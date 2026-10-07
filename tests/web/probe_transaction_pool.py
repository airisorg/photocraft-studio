"""Loopback-only diagnostic for Parse/Bind crossing an idle transaction boundary.

This deliberately swaps PostgreSQL backends after Parse/Sync returns idle. It is a
protocol fault fixture, not a Supavisor emulator or proof of a hosted failure cause.
Requires the existing disposable, trust-authenticated local test database. Prints
statuses and SQLSTATE only; cleans its own synthetic account. Use --expect-success
to turn a reproduced HTTP failure into a failing regression command.

Run from the repository with tests/web/requirements.txt installed:
    python tests/web/probe_transaction_pool.py --output /tmp/pool-probe.json
The output's all_routes_ok is the application result; the default process exit
only means the diagnostic completed. Add --expect-success for red/green testing.
This opt-in fixture is separate from the normal API/auth/startup release suites.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
import uuid

import psycopg
import requests

from benchmark_collaboration import ROOT, local_database, worker


def exact(sock, size):
    result = b''
    while len(result) < size:
        chunk = sock.recv(size-len(result))
        if not chunk:
            raise EOFError()
        result += chunk
    return result


def message(sock):
    header = exact(sock, 5)
    size = int.from_bytes(header[1:], 'big')
    if not 4 <= size <= 128 * 1024 * 1024:
        raise ValueError('Unexpected PostgreSQL frame size')
    return header + exact(sock, size-4)


def probe(database, binary, output, api_suite=False):
    host = local_database(database)
    target = urlparse(database)
    if target.password:
        raise ValueError('Use the disposable trust-authenticated fixture, without a password')
    counts = {'idle_backend_swaps': 0, 'idle_parse_swaps': 0, 'named_parses': 0, 'sqlstates': []}
    if api_suite and not Path(os.environ.get('PHOTOCRAFT_FIXTURE', '')).is_file():
        raise ValueError('--api-suite requires PHOTOCRAFT_FIXTURE pointing to the native test document')

    def connect(startup):
        upstream = socket.create_connection((host, target.port or 5432), timeout=5)
        upstream.sendall(startup)
        frames = []
        while True:
            frame = message(upstream)
            frames.append(frame)
            if frame[:1] == b'R' and frame[5:] != b'\0\0\0\0':
                upstream.close()
                raise ValueError('Proxy requires the local trust-authenticated fixture')
            if frame[:1] == b'Z':
                return upstream, frames

    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            upstream = None
            try:
                length = exact(self.request, 4)
                startup = length + exact(self.request, int.from_bytes(length, 'big')-4)
                upstream, frames = connect(startup)
                for frame in frames:
                    self.request.sendall(frame)
                parsed = False
                expected_ready = 0
                open_batch = False
                while True:
                    readable, _, _ = select.select([self.request, upstream], [], [], 5)
                    for source in readable:
                        frame = message(source)
                        if source is self.request:
                            if frame[:1] in (b'Q', b'S', b'F'):
                                expected_ready += 1
                            if frame[:1] in (b'P', b'B', b'E', b'D', b'C'):
                                open_batch = True
                            elif frame[:1] == b'S':
                                open_batch = False
                            if frame[:1] == b'P':
                                parsed = True
                                counts['named_parses'] += int(frame[5:6] != b'\0')
                            upstream.sendall(frame)
                        else:
                            if frame[:1] == b'E':
                                for field in frame[5:].split(b'\0'):
                                    code = field[1:]
                                    if (field[:1] == b'C' and len(code) == 5
                                            and all(48 <= char <= 57 or 65 <= char <= 90 for char in code)):
                                        counts['sqlstates'].append(code.decode('ascii'))
                            if frame[:1] == b'Z':
                                expected_ready = max(0, expected_ready-1)
                                # Pin a backend inside BEGIN/COMMIT; only idle releases it.
                                # Also finish queued rollback/ping batches before release,
                                # matching the provider's outstanding-ReadyForQuery rule.
                                if frame[5:6] == b'I' and expected_ready == 0 and not open_batch:
                                    upstream.close()
                                    upstream, _ = connect(startup)
                                    counts['idle_backend_swaps'] += 1
                                    counts['idle_parse_swaps'] += int(parsed)
                                parsed = False
                            self.request.sendall(frame)
            except (EOFError, OSError):
                pass
            finally:
                if upstream:
                    upstream.close()

    class Proxy(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    output.parent.mkdir(parents=True, exist_ok=True)
    ident, token = str(uuid.uuid4()), secrets.token_hex(32)
    http = requests.Session()
    http.trust_env = False
    http.cookies.set('pc_session', token)
    rows = []
    api_exit_code = None
    with Proxy(('127.0.0.1', 0), Forward) as proxy:
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        proxy_url = database.replace(target.netloc, f'{target.username}@127.0.0.1:{proxy.server_address[1]}')
        try:
            with worker(binary, proxy_url, output.with_suffix('.server.log')) as base:
                config = http.get(base+'/api/config', timeout=20, allow_redirects=False)
                if config.status_code != 200 or not config.json().get('cloud'):
                    raise RuntimeError('Fixture worker did not initialize its schema')
                with psycopg.connect(database, hostaddr=host, autocommit=True) as db:
                    try:
                        db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                                   (ident, f'{ident}@example.invalid', 'Transaction boundary probe'))
                        db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                                   (hashlib.sha256(token.encode()).hexdigest(), ident))
                        for path in ['/api/me', '/api/projects']:
                            started = time.perf_counter()
                            response = http.get(base+path, timeout=20, allow_redirects=False)
                            rows.append({'path': path, 'status': response.status_code,
                                         'ms': round((time.perf_counter()-started)*1000, 3)})
                        if api_suite:
                            env = {**os.environ, 'PHOTOCRAFT_TEST_ORIGIN': base,
                                   'PHOTOCRAFT_TEST_DATABASE_URL': database}
                            with output.with_suffix('.api.log').open('w') as log:
                                api_exit_code = subprocess.run([sys.executable, str(ROOT/'tests/web/test_api.py'), '-f'],
                                                               env=env, stdout=log, stderr=subprocess.STDOUT).returncode
                    finally:
                        db.execute('DELETE FROM photocraft.sessions WHERE account_id=%s', (ident,))
                        db.execute('DELETE FROM photocraft.accounts WHERE id=%s', (ident,))
        finally:
            http.close()
            proxy.shutdown()
            thread.join(timeout=5)
    result = {'fixture': 'backend swap after each completed idle protocol batch; not actual Supavisor',
              'all_routes_ok': all(row['status'] == 200 for row in rows) and api_exit_code in (None, 0),
              'api_suite_exit_code': api_exit_code,
              'results': rows, **counts}
    output.write_text(json.dumps(result, indent=2)+'\n')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default=os.environ.get('PHOTOCRAFT_TEST_DATABASE_URL',
                        'postgresql://photocraft_test@127.0.0.1:55438/postgres'))
    parser.add_argument('--binary', type=Path, default=ROOT/'target/cloud/debug/photocraft-cloud')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expect-success', action='store_true')
    parser.add_argument('--api-suite', action='store_true',
                        help='Run full API suite through swaps; set PHOTOCRAFT_FIXTURE and PUBLIC_DIR=dist/web')
    args = parser.parse_args()
    result = probe(args.database, args.binary.resolve(), args.output.resolve(), args.api_suite)
    print(json.dumps(result, indent=2))
    if args.expect_success and not result['all_routes_ok']:
        raise SystemExit(1)
