"""Unit tests for the non-GUI parts of the flasher.  Run:  python -m unittest"""

import os, sys, shutil, struct, tempfile, unittest, zipfile, pathlib

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")
sys.path.insert(0, os.path.dirname(HERE))

# keep downloads / generated files out of the real user cache
_CACHE = tempfile.mkdtemp(prefix="oe_test_cache_")
os.environ["XDG_CACHE_HOME"] = _CACHE
os.environ["XDG_CONFIG_HOME"] = _CACHE
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GH_TOKEN", None)

import openevse_esp32_flasher as fl  # noqa: E402


def make_app(size=4096, idf="5.5.5", project="openevse_esp32_firmware", version="v6.0.0"):
    """Minimal ESP32 app image: image header, segment header, esp_app_desc_t."""
    hdr = bytes([fl.ESP_IMAGE_MAGIC, 1, 2, 0x20]) + b"\x00" * 20
    seg = struct.pack("<LL", 0x3F400020, 256)
    desc = struct.pack("<LL8x", fl.APP_DESC_MAGIC, 0)
    desc += version.encode().ljust(32, b"\0") + project.encode().ljust(32, b"\0")
    desc += b"12:00:00".ljust(16, b"\0") + b"Oct 24 2026".ljust(16, b"\0")
    desc += idf.encode().ljust(32, b"\0")
    data = hdr + seg + desc
    return data + b"\x00" * (size - len(data))


def make_bootloader():
    return bytes([fl.ESP_IMAGE_MAGIC, 3, 2, 0x20]) + b"\x00" * 1020


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return path


def read(path):
    with open(path, "rb") as f:
        return f.read()


def nolog(*_a, **_k):
    pass


class PartitionTests(unittest.TestCase):

    def test_generated_tables_match_release_files(self):
        for key, name in (("wifi_v1_4mb", "partitions_4mb.bin"),
                          ("wifi_v1_16mb", "partitions_v1_16mb.bin"),
                          ("tft_v1_16mb", "partitions_16mb.bin")):
            gen = fl.build_partition_table(fl.VARIANTS[key].partitions)
            self.assertEqual(gen, read(os.path.join(FIX, name)), name)

    def test_identify_layout(self):
        t4 = fl.parse_partition_table(read(os.path.join(FIX, "partitions_4mb.bin")))
        t16 = fl.parse_partition_table(read(os.path.join(FIX, "partitions_16mb.bin")))
        self.assertEqual(fl.identify_layout(t4), "OpenEVSE 4MB (min_spiffs)")
        self.assertEqual(fl.identify_layout(t16), "OpenEVSE 16MB")
        self.assertEqual(fl.table_flash_size(t4), 4 * fl.MB)
        self.assertEqual(fl.table_flash_size(t16), 16 * fl.MB)
        self.assertEqual(fl.identify_layout([]), "blank / erased")

    def test_blank_and_invalid(self):
        self.assertEqual(fl.parse_partition_table(b"\xFF" * 0xC00), [])
        with self.assertRaises(ValueError):
            fl.parse_partition_table(b"\x12\x34" + b"\x00" * 30)
        bad = bytearray(read(os.path.join(FIX, "partitions_4mb.bin")))
        bad[10] ^= 0xFF
        with self.assertRaises(ValueError):
            fl.parse_partition_table(bytes(bad))


class VariantTests(unittest.TestCase):

    def test_auto_selection(self):
        cv = fl.choose_variant
        self.assertEqual(cv("openevse_wifi_v1", 4).key, "wifi_v1_4mb")
        self.assertEqual(cv("openevse_wifi_v1", 8).key, "wifi_v1_4mb")
        self.assertEqual(cv("openevse_wifi_v1", 16).key, "wifi_v1_16mb")
        self.assertEqual(cv("openevse_wifi_v1", 32).key, "wifi_v1_16mb")
        self.assertEqual(cv("openevse_tft_wifi_v1", 16).key, "tft_v1_16mb")
        self.assertEqual(cv("openevse_wifi_tft_v1", 16).key, "tft_v1_16mb")
        self.assertEqual(cv("tft", 16).env, "openevse_wifi_tft_v1")

    def test_errors(self):
        with self.assertRaises(ValueError):
            fl.choose_variant("openevse_tft_wifi_v1", 4)
        with self.assertRaises(ValueError):
            fl.choose_variant("openevse_wifi_v1", None)
        with self.assertRaises(ValueError):
            fl.choose_variant("openevse_wifi_v1", 4, "16mb")
        with self.assertRaises(ValueError):
            fl.choose_variant("openevse_tft_wifi_v1", 16, "4mb")

    def test_manual_layout(self):
        self.assertEqual(fl.choose_variant("openevse_wifi_v1", 16, "4mb").key, "wifi_v1_4mb")
        self.assertEqual(fl.choose_variant("openevse_wifi_v1", None, "16mb").key,
                         "wifi_v1_16mb")

    def test_parse_size(self):
        self.assertEqual(fl.parse_size_mb("16MB"), 16)
        self.assertEqual(fl.parse_size_mb("4MB"), 4)
        self.assertEqual(fl.parse_size_mb("512KB"), 0.5)
        self.assertIsNone(fl.parse_size_mb("Unknown"))


class ImageTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_app_info(self):
        info = fl.app_info(make_app())
        self.assertEqual(info["project"], "openevse_esp32_firmware")
        self.assertEqual(info["version"], "v6.0.0")
        self.assertEqual(fl.idf_major(info), 5)
        self.assertEqual(fl.idf_major(fl.app_info(make_app(idf="v4.4.7-dirty"))), 4)
        self.assertIsNone(fl.app_info(b"\x00" * 512))

    def _fw(self, app, full=True, parts="partitions_16mb.bin"):
        fw = fl.FirmwareSet("test", write(os.path.join(self.tmp, "app.bin"), app))
        if full:
            fw.bootloader = write(os.path.join(self.tmp, "bl.bin"), make_bootloader())
            fw.partitions = os.path.join(FIX, parts)
        return fw

    def test_validate_ok(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        self.assertEqual(fl.validate_firmware(self._fw(make_app()), v, 16, True, None, nolog), [])

    def test_validate_wrong_idf_warns(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        w = fl.validate_firmware(self._fw(make_app(idf="v4.4.7")), v, 16, True, None, nolog)
        self.assertTrue(any("ESP-IDF" in x for x in w))

    def test_validate_too_big(self):
        v = fl.VARIANTS["wifi_v1_4mb"]
        with self.assertRaises(ValueError):
            fl.validate_firmware(self._fw(make_app(0x1E0000 + 16, idf="v4.4"), parts=
                                          "partitions_4mb.bin"), v, 4, True, None, nolog)

    def test_validate_layout_larger_than_flash(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        with self.assertRaises(ValueError):
            fl.validate_firmware(self._fw(make_app()), v, 4, True, None, nolog)

    def test_validate_not_an_image(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        with self.assertRaises(ValueError):
            fl.validate_firmware(self._fw(b"\x00" * 4096), v, 16, True, None, nolog)

    def test_app_only_uses_device_table(self):
        v = fl.VARIANTS["wifi_v1_4mb"]
        dev = fl.parse_partition_table(read(os.path.join(FIX, "partitions_4mb.bin")))
        fw = self._fw(make_app(idf="v4.4.7"), full=False)
        self.assertEqual(fl.validate_firmware(fw, v, 4, False, dev, nolog), [])


class SourceTests(unittest.TestCase):
    """Release / Actions / local resolution against file:// URLs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.files = {}
        for name, data in (("openevse_wifi_v1_16mb.bin", make_app()),
                           ("bootloader_v1_16mb.bin", make_bootloader()),
                           ("partitions_v1_16mb.bin",
                            read(os.path.join(FIX, "partitions_v1_16mb.bin"))),
                           ("openevse_wifi_v1.bin", make_app(idf="v4.4.7"))):
            self.files[name] = write(os.path.join(self.tmp, name), data)
        self._id = 1000

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def asset(self, name):
        self._id += 1
        return {"id": self._id, "name": name,
                "browser_download_url": pathlib.Path(self.files[name]).as_uri()}

    def rel(self, tag, names, pre=True):
        return {"tag_name": tag, "prerelease": pre, "published_at": "2026-10-24T00:00:00Z",
                "assets": [self.asset(n) for n in names]}

    def test_release_full(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        rel = self.rel("v6.0.0", ["openevse_wifi_v1_16mb.bin", "bootloader_v1_16mb.bin",
                                  "partitions_v1_16mb.bin"])
        fw = fl.resolve_release(fl.GitHub(), rel, v, True, [rel], nolog)
        self.assertEqual(read(fw.app), read(self.files["openevse_wifi_v1_16mb.bin"]))
        self.assertEqual(read(fw.bootloader), make_bootloader())
        self.assertEqual(fw.notes, [])

    def test_release_missing_boot_files_falls_back(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        old = self.rel("v5.1.0", ["openevse_wifi_v1_16mb.bin"])
        new = self.rel("latest", ["bootloader_v1_16mb.bin"])
        fw = fl.resolve_release(fl.GitHub(), old, v, True, [new, old], nolog)
        self.assertEqual(read(fw.partitions),
                         read(os.path.join(FIX, "partitions_v1_16mb.bin")))
        self.assertEqual(read(fw.bootloader), make_bootloader())
        self.assertEqual(len(fw.notes), 2)

    def test_release_without_variant(self):
        v = fl.VARIANTS["tft_v1_16mb"]
        rel = self.rel("v6.0.0", ["openevse_wifi_v1.bin"])
        with self.assertRaises(ValueError):
            fl.resolve_release(fl.GitHub(), rel, v, False, [rel], nolog)

    def test_no_bootloader_anywhere(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        rel = self.rel("v6.0.0", ["openevse_wifi_v1_16mb.bin"])
        with self.assertRaises(ValueError):
            fl.resolve_release(fl.GitHub(), rel, v, True, [rel], nolog)

    def _artifact_zip(self, name):
        zpath = os.path.join(self.tmp, name + ".zip")
        with zipfile.ZipFile(zpath, "w") as z:
            z.write(self.files["openevse_wifi_v1_16mb.bin"], "firmware.bin")
            z.write(self.files["bootloader_v1_16mb.bin"], "bootloader.bin")
            z.write(self.files["partitions_v1_16mb.bin"], "partitions.bin")
        return zpath

    def test_actions_artifact(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        zpath = self._artifact_zip(v.artifact_name)
        test = self

        class FakeGH(fl.GitHub):
            def run_artifacts(self, run_id):
                test.assertEqual(run_id, 42)
                return [{"id": 7, "name": "openevse_wifi_v1.bin", "expired": False},
                        {"id": 8, "name": v.artifact_name, "expired": False}]

            def artifact_download_url(self, run_id, artifact):
                test.assertEqual(artifact["id"], 8)
                return pathlib.Path(zpath).as_uri(), False

        run = {"id": 42, "run_number": 99, "head_branch": "master", "head_sha": "abc1234def",
               "created_at": "2026-10-24T00:00:00Z", "display_title": "Fix things"}
        fw = fl.resolve_actions(FakeGH(), run, v, True, [], nolog)
        self.assertEqual(read(fw.app), make_app())
        self.assertEqual(read(fw.bootloader), make_bootloader())
        self.assertIn("#99", fw.description)

    def test_actions_expired(self):
        v = fl.VARIANTS["wifi_v1_16mb"]

        class FakeGH(fl.GitHub):
            def run_artifacts(self, run_id):
                return [{"id": 9, "name": v.artifact_name, "expired": True}]

        with self.assertRaises(ValueError):
            fl.resolve_actions(FakeGH(), {"id": 1, "run_number": 1}, v, True, [], nolog)

    def test_local_zip(self):
        v = fl.VARIANTS["wifi_v1_16mb"]
        zpath = self._artifact_zip("download")
        fw = fl.resolve_local(zpath, "", "", v, True, fl.GitHub(), [], nolog)
        self.assertEqual(read(fw.app), make_app())
        self.assertEqual(read(fw.partitions),
                         read(os.path.join(FIX, "partitions_v1_16mb.bin")))

    def test_local_app_only(self):
        v = fl.VARIANTS["wifi_v1_4mb"]
        fw = fl.resolve_local(self.files["openevse_wifi_v1.bin"], "", "", v, False,
                              fl.GitHub(), [], nolog)
        self.assertIsNone(fw.bootloader)

    def test_run_label(self):
        run = {"run_number": 5, "head_branch": "feature", "head_sha": "0123456789",
               "created_at": "2026-10-01T00:00:00Z", "display_title": "Add thing\nmore",
               "pull_requests": [{"number": 321}]}
        self.assertEqual(fl.run_label(run), "#5  PR #321  0123456  2026-10-01  Add thing")


class EsptoolRunnerTests(unittest.TestCase):

    def test_worker_runs_esptool(self):
        lines = []
        out = fl.run_esptool(["version"], lines.append)
        self.assertRegex(out, r"\d+\.\d+")

    def test_progress_protocol(self):
        script = ("import sys\n"
                  "print('Connecting...')\n"
                  "sys.stdout.write('@@PROGRESS 25.0 Writing at 0x10000\\n')\n"
                  "sys.stdout.write('Writing at 0x00010000... (50 %)\\r')\n"
                  "print('@@PROGRESS 100.0 Writing at 0x10000')\n"
                  "print('Hash of data verified.')\n")
        orig = fl.esptool_command
        fl.esptool_command = lambda args: [sys.executable, "-c", script]
        try:
            logs, prog = [], []
            fl.run_esptool([], logs.append, lambda p, s: prog.append(p))
        finally:
            fl.esptool_command = orig
        self.assertEqual(prog, [25.0, 50.0, 100.0])
        self.assertEqual(logs[1:], ["Connecting...", "Hash of data verified."])

    def test_failure_hint(self):
        orig = fl.esptool_command
        fl.esptool_command = lambda args: [sys.executable, "-c",
                                           "print('Failed to connect to ESP32'); exit(2)"]
        try:
            with self.assertRaises(fl.EsptoolError) as cm:
                fl.run_esptool([], nolog)
        finally:
            fl.esptool_command = orig
        self.assertIn("BOOT", str(cm.exception))

    def test_v4_args(self):
        self.assertEqual(
            fl._esptool_v4_args(["--before", "default-reset", "write-flash",
                                 "--flash-mode", "dio", "0x1000", "boot.bin"]),
            ["--before", "default_reset", "write_flash", "--flash_mode", "dio",
             "0x1000", "boot.bin"])


if __name__ == "__main__":
    unittest.main()
