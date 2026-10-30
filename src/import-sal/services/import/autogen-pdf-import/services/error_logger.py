"""
Error Logger Service
Registra errori di import in file JSON su Blob Storage
"""
import json
import logging
from datetime import datetime

from azure_clients import get_blob_service_client
from config import ERROR_LOG_CONTAINER

logger = logging.getLogger(__name__)


class ErrorLogger:
    """
    Servizio per logging errori su Blob Storage
    """

    def __init__(self):
        # Identity-first (AZURE_STORAGE_ACCOUNT_NAME), fallback connection string legacy
        self.blob_service = get_blob_service_client()

    def log_import_error(
        self,
        blob_name: str,
        doc_type: str,
        sal_number: int,
        error_type: str,
        error_message: str,
        json_extracted: dict = None,
        query_attempted: str = None
    ):
        """
        Registra errore di import in file JSON su Blob Storage

        Args:
            blob_name: Nome del blob che ha causato l'errore
            doc_type: Tipo documento rilevato
            sal_number: Numero SAL
            error_type: Tipo errore (ocr_failed, json_malformed, insert_failed, etc.)
            error_message: Messaggio di errore
            json_extracted: Dati JSON estratti (opzionale)
            query_attempted: Query SQL tentata (opzionale)
        """
        error_record = {
            "timestamp": datetime.now().isoformat(),
            "file": blob_name,
            "document_type": doc_type,
            "sal_number": sal_number,
            "error_type": error_type,
            "error_message": error_message,
            "json_extracted": json_extracted,
            "query_attempted": query_attempted[:2000] if query_attempted else None
        }

        # Costruisci path per blob errore
        date_str = datetime.now().strftime('%Y%m%d')
        filename = blob_name.split('/')[-1]
        error_blob_name = f"errors/{sal_number}/{date_str}/{filename}.error.json"

        try:
            blob_client = self.blob_service.get_blob_client(
                container=ERROR_LOG_CONTAINER,
                blob=error_blob_name
            )

            blob_client.upload_blob(
                json.dumps(error_record, indent=2, ensure_ascii=False),
                overwrite=True
            )

            logger.info(f"Errore import salvato: {error_blob_name}")

        except Exception as e:
            logger.error(f"Impossibile salvare errore su blob: {e}")
            # Log comunque su console
            logger.error(f"ERRORE IMPORT: {json.dumps(error_record, indent=2)}")


# Tipi di errore standard
class ErrorTypes:
    OCR_FAILED = "ocr_failed"
    JSON_MALFORMED = "json_malformed"
    INSERT_FAILED = "insert_failed"
    UNKNOWN_TYPE = "unknown_type"
    P7M_EXTRACTION_FAILED = "p7m_extraction_failed"
    VALIDATION_FAILED = "validation_failed"
    DOWNLOAD_FAILED = "download_failed"
    FILE_CORRUPTED = "file_corrupted"


# Istanza globale
error_logger = ErrorLogger()

