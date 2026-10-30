Set-StrictMode -Version Latest

<#
Discovery delle risorse Polis nel resource group e classificazione.
Dipende da: Az.ps1 (Invoke-Az), Mapping.ps1, Settings.ps1.
#>

function Get-AppSettingsHashtable {
    param([object[]]$Items)
    $h = @{}
    foreach ($i in @($Items)) { if ($null -ne $i -and $i.PSObject.Properties['name']) { $h[$i.name] = [string]$i.value } }
    return $h
}

function Get-ContainerAppDetails {
    param([string]$Rg, [string]$Name)
    $app = Invoke-Az @('containerapp', 'show', '-g', $Rg, '-n', $Name)
    $container = $app.properties.template.containers[0]
    $settings = @{}
    $secretRefs = @{}
    foreach ($e in @($container.env)) {
        if ($e.PSObject.Properties['secretRef'] -and $e.secretRef) { $secretRefs[$e.name] = $e.secretRef; $settings[$e.name] = "secretref:$($e.secretRef)" }
        else { $settings[$e.name] = [string]$e.value }
    }
    $secretNames = @()
    if ($app.properties.configuration.PSObject.Properties['secrets'] -and $app.properties.configuration.secrets) {
        $secretNames = @($app.properties.configuration.secrets | ForEach-Object { $_.name })
    }
    $registry = $null
    if ($app.properties.configuration.PSObject.Properties['registries'] -and $app.properties.configuration.registries) {
        $registry = $app.properties.configuration.registries[0].server
    }
    $principal = $null
    if ($app.PSObject.Properties['identity'] -and $app.identity -and $app.identity.PSObject.Properties['principalId']) { $principal = $app.identity.principalId }
    $minReplicas = 0
    if ($app.properties.template.PSObject.Properties['scale'] -and $app.properties.template.scale -and $app.properties.template.scale.PSObject.Properties['minReplicas']) { $minReplicas = [int]$app.properties.template.scale.minReplicas }
    return @{ Image = $container.image; Settings = $settings; SecretRefs = $secretRefs; SecretNames = $secretNames; Registry = $registry; Identity = $principal; MinReplicas = $minReplicas }
}

function Get-KeyBasedForContainerApp {
    <# Per le container app i valori possono essere secretref: usa il nome della variabile. #>
    param([hashtable]$Settings, [hashtable]$SecretRefs)
    $found = @(Get-KeyBasedSettings -Settings $Settings)
    foreach ($k in $SecretRefs.Keys) {
        if ($k -match 'CONNECTION_STRING|ACCOUNT_KEY|REGISTRY_PASSWORD|CLIENT_SECRET' -and $found -notcontains $k) { $found += $k }
    }
    return [string[]]@($found | Sort-Object)
}

function Get-WebAppDetails {
    param([string]$Rg, [string]$Name, [bool]$IsFunction)
    $cmd = if ($IsFunction) { 'functionapp' } else { 'webapp' }
    $settings = Get-AppSettingsHashtable (Invoke-Az @($cmd, 'config', 'appsettings', 'list', '-g', $Rg, '-n', $Name))
    $identity = Invoke-Az @($cmd, 'identity', 'show', '-g', $Rg, '-n', $Name) -AllowFailure
    $principal = $null
    if ($identity -and $identity.PSObject.Properties['principalId']) { $principal = $identity.principalId }
    $mounts = @()
    $m = Invoke-Az @('webapp', 'config', 'storage-account', 'list', '-g', $Rg, '-n', $Name) -AllowFailure
    foreach ($x in @($m)) { if ($null -ne $x) { $mounts += [pscustomobject]@{ name = $x.name; accountName = $x.value.accountName; shareName = $x.value.shareName; mountPath = $x.value.mountPath; type = $x.value.type } } }
    $bindings = @()
    if ($IsFunction) {
        $fns = Invoke-Az @('functionapp', 'function', 'list', '-g', $Rg, '-n', $Name) -AllowFailure
        foreach ($f in @($fns)) { if ($null -ne $f) { $bindings += [pscustomobject]@{ name = $f.name; bindings = $f.config.bindings } } }
    }
    return @{ Settings = $settings; Identity = $principal; Mounts = $mounts; Functions = $bindings }
}

