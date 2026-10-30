# Polis RBAC Migration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consegnare un unico `Run-PolisUpdate.ps1` che, su un PC Windows vergine con accesso Owner al RG Polis di produzione, migra tutti i servizi presenti da Access Key a managed identity, rilascia le modifiche di luglio 2026, disabilita le chiavi e si ripulisce; più il rilascio via SCM, dal PC dello sviluppatore, del codice migrato delle Web App.

**Architecture:** Repo GitHub pubblico `Jonato01/polis-rbac-migration` con (a) `src/`: sorgenti dei servizi container/function migrati a identity con doppia lettura (nuova variabile oppure vecchia connection string), (b) `tools/`: script PowerShell a fasi idempotenti (discovery per tipo/immagine, identity, RBAC, build cloud `az acr build`, switch settings, verifica, hardening con doppia conferma, report, cleanup) con fallback Claude Code. Un bootstrap separato, consegnato privatamente, installa Azure CLI, scarica il repo a SHA fissato e lancia le fasi. Le Web App vengono patchate nei repo originali e pushate sui remote SCM `prod` dallo sviluppatore.

**Tech Stack:** Python 3.11 (`azure-identity`, `azure-storage-blob`, `azure-servicebus`), pytest; Windows PowerShell 5.1 + Pester 5; Azure CLI; Docker-less build via ACR Tasks; GitHub (`gh`).

**Spec:** `docs/superpowers/specs/2026-10-30-polis-rbac-migration-design.md`

## Global Constraints

- PowerShell: tutto compatibile **Windows PowerShell 5.1** (no `&&`, no `?:`, no `??`, niente moduli esterni oltre Pester per i test). `Set-StrictMode -Version Latest` in ogni file.
- Nessun segreto nel repo: nessun file `.env*`, `.prodenv`, `.colenv`, `.envprod`, `.envcol`, `local.settings.json`, `.idea/`, `.claude/settings.local.json`, `logs/`, `*.zip`, `db_pass.sh`, `set_api_settings.sh`, `set_web_settings.sh`, `file_fallito.ps1`, `deploy-*.ps1` originali; nessuna stringa `AccountKey=`, `SharedAccessKey=`, `accesskey=`, `sk-ant-`, password in chiaro. Il push è vietato con scansione non pulita (Task 20).
- Il bootstrap con la key Anthropic (`Run-PolisUpdate.ps1` generato) è in `.gitignore`; nel repo c'è solo il template.
- Nomi variabili nuove: `AZURE_STORAGE_ACCOUNT_NAME`, `SERVICE_BUS_NAMESPACE` (FQDN); in `import_fatture`: `AZURE_STORAGE_ACCOUNT_NAME`, `AZURE_SERVICEBUS_NAMESPACE`; function app: `DATA_STORAGE_ACCOUNT_NAME`, `AzureWebJobsStorage__accountName`.
- Ruoli (nomi esatti): `Storage Blob Data Contributor`, `Storage Blob Data Owner`, `Storage Queue Data Contributor`, `Azure Service Bus Data Owner`, `AcrPull`. Scope = risorsa.
- Immagine: tag = `yyyyMMdd-HHmmss` della run. Nome normalizzato: senza registry, senza tag, senza suffisso `-prod`/`-coll`, senza prefisso `polis-prod-`/`polis-coll-`.
- Hardening mai senza: (1) tutti i servizi mappati verdi, (2) nessun servizio key-based non mappato (incluso `polis-prod-api-storage`), (3) conferma esplicita operatore. Override solo `-ForceHardening`, tracciato nel report.
- Niente upload di zip SAL in prod da parte dello script (D15).
- Commit message: Conventional Commits, ≤ 15 parole, senza co-author.
- Comandi locali: Python = `py -3.12`; test Python dal venv del repo `polis-rbac-migration\.venv`; Pester = `Invoke-Pester -Path tests\ps -Output Detailed`.

## Review Focus

1. Settings di prod con **entrambe** le variabili (vecchia connection string e nuova) → il codice deve preferire identity (Task 2, test `test_prefers_identity_when_both_set`).
2. Connection string Service Bus con `EntityPath=` o endpoint con porta/slash finale → il parser deve estrarre solo il namespace FQDN (Task 8, test `Get-ConnectionStringParts` casi 3-4).
3. Immagine prod con tag digest (`@sha256:`) o registry in maiuscolo → normalizzazione deve mappare comunque (Task 8, test `Get-NormalizedImageName` caso 5).
4. Role assignment appena creata ma non ancora propagata → il servizio riavviato fallisce con 403: `Wait-RoleAssignment` deve attendere e la fase 6 non deve partire prima (Task 11, test `Wait-RoleAssignment retries until listed`).
5. Interruzione tra fase 6 e 8 e rilancio: il backup della fase 2 non deve essere **sovrascritto** con lo stato già migrato (Task 15, test `Save-ServiceBackup does not overwrite existing backup`).

---

## Struttura file (repo `C:\Users\rgioana\PycharmProjects\polis-rbac-migration`)

```
.gitignore
README.md
requirements-dev.txt                 # azure-identity azure-storage-blob azure-servicebus pytest
src/_shared/azure_clients.py         # Task 2 — fonte unica
src/import_fatture/…                 # Task 3
src/import-sal/…                     # Task 4-5
src/servicebus-bridge/…              # Task 6
src/polis-sal-sftp-sync/…            # Task 7
tools/mapping.json                   # Task 9
tools/Invoke-PolisMigration.ps1      # Task 18
tools/Sync-Shared.ps1                # Task 2
tools/lib/Az.ps1 Mapping.ps1 Settings.ps1 State.ps1 Discovery.ps1 Rbac.ps1 Deploy.ps1 Verify.ps1 Hardening.ps1 Report.ps1 Fallback.ps1 Cleanup.ps1
tools/phases/00-Preflight.ps1 … 10-Cleanup.ps1
tools/fallback/CLAUDE.md
tools/scrub/Test-NoSecrets.ps1       # Task 20
bootstrap/Run-PolisUpdate.template.ps1   # Task 19
bootstrap/New-Bootstrap.ps1          # genera Run-PolisUpdate.ps1 (gitignored) con key, SHA, hash
tests/python/test_azure_clients.py test_shared_copies.py
tests/ps/*.Tests.ps1
tests/fixtures/az/*.json             # output az finti per i test di discovery
```

Ogni `lib/*.ps1` espone solo funzioni (dot-source). Ogni `phases/NN-*.ps1` espone `Invoke-PhaseNN -Context $ctx` e ritorna `$ctx` aggiornato. `$ctx` è un `[hashtable]` con: `Rg`, `Sub`, `WorkDir`, `Inventory`, `Plan`, `State` (vedi Task 15), `Options` (`WhatIf`, `ForceHardening`, `KeepBackup`, `SmokeTest`, `AnthropicApiKey`).

---

## Parte A — Fondamenta

### Task 1: Scaffold repo e tooling di test

**Files:**
- Create: `.gitignore`, `README.md`, `requirements-dev.txt`, `tests/ps/Smoke.Tests.ps1`, `tests/python/test_smoke.py`

- [ ] **Step 1: Inizializza repo e venv**

