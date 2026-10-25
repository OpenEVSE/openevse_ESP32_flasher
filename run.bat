@echo off
rem ============================================================
rem  OpenEVSE ESP32 Flasher  -  Windows launcher
rem  Double-click to start the GUI, or: run.bat --help
rem ============================================================
setlocal
cd /d "%~dp0"

set PYTHON=
set PYTHONW=
where py >nul 2>nul && (set "PYTHON=py -3" & set "PYTHONW=pyw -3")
if not defined PYTHON (
    where python >nul 2>nul && (set "PYTHON=python" & set "PYTHONW=pythonw")
)
if not defined PYTHON (
    echo.
    echo   Python 3 not found. Install it from https://www.python.org/downloads/
    echo   and tick "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

if not exist "vendor\esptool" (
    echo Installing esptool and pyserial into vendor\ ^(first run only^)...
    %PYTHON% -m pip install --quiet --upgrade --target vendor -r requirements.txt
    if errorlevel 1 (
        echo pip install failed.
        pause
        exit /b 1
    )
)

if "%~1"=="" (
    start "" %PYTHONW% openevse_esp32_flasher.py
) else (
    %PYTHON% openevse_esp32_flasher.py %*
)
