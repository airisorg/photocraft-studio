#!/usr/bin/env bash
# Build the unchanged engine plus thin web shell; stage only public browser artifacts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
python3 "$ROOT/packaging/web/build-release.py"
python3 "$ROOT/packaging/web/tofu-package.py" "${1:-$ROOT/dist/photocraft-tofu.zip}"