```powershell
cd C:\Users\rgioana\PycharmProjects\polis-rbac-migration
git init -b main
py -3.12 -m venv .venv
.\.venv\Scripts\pip install azure-identity azure-storage-blob azure-servicebus pytest
Install-Module Pester -Scope CurrentUser -Force -SkipPublisherCheck -MinimumVersion 5.5
```
Expected: `.\.venv\Scripts\pytest --version` stampa pytest 8.x; `Import-Module Pester -MinimumVersion 5.5; (Get-Module Pester).Version` ≥ 5.5.

- [ ] **Step 2: `.gitignore`** con: `.venv/`, `__pycache__/`, `bootstrap/Run-PolisUpdate.ps1`, `work/`, `*.zip`, `.env*`, `.prodenv`, `.colenv`, `.envprod`, `.envcol`, `local.settings.json`, `.idea/`, `**/.claude/settings.local.json`, `logs/`.

- [ ] **Step 3: Test smoke** — `tests/ps/Smoke.Tests.ps1` con `Describe 'repo' { It 'has gitignore' { Test-Path "$PSScriptRoot\..\..\.gitignore" | Should -BeTrue } }`; `tests/python/test_smoke.py` con `def test_imports(): import azure.identity, azure.storage.blob, azure.servicebus`.

- [ ] **Step 4: Esegui** `Invoke-Pester -Path tests\ps -Output Detailed` e `.\.venv\Scripts\pytest tests\python -q`. Expected: entrambi PASS.

- [ ] **Step 5: README.md** — 20 righe: scopo, struttura cartelle, come lanciare i test, avviso "nessun segreto". Commit:

```bash
git add -A && git commit -m "chore: scaffold repo, test tooling, gitignore"
```

### Task 2: Helper Python `azure_clients.py` (fonte unica) e sync nei build context

**Files:**
- Create: `src/_shared/azure_clients.py`, `tests/python/test_azure_clients.py`, `tools/Sync-Shared.ps1`, `tests/python/test_shared_copies.py`

**Interfaces (Produces):**
```python
# src/_shared/azure_clients.py
def get_credential() -> DefaultAzureCredential            # cached per processo
def get_credential_aio() -> azure.identity.aio.DefaultAzureCredential
def get_blob_service_client(account_name_var: str = "AZURE_STORAGE_ACCOUNT_NAME",
                            conn_str_var: str = "AZURE_STORAGE_CONNECTION_STRING") -> BlobServiceClient
def get_blob_service_client_aio(account_name_var=..., conn_str_var=...) -> azure.storage.blob.aio.BlobServiceClient
def get_servicebus_client(namespace_var: str = "SERVICE_BUS_NAMESPACE",
                          conn_str_var: str = "SERVICE_BUS_CONNECTION_STRING") -> ServiceBusClient
def get_servicebus_client_aio(namespace_var=..., conn_str_var=...) -> azure.servicebus.aio.ServiceBusClient
def get_blob_sas_token(blob_service: BlobServiceClient, container: str, blob: str,
                       permission: BlobSasPermissions, expiry: datetime) -> str
def describe_auth_mode(account_name_var=..., conn_str_var=...) -> str   # "identity" | "connection_string" | "missing"
```
Regole: se `os.environ[account_name_var]` non vuoto → identity (`account_url=f"https://{name}.blob.core.windows.net"`); altrimenti se connection string presente → `from_connection_string` + `logging.warning("… usa connection string legacy, migrare a %s", account_name_var)`; altrimenti `raise RuntimeError(f"Configurare {account_name_var} (identity) oppure {conn_str_var}")`. Service Bus: namespace → `ServiceBusClient(fully_qualified_namespace=ns, credential=get_credential())`; il valore può essere `polis-messaging-prod` o FQDN: se non contiene `.` aggiungere `.servicebus.windows.net`. `get_blob_sas_token`: se `blob_service.credential` ha attributo `account_key` → `generate_blob_sas(account_key=...)`; altrimenti `udk = blob_service.get_user_delegation_key(key_start_time=now-5min, key_expiry_time=min(expiry, now+7d))` e `generate_blob_sas(user_delegation_key=udk, ...)`.

- [ ] **Step 1: Test** `tests/python/test_azure_clients.py` (importa via `sys.path.insert(0, "src/_shared")`; usa `monkeypatch.setenv/delenv`; mocka `DefaultAzureCredential` con `monkeypatch.setattr(azure_clients, "DefaultAzureCredential", FakeCred)`):
  - `test_blob_identity_when_account_set` → `client.url == "https://acct.blob.core.windows.net/"`, `describe_auth_mode() == "identity"`.
  - `test_blob_connection_string_fallback_warns` (caplog contiene "legacy") → `describe_auth_mode() == "connection_string"`.
  - `test_blob_missing_raises` → `RuntimeError` con entrambi i nomi variabile nel messaggio.
  - `test_prefers_identity_when_both_set`.
  - `test_servicebus_namespace_short_name_gets_suffix` → `client.fully_qualified_namespace == "polis-messaging-prod.servicebus.windows.net"`.
  - `test_servicebus_custom_var_names` (es. `AZURE_SERVICEBUS_NAMESPACE`).
  - `test_sas_uses_user_delegation_key_without_account_key` (mock `get_user_delegation_key` chiamato una volta, `generate_blob_sas` mockato riceve `user_delegation_key`).
  - `test_sas_caps_expiry_to_7_days`.

- [ ] **Step 2: Run** `.\.venv\Scripts\pytest tests\python\test_azure_clients.py -q`. Expected: FAIL `ModuleNotFoundError: azure_clients`.

- [ ] **Step 3: Implementa** `src/_shared/azure_clients.py` secondo le interfacce sopra. Nessun'altra dipendenza oltre azure-identity/storage-blob/servicebus.

- [ ] **Step 4: Run** stesso comando. Expected: 8 PASS.

- [ ] **Step 5: `tools/Sync-Shared.ps1`**: legge `tools/mapping.json` (campo `sharedTargets: string[]`, lista di cartelle relative al repo; finché il mapping non esiste, legge un parametro `-Targets`), copia `src/_shared/azure_clients.py` in ognuna. `tests/python/test_shared_copies.py::test_all_copies_identical` legge `tools/mapping.json` → per ogni target confronta `filecmp.cmp(shared, target/azure_clients.py, shallow=False)`; se il mapping non esiste ancora, `pytest.skip`.

- [ ] **Step 6: Commit** `git commit -am "feat: shared azure_clients helper with identity-first auth"`.

## Parte B — Migrazione codice servizi (repo pubblico)

Regola per ogni task B: copiare dal repo originale **solo** i file necessari al build context (Dockerfile + ciò che `COPY`a), **mai** i file elencati in Global Constraints; sostituire ogni `*.from_connection_string(` con l'helper; aggiungere `azure-identity` ai requirements se manca; verificare con `py -3.12 -m compileall -q <cartella>` e con una `grep` che non restino `from_connection_string` (eccetto dentro `azure_clients.py`). Test unitari solo dove c'è logica propria (sotto).

### Task 3: `src/import_fatture` (polis-import-app) con fix di luglio

**Files:**
- Create: `src/import_fatture/**` (copia da `C:\Users\rgioana\PycharmProjects\import_fatture`, **working tree** incluse le 4 modifiche non committate), `src/import_fatture/container_app/azure_clients.py` (sync)
- Modify: `container_app/config.py`, `container_app/blob_manager.py:20-30`, `container_app/queue_manager.py:20-40`, `container_app/main.py:52-64`, `container_app/requirements.txt`

