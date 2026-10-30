Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Az.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Verify.ps1"
    $script:Patterns = @('Unauthorized', 'AuthenticationFailed', 'CredentialUnavailableError', ' 401 ', ' 403 ')
    $script:Calls = [System.Collections.ArrayList]::new()
    function script:Record($Arguments) { [void]$script:Calls.Add(($Arguments -join ' ')) }
}

Describe 'Test-LogClean' {
    It 'is false when an error pattern appears (case-insensitive)' {
        Test-LogClean -LogText "2026 INFO ok`n2026 ERROR azure.core: authenticationfailed for blob" -ErrorPatterns $Patterns | Should -BeFalse
    }
    It 'is true on a clean log' {
        Test-LogClean -LogText "started`nlistening on queue`nmessage processed" -ErrorPatterns $Patterns | Should -BeTrue
    }
    It 'is true on empty log' {
        Test-LogClean -LogText '' -ErrorPatterns $Patterns | Should -BeTrue
    }
}

Describe 'Test-ContainerAppHealthy' {
    It 'scales to 1 replica, reads status and logs, then scales back to 0' {
        $script:Calls.Clear()
        Mock Invoke-Az { param($Arguments) Record $Arguments; $a = $Arguments -join ' '
            if ($a -match '^containerapp show') { return [pscustomobject]@{ properties = [pscustomobject]@{ runningStatus = 'Running'; latestRevisionName = 'r1'; template = [pscustomobject]@{ scale = [pscustomobject]@{ minReplicas = 1 } } } } }
            if ($a -match '^containerapp logs show') { return "INFO started`nINFO listening" }
            if ($a -match '^containerapp revision list') { return @([pscustomobject]@{ name = 'r1'; properties = [pscustomobject]@{ healthState = 'Healthy'; runningState = 'Running'; replicas = 1 } }) }
            return $null }
        $svc = [pscustomobject]@{ Kind = 'containerApp'; Name = 'app'; MinReplicas = 0 }
        $r = Test-ContainerAppHealthy -Rg 'rg' -Service $svc -ErrorPatterns $Patterns -ObserveSec 0
        $r.Ok | Should -BeTrue
        $up = [array]::IndexOf($script:Calls.ToArray(), 'containerapp update -g rg -n app --min-replicas 1')
        $logs = ($script:Calls | Select-String -Pattern '^containerapp logs show' -List).LineNumber
        $down = [array]::IndexOf($script:Calls.ToArray(), 'containerapp update -g rg -n app --min-replicas 0')
        $up | Should -BeGreaterOrEqual 0
        $down | Should -BeGreaterThan $up
        @($script:Calls | Where-Object { $_ -match '^containerapp logs show -g rg -n app --type console' }).Count | Should -Be 1
    }
    It 'does not touch replicas when minReplicas is already > 0' {
        $script:Calls.Clear()
        Mock Invoke-Az { param($Arguments) Record $Arguments; $a = $Arguments -join ' '
            if ($a -match '^containerapp show') { return [pscustomobject]@{ properties = [pscustomobject]@{ runningStatus = 'Running'; latestRevisionName = 'r1' } } }
            if ($a -match '^containerapp logs show') { return 'INFO ok' }
            if ($a -match '^containerapp revision list') { return @([pscustomobject]@{ name = 'r1'; properties = [pscustomobject]@{ healthState = 'Healthy'; runningState = 'Running'; replicas = 1 } }) }
            return $null }
        $svc = [pscustomobject]@{ Kind = 'containerApp'; Name = 'app'; MinReplicas = 1 }
        (Test-ContainerAppHealthy -Rg 'rg' -Service $svc -ErrorPatterns $Patterns -ObserveSec 0).Ok | Should -BeTrue
        @($script:Calls | Where-Object { $_ -match '--min-replicas' }).Count | Should -Be 0
    }
    It 'is not ok when logs contain auth errors' {
        Mock Invoke-Az { param($Arguments) $a = $Arguments -join ' '
            if ($a -match '^containerapp show') { return [pscustomobject]@{ properties = [pscustomobject]@{ runningStatus = 'Running'; latestRevisionName = 'r1' } } }
            if ($a -match '^containerapp logs show') { return 'ERROR CredentialUnavailableError: ManagedIdentityCredential authentication unavailable' }
            if ($a -match '^containerapp revision list') { return @([pscustomobject]@{ name = 'r1'; properties = [pscustomobject]@{ healthState = 'Healthy'; runningState = 'Running'; replicas = 1 } }) }
            return $null }
        $svc = [pscustomobject]@{ Kind = 'containerApp'; Name = 'app'; MinReplicas = 1 }
        $r = Test-ContainerAppHealthy -Rg 'rg' -Service $svc -ErrorPatterns $Patterns -ObserveSec 0
        $r.Ok | Should -BeFalse
        $r.Detail | Should -Match 'CredentialUnavailableError'
    }
    It 'is not ok when the latest revision is unhealthy' {
        Mock Invoke-Az { param($Arguments) $a = $Arguments -join ' '
            if ($a -match '^containerapp show') { return [pscustomobject]@{ properties = [pscustomobject]@{ runningStatus = 'Running'; latestRevisionName = 'r2' } } }
            if ($a -match '^containerapp logs show') { return 'INFO ok' }
            if ($a -match '^containerapp revision list') { return @([pscustomobject]@{ name = 'r2'; properties = [pscustomobject]@{ healthState = 'Unhealthy'; runningState = 'Failed'; replicas = 0 } }) }
            return $null }
        $svc = [pscustomobject]@{ Kind = 'containerApp'; Name = 'app'; MinReplicas = 1 }
        (Test-ContainerAppHealthy -Rg 'rg' -Service $svc -ErrorPatterns $Patterns -ObserveSec 0).Ok | Should -BeFalse
    }
}

