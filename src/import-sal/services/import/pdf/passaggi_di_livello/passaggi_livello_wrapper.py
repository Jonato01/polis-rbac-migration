#!/usr/bin/env python3
"""
Wrapper for passaggi_livello import with INLINE matching
v3.2 - Match BEFORE insert (all-in-one)
"""

from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import argparse
import sys
import os
import re
import json

# Path setup
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

import concurrent.futures
from datetime import datetime
import traceback
import mysql.connector

from ocr_common import (
    execute_batch_queries, 
    get_db_config, 
    get_pdf_paths_from_sal,
    pdf_to_json_via_gpt_vision,
    fetch_mysql_query
)



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
        return config
    except Exception as e:
        robust_log(f"ERROR loading API config: {e}")
        raise


# ============================================================================
# LOGGING
# ============================================================================

_log_file = None

def setup_logging(log_filename=None):
    """Setup logging to file and console"""
    global _log_file
    
    if log_filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"passaggi_livello_{timestamp}.log"
    
    try:
        _log_file = open(log_filename, 'a', encoding='utf-8', buffering=1)
        robust_log(f"Logging: {log_filename}")
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


# ============================================================================
# DYNAMIC SCALING FUNCTIONS
# ============================================================================

def count_pdfs_to_import():
    """Count total number of PDFs to import from sal storage"""
    try:
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("lettere_passaggi", sal_name=sal_folder)
        return len(pdf_paths) if pdf_paths else 0
    except Exception as e:
        robust_log(f"ERROR counting PDFs: {e}")
        return 0


def calculate_optimal_workers(total_pdfs, base_workers=4):
    """
    Calculate optimal number of workers based on PDF count using effective cap (max(base_workers, MAX_WORKERS_ABSOLUTE)).
    Returns (optimal_workers, suggested_batch_size). Preserves (1,1) when total_pdfs == 0.
    """
    try:
        MAX_WORKERS_ABSOLUTE = int(os.getenv('MAX_WORKERS_ABSOLUTE', '4'))
        SCALING_MULTIPLIER = float(os.getenv('SCALING_MULTIPLIER', '2.0'))
        PDFS_PER_WORKER = int(os.getenv('PDFS_PER_WORKER', '10'))
        effective_cap = max(base_workers, MAX_WORKERS_ABSOLUTE)

        if total_pdfs == 0:
            return 1, 1
        if total_pdfs <= 10:
            optimal_workers = min(total_pdfs, effective_cap, max(1, base_workers))
            suggested_batch = total_pdfs
        elif total_pdfs <= 50:
            raw = int(total_pdfs / PDFS_PER_WORKER)
            optimal_workers = min(max(raw, base_workers), effective_cap)
            suggested_batch = max(int(total_pdfs / 2), 5)
        elif total_pdfs <= 100:
            raw = int(total_pdfs / PDFS_PER_WORKER * SCALING_MULTIPLIER)
            optimal_workers = min(max(raw, base_workers), effective_cap)
            suggested_batch = max(int(total_pdfs / 3), 10)
        else:
            raw = int(total_pdfs / PDFS_PER_WORKER * SCALING_MULTIPLIER)
            optimal_workers = min(max(raw, base_workers), effective_cap)
            suggested_batch = max(int(total_pdfs / max(1, optimal_workers)), 10)

        optimal_workers = max(1, min(optimal_workers, effective_cap))
        suggested_batch = max(1, suggested_batch)
        return optimal_workers, suggested_batch
    except Exception as e:
        robust_log(f"ERROR calculating optimal workers: {e}")
        return base_workers, max(1, total_pdfs)


# ============================================================================
# IMPORT MODULE
# ============================================================================

def safe_import():
    """Import passaggi_livello module safely"""
    try:
        robust_log("Importing passaggi_livello v3...")
        import passaggi_livello
        robust_log("Module v3 imported")
        return passaggi_livello
    except ImportError:
        robust_log("ERROR: passaggi_livello not found")
        sys.exit(1)
    except Exception as e:
        robust_log(f"FATAL: Failed to import: {e}")
        sys.exit(1)

