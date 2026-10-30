Set-StrictMode -Version Latest

Describe 'Sync-Shared.ps1' {
    BeforeAll {
        $script:RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
        $script:Script = Join-Path $RepoRoot 'tools\Sync-Shared.ps1'
        $script:Tmp = Join-Path $env:TEMP ("syncshared-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path "$Tmp\a", "$Tmp\b\sub" -Force | Out-Null
    }
    AfterAll { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }

    It 'copies the shared helper into every target passed explicitly' {
        & $Script -Targets @("$Tmp\a", "$Tmp\b\sub")
        $src = Get-Content (Join-Path $RepoRoot 'src\_shared\azure_clients.py') -Raw
        (Get-Content "$Tmp\a\azure_clients.py" -Raw) | Should -Be $src
        (Get-Content "$Tmp\b\sub\azure_clients.py" -Raw) | Should -Be $src
    }

    It 'reads targets from a mapping file when given' {
        $mapping = Join-Path $Tmp 'mapping.json'
        @{ sharedTargets = @("$Tmp\a") } | ConvertTo-Json | Set-Content $mapping -Encoding UTF8
        Remove-Item "$Tmp\a\azure_clients.py" -Force -ErrorAction SilentlyContinue
        & $Script -MappingPath $mapping
        Test-Path "$Tmp\a\azure_clients.py" | Should -BeTrue
    }
}
