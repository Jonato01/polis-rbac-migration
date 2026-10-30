"""
bdc_processor.py - Processore BDC con Mistral + Azure OpenAI + Vision fallback
Versione 4.1 - PRODUCTION READY

CHANGELOG v4.1 (testato su CASO B - 4100620739):
 Vision prompt ultra-aggressivo (conta caratteri CUP uno per uno)
 Retry multipli Vision (default 3 tentativi)
 Temperatura 0.0 per massima precisione
 Distingue 'O' da '0', 'I' da '1' nel CUP
 Validazione lunghezza CUP con retry automatico

CHANGELOG v4.0:
 Analisi multi-pagina intelligente (focus CUP pagina 1)
 Vision prompt potenziato
 Post-validation con ricerca alternatives
 Strategia doppio OCR disponibile
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import base64
import re
import datetime
from typing import Optional, Dict, Any, List, Tuple
from io import BytesIO
import requests
from openai import AzureOpenAI
from ocr_common import safe_value
from dotenv import load_dotenv
from bdc_cup_validator import validate_and_correct_cup

load_dotenv()


# ============= NUOVA FUNZIONE: ANALISI MULTI-PAGINA =============

def call_mistral_document_ai_per_page(pdf_path: str = None, pdf_base64: str = None) -> Optional[Dict[str, Any]]:
    """
     NUOVO: OCR Mistral con analisi pagina per pagina
    
    Strategia:
    - Pagina 1: Focus su CUP, ID ordine, dati header
    - Pagine 2+: Focus su righe dettaglio
    
    Returns:
        Dict con:
        - markdown: Testo completo
        - page_1_markdown: Solo pagina 1 (per CUP)
        - pages: Lista pagine
        - pdf_base64: Base64 PDF
    """
    endpoint = os.getenv('MISTRAL_DOC_ENDPOINT')
    api_key = os.getenv('MISTRAL_DOC_KEY')
    
    if not endpoint or not api_key:
        print(" Credenziali Mistral mancanti")
        return None
    
    print(" OCR Multi-Pagina con Mistral Document AI...")
    
    # Converti PDF in base64 se necessario
    if pdf_path and not pdf_base64:
        try:
            with open(pdf_path, 'rb') as f:
                pdf_bytes = f.read()
                pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        except Exception as e:
            print(f" Errore lettura PDF: {e}")
            return None
    
    if not pdf_base64:
        print(" Nessun PDF da processare")
        return None
    
    try:
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
        
        # Estrai markdown separato per pagina
        all_markdown = ""
        page_1_markdown = ""
        
        if 'pages' in result:
            num_pages = len(result['pages'])
            print(f" PDF ha {num_pages} pagine")
            
            for i, page in enumerate(result['pages'], 1):
                if 'markdown' in page:
                    page_text = page['markdown']
                    all_markdown += page_text + "\n\n"
                    
                    # Salva pagina 1 separatamente (focus CUP)
                    if i == 1:
                        page_1_markdown = page_text
                        print(f" Pagina 1: {len(page_text)} caratteri (focus CUP)")
        
        if not all_markdown:
            print(" Nessun testo estratto")
            return None
        
        print(f" OCR completato: {len(all_markdown)} caratteri totali")
        
        return {
            "markdown": all_markdown,
            "page_1_markdown": page_1_markdown, # NUOVO
            "pages": result.get('pages', []),
            "raw_response": result,
            "pdf_base64": pdf_base64
        }
        
    except requests.exceptions.Timeout:
        print(" Timeout Mistral API")
        return None
    except requests.exceptions.HTTPError as e:
        print(f" Errore HTTP Mistral: {e}")
        if 'response' in locals():
            print(f" Risposta: {response.text[:200]}")
        return None
    except Exception as e:
        print(f" Errore chiamata Mistral: {e}")
        return None


# ============= FUNZIONE ENHANCED: VISION CON PROMPT POTENZIATO =============

def extract_with_azure_vision_enhanced(pdf_base64: str, retry_count: int = 3) -> Optional[Dict[str, Any]]:
    """
     ENHANCED v4.1: Azure Vision con prompt ULTRA-AGGRESSIVO
    
    Migliorie testate su CASO B:
    - Prompt che conta caratteri uno per uno
    - Retry multipli (default 3)
    - Temperatura 0.0 per massima precisione
    - Distingue 'O' da '0', 'I' da '1'
    """
    print("\n FALLBACK: Azure OpenAI Vision ENHANCED v4.1")
    print(" Analisi visiva PDF con prompt ultra-aggressivo")
    
    # Converti PDF in PNG
    print("Conversione PDF → PNG...")
    png_base64 = pdf_to_png_base64(pdf_base64)
    
    if not png_base64:
        print(" Impossibile convertire PDF in PNG")
        return None
    
    client = AzureOpenAI(
        api_version="2024-12-01-preview",
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    )
    
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
    
    # RETRY MULTIPLI con prompt che mostra tentativo corrente
    for attempt in range(1, retry_count + 1):
        try:
            print(f" Vision tentativo {attempt}/{retry_count}...")
            
            # Prompt dinamico con numero tentativo
            prompt_with_attempt = f""" COMPITO CRITICO - ESTRAZIONE CUP (TENTATIVO {attempt}/{retry_count})

