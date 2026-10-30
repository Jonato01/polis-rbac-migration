Set-StrictMode -Version Latest

function Invoke-Phase08 {
    <# Disabilitazione chiavi: solo con gate verde (o -ForceHardening) e conferma esplicita. #>
    param([Parameter(Mandatory)][hashtable]$Context)
    if ($Context.Options.WhatIf) {
        Set-StepStatus -State $Context.State -Phase '08' -Service '_global' -Status 'skipped' -Detail 'WhatIf: hardening non eseguito'
        return
    }
    $gate = Test-HardeningAllowed -Inventory $Context.Inventory -State $Context.State -Force ([bool]$Context.Options.ForceHardening)
    $Context.HardeningReasons = @($gate.Reasons)
    foreach ($r in @($gate.Reasons)) { Write-Step "  blocco: $r" 'Yellow' }
    if (-not $gate.Allowed) {
        Set-StepStatus -State $Context.State -Phase '08' -Service '_global' -Status 'skipped' -Detail 'bloccato: servizi key-based non migrati o verifiche fallite (vedi report)'
        return
    }
    Write-Host ''
    Write-Host 'HARDENING: verranno disabilitate shared key sugli storage dati, local auth sul Service Bus, admin ACR, e ruotate tutte le chiavi.' -ForegroundColor Yellow
    if ($gate.Reasons.Count -gt 0) { Write-Host 'ATTENZIONE: -ForceHardening attivo, i blocchi sopra verranno ignorati.' -ForegroundColor Red }
    if (-not (Read-Confirmation -Message 'Confermi la disabilitazione delle chiavi (hardening)?' -NonInteractive ([bool]$Context.Options.NonInteractive))) {
        Set-StepStatus -State $Context.State -Phase '08' -Service '_global' -Status 'skipped' -Detail 'operatore ha rifiutato: chiavi ancora attive'
        return
    }
    try {
        Invoke-Hardening -Inventory $Context.Inventory
        foreach ($s in @($Context.Inventory.StorageAccounts)) { if ($s.Role -eq 'data') { $s.AllowSharedKey = $false } }
        if ($Context.Inventory.ServiceBus) { $Context.Inventory.ServiceBus.DisableLocalAuth = $true }
        if ($Context.Inventory.Acr) { $Context.Inventory.Acr.AdminEnabled = $false }
        Set-StepStatus -State $Context.State -Phase '08' -Service '_global' -Status 'done' -Detail ($(if ($gate.Reasons.Count -gt 0) { 'eseguito con -ForceHardening' } else { 'eseguito' }))
    } catch {
        Set-StepStatus -State $Context.State -Phase '08' -Service '_global' -Status 'failed' -Detail $_.Exception.Message
        Write-Step "  hardening fallito: $($_.Exception.Message)" 'Red'
    }
    Save-RunState -State $Context.State
}