Describe 'Test-WebAppHealthy' {
    It 'is ok on HTTP 200' {
        Mock Invoke-Az { return [pscustomobject]@{ defaultHostName = 'web.azurewebsites.net'; state = 'Running' } }
        Mock Invoke-HttpProbe { return 200 }
        $r = Test-WebAppHealthy -Rg 'rg' -Service ([pscustomobject]@{ Kind = 'webApp'; Name = 'web' }) -HealthPath '/docs'
        $r.Ok | Should -BeTrue
        Should -Invoke Invoke-HttpProbe -Times 1 -ParameterFilter { $Url -eq 'https://web.azurewebsites.net/docs' }
    }
    It 'is not ok on HTTP 503' {
        Mock Invoke-Az { return [pscustomobject]@{ defaultHostName = 'web.azurewebsites.net'; state = 'Running' } }
        Mock Invoke-HttpProbe { return 503 }
        $r = Test-WebAppHealthy -Rg 'rg' -Service ([pscustomobject]@{ Kind = 'webApp'; Name = 'web' }) -HealthPath '/'
        $r.Ok | Should -BeFalse
        $r.Detail | Should -Match '503'
    }
    It 'treats 401/403 from the app itself as alive (auth-protected endpoints)' {
        Mock Invoke-Az { return [pscustomobject]@{ defaultHostName = 'web.azurewebsites.net'; state = 'Running' } }
        Mock Invoke-HttpProbe { return 401 }
        (Test-WebAppHealthy -Rg 'rg' -Service ([pscustomobject]@{ Kind = 'webApp'; Name = 'web' }) -HealthPath '/').Ok | Should -BeTrue
    }
}

Describe 'Test-FunctionAppHealthy' {
    It 'is ok when Running with functions listed' {
        Mock Invoke-Az { param($Arguments) $a = $Arguments -join ' '
            if ($a -match '^functionapp show') { return [pscustomobject]@{ state = 'Running' } }
            if ($a -match '^functionapp function list') { return @([pscustomobject]@{ name = 'fn/process_zip_timer' }) }
            return $null }
        (Test-FunctionAppHealthy -Rg 'rg' -Service ([pscustomobject]@{ Kind = 'functionApp'; Name = 'fn' })).Ok | Should -BeTrue
    }
    It 'is not ok when no functions are loaded (bad deploy)' {
        Mock Invoke-Az { param($Arguments) $a = $Arguments -join ' '
            if ($a -match '^functionapp show') { return [pscustomobject]@{ state = 'Running' } }
            if ($a -match '^functionapp function list') { return @() }
            return $null }
        $r = Test-FunctionAppHealthy -Rg 'rg' -Service ([pscustomobject]@{ Kind = 'functionApp'; Name = 'fn' })
        $r.Ok | Should -BeFalse
        $r.Detail | Should -Match 'nessuna function'
    }
}
