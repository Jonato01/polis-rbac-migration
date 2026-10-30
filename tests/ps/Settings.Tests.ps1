Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Settings.ps1"
    $script:StorageCs = 'DefaultEndpointsProtocol=https;AccountName=polisprodstorage;AccountKey=RkFLRQ==;EndpointSuffix=core.windows.net'
    $script:SbCs = 'Endpoint=sb://polis-messaging-prod.servicebus.windows.net/;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=RkFLRQ=='
    $script:Resolved = @{ StorageAccount = 'polisprodstorage'; ServiceBusNamespace = 'polis-messaging-prod.servicebus.windows.net'; HostStorageAccount = 'polisprodfunchost' }
}

Describe 'Get-ConnectionStringParts' {
    It 'parses a storage connection string' {
        $p = Get-ConnectionStringParts $StorageCs
        $p.Kind | Should -Be 'storage'
        $p.AccountName | Should -Be 'polisprodstorage'
    }
    It 'parses a service bus connection string' {
        $p = Get-ConnectionStringParts $SbCs
        $p.Kind | Should -Be 'servicebus'
        $p.Namespace | Should -Be 'polis-messaging-prod.servicebus.windows.net'
    }
    It 'ignores EntityPath' {
        $p = Get-ConnectionStringParts ($SbCs + ';EntityPath=import-pdf')
        $p.Namespace | Should -Be 'polis-messaging-prod.servicebus.windows.net'
    }
    It 'handles endpoint without trailing slash' {
        $p = Get-ConnectionStringParts 'Endpoint=sb://ns1.servicebus.windows.net;SharedAccessKeyName=k;SharedAccessKey=v'
        $p.Namespace | Should -Be 'ns1.servicebus.windows.net'
    }
    It 'returns unknown for empty or unrelated strings' {
        (Get-ConnectionStringParts '').Kind | Should -Be 'unknown'
        (Get-ConnectionStringParts 'mysql://x').Kind | Should -Be 'unknown'
    }
}

Describe 'ConvertTo-IdentitySettings' {
    It 'import_fatture: adds identity vars and removes both connection strings' {
        $entry = [pscustomobject]@{
            add = [pscustomobject]@{ AZURE_STORAGE_ACCOUNT_NAME = '{storage}'; AZURE_SERVICEBUS_NAMESPACE = '{servicebus}' }
            remove = @('AZURE_STORAGE_CONNECTION_STRING', 'AZURE_SERVICEBUS_CONNECTION_STRING')
        }
        $current = @{ AZURE_STORAGE_CONNECTION_STRING = $StorageCs; AZURE_SERVICEBUS_CONNECTION_STRING = $SbCs; DB_HOST = 'h' }
        $r = ConvertTo-IdentitySettings -Current $current -Entry $entry -Resolved $Resolved
        $r.Add['AZURE_STORAGE_ACCOUNT_NAME'] | Should -Be 'polisprodstorage'
        $r.Add['AZURE_SERVICEBUS_NAMESPACE'] | Should -Be 'polis-messaging-prod.servicebus.windows.net'
        $r.Remove | Should -Be @('AZURE_STORAGE_CONNECTION_STRING', 'AZURE_SERVICEBUS_CONNECTION_STRING')
    }
    It 'function app: uses host storage for AzureWebJobsStorage__accountName' {
        $entry = [pscustomobject]@{
            add = [pscustomobject]@{ 'AzureWebJobsStorage__accountName' = '{hostStorage}'; DATA_STORAGE_ACCOUNT_NAME = '{storage}'; SERVICE_BUS_NAMESPACE = '{servicebus}' }
            remove = @('AzureWebJobsStorage', 'SERVICE_BUS_CONNECTION_STRING')
        }
        $current = @{ AzureWebJobsStorage = $StorageCs; SERVICE_BUS_CONNECTION_STRING = $SbCs }
        $r = ConvertTo-IdentitySettings -Current $current -Entry $entry -Resolved $Resolved
        $r.Add['AzureWebJobsStorage__accountName'] | Should -Be 'polisprodfunchost'
        $r.Add['DATA_STORAGE_ACCOUNT_NAME'] | Should -Be 'polisprodstorage'
        $r.Remove | Should -Contain 'AzureWebJobsStorage'
    }
    It 'sal-sftp-api: copies current AZURE_ACCOUNT_NAME and removes the key' {
        $entry = [pscustomobject]@{
            add = [pscustomobject]@{ AZURE_STORAGE_ACCOUNT_NAME = '{currentSetting:AZURE_ACCOUNT_NAME}'; SERVICE_BUS_NAMESPACE = '{servicebus}' }
            remove = @('AZURE_ACCOUNT_KEY', 'SERVICE_BUS_CONNECTION_STRING')
        }
        $current = @{ AZURE_ACCOUNT_NAME = 'polisprodstorage'; AZURE_ACCOUNT_KEY = 'RkFLRQ=='; SERVICE_BUS_CONNECTION_STRING = $SbCs }
        $r = ConvertTo-IdentitySettings -Current $current -Entry $entry -Resolved $Resolved
        $r.Add['AZURE_STORAGE_ACCOUNT_NAME'] | Should -Be 'polisprodstorage'
        $r.Remove | Should -Contain 'AZURE_ACCOUNT_KEY'
    }
    It 'only removes keys that exist in the current settings' {
        $entry = [pscustomobject]@{ add = [pscustomobject]@{}; remove = @('SERVICE_BUS_CONNECTION_STRING', 'NOT_THERE') }
        $r = ConvertTo-IdentitySettings -Current @{ SERVICE_BUS_CONNECTION_STRING = $SbCs } -Entry $entry -Resolved $Resolved
        $r.Remove | Should -Be @('SERVICE_BUS_CONNECTION_STRING')
    }
    It 'throws when a placeholder cannot be resolved' {
        $entry = [pscustomobject]@{ add = [pscustomobject]@{ X = '{hostStorage}' }; remove = @() }
        { ConvertTo-IdentitySettings -Current @{} -Entry $entry -Resolved @{ StorageAccount = 's' } } | Should -Throw
    }
}

Describe 'Get-KeyBasedSettings' {
    It 'finds connection strings, account keys and service principal secrets' {
        $s = @{ AZURE_STORAGE_CONNECTION_STRING = $StorageCs; SERVICE_BUS_CONNECTION_STRING = $SbCs; AZURE_ACCOUNT_KEY = 'RkFLRQ=='; AZURE_CLIENT_SECRET = 'x'; DB_HOST = 'h'; EMAIL_ACS_CONNECTION_STRING = 'endpoint=https://x;accesskey=y' }
        $found = Get-KeyBasedSettings -Settings $s
        $found | Should -Contain 'AZURE_STORAGE_CONNECTION_STRING'
        $found | Should -Contain 'SERVICE_BUS_CONNECTION_STRING'
        $found | Should -Contain 'AZURE_ACCOUNT_KEY'
        $found | Should -Contain 'AZURE_CLIENT_SECRET'
        $found | Should -Not -Contain 'DB_HOST'
        $found | Should -Not -Contain 'EMAIL_ACS_CONNECTION_STRING'
    }
    It 'treats AzureWebJobsStorage with a key as key-based but __accountName as identity' {
        (Get-KeyBasedSettings -Settings @{ AzureWebJobsStorage = $StorageCs }) | Should -Contain 'AzureWebJobsStorage'
        (Get-KeyBasedSettings -Settings @{ 'AzureWebJobsStorage__accountName' = 'acct' }) | Should -BeNullOrEmpty
    }
}
