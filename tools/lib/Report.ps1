Set-StrictMode -Version Latest

<#
Report finale in markdown. Dipende da State.ps1.
#>

$script:PhaseLabels = [ordered]@{
    '03' = 'Identity'; '04' = 'RBAC'; '05' = 'Build/Deploy'; '06' = 'Settings'; '07' = 'Verifica'
}

function Get-ServiceRow {
    param([pscustomobject]$Svc, [hashtable]$State)
    $mapped = if ($Svc.Mapping) { 'sì' } elseif ($Svc.Excluded) { 'escluso' } else { 'NO' }
    $cells = @("| $($Svc.Name) | $($Svc.Kind) | $mapped | $(if ($Svc.Legacy) { 'legacy' } else { '' }) | $(if ($Svc.KeyBased) { 'sì' } else { 'no' })")
    foreach ($ph in $script:PhaseLabels.Keys) {
        $st = Get-StepStatus -State $State -Phase $ph -Service $Svc.Name
        $cells += $st
    }
    $detail = Get-StepDetail -State $State -Phase '05' -Service $Svc.Name
    $verify = Get-StepDetail -State $State -Phase '07' -Service $Svc.Name
    $cells += ("$detail $verify".Trim() -replace '\|', '/')
    return ($cells -join ' | ') + ' |'
}

function New-RunReport {
    <#
    .SYNOPSIS
    Scrive il report markdown della run e ritorna il path.
    #>
    param(
        [Parameter(Mandatory)][pscustomobject]$Inventory,
        [Parameter(Mandatory)][hashtable]$State,
        [Parameter(Mandatory)][string]$OutPath,
        [object[]]$Fallback = @(),
        [string[]]$Cleanup = @(),
        [string[]]$HardeningReasons = @()
    )
    $md = @()
    $md += "# Polis - report aggiornamento identity/RBAC"
    $md += ""
    $md += "- Run: $($State.RunId) (avvio $($State.StartedAt))"
    $md += "- Resource group: $($Inventory.Rg)"
    $md += "- Generato: $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
    $md += ""
    $md += "## Servizi"
    $md += ""
    $header = "| Servizio | Tipo | Mappato | Note | Key-based prima | " + (($script:PhaseLabels.Values) -join ' | ') + " | Dettaglio |"
    $md += $header
    $md += "|" + ("---|" * (($header -split '\|').Count - 2))
    foreach ($s in @($Inventory.Services | Sort-Object Kind, Name)) { $md += (Get-ServiceRow -Svc $s -State $State) }
    $md += ""

    $md += "## Risorse condivise"
    $md += ""
    foreach ($s in @($Inventory.StorageAccounts)) {
        $sk = if ($s.PSObject.Properties['AllowSharedKey'] -and $s.AllowSharedKey -eq $false) { 'shared key DISABILITATA' } else { 'shared key abilitata' }
        $md += "- Storage account **$($s.Name)** (ruolo: $($s.Role)): $sk"
    }
    if ($Inventory.ServiceBus) { $md += "- Service Bus **$($Inventory.ServiceBus.Name)**: local auth $(if ($Inventory.ServiceBus.DisableLocalAuth) { 'DISABILITATA' } else { 'abilitata' })" }
    if ($Inventory.Acr) { $md += "- ACR **$($Inventory.Acr.Name)**: admin user $(if ($Inventory.Acr.AdminEnabled) { 'abilitato' } else { 'DISABILITATO' })" }
    $md += ""

    $hardStatus = Get-StepStatus -State $State -Phase '08' -Service '_global'
    $hardDetail = Get-StepDetail -State $State -Phase '08' -Service '_global'
    $md += "## Hardening"
    $md += ""
    $md += "- Esito: **$hardStatus** $hardDetail"
    foreach ($r in @($HardeningReasons)) { if ($r) { $md += "- Motivo: $r" } }
    $md += ""

    $md += "## Eccezioni"
    $md += ""
    $hosts = @($Inventory.StorageAccounts | Where-Object { $_.Role -eq 'host' })
    if ($hosts.Count -gt 0) {
        foreach ($h in $hosts) { $md += "- Lo storage account host **$($h.Name)** resta con shared key abilitata: Azure Files (content share e mount della Function App) non supporta l'accesso con identity. Le chiavi sono state ruotate e riapplicate dallo script." }
    } else { $md += "- Nessuna." }
    $md += ""

    $md += "## Servizi non mappati o esclusi ancora key-based"
    $md += ""
    $any = $false
    foreach ($u in @($Inventory.Unmapped)) { if ($null -ne $u) { $md += "- $($u.Name) ($($u.Kind)): non riconosciuto dalla mappa, non toccato"; $any = $true } }
    foreach ($s in @($Inventory.Services | Where-Object { $_.Excluded -and $_.KeyBased })) { $md += "- $($s.Name): escluso dallo script (gestito dal cliente), ancora key-based"; $any = $true }
    if (-not $any) { $md += "- Nessuno." }
    $md += ""

    $md += "## Servizi attesi ma assenti nel resource group"
    $md += ""
    if (@($Inventory.MissingExpected).Count -gt 0) { foreach ($m in $Inventory.MissingExpected) { $md += "- $m : non presente, considerato non in uso" } } else { $md += "- Nessuno." }
    $md += ""

    $md += "## Interventi del fallback (Claude Code)"
    $md += ""
    if (@($Fallback | Where-Object { $null -ne $_ }).Count -gt 0) {
        foreach ($f in $Fallback) {
            $md += "### Fase $($f.Phase) - $($f.Service) - tentativo $($f.Attempt) - esito: $(if ($f.Ok) { 'risolto' } else { 'non risolto' })"
            $md += ""
            $md += '```'
            $md += [string]$f.Transcript
            $md += '```'
            $md += ""
        }
    } else { $md += "- Nessun intervento." }
    $md += ""

    $md += "## Software rimosso dal PC"
    $md += ""
    if (@($Cleanup | Where-Object { $_ }).Count -gt 0) { foreach ($c in $Cleanup) { $md += "- $c" } } else { $md += "- (cleanup non ancora eseguito)" }
    $md += ""

    $dir = Split-Path $OutPath -Parent
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    ($md -join "`n") | Set-Content -Path $OutPath -Encoding UTF8
    return $OutPath
}
