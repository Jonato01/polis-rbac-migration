Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Az.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Rbac.ps1"
    $script:Mapping = Get-Content "$PSScriptRoot\..\..\tools\mapping.json" -Raw -Encoding UTF8 | ConvertFrom-Json
    $script:Sub = '/subscriptions/00000000-0000-0000-0000-000000000000'
    $script:Inv = [pscustomobject]@{
        Rg = 'rg-polis'
        StorageAccounts = @(
            [pscustomobject]@{ Name = 'polisprodstorage'; Id = "$Sub/resourceGroups/rg-polis/providers/Microsoft.Storage/storageAccounts/polisprodstorage"; Role = 'data' },
            [pscustomobject]@{ Name = 'polisprodhost'; Id = "$Sub/resourceGroups/rg-polis/providers/Microsoft.Storage/storageAccounts/polisprodhost"; Role = 'host' }
        )
        ServiceBus = [pscustomobject]@{ Name = 'polis-messaging-prod'; Id = "$Sub/resourceGroups/rg-polis/providers/Microsoft.ServiceBus/namespaces/polis-messaging-prod" }
        Acr = [pscustomobject]@{ Name = 'polisregistryprod'; Id = "$Sub/resourceGroups/rg-polis/providers/Microsoft.ContainerRegistry/registries/polisregistryprod" }
    }
    function script:Svc([string]$kind, [string]$name, $mapping, [string]$identity) {
        [pscustomobject]@{ Kind = $kind; Name = $name; Mapping = $mapping; Identity = $identity }
    }
}

Describe 'Get-RequiredRoles' {
    It 'import_fatture container app: blob contributor, service bus owner, AcrPull' {
        $m = $Mapping.containerApps | Where-Object image -eq 'polis-import'
        $roles = @(Get-RequiredRoles -Service (Svc 'containerApp' 'polis-prod-import-app' $m $null) -Inventory $Inv)
        $roles.Count | Should -Be 3
        ($roles | Where-Object Role -eq 'Storage Blob Data Contributor').Scope | Should -Match 'polisprodstorage$'
        ($roles | Where-Object Role -eq 'Azure Service Bus Data Owner').Scope | Should -Match 'polis-messaging-prod$'
        ($roles | Where-Object Role -eq 'AcrPull').Scope | Should -Match 'polisregistryprod$'
    }
    It 'sftp-sync function app: host owner + host queue + data contributor + service bus owner' {
        $m = $Mapping.functionApps | Where-Object match -eq 'sftp-upload'
        $roles = @(Get-RequiredRoles -Service (Svc 'functionApp' 'polis-prod-sftp-upload' $m $null) -Inventory $Inv)
        $roles.Count | Should -Be 4
        ($roles | Where-Object Role -eq 'Storage Blob Data Owner').Scope | Should -Match 'polisprodhost$'
        ($roles | Where-Object Role -eq 'Storage Queue Data Contributor').Scope | Should -Match 'polisprodhost$'
        ($roles | Where-Object Role -eq 'Storage Blob Data Contributor').Scope | Should -Match 'polisprodstorage$'
    }
    It 'api-database web app: only service bus owner' {
        $m = $Mapping.webApps | Where-Object repo -eq 'polis-api-database'
        $roles = @(Get-RequiredRoles -Service (Svc 'webApp' 'polis-prod-api-database' $m $null) -Inventory $Inv)
        $roles.Count | Should -Be 1
        $roles[0].Role | Should -Be 'Azure Service Bus Data Owner'
    }
    It 'uses an explicit host storage id when the inventory has no host account' {
        $inv2 = [pscustomobject]@{ Rg = 'rg'; StorageAccounts = @($Inv.StorageAccounts[0]); ServiceBus = $Inv.ServiceBus; Acr = $Inv.Acr }
        $m = $Mapping.functionApps | Where-Object match -eq 'sftp-upload'
        $roles = @(Get-RequiredRoles -Service (Svc 'functionApp' 'f' $m $null) -Inventory $inv2 -HostStorageId "$Sub/x/newhost")
        ($roles | Where-Object Role -eq 'Storage Blob Data Owner').Scope | Should -Be "$Sub/x/newhost"
    }
}

Describe 'Ensure-SystemIdentity' {
    It 'returns the existing principal without calling az' {
        Mock Invoke-Az { throw 'should not be called' }
        Ensure-SystemIdentity -Service (Svc 'webApp' 'a' $null 'p-existing') | Should -Be 'p-existing'
    }
    It 'assigns a system identity to a container app and returns the principal id' {
        Mock Invoke-Az { param($Arguments) if (($Arguments -join ' ') -match '^containerapp identity assign') { return [pscustomobject]@{ principalId = 'p-new'; type = 'SystemAssigned' } }; throw "unexpected: $($Arguments -join ' ')" }
        Ensure-SystemIdentity -Service (Svc 'containerApp' 'polis-prod-import-app' $null $null) | Should -Be 'p-new'
        Should -Invoke Invoke-Az -Times 1 -ParameterFilter { ($Arguments -join ' ') -match '--system-assigned' }
    }
    It 'uses functionapp identity assign for function apps' {
        Mock Invoke-Az { param($Arguments) if (($Arguments -join ' ') -match '^functionapp identity assign') { return [pscustomobject]@{ principalId = 'p-fn' } }; throw 'unexpected' }
        Ensure-SystemIdentity -Service (Svc 'functionApp' 'f' $null $null) | Should -Be 'p-fn'
    }
}

Describe 'Ensure-RoleAssignment' {
    It 'does not recreate an existing assignment' {
        Mock Invoke-Az { param($Arguments) if (($Arguments -join ' ') -match '^role assignment list') { return @([pscustomobject]@{ roleDefinitionName = 'AcrPull' }) }; throw 'unexpected create' }
        Ensure-RoleAssignment -PrincipalId 'p' -Role 'AcrPull' -Scope '/s' | Should -BeFalse
    }
    It 'creates a missing assignment with object id and ServicePrincipal type' {
        Mock Invoke-Az { param($Arguments) $a = $Arguments -join ' '; if ($a -match '^role assignment list') { return @() }; if ($a -match '^role assignment create') { return [pscustomobject]@{ id = 'ra1' } }; throw 'unexpected' }
        Ensure-RoleAssignment -PrincipalId 'p' -Role 'AcrPull' -Scope '/s' | Should -BeTrue
        Should -Invoke Invoke-Az -Times 1 -ParameterFilter { ($Arguments -join ' ') -match '--assignee-object-id p --assignee-principal-type ServicePrincipal --role AcrPull --scope /s' }
    }
}

Describe 'Wait-RoleAssignment' {
    It 'retries until the assignment is listed' {
        $script:calls = 0
        Mock Invoke-Az { $script:calls++; if ($script:calls -lt 3) { return @() }; return @([pscustomobject]@{ roleDefinitionName = 'AcrPull' }) }
        { Wait-RoleAssignment -PrincipalId 'p' -Role 'AcrPull' -Scope '/s' -TimeoutSec 60 -PollSec 0 } | Should -Not -Throw
        $script:calls | Should -Be 3
    }
    It 'throws after the timeout' {
        Mock Invoke-Az { return @() }
        { Wait-RoleAssignment -PrincipalId 'p' -Role 'AcrPull' -Scope '/s' -TimeoutSec 0 -PollSec 0 } | Should -Throw '*AcrPull*'
    }
}
