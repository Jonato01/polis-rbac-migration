Set-StrictMode -Version Latest

function Invoke-Phase02 {
    <# Discovery, backup settings, piano a video, conferma. #>
    param([Parameter(Mandatory)][hashtable]$Context)
    $inv = Get-PolisInventory -Rg $Context.Rg -Mapping $Context.Mapping
    $Context.Inventory = $inv
    foreach ($s in @($inv.Services)) { Save-ServiceBackup -Service $s -WorkDir $Context.WorkDir | Out-Null }

    Write-Host ''
    Write-Host 'PIANO' -ForegroundColor Green
    $rows = @()
    foreach ($s in @($inv.Services | Sort-Object Kind, Name)) {
        $azione = if ($s.Excluded) { 'ESCLUSO (gestito dal cliente)' }
                  elseif (-not $s.Mapping) { if ($s.KeyBased) { 'NON MAPPATO: key-based, non toccato (blocca hardening)' } else { 'non mappato, nessuna chiave: ignorato' } }
                  elseif (Test-MappingFlag $s.Mapping 'settingsOnly') { 'solo settings' }
                  elseif ($s.Kind -eq 'webApp') { 'identity + ruoli + settings (codice gia'' rilasciato via SCM)' }
                  else { 'identity + ruoli + build/deploy + settings' }
        if ($s.Legacy) { $azione += ' [legacy]' }
        $rows += [pscustomobject]@{ Servizio = $s.Name; Tipo = $s.Kind; KeyBased = $s.KeyBased; Azione = $azione }
    }
    $rows | Format-Table -AutoSize | Out-String -Width 200 | Write-Host
    Write-Host "Storage dati: $(($inv.StorageAccounts | Where-Object Role -eq 'data' | ForEach-Object Name) -join ', ')"
    Write-Host "Storage host: $(($inv.StorageAccounts | Where-Object Role -eq 'host' | ForEach-Object Name) -join ', ')"
    if ($inv.ServiceBus) { Write-Host "Service Bus: $($inv.ServiceBus.Name)" }
    if ($inv.Acr) { Write-Host "ACR: $($inv.Acr.Name)" }
    if (@($inv.MissingExpected).Count -gt 0) { Write-Host "Attesi ma assenti (non in uso): $($inv.MissingExpected -join ', ')" -ForegroundColor DarkGray }
    if (@($inv.Unmapped).Count -gt 0) { Write-Host "ATTENZIONE servizi key-based non mappati: $(($inv.Unmapped | ForEach-Object Name) -join ', ')" -ForegroundColor Yellow }
    if ($Context.Options.WhatIf) { Write-Host 'Modalita'' WhatIf: nessuna modifica verra'' applicata' -ForegroundColor Yellow }
    Write-Host ''
    if (-not (Read-Confirmation -Message 'Procedere con la migrazione?' -NonInteractive ([bool]$Context.Options.NonInteractive))) {
        Stop-Migration 'Operatore ha annullato dopo il piano'
    }
}
