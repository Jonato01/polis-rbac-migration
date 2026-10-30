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

## Operatore: come eseguire l'aggiornamento

Serve un PC Windows 10/11 con accesso a internet e un account Azure con ruolo **Owner**
(o User Access Administrator) sul resource group di produzione. Non serve installare nulla.

1. Salvare sul Desktop il file `Run-PolisUpdate.ps1` ricevuto via canale privato (contiene
   la chiave per il supporto automatico; non inoltrarlo e cancellarlo a fine lavoro).
2. Tasto destro sul file -> **Esegui con PowerShell**. In alternativa, da PowerShell:
   `powershell -ExecutionPolicy Bypass -File .\Run-PolisUpdate.ps1`
3. Accettare la richiesta di diritti di amministratore (serve per installare Azure CLI).
4. Fare il login Azure quando si apre il browser (o usare `-DeviceCode` se il browser non c'e').
5. Leggere il **piano** stampato a video e confermare con `S`.
6. Attendere: identity, ruoli, build in cloud, deploy e verifica (15-40 minuti).
7. Alla domanda di **hardening** (disabilitazione chiavi) confermare con `S` solo se tutte le
   verifiche sono verdi; con `N` le chiavi restano attive e lo script lo scrive nel report.
8. Al termine sul Desktop ci sono `polis-update-report-<data>.md` e il log. Azure CLI, Claude Code,
   i token Azure e i file di lavoro vengono rimossi automaticamente.

Opzioni utili: `-WhatIf` (solo piano, nessuna modifica), `-Rg <nome>` (se ci sono piu' resource
group), `-KeepAzureCli`, `-KeepBackup` (conserva i backup delle settings: contengono chiavi).

Se qualcosa si interrompe, rilanciare lo stesso file: riprende da dove era arrivato.
In caso di problemi gravi: `tools\Invoke-PolisMigration.ps1 -Rg <rg> -WorkDir <work> -Rollback`
ripristina le settings salvate (prima dell'hardening).
