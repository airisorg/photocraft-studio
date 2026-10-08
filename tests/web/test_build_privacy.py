"""Release path privacy tests; only a synthetic Trunk executable is invoked."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("build_release", ROOT / "packaging/web/build-release.py")
build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build)


class BuildPrivacy(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.home = self.directory / "operator"
        self.root = self.home / "repository with spaces"
        self.root.mkdir(parents=True)

    def test_real_wrapper_invokes_only_trunk_with_preserved_encoded_flags(self):
        script = self.root / "packaging/web/build-release.py"
        script.parent.mkdir(parents=True)
        shutil.copyfile(SPEC.origin, script)
        (self.root / "apps/photocraft-web").mkdir(parents=True)
        binary = self.directory / "bin"
        binary.mkdir()
        trunk = binary / "trunk"
        trunk.write_text("#!" + sys.executable + "\nimport os,json,sys\nfrom pathlib import Path\n"
                         "Path(os.environ['FIXTURE_CAPTURE']).write_text(json.dumps({'argv':sys.argv[1:],"
                         "'cwd':os.getcwd(),'flags':os.environ['CARGO_ENCODED_RUSTFLAGS'],"
                         "'plainFlagsPresent':'RUSTFLAGS' in os.environ,'target':os.environ['CARGO_TARGET_DIR']}))\n")
        trunk.chmod(0o755)
        capture = self.directory / "capture.json"
        encoded = "--cfg" + build.SEPARATOR + 'fixture="a b"'
        target = self.root / "cache"
        environment = {**os.environ, "PATH": str(binary), "FIXTURE_CAPTURE": str(capture),
                       "CARGO_HOME": str(self.home / "cargo"), "RUSTUP_HOME": str(self.home / "rustup"),
                       "CARGO_TARGET_DIR": str(target), "CARGO_ENCODED_RUSTFLAGS": encoded,
                       "RUSTFLAGS": "unselected flags must not leak"}
        subprocess.run([sys.executable, str(script)], env=environment, cwd=self.directory,
                       capture_output=True, check=True)
        result = json.loads(capture.read_text())
        self.assertEqual(result["argv"], ["build", "--release"])
        self.assertEqual(result["cwd"], str(self.root / "apps/photocraft-web"))
        flags = result["flags"].split(build.SEPARATOR)
        self.assertEqual(flags[:2], ["--cfg", 'fixture="a b"'])
        self.assertIn("--remap-path-prefix=" + str(self.root) + "=/source", flags)
        self.assertIn("--remap-path-prefix=" + str(target) + "=/build", flags)
        self.assertEqual(result["target"], str(target), "Use the existing build cache")
        self.assertFalse(result["plainFlagsPresent"])

    def test_plain_flags_preserve_cargo_cfg_quotes_without_shell_expansion(self):
        flags = '  --cfg feature="browser" --cfg literal="$(must_not_run)" -C opt-level=2  '
        environment = build.build_environment(self.root, {"RUSTFLAGS": flags}, self.home)
        actual = environment["CARGO_ENCODED_RUSTFLAGS"].split(build.SEPARATOR)
        self.assertEqual(actual[:6], ["--cfg", 'feature="browser"', "--cfg",
                                     'literal="$(must_not_run)"', "-C", "opt-level=2"])
        empty = build.build_environment(self.root, {"CARGO_ENCODED_RUSTFLAGS": "", "RUSTFLAGS": "ignored"}, self.home)
        self.assertNotIn("ignored", empty["CARGO_ENCODED_RUSTFLAGS"])
        # Cargo forwards invalid cfg quoting for rustc to reject; do not repair it
        # or treat it as executable shell syntax in the wrapper.
        invalid = build.build_environment(self.root, {"RUSTFLAGS": '--cfg feature="unclosed'}, self.home)
        self.assertEqual(invalid["CARGO_ENCODED_RUSTFLAGS"].split(build.SEPARATOR)[:2], ["--cfg", 'feature="unclosed'])
        # Pinned Cargo splits literal spaces, then trims; interior tabs are not
        # argument separators, unlike Python split() or shell parsing.
        tabbed = build.build_environment(self.root, {"RUSTFLAGS": '\t--cfg\tfeature="browser"\n  -C'}, self.home)
        self.assertEqual(tabbed["CARGO_ENCODED_RUSTFLAGS"].split(build.SEPARATOR)[:2], ['--cfg\tfeature="browser"', '-C'])
        separators = build.build_environment(self.root, {"RUSTFLAGS": '\u2000--cfg\u2000 \x1cunchanged\x1c'}, self.home)
        self.assertEqual(separators["CARGO_ENCODED_RUSTFLAGS"].split(build.SEPARATOR)[:2], ['--cfg', '\x1cunchanged\x1c'])
        with self.assertRaises(ValueError):
            build.build_environment(self.root, {"RUSTFLAGS": "unsupported" + build.SEPARATOR + "delimiter"}, self.home)

    def test_nested_and_resolved_paths_get_specific_remaps_last(self):
        alias = self.directory / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        environment = build.build_environment(alias, {"CARGO_TARGET_DIR": "../../cache"}, self.home)
        flags = environment["CARGO_ENCODED_RUSTFLAGS"].split(build.SEPARATOR)
        mappings = [x.removeprefix("--remap-path-prefix=").rsplit("=", 1) for x in flags]
        expected = {str(self.home): "/operator-home", str(self.root): "/source",
                    str(self.home / ".cargo"): "/cargo-home", str(self.home / ".rustup"): "/rustup-home",
                    str(self.root / "cache"): "/build"}
        self.assertEqual(dict(mappings), expected)
        self.assertEqual([len(x[0]) for x in mappings], sorted(len(x[0]) for x in mappings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
