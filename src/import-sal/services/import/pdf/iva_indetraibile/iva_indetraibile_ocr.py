# iva_indetraibile_ocr.py - OCR specifico per IVA Indetraibile
# Version 3.0 - File logica separato dal wrapper

import json
import time
import os
import io
import sys
import base64
import re
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
import PyPDF2
from openai import AzureOpenAI
from ocr_common import *
from dotenv import load_dotenv

load_dotenv()

def extract_pdf_with_azure(pdf_url):
    """Extract tables from PDF using Azure Document Intelligence"""
    try:
        endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
        api_key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
        
        if not endpoint or not api_key:
            raise Exception("Azure credentials not found")
        
        print(" Connecting to Azure Document Intelligence...")
        client = DocumentAnalysisClient(endpoint=endpoint, credential=AzureKeyCredential(api_key))
        
        # Fetch PDF
        b64_pdf = fetch_pdf_base64_cached(pdf_url) or fetch_pdf_base64(pdf_url)
        if not b64_pdf:
            return None
            
        pdf_bytes = base64.b64decode(b64_pdf)
        pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        total_pages = len(pdf_reader.pages)
        
        print(f" PDF has {total_pages} pages. Analyzing...")
        
        all_tables = []
        table_counter = 1
        
        for page_num in range(total_pages):
            print(f" Page {page_num + 1}/{total_pages}...", end="")
            
            # Extract single page
            pdf_reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            pdf_writer = PyPDF2.PdfWriter()
            pdf_writer.add_page(pdf_reader.pages[page_num])
            
            page_buffer = io.BytesIO()
            pdf_writer.write(page_buffer)
            page_buffer.seek(0)
            
            # Analyze page
            poller = client.begin_analyze_document("prebuilt-layout", document=page_buffer.read())
            result = poller.result()
            
            print(f" {len(result.tables)} tables found")
            
            for table in result.tables:
                # Build table matrix
                matrix = [['' for _ in range(table.column_count)] for _ in range(table.row_count)]
                for cell in table.cells:
                    matrix[cell.row_index][cell.column_index] = cell.content
                
                all_tables.append({
                    "tabella_num": table_counter,
                    "pagina": page_num + 1,
                    "righe": matrix
                })
                table_counter += 1
        
        print(f" Total tables found: {len(all_tables)}")
        return all_tables
        
    except Exception as e:
        print(f"Error: {e}")
        return None

def extract_periods_from_header(header_row):
    """Extract periods from header row"""
    periodi = []
    italian_months = {
        'gennaio': '01', 'febbraio': '02', 'marzo': '03', 'aprile': '04',
        'maggio': '05', 'giugno': '06', 'luglio': '07', 'agosto': '08',
        'settembre': '09', 'ottobre': '10', 'novembre': '11', 'dicembre': '12'
    }
    
    for idx, cell in enumerate(header_row):
        if not cell:
            continue
        
        # Pattern: Dal DD mese YYYY al DD mese YYYY
        match = re.search(r'[Dd]al\s+(\d{1,2})\s+(\w+)\s+(\d{4})\s+al?\s+(\d{1,2})\s+(\w+)\s+(\d{4})', str(cell))
        if match:
            d1, m1, y1, d2, m2, y2 = match.groups()
            month1 = italian_months.get(m1.lower(), '01')
            month2 = italian_months.get(m2.lower(), '01')
            
            periodi.append({
                'data_inizio': f"{y1}-{month1}-{d1.zfill(2)}",
                'data_fine': f"{y2}-{month2}-{d2.zfill(2)}"
            })
    
    return periodi

def parse_decimal(value):
    """Parse decimal value handling special characters"""
    if not value:
        return 0.0
    
    val_str = str(value).strip()
    val_str = re.sub(r'[;:]+$', '', val_str) # Remove trailing : or ;
    val_str = val_str.replace(',', '.')
    val_str = val_str.replace(' ', '')
    
    try:
        return float(val_str)
    except:
        # Extract numbers only
        cleaned = re.findall(r'[\d.]+', val_str)
        if cleaned:
            try:
                return float(cleaned[0])
            except:
                pass
        return 0.0

