"""
Azure Blob Storage Service
Gestisce lettura/scrittura file e metadata su Azure Blob Storage
"""
import logging
import base64
from datetime import datetime
from typing import AsyncIterator

from azure.storage.blob.aio import BlobServiceClient, ContainerClient
from azure.storage.blob import BlobProperties

from azure_clients import get_blob_service_client_aio
from config import AZURE_STORAGE_CONTAINER

logger = logging.getLogger(__name__)


class BlobService:
    """
    Servizio per interazione con Azure Blob Storage
    """

    def __init__(self):
        self.container_name = AZURE_STORAGE_CONTAINER
        self._client: BlobServiceClient | None = None
        self._container: ContainerClient | None = None

    async def __aenter__(self):
        # Identity-first (AZURE_STORAGE_ACCOUNT_NAME), fallback connection string legacy
        self._client = get_blob_service_client_aio()
        self._container = self._client.get_container_client(self.container_name)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.close()

    async def list_files(
        self,
        folder_path: str,
        extensions: tuple[str, ...] = ('.pdf', '.p7m', '.docx')
    ) -> list[str]:
        """
        Lista tutti i file PDF/P7M in una cartella

        Args:
            folder_path: Path della cartella (es. "8_SAL_30_04_2025/documenti")
            extensions: Tuple di estensioni da filtrare

        Returns:
            Lista di path completi dei file
        """
        files = []

        async for blob in self._container.list_blobs(name_starts_with=folder_path):
            if blob.name.lower().endswith(extensions):
                files.append(blob.name)

        logger.info(f"Trovati {len(files)} file in {folder_path}")
        return files

    async def list_unprocessed_files(
        self,
        folder_path: str,
        extensions: tuple[str, ...] = ('.pdf', '.p7m', '.docx')
    ) -> list[str]:
        """
        Lista solo i file non ancora processati (metadata processed != true)

        Args:
            folder_path: Path della cartella
            extensions: Tuple di estensioni da filtrare

        Returns:
            Lista di path dei file non processati
        """
        unprocessed = []

        async for blob in self._container.list_blobs(
            name_starts_with=folder_path,
            include=['metadata']
        ):
            if not blob.name.lower().endswith(extensions):
                continue

            metadata = blob.metadata or {}
            if metadata.get("processed") != "true":
                unprocessed.append(blob.name)

        logger.info(f"Trovati {len(unprocessed)} file non processati in {folder_path}")
        return unprocessed

    async def list_unknown_files(
        self,
        folder_path: str,
        extensions: tuple[str, ...] = ('.pdf', '.p7m', '.docx')
    ) -> list[str]:
        """
        Lista solo i file classificati come UNKNOWN (da riprocessare).

        Args:
            folder_path: Path della cartella
            extensions: Tuple di estensioni da filtrare

        Returns:
            Lista di path dei file con document_type=UNKNOWN
        """
        unknown = []

        async for blob in self._container.list_blobs(
            name_starts_with=folder_path,
            include=['metadata']
        ):
            if not blob.name.lower().endswith(extensions):
                continue

            metadata = blob.metadata or {}
            if metadata.get("document_type") == "UNKNOWN":
                unknown.append(blob.name)

        logger.info(f"Trovati {len(unknown)} file UNKNOWN in {folder_path}")
        return unknown

    async def list_files_by_doc_type(
        self,
        folder_path: str,
        doc_type: str,
        extensions: tuple[str, ...] = ('.pdf', '.p7m', '.docx'),
        date_from: str = None,
        date_to: str = None
    ) -> list[str]:
        """
        Lista solo i file con metadata document_type == doc_type,
        opzionalmente filtrati per range di processed_date.

        Args:
            folder_path: Path della cartella
            doc_type: Tipo documento da filtrare (es. "FATTURA")
            extensions: Tuple di estensioni da filtrare
            date_from: Data inizio range ISO (es. "2026-02-20T10:00:00")
            date_to: Data fine range ISO (es. "2026-02-26T18:30:00")

        Returns:
            Lista di path dei file con quel document_type
        """
        # Pre-parse date filter bounds
        dt_from = datetime.fromisoformat(date_from) if date_from else None
        dt_to = datetime.fromisoformat(date_to) if date_to else None
        has_date_filter = dt_from is not None or dt_to is not None

        if has_date_filter:
            logger.info(
                f"Filtro date attivo: {date_from or '*'} -> {date_to or '*'}"
            )

        matched = []

        async for blob in self._container.list_blobs(
            name_starts_with=folder_path,
            include=['metadata']
        ):
            if not blob.name.lower().endswith(extensions):
                continue

            metadata = blob.metadata or {}
            if metadata.get("document_type") != doc_type:
                continue

            # Date range filter
            if has_date_filter:
                processed_date_str = metadata.get("processed_date")
                if not processed_date_str:
                    continue

                try:
                    processed_date = datetime.fromisoformat(processed_date_str)
                except ValueError:
                    continue

                if dt_from and processed_date < dt_from:
                    continue
                if dt_to and processed_date > dt_to:
                    continue

            matched.append(blob.name)

        logger.info(
            f"Trovati {len(matched)} file con document_type={doc_type} in {folder_path}"
            + (f" (date range: {date_from} -> {date_to})" if has_date_filter else "")
        )
        return matched

    async def download_file(self, blob_name: str) -> bytes:
        """
        Scarica il contenuto di un blob

        Args:
            blob_name: Nome completo del blob

        Returns:
            Contenuto del file in bytes
        """
        blob_client = self._container.get_blob_client(blob_name)
        download = await blob_client.download_blob()
        content = await download.readall()

        logger.debug(f"Scaricato {blob_name}: {len(content)} bytes")
        return content

    async def download_file_base64(self, blob_name: str) -> str:
        """
        Scarica il contenuto di un blob e lo converte in base64

        Args:
            blob_name: Nome completo del blob

        Returns:
            Contenuto del file in base64
        """
        content = await self.download_file(blob_name)
        return base64.b64encode(content).decode('utf-8')

    async def get_blob_metadata(self, blob_name: str) -> dict:
        """
        Recupera i metadata di un blob

        Args:
            blob_name: Nome completo del blob

        Returns:
            Dictionary dei metadata
        """
        blob_client = self._container.get_blob_client(blob_name)
        properties = await blob_client.get_blob_properties()
        return properties.metadata or {}

    async def set_blob_metadata(self, blob_name: str, metadata: dict):
        """
        Imposta i metadata di un blob

        Args:
            blob_name: Nome completo del blob
            metadata: Dictionary dei metadata da impostare
        """
        blob_client = self._container.get_blob_client(blob_name)
        await blob_client.set_blob_metadata(metadata)
        logger.debug(f"Metadata aggiornati per {blob_name}")

    async def check_and_mark_processing(self, blob_name: str) -> bool:
        """
        Controlla se blob già elaborato e lo marca come "in elaborazione"

        Args:
            blob_name: Nome completo del blob

        Returns:
            True se il file può essere processato, False se già processato
        """
        blob_client = self._container.get_blob_client(blob_name)

        # Leggi metadata esistenti
        properties = await blob_client.get_blob_properties()
        metadata = properties.metadata or {}

        # Controlla se già processato
        if metadata.get("processed") == "true":
            logger.info(f"Blob {blob_name} già elaborato, skip")
            return False

        # Marca come "in elaborazione" (lock ottimistico)
        await blob_client.set_blob_metadata({
            **metadata,
            "processing": "true",
            "processing_started": datetime.now().isoformat()
        })

        return True

    async def mark_blob_completed(
        self,
        blob_name: str,
        doc_type: str,
        success: bool,
        error_msg: str | None = None
    ):
        """
        Marca blob come elaborato con esito

        Args:
            blob_name: Nome completo del blob
            doc_type: Tipo documento rilevato
            success: True se elaborazione riuscita
            error_msg: Messaggio di errore (se presente)
        """
        blob_client = self._container.get_blob_client(blob_name)

        metadata = {
            "processed": "true",
            "processed_date": datetime.now().isoformat(),
            "document_type": doc_type,
            "import_status": "success" if success else "error",
            "processing": "false"
        }

        if error_msg:
            # Tronca errore se troppo lungo (metadata max 8KB)
            metadata["error_message"] = error_msg[:500]

        await blob_client.set_blob_metadata(metadata)
        logger.info(f"Blob {blob_name} marcato come {'success' if success else 'error'}")

    async def mark_blob_unknown(self, blob_name: str):
        """
        Marca blob come documento non riconosciuto

        Args:
            blob_name: Nome completo del blob
        """
        blob_client = self._container.get_blob_client(blob_name)

        await blob_client.set_blob_metadata({
            "processed": "true",
            "processed_date": datetime.now().isoformat(),
            "document_type": "UNKNOWN",
            "import_status": "skipped",
            "error_message": "Documento non classificabile in nessuna categoria"
        })

        logger.warning(f"Blob {blob_name} marcato come UNKNOWN")

    async def mark_blob_failed(
        self,
        blob_name: str,
        doc_type: str,
        error_msg: str
    ):
        """
        Marca blob come fallito MA non come processed (per retry futuro)

        Args:
            blob_name: Nome completo del blob
            doc_type: Tipo documento rilevato
            error_msg: Messaggio di errore
        """
        blob_client = self._container.get_blob_client(blob_name)

        await blob_client.set_blob_metadata({
            "processed": "false",
            "last_attempt": datetime.now().isoformat(),
            "document_type": doc_type,
            "import_status": "failed",
            "error_message": error_msg[:500] if error_msg else "Unknown error"
        })

        logger.error(f"Blob {blob_name} marcato come failed (retry possibile)")