passaggi_livello = safe_import()


# ============================================================================
# PROCESS SINGLE PDF WITH INLINE MATCHING
# ============================================================================

def process_single_pdf_with_matching(pdf_url, text_input, json_to_queries_func, dipendenti_cache, qualifiche_cache):
    """
    Process PDF + match BEFORE insert
    
    Returns complete INSERT with cod_dipendente and cod_qualifica already filled
    """
    try:
        from match_passaggi_dipendenti import smart_match_dipendente
        
        # STEP 1: OCR
        json_result, raw_text = pdf_to_json_via_gpt_vision(
            pdf_url, None, text_input, return_raw_text=True
        )

        if not json_result or json_result.startswith("Error:"):
            return ('ocr_failed', pdf_url)
        
        # STEP 2: Generate base query (without cod_dipendente/cod_qualifica)
        success_queries, error_query = json_to_queries_func(json_result, pdf_url, raw_text)
        
        if error_query:
            return ('error', error_query)
        
        if not success_queries:
            return ('skip', None)
        
        # STEP 3: Parse JSON to extract nome_cognome and livello
        clean_json = json_result.replace("```json", "").replace("```", "").strip()
        data = json.loads(clean_json)
        
        dipendente = data.get("dipendente", {})
        passaggio = data.get("passaggio", {})
        
        nome = dipendente.get('nome')
        cognome = dipendente.get('cognome')
        nome_cognome = f"{nome} {cognome}" if nome and cognome else None
        livello_nuovo = passaggio.get('livello_nuovo')
        
        # STEP 4: Match cod_dipendente (IN MEMORY)
        cod_dipendente = None
        if nome_cognome and dipendenti_cache:
            matched_dip = smart_match_dipendente(nome_cognome, dipendenti_cache)
            if matched_dip:
                cod_dipendente = matched_dip.get('cod_dipendente')
        
        # STEP 5: Match cod_qualifica (IN MEMORY)
        cod_qualifica = None
        if livello_nuovo and qualifiche_cache:
            for q in qualifiche_cache:
                if q.get('des_qualifica') == livello_nuovo:
                    cod_qualifica = q.get('cod_qualifica')
                    break
        
        # STEP 6: Modify query to include matched fields
        base_query = success_queries[0] if isinstance(success_queries, list) else success_queries
        
        if cod_dipendente or cod_qualifica:
            # Find position after "nome_cognome," in INSERT
            insert_pos = base_query.find("nome_cognome,")
            if insert_pos > 0:
                # Add fields to column list
                new_fields = ""
                if cod_dipendente:
                    new_fields += "\n cod_dipendente,"
                if cod_qualifica:
                    new_fields += "\n cod_qualifica_nuovo,"
                
                base_query = base_query.replace(
                    "nome_cognome,",
                    f"nome_cognome,{new_fields}",
                    1
                )
                
                # Add values to SELECT
                nome_cognome_safe = nome_cognome.replace("'", "''")
                select_pos = base_query.find(f"'{nome_cognome_safe}' as nome_cognome,")
                if select_pos > 0:
                    new_values = ""
                    if cod_dipendente:
                        new_values += f"\n {cod_dipendente} as cod_dipendente,"
                    if cod_qualifica:
                        new_values += f"\n {cod_qualifica} as cod_qualifica_nuovo,"
                    
                    base_query = base_query.replace(
                        f"'{nome_cognome_safe}' as nome_cognome,",
                        f"'{nome_cognome_safe}' as nome_cognome,{new_values}",
                        1
                    )
        
        return ('success', base_query)
            
    except Exception as e:
        print(f"ERROR in process_single_pdf_with_matching: {e}")
        traceback.print_exc()
        return ('exception', (pdf_url, str(e)))


# ============================================================================
# IMPORT PROCESS
# ============================================================================

