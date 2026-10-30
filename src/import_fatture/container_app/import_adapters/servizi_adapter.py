"""
Adapter per import servizi.
Usa import_servizi_api.ImportatoreServiziAPI con il metodo importa_da_file_locale.

L'anno viene calcolato dinamicamente tramite SALManager in base al numero SAL.
"""
import logging

from utils.sal_manager import SALManager
from .base_adapter import BaseAdapter, ImportResult

logger = logging.getLogger("container_app.adapters.servizi")


class ServiziAdapter(BaseAdapter):
    def run_import(self, file_path: str) -> ImportResult:
        self._log_start(file_path, "servizi")

        try:
            from import_servizi_api import ImportatoreServiziAPI
        except ImportError as e:
            msg = f"Impossibile importare ImportatoreServiziAPI: {e}"
            logger.error(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "servizi")
            return result

        anno = SALManager.get_mesi_sal(self.sal_number)[0]['anno']
        importer = None

        try:
            importer = ImportatoreServiziAPI(
                self.db_config["host"],
                self.db_config["port"],
                self.db_config["user"],
                self.db_config["password"],
                self.db_config["database"],
                dry_run=False,
            )

            if not importer.connetti_db():
                result = ImportResult(
                    success=False,
                    error_message="Connessione DB fallita",
                )
                self._log_end(result, "servizi")
                return result

            importer.carica_cache()

            success = importer.importa_da_file_locale(
                file_path, self.sal_number, anno
            )

            rows_imported = (
                importer.stats.get("dati_inseriti", 0)
                + importer.stats.get("dati_aggiornati", 0)
                + importer.stats.get("servizi_nuovi", 0)
            )
            rows_errors = 0

            # Estrai statistiche come warnings
            warnings = []
            stats_mapping = [
                ("dati_aggiornati", "data_updated", "{count} record aggiornati"),
                ("dati_saltati", "data_skipped", "{count} record saltati"),
                ("servizi_nuovi", "new_services", "{count} servizi nuovi creati"),
            ]
            for stat_key, category, msg_template in stats_mapping:
                count = importer.stats.get(stat_key, 0)
                if count > 0:
                    warnings.append({
                        "category": category,
                        "message": msg_template.format(count=count),
                        "count": count,
                    })

            # Costruisci messaggio errore se success=False
            error_message = None
            if not success:
                error_message = f"Import servizi fallito (SAL {self.sal_number}, anno {anno})"

            result = ImportResult(
                success=success,
                rows_imported=rows_imported,
                rows_errors=rows_errors,
                error_message=error_message,
                warnings=warnings,
            )
            self._log_end(result, "servizi")
            return result

        except Exception as e:
            msg = f"Errore import servizi SAL {self.sal_number}: {e}"
            logger.exception(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "servizi")
            return result

        finally:
            if importer is not None:
                if hasattr(importer, "connection") and importer.connection:
                    try:
                        importer.connection.close()
                    except Exception:
                        pass
