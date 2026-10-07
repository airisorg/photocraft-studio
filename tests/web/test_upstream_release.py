"""Exercise update/promotion safety using disposable Git repos, without GitHub or Tofu."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
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


if __name__ == '__main__':
    unittest.main()
