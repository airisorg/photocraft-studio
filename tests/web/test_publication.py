"""Publication checks use disposable Git history; no private values are emitted."""
import importlib.util
import hashlib
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET

SPEC = importlib.util.spec_from_file_location("publication", Path(__file__).resolve().parents[2]/"packaging/web/check-publication.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


class PublicationHistory(unittest.TestCase):
    def test_freebsd_bootstrap_pin_and_fail_closed_execution(self):
        root = Path(__file__).resolve().parents[2]
        pinned = '4d9fef2e40731489f3186c61a0f178d54d79864fe3d34791edf5b17f70074956'
        url = 'https://static.rust-lang.org/rustup/archive/1.28.2/x86_64-unknown-freebsd/rustup-init'
        # A harmless marker replaces the binary. The official downloaded installer
        # is never invoked; only the actual workflow control flow is exercised.
        payload = b'#!/bin/sh\nprintf "%s\\n" "$@" > "$FIXTURE_MARKER"\n'
        fixture_digest = hashlib.sha256(payload).hexdigest()
        for name in ['freebsd.yml', 'release.yml']:
            source = (root/'.github/workflows'/name).read_text()
            self.assertNotIn('sh.rustup.rs', source)
            blocks = re.findall(r'^          prepare: \|\n((?: {12}.*\n)+)', source, re.M)
            self.assertEqual(len(blocks), 1, name)
            block = textwrap.dedent(blocks[0])
            self.assertIn(url, block)
            self.assertEqual(block.count(pinned), 1)
            self.assertIn('--default-host x86_64-unknown-freebsd --default-toolchain stable', block)
            self.assertNotRegex(block, r'curl[^\n]*\|')
            with tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                binaries = temp/'bin'
                binaries.mkdir()
                programs = {
                    'pkg': '#!/bin/sh\nexit 0\n',
                    # FreeBSD's mktemp -t accepts a prefix; GNU mktemp expects a
                    # template. Keep this fixture independent of the test host.
                    'mktemp': '#!'+sys.executable+'\nimport os,sys,tempfile\n'
                              'assert sys.argv[1:] == ["-t", "photocraft-rustup"]\n'
                              'fd,path=tempfile.mkstemp(prefix="photocraft-rustup",dir=os.environ["TMPDIR"])\n'
                              'os.close(fd)\nprint(path)\n',
                    'curl': '#!'+sys.executable+'\nimport os,sys\nfrom pathlib import Path\n'
                            'if os.environ["FIXTURE_MODE"] == "download-error": sys.exit(22)\n'
                            'data='+repr(payload)+'\n'
                            'if os.environ["FIXTURE_MODE"] == "tampered": data += b"tampered"\n'
                            'Path(sys.argv[sys.argv.index("--output")+1]).write_bytes(data)\n',
                    'sha256': '#!'+sys.executable+'\nimport hashlib,sys\nfrom pathlib import Path\n'
                              'assert sys.argv[1] == "-q"\n'
                              'print(hashlib.sha256(Path(sys.argv[2]).read_bytes()).hexdigest())\n',
                }
                for executable, content in programs.items():
                    path = binaries/executable
                    path.write_text(content)
                    path.chmod(0o700)
                marker = temp/'invoked'
                for mode in ['tampered', 'download-error', 'valid']:
                    with self.subTest(workflow=name, mode=mode):
                        environment = {**os.environ, 'PATH':str(binaries)+':/usr/bin:/bin',
                                       'TMPDIR':str(temp), 'FIXTURE_MODE':mode, 'FIXTURE_MARKER':str(marker)}
                        result = subprocess.run(['/bin/sh', '-c', block.replace(pinned, fixture_digest)],
                                                env=environment, capture_output=True)
                        self.assertEqual(result.returncode == 0, mode == 'valid')
                        self.assertEqual(marker.exists(), mode == 'valid')
                        self.assertEqual(list(temp.glob('photocraft-rustup*')), [], 'Downloaded file must be removed')
                        if mode == 'valid':
                            self.assertEqual(marker.read_text().splitlines(), ['-y', '--profile', 'minimal',
                                             '--default-host', 'x86_64-unknown-freebsd', '--default-toolchain', 'stable'])

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