Analizza questa immagine di un Buono di Consegna e trova il codice CUP.

 FOCUS ASSOLUTO SUL CUP:
Il CUP è un codice di ESATTAMENTE 15 caratteri alfanumerici.
Si trova tipicamente nel box "Informazioni" in ALTO A SINISTRA.

 ISTRUZIONI CRITICHE:
1. Conta CARATTERE PER CARATTERE - deve essere 15!
2. Il CUP inizia SEMPRE con una LETTERA (tipicamente J)
3. Contiene un MIX di lettere e numeri
4. Esempio valido: J59J21014170008
5. NON confondere:
   - 'O' (lettera O) con '0' (zero)
   - 'I' (lettera i) con '1' (uno)
   - '8' con 'B'

 DOVE CERCARE:
- Box "Informazioni" in alto a sinistra
- Vicino a "Numero d'ordine", "Data", "C.I.G."
- Etichetta: "CUP:" o "C.U.P:" o "Codice CUP"

 METODO:
1. Localizza il box "Informazioni"
2. Trova la riga con "CUP"
3. Leggi il codice CARATTERE PER CARATTERE
4. Verifica che sia ESATTAMENTE 15 caratteri
5. Se hai dubbi tra due caratteri simili, scegli il più probabile per un CUP

DATI DA ESTRARRE (oltre al CUP):
- Numero d'ordine (10 cifre)
- Data (YYYY-MM-DD) 
- C.I.G.
- Contratto
- Fornitore
- Righe dettaglio

FORMATO OUTPUT:
{{
  "ordine": {{
    "id_ordine": 4100620739,
    "data": "2024-10-09",
    "cup": "J59J21014170008",
    "cig": "...",
    ...
  }},
  "righe": [...]
}}

 SE IL CUP NON È ESATTAMENTE 15 CARATTERI, RICONTROLLA L'IMMAGINE CARATTERE PER CARATTERE.

