Set-StrictMode -Version Latest

<#
Funzioni pure per mappare le risorse Azure scoperte alle voci di tools/mapping.json.
Nessuna chiamata az qui: tutto testabile offline.
#>

function Get-NormalizedImageName {
    <#
    .SYNOPSIS
    Riduce un riferimento immagine al nome "canonico" usato in mapping.json.
    Rimuove registry, tag/digest, suffissi -prod/-coll e prefissi polis-prod-/polis-coll-.
    #>
    param([string]$Image)
    if ([string]::IsNullOrWhiteSpace($Image)) { return '' }
    $name = $Image.Trim()
    # digest
    $at = $name.IndexOf('@')
    if ($at -ge 0) { $name = $name.Substring(0, $at) }
    # registry (tutto fino all'ultimo '/')
    $slash = $name.LastIndexOf('/')
    if ($slash -ge 0) { $name = $name.Substring($slash + 1) }
    # tag
    $colon = $name.IndexOf(':')
    if ($colon -ge 0) { $name = $name.Substring(0, $colon) }
    $name = $name.ToLowerInvariant()
    $name = $name -replace '^polis-(prod|coll)-', 'polis-'
    $name = $name -replace '-(prod|coll)$', ''
    $name = $name -replace '-(prod|coll)-', '-'   # es. autogen-prod-pdf-import
    return $name
}

function Test-ExcludedWebApp {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][pscustomobject]$Mapping
    )
    $excluded = @()
    if ($Mapping.PSObject.Properties['excludedWebApps']) { $excluded = @($Mapping.excludedWebApps) }
    foreach ($e in $excluded) {
        if ($Name -imatch [regex]::Escape($e)) { return $true }
    }
    return $false
}

function Resolve-ServiceMapping {
    <#
    .SYNOPSIS
    Restituisce la voce di mapping per una risorsa, o $null.
    $Resource: @{ Kind = 'containerApp'|'webApp'|'functionApp'; Name; Image }
    #>
    param(
        [Parameter(Mandatory)][pscustomobject]$Resource,
        [Parameter(Mandatory)][pscustomobject]$Mapping
    )
    switch ($Resource.Kind) {
        'containerApp' {
            $img = Get-NormalizedImageName $Resource.Image
            if (-not $img) { return $null }
            foreach ($e in @($Mapping.containerApps)) {
                if ($e.image -eq $img) { return $e }
            }
            return $null
        }
        'webApp' {
            if (Test-ExcludedWebApp -Name $Resource.Name -Mapping $Mapping) { return $null }
            foreach ($e in @($Mapping.webApps)) {
                if ($Resource.Name -imatch $e.match) { return $e }
            }
            return $null
        }
        'functionApp' {
            foreach ($e in @($Mapping.functionApps)) {
                if ($Resource.Name -imatch $e.match) { return $e }
            }
            return $null
        }
        default { throw "Kind non gestito: $($Resource.Kind)" }
    }
}
