#!/usr/bin/env python3
"""
AQ Missing Import Service - Container Apps version
Trova gli Accordi Quadro mancanti nel DB, cerca i PDF tramite API Storage e li importa
"""
from azure_clients import get_blob_service_client, get_servicebus_client, describe_auth_mode
import os
import sys
import json
import logging
import time
import urllib.parse
import base64
import requests
from datetime import datetime
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusReceiveMode
from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from openai import AzureOpenAI

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# Path setup
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from ocr_common import fetch_mysql_query, execute_batch_queries, get_db_config, pdf_to_json_via_gpt_vision
import aq_ocr

# ============================================================================
# CONFIGURATION
# ============================================================================

# API Storage per la collezione (diverso dall'API principale)
STORAGE_API_BASE_URL = os.getenv(
    'STORAGE_COLL_API_URL',
    'https://polis-prod-api-storage.azurewebsites.net'
)

def get_api_config():
    """Configurazione API per il servizio AQ mancanti"""
    return {
        "search_url": f"{STORAGE_API_BASE_URL}/api/search/unified",
        "download_url": f"{STORAGE_API_BASE_URL}/api/download",
        "openai_api_key": os.getenv('OPENAI_API_KEY', '')
    }


# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

def get_missing_aq_codes(db_config, limit=None):
    """
    Estrae i codici AQ presenti in accordi_quadro ma mancanti in pdf_aq

    Returns:
        List[int]: Lista di codici AQ mancanti
    """
    query = """
    SELECT DISTINCT codice FROM accordi_quadro
    WHERE codice NOT IN (
        SELECT codice_aq
        FROM pdf_aq
        WHERE codice_aq IS NOT NULL
    )
    """

    if limit:
        query += f" LIMIT {limit}"

    try:
        logger.info("Esecuzione query per trovare AQ mancanti...")
        results = fetch_mysql_query(query, db_config)

        if results:
            codes = [row['codice'] for row in results]
            logger.info(f"Trovati {len(codes)} codici AQ mancanti")
            return codes
        else:
            logger.info("Nessun codice AQ mancante trovato")
            return []

    except Exception as e:
        logger.error(f"Errore query AQ mancanti: {e}")
        return []


# ============================================================================
# API STORAGE FUNCTIONS
# ============================================================================

def search_aq_pdf(codice_aq, api_config):
    """
    Cerca un PDF AQ tramite l'API search/unified

    Args:
        codice_aq: Codice AQ da cercare (es. 4490013071)
        api_config: Configurazione API

    Returns:
        List[dict]: Lista di risultati con path dei file trovati
    """
    try:
        search_url = api_config['search_url']
        params = {
            'q': str(codice_aq),
            'search_type': 'both',
            'root_only': 'false',
            'exact': 'true',
            'limit': 100
        }

        logger.info(f"Ricerca PDF per AQ {codice_aq}...")
        response = requests.get(search_url, params=params, timeout=60)
        response.raise_for_status()

        data = response.json()

        if data.get('success') and data.get('results'):
            # Filtra solo i file PDF
            pdf_results = [
                r for r in data['results']
                if r.get('type') == 'file' and
                (r.get('name', '').lower().endswith('.pdf') or
                 r.get('name', '').lower().endswith('.pdf.pdf'))
            ]

            logger.info(f"AQ {codice_aq}: trovati {len(pdf_results)} file PDF")
            return pdf_results
        else:
            logger.warning(f"AQ {codice_aq}: nessun risultato dalla ricerca")
            return []

    except requests.exceptions.RequestException as e:
        logger.error(f"Errore ricerca AQ {codice_aq}: {e}")
        return []
    except Exception as e:
        logger.error(f"Errore imprevisto ricerca AQ {codice_aq}: {e}")
        return []


