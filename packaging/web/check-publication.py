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
    return {"textBlobCount": text_blobs, "personalHomePathBlobCount": home_blobs,
            "restrictedBrandPathCount": len(restricted), "authorIdentityCountForManualReview": len(identities)}


def secrets(root, ref=None):
    tool = shutil.which("gitleaks")
    if tool is None:
        return {"status": "unavailable", "findings": None}
    root = Path(root).resolve()
    # Gitleaks also loads <target>/.gitleaksignore even with an explicit ignore
    # flag. Git history is available from its administrative directory without
    # exposing the candidate checkout's policy files to that implicit lookup.
    git_dir = git(root, "rev-parse", "--absolute-git-dir").decode().strip()
    with tempfile.TemporaryDirectory(prefix="photocraft-publication-policy-") as temporary:
        policy = Path(temporary)
        report = policy/"redacted.json"
        config = policy/"trusted.toml"
        ignore = policy/"empty.ignore"
        # The candidate is data, including its scanner configuration and comments.
        # Only Gitleaks' embedded default rules may define this publication gate.
        config.write_text("[extend]\nuseDefault = true\n")
        ignore.write_text("")
        environment = {key: value for key, value in os.environ.items()
                       if key not in {"GITLEAKS_CONFIG", "GITLEAKS_CONFIG_TOML"}}
        # Ordinary `git log -p` omits merge diffs. Inspect each parent and force
        # raw text so merge resolutions or diff attributes cannot hide content.
        log_opts = "--diff-merges=separate --text --no-ext-diff --no-textconv " + (ref or "--all")
        result = subprocess.run([tool, "git", git_dir, "--log-opts=" + log_opts, "--redact=100",
                                 "--config", str(config), "--gitleaks-ignore-path", str(ignore),
                                 "--ignore-gitleaks-allow",
                                 "--no-banner", "--no-color", "--report-format=json",
                                 "--report-path", str(report), "--timeout=180"],
                                cwd=policy, env=environment,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=200)
        if result.returncode not in {0, 1}:
            return {"status": "error", "findings": None}
        try:
            found = json.loads(report.read_text())
        except (OSError, ValueError):
            return {"status": "error", "findings": None}
        if not isinstance(found, list) or bool(found) != (result.returncode == 1):
            return {"status": "error", "findings": None}
        return {"status": "checked", "findings": len(found)}


def check(root, ref=None):
    root = Path(root).resolve()
    if ref is not None and not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise ValueError("Publication ref must be a full commit SHA")
    report = {"sourceCommit": git(root, "rev-parse", ref or "HEAD").decode().strip(),
              "dirty": bool(git(root, "status", "--porcelain")),
              "scope": ("Exact candidate and its ancestors" if ref else "All locally reachable refs") + "; values are intentionally redacted",
              "history": history(root, ref), "secrets": secrets(root, ref)}
    required = ["LICENSE-MIT", "LICENSE-APACHE", "NOTICE", "ATTRIBUTION.md", "SECURITY.md",
                "assets/fonts/OFL-Inter.txt", "assets/fonts/OFL-JetBrainsMono.txt",
                "assets/icons/LICENSE-lucide.txt", "assets/dict/LICENSE-SCOWL.txt"]
    if ref:
        # A tree/symlink at the right name is not a distributable notice file.
        entries = [git(root, "ls-tree", ref, "--", name).split() for name in required]
        report["missingNoticeCount"] = sum(not entry or entry[0] not in {b"100644", b"100755"}
                                          or entry[1] != b"blob" for entry in entries)
    else:
        report["missingNoticeCount"] = sum(not (root/name).is_file() or (root/name).is_symlink()
                                          for name in required)
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
    encoded = json.dumps(result, indent=2)+"\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")
    raise SystemExit(0 if result["passed"] else 1)
