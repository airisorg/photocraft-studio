"""Install the pinned Linux x64 publication scanner; never execute downloaded code.

Official v8.30.0 checksums.txt and the GitHub release-asset digest agree on SHA256.
Only the single verified regular binary is written, into a new caller-owned directory.
"""
import argparse
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile


VERSION = "8.30.0"
URL = "https://github.com/gitleaks/gitleaks/releases/download/v8.30.0/gitleaks_8.30.0_linux_x64.tar.gz"
SHA256 = "79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e"
MAX_ARCHIVE = 16 * 1024 * 1024
MAX_BINARY = 32 * 1024 * 1024


def download():
    # -q must be first: do not accept ~/.curlrc or proxy/config credentials.
    # Curl bounds the whole transfer (including headers), not just each read.
    command = ["curl", "-q", "--proto", "=https", "--proto-redir", "=https", "--tlsv1.2",
               "--fail", "--silent", "--show-error", "--location", "--connect-timeout", "15",
               "--max-time", "90", "--max-filesize", str(MAX_ARCHIVE), URL]
    environment = {key: value for key, value in os.environ.items() if key in {"PATH", "LANG", "LC_ALL"}}
    process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    data = bytearray()
    try:
        while chunk := process.stdout.read(65536):
            if len(data) + len(chunk) > MAX_ARCHIVE:
                raise ValueError("Scanner download exceeded its size bound")
            data.extend(chunk)
        if process.wait(timeout=5) != 0:
            raise ValueError("Scanner download failed")
        return bytes(data)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()


def binary_from_archive(data):
    if len(data) > MAX_ARCHIVE or hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("Scanner archive checksum mismatch")
    binary = None
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for count, entry in enumerate(archive, 1):
            path = PurePosixPath(entry.name)
            total += entry.size
            if (count > 16 or total > 40 * 1024 * 1024 or not entry.isfile()
                    or path.is_absolute() or ".." in path.parts or "\\" in entry.name
                    or path.as_posix() != entry.name or entry.size > MAX_BINARY):
                raise ValueError("Unsafe scanner archive member")
            if entry.name == "gitleaks":
                if binary is not None:
                    raise ValueError("Duplicate scanner binary")
                binary = archive.extractfile(entry).read(MAX_BINARY + 1)
                if len(binary) != entry.size:
                    raise ValueError("Truncated scanner binary")
    if not binary or not binary.startswith(b"\x7fELF"):
        raise ValueError("Scanner archive has no Linux executable")
    return binary


def install(destination):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Scanner destination must not already exist")
    binary = binary_from_archive(download())
    destination.mkdir(mode=0o700)
    try:
        path = destination / "gitleaks"
        with path.open("xb") as output:
            output.write(binary)
        path.chmod(0o700)
    except BaseException:
        # This function created the directory; never remove a preexisting path.
        shutil.rmtree(destination)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    try:
        install(args.destination)
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError):
        raise SystemExit("Verified scanner installation failed; no unverified binary was installed") from None
    print("Installed verified Gitleaks " + VERSION + " (Linux x64)")


if __name__ == "__main__":
    main()
