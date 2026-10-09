"""Real scanner policy boundaries; disposable synthetic history, no remote writes."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "publication_policy", Path(__file__).resolve().parents[2]/"packaging/web/check-publication.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)
SUPPRESS_ALL = "[extend]\nuseDefault = true\n\n[allowlist]\npaths = ['''.*''']\n"


class PublicationPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scanner = shutil.which("gitleaks")
        if cls.scanner is None:
            raise RuntimeError("Install the pinned Gitleaks binary before running publication-policy tests")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="photocraft-policy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)/"candidate"
        self.root.mkdir()
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Synthetic fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.commit("README.md", "Synthetic scanner fixture\n")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root,
                                       stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self, path, content):
        (self.root/path).write_text(content)
        self.git("add", path)
        self.git("commit", "-m", "Synthetic publication fixture")
        self.head = self.git("rev-parse", "HEAD")
        return self.head

    def secret(self, suffix=""):
        # Construct a nonfunctional test value so the test source is not itself a leak.
        value = "ghp_" + "4DF7g3Ha8BkL2mNpQ9rStUvWxYz056AcDeF1"
        return self.commit("example.txt", 'github_token = "'+value+'"'+suffix+'\n')

    def scan(self, ref=None):
        return publication.secrets(self.root, ref or self.head)

    def assert_detected(self):
        self.assertEqual(self.scan(), {"status": "checked", "findings": 1})

    def test_clean_history_and_actual_default_rule_positive(self):
        self.assertEqual(self.scan(), {"status": "checked", "findings": 0})
        self.secret()
        self.assert_detected()

    def test_candidate_config_cannot_suppress_default_rules(self):
        self.secret()
        self.commit(".gitleaks.toml", SUPPRESS_ALL)
        self.assert_detected()

    def test_secret_added_only_by_merge_blocks_full_publication(self):
        for name in ["LICENSE-MIT", "LICENSE-APACHE", "NOTICE", "ATTRIBUTION.md", "SECURITY.md",
                     "assets/fonts/OFL-Inter.txt", "assets/fonts/OFL-JetBrainsMono.txt",
                     "assets/icons/LICENSE-lucide.txt", "assets/dict/LICENSE-SCOWL.txt"]:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("Synthetic notice\n")
        self.git("add", "--all")
        self.git("commit", "-m", "Synthetic required notices")
        base = self.git("rev-parse", "HEAD")
        self.commit("left.txt", "Left parent is clean\n")
        self.git("checkout", "-b", "right", base)
        self.commit("right.txt", "Right parent is clean\n")
        self.git("checkout", "main")
        self.git("merge", "--no-ff", "--no-commit", "right")
        self.secret()
        self.assertEqual(len(self.git("rev-list", "--parents", "-n", "1", self.head).split()), 3)
        self.assertEqual(self.scan(base), {"status": "checked", "findings": 0})
        result = publication.check(self.root, ref=self.head)
        self.assertFalse(result["dirty"])
        self.assertEqual(result["missingNoticeCount"], 0)
        self.assertEqual(result["secrets"]["status"], "checked")
        self.assertGreater(result["secrets"]["findings"], 0)
        self.assertFalse(result["passed"])

    def test_binary_diff_attributes_cannot_hide_plaintext_finding(self):
        self.commit(".gitattributes", "example.txt binary\n")
        self.secret()
        self.assert_detected()

    def test_environment_config_cannot_suppress_default_rules(self):
        self.secret()
        config = Path(self.temporary.name)/"external-suppression.toml"
        config.write_text(SUPPRESS_ALL)
        for environment in [{"GITLEAKS_CONFIG": str(config)},
                            {"GITLEAKS_CONFIG_TOML": SUPPRESS_ALL}]:
            with self.subTest(environment=next(iter(environment))), mock.patch.dict(os.environ, environment):
                self.assert_detected()

    def test_candidate_ignore_fingerprint_cannot_suppress_finding(self):
        self.secret()
        captured = []
        run = subprocess.run

        def inspect_report(*args, **kwargs):
            result = run(*args, **kwargs)
            argv = args[0]
            if "--report-path" in argv:
                captured.extend(json.loads(Path(argv[argv.index("--report-path")+1]).read_text()))
            return result

        with mock.patch.object(publication.subprocess, "run", side_effect=inspect_report):
            self.assert_detected()
        self.assertEqual(len(captured), 1)
        self.commit(".gitleaksignore", captured[0]["Fingerprint"]+"\n*\n")
        self.assert_detected()

    def test_inline_allow_comment_cannot_suppress_finding(self):
        self.secret(" # gitleaks:allow")
        self.assert_detected()

    def test_linked_worktree_scans_same_history_without_checkout_policy(self):
        self.secret()
        self.commit(".gitleaks.toml", SUPPRESS_ALL)
        linked = Path(self.temporary.name)/"linked"
        self.git("worktree", "add", "--detach", str(linked), self.head)
        self.assertTrue((linked/".git").is_file())
        self.assertEqual(publication.secrets(linked, self.head), {"status": "checked", "findings": 1})

    def test_owned_policy_is_explicit_and_removed_on_scanner_error(self):
        owned = []
        run = subprocess.run

        def inspect(argv, **kwargs):
            if argv[0] == "git":
                return run(argv, **kwargs)
            directory = Path(kwargs["cwd"])
            owned.append(directory)
            self.assertNotEqual(directory, self.root)
            self.assertEqual(Path(argv[argv.index("--config")+1]).read_text(), "[extend]\nuseDefault = true\n")
            self.assertEqual(Path(argv[argv.index("--gitleaks-ignore-path")+1]).read_text(), "")
            self.assertIn("--ignore-gitleaks-allow", argv)
            self.assertIn("--redact=100", argv)
            self.assertIn("--log-opts=--diff-merges=separate --text --no-ext-diff --no-textconv "+self.head, argv)
            self.assertNotIn("GITLEAKS_CONFIG", kwargs["env"])
            self.assertNotIn("GITLEAKS_CONFIG_TOML", kwargs["env"])
            return subprocess.CompletedProcess(argv, 2)

        with mock.patch.object(publication.subprocess, "run", side_effect=inspect):
            self.assertEqual(self.scan(), {"status": "error", "findings": None})
        self.assertEqual(len(owned), 1)
        self.assertFalse(owned[0].exists())

    def test_missing_or_inconsistent_report_is_not_a_clean_scan(self):
        run = subprocess.run
        for code, payload in [(0, None), (1, None), (1, "[]"), (0, "{}"), (0, "invalid")]:
            with self.subTest(code=code, payload=payload):
                def incomplete(argv, **kwargs):
                    if argv[0] == "git":
                        return run(argv, **kwargs)
                    if payload is not None:
                        Path(argv[argv.index("--report-path")+1]).write_text(payload)
                    return subprocess.CompletedProcess(argv, code)
                with mock.patch.object(publication.subprocess, "run", side_effect=incomplete):
                    self.assertEqual(self.scan(), {"status": "error", "findings": None})


if __name__ == "__main__":
    unittest.main(verbosity=2)
