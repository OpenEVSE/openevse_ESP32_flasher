#!/usr/bin/env bash
# ============================================================
#  OpenEVSE ESP32 Flasher  -  macOS / Linux launcher
#  Run:  bash run.sh            (GUI)
#        bash run.sh --help     (command line)
# ============================================================
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Find Python 3.8+ ─────────────────────────────────────────
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null && \
       "$candidate" -c "import sys; sys.exit(sys.version_info < (3, 8))" 2>/dev/null; then
        PYTHON="$candidate"; break
    fi
done
if [ -z "$PYTHON" ]; then
    echo ""
    echo "  Python 3.8+ not found."
    echo "  macOS:  brew install python python-tk  (or https://www.python.org)"
    echo "  Ubuntu: sudo apt install python3 python3-tk python3-pip"
    echo "  Fedora: sudo dnf install python3 python3-tkinter"
    echo ""
    exit 1
fi

# ── Bundle esptool / pyserial into ./vendor on first run ────
if [ ! -d "$SCRIPT_DIR/vendor/esptool" ]; then
    echo "Installing esptool and pyserial into vendor/ (first run only)..."
    "$PYTHON" -m pip install --quiet --upgrade --target "$SCRIPT_DIR/vendor" \
        -r "$SCRIPT_DIR/requirements.txt"
fi

# ── Linux: serial port permissions ───────────────────────────
if [[ "$(uname)" == "Linux" ]] && ! id -nG | grep -qwE "dialout|uucp"; then
    echo ""
    echo "  Note: your user is not in the 'dialout' group, serial ports may be"
    echo "  inaccessible. Fix with:  sudo usermod -aG dialout \$USER  (then log out/in)"
    echo ""
fi

exec "$PYTHON" "$SCRIPT_DIR/openevse_esp32_flasher.py" "$@"
