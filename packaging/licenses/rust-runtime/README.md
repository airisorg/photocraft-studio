# Rust runtime notices

This is a conservative notice supplement for Rust 1.95.0, commit
`59807616e1fa2540724bfbac14d7976d7e4a3860`, used by the browser WASM and Linux
cloud service. It complements the Cargo dependency bundle; it is not a per-symbol
linker census or a claim covering every native installer or operating system.

`COPYRIGHT-library.html` and `licenses/` are exact files from the official
Rust 1.95.0 Linux compiler distribution. The downloaded archive was checked
against its official release-manifest SHA-256 before selectively reading these
files. They also match the installed macOS 1.95.0 toolchain byte for byte. Rust's
pinned `COPYRIGHT` explains why this subset covers the standard library rather
than distributing the much larger notice inventory for the compiler executable.
The library document contains dependency license texts and actual copyright
attributions; generic SPDX license texts retain their original placeholders.

The pinned compiler-builtins license explicitly refers to libm's notice and
compiler-rt contributors. Their unmodified notices are included separately.
The compiler-rt source revision is the LLVM submodule commit recorded by this
exact Rust release, not the current LLVM main branch. All origins, archive
members, byte counts and SHA-256 digests are in `manifest.json`. Inclusion of
target-specific alternatives does not mean every listed component is linked.

`packaging/web/runtime-notices.py` verifies these bytes offline and checks the
actual browser artifact's WASM `producers` section. A missing or different rustc
version/commit stops packaging. The Docker build separately checks the exact
compiler release and full commit before building the Linux service. Updating the
compiler requires a reviewed refresh of this bundle and those checks.

The distributed files live under `public/runtime-notices/`, with `index.json`
binding the verified manifest to the WASM digest. Debian image contents,
dynamically linked system libraries, build tools themselves and other native
platform distributions remain separate inventories. No provider credentials or
local filesystem paths belong in this bundle.
