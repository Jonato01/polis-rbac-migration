Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\State.ps1"
    . "$PSScriptRoot\..\..\tools\lib\Report.ps1"
    $script:Tmp = Join-Path $env:TEMP ("rep-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Tmp -Force | Out-Null
}
AfterAll { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }

Describe 'New-RunReport' {
    It 'produces markdown with one row per service, exceptions, unmapped, fallback and cleanup sections' {
        $inv = [pscustomobject]@{
            Rg = 'rg-polis'
            Services = @(
                [pscustomobject]@{ Kind = 'containerApp'; Name = 'polis-prod-import-app'; Mapping = [pscustomobject]@{ image = 'polis-import' }; Legacy = $false; Excluded = $false; KeyBased = $true; Identity = 'p1' },
                [pscustomobject]@{ Kind = 'webApp'; Name = 'polis-prod-api-storage'; Mapping = $null; Legacy = $false; Excluded = $true; KeyBased = $true; Identity = $null },
                [pscustomobject]@{ Kind = 'containerApp'; Name = 'mystery-prod'; Mapping = $null; Legacy = $false; Excluded = $false; KeyBased = $true; Identity = $null }
            )
            StorageAccounts = @(
                [pscustomobject]@{ Name = 'polisprodstorage'; Role = 'data'; AllowSharedKey = $false },
                [pscustomobject]@{ Name = 'polisprodfunchost'; Role = 'host'; AllowSharedKey = $true })
            ServiceBus = [pscustomobject]@{ Name = 'polis-messaging-prod'; DisableLocalAuth = $true }
            Acr = [pscustomobject]@{ Name = 'polisregistryprod'; AdminEnabled = $false }
            Unmapped = @([pscustomobject]@{ Name = 'mystery-prod'; Kind = 'containerApp' })
            MissingExpected = @('orchestrator-func')
        }
        $s = New-RunState -WorkDir $Tmp
        Set-StepStatus -State $s -Phase '05' -Service 'polis-prod-import-app' -Status 'done' -Detail 'polisregistryprod.azurecr.io/polis-import:20261030-120000'
        Set-StepStatus -State $s -Phase '07' -Service 'polis-prod-import-app' -Status 'done' -Detail 'Running'
        Set-StepStatus -State $s -Phase '08' -Service '_global' -Status 'skipped' -Detail 'operatore ha rifiutato'
        $out = Join-Path $Tmp 'report.md'
        $path = New-RunReport -Inventory $inv -State $s -OutPath $out -Fallback @([pscustomobject]@{ Phase = '05'; Service = 'polis-prod-import-app'; Attempt = 1; Ok = $true; Transcript = 'fixed dockerfile path' }) -Cleanup @('Azure CLI disinstallata', 'Claude Code rimosso')
        $path | Should -Be $out
        $md = Get-Content $out -Raw
        $md | Should -Match '\| polis-prod-import-app \|'
        $md | Should -Match 'polis-import:20261030-120000'
        $md | Should -Match '(?i)## Eccezioni'
        $md | Should -Match 'polisprodfunchost'
        $md | Should -Match '(?i)## Servizi non mappati'
        $md | Should -Match 'mystery-prod'
        $md | Should -Match 'polis-prod-api-storage'
        $md | Should -Match 'orchestrator-func'
        $md | Should -Match '(?i)## Interventi del fallback'
        $md | Should -Match 'fixed dockerfile path'
        $md | Should -Match '(?i)## Software rimosso'
        $md | Should -Match 'Azure CLI disinstallata'
        $md | Should -Match '(?i)hardening.*skipped|skipped.*hardening|operatore ha rifiutato'
    }
}
