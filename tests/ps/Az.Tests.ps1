Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Az.ps1"
}

Describe 'Test-AzMutating' {
    It 'is true for mutating commands' {
        Test-AzMutating @('containerapp', 'update', '--name', 'x') | Should -BeTrue
        Test-AzMutating @('role', 'assignment', 'create') | Should -BeTrue
        Test-AzMutating @('storage', 'account', 'keys', 'renew') | Should -BeTrue
        Test-AzMutating @('webapp', 'config', 'appsettings', 'set') | Should -BeTrue
        Test-AzMutating @('webapp', 'config', 'appsettings', 'delete') | Should -BeTrue
        Test-AzMutating @('acr', 'build') | Should -BeTrue
        Test-AzMutating @('functionapp', 'deployment', 'source', 'config-zip') | Should -BeTrue
    }
    It 'is false for read-only commands' {
        Test-AzMutating @('containerapp', 'show', '--name', 'x') | Should -BeFalse
        Test-AzMutating @('role', 'assignment', 'list') | Should -BeFalse
        Test-AzMutating @('account', 'show') | Should -BeFalse
        Test-AzMutating @('webapp', 'config', 'appsettings', 'list') | Should -BeFalse
    }
}

Describe 'Invoke-Az' {
    BeforeEach { Set-AzWhatIf $false }
    It 'parses json output of the az process' {
        Mock Invoke-AzProcess { return @{ ExitCode = 0; StdOut = '{"name":"x","n":3}'; StdErr = '' } }
        $r = Invoke-Az @('account', 'show')
        $r.name | Should -Be 'x'
        $r.n | Should -Be 3
    }
    It 'throws on non-zero exit including stderr text' {
        Mock Invoke-AzProcess { return @{ ExitCode = 1; StdOut = ''; StdErr = 'ERROR: boom' } }
        { Invoke-Az @('account', 'show') } | Should -Throw '*boom*'
    }
    It 'returns null instead of throwing with -AllowFailure' {
        Mock Invoke-AzProcess { return @{ ExitCode = 1; StdOut = ''; StdErr = 'nope' } }
        Invoke-Az @('containerapp', 'show') -AllowFailure | Should -BeNullOrEmpty
    }
    It 'skips mutating commands in WhatIf mode and does not call az' {
        Set-AzWhatIf $true
        Mock Invoke-AzProcess { return @{ ExitCode = 0; StdOut = '{}'; StdErr = '' } }
        Invoke-Az @('containerapp', 'update', '--name', 'x') | Should -BeNullOrEmpty
        Should -Invoke Invoke-AzProcess -Times 0
    }
    It 'still runs read-only commands in WhatIf mode' {
        Set-AzWhatIf $true
        Mock Invoke-AzProcess { return @{ ExitCode = 0; StdOut = '[]'; StdErr = '' } }
        Invoke-Az @('containerapp', 'list') | Out-Null
        Should -Invoke Invoke-AzProcess -Times 1
    }
    It 'masks values after --settings / --secrets / --set-env-vars in the log line' {
        $script:logged = @()
        Mock Write-AzLog { param($Message) $script:logged += $Message }
        Mock Invoke-AzProcess { return @{ ExitCode = 0; StdOut = '{}'; StdErr = '' } }
        Invoke-Az @('webapp', 'config', 'appsettings', 'set', '--settings', 'KEY=supersecretvalue', '--name', 'app') | Out-Null
        ($script:logged -join ' ') | Should -Not -Match 'supersecretvalue'
        ($script:logged -join ' ') | Should -Match 'KEY=\*\*\*'
    }
    It 'returns raw text with -Raw' {
        Mock Invoke-AzProcess { return @{ ExitCode = 0; StdOut = 'plain text'; StdErr = '' } }
        Invoke-Az @('acr', 'build') -Raw | Should -Be 'plain text'
    }
}
