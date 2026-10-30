#!/usr/bin/env python3
"""
Timesheet Wrapper v1.0
Orchestratore per gestione importazione timesheet da PDF con Azure OpenAI

Modalità operative:
- import: Importa nuovi timesheet (batch mode)
- history: Mostra ultimi record importati
- rollback: Cancella record recenti (con conferma)

Utilizzo:
    python timesheet_wrapper.py --mode import --limit 5 --dry-run
    python timesheet_wrapper.py --mode history --limit 20
    python timesheet_wrapper.py --mode rollback --minutes 60
"""

import sys
import argparse
import mysql.connector
from datetime import datetime, timedelta
import traceback
from pathlib import Path
from dotenv import load_dotenv
import os
import concurrent.futures  # Parallelizzazione
import requests

# Path setup - AGGIUNGERE QUESTO
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
    
# Import del processore principale
from timesheet import PDFTimesheetProcessor

# Import funzioni comuni
from ocr_common import get_pdf_paths_from_sal

# Carica variabili d'ambiente
load_dotenv()

# ============================================================================
# DYNAMIC SCALING FUNCTIONS (aggiunte per versione Container App)
# ============================================================================

def count_pdfs_to_import() -> int:
    """Conta il numero totale di PDF timesheet firmati disponibili per import.
    Usa preferibilmente l'API standardizzata dei path storage se disponibile, altrimenti fallback su processor.get_pdf_list()."""
    try:
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        paths = get_pdf_paths_from_sal("timesheet_firmati", sal_name=sal_folder)
        if paths:
            return len(paths)
        processor = PDFTimesheetProcessor()
        pdf_list = processor.get_pdf_list()
        return len(pdf_list) if pdf_list else 0
    except Exception as e:
        robust_log(f"[SCALING] Errore conteggio PDF: {e}")
        return 0


def calculate_optimal_workers(total_pdfs: int, base_workers: int) -> tuple[int, int]:
    """Calcola numero ottimale di workers e batch size suggerito.
    Ritorna (optimal_workers, suggested_batch_size).
    Limite effettivo: max(base_workers, MAX_WORKERS_ABSOLUTE). Mantiene (1,1) se total_pdfs==0.
    """
    try:
        MAX_WORKERS_ABSOLUTE = int(os.getenv('MAX_WORKERS_ABSOLUTE', '4'))
        SCALING_MULTIPLIER = float(os.getenv('SCALING_MULTIPLIER', '2.0'))
        PDFS_PER_WORKER = int(os.getenv('PDFS_PER_WORKER', '10'))
        effective_cap = max(base_workers, MAX_WORKERS_ABSOLUTE)

        if total_pdfs == 0:
            return 1, 1
        if total_pdfs <= 10:
            optimal = min(total_pdfs, effective_cap, max(1, base_workers))
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
        robust_log(f"[SCALING] Errore calcolo workers: {e}")
        return base_workers, max(1, total_pdfs)

# ============================================================================
# LOGGING SETUP
# ============================================================================

_log_file = None
log_filename = None # Global variable for log filename


def setup_logging(filename=None):
    """Setup logging to file and console"""
    global _log_file, log_filename
    
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"timesheet_{timestamp}.log"
    else:
        log_filename = filename
    
    try:
        _log_file = open(log_filename, 'a', encoding='utf-8', buffering=1)
        robust_log(f"Logging to file: {log_filename}")
    except Exception as e:
        print(f"Could not open log file: {e}")
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




