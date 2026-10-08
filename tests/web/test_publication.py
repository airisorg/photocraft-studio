"""Publication checks use disposable Git history; no private values are emitted."""
import importlib.util
import os
import re
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET

SPEC = importlib.util.spec_from_file_location("publication", Path(__file__).resolve().parents[2]/"packaging/web/check-publication.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


class PublicationHistory(unittest.TestCase):
    def test_privileged_workflow_actions_are_immutable(self):
        root = Path(__file__).resolve().parents[2]
        for name in ["release.yml", "update-and-release.yml"]:
            for action in re.findall(r"uses:\s*([^\s#]+)", (root/".github/workflows"/name).read_text()):
                if not action.startswith("./"):
                    self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", name)

    def test_msi_references_the_staged_notice_tree(self):
        root = Path(__file__).resolve().parents[2]
        tree = ET.parse(root/"packaging/windows/photocraft.wxs")
        namespace = {"w": "http://wixtoolset.org/schemas/v4/wxs"}
        group = tree.find(".//w:ComponentGroup[@Id='PhotocraftFiles']", namespace)
        files = group.find("w:Files", namespace)
        self.assertEqual(files.attrib, {"Directory": "INSTALLFOLDER", "Subdirectory": "Licenses",
                                       "Include": "$(var.BinDir)\\Licenses\\**"})
        self.assertIsNotNone(tree.find(".//w:Feature/w:ComponentGroupRef[@Id='PhotocraftFiles']", namespace))

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell is required to execute the Windows notice copier")
    def test_windows_notice_copy_includes_optional_embedded_font_licenses(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/"portable notices"
            fonts = Path(directory)/"craft fonts"
            license = fonts/"fonts/Synthetic Family/OFL.txt"
            license.parent.mkdir(parents=True)
            license.write_text("Synthetic font license fixture")
            subprocess.run([shutil.which("pwsh"), "-NoProfile", "-File", str(root/"packaging/windows/copy-notices.ps1"),
                            "-Root", str(root), "-Destination", str(destination)],
                           env=dict(os.environ, CRAFT_FONTS_DIR=str(fonts)), check=True, capture_output=True)
            for name in ["NOTICE", "ATTRIBUTION.md", "SECURITY.md", "LICENSE-MIT", "LICENSE-APACHE",
                         "assets/fonts/OFL-Inter.txt", "assets/fonts/OFL-JetBrainsMono.txt",
                         "assets/icons/LICENSE-lucide.txt", "assets/dict/LICENSE-SCOWL.txt"]:
                self.assertEqual((destination/name).read_bytes(), (root/name).read_bytes())
            self.assertEqual((destination/"OFL-Synthetic Family.txt").read_bytes(), license.read_bytes())

    def test_generic_binary_package_keeps_embedded_asset_notices(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)/"package"
            destination.mkdir()
            environment = dict(os.environ, DIST=str(Path(directory)/"dist"),
                               CARGO_TARGET_DIR=str(Path(directory)/"target"),
                               CRAFT_FONTS_DIR="", PHOTOCRAFT_VERSION="0.0.0")
            subprocess.run(["bash", "-c", 'source "$1"; copy_docs "$2"', "publication-test",
                            str(root/"packaging/env.sh"), str(destination)],
                           env=environment, check=True, capture_output=True)
            for name in ["NOTICE", "ATTRIBUTION.md", "SECURITY.md", "LICENSE-MIT", "LICENSE-APACHE",
                         "assets/fonts/OFL-Inter.txt", "assets/fonts/OFL-JetBrainsMono.txt",
                         "assets/icons/LICENSE-lucide.txt", "assets/dict/LICENSE-SCOWL.txt"]:
                self.assertEqual((destination/name).read_bytes(), (root/name).read_bytes())

    def test_deleted_assets_and_home_paths_still_block_history_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
            git("init", "-b", "main")
            git("config", "user.name", "Synthetic fixture")
            git("config", "user.email", "fixture@example.invalid")
            (root/"docs/brand").mkdir(parents=True)
            (root/"docs/brand/mark.svg").write_text("<svg/>")
            (root/"notes.txt").write_text("synthetic private location: /"+"Users/"+"synthetic-fixture/private/")
            git("add", ".")
            git("commit", "-m", "Synthetic historical content")
            git("rm", "docs/brand/mark.svg", "notes.txt")
            git("commit", "-m", "Remove from current tree")
            result = publication.history(root)
            self.assertEqual(result["restrictedBrandPathCount"], 1)
            self.assertEqual(result["personalHomePathBlobCount"], 1)
            self.assertEqual(result["authorIdentityCountForManualReview"], 1)
            self.assertNotIn("synthetic-fixture", str(result))
            self.assertNotIn("fixture@example.invalid", str(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
