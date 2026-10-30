# Contesto per il fallback (Claude Code)

Stai lavorando dentro `tools/` dello script di migrazione Polis. L'operatore è loggato con
`az` (Owner sul resource group di produzione). Tu intervieni solo su uno step fallito.

## Struttura

| Path | Ruolo |
|---|---|
| `Invoke-PolisMigration.ps1` | orchestratore a fasi, parametri `-Rg -WorkDir [-WhatIf] [-ForceHardening] ...` |
| `phases/NN-*.ps1` | una fase per file, funzione `Invoke-PhaseNN -Context $ctx` |
| `lib/Az.ps1` | `Invoke-Az @('containerapp','show',...)` unico punto di chiamata ad az; `Set-AzWhatIf` |
| `lib/Mapping.ps1` | `Get-NormalizedImageName`, `Resolve-ServiceMapping` |
| `lib/Settings.ps1` | `Get-ConnectionStringParts`, `ConvertTo-IdentitySettings`, `Get-KeyBasedSettings` |
| `lib/Discovery.ps1` | `Get-PolisInventory -Rg -Mapping`, `Test-OperatorRole` |
| `lib/Rbac.ps1` | `Ensure-SystemIdentity`, `Get-RequiredRoles`, `Ensure-RoleAssignment`, `Wait-RoleAssignment` |
| `lib/Deploy.ps1` | `Build-ContainerImage` (az acr build), `Update-ContainerAppImage`, `Set-ContainerAppEnv`, `Publish-FunctionZip`, `Set-WebAppSettings`, `Ensure-HostStorageAccount`, `Move-FunctionShares` |
| `lib/Verify.ps1` | `Test-ContainerAppHealthy`, `Test-WebAppHealthy`, `Test-FunctionAppHealthy` |
| `lib/Hardening.ps1` | gate e hardening: **non invocare** |
| `mapping.json` | servizi -> sorgenti (`../src/...`) -> variabili da aggiungere/rimuovere |
| `../src/_shared/azure_clients.py` | helper Python identity-first copiato in ogni build context |

`$ctx` (hashtable): `Rg`, `Sub`, `WorkDir`, `Inventory`, `State`, `Options`, `ToolsDir`, `RepoRoot`.
Stato della run: `work/state.json` (fase|servizio -> status). Backup settings: `work/backup/*.json`.

## Convenzioni

- Windows PowerShell 5.1: niente `&&`, `??`, `?:`. `Set-StrictMode -Version Latest` ovunque.
- Le nuove variabili dei servizi: `AZURE_STORAGE_ACCOUNT_NAME`, `SERVICE_BUS_NAMESPACE`
  (`AZURE_SERVICEBUS_NAMESPACE` in import_fatture), function app: `AzureWebJobsStorage__accountName`,
  `DATA_STORAGE_ACCOUNT_NAME`. Il codice accetta ancora le vecchie connection string come fallback.
- Build immagini: `az acr build --registry <acr> --image <repo>:<tag> --file <dockerfile> <context>`.
- Per rilanciare un solo step, dot-source le lib e chiama la funzione corrispondente con `$ctx`.

## Vietato

- Disabilitare chiavi / local auth / admin ACR, ruotare chiavi (è la fase 08, decide l'operatore).
- Cancellare risorse. Toccare `polis-api-storage`, `polis-sal-import-web`, servizi non mappati.
- Stampare valori di chiavi, connection string, password.
