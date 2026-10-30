Set-StrictMode -Version Latest

BeforeAll {
    $script:RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
    $script:Template = Join-Path $RepoRoot 'bootstrap\Run-PolisUpdate.template.ps1'
    $script:Generator = Join-Path $RepoRoot 'bootstrap\New-Bootstrap.ps1'
    $script:Tmp = Join-Path $env:TEMP ("boot-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $Tmp -Force | Out-Null
}
AfterAll { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }

Describe 'Run-PolisUpdate.template.ps1' {
    It 'parses without errors' {
        $t = $null; $e = $null
        [System.Management.Automation.Language.Parser]::ParseFile($Template, [ref]$t, [ref]$e) | Out-Null
        @($e).Count | Should -Be 0
    }
    It 'carries the placeholders the generator fills' {
        $c = Get-Content $Template -Raw
        foreach ($p in '__REPO_ZIP_URL__', '__ZIP_SHA256__', '__ANTHROPIC_KEY__', '__COMMIT__') { $c | Should -Match ([regex]::Escape($p)) }
    }
    It 'always runs the cleanup phase in a finally block' {
        $c = Get-Content $Template -Raw
        $c | Should -Match '(?s)finally\s*\{.*10-Cleanup\.ps1'
    }
    It 'verifies the zip hash before extracting and installs Azure CLI silently' {
        $c = Get-Content $Template -Raw
        $c | Should -Match 'Get-FileHash'
        $c | Should -Match 'aka\.ms/installazurecliwindowsx64'
        $c | Should -Match 'msiexec'
        $c | Should -Match 'winget.*install --id Microsoft\.AzureCLI'
        $c | Should -Match 'Expand-Archive'
    }
    It 'elevates itself when not running as administrator' {
        (Get-Content $Template -Raw) | Should -Match 'Start-Process powershell.* -Verb RunAs'
    }
}

Describe 'New-Bootstrap.ps1' {
    It 'downloads the pinned commit zip, computes its hash and fills every placeholder' {
        $zip = Join-Path $Tmp 'repo.zip'
        Set-Content -Path (Join-Path $Tmp 'payload.txt') -Value 'hello'
        Compress-Archive -Path (Join-Path $Tmp 'payload.txt') -DestinationPath $zip -Force
        $expectedHash = (Get-FileHash $zip -Algorithm SHA256).Hash
        $out = Join-Path $Tmp 'Run-PolisUpdate.ps1'
        Mock Invoke-WebRequest { param($Uri, $OutFile) Copy-Item $zip $OutFile -Force }
        & $Generator -Commit 'abcdef1234567890abcdef1234567890abcdef12' -AnthropicApiKey 'sk-ant-FAKE-for-test' -OutFile $out -RepoSlug 'Jonato01/polis-rbac-migration'
        Test-Path $out | Should -BeTrue
        $c = Get-Content $out -Raw
        $c | Should -Match $expectedHash
        $c | Should -Match 'sk-ant-FAKE-for-test'
        $c | Should -Match 'https://github.com/Jonato01/polis-rbac-migration/archive/abcdef1234567890abcdef1234567890abcdef12\.zip'
        $c | Should -Not -Match '__[A-Z_]+__'
        $t = $null; $e = $null
        [System.Management.Automation.Language.Parser]::ParseFile($out, [ref]$t, [ref]$e) | Out-Null
        @($e).Count | Should -Be 0
    }
    It 'refuses a commit that is not a full 40-char sha' {
        { & $Generator -Commit 'abc' -AnthropicApiKey 'k' -OutFile (Join-Path $Tmp 'x.ps1') } | Should -Throw
    }
}
