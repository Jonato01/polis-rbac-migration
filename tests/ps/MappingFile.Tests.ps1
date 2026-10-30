Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Mapping.ps1"
    $script:RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
    $script:MappingPath = Join-Path $RepoRoot 'tools\mapping.json'
    $script:Mapping = if (Test-Path $MappingPath) { Get-Content $MappingPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
    $script:Shared = Get-Content (Join-Path $RepoRoot 'src\_shared\azure_clients.py') -Raw
}

Describe 'tools/mapping.json' {
    It 'exists and is valid JSON with the expected sections' {
        $Mapping | Should -Not -BeNullOrEmpty
        foreach ($s in 'sharedTargets', 'containerApps', 'webApps', 'functionApps', 'excludedWebApps') {
            $Mapping.PSObject.Properties[$s] | Should -Not -BeNullOrEmpty -Because "sezione $s"
        }
    }

    It 'every container app context exists and contains its dockerfile' {
        foreach ($e in $Mapping.containerApps) {
            $ctx = Join-Path $RepoRoot $e.context
            Test-Path $ctx | Should -BeTrue -Because "context $($e.context)"
            Test-Path (Join-Path $ctx $e.dockerfile) | Should -BeTrue -Because "$($e.image): $($e.context)/$($e.dockerfile)"
        }
    }

    It 'every function app context exists' {
        foreach ($e in ($Mapping.functionApps | Where-Object { $_.PSObject.Properties['context'] })) {
            Test-Path (Join-Path $RepoRoot $e.context) | Should -BeTrue -Because $e.context
        }
    }

    It 'every sharedTarget holds an identical copy of azure_clients.py' {
        foreach ($t in $Mapping.sharedTargets) {
            $copy = Join-Path $RepoRoot (Join-Path $t 'azure_clients.py')
            Test-Path $copy | Should -BeTrue -Because $t
            (Get-Content $copy -Raw) | Should -Be $Shared -Because "$t divergente"
        }
    }

    It 'covers the in-use pipeline images' {
        $images = @($Mapping.containerApps | ForEach-Object { $_.image })
        foreach ($img in 'polis-import', 'autogen-pdf-import', 'servicebus-bridge') {
            $images | Should -Contain $img
        }
        ($Mapping.containerApps | Where-Object image -eq 'polis-import').legacy | Should -BeFalse
    }

    It 'covers the legacy import-sal images as legacy' {
        $legacy = @($Mapping.containerApps | Where-Object { $_.legacy } | ForEach-Object { $_.image })
        foreach ($img in 'import-sal-orchestrator', 'sftp-service', 'sftp-bulk-sync', 'db-backup-service', 'container-backup-service',
                         'documentation-service', 'storage-cleanup', 'personale-service', 'fatture-service', 'db-rollback-sal',
                         'aq-service', 'aq-missing-service', 'bdc-service', 'bdc-missing-service', 'fatture-pdf-service',
                         'iva-indetraibile-service', 'lettere-incarico-service', 'nota-metodologica-service',
                         'nota-trasmissione-service', 'passaggi-livello-service', 'timesheet-service', 'vap-service') {
            $legacy | Should -Contain $img
        }
    }

    It 'container image names are unique and already normalized' {
        $images = @($Mapping.containerApps | ForEach-Object { $_.image })
        ($images | Select-Object -Unique).Count | Should -Be $images.Count
        foreach ($img in $images) { Get-NormalizedImageName $img | Should -Be $img }
    }

    It 'maps the production web and function app names' {
        (Resolve-ServiceMapping -Resource ([pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-sal-import'; Image = $null }) -Mapping $Mapping).repo | Should -Be 'polis-sal-sftp-api'
        (Resolve-ServiceMapping -Resource ([pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-api-database'; Image = $null }) -Mapping $Mapping).repo | Should -Be 'polis-api-database'
        (Resolve-ServiceMapping -Resource ([pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-api-ai'; Image = $null }) -Mapping $Mapping).repo | Should -Be 'polis-ai-2'
        (Resolve-ServiceMapping -Resource ([pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-frontend'; Image = $null }) -Mapping $Mapping).repo | Should -Be 'polis-frontend'
        (Resolve-ServiceMapping -Resource ([pscustomobject]@{ Kind = 'functionApp'; Name = 'polis-prod-sftp-upload'; Image = $null }) -Mapping $Mapping).context | Should -Be 'src/polis-sal-sftp-sync'
        (Resolve-ServiceMapping -Resource ([pscustomobject]@{ Kind = 'functionApp'; Name = 'polis-prod-orchestrator-func'; Image = $null }) -Mapping $Mapping).settingsOnly | Should -BeTrue
    }

    It 'never maps the excluded web apps' {
        foreach ($n in 'polis-prod-api-storage', 'polis-prod-sal-import-web', 'polis-api-storage', 'polis-sal-import-web') {
            Resolve-ServiceMapping -Resource ([pscustomobject]@{ Kind = 'webApp'; Name = $n; Image = $null }) -Mapping $Mapping | Should -BeNullOrEmpty -Because $n
        }
    }

    It 'every entry with needsStorage/needsServiceBus has add/remove and errorPatterns' {
        foreach ($e in @($Mapping.containerApps) + @($Mapping.webApps) + @($Mapping.functionApps)) {
            $label = if ($e.PSObject.Properties['image']) { $e.image } else { $e.match }
            $e.PSObject.Properties['add'] | Should -Not -BeNullOrEmpty -Because $label
            $e.PSObject.Properties['remove'] | Should -Not -BeNullOrEmpty -Because $label
        }
        foreach ($e in $Mapping.containerApps) { @($e.errorPatterns).Count | Should -BeGreaterThan 0 }
    }
}
