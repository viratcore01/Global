#Requires -Version 5.1
<#
.SYNOPSIS
    Register (or remove) Windows Scheduled Tasks for the radar.

.DESCRIPTION
    Creates three tasks under the current user:
      RadarWorker  - long-running worker, starts at logon (or boot with -AtStartup)
      RadarDigest  - daily digest at 08:00 local time
      RadarBackup  - daily snapshot + verify at 03:15 local time

    By default tasks run only while you are logged on, which is what a personal
    radar wants. Use -AtStartup from an elevated shell to start the worker at
    boot regardless of logon.

.PARAMETER Uninstall
    Remove the three tasks instead of creating them.

.PARAMETER AtStartup
    Use an AtStartup trigger for the worker (requires an elevated shell).

.PARAMETER TimeZone
    Informational only; tasks use the machine's local time. Set the machine (or
    the worker's TZ environment) to Asia/Kolkata if that is your schedule.

.EXAMPLE
    .\scripts\install-task.ps1

.EXAMPLE
    .\scripts\install-task.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch] $Uninstall,
    [switch] $AtStartup,
    [string] $TimeZone = 'Asia/Kolkata'
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$taskNames = @('RadarWorker', 'RadarDigest', 'RadarBackup')

if ($Uninstall) {
    foreach ($name in $taskNames) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "Removed scheduled task $name"
        }
    }
    return
}

if (-not (Test-Path -LiteralPath $python)) {
    throw "Virtualenv not found at $python. Create it and install requirements first."
}

$radarExe = $python
$commonSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Write-Host "Repository: $root"
Write-Host "Python:     $python"
Write-Host "Timezone:   $TimeZone (tasks use local machine time)"

# --- Worker ----------------------------------------------------------------
$workerAction = New-ScheduledTaskAction -Execute $radarExe `
    -Argument '-m radar worker' -WorkingDirectory $root
if ($AtStartup) {
    $workerTrigger = New-ScheduledTaskTrigger -AtStartup
} else {
    $workerTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
}
$workerSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName 'RadarWorker' `
    -Action $workerAction -Trigger $workerTrigger -Settings $workerSettings `
    -Description 'Global Cinema Opportunity Radar long-running worker' -Force | Out-Null
Write-Host "Registered RadarWorker ($(if ($AtStartup) { 'at startup' } else { 'at logon' }))"

# --- Digest ----------------------------------------------------------------
$digestAction = New-ScheduledTaskAction -Execute $radarExe `
    -Argument '-m radar digest' -WorkingDirectory $root
$digestTrigger = New-ScheduledTaskTrigger -Daily -At '08:00'
Register-ScheduledTask -TaskName 'RadarDigest' `
    -Action $digestAction -Trigger $digestTrigger -Settings $commonSettings `
    -Description 'Global Cinema Opportunity Radar daily digest (08:00)' -Force | Out-Null
Write-Host 'Registered RadarDigest (daily 08:00)'

# --- Backup ----------------------------------------------------------------
$backupAction = New-ScheduledTaskAction -Execute $radarExe `
    -Argument '-m radar backup snapshot' -WorkingDirectory $root
# Verify the snapshot we just wrote (matches systemd's ExecStartPost).
$verifyAction = New-ScheduledTaskAction -Execute $radarExe `
    -Argument '-m radar backup verify' -WorkingDirectory $root
$backupTrigger = New-ScheduledTaskTrigger -Daily -At '03:15'
Register-ScheduledTask -TaskName 'RadarBackup' `
    -Action @($backupAction, $verifyAction) -Trigger $backupTrigger -Settings $commonSettings `
    -Description 'Global Cinema Opportunity Radar nightly snapshot (03:15)' -Force | Out-Null
Write-Host 'Registered RadarBackup (daily 03:15)'

Write-Host ''
Write-Host 'Check with:  Get-ScheduledTask -TaskName Radar* | Format-Table TaskName, State'
Write-Host 'Run now:     Start-ScheduledTask -TaskName RadarWorker'
Write-Host 'Logs:        data\logs and Event Viewer -> Task Scheduler'
