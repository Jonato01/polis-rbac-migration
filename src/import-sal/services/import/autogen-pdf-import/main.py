"""
Entry point principale per AutoGen PDF Import Container App

Ascolta su due code Service Bus:
- INPUT_QUEUE: riceve richieste di import cartella, le suddivide in batch e
  li pubblica sulla WORK_QUEUE per distribuzione multi-replica
- WORK_QUEUE: riceve batch singoli da elaborare. L'ultimo batch a completarsi
  invia la response aggregata alla RESPONSE_QUEUE
"""
import asyncio
import json
import logging
import re
import signal
import time
import uuid
from datetime import datetime

from azure.servicebus.aio import ServiceBusClient
from azure.servicebus import ServiceBusMessage

from azure_clients import get_servicebus_client_aio
from config import (
    INPUT_QUEUE,
    WORK_QUEUE,
    RESPONSE_QUEUE,
    missing_required_vars as config_missing_required_vars,
)
from config.document_types import reload_config
from agents.importers.dynamic_importer import clear_importer_cache
from services.dispatcher import FileDispatcher
from services.job_service import JobService

# Flag per shutdown graceful (SIGTERM da Azure scale-down)
_shutdown_requested = False

# Configurazione logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def extract_sal_from_path(folder_path: str) -> int | None:
    """
    Estrae il numero SAL dal path della cartella

    Esempi:
    - "8_SAL_30_04_2025/documenti" -> 8
    - "10_SAL_15_06_2025" -> 10
    """
    match = re.search(r'/?(\d+)_SAL_', folder_path)
    if match:
        return int(match.group(1))
    return None


# ============================================================
# INPUT_QUEUE handler - Dispatcher: riceve richiesta folder,
# crea job, pubblica batch sulla WORK_QUEUE
# ============================================================

async def handle_input_message(message_body: str):
    """
    Processa messaggio dalla INPUT_QUEUE.
    Suddivide i file in batch e li pubblica sulla WORK_QUEUE.

    Formato messaggio atteso:
    {
        "task": "import-pdf",
        "folder_path": "8_SAL_30_04_2025/documenti",
        "sal_number": 8,
        "force_reprocess": false
    }
    """
    data = json.loads(message_body)
    folder_path = data["folder_path"]
    force_reprocess = data.get("force_reprocess", False)
    doc_type = data.get("doc_type")
    upload_id = data.get("upload_id")
    sal_number = data.get("sal_number") or extract_sal_from_path(folder_path)

    logger.info(
        f"[INPUT] Ricevuto task import per: {folder_path} (SAL {sal_number})"
        f"{f' upload_id={upload_id}' if upload_id else ''}"
        f"{' [FORCE REPROCESS]' if force_reprocess else ''}"
        f"{f' [DOC_TYPE: {doc_type}]' if doc_type else ''}"
    )

    reload_config()
    clear_importer_cache()

    job_id = str(uuid.uuid4())
    job_service = JobService()

    # Dispatch: lista file, crea batch, pubblica sulla WORK_QUEUE
    dispatcher = FileDispatcher()
    total_files, total_batches, batches = await dispatcher.dispatch_to_queue(
        folder_path, sal_number,
        job_id=job_id,
        force_reprocess=force_reprocess,
        doc_type=doc_type,
        upload_id=upload_id
    )

    if total_files == 0:
        logger.info(f"[INPUT] Nessun file da processare per {folder_path}")
        # Invia response vuota
        async with get_servicebus_client_aio() as sb:
            await send_completion_message(sb, upload_id, [])
        return

    # Crea job record in MySQL
    job_service.create_job(
        job_id=job_id,
        folder_path=folder_path,
        sal_number=sal_number,
        total_files=total_files,
        total_batches=total_batches,
        force_reprocess=force_reprocess
    )
    job_service.mark_job_processing(job_id)

    logger.info(
        f"[INPUT] Job {job_id}: dispatched {total_batches} batch "
        f"({total_files} file) per {folder_path}"
    )


# ============================================================
# WORK_QUEUE handler - Worker: processa un batch,
# aggiorna contatori job, invia response se ultimo batch
# ============================================================

