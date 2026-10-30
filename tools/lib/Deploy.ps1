Set-StrictMode -Version Latest

<#
Operazioni di build/deploy/settings. Dipende da Az.ps1 (Invoke-Az).
Nessun Docker locale: build in cloud con az acr build; web/function app via zip deploy.
#>

function Build-ContainerImage {
    <#
    .SYNOPSIS
    Build cloud dell'immagine (ACR Tasks). Ritorna il riferimento completo dell'immagine.
    #>
    param(
        [Parameter(Mandatory)][string]$Acr,
        [Parameter(Mandatory)][string]$Repository,
        [Parameter(Mandatory)][string]$Tag,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string]$Dockerfile,
        [string]$RepoRoot
    )
    $ctxPath = if ([IO.Path]::IsPathRooted($Context)) { $Context } elseif ($RepoRoot) { Join-Path $RepoRoot $Context } else { $Context }
    Invoke-Az @('acr', 'build', '--registry', $Acr, '--image', "${Repository}:${Tag}", '--file', $Dockerfile, $ctxPath) -Raw | Out-Null
    return "$Acr.azurecr.io/${Repository}:${Tag}"
}

function Update-ContainerAppImage {
    <#
    .SYNOPSIS
    Pull dall'ACR con la system identity della container app e aggiornamento dell'immagine.
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Image
    )
    $server = ($Image -split '/')[0]
    Invoke-Az @('containerapp', 'registry', 'set', '-g', $Rg, '-n', $Name, '--server', $server, '--identity', 'system') | Out-Null
    Invoke-Az @('containerapp', 'update', '-g', $Rg, '-n', $Name, '--image', $Image) | Out-Null
}

function Set-ContainerAppEnv {
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][string]$Name,
        [hashtable]$Add = @{},
        [string[]]$Remove = @(),
        [string[]]$RemoveSecrets = @()
    )
    $args = @('containerapp', 'update', '-g', $Rg, '-n', $Name)
    $changed = $false
    if ($Add.Count -gt 0) {
        $args += '--set-env-vars'
        foreach ($k in $Add.Keys) { $args += "$k=$($Add[$k])" }
        $changed = $true
    }
    $rem = @($Remove | Where-Object { $_ })
    if ($rem.Count -gt 0) { $args += '--remove-env-vars'; $args += $rem; $changed = $true }
    if ($changed) { Invoke-Az $args | Out-Null }
    $sec = @($RemoveSecrets | Where-Object { $_ })
    if ($sec.Count -gt 0) {
        Invoke-Az (@('containerapp', 'secret', 'remove', '-g', $Rg, '-n', $Name, '--secret-names') + $sec) -AllowFailure | Out-Null
    }
}

function Set-WebAppSettings {
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][string]$Name,
        [hashtable]$Add = @{},
        [string[]]$Remove = @(),
        [switch]$FunctionApp
    )
    $cmd = if ($FunctionApp) { 'functionapp' } else { 'webapp' }
    if ($Add.Count -gt 0) {
        $args = @($cmd, 'config', 'appsettings', 'set', '-g', $Rg, '-n', $Name, '--settings')
        foreach ($k in $Add.Keys) { $args += "$k=$($Add[$k])" }
        Invoke-Az $args | Out-Null
    }
    $rem = @($Remove | Where-Object { $_ })
    if ($rem.Count -gt 0) {
        Invoke-Az (@($cmd, 'config', 'appsettings', 'delete', '-g', $Rg, '-n', $Name, '--setting-names') + $rem) | Out-Null
    }
}

