Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Az.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Deploy.ps1"
    $script:RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
    $script:Calls = [System.Collections.ArrayList]::new()
    function script:Record($Arguments) { [void]$script:Calls.Add(($Arguments -join ' ')) }
}

Describe 'Build-ContainerImage' {
    It 'runs az acr build with image, dockerfile and context' {
        $script:Calls.Clear()
        Mock Invoke-Az { Record $Arguments; return 'Run ID: abc' }
        $img = Build-ContainerImage -Acr 'polisregistryprod' -Repository 'polis-import' -Tag '20261030-120000' -Context 'src/import_fatture' -Dockerfile 'container_app/Dockerfile' -RepoRoot $RepoRoot
        $img | Should -Be 'polisregistryprod.azurecr.io/polis-import:20261030-120000'
        $script:Calls[0] | Should -Match '^acr build --registry polisregistryprod --image polis-import:20261030-120000 --file container_app/Dockerfile .*src\\import_fatture$'
    }
}

Describe 'Update-ContainerAppImage' {
    It 'sets registry identity to system and updates the image' {
        $script:Calls.Clear()
        Mock Invoke-Az { Record $Arguments; return $null }
        Update-ContainerAppImage -Rg 'rg' -Name 'app' -Image 'reg.azurecr.io/polis-import:1'
        @($script:Calls | Where-Object { $_ -match '^containerapp registry set -g rg -n app --server reg.azurecr.io --identity system' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^containerapp update -g rg -n app --image reg.azurecr.io/polis-import:1' }).Count | Should -Be 1
    }
}

Describe 'Set-ContainerAppEnv' {
    It 'renders set/remove env vars and removes secrets' {
        $script:Calls.Clear()
        Mock Invoke-Az { Record $Arguments; return $null }
        Set-ContainerAppEnv -Rg 'rg' -Name 'app' -Add @{ X = 'y'; SERVICE_BUS_NAMESPACE = 'ns' } -Remove @('A', 'B') -RemoveSecrets @('s1')
        @($script:Calls | Where-Object { $_ -match '^containerapp update -g rg -n app --set-env-vars (X=y SERVICE_BUS_NAMESPACE=ns|SERVICE_BUS_NAMESPACE=ns X=y) --remove-env-vars A B$' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^containerapp secret remove -g rg -n app --secret-names s1$' }).Count | Should -Be 1
    }
    It 'skips the update when nothing changes' {
        $script:Calls.Clear()
        Mock Invoke-Az { Record $Arguments; return $null }
        Set-ContainerAppEnv -Rg 'rg' -Name 'app' -Add @{} -Remove @() -RemoveSecrets @()
        $script:Calls.Count | Should -Be 0
    }
}

Describe 'Set-WebAppSettings' {
    It 'adds then deletes settings' {
        $script:Calls.Clear()
        Mock Invoke-Az { Record $Arguments; return $null }
        Set-WebAppSettings -Rg 'rg' -Name 'web' -Add @{ SERVICE_BUS_NAMESPACE = 'ns' } -Remove @('SERVICE_BUS_CONNECTION_STRING')
        @($script:Calls | Where-Object { $_ -match '^webapp config appsettings set -g rg -n web --settings SERVICE_BUS_NAMESPACE=ns$' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^webapp config appsettings delete -g rg -n web --setting-names SERVICE_BUS_CONNECTION_STRING$' }).Count | Should -Be 1
    }
}

Describe 'Publish-FunctionZip' {
    It 'zips the context without caches and deploys with remote build' {
        $script:Calls.Clear()
        Mock Invoke-Az { Record $Arguments; return $null }
        $ctx = Join-Path $env:TEMP ("fnctx-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path "$ctx\__pycache__", "$ctx\tests" -Force | Out-Null
        Set-Content "$ctx\function_app.py" 'print(1)'; Set-Content "$ctx\__pycache__\x.pyc" 'x'; Set-Content "$ctx\tests\t.py" 'x'
        $zip = Publish-FunctionZip -Rg 'rg' -Name 'fn' -Context $ctx -WorkDir $ctx
        Test-Path $zip | Should -BeTrue
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $entries = [IO.Compression.ZipFile]::OpenRead($zip).Entries | ForEach-Object { $_.FullName }
        $entries | Should -Contain 'function_app.py'
        ($entries | Where-Object { $_ -match '__pycache__|^tests/' }) | Should -BeNullOrEmpty
        @($script:Calls | Where-Object { $_ -match '^functionapp config appsettings set -g rg -n fn --settings .*SCM_DO_BUILD_DURING_DEPLOYMENT=true' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^functionapp deployment source config-zip -g rg -n fn --src .*\.zip --build-remote true' }).Count | Should -Be 1
        Remove-Item $ctx -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Describe 'Test-WebAppDeployedCommit' {
    It 'is true when a deployment lists the commit' {
        Mock Invoke-Az { return @([pscustomobject]@{ id = 'abcdef1234567890'; status = 4; message = 'feat x' }) }
        Test-WebAppDeployedCommit -Rg 'rg' -Name 'web' -Sha 'abcdef1' | Should -BeTrue
    }
    It 'is false otherwise' {
        Mock Invoke-Az { return @([pscustomobject]@{ id = '999999'; status = 4; message = 'other' }) }
        Test-WebAppDeployedCommit -Rg 'rg' -Name 'web' -Sha 'abcdef1' | Should -BeFalse
    }
}

Describe 'Ensure-HostStorageAccount' {
    It 'reuses an existing host account distinct from the data accounts' {
        Mock Invoke-Az { throw 'no az expected' }
        $inv = [pscustomobject]@{ StorageAccounts = @(
            [pscustomobject]@{ Name = 'polisprodstorage'; Role = 'data'; Id = '/d' },
            [pscustomobject]@{ Name = 'polisprodhost'; Role = 'host'; Id = '/h' }) }
        Ensure-HostStorageAccount -Rg 'rg' -Location 'westeurope' -Inventory $inv | Should -Be 'polisprodhost'
    }
    It 'creates a new Standard_LRS account when there is no separate host' {
        $script:Calls.Clear()
        Mock Invoke-Az { param($Arguments) Record $Arguments; $a = $Arguments -join ' '
            if ($a -match '^storage account check-name') { return [pscustomobject]@{ nameAvailable = $true } }
            if ($a -match '^storage account create') { return [pscustomobject]@{ name = $Arguments[[array]::IndexOf($Arguments, '-n') + 1]; id = '/new' } }
            throw "unexpected $a" }
        $inv = [pscustomobject]@{ StorageAccounts = @([pscustomobject]@{ Name = 'polisprodstorage'; Role = 'data'; Id = '/d' }) }
        $name = Ensure-HostStorageAccount -Rg 'rg' -Location 'westeurope' -Inventory $inv
        $name | Should -Match '^polisprodfunchost[a-z0-9]{0,4}$'
        @($script:Calls | Where-Object { $_ -match "^storage account create -n $name -g rg -l westeurope --sku Standard_LRS --kind StorageV2 --min-tls-version TLS1_2 --allow-blob-public-access false --https-only true" }).Count | Should -Be 1
    }
    It 'adds a random suffix when the base name is taken' {
        Mock Invoke-Az { param($Arguments) $a = $Arguments -join ' '
            if ($a -match '^storage account check-name -n polisprodfunchost$') { return [pscustomobject]@{ nameAvailable = $false } }
            if ($a -match '^storage account check-name') { return [pscustomobject]@{ nameAvailable = $true } }
            if ($a -match '^storage account create') { return [pscustomobject]@{ id = '/new' } }
            throw "unexpected $a" }
        $inv = [pscustomobject]@{ StorageAccounts = @([pscustomobject]@{ Name = 'polisprodstorage'; Role = 'data'; Id = '/d' }) }
        Ensure-HostStorageAccount -Rg 'rg' -Location 'westeurope' -Inventory $inv | Should -Match '^polisprodfunchost[a-z0-9]{4}$'
    }
}

Describe 'Move-FunctionShares' {
    It 'creates shares on the host account, re-points content share and mounts' {
        $script:Calls.Clear()
        Mock Invoke-Az { param($Arguments) Record $Arguments; $a = $Arguments -join ' '
            if ($a -match '^storage account keys list') { return @([pscustomobject]@{ keyName = 'key1'; value = 'K1' }) }
            if ($a -match '^storage account show -n polisprodhost') { return [pscustomobject]@{ resourceGroup = 'rg' } }
            return $null }
        $fn = [pscustomobject]@{ Kind = 'functionApp'; Name = 'fn'; Settings = @{ WEBSITE_CONTENTSHARE = 'fn-content'; WEBSITE_CONTENTAZUREFILECONNECTIONSTRING = 'DefaultEndpointsProtocol=https;AccountName=polisprodstorage;AccountKey=RkFLRQ==;EndpointSuffix=core.windows.net' }
            Mounts = @([pscustomobject]@{ name = 'data-mount'; accountName = 'polisprodstorage'; shareName = 'function-data'; mountPath = '/data'; type = 'AzureFiles' }) }
        Move-FunctionShares -Rg 'rg' -FunctionService $fn -HostAccount 'polisprodhost'
        @($script:Calls | Where-Object { $_ -match '^storage share-rm create -g rg --storage-account polisprodhost -n fn-content' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^storage share-rm create -g rg --storage-account polisprodhost -n function-data' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^functionapp config appsettings set -g rg -n fn --settings WEBSITE_CONTENTAZUREFILECONNECTIONSTRING=.*AccountName=polisprodhost;AccountKey=K1' }).Count | Should -Be 1
        @($script:Calls | Where-Object { $_ -match '^webapp config storage-account update -g rg -n fn --custom-id data-mount --storage-type AzureFiles --account-name polisprodhost --share-name function-data --access-key K1 --mount-path /data' }).Count | Should -Be 1
    }
}