async def handle_work_message(message_body: str):
    """
    Processa messaggio dalla WORK_QUEUE.
    Elabora un singolo batch e aggiorna il job.

    Formato messaggio atteso:
    {
        "task": "process-batch",
        "job_id": "uuid4",
        "folder_path": "9_SAL_31_08_2025",
        "sal_number": 9,
        "batch_number": 1,
        "total_batches": 200,
        "files": ["path/file1.pdf", ...],
        "total_files_in_folder": 10000,
        "force_reprocess": false
    }
    """
    data = json.loads(message_body)
    job_id = data.get("job_id")
    folder_path = data["folder_path"]
    sal_number = data["sal_number"]
    batch_number = data["batch_number"]
    total_batches = data["total_batches"]
    files = data["files"]
    force_reprocess = data.get("force_reprocess", False)
    doc_type = data.get("doc_type")
    upload_id = data.get("upload_id")

    logger.info(
        f"[WORK] Batch {batch_number}/{total_batches} - "
        f"{len(files)} file - job {job_id}"
    )

    reload_config()
    clear_importer_cache()

    start_time = time.time()

    # Processa batch
    from agents.worker import BatchWorker
    worker = BatchWorker()
    batch_results = await worker.process_batch(
        files=files,
        sal_number=sal_number,
        folder_path=folder_path,
        batch_number=batch_number,
        total_batches=total_batches,
        force_reprocess=force_reprocess,
        doc_type=doc_type
    )

    processing_time = time.time() - start_time
    success_count = sum(1 for r in batch_results if r.get("status") == "success")

    logger.info(
        f"[WORK] Batch {batch_number}/{total_batches} completato in {processing_time:.1f}s: "
        f"{success_count}/{len(files)} success"
    )

    if not job_id:
        # Fallback: nessun job_id (compatibilita' con messaggi vecchi)
        logger.warning("[WORK] Nessun job_id nel messaggio, skip coordinamento")
        return

    # Aggiorna job atomicamente
    job_service = JobService()
    completed_job = job_service.report_batch_completion(job_id, batch_results)

    if completed_job:
        # Questo worker ha completato l'ultimo batch -> invia response aggregata
        logger.info(f"[WORK] Job {job_id} COMPLETATO, invio response aggregata")

        async with get_servicebus_client_aio() as sb:
            await send_completion_message(sb, upload_id, [], counts=completed_job)


# ============================================================
# Messaggio di completamento (formato invariato per orchestratore)
# ============================================================

async def send_completion_message(
    sb_client: ServiceBusClient,
    upload_id: int | None,
    results: list[dict],
    counts: dict = None
):
    """
    Invia messaggio di completamento alla RESPONSE_QUEUE.
    Formato compatibile con il Bridge.

    Se counts e' fornito (es. da retry chain), usa quelli invece di ricalcolare da results.
    """
    if counts:
        success = counts.get("success_count", 0)
        failed = counts.get("failed_count", 0)
        skipped = counts.get("skipped_count", 0)
        errors = counts.get("error_count", 0)
        total = success + failed + skipped + errors
    else:
        total = len(results)
        success = sum(1 for r in results if r.get("status") == "success")
        failed = sum(1 for r in results if r.get("status") == "failed")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        errors = sum(1 for r in results if r.get("status") == "error")

    message_data = {
        "import_type": "pdf",
        "upload_id": upload_id,
        "status": "success" if failed == 0 and errors == 0 else "completed_with_errors",
        "summary": {
            "total_files": total,
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "errors": errors
        },
        "failed_files": [
            {"file": r.get("file"), "error": r.get("error", "")[:200]}
            for r in results if r.get("status") in ("failed", "error")
        ]
    }

    sender = sb_client.get_queue_sender(queue_name=RESPONSE_QUEUE)
    async with sender:
        message = ServiceBusMessage(json.dumps(message_data))
        await sender.send_messages(message)

    logger.info(f"Messaggio completamento inviato: {success}/{total} success, {failed} failed")


# ============================================================
# Lock renewal - mantiene il messaggio bloccato durante il processing
# ============================================================

async def _renew_lock_periodically(receiver, message, interval=25):
    """
    Rinnova il lock del messaggio Service Bus ogni `interval` secondi.
    Necessario perché il processing di un batch può durare diversi minuti,
    mentre il lock di default è 30s-5min.
    """
    try:
        while True:
            await asyncio.sleep(interval)
            try:
                await receiver.renew_message_lock(message)
            except Exception as e:
                logger.warning(f"Lock renewal fallito: {e}")
                break
    except asyncio.CancelledError:
        pass


