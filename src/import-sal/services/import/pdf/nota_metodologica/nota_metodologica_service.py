#!/usr/bin/env python3
"""
Nota Metodologica Import Service - Azure Container Apps
Ascolta coda Service Bus e processa richieste import Nota Metodologica PDF con parallelizzazione dinamica
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import logging
import time
from datetime import datetime
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusReceiveMode

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Import del wrapper che gestisce la logica
from nota_metodologica_ocr_wrapper import run_import, test_connections, count_pdfs_to_import, calculate_optimal_workers

def send_completion_message(service_bus_conn, sal_name, success, details, queue_name, metadata=None):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'nota-metodologica-import',
        'sal': sal_name,
        'status': 'success' if success else 'failed',
        'details': details,
        'timestamp': datetime.now().isoformat()
    }
    if metadata:
        message_data['metadata'] = metadata

    for attempt in range(5):
        try:
            logger.info(f"Sending completion message (attempt {attempt + 1}/5)...")
            client = get_servicebus_client()
            with client:
                sender = client.get_queue_sender(queue_name=queue_name)
                with sender:
                    message = ServiceBusMessage(json.dumps(message_data))
                    sender.send_messages(message)
            time.sleep(2)
            logger.info("✅ Completion message sent!")
            return True
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < 4:
                time.sleep(2 ** attempt)

    logger.error("❌ Failed to send completion message")
    return False


def process_import_request(message_data):
    """Process a single Nota Metodologica import request"""
    sal_storage_name = message_data.get('sal_storage_name')
    request_id = message_data.get('request_id', 'unknown')

    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')
    MAX_WORKERS = int(os.getenv('MAX_WORKERS', '4'))

    logger.info("="*60)
    logger.info("NOTA METODOLOGICA PDF IMPORT SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL Storage: {sal_storage_name}")
    logger.info(f"[{request_id}] Max Workers: {MAX_WORKERS}")

    required_vars = {
        'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or SERVICE_BUS),
        'RESPONSE_QUEUE': RESPONSE_QUEUE
    }
    missing_vars = [var for var, value in required_vars.items() if value is None]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise Exception(f"Missing environment variables: {', '.join(missing_vars)}")

    if not sal_storage_name:
        raise Exception("sal_storage_name not provided in message")

    try:
        # Set SAL folder env for ocr_common
        os.environ['SAL_STORAGE_NAME'] = sal_storage_name

        logger.info(f"[{request_id}] Testing connections...")
        connections_ok = test_connections()
        if not connections_ok:
            error_msg = "Failed to establish connections to database/API"
            logger.error(f"[{request_id}] {error_msg}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)
            return

        # Dynamic scaling analysis
        logger.info("="*60)
        logger.info(f"[{request_id}] 📊 DYNAMIC SCALING ANALYSIS")
        logger.info("="*60)

        total_pdf_count = count_pdfs_to_import()
        logger.info(f"[{request_id}] Total PDFs found: {total_pdf_count}")

        if total_pdf_count == 0:
            logger.warning(f"[{request_id}] No PDFs to import. Completing with success.")
            metadata = {'total_pdfs': 0, 'workers_used': 0, 'scaling_decision': 'none'}
            send_completion_message(SERVICE_BUS, sal_storage_name, True, "No PDFs to import", RESPONSE_QUEUE, metadata)
            return

        optimal_workers, suggested_batch = calculate_optimal_workers(total_pdf_count, MAX_WORKERS)
        logger.info(f"[{request_id}] 📈 SCALING DECISION:")
        logger.info(f"[{request_id}]    Total PDFs: {total_pdf_count}")
        logger.info(f"[{request_id}]    Optimal workers: {optimal_workers}")
        logger.info(f"[{request_id}]    Suggested batch size: {suggested_batch}")
        logger.info(f"[{request_id}]    Max workers from env: {MAX_WORKERS}")

        workers_to_use = optimal_workers if optimal_workers > MAX_WORKERS else MAX_WORKERS
        logger.info(f"[{request_id}]    ✅ Final workers to use: {workers_to_use}")
        logger.info("="*60)

        logger.info(f"[{request_id}] Starting Nota Metodologica PDF import...")
        logger.info(f"[{request_id}] Processing with {workers_to_use} parallel workers")
        logger.info(f"[{request_id}] Total PDFs to process: {total_pdf_count}")

        success = run_import(
            sal_folder=sal_storage_name,
            start_index=None,
            batch_size=None,
            max_workers=workers_to_use,
            auto_scale=True
        )

        metadata = {
            'total_pdfs': total_pdf_count,
            'workers_used': workers_to_use,
            'optimal_workers_calculated': optimal_workers,
            'suggested_batch_size': suggested_batch,
            'scaling_decision': 'auto' if optimal_workers > MAX_WORKERS else 'manual'
        }

        if success:
            status_message = f"Import completed: {total_pdf_count} PDFs processed with {workers_to_use} workers"
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ NOTA METODOLOGICA IMPORT COMPLETED")
            logger.info(f"[{request_id}] Total PDFs processed: {total_pdf_count}")
            logger.info(f"[{request_id}] Workers used: {workers_to_use}")
            logger.info("="*60)
            send_completion_message(SERVICE_BUS, sal_storage_name, True, status_message, RESPONSE_QUEUE, metadata)
        else:
            error_msg = "Nota Metodologica PDF import failed - check logs for details"
            logger.error(f"[{request_id}] {error_msg}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE, metadata)

        logger.info(f"[{request_id}] Waiting 5 seconds...")
        time.sleep(5)
        logger.info(f"[{request_id}] ✅ Message completed")

    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        metadata = {'error_type': type(e).__name__, 'error_message': str(e)}
        send_completion_message(SERVICE_BUS, sal_storage_name, False, str(e), RESPONSE_QUEUE, metadata)
        raise


def listen_for_import_requests():
    """Listen for import requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('REQUEST_QUEUE', 'import-sal-nota-metodologica')

    logger.info("🚀 Starting Nota Metodologica import queue processor...")
    logger.info(f"📡 Listening for messages on queue: {REQUEST_QUEUE}")
    logger.info(f"⚙️  Max parallel workers: {os.getenv('MAX_WORKERS', '4')}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing':
        raise Exception("SERVICE_BUS_CONNECTION_STRING not set")

    from azure.servicebus.exceptions import ServiceBusError

    while True:  # Loop infinito con riconnessione automatica
        try:
            with get_servicebus_client() as client:
                with client.get_queue_receiver(
                    queue_name=REQUEST_QUEUE,
                    max_wait_time=60,
                    receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE
                ) as receiver:
                    logger.info("✅ Connected to queue, waiting for messages...")
                    for msg in receiver:
                        try:
                            logger.info("=============================================================")
                            logger.info("📨 NEW MESSAGE RECEIVED")
                            logger.info("=============================================================")
                            data = json.loads(str(msg))
                            process_import_request(data)
                            # Auto-deleted in RECEIVE_AND_DELETE mode
                        except Exception as e:
                            logger.error(f"Processing failed: {e}")
                            import traceback
                            logger.error(f"Full traceback: {traceback.format_exc()}")
                            # Message already removed

        except ServiceBusError as e:
            logger.info(f"Service Bus error occurred: {e}")
            logger.info("Reconnecting to Service Bus in 2 seconds...")
            time.sleep(2)
            continue

        except Exception as e:
            logger.error(f"Fatal error in listener: {e}")
            import traceback
            logger.error(f"Full traceback: {traceback.format_exc()}")
            logger.info("Attempting to reconnect in 5 seconds...")
            time.sleep(5)
            continue

if __name__ == "__main__":
    try:
        listen_for_import_requests()
    except KeyboardInterrupt:
        logger.info("Service interrupted by user")
    except Exception as e:
        logger.error(f"Fatal service error: {e}")
        sys.exit(1)
