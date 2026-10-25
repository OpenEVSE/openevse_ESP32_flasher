#!/usr/bin/env python3
"""
OpenEVSE ESP32 WiFi Firmware Flasher

Flashes the OpenEVSE WiFi gateway firmware onto ESP32 based OpenEVSE boards:

  * openevse_wifi_v1      - OpenEVSE WiFi V1 module (4MB or 16MB flash)
  * openevse_tft_wifi_v1  - OpenEVSE TFT WiFi V1 module (16MB flash)

The ESP32 flash size is read over USB and the matching build / partition
layout is chosen automatically:

  board            flash   firmware                   bootloader / partitions
  ---------------  ------  -------------------------  ---------------------------------
  openevse_wifi_v1   4MB   openevse_wifi_v1.bin       bootloader_4mb / partitions_4mb
  openevse_wifi_v1  16MB   openevse_wifi_v1_16mb.bin  bootloader_v1_16mb / partitions_v1_16mb
  TFT wifi v1       16MB   openevse_wifi_tft_v1.bin   bootloader_16mb / partitions_16mb

Firmware sources: GitHub Releases, the "latest" Development Build, GitHub
Actions build artifacts, or local files.

esptool and pyserial are bundled (PyInstaller build, or the 'vendor/' folder
next to this script). USB-serial drivers for Windows (CP210x / CH340) are
bundled in 'drivers/' or downloaded on demand.

Run without arguments for the GUI, or with a sub-command for the CLI:
    openevse_esp32_flasher.py --help
"""

import sys, os

# ---------------------------------------------------------------------------
# Paths / bundled packages (must run before importing esptool / serial)
# ---------------------------------------------------------------------------
FROZEN  = getattr(sys, "frozen", False)
APP_DIR = os.path.dirname(os.path.abspath(sys.executable if FROZEN else __file__))
RES_DIR = getattr(sys, "_MEIPASS", APP_DIR)        # PyInstaller data dir
VENDOR  = os.path.join(APP_DIR, "vendor")
if os.path.isdir(VENDOR) and VENDOR not in sys.path:
    sys.path.insert(0, VENDOR)

import re, json, time, queue, shutil, struct, hashlib, zipfile, argparse
import platform, tempfile, threading, subprocess, urllib.request, urllib.error
from dataclasses import dataclass, field

APP_NAME    = "OpenEVSE ESP32 Flasher"
APP_VERSION = "1.0.0"
USER_AGENT  = "OpenEVSE-ESP32-Flasher/" + APP_VERSION

# ---------------------------------------------------------------------------
# Firmware repository
# ---------------------------------------------------------------------------
FW_REPO       = "OpenEVSE/openevse_esp32_firmware"
FW_WORKFLOW   = "build.yaml"
DEV_TAG       = "latest"           # master builds are published to this tag
GH_API        = "https://api.github.com"
NIGHTLY_LINK  = "https://nightly.link/{repo}/actions/runs/{run_id}/{name}.zip"

# ESP32 (classic) flash map
ADDR_BOOTLOADER = 0x1000
ADDR_PARTITIONS = 0x8000
ADDR_OTADATA    = 0xE000
OTADATA_SIZE    = 0x2000
ADDR_APP        = 0x10000
PART_TABLE_LEN  = 0xC00

DEFAULT_BAUD = "460800"
BAUD_RATES   = ["921600", "460800", "230400", "115200"]

# ---------------------------------------------------------------------------
# USB serial adapters / drivers
# ---------------------------------------------------------------------------
USB_CHIPS = {
    # vid: (name, driver key)
    0x10C4: ("Silicon Labs CP210x", "cp210x"),
    0x1A86: ("WCH CH340/CH9102",    "ch340"),
    0x0403: ("FTDI FT232",          "ftdi"),
    0x303A: ("Espressif USB",       None),
}
DRIVERS = {
    "cp210x": {
        "name":   "Silicon Labs CP210x USB to UART",
        "dir":    "CP210x",
        "url":    "https://www.silabs.com/documents/public/software/CP210x_Universal_Windows_Driver.zip",
        "page":   "https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers",
    },
    "ch340": {
        "name":   "WCH CH340/CH341 USB to serial",
        "dir":    "CH341SER",
        "url":    "https://www.wch-ic.com/download/file?id=65",
        "page":   "https://www.wch-ic.com/downloads/CH341SER_EXE.html",
    },
    "ftdi": {
        "name":   "FTDI VCP",
        "dir":    "FTDI",
        "url":    None,     # installed automatically by Windows Update
        "page":   "https://ftdichip.com/drivers/vcp-drivers/",
    },
}

# ---------------------------------------------------------------------------
# Partition tables (copied from openevse_esp32_firmware)
# ---------------------------------------------------------------------------
PARTITIONS_4MB_CSV = """\
# Name,   Type, SubType, Offset,  Size, Flags
nvs,      data, nvs,     0x9000,  0x5000,
otadata,  data, ota,     0xe000,  0x2000,
app0,     app,  ota_0,   0x10000, 0x1E0000,
app1,     app,  ota_1,   0x1F0000,0x1E0000,
spiffs,   data, spiffs,  0x3D0000,0x20000,
coredump, data, coredump,0x3F0000,0x10000,
"""

PARTITIONS_16MB_CSV = """\
# Name,   Type, SubType, Offset,  Size, Flags
nvs,      data, nvs,     0x9000,  0x5000,
otadata,  data, ota,     0xe000,  0x2000,
app0,     app,  ota_0,   0x10000, 0x640000,
app1,     app,  ota_1,   0x650000,0x640000,
spiffs,   data, spiffs,  0xc90000,0x360000,
coredump, data, coredump,0xFF0000,0x10000,
"""

PART_TYPES    = {"app": 0x00, "data": 0x01}
PART_SUBTYPES = {
    0x00: {"factory": 0x00, "test": 0x20,
           **{"ota_%d" % i: 0x10 + i for i in range(16)}},
    0x01: {"ota": 0x00, "phy": 0x01, "nvs": 0x02, "coredump": 0x03,
           "nvs_keys": 0x04, "efuse": 0x05, "undefined": 0x06,
           "esphttpd": 0x80, "fat": 0x81, "spiffs": 0x82, "littlefs": 0x83},
}
PART_MAGIC = b"\xAA\x50"
MD5_MAGIC  = b"\xEB\xEB"


@dataclass(frozen=True)
class Partition:
    name: str
    type: int
    subtype: int
    offset: int
    size: int
    flags: int = 0

    @property
    def end(self):
        return self.offset + self.size

    def layout_key(self):
        return (self.type, self.subtype, self.offset, self.size)


def parse_partitions_csv(text):
    parts = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        cols = [c.strip() for c in line.split(",")]
        cols += [""] * (6 - len(cols))
        name, ptype, sub, off, size, flags = cols[:6]
        t = PART_TYPES[ptype]
        parts.append(Partition(name, t, PART_SUBTYPES[t][sub],
                               int(off, 0), int(size, 0),
                               1 if "encrypted" in flags else 0))
    return parts


def build_partition_table(parts):
    """Binary partition table identical to ESP-IDF gen_esp32part.py output."""
    data = b"".join(
        struct.pack("<2sBBLL16sL", PART_MAGIC, p.type, p.subtype,
                    p.offset, p.size, p.name.encode()[:16], p.flags)
        for p in parts)
    data += MD5_MAGIC + b"\xFF" * 14 + hashlib.md5(data).digest()
    return data + b"\xFF" * (PART_TABLE_LEN - len(data))


def parse_partition_table(data):
    """Parse a binary partition table. Returns a list of Partition, [] if the
    region is blank, or raises ValueError if it is not a partition table."""
    parts, md5_ok = [], None
    for i in range(0, min(len(data), PART_TABLE_LEN), 32):
        entry = data[i:i + 32]
        if len(entry) < 32 or entry == b"\xFF" * 32:
            break
        if entry[:2] == MD5_MAGIC:
            md5_ok = entry[16:32] == hashlib.md5(data[:i]).digest()
            break
        if entry[:2] != PART_MAGIC:
            raise ValueError("invalid partition entry at offset 0x%X" % i)
        _, t, st, off, size, name, flags = struct.unpack("<2sBBLL16sL", entry)
        parts.append(Partition(name.rstrip(b"\x00").decode(errors="replace"),
                               t, st, off, size, flags))
    if md5_ok is False:
        raise ValueError("partition table MD5 mismatch")
    return parts


def same_layout(a, b):
    return bool(a) and bool(b) and \
        [p.layout_key() for p in a] == [p.layout_key() for p in b]


def table_flash_size(parts):
    return max((p.end for p in parts), default=0)


def find_partition(parts, type_, subtype):
    return next((p for p in parts if p.type == type_ and p.subtype == subtype), None)


# ---------------------------------------------------------------------------
# Boards and flash-size variants
# ---------------------------------------------------------------------------
MB = 1024 * 1024


@dataclass(frozen=True)
class Variant:
    key: str
    env: str                 # PlatformIO env / Actions artifact name prefix
    flash_mb: int
    app_asset: str
    bootloader_asset: str
    partitions_asset: str
    partitions_csv: str
    idf_major: int           # 4 = Arduino core 2.x, 5 = Arduino core 3.x
    flash_mode: str = "dio"  # validated settings for USB flashing
    flash_freq: str = "40m"

    @property
    def flash_size(self):
        return "%dMB" % self.flash_mb

    @property
    def partitions(self):
        return parse_partitions_csv(self.partitions_csv)

    @property
    def artifact_name(self):
        return self.env + ".bin"


VARIANTS = {
    "wifi_v1_4mb": Variant(
        "wifi_v1_4mb", "openevse_wifi_v1", 4,
        "openevse_wifi_v1.bin", "bootloader_4mb.bin", "partitions_4mb.bin",
        PARTITIONS_4MB_CSV, idf_major=4),
    "wifi_v1_16mb": Variant(
        "wifi_v1_16mb", "openevse_wifi_v1_16mb", 16,
        "openevse_wifi_v1_16mb.bin", "bootloader_v1_16mb.bin", "partitions_v1_16mb.bin",
        PARTITIONS_16MB_CSV, idf_major=5),
    "tft_v1_16mb": Variant(
        "tft_v1_16mb", "openevse_wifi_tft_v1", 16,
        "openevse_wifi_tft_v1.bin", "bootloader_16mb.bin", "partitions_16mb.bin",
        PARTITIONS_16MB_CSV, idf_major=5),
}

