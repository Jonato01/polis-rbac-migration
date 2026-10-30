#!/usr/bin/env python3
"""
IVA Indetraibile Import Service - Container Apps version
Ascolta coda Service Bus e processa richieste import IVA Indetraibile PDF con parallelizzazione
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
from iva_indetraibile_ocr_wrapper import run_import, get_db_config, get_api_config, test_connections

def send_completion_message(service_bus_conn, sal_name, success, details, queue_name):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'iva-indetraibile-import',
        'sal': sal_name,
        'status': 'success' if success else 'failed',
        'details': details,
        'timestamp': datetime.now().isoformat()
    }

    for attempt in range(5):
        try:
            logger.info(f"Sending completion message (attempt {attempt + 1}/5)...")

            client = get_servicebus_client()
            with client:
                sender = client.get_queue_sender(queue_name=queue_name)
                with sender:
                    message = ServiceBusMessage(json.dumps(message_data))
                    sender.send_messages(message)

            time.sleep(2)  # Wait to ensure sent
            logger.info("✅ Completion message sent!")
            return True
        except Exception as e:
            logger.error(f"Attempt {attempt + 1} failed: {e}")
            if attempt < 4:
                time.sleep(2 ** attempt)

    logger.error("❌ Failed to send completion message")
    return False

def process_import_request(message_data):
    """Process a single IVA Indetraibile import request"""
    # Extract data from message
    sal_storage_name = message_data.get('sal_storage_name')
    request_id = message_data.get('request_id', 'unknown')

    # Get environment variables
    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')
    MAX_WORKERS = int(os.getenv('MAX_WORKERS', '4'))  # Parallelize 4 PDFs at a time

    logger.info("="*60)
    logger.info("IVA INDETRAIBILE PDF IMPORT SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL Storage: {sal_storage_name}")
    logger.info(f"[{request_id}] Max Workers: {MAX_WORKERS}")

    # Validate required environment variables
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

        # Test connections first
        logger.info(f"[{request_id}] Testing connections...")
        connections_ok = test_connections()

        if not connections_ok:
            error_msg = "Failed to establish connections to database/API"
            logger.error(f"[{request_id}] {error_msg}")
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)
            return

        # Run the import with parallelization
        logger.info(f"[{request_id}] Starting IVA Indetraibile PDF import...")
        logger.info(f"[{request_id}] Processing with {MAX_WORKERS} parallel workers")

        # The run_import function will use the MAX_WORKERS env variable
        success = run_import(
            sal_folder=sal_storage_name,
            duplicate_handling="skip",
            limit=None,  # Process all files
            start_index=None,
            batch_size=None,
            max_workers=MAX_WORKERS
        )

        if success:
            status_message = f"IVA Indetraibile PDF import completed successfully with {MAX_WORKERS} parallel workers"
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ IVA INDETRAIBILE IMPORT COMPLETED")
            logger.info("="*60)

            # Send success message
            send_completion_message(SERVICE_BUS, sal_storage_name, True, status_message, RESPONSE_QUEUE)
        else:
            error_msg = "IVA Indetraibile PDF import failed - check logs for details"
            logger.error(f"[{request_id}] {error_msg}")

            # Send failure message
            send_completion_message(SERVICE_BUS, sal_storage_name, False, error_msg, RESPONSE_QUEUE)

        # Wait before completing
        logger.info(f"[{request_id}] Waiting 5 seconds...")
        time.sleep(5)

        logger.info(f"[{request_id}] ✅ Message completed")

    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        send_completion_message(SERVICE_BUS, sal_storage_name, False, str(e), RESPONSE_QUEUE)
        raise

def listen_for_import_requests():
    """Listen for import requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('REQUEST_QUEUE', 'import-sal-iva-indetraibile')

    logger.info("🚀 Starting IVA Indetraibile import queue processor...")
    logger.info(f"📡 Listening for messages on queue: {REQUEST_QUEUE}")
    logger.info(f"⚙️  Max parallel workers: {os.getenv('MAX_WORKERS', '4')}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing':
        logger.error("SERVICE_BUS_CONNECTION_STRING not configured")
        sys.exit(1)

    try:
        client = get_servicebus_client()
        with client:
            receiver = client.get_queue_receiver(queue_name=REQUEST_QUEUE, receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE)
            with receiver:
                logger.info(f"✅ Connected to queue: {REQUEST_QUEUE}")
                logger.info("⏳ Waiting for messages...")

                for message in receiver:
                    try:
                        logger.info("="*60)
                        logger.info("📨 NEW MESSAGE RECEIVED")
                        logger.info("="*60)
                        logger.info(f"Message: {str(message)}")

                        # Parse message
                        message_data = json.loads(str(message))

                        # Process the import request
                        process_import_request(message_data)

                        # Auto-deleted in RECEIVE_AND_DELETE mode
                        logger.info("✅ Message handled (auto-delete)")

                    except json.JSONDecodeError as e:
                        logger.error(f"❌ Invalid JSON in message: {e}")
                        logger.error(f"Message content: {str(message)}")
                        # Message already removed

                    except Exception as e:
                        logger.error(f"❌ Error processing message: {e}")
                        import traceback
                        logger.error(f"Full traceback: {traceback.format_exc()}")
                        # Message already removed

    except KeyboardInterrupt:
        logger.info("🛑 Shutting down gracefully...")
        sys.exit(0)

    except Exception as e:
        logger.error(f"❌ Fatal error in queue listener: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)

def main():
    """Entry point for the service"""
    logger.info("="*60)
    logger.info("IVA INDETRAIBILE IMPORT SERVICE - CONTAINER APPS")
    logger.info("="*60)

    # Start listening for messages
    listen_for_import_requests()

if __name__ == "__main__":
    main()
