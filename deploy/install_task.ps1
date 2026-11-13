# ============================================================
#  Register the 4H signal runner in Windows Task Scheduler.
#
#  Install: powershell -ExecutionPolicy Bypass -File install_task.ps1
#  Remove : powershell -ExecutionPolicy Bypass -File install_task.ps1 -Remove
#  Test   : Start-ScheduledTask -TaskName RegimeSignal4H
#  Log    : ..\logs\signal.log
#
#  Runs as the current user (no stored password, no admin needed).
#  StartWhenAvailable = catch up a missed run after reboot.
#  WakeToRun          = wake the machine from sleep at the trigger.
# ============================================================
param([switch]$Remove)

$ErrorActionPreference = 'Stop'
$TaskName = 'RegimeSignal4H'

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "removed: $TaskName"
    exit 0
}

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoDir   = Split-Path -Parent $DeployDir
$CmdPath   = Join-Path $DeployDir 'run_signal.cmd'
if (-not (Test-Path $CmdPath)) { throw "missing: $CmdPath" }

# Local time is UTC+8. A 4H bar closes at 00/04/08/12/16/20 UTC,
# i.e. 08:00/12:00/16:00/20:00/00:00/04:00 Beijing. Fire 5 minutes after.
$times = @('08:05', '12:05', '16:05', '20:05', '00:05', '04:05')

$action = New-ScheduledTaskAction -Execute $CmdPath -WorkingDirectory $RepoDir

$triggers = foreach ($t in $times) {
    $at = [datetime]::ParseExact($t, 'HH:mm', [cultureinfo]::InvariantCulture)
    New-ScheduledTaskTrigger -Daily -At $at
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 `
    -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Description 'Crypto 4H signal: daily G regime + 4H breakout' -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
Write-Host "registered: $($task.TaskName)   state=$($task.State)"
Get-ScheduledTaskInfo -TaskName $TaskName |
    Select-Object LastRunTime, LastTaskResult, NextRunTime |
    Format-List
