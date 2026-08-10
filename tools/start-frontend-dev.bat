@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\dev_server.py frontend
) else (
    py -3.14 scripts\dev_server.py frontend
)

set "EXIT_CODE=%errorlevel%"
if not "%EXIT_CODE%"=="0" pause
exit /b %EXIT_CODE%
