# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the OpenEVSE ESP32 Flasher
# Build:  pyinstaller openevse_esp32_flasher.spec
#
# Bundles esptool (including its flasher stubs), pyserial, certifi and the
# Windows USB drivers.
#
# Windows is built as a folder ("onedir": OpenEVSE_ESP32_Flasher.exe plus an
# _internal folder) rather than a single self-extracting exe. Self-extracting
# PyInstaller executables are a common trigger for Chrome Safe Browsing,
# SmartScreen and antivirus heuristics; the folder build is not, and it also
# starts faster. macOS and Linux stay single-file. Set ONEDIR=1 to force the
# folder build elsewhere.
import os, re, sys
from PyInstaller.utils.hooks import collect_all, collect_data_files

WINDOWS = sys.platform == "win32"
ONEDIR = WINDOWS or os.environ.get("ONEDIR") == "1"
NAME = "OpenEVSE_ESP32_Flasher"

with open("openevse_esp32_flasher.py", encoding="utf-8") as f:
    VERSION = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', f.read(), re.M).group(1)

datas, binaries, hiddenimports = [], [], []
for pkg in ("esptool", "espefuse", "espsecure", "esp_pylib", "rich_click"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass
datas += collect_data_files("certifi")
hiddenimports += ["serial.tools.list_ports"]

# Windows USB-serial drivers (fetched by scripts/fetch_drivers.py). In the
# folder build these stay ordinary files (WCH's installer keeps its own
# signature) instead of being packed inside our executable.
if WINDOWS and os.path.isdir(os.path.join("drivers", "windows")):
    datas.append((os.path.join("drivers", "windows"), os.path.join("drivers", "windows")))

version_info = None
if WINDOWS:
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable, StringStruct,
        VarFileInfo, VarStruct)
    nums = tuple((list(map(int, re.findall(r"\d+", VERSION))) + [0, 0, 0, 0])[:4])
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x0,
                          OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "OpenEVSE"),
                StringStruct("FileDescription", "OpenEVSE ESP32 Firmware Flasher"),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", NAME),
                StringStruct("LegalCopyright", "GPL-3.0 - OpenEVSE contributors"),
                StringStruct("OriginalFilename", NAME + ".exe"),
                StringStruct("ProductName", "OpenEVSE ESP32 Flasher"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ])

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

common = dict(
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # UPX-packed executables are flagged far more often
    console=False,       # no terminal window (CLI output attaches to the calling console)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
    icon=None,           # replace with 'icon.ico' (Windows) or 'icon.icns' (macOS) if available
)

if ONEDIR:
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, **common)
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], runtime_tmpdir=None, **common)
