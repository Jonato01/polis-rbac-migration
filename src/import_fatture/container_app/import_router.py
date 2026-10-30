"""
Routing: tipo file + numero SAL -> adapter di import corretto.
"""
import logging
from typing import Optional

from container_app.models import FileType
from container_app.import_adapters.base_adapter import BaseAdapter
from container_app.import_adapters.fatture_adapter import FattureAdapter
from container_app.import_adapters.personale_adapter import PersonaleAdapter
from container_app.import_adapters.servizi_adapter import ServiziAdapter
from container_app.import_adapters.gare_adapter import GareAdapter
from container_app.import_adapters.rendicontazione_adapter import RendicontazioneAdapter
from container_app.import_adapters.colonnine_adapter import ColonnineAdapter

logger = logging.getLogger("container_app.import_router")

# Ordine di import per una cartella SAL
IMPORT_ORDER = [
    FileType.FATTURE,
    FileType.PERSONALE,
    FileType.SERVIZI,
    FileType.GARE,
    FileType.COLONNINE,
    FileType.RENDICONTAZIONE,
]


def get_importer(
    file_type: FileType, sal_number: int, db_config: dict
) -> Optional[BaseAdapter]:
    """
    Restituisce l'adapter corretto per il tipo di file e il numero SAL.
    Restituisce None se il tipo e' unknown.
    """
    if file_type == FileType.FATTURE:
        return FattureAdapter(sal_number, db_config)

    elif file_type == FileType.PERSONALE:
        return PersonaleAdapter(sal_number, db_config)

    elif file_type == FileType.SERVIZI:
        return ServiziAdapter(sal_number, db_config)

    elif file_type == FileType.GARE:
        return GareAdapter(sal_number, db_config)

    elif file_type == FileType.COLONNINE:
        return ColonnineAdapter(sal_number, db_config)

    elif file_type == FileType.RENDICONTAZIONE:
        return RendicontazioneAdapter(sal_number, db_config)

    else:
        logger.warning(
            "Tipo file '%s' non supportato, nessun adapter disponibile",
            file_type.value,
        )
        return None


def sort_files_by_import_order(
    classified_files: list[tuple],
) -> list[tuple]:
    """
    Ordina i file classificati secondo l'ordine di import corretto:
    fatture -> personale -> servizi -> gare -> rendicontazione.

    Ogni elemento e' una tupla (blob_path, classification_result, local_path).
    """
    return sorted(
        classified_files,
        key=lambda x: x[1].file_type.import_order,
    )