- [ ] **Step 1: Copia** con `robocopy` escludendo `.venv .git .idea __pycache__ .env .prodenv .colenv container_app\deploy*.ps1`. Verifica che `git -C <orig> diff --stat` mostri i 4 file di luglio e che le copie li contengano (`Select-String '_romano_a_intero' src\import_fatture\SAL-8\import_personale_sal8.py`).

- [ ] **Step 2: Test** `tests/python/test_import_fatture_config.py`:
  - `test_config_exposes_identity_vars`: con env `AZURE_STORAGE_ACCOUNT_NAME=a`, `AZURE_SERVICEBUS_NAMESPACE=n` e senza connection string, `import container_app.config as c` → `c.AZURE_STORAGE_ACCOUNT_NAME == "a"`, `c.AZURE_SERVICEBUS_NAMESPACE == "n"`.
  - `test_main_required_vars_accept_identity`: `from container_app.main import missing_required_vars` → `[]` con sole variabili identity + AI; `["AZURE_STORAGE_ACCOUNT_NAME|AZURE_STORAGE_CONNECTION_STRING", ...]` senza nulla.

- [ ] **Step 3: Run** → FAIL (`missing_required_vars` non esiste).

- [ ] **Step 4: Implementa**: `config.py` aggiunge le due variabili; `blob_manager.BlobManager.__init__` usa `get_blob_service_client(conn_str_var="AZURE_STORAGE_CONNECTION_STRING")`; `queue_manager.QueueManager` usa `get_servicebus_client(namespace_var="AZURE_SERVICEBUS_NAMESPACE", conn_str_var="AZURE_SERVICEBUS_CONNECTION_STRING")`; `main.py` estrae `def missing_required_vars() -> list[str]` usata all'avvio (coppie "A|B" quando una delle due basta). `azure_clients.py` importato come `from container_app.azure_clients import …`.

- [ ] **Step 5: Run** test + `py -3.12 -m compileall -q src\import_fatture\container_app` + `Select-String -Path src\import_fatture\container_app\*.py -Pattern from_connection_string | ? Filename -ne azure_clients.py` → vuoto.

- [ ] **Step 6: Commit** `git commit -am "feat(import_fatture): identity auth and july 2026 import fixes"`.

### Task 4: `src/import-sal/services/import/autogen-pdf-import`

**Files:**
- Create: copia della cartella (escl. `.env*`, `.prodenv`, `.colenv`, `deploy-*.ps1`, `__pycache__`), `azure_clients.py` (sync)
- Modify: `config_root.py:12-16`, `config/__init__.py:24-26,77-80`, `services/blob_service.py:24-30`, `main.py:109,208,331,396`, `services/dispatcher.py:185`

- [ ] **Step 1: Test** `tests/python/test_autogen_config.py::test_required_accepts_identity_vars`: importando `config` con `AZURE_STORAGE_ACCOUNT_NAME` e `SERVICE_BUS_NAMESPACE` settate e senza connection string, `config.validate_required()` non alza (oggi la lista required alle righe 77-80 esige le connection string).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implementa**: nuove variabili in `config_root.py`; required a coppie; `BlobService._get_client` → `get_blob_service_client_aio()`; i 5 `ServiceBusClient.from_connection_string(...)` → `get_servicebus_client_aio()`.
- [ ] **Step 4: Run** test + compileall + grep. Expected: PASS, nessun `from_connection_string` fuori dall'helper.
- [ ] **Step 5: Commit** `git commit -am "feat(autogen-pdf-import): identity auth for blob and service bus"`.

### Task 5: `src/import-sal` legacy — orchestrator e servizi `*-processor`

**Files:**
- Create: `src/import-sal/{Dockerfile,requirements.txt,import_sal_orchestrator.py,import_sal_service_manager.py,import_sal_utils.py,azure_clients.py}`; `src/import-sal/services/{sftp,backup-db,backup-container,documentation,cleanup,rollback,import/personale,import/fatture,import/pdf/**}` (solo file che i rispettivi `Dockerfile*` copiano, più `azure_clients.py`)
- Modify: ogni `*_service.py`/`*.py` con `from_connection_string`; `import_sal_utils.py` (rimuovere `create_container_group`, `delete_container`, `get_base_environment_variables`); `import_sal_orchestrator.py:13-15,37,61,81,120-129,156,237,299`; `requirements.txt` root (togliere `azure-mgmt-containerinstance`).

- [ ] **Step 1: Inventario** — per ogni `Dockerfile*` sotto `services/` leggere le `COPY` e produrre `tools/mapping.json` → sezione `containerApps` provvisoria (vedi Task 9 per lo schema) con `image`, `context`, `dockerfile`, `legacy: true`. Immagini normalizzate: dai `deploy-*.ps1` originali (`$IMAGE_NAME` senza `-prod`).
- [ ] **Step 2: Test** `tests/python/test_import_sal_orchestrator.py::test_validate_configuration_accepts_identity`: con `SERVICE_BUS_NAMESPACE` e senza `SERVICE_BUS_CONNECTION_STRING`/`AZURE_STORAGE_CONNECTION_STRING`, `ImportSalOrchestrator._validate_configuration()` non alza (mockare `ServiceBusClient` e il DB).
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implementa** su tutti i file inventariati; nell'orchestrator `self.credential`/`aci_client` rimossi.
- [ ] **Step 5: Run** test + `compileall` su `src/import-sal` + grep. PASS.
- [ ] **Step 6: Commit** `git commit -am "feat(import-sal): identity auth for legacy orchestrator and processors"`.

### Task 6: `src/servicebus-bridge`

**Files:** Create copia (`Dockerfile`, `requirements.txt`, `servicebus_bridge.py`, `check_dlq.py`, `azure_clients.py`) dal **working tree**; Modify `servicebus_bridge.py:20-21,45-46,93-98`, `check_dlq.py:6`, `requirements.txt` (+`azure-identity`).

- [ ] **Step 1: Test** `tests/python/test_servicebus_bridge.py::test_bridge_connects_with_namespace`: con `SERVICE_BUS_NAMESPACE=ns` e `get_servicebus_client` mockato, `ServiceBusBridge().connect()` non logga "non configurata" e chiama il mock una volta.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implementa**. **Step 4: Run** + compileall + grep → PASS.
- [ ] **Step 5: Commit** `git commit -am "feat(servicebus-bridge): identity auth"`.

### Task 7: `src/polis-sal-sftp-sync` (Function App unzip)

**Files:** Create copia (`function_app.py`, `config.py`, `host.json` **del working tree** con `functionTimeout: -1`, `requirements.txt`, `azure_clients.py`, eventuali moduli importati; escludere `local.settings.json`, `.env*`, `.prodenv`, `.colenv`); Modify `config.py:13,37`, `function_app.py:56,92`, `requirements.txt` (+`azure-identity`).

