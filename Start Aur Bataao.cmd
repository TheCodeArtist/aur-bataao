@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\aur-bataao.exe" (
    echo Aur Bataao is not installed.
    echo Run: .venv\Scripts\python.exe -m pip install -e .
    pause
    exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start Aur Bataao.ps1"
set "launcher_exit_code=%errorlevel%"

if not "%launcher_exit_code%"=="0" (
    echo.
    echo Aur Bataao could not be started.
    pause
)

exit /b %launcher_exit_code%