function Resolve-ResourceId {
    <#
    .SYNOPSIS
    Id ARM di storage account / namespace Service Bus / ACR cercando in tutta la subscription.
    #>
    param(
        [Parameter(Mandatory)][ValidateSet('storage', 'servicebus', 'acr')][string]$Type,
        [Parameter(Mandatory)][string]$Name
    )
    switch ($Type) {
        'storage' {
            $s = Invoke-Az @('storage', 'account', 'show', '-n', $Name) -AllowFailure
            if ($s) { return $s.id }
        }
        'servicebus' {
            $all = Invoke-Az @('servicebus', 'namespace', 'list')
            $m = @($all | Where-Object { $_.name -eq $Name })
            if ($m.Count -gt 0) { return $m[0].id }
        }
        'acr' {
            $a = Invoke-Az @('acr', 'show', '-n', $Name) -AllowFailure
            if ($a) { return $a.id }
        }
    }
    return $null
}

function Test-OperatorRole {
    <#
    .SYNOPSIS
    Vero se l'utente loggato ha Owner o User Access Administrator sullo scope (anche ereditato).
    #>
    param([Parameter(Mandatory)][string]$Scope)
    $me = Invoke-Az @('ad', 'signed-in-user', 'show') -AllowFailure
    $assignee = if ($me -and $me.PSObject.Properties['id']) { $me.id } else { (Invoke-Az @('account', 'show')).user.name }
    $assignments = Invoke-Az @('role', 'assignment', 'list', '--assignee', $assignee, '--scope', $Scope, '--include-inherited', '--include-groups') -AllowFailure
    foreach ($a in @($assignments)) {
        if ($null -ne $a -and $a.roleDefinitionName -in @('Owner', 'User Access Administrator')) { return $true }
    }
    return $false
}

