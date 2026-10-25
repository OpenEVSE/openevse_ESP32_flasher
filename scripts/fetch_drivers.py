#!/usr/bin/env python3
"""
Download the Windows USB-serial drivers bundled with the flasher:

  drivers/windows/CP210x/    Silicon Labs CP210x Universal Windows Driver (silabser.inf)
  drivers/windows/CH341SER/  WCH CH340/CH341 installer (CH341SER.EXE)

Run before building:  python scripts/fetch_drivers.py
Failures are reported but are not fatal: the app downloads a missing driver
on demand at install time.
"""

import os, sys, io, zipfile, urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "drivers", "windows")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenEVSE-ESP32-Flasher-build"

CP210X_URLS = list(filter(None, [
    os.environ.get("CP210X_DRIVER_URL"),        # optional mirror
    "https://www.silabs.com/documents/public/software/CP210x_Universal_Windows_Driver.zip",
]))
CH340_URLS = [
    "https://www.wch-ic.com/download/file?id=65",
    "https://www.wch.cn/download/file?id=65",
]


def _ctx():
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


def fetch(urls):
    last = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60, context=_ctx()) as r:
                data = r.read()
            print("  downloaded %s (%d bytes)" % (url, len(data)))
            return data
        except Exception as e:
            last = e
            print("  %s: %s" % (url, e))
    raise RuntimeError(last)


def cp210x():
    dest = os.path.join(ROOT, "CP210x")
    if os.path.isfile(os.path.join(dest, "silabser.inf")):
        print("CP210x driver already present")
        return True
    print("Fetching CP210x driver...")
    data = fetch(CP210X_URLS)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            name = os.path.normpath(info.filename)
            if info.is_dir() or name.startswith("..") or os.path.isabs(name):
                continue
            out = os.path.join(dest, name)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(info) as src, open(out, "wb") as dst:
                dst.write(src.read())
    if not any(f.lower() == "silabser.inf" for _, _, fs in os.walk(dest) for f in fs):
        raise RuntimeError("silabser.inf not found in the CP210x download")
    return True


def ch340():
    dest = os.path.join(ROOT, "CH341SER")
    exe = os.path.join(dest, "CH341SER.EXE")
    if os.path.isfile(exe):
        print("CH340 driver already present")
        return True
    print("Fetching CH340 driver...")
    data = fetch(CH340_URLS)
    if data[:2] != b"MZ":
        raise RuntimeError("CH340 download is not a Windows executable")
    os.makedirs(dest, exist_ok=True)
    with open(exe, "wb") as f:
        f.write(data)
    return True


def main():
    ok = True
    for fn in (cp210x, ch340):
        try:
            fn()
        except Exception as e:
            ok = False
            print("WARNING: %s failed: %s" % (fn.__name__, e))
    if not ok:
        print("Some drivers were not bundled; the app will download them on demand.")
    return 0 if ok or "--allow-missing" in sys.argv else 1


if __name__ == "__main__":
    sys.exit(main())
