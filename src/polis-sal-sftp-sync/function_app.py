import azure.functions as func
import logging
import zipfile
import os
import time
import uuid
from azure_clients import describe_auth_mode, get_blob_service_client, get_servicebus_client

from config import (
    CONTAINER_NAME,
    UPLOAD_PREFIX, SAL_PREFIX, PROCESSED_PREFIX, DONE_PREFIX,
    ZIP_WORKDIR
)
from db import update_sal_upload_stato, set_sal_upload_error, get_stuck_uploads, get_sal_upload_by_blob

import json
from azure.servicebus import ServiceBusMessage
from config import IMPORT_EXCEL_QUEUE

# Storage dati: DATA_STORAGE_ACCOUNT_NAME + managed identity; fallback legacy = connection string
# in AzureWebJobsStorage (quando host storage e storage dati coincidevano).
DATA_STORAGE_VARS = dict(account_name_var="DATA_STORAGE_ACCOUNT_NAME", conn_str_var="AzureWebJobsStorage")
SERVICE_BUS_VARS = dict(namespace_var="SERVICE_BUS_NAMESPACE", conn_str_var="SERVICE_BUS_CONNECTION_STRING")

app = func.FunctionApp()


def send_import_excel_message(blob_name: str, normalized_folder: str):
    """
    Invia messaggio alla coda 'import-excel' dopo l'unzip.
    Il simulatore (o il sistema Autogen reale) riceverà questo messaggio,
    processerà i file Excel, e invierà il risultato alla coda 'import-response'.
    """
    try:
        if describe_auth_mode("SERVICE_BUS_NAMESPACE", "SERVICE_BUS_CONNECTION_STRING") == "missing":
            logging.warning(">>> SERVICE_BUS_NAMESPACE (o SERVICE_BUS_CONNECTION_STRING) non configurata - skip invio messaggio")
            return False
        
        # Recupera dati dal DB
        sal_upload = get_sal_upload_by_blob(blob_name)
        if not sal_upload:
            logging.warning(f">>> Nessun sal_upload trovato per blob={blob_name} - skip invio messaggio")
            return False
        
        upload_id = sal_upload['id']
        numero_sal = sal_upload['numero_sal']
        
        # Costruisci messaggio nel formato atteso dal simulatore/Autogen
        message_data = {
            "task": "import-excel",
            "upload_id": upload_id,
            "folder_path": normalized_folder,
            "sal_number": numero_sal,
            "import_type": "excel",
            "priority": "normal"
        }
        
        logging.warning(f">>> SERVICE BUS: invio messaggio a coda '{IMPORT_EXCEL_QUEUE}'")
        logging.warning(f">>>   upload_id={upload_id}, sal_number={numero_sal}, folder={normalized_folder}")
        
        sb_client = get_servicebus_client(**SERVICE_BUS_VARS)
        try:
            sender = sb_client.get_queue_sender(queue_name=IMPORT_EXCEL_QUEUE)
            with sender:
                message = ServiceBusMessage(json.dumps(message_data))
                sender.send_messages(message)
            logging.warning(f">>> SERVICE BUS: messaggio inviato con successo")
            return True
        finally:
            sb_client.close()
            
    except Exception as e:
        logging.warning(f">>> SERVICE BUS: ERRORE invio messaggio: {e}")
        import traceback
        logging.warning(traceback.format_exc())
        return False


@app.timer_trigger(
    schedule="0 */2 * * * *",
    arg_name="timer",
    run_on_startup=False
)
def process_zip_timer(timer: func.TimerRequest):
    """
    Timer Trigger: controlla ogni 2 minuti se ci sono ZIP da processare.
    
    3 LIVELLI DI PROTEZIONE ANTI-DUPLICATO:
    1. Blob Lease - lock esclusivo sul file
    2. Marker check - verifica se giÃ  processato (zip_processed/)
    3. Move blob - sposta in zip_done/ dopo completamento
    """
    logging.warning("=" * 60)
    logging.warning("========== TIMER TRIGGER - CONTROLLO ZIP ==========")
    logging.warning("=" * 60)
    
    blob_service = get_blob_service_client(**DATA_STORAGE_VARS)
    container_client = blob_service.get_container_client(CONTAINER_NAME)
    
    # ========== RECOVERY: controlla sal_upload bloccati in UPLOADED ==========
    recover_stuck_uploads(container_client)
    
    blobs = list(container_client.list_blobs(name_starts_with=UPLOAD_PREFIX))
    zip_blobs = [b for b in blobs if b.name.endswith('.zip')]
    
    if not zip_blobs:
        logging.warning(">>> Nessun ZIP trovato in zip_upload/")
        return
    
    logging.warning(f">>> Trovati {len(zip_blobs)} ZIP da verificare")
    
    for blob in zip_blobs:
        try:
            process_single_zip(container_client, blob)
        except Exception as e:
            logging.warning(f">>> ERRORE processando {blob.name}: {e}")
            set_sal_upload_error(blob.name, str(e))


