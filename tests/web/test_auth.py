"""HTTP auth adapter tests with a loopback GoTrue simulator; this is not Google proof."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import unittest
from urllib.parse import parse_qs, urlencode, urlparse
import uuid

import psycopg
import requests

DATABASE = os.environ.get('PHOTOCRAFT_TEST_DATABASE_URL', 'postgresql://photocraft_test@127.0.0.1:55438/postgres')
ORIGIN = 'https://photocraft-auth-test.example.invalid'


class Provider(BaseHTTPRequestHandler):
    mode = 'success'
    ident = str(uuid.uuid4())
    calls = []
    used = set()

    def log_message(self, *args):
        pass

    def response(self, code, body):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        Provider.calls.append(self.path)
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        token = body.get('token_hash')
        if Provider.mode == 'expired' or token in Provider.used:
            self.response(403, {'error':'Expired single-use token'})
            return
        Provider.used.add(token)
        self.response(200, {'access_token': 'synthetic-provider-token', 'refresh_token': 'synthetic-refresh-token'})

    def do_GET(self):
        Provider.calls.append(self.path)
        if Provider.mode == 'rejected_user':
            self.response(401, {'error':'Invalid user'})
        else:
            self.response(200, {'id':Provider.ident, 'email':Provider.ident+'@example.invalid',
                               'email_confirmed_at': None if Provider.mode=='unverified' else '2026-01-01T00:00:00Z',
                               'user_metadata': {'full_name':'Synthetic sign-in'}})


class AuthContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if urlparse(DATABASE).hostname not in {'localhost','127.0.0.1','::1'}:
            raise RuntimeError('Auth simulator requires a disposable loopback database')
        cls.provider = ThreadingHTTPServer(('127.0.0.1',0), Provider)
        cls.thread = threading.Thread(target=cls.provider.serve_forever, daemon=True)
        cls.thread.start()
        with socket.socket() as s:
            s.bind(('127.0.0.1',0))
            port = s.getsockname()[1]
        cls.base = f'http://127.0.0.1:{port}'
        binary = Path(os.environ.get('PHOTOCRAFT_CLOUD_BIN', 'target/debug/photocraft-cloud')).resolve()
        cls.process = subprocess.Popen([str(binary)], env={**os.environ, 'DATABASE_URL':DATABASE,
            'CLOUD_LOCAL_DEV':'1','PORT':str(port),'APP_ORIGIN':ORIGIN,'SUPABASE_ANON_KEY':'public-test-key',
            'SUPABASE_URL':f'http://127.0.0.1:{cls.provider.server_port}'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.http = requests.Session()
        cls.http.trust_env = False
        for _ in range(50):
            try:
                if cls.http.get(cls.base+'/healthz',timeout=1).ok:
                    break
            except requests.ConnectionError:
                time.sleep(.1)
        else:
            raise RuntimeError('Disposable auth test service did not start')
        cls.db = psycopg.connect(DATABASE, autocommit=True)

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=10)
        cls.provider.shutdown()
        cls.provider.server_close()
        cls.db.execute('DELETE FROM photocraft.sessions WHERE account_id=%s',(Provider.ident,))
        cls.db.execute('DELETE FROM photocraft.accounts WHERE id=%s',(Provider.ident,))
        cls.db.close()

    def setUp(self):
        Provider.mode = 'success'
        Provider.calls = []

    def callback(self, state=None, cookie=None, token=None, kind='email'):
        nonce = secrets.token_hex(32)
        query = urlencode({'state':state or nonce,'token_hash':token or secrets.token_hex(32),'type':kind})
        return self.http.get(self.base+'/auth/callback?'+query, headers={'Cookie':'pc_login='+ (cookie or nonce)},allow_redirects=False)

    def test_login_uses_own_origin_and_secure_nonce(self):
        r=self.http.get(self.base+'/auth/login?return=https://evil.invalid',allow_redirects=False)
        self.assertEqual(r.status_code,303)
        target=urlparse(r.headers['Location'])
        self.assertEqual(target.hostname,'oauth.trytofu.ai')
        args=parse_qs(target.query)
        self.assertEqual(args['flow'],['pkce'])
        self.assertTrue(args['return'][0].startswith(ORIGIN+'/auth/callback?state='))
        for flag in ['HttpOnly','Secure','SameSite=Lax','Max-Age=600']:
            self.assertIn(flag,r.headers['Set-Cookie'])

    def test_state_mismatch_never_redeems(self):
        self.assertEqual(self.callback(state='mismatch').status_code,400)
        self.assertEqual(Provider.calls,[])

    def test_expired_token_never_creates_session(self):
        Provider.mode='expired'
        self.assertEqual(self.callback().status_code,400)
        self.assertEqual(Provider.calls,['/auth/v1/verify'])

    def test_verified_identity_required(self):
        for mode in ['rejected_user','unverified']:
            with self.subTest(mode=mode):
                Provider.mode=mode
                r=self.callback()
                self.assertEqual(r.status_code,400)
                self.assertNotIn('pc_session=',r.headers.get('Set-Cookie',''))

    def test_success_stores_only_session_digest_and_reads_identity(self):
        r=self.callback()
        self.assertEqual(r.status_code,303)
        cookie=r.cookies.get('pc_session')
        self.assertEqual(len(cookie),64)
        self.assertEqual(Provider.calls,['/auth/v1/verify','/auth/v1/user'])
        record=self.db.execute('SELECT hash FROM photocraft.sessions WHERE hash=%s',(hashlib.sha256(cookie.encode()).hexdigest(),)).fetchone()
        self.assertIsNotNone(record)
        self.assertNotEqual(record[0],cookie)
        me=self.http.get(self.base+'/api/me',headers={'Cookie':'pc_session='+cookie}).json()
        self.assertEqual(me['id'],Provider.ident)
        for flag in ['HttpOnly','Secure','SameSite=Lax']:
            self.assertIn(flag,r.headers['Set-Cookie'])

    def test_replayed_single_use_token_fails(self):
        token=secrets.token_hex(32)
        self.assertEqual(self.callback(token=token).status_code,303)
        self.assertEqual(self.callback(token=token).status_code,400)

    def test_unknown_token_type_never_redeems(self):
        self.assertEqual(self.callback(kind='recovery').status_code,400)
        self.assertEqual(Provider.calls,[])


if __name__=='__main__':
    unittest.main(verbosity=2)
