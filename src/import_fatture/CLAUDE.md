# POLIS Import System

ETL system for importing financial data (invoices, personnel costs, services, tenders) from Excel into a MySQL database for the POLIS project. Tracks SAL (Stato Avanzamento Lavori) periods — each a 4-month window starting September 2022.

## Tech Stack

- **Language:** Python 3.11
- **Data processing:** pandas 2.2, openpyxl 3.1, numpy
- **Database:** MySQL via mysql-connector-python 9.3
- **AI mapping:** Anthropic Claude API (anthropic 0.68) for invoice/personnel column detection (SAL 7+); OpenAI GPT-4o-mini for rendicontazione column mapping
- **DB access:** mysql-connector-python 9.3 (most scripts); SQLAlchemy (rendicontazione only)
- **API:** requests for Azure Blob Storage API (servizi_api, SAL-8 fatture)
- **Async:** asyncio (gare importer)
- **Config:** python-dotenv (.env for API keys and feature flags)
- **Dependencies:** `requirements.txt`
- **Container app (Azure):** azure-storage-blob, azure-servicebus, anthropic SDK (Opus 4.6 via Azure AI Foundry)
- **Container dependencies:** `container_app/requirements.txt` (superset of root)

## Project Structure

| Path | Purpose |
|------|---------|
| `import_fatture.py` | Invoice import — unified entry point (SAL 1-6 legacy) |
| `import_personale.py` | Personnel cost import with dynamic SAL period handling |
| `import_servizi.py` | Service metrics import (local file, SAL 7 only) |
| `import_servizi_api.py` | Service import with Azure API, dry-run support, batch folder import |
| `import_rendicontazione.py` | Accounting report import — uses OpenAI + SQLAlchemy (different stack) |
| `script gare.py` | Tender/contract batch import — async, openpyxl direct, merged cell handling |
| `import_error_handler.py` | Shared error tracking — used by fatture/personale importers |
| `utils/sal_manager.py` | SAL period calculation utility (dates, month mappings) |
| `SAL-1/` through `SAL-6/` | Legacy per-SAL import scripts |
| `SAL-7/`, `SAL-8/` | Modern SAL scripts with AI mapping and API integration |
| `logs/` | Generated import logs (gitignored) |
| `container_app/` | Azure Container App for automated import (see below) |
| `container_app/main.py` | Container entry point — Service Bus listener loop |
| `container_app/config.py` | Centralized config from environment variables |
| `container_app/models.py` | Dataclasses for messages, results, metadata |
| `container_app/blob_manager.py` | Azure Blob Storage: scan, download, metadata tagging |
| `container_app/queue_manager.py` | Azure Service Bus: receive requests, send results |
| `container_app/file_classifier.py` | AI classification of Excel files (Anthropic SDK + Opus 4.6) |
| `container_app/azure_ai_bridge.py` | Drop-in replacement for anthropic SDK → Azure AI Foundry |
| `container_app/import_router.py` | Routes file type + SAL number to correct adapter |
| `container_app/orchestrator.py` | Main flow: scan → classify → import → tag → respond |
| `container_app/import_adapters/` | Adapter wrappers for each existing import script |
| `container_app/Dockerfile` | Container image definition (python:3.11-slim) |

## Key Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Import invoices
python import_fatture.py --sal <N> --file "path/to/file.xlsx"
python SAL-7/import_fatture_sal7.py --sal 7              # AI-mapped
python SAL-8/import_fatture_sal8.py --sal 8 --cartella "folder_name"  # API-sourced

# Import personnel costs
python import_personale.py "path/to/personnel.xlsx"

# Import services
python import_servizi.py                                  # Local file
python import_servizi_api.py --cartella "path" --dry-run  # API + dry-run

# Import tenders
python "script gare.py"

# Import accounting reports
python import_rendicontazione.py

# --- Container App (automated import) ---
# Build Docker image
docker build -t polis-import -f container_app/Dockerfile .

# Run locally (requires env vars — see container_app/config.py)
docker run --env-file .env polis-import

# Run entry point directly (for local dev/testing)
python container_app/main.py

