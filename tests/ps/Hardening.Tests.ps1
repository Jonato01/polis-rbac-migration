Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Az.ps1"
    . "$PSScriptRoot\..\..\tools\lib\State.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Deploy.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Hardening.ps1"
    $script:Tmp = Join-Path $env:TEMP ("hard-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Tmp -Force | Out-Null
    $script:Calls = [System.Collections.ArrayList]::new()
    function script:Record($Arguments) { [void]$script:Calls.Add(($Arguments -join ' ')) }
    $script:Sub = '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg'
    function script:NewInv([object[]]$services, [object[]]$unmapped) {
        [pscustomobject]@{
            Rg = 'rg'; Services = $services; Unmapped = $unmapped
            StorageAccounts = @(
                [pscustomobject]@{ Name = 'polisprodstorage'; Id = "$Sub/providers/Microsoft.Storage/storageAccounts/polisprodstorage"; Role = 'data'; ResourceGroup = 'rg'; AllowSharedKey = $true },
                [pscustomobject]@{ Name = 'polisprodfunchost'; Id = "$Sub/providers/Microsoft.Storage/storageAccounts/polisprodfunchost"; Role = 'host'; ResourceGroup = 'rg'; AllowSharedKey = $true })
            ServiceBus = [pscustomobject]@{ Name = 'polis-messaging-prod'; Id = "$Sub/providers/Microsoft.ServiceBus/namespaces/polis-messaging-prod"; DisableLocalAuth = $false }
            Acr = [pscustomobject]@{ Name = 'polisregistryprod'; Id = "$Sub/providers/Microsoft.ContainerRegistry/registries/polisregistryprod"; AdminEnabled = $true }
            MissingExpected = @()
        }
    }
    $script:FnMapping = [pscustomobject]@{ match = 'sftp-upload'; hostStorage = $true }
    $script:Fn = [pscustomobject]@{ Kind = 'functionApp'; Name = 'fn'; Mapping = $FnMapping; Excluded = $false; KeyBased = $false
        Settings = @{ WEBSITE_CONTENTSHARE = 'fn-content'; WEBSITE_CONTENTAZUREFILECONNECTIONSTRING = 'DefaultEndpointsProtocol=https;AccountName=polisprodfunchost;AccountKey=OLD==;EndpointSuffix=core.windows.net' }
        Mounts = @([pscustomobject]@{ name = 'data-mount'; accountName = 'polisprodfunchost'; shareName = 'function-data'; mountPath = '/data'; type = 'AzureFiles' }) }
    $script:Web = [pscustomobject]@{ Kind = 'webApp'; Name = 'web'; Mapping = [pscustomobject]@{ repo = 'x' }; Excluded = $false; KeyBased = $true; Settings = @{ A = '1'; B = '2' }; Mounts = @() }
    $script:Storage = [pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-api-storage'; Mapping = $null; Excluded = $true; KeyBased = $true; Settings = @{}; Mounts = @() }
}
AfterAll { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }

Describe 'Save-ServiceBackup / Restore-ServiceBackup' {
    It 'writes a json backup and does not overwrite an existing one' {
        $p = Save-ServiceBackup -Service $Web -WorkDir $Tmp
        Test-Path $p | Should -BeTrue
        $first = Get-Content $p -Raw
        $Web.Settings['A'] = 'changed'
        Save-ServiceBackup -Service $Web -WorkDir $Tmp | Should -Be $p
        (Get-Content $p -Raw) | Should -Be $first
        $Web.Settings['A'] = '1'
    }
    It 'restores web app settings from the backup' {
        $script:Calls.Clear()
        Mock Invoke-Az { param($Arguments) Record $Arguments; return $null }
        $p = Save-ServiceBackup -Service $Web -WorkDir (Join-Path $Tmp 'r')
        Restore-ServiceBackup -Path $p -Rg 'rg'
        @($script:Calls | Where-Object { $_ -match '^webapp config appsettings set -g rg -n web --settings (A=1 B=2|B=2 A=1)$' }).Count | Should -Be 1
    }
}

Describe 'Test-HardeningAllowed' {
    It 'denies when an unmapped key-based service exists and names it' {
        $m = [pscustomobject]@{ Kind = 'containerApp'; Name = 'mystery-prod'; Mapping = $null; Excluded = $false; KeyBased = $true; Settings = @{}; Mounts = @() }
        $inv = NewInv @($Web, $m) @($m)
        $r = Test-HardeningAllowed -Inventory $inv -State (New-RunState -WorkDir (Join-Path $Tmp 'a')) -Force $false
        $r.Allowed | Should -BeFalse
        ($r.Reasons -join ' ') | Should -Match 'mystery-prod'
    }
    It 'denies when polis-prod-api-storage is still key-based' {
        $inv = NewInv @($Web, $Storage) @()
        $r = Test-HardeningAllowed -Inventory $inv -State (New-RunState -WorkDir (Join-Path $Tmp 'b')) -Force $false
        $r.Allowed | Should -BeFalse
        ($r.Reasons -join ' ') | Should -Match 'api-storage'
    }
    It 'denies when a phase 07 step failed' {
        $inv = NewInv @($Web) @()
        $s = New-RunState -WorkDir (Join-Path $Tmp 'c')
        Set-StepStatus -State $s -Phase '07' -Service 'web' -Status 'failed' -Detail 'HTTP 503'
        $r = Test-HardeningAllowed -Inventory $inv -State $s -Force $false
        $r.Allowed | Should -BeFalse
        ($r.Reasons -join ' ') | Should -Match 'web'
    }
    It 'allows when everything is green' {
        $inv = NewInv @($Web) @()
        $s = New-RunState -WorkDir (Join-Path $Tmp 'd')
        Set-StepStatus -State $s -Phase '07' -Service 'web' -Status 'done'
        (Test-HardeningAllowed -Inventory $inv -State $s -Force $false).Allowed | Should -BeTrue
    }
    It 'allows with -Force but keeps the reasons' {
        $inv = NewInv @($Web, $Storage) @()
        $r = Test-HardeningAllowed -Inventory $inv -State (New-RunState -WorkDir (Join-Path $Tmp 'e')) -Force $true
        $r.Allowed | Should -BeTrue
        $r.Reasons.Count | Should -BeGreaterThan 0
    }
}

Describe 'Invoke-Hardening' {
    It 'disables shared key on data storage only, local auth on service bus, acr admin, renews keys and re-applies the host key' {
        $script:Calls.Clear()
        Mock Invoke-Az { param($Arguments) Record $Arguments; $a = $Arguments -join ' '
            if ($a -match '^storage account keys renew') { return @([pscustomobject]@{ keyName = 'key1'; value = 'NEW1' }, [pscustomobject]@{ keyName = 'key2'; value = 'NEW2' }) }
            if ($a -match '^storage account keys list') { return @([pscustomobject]@{ keyName = 'key1'; value = 'NEW1' }) }
            return $null }
        $inv = NewInv @($Fn, $Web) @()
        Invoke-Hardening -Inventory $inv
        @($script:Calls | Where-Object { $_ -match '^storage account update -n polisprodstorage -g rg --allow-shared-key-access false' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^storage account update -n polisprodfunchost .*--allow-shared-key-access false' }).Count | Should -Be 0
        @($script:Calls | Where-Object { $_ -match '^servicebus namespace update -g rg -n polis-messaging-prod --disable-local-auth true' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^acr update -n polisregistryprod --admin-enabled false' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^storage account keys renew -n polisprodstorage -g rg --key (key1|key2)' }).Count | Should -Be 2
        @($script:Calls | Where-Object { $_ -match '^storage account keys renew -n polisprodfunchost -g rg --key (key1|key2)' }).Count | Should -Be 2
        @($script:Calls | Where-Object { $_ -match '^functionapp config appsettings set -g rg -n fn --settings WEBSITE_CONTENTAZUREFILECONNECTIONSTRING=.*AccountName=polisprodfunchost;AccountKey=NEW1' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^webapp config storage-account update -g rg -n fn --custom-id data-mount .*--access-key NEW1' }).Count | Should -Be 1
    }
    It 'renews host keys after the function settings were read, never before data lockdown fails' {
        $script:Calls.Clear()
        Mock Invoke-Az { param($Arguments) Record $Arguments; if (($Arguments -join ' ') -match '^storage account keys') { return @([pscustomobject]@{ keyName = 'key1'; value = 'N' }) }; return $null }
        Invoke-Hardening -Inventory (NewInv @($Fn) @())
        $renew = ($script:Calls | Select-String -Pattern '^storage account keys renew -n polisprodfunchost' | Select-Object -First 1).LineNumber
        $reapply = ($script:Calls | Select-String -Pattern '^functionapp config appsettings set -g rg -n fn' | Select-Object -First 1).LineNumber
        $reapply | Should -BeGreaterThan $renew
    }
}