- [ ] **Step 1: Test** `tests/python/test_sftp_sync_config.py`:
  - `test_data_storage_from_dedicated_var`: `DATA_STORAGE_ACCOUNT_NAME=polisprodstorage` → `config.DATA_STORAGE_ACCOUNT_NAME == "polisprodstorage"`.
  - `test_legacy_fallback_to_webjobs_storage`: senza `DATA_STORAGE_ACCOUNT_NAME`, con `AzureWebJobsStorage=<cs finta>` → `config.STORAGE_CONNECTION` = quella cs (compatibilità).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implementa**: `function_app.py` usa `get_blob_service_client(account_name_var="DATA_STORAGE_ACCOUNT_NAME", conn_str_var="AzureWebJobsStorage")` e `get_servicebus_client()`. **Step 4: Run** + compileall + grep → PASS.
- [ ] **Step 5: Commit** `git commit -am "feat(sftp-sync): identity auth, separate data storage variable"`.

## Parte C — Tooling PowerShell dell'operatore

Convenzione test: `tests/ps/<Lib>.Tests.ps1`, Pester 5, `BeforeAll { . "$PSScriptRoot\..\..\tools\lib\<Lib>.ps1" }`. Le funzioni che chiamano `az` passano **sempre** da `Invoke-Az` (Task 10) così i test la mockano con `Mock Invoke-Az { Get-Content "$fixtures\x.json" | ConvertFrom-Json }`.

### Task 8: `lib/Mapping.ps1` e `lib/Settings.ps1` (funzioni pure)

**Interfaces (Produces):**
```powershell
function Get-NormalizedImageName([string]$Image) -> [string]
function Resolve-ServiceMapping([pscustomobject]$Resource, [pscustomobject]$Mapping) -> [pscustomobject]|$null
#   $Resource: @{ Kind='containerApp'|'webApp'|'functionApp'; Name; Image }
function Get-ConnectionStringParts([string]$Cs) -> @{ AccountName; Namespace; Kind='storage'|'servicebus'|'unknown' }
function ConvertTo-IdentitySettings([hashtable]$Current, [pscustomobject]$Entry, [hashtable]$Resolved)
#   -> @{ Add=[hashtable]; Remove=[string[]] }   $Resolved = @{ StorageAccount; ServiceBusNamespace; HostStorageAccount }
```

- [ ] **Step 1: Test** `tests/ps/Mapping.Tests.ps1`:
  - `Get-NormalizedImageName`: (1) `polisregistryprod.azurecr.io/polis-prod-import:20260331` → `polis-import`; (2) `polisregistryprod.azurecr.io/sftp-service-prod:latest` → `sftp-service`; (3) `polisregistry.azurecr.io/autogen-pdf-import:latest` → `autogen-pdf-import`; (4) `x.azurecr.io/fatture-service-coll:v1` → `fatture-service`; (5) `POLISREGISTRYPROD.azurecr.io/servicebus-bridge@sha256:abc` → `servicebus-bridge`.
  - `Resolve-ServiceMapping`: container app con immagine `polis-prod-import` → entry `context = 'src/import_fatture'`; web app `polis-prod-sal-import` → entry repo `polis-sal-sftp-api`; web app `polis-prod-sal-import-web` → `$null`; function `polis-prod-sftp-upload` → entry sftp-sync.
  `tests/ps/Settings.Tests.ps1`:
  - `Get-ConnectionStringParts`: (1) storage cs → `AccountName='polisprodstorage'`, `Kind='storage'`; (2) `Endpoint=sb://polis-messaging-prod.servicebus.windows.net/;SharedAccessKeyName=…;SharedAccessKey=…` → `Namespace='polis-messaging-prod.servicebus.windows.net'`; (3) idem con `;EntityPath=import-pdf` in coda; (4) endpoint senza slash finale; (5) stringa vuota → `Kind='unknown'`.
  - `ConvertTo-IdentitySettings`: entry import_fatture + current con le due connection string → `Add` = `AZURE_STORAGE_ACCOUNT_NAME`, `AZURE_SERVICEBUS_NAMESPACE`; `Remove` = le due connection string; entry sftp-sync → `Add` include `AzureWebJobsStorage__accountName=<HostStorageAccount>`, `DATA_STORAGE_ACCOUNT_NAME`, `SERVICE_BUS_NAMESPACE`; `Remove` include `AzureWebJobsStorage`, `SERVICE_BUS_CONNECTION_STRING`; entry sal-sftp-api → `Add` `AZURE_STORAGE_ACCOUNT_NAME` = valore attuale di `AZURE_ACCOUNT_NAME`, `Remove` `AZURE_ACCOUNT_KEY`.

