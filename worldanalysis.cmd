@echo off
setlocal
chcp 65001 >nul
set "WA_PYTHON=D:\桌面\awesome-dsh\.dsh-home\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
if not exist "%WA_PYTHON%" (
  echo Required host Python 3.12 runtime is unavailable. Do not substitute a different ABI for .runtime dependencies.
  exit /b 2
)
pushd "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%WA_PYTHON%" -B -m worldanalysis %*
set "WA_EXIT=%ERRORLEVEL%"
popd
exit /b %WA_EXIT%
