#!/usr/bin/env python3
"""
Enhanced wrapper for nota_trasmissione_ocr.py with environment-based configuration
"""

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


def robust_log(message, force_flush=True):
    """Log message with timestamp and force flush"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    print(log_msg)
    if force_flush:
        sys.stdout.flush()
        sys.stderr.flush()


def safe_import():
    """Import nota_trasmissione_ocr module safely with logging"""
    try:
        robust_log("Attempting to import nota_trasmissione_ocr module...")
        import nota_trasmissione_ocr
        robust_log("nota_trasmissione_ocr module imported successfully")
        return nota_trasmissione_ocr
    except Exception as e:
        robust_log(f"FATAL: Failed to import nota_trasmissione_ocr module: {e}")
        robust_log(f"Import error traceback: {traceback.format_exc()}")
        sys.exit(1)


# Global import - do this at module level
nota_ocr = safe_import()


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
            "base_download_url": os.getenv('AZURE_DOWNLOAD_URL', 
                'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/download_base64'),
            "openai_api_key": os.getenv('OPENAI_API_KEY')
        }
        robust_log(f"API config loaded: download_url={config['base_download_url'][:50]}...")
        if config['openai_api_key']:
            robust_log(f"OpenAI API key loaded: {config['openai_api_key'][:10]}...")
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
            test_url = f"{api_config['base_download_url']}"
            robust_log(f"API URL configured: {test_url[:60]}...")
            api_ok = True
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


def dry_run():
    """Show what files would be processed without doing anything"""
    robust_log(f"=== STARTING DRY RUN ===")
    
    try:
        robust_log("Searching for NOTA_TRASMISSIONE PDF files...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("nota_trasmissione", sal_name=sal_folder)
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


def run_import(duplicate_handling="skip", limit=None, start_index=None, batch_size=None, max_workers=None):
    """Run the actual import process with duplicate handling and progress tracking"""
    # Usa environment variable se max_workers non specificato
    if max_workers is None:
        max_workers = int(os.getenv('MAX_WORKERS', '4'))

    robust_log(f"=== STARTING NOTA_TRASMISSIONE IMPORT PROCESS ===")
    robust_log(f"Parameters: duplicate_handling={duplicate_handling}")
    robust_log(f"Batch parameters: limit={limit}, start_index={start_index}, batch_size={batch_size}")
    robust_log(f"Max workers: {max_workers}")
    
    try:
        # Get configurations
        robust_log("Loading configurations...")
        db_config = get_db_config()
        api_config = get_api_config()
        robust_log("Configurations loaded successfully")

        # Get PDF paths
        robust_log("Fetching PDF file paths...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("nota_trasmissione", sal_name=sal_folder)

        if not pdf_paths:
            robust_log("ERROR: No PDF files found")
            robust_log("=== IMPORT PROCESS COMPLETED: NO FILES ===")
            return False

        total_pdfs = len(pdf_paths)
        robust_log(f"Found {total_pdfs} PDF files")

        # Apply batch slicing
        start_index = start_index or 0
        if batch_size:
            end_index = min(start_index + batch_size, total_pdfs)
            pdf_paths = pdf_paths[start_index:end_index]
            robust_log(f"Batch mode: processing files {start_index} to {end_index-1} ({len(pdf_paths)} files)")
        elif limit:
            pdf_paths = pdf_paths[:limit]
            robust_log(f"Limit mode: processing first {limit} files")

        if not pdf_paths:
            robust_log("ERROR: Batch slice resulted in empty list")
            robust_log("=== IMPORT PROCESS COMPLETED: EMPTY BATCH ===")
            return False

        # Show which files will be processed
        robust_log("Files to be processed:")
        for i, url in enumerate(pdf_paths, 1):
            if i <= 5: # Show first 5
                file_index = (start_index or 0) + i - 1
                robust_log(f" [{file_index}] {url.split('/')[-1] if '/' in url else url}")
            elif i == 6 and len(pdf_paths) > 5:
                robust_log(f" ... and {len(pdf_paths) - 5} more files")

        # Construct full URLs
        base_url = api_config['base_download_url']
        pdf_urls = [f"{base_url}/{path}" for path in pdf_paths]

        # Get text prompt
        robust_log("Getting NOTA_TRASMISSIONE text prompt...")
        text_input = nota_ocr.get_nota_trasmissione_json_prompt()
        robust_log(f"Text prompt loaded: {len(text_input)} characters")

        # Process PDFs
        robust_log("Starting PDF processing...")
        all_queries = []
        completed = 0
        max_workers = min(max_workers, len(pdf_urls))
        robust_log(f"Using {max_workers} parallel workers")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            robust_log("Submitting PDF processing tasks...")
            future_to_url = {
                executor.submit(process_single_pdf, url, api_config['openai_api_key'], 
                               text_input, nota_ocr.json_to_nota_trasmissione_queries): url
                for url in pdf_urls
            }
            robust_log(f"Submitted {len(future_to_url)} tasks to executor")

            # Collect results as they complete
            robust_log("Waiting for PDF processing results...")
            for future in concurrent.futures.as_completed(future_to_url):
                completed += 1
                url = future_to_url[future]
                
                try:
                    robust_log(f"[{completed}/{len(pdf_urls)}] Getting result for {url.split('/')[-1] if '/' in url else url}")
                    result = future.result()
                    
                    if result:
                        all_queries.append(result)
                        robust_log(f"[{completed}/{len(pdf_urls)}] PDF processed successfully - generated {len(result)} characters of SQL")
                    else:
                        robust_log(f"[{completed}/{len(pdf_urls)}] PDF skipped (no valid content)")
                        
                except Exception as exc:
                    robust_log(f"[{completed}/{len(pdf_urls)}] PDF processing failed: {exc}")
                    robust_log(f"PDF processing error details: {traceback.format_exc()}")
                
                # Show progress
                percentage = (completed / len(pdf_urls)) * 100
                robust_log(f"Progress: {percentage:.1f}% ({completed}/{len(pdf_urls)})")

        robust_log(f"PDF processing completed. Generated {len(all_queries)} valid queries")

        if not all_queries:
            robust_log("ERROR: No valid queries generated from PDFs")
            robust_log("=== IMPORT PROCESS COMPLETED: NO VALID QUERIES ===")
            return False

        # Execute queries
        robust_log("Starting database operations...")
        robust_log(f"Executing {len(all_queries)} queries")
        
        success = execute_batch_queries(all_queries, db_config)
        
        if success:
            robust_log(f"Database operations completed successfully!")
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
    """Main function with comprehensive error handling and logging"""
    robust_log("=" * 60)
    robust_log("NOTA_TRASMISSIONE OCR PROCESSOR - ENHANCED WRAPPER")
    robust_log("=" * 60)
    
    try:
        # Show environment info
        robust_log(f"Python version: {sys.version}")
        robust_log(f"Working directory: {os.getcwd()}")
        robust_log(f"Script path: {os.path.abspath(__file__)}")
        
        # Parse arguments
        robust_log("Parsing command line arguments...")
        parser = argparse.ArgumentParser(description='NOTA_TRASMISSIONE OCR Processor - Enhanced Wrapper')
        parser.add_argument('--mode', choices=['test', 'dry-run', 'import'], 
                           default='import', help='Operation mode')
        parser.add_argument('--duplicate-handling', choices=['skip', 'replace', 'none'],
                           default='skip', help='How to handle duplicate records')
        parser.add_argument('--limit', type=int, help='Limit number of PDF files to process (for testing)')
        parser.add_argument('--start-index', type=int, help='Start index for batch processing')
        parser.add_argument('--batch-size', type=int, help='Batch size for multi-container processing')
        parser.add_argument('--max-workers', type=int, default=4, help='Number of parallel workers for PDF processing')

        args = parser.parse_args()
        robust_log("Arguments parsed successfully")
        
        # Show configuration
        robust_log("Configuration:")
        robust_log(f" Mode: {args.mode}")
        robust_log(f" Duplicate Handling: {args.duplicate_handling}")
        robust_log(f" File Limit: {args.limit}")
        robust_log(f" Start Index: {args.start_index}")
        robust_log(f" Batch Size: {args.batch_size}")
        robust_log(f" Max Workers: {args.max_workers}")
        robust_log(f" Timestamp: {datetime.now()}")
        robust_log("-" * 60)
        
        # Execute based on mode
        robust_log(f"Starting execution in {args.mode} mode...")
        
        if args.mode == 'test':
            robust_log("Calling test_connections...")
            success = test_connections()
            robust_log(f"test_connections completed with result: {success}")
            
        elif args.mode == 'dry-run':
            robust_log("Calling dry_run...")
            success = dry_run()
            robust_log(f"dry_run completed with result: {success}")
            
        elif args.mode == 'import':
            robust_log("Calling run_import...")
            success = run_import(args.duplicate_handling, args.limit, 
                               args.start_index, args.batch_size, args.max_workers)
            robust_log(f"run_import completed with result: {success}")
        
        # Final results
        robust_log("-" * 60)
        robust_log(f"FINAL RESULT: {'SUCCESS' if success else 'FAILED'}")
        robust_log("=" * 60)
        
        # Force output and wait
        sys.stdout.flush()
        sys.stderr.flush()
        robust_log("Waiting 10 seconds to ensure logs are captured...")
        time.sleep(10)
        robust_log("Process completed. Exiting.")
        
        return success
        
    except Exception as e:
        robust_log("=" * 60)
        robust_log(f"FATAL ERROR in main function: {e}")
        robust_log(f"FATAL ERROR TRACEBACK: {traceback.format_exc()}")
        robust_log("=" * 60)
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(10) # Wait even on error
        return False


if __name__ == "__main__":
    try:
        robust_log("Starting NOTA_TRASMISSIONE OCR Processor...")
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