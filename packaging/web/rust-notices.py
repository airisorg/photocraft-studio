"""Collect exact Rust dependency notices without builds, downloads or source execution.

The selected normal/build graphs match the HEIF-enabled browser and Linux service.
Build dependencies are conservatively included; this is not a binary-link census.
Registry omissions require reviewed, hash-pinned public upstream supplements.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tomllib
from urllib.parse import urlsplit

GRAPHS = [
    ("browser", "photocraft-web", "wasm32-unknown-unknown", ["heif"]),
    ("cloud", "photocraft-cloud", "x86_64-unknown-linux-gnu", []),
]
MAX_FILE = 2 * 1024 * 1024
MAX_TOTAL = 32 * 1024 * 1024
NOTICE_NAME = re.compile(r"^(?:licen[cs]e|copying|copyright|notice)(?:[-_.].*)?$", re.I)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def cargo(root, *args):
    result = subprocess.run([os.environ.get("CARGO", "cargo"), *args], cwd=root, capture_output=True, text=True)
    if result.returncode:
        # Cargo diagnostics may include a contributor's private filesystem path.
        raise ValueError(f"Cargo notice inspection failed (exit {result.returncode}); inspect Cargo locally")
    return result.stdout


def graph_packages(root):
    lock_hash = digest((root / "Cargo.lock").read_bytes())
    metadata = json.loads(cargo(root, "metadata", "--locked", "--offline", "--format-version", "1"))
    by_key = {}
    for package in metadata["packages"]:
        by_key.setdefault((package["name"], package["version"]), []).append(package)
    selected, graphs = {}, []
    for label, package, target, features in GRAPHS:
        args = ["tree", "--locked", "--offline", "-p", package, "--target", target,
                "--edges", "normal,build", "--prefix", "none", "--format", "{p}"]
        if features:
            args.extend(["--features", ",".join(features)])
        lines = cargo(root, *args).splitlines()
        keys = set()
        for line in lines:
            match = re.match(r"^(\S+) v(\S+)(?:\s|$)", line)
            if not match:
                raise ValueError("Unrecognized Cargo graph entry")
            key = match.groups()
            matches = by_key.get(key, [])
            if len(matches) != 1:
                raise ValueError(f"Ambiguous resolved package: {key[0]} {key[1]}")
            item = matches[0]
            if item["source"] is None:
                try:
                    Path(item["manifest_path"]).resolve().relative_to(root.resolve())
                except ValueError:
                    raise ValueError("An external path dependency needs explicit notice review") from None
                continue  # Workspace notices already ship separately.
            if item["source"] != "registry+https://github.com/rust-lang/crates.io-index":
                raise ValueError(f"Unreviewed registry/source for {key[0]} {key[1]}")
            selected[key] = item
            keys.add(key)
        graphs.append({"name": label, "package": package, "target": target, "features": features,
                       "edges": ["normal", "build"], "packages": [f"{n}@{v}" for n, v in sorted(keys)]})
    if digest((root / "Cargo.lock").read_bytes()) != lock_hash:
        raise ValueError("Cargo.lock changed during notice inspection")
    return list(selected.values()), graphs, lock_hash


def safe_file(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("A notice path escaped its package")
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError("A notice path escaped its package") from None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError("A notice is missing, linked or over its size limit")
    return path


def license_files(package):
    root = Path(package["manifest_path"]).parent
    files, main_license = set(), False
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        notice = bool(NOTICE_NAME.match(path.name))
        in_licenses = any(part.lower() in {"licenses", "licences"} for part in relative.parts[:-1])
        font_notice = "fonts" in relative.parts[:-1] and path.suffix.lower() == ".txt"
        if notice or in_licenses or font_notice:
            files.add(relative.as_posix())
            main_license |= notice and len(relative.parts) == 1 or in_licenses
    if package.get("license_file"):
        files.add(package["license_file"])
        main_license = True
    return sorted(files), main_license


def registry_checksums(root, package, files):
    source_root = Path(package["manifest_path"]).parent
    # Modern Cargo caches do not retain per-file checksum manifests. Verify the
    # cached .crate against Cargo.lock and compare notice bytes to that archive;
    # never trust a locally changed unpacked license just because Cargo resolved it.
    lock = tomllib.loads((root / "Cargo.lock").read_text())
    entry = next((p for p in lock["package"] if p["name"] == package["name"] and p["version"] == package["version"]
                  and p.get("source") == package["source"]), None)
    if not entry or not entry.get("checksum"):
        raise ValueError(f"No locked registry archive digest for {package['name']}")
    base = f"{package['name']}-{package['version']}"
    archive = source_root.parents[2] / "cache" / source_root.parent.name / (base + ".crate")
    if not archive.is_file() or archive.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"Missing or over-budget cached registry archive for {package['name']}")
    checksum = hashlib.sha256()
    with archive.open("rb") as source:
        for block in iter(lambda: source.read(256 * 1024), b""):
            checksum.update(block)
    if checksum.hexdigest() != entry["checksum"]:
        raise ValueError(f"Locked registry archive checksum mismatch for {package['name']}")
    hashes = {}
    with tarfile.open(archive, "r:gz") as bundle:
        for relative in files:
            member = bundle.getmember(base + "/" + relative)
            if not member.isfile() or member.size > MAX_FILE:
                raise ValueError(f"Invalid registry notice archive member for {package['name']}")
            with bundle.extractfile(member) as source:
                hashes[relative] = digest(source.read(MAX_FILE + 1))
    return hashes


def collect(root, output, packages, graphs, lock_hash):
    supplement_root = root / "packaging/licenses"
    supplement_manifest = supplement_root / "rust-supplements.json"
    supplements = json.loads(supplement_manifest.read_text()) if supplement_manifest.exists() else []
    supplements = {(entry["package"], entry["version"]): entry for entry in supplements}
    pending, records, missing, total = [], [], [], 0
    for package in sorted(packages, key=lambda p: (p["name"], p["version"])):
        name, version = package["name"], package["version"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or not re.fullmatch(r"[A-Za-z0-9.+_-]+", version):
            raise ValueError("Invalid Cargo package name/version")
        source_root = Path(package["manifest_path"]).parent
        files, main_license = license_files(package)
        for relative in files:
            safe_file(source_root, relative)
        checksums = registry_checksums(root, package, files)
        record = {"name": name, "version": version, "license": package["license"], "source": "crates.io", "files": []}
        if package["license"] == "Apache-2.0 OR GPL-2.0-only":
            record["selectedLicense"] = "Apache-2.0"
            record["selectionReason"] = "Use the author's offered permissive alternative; preserve the original license expression and texts."
        for relative in files:
            data = safe_file(source_root, relative).read_bytes()
            checksum = digest(data)
            if checksums.get(relative) != checksum:
                raise ValueError(f"Registry notice checksum mismatch for {name} {version}")
            destination = f"{name}-{version}/registry/{relative}"
            pending.append((destination, data))
            record["files"].append({"path": destination, "sha256": checksum, "source": f"crates.io:{name}/{version}/{relative}"})
        supplement = supplements.get((name, version))
        if supplement:
            if supplement["license"] != package["license"]:
                raise ValueError(f"Supplement license changed for {name} {version}")
            if supplement.get("selectedLicense"):
                record["selectedLicense"] = supplement["selectedLicense"]
                record["selectionReason"] = supplement["selectionReason"]
            for entry in supplement["files"]:
                url = urlsplit(entry["source"])
                if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ValueError(f"Invalid public supplement provenance for {name}")
                if not re.search(r"(?:^|/)[0-9a-f]{40}(?:/|$)", url.path):
                    raise ValueError(f"Supplement source must use an immutable upstream commit for {name}")
                data = safe_file(supplement_root, entry["path"]).read_bytes()
                if digest(data) != entry["sha256"]:
                    raise ValueError(f"Supplement checksum mismatch for {name} {version}")
                destination = f"{name}-{version}/upstream/{Path(entry['path']).name}"
                pending.append((destination, data))
                provenance = {key: entry[key] for key in ("sourceLines", "sourceFileSha256", "extraction") if key in entry}
                record["files"].append({"path": destination, "sha256": entry["sha256"], "source": entry["source"], **provenance})
            main_license |= bool(supplement["files"])
        if not main_license or not package["license"]:
            missing.append(f"{name}@{version}")
        records.append(record)
    if missing:
        raise ValueError("Missing reviewed crate license texts: " + ", ".join(missing))
    destinations = set()
    for relative, data in pending:
        total += len(data)
        if not data or b"\0" in data or total > MAX_TOTAL or relative in destinations:
            raise ValueError("Invalid, duplicate or over-budget notice collection")
        destinations.add(relative)
    if output.exists() and any(output.iterdir()):
        raise ValueError("Notice output must be empty; refusing to replace unrelated files")
    output.mkdir(parents=True, exist_ok=True)
    for relative, data in pending:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    index = {"schema": 1, "cargoLockSha256": lock_hash, "graphs": graphs, "packages": records,
             "scope": "Resolved normal/build dependency notice texts, including bundled crate fonts. Build dependencies are conservatively included. This inventory excludes the Rust toolchain and container operating system packages; it is not a legal conclusion or binary-link census."}
    (output / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    (output / "README.txt").write_text(
        "Rust dependency notices\n\nExact publisher license/NOTICE texts are preserved under each package.\n"
        "index.json records package versions, offered licenses, selected target graphs, source references and file SHA-256 digests.\n"
        "Upstream supplements fill omissions in published registry archives at reviewed immutable source revisions.\n"
        "Original license alternatives remain stated; explicit selections and their reasons are recorded per package.\n"
        "Build dependencies are included conservatively. Rust toolchain and base operating-system notices are separate.\n")
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        packages, graphs, lock_hash = graph_packages(root)
        index = collect(root, args.output, packages, graphs, lock_hash)
    except (ValueError, KeyError, tarfile.TarError) as error:
        raise SystemExit(str(error)) from None
    except OSError:
        raise SystemExit("Could not read or write the locked dependency notice collection") from None
    print(json.dumps({"packages": len(index["packages"]), "graphs": {g["name"]: len(g["packages"]) for g in graphs}, "cargoLockSha256": lock_hash}))


if __name__ == "__main__":
    main()
