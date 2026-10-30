"""
Configurazione centralizzata per Azure Function - Sync ZIP.

Tutte le variabili si leggono dalle Application Settings della Function App
nel portale Azure (Configurazione > Impostazioni applicazione).

Per sviluppo locale, creare un file local.settings.json con gli stessi valori.
"""
import os

# ==================== AZURE STORAGE ====================
# Storage DATI (zip, cartelle SAL): DATA_STORAGE_ACCOUNT_NAME + managed identity (preferito).
# Fallback legacy: connection string in AzureWebJobsStorage (host storage == storage dati).
DATA_STORAGE_ACCOUNT_NAME = os.environ.get("DATA_STORAGE_ACCOUNT_NAME", "")
STORAGE_CONNECTION = os.environ.get("AzureWebJobsStorage")

# Container blob
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "polis")

# Prefissi blob
UPLOAD_PREFIX = os.environ.get("UPLOAD_PREFIX", "zip_upload/")
SAL_PREFIX = os.environ.get("SAL_PREFIX", "")
PROCESSED_PREFIX = os.environ.get("PROCESSED_PREFIX", "zip_processed/")
DONE_PREFIX = os.environ.get("DONE_PREFIX", "zip_done/")
NOTE_PREFIX = os.environ.get("NOTE_PREFIX", "note_trasmissione/")

# Directory di lavoro temporanea (Azure Files montato)
ZIP_WORKDIR = os.environ.get("ZIP_WORKDIR", "/data")

# ==================== DATABASE MYSQL ====================
DB_HOST = os.environ.get("DB_HOST", "colibri-mysql-flexible.mysql.database.azure.com")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "polis")
DB_USER = os.environ.get("DB_USER", "polisammin")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
DB_SSL = os.environ.get("DB_SSL", "true").lower() == "true"

# ==================== AZURE SERVICE BUS ====================
# SERVICE_BUS_NAMESPACE + managed identity (preferito); connection string = fallback legacy
SERVICE_BUS_NAMESPACE = os.environ.get("SERVICE_BUS_NAMESPACE", "")
SERVICE_BUS_CONNECTION_STRING = os.environ.get("SERVICE_BUS_CONNECTION_STRING", "")

# Code per comunicazione con sistema Autogen Import
IMPORT_EXCEL_QUEUE = os.environ.get("IMPORT_EXCEL_QUEUE", "import-excel")
IMPORT_PDF_QUEUE = os.environ.get("IMPORT_PDF_QUEUE", "import-pdf")
IMPORT_RESPONSE_QUEUE = os.environ.get("IMPORT_RESPONSE_QUEUE", "import-response")
