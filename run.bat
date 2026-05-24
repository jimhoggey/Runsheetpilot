@echo off
REM Runsheet Pilot — Windows Launcher
title Runsheet Pilot

echo ================================================
echo  Runsheet Pilot
echo ================================================
echo.

REM Check Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found.
    echo Please install Python from https://python.org
    echo Make sure to tick "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo %%i
echo.
echo Installing / updating dependencies...
python -m pip install -r requirements.txt --quiet --upgrade

echo.
echo Starting server -- your browser will open automatically.
echo Press Ctrl+C to quit.
echo.

cd /d "%~dp0"
python propresenter_app.py
pause
