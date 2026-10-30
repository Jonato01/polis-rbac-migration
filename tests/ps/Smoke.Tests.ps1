Set-StrictMode -Version Latest

Describe 'repo' {
    It 'has gitignore' {
        Test-Path "$PSScriptRoot\..\..\.gitignore" | Should -BeTrue
    }
    It 'ignores the generated bootstrap with secrets' {
        (Get-Content "$PSScriptRoot\..\..\.gitignore") -contains 'bootstrap/Run-PolisUpdate.ps1' | Should -BeTrue
    }
}
