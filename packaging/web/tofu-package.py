"""Package tracked source and built WASM without local caches, secrets or Git metadata."""
from pathlib import Path
import subprocess
import sys
import zipfile

root = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1]).resolve()
out.parent.mkdir(parents=True, exist_ok=True)
assets = root / 'dist/web'
wasm = list(assets.glob('*.wasm'))
if len(wasm) != 1 or wasm[0].stat().st_size > 24 * 1024 * 1024:
    raise SystemExit('Expected one release WASM within the upstream 24 MiB budget')
source = subprocess.check_output(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], cwd=root).decode().split('\0')
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
    for name in sorted(set(source)):
        path = root / name
        if not name or not path.is_file() or path.is_symlink():
            continue
        if any(part.startswith('.env') or part in {'.git', 'node_modules', 'target', 'corpus', 'test-results'} for part in Path(name).parts):
            continue
        archive.write(path, name)
    for path in sorted(assets.iterdir()):
        if path.is_file() and path.suffix in {'.html', '.js', '.wasm', '.svg', '.png'}:
            archive.write(path, 'public/'+path.name)
    for name in ['LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE']:
        archive.write(root/name, 'public/'+name)
print(f'{out}: {out.stat().st_size:,} bytes; WASM {wasm[0].stat().st_size:,} bytes')
