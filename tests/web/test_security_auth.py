"""Loopback security regressions: authorization ordering, quotas and provider isolation.

The row-lock fixtures deliberately interleave synthetic requests at PostgreSQL
transaction boundaries. No deployed service, real user, or email is involved.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlparse
import uuid

import psycopg
import requests

import test_api as api
from benchmark_collaboration import local_database


def hold_project(lock, pid):
    # Match the canonical advisory-before-row barrier used by durable/live writers.
    lock.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ('photocraft.live.project:'+pid,))
    lock.execute('SELECT id FROM photocraft.projects WHERE id=%s FOR UPDATE', (pid,))


class SecurityMutations(unittest.TestCase):
    req = api.CloudContract.req
    project = api.CloudContract.project
    member = api.CloudContract.member
    begin = api.CloudContract.begin
    upload = api.CloudContract.upload
    tearDown = api.CloudContract.tearDown

    @classmethod
    def setUpClass(cls):
        local_database(api.DATABASE)
        api.CloudContract.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        api.CloudContract.tearDownClass.__func__(cls)

    def request(self, session, method, path, **kwargs):
        # Each concurrent caller gets its own transport while retaining the fixture identity.
        with requests.Session() as client:
            client.trust_env = False
            client.headers.update(session.headers)
            client.cookies.update(session.cookies)
            return client.request(method, api.BASE + path, timeout=15, **kwargs)

    def wait_for_lock_or_done(self, blocker, futures, minimum=1):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            waiting = self.db.execute(
                'SELECT count(*) FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))',
                (blocker,)).fetchone()[0]
            if waiting >= minimum or any(f.done() for f in futures):
                return waiting
            time.sleep(.01)
        self.fail('Synthetic request did not reach its controlled project lock')

    def denied_after_waiting_revocation(self, pid, method, path, **kwargs):
        """Access passes initially, then is removed by the transaction ahead of it."""
        with ThreadPoolExecutor(max_workers=1) as executor:
            with psycopg.connect(api.DATABASE) as lock:
                hold_project(lock, pid)
                pending = executor.submit(self.request, self.editor, method, path, **kwargs)
                self.wait_for_lock_or_done(lock.info.backend_pid, [pending])
                early = pending.done()
                # This is the same serialized membership deletion as the owner endpoint.
                lock.execute('DELETE FROM photocraft.members WHERE project_id=%s AND email=%s',
                             (pid, self.accounts[1][1]))
                lock.commit()
            response = pending.result(timeout=15)
        self.assertFalse(early, 'Durable action bypassed the project authorization boundary')
        self.assertEqual(response.status_code, 404, response.text[:250])

    def editable_project(self):
        pid = self.project()
        self.member(pid, 1, 'edit')
        return pid

    def upload_payload(self):
        return {'base_revision': 0, 'bytes': len(self.fixture), 'parts': 1,
                'sha256': hashlib.sha256(self.fixture).hexdigest(), 'title': 'Security fixture',
                'width': 64, 'height': 48}

    def test_01_commit_rechecks_access_after_project_lock(self):
        pid = self.editable_project()
        uid = self.begin(pid, session=self.editor)
        self.req(self.editor, 'PUT', f'/api/uploads/{uid}/0', data=self.fixture)
        self.denied_after_waiting_revocation(pid, 'POST', f'/api/uploads/{uid}/commit', json={})
        self.assertEqual(self.req(self.owner, 'GET', f'/api/projects/{pid}').json()['revision'], 0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.uploads WHERE id=%s', (uid,)).fetchone()[0], 1)
        # The author can still discard a failed reservation after losing membership.
        self.req(self.editor, 'DELETE', f'/api/uploads/{uid}')

    def test_02_thumbnail_rechecks_access_after_project_lock(self):
        pid = self.editable_project()
        import base64
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a8ZkAAAAASUVORK5CYII=')
        self.denied_after_waiting_revocation(pid, 'PUT', f'/api/projects/{pid}/thumbnail', data=png)
        self.assertIsNone(self.db.execute('SELECT thumbnail FROM photocraft.projects WHERE id=%s', (pid,)).fetchone()[0])

    def test_03_reservation_rechecks_access_after_project_lock(self):
        pid = self.editable_project()
        self.denied_after_waiting_revocation(pid, 'POST', f'/api/projects/{pid}/uploads', json=self.upload_payload())
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.uploads WHERE project_id=%s', (pid,)).fetchone()[0], 0)

    def test_04_chunk_rechecks_access_after_project_lock(self):
        pid = self.editable_project()
        uid = self.begin(pid, session=self.editor)
        self.denied_after_waiting_revocation(pid, 'PUT', f'/api/uploads/{uid}/0', data=self.fixture)
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.chunks WHERE upload_id=%s', (uid,)).fetchone()[0], 0)

    def test_05_duplicate_rechecks_access_after_project_lock(self):
        pid = self.editable_project()
        self.denied_after_waiting_revocation(pid, 'POST', f'/api/projects/{pid}/duplicate', json={})
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.projects WHERE owner_id=%s', (self.accounts[1][0],)).fetchone()[0], 0)

    def test_06_comment_rechecks_access_after_project_lock(self):
        pid = self.editable_project()
        self.denied_after_waiting_revocation(pid, 'POST', f'/api/projects/{pid}/comments', json={'body': 'Should not be stored'})
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.comments WHERE project_id=%s', (pid,)).fetchone()[0], 0)

    def test_07_resolve_rechecks_access_after_project_lock(self):
        pid = self.editable_project()
        cid = self.req(self.owner, 'POST', f'/api/projects/{pid}/comments', json={'body': 'Unresolved'}).json()['id']
        self.denied_after_waiting_revocation(pid, 'PUT', f'/api/projects/{pid}/comments/{cid}', json={'resolved': True})
        self.assertFalse(self.db.execute('SELECT resolved FROM photocraft.comments WHERE id=%s', (cid,)).fetchone()[0])

    def test_08_owner_mutations_share_project_serialization(self):
        pid = self.editable_project()
        calls = [('PUT', f'/api/projects/{pid}/members', {'json': {'email': self.accounts[1][1], 'role': 'remove'}}),
                 ('PATCH', f'/api/projects/{pid}', {'json': {'trashed': True}}),
                 ('DELETE', f'/api/projects/{pid}/share', {})]
        for method, path, kwargs in calls:
            with self.subTest(method=method, path=path), ThreadPoolExecutor(max_workers=1) as executor:
                with psycopg.connect(api.DATABASE) as lock:
                    hold_project(lock, pid)
                    pending = executor.submit(self.request, self.owner, method, path, **kwargs)
                    self.wait_for_lock_or_done(lock.info.backend_pid, [pending])
                    early = pending.done()
                    lock.commit()
                response = pending.result(timeout=15)
                self.assertFalse(early, 'Owner change bypassed the project serialization boundary')
                self.assertEqual(response.status_code, 200, response.text[:250])

    def test_09_concurrent_link_rotation_leaves_only_one_valid_token(self):
        pid = self.project()
        with ThreadPoolExecutor(max_workers=2) as executor:
            with psycopg.connect(api.DATABASE) as lock:
                hold_project(lock, pid)
                pending = [executor.submit(self.request, self.owner, 'POST', f'/api/projects/{pid}/share') for _ in range(2)]
                self.wait_for_lock_or_done(lock.info.backend_pid, pending, minimum=2)
                lock.commit()
            replies = [f.result(timeout=15) for f in pending]
        self.assertEqual([r.status_code for r in replies], [200, 200])
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.shares WHERE project_id=%s', (pid,)).fetchone()[0], 1)
        tokens = [parse_qs(urlparse(r.json()['url']).query)['share'][0] for r in replies]
        self.assertEqual(sorted(self.request(self.anon, 'GET', f'/api/share/{t}').status_code for t in tokens), [200, 404])
        self.req(self.owner, 'DELETE', f'/api/projects/{pid}/share')
        self.assertEqual([self.request(self.anon, 'GET', f'/api/share/{t}').status_code for t in tokens], [404, 404])

    def test_10_member_limit_is_atomic_and_allows_existing_role_changes(self):
        pid = self.project()
        self.db.execute("INSERT INTO photocraft.members(project_id,email,role) SELECT %s,'member-'||n||'@example.invalid','view' FROM generate_series(1,99) n", (pid,))
        with ThreadPoolExecutor(max_workers=2) as executor:
            replies = list(executor.map(lambda i: self.request(self.owner, 'PUT', f'/api/projects/{pid}/members',
                json={'email': f'last-{i}@example.invalid', 'role': 'edit'}), range(2)))
        self.assertEqual(sorted(r.status_code for r in replies), [200, 400])
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.members WHERE project_id=%s', (pid,)).fetchone()[0], 100)
        self.req(self.owner, 'PUT', f'/api/projects/{pid}/members', json={'email': 'member-1@example.invalid', 'role': 'edit'})
        self.req(self.owner, 'PUT', f'/api/projects/{pid}/members', json={'email': 'member-2@example.invalid', 'role': 'remove'})
        self.req(self.owner, 'PUT', f'/api/projects/{pid}/members', json={'email': 'replacement@example.invalid', 'role': 'view'})

    def test_11_comment_limit_is_atomic(self):
        pid = self.project()
        with self.db.cursor() as cur:
            cur.executemany('INSERT INTO photocraft.comments(id,project_id,author_id,body) VALUES(%s,%s,%s,%s)',
                            [(str(uuid.uuid4()), pid, self.accounts[0][0], 'Synthetic comment') for _ in range(999)])
        with ThreadPoolExecutor(max_workers=2) as executor:
            with psycopg.connect(api.DATABASE) as lock:
                hold_project(lock, pid)
                pending = [executor.submit(self.request, self.owner, 'POST', f'/api/projects/{pid}/comments',
                           json={'body': f'Last {i}'}) for i in range(2)]
                self.wait_for_lock_or_done(lock.info.backend_pid, pending, minimum=2)
                lock.commit()
            replies = [f.result(timeout=15) for f in pending]
        self.assertEqual(sorted(r.status_code for r in replies), [200, 400])
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.comments WHERE project_id=%s', (pid,)).fetchone()[0], 1000)


    @contextmanager
    def additional_editor_session(self):
        token = secrets.token_hex(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)', (digest, self.accounts[1][0]))
        with requests.Session() as client:
            client.trust_env = False
            client.headers['Origin'] = api.BASE
            client.cookies.set('pc_session', token)
            try:
                yield client, digest
            finally:
                self.db.execute('DELETE FROM photocraft.sessions WHERE hash=%s', (digest,))

    def test_15_commit_rechecks_session_expiry_after_project_lock(self):
        pid = self.editable_project()
        with self.additional_editor_session() as (client, digest):
            uid = self.begin(pid, session=client)
            self.req(client, 'PUT', f'/api/uploads/{uid}/0', data=self.fixture)
            with ThreadPoolExecutor(max_workers=1) as executor:
                with psycopg.connect(api.DATABASE) as lock:
                    hold_project(lock, pid)
                    pending = executor.submit(self.request, client, 'POST', f'/api/uploads/{uid}/commit', json={})
                    self.assertEqual(self.wait_for_lock_or_done(lock.info.backend_pid, [pending]), 1)
                    # This timestamp is after the waiting transaction began. now()
                    # would incorrectly admit it; clock_timestamp() must deny it.
                    self.db.execute('UPDATE photocraft.sessions SET expires_at=clock_timestamp() WHERE hash=%s', (digest,))
                    lock.commit()
                response = pending.result(timeout=15)
            self.assertEqual(response.status_code, 401, response.text[:250])
            self.assertEqual(self.req(self.owner, 'GET', f'/api/projects/{pid}').json()['revision'], 0)
            self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.uploads WHERE id=%s', (uid,)).fetchone()[0], 1)

    def test_16_completed_logout_denies_queued_comment_without_deadlock(self):
        pid = self.editable_project()
        with self.additional_editor_session() as (client, _):
            with ThreadPoolExecutor(max_workers=1) as executor:
                with psycopg.connect(api.DATABASE) as lock:
                    hold_project(lock, pid)
                    pending = executor.submit(self.request, client, 'POST', f'/api/projects/{pid}/comments',
                                              json={'body': 'Must not outlive logout'})
                    self.assertEqual(self.wait_for_lock_or_done(lock.info.backend_pid, [pending]), 1)
                    logged_out = self.request(client, 'POST', '/api/logout', json={})
                    self.assertEqual(logged_out.status_code, 200, logged_out.text)
                    self.assertFalse(pending.done(), 'Comment should still be waiting for its project lock')
                    lock.commit()
                response = pending.result(timeout=15)
            self.assertEqual(response.status_code, 401, response.text[:250])
            self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.comments WHERE project_id=%s', (pid,)).fetchone()[0], 0)


    def test_17_create_project_rechecks_session_after_account_lock(self):
        with self.additional_editor_session() as (client, digest), ThreadPoolExecutor(max_workers=1) as executor:
            before = self.db.execute('SELECT count(*) FROM photocraft.projects WHERE owner_id=%s', (self.accounts[1][0],)).fetchone()[0]
            with psycopg.connect(api.DATABASE) as lock:
                lock.execute('SELECT id FROM photocraft.accounts WHERE id=%s FOR UPDATE', (self.accounts[1][0],))
                pending = executor.submit(self.request, client, 'POST', '/api/projects', json={'title': 'Expired creation'})
                self.assertEqual(self.wait_for_lock_or_done(lock.info.backend_pid, [pending]), 1)
                self.db.execute('UPDATE photocraft.sessions SET expires_at=clock_timestamp() WHERE hash=%s', (digest,))
                lock.commit()
            response = pending.result(timeout=15)
            self.assertEqual(response.status_code, 401, response.text[:250])
            self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.projects WHERE owner_id=%s', (self.accounts[1][0],)).fetchone()[0], before)

    def test_18_cancel_upload_rechecks_session_after_upload_lock(self):
        pid = self.editable_project()
        with self.additional_editor_session() as (client, digest), ThreadPoolExecutor(max_workers=1) as executor:
            uid = self.begin(pid, session=client)
            with psycopg.connect(api.DATABASE) as lock:
                lock.execute('SELECT id FROM photocraft.uploads WHERE id=%s FOR UPDATE', (uid,))
                pending = executor.submit(self.request, client, 'DELETE', f'/api/uploads/{uid}')
                self.assertEqual(self.wait_for_lock_or_done(lock.info.backend_pid, [pending]), 1)
                self.db.execute('UPDATE photocraft.sessions SET expires_at=clock_timestamp() WHERE hash=%s', (digest,))
                lock.commit()
            response = pending.result(timeout=15)
            self.assertEqual(response.status_code, 401, response.text[:250])
            self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.uploads WHERE id=%s', (uid,)).fetchone()[0], 1)

    def test_19_project_mutations_recheck_session_after_secondary_locks(self):
        for action in ('duplicate', 'reservation', 'chunk', 'commit'):
            with self.subTest(action=action):
                pid = self.editable_project()
                with self.additional_editor_session() as (client, digest), ThreadPoolExecutor(max_workers=1) as executor:
                    uid = self.begin(pid, session=client) if action in ('chunk', 'commit') else None
                    if action == 'commit':
                        self.req(client, 'PUT', f'/api/uploads/{uid}/0', data=self.fixture)
                    method, path, kwargs = {
                        'duplicate': ('POST', f'/api/projects/{pid}/duplicate', {}),
                        'reservation': ('POST', f'/api/projects/{pid}/uploads', {'json': self.upload_payload()}),
                        'chunk': ('PUT', f'/api/uploads/{uid}/0', {'data': self.fixture}),
                        'commit': ('POST', f'/api/uploads/{uid}/commit', {'json': {}}),
                    }[action]
                    with psycopg.connect(api.DATABASE) as lock:
                        if action in ('duplicate', 'commit'):
                            who = 1 if action == 'duplicate' else 0
                            lock.execute('SELECT id FROM photocraft.accounts WHERE id=%s FOR UPDATE', (self.accounts[who][0],))
                        elif action == 'chunk':
                            lock.execute('SELECT id FROM photocraft.uploads WHERE id=%s FOR UPDATE', (uid,))
                        else:
                            lock.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                                         ('photocraft.uploads/'+self.accounts[1][0],))
                        pending = executor.submit(self.request, client, method, path, **kwargs)
                        self.assertEqual(self.wait_for_lock_or_done(lock.info.backend_pid, [pending]), 1)
                        self.db.execute('UPDATE photocraft.sessions SET expires_at=clock_timestamp() WHERE hash=%s', (digest,))
                        lock.commit()
                    response = pending.result(timeout=15)
                    self.assertEqual(response.status_code, 401, response.text[:250])
                    self.assertEqual(self.req(self.owner, 'GET', f'/api/projects/{pid}').json()['revision'], 0)
                    if action == 'reservation':
                        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.uploads WHERE project_id=%s', (pid,)).fetchone()[0], 0)
                    elif action == 'chunk':
                        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.chunks WHERE upload_id=%s', (uid,)).fetchone()[0], 0)


class SlowProvider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, value):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def hold(self):
        with self.server.condition:
            self.server.entered += 1
            self.server.condition.notify_all()
        self.server.release.wait(timeout=10)

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if self.path.startswith('/auth/v1/otp'):
            with self.server.condition:
                self.server.mail_count += 1
            self.respond(200, {})
        elif self.server.stage == 'verify':
            self.hold()
            self.respond(403, {'error': 'Synthetic expired token'})
        else:
            self.respond(200, {'access_token': 'synthetic-provider-only'})

    def do_GET(self):
        self.hold()
        self.respond(401, {'error': 'Synthetic rejected identity'})


@contextmanager
def provider_worker():
    local_database(api.DATABASE)
    provider = ThreadingHTTPServer(('127.0.0.1', 0), SlowProvider)
    provider.condition = threading.Condition()
    provider.release = threading.Event()
    provider.stage, provider.entered, provider.mail_count = 'verify', 0, 0
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    tag = 'photocraft-security-' + uuid.uuid4().hex
    database = api.DATABASE + ('&' if '?' in api.DATABASE else '?') + 'application_name=' + tag
    binary = Path(os.environ['PHOTOCRAFT_CLOUD_BIN']).resolve()
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen([str(binary)], env={**os.environ, 'DATABASE_URL': database,
            'CLOUD_LOCAL_DEV': '1', 'APP_ORIGIN': base, 'PORT': str(port),
            'SUPABASE_URL': f'http://127.0.0.1:{provider.server_port}', 'SUPABASE_ANON_KEY': 'public-synthetic'},
            stdout=log, stderr=log)
        try:
            with requests.Session() as client:
                client.trust_env = False
                deadline = time.monotonic() + 8
                while True:
                    try:
                        if client.get(base + '/api/config', timeout=.5).json().get('cloud'):
                            break
                    except (requests.RequestException, ValueError):
                        pass
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Synthetic provider worker did not initialize')
                    time.sleep(.05)
            yield base, tag, provider
        finally:
            provider.release.set()
            process.terminate()
            process.wait(timeout=5)
            provider.shutdown()
            provider.server_close()
            thread.join(timeout=5)


class ProviderIsolation(unittest.TestCase):
    def test_12_stalled_verify_and_user_calls_do_not_hold_database_transactions(self):
        with provider_worker() as (base, tag, provider), psycopg.connect(api.DATABASE, autocommit=True) as db:
            ident, token = str(uuid.uuid4()), secrets.token_hex(32)
            db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)', (ident, ident+'@example.invalid', 'Pool fixture'))
            db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)', (hashlib.sha256(token.encode()).hexdigest(), ident))
            try:
                for stage in ('verify', 'user'):
                    with self.subTest(stage=stage), ThreadPoolExecutor(max_workers=5) as executor:
                        provider.stage, provider.entered = stage, 0
                        provider.release.clear()
                        def confirm(_):
                            with requests.Session() as client:
                                client.trust_env = False
                                return client.post(base+'/auth/confirm', headers={'Origin': base},
                                    data={'token_hash': secrets.token_hex(32), 'type': 'email'}, timeout=12)
                        pending = [executor.submit(confirm, i) for i in range(5)]
                        try:
                            deadline = time.monotonic()+4
                            while time.monotonic() < deadline:
                                with provider.condition:
                                    if provider.entered + sum(f.done() for f in pending) >= 5:
                                        break
                                time.sleep(.01)
                            self.assertGreater(provider.entered, 0, 'No request reached the synthetic provider')
                            held = db.execute("SELECT count(*) FROM pg_stat_activity WHERE application_name=%s AND state='idle in transaction'", (tag,)).fetchone()[0]
                            self.assertEqual(held, 0, 'External provider latency occupied database transactions')
                            with requests.Session() as client:
                                client.trust_env = False
                                for path in ('/api/me', '/api/projects'):
                                    response = client.get(base+path, cookies={'pc_session': token}, timeout=2)
                                    self.assertEqual(response.status_code, 200, response.text[:100])
                        finally:
                            provider.release.set()
                            replies = [f.result(timeout=12) for f in pending]
                        self.assertTrue(all(r.status_code in (400, 429, 503) for r in replies))
            finally:
                db.execute('DELETE FROM photocraft.sessions WHERE account_id=%s', (ident,))
                db.execute('DELETE FROM photocraft.accounts WHERE id=%s', (ident,))


@contextmanager
def synthetic_owners(db, base, count=2):
    accounts = []
    try:
        for _ in range(count):
            ident, token, pid = str(uuid.uuid4()), secrets.token_hex(32), str(uuid.uuid4())
            db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                       (ident, ident+'@example.invalid', 'Invitation fixture'))
            db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                       (hashlib.sha256(token.encode()).hexdigest(), ident))
            db.execute('INSERT INTO photocraft.projects(id,owner_id,title) VALUES(%s,%s,%s)',
                       (pid, ident, 'Synthetic invitation'))
            client = requests.Session()
            client.trust_env = False
            client.headers['Origin'] = base
            client.cookies.set('pc_session', token)
            accounts.append((ident, pid, client))
        yield accounts
    finally:
        for ident, _, client in accounts:
            client.close()
            db.execute('DELETE FROM photocraft.projects WHERE owner_id=%s', (ident,))
            db.execute('DELETE FROM photocraft.sessions WHERE account_id=%s', (ident,))
            db.execute('DELETE FROM photocraft.accounts WHERE id=%s', (ident,))


class InvitationSecurity(unittest.TestCase):
    def test_13_recipient_cooldown_serializes_across_different_owners(self):
        with provider_worker() as (base, _, provider), psycopg.connect(api.DATABASE, autocommit=True) as db:
            with synthetic_owners(db, base) as owners, ThreadPoolExecutor(max_workers=2) as executor:
                email = str(uuid.uuid4())+'@example.invalid'
                with psycopg.connect(api.DATABASE) as lock:
                    for _, pid, _ in owners:
                        hold_project(lock, pid)
                    pending = [executor.submit(client.post, base+f'/api/projects/{pid}/invite',
                               json={'email': email, 'role': 'edit'}, timeout=15) for _, pid, client in owners]
                    deadline = time.monotonic()+4
                    while time.monotonic() < deadline:
                        waiting = db.execute('SELECT count(*) FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))',
                                             (lock.info.backend_pid,)).fetchone()[0]
                        if waiting >= 2 or any(f.done() for f in pending):
                            break
                        time.sleep(.01)
                    self.assertEqual(waiting, 2, 'Both synthetic invitation transactions must reach the barrier')
                    lock.commit()
                replies = [f.result(timeout=15) for f in pending]
                self.assertEqual(sorted(r.status_code for r in replies), [200, 429], [r.text for r in replies])
                self.assertEqual(provider.mail_count, 1, 'Only one fake-provider send is allowed per recipient cooldown')
                self.assertEqual(db.execute('SELECT count(*) FROM photocraft.invitation_deliveries WHERE email=%s', (email,)).fetchone()[0], 1)

    def test_14_full_project_can_reinvite_existing_member_but_not_add_another(self):
        with provider_worker() as (base, _, provider), psycopg.connect(api.DATABASE, autocommit=True) as db:
            with synthetic_owners(db, base, count=1) as owners:
                _, pid, client = owners[0]
                db.execute("INSERT INTO photocraft.members(project_id,email,role) SELECT %s,'existing-'||n||'@example.invalid','view' FROM generate_series(1,100) n", (pid,))
                blocked = client.post(base+f'/api/projects/{pid}/invite', json={'email': 'overflow@example.invalid', 'role': 'view'}, timeout=5)
                self.assertEqual(blocked.status_code, 400)
                self.assertEqual(provider.mail_count, 0)
                updated = client.post(base+f'/api/projects/{pid}/invite', json={'email': 'existing-1@example.invalid', 'role': 'edit'}, timeout=5)
                self.assertEqual(updated.status_code, 200, updated.text)
                self.assertEqual(provider.mail_count, 1)
                self.assertEqual(db.execute('SELECT count(*) FROM photocraft.members WHERE project_id=%s', (pid,)).fetchone()[0], 100)
                self.assertEqual(db.execute('SELECT role FROM photocraft.members WHERE project_id=%s AND email=%s', (pid, 'existing-1@example.invalid')).fetchone()[0], 'edit')


if __name__ == '__main__':
    unittest.main()
