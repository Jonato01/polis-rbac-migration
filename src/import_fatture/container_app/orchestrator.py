"""
Orchestratore principale del flusso di import.

Flusso:
1. Riceve una richiesta (cartella SAL)
2. Scansiona i blob per trovare i file Excel
3. Filtra i file gia' elaborati
4. Scarica ogni file e legge metadata Excel (fogli + header)
5. Classifica ogni file con AI
6. Ordina i file per tipo (fatture -> personale -> servizi -> gare -> rendicontazione)
7. Esegue l'import per ogni file
8. Tagga i blob con il risultato
9. Pulisce i file temporanei
10. Restituisce il risultato aggregato
"""
import logging
import os
import time
from typing import Optional

import mysql.connector

from container_app import config
from container_app.blob_manager import BlobManager
from container_app.file_classifier import classify_file, read_excel_metadata
from container_app.import_router import get_importer, sort_files_by_import_order
from container_app.models import (
    BlobMetadata,
    ClassificationResult,
    FileImportResult,
    FileType,
    ImportRequest,
    ImportResponse,
    ImportStatus,
    ImportWarning,
    RowErrorDetail,
)

logger = logging.getLogger("container_app.orchestrator")


class Orchestrator:
    def __init__(
        self,
        blob_manager: BlobManager,
        db_config: Optional[dict] = None,
    ):
        self.blob_manager = blob_manager
        self.db_config = db_config or config.get_db_config()

    def process_folder(
        self, request: ImportRequest,
    ) -> ImportResponse:
        """
        Elabora tutti i file Excel in una cartella SAL.
        Restituisce il risultato aggregato.
        """
        start_time = time.time()
        logger.info(
            "=== Inizio elaborazione richiesta %s: cartella '%s' ===",
            request.upload_id,
            request.folder_path,
        )

        response = ImportResponse(
            upload_id=request.upload_id,
            import_type=request.import_type,
        )

        try:
            # 1. Scansione blob
            blobs = self.blob_manager.list_excel_files(request.folder_path)
            response.files_found = len(blobs)

            if not blobs:
                logger.warning(
                    "Nessun file Excel trovato in '%s'", request.folder_path
                )
                response.status = ImportStatus.COMPLETED
                response.processing_time_seconds = time.time() - start_time
                return response

            # 2. Filtra file gia' elaborati (skip se force_reprocess=true)
            blobs_to_process = []
            if request.force_reprocess:
                logger.info("force_reprocess=true: rielaborazione di tutti i file")
                blobs_to_process = list(blobs)
            else:
                for blob in blobs:
                    if self.blob_manager.is_already_processed(blob):
                        logger.info(
                            "File gia' elaborato, skip: '%s'", blob.name
                        )
                        response.files_skipped += 1
                        response.results.append(
                            FileImportResult(
                                blob_path=blob.name,
                                classified_as="unknown",
                                sal_number=0,
                                status=ImportStatus.SKIPPED,
                            )
                        )
                    else:
                        blobs_to_process.append(blob)

            if not blobs_to_process:
                logger.info("Tutti i file sono gia' stati elaborati")
                response.status = ImportStatus.COMPLETED
                response.processing_time_seconds = time.time() - start_time
                return response

            # 3. Download + classificazione
            classified_files = []  # (blob_path, ClassificationResult, local_path)

            for blob in blobs_to_process:
                local_path = self.blob_manager.download_blob(blob.name)

                # Leggi metadata Excel
                sheet_names, headers_per_sheet = read_excel_metadata(local_path)

                # Classifica
                classification = classify_file(
                    blob.name, sheet_names, headers_per_sheet
                )

                if classification.file_type == FileType.UNKNOWN:
                    logger.warning(
                        "File non classificato: '%s'", blob.name
                    )
                    # Tagga come unknown e skip
                    self.blob_manager.set_blob_metadata(
                        blob.name,
                        BlobMetadata(
                            import_status=ImportStatus.UNKNOWN_TYPE,
                            import_type=FileType.UNKNOWN,
                            import_request_id=str(request.upload_id),
                            import_sal_number=str(classification.sal_number),
                        ),
                    )
                    response.files_skipped += 1
                    response.results.append(
                        FileImportResult(
                            blob_path=blob.name,
                            classified_as="unknown",
                            sal_number=classification.sal_number,
                            status=ImportStatus.SKIPPED,
                            error="Tipo file non riconosciuto",
                        )
                    )
                    BlobManager.cleanup_local_file(local_path)
                    continue

                classified_files.append(
                    (blob.name, classification, local_path)
                )

            # 4. Ordina per tipo di import
            classified_files = sort_files_by_import_order(classified_files)

            # 4b. Assicura che i record SAL esistano nel database
            sal_numbers = set(
                c.sal_number for _, c, _ in classified_files if c.sal_number > 0
            )
            for sal_num in sal_numbers:
                self._ensure_sal_exists(sal_num)

            # 5. Esegui import nell'ordine
            for blob_path, classification, local_path in classified_files:
                file_result = self._import_single_file(
                    blob_path, classification, local_path, str(request.upload_id)
                )
                response.results.append(file_result)

                if file_result.status == ImportStatus.SUCCESS:
                    response.files_processed += 1
                else:
                    response.files_failed += 1

                # Pulizia file temporaneo
                BlobManager.cleanup_local_file(local_path)

            # 6. Determina status complessivo
            if response.files_failed == 0:
                response.status = ImportStatus.COMPLETED
            elif response.files_processed > 0:
                response.status = ImportStatus.PARTIAL
            else:
                response.status = ImportStatus.FAILED

        except Exception as e:
            logger.exception(
                "Errore fatale durante elaborazione cartella '%s': %s",
                request.folder_path, e,
            )
            response.status = ImportStatus.FAILED

        response.processing_time_seconds = time.time() - start_time
        logger.info(
            "=== Fine elaborazione richiesta %s: "
            "found=%d, processed=%d, skipped=%d, failed=%d, time=%.1fs ===",
            request.upload_id,
            response.files_found,
            response.files_processed,
            response.files_skipped,
            response.files_failed,
            response.processing_time_seconds,
        )
        return response

    def _ensure_sal_exists(self, sal_number: int) -> None:
        """
        Assicura che il record SAL esista nella tabella `sal`.
        Se mancante, lo crea con id_sal = numero_sal e le date calcolate.
        """
        from utils.sal_manager import SALManager

        conn = None
        try:
            conn = mysql.connector.connect(
                host=self.db_config["host"],
                port=self.db_config["port"],
                user=self.db_config["user"],
                password=self.db_config["password"],
                database=self.db_config["database"],
            )
            cursor = conn.cursor()

            cursor.execute(
                "SELECT id_sal FROM sal WHERE id_sal = %s", (sal_number,)
            )
            if cursor.fetchone():
                logger.debug("SAL %d gia' presente nel database", sal_number)
                return

            data_inizio, data_fine = SALManager.get_periodo_sal(sal_number)

            cursor.execute(
                "INSERT INTO sal (id_sal, numero_sal, data_inizio, data_fine) "
                "VALUES (%s, %s, %s, %s)",
                (sal_number, sal_number, data_inizio, data_fine),
            )
            conn.commit()
            logger.info(
                "Creato record SAL %d: %s - %s",
                sal_number, data_inizio, data_fine,
            )

        except Exception as e:
            logger.error("Errore creazione record SAL %d: %s", sal_number, e)

        finally:
            if conn:
                conn.close()

    def _import_single_file(
        self,
        blob_path: str,
        classification: ClassificationResult,
        local_path: str,
        request_id: str,
    ) -> FileImportResult:
        """Esegue l'import di un singolo file classificato."""
        logger.info(
            "Import file '%s': tipo=%s, SAL=%d (confidence=%.2f)",
            blob_path,
            classification.file_type.value,
            classification.sal_number,
            classification.confidence,
        )

        adapter = get_importer(
            classification.file_type,
            classification.sal_number,
            self.db_config,
        )

        if adapter is None:
            self.blob_manager.set_blob_metadata(
                blob_path,
                BlobMetadata(
                    import_status=ImportStatus.FAILED,
                    import_type=classification.file_type,
                    import_request_id=request_id,
                    import_sal_number=str(classification.sal_number),
                    import_error="Nessun adapter disponibile",
                ),
            )
            return FileImportResult(
                blob_path=blob_path,
                classified_as=classification.file_type.value,
                sal_number=classification.sal_number,
                status=ImportStatus.FAILED,
                error="Nessun adapter disponibile",
            )

        try:
            import_result = adapter.run_import(local_path)

            status = (
                ImportStatus.SUCCESS if import_result.success else ImportStatus.FAILED
            )

            # Tagga il blob
            self.blob_manager.set_blob_metadata(
                blob_path,
                BlobMetadata(
                    import_status=status,
                    import_type=classification.file_type,
                    import_request_id=request_id,
                    import_sal_number=str(classification.sal_number),
                    import_error=import_result.error_message or "",
                ),
            )

            # Converti dettagli errori da dict a RowErrorDetail
            error_details = [
                RowErrorDetail(
                    row_number=d.get("row_number", 0),
                    sheet_name=d.get("sheet_name", ""),
                    error_type=d.get("error_type", ""),
                    message=d.get("message", ""),
                    row_data=d.get("row_data"),
                )
                for d in import_result.error_details
            ]

            # Converti warnings da dict a ImportWarning
            warnings = [
                ImportWarning(
                    category=w.get("category", ""),
                    message=w.get("message", ""),
                    count=w.get("count", 0),
                )
                for w in import_result.warnings
            ]

            return FileImportResult(
                blob_path=blob_path,
                classified_as=classification.file_type.value,
                sal_number=classification.sal_number,
                status=status,
                error=import_result.error_message,
                rows_imported=import_result.rows_imported,
                rows_errors=import_result.rows_errors,
                error_details=error_details,
                warnings=warnings,
                errors_total=import_result.errors_total,
            )

        except Exception as e:
            error_msg = f"Eccezione durante import: {e}"
            logger.exception(error_msg)

            self.blob_manager.set_blob_metadata(
                blob_path,
                BlobMetadata(
                    import_status=ImportStatus.FAILED,
                    import_type=classification.file_type,
                    import_request_id=request_id,
                    import_sal_number=str(classification.sal_number),
                    import_error=error_msg,
                ),
            )

            return FileImportResult(
                blob_path=blob_path,
                classified_as=classification.file_type.value,
                sal_number=classification.sal_number,
                status=ImportStatus.FAILED,
                error=error_msg,
            )
