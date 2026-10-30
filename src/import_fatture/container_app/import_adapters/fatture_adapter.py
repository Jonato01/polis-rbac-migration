"""
Adapter per import fatture.
Supporta SAL 1-8+ con routing verso lo script SAL-specifico.

Classi esistenti:
  SAL 1:   SAL-1.import_fatture_sal1.ImportatoreFattureSAL1
  SAL 2:   SAL-2.import_fatture_sal2.ImportatoreFattureSAL2
  SAL 3:   SAL-3.import_fatture_sal3.ImportatoreFattureSAL3
  SAL 4:   SAL-4.import_fatture_sal4.ImportatoreFattureSAL4
  SAL 5:   SAL-5.import_fatture_sal5.ImportatoreFattureSAL5
  SAL 6:   SAL-6.import_fatture_sal6.ImportatoreFattureSAL6
  SAL 7:   SAL-7.import_fatture_sal7.ImportatoreFattureSAL7
  SAL 8+:  SAL-8.import_fatture_sal8.ImportatoreFattureSAL7  (riutilizzato)

Tutte hanno: __init__(host, port, user, password, database)
             connetti_db() -> bool
             carica_cache()
             SAL 1-6: importa_excel_sicuro(file_path) -> bool
             SAL 7-8: importa_excel_sicuro(file_path, numero_sal=N) -> bool
             chiudi_connessione()
"""
import importlib
import importlib.util
import json
import logging
import os

import mysql.connector

from .base_adapter import BaseAdapter, ImportResult, MAX_ERROR_DETAILS_PER_FILE

logger = logging.getLogger("container_app.adapters.fatture")

# Mapping SAL -> (modulo, classe)
_SAL_MODULE_MAP = {
    1: ("SAL-1.import_fatture_sal1", "ImportatoreFattureSAL1"),
    2: ("SAL-2.import_fatture_sal2", "ImportatoreFattureSAL2"),
    3: ("SAL-3.import_fatture_sal3", "ImportatoreFattureSAL3"),
    4: ("SAL-4.import_fatture_sal4", "ImportatoreFattureSAL4"),
    5: ("SAL-5.import_fatture_sal5", "ImportatoreFattureSAL5"),
    6: ("SAL-6.import_fatture_sal6", "ImportatoreFattureSAL6"),
    7: ("SAL-7.import_fatture_sal7", "ImportatoreFattureSAL7"),
    8: ("SAL-8.import_fatture_sal8", "ImportatoreFattureSAL7"),
}


