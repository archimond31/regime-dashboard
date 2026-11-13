# ============================================================
#  Control the RegimeSignal4H scheduled task.
#
#  status (default) -> show state, last run, next run
#  on               -> enable  (resume the 4-hourly runs)
#  off              -> disable (pause)
#  run              -> run once right now
#
#  Usually invoked through task_ctl.cmd (double-click friendly).
# ============================================================
param([string]$Action = 'status')

$ErrorActionPreference = 'Stop'
$TaskName = 'RegimeSignal4H'

switch ($Action.ToLower()) {
    'on'  { Enable-ScheduledTask  -TaskName $TaskName | Out-Null }
    'off' { Disable-ScheduledTask -TaskName $TaskName | Out-Null }
    'run' { Start-ScheduledTask   -TaskName $TaskName }
    'status' { }
    default {
        Write-Host "unknown action: $Action"
        Write-Host "usage: task_ctl.cmd [on | off | run]"
        exit 2
    }
}

$t = Get-ScheduledTask     -TaskName $TaskName
$i = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Host ""
Write-Host "=== $TaskName ==="
Write-Host ("State       : " + $t.State)
Write-Host ("Last run    : " + $i.LastRunTime)
Write-Host ("Last result : " + $i.LastTaskResult + "   (0 = ok)")
Write-Host ("Next run    : " + $i.NextRunTime)
Write-Host ""
Write-Host "Usage: task_ctl.cmd [on | off | run]"
