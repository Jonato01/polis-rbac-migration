"""
Adapter per import gare.
Usa 'script gare.py' -> BatchGareImporter.

Adattamento necessario: BatchGareImporter.__init__() hardcoda le credenziali DB.
Creiamo una sottoclasse che accetta config esterna.
"""
import asyncio
import importlib
import importlib.util
import logging
import os

import mysql.connector

from .base_adapter import BaseAdapter, ImportResult

logger = logging.getLogger("container_app.adapters.gare")


def _load_gare_module():
    """
    Carica il modulo 'script gare.py' che ha un nome non valido come modulo Python.
    """
    project_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    file_path = os.path.join(project_root, "script gare.py")
    spec = importlib.util.spec_from_file_location("script_gare", file_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GareAdapter(BaseAdapter):
    def run_import(self, file_path: str) -> ImportResult:
        self._log_start(file_path, "gare")

        try:
            gare_mod = _load_gare_module()
            BatchGareImporter = gare_mod.BatchGareImporter
        except Exception as e:
            msg = f"Impossibile caricare modulo gare: {e}"
            logger.error(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "gare")
            return result

        try:
            # Creiamo l'importer ma override della connessione DB
            # senza modificare lo script originale.
            # BatchGareImporter.__init__ crea la connessione con credenziali
            # hardcoded. Creiamo l'oggetto e sostituiamo la connessione.
            importer = object.__new__(BatchGareImporter)
            importer.conn = mysql.connector.connect(
                host=self.db_config["host"],
                port=self.db_config["port"],
                user=self.db_config["user"],
                password=self.db_config["password"],
                database=self.db_config["database"],
                charset="utf8mb4",
                collation="utf8mb4_unicode_ci",
                ssl_disabled=False,
            )
            importer.conn.autocommit = True

            # Parse Excel
            rows = importer.parse_excel(file_path)
            if not rows:
                result = ImportResult(
                    success=True,
                    rows_imported=0,
                    error_message="Nessuna riga trovata nel file",
                )
                self._log_end(result, "gare")
                return result

            # Import to DB (async)
            filename = os.path.basename(file_path)
            asyncio.run(
                importer.import_to_db(rows, self.sal_number, filename)
            )

            result = ImportResult(
                success=True,
                rows_imported=len(rows),
                rows_errors=0,
            )
            self._log_end(result, "gare")
            return result

        except Exception as e:
            msg = f"Errore import gare SAL {self.sal_number}: {e}"
            logger.exception(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "gare")
            return result

        finally:
            try:
                if hasattr(importer, "conn") and importer.conn:
                    importer.conn.close()
            except Exception:
                pass
