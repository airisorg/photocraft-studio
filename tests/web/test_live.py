"""Live-state HTTP authority, ordering and expiry tests against loopback only.

Uses the existing CloudContract fixture lifecycle, never a test-auth route. Run with
PHOTOCRAFT_TEST_ORIGIN, PHOTOCRAFT_TEST_DATABASE_URL and PHOTOCRAFT_FIXTURE set as
for test_api.py. No server is started; only this suite's synthetic accounts are removed.
"""
import hashlib
import secrets
import time
import unittest
import uuid

import requests

import test_api as api


class LiveContract(unittest.TestCase):
    req = api.CloudContract.req
    project = api.CloudContract.project
    member = api.CloudContract.member
    begin = api.CloudContract.begin
    upload = api.CloudContract.upload
    tearDown = api.CloudContract.tearDown

    @classmethod
    def setUpClass(cls):
        api.CloudContract.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        api.CloudContract.tearDownClass.__func__(cls)

    def setUp(self):
        self.pid = self.project()
        self.upload(self.pid)
        self.member(self.pid, 1, 'edit')
        self.member(self.pid, 2, 'view')
        self.tab = str(uuid.uuid4())
        self.path = f'/api/projects/{self.pid}/live'
        self.read_path = self.path+'?tab='+str(uuid.uuid4())

    def state(self, seq=1, tab=None, **changes):
        return {'tab': tab or self.tab, 'seq': seq, 'baseRevision': 1,
                'cursor': {'x': 20, 'y': 20}, 'gesture': None, **changes}

    def put(self, body=None, session=None, status=200, **kwargs):
        response = self.req(session or self.owner, 'PUT', self.path, status=status,
                            json=self.state() if body is None else body, **kwargs)
        return response.json() if status == 200 else response

    def peers(self, session=None, tab=None):
        query = '?tab='+(tab or str(uuid.uuid4()))
        result = self.req(session or self.owner, 'GET', self.path+query).json()
        self.assertEqual(result['revision'], 1)
        return result['peers']

    def test_01_session_origin_and_expected_account(self):
        for session in [self.anon, self.outsider]:
            expected = 401 if session is self.anon else 404
            self.req(session, 'GET', self.read_path, status=expected)
            self.put(session=session, status=expected)
        for origin in ['', 'null', 'https://outside.invalid']:
            self.put(status=403, headers={'Origin': origin})
        self.put(status=401, headers={'X-Photocraft-Account': self.accounts[1][0]})
        self.req(self.owner, 'GET', self.read_path, status=401,
                 headers={'X-Photocraft-Account': self.accounts[1][0]})
        self.assertEqual(self.peers(), [])
        self.assertTrue(self.put(headers={'X-Photocraft-Account': self.accounts[0][0]})['accepted'])

    def test_02_viewer_cursor_but_no_gesture(self):
        self.assertTrue(self.put(session=self.viewer)['accepted'])
        self.assertEqual(self.peers()[0]['actor'], self.accounts[2][0])
        gesture = {'events': [{'gesture': 1, 'sequence': 1, 'kind': 'cancel'}]}
        self.put(self.state(2, gesture=gesture), session=self.viewer, status=403)
        self.assertTrue(self.put(self.state(2, gesture=gesture), session=self.editor)['accepted'])

    def test_03_duplicate_old_sequence_and_clear_tombstone(self):
        self.assertTrue(self.put(self.state(10))['accepted'])
        for seq in [10, 9]:
            response = self.put(self.state(seq, cursor={'x': 40, 'y': 40}))
            self.assertFalse(response['accepted'])
            self.assertEqual(response['seq'], 10)
            self.assertEqual(self.peers()[0]['cursor'], {'x': 20.0, 'y': 20.0})
        self.assertTrue(self.put(self.state(11, cursor=None, gesture=None))['accepted'])
        self.assertEqual(self.peers(), [])
        self.assertFalse(self.put(self.state(10))['accepted'])
        self.assertEqual(self.peers(), [])

    def test_04_tab_exclusion_is_scoped_to_authenticated_session(self):
        self.put(self.state(1))
        self.put(self.state(1), session=self.editor)
        self.assertEqual([p['actor'] for p in self.peers(tab=self.tab)], [self.accounts[1][0]])
        self.assertEqual([p['actor'] for p in self.peers(self.editor, self.tab)], [self.accounts[0][0]])
        second_tab = str(uuid.uuid4())
        self.assertTrue(self.put(self.state(1, tab=second_tab))['accepted'])
        self.assertEqual(len(self.peers(self.viewer)), 3)

    def test_05_stale_writes_do_not_extend_ttl_or_reset_watermark(self):
        self.put(self.state(10))
        time.sleep(1.2)
        self.assertFalse(self.put(self.state(10))['accepted'])
        self.put(self.state(11, baseRevision=2, gesture={'events': [{'gesture': 1, 'sequence': 1, 'kind': 'cancel'}]}), status=409)
        time.sleep(1.1)
        self.assertEqual(self.peers(), [], 'Duplicate state incorrectly extended the two-second TTL')
        self.assertFalse(self.put(self.state(9))['accepted'])
        self.assertEqual(self.peers(), [], 'Expired lower sequence resurrected old state')
        self.assertTrue(self.put(self.state(11))['accepted'])
        self.assertEqual(len(self.peers()), 1)

    def test_06_membership_revocation_filters_existing_state_and_requests(self):
        self.put(session=self.editor)
        self.assertEqual(len(self.peers()), 1)
        self.member(self.pid, 1, 'remove')
        self.assertEqual(self.peers(), [], 'Revoked author state leaked through a fresh read')
        self.put(self.state(2), session=self.editor, status=404)
        self.req(self.editor, 'GET', self.read_path, status=404)

    def test_07_session_expiry_filters_state_without_waiting_for_ttl(self):
        token = secrets.token_hex(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                        (digest, self.accounts[0][0]))
        session = requests.Session()
        session.trust_env = False
        session.headers['Origin'] = api.BASE
        session.cookies.set('pc_session', token)
        try:
            self.put(session=session)
            self.assertEqual(len(self.peers()), 1)
            self.db.execute('DELETE FROM photocraft.sessions WHERE hash=%s', (digest,))
            self.put(self.state(2), session=session, status=401)
            self.req(session, 'GET', self.read_path, status=401)
            self.assertEqual(self.peers(), [], 'Expired session state leaked through a fresh read')
        finally:
            session.close()
            self.db.execute('DELETE FROM photocraft.sessions WHERE hash=%s', (digest,))

    def test_08_base_revision_and_project_switch_watermark(self):
        self.put(self.state(baseRevision=0), status=400)
        self.put(self.state(baseRevision=2, gesture={'events': [{'gesture': 1, 'sequence': 1, 'kind': 'cancel'}]}), status=409)
        pending = self.project()
        self.req(self.owner, 'PUT', f'/api/projects/{pending}/live', status=409, json=self.state())
        self.put(self.state(10))
        second = self.project()
        self.upload(second)
        response = self.req(self.owner, 'PUT', f'/api/projects/{second}/live', json=self.state(9)).json()
        self.assertFalse(response['accepted'], 'A project switch reset the session/tab watermark')
        response = self.req(self.owner, 'PUT', f'/api/projects/{second}/live', json=self.state(11)).json()
        self.assertTrue(response['accepted'])

    def test_09_bounds_fail_without_replacing_valid_state(self):
        self.put(self.state(1))
        malformed = [self.state(2, tab='bad'), self.state(-1), self.state(2, cursor={'x': 'nan', 'y': 1}),
                     self.state(2, gesture={'events': [{'gesture': 1, 'sequence': i+1, 'kind': 'cancel'} for i in range(257)]}),
                     self.state(2, gesture={'events': [{'gesture': 1, 'sequence': 1, 'kind': 'points',
                         'points': [[1, 1, 1, 0, 0, 0]]*1025}]})]
        for value in malformed:
            with self.subTest(value=str(value)[:80]):
                self.put(value, status=400)
        self.req(self.owner, 'PUT', self.path, status=400, data='{"tab":',
                 headers={'Content-Type': 'application/json'})
        self.put(self.state(2, padding='x'*66000), status=413)
        self.assertEqual(self.peers()[0]['seq'], 1)

    def test_10_transient_state_never_changes_saved_project(self):
        before = self.req(self.owner, 'GET', f'/api/projects/{self.pid}').json()
        self.put()
        self.put(self.state(2, gesture={'events': [{'gesture': 1, 'sequence': 1, 'kind': 'cancel'}]}))
        self.put(self.state(3, cursor=None, gesture=None))
        after = self.req(self.owner, 'GET', f'/api/projects/{self.pid}').json()
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(after['content']['sha256'], before['content']['sha256'])
        self.assertEqual(self.req(self.owner, 'GET', f'/api/projects/{self.pid}/content?part=0').content, self.fixture)
        self.assertEqual(len(self.req(self.owner, 'GET', f'/api/projects/{self.pid}/versions').json()), 1)
        self.req(self.owner, 'PATCH', f'/api/projects/{self.pid}', json={'trashed': True})
        self.req(self.owner, 'GET', self.read_path, status=404)
        self.put(self.state(4), status=404)

    def test_11_cursor_survives_revision_changes_but_old_gesture_is_hidden(self):
        self.put(self.state(1, gesture={'events': [{'gesture': 1, 'sequence': 1, 'kind': 'cancel'}]}), session=self.editor)
        self.upload(self.pid, base=1)
        response = self.req(self.owner, 'GET', self.read_path).json()
        self.assertEqual(response['revision'], 2)
        self.assertEqual(len(response['peers']), 1)
        self.assertEqual(response['peers'][0]['cursor'], {'x': 20.0, 'y': 20.0})
        self.assertIsNone(response['peers'][0]['gesture'])
        self.assertTrue(self.put(self.state(2, baseRevision=1), session=self.editor)['accepted'])
        self.put(self.state(3, baseRevision=1, gesture={'events': [{'gesture': 1, 'sequence': 1, 'kind': 'cancel'}]}),
                 session=self.editor, status=409)

    def test_12_active_slot_cap_allows_updates_and_clear_frees_capacity(self):
        token = secrets.token_hex(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)', (digest, self.accounts[0][0]))
        session = requests.Session()
        session.trust_env = False
        session.headers['Origin'] = api.BASE
        session.cookies.set('pc_session', token)
        tabs = [str(uuid.uuid4()) for _ in range(9)]
        try:
            for tab in tabs[:8]:
                self.assertTrue(self.put(self.state(tab=tab), session=session)['accepted'])
            self.assertTrue(self.put(self.state(2, tab=tabs[0]), session=session)['accepted'])
            self.put(self.state(tab=tabs[8]), session=session, status=429)
            self.assertTrue(self.put(self.state(3, tab=tabs[0], cursor=None), session=session)['accepted'])
            self.assertTrue(self.put(self.state(tab=tabs[8]), session=session)['accepted'])
            self.assertEqual(len(self.peers()), 8)
        finally:
            session.close()
            self.db.execute('DELETE FROM photocraft.sessions WHERE hash=%s', (digest,))


if __name__ == '__main__':
    unittest.main()
