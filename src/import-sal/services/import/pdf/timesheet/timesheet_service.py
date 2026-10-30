#!/usr/bin/env python3
"""
Timesheet Import Service - Azure Container App version
Ascolta coda Service Bus e processa richieste import Timesheet PDF con parallelizzazione e dynamic scaling.
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import logging
import time
from datetime import datetime
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusReceiveMode

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Import wrapper functions
from timesheet_wrapper import run_import, test_connections, count_pdfs_to_import, calculate_optimal_workers

SERVICE_TASK_NAME = "timesheet-import"


def send_completion_message(service_bus_conn: str, sal_name: str, success: bool, details: str, queue_name: str, metadata=None) -> bool:
    """Invia messaggio di completamento all'orchestratore con retry."""
    message_data = {
        'task': SERVICE_TASK_NAME,
        'sal': sal_name,
        'status': 'success' if success else 'failed',
        'details': details,
        'timestamp': datetime.now().isoformat()
    }
    if metadata:
        message_data['metadata'] = metadata

    for attempt in range(5):
        try:
            logger.info(f"Invio completion message (tentativo {attempt+1}/5)...")
            client = get_servicebus_client()
            with client:
                sender = client.get_queue_sender(queue_name=queue_name)
                with sender:
                    sender.send_messages(ServiceBusMessage(json.dumps(message_data)))
            time.sleep(2)
            logger.info("✅ Completion message inviato!")
            return True
        except Exception as e:
            logger.error(f"Tentativo {attempt+1} fallito: {e}")
            if attempt < 4:
                time.sleep(2 ** attempt)
    logger.error("❌ Impossibile inviare completion message dopo 5 tentativi")
    return False


