"""
Interfaccia comune per tutti gli adapter di import.
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("container_app.adapters")

# Limite errori dettagliati per file (Service Bus max 256KB)
MAX_ERROR_DETAILS_PER_FILE = 50


@dataclass
class ImportResult:
    """Risultato dell'esecuzione di un import."""
    success: bool
    rows_imported: int = 0
    rows_errors: int = 0
    error_message: Optional[str] = None
    error_details: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors_total: int = 0


class BaseAdapter(ABC):
    """
    Classe base per tutti gli adapter.
    Ogni adapter wrappa uno degli script di import esistenti.
    """

    def __init__(self, sal_number: int, db_config: dict):
        self.sal_number = sal_number
        self.db_config = db_config

    @abstractmethod
    def run_import(self, file_path: str) -> ImportResult:
        """Esegue l'import e restituisce il risultato."""
        ...

    def _log_start(self, file_path: str, tipo: str) -> None:
        logger.info(
            "Avvio import %s: file='%s', SAL=%d",
            tipo, file_path, self.sal_number,
        )

    def _log_end(self, result: ImportResult, tipo: str) -> None:
        if result.success:
            logger.info(
                "Import %s completato: %d righe importate, %d errori",
                tipo, result.rows_imported, result.rows_errors,
            )
        else:
            logger.error(
                "Import %s fallito: %s",
                tipo, result.error_message,
            )
