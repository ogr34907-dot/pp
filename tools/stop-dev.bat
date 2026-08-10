@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\dev_server.py stop
) else (
    py -3.14 scripts\dev_server.py stop
)

pause
exit /b %errorlevel%
