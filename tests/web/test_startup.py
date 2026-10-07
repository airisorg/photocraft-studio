"""A delayed local database must not prevent the browser editor from loading."""
import os
from pathlib import Path
import select
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import urlparse

import requests

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

        class Forward(socketserver.BaseRequestHandler):
            def handle(self):
                with socket.create_connection((target.hostname, target.port or 5432), timeout=3) as remote:
                    peers = [self.request, remote]
                    while True:
                        readable, _, _ = select.select(peers, [], [], 1)
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
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
                self.assertFalse(http.get(base+'/api/config').json()['cloud'])
                self.assertEqual(http.get(base+'/healthz').json()['cloud'], 'starting')
                # The provider at port 1 is unreachable. A 503 proves storage readiness
                # rejected this confirmation before it could consume the single-use token.
                confirmation = http.post(base+'/auth/confirm', headers={'Origin':base},
                    data={'token_hash':'a'*64,'type':'email'},timeout=7)
                self.assertEqual(confirmation.status_code, 503)
                self.assertIsNone(process.poll())

                proxy = Proxy(('127.0.0.1', db_port), Forward)
                threading.Thread(target=proxy.serve_forever, daemon=True).start()
                deadline = time.monotonic() + 20
                while not http.get(base+'/api/config', timeout=1).json()['cloud']:
                    self.assertLess(time.monotonic(), deadline, 'Database never recovered')
                    time.sleep(.1)
                self.assertEqual(http.get(base+'/healthz').json()['cloud'], 'ready')
                self.assertEqual(http.get(base+'/api/me').status_code, 401)
                self.assertEqual(http.get(base+'/').status_code, 200)
                self.assertIsNone(process.poll())
            finally:
                process.terminate()
                process.wait(timeout=5)
                http.close()
                if proxy:
                    proxy.shutdown()
                    proxy.server_close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
