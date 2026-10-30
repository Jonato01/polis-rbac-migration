Set-StrictMode -Version Latest

<#
Orchestrazione a fasi. Dipende da tutte le lib e dai file tools/phases/NN-*.ps1
(ognuno definisce Invoke-PhaseNN -Context $ctx e MUTA $ctx senza restituire nulla).
#>

$script:StopPrefix = '[STOP]'

function Stop-Migration {
    param([string]$Message)
    throw "$($script:StopPrefix) $Message"
}

function Read-Confirmation {
    <# Chiede S/N all'operatore. In NonInteractive risponde sempre si'. Mockabile. #>
    param([Parameter(Mandatory)][string]$Message, [bool]$NonInteractive = $false)
    if ($NonInteractive) { return $true }
    while ($true) {
        $a = Read-Host "$Message [S/N]"
        if ($a -match '^[sSyY]') { return $true }
        if ($a -match '^[nN]') { return $false }
    }
}

function Test-PreflightEnvironment {
    <# Ritorna la lista dei problemi bloccanti (vuota = ok). Mockabile. #>
    $problems = @()
    if ($PSVersionTable.PSVersion.Major -lt 5) { $problems += "PowerShell $($PSVersionTable.PSVersion) troppo vecchio (serve 5.1)" }
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { $problems += 'Impossibile abilitare TLS 1.2' }
    try { Invoke-WebRequest -Uri 'https://management.azure.com/' -UseBasicParsing -TimeoutSec 20 -ErrorAction Stop | Out-Null } catch {
        if (-not ($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response)) { $problems += "Nessuna connettivita' verso management.azure.com: $($_.Exception.Message)" }
    }
    return [string[]]@($problems)
}

function Write-Step {
    param([string]$Message, [string]$Color = 'Cyan')
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] $Message" -ForegroundColor $Color
}

function New-MigrationContext {
    param([Parameter(Mandatory)][hashtable]$Options)
    if (-not (Test-Path $Options.WorkDir)) { New-Item -ItemType Directory -Path $Options.WorkDir -Force | Out-Null }
    $toolsDir = if ($Options.ContainsKey('ToolsDir') -and $Options.ToolsDir) { $Options.ToolsDir } else { (Resolve-Path (Join-Path $PSScriptRoot '..')).Path }
    $repoRoot = if ($Options.ContainsKey('RepoRoot') -and $Options.RepoRoot) { $Options.RepoRoot } else { (Resolve-Path (Join-Path $toolsDir '..')).Path }
    return @{
        Options = $Options
        Rg = $Options.Rg
        Sub = $null
        RgId = $null
        Location = $null
        WorkDir = $Options.WorkDir
        ToolsDir = $toolsDir
        RepoRoot = $repoRoot
        Mapping = (Get-Content (Join-Path $toolsDir 'mapping.json') -Raw -Encoding UTF8 | ConvertFrom-Json)
        State = (New-RunState -WorkDir $Options.WorkDir)
        Inventory = $null
        Principals = @{}
        HostStorageAccount = $null
        HostStorageId = $null
        FallbackLog = [System.Collections.ArrayList]::new()
        HardeningReasons = @()
        ReportPath = $null
    }
}

function Get-TargetServices {
    <# Servizi mappati e non esclusi. #>
    param([Parameter(Mandatory)][hashtable]$Context)
    return @($Context.Inventory.Services | Where-Object { $_.Mapping -and -not $_.Excluded })
}

function Test-MappingFlag {
    param($Mapping, [string]$Name)
    return [bool]($Mapping.PSObject.Properties[$Name] -and $Mapping.$Name)
}

