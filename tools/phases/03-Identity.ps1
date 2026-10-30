Set-StrictMode -Version Latest

function Invoke-Phase03 {
    <# System-assigned identity per ogni servizio mappato; storage host per le Function App. #>
    param([Parameter(Mandatory)][hashtable]$Context)
    $needsHost = @(Get-TargetServices $Context | Where-Object { Test-MappingFlag $_.Mapping 'hostStorage' })
    if ($needsHost.Count -gt 0 -and -not $Context.HostStorageAccount) {
        $loc = if ($Context.Location) { $Context.Location } else { 'westeurope' }
        $Context.HostStorageAccount = Ensure-HostStorageAccount -Rg $Context.Rg -Location $loc -Inventory $Context.Inventory
        $Context.HostStorageId = Resolve-ResourceId -Type 'storage' -Name $Context.HostStorageAccount
        if (-not $Context.HostStorageId) { $Context.HostStorageId = "$($Context.RgId)/providers/Microsoft.Storage/storageAccounts/$($Context.HostStorageAccount)" }
        $known = @($Context.Inventory.StorageAccounts | Where-Object { $_.Name -eq $Context.HostStorageAccount })
        if ($known.Count -eq 0) {
            $Context.Inventory.StorageAccounts += [pscustomobject]@{ Name = $Context.HostStorageAccount; Id = $Context.HostStorageId; Role = 'host'; AllowSharedKey = $true; Location = $loc; ResourceGroup = $Context.Rg; Found = $true }
        } else { foreach ($k in $known) { $k.Role = 'host' } }
        Write-Step "  Storage host per Function App: $($Context.HostStorageAccount)"
    }
    foreach ($svc in (Get-TargetServices $Context)) {
        $m = $svc.Mapping
        $needsIdentity = ($svc.Kind -eq 'containerApp') -or (Test-MappingFlag $m 'needsStorage') -or (Test-MappingFlag $m 'needsServiceBus') -or (Test-MappingFlag $m 'hostStorage')
        if (-not $needsIdentity) { Set-StepStatus -State $Context.State -Phase '03' -Service $svc.Name -Status 'skipped' -Detail 'nessun accesso Azure da migrare'; continue }
        $name = $svc.Name
        Invoke-StepWithFallback -Context $Context -Phase '03' -Service $name -Action {
            $principalId = Ensure-SystemIdentity -Service $svc
            if ($principalId) { $Context.Principals[$name] = $principalId; $svc.Identity = $principalId }
            elseif ($Context.Options.WhatIf) { $Context.Principals[$name] = 'whatif-principal' }
        } -Verify { [bool]$Context.Principals.ContainsKey($name) } -Detail 'system-assigned identity' | Out-Null
    }
}
