#!/usr/bin/env python3
"""
SFTP Service - Container Apps version
Ascolta coda Service Bus e processa richieste SFTP
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import time
import logging
import paramiko
import re
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

def convert_storage_to_sftp_name(storage_name):
    """Convert storage name to SFTP name.
    Accetta formati tipo: "7_SAL_31_12_2024" e genera il nome convenzionale.
    Preferisce il formato abbreviato senza spazi tra giorno e mese (es. "31Dic24").
    """
    try:
        match = re.match(r'^(\d+)_SAL_(\d{2})_(\d{2})_(\d{4})$', storage_name)
        if not match:
            return storage_name
        numero, giorno, mese, anno = match.groups()

        mesi_full = {
            '01': 'Gennaio', '02': 'Febbraio', '03': 'Marzo', '04': 'Aprile',
            '05': 'Maggio', '06': 'Giugno', '07': 'Luglio', '08': 'Agosto',
            '09': 'Settembre', '10': 'Ottobre', '11': 'Novembre', '12': 'Dicembre'
        }
        mesi_abbr = {
            '01': 'Gen', '02': 'Feb', '03': 'Mar', '04': 'Apr',
            '05': 'Mag', '06': 'Giu', '07': 'Lug', '08': 'Ago',
            '09': 'Set', '10': 'Ott', '11': 'Nov', '12': 'Dic'
        }

        # Usa abbreviazioni e anno a 2 cifre, senza spazio tra giorno e mese.
        anno_2 = anno[-2:]
        return f"{numero}° SAL al {int(giorno)}{mesi_abbr.get(mese, mese)}{anno_2}"
    except Exception:
        return storage_name

def copy_files(sftp, remote_path, blob_client, container_name, sal_name):
    """Copy files from SFTP to Azure Storage"""
    stats = {'files': 0, 'bytes': 0}
    
    def copy_recursive(sftp_path, blob_prefix=""):
        items = sftp.listdir_attr(sftp_path)
        for item in items:
            remote_file = f"{sftp_path}/{item.filename}"
            blob_path = f"{blob_prefix}{item.filename}" if blob_prefix else item.filename
            
            if item.st_mode & 0o040000:  # Directory
                logger.info(f"📁 Directory: {item.filename}")
                copy_recursive(remote_file, f"{blob_path}/")
            else:  # File
                logger.info(f"📄 Copying: {item.filename} ({item.st_size:,} bytes)")
                
                # Download to temp
                temp_file = f"/tmp/{item.filename.replace('/', '_')}"
                sftp.get(remote_file, temp_file)
                
                # Upload to blob
                full_blob = f"{sal_name}/{blob_path}"
                blob = blob_client.get_blob_client(container=container_name, blob=full_blob)
                
                with open(temp_file, 'rb') as data:
                    blob.upload_blob(data, overwrite=True)
                
                os.remove(temp_file)
                stats['files'] += 1
                stats['bytes'] += item.st_size
    
    copy_recursive(remote_path)
    return stats

def send_completion_message(service_bus_conn, sal_name, success, details, queue_name):
    """Send message to Service Bus with retries"""
    message_data = {
        'task': 'sftp-verify-copy',
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

def _generate_sal_name_variants(storage_name):
    """Genera varianti plausibili del nome SAL per compatibilità con diverse convenzioni SFTP."""
    variants = []
    # Base parse
    m = re.match(r'^(\d+)_SAL_(\d{2})_(\d{2})_(\d{4})$', storage_name or '')
    if not m:
        # Se non corrisponde allo schema, usa come unica variante il nome originale
        return [storage_name]
    numero, giorno, mese, anno = m.groups()
    giorno_int = int(giorno)
    anno2 = anno[-2:]

    mesi_full = {
        '01': 'Gennaio', '02': 'Febbraio', '03': 'Marzo', '04': 'Aprile',
        '05': 'Maggio', '06': 'Giugno', '07': 'Luglio', '08': 'Agosto',
        '09': 'Settembre', '10': 'Ottobre', '11': 'Novembre', '12': 'Dicembre'
    }
    mesi_abbr = {
        '01': 'Gen', '02': 'Feb', '03': 'Mar', '04': 'Apr',
        '05': 'Mag', '06': 'Giu', '07': 'Lug', '08': 'Ago',
        '09': 'Set', '10': 'Ott', '11': 'Nov', '12': 'Dic'
    }

    # Formati comuni:
    # 1) Abbreviato senza spazio: "7° SAL al 31Dic24"
    variants.append(f"{numero}° SAL al {giorno_int}{mesi_abbr[mese]}{anno2}")
    # 2) Abbreviato con spazio: "7° SAL al 31 Dic 24"
    variants.append(f"{numero}° SAL al {giorno_int} {mesi_abbr[mese]} {anno2}")
    # 3) Completo con mese intero e anno 4 cifre: "7° SAL al 31 Dicembre 2024"
    variants.append(f"{numero}° SAL al {giorno_int} {mesi_full[mese]} {anno}")
    # 4) Completo senza spazio tra giorno e mese: "7° SAL al 31Dicembre2024"
    variants.append(f"{numero}° SAL al {giorno_int}{mesi_full[mese]}{anno}")

    # Ritorna uniche mantenendo ordine
    seen = set()
    result = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            result.append(v)
    return result

def process_sftp_request(message_data):
    """Process a single SFTP request"""
    # Extract data from message
    sal_storage_name = message_data.get('sal_storage_name')
    sal_sftp_name = message_data.get('sal_sftp_name', '')
    request_id = message_data.get('request_id', 'unknown')
    
    # Get environment variables
    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    STORAGE_CONN = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
    
    # SFTP Configuration
    SFTP_HOST = os.getenv('SFTP_HOST')
    SFTP_PORT = int(os.getenv('SFTP_PORT')) if os.getenv('SFTP_PORT') else None
    SFTP_USER = os.getenv('SFTP_USER')
    SFTP_PASSWORD = os.getenv('SFTP_PASSWORD')
    SFTP_BASE_PATH = os.getenv('SFTP_BASE_PATH')
    
    # Storage Configuration
    STORAGE_CONTAINER = os.getenv('STORAGE_CONTAINER')
    
    # Service Bus Configuration
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE')
    
    logger.info("="*60)
    logger.info("SFTP SERVICE - SIMPLE VERSION")
    logger.info("="*60)
    logger.info(f"[{request_id}] SAL Storage: {sal_storage_name}")
    logger.info(f"[{request_id}] SAL SFTP: {sal_sftp_name}")
    logger.info(f"[{request_id}] Processing SAL: {sal_storage_name}")
    
    # Validate required environment variables
    required_vars = {
        'SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING': (os.getenv('SERVICE_BUS_NAMESPACE') or SERVICE_BUS),
        'AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING': (os.getenv('AZURE_STORAGE_ACCOUNT_NAME') or STORAGE_CONN),
        'SFTP_HOST': SFTP_HOST,
        'SFTP_PORT': SFTP_PORT,
        'SFTP_USER': SFTP_USER,
        'SFTP_PASSWORD': SFTP_PASSWORD,
        'SFTP_BASE_PATH': SFTP_BASE_PATH,
        'STORAGE_CONTAINER': STORAGE_CONTAINER,
        'RESPONSE_QUEUE': RESPONSE_QUEUE
    }
    
    missing_vars = [var for var, value in required_vars.items() if value is None]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        raise Exception(f"Missing environment variables: {', '.join(missing_vars)}")
    
    if not sal_storage_name:
        raise Exception("sal_storage_name not provided in message")
    
    # Convert SAL name if needed
    if not sal_sftp_name:
        # Preferiamo il nome abbreviato senza spazio
        sal_sftp_name = convert_storage_to_sftp_name(sal_storage_name)
    logger.info(f"[{request_id}] SAL SFTP: {sal_sftp_name}")
    
    try:
        # Connect to SFTP
        logger.info(f"[{request_id}] Connecting to SFTP {SFTP_HOST}:{SFTP_PORT}...")
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)
        
        # Check if SAL exists - prova varianti
        base_variants = [sal_sftp_name] if sal_sftp_name else []
        gen_variants = _generate_sal_name_variants(sal_storage_name)
        candidates = base_variants + gen_variants

        found_path = None
        found_name = None
        for candidate in candidates:
            sal_path = f"{SFTP_BASE_PATH}/{candidate}"
            logger.info(f"[{request_id}] Checking: {sal_path}")
            try:
                items = sftp.listdir(sal_path)
                logger.info(f"[{request_id}] ✅ Found {len(items)} items in '{candidate}'")
                found_path = sal_path
                found_name = candidate
                break
            except FileNotFoundError:
                logger.info(f"[{request_id}] ❌ Not found: {sal_path}")
            except Exception as e:
                logger.error(f"[{request_id}] Error probing path {sal_path}: {e}")

        if not found_path:
            logger.error(f"[{request_id}] ❌ SAL not found in any candidate path")
            send_completion_message(SERVICE_BUS, sal_storage_name, False,
                                  f"SAL not found. Tried: {', '.join(candidates)}", RESPONSE_QUEUE)
            sftp.close()
            transport.close()
            return
        
        # Copy files
        logger.info(f"[{request_id}] Starting copy to Azure Storage from '{found_name}'...")
        blob_client = get_blob_service_client()
        stats = copy_files(sftp, found_path, blob_client, STORAGE_CONTAINER, sal_storage_name)

        sftp.close()
        transport.close()
        
        # Success
        logger.info("="*60)
        logger.info(f"[{request_id}] ✅ COPY COMPLETED")
        logger.info(f"[{request_id}]    Files: {stats['files']}")
        logger.info(f"[{request_id}]    Size: {stats['bytes']:,} bytes")
        logger.info("="*60)
        
        # Send success message
        send_completion_message(SERVICE_BUS, sal_storage_name, True, 
                              f"Copied {stats['files']} files ({stats['bytes']} bytes)",
                              RESPONSE_QUEUE)
        
        # Wait before completing
        logger.info(f"[{request_id}] Waiting 5 seconds...")
        time.sleep(5)
        
        logger.info(f"[{request_id}] ✅ Message completed")
        
    except Exception as e:
        logger.error(f"[{request_id}] ERROR: {e}")
        send_completion_message(SERVICE_BUS, sal_storage_name, False, str(e), RESPONSE_QUEUE)
        raise

def listen_for_sftp_requests():
    """Listen for SFTP requests on the queue"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('REQUEST_QUEUE', 'import-sal-sftp')
    
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
                        
                        # Process the SFTP request
                        process_sftp_request(message_data)
                        
                        # Message is auto-deleted in RECEIVE_AND_DELETE mode
                        # ...no manual completion required...

                    except Exception as e:
                        logger.error(f"❌ Error processing message: {e}")
                        import traceback
                        logger.error(f"Full traceback: {traceback.format_exc()}")
                        # In RECEIVE_AND_DELETE mode, message has already been removed

    except Exception as e:
        logger.error(f"❌ Fatal error in queue listener: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)

def main():
    """Main function"""
    try:
        listen_for_sftp_requests()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()