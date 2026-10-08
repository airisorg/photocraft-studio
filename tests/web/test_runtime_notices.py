"""Offline provenance and fail-closed tests for distributed Rust runtime notices."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("runtime_notices", ROOT / "packaging/web/runtime-notices.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
PRODUCER = "1.95.0 (59807616e 2026-04-14)"


def integer(value):
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def string(value):
    data = value.encode()
    return integer(len(data)) + data


def wasm(producer=PRODUCER):
    data = string("producers") + integer(1) + string("processed-by") + integer(1) + string("rustc") + string(producer)
    return b"\0asm\x01\0\0\0\0" + integer(len(data)) + data


class RuntimeNotices(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        shutil.copytree(ROOT / "packaging/licenses/rust-runtime", self.bundle)
        self.wasm = self.root / "app.wasm"
        self.wasm.write_bytes(wasm())

    def collect(self, label="output"):
        return runtime.collect(self.bundle, self.wasm, self.root / label)

    def test_exact_notice_bytes_and_deterministic_offline_output(self):
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")), \
             patch("subprocess.Popen", side_effect=AssertionError("Processes forbidden")):
            first, second = self.collect("first"), self.collect("second")
        self.assertEqual(first, second)
        self.assertEqual(first["browserArtifact"]["rustcProducer"], PRODUCER)
        self.assertEqual(first["browserArtifact"]["sha256"], runtime.digest(self.wasm.read_bytes()))
        paths = {item["path"] for item in first["files"]}
        self.assertTrue({"COPYRIGHT-library.html", "compiler-builtins/LICENSE.txt", "compiler-builtins/libm/LICENSE.txt", "compiler-rt/CREDITS.TXT", "compiler-rt/LICENSE.TXT"} <= paths)
        for name in paths | {"index.json"}:
            self.assertEqual((self.root / "first" / name).read_bytes(), (self.root / "second" / name).read_bytes())
            if name != "index.json":
                self.assertEqual((self.root / "first" / name).read_bytes(), (self.bundle / name).read_bytes())
        self.assertNotIn(str(self.root), json.dumps(first))

    def test_changed_or_absent_compiler_fails_before_output(self):
        for data in [wasm("1.96.0 (different commit)"), wasm("1.95.0 (000000000 2026-04-14)"), b"\0asm\x01\0\0\0"]:
            with self.subTest(data=data):
                self.wasm.write_bytes(data)
                with self.assertRaises(ValueError):
                    self.collect()
                self.assertFalse((self.root / "output").exists())

    def test_missing_or_tampered_notice_fails_before_output(self):
        text = self.bundle / "compiler-builtins/LICENSE.txt"
        original = text.read_bytes()
        for data in [None, original + b"Changed attribution"]:
            if data is None:
                text.unlink()
            else:
                text.write_bytes(data)
            with self.assertRaises((ValueError, OSError)):
                self.collect()
            self.assertFalse((self.root / "output").exists())

    def test_traversal_and_symlink_notices_rejected(self):
        manifest = self.bundle / "manifest.json"
        original = json.loads(manifest.read_text())
        changed = json.loads(manifest.read_text())
        changed["files"][0]["path"] = "../outside"
        manifest.write_text(json.dumps(changed))
        with self.assertRaises(ValueError):
            self.collect()
        manifest.write_text(json.dumps(original))
        text = self.bundle / original["files"][0]["path"]
        outside = self.root / "outside"
        outside.write_bytes(text.read_bytes())
        text.unlink()
        text.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.collect()
        self.assertFalse((self.root / "output").exists())

    def test_malformed_or_duplicate_producers_rejected(self):
        for data in [b"bad", wasm()[:-1], wasm() + wasm()[8:], b"\0asm\x01\0\0\0\0\xff\xff\xff\xff\xff", wasm() + b"\0\x01\0" * 128]:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    runtime.wasm_compiler(data)

    def test_cloud_build_rejects_wrong_compiler_before_cargo(self):
        lines = (ROOT / "Dockerfile").read_text().splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("RUN rustc -Vv"))
        parts = [lines[start].removeprefix("RUN ")]
        while parts[-1].endswith("\\"):
            parts.append(lines[start + len(parts)])
        command = "\n".join(parts)
        marker = self.root / "cargo-called"
        compiler = self.root / "rustc"
        cargo = self.root / "cargo"
        compiler.write_text("#!" + sys.executable + "\nimport os\nprint(os.environ['FIXTURE_RUSTC'])\n")
        cargo.write_text("#!" + sys.executable + "\nfrom pathlib import Path\nimport os\nPath(os.environ['FIXTURE_MARKER']).write_text('called')\n")
        compiler.chmod(0o755)
        cargo.chmod(0o755)
        pin = json.loads((self.bundle / "manifest.json").read_text())["rust"]
        valid = "release: " + pin["version"] + "\ncommit-hash: " + pin["commit"]
        for version, expected in [(valid.replace(pin["version"], "1.96.0"), False),
                                  (valid.replace(pin["commit"], "0" * 40), False), (valid, True)]:
            env = {**os.environ, "PATH": str(self.root) + ":/usr/bin:/bin", "FIXTURE_RUSTC": version, "FIXTURE_MARKER": str(marker)}
            result = subprocess.run(["/bin/sh", "-c", command], env=env, capture_output=True)
            self.assertEqual(result.returncode == 0, expected)
            self.assertEqual(marker.exists(), expected, "Wrong compiler must not invoke cargo")


if __name__ == "__main__":
    unittest.main(verbosity=2)
