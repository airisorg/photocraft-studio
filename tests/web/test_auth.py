"""HTTP auth adapter tests with a loopback GoTrue simulator; this is not Google proof."""
import hashlib
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import tempfile
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
    mail = []
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
        if self.path.startswith('/auth/v1/otp'):
            Provider.mail.append((self.path, self.headers.get('apikey'), body))
            self.response(503 if Provider.mode=='mail_failed' else 200, {})
            return
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
                if cls.http.get(cls.base+'/api/config',timeout=1).json().get('cloud'):
                    break
            except requests.ConnectionError:
                pass
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
        cls.db.execute('DELETE FROM photocraft.projects WHERE owner_id=%s',(Provider.ident,))
        cls.db.execute('DELETE FROM photocraft.sessions WHERE account_id=%s',(Provider.ident,))
        cls.db.execute('DELETE FROM photocraft.accounts WHERE id=%s',(Provider.ident,))
        cls.db.close()

    def setUp(self):
        Provider.mode = 'success'
        Provider.calls = []
        Provider.mail = []

    @contextmanager
    def cold_worker(self, same_origin=False):
        with socket.socket() as reserve:
            reserve.bind(('127.0.0.1', 0))
            port = reserve.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        public = tempfile.TemporaryDirectory()
        Path(public.name, 'index.html').write_text('<title>PhotoCraft auth fixture</title><p>Signed in</p>')
        binary = Path(os.environ.get('PHOTOCRAFT_CLOUD_BIN', 'target/debug/photocraft-cloud')).resolve()
        process = subprocess.Popen([str(binary)], env={**os.environ, 'DATABASE_URL':DATABASE,
            'CLOUD_LOCAL_DEV':'1','PORT':str(port),'APP_ORIGIN':base if same_origin else ORIGIN,
            'SUPABASE_ANON_KEY':'public-test-key','PUBLIC_DIR':public.name,
            'SUPABASE_URL':f'http://127.0.0.1:{self.provider.server_port}'},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic()+5
            while True:
                try:
                    # Only probe TCP: invitation POST must be this worker's first request.
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    self.assertIsNone(process.poll(), 'Cold invitation worker exited')
                    self.assertLess(time.monotonic(), deadline, 'Cold invitation worker did not listen')
                    time.sleep(.02)
            yield base
        finally:
            process.terminate()
            process.wait(timeout=5)
            public.cleanup()

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

    def test_email_link_get_does_not_consume_token(self):
        params={'token_hash':secrets.token_hex(32),'type':'email'}
        url=self.base+'/auth/confirm?'+urlencode(params)
        for _ in range(2):
            r=self.http.get(url)
            self.assertEqual(r.status_code,200)
            self.assertIn('Continue to PhotoCraft',r.text)
        self.assertEqual(Provider.calls,[])
        r=self.http.post(self.base+'/auth/confirm',data=params,headers={'Origin':ORIGIN},allow_redirects=False)
        self.assertEqual(r.status_code,303)
        self.assertIn('pc_session',r.cookies)
        r=self.http.post(self.base+'/auth/confirm',data=params,headers={'Origin':ORIGIN},allow_redirects=False)
        self.assertEqual(r.status_code,400)

    def test_email_confirmation_rejects_missing_null_and_foreign_origins(self):
        for origin in [None, 'null', 'https://evil.invalid']:
            with self.subTest(origin=origin):
                headers = {} if origin is None else {'Origin': origin}
                r = self.http.post(self.base+'/auth/confirm',
                    data={'token_hash':'a'*64,'type':'email'},headers=headers)
                self.assertEqual(r.status_code,403)
                self.assertEqual(Provider.calls,[])

    def test_browser_email_confirmation_submits_real_same_origin_form(self):
        from playwright.sync_api import sync_playwright

        artifact_dir = os.environ.get('PHOTOCRAFT_TEST_ARTIFACTS')
        artifacts = Path(artifact_dir) if artifact_dir else None
        if artifacts:
            artifacts.mkdir(parents=True, exist_ok=True)
        observations = []
        with sync_playwright() as playwright:
            for engine in ['chromium', 'webkit']:
                with self.subTest(engine=engine), self.cold_worker(same_origin=True) as base:
                    Provider.calls = []
                    options = {'headless':True}
                    if engine == 'chromium' and os.environ.get('PHOTOCRAFT_CHROME'):
                        options['executable_path'] = os.environ['PHOTOCRAFT_CHROME']
                    browser = getattr(playwright, engine).launch(**options)
                    try:
                        context = browser.new_context()
                        page = context.new_page()
                        params = {'token_hash':secrets.token_hex(32),'type':'email'}
                        url = base+'/auth/confirm?'+urlencode(params)
                        response = page.goto(url, wait_until='domcontentloaded')
                        self.assertEqual(response.status, 200)
                        self.assertTrue(page.get_by_role('button', name='Continue to PhotoCraft').is_visible())
                        self.assertEqual(page.reload(wait_until='domcontentloaded').status, 200)
                        self.assertEqual(Provider.calls, [], 'Opening or reloading the email link consumed its token')
                        if artifacts:
                            page.screenshot(path=str(artifacts/f'confirmation-{engine}-before.png'))
                        # Do not supply Origin or use an API POST: the browser must
                        # apply the delivered page's referrer policy to its real form.
                        with page.expect_response(lambda r:r.url==base+'/auth/confirm'
                                                  and r.request.method=='POST') as submitted:
                            page.get_by_role('button', name='Continue to PhotoCraft').click()
                        response = submitted.value
                        page.wait_for_load_state('domcontentloaded')
                        headers = response.request.all_headers()
                        referer = headers.get('referer', '')
                        cookies = context.cookies(base)
                        record = {'engine':engine,'origin':headers.get('origin'),
                                  'expected_origin':base,'post_status':response.status,
                                  'referer_has_token':params['token_hash'] in referer or 'token_hash' in referer,
                                  'provider_calls':list(Provider.calls),
                                  'browser_accepted_session':any(c['name']=='pc_session' for c in cookies)}
                        observations.append(record)
                        if artifacts:
                            page.screenshot(path=str(artifacts/f'confirmation-{engine}-after.png'))
                            (artifacts/'confirmation-browser-observations.json').write_text(json.dumps(observations,indent=2))
                        self.assertEqual((response.status, headers.get('origin')), (303, base), record)
                        self.assertNotIn(params['token_hash'], referer)
                        self.assertNotIn('token_hash', referer)
                        self.assertEqual(Provider.calls, ['/auth/v1/verify','/auth/v1/user'])
                        session = next((c for c in cookies if c['name']=='pc_session'), None)
                        self.assertIsNotNone(session, 'The browser did not accept the session cookie')
                        self.assertTrue(session['httpOnly'])
                        self.assertEqual(session['sameSite'], 'Lax')
                        identity = context.request.get(base+'/api/me')
                        self.assertEqual(identity.status, 200)
                        self.assertEqual(identity.json()['id'], Provider.ident)
                        self.assertEqual(page.url, base+'/')
                        # Reopening remains scanner-safe; a second actual form
                        # submission must fail because the provider token was used.
                        self.assertEqual(page.goto(url, wait_until='domcontentloaded').status, 200)
                        self.assertEqual(Provider.calls, ['/auth/v1/verify','/auth/v1/user'])
                        with page.expect_response(lambda r:r.url==base+'/auth/confirm'
                                                  and r.request.method=='POST') as replayed:
                            page.get_by_role('button', name='Continue to PhotoCraft').click()
                        self.assertEqual(replayed.value.status, 400)
                        self.assertEqual(Provider.calls, ['/auth/v1/verify','/auth/v1/user','/auth/v1/verify'])
                        record.update(identity_verified=True, replay_status=replayed.value.status)
                        if artifacts:
                            (artifacts/'confirmation-browser-observations.json').write_text(json.dumps(observations,indent=2))
                    finally:
                        browser.close()

    def invite_fixture(self):
        auth=self.callback()
        h={'Cookie':'pc_session='+auth.cookies.get('pc_session'),'Origin':ORIGIN}
        r=self.http.post(self.base+'/api/projects',json={'title':'Invitation contract'},headers=h)
        self.assertEqual(r.status_code,200,r.text)
        return r.json()['id'],h

    def test_invitation_sends_mail_and_preserves_access_on_delivery_failure(self):
        pid,h=self.invite_fixture()
        for failed in [False,True]:
            email=str(uuid.uuid4())+'@example.invalid'
            Provider.mode='mail_failed' if failed else 'success'
            r=self.http.post(self.base+f'/api/projects/{pid}/invite',json={'email':email,'role':'edit'},headers=h)
            self.assertEqual(r.status_code,502 if failed else 200,r.text)
            member=self.db.execute('SELECT role FROM photocraft.members WHERE project_id=%s AND email=%s',(pid,email)).fetchone()
            self.assertEqual(member,('edit',))
            delivery=self.db.execute('SELECT status FROM photocraft.invitation_deliveries WHERE project_id=%s AND email=%s',(pid,email)).fetchone()
            self.assertEqual(delivery,('failed' if failed else 'sent',))
            repeat=self.http.post(self.base+f'/api/projects/{pid}/invite',json={'email':email,'role':'edit'},headers=h)
            self.assertEqual(repeat.status_code,429)
        self.assertEqual(sum(path.startswith('/auth/v1/otp') for path in Provider.calls),2)

    def test_invitation_first_request_on_cold_workers(self):
        pid,h=self.invite_fixture()
        for failed in [False, True]:
            with self.subTest(provider_failed=failed):
                email=str(uuid.uuid4())+'@example.invalid'
                Provider.mode='mail_failed' if failed else 'success'
                with self.cold_worker() as base:
                    response=self.http.post(base+f'/api/projects/{pid}/invite',
                        json={'email':email,'role':'edit'},headers=h,timeout=20)
                self.assertEqual(response.status_code,502 if failed else 200,response.text)
                if failed:
                    self.assertIn('Access was granted',response.json()['error'])
                self.assertEqual(self.db.execute('SELECT role FROM photocraft.members WHERE project_id=%s AND email=%s',
                    (pid,email)).fetchone(),('edit',))
                self.assertEqual(self.db.execute('SELECT status FROM photocraft.invitation_deliveries WHERE project_id=%s AND email=%s',
                    (pid,email)).fetchone(),('failed' if failed else 'sent',))
                self.assertEqual(len(Provider.mail),2 if failed else 1)
                path,key,body=Provider.mail[-1]
                self.assertEqual(urlparse(path).path,'/auth/v1/otp')
                self.assertEqual(parse_qs(urlparse(path).query),{'redirect_to':[ORIGIN+'/auth/confirm']})
                self.assertEqual(key,'public-test-key')
                self.assertEqual(body,{'email':email,'create_user':True})

    def test_invitation_validates_owner_email_and_role_before_mail(self):
        pid,h=self.invite_fixture()
        Provider.calls=[]
        for payload in [{'email':'broken','role':'edit'},{'email':'a@b.test','role':'owner'}]:
            r=self.http.post(self.base+f'/api/projects/{pid}/invite',json=payload,headers=h)
            self.assertEqual(r.status_code,400)
        r=self.http.post(self.base+f'/api/projects/{pid}/invite',json={'email':'a@b.test','role':'edit'},headers={'Origin':ORIGIN})
        self.assertEqual(r.status_code,401)
        self.assertEqual(Provider.calls,[])


if __name__=='__main__':
    unittest.main(verbosity=2)
