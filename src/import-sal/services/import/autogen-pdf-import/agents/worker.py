"""
Batch Worker
Elabora batch di file PDF e P7M per l'import
"""
import asyncio
import logging
from typing import Optional

from config.document_types import load_document_types_from_db
from config import MAX_CONCURRENT_IN_BATCH
from services.blob_service import BlobService
from services.ocr_service import OCRService
from services.claude_service import ClaudeService
from services.error_logger import error_logger, ErrorTypes
from utils.pdf_utils import get_pdf_content
from agents.importers import get_importer

logger = logging.getLogger(__name__)


class BatchWorker:
    """
    Worker che elabora un batch di file
    """

    def __init__(self):
        self.claude_service = ClaudeService()

    async def process_batch(
        self,
        files: list[str],
        sal_number: int,
        folder_path: str,
        batch_number: int,
        total_batches: int,
        force_reprocess: bool = False,
        doc_type: str = None
    ) -> list[dict]:
        """
        Elabora un batch di file in parallelo con semaforo.

        Args:
            files: Lista di path file da elaborare
            sal_number: Numero SAL
            folder_path: Path cartella originale
            batch_number: Numero batch corrente
            total_batches: Totale batch
            force_reprocess: Se True, rielabora anche file già importati
            doc_type: Se specificato, salta classificazione e usa questo tipo

        Returns:
            Lista risultati per ogni file
        """
        sem = asyncio.Semaphore(MAX_CONCURRENT_IN_BATCH)

        async def process_with_sem(file_path: str, idx: int) -> dict:
            filename = file_path.split('/')[-1]
            async with sem:
                logger.info(
                    f"[Batch {batch_number}/{total_batches}] "
                    f"File {idx}/{len(files)}: {filename}"
                )
                try:
                    return await self._process_single_file(
                        blob_service=blob_service,
                        ocr_service=ocr_service,
                        file_path=file_path,
                        sal_number=sal_number,
                        force_reprocess=force_reprocess,
                        doc_type=doc_type
                    )
                except Exception as e:
                    logger.exception(f"Errore critico file {filename}: {e}")
                    return {
                        "file": file_path,
                        "status": "error",
                        "error": str(e)
                    }

        async with BlobService() as blob_service:
            async with OCRService() as ocr_service:
                tasks = [
                    process_with_sem(file_path, idx)
                    for idx, file_path in enumerate(files, 1)
                ]
                results = await asyncio.gather(*tasks)

        return list(results)

    async def _process_single_file(
        self,
        blob_service: BlobService,
        ocr_service: OCRService,
        file_path: str,
        sal_number: int,
        force_reprocess: bool = False,
        doc_type: str = None
    ) -> dict:
        """
        Elabora un singolo file PDF/P7M con pipeline completa.
        """
        filename = file_path.split('/')[-1]
        result = {
            "file": file_path,
            "status": "pending",
            "document_type": None,
            "error": None
        }

        try:
            # 1. Verifica se già processato e marca come in elaborazione
            if force_reprocess:
                logger.info(f"Force reprocess: {filename}")
            else:
                can_process = await blob_service.check_and_mark_processing(file_path)
                if not can_process:
                    result["status"] = "skipped"
                    result["reason"] = "Already processed"
                    return result

            # 2. Download file
            file_bytes = await blob_service.download_file(file_path)
            if not file_bytes:
                await self._handle_error(
                    blob_service, file_path, None, sal_number,
                    ErrorTypes.DOWNLOAD_FAILED, "Download fallito"
                )
                result["status"] = "error"
                result["error"] = "Download failed"
                return result

            return await self._process_pdf_flow(
                blob_service, ocr_service, file_bytes, file_path, filename,
                sal_number, result, forced_doc_type=doc_type
            )

        except Exception as e:
            logger.exception(f"Errore processando {filename}")
            result["status"] = "error"
            result["error"] = str(e)
            return result

    # ==========================================
    # FLOW: File PDF/P7M
    # ==========================================

    async def _process_pdf_flow(
        self,
        blob_service: BlobService,
        ocr_service: OCRService,
        file_bytes: bytes,
        file_path: str,
        filename: str,
        sal_number: int,
        result: dict,
        forced_doc_type: str = None
    ) -> dict:
        """
        Pipeline per file PDF/P7M:
        estrazione PDF → OCR → classificazione AI → estrazione dati AI → import DB
        """
        # 3. Estrai PDF (gestisce P7M, DOCX, ricorsione) - CPU-bound, offload al thread pool
        pdf_bytes, error_reason = await asyncio.to_thread(get_pdf_content, file_bytes, filename)
        if not pdf_bytes:
            if error_reason == "corrupted":
                error_type = ErrorTypes.FILE_CORRUPTED
                error_msg = f"File corrotto ({len(file_bytes)} bytes)"
            else:
                error_type = ErrorTypes.P7M_EXTRACTION_FAILED
                error_msg = "Estrazione PDF fallita"

            await self._handle_error(
                blob_service, file_path, None, sal_number,
                error_type, error_msg
            )
            result["status"] = "error"
            result["error"] = error_msg
            return result

        # 4. OCR con strategia best result
        ocr_result = await ocr_service.ocr_best_result(pdf_bytes, filename)
        if not ocr_result or not ocr_result.content:
            await self._handle_error(
                blob_service, file_path, None, sal_number,
                ErrorTypes.OCR_FAILED, "OCR non ha estratto testo"
            )
            result["status"] = "error"
            result["error"] = "OCR failed"
            return result

        result["ocr_provider"] = ocr_result.provider
        result["ocr_rotation"] = ocr_result.rotation_applied
        result["ocr_quality"] = ocr_result.quality_score

        # 5. Classificazione (skip se doc_type forzato)
        if forced_doc_type:
            doc_type = forced_doc_type
            confidence = 1.0
            result["document_type"] = doc_type
            result["confidence"] = confidence
            logger.info(f"[{filename}] Doc type forzato: {doc_type} (skip classificazione)")
        else:
            classification = await self.claude_service.classify_document(
                ocr_result.content, filename, folder_path=file_path
            )
            doc_type = classification.get("categoria", "UNKNOWN")
            confidence = classification.get("confidence", 0)
            result["document_type"] = doc_type
            result["confidence"] = confidence

        # 6. Gestione UNKNOWN
        doc_types_config = load_document_types_from_db()
        if doc_type == "UNKNOWN" or doc_type not in doc_types_config:
            await blob_service.mark_blob_unknown(file_path)
            result["status"] = "skipped"
            result["reason"] = classification.get("motivo", "Non classificabile")
            return result

        # 7. Estrazione dati con agent specializzato
        importer = get_importer(doc_type)
        if not importer:
            await self._handle_error(
                blob_service, file_path, doc_type, sal_number,
                ErrorTypes.UNKNOWN_TYPE, f"Importer non implementato: {doc_type}"
            )
            result["status"] = "error"
            result["error"] = f"No importer for {doc_type}"
            return result

        json_data = await importer.extract(
            ocr_result.content, filename, file_path,
            page_images=ocr_result.page_images
        )

        # Libera memoria immagini dopo estrazione
        ocr_result.page_images = None

        if not json_data:
            await self._handle_error(
                blob_service, file_path, doc_type, sal_number,
                ErrorTypes.JSON_MALFORMED, "Estrazione JSON fallita"
            )
            result["status"] = "failed"
            result["error"] = "Data extraction failed"
            return result

        # 8. Import nel database
        success, error = await importer.import_to_db(json_data, sal_number, file_path)

        # 9. Marcatura blob
        if success:
            await blob_service.mark_blob_completed(file_path, doc_type, True)
            result["status"] = "success"
        else:
            await blob_service.mark_blob_failed(file_path, doc_type, error)
            error_logger.log_import_error(
                blob_name=file_path,
                doc_type=doc_type,
                sal_number=sal_number,
                error_type=ErrorTypes.INSERT_FAILED,
                error_message=error,
                json_extracted=json_data
            )
            result["status"] = "failed"
            result["error"] = error

        return result

    async def _handle_error(
        self,
        blob_service: BlobService,
        file_path: str,
        doc_type: Optional[str],
        sal_number: int,
        error_type: str,
        error_message: str
    ):
        """
        Gestisce errore marcando blob e loggando
        """
        await blob_service.mark_blob_failed(
            file_path,
            doc_type or "UNKNOWN",
            error_message
        )

        error_logger.log_import_error(
            blob_name=file_path,
            doc_type=doc_type or "UNKNOWN",
            sal_number=sal_number,
            error_type=error_type,
            error_message=error_message
        )