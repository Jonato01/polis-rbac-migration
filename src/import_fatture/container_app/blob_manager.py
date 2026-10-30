"""
Gestione Azure Blob Storage: scan cartelle, download file, metadata tagging.
"""
import os
import logging
from typing import Optional

from azure.storage.blob import BlobServiceClient, BlobProperties

from container_app.azure_clients import get_blob_service_client
from container_app.config import (
    AZURE_STORAGE_CONTAINER_NAME,
    TEMP_DOWNLOAD_DIR,
)
from container_app.models import BlobMetadata

logger = logging.getLogger("container_app.blob_manager")


class BlobManager:
    def __init__(
        self,
        container_name: str = AZURE_STORAGE_CONTAINER_NAME,
        blob_service_client: Optional[BlobServiceClient] = None,
    ):
        self.container_name = container_name
        # Identity-first: AZURE_STORAGE_ACCOUNT_NAME + managed identity,
        # fallback legacy AZURE_STORAGE_CONNECTION_STRING (vedi azure_clients.py)
        self.blob_service_client = blob_service_client or get_blob_service_client(
            account_name_var="AZURE_STORAGE_ACCOUNT_NAME",
            conn_str_var="AZURE_STORAGE_CONNECTION_STRING",
        )
        self.container_client = self.blob_service_client.get_container_client(
            container_name
        )
        logger.info(
            "BlobManager inizializzato per container '%s'", container_name
        )

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    def list_excel_files(self, folder_path: str) -> list[BlobProperties]:
        """
        Elenca ricorsivamente i file .xlsx/.xls in una cartella blob,
        escludendo i file temporanei Excel (~$...).
        """
        # Normalizza il prefisso: rimuovi slash iniziali, aggiungi finale
        prefix = folder_path.strip("/") + "/"
        blobs: list[BlobProperties] = []

        for blob in self.container_client.list_blobs(name_starts_with=prefix):
            nome = blob.name.lower()
            if not (nome.endswith(".xlsx") or nome.endswith(".xls")):
                continue
            # Escludi file temporanei Excel
            basename = os.path.basename(blob.name)
            if basename.startswith("~$"):
                continue
            blobs.append(blob)

        logger.info(
            "Trovati %d file Excel in '%s'", len(blobs), folder_path
        )
        return blobs

    # ------------------------------------------------------------------
    # Metadata check
    # ------------------------------------------------------------------

    def is_already_processed(self, blob: BlobProperties) -> bool:
        """Controlla se il blob ha metadata import_status='success'."""
        blob_client = self.container_client.get_blob_client(blob.name)
        try:
            props = blob_client.get_blob_properties()
            metadata = props.metadata or {}
        except Exception:
            return False
        return metadata.get("import_status") == "success"

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download_blob(
        self, blob_path: str, local_dir: str = TEMP_DOWNLOAD_DIR
    ) -> str:
        """
        Scarica un blob in una directory locale.
        Mantiene il nome originale del file (necessario per personale che
        estrae il SAL dal nome file).
        Restituisce il percorso locale completo.
        """
        os.makedirs(local_dir, exist_ok=True)
        local_filename = os.path.basename(blob_path)
        local_path = os.path.join(local_dir, local_filename)

        blob_client = self.container_client.get_blob_client(blob_path)
        with open(local_path, "wb") as f:
            data = blob_client.download_blob()
            data.readinto(f)

        logger.info("Scaricato '%s' -> '%s'", blob_path, local_path)
        return local_path

    # ------------------------------------------------------------------
    # Metadata tagging
    # ------------------------------------------------------------------

    def set_blob_metadata(
        self, blob_path: str, metadata: BlobMetadata
    ) -> None:
        """Scrive i metadata di risultato su un blob."""
        blob_client = self.container_client.get_blob_client(blob_path)
        meta_dict = metadata.to_metadata()
        blob_client.set_blob_metadata(meta_dict)
        logger.info(
            "Metadata impostati su '%s': status=%s, type=%s",
            blob_path,
            metadata.import_status.value,
            metadata.import_type.value,
        )

    # ------------------------------------------------------------------
    # Preview (per classificazione AI)
    # ------------------------------------------------------------------

    def get_blob_content_preview(
        self, blob_path: str, max_bytes: int = 32768
    ) -> bytes:
        """
        Scarica i primi N byte di un blob per la classificazione AI.
        Utile per leggere gli header Excel senza scaricare tutto il file.
        """
        blob_client = self.container_client.get_blob_client(blob_path)
        stream = blob_client.download_blob(offset=0, length=max_bytes)
        return stream.readall()

    # ------------------------------------------------------------------
    # Pulizia
    # ------------------------------------------------------------------

    @staticmethod
    def cleanup_local_file(local_path: str) -> None:
        """Rimuove un file locale temporaneo."""
        try:
            if os.path.exists(local_path):
                os.remove(local_path)
                logger.debug("Rimosso file temporaneo: %s", local_path)
        except OSError as e:
            logger.warning(
                "Impossibile rimuovere '%s': %s", local_path, e
            )
