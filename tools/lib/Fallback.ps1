Set-StrictMode -Version Latest

<#
Fallback con Claude Code: quando uno step fallisce dopo il retry, installa Claude Code
(installer nativo Windows, nessun Node) e lo lancia in modalita' non interattiva con il
contesto dell'errore. Il risultato viene letto da un file JSON scritto da Claude.
#>

function Invoke-AnthropicProbe {
    <# Separata per essere mockabile: GET /v1/models con la key, ritorna lo status HTTP. #>
    param([Parameter(Mandatory)][string]$Key)
    try {
        $r = Invoke-WebRequest -Uri 'https://api.anthropic.com/v1/models?limit=1' -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop `
            -Headers @{ 'x-api-key' = $Key; 'anthropic-version' = '2023-06-01' }
        return [int]$r.StatusCode
    } catch {
        if ($_.Exception.PSObject.Properties['Response'] -and $_.Exception.Response) { return [int]$_.Exception.Response.StatusCode }
        return 0
    }
}

function Test-AnthropicKey {
    param([string]$Key)
    if ([string]::IsNullOrWhiteSpace($Key)) { return $false }
    return ((Invoke-AnthropicProbe -Key $Key) -eq 200)
}

function Read-FallbackKey {
    <# Chiede all'operatore una key (input mascherato). Vuoto = salta il fallback. #>
    Write-Host ''
    Write-Host "Il fallback automatico richiede una API key Anthropic valida (quella nel bootstrap manca o e' scaduta)." -ForegroundColor Yellow
    $sec = Read-Host 'Incolla la key (Invio per saltare il fallback)' -AsSecureString
    if (-not $sec -or $sec.Length -eq 0) { return '' }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Get-ClaudeExePath {
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\claude.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\claude\claude.exe')
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command claude -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Ensure-ClaudeCode {
    <#
    .SYNOPSIS
    Installa Claude Code con l'installer nativo se assente. Ritorna il path dell'eseguibile.
    #>
    $exe = Get-ClaudeExePath
    if ($exe) { return $exe }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $installer = Invoke-RestMethod -Uri 'https://claude.ai/install.ps1' -UseBasicParsing
    Invoke-Expression $installer | Out-Null
    $env:PATH = "$env:PATH;$(Join-Path $env:USERPROFILE '.local\bin')"
    $exe = Get-ClaudeExePath
    if (-not $exe) { throw 'Installazione di Claude Code fallita: eseguibile non trovato' }
    return $exe
}

function New-FallbackPrompt {
    param(
        [Parameter(Mandatory)][hashtable]$Context,
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][string]$ErrorText,
        [Parameter(Mandatory)][int]$Attempt,
        [Parameter(Mandatory)][string]$ResultPath
    )
    $template = Get-Content (Join-Path $Context.ToolsDir 'fallback\prompt.txt') -Raw -Encoding UTF8
    $map = @{
        '__PHASE__' = $Phase; '__SERVICE__' = $Service; '__ATTEMPT__' = $Attempt; '__ERROR__' = $ErrorText
        '__RG__' = $Context.Rg; '__SUB__' = [string]$Context.Sub; '__WORKDIR__' = $Context.WorkDir; '__TOOLSDIR__' = $Context.ToolsDir
        '__REPOROOT__' = $Context.RepoRoot; '__MAPPING__' = (Join-Path $Context.ToolsDir 'mapping.json'); '__STATE__' = (Join-Path $Context.WorkDir 'state.json')
        '__RESULT__' = $ResultPath
    }
    foreach ($k in $map.Keys) { $template = $template.Replace($k, [string]$map[$k]) }
    return $template
}

function Invoke-ClaudeProcess {
    <# Separata per essere mockabile. Lancia claude -p e ritorna exit code / stdout. #>
    param([string]$Exe, [string]$Prompt, [string]$Cwd, [string]$ResultPath, [int]$MaxTurns = 40)
    $promptFile = [IO.Path]::GetTempFileName()
    Set-Content -Path $promptFile -Value $Prompt -Encoding UTF8
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Exe
        $psi.WorkingDirectory = $Cwd
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardInput = $true
        $psi.Arguments = "-p --dangerously-skip-permissions --output-format json --max-turns $MaxTurns"
        $p = [Diagnostics.Process]::Start($psi)
        $p.StandardInput.Write((Get-Content $promptFile -Raw))
        $p.StandardInput.Close()
        $out = $p.StandardOutput.ReadToEnd()
        $p.WaitForExit()
        return @{ ExitCode = $p.ExitCode; StdOut = $out }
    } finally { Remove-Item $promptFile -Force -ErrorAction SilentlyContinue }
}

function Invoke-ClaudeFallback {
    <#
    .SYNOPSIS
    Tenta di far risolvere lo step a Claude Code. Ritorna @{ Ok; Skipped; Summary; ResultPath; Transcript }.
    #>
    param(
        [Parameter(Mandatory)][hashtable]$Context,
        [Parameter(Mandatory)][string]$Phase,
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][string]$ErrorText,
        [Parameter(Mandatory)][int]$Attempt
    )
    $key = [string]$Context.Options.AnthropicApiKey
    if (-not (Test-AnthropicKey -Key $key)) {
        $key = Read-FallbackKey
        if (-not (Test-AnthropicKey -Key $key)) {
            return @{ Ok = $false; Skipped = $true; Summary = 'fallback saltato: nessuna API key Anthropic valida'; ResultPath = $null; Transcript = '' }
        }
        $Context.Options.AnthropicApiKey = $key
    }
    $dir = Join-Path $Context.WorkDir 'fallback'
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $resultPath = Join-Path $dir "$Phase-$Service-$Attempt.result.json"
    if (Test-Path $resultPath) { Remove-Item $resultPath -Force }
    $prompt = New-FallbackPrompt -Context $Context -Phase $Phase -Service $Service -ErrorText $ErrorText -Attempt $Attempt -ResultPath $resultPath

    $exe = Ensure-ClaudeCode
    $env:ANTHROPIC_API_KEY = $key
    try {
        $res = Invoke-ClaudeProcess -Exe $exe -Prompt $prompt -Cwd $Context.ToolsDir -ResultPath $resultPath
    } finally { Remove-Item Env:\ANTHROPIC_API_KEY -ErrorAction SilentlyContinue }

    $transcriptPath = Join-Path $dir "$Phase-$Service-$Attempt.transcript.json"
    Set-Content -Path $transcriptPath -Value ([string]$res.StdOut) -Encoding UTF8
    if (-not (Test-Path $resultPath)) {
        return @{ Ok = $false; Skipped = $false; Summary = "claude exit $($res.ExitCode), nessun file risultato"; ResultPath = $resultPath; Transcript = [string]$res.StdOut }
    }
    $result = Get-Content $resultPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $ok = $result.PSObject.Properties['ok'] -and [bool]$result.ok
    $summary = if ($result.PSObject.Properties['summary']) { [string]$result.summary } else { '' }
    return @{ Ok = $ok; Skipped = $false; Summary = $summary; ResultPath = $resultPath; Transcript = [string]$res.StdOut }
}
