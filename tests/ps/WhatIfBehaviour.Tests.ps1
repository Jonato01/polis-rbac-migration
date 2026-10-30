Set-StrictMode -Version Latest

BeforeAll {
    $script:Tools = (Resolve-Path "$PSScriptRoot\..\..\tools").Path
    foreach ($lib in 'Az', 'Rbac', 'Fallback') { . (Join-Path $Tools "lib\$lib.ps1") }
}

Describe 'RBAC in WhatIf mode' {
    AfterEach { Set-AzWhatIf $false }
    It 'Ensure-RoleAssignment reports nothing created and Wait-RoleAssignment returns at once' {
        Set-AzWhatIf $true
        $script:calls = 0
        Mock Invoke-AzProcess { $script:calls++; return @{ ExitCode = 0; StdOut = '[]'; StdErr = '' } }
        Ensure-RoleAssignment -PrincipalId 'p' -Role 'AcrPull' -Scope '/s' | Should -BeFalse
        { Wait-RoleAssignment -PrincipalId 'p' -Role 'AcrPull' -Scope '/s' -TimeoutSec 1 -PollSec 0 } | Should -Not -Throw
        $script:calls | Should -BeLessOrEqual 2
    }
}

Describe 'Fallback in NonInteractive mode' {
    It 'never prompts for a key and reports Skipped' {
        $ctx = @{ Rg = 'rg'; Sub = 's'; WorkDir = $env:TEMP; ToolsDir = $Tools; RepoRoot = (Split-Path $Tools -Parent); Options = @{ AnthropicApiKey = ''; NonInteractive = $true } }
        Mock Read-FallbackKey { throw 'must not prompt' }
        Mock Ensure-ClaudeCode { throw 'must not install' }
        Mock Invoke-AnthropicProbe { return 401 }
        $r = Invoke-ClaudeFallback -Context $ctx -Phase '04' -Service 'x' -ErrorText 'e' -Attempt 1
        $r.Skipped | Should -BeTrue
        $r.Ok | Should -BeFalse
    }
    It 'still prompts when interactive' {
        $ctx = @{ Rg = 'rg'; Sub = 's'; WorkDir = $env:TEMP; ToolsDir = $Tools; RepoRoot = (Split-Path $Tools -Parent); Options = @{ AnthropicApiKey = ''; NonInteractive = $false } }
        Mock Read-FallbackKey { return '' }
        Mock Invoke-AnthropicProbe { return 401 }
        (Invoke-ClaudeFallback -Context $ctx -Phase '04' -Service 'x' -ErrorText 'e' -Attempt 1).Skipped | Should -BeTrue
        Should -Invoke Read-FallbackKey -Times 1
    }
}