def parse_tables_to_records(azure_tables, skip_empty=True):
    """Parse Azure tables into structured records"""
    parsed_records = []
    
    for table_idx, table in enumerate(azure_tables):
        rows = table.get('righe', [])
        if len(rows) < 3:
            continue
        
        periodi = extract_periods_from_header(rows[0])
        if not periodi:
            print(f" Table {table_idx + 1}: no periods found")
            continue
        
        print(f" Table {table_idx + 1}: {len(periodi)} periods found")
        
        for row_idx in range(2, len(rows)):
            row = rows[row_idx]
            
            if not row[0] or not any(c.isalpha() for c in str(row[0])):
                continue
            
            classe = str(row[0]).strip()
            
            for periodo_idx, periodo in enumerate(periodi):
                col_start = 1 + (periodo_idx * 5)
                
                if col_start + 4 >= len(row):
                    continue
                
                vals = [parse_decimal(row[col_start + i] if col_start + i < len(row) else None) for i in range(5)]
                
                # Skip se TUTTE e 5 le colonne sono a zero
                if all(v == 0 for v in vals):
                    continue # Skip record completamente vuoto
                        
                parsed_records.append({
                    'classe_cespiti': classe,
                    'attivita_finanziaria': vals[0],
                    'attivita_universale': vals[1],
                    'attivita_residuale': vals[2],
                    'perc_indetraibilita_iva': vals[3],
                    'aliquota_iva_indetraibile': vals[4],
                    'periodo_inizio': periodo['data_inizio'],
                    'periodo_fine': periodo['data_fine']
                })
    
    return parsed_records

