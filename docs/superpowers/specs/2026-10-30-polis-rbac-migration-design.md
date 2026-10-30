# Polis — migrazione a Entra ID / RBAC e rilascio modifiche luglio 2026

Data: 2026-10-30
Stato: bozza per approvazione

## 1. Intento e perché

Il cliente ha aperto una finding di sicurezza: i servizi Polis accedono a Storage Account,
Service Bus (e AI Search) con Access Key / connection string. Richiede la migrazione ad
autenticazione **Microsoft Entra ID + RBAC** (managed identity), con intervento sia sulla
configurazione Azure sia sul codice applicativo.

In parallelo esistono modifiche applicative di luglio 2026 mai rilasciate in produzione:

- `import_fatture` (container app import Excel): 4 fix non committati del 1–2 luglio
  (numero SAL in cifre romane, mapping SAL→anno/mesi dinamico via `SALManager`,
  dedup fatture su 5 campi null-safe).
- `polis-sal-sftp-api`: commit del 9–10 luglio (`8774d16`, `fd7fa12`, `36a7d66`, `6cc7f0d`:
  cast DECIMAL negli arrotondamenti, report finale in Excel, rimozione summary init).

Vincolo operativo che modella tutto il design: **chi ha accesso al resource group di
produzione non è lo sviluppatore**. L'aggiornamento verrà eseguito da un operatore del
cliente (ruolo amministratore sul RG) su un **PC Windows completamente vergine**: niente git,
niente Azure CLI, niente Docker, niente account GitHub. L'operatore deve ricevere **un solo
file** e lanciarlo.

## 2. Obiettivi

1. Tutti i servizi Polis presenti nel RG di produzione (tranne le esclusioni in §3) accedono a
   Storage e Service Bus tramite managed identity; nessuna connection string / account key
   nelle app settings o nelle env dei container.
2. Le modifiche di luglio 2026 di `import_fatture` e `polis-sal-sftp-api` sono in produzione.
3. Al termine, shared key access sugli storage account dati e local auth sul namespace
   Service Bus sono **disabilitati**; le chiavi residue (storage host, ACR) sono ruotate.
4. L'operatore esegue tutto con un unico script PowerShell autosufficiente, con
   fallback automatico (Claude Code + API key Anthropic) per sbloccare errori imprevisti.
5. Al termine il PC dell'operatore è riportato allo stato iniziale: nessun programma
   installato, nessuna credenziale, nessun sorgente residuo. Resta solo il report finale.

## 3. Non-obiettivi

- `polis-api-storage` e Azure AI Search: **esclusi dallo script**, il codice lo gestisce il
  cliente. Il rilascio di quel codice in prod avviene dal PC dello sviluppatore con
  `git pull origin master` + `git push prod master` (il remote SCM `prod` è raggiungibile da
  lì, verificato con `git fetch`). Al 2026-10-30 `origin/master` = `prod/master` = `7e47a73`
  (22 giu): nessun codice identity ancora arrivato, niente da pushare. Prima dell'hardening
  lo script controlla comunque le settings di `polis-prod-api-storage`: se usa ancora
  connection string, blocca l'hardening (D9).
- `polis-sal-import-web` (frontend FastAPI del portale SAL): nessun accesso Azure, non si tocca.
- Chiavi di Azure OpenAI, Document Intelligence, Mistral, Azure Communication Services
  (email), credenziali MySQL, `INTERNAL_API_KEY`, JWT: fuori mandato, restano com'erano.
- Ambiente collaudo (`polis-collaudo`, `*-col*`): non toccato.
- Refactoring del codice oltre lo stretto necessario alla migrazione credenziali.

## 4. Anti-obiettivi (fallimento anche se "funziona")

- Pipeline import SAL (upload zip → function → import Excel/PDF → report) rotta in produzione
  dopo l'intervento, o rotta a metà con chiavi già disabilitate e senza rollback.
- Segreti (chiavi Azure, password DB, key Anthropic) finiti nel repository GitHub pubblico.
- Chiavi lasciate attive senza che il report lo dica esplicitamente.
- Software, token Azure (`~/.azure`), Claude Code o sorgenti lasciati sul PC dell'operatore.
- Lo script che "va avanti" ignorando un servizio che in realtà esiste in prod.

## 5. Vincoli e fatti accertati

- PC operatore: Windows (10/11), PowerShell 5.1 disponibile di default, diritti di
  amministratore locale (necessari per installare Azure CLI), accesso internet.
  Nessun tool installato. L'operatore è **amministratore sul RG** (Owner): lo script lo
  verifica comunque e si ferma se manca Owner o User Access Administrator.
- Il RG di produzione (`rg-polis`, tenant del cliente) **non è raggiungibile** dalla macchina
  dello sviluppatore. Il RG Divitech `polis-rg` (subscription "Divitech Spa Pagamento in base
  al consumo") è l'ambiente usato per la ricognizione, ma **non è speculare**: lo storage
  dati del mirror (`backupdivitech`) sta in un altro RG, e le container app figlie
  dell'orchestrator non esistono nel mirror. Nomi prod noti dagli script `deploy-prod.ps1` e
  dai `.prodenv` locali: RG `rg-polis`, ACR **dedicato** `polisregistryprod` (non condiviso con
  Divitech/collaudo), Container Apps Environment `polis-env-prod`, app
  `polis-prod-import-app` (immagine `polis-prod-import`), `servicebus-bridge-prod`,
  `autogen-prod-pdf-import`, `import-sal-orchestrator-prod`, `sftp-processor-prod`,
  `fatture-processor-prod`, `db-backup-processor-prod`, `bdc-processor-prod`, …
  (immagini `<servizio>-service-prod`), Web App `polis-prod-sal-import`,
  `polis-prod-api-database`, `polis-prod-api-ai`, `polis-prod-frontend`,
  `polis-prod-sal-import-web`, Function `polis-prod-sftp-upload`. **Un solo storage
  account** `polisprodstorage` per tutto (dati, `AzureWebJobsStorage`, content share),
  Service Bus `polis-messaging-prod`. **Lo script non deve dipendere dai nomi**: scopre le
  risorse per tipo e le mappa ai repo per nome immagine (senza suffisso `-prod`) / keyword
  (§8), e risolve storage/Service Bus/ACR per nome in tutta la subscription, non solo nel RG.
- Nessun Docker sul PC: le immagini si costruiscono in cloud con `az acr build`.
- Azure Files (content share della Function App su piano Elastic Premium e mount SMB
  `/data`) **non supporta identity-based access**: serve una account key. Vedi decisione D3.
- `polis-orchestrator-func`: Function App Python 3.9 con zero funzioni deployate nel RG
  Divitech, nessun sorgente locale. In prod potrebbe esistere e avere funzioni.
- `polis-sal-sftp-api` genera SAS di download firmati con l'account key
  (`generate_blob_sas(account_key=...)`, ~8 occorrenze).
- **Pipeline import in uso oggi** (confermato dal committente): Function App unzip
  (`polis-sal-sftp-sync`) → `polis-prod-import-app` per gli Excel (`import_fatture`) →
  `autogen-prod-pdf-import` per i PDF. Tutto il resto di `import-sal` (orchestrator
  `import-sal-orchestrator`, container app `*-processor-prod`, sftp-processor, bulk-sync…)
  sono **versioni precedenti dell'import**, possibilmente ancora deployate ma non in uso.
  Vanno migrate **se presenti nel RG** (altrimenti terrebbero chiavi vive), ma segnalate nel
  report come "legacy". Il sorgente locale di `import-sal` (orchestrator via code Service
  Bus; codice ACI in `import_sal_utils.create_container_group` morto, nessun chiamante) è
  la base per ricostruirle.
- Dal PC dello sviluppatore i remote SCM `prod` delle Web App (`polis-prod-*.scm.azurewebsites.net`)
  sono raggiungibili in fetch/push: il **codice** delle Web App può essere rilasciato da lì
  con `git push`, senza passare dall'operatore. Vedi §15.
- Tutte le container app pullano da ACR con credenziale admin (secret
  `polisregistryazurecrio-polisregistry`): anche questa è una "chiave" e va migrata.
- La key Anthropic fornita (`sk-ant-usr-…`) è valida oggi (verificata con `GET /v1/models`,
  HTTP 200) e **scade il 2026-10-31**. Deve stare solo nello script consegnato privatamente,
  mai nel repo.
- GitHub: account `Jonato01` autenticato via `gh` sulla macchina dello sviluppatore.
  Il repo deve essere **pubblico** (richiesta esplicita: nessun account GitHub sul PC
  operatore). Conseguenza accettata dal committente: il codice Polis diventa visibile;
  conseguenza non negoziabile: zero segreti nel repo.

## 6. Architettura della consegna

Due artefatti.

### 6.1 Repository GitHub pubblico `polis-rbac-migration`

Contiene **solo codice e script, nessun segreto**:

```
polis-rbac-migration/
  README.md                     # cosa fa, come si lancia, cosa aspettarsi
  tools/
    Invoke-PolisMigration.ps1   # entry point delle fasi (chiamato dal bootstrap)
    lib/*.ps1                   # funzioni: discovery, rbac, deploy, settings, verify, report
    phases/0x-*.ps1             # una fase per file, idempotenti
    mapping.json                # regole servizio -> repo -> modifiche (vedi §8)
    fallback/CLAUDE.md          # contesto per Claude Code quando interviene
  src/
    _shared/azure_clients.py    # helper identity, fonte unica; copiato nei build context
    import_fatture/             # albero completo + fix luglio + migrazione identity
    import-sal/                 # orchestrator + services/* necessari, migrati
    servicebus-bridge/
    polis-sal-sftp-sync/        # function app (deployata dallo script)
  docs/superpowers/specs/       # questo documento
```

Le Web App (`polis-sal-sftp-api`, `polis-api-database`, `polis-ai-2`, `polis-frontend`) **non**
stanno nel repo pubblico: il loro codice viene modificato nei repo originali e rilasciato via
SCM dal PC dello sviluppatore (D18). Meno codice esposto, nessuna doppia copia.

```
```

Lo script bootstrap scarica il repo come **zip da GitHub a un commit SHA fissato**
(`https://github.com/Jonato01/polis-rbac-migration/archive/<sha>.zip`): niente git, niente
autenticazione, integrità garantita dallo SHA. In più verifica l'hash SHA-256 dello zip
(valore scritto nel bootstrap).

### 6.2 Script bootstrap `Run-PolisUpdate.ps1` (consegnato privatamente)

Unico file che riceve l'operatore. Contiene in testa, come variabili:
URL del repo, commit SHA, hash dello zip, **key Anthropic in chiaro** (scelta esplicita del
committente, key a scadenza giornaliera), eventuali override (nome RG, subscription).
Fa: elevazione a admin, installazione Azure CLI, login, download repo, lancio
`Invoke-PolisMigration.ps1`, cleanup finale. Non contiene logica di migrazione: così la
logica è versionata nel repo e il file con il segreto è minimo.

## 7. Flusso lato operatore

1. Riceve `Run-PolisUpdate.ps1` via canale privato. Lo salva sul Desktop.
2. Tasto destro → "Esegui con PowerShell" (o `powershell -ExecutionPolicy Bypass -File …`).
   Lo script si rilancia elevato (UAC) se non lo è già.
3. Lo script installa Azure CLI (MSI x64 silente da `https://aka.ms/installazurecliwindowsx64`,
   fallback `winget install Microsoft.AzureCLI`), apre `az login` (browser / device code).
4. Sceglie subscription e RG: automatico se c'è un solo RG con "polis" nel nome, altrimenti
   menu. Verifica ruolo Owner / User Access Administrator sul RG: se manca **si ferma** con
   messaggio chiaro.
5. Fase di **discovery + piano**: stampa tabella "servizio → azione → ruoli → codice da
   deployare" e lista "trovato ma non mappato" / "atteso ma assente". Chiede conferma `S/N`.
6. Esegue le fasi 3–6 (identity, RBAC, deploy, switch settings, verifica). Output a video
   e in log.
7. **Hardening** (disabilitazione chiavi): mostra riepilogo verifiche e chiede una seconda
   conferma esplicita. Con `N` termina lasciando chiavi attive e lo scrive nel report.
8. Scrive `Desktop\polis-update-report-<timestamp>.md` e `.log`.
9. **Cleanup**: `az logout`, rimozione `~/.azure`, disinstallazione Azure CLI (msiexec /x),
   disinstallazione Claude Code se installato (cartella `~/.local/bin/claude*`, `~/.claude`,
   voce PATH), cancellazione cartella di lavoro (zip, sorgenti, backup settings).
   Resta solo report + log. Il report finisce con l'elenco di ciò che è stato rimosso.

Rilancio: lo script è idempotente. Se interrotto, si rilancia e riprende (ogni fase controlla
lo stato reale prima di agire).

## 8. Discovery e mappatura servizi

Lo script enumera il RG e classifica:

- **Storage account**: tutti quelli referenziati da connection string / `AZURE_ACCOUNT_NAME`
  / mount nelle settings dei servizi. Classificazione:
  - *host*: account referenziato da `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` di una
    Function App (nel RG Divitech: `polissftpstorage`);
  - *dati*: tutti gli altri (prod: `polisprodstorage`).
- **Service Bus namespace**: da `Endpoint=sb://…` nelle settings (prod `polis-messaging-prod`).
- **ACR**: `Microsoft.ContainerRegistry/registries` nel RG.
- **Container App**: mappate per *nome repository dell'immagine*, normalizzato togliendo i
  suffissi `-prod` / `-coll` e il prefisso `polis-prod-` (in prod: `polis-prod-import`,
  `sftp-service-prod`, `fatture-service-prod`, …):

  | immagine normalizzata | sorgente nel repo | Dockerfile |
  |---|---|---|
  | `polis-import` | `src/import_fatture` (context root) | `container_app/Dockerfile` |
  | `autogen-pdf-import` | `src/import-sal/services/import/autogen-pdf-import` | `Dockerfile` |
  | `import-sal-orchestrator` | `src/import-sal` (context root) | `Dockerfile` (vedi §9.3: condizionato) |
  | `servicebus-bridge` | `src/servicebus-bridge` | `Dockerfile` |
  | `sftp-service` | `src/import-sal/services/sftp` | `Dockerfile` |
  | `sftp-bulk-sync-service` | `src/import-sal/services/sftp` | `Dockerfile.bulk-sync` |
  | `db-backup-service` | `src/import-sal/services/backup-db` | `Dockerfile` |
  | `container-backup-service` | `src/import-sal/services/backup-container` | `Dockerfile.container-backup` |
  | `documentation-service` | `src/import-sal/services/documentation` | `Dockerfile.documentation` |
  | `storage-cleanup-service` | `src/import-sal/services/cleanup` | `Dockerfile.storage-cleanup` |
  | `personale-service` | `src/import-sal/services/import/personale` | `Dockerfile.personale` |
  | `fatture-service` | `src/import-sal/services/import/fatture` | `Dockerfile.fatture` |
  | `db-rollback-sal-service` | `src/import-sal/services/rollback` | `Dockerfile` |
  | `aq-service`, `aq-missing-service`, `bdc-service`, `bdc-missing-service`, `fatture-pdf-service`, `iva-indetraibile-service`, `lettere-incarico-service`, `nota-metodologica-service`, `nota-trasmissione-service`, `passaggi-livello-service`, `timesheet-service`, `vap-service` | `src/import-sal/services/import/pdf/<x>` | `Dockerfile.<x>` nella cartella |

  I nomi esatti delle immagini vanno letti dagli script `deploy-*.ps1` di ogni servizio e
  fissati in `mapping.json`; il builder li verifica uno per uno. Si ricostruiscono **solo**
  le immagini di container app effettivamente presenti nel RG di prod.
- **Web App / Function App**: per keyword sul nome (regex in `mapping.json`):

  | regex nome | sorgente | tipo | note |
  |---|---|---|---|
  | `sftp-upload` | `src/polis-sal-sftp-sync` | Function | timer `process_zip_timer` |
  | `sal-import$` oppure `sal-sftp-api` | `src/polis-sal-sftp-api` | Web App Python | include commit luglio |
  | `api-database` | `src/polis-api-database` | Web App Python | solo Service Bus |
  | `polis(-prod)?-ai$` | `src/polis-ai-2` | Web App Python | solo Service Bus |
  | `frontend` | `src/polis-frontend` | Web App Django | solo Service Bus |
  | `orchestrator-func` | nessuno | Function | solo settings (§9.8) |

- Qualunque risorsa con settings contenenti `AccountKey=`, `SharedAccessKey=`,
  `AZURE_ACCOUNT_KEY` o `*CONNECTION_STRING*` verso storage/service bus e **non mappata**
  finisce in "trovato ma non mappato": lo script non la tocca, la riporta, e in fase di
  hardening **si rifiuta di disabilitare le chiavi** finché esiste almeno un servizio non
  migrato (override possibile solo con flag esplicito `-ForceHardening`, segnalato nel report).
- Servizi previsti da `mapping.json` ma assenti nel RG: "non presente, non in uso" nel report.

## 9. Modifiche per servizio

Convenzioni comuni (Python):

- Nuove variabili d'ambiente al posto delle connection string:
  `AZURE_STORAGE_ACCOUNT_NAME`, `SERVICE_BUS_NAMESPACE` (FQDN `xxx.servicebus.windows.net`).
  In `import_fatture` i nomi seguono il prefisso già usato (`AZURE_SERVICEBUS_NAMESPACE`).
- Un modulo helper `azure_clients.py` (copiato in ogni build context, non condiviso tra
  repo) espone `get_blob_service_client()`, `get_servicebus_client()` (sync e aio) che usano
  `DefaultAzureCredential` (`azure-identity`). Compatibilità transitoria: se la nuova
  variabile manca e la vecchia connection string è presente, usa la connection string e
  logga un warning. Questo permette rollback lato settings senza ridistribuire codice.
- `azure-identity` aggiunto ai `requirements.txt` dove manca.
- Nessun'altra modifica funzionale oltre a quelle elencate.

Ruoli RBAC (assegnati a scope della risorsa, non del RG):

- Storage dati: `Storage Blob Data Contributor` a ogni identità che legge/scrive blob.
  Include il permesso `generateUserDelegationKey` necessario ai SAS (§9.6).
- Storage host (function): `Storage Blob Data Owner` + `Storage Queue Data Contributor`
  all'identità della Function App (richiesto da `AzureWebJobsStorage__accountName`).
- Service Bus: `Azure Service Bus Data Owner` (semplifica: tutti i servizi sia inviano sia
  ricevono).
- ACR: `AcrPull` a ogni container app e alla user-assigned identity dell'orchestrator.
- RG (solo identità orchestrator): `Azure Container Instances Contributor Role` e
  `Managed Identity Operator` (per creare ACI e agganciarvi la user-assigned identity).

### 9.1 `polis-import-app` ← `src/import_fatture`

- Codice: `blob_manager.py` → `BlobServiceClient(account_url, credential)`;
  `queue_manager.py` → `ServiceBusClient(fully_qualified_namespace, credential)`;
  `config.py`/`main.py` → nuove variabili e check "required" aggiornato.
- Include i 4 fix di luglio (oggi solo nel working tree locale): `SAL-8/import_personale_sal8.py`,
  `SAL-8/import_fatture_sal8.py`, `import_servizi_api.py`, `container_app/import_adapters/servizi_adapter.py`.
- Infra: system-assigned identity; ruoli storage dati, Service Bus, AcrPull;
  registry della container app passato a `--registry-identity system`; rimozione secret
  `polisregistryazurecrio-polisregistry` e delle env `AZURE_STORAGE_CONNECTION_STRING`,
  `AZURE_SERVICEBUS_CONNECTION_STRING`.
- Deploy: `az acr build -t polis-import:<timestamp> -f container_app/Dockerfile .` poi
  `az containerapp update --image …` con le nuove env.

### 9.2 `autogen-pdf-import` ← `src/import-sal/services/import/autogen-pdf-import`

- Codice: `services/blob_service.py` (aio `BlobServiceClient`), `main.py` e
  `services/dispatcher.py` (aio `ServiceBusClient`), `config/__init__.py` e `config_root.py`
  (nuove variabili, lista required). `MAX_CONTAINER_INSTANCES` suggerisce che anche questo
  servizio possa creare container: il builder verifica e, se sì, applica lo schema §9.3.
- Infra: come 9.1. Le env sono oggi secret-ref (`service-bus-connection-string`,
  `azure-storage-connection`): i secret vanno rimossi.

### 9.3 Servizi legacy di `import-sal` (orchestrator + `*-processor-prod`) — solo se presenti

Non fanno parte della pipeline in uso (§5), ma se esistono nel RG tengono chiavi vive e vanno
migrati. Lo script li tratta come gli altri, marcandoli "legacy" nel report.

- **Orchestrator** `import-sal-orchestrator` ← `src/import-sal` (root): `ServiceBusClient`
  identity in `import_sal_orchestrator.py` e `import_sal_utils.py`; rimozione del codice ACI
  morto (`create_container_group`, `delete_container`, `get_base_environment_variables`,
  dipendenza `azure-mgmt-containerinstance`); `_validate_configuration` aggiornata. Infra:
  system-assigned identity, ruoli Service Bus Data Owner + AcrPull (+ storage dati se il
  codice tocca blob, verificare); rimozione env/secret `SERVICE_BUS_CONNECTION_STRING`,
  `AZURE_STORAGE_CONNECTION_STRING`, `CONTAINER_REGISTRY_PASSWORD`,
  `CONTAINER_REGISTRY_USERNAME` e `*_SERVICE_IMAGE` (non letti dal sorgente).
- **Servizi figli** (Container App `*-processor-prod`, tabella §8): ogni servizio ha il suo
  `*_service.py` con `BlobServiceClient.from_connection_string` / `ServiceBusClient.from_connection_string`
  → helper identity; system-assigned identity per app; ruoli storage dati + Service Bus +
  AcrPull; rimozione env/secret key-based; `--registry-identity system`. `ocr_common.py`
  (Document Intelligence, OpenAI) e le chiavi AI restano com'erano (fuori mandato).
- Alternativa, a scelta dell'operatore nel piano (fase 2): per i legacy con `minReplicas=0`
  e nessuna revision attiva da > 30 giorni lo script propone **eliminazione** invece di
  migrazione (meno superficie, meno build). Default: migrazione; eliminazione solo con
  conferma esplicita per servizio.

### 9.4 `sftp-processor` ← `src/import-sal/services/sftp`

- Codice: `sftp_service.py` (Blob + Service Bus identity). Se spawna container (riga ~194
  passa env a qualcosa), stesso trattamento di 9.3.
- Infra: come 9.1.

### 9.5 `servicebus-bridge` ← `src/servicebus-bridge`

- Codice: `servicebus_bridge.py` → identity; `check_dlq.py` idem (tool manuale).
  Il working tree locale ha modifiche non committate (`servicebus_bridge.py`,
  `deploy-prod.ps1`): si parte dal working tree, non da HEAD.
- Infra: come 9.1 (solo Service Bus + AcrPull).

### 9.6 `polis-sal-import` (App Service) ← `src/polis-sal-sftp-api`

- Codice: le ~12 costruzioni di `connection_string = f"…AccountKey={AZURE_ACCOUNT_KEY}…"`
  sostituite da `get_blob_service_client()`; le ~8 `generate_blob_sas(account_key=…)`
  sostituite da SAS con **user delegation key**
  (`blob_service.get_user_delegation_key(start, expiry)` → `generate_blob_sas(user_delegation_key=…)`),
  scadenza massima 7 giorni (limite Azure). `ServiceBusClient` identity. `EmailClient` ACS
  resta su connection string (fuori mandato).
- Include i commit di luglio già su master.
- Infra: system-assigned identity; ruoli storage dati + Service Bus; settings
  `AZURE_ACCOUNT_KEY` e `SERVICE_BUS_CONNECTION_STRING` rimossi, aggiunte
  `AZURE_STORAGE_ACCOUNT_NAME` (= valore attuale di `AZURE_ACCOUNT_NAME`) e `SERVICE_BUS_NAMESPACE`.
- Deploy: zip deploy con build remota Oryx (`SCM_DO_BUILD_DURING_DEPLOYMENT=true`).

### 9.7 `polis-sftp-upload` (Function App) ← `src/polis-sal-sftp-sync`

- Codice: `config.py` non legge più `AzureWebJobsStorage` come connection string dati; nuova
  `DATA_STORAGE_ACCOUNT_NAME`; `function_app.py` usa `BlobServiceClient(account_url, credential)`
  e `ServiceBusClient` identity.
- Infra: system-assigned identity. Settings: `AzureWebJobsStorage` → `AzureWebJobsStorage__accountName=<host>`;
  `SERVICE_BUS_CONNECTION_STRING` rimossa → `SERVICE_BUS_NAMESPACE`. Ruoli: su storage host
  `Storage Blob Data Owner` + `Storage Queue Data Contributor`; su storage dati
  `Storage Blob Data Contributor`; Service Bus Data Owner.
- **Azure Files (decisione D3)**: content share (`WEBSITE_CONTENTAZUREFILECONNECTIONSTRING`)
  ed eventuale mount SMB `/data` restano key-based perché Azure non offre alternativa.
  In prod c'è **un solo storage account** (`polisprodstorage`) che fa da dati e da host:
  per renderlo keyless lo script **crea un nuovo storage account host** nel RG
  (`polisprodfunchost` + 4 caratteri casuali se il nome è occupato, `Standard_LRS`, `StorageV2`,
  TLS 1.2, public blob access off, costo trascurabile), vi crea la content share (nome =
  `WEBSITE_CONTENTSHARE` attuale) e, se esiste un mount `/data`, la share `function-data`,
  copiando i file con `az storage file` (contenuto di lavoro, in dubbio si parte vuota).
  Poi: `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` → nuovo account (key),
  `AzureWebJobsStorage__accountName` → nuovo account (identity), mount `/data` → nuovo
  account, `DATA_STORAGE_ACCOUNT_NAME` → `polisprodstorage`. Il nuovo account resta con shared
  key abilitata (unica eccezione, dichiarata nel report), chiavi ruotate a fine run e
  riapplicate. Lo switch del content share comporta un restart della Function App e la
  perdita del contenuto precedente della share (è ricreato dal deploy: il deploy zip avviene
  **dopo** lo switch, nella stessa fase).
- Deploy: zip deploy con build remota.

### 9.8 `polis-orchestrator-func`

- Nessun sorgente. Se in prod esiste: system-assigned identity (già presente nel RG Divitech),
  ruoli Service Bus Data Owner + storage host come 9.7, settings
  `ServiceBusConnection` → `ServiceBusConnection__fullyQualifiedNamespace`,
  `AzureWebJobsStorage` → `AzureWebJobsStorage__accountName`. Funziona solo se le funzioni
  usano binding dichiarativi con `connection="ServiceBusConnection"`; lo script lo verifica
  (`az functionapp function list` → bindings). Se le funzioni leggono la connection string
  nel codice, il servizio va in "non migrabile senza codice" e blocca l'hardening (§8).
  Se la Function App non ha funzioni, viene segnalata come inattiva e saltata.

### 9.9 `polis-api-database`, `polis-ai` (App Service) e `polis-frontend`

- Codice: `api/notifications.py` (3 punti) e `main.py` di polis-ai-2 (3 punti):
  `ServiceBusClient` identity, env `SERVICE_BUS_NAMESPACE`. `polis-api-database` ha già
  `azure-identity` nei requirements; polis-ai-2 no (aggiungere).
- `polis-frontend`: `front_end/views.py` importa `ServiceBusClient` ma **non lo usa**.
  Nessun deploy: lo script rimuove solo la setting `SERVICE_BUS_CONNECTION_STRING` (e l'import
  inutile viene tolto nel repo per coerenza, senza rilascio).
- Infra: system-assigned identity (api-database già ce l'ha), ruolo Service Bus Data Owner,
  rimozione `SERVICE_BUS_CONNECTION_STRING`.
- Deploy: zip deploy con build remota del commit che risulta già in prod (`prod/master`,
  vedi §5 e §15) **più** la sola patch Service Bus: così il rilascio non porta altro.

## 10. Fasi dello script

| # | Fase | Cosa fa | Idempotenza |
|---|---|---|---|
| 0 | Preflight | admin locale, PowerShell ≥ 5.1, internet, TLS 1.2 | — |
| 1 | Bootstrap | installa Azure CLI, `az login`, scelta sub/RG, check ruolo Owner/UAA, download+verifica zip repo | salta install se `az` già presente |
| 2 | Discovery + piano | §8; backup completo di settings/env/secret-name/mount/identity di ogni servizio in `work/backup/<servizio>.json`; stampa piano; conferma | sempre rieseguita |
| 3 | Identità | abilita system-assigned su ogni servizio mappato; crea `id-polis-import-sal` | skip se esiste |
| 4 | RBAC | assegna i ruoli §9; attende propagazione (poll `az role assignment list`, max 5 min) | skip se già assegnato |
| 5 | Build & deploy codice | `az acr build` per ogni immagine mappata; zip deploy per web/function app; `--registry-identity system` | tag immagine = timestamp run; ridistribuire è innocuo |
| 6 | Switch settings | per servizio: aggiunge nuove variabili, poi rimuove le vecchie key-based e i secret; per 9.7 anche share/mount | confronto stato desiderato vs attuale |
| 7 | Verifica | §11; stampa esito per servizio | — |
| 8 | Hardening | solo se 7 è tutto verde (o `-ForceHardening`) e dopo conferma: `allowSharedKeyAccess=false` su storage dati; `disableLocalAuth=true` su Service Bus; ACR `--admin-enabled false`; rotazione key1/key2 di tutti gli storage (host incluso, con riapplicazione a content share/mount) | controlla stato prima |
| 9 | Report | `polis-update-report-<ts>.md`: tabella servizi (stato prima/dopo, ruoli, immagine/tag, esito verifica), eccezioni (host storage key-based), non mappati, interventi del fallback con transcript, software rimosso | — |
| 10 | Cleanup | `az logout`, rimuove `~/.azure`, disinstalla Azure CLI e Claude Code, cancella `work/` (backup inclusi, contengono chiavi), ripristina PATH | — |

Ordine 5 → 6 è voluto: il nuovo codice accetta sia la nuova variabile sia la vecchia
connection string, quindi il deploy non rompe nulla; lo switch delle settings avviene a
codice già aggiornato. Il backup della fase 2 permette rollback manuale (`-Rollback`
riapplica i JSON salvati) finché la fase 8 non ha ruotato le chiavi; dopo la fase 8 il
rollback consiste nel riabilitare shared key / local auth e rigenerare settings con le
chiavi nuove (lo script lo supporta con `-Rollback -AfterHardening`).

## 11. Verifica (fase 7)

Per ogni servizio, criterio minimo di "verde":

- Container App: ultima revision `Running`/`Healthy`, replica attiva (per quelle con
  `minReplicas=0` lo script forza temporaneamente 1 replica e la riporta a 0), log degli
  ultimi 3 minuti senza `Unauthorized`, `AuthenticationFailed`, `CredentialUnavailableError`,
  `401`, `403`; per orchestrator `GET :8080/health` via `az containerapp exec` o log.
- Web App: HTTP 200 su `/` o `/health` (per endpoint in `mapping.json`), log stream 60 s
  senza gli stessi pattern di errore.
- Function App: stato `Running`, nessun errore nel log stream per un ciclo di timer (2 min).
- Prova funzionale leggera opzionale (`-SmokeTest`): invia un messaggio su una coda di test
  (`import-response`) con la nuova identità dell'operatore e verifica che venga consumato.

Un servizio rosso non blocca gli altri; blocca la fase 8.

## 12. Fallback con Claude Code

Attivazione: uno step fallisce dopo 1 retry automatico. Lo script allora:

1. Installa Claude Code se assente: `irm https://claude.ai/install.ps1 | iex`
   (installer nativo, nessun Node). Esporta `ANTHROPIC_API_KEY` dalla variabile del bootstrap.
2. Lancia, dalla cartella `tools/`, `claude -p "<prompt>" --dangerously-skip-permissions
   --output-format json --max-turns 40` con prompt che contiene: fase e step falliti, comando
   eseguito, stdout/stderr completi, RG/sub, path dei sorgenti e di `mapping.json`,
   il file di stato della run, e le regole: *risolvi e porta a termine lo step; non disabilitare
   chiavi; non cancellare risorse; non modificare servizi fuori perimetro; non toccare
   `polis-api-storage`; al termine scrivi in `work/fallback/<step>.result.json` esito e cosa hai
   cambiato*. `tools/fallback/CLAUDE.md` descrive struttura script, convenzioni, comandi `az`
   disponibili.
3. Rilegge il result, riesegue la **verifica** dello step (non si fida dell'esito dichiarato).
   Massimo 2 interventi per step; poi lo step è rosso e si continua con gli altri.
4. Tutto il transcript finisce nel report. Claude Code viene disinstallato in fase 10.

La key è una variabile in testa al bootstrap (`$AnthropicApiKey`). Se vuota, scaduta o
invalida (HTTP 401 su `/v1/models` al primo bisogno), lo script **chiede all'operatore** di
incollarne una (input mascherato, facoltativo, Invio per saltare); senza key il fallback viene
saltato con messaggio esplicito e lo step resta rosso. Così il bootstrap non va rigenerato
se la data della run slitta: basta comunicare una key nuova all'operatore.

## 13. Decisioni prese (dal progettista, contestabili)

- **D1** Repo pubblico, codice senza segreti, download zip a SHA fissato, hash verificato.
  Il bootstrap con la key viaggia fuori dal repo.
- **D2** Transizione "doppia lettura" (nuova variabile oppure vecchia connection string) nel
  codice, per poter deployare prima e switchare dopo, e per rollback via settings.
- **D3** Azure Files resta key-based su uno storage account **host** dedicato, **creato dallo
  script** perché in prod ne esiste uno solo. Lo storage dati diventa keyless. Alternativa
  scartata: riscrivere la function per usare `/tmp` (rischio spazio disco su zip grandi,
  modifica funzionale non richiesta). Altra alternativa scartata: lasciare `polisprodstorage`
  con shared key (la finding resterebbe aperta sullo storage principale).
- **D4** ACR `polisregistryprod` è dedicato a prod: pull via managed identity, admin user
  disabilitato in hardening. Nessun impatto su Divitech/collaudo.
- **D5** Ruolo `Azure Service Bus Data Owner` per tutti (non Sender/Receiver separati).
- **D6** Role assignment a scope risorsa, mai RG, tranne i due ruoli ACI dell'orchestrator.
- **D7** SAS in `polis-sal-sftp-api` via user delegation key, scadenza ≤ 7 giorni.
- **D8** Ordine: deploy codice → switch settings → verifica → hardening con doppia conferma.
- **D9** Hardening bloccato se esiste un servizio key-based non migrato; override solo con
  flag esplicito e tracciato.
- **D10** Per le Web App, base del deploy = commit che risulta già in prod (`prod/master`
  dei remote SCM locali) + sola patch identity, così nessun cambio funzionale non voluto.
  Per `polis-sal-sftp-api` e `import_fatture` la base include invece le modifiche di luglio,
  che sono l'oggetto del rilascio. Vedi §15 per la verifica di cosa è davvero in prod.
- **D11** Orchestrator: nessuna user-assigned identity né ruoli ACI (codice ACI morto nel
  sorgente). Se in prod gira la versione ACI, l'orchestrator non viene toccato e blocca
  l'hardening (§9.3 caso B).
- **D13** Discovery segnala e rimuove (dopo verifica che non servano) eventuali
  `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID` nelle env dei servizi:
  `DefaultAzureCredential` darebbe loro precedenza sulla managed identity, e un service
  principal con secret è a sua volta una "chiave". Se presenti, il report li elenca e il
  servizio richiede conferma operatore prima della rimozione.
- **D14** Storage dati, Service Bus e ACR vengono risolti per nome in tutta la subscription
  (possono stare fuori dal RG); i ruoli RBAC vengono assegnati sulla risorsa ovunque sia, e
  il check permessi della fase 1 copre anche quegli scope.
- **D15** Verifica prima dell'hardening = solo tecnica (§11). `-SmokeTest` facoltativo invia
  un messaggio su `import-response` e verifica il consumo; **nessun upload di zip SAL** in
  prod da parte dello script (scriverebbe sul DB di produzione). Il test funzionale completo,
  se voluto, lo decide il committente a parte.
- **D16** La key Anthropic è variabile del bootstrap con fallback a input dell'operatore
  (§12); la data della run non vincola la consegna del bootstrap.
- **D17** Per i servizi `import-sal` legacy: migrazione di default, eliminazione proposta ma
  mai automatica (§9.3).
- **D12** Al cleanup si cancella anche il backup settings (contiene chiavi); chi vuole
  conservarlo passa `-KeepBackup` e il report avvisa che il file contiene segreti.

## 14. Lasciato al builder

- Forma esatta di `azure_clients.py` (sync/aio), gestione cache credenziali, timeout.
- Comandi `az` precisi, parsing output, attesa propagazione RBAC, gestione rate limit.
- Dettaglio dei pattern di errore nei log e durata delle finestre di osservazione.
- Struttura di `mapping.json` e del file di stato della run.
- Layout del report e del log.
- Procedura di scrub del repo prima del push: esclusione di `.env*`, `.prodenv`, `.colenv`,
  `.envprod`, `.envcol`, `local.settings.json`, `.idea/`, `.claude/settings.local.json`,
  `logs/`, `*.zip`, `db_pass.sh`, `set_api_settings.sh`, `set_web_settings.sh`,
  `file_fallito.ps1`, gli script `deploy-*.ps1` originali e ogni file con `AccountKey=`,
  `SharedAccessKey=`, `accesskey=`, password in chiaro; scansione regex pre-push e verifica
  che la key Anthropic non compaia. Il push è vietato finché la scansione non è pulita.
- Test dello script: dry-run completo contro il RG Divitech `polis-rg` (ambiente speculare)
  prima della consegna, con `-WhatIf` e poi reale su collaudo se autorizzato.

## 15. Domande aperte (solo il committente può rispondere)

Risolte il 2026-10-30:

- Pipeline in uso: function unzip + `polis-prod-import-app` + `autogen-prod-pdf-import`; il
  resto di `import-sal` è legacy, da migrare solo se presente (§9.3).
- `polis-api-storage`: codice del cliente, rilascio via `git push prod` dal PC dello
  sviluppatore quando arriva su `origin` (§3). Lo script non lo tocca ma lo controlla prima
  dell'hardening (D9).
- Base del rilascio Web App: `prod/master` + patch identity (D10) rende irrilevante sapere se
  i deploy di luglio riuscirono: il risultato è lo stesso.
- Smoke test e key Anthropic: D15, D16.

- **Rilascio codice Web App dal PC dello sviluppatore** (autorizzato dal committente il
  2026-10-30, **D18**): il codice migrato di `polis-sal-sftp-api`, `polis-api-database`,
  `polis-ai-2` viene committato su `origin` e pushato sui remote SCM `prod` dal PC dello
  sviluppatore **prima** della run dell'operatore. Grazie alla doppia lettura (D2) i servizi
  continuano a funzionare con le connection string finché lo script non switcha le settings.
  Lo script dell'operatore quindi **non fa zip deploy delle Web App**: per esse fa solo
  identity, ruoli, settings e verifica; controlla però che il deploy SCM sia avvenuto
  (commit atteso presente in `https://<app>.scm.azurewebsites.net/api/deployments`, o in
  alternativa che l'app esponga la versione) e, se manca, lo segnala e salta lo switch
  settings di quell'app. §9.6 e §9.9 vanno letti con questa modifica. La Function App
  (`polis-sal-sftp-sync`, nessun remote SCM locale) e le container app restano a carico
  dello script.

Stato: **approvato dal committente il 2026-10-30**. Nessuna domanda aperta.