def process_single_zip(container_client, blob):
    """Processa un singolo file ZIP: scarica, unzippa su Storage, aggiorna DB"""
    
    blob_name = blob.name
    blob_size = blob.size or 0
    file_name = blob_name.replace(UPLOAD_PREFIX, "")
    
    start_time = time.time()
    
    logging.warning("")
    logging.warning("-" * 50)
    logging.warning(f">>> PROCESSO: {file_name}")
    logging.warning(f">>> DIMENSIONE: {blob_size / (1024*1024):.1f} MB")
    logging.warning("-" * 50)
    
    lease = None
    zip_path = None
    source_blob = container_client.get_blob_client(blob_name)
    
    try:
        # ========== PROTEZIONE 1: BLOB LEASE ==========
        logging.warning("***** PROTEZIONE 1: ACQUISIZIONE LEASE *****")
        
        try:
            lease = source_blob.acquire_lease(lease_duration=60)
            logging.warning(f">>> Lease acquisito: {lease.id[:8]}...")
        except Exception as e:
            logging.warning(f">>> Lease NON disponibile: {e}")
            logging.warning(">>> SKIP - altro processo attivo")
            return
        
        # ========== PROTEZIONE 2: CHECK MARKER ==========
        logging.warning("***** PROTEZIONE 2: CHECK MARKER *****")
        
        marker_blob_name = f"{PROCESSED_PREFIX}{file_name}.done"
        marker_blob = container_client.get_blob_client(marker_blob_name)
        
        if marker_blob.exists():
            logging.warning(f">>> Marker trovato - giÃ  processato")
            logging.warning(">>> SKIP")
            release_lease_safe(lease)
            return
        
        logging.warning(f">>> Marker non esiste, procedo...")
        
        # ========== FASE 1: DOWNLOAD BLOB IN LOCALE ==========
        logging.warning("***** FASE 1/3: DOWNLOAD BLOB *****")
        download_start = time.time()
        
        zip_path = f"{ZIP_WORKDIR}/{uuid.uuid4()}.zip"
        logging.warning(f">>> Scrittura ZIP su: {zip_path}")
        
        download_stream = source_blob.download_blob()
        bytes_downloaded = 0
        last_log_percent = 0
        
        with open(zip_path, 'wb') as f:
            for chunk in download_stream.chunks():
                f.write(chunk)
                bytes_downloaded += len(chunk)
                
                if bytes_downloaded % (100 * 1024 * 1024) < len(chunk):
                    try:
                        lease.renew()
                    except:
                        pass
                
                if blob_size > 0:
                    percent = int((bytes_downloaded / blob_size) * 100)
                    if percent >= last_log_percent + 10:
                        logging.warning(f"    [DOWNLOAD] {percent}% ({bytes_downloaded // (1024*1024)} MB)")
                        last_log_percent = percent
        
        download_time = time.time() - download_start
        logging.warning(f">>> Download completato in {download_time:.1f}s")
        
        # Leggi root folder dallo ZIP
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            root_folder = get_root_folder(zip_ref)
        
        if not root_folder:
            logging.warning(">>> ZIP non valido: nessuna cartella principale")
            set_sal_upload_error(blob_name, "ZIP non valido: nessuna cartella principale")
            release_lease_safe(lease)
            cleanup_temp(zip_path)
            return
        
        logging.warning(f">>> Root folder: {root_folder}")
        
        # Normalizza il nome della cartella SAL
        normalized_folder = normalize_sal_folder_name(root_folder)
        
        # ========== CHECK: CARTELLA ESISTE GIÃ€ SU STORAGE? ==========
        logging.warning("***** CHECK CARTELLA SU STORAGE *****")
        
        target_prefix = f"{SAL_PREFIX}{normalized_folder}/"
        existing = list(container_client.list_blobs(name_starts_with=target_prefix, results_per_page=1))
        
        if existing:
            logging.warning(f">>> Cartella {target_prefix} ESISTE giÃ  su Storage")
            logging.warning(">>> SKIP - creo marker")
            create_marker(marker_blob, f"Skipped: folder exists on Storage")
            set_sal_upload_error(blob_name, f"Cartella {normalized_folder} esiste giÃ  su Storage")
            release_lease_safe(lease)
            cleanup_temp(zip_path)
            return
        
        # ========== FASE 2: UNZIP SU STORAGE ==========
        logging.warning("***** FASE 2/3: UNZIP SU STORAGE *****")
        upload_start = time.time()
        
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            file_list = [f for f in zip_ref.namelist() if not f.endswith('/')]
            total_files = len(file_list)
            logging.warning(f">>> File da estrarre: {total_files}")
            
            success_count = 0
            error_count = 0
            last_log_percent = 0
            
            for file_path in file_list:
                try:
                    upload_to_storage(container_client, zip_ref, file_path, root_folder, normalized_folder)
                    success_count += 1
                    
                    if success_count % 50 == 0:
                        try:
                            lease.renew()
                        except:
                            pass
                    
                    if total_files > 0:
                        percent = int((success_count / total_files) * 100)
                        if percent >= last_log_percent + 10:
                            logging.warning(f"    [STORAGE] {percent}% ({success_count}/{total_files})")
                            last_log_percent = percent
                            
                except Exception as e:
                    logging.warning(f"    [ERRORE] {file_path}: {e}")
                    error_count += 1
        
        upload_time = time.time() - upload_start
        logging.warning(f">>> Unzip su Storage completato in {upload_time:.1f}s")
        
        # ========== AGGIORNA DB: stato â†’ UNZIPPED ==========
        logging.warning("***** AGGIORNAMENTO DB â†’ UNZIPPED *****")
        try:
            db_ok = update_sal_upload_stato(blob_name, 'UNZIPPED', {
                'unzipped_at': 'NOW()',
                'normalized_folder': normalized_folder
            })
        except Exception as e:
            # DB fallito dopo tutti i retry: NON spostare il blob
            # Al prossimo ciclo il recovery check lo sistemerÃ 
            logging.warning(f">>> DB UPDATE FALLITO: {e}")
            logging.warning(">>> Blob NON spostato - verrÃ  ripreso al prossimo ciclo")
            release_lease_safe(lease)
            cleanup_temp(zip_path)
            return
        
        # ========== STEP 5: INVIO MESSAGGIO A CODA IMPORT-EXCEL ==========
        logging.warning("***** INVIO MESSAGGIO IMPORT-EXCEL *****")
        try:
            send_import_excel_message(blob_name, normalized_folder)
        except Exception as e:
            logging.warning(f">>> Errore invio messaggio import-excel: {e}")
            # Non blocchiamo il flusso: il messaggio può essere reinviato manualmente
        
        # ========== FASE 3: SPOSTA BLOB + CREA MARKER ==========
        logging.warning("***** FASE 3/3: SPOSTA BLOB E CREA MARKER *****")
        
        create_marker(marker_blob, f"Completed: {success_count} files, {error_count} errors")
        logging.warning(f">>> Marker creato")
        
        try:
            done_blob = container_client.get_blob_client(f"{DONE_PREFIX}{file_name}")
            done_blob.start_copy_from_url(source_blob.url)
            
            for _ in range(60):
                props = done_blob.get_blob_properties()
                if props.copy.status == "success":
                    break
                time.sleep(1)
            
            release_lease_safe(lease)
            lease = None
            source_blob.delete_blob()
            logging.warning(f">>> Blob spostato in zip_done/")
        except Exception as e:
            logging.warning(f">>> Errore spostamento: {e}")
        
        total_time = time.time() - start_time
        logging.warning("")
        logging.warning("=" * 50)
        logging.warning(f">>> COMPLETATO in {total_time:.1f}s")
        logging.warning(f">>> File estratti: {success_count} OK, {error_count} errori")
        logging.warning("=" * 50)
        
    except Exception as e:
        logging.warning(f">>> ERRORE: {e}")
        set_sal_upload_error(blob_name, str(e))
        raise
        
    finally:
        release_lease_safe(lease)
        cleanup_temp(zip_path)


