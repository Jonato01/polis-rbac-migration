"""
Adapter per import colonnine (stazioni di ricarica).
Usa import_colonnine.ImportatoreColonnine con AI mapping via Azure AI bridge.
"""
import logging

from .base_adapter import BaseAdapter, ImportResult

logger = logging.getLogger("container_app.adapters.colonnine")


class ColonnineAdapter(BaseAdapter):
    def run_import(self, file_path: str) -> ImportResult:
        self._log_start(file_path, "colonnine")

        try:
            from import_colonnine import ImportatoreColonnine
        except ImportError as e:
            msg = f"Impossibile importare ImportatoreColonnine: {e}"
            logger.error(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "colonnine")
            return result

        try:
            from container_app.azure_ai_bridge import call_ai
        except ImportError:
            call_ai = None
            logger.warning("azure_ai_bridge non disponibile, AI mapping disabilitato")

        importer = None

        try:
            importer = ImportatoreColonnine(
                self.db_config["host"],
                self.db_config["port"],
                self.db_config["user"],
                self.db_config["password"],
                self.db_config["database"],
                ai_caller=call_ai,
            )

            if not importer.connetti_db():
                result = ImportResult(
                    success=False,
                    error_message="Connessione DB fallita",
                )
                self._log_end(result, "colonnine")
                return result

            importer.carica_cache()

            success = importer.importa_excel(file_path, self.sal_number)

            rows_imported = (
                importer.stats.get("righe_inserite", 0)
                + importer.stats.get("righe_aggiornate", 0)
            )
            rows_errors = importer.stats.get("righe_errore", 0)

            # Statistiche come warnings
            warnings = []
            stats_mapping = [
                ("righe_aggiornate", "data_updated", "{count} record aggiornati"),
                ("id_pun_inseriti", "id_pun", "{count} ID Piattaforma inseriti"),
                ("bdc_inseriti", "bdc", "{count} Nr BdC inseriti"),
            ]
            for stat_key, category, msg_template in stats_mapping:
                count = importer.stats.get(stat_key, 0)
                if count > 0:
                    warnings.append({
                        "category": category,
                        "message": msg_template.format(count=count),
                        "count": count,
                    })

            error_message = None
            if not success:
                error_message = f"Import colonnine fallito (SAL {self.sal_number})"

            result = ImportResult(
                success=success,
                rows_imported=rows_imported,
                rows_errors=rows_errors,
                error_message=error_message,
                warnings=warnings,
            )
            self._log_end(result, "colonnine")
            return result

        except Exception as e:
            msg = f"Errore import colonnine SAL {self.sal_number}: {e}"
            logger.exception(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "colonnine")
            return result

        finally:
            if importer is not None:
                if hasattr(importer, "connection") and importer.connection:
                    try:
                        importer.connection.close()
                    except Exception:
                        pass
