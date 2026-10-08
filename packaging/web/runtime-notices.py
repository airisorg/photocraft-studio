"""Verify and stage pinned Rust runtime notices offline, using actual WASM provenance.

This supplements Cargo dependency notices; it is not a linker or base-image census.
No build, network request or installed-toolchain lookup is performed.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


MAX_WASM = 32 * 1024 * 1024
MAX_FILE = 1024 * 1024
MAX_BUNDLE = 2 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def take(self, length):
        end = self.offset + length
        if end > len(self.data):
            raise ValueError("Truncated WASM metadata")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def integer(self):
        value = 0
        for shift in range(0, 35, 7):
            byte = self.take(1)[0]
            value |= (byte & 127) << shift
            if byte < 128:
                if value > 0xffffffff:
                    break
                return value
        raise ValueError("Invalid WASM metadata integer")

    def string(self):
        size = self.integer()
        if size > 4096:
            raise ValueError("Oversized WASM metadata string")
        try:
            return self.take(size).decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Invalid WASM metadata text") from None


def wasm_compiler(data):
    reader = Reader(data)
    if len(data) > MAX_WASM or reader.take(8) != b"\0asm\x01\0\0\0":
        raise ValueError("Expected bounded WebAssembly version1 artifact")
    compilers, producers, sections = [], 0, 0
    while reader.offset < len(data):
        sections += 1
        if sections > 128:
            raise ValueError("Too many WASM sections for notice verification")
        kind = reader.take(1)[0]
        section = Reader(reader.take(reader.integer()))
        if kind != 0 or section.string() != "producers":
            continue
        producers += 1
        fields = section.integer()
        if producers > 1 or fields > 16:
            raise ValueError("Ambiguous WASM producers metadata")
        for _ in range(fields):
            field, count = section.string(), section.integer()
            if count > 64:
                raise ValueError("Oversized WASM producers metadata")
            for _ in range(count):
                name, version = section.string(), section.string()
                if field == "processed-by" and name == "rustc":
                    compilers.append(version)
        if section.offset != len(section.data):
            raise ValueError("Trailing WASM producers metadata")
    if len(compilers) != 1:
        raise ValueError("Expected exactly one rustc producer in actual WASM")
    return compilers[0]


def collect(bundle, wasm, output):
    manifest_path = bundle / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > MAX_FILE:
        raise ValueError("Invalid runtime notice manifest")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest["schema"] != 1:
        raise ValueError("Unsupported runtime notice manifest")
    if wasm.is_symlink() or wasm.stat().st_size > MAX_WASM:
        raise ValueError("Invalid WASM artifact for runtime notice verification")
    wasm_bytes = wasm.read_bytes()
    compiler = wasm_compiler(wasm_bytes)
    if compiler != manifest["rust"]["wasmProducer"]:
        raise ValueError("WASM rustc producer differs from pinned runtime notices; refresh the verified bundle")
    verified, total = {}, 0
    for item in manifest["files"]:
        relative = PurePosixPath(item["path"])
        if relative.is_absolute() or ".." in relative.parts or "\\" in str(relative) or str(relative) in verified:
            raise ValueError("Unsafe or duplicate runtime notice path")
        path = bundle / str(relative)
        if not path.resolve().is_relative_to(bundle.resolve()) or path.is_symlink() or path.stat().st_size > MAX_FILE:
            raise ValueError("Invalid runtime notice file")
        data = path.read_bytes()
        total += len(data)
        if total > MAX_BUNDLE or len(data) != item["bytes"] or digest(data) != item["sha256"]:
            raise ValueError("Runtime notice bytes differ from verified provenance: " + str(relative))
        verified[str(relative)] = data
    if not verified:
        raise ValueError("Empty runtime notice bundle")
    # Validate everything before creating output, so a failed check cannot produce
    # a partially valid distribution. Tofu packaging uses a fresh temporary path.
    output.mkdir(parents=True, exist_ok=False)
    for relative, data in verified.items():
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    index = {**manifest, "manifestSha256": digest(manifest_bytes),
             "browserArtifact": {"sha256": digest(wasm_bytes), "rustcProducer": compiler}}
    (output / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wasm", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parents[1] / "licenses/rust-runtime"
    try:
        index = collect(bundle, args.wasm, args.output)
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Runtime notice verification failed; inspect pinned bundle and actual WASM producers locally") from None
    print(json.dumps({"runtimeNoticeFiles": len(index["files"]), "rustcProducer": index["browserArtifact"]["rustcProducer"]}))


if __name__ == "__main__":
    main()