# ============================================================
# Listener per singola coda
# ============================================================

async def listen_queue(queue_name: str, handler, sb_client: ServiceBusClient):
    """
    Ascolta messaggi da una coda e li processa con l'handler fornito.

    Il messaggio viene completato DOPO il processing (non prima).
    Questo garantisce che:
    - Il messaggio resta nella coda (locked) durante l'elaborazione
    - KEDA vede messaggi attivi e non scala a zero prematuramente
    - Se la replica viene uccisa, il lock scade e il messaggio
      torna in coda per essere processato da un'altra replica
    """
    receiver = sb_client.get_queue_receiver(queue_name=queue_name)

    async with receiver:
        logger.info(f"In ascolto sulla coda {queue_name}...")

        async for message in receiver:
            if _shutdown_requested:
                logger.info(f"[{queue_name}] Shutdown in corso, abbandono messaggio")
                await receiver.abandon_message(message)
                break

            message_body = str(message)
            logger.info(f"[{queue_name}] Ricevuto messaggio: {message_body[:200]}...")

            # Rinnova lock periodicamente durante il processing
            renewal_task = asyncio.create_task(
                _renew_lock_periodically(receiver, message)
            )

            try:
                try:
                    await handler(message_body)
                except Exception as processing_error:
                    logger.exception(
                        f"[{queue_name}] Errore elaborazione: {processing_error}"
                    )
                    # Invia errore alla RESPONSE_QUEUE
                    try:
                        async with get_servicebus_client_aio() as sb_error:
                            sender = sb_error.get_queue_sender(queue_name=RESPONSE_QUEUE)
                            async with sender:
                                error_msg = ServiceBusMessage(json.dumps({
                                    "task": "autogen-pdf-import",
                                    "status": "error",
                                    "timestamp": datetime.now().isoformat(),
                                    "error": str(processing_error)[:500],
                                    "message_body": message_body[:500]
                                }))
                                await sender.send_messages(error_msg)
                    except Exception as send_err:
                        logger.error(f"Impossibile inviare messaggio errore: {send_err}")

                # Completa il messaggio DOPO il processing (successo o errore gestito)
                await receiver.complete_message(message)

            except Exception as e:
                logger.exception(f"[{queue_name}] Errore critico: {e}")
                try:
                    await receiver.abandon_message(message)
                except Exception:
                    pass
            finally:
                renewal_task.cancel()
                try:
                    await renewal_task
                except asyncio.CancelledError:
                    pass


# ============================================================
# Main - ascolto su entrambe le code
# ============================================================

async def main():
    """
    Main loop - ascolta messaggi da INPUT_QUEUE e WORK_QUEUE in parallelo.
    - INPUT_QUEUE: richieste import cartella -> dispatcher -> batch sulla WORK_QUEUE
    - WORK_QUEUE: batch singoli -> worker -> aggiornamento job -> response se ultimo
    """
    global _shutdown_requested

    logger.info("Avvio AutoGen PDF Import Service (dual-queue)...")
    logger.info(f"Input Queue: {INPUT_QUEUE}")
    logger.info(f"Work Queue: {WORK_QUEUE}")
    logger.info(f"Response Queue: {RESPONSE_QUEUE}")

    # SIGTERM handler per shutdown graceful (Azure scale-down)
    # Quando KEDA scala giù, Azure invia SIGTERM prima di uccidere la replica.
    # Il listener smette di accettare nuovi messaggi e lascia finire il batch corrente.
    def _request_shutdown():
        global _shutdown_requested
        logger.info("SIGTERM ricevuto, shutdown graceful in corso...")
        _shutdown_requested = True

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, _request_shutdown)
        loop.add_signal_handler(signal.SIGINT, _request_shutdown)
    except NotImplementedError:
        pass  # Windows non supporta add_signal_handler

    missing = config_missing_required_vars()
    if missing:
        logger.error(f"Variabili d'ambiente mancanti: {', '.join(missing)}")
        raise SystemExit(1)

    async with get_servicebus_client_aio() as sb_client:
        await asyncio.gather(
            listen_queue(INPUT_QUEUE, handle_input_message, sb_client),
            listen_queue(WORK_QUEUE, handle_work_message, sb_client)
        )


if __name__ == "__main__":
    asyncio.run(main())
