"""Verify redistributable notice bytes, provenance and fail-closed missing-text gates."""
import importlib.util
import json
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("rust_notices", Path(__file__).resolve().parents[2] / "packaging/web/rust-notices.py")
notices = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notices)


class RustNotices(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / "registry/src/synthetic-index/synthetic-1.2.3"
        self.registry.mkdir(parents=True)
        self.archive = self.root / "registry/cache/synthetic-index/synthetic-1.2.3.crate"
        self.archive.parent.mkdir(parents=True)
        self.package = {"name": "synthetic", "version": "1.2.3", "manifest_path": str(self.registry / "Cargo.toml"),
                        "license": "MIT", "license_file": None, "source": "registry+https://github.com/rust-lang/crates.io-index"}
        self.graphs = [{"name": "browser", "packages": ["synthetic@1.2.3"]}]
        self.files = {}

    def registry_file(self, name, data):
        path = self.registry / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.files[name] = data
        with tarfile.open(self.archive, "w:gz") as archive:
            for relative, content in self.files.items():
                member = tarfile.TarInfo("synthetic-1.2.3/" + relative)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        (self.root / "Cargo.lock").write_text('[[package]]\nname="synthetic"\nversion="1.2.3"\nsource="' + self.package['source'] + '"\nchecksum="' + notices.digest(self.archive.read_bytes()) + '"\n')

    def collect(self, name="output"):
        return notices.collect(self.root, self.root / name, [self.package], self.graphs, "locked-fixture-hash")

    def supplement(self, data=b"MIT License\nCopyright (c) Original test publisher\n"):
        path = self.root / "packaging/licenses/rust-supplements/synthetic/LICENSE-MIT"
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        manifest = [{"package": "synthetic", "version": "1.2.3", "license": "MIT", "files": [
            {"path": "rust-supplements/synthetic/LICENSE-MIT", "source": "https://example.invalid/upstream/0123456789abcdef0123456789abcdef01234567/LICENSE-MIT",
             "sha256": notices.digest(data)}]}]
        (self.root / "packaging/licenses/rust-supplements.json").write_text(json.dumps(manifest))
        return path

    def test_exact_nested_texts_and_reproducible_path_free_manifest(self):
        for name, data in {"LICENSE-MIT": b"Original MIT copyright and grant\n", "NOTICE": b"Original attribution\n",
                           "fonts/OFL.txt": b"Exact font copyright\n", "fonts/UFL.txt": b"Exact Ubuntu font notice\n",
                           "fonts/Hack-Regular.txt": b"Exact Hack font notice\n", "third_party/source/LICENSE": b"Third-party notice\n"}.items():
            self.registry_file(name, data)
        self.collect("first")
        self.collect("second")
        first, second = self.root / "first", self.root / "second"
        self.assertEqual({p.relative_to(first): p.read_bytes() for p in first.rglob("*") if p.is_file()},
                         {p.relative_to(second): p.read_bytes() for p in second.rglob("*") if p.is_file()})
        for name in self.files:
            self.assertEqual((first / "synthetic-1.2.3/registry" / name).read_bytes(), (self.registry / name).read_bytes())
        self.assertNotIn(str(self.root), (first / "index.json").read_text())

    def test_nested_font_notice_does_not_hide_missing_crate_license(self):
        self.registry_file("fonts/OFL.txt", b"Font-only license\n")
        with self.assertRaisesRegex(ValueError, "Missing reviewed crate license texts: synthetic@1.2.3"):
            self.collect()
        self.assertFalse((self.root / "output").exists())

    def test_modified_registry_notice_is_rejected_before_distribution(self):
        self.registry_file("LICENSE-MIT", b"Original publisher notice\n")
        (self.registry / "LICENSE-MIT").write_bytes(b"Replaced notice\n")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.collect()
        self.assertFalse((self.root / "output").exists())

    def test_modified_cached_archive_cannot_supply_authentic_notices(self):
        self.registry_file("LICENSE-MIT", b"Original publisher notice\n")
        self.archive.write_bytes(self.archive.read_bytes() + b"modified")
        with self.assertRaisesRegex(ValueError, "Locked registry archive checksum mismatch"):
            self.collect()
        self.assertFalse((self.root / "output").exists())

    def test_pinned_supplement_preserves_bytes_and_rejects_tampering(self):
        self.registry_file("README.md", b"The publisher omitted its license file\n")
        path = self.supplement()
        index = self.collect()
        record = index["packages"][0]["files"][0]
        self.assertEqual((self.root / "output" / record["path"]).read_bytes(), path.read_bytes())
        self.assertEqual(record["sha256"], notices.digest(path.read_bytes()))
        path.write_bytes(b"Unauthentic replacement\n")
        with self.assertRaisesRegex(ValueError, "Supplement checksum mismatch"):
            self.collect("tampered")
        self.assertFalse((self.root / "tampered").exists())

    def test_license_paths_cannot_copy_files_outside_the_package(self):
        self.registry_file("README.md", b"fixture\n")
        (self.root / "unrelated.txt").write_bytes(b"Must not be distributed\n")
        self.package["license_file"] = "../unrelated.txt"
        with self.assertRaisesRegex(ValueError, "escaped"):
            self.collect()
        self.assertFalse((self.root / "output").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
