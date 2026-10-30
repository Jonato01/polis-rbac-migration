#!/usr/bin/env python3
"""
Nota Metodologica OCR wrapper - Mistral + Azure OpenAI GPT-4.1
"""

import argparse
import sys
import os
import time
from datetime import datetime
import traceback
from dotenv import load_dotenv
import concurrent.futures

# Path setup - DEVE ESSERE PRIMA DEGLI IMPORT LOCALI
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
    
# Carica il file .env
load_dotenv()

# Import DOPO il path setup
from ocr_common import get_pdf_paths_from_sal, fetch_mysql_query, execute_batch_queries, get_db_config

def robust_log(message, force_flush=True):
    """Log message with timestamp and force flush"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    print(log_msg)
    if force_flush:
        sys.stdout.flush()
        sys.stderr.flush()


# Global import - do this at module level
nota_ocr = None

def safe_import():
    """Import nota_metodologica_ocr module safely with logging"""
    try:
        robust_log("Attempting to import nota_metodologica_ocr module...")
        import nota_metodologica_ocr
        robust_log("nota_metodologica_ocr module imported successfully")
        return nota_metodologica_ocr
    except Exception as e:
        robust_log(f"FATAL: Failed to import nota_metodologica_ocr module: {e}")
        robust_log(f"Import error traceback: {traceback.format_exc()}")
        sys.exit(1)

if nota_ocr is None:
    nota_ocr = safe_import()

# ============================================================================
# DYNAMIC SCALING FUNCTIONS
# ============================================================================

def calculate_optimal_workers(total_pdfs, max_workers=None):
    """Calcola il numero ottimale di worker in base al numero di PDF da processare
    Returns tuple: (optimal_workers, suggested_batch_size)
    Uses env vars: MAX_WORKERS, MAX_WORKERS_ABSOLUTE, SCALING_MULTIPLIER, PDFS_PER_WORKER
    Limite effettivo = max(max_workers, MAX_WORKERS_ABSOLUTE). Mantiene (1,1) se total_pdfs==0.
    """
    if max_workers is None:
        max_workers = int(os.getenv('MAX_WORKERS', '4'))

    max_workers_absolute = int(os.getenv('MAX_WORKERS_ABSOLUTE', '4'))
    scaling_multiplier = float(os.getenv('SCALING_MULTIPLIER', '2.0'))
    pdfs_per_worker = int(os.getenv('PDFS_PER_WORKER', '10'))

    effective_cap = max(max_workers, max_workers_absolute)

    if total_pdfs == 0:
        return 1, 1
    if total_pdfs <= 5:
        return min(2, effective_cap), total_pdfs
    if total_pdfs <= 20:
        optimal = min(max(2, max_workers // 2), effective_cap)
        return optimal, total_pdfs
    if total_pdfs <= 50:
        optimal = min(max_workers, effective_cap)
        batch = max(pdfs_per_worker, max(1, total_pdfs // max(1, optimal)))
        return optimal, batch
    if total_pdfs <= 100:
        optimal = min(max_workers, effective_cap)
        batch = max(15, max(1, total_pdfs // max(1, optimal)))
        return optimal, batch

    calculated_workers = max(1, total_pdfs // max(1, pdfs_per_worker))
    scaled = int(max_workers * scaling_multiplier)
    suggested_workers = min(max(calculated_workers, max_workers), scaled, effective_cap)
    batch_size = max(20, max(1, total_pdfs // max(1, suggested_workers)))
    return suggested_workers, batch_size


def count_pdfs_to_import():
    """Conta il numero di PDF Nota Metodologica da importare"""
    try:
        robust_log("Counting PDFs for document type: nota_metodologica")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("nota_metodologica", sal_name=sal_folder)
        count = len(pdf_paths) if pdf_paths else 0
        robust_log(f"Found {count} PDF files to import")
        return count
    except Exception as e:
        robust_log(f"Error counting PDFs: {e}")
        return 0


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
        
        # Test Azure Storage API
        robust_log("Testing Azure Storage API connection...")
        try:
            import requests
            test_url = f"{api_config['base_search_url']}/unified?search_type=folders&limit=1&root_only=true"
            robust_log(f"Making request to: {test_url}")
            response = requests.get(test_url, timeout=10)
            robust_log(f"API response status code: {response.status_code}")
            
            if response.status_code == 200:
                robust_log("Azure Storage API connection: SUCCESS")
                api_ok = True
            else:
                robust_log(f"Azure Storage API connection: FAILED - HTTP {response.status_code}")
                api_ok = False
        except Exception as e:
            robust_log(f"Azure Storage API connection: FAILED - {e}")
            api_ok = False
        
        # Check Mistral Document AI
        robust_log("Checking Mistral Document AI configuration...")
        mistral_ok = bool(api_config.get('mistral_endpoint')) and bool(api_config.get('mistral_key'))
        if mistral_ok:
            robust_log("Mistral Document AI: CONFIGURED")
        else:
            robust_log("Mistral Document AI: NOT CONFIGURED")
            if not api_config.get('mistral_endpoint'):
                robust_log(" - Missing: MISTRAL_DOC_ENDPOINT")
            if not api_config.get('mistral_key'):
                robust_log(" - Missing: MISTRAL_DOC_KEY")
        
        # Check Azure OpenAI
        robust_log("Checking Azure OpenAI configuration...")
        openai_ok = bool(api_config.get('azure_openai_endpoint')) and bool(api_config.get('azure_openai_key'))
        if openai_ok:
            robust_log(f"Azure OpenAI: CONFIGURED (using {api_config.get('azure_openai_deployment')})")
        else:
            robust_log("Azure OpenAI: NOT CONFIGURED")
            if not api_config.get('azure_openai_endpoint'):
                robust_log(" - Missing: AZURE_OPENAI_ENDPOINT")
            if not api_config.get('azure_openai_key'):
                robust_log(" - Missing: AZURE_OPENAI_KEY")
        
        overall_result = db_ok and api_ok and mistral_ok and openai_ok
        robust_log(f"=== CONNECTION TESTS COMPLETED: {'SUCCESS' if overall_result else 'FAILED'} ===")
        return overall_result
        
    except Exception as e:
        robust_log(f"FATAL ERROR in test_connections: {e}")
        robust_log(f"Error details: {traceback.format_exc()}")
        return False


def dry_run(sal_folder=None):
    """Show what files would be processed without doing anything"""
    robust_log(f"=== STARTING DRY RUN ===")
    if sal_folder:
        robust_log(f"SAL folder: {sal_folder}")
    
    try:
        robust_log("Searching for Nota Metodologica PDFs...")
        pdf_paths = get_pdf_paths_from_sal("nota_metodologica", sal_name=sal_folder)
        robust_log(f"Search completed. Found: {len(pdf_paths) if pdf_paths else 0} files")
        
        if pdf_paths:
            robust_log(f"Found {len(pdf_paths)} Nota Metodologica PDF files:")
            for i, path in enumerate(pdf_paths, 1):
                robust_log(f" {i}. {path}")
            robust_log("=== DRY RUN COMPLETED: SUCCESS ===")
            return True
        else:
            robust_log("No Nota Metodologica PDF files found")
            robust_log("=== DRY RUN COMPLETED: NO FILES ===")
            return False
            
    except Exception as e:
        robust_log(f"ERROR during dry run: {e}")
        robust_log(f"Dry run error details: {traceback.format_exc()}")
        robust_log("=== DRY RUN COMPLETED: FAILED ===")
        return False


def get_api_config():
    """Legge configurazione API da environment variables per Mistral + Azure OpenAI"""
    try:
        robust_log("Loading API configuration...")
        config = {
            "base_search_url": os.getenv('AZURE_SEARCH_URL',
                'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/search'),
            "base_download_url": os.getenv('AZURE_DOWNLOAD_URL',
                'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/download_base64'),
            # Mistral credentials
            "mistral_endpoint": os.getenv('MISTRAL_DOC_ENDPOINT'),
            "mistral_key": os.getenv('MISTRAL_DOC_KEY'),
            # Azure OpenAI credentials
            "azure_openai_endpoint": os.getenv('AZURE_OPENAI_ENDPOINT'),
            "azure_openai_key": os.getenv('AZURE_OPENAI_KEY'),
            "azure_openai_deployment": os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4.1')
        }
        robust_log(f"API config loaded: search_url={config['base_search_url'][:50]}...")

        # Check Mistral credentials
        if config['mistral_endpoint'] and config['mistral_key']:
            robust_log("Mistral Document AI credentials loaded")
        else:
            robust_log("WARNING: Mistral Document AI credentials not found!")

        # Check Azure OpenAI credentials
        if config['azure_openai_endpoint'] and config['azure_openai_key']:
            robust_log(f"Azure OpenAI credentials loaded (deployment: {config['azure_openai_deployment']})")
        else:
            robust_log("WARNING: Azure OpenAI credentials not found!")

        return config
    except Exception as e:
        robust_log(f"ERROR loading API config: {e}")
        raise


def run_import(sal_folder=None, start_index=None, batch_size=None, max_workers=None, auto_scale=True):
    """Run the actual import process using Mistral + Azure OpenAI GPT-4.1 with dynamic scaling"""
    robust_log(f"=== STARTING NOTA METODOLOGICA IMPORT (Mistral + GPT-4.1) v2.0 ===")
    if sal_folder:
        robust_log(f"SAL folder: {sal_folder}")
        # Propaga per ocr_common
        os.environ['SAL_STORAGE_NAME'] = sal_folder
    robust_log(f"Batch parameters: start_index={start_index}, batch_size={batch_size}")
    robust_log(f"Auto-scaling enabled: {auto_scale}")

    try:
        # Get configurations
        robust_log("Loading configurations...")
        db_config = get_db_config()
        api_config = get_api_config()
        robust_log("Configurations loaded successfully")
        
        # Check required credentials for Mistral + Azure OpenAI
        if not api_config.get('mistral_endpoint') or not api_config.get('mistral_key'):
            robust_log("ERROR: Mistral Document AI credentials not found")
            return False
        if not api_config.get('azure_openai_endpoint') or not api_config.get('azure_openai_key'):
            robust_log("ERROR: Azure OpenAI credentials not found")
            return False

        # Search for PDF files
        robust_log("Searching for Nota Metodologica PDF files...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("nota_metodologica", sal_name=sal_folder)
        robust_log(f"Search completed. Found: {len(pdf_paths) if pdf_paths else 0} files")

        if not pdf_paths:
            robust_log("ERROR: No Nota Metodologica PDF files found")
            robust_log("=== IMPORT PROCESS COMPLETED: NO FILES ===")
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

        total_pdfs = len(pdf_urls)
        if total_pdfs == 0:
            robust_log("ERROR: No files in specified batch range")
            robust_log("=== IMPORT PROCESS COMPLETED: EMPTY BATCH ===")
            return False

        # Decide scaling
        if auto_scale:
            robust_log("=" * 60)
            robust_log("DYNAMIC SCALING ANALYSIS")
            robust_log("=" * 60)
            total_to_process = total_pdfs  # after batch selection
            optimal_workers, suggested_batch = calculate_optimal_workers(total_to_process, max_workers)
            robust_log("📊 SCALING DECISION:")
            robust_log(f"   Total PDFs to import: {total_to_process}")
            robust_log(f"   MAX_WORKERS (env): {os.getenv('MAX_WORKERS', '4')}")
            robust_log(f"   MAX_WORKERS_ABSOLUTE (env): {os.getenv('MAX_WORKERS_ABSOLUTE', '4')}")
            robust_log(f"   SCALING_MULTIPLIER (env): {os.getenv('SCALING_MULTIPLIER', '2.0')}")
            robust_log(f"   PDFS_PER_WORKER (env): {os.getenv('PDFS_PER_WORKER', '10')}")
            robust_log("   ---")
            if max_workers is None:
                max_workers = optimal_workers
            robust_log(f"   ✅ Using workers: {max_workers}")
            if batch_size is None and total_to_process > 20:
                batch_size = suggested_batch
                robust_log(f"   ✅ Suggested batch size: {batch_size}")
            robust_log("=" * 60)
        else:
            if max_workers is None:
                max_workers = int(os.getenv('MAX_WORKERS', '4'))
            robust_log(f"Auto-scaling disabled. Using fixed workers: {max_workers}")

        # Show which files will be processed (first few)
        robust_log("Files to be processed (first 5):")
        for i, url in enumerate(pdf_urls[:5], 1):
            file_index = (start_index or 0) + i - 1
            robust_log(f" [{file_index}] {url.split('/')[-1] if '/' in url else url}")
        if total_pdfs > 5:
            robust_log(f" ... and {total_pdfs - 5} more files")

        # Process PDFs in parallel
        robust_log(f"Starting PDF processing with {max_workers} parallel workers (Mistral + Azure OpenAI)...")
        all_queries = []
        completed = 0
        total = total_pdfs

        def process_one(pdf_url):
            try:
                sql_script = nota_ocr.process_single_nota_metodologica_pdf(pdf_url)
                return (pdf_url, sql_script, None)
            except Exception as exc:
                return (pdf_url, None, exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {executor.submit(process_one, url): url for url in pdf_urls}
            for future in concurrent.futures.as_completed(future_to_url):
                url = future_to_url[future]
                completed += 1
                try:
                    url_done, sql_script, err = future.result()
                    file_name = url_done.split('/')[-1] if '/' in url_done else url_done
                    if err is None and sql_script:
                        all_queries.append(sql_script)
                        robust_log(f"[{completed}/{total}] ✅ {file_name} processed - SQL generated")
                    elif err is None:
                        robust_log(f"[{completed}/{total}] ⚠️ {file_name} skipped (no valid content)")
                    else:
                        robust_log(f"[{completed}/{total}] ❌ {file_name} failed: {err}")
                        robust_log(f"Error details: {traceback.format_exc()}")
                except Exception as e:
                    robust_log(f"[{completed}/{total}] ❌ Unexpected future error: {e}")
                # Progress
                percentage = (completed / total) * 100
                robust_log(f"Progress: {percentage:.1f}% ({completed}/{total})")

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
            robust_log("\nVerify results with:")
            robust_log(" SELECT COUNT(*) FROM nota_metodologica_pdf;")
            robust_log(" SELECT * FROM nota_metodologica_pdf ORDER BY id DESC LIMIT 10;")
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
    robust_log("NOTA METODOLOGICA OCR PROCESSOR - WRAPPER")
    robust_log("Using: Mistral Document AI + Azure OpenAI GPT-4.1")
    robust_log("=" * 60)
    
    try:
        # Show environment info
        robust_log(f"Python version: {sys.version}")
        robust_log(f"Working directory: {os.getcwd()}")
        robust_log(f"Script path: {os.path.abspath(__file__)}")
        
        # Parse arguments
        robust_log("Parsing command line arguments...")
        parser = argparse.ArgumentParser(description='Nota Metodologica OCR Processor - Mistral + GPT-4.1')
        parser.add_argument('--mode', choices=['test', 'dry-run', 'import'], 
                           default='import', help='Operation mode')
        parser.add_argument('--sal-folder', help='SAL folder name (optional)')
        parser.add_argument('--start-index', type=int, help='Start index for batch processing')
        parser.add_argument('--batch-size', type=int, help='Batch size for multi-container processing')

        args = parser.parse_args()
        robust_log("Arguments parsed successfully")
        
        # Show configuration
        robust_log("Configuration:")
        robust_log(f" Mode: {args.mode}")
        robust_log(f" AI Models: Mistral Document AI + Azure OpenAI GPT-4.1")
        if args.sal_folder:
            robust_log(f" SAL Folder: {args.sal_folder}")
        robust_log(f" Start Index: {args.start_index}")
        robust_log(f" Batch Size: {args.batch_size}")
        robust_log(f" Timestamp: {datetime.now()}")
        robust_log("-" * 60)
        
        # Execute based on mode
        robust_log(f"Starting execution in {args.mode} mode...")
        
        if args.mode == 'test':
            robust_log("Running connection tests...")
            success = test_connections()
            robust_log(f"Connection tests completed with result: {success}")
            
        elif args.mode == 'dry-run':
            robust_log("Running dry-run...")
            success = dry_run(args.sal_folder)
            robust_log(f"Dry-run completed with result: {success}")
            
        elif args.mode == 'import':
            robust_log("Running import process...")
            success = run_import(args.sal_folder, args.start_index, args.batch_size)
            robust_log(f"Import process completed with result: {success}")
        
        # Final results
        robust_log("-" * 60)
        robust_log(f"FINAL RESULT: {'SUCCESS' if success else 'FAILED'}")
        robust_log("=" * 60)
        
        # Force output and wait
        sys.stdout.flush()
        sys.stderr.flush()
        robust_log("Waiting 5 seconds to ensure logs are captured...")
        time.sleep(5)
        robust_log("Process completed. Exiting.")
        
        return success
        
    except Exception as e:
        robust_log("=" * 60)
        robust_log(f"FATAL ERROR in main function: {e}")
        robust_log(f"FATAL ERROR TRACEBACK: {traceback.format_exc()}")
        robust_log("=" * 60)
        sys.stdout.flush()
        sys.stderr.flush()
        time.sleep(5)
        return False


if __name__ == "__main__":
    try:
        robust_log("Starting Nota Metodologica OCR Processor (Mistral + GPT-4.1)...")
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