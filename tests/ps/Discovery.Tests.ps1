Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Az.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Mapping.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Settings.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Discovery.ps1"
    $script:Mapping = Get-Content "$PSScriptRoot\..\..\tools\mapping.json" -Raw -Encoding UTF8 | ConvertFrom-Json

    $script:DataCs = 'DefaultEndpointsProtocol=https;AccountName=polisprodstorage;AccountKey=RkFLRQ==;EndpointSuffix=core.windows.net'
    $script:HostCs = 'DefaultEndpointsProtocol=https;AccountName=polisprodhost;AccountKey=RkFLRQ==;EndpointSuffix=core.windows.net'
    $script:SbCs = 'Endpoint=sb://polis-messaging-prod.servicebus.windows.net/;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=RkFLRQ=='
    $script:Sub = '/subscriptions/00000000-0000-0000-0000-000000000000'
    $script:RgId = "$Sub/resourceGroups/rg-polis"

    function script:Res([string]$n, [string]$t, [string]$k) { [pscustomobject]@{ name = $n; type = $t; kind = $k; id = "$RgId/providers/$t/$n" } }
    $script:ResourceList = @(
        (Res 'polis-prod-import-app' 'Microsoft.App/containerApps' $null),
        (Res 'autogen-prod-pdf-import' 'Microsoft.App/containerApps' $null),
        (Res 'servicebus-bridge-prod' 'Microsoft.App/containerApps' $null),
        (Res 'sftp-processor-prod' 'Microsoft.App/containerApps' $null),
        (Res 'import-sal-orchestrator-prod' 'Microsoft.App/containerApps' $null),
        (Res 'mystery-prod' 'Microsoft.App/containerApps' $null),
        (Res 'polis-prod-sal-import' 'Microsoft.Web/sites' 'app,linux'),
        (Res 'polis-prod-api-storage' 'Microsoft.Web/sites' 'app,linux'),
        (Res 'polis-prod-sal-import-web' 'Microsoft.Web/sites' 'app,linux'),
        (Res 'polis-prod-api-database' 'Microsoft.Web/sites' 'app,linux'),
        (Res 'polis-prod-frontend' 'Microsoft.Web/sites' 'app,linux'),
        (Res 'polis-prod-sftp-upload' 'Microsoft.Web/sites' 'functionapp,linux'),
        (Res 'polisregistryprod' 'Microsoft.ContainerRegistry/registries' $null),
        (Res 'polis-messaging-prod' 'Microsoft.ServiceBus/namespaces' $null),
        (Res 'polis-env-prod' 'Microsoft.App/managedEnvironments' $null)
    )

    function script:CApp([string]$image, [hashtable]$env, [string[]]$secrets, [string]$principal) {
        $envList = @(); foreach ($k in $env.Keys) { $v = $env[$k]; if ($v -like 'secretref:*') { $envList += [pscustomobject]@{ name = $k; value = $null; secretRef = $v.Substring(10) } } else { $envList += [pscustomobject]@{ name = $k; value = $v; secretRef = $null } } }
        [pscustomobject]@{
            identity = [pscustomobject]@{ type = $(if ($principal) { 'SystemAssigned' } else { 'None' }); principalId = $principal }
            properties = [pscustomobject]@{
                template = [pscustomobject]@{ containers = @([pscustomobject]@{ image = $image; env = $envList }); scale = [pscustomobject]@{ minReplicas = 0 } }
                configuration = [pscustomobject]@{ secrets = @($secrets | ForEach-Object { [pscustomobject]@{ name = $_ } }); registries = @([pscustomobject]@{ server = 'polisregistryprod.azurecr.io'; identity = '' }) }
                latestRevisionName = 'rev--1'
            }
        }
    }
    $script:ContainerApps = @{
        'polis-prod-import-app'        = CApp 'polisregistryprod.azurecr.io/polis-prod-import:20260331' @{ AZURE_STORAGE_CONNECTION_STRING = $DataCs; AZURE_SERVICEBUS_CONNECTION_STRING = $SbCs; DB_HOST = 'h' } @('polisregistryprodazurecrio-polisregistryprod') $null
        'autogen-prod-pdf-import'      = CApp 'polisregistryprod.azurecr.io/autogen-prod-pdf-import:latest' @{ AZURE_STORAGE_CONNECTION_STRING = 'secretref:azure-storage-connection'; SERVICE_BUS_CONNECTION_STRING = 'secretref:service-bus-connection-string' } @('azure-storage-connection', 'service-bus-connection-string') $null
        'servicebus-bridge-prod'       = CApp 'polisregistryprod.azurecr.io/servicebus-bridge:v1' @{ SERVICE_BUS_CONNECTION_STRING = $SbCs } @() 'p-bridge'
        'sftp-processor-prod'          = CApp 'polisregistryprod.azurecr.io/sftp-service-prod:latest' @{ SERVICE_BUS_CONNECTION_STRING = $SbCs; AZURE_STORAGE_CONNECTION_STRING = $DataCs } @() $null
        'import-sal-orchestrator-prod' = CApp 'polisregistryprod.azurecr.io/import-sal-orchestrator-prod:latest' @{ SERVICE_BUS_CONNECTION_STRING = $SbCs; CONTAINER_REGISTRY_PASSWORD = 'secretref:container-registry-password' } @('container-registry-password') $null
        'mystery-prod'                 = CApp 'polisregistryprod.azurecr.io/mystery:1' @{ SERVICE_BUS_CONNECTION_STRING = $SbCs } @() $null
    }
    function script:Settings([hashtable]$h) { @($h.Keys | ForEach-Object { [pscustomobject]@{ name = $_; value = $h[$_]; slotSetting = $false } }) }
    $script:AppSettings = @{
        'polis-prod-sal-import'     = Settings @{ AZURE_ACCOUNT_NAME = 'polisprodstorage'; AZURE_ACCOUNT_KEY = 'RkFLRQ=='; SERVICE_BUS_CONNECTION_STRING = $SbCs; EMAIL_ACS_CONNECTION_STRING = 'endpoint=https://x;accesskey=y' }
        'polis-prod-api-storage'    = Settings @{ AZURE_STORAGE_CONNECTION_STRING = $DataCs; SERVICE_BUS_CONNECTION_STRING = $SbCs }
        'polis-prod-sal-import-web' = Settings @{ API_BASE_URL = 'https://x' }
        'polis-prod-api-database'   = Settings @{ SERVICE_BUS_CONNECTION_STRING = $SbCs; DB_HOST = 'h' }
        'polis-prod-frontend'       = Settings @{ SERVICE_BUS_CONNECTION_STRING = $SbCs }
        'polis-prod-sftp-upload'    = Settings @{ AzureWebJobsStorage = $DataCs; WEBSITE_CONTENTAZUREFILECONNECTIONSTRING = $HostCs; WEBSITE_CONTENTSHARE = 'polis-prod-sftp-uploadabc'; SERVICE_BUS_CONNECTION_STRING = $SbCs; FUNCTIONS_WORKER_RUNTIME = 'python' }
    }
    $script:Identities = @{ 'polis-prod-api-database' = 'p-db' }
    $script:Mounts = @{ 'polis-prod-sftp-upload' = @([pscustomobject]@{ name = 'data-mount'; value = [pscustomobject]@{ accountName = 'polisprodstorage'; shareName = 'function-data'; mountPath = '/data'; type = 'AzureFiles' } }) }
    $script:StorageAccounts = @{
        'polisprodstorage' = [pscustomobject]@{ name = 'polisprodstorage'; id = "$RgId/providers/Microsoft.Storage/storageAccounts/polisprodstorage"; allowSharedKeyAccess = $null; resourceGroup = 'rg-polis'; location = 'westeurope' }
        'polisprodhost'    = [pscustomobject]@{ name = 'polisprodhost'; id = "$Sub/resourceGroups/other-rg/providers/Microsoft.Storage/storageAccounts/polisprodhost"; allowSharedKeyAccess = $true; resourceGroup = 'other-rg'; location = 'westeurope' }
    }
    $script:RoleAssignments = @([pscustomobject]@{ roleDefinitionName = 'Owner'; scope = $Sub; principalId = 'u1' })

    function script:Dispatch([string[]]$Arguments) {
        $a = $Arguments -join ' '
        $n = $null; for ($i = 0; $i -lt $Arguments.Count; $i++) { if ($Arguments[$i] -in '-n', '--name') { $n = $Arguments[$i + 1] } }
        switch -Regex ($a) {
            '^resource list' { return $script:ResourceList }
            '^containerapp show' { return $script:ContainerApps[$n] }
            '^webapp config appsettings list|^functionapp config appsettings list' { return $script:AppSettings[$n] }
            '^webapp identity show|^functionapp identity show' { if ($script:Identities.ContainsKey($n)) { return [pscustomobject]@{ principalId = $script:Identities[$n]; type = 'SystemAssigned' } } else { return $null } }
            '^webapp config storage-account list' { if ($script:Mounts.ContainsKey($n)) { return $script:Mounts[$n] } else { return @() } }
            '^functionapp function list' { return @() }
            '^storage account show' { return $script:StorageAccounts[$n] }
            '^storage account list' { return @($script:StorageAccounts.Values) }
            '^servicebus namespace list' { return @([pscustomobject]@{ name = 'polis-messaging-prod'; id = "$RgId/providers/Microsoft.ServiceBus/namespaces/polis-messaging-prod"; disableLocalAuth = $false; serviceBusEndpoint = 'https://polis-messaging-prod.servicebus.windows.net:443/' }) }
            '^acr show' { return [pscustomobject]@{ name = 'polisregistryprod'; id = "$RgId/providers/Microsoft.ContainerRegistry/registries/polisregistryprod"; loginServer = 'polisregistryprod.azurecr.io'; adminUserEnabled = $true } }
            '^acr list' { return @([pscustomobject]@{ name = 'polisregistryprod'; id = "$RgId/providers/Microsoft.ContainerRegistry/registries/polisregistryprod"; loginServer = 'polisregistryprod.azurecr.io'; adminUserEnabled = $true }) }
            '^ad signed-in-user show' { return [pscustomobject]@{ id = 'u1'; userPrincipalName = 'op@x.it' } }
            '^role assignment list' { return $script:RoleAssignments }
            '^group show' { return [pscustomobject]@{ id = $RgId; name = 'rg-polis'; location = 'westeurope' } }
            default { throw "mock: comando az non previsto: $a" }
        }
    }
}

