"""
Configurazione centralizzata per il container app.
Tutte le variabili d'ambiente in un unico posto.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# --- Database MySQL ---
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "polis")

# --- Azure Blob Storage ---
# Autenticazione: AZURE_STORAGE_ACCOUNT_NAME + managed identity (preferita);
# AZURE_STORAGE_CONNECTION_STRING resta accettata come fallback legacy.
AZURE_STORAGE_ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "")
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_STORAGE_CONTAINER_NAME = os.getenv("AZURE_STORAGE_CONTAINER_NAME", "import-files")

# --- Azure Service Bus ---
# Autenticazione: AZURE_SERVICEBUS_NAMESPACE (FQDN o nome) + managed identity (preferita);
# AZURE_SERVICEBUS_CONNECTION_STRING resta accettata come fallback legacy.
AZURE_SERVICEBUS_NAMESPACE = os.getenv("AZURE_SERVICEBUS_NAMESPACE", "")
AZURE_SERVICEBUS_CONNECTION_STRING = os.getenv("AZURE_SERVICEBUS_CONNECTION_STRING", "")
SERVICEBUS_INPUT_QUEUE = os.getenv("SERVICEBUS_INPUT_QUEUE", "import-requests")
SERVICEBUS_OUTPUT_QUEUE = os.getenv("SERVICEBUS_OUTPUT_QUEUE", "import-results")

# --- Azure AI (Opus 4.6 via AutoGen) ---
AZURE_AI_ENDPOINT = os.getenv("AZURE_AI_ENDPOINT", "")
AZURE_AI_DEPLOYMENT_NAME = os.getenv("AZURE_AI_DEPLOYMENT_NAME", "claude-opus-46")
AZURE_AI_API_KEY = os.getenv("AZURE_AI_API_KEY", "")
AZURE_AI_API_VERSION = os.getenv("AZURE_AI_API_VERSION", "2024-12-01-preview")

# --- Configurazione app ---
TEMP_DOWNLOAD_DIR = os.getenv("TEMP_DOWNLOAD_DIR", "/tmp/imports")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


def missing_required_vars() -> list:
    """
    Variabili d'ambiente essenziali mancanti. Per storage e Service Bus basta una delle
    due alternative (identity oppure connection string legacy): in tal caso la voce
    mancante viene riportata come coppia "A|B".
    """
    missing = []
    if not (AZURE_STORAGE_ACCOUNT_NAME or AZURE_STORAGE_CONNECTION_STRING):
        missing.append("AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING")
    if not (AZURE_SERVICEBUS_NAMESPACE or AZURE_SERVICEBUS_CONNECTION_STRING):
        missing.append("AZURE_SERVICEBUS_NAMESPACE|AZURE_SERVICEBUS_CONNECTION_STRING")
    if not AZURE_AI_API_KEY:
        missing.append("AZURE_AI_API_KEY")
    if not AZURE_AI_ENDPOINT:
        missing.append("AZURE_AI_ENDPOINT")
    return missing


def get_db_config() -> dict:
    """Restituisce la configurazione DB come dizionario."""
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "database": DB_NAME,
    }


def get_db_connection_string() -> str:
    """Restituisce la connection string SQLAlchemy per il DB."""
    return (
        f"mysql+mysqlconnector://{DB_USER}:{DB_PASSWORD}"
        f"@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    )
