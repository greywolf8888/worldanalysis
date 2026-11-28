@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0" || exit /b 1
set "DSH_PYTHON=%SystemDrive%\Python314\python.exe"
if not exist "%DSH_PYTHON%" set "DSH_PYTHON=python.exe"
"%DSH_PYTHON%" -B -X utf8 "_tools\dsh_dedicated.py" desktop
if errorlevel 1 pause
