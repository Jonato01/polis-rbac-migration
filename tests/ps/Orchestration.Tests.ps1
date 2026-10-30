Set-StrictMode -Version Latest

BeforeAll {
    $script:Tools = (Resolve-Path "$PSScriptRoot\..\..\tools").Path
    foreach ($lib in 'Az', 'Mapping', 'Settings', 'State', 'Discovery', 'Rbac', 'Deploy', 'Verify', 'Hardening', 'Report', 'Fallback', 'Cleanup', 'Orchestration') { . (Join-Path $Tools "lib\$lib.ps1") }
    foreach ($ph in (Get-ChildItem (Join-Path $Tools 'phases') -Filter '*.ps1' | Sort-Object Name)) { . $ph.FullName }
    $script:Mapping = Get-Content (Join-Path $Tools 'mapping.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $script:Sub = '/subscriptions/00000000-0000-0000-0000-000000000000'
    $script:DataCs = 'DefaultEndpointsProtocol=https;AccountName=polisprodstorage;AccountKey=RkFLRQ==;EndpointSuffix=core.windows.net'
    $script:SbCs = 'Endpoint=sb://polis-messaging-prod.servicebus.windows.net/;SharedAccessKeyName=k;SharedAccessKey=RkFLRQ=='

    function script:NewTmp { $p = Join-Path $env:TEMP ("orch-" + [guid]::NewGuid().ToString('N')); New-Item -ItemType Directory -Path $p -Force | Out-Null; return $p }
    function script:NewInventory([object[]]$services) {
        [pscustomobject]@{
            Rg = 'rg-polis'; Services = $services; Unmapped = @(); MissingExpected = @()
            StorageAccounts = @([pscustomobject]@{ Name = 'polisprodstorage'; Id = "$Sub/rg/sa/polisprodstorage"; Role = 'data'; ResourceGroup = 'rg-polis'; AllowSharedKey = $true; Location = 'westeurope' })
            ServiceBus = [pscustomobject]@{ Name = 'polis-messaging-prod'; Id = "$Sub/rg/sb/polis-messaging-prod"; Namespace = 'polis-messaging-prod.servicebus.windows.net'; DisableLocalAuth = $false }
            Acr = [pscustomobject]@{ Name = 'polisregistryprod'; Id = "$Sub/rg/acr/polisregistryprod"; LoginServer = 'polisregistryprod.azurecr.io'; AdminEnabled = $true }
        }
    }
    function script:ImportApp { [pscustomobject]@{ Kind = 'containerApp'; Name = 'polis-prod-import-app'; Id = '/ca'; Image = 'polisregistryprod.azurecr.io/polis-prod-import:1'
            Settings = @{ AZURE_STORAGE_CONNECTION_STRING = $DataCs; AZURE_SERVICEBUS_CONNECTION_STRING = $SbCs }; SecretRefs = @{}; SecretNames = @(); Registry = 'polisregistryprod.azurecr.io'
            Identity = $null; Mounts = @(); Functions = @(); MinReplicas = 0; Mapping = ($Mapping.containerApps | Where-Object image -eq 'polis-import'); KeyBased = $true; KeyBasedSettings = @('AZURE_STORAGE_CONNECTION_STRING'); Excluded = $false; Legacy = $false } }
    function script:DbApp([hashtable]$extra) {
        $s = @{ SERVICE_BUS_CONNECTION_STRING = $SbCs }; foreach ($k in $extra.Keys) { $s[$k] = $extra[$k] }
        $m = ($Mapping.webApps | Where-Object repo -eq 'polis-api-database') | Select-Object *
        $m.expectedCommit = 'abc1234'   # nel mapping reale viene fissato dal Task 21
        [pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-api-database'; Id = '/wa'; Image = $null; Settings = $s; SecretRefs = @{}; SecretNames = @(); Registry = $null
            Identity = 'p-db'; Mounts = @(); Functions = @(); MinReplicas = 0; Mapping = $m; KeyBased = $true; KeyBasedSettings = @('SERVICE_BUS_CONNECTION_STRING'); Excluded = $false; Legacy = $false } }
    function script:BaseOptions([string]$work) { @{ Rg = 'rg-polis'; WorkDir = $work; WhatIf = $false; ForceHardening = $false; KeepBackup = $false; SmokeTest = $false; AnthropicApiKey = ''; NonInteractive = $true; ReportDir = $work; Rollback = $false; AfterHardening = $false; ToolsDir = $Tools; RepoRoot = (Split-Path $Tools -Parent) } }
    function script:MockAll {
        Mock Test-PreflightEnvironment { return @() }
        Mock Test-CommandAvailable { return $true }
        Mock Invoke-Az { param($Arguments) $a = $Arguments -join ' '
            if ($a -match '^account show') { return [pscustomobject]@{ id = 'sub1'; name = 'Sub'; user = [pscustomobject]@{ name = 'op@x.it' } } }
            if ($a -match '^group show') { return [pscustomobject]@{ id = "$Sub/resourceGroups/rg-polis"; location = 'westeurope' } }
            return $null }
        Mock Test-OperatorRole { return $true }
        Mock Ensure-SystemIdentity { return 'p-new' }
        Mock Ensure-RoleAssignment { return $true }
        Mock Wait-RoleAssignment { }
        Mock Build-ContainerImage { return 'polisregistryprod.azurecr.io/polis-import:tag' }
        Mock Update-ContainerAppImage { }
        Mock Set-ContainerAppEnv { }
        Mock Set-WebAppSettings { }
        Mock Publish-FunctionZip { return 'x.zip' }
        Mock Ensure-HostStorageAccount { return 'polisprodfunchost' }
        Mock Move-FunctionShares { }
        Mock Test-WebAppDeployedCommit { return $true }
        Mock Test-ContainerAppHealthy { return @{ Ok = $true; Detail = 'Running' } }
        Mock Test-WebAppHealthy { return @{ Ok = $true; Detail = 'HTTP 200' } }
        Mock Test-FunctionAppHealthy { return @{ Ok = $true; Detail = 'Running' } }
        Mock Invoke-Hardening { }
        Mock Invoke-ClaudeFallback { return @{ Ok = $false; Skipped = $false; Summary = 'no'; ResultPath = $null; Transcript = '' } }
        Mock Read-Confirmation { return $true }
        Mock Write-Host { }
    }
}

Describe 'Invoke-StepWithFallback' {
    It 'retries once, then calls the fallback at most twice, re-verifying after each, and marks failed' {
        $work = NewTmp
        $ctx = @{ Options = (BaseOptions $work); State = (New-RunState -WorkDir $work); FallbackLog = [System.Collections.ArrayList]::new(); Rg = 'rg-polis'; Sub = 's'; WorkDir = $work; ToolsDir = $Tools; RepoRoot = (Split-Path $Tools -Parent) }
        $script:actions = 0; $script:verifies = 0
        Mock Invoke-ClaudeFallback { return @{ Ok = $true; Skipped = $false; Summary = 'tried'; ResultPath = $null; Transcript = 't' } }
        Mock Write-Host { }
        $ok = Invoke-StepWithFallback -Context $ctx -Phase '05' -Service 'svc' -Action { $script:actions++; throw 'boom' } -Verify { $script:verifies++; return $false }
        $ok | Should -BeFalse
        $script:actions | Should -Be 2
        $script:verifies | Should -Be 2
        Should -Invoke Invoke-ClaudeFallback -Times 2 -Exactly
        Get-StepStatus -State $ctx.State -Phase '05' -Service 'svc' | Should -Be 'failed'
        $ctx.FallbackLog.Count | Should -Be 2
    }
    It 'marks done when the action succeeds and verify passes' {
        $work = NewTmp
        $ctx = @{ Options = (BaseOptions $work); State = (New-RunState -WorkDir $work); FallbackLog = [System.Collections.ArrayList]::new(); Rg = 'rg-polis'; Sub = 's'; WorkDir = $work; ToolsDir = $Tools; RepoRoot = (Split-Path $Tools -Parent) }
        Mock Invoke-ClaudeFallback { throw 'must not be called' }
        Mock Write-Host { }
        Invoke-StepWithFallback -Context $ctx -Phase '05' -Service 'svc' -Action { 'ok' } -Verify { $true } -Detail 'image x' | Should -BeTrue
        Get-StepStatus -State $ctx.State -Phase '05' -Service 'svc' | Should -Be 'done'
        Get-StepDetail -State $ctx.State -Phase '05' -Service 'svc' | Should -Be 'image x'
    }
    It 'marks done when the fallback fixes the step (verify passes afterwards)' {
        $work = NewTmp
        $ctx = @{ Options = (BaseOptions $work); State = (New-RunState -WorkDir $work); FallbackLog = [System.Collections.ArrayList]::new(); Rg = 'rg-polis'; Sub = 's'; WorkDir = $work; ToolsDir = $Tools; RepoRoot = (Split-Path $Tools -Parent) }
        $script:v = 0
        Mock Invoke-ClaudeFallback { return @{ Ok = $true; Skipped = $false; Summary = 'fixed'; ResultPath = $null; Transcript = 't' } }
        Mock Write-Host { }
        Invoke-StepWithFallback -Context $ctx -Phase '06' -Service 'svc' -Action { throw 'x' } -Verify { $script:v++; return ($script:v -ge 1) } | Should -BeTrue
        Should -Invoke Invoke-ClaudeFallback -Times 1 -Exactly
        Get-StepStatus -State $ctx.State -Phase '06' -Service 'svc' | Should -Be 'done'
    }
}

Describe 'Invoke-PolisMigrationCore' {
    It '(a) WhatIf runs discovery, deploy, settings, verify and report but never hardens' {
        MockAll
        $work = NewTmp
        Mock Get-PolisInventory { return (NewInventory @((ImportApp), (DbApp @{}))) }
        $opt = BaseOptions $work; $opt.WhatIf = $true
        $code = Invoke-PolisMigrationCore -Options $opt
        $code | Should -Be 0
        Should -Invoke Get-PolisInventory -Times 1
        Should -Invoke Build-ContainerImage -Times 1
        Should -Invoke Invoke-Hardening -Times 0
        Test-Path (Join-Path $work "polis-update-report-*.md") | Should -BeTrue
        $state = New-RunState -WorkDir $work
        Get-StepStatus -State $state -Phase '08' -Service '_global' | Should -Be 'skipped'
    }
    It '(b) a failing step gets exactly two fallback attempts and the run continues with exit 2' {
        MockAll
        $work = NewTmp
        Mock Get-PolisInventory { return (NewInventory @((ImportApp), (DbApp @{}))) }
        Mock Build-ContainerImage { throw 'acr build failed' }
        $code = Invoke-PolisMigrationCore -Options (BaseOptions $work)
        $code | Should -Be 2
        Should -Invoke Invoke-ClaudeFallback -Times 2 -Exactly
        Should -Invoke Set-WebAppSettings -Times 1   # the web app still proceeds
        $state = New-RunState -WorkDir $work
        Get-StepStatus -State $state -Phase '05' -Service 'polis-prod-import-app' | Should -Be 'failed'
    }
    It '(c) stops with exit 3 before discovery when the operator lacks Owner/UAA' {
        MockAll
        $work = NewTmp
        Mock Test-OperatorRole { return $false }
        Mock Get-PolisInventory { throw 'must not run' }
        Invoke-PolisMigrationCore -Options (BaseOptions $work) | Should -Be 3
    }
    It '(d) a rerun skips build for a service already done in phase 05' {
        MockAll
        $work = NewTmp
        $pre = New-RunState -WorkDir $work
        Set-StepStatus -State $pre -Phase '05' -Service 'polis-prod-import-app' -Status 'done' -Detail 'already'
        Save-RunState -State $pre
        Mock Get-PolisInventory { return (NewInventory @((ImportApp))) }
        Invoke-PolisMigrationCore -Options (BaseOptions $work) | Should -Be 0
        Should -Invoke Build-ContainerImage -Times 0
    }
    It '(e) a web app whose expected commit is not deployed is skipped in phase 06' {
        MockAll
        $work = NewTmp
        Mock Get-PolisInventory { return (NewInventory @((DbApp @{}))) }
        Mock Test-WebAppDeployedCommit { return $false }
        Invoke-PolisMigrationCore -Options (BaseOptions $work) | Should -Be 0
        Should -Invoke Set-WebAppSettings -Times 0
        $state = New-RunState -WorkDir $work
        Get-StepStatus -State $state -Phase '06' -Service 'polis-prod-api-database' | Should -Be 'skipped'
    }
    It '(f) service principal credentials are kept in NonInteractive mode and reported' {
        MockAll
        $work = NewTmp
        Mock Get-PolisInventory { return (NewInventory @((DbApp @{ AZURE_CLIENT_ID = 'c'; AZURE_CLIENT_SECRET = 's'; AZURE_TENANT_ID = 't' }))) }
        Invoke-PolisMigrationCore -Options (BaseOptions $work) | Should -Be 0
        Should -Invoke Set-WebAppSettings -Times 1 -ParameterFilter { $Remove -notcontains 'AZURE_CLIENT_SECRET' }
        $state = New-RunState -WorkDir $work
        (Get-StepDetail -State $state -Phase '06' -Service 'polis-prod-api-database') | Should -Match 'AZURE_CLIENT_SECRET'
    }
    It '(g) hardening runs when everything is green and the operator confirms' {
        MockAll
        $work = NewTmp
        Mock Get-PolisInventory { return (NewInventory @((ImportApp), (DbApp @{}))) }
        Invoke-PolisMigrationCore -Options (BaseOptions $work) | Should -Be 0
        Should -Invoke Invoke-Hardening -Times 1
        $state = New-RunState -WorkDir $work
        Get-StepStatus -State $state -Phase '08' -Service '_global' | Should -Be 'done'
    }
    It '(h) hardening is skipped when the operator declines' {
        MockAll
        $work = NewTmp
        Mock Get-PolisInventory { return (NewInventory @((ImportApp))) }
        $opt = BaseOptions $work; $opt.NonInteractive = $false
        Mock Read-Confirmation { param($Message) if ($Message -match 'hardening|chiavi') { return $false }; return $true }
        Invoke-PolisMigrationCore -Options $opt | Should -Be 0
        Should -Invoke Invoke-Hardening -Times 0
    }
}

Describe 'Invoke-PolisMigration.ps1 entry point' {
    It 'parses with the documented parameters' {
        $t = $null; $e = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Tools 'Invoke-PolisMigration.ps1'), [ref]$t, [ref]$e)
        @($e).Count | Should -Be 0
        $names = $ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }
        foreach ($p in 'Rg', 'WorkDir', 'WhatIf', 'ForceHardening', 'KeepBackup', 'SmokeTest', 'AnthropicApiKey', 'Rollback', 'AfterHardening', 'NonInteractive') { $names | Should -Contain $p }
    }
}
