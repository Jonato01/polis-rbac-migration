#!/usr/bin/env python3
"""
Storage Cleanup Service - Cancella una cartella SAL dallo storage
Ascolta coda Service Bus e processa richieste di cleanup
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

def delete_sal_folder(blob_service_client, container_name, sal_name, root_path, doc_base_path):
    """Cancella la cartella SAL dallo storage"""
    delete_stats = {
        'root_deleted': 0,
        'doc_deleted': 0,
        'total_bytes': 0
    }
    
    try:
        container_client = blob_service_client.get_container_client(container_name)
        
        # Cancella dalla ROOT
        logger.info(f"Deleting from ROOT: {root_path}/{sal_name}/")
        root_prefix = f"{root_path}/{sal_name}/" if root_path else f"{sal_name}/"
        blobs = container_client.list_blobs(name_starts_with=root_prefix)
        
        for blob in blobs:
            try:
                blob_client = container_client.get_blob_client(blob.name)
                blob_properties = blob_client.get_blob_properties()
                
                blob_client.delete_blob()
                delete_stats['root_deleted'] += 1
                delete_stats['total_bytes'] += blob_properties.size
                
                if delete_stats['root_deleted'] % 10 == 0:
                    logger.info(f"  Deleted {delete_stats['root_deleted']} files...")
                    
            except Exception as e:
                logger.error(f"Error deleting {blob.name}: {e}")
        
        logger.info(f"✅ Deleted {delete_stats['root_deleted']} files from root")
        
        # Cancella da Documentazione generale SAL
        logger.info(f"Deleting from DOCUMENTAZIONE: {doc_base_path}/{sal_name}/")
        doc_prefix = f"{doc_base_path}/{sal_name}/"
        blobs = container_client.list_blobs(name_starts_with=doc_prefix)
        
        for blob in blobs:
            try:
                blob_client = container_client.get_blob_client(blob.name)
                blob_properties = blob_client.get_blob_properties()
                
                blob_client.delete_blob()
                delete_stats['doc_deleted'] += 1
                delete_stats['total_bytes'] += blob_properties.size
                
            except Exception as e:
                logger.error(f"Error deleting {blob.name}: {e}")
        
        logger.info(f"✅ Deleted {delete_stats['doc_deleted']} files from documentation")
        
        return True, delete_stats
        
    except Exception as e:
        logger.error(f"Delete operation failed: {e}")
        return False, delete_stats

def send_completion_message(service_bus_conn, sal_name, success, stats, response_queue):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'storage-cleanup',
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

def process_cleanup_request(message_data):
    """Process a single cleanup request"""
    # Extract data from message
    sal_name = message_data.get('sal_storage_name') or message_data.get('sal')
    cleanup_reason = message_data.get('cleanup_reason', 'Unknown')
    request_id = message_data.get('request_id', 'unknown')

    # Get environment variables
    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    STORAGE_CONN = os.getenv('AZURE_STORAGE_CONNECTION_STRING')

    # Storage Configuration
    STORAGE_CONTAINER = os.getenv('STORAGE_CONTAINER')
    ROOT_PATH = os.getenv('ROOT_PATH', '')  # Can be empty for root level
    DOC_BASE_PATH = os.getenv('DOC_BASE_PATH')

    # Service Bus Configuration
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')
    
    logger.info("="*60)
    logger.info("STORAGE CLEANUP SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL: {sal_name}")
    logger.info(f"[{request_id}] Reason: {cleanup_reason}")
    logger.info("="*60)
    
    # Validate required environment variables
    required_vars = {
        'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or SERVICE_BUS),
        'AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING': (os.getenv('AZURE_STORAGE_ACCOUNT_NAME') or STORAGE_CONN),
        'STORAGE_CONTAINER': STORAGE_CONTAINER,
        'DOC_BASE_PATH': DOC_BASE_PATH,
        'RESPONSE_QUEUE': RESPONSE_QUEUE
    }
    
    missing_vars = [var for var, value in required_vars.items() if value is None]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise Exception(f"Missing environment variables: {', '.join(missing_vars)}")

    if not sal_name:
        raise Exception("sal_storage_name or sal not provided in message")

    try:
        # Connetti allo storage
        blob_service_client = get_blob_service_client()
        
        # Cancella i file
        logger.info(f"[{request_id}] Starting deletion for SAL: {sal_name}")
        success, stats = delete_sal_folder(
            blob_service_client, STORAGE_CONTAINER, sal_name,
            ROOT_PATH, DOC_BASE_PATH
        )
        
        if success:
            total_deleted = stats['root_deleted'] + stats['doc_deleted']
            
            logger.info("="*60)
            logger.info(f"[{request_id}] ✅ STORAGE CLEANUP COMPLETED")
            logger.info(f"[{request_id}]    Total files deleted: {total_deleted}")
            logger.info(f"[{request_id}]    Total size deleted: {stats['total_bytes']:,} bytes")
            logger.info(f"[{request_id}]    Reason: {cleanup_reason}")
            logger.info("="*60)
            
            # Send success message
            send_completion_message(SERVICE_BUS, sal_name, True, stats, RESPONSE_QUEUE)
        else:
            logger.error(f"[{request_id}] ❌ Storage cleanup failed")
            send_completion_message(SERVICE_BUS, sal_name, False, stats, RESPONSE_QUEUE)
            raise Exception("Storage cleanup failed")

        # Wait before completing
        logger.info(f"[{request_id}] Waiting 3 seconds...")
        time.sleep(3)

        logger.info(f"[{request_id}] ✅ Message completed")

    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        send_completion_message(SERVICE_BUS, sal_name, False,
                              {'error': str(e), 'root_deleted': 0, 'doc_deleted': 0, 'total_bytes': 0},
                              RESPONSE_QUEUE)
        raise

def listen_for_cleanup_requests():
    """Listen for cleanup requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('STORAGE_CLEANUP_SERVICE_QUEUE', 'import-sal-storage-cleanup')

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

                        # Process the cleanup request
                        process_cleanup_request(message_data)

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
        listen_for_cleanup_requests()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()