Set-StrictMode -Version Latest

function Invoke-Phase00 {
    param([Parameter(Mandatory)][hashtable]$Context)
    $problems = @(Test-PreflightEnvironment)
    foreach ($p in $problems) { Write-Step "  PROBLEMA: $p" 'Red' }
    if ($problems.Count -gt 0) { Stop-Migration "Preflight fallito: $($problems -join '; ')" }
    Write-Step "  PowerShell $($PSVersionTable.PSVersion), TLS 1.2, rete: ok"
}
