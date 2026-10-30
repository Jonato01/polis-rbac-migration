"""
bdc_addendum_cup_fixer.py - Aggiorna CUP mancanti usando addendum
Versione 1.2 - Fix pattern regex bidirezionale

Pipeline:
1. Query DB: BDC con cup IS NULL
2. Per ogni id_ordine:
   - Cerca addendum via API (pattern: addendum*{id_ordine} OR {id_ordine}*addendum)
   - Download + OCR Mistral
   - Estrai CUP (regex + Azure fallback)
   - Valida con bdc_cup_validator
   - UPDATE database
3. Report: successi/fallimenti
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import tempfile
import base64
from typing import Optional, Dict, Any, List
import mysql.connector
from ocr_common import fetch_pdf_base64, get_connection_pool, get_pdf_paths_from_sal
from bdc_cup_validator import validate_and_correct_cup


def robust_log(message):
    """Log semplice per debugging"""
    print(f" {message}")


def log_error_to_file(id_ordine, error_type, error_message, error_log_path):
    """Salva errore nel file di log"""
    try:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(error_log_path, 'a', encoding='utf-8') as f:
            f.write(f"{timestamp}|ADDENDUM_{id_ordine}|{error_type}|{error_message}\n")
    except Exception as e:
        robust_log(f" Errore log: {e}")


def get_bdc_with_missing_cup(db_config: Dict) -> List[int]:
    """
    Query database per BDC con cup IS NULL
    
    Returns:
        Lista di id_ordine
    """
    try:
        pool = get_connection_pool(db_config)
        conn = pool.get_connection()
        cursor = conn.cursor()
        
        query = """
            SELECT id_ordine 
            FROM bdc_pdf 
            WHERE cup IS NULL OR cup = ''
            ORDER BY id_ordine DESC
        """
        
        cursor.execute(query)
        results = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        id_ordini = [row[0] for row in results]
        robust_log(f" Trovati {len(id_ordini)} BDC con CUP mancante")
        
        return id_ordini
        
    except Exception as e:
        robust_log(f" Errore query database: {e}")
        return []


def search_addendum_file(id_ordine: int, base_download_url: str, debug: bool = True) -> Optional[str]:
    """
    Cerca file addendum usando get_pdf_paths_from_sal
    
    Pattern bidirezionale:
    - {id_ordine}*addendum*.pdf (es: 4100570989 addendum-signed.pdf)
    - addendum*{id_ordine}*.pdf (es: addendum BC 4100632075.pdf)
    
    Returns:
        Path completo del file addendum o None
    """
    try:
        if debug:
            robust_log(f" Ricerca addendum per ID: {id_ordine}")
        
        # Ottieni TUTTI i file BDC
        all_bdc_files = get_pdf_paths_from_sal('bdc', include_p7m=False)
        
        if debug:
            robust_log(f" Trovati {len(all_bdc_files)} file BDC totali")
        
        # Pattern BIDIREZIONALE - matcha entrambi gli ordini
        # Deve contenere ENTRAMBI "addendum" (case insensitive) E id_ordine
        # in qualsiasi ordine
        pattern_id_first = re.compile(rf'.*{id_ordine}.*addendum.*\.pdf$', re.IGNORECASE)
        pattern_addendum_first = re.compile(rf'.*addendum.*{id_ordine}.*\.pdf$', re.IGNORECASE)
        
        if debug:
            robust_log(f" Pattern 1: .*{id_ordine}.*addendum.*\\.pdf")
            robust_log(f" Pattern 2: .*addendum.*{id_ordine}.*\\.pdf")
        
        matched = []
        for file_path in all_bdc_files:
            filename = file_path.split('/')[-1]
            
            # Controlla se contiene l'ID
            if str(id_ordine) in filename:
                if debug:
                    robust_log(f" File con ID: {filename}")
                
                # Prova entrambi i pattern
                if pattern_id_first.match(filename) or pattern_addendum_first.match(filename):
                    matched.append(file_path)
                    if debug:
                        robust_log(f" MATCH addendum!")
        
        if not matched:
            if debug:
                robust_log(f" Nessun addendum trovato per ID {id_ordine}")
            return None
        
        # Prendi il primo match
        filename = matched[0].split('/')[-1]
        robust_log(f" Addendum trovato: {filename}")
        
        if len(matched) > 1:
            robust_log(f" Trovati {len(matched)} addendum, uso il primo")
        
        return matched[0]
        
    except Exception as e:
        robust_log(f" Errore ricerca addendum: {e}")
        if debug:
            import traceback
            robust_log(f"Stack trace: {traceback.format_exc()}")
        return None


def call_mistral_ocr_on_addendum(pdf_base64: str) -> Optional[str]:
    """
    OCR Mistral su addendum PDF
    
    Returns:
        Markdown text o None
    """
    try:
        import requests
        
        endpoint = os.getenv('MISTRAL_DOC_ENDPOINT')
        api_key = os.getenv('MISTRAL_DOC_KEY')
        
        if not endpoint or not api_key:
            robust_log(" Credenziali Mistral mancanti")
            return None
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        payload = {
            "model": "mistral-col-document-ai",
            "document": {
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{pdf_base64}"
            },
            "include_image_base64": False
        }
        
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=120
        )
        
        response.raise_for_status()
        result = response.json()
        
        # Estrai markdown
        all_markdown = ""
        if 'pages' in result:
            for page in result['pages']:
                if 'markdown' in page:
                    all_markdown += page['markdown'] + "\n\n"
        
        if not all_markdown:
            return None
        
        robust_log(f" OCR completato: {len(all_markdown)} caratteri")
        return all_markdown
        
    except Exception as e:
        robust_log(f" Errore Mistral OCR: {e}")
        return None


def extract_cup_from_addendum_text(markdown: str) -> Optional[str]:
    """
    Estrae CUP da testo addendum usando regex
    
    Pattern tipico:
    "...con l'aggiunta del Codice Univoco di Progetto (CUP) J59J21014170008..."
    
    Returns:
        CUP estratto o None
    """
    # Pattern 1: dopo "CUP)" o "CUP " o "CUP:"
    patterns = [
        r'CUP\)\s*([A-Z][A-Z0-9]{14})', # CUP) J59J21014170008
        r'CUP\s+([A-Z][A-Z0-9]{14})', # CUP J59J21014170008
        r'CUP:\s*([A-Z][A-Z0-9]{14})', # CUP: J59J21014170008
        r'\(CUP\)\s*([A-Z][A-Z0-9]{14})', # (CUP) J59J21014170008
    ]
    
    for pattern in patterns:
        match = re.search(pattern, markdown, re.IGNORECASE)
        if match:
            cup = match.group(1).upper()
            robust_log(f" CUP trovato via regex: {cup}")
            return cup
    
    robust_log(" CUP non trovato via regex")
    return None


def extract_cup_from_addendum_ai(markdown: str, id_ordine: int) -> Optional[str]:
    """
    Fallback: estrae CUP usando Azure OpenAI
    
    Returns:
        CUP estratto o None
    """
    try:
        from openai import AzureOpenAI
        
        client = AzureOpenAI(
            api_version="2024-12-01-preview",
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
            api_key=os.getenv("AZURE_OPENAI_KEY")
        )
        
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
        
        # Limita testo
        text_excerpt = markdown[:2000]
        
        prompt = f"""Questo è un addendum che modifica il Buono di Consegna n. {id_ordine}.

