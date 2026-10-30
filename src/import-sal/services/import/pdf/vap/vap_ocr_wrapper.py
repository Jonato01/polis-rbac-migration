#!/usr/bin/env python3
"""
Enhanced wrapper for existing vap_ocr.py with environment-based configuration
"""

from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import argparse
import sys
import os
import concurrent.futures
from datetime import datetime
import traceback
import time

# Path setup - AGGIUNGERE QUESTO
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
    
from ocr_common import fetch_mysql_query, get_pdf_paths_from_sal, process_single_pdf, execute_batch_queries

# Logging helper (ripristinato)
def robust_log(message, force_flush=True):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    print(log_msg)
    if force_flush:
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except:
            pass

# Safe import vap_ocr (ripristinato)
vap_ocr = None

def safe_import():
    global vap_ocr
    if vap_ocr is not None:
        return vap_ocr
    try:
        robust_log("Importing vap_ocr module...")
        import vap_ocr as _vap
        vap_ocr = _vap
        robust_log("vap_ocr module imported")
        return vap_ocr
    except Exception as e:
        robust_log(f"FATAL: Cannot import vap_ocr: {e}")
        robust_log(traceback.format_exc())
        sys.exit(1)

safe_import()

# DB config (ripristinato minimale)
def get_db_config():
    try:
        config = {
            "host": os.getenv('DB_HOST', 'colibri-mysql-flexible.mysql.database.azure.com'),
            "port": int(os.getenv('DB_PORT', '3306')),
            "user": os.getenv('DB_USER', 'polisammin'),
            "password": os.getenv('DB_PASSWORD', 'ComplexPass123!'),
            "database": os.getenv('DB_NAME', 'polis'),
            "pool_size": int(os.getenv('DB_POOL_SIZE', '10')),
            "pool_reset_session": os.getenv('DB_POOL_RESET', 'True').lower() == 'true'
        }
        robust_log(f"Database config loaded: host={config['host']}, port={config['port']}, database={config['database']}")
        return config
    except Exception as e:
        robust_log(f"ERROR loading database config: {e}")
        raise

# ============================ NEW: SCALING HELPERS ============================
def count_pdfs_to_import(doc_type: str = "VAP") -> int:
    """Conta i PDF disponibili per il tipo documento (default VAP)."""
    try:
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        paths = get_pdf_paths_from_sal(doc_type, sal_name=sal_folder)
        return len(paths) if paths else 0
    except Exception as e:
        robust_log(f"[SCALING] Error counting PDFs: {e}")
        return 0


def calculate_optimal_workers(total_pdfs: int, base_workers: int) -> tuple[int, int]:
    """Calcola il numero ottimale di workers e batch suggerito.
    Ritorna (optimal_workers, suggested_batch_size).
    Usa il più alto tra base_workers e MAX_WORKERS_ABSOLUTE come limite effettivo.
    Mantiene comportamento (1,1) se total_pdfs == 0.
    """
    try:
        MAX_WORKERS_ABSOLUTE = int(os.getenv('MAX_WORKERS_ABSOLUTE', '4'))
        SCALING_MULTIPLIER = float(os.getenv('SCALING_MULTIPLIER', '2.0'))
        PDFS_PER_WORKER = int(os.getenv('PDFS_PER_WORKER', '10'))

        effective_cap = max(base_workers, MAX_WORKERS_ABSOLUTE)

        if total_pdfs == 0:
            return 1, 1
        if total_pdfs <= 10:
            optimal = min(total_pdfs, effective_cap, max(1, min(base_workers, total_pdfs)))
            batch = total_pdfs
        elif total_pdfs <= 50:
            raw = max(int(total_pdfs / PDFS_PER_WORKER), base_workers)
            optimal = min(raw, effective_cap)
            batch = max(int(total_pdfs / 2), 5)
        elif total_pdfs <= 100:
            raw = int(total_pdfs / PDFS_PER_WORKER * SCALING_MULTIPLIER)
            optimal = min(raw, effective_cap)
            batch = max(int(total_pdfs / 3), 10)
        else:
            raw = int(total_pdfs / PDFS_PER_WORKER * SCALING_MULTIPLIER)
            optimal = min(raw, effective_cap)
            batch = max(int(total_pdfs / max(1, optimal)), 10)

        optimal = max(1, min(optimal, effective_cap))
        batch = max(1, batch)
        return optimal, batch
    except Exception as e:
        robust_log(f"[SCALING] Error calculating optimal workers: {e}")
        return base_workers, max(1, total_pdfs)
# ============================================================================


def get_api_config():
    """Legge configurazione API da environment variables"""
    try:
        robust_log("Loading API configuration...")
        config = {
            "base_search_url": os.getenv('AZURE_SEARCH_URL', 'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/search'),
            "base_download_url": os.getenv('AZURE_DOWNLOAD_URL', 'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/download_base64'),
            "openai_api_key": os.getenv('OPENAI_API_KEY', '')
        }
        robust_log(f"API config loaded: search_url={config['base_search_url'][:50]}...")
        if config['openai_api_key']:
            robust_log(f"OpenAI API key loaded: {config['openai_api_key'][:8]}...")
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
            robust_log("Database connection: SUCCESS")
            db_ok = True
        except Exception as e:
            robust_log(f"Database connection: FAILED - {e}")
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
                robust_log("Azure API connection: SUCCESS")
                api_ok = True
            else:
                robust_log(f"Azure API connection: FAILED - HTTP {response.status_code}")
                api_ok = False
        except Exception as e:
            robust_log(f"Azure API connection: FAILED - {e}")
            robust_log(f"API error details: {traceback.format_exc()}")
            api_ok = False
        
        overall_result = db_ok and api_ok
        robust_log(f"=== CONNECTION TESTS COMPLETED: {'SUCCESS' if overall_result else 'FAILED'} ===")
        return overall_result
        
    except Exception as e:
        robust_log(f"FATAL ERROR in test_connections: {e}")
        robust_log(f"Error details: {traceback.format_exc()}")
        return False