BOARDS = {
    "openevse_wifi_v1": {
        "label":    "OpenEVSE WiFi V1",
        "variants": {4: "wifi_v1_4mb", 16: "wifi_v1_16mb"},
    },
    "openevse_tft_wifi_v1": {
        "label":    "OpenEVSE TFT WiFi V1",
        "variants": {16: "tft_v1_16mb"},
    },
}
BOARD_ALIASES = {"wifi_v1": "openevse_wifi_v1", "tft": "openevse_tft_wifi_v1",
                 "openevse_wifi_tft_v1": "openevse_tft_wifi_v1"}

KNOWN_LAYOUTS = [
    ("OpenEVSE 4MB (min_spiffs)", parse_partitions_csv(PARTITIONS_4MB_CSV)),
    ("OpenEVSE 16MB",             parse_partitions_csv(PARTITIONS_16MB_CSV)),
]


def parse_size_mb(text):
    m = re.match(r"\s*(\d+)\s*([KM])B", text or "", re.I)
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2).upper() == "M" else n / 1024


def choose_variant(board, flash_mb, layout="auto"):
    """Pick the firmware variant for a board given the detected flash size.
    layout: 'auto', '4mb' or '16mb'. Raises ValueError if nothing fits."""
    board = BOARD_ALIASES.get(board, board)
    variants = BOARDS[board]["variants"]
    label = BOARDS[board]["label"]
    if layout and layout != "auto":
        want = int(layout.lower().rstrip("mb"))
        if want not in variants:
            raise ValueError("%s has no %dMB build." % (label, want))
        v = VARIANTS[variants[want]]
        if flash_mb and v.flash_mb > flash_mb:
            raise ValueError("%s layout needs %dMB flash but the ESP32 has %sMB."
                             % (v.flash_size, v.flash_mb, _fmt_mb(flash_mb)))
        return v
    if not flash_mb:
        raise ValueError("Flash size unknown - click Detect, or choose a layout manually.")
    fitting = [mb for mb in variants if mb <= flash_mb]
    if not fitting:
        raise ValueError("%s needs at least %dMB flash; this ESP32 has %sMB."
                         % (label, min(variants), _fmt_mb(flash_mb)))
    return VARIANTS[variants[max(fitting)]]


def _fmt_mb(mb):
    return ("%g" % mb) if mb else "?"


def identify_layout(parts):
    if not parts:
        return "blank / erased"
    for name, known in KNOWN_LAYOUTS:
        if same_layout(parts, known):
            return name
    return "unknown (%d partitions, %dMB)" % (len(parts), table_flash_size(parts) // MB)


# ---------------------------------------------------------------------------
# Image inspection
# ---------------------------------------------------------------------------
ESP_IMAGE_MAGIC   = 0xE9
APP_DESC_MAGIC    = 0xABCD5432


def app_info(data):
    """Read esp_app_desc_t from an application image (None if absent)."""
    if len(data) < 256 or data[0] != ESP_IMAGE_MAGIC:
        return None
    off = 24 + 8   # image header + first segment header
    magic, = struct.unpack_from("<L", data, off)
    if magic != APP_DESC_MAGIC:
        return None
    def s(o, n):
        return data[off + o:off + o + n].split(b"\x00", 1)[0].decode(errors="replace")
    return {"version": s(16, 32), "project": s(48, 32), "time": s(80, 16),
            "date": s(96, 16), "idf": s(112, 32)}


def idf_major(info):
    m = re.match(r"v?(\d+)", (info or {}).get("idf", ""))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Config / cache
# ---------------------------------------------------------------------------

def user_dir(kind):
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA" if kind == "cache" else "APPDATA",
                              os.path.expanduser("~"))
    elif system == "Darwin":
        base = os.path.expanduser("~/Library/" +
                                  ("Caches" if kind == "cache" else "Application Support"))
    else:
        base = os.environ.get("XDG_CACHE_HOME" if kind == "cache" else "XDG_CONFIG_HOME",
                              os.path.expanduser("~/.cache" if kind == "cache" else "~/.config"))
    path = os.path.join(base, "openevse_esp32_flasher")
    os.makedirs(path, exist_ok=True)
    return path


CONFIG_PATH = None


def load_config():
    global CONFIG_PATH
    CONFIG_PATH = os.path.join(user_dir("config"), "config.json")
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH or os.path.join(user_dir("config"), "config.json"), "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def cache_dir(*sub):
    path = os.path.join(user_dir("cache"), *sub)
    os.makedirs(path, exist_ok=True)
    return path


def resource_path(*parts):
    """Bundled resource: inside the PyInstaller bundle, else next to the script."""
    for base in (RES_DIR, APP_DIR):
        p = os.path.join(base, *parts)
        if os.path.exists(p):
            return p
    return None


# ---------------------------------------------------------------------------
# HTTP / GitHub
# ---------------------------------------------------------------------------

def _ssl_context():
    """System trust store plus certifi (frozen apps on macOS have no usable
    system bundle; corporate proxies add their CA to the system store)."""
    import ssl
    ctx = ssl.create_default_context()
    extra = [os.environ.get("SSL_CERT_FILE"), os.environ.get("REQUESTS_CA_BUNDLE")]
    try:
        import certifi
        extra.append(certifi.where())
    except ImportError:
        pass
    for path in filter(None, extra):
        try:
            ctx.load_verify_locations(cafile=path)
        except (OSError, ssl.SSLError):
            pass
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo=FW_REPO, token=None):
        self.repo  = repo
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        self.ctx   = _ssl_context()

    def _headers(self, auth=True):
        h = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        if auth and self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def api(self, path):
        url = path if path.startswith("http") else GH_API + path
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=20, context=self.ctx) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise GitHubError(self._explain(e)) from None
        except urllib.error.URLError as e:
            raise GitHubError("Network error: %s" % e.reason) from None

    def _explain(self, e):
        try:
            msg = json.loads(e.read().decode()).get("message", "")
        except Exception:
            msg = ""
        if e.code in (403, 429) and e.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(e.headers.get("X-RateLimit-Reset", "0") or 0)
            wait = max(0, reset - int(time.time())) // 60
            return ("GitHub API rate limit reached (try again in ~%d min). "
                    "Add a GitHub token to raise the limit." % wait)
        if e.code == 401:
            return "GitHub rejected the token (401). Check or clear the token."
        return "GitHub HTTP %d %s" % (e.code, msg or e.reason)

    # releases ---------------------------------------------------------------
    def releases(self, count=40):
        return self.api("/repos/%s/releases?per_page=%d" % (self.repo, count))

    # actions ----------------------------------------------------------------
    def workflow_runs(self, branch=None, count=30):
        q = "per_page=%d&status=success" % count
        if branch:
            q += "&branch=" + urllib.request.quote(branch)
        return self.api("/repos/%s/actions/workflows/%s/runs?%s"
                        % (self.repo, FW_WORKFLOW, q)).get("workflow_runs", [])

    def run_artifacts(self, run_id):
        return self.api("/repos/%s/actions/runs/%s/artifacts?per_page=100"
                        % (self.repo, run_id)).get("artifacts", [])

    def artifact_download_url(self, run_id, artifact):
        """Signed download URL for an artifact zip. Without a token GitHub does
        not allow artifact downloads, so nightly.link is used instead."""
        if not self.token:
            return NIGHTLY_LINK.format(repo=self.repo, run_id=run_id,
                                       name=artifact["name"]), False
        opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPSHandler(context=self.ctx))
        req = urllib.request.Request(artifact["archive_download_url"],
                                     headers=self._headers())
        try:
            opener.open(req, timeout=20)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                return e.headers["Location"], True
            raise GitHubError(self._explain(e)) from None
        raise GitHubError("Unexpected response downloading artifact.")

    # downloads --------------------------------------------------------------
    def download(self, url, dest, log=None, progress=None, cancel=None):
        if os.path.isfile(dest) and os.path.getsize(dest) > 0:
            if log:
                log("Using cached " + os.path.basename(dest))
            return dest
        tmp = dest + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/octet-stream"})
        if log:
            log("Downloading " + url)
        try:
            with urllib.request.urlopen(req, timeout=120, context=self.ctx) as resp, \
                    open(tmp, "wb") as fh:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    if cancel and cancel.is_set():
                        raise CancelledError()
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress and total:
                        progress(100.0 * done / total, "Downloading")
        except urllib.error.HTTPError as e:
            _silent_remove(tmp)
            raise GitHubError("Download failed: HTTP %d %s" % (e.code, e.reason)) from None
        except urllib.error.URLError as e:
            _silent_remove(tmp)
            raise GitHubError("Download failed: %s" % e.reason) from None
        except BaseException:
            _silent_remove(tmp)
            raise
        os.replace(tmp, dest)
        return dest


def _silent_remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


class CancelledError(Exception):
    pass


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text))


# ---------------------------------------------------------------------------
# Firmware sources
# ---------------------------------------------------------------------------

@dataclass
class FirmwareSet:
    """Resolved files for one flash operation."""
    description: str
    app: str
    bootloader: str = None
    partitions: str = None
    notes: list = field(default_factory=list)


def is_stable_release(rel):
    return rel.get("tag_name") != DEV_TAG and not rel.get("draft")


def release_label(rel):
    tag = rel["tag_name"]
    date = (rel.get("published_at") or "")[:10]
    if tag == DEV_TAG:
        return "Development Build (%s)" % date
    pre = "  [pre-release]" if rel.get("prerelease") else ""
    return "%s  (%s)%s" % (tag, date, pre)


def _asset(rel, name):
    return next((a for a in rel.get("assets", []) if a["name"] == name), None)


def release_has_variant(rel, variant):
    return _asset(rel, variant.app_asset) is not None


def _fetch_asset(gh, rel, asset, log, progress, cancel):
    folder = cache_dir("releases", _safe_name(rel["tag_name"]))
    # asset ids change when a file is re-uploaded (the 'latest' tag is rebuilt
    # on every master push) so they make a safe cache key
    dest = os.path.join(folder, "%s_%s" % (asset["id"], _safe_name(asset["name"])))
    return gh.download(asset["browser_download_url"], dest, log, progress, cancel)


