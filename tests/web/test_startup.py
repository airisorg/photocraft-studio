"""A delayed local database must not prevent the browser editor from loading."""
import os
import hashlib
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