def download_pdf_base64(file_path, api_config):
    """
    Scarica un PDF e restituisce il contenuto in base64

    Args:
        file_path: Path del file nello storage
        api_config: Configurazione API

    Returns:
        str: Contenuto PDF in base64, o None se errore
    """
    try:
        # URL encode del path
        encoded_path = urllib.parse.quote(file_path, safe='')
        download_url = f"{api_config['download_url']}/{encoded_path}"

        logger.info(f"Download PDF: {file_path[:80]}...")
        response = requests.get(download_url, timeout=120)
        response.raise_for_status()

        # Verifica se la risposta è già base64 (JSON) o binaria
        content_type = response.headers.get('content-type', '')

        if 'application/json' in content_type:
            # La risposta è JSON con base64
            data = response.json()
            if data.get('success') and data.get('base64'):
                return data['base64']
            else:
                logger.error(f"Risposta JSON non valida per {file_path}")
                return None
        else:
            # La risposta è binaria, converti in base64
            import base64
            pdf_base64 = base64.b64encode(response.content).decode('utf-8')
            return pdf_base64

    except requests.exceptions.RequestException as e:
        logger.error(f"Errore download PDF {file_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Errore imprevisto download PDF {file_path}: {e}")
        return None


# ============================================================================
# PDF PROCESSING
# ============================================================================

