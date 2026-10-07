"""Cold workers initialize storage on demand while a delayed database keeps the editor usable."""
import os
import hashlib
from contextlib import contextmanager
from pathlib import Path
import select
import secrets
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
import uuid
from urllib.parse import urlparse

import requests
import psycopg

DATABASE = os.environ.get('PHOTOCRAFT_TEST_DATABASE_URL', 'postgresql://photocraft_test@127.0.0.1:55438/postgres')


class ColdWorkerReadiness(unittest.TestCase):
    """Every worker must initialize storage on the request that actually needs it."""

    @classmethod
    def setUpClass(cls):
        if urlparse(DATABASE).hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise RuntimeError('Cold-worker tests require a disposable loopback database')
        migration = Path(__file__).resolve().parents[2] / 'apps/photocraft-cloud/migrations/001_cloud.sql'
        # Seed the shared database directly; no HTTP request may warm the tested workers.
        with psycopg.connect(DATABASE) as database:
            database.execute('SELECT pg_advisory_xact_lock(735193624)')
            database.execute(migration.read_text())

    def setUp(self):
        self.fixture = Path(os.environ['PHOTOCRAFT_FIXTURE']).read_bytes()
        self.db = psycopg.connect(DATABASE, autocommit=True)
        self.ident, self.token = str(uuid.uuid4()), secrets.token_hex(32)
        self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
            (self.ident, f'{self.ident}@example.invalid', 'Cold worker test'))
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
            (hashlib.sha256(self.token.encode()).hexdigest(), self.ident))

    def tearDown(self):
        self.db.execute('DELETE FROM photocraft.projects WHERE owner_id=%s', (self.ident,))
        self.db.execute('DELETE FROM photocraft.sessions WHERE account_id=%s', (self.ident,))
        self.db.execute('DELETE FROM photocraft.accounts WHERE id=%s', (self.ident,))
        self.db.close()

    @contextmanager
    def worker(self):
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        binary = str(Path(os.environ.get('PHOTOCRAFT_CLOUD_BIN', 'target/debug/photocraft-cloud')).resolve())
        process = subprocess.Popen([binary], env={**os.environ, 'CLOUD_LOCAL_DEV': '1',
            'DATABASE_URL': DATABASE, 'APP_ORIGIN': base, 'PORT': str(port)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        http = requests.Session()
        http.trust_env = False
        http.headers['Origin'] = base
        http.cookies.set('pc_session', self.token)
        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    # A TCP probe must not warm /api/config or any database-backed route.
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    self.assertIsNone(process.poll(), 'Disposable worker exited before listening')
                    self.assertLess(time.monotonic(), deadline, 'Disposable worker did not listen')
                    time.sleep(.02)
            yield base, http
        finally:
            http.close()
            process.terminate()
            process.wait(timeout=5)

    def cold_request(self, method, path, **kwargs):
        with self.worker() as (base, http):
            response = http.request(method, base + path, timeout=20, **kwargs)
            self.assertEqual(response.status_code, 200,
                f'First request {method} {path}: {response.status_code} {response.text[:200]}')
            return response

    def reservation(self):
        pid = str(uuid.uuid4())
        self.db.execute('INSERT INTO photocraft.projects(id,owner_id,title) VALUES(%s,%s,%s)',
            (pid, self.ident, 'Cold save'))
        return pid

    def upload_spec(self):
        return {'base_revision': 0, 'bytes': len(self.fixture), 'parts': (len(self.fixture)+524287)//524288,
            'sha256': hashlib.sha256(self.fixture).hexdigest(), 'title': 'Cold save', 'width': 64, 'height': 48}

    def test_authenticated_first_requests_initialize_storage(self):
        pid = self.reservation()
        for method, path, payload in [
            ('GET', '/api/projects', None),
            ('GET', '/api/me', None),
            ('POST', '/api/projects', {'title': 'Cold create'}),
            ('PATCH', f'/api/projects/{pid}', {'trashed': True}),
            ('PATCH', f'/api/projects/{pid}', {'trashed': False}),
            ('POST', f'/api/projects/{pid}/uploads', self.upload_spec()),
        ]:
            with self.subTest(method=method, path=path):
                self.cold_request(method, path, **({'json': payload} if payload is not None else {}))

    def test_public_share_and_logout_first_requests_initialize_storage(self):
        pid, key = self.reservation(), secrets.token_hex(32)
        self.db.execute('INSERT INTO photocraft.versions(project_id,revision,author_id,title,data,sha256) VALUES(%s,1,%s,%s,%s,%s)',
            (pid, self.ident, 'Saved', self.fixture, hashlib.sha256(self.fixture).hexdigest()))
        self.db.execute('UPDATE photocraft.projects SET revision=1,width=64,height=48 WHERE id=%s', (pid,))
        self.db.execute('INSERT INTO photocraft.shares(hash,project_id) VALUES(%s,%s)',
            (hashlib.sha256(key.encode()).hexdigest(), pid))
        for suffix in ['', '/content?part=0']:
            with self.subTest(public_path=suffix):
                with self.worker() as (base, http):
                    http.cookies.clear()
                    response = http.get(base + f'/api/share/{key}' + suffix, timeout=20)
                    self.assertEqual(response.status_code, 200, response.text[:200])
                    if suffix:
                        self.assertEqual(response.content, self.fixture)
                    else:
                        self.assertEqual(response.json()['revision'], 1)
        self.cold_request('POST', '/api/logout')
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.sessions WHERE account_id=%s',
            (self.ident,)).fetchone()[0], 0)

    def test_upload_survives_worker_replacement_between_stages(self):
        # Establish the reservation on one warm worker, then replace it before uploading.
        with self.worker() as (base, http):
            self.assertTrue(http.get(base + '/api/config', timeout=20).json()['cloud'])
            project = http.post(base + '/api/projects', json={'title': 'Survives restart'}, timeout=20)
            self.assertEqual(project.status_code, 200, project.text)
            pid = project.json()['id']
            upload = http.post(base + f'/api/projects/{pid}/uploads', json=self.upload_spec(), timeout=20)
            self.assertEqual(upload.status_code, 200, upload.text)
            uid = upload.json()['id']
        for offset in range(0, len(self.fixture), 524288):
            self.cold_request('PUT', f'/api/uploads/{uid}/{offset//524288}', data=self.fixture[offset:offset+524288])
        self.assertEqual(self.cold_request('POST', f'/api/uploads/{uid}/commit', json={}).json()['revision'], 1)
        rows = self.cold_request('GET', '/api/projects').json()
        item = next(row for row in rows if row['id'] == pid)
        self.assertEqual((item['revision'], item['width'], item['height']), (1, 64, 48))
        metadata = self.cold_request('GET', f'/api/projects/{pid}').json()
        self.assertEqual(metadata['content']['sha256'], hashlib.sha256(self.fixture).hexdigest())
        self.assertEqual(self.cold_request('GET', f'/api/projects/{pid}/content?part=0').content, self.fixture)
        self.cold_request('PATCH', f'/api/projects/{pid}', json={'trashed': True})
        rows = self.cold_request('GET', '/api/projects').json()
        self.assertTrue(next(row for row in rows if row['id'] == pid)['trashed'])


