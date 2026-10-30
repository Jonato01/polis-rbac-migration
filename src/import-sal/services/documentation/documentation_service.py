#!/usr/bin/env python3
"""
Documentation Service - Crea la struttura delle cartelle di documentazione
Ascolta coda Service Bus e processa richieste di creazione documentazione
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import time
import logging
from datetime import datetime
from azure.storage.blob import BlobServiceClient
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusReceiveMode

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

def create_folder_structure(blob_service_client, container_name, sal_name, base_path, subfolders, placeholder_file):
    """Create documentation folder structure in Azure Storage"""
    folders_created = 0
    
    try:
        # Crea le cartelle
        # Nota: In Azure Blob Storage, le cartelle sono virtuali - si creano caricando un file placeholder
        container_client = blob_service_client.get_container_client(container_name)
        
        # Crea la struttura base_path/SAL_NAME/sottocartella/
        for subfolder in subfolders:
            # Path completo: base_path/9_SAL_31_08_2025/Approfondimenti/placeholder_file
            blob_path = f"{base_path}/{sal_name}/{subfolder}/{placeholder_file}"
            
            logger.info(f"Creating folder: {base_path}/{sal_name}/{subfolder}/")
            
            blob_client = container_client.get_blob_client(blob_path)
            
            # Crea un file placeholder per creare la struttura delle cartelle
            blob_client.upload_blob(b"", overwrite=True)
            folders_created += 1
        
        logger.info(f"✅ Created {folders_created} folders")
        return True, folders_created
        
    except Exception as e:
        logger.error(f"Error creating folder structure: {e}")
        return False, folders_created

def send_completion_message(service_bus_conn, sal_name, success, details, response_queue):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'documentation-setup',
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
                sender = client.get_queue_sender(queue_name=response_queue)
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

def process_documentation_request(message_data):
    """Process a single documentation request"""
    # Extract data from message
    sal_name = message_data.get('sal_storage_name') or message_data.get('sal')
    request_id = message_data.get('request_id', 'unknown')

    # Get environment variables
    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    STORAGE_CONN = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
    
    # Documentation Configuration
    STORAGE_CONTAINER = os.getenv('STORAGE_CONTAINER')
    DOC_BASE_PATH = os.getenv('DOC_BASE_PATH')
    DOC_SUBFOLDERS = os.getenv('DOC_SUBFOLDERS')
    PLACEHOLDER_FILE = os.getenv('PLACEHOLDER_FILE', '.placeholder')

    # Service Bus Configuration
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')
    
    logger.info("="*60)
    logger.info("DOCUMENTATION SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL: {sal_name}")
    logger.info("="*60)
    
    # Validate required environment variables
    required_vars = {
        'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or SERVICE_BUS),
        'AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING': (os.getenv('AZURE_STORAGE_ACCOUNT_NAME') or STORAGE_CONN),
        'STORAGE_CONTAINER': STORAGE_CONTAINER,
        'DOC_BASE_PATH': DOC_BASE_PATH,
        'DOC_SUBFOLDERS': DOC_SUBFOLDERS,
        'RESPONSE_QUEUE': RESPONSE_QUEUE
    }
    
    missing_vars = [var for var, value in required_vars.items() if value is None]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise Exception(f"Missing environment variables: {', '.join(missing_vars)}")

    if not sal_name:
        raise Exception("sal_storage_name or sal not provided in message")

    # Parse subfolders from environment variable (comma-separated)
    subfolders = [folder.strip() for folder in DOC_SUBFOLDERS.split(',')]
    
    try:
        # Create folder structure
        logger.info(f"[{request_id}] Creating documentation folder structure...")
        logger.info(f"[{request_id}] Target path: {DOC_BASE_PATH}/{sal_name}/")

        blob_service_client = get_blob_service_client()
        success, folders_count = create_folder_structure(
            blob_service_client, STORAGE_CONTAINER, sal_name,
            DOC_BASE_PATH, subfolders, PLACEHOLDER_FILE
        )
        
        if success:
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ DOCUMENTATION STRUCTURE CREATED")
            logger.info(f"[{request_id}]    Folders created: {folders_count}")
            logger.info(f"[{request_id}]    Path: {DOC_BASE_PATH}/{sal_name}/")
            logger.info(f"[{request_id}]    Subfolders:")
            for subfolder in subfolders:
                logger.info(f"[{request_id}]      - {subfolder}")
            logger.info("="*60)
            
            # Send success message
            send_completion_message(
                SERVICE_BUS, sal_name, True,
                f"Created documentation structure: {folders_count} folders",
                RESPONSE_QUEUE
            )
        else:
            logger.error(f"[{request_id}] ❌ Failed to create documentation structure")
            send_completion_message(SERVICE_BUS, sal_name, False, "Failed to create folders", RESPONSE_QUEUE)
            raise Exception("Failed to create documentation structure")

        # Wait before completing
        logger.info(f"[{request_id}] Waiting 3 seconds...")
        time.sleep(3)

        logger.info(f"[{request_id}] ✅ Message completed")

    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        send_completion_message(SERVICE_BUS, sal_name, False, str(e), RESPONSE_QUEUE)
        raise

def listen_for_documentation_requests():
    """Listen for documentation requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('DOCUMENTATION_SERVICE_QUEUE', 'import-sal-documentation')

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

                        # Process the documentation request
                        process_documentation_request(message_data)

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
        listen_for_documentation_requests()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()