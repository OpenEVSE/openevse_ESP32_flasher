# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the OpenEVSE ESP32 Flasher
# Build:  pyinstaller openevse_esp32_flasher.spec
#
# Produces a single self-contained executable with esptool (including its
# flasher stubs), pyserial, certifi and the Windows USB drivers embedded.
import os
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas, binaries, hiddenimports = [], [], []
for pkg in ("esptool", "espefuse", "espsecure", "esp_pylib", "rich_click"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
datas += collect_data_files("certifi")
hiddenimports += ["serial.tools.list_ports"]

# Windows USB-serial drivers (fetched by scripts/fetch_drivers.py)
if os.path.isdir(os.path.join("drivers", "windows")):
    datas.append((os.path.join("drivers", "windows"), os.path.join("drivers", "windows")))

a = Analysis(
    ['openevse_esp32_flasher.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['test', 'unittest', 'pytest'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='OpenEVSE_ESP32_Flasher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # no terminal window (CLI output attaches to the calling console)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,           # replace with 'icon.ico' (Windows) or 'icon.icns' (macOS) if available
)
