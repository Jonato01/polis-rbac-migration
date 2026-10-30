"""
File Dispatcher Service
Divide i file in batch e li distribuisce ai worker tramite Service Bus
"""
import asyncio
import json
import logging
from datetime import datetime

from azure.servicebus import ServiceBusMessage

from azure_clients import get_servicebus_client_aio
from config import (
    WORK_QUEUE,
    BATCH_SIZE
)
from services.blob_service import BlobService

logger = logging.getLogger(__name__)


class FileDispatcher:
    """
    Dispatcher che divide i file in batch e li pubblica sulla work queue
    """

    async def dispatch_folder(
        self,
        folder_path: str,
        sal_number: int,
        force_reprocess: bool = False,
        doc_type: str = None,
        date_from: str = None,
        date_to: str = None,
        reprocess_unknown: bool = False
    ) -> list[dict]:
        """
        Legge tutti i file dalla cartella, li divide in batch
        e li elabora

        Args:
            folder_path: Path cartella su Blob Storage
            sal_number: Numero SAL
            force_reprocess: Se True, rielabora anche file già importati
            doc_type: Se specificato, filtra solo file con quel document_type
            date_from: Data inizio range ISO per filtro processed_date
            date_to: Data fine range ISO per filtro processed_date
            reprocess_unknown: Se True, rielabora solo file classificati UNKNOWN

        Returns:
            Lista risultati elaborazione
        """
        logger.info(f"Dispatcher: analisi cartella {folder_path}")

        # 1. Lista file
        async with BlobService() as blob_service:
            if doc_type:
                all_files = await blob_service.list_files_by_doc_type(
                    folder_path, doc_type,
                    date_from=date_from, date_to=date_to
                )
                logger.info(f"FILTRO DOC_TYPE: solo file classificati come {doc_type}")
            elif reprocess_unknown:
                all_files = await blob_service.list_unknown_files(folder_path)
                logger.info(f"REPROCESS UNKNOWN: solo file classificati come UNKNOWN")
            elif force_reprocess:
                all_files = await blob_service.list_files(folder_path)
                logger.info(f"FORCE REPROCESS: inclusi tutti i file (anche già processati)")
            else:
                all_files = await blob_service.list_unprocessed_files(folder_path)

        total_files = len(all_files)
        logger.info(f"Dispatcher: trovati {total_files} file da processare")

        if total_files == 0:
            logger.info("Nessun file da processare")
            return []

        # 2. Dividi in batch
        batches = [
            all_files[i:i + BATCH_SIZE]
            for i in range(0, total_files, BATCH_SIZE)
        ]
        total_batches = len(batches)

        logger.info(f"Dispatcher: creati {total_batches} batch da max {BATCH_SIZE} file")

        # 3. Elabora batch (in questo caso in sequenza, in produzione via queue)
        all_results = []

        from agents.worker import BatchWorker
        worker = BatchWorker()

        for batch_idx, batch_files in enumerate(batches, 1):
            logger.info(f"Elaboro batch {batch_idx}/{total_batches} ({len(batch_files)} file)")

            batch_results = await worker.process_batch(
                files=batch_files,
                sal_number=sal_number,
                folder_path=folder_path,
                batch_number=batch_idx,
                total_batches=total_batches,
                force_reprocess=force_reprocess,
                doc_type=doc_type
            )

            all_results.extend(batch_results)

            # Log progresso
            success_count = sum(1 for r in batch_results if r.get("status") == "success")
            logger.info(f"Batch {batch_idx} completato: {success_count}/{len(batch_files)} success")

        logger.info(f"Dispatcher completato: {len(all_results)} file elaborati")
        return all_results

    async def dispatch_to_queue(
        self,
        folder_path: str,
        sal_number: int,
        job_id: str = None,
        force_reprocess: bool = False,
        doc_type: str = None,
        upload_id: int = None,
        date_from: str = None,
        date_to: str = None,
        reprocess_unknown: bool = False
    ) -> tuple[int, int, list[list[str]]]:
        """
        Versione distribuita: pubblica batch sulla work queue
        per elaborazione da parte di worker separati

        Args:
            folder_path: Path cartella su Blob Storage
            sal_number: Numero SAL
            job_id: UUID del job per coordinamento multi-replica
            force_reprocess: Se True, rielabora anche file gia' importati
            doc_type: Se specificato, filtra solo file con quel document_type
            upload_id: ID del sal_upload per tracking
            date_from: Data inizio range ISO per filtro processed_date
            date_to: Data fine range ISO per filtro processed_date
            reprocess_unknown: Se True, rielabora solo file classificati UNKNOWN

        Returns:
            (total_files, total_batches, batches) per permettere al chiamante
            di creare il job record
        """
        logger.info(f"Dispatcher (queue mode): analisi cartella {folder_path}")

        # 1. Lista file
        async with BlobService() as blob_service:
            if doc_type:
                all_files = await blob_service.list_files_by_doc_type(
                    folder_path, doc_type,
                    date_from=date_from, date_to=date_to
                )
                logger.info(f"FILTRO DOC_TYPE: solo file classificati come {doc_type}")
            elif reprocess_unknown:
                all_files = await blob_service.list_unknown_files(folder_path)
                logger.info("REPROCESS UNKNOWN: solo file classificati come UNKNOWN")
            elif force_reprocess:
                all_files = await blob_service.list_files(folder_path)
                logger.info("FORCE REPROCESS: inclusi tutti i file")
            else:
                all_files = await blob_service.list_unprocessed_files(folder_path)

        total_files = len(all_files)

        if total_files == 0:
            logger.info("Nessun file da processare")
            return 0, 0, []

        # 2. Dividi in batch
        batches = [
            all_files[i:i + BATCH_SIZE]
            for i in range(0, total_files, BATCH_SIZE)
        ]
        total_batches = len(batches)

        logger.info(
            f"Dispatcher: {total_files} file -> {total_batches} batch da max {BATCH_SIZE} file"
        )

        # 3. Pubblica batch sulla work queue
        async with get_servicebus_client_aio() as sb_client:
            sender = sb_client.get_queue_sender(queue_name=WORK_QUEUE)

            async with sender:
                for batch_idx, batch_files in enumerate(batches, 1):
                    message_data = {
                        "task": "process-batch",
                        "job_id": job_id,
                        "folder_path": folder_path,
                        "sal_number": sal_number,
                        "batch_number": batch_idx,
                        "total_batches": total_batches,
                        "files": batch_files,
                        "total_files_in_folder": total_files,
                        "force_reprocess": force_reprocess or reprocess_unknown,
                        "doc_type": doc_type,
                        "upload_id": upload_id,
                        "date_from": date_from,
                        "date_to": date_to
                    }

                    message = ServiceBusMessage(
                        json.dumps(message_data),
                        subject=f"batch-{batch_idx}-of-{total_batches}"
                    )

                    await sender.send_messages(message)
                    logger.info(
                        f"Pubblicato batch {batch_idx}/{total_batches} "
                        f"({len(batch_files)} file)"
                    )

        logger.info(f"Dispatcher completato: {total_batches} batch pubblicati")
        return total_files, total_batches, batches