function New-DeployZip {
    param([Parameter(Mandatory)][string]$Context, [Parameter(Mandatory)][string]$ZipPath)
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
    $excludeDirs = @('__pycache__', 'tests', 'test', '.venv', 'venv', '.git', '.idea', '.pytest_cache', 'logs')
    $zip = [IO.Compression.ZipFile]::Open($ZipPath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        $root = (Resolve-Path $Context).Path.TrimEnd('\')
        Get-ChildItem $root -Recurse -File | ForEach-Object {
            $rel = $_.FullName.Substring($root.Length + 1)
            $parts = $rel -split '[\\/]'
            $skip = $false
            foreach ($d in $parts[0..([Math]::Max(0, $parts.Count - 2))]) { if ($excludeDirs -contains $d) { $skip = $true } }
            if ($_.Extension -in '.pyc', '.log', '.zip') { $skip = $true }
            if ($_.Name -like '.env*' -or $_.Name -in '.prodenv', '.colenv', 'local.settings.json') { $skip = $true }
            if (-not $skip) { [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $_.FullName, ($rel -replace '\\', '/')) | Out-Null }
        }
    } finally { $zip.Dispose() }
    return $ZipPath
}

function Publish-FunctionZip {
    <#
    .SYNOPSIS
    Zip del contesto (senza cache/test/segreti) e deploy con build remota (Oryx).
    Ritorna il path dello zip.
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][string]$WorkDir
    )
    if (-not (Test-Path $WorkDir)) { New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null }
    $zip = New-DeployZip -Context $Context -ZipPath (Join-Path $WorkDir "$Name.zip")
    Invoke-Az @('functionapp', 'config', 'appsettings', 'set', '-g', $Rg, '-n', $Name, '--settings', 'SCM_DO_BUILD_DURING_DEPLOYMENT=true', 'ENABLE_ORYX_BUILD=true') | Out-Null
    Invoke-Az @('functionapp', 'deployment', 'source', 'config-zip', '-g', $Rg, '-n', $Name, '--src', $zip, '--build-remote', 'true', '--timeout', '900') | Out-Null
    return $zip
}

function Test-WebAppDeployedCommit {
    <#
    .SYNOPSIS
    Vero se tra i deployment della web app ne esiste uno il cui id/message contiene lo SHA (anche abbreviato).
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Sha
    )
    if ([string]::IsNullOrWhiteSpace($Sha)) { return $false }
    $list = Invoke-Az @('webapp', 'log', 'deployment', 'list', '-g', $Rg, '-n', $Name) -AllowFailure
    foreach ($d in @($list)) {
        if ($null -eq $d) { continue }
        $id = if ($d.PSObject.Properties['id']) { [string]$d.id } else { '' }
        $msg = if ($d.PSObject.Properties['message']) { [string]$d.message } else { '' }
        if ($id.StartsWith($Sha, [StringComparison]::OrdinalIgnoreCase) -or $msg -match [regex]::Escape($Sha)) { return $true }
    }
    return $false
}

function Ensure-HostStorageAccount {
    <#
    .SYNOPSIS
    Storage account "host" per la Function App (content share / mount Azure Files, key-based per limite Azure).
    Riusa un account host gia' distinto dai dati, altrimenti ne crea uno (polisprodfunchost[+4 random]).
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][string]$Location,
        [Parameter(Mandatory)][pscustomobject]$Inventory,
        [string]$BaseName = 'polisprodfunchost'
    )
    $dataNames = @($Inventory.StorageAccounts | Where-Object { $_.Role -eq 'data' } | ForEach-Object { $_.Name })
    $hostAccounts = @($Inventory.StorageAccounts | Where-Object { $_.Role -eq 'host' -and ($dataNames -notcontains $_.Name) })
    if ($hostAccounts.Count -gt 0) { return $hostAccounts[0].Name }

    $name = $BaseName
    $check = Invoke-Az @('storage', 'account', 'check-name', '-n', $name)
    if (-not ($check -and $check.nameAvailable)) {
        $suffix = -join ((97..122) + (48..57) | Get-Random -Count 4 | ForEach-Object { [char]$_ })
        $name = "$BaseName$suffix"
        $check = Invoke-Az @('storage', 'account', 'check-name', '-n', $name)
        if (-not ($check -and $check.nameAvailable)) { throw "Nome storage account non disponibile: $name" }
    }
    Invoke-Az @('storage', 'account', 'create', '-n', $name, '-g', $Rg, '-l', $Location, '--sku', 'Standard_LRS', '--kind', 'StorageV2',
        '--min-tls-version', 'TLS1_2', '--allow-blob-public-access', 'false', '--https-only', 'true') | Out-Null
    return $name
}

