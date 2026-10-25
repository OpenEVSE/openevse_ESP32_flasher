# OpenEVSE ESP32 Firmware Flasher

A graphical tool for flashing the [OpenEVSE WiFi gateway firmware](https://github.com/OpenEVSE/openevse_esp32_firmware)
onto ESP32-based OpenEVSE boards over USB. You don't need to use the command line. It's modelled on the
[OpenEVSE AVR flasher](https://github.com/OpenEVSE/openevse_avr_flasher).

| Board | Flash | Firmware | Bootloader / partitions |
|---|---|---|---|
| `openevse_wifi_v1` | 4MB | `openevse_wifi_v1.bin` | `bootloader_4mb.bin` / `partitions_4mb.bin` |
| `openevse_wifi_v1` | 16MB | `openevse_wifi_v1_16mb.bin` | `bootloader_v1_16mb.bin` / `partitions_v1_16mb.bin` |
| `openevse_tft_wifi_v1` | 16MB | `openevse_wifi_tft_v1.bin` | `bootloader_16mb.bin` / `partitions_16mb.bin` |

The flasher reads the ESP32's flash size over USB and picks the matching build and partition layout for you.

---

## Features

- **Detects the ESP32 automatically.** It shows the chip, MAC address, flash size and the partition layout currently on the board.
- **Picks the right build from the flash size.**
  - A 16MB OpenEVSE WiFi V1 gets the 16MB build.
  - A 4MB board gets the 4MB build.
  - The TFT board needs 16MB. The flasher refuses to flash it onto a smaller chip.
- **Four firmware sources:**
  - **Release**: tagged releases from GitHub.
  - **Development**: the `latest` pre-release that is rebuilt on every merge to `master`.
  - **Actions**: build artifacts from any branch or pull request run of `build.yaml`.
  - **Local file**: a `.bin`, or an Actions artifact `.zip` exactly as downloaded.
- **Self-contained.** esptool, pyserial and the CA certificates are built into the executable. The Windows CP210x / CH340 USB drivers are bundled too, or downloaded on demand.
- **Safety checks before writing anything:**
  - Image magic and app descriptor.
  - Firmware size vs. app partition.
  - Partition table vs. flash size.
  - ESP-IDF version vs. board build, which catches a 4MB image picked for a 16MB board.
  - Current layout vs. target before a firmware-only flash.
- **Changes the layout without losing settings.** The bootloader, partition table, otadata and app are rewritten together. NVS (WiFi settings) is at the same address in both layouts, so it is kept.
- **Command line mode** for scripting and headless use.

---

## Getting started

### Pre-built executable (recommended)

Download `OpenEVSE_ESP32_Flasher-<windows|macos-arm64|macos-intel|linux>.zip` from the Releases page, unzip it and run
`OpenEVSE_ESP32_Flasher`. You don't need to install Python or anything else.

> macOS: the app is unsigned. Right-click → **Open** the first time.
> Linux: `chmod +x OpenEVSE_ESP32_Flasher` if needed.

### From source

| OS | Command |
|---|---|
| Windows | double-click **`run.bat`** |
| macOS / Linux | `bash run.sh` |

The first run installs esptool / pyserial into a local `vendor/` folder. Nothing is installed system-wide.
You need Python 3.8+ with Tk (`sudo apt install python3-tk` on Debian/Ubuntu).

---

## Flashing

1. **Connect the USB-serial programmer** to the 6-pin header of the OpenEVSE WiFi module and to your computer.
   An example is the [OpenEVSE USB-to-serial programmer](https://store.openevse.com/collections/all-products/products/usb-to-serial-comm).
2. **Select the port** (adapters with CP210x / CH340 / FTDI chips are picked automatically).
3. Click **Detect ESP32**. The chip, flash size and current layout are shown.
4. **Choose the board**: *OpenEVSE WiFi V1* or *OpenEVSE TFT WiFi V1*. The *Target* line shows the file that will be flashed.
5. **Choose the firmware** from the Release, Development, Actions or Local file tab.
6. **Choose the options:**
   - **Full install** (default) writes the bootloader, partition table, otadata and firmware. Use it for new
     boards, recovery, and changing between the 4MB and 16MB layouts.
   - **Firmware only** writes just the app at `0x10000`. It's only allowed when the board already has the
     correct layout.
   - **Erase all flash first** is a factory reset. It removes WiFi settings and all configuration.
7. Click **Flash Firmware**, check the summary, and confirm.

If you click Flash Firmware without detecting first, the flash size is read automatically.

### Boards without auto-reset

Some programmers/modules have no DTR/RTS auto-reset wiring. On those, esptool reports
*"Failed to connect"* or *"No serial data received"*. In that case:

1. Tick **Manual boot mode (no auto-reset)**.
2. Hold the **BOOT / WiFi button (GPIO0)**, power-cycle or reset the board, then release the button.
3. Click **Detect** / **Flash**. You will be prompted when to do step 2 again.
4. Power-cycle the board after flashing.

### What gets written (Full install)

| Address | Content |
|---|---|
| `0x1000` | bootloader (`bootloader_*.bin`) |
| `0x8000` | partition table (`partitions_*.bin`) |
| `0xE000` | blank otadata, so the board boots `app0` |
| `0x10000` | firmware |

Settings used: `--flash-mode dio --flash-freq 40m --flash-size 4MB|16MB`. These match the
[OpenEVSE docs](https://github.com/OpenEVSE/openevse_esp32_firmware/blob/master/docs/user/firmware-update.md)
and the validated 16MB USB conversion.

If a release doesn't publish a bootloader or partition table for the selected board (older releases don't):

- The partition table is generated from the built-in copy of the firmware's CSV. It is byte-identical to the
  release files, and the unit tests check this.
- The bootloader is taken from the newest release that has one.

The summary tells you when this happens.

---

## Firmware sources

| Tab | Source |
|---|---|
| Release | `github.com/OpenEVSE/openevse_esp32_firmware/releases` (all tags except `latest`) |
| Development | the `latest` pre-release ("Development Build"), rebuilt on every `master` merge |
| Actions | successful runs of `.github/workflows/build.yaml`. Filter by branch, or leave it blank for all branches and PRs. Artifacts are named `<env>.bin` and contain `firmware.bin`, `bootloader.bin` and `partitions.bin`. |
| Local file | any `.bin`, or an artifact/assets `.zip`, plus an optional bootloader / partition table |

**GitHub token (optional).** GitHub only lets signed-in users download Actions artifacts.

- **Without a token**, artifacts are fetched through [nightly.link](https://nightly.link). The GitHub API is
  limited to 60 requests per hour.
- **With a token**, artifacts are downloaded directly and the API limit is higher. A
  [fine-grained token](https://github.com/settings/personal-access-tokens) with *public repositories read-only*
  access is enough.

The token can also be supplied with the `GITHUB_TOKEN` environment variable. Tick **Remember** to store it in
the settings file.

Downloads are cached in the user cache folder (**Open download folder**). Release assets are keyed by asset ID,
so a rebuilt Development Build is always downloaded fresh.

---

## USB drivers

| OS | |
|---|---|
| **Windows** | The app checks for CP210x / CH340 / FTDI adapters with a missing driver. **Install Driver** runs the bundled installer: CP210x via `pnputil` (UAC prompt), CH340 via `CH341SER.EXE`. A missing driver is downloaded on demand. FTDI drivers come from Windows Update. |
| **macOS** | macOS 11+ includes CP210x, CH34x and FTDI drivers. |
| **Linux** | Kernel drivers are built in. Your user must be in the `dialout` group (`uucp` on Arch). **Fix Permissions** does this, then log out and back in. Alternatively install `99-openevse-usb-serial.rules`. On Ubuntu, `brltty` can grab CH340 adapters: `sudo apt remove brltty`. |

---

## Command line

```text
OpenEVSE_ESP32_Flasher ports                        # list serial ports
OpenEVSE_ESP32_Flasher detect -p COM5               # chip, flash size, partition layout
OpenEVSE_ESP32_Flasher list releases                # or: list actions --branch master
OpenEVSE_ESP32_Flasher flash -p /dev/ttyUSB0 --board openevse_wifi_v1            # newest release
OpenEVSE_ESP32_Flasher flash -p COM5 --board openevse_tft_wifi_v1 --dev          # Development Build
OpenEVSE_ESP32_Flasher flash -p COM5 --board wifi_v1 --actions 1234567890        # Actions run id
OpenEVSE_ESP32_Flasher flash -p COM5 --board wifi_v1 --file firmware.bin --app-only
OpenEVSE_ESP32_Flasher esptool chip-id -p COM5      # raw bundled esptool
OpenEVSE_ESP32_Flasher drivers | install-driver cp210x|ch340
```

Other `flash` options:

- `--layout auto|4mb|16mb`
- `--erase`
- `--baud 921600`
- `--manual-boot`
- `--bootloader F` / `--partitions F` (with `--file`)
- `-y` (no confirmation)

From source, use `python openevse_esp32_flasher.py …` or `bash run.sh …`.

---

## Building

```bash
bash build.sh        # macOS / Linux  -> OpenEVSE_ESP32_Flasher-<platform>.zip
build.bat            # Windows        -> OpenEVSE_ESP32_Flasher-windows.zip
```

The build does the following:

1. Installs PyInstaller and the requirements.
2. Fetches the Windows drivers into `drivers/windows/` (`scripts/fetch_drivers.py`).
3. Runs the unit tests.
4. Builds a single-file executable with esptool's flasher stubs embedded.
5. Smoke-tests the bundled esptool.

Every push builds Windows, macOS (Apple Silicon and Intel) and Linux executables as workflow artifacts. Pushing a `v*.*.*` tag also attaches them to a GitHub Release
(`.github/workflows/release.yml`).

> Silicon Labs sometimes blocks automated downloads of the CP210x driver. If the build log shows a
> 403, there are two fixes. You can set the repository variable `CP210X_DRIVER_URL` to a mirror. Or you can
> extract `CP210x_Universal_Windows_Driver.zip` into `drivers/windows/CP210x/` and commit it (remove that line
> from `.gitignore`). Either way, the app still downloads the driver on demand.

Tests: `python -m unittest discover -s tests`

---

## Troubleshooting

| Problem | Fix |
|---|---|
| No serial port listed | Check the cable (some are charge-only). Install the driver (Windows). On Linux, check `dialout` and `brltty`. |
| *Failed to connect* | Use manual boot mode (see above). Try 115200 baud. |
| *Could not open port* | Close serial monitors / Arduino IDE. On Linux, check permissions. |
| Board boot-loops after flashing | Do a **Full install** with **Erase all flash first**. |
| *rate limit reached* | Add a GitHub token. |
| TFT board refused on 4MB flash | The TFT firmware needs 16MB flash. |

---

## Project layout

```
openevse_esp32_flasher.py      application (GUI + CLI + esptool worker)
openevse_esp32_flasher.spec    PyInstaller build definition
requirements.txt               esptool, pyserial, certifi
run.sh / run.bat               run from source (installs deps into vendor/)
build.sh / build.bat           build release zips
scripts/fetch_drivers.py       downloads Windows drivers to bundle
drivers/windows/               CP210x + CH340 drivers (fetched at build time)
drivers/linux/                 optional udev rule
tests/                         unit tests + real partition table fixtures
```
