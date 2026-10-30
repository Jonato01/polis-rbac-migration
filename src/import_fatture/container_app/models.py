"""
Schemi dati per messaggi Service Bus, risultati import, e metadata blob.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import json


class ImportStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    PARTIAL = "partial"
    COMPLETED = "completed"
    UNKNOWN_TYPE = "unknown_type"


class FileType(str, Enum):
    FATTURE = "fatture"
    PERSONALE = "personale"
    SERVIZI = "servizi"
    GARE = "gare"
    RENDICONTAZIONE = "rendicontazione"
    COLONNINE = "colonnine"
    UNKNOWN = "unknown"

    # Ordine di import: fatture -> personale -> servizi -> gare -> colonnine -> rendicontazione
    @property
    def import_order(self) -> int:
        ordine = {
            FileType.FATTURE: 1,
            FileType.PERSONALE: 2,
            FileType.SERVIZI: 3,
            FileType.GARE: 4,
            FileType.COLONNINE: 5,
            FileType.RENDICONTAZIONE: 6,
            FileType.UNKNOWN: 99,
        }
        return ordine.get(self, 99)


@dataclass
class ImportRequest:
    """Messaggio ricevuto dalla coda Service Bus di input."""
    upload_id: int
    folder_path: str
    sal_number: int
    task: str = "import-excel"
    import_type: str = "excel"
    priority: str = "normal"
    force_reprocess: bool = False

    @classmethod
    def from_json(cls, data: str) -> "ImportRequest":
        parsed = json.loads(data)
        return cls(
            upload_id=int(parsed["upload_id"]),
            folder_path=parsed["folder_path"],
            sal_number=int(parsed["sal_number"]),
            task=parsed.get("task", "import-excel"),
            import_type=parsed.get("import_type", "excel"),
            priority=parsed.get("priority", "normal"),
            force_reprocess=parsed.get("force_reprocess", False),
        )

    def to_json(self) -> str:
        return json.dumps({
            "upload_id": self.upload_id,
            "folder_path": self.folder_path,
            "sal_number": self.sal_number,
            "task": self.task,
            "import_type": self.import_type,
            "priority": self.priority,
            "force_reprocess": self.force_reprocess,
        })


@dataclass
class ClassificationResult:
    """Risultato della classificazione AI di un file Excel."""
    file_type: FileType
    confidence: float
    sal_number: int
    reasoning: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.file_type.value,
            "confidence": self.confidence,
            "sal_number": self.sal_number,
            "reasoning": self.reasoning,
        }


@dataclass
class RowErrorDetail:
    """Dettaglio di un errore su una singola riga."""
    row_number: int
    sheet_name: str
    error_type: str
    message: str
    row_data: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {
            "row_number": self.row_number,
            "sheet_name": self.sheet_name,
            "error_type": self.error_type,
            "message": self.message,
        }
        if self.row_data:
            d["row_data"] = self.row_data
        return d


@dataclass
class ImportWarning:
    """Warning o statistica non-bloccante dell'import."""
    category: str
    message: str
    count: int = 0

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "message": self.message,
            "count": self.count,
        }


@dataclass
class FileImportResult:
    """Risultato dell'import di un singolo file."""
    blob_path: str
    classified_as: str
    sal_number: int
    status: ImportStatus
    error: Optional[str] = None
    rows_imported: int = 0
    rows_errors: int = 0
    error_details: list[RowErrorDetail] = field(default_factory=list)
    warnings: list[ImportWarning] = field(default_factory=list)
    errors_total: int = 0

    def to_dict(self) -> dict:
        d = {
            "blob_path": self.blob_path,
            "classified_as": self.classified_as,
            "sal_number": self.sal_number,
            "status": self.status.value,
            "error": self.error,
            "rows_imported": self.rows_imported,
            "rows_errors": self.rows_errors,
        }
        if self.error_details:
            d["error_details"] = [e.to_dict() for e in self.error_details]
            d["errors_truncated"] = self.errors_total > len(self.error_details)
            d["errors_total"] = self.errors_total
        if self.warnings:
            d["warnings"] = [w.to_dict() for w in self.warnings]
        return d


@dataclass
class ImportResponse:
    """Messaggio inviato sulla coda Service Bus di output."""
    upload_id: int
    import_type: str
    status: ImportStatus = ImportStatus.COMPLETED
    files_found: int = 0
    files_processed: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    results: list[FileImportResult] = field(default_factory=list)
    processing_time_seconds: float = 0.0

    def to_json(self) -> str:
        # Mappa status interno -> contratto output
        if self.status == ImportStatus.COMPLETED:
            output_status = "success"
        else:
            output_status = "completed_with_errors"

        # Costruisci lista file falliti con dettagli errore
        failed_files = [
            {
                "blob_path": r.blob_path,
                "classified_as": r.classified_as,
                "error": r.error,
                "rows_imported": r.rows_imported,
                "rows_errors": r.rows_errors,
            }
            for r in self.results
            if r.status == ImportStatus.FAILED
        ]

        total_row_errors = sum(r.rows_errors for r in self.results)
        total_rows_imported = sum(r.rows_imported for r in self.results)

        # Messaggi errore per visualizzazione rapida nel frontend
        error_messages = [
            f"[{r.classified_as}] {r.error}"
            for r in self.results
            if r.status == ImportStatus.FAILED and r.error
        ]

        # Dettagli per file (nuovo campo)
        file_results = [r.to_dict() for r in self.results]

        return json.dumps({
            "import_type": self.import_type,
            "upload_id": self.upload_id,
            "status": output_status,
            "summary": {
                "total_files": self.files_found,
                "success": self.files_processed,
                "failed": self.files_failed,
                "skipped": self.files_skipped,
                "errors": total_row_errors,
                "rows_imported": total_rows_imported,
                "error_messages": error_messages,
            },
            "file_results": file_results,
            "failed_files": failed_files,
        })


@dataclass
class BlobMetadata:
    """Metadata da scrivere sui blob dopo l'elaborazione."""
    import_status: ImportStatus
    import_type: FileType
    import_date: str = ""
    import_request_id: str = ""
    import_sal_number: str = ""
    import_error: str = ""

    def __post_init__(self):
        if not self.import_date:
            self.import_date = datetime.utcnow().isoformat() + "Z"

    def to_metadata(self) -> dict[str, str]:
        """Converte in dict per Azure Blob metadata (chiavi senza trattini)."""
        meta = {
            "import_status": self.import_status.value,
            "import_type": self.import_type.value,
            "import_date": self.import_date,
            "import_request_id": self.import_request_id,
            "import_sal_number": self.import_sal_number,
        }
        if self.import_error:
            meta["import_error"] = self.import_error[:256]
        return meta