function Get-StorageAccountKey {
    param([Parameter(Mandatory)][string]$Account, [string]$Rg)
    $args = @('storage', 'account', 'keys', 'list', '-n', $Account)
    if ($Rg) { $args += @('-g', $Rg) }
    $keys = Invoke-Az $args
    if ($null -eq $keys) { return $null }
    return (@($keys)[0]).value
}

function New-StorageConnectionString {
    param([string]$Account, [string]$Key)
    return "DefaultEndpointsProtocol=https;AccountName=$Account;AccountKey=$Key;EndpointSuffix=core.windows.net"
}

function Move-FunctionShares {
    <#
    .SYNOPSIS
    Sposta content share e mount Azure Files della Function App sullo storage host:
    crea le share, ripunta WEBSITE_CONTENTAZUREFILECONNECTIONSTRING e i mount.
    I file del mount vengono copiati con az storage file copy (best effort).
    #>
    param(
        [Parameter(Mandatory)][string]$Rg,
        [Parameter(Mandatory)][pscustomobject]$FunctionService,
        [Parameter(Mandatory)][string]$HostAccount
    )
    $hostInfo = Invoke-Az @('storage', 'account', 'show', '-n', $HostAccount) -AllowFailure
    $hostRg = if ($hostInfo -and $hostInfo.PSObject.Properties['resourceGroup']) { $hostInfo.resourceGroup } else { $Rg }
    $hostKey = Get-StorageAccountKey -Account $HostAccount -Rg $hostRg
    if (-not $hostKey) { $hostKey = '' }   # WhatIf
    $settings = $FunctionService.Settings

    # content share
    $share = if ($settings.ContainsKey('WEBSITE_CONTENTSHARE') -and $settings['WEBSITE_CONTENTSHARE']) { $settings['WEBSITE_CONTENTSHARE'] } else { ($FunctionService.Name.ToLowerInvariant() + '-content') }
    Invoke-Az @('storage', 'share-rm', 'create', '-g', $hostRg, '--storage-account', $HostAccount, '-n', $share, '--quota', '5120') -AllowFailure | Out-Null
    $cs = New-StorageConnectionString -Account $HostAccount -Key $hostKey
    Invoke-Az @('functionapp', 'config', 'appsettings', 'set', '-g', $Rg, '-n', $FunctionService.Name, '--settings', "WEBSITE_CONTENTAZUREFILECONNECTIONSTRING=$cs", "WEBSITE_CONTENTSHARE=$share") | Out-Null

    # mount Azure Files
    foreach ($m in @($FunctionService.Mounts)) {
        if ($null -eq $m -or $m.type -ne 'AzureFiles') { continue }
        if ($m.accountName -eq $HostAccount) { continue }
        Invoke-Az @('storage', 'share-rm', 'create', '-g', $hostRg, '--storage-account', $HostAccount, '-n', $m.shareName, '--quota', '5120') -AllowFailure | Out-Null
        $srcKey = Get-StorageAccountKey -Account $m.accountName
        if ($srcKey) {
            Invoke-Az @('storage', 'file', 'copy', 'start-batch', '--destination-share', $m.shareName, '--account-name', $HostAccount, '--account-key', $hostKey,
                '--source-account-name', $m.accountName, '--source-account-key', $srcKey, '--source-share', $m.shareName) -AllowFailure | Out-Null
        }
        Invoke-Az @('webapp', 'config', 'storage-account', 'update', '-g', $Rg, '-n', $FunctionService.Name, '--custom-id', $m.name, '--storage-type', 'AzureFiles',
            '--account-name', $HostAccount, '--share-name', $m.shareName, '--access-key', $hostKey, '--mount-path', $m.mountPath) | Out-Null
    }
}