- [ ] **Step 2: Run** `Invoke-Pester tests\ps\Mapping.Tests.ps1, tests\ps\Settings.Tests.ps1` → FAIL (funzioni non definite).
- [ ] **Step 3: Implementa** le 4 funzioni. `Resolve-ServiceMapping` usa `$Mapping.containerApps` (match su `image`), `$Mapping.webApps`/`functionApps` (match `-imatch` su campo `match`).
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "feat(tools): pure mapping and settings transforms"`.

### Task 9: `tools/mapping.json` definitivo

**Files:** Create/Modify `tools/mapping.json`; Test `tests/ps/MappingFile.Tests.ps1`.

Schema:
```json
{
  "sharedTargets": ["src/import_fatture/container_app", "src/import-sal", "src/import-sal/services/sftp", "..."],
  "containerApps": [
    { "image": "polis-import", "context": "src/import_fatture", "dockerfile": "container_app/Dockerfile",
      "legacy": false, "needsStorage": true, "needsServiceBus": true,
      "add": { "AZURE_STORAGE_ACCOUNT_NAME": "{storage}", "AZURE_SERVICEBUS_NAMESPACE": "{servicebus}" },
      "remove": ["AZURE_STORAGE_CONNECTION_STRING", "AZURE_SERVICEBUS_CONNECTION_STRING"],
      "errorPatterns": ["Unauthorized", "AuthenticationFailed", "CredentialUnavailableError", " 401", " 403"] }
  ],
  "webApps": [
    { "match": "sal-import$|sal-sftp-api", "repo": "polis-sal-sftp-api", "expectedCommit": "",
      "needsStorage": true, "needsServiceBus": true, "health": "/health",
      "add": { "AZURE_STORAGE_ACCOUNT_NAME": "{currentSetting:AZURE_ACCOUNT_NAME}", "SERVICE_BUS_NAMESPACE": "{servicebus}" },
      "remove": ["AZURE_ACCOUNT_KEY", "SERVICE_BUS_CONNECTION_STRING"] },
    { "match": "api-database", "repo": "polis-api-database", "expectedCommit": "", "needsServiceBus": true, "health": "/", "add": {"SERVICE_BUS_NAMESPACE": "{servicebus}"}, "remove": ["SERVICE_BUS_CONNECTION_STRING"] },
    { "match": "polis(-prod|-coll)?-(api-)?ai$", "repo": "polis-ai-2", "...": "..." },
    { "match": "frontend", "repo": "polis-frontend", "settingsOnly": true, "remove": ["SERVICE_BUS_CONNECTION_STRING"] }
  ],
  "functionApps": [
    { "match": "sftp-upload", "context": "src/polis-sal-sftp-sync", "needsStorage": true, "needsServiceBus": true, "hostStorage": true,
      "add": { "AzureWebJobsStorage__accountName": "{hostStorage}", "DATA_STORAGE_ACCOUNT_NAME": "{storage}", "SERVICE_BUS_NAMESPACE": "{servicebus}" },
      "remove": ["AzureWebJobsStorage", "SERVICE_BUS_CONNECTION_STRING"] },
    { "match": "orchestrator-func", "settingsOnly": true, "requireDeclarativeBindings": true, "hostStorage": true,
      "add": { "AzureWebJobsStorage__accountName": "{hostStorage}", "ServiceBusConnection__fullyQualifiedNamespace": "{servicebus}" },
      "remove": ["AzureWebJobsStorage", "ServiceBusConnection"] }
  ],
  "excludedWebApps": ["api-storage", "sal-import-web"]
}
```
Placeholder risolti da `ConvertTo-IdentitySettings`: `{storage}`, `{servicebus}`, `{hostStorage}`, `{currentSetting:NAME}`.

- [ ] **Step 1: Test** `MappingFile.Tests.ps1`: JSON valido; ogni `context` esiste e contiene il `dockerfile`; ogni `context`/`sharedTargets` contiene `azure_clients.py` identico a `src/_shared`; tutte le 5 immagini della pipeline in uso + tutte le legacy del Task 5 presenti; nessun `webApps.match` collide con `excludedWebApps` (`polis-prod-api-storage`, `polis-prod-sal-import-web` non devono matchare nessuna entry).
- [ ] **Step 2: Run** → FAIL. **Step 3: Scrivi** il file completo; esegui `tools/Sync-Shared.ps1`. **Step 4: Run** Pester + `pytest tests/python/test_shared_copies.py` → PASS.
- [ ] **Step 5: Commit** `git commit -am "feat(tools): service mapping definition"`.

### Task 10: `lib/Az.ps1` e `lib/State.ps1`

**Interfaces (Produces):**
```powershell
function Invoke-Az([string[]]$Arguments, [switch]$AllowFailure, [switch]$Raw) -> object   # json → PSObject; lancia su exit≠0 salvo AllowFailure; logga comando (mai valori di --settings/--secrets: li maschera)
function Set-AzWhatIf([bool]$On)   # con WhatIf, i comandi mutanti (create/update/set/delete/assignment create/regenerate) vengono solo loggati e ritornano $null
function Test-AzMutating([string[]]$Arguments) -> [bool]
function New-RunState([string]$WorkDir) -> [hashtable]      # carica work/state.json se esiste
function Save-RunState([hashtable]$State)
function Set-StepStatus([hashtable]$State, [string]$Phase, [string]$Service, [string]$Status, [string]$Detail)  # Status: pending|done|failed|skipped
function Get-StepStatus([hashtable]$State, [string]$Phase, [string]$Service) -> [string]
```
- [ ] **Step 1: Test** `Az.Tests.ps1`: `Test-AzMutating` vero per `containerapp update`, `role assignment create`, `storage account keys renew`, falso per `show`/`list`; `Invoke-Az` con `Mock` di `& az` e WhatIf on su comando mutante ritorna `$null` senza invocare; mascheratura: log non contiene il valore passato dopo `--settings`. `State.Tests.ps1`: round-trip `Set-StepStatus`/`Save-RunState`/`New-RunState`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implementa** (`Invoke-Az` chiama `& az @Arguments --only-show-errors -o json` tramite una funzione interna `Invoke-AzProcess` mockabile). **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `git commit -am "feat(tools): az wrapper with whatif and run state"`.

### Task 11: `lib/Discovery.ps1`

**Interfaces (Produces):**
```powershell
function Get-PolisInventory([string]$Rg, [pscustomobject]$Mapping) -> [pscustomobject] Inventory
# Inventory: Services[] (Kind, Name, Image, Settings[hashtable], SecretNames[], Identity (principalId|$null), Mapping (entry|$null), KeyBased[bool], Excluded[bool], Legacy[bool], Mounts[]),
#            StorageAccounts[] (Name, Id, Role='data'|'host', AllowSharedKey), ServiceBus (Name, Id, Namespace, DisableLocalAuth), Acr (Name, Id, AdminEnabled), Unmapped[], MissingExpected[]
function Resolve-ResourceId([string]$Type, [string]$Name) -> [string]   # 'storage'|'servicebus'|'acr', ricerca su tutta la subscription
function Test-OperatorRole([string]$Scope) -> [bool]   # Owner o User Access Administrator (anche ereditato)
function Get-KeyBasedSettings([hashtable]$Settings) -> [string[]]   # chiavi con AccountKey=/SharedAccessKey=/AZURE_ACCOUNT_KEY/…
```
- [ ] **Step 1: Fixtures** `tests/fixtures/az/`: `resource-list.json`, `containerapp-show-*.json`, `webapp-appsettings-*.json`, `functionapp-*.json`, `storage-list.json`, `servicebus-show.json`, `role-assignments.json` costruiti (nomi `polis-prod-*`, valori segreti **finti** `AccountKey=FAKE==`).
- [ ] **Step 2: Test** `Discovery.Tests.ps1` con `Mock Invoke-Az` che smista sugli argomenti: inventario trova 5 container app mappate + 1 unmapped; `polis-prod-api-storage` → `Excluded=$true, KeyBased=$true`; `polis-prod-sal-import-web` → Excluded; storage `polisprodstorage` → `Role='data'`; un secondo account referenziato solo da `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` → `Role='host'`; `MissingExpected` contiene `orchestrator-func` se assente; `Test-OperatorRole` vero con assignment `Owner` ereditata dalla subscription, falso con solo `Contributor`.
- [ ] **Step 3: Run** → FAIL. **Step 4: Implementa**. **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** `git commit -am "feat(tools): resource discovery and classification"`.

### Task 12: `lib/Rbac.ps1`

**Interfaces (Produces):**
```powershell
function Ensure-SystemIdentity([pscustomobject]$Service) -> [string] principalId     # containerapp/webapp/functionapp identity assign
function Ensure-RoleAssignment([string]$PrincipalId, [string]$Role, [string]$Scope) -> [bool] created
function Wait-RoleAssignment([string]$PrincipalId, [string]$Role, [string]$Scope, [int]$TimeoutSec = 300) -> [void]  # poll 10 s, lancia a timeout
function Get-RequiredRoles([pscustomobject]$Service, [pscustomobject]$Inventory) -> @( @{Role;Scope} )
```
Regole `Get-RequiredRoles`: `needsStorage` → Blob Data Contributor su ogni storage `data`; `hostStorage` → Blob Data Owner + Queue Data Contributor sull'account `host`; `needsServiceBus` → Service Bus Data Owner; container app → AcrPull sull'ACR.
- [ ] **Step 1: Test**: `Get-RequiredRoles` per import_fatture → 3 ruoli; per sftp-sync → 4; per api-database → 1. `Wait-RoleAssignment retries until listed`: mock `Invoke-Az` ritorna `[]` due volte poi l'assignment → nessuna eccezione, 3 chiamate. `Ensure-RoleAssignment` non ricrea se presente (`created=$false`).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implementa**. **Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "feat(tools): identity and role assignment helpers"`.

### Task 13: `lib/Deploy.ps1`

