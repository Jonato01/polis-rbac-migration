Set-StrictMode -Version Latest

function Invoke-Phase04 {
    <# Role assignment per ogni identity, con attesa di propagazione per quelle nuove. #>
    param([Parameter(Mandatory)][hashtable]$Context)
    foreach ($svc in (Get-TargetServices $Context)) {
        $name = $svc.Name
        if (-not $Context.Principals.ContainsKey($name)) {
            Set-StepStatus -State $Context.State -Phase '04' -Service $name -Status 'skipped' -Detail 'nessuna identity'
            continue
        }
        $principalId = $Context.Principals[$name]
        $roles = @(Get-RequiredRoles -Service $svc -Inventory $Context.Inventory -HostStorageId $Context.HostStorageId)
        $Context["Roles_$name"] = $roles
        Invoke-StepWithFallback -Context $Context -Phase '04' -Service $name -Action {
            if ($principalId -eq 'whatif-principal') { foreach ($r in $roles) { Write-Step "  [WHATIF] $name <- $($r.Role) su $($r.Scope)" 'DarkGray' }; return }
            $created = @()
            foreach ($r in $roles) {
                if (Ensure-RoleAssignment -PrincipalId $principalId -Role $r.Role -Scope $r.Scope) { $created += $r }
            }
            foreach ($r in $created) { Wait-RoleAssignment -PrincipalId $principalId -Role $r.Role -Scope $r.Scope }
        } -Verify { $true } -Detail (($roles | ForEach-Object { $_.Role }) -join ', ') | Out-Null
    }
}
