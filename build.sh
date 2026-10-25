#!/usr/bin/env bash
# ============================================================
#  OpenEVSE ESP32 Flasher - macOS / Linux Build Script
#  Produces:  OpenEVSE_ESP32_Flasher-<platform>.zip
#  Requires:  Python 3.8+ with tkinter, pip
# ============================================================
set -e
cd "$(dirname "$0")"

case "$(uname -s)" in
    Darwin) PLATFORM="macos" ;;
    Linux)  PLATFORM="linux" ;;
    *)      echo "Unsupported platform: $(uname -s)"; exit 1 ;;
esac
PYTHON="${PYTHON:-python3}"

echo " OpenEVSE ESP32 Flasher - Build ($PLATFORM)"
echo " Using $($PYTHON --version)"

"$PYTHON" -m pip install --quiet --upgrade pyinstaller -r requirements.txt

echo " Running tests..."
"$PYTHON" -m unittest discover -s tests

rm -rf dist build release
"$PYTHON" -m PyInstaller --noconfirm openevse_esp32_flasher.spec

echo " Smoke test..."
dist/OpenEVSE_ESP32_Flasher esptool version

mkdir -p release
cp dist/OpenEVSE_ESP32_Flasher release/
chmod +x release/OpenEVSE_ESP32_Flasher
cp README.md release/
[ "$PLATFORM" = "linux" ] && cp drivers/linux/99-openevse-usb-serial.rules release/

ZIP_NAME="OpenEVSE_ESP32_Flasher-${PLATFORM}.zip"
rm -f "$ZIP_NAME"
(cd release && zip -qr "../${ZIP_NAME}" .)
echo " Done!  Output: ${ZIP_NAME}"
