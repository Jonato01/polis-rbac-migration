Set-StrictMode -Version Latest

function Invoke-Phase09 {
    param([Parameter(Mandatory)][hashtable]$Context)
    $dir = if ($Context.Options.ContainsKey('ReportDir') -and $Context.Options.ReportDir) { $Context.Options.ReportDir } else { [Environment]::GetFolderPath('Desktop') }
    $path = Join-Path $dir "polis-update-report-$($Context.State.RunId).md"
    $Context.ReportPath = New-RunReport -Inventory $Context.Inventory -State $Context.State -OutPath $path -Fallback @($Context.FallbackLog) -HardeningReasons @($Context.HardeningReasons)
    Set-Content -Path (Join-Path $Context.WorkDir 'report-path.txt') -Value $Context.ReportPath -Encoding UTF8
    Write-Step "  Report: $($Context.ReportPath)" 'Green'
}