def generated_partitions(variant):
    path = os.path.join(cache_dir("generated"), "partitions_%s.bin" % variant.key)
    with open(path, "wb") as f:
        f.write(build_partition_table(variant.partitions))
    return path


def blank_otadata():
    path = os.path.join(cache_dir("generated"), "otadata_blank.bin")
    if not os.path.isfile(path):
        with open(path, "wb") as f:
            f.write(b"\xFF" * OTADATA_SIZE)
    return path


def fill_boot_files(fw, variant, gh, releases, log, progress=None, cancel=None):
    """Provide a bootloader / partition table when the source lacks them.
    The partition table is regenerated from the CSV the firmware is built with;
    the bootloader is taken from the newest release that publishes one."""
    if not fw.partitions:
        fw.partitions = generated_partitions(variant)
        fw.notes.append("partition table generated from built-in %s layout"
                        % variant.flash_size)
    if not fw.bootloader:
        ordered = [r for r in releases or [] if is_stable_release(r)] + \
                  [r for r in releases or [] if not is_stable_release(r)]
        for rel in ordered:
            a = _asset(rel, variant.bootloader_asset)
            if a:
                fw.bootloader = _fetch_asset(gh, rel, a, log, progress, cancel)
                fw.notes.append("bootloader taken from release %s" % rel["tag_name"])
                break
    if not fw.bootloader:
        raise ValueError("No %s available. Pick a bootloader file, or use "
                         "'Firmware only' mode." % variant.bootloader_asset)
    return fw


def resolve_release(gh, rel, variant, need_boot, releases, log, progress=None, cancel=None):
    app = _asset(rel, variant.app_asset)
    if not app:
        raise ValueError("Release %s has no %s." % (rel["tag_name"], variant.app_asset))
    fw = FirmwareSet(release_label(rel) + " / " + variant.app_asset,
                     _fetch_asset(gh, rel, app, log, progress, cancel))
    if need_boot:
        for attr, name in (("bootloader", variant.bootloader_asset),
                           ("partitions", variant.partitions_asset)):
            a = _asset(rel, name)
            if a:
                setattr(fw, attr, _fetch_asset(gh, rel, a, log, progress, cancel))
        fill_boot_files(fw, variant, gh, releases, log, progress, cancel)
    return fw


def run_label(run):
    title = run.get("display_title") or (run.get("head_commit") or {}).get("message") or ""
    title = title.splitlines()[0][:60] if title else ""
    prs = run.get("pull_requests") or []
    where = ("PR #%d" % prs[0]["number"]) if prs else run.get("head_branch", "")
    return "#%s  %s  %s  %s  %s" % (run.get("run_number"), where,
                                    (run.get("head_sha") or "")[:7],
                                    (run.get("created_at") or "")[:10], title)


