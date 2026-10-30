"""
bdc_ocr.py - OCR per Buoni di Consegna (BDC)
Versione 3.0 - Con supporto P7M + Mistral + Azure OpenAI

Pipeline:
1. Gestione P7M → estrazione PDF temporaneo
2. OCR Mistral Document AI → gestisce PDF nativi e immagini
3. Estrazione dati Azure OpenAI → con validazione CUP
4. Generazione SQL → INSERT database

Dipendenze:
- bdc_p7m_handler.py
- bdc_processor.py
- ocr_common.py
"""
import os
import sys
# Aggiungi parent directory al path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


import time
from ocr_common import get_pdf_paths_from_sal, execute_batch_queries, get_db_config
from bdc_p7m_handler import process_bdc_file, cleanup_temp_file
from bdc_processor import process_single_bdc_pdf



def process_bdc_file_complete(file_url: str, base_download_url: str) -> str:
    """
    Processa un file BDC completo (gestisce P7M e PDF)
    
    Args:
        file_url: URL file da processare (path relativo)
        base_download_url: Base URL per download
    
    Returns:
        SQL query string o None
    """
    full_url = f"{base_download_url}/{file_url}"
    filename = file_url.split('/')[-1]
    
    print(f"\n{'='*60}")
    print(f" File: {filename}")
    print('='*60)
    
    pdf_path = None
    is_temp = False
    
    try:
        # Step 1: Download file
        print(" Download file...")
        import requests
        from ocr_common import fetch_pdf_base64_cached, fetch_pdf_base64
        
        # Usa cache se disponibile
        file_base64 = fetch_pdf_base64_cached(full_url) or fetch_pdf_base64(full_url)
        
        if not file_base64:
            print(" Download fallito")
            return None
        
        # Salva temporaneamente
        import tempfile
        import base64
        
        suffix = '.p7m' if filename.lower().endswith('.p7m') else '.pdf'
        temp_fd, temp_path = tempfile.mkstemp(suffix=suffix, prefix='bdc_')
        
        with os.fdopen(temp_fd, 'wb') as f:
            f.write(base64.b64decode(file_base64))
        
        print(f" File salvato: {os.path.basename(temp_path)}")
        
        # Step 2: Gestione P7M (se necessario)
        if filename.lower().endswith('.p7m'):
            print("\n Estrazione da P7M...")
            pdf_path, is_temp = process_bdc_file(temp_path)
            
            # Elimina P7M temporaneo
            cleanup_temp_file(temp_path)
        else:
            pdf_path = temp_path
            is_temp = True
        
        # Step 3: Processa PDF con Mistral + Azure
        sql = process_single_bdc_pdf(pdf_path)
        
        return sql
        
    except Exception as e:
        print(f" Errore processamento: {e}")
        import traceback
        traceback.print_exc()
        return None
        
    finally:
        # Cleanup file temporanei
        if is_temp and pdf_path:
            cleanup_temp_file(pdf_path)


def load_env_file(env_file='.colenv'):
    """Load environment variables from .colenv (lightweight, local copy)"""
    try:
        if os.path.exists(env_file):
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        os.environ.setdefault(key, value)
            print(f"Loaded environment from {env_file}")
        else:
            print(f".colenv not found: {env_file}")
    except Exception as e:
        print(f"Error loading .colenv: {e}")


def main():
    """
    Main function per processing batch BDC
    """
    start_time = time.time()
    
    print("=" * 60)
    print("BDC OCR PROCESSING v3.0")
    print("Mistral Document AI + Azure OpenAI GPT-4.1")
    print("=" * 60)
    
    # Check environment variables
    required_vars = [
        'MISTRAL_DOC_ENDPOINT',
        'MISTRAL_DOC_KEY',
        'AZURE_OPENAI_ENDPOINT',
        'AZURE_OPENAI_KEY',
        'AZURE_DOWNLOAD_URL'
    ]
    
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        print(f" Variabili mancanti in .env: {', '.join(missing)}")
        return False
    
    # Get PDF e P7M paths
    print("\n Ricerca file BDC (PDF + P7M)...")
    # Load .colenv to ensure AZURE_IMPORT_PATH_URL can be read
    load_env_file('.colenv')
    # Ensure AZURE_IMPORT_PATH_URL has the production value if not set in env/.colenv
    os.environ.setdefault('AZURE_IMPORT_PATH_URL', 'https://polis-coll-api-storage-dec5dpfhhzdrehck.northeurope-01.azurewebsites.net/api/sal/import-path')
    # Determine sal_name: prefer SAL_STORAGE_NAME env if set
    sal_name = os.getenv('SAL_STORAGE_NAME')
    if sal_name:
        print(f"Using SAL_STORAGE_NAME from env: {sal_name}")
    else:
        print("No SAL_STORAGE_NAME in env; calling API without sal_name parameter")

    # Pass sal_name explicitly to get_pdf_paths_from_sal
    pdf_paths = get_pdf_paths_from_sal("BDC", include_p7m=True, sal_name=sal_name)

    if not pdf_paths:
        print(" Nessun file trovato")
        return False
    
    print(f" Trovati {len(pdf_paths)} file")
    
    # Show sample
    print("\nEsempi file:")
    for i, path in enumerate(pdf_paths[:5], 1):
        print(f" {i}. {path.split('/')[-1]}")
    if len(pdf_paths) > 5:
        print(f" ... e altri {len(pdf_paths) - 5} file")
    
    # Process files
    base_url = os.getenv('AZURE_DOWNLOAD_URL')
    all_sql = []
    
    print(f"\n{'='*60}")
    print("INIZIO PROCESSAMENTO")
    print('='*60)
    
    for i, file_path in enumerate(pdf_paths, 1):
        print(f"\n[{i}/{len(pdf_paths)}] Processamento file...")
        
        sql = process_bdc_file_complete(file_path, base_url)
        
        if sql:
            all_sql.append(sql)
            print(f" [{i}/{len(pdf_paths)}] Completato")
        else:
            print(f" [{i}/{len(pdf_paths)}] Skipped")
    
    # Execute SQL
    if not all_sql:
        print("\n Nessuna query generata")
        return False
    
    print(f"\n{'='*60}")
    print("ESECUZIONE SQL")
    print('='*60)
    print(f"Query da eseguire: {len(all_sql)}")
    
    db_config = get_db_config()
    success = execute_batch_queries(all_sql, db_config)
    
    elapsed = time.time() - start_time
    
    print(f"\n{'='*60}")
    if success:
        print(" COMPLETATO CON SUCCESSO")
        print(f" Tempo: {elapsed:.1f} secondi")
        print(f" File processati: {len(all_sql)}/{len(pdf_paths)}")
        print("\nVerifica database:")
        print(" SELECT COUNT(*) FROM bdc_pdf;")
        print(" SELECT COUNT(*) FROM righe_bdc_pdf;")
    else:
        print(" ERRORE DURANTE ESECUZIONE SQL")
    print('='*60)
    
    return success