**Interfaces (Produces):**
```powershell
function Build-ContainerImage([string]$Acr, [string]$Repository, [string]$Tag, [string]$Context, [string]$Dockerfile) -> [string] fullImage   # az acr build --image repo:tag --file … context
function Update-ContainerAppImage([string]$Rg, [string]$Name, [string]$Image) -> [void]          # az containerapp update --image + --registry-server … --registry-identity system
function Set-ContainerAppEnv([string]$Rg, [string]$Name, [hashtable]$Add, [string[]]$Remove, [string[]]$RemoveSecrets)
function Publish-FunctionZip([string]$Rg, [string]$Name, [string]$Context) -> [void]              # Compress-Archive escludendo tests/__pycache__; az functionapp deployment source config-zip; SCM_DO_BUILD_DURING_DEPLOYMENT=true
function Set-WebAppSettings([string]$Rg, [string]$Name, [hashtable]$Add, [string[]]$Remove)
function Test-WebAppDeployedCommit([string]$Rg, [string]$Name, [string]$Sha) -> [bool]            # az webapp log deployment list → id/commit contiene Sha
function Ensure-HostStorageAccount([string]$Rg, [string]$Location, [pscustomobject]$Inventory) -> [string] accountName   # crea polisprodfunchost(+4 random) Standard_LRS StorageV2 TLS1.2 no public blob, se Inventory non ha già 'host' distinto da 'data'
function Move-FunctionShares([pscustomobject]$FunctionService, [string]$HostAccount) -> [void]   # content share + mount /data (se presente) su host account, copia file
```
- [ ] **Step 1: Test** (mock `Invoke-Az`, ispezione degli argomenti catturati): `Build-ContainerImage` passa `--image polis-import:20261030-120000 --file container_app/Dockerfile`; `Update-ContainerAppImage` include `--registry-identity system`; `Set-ContainerAppEnv` rende `--remove-env-vars A B` e `--set-env-vars X=Y`; `Test-WebAppDeployedCommit` vero/falso su fixture; `Ensure-HostStorageAccount` non crea se esiste già un account `host` ≠ `data`, crea con nome che matcha `^polisprodfunchost[a-z0-9]{0,4}$` altrimenti.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implementa**. **Step 4: Run** → PASS. **Step 5: Commit** `git commit -am "feat(tools): build, deploy and settings operations"`.

### Task 14: `lib/Verify.ps1`

**Interfaces (Produces):**
```powershell
function Test-ContainerAppHealthy([pscustomobject]$Service, [string[]]$ErrorPatterns, [int]$ObserveSec = 180) -> @{ Ok; Detail }  # forza 1 replica se minReplicas=0, legge log, ripristina
function Test-WebAppHealthy([pscustomobject]$Service, [string]$HealthPath, [string[]]$ErrorPatterns) -> @{ Ok; Detail }    # HTTP 200 (Invoke-WebRequest) + log tail 60 s
function Test-FunctionAppHealthy([pscustomobject]$Service, [string[]]$ErrorPatterns) -> @{ Ok; Detail }  # stato Running + 2 min log
function Test-LogClean([string]$LogText, [string[]]$ErrorPatterns) -> [bool]
```
- [ ] **Step 1: Test**: `Test-LogClean` falso con riga `… AuthenticationFailed …`, vero su log pulito, insensibile a case; `Test-ContainerAppHealthy` chiama `containerapp update --min-replicas 1` e poi `--min-replicas 0` quando minReplicas era 0 (verifica ordine chiamate); `Test-WebAppHealthy` ritorna `Ok=$false` con HTTP 503 (mock `Invoke-WebRequest`).
- [ ] **Step 2-5:** FAIL → implementa → PASS → `git commit -am "feat(tools): post-switch health verification"`.

### Task 15: `lib/Hardening.ps1`, `lib/Report.ps1`, backup

**Interfaces (Produces):**
```powershell
function Save-ServiceBackup([pscustomobject]$Service, [string]$WorkDir) -> [string] path   # work/backup/<name>.json; NON sovrascrive se esiste
function Restore-ServiceBackup([string]$Path) -> [void]
function Test-HardeningAllowed([pscustomobject]$Inventory, [hashtable]$State, [bool]$Force) -> @{ Allowed; Reasons[] }
function Invoke-Hardening([pscustomobject]$Inventory) -> [void]   # storage data: allow-shared-key-access false; servicebus: disable-local-auth true; acr: admin-enabled false; keys renew key1+key2 su tutti gli storage; riapplica key host a content share/mount
function New-RunReport([pscustomobject]$Inventory, [hashtable]$State, [string]$OutPath) -> [string]
```
- [ ] **Step 1: Test**: `Save-ServiceBackup does not overwrite existing backup`; `Test-HardeningAllowed` nega con un servizio `KeyBased` non mappato (reason cita il nome), nega con `polis-prod-api-storage` key-based, nega con uno step `failed` in fase 7, permette con tutto verde, permette con `Force` ma `Reasons` non vuote; `New-RunReport` produce markdown con una riga per servizio, sezione "Eccezioni" che cita l'account host, sezione "Software rimosso".
- [ ] **Step 2-5:** FAIL → implementa → PASS → `git commit -am "feat(tools): hardening gate, backup and report"`.

### Task 16: `lib/Fallback.ps1`

**Interfaces (Produces):**
```powershell
function Test-AnthropicKey([string]$Key) -> [bool]              # GET https://api.anthropic.com/v1/models, header x-api-key, anthropic-version 2023-06-01
function Ensure-ClaudeCode() -> [string] exePath                 # irm https://claude.ai/install.ps1 | iex ; cerca %USERPROFILE%\.local\bin\claude.exe
function Invoke-ClaudeFallback([hashtable]$Context, [string]$Phase, [string]$Service, [string]$ErrorText, [int]$Attempt) -> @{ Ok; ResultPath }
#   prompt da tools/fallback/prompt.txt con placeholder; claude -p --dangerously-skip-permissions --output-format json --max-turns 40, cwd=tools/; legge work/fallback/<phase>-<service>-<attempt>.result.json
function Read-FallbackKey() -> [string]   # Read-Host -AsSecureString, vuoto = skip
```
- [ ] **Step 1: Test**: `Test-AnthropicKey` vero con mock `Invoke-WebRequest` 200, falso con eccezione 401; `Invoke-ClaudeFallback` costruisce il prompt contenente fase, servizio, errore e le 5 regole (stringhe esatte: "non disabilitare chiavi", "non cancellare risorse", "non toccare servizi fuori perimetro", "non toccare polis-api-storage", "scrivi il risultato in"); non viene mai chiamato più di 2 volte per step (test sul chiamante in Task 18).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implementa** + `tools/fallback/CLAUDE.md` (struttura tools/, `$ctx`, convenzioni, comandi vietati) + `tools/fallback/prompt.txt`. **Step 4:** PASS. **Step 5:** `git commit -am "feat(tools): claude code fallback"`.

### Task 17: `lib/Cleanup.ps1`

**Interfaces (Produces):**
```powershell
function Remove-AzureCli() -> [void]       # msiexec /x {ProductCode} /qn via registro Uninstall (DisplayName 'Microsoft Azure CLI'); fallback winget uninstall --id Microsoft.AzureCLI
function Remove-ClaudeCode() -> [void]     # rimuove %USERPROFILE%\.local\bin\claude*, %USERPROFILE%\.claude, voce PATH utente
function Remove-AzureProfile() -> [void]   # az logout (se az presente), Remove-Item %USERPROFILE%\.azure
function Remove-WorkDir([string]$WorkDir, [bool]$KeepBackup) -> [void]
function Get-CleanupSummary() -> [string[]]  # per il report
```
- [ ] **Step 1: Test** (mock `Start-Process`, `Remove-Item`, `Get-ItemProperty`): `Remove-AzureCli` cerca il ProductCode e invoca msiexec con `/x <code> /qn`; `Remove-WorkDir` con `KeepBackup` conserva `work/backup`; `Remove-ClaudeCode` toglie solo la voce PATH che contiene `\.local\bin`.
- [ ] **Step 2-5:** FAIL → implementa → PASS → `git commit -am "feat(tools): operator machine cleanup"`.