class TimesheetWrapperOrchestrator:
    """Orchestratore per gestione import/history/rollback timesheet"""
    
    def __init__(self):
        """Inizializza orchestratore con configurazione database"""
        self.db_config = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'user': os.getenv('DB_USER', 'root'),
            'password': os.getenv('DB_PASSWORD', ''),
            'database': os.getenv('DB_NAME', 'polis'),
            'charset': 'utf8mb4'
        }
        
        # Inizializza processore principale
        self.processor = PDFTimesheetProcessor()
        
        robust_log("TimesheetWrapperOrchestrator inizializzato")
    
    def import_mode(self, limit: int = 3, dry_run: bool = False):
        """
        ModalitÃƒÂ  IMPORT: processa batch di PDF timesheet
        
        Args:
            limit: Numero massimo di PDF da processare
            dry_run: Se True, non salva nel database
        """
        robust_log("="*70)
        robust_log(f"MODALITÃƒâ‚¬ IMPORT - Timesheet (limit={limit}, dry_run={dry_run})")
        robust_log("="*70)
        
        try:
            # Processa batch usando il processore principale
            result = self.processor.process_batch_from_api(
                limit=limit,
                dry_run=dry_run
            )
            
            # Report finale
            if result.get("success", 0) > 0:
                robust_log("\n" + "="*70)
                robust_log("Ã¢Å“â€¦ IMPORT COMPLETATO CON SUCCESSO!")
                robust_log(f" PDF processati: {result.get('processed', 0)}")
                robust_log(f" Successi: {result.get('success', 0)}")
                robust_log(f" Fallimenti: {result.get('failed', 0)}")
                robust_log("="*70)
                return 0
            else:
                robust_log("\n" + "="*70)
                robust_log("Ã¢ÂÅ’ IMPORT FALLITO O NESSUN SUCCESSO!")
                robust_log("="*70)
                return 1
                
        except Exception as e:
            robust_log(f"Errore durante import: {e}")
            return 1
    
    def history_mode(self, limit: int = 20):
        """
        ModalitÃƒÂ  HISTORY: mostra ultimi record importati
        
        Args:
            limit: Numero di record da visualizzare
        """
        robust_log("="*70)
        robust_log(f"MODALITÃƒâ‚¬ HISTORY - Ultimi {limit} timesheet importati")
        robust_log("="*70)
        
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor(dictionary=True)
            
            # Query con JOIN per ottenere anche il nome completo del dipendente
            cursor.execute("""
                SELECT 
                    t.id,
                    t.nome_cognome,
                    t.cod_dipendente,
                    COALESCE(d.nome_completo, 'NON TROVATO') as dipendente_db,
                    t.categoria_dipendente,
                    t.numero_sal,
                    t.mese,
                    t.anno,
                    t.totale_ore_mese,
                    t.firma_presente,
                    t.data_estrazione,
                    t.nome_file
                FROM pdf_timesheet t
                LEFT JOIN dipendenti d ON t.cod_dipendente = d.cod_dipendente
                ORDER BY t.data_estrazione DESC
                LIMIT %s
            """, (limit,))
            
            records = cursor.fetchall()
            
            if not records:
                robust_log("Nessun record trovato nel database")
                return 0
            
            robust_log(f"\nTrovati {len(records)} record:\n")
            
            # Header tabella
            print(f"{'ID':<5} {'Nome Estratto':<25} {'Cod':<5} {'Nome DB':<25} {'Cat':<4} {'SAL':<8} {'M/A':<7} {'Ore':<6} {'Firma':<5} {'Data Import':<20} {'File':<30}")
            print("-" * 170)
            
            # Righe
            for rec in records:
                match_status = "Ã¢Å“â€œ" if rec['cod_dipendente'] else "Ã¢Å“â€”"
                firma_status = "Ã¢Å“â€œ" if rec['firma_presente'] else "Ã¢Å“â€”"
                
                print(
                    f"{rec['id']:<5} "
                    f"{rec['nome_cognome'][:24]:<25} "
                    f"{rec['cod_dipendente'] or '-':<5} "
                    f"{rec['dipendente_db'][:24]:<25} "
                    f"{rec['categoria_dipendente']:<4} "
                    f"{rec['numero_sal'] or '-':<8} "
                    f"{rec['mese']}/{rec['anno']:<7} "
                    f"{float(rec['totale_ore_mese']):<6.1f} "
                    f"{firma_status:<5} "
                    f"{rec['data_estrazione'].strftime('%Y-%m-%d %H:%M:%S'):<20} "
                    f"{rec['nome_file'][:29] if rec['nome_file'] else '-':<30}"
                )
            
            # Statistiche
            total_ore = sum(float(r['totale_ore_mese']) for r in records)
            matched = sum(1 for r in records if r['cod_dipendente'])
            match_rate = (matched / len(records) * 100) if records else 0
            
            robust_log(f"\n{'='*70}")
            robust_log(f"Statistiche:")
            robust_log(f" Record totali: {len(records)}")
            robust_log(f" Ore totali: {total_ore:.1f}")
            robust_log(f" Match dipendenti: {matched}/{len(records)} ({match_rate:.1f}%)")
            robust_log("="*70)
            
            cursor.close()
            conn.close()
            return 0
            
        except Exception as e:
            robust_log(f"Errore durante visualizzazione history: {e}")
            return 1
    
    def rollback_mode(self, minutes: int = 60, force: bool = False):
        """
        ModalitÃƒÂ  ROLLBACK: cancella record importati negli ultimi N minuti
        
        Args:
            minutes: Finestra temporale in minuti
            force: Se True, salta conferma interattiva
        """
        robust_log("="*70)
        robust_log(f"MODALITÃƒâ‚¬ ROLLBACK - Ultimi {minutes} minuti")
        robust_log("="*70)
        
        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor(dictionary=True)
            
            # Calcola timestamp limite
            time_threshold = datetime.now() - timedelta(minutes=minutes)
            
            # Conta record da cancellare
            cursor.execute("""
                SELECT COUNT(*) as count
                FROM pdf_timesheet
                WHERE data_estrazione >= %s
            """, (time_threshold,))
            
            count_result = cursor.fetchone()
            record_count = count_result['count'] if count_result else 0
            
            if record_count == 0:
                robust_log(f"Nessun record trovato negli ultimi {minutes} minuti")
                cursor.close()
                conn.close()
                return 0
            
            # Mostra anteprima record da cancellare
            cursor.execute("""
                SELECT id, nome_cognome, categoria_dipendente, mese, anno, 
                       totale_ore_mese, data_estrazione, nome_file
                FROM pdf_timesheet
                WHERE data_estrazione >= %s
                ORDER BY data_estrazione DESC
            """, (time_threshold,))
            
            records = cursor.fetchall()
            
            robust_log(f"\nÃ¢Å¡Â Ã¯Â¸Â ATTENZIONE: {record_count} record saranno cancellati!\n")
            
            print(f"{'ID':<5} {'Nome':<25} {'Cat':<4} {'M/A':<7} {'Ore':<6} {'Data Import':<20} {'File':<30}")
            print("-" * 120)
            
            for rec in records:
                print(
                    f"{rec['id']:<5} "
                    f"{rec['nome_cognome'][:24]:<25} "
                    f"{rec['categoria_dipendente']:<4} "
                    f"{rec['mese']}/{rec['anno']:<7} "
                    f"{float(rec['totale_ore_mese']):<6.1f} "
                    f"{rec['data_estrazione'].strftime('%Y-%m-%d %H:%M:%S'):<20} "
                    f"{rec['nome_file'][:29] if rec['nome_file'] else '-':<30}"
                )
            
            # Conferma utente (se non force)
            if not force:
                print(f"\nÃ¢Å¡Â Ã¯Â¸Â Procedere con la cancellazione di {record_count} record? (yes/no): ", end='')
                confirm = input().strip().lower()
                
                if confirm != 'yes':
                    robust_log("Rollback annullato dall'utente")
                    cursor.close()
                    conn.close()
                    return 0
            
            # Esegue DELETE
            cursor.execute("""
                DELETE FROM pdf_timesheet
                WHERE data_estrazione >= %s
            """, (time_threshold,))
            
            deleted_count = cursor.rowcount
            conn.commit()
            
            robust_log(f"\nÃ¢Å“â€¦ Rollback completato: {deleted_count} record cancellati")
            
            cursor.close()
            conn.close()
            return 0
            
        except Exception as e:
            robust_log(f"Errore durante rollback: {e}")
            return 1