def run_import(limit=None, start_index=None, batch_size=None, max_workers=4):
    """Run import with inline matching"""
    
    robust_log(f"=== PASSAGGI LIVELLO IMPORT v3.2 (INLINE MATCHING) ===")
    robust_log(f"Params: limit={limit}, start={start_index}, batch={batch_size}, workers={max_workers}")
    
    try:
        db_config = get_db_config()
        base_download_url = os.getenv('AZURE_DOWNLOAD_URL',
            'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/download_base64')

        # ========== LOAD CACHES ==========
        robust_log("Loading dipendenti cache...")
        dipendenti_cache = fetch_mysql_query(
            "SELECT cod_dipendente, nome, cognome FROM dipendenti",
            db_config
        )
        robust_log(f"Loaded {len(dipendenti_cache)} dipendenti")
        
        robust_log("Loading qualifiche cache...")
        qualifiche_cache = fetch_mysql_query(
            "SELECT cod_qualifica, des_qualifica FROM qualifica",
            db_config
        )
        robust_log(f"Loaded {len(qualifiche_cache)} qualifiche\n")

        robust_log("Searching PDFs...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("lettere_passaggi", sal_name=sal_folder)
        robust_log(f"Found: {len(pdf_paths) if pdf_paths else 0} files")

        if not pdf_paths:
            robust_log("ERROR: No PDF files found")
            return False

        pdf_urls = [f"{base_download_url}/{path}" for path in pdf_paths]
        total_available = len(pdf_urls)

        # Apply batch parameters
        if start_index is not None and batch_size is not None:
            end_index = min(start_index + batch_size, total_available)
            pdf_urls = pdf_urls[start_index:end_index]
            robust_log(f"Batch: files {start_index}-{end_index-1}")
        elif limit and limit > 0:
            pdf_urls = pdf_urls[:limit]
            robust_log(f"Limit: {len(pdf_urls)} files")

        total_pdfs = len(pdf_urls)
        
        if total_pdfs == 0:
            robust_log("ERROR: No files in specified range")
            return False

        # Show files
        robust_log("Files to process:")
        for i, url in enumerate(pdf_urls, 1):
            if i <= 5:
                filename = url.split('/')[-1] if '/' in url else url
                robust_log(f" [{i}] {filename}")
            elif i == 6 and total_pdfs > 5:
                robust_log(f" ... and {total_pdfs - 5} more")

        text_input = passaggi_livello.get_passaggi_livello_prompt()

        # Statistics
        total_inserted = 0
        total_matched = 0
        total_errors_logged = 0
        completed = 0
        
        max_workers = min(max_workers, total_pdfs)

        # ========== PROCESS WITH INLINE MATCHING ==========
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_url = {
                executor.submit(
                    process_single_pdf_with_matching, 
                    url, 
                    text_input, 
                    passaggi_livello.json_to_passaggi_queries,
                    dipendenti_cache,
                    qualifiche_cache
                ): url
                for url in pdf_urls
            }

            for future in concurrent.futures.as_completed(future_to_url):
                completed += 1
                url = future_to_url[future]
                
                try:
                    filename = url.split('/')[-1] if '/' in url else url
                    robust_log(f"[{completed}/{total_pdfs}] {filename}")
                    
                    result_type, result_data = future.result()
                    
                    if result_type == 'success':
                        # Check if has cod_dipendente (means matched)
                        has_match = 'cod_dipendente' in result_data and result_data.count('cod_dipendente') > 1
                        
                        success = execute_batch_queries([result_data], db_config)
                        if success:
                            total_inserted += 1
                            if has_match:
                                total_matched += 1
                                robust_log(f"SAVED+MATCHED (total: {total_inserted}, matched: {total_matched})")
                            else:
                                robust_log(f"SAVED (total: {total_inserted})")
                        else:
                            robust_log(f"DB SAVE FAILED")
                            
                    elif result_type == 'error':
                        success = execute_batch_queries([result_data], db_config)
                        if success:
                            total_errors_logged += 1
                            robust_log(f"ERROR LOGGED (total: {total_errors_logged})")
                        else:
                            robust_log(f"ERROR LOG FAILED")
                    
                    elif result_type == 'ocr_failed':
                        robust_log(f"OCR FAILED")
                        
                    elif result_type == 'exception':
                        pdf_url, error_msg = result_data
                        robust_log(f"EXCEPTION: {error_msg}")
                    
                    else:
                        robust_log(f"SKIPPED")
                    
                except Exception as exc:
                    robust_log(f"[{completed}/{total_pdfs}] PROCESSING ERROR: {exc}")
                
                percentage = (completed / total_pdfs) * 100
                robust_log(f"Progress: {percentage:.1f}% | Success: {total_inserted} | Matched: {total_matched} | Errors: {total_errors_logged}")

        robust_log(f"\n{'='*60}")
        robust_log(f"IMPORT COMPLETED")
        robust_log(f"{'='*60}")
        robust_log(f"Total processed: {completed}")
        robust_log(f"Imported: {total_inserted}")
        robust_log(f"Matched: {total_matched}")
        robust_log(f"Errors: {total_errors_logged}")
        if total_inserted > 0:
            match_rate = (total_matched / total_inserted) * 100
            robust_log(f"Match rate: {match_rate:.1f}%")
        robust_log(f"{'='*60}")
        
        return True

    except Exception as e:
        robust_log(f"FATAL ERROR: {e}")
        robust_log(f"Traceback: {traceback.format_exc()}")
        return False


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
            conn = mysql.connector.connect(**db_config)
            cursor = conn.cursor()
            cursor.execute("SELECT 1 as test")
            result = cursor.fetchone()
            cursor.close()
            conn.close()
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


def dry_run():
    """Show what files would be processed without doing anything"""
    robust_log("=== STARTING DRY RUN ===")
    
    try:
        robust_log("Searching for PASSAGGI DI LIVELLO PDF files...")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("lettere_passaggi", sal_name=sal_folder)

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
# MAIN
# ============================================================================

def main():
    """Main function"""
    
    setup_logging()
    
    robust_log("=" * 60)
    robust_log("PASSAGGI LIVELLO WRAPPER v3.2 (INLINE MATCHING)")
    robust_log("=" * 60)
    
    try:
        parser = argparse.ArgumentParser(description='Passaggi Livello Wrapper v3.2')
        
        parser.add_argument('--mode', choices=['test', 'dry-run', 'import'], 
                           default='import', help='Operation mode')
        parser.add_argument('--limit', type=int, help='Limit number of PDF files')
        parser.add_argument('--start-index', type=int, help='Start index for batch')
        parser.add_argument('--batch-size', type=int, help='Batch size')
        parser.add_argument('--max-workers', type=int, default=4, help='Parallel workers')

        args = parser.parse_args()
        
        robust_log(f"Mode: {args.mode}")
        robust_log(f"Max Workers: {args.max_workers}")
        robust_log(f"Limit: {args.limit}")
        robust_log("-" * 60)
        
        # Execute based on mode
        if args.mode == 'test':
            robust_log("Calling test_connections...")
            success = test_connections()
            robust_log(f"test_connections completed: {success}")
            
        elif args.mode == 'dry-run':
            robust_log("Calling dry_run...")
            success = dry_run()
            robust_log(f"dry_run completed: {success}")
            
        elif args.mode == 'import':
            robust_log("Calling run_import...")
            success = run_import(args.limit, args.start_index, args.batch_size, args.max_workers)
            robust_log(f"run_import completed: {success}")
        
        robust_log("-" * 60)
        robust_log(f"RESULT: {'SUCCESS' if success else 'FAILED'}")
        robust_log("=" * 60)
        
        return success
        
    except Exception as e:
        robust_log("=" * 60)
        robust_log(f"FATAL ERROR: {e}")
        robust_log(f"TRACEBACK: {traceback.format_exc()}")
        robust_log("=" * 60)
        return False
    finally:
        close_logging()


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except Exception as final_error:
        print(f"ULTIMATE FATAL ERROR: {final_error}")
        traceback.print_exc()
        sys.exit(1)