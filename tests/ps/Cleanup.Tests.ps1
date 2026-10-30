Set-StrictMode -Version Latest

BeforeAll {
    . "$PSScriptRoot\..\..\tools\lib\Cleanup.ps1"
    $script:Tmp = Join-Path $env:TEMP ("cl-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path "$Tmp\work\backup", "$Tmp\work\fallback", "$Tmp\home\.local\bin", "$Tmp\home\.claude", "$Tmp\home\.azure" -Force | Out-Null
    Set-Content "$Tmp\work\backup\x.json" '{}'; Set-Content "$Tmp\work\fallback\y.json" '{}'; Set-Content "$Tmp\work\state.json" '{}'
    Set-Content "$Tmp\home\.local\bin\claude.exe" 'x'; Set-Content "$Tmp\home\.claude\settings.json" '{}'; Set-Content "$Tmp\home\.azure\azureProfile.json" '{}'
}
AfterAll { Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue }

Describe 'Remove-AzureCli' {
    It 'uninstalls via msiexec with the product code from the registry' {
        Reset-CleanupLog
        Mock Get-UninstallEntries { return @([pscustomobject]@{ DisplayName = 'Microsoft Azure CLI (64-bit)'; PSChildName = '{ABCD-1234}'; UninstallString = 'MsiExec.exe /X{ABCD-1234}' }) }
        Mock Start-UninstallProcess { param($FilePath, $ArgumentList) return 0 }
        Remove-AzureCli
        Should -Invoke Start-UninstallProcess -Times 1 -ParameterFilter { $FilePath -eq 'msiexec.exe' -and $ArgumentList -match '/x \{ABCD-1234\} /qn' }
        (Get-CleanupSummary) -join ' ' | Should -Match 'Azure CLI'
    }
    It 'falls back to winget when no registry entry exists' {
        Reset-CleanupLog
        Mock Get-UninstallEntries { return @() }
        Mock Start-UninstallProcess { param($FilePath, $ArgumentList) return 0 }
        Mock Test-CommandAvailable { param($Name) return ($Name -eq 'winget') }
        Remove-AzureCli
        Should -Invoke Start-UninstallProcess -Times 1 -ParameterFilter { $FilePath -eq 'winget' -and $ArgumentList -match 'uninstall --id Microsoft.AzureCLI' }
    }
    It 'records that nothing was installed when neither is available' {
        Reset-CleanupLog
        Mock Get-UninstallEntries { return @() }
        Mock Test-CommandAvailable { return $false }
        Mock Start-UninstallProcess { throw 'must not run' }
        Remove-AzureCli
        (Get-CleanupSummary) -join ' ' | Should -Match 'Azure CLI.*non trovata'
    }
}

Describe 'Remove-ClaudeCode' {
    It 'removes binaries, config dir and only the .local\bin PATH entry' {
        Reset-CleanupLog
        Mock Get-UserPath { return "C:\Windows;$Tmp\home\.local\bin;C:\Tools" }
        $script:newPath = $null
        Mock Set-UserPath { param($Value) $script:newPath = $Value }
        Remove-ClaudeCode -HomeDir "$Tmp\home"
        Test-Path "$Tmp\home\.local\bin\claude.exe" | Should -BeFalse
        Test-Path "$Tmp\home\.claude" | Should -BeFalse
        $script:newPath | Should -Be 'C:\Windows;C:\Tools'
        (Get-CleanupSummary) -join ' ' | Should -Match 'Claude Code'
    }
    It 'is a no-op when Claude Code was never installed' {
        Reset-CleanupLog
        Mock Get-UserPath { return 'C:\Windows' }
        Mock Set-UserPath { throw 'must not change PATH' }
        Remove-ClaudeCode -HomeDir "$Tmp\nothome"
        (Get-CleanupSummary) -join ' ' | Should -Match 'Claude Code.*non installato'
    }
}

Describe 'Remove-AzureProfile' {
    It 'logs out and deletes the .azure folder' {
        Reset-CleanupLog
        Mock Test-CommandAvailable { return $true }
        Mock Start-UninstallProcess { param($FilePath, $ArgumentList) return 0 }
        Remove-AzureProfile -HomeDir "$Tmp\home"
        Should -Invoke Start-UninstallProcess -Times 1 -ParameterFilter { $ArgumentList -match 'logout' }
        Test-Path "$Tmp\home\.azure" | Should -BeFalse
    }
}

Describe 'Remove-WorkDir' {
    It 'keeps the backup folder with -KeepBackup and warns that it holds secrets' {
        Reset-CleanupLog
        Remove-WorkDir -WorkDir "$Tmp\work" -KeepBackup $true
        Test-Path "$Tmp\work\backup\x.json" | Should -BeTrue
        Test-Path "$Tmp\work\fallback" | Should -BeFalse
        Test-Path "$Tmp\work\state.json" | Should -BeFalse
        (Get-CleanupSummary) -join ' ' | Should -Match 'backup.*(chiavi|segreti)'
    }
    It 'removes everything without -KeepBackup' {
        Remove-WorkDir -WorkDir "$Tmp\work" -KeepBackup $false
        Test-Path "$Tmp\work" | Should -BeFalse
    }
}
