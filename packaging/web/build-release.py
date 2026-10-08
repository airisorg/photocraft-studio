"""Build the browser release with compiler path privacy, preserving caller flags.

Run from any directory: python3 packaging/web/build-release.py
The existing Trunk config, toolchain, Cargo cache and target directory are retained.
CARGO_ENCODED_RUSTFLAGS takes precedence, including an explicitly empty value.
Otherwise RUSTFLAGS follows pinned Cargo 1.95: split on literal spaces, trim each
piece and discard empty pieces. Quotes stay literal, even unmatched ones; rustc
validates its arguments. There is no shell parsing or expansion. Use encoded flags
for arguments containing spaces. Privacy remaps are appended so they take priority.
As with Cargo's RUSTFLAGS, encoded environment flags override target-specific Cargo
configuration flags; supply any required target flags in the caller environment.
"""
import os
from pathlib import Path
import subprocess


SEPARATOR = "\x1f"
# Unicode White_Space used by Rust str::trim; Python also strips several ASCII
# record separators, so its default strip() would subtly change caller arguments.
RUST_WHITESPACE = " \t\n\v\f\r\x85\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000"


def build_environment(root, inherited, home):
    root, home = Path(root).resolve(), Path(home).resolve()
    working = root / "apps/photocraft-web"
    environment = dict(inherited)
    if "CARGO_ENCODED_RUSTFLAGS" in environment:
        encoded = environment["CARGO_ENCODED_RUSTFLAGS"]
        flags = encoded.split(SEPARATOR) if encoded else []
    else:
        # Cargo rust-1.95.0, target_info.rs::rustflags_from_env: split(' '), trim,
        # filter empty. Shell parsing would corrupt valid --cfg feature="browser".
        # https://github.com/rust-lang/cargo/blob/rust-1.95.0/src/cargo/core/compiler/build_context/target_info.rs#L785-L792
        plain = environment.get("RUSTFLAGS", "")
        if SEPARATOR in plain:
            raise ValueError("Plain Rust flags cannot contain the encoded argument separator")
        flags = [trimmed for part in plain.split(" ") if (trimmed := part.strip(RUST_WHITESPACE))]

    def location(variable, default):
        value = environment.get(variable)
        path = Path(value).expanduser() if value else default
        return (path if path.is_absolute() else working / path).resolve()

    paths = [
        (home, "/operator-home"),
        (root, "/source"),
        (location("CARGO_HOME", home / ".cargo"), "/cargo-home"),
        (location("RUSTUP_HOME", home / ".rustup"), "/rustup-home"),
        (location("CARGO_TARGET_DIR", root / "target"), "/build"),
    ]
    # Rust applies the last matching prefix. Put longer, more specific paths last.
    # A path configured for multiple roles is ambiguous; reject instead of hiding it.
    if len({str(path) for path, _ in paths}) != len(paths):
        raise ValueError("Build path roles must use distinct directories")
    for path, replacement in sorted(paths, key=lambda entry: len(str(entry[0]))):
        source = str(path)
        if any(character in source for character in (SEPARATOR, "=", "\n", "\r")):
            raise ValueError("Build path cannot be represented safely in a compiler remap")
        flags.append("--remap-path-prefix=" + source + "=" + replacement)
    environment["CARGO_ENCODED_RUSTFLAGS"] = SEPARATOR.join(flags)
    environment.pop("RUSTFLAGS", None)
    environment["NO_COLOR"] = "true"
    return environment


def main():
    root = Path(__file__).resolve().parents[2]
    try:
        environment = build_environment(root, os.environ, Path.home())
    except ValueError:
        raise SystemExit("Invalid build paths or Rust flags; no compiler was started") from None
    result = subprocess.run(["trunk", "build", "--release"],
                            cwd=root / "apps/photocraft-web", env=environment)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
