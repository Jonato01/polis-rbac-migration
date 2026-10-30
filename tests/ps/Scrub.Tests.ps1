Set-StrictMode -Version Latest

BeforeAll {
    $script:RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
    $script:Script = Join-Path $RepoRoot 'tools\scrub\Test-NoSecrets.ps1'
    $script:Tmp = Join-Path $env:TEMP ("scrub-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path "$Tmp\dirty\sub", "$Tmp\clean" -Force | Out-Null
    # valori finti ma con la forma di una chiave reale
    Set-Content "$Tmp\dirty\a.py" ("conn = 'DefaultEndpointsProtocol=https;AccountName=x;AccountKey=" + ('A' * 40) + "==;'")
    Set-Content "$Tmp\dirty\sub\.prodenv" 'DB_PASSWORD=whatever'
    Set-Content "$Tmp\dirty\b.ps1" ('$k = "sk-ant-' + ('x' * 30) + '"')
    Set-Content "$Tmp\dirty\c.md" 'SharedAccessKey=<redacted>   # placeholder, non deve essere segnalato'
    Set-Content "$Tmp\clean\ok.py" 'password = os.getenv("DB_PASSWORD", "")'
    Set-Content "$Tmp\clean\doc.md" 'Endpoint=sb://<namespace>.servicebus.windows.net/;SharedAccessKeyName=...;SharedAccessKey=...'
}
AfterAll { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }

Describe 'Test-NoSecrets.ps1' {
    It 'reports a real-looking account key, an anthropic key and a forbidden env file' {
        $findings = @(& $Script -Path "$Tmp\dirty")
        $findings.Count | Should -Be 3
        ($findings | Where-Object { $_.File -like '*a.py' }).Pattern | Should -Match 'AccountKey'
        ($findings | Where-Object { $_.File -like '*b.ps1' }).Pattern | Should -Match 'sk-ant'
        ($findings | Where-Object { $_.File -like '*.prodenv' }).Pattern | Should -Match 'file vietato'
    }
    It 'does not flag placeholders or os.getenv reads' {
        @(& $Script -Path "$Tmp\clean").Count | Should -Be 0
    }
    It 'returns exit code 1 with -FailOnFindings when something is found' {
        & $Script -Path "$Tmp\dirty" -FailOnFindings | Out-Null
        $LASTEXITCODE | Should -Be 1
    }
    It 'the repository itself is clean' {
        $findings = @(& $Script -Path $RepoRoot)
        ($findings | ForEach-Object { "$($_.File):$($_.Line) $($_.Pattern)" }) -join "`n" | Should -BeNullOrEmpty
    }
}
