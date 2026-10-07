"""Merge the real upstream, then publish only a package from the passing candidate.

The workflow separates preparation/promotion (write permission) from executing the
candidate (read permission). This script is also exercised against local Git remotes.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile
import zipfile


def git(*args, cwd=None, check=True):
    return subprocess.run(['git', *args], cwd=cwd, text=True, check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def sha(value):
    if not re.fullmatch(r'[0-9a-f]{40}', value):
        raise ValueError('Expected a full Git commit SHA')
    return value


def prepare(branch, upstream_url='https://github.com/storytold/photocraft.git'):
    if not re.fullmatch(r'automation/upstream-[a-zA-Z0-9-]+', branch):
        raise ValueError('Candidate must use the reserved automation/upstream- prefix')
    if git('status', '--porcelain').stdout.strip():
        raise ValueError('Candidate preparation requires a clean checkout')
    git('fetch', 'origin', 'main')
    base = git('rev-parse', 'FETCH_HEAD').stdout.strip()
    git('fetch', upstream_url, 'refs/heads/main')
    upstream = git('rev-parse', 'FETCH_HEAD').stdout.strip()
    git('checkout', '--detach', base)
    if git('merge-base', '--is-ancestor', upstream, base, check=False).returncode:
        merged = git('merge', '--no-ff', '--no-edit', upstream, check=False)
        if merged.returncode:
            conflicts = git('diff', '--name-only', '--diff-filter=U').stdout
            git('merge', '--abort')
            raise RuntimeError('Upstream conflicts; main and deployment are unchanged:\n' + conflicts)
    candidate = git('rev-parse', 'HEAD').stdout.strip()
    released = git('ls-remote', 'origin', 'refs/heads/tofu-release').stdout.strip()
    if released:
        git('fetch', 'origin', 'tofu-release')
        previous = git('show', 'FETCH_HEAD:release-source.json', check=False)
        if previous.returncode == 0 and json.loads(previous.stdout).get('sourceCommit') == candidate:
            return dict(base=base, candidate=candidate, upstream=upstream, branch=branch, needed='false')
    git('push', 'origin', f'{candidate}:refs/heads/{branch}')
    return dict(base=base, candidate=candidate, upstream=upstream, branch=branch, needed='true')


def unpack_package(package, dest, candidate):
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError('Duplicate package paths')
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or any(
                    p in {'.git', '.env', 'node_modules', 'target'} or p.startswith('.env.') for p in path.parts):
                raise ValueError('Unsafe package path: ' + entry.filename)
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Symlinks are not accepted in a release package')
        manifest = json.loads(archive.read('release-source.json'))
        if manifest.get('sourceCommit') != candidate or manifest.get('dirty') is not False:
            raise ValueError('Package does not identify the clean tested candidate')
        wasm_name = manifest['wasm']['path']
        if not wasm_name.startswith('public/') or not wasm_name.endswith('.wasm'):
            raise ValueError('Invalid WASM manifest path')
        if hashlib.sha256(archive.read(wasm_name)).hexdigest() != manifest['wasm']['sha256']:
            raise ValueError('WASM checksum does not match the tested package')
        for required in ['Dockerfile', 'Cargo.toml', 'Cargo.lock', 'public/index.html']:
            if required not in names:
                raise ValueError('Missing release file: ' + required)
        archive.extractall(dest)
    return manifest


def promote(base, candidate, upstream, branch, package):
    base, candidate, upstream = map(sha, (base, candidate, upstream))
    if not re.fullmatch(r'automation/upstream-[a-zA-Z0-9-]+', branch):
        raise ValueError('Invalid candidate branch')
    git('fetch', 'origin', 'main', branch)
    current = git('ls-remote', 'origin', 'refs/heads/main').stdout.split()[0]
    if current != base:
        raise ValueError('Main changed during acceptance; validate the newer revision before promotion')
    git('merge-base', '--is-ancestor', base, candidate)
    git('merge-base', '--is-ancestor', upstream, candidate)
    remote = git('remote', 'get-url', 'origin').stdout.strip()
    with tempfile.TemporaryDirectory(prefix='photocraft-release-') as folder:
        dest = Path(folder)
        manifest = unpack_package(package, dest, candidate)
        manifest['upstreamCommit'] = upstream
        (dest / 'release-source.json').write_text(json.dumps(manifest, indent=2) + '\n')
        git('init', '--quiet', cwd=dest)
        git('remote', 'add', 'origin', remote, cwd=dest)
        for key in ['user.name', 'user.email']:
            git('config', key, git('config', '--get', key).stdout.strip(), cwd=dest)
        exists = git('ls-remote', 'origin', 'refs/heads/tofu-release').stdout.strip()
        if exists:
            git('fetch', 'origin', 'tofu-release')
            parent = git('rev-parse', 'FETCH_HEAD').stdout.strip()
            repository = git('rev-parse', '--show-toplevel').stdout.strip()
            git('fetch', '--depth=1', repository, parent, cwd=dest)
            git('reset', '--soft', 'FETCH_HEAD', cwd=dest)
        git('add', '--all', '--force', cwd=dest)
        git('commit', '--allow-empty', '-m', f'Tested PhotoCraft {candidate[:12]}', cwd=dest)
        release = git('rev-parse', 'HEAD', cwd=dest).stdout.strip()
        # Import the generated commit into this repo, then move both branches atomically.
        git('fetch', str(dest), release)
        git('push', '--atomic', 'origin', f'{candidate}:refs/heads/main', f'{release}:refs/heads/tofu-release')
    git('push', 'origin', '--delete', branch)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    create = sub.add_parser('prepare')
    create.add_argument('--branch', required=True)
    publish = sub.add_parser('promote')
    for name in ['base', 'candidate', 'upstream', 'branch', 'package']:
        publish.add_argument('--' + name, required=True)
    args = vars(parser.parse_args())
    action = args.pop('action')
    if action == 'prepare':
        for key, value in prepare(**args).items():
            print(f'{key}={value}')
    else:
        promote(**args)
