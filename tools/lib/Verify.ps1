Set-StrictMode -Version Latest

<#
Verifica post-switch dei servizi. Dipende da Az.ps1 (Invoke-Az).
Criterio: stato Running/Healthy + nessun pattern di errore di autenticazione nei log recenti.
#>

function Test-LogClean {
    param([string]$LogText, [string[]]$ErrorPatterns)
    if ([string]::IsNullOrEmpty($LogText)) { return $true }
    foreach ($p in @($ErrorPatterns)) {
        if ($p -and $LogText.IndexOf($p, [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $false }
    }
    return $true
}

function Get-LogErrorLines {
    param([string]$LogText, [string[]]$ErrorPatterns)
    $lines = @()
    foreach ($l in ($LogText -split "`r?`n")) {
        foreach ($p in @($ErrorPatterns)) {
            if ($p -and $l.IndexOf($p, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $lines += $l.Trim(); break }
        }
    }
    return $lines
}

function Invoke-HttpProbe {
    <# Separata per essere mockabile. Ritorna lo status code HTTP (0 se irraggiungibile). #>
    param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSec = 30)
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec -ErrorAction Stop
        return [int]$resp.StatusCode
    } catch {
        if ($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    }
}

function Test-ContainerAppHealthy {
    <#
    .SYNOPSIS
    Se minReplicas=0 forza temporaneamente 1 replica; osserva stato revision e log console/system.
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][pscustomobject]$Service,
        [string[]]$ErrorPatterns = @(),
        [int]$ObserveSec = 180
    )
    $name = $Service.Name
    $scaled = $false
    $detail = @()
    try {
        if ($Service.PSObject.Properties['MinReplicas'] -and [int]$Service.MinReplicas -eq 0) {
            Invoke-Az @('containerapp', 'update', '-g', $Rg, '-n', $name, '--min-replicas', '1') | Out-Null
            $scaled = $true
        }
        if ($ObserveSec -gt 0) { Start-Sleep -Seconds $ObserveSec }

        $app = Invoke-Az @('containerapp', 'show', '-g', $Rg, '-n', $name)
        $status = if ($app -and $app.properties.PSObject.Properties['runningStatus']) { [string]$app.properties.runningStatus } else { 'unknown' }
        $latest = if ($app -and $app.properties.PSObject.Properties['latestRevisionName']) { [string]$app.properties.latestRevisionName } else { $null }
        $ok = $status -eq 'Running'
        if (-not $ok) { $detail += "runningStatus=$status" }

        $revs = Invoke-Az @('containerapp', 'revision', 'list', '-g', $Rg, '-n', $name) -AllowFailure
        $rev = $null
        foreach ($rv in @($revs)) { if ($null -ne $rv -and ($null -eq $latest -or $rv.name -eq $latest)) { $rev = $rv } }
        if ($rev) {
            $health = [string]$rev.properties.healthState
            $running = [string]$rev.properties.runningState
            if ($health -ne 'Healthy' -or $running -notin @('Running', 'RunningAtMaxScale', 'Activating', 'Processing')) {
                $ok = $false; $detail += "revision $($rev.name): health=$health running=$running"
            }
        }

        $console = Invoke-Az @('containerapp', 'logs', 'show', '-g', $Rg, '-n', $name, '--type', 'console', '--tail', '200') -Raw -AllowFailure
        $system = Invoke-Az @('containerapp', 'logs', 'show', '-g', $Rg, '-n', $name, '--type', 'system', '--tail', '100') -Raw -AllowFailure
        $logText = (([string]$console) + "`n" + ([string]$system))
        if (-not (Test-LogClean -LogText $logText -ErrorPatterns $ErrorPatterns)) {
            $ok = $false
            $detail += (Get-LogErrorLines -LogText $logText -ErrorPatterns $ErrorPatterns | Select-Object -First 5)
        }
        return @{ Ok = $ok; Detail = ($detail -join '; ') }
    } finally {
        if ($scaled) { Invoke-Az @('containerapp', 'update', '-g', $Rg, '-n', $name, '--min-replicas', '0') -AllowFailure | Out-Null }
    }
}

function Test-WebAppHealthy {
    <#
    .SYNOPSIS
    Stato Running e probe HTTP sul path di health. 401/403 contano come "vivo" (endpoint protetti).
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][pscustomobject]$Service,
        [string]$HealthPath = '/',
        [string[]]$ErrorPatterns = @()
    )
    $app = Invoke-Az @('webapp', 'show', '-g', $Rg, '-n', $Service.Name)
    if (-not $app) { return @{ Ok = $false; Detail = 'webapp show vuoto' } }
    $state = [string]$app.state
    if ($state -ne 'Running') { return @{ Ok = $false; Detail = "state=$state" } }
    $path = if ($HealthPath) { $HealthPath } else { '/' }
    if (-not $path.StartsWith('/')) { $path = '/' + $path }
    $url = "https://$($app.defaultHostName)$path"
    $code = Invoke-HttpProbe -Url $url
    $alive = ($code -ge 200 -and $code -lt 400) -or $code -in @(401, 403)
    return @{ Ok = $alive; Detail = "HTTP $code $url" }
}

function Test-FunctionAppHealthy {
    <#
    .SYNOPSIS
    Stato Running e almeno una function caricata (un deploy rotto lascia la lista vuota).
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][pscustomobject]$Service,
        [string[]]$ErrorPatterns = @()
    )
    $app = Invoke-Az @('functionapp', 'show', '-g', $Rg, '-n', $Service.Name)
    $state = if ($app) { [string]$app.state } else { 'unknown' }
    if ($state -ne 'Running') { return @{ Ok = $false; Detail = "state=$state" } }
    $fns = @(Invoke-Az @('functionapp', 'function', 'list', '-g', $Rg, '-n', $Service.Name) -AllowFailure | Where-Object { $null -ne $_ })
    if ($fns.Count -eq 0) { return @{ Ok = $false; Detail = 'nessuna function caricata (deploy fallito o host non avviato)' } }
    return @{ Ok = $true; Detail = "functions: $(($fns | ForEach-Object { $_.name }) -join ', ')" }
}
