@echo off
rem ============================================================
rem  Crypto 4H signal - local runner
rem  Called by Windows Task Scheduler every 4h at :05 local time.
rem  Log: ..\logs\signal.log   (UTF-8, stamped by Python)
rem  Setup: run install_task.ps1 once.
rem  Test : run_signal.cmd --force --dry
rem
rem  Three-state reporting, so silence is never ambiguous:
rem    exit 0 + email      -> there is an action, or today's first digest
rem    exit 0 + no email   -> nothing to do, and a mail already went out today
rem    exit != 0           -> alert_fail.py sends a failure mail (never silent)
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
  rem No email here: if python is gone, alert_fail.py cannot run either.
  rem This case can only be caught from outside -> the heartbeat block below
  rem never fires, so healthchecks.io (or any watcher) will alarm.
  exit /b 9009
)

rem No timestamp here on purpose: cmd's %DATE% carries a localized
rem weekday in GBK and would corrupt the UTF-8 log. Python stamps it.
"%PY%" -u run_signal.py %* >>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo ----- exit=%RC% ----->>"%LOG%"

if not "%RC%"=="0" goto :failed

rem ---- heartbeat -------------------------------------------------------
rem Only ping on success, so "process died" looks different from
rem "nothing to report" from the outside. Put the ping URL (one line)
rem in deploy\heartbeat.url; without that file this block is a no-op.
if not exist "%~dp0heartbeat.url" goto :done
set "HB="
for /f "usebackq delims=" %%i in ("%~dp0heartbeat.url") do set "HB=%%i"
if not defined HB goto :done
curl -fsS -m 10 -o nul "%HB%" 2>nul
goto :done

:failed
rem A crash must make noise. Without this, "no email" and "crashed"
rem are indistinguishable to the reader.
"%PY%" alert_fail.py %RC% >>"%LOG%" 2>&1

:done
exit /b %RC%