# ============================================================================
# TEST & DRY-RUN FUNCTIONS
# ============================================================================

def test_connections_standalone():
    """Test database and API connections"""
    robust_log("=== STARTING CONNECTION TESTS ===")
    
    try:
        # Test database
        robust_log("Testing database connection...")
        try:
            db_config = {
                'host': os.getenv('DB_HOST', 'localhost'),
                'user': os.getenv('DB_USER', 'root'),
                'password': os.getenv('DB_PASSWORD', ''),
                'database': os.getenv('DB_NAME', 'polis'),
                'charset': 'utf8mb4'
            }
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
            azure_search_url = os.getenv('AZURE_SEARCH_URL', 'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/search')
            test_url = f"{azure_search_url}/unified?search_type=folders&limit=1&root_only=true"
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


def dry_run_standalone(limit=3):
    """Show what files would be processed without doing anything"""
    robust_log("=== STARTING DRY RUN ===")
    robust_log(f"Limit: {limit}")
    
    try:
        robust_log("Searching for TIMESHEET PDF files...")
        
        # Usa il nuovo sistema standardizzato
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("timesheet_firmati", sal_name=sal_folder)

        if pdf_paths:
            robust_log(f"Found {len(pdf_paths)} TIMESHEET PDF files:")
            for i, path in enumerate(pdf_paths, 1):
                if i <= 10:
                    robust_log(f" {i}. {path}")
                elif i == 11:
                    robust_log(f" ... and {len(pdf_paths) - 10} more files")
            robust_log("=== DRY RUN COMPLETED: SUCCESS ===")
            return True
        else:
            robust_log("No TIMESHEET PDF files found")
            robust_log("=== DRY RUN COMPLETED: NO FILES ===")
            return False
            
    except Exception as e:
        robust_log(f"ERROR during dry run: {e}")
        robust_log(f"Dry run error details: {traceback.format_exc()}")
        robust_log("=== DRY RUN COMPLETED: FAILED ===")
        return False