def normalize_sal_folder_name(folder_name: str) -> str:
    """
    Normalizza il nome della cartella SAL.
    
    Input:  "12Â° SAL al 31Ago26"
    Output: "12_SAL_31_08_2026"
    
    Estrae: numero SAL, giorno, mese, anno e li ricompone nel formato standard.
    """
    import re
    
    # Pattern per estrarre: numero SAL, giorno, mese (testo), anno (2 cifre)
    # Esempi: "12° SAL al 31Ago26", "9° SAL al 31Dic25"
    # [°Â]* gestisce sia ° (UTF-8 corretto) sia Â° (doppio encoding) sia assenza
    pattern = r'(\d+)[°Â]*\s*SAL\s*al\s*(\d{1,2})([A-Za-z]{3})(\d{2})'
    match = re.search(pattern, folder_name)
    
    if not match:
        # Se non matcha il pattern, restituisci il nome originale pulito
        logging.warning(f">>> Nome cartella non riconosciuto: {folder_name}")
        return folder_name.replace('°', '_').replace('Â°', '_').replace(' ', '_')
    
    numero_sal = match.group(1)
    giorno = match.group(2).zfill(2)
    mese_testo = match.group(3).lower()
    anno_2cifre = match.group(4)
    
    # Mappa mesi italiani abbreviati
    mesi = {
        'gen': '01', 'feb': '02', 'mar': '03', 'apr': '04',
        'mag': '05', 'giu': '06', 'lug': '07', 'ago': '08',
        'set': '09', 'ott': '10', 'nov': '11', 'dic': '12'
    }
    
    mese = mesi.get(mese_testo, '00')
    
    # Anno: assumiamo 20xx per anni < 50, 19xx altrimenti
    anno_int = int(anno_2cifre)
    anno = f"20{anno_2cifre}" if anno_int < 50 else f"19{anno_2cifre}"
    
    normalized = f"{numero_sal}_SAL_{giorno}_{mese}_{anno}"
    logging.warning(f">>> Normalizzato: {folder_name} â†’ {normalized}")
    
    return normalized


