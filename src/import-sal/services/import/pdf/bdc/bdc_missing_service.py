#!/usr/bin/env python3
"""
BDC Missing Import Service - Container Apps version
Trova i Buoni di Consegna mancanti nel DB, cerca i PDF tramite API Storage e li importa

v2.0 - Aggiunto timeout robusto con concurrent.futures e logging migliorato
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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import threading
from azure.servicebus import ServiceBusClient, ServiceBusMessage, ServiceBusReceiveMode

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

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

from ocr_common import fetch_mysql_query, execute_batch_queries, get_db_config
from bdc_processor import process_single_bdc_pdf
from bdc_p7m_handler import process_bdc_file, cleanup_temp_file

# ============================================================================
# CONFIGURATION
# ============================================================================

# Timeout per l'elaborazione OCR (in secondi)
OCR_TIMEOUT_SECONDS = int(os.getenv('OCR_TIMEOUT_SECONDS', '180'))  # 3 minuti default

# Intervallo heartbeat (in secondi)
HEARTBEAT_INTERVAL = int(os.getenv('HEARTBEAT_INTERVAL', '30'))

# API Storage per la collezione
STORAGE_API_BASE_URL = os.getenv(
    'STORAGE_COLL_API_URL',
    'https://polis-coll-api-storage-dec5dpfhhzdrehck.northeurope-01.azurewebsites.net'
)

def get_api_config():
    """Configurazione API per il servizio BDC mancanti"""
    return {
        "search_url": f"{STORAGE_API_BASE_URL}/api/search/unified",
        "download_url": f"{STORAGE_API_BASE_URL}/api/download",
    }


# ============================================================================
# HEARTBEAT HELPER
# ============================================================================

class HeartbeatThread:
    """Thread per logging heartbeat durante operazioni lunghe"""
    
    def __init__(self, context_msg, interval=HEARTBEAT_INTERVAL):
        self.context_msg = context_msg
        self.interval = interval
        self._stop_event = threading.Event()
        self._thread = None
        self._start_time = None
    
    def _heartbeat_loop(self):
        """Loop interno del heartbeat"""
        count = 0
        while not self._stop_event.wait(self.interval):
            count += 1
            elapsed = time.time() - self._start_time
            logger.info(f"💓 HEARTBEAT [{self.context_msg}] - attivo da {elapsed:.0f}s (beat #{count})")
    
    def start(self):
        """Avvia il thread heartbeat"""
        self._start_time = time.time()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._thread.start()
        logger.info(f"💓 HEARTBEAT START [{self.context_msg}]")
    
    def stop(self):
        """Ferma il thread heartbeat"""
        if self._thread:
            self._stop_event.set()
            self._thread.join(timeout=2)
            elapsed = time.time() - self._start_time
            logger.info(f"💓 HEARTBEAT STOP [{self.context_msg}] - durata totale: {elapsed:.1f}s")


# ============================================================================
# DATABASE FUNCTIONS
# ============================================================================

def get_missing_bdc_codes(db_config, limit=None):
    """
    Estrae i codici BDC (numero_bdc) presenti in buoni_consegna ma mancanti in bdc_pdf

    Returns:
        List[int]: Lista di codici BDC mancanti
    """
    query = """
    SELECT DISTINCT numero_bdc FROM buoni_consegna
    WHERE numero_bdc NOT IN (
        SELECT id_ordine
        FROM bdc_pdf
        WHERE id_ordine IS NOT NULL
    )
    """

    if limit:
        query += f" LIMIT {limit}"

    try:
        logger.info("Esecuzione query per trovare BDC mancanti...")
        results = fetch_mysql_query(query, db_config)

        if results:
            codes = [row['numero_bdc'] for row in results]
            logger.info(f"Trovati {len(codes)} codici BDC mancanti")
            return codes
        else:
            logger.info("Nessun codice BDC mancante trovato")
            return []

    except Exception as e:
        logger.error(f"Errore query BDC mancanti: {e}")
        return []


# ============================================================================
# API STORAGE FUNCTIONS
# ============================================================================

def search_bdc_pdf(numero_bdc, api_config):
    """
    Cerca un PDF BDC tramite l'API search/unified

    Args:
        numero_bdc: Numero BDC da cercare (es. 4100620739)
        api_config: Configurazione API

    Returns:
        List[dict]: Lista di risultati con path dei file trovati, ordinati per preferenza
    """
    try:
        search_url = api_config['search_url']
        params = {
            'q': str(numero_bdc),
            'search_type': 'both',
            'root_only': 'false',
            'exact': 'true',
            'limit': 100
        }

        logger.info(f"🔍 Ricerca PDF per BDC {numero_bdc}...")
        logger.debug(f"   URL: {search_url}")
        logger.debug(f"   Params: {params}")
        
        response = requests.get(search_url, params=params, timeout=60)
        response.raise_for_status()

        data = response.json()
        logger.debug(f"   Response success: {data.get('success')}, results count: {len(data.get('results', []))}")

        if data.get('success') and data.get('results'):
            # Filtra solo i file PDF e P7M (BDC possono essere firmati)
            pdf_results = [
                r for r in data['results']
                if r.get('type') == 'file' and
                (r.get('name', '').lower().endswith('.pdf') or
                 r.get('name', '').lower().endswith('.pdf.pdf') or
                 r.get('name', '').lower().endswith('.p7m'))
            ]

            # Ordina per preferenza: PDF prima di P7M, file più recenti prima
            def sort_key(item):
                name = item.get('name', '').lower()
                # Preferisci PDF puri rispetto a P7M
                is_p7m = 1 if name.endswith('.p7m') else 0
                # Preferisci file più recenti
                last_modified = item.get('lastModified', '')
                return (is_p7m, -len(last_modified) if last_modified else 0)

            pdf_results.sort(key=sort_key)

            logger.info(f"BDC {numero_bdc}: trovati {len(pdf_results)} file PDF/P7M")
            for i, r in enumerate(pdf_results[:3]):  # Log primi 3
                logger.debug(f"   [{i+1}] {r.get('name', 'N/A')[:60]}")
            return pdf_results
        else:
            logger.warning(f"BDC {numero_bdc}: nessun risultato dalla ricerca")
            return []

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Errore ricerca BDC {numero_bdc}: {e}")
        return []
    except Exception as e:
        logger.error(f"❌ Errore imprevisto ricerca BDC {numero_bdc}: {e}")
        import traceback
        logger.error(f"   Traceback: {traceback.format_exc()}")
        return []


def download_file_bytes(file_path, api_config):
    """
    Scarica un file e restituisce i bytes

    Args:
        file_path: Path del file nello storage
        api_config: Configurazione API

    Returns:
        bytes: Contenuto file, o None se errore
    """
    try:
        # URL encode del path
        encoded_path = urllib.parse.quote(file_path, safe='')
        download_url = f"{api_config['download_url']}/{encoded_path}"

        logger.info(f"📥 Download file: {file_path[:80]}...")
        logger.debug(f"   URL: {download_url[:100]}...")
        
        response = requests.get(download_url, timeout=120)
        response.raise_for_status()

        # Verifica se la risposta è già base64 (JSON) o binaria
        content_type = response.headers.get('content-type', '')
        logger.debug(f"   Content-Type: {content_type}")
        logger.debug(f"   Content-Length: {len(response.content)} bytes")

        if 'application/json' in content_type:
            # La risposta è JSON con base64
            data = response.json()
            if data.get('success') and data.get('base64'):
                decoded = base64.b64decode(data['base64'])
                logger.info(f"✅ Download completato (JSON/base64): {len(decoded)} bytes")
                return decoded
            else:
                logger.error(f"❌ Risposta JSON non valida per {file_path}")
                return None
        else:
            # La risposta è binaria
            logger.info(f"✅ Download completato (binary): {len(response.content)} bytes")
            return response.content

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Errore download file {file_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"❌ Errore imprevisto download file {file_path}: {e}")
        import traceback
        logger.error(f"   Traceback: {traceback.format_exc()}")
        return None


# ============================================================================
# PDF PROCESSING WITH ROBUST TIMEOUT
# ============================================================================

def _process_pdf_worker(temp_pdf_path, numero_bdc_fallback):
    """
    Worker function per processare il PDF (eseguita nel thread con timeout)
    """
    logger.info(f"🔧 Worker OCR avviato per {os.path.basename(temp_pdf_path)}")
    try:
        sql = process_single_bdc_pdf(temp_pdf_path)
        logger.info(f"🔧 Worker OCR completato, SQL generato: {bool(sql)}")
        return sql
    except Exception as e:
        logger.error(f"🔧 Worker OCR errore: {e}")
        raise


def process_bdc_pdf_from_bytes(file_path, file_bytes, numero_bdc_fallback=None):
    """
    Processa un PDF/P7M BDC scaricato e genera le query SQL
    Usa concurrent.futures per timeout robusto

    Args:
        file_path: Path del file (per logging e per identificare tipo file)
        file_bytes: Contenuto file in bytes
        numero_bdc_fallback: Numero BDC da usare se non trovato nel documento

    Returns:
        str: Query SQL, o None se errore
    """
    import tempfile

    temp_pdf_path = None
    temp_p7m_path = None
    is_temp_pdf = False
    heartbeat = None

    try:
        filename = os.path.basename(file_path)
        logger.info(f"📄 Elaborazione: {filename[:60]}...")
        if numero_bdc_fallback:
            logger.info(f"   Numero BDC fallback disponibile: {numero_bdc_fallback}")

        # Determina se è un P7M o PDF
        is_p7m = filename.lower().endswith('.p7m')
        logger.info(f"   Tipo file: {'P7M (firmato)' if is_p7m else 'PDF'}")
        logger.info(f"   Dimensione: {len(file_bytes)} bytes")

        if is_p7m:
            # Salva come P7M temporaneo ed estrai PDF
            suffix = '.p7m'
            temp_fd, temp_p7m_path = tempfile.mkstemp(suffix=suffix, prefix='bdc_')
            with os.fdopen(temp_fd, 'wb') as f:
                f.write(file_bytes)

            logger.info(f"   File P7M salvato: {os.path.basename(temp_p7m_path)}")

            # Estrai PDF dal P7M
            try:
                logger.info(f"   Estrazione PDF da P7M in corso...")
                temp_pdf_path, is_temp_pdf = process_bdc_file(temp_p7m_path)
                logger.info(f"   Estrazione P7M completata: {temp_pdf_path}")
            except Exception as e:
                logger.error(f"❌ Errore estrazione P7M: {e}")
                import traceback
                logger.error(f"   Traceback: {traceback.format_exc()}")
                return None

            if not temp_pdf_path:
                logger.warning(f"⚠️ Impossibile estrarre PDF da P7M: {filename}")
                return None
        else:
            # Salva come PDF temporaneo
            suffix = '.pdf'
            temp_fd, temp_pdf_path = tempfile.mkstemp(suffix=suffix, prefix='bdc_')
            with os.fdopen(temp_fd, 'wb') as f:
                f.write(file_bytes)
            is_temp_pdf = True

            logger.info(f"   File PDF salvato: {os.path.basename(temp_pdf_path)}")

        # Processa con il processor BDC standard CON TIMEOUT ROBUSTO
        logger.info(f"🚀 Avvio OCR con timeout di {OCR_TIMEOUT_SECONDS}s...")
        
        # Avvia heartbeat
        heartbeat = HeartbeatThread(f"OCR-{numero_bdc_fallback or filename[:20]}")
        heartbeat.start()
        
        sql = None
        try:
            # Usa ThreadPoolExecutor per timeout robusto
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_process_pdf_worker, temp_pdf_path, numero_bdc_fallback)
                try:
                    sql = future.result(timeout=OCR_TIMEOUT_SECONDS)
                    logger.info(f"✅ OCR completato con successo")
                except FuturesTimeoutError:
                    logger.error(f"⏰ TIMEOUT OCR dopo {OCR_TIMEOUT_SECONDS}s per {filename}")
                    # Tentativo di cancellare il future (potrebbe non funzionare per operazioni bloccanti)
                    future.cancel()
                    return None
                    
        except Exception as e:
            logger.error(f"❌ Errore process_single_bdc_pdf: {e}")
            import traceback
            logger.error(f"   Traceback: {traceback.format_exc()}")
            return None
        finally:
            # Ferma heartbeat
            if heartbeat:
                heartbeat.stop()

        if sql:
            logger.info(f"✅ Query SQL generata per {filename[:50]}...")
            logger.debug(f"   SQL preview: {sql[:200]}...")
            return sql
        else:
            logger.warning(f"⚠️ Nessuna query generata per {filename}")
            return None

    except Exception as e:
        logger.error(f"❌ Errore processing file {file_path}: {e}")
        import traceback
        logger.error(f"   Traceback: {traceback.format_exc()}")
        return None

    finally:
        # Cleanup file temporanei
        try:
            if temp_p7m_path and os.path.exists(temp_p7m_path):
                logger.debug(f"   Cleanup P7M: {temp_p7m_path}")
                cleanup_temp_file(temp_p7m_path)
            if is_temp_pdf and temp_pdf_path and os.path.exists(temp_pdf_path):
                logger.debug(f"   Cleanup PDF: {temp_pdf_path}")
                cleanup_temp_file(temp_pdf_path)
        except Exception as e:
            logger.warning(f"⚠️ Errore cleanup file temporanei: {e}")


# ============================================================================
# MAIN PROCESSING LOGIC
# ============================================================================

def process_single_bdc(numero_bdc, db_config, api_config):
    """
    Processa un singolo numero BDC: cerca, scarica e importa
    Prova tutti i file trovati in ordine finché uno non viene importato con successo

    Args:
        numero_bdc: Numero BDC da processare
        db_config: Configurazione database
        api_config: Configurazione API

    Returns:
        dict: Risultato con status e dettagli
    """
    result = {
        'numero_bdc': numero_bdc,
        'status': 'pending',
        'files_found': 0,
        'files_tried': 0,
        'files_processed': 0,
        'error': None
    }

    logger.info(f"{'='*60}")
    logger.info(f"📋 PROCESSO BDC {numero_bdc}")
    logger.info(f"{'='*60}")

    try:
        # Step 1: Cerca PDF per questo BDC
        logger.info(f"[STEP 1/5] Ricerca file per BDC {numero_bdc}...")
        pdf_results = search_bdc_pdf(numero_bdc, api_config)
        result['files_found'] = len(pdf_results)

        if not pdf_results:
            result['status'] = 'skipped'
            result['error'] = 'Nessun PDF trovato'
            logger.warning(f"⚠️ BDC {numero_bdc}: saltato - nessun PDF trovato")
            return result

        logger.info(f"[STEP 2/5] BDC {numero_bdc}: trovati {len(pdf_results)} file, provo in ordine...")

        # Step 2: Prova ogni file trovato in ordine finché uno non funziona
        for idx, pdf_info in enumerate(pdf_results, 1):
            file_path = pdf_info.get('path', '')
            file_name = pdf_info.get('name', '')

            if not file_path:
                logger.warning(f"   File {idx}: path vuoto, skip")
                continue

            result['files_tried'] += 1
            logger.info(f"[STEP 3/5] BDC {numero_bdc}: tentativo {idx}/{len(pdf_results)} - {file_name[:50]}...")

            # Step 3: Scarica il file
            file_bytes = download_file_bytes(file_path, api_config)

            if not file_bytes:
                logger.warning(f"⚠️ BDC {numero_bdc}: impossibile scaricare {file_name}")
                continue

            # Step 4: Processa con OCR
            logger.info(f"[STEP 4/5] Elaborazione OCR...")
            sql = process_bdc_pdf_from_bytes(file_path, file_bytes, numero_bdc_fallback=numero_bdc)

            if not sql:
                logger.warning(f"⚠️ BDC {numero_bdc}: nessuna query generata da {file_name}")
                # Rate limiting prima di provare il prossimo file
                time.sleep(1)
                continue

            # Step 5: Esegui la query nel database
            logger.info(f"[STEP 5/5] BDC {numero_bdc}: esecuzione query nel DB...")

            success = execute_batch_queries([sql], db_config)

            if success:
                result['files_processed'] = 1
                result['status'] = 'success'
                logger.info(f"✅ BDC {numero_bdc}: IMPORTATO CON SUCCESSO da {file_name}")
                return result
            else:
                logger.warning(f"⚠️ BDC {numero_bdc}: errore esecuzione query da {file_name}")
                # Rate limiting prima di provare il prossimo file
                time.sleep(1)
                continue

        # Se arriviamo qui, nessun file ha funzionato
        result['status'] = 'failed'
        result['error'] = f'Nessuno dei {len(pdf_results)} file trovati ha prodotto un import valido'
        logger.error(f"❌ BDC {numero_bdc}: FALLITO - provati {result['files_tried']} file senza successo")

        return result

    except Exception as e:
        result['status'] = 'error'
        result['error'] = str(e)
        logger.error(f"❌ BDC {numero_bdc}: ERRORE - {e}")
        import traceback
        logger.error(f"   Traceback: {traceback.format_exc()}")
        return result


def run_missing_bdc_import(batch_size=50, max_workers=4):
    """
    Esegue l'import di tutti i BDC mancanti

    Args:
        batch_size: Numero massimo di BDC da processare per esecuzione
        max_workers: Numero di worker paralleli (non usato, processamento sequenziale)

    Returns:
        dict: Riepilogo dell'esecuzione
    """
    logger.info("="*60)
    logger.info("🚀 BDC MISSING IMPORT SERVICE - START")
    logger.info("="*60)
    logger.info(f"   Batch size: {batch_size}")
    logger.info(f"   OCR Timeout: {OCR_TIMEOUT_SECONDS}s")
    logger.info(f"   Heartbeat interval: {HEARTBEAT_INTERVAL}s")
    logger.info(f"   Storage API: {STORAGE_API_BASE_URL}")

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
        logger.info("🔌 Test connessione database...")
        test_result = fetch_mysql_query("SELECT 1 as test", db_config)
        if not test_result:
            raise Exception("Connessione database fallita")
        logger.info("✅ Connessione database OK")

        # Ottieni BDC mancanti
        logger.info("📊 Recupero BDC mancanti dal database...")
        missing_codes = get_missing_bdc_codes(db_config, limit=batch_size)
        summary['total_missing'] = len(missing_codes)

        if not missing_codes:
            logger.info("✅ Nessun BDC mancante da importare")
            summary['end_time'] = datetime.now().isoformat()
            return summary

        logger.info(f"📋 Trovati {len(missing_codes)} BDC mancanti da processare")
        logger.info(f"   Primi 10 codici: {missing_codes[:10]}")

        # Processa i BDC
        # Nota: processamento sequenziale per evitare rate limiting API
        for i, numero_bdc in enumerate(missing_codes, 1):
            logger.info(f"\n{'#'*60}")
            logger.info(f"# [{i}/{len(missing_codes)}] Elaborazione BDC {numero_bdc}...")
            logger.info(f"{'#'*60}")

            result = process_single_bdc(numero_bdc, db_config, api_config)
            summary['processed'] += 1

            if result['status'] == 'success':
                summary['success'] += 1
            elif result['status'] == 'skipped':
                summary['skipped'] += 1
            else:
                summary['failed'] += 1
                if result.get('error'):
                    summary['errors'].append({
                        'numero_bdc': numero_bdc,
                        'error': result['error']
                    })

            # Progresso
            progress = (i / len(missing_codes)) * 100
            logger.info(f"📈 Progresso: {progress:.1f}% - ✅Success: {summary['success']}, ⏭️Skip: {summary['skipped']}, ❌Fail: {summary['failed']}")

            # Rate limiting tra BDC
            logger.info(f"⏳ Rate limiting: pausa 2s prima del prossimo BDC...")
            time.sleep(2)

        summary['end_time'] = datetime.now().isoformat()

        logger.info("="*60)
        logger.info("🏁 BDC MISSING IMPORT - COMPLETATO")
        logger.info("="*60)
        logger.info(f"   Totale mancanti: {summary['total_missing']}")
        logger.info(f"   Processati: {summary['processed']}")
        logger.info(f"   ✅ Successo: {summary['success']}")
        logger.info(f"   ⏭️ Saltati: {summary['skipped']}")
        logger.info(f"   ❌ Falliti: {summary['failed']}")
        logger.info("="*60)

        return summary

    except Exception as e:
        logger.error(f"❌ ERRORE FATALE: {e}")
        import traceback
        logger.error(f"   Traceback: {traceback.format_exc()}")
        summary['fatal_error'] = str(e)
        summary['end_time'] = datetime.now().isoformat()
        return summary


# ============================================================================
# SERVICE BUS INTEGRATION
# ============================================================================

def send_completion_message(service_bus_conn, success, summary, queue_name):
    """Invia messaggio di completamento al Service Bus"""
    message_data = {
        'task': 'bdc-missing-import',
        'status': 'success' if success else 'failed',
        'summary': summary,
        'timestamp': datetime.now().isoformat()
    }

    for attempt in range(5):
        try:
            logger.info(f"📤 Invio messaggio completamento (tentativo {attempt + 1}/5)...")

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
            logger.error(f"❌ Tentativo {attempt + 1} fallito: {e}")
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
    logger.info("📨 BDC MISSING IMPORT SERVICE - REQUEST")
    logger.info("="*60)
    logger.info(f"   Request ID: {request_id}")
    logger.info(f"   Batch size: {batch_size}")
    logger.info(f"   Max workers: {max_workers}")

    try:
        # Esegui import
        summary = run_missing_bdc_import(batch_size=batch_size, max_workers=max_workers)

        # Determina successo
        success = summary.get('fatal_error') is None and summary['failed'] < summary['processed'] / 2

        # Invia messaggio completamento
        if SERVICE_BUS and RESPONSE_QUEUE:
            send_completion_message(SERVICE_BUS, success, summary, RESPONSE_QUEUE)

        return success

    except Exception as e:
        logger.error(f"❌ [{request_id}] Errore: {e}")
        import traceback
        logger.error(f"   Traceback: {traceback.format_exc()}")

        if SERVICE_BUS and RESPONSE_QUEUE:
            send_completion_message(SERVICE_BUS, False, {'error': str(e)}, RESPONSE_QUEUE)

        return False


def listen_for_import_requests():
    """Ascolta le richieste di import sulla coda Service Bus"""
    SERVICE_BUS_CONN = os.getenv('SERVICE_BUS_CONNECTION_STRING')
    REQUEST_QUEUE = os.getenv('REQUEST_QUEUE', 'import-sal-bdc-missing')

    logger.info("🎧 Avvio BDC Missing Import Service...")
    logger.info(f"   In ascolto sulla coda: {REQUEST_QUEUE}")

    if describe_auth_mode('SERVICE_BUS_NAMESPACE', 'SERVICE_BUS_CONNECTION_STRING') == 'missing':
        logger.error("❌ SERVICE_BUS_CONNECTION_STRING non configurato")
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
                        logger.info("📬 NUOVO MESSAGGIO RICEVUTO")
                        logger.info("="*60)

                        message_data = json.loads(str(message))
                        process_import_request(message_data)

                        logger.info("✅ Messaggio gestito")

                    except json.JSONDecodeError as e:
                        logger.error(f"❌ JSON non valido: {e}")
                    except Exception as e:
                        logger.error(f"❌ Errore elaborazione: {e}")
                        import traceback
                        logger.error(f"   Traceback: {traceback.format_exc()}")

    except KeyboardInterrupt:
        logger.info("🛑 Arresto in corso...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Errore fatale: {e}")
        import traceback
        logger.error(f"   Traceback: {traceback.format_exc()}")
        sys.exit(1)


def main():
    """Main function"""
    import argparse

    parser = argparse.ArgumentParser(description='BDC Missing Import Service')
    parser.add_argument('--mode', choices=['service', 'standalone', 'test'],
                       default='service', help='Modalita esecuzione')
    parser.add_argument('--batch-size', type=int, default=50,
                       help='Numero max BDC da processare')
    parser.add_argument('--test-code', type=int,
                       help='Numero BDC specifico da testare')

    args = parser.parse_args()

    logger.info("="*60)
    logger.info("🚀 BDC MISSING IMPORT SERVICE - AVVIO")
    logger.info("="*60)
    logger.info(f"   Python: {sys.version}")
    logger.info(f"   Working directory: {os.getcwd()}")
    logger.info(f"   Modalita: {args.mode}")
    logger.info(f"   Storage API: {STORAGE_API_BASE_URL}")
    logger.info(f"   OCR Timeout: {OCR_TIMEOUT_SECONDS}s")
    logger.info(f"   Heartbeat interval: {HEARTBEAT_INTERVAL}s")
    logger.info("="*60)

    if args.mode == 'test':
        # Modalita test: processa un singolo BDC
        if args.test_code:
            db_config = get_db_config()
            api_config = get_api_config()
            result = process_single_bdc(args.test_code, db_config, api_config)
            logger.info(f"📊 Risultato test: {json.dumps(result, indent=2)}")
        else:
            logger.error("❌ Specificare --test-code per modalita test")
            sys.exit(1)

    elif args.mode == 'standalone':
        # Modalita standalone: esegui una volta e termina
        summary = run_missing_bdc_import(batch_size=args.batch_size)
        logger.info(f"📊 Riepilogo: {json.dumps(summary, indent=2, default=str)}")

    else:
        # Modalita service: ascolta coda Service Bus
        listen_for_import_requests()


if __name__ == "__main__":
    main()