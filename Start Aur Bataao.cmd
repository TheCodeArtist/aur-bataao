@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\aur-bataao.exe" (
    echo Aur Bataao is not installed.
    echo Run: .venv\Scripts\python.exe -m pip install -e .
    pause
    exit /b 1
)

start "" "http://127.0.0.1:8080"
".venv\Scripts\aur-bataao.exe"