Describe 'Get-PolisInventory' {
    BeforeAll {
        Mock Invoke-Az { Dispatch $Arguments }
        $script:Inv = Get-PolisInventory -Rg 'rg-polis' -Mapping $Mapping
    }
    It 'maps the five in-use/legacy container apps and flags the unknown one' {
        $mapped = @($Inv.Services | Where-Object { $_.Kind -eq 'containerApp' -and $_.Mapping })
        $mapped.Count | Should -Be 5
        @($Inv.Unmapped | ForEach-Object { $_.Name }) | Should -Contain 'mystery-prod'
        ($Inv.Services | Where-Object Name -eq 'mystery-prod').KeyBased | Should -BeTrue
    }
    It 'reads env values and secret refs of container apps' {
        $imp = $Inv.Services | Where-Object Name -eq 'polis-prod-import-app'
        $imp.Settings['AZURE_STORAGE_CONNECTION_STRING'] | Should -Be $DataCs
        $imp.KeyBased | Should -BeTrue
        $auto = $Inv.Services | Where-Object Name -eq 'autogen-prod-pdf-import'
        $auto.SecretNames | Should -Contain 'service-bus-connection-string'
        $auto.KeyBased | Should -BeTrue
    }
    It 'marks legacy container apps' {
        ($Inv.Services | Where-Object Name -eq 'sftp-processor-prod').Legacy | Should -BeTrue
        ($Inv.Services | Where-Object Name -eq 'polis-prod-import-app').Legacy | Should -BeFalse
    }
    It 'classifies excluded web apps and keeps their key-based flag' {
        $st = $Inv.Services | Where-Object Name -eq 'polis-prod-api-storage'
        $st.Excluded | Should -BeTrue
        $st.KeyBased | Should -BeTrue
        ($Inv.Services | Where-Object Name -eq 'polis-prod-sal-import-web').Excluded | Should -BeTrue
        ($Inv.Services | Where-Object Name -eq 'polis-prod-sal-import-web').KeyBased | Should -BeFalse
    }
    It 'reads identity principal ids' {
        ($Inv.Services | Where-Object Name -eq 'polis-prod-api-database').Identity | Should -Be 'p-db'
        ($Inv.Services | Where-Object Name -eq 'servicebus-bridge-prod').Identity | Should -Be 'p-bridge'
        ($Inv.Services | Where-Object Name -eq 'polis-prod-sal-import').Identity | Should -BeNullOrEmpty
    }
    It 'detects function app mounts and kind' {
        $fn = $Inv.Services | Where-Object Name -eq 'polis-prod-sftp-upload'
        $fn.Kind | Should -Be 'functionApp'
        $fn.Mounts.Count | Should -Be 1
        $fn.Mounts[0].accountName | Should -Be 'polisprodstorage'
    }
    It 'classifies storage accounts as data or host' {
        ($Inv.StorageAccounts | Where-Object Name -eq 'polisprodstorage').Role | Should -Be 'data'
        ($Inv.StorageAccounts | Where-Object Name -eq 'polisprodhost').Role | Should -Be 'host'
        ($Inv.StorageAccounts | Where-Object Name -eq 'polisprodhost').Id | Should -Match 'other-rg'
    }
    It 'resolves service bus namespace and acr' {
        $Inv.ServiceBus.Namespace | Should -Be 'polis-messaging-prod.servicebus.windows.net'
        $Inv.ServiceBus.DisableLocalAuth | Should -BeFalse
        $Inv.Acr.Name | Should -Be 'polisregistryprod'
        $Inv.Acr.AdminEnabled | Should -BeTrue
    }
    It 'lists expected-but-missing services' {
        $Inv.MissingExpected | Should -Contain 'orchestrator-func'
        $Inv.MissingExpected | Should -Not -Contain 'sftp-upload'
    }
}