def extract_build_zip(zpath, variant, out_dir):
    """Pull firmware / bootloader / partitions out of an Actions artifact
    (firmware.bin, bootloader.bin, partitions.bin) or a zip of release assets."""
    found = {}
    wanted = {
        "app":        ("firmware.bin", variant.app_asset),
        "bootloader": ("bootloader.bin", variant.bootloader_asset),
        "partitions": ("partitions.bin", variant.partitions_asset),
    }
    with zipfile.ZipFile(zpath) as z:
        names = {os.path.basename(n): n for n in z.namelist() if not n.endswith("/")}
        for key, candidates in wanted.items():
            for c in candidates:
                if c in names:
                    dest = os.path.join(out_dir, key + ".bin")
                    with z.open(names[c]) as src, open(dest, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    found[key] = dest
                    break
    if "app" not in found:
        raise ValueError("No firmware.bin / %s found in %s"
                         % (variant.app_asset, os.path.basename(zpath)))
    return found


def resolve_actions(gh, run, variant, need_boot, releases, log, progress=None, cancel=None):
    arts = gh.run_artifacts(run["id"])
    art = next((a for a in arts if a["name"] == variant.artifact_name), None)
    if not art:
        raise ValueError("Build run #%s has no %s artifact."
                         % (run.get("run_number"), variant.artifact_name))
    if art.get("expired"):
        raise ValueError("Artifact %s from run #%s has expired."
                         % (art["name"], run.get("run_number")))
    folder = cache_dir("actions", str(art["id"]))
    zpath = os.path.join(folder, _safe_name(art["name"]) + ".zip")
    if not os.path.isfile(zpath):
        url, signed = gh.artifact_download_url(run["id"], art)
        if not signed:
            log("No GitHub token - downloading artifact via nightly.link")
        gh.download(url, zpath, log, progress, cancel)
    files = extract_build_zip(zpath, variant, folder)
    fw = FirmwareSet("Actions run %s / %s" % (run_label(run), art["name"]),
                     files["app"], files.get("bootloader"), files.get("partitions"))
    if need_boot:
        fill_boot_files(fw, variant, gh, releases, log, progress, cancel)
    return fw


def resolve_local(app, bootloader, partitions, variant, need_boot, gh, releases, log,
                  progress=None, cancel=None):
    if not app or not os.path.isfile(app):
        raise ValueError("Select a firmware file.")
    if app.lower().endswith(".zip"):
        folder = cache_dir("local", hashlib.sha1(os.path.abspath(app).encode()).hexdigest()[:12])
        files = extract_build_zip(app, variant, folder)
        app = files["app"]
        bootloader = bootloader or files.get("bootloader")
        partitions = partitions or files.get("partitions")
    fw = FirmwareSet("Local file " + os.path.basename(app), app,
                     bootloader or None, partitions or None)
    if need_boot:
        fill_boot_files(fw, variant, gh, releases, log, progress, cancel)
    return fw


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_firmware(fw, variant, flash_mb, need_boot, device_parts, log):
    """Sanity check the images. Returns list of warnings; raises on hard errors."""
    warnings = []
    with open(fw.app, "rb") as f:
        app = f.read()
    if not app or app[0] != ESP_IMAGE_MAGIC:
        raise ValueError("%s is not an ESP32 application image." % os.path.basename(fw.app))
    info = app_info(app)
    if info:
        if info["project"] == "arduino-lib-builder":
            # Arduino core 2.x images carry the prebuilt core's descriptor
            log("Firmware: Arduino core 2.x build (IDF %s)" % info["idf"])
        else:
            log("Firmware: %s %s  (built %s %s, IDF %s)" % (
                info["project"], info["version"], info["date"], info["time"], info["idf"]))
        major = idf_major(info)
        if major and major != variant.idf_major:
            warnings.append(
                "This image was built with ESP-IDF %s but the %s build uses IDF v%d.x - "
                "it is probably the wrong file for this board/flash size."
                % (info["idf"], variant.env, variant.idf_major))

    if need_boot:
        with open(fw.bootloader, "rb") as f:
            if f.read(1) != bytes([ESP_IMAGE_MAGIC]):
                raise ValueError("%s is not an ESP32 bootloader image."
                                 % os.path.basename(fw.bootloader))
        with open(fw.partitions, "rb") as f:
            target = parse_partition_table(f.read())
    else:
        target = device_parts or variant.partitions

    if not target:
        raise ValueError("Partition table is empty.")
    need = table_flash_size(target)
    if flash_mb and need > flash_mb * MB:
        raise ValueError("Partition table needs %dMB flash but the ESP32 has %sMB."
                         % (need // MB, _fmt_mb(flash_mb)))
    if not same_layout(target, variant.partitions):
        warnings.append("Partition table differs from the standard %s OpenEVSE layout."
                        % variant.flash_size)
    app0 = find_partition(target, 0x00, 0x10) or find_partition(target, 0x00, 0x00)
    if not app0 or app0.offset != ADDR_APP:
        raise ValueError("Partition table has no app partition at 0x%X." % ADDR_APP)
    if len(app) > app0.size:
        raise ValueError("Firmware (%d bytes) does not fit the %d byte app partition."
                         % (len(app), app0.size))
    log("Firmware size %s bytes - %.1f%% of app partition"
        % (format(len(app), ","), 100.0 * len(app) / app0.size))
    return warnings


# ---------------------------------------------------------------------------
# esptool runner
# ---------------------------------------------------------------------------
PROGRESS_TAG = "@@PROGRESS"
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _ensure_stdout():
    """Windowed (PyInstaller --noconsole) builds start with sys.stdout = None.
    The worker is always started with a pipe on fd 1, so use that."""
    if sys.stdout is None:
        try:
            sys.stdout = open(1, "w", encoding="utf-8", errors="replace",
                              buffering=1, closefd=False)
        except OSError:
            sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = sys.stdout


def _attach_console():
    """Let the windowed Windows exe print CLI output: to an inherited pipe or
    file if there is one, else to the console it was started from."""
    if sys.stdout is not None:
        return
    try:
        sys.stdout = sys.stderr = open(1, "w", encoding="utf-8", errors="replace",
                                       buffering=1, closefd=False)
        return
    except OSError:
        pass
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdout = sys.stderr = open("CONOUT$", "w", buffering=1)
            sys.stdin = open("CONIN$")
    except Exception:
        pass


def esptool_worker(argv):
    """Runs inside a child process: esptool with a log-friendly logger."""
    _ensure_stdout()
    import esptool
    major = int(re.match(r"(\d+)", getattr(esptool, "__version__", "5")).group(1))
    if major < 5:
        argv = _esptool_v4_args(argv)
    try:
        from esptool.logger import log, TemplateLogger

        class PipeLogger(TemplateLogger):
            def _w(self, text):
                sys.stdout.write(text)
                sys.stdout.flush()

            def print(self, *args, **kwargs):
                self._w(kwargs.get("sep", " ").join(str(a) for a in args)
                        + kwargs.get("end", "\n"))

            def note(self, message):
                self._w("Note: %s\n" % message)

            def warning(self, message):
                self._w("Warning: %s\n" % message)

            def error(self, message):
                self._w("Error: %s\n" % message)

            def stage(self, finish=False):
                pass

            def progress_bar(self, cur_iter, total_iters, prefix="", suffix="", bar_length=30):
                pct = 100.0 if total_iters <= 0 else 100.0 * cur_iter / total_iters
                self._w("%s %.1f %s\n" % (PROGRESS_TAG, pct, prefix.strip()))

            def set_verbosity(self, verbosity):
                pass

        log.set_logger(PipeLogger())
    except Exception:
        pass   # older esptool: plain output, progress parsed from "(NN %)"
    try:
        esptool.main(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    except Exception as e:
        print("\nA fatal error occurred: %s" % e)
        return 2
    return 0


def _esptool_v4_args(argv):
    out = []
    for a in argv:
        if re.fullmatch(r"[a-z]+(-[a-z]+)+", a) or a.startswith("--"):
            a = a.replace("-", "_") if not a.startswith("--") else "--" + a[2:].replace("-", "_")
        out.append(a)
    return out


def esptool_command(args):
    if FROZEN:
        return [sys.executable, "--esptool-worker"] + args
    return [sys.executable, os.path.abspath(__file__), "--esptool-worker"] + args


class EsptoolError(RuntimeError):
    pass


def run_esptool(args, log, progress=None, cancel=None):
    """Run esptool in a child process, streaming output. Returns the output."""
    cmd = esptool_command(args)
    log("$ esptool " + " ".join(args))
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8",
               NO_COLOR="1", TERM="dumb")
    kw = {}
    if platform.system() == "Windows":
        kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, env=env, **kw)
    out_lines, buf, last_stage = [], b"", None
    watcher = None
    if cancel is not None:
        def watch():
            while proc.poll() is None:
                if cancel.wait(0.2):
                    proc.terminate()
                    return
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()

    def handle(line):
        nonlocal last_stage
        line = ANSI_RE.sub("", line).rstrip()
        if not line.strip():
            return
        if line.startswith(PROGRESS_TAG):
            parts = line.split(None, 2)
            try:
                pct = float(parts[1])
            except (IndexError, ValueError):
                return
            stage = parts[2] if len(parts) > 2 else ""
            if progress:
                progress(pct, stage)
            if stage != last_stage:
                last_stage = stage
            return
        m = re.search(r"\((\d+)\s?%\)", line)
        if m and progress:
            progress(float(m.group(1)), line.split("...")[0].strip())
            if not line.rstrip().endswith("(100 %)"):
                return
        out_lines.append(line)
        log(line)

    while True:
        chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(1)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for p in parts:
            handle(p.decode("utf-8", "replace"))
    if buf:
        handle(buf.decode("utf-8", "replace"))
    proc.stdout.close()
    rc = proc.wait()
    if cancel is not None and cancel.is_set():
        raise CancelledError()
    output = "\n".join(out_lines)
    if rc != 0:
        raise EsptoolError(_esptool_hint(output) or "esptool exited with code %d" % rc)
    return output


def _esptool_hint(output):
    low = output.lower()
    if "failed to connect" in low or "no serial data received" in low \
            or "wrong boot mode" in low or "invalid head of packet" in low:
        return ("Could not enter the ESP32 bootloader. Hold the BOOT / WiFi button "
                "(GPIO0), power-cycle or reset the board, release the button, then "
                "try again with 'Manual boot mode' ticked.")
    if "could not open port" in low or "permission denied" in low or "access is denied" in low:
        return ("Could not open the serial port. Close other programs using it "
                "(serial monitors, the Arduino IDE) and check USB permissions/driver.")
    if "unexpected chip id" in low or "this chip is" in low:
        return "The connected chip is not an ESP32."
    return None


# ---------------------------------------------------------------------------
# Serial ports / drivers
# ---------------------------------------------------------------------------

@dataclass
class Port:
    device: str
    description: str
    vid: int = None
    pid: int = None

    @property
    def chip(self):
        return USB_CHIPS.get(self.vid, (None, None))[0]

    @property
    def known(self):
        return self.vid in USB_CHIPS

    def label(self):
        ids = " [%04X:%04X]" % (self.vid, self.pid) if self.vid is not None else ""
        desc = self.chip or self.description or ""
        return "%s - %s%s" % (self.device, desc, ids)


def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        return []
    ports = []
    for p in lp.comports():
        if platform.system() == "Darwin" and "/cu." not in p.device:
            continue    # use the callout devices on macOS
        if platform.system() == "Linux" and p.vid is None and "ttyS" in p.device:
            continue    # legacy on-board UARTs
        ports.append(Port(p.device, p.description or "", p.vid, p.pid))
    ports.sort(key=lambda p: (not p.known, p.device))
    return ports


def _powershell(script, timeout=20):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=timeout,
                       creationflags=0x08000000)
    return r.returncode, r.stdout, r.stderr


def windows_usb_devices():
    """USB serial adapters known to Windows: list of dicts with vid, status."""
    vids = "|".join("%04X" % v for v in USB_CHIPS)
    script = ("Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match "
              "'VID_(%s)' -and $_.InstanceId -match '^(USB|FTDIBUS)' } | "
              "ForEach-Object { \"$($_.InstanceId)|$($_.Status)|$($_.Class)|$($_.FriendlyName)\" }"
              % vids)
    try:
        rc, out, _ = _powershell(script)
    except Exception:
        return None
    if rc != 0:
        return None
    devs = []
    for line in out.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 4:
            continue
        m = re.search(r"VID_([0-9A-F]{4})", parts[0], re.I)
        vid = int(m.group(1), 16) if m else None
        devs.append({"id": parts[0], "status": parts[1], "class": parts[2],
                     "name": parts[3], "vid": vid,
                     "driver": USB_CHIPS.get(vid, (None, None))[1]})
    return devs


def driver_status():
    """Platform specific USB-serial readiness: (level, message, action)
    level: 'ok' | 'warn' | 'info'; action: None | ('driver', key) | 'linux-group'."""
    system = platform.system()
    if system == "Windows":
        devs = windows_usb_devices()
        if devs is None:
            return "info", "Could not query USB devices - install a driver if no COM port appears.", None
        if not devs:
            return "info", "No USB-serial adapter detected. Plug in the programmer.", None
        bad = [d for d in devs if d["status"].upper() != "OK" and d["driver"]]
        if bad:
            d = bad[0]
            return "warn", "%s found but its driver is missing." % DRIVERS[d["driver"]]["name"], \
                ("driver", d["driver"])
        good = devs[0]
        return "ok", "%s ready (%s)." % (good["name"], good["status"]), None
    if system == "Linux":
        group = _linux_serial_group()
        if group and not _in_group(group):
            return "warn", "Your user is not in the '%s' group - serial ports may be "\
                           "inaccessible." % group, "linux-group"
        if shutil.which("brltty") and not list_ports():
            return "info", "No serial port found. Note: brltty can grab CH340 adapters - "\
                           "remove it (sudo apt remove brltty) if the port never appears.", None
        return "ok", "Serial port access OK.", None
    if system == "Darwin":
        return "ok", "macOS includes drivers for CP210x, CH340 and FTDI adapters.", None
    return "info", "", None


def _linux_serial_group():
    import grp
    for dev in sorted(os.listdir("/dev")):
        if dev.startswith(("ttyUSB", "ttyACM")):
            try:
                return grp.getgrgid(os.stat("/dev/" + dev).st_gid).gr_name
            except Exception:
                pass
    for g in ("dialout", "uucp"):
        try:
            grp.getgrnam(g)
            return g
        except KeyError:
            pass
    return None


def _in_group(group):
    import grp
    if os.geteuid() == 0:
        return True
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return True
    return gid in os.getgroups()


def linux_add_to_group(log):
    group = _linux_serial_group() or "dialout"
    user = os.environ.get("USER") or os.getlogin()
    for elev in (["pkexec"], ["sudo", "-n"]):
        if not shutil.which(elev[0]):
            continue
        r = subprocess.run(elev + ["usermod", "-aG", group, user],
                           capture_output=True, text=True)
        if r.returncode == 0:
            log("Added %s to '%s'. Log out and back in (or reboot) for it to apply."
                % (user, group))
            return True
        log("  %s: %s" % (elev[0], r.stderr.strip()))
    log("Could not change groups automatically. Run:")
    log("  sudo usermod -aG %s %s   (then log out and back in)" % (group, user))
    return False


def find_bundled_driver(key):
    d = DRIVERS[key]
    folder = resource_path("drivers", "windows", d["dir"])
    for base in filter(None, [folder, os.path.join(cache_dir("drivers"), d["dir"])]):
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for f in files:
                low = f.lower()
                if key == "cp210x" and low == "silabser.inf":
                    return os.path.join(root, f)
                if key == "ch340" and low.startswith("ch341ser") and low.endswith(".exe"):
                    return os.path.join(root, f)
    return None


def install_windows_driver(key, log, gh=None):
    d = DRIVERS[key]
    path = find_bundled_driver(key)
    if not path and d["url"]:
        log("Driver not bundled - downloading %s ..." % d["name"])
        folder = cache_dir("drivers", d["dir"])
        dest = os.path.join(folder, "CH341SER.EXE" if key == "ch340" else "driver.zip")
        try:
            (gh or GitHub()).download(d["url"], dest, log)
            if dest.endswith(".zip"):
                with zipfile.ZipFile(dest) as z:
                    z.extractall(folder)
            path = find_bundled_driver(key)
        except Exception as e:
            log("Download failed: %s" % e)
    if not path:
        log("Please install the %s driver from %s" % (d["name"], d["page"]))
        open_url(d["page"])
        return False
    log("Installing %s driver from %s" % (d["name"], path))
    if path.lower().endswith(".inf"):
        # pnputil needs admin rights: elevate through a UAC prompt
        ps = ("$p = Start-Process -FilePath pnputil.exe -ArgumentList "
              "'/add-driver','\"%s\"','/install' -Verb RunAs -Wait -PassThru; "
              "exit $p.ExitCode" % path.replace("'", "''"))
        try:
            rc, out, err = _powershell(ps, timeout=300)
        except Exception as e:
            log("Driver install failed: %s" % e)
            return False
        # pnputil: 0 ok, 259 = already up to date, 3010 = reboot required
        if rc in (0, 259, 3010):
            log("Driver installed%s. Unplug and reconnect the adapter."
                % (" (reboot required)" if rc == 3010 else ""))
            return True
        log("Driver install failed (code %s). %s" % (rc, (err or out).strip()))
        return False
    try:
        os.startfile(path)      # CH341SER.EXE elevates itself; click INSTALL
        log("Driver installer opened - click INSTALL, then reconnect the adapter.")
        return True
    except Exception as e:
        log("Could not start installer: %s" % e)
        return False


def open_url(url):
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception:
        pass


def open_folder(path):
    try:
        if platform.system() == "Windows":
            os.startfile(path)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Device operations
# ---------------------------------------------------------------------------

@dataclass
class DeviceInfo:
    chip: str = ""
    mac: str = ""
    flash_size: str = ""
    flash_mb: float = None
    partitions: list = None        # None = unknown, [] = blank
    layout: str = ""

    def summary(self):
        s = "%s  |  Flash %s" % (self.chip or "ESP32", self.flash_size or "?")
        if self.mac:
            s += "  |  MAC %s" % self.mac
        if self.layout:
            s += "\nCurrent layout: %s" % self.layout
        return s


def _reset_args(manual, after):
    return ["--before", "no-reset" if manual else "default-reset",
            "--after", "no-reset" if manual else after]


def detect_device(port, baud, manual, log, progress=None, cancel=None):
    out = run_esptool(["--chip", "esp32", "--port", port, "--baud", baud]
                      + _reset_args(manual, "no-reset") + ["flash-id"],
                      log, progress, cancel)
    info = DeviceInfo()
    m = re.search(r"Chip (?:is|type:)\s*(.+)", out)
    info.chip = m.group(1).strip() if m else "ESP32"
    m = re.search(r"MAC:\s*([0-9a-fA-F:]{17})", out)
    info.mac = m.group(1) if m else ""
    m = re.search(r"Detected flash size:\s*(\S+)", out)
    info.flash_size = m.group(1) if m else "Unknown"
    info.flash_mb = parse_size_mb(info.flash_size)

    tmp = os.path.join(tempfile.mkdtemp(prefix="oe_"), "partitions.bin")
    try:
        # the stub is still running after 'flash-id', no reset needed
        run_esptool(["--chip", "esp32", "--port", port, "--baud", baud,
                     "--before", "no-reset", "--after", "no-reset" if manual else "hard-reset",
                     "read-flash", hex(ADDR_PARTITIONS), hex(PART_TABLE_LEN), tmp],
                    log, progress, cancel)
        with open(tmp, "rb") as f:
            info.partitions = parse_partition_table(f.read())
        info.layout = identify_layout(info.partitions)
    except (EsptoolError, ValueError, OSError) as e:
        log("Could not read the partition table: %s" % e)
        info.layout = "unreadable"
        if not manual:      # don't leave the board sitting in the bootloader
            try:
                run_esptool(["--chip", "esp32", "--port", port, "--baud", baud,
                             "--before", "no-reset", "--after", "hard-reset", "chip-id"],
                            lambda *_: None, None, cancel)
            except (EsptoolError, OSError):
                pass
    finally:
        shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
    return info


def flash_device(port, baud, manual, variant, fw, full, erase, log, progress=None,
                 cancel=None):
    args = ["--chip", "esp32", "--port", port, "--baud", baud] + \
        _reset_args(manual, "hard-reset") + \
        ["write-flash", "--flash-mode", variant.flash_mode,
         "--flash-freq", variant.flash_freq, "--flash-size", variant.flash_size]
    if erase:
        args.append("--erase-all")
    if full:
        args += [hex(ADDR_BOOTLOADER), fw.bootloader,
                 hex(ADDR_PARTITIONS), fw.partitions,
                 hex(ADDR_OTADATA), blank_otadata()]   # boot app0 (ota_0)
    args += [hex(ADDR_APP), fw.app]
    run_esptool(args, log, progress, cancel)


def plan_summary(variant, fw, full, erase, device):
    lines = ["Target build : %s (%s flash)" % (variant.env, variant.flash_size),
             "Firmware     : %s" % fw.description]
    if full:
        lines += ["Write        : bootloader @0x1000, partitions @0x8000, "
                  "otadata @0xE000, app @0x10000"]
    else:
        lines += ["Write        : app @0x10000 only"]
    if erase:
        lines += ["Erase        : ENTIRE flash (WiFi and all settings are lost)"]
    elif full and device and device.partitions and not same_layout(device.partitions,
                                                                   variant.partitions):
        lines += ["Layout       : %s -> OpenEVSE %s (NVS/WiFi settings kept)"
                  % (device.layout, variant.flash_size)]
    for n in fw.notes:
        lines.append("Note         : " + n)
    return lines


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
BLUE    = "#1565C0"
LBLUE   = "#1E88E5"
BG      = "#F5F5F5"
CONS_BG = "#1e1e1e"
CONS_FG = "#d4d4d4"
GREEN   = "#2E7D32"
ORANGE  = "#E65100"
RED     = "#C62828"
GRAY    = "#616161"


def run_gui():
    import tkinter as tk
    from tkinter import ttk, scrolledtext, messagebox, filedialog

    class FlasherApp(tk.Tk):

        def __init__(self):
            super().__init__()
            self.title("%s %s" % (APP_NAME, APP_VERSION))
            self.geometry("860x900")
            self.minsize(760, 720)
            self.configure(bg=BG)
            self.cfg       = load_config()
            self.gh        = GitHub(token=self.cfg.get("token"))
            self.releases  = []
            self.runs      = []
            self.ports     = []
            self.device    = None
            self.device_port = None
            self.cancel    = threading.Event()
            self.task      = None
            self.ui_q      = queue.Queue()
            self._driver_action = None

            self.port_var    = tk.StringVar()
            self.baud_var    = tk.StringVar(value=self.cfg.get("baud", DEFAULT_BAUD))
            self.manual_var  = tk.BooleanVar(value=self.cfg.get("manual_boot", False))
            self.board_var   = tk.StringVar(value=self.cfg.get("board", "openevse_wifi_v1"))
            self.layout_var  = tk.StringVar(value="Auto (detected flash size)")
            self.full_var    = tk.BooleanVar(value=True)
            self.erase_var   = tk.BooleanVar(value=False)
            self.rel_var     = tk.StringVar()
            self.run_var     = tk.StringVar()
            self.branch_var  = tk.StringVar(value=self.cfg.get("branch", "master"))
            self.token_var   = tk.StringVar(value=self.cfg.get("token", ""))
            self.remember_var = tk.BooleanVar(value=bool(self.cfg.get("token")))
            self.local_app   = tk.StringVar()
            self.local_bl    = tk.StringVar()
            self.local_pt    = tk.StringVar()

            self._style()
            self._build_header()
            self._build_statusbar()       # packed first so it is never squeezed out
            body = ttk.Frame(self)
            body.pack(fill="both", expand=True)
            self._build_device_panel(body)
            self._build_board_panel(body)
            self._build_firmware_panel(body)
            self._build_actions_bar(body)
            self._build_console(body)
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(50, self._pump)
            self.after(100, self._startup)

        # --- style --------------------------------------------------------
        def _style(self):
            s = ttk.Style(self)
            try:
                s.theme_use("clam")
            except Exception:
                pass
            s.configure("TFrame", background=BG)
            s.configure("TLabel", background=BG)
            s.configure("TCheckbutton", background=BG)
            s.configure("TRadiobutton", background=BG)
            s.configure("TLabelframe", background=BG)
            s.configure("TLabelframe.Label", background=BG, font=("Helvetica", 9, "bold"))
            s.configure("Big.TButton", font=("Helvetica", 11, "bold"), padding=(14, 6))
            s.configure("Info.TLabel", foreground=GRAY)
            s.configure("Target.TLabel", foreground=BLUE, font=("Helvetica", 10, "bold"))

        def _build_header(self):
            h = tk.Frame(self, bg=BLUE, pady=10)
            h.pack(fill="x")
            tk.Label(h, text="OpenEVSE", font=("Helvetica", 20, "bold"),
                     bg=BLUE, fg="white").pack()
            tk.Label(h, text="ESP32 WiFi Firmware Flasher", font=("Helvetica", 11),
                     bg=BLUE, fg="#BBDEFB").pack()

        # --- device panel -------------------------------------------------
        def _build_device_panel(self, parent):
            f = ttk.LabelFrame(parent, text="1. USB Serial / ESP32", padding=8)
            f.pack(fill="x", padx=12, pady=(8, 4))
            f.columnconfigure(1, weight=1)

            ttk.Label(f, text="Port:").grid(row=0, column=0, sticky="w")
            self.port_cb = ttk.Combobox(f, textvariable=self.port_var, state="readonly")
            self.port_cb.grid(row=0, column=1, sticky="ew", padx=(6, 6))
            self.port_cb.bind("<<ComboboxSelected>>", lambda *_: self._forget_device())
            ttk.Button(f, text="Refresh", command=self._refresh_ports).grid(row=0, column=2)
            ttk.Label(f, text="Baud:").grid(row=0, column=3, padx=(12, 4))
            ttk.Combobox(f, textvariable=self.baud_var, values=BAUD_RATES, width=8,
                         state="readonly").grid(row=0, column=4)

            row = ttk.Frame(f)
            row.grid(row=1, column=0, columnspan=5, sticky="ew", pady=(6, 0))
            self.driver_lbl = ttk.Label(row, text="Checking USB driver...", style="Info.TLabel")
            self.driver_lbl.pack(side="left")
            self.driver_btn = ttk.Button(row, text="Install Driver", command=self._driver_fix)
            ttk.Checkbutton(row, text="Manual boot mode (no auto-reset)",
                            variable=self.manual_var).pack(side="right")

            row2 = ttk.Frame(f)
            row2.grid(row=2, column=0, columnspan=5, sticky="ew", pady=(6, 0))
            ttk.Button(row2, text="Detect ESP32", command=self._detect).pack(side="left")
            self.dev_lbl = ttk.Label(row2, text="Not detected - click Detect ESP32 to "
                                     "read chip and flash size.", style="Info.TLabel",
                                     justify="left")
            self.dev_lbl.pack(side="left", padx=(10, 0))

        # --- board panel --------------------------------------------------
        def _build_board_panel(self, parent):
            f = ttk.LabelFrame(parent, text="2. Board", padding=8)
            f.pack(fill="x", padx=12, pady=4)
            for i, (key, b) in enumerate(BOARDS.items()):
                sizes = "/".join("%dMB" % mb for mb in sorted(b["variants"]))
                ttk.Radiobutton(f, text="%s  (%s)  - %s" % (b["label"], key, sizes),
                                value=key, variable=self.board_var,
                                command=self._update_target).grid(row=i, column=0,
                                                                  columnspan=3, sticky="w")
            ttk.Label(f, text="Partition layout:").grid(row=2, column=0, sticky="w", pady=(6, 0))
            cb = ttk.Combobox(f, textvariable=self.layout_var, state="readonly", width=28,
                              values=["Auto (detected flash size)", "4MB", "16MB"])
            cb.grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
            cb.bind("<<ComboboxSelected>>", lambda *_: self._update_target())
            self.target_lbl = ttk.Label(f, text="", style="Target.TLabel")
            self.target_lbl.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def _layout_choice(self):
            v = self.layout_var.get()
            return "auto" if v.startswith("Auto") else v.lower()

        def _variant(self, quiet=True):
            try:
                return choose_variant(self.board_var.get(),
                                      self.device.flash_mb if self.device else None,
                                      self._layout_choice())
            except ValueError:
                if quiet:
                    return None
                raise

        def _update_target(self):
            try:
                v = self._variant(quiet=False)
                txt = "Target: %s  ->  %s  (%s layout, bootloader %s)" % (
                    BOARDS[self.board_var.get()]["label"], v.app_asset, v.flash_size,
                    v.bootloader_asset)
                self.target_lbl.config(text=txt, foreground=BLUE)
            except ValueError as e:
                self.target_lbl.config(text=str(e), foreground=ORANGE)
            self._update_source_info()

        # --- firmware panel -----------------------------------------------
        def _build_firmware_panel(self, parent):
            f = ttk.LabelFrame(parent, text="3. Firmware", padding=8)
            f.pack(fill="x", padx=12, pady=4)
            self.nb = ttk.Notebook(f)
            self.nb.pack(fill="x")
            self.nb.bind("<<NotebookTabChanged>>", lambda *_: self._update_source_info())

            # Release
            t = ttk.Frame(self.nb, padding=8)
            self.nb.add(t, text="Release")
            ttk.Label(t, text="Release:").grid(row=0, column=0, sticky="w")
            self.rel_cb = ttk.Combobox(t, textvariable=self.rel_var, state="readonly", width=48)
            self.rel_cb.grid(row=0, column=1, sticky="w", padx=6)
            self.rel_cb.bind("<<ComboboxSelected>>", lambda *_: self._update_source_info())
            ttk.Button(t, text="Refresh", command=self._load_releases).grid(row=0, column=2)
            self.rel_info = ttk.Label(t, text="", style="Info.TLabel")
            self.rel_info.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))

            # Development
            t = ttk.Frame(self.nb, padding=8)
            self.nb.add(t, text="Development")
            ttk.Label(t, text="Latest master build, published to the '%s' "
                              "pre-release on every merge." % DEV_TAG).grid(
                row=0, column=0, sticky="w")
            self.dev_info = ttk.Label(t, text="", style="Info.TLabel", justify="left")
            self.dev_info.grid(row=1, column=0, sticky="w", pady=(6, 0))

            # Actions
            t = ttk.Frame(self.nb, padding=8)
            self.nb.add(t, text="Actions")
            ttk.Label(t, text="Branch:").grid(row=0, column=0, sticky="w")
            br = ttk.Combobox(t, textvariable=self.branch_var, width=24,
                              values=["master", "(all branches / PRs)"])
            br.grid(row=0, column=1, sticky="w", padx=6)
            br.bind("<<ComboboxSelected>>", lambda *_: self._load_runs())
            br.bind("<Return>", lambda *_: self._load_runs())
            ttk.Button(t, text="Load builds", command=self._load_runs).grid(row=0, column=2,
                                                                            sticky="w")
            ttk.Label(t, text="Build:").grid(row=1, column=0, sticky="w", pady=(6, 0))
            self.run_cb = ttk.Combobox(t, textvariable=self.run_var, state="readonly", width=70)
            self.run_cb.grid(row=1, column=1, columnspan=3, sticky="w", padx=6, pady=(6, 0))
            ttk.Label(t, text="GitHub token:").grid(row=2, column=0, sticky="w", pady=(6, 0))
            ttk.Entry(t, textvariable=self.token_var, show="*", width=30).grid(
                row=2, column=1, sticky="w", padx=6, pady=(6, 0))
            ttk.Checkbutton(t, text="Remember", variable=self.remember_var).grid(
                row=2, column=2, sticky="w", pady=(6, 0))
            ttk.Label(t, text="Optional. Without a token artifacts are fetched through "
                              "nightly.link; with one (no scopes needed) directly from GitHub.",
                      style="Info.TLabel", wraplength=700).grid(
                row=3, column=0, columnspan=4, sticky="w", pady=(4, 0))

            # Local
            t = ttk.Frame(self.nb, padding=8)
            self.nb.add(t, text="Local file")
            for r, (lbl, var, req) in enumerate((
                    ("Firmware (.bin / .zip):", self.local_app, True),
                    ("Bootloader (optional):", self.local_bl, False),
                    ("Partitions (optional):", self.local_pt, False))):
                ttk.Label(t, text=lbl).grid(row=r, column=0, sticky="w", pady=2)
                ttk.Entry(t, textvariable=var, width=60).grid(row=r, column=1, sticky="w",
                                                              padx=6, pady=2)
                ttk.Button(t, text="Browse...",
                           command=lambda v=var, z=req: self._browse(v, z)).grid(
                    row=r, column=2, pady=2)
            ttk.Label(t, text="A zip downloaded from GitHub Actions is accepted as-is. "
                              "Missing bootloader/partitions are taken from the newest "
                              "release / built-in layout.",
                      style="Info.TLabel", wraplength=700).grid(
                row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

            # Options
            o = ttk.Frame(f)
            o.pack(fill="x", pady=(8, 0))
            ttk.Radiobutton(o, text="Full install (bootloader + partitions + firmware)",
                            value=True, variable=self.full_var).pack(side="left")
            ttk.Radiobutton(o, text="Firmware only", value=False,
                            variable=self.full_var).pack(side="left", padx=(16, 0))
            ttk.Checkbutton(o, text="Erase all flash first (factory reset)",
                            variable=self.erase_var).pack(side="right")

        def _build_actions_bar(self, parent):
            bar = ttk.Frame(parent)
            bar.pack(fill="x", padx=12, pady=6)
            self.flash_btn = ttk.Button(bar, text="Flash Firmware", style="Big.TButton",
                                        command=self._flash)
            self.flash_btn.pack(side="left")
            self.cancel_btn = ttk.Button(bar, text="Cancel", command=self._cancel,
                                         state="disabled")
            self.cancel_btn.pack(side="left", padx=8)
            self.progress = ttk.Progressbar(bar, mode="determinate", maximum=100)
            self.progress.pack(side="left", fill="x", expand=True, padx=(12, 6))
            self.pct_lbl = ttk.Label(bar, text="", width=24)
            self.pct_lbl.pack(side="left")

        def _build_console(self, parent):
            cf = ttk.LabelFrame(parent, text="Output", padding=4)
            cf.pack(fill="both", expand=True, padx=12, pady=(0, 4))
            self.console = scrolledtext.ScrolledText(
                cf, font=("Courier New", 9), height=12, bg=CONS_BG, fg=CONS_FG,
                insertbackground=CONS_FG, relief="flat", state="disabled")
            self.console.pack(fill="both", expand=True)
            self.console.tag_config("err", foreground="#ff7b72")
            self.console.tag_config("ok", foreground="#7ee787")
            self.console.tag_config("head", foreground="#79c0ff")

        def _build_statusbar(self):
            bar = tk.Frame(self, bg="#E0E0E0", pady=3)
            bar.pack(fill="x", side="bottom")
            ttk.Button(bar, text="Clear log", command=self._clear_log).pack(side="left", padx=6)
            ttk.Button(bar, text="Open download folder",
                       command=lambda: open_folder(user_dir("cache"))).pack(side="left")
            self.status_lbl = tk.Label(bar, text="Starting...", bg="#E0E0E0",
                                       font=("Helvetica", 9), fg="gray")
            self.status_lbl.pack(side="right", padx=8)

        # --- UI queue -----------------------------------------------------
        def _pump(self):
            latest_progress = None
            try:
                while True:
                    kind, a, b = self.ui_q.get_nowait()
                    if kind == "log":
                        self._append(a, b)
                    elif kind == "progress":
                        latest_progress = (a, b)
                    elif kind == "call":
                        a(*b)
            except queue.Empty:
                pass
            if latest_progress:
                pct, stage = latest_progress
                self.progress["value"] = pct
                self.pct_lbl.config(text=("%s %3.0f%%" % (stage[:16], pct)).strip())
            self.after(50, self._pump)

        def log(self, text, tag=None):
            if tag is None:
                low = text.lower()
                if "error" in low or "failed" in low:
                    tag = "err"
                elif text.startswith("---") or text.startswith("==="):
                    tag = "head"
            self.ui_q.put(("log", text, tag))

        def set_progress(self, pct, stage=""):
            self.ui_q.put(("progress", pct, stage))

        def ui(self, fn, *args):
            self.ui_q.put(("call", fn, args))

        def _append(self, text, tag):
            self.console.config(state="normal")
            self.console.insert("end", text + "\n", tag or ())
            self.console.see("end")
            self.console.config(state="disabled")

        def _clear_log(self):
            self.console.config(state="normal")
            self.console.delete("1.0", "end")
            self.console.config(state="disabled")

        def status(self, msg):
            self.ui(lambda: self.status_lbl.config(text=msg))

        # --- tasks --------------------------------------------------------
        def _run_task(self, fn, name="Working", on_success=None):
            if self.task and self.task.is_alive():
                messagebox.showwarning("Busy", "A task is already running - please wait.")
                return False
            self.cancel.clear()
            self.flash_btn.config(state="disabled")
            self.cancel_btn.config(state="normal")
            self.status(name + "...")
            self.set_progress(0, "")

            def runner():
                ok = False
                try:
                    fn()
                    ok = True
                except CancelledError:
                    self.log("Cancelled.", "err")
                except Exception as e:
                    self.log("ERROR: %s" % e, "err")
                finally:
                    self.ui(self._task_done)
                    if ok and on_success:
                        self.ui(on_success)
            self.task = threading.Thread(target=runner, daemon=True)
            self.task.start()
            return True

        def _task_done(self):
            self.flash_btn.config(state="normal")
            self.cancel_btn.config(state="disabled")
            self.status_lbl.config(text="Ready")

        def _cancel(self):
            self.cancel.set()
            self.log("Cancelling...")

        def _bg(self, fn):
            threading.Thread(target=fn, daemon=True).start()

        # --- startup ------------------------------------------------------
        def _startup(self):
            self.log("%s %s" % (APP_NAME, APP_VERSION), "head")
            self.log("Settings: " + (CONFIG_PATH or ""))
            self._check_esptool()
            self._refresh_ports()
            self._update_target()
            self._bg(self._check_driver)
            self._load_releases()

        def _check_esptool(self):
            try:
                import esptool
                self.log("esptool %s (%s)" % (getattr(esptool, "__version__", "?"),
                                              "bundled" if FROZEN or
                                              esptool.__file__.startswith(VENDOR)
                                              else os.path.dirname(esptool.__file__)))
            except ImportError:
                self.log("esptool is not installed. Run 'run.sh' / 'run.bat', or "
                         "pip install -r requirements.txt", "err")
            try:
                import serial  # noqa: F401
            except ImportError:
                self.log("pyserial is not installed.", "err")

        # --- ports / drivers ----------------------------------------------
        def _refresh_ports(self):
            self.ports = list_ports()
            labels = [p.label() for p in self.ports]
            self.port_cb["values"] = labels
            last = self.cfg.get("port")
            pick = next((p.label() for p in self.ports if p.device == last), None) or \
                next((p.label() for p in self.ports if p.known), None) or \
                (labels[0] if labels else "")
            self.port_var.set(pick)
            if not labels:
                self.log("No serial ports found - plug in the USB programmer, "
                         "then click Refresh.")
            self._bg(self._check_driver)

        def _port(self):
            label = self.port_var.get()
            p = next((p for p in self.ports if p.label() == label), None)
            return p.device if p else None

        def _check_driver(self):
            level, msg, action = driver_status()
            self.ui(self._apply_driver, level, msg, action)

        def _apply_driver(self, level, msg, action):
            colour = {"ok": GREEN, "warn": ORANGE}.get(level, GRAY)
            self.driver_lbl.config(text=msg, foreground=colour)
            self._driver_action = action
            if action:
                self.driver_btn.config(text="Install Driver" if action != "linux-group"
                                       else "Fix Permissions")
                self.driver_btn.pack(side="left", padx=(10, 0))
            elif platform.system() == "Windows":
                self.driver_btn.config(text="Install Driver...")
                self.driver_btn.pack(side="left", padx=(10, 0))
            else:
                self.driver_btn.pack_forget()

        def _driver_fix(self):
            action = self._driver_action
            if action == "linux-group":
                self._run_task(lambda: linux_add_to_group(self.log), "Fixing permissions")
                return
            if action and action[0] == "driver":
                key = action[1]
            else:
                key = self._ask_driver()
                if not key:
                    return
            if not messagebox.askokcancel(
                    "Install USB driver",
                    "Install the %s driver?\n\nWindows will ask for administrator "
                    "permission. Unplug and reconnect the adapter afterwards."
                    % DRIVERS[key]["name"]):
                return

            def task():
                install_windows_driver(key, self.log, self.gh)
                time.sleep(2)
                self._check_driver()
                self.ui(self._refresh_ports)
            self._run_task(task, "Installing driver")

        def _ask_driver(self):
            win = tk.Toplevel(self)
            win.title("Install USB driver")
            win.transient(self)
            win.grab_set()
            choice = tk.StringVar(value="cp210x")
            ttk.Label(win, text="Which USB-serial adapter do you have?",
                      padding=10).pack(anchor="w")
            for key, d in DRIVERS.items():
                ttk.Radiobutton(win, text=d["name"], value=key,
                                variable=choice).pack(anchor="w", padx=20)
            result = {}
            bf = ttk.Frame(win, padding=10)
            bf.pack(fill="x")
            ttk.Button(bf, text="Install", command=lambda: (result.update(k=choice.get()),
                                                            win.destroy())).pack(side="right")
            ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="right", padx=6)
            self.wait_window(win)
            return result.get("k")

        # --- detect -------------------------------------------------------
        def _forget_device(self):
            if self.device and self.device_port != self._port():
                self.device = None
                self.dev_lbl.config(text="Not detected - click Detect ESP32.",
                                    foreground=GRAY)
                self._update_target()

        def _detect(self, then=None):
            port = self._port()
            if not port:
                messagebox.showerror("No port", "Select the serial port of the USB programmer.")
                return
            if self.manual_var.get():
                messagebox.showinfo("Manual boot mode",
                                    "Hold the BOOT / WiFi button (GPIO0), power-cycle or "
                                    "reset the board, then release the button.\n\n"
                                    "Click OK when the board is in bootloader mode.")

            def task():
                self.log("--- Detecting ESP32 on %s ---" % port)
                info = detect_device(port, self.baud_var.get(), self.manual_var.get(),
                                     self.log, self.set_progress, self.cancel)
                self.device, self.device_port = info, port
                self.log("Detected: %s" % info.summary().replace("\n", "  |  "), "ok")
                self.set_progress(100, "Detected")
                self.ui(self._apply_device)
            self._run_task(task, "Detecting", on_success=then)

        def _apply_device(self):
            d = self.device
            colour = GREEN if d.flash_mb else ORANGE
            self.dev_lbl.config(text=d.summary(), foreground=colour)
            self._update_target()

        # --- releases / runs ----------------------------------------------
        def _apply_token(self):
            tok = self.token_var.get().strip()
            self.gh.token = tok or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")

        def _load_releases(self):
            self._apply_token()

            def work():
                self.status("Fetching releases...")
                try:
                    rels = self.gh.releases()
                    self.releases = rels
                    stable = [r for r in rels if is_stable_release(r)]
                    self.log("Found %d release(s) of %s" % (len(stable), FW_REPO))
                    self.ui(self._apply_releases)
                except Exception as e:
                    self.log("Could not fetch releases: %s" % e, "err")
                self.status("Ready")
            self._bg(work)

        def _apply_releases(self):
            labels = [release_label(r) for r in self.releases if is_stable_release(r)]
            self.rel_cb["values"] = labels
            if labels and not self.rel_var.get():
                self.rel_var.set(labels[0])
            self._update_source_info()

        def _selected_release(self):
            label = self.rel_var.get()
            return next((r for r in self.releases
                         if is_stable_release(r) and release_label(r) == label), None)

        def _dev_release(self):
            return next((r for r in self.releases if r.get("tag_name") == DEV_TAG), None)

        def _load_runs(self):
            self._apply_token()
            branch = self.branch_var.get().strip()
            if branch.startswith("("):
                branch = ""

            def work():
                self.status("Fetching Actions builds...")
                try:
                    runs = self.gh.workflow_runs(branch or None)
                    self.runs = runs
                    self.log("Found %d successful build(s)%s" % (
                        len(runs), " on " + branch if branch else ""))
                    self.ui(self._apply_runs)
                except Exception as e:
                    self.log("Could not fetch Actions builds: %s" % e, "err")
                self.status("Ready")
            self._bg(work)

        def _apply_runs(self):
            labels = [run_label(r) for r in self.runs]
            self.run_cb["values"] = labels
            self.run_var.set(labels[0] if labels else "")

        def _selected_run(self):
            label = self.run_var.get()
            return next((r for r in self.runs if run_label(r) == label), None)

        def _source(self):
            return ["release", "dev", "actions", "local"][self.nb.index(self.nb.select())]

        def _update_source_info(self):
            if not hasattr(self, "rel_info"):
                return
            v = self._variant()
            rel = self._selected_release()
            if rel and v:
                have = [n for n in (v.app_asset, v.bootloader_asset, v.partitions_asset)
                        if _asset(rel, n)]
                missing = [n for n in (v.app_asset, v.bootloader_asset, v.partitions_asset)
                           if n not in have]
                txt = "Contains: " + (", ".join(have) or "nothing for this board")
                if missing:
                    txt += "\nMissing: " + ", ".join(missing)
                self.rel_info.config(text=txt, foreground=GRAY if v.app_asset in have
                                     else ORANGE)
            elif rel:
                self.rel_info.config(text="Detect the ESP32 or choose a layout to see "
                                          "the matching files.", foreground=GRAY)
            dev = self._dev_release()
            if dev:
                txt = "Published %s  -  commit %s" % (
                    (dev.get("published_at") or "")[:16].replace("T", " "),
                    (dev.get("target_commitish") or "")[:12])
                if v:
                    txt += "\nFile: %s%s" % (v.app_asset, "" if release_has_variant(dev, v)
                                             else "  (NOT in this build)")
                self.dev_info.config(text=txt)
            elif self.releases:
                self.dev_info.config(text="No Development Build published.")

        def _browse(self, var, firmware):
            types = [("Firmware", "*.bin *.zip"), ("All files", "*.*")] if firmware else \
                [("Binary", "*.bin"), ("All files", "*.*")]
            path = filedialog.askopenfilename(title="Select file", filetypes=types)
            if path:
                var.set(path)
                if firmware:
                    self.nb.select(3)

        # --- flash --------------------------------------------------------
        def _flash(self):
            if not self._port():
                messagebox.showerror("No port", "Select the serial port of the USB programmer.")
                return
            if self.task and self.task.is_alive():
                messagebox.showwarning("Busy", "A task is already running - please wait.")
                return
            if not self.device or self.device_port != self._port():
                # flash size is needed to pick the build: detect first
                self.log("Reading flash size before flashing...")
                self._detect(then=self._flash)
                return
            try:
                variant = self._variant(quiet=False)
            except ValueError as e:
                messagebox.showerror("Board / flash size", str(e))
                return
            source = self._source()
            full, erase = self.full_var.get(), self.erase_var.get()
            port, baud, manual = self._port(), self.baud_var.get(), self.manual_var.get()
            run, rel = self._selected_run(), self._selected_release()
            dev = self._dev_release()
            if source == "release" and not rel:
                messagebox.showerror("Firmware", "Select a release (click Refresh).")
                return
            if source == "dev" and not dev:
                messagebox.showerror("Firmware", "No Development Build found (refresh releases).")
                return
            if source == "actions" and not run:
                messagebox.showerror("Firmware", "Load and select an Actions build.")
                return
            if source == "local" and not self.local_app.get():
                messagebox.showerror("Firmware", "Choose a firmware file.")
                return
            device = self.device
            if not full and not erase and not same_layout(device.partitions, variant.partitions):
                if not messagebox.askyesno(
                        "Partition layout mismatch",
                        "The ESP32 currently has layout:\n  %s\n\nbut %s needs the standard "
                        "OpenEVSE %s layout. 'Firmware only' will probably not boot.\n\n"
                        "Switch to Full install?" % (device.layout, variant.env,
                                                     variant.flash_size),
                        icon="warning"):
                    return
                full = True
                self.full_var.set(True)
            if erase and not messagebox.askyesno(
                    "Erase all flash",
                    "Erasing all flash removes WiFi settings and all configuration.\n\nContinue?",
                    icon="warning"):
                return
            self._save_settings()
            local = (self.local_app.get(), self.local_bl.get(), self.local_pt.get())

            def task():
                need_boot = full or erase
                self.log("=== Preparing firmware for %s (%s) ===" % (variant.env,
                                                                    variant.flash_size))
                if source == "release":
                    fw = resolve_release(self.gh, rel, variant, need_boot, self.releases,
                                         self.log, self.set_progress, self.cancel)
                elif source == "dev":
                    fw = resolve_release(self.gh, dev, variant, need_boot, self.releases,
                                         self.log, self.set_progress, self.cancel)
                elif source == "actions":
                    fw = resolve_actions(self.gh, run, variant, need_boot, self.releases,
                                         self.log, self.set_progress, self.cancel)
                else:
                    fw = resolve_local(*local, variant, need_boot, self.gh, self.releases,
                                       self.log, self.set_progress, self.cancel)
                warnings = validate_firmware(fw, variant, device.flash_mb, need_boot,
                                             device.partitions, self.log)
                summary = plan_summary(variant, fw, need_boot, erase, device)
                for line in summary:
                    self.log("  " + line)
                answer = self._confirm(summary, warnings)
                if not answer:
                    self.log("Flash cancelled.")
                    return
                if manual:
                    self._wait_dialog("Manual boot mode",
                                      "Put the ESP32 in bootloader mode again:\nhold BOOT / "
                                      "WiFi, power-cycle or reset, release.\n\nClick OK to "
                                      "start flashing.")
                self.log("=== Flashing %s ===" % port)
                flash_device(port, baud, manual, variant, fw, need_boot, erase, self.log,
                             self.set_progress, self.cancel)
                self.set_progress(100, "Done")
                self.log("Done! %s" % ("Power-cycle the board to start the new firmware."
                                       if manual else "The board is restarting."), "ok")
                self.device = None   # layout may have changed
                self.ui(lambda: self.dev_lbl.config(
                    text="Flashed - click Detect ESP32 to re-read.", foreground=GREEN))
            self._run_task(task, "Flashing")

        def _confirm(self, summary, warnings):
            text = "\n".join(summary)
            if warnings:
                text += "\n\nWARNING:\n- " + "\n- ".join(warnings)
            text += "\n\nStart flashing?"
            return self._ask_main(lambda: messagebox.askyesno(
                "Confirm flash", text, icon="warning" if warnings else "question"))

        def _wait_dialog(self, title, msg):
            self._ask_main(lambda: messagebox.showinfo(title, msg))

        def _ask_main(self, fn):
            """Run a dialog on the Tk thread from a worker, wait for its result."""
            done, box = threading.Event(), {}

            def call():
                box["r"] = fn()
                done.set()
            self.ui(call)
            while not done.wait(0.1):
                if self.cancel.is_set():
                    raise CancelledError()
            return box.get("r")

        def _save_settings(self):
            self.cfg.update(port=self._port(), baud=self.baud_var.get(),
                            board=self.board_var.get(), manual_boot=self.manual_var.get(),
                            branch=self.branch_var.get())
            if self.remember_var.get() and self.token_var.get().strip():
                self.cfg["token"] = self.token_var.get().strip()
            else:
                self.cfg.pop("token", None)
            save_config(self.cfg)

        def _on_close(self):
            self.cancel.set()
            self._save_settings()
            self.destroy()

    FlasherApp().mainloop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cli_log(text, tag=None):
    print(text, flush=True)


