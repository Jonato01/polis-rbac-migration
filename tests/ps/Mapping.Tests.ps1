Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Mapping.ps1"
    $script:Mapping = [pscustomobject]@{
        containerApps = @(
            [pscustomobject]@{ image = 'polis-import'; context = 'src/import_fatture'; dockerfile = 'container_app/Dockerfile' },
            [pscustomobject]@{ image = 'sftp-service'; context = 'src/import-sal/services/sftp'; dockerfile = 'Dockerfile' },
            [pscustomobject]@{ image = 'servicebus-bridge'; context = 'src/servicebus-bridge'; dockerfile = 'Dockerfile' }
        )
        webApps = @(
            [pscustomobject]@{ match = 'sal-import$|sal-sftp-api'; repo = 'polis-sal-sftp-api' },
            [pscustomobject]@{ match = 'api-database'; repo = 'polis-api-database' }
        )
        functionApps = @(
            [pscustomobject]@{ match = 'sftp-upload'; context = 'src/polis-sal-sftp-sync' }
        )
        excludedWebApps = @('api-storage', 'sal-import-web')
    }
}

Describe 'Get-NormalizedImageName' {
    It 'strips registry, tag and polis-prod- prefix' {
        Get-NormalizedImageName 'polisregistryprod.azurecr.io/polis-prod-import:20260331' | Should -Be 'polis-import'
    }
    It 'strips -prod suffix' {
        Get-NormalizedImageName 'polisregistryprod.azurecr.io/sftp-service-prod:latest' | Should -Be 'sftp-service'
    }
    It 'keeps plain names' {
        Get-NormalizedImageName 'polisregistry.azurecr.io/autogen-pdf-import:latest' | Should -Be 'autogen-pdf-import'
    }
    It 'strips -coll suffix' {
        Get-NormalizedImageName 'x.azurecr.io/fatture-service-coll:v1' | Should -Be 'fatture-service'
    }
    It 'handles digest references and upper-case registry' {
        Get-NormalizedImageName 'POLISREGISTRYPROD.azurecr.io/servicebus-bridge@sha256:abc123' | Should -Be 'servicebus-bridge'
    }
    It 'returns empty string for empty input' {
        Get-NormalizedImageName '' | Should -Be ''
    }
}

Describe 'Resolve-ServiceMapping' {
    It 'maps a container app by normalized image' {
        $r = [pscustomobject]@{ Kind = 'containerApp'; Name = 'polis-prod-import-app'; Image = 'polisregistryprod.azurecr.io/polis-prod-import:1' }
        (Resolve-ServiceMapping -Resource $r -Mapping $Mapping).context | Should -Be 'src/import_fatture'
    }
    It 'maps a web app by name regex' {
        $r = [pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-sal-import'; Image = $null }
        (Resolve-ServiceMapping -Resource $r -Mapping $Mapping).repo | Should -Be 'polis-sal-sftp-api'
    }
    It 'does not map an excluded web app even if a regex would match' {
        $r = [pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-sal-import-web'; Image = $null }
        Resolve-ServiceMapping -Resource $r -Mapping $Mapping | Should -BeNullOrEmpty
    }
    It 'maps a function app by name regex' {
        $r = [pscustomobject]@{ Kind = 'functionApp'; Name = 'polis-prod-sftp-upload'; Image = $null }
        (Resolve-ServiceMapping -Resource $r -Mapping $Mapping).context | Should -Be 'src/polis-sal-sftp-sync'
    }
    It 'returns null for unknown container image' {
        $r = [pscustomobject]@{ Kind = 'containerApp'; Name = 'x'; Image = 'reg.azurecr.io/unknown-thing:1' }
        Resolve-ServiceMapping -Resource $r -Mapping $Mapping | Should -BeNullOrEmpty
    }
}

Describe 'Test-ExcludedWebApp' {
    It 'is true for api-storage' { Test-ExcludedWebApp -Name 'polis-prod-api-storage' -Mapping $Mapping | Should -BeTrue }
    It 'is false for sal-import' { Test-ExcludedWebApp -Name 'polis-prod-sal-import' -Mapping $Mapping | Should -BeFalse }
}