def upload_to_storage(container_client, zip_ref: zipfile.ZipFile, file_path: str, root_folder: str, normalized_folder: str):
    """
    Estrae file da ZIP e lo carica come blob su Storage.
    Sostituisce il nome della cartella root con quello normalizzato.
    """
    # Sostituisci la root folder con quella normalizzata
    if file_path.startswith(root_folder + '/'):
        normalized_path = file_path.replace(root_folder + '/', normalized_folder + '/', 1)
    else:
        normalized_path = file_path
    
    blob_name = f"{SAL_PREFIX}{normalized_path}"
    blob_client = container_client.get_blob_client(blob_name)
    
    with zip_ref.open(file_path, 'r') as zip_file:
        data = zip_file.read()
        blob_client.upload_blob(data, overwrite=True)


def release_lease_safe(lease):
    if lease:
        try:
            lease.release()
        except:
            pass


def cleanup_temp(zip_path):
    if zip_path and os.path.exists(zip_path):
        try:
            os.remove(zip_path)
        except:
            pass


def create_marker(marker_blob, content: str):
    try:
        marker_blob.upload_blob(content.encode(), overwrite=True)
    except Exception as e:
        logging.warning(f">>> Errore creazione marker: {e}")


def get_root_folder(zip_ref: zipfile.ZipFile) -> str | None:
    names = zip_ref.namelist()
    if not names:
        return None
    
    first = names[0]
    root = first.split('/')[0]
    
    for name in names:
        if not name.startswith(root + '/') and name != root:
            return None
    
    return root


def recover_stuck_uploads(container_client):
    """
    Recovery: per ogni sal_upload in stato UPLOADED da piÃ¹ di 5 minuti,
    verifica se il marker .done esiste (= unzip completato ma DB non aggiornato).
    Se sÃ¬, aggiorna DB â†’ UNZIPPED.
    """
    stuck = get_stuck_uploads()
    if not stuck:
        return
    
    logging.warning(f">>> RECOVERY: trovati {len(stuck)} sal_upload bloccati in UPLOADED")
    
    for row in stuck:
        blob_name = row['zip_blob_name']
        file_name = blob_name.replace(UPLOAD_PREFIX, "")
        marker_blob_name = f"{PROCESSED_PREFIX}{file_name}.done"
        marker_blob = container_client.get_blob_client(marker_blob_name)
        
        try:
            if marker_blob.exists():
                # Il marker esiste â†’ l'unzip era completato, solo il DB Ã¨ rimasto indietro
                logging.warning(f">>> RECOVERY: marker trovato per {file_name}, aggiorno DB â†’ UNZIPPED")
                try:
                    update_sal_upload_stato(blob_name, 'UNZIPPED', {
                        'unzipped_at': 'NOW()'
                    })
                    logging.warning(f">>> RECOVERY: {file_name} recuperato con successo")
                except Exception as e:
                    logging.warning(f">>> RECOVERY: DB update fallito per {file_name}: {e}")
            else:
                logging.warning(f">>> RECOVERY: nessun marker per {file_name} - verrÃ  riprocessato normalmente")
        except Exception as e:
            logging.warning(f">>> RECOVERY: errore check {file_name}: {e}")