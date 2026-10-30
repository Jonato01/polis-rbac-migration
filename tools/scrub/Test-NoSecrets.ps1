<#
.SYNOPSIS
Scansione pre-push: segnala file vietati (env, settings locali) e stringhe che sembrano segreti reali.

.OUTPUTS
Oggetti { File; Line; Pattern; Snippet } (snippet mascherato). Con -FailOnFindings esce con codice 1 se trova qualcosa.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Path,
    [switch]$FailOnFindings
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$excludeDirs = @('.git', '.venv', 'venv', 'node_modules', '__pycache__', '.superpowers', '.pytest_cache', '.idea', 'work')
$forbiddenNames = '^(\.env($|\..*)|.*\.env|\.prodenv|\.colenv|\.envprod|\.envcol|local\.settings\.json|db_pass\.sh|set_api_settings\.sh|set_web_settings\.sh|file_fallito\.ps1|.*\.(pem|pfx|key|p12))$'
$patterns = [ordered]@{
    'AccountKey reale'           = 'AccountKey=[A-Za-z0-9+/]{20,}={0,2}'
    'SharedAccessKey reale'      = 'SharedAccessKey=[A-Za-z0-9+/]{20,}={0,2}'
    'accesskey reale'            = '(?i)accesskey=[A-Za-z0-9+/]{20,}'
    'sk-ant (Anthropic)'         = 'sk-ant-[A-Za-z0-9_\-]{20,}'
    'sk- (OpenAI)'               = '\bsk-[A-Za-z0-9]{32,}'
    'SAS token'                  = '(?i)[?&]sig=[A-Za-z0-9%+/]{20,}'
    'password/secret letterale'  = '(?i)\b(password|passwd|pwd|client_secret|api_key|apikey|secret_key|jwt_secret)\b\s*[:=]\s*["''][^"''\s<>{}$]{6,}["'']'
}
$allowLine = '(?i)os\.getenv|os\.environ|getenv\(|<[a-z_\- ]+>|\.\.\.|your-|change-?me|placeholder|example|RkFLRQ==|FAKE|__[A-Z_]+__'

$findings = @()
$root = (Resolve-Path $Path).Path.TrimEnd('\')
$files = Get-ChildItem -Path $root -Recurse -File -Force | Where-Object {
    $rel = $_.FullName.Substring($root.Length).TrimStart('\')
    $parts = $rel -split '\\'
    $skip = $false
    foreach ($d in $parts[0..([Math]::Max(0, $parts.Count - 2))]) { if ($excludeDirs -contains $d) { $skip = $true } }
    -not $skip
}
foreach ($f in $files) {
    $rel = $f.FullName.Substring($root.Length).TrimStart('\')
    if ($f.Name -match $forbiddenNames) {
        $findings += [pscustomobject]@{ File = $rel; Line = 0; Pattern = 'file vietato nel repo pubblico'; Snippet = $f.Name }
        continue
    }
    if ($f.Extension -in '.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.zip', '.xlsx', '.xls', '.docx', '.pyc', '.dll', '.exe') { continue }
    if ($f.Length -gt 5MB) { continue }
    $n = 0
    foreach ($line in [IO.File]::ReadLines($f.FullName)) {
        $n++
        if ($line -match $allowLine) { continue }
        foreach ($name in $patterns.Keys) {
            if ($line -match $patterns[$name]) {
                $snippet = $line.Trim()
                if ($snippet.Length -gt 60) { $snippet = $snippet.Substring(0, 60) }
                $snippet = $snippet -replace '([A-Za-z0-9+/_\-]{8})[A-Za-z0-9+/_\-=]{8,}', '$1***'
                $findings += [pscustomobject]@{ File = $rel; Line = $n; Pattern = $name; Snippet = $snippet }
                break
            }
        }
    }
}

$findings
if ($FailOnFindings) {
    if (@($findings).Count -gt 0) { Write-Host "Test-NoSecrets: $(@($findings).Count) finding" -ForegroundColor Red; exit 1 }
    Write-Host 'Test-NoSecrets: pulito' -ForegroundColor Green
    exit 0
}
