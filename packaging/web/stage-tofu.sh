#!/usr/bin/env bash
# Build the unchanged engine plus thin web shell; stage only public browser artifacts.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
(cd "$ROOT/apps/photocraft-web" && NO_COLOR=true trunk build --release)
python3 "$ROOT/packaging/web/tofu-package.py" "${1:-$ROOT/dist/photocraft-tofu.zip}"