### Task 18: Fasi e `Invoke-PolisMigration.ps1`

**Files:** Create `tools/phases/00-Preflight.ps1 … 10-Cleanup.ps1`, `tools/Invoke-PolisMigration.ps1`, `tests/ps/Phases.Tests.ps1`, `tests/ps/Orchestration.Tests.ps1`.

**Interfaces:** `Invoke-PolisMigration.ps1 -Rg <string> -WorkDir <string> [-WhatIf] [-ForceHardening] [-KeepBackup] [-SmokeTest] [-AnthropicApiKey <string>] [-Rollback] [-AfterHardening] [-NonInteractive]` → exit code 0 tutto verde, 2 completato con servizi rossi, 3 fermato (ruolo mancante / conferma negata). Ogni fase: `Invoke-PhaseNN -Context $ctx -> $ctx`; ogni step per servizio avvolto in `Invoke-StepWithFallback -Context -Phase -Service -Action {scriptblock} -Verify {scriptblock}` (1 retry, poi fallback max 2, poi `failed`).

Mappa fase → funzioni: 00 preflight (admin, PS ≥ 5.1, TLS 1.2, internet); 01 bootstrap-check (az presente, `az account show`, `Test-OperatorRole`); 02 discovery+piano (`Get-PolisInventory`, `Save-ServiceBackup` per servizio, tabella a video via `Format-Table`, conferma `S/N` salvo `-NonInteractive`); 03 identity (`Ensure-SystemIdentity`); 04 rbac (`Get-RequiredRoles` → `Ensure-RoleAssignment` → `Wait-RoleAssignment`); 05 build&deploy (container app: `Build-ContainerImage` + `Update-ContainerAppImage`; function: `Ensure-HostStorageAccount` → `Move-FunctionShares` → `Publish-FunctionZip`; web app: `Test-WebAppDeployedCommit` con `expectedCommit`, se falso → `skipped` con motivo); 06 settings (`ConvertTo-IdentitySettings` → `Set-ContainerAppEnv`/`Set-WebAppSettings`); 07 verify; 08 hardening (`Test-HardeningAllowed`, conferma, `Invoke-Hardening`); 09 report (`New-RunReport` su Desktop); 10 cleanup (chiamato dal bootstrap, non da qui: vedi Task 19).

- [ ] **Step 1: Test** `Orchestration.Tests.ps1`: con tutte le lib mockate, (a) `-WhatIf` esegue fasi 00-07 e 09, mai 08; (b) uno step che fallisce 3 volte produce `failed`, `Invoke-ClaudeFallback` chiamato esattamente 2 volte, le altre fasi proseguono; (c) `Test-OperatorRole` falso → exit 3 prima della fase 02; (d) rilancio con `state.json` che ha fase 05 `done` per un servizio → `Build-ContainerImage` non richiamato per quello; (e) web app con `expectedCommit` non trovato → fase 06 la salta.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implementa** fasi e orchestratore. **Step 4: Run** tutta la suite `Invoke-Pester tests\ps` → PASS.
- [ ] **Step 5: Commit** `git commit -am "feat(tools): phased migration orchestrator"`.

### Task 19: Bootstrap `Run-PolisUpdate`

**Files:** Create `bootstrap/Run-PolisUpdate.template.ps1`, `bootstrap/New-Bootstrap.ps1`, `tests/ps/Bootstrap.Tests.ps1`.

Template (placeholder `__REPO_ZIP_URL__`, `__ZIP_SHA256__`, `__ANTHROPIC_KEY__`, `__COMMIT__`): auto-elevazione (`Start-Process powershell -Verb RunAs` se non admin), `[Net.ServicePointManager]::SecurityProtocol = Tls12`, installa Azure CLI se `Get-Command az` fallisce (download MSI `https://aka.ms/installazurecliwindowsx64` → `msiexec /i … /qn`; fallback `winget install --id Microsoft.AzureCLI -e --silent`; refresh PATH), `az login` (browser; `--use-device-code` se `-DeviceCode`), download zip → `Get-FileHash` deve coincidere → `Expand-Archive` in `%LOCALAPPDATA%\PolisUpdate\<commit>`, scelta sub/RG (auto se unico RG che matcha `polis`), chiama `tools\Invoke-PolisMigration.ps1` passando i parametri, poi `tools\phases\10-Cleanup.ps1` (sempre, anche su errore, in `finally`), stampa path del report. `New-Bootstrap.ps1 -Commit <sha> -AnthropicApiKey <key>`: scarica lo zip di quel commit da GitHub, calcola SHA-256, sostituisce i placeholder, scrive `bootstrap/Run-PolisUpdate.ps1` (gitignored).

- [ ] **Step 1: Test**: `New-Bootstrap.ps1` con `Mock Invoke-WebRequest` che restituisce un file fisso → output contiene lo SHA calcolato e nessun placeholder residuo; il template passa `[System.Management.Automation.Language.Parser]::ParseFile` senza errori; il template contiene `finally` che invoca `10-Cleanup.ps1`.
- [ ] **Step 2-4:** FAIL → implementa → PASS.
- [ ] **Step 5: Commit** `git commit -am "feat(bootstrap): self-contained operator launcher template"`.

## Parte D — Pubblicazione, Web App e prova generale

### Task 20: Scansione segreti e pubblicazione repo

**Files:** Create `tools/scrub/Test-NoSecrets.ps1`, `tests/ps/Scrub.Tests.ps1`; Modify `README.md` (istruzioni operatore in italiano).

- [ ] **Step 1: Test**: `Test-NoSecrets -Path <tmp>` su cartella con file contenente `AccountKey=abc==` → ritorna 1 finding con path e pattern; su cartella pulita → 0; file in `.gitignore` patterns (es. `.prodenv`) presente → finding "file vietato". Pattern: `AccountKey=`, `SharedAccessKey=`, `accesskey=`, `sk-ant-`, `(?i)password\s*=\s*["'][^"']{4,}`, `AZURE_AI_API_KEY=\S{10,}`, `Bearer [A-Za-z0-9\-_]{20,}`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implementa**. **Step 4: Run** → PASS; poi `tools\scrub\Test-NoSecrets.ps1 -Path .` sull'intero repo → **0 finding** (correggere finché non è zero: in particolare default hardcoded nei sorgenti copiati).
- [ ] **Step 5: Pubblica**: `gh repo create Jonato01/polis-rbac-migration --public --source . --remote origin --push`. Verifica `gh repo view --json visibility` = PUBLIC e che nel repo remoto non ci sia nessun file vietato (`gh api repos/Jonato01/polis-rbac-migration/git/trees/main?recursive=1 | ConvertFrom-Json`, filtrare path contro la lista).
- [ ] **Step 6: Commit** (già pushato) — annotare lo SHA di `main` nel report di consegna.