COMPITO: Trova il codice CUP (Codice Univoco di Progetto) menzionato nel testo.

TESTO:
{text_excerpt}

REGOLE CUP:
- Esattamente 15 caratteri alfanumerici
- Inizia con lettera maiuscola
- Esempio: J59J21014170008
- DEVE contenere almeno un numero

Se trovi il CUP, rispondi SOLO con il codice (15 caratteri).
Se NON trovi il CUP, rispondi "NOT_FOUND".

NO spiegazioni, NO markdown, SOLO il codice o NOT_FOUND."""
        
        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {"role": "system", "content": "Estrai solo il codice CUP."},
                {"role": "user", "content": prompt}
            ],
            max_completion_tokens=50,
            temperature=0.0
        )
        
        cup_found = response.choices[0].message.content.strip().replace('`', '').replace('"', '').strip()
        
        if cup_found != "NOT_FOUND" and len(cup_found) == 15:
            robust_log(f" CUP trovato via Azure AI: {cup_found}")
            return cup_found.upper()
        
        robust_log(" Azure AI non ha trovato CUP")
        return None
        
    except Exception as e:
        robust_log(f" Errore Azure AI: {e}")
        return None


def extract_cup_from_addendum(pdf_base64: str, id_ordine: int) -> Optional[str]:
    """
    Pipeline completa estrazione CUP da addendum
    
    1. OCR Mistral
    2. Regex search
    3. Azure AI fallback
    4. Validazione
    
    Returns:
        CUP validato o None
    """
    # Step 1: OCR
    markdown = call_mistral_ocr_on_addendum(pdf_base64)
    if not markdown:
        robust_log(" OCR fallito")
        return None
    
    # Step 2: Regex search
    cup_regex = extract_cup_from_addendum_text(markdown)
    if cup_regex:
        return cup_regex
    
    # Step 3: Azure AI fallback
    cup_ai = extract_cup_from_addendum_ai(markdown, id_ordine)
    if cup_ai:
        return cup_ai
    
    robust_log(" Nessun CUP trovato nell'addendum")
    return None


def update_bdc_cup(id_ordine: int, cup: str, db_config: Dict) -> bool:
    """
    UPDATE database con nuovo CUP
    
    Returns:
        True se successo
    """
    try:
        pool = get_connection_pool(db_config)
        conn = pool.get_connection()
        cursor = conn.cursor()
        
        query = """
            UPDATE bdc_pdf 
            SET cup = %s 
            WHERE id_ordine = %s
        """
        
        cursor.execute(query, (cup, id_ordine))
        conn.commit()
        
        rows_affected = cursor.rowcount
        
        cursor.close()
        conn.close()
        
        if rows_affected > 0:
            robust_log(f" Database aggiornato: id_ordine={id_ordine}, cup={cup}")
            return True
        else:
            robust_log(f" Nessuna riga aggiornata per id_ordine={id_ordine}")
            return False
        
    except Exception as e:
        robust_log(f" Errore UPDATE database: {e}")
        return False


def process_single_addendum(
    id_ordine: int,
    base_download_url: str,
    db_config: Dict,
    error_log_path: str
) -> bool:
    """
    Processa singolo BDC con CUP mancante
    
    Returns:
        True se CUP aggiornato con successo
    """
    robust_log(f"\n{'='*50}")
    robust_log(f" ID Ordine: {id_ordine}")
    robust_log('='*50)
    
    # Step 1: Cerca addendum (con debug)
    addendum_path = search_addendum_file(id_ordine, base_download_url, debug=True)
    
    if not addendum_path:
        robust_log(" Addendum non trovato")
        log_error_to_file(id_ordine, "ADDENDUM_NOT_FOUND", "Nessun file addendum trovato", error_log_path)
        return False
    
    # Step 2: Download addendum
    try:
        full_url = f"{base_download_url}/{addendum_path}"
        robust_log(f" Download da: {full_url[:80]}...")
        pdf_base64 = fetch_pdf_base64(full_url)
        
        if not pdf_base64:
            robust_log(" Download addendum fallito")
            log_error_to_file(id_ordine, "DOWNLOAD_FAILED", "Download addendum fallito", error_log_path)
            return False
    except Exception as e:
        robust_log(f" Errore download: {e}")
        log_error_to_file(id_ordine, "DOWNLOAD_ERROR", str(e), error_log_path)
        return False
    
    # Step 3: Estrai CUP
    cup_extracted = extract_cup_from_addendum(pdf_base64, id_ordine)
    
    if not cup_extracted:
        robust_log(" CUP non estratto dall'addendum")
        log_error_to_file(id_ordine, "CUP_NOT_EXTRACTED", "CUP non trovato nel PDF", error_log_path)
        return False
    
    # Step 4: Valida CUP con bdc_cup_validator
    robust_log(" Validazione CUP...")
    
    # Per addendum, markdown è vuoto (CUP già estratto)
    cup_result = validate_and_correct_cup(
        cup=cup_extracted,
        markdown='', # Non serve markdown, CUP già estratto
        max_ai_attempts=1
    )
    
    if not cup_result['valid']:
        robust_log(f" CUP non valido: {cup_result['cup']}")
        log_error_to_file(id_ordine, "CUP_INVALID", f"CUP {cup_result['cup']} non valido", error_log_path)
        # Procedi comunque con UPDATE (meglio CUP dubbio che NULL)
    
    cup_final = cup_result['cup']
    
    # Step 5: UPDATE database
    success = update_bdc_cup(id_ordine, cup_final, db_config)
    
    if success:
        robust_log(f" CUP aggiornato: {cup_final}")
        return True
    else:
        log_error_to_file(id_ordine, "UPDATE_FAILED", "UPDATE database fallito", error_log_path)
        return False


def fix_missing_cups_from_addendum(
    db_config: Dict,
    base_download_url: str,
    error_log_path: str
) -> int:
    """
    Pipeline principale: aggiorna tutti i CUP mancanti
    
    Returns:
        Numero di CUP aggiornati con successo
    """
    robust_log(" Ricerca BDC con CUP mancante...")
    
    # Query database
    id_ordini_missing_cup = get_bdc_with_missing_cup(db_config)
    
    if not id_ordini_missing_cup:
        robust_log(" Nessun BDC con CUP mancante")
        return 0
    
    robust_log(f" Trovati {len(id_ordini_missing_cup)} BDC da processare")
    
    # Processa ogni BDC
    success_count = 0
    
    for i, id_ordine in enumerate(id_ordini_missing_cup, 1):
        robust_log(f"\n[{i}/{len(id_ordini_missing_cup)}] Processamento...")
        
        try:
            success = process_single_addendum(
                id_ordine=id_ordine,
                base_download_url=base_download_url,
                db_config=db_config,
                error_log_path=error_log_path
            )
            
            if success:
                success_count += 1
        except Exception as e:
            robust_log(f" Errore processamento id_ordine {id_ordine}: {e}")
            log_error_to_file(id_ordine, "PROCESSING_ERROR", str(e), error_log_path)
    
    # Report finale
    robust_log("\n" + "="*50)
    robust_log(f" REPORT ADDENDUM FIXER")
    robust_log("="*50)
    robust_log(f"BDC con CUP mancante: {len(id_ordini_missing_cup)}")
    robust_log(f" CUP aggiornati: {success_count}")
    robust_log(f" Falliti: {len(id_ordini_missing_cup) - success_count}")
    robust_log("="*50)
    
    return success_count


def test_addendum_fixer():
    """Test standalone del fixer"""
    print("="*60)
    print("TEST ADDENDUM CUP FIXER")
    print("="*60)
    
    # Config
    from ocr_common import get_db_config
    
    db_config = get_db_config()
    base_url = os.getenv('AZURE_DOWNLOAD_URL')
    error_log_path = "test_addendum_errors.txt"
    
    # Test
    count = fix_missing_cups_from_addendum(db_config, base_url, error_log_path)
    
    print(f"\n Test completato: {count} CUP aggiornati")


if __name__ == "__main__":
    test_addendum_fixer()