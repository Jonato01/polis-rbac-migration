#!/usr/bin/env python3
"""
Container Backup Service - Fa il backup di una cartella SAL
Supporta sia import che cancellazione con prefissi diversi
Versione con Service Bus Queue Listener
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import time
import logging
import traceback
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

def backup_sal_folder(blob_service_client, storage_container, sal_name, backup_config, backup_type='import'):
    """
    Backup della cartella SAL
    backup_type: 'import' o 'delete'
    """
    backup_stats = {
        'root_files': 0,
        'root_bytes': 0,
        'doc_files': 0,
        'doc_bytes': 0,
        'backup_paths': []
    }
    
    try:
        container_client = blob_service_client.get_container_client(storage_container)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Backup dalla ROOT
        logger.info("="*60)
        logger.info(f"Backing up ROOT: {sal_name}/")
        logger.info(f"Backup type: {backup_type}")
        logger.info("="*60)
        
        source_prefix = f"{sal_name}/"
        # Usa prefisso diverso basato sul tipo di backup
        backup_prefix = f"{backup_config['container_path']}/{backup_type}_{sal_name}_{timestamp}/"
        
        blobs = container_client.list_blobs(name_starts_with=source_prefix)
        
        for blob in blobs:
            try:
                relative_path = blob.name[len(source_prefix):]
                backup_blob_name = f"{backup_prefix}{relative_path}"
                
                source_blob = container_client.get_blob_client(blob.name)
                backup_blob = container_client.get_blob_client(backup_blob_name)
                
                source_url = source_blob.url
                backup_blob.start_copy_from_url(source_url)
                
                backup_stats['root_files'] += 1
                backup_stats['root_bytes'] += blob.size or 0
                
                if backup_stats['root_files'] % 10 == 0:
                    logger.info(f"  Backed up {backup_stats['root_files']} files...")
                    
            except Exception as e:
                logger.error(f"Error backing up {blob.name}: {e}")
        
        if backup_stats['root_files'] > 0:
            backup_stats['backup_paths'].append(backup_prefix)
            logger.info(f"✅ Root backup: {backup_stats['root_files']} files ({backup_stats['root_bytes']:,} bytes)")
        
        # Backup da Documentazione generale SAL (solo per delete)
        if backup_type == 'delete':
            logger.info("="*60)
            logger.info(f"Backing up DOCUMENTAZIONE: {backup_config['doc_base_path']}/{sal_name}/")
            logger.info("="*60)
            
            doc_source = f"{backup_config['doc_base_path']}/{sal_name}/"
            doc_backup = f"{backup_config['docs_path']}/{backup_type}_{sal_name}_{timestamp}/"
            
            blobs = container_client.list_blobs(name_starts_with=doc_source)
            
            for blob in blobs:
                try:
                    relative_path = blob.name[len(doc_source):]
                    backup_blob_name = f"{doc_backup}{relative_path}"
                    
                    source_blob = container_client.get_blob_client(blob.name)
                    backup_blob = container_client.get_blob_client(backup_blob_name)
                    
                    source_url = source_blob.url
                    backup_blob.start_copy_from_url(source_url)
                    
                    backup_stats['doc_files'] += 1
                    backup_stats['doc_bytes'] += blob.size or 0
                    
                except Exception as e:
                    logger.error(f"Error backing up {blob.name}: {e}")
            
            if backup_stats['doc_files'] > 0:
                backup_stats['backup_paths'].append(doc_backup)
                logger.info(f"✅ Doc backup: {backup_stats['doc_files']} files ({backup_stats['doc_bytes']:,} bytes)")
        
        return True, backup_stats
        
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        return False, backup_stats

def send_completion_message(service_bus_conn, sal_name, success, stats, response_queue):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'container-backup',
        'sal': sal_name,
        'status': 'success' if success else 'failed',
        'stats': stats,
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

def process_backup_request(message_data):
    """Process a single backup request"""
    # Extract data from message
    sal_name = message_data.get('sal_storage_name') or message_data.get('sal')
    backup_type = message_data.get('backup_type', 'import')
    request_id = message_data.get('request_id', 'unknown')

    # Get environment variables
    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    STORAGE_CONN = os.getenv('AZURE_STORAGE_CONNECTION_STRING')

    # Storage Configuration
    STORAGE_CONTAINER = os.getenv('STORAGE_CONTAINER')
    
    # Backup Configuration
    BACKUP_CONTAINER_PATH = os.getenv('BACKUP_CONTAINER_PATH')
    BACKUP_DOCS_PATH = os.getenv('BACKUP_DOCS_PATH')
    DOC_BASE_PATH = os.getenv('DOC_BASE_PATH')
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')
    
    logger.info("="*60)
    logger.info("CONTAINER BACKUP SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL: {sal_name}")
    logger.info(f"[{request_id}] Backup Type: {backup_type}")
    logger.info(f"[{request_id}] Processing backup request...")

    # Validate required environment variables
    required_vars = {
        'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or SERVICE_BUS),
        'AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING': (os.getenv('AZURE_STORAGE_ACCOUNT_NAME') or STORAGE_CONN),
        'STORAGE_CONTAINER': STORAGE_CONTAINER,
        'BACKUP_CONTAINER_PATH': BACKUP_CONTAINER_PATH,
        'BACKUP_DOCS_PATH': BACKUP_DOCS_PATH,
        'DOC_BASE_PATH': DOC_BASE_PATH,
        'RESPONSE_QUEUE': RESPONSE_QUEUE
    }
    
    missing_vars = [var for var, value in required_vars.items() if value is None]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise Exception(f"Missing environment variables: {', '.join(missing_vars)}")

    if not sal_name:
        raise Exception("sal_name not provided in message")

    # Build backup configuration
    backup_config = {
        'container_path': BACKUP_CONTAINER_PATH,
        'docs_path': BACKUP_DOCS_PATH,
        'doc_base_path': DOC_BASE_PATH
    }
    
    try:
        # Connect to storage
        logger.info(f"[{request_id}] Connecting to Azure Storage...")
        blob_service_client = get_blob_service_client()
        
        # Execute backup
        logger.info(f"[{request_id}] Starting {backup_type} backup for SAL: {sal_name}")
        success, stats = backup_sal_folder(
            blob_service_client, STORAGE_CONTAINER, sal_name,
            backup_config, backup_type
        )
        
        if success:
            total_files = stats['root_files'] + stats['doc_files']
            total_bytes = stats['root_bytes'] + stats['doc_bytes']
            
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ CONTAINER BACKUP COMPLETED")
            logger.info(f"[{request_id}]    Type: {backup_type}")
            logger.info(f"[{request_id}]    Total files: {total_files}")
            logger.info(f"[{request_id}]    Total size: {total_bytes:,} bytes")
            logger.info(f"[{request_id}]    Backup locations:")
            for path in stats['backup_paths']:
                logger.info(f"[{request_id}]      - {path}")
            logger.info("="*60)
            
            # Send success message
            send_completion_message(SERVICE_BUS, sal_name, True, stats, RESPONSE_QUEUE)
        else:
            logger.error(f"[{request_id}] ❌ Container backup failed")
            send_completion_message(SERVICE_BUS, sal_name, False, stats, RESPONSE_QUEUE)
            raise Exception("Backup failed")

        # Wait before completing
        logger.info(f"[{request_id}] Waiting 5 seconds...")
        time.sleep(5)

        logger.info(f"[{request_id}] ✅ Request completed")

    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        send_completion_message(SERVICE_BUS, sal_name, False,
                              {'error': str(e), 'root_files': 0, 'root_bytes': 0,
                               'doc_files': 0, 'doc_bytes': 0, 'backup_paths': []},
                              RESPONSE_QUEUE)
        raise

def listen_for_backup_requests():
    """Listen for backup requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('CONTAINER_BACKUP_SERVICE_QUEUE', 'import-sal-container-backup')

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

                        # Process the backup request
                        process_backup_request(message_data)

                        # Auto-deleted in RECEIVE_AND_DELETE mode

                    except Exception as e:
                        logger.error(f"❌ Error processing message: {e}")
                        logger.error(f"Full traceback: {traceback.format_exc()}")
                        # Message already removed

    except Exception as e:
        logger.error(f"❌ Fatal error in queue listener: {e}")
        logger.error(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)

def main():
    """Main function"""
    try:
        listen_for_backup_requests()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()