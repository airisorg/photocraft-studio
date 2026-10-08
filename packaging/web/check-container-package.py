"""Validate the exact release ZIP in Linux CI; default mode only prints its plan.

No Docker daemon or database is contacted without --execute. The promotion job
uses --verify-receipt to bind completed image checks to its downloaded package.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import platform
import queue
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote
import uuid
import zipfile


SPEC = importlib.util.spec_from_file_location("upstream_release", Path(__file__).with_name("upstream-release.py"))
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)
SUITES = ("test_api.py", "test_live.py", "test_live_scale.py", "test_live_handoff.py")
TRUSTED_ROOT = Path(__file__).resolve().parents[2]
LABEL = "ai.photocraft.package-check"
HTTP_CHECK_SECONDS = 95


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def inspect_package(package, candidate, destination):
    release.sha(candidate)
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        if len(entries) > 20000 or sum(e.file_size for e in entries) > 512 * 1024 * 1024:
            raise ValueError("Package exceeds the bounded CI extraction budget")
        if any(e.file_size > 64 * 1024 * 1024 for e in entries):
            raise ValueError("Package entry exceeds the CI extraction budget")
        canonical = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            normalized = path.as_posix()
            if normalized in canonical or normalized == "." or normalized != entry.filename.rstrip("/"):
                raise ValueError("Package contains colliding or noncanonical paths")
            canonical.add(normalized)
    manifest = release.unpack_package(package, destination, candidate)
    for name in SUITES:
        if not (destination / "tests/web" / name).is_file():
            raise ValueError("Package is missing a required contract suite")
    return {
        "source_commit": candidate,
        "package_sha256": digest(package),
        "wasm_path": manifest["wasm"]["path"],
        "wasm_sha256": manifest["wasm"]["sha256"],
        "wasm_bytes": (destination / manifest["wasm"]["path"]).stat().st_size,
        "index_sha256": digest(destination / "public/index.html"),
        "index_bytes": (destination / "public/index.html").stat().st_size,
        "gate_suite_sha256": {name: digest(TRUSTED_ROOT / "tests/web" / name) for name in SUITES},
        "gate_helper_sha256": {name: digest(Path(__file__).with_name(name))
                               for name in [Path(__file__).name, "upstream-release.py"]},
    }


def verify_receipt(receipt, expected):
    if receipt.get("version") != 1 or receipt.get("status") != "passed" or receipt.get("platform") != "Linux":
        raise ValueError("Container gate did not pass on Linux")
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise ValueError("Container receipt does not match the exact candidate package")
    if receipt.get("checks") != [{"name": name, "exit_code": 0} for name in SUITES]:
        raise ValueError("Container contract suites did not all pass")
    if receipt.get("served_wasm_sha256") != expected["wasm_sha256"] or receipt.get("served_index_sha256") != expected["index_sha256"]:
        raise ValueError("Container did not serve the tested browser bytes")
    if receipt.get("database_transport") != "TLS verify-full":
        raise ValueError("Release-image database TLS verification is missing")
    if receipt.get("cleanup") != {"container": True, "image": True, "database": True, "cluster": True, "context": True}:
        raise ValueError("Container check cleanup was incomplete")
    image = receipt.get("image_id", "")
    if len(image) != 71 or not image.startswith("sha256:") or any(c not in "0123456789abcdef" for c in image[7:]):
        raise ValueError("Container image identity is missing")


def write_receipt(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def run(command, *, timeout, log=None, env=None, cwd=None, check=True):
    if command[0] == "docker":
        # Never follow the operator's remote Docker context or inherited proxy.
        command = ["docker", "--host", "unix:///var/run/docker.sock", *command[1:]]
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("DOCKER_") and key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
    if log is not None:
        with log.open("w") as output:
            result = subprocess.run(command, timeout=timeout, env=env, cwd=cwd,
                                    stdout=output, stderr=subprocess.STDOUT, text=True, check=check)
    else:
        result = subprocess.run(command, timeout=timeout, env=env, cwd=cwd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=check)
    return result


def remove_owned(kind, name, token):
    found = run(["docker", kind, "inspect", "--format", '{{ index .Config.Labels "' + LABEL + '" }}', name],
                timeout=20, check=False)
    if found.returncode:
        # Distinguish absent resources from an unavailable daemon; never claim
        # successful cleanup when the daemon cannot be reached.
        run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
        return True
    if found.stdout.strip() != token:
        raise RuntimeError("Refusing to remove a Docker resource without this run's label")
    run(["docker", kind, "rm", "--force", name], timeout=30)
    return True


def database(action, name, fixture):
    # Never accept a caller-supplied database URL. This is the ephemeral CI
    # PostgreSQL service, and identifiers come exclusively from a fresh UUID.
    if not name.startswith("photocraft_image_") or len(name) != 49 or any(c not in "0123456789abcdef" for c in name[17:]):
        raise ValueError("Invalid owned database name")
    import psycopg
    from psycopg import sql
    with psycopg.connect(fixture.admin_url, autocommit=True, connect_timeout=5, hostaddr="127.0.0.1",
                         sslmode="verify-full", sslrootcert=str(fixture.ca),
                         options="-c statement_timeout=10000") as db:
        if action == "create":
            db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        elif action == "drop":
            db.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        else:
            raise ValueError("Unknown database operation")


class LocalPostgres:
    """Owned cluster only; never alter or stop a system/CI-service cluster."""
    def __init__(self):
        self.temporary = None
        self.start_attempted = False

    def start(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="photocraft-image-db-")
        folder = Path(self.temporary.name)
        self.data = folder / "data"
        self.ca = folder / "ca.crt"
        self.bindir = Path(run(["pg_config", "--bindir"], timeout=10).stdout.strip())
        for executable in ["initdb", "pg_ctl"]:
            if not (self.bindir / executable).is_file():
                raise RuntimeError("The Linux runner requires PostgreSQL initdb and pg_ctl")
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=PhotoCraft isolated CI CA", "-addext", "basicConstraints=critical,CA:TRUE",
             "-keyout", str(folder / "ca.key"), "-out", str(self.ca)], timeout=20)
        run(["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1",
             "-keyout", str(folder / "server.key"), "-out", str(folder / "server.csr")], timeout=20)
        (folder / "server.ext").write_text("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")
        run(["openssl", "x509", "-req", "-in", str(folder / "server.csr"), "-CA", str(self.ca),
             "-CAkey", str(folder / "ca.key"), "-CAcreateserial", "-days", "1",
             "-extfile", str(folder / "server.ext"), "-out", str(folder / "server.crt")], timeout=20)
        (folder / "server.key").chmod(0o600)
        run([str(self.bindir / "initdb"), "-D", str(self.data), "-A", "trust", "-U", "photocraft_test",
             "--encoding=UTF8", "--no-locale"], timeout=30)
        number = port()
        config = {"listen_addresses": "127.0.0.1", "ssl_cert_file": str(folder / "server.crt"),
                  "ssl_key_file": str(folder / "server.key"), "unix_socket_directories": str(folder)}
        with (self.data / "postgresql.conf").open("a") as stream:
            for key, value in config.items():
                stream.write("\n" + key + " = '" + value.replace("'", "''") + "'\n")
            stream.write(f"port = {number}\nssl = on\nshared_buffers = '32MB'\nmax_connections = 40\n")
        self.admin_url = f"postgresql://photocraft_test@127.0.0.1:{number}/postgres"
        self.start_attempted = True
        run([str(self.bindir / "pg_ctl"), "-D", str(self.data), "-l", str(folder / "postgres.log"),
             "-w", "-t", "30", "start"], timeout=40)

    def close(self):
        if self.temporary is None:
            return
        if self.start_attempted:
            status = run([str(self.bindir / "pg_ctl"), "-D", str(self.data), "status"], timeout=10, check=False)
            if status.returncode == 0:
                run([str(self.bindir / "pg_ctl"), "-D", str(self.data), "-m", "immediate", "-w", "-t", "10", "stop"], timeout=20)
            elif status.returncode != 3:
                raise RuntimeError("Cannot confirm owned PostgreSQL cluster shutdown")
        self.temporary.cleanup()


def port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _serve_check(origin, expected):
    import requests
    with requests.Session() as client:
        client.trust_env = False
        deadline = time.monotonic() + 60
        while True:
            try:
                # /healthz does not initialize the schema. The contract suites
                # seed rows directly, so wait for the same config readiness gate
                # as the ordinary browser/API fixture before starting them.
                body = bytearray()
                with client.get(origin + "/api/config", timeout=2, stream=True, allow_redirects=False) as response:
                    for chunk in response.iter_content(1024):
                        if len(body) + len(chunk) > 65536:
                            raise RuntimeError("Packaged container config response exceeds 64 KiB")
                        body.extend(chunk)
                    ready = response.status_code == 200 and json.loads(body).get("cloud") is True
                if ready:
                    break
            except (requests.RequestException, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("Packaged container did not become ready within 60 seconds")
            time.sleep(.1)
        observed = {}
        for path, key in [("/", "index"), ("/" + quote(expected["wasm_path"][7:]), "wasm")]:
            total, hasher = 0, hashlib.sha256()
            deadline = time.monotonic() + 30
            with client.get(origin + path, timeout=(2, 5), stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise RuntimeError("Packaged container did not serve " + key)
                for chunk in response.iter_content(65536):
                    total += len(chunk)
                    if total > expected[key + "_bytes"] or time.monotonic() > deadline:
                        raise RuntimeError("Packaged container response exceeded its exact byte/time bound")
                    hasher.update(chunk)
            if total != expected[key + "_bytes"] or hasher.hexdigest() != expected[key + "_sha256"]:
                raise RuntimeError("Packaged container served different " + key + " bytes")
            observed["served_" + key + "_sha256"] = expected[key + "_sha256"]
        return observed


def serve_check(origin, expected):
    # Requests' read timeout resets when a peer drips bytes. Bound the whole
    # probe independently; failure then stops our container in execute.finally.
    # A daemon avoids waiting forever for a malicious partial HTTP header.
    result = queue.Queue(maxsize=1)
    def probe():
        try:
            result.put((True, _serve_check(origin, expected)))
        except Exception as error:
            result.put((False, error))
    threading.Thread(target=probe, daemon=True).start()
    try:
        ok, value = result.get(timeout=HTTP_CHECK_SECONDS)
    except queue.Empty as error:
        raise RuntimeError("Packaged container HTTP probe exceeded its absolute deadline") from error
    if not ok:
        raise value
    return value


def execute(context, expected, fixture, receipt_path, receipt):
    if platform.system() != "Linux" or os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("CI") != "true":
        raise RuntimeError("--execute is restricted to the Linux GitHub CI fixture")
    if not fixture.is_file():
        raise ValueError("The native Rust fixture is required")
    if not stat.S_ISSOCK(Path("/var/run/docker.sock").stat().st_mode):
        raise RuntimeError("The isolated Linux runner's local Docker socket is required")
    token = uuid.uuid4().hex
    name = "photocraft-image-" + token
    image = name + ":check"
    db_name = "photocraft_image_" + token
    receipt.update(platform="Linux", checks=[], database_transport="TLS verify-full",
                   cleanup={"container": False, "image": False, "database": False, "cluster": False, "context": False})
    write_receipt(receipt_path, receipt)
    database_attempted = False
    build_started = False
    local_db = LocalPostgres()
    try:
        build_started = True
        run(["docker", "build", "--label", LABEL + "=" + token, "--tag", image, str(context)],
            timeout=1200, log=receipt_path.parent / "container-build.log")
        receipt["image_id"] = run(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=20).stdout.strip()
        local_db.start()
        db_url = local_db.admin_url.rsplit("/", 1)[0] + "/" + db_name
        origin = "http://127.0.0.1:" + str(port())
        # Register cleanup before CREATE: a timeout can follow a durable create.
        database_attempted = True
        database("create", db_name, local_db)
        run(["docker", "run", "--detach", "--name", name, "--label", LABEL + "=" + token,
             "--network", "host", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
             "--env", "DATABASE_URL=" + db_url, "--env", "SUPABASE_CA_CERT=" + local_db.ca.read_text(),
             "--env", "APP_ORIGIN=" + origin, "--env", "PORT=" + origin.rsplit(":", 1)[1], image], timeout=30)
        receipt.update(serve_check(origin, expected))
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}}
        # The suite fixture has no provider credentials or production endpoints.
        # Overrides are explicit; application env is never inherited by Docker.
        env.update(PHOTOCRAFT_TEST_ORIGIN=origin, PHOTOCRAFT_TEST_DATABASE_URL=db_url,
                   PHOTOCRAFT_FIXTURE=str(fixture.resolve()), PGSSLMODE="verify-full", PGSSLROOTCERT=str(local_db.ca),
                   PGHOSTADDR="127.0.0.1")
        for suite in SUITES:
            run([sys.executable, "tests/web/" + suite, "-f", "-v"], timeout=180,
                cwd=TRUSTED_ROOT, env=env, log=receipt_path.parent / ("container-" + suite + ".log"))
            receipt["checks"].append({"name": suite, "exit_code": 0})
        receipt["status"] = "passed"
    finally:
        # Attempt every cleanup independently; one failure cannot skip the rest.
        operations = []
        if build_started:
            operations.extend([("container", lambda: remove_owned("container", name, token)),
                               ("image", lambda: remove_owned("image", image, token))])
        if database_attempted:
            operations.append(("database", lambda: database("drop", db_name, local_db)))
        else:
            receipt["cleanup"]["database"] = True
        operations.append(("cluster", local_db.close))
        for key, operation in operations:
            try:
                operation()
                receipt["cleanup"][key] = True
            except Exception:
                receipt["status"] = "failed"
        write_receipt(receipt_path, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--receipt", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true")
    mode.add_argument("--verify-receipt", action="store_true")
    args = parser.parse_args(argv)
    if (args.execute or args.verify_receipt) and args.receipt is None:
        parser.error("Execution and verification require --receipt")
    if (args.execute or args.verify_receipt) and args.fixture is None:
        parser.error("Execution and receipt verification require --fixture")
    if not args.execute and not args.verify_receipt:
        release.sha(args.candidate)
        print(json.dumps({"mode": "plan", "package": str(args.package), "candidate": args.candidate,
                          "validation": "not performed", "steps": ["validate exact ZIP", "build isolated image",
                          "start owned TLS PostgreSQL", "serve index/WASM bytes", *SUITES, "remove owned resources"]}, indent=2))
        return
    receipt = {"version": 1, "status": "failed"}
    context = None
    try:
        with tempfile.TemporaryDirectory(prefix="photocraft-image-context-") as temporary:
            context = Path(temporary)
            expected = inspect_package(args.package.resolve(), args.candidate, context)
            if not args.fixture.is_file() or args.fixture.is_symlink() or not 0 < args.fixture.stat().st_size <= 1024 * 1024:
                raise ValueError("Native CI fixture must be a regular file of at most 1 MiB")
            expected.update(fixture_sha256=digest(args.fixture), fixture_bytes=args.fixture.stat().st_size)
            if args.verify_receipt:
                verify_receipt(json.loads(args.receipt.read_text()), expected)
                print("Exact package container receipt verified")
            elif args.execute:
                receipt.update(expected)
                execute(context, expected, args.fixture, args.receipt, receipt)
        if args.execute:
            receipt["cleanup"]["context"] = True
            verify_receipt(receipt, expected)
    except BaseException:
        receipt["status"] = "failed"
        raise
    finally:
        if args.execute:
            if context is not None and "cleanup" in receipt:
                receipt["cleanup"]["context"] = not context.exists()
            write_receipt(args.receipt, receipt)


if __name__ == "__main__":
    def interrupted(_signal, _frame):
        raise RuntimeError("Container package check interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    main()