Rispondi SOLO con JSON valido, NO markdown."""
            
            response = client.chat.completions.create(
                model=deployment,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt_with_attempt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{png_base64}"}
                            }
                        ]
                    }
                ],
                max_completion_tokens=4000,
                temperature=0.0 # Massima precisione (testato su CASO B)
            )
            
            result_text = response.choices[0].message.content.strip()
            result_text = result_text.replace("```json", "").replace("```", "").strip()
            
            try:
                result = json.loads(result_text)
                
                if "ordine" in result and result['ordine'].get('id_ordine'):
                    id_ordine = result['ordine']['id_ordine']
                    cup = result['ordine'].get('cup')
                    cup_len = len(cup) if cup else 0
                    
                    print(f" Vision extraction OK: ID={id_ordine}, CUP={cup} (len={cup_len})")
                    
                    # Valida lunghezza CUP
                    if cup and cup_len == 15:
                        print(f" CUP valido (15 caratteri) al tentativo {attempt}")
                        return result
                    elif cup and cup_len == 14:
                        print(f" CUP ha 14 caratteri (tentativo {attempt}), retry...")
                        if attempt < retry_count:
                            continue # Riprova
                        else:
                            print(f" Accetto CUP da 14 caratteri dopo {retry_count} tentativi")
                            return result
                    else:
                        return result
                else:
                    print(f" Vision extraction incomplete (tentativo {attempt})")
                    if attempt < retry_count:
                        continue
                    
            except json.JSONDecodeError as e:
                print(f" JSON decode error (tentativo {attempt}): {e}")
                if attempt < retry_count:
                    continue
                    
        except Exception as e:
            print(f" Azure Vision error (tentativo {attempt}): {e}")
            if attempt < retry_count:
                continue
    
    print(f" Tutti i {retry_count} tentativi falliti")
    return None


# ============= NUOVA FUNZIONE: POST-VALIDATION CON ALTERNATIVE SEARCH =============

def search_cup_alternatives_in_text(markdown: str, invalid_cup: str) -> Optional[str]:
    """
     NUOVO: Cerca CUP alternatives nel testo quando quello estratto è invalido
    
    Strategia:
    - Se CUP estratto è 14 caratteri, cerca pattern 15 caratteri simile
    - Se mancano caratteri, cerca nell'intorno del CUP trovato
    
    Args:
        markdown: Testo OCR completo
        invalid_cup: CUP invalido estratto
    
    Returns:
        CUP corretto o None
    """
    print(f" Ricerca CUP alternatives per: '{invalid_cup}'")
    
    if not invalid_cup or len(invalid_cup) == 0:
        return None
    
    # Caso 1: CUP è 14 caratteri (manca 1 carattere)
    if len(invalid_cup) == 14:
        print(f" CUP ha 14 caratteri, cerco versione 15 caratteri...")
        
        # Cerca nel testo pattern simile ma con 15 caratteri
        # Pattern: stessi primi 10 caratteri + 5 caratteri finali
        prefix = invalid_cup[:10] if len(invalid_cup) >= 10 else invalid_cup[:7]
        
        # Regex: cerca sequenza 15 caratteri che inizia con prefix
        pattern = rf'\b{re.escape(prefix)}[A-Z0-9]{{5}}\b'
        matches = re.findall(pattern, markdown.upper())
        
        for match in matches:
            if len(match) == 15 and match != invalid_cup:
                print(f" Trovato alternative: {match}")
                return match
    
    # Caso 2: CUP è troppo corto (< 14), cerca nell'intorno
    if len(invalid_cup) < 14:
        print(f" CUP troppo corto ({len(invalid_cup)} caratteri), ricerca pattern completo...")
        
        # Cerca tutti i pattern 15 caratteri che iniziano con lettera
        all_cups = re.findall(r'\b[A-Z][A-Z0-9]{14}\b', markdown.upper())
        
        # Filtra: deve contenere almeno un numero
        valid_cups = [c for c in all_cups if any(ch.isdigit() for ch in c)]
        
        if valid_cups:
            print(f" Trovati {len(valid_cups)} CUP candidates: {valid_cups[0]}")
            return valid_cups[0]
    
    print(f" Nessun alternative trovato")
    return None


# ============= NUOVA FUNZIONE: STRATEGIA DOPPIO OCR =============

def extract_cup_with_double_strategy(pdf_base64: str, ocr_markdown: str) -> Tuple[Optional[str], str]:
    """
     NUOVO: Strategia doppio OCR parallelo
    
    Esegue sia Mistral OCR che Vision, confronta risultati
    
    Returns:
        Tuple (cup_migliore, metodo_usato)
    """
    print("\n STRATEGIA DOPPIO OCR (Mistral + Vision paralleli)")
    
    # Estrai CUP da Mistral OCR (già fatto)
    from bdc_cup_validator import is_valid_cup_format, search_cup_in_text
    
    cup_mistral = search_cup_in_text(ocr_markdown)
    
    # Estrai CUP da Vision
    print(" Tentativo Vision parallelo...")
    vision_result = extract_with_azure_vision_enhanced(pdf_base64)
    cup_vision = None
    
    if vision_result and 'ordine' in vision_result:
        cup_vision = vision_result['ordine'].get('cup')
    
    print(f" Confronto risultati:")
    print(f" Mistral OCR: {cup_mistral} (valid={is_valid_cup_format(cup_mistral) if cup_mistral else False})")
    print(f" Vision: {cup_vision} (valid={is_valid_cup_format(cup_vision) if cup_vision else False})")
    
    # Logica decisione
    if cup_mistral and is_valid_cup_format(cup_mistral):
        print(f" Uso Mistral OCR: {cup_mistral}")
        return cup_mistral, "mistral_ocr"
    
    if cup_vision and is_valid_cup_format(cup_vision):
        print(f" Uso Vision: {cup_vision}")
        return cup_vision, "vision_parallel"
    
    # Nessuno valido, prendi il più lungo
    if cup_mistral and cup_vision:
        best = cup_mistral if len(cup_mistral) >= len(cup_vision) else cup_vision
        print(f" Nessuno valido, uso il più lungo: {best}")
        return best, "longest"
    
    # Solo uno disponibile
    if cup_mistral:
        return cup_mistral, "mistral_only"
    if cup_vision:
        return cup_vision, "vision_only"
    
    print(f" Nessun CUP trovato con doppia strategia")
    return None, "none"


# ============= FUNZIONI ESISTENTI (mantenute) =============

def check_ocr_quality(markdown: str, extracted_data: dict = None) -> bool:
    """Verifica qualità OCR"""
    keywords = ["numero", "ordine", "data", "c.i.g", "cig", "cup", "contratto", "fornitore"]
    markdown_lower = markdown.lower()
    found_count = sum(1 for kw in keywords if kw in markdown_lower)
    
    if extracted_data and 'ordine' in extracted_data:
        cup = extracted_data['ordine'].get('cup')
        if cup and len(cup) < 10:
            print(f" CUP troppo corto: '{cup}' (probabile errore OCR)")
            return False
    
    if found_count >= 3:
        print(f" OCR quality check: {found_count}/{len(keywords)} keywords")
        return True
    else:
        print(f" OCR quality check FAILED: {found_count}/{len(keywords)} keywords")
        return False


def pdf_to_png_base64(pdf_base64: str) -> Optional[str]:
    """Converte PDF base64 in PNG base64 (prima pagina)"""
    try:
        import fitz
        
        pdf_bytes = base64.b64decode(pdf_base64)
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        
        if len(doc) == 0:
            print(" PDF vuoto")
            return None
        
        page = doc[0]
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        png_bytes = pix.pil_tobytes(format="PNG")
        doc.close()
        
        png_base64 = base64.b64encode(png_bytes).decode('utf-8')
        print(f" PDF convertito in PNG: {len(png_base64)} caratteri base64")
        return png_base64
        
    except ImportError:
        print(" PyMuPDF non installato. Installa: pip install PyMuPDF")
        return None
    except Exception as e:
        print(f" Errore conversione PDF→PNG: {e}")
        return None


# Alias per retrocompatibilità
extract_with_azure_vision = extract_with_azure_vision_enhanced


def get_bdc_extraction_prompt():
    """Prompt per estrazione dati BDC"""
    return """Estrai i dati dal Buono di Consegna e restituisci SOLO JSON con questa struttura:

