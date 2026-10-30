Set-StrictMode -Version Latest

<#
Funzioni pure sulle app settings / env dei servizi: parsing connection string,
calcolo delle modifiche identity, rilevamento settings key-based.
#>

function Get-ConnectionStringParts {
    <#
    .SYNOPSIS
    Estrae AccountName (storage) o Namespace FQDN (Service Bus) da una connection string.
    #>
    param([string]$Cs)
    $result = @{ AccountName = $null; Namespace = $null; Kind = 'unknown' }
    if ([string]::IsNullOrWhiteSpace($Cs)) { return $result }
    $parts = @{}
    foreach ($seg in ($Cs -split ';')) {
        $eq = $seg.IndexOf('=')
        if ($eq -gt 0) { $parts[$seg.Substring(0, $eq).Trim()] = $seg.Substring($eq + 1).Trim() }
    }
    if ($parts.ContainsKey('AccountName') -and ($parts.ContainsKey('AccountKey') -or $parts.ContainsKey('DefaultEndpointsProtocol') -or $parts.ContainsKey('SharedAccessSignature'))) {
        $result.Kind = 'storage'
        $result.AccountName = $parts['AccountName']
        return $result
    }
    if ($parts.ContainsKey('Endpoint') -and $parts['Endpoint'] -match '^sb://([^/;]+)') {
        $result.Kind = 'servicebus'
        $result.Namespace = $Matches[1]
        return $result
    }
    return $result
}

function Resolve-SettingPlaceholder {
    param(
        [string]$Value,
        [hashtable]$Current,
        [hashtable]$Resolved
    )
    switch -Regex ($Value) {
        '^\{storage\}$' {
            if (-not $Resolved.ContainsKey('StorageAccount') -or -not $Resolved['StorageAccount']) { throw 'Placeholder {storage} non risolvibile: StorageAccount assente' }
            return $Resolved['StorageAccount']
        }
        '^\{servicebus\}$' {
            if (-not $Resolved.ContainsKey('ServiceBusNamespace') -or -not $Resolved['ServiceBusNamespace']) { throw 'Placeholder {servicebus} non risolvibile: ServiceBusNamespace assente' }
            return $Resolved['ServiceBusNamespace']
        }
        '^\{hostStorage\}$' {
            if (-not $Resolved.ContainsKey('HostStorageAccount') -or -not $Resolved['HostStorageAccount']) { throw 'Placeholder {hostStorage} non risolvibile: HostStorageAccount assente' }
            return $Resolved['HostStorageAccount']
        }
        '^\{currentSetting:([^}]+)\}$' {
            $key = $Matches[1]
            if (-not $Current.ContainsKey($key) -or [string]::IsNullOrEmpty($Current[$key])) { throw "Placeholder {currentSetting:$key} non risolvibile: setting assente" }
            return $Current[$key]
        }
        default { return $Value }
    }
}

function ConvertTo-IdentitySettings {
    <#
    .SYNOPSIS
    Calcola le settings da aggiungere e da rimuovere per portare un servizio a identity.
    .OUTPUTS
    @{ Add = [hashtable]; Remove = [string[]] }
    #>
    param(
        [Parameter(Mandatory)][hashtable]$Current,
        [Parameter(Mandatory)][pscustomobject]$Entry,
        [Parameter(Mandatory)][hashtable]$Resolved
    )
    $add = @{}
    if ($Entry.PSObject.Properties['add'] -and $null -ne $Entry.add) {
        foreach ($p in $Entry.add.PSObject.Properties) {
            $add[$p.Name] = Resolve-SettingPlaceholder -Value ([string]$p.Value) -Current $Current -Resolved $Resolved
        }
    }
    $remove = @()
    if ($Entry.PSObject.Properties['remove'] -and $null -ne $Entry.remove) {
        foreach ($k in @($Entry.remove)) {
            if ($Current.ContainsKey($k)) { $remove += $k }
        }
    }
    return @{ Add = $add; Remove = [string[]]$remove }
}

function Get-KeyBasedSettings {
    <#
    .SYNOPSIS
    Nomi delle settings che contengono credenziali storage/Service Bus o secret di service principal.
    Le connection string di altri servizi (es. ACS email) non rientrano nel perimetro.
    #>
    param([Parameter(Mandatory)][hashtable]$Settings)
    $found = @()
    foreach ($k in $Settings.Keys) {
        $v = [string]$Settings[$k]
        if ($k -eq 'AZURE_CLIENT_SECRET' -or $k -eq 'AZURE_ACCOUNT_KEY') { $found += $k; continue }
        if ($k -match '__accountName$|__fullyQualifiedNamespace$') { continue }
        if ($v -match 'AccountKey=' -or $v -match 'SharedAccessKey=' -or $v -match 'SharedAccessSignature=') { $found += $k; continue }
    }
    return [string[]]($found | Sort-Object)
}