class ProgressTracker:
    """
    Traccia il progresso dell'import per una cartella
    """

    def __init__(
        self,
        folder_path: str,
        total_files: int,
        total_batches: int
    ):
        self.folder_path = folder_path
        self.total_files = total_files
        self.total_batches = total_batches
        self.completed_batches = 0
        self.results = {
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "by_type": {}
        }

    def record_batch_result(self, batch_results: list[dict]):
        """
        Registra risultato di un batch completato
        """
        for result in batch_results:
            status = result.get("status")
            doc_type = result.get("document_type", "UNKNOWN")

            if status == "success":
                self.results["success"] += 1
            elif status == "failed":
                self.results["failed"] += 1
            else:
                self.results["skipped"] += 1

            # Conta per tipo
            if doc_type not in self.results["by_type"]:
                self.results["by_type"][doc_type] = {"success": 0, "failed": 0}

            if status == "success":
                self.results["by_type"][doc_type]["success"] += 1
            elif status == "failed":
                self.results["by_type"][doc_type]["failed"] += 1

        self.completed_batches += 1

        # Log progresso
        progress = (self.completed_batches / self.total_batches) * 100
        processed = (
            self.results["success"] +
            self.results["failed"] +
            self.results["skipped"]
        )

        logger.info(
            f"PROGRESSO: {progress:.1f}% - "
            f"Batch {self.completed_batches}/{self.total_batches} - "
            f"File {processed}/{self.total_files} - "
            f"Success: {self.results['success']}, Failed: {self.results['failed']}"
        )

    def is_complete(self) -> bool:
        return self.completed_batches >= self.total_batches

    def get_final_report(self) -> dict:
        return {
            "folder": self.folder_path,
            "total_files": self.total_files,
            "total_batches": self.total_batches,
            "results": self.results,
            "success_rate": (
                (self.results["success"] / self.total_files * 100)
                if self.total_files > 0 else 0
            )
        }