def validate_with_claude(azure_tables, parsed_data, existing_db, attempt=1):
    """Validate parsed data with Azure OpenAI"""
    # Initialize Azure OpenAI client
    client = AzureOpenAI(
        api_version="2024-12-01-preview",
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        api_key=os.getenv("AZURE_OPENAI_KEY")
    )
    
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o")
    
    # Prepare summary
    classi = list(set(r['classe_cespiti'] for r in parsed_data))
    periodi = list(set((r['periodo_inizio'], r['periodo_fine']) for r in parsed_data))
    
    prompt = f"""
Validazione dati IVA (Tentativo {attempt}/3).

IMPORTANTE: NON tutte le classi hanno dati per tutti i periodi (alcune celle sono vuote).
Verifica che ogni dato parsato corrisponda alle tabelle Azure.

## Tabelle Azure (prime 2):
{json.dumps(azure_tables[:2], ensure_ascii=False, indent=2)}

## Dati parsati: {len(parsed_data)} record
Classi: {len(classi)} uniche
Periodi: {len(periodi)} unici

## Verifica:
1. Conta celle effettivamente popolate (non vuote) nelle tabelle
2. Confronta con i record parsati
3. Identifica dati mancanti o errati

Rispondi SOLO con JSON:
{{
    "validazione_ok": true/false,
    "statistiche": {{
        "celle_popolate_nelle_tabelle": numero,
        "record_parsati": {len(parsed_data)},
        "accuratezza_percentuale": numero
    }},
    "problemi": [
        {{"tipo": "...", "descrizione": "...", "gravita": "alta/media/bassa"}}
    ],
    "suggerimenti": {{"correzione": "..."}}
}}
"""
    
    print(f" Validating with Azure OpenAI (attempt {attempt})...")
    
    response = client.chat.completions.create(
        model=deployment,
        messages=[
            {"role": "system", "content": "You are a data validation expert. Respond only with valid JSON."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=2000,
        temperature=0.1
    )
    
    # Parse JSON response
    try:
        response_text = response.choices[0].message.content
        json_match = re.search(r'\{[\s\S]*\}', response_text)
        if json_match:
            return json.loads(json_match.group())
    except:
        pass
    return None

def generate_sql_statements(records, classe_cespiti_data):
    """Generate SQL INSERT statements with FK mapping"""
    sql_lines = []
    
    fk_map = {}
    for row in classe_cespiti_data:
        if 'descrizione' in row and 'id_classe_cespiti' in row:
            # Normalizza rimuovendo spazi extra
            key = row['descrizione'].upper().strip().replace(' ', '')
            fk_map[key] = row['id_classe_cespiti']
    
    print(f" FK mapping available for {len(fk_map)} classes")
    
    for record in records:
        # Normalizza anche la classe del record
        classe_normalized = record['classe_cespiti'].upper().strip().replace(' ', '')
        fk = fk_map.get(classe_normalized, 'NULL')
        
        if fk == 'NULL':
            print(f" No FK found for: {record['classe_cespiti']}")
        
        classe_escaped = record['classe_cespiti'].replace("'", "\\'")
        
        sql = f"""INSERT INTO polis.indetraibilita_iva_pdf 
(classe_cespiti_fk, classe_cespiti, attivita_finanziaria, attivita_universale, 
attivita_residuale, perc_indetraibilita_iva, aliquota_iva_indetraibile, 
periodo_inizio, periodo_fine)
VALUES ({fk}, '{classe_escaped}', {record['attivita_finanziaria']:.2f}, 
{record['attivita_universale']:.2f}, {record['attivita_residuale']:.2f}, 
{record['perc_indetraibilita_iva']:.2f}, {record['aliquota_iva_indetraibile']:.2f}, 
'{record['periodo_inizio']}', '{record['periodo_fine']}');"""
        
        sql_lines.append(sql)
    
    return '\n'.join(sql_lines)

def process_pdf_with_retry(pdf_url, max_attempts=3):
    """Process PDF with retry logic"""
    print(f"\nProcessing: {pdf_url}")
    
    # Extract tables once
    azure_tables = extract_pdf_with_azure(pdf_url)
    if not azure_tables:
        return None
    
    # Get DB data
    db_config = get_db_config()
    existing_data = fetch_mysql_query("SELECT * FROM indetraibilita_iva_pdf LIMIT 10", db_config)
    classe_cespiti = fetch_mysql_query("SELECT * FROM classe_cespiti", db_config)
    
    best_result = None
    best_accuracy = 0
    
    # Try multiple times with different strategies
    for attempt in range(1, max_attempts + 1):
        print(f"\n Attempt {attempt}/{max_attempts}")
        
        # Vary parsing strategy
        if attempt == 1:
            parsed = parse_tables_to_records(azure_tables, skip_empty=True)
        elif attempt == 2:
            parsed = parse_tables_to_records(azure_tables, skip_empty=False)
        else:
            # Remove duplicates
            parsed = parse_tables_to_records(azure_tables, skip_empty=True)
            seen = set()
            filtered = []
            for r in parsed:
                key = f"{r['classe_cespiti']}|{r['periodo_inizio']}|{r['periodo_fine']}"
                if key not in seen:
                    seen.add(key)
                    filtered.append(r)
            parsed = filtered
        
        print(f" Parsed {len(parsed)} records")
        
        # Validate
        validation = validate_with_claude(azure_tables, parsed, existing_data, attempt)
        
        if validation:
            accuracy = validation.get('statistiche', {}).get('accuratezza_percentuale', 0)
            print(f" Accuracy: {accuracy}%")
            
            if accuracy > best_accuracy:
                best_result = parsed
                best_accuracy = accuracy
            
            if accuracy >= 95:
                print(" Validation successful!")
                break
    
    # Generate SQL with best result
    if best_result and best_accuracy > 30:
        print(f"\n Using best result with {best_accuracy}% accuracy")
        return generate_sql_statements(best_result, classe_cespiti)
    
    return None