# Full deploy su Azure (build + push ACR + delete old + create new)
.\container_app\deploy.ps1                    # tag automatico (timestamp)
.\container_app\deploy.ps1 -ImageTag "v2.1"   # tag custom
.\container_app\deploy.ps1 -SkipBuild          # solo redeploy (usa :latest)
```

## Environment Variables (.env)

**CLI scripts:**
- `USE_AI_MAPPING` — `true`/`false` toggle for AI-based header detection
- OpenAI API key for rendicontazione is hardcoded in `import_rendicontazione.py:11` (in container, monkey-patched to use Azure AI)

**Container app** (all required in production):
- `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` — MySQL connection
- `AZURE_STORAGE_CONNECTION_STRING` — Azure Blob Storage access
- `AZURE_STORAGE_CONTAINER_NAME` — Blob container name (default: `import-files`)
- `AZURE_SERVICEBUS_CONNECTION_STRING` — Azure Service Bus access
- `SERVICEBUS_INPUT_QUEUE` — Input queue name (default: `import-requests`)
- `SERVICEBUS_OUTPUT_QUEUE` — Output queue name (default: `import-results`)
- `AZURE_AI_ENDPOINT` — Azure AI Foundry endpoint for Opus 4.6
- `AZURE_AI_DEPLOYMENT_NAME` — Deployment name (default: `claude-opus-46`)
- `AZURE_AI_API_KEY` — Azure AI API key
- `AZURE_AI_API_VERSION` — API version (default: `2024-12-01-preview`)
- `TEMP_DOWNLOAD_DIR` — Temp directory for downloads (default: `/tmp/imports`)
- `LOG_LEVEL` — Logging level (default: `INFO`)

## Database

- **Dev:** localhost:3306, database `polis`
- **Prod:** Azure MySQL Flexible Server (`colibri-mysql-flexible.mysql.database.azure.com`)
- DB config is hardcoded per-script in `DB_CONFIG` dicts (see each script's `main()` function)

## SAL Period Logic

SAL numbering starts at 1 (Sep 2022). Each SAL spans 4 months. Period calculation lives in `utils/sal_manager.py:29` (`get_periodo_sal`). SAL 1-6 are legacy (per-folder scripts); SAL 7+ use the unified framework with AI mapping.

## Error Handling

**Fatture/personale:** Use `ImportErrorHandler` (`import_error_handler.py:5`) for session-based error tracking in `import_sessions`/`import_errors` DB tables. Errors logged via a **separate DB connection** to survive rollbacks (`import_fatture.py:495`).

**Servizi:** Simple try/except per row, errors logged but no `ImportErrorHandler` integration. Continues on failure.

**Gare:** Per-row try/except with autocommit (`script gare.py:91`). No error handler; errors logged to console only.

**Rendicontazione:** Entire dataframe written with `pandas.to_sql` (`import_rendicontazione.py:417`). On failure, saves debug CSV (`import_rendicontazione.py:424`).

## Conventions

- All code and comments are in **Italian** (variable names, class names, log messages)
- **Fatture/personale** follow full class structure: `__init__` -> `connetti_db` -> `carica_cache` -> `importa_excel` -> `importa_foglio_excel` -> `importa_riga_atomica`
- **Servizi** follow a simpler class structure: `__init__` -> `connetti_db` -> `carica_cache` -> `importa_excel` (no atomicity/savepoints)
- **Gare** uses a class with `parse_excel` + async `import_to_db`, reads via openpyxl directly (not pandas)
- **Rendicontazione** is purely functional (no class), uses OpenAI + SQLAlchemy `to_sql` for bulk inserts
- Custom exceptions only in fatture/personale importers (e.g., `import_fatture.py:13-35`)
- Data conversion (`converti_numero`, `converti_data`) duplicated per importer; gare uses `_parse_importo`/`_parse_date` instead

## Container App Architecture

The `container_app/` directory implements an automated import pipeline deployed as an Azure Container App. It wraps all existing import scripts.

**Flow:** Service Bus message → Blob Storage scan → AI classification (Anthropic SDK + Opus 4.6 via Azure AI Foundry) → Import via adapters → Blob metadata tagging → Service Bus result

**Import order per SAL folder:** fatture → personale → servizi → gare → rendicontazione

**Adapters** (`container_app/import_adapters/`) wrap each existing script:
- `fatture_adapter.py` — Dynamic loading of SAL-specific scripts via `importlib.util.spec_from_file_location` (SAL folder names contain `-` which is invalid for Python packages). For SAL 7+, patches `anthropic.Anthropic` → `AzureAIClient` to redirect API calls to Azure AI Foundry via `azure_ai_bridge.py`
- `personale_adapter.py` — Same dynamic loading + anthropic patching for SAL 7+; downloaded files must preserve original blob names (SAL number extracted from filename)
- `servizi_adapter.py` — Wraps `ImportatoreServiziAPI.importa_da_file_locale()`
- `gare_adapter.py` — Uses `object.__new__()` to bypass hardcoded DB config in `BatchGareImporter.__init__()`, then sets `conn` manually
- `rendicontazione_adapter.py` — Monkey-patches `chiedi_ai()` (OpenAI → Azure AI), `engine` (hardcoded → env vars), and `estrai_info_file()` (forces correct `id_sal` from classification) at module level before calling `importa_excel()`. Originals restored in `finally` block.

**AI unification** (`azure_ai_bridge.py`): Drop-in replacement for the `anthropic` SDK that redirects all `client.messages.create()` calls to Azure AI Foundry's OpenAI-compatible endpoint. Used by fatture/personale SAL 7+ adapters (patched via `anthropic.Anthropic = AzureAIClient`), file classifier, and rendicontazione adapter. No `ANTHROPIC_API_KEY` needed — everything uses `AZURE_AI_*` vars.

**File classification** (`file_classifier.py`): Anthropic SDK (via Azure AI bridge) analyzes sheet names + headers to classify each Excel file. Falls back to keyword-based heuristics if AI fails. Also extracts SAL number from blob path via regex patterns.

**Blob metadata** written after each import: `import_status`, `import_type`, `import_date`, `import_request_id`, `import_sal_number`, `import_error`. Already-processed files (status=success) are skipped on re-run (unless `force_reprocess=true`).

**SAL auto-creation**: Orchestrator ensures SAL records exist in the `sal` table before starting imports (`orchestrator.py:_ensure_sal_exists`). Uses `SALManager.get_periodo_sal()` for date calculation.

**Sheet name detection (SAL 8+)**: `_rileva_fogli_con_ai()` in SAL-8 uses AI to identify invoice sheets and their linea di intervento from sheet names. Falls back to `_rileva_fogli_fallback()` (case-insensitive keyword matching) if AI fails.

**Message format**: Input queue accepts `{"sal_folder": "9_SAL_31_08_2025", "force_reprocess": true}`. Output queue returns full result with per-file status, row counts, and processing time. See `container_app/models.py` for schema.

## Additional Documentation

Check these when working on related topics:

| Document | When to check |
|----------|---------------|
| [Architectural Patterns](.claude/docs/architectural_patterns.md) | Adding new importers, modifying data flow, understanding caching/transactions |
