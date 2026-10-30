Set-StrictMode -Version Latest

function Invoke-Phase10 {
    <#
    .SYNOPSIS
    Ripulisce il PC: profilo az, Azure CLI, Claude Code, cartella di lavoro. Aggiorna la sezione
    "Software rimosso" del report. Chiamata dal bootstrap in finally.
    #>
    param(
        [Parameter(Mandatory)][string]$WorkDir,
        [bool]$KeepBackup = $false,
        [string]$ReportPath,
        [bool]$KeepAzureCli = $false
    )
    Reset-CleanupLog
    Remove-AzureProfile
    if (-not $KeepAzureCli) { Remove-AzureCli } else { Add-CleanupLog 'Azure CLI lasciata installata su richiesta (-KeepAzureCli)' }
    Remove-ClaudeCode
    Remove-WorkDir -WorkDir $WorkDir -KeepBackup $KeepBackup
    $summary = Get-CleanupSummary
    if ($ReportPath -and (Test-Path $ReportPath)) {
        $md = Get-Content $ReportPath -Raw -Encoding UTF8
        $section = "## Software rimosso dal PC`n`n" + (($summary | ForEach-Object { "- $_" }) -join "`n") + "`n"
        if ($md -match '(?s)## Software rimosso dal PC.*$') { $md = $md -replace '(?s)## Software rimosso dal PC.*$', $section } else { $md += "`n$section" }
        Set-Content -Path $ReportPath -Value $md -Encoding UTF8
    }
    return $summary
}
