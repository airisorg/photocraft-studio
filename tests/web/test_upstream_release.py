"""Exercise update/promotion safety using disposable Git repos, without GitHub or Tofu."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
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
        for name in ['LICENSE-MIT', 'LICENSE-APACHE', 'NOTICE', 'ATTRIBUTION.md', 'SECURITY.md',
                     'assets/fonts/OFL-Inter.txt', 'assets/fonts/OFL-JetBrainsMono.txt',
                     'assets/icons/LICENSE-lucide.txt', 'assets/dict/LICENSE-SCOWL.txt']:
            path = Path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('Synthetic publication notice')
        release.git('add', '--all')
        release.git('commit', '-m', 'Synthetic required notices')
        # The real Git history/ref/notice checks run below; only the external
        # scanner is substituted so these fixtures do not require its binary.
        scanner = mock.patch.object(release.publication, 'secrets',
                                    return_value={'status': 'checked', 'findings': 0})
        self.secret_scan = scanner.start()
        self.addCleanup(scanner.stop)
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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
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

    def test_workflow_opt_in_precedes_every_candidate_write(self):
        workflow = (Path(SPEC.origin).parents[2] / '.github/workflows/update-and-release.yml').read_text()
        jobs = dict(re.findall(r'^  ([a-z]+):\n((?:^    .*\n|^\n)+)', workflow.split('\njobs:\n', 1)[1], re.M))
        self.assertEqual(set(jobs), {'prepare', 'native', 'browser', 'promote'})
        # Accept only a single, event-independent equality at job scope. A step
        # guard, truthy string, manual-event escape or `always()` must fail here.
        conditions = re.findall(r'^    if: (.+)$', jobs['prepare'], re.M)
        self.assertEqual(len(conditions), 1, 'Preparation must default to skipped before checkout or Git writes')
        condition = conditions[0].strip()
        if condition.startswith('${{') and condition.endswith('}}'):
            condition = condition[3:-2].strip()
        equality = re.fullmatch(r"vars\.(PHOTOCRAFT_UPSTREAM_UPDATES_ENABLED) == 'true'", condition)
        self.assertIsNotNone(equality, 'Only the explicit repository-variable opt-in may admit preparation')
        self.assertRegex(workflow, r'(?m)^  push:\n    branches: \[main\]$')
        self.assertRegex(workflow, r'(?m)^  schedule:$')
        self.assertRegex(workflow, r'(?m)^  workflow_dispatch:$')
        for name in ('native', 'browser'):
            self.assertRegex(jobs[name], r'(?m)^    needs: prepare$')
            self.assertRegex(jobs[name], r"(?m)^    if: needs.prepare.outputs.needed == 'true'$")
        self.assertRegex(jobs['promote'], r'(?m)^    needs: \[prepare, native, browser\]$')
        self.assertNotRegex(jobs['promote'], r'(?m)^    if:')

        # Exercise the actual candidate-producing helper against disposable Git
        # remotes under the parsed guard. This is not a hosted Actions run. GitHub
        # string equality ignores case; missing vars resolve to the empty string.
        original_refs = release.git('ls-remote', 'origin').stdout
        for event in ('default', 'push', 'schedule', 'workflow_dispatch'):
            for value in (None, '', 'false', '0', 'yes', ' true '):
                with self.subTest(event=event, value=value):
                    variables = {} if value is None else {equality[1]: value}
                    admitted = variables.get(equality[1], '').lower() == 'true'
                    self.assertFalse(admitted)
                    if admitted:
                        self.prepare()
                    self.assertEqual(release.git('ls-remote', 'origin').stdout, original_refs)
                    self.assertEqual(self.head(), self.base)
        for event, value in (('push', 'true'), ('schedule', 'TRUE'), ('workflow_dispatch', 'true')):
            with self.subTest(event=event, enabled=value):
                self.assertEqual(value.lower(), 'true')
                info = self.prepare()
                self.assertEqual(self.remote_head('automation/upstream-test'), info['candidate'])
                self.assertEqual(self.remote_head(), self.base, 'Enabling preparation still cannot promote main')
                release.git('push', 'origin', '--delete', 'automation/upstream-test')

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

    def test_deleted_restricted_ancestor_is_rejected_before_candidate_push(self):
        with self.at(self.upstream):
            self.commit('docs/brand/synthetic-mark.svg', '<svg>restricted fixture</svg>')
            release.git('rm', 'docs/brand/synthetic-mark.svg')
            release.git('commit', '-m', 'Remove current mark but retain history')
        with self.assertRaisesRegex(ValueError, 'publication check failed'):
            self.prepare()
        self.assertEqual(self.remote_head(), self.base)
        self.assertEqual(release.git('ls-remote', 'origin', 'refs/heads/automation/upstream-test').stdout, '')

    def test_candidate_cannot_replace_the_trusted_publication_checker(self):
        with self.at(self.upstream):
            self.commit('packaging/web/check-publication.py',
                        'from pathlib import Path\nPath("untrusted-checker-executed").write_text("bad")\n')
            self.commit('private-fixture.txt', '/Users/' + 'synthetic-fixture/private-project/')
        with self.assertRaisesRegex(ValueError, 'publication check failed'):
            self.prepare()
        self.assertFalse(Path('untrusted-checker-executed').exists())
        self.assertEqual(release.git('ls-remote', 'origin', 'refs/heads/automation/upstream-test').stdout, '')

    def test_unavailable_or_positive_secret_scan_refuses_remote_writes(self):
        for status in [{'status': 'unavailable', 'findings': None},
                       {'status': 'error', 'findings': None},
                       {'status': 'checked', 'findings': 1}]:
            with self.subTest(status=status):
                self.secret_scan.return_value = status
                with self.assertRaisesRegex(ValueError, 'publication check failed'):
                    self.prepare()
                self.assertEqual(self.remote_head(), self.base)
                self.assertEqual(release.git('ls-remote', 'origin', 'refs/heads/automation/upstream-test').stdout, '')

    def test_exact_ref_checks_candidate_notices_and_rejects_option_like_refs(self):
        release.git('rm', 'NOTICE')
        release.git('commit', '-m', 'Synthetic missing candidate notice')
        candidate = self.head()
        release.git('checkout', '--detach', self.base)
        report = release.publication.check(self.source, ref=candidate)
        self.assertEqual(report['sourceCommit'], candidate)
        self.assertEqual(report['missingNoticeCount'], 1)
        self.assertFalse(report['passed'])
        self.assertTrue(Path('NOTICE').exists(), 'Current checkout must not substitute candidate notices')
        Path('NOTICE').unlink()
        Path('NOTICE').symlink_to('LICENSE-MIT')
        release.git('add', 'NOTICE')
        release.git('commit', '-m', 'Synthetic symlink notice')
        symlink_candidate = self.head()
        release.git('checkout', '--detach', self.base)
        self.assertEqual(release.publication.check(self.source, ref=symlink_candidate)['missingNoticeCount'], 1)
        for ref in ['main', '--all', 'a'*39, 'A'*40, 'a'*40+' --all']:
            with self.subTest(ref=ref), self.assertRaises(ValueError):
                release.publication.check(self.source, ref=ref)

    def test_exact_ref_excludes_unpublished_private_branch_but_default_does_not(self):
        release.git('checkout', '-b', 'unpublished-private')
        self.commit('private-fixture.txt', '/home/' + 'synthetic-fixture/private/')
        release.git('checkout', 'main')
        self.assertTrue(release.publication.check(self.source, ref=self.base)['passed'])
        self.assertFalse(release.publication.check(self.source)['passed'])
        info = self.prepare()
        self.assertEqual(self.remote_head('automation/upstream-test'), info['candidate'])

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
        private_paths = [b'/' + b'Users/' + b'synthetic-operator/project/source.rs',
                         b'/' + b'home/' + b'synthetic-operator/project/source.rs',
                         b'C:' + bytes([92]) + b'Users' + bytes([92]) + b'synthetic-operator' + bytes([92]) + b'source.rs']
        for private_path in private_paths:
            # A valid WASM custom section represents compiler-emitted file strings.
            from test_runtime_notices import integer, string
            section = string('fixture-path') + private_path
            (assets / 'editor.wasm').write_bytes(runtime_wasm() + b'\0' + integer(len(section)) + section)
            with self.assertRaises(subprocess.CalledProcessError) as failure:
                manifest()
            self.assertIn('private home path', failure.exception.stderr)
            self.assertNotIn(private_path.decode(), failure.exception.stderr)
            self.assertEqual(output.read_bytes(), previous_package, 'Private artifact must not replace the distributable')
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
