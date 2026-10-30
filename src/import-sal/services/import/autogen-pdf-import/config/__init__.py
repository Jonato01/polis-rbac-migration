"""
Config package per AutoGen PDF Import

La configurazione dei tipi documento viene caricata dal DATABASE.
I campi delle tabelle vengono scoperti automaticamente via DESCRIBE.

Per aggiungere un nuovo tipo documento:
1. Creare la tabella destinazione nel DB (con commenti sulle colonne)
2. Inserire record in autogen_document_types
3. NESSUNA modifica al codice Python necessaria!
"""
import sys
import os

# Aggiungi la directory parent al path per permettere l'import di config_root.py
parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Import delle variabili di configurazione da config_root.py
import config_root as root_config

# Esporta tutte le variabili di configurazione
AZURE_STORAGE_ACCOUNT_NAME = root_config.AZURE_STORAGE_ACCOUNT_NAME
AZURE_STORAGE_CONNECTION_STRING = root_config.AZURE_STORAGE_CONNECTION_STRING
AZURE_STORAGE_CONTAINER = root_config.AZURE_STORAGE_CONTAINER
SERVICE_BUS_NAMESPACE = root_config.SERVICE_BUS_NAMESPACE
SERVICE_BUS_CONNECTION_STRING = root_config.SERVICE_BUS_CONNECTION_STRING
missing_required_vars = root_config.missing_required_vars
INPUT_QUEUE = root_config.INPUT_QUEUE
WORK_QUEUE = root_config.WORK_QUEUE
RESPONSE_QUEUE = root_config.RESPONSE_QUEUE
CLAUDE_API_KEY = root_config.CLAUDE_API_KEY
CLAUDE_BASE_URL = root_config.CLAUDE_BASE_URL
CLAUDE_MODEL = root_config.CLAUDE_MODEL
MISTRAL_DOC_ENDPOINT = root_config.MISTRAL_DOC_ENDPOINT
MISTRAL_DOC_KEY = root_config.MISTRAL_DOC_KEY
MISTRAL_RATE_LIMIT_GLOBAL = root_config.MISTRAL_RATE_LIMIT_GLOBAL
MISTRAL_RATE_LIMIT_PER_INSTANCE = root_config.MISTRAL_RATE_LIMIT_PER_INSTANCE
DB_HOST = root_config.DB_HOST
DB_PORT = root_config.DB_PORT
DB_USER = root_config.DB_USER
DB_PASSWORD = root_config.DB_PASSWORD
DB_NAME = root_config.DB_NAME
DB_POOL_SIZE = root_config.DB_POOL_SIZE
MAX_CONTAINER_INSTANCES = root_config.MAX_CONTAINER_INSTANCES
BATCH_SIZE = root_config.BATCH_SIZE
MAX_CONCURRENT_IN_BATCH = root_config.MAX_CONCURRENT_IN_BATCH
STALE_JOB_TIMEOUT_MINUTES = root_config.STALE_JOB_TIMEOUT_MINUTES
MAX_STALE_RETRIES = root_config.MAX_STALE_RETRIES
OCR_QUALITY_THRESHOLD_GOOD = root_config.OCR_QUALITY_THRESHOLD_GOOD
OCR_QUALITY_THRESHOLD_BEST = root_config.OCR_QUALITY_THRESHOLD_BEST
RATE_LIMIT_RETRY_BASE_DELAY = root_config.RATE_LIMIT_RETRY_BASE_DELAY
RATE_LIMIT_RETRY_MAX_DELAY = root_config.RATE_LIMIT_RETRY_MAX_DELAY
RATE_LIMIT_RETRY_MAX_ATTEMPTS = root_config.RATE_LIMIT_RETRY_MAX_ATTEMPTS
CONFIDENCE_THRESHOLD = root_config.CONFIDENCE_THRESHOLD
ERROR_LOG_CONTAINER = root_config.ERROR_LOG_CONTAINER

# Import document types configuration
from config.document_types import (
    DOCUMENT_TYPES_CONFIG,
    load_document_types_from_db,
    get_all_document_types,
    get_document_config,
    get_classification_hints,
    reload_config,
    search_table_by_name
)

__all__ = [
    # Document types
    'DOCUMENT_TYPES_CONFIG',
    'load_document_types_from_db',
    'get_all_document_types',
    'get_document_config',
    'get_classification_hints',
    'reload_config',
    'search_table_by_name',
    # Azure Storage
    'AZURE_STORAGE_ACCOUNT_NAME',
    'AZURE_STORAGE_CONNECTION_STRING',
    'AZURE_STORAGE_CONTAINER',
    # Service Bus
    'SERVICE_BUS_NAMESPACE',
    'SERVICE_BUS_CONNECTION_STRING',
    'missing_required_vars',
    'INPUT_QUEUE',
    'WORK_QUEUE',
    'RESPONSE_QUEUE',
    # Claude (Azure AI Foundry)
    'CLAUDE_API_KEY',
    'CLAUDE_BASE_URL',
    'CLAUDE_MODEL',
    # Mistral
    'MISTRAL_DOC_ENDPOINT',
    'MISTRAL_DOC_KEY',
    'MISTRAL_RATE_LIMIT_GLOBAL',
    'MISTRAL_RATE_LIMIT_PER_INSTANCE',
    # Database
    'DB_HOST',
    'DB_PORT',
    'DB_USER',
    'DB_PASSWORD',
    'DB_NAME',
    'DB_POOL_SIZE',
    # Scaling
    'MAX_CONTAINER_INSTANCES',
    'BATCH_SIZE',
    'MAX_CONCURRENT_IN_BATCH',
    'STALE_JOB_TIMEOUT_MINUTES',
    'MAX_STALE_RETRIES',
    # OCR Quality
    'OCR_QUALITY_THRESHOLD_GOOD',
    'OCR_QUALITY_THRESHOLD_BEST',
    # Rate Limiting
    'RATE_LIMIT_RETRY_BASE_DELAY',
    'RATE_LIMIT_RETRY_MAX_DELAY',
    'RATE_LIMIT_RETRY_MAX_ATTEMPTS',
    # Classification
    'CONFIDENCE_THRESHOLD',
    # Error Log
    'ERROR_LOG_CONTAINER'
]