class FattureAdapter(BaseAdapter):
    def _read_session_counts(self, session_id: int | None) -> tuple[int, int]:
        """Legge righe_importate e righe_errore dalla tabella import_sessions."""
        if not session_id:
            return 0, 0

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
                "SELECT COALESCE(righe_importate, 0), COALESCE(righe_errore, 0) "
                "FROM import_sessions WHERE id_session = %s",
                (session_id,),
            )
            row = cursor.fetchone()
            if row:
                return row[0], row[1]
            return 0, 0
        except Exception as e:
            logger.warning("Impossibile leggere conteggi sessione %s: %s", session_id, e)
            return 0, 0
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def _read_error_details(self, session_id: int | None) -> tuple[list[dict], int]:
        """Legge i dettagli errori dalla tabella import_errors con connessione separata."""
        if not session_id:
            return [], 0

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

            # Conta totale errori
            cursor.execute(
                "SELECT COUNT(*) FROM import_errors WHERE id_session = %s",
                (session_id,),
            )
            errors_total = cursor.fetchone()[0]

            if errors_total == 0:
                return [], 0

            # Leggi dettagli (limitati)
            cursor.execute(
                "SELECT numero_riga, nome_foglio, tipo_errore, messaggio_errore, dati_riga "
                "FROM import_errors WHERE id_session = %s "
                "ORDER BY numero_riga LIMIT %s",
                (session_id, MAX_ERROR_DETAILS_PER_FILE),
            )
            rows = cursor.fetchall()

            details = []
            for row in rows:
                row_data = None
                if row[4]:
                    try:
                        row_data = json.loads(row[4])
                    except (json.JSONDecodeError, TypeError):
                        row_data = {"raw": str(row[4])[:200]}

                details.append({
                    "row_number": row[0] or 0,
                    "sheet_name": row[1] or "",
                    "error_type": row[2] or "",
                    "message": row[3] or "",
                    "row_data": row_data,
                })

            return details, errors_total

        except Exception as e:
            logger.warning("Impossibile leggere dettagli errori (session %s): %s", session_id, e)
            return [], 0

        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def run_import(self, file_path: str) -> ImportResult:
        self._log_start(file_path, "fatture")

        # Per SAL > 8, usa lo script SAL-8
        effective_sal = min(self.sal_number, 8)

        module_path, class_name = _SAL_MODULE_MAP.get(
            effective_sal, _SAL_MODULE_MAP[8]
        )

        # Import dinamico: "SAL-7.import_fatture_sal7" non e' un nome modulo
        # valido (contiene '-'), quindi usiamo importlib con il path
        # convertendo "SAL-7" in nome di package
        try:
            # I nomi con '-' non sono package Python validi.
            # Usiamo importlib.import_module con il nome reale del file.
            # PYTHONPATH=/app permette di risolvere i path.
            # Convertiamo "SAL-7.import_fatture_sal7" -> import dal path
            module_name = module_path.replace("-", "_")
            # Proviamo prima con il nome originale (se i folder sono accessibili)
            try:
                mod = importlib.import_module(module_path)
            except ModuleNotFoundError:
                # Fallback: carica direttamente il file
                project_root = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
                # "SAL-7.import_fatture_sal7" -> "SAL-7/import_fatture_sal7.py"
                parts = module_path.split(".")
                file_name = os.path.join(project_root, parts[0], parts[1] + ".py")
                spec = importlib.util.spec_from_file_location(module_name, file_name)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)

            # SAL 7+: patchiamo anthropic.Anthropic -> Azure AI Foundry.
            # SAL-7 lo importa a top-level, SAL-8 dentro _rileva_fogli_con_ai.
            # Patchiamo il modulo globale cosi' entrambi funzionano.
            if effective_sal >= 7:
                import anthropic as _anthropic_mod
                from container_app.azure_ai_bridge import AzureAIClient
                _anthropic_mod.Anthropic = AzureAIClient

            importer_class = getattr(mod, class_name)
        except Exception as e:
            msg = f"Impossibile caricare modulo {module_path}.{class_name}: {e}"
            logger.error(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "fatture")
            return result

        importer = None
        try:
            importer = importer_class(
                self.db_config["host"],
                self.db_config["port"],
                self.db_config["user"],
                self.db_config["password"],
                self.db_config["database"],
            )

            if not importer.connetti_db():
                result = ImportResult(
                    success=False,
                    error_message="Connessione DB fallita",
                )
                self._log_end(result, "fatture")
                return result

            importer.carica_cache()

            # SAL 7+: passano numero_sal; SAL 1-6: no
            if effective_sal >= 7:
                success = importer.importa_excel_sicuro(
                    file_path, numero_sal=self.sal_number
                )
            else:
                success = importer.importa_excel_sicuro(file_path)

            # Recupera session_id dall'error handler
            rows_imported = 0
            rows_errors = 0
            session_id = None
            if hasattr(importer, "error_handler") and importer.error_handler:
                eh = importer.error_handler
                if hasattr(eh, "session_id") and eh.session_id:
                    session_id = eh.session_id

            # Leggi conteggi dalla tabella import_sessions (scritti da aggiorna_sessione)
            if session_id:
                rows_imported, rows_errors = self._read_session_counts(session_id)

            # Fallback su attributi dell'importer (se presenti)
            if rows_imported == 0 and hasattr(importer, "righe_importate"):
                rows_imported = importer.righe_importate
            if rows_errors == 0 and hasattr(importer, "righe_errore"):
                rows_errors = importer.righe_errore

            # Leggi dettagli errori dalla tabella import_errors (connessione separata)
            error_details, errors_total = self._read_error_details(session_id)

            # Costruisci messaggio errore se success=False
            error_message = None
            if not success:
                if errors_total > 0:
                    error_message = f"Import fallito con {errors_total} errori"
                    if error_details:
                        primi_errori = "; ".join(
                            d["message"] for d in error_details[:3] if d.get("message")
                        )
                        if primi_errori:
                            error_message += f": {primi_errori}"
                elif rows_errors > 0:
                    error_message = f"Import fallito con {rows_errors} righe in errore"
                else:
                    error_message = "Import restituito fallito senza dettagli specifici"

            result = ImportResult(
                success=success,
                rows_imported=rows_imported,
                rows_errors=rows_errors,
                error_message=error_message,
                error_details=error_details,
                errors_total=errors_total,
            )
            self._log_end(result, "fatture")
            return result

        except Exception as e:
            msg = f"Errore import fatture SAL {self.sal_number}: {e}"
            logger.exception(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "fatture")
            return result

        finally:
            if importer is not None and hasattr(importer, "chiudi_connessione"):
                try:
                    importer.chiudi_connessione()
                except Exception:
                    pass