def process_aq_pdf(file_path, pdf_base64, api_config, codice_aq_fallback=None):
    """
    Processa un PDF AQ usando OCR e genera le query SQL

    Args:
        file_path: Path del file (per logging e per estrarre codice AQ dal nome)
        pdf_base64: Contenuto PDF in base64
        api_config: Configurazione API
        codice_aq_fallback: Codice AQ da usare se non trovato nel documento

    Returns:
        List[str]: Lista di query SQL, o None se errore
    """
    try:
        # Estrai il nome del file dal path
        file_name = os.path.basename(file_path) if file_path else None

        logger.info(f"Elaborazione OCR per: {file_path[:60]}...")
        if codice_aq_fallback:
            logger.info(f"Codice AQ fallback disponibile: {codice_aq_fallback}")
        if file_name:
            logger.info(f"Nome file per estrazione AQ: {file_name}")

        # Ottieni il prompt per l'estrazione dati AQ, passando il nome del file
        text_prompt = aq_ocr.get_aq_json_prompt(file_name=file_name)

        # Crea un URL fittizio per compatibilità con la funzione esistente
        # La funzione pdf_to_json_via_gpt_vision supporta anche base64 diretto
        # Ma per semplicità usiamo un approccio diverso

        # Usa Azure Document Intelligence + GPT-4 per processare il PDF
        json_response = process_pdf_base64_with_ocr(pdf_base64, text_prompt)

        if json_response and not json_response.startswith("Error:"):
            # Converti JSON in query SQL, passando il codice_aq fallback
            queries = aq_ocr.json_to_aq_queries(json_response, file_path, codice_aq_fallback)

            if queries:
                logger.info(f"Generate {len(queries)} query SQL per {file_path[:50]}...")
                return queries
            else:
                logger.warning(f"Nessuna query generata per {file_path}")
                return None
        else:
            logger.warning(f"OCR fallito per {file_path}: {json_response[:100] if json_response else 'None'}")
            return None

    except Exception as e:
        logger.error(f"Errore processing PDF {file_path}: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return None


def process_pdf_base64_with_ocr(pdf_base64, text_prompt):
    """
    Processa un PDF (in base64) con Azure Document Intelligence + GPT-4

    Args:
        pdf_base64: Contenuto PDF in base64
        text_prompt: Prompt per l'estrazione dati

    Returns:
        str: JSON response, o None se errore
    """
    try:
        # Step 1: Estrai testo con Azure Document Intelligence
        doc_intelligence_endpoint = os.getenv('AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT')
        doc_intelligence_key = os.getenv('AZURE_DOCUMENT_INTELLIGENCE_KEY')

        if not doc_intelligence_endpoint or not doc_intelligence_key:
            logger.error("Azure Document Intelligence credentials mancanti")
            return "Error: Missing Document Intelligence credentials"

        # Inizializza client
        document_client = DocumentAnalysisClient(
            endpoint=doc_intelligence_endpoint,
            credential=AzureKeyCredential(doc_intelligence_key)
        )

        # Converti base64 in bytes
        pdf_bytes = base64.b64decode(pdf_base64)

        # Analizza documento
        logger.info("Analisi documento con Azure Document Intelligence...")
        poller = document_client.begin_analyze_document(
            "prebuilt-layout", document=pdf_bytes
        )
        result = poller.result()

        # Estrai testo
        extracted_text = []

        # Estrai tabelle
        if result.tables:
            extracted_text.append("\n=== TABLES ===\n")
            for table_idx, table in enumerate(result.tables):
                extracted_text.append(f"\nTable {table_idx + 1}:\n")
                for cell in table.cells:
                    extracted_text.append(
                        f"[Row {cell.row_index}, Col {cell.column_index}]: {cell.content}\n"
                    )

        # Estrai paragrafi
        if result.paragraphs:
            extracted_text.append("\n=== TEXT CONTENT ===\n")
            for para in result.paragraphs:
                extracted_text.append(para.content + "\n")

        full_text = "".join(extracted_text)

        if not full_text.strip():
            logger.warning("Nessun testo estratto dal PDF")
            return "Error: No text extracted"

        logger.info(f"Estratti {len(full_text)} caratteri dal PDF")

        # Step 2: Usa GPT-4 per estrarre dati strutturati
        azure_openai_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
        azure_openai_key = os.getenv('AZURE_OPENAI_KEY')
        azure_openai_deployment = os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4o')

        if not azure_openai_endpoint or not azure_openai_key:
            # Fallback a Mistral se Azure OpenAI non configurato
            return process_with_mistral(full_text, text_prompt)

        client = AzureOpenAI(
            azure_endpoint=azure_openai_endpoint,
            api_key=azure_openai_key,
            api_version="2024-02-15-preview"
        )

        messages = [
            {"role": "system", "content": "Sei un assistente esperto nell'estrazione di dati da documenti PDF di Accordi Quadro. Restituisci SOLO JSON valido."},
            {"role": "user", "content": f"{text_prompt}\n\n=== CONTENUTO DOCUMENTO ===\n{full_text}"}
        ]

        logger.info("Chiamata GPT-4 per estrazione dati...")
        response = client.chat.completions.create(
            model=azure_openai_deployment,
            messages=messages,
            temperature=0.1,
            max_tokens=4000
        )

        json_result = response.choices[0].message.content
        logger.info(f"Risposta GPT-4 ricevuta: {len(json_result)} caratteri")

        return json_result

    except Exception as e:
        logger.error(f"Errore OCR: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return f"Error: {str(e)}"


def process_with_mistral(text_content, text_prompt):
    """
    Fallback: processa con Mistral AI se Azure OpenAI non disponibile
    """
    try:
        mistral_api_key = os.getenv('MISTRAL_API_KEY')
        mistral_endpoint = os.getenv('MISTRAL_ENDPOINT', 'https://api.mistral.ai/v1')

        if not mistral_api_key:
            return "Error: No AI service configured (Azure OpenAI or Mistral)"

        headers = {
            "Authorization": f"Bearer {mistral_api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "mistral-large-latest",
            "messages": [
                {"role": "system", "content": "Sei un assistente esperto nell'estrazione di dati da documenti. Restituisci SOLO JSON valido."},
                {"role": "user", "content": f"{text_prompt}\n\n=== CONTENUTO DOCUMENTO ===\n{text_content}"}
            ],
            "temperature": 0.1,
            "max_tokens": 4000
        }

        logger.info("Chiamata Mistral AI per estrazione dati...")
        response = requests.post(
            f"{mistral_endpoint}/chat/completions",
            headers=headers,
            json=payload,
            timeout=120
        )
        response.raise_for_status()

        data = response.json()
        json_result = data['choices'][0]['message']['content']
        logger.info(f"Risposta Mistral ricevuta: {len(json_result)} caratteri")

        return json_result

    except Exception as e:
        logger.error(f"Errore Mistral: {e}")
        return f"Error: {str(e)}"


# ============================================================================
# MAIN PROCESSING LOGIC
# ============================================================================

def process_single_aq(codice_aq, db_config, api_config):
    """
    Processa un singolo codice AQ: cerca, scarica e importa

    Args:
        codice_aq: Codice AQ da processare
        db_config: Configurazione database
        api_config: Configurazione API

    Returns:
        dict: Risultato con status e dettagli
    """
    result = {
        'codice_aq': codice_aq,
        'status': 'pending',
        'files_found': 0,
        'files_processed': 0,
        'queries_generated': 0,
        'error': None
    }

    try:
        # Step 1: Cerca PDF per questo AQ
        pdf_results = search_aq_pdf(codice_aq, api_config)
        result['files_found'] = len(pdf_results)

        if not pdf_results:
            result['status'] = 'skipped'
            result['error'] = 'Nessun PDF trovato'
            logger.warning(f"AQ {codice_aq}: saltato - nessun PDF trovato")
            return result

        all_queries = []

        # Step 2: Per ogni file trovato
        for pdf_info in pdf_results:
            file_path = pdf_info.get('path', '')

            if not file_path:
                continue

            # Step 3: Scarica il PDF
            pdf_base64 = download_pdf_base64(file_path, api_config)

            if not pdf_base64:
                logger.warning(f"AQ {codice_aq}: impossibile scaricare {file_path}")
                continue

            # Step 4: Processa con OCR, passando il codice_aq come fallback
            queries = process_aq_pdf(file_path, pdf_base64, api_config, codice_aq_fallback=codice_aq)

            if queries:
                all_queries.extend(queries)
                result['files_processed'] += 1

            # Rate limiting tra file
            time.sleep(1)

        result['queries_generated'] = len(all_queries)

        # Step 5: Esegui le query nel database
        if all_queries:
            logger.info(f"AQ {codice_aq}: esecuzione {len(all_queries)} query...")

            # Converti lista di query in formato per execute_batch_queries
            sql_batch = "; ".join(all_queries)
            success = execute_batch_queries([sql_batch], db_config)

            if success:
                result['status'] = 'success'
                logger.info(f"AQ {codice_aq}: importato con successo")
            else:
                result['status'] = 'failed'
                result['error'] = 'Errore esecuzione query database'
        else:
            result['status'] = 'failed'
            result['error'] = 'Nessuna query generata dai PDF'

        return result

    except Exception as e:
        result['status'] = 'error'
        result['error'] = str(e)
        logger.error(f"AQ {codice_aq}: errore - {e}")
        return result


def run_missing_aq_import(batch_size=50, max_workers=4):
    """
    Esegue l'import di tutti gli AQ mancanti

    Args:
        batch_size: Numero massimo di AQ da processare per esecuzione
        max_workers: Numero di worker paralleli

    Returns:
        dict: Riepilogo dell'esecuzione
    """
    logger.info("="*60)
    logger.info("AQ MISSING IMPORT SERVICE - START")
    logger.info("="*60)

    summary = {
        'start_time': datetime.now().isoformat(),
        'total_missing': 0,
        'processed': 0,
        'success': 0,
        'skipped': 0,
        'failed': 0,
        'errors': []
    }

    try:
        # Configurazioni
        db_config = get_db_config()
        api_config = get_api_config()

        # Test connessione DB
        logger.info("Test connessione database...")
        test_result = fetch_mysql_query("SELECT 1 as test", db_config)
        if not test_result:
            raise Exception("Connessione database fallita")
        logger.info("Connessione database OK")

        # Ottieni AQ mancanti
        missing_codes = get_missing_aq_codes(db_config, limit=batch_size)
        summary['total_missing'] = len(missing_codes)

        if not missing_codes:
            logger.info("Nessun AQ mancante da importare")
            summary['end_time'] = datetime.now().isoformat()
            return summary

        logger.info(f"Trovati {len(missing_codes)} AQ mancanti da processare")
        logger.info(f"Primi 10 codici: {missing_codes[:10]}")

        # Processa gli AQ
        # Nota: processamento sequenziale per evitare rate limiting API
        for i, codice_aq in enumerate(missing_codes, 1):
            logger.info(f"\n[{i}/{len(missing_codes)}] Elaborazione AQ {codice_aq}...")

            result = process_single_aq(codice_aq, db_config, api_config)
            summary['processed'] += 1

            if result['status'] == 'success':
                summary['success'] += 1
            elif result['status'] == 'skipped':
                summary['skipped'] += 1
            else:
                summary['failed'] += 1
                if result.get('error'):
                    summary['errors'].append({
                        'codice_aq': codice_aq,
                        'error': result['error']
                    })

            # Progresso
            progress = (i / len(missing_codes)) * 100
            logger.info(f"Progresso: {progress:.1f}% - Success: {summary['success']}, Skip: {summary['skipped']}, Fail: {summary['failed']}")

            # Rate limiting tra AQ
            time.sleep(2)

        summary['end_time'] = datetime.now().isoformat()

        logger.info("="*60)
        logger.info("AQ MISSING IMPORT - COMPLETATO")
        logger.info(f"Totale mancanti: {summary['total_missing']}")
        logger.info(f"Processati: {summary['processed']}")
        logger.info(f"Successo: {summary['success']}")
        logger.info(f"Saltati: {summary['skipped']}")
        logger.info(f"Falliti: {summary['failed']}")
        logger.info("="*60)

        return summary

    except Exception as e:
        logger.error(f"Errore fatale: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        summary['fatal_error'] = str(e)
        summary['end_time'] = datetime.now().isoformat()
        return summary


# ============================================================================
# SERVICE BUS INTEGRATION
# ============================================================================

def send_completion_message(service_bus_conn, success, summary, queue_name):
    """Invia messaggio di completamento al Service Bus"""
    message_data = {
        'task': 'aq-missing-import',
        'status': 'success' if success else 'failed',
        'summary': summary,
        'timestamp': datetime.now().isoformat()
    }

    for attempt in range(5):
        try:
            logger.info(f"Invio messaggio completamento (tentativo {attempt + 1}/5)...")

            client = get_servicebus_client()
            with client:
                sender = client.get_queue_sender(queue_name=queue_name)
                with sender:
                    message = ServiceBusMessage(json.dumps(message_data))
                    sender.send_messages(message)

            time.sleep(2)
            logger.info("✅ Messaggio completamento inviato!")
            return True
        except Exception as e:
            logger.error(f"Tentativo {attempt + 1} fallito: {e}")
            if attempt < 4:
                time.sleep(2 ** attempt)

    logger.error("❌ Impossibile inviare messaggio completamento")
    return False


def process_import_request(message_data):
    """Processa una richiesta di import dalla coda"""
    request_id = message_data.get('request_id', 'unknown')
    batch_size = message_data.get('batch_size', 50)
    max_workers = message_data.get('max_workers', 4)

    SERVICE_BUS = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    RESPONSE_QUEUE = os.getenv('RESPONSE_QUEUE', 'import-sal-responses')

    logger.info("="*60)
    logger.info("AQ MISSING IMPORT SERVICE")
    logger.info("="*60)
    logger.info(f"[{request_id}] Batch size: {batch_size}")
    logger.info(f"[{request_id}] Max workers: {max_workers}")

    try:
        # Esegui import
        summary = run_missing_aq_import(batch_size=batch_size, max_workers=max_workers)

        # Determina successo
        success = summary.get('fatal_error') is None and summary['failed'] < summary['processed'] / 2

        # Invia messaggio completamento
        if SERVICE_BUS and RESPONSE_QUEUE:
            send_completion_message(SERVICE_BUS, success, summary, RESPONSE_QUEUE)

        return success

    except Exception as e:
        logger.error(f"[{request_id}] Errore: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")

        if SERVICE_BUS and RESPONSE_QUEUE:
            send_completion_message(SERVICE_BUS, False, {'error': str(e)}, RESPONSE_QUEUE)

        return False


def listen_for_import_requests():
    """Ascolta le richieste di import sulla coda Service Bus"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('REQUEST_QUEUE', 'import-sal-aq-missing')

    logger.info("🚀 Avvio AQ Missing Import Service...")
    logger.info(f"📡 In ascolto sulla coda: {REQUEST_QUEUE}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing':
        logger.error("SERVICE_BUS_CONNECTION_STRING non configurato")
        sys.exit(1)

    try:
        client = get_servicebus_client()
        with client:
            receiver = client.get_queue_receiver(
                queue_name=REQUEST_QUEUE,
                receive_mode=ServiceBusReceiveMode.RECEIVE_AND_DELETE
            )
            with receiver:
                logger.info(f"✅ Connesso alla coda: {REQUEST_QUEUE}")
                logger.info("⏳ In attesa di messaggi...")

                for message in receiver:
                    try:
                        logger.info("="*60)
                        logger.info("📨 NUOVO MESSAGGIO RICEVUTO")
                        logger.info("="*60)

                        message_data = json.loads(str(message))
                        process_import_request(message_data)

                        logger.info("✅ Messaggio gestito")

                    except json.JSONDecodeError as e:
                        logger.error(f"❌ JSON non valido: {e}")
                    except Exception as e:
                        logger.error(f"❌ Errore elaborazione: {e}")
                        import traceback
                        logger.error(f"Traceback: {traceback.format_exc()}")

    except KeyboardInterrupt:
        logger.info("🛑 Arresto in corso...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Errore fatale: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        sys.exit(1)


def main():
    """Main function"""
    import argparse

    parser = argparse.ArgumentParser(description='AQ Missing Import Service')
    parser.add_argument('--mode', choices=['service', 'standalone', 'test'],
                       default='service', help='Modalità esecuzione')
    parser.add_argument('--batch-size', type=int, default=50,
                       help='Numero max AQ da processare')
    parser.add_argument('--test-code', type=int,
                       help='Codice AQ specifico da testare')

    args = parser.parse_args()

    logger.info("="*60)
    logger.info("AQ MISSING IMPORT SERVICE - AVVIO")
    logger.info("="*60)
    logger.info(f"Python: {sys.version}")
    logger.info(f"Working directory: {os.getcwd()}")
    logger.info(f"Modalità: {args.mode}")
    logger.info(f"Storage API: {STORAGE_API_BASE_URL}")
    logger.info("="*60)

    if args.mode == 'test':
        # Modalità test: processa un singolo AQ
        if args.test_code:
            db_config = get_db_config()
            api_config = get_api_config()
            result = process_single_aq(args.test_code, db_config, api_config)
            logger.info(f"Risultato test: {json.dumps(result, indent=2)}")
        else:
            logger.error("Specificare --test-code per modalità test")
            sys.exit(1)

    elif args.mode == 'standalone':
        # Modalità standalone: esegui una volta e termina
        summary = run_missing_aq_import(batch_size=args.batch_size)
        logger.info(f"Riepilogo: {json.dumps(summary, indent=2, default=str)}")

    else:
        # Modalità service: ascolta coda Service Bus
        listen_for_import_requests()


if __name__ == "__main__":
    main()