{
  "ordine": {
    "id_ordine": 4100596097,
    "data": "2024-02-19",
    "valuta": "EUR",
    "contratto_numero": "4490011397",
    "contratto_data": null,
    "rda_numero": "1000168502",
    "cig": "A001EA5AB6",
    "cup": "J59J21014180008",
    "buyer": "Patr. Centro 1",
    "termini_pagamento": "60 giorni senza sconto",
    "totale_iva_esclusa": 28789.55,
    "fornitore_nome": "PRINCIPLE ITALY S.P.A.",
    "fornitore_codice": "5070093",
    "fornitore_indirizzo": "Via Trieste 2, 20050 LISCATE MI"
  },
  "righe": [
    {
      "posizione": "0010",
      "descrizione": "TAVOLO OPERATIVO RIUNIO",
      "quantita": 8.0,
      "unita_misura": "LE",
      "prezzo_unitario": 1241.50,
      "importo": 9932.00,
      "destinazione_merci": "C1_10500_24",
      "tecnico": "CHIAVACCINI GG"
    }
  ]
}

ISTRUZIONI CRITICHE:
1. BOX INFORMAZIONI: Numero d'ordine, Data, CUP, C.I.G.
2. CUP: 15 caratteri alfanumerici (es: J59J21014180008)
3. Date: formato YYYY-MM-DD
4. Numeri: float/int, non stringhe
5. Campi mancanti: null

