@echo off
rem ============================================================
rem  Crypto 4H signal - local runner
rem  Called by Windows Task Scheduler every 4h at :05 local time.
rem  Log: ..\logs\signal.log   (UTF-8, stamped by Python)
rem  Setup: run install_task.ps1 once.
rem  Test : run_signal.cmd --force --dry
rem ============================================================
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

set "LOGDIR=%~dp0..\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\signal.log"

rem Prefer the stand-alone Python install. The WorkBuddy bundled one
rem lives under a versioned path and moves on every WorkBuddy update.
set "PY=C:\Users\archimond\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "%PY%" (
  echo [FATAL] python not found.>>"%LOG%"
  exit /b 9009
)

rem No timestamp here on purpose: cmd's %DATE% carries a localized
rem weekday in GBK and would corrupt the UTF-8 log. Python stamps it.
"%PY%" -u run_signal.py %* >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo ----- exit=%RC% ----->>"%LOG%"
exit /b %RC%