def main():
    """Entry point principale con gestione modalitÃƒÂ """
    
    parser = argparse.ArgumentParser(
        description='Timesheet Wrapper - Orchestratore import/history/rollback',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi d'uso:

  # Import: processa 5 PDF in modalitÃƒÂ  dry-run
  python timesheet_wrapper.py --mode import --limit 5 --dry-run
  
  # Import: processa 10 PDF e salva nel database
  python timesheet_wrapper.py --mode import --limit 10
  
  # History: mostra ultimi 30 record importati
  python timesheet_wrapper.py --mode history --limit 30
  
  # Rollback: cancella import degli ultimi 30 minuti (con conferma)
  python timesheet_wrapper.py --mode rollback --minutes 30
  
  # Rollback: cancella senza conferma (automatico)
  python timesheet_wrapper.py --mode rollback --minutes 60 --force
        """
    )
    
    parser.add_argument(
        '--mode',
        choices=['test', 'dry-run', 'import', 'history', 'rollback'],
        required=True,
        help='ModalitÃƒÂ  operativa'
    )
    
    parser.add_argument(
        '--limit',
        type=int,
        default=3,
        help='Import: numero PDF da processare (default: 3) | History: record da mostrare (default: 20)'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Import: non salva nel database, solo estrazione'
    )
    
    parser.add_argument(
        '--minutes',
        type=int,
        default=60,
        help='Rollback: finestra temporale in minuti (default: 60)'
    )
    
    parser.add_argument(
        '--force',
        action='store_true',
        help='Rollback: salta conferma interattiva'
    )
    
    args = parser.parse_args()
    
    # Banner iniziale
    robust_log("\n" + "="*70)
    robust_log("TIMESHEET WRAPPER v1.0 - Azure OpenAI PDF Processor")
    robust_log("="*70)
    robust_log(f"ModalitÃƒÂ : {args.mode.upper()}")
    robust_log(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    robust_log(f"Log file: {log_filename}")
    robust_log("="*70 + "\n")
    
    # Inizializza orchestratore
    orchestrator = TimesheetWrapperOrchestrator()
    
    # Esegui modalitÃƒÂ  richiesta
    try:
        if args.mode == 'test':
            robust_log("Executing connection tests...")
            success = test_connections_standalone()
            exit_code = 0 if success else 1
            robust_log(f"\nTest completed with exit code: {exit_code}")
            sys.exit(exit_code)
            
        elif args.mode == 'dry-run':
            robust_log("Executing dry run...")
            success = dry_run_standalone(limit=args.limit)
            exit_code = 0 if success else 1
            robust_log(f"\nDry run completed with exit code: {exit_code}")
            sys.exit(exit_code)
            
        elif args.mode == 'import':
            exit_code = orchestrator.import_mode(
                limit=args.limit,
                dry_run=args.dry_run
            )
        
        elif args.mode == 'history':
            # Per history, usa limit con default diverso se non specificato
            history_limit = args.limit if args.limit != 3 else 20
            exit_code = orchestrator.history_mode(limit=history_limit)
        
        elif args.mode == 'rollback':
            exit_code = orchestrator.rollback_mode(
                minutes=args.minutes,
                force=args.force
            )
        
        else:
            robust_log(f"ModalitÃƒÂ  non riconosciuta: {args.mode}")
            exit_code = 1
        
        sys.exit(exit_code)
        
    except KeyboardInterrupt:
        robust_log("\nÃ¢Å¡Â Ã¯Â¸Â Operazione interrotta dall'utente (Ctrl+C)")
        sys.exit(130)
    
    except Exception as e:
        robust_log(f"Errore fatale: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

# ============================================================================
# FUNZIONI PER CONTAINER SERVICE (parallel import + test connessioni)
# ============================================================================

def test_connections() -> bool:
    """Testa connessione DB e API (versione per Container App)."""
    robust_log("=== CONNECTION TESTS (Container Service) ===")
    db_ok = False
    api_ok = False
    # DB
    try:
        db_config = {
            'host': os.getenv('DB_HOST'),
            'user': os.getenv('DB_USER'),
            'password': os.getenv('DB_PASSWORD'),
            'database': os.getenv('DB_NAME', 'polis'),
            'charset': 'utf8mb4'
        }
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close(); conn.close()
        robust_log("Database connection: SUCCESS")
        db_ok = True
    except Exception as e:
        robust_log(f"Database connection FAILED: {e}")
    # API
    try:
        azure_search_url = os.getenv('AZURE_SEARCH_URL') or os.getenv('AZURE_STORAGE_API_URL')
        if azure_search_url:
            test_url = f"{azure_search_url}/search?limit=1"
            resp = requests.get(test_url, timeout=10)
            if resp.status_code < 500:
                robust_log(f"Azure API reachable (status {resp.status_code})")
                api_ok = True
            else:
                robust_log(f"Azure API failed status: {resp.status_code}")
        else:
            robust_log("Azure API URL non configurato")
    except Exception as e:
        robust_log(f"Azure API connection FAILED: {e}")
    overall = db_ok and api_ok
    robust_log(f"=== CONNECTION TESTS RESULT: {'SUCCESS' if overall else 'FAILED'} ===")
    return overall

def run_import(limit: int | None = None,
               start_index: int | None = None,
               batch_size: int | None = None,
               max_workers: int = 4,
               auto_scale: bool = True,
               dry_run: bool = False) -> bool:
    """Esegue import parallelo dei PDF timesheet con optional dynamic scaling.
    Usa PDFTimesheetProcessor.process_pdf per ogni file.
    """
    robust_log("== TIMESHEET PARALLEL IMPORT START ==")
    processor = PDFTimesheetProcessor()

    # Recupera lista PDF (preferendo storage path standardizzato)
    sal_folder = os.getenv('SAL_STORAGE_NAME')
    storage_paths = get_pdf_paths_from_sal("timesheet_firmati", sal_name=sal_folder)
    if storage_paths:
        pdf_list = storage_paths
        robust_log(f"[SOURCE] Storage path API (timesheet_firmati) - {len(pdf_list)} files")
        is_local = False  # Saranno scaricati via download API
    else:
        pdf_list = processor.get_pdf_list()
        robust_log(f"[SOURCE] Processor API import path - {len(pdf_list)} files")
        is_local = False

    if not pdf_list:
        robust_log("Nessun PDF disponibile per l'import")
        return False

    total_available = len(pdf_list)

    # Applica limit o batch range
    if start_index is not None and batch_size is not None:
        end_index = min(start_index + batch_size, total_available)
        pdf_list = pdf_list[start_index:end_index]
        robust_log(f"Batch range selezionato: {start_index}-{end_index-1}")
    elif limit and limit > 0:
        pdf_list = pdf_list[:limit]
        robust_log(f"Limit applicato: {limit} files")

    total_pdfs = len(pdf_list)

    if total_pdfs == 0:
        robust_log("Nessun PDF nel range specificato")
        return False

    # Dynamic scaling override
    env_max = int(os.getenv('MAX_WORKERS', str(max_workers)))
    if auto_scale:
        optimal_workers, suggested_batch = calculate_optimal_workers(total_pdfs, env_max)
        workers_to_use = optimal_workers if optimal_workers > env_max else env_max
        robust_log(f"[SCALING] total={total_pdfs} optimal={optimal_workers} env_max={env_max} chosen={workers_to_use} suggested_batch={suggested_batch}")
    else:
        workers_to_use = env_max
        robust_log(f"[SCALING] auto_scale disabilitato: workers={workers_to_use}")

    workers_to_use = min(workers_to_use, total_pdfs)

    robust_log(f"Processo con {workers_to_use} worker paralleli")
    robust_log("Prime 5 files:")
    for i, p in enumerate(pdf_list[:5], 1):
        robust_log(f" {i}. {p}")
    if total_pdfs > 5:
        robust_log(f" ... altri {total_pdfs - 5} files")

    stats_processed = 0
    stats_success = 0
    stats_failed = 0

    def _process_single(path: str):
        try:
            return processor.process_pdf(path, is_local=False, dry_run=dry_run)
        except Exception as e:
            return {"error": str(e)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers_to_use) as executor:
        future_map = {executor.submit(_process_single, pdf_path): pdf_path for pdf_path in pdf_list}
        for future in concurrent.futures.as_completed(future_map):
            pdf_path = future_map[future]
            filename = Path(pdf_path).name
            try:
                result = future.result()
                stats_processed += 1
                if "error" in result:
                    stats_failed += 1
                    robust_log(f"[FAIL] {filename}: {result['error']}")
                else:
                    stats_success += 1
                    robust_log(f"[OK] {filename} (saved_rows={result.get('db_rows_saved')})")
            except Exception as e:
                stats_processed += 1
                stats_failed += 1
                robust_log(f"[EXCEPTION] {filename}: {e}")
            # Progress
            pct = (stats_processed / total_pdfs) * 100
            robust_log(f"Progress {pct:.1f}% | Success {stats_success} | Failed {stats_failed}")

    robust_log("== TIMESHEET PARALLEL IMPORT COMPLETED ==")
    robust_log(f"Processed: {stats_processed}")
    robust_log(f"Success: {stats_success}")
    robust_log(f"Failed: {stats_failed}")
    success_rate = (stats_success / stats_processed * 100) if stats_processed else 0
    robust_log(f"Success rate: {success_rate:.1f}%")
    return stats_success > 0

# ============================================================================
# LOGGING SETUP (RESTO ORIGINALE)
# ============================================================================

_log_file = None
log_filename = None # Global variable for log filename


def setup_logging(filename=None):
    """Setup logging to file and console"""
    global _log_file, log_filename

    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"timesheet_{timestamp}.log"
    else:
        log_filename = filename

    try:
        _log_file = open(log_filename, 'a', encoding='utf-8', buffering=1)
        robust_log(f"Logging to file: {log_filename}")
    except Exception as e:
        print(f"Could not open log file: {e}")
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




class TimesheetWrapperOrchestrator:
    """Orchestratore per gestione import/history/rollback timesheet"""

    def __init__(self):
        """Inizializza orchestratore con configurazione database"""
        self.db_config = {
            'host': os.getenv('DB_HOST', 'localhost'),
            'user': os.getenv('DB_USER', 'root'),
            'password': os.getenv('DB_PASSWORD', ''),
            'database': os.getenv('DB_NAME', 'polis'),
            'charset': 'utf8mb4'
        }

        # Inizializza processore principale
        self.processor = PDFTimesheetProcessor()

        robust_log("TimesheetWrapperOrchestrator inizializzato")

    def import_mode(self, limit: int = 3, dry_run: bool = False):
        """
        ModalitÃƒÂ  IMPORT: processa batch di PDF timesheet

        Args:
            limit: Numero massimo di PDF da processare
            dry_run: Se True, non salva nel database
        """
        robust_log("="*70)
        robust_log(f"MODALITÃƒâ‚¬ IMPORT - Timesheet (limit={limit}, dry_run={dry_run})")
        robust_log("="*70)

        try:
            # Processa batch usando il processore principale
            result = self.processor.process_batch_from_api(
                limit=limit,
                dry_run=dry_run
            )

            # Report finale
            if result.get("success", 0) > 0:
                robust_log("\n" + "="*70)
                robust_log("Ã¢Å“â€¦ IMPORT COMPLETATO CON SUCCESSO!")
                robust_log(f" PDF processati: {result.get('processed', 0)}")
                robust_log(f" Successi: {result.get('success', 0)}")
                robust_log(f" Fallimenti: {result.get('failed', 0)}")
                robust_log("="*70)
                return 0
            else:
                robust_log("\n" + "="*70)
                robust_log("Ã¢ÂÅ’ IMPORT FALLITO O NESSUN SUCCESSO!")
                robust_log("="*70)
                return 1

        except Exception as e:
            robust_log(f"Errore durante import: {e}")
            return 1

    def history_mode(self, limit: int = 20):
        """
        ModalitÃƒÂ  HISTORY: mostra ultimi record importati

        Args:
            limit: Numero di record da visualizzare
        """
        robust_log("="*70)
        robust_log(f"MODALITÃƒâ‚¬ HISTORY - Ultimi {limit} timesheet importati")
        robust_log("="*70)

        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor(dictionary=True)

            # Query con JOIN per ottenere anche il nome completo del dipendente
            cursor.execute("""
                SELECT 
                    t.id,
                    t.nome_cognome,
                    t.cod_dipendente,
                    COALESCE(d.nome_completo, 'NON TROVATO') as dipendente_db,
                    t.categoria_dipendente,
                    t.numero_sal,
                    t.mese,
                    t.anno,
                    t.totale_ore_mese,
                    t.firma_presente,
                    t.data_estrazione,
                    t.nome_file
                FROM pdf_timesheet t
                LEFT JOIN dipendenti d ON t.cod_dipendente = d.cod_dipendente
                ORDER BY t.data_estrazione DESC
                LIMIT %s
            """, (limit,))

            records = cursor.fetchall()

            if not records:
                robust_log("Nessun record trovato nel database")
                return 0

            robust_log(f"\nTrovati {len(records)} record:\n")

            # Header tabella
            print(f"{'ID':<5} {'Nome Estratto':<25} {'Cod':<5} {'Nome DB':<25} {'Cat':<4} {'SAL':<8} {'M/A':<7} {'Ore':<6} {'Firma':<5} {'Data Import':<20} {'File':<30}")
            print("-" * 170)

            # Righe
            for rec in records:
                match_status = "Ã¢Å“â€œ" if rec['cod_dipendente'] else "Ã¢Å“â€”"
                firma_status = "Ã¢Å“â€œ" if rec['firma_presente'] else "Ã¢Å“â€”"

                print(
                    f"{rec['id']:<5} "
                    f"{rec['nome_cognome'][:24]:<25} "
                    f"{rec['cod_dipendente'] or '-':<5} "
                    f"{rec['dipendente_db'][:24]:<25} "
                    f"{rec['categoria_dipendente']:<4} "
                    f"{rec['numero_sal'] or '-':<8} "
                    f"{rec['mese']}/{rec['anno']:<7} "
                    f"{float(rec['totale_ore_mese']):<6.1f} "
                    f"{firma_status:<5} "
                    f"{rec['data_estrazione'].strftime('%Y-%m-%d %H:%M:%S'):<20} "
                    f"{rec['nome_file'][:29] if rec['nome_file'] else '-':<30}"
                )

            # Statistiche
            total_ore = sum(float(r['totale_ore_mese']) for r in records)
            matched = sum(1 for r in records if r['cod_dipendente'])
            match_rate = (matched / len(records) * 100) if records else 0

            robust_log(f"\n{'='*70}")
            robust_log(f"Statistiche:")
            robust_log(f" Record totali: {len(records)}")
            robust_log(f" Ore totali: {total_ore:.1f}")
            robust_log(f" Match dipendenti: {matched}/{len(records)} ({match_rate:.1f}%)")
            robust_log("="*70)

            cursor.close()
            conn.close()
            return 0

        except Exception as e:
            robust_log(f"Errore durante visualizzazione history: {e}")
            return 1

    def rollback_mode(self, minutes: int = 60, force: bool = False):
        """
        ModalitÃƒÂ  ROLLBACK: cancella record importati negli ultimi N minuti

        Args:
            minutes: Finestra temporale in minuti
            force: Se True, salta conferma interattiva
        """
        robust_log("="*70)
        robust_log(f"MODALITÃƒâ‚¬ ROLLBACK - Ultimi {minutes} minuti")
        robust_log("="*70)

        try:
            conn = mysql.connector.connect(**self.db_config)
            cursor = conn.cursor(dictionary=True)

            # Calcola timestamp limite
            time_threshold = datetime.now() - timedelta(minutes=minutes)

            # Conta record da cancellare
            cursor.execute("""
                SELECT COUNT(*) as count
                FROM pdf_timesheet
                WHERE data_estrazione >= %s
            """, (time_threshold,))

            count_result = cursor.fetchone()
            record_count = count_result['count'] if count_result else 0

            if record_count == 0:
                robust_log(f"Nessun record trovato negli ultimi {minutes} minuti")
                cursor.close()
                conn.close()
                return 0

            # Mostra anteprima record da cancellare
            cursor.execute("""
                SELECT id, nome_cognome, categoria_dipendente, mese, anno, 
                       totale_ore_mese, data_estrazione, nome_file
                FROM pdf_timesheet
                WHERE data_estrazione >= %s
                ORDER BY data_estrazione DESC
            """, (time_threshold,))

            records = cursor.fetchall()

            robust_log(f"\nÃ¢Å¡Â Ã¯Â¸Â ATTENZIONE: {record_count} record saranno cancellati!\n")

            print(f"{'ID':<5} {'Nome':<25} {'Cat':<4} {'M/A':<7} {'Ore':<6} {'Data Import':<20} {'File':<30}")
            print("-" * 120)

            for rec in records:
                print(
                    f"{rec['id']:<5} "
                    f"{rec['nome_cognome'][:24]:<25} "
                    f"{rec['categoria_dipendente']:<4} "
                    f"{rec['mese']}/{rec['anno']:<7} "
                    f"{float(rec['totale_ore_mese']):<6.1f} "
                    f"{rec['data_estrazione'].strftime('%Y-%m-%d %H:%M:%S'):<20} "
                    f"{rec['nome_file'][:29] if rec['nome_file'] else '-':<30}"
                )

            # Conferma utente (se non force)
            if not force:
                print(f"\nÃ¢Å¡Â Ã¯Â¸Â Procedere con la cancellazione di {record_count} record? (yes/no): ", end='')
                confirm = input().strip().lower()

                if confirm != 'yes':
                    robust_log("Rollback annullato dall'utente")
                    cursor.close()
                    conn.close()
                    return 0

            # Esegue DELETE
            cursor.execute("""
                DELETE FROM pdf_timesheet
                WHERE data_estrazione >= %s
            """, (time_threshold,))

            deleted_count = cursor.rowcount
            conn.commit()

            robust_log(f"\nÃ¢Å“â€¦ Rollback completato: {deleted_count} record cancellati")

            cursor.close()
            conn.close()
            return 0

        except Exception as e:
            robust_log(f"Errore durante rollback: {e}")
            return 1


# ============================================================================
# TEST & DRY-RUN FUNCTIONS
# ============================================================================

def test_connections_standalone():
    """Test database and API connections"""
    robust_log("=== STARTING CONNECTION TESTS ===")

    try:
        # Test database
        robust_log("Testing database connection...")
        try:
            db_config = {
                'host': os.getenv('DB_HOST', 'localhost'),
                'user': os.getenv('DB_USER', 'root'),
                'password': os.getenv('DB_PASSWORD', ''),
                'database': os.getenv('DB_NAME', 'polis'),
                'charset': 'utf8mb4'
            }
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
            azure_search_url = os.getenv('AZURE_SEARCH_URL', 'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/search')
            test_url = f"{azure_search_url}/unified?search_type=folders&limit=1&root_only=true"
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


def dry_run_standalone(limit=3):
    """Show what files would be processed without doing anything"""
    robust_log("=== STARTING DRY RUN ===")
    robust_log(f"Limit: {limit}")

    try:
        robust_log("Searching for TIMESHEET PDF files...")

        # Usa il nuovo sistema standardizzato
        sal_folder = os.getenv('SAL_STORAGE_NAME')
        pdf_paths = get_pdf_paths_from_sal("timesheet_firmati", sal_name=sal_folder)

        if pdf_paths:
            robust_log(f"Found {len(pdf_paths)} TIMESHEET PDF files:")
            for i, path in enumerate(pdf_paths, 1):
                if i <= 10:
                    robust_log(f" {i}. {path}")
                elif i == 11:
                    robust_log(f" ... and {len(pdf_paths) - 10} more files")
            robust_log("=== DRY RUN COMPLETED: SUCCESS ===")
            return True
        else:
            robust_log("No TIMESHEET PDF files found")
            robust_log("=== DRY RUN COMPLETED: NO FILES ===")
            return False

    except Exception as e:
        robust_log(f"ERROR during dry run: {e}")
        robust_log(f"Dry run error details: {traceback.format_exc()}")
        robust_log("=== DRY RUN COMPLETED: FAILED ===")
        return False


def main():
    """Entry point principale con gestione modalitÃƒÂ """

    parser = argparse.ArgumentParser(
        description='Timesheet Wrapper - Orchestratore import/history/rollback',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi d'uso:

  # Import: processa 5 PDF in modalitÃƒÂ  dry-run
  python timesheet_wrapper.py --mode import --limit 5 --dry-run
  
  # Import: processa 10 PDF e salva nel database
  python timesheet_wrapper.py --mode import --limit 10
  
  # History: mostra ultimi 30 record importati
  python timesheet_wrapper.py --mode history --limit 30
  
  # Rollback: cancella import degli ultimi 30 minuti (con conferma)
  python timesheet_wrapper.py --mode rollback --minutes 30
  
  # Rollback: cancella senza conferma (automatico)
  python timesheet_wrapper.py --mode rollback --minutes 60 --force
        """
    )

    parser.add_argument(
        '--mode',
        choices=['test', 'dry-run', 'import', 'history', 'rollback'],
        required=True,
        help='ModalitÃƒÂ  operativa'
    )

    parser.add_argument(
        '--limit',
        type=int,
        default=3,
        help='Import: numero PDF da processare (default: 3) | History: record da mostrare (default: 20)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Import: non salva nel database, solo estrazione'
    )

    parser.add_argument(
        '--minutes',
        type=int,
        default=60,
        help='Rollback: finestra temporale in minuti (default: 60)'
    )

    parser.add_argument(
        '--force',
        action='store_true',
        help='Rollback: salta conferma interattiva'
    )

    args = parser.parse_args()

    # Banner iniziale
    robust_log("\n" + "="*70)
    robust_log("TIMESHEET WRAPPER v1.0 - Azure OpenAI PDF Processor")
    robust_log("="*70)
    robust_log(f"ModalitÃƒÂ : {args.mode.upper()}")
    robust_log(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    robust_log(f"Log file: {log_filename}")
    robust_log("="*70 + "\n")

    # Inizializza orchestratore
    orchestrator = TimesheetWrapperOrchestrator()

    # Esegui modalitÃƒÂ  richiesta
    try:
        if args.mode == 'test':
            robust_log("Executing connection tests...")
            success = test_connections_standalone()
            exit_code = 0 if success else 1
            robust_log(f"\nTest completed with exit code: {exit_code}")
            sys.exit(exit_code)

        elif args.mode == 'dry-run':
            robust_log("Executing dry run...")
            success = dry_run_standalone(limit=args.limit)
            exit_code = 0 if success else 1
            robust_log(f"\nDry run completed with exit code: {exit_code}")
            sys.exit(exit_code)

        elif args.mode == 'import':
            exit_code = orchestrator.import_mode(
                limit=args.limit,
                dry_run=args.dry_run
            )

        elif args.mode == 'history':
            # Per history, usa limit con default diverso se non specificato
            history_limit = args.limit if args.limit != 3 else 20
            exit_code = orchestrator.history_mode(limit=history_limit)

        elif args.mode == 'rollback':
            exit_code = orchestrator.rollback_mode(
                minutes=args.minutes,
                force=args.force
            )

        else:
            robust_log(f"ModalitÃƒÂ  non riconosciuta: {args.mode}")
            exit_code = 1

        sys.exit(exit_code)

    except KeyboardInterrupt:
        robust_log("\nÃ¢Å¡Â Ã¯Â¸Â Operazione interrotta dall'utente (Ctrl+C)")
        sys.exit(130)

    except Exception as e:
        robust_log(f"Errore fatale: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
