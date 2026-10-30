"""
Entry point per il container app.

- Configura logging e sys.path
- Inizializza blob_manager, queue_manager, orchestrator
- Avvia il loop di ascolto Service Bus
"""
import logging
import os
import sys

# Configura sys.path per importare gli script esistenti dalla root del progetto
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from container_app import config
from container_app.blob_manager import BlobManager
from container_app.queue_manager import QueueManager
from container_app.orchestrator import Orchestrator


def setup_logging() -> None:
    """Configura il logging per il container."""
    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Riduci verbosita' di librerie esterne
    logging.getLogger("azure").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    logger = logging.getLogger("container_app.main")

    logger.info("=" * 60)
    logger.info("POLIS Import Container App - Avvio")
    logger.info("=" * 60)

    # Crea directory temporanea
    os.makedirs(config.TEMP_DOWNLOAD_DIR, exist_ok=True)

    # Verifica configurazione essenziale
    missing = config.missing_required_vars()

    if missing:
        logger.error(
            "Variabili d'ambiente mancanti: %s", ", ".join(missing)
        )
        sys.exit(1)

    logger.info("DB: %s:%d/%s", config.DB_HOST, config.DB_PORT, config.DB_NAME)
    logger.info("Blob container: %s", config.AZURE_STORAGE_CONTAINER_NAME)
    logger.info("Input queue: %s", config.SERVICEBUS_INPUT_QUEUE)
    logger.info("Output queue: %s", config.SERVICEBUS_OUTPUT_QUEUE)
    logger.info("AI deployment: %s", config.AZURE_AI_DEPLOYMENT_NAME)

    # Inizializza componenti
    blob_manager = BlobManager()
    queue_manager = QueueManager()
    orchestrator = Orchestrator(blob_manager=blob_manager)

    logger.info("Componenti inizializzati. Avvio listener Service Bus...")

    try:
        queue_manager.start_listening(callback=orchestrator.process_folder)
    except KeyboardInterrupt:
        logger.info("Shutdown richiesto dall'utente")
    except Exception as e:
        logger.exception("Errore fatale nel listener: %s", e)
        sys.exit(1)
    finally:
        queue_manager.close()
        logger.info("Container App terminata")


if __name__ == "__main__":
    main()