### Task 21: Web App — patch identity nei repo originali e push su `origin` + `prod`

**Files (repo originali):**
- `polis-sal-sftp-api/main.py:68-69,79,663,893,1073,1283,1336,2398,2949,3033,3321,3381,3425,3442,3482,3712,4075,4207,4235,4605,805-816,285-295,328-338`, `polis-sal-sftp-api/azure_clients.py` (copia), `requirements.txt` (+`azure-identity`)
- `polis-api-database/api/notifications.py:63-87,235-284,344-356`, `polis-api-database/api/azure_clients.py`
- `polis-ai-2/main.py:127-138,235-248,525-536`, `polis-ai-2/azure_clients.py`, `requirements.txt` (+`azure-identity`)
- `polis-frontend/front_end/views.py:10` (rimuovere import inutilizzato; **nessun push su prod**, solo origin)

- [ ] **Step 1: Per `polis-sal-sftp-api`**: nuovo modulo `storage.py` con `def blob_service() -> BlobServiceClient` (helper, `account_name_var="AZURE_STORAGE_ACCOUNT_NAME"`, fallback a `AZURE_ACCOUNT_NAME`+`AZURE_ACCOUNT_KEY` ricostruendo la connection string se `AZURE_ACCOUNT_KEY` è settata) e `def blob_sas_url(blob_name, permission, expiry) -> str` che usa `get_blob_sas_token`. Test `tests/test_storage.py` nel repo (pytest dal suo `.venv`): `test_sas_url_uses_user_delegation_when_no_key` (mock), `test_legacy_key_mode_builds_connection_string`. Sostituire le 12 costruzioni di `connection_string` e le 8 `generate_blob_sas` con le due funzioni; i 3 `ServiceBusClient.from_connection_string` con `get_servicebus_client()`.
- [ ] **Step 2: Run** `.\.venv\Scripts\pytest tests -q` + `py -3.12 -m compileall -q .` + grep `from_connection_string` (solo `EmailClient` ammesso). PASS.
- [ ] **Step 3: Commit** su master: `git commit -am "feat: managed identity for storage and service bus"`; `git push origin master`; `git push prod master`. Attendere fine build Oryx: `az webapp log deployment list` **non** disponibile da qui (RG prod non visibile): verificare con `Invoke-WebRequest https://polis-prod-sal-import.azurewebsites.net/health` (o `/docs`) → 200 entro 10 minuti; annotare lo SHA in `tools/mapping.json` → `expectedCommit`.
- [ ] **Step 4: Ripetere** Step 1-3 per `polis-api-database` (3 sostituzioni; test `tests/test_notifications_sb.py::test_uses_identity_client` con mock) e `polis-ai-2` (3 sostituzioni; test analogo). Endpoint di verifica: `https://polis-prod-api-database.azurewebsites.net/` e `https://polis-prod-api-ai.azurewebsites.net/`.
- [ ] **Step 5: `polis-frontend`**: rimuovere l'import, `git commit -am "chore: drop unused ServiceBusClient import"`, `git push origin master` soltanto.
- [ ] **Step 6:** aggiornare `expectedCommit` dei 3 servizi in `tools/mapping.json`, commit `git commit -am "chore(tools): pin deployed web app commits"` e push.

### Task 22: Prova generale sul RG Divitech `polis-rg` e generazione bootstrap

- [ ] **Step 1: WhatIf completo** dal PC dello sviluppatore (az già loggato): `tools\Invoke-PolisMigration.ps1 -Rg polis-rg -WorkDir $env:TEMP\polis-whatif -WhatIf -NonInteractive`. Expected: exit 0, piano stampato con 5 container app + function + web app mappate, `polis-api-storage` e `polis-sal-import-web` come esclusi, nessun comando mutante eseguito (log).
- [ ] **Step 2: Correzioni** emerse dal WhatIf (mapping, parsing di output `az` reali): commit `fix(tools): …` e push.
- [ ] **Step 3: Run reale su `polis-rg`** — **solo dopo OK esplicito del committente** (modifica l'ambiente Divitech: identity, ruoli, nuovo storage host, nuove immagini). Fasi 1-7; hardening bloccato automaticamente da `polis-api-storage` key-based (atteso). Verificare report e che la pipeline Divitech funzioni ancora (upload zip di prova su collaudo Divitech).
- [ ] **Step 4: Genera il bootstrap**: `bootstrap\New-Bootstrap.ps1 -Commit (git rev-parse HEAD) -AnthropicApiKey <key fornita>`; eseguire `Test-NoSecrets` su `bootstrap\Run-PolisUpdate.ps1` **aspettandosi** il finding `sk-ant-` (conferma che la key è nel bootstrap e che il file è gitignored: `git check-ignore bootstrap\Run-PolisUpdate.ps1` → path stampato).
- [ ] **Step 5: Prova del bootstrap su macchina pulita**: VM Windows (Hyper-V/Sandbox) senza az/git: eseguire `Run-PolisUpdate.ps1 -WhatIf` contro `polis-rg` con login dell'utente sviluppatore. Expected: installa az, scarica zip, hash ok, stampa piano, cleanup rimuove az (`Get-Command az` fallisce dopo) e `%USERPROFILE%\.azure` assente.
- [ ] **Step 6: Consegna**: `bootstrap\Run-PolisUpdate.ps1` + istruzioni operatore (README §Operatore) via canale privato. Commit finale `docs: operator instructions` e push.

---

## Self-review (eseguita)

- **Copertura spec**: §6 → Task 1, 19, 20; §7 → Task 19, 18; §8 → Task 8, 9, 11; §9.1 → 3; §9.2 → 4; §9.3 → 5; §9.4 → 5 (sftp dentro import-sal); §9.5 → 6; §9.6/§9.9 → 21 (+ settings in 18); §9.7 → 7, 13; §9.8 → 9 (entry `orchestrator-func`), 18; §10 → 18; §11 → 14; §12 → 16; D3 → 13; D9 → 15; D13 (SP creds) → **aggiunto**: `Get-KeyBasedSettings` in Task 11 include `AZURE_CLIENT_SECRET` e la fase 06 rimuove le tre variabili SP solo con conferma (`Orchestration.Tests` caso (f): servizio con `AZURE_CLIENT_SECRET` → richiesta conferma, in `-NonInteractive` → skipped). D15 (`-SmokeTest`) → Task 18 fase 07: con il flag invia un messaggio su `import-response` tramite `az servicebus` … non esiste comando CLI di invio: usare `Invoke-RestMethod` sull'endpoint REST del namespace con token `az account get-access-token --resource https://servicebus.azure.net`; verifica consumo = `az servicebus queue show … --query countDetails.activeMessageCount` torna a 0 entro 60 s. Rollback (`-Rollback`) → Task 15 `Restore-ServiceBackup` + Task 18 parametri.
- **Coerenza nomi**: `Invoke-Az`, `Get-PolisInventory`, `Resolve-ServiceMapping`, `ConvertTo-IdentitySettings`, `Ensure-*`, `Test-*Healthy`, `Invoke-ClaudeFallback` usati con gli stessi nomi in Task 18.
- **Proporzione**: il piano fissa interfacce e test, non corpi; i corpi restano al builder.
