@echo off
rem ============================================================
rem  Store the read-only Binance API key (spot; futures if allowed).
rem
rem  The key is encrypted with Windows DPAPI into
rem    %LOCALAPPDATA%\regime_dashboard\cex.enc
rem  which is OUTSIDE this repo. The repo is PUBLIC, so plaintext
rem  must never land here -- this script only forwards it to python.
rem
rem  Key is typed with getpass: not echoed, not in shell history.
rem  If your terminal still echoes it, Ctrl+C and use:
rem     binance_account.py set --from %TEMP%\k.txt
rem
rem  Re-run any time to replace the key. Delete with:
rem     binance_account.py rm
rem ============================================================
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d "%~dp0.."

rem Prefer the stand-alone Python install; fall back to PATH.
set "PY=C:\Users\archimond\AppData\Local\Programs\Python\Python313\python.exe"
if not exist "%PY%" set "PY=python"

echo.
echo  Read-only Binance key setup
echo  ----------------------------
echo  1) key and secret are typed here, never stored in plaintext
echo  2) permissions are probed by actually calling 3 endpoints
echo     spot / futures / apiRestrictions -- reachable, not declared
echo.

"%PY%" binance_account.py set %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" goto :done
echo  Next:
echo     python binance_account.py snap
echo     python _diag_account_xcheck.py
:done
echo.
pause
exit /b %RC%
