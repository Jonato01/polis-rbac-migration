Set-StrictMode -Version Latest

<#
Backup/restore delle settings, gate dell'hardening e hardening (disabilitazione chiavi).
Dipende da Az.ps1 (Invoke-Az), State.ps1, Deploy.ps1 (Get-StorageAccountKey, New-StorageConnectionString).
#>

function Save-ServiceBackup {
    <#
    .SYNOPSIS
    Salva settings/env/secret-name/mount/identity del servizio in work/backup/<name>.json.
    Non sovrascrive un backup esistente (il primo e' lo stato pre-migrazione).
    #>
    param(
        [Parameter(Mandatory)][pscustomobject]$Service,
        [Parameter(Mandatory)][string]$WorkDir
    )
    $dir = Join-Path $WorkDir 'backup'
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $path = Join-Path $dir "$($Service.Name).json"
    if (Test-Path $path) { return $path }
    $obj = @{
        Kind = $Service.Kind; Name = $Service.Name
        Settings = $Service.Settings
        SecretNames = @(if ($Service.PSObject.Properties['SecretNames']) { $Service.SecretNames } else { @() })
        Mounts = @(if ($Service.PSObject.Properties['Mounts']) { $Service.Mounts } else { @() })
        Identity = $(if ($Service.PSObject.Properties['Identity']) { $Service.Identity } else { $null })
        Image = $(if ($Service.PSObject.Properties['Image']) { $Service.Image } else { $null })
        SavedAt = (Get-Date).ToString('o')
    }
    $obj | ConvertTo-Json -Depth 6 | Set-Content -Path $path -Encoding UTF8
    return $path
}

function Restore-ServiceBackup {
    <#
    .SYNOPSIS
    Riapplica le settings (web/function app) o le env (container app) salvate nel backup.
    I valori secretref delle container app non sono ripristinabili (i secret non vengono letti).
    #>
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Rg
    )
    $b = Get-Content $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    $settings = @{}
    foreach ($p in $b.Settings.PSObject.Properties) { if ([string]$p.Value -notlike 'secretref:*') { $settings[$p.Name] = [string]$p.Value } }
    if ($settings.Count -eq 0) { return }
    switch ($b.Kind) {
        'containerApp' {
            $args = @('containerapp', 'update', '-g', $Rg, '-n', $b.Name, '--set-env-vars')
            foreach ($k in $settings.Keys) { $args += "$k=$($settings[$k])" }
            Invoke-Az $args | Out-Null
        }
        default {
            $cmd = if ($b.Kind -eq 'functionApp') { 'functionapp' } else { 'webapp' }
            $args = @($cmd, 'config', 'appsettings', 'set', '-g', $Rg, '-n', $b.Name, '--settings')
            foreach ($k in $settings.Keys) { $args += "$k=$($settings[$k])" }
            Invoke-Az $args | Out-Null
        }
    }
}

function Test-HardeningAllowed {
    <#
    .SYNOPSIS
    Gate: nessun servizio key-based non migrato (mappato o no, escluso o no) e nessuno step rosso in fase 07.
    Con -Force ritorna Allowed=true ma conserva le ragioni (finiscono nel report).
    #>
    param(
        [Parameter(Mandatory)][pscustomobject]$Inventory,
        [Parameter(Mandatory)][hashtable]$State,
        [bool]$Force = $false
    )
    $reasons = @()
    foreach ($u in @($Inventory.Unmapped)) { if ($null -ne $u) { $reasons += "servizio key-based non mappato: $($u.Name)" } }
    foreach ($s in @($Inventory.Services | Where-Object { $_.Excluded -and $_.KeyBased })) {
        $reasons += "servizio escluso dallo script ma ancora key-based: $($s.Name) (va migrato dal cliente prima di disabilitare le chiavi)"
    }
    foreach ($svc in (Get-ServicesByStatus -State $State -Phase '07' -Status 'failed')) {
        $reasons += "verifica fallita (fase 07) per $svc : $(Get-StepDetail -State $State -Phase '07' -Service $svc)"
    }
    foreach ($svc in (Get-ServicesByStatus -State $State -Phase '06' -Status 'failed')) {
        $reasons += "switch settings fallito (fase 06) per $svc"
    }
    $allowed = ($reasons.Count -eq 0) -or $Force
    return @{ Allowed = $allowed; Reasons = [string[]]@($reasons) }
}

