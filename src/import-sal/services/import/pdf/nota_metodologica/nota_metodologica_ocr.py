# nota_metodologica_ocr.py - OCR con Mistral Document AI + Azure OpenAI GPT-4.1
# Version 3.0 - Versione production con Mistral + GPT-4.1

import json
import time
import os
import base64
import re
import requests
from openai import AzureOpenAI

from ocr_common import *
from dotenv import load_dotenv

load_dotenv()

def call_mistral_document_ai(pdf_path=None, pdf_base64=None):
    """Chiama Mistral Document AI per estrarre il contenuto OCR"""
    endpoint = os.getenv("MISTRAL_DOC_ENDPOINT")
    api_key = os.getenv("MISTRAL_DOC_KEY")
    
    if not endpoint or not api_key:
        print(" ERRORE: Credenziali Mistral mancanti nel .env")
        return None
    
    print(" Connessione a Mistral Document AI...")
    
    # Converti PDF in base64 se necessario
    if pdf_path and not pdf_base64:
        try:
            with open(pdf_path, 'rb') as pdf_file:
                pdf_bytes = pdf_file.read()
                pdf_base64 = base64.b64encode(pdf_bytes).decode('utf-8')
        except Exception as e:
            print(f" Errore lettura PDF: {e}")
            return None
    
    if not pdf_base64:
        print(" Nessun PDF da processare")
        return None
    
    # Headers per la richiesta
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    # Payload
    payload = {
        "model": "mistral-col-document-ai",
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{pdf_base64}"
        },
        "include_image_base64": True
    }
    
    print(" Invio richiesta OCR a Mistral...")
    
    try:
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            print(" OCR completato con successo")
            
            # Estrai tutto il markdown
            all_markdown = ""
            if 'pages' in result:
                for page in result['pages']:
                    if 'markdown' in page:
                        all_markdown += page['markdown'] + "\n\n"
            
            return {
                "markdown": all_markdown,
                "pages": result.get('pages', []),
                "raw_response": result
            }
        else:
            print(f" Errore Mistral: {response.status_code}")
            print(f"Risposta: {response.text}")
            return None
            
    except Exception as e:
        print(f" Errore chiamata Mistral: {e}")
        return None