class CliProgress:
    def __init__(self):
        self.last = None

    def __call__(self, pct, stage=""):
        key = (int(pct) // 5, stage)
        if key != self.last:
            self.last = key
            sys.stdout.write("\r  %-20s %5.1f%%" % (stage[:20], pct))
            if pct >= 100:
                sys.stdout.write("\n")
            sys.stdout.flush()


def cli_main(argv):
    p = argparse.ArgumentParser(
        prog="openevse_esp32_flasher",
        description="%s %s - run without arguments for the GUI." % (APP_NAME, APP_VERSION))
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("ports", help="list serial ports")

    def conn(sp):
        sp.add_argument("-p", "--port", required=True)
        sp.add_argument("-b", "--baud", default=DEFAULT_BAUD)
        sp.add_argument("--manual-boot", action="store_true",
                        help="board has no auto-reset; enter bootloader with BOOT button")

    sp = sub.add_parser("detect", help="read chip, flash size and partition layout")
    conn(sp)

    sp = sub.add_parser("list", help="list available firmware")
    sp.add_argument("source", choices=["releases", "actions"])
    sp.add_argument("--branch", default="master", help="Actions branch ('' for all)")
    sp.add_argument("--token")

    sp = sub.add_parser("flash", help="flash firmware")
    conn(sp)
    sp.add_argument("--board", required=True,
                    choices=sorted(set(BOARDS) | set(BOARD_ALIASES)))
    sp.add_argument("--layout", default="auto", choices=["auto", "4mb", "16mb"])
    src = sp.add_mutually_exclusive_group()
    src.add_argument("--release", metavar="TAG", help="release tag (default: newest)")
    src.add_argument("--dev", action="store_true", help="Development Build ('latest')")
    src.add_argument("--actions", metavar="RUN_ID", help="GitHub Actions run id")
    src.add_argument("--file", metavar="APP", help="local firmware .bin or artifact .zip")
    sp.add_argument("--bootloader", help="local bootloader.bin (with --file)")
    sp.add_argument("--partitions", help="local partitions.bin (with --file)")
    sp.add_argument("--app-only", action="store_true", help="write firmware only")
    sp.add_argument("--erase", action="store_true", help="erase entire flash first")
    sp.add_argument("--token")
    sp.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    sp = sub.add_parser("esptool", help="run the bundled esptool with raw arguments")
    sp.add_argument("args", nargs=argparse.REMAINDER)

    sub.add_parser("drivers", help="show USB-serial driver status")
    sp = sub.add_parser("install-driver", help="install a bundled Windows driver")
    sp.add_argument("driver", choices=list(DRIVERS))

    if argv[:1] == ["esptool"]:
        return esptool_worker(argv[1:])     # raw pass-through, options included
    a = p.parse_args(argv)
    if not a.cmd:
        p.print_help()
        return 0
    load_config()

    try:
        if a.cmd == "ports":
            for port in list_ports():
                print(port.label())
            return 0
        if a.cmd == "drivers":
            level, msg, action = driver_status()
            print("[%s] %s" % (level, msg))
            return 0
        if a.cmd == "install-driver":
            return 0 if install_windows_driver(a.driver, cli_log) else 1
        if a.cmd == "detect":
            info = detect_device(a.port, a.baud, a.manual_boot, cli_log, CliProgress())
            print(info.summary())
            for part in info.partitions or []:
                print("  %-10s type %02x/%02x  0x%06X  0x%06X" % (
                    part.name, part.type, part.subtype, part.offset, part.size))
            return 0
        gh = GitHub(token=a.token)
        if a.cmd == "list":
            if a.source == "releases":
                for r in gh.releases():
                    print(release_label(r))
            else:
                for r in gh.workflow_runs(a.branch or None):
                    print("%-12s %s" % (r["id"], run_label(r)))
            return 0
        if a.cmd == "flash":
            return cli_flash(a, gh)
    except CancelledError:
        print("Cancelled.")
        return 130
    except (GitHubError, EsptoolError, ValueError) as e:
        print("ERROR: %s" % e)
        return 1
    return 0


def cli_flash(a, gh):
    prog = CliProgress()
    info = detect_device(a.port, a.baud, a.manual_boot, cli_log, prog)
    print(info.summary())
    variant = choose_variant(a.board, info.flash_mb, a.layout)
    full = not a.app_only or a.erase
    if not full and not same_layout(info.partitions, variant.partitions):
        raise ValueError("Device layout (%s) does not match %s - drop --app-only."
                         % (info.layout, variant.env))
    releases = []
    if not a.file or full:
        try:
            releases = gh.releases()
        except GitHubError as e:
            if not a.file:
                raise
            print("Warning: %s" % e)
    if a.file:
        fw = resolve_local(a.file, a.bootloader, a.partitions, variant, full, gh, releases,
                           cli_log, prog)
    elif a.actions:
        runs = gh.workflow_runs(None, 100)
        run = next((r for r in runs if str(r["id"]) == str(a.actions)), None) or \
            gh.api("/repos/%s/actions/runs/%s" % (gh.repo, a.actions))
        fw = resolve_actions(gh, run, variant, full, releases, cli_log, prog)
    else:
        if a.dev:
            rel = next((r for r in releases if r["tag_name"] == DEV_TAG), None)
        elif a.release:
            rel = next((r for r in releases if r["tag_name"] == a.release), None)
        else:
            rel = next((r for r in releases if is_stable_release(r)
                        and release_has_variant(r, variant)), None)
        if not rel:
            raise ValueError("Release not found.")
        fw = resolve_release(gh, rel, variant, full, releases, cli_log, prog)
    warnings = validate_firmware(fw, variant, info.flash_mb, full, info.partitions, cli_log)
    for line in plan_summary(variant, fw, full, a.erase, info):
        print("  " + line)
    for w in warnings:
        print("WARNING: " + w)
    if not a.yes and input("Flash now? [y/N] ").strip().lower() not in ("y", "yes"):
        return 1
    if a.manual_boot:
        input("Put the ESP32 in bootloader mode again, then press Enter...")
    flash_device(a.port, a.baud, a.manual_boot, variant, fw, full, a.erase, cli_log, prog)
    print("Done!")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--esptool-worker"]:
        sys.exit(esptool_worker(argv[1:]))
    if argv:
        _attach_console()
        sys.exit(cli_main(argv))
    try:
        run_gui()
    except ImportError as e:
        print("GUI unavailable (%s). Use the command line - see --help." % e)
        sys.exit(1)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
