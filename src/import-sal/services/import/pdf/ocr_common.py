# ocr_common.py - Funzioni condivise per OCR BDC
# Versione pulita - SENZA dipendenze OpenAI vecchie
# v6.0 - Aggiunto supporto return_raw_text per passaggi_livello

from mysql.connector import pooling
import requests
import threading
import os
import re
import json
from functools import lru_cache
from openai import AzureOpenAI
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
import base64
import time
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

 
# ============= CONFIGURAZIONE =============

def get_db_config():
    """Legge configurazione database da environment variables"""
    return {
        "host": os.getenv('DB_HOST', 'colibri-mysql-flexible.mysql.database.azure.com'),
        "port": int(os.getenv('DB_PORT', '3306')),
        "user": os.getenv('DB_USER', 'polisammin'),
        "password": os.getenv('DB_PASSWORD', 'ComplexPass123!'),
        "database": os.getenv('DB_NAME', 'polis'),
        "pool_size": int(os.getenv('DB_POOL_SIZE', '10')),
        "pool_reset_session": os.getenv('DB_POOL_RESET', 'True').lower() == 'true'
    }

# ============= API AZURE STORAGE =============

def get_pdf_paths_from_sal(document_type="BDC", include_p7m=True, sal_name=None):
    """
    Ottiene i path dei file dinamicamente usando l'API SAL import-path.
    
    Args:
        document_type: Tipo documento (BDC, AQ, fatture, etc.)
        include_p7m: Se True, include anche file .p7m (default: False per retrocompatibilitÃ )
        sal_name: Nome del SAL (es. "1_SAL_31_12_2022") - se fornito, viene aggiunto alla query

    Returns:
        List di path file (PDF o PDF+P7M)
    """
    include_p7m = True
    try:
        # Costruisci l'URL con il parametro per il tipo di documento
        import_path_url = os.getenv('AZURE_IMPORT_PATH_URL',
                            'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/sal/import-path')
        
        # Aggiungi i parametri per il tipo di documento e il SAL
        full_url = f"{import_path_url}?type={document_type.lower()}"
        if sal_name:
            full_url += f"&sal_name={sal_name}"

        response = requests.get(full_url, timeout=300)
        response.raise_for_status()
        sal_data = response.json()
        
        if not sal_data.get('success'):
            raise Exception(f"API import-path failed for {document_type}")
        
        # Ottieni l'array dei path
        import_paths = sal_data.get('import_path', [])

        # Assicurati che sia sempre un array
        if not isinstance(import_paths, list):
            import_paths = [import_paths] if import_paths else []
        
        if not import_paths:
            raise Exception(f"No import paths found for {document_type}")
        
        print(f"Import paths for {document_type}: {import_paths}")
        
        # Nuova logica: rileva automaticamente se i path sono file o cartelle
        # 1) Raccogli direttamente i file (terminano con .pdf/.p7m)
        direct_files = []
        for p in import_paths:
            low = str(p).lower()
            if low.endswith('.pdf') or low.endswith('.p7m'):
                direct_files.append(p)

        # 2) Per i path che non sono file, trattali come cartelle e cerca i file al loro interno
        folder_paths = [p for p in import_paths if p not in direct_files]
        all_files_found = []
        if folder_paths:
            import urllib.parse
            search_in_folder_url = os.getenv('AZURE_SEARCH_IN_FOLDER_URL',
                                 'https://polis-api-storage-h0bhheb8fdczhgh7.westeurope-01.azurewebsites.net/api/search/in-folder')
            for folder in folder_paths:
                try:
                    encoded_folder = urllib.parse.quote(folder, safe='')
                    search_url = f"{search_in_folder_url}?folder={encoded_folder}&recursive=true"
                    resp = requests.get(search_url, timeout=60)
                    resp.raise_for_status()
                    data = resp.json()

                    if data.get('success') and data.get('files'):
                        all_files_found.extend(data['files'])
                        print(f"Found {len(data['files'])} files in {folder}")
                    else:
                        print(f"No files found in folder {folder}")
                except Exception as e:
                    print(f"Error searching in path {folder}: {e}")

        # 3) Unisci e filtra i risultati secondo include_p7m
        results = []
        seen = set()

        # A) Aggiungi file diretti
        for f in direct_files:
            low = f.lower()
            if (include_p7m and (low.endswith('.pdf') or low.endswith('.p7m'))) or (not include_p7m and low.endswith('.pdf')):
                if f not in seen:
                    results.append(f)
                    seen.add(f)
        if direct_files:
            print(f"Direct paths include {len(direct_files)} potential files; kept {len(results)} after filtering")

        # B) Aggiungi file trovati tramite ricerca nelle cartelle
        if all_files_found:
            if include_p7m:
                filtered_in_folders = [item['path'] for item in all_files_found
                                       if item.get('path', '').lower().endswith('.pdf') or item.get('path', '').lower().endswith('.p7m')]
                print(f"Found {len(filtered_in_folders)} PDF+P7M files in folders for {document_type}")
            else:
                filtered_in_folders = [item['path'] for item in all_files_found
                                       if item.get('path', '').lower().endswith('.pdf')]
                print(f"Found {len(filtered_in_folders)} PDF files in folders for {document_type}")
            for f in filtered_in_folders:
                if f not in seen:
                    results.append(f)
                    seen.add(f)

        if results:
            return results

        raise Exception(f"No {document_type} files found")
        
    except Exception as e:
        print(f"Error searching for {document_type} files: {e}")
        return None


