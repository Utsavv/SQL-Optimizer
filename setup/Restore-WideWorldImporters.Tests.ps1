<#
Pester tests for Restore-WideWorldImporters.ps1 (Issue 8).

Run with:  Invoke-Pester -Path ./setup/Restore-WideWorldImporters.Tests.ps1

These cover the ONLINE, SINGLE_USER, RESTORING and failed-restore states with a
mocked SQL layer (no server required):
  * the RESTORING state issues NO ALTER before the restore;
  * a failed restore preserves the PRIMARY error and never runs a cleanup ALTER
    that would mask it;
  * ONLINE/SINGLE_USER take the single-user → restore → multi-user path;
  * a successful restore ends ONLINE + MULTI_USER.
#>

BeforeAll {
    # Dot-source under test (the guard at the bottom of the script prevents auto-run).
    . $PSScriptRoot/Restore-WideWorldImporters.ps1 -BackupPath 'C:\x.bak' -ServerInstance 'localhost' -ErrorAction SilentlyContinue
}

Describe 'Get-RecoveryPlan (pure decision logic)' {

    It 'issues NO pre-restore ALTER for a RESTORING database' {
        $plan = Get-RecoveryPlan -State 'RESTORING' -UserAccess 'SINGLE_USER'
        $plan.PreRestore | Should -BeNullOrEmpty
        $plan.RestoreWith | Should -Contain 'RECOVERY'
        $plan.PostRestore | Should -Contain 'SET_MULTI_USER'
    }

    It 'takes single-user -> restore -> multi-user for an ONLINE database' {
        $plan = Get-RecoveryPlan -State 'ONLINE' -UserAccess 'MULTI_USER'
        $plan.PreRestore | Should -Contain 'SET_SINGLE_USER'
        $plan.PostRestore | Should -Contain 'SET_MULTI_USER'
    }

    It 'handles an already SINGLE_USER database without erroring' {
        $plan = Get-RecoveryPlan -State 'ONLINE' -UserAccess 'SINGLE_USER'
        $plan.PreRestore | Should -Contain 'SET_SINGLE_USER'
    }

    It 'restores an ABSENT database directly' {
        $plan = Get-RecoveryPlan -State 'ABSENT'
        $plan.PreRestore | Should -BeNullOrEmpty
    }
}

Describe 'Restore-Database orchestration' {

    BeforeEach {
        $script:executed = New-Object System.Collections.ArrayList
        Mock Invoke-Sql {
            param($Query, $QueryTimeout)
            [void]$script:executed.Add($Query)
            if ($Query -match 'FILELISTONLY') {
                return @([pscustomobject]@{ LogicalName = 'WWI_Primary'; Type = 'D' },
                         [pscustomobject]@{ LogicalName = 'WWI_Log';     Type = 'L' })
            }
            return $null
        }
    }

    It 'never issues ALTER before RESTORE when the database is RESTORING' {
        Mock Get-DatabaseState { [pscustomobject]@{ State = 'RESTORING'; UserAccess = 'SINGLE_USER' } }
        Restore-Database -Database 'WideWorldImporters' -BackupPath 'C:\x.bak' -DataPath 'C:\data' | Out-Null

        $alterBeforeRestore = $false
        foreach ($q in $script:executed) {
            if ($q -match 'RESTORE DATABASE') { break }
            if ($q -match 'ALTER DATABASE')   { $alterBeforeRestore = $true; break }
        }
        $alterBeforeRestore | Should -BeFalse
        ($script:executed -join "`n") | Should -Match 'RESTORE DATABASE .* WITH .*RECOVERY'
    }

    It 'preserves the PRIMARY restore error and runs no cleanup ALTER on failure' {
        Mock Get-DatabaseState { [pscustomobject]@{ State = 'RESTORING'; UserAccess = 'SINGLE_USER' } }
        Mock Invoke-Sql {
            param($Query, $QueryTimeout)
            [void]$script:executed.Add($Query)
            if ($Query -match 'FILELISTONLY') {
                return @([pscustomobject]@{ LogicalName = 'WWI_Primary'; Type = 'D' })
            }
            if ($Query -match 'RESTORE DATABASE') { throw 'PRIMARY: media set has 2 media families but only 1 provided' }
            return $null
        }

        { Restore-Database -Database 'WideWorldImporters' -BackupPath 'C:\x.bak' -DataPath 'C:\data' } |
            Should -Throw -ExpectedMessage '*PRIMARY:*'

        # No ALTER (cleanup) after the failing RESTORE.
        $sawRestore = $false
        foreach ($q in $script:executed) {
            if ($q -match 'RESTORE DATABASE') { $sawRestore = $true; continue }
            if ($sawRestore -and $q -match 'ALTER DATABASE') {
                throw "a cleanup ALTER ran after the failed restore and would mask the primary error"
            }
        }
    }

    It 'finishes ONLINE + MULTI_USER on a successful restore from ONLINE' {
        $script:stateCalls = 0
        Mock Get-DatabaseState {
            $script:stateCalls++
            if ($script:stateCalls -eq 1) { [pscustomobject]@{ State = 'ONLINE'; UserAccess = 'MULTI_USER' } }
            else                          { [pscustomobject]@{ State = 'ONLINE'; UserAccess = 'MULTI_USER' } }
        }
        $final = Restore-Database -Database 'WideWorldImporters' -BackupPath 'C:\x.bak' -DataPath 'C:\data'
        $final.State      | Should -Be 'ONLINE'
        $final.UserAccess | Should -Be 'MULTI_USER'
        ($script:executed -join "`n") | Should -Match 'SET MULTI_USER'
    }
}
