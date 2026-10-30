#!/usr/bin/env python3
"""
Personale Import Service - Container Apps version
Ascolta coda Service Bus e processa richieste import personale
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import logging
import requests
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

# Import del modulo principale
from import_personale_sal7 import importa_personale_da_azure_sal7

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

def find_personale_file(files_data):
    """
    Trova il file del personale dalla lista dei file

    Args:
        files_data (dict): Dati dei file dall'API

    Returns:
        str: Path del file personale o None se non trovato
    """
    if not files_data or not files_data.get('files'):
        return None

    for file_info in files_data['files']:
        if file_info.get('type') == 'personale':
            return file_info.get('path')

    # Fallback: cerca per nome contenente "personale"
    for file_info in files_data['files']:
        if 'personale' in file_info.get('name', '').lower():
            return file_info.get('path')

    return None

def send_completion_message(service_bus_conn, sal_name, success, details, queue_name):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'personale-import',
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
                sender = client.get_queue_sender(queue_name=queue_name)
                with sender:
                    message = ServiceBusMessage(json.dumps(message_data))
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

def process_import_request(message_data):
    """Process a single import request"""
    # Extract data from message
    sal_storage_name = message_data.get('sal_storage_name')
    request_id = message_data.get('request_id', 'unknown')

    # Get environment variables
    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')

    # Database Configuration
    db_config = {
        'host': os.getenv('DB_HOST', 'colibri-mysql-flexible.mysql.database.azure.com'),
        'port': int(os.getenv('DB_PORT', '3306')),
        'user': os.getenv('DB_USER', 'polisammin'),
        'password': os.getenv('DB_PASSWORD', 'ComplexPass123!'),
        'database': os.getenv('DB_DATABASE', 'polis')
    }

    # Service Bus Configuration
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')

    logger.info("="*60)
    logger.info("PERSONALE IMPORT SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL Storage: {sal_storage_name}")
    logger.info(f"[{request_id}] Processing SAL: {sal_storage_name}")

    # Validate required environment variables
    required_vars = {
        'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or SERVICE_BUS),
        'DB_HOST': db_config['host'],
        'DB_USER': db_config['user'],
        'DB_PASSWORD': db_config['password'],
        'DB_DATABASE': db_config['database'],
        'RESPONSE_QUEUE': RESPONSE_QUEUE
    }

    missing_vars = [var for var, value in required_vars.items() if value is None]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise Exception(f"Missing environment variables: {', '.join(missing_vars)}")

    if not sal_storage_name:
        raise Exception("sal_storage_name not provided in message")

    try:
        # Ottieni i file Excel per questo SAL
        logger.info(f"[{request_id}] Getting Excel files information from API...")
        files_data = get_excel_files_for_sal(sal_storage_name)
        if not files_data:
            error_msg = f"Failed to get Excel files for SAL: {sal_storage_name}"
            logger.error(f"[{request_id}] {error_msg}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)
            return

        # Trova il file del personale
        personale_file_path = find_personale_file(files_data)
        if not personale_file_path:
            error_msg = f"No personale file found for SAL {sal_storage_name}"
            logger.error(f"[{request_id}] {error_msg}")
            logger.info(f"[{request_id}] Available files: {[f.get('name') for f in files_data.get('files', [])]}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)
            return

        logger.info(f"[{request_id}] Found personale file: {personale_file_path}")

        # Processa il file del personale
        logger.info(f"[{request_id}] Processing file: {personale_file_path}")

        # Chiama la funzione di import con il path reale ottenuto dalle API
        result = importa_personale_da_azure_sal7(personale_file_path, db_config)

        # Log del risultato
        if result.get('success'):
            logger.info(f"[{request_id}] Import completed successfully")

            # Log statistiche se disponibili
            summary = result.get('summary', {})
            failed_rows = summary.get('rows_failed', 0)

            if 'summary' in result:
                logger.info(f"[{request_id}] Statistics:")
                logger.info(f"[{request_id}]   - Total rows: {summary.get('rows_total', 0)}")
                logger.info(f"[{request_id}]   - Imported rows: {summary.get('rows_imported', 0)}")
                logger.info(f"[{request_id}]   - Failed rows: {failed_rows}")
                logger.info(f"[{request_id}]   - Success rate: {summary.get('success_rate', 0):.2f}%")
                logger.info(f"[{request_id}]   - Duration: {summary.get('duration', 0):.2f} seconds")

            # Determina il successo basato sulle righe fallite
            import_success = (failed_rows == 0)
            status_message = "Import completed successfully with no failed rows" if import_success else f"Import completed but {failed_rows} rows failed"

            # Success
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ IMPORT COMPLETED")
            logger.info(f"[{request_id}]    Total rows: {summary.get('rows_total', 0)}")
            logger.info(f"[{request_id}]    Imported: {summary.get('rows_imported', 0)}")
            logger.info(f"[{request_id}]    Failed: {failed_rows}")
            logger.info("="*60)

            # Invia messaggio di completamento
            send_completion_message(SERVICE_BUS, sal_storage_name, import_success, status_message, RESPONSE_QUEUE)

        else:
            error_msg = result.get('message', 'Unknown error')
            logger.error(f"[{request_id}] Import failed: {error_msg}")

            # Invia messaggio di errore
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)

        # Wait before completing
        logger.info(f"[{request_id}] Waiting 5 seconds...")
        time.sleep(5)

        logger.info(f"[{request_id}] ✅ Message completed")

    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        send_completion_message(SERVICE_BUS, sal_storage_name, False, str(e), RESPONSE_QUEUE)
        raise

def listen_for_import_requests():
    """Listen for import requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('REQUEST_QUEUE', 'import-sal-personale')

    logger.info("🚀 Starting queue processor...")
    logger.info(f"📡 Listening for messages on queue: {REQUEST_QUEUE}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing':
        logger.error("SERVICE_BUS_CONNECTION_STRING not configured")
        sys.exit(1)

    try:
        client = get_servicebus_client()
        with client:
            receiver = client.get_queue_receiver(queue_name=REQUEST_QUEUE, receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE)
            with receiver:
                logger.info(f"✅ Connected to queue: {REQUEST_QUEUE}")

                for message in receiver:
                    try:
                        logger.info("📨 Received message: " + str(message))

                        # Parse message
                        message_data = json.loads(str(message))

                        # Process the import request
                        process_import_request(message_data)

                        # Auto-deleted in RECEIVE_AND_DELETE mode

                    except Exception as e:
                        logger.error(f"❌ Error processing message: {e}")
                        import traceback
                        logger.error(f"Full traceback: {traceback.format_exc()}")
                        # Message already removed

    except Exception as e:
        logger.error(f"❌ Fatal error in queue listener: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)

def main():
    """Main function"""
    try:
        listen_for_import_requests()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()