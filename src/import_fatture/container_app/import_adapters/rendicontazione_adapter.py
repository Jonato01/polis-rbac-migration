"""
Adapter per import rendicontazione.
Usa import_rendicontazione.py (funzionale, no classi).

Adattamento necessario:
- Lo script usa OpenAI client hardcoded e SQLAlchemy engine hardcoded.
- Monkey-patchiamo il modulo importato per sostituire:
  1. `chiedi_ai()` con versione che usa Azure AI (Opus 4.6)
  2. `engine` con SQLAlchemy engine dalle env vars
"""
import logging
import os

from sqlalchemy import create_engine

from container_app import config
from container_app.azure_ai_bridge import call_ai
from .base_adapter import BaseAdapter, ImportResult

logger = logging.getLogger("container_app.adapters.rendicontazione")


def _create_chiedi_ai_replacement() -> callable:
    """
    Crea una funzione sostitutiva per chiedi_ai() che usa
    Opus 4.6 su Azure AI tramite il bridge diretto.
    """
    def chiedi_ai_azure(prompt: str) -> str:
        """Sostituto di chiedi_ai() che usa Azure AI."""
        return call_ai(prompt)

    return chiedi_ai_azure


class RendicontazioneAdapter(BaseAdapter):
    def run_import(self, file_path: str) -> ImportResult:
        self._log_start(file_path, "rendicontazione")

        try:
            import import_rendicontazione as rend_mod
        except ImportError as e:
            msg = f"Impossibile importare import_rendicontazione: {e}"
            logger.error(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "rendicontazione")
            return result

        # --- Monkey-patch: sostituisci chiedi_ai ---
        original_chiedi_ai = rend_mod.chiedi_ai
        rend_mod.chiedi_ai = _create_chiedi_ai_replacement()

        # --- Monkey-patch: sostituisci engine SQLAlchemy ---
        original_engine = rend_mod.engine
        rend_mod.engine = create_engine(config.get_db_connection_string())

        # --- Monkey-patch: forza id_sal corretto ---
        # estrai_info_file() usa l'AI per estrarre id_sal dal nome file,
        # ma spesso sbaglia (None o SAL errato). Noi lo conosciamo gia'
        # dalla classificazione, quindi forziamo il valore corretto.
        original_estrai_info = rend_mod.estrai_info_file
        sal_number = self.sal_number

        def estrai_info_con_sal_corretto(nome_file, path_cartella):
            info = original_estrai_info(nome_file, path_cartella)
            info['id_sal'] = sal_number
            # Normalizza linea_intervento (la funzione e' gia' chiamata
            # in estrai_info_file, ma rinforziamo per sicurezza)
            if hasattr(rend_mod, 'normalizza_linea_intervento'):
                info['linea_intervento'] = rend_mod.normalizza_linea_intervento(
                    info.get('linea_intervento', '')
                )
            return info

        rend_mod.estrai_info_file = estrai_info_con_sal_corretto

        try:
            folder_path = os.path.dirname(file_path)

            rows = rend_mod.importa_excel(file_path, folder_path) or 0

            result = ImportResult(
                success=True,
                rows_imported=rows,
                rows_errors=0,
            )
            self._log_end(result, "rendicontazione")
            return result

        except Exception as e:
            msg = f"Errore import rendicontazione SAL {self.sal_number}: {e}"
            logger.exception(msg)
            result = ImportResult(success=False, error_message=msg)
            self._log_end(result, "rendicontazione")
            return result

        finally:
            # Ripristina le funzioni originali per sicurezza
            rend_mod.chiedi_ai = original_chiedi_ai
            rend_mod.engine = original_engine
            rend_mod.estrai_info_file = original_estrai_info
