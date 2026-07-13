<#
.SYNOPSIS
    Restore the WideWorldImporters sample database in a way that is safe to repeat
    even after a previous run left the database in RESTORING or SINGLE_USER state.

.DESCRIPTION
    The naive restore path issues an unconditional
    ``ALTER DATABASE ... SET SINGLE_USER WITH ROLLBACK IMMEDIATE`` before every
    restore. That statement is ILLEGAL against a database that is already in the
    RESTORING state ("ALTER DATABASE is not permitted while a database is in the
    Restoring state"), so a run that was interrupted mid-restore — e.g. a capture
    session that was killed — leaves the database RESTORING + SINGLE_USER and the
    NEXT restore aborts on its own cleanup before it ever reaches RESTORE.

    This script fixes that by:
      * reading the database's CURRENT state (state_desc + user_access_desc)
        BEFORE issuing any ALTER, and choosing the recovery plan from it
        (Get-RecoveryPlan is a pure function, unit-tested with Pester);
      * NEVER issuing an unsupported ALTER against a RESTORING database — it goes
        straight to RESTORE ... WITH REPLACE, RECOVERY;
      * on restore FAILURE, preserving the PRIMARY restore error (a cleanup error
        never replaces it) and printing an exact, copy-pasteable recovery command;
      * finishing every successful restore ONLINE and MULTI_USER.

.PARAMETER ServerInstance
    SQL Server instance (default: localhost). Windows integrated auth by default.

.PARAMETER BackupPath
    Full path to WideWorldImporters-Full.bak.

.PARAMETER DataPath
    Directory for the restored data/log files (used to build MOVE clauses).

.EXAMPLE
    ./Restore-WideWorldImporters.ps1 -BackupPath C:\backups\WideWorldImporters-Full.bak
#>
[CmdletBinding()]
param(
    [string]$ServerInstance = 'localhost',
    [string]$Database       = 'WideWorldImporters',
    [Parameter(Mandatory = $true)][string]$BackupPath,
    [string]$DataPath       = $env:TEMP
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Pure decision logic (no I/O) — unit-tested in Restore-WideWorldImporters.Tests.ps1.
# Given the CURRENT database state, return the ordered plan the caller executes.
# ---------------------------------------------------------------------------
function Get-RecoveryPlan {
    <#
    .SYNOPSIS  Map a database state to an ordered, side-effect-free recovery plan.
    .OUTPUTS   [pscustomobject] with:
                 PreRestore  : ordered step names to run BEFORE the RESTORE
                 RestoreWith : the RESTORE ... WITH options (array)
                 PostRestore : ordered step names to run AFTER a successful RESTORE
                 Notes       : human explanation
               Step names are stable tokens the executor maps to SQL, so the plan
               is trivially assertable in tests without touching a server.
    #>
    param(
        # 'ABSENT' (db does not exist), 'ONLINE', 'RESTORING', 'RECOVERY_PENDING',
        # 'SUSPECT', or any other state_desc value.
        [Parameter(Mandatory = $true)][string]$State,
        # 'MULTI_USER' | 'SINGLE_USER' | 'RESTRICTED_USER' | '' (unknown/absent)
        [string]$UserAccess = ''
    )

    switch ($State.ToUpperInvariant()) {
        'ABSENT' {
            # Nothing to detach from; a plain restore creates it.
            return [pscustomobject]@{
                PreRestore  = @()
                RestoreWith = @('REPLACE', 'RECOVERY')
                PostRestore = @('SET_MULTI_USER')
                Notes       = 'database absent; straight restore'
            }
        }
        'RESTORING' {
            # CRITICAL: ALTER DATABASE is NOT permitted in the RESTORING state, so
            # emit NO pre-restore ALTER. Bring the database online by completing
            # the restore with RECOVERY.
            return [pscustomobject]@{
                PreRestore  = @()
                RestoreWith = @('REPLACE', 'RECOVERY')
                PostRestore = @('SET_MULTI_USER')
                Notes       = 'database in RESTORING state; no ALTER is legal here — restore WITH RECOVERY'
            }
        }
        default {
            # ONLINE / RECOVERY_PENDING / SUSPECT etc.: an ALTER is legal. Take it
            # single-user (rolling back open sessions) so the restore can proceed,
            # then restore and return it to multi-user. SET_SINGLE_USER is
            # idempotent when the db is already SINGLE_USER.
            return [pscustomobject]@{
                PreRestore  = @('SET_SINGLE_USER')
                RestoreWith = @('REPLACE', 'RECOVERY')
                PostRestore = @('SET_MULTI_USER')
                Notes       = "database $State/$UserAccess; single-user, restore, multi-user"
            }
        }
    }
}

function Get-RecoveryCommand {
    <#
    .SYNOPSIS  The exact, tested command that recovers a database stuck RESTORING.
               Printed verbatim on failure so the operator can copy-paste it.
    #>
    param([string]$Database, [string]$BackupPath)
    return "RESTORE DATABASE [$Database] FROM DISK = N'$BackupPath' WITH REPLACE, RECOVERY;"
}

# ---------------------------------------------------------------------------
# I/O helpers — thin, so Pester can mock them.
# ---------------------------------------------------------------------------
function Invoke-Sql {
    param([string]$Query, [int]$QueryTimeout = 0)
    Invoke-Sqlcmd -ServerInstance $ServerInstance -Query $Query -QueryTimeout $QueryTimeout -TrustServerCertificate
}

function Get-DatabaseState {
    param([string]$Database)
    $row = Invoke-Sql -Query @"
SELECT state_desc, user_access_desc
FROM sys.databases WHERE name = N'$Database';
"@
    if ($null -eq $row) {
        return [pscustomobject]@{ State = 'ABSENT'; UserAccess = '' }
    }
    return [pscustomobject]@{ State = $row.state_desc; UserAccess = $row.user_access_desc }
}

function Get-RelocationClause {
    param([string]$Database, [string]$BackupPath, [string]$DataPath)
    $files = Invoke-Sql -Query "RESTORE FILELISTONLY FROM DISK = N'$BackupPath';"
    $moves = foreach ($f in $files) {
        $ext = if ($f.Type -eq 'L') { '.ldf' } else { '.mdf' }
        $target = Join-Path $DataPath ("{0}{1}" -f $f.LogicalName, $ext)
        "MOVE N'$($f.LogicalName)' TO N'$target'"
    }
    return ($moves -join ", ")
}

# ---------------------------------------------------------------------------
# Executor — turns a plan step token into SQL and runs it.
# ---------------------------------------------------------------------------
function Invoke-PlanStep {
    param([string]$Step, [string]$Database)
    switch ($Step) {
        'SET_SINGLE_USER' {
            Invoke-Sql -Query "ALTER DATABASE [$Database] SET SINGLE_USER WITH ROLLBACK IMMEDIATE;"
        }
        'SET_MULTI_USER' {
            Invoke-Sql -Query "ALTER DATABASE [$Database] SET MULTI_USER;"
        }
        default { throw "unknown plan step '$Step'" }
    }
}

function Restore-Database {
    <#
    .SYNOPSIS  Orchestrate a repeatable restore. Returns the final state; throws
               the PRIMARY restore error (never a cleanup error) on failure.
    #>
    param([string]$Database, [string]$BackupPath, [string]$DataPath)

    $current = Get-DatabaseState -Database $Database
    Write-Host "Current state of [$Database]: $($current.State) / $($current.UserAccess)"
    $plan = Get-RecoveryPlan -State $current.State -UserAccess $current.UserAccess
    Write-Host "Recovery plan: $($plan.Notes)"

    # Pre-restore steps (skipped entirely for a RESTORING database — no ALTER is legal).
    foreach ($step in $plan.PreRestore) {
        Invoke-PlanStep -Step $step -Database $Database
    }

    $moves = Get-RelocationClause -Database $Database -BackupPath $BackupPath -DataPath $DataPath
    $withOpts = ($plan.RestoreWith + @($moves)) -join ", "
    $restoreSql = "RESTORE DATABASE [$Database] FROM DISK = N'$BackupPath' WITH $withOpts;"

    try {
        Invoke-Sql -Query $restoreSql -QueryTimeout 0
    }
    catch {
        # Preserve the PRIMARY restore error. Do NOT run any ALTER cleanup here —
        # against a half-restored (RESTORING) database it would fail and mask the
        # real cause. Surface an exact, copy-pasteable recovery command instead.
        $primary = $_
        Write-Error "Restore of [$Database] FAILED. Primary error preserved below; no cleanup ALTER was attempted (it is illegal in the RESTORING state)."
        Write-Host  "To recover a database left in the RESTORING state, run:`n    $(Get-RecoveryCommand -Database $Database -BackupPath $BackupPath)"
        throw $primary
    }

    # Post-restore: return the (now ONLINE) database to multi-user.
    foreach ($step in $plan.PostRestore) {
        Invoke-PlanStep -Step $step -Database $Database
    }

    $final = Get-DatabaseState -Database $Database
    Write-Host "Final state of [$Database]: $($final.State) / $($final.UserAccess)"
    return $final
}

# Only auto-run when invoked as a script (not when dot-sourced by the Pester tests).
if ($MyInvocation.InvocationName -ne '.') {
    Restore-Database -Database $Database -BackupPath $BackupPath -DataPath $DataPath | Out-Null
}