function Set-FunctionHostKey {
    <# Riapplica la key dello storage host a content share e mount Azure Files della Function App. #>
    param([string]$Rg, [pscustomobject]$Service, [string]$HostAccount, [string]$Key)
    $settings = $Service.Settings
    $share = if ($settings.ContainsKey('WEBSITE_CONTENTSHARE')) { $settings['WEBSITE_CONTENTSHARE'] } else { $null }
    $cs = New-StorageConnectionString -Account $HostAccount -Key $Key
    $args = @('functionapp', 'config', 'appsettings', 'set', '-g', $Rg, '-n', $Service.Name, '--settings', "WEBSITE_CONTENTAZUREFILECONNECTIONSTRING=$cs")
    if ($share) { $args += "WEBSITE_CONTENTSHARE=$share" }
    Invoke-Az $args | Out-Null
    foreach ($m in @($Service.Mounts)) {
        if ($null -eq $m -or $m.type -ne 'AzureFiles') { continue }
        Invoke-Az @('webapp', 'config', 'storage-account', 'update', '-g', $Rg, '-n', $Service.Name, '--custom-id', $m.name, '--storage-type', 'AzureFiles',
            '--account-name', $HostAccount, '--share-name', $m.shareName, '--access-key', $Key, '--mount-path', $m.mountPath) | Out-Null
    }
}

function Invoke-Hardening {
    <#
    .SYNOPSIS
    Disabilita shared key sugli storage dati, local auth sul Service Bus, admin user sull'ACR;
    ruota key1/key2 di tutti gli storage e riapplica la key host alle Function App.
    #>
    param([Parameter(Mandatory)][pscustomobject]$Inventory)
    $rg = $Inventory.Rg
    foreach ($s in @($Inventory.StorageAccounts)) {
        $sRg = if ($s.PSObject.Properties['ResourceGroup'] -and $s.ResourceGroup) { $s.ResourceGroup } else { $rg }
        if ($s.Role -eq 'data') {
            Invoke-Az @('storage', 'account', 'update', '-n', $s.Name, '-g', $sRg, '--allow-shared-key-access', 'false') | Out-Null
        }
    }
    if ($Inventory.ServiceBus) {
        Invoke-Az @('servicebus', 'namespace', 'update', '-g', $rg, '-n', $Inventory.ServiceBus.Name, '--disable-local-auth', 'true') | Out-Null
    }
    if ($Inventory.Acr) {
        Invoke-Az @('acr', 'update', '-n', $Inventory.Acr.Name, '--admin-enabled', 'false') | Out-Null
    }
    # rotazione chiavi: le vecchie (eventualmente trapelate) smettono di funzionare
    foreach ($s in @($Inventory.StorageAccounts)) {
        $sRg = if ($s.PSObject.Properties['ResourceGroup'] -and $s.ResourceGroup) { $s.ResourceGroup } else { $rg }
        Invoke-Az @('storage', 'account', 'keys', 'renew', '-n', $s.Name, '-g', $sRg, '--key', 'key1') -AllowFailure | Out-Null
        Invoke-Az @('storage', 'account', 'keys', 'renew', '-n', $s.Name, '-g', $sRg, '--key', 'key2') -AllowFailure | Out-Null
        if ($s.Role -eq 'host') {
            $newKey = Get-StorageAccountKey -Account $s.Name -Rg $sRg
            if (-not $newKey) { $newKey = '' }
            foreach ($fn in @($Inventory.Services | Where-Object { $_.Kind -eq 'functionApp' -and $_.Mapping -and $_.Mapping.PSObject.Properties['hostStorage'] -and $_.Mapping.hostStorage })) {
                Set-FunctionHostKey -Rg $rg -Service $fn -HostAccount $s.Name -Key $newKey
            }
        }
    }
}