@lru_cache(maxsize=100)
def fetch_pdf_base64_cached(url):
    """Cached version of fetch_pdf_base64 to avoid repeated API calls"""
    return fetch_pdf_base64(url)


def fetch_pdf_base64(url):
    """Fetch base64 PDF data from the API endpoint with timeout optimization"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        data = response.json()

        if data.get('success'):
            return data.get('base64')
        else:
            raise Exception("API request was not successful")
    except Exception as e:
        print(f"Error fetching PDF: {e}")
        return None


# ============= DATABASE =============

def fetch_mysql_query(query, db_config):
    """Fetch data from MySQL database using a SELECT query"""
    try:
        pool = get_connection_pool(db_config)
        connection = pool.get_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute(query)
        results = cursor.fetchall()

        cursor.close()
        connection.close()
        return results

    except Exception as e:
        print(f"Error fetching data: {e}")
        return None


_connection_pool = None
_pool_lock = threading.Lock()


def get_connection_pool(db_config):
    """Get or create a global connection pool"""
    global _connection_pool
    with _pool_lock:
        if _connection_pool is None:
            _connection_pool = pooling.MySQLConnectionPool(
                pool_name="polis_pool",
                pool_size=db_config['pool_size'],
                pool_reset_session=db_config['pool_reset_session'],
                host=db_config['host'],
                port=db_config['port'],
                user=db_config['user'],
                password=db_config['password'],
                database=db_config['database']
            )
        return _connection_pool


def execute_batch_queries(queries, db_config):
    """Execute multiple queries using UPSERT for duplicate handling"""
    try:
        pool = get_connection_pool(db_config)
        connection = pool.get_connection()
        cursor = connection.cursor()

        success_count = 0
        error_count = 0
        
        for query_batch in queries:
            if query_batch and query_batch.strip():
                for q in query_batch.split(";"):
                    q = q.strip()
                    if q:
                        try:
                            # âœ¨ AGGIUNGI QUESTA RIGA
                            print(f"DEBUG QUERY: {q[:500]}...") # Primi 500 caratteri
                            cursor.execute(q)
                            
                            # Se Ã¨ una SELECT, leggi i risultati
                            if q.upper().startswith('SELECT'):
                                results = cursor.fetchall()
                                print(f"SELECT query executed (found {len(results) if results else 0} rows)")
                            else:
                                print(f"Query executed successfully")
                            
                            success_count += 1
                        except Exception as qe:
                            error_count += 1
                            print(f"Query error (#{error_count}): {qe}")
                            print(f"Problematic query (first 200 chars): {q[:200]}...")

        connection.commit()
        print(f"Batch execution completed - Success: {success_count}, Errors: {error_count}")

        cursor.close()
        connection.close()
        return True

    except Exception as e:
        print(f"Fatal error executing batch queries: {e}")
        return False


def execute_single_query(query, db_config, params=None):
    """Esegue una singola query SQL (INSERT/UPDATE/DELETE) e restituisce True/False"""
    try:
        pool = get_connection_pool(db_config)
        connection = pool.get_connection()
        cursor = connection.cursor()

        if params:
            cursor.execute(query, params)
        else:
            cursor.execute(query)
        
        connection.commit()
        
        cursor.close()
        connection.close()
        return True

    except Exception as e:
        print(f"Error executing single query: {e}")
        print(f"Query (first 200 chars): {query[:200]}...")
        return False


# ============= UTILITÃ€ SQL =============

def safe_value(value, data_type="str"):
    """
    Converte valori in formato sicuro per SQL injection prevention.
    Supporta: int, decimal, date, enum (SI/NO), str (default)
    """
    original_value = value
    
    # Gestione valori NULL
    if value is None or str(value).strip() == "":
        return "NULL"

    # === INT ===
    if data_type == "int":
        try:
            cleaned = str(value).strip().replace(',', '').replace(' ', '')
            if cleaned.lower() in ['null', 'none', '']:
                return "NULL"
            # Rimuovi eventuale parte decimale (.0, .00, etc) senza passare per float
            # per evitare perdita di precisione su numeri grandi come codice_aq
            if '.' in cleaned:
                int_part, dec_part = cleaned.split('.', 1)
                # Verifica che la parte decimale sia solo zeri
                if dec_part.replace('0', '') == '':
                    cleaned = int_part
                else:
                    # Ha decimali significativi, tronca
                    cleaned = int_part
            result = str(int(cleaned))
            print(f"DEBUG safe_value() int: {original_value} -> {result}")
            return result
        except (ValueError, TypeError, OverflowError) as e:
            print(f"DEBUG safe_value() int FAILED: {original_value} -> NULL (error: {e})")
            return "NULL"

    # === DECIMAL ===
    if data_type == "decimal":
        try:
            cleaned = str(value).strip().replace(',', '.').replace(' ', '')
            if cleaned.lower() in ['null', 'none', '']:
                return "NULL"
            num_value = float(cleaned)
            if abs(num_value) > 1e15: # limite MySQL DECIMAL
                print(f"DEBUG safe_value() decimal too large: {num_value} -> NULL")
                return "NULL"
            result = str(round(num_value, 2))
            print(f"DEBUG safe_value() decimal: {original_value} -> {result}")
            return result
        except (ValueError, TypeError, OverflowError) as e:
            print(f"DEBUG safe_value() decimal FAILED: {original_value} -> NULL (error: {e})")
            return "NULL"

    # === DATE ===
    if data_type == "date":
        try:
            date_str = str(value).strip()
            if not date_str or date_str.lower() in ['null', 'none', '']:
                return "NULL"
            # Accetta solo formato YYYY-MM-DD
            if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
                print(f"DEBUG safe_value() date: {original_value} -> '{date_str}'")
                return f"'{date_str}'"
            print(f"DEBUG safe_value() date FAILED: {original_value} -> NULL")
            return "NULL"
        except Exception as e:
            print(f"DEBUG safe_value() date ERROR: {original_value} -> NULL (error: {e})")
            return "NULL"

    # === ENUM (per campi SI/NO) ===
    if data_type == "enum":
        try:
            enum_str = str(value).strip().upper()
            if enum_str in ['SI', 'SÃŒ', 'YES', 'Y', '1', 'TRUE']:
                return "'SI'"
            elif enum_str in ['NO', 'N', '0', 'FALSE']:
                return "'NO'"
            else:
                return "NULL"
        except Exception as e:
            print(f"DEBUG safe_value() enum ERROR: {original_value} -> NULL (error: {e})")
            return "NULL"

    # === STRING (default) ===
    text = str(value).strip()
    safe_chars = []
    
    # Caratteri permessi (estesi per supportare email, IBAN, indirizzi)
    allowed_special_chars = [' ', '.', ',', '-', '/', '&', '@', '(', ')', ':', '_', '#', '+', '=']
    
    for char in text:
        if char.isalnum() or char in allowed_special_chars:
            safe_chars.append(char)
        elif char == "'":
            safe_chars.append("''") # SQL escape
        elif char == "\\":
            safe_chars.append("\\\\") # Escape backslash
        else:
            safe_chars.append(' ') # Sostituisci caratteri non validi con spazio
    
    result_text = ''.join(safe_chars)
    print(f"DEBUG safe_value() string: {original_value[:50]} -> {result_text[:50]}")
    return f"'{result_text}'"


def process_single_pdf(pdf_url, api_key, text_input, json_to_queries_func):
    """Generic function to process a single PDF and generate SQL queries"""
    try:
        json_response = pdf_to_json_via_gpt_vision(pdf_url, api_key, text_input)

        if json_response and not json_response.startswith("Error:"):
            queries = json_to_queries_func(json_response, pdf_url)
            
            if queries:
                sql_string = "; ".join(queries) if isinstance(queries, list) else queries
                return sql_string
            else:
                print(f"Query generation failed for {pdf_url}")
                return None
        else:
            print(f"OCR failed for {pdf_url}: {json_response}")
            return None
            
    except Exception as e:
        print(f"Error processing PDF {pdf_url}: {e}")
        return None


# ============= OCR FUNCTIONS =============

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    retry=retry_if_exception_type((requests.exceptions.RequestException, Exception)),
    reraise=True
)
def pdf_to_json_via_gpt_vision(pdf_url, api_key, text_input, return_raw_text=False):
    """
    Process PDF using Azure Document Intelligence + GPT-4 with retry logic
    
    Args:
        pdf_url: URL del PDF da processare
        api_key: Deprecato (non piÃ¹ usato, mantenuto per backwards compatibility)
        text_input: Prompt/istruzioni per GPT-4
        return_raw_text: Se True, restituisce tupla (json_result, raw_text) invece di solo json_result
    
    Returns:
        Se return_raw_text=False: json_string (comportamento default, backwards compatible)
        Se return_raw_text=True: (json_string, raw_text_string)
    
    Example:
        # Vecchio modo (backwards compatible - usato da fatture_ocr):
        json_only = pdf_to_json_via_gpt_vision(url, None, prompt)
        
        # Nuovo modo (con raw_text - usato da passaggi_livello):
        json_result, raw_text = pdf_to_json_via_gpt_vision(url, None, prompt, return_raw_text=True)
    """
    print(f"pdf_to_json_via_gpt_vision called with return_raw_text={return_raw_text}")
    result = _pdf_to_json_via_gpt_vision_single_attempt(pdf_url, text_input, return_raw_text)
    return result


def _pdf_to_json_via_gpt_vision_single_attempt(pdf_url, text_input="", return_raw_text=False):
    """
    Single attempt to process PDF - called by retry wrapper
    
    Args:
        pdf_url: URL del PDF
        text_input: Prompt per GPT
        return_raw_text: Se True, restituisce (json, raw_text)
    
    Returns:
        Se return_raw_text=False: json_string
        Se return_raw_text=True: (json_string, raw_text_string)
    """
    
    # Rate limiting
    time.sleep(1)
    
    try:
        # Step 1: Get PDF base64 data
        b64_pdf = fetch_pdf_base64_cached(pdf_url)
        if not b64_pdf:
            b64_pdf = fetch_pdf_base64(pdf_url)
            if not b64_pdf:
                error_msg = "Error: Could not fetch PDF data"
                if return_raw_text:
                    return (error_msg, "")
                return error_msg

        # Step 2: Use Azure Document Intelligence to extract text
        doc_intelligence_endpoint = os.getenv('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT')
        doc_intelligence_key = os.getenv('AZURE_DOCUMENT_INTELLIGENCE_KEY')

        if not doc_intelligence_endpoint or not doc_intelligence_key:
            print("Warning: Azure Document Intelligence credentials not found, falling back to direct vision API")
            # Fallback to original method
            result = _pdf_to_json_direct_vision(b64_pdf, text_input)
            if return_raw_text:
                return (result, "") # Nessun raw_text disponibile nel fallback
            return result

        # Initialize Document Intelligence client
        document_analysis_client = DocumentAnalysisClient(
            endpoint=doc_intelligence_endpoint,
            credential=AzureKeyCredential(doc_intelligence_key)
        )

        # Convert base64 to bytes
        pdf_bytes = base64.b64decode(b64_pdf)

        # Analyze document with prebuilt-layout model (best for tables and structured content)
        print("Analyzing document with Azure Document Intelligence...")
        poller = document_analysis_client.begin_analyze_document(
            "prebuilt-layout", document=pdf_bytes
        )
        result = poller.result()

        # Extract text content with layout preservation
        extracted_text = []

        # Extract tables
        if result.tables:
            extracted_text.append("\n=== TABLES FOUND ===\n")
            for table_idx, table in enumerate(result.tables):
                extracted_text.append(f"\nTable {table_idx + 1} (Rows: {table.row_count}, Cols: {table.column_count}):\n")
                for cell in table.cells:
                    extracted_text.append(
                        f"[Row {cell.row_index}, Col {cell.column_index}]: {cell.content}\n"
                    )

        # Extract key-value pairs
        if result.key_value_pairs:
            extracted_text.append("\n=== KEY-VALUE PAIRS ===\n")
            for kv in result.key_value_pairs:
                key = kv.key.content if kv.key else ""
                value = kv.value.content if kv.value else ""
                if key:
                    extracted_text.append(f"{key}: {value}\n")

        # Extract all text content
        extracted_text.append("\n=== FULL TEXT CONTENT ===\n")
        for page in result.pages:
            for line in page.lines:
                extracted_text.append(f"{line.content}\n")

        document_intelligence_output = "".join(extracted_text)
        
        # SALVA IL RAW TEXT per eventuale restituzione
        raw_text_extracted = document_intelligence_output

        print(f"Document Intelligence extracted {len(document_intelligence_output)} characters")

        # Step 3: Send extracted text to GPT-4 for formatting and normalization
        client = AzureOpenAI(
            api_key=os.getenv('AZURE_OPENAI_KEY'),
            api_version="2024-02-15-preview",
            azure_endpoint=os.getenv('AZURE_OPENAI_ENDPOINT')
        )

        # Combine the original instructions with the extracted content
        formatted_prompt = f"""{text_input}

        Here is the content extracted from the document by Azure Document Intelligence:

        {document_intelligence_output}

        Please analyze this extracted content and format it according to the instructions above.
        IMPORTANT: Return ONLY valid JSON. Ensure all strings are properly closed with quotes."""

        print("Sending extracted content to GPT-4 for formatting...")

        # RETRY LOOP for malformed JSON
        max_json_retries = 2
        for json_attempt in range(max_json_retries + 1):
            try:
                response = client.chat.completions.create(
                    model=os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4o'),
                    messages=[
                        {
                            "role": "user",
                            "content": formatted_prompt
                        }
                    ],
                    max_tokens=4000,
                    temperature=0.1
                )
                
                json_content = response.choices[0].message.content
                
                # Validate JSON immediately
                clean_json = json_content.replace("```json", "").replace("```", "").strip()
                json.loads(clean_json) # If fails, raises exception
                
                print(f"Valid JSON received (attempt {json_attempt + 1})")
                
                # RETURN BASATO SU return_raw_text
                if return_raw_text:
                    return (json_content, raw_text_extracted)
                else:
                    return json_content
                
            except json.JSONDecodeError as e:
                print(f"Invalid JSON on attempt {json_attempt + 1}: {e}")
                if json_attempt < max_json_retries:
                    print(f"Retrying with stricter instructions...")
                    formatted_prompt += "\n\nWARNING: Previous response had malformed JSON. Please ensure ALL strings are properly quoted and closed."
                else:
                    print(f"Failed after {max_json_retries + 1} attempts, returning error")
                    error_json = '{"error": "invalid_json_after_retries"}'
                    if return_raw_text:
                        return (error_json, raw_text_extracted)
                    return error_json

        # Fallback finale (non dovrebbe mai arrivarci)
        final_result = response.choices[0].message.content
        if return_raw_text:
            return (final_result, raw_text_extracted)
        return final_result
        
        
    except Exception as e:
        print(f"Error processing PDF via Azure Document Intelligence + GPT-4: {e}")
        # Fallback rimosso: data:application/pdf non supportato da Vision API
        # Il PDF Ã¨ giÃ  stato processato da Document Intelligence
        print("Skipping fallback - PDF already processed by Document Intelligence")
        
        error_msg = ""
        if return_raw_text:
            return (error_msg, "")
        return error_msg


def _pdf_to_json_direct_vision(b64_pdf, text_input):
    """Fallback method: Direct PDF vision analysis (original method)"""
    try:
        client = AzureOpenAI(
            api_key=os.getenv('AZURE_OPENAI_KEY'),
            api_version="2024-02-15-preview",
            azure_endpoint=os.getenv('AZURE_OPENAI_ENDPOINT')
        )

        response = client.chat.completions.create(
            model=os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4o'),
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": text_input
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:application/pdf;base64,{b64_pdf}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=4000
        )

        return response.choices[0].message.content
    except Exception as e:
        print(f"Error in fallback vision API: {e}")
        return ""

# ============================================================================
# LOG AUDIT per fatture skipped
# ============================================================================

def log_skipped_fattura(filename: str, reason: str, log_path: str = "skipped_fatture.log"):
    """Log fatture skippate con timestamp"""
    try:
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_path, 'a', encoding='utf-8') as f:
            f.write(f"{timestamp}|{filename}|{reason}\n")
    except Exception as e:
        print(f"Warning: Could not write to skipped log: {e}")