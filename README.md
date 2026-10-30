# polis-rbac-migration

Migrazione dei servizi Polis da Access Key / connection string a **Microsoft Entra ID +
RBAC (managed identity)** e rilascio delle modifiche applicative di luglio 2026.

Il repository è **pubblico** e contiene solo codice e script: nessun segreto, nessun file
`.env`, nessuna chiave. Il bootstrap che l'operatore riceve (`Run-PolisUpdate.ps1`) è
generato localmente da `bootstrap/New-Bootstrap.ps1` e **non** è versionato.

## Struttura

| Cartella | Contenuto |
|---|---|
| `src/_shared/azure_clients.py` | helper Python: client Blob / Service Bus con identity, fallback a connection string |
| `src/<servizio>/` | sorgenti dei servizi container/function migrati (copiati nei build context) |
| `tools/` | script PowerShell 5.1 a fasi: discovery, identity, RBAC, build (`az acr build`), settings, verifica, hardening, report, cleanup |
| `tools/mapping.json` | mappa servizi Azure → sorgente → variabili da aggiungere/rimuovere |
| `bootstrap/` | template del launcher per il PC dell'operatore e generatore |
| `tests/` | pytest (`tests/python`) e Pester 5 (`tests/ps`) |
| `docs/superpowers/` | design e piano di implementazione |

## Test

```powershell
.\.venv\Scripts\pytest tests\python -q
Invoke-Pester -Path tests\ps -Output Detailed
```

Setup locale: `py -3.12 -m venv .venv; .\.venv\Scripts\pip install -r requirements-dev.txt;
Install-Module Pester -Scope CurrentUser -Force -MinimumVersion 5.5`.

## Operatore

Le istruzioni per chi esegue l'aggiornamento sul resource group di produzione sono nella
sezione finale di questo file (aggiunte alla consegna).
