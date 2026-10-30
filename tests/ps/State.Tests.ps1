Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\State.ps1"
    $script:Tmp = Join-Path $env:TEMP ("state-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Tmp -Force | Out-Null
}
AfterAll { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }

Describe 'run state' {
    It 'starts empty when no state file exists' {
        $s = New-RunState -WorkDir $Tmp
        Get-StepStatus -State $s -Phase '05' -Service 'polis-prod-import-app' | Should -Be 'pending'
    }
    It 'round-trips through Save-RunState / New-RunState' {
        $s = New-RunState -WorkDir $Tmp
        Set-StepStatus -State $s -Phase '05' -Service 'polis-prod-import-app' -Status 'done' -Detail 'image polis-import:20261030-1200'
        Set-StepStatus -State $s -Phase '06' -Service 'polis-prod-import-app' -Status 'failed' -Detail 'boom'
        Save-RunState -State $s
        $s2 = New-RunState -WorkDir $Tmp
        Get-StepStatus -State $s2 -Phase '05' -Service 'polis-prod-import-app' | Should -Be 'done'
        Get-StepStatus -State $s2 -Phase '06' -Service 'polis-prod-import-app' | Should -Be 'failed'
        (Get-StepDetail -State $s2 -Phase '06' -Service 'polis-prod-import-app') | Should -Be 'boom'
    }
    It 'rejects unknown status values' {
        $s = New-RunState -WorkDir $Tmp
        { Set-StepStatus -State $s -Phase '05' -Service 'x' -Status 'weird' } | Should -Throw
    }
    It 'lists services with a given status in a phase' {
        $s = New-RunState -WorkDir (Join-Path $Tmp 'b')
        Set-StepStatus -State $s -Phase '07' -Service 'a' -Status 'done'
        Set-StepStatus -State $s -Phase '07' -Service 'b' -Status 'failed'
        Set-StepStatus -State $s -Phase '07' -Service 'c' -Status 'failed'
        (Get-ServicesByStatus -State $s -Phase '07' -Status 'failed') | Should -Be @('b', 'c')
    }
    It 'records run metadata (RunId, StartedAt) once' {
        $s = New-RunState -WorkDir (Join-Path $Tmp 'c')
        $s.RunId | Should -Match '^\d{8}-\d{6}$'
        Save-RunState -State $s
        (New-RunState -WorkDir (Join-Path $Tmp 'c')).RunId | Should -Be $s.RunId
    }
}