def process_import_request(message_data: dict):
    """Processa una singola richiesta di import timesheet.
    Il messaggio deve contenere almeno sal_storage_name.
    """
    sal_storage_name = message_data.get('sal_storage_name')
    request_id = message_data.get('request_id', 'unknown')

    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')
    MAX_WORKERS_ENV = int(os.getenv('MAX_WORKERS', '4'))

    logger.info("="*60)
    logger.info("TIMESHEET PDF IMPORT SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL Storage: {sal_storage_name}")
    logger.info(f"[{request_id}] Max Workers (env): {MAX_WORKERS_ENV}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing' or not RESPONSE_QUEUE:
        missing = []
        if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing': missing.append('SERVICE_BUS_CONNECTION_STRING')
        if not RESPONSE_QUEUE: missing.append('RESPONSE_QUEUE')
        raise RuntimeError(f"Environment variables mancanti: {', '.join(missing)}")
    if not sal_storage_name:
        raise RuntimeError("sal_storage_name non presente nel messaggio")

    try:
        # Set SAL folder env for ocr_common
        os.environ['SAL_STORAGE_NAME'] = sal_storage_name

        logger.info(f"[{request_id}] Test connessioni...")
        if not test_connections():
            err = "Connessioni DB/API fallite"
            logger.error(f"[{request_id}] {err}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, err, RESPONSE_QUEUE)
            return

        # Dynamic scaling analysis
        logger.info("="*60)
        logger.info(f"[{request_id}] 📊 DYNAMIC SCALING ANALYSIS")
        logger.info("="*60)
        total_pdf_count = count_pdfs_to_import()
        logger.info(f"[{request_id}] Total PDFs trovati: {total_pdf_count}")

        if total_pdf_count == 0:
            metadata = {'total_pdfs': 0, 'workers_used': 0, 'scaling_decision': 'none'}
            logger.warning(f"[{request_id}] Nessun PDF da importare")
            send_completion_message(SERVICE_BUS, sal_storage_name, True, "Nessun timesheet da importare", RESPONSE_QUEUE, metadata)
            return

        optimal_workers, suggested_batch = calculate_optimal_workers(total_pdf_count, MAX_WORKERS_ENV)
        workers_to_use = optimal_workers if optimal_workers > MAX_WORKERS_ENV else MAX_WORKERS_ENV

        logger.info(f"[{request_id}] 📈 SCALING DECISION:")
        logger.info(f"[{request_id}]   Total PDFs: {total_pdf_count}")
        logger.info(f"[{request_id}]   Optimal workers: {optimal_workers}")
        logger.info(f"[{request_id}]   Suggested batch size: {suggested_batch}")
        logger.info(f"[{request_id}]   Env max workers: {MAX_WORKERS_ENV}")
        logger.info(f"[{request_id}]   ✅ Workers finali: {workers_to_use}")

        logger.info(f"[{request_id}] Avvio import parallelo timesheet...")
        success = run_import(limit=None,
                             start_index=None,
                             batch_size=None,
                             max_workers=workers_to_use,
                             auto_scale=False,  # scaling già deciso sopra
                             dry_run=False)

        metadata = {
            'total_pdfs': total_pdf_count,
            'workers_used': workers_to_use,
            'optimal_workers_calculated': optimal_workers,
            'suggested_batch_size': suggested_batch,
            'scaling_decision': 'auto' if optimal_workers > MAX_WORKERS_ENV else 'env'
        }

        if success:
            msg = f"Import completato: {total_pdf_count} PDF con {workers_to_use} workers"
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ TIMESHEET IMPORT COMPLETED")
            logger.info(f"[{request_id}] PDF totali processati: {total_pdf_count}")
            logger.info(f"[{request_id}] Workers usati: {workers_to_use}")
            logger.info("="*60)
            send_completion_message(SERVICE_BUS, sal_storage_name, True, msg, RESPONSE_QUEUE, metadata)
        else:
            err = "Timesheet import fallito - controllare i log"
            logger.error(f"[{request_id}] {err}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, err, RESPONSE_QUEUE, metadata)

        logger.info(f"[{request_id}] Attesa 5s prima di completare il messaggio...")
        time.sleep(5)
        logger.info(f"[{request_id}] ✅ Messaggio completato")

    except Exception as e:
        import traceback
        logger.error(f"[{request_id}] ERRORE: {e}")
        logger.error(traceback.format_exc())
        metadata = {'error_type': type(e).__name__, 'error_message': str(e)}
        send_completion_message(SERVICE_BUS, sal_storage_name or 'unknown', False, str(e), RESPONSE_QUEUE, metadata)
        raise


def listen_for_import_requests():
    """Loop principale di ascolto della coda Service Bus per richieste import timesheet."""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('REQUEST_QUEUE', 'import-sal-timesheet')

    logger.info("🚀 Avvio Timesheet import queue processor...")
    logger.info(f"📡 In ascolto sulla coda: {REQUEST_QUEUE}")
    logger.info(f"⚙️  Max parallel workers (env): {os.getenv('MAX_WORKERS', '4')}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing':
        logger.error("Variabile SERVICE_BUS_CONNECTION_STRING non configurata")
        sys.exit(1)

    try:
        client = get_servicebus_client()
        with client:
            receiver = client.get_queue_receiver(queue_name=REQUEST_QUEUE, receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE)
            with receiver:
                logger.info(f"✅ Connesso alla coda: {REQUEST_QUEUE}")
                logger.info("⏳ In attesa di messaggi...")
                for message in receiver:
                    try:
                        logger.info("="*60)
                        logger.info("📨 NUOVO MESSAGGIO RICEVUTO")
                        logger.info("="*60)
                        logger.info(f"Contenuto: {str(message)}")
                        data = json.loads(str(message))
                        process_import_request(data)
                        # In RECEIVE_AND_DELETE, il messaggio è già rimosso
                        logger.info("✅ Messaggio gestito (auto-delete)")
                    except json.JSONDecodeError as e:
                        logger.error(f"❌ JSON non valido nel messaggio: {e}")
                        logger.error(f"Contenuto: {str(message)}")
                        # Messaggio già rimosso
                    except Exception as e:
                        logger.error(f"❌ Errore elaborando messaggio: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                        # Messaggio già rimosso
    except KeyboardInterrupt:
        logger.info("Chiusura gracile (Ctrl+C)")
    except Exception as e:
        logger.error(f"Errore fatale nel service: {e}")
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    logger.info("="*60)
    logger.info("TIMESHEET IMPORT SERVICE - STARTING")
    logger.info("="*60)
    listen_for_import_requests()
