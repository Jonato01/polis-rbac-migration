Set-StrictMode -Version Latest

<#
Identity e role assignment. Dipende da Az.ps1 (Invoke-Az).
Ruoli (nomi esatti Azure built-in):
  Storage Blob Data Contributor, Storage Blob Data Owner, Storage Queue Data Contributor,
  Azure Service Bus Data Owner, AcrPull
#>

function Get-AzCommandForKind {
    param([string]$Kind)
    switch ($Kind) {
        'containerApp' { return 'containerapp' }
        'webApp' { return 'webapp' }
        'functionApp' { return 'functionapp' }
        default { throw "Kind non gestito: $Kind" }
    }
}

function Ensure-SystemIdentity {
    <#
    .SYNOPSIS
    Abilita la system-assigned identity sul servizio se manca; ritorna il principalId
    (null in WhatIf quando l'identity non esiste ancora).
    #>
    param([Parameter(Mandatory)][pscustomobject]$Service)
    if ($Service.PSObject.Properties['Identity'] -and $Service.Identity) { return $Service.Identity }
    $cmd = Get-AzCommandForKind $Service.Kind
    $rg = if ($Service.PSObject.Properties['Rg'] -and $Service.Rg) { $Service.Rg } else { $script:CurrentRg }
    $args = @($cmd, 'identity', 'assign', '-n', $Service.Name, '--system-assigned')
    if ($rg) { $args += @('-g', $rg) }
    $res = Invoke-Az $args
    if ($null -eq $res) { return $null }
    if ($res.PSObject.Properties['principalId']) { return $res.principalId }
    if ($res.PSObject.Properties['identity'] -and $res.identity.PSObject.Properties['principalId']) { return $res.identity.principalId }
    throw "Impossibile leggere principalId dell'identity di $($Service.Name)"
}

function Set-RbacResourceGroup { param([string]$Rg) $script:CurrentRg = $Rg }
$script:CurrentRg = $null

function Get-RequiredRoles {
    <#
    .SYNOPSIS
    Ruoli (Role, Scope) richiesti dal servizio in base alla sua voce di mapping e all'inventario.
    #>
    param(
        [Parameter(Mandatory)][pscustomobject]$Service,
        [Parameter(Mandatory)][pscustomobject]$Inventory,
        [string]$HostStorageId
    )
    $roles = @()
    $m = $Service.Mapping
    if (-not $m) { return $roles }
    function Flag([string]$n) { return ($m.PSObject.Properties[$n] -and [bool]$m.$n) }

    if (Flag 'needsStorage') {
        foreach ($s in @($Inventory.StorageAccounts | Where-Object { $_.Role -eq 'data' -and $_.Id })) {
            $roles += @{ Role = 'Storage Blob Data Contributor'; Scope = $s.Id }
        }
    }
    if (Flag 'hostStorage') {
        $hostId = $HostStorageId
        if (-not $hostId) {
            $h = @($Inventory.StorageAccounts | Where-Object { $_.Role -eq 'host' -and $_.Id })
            if ($h.Count -gt 0) { $hostId = $h[0].Id }
        }
        if (-not $hostId) { throw "Nessuno storage host per $($Service.Name): passare -HostStorageId" }
        $roles += @{ Role = 'Storage Blob Data Owner'; Scope = $hostId }
        $roles += @{ Role = 'Storage Queue Data Contributor'; Scope = $hostId }
    }
    if ((Flag 'needsServiceBus') -and $Inventory.ServiceBus -and $Inventory.ServiceBus.Id) {
        $roles += @{ Role = 'Azure Service Bus Data Owner'; Scope = $Inventory.ServiceBus.Id }
    }
    if ($Service.Kind -eq 'containerApp' -and $Inventory.Acr -and $Inventory.Acr.Id) {
        $roles += @{ Role = 'AcrPull'; Scope = $Inventory.Acr.Id }
    }
    return $roles | ForEach-Object { [pscustomobject]$_ }
}

function Test-RoleAssignmentExists {
    param([string]$PrincipalId, [string]$Role, [string]$Scope)
    $list = Invoke-Az @('role', 'assignment', 'list', '--assignee', $PrincipalId, '--role', $Role, '--scope', $Scope) -AllowFailure
    foreach ($a in @($list)) { if ($null -ne $a) { return $true } }
    return $false
}

function Ensure-RoleAssignment {
    <#
    .SYNOPSIS
    Crea la role assignment se manca. Ritorna $true se creata ora, $false se gia' presente.
    #>
    param(
        [Parameter(Mandatory)][string]$PrincipalId,
        [Parameter(Mandatory)][string]$Role,
        [Parameter(Mandatory)][string]$Scope
    )
    if (Test-RoleAssignmentExists -PrincipalId $PrincipalId -Role $Role -Scope $Scope) { return $false }
    if (Get-AzWhatIf) { Write-AzLog "[WHATIF] role assignment $Role -> $PrincipalId @ $Scope"; return $false }
    Invoke-Az @('role', 'assignment', 'create', '--assignee-object-id', $PrincipalId, '--assignee-principal-type', 'ServicePrincipal', '--role', $Role, '--scope', $Scope) | Out-Null
    return $true
}

function Wait-RoleAssignment {
    <#
    .SYNOPSIS
    Attende che la role assignment risulti elencata (propagazione RBAC). Lancia a timeout.
    #>
    param(
        [Parameter(Mandatory)][string]$PrincipalId,
        [Parameter(Mandatory)][string]$Role,
        [Parameter(Mandatory)][string]$Scope,
        [int]$TimeoutSec = 300,
        [int]$PollSec = 10
    )
    if (Get-AzWhatIf) { return }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        if (Test-RoleAssignmentExists -PrincipalId $PrincipalId -Role $Role -Scope $Scope) { return }
        if ((Get-Date) -ge $deadline) { break }
        if ($PollSec -gt 0) { Start-Sleep -Seconds $PollSec }
    } while ($true)
    throw "Role assignment '$Role' per $PrincipalId su $Scope non visibile dopo $TimeoutSec s"
}
