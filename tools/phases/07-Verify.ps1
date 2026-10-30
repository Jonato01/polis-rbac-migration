Set-StrictMode -Version Latest

function Invoke-SmokeTest {
    <# Invia un messaggio di test sulla coda import-response via REST e verifica che venga consumato. Best effort. #>
    param([Parameter(Mandatory)][hashtable]$Context)
    $sb = $Context.Inventory.ServiceBus
    if (-not $sb) { return 'nessun Service Bus' }
    $queue = 'import-response'
    $body = '{"task":"smoke-test","source":"polis-rbac-migration","run":"' + $Context.State.RunId + '"}'
    $url = "https://$($sb.Namespace)/$queue/messages"
    Invoke-Az @('rest', '--method', 'post', '--url', $url, '--resource', 'https://servicebus.azure.net', '--headers', 'Content-Type=application/json', '--body', $body) -AllowFailure | Out-Null
    Start-Sleep -Seconds 60
    $q = Invoke-Az @('servicebus', 'queue', 'show', '-g', $Context.Rg, '--namespace-name', $sb.Name, '-n', $queue) -AllowFailure
    if ($q -and $q.PSObject.Properties['countDetails']) { return "smoke: activeMessageCount=$($q.countDetails.activeMessageCount)" }
    return 'smoke: esito non leggibile'
}

function Invoke-Phase07 {
    param([Parameter(Mandatory)][hashtable]$Context)
    $patterns = @($Context.Mapping.errorPatterns)
    foreach ($svc in (Get-TargetServices $Context)) {
        $name = $svc.Name
        $m = $svc.Mapping
        if ((Get-StepStatus -State $Context.State -Phase '06' -Service $name) -ne 'done') {
            Set-StepStatus -State $Context.State -Phase '07' -Service $name -Status 'skipped' -Detail 'settings non switchate'
            continue
        }
        if ($Context.Options.WhatIf) { Set-StepStatus -State $Context.State -Phase '07' -Service $name -Status 'skipped' -Detail 'WhatIf'; continue }
        $pat = if ($m.PSObject.Properties['errorPatterns']) { @($m.errorPatterns) } else { $patterns }
        $result = switch ($svc.Kind) {
            'containerApp' { Test-ContainerAppHealthy -Rg $Context.Rg -Service $svc -ErrorPatterns $pat }
            'functionApp' { Test-FunctionAppHealthy -Rg $Context.Rg -Service $svc -ErrorPatterns $pat }
            default { Test-WebAppHealthy -Rg $Context.Rg -Service $svc -HealthPath ($(if ($m.PSObject.Properties['health']) { $m.health } else { '/' })) -ErrorPatterns $pat }
        }
        if ($result.Ok) { Set-StepStatus -State $Context.State -Phase '07' -Service $name -Status 'done' -Detail $result.Detail; Write-Step "  $name OK: $($result.Detail)" }
        else { Set-StepStatus -State $Context.State -Phase '07' -Service $name -Status 'failed' -Detail $result.Detail; Write-Step "  $name KO: $($result.Detail)" 'Red' }
    }
    if ($Context.Options.SmokeTest -and -not $Context.Options.WhatIf) {
        $d = Invoke-SmokeTest -Context $Context
        Set-StepStatus -State $Context.State -Phase '07' -Service '_smoke' -Status 'done' -Detail $d
    }
    Save-RunState -State $Context.State
}
