"""Read-only publication gate; never print discovered identifiers or secret text."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)


def history(root, ref=None):
    objects = {}
    revisions = [ref] if ref else ["--all"]
    for line in git(root, "rev-list", "--objects", *revisions).splitlines():
        fields = line.split(b" ", 1)
        if len(fields) == 2:
            objects.setdefault(fields[0], set()).add(fields[1])
    restricted = set()
    home_blobs = 0
    text_blobs = 0
    home = re.compile(rb"(?:/Users/|/home/)[A-Za-z0-9_.-]{1,128}/")
    process = subprocess.Popen(["git", "cat-file", "--batch"], cwd=root,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
    try:
        for oid, paths in objects.items():
            restricted.update(path for path in paths if path.startswith(b"docs/brand/")
                              and path.rsplit(b".", 1)[-1].lower() in {b"svg", b"png", b"jpg", b"jpeg", b"webp"})
            process.stdin.write(oid+b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            size = int(header[2])
            remaining = size
            binary = False
            found_home = False
            tail = b""
            while remaining:
                chunk = process.stdout.read(min(remaining, 64*1024))
                if not chunk:
                    raise RuntimeError("Truncated Git object stream")
                remaining -= len(chunk)
                binary |= b"\0" in chunk
                combined = tail+chunk
                found_home |= bool(home.search(combined))
                tail = combined[-256:]
            process.stdout.read(1)
            if header[1] == b"blob" and not binary:
                text_blobs += 1
                home_blobs += found_home
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    if process.returncode:
        raise RuntimeError("Git object scan failed")
    identities = set(git(root, "log", *revisions, "--format=%ae").splitlines())
    identities = set(git(root, "log", "--all", "--format=%ae").splitlines())
    return {"textBlobCount": text_blobs, "personalHomePathBlobCount": home_blobs,
            "restrictedBrandPathCount": len(restricted), "authorIdentityCountForManualReview": len(identities)}


def secrets(root):
    tool = shutil.which("gitleaks")
    if tool is None:
        return {"status": "unavailable", "findings": None}
    with tempfile.TemporaryDirectory() as temporary:
        report = Path(temporary)/"redacted.json"
        result = subprocess.run([tool, "git", str(root), "--log-opts=--all", "--redact=100",
                                 "--no-banner", "--no-color", "--report-format=json",
                                 "--report-path", str(report), "--timeout=180"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=200)
        if result.returncode not in {0, 1}:
            return {"status": "error", "findings": None}
        found = json.loads(report.read_text()) if report.exists() else []
        return {"status": "checked", "findings": len(found)}


def check(root):
    root = Path(root).resolve()
    report = {"sourceCommit": git(root, "rev-parse", "HEAD").decode().strip(),
              "dirty": bool(git(root, "status", "--porcelain")),
              "scope": "All locally reachable refs; values are intentionally redacted",
              "history": history(root), "secrets": secrets(root)}
    required = ["LICENSE-MIT", "LICENSE-APACHE", "NOTICE", "ATTRIBUTION.md", "SECURITY.md",
                "assets/fonts/OFL-Inter.txt", "assets/fonts/OFL-JetBrainsMono.txt",
                "assets/icons/LICENSE-lucide.txt", "assets/dict/LICENSE-SCOWL.txt"]
    report["missingNoticeCount"] = sum(not (root/name).is_file() for name in required)
    report["passed"] = (not report["dirty"] and report["missingNoticeCount"] == 0
                        and report["history"]["personalHomePathBlobCount"] == 0
                        and report["history"]["restrictedBrandPathCount"] == 0
                        and report["secrets"] == {"status": "checked", "findings": 0})
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ref", help="Full candidate SHA; default inspects every local ref")
    args = parser.parse_args()
    result = check(args.root, args.ref)
    args = parser.parse_args()
    result = check(args.root)
    encoded = json.dumps(result, indent=2)+"\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    raise SystemExit(0 if result["passed"] else 1)
