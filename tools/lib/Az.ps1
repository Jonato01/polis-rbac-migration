Set-StrictMode -Version Latest

<#
Wrapper unico per Azure CLI. Tutte le altre lib passano da Invoke-Az, così:
- i test possono mockare Invoke-AzProcess / Invoke-Az
- WhatIf salta i comandi mutanti
- il log non contiene mai valori di settings/secret
#>

$script:AzWhatIf = $false
$script:AzLogPath = $null

function Set-AzWhatIf {
    param([bool]$On)
    $script:AzWhatIf = $On
}

function Get-AzWhatIf { return $script:AzWhatIf }

function Set-AzLogPath {
    param([string]$Path)
    $script:AzLogPath = $Path
}

function Write-AzLog {
    param([string]$Message)
    $line = "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Verbose $line
    if ($script:AzLogPath) { Add-Content -Path $script:AzLogPath -Value $line -Encoding UTF8 }
}

$script:MutatingVerbs = @('create', 'update', 'set', 'delete', 'remove', 'renew', 'regenerate', 'build', 'config-zip',
    'assign', 'start', 'stop', 'restart', 'upload', 'import', 'add', 'deploy', 'activate', 'deactivate', 'copy')

function Test-AzMutating {
    param([string[]]$Arguments)
    foreach ($a in $Arguments) {
        if ($a -like '--*') { break }
        if ($script:MutatingVerbs -contains $a.ToLowerInvariant()) { return $true }
    }
    return $false
}

$script:SensitiveFlags = @('--settings', '--secrets', '--set-env-vars', '--env-vars', '--registry-password', '--password',
    '--connection-string', '--account-key', '--sas-token', '--value')

function Get-MaskedArguments {
    param([string[]]$Arguments)
    $out = @()
    $maskNext = $false
    foreach ($a in $Arguments) {
        if ($maskNext -and -not ($a -like '--*')) {
            if ($a -match '^([^=]+)=') { $out += "$($Matches[1])=***" } else { $out += '***' }
            continue
        }
        $maskNext = $script:SensitiveFlags -contains $a.ToLowerInvariant()
        $out += $a
    }
    return $out
}

function Invoke-AzProcess {
    <#
    .SYNOPSIS
    Esegue az con gli argomenti dati. Separata per essere mockabile nei test.
    #>
    param([string[]]$Arguments)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'az'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    # az.cmd su Windows: passare tramite cmd per risolvere lo shim
    $psi.FileName = 'cmd.exe'
    $quoted = $Arguments | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }
    $psi.Arguments = '/d /c az ' + ($quoted -join ' ')
    $p = [System.Diagnostics.Process]::Start($psi)
    $stdout = $p.StandardOutput.ReadToEnd()
    $stderr = $p.StandardError.ReadToEnd()
    $p.WaitForExit()
    return @{ ExitCode = $p.ExitCode; StdOut = $stdout; StdErr = $stderr }
}

function Invoke-Az {
    <#
    .SYNOPSIS
    Esegue un comando az e restituisce l'output JSON convertito (o testo con -Raw).
    Lancia su exit code != 0 salvo -AllowFailure (allora ritorna $null).
    In WhatIf i comandi mutanti vengono solo loggati e ritornano $null.
    #>
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$AllowFailure,
        [switch]$Raw
    )
    $masked = (Get-MaskedArguments $Arguments) -join ' '
    if ($script:AzWhatIf -and (Test-AzMutating $Arguments)) {
        Write-AzLog "[WHATIF] az $masked"
        return $null
    }
    $args2 = @($Arguments) + @('--only-show-errors')
    if (-not $Raw -and -not ($Arguments -contains '-o') -and -not ($Arguments -contains '--output')) { $args2 += @('-o', 'json') }
    Write-AzLog "az $masked"
    $res = Invoke-AzProcess -Arguments $args2
    if ($res.ExitCode -ne 0) {
        $msg = "az $masked fallito (exit $($res.ExitCode)): $($res.StdErr.Trim())"
        Write-AzLog "[ERROR] $msg"
        if ($AllowFailure) { return $null }
        throw $msg
    }
    if ($Raw) { return $res.StdOut }
    $text = $res.StdOut.Trim()
    if ([string]::IsNullOrEmpty($text)) { return $null }
    try { return ($text | ConvertFrom-Json) } catch { return $text }
}
