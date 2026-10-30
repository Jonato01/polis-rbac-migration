#!/usr/bin/env python3
"""
Entry point per il container Excel Processor Fatture
Legge i parametri dall'environment e chiama import_fatture_sal7
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import logging
import requests
import argparse
import time
from datetime import datetime
from azure.servicebus import ServiceBusClient, ServiceBusMessage

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import del modulo principale
from import_fatture_sal7 import ImportatoreFattureSAL7, importa_fatture_da_azure_sal7

def make_json_safe(obj):
    """
    Recursively convert datetime objects to ISO format strings for JSON serialization

    Args:
        obj: Object to make JSON-safe

    Returns:
        JSON-safe version of the object
    """
    if isinstance(obj, datetime):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: make_json_safe(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [make_json_safe(item) for item in obj]
    else:
        return obj

def get_latest_sal_info():
    """
    Chiama l'API per ottenere le informazioni sull'ultimo SAL

    Returns:
        dict: Informazioni sul SAL o None se errore
    """
    try:
        base_url = os.environ.get('AZURE_STORAGE',
                                  "https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api")
        url = f"{base_url}/sal/latest"
        headers = {"accept": "application/json"}

        logger.info(f"Calling SAL API: {url}")
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()

        data = response.json()
        if data.get('success'):
            logger.info(f"Latest SAL: {data.get('latest_sal')}")
            return data
        else:
            logger.error(f"SAL API returned error: {data}")
            return None

    except Exception as e:
        logger.error(f"Error calling SAL API: {e}")
        return None


def get_excel_files_for_sal(sal_path):
    """
    Chiama l'API per ottenere i file Excel per un SAL specifico

    Args:
        sal_path (str): Path del SAL (es: "8_SAL_30_04_2025")

    Returns:
        dict: Informazioni sui file Excel o None se errore
    """
    try:
        base_url = os.environ.get('POLIS_API_DATABASE_URL',
                                    "https://polis-api-database-f8fvgqfgd3gpdthd.westeurope-01.azurewebsites.net") + "/api"
        url = f"{base_url}/v1/check-excel-files/{sal_path}"
        headers = {"accept": "application/json"}

        logger.info(f"Calling Excel files API: {url}")
        response = requests.get(url, headers=headers, timeout=600)
        response.raise_for_status()

        data = response.json()
        if data.get('success'):
            logger.info(f"Found {len(data.get('files', []))} Excel files")
            return data
        else:
            logger.error(f"Excel files API returned error: {data}")
            return None

    except Exception as e:
        logger.error(f"Error calling Excel files API: {e}")
        return None


def send_completion_message(service_bus_conn, sal_name, success, details):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'fatture-import',
        'sal': sal_name,
        'status': 'success' if success else 'failed',
        'details': details,
        'timestamp': datetime.now().isoformat()
    }

    for attempt in range(5):
        try:
            logger.info(f"Sending message (attempt {attempt + 1}/5)...")
            client = get_servicebus_client()
            with client:
                sender = client.get_queue_sender(queue_name="orchestrator-responses")
                with sender:
                    message = ServiceBusMessage(json.dumps(make_json_safe(message_data)))
                    sender.send_messages(message)

            time.sleep(2)  # Wait to ensure sent
            logger.info("✅ Message sent!")
            return True
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < 4:
                time.sleep(2 ** attempt)

    logger.error("❌ Failed to send message")
    return False


def main():
    """
    Main entry point per il container
    """
    parser = argparse.ArgumentParser(description='Importa fatture SAL da Excel nel database')
    parser.add_argument('SAL_STORAGE_NAME', nargs='?', default=None,
                        help='Argomento SAL (es: "8_SAL_30_04_2025")')
    parser.add_argument('--sal', type=int, help='Numero del SAL da importare')
    parser.add_argument('--file', type=str, help='Path del file Excel da importare')

    args = parser.parse_args()

    # Se non è stato passato un argomento SAL, prova a ottenerlo dall'environment
    sal_argument = args.SAL_STORAGE_NAME or os.environ.get('SAL_STORAGE_NAME')

    if not sal_argument and not args.sal:
        logger.info("Nessun argomento SAL specificato, provo a ottenere l'ultimo SAL dall'API")
        sal_info = get_latest_sal_info()
        if sal_info and sal_info.get('latest_sal'):
            sal_number = sal_info['latest_sal']
            logger.info(f"Usando SAL {sal_number} dall'API")
        else:
            logger.error("Impossibile ottenere informazioni sul SAL dall'API")
            return 1
    elif args.sal:
        sal_number = args.sal
    else:
        # Estrai il numero SAL dall'argomento (es: "8_SAL_30_04_2025" -> 8)
        try:
            sal_number = int(sal_argument.split('_')[0])
            logger.info(f"Estratto numero SAL: {sal_number} da argomento: {sal_argument}")
        except (ValueError, AttributeError, IndexError):
            logger.error(f"Formato argomento SAL non valido: {sal_argument}")
            return 1

    # Verifica che il SAL sia >= 7 (supportato da questo script)
    if sal_number < 7:
        logger.error(f"SAL {sal_number} non supportato. Questo script supporta solo SAL >= 7")
        return 1

    # Configurazione database
    DB_CONFIG = {
        'host': os.environ.get('DB_HOST', 'localhost'),
        'port': int(os.environ.get('DB_PORT', 3306)),
        'user': os.environ.get('DB_USER', 'root'),
        'password': os.environ.get('DB_PASSWORD', 'divi99784'),
        'database': os.environ.get('DB_NAME', 'polis')
    }

    logger.info("=" * 80)
    logger.info("AVVIO IMPORTAZIONE FATTURE SAL 7+ DA AZURE STORAGE")
    logger.info(f"SAL: {sal_number}")
    logger.info(f"Database: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")
    logger.info("=" * 80)

    # Se è specificato un file locale, usa la logica tradizionale
    if args.file and os.path.exists(args.file):
        logger.info(f"Usando file locale specificato: {args.file}")

        # Logica tradizionale per file locali
        importatore = ImportatoreFattureSAL7(**DB_CONFIG)
        try:
            if not importatore.connetti_db():
                logger.error("Impossibile connettersi al database")
                return 1

            importatore.carica_cache()

            # Verifico esistenza SAL, altrimenti lo creo
            if sal_number not in importatore.sal_cache:
                logger.info(f"Inserimento SAL {sal_number} nel database")

                # Date per SAL 7+ (ogni SAL copre 4 mesi a partire da settembre 2024)
                from datetime import date
                from dateutil.relativedelta import relativedelta

                # SAL 7 inizia il 1° settembre 2024
                data_inizio_sal7 = date(2024, 9, 1)

                # Calcola l'inizio del SAL corrente
                mesi_dal_sal7 = (sal_number - 7) * 4
                data_inizio = data_inizio_sal7 + relativedelta(months=mesi_dal_sal7)
                data_fine = data_inizio + relativedelta(months=4, days=-1)

                q = """INSERT INTO sal (numero_sal, data_inizio, data_fine)
                       VALUES (%s, %s, %s)"""
                importatore.cursor.execute(q, (sal_number, data_inizio, data_fine))
                importatore.connection.commit()
                importatore.carica_cache()

            # IMPORTAZIONE SICURA SAL 7+ da file locale
            import_result = importatore.importa_excel_sicuro(args.file, numero_sal=sal_number)

            # Ottieni statistiche errori per determinare il successo reale
            failed_rows = 0
            success_details = "Importazione completata"

            if hasattr(importatore, 'error_handler') and importatore.error_handler:
                stats = importatore.error_handler.get_statistiche_errori()
                if stats:
                    total_errors = sum(conteggio for tipo_errore, conteggio, da_correggere, corretti in stats)
                    failed_rows = total_errors
                    success_details = f"Importazione completata con {failed_rows} righe con errori"

            # Determina il successo basato sulle righe fallite
            actual_success = import_result and failed_rows == 0

        except Exception as e:
            logger.error(f"Errore nell'importazione da file locale: {e}")
            import traceback
            logger.error(traceback.format_exc())
            actual_success = False
            success_details = f"Errore durante l'importazione: {str(e)}"
        finally:
            importatore.chiudi_connessione()

    else:
        # NUOVA LOGICA: Importazione diretta da Azure Storage
        logger.info("Importazione diretta da Azure Storage")

        # Determina il path Azure del file delle fatture
        azure_file_path = None

        if sal_argument:
            # Prova a ottenere i file Excel dall'API
            logger.info(f"Cercando file Excel per SAL {sal_argument} tramite API...")
            excel_files_info = get_excel_files_for_sal(sal_argument)

            if excel_files_info and excel_files_info.get('files'):
                # Cerca il file delle fatture/spese tra quelli disponibili
                files = excel_files_info['files']

                # Cerca file che contengono "fatture", "spese" o "riepilogo" nel nome
                for file_info in files:
                    filename = file_info.get('name', '').lower()
                    if any(keyword in filename for keyword in ['fatture', 'spese', 'riepilogo']):
                        # Usa full_path se disponibile, altrimenti costruisci il path
                        azure_file_path = file_info.get('full_path') or file_info.get('path') or f"{sal_argument}/{file_info.get('name')}"
                        logger.info(f"Trovato file fatture su Azure: {azure_file_path}")
                        logger.debug(f"File info completo: {file_info}")
                        break

                if not azure_file_path:
                    logger.warning("Nessun file fatture trovato dall'API, uso il primo file disponibile")
                    first_file_info = files[0]
                    azure_file_path = first_file_info.get('full_path') or first_file_info.get('path') or f"{sal_argument}/{first_file_info.get('name')}"
            else:
                logger.warning("Nessun file Excel trovato dall'API, uso il path di default")
                # Path di default basato sul pattern standard
                azure_file_path = f"{sal_argument}/Riepilogo analitico spese sostenute {sal_number} SAL.xlsx"
        else:
            # Fallback se non abbiamo sal_argument
            logger.warning("Nessun sal_argument disponibile, uso path di default")
            azure_file_path = f"{sal_number}_SAL/Riepilogo analitico spese sostenute {sal_number} SAL.xlsx"

        if not azure_file_path:
            logger.error("Impossibile determinare il path del file su Azure Storage")
            return 1

        logger.info(f"Path Azure del file fatture: {azure_file_path}")

        # IMPORTAZIONE DA AZURE STORAGE
        try:
            result = importa_fatture_da_azure_sal7(azure_file_path, DB_CONFIG)

            actual_success = result.get('success', False)
            success_details = result.get('message', 'Importazione completata')

            if actual_success:
                logger.info(f"✅ Importazione da Azure completata con successo per SAL {result.get('numero_sal')}")
            else:
                logger.error(f"❌ Importazione da Azure fallita: {result.get('error', 'Errore sconosciuto')}")

        except Exception as e:
            logger.error(f"Errore nell'importazione da Azure Storage: {e}")
            import traceback
            logger.error(traceback.format_exc())
            actual_success = False
            success_details = f"Errore durante l'importazione da Azure: {str(e)}"

    # Invia sempre il messaggio di completamento a Service Bus
    service_bus_conn = os.environ.get('SERVICE_BUS_CONNECTION_STRING')
    if service_bus_conn:
        send_completion_message(
            service_bus_conn,
            sal_argument,
            actual_success,
            success_details
        )

    if actual_success:
        logger.info("✅ IMPORTAZIONE FATTURE SAL 7+ COMPLETATA CON SUCCESSO")
        return 0
    else:
        logger.error("❌ IMPORTAZIONE FATTURE SAL 7+ FALLITA")
        return 1

if __name__ == "__main__":
    sys.exit(main())