Describe 'Test-OperatorRole' {
    It 'is true with Owner inherited from the subscription' {
        Mock Invoke-Az { Dispatch $Arguments }
        Test-OperatorRole -Scope $RgId | Should -BeTrue
    }
    It 'is false with only Contributor' {
        Mock Invoke-Az {
            if (($Arguments -join ' ') -match '^role assignment list') { return @([pscustomobject]@{ roleDefinitionName = 'Contributor'; scope = $RgId; principalId = 'u1' }) }
            Dispatch $Arguments
        }
        Test-OperatorRole -Scope $RgId | Should -BeFalse
    }
    It 'is true with User Access Administrator' {
        Mock Invoke-Az {
            if (($Arguments -join ' ') -match '^role assignment list') { return @([pscustomobject]@{ roleDefinitionName = 'User Access Administrator'; scope = $RgId; principalId = 'u1' }) }
            Dispatch $Arguments
        }
        Test-OperatorRole -Scope $RgId | Should -BeTrue
    }
}

Describe 'Resolve-ResourceId' {
    It 'resolves storage, servicebus and acr across the subscription' {
        Mock Invoke-Az { Dispatch $Arguments }
        Resolve-ResourceId -Type 'storage' -Name 'polisprodhost' | Should -Match 'other-rg.*polisprodhost'
        Resolve-ResourceId -Type 'servicebus' -Name 'polis-messaging-prod' | Should -Match 'namespaces/polis-messaging-prod$'
        Resolve-ResourceId -Type 'acr' -Name 'polisregistryprod' | Should -Match 'registries/polisregistryprod$'
    }
}
