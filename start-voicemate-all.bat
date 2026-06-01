@echo off
chcp 65001 >nul
setlocal EnableExtensions

title VoiceMate 一键启动全部服务

:: ============================================================
::  VoiceMate 全部服务一键启动
::  启动：API 后端 + LiveKit Server + LiveKit Agent
::  用法：双击运行（Windows Explorer）
:: ============================================================

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR:~0,-1%"
set "BACKEND_DIR=%PROJECT_DIR%\backend"
set "LIVEKIT_DIR=%PROJECT_DIR%\tools\livekit"
set "VENV_PY=%BACKEND_DIR%\.venv\Scripts\python.exe"

echo.
echo ============================================================
echo   VoiceMate 全部服务启动器
echo   (关闭窗口 = 停止服务)
echo ============================================================
echo.

:: 读取 .env
if exist "%BACKEND_DIR%\.env" (
    for /f "usebackq tokens=1,* delims==" %%A in ("%BACKEND_DIR%\.env") do (
        if /i "%%A"=="LIVEKIT_HOST" set "LIVEKIT_HOST=%%B"
        if /i "%%A"=="LIVEKIT_PORT" set "LIVEKIT_PORT=%%B"
    )
)
if not defined LIVEKIT_HOST set "LIVEKIT_HOST=192.168.10.233"
if not defined LIVEKIT_PORT set "LIVEKIT_PORT=7880"

:: 1) 清理旧进程
echo [1/3] 清理旧进程...
taskkill /F /IM python.exe >nul 2>&1
taskkill /F /IM livekit-server.exe >nul 2>&1
timeout /t 2 /nobreak >nul
echo   完成

:: 2) 启动 API 后端
echo [2/3] 启动 VoiceMate API...
start "VoiceMate-API" /MIN /D "%BACKEND_DIR%" "%VENV_PY%" server.py
timeout /t 3 /nobreak >nul

:: 3) 启动 LiveKit Server
echo [3/3] 启动 LiveKit Server...
start "VoiceMate-LiveKit" /MIN /D "%LIVEKIT_DIR%" livekit-server.exe --config voicemate-livekit.yaml --node-ip %LIVEKIT_HOST%
timeout /t 3 /nobreak >nul

:: 4) 启动 LiveKit Agent
echo [3/3] 启动 LiveKit Agent...
start "VoiceMate-Agent" /MIN /D "%BACKEND_DIR%" cmd /c ""%VENV_PY%" "%BACKEND_DIR%\livekit_agent.py" dev"

echo.
echo ============================================================
echo   服务已启动！
echo ============================================================
echo   API:       http://%LIVEKIT_HOST%:8000
echo   健康检查:  http://127.0.0.1:8000/v1/health
echo   LiveKit:   ws://%LIVEKIT_HOST%:%LIVEKIT_PORT%
echo   RTC TCP:   %LIVEKIT_HOST%:7881
echo   RTC UDP:   50000-50100
echo.
echo   已打开 3 个最小化窗口：
echo     VoiceMate-API      - Python 后端
echo     VoiceMate-LiveKit  - LiveKit 信令
echo     VoiceMate-Agent    - LiveKit Agent
echo.
echo   停止服务：关闭窗口 或 运行 stop-voicemate.bat
echo ============================================================
echo.
pause
