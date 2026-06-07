@echo off
chcp 65001 >nul
setlocal

set "SCRIPT_DIR=%~dp0"
set "PY=%SCRIPT_DIR%backend\.venv\Scripts\python.exe"
set "STARTER=%SCRIPT_DIR%backend\start_services.py"

if not exist "%PY%" (
  echo Python venv not found: %PY%
  pause
  exit /b 1
)

if not exist "%STARTER%" (
  echo Starter not found: %STARTER%
  pause
  exit /b 1
)

"%PY%" "%STARTER%"
pause