def analyze_with_azure_openai(ocr_content, attempt=1):
    """Analizza e struttura il contenuto OCR usando Azure OpenAI GPT-4.1"""
    
    # Configurazione Azure OpenAI
    client = AzureOpenAI(
        api_version="2024-12-01-preview",
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    )
    
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1")
    
    prompt = f"""Sei un esperto nell'analisi di documenti contenenti tabelle di costi del personale.

Tentativo {attempt}/3 - Analizza TUTTE le tabelle dei costi del personale nel documento.

CONTENUTO OCR DA MISTRAL:
{ocr_content[:8000]} # Limitato per token

COMPITO:
1. Identifica TUTTE le tabelle presenti (dipendenti, dirigenti, DTO, TA, etc.)
2. Estrai TUTTI i valori numerici con precisione
3. Struttura i dati per inserimento database con questi campi:
   - livello (es: A1, A2, B, C, D, E, DIRIGENTE)
   - minimi_contrattuali
   - retribuzione_individuale 
   - elemento_retribuzione_fissa
   - indennita_funzione_quadri
   - tredicesima_quattordicesima (13° e 14°)
   - premio_risultato
   - previdenza_complementare
   - polizza_sanitaria
   - totale_mensile
   - totale_annuo
   - oneri_sociali
   - tfr
   - costo_azienda
   - costo_orario

IMPORTANTE:
- I documenti possono avere varianti: solo dipendenti, dipendenti+dirigenti, con DTO/TA
- Valori tipici dirigenti: ~9.000-10.000 per trattamento economico
- Se trovi DTO cerca anche TA (sono sempre in coppia)
- Mantieni i valori decimali come nel documento (punto o virgola)

Rispondi SOLO con JSON valido nel formato:
{{
  "total_tables_found": numero,
  "tables": [
    {{
      "table_id": 1,
      "type": "dipendenti|dirigenti", 
      "variant": "standard|DTO|TA",
      "description": "descrizione tabella",
      "data": [
        {{
          "livello": "A1",
          "minimi_contrattuali": 2405.0,
          "retribuzione_individuale": 58.0,
          "elemento_retribuzione_fissa": 1606.0,
          "indennita_funzione_quadri": 333.0,
          "tredicesima_quattordicesima": 410.0,
          "premio_risultato": 175.0,
          "previdenza_complementare": 111.0,
          "polizza_sanitaria": 12.50,
          "totale_mensile": 5111.0,
          "totale_annuo": 61328.0,
          "oneri_sociali": 16399.0,
          "tfr": 3985.0,
          "costo_azienda": 81712.0,
          "costo_orario": 48.0
        }}
      ]
    }}
  ],
  "validation": {{
    "accuracy": 0-100,
    "warnings": []
  }}
}}"""

    print(f" Analisi con Azure OpenAI GPT-4.1 (tentativo {attempt})...")
    
    try:
        response = client.chat.completions.create(
            model=deployment,
            messages=[
                {
                    "role": "system",
                    "content": "Sei un esperto di analisi documenti. Restituisci sempre JSON valido."
                },
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            max_completion_tokens=4000 
        )
        
        result_text = response.choices[0].message.content
        
        # Pulisci e estrai JSON
        result_text = result_text.strip()
        if result_text.startswith("```json"):
            result_text = result_text[7:]
        if result_text.startswith("```"):
            result_text = result_text[3:]
        if result_text.endswith("```"):
            result_text = result_text[:-3]
        
        # Parse JSON
        try:
            result = json.loads(result_text)
            print(f" Trovate {result.get('total_tables_found', 0)} tabelle")
            return result
        except json.JSONDecodeError as e:
            print(f" Errore parsing JSON: {e}")
            # Prova a estrarre JSON con regex
            json_match = re.search(r'\{[^{}]*(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}[^{}]*)*\}', result_text)
            if json_match:
                return json.loads(json_match.group())
            return None
            
    except Exception as e:
        print(f" Errore Azure OpenAI: {e}")
        return None

def generate_sql_from_tables(tables_data):
    """Genera SQL INSERT dalle tabelle strutturate"""
    sql_statements = []
    
    for table in tables_data.get('tables', []):
        table_type = table.get('type', 'unknown')
        variant = table.get('variant', 'standard')
        
        for record in table.get('data', []):
            # Prepara i valori
            livello = record.get('livello', '').replace("'", "''")
            
            # Se è un valore mancante, usa 0
            def get_float(key):
                val = record.get(key, 0)
                if val == '' or val is None:
                    return 0.0
                if isinstance(val, str):
                    # Gestisci virgola come separatore decimale
                    val = val.replace(',', '.')
                    try:
                        return float(val)
                    except:
                        return 0.0
                return float(val)
            
            # Costruisci SQL
            sql = f"""INSERT INTO polis.nota_metodologica_pdf 
(livello, tipo_tabella, variante, minimi_contrattuali, retribuzione_individuale, 
elemento_retribuzione_fissa, indennita_funzione_quadri, tredicesima_quattordicesima, 
premio_risultato, previdenza_complementare, polizza_sanitaria, totale_mensile, 
totale_annuo, oneri_sociali, tfr, costo_azienda, costo_orario)
VALUES (
'{livello}', '{table_type}', '{variant}',
{get_float('minimi_contrattuali')}, {get_float('retribuzione_individuale')},
{get_float('elemento_retribuzione_fissa')}, {get_float('indennita_funzione_quadri')},
{get_float('tredicesima_quattordicesima')}, {get_float('premio_risultato')},
{get_float('previdenza_complementare')}, {get_float('polizza_sanitaria')},
{get_float('totale_mensile')}, {get_float('totale_annuo')},
{get_float('oneri_sociali')}, {get_float('tfr')},
{get_float('costo_azienda')}, {get_float('costo_orario')}
);"""
            
            sql_statements.append(sql)
    
    return '\n'.join(sql_statements)

def process_single_nota_metodologica_pdf(pdf_url, api_key=None, max_attempts=3):
    """Processa un singolo PDF con retry logic"""
    
    print(f"\nProcessing: {pdf_url}")
    
    # Download PDF if URL provided
    pdf_base64 = None
    if pdf_url.startswith('http'):
        print(f" Download PDF da: {pdf_url}")
        try:
            # Try to get base64 from cache or download
            pdf_base64 = fetch_pdf_base64_cached(pdf_url) or fetch_pdf_base64(pdf_url)
            if not pdf_base64:
                print(f" Errore download PDF")
                return None
        except Exception as e:
            print(f" Errore download: {e}")
            return None
    
    # Step 1: OCR con Mistral
    print("\n STEP 1: OCR con Mistral Document AI")
    ocr_result = call_mistral_document_ai(pdf_base64=pdf_base64)
    
    if not ocr_result or not ocr_result.get('markdown'):
        print(" OCR fallito")
        return None
    
    # Step 2: Analisi con Azure OpenAI
    print("\n STEP 2: Strutturazione con Azure OpenAI GPT-4.1")
    
    best_result = None
    best_accuracy = 0
    
    for attempt in range(1, max_attempts + 1):
        analysis = analyze_with_azure_openai(ocr_result['markdown'], attempt)
        
        if analysis:
            accuracy = analysis.get('validation', {}).get('accuracy', 0)
            print(f" Accuratezza: {accuracy}%")
            
            if accuracy > best_accuracy:
                best_result = analysis
                best_accuracy = accuracy
            
            if accuracy >= 90:
                print(" Analisi ottimale!")
                break
            elif attempt < max_attempts:
                print(f" Accuratezza bassa, riprovo...")
                time.sleep(2) # Pausa tra tentativi
    
    if not best_result:
        print(" Analisi fallita")
        return None
    
    # Step 3: Genera SQL
    print("\n STEP 3: Generazione SQL")
    sql = generate_sql_from_tables(best_result)
    
    if sql:
        # Statistiche
        num_records = sql.count('INSERT INTO')
        print(f" SQL generato con {num_records} records")
        return sql
    else:
        print(" Nessun SQL generato")
        return None

