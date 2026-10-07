"""Package tracked source and built WASM without local caches, secrets or Git metadata."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
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
    'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no'], cwd=root, text=True).strip()),
    'wasm': {'path': 'public/' + wasm[0].name, 'sha256': hashlib.sha256(wasm[0].read_bytes()).hexdigest()},
}
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
    for name in ['LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE']:
        archive.write(root/name, 'public/'+name)
print(f'{out}: {out.stat().st_size:,} bytes; WASM {wasm[0].stat().st_size:,} bytes')
