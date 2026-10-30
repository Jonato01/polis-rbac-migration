<#
.SYNOPSIS
Migrazione dei servizi Polis da Access Key a managed identity (Entra ID + RBAC).

.DESCRIPTION
Esegue le fasi 00-09 (preflight, bootstrap-check, discovery+piano, identity, RBAC, build/deploy,
switch settings, verifica, hardening, report). Idempotente: rilanciabile dopo un'interruzione.
Exit code: 0 tutto verde, 2 completato con step falliti, 3 fermato (ruolo mancante / conferma negata).

.PARAMETER Rg
Resource group di produzione (es. rg-polis).
.PARAMETER WorkDir
Cartella di lavoro (stato, backup, log, zip). Viene rimossa dal cleanup.
.PARAMETER WhatIf
Nessun comando az mutante: solo discovery, piano e report.
.PARAMETER ForceHardening
Esegue l'hardening anche con servizi key-based non migrati (tracciato nel report).
.PARAMETER KeepBackup
Conserva work/backup (contiene chiavi) al cleanup.
.PARAMETER SmokeTest
Invia un messaggio di test su import-response dopo la verifica.
.PARAMETER AnthropicApiKey
Key per il fallback Claude Code (se vuota/scaduta viene chiesta all'operatore).
.PARAMETER Rollback
Riapplica le settings salvate in work/backup e termina.
.PARAMETER AfterHardening
Con -Rollback: riabilita anche shared key / local auth / admin ACR.
.PARAMETER NonInteractive
Nessuna domanda: conferme implicite (le credenziali service principal NON vengono rimosse).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Rg,
    [string]$WorkDir = (Join-Path $env:LOCALAPPDATA 'PolisUpdate\work'),
    [switch]$WhatIf,
    [switch]$ForceHardening,
    [switch]$KeepBackup,
    [switch]$SmokeTest,
    [string]$AnthropicApiKey = '',
    [switch]$Rollback,
    [switch]$AfterHardening,
    [switch]$NonInteractive,
    [string]$ReportDir = [Environment]::GetFolderPath('Desktop')
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$toolsDir = $PSScriptRoot
foreach ($lib in 'Az', 'Mapping', 'Settings', 'State', 'Discovery', 'Rbac', 'Deploy', 'Verify', 'Hardening', 'Report', 'Fallback', 'Cleanup', 'Orchestration') {
    . (Join-Path $toolsDir "lib\$lib.ps1")
}
foreach ($ph in (Get-ChildItem (Join-Path $toolsDir 'phases') -Filter '*.ps1' | Sort-Object Name)) { . $ph.FullName }

$options = @{
    Rg = $Rg; WorkDir = $WorkDir; WhatIf = [bool]$WhatIf; ForceHardening = [bool]$ForceHardening; KeepBackup = [bool]$KeepBackup
    SmokeTest = [bool]$SmokeTest; AnthropicApiKey = $AnthropicApiKey; Rollback = [bool]$Rollback; AfterHardening = [bool]$AfterHardening
    NonInteractive = [bool]$NonInteractive; ReportDir = $ReportDir; ToolsDir = $toolsDir; RepoRoot = (Resolve-Path (Join-Path $toolsDir '..')).Path
}
$code = Invoke-PolisMigrationCore -Options $options
exit $code