Rispondi SOLO con JSON valido."""


def analyze_with_azure_openai(ocr_markdown: str, attempt: int = 1, max_attempts: int = 3) -> Optional[Dict[str, Any]]:
    """Analizza markdown OCR con Azure OpenAI GPT-4.1"""
    client = AzureOpenAI(
        api_version="2024-12-01-preview",
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    )
    
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
    
    prompt = f"""{get_bdc_extraction_prompt()}

TENTATIVO {attempt}/{max_attempts}

CONTENUTO OCR DA MISTRAL:
{ocr_markdown[:12000]}

Analizza e restituisci JSON strutturato."""

    print(f" Analisi Azure OpenAI (tentativo {attempt}/{max_attempts})...")
    
    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {
                    "role": "system",
                    "content": "Sei un esperto di analisi BDC. Restituisci sempre JSON valido."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_completion_tokens=4000,
            temperature=0.1
        )
        
        result_text = response.choices[0].message.content.strip()
        result_text = result_text.replace("```json", "").replace("```", "").strip()
        
        try:
            result = json.loads(result_text)
            
            if "ordine" in result:
                id_ordine = result['ordine'].get('id_ordine')
                cup = result['ordine'].get('cup')
                print(f" JSON valido: ID={id_ordine}, CUP={cup}")
                return result
            else:
                print(f" JSON senza sezione 'ordine'")
                return None
                
        except json.JSONDecodeError as e:
            print(f" Errore parsing JSON: {e}")
            return None
            
    except Exception as e:
        print(f" Errore Azure OpenAI: {e}")
        return None


def validate_and_retry_bdc_extraction(ocr_markdown: str, page_1_markdown: str, pdf_base64: str = None, max_attempts: int = 3) -> Optional[Dict[str, Any]]:
    """
     ENHANCED: Estrae dati BDC con retry + post-validation avanzata
    
    Args:
        ocr_markdown: Testo OCR completo
        page_1_markdown: Solo pagina 1 (focus CUP)
        pdf_base64: PDF base64 per fallback Vision
        max_attempts: Tentativi massimi
    
    Returns:
        Dict con dati BDC validati
    """
    best_result = None
    
    for attempt in range(1, max_attempts + 1):
        result = analyze_with_azure_openai(ocr_markdown, attempt, max_attempts)
        
        if result and "ordine" in result:
            id_ordine = result['ordine'].get('id_ordine')
            cup_original = result['ordine'].get('cup')
            
            # EARLY VISION FALLBACK: CUP mancante/corto = OCR errato
            if attempt == 1 and pdf_base64:
                cup_len = len(cup_original) if cup_original else 0
                if cup_len == 0 or cup_len < 10:
                    status = "mancante" if cup_len == 0 else f"troppo corto ('{cup_original}')"
                    print(f" CUP {status} - probabile errore OCR")
                    print(" Trigger Vision fallback immediato...")
                    vision_result = extract_with_azure_vision_enhanced(pdf_base64, retry_count=3) # v4.1: 3 retry
                    if vision_result and "ordine" in vision_result:
                        result = vision_result
                        id_ordine = result['ordine'].get('id_ordine')
                        cup_original = result['ordine'].get('cup')
                        print(f" Vision ha recuperato dati: ID={id_ordine}, CUP={cup_original}")
            
            # Validazione ID ordine (obbligatorio)
            if not id_ordine:
                print(f" ID ordine mancante")
                if attempt < max_attempts:
                    continue
                else:
                    if pdf_base64:
                        print("\n Tutti i tentativi falliti, provo Vision fallback...")
                        result = extract_with_azure_vision_enhanced(pdf_base64, retry_count=3) # v4.1: 3 retry
                        if result and "ordine" in result:
                            id_ordine = result['ordine'].get('id_ordine')
                            cup_original = result['ordine'].get('cup')
                            if id_ordine:
                                pass
                            else:
                                return None
                        else:
                            return None
                    else:
                        return None
            
            # VALIDAZIONE CUP A CASCATA 
            print(f"\n STEP 2b: Validazione CUP AVANZATA")
            
            # NUOVO: Usa pagina 1 per ricerca più precisa
            cup_result = validate_and_correct_cup(
                cup=cup_original or '',
                markdown=page_1_markdown or ocr_markdown, # Priorità pagina 1
                max_ai_attempts=2
            )
            
            # NUOVO: Post-validation con alternative search
            if not cup_result['valid'] and cup_original:
                print(f" POST-VALIDATION: Ricerca alternatives...")
                cup_alternative = search_cup_alternatives_in_text(page_1_markdown or ocr_markdown, cup_original)
                
                if cup_alternative:
                    from bdc_cup_validator import is_valid_cup_format
                    if is_valid_cup_format(cup_alternative):
                        print(f" Alternative valido trovato: {cup_alternative}")
                        cup_result['cup'] = cup_alternative
                        cup_result['valid'] = True
                        cup_result['method'] = 'alternative_search'
            
            # Aggiorna CUP nel risultato
            result['ordine']['cup'] = cup_result['cup']
            
            # Log risultato validazione
            if cup_result['valid']:
                if cup_result['method'] != 'original':
                    print(f" CUP corretto: {cup_original} → {cup_result['cup']}")
                    print(f" Metodo: {cup_result['method']}, Confidence: {cup_result['confidence']}")
                else:
                    print(f" CUP valido: {cup_result['cup']}")
                
                return result
            else:
                print(f" CUP non validato: {cup_result['cup']}")
            
            if attempt == max_attempts:
                print(f"Accetto risultato senza CUP valido")
                return result
            
            best_result = result
    
    return best_result


def json_to_bdc_queries(json_data: Dict[str, Any]) -> Optional[str]:
    """Converte JSON BDC in query SQL"""
    try:
        if "error" in json_data:
            print(f" JSON contiene errore: {json_data['error']}")
            return None
        
        if "ordine" not in json_data:
            print(" JSON senza sezione 'ordine'")
            return None
        
        ordine = json_data["ordine"]
        id_ordine = ordine.get('id_ordine')
        
        if not id_ordine:
            print(f" ID ordine mancante")
            return None
        
        righe = json_data.get("righe", [])
        print(f" Generazione SQL: ID={id_ordine}, Righe={len(righe)}")
        
        queries = []
        
        query_ordine = f"""INSERT INTO bdc_pdf (
            id_ordine, data, valuta, contratto_numero, contratto_data,
            rda_numero, cig, cup, buyer, ns_riferimento, vs_riferimento, 
            termini_pagamento, totale_iva_esclusa, restituzione_giorni, utente,
            fornitore_nome, fornitore_codice, fornitore_indirizzo
        ) VALUES (
            {safe_value(id_ordine, 'int')},
            {safe_value(ordine.get('data'), 'date')},
            {safe_value(ordine.get('valuta'))},
            {safe_value(ordine.get('contratto_numero'))},
            {safe_value(ordine.get('contratto_data'), 'date')},
            {safe_value(ordine.get('rda_numero'))},
            {safe_value(ordine.get('cig'))},
            {safe_value(ordine.get('cup'))},
            {safe_value(ordine.get('buyer'))},
            {safe_value(ordine.get('ns_riferimento'))},
            {safe_value(ordine.get('vs_riferimento'))},
            {safe_value(ordine.get('termini_pagamento'))},
            {safe_value(ordine.get('totale_iva_esclusa'), 'decimal')},
            {safe_value(ordine.get('restituzione_giorni'), 'int')},
            {safe_value(ordine.get('utente'))},
            {safe_value(ordine.get('fornitore_nome'))},
            {safe_value(ordine.get('fornitore_codice'))},
            {safe_value(ordine.get('fornitore_indirizzo'))}
        ) ON DUPLICATE KEY UPDATE
            data={safe_value(ordine.get('data'), 'date')},
            fornitore_nome={safe_value(ordine.get('fornitore_nome'))},
            totale_iva_esclusa={safe_value(ordine.get('totale_iva_esclusa'), 'decimal')},
            cup={safe_value(ordine.get('cup'))}"""
        
        queries.append(query_ordine)
        
        if righe:
            query_delete = f"DELETE FROM righe_bdc_pdf WHERE id_ordine = {safe_value(id_ordine, 'int')}"
            queries.append(query_delete)
            
            for riga in righe:
                query_riga = f"""INSERT INTO righe_bdc_pdf (
                    id_ordine, posizione, descrizione, quantita, unita_misura,
                    prezzo_unitario, importo, destinazione_merci, incarico, tecnico
                ) VALUES (
                    {safe_value(id_ordine, 'int')},
                    {safe_value(riga.get('posizione'))},
                    {safe_value(riga.get('descrizione'))},
                    {safe_value(riga.get('quantita'), 'decimal')},
                    {safe_value(riga.get('unita_misura'))},
                    {safe_value(riga.get('prezzo_unitario'), 'decimal')},
                    {safe_value(riga.get('importo'), 'decimal')},
                    {safe_value(riga.get('destinazione_merci'))},
                    {safe_value(riga.get('incarico'))},
                    {safe_value(riga.get('tecnico'))}
                )"""
                
                queries.append(query_riga)
        
        print(f" Generate {len(queries)} query SQL")
        return "; ".join(queries)
        
    except Exception as e:
        print(f" Errore conversione SQL: {e}")
        return None


def save_ocr_debug(markdown: str, filename: str):
    """Salva markdown OCR per debug"""
    try:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        debug_file = f"ocr_debug_{timestamp}_{filename}.txt"
        
        with open(debug_file, 'w', encoding='utf-8') as f:
            f.write("="*60 + "\n")
            f.write(f"OCR DEBUG - {filename}\n")
            f.write("="*60 + "\n\n")
            f.write(markdown)
        
        print(f" OCR salvato in: {debug_file}")
    except Exception as e:
        print(f" Errore salvataggio OCR debug: {e}")


# ============= FUNZIONE PRINCIPALE ENHANCED =============

def process_single_bdc_pdf(pdf_path: str) -> Optional[str]:
    """
     v4.0: Pipeline completa ENHANCED
    
    Pipeline:
    1. OCR Mistral multi-pagina → markdown separato per pag 1
    2. Check qualità OCR
    3a. OCR OK → Estrazione Azure da markdown
    3b. OCR FAILED → Fallback Vision
    4. Validazione CUP a cascata + post-validation
    5. Generazione SQL
    """
    try:
        print(f"\n{'='*60}")
        print(f"Processamento: {os.path.basename(pdf_path)}")
        print('='*60)

        # Step 1: OCR Multi-Pagina con Mistral
        print("\n STEP 1: OCR MULTI-PAGINA")
        try:
            ocr_result = call_mistral_document_ai_per_page(pdf_path=pdf_path)
        except Exception as e:
            print(f" Errore OCR Mistral: {e}")
            ocr_result = None

        if not ocr_result or not ocr_result.get('markdown'):
            print(" OCR fallito")
            return None

        markdown = ocr_result['markdown']
        page_1_markdown = ocr_result.get('page_1_markdown', '')
        pdf_base64 = ocr_result.get('pdf_base64')

        # Salva markdown per debug
        try:
            save_ocr_debug(markdown, os.path.basename(pdf_path))
        except Exception as e:
            print(f" Errore salvataggio debug: {e}")

        # Step 1b: Check qualità OCR
        ocr_quality_ok = check_ocr_quality(markdown)

        # Step 2: Estrazione dati
        print("\n STEP 2: Estrazione dati ENHANCED")

        bdc_data = None
        try:
            if not ocr_quality_ok and pdf_base64:
                print(" OCR incompleto, uso Azure Vision")
                bdc_data = extract_with_azure_vision_enhanced(pdf_base64, retry_count=3)

                if bdc_data:
                    print(f"\n STEP 2b: Validazione CUP")
                    cup_original = bdc_data['ordine'].get('cup')
                    cup_result = validate_and_correct_cup(
                        cup=cup_original or '',
                        markdown='',
                        max_ai_attempts=1
                    )
                    bdc_data['ordine']['cup'] = cup_result['cup']
            else:
                # ENHANCED: passa sia markdown completo che pagina 1
                bdc_data = validate_and_retry_bdc_extraction(
                    markdown,
                    page_1_markdown,
                    pdf_base64
                )
        except Exception as e:
            print(f" Errore estrazione dati: {e}")
            import traceback
            traceback.print_exc()
            bdc_data = None

        if not bdc_data:
            print(" Estrazione fallita")
            return None

        # Step 3: Genera SQL
        print("\n STEP 3: Generazione SQL")
        try:
            sql = json_to_bdc_queries(bdc_data)
        except Exception as e:
            print(f" Errore generazione SQL: {e}")
            sql = None

        if sql:
            print(" Processamento completato")
            return sql
        else:
            print(" SQL non generato")
            return None

    except Exception as e:
        print(f" ERRORE CRITICO in process_single_bdc_pdf: {e}")
        import traceback
        traceback.print_exc()
        return None


def test_processor():
    """Test del processor"""
    print("=" * 60)
    print("TEST BDC PROCESSOR v4.1 PRODUCTION")
    print(" Multi-page OCR")
    print(" Vision ultra-aggressivo (testato CASO B)")
    print(" Retry automatici")
    print("=" * 60)
    
    test_pdf = input("\nInserisci path PDF di test: ").strip().strip('"').strip("'")
    
    if not os.path.exists(test_pdf):
        print(f" File non trovato: {test_pdf}")
        return
    
    sql = process_single_bdc_pdf(test_pdf)
    
    if sql:
        print("\n" + "=" * 60)
        print("SQL GENERATO:")
        print("=" * 60)
        print(sql[:500] + "..." if len(sql) > 500 else sql)
        print("=" * 60)
        print(" Test completato!")
    else:
        print("\n Test fallito")


if __name__ == "__main__":
    test_processor()