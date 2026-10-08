"""Focused local rendered regressions for native live-preview races and leases.

Run after building the matching WASM/backend. Uses only synthetic loopback accounts;
load_tests deliberately excludes BrowserAcceptance's inherited general suite.
"""
import hashlib
import io
import json
import secrets
import time
import unittest
import uuid

from PIL import Image

from benchmark_paint_collaboration import canvas_rectangle
from paint_latency import PaintObserver, PixelOracle
from test_browser import ARTIFACTS, BASE, BrowserAcceptance


class LiveBrowser(BrowserAcceptance):
    def setUp(self):
        self.observations = {}
        super().setUp()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            (ARTIFACTS/(self._testMethodName+'.json')).write_text(json.dumps(self.observations, indent=2)+'\n')

    def pair(self):
        self.signed_in()
        self.new(320, 240)
        self.configure_pencil(self.page)
        self.auth_save(bound=False)
        pid = self.wait_revision(1)['id']
        peer_id, token = str(uuid.uuid4()), secrets.token_hex(32)
        self.accounts.append(peer_id)
        email = peer_id+'@example.invalid'
        self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                        (peer_id, email, 'Live regression peer'))
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                        (hashlib.sha256(token.encode()).hexdigest(), peer_id))
        grant = self.context.request.put(BASE+f'/api/projects/{pid}/members',
            headers={'Origin': BASE}, data={'email': email, 'role': 'edit'})
        self.assertTrue(grant.ok, grant.text())
        _, peer = self.context_page('?project='+pid, token=token)
        self.configure_pencil(peer)
        owner_rect = canvas_rectangle(Image.open(io.BytesIO(self.page.screenshot())))
        peer_rect = canvas_rectangle(Image.open(io.BytesIO(peer.screenshot())))
        return pid, peer, owner_rect, peer_rect, email

    def configure_pencil(self, page):
        self.command('ui.set', {'tool': 'pencil', 'zoom': 1, 'center': [160, 120]}, page)
        self.execute('tools.setBrush', {'reset': True, 'size': 12, 'opacity': 1, 'flow': 1, 'hardness': 1}, page)
        self.execute('tools.setColors', {'foreground': '#19c563', 'background': '#ffffff'}, page)
        page.mouse.move(0, 0)

    @staticmethod
    def patch(rect, x, y, color):
        return PixelOracle.solid((rect[0]+x-1, rect[1]+y-1, rect[0]+x+1, rect[1]+y+1), color, tolerance=3)

    def pixels(self, page, oracle, label, timeout=3500):
        deadline = time.monotonic()+timeout/1000
        while time.monotonic() < deadline:
            data = page.screenshot()
            if oracle.matches(Image.open(io.BytesIO(data))):
                (ARTIFACTS/(self._testMethodName+'-'+label+'.png')).write_bytes(data)
                return
            page.wait_for_timeout(40)
        self.fail(f'Rendered pixels never matched {label}')

    def wait_native(self, page, point, expected):
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            if self.execute('document.pixel', point, page) == expected:
                return
            page.wait_for_timeout(40)
        self.fail(f'Native pixel did not converge at {point}')

    def test_live_delayed_old_409_preserves_new_held_gesture(self):
        pid, peer, owner_rect, peer_rect, _ = self.pair()
        path = BASE+f'/api/projects/{pid}/live'
        held, writes = [], []

        def intercept(route):
            if route.request.method != 'PUT':
                route.continue_()
                return
            payload = route.request.post_data_json
            events = (payload.get('gesture') or {}).get('events', [])
            if not held and payload['baseRevision'] == 1 and any(e.get('kind') == 'end' for e in events):
                held.append((route, time.monotonic(), payload))
                return
            route.continue_()

        def response_seen(response):
            if response.url == path and response.request.method == 'PUT':
                body = response.request.post_data_json
                writes.append({'status': response.status, 'base': body['baseRevision'],
                               'seq': body['seq'], 'has_gesture': bool(body.get('gesture'))})

        self.page.route(path, intercept)
        self.page.on('response', response_seen)
        self.observations['writes'] = writes
        first, second = {'x': 72, 'y': 48}, {'x': 72, 'y': 72}
        white = self.execute('document.pixel', second, peer)
        try:
            with PaintObserver(peer) as observer:
                self.page.mouse.move(owner_rect[0]+24, owner_rect[1]+48)
                self.page.mouse.down()
                self.page.wait_for_timeout(30)
                self.page.mouse.move(owner_rect[0]+112, owner_rect[1]+48)
                self.page.wait_for_timeout(50)
                self.page.mouse.up()
                deadline = time.monotonic()+1
                while not held and time.monotonic() < deadline:
                    self.page.wait_for_timeout(10)
                self.assertEqual(len(held), 1, 'No real first-gesture End request was intercepted')
                self.assertEqual(self.auth_revision(pid, 2)['revision'], 2)
                self.wait_native(peer, first, self.execute('document.pixel', first))
                # Give the acknowledged Saved message one repaint to advance its binding.
                self.page.wait_for_timeout(50)
                history = self.inspect()['document']['history']
                revision = self.inspect()['document']['revision']
                self.page.mouse.move(owner_rect[0]+24, owner_rect[1]+72)
                self.page.mouse.down()
                self.page.wait_for_timeout(30)

                def trigger():
                    self.page.mouse.move(owner_rect[0]+80, owner_rect[1]+72)
                    self.page.wait_for_timeout(30)
                    route, started, old = held[0]
                    stale = route.fetch(timeout=1000)
                    self.observations['stale_response'] = {
                        'status': stale.status, 'body': stale.json(), 'submitted_base': old['baseRevision'],
                        'held_ms': (time.monotonic()-started)*1000}
                    self.assertEqual(stale.status, 409)
                    self.assertLess(self.observations['stale_response']['held_ms'], 1800)
                    route.fulfill(response=stale)
                    for x in [96, 112, 128]:
                        self.page.wait_for_timeout(1000/60)
                        self.page.mouse.move(owner_rect[0]+x, owner_rect[1]+72)

                sample = observer.measure(self.page, trigger, self.patch(peer_rect, 72, 72, (25, 197, 99)),
                                          event_type='pointermove', target_ms=500, timeout_ms=1500)
                self.observations['held_second_gesture_paint'] = sample
                if observer.last_baseline_png:
                    (ARTIFACTS/(self._testMethodName+'-held-baseline.png')).write_bytes(observer.last_baseline_png)
                if observer.last_matching_png:
                    (ARTIFACTS/(self._testMethodName+'-held-match.png')).write_bytes(observer.last_matching_png)
                self.assertEqual(sample['status'], 'passed', sample)
                self.assertEqual(self.inspect()['document']['revision'], revision)
                self.assertEqual(self.inspect()['document']['history'], history)
                self.assertEqual(self.execute('document.pixel', second, peer), white)
                self.assertEqual(self.auth_revision(pid, 2)['revision'], 2)
                self.assertTrue(any(w['status'] == 200 and w['base'] == 2 and w['has_gesture'] for w in writes), writes)
                self.page.mouse.up()
                self.page.mouse.move(0, 0)
            self.assertEqual(self.auth_revision(pid, 3)['revision'], 3)
            green = self.execute('document.pixel', second)
            self.assertNotEqual(green, white)
            self.wait_native(peer, first, self.execute('document.pixel', first))
            self.wait_native(peer, second, green)
            self.auth_cloud_document(pid)  # Verifies native archive SHA as well as manifest.
            self.load(peer, '?project='+pid)
            self.assertEqual(self.execute('document.pixel', second, peer), green)
            self.execute('edit.undo')
            self.assertEqual(self.execute('document.pixel', second), white)
            self.assertNotEqual(self.execute('document.pixel', first), white)
            self.execute('edit.redo')
            self.assertEqual(self.execute('document.pixel', second), green)
            self.observations['durable_reload_and_native_undo'] = 'passed'
        finally:
            self.page.mouse.up()
            self.page.unroute(path, intercept)

    def test_live_role_downgrade_clears_preview_keeps_cursor_and_local_pixels(self):
        pid, peer, owner_rect, peer_rect, email = self.pair()
        point = {'x': 72, 'y': 72}
        before = self.execute('document.pixel', point)
        history = self.inspect(peer)['document']['history']
        peer.mouse.move(peer_rect[0]+24, peer_rect[1]+72)
        peer.mouse.down()
        peer.wait_for_timeout(30)
        try:
            peer.mouse.move(peer_rect[0]+128, peer_rect[1]+72)
            self.pixels(self.page, self.patch(owner_rect, 72, 72, (25, 197, 99)), 'edit-preview')
            result = self.context.request.put(BASE+f'/api/projects/{pid}/members',
                headers={'Origin': BASE}, data={'email': email, 'role': 'view'})
            self.assertTrue(result.ok, result.text())
            self.pixels(self.page, self.patch(owner_rect, 72, 72, (255, 255, 255)), 'downgrade-cleared-preview')
            self.assertEqual(self.inspect(peer)['document']['history'], history)
            self.assertEqual(self.execute('document.pixel', point), before)
        finally:
            peer.mouse.up()
        peer.mouse.move(peer_rect[0]+180, peer_rect[1]+20)
        self.pixels(self.page, self.patch(owner_rect, 180, 20, (154, 107, 255)), 'viewer-cursor')
        self.assertNotEqual(self.execute('document.pixel', point, peer), before)
        self.page.wait_for_timeout(400)
        self.assertEqual(self.auth_revision(pid, 1)['revision'], 1)
        self.assertEqual(self.execute('document.pixel', point), before)
        self.observations['downgrade'] = {'preview_cleared': True, 'viewer_cursor_visible': True,
            'local_native_stroke_preserved': True, 'canonical_revision': 1}

    def test_live_cursor_lease_expires_and_reconnects_without_native_mutation(self):
        pid, peer, owner_rect, peer_rect, _ = self.pair()
        before = self.inspect(peer)['document']
        point = {'x': 80, 'y': 20}
        pixel = self.execute('document.pixel', point, peer)
        self.page.mouse.move(owner_rect[0]+80, owner_rect[1]+20)
        self.pixels(peer, self.patch(peer_rect, 80, 20, (154, 107, 255)), 'cursor-before-loss')
        path = BASE+f'/api/projects/{pid}/live'
        failures = []
        def disconnected(route):
            if route.request.method == 'PUT':
                failures.append(time.monotonic())
                route.abort('connectionfailed')
            else:
                route.continue_()
        self.page.route(path, disconnected)
        started = time.monotonic()
        try:
            self.page.mouse.move(0, 0)
            self.pixels(peer, self.patch(peer_rect, 80, 20, (255, 255, 255)), 'expired-cursor', timeout=3500)
            self.assertTrue(failures)
            self.observations['lease'] = {'cleared_after_ms': (time.monotonic()-started)*1000,
                                          'failed_writes': len(failures)}
        finally:
            self.page.unroute(path, disconnected)
        self.page.mouse.move(owner_rect[0]+160, owner_rect[1]+20)
        self.pixels(peer, self.patch(peer_rect, 160, 20, (154, 107, 255)), 'cursor-after-reconnect')
        self.assertEqual(self.inspect(peer)['document']['revision'], before['revision'])
        self.assertEqual(self.inspect(peer)['document']['history'], before['history'])
        self.assertEqual(self.execute('document.pixel', point, peer), pixel)
        self.assertEqual(self.auth_revision(pid, 1)['revision'], 1)
        self.observations['reconnect_native_unchanged'] = True


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(LiveBrowser(name) for name in sorted(LiveBrowser.__dict__) if name.startswith('test_live_'))


if __name__ == '__main__':
    unittest.main()
