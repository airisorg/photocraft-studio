"""No Docker, database or network: package/receipt and failure-cleanup guards."""
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("container_gate", ROOT / "packaging/web/check-container-package.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class ContainerGate(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="photocraft-image-guard-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.candidate = "a" * 40
        self.package = self.root / "package.zip"
        self.receipt = self.root / "receipt.json"
        self.fixture = self.root / "fixture.pcraft"
        self.fixture.write_bytes(b"fixture")
        self.create_package()

    def create_package(self, *, dirty=False, extra=None):
        wasm = b"\x00asm-exact-test-bytes"
        manifest = {"sourceCommit": self.candidate, "dirty": dirty,
                    "wasm": {"path": "public/editor.wasm", "sha256": hashlib.sha256(wasm).hexdigest()}}
        items = {"release-source.json": json.dumps(manifest), "public/editor.wasm": wasm,
                 "public/index.html": "<canvas></canvas>", "Dockerfile": "FROM scratch\n",
                 "Cargo.toml": "fixture", "Cargo.lock": "fixture"}
        items.update({"tests/web/" + name: "raise RuntimeError('candidate suites must not be executed')" for name in gate.SUITES})
        if extra:
            items.update(extra)
        with zipfile.ZipFile(self.package, "w") as archive:
            for name, data in items.items():
                archive.writestr(name, data)

    def args(self, mode=None):
        values = ["--package", str(self.package), "--candidate", self.candidate]
        if mode:
            values += [mode, "--receipt", str(self.receipt)]
        if mode:
            values += ["--fixture", str(self.fixture)]
        return values

    def expected(self):
        with tempfile.TemporaryDirectory() as temporary:
            return gate.inspect_package(self.package, self.candidate, Path(temporary))

    def passing(self):
        expected = self.expected()
        return {"version": 1, "status": "passed", "platform": "Linux", **expected,
                "database_transport": "TLS verify-full",
                "image_id": "sha256:" + "b" * 64,
                "served_wasm_sha256": expected["wasm_sha256"], "served_index_sha256": expected["index_sha256"],
                "checks": [{"name": name, "exit_code": 0} for name in gate.SUITES],
                "cleanup": {key: True for key in ["container", "image", "database", "cluster", "context"]}}

    def fake_execution(self, *, fail=None, cleanup_fail=None):
        calls, databases, removed, contexts = [], [], [], []

        def command(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[:2] == ["docker", "build"]:
                contexts.append(Path(argv[-1]))
                self.assertEqual((contexts[-1] / "Dockerfile").read_text(), "FROM scratch\n")
            if fail and fail in " ".join(argv):
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return SimpleNamespace(returncode=0, stdout="sha256:" + "b" * 64 + "\n")

        def database(action, name, fixture):
            databases.append((action, name))
            self.assertRegex(name, r"^photocraft_image_[0-9a-f]{32}$")
            if fail == "create" and action == "create":
                raise TimeoutError("CREATE outcome uncertain")

        def remove(kind, name, token):
            removed.append(kind)
            self.assertIn(token, name)
            if cleanup_fail == kind:
                raise RuntimeError("cleanup failed")

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"CI": "true", "GITHUB_ACTIONS": "true"}))
            stack.enter_context(patch.object(gate.platform, "system", return_value="Linux"))
            original_stat = Path.stat
            stack.enter_context(patch.object(Path, "stat", lambda p, *a, **kw: SimpleNamespace(st_mode=stat.S_IFSOCK) if str(p) == "/var/run/docker.sock" else original_stat(p, *a, **kw)))
            stack.enter_context(patch.object(gate, "run", side_effect=command))
            stack.enter_context(patch.object(gate, "database", side_effect=database))
            ca = self.root / "fixture-ca.crt"
            ca.write_text("public synthetic CA")
            local = SimpleNamespace(start=lambda: None, close=lambda: None,
                                    admin_url="postgresql://photocraft_test@127.0.0.1:49153/postgres", ca=ca)
            stack.enter_context(patch.object(gate, "LocalPostgres", return_value=local))
            stack.enter_context(patch.object(gate, "port", return_value=49152))
            stack.enter_context(patch.object(gate, "remove_owned", side_effect=remove))
            stack.enter_context(patch.object(gate, "serve_check", side_effect=lambda origin, expected: {
                "served_wasm_sha256": expected["wasm_sha256"], "served_index_sha256": expected["index_sha256"]}))
            if fail or cleanup_fail:
                with self.assertRaises((subprocess.TimeoutExpired, TimeoutError, ValueError)):
                    gate.main(self.args("--execute"))
            else:
                gate.main(self.args("--execute"))
        return calls, databases, removed, contexts

    def test_default_plan_never_contacts_docker_database_or_http(self):
        with patch.object(gate, "run") as run, patch.object(gate, "database") as database, patch.object(gate, "serve_check") as serve, patch.object(gate.tempfile, "TemporaryDirectory") as temporary, contextlib.redirect_stdout(io.StringIO()) as output:
            gate.main(self.args())
        self.assertEqual(json.loads(output.getvalue())["mode"], "plan")
        run.assert_not_called(); database.assert_not_called(); serve.assert_not_called()
        temporary.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_bad_package_is_rejected_before_any_execution(self):
        for changes in [{"dirty": True}, {"extra": {"public/../escape": "x"}},
                        {"extra": {"public//index.html": "alias"}}, {"extra": {"public/./index.html": "alias"}}]:
            with self.subTest(changes=changes):
                self.create_package(**changes)
                with patch.object(gate, "run") as run, self.assertRaises(ValueError):
                    gate.main(self.args("--execute"))
                run.assert_not_called()
                self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_execute_refuses_non_linux_or_non_ci(self):
        for system, ci in [("Darwin", "true"), ("Linux", "false")]:
            with patch.object(gate.platform, "system", return_value=system), patch.dict(os.environ, {"CI": ci, "GITHUB_ACTIONS": "true"}), patch.object(gate, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "Linux GitHub CI"):
                    gate.main(self.args("--execute"))
                run.assert_not_called()

    def test_passing_gate_uses_exact_package_trusted_suites_and_owned_cleanup(self):
        calls, databases, removed, contexts = self.fake_execution()
        self.assertEqual([a for a, _ in databases], ["create", "drop"])
        self.assertEqual(databases[0][1], databases[1][1])
        self.assertEqual(removed, ["container", "image"])
        self.assertFalse(contexts[0].exists())
        suites = [(argv, options) for argv, options in calls if argv[0] != "docker"]
        self.assertEqual([Path(argv[1]).name for argv, _ in suites], list(gate.SUITES))
        for _, options in suites:
            self.assertEqual(options["cwd"], ROOT)
            self.assertTrue(options["env"]["PHOTOCRAFT_TEST_DATABASE_URL"].endswith(databases[0][1]))
            self.assertEqual(options["timeout"], 180)
            self.assertEqual(options["env"]["PGSSLMODE"], "verify-full")
            self.assertEqual(options["env"]["PGHOSTADDR"], "127.0.0.1")
        docker_run = next(argv for argv, _ in calls if argv[:2] == ["docker", "run"])
        self.assertIn("host", docker_run)
        self.assertIn("SUPABASE_CA_CERT=public synthetic CA", docker_run)
        self.assertFalse(any("CLOUD_LOCAL_DEV" in arg or "TOKEN" in arg for arg in docker_run))
        with contextlib.redirect_stdout(io.StringIO()):
            gate.main(self.args("--verify-receipt"))

    def test_build_timeout_cleans_exact_docker_resources_without_creating_database(self):
        _, databases, removed, contexts = self.fake_execution(fail="build")
        self.assertEqual(databases, [])
        self.assertEqual(removed, ["container", "image"])
        self.assertFalse(contexts[0].exists())
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_uncertain_create_outcome_still_drops_only_owned_database(self):
        _, databases, removed, _ = self.fake_execution(fail="create")
        self.assertEqual([a for a, _ in databases], ["create", "drop"])
        self.assertEqual(databases[0][1], databases[1][1])
        self.assertEqual(removed, ["container", "image"])

    def test_contract_failure_does_not_skip_cleanup_or_claim_success(self):
        calls, databases, removed, _ = self.fake_execution(fail="test_live.py")
        self.assertFalse(any("tests/web/test_live_scale.py" in argv for argv, _ in calls))
        self.assertEqual([a for a, _ in databases], ["create", "drop"])
        self.assertEqual(removed, ["container", "image"])
        self.assertEqual(json.loads(self.receipt.read_text())["status"], "failed")

    def test_cleanup_failure_cannot_pass_and_other_resources_are_still_removed(self):
        _, databases, removed, _ = self.fake_execution(cleanup_fail="container")
        self.assertEqual(removed, ["container", "image"])
        self.assertEqual(databases[-1][0], "drop")
        report = json.loads(self.receipt.read_text())
        self.assertEqual(report["status"], "failed")
        self.assertFalse(report["cleanup"]["container"])
        self.assertTrue(report["cleanup"]["image"])

    def test_receipt_cannot_skip_checks_change_bytes_or_claim_unclean_execution(self):
        original = self.passing()
        for key, value in [("source_commit", "c" * 40), ("package_sha256", "c" * 64),
                           ("served_wasm_sha256", "c" * 64), ("checks", []), ("cleanup", {}),
                           ("gate_suite_sha256", {}), ("gate_helper_sha256", {}), ("platform", "Darwin")]:
            with self.subTest(key=key):
                report = copy.deepcopy(original); report[key] = value
                with self.assertRaises(ValueError):
                    gate.verify_receipt(report, self.expected())

    def test_docker_context_and_proxy_environment_cannot_redirect_execution(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "tcp://remote.invalid:2375", "DOCKER_CONTEXT": "remote", "HTTPS_PROXY": "https://outside.invalid"}), patch.object(gate.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            gate.run(["docker", "info"], timeout=20)
        args, kwargs = run.call_args
        self.assertEqual(args[0][:3], ["docker", "--host", "unix:///var/run/docker.sock"])
        self.assertFalse(any(k.startswith("DOCKER_") or k.lower() == "https_proxy" for k in kwargs["env"]))

    def test_cleanup_refuses_other_resources_and_unreachable_daemon(self):
        with patch.object(gate, "run", return_value=SimpleNamespace(returncode=0, stdout="someone-else")) as run:
            with self.assertRaises(RuntimeError):
                gate.remove_owned("container", "owned", "ours")
        self.assertEqual(run.call_count, 1)
        with patch.object(gate, "run", side_effect=[SimpleNamespace(returncode=1), subprocess.TimeoutExpired("info", 20)]):
            with self.assertRaises(subprocess.TimeoutExpired):
                gate.remove_owned("image", "owned", "ours")

    def test_owned_native_postgres_uses_verified_tls_and_cleans_after_start_timeout(self):
        for start_timeout in [False, True]:
            with self.subTest(start_timeout=start_timeout):
                bindir = self.root / "mock-bin"
                bindir.mkdir(exist_ok=True)
                for name in ["initdb", "pg_ctl"]:
                    (bindir / name).touch()
                commands = []

                def command(argv, **kwargs):
                    commands.append(argv)
                    if argv[0] == "pg_config":
                        return SimpleNamespace(returncode=0, stdout=str(bindir))
                    if argv[0] == "openssl":
                        for flag in ["-keyout", "-out"]:
                            if flag in argv:
                                Path(argv[argv.index(flag) + 1]).write_text("synthetic fixture")
                    if Path(argv[0]).name == "initdb":
                        data = Path(argv[argv.index("-D") + 1]); data.mkdir()
                        (data / "postgresql.conf").write_text("")
                    if argv[-1] == "start" and start_timeout:
                        raise subprocess.TimeoutExpired(argv, 40)
                    return SimpleNamespace(returncode=0, stdout="")

                with patch.object(gate, "run", side_effect=command), patch.object(gate, "port", return_value=49154):
                    fixture = gate.LocalPostgres()
                    if start_timeout:
                        with self.assertRaises(subprocess.TimeoutExpired):
                            fixture.start()
                    else:
                        fixture.start()
                    folder = Path(fixture.temporary.name)
                    config = (fixture.data / "postgresql.conf").read_text()
                    self.assertIn("listen_addresses = '127.0.0.1'", config)
                    self.assertIn("ssl = on", config)
                    self.assertIn("shared_buffers = '32MB'", config)
                    self.assertIn("max_connections = 40", config)
                    self.assertEqual(stat.S_IMODE((folder / "server.key").stat().st_mode), 0o600)
                    self.assertIn("subjectAltName=IP:127.0.0.1", (folder / "server.ext").read_text())
                    fixture.close()
                self.assertFalse(folder.exists())
                stops = [c for c in commands if c[-1] == "stop"]
                self.assertEqual(len(stops), 1)
                self.assertEqual(stops[0][stops[0].index("-D") + 1], str(fixture.data))

    def test_served_bytes_are_bounded_and_schema_readiness_precedes_them(self):
        expected = self.expected()
        with zipfile.ZipFile(self.package) as archive:
            content = {"/": archive.read("public/index.html"), "/editor.wasm": archive.read("public/editor.wasm")}

        class Response:
            status_code = 200
            def __init__(self, data=None, ready=False): self.data, self.ready = data, ready
            def json(self): return {"cloud": self.ready}
            def iter_content(self, _size): yield self.data if self.data is not None else json.dumps({"cloud": self.ready}).encode()
            def __enter__(self): return self
            def __exit__(self, *_args): return False

        for wrong in [None, b"wrong bytes", b"X" * (expected["wasm_bytes"] + 1)]:
            with self.subTest(wrong=wrong):
                paths = []
                class Client:
                    def get(self, url, **kwargs):
                        path = url.removeprefix("http://127.0.0.1:49152"); paths.append(path)
                        if path == "/api/config": return Response(ready=paths.count(path) >= 2)
                        return Response(data=wrong if wrong is not None and path == "/editor.wasm" else content[path])
                    def __enter__(self): return self
                    def __exit__(self, *_args): return False
                module = SimpleNamespace(Session=Client, RequestException=RuntimeError)
                with patch.dict(sys.modules, {"requests": module}), patch.object(gate.time, "sleep"):
                    if wrong is not None:
                        with self.assertRaisesRegex(RuntimeError, "different|exceeded"):
                            gate.serve_check("http://127.0.0.1:49152", expected)
                    else:
                        found = gate.serve_check("http://127.0.0.1:49152", expected)
                        self.assertEqual(found["served_wasm_sha256"], expected["wasm_sha256"])
                self.assertEqual(paths[:2], ["/api/config", "/api/config"])
                self.assertEqual(paths[2:], ["/", "/editor.wasm"])

    def test_entire_http_probe_has_deadline_even_if_peer_never_finishes_headers(self):
        unblock = threading.Event()
        try:
            with patch.object(gate, "HTTP_CHECK_SECONDS", .01), patch.object(gate, "_serve_check", side_effect=lambda *_args: unblock.wait(1)):
                with self.assertRaisesRegex(RuntimeError, "absolute deadline"):
                    gate.serve_check("http://127.0.0.1:49152", {})
        finally:
            unblock.set()

    def test_oversized_or_changed_fixture_cannot_execute_or_verify(self):
        self.fixture.write_bytes(b"x" * (1024 * 1024 + 1))
        with patch.object(gate, "run") as run, self.assertRaisesRegex(ValueError, "at most 1 MiB"):
            gate.main(self.args("--execute"))
        run.assert_not_called()
        self.fixture.write_bytes(b"original")
        self.fake_execution()
        self.fixture.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "exact candidate package"):
            gate.main(self.args("--verify-receipt"))

    def test_workflows_require_trusted_image_gate_before_atomic_promotion(self):
        browser = (ROOT / ".github/workflows/web-cloud.yml").read_text()
        promotion = (ROOT / ".github/workflows/update-and-release.yml").read_text()
        self.assertLess(browser.index("python packaging/web/tofu-package.py"), browser.index("  container-package:"))
        self.assertIn("  container-package:\n    needs: acceptance\n    runs-on: ubuntu-latest", browser)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha || github.sha }}", browser)
        self.assertIn("python packaging/web/check-container-package.py", browser)
        self.assertIn("--execute --candidate", browser)
        self.assertNotIn("continue-on-error:", browser)
        self.assertIn("needs: [prepare, native, browser]", promotion)
        self.assertLess(promotion.index("check-container-package.py --verify-receipt"), promotion.index("upstream-release.py promote"))
        self.assertIn("--receipt /tmp/photocraft-acceptance/container-package.json", promotion)
        self.assertIn("name: container-package-evidence", promotion)


if __name__ == "__main__":
    unittest.main()
