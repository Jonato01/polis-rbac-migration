"""
Configurazione root per AutoGen PDF Import
Carica variabili d'ambiente con valori di default
"""
import os
from dotenv import load_dotenv

# Carica .env se presente
load_dotenv()

# ============= AZURE BLOB STORAGE =============
# Identity-first: AZURE_STORAGE_ACCOUNT_NAME + managed identity (vedi azure_clients.py);
# AZURE_STORAGE_CONNECTION_STRING accettata come fallback legacy.
AZURE_STORAGE_ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "")
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_STORAGE_CONTAINER = os.getenv("AZURE_STORAGE_CONTAINER", "documenti-sal")

# ============= AZURE SERVICE BUS =============
# Identity-first: SERVICE_BUS_NAMESPACE + managed identity; connection string = fallback legacy.
SERVICE_BUS_NAMESPACE = os.getenv("SERVICE_BUS_NAMESPACE", "")
SERVICE_BUS_CONNECTION_STRING = os.getenv("SERVICE_BUS_CONNECTION_STRING", "")
INPUT_QUEUE = os.getenv("INPUT_QUEUE", "autogen-pdf-import")
WORK_QUEUE = os.getenv("WORK_QUEUE", "autogen-pdf-work")
RESPONSE_QUEUE = os.getenv("RESPONSE_QUEUE", "import-sal-orchestrator")

# ============= CLAUDE (Azure AI Foundry) =============
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY", "")
CLAUDE_BASE_URL = os.getenv("CLAUDE_BASE_URL", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5")

# ============= MISTRAL DOCUMENT AI =============
MISTRAL_DOC_ENDPOINT = os.getenv("MISTRAL_DOC_ENDPOINT", "")
MISTRAL_DOC_KEY = os.getenv("MISTRAL_DOC_KEY", "")
MISTRAL_RATE_LIMIT_GLOBAL = int(os.getenv("MISTRAL_RATE_LIMIT_GLOBAL", "600"))
MISTRAL_RATE_LIMIT_PER_INSTANCE = int(os.getenv("MISTRAL_RATE_LIMIT_PER_INSTANCE", "30"))


# ============= DATABASE MYSQL =============
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "polis")
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "3"))

# ============= SCALING =============
MAX_CONTAINER_INSTANCES = int(os.getenv("MAX_CONTAINER_INSTANCES", "20"))
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "50"))
MAX_CONCURRENT_IN_BATCH = int(os.getenv("MAX_CONCURRENT_IN_BATCH", "4"))
STALE_JOB_TIMEOUT_MINUTES = int(os.getenv("STALE_JOB_TIMEOUT_MINUTES", "20"))
MAX_STALE_RETRIES = int(os.getenv("MAX_STALE_RETRIES", "5"))

# ============= OCR QUALITY THRESHOLDS =============
OCR_QUALITY_THRESHOLD_GOOD = int(os.getenv("OCR_QUALITY_THRESHOLD_GOOD", "70"))
OCR_QUALITY_THRESHOLD_BEST = int(os.getenv("OCR_QUALITY_THRESHOLD_BEST", "85"))

# ============= RATE LIMITING =============
RATE_LIMIT_RETRY_BASE_DELAY = int(os.getenv("RATE_LIMIT_RETRY_BASE_DELAY", "2"))
RATE_LIMIT_RETRY_MAX_DELAY = int(os.getenv("RATE_LIMIT_RETRY_MAX_DELAY", "30"))
RATE_LIMIT_RETRY_MAX_ATTEMPTS = int(os.getenv("RATE_LIMIT_RETRY_MAX_ATTEMPTS", "3"))

# ============= CLASSIFICAZIONE =============
# Soglia di confidence per classificazione documenti
# Se la confidence AI è sotto questa soglia, il documento viene classificato come UNKNOWN
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))

# ============= ERROR LOG =============
ERROR_LOG_CONTAINER = os.getenv("ERROR_LOG_CONTAINER", "import-errors")


def missing_required_vars() -> list:
    """
    Variabili essenziali mancanti. Per storage e Service Bus basta una delle due
    alternative (identity oppure connection string legacy): la voce mancante viene
    riportata come coppia "A|B".
    """
    missing = []
    if not (AZURE_STORAGE_ACCOUNT_NAME or AZURE_STORAGE_CONNECTION_STRING):
        missing.append("AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING")
    if not (SERVICE_BUS_NAMESPACE or SERVICE_BUS_CONNECTION_STRING):
        missing.append("SERVICE_BUS_NAMESPACE|SERVICE_BUS_CONNECTION_STRING")
    return missing
