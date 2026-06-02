@echo off
REM Build the Windows .exe using PyInstaller.
REM Output: dist\Runsheet Pilot.exe
REM
REM Run:    build_win.bat
REM Clean:  build_win.bat clean

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "APP_NAME=Runsheet Pilot"
set "ENTRY=propresenter_app.py"

if /I "%~1"=="clean" (
    echo Cleaning build\ dist\ *.spec ...
    if exist build rmdir /s /q build
    if exist dist  rmdir /s /q dist
    if exist __pycache__ rmdir /s /q __pycache__
    del /q *.spec >nul 2>&1
    echo Done.
    exit /b 0
)

echo ================================================================
echo   Building "%APP_NAME%" for Windows
echo ================================================================

REM Sanity: Python
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Install from https://python.org and tick "Add Python to PATH".
    pause
    exit /b 1
)

for /f "tokens=*" %%v in ('python --version') do echo Python: %%v

REM Use a venv for a clean, predictable build
set "VENV_DIR=build_venv"
if not exist "%VENV_DIR%" (
    echo Creating build venv at %VENV_DIR% ...
    python -m venv "%VENV_DIR%"
)
call "%VENV_DIR%\Scripts\activate.bat"

echo Installing build dependencies ...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements-dev.txt --quiet

echo Cleaning previous build artifacts ...
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
del /q *.spec >nul 2>&1

echo Running PyInstaller ...
pyinstaller ^
    --name "%APP_NAME%" ^
    --windowed ^
    --onefile ^
    --noconfirm ^
    --clean ^
    --collect-submodules pdfplumber ^
    --collect-submodules waitress ^
    --collect-submodules cryptography ^
    --hidden-import waitress ^
    --hidden-import flask ^
    --hidden-import pdfplumber ^
    --hidden-import tkinter ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    "%ENTRY%"

if not exist "dist\%APP_NAME%.exe" (
    echo ERROR: Build did not produce dist\%APP_NAME%.exe
    pause
    exit /b 1
)

echo.
echo ================================================================
echo   Done.
echo   EXE: dist\%APP_NAME%.exe
echo ================================================================
echo.
echo Note: The .exe is unsigned. On first launch, Windows SmartScreen
echo       may show "Windows protected your PC" --- click "More info"
echo       then "Run anyway".
echo.
pause
