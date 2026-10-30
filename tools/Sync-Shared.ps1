<#
.SYNOPSIS
Copia src/_shared/azure_clients.py in ogni build context elencato.

.DESCRIPTION
I target arrivano da -Targets (percorsi assoluti o relativi alla root del repo) oppure dal
campo "sharedTargets" di un mapping.json (-MappingPath, default tools/mapping.json).
#>
[CmdletBinding()]
param(
    [string[]]$Targets,
    [string]$MappingPath
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$source = Join-Path $repoRoot 'src\_shared\azure_clients.py'
if (-not (Test-Path $source)) { throw "Helper condiviso non trovato: $source" }

if (-not $Targets) {
    if (-not $MappingPath) { $MappingPath = Join-Path $PSScriptRoot 'mapping.json' }
    if (-not (Test-Path $MappingPath)) { throw "Nessun -Targets e mapping non trovato: $MappingPath" }
    $mapping = Get-Content $MappingPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Targets = @($mapping.sharedTargets)
}

foreach ($t in $Targets) {
    $dir = if ([System.IO.Path]::IsPathRooted($t)) { $t } else { Join-Path $repoRoot $t }
    if (-not (Test-Path $dir)) { throw "Target inesistente: $dir" }
    Copy-Item -Path $source -Destination (Join-Path $dir 'azure_clients.py') -Force
    Write-Verbose "azure_clients.py -> $dir"
}
