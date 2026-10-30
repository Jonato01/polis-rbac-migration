Set-StrictMode -Version Latest

<#
Stato della run (work/state.json): per ogni fase e servizio, status + dettaglio.
Permette il rilancio idempotente dopo un'interruzione.
#>

$script:ValidStatuses = @('pending', 'done', 'failed', 'skipped')

function New-RunState {
    param([Parameter(Mandatory)][string]$WorkDir)
    if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null }
    $path = Join-Path $WorkDir 'state.json'
    $state = @{ Path = $path; RunId = (Get-Date -Format 'yyyyMMdd-HHmmss'); StartedAt = (Get-Date).ToString('o'); Steps = @{} }
    if (Test-Path $path) {
        $json = Get-Content $path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($json.PSObject.Properties['RunId']) { $state.RunId = $json.RunId }
        if ($json.PSObject.Properties['StartedAt']) { $state.StartedAt = $json.StartedAt }
        if ($json.PSObject.Properties['Steps']) {
            foreach ($p in $json.Steps.PSObject.Properties) {
                $state.Steps[$p.Name] = @{ Status = $p.Value.Status; Detail = $p.Value.Detail; UpdatedAt = $p.Value.UpdatedAt }
            }
        }
    }
    return $state
}

function Save-RunState {
    param([Parameter(Mandatory)][hashtable]$State)
    $obj = @{ RunId = $State.RunId; StartedAt = $State.StartedAt; Steps = $State.Steps }
    $obj | ConvertTo-Json -Depth 5 | Set-Content -Path $State.Path -Encoding UTF8
}

function Get-StepKey { param([string]$Phase, [string]$Service) return "$Phase|$Service" }

function Set-StepStatus {
    param(
        [Parameter(Mandatory)][hashtable]$State,
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][string]$Status,
        [string]$Detail = ''
    )
    if ($script:ValidStatuses -notcontains $Status) { throw "Status non valido: $Status (ammessi: $($script:ValidStatuses -join ', '))" }
    $State.Steps[(Get-StepKey $Phase $Service)] = @{ Status = $Status; Detail = $Detail; UpdatedAt = (Get-Date).ToString('o') }
}

function Get-StepStatus {
    param([Parameter(Mandatory)][hashtable]$State, [Parameter(Mandatory)][string]$Phase, [Parameter(Mandatory)][string]$Service)
    $k = Get-StepKey $Phase $Service
    if ($State.Steps.ContainsKey($k)) { return $State.Steps[$k].Status }
    return 'pending'
}

function Get-StepDetail {
    param([Parameter(Mandatory)][hashtable]$State, [Parameter(Mandatory)][string]$Phase, [Parameter(Mandatory)][string]$Service)
    $k = Get-StepKey $Phase $Service
    if ($State.Steps.ContainsKey($k)) { return $State.Steps[$k].Detail }
    return ''
}

function Get-ServicesByStatus {
    param([Parameter(Mandatory)][hashtable]$State, [Parameter(Mandatory)][string]$Phase, [Parameter(Mandatory)][string]$Status)
    $result = @()
    foreach ($k in $State.Steps.Keys) {
        $parts = $k -split '\|', 2
        if ($parts[0] -eq $Phase -and $State.Steps[$k].Status -eq $Status) { $result += $parts[1] }
    }
    return [string[]]($result | Sort-Object)
}
