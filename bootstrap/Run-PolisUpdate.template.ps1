<#
.SYNOPSIS
Polis - aggiornamento identity/RBAC: launcher per il PC dell'operatore.

.DESCRIPTION
File generato da bootstrap/New-Bootstrap.ps1 (contiene URL del repo a commit fissato, hash dello zip e
API key Anthropic per il fallback). Da eseguire con tasto destro -> "Esegui con PowerShell" oppure:
    powershell -ExecutionPolicy Bypass -File .\Run-PolisUpdate.ps1
Cosa fa: si eleva ad amministratore, installa Azure CLI, chiede il login Azure, scarica e verifica
il pacchetto, esegue la migrazione, scrive il report sul Desktop e infine disinstalla tutto.

.PARAMETER Rg
Resource group di produzione (se omesso viene scelto automaticamente tra quelli con "polis" nel nome).
.PARAMETER WhatIf
Solo discovery, piano e report: nessuna modifica.
.PARAMETER DeviceCode
Login Azure con device code (PC senza browser).
.PARAMETER KeepAzureCli
Non disinstallare Azure CLI al termine.
#>
[CmdletBinding()]
param(
    [string]$Rg = '',
    [switch]$WhatIf,
    [switch]$ForceHardening,
    [switch]$KeepBackup,
    [switch]$SmokeTest,
    [switch]$DeviceCode,
    [switch]$KeepAzureCli,
    [switch]$NonInteractive,
    [switch]$Elevated
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---- valori fissati dal generatore ------------------------------------------------------------
$RepoZipUrl      = '__REPO_ZIP_URL__'
$ZipSha256       = '__ZIP_SHA256__'
$AnthropicApiKey = '__ANTHROPIC_KEY__'
$Commit          = '__COMMIT__'
# -----------------------------------------------------------------------------------------------

function Write-Info([string]$m) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $m" -ForegroundColor Cyan }
function Write-Warn([string]$m) { Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $m" -ForegroundColor Yellow }

# ---- elevazione ---------------------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warn 'Servono i diritti di amministratore locale (installazione Azure CLI): rilancio elevato...'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Elevated')
    foreach ($k in $PSBoundParameters.Keys) {
        if ($k -eq 'Elevated') { continue }
        $v = $PSBoundParameters[$k]
        if ($v -is [switch]) { if ($v) { $argList += "-$k" } } else { $argList += @("-$k", "`"$v`"") }
    }
    Start-Process powershell.exe -ArgumentList $argList -Verb RunAs -Wait
    exit
}

[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$base = Join-Path $env:LOCALAPPDATA 'PolisUpdate'
$work = Join-Path $base 'work'
$srcDir = Join-Path $base $Commit
New-Item -ItemType Directory -Path $base, $work -Force | Out-Null
$transcript = Join-Path ([Environment]::GetFolderPath('Desktop')) "polis-update-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
Start-Transcript -Path $transcript | Out-Null

$exitCode = 1
$reportPath = $null
try {
    # ---- Azure CLI ------------------------------------------------------------------------------
    function Refresh-Path { $env:PATH = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User') }
    if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
        Write-Info 'Installo Azure CLI (MSI silente)...'
        $msi = Join-Path $base 'AzureCLI.msi'
        try {
            Invoke-WebRequest -Uri 'https://aka.ms/installazurecliwindowsx64' -OutFile $msi -UseBasicParsing
            $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart" -Wait -PassThru
            if ($p.ExitCode -ne 0) { throw "msiexec exit $($p.ExitCode)" }
        } catch {
            Write-Warn "MSI fallito ($($_.Exception.Message)), provo con winget..."
            Start-Process winget -ArgumentList 'install --id Microsoft.AzureCLI -e --silent --accept-source-agreements --accept-package-agreements' -Wait | Out-Null
        }
        Refresh-Path
        if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw 'Azure CLI non disponibile dopo l''installazione' }
    }
    Write-Info "Azure CLI: $((az version --only-show-errors -o tsv --query '\"azure-cli\"' 2>$null))"

    # ---- login ----------------------------------------------------------------------------------
    $acct = $null
    try { $acct = az account show --only-show-errors -o json 2>$null | ConvertFrom-Json } catch { $acct = $null }
    if (-not $acct) {
        Write-Info 'Login Azure: segui le istruzioni (browser o device code)...'
        if ($DeviceCode) { az login --use-device-code --only-show-errors -o none } else { az login --only-show-errors -o none }
        $acct = az account show --only-show-errors -o json | ConvertFrom-Json
    }
    Write-Info "Account: $($acct.user.name) - subscription: $($acct.name)"

    # ---- pacchetto ------------------------------------------------------------------------------
    $zip = Join-Path $base 'repo.zip'
    if (-not (Test-Path (Join-Path $srcDir 'tools\Invoke-PolisMigration.ps1'))) {
        Write-Info "Scarico il pacchetto ($Commit)..."
        Invoke-WebRequest -Uri $RepoZipUrl -OutFile $zip -UseBasicParsing
        $hash = (Get-FileHash -Path $zip -Algorithm SHA256).Hash
        if ($hash -ne $ZipSha256) { throw "Hash dello zip non corrisponde (atteso $ZipSha256, trovato $hash): pacchetto non affidabile" }
        $extract = Join-Path $base 'extract'
        if (Test-Path $extract) { Remove-Item $extract -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $extract -Force
        $inner = Get-ChildItem $extract -Directory | Select-Object -First 1
        if (Test-Path $srcDir) { Remove-Item $srcDir -Recurse -Force }
        Move-Item $inner.FullName $srcDir
        Remove-Item $extract -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Info "Pacchetto verificato in $srcDir"

    # ---- scelta resource group ------------------------------------------------------------------
    if (-not $Rg) {
        $groups = @(az group list --only-show-errors -o json | ConvertFrom-Json | Where-Object { $_.name -match 'polis' } | ForEach-Object { $_.name })
        if ($groups.Count -eq 1) { $Rg = $groups[0] }
        elseif ($groups.Count -eq 0) { throw 'Nessun resource group con "polis" nel nome: specificare -Rg' }
        elseif ($NonInteractive) { throw "Piu' resource group candidati ($($groups -join ', ')): specificare -Rg" }
        else {
            Write-Host 'Resource group candidati:'
            for ($i = 0; $i -lt $groups.Count; $i++) { Write-Host "  [$($i + 1)] $($groups[$i])" }
            $sel = Read-Host 'Scegli il numero'
            $Rg = $groups[[int]$sel - 1]
        }
    }
    Write-Info "Resource group: $Rg"

    # ---- migrazione -----------------------------------------------------------------------------
    $migArgs = @{ Rg = $Rg; WorkDir = $work; AnthropicApiKey = $AnthropicApiKey }
    if ($WhatIf) { $migArgs.WhatIf = $true }
    if ($ForceHardening) { $migArgs.ForceHardening = $true }
    if ($KeepBackup) { $migArgs.KeepBackup = $true }
    if ($SmokeTest) { $migArgs.SmokeTest = $true }
    if ($NonInteractive) { $migArgs.NonInteractive = $true }
    & (Join-Path $srcDir 'tools\Invoke-PolisMigration.ps1') @migArgs
    $exitCode = $LASTEXITCODE
    $rp = Join-Path $work 'report-path.txt'
    if (Test-Path $rp) { $reportPath = (Get-Content $rp -Raw).Trim() }
} catch {
    Write-Host "ERRORE: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
} finally {
    # ---- cleanup: sempre, anche su errore -------------------------------------------------------
    Write-Info 'Pulizia del PC (profilo Azure, Azure CLI, Claude Code, file di lavoro)...'
    try {
        . (Join-Path $srcDir 'tools\lib\Cleanup.ps1')
        . (Join-Path $srcDir 'tools\phases\10-Cleanup.ps1')
        $summary = Invoke-Phase10 -WorkDir $work -KeepBackup ([bool]$KeepBackup) -ReportPath $reportPath -KeepAzureCli ([bool]$KeepAzureCli)
        foreach ($s in $summary) { Write-Host "  - $s" }
    } catch {
        Write-Warn "Cleanup parziale: $($_.Exception.Message)"
        try { Remove-Item (Join-Path $env:USERPROFILE '.azure') -Recurse -Force -ErrorAction SilentlyContinue } catch { }
    }
    Remove-Item $srcDir -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $base 'repo.zip'), (Join-Path $base 'AzureCLI.msi') -Force -ErrorAction SilentlyContinue
    if ($KeepBackup) { Write-Warn "Backup settings conservato in $work\backup (contiene chiavi: cancellarlo dopo l'uso)" } else { Remove-Item $base -Recurse -Force -ErrorAction SilentlyContinue }
    Stop-Transcript | Out-Null
}

Write-Host ''
if ($reportPath) { Write-Host "Report: $reportPath" -ForegroundColor Green }
Write-Host "Log completo: $transcript"
switch ($exitCode) {
    0 { Write-Host 'Completato: tutti i servizi verificati.' -ForegroundColor Green }
    2 { Write-Host 'Completato con errori su alcuni servizi: vedi report.' -ForegroundColor Yellow }
    3 { Write-Host 'Interrotto (ruolo mancante o conferma negata): nessuna modifica distruttiva eseguita.' -ForegroundColor Yellow }
    default { Write-Host 'Terminato con errore: vedi log.' -ForegroundColor Red }
}
if (-not $NonInteractive) { Read-Host 'Premi Invio per chiudere' | Out-Null }
exit $exitCode
