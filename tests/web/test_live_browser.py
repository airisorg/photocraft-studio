"""Focused local rendered regressions for native live-preview races and leases.

Run after building the matching WASM/backend. Uses only synthetic loopback accounts;
load_tests deliberately excludes BrowserAcceptance's inherited general suite.
"""
import hashlib
import io
import json
import secrets
import threading
import time
import unittest
import uuid

from PIL import Image
import requests

from benchmark_paint_collaboration import canvas_rectangle
from paint_latency import PaintObserver, PixelOracle
from test_browser import ARTIFACTS, BASE, BrowserAcceptance
from visual_assertions import flat_native_canvas_bounds, native_cursor_label_measurements


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

    def wait_live(self, page, predicate, message, timeout=2500):
        deadline = time.monotonic()+timeout/1000
        while not predicate() and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        self.assertTrue(predicate(), message)

    def live_trace(self, page, path):
        """Observe browser requests, including held bodies, without changing fetch."""
        trace = {'requests': [], 'peak_inflight': 0}
        pending = {}
        def started(request):
            if request.url.split('?')[0] != path:
                return
            item = {'method': request.method, 'started': time.monotonic()}
            trace['requests'].append(item)
            pending[request] = item
            trace['peak_inflight'] = max(trace['peak_inflight'], len(pending))
        def finished(request):
            item = pending.pop(request, None)
            if item is not None:
                item['finished'] = time.monotonic()
        page.on('request', started)
        page.on('requestfinished', finished)
        page.on('requestfailed', finished)
        return trace

    def test_live_exchange_put_carries_paint_and_coalesces_single_flight(self):
        pid, peer, owner_rect, peer_rect, _ = self.pair()
        path = BASE+f'/api/projects/{pid}/live'
        phase = {'value': 'hold-first-put'}
        held, parked, snapshots = [], [], []
        trace = self.live_trace(peer, path)
        self.observations['receiver_transport'] = trace
        before = self.inspect(peer)['document']
        untouched = self.execute('document.pixel', {'x': 72, 'y': 72}, peer)

        def intercept(route):
            if route.request.method == 'GET':
                if phase['value'] == 'active':
                    parked.append(route)  # No GET peer data can explain the paint.
                    return
                response = route.fetch()
                body = response.json()
                body['peers'] = []
                route.fulfill(response=response, json=body)
            elif phase['value'] == 'hold-first-put' and not held:
                held.append(route)
            else:
                if phase['value'] == 'arming':
                    # Start cadence assertions at the first publishing cycle,
                    # excluding the unavoidable idle-to-active GET boundary.
                    phase['value'] = 'active'
                    self.observations['active_started'] = time.monotonic()
                response = route.fetch()
                body = response.json()
                snapshots.append({'status': response.status, 'role': body.get('role'),
                                  'peers': body.get('peers'), 'at': time.monotonic()})
                route.fulfill(response=response)

        peer.route(path+'*', intercept)
        try:
            peer.mouse.move(peer_rect[0]+150, peer_rect[1]+20)
            self.wait_live(peer, lambda: bool(held), 'No cursor PUT was intercepted')
            started_count = len(trace['requests'])
            for x in range(151, 166):
                peer.mouse.move(peer_rect[0]+x, peer_rect[1]+20)
                peer.wait_for_timeout(16)
            self.assertEqual(len(trace['requests']), started_count,
                             'GET or a second PUT overlapped the held exchange')
            phase['value'] = 'warming'
            # Fetch and release the actual server response, preserving its wire shape.
            route = held[0]
            response = route.fetch()
            self.assertEqual(response.status, 200)
            route.fulfill(response=response)
            held.clear()
            peer.wait_for_timeout(100)
            with PaintObserver(peer) as observer:
                def trigger():
                    phase['value'] = 'arming'
                    self.page.mouse.move(owner_rect[0]+24, owner_rect[1]+72)
                    self.page.mouse.down()
                    self.page.mouse.move(owner_rect[0]+128, owner_rect[1]+72)
                    # Keep the receiver publishing during the short measured
                    # prefix; a long synchronous burst would overflow CDP's queue
                    # before measure() can consume frames.
                    for index in range(12):
                        peer.mouse.move(peer_rect[0]+150+index, peer_rect[1]+20)
                        peer.wait_for_timeout(16)

                sample = observer.measure(self.page, trigger, self.patch(peer_rect, 72, 72, (25, 197, 99)),
                                          event_type='pointermove', target_ms=500, timeout_ms=1500)
                self.observations['put_only_paint'] = sample
                if observer.last_matching_png:
                    (ARTIFACTS/(self._testMethodName+'-put-paint.png')).write_bytes(observer.last_matching_png)
                self.assertEqual(sample['status'], 'passed', sample)
            # Check sustained coalescing separately, with the screencast stopped.
            # Any idle read at the end of measurement receives no peer data.
            phase['value'] = 'warming'
            for route in parked:
                response = route.fetch()
                body = response.json()
                body['peers'] = []
                route.fulfill(response=response, json=body)
            parked.clear()
            peer.wait_for_timeout(100)
            phase['value'] = 'arming'
            self.observations.pop('active_started', None)
            for index in range(48):
                peer.mouse.move(peer_rect[0]+150+index%50, peer_rect[1]+20)
                peer.wait_for_timeout(16)
            self.observations['active_ended'] = time.monotonic()
            self.assertIn('active_started', self.observations, 'No publishing cycle started')
            begin, end = self.observations['active_started'], self.observations['active_ended']
            active = [r for r in trace['requests'] if begin <= r['started'] <= end]
            self.assertEqual(parked, [], 'An active receiver started an unnecessary GET')
            self.assertTrue(active and all(r['method'] == 'PUT' for r in active), active)
            self.assertGreaterEqual(len(active), 4, 'No sustained active exchange was observed')
            self.assertLessEqual(len(active), int((end-begin)/.08)+2, 'Exchange rate exceeded the coalesced cadence')
            intervals = [(b['started']-a['started'])*1000 for a, b in zip(active, active[1:])]
            self.assertTrue(all(value >= 65 for value in intervals), intervals)
            self.assertLess(len(active)*2, 48, 'Input samples were not meaningfully coalesced')
            self.assertEqual(trace['peak_inflight'], 1, trace)
            self.assertTrue(any(s['status'] == 200 and s['role'] == 'edit'
                                and any(p.get('gesture') for p in s['peers'] or []) for s in snapshots), snapshots)
            self.assertEqual(self.inspect(peer)['document']['history'], before['history'])
            self.assertEqual(self.inspect(peer)['document']['revision'], before['revision'])
            self.assertEqual(self.execute('document.pixel', {'x': 72, 'y': 72}, peer), untouched)
            self.assertEqual(self.auth_revision(pid, 1)['revision'], 1)
            self.observations['cadence'] = {'input_moves': 48, 'active_puts': len(active), 'interval_ms': intervals}
        finally:
            phase['value'] = 'cleanup'
            self.page.mouse.up()
            for route in held+parked:
                route.abort()
            peer.unroute(path+'*', intercept)

    def test_live_exchange_delayed_put_does_not_extend_peer_lease_or_cross_generation(self):
        pid, peer, owner_rect, peer_rect, _ = self.pair()
        path = BASE+f'/api/projects/{pid}/live'
        captured, parked, published = [], [], []
        phase = {'value': 'warming'}
        owner_frozen = {'value': False}
        started, finished, failed = {}, {}, {}
        before = self.inspect(peer)['document']
        peer.on('request', lambda request: started.setdefault(request, time.monotonic()))
        peer.on('requestfinished', lambda request: finished.setdefault(request, time.monotonic()))
        peer.on('requestfailed', lambda request: failed.setdefault(request, request.failure))

        def owner_intercept(route):
            if route.request.method == 'PUT' and owner_frozen['value']:
                route.abort()  # No heartbeat may renew the lease being aged.
                return
            response = route.fetch()
            route.fulfill(response=response)
            if route.request.method == 'PUT' and response.status == 200:
                cursor = route.request.post_data_json.get('cursor')
                if cursor and cursor['x'] == 80 and cursor['y'] == 20:
                    published.append(time.monotonic())
                    owner_frozen['value'] = True

        def intercept(route):
            if phase['value'] == 'park':
                parked.append(route)
                return
            response = route.fetch()
            body = response.json()
            if (phase['value'] == 'capture' and route.request.method == 'PUT'
                    and any(p.get('cursor') for p in body.get('peers', []))):
                captured.append((route, response, body, time.monotonic()))
                phase['value'] = 'park'
                return
            body['peers'] = []  # Earlier GETs or PUTs cannot explain cursor pixels.
            route.fulfill(response=response, json=body)

        self.page.route(path+'*', owner_intercept)
        peer.route(path+'*', intercept)
        try:
            self.page.mouse.move(owner_rect[0]+80, owner_rect[1]+20)
            self.wait_live(peer, lambda: bool(published), 'Owner cursor was not acknowledged')
            # Capture an aged, real server lease. The ensuing hold crosses its TTL
            # while remaining comfortably inside the client's 2000ms deadline.
            peer.wait_for_timeout(1100)
            phase['value'] = 'capture'
            peer.mouse.move(peer_rect[0]+160, peer_rect[1]+20)
            self.wait_live(peer, lambda: bool(captured), 'PUT did not contain a real peer cursor')
            route, response, body, received = captured[0]
            self.assertEqual(response.status, 200)
            ttl = max(p['ttlMs'] for p in body['peers'] if p.get('cursor'))
            self.assertGreaterEqual(ttl, 300, body)
            self.assertLessEqual(ttl, 1000, body)
            peer.wait_for_timeout(ttl+150)
            request = route.request
            route.fulfill(response=response)
            captured.clear()
            self.wait_live(peer, lambda: request in finished or request in failed,
                           'Delayed PUT never completed', timeout=500)
            self.assertNotIn(request, failed, failed.get(request))
            self.assertIn(request, finished)
            elapsed = (finished[request]-started[request])*1000
            self.assertLess(elapsed, 1800, 'Delayed body approached the 2000ms abort deadline')
            self.observations['delayed_put'] = {
                'ttl_ms': ttl, 'response_held_ms': (finished[request]-received)*1000,
                'request_elapsed_ms': elapsed, 'request_finished': True, 'status': response.status}
            # No replacement response is admitted: a ghost would persist until its
            # incorrectly restarted lease. Inspect multiple actual rendered frames.
            white = self.patch(peer_rect, 80, 20, (255, 255, 255))
            for _ in range(5):
                peer.wait_for_timeout(60)
                self.assertTrue(white.matches(Image.open(io.BytesIO(peer.screenshot()))),
                                'Delayed PUT restarted an expired cursor lease')
            peer.screenshot(path=str(ARTIFACTS/(self._testMethodName+'-expired.png')))
            self.assertEqual(self.inspect(peer)['document']['history'], before['history'])
            self.assertEqual(self.inspect(peer)['document']['revision'], before['revision'])

            # Separately renew the owner, then hold a fresh snapshot across an
            # active-document change. Its still-valid lease must not cross scope.
            owner_frozen['value'] = False
            previous_publications = len(published)
            self.page.mouse.move(0, 0)
            self.page.mouse.move(owner_rect[0]+80, owner_rect[1]+20)
            self.wait_live(peer, lambda: len(published) > previous_publications,
                           'Owner did not publish the second cursor lease')
            phase['value'] = 'capture'
            for pending in parked:
                pending.abort()
            parked.clear()
            peer.mouse.move(peer_rect[0]+190, peer_rect[1]+20)
            self.wait_live(peer, lambda: bool(captured), 'No second peer snapshot was captured')
            route, response, body, _ = captured[0]
            self.new(320, 240, page=peer)
            self.configure_pencil(peer)
            fresh = self.inspect(peer)['document']
            fresh_rect = canvas_rectangle(Image.open(io.BytesIO(peer.screenshot())))
            request = route.request
            route.fulfill(response=response)
            captured.clear()
            self.wait_live(peer, lambda: request in finished or request in failed,
                           'Old-generation PUT never completed', timeout=500)
            self.assertNotIn(request, failed, failed.get(request))
            self.assertIn(request, finished)
            peer.wait_for_timeout(150)
            self.assertTrue(self.patch(fresh_rect, 80, 20, (255, 255, 255)).matches(Image.open(io.BytesIO(peer.screenshot()))))
            self.assertEqual(self.inspect(peer)['document'], fresh)
            self.assertEqual(self.auth_revision(pid, 1)['revision'], 1)
            self.observations['old_generation_ignored'] = True
        finally:
            for route, *_ in captured:
                route.abort()
            for route in parked:
                route.abort()
            peer.unroute(path+'*', intercept)
            self.page.unroute(path+'*', owner_intercept)

    def test_live_exchange_old_ack_requires_get_before_next_put(self):
        pid, peer, owner_rect, peer_rect, _ = self.pair()
        path = BASE+f'/api/projects/{pid}/live'
        legacy, fallback, parked = [], [], []
        trace = self.live_trace(peer, path)
        self.observations['receiver_transport'] = trace

        def intercept(route):
            if legacy:
                (fallback if route.request.method == 'GET' else parked).append(route)
                return
            response = route.fetch()
            body = response.json()
            if route.request.method == 'PUT':
                body.pop('peers', None)
                body.pop('role', None)
                legacy.append({'body': body, 'at': time.monotonic()})
            else:
                body['peers'] = []
            route.fulfill(response=response, json=body)

        peer.route(path+'*', intercept)
        try:
            peer.mouse.move(peer_rect[0]+160, peer_rect[1]+20)
            self.wait_live(peer, lambda: bool(fallback), 'Old PUT acknowledgement did not force an authoritative GET')
            for x in range(161, 177):
                peer.mouse.move(peer_rect[0]+x, peer_rect[1]+20)
                peer.wait_for_timeout(16)
            self.assertEqual(len(fallback), 1)
            self.assertEqual(parked, [], 'PUT overlapped or bypassed the required fallback GET')
            with PaintObserver(peer) as observer:
                def trigger():
                    self.page.mouse.move(owner_rect[0]+110, owner_rect[1]+20)
                    self.page.wait_for_timeout(120)
                    route = fallback.pop()
                    response = route.fetch()
                    self.assertEqual(response.status, 200)
                    self.observations['fallback_snapshot'] = response.json()
                    route.fulfill(response=response)

                sample = observer.measure(self.page, trigger, self.patch(peer_rect, 110, 20, (154, 107, 255)),
                                          event_type='pointermove', target_ms=500, timeout_ms=1500)
                self.observations['legacy_fallback_paint'] = sample
                self.assertEqual(sample['status'], 'passed', sample)
                if observer.last_matching_png:
                    (ARTIFACTS/(self._testMethodName+'-fallback.png')).write_bytes(observer.last_matching_png)
            self.assertEqual(trace['peak_inflight'], 1, trace)
            self.assertEqual(legacy[0]['body'].get('accepted'), True)
            self.assertTrue(self.observations['fallback_snapshot']['peers'])
            self.assertEqual(self.auth_revision(pid, 1)['revision'], 1)
            self.observations['legacy_ack'] = legacy[0]['body']
        finally:
            for route in fallback+parked:
                route.abort()
            peer.unroute(path+'*', intercept)

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

    def test_live_clustered_cursor_labels_are_readable_and_keep_native_anchors(self):
        pid,peer,_,_,_=self.pair()
        self.command('ui.set',{'theme':'proMedium'},peer)
        peer.wait_for_timeout(300)
        clean=Image.open(io.BytesIO(peer.screenshot())).convert('RGB')
        document_rect=canvas_rectangle(clean)
        viewport=flat_native_canvas_bounds(clean,document_rect)
        before=self.inspect(peer)['document']
        before_meta=self.context.request.get(BASE+f'/api/projects/{pid}').json()
        actors=[]
        for index in range(8):
            ident,token=str(uuid.uuid4()),secrets.token_hex(32)
            self.accounts.append(ident)
            email=ident+'@example.invalid'
            name=f'Peer {index+1:02}' if index<7 else 'M'*32
            self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',(ident,email,name))
            self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',(hashlib.sha256(token.encode()).hexdigest(),ident))
            grant=self.context.request.put(BASE+f'/api/projects/{pid}/members',headers={'Origin':BASE},data={'email':email,'role':'edit'})
            self.assertTrue(grant.ok,grant.text())
            actors.append({'id':ident,'token':token,'tab':str(uuid.uuid4())})
        stop=threading.Event();lock=threading.Lock()
        state={'generation':0,'positions':[(160,120)]*8,'acknowledged':-1,'errors':[]}
        def publish():
            sessions=[]
            try:
                for actor in actors:
                    session=requests.Session();session.trust_env=False
                    session.headers.update({'Origin':BASE,'X-Photocraft-Account':actor['id']})
                    session.cookies.set('pc_session',actor['token']);sessions.append(session)
                sequence=0
                while not stop.is_set():
                    with lock:
                        generation,positions=state['generation'],list(state['positions'])
                    sequence+=1
                    for actor,session,(x,y) in zip(actors,sessions,positions):
                        if stop.is_set(): return
                        response=session.put(BASE+f'/api/projects/{pid}/live',json={'tab':actor['tab'],'seq':sequence,
                            'baseRevision':1,'cursor':{'x':x,'y':y},'gesture':None},timeout=1)
                        if response.status_code!=200 or response.json().get('accepted') is not True or response.json().get('seq')!=sequence:
                            raise AssertionError(f'Fixture cursor publication failed: HTTP{response.status_code}')
                    with lock: state['acknowledged']=generation
                    stop.wait(.4)
            except Exception as error:
                with lock: state['errors'].append(type(error).__name__)
            finally:
                for session in sessions: session.close()
        worker=threading.Thread(target=publish,name='owned-cursor-label-fixture',daemon=True)
        worker.start()
        phases=[('coincident',[(160,120)]*8),('clustered',[(160,90+7*i) for i in range(8)]),
                ('viewport-edge',[(viewport[2]-document_rect[0]-9,viewport[3]-document_rect[1]-9)]*8)]
        self.observations['cursor_labels']={'theme':'proMedium','viewport':viewport,'phases':[],
            'scope':'Actual native plates and glyph pixels; bounded long name, no semantic OCR assertion.'}
        try:
            for generation,(label,positions) in enumerate(phases,1):
                with lock: state.update(generation=generation,positions=positions)
                self.wait_live(peer,lambda:state['acknowledged']>=generation or bool(state['errors']),
                               'Authenticated cursor fixture did not publish',timeout=5000)
                self.assertEqual(state['errors'],[])
                anchors=[(document_rect[0]+x,document_rect[1]+y) for x,y in positions]
                deadline=time.monotonic()+5;last=None
                while time.monotonic()<deadline:
                    picture=peer.screenshot()
                    try:
                        measured=native_cursor_label_measurements(Image.open(io.BytesIO(picture)),viewport,anchors)
                        break
                    except AssertionError as error:
                        last=str(error);peer.wait_for_timeout(50)
                else:
                    (ARTIFACTS/(self._testMethodName+'-'+label+'-failed.png')).write_bytes(picture)
                    self.fail(last)
                (ARTIFACTS/(self._testMethodName+'-'+label+'.png')).write_bytes(picture)
                self.assertTrue(any(row['rect'][2]-row['rect'][0]>=150 for row in measured),'Long native label was not visibly bounded/truncated')
                self.observations['cursor_labels']['phases'].append({'name':label,'labels':measured,'anchor_count':len(set(anchors))})
                self.assertEqual(self.inspect(peer)['document']['history'],before['history'])
                self.assertEqual(self.inspect(peer)['document']['revision'],before['revision'])
                self.assertEqual(state['errors'],[])
            after=self.context.request.get(BASE+f'/api/projects/{pid}').json()
            self.assertEqual((after['revision'],after['content']['sha256']),(1,before_meta['content']['sha256']))
        finally:
            stop.set();worker.join(timeout=3)
            self.observations['cursor_labels']['fixture_thread_stopped']=not worker.is_alive()
            self.assertFalse(worker.is_alive(),'Owned cursor fixture did not stop')


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(LiveBrowser(name) for name in sorted(LiveBrowser.__dict__) if name.startswith('test_live_'))


if __name__ == '__main__':
    unittest.main()
