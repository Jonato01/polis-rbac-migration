#!/usr/bin/env python3
"""
Enhanced wrapper for BDC OCR v3.1 with retry logic and error logging
Supporta P7M + Mistral + Azure OpenAI + parallelizzazione + Addendum CUP Fixer
"""

import argparse
import os
import sys


# Path setup - AGGIUNGERE QUESTO
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
    
import concurrent.futures
from datetime import datetime
import traceback
import time
from ocr_common import get_pdf_paths_from_sal, execute_batch_queries, fetch_mysql_query


def robust_log(message, force_flush=True):
    """Log con timestamp e flush"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{timestamp}] {message}"
    print(log_msg)
    if force_flush:
        sys.stdout.flush()
        sys.stderr.flush()


def log_error_to_file(filename, error_type, error_message, error_log_path):
    """Salva errore nel file di log errori"""
    try:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(error_log_path, 'a', encoding='utf-8') as f:
            f.write(f"{timestamp}|{filename}|{error_type}|{error_message}\n")
    except Exception as e:
        robust_log(f" Impossibile scrivere nel file errori: {e}")


def create_error_log_file():
    """Crea file di log errori con header"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    error_log_path = f"errors_bdc_{timestamp}.txt"
    
    with open(error_log_path, 'w', encoding='utf-8') as f:
        f.write("TIMESTAMP|FILENAME|ERROR_TYPE|ERROR_MESSAGE\n")
        f.write("="*100 + "\n")
    
    robust_log(f" File errori creato: {error_log_path}")
    return error_log_path


def safe_import():
    """Import modulo bdc_ocr in modo sicuro"""
    try:
        robust_log("Import modulo bdc_ocr...")
        import bdc_ocr
        robust_log(" bdc_ocr importato")
        return bdc_ocr
    except Exception as e:
        robust_log(f" FATAL: Import bdc_ocr fallito: {e}")
        robust_log(f"Traceback: {traceback.format_exc()}")
        sys.exit(1)


# Import globale
bdc_ocr = safe_import()


# ============================================================================
# DYNAMIC SCALING FUNCTIONS
# ============================================================================