def dry_run(sal_folder):
    """Show what files would be processed without doing anything"""
    robust_log(f"=== STARTING DRY RUN ===")
    robust_log(f"SAL folder: {sal_folder}")
    
    try:
        _ = get_api_config()  # Only to ensure config loads
        robust_log("Searching for VAP PDF files...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("VAP", sal_name=sal_folder)
        robust_log(f"Search returned: {type(pdf_paths)} with {len(pdf_paths) if pdf_paths else 0} items")
        
        if pdf_paths:
            robust_log(f"Found {len(pdf_paths)} PDF files:")
            for i, path in enumerate(pdf_paths, 1):
                if i <= 10:
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


def run_import(sal_folder, duplicate_handling="skip", limit=None, start_index=None, batch_size=None, max_workers=None):
    """Run the actual import process with duplicate handling and progress tracking"""
    if max_workers is None:
        max_workers = int(os.getenv('MAX_WORKERS', '4'))

    robust_log(f"=== STARTING VAP IMPORT PROCESS ===")
    robust_log(f"Parameters: sal_folder={sal_folder}, duplicate_handling={duplicate_handling}")
    robust_log(f"Batch parameters: limit={limit}, start_index={start_index}, batch_size={batch_size}")
    robust_log(f"Max workers (requested): {max_workers}")

    try:
        robust_log("Loading configurations...")
        db_config = get_db_config()
        api_config = get_api_config()
        robust_log("Configurations loaded successfully")

        robust_log("Searching for VAP PDF files...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("VAP", sal_name=sal_folder)
        robust_log(f"Search completed. Found: {len(pdf_paths) if pdf_paths else 0} files")

        if not pdf_paths:
            robust_log("ERROR: No PDF files found")
            robust_log("=== IMPORT PROCESS COMPLETED: NO FILES ===")
            return False

        robust_log("Creating download URLs...")
        pdf_urls = [f"{api_config['base_download_url']}/{path}" for path in pdf_paths]
        total_available = len(pdf_urls)
        robust_log(f"Created {total_available} download URLs")

        if start_index is not None and batch_size is not None:
            end_index = min(start_index + batch_size, total_available)
            pdf_urls = pdf_urls[start_index:end_index]
            robust_log(f"Applied batch: processing files {start_index}-{end_index-1} ({len(pdf_urls)} files)")
        elif limit and limit > 0:
            pdf_urls = pdf_urls[:limit]
            robust_log(f"Applied limit: processing first {len(pdf_urls)} files")

        total_pdfs = len(pdf_urls)
        if total_pdfs == 0:
            robust_log("ERROR: No files in specified batch range")
            robust_log("=== IMPORT PROCESS COMPLETED: EMPTY BATCH ===")
            return False

        robust_log("Files to be processed:")
        for i, url in enumerate(pdf_urls, 1):
            if i <= 5:
                file_index = (start_index or 0) + i - 1
                robust_log(f" [{file_index}] {url.split('/')[-1] if '/' in url else url}")
            elif i == 6 and total_pdfs > 5:
                robust_log(f" ... and {total_pdfs - 5} more files")

        robust_log("Getting VAP text prompt...")
        text_input = vap_ocr.get_vap_json_prompt()
        robust_log(f"Text prompt loaded: {len(text_input)} characters")

        robust_log("Starting PDF processing...")
        all_queries = []
        completed = 0
        max_workers = min(max_workers, total_pdfs)
        robust_log(f"Using {max_workers} parallel workers")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            robust_log("Submitting PDF processing tasks...")
            future_to_url = {
                executor.submit(process_single_pdf, url, api_config['openai_api_key'], 
                                text_input, vap_ocr.json_to_vap_queries): url
                for url in pdf_urls
            }
            robust_log(f"Submitted {len(future_to_url)} tasks to executor")

            robust_log("Waiting for PDF processing results...")
            for future in concurrent.futures.as_completed(future_to_url):
                completed += 1
                url = future_to_url[future]
                try:
                    robust_log(f"[{completed}/{total_pdfs}] Getting result for {url.split('/')[-1] if '/' in url else url}")
                    result = future.result()
                    if result:
                        all_queries.append(result)
                        robust_log(f"[{completed}/{total_pdfs}] PDF processed successfully - generated {len(result)} characters of SQL")
                    else:
                        robust_log(f"[{completed}/{total_pdfs}] PDF skipped (no valid content)")
                except Exception as exc:
                    robust_log(f"[{completed}/{total_pdfs}] PDF processing failed: {exc}")
                    robust_log(f"PDF processing error details: {traceback.format_exc()}")
                percentage = (completed / total_pdfs) * 100
                robust_log(f"Progress: {percentage:.1f}% ({completed}/{total_pdfs})")

        robust_log(f"PDF processing completed. Generated {len(all_queries)} valid queries")
        if not all_queries:
            robust_log("ERROR: No valid queries generated from PDFs")
            robust_log("=== IMPORT PROCESS COMPLETED: NO VALID QUERIES ===")
            return False

        robust_log("Starting database operations...")
        robust_log(f"Executing {len(all_queries)} queries")
        success = execute_batch_queries(all_queries, db_config)
        if success:
            robust_log("Database operations completed successfully!")
            robust_log("=== IMPORT PROCESS COMPLETED: SUCCESS ===")
        else:
            robust_log("Database operations failed")
            robust_log("=== IMPORT PROCESS COMPLETED: DATABASE FAILED ===")
        return success
    except Exception as e:
        robust_log(f"FATAL ERROR during import: {e}")
        robust_log(f"Import error details: {traceback.format_exc()}")
        robust_log("=== IMPORT PROCESS COMPLETED: FATAL ERROR ===")
        return False


def main():
    robust_log("=" * 60)
    robust_log("VAP OCR PROCESSOR - ENHANCED WRAPPER")
    robust_log("=" * 60)
    try:
        robust_log(f"Python version: {sys.version}")
        robust_log(f"Working directory: {os.getcwd()}")
        robust_log(f"Script path: {os.path.abspath(__file__)}")
        robust_log(f"Available files: {[f for f in os.listdir('.') if f.endswith('.py')]}")
        robust_log("Parsing command line arguments...")
        parser = argparse.ArgumentParser(description='VAP OCR Processor - Enhanced Wrapper')
        parser.add_argument('--mode', choices=['test', 'dry-run', 'import'], default='import', help='Operation mode')
        parser.add_argument('--sal-folder', default='8_SAL_30_04_2025', help='SAL folder name')
        parser.add_argument('--duplicate-handling', choices=['skip', 'replace', 'none'], default='skip', help='Duplicate handling strategy')
        parser.add_argument('--limit', type=int, help='Limit number of PDF files to process')
        parser.add_argument('--start-index', type=int, help='Start index for batch processing')
        parser.add_argument('--batch-size', type=int, help='Batch size for multi-container processing')
        parser.add_argument('--max-workers', type=int, default=4, help='Parallel workers requested')
        args = parser.parse_args()
        robust_log("Arguments parsed successfully")
        robust_log("Configuration:")
        robust_log(f" Mode: {args.mode}")
        robust_log(f" SAL Folder: {args.sal_folder}")
        robust_log(f" Duplicate Handling: {args.duplicate_handling}")
        robust_log(f" File Limit: {args.limit}")
        robust_log(f" Start Index: {args.start_index}")
        robust_log(f" Batch Size: {args.batch_size}")
        robust_log(f" Max Workers: {args.max_workers}")
        robust_log(f" Timestamp: {datetime.now()}")
        robust_log("-" * 60)
        robust_log(f"Starting execution in {args.mode} mode...")
        if args.mode == 'test':
            robust_log("Calling test_connections...")
            success = test_connections()
            robust_log(f"test_connections completed with result: {success}")
        elif args.mode == 'dry-run':
            robust_log("Calling dry_run...")
            success = dry_run(args.sal_folder)
            robust_log(f"dry_run completed with result: {success}")
        else:
            robust_log("Calling run_import...")
            success = run_import(args.sal_folder, args.duplicate_handling, args.limit, args.start_index, args.batch_size, args.max_workers)
            robust_log(f"run_import completed with result: {success}")
        robust_log("-" * 60)
        robust_log(f"FINAL RESULT: {'SUCCESS' if success else 'FAILED'}")
        robust_log("=" * 60)
        sys.stdout.flush(); sys.stderr.flush()
        robust_log("Waiting 5 seconds to ensure logs are captured...")
        time.sleep(5)
        robust_log("Process completed. Exiting.")
        return success
    except Exception as e:
        robust_log("=" * 60)
        robust_log(f"FATAL ERROR in main: {e}")
        robust_log(traceback.format_exc())
        robust_log("=" * 60)
        sys.stdout.flush(); sys.stderr.flush()
        time.sleep(5)
        return False

if __name__ == "__main__":
    try:
        robust_log("Starting VAP OCR Processor...")
        success = main()
        exit_code = 0 if success else 1
        robust_log(f"Exiting with code: {exit_code}")
        sys.exit(exit_code)
    except Exception as final_error:
        print(f"ULTIMATE FATAL ERROR: {final_error}")
        traceback.print_exc()
        sys.stdout.flush(); sys.stderr.flush()
        time.sleep(3)
        sys.exit(1)