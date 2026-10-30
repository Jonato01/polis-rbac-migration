#!/usr/bin/env python3
"""
Enhanced wrapper for fatture_ocr.py with COMPLETE error logging
v5.0 - ALL failures logged to database (ocr_failed, exception, skip)
"""

import argparse
import sys
import os

# Path setup
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

import concurrent.futures
from datetime import datetime
import traceback
import time

from ocr_common import execute_batch_queries, fetch_mysql_query, get_pdf_paths_from_sal, process_single_pdf


# ============================================================================
# LOGGING SETUP - File + Console
# ============================================================================

_log_file = None


def setup_logging(log_filename=None):
    """Setup logging to file and console"""
    global _log_file
    
    if log_filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"fatture_ocr_{timestamp}.log"
    
    try:
        _log_file = open(log_filename, 'a', encoding='utf-8', buffering=1)
        robust_log(f"Logging to file: {log_filename}")
    except Exception as e:
        robust_log(f"Could not open log file: {e}")
        _log_file = None


def close_logging():
    """Close log file"""
    global _log_file
    if _log_file:
        try:
            _log_file.close()
            _log_file = None
        except:
            pass


def robust_log(message, force_flush=True):
    """Log message with timestamp to console AND file"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    
    print(log_msg)
    
    if _log_file:
        try:
            _log_file.write(log_msg + '\n')
            if force_flush:
                _log_file.flush()
        except:
            pass
    
    if force_flush:
        sys.stdout.flush()
        sys.stderr.flush()


# ============================================================================
# SAFE IMPORT
# ============================================================================

def safe_import():
    """Import fatture_ocr module safely with logging"""
    try:
        robust_log("Attempting to import fatture_ocr module...")
        import fatture_ocr
        robust_log("fatture_ocr module imported successfully")
        return fatture_ocr
    except Exception as e:
        robust_log(f"FATAL: Failed to import fatture_ocr module: {e}")
        robust_log(f"Import error traceback: {traceback.format_exc()}")
        sys.exit(1)


# Global import
fatture_ocr = safe_import()


# ============================================================================
# CONFIGURATION
# ============================================================================

def get_db_config():
    """Legge configurazione database da environment variables"""
    try:
        robust_log("Loading database configuration...")
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


def get_api_config():
    """Legge configurazione API da environment variables"""
    try:
        robust_log("Loading API configuration...")
        config = {
            "base_search_url": os.getenv('AZURE_SEARCH_URL', 'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/search'),
            "base_download_url": os.getenv('AZURE_DOWNLOAD_URL', 'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/download_base64'),
            # Azure OpenAI configuration (used in ocr_common.py)
            "azure_openai_endpoint": os.getenv('AZURE_OPENAI_ENDPOINT'),
            "azure_openai_key": os.getenv('AZURE_OPENAI_KEY'),
            "azure_openai_deployment": os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4.1')
        }

        if config['azure_openai_endpoint']:
            robust_log(f"Azure OpenAI endpoint: {config['azure_openai_endpoint'][:50]}...")

        return config
    except Exception as e:
        robust_log(f"ERROR loading API config: {e}")
        raise


# ============================================================================
# MODIFIED process_single_pdf for v5.0
# ============================================================================

def process_single_pdf_v5(pdf_url, api_key, text_input, json_to_queries_func):
    """
    Process a single PDF and return tuple (success_queries, error_query)
    v5.0: Returns tuple instead of single result
    """
    try:
        from ocr_common import pdf_to_json_via_gpt_vision
        
        json_response = pdf_to_json_via_gpt_vision(pdf_url, api_key, text_input)

        if json_response and not json_response.startswith("Error:"):
            # json_to_queries_func now returns (success_queries, error_query)
            success_queries, error_query = json_to_queries_func(json_response, pdf_url)
            
            if success_queries:
                # Successo: converto lista in stringa SQL unica
                sql_string = "; ".join(success_queries) if isinstance(success_queries, list) else success_queries
                return ('success', sql_string)
            elif error_query:
                # Errore validazione: ritorna query errore
                return ('error', error_query)
            else:
                # Caso imprevisto
                return ('skip', None)
        else:
            # OCR ha fallito completamente
            return ('ocr_failed', pdf_url)
            
    except Exception as e:
        print(f"Error processing PDF {pdf_url}: {e}")
        return ('exception', (pdf_url, str(e)))


# ============================================================================
# TEST & DRY-RUN FUNCTIONS
# ============================================================================

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


def dry_run(sal_folder=None):
    """Show what files would be processed without doing anything"""
    # Use environment variable if sal_folder not provided
    if sal_folder is None:
        sal_folder = os.getenv('SAL_STORAGE_NAME')

    robust_log(f"=== STARTING DRY RUN ===")
    robust_log(f"SAL folder: {sal_folder}")
    
    # Set environment variable for API calls
    if sal_folder:
        os.environ['SAL_STORAGE_NAME'] = sal_folder

    try:
        robust_log("Searching for FATTURE PDF files...")
        pdf_paths = get_pdf_paths_from_sal("FATTURE", sal_name=sal_folder)  # <-- Aggiunto sal_name
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


# ============================================================================
# DYNAMIC SCALING FUNCTIONS
# ============================================================================

def calculate_optimal_workers(total_pdfs, max_workers=None):
    """Calcola il numero ottimale di worker in base ai PDF, usando effective_cap = max(max_workers, MAX_WORKERS_ABSOLUTE)."""
    if max_workers is None:
        max_workers = int(os.getenv('MAX_WORKERS', '4'))
    max_workers_absolute = int(os.getenv('MAX_WORKERS_ABSOLUTE', '4'))
    scaling_multiplier = float(os.getenv('SCALING_MULTIPLIER', '2.0'))
    pdfs_per_worker = int(os.getenv('PDFS_PER_WORKER', '10'))
    effective_cap = max(max_workers, max_workers_absolute)

    if total_pdfs == 0:
        return 1, 1
    if total_pdfs <= 5:
        optimal = min(2, effective_cap)
        return optimal, total_pdfs
    if total_pdfs <= 20:
        optimal = min(max(2, max_workers // 2), effective_cap)
        return optimal, total_pdfs
    if total_pdfs <= 50:
        optimal = min(max_workers, effective_cap)
        batch = max(pdfs_per_worker, total_pdfs // max(1, optimal))
        return optimal, batch
    if total_pdfs <= 100:
        optimal = min(max_workers, effective_cap)
        batch = max(15, total_pdfs // max(1, optimal))
        return optimal, batch

    calculated_workers = max(1, total_pdfs // max(1, pdfs_per_worker))
    scaled = int(max_workers * scaling_multiplier)
    suggested_workers = min(max(calculated_workers, max_workers), scaled, effective_cap)
    batch_size = max(20, total_pdfs // max(1, suggested_workers))
    return suggested_workers, batch_size


def count_pdfs_to_import(sal_name=None):
    """
    Conta il numero di PDF da importare per le fatture

    Args:
        sal_name: Nome del SAL (es. "9_SAL_31_08_2025")

    Returns:
        int: Numero di PDF trovati
    """
    if sal_name is None:
        sal_name = os.getenv('SAL_STORAGE_NAME')

    try:
        robust_log(f"Counting PDFs for FATTURE in SAL: {sal_name}")
        pdf_paths = get_pdf_paths_from_sal("FATTURE", sal_name=sal_name)

        if pdf_paths:
            count = len(pdf_paths)
            robust_log(f"Found {count} PDF files to import")
            return count
        else:
            robust_log("No PDF files found")
            return 0
    except Exception as e:
        robust_log(f"Error counting PDFs: {e}")
        return 0


# ============================================================================
# IMPORT PROCESS
# ============================================================================

def run_import(sal_folder=None, duplicate_handling="skip", limit=None, start_index=None, batch_size=None, max_workers=None, auto_scale=True):
    """Run the actual import process with COMPLETE error tracking and dynamic scaling"""
    # Use environment variable if sal_folder not provided
    if sal_folder is None:
        sal_folder = os.getenv('SAL_STORAGE_NAME')

    # Set environment variable for API calls
    if sal_folder:
        os.environ['SAL_STORAGE_NAME'] = sal_folder

    robust_log(f"=== STARTING FATTURE IMPORT PROCESS v6.0 (DYNAMIC SCALING) ===")
    robust_log(f"Parameters: sal_folder={sal_folder}, duplicate_handling={duplicate_handling}")
    robust_log(f"Auto-scaling enabled: {auto_scale}")

    # FASE 1: CONTA I PDF DA IMPORTARE E CALCOLA SCALING
    if auto_scale:
        robust_log("=" * 60)
        robust_log("DYNAMIC SCALING ANALYSIS")
        robust_log("=" * 60)

        total_pdf_count = count_pdfs_to_import(sal_folder)

        if total_pdf_count == 0:
            robust_log("No PDFs to import. Exiting.")
            return True

        # Calcola worker e batch size ottimali
        optimal_workers, suggested_batch = calculate_optimal_workers(total_pdf_count, max_workers)

        robust_log(f"📊 SCALING DECISION:")
        robust_log(f"   Total PDFs to import: {total_pdf_count}")
        robust_log(f"   MAX_WORKERS (env): {os.getenv('MAX_WORKERS', '4')}")
        robust_log(f"   MAX_WORKERS_ABSOLUTE (env): {os.getenv('MAX_WORKERS_ABSOLUTE', '4')}")
        robust_log(f"   SCALING_MULTIPLIER (env): {os.getenv('SCALING_MULTIPLIER', '2.0')}")
        robust_log(f"   PDFS_PER_WORKER (env): {os.getenv('PDFS_PER_WORKER', '10')}")
        robust_log(f"   ---")
        robust_log(f"   ✅ Calculated optimal workers: {optimal_workers}")
        robust_log(f"   ✅ Suggested batch size: {suggested_batch}")

        # Usa i valori calcolati se non specificati
        if max_workers is None:
            max_workers = optimal_workers
            robust_log(f"   ✅ Using calculated max_workers: {max_workers}")

        if batch_size is None and total_pdf_count > 20:
            batch_size = suggested_batch
            robust_log(f"   ✅ Using calculated batch_size: {batch_size}")

        robust_log("=" * 60)
    else:
        # Modalità legacy: usa environment variable o default
        if max_workers is None:
            max_workers = int(os.getenv('MAX_WORKERS', '4'))
        robust_log(f"Auto-scaling disabled. Using fixed max_workers: {max_workers}")

    robust_log(f"Final parameters: limit={limit}, start_index={start_index}, batch_size={batch_size}")
    robust_log(f"Max workers: {max_workers}")
    
    try:
        # Get configurations
        robust_log("Loading configurations...")
        db_config = get_db_config()
        api_config = get_api_config()
        robust_log("Configurations loaded successfully")

        # Search for PDF files
        robust_log("Searching for FATTURE PDF files...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("FATTURE", sal_name=sal_folder)
        robust_log(f"Search completed. Found: {len(pdf_paths) if pdf_paths else 0} files")

        if not pdf_paths:
            robust_log("ERROR: No PDF files found")
            return False

        # Create download URLs
        robust_log("Creating download URLs...")
        pdf_urls = [f"{api_config['base_download_url']}/{path}" for path in pdf_paths]
        total_available = len(pdf_urls)
        robust_log(f"Created {total_available} download URLs")

        # Apply batch parameters if specified
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
            return False

        # Show which files will be processed
        robust_log("Files to be processed:")
        for i, url in enumerate(pdf_urls, 1):
            if i <= 5:
                file_index = (start_index or 0) + i - 1
                robust_log(f" [{file_index}] {url.split('/')[-1] if '/' in url else url}")
            elif i == 6 and total_pdfs > 5:
                robust_log(f" ... and {total_pdfs - 5} more files")

        # Get text prompt
        robust_log("Getting FATTURE text prompt...")
        text_input = fatture_ocr.get_fattura_json_prompt()
        robust_log(f"Text prompt loaded: {len(text_input)} characters")

        # Statistics counters
        total_inserted = 0
        total_errors_logged = 0
        completed = 0
        
        max_workers = min(max_workers, total_pdfs)
        robust_log(f"Using {max_workers} parallel workers")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            robust_log("Submitting PDF processing tasks...")
            future_to_url = {
                executor.submit(process_single_pdf_v5, url, None, text_input, fatture_ocr.json_to_fatture_queries): url
                for url in pdf_urls
            }
            robust_log(f"Submitted {len(future_to_url)} tasks to executor")

            robust_log("Waiting for PDF processing results...")
            for future in concurrent.futures.as_completed(future_to_url):
                completed += 1
                url = future_to_url[future]
                
                try:
                    filename = url.split('/')[-1] if '/' in url else url
                    path_after_base = url.split('/download_base64/')[-1] if '/download_base64/' in url else url
                    
                    robust_log(f"[{completed}/{total_pdfs}] Processing: {filename}")
                    
                    result_type, result_data = future.result()
                    
                    if result_type == 'success':
                        # INSERT fattura con successo
                        robust_log(f"Executing INSERT for current PDF...")
                        success = execute_batch_queries([result_data], db_config)
                        if success:
                            total_inserted += 1
                            robust_log(f"PDF saved to database (total: {total_inserted})")
                        else:
                            robust_log(f"Failed to save PDF to database")
                            
                    elif result_type == 'error':
                        # INSERT nella tabella errori (validazione fallita)
                        robust_log(f"PDF has validation errors, logging to error table...")
                        success = execute_batch_queries([result_data], db_config)
                        if success:
                            total_errors_logged += 1
                            robust_log(f"Error logged to database (total errors: {total_errors_logged})")
                        else:
                            robust_log(f"Failed to log error to database")
                    
                    elif result_type == 'ocr_failed':
                        # OCR completamente fallito - LOG TO ERROR TABLE
                        robust_log(f"OCR failed completely for: {filename}")
                        error_query = fatture_ocr.log_fattura_error(
                            pdf_url=url,
                            sal=fatture_ocr.extract_sal_from_path(url),
                            tipo_errore="ocr_fallito",
                            dettaglio_errore=f"OCR completely failed - no response from Azure Document Intelligence or GPT-4",
                            json_estratto=None
                        )
                        success = execute_batch_queries([error_query], db_config)
                        if success:
                            total_errors_logged += 1
                            robust_log(f"OCR failure logged to database (total errors: {total_errors_logged})")
                        else:
                            robust_log(f"Failed to log OCR failure to database")
                        
                    elif result_type == 'exception':
                        # Exception Python - LOG TO ERROR TABLE
                        pdf_url_err, error_msg = result_data
                        robust_log(f"Exception during processing: {error_msg}")
                        error_query = fatture_ocr.log_fattura_error(
                            pdf_url=url,
                            sal=fatture_ocr.extract_sal_from_path(url),
                            tipo_errore="altro",
                            dettaglio_errore=f"Python exception: {error_msg[:500]}",
                            json_estratto=None
                        )
                        success = execute_batch_queries([error_query], db_config)
                        if success:
                            total_errors_logged += 1
                            robust_log(f"Exception logged to database (total errors: {total_errors_logged})")
                        else:
                            robust_log(f"Failed to log exception to database")
                    
                    else:
                        # Caso imprevisto (skip) - LOG TO ERROR TABLE
                        robust_log(f"PDF skipped (unknown reason)")
                        error_query = fatture_ocr.log_fattura_error(
                            pdf_url=url,
                            sal=fatture_ocr.extract_sal_from_path(url),
                            tipo_errore="altro",
                            dettaglio_errore="PDF skipped - unknown reason (no success queries, no error query)",
                            json_estratto=None
                        )
                        success = execute_batch_queries([error_query], db_config)
                        if success:
                            total_errors_logged += 1
                            robust_log(f"Skip logged to database (total errors: {total_errors_logged})")
                        else:
                            robust_log(f"Failed to log skip to database")
                    
                except Exception as exc:
                    # Exception nel wrapper stesso - LOG TO ERROR TABLE
                    robust_log(f"[{completed}/{total_pdfs}] PDF processing failed: {exc}")
                    robust_log(f"PDF processing error details: {traceback.format_exc()}")
                    try:
                        error_query = fatture_ocr.log_fattura_error(
                            pdf_url=url,
                            sal=fatture_ocr.extract_sal_from_path(url),
                            tipo_errore="altro",
                            dettaglio_errore=f"Wrapper exception: {str(exc)[:500]}",
                            json_estratto=None
                        )
                        success = execute_batch_queries([error_query], db_config)
                        if success:
                            total_errors_logged += 1
                            robust_log(f"Wrapper exception logged to database (total errors: {total_errors_logged})")
                    except:
                        robust_log(f"Failed to log wrapper exception to database")
                
                # Show progress
                percentage = (completed / total_pdfs) * 100
                robust_log(f"Progress: {percentage:.1f}% ({completed}/{total_pdfs}) | Success: {total_inserted} | Errors: {total_errors_logged}")

        robust_log(f"PDF processing completed.")
        robust_log(f" Successfully inserted: {total_inserted}")
        robust_log(f" Errors logged: {total_errors_logged}")
        robust_log(f" Total processed: {completed}")
        robust_log("=== IMPORT PROCESS COMPLETED ===")
        return True

    except Exception as e:
        robust_log(f"FATAL ERROR during import: {e}")
        robust_log(f"Import error details: {traceback.format_exc()}")
        return False


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main function with comprehensive error handling and logging"""
    
    setup_logging()
    
    robust_log("=" * 60)
    robust_log("FATTURE OCR PROCESSOR - COMPLETE ERROR TRACKING v5.0")
    robust_log("=" * 60)
    
    # Inizializza lo stato di successo per evitare warning
    success = False

    try:
        robust_log(f"Python version: {sys.version}")
        robust_log(f"Working directory: {os.getcwd()}")
        
        robust_log("Parsing command line arguments...")
        parser = argparse.ArgumentParser(description='FATTURE OCR Processor - Complete Error Tracking v5.0')
        
        parser.add_argument('--mode', choices=['test', 'dry-run', 'import'], 
                           default='import', help='Operation mode')
        parser.add_argument('--sal-folder',
                           default=os.getenv('SAL_STORAGE_NAME', '8_SAL_30_04_2025'),
                           help='SAL folder name (can use SAL_STORAGE_NAME env var)')
        parser.add_argument('--duplicate-handling', choices=['skip', 'replace', 'none'],
                           default='skip', help='How to handle duplicate records')
        parser.add_argument('--limit', type=int, help='Limit number of PDF files to process')
        parser.add_argument('--start-index', type=int, help='Start index for batch processing')
        parser.add_argument('--batch-size', type=int, help='Batch size for multi-container processing')
        parser.add_argument('--max-workers', type=int, help='Number of parallel workers')

        args = parser.parse_args()
        robust_log("Arguments parsed successfully")
        
        robust_log("Configuration:")
        robust_log(f" Mode: {args.mode}")
        robust_log(f" Max Workers: {args.max_workers}")
        robust_log(f" File Limit: {args.limit}")
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
            
        elif args.mode == 'import':
            robust_log("Calling run_import...")
            success = run_import(args.sal_folder, args.duplicate_handling, args.limit, 
                               args.start_index, args.batch_size, args.max_workers)
            robust_log(f"run_import completed with result: {success}")
        
        robust_log("-" * 60)
        robust_log(f"FINAL RESULT: {'SUCCESS' if success else 'FAILED'}")
        robust_log("=" * 60)
        
        return success
        
    except Exception as e:
        robust_log("=" * 60)
        robust_log(f"FATAL ERROR in main function: {e}")
        robust_log(f"FATAL ERROR TRACEBACK: {traceback.format_exc()}")
        robust_log("=" * 60)
        return False
    finally:
        close_logging()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    try:
        robust_log("Starting FATTURE OCR Processor v5.0...")
        success = main()
        exit_code = 0 if success else 1
        robust_log(f"Exiting with code: {exit_code}")
        sys.exit(exit_code)
    except Exception as final_error:
        print(f"ULTIMATE FATAL ERROR: {final_error}")
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(5)
        sys.exit(1)