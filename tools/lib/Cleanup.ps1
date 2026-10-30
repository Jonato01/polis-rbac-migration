Set-StrictMode -Version Latest

<#
Ripristino del PC dell'operatore: disinstalla Azure CLI e Claude Code, rimuove profilo az
e cartella di lavoro. Ogni azione finisce nel riepilogo per il report.
#>

$script:CleanupLog = New-Object System.Collections.ArrayList

function Reset-CleanupLog { $script:CleanupLog.Clear() }
function Add-CleanupLog { param([string]$Message) [void]$script:CleanupLog.Add($Message); Write-Host "[CLEANUP] $Message" }
function Get-CleanupSummary { return [string[]]@($script:CleanupLog) }

function Test-CommandAvailable {
    param([Parameter(Mandatory)][string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Start-UninstallProcess {
    <# Separata per essere mockabile. Ritorna l'exit code. #>
    param([Parameter(Mandatory)][string]$FilePath, [string]$ArgumentList)
    $p = Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -Wait -PassThru -WindowStyle Hidden
    return $p.ExitCode
}

function Get-UninstallEntries {
    <# Voci di disinstallazione dal registro (64 e 32 bit). #>
    $keys = @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*', 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*')
    $entries = @()
    foreach ($k in $keys) { $entries += @(Get-ItemProperty $k -ErrorAction SilentlyContinue | Where-Object { $_.PSObject.Properties['DisplayName'] -and $_.DisplayName }) }
    return $entries
}

function Remove-AzureCli {
    $entries = @(Get-UninstallEntries | Where-Object { $_.DisplayName -like 'Microsoft Azure CLI*' })
    if ($entries.Count -gt 0) {
        $code = $entries[0].PSChildName
        if ($code -notmatch '^\{') {
            if ($entries[0].PSObject.Properties['UninstallString'] -and $entries[0].UninstallString -match '(\{[0-9A-Fa-f-]+\})') { $code = $Matches[1] }
        }
        $exit = Start-UninstallProcess -FilePath 'msiexec.exe' -ArgumentList "/x $code /qn /norestart"
        Add-CleanupLog "Azure CLI disinstallata (msiexec exit $exit)"
        return
    }
    if (Test-CommandAvailable 'winget') {
        $exit = Start-UninstallProcess -FilePath 'winget' -ArgumentList 'uninstall --id Microsoft.AzureCLI -e --silent --accept-source-agreements'
        Add-CleanupLog "Azure CLI disinstallata via winget (exit $exit)"
        return
    }
    Add-CleanupLog 'Azure CLI non trovata: nulla da disinstallare'
}

function Get-UserPath { return [Environment]::GetEnvironmentVariable('Path', 'User') }
function Set-UserPath { param([string]$Value) [Environment]::SetEnvironmentVariable('Path', $Value, 'User') }

function Remove-ClaudeCode {
    param([string]$HomeDir = $env:USERPROFILE)
    $bin = Join-Path $HomeDir '.local\bin'
    $cfg = Join-Path $HomeDir '.claude'
    $found = $false
    if (Test-Path $bin) {
        $exes = @(Get-ChildItem $bin -Filter 'claude*' -ErrorAction SilentlyContinue)
        if ($exes.Count -gt 0) { $found = $true; $exes | Remove-Item -Force -Recurse -ErrorAction SilentlyContinue }
        if (-not (Get-ChildItem $bin -ErrorAction SilentlyContinue)) { Remove-Item $bin -Force -Recurse -ErrorAction SilentlyContinue }
    }
    if (Test-Path $cfg) { $found = $true; Remove-Item $cfg -Recurse -Force -ErrorAction SilentlyContinue }
    $cfgJson = Join-Path $HomeDir '.claude.json'
    if (Test-Path $cfgJson) { $found = $true; Remove-Item $cfgJson -Force -ErrorAction SilentlyContinue }
    $path = [string](Get-UserPath)
    if ($path) {
        $parts = @($path -split ';' | Where-Object { $_ -and $_ -notmatch '\\\.local\\bin\\?$' })
        $newPath = $parts -join ';'
        if ($newPath -ne $path) { Set-UserPath -Value $newPath; $found = $true }
    }
    if ($found) { Add-CleanupLog 'Claude Code rimosso (eseguibile, configurazione, voce PATH)' } else { Add-CleanupLog 'Claude Code non installato: nulla da rimuovere' }
}

function Remove-AzureProfile {
    param([string]$HomeDir = $env:USERPROFILE)
    if (Test-CommandAvailable 'az') {
        try { Start-UninstallProcess -FilePath 'cmd.exe' -ArgumentList '/d /c az logout --only-show-errors' | Out-Null } catch { }
        Add-CleanupLog 'az logout eseguito'
    }
    $dir = Join-Path $HomeDir '.azure'
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue; Add-CleanupLog 'Profilo Azure CLI (~/.azure, token cache) rimosso' }
}

function Remove-WorkDir {
    param([Parameter(Mandatory)][string]$WorkDir, [bool]$KeepBackup = $false)
    if (-not (Test-Path $WorkDir)) { return }
    if ($KeepBackup) {
        foreach ($item in (Get-ChildItem $WorkDir -Force)) {
            if ($item.Name -ne 'backup') { Remove-Item $item.FullName -Recurse -Force -ErrorAction SilentlyContinue }
        }
        Add-CleanupLog "Cartella di lavoro rimossa; conservato $WorkDir\backup (ATTENZIONE: contiene settings con chiavi/segreti, cancellarlo dopo l'uso)"
    } else {
        Remove-Item $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
        Add-CleanupLog "Cartella di lavoro $WorkDir rimossa (backup settings inclusi)"
    }
}
