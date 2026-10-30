Set-StrictMode -Version Latest

function Get-ImageRepository {
    <# Repository dell'immagine corrente (senza registry e tag), per non cambiare naming in prod. #>
    param([string]$Image, [string]$Fallback)
    if ([string]::IsNullOrWhiteSpace($Image)) { return $Fallback }
    $n = $Image
    $at = $n.IndexOf('@'); if ($at -ge 0) { $n = $n.Substring(0, $at) }
    $slash = $n.IndexOf('/'); if ($slash -ge 0) { $n = $n.Substring($slash + 1) }
    $colon = $n.LastIndexOf(':'); if ($colon -ge 0) { $n = $n.Substring(0, $colon) }
    if (-not $n) { return $Fallback }
    return $n
}

function Invoke-Phase05 {
    <# Build cloud + update immagine (container app), share host + zip deploy (function), check commit SCM (web app). #>
    param([Parameter(Mandatory)][hashtable]$Context)
    $tag = $Context.State.RunId
    foreach ($svc in (Get-TargetServices $Context)) {
        $name = $svc.Name
        $m = $svc.Mapping
        if (Test-MappingFlag $m 'settingsOnly') {
            Set-StepStatus -State $Context.State -Phase '05' -Service $name -Status 'done' -Detail 'solo settings, nessun deploy'
            continue
        }
        switch ($svc.Kind) {
            'containerApp' {
                if (-not $Context.Inventory.Acr) { Set-StepStatus -State $Context.State -Phase '05' -Service $name -Status 'failed' -Detail 'nessun ACR trovato'; continue }
                $repo = Get-ImageRepository -Image $svc.Image -Fallback $m.image
                $acr = $Context.Inventory.Acr.Name
                $Context["Image_$name"] = $null
                Invoke-StepWithFallback -Context $Context -Phase '05' -Service $name -Action {
                    $img = Build-ContainerImage -Acr $acr -Repository $repo -Tag $tag -Context $m.context -Dockerfile $m.dockerfile -RepoRoot $Context.RepoRoot
                    Update-ContainerAppImage -Rg $Context.Rg -Name $name -Image $img
                    $Context["Image_$name"] = $img
                } -Verify { $true } -Detail "$acr.azurecr.io/${repo}:$tag" | Out-Null
            }
            'functionApp' {
                Invoke-StepWithFallback -Context $Context -Phase '05' -Service $name -Action {
                    if ((Test-MappingFlag $m 'hostStorage') -and $Context.HostStorageAccount) {
                        $cur = $null
                        if ($svc.Settings.ContainsKey('WEBSITE_CONTENTAZUREFILECONNECTIONSTRING')) { $cur = (Get-ConnectionStringParts $svc.Settings['WEBSITE_CONTENTAZUREFILECONNECTIONSTRING']).AccountName }
                        $mountsOnData = @($svc.Mounts | Where-Object { $_.accountName -ne $Context.HostStorageAccount })
                        if ($cur -ne $Context.HostStorageAccount -or $mountsOnData.Count -gt 0) {
                            Move-FunctionShares -Rg $Context.Rg -FunctionService $svc -HostAccount $Context.HostStorageAccount
                        }
                    }
                    if ($m.PSObject.Properties['context'] -and $m.context) {
                        Publish-FunctionZip -Rg $Context.Rg -Name $name -Context (Join-Path $Context.RepoRoot $m.context) -WorkDir $Context.WorkDir | Out-Null
                    }
                } -Verify { $true } -Detail "zip deploy $($m.context)" | Out-Null
            }
            'webApp' {
                $sha = if ($m.PSObject.Properties['expectedCommit']) { [string]$m.expectedCommit } else { '' }
                if (-not $sha) {
                    Set-StepStatus -State $Context.State -Phase '05' -Service $name -Status 'skipped' -Detail 'expectedCommit non impostato nel mapping: codice identity non verificabile'
                    continue
                }
                if (Test-WebAppDeployedCommit -Rg $Context.Rg -Name $name -Sha $sha) {
                    Set-StepStatus -State $Context.State -Phase '05' -Service $name -Status 'done' -Detail "commit $sha presente (deploy SCM)"
                } else {
                    Set-StepStatus -State $Context.State -Phase '05' -Service $name -Status 'skipped' -Detail "commit $sha NON trovato tra i deployment: rilasciare il codice via SCM prima dello switch"
                }
            }
        }
    }
    Save-RunState -State $Context.State
}
