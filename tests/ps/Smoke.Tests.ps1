Set-StrictMode -Version Latest

Describe 'repo' {
    It 'has gitignore' {
        Test-Path "$PSScriptRoot\..\..\.gitignore" | Should -BeTrue
    }
    It 'ignores the generated bootstrap with secrets' {
        (Get-Content "$PSScriptRoot\..\..\.gitignore") -contains 'bootstrap/Run-PolisUpdate.ps1' | Should -BeTrue
    }
    It 'keeps every operator-side .ps1 ASCII-only (PowerShell 5.1 reads BOM-less files as ANSI)' {
        $bad = @()
        foreach ($f in (Get-ChildItem "$PSScriptRoot\..\..\tools", "$PSScriptRoot\..\..\bootstrap" -Recurse -Include *.ps1 -ErrorAction SilentlyContinue)) {
            $bytes = [IO.File]::ReadAllBytes($f.FullName)
            if (@($bytes | Where-Object { $_ -gt 127 }).Count -gt 0) { $bad += $f.Name }
        }
        $bad | Should -BeNullOrEmpty
    }
}
