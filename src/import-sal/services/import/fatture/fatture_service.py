#!/usr/bin/env python3
"""
Fatture Service - Container Apps version
Ascolta coda Service Bus e processa richieste importazione fatture
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import time
import logging
import requests
from datetime import datetime
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusReceiveMode
from azure.storage.blob import BlobServiceClient
from import_fatture_sal7 import ImportatoreFattureSAL7, importa_fatture_da_azure_sal7

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

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
        response = requests.get(url, headers=headers, timeout=200)
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

def send_completion_message(service_bus_conn, sal_name, success, details, queue_name):
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
                sender = client.get_queue_sender(queue_name=queue_name)
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

def process_fatture_request(message_data):
    """Process a single fatture import request"""
    # Extract data from message
    sal_storage_name = message_data.get('sal_storage_name')
    request_id = message_data.get('request_id', 'unknown')

    # Get environment variables
    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    STORAGE_CONN = os.getenv('AZURE_STORAGE_CONNECTION_STRING')

    # Database Configuration
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = int(os.getenv('DB_PORT', 3306))
    DB_USER = os.getenv('DB_USER')
    DB_PASSWORD = os.getenv('DB_PASSWORD')
    DB_NAME = os.getenv('DB_NAME', 'polis')

    # Storage Configuration
    STORAGE_CONTAINER = os.getenv('STORAGE_CONTAINER')

    # Service Bus Configuration
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')

    logger.info("="*60)
    logger.info("FATTURE IMPORT SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL Storage: {sal_storage_name}")

    # Validate required environment variables
    required_vars = {
        'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or SERVICE_BUS),
        'AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING': (os.getenv('AZURE_STORAGE_ACCOUNT_NAME') or STORAGE_CONN),
        'DB_HOST': DB_HOST,
        'DB_USER': DB_USER,
        'DB_PASSWORD': DB_PASSWORD,
        'DB_NAME': DB_NAME,
        'STORAGE_CONTAINER': STORAGE_CONTAINER,
        'RESPONSE_QUEUE': RESPONSE_QUEUE
    }

    missing_vars = [var for var, value in required_vars.items() if value is None]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise Exception(f"Missing environment variables: {', '.join(missing_vars)}")

    if not sal_storage_name:
        raise Exception("sal_storage_name not provided in message")

    # Extract SAL number from sal_storage_name (e.g., "8_SAL_30_04_2025" -> 8)
    try:
        sal_number = int(sal_storage_name.split('_')[0])
        logger.info(f"[{request_id}] Extracted SAL number: {sal_number}")
    except (ValueError, AttributeError, IndexError):
        logger.error(f"[{request_id}] Could not extract SAL number from: {sal_storage_name}")
        error_msg = f"Invalid SAL storage name format: {sal_storage_name}"
        send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)
        return

    # Verify SAL >= 7
    if sal_number < 7:
        error_msg = f"SAL {sal_number} not supported. This service supports only SAL >= 7"
        logger.error(f"[{request_id}] {error_msg}")
        send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)
        return

    try:
        # Determina il path Azure del file delle fatture usando lo stesso metodo del processor
        azure_file_path = None

        logger.info(f"[{request_id}] Cercando file Excel per SAL {sal_storage_name} tramite API...")
        excel_files_info = get_excel_files_for_sal(sal_storage_name)

        if excel_files_info and excel_files_info.get('files'):
            # Cerca il file delle fatture/spese tra quelli disponibili
            files = excel_files_info['files']

            # Cerca file che contengono "fatture", "spese" o "riepilogo" nel nome
            for file_info in files:
                filename = file_info.get('name', '').lower()
                if any(keyword in filename for keyword in ['fatture', 'spese', 'riepilogo']):
                    # Usa full_path se disponibile, altrimenti costruisci il path
                    azure_file_path = file_info.get('full_path') or file_info.get('path') or f"{sal_storage_name}/{file_info.get('name')}"
                    logger.info(f"[{request_id}] Trovato file fatture su Azure: {azure_file_path}")
                    logger.debug(f"[{request_id}] File info completo: {file_info}")
                    break

            if not azure_file_path:
                logger.warning(f"[{request_id}] Nessun file fatture trovato dall'API, uso il primo file disponibile")
                first_file_info = files[0]
                azure_file_path = first_file_info.get('full_path') or first_file_info.get('path') or f"{sal_storage_name}/{first_file_info.get('name')}"
        else:
            logger.warning(f"[{request_id}] Nessun file Excel trovato dall'API, uso il path di default")
            # Path di default basato sul pattern standard
            azure_file_path = f"{sal_storage_name}/Riepilogo analitico spese sostenute {sal_number} SAL.xlsx"

        if not azure_file_path:
            error_msg = "Impossibile determinare il path del file su Azure Storage"
            logger.error(f"[{request_id}] {error_msg}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)
            return

        logger.info(f"[{request_id}] Path Azure del file fatture: {azure_file_path}")

        # Database configuration
        db_config = {
            'host': DB_HOST,
            'port': DB_PORT,
            'user': DB_USER,
            'password': DB_PASSWORD,
            'database': DB_NAME
        }

        logger.info(f"[{request_id}] Starting fatture import from Azure Storage...")
        logger.info(f"[{request_id}] Database: {DB_HOST}:{DB_PORT}/{DB_NAME}")

        # IMPORTAZIONE DA AZURE STORAGE usando lo stesso metodo del processor
        result = importa_fatture_da_azure_sal7(azure_file_path, db_config)

        actual_success = result.get('success', False)
        success_details = result.get('message', 'Importazione completata')

        if actual_success:
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ IMPORT COMPLETED")
            logger.info(f"[{request_id}]    SAL Number: {result.get('numero_sal')}")
            logger.info(f"[{request_id}]    Message: {success_details}")
            logger.info("="*60)

            send_completion_message(SERVICE_BUS, sal_storage_name, True,
                                  success_details, RESPONSE_QUEUE)
        else:
            error_msg = result.get('error', 'Errore sconosciuto')
            logger.error(f"[{request_id}] ❌ Import failed: {error_msg}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)

        logger.info(f"[{request_id}] ✅ Processing completed")

    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        error_msg = f"Errore durante l'importazione: {str(e)}"
        send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)
        raise

def listen_for_fatture_requests():
    """Listen for fatture import requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('FATTURE_SERVICE_QUEUE', 'import-sal-fatture')

    logger.info("🚀 Starting queue processor...")
    logger.info(f"📡 Listening for messages on queue: {REQUEST_QUEUE}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing':
        logger.error("SERVICE_BUS_CONNECTION_STRING not configured")
        sys.exit(1)

    try:
        client = get_servicebus_client()
        with client:
            receiver = client.get_queue_receiver(
                queue_name=REQUEST_QUEUE,
                receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE
            )
            with receiver:
                logger.info(f"✅ Connected to queue: {REQUEST_QUEUE}")

                for message in receiver:
                    try:
                        logger.info("📨 Received message: " + str(message))

                        # Parse message
                        message_data = json.loads(str(message))

                        # Process the fatture import request
                        process_fatture_request(message_data)

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
        listen_for_fatture_requests()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
