#!/usr/bin/env python3
"""
SFTP Bulk Sync Service - Container Apps version
Sincronizza TUTTI i SAL da SFTP ad Azure Storage all'avvio del container
Non ascolta code, ma processa sequenzialmente tutti i SAL disponibili
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import time
import logging
import paramiko
import re
import tempfile
from azure.storage.blob import BlobServiceClient

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

def load_env_file(env_file='.colenv'):
    """Load environment variables from .colenv file"""
    if os.path.exists(env_file):
        logger.info(f"Loading environment variables from {env_file}...")
        try:
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    # Skip comments and empty lines
                    if line and not line.startswith('#'):
                        if '=' in line:
                            key, value = line.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            # Remove quotes if present
                            if value.startswith('"') and value.endswith('"'):
                                value = value[1:-1]
                            elif value.startswith("'") and value.endswith("'"):
                                value = value[1:-1]
                            os.environ[key] = value
                            # Log without sensitive data
                            if key.upper() in ['SFTP_PASSWORD', 'AZURE_STORAGE_CONNECTION_STRING']:
                                logger.info(f"  ✓ {key}: ***")
                            else:
                                logger.info(f"  ✓ {key}: {value}")
            logger.info("✅ Environment variables loaded from .colenv")
        except Exception as e:
            logger.error(f"❌ Error loading {env_file}: {e}")
            raise
    else:
        logger.warning(f"⚠️  {env_file} file not found, using system environment variables")

def convert_sftp_to_storage_name(sftp_name):
    """Convert SFTP name to storage name format
    Examples:
    - "1° SAL al 31 Agosto 2024" -> "1_SAL_31_08_2024"
    - "1° SAL al 31Dic22" -> "1_SAL_31_12_2022"
    - "2° SAL al 30Apr23" -> "2_SAL_30_04_2023"
    """
    try:
        # Pattern 1: "numero° SAL al giornoMeseAnno" (es. "1° SAL al 31Dic22")
        match = re.match(r'^(\d+)°\s+SAL\s+al\s+(\d{1,2})([A-Za-z]+)(\d{2,4})$', sftp_name)
        if match:
            numero, giorno, nome_mese, anno = match.groups()
        else:
            # Pattern 2: "numero° SAL al giorno mese anno" (es. "1° SAL al 31 Agosto 2024")
            match = re.match(r'^(\d+)°\s+SAL\s+al\s+(\d{1,2})\s+(\w+)\s+(\d{4})$', sftp_name)
            if not match:
                return sftp_name
            numero, giorno, nome_mese, anno = match.groups()

        # Dizionario mesi completi e abbreviati
        mesi = {
            # Mesi completi
            'Gennaio': '01', 'Febbraio': '02', 'Marzo': '03', 'Aprile': '04',
            'Maggio': '05', 'Giugno': '06', 'Luglio': '07', 'Agosto': '08',
            'Settembre': '09', 'Ottobre': '10', 'Novembre': '11', 'Dicembre': '12',
            # Abbreviazioni italiane
            'Gen': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04',
            'Mag': '05', 'Giu': '06', 'Lug': '07', 'Ago': '08',
            'Set': '09', 'Ott': '10', 'Nov': '11', 'Dic': '12',
            # Varianti maiuscole/minuscole
            'gen': '01', 'feb': '02', 'mar': '03', 'apr': '04',
            'mag': '05', 'giu': '06', 'lug': '07', 'ago': '08',
            'set': '09', 'ott': '10', 'nov': '11', 'dic': '12'
        }

        mese_num = mesi.get(nome_mese, '01')
        giorno_pad = str(giorno).zfill(2)

        # Converti anno a 2 cifre in 4 cifre (assumendo 20xx)
        if len(anno) == 2:
            anno = f"20{anno}"

        return f"{numero}_SAL_{giorno_pad}_{mese_num}_{anno}"
    except Exception as e:
        logger.warning(f"⚠️  Could not convert '{sftp_name}': {e}")
        return sftp_name

def copy_files(sftp, remote_path, blob_client, container_name, sal_name, sal_counter):
    """Copy files from SFTP to Azure Storage"""
    stats = {'files': 0, 'bytes': 0, 'errors': 0}

    def copy_recursive(sftp_path, blob_prefix=""):
        items = sftp.listdir_attr(sftp_path)
        for item in items:
            remote_file = f"{sftp_path}/{item.filename}"
            blob_path = f"{blob_prefix}{item.filename}" if blob_prefix else item.filename

            if item.st_mode & 0o040000:  # Directory
                logger.info(f"   [SAL {sal_counter}] 📁 Directory: {item.filename}")
                copy_recursive(remote_file, f"{blob_path}/")
            else:  # File
                logger.info(f"   [SAL {sal_counter}] 📄 Copying: {item.filename} ({item.st_size:,} bytes)")

                try:
                    # Download to temp
                    temp_file = os.path.join(tempfile.gettempdir(), item.filename.replace('/', '_'))
                    sftp.get(remote_file, temp_file)

                    # Upload to blob
                    full_blob = f"{sal_name}/{blob_path}"
                    blob = blob_client.get_blob_client(container=container_name, blob=full_blob)

                    with open(temp_file, 'rb') as data:
                        blob.upload_blob(data, overwrite=True)

                    os.remove(temp_file)
                    stats['files'] += 1
                    stats['bytes'] += item.st_size
                except Exception as e:
                    logger.error(f"   [SAL {sal_counter}] ❌ Error copying {item.filename}: {e}")
                    stats['errors'] += 1

    copy_recursive(remote_path)
    return stats

def get_all_sals(sftp, base_path):
    """Get all SAL directories from SFTP"""
    sals = []
    try:
        items = sftp.listdir_attr(base_path)
        for item in items:
            if item.st_mode & 0o040000:  # Is directory
                sals.append({
                    'sftp_name': item.filename,
                    'path': f"{base_path}/{item.filename}",
                    'size': item.st_size
                })
    except Exception as e:
        logger.error(f"❌ Error listing SFTP directory {base_path}: {e}")

    return sorted(sals, key=lambda x: x['sftp_name'])

def process_all_sals():
    """Process all SALs from SFTP"""

    # Load environment variables from .colenv
    load_env_file('.colenv')

    # Get environment variables
    SFTP_HOST = os.getenv('SFTP_HOST')
    SFTP_PORT = int(os.getenv('SFTP_PORT', '22'))
    SFTP_USER = os.getenv('SFTP_USER')
    SFTP_PASSWORD = os.getenv('SFTP_PASSWORD')
    SFTP_BASE_PATH = os.getenv('SFTP_BASE_PATH', '/upload')

    STORAGE_CONN = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
    STORAGE_CONTAINER = os.getenv('STORAGE_CONTAINER', 'polis')

    # Validate required environment variables
    required_vars = {
        'SFTP_HOST': SFTP_HOST,
        'SFTP_USER': SFTP_USER,
        'SFTP_PASSWORD': SFTP_PASSWORD,
        'AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING': (os.getenv('AZURE_STORAGE_ACCOUNT_NAME') or STORAGE_CONN)
    }

    missing_vars = [var for var, value in required_vars.items() if not value]
    if missing_vars:
        logger.error(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
        sys.exit(1)

    logger.info("="*70)
    logger.info("SFTP BULK SYNC SERVICE - Sincronizzazione completa")
    logger.info("="*70)
    logger.info(f"SFTP Host: {SFTP_HOST}:{SFTP_PORT}")
    logger.info(f"Base Path: {SFTP_BASE_PATH}")
    logger.info(f"Storage Container: {STORAGE_CONTAINER}")
    logger.info("="*70)

    total_stats = {'sals': 0, 'files': 0, 'bytes': 0, 'errors': 0, 'failed_sals': []}

    try:
        # Connect to SFTP
        logger.info(f"🔗 Connecting to SFTP {SFTP_HOST}:{SFTP_PORT}...")
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)
        logger.info("✅ Connected to SFTP")

        # Get all SALs
        logger.info(f"📋 Scanning for SALs in {SFTP_BASE_PATH}...")
        sals = get_all_sals(sftp, SFTP_BASE_PATH)
        logger.info(f"✅ Found {len(sals)} SALs to process")

        if not sals:
            logger.warning("⚠️  No SALs found on SFTP server")
            sftp.close()
            transport.close()
            return

        # Process each SAL
        blob_client = get_blob_service_client()
        sal_counter = 0

        for sal in sals:
            sal_counter += 1
            sal_sftp_name = sal['sftp_name']
            sal_path = sal['path']

            # Convert SFTP name to storage name
            sal_storage_name = convert_sftp_to_storage_name(sal_sftp_name)

            logger.info("="*70)
            logger.info(f"[{sal_counter}/{len(sals)}] Processing: {sal_sftp_name}")
            logger.info(f"                 Storage: {sal_storage_name}")
            logger.info(f"                 Path: {sal_path}")
            logger.info("="*70)

            try:
                # Copy files
                logger.info(f"[SAL {sal_counter}] 📂 Starting copy to Azure Storage...")
                stats = copy_files(sftp, sal_path, blob_client, STORAGE_CONTAINER, sal_storage_name, sal_counter)

                # Log results
                logger.info(f"[SAL {sal_counter}] ✅ COMPLETED")
                logger.info(f"[SAL {sal_counter}]    Files: {stats['files']}")
                logger.info(f"[SAL {sal_counter}]    Size: {stats['bytes']:,} bytes ({stats['bytes']/1024/1024:.2f} MB)")
                if stats['errors'] > 0:
                    logger.warning(f"[SAL {sal_counter}]    Errors: {stats['errors']}")

                total_stats['sals'] += 1
                total_stats['files'] += stats['files']
                total_stats['bytes'] += stats['bytes']
                total_stats['errors'] += stats['errors']

            except Exception as e:
                logger.error(f"[SAL {sal_counter}] ❌ ERROR: {e}")
                total_stats['failed_sals'].append({
                    'sftp_name': sal_sftp_name,
                    'storage_name': sal_storage_name,
                    'error': str(e)
                })
                import traceback
                logger.error(f"Full traceback: {traceback.format_exc()}")
                continue

            # Wait between SALs to avoid overwhelming the server
            if sal_counter < len(sals):
                logger.info(f"⏳ Waiting 10 seconds before next SAL...")
                time.sleep(10)

        sftp.close()
        transport.close()

        # Final summary
        logger.info("="*70)
        logger.info("🎉 BULK SYNC COMPLETED")
        logger.info("="*70)
        logger.info(f"✅ SALs processed: {total_stats['sals']}/{len(sals)}")
        logger.info(f"✅ Total files: {total_stats['files']}")
        logger.info(f"✅ Total size: {total_stats['bytes']:,} bytes ({total_stats['bytes']/1024/1024:.2f} MB)")
        if total_stats['errors'] > 0:
            logger.warning(f"⚠️  Total errors: {total_stats['errors']}")

        if total_stats['failed_sals']:
            logger.error("❌ Failed SALs:")
            for failed in total_stats['failed_sals']:
                logger.error(f"   - {failed['sftp_name']} ({failed['storage_name']}): {failed['error']}")

        logger.info("="*70)

        # Keep container running
        logger.info("🟢 Service completed. Container will remain active.")

    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)

def main():
    """Main function"""
    try:
        process_all_sals()
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()