function Get-PolisInventory {
    <#
    .SYNOPSIS
    Enumera il RG, legge settings/identity/mount di ogni servizio, mappa ai sorgenti e classifica
    storage (data/host), Service Bus, ACR, servizi non mappati e attesi ma assenti.
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][pscustomobject]$Mapping
    )
    $resources = @(Invoke-Az @('resource', 'list', '-g', $Rg))
    $services = @()
    $storageNames = @{}   # name -> 'data'|'host'
    $sbNamespaces = @{}
    $registryServers = @{}

    foreach ($r in $resources) {
        $kind = $null
        if ($r.type -eq 'Microsoft.App/containerApps') { $kind = 'containerApp' }
        elseif ($r.type -eq 'Microsoft.Web/sites') { $kind = if (([string]$r.kind) -match 'functionapp') { 'functionApp' } else { 'webApp' } }
        if (-not $kind) { continue }

        $svc = [pscustomobject]@{
            Kind = $kind; Name = $r.name; Id = $r.id; Image = $null; Settings = @{}; SecretRefs = @{}; SecretNames = @(); Registry = $null
            Identity = $null; Mounts = @(); Functions = @(); MinReplicas = 0
            Mapping = $null; KeyBased = $false; KeyBasedSettings = @(); Excluded = $false; Legacy = $false
        }
        if ($kind -eq 'containerApp') {
            $d = Get-ContainerAppDetails -Rg $Rg -Name $r.name
            $svc.Image = $d.Image; $svc.Settings = $d.Settings; $svc.SecretRefs = $d.SecretRefs; $svc.SecretNames = $d.SecretNames
            $svc.Registry = $d.Registry; $svc.Identity = $d.Identity; $svc.MinReplicas = $d.MinReplicas
            $svc.KeyBasedSettings = Get-KeyBasedForContainerApp -Settings $d.Settings -SecretRefs $d.SecretRefs
            if ($d.Registry) { $registryServers[$d.Registry] = $true }
        } else {
            $d = Get-WebAppDetails -Rg $Rg -Name $r.name -IsFunction ($kind -eq 'functionApp')
            $svc.Settings = $d.Settings; $svc.Identity = $d.Identity; $svc.Mounts = $d.Mounts; $svc.Functions = $d.Functions
            $svc.KeyBasedSettings = Get-KeyBasedSettings -Settings $d.Settings
            $svc.Excluded = ($kind -eq 'webApp') -and (Test-ExcludedWebApp -Name $r.name -Mapping $Mapping)
        }
        $svc.KeyBased = (@($svc.KeyBasedSettings | Where-Object { $_ }).Count -gt 0)
        $svc.Mapping = Resolve-ServiceMapping -Resource $svc -Mapping $Mapping
        if ($svc.Mapping -and $svc.Mapping.PSObject.Properties['legacy'] -and $svc.Mapping.legacy) { $svc.Legacy = $true }

        # storage / service bus referenziati
        foreach ($k in $svc.Settings.Keys) {
            $v = [string]$svc.Settings[$k]
            $parts = Get-ConnectionStringParts $v
            if ($parts.Kind -eq 'storage') {
                $role = if ($k -eq 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING') { 'host' } else { 'data' }
                if (-not $storageNames.ContainsKey($parts.AccountName) -or $role -eq 'data') { $storageNames[$parts.AccountName] = $role }
            } elseif ($parts.Kind -eq 'servicebus') { $sbNamespaces[$parts.Namespace] = $true }
        }
        if ($svc.Settings.ContainsKey('AZURE_ACCOUNT_NAME') -and $svc.Settings['AZURE_ACCOUNT_NAME']) { $storageNames[$svc.Settings['AZURE_ACCOUNT_NAME']] = 'data' }
        foreach ($m in $svc.Mounts) { if ($m.accountName -and -not $storageNames.ContainsKey($m.accountName)) { $storageNames[$m.accountName] = 'data' } }
        $services += $svc
    }

    # storage accounts
    $storageAccounts = @()
    foreach ($n in $storageNames.Keys) {
        $s = Invoke-Az @('storage', 'account', 'show', '-n', $n) -AllowFailure
        $allowShared = $true
        $id = $null; $location = $null; $rgName = $null
        if ($s) {
            $id = $s.id; $location = $s.location; $rgName = $s.resourceGroup
            if ($s.PSObject.Properties['allowSharedKeyAccess'] -and $null -ne $s.allowSharedKeyAccess) { $allowShared = [bool]$s.allowSharedKeyAccess }
        }
        $storageAccounts += [pscustomobject]@{ Name = $n; Id = $id; Role = $storageNames[$n]; AllowSharedKey = $allowShared; Location = $location; ResourceGroup = $rgName; Found = [bool]$s }
    }

    # service bus
    $serviceBus = $null
    $nsList = @(Invoke-Az @('servicebus', 'namespace', 'list'))
    $wanted = @($sbNamespaces.Keys)
    foreach ($ns in $nsList) {
        $fqdn = "$($ns.name).servicebus.windows.net"
        if (($wanted.Count -eq 0 -and $ns.id -like "*/resourceGroups/$Rg/*") -or ($wanted -contains $fqdn)) {
            $serviceBus = [pscustomobject]@{ Name = $ns.name; Id = $ns.id; Namespace = $fqdn; DisableLocalAuth = [bool]$ns.disableLocalAuth }
            break
        }
    }

    # acr
    $acr = $null
    $acrRes = @($resources | Where-Object { $_.type -eq 'Microsoft.ContainerRegistry/registries' })
    $acrName = if ($acrRes.Count -gt 0) { $acrRes[0].name } elseif ($registryServers.Keys.Count -gt 0) { (@($registryServers.Keys)[0] -split '\.')[0] } else { $null }
    if ($acrName) {
        $a = Invoke-Az @('acr', 'show', '-n', $acrName) -AllowFailure
        if ($a) { $acr = [pscustomobject]@{ Name = $a.name; Id = $a.id; LoginServer = $a.loginServer; AdminEnabled = [bool]$a.adminUserEnabled } }
    }

    # unmapped (key-based, non esclusi, senza mapping) e attesi-ma-assenti
    $unmapped = @($services | Where-Object { -not $_.Mapping -and -not $_.Excluded -and $_.KeyBased })
    $missing = @()
    foreach ($e in @($Mapping.webApps) + @($Mapping.functionApps)) {
        $found = @($services | Where-Object { $_.Name -imatch $e.match -and -not $_.Excluded })
        if ($found.Count -eq 0) { $missing += $e.match }
    }
    foreach ($e in @($Mapping.containerApps | Where-Object { -not $_.legacy })) {
        $found = @($services | Where-Object { $_.Mapping -and $_.Mapping.PSObject.Properties['image'] -and $_.Mapping.image -eq $e.image })
        if ($found.Count -eq 0) { $missing += $e.image }
    }

    return [pscustomobject]@{
        Rg = $Rg; Services = $services; StorageAccounts = $storageAccounts; ServiceBus = $serviceBus; Acr = $acr
        Unmapped = $unmapped; MissingExpected = [string[]]$missing
    }
}
