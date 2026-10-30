#!/usr/bin/env python3
"""
SFTP Manual Import - Command line version
Import SAL from SFTP to Azure Storage manually with multiple transfer methods
Usage: python sftp_import_manual.py <sal_storage_name> [--method METHOD]
Methods: http (default), azcopy, parallel
Example: python sftp_import_manual.py 6_SAL_31_08_2024 --method azcopy
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import logging
import paramiko
import tempfile
import subprocess
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient
from sftp_service import convert_storage_to_sftp_name

# Load environment variables from .env file
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

def copy_files_http(sftp, remote_path, blob_client, container_name, sal_name):
    """Copy files from SFTP to Azure Storage using HTTP (sequential)"""
    stats = {'files': 0, 'bytes': 0}
    temp_dir = tempfile.gettempdir()
    
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
                
                temp_file = os.path.join(temp_dir, item.filename.replace('/', '_'))
                sftp.get(remote_file, temp_file)
                
                full_blob = f"{sal_name}/{blob_path}"
                blob = blob_client.get_blob_client(container=container_name, blob=full_blob)
                
                with open(temp_file, 'rb') as data:
                    blob.upload_blob(data, overwrite=True)
                
                os.remove(temp_file)
                stats['files'] += 1
                stats['bytes'] += item.st_size
    
    copy_recursive(remote_path)
    return stats

def copy_files_parallel(sftp, remote_path, blob_client, container_name, sal_name, max_workers=5):
    """Copy files from SFTP to Azure Storage using parallel uploads"""
    stats = {'files': 0, 'bytes': 0}
    temp_dir = tempfile.gettempdir()
    files_to_copy = []
    
    def scan_recursive(sftp_path, blob_prefix=""):
        """Scan and collect all files to copy"""
        items = sftp.listdir_attr(sftp_path)
        for item in items:
            remote_file = f"{sftp_path}/{item.filename}"
            blob_path = f"{blob_prefix}{item.filename}" if blob_prefix else item.filename
            
            if item.st_mode & 0o040000:  # Directory
                logger.info(f"📁 Directory: {item.filename}")
                scan_recursive(remote_file, f"{blob_path}/")
            else:
                files_to_copy.append({
                    'remote_file': remote_file,
                    'blob_path': blob_path,
                    'filename': item.filename,
                    'size': item.st_size
                })
    
    def copy_single_file(file_info):
        """Copy a single file"""
        try:
            logger.info(f"📄 Copying: {file_info['filename']} ({file_info['size']:,} bytes)")
            
            temp_file = os.path.join(temp_dir, file_info['filename'].replace('/', '_'))
            sftp.get(file_info['remote_file'], temp_file)
            
            full_blob = f"{sal_name}/{file_info['blob_path']}"
            blob = blob_client.get_blob_client(container=container_name, blob=full_blob)
            
            with open(temp_file, 'rb') as data:
                blob.upload_blob(data, overwrite=True)
            
            os.remove(temp_file)
            return file_info['size']
        except Exception as e:
            logger.error(f"Error copying {file_info['filename']}: {e}")
            return 0
    
    # Scan all files
    logger.info("Scanning files...")
    scan_recursive(remote_path)
    logger.info(f"Found {len(files_to_copy)} files to copy")
    
    # Copy in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(copy_single_file, file_info) for file_info in files_to_copy]
        
        for future in as_completed(futures):
            size = future.result()
            if size > 0:
                stats['files'] += 1
                stats['bytes'] += size
    
    return stats

def copy_files_azcopy(sftp, remote_path, blob_client, container_name, sal_name, storage_conn):
    """Copy files from SFTP to Azure Storage using AzCopy"""
    stats = {'files': 0, 'bytes': 0}
    
    # Check if azcopy is available
    azcopy_path = shutil.which('azcopy')
    if not azcopy_path:
        logger.error("❌ AzCopy not found! Please install AzCopy first.")
        logger.info("Download from: https://aka.ms/downloadazcopy")
        sys.exit(1)
    
    logger.info(f"✅ Using AzCopy: {azcopy_path}")
    
    # Create temp directory for SFTP download
    with tempfile.TemporaryDirectory() as temp_dir:
        local_path = os.path.join(temp_dir, sal_name)
        os.makedirs(local_path, exist_ok=True)
        
        # Download all files from SFTP first
        logger.info(f"📥 Downloading from SFTP to {local_path}...")
        stats_download = download_recursive(sftp, remote_path, local_path)
        logger.info(f"✅ Downloaded {stats_download['files']} files ({stats_download['bytes']:,} bytes)")
        
        # Extract storage account details from connection string
        storage_account = None
        storage_key = None
        for part in storage_conn.split(';'):
            if part.startswith('AccountName='):
                storage_account = part.split('=', 1)[1]
            elif part.startswith('AccountKey='):
                storage_key = part.split('=', 1)[1]
        
        if not storage_account or not storage_key:
            logger.error("❌ Could not extract storage account details")
            sys.exit(1)
        
        # Build AzCopy command
        blob_url = f"https://{storage_account}.blob.core.windows.net/{container_name}/{sal_name}?{get_sas_token(storage_conn, container_name)}"
        
        cmd = [
            azcopy_path,
            'copy',
            local_path,
            blob_url,
            '--recursive=true',
            '--overwrite=true'
        ]
        
        logger.info(f"📤 Uploading to Azure Storage with AzCopy...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info("✅ AzCopy completed successfully")
            stats = stats_download
        else:
            logger.error(f"❌ AzCopy failed: {result.stderr}")
            sys.exit(1)
    
    return stats

def download_recursive(sftp, remote_path, local_path):
    """Download files recursively from SFTP"""
    stats = {'files': 0, 'bytes': 0}
    
    items = sftp.listdir_attr(remote_path)
    for item in items:
        remote_file = f"{remote_path}/{item.filename}"
        local_file = os.path.join(local_path, item.filename)
        
        if item.st_mode & 0o040000:  # Directory
            os.makedirs(local_file, exist_ok=True)
            sub_stats = download_recursive(sftp, remote_file, local_file)
            stats['files'] += sub_stats['files']
            stats['bytes'] += sub_stats['bytes']
        else:  # File
            logger.info(f"📄 Downloading: {item.filename} ({item.st_size:,} bytes)")
            sftp.get(remote_file, local_file)
            stats['files'] += 1
            stats['bytes'] += item.st_size
    
    return stats

def get_sas_token(storage_conn, container_name):
    """Generate SAS token for AzCopy (simplified - uses account key directly)"""
    # For simplicity, using account key auth with AzCopy
    # In production, generate proper SAS token
    from azure.storage.blob import generate_container_sas, ContainerSasPermissions
    from datetime import datetime, timedelta
    
    blob_client = get_blob_service_client()
    
    sas = generate_container_sas(
        account_name=blob_client.account_name,
        container_name=container_name,
        account_key=blob_client.credential.account_key,
        permission=ContainerSasPermissions(read=True, write=True, delete=True, list=True),
        expiry=datetime.utcnow() + timedelta(hours=1)
    )
    
    return sas

def manual_import(sal_storage_name, method='http'):
    """Import SAL from SFTP to Storage manually"""
    
    # Get environment variables
    STORAGE_CONN = os.getenv('AZURE_STORAGE_CONNECTION_STRING')
    SFTP_HOST = os.getenv('SFTP_HOST')
    SFTP_PORT = int(os.getenv('SFTP_PORT', '22'))
    SFTP_USER = os.getenv('SFTP_USER')
    SFTP_PASSWORD = os.getenv('SFTP_PASSWORD')
    SFTP_BASE_PATH = os.getenv('SFTP_BASE_PATH', '/upload')
    STORAGE_CONTAINER = os.getenv('STORAGE_CONTAINER', 'polis')
    
    logger.info("="*60)
    logger.info("SFTP MANUAL IMPORT")
    logger.info("="*60)
    logger.info(f"Transfer Method: {method.upper()}")
    logger.info(f"SAL Storage: {sal_storage_name}")
    
    # Validate required environment variables
    required_vars = {
        'AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING': (os.getenv('AZURE_STORAGE_ACCOUNT_NAME') or STORAGE_CONN),
        'SFTP_HOST': SFTP_HOST,
        'SFTP_USER': SFTP_USER,
        'SFTP_PASSWORD': SFTP_PASSWORD
    }
    
    missing_vars = [var for var, value in required_vars.items() if not value]
    if missing_vars:
        logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
        sys.exit(1)
    
    # Convert SAL name
    sal_sftp_name = convert_storage_to_sftp_name(sal_storage_name)
    logger.info(f"SAL SFTP: {sal_sftp_name}")
    
    try:
        # Connect to SFTP
        logger.info(f"Connecting to SFTP {SFTP_HOST}:{SFTP_PORT}...")
        transport = paramiko.Transport((SFTP_HOST, SFTP_PORT))
        transport.connect(username=SFTP_USER, password=SFTP_PASSWORD)
        sftp = paramiko.SFTPClient.from_transport(transport)
        
        # Check if SAL exists
        sal_path = f"{SFTP_BASE_PATH}/{sal_sftp_name}"
        logger.info(f"Checking: {sal_path}")
        
        try:
            items = sftp.listdir(sal_path)
            logger.info(f"✅ Found {len(items)} items")
        except FileNotFoundError:
            logger.error(f"❌ SAL not found at {sal_path}")
            sys.exit(1)
        
        # Copy files using selected method
        logger.info(f"Starting copy to Azure Storage...")
        blob_client = get_blob_service_client()
        
        if method == 'http':
            stats = copy_files_http(sftp, sal_path, blob_client, STORAGE_CONTAINER, sal_storage_name)
        elif method == 'parallel':
            stats = copy_files_parallel(sftp, sal_path, blob_client, STORAGE_CONTAINER, sal_storage_name)
        elif method == 'azcopy':
            stats = copy_files_azcopy(sftp, sal_path, blob_client, STORAGE_CONTAINER, sal_storage_name, STORAGE_CONN)
        else:
            logger.error(f"❌ Unknown method: {method}")
            sys.exit(1)
        
        sftp.close()
        transport.close()
        
        # Success
        logger.info("="*60)
        logger.info(f"✅ COPY COMPLETED")
        logger.info(f"   Method: {method.upper()}")
        logger.info(f"   Files: {stats['files']}")
        logger.info(f"   Size: {stats['bytes']:,} bytes ({stats['bytes']/1024/1024:.2f} MB)")
        logger.info("="*60)
        
    except Exception as e:
        logger.error(f"❌ ERROR: {e}")
        import traceback
        logger.error(f"Full traceback: {traceback.format_exc()}")
        sys.exit(1)

def main():
    """Main function"""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Import SAL from SFTP to Azure Storage',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Transfer Methods:
  http      - Sequential HTTP uploads (default, simple, no dependencies)
  parallel  - Parallel HTTP uploads (faster, 5 concurrent threads)
  azcopy    - AzCopy CLI (fastest, requires azcopy installed)

Examples:
  python sftp_import_manual.py 6_SAL_31_08_2024
  python sftp_import_manual.py 6_SAL_31_08_2024 --method parallel
  python sftp_import_manual.py 6_SAL_31_08_2024 --method azcopy
        """
    )
    
    parser.add_argument('sal_storage_name', help='SAL name in storage format (e.g., 6_SAL_31_08_2024)')
    parser.add_argument('--method', choices=['http', 'parallel', 'azcopy'], default='http',
                       help='Transfer method (default: http)')
    
    args = parser.parse_args()
    manual_import(args.sal_storage_name, args.method)

if __name__ == "__main__":
    main()