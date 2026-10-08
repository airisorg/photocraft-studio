"""Exercise update/promotion safety using disposable Git repos, without GitHub or Tofu."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

SPEC = importlib.util.spec_from_file_location('upstream_release', Path(__file__).resolve().parents[2] / 'packaging/web/upstream-release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class UpstreamRelease(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.old = Path.cwd()
        self.addCleanup(os.chdir, self.old)
        self.source = self.root / 'source'
        self.origin = self.root / 'origin.git'
        self.upstream = self.root / 'upstream'
        release.git('init', '-b', 'main', str(self.source))
        os.chdir(self.source)
        self.identity()
        self.commit('engine.rs', 'original engine')
        release.git('clone', str(self.source), str(self.upstream))
        release.git('init', '--bare', str(self.origin))
        release.git('remote', 'add', 'origin', str(self.origin))
        self.commit('adapter.rs', 'our browser and sharing adapter')
        release.git('push', 'origin', 'main')
        self.base = self.head()
        with self.at(self.upstream):
            self.identity()
            self.commit('engine.rs', 'upstream improvement')

    @contextlib.contextmanager
    def at(self, path):
        previous = Path.cwd()
        os.chdir(path)
        try:
            yield
        finally:
            os.chdir(previous)

    def identity(self):
        release.git('config', 'user.name', 'Test')
        release.git('config', 'user.email', 'test@example.invalid')

    def commit(self, path, content):
        Path(path).write_text(content)
        release.git('add', path)
        release.git('commit', '-m', 'Fixture change')

    def head(self):
        return release.git('rev-parse', 'HEAD').stdout.strip()

    def prepare(self):
        return release.prepare('automation/upstream-test', str(self.upstream))

    def remote_head(self, branch='main'):
        return release.git('ls-remote', 'origin', 'refs/heads/' + branch).stdout.split()[0]

    def package(self, info, *, dirty=False, digest=None, extra=None):
        package = self.root / 'package.zip'
        wasm = b'\0asm-test-fixture'
        manifest = dict(sourceCommit=info['candidate'], dirty=dirty,
                        wasm=dict(path='public/editor.wasm', sha256=digest or hashlib.sha256(wasm).hexdigest()))
        with zipfile.ZipFile(package, 'w') as archive:
            for name, data in {'release-source.json': json.dumps(manifest), 'public/editor.wasm': wasm,
                               'public/index.html': '<canvas>', 'Dockerfile': 'FROM scratch',
                               'Cargo.toml': 'fixture', 'Cargo.lock': 'fixture'}.items():
                archive.writestr(name, data)
            if extra:
                archive.writestr(*extra)
        return package

    def promote(self, info, package=None):
        release.promote(**{k: v for k, v in info.items() if k != 'needed'}, package=package or self.package(info))

    def test_merge_preserves_adapter_and_promotion_keeps_exact_source(self):
        info = self.prepare()
        self.assertEqual(Path('engine.rs').read_text(), 'upstream improvement')
        self.assertEqual(Path('adapter.rs').read_text(), 'our browser and sharing adapter')
        self.assertEqual(self.remote_head(), self.base, 'Preparation must not modify main')
        self.promote(info)
        self.assertEqual(self.remote_head(), info['candidate'])
        release.git('fetch', 'origin', 'tofu-release')
        manifest = json.loads(release.git('show', 'FETCH_HEAD:release-source.json').stdout)
        self.assertEqual(manifest['sourceCommit'], info['candidate'])
        self.assertEqual(manifest['upstreamCommit'], info['upstream'])
        self.assertEqual(self.prepare()['needed'], 'false', 'An unchanged schedule must not redeploy')

    def test_later_release_is_fast_forward_and_removes_obsolete_files(self):
        first = self.prepare()
        self.promote(first, self.package(first, extra=('obsolete.txt', 'old')))
        previous = self.remote_head('tofu-release')
        with self.at(self.upstream):
            self.commit('engine.rs', 'second upstream improvement')
        second = self.prepare()
        self.promote(second)
        release.git('fetch', 'origin', 'tofu-release')
        release.git('merge-base', '--is-ancestor', previous, 'FETCH_HEAD')
        self.assertNotEqual(release.git('show', 'FETCH_HEAD:obsolete.txt', check=False).returncode, 0)

    def test_conflict_stops_before_main_or_release_changes(self):
        self.commit('engine.rs', 'incompatible adapter edit')
        release.git('push', 'origin', 'main')
        current = self.head()
        with self.assertRaisesRegex(RuntimeError, 'conflicts'):
            self.prepare()
        self.assertEqual(self.remote_head(), current)
        self.assertEqual(release.git('status', '--porcelain').stdout, '')
        self.assertEqual(release.git('ls-remote', 'origin', 'refs/heads/tofu-release').stdout, '')

    def test_main_change_during_acceptance_blocks_promotion(self):
        info = self.prepare()
        release.git('checkout', '--detach', self.base)
        self.commit('new-work.txt', 'another person changed main')
        release.git('push', 'origin', 'HEAD:main')
        current = self.head()
        with self.assertRaisesRegex(ValueError, 'Main changed'):
            self.promote(info)
        self.assertEqual(self.remote_head(), current)

    def test_dirty_or_mismatched_package_never_promotes(self):
        info = self.prepare()
        for changes in [{'dirty': True}, {'digest': 'wrong'}, {'extra': ('../escape', 'unsafe')}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.promote(info, self.package(info, **changes))
            self.assertEqual(self.remote_head(), self.base)
        wrong = dict(info, candidate=self.base)
        with self.assertRaisesRegex(ValueError, 'clean tested candidate'):
            self.promote(info, self.package(wrong))

    def test_dirty_checkout_and_unreserved_branch_are_rejected(self):
        Path('unfinished.rs').write_text('work in progress')
        with self.assertRaisesRegex(ValueError, 'clean checkout'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'reserved'):
            release.prepare('main', str(self.upstream))

    def test_packaged_untracked_source_cannot_claim_a_clean_commit(self):
        script = self.source / 'packaging/web/tofu-package.py'
        script.parent.mkdir(parents=True)
        self.commit(str(script.relative_to(self.source)), Path(SPEC.origin).with_name('tofu-package.py').read_text())
        self.commit('packaging/web/rust-notices.py', Path(SPEC.origin).with_name('rust-notices.py').read_text())
        self.commit('packaging/web/runtime-notices.py', Path(SPEC.origin).with_name('runtime-notices.py').read_text())
        runtime_source = Path(SPEC.origin).parents[1] / 'licenses/rust-runtime'
        for path in sorted(runtime_source.rglob('*')):
            if path.is_file():
                destination = 'packaging/licenses/rust-runtime/' + path.relative_to(runtime_source).as_posix()
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                self.commit(destination, path.read_text())
        self.commit('.gitignore', 'dist/\n')
        registry = self.root / 'registry/src/synthetic-index/synthetic-dependency-1.0.0'
        registry.mkdir(parents=True)
        license_bytes = b'MIT License\nCopyright (c) Synthetic test publisher\n'
        (registry / 'LICENSE-MIT').write_bytes(license_bytes)
        cached = self.root / 'registry/cache/synthetic-index/synthetic-dependency-1.0.0.crate'
        cached.parent.mkdir(parents=True)
        with tarfile.open(cached, 'w:gz') as archive:
            member = tarfile.TarInfo('synthetic-dependency-1.0.0/LICENSE-MIT')
            member.size = len(license_bytes)
            archive.addfile(member, io.BytesIO(license_bytes))
        self.commit('Cargo.lock', '[[package]]\nname="synthetic-dependency"\nversion="1.0.0"\nsource="registry+https://github.com/rust-lang/crates.io-index"\nchecksum="'+hashlib.sha256(cached.read_bytes()).hexdigest()+'"\n')
        metadata = {'packages': [{'name': 'synthetic-dependency', 'version': '1.0.0', 'license': 'MIT', 'license_file': None,
                                 'manifest_path': str(registry / 'Cargo.toml'), 'source': 'registry+https://github.com/rust-lang/crates.io-index'}]}
        cargo = self.root / 'synthetic-cargo'
        cargo.write_text('#!' + sys.executable + '\nimport sys\nprint(' + repr(json.dumps(metadata)) + " if sys.argv[1] == 'metadata' else 'synthetic-dependency v1.0.0')\n")
        cargo.chmod(0o755)
        notices = ['LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE', 'ATTRIBUTION.md', 'SECURITY.md',
                   'assets/fonts/OFL-Inter.txt', 'assets/fonts/OFL-JetBrainsMono.txt',
                   'assets/icons/LICENSE-lucide.txt', 'assets/dict/LICENSE-SCOWL.txt',
                   'assets/app-icon/LICENSE.txt', 'crates/ui-egui/src/i18n/LICENSE-translations.txt',
                   'docs/brand/LICENSE-brand.txt']
        for name in notices:
            Path(name).parent.mkdir(parents=True, exist_ok=True)
            self.commit(name, 'Fixture notice')
        assets = self.source / 'dist/web'
        assets.mkdir(parents=True)
        from test_runtime_notices import wasm as runtime_wasm
        (assets / 'editor.wasm').write_bytes(runtime_wasm())
        output = self.root / 'package-provenance.zip'

        def manifest():
            subprocess.run([sys.executable, str(script), str(output)], check=True, capture_output=True, text=True, env={**os.environ, 'CARGO': str(cargo)})
            with zipfile.ZipFile(output) as archive:
                return json.loads(archive.read('release-source.json')), archive.namelist()

        clean, clean_files = manifest()
        self.assertFalse(clean['dirty'])
        with zipfile.ZipFile(output) as archive:
            for name in notices:
                self.assertIn('public/'+name, clean_files)
                self.assertEqual(archive.read('public/'+name), Path(name).read_bytes())
            notice_index = json.loads(archive.read('public/rust-notices/index.json'))
            self.assertEqual(notice_index['cargoLockSha256'], hashlib.sha256(Path('Cargo.lock').read_bytes()).hexdigest())
            self.assertEqual(archive.read('public/rust-notices/synthetic-dependency-1.0.0/registry/LICENSE-MIT'), license_bytes)
            self.assertNotIn(str(registry), json.dumps(notice_index))
            runtime_index = json.loads(archive.read('public/runtime-notices/index.json'))
            self.assertEqual(runtime_index['browserArtifact']['rustcProducer'], '1.95.0 (59807616e 2026-04-14)')
            for item in runtime_index['files']:
                self.assertEqual(archive.read('public/runtime-notices/' + item['path']), (runtime_source / item['path']).read_bytes())
        Path('uncommitted-adapter.rs').write_text('unreviewed implementation')
        dirty, files = manifest()
        self.assertIn('uncommitted-adapter.rs', files)
        self.assertTrue(dirty['dirty'], 'Packaged source absent from the named commit must never claim to be clean')
        previous_package = output.read_bytes()
        (assets / 'editor.wasm').write_bytes(runtime_wasm('1.96.0 (wrong compiler)'))
        with self.assertRaises(subprocess.CalledProcessError):
            manifest()
        self.assertEqual(output.read_bytes(), previous_package, 'Wrong actual WASM compiler must stop before replacing the distributable')
        (assets / 'editor.wasm').write_bytes(runtime_wasm())
        (registry / 'LICENSE-MIT').unlink()
        with self.assertRaises(subprocess.CalledProcessError):
            manifest()
        self.assertEqual(output.read_bytes(), previous_package, 'Missing dependency notices must stop before writing a new distributable')


if __name__ == '__main__':
    unittest.main()
