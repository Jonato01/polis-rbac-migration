<#
.SYNOPSIS
Genera bootstrap/Run-PolisUpdate.ps1 (gitignored) dal template, fissando commit, hash dello zip e API key.

.EXAMPLE
.\bootstrap\New-Bootstrap.ps1 -Commit (git rev-parse HEAD) -AnthropicApiKey $key
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Commit,
    [Parameter(Mandatory)][string]$AnthropicApiKey,
    [string]$OutFile = (Join-Path $PSScriptRoot 'Run-PolisUpdate.ps1'),
    [string]$RepoSlug = 'Jonato01/polis-rbac-migration'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ($Commit -notmatch '^[0-9a-fA-F]{40}$') { throw "Commit non valido: serve lo SHA completo a 40 caratteri (ricevuto '$Commit')" }
$url = "https://github.com/$RepoSlug/archive/$Commit.zip"
$tmp = Join-Path $env:TEMP ("polis-bootstrap-" + [guid]::NewGuid().ToString('N') + '.zip')
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    if (-not (Test-Path $tmp)) { throw "Download fallito: $url" }
    $hash = (Get-FileHash -Path $tmp -Algorithm SHA256).Hash
} finally { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }

$template = Get-Content (Join-Path $PSScriptRoot 'Run-PolisUpdate.template.ps1') -Raw
$out = $template.Replace('__REPO_ZIP_URL__', $url).Replace('__ZIP_SHA256__', $hash).Replace('__ANTHROPIC_KEY__', $AnthropicApiKey).Replace('__COMMIT__', $Commit)
if ($out -match '__[A-Z_]+__') { throw 'Placeholder non sostituiti nel template' }
[IO.File]::WriteAllText($OutFile, $out, (New-Object Text.ASCIIEncoding))
Write-Host "Bootstrap generato: $OutFile"
Write-Host "  repo:   $url"
Write-Host "  sha256: $hash"
Write-Host '  ATTENZIONE: il file contiene la API key Anthropic. Consegnarlo solo via canale privato, mai nel repo.'
