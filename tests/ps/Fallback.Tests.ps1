Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Fallback.ps1"
    $script:ToolsDir = (Resolve-Path "$PSScriptRoot\..\..\tools").Path
    $script:Tmp = Join-Path $env:TEMP ("fb-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Tmp -Force | Out-Null
}
AfterAll { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }

Describe 'Test-AnthropicKey' {
    It 'is true on HTTP 200' {
        Mock Invoke-AnthropicProbe { return 200 }
        Test-AnthropicKey -Key 'sk-ant-test' | Should -BeTrue
    }
    It 'is false on 401 or empty key' {
        Mock Invoke-AnthropicProbe { return 401 }
        Test-AnthropicKey -Key 'sk-ant-test' | Should -BeFalse
        Test-AnthropicKey -Key '' | Should -BeFalse
    }
}

Describe 'New-FallbackPrompt' {
    It 'contains phase, service, error, paths and the five rules' {
        $ctx = @{ Rg = 'rg-polis'; Sub = 'sub1'; WorkDir = $Tmp; ToolsDir = $ToolsDir; RepoRoot = (Split-Path $ToolsDir -Parent) }
        $p = New-FallbackPrompt -Context $ctx -Phase '05' -Service 'polis-prod-import-app' -ErrorText 'az acr build fallito: Dockerfile not found' -Attempt 1 -ResultPath (Join-Path $Tmp 'r.json')
        $p | Should -Match 'Fase: 05'
        $p | Should -Match 'polis-prod-import-app'
        $p | Should -Match 'Dockerfile not found'
        $p | Should -Match 'rg-polis'
        $p | Should -Match 'mapping.json'
        $p | Should -Match 'non disabilitare chiavi'
        $p | Should -Match 'non cancellare risorse'
        $p | Should -Match 'non toccare servizi fuori perimetro'
        $p | Should -Match 'non toccare polis-api-storage'
        $p | Should -Match 'scrivi il risultato in'
        $p | Should -Match ([regex]::Escape((Join-Path $Tmp 'r.json')))
    }
}

Describe 'Invoke-ClaudeFallback' {
    It 'runs claude with the prompt, reads the result file and reports Ok' {
        $ctx = @{ Rg = 'rg-polis'; Sub = 'sub1'; WorkDir = $Tmp; ToolsDir = $ToolsDir; RepoRoot = (Split-Path $ToolsDir -Parent); Options = @{ AnthropicApiKey = 'sk-ant-test' } }
        Mock Invoke-AnthropicProbe { return 200 }
        Mock Ensure-ClaudeCode { return 'C:\fake\claude.exe' }
        Mock Invoke-ClaudeProcess {
            param($Exe, $Prompt, $Cwd, $ResultPath)
            @{ ok = $true; summary = 'fixed dockerfile path' } | ConvertTo-Json | Set-Content $ResultPath
            return @{ ExitCode = 0; StdOut = '{"result":"done"}' }
        }
        $r = Invoke-ClaudeFallback -Context $ctx -Phase '05' -Service 'polis-prod-import-app' -ErrorText 'boom' -Attempt 1
        $r.Ok | Should -BeTrue
        $r.Summary | Should -Be 'fixed dockerfile path'
        Test-Path $r.ResultPath | Should -BeTrue
        Should -Invoke Invoke-ClaudeProcess -Times 1 -ParameterFilter { $Prompt -match 'boom' -and $Cwd -eq $ToolsDir }
    }
    It 'is not Ok when claude writes no result file' {
        $ctx = @{ Rg = 'rg'; Sub = 's'; WorkDir = $Tmp; ToolsDir = $ToolsDir; RepoRoot = (Split-Path $ToolsDir -Parent); Options = @{ AnthropicApiKey = 'sk-ant-test' } }
        Mock Invoke-AnthropicProbe { return 200 }
        Mock Ensure-ClaudeCode { return 'C:\fake\claude.exe' }
        Mock Invoke-ClaudeProcess { return @{ ExitCode = 1; StdOut = 'error' } }
        (Invoke-ClaudeFallback -Context $ctx -Phase '06' -Service 'x' -ErrorText 'e' -Attempt 2).Ok | Should -BeFalse
    }
    It 'skips (Ok=false, Skipped=true) when no key is available' {
        $ctx = @{ Rg = 'rg'; Sub = 's'; WorkDir = $Tmp; ToolsDir = $ToolsDir; RepoRoot = (Split-Path $ToolsDir -Parent); Options = @{ AnthropicApiKey = '' } }
        Mock Ensure-ClaudeCode { throw 'must not install' }
        Mock Read-FallbackKey { return '' }
        $r = Invoke-ClaudeFallback -Context $ctx -Phase '06' -Service 'x' -ErrorText 'e' -Attempt 1
        $r.Ok | Should -BeFalse
        $r.Skipped | Should -BeTrue
    }
}

Describe 'fallback assets' {
    It 'ships CLAUDE.md and prompt.txt with the required rules' {
        Test-Path (Join-Path $ToolsDir 'fallback\CLAUDE.md') | Should -BeTrue
        $t = Get-Content (Join-Path $ToolsDir 'fallback\prompt.txt') -Raw
        foreach ($rule in 'non disabilitare chiavi', 'non cancellare risorse', 'non toccare servizi fuori perimetro', 'non toccare polis-api-storage', 'scrivi il risultato in') { $t | Should -Match $rule }
    }
}
