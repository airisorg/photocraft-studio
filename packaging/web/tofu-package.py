"""Package tracked source and built WASM without local caches, secrets or Git metadata."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile

root = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1]).resolve()
out.parent.mkdir(parents=True, exist_ok=True)
assets = root / 'dist/web'
wasm = list(assets.glob('*.wasm'))
# This container serves the upstream 0.3 editor including HEIF. Its measured binary is
# about 24.6 MiB; Cloudflare's separate distribution keeps its own 24 MiB gate.
if len(wasm) != 1 or wasm[0].stat().st_size > 26 * 1024 * 1024:
    raise SystemExit('Expected one release WASM within the Tofu container 26 MiB budget')
source = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root).decode().split('\0')
manifest = {
    'sourceCommit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip(),
    'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, text=True).strip()),
    'wasm': {'path': 'public/' + wasm[0].name, 'sha256': hashlib.sha256(wasm[0].read_bytes()).hexdigest()},
}
# Generate from the resolved, locked registry graph before opening the archive.
# Missing or modified license texts stop redistribution instead of silently omitting notices.
with tempfile.TemporaryDirectory(prefix="photocraft-rust-notices-") as temporary:
    notices = Path(temporary) / "notices"
    subprocess.run([sys.executable, str(root / "packaging/web/rust-notices.py"), "--output", str(notices)], check=True)
    runtime = Path(temporary) / "runtime"
    subprocess.run([sys.executable, str(root / "packaging/web/runtime-notices.py"), "--wasm", str(wasm[0]), "--output", str(runtime)], check=True)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(set(source)):
            path = root / name
            if not name or not path.is_file() or path.is_symlink():
                continue
            if any(part.startswith('.env') or part in {'.git', 'node_modules', 'target', 'corpus', 'test-results'} for part in Path(name).parts):
                continue
            archive.write(path, name)
        for path in sorted(assets.rglob('*')):
            if path.is_file() and path.suffix in {'.html', '.js', '.wasm', '.svg', '.png', '.pcraft'}:
                archive.write(path, 'public/'+path.relative_to(assets).as_posix())
        archive.writestr('release-source.json', json.dumps(manifest, indent=2) + '\n')
        # The browser receives public/, not the source tree in the container. Ship
        # every embedded-asset notice at its documented relative path there too.
        for name in ['LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE', 'ATTRIBUTION.md', 'SECURITY.md',
                     'assets/fonts/OFL-Inter.txt', 'assets/fonts/OFL-JetBrainsMono.txt',
                     'assets/icons/LICENSE-lucide.txt', 'assets/dict/LICENSE-SCOWL.txt',
                     'assets/app-icon/LICENSE.txt', 'crates/ui-egui/src/i18n/LICENSE-translations.txt',
                     'docs/brand/LICENSE-brand.txt']:
            archive.write(root/name, 'public/'+name)
        for path in sorted(notices.rglob("*")):
            if path.is_file():
                archive.write(path, "public/rust-notices/" + path.relative_to(notices).as_posix())
        for path in sorted(runtime.rglob("*")):
            if path.is_file():
                archive.write(path, "public/runtime-notices/" + path.relative_to(runtime).as_posix())
print(f'{out}: {out.stat().st_size:,} bytes; WASM {wasm[0].stat().st_size:,} bytes')
