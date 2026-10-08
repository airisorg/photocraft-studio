"""Bounded loopback-only two-account cursor and held native Pencil paint probe.

No server is started. CDP HTTP delay is synthetic minimum request latency, not
physical packet RTT; observed /api/me round trips are reported separately. It
never sleeps inside synchronous request interception or changes the paint helper.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import secrets
import time
import unittest
from urllib.parse import urlparse
import uuid

from PIL import Image

from benchmark_collaboration import local_database
from benchmark_paint_collaboration import canvas_rectangle
from paint_latency import PaintObserver, PixelOracle, summarize_results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default=os.environ.get('PHOTOCRAFT_TEST_ORIGIN', 'http://127.0.0.1:8876'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--context', required=True, help='Actual host/build contention context')
    parser.add_argument('--samples', type=int, default=3, choices=range(1, 7))
    parser.add_argument('--latency-ms', type=int, nargs='+', default=[0, 50, 100], choices=[0, 50, 100])
    args = parser.parse_args()
    parsed = urlparse(args.origin)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise ValueError('This synthetic-account benchmark requires a literal loopback HTTP origin')
    if len(args.latency_ms) != len(set(args.latency_ms)):
        raise ValueError('Duplicate network profiles would obscure sample counts')
    local_database(os.environ.get('PHOTOCRAFT_TEST_DATABASE_URL', 'postgresql://photocraft_test@127.0.0.1:55438/postgres'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    artifacts = args.output.parent/(args.output.stem+'-screenshots')
    os.environ['PHOTOCRAFT_TEST_ORIGIN'] = args.origin.rstrip('/')
    os.environ['PHOTOCRAFT_TEST_ARTIFACTS'] = str(artifacts)
    from test_browser import BrowserAcceptance, BASE

    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'status': 'running',
              'scope': 'Two independent synthetic accounts; trusted cursor input and native held Pencil preview; local only.',
              'environment': {'system': platform.platform(), 'cpus': os.cpu_count(), 'context': args.context,
                  'load_average_start': os.getloadavg(), 'origin': BASE, 'clients': 2,
                  'viewport': [1440, 960], 'device_scale_factor': 1,
                  'network': 'CDP minimum HTTP request latency on both pages; no packet loss, physical RTT or hosted capacity claim'},
              'target_ms': 500, 'preferred_ms': 150,
              'statistics_note': 'Bounded small samples: p95/p99 are descriptive order statistics, not tail reliability or capacity estimates.',
              'profiles': [], 'samples': []}

    class LivePaint(BrowserAcceptance):
        def wait_pixel(self, page, point, expected):
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                actual = self.execute('document.pixel', point, page)
                if actual == expected:
                    return
                page.wait_for_timeout(50)
            self.fail(f'Native peer pixel did not converge at {point}: {actual} != {expected}')

        def pencil_sample(self, peer, owner_rect, peer_rect, profile, index, revisions,
                          durable_points, pid, scenario='held_pencil_prefix'):
            # Separate rows avoid old pixels. The sampled patch is behind
            # the new endpoint, outside the prior dab and purple cursor.
            y = 48+8*len(durable_points)
            point = {'x': 72, 'y': y}
            before_pixel = self.execute('document.pixel', point, peer)
            before_revision = self.inspect()['document']['revision']
            self.page.mouse.move(owner_rect[0]+24, owner_rect[1]+y)
            self.page.mouse.down()
            self.page.wait_for_timeout(30)
            key = '__live_moves_'+uuid.uuid4().hex
            self.page.evaluate('''key=>{
                const state={events:[]};
                state.listener=e=>{if(state.events.length<32)state.events.push({x:e.clientX,y:e.clientY,
                    buttons:e.buttons,trusted:e.isTrusted,epoch_ms:performance.timeOrigin+e.timeStamp})};
                window[key]=state;window.addEventListener('pointermove',state.listener,true);
            }''', key)

            def paced_prefix():
                self.page.mouse.move(owner_rect[0]+80, owner_rect[1]+y)
                for x in range(88, 137, 8):
                    self.page.wait_for_timeout(1000/60)
                    self.page.mouse.move(owner_rect[0]+x, owner_rect[1]+y)

            try:
                oracle = PixelOracle.solid((peer_rect[0]+70, peer_rect[1]+y-1,
                                           peer_rect[0]+74, peer_rect[1]+y+2), (25, 197, 99), tolerance=3)
                # Stop capture before canonical waits so idle frames cannot fill its bounded queue.
                with PaintObserver(peer) as observer:
                    sample = observer.measure(self.page, paced_prefix, oracle,
                        event_type='pointermove', target_ms=500, timeout_ms=3000)
                    moves = self.page.evaluate('key=>window[key].events', key)
                    sample.update(scenario=scenario, configured_http_latency_ms=profile['configured_http_latency_ms'],
                                  sample=index+1, document_point=[72, y], paced_moves=moves,
                                  stroke_still_held=True)
                    self.record_sample(sample, profile, observer)
                self.assertEqual(len(moves), 8)
                self.assertTrue(all(m['trusted'] and m['buttons'] == 1 for m in moves), moves)
                # Preview must be visual only. The native document/history
                # and saved archive remain unchanged until mouse-up.
                self.assertEqual(self.inspect()['document']['revision'], before_revision)
                self.assertEqual(self.execute('document.pixel', point, peer), before_pixel)
                self.assertEqual(self.context.request.get(BASE+f'/api/projects/{pid}').json()['revision'], revisions)
            finally:
                self.page.evaluate('''key=>{if(window[key])window.removeEventListener('pointermove',window[key].listener,true);delete window[key]}''', key)
                self.page.mouse.up()
                self.page.mouse.move(0, 0)
            revisions += 1
            self.auth_revision(pid, revisions)
            expected = self.execute('document.pixel', point)
            self.assertNotEqual(expected, before_pixel)
            self.wait_pixel(peer, point, expected)
            durable_points.append((point, expected))
            return revisions

        def test_cursor_and_held_native_pencil(self):
            self.signed_in()
            self.new(320, 240)
            self.command('ui.set', {'tool': 'pencil', 'zoom': 1, 'center': [160, 120]})
            self.execute('tools.setBrush', {'reset': True, 'size': 12, 'opacity': 1, 'flow': 1, 'hardness': 1})
            self.execute('tools.setColors', {'foreground': '#19c563', 'background': '#ffffff'})
            self.page.mouse.move(0, 0)
            self.auth_save(bound=False)
            project = self.wait_revision(1)
            pid = project['id']
            owner_id = self.accounts[0]
            peer_id, token = str(uuid.uuid4()), secrets.token_hex(32)
            email = peer_id+'@example.invalid'
            self.accounts.append(peer_id)
            self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                            (peer_id, email, 'Live paint peer'))
            self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                            (hashlib.sha256(token.encode()).hexdigest(), peer_id))
            grant = self.context.request.put(BASE+f'/api/projects/{pid}/members',
                headers={'Origin': BASE}, data={'email': email, 'role': 'edit'})
            self.assertTrue(grant.ok, grant.text())
            _, peer = self.context_page('?project='+pid, token=token)
            self.command('ui.set', {'zoom': 1, 'center': [160, 120]}, peer)
            peer.mouse.move(0, 0)
            owner_rect = canvas_rectangle(Image.open(io.BytesIO(self.page.screenshot())))
            peer_rect = canvas_rectangle(Image.open(io.BytesIO(peer.screenshot())))
            meta = self.context.request.get(BASE+f'/api/projects/{pid}').json()
            report['fixture'] = {'width': 320, 'height': 240, 'layers': 1, 'bytes': meta['content']['bytes'],
                                 'sha256': meta['content']['sha256'], 'owner_canvas': owner_rect, 'peer_canvas': peer_rect}
            report['environment']['browser'] = self.browser.version
            report['environment']['visibility'] = {
                'sender': self.page.evaluate('document.visibilityState'),
                'receiver': peer.evaluate('document.visibilityState')}
            report['live_http_responses'] = {}
            def live_response(response):
                if urlparse(response.url).path == f'/api/projects/{pid}/live':
                    key = response.request.method+' '+str(response.status)
                    counts = report['live_http_responses']
                    counts[key] = counts.get(key, 0)+1
            self.page.on('response', live_response)
            peer.on('response', live_response)
            network = [page.context.new_cdp_session(page) for page in [self.page, peer]]
            revisions = 1
            durable_points = []
            try:
                for delay in args.latency_ms:
                    for session in network:
                        session.send('Network.enable')
                        session.send('Network.emulateNetworkConditions', {'offline': False, 'latency': delay,
                                     'downloadThroughput': -1, 'uploadThroughput': -1})
                    profile = {'configured_http_latency_ms': delay, 'round_trips': {}, 'samples': []}
                    report['profiles'].append(profile)
                    for label, page, account in [('sender', self.page, owner_id), ('receiver', peer, peer_id)]:
                        probes = page.evaluate('''async (expected) => {
                            const out=[];
                            for(let i=0;i<5;i++) {
                                const start=performance.now();
                                const response=await fetch('/api/me',{cache:'no-store',headers:{'X-Photocraft-Account':expected}});
                                const value=await response.json();
                                out.push({ms:performance.now()-start,status:response.status,identity_matches:value.id===expected});
                            }
                            return out;
                        }''', account)
                        profile['round_trips'][label] = probes
                        self.assertTrue(all(p['status'] == 200 and p['identity_matches'] for p in probes), probes)
                    for index in range(args.samples):
                        x, y = 40+40*index, 20
                        self.page.mouse.move(0, 0)
                        oracle = PixelOracle.solid((peer_rect[0]+x-1, peer_rect[1]+y-1,
                                                   peer_rect[0]+x+1, peer_rect[1]+y+1), (154, 107, 255), tolerance=3)
                        with PaintObserver(peer) as observer:
                            sample = observer.measure(self.page,
                                lambda: self.page.mouse.move(owner_rect[0]+x, owner_rect[1]+y), oracle,
                                event_type='pointermove', target_ms=500, timeout_ms=3000)
                            sample.update(scenario='cursor', configured_http_latency_ms=delay, sample=index+1,
                                          document_point=[x, y])
                            self.record_sample(sample, profile, observer)

                    for index in range(args.samples):
                        revisions = self.pencil_sample(peer, owner_rect, peer_rect, profile,
                                                       index, revisions, durable_points, pid)
                    profile['summary'] = summarize_results(profile['samples'])
                # Preserve the sender's actual tab identity across a real reload.
                # The receiver stays alive with its first gesture already retired.
                for session in network:
                    session.send('Network.emulateNetworkConditions', {'offline': False, 'latency': 0,
                                 'downloadThroughput': -1, 'uploadThroughput': -1})
                tab_before = self.page.evaluate("JSON.parse(sessionStorage.getItem('photocraft.live.tab.v1'))")
                self.assertIsNotNone(tab_before)
                self.page.evaluate("pid=>history.replaceState(null,'','?project='+pid)", pid)
                self.page.reload(wait_until='domcontentloaded', timeout=90000)
                self.page.wait_for_function('typeof window.photocraftCommand === "function"', timeout=60000)
                deadline = time.monotonic()+15
                while time.monotonic() < deadline:
                    if self.inspect().get('document'):
                        break
                    self.page.wait_for_timeout(50)
                self.assertIsNotNone(self.inspect().get('document'))
                self.command('ui.set', {'tool': 'pencil', 'zoom': 1, 'center': [160, 120]})
                self.execute('tools.setBrush', {'reset': True, 'size': 12, 'opacity': 1, 'flow': 1, 'hardness': 1})
                self.execute('tools.setColors', {'foreground': '#19c563', 'background': '#ffffff'})
                self.page.mouse.move(0, 0)
                self.page.wait_for_timeout(100)
                tab_after = self.page.evaluate("JSON.parse(sessionStorage.getItem('photocraft.live.tab.v1'))")
                self.assertEqual(tab_before['tab'], tab_after['tab'])
                self.assertGreaterEqual(tab_after['seq'], tab_before['seq'])
                report['sender_reload'] = {'same_tab_identity': True, 'sequence_before': tab_before['seq'],
                                          'sequence_after': tab_after['seq'], 'receiver_reloaded': False}
                reload_profile = {'configured_http_latency_ms': 0, 'label': 'sender_reload', 'samples': []}
                report['profiles'].append(reload_profile)
                revisions = self.pencil_sample(peer, owner_rect, peer_rect, reload_profile, 0,
                    revisions, durable_points, pid, scenario='held_pencil_after_sender_reload')
                reload_profile['summary'] = summarize_results(reload_profile['samples'])
                manifest = self.auth_cloud_document(pid)
                self.assertEqual((manifest['size']['width'], manifest['size']['height']), (320, 240))
                latest = self.auth_revision(pid, revisions)
                report['final_native_sha256'] = latest['content']['sha256']
                report['final_revision'] = latest['revision']
                self.load(peer, '?project='+pid)
                for point, expected in durable_points:
                    self.assertEqual(self.execute('document.pixel', point, peer), expected)
                report['native_convergence_and_reload'] = 'passed'
            finally:
                for session in network:
                    session.detach()

        def record_sample(self, sample, profile, observer):
            report['samples'].append(sample)
            profile['samples'].append(sample)
            name = f"{sample['scenario']}-{sample['configured_http_latency_ms']}-{sample['sample']}"
            if observer.last_baseline_png:
                (artifacts/(name+'-baseline.png')).write_bytes(observer.last_baseline_png)
            if observer.last_matching_png:
                (artifacts/(name+'-match.png')).write_bytes(observer.last_matching_png)
            print(json.dumps({key: sample.get(key) for key in ['scenario', 'configured_http_latency_ms',
                                                               'sample', 'status', 'latency_upper_ms']}), flush=True)

        def tearDown(self):
            ids = list(self.accounts)
            super().tearDown()
            remaining = self.db.execute('SELECT count(*) FROM photocraft.accounts WHERE id=ANY(%s::uuid[])', (ids,)).fetchone()[0]
            self.assertEqual(remaining, 0)
            report['cleanup'] = {'synthetic_accounts_removed': len(ids), 'remaining_accounts': remaining}

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([LivePaint('test_cursor_and_held_native_pencil')]))
    report['correctness_status'] = 'passed' if result.wasSuccessful() else 'failed'
    report['failures'] = [detail for _, detail in result.failures+result.errors]
    report['summary'] = summarize_results(report['samples'])
    expected_count = len(args.latency_ms)*args.samples*2+1
    complete = len(report['samples']) == expected_count
    report['missing_samples'] = expected_count-len(report['samples'])
    report['preferred_150ms_passed'] = complete and all(s['status'] == 'passed' and s['latency_upper_ms'] < 150 for s in report['samples'])
    report['status'] = 'passed' if result.wasSuccessful() and complete and report['summary']['all_passed'] else 'failed'
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    report['environment']['load_average_end'] = os.getloadavg()
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(f'Live paint benchmark {report["status"]}: {args.output}', flush=True)
    raise SystemExit(0 if report['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
