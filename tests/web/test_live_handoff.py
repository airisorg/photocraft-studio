"""Loopback-only HTTP proof for pointer-exit metadata during canonical handoff.

All test identities belong to the existing synthetic fixture. Stored native End
payloads remain hidden after revision advance; tests inspect metadata and lease
invariants, while test_live_browser covers actual native pixels and pointer input.
"""
import hashlib
import unittest
import uuid

from test_live_scale import LiveScale


class LiveHandoff(LiveScale):
    def setUp(self):
        super().setUp()
        # Active-tab capacity is session-global, including other projects. Use
        # an owned per-case editor session so one test's2s lease cannot fill another.
        self.editor, _ = self.actor(account=1)

    @staticmethod
    def ended(kind='end'):
        event = {'gesture': 7, 'sequence': 1, 'kind': kind}
        if kind == 'unavailable':
            event['reason'] = 'fixture unavailable'
        return {'events': [event]}

    def stored(self, session=None, tab=None):
        digest = hashlib.sha256((session or self.editor).cookies.get('pc_session').encode()).hexdigest()
        return self.db.execute(
            'SELECT seq,project_id,base_revision,cursor,gesture,seen_at FROM photocraft.live_previews WHERE session_hash=%s AND tab_id=%s',
            (digest, tab or self.tab)).fetchone()

    def snapshots(self):
        read = self.req(self.owner, 'GET', self.read_path).json()
        exchange = self.put(self.state(tab=str(uuid.uuid4()), baseRevision=2, cursor=None))
        self.assertTrue(exchange['accepted'])
        self.assertEqual((read['revision'], exchange['revision']), (2, 2))
        return read['peers'], exchange['peers']

    def handoff(self, session=None, tab=None):
        session = session or self.editor
        self.assertTrue(self.put(self.state(tab=tab, cursor=None, gesture=self.ended()), session=session)['accepted'])
        original = self.stored(session, tab)
        self.upload(self.pid, base=1)
        return original

    def assert_metadata(self, snapshots, actor=None, tab=None):
        for peers in snapshots:
            matches = [p for p in peers if p['actor'] == (actor or self.accounts[1][0]) and p['tab'] == (tab or self.tab)]
            self.assertEqual(len(matches), 1, 'Authorized completed peer disappeared during canonical handoff')
            peer = matches[0]
            self.assertIsNone(peer['cursor'], 'Metadata retention fabricated a cursor')
            self.assertIsNone(peer['gesture'], 'An obsolete gesture was exposed')
            self.assertEqual(peer['baseRevision'], 1)
            self.assertGreater(peer['ttlMs'], 0)
            self.assertLessEqual(peer['ttlMs'], 2000)

    def test_handoff_get_put_keep_metadata_and_clears_never_renew_lease(self):
        original = self.handoff()
        self.assert_metadata(self.snapshots())
        before = self.req(self.owner, 'GET', f'/api/projects/{self.pid}').json()
        last_seq = 1
        for seq in [2, 3, 3, 2, 4]:
            reply = self.put(self.state(seq, baseRevision=2, cursor=None), session=self.editor)
            self.assertEqual(reply['accepted'], seq > last_seq)
            last_seq = max(last_seq, seq)
            stored = self.stored()
            self.assertEqual(stored[0], last_seq)
            self.assertEqual(stored[1:], original[1:], 'Clear/replay changed the admitted payload or original lease')
            self.assert_metadata(self.snapshots())
        after = self.req(self.owner, 'GET', f'/api/projects/{self.pid}').json()
        self.assertEqual((before['revision'], before['content']['sha256']), (after['revision'], after['content']['sha256']))
        digest = hashlib.sha256(self.editor.cookies.get('pc_session').encode()).hexdigest()
        self.db.execute("UPDATE photocraft.live_previews SET seen_at=clock_timestamp()-interval '3 seconds' WHERE session_hash=%s AND tab_id=%s", (digest, self.tab))
        self.assertEqual(self.snapshots(), ([], []))
        self.assertTrue(self.put(self.state(5, baseRevision=2, cursor=None), session=self.editor)['accepted'])
        self.assertIsNone(self.stored()[4], 'An expired End was retained by a fresh empty write')
        self.assertEqual(self.snapshots(), ([], []))

    def test_handoff_downgrade_and_revocation_remove_metadata(self):
        self.handoff()
        self.assert_metadata(self.snapshots())
        self.member(self.pid, 1, 'view')
        self.assertEqual(self.snapshots(), ([], []))
        self.member(self.pid, 1, 'edit')
        self.assert_metadata(self.snapshots())
        self.member(self.pid, 1, 'remove')
        self.assertEqual(self.snapshots(), ([], []))
        self.put(self.state(2, baseRevision=2, cursor=None), session=self.editor, status=404)

    def test_handoff_viewer_empty_update_cannot_retain_editing_payload(self):
        self.handoff()
        self.member(self.pid, 1, 'view')
        self.assertTrue(self.put(self.state(2, baseRevision=2, cursor=None), session=self.editor)['accepted'])
        self.assertIsNone(self.stored()[4], 'Viewer clear retained editing payload')
        self.member(self.pid, 1, 'edit')
        self.assertEqual(self.snapshots(), ([], []))

    def test_handoff_viewer_cursor_ahead_hides_old_gesture_and_denies_edit(self):
        cursor = {'x': 20, 'y': 20}
        self.put(self.state(cursor=cursor, gesture=self.ended()), session=self.editor)
        original = self.stored()
        self.upload(self.pid, base=1)
        before = self.req(self.owner, 'GET', f'/api/projects/{self.pid}').json()
        self.member(self.pid, 1, 'view')
        # A viewer's authorized cursor keeps this peer present even while the
        # receiver has not installed revision2. The server must hide old bytes.
        for peers in self.snapshots():
            self.assertEqual(len(peers), 1)
            peer = peers[0]
            self.assertEqual((peer['actor'], peer['tab']), (self.accounts[1][0], self.tab))
            self.assertEqual(peer['cursor'], cursor)
            self.assertEqual(peer['baseRevision'], 1)
            self.assertIsNone(peer['gesture'])
            self.assertGreater(peer['ttlMs'], 0)
            self.assertLessEqual(peer['ttlMs'], 2000)
        self.assertEqual(self.stored(), original, 'Reading downgraded metadata renewed the lease')
        self.put(self.state(2, baseRevision=2, gesture=self.ended()), session=self.editor, status=403)
        self.assertEqual(self.stored(), original, 'Denied editing changed stored sequence or lease')
        self.assertTrue(self.put(self.state(2, baseRevision=2, cursor=cursor), session=self.editor)['accepted'])
        self.assertIsNone(self.stored()[4], 'A fresh viewer cursor kept the editing payload')
        for peers in self.snapshots():
            self.assertEqual(peers[0]['cursor'], cursor)
            self.assertIsNone(peers[0]['gesture'])
        after = self.req(self.owner, 'GET', f'/api/projects/{self.pid}').json()
        self.assertEqual((before['revision'], before['content']['sha256']),
                         (after['revision'], after['content']['sha256']))

    def test_handoff_session_expiry_and_logout_remove_metadata(self):
        session, digest = self.actor(account=1)
        other, _ = self.actor(account=1)
        other_tab = str(uuid.uuid4())
        self.put(self.state(tab=other_tab, cursor=None, gesture=self.ended()), session=other)
        self.handoff(session)
        self.assert_metadata(self.snapshots())
        self.assert_metadata(self.snapshots(), tab=other_tab)
        self.db.execute("UPDATE photocraft.sessions SET expires_at=clock_timestamp()-interval '1 second' WHERE hash=%s", (digest,))
        self.assert_metadata(self.snapshots(), tab=other_tab)
        self.assertEqual(len(self.snapshots()[0]), 1)
        self.put(self.state(2, baseRevision=2, cursor=None), session=session, status=401)
        self.req(other, 'POST', '/api/logout', json={})
        self.assertEqual(self.snapshots(), ([], []))

    def test_handoff_same_base_cancel_unavailable_and_rebinding_clear(self):
        # A same-base clear is still immediate, even following End.
        self.put(self.state(cursor=None, gesture=self.ended()), session=self.editor)
        self.put(self.state(2, cursor=None), session=self.editor)
        self.assertEqual(self.peers(), [])
        tabs = {kind: str(uuid.uuid4()) for kind in ['end', 'cancel', 'unavailable']}
        for kind, tab in tabs.items():
            self.put(self.state(tab=tab, cursor=None, gesture=self.ended(kind)), session=self.editor)
        self.upload(self.pid, base=1)
        self.assert_metadata(self.snapshots(), tab=tabs['end'])
        self.assertEqual(len(self.snapshots()[0]), 1)
        for kind, tab in tabs.items():
            self.put(self.state(2, tab=tab, baseRevision=2, cursor=None), session=self.editor)
            self.assertEqual(self.stored(tab=tab)[4] is not None, kind == 'end')
        other = self.project()
        self.upload(other)
        self.member(other, 1, 'edit')
        self.req(self.editor, 'PUT', f'/api/projects/{other}/live',
                 json=self.state(3, tab=tabs['end'], cursor=None))
        self.assertEqual(self.snapshots(), ([], []), 'Project rebinding retained an old room identity')
        self.assertIsNone(self.stored(tab=tabs['end'])[4])

    def test_handoff_counts_existing_session_and_room_slots(self):
        tabs = [str(uuid.uuid4()) for _ in range(9)]
        for tab in tabs[:8]:
            self.put(self.state(tab=tab, cursor=None, gesture=self.ended()), session=self.editor)
        self.upload(self.pid, base=1)
        for tab in tabs[:8]:
            self.put(self.state(2, tab=tab, baseRevision=2, cursor=None), session=self.editor)
        self.put(self.state(tab=tabs[8], baseRevision=2), session=self.editor, status=429)
        self.assertEqual(self.active_count(), 8)
        # Add the other56 active room slots with owned sessions. Empty tombstones
        # never enter the response or create room capacity beyond the existing64.
        self.seeded(56)
        self.assertEqual(self.active_count(), 64)
        candidate, _ = self.actor()
        self.put(self.state(baseRevision=2), session=candidate, status=429)
        for _ in range(12):
            self.assertTrue(self.put(self.state(tab=str(uuid.uuid4()), baseRevision=2, cursor=None))['accepted'])
        read, exchange = self.snapshots()
        self.assertEqual((len(read), len(exchange)), (64, 64))
        digest = hashlib.sha256(self.editor.cookies.get('pc_session').encode()).hexdigest()
        self.db.execute("UPDATE photocraft.live_previews SET seen_at=clock_timestamp()-interval '3 seconds' WHERE session_hash=%s AND tab_id=%s", (digest, tabs[0]))
        self.assertTrue(self.put(self.state(baseRevision=2), session=candidate)['accepted'])
        self.assertEqual(self.active_count(), 64)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(LiveHandoff(name) for name in sorted(LiveHandoff.__dict__) if name.startswith('test_handoff_'))


if __name__ == '__main__':
    unittest.main()