class StartupRecovery(unittest.TestCase):
    def test_editor_available_before_database_and_recovers_without_restart(self):
        target = urlparse(DATABASE)
        self.assertIn(target.hostname, {'127.0.0.1', 'localhost', '::1'})
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            db_port = reserve.getsockname()[1]
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            http_port = reserve.getsockname()[1]
        base = f'http://127.0.0.1:{http_port}'
        delayed_url = DATABASE.replace(target.netloc, f'{target.username}@127.0.0.1:{db_port}')
        statement_names = []

        class Forward(socketserver.BaseRequestHandler):
            def handle(self):
                client_bytes = bytearray()
                startup_seen = False
                with socket.create_connection((target.hostname, target.port or 5432), timeout=3) as remote:
                    peers = [self.request, remote]
                    while True:
                        readable, _, _ = select.select(peers, [], [], 1)
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            if source is self.request:
                                client_bytes.extend(data)
                                if not startup_seen and len(client_bytes) >= 4:
                                    length = int.from_bytes(client_bytes[:4], 'big')
                                    if len(client_bytes) >= length:
                                        del client_bytes[:length]
                                        startup_seen = True
                                while startup_seen and len(client_bytes) >= 5:
                                    length = int.from_bytes(client_bytes[1:5], 'big')
                                    if len(client_bytes) < length + 1:
                                        break
                                    if client_bytes[0] == ord('P'):
                                        statement_names.append(bytes(client_bytes[5:1+length]).split(b'\0', 1)[0])
                                    del client_bytes[:length+1]
                            (remote if source is self.request else self.request).sendall(data)

        class Proxy(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        proxy = None
        http = requests.Session()
        http.trust_env = False
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'index.html').write_text('<h1>Local editor available</h1>')
            binary = str(Path(os.environ.get('PHOTOCRAFT_CLOUD_BIN', 'target/debug/photocraft-cloud')).resolve())
            process = subprocess.Popen([binary], env={**os.environ, 'CLOUD_LOCAL_DEV': '1',
                'DATABASE_URL': delayed_url, 'APP_ORIGIN': base, 'PORT': str(http_port), 'PUBLIC_DIR': directory,
                'SUPABASE_URL':'http://127.0.0.1:1','SUPABASE_ANON_KEY':'public-test-key'},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 2
                while True:
                    try:
                        response = http.get(base+'/', timeout=.3)
                        break
                    except requests.ConnectionError:
                        if time.monotonic() >= deadline:
                            self.fail('Database delay prevented the editor from starting')
                        time.sleep(.05)
                self.assertEqual(response.status_code, 200)
                self.assertIn('Local editor available', response.text)
                configuration = http.get(base+'/api/config', timeout=15).json()
                self.assertFalse(configuration['cloud'])
                self.assertIn(configuration['cloudIssue'], {'connection_timeout', 'connection_unavailable', 'setup_timeout'})
                self.assertEqual(http.get(base+'/healthz').json()['cloud'], 'starting')
                # The provider at port 1 is unreachable. A 503 proves storage readiness
                # rejected this confirmation before it could consume the single-use token.
                confirmation = http.post(base+'/auth/confirm', headers={'Origin':base},
                    data={'token_hash':'a'*64,'type':'email'},timeout=15)
                self.assertEqual(confirmation.status_code, 503)
                self.assertIsNone(process.poll())

                proxy = Proxy(('127.0.0.1', db_port), Forward)
                threading.Thread(target=proxy.serve_forever, daemon=True).start()
                deadline = time.monotonic() + 20
                while not http.get(base+'/api/config', timeout=15).json()['cloud']:
                    self.assertLess(time.monotonic(), deadline, 'Database never recovered')
                    time.sleep(.1)
                self.assertEqual(http.get(base+'/healthz').json()['cloud'], 'ready')
                self.assertEqual(http.get(base+'/api/me').status_code, 401)
                self.assertEqual(http.get(base+'/').status_code, 200)
                self.assertIsNone(process.poll())
                # Exercise bound row and scalar queries through the same wire observer.
                ident, token = str(uuid.uuid4()), secrets.token_hex(32)
                with psycopg.connect(DATABASE, autocommit=True) as database:
                    try:
                        database.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                            (ident, f'{ident}@example.invalid', 'Pooler test'))
                        database.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                            (hashlib.sha256(token.encode()).hexdigest(), ident))
                        http.cookies.set('pc_session', token)
                        self.assertEqual(http.get(base+'/api/me', timeout=15).status_code, 200)
                        project = http.post(base+'/api/projects', headers={'Origin':base},
                            json={'title':'Pooler compatibility test'}, timeout=15)
                        self.assertEqual(project.status_code, 200)
                    finally:
                        database.execute('DELETE FROM photocraft.projects WHERE owner_id=%s', (ident,))
                        database.execute('DELETE FROM photocraft.sessions WHERE account_id=%s', (ident,))
                        database.execute('DELETE FROM photocraft.accounts WHERE id=%s', (ident,))
                self.assertTrue(statement_names, 'The wire check must observe SQL Parse messages')
                self.assertTrue(all(name == b'' for name in statement_names),
                    'Named prepared statements are incompatible with transaction poolers')
            finally:
                process.terminate()
                process.wait(timeout=5)
                http.close()
                if proxy:
                    proxy.shutdown()
                    proxy.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
