@echo off
rem ============================================================
rem  Double-click friendly wrapper for task_ctl.ps1
rem
rem  Double-click      -> show status
rem  task_ctl.cmd off  -> pause the 4-hourly runs
rem  task_ctl.cmd on   -> resume
rem  task_ctl.cmd run  -> run once right now
rem ============================================================
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0task_ctl.ps1" %*
if "%~1"=="" pause