def calculate_optimal_workers(total_pdfs, max_workers=None):
    """
    Calcola il numero ottimale di worker in base al numero di PDF/P7M da processare
    Limite effettivo = max(max_workers, MAX_WORKERS_ABSOLUTE). Mantiene (1,1) se total_pdfs==0.
    """
    if max_workers is None:
        max_workers = 4

    max_workers_absolute = int(os.getenv('MAX_WORKERS_ABSOLUTE', '4'))
    scaling_multiplier = float(os.getenv('SCALING_MULTIPLIER', '1.5'))
    pdfs_per_worker = int(os.getenv('PDFS_PER_WORKER', '20'))

    effective_cap = 4

    if total_pdfs == 0:
        return 1, 1
    if total_pdfs <= 5:
        return 1, total_pdfs
    if total_pdfs <= 20:
        optimal = min(2, effective_cap)
        return optimal, total_pdfs
    if total_pdfs <= 50:
        optimal = min(max_workers, effective_cap)
        batch = max(pdfs_per_worker, total_pdfs // max(1, optimal))
        return optimal, batch
    if total_pdfs <= 100:
        optimal = min(max_workers, effective_cap)
        batch = max(20, total_pdfs // max(1, optimal))
        return optimal, batch

    calculated_workers = max(1, total_pdfs // max(1, pdfs_per_worker))
    scaled = int(max_workers * scaling_multiplier)
    suggested_workers = min(max(calculated_workers, max_workers), scaled, effective_cap)
    batch_size = max(25, total_pdfs // max(1, suggested_workers))
    return suggested_workers, batch_size


def count_pdfs_to_import(folder="bdc", include_p7m=True):
    """
    Conta il numero di file BDC (PDF + P7M) da importare

    Args:
        folder: Nome della cartella (default: "bdc")
        include_p7m: Include file P7M (default: True)

    Returns:
        int: Numero di file trovati
    """
    try:
        robust_log(f"Counting files in folder: {folder} (include_p7m={include_p7m})")
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal(folder, include_p7m=include_p7m, sal_name=sal_folder)

        if pdf_paths:
            count = len(pdf_paths)
            robust_log(f"Found {count} files to import")
            return count
        else:
            robust_log("No files found")
            return 0
    except Exception as e:
        robust_log(f"Error counting files: {e}")
        return 0


def get_db_config():
    """Configurazione database da environment"""
    try:
        robust_log("Caricamento config database...")
        config = {
            "host": os.getenv('DB_HOST', 'colibri-mysql-flexible.mysql.database.azure.com'),
            "port": int(os.getenv('DB_PORT', '3306')),
            "user": os.getenv('DB_USER', 'polisammin'),
            "password": os.getenv('DB_PASSWORD', 'ComplexPass123!'),
            "database": os.getenv('DB_NAME', 'polis'),
            "pool_size": int(os.getenv('DB_POOL_SIZE', '10')),
            "pool_reset_session": os.getenv('DB_POOL_RESET', 'True').lower() == 'true'
        }
        robust_log(f"Config DB: host={config['host']}, db={config['database']}")
        return config
    except Exception as e:
        robust_log(f" FATAL: Config DB fallita: {e}")
        sys.exit(1)


def get_api_config():
    """Configurazione API download"""
    try:
        robust_log("Caricamento config API...")
        download_url = os.getenv('AZURE_DOWNLOAD_URL')
        if not download_url:
            raise ValueError("AZURE_DOWNLOAD_URL non configurato")
        robust_log(f"Config API: download_url={download_url[:60]}...")
        return {"download_url": download_url}
    except Exception as e:
        robust_log(f" FATAL: Config API fallita: {e}")
        sys.exit(1)


def download_pdf_with_retry(full_url, max_retries=3):
    """Download PDF con retry su errori di rete"""
    from ocr_common import fetch_pdf_base64_cached, fetch_pdf_base64
    import requests
    
    for attempt in range(1, max_retries + 1):
        try:
            if attempt == 1:
                print(f" Download file... (tentativo {attempt}/{max_retries})")
            else:
                print(f" Retry download... (tentativo {attempt}/{max_retries})")
            
            # Prova con cache
            file_base64 = fetch_pdf_base64_cached(full_url)
            if file_base64:
                return file_base64
            
            # Prova download diretto
            file_base64 = fetch_pdf_base64(full_url)
            if file_base64:
                return file_base64
                
            # Nessun risultato
            if attempt < max_retries:
                time.sleep(2 * attempt) # Backoff esponenziale
                
        except (requests.exceptions.ConnectionError, 
                requests.exceptions.Timeout,
                Exception) as e:
            if attempt < max_retries:
                print(f" Errore download (tentativo {attempt}): {e}")
                time.sleep(2 * attempt)
            else:
                print(f" Download fallito dopo {max_retries} tentativi: {e}")
                raise
    
    return None


def process_file_task(file_info, error_log_path):
    """
    Processa singolo file BDC con retry logic e error logging
    
    Returns:
        dict con 'success', 'filename', 'sql', 'error'
    """
    file_url, base_download_url = file_info
    filename = file_url.split('/')[-1]
    full_url = f"{base_download_url}/{file_url}"
    
    result = {
        'success': False,
        'filename': filename,
        'sql': None,
        'error': None
    }
    
    pdf_path = None
    is_temp = False
    
    try:
        print(f"\n{'='*60}")
        print(f" File: {filename}")
        print('='*60)
        
        # STEP 1: Download con retry
        try:
            file_base64 = download_pdf_with_retry(full_url, max_retries=3)
            if not file_base64:
                error_msg = "Download fallito dopo 3 tentativi"
                log_error_to_file(filename, "DOWNLOAD_ERROR", error_msg, error_log_path)
                result['error'] = error_msg
                return result
        except Exception as e:
            error_msg = f"Errore download: {str(e)}"
            log_error_to_file(filename, "DOWNLOAD_ERROR", error_msg, error_log_path)
            result['error'] = error_msg
            return result
        
        # Salva temporaneamente
        import tempfile
        import base64
        
        suffix = '.p7m' if filename.lower().endswith('.p7m') else '.pdf'
        temp_fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix='bdc_')
        
        with os.fdopen(temp_fd, 'wb') as f:
            f.write(base64.b64decode(file_base64))
        
        print(f" File salvato: {os.path.basename(temp_path)}")
        
        # STEP 2: Gestione P7M (se necessario)
        if filename.lower().endswith('.p7m'):
            from bdc_p7m_handler import process_bdc_file, cleanup_temp_file
            print("\n Estrazione da P7M...")
            try:
                pdf_path, is_temp = process_bdc_file(temp_path)
                cleanup_temp_file(temp_path)
            except Exception as e:
                error_msg = f"Errore estrazione P7M: {str(e)}"
                log_error_to_file(filename, "P7M_ERROR", error_msg, error_log_path)
                result['error'] = error_msg
                cleanup_temp_file(temp_path)
                return result
        else:
            pdf_path = temp_path
            is_temp = True
        
        # STEP 3: Processa PDF con Mistral + Azure (con retry Mistral integrato)
        print(f"\n{'='*60}")
        print(f"Processamento: {os.path.basename(pdf_path)}")
        print('='*60)
        
        from bdc_processor import process_single_bdc_pdf
        
        try:
            sql = process_single_bdc_pdf(pdf_path)
            
            if not sql:
                error_msg = "Processamento fallito - nessuna query SQL generata"
                log_error_to_file(filename, "PROCESSING_ERROR", error_msg, error_log_path)
                result['error'] = error_msg
                return result
            
            result['sql'] = sql
            result['success'] = True
            print(" Processamento completato")
            
        except Exception as e:
            error_msg = f"Errore processamento: {str(e)}"
            log_error_to_file(filename, "PROCESSING_ERROR", error_msg, error_log_path)
            result['error'] = error_msg
            return result
            
    except Exception as e:
        error_msg = f"Errore generico: {str(e)}"
        log_error_to_file(filename, "GENERIC_ERROR", error_msg, error_log_path)
        result['error'] = error_msg
        
    finally:
        # Cleanup file temporanei
        if is_temp and pdf_path:
            from bdc_p7m_handler import cleanup_temp_file
            cleanup_temp_file(pdf_path)
            print("File temporaneo eliminato")
    
    return result


def run_import(sal_folder=None,
               duplicate_handling='skip',
               limit=None,
               start_index=None,
               batch_size=None,
               max_workers=1):
    """
    Entry point per Container App service
    Wrapper compatibile con bdc_service.py
    """
    # sal_folder viene ignorato per ora - usa la config globale
    return import_bdc(
        duplicate_handling=duplicate_handling,
        limit=limit,
        start_index=start_index,
        batch_size=batch_size,
        max_workers=max_workers
    )


def import_bdc(duplicate_handling='skip',
               limit=None,
               start_index=None,
               batch_size=None,
               max_workers=1,
               auto_scale=True):
    """Importa BDC con parallelizzazione, error logging, Addendum CUP Fixer e Dynamic Scaling"""

    robust_log('='*60)
    robust_log(f"IMPORT BDC v4.0 - Dynamic Scaling + Addendum CUP Fixer")
    robust_log('='*60)
    robust_log(f"Duplicate handling: {duplicate_handling}")
    robust_log(f"Auto-scaling enabled: {auto_scale}")

    # FASE 0: DYNAMIC SCALING ANALYSIS
    if auto_scale:
        robust_log("=" * 60)
        robust_log("DYNAMIC SCALING ANALYSIS")
        robust_log("=" * 60)

        total_file_count = count_pdfs_to_import('bdc', include_p7m=True)

        if total_file_count == 0:
            robust_log("No files to import. Exiting.")
            return True

        # Calcola worker e batch size ottimali
        optimal_workers, suggested_batch = calculate_optimal_workers(total_file_count, max_workers)

        robust_log(f"📊 SCALING DECISION:")
        robust_log(f"   Total files to import: {total_file_count}")
        robust_log(f"   MAX_WORKERS (env): {os.getenv('MAX_WORKERS', '1')}")
        robust_log(f"   MAX_WORKERS_ABSOLUTE (env): {os.getenv('MAX_WORKERS_ABSOLUTE', '4')}")
        robust_log(f"   SCALING_MULTIPLIER (env): {os.getenv('SCALING_MULTIPLIER', '1.5')}")
        robust_log(f"   PDFS_PER_WORKER (env): {os.getenv('PDFS_PER_WORKER', '20')}")
        robust_log(f"   ---")
        robust_log(f"   ✅ Calculated optimal workers: {optimal_workers}")
        robust_log(f"   ✅ Suggested batch size: {suggested_batch}")

        # Usa i valori calcolati se non specificati
        if max_workers is None or max_workers == 1:
            max_workers = optimal_workers
            robust_log(f"   ✅ Using calculated max_workers: {max_workers}")

        if batch_size is None and total_file_count > 20:
            batch_size = suggested_batch
            robust_log(f"   ✅ Using calculated batch_size: {batch_size}")

        robust_log("=" * 60)
    else:
        # Modalità legacy
        if max_workers is None:
            max_workers = int(os.getenv('MAX_WORKERS', '1'))
        robust_log(f"Auto-scaling disabled. Using fixed max_workers: {max_workers}")

    robust_log(f"Final parameters: limit={limit}, start_index={start_index}, batch_size={batch_size}")
    robust_log(f"Max workers: {max_workers}")
    
    # Config
    db_config = get_db_config()
    api_config = get_api_config()
    base_url = api_config['download_url']
    
    # Crea file errori
    error_log_path = create_error_log_file()
    
    # Trova file
    robust_log("\n Ricerca file BDC (PDF + P7M)...")
    sal_folder = os.getenv('SAL_STORAGE_NAME')
    paths = get_pdf_paths_from_sal('bdc', include_p7m=True, sal_name=sal_folder)
    robust_log(f" Trovati {len(paths)} file")
    
    # Applica slice per batch
    if start_index is not None and batch_size is not None:
        end_index = min(start_index + batch_size, len(paths))
        paths = paths[start_index:end_index]
        robust_log(f" Batch: file {start_index}-{end_index-1} ({len(paths)} file)")
    elif limit:
        paths = paths[:limit]
        robust_log(f" Limit: primi {limit} file")
    
    if not paths:
        robust_log(" Nessun file da processare")
        return False
    
    # Mostra preview
    robust_log("\nFile da processare:")
    for i, path in enumerate(paths[:5], 1):
        robust_log(f" {i}. {path.split('/')[-1]}")
    if len(paths) > 5:
        robust_log(f" ... e altri {len(paths)-5} file")
    
    robust_log("\n" + "="*60)
    robust_log("FASE 1: IMPORT BDC STANDARD")
    robust_log("="*60)
    robust_log(f"Workers paralleli: {max_workers}\n")
    
    # Prepara task
    file_tasks = [(path, base_url) for path in paths]
    
    success_count = 0
    skip_count = 0
    error_count = 0
    
    # Esegui in parallelo
    robust_log(f"Task inviati: {len(file_tasks)}")
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit tutti i task con error_log_path
        future_to_file = {
            executor.submit(process_file_task, task, error_log_path): task[0] 
            for task in file_tasks
        }
        
        for i, future in enumerate(concurrent.futures.as_completed(future_to_file), 1):
            file_path = future_to_file[future]
            filename = file_path.split('/')[-1]
            
            try:
                result = future.result()
                
                if result['success'] and result['sql']:
                    # Esegui SQL immediatamente
                    try:
                        # execute_batch_queries si aspetta una lista di query
                        success = execute_batch_queries([result['sql']], db_config)
                        if success:
                            success_count += 1
                            robust_log(f" [{i}/{len(paths)}] {filename}")
                        else:
                            error_count += 1
                            error_msg = "Errore esecuzione SQL"
                            log_error_to_file(filename, "SQL_ERROR", error_msg, error_log_path)
                            robust_log(f" [{i}/{len(paths)}] {filename} - SQL error")
                    except Exception as e:
                        error_count += 1
                        error_msg = f"Eccezione SQL: {str(e)}"
                        log_error_to_file(filename, "SQL_EXCEPTION", error_msg, error_log_path)
                        robust_log(f" [{i}/{len(paths)}] {filename} - {error_msg}")
                else:
                    skip_count += 1
                    robust_log(f" [{i}/{len(paths)}] {filename} - skipped")
                
                # Progress ogni 50 file
                if i % 50 == 0:
                    progress = (i / len(paths)) * 100
                    robust_log(f" Progress: {progress:.1f}% ({i}/{len(paths)})")
                    
            except Exception as e:
                error_count += 1
                error_msg = f"Eccezione task: {str(e)}"
                log_error_to_file(filename, "TASK_EXCEPTION", error_msg, error_log_path)
                robust_log(f" [{i}/{len(paths)}] {filename} - {error_msg}")
    
    # FASE 2: ADDENDUM CUP FIXER (AUTOMATICO)
    robust_log("\n" + "="*60)
    robust_log("FASE 2: ADDENDUM CUP FIXER (AUTOMATICO)")
    robust_log("="*60)
    robust_log(" Ricerca CUP mancanti in addendum...\n")
    
    from bdc_addendum_cup_fixer import fix_missing_cups_from_addendum
    
    try:
        cup_fixed_count = fix_missing_cups_from_addendum(
            db_config=db_config,
            base_download_url=base_url,
            error_log_path=error_log_path
        )
        robust_log(f"\n Addendum CUP Fixer completato: {cup_fixed_count} CUP aggiornati")
    except Exception as e:
        robust_log(f"\n Errore Addendum CUP Fixer: {e}")
        cup_fixed_count = 0
    
    # Report finale completo
    robust_log("\n" + "="*60)
    robust_log("REPORT FINALE COMPLETO")
    robust_log("="*60)
    robust_log(f" BDC importati con successo: {success_count}")
    robust_log(f" CUP aggiornati da addendum: {cup_fixed_count}")
    robust_log(f" BDC skipped: {skip_count}")
    robust_log(f" Errori: {error_count}")
    robust_log(f" Totale BDC processati: {len(paths)}")
    robust_log(f" File errori: {error_log_path}")
    robust_log("="*60)
    
    return success_count > 0


def test_connections():
    """Test connessioni database e API"""
    robust_log(" Test connessioni...")
    
    # Test DB
    try:
        db_config = get_db_config()
        result = fetch_mysql_query("SELECT 1 as test", db_config)
        robust_log(" Database: OK")
    except Exception as e:
        robust_log(f" Database: FAIL - {e}")
        return False
    
    # Test API
    try:
        api_config = get_api_config()
        robust_log(" API Config: OK")
    except Exception as e:
        robust_log(f" API Config: FAIL - {e}")
        return False
    
    robust_log(" Tutti i test superati")
    return True


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='BDC OCR Wrapper v3.1')
    parser.add_argument('--mode', choices=['test', 'dry-run', 'import'], 
                       default='import', help='Modalità operazione')
    parser.add_argument('--duplicate-handling', choices=['skip', 'replace', 'error'],
                       default='skip', help='Gestione duplicati')
    parser.add_argument('--limit', type=int, help='Limita numero file')
    parser.add_argument('--start-index', type=int, default=0,
                       help='Indice inizio batch')
    parser.add_argument('--batch-size', type=int, help='Dimensione batch')
    parser.add_argument('--max-workers', type=int, default=1,
                       help='Worker paralleli')
    
    args = parser.parse_args()
    
    robust_log('='*60)
    robust_log(f"BDC OCR PROCESSOR v3.1 - WRAPPER + ADDENDUM FIXER")
    robust_log('='*60)
    robust_log(f"Python: {sys.version}")
    robust_log(f"Working dir: {os.getcwd()}")
    robust_log(f"\nConfigurazione:")
    robust_log(f" Mode: {args.mode}")
    robust_log(f" Duplicate Handling: {args.duplicate_handling}")
    robust_log(f" Limit: {args.limit}")
    robust_log(f" Start Index: {args.start_index}")
    robust_log(f" Batch Size: {args.batch_size}")
    robust_log(f" Max Workers: {args.max_workers}")
    robust_log(f" Timestamp: {datetime.now()}")
    robust_log('-'*60)
    
    try:
        if args.mode == 'test':
            return test_connections()
        
        elif args.mode == 'dry-run':
            sal_folder = os.getenv('SAL_STORAGE_NAME')
            paths = get_pdf_paths_from_sal('bdc', include_p7m=True, sal_name=sal_folder)
            robust_log(f" Trovati {len(paths)} file BDC")
            for i, path in enumerate(paths[:10], 1):
                robust_log(f" {i}. {path}")
            if len(paths) > 10:
                robust_log(f" ... e altri {len(paths)-10} file")
            return True
        
        elif args.mode == 'import':
            return import_bdc(
                duplicate_handling=args.duplicate_handling,
                limit=args.limit,
                start_index=args.start_index,
                batch_size=args.batch_size,
                max_workers=args.max_workers
            )
        
    except KeyboardInterrupt:
        robust_log("\n Interrotto dall'utente")
        return False
    except Exception as e:
        final_error = f"ERRORE FATALE: {e}\n{traceback.format_exc()}"
        robust_log(f"\n {final_error}")
        return False


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        robust_log("\n Interrotto dall'utente")
        sys.exit(1)
    except Exception as e:
        robust_log(f"\n ERRORE FATALE: {e}")
        traceback.print_exc()
        sys.exit(1)