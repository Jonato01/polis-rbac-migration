#!/usr/bin/env python3
"""
Enhanced wrapper for existing iva_indetraibile_ocr.py with environment-based configuration
Adattato per Azure Container Apps
"""

import argparse
import sys
import os
import concurrent.futures
from datetime import datetime
import traceback
import time

# Path setup per import moduli
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from ocr_common import fetch_mysql_query, get_pdf_paths_from_sal, process_single_pdf, execute_batch_queries
from dotenv import load_dotenv

load_dotenv()

def robust_log(message, force_flush=True):
    """Log message with timestamp and force flush"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    print(log_msg)
    if force_flush:
        sys.stdout.flush()
        sys.stderr.flush()


def safe_import():
    """Import iva_indetraibile_ocr module safely with logging"""
    try:
        robust_log("Attempting to import iva_indetraibile_ocr module...")
        import iva_indetraibile_ocr
        robust_log("iva_indetraibile_ocr module imported successfully")
        return iva_indetraibile_ocr
    except Exception as e:
        robust_log(f"FATAL: Failed to import iva_indetraibile_ocr module: {e}")
        robust_log(f"Import error traceback: {traceback.format_exc()}")
        sys.exit(1)


# Global import - do this at module level
iva_indetraibile_ocr = safe_import()


def get_db_config():
    """Legge configurazione database da environment variables"""
    try:
        robust_log("Loading database configuration...")
        config = {
            "host": os.getenv('DB_HOST', 'polis-coll-mysql.mysql.database.azure.com'),
            "port": int(os.getenv('DB_PORT', '3306')),
            "user": os.getenv('DB_USER', 'polisammin'),
            "password": os.getenv('DB_PASSWORD'),
            "database": os.getenv('DB_NAME', 'polis'),
            "pool_size": int(os.getenv('DB_POOL_SIZE', '10')),
            "pool_reset_session": os.getenv('DB_POOL_RESET', 'True').lower() == 'true'
        }
        robust_log(f"Database config loaded: host={config['host']}, port={config['port']}, database={config['database']}")
        return config
    except Exception as e:
        robust_log(f"ERROR loading database config: {e}")
        raise


def get_api_config():
    """Legge configurazione API da environment variables"""
    try:
        robust_log("Loading API configuration...")

        # Costruisci URL da base URL se disponibile
        azure_storage_api_url = os.getenv('AZURE_STORAGE_API_URL')
        if azure_storage_api_url:
            base_search_url = f"{azure_storage_api_url}/search"
            base_download_url = f"{azure_storage_api_url}/download_base64"
        else:
            base_search_url = os.getenv('AZURE_SEARCH_URL')
            base_download_url = os.getenv('AZURE_DOWNLOAD_URL')

        config = {
            "base_search_url": base_search_url,
            "base_download_url": base_download_url,
            "import_path_url": os.getenv('AZURE_IMPORT_PATH_URL'),
            "search_in_folder_url": os.getenv('AZURE_SEARCH_IN_FOLDER_URL'),
            "openai_api_key": os.getenv('AZURE_OPENAI_KEY'),
            "openai_endpoint": os.getenv('AZURE_OPENAI_ENDPOINT'),
            "openai_deployment": os.getenv('AZURE_OPENAI_DEPLOYMENT'),
            "document_intelligence_endpoint": os.getenv('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT'),
            "document_intelligence_key": os.getenv('AZURE_DOCUMENT_INTELLIGENCE_KEY')
        }
        robust_log(f"API config loaded: search_url={config['base_search_url'][:50] if config['base_search_url'] else 'None'}...")
        return config
    except Exception as e:
        robust_log(f"ERROR loading API config: {e}")
        raise


def test_connections():
    """Test database and API connections"""
    robust_log("=== STARTING CONNECTION TESTS ===")

    try:
        db_config = get_db_config()
        api_config = get_api_config()

        # Test database
        robust_log("Testing database connection...")
        try:
            result = fetch_mysql_query("SELECT 1 as test", db_config)
            robust_log(f"Database test query result: {result}")
            robust_log("✅ Database connection OK")
            db_ok = True
        except Exception as e:
            robust_log(f"❌ Database connection FAILED: {e}")
            robust_log(f"Database error details: {traceback.format_exc()}")
            db_ok = False

        # Test Azure API
        robust_log("Testing Azure API connection...")
        try:
            import requests
            test_url = f"{api_config['base_search_url']}/unified?search_type=folders&limit=1&root_only=true"
            robust_log(f"Making request to: {test_url}")
            response = requests.get(test_url, timeout=10)
            robust_log(f"API response status code: {response.status_code}")

            if response.status_code == 200:
                robust_log("✅ API connection OK")
                api_ok = True
            else:
                robust_log(f"❌ API connection FAILED - HTTP {response.status_code}")
                api_ok = False
        except Exception as e:
            robust_log(f"❌ API connection FAILED: {e}")
            robust_log(f"API error details: {traceback.format_exc()}")
            api_ok = False

        overall_result = db_ok and api_ok
        robust_log(f"=== CONNECTION TESTS COMPLETED: {'SUCCESS' if overall_result else 'FAILED'} ===")
        return overall_result

    except Exception as e:
        robust_log(f"FATAL ERROR in test_connections: {e}")
        robust_log(f"Error details: {traceback.format_exc()}")
        return False


def dry_run(sal_folder=None):
    """Show what files would be processed without doing anything"""
    # Use environment variable if sal_folder not provided
    if sal_folder is None:
        sal_folder = os.getenv('SAL_STORAGE_NAME')

    robust_log("=== STARTING DRY RUN ===")
    robust_log(f"SAL folder: {sal_folder}")

    # Set environment variable for API calls
    if sal_folder:
        os.environ['SAL_STORAGE_NAME'] = sal_folder

    try:
        robust_log("Searching for IVA Indetraibile PDF files...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("iva_indetraibile", sal_name=sal_folder)
        robust_log(f"Search returned: {type(pdf_paths)} with {len(pdf_paths) if pdf_paths else 0} items")
        
        if pdf_paths:
            robust_log(f"Found {len(pdf_paths)} PDF files:")
            for i, path in enumerate(pdf_paths, 1):
                if i <= 10: # Show only first 10
                    robust_log(f" {i}. {path}")
                elif i == 11:
                    robust_log(f" ... and {len(pdf_paths) - 10} more files")
            robust_log("=== DRY RUN COMPLETED: SUCCESS ===")
            return True
        else:
            robust_log("No PDF files found")
            robust_log("=== DRY RUN COMPLETED: NO FILES ===")
            return False
            
    except Exception as e:
        robust_log(f"ERROR during dry run: {e}")
        robust_log(f"Dry run error details: {traceback.format_exc()}")
        robust_log("=== DRY RUN COMPLETED: FAILED ===")
        return False

def run_import(sal_folder=None, duplicate_handling="skip", limit=None, start_index=None, batch_size=None, max_workers=None):
    """Run the actual import process with duplicate handling and progress tracking"""
    # Use environment variable if sal_folder not provided
    if sal_folder is None:
        sal_folder = os.getenv('SAL_STORAGE_NAME')

    # Set environment variable for API calls
    if sal_folder:
        os.environ['SAL_STORAGE_NAME'] = sal_folder

    # Usa environment variable se max_workers non specificato
    if max_workers is None:
        max_workers = int(os.getenv('MAX_WORKERS', '4'))

    robust_log(f"=== STARTING IVA INDETRAIBILE IMPORT PROCESS ===")
    robust_log(f"Parameters: sal_folder={sal_folder}, duplicate_handling={duplicate_handling}")
    robust_log(f"Batch parameters: limit={limit}, start_index={start_index}, batch_size={batch_size}")
    robust_log(f"Max workers: {max_workers}")

    start_time = time.time()
    
    try:
        # Get configurations
        db_config = get_db_config()
        api_config = get_api_config()

        # Get PDF paths
        robust_log("Searching for IVA Indetraibile PDF files...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("iva_indetraibile", sal_name=sal_folder)

        if not pdf_paths:
            robust_log("❌ No IVA Indetraibile PDFs found")
            return False

        robust_log(f"Found {len(pdf_paths)} PDF files")

        # Apply batch limits
        if start_index is not None:
            pdf_paths = pdf_paths[start_index:]
            robust_log(f"Starting from index {start_index}")

        if limit is not None:
            pdf_paths = pdf_paths[:limit]
            robust_log(f"Limited to {limit} files")

        base_download_url = api_config['base_download_url']

        # Process PDFs in parallel
        robust_log(f"Processing {len(pdf_paths)} PDFs with {max_workers} workers...")
        all_sql = []

        def process_single_iva_pdf(path):
            """Process a single PDF and return SQL"""
            try:
                url = f"{base_download_url}/{path}"
                robust_log(f"Processing: {path}")
                sql = iva_indetraibile_ocr.process_pdf_with_retry(url)
                if sql:
                    robust_log(f"✅ Processed: {path}")
                    return sql
                else:
                    robust_log(f"⚠️  No data extracted from: {path}")
                    return None
            except Exception as e:
                robust_log(f"❌ Error processing {path}: {e}")
                return None

        # Use ThreadPoolExecutor for parallel processing
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_path = {executor.submit(process_single_iva_pdf, path): path for path in pdf_paths}

            for future in concurrent.futures.as_completed(future_to_path):
                sql = future.result()
                if sql:
                    all_sql.append(sql)

        if not all_sql:
            robust_log("❌ No SQL statements generated")
            return False

        robust_log(f"Generated {len(all_sql)} SQL statements")

        # Execute SQL
        robust_log("Executing SQL statements...")
        success = execute_batch_queries(all_sql, db_config)

        elapsed_time = time.time() - start_time
        robust_log(f"\n{'='*60}")
        robust_log(f"✅ Import completed in {elapsed_time:.1f} seconds")
        robust_log(f"Check with: SELECT COUNT(*) FROM indetraibilita_iva_pdf;")
        robust_log(f"{'='*60}")

        return success

    except Exception as e:
        robust_log(f"FATAL ERROR in run_import: {e}")
        robust_log(f"Error details: {traceback.format_exc()}")
        return False


def main(mode='import'):
    """Main processing function - Orchestrazione (for backward compatibility)"""
    if mode == 'dry-run':
        return dry_run()
    elif mode == 'test':
        return test_connections()
    else:
        return run_import()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='IVA Indetraibile OCR Processor')
    parser.add_argument('--mode', choices=['test', 'dry-run', 'import'], 
                       default='import',
                       help='Processing mode')
    
    args = parser.parse_args()
    
    robust_log(f"Starting with mode: {args.mode}")
    
    try:
        if args.mode == 'dry-run':
            robust_log("Calling dry_run...")
            success = dry_run()
            robust_log(f"dry_run completed with result: {success}")
        elif args.mode == 'test':
            robust_log("Test mode - checking configuration...")
            success = dry_run() # Test mode = dry run
        else: # import
            robust_log("Calling main import...")
            success = main(mode=args.mode)
            robust_log(f"main completed with result: {success}")
        
        exit(0 if success else 1)
        
    except Exception as e:
        robust_log(f"FATAL ERROR: {e}")
        robust_log(f"Error traceback: {traceback.format_exc()}")
        exit(1)