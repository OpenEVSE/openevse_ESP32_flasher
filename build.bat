@echo off
rem ============================================================
rem  OpenEVSE ESP32 Flasher - Windows Build Script
rem  Produces:  OpenEVSE_ESP32_Flasher-windows.zip
rem ============================================================
setlocal
cd /d "%~dp0"
set PYTHON=python
where py >nul 2>nul && set "PYTHON=py -3"

%PYTHON% -m pip install --quiet --upgrade pyinstaller -r requirements.txt || exit /b 1

echo Fetching USB drivers to bundle...
%PYTHON% scripts\fetch_drivers.py --allow-missing

echo Running tests...
%PYTHON% -m unittest discover -s tests || exit /b 1

if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
if exist release rmdir /s /q release
%PYTHON% -m PyInstaller --noconfirm openevse_esp32_flasher.spec || exit /b 1

mkdir release
xcopy /e /i /q dist\OpenEVSE_ESP32_Flasher release >nul
copy README.md release\ >nul
powershell -NoProfile -Command "Compress-Archive -Path release\* -DestinationPath OpenEVSE_ESP32_Flasher-windows.zip -Force"
echo Done!  Output: OpenEVSE_ESP32_Flasher-windows.zip
