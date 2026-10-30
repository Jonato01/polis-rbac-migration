Set-StrictMode -Version Latest

$script:SpVars = @('AZURE_CLIENT_ID', 'AZURE_CLIENT_SECRET', 'AZURE_TENANT_ID')

function Invoke-Phase06 {
    <# Switch settings: aggiunge le variabili identity, rimuove quelle key-based (e i secret). #>
    param([Parameter(Mandatory)][hashtable]$Context)
    foreach ($svc in (Get-TargetServices $Context)) {
        $name = $svc.Name
        $m = $svc.Mapping
        $prev = Get-StepStatus -State $Context.State -Phase '05' -Service $name
        if ($prev -ne 'done') {
            Set-StepStatus -State $Context.State -Phase '06' -Service $name -Status 'skipped' -Detail "fase 05 = $prev : settings non toccate"
            continue
        }
        if ($svc.Kind -eq 'functionApp' -and (Test-MappingFlag $m 'requireDeclarativeBindings')) {
            $fns = @($svc.Functions | Where-Object { $null -ne $_ })
            if ($fns.Count -eq 0) { Set-StepStatus -State $Context.State -Phase '06' -Service $name -Status 'skipped' -Detail 'nessuna function deployata: inattiva, non migrata'; continue }
            $bad = @()
            foreach ($f in $fns) { foreach ($b in @($f.bindings)) { if ($b.type -match 'serviceBus|blob|queue' -and -not ($b.PSObject.Properties['connection'] -and $b.connection)) { $bad += $f.name } } }
            if ($bad.Count -gt 0) { Set-StepStatus -State $Context.State -Phase '06' -Service $name -Status 'failed' -Detail "binding senza 'connection' ($($bad -join ', ')): non migrabile senza codice"; continue }
        }
        $resolved = Get-ResolvedForService -Context $Context -Service $svc
        $change = ConvertTo-IdentitySettings -Current $svc.Settings -Entry $m -Resolved $resolved
        $add = $change.Add
        $remove = @($change.Remove)
        $detail = "add: $($add.Keys -join ','); remove: $($remove -join ',')"

        # credenziali service principal: rimozione solo con conferma esplicita
        $sp = @($script:SpVars | Where-Object { $svc.Settings.ContainsKey($_) })
        if ($sp.Count -gt 0) {
            $removeSp = $false
            if (-not $Context.Options.NonInteractive) {
                $removeSp = Read-Confirmation -Message "$name ha credenziali service principal ($($sp -join ', ')). Rimuoverle? (DefaultAzureCredential le preferirebbe alla managed identity)" -NonInteractive $false
            }
            if ($removeSp) { $remove += $sp; $detail += "; SP rimosse: $($sp -join ',')" } else { $detail += "; ATTENZIONE: $($sp -join ',') conservate (service principal ancora attivo)" }
        }
        $secrets = @()
        if ($m.PSObject.Properties['removeSecrets']) { $secrets = @($m.removeSecrets | Where-Object { $svc.SecretNames -contains $_ }) }
        foreach ($k in $svc.SecretRefs.Keys) { if ($remove -contains $k -and $secrets -notcontains $svc.SecretRefs[$k]) { $secrets += $svc.SecretRefs[$k] } }

        Invoke-StepWithFallback -Context $Context -Phase '06' -Service $name -Action {
            switch ($svc.Kind) {
                'containerApp' { Set-ContainerAppEnv -Rg $Context.Rg -Name $name -Add $add -Remove $remove -RemoveSecrets $secrets }
                'functionApp' { Set-WebAppSettings -Rg $Context.Rg -Name $name -Add $add -Remove $remove -FunctionApp }
                default { Set-WebAppSettings -Rg $Context.Rg -Name $name -Add $add -Remove $remove }
            }
            foreach ($k in $add.Keys) { $svc.Settings[$k] = $add[$k] }
            foreach ($k in $remove) { $svc.Settings.Remove($k) }
        } -Verify { $true } -Detail $detail | Out-Null
    }
    Save-RunState -State $Context.State
}