function Get-ResolvedForService {
    <# Valori per i placeholder {storage} {servicebus} {hostStorage} del servizio. #>
    param([Parameter(Mandatory)][hashtable]$Context, [Parameter(Mandatory)][pscustomobject]$Service)
    $storage = $null
    foreach ($k in $Service.Settings.Keys) {
        if ($k -eq 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING') { continue }
        $p = Get-ConnectionStringParts ([string]$Service.Settings[$k])
        if ($p.Kind -eq 'storage') { $storage = $p.AccountName; break }
    }
    if (-not $storage -and $Service.Settings.ContainsKey('AZURE_ACCOUNT_NAME')) { $storage = $Service.Settings['AZURE_ACCOUNT_NAME'] }
    if (-not $storage) {
        $data = @($Context.Inventory.StorageAccounts | Where-Object { $_.Role -eq 'data' })
        if ($data.Count -gt 0) { $storage = $data[0].Name }
    }
    $ns = if ($Context.Inventory.ServiceBus) { $Context.Inventory.ServiceBus.Namespace } else { $null }
    return @{ StorageAccount = $storage; ServiceBusNamespace = $ns; HostStorageAccount = $Context.HostStorageAccount }
}

function Invoke-StepWithFallback {
    <#
    .SYNOPSIS
    Esegue uno step idempotente: 1 retry, poi fino a 2 interventi del fallback, ognuno seguito da Verify.
    Ritorna $true se lo step e' done. Lo stato viene salvato subito.
    #>
    param(
        [Parameter(Mandatory)][hashtable]$Context,
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][scriptblock]$Action,
        [scriptblock]$Verify = { $true },
        [string]$Detail = ''
    )
    $state = $Context.State
    if ((Get-StepStatus -State $state -Phase $Phase -Service $Service) -eq 'done') {
        Write-Step "  [$Phase] $Service gia' completato, salto" 'DarkGray'
        return $true
    }
    $lastError = $null
    $succeeded = $false
    for ($attempt = 1; $attempt -le 2 -and -not $succeeded; $attempt++) {
        try {
            & $Action | Out-Null
            if (& $Verify) { $succeeded = $true } else { $lastError = 'verifica dello step fallita' }
        } catch {
            $lastError = $_.Exception.Message
            Write-Step "  [$Phase] $Service tentativo $attempt fallito: $lastError" 'Yellow'
        }
    }
    $fb = 0
    while (-not $succeeded -and $fb -lt 2) {
        $fb++
        Write-Step "  [$Phase] $Service -> fallback Claude Code (intervento $fb/2)" 'Magenta'
        $r = Invoke-ClaudeFallback -Context $Context -Phase $Phase -Service $Service -ErrorText ([string]$lastError) -Attempt $fb
        [void]$Context.FallbackLog.Add([pscustomobject]@{ Phase = $Phase; Service = $Service; Attempt = $fb; Ok = $r.Ok; Transcript = ("$($r.Summary)`n$($r.Transcript)") })
        if ($r.Skipped) { break }
        if (-not $r.Ok) { $lastError = "fallback $fb non ha risolto: $($r.Summary)"; continue }
        try {
            if (& $Verify) { $succeeded = $true } else { $lastError = "dopo fallback $fb : verifica ancora fallita ($($r.Summary))" }
        } catch { $lastError = $_.Exception.Message }
    }
    if ($succeeded) {
        Set-StepStatus -State $state -Phase $Phase -Service $Service -Status 'done' -Detail $Detail
    } else {
        Set-StepStatus -State $state -Phase $Phase -Service $Service -Status 'failed' -Detail ([string]$lastError)
        Write-Step "  [$Phase] $Service FALLITO: $lastError" 'Red'
    }
    Save-RunState -State $state
    return $succeeded
}

function Invoke-Rollback {
    param([Parameter(Mandatory)][hashtable]$Context)
    $dir = Join-Path $Context.WorkDir 'backup'
    if (-not (Test-Path $dir)) { Write-Step 'Nessun backup da ripristinare' 'Yellow'; return }
    foreach ($f in (Get-ChildItem $dir -Filter '*.json')) {
        Write-Step "Ripristino $($f.BaseName)"
        try { Restore-ServiceBackup -Path $f.FullName -Rg $Context.Rg } catch { Write-Step "  errore: $($_.Exception.Message)" 'Red' }
    }
    if ($Context.Options.AfterHardening) {
        Write-Step 'Riabilito shared key / local auth / admin ACR (rollback post-hardening)' 'Yellow'
        $inv = Get-PolisInventory -Rg $Context.Rg -Mapping $Context.Mapping
        foreach ($s in @($inv.StorageAccounts)) { Invoke-Az @('storage', 'account', 'update', '-n', $s.Name, '--allow-shared-key-access', 'true') -AllowFailure | Out-Null }
        if ($inv.ServiceBus) { Invoke-Az @('servicebus', 'namespace', 'update', '-g', $Context.Rg, '-n', $inv.ServiceBus.Name, '--disable-local-auth', 'false') -AllowFailure | Out-Null }
        if ($inv.Acr) { Invoke-Az @('acr', 'update', '-n', $inv.Acr.Name, '--admin-enabled', 'true') -AllowFailure | Out-Null }
        Write-Step 'NB: le chiavi sono state ruotate dall''hardening: le settings ripristinate dal backup contengono chiavi vecchie e vanno rigenerate a mano.' 'Red'
    }
}

function Invoke-PolisMigrationCore {
    <#
    .SYNOPSIS
    Esegue le fasi 00-09. Exit code: 0 tutto verde, 2 completato con step falliti, 3 fermato.
    #>
    param([Parameter(Mandatory)][hashtable]$Options)
    $ctx = New-MigrationContext -Options $Options
    Set-AzWhatIf ([bool]$Options.WhatIf)
    Set-AzLogPath (Join-Path $ctx.WorkDir 'az.log')
    if ($Options.ContainsKey('Rollback') -and $Options.Rollback) { Invoke-Rollback -Context $ctx; return 0 }

    $phases = @('00', '01', '02', '03', '04', '05', '06', '07', '08', '09')
    try {
        foreach ($p in $phases) {
            $fn = "Invoke-Phase$p"
            Write-Step "=== Fase $p ===" 'Green'
            & $fn -Context $ctx | Out-Null
            Save-RunState -State $ctx.State
        }
    } catch {
        $msg = $_.Exception.Message
        if ($msg.StartsWith($script:StopPrefix)) {
            Write-Step $msg 'Red'
            Save-RunState -State $ctx.State
            return 3
        }
        throw
    }
    $failed = 0
    foreach ($k in $ctx.State.Steps.Keys) { if ($ctx.State.Steps[$k].Status -eq 'failed') { $failed++ } }
    if ($failed -gt 0) { return 2 }
    return 0
}
