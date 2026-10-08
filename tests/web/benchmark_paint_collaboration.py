"""Bounded loopback-only trusted Pencil input -> collaborator canvas paint benchmark.

Uses the original native Pencil tool, two independent synthetic accounts and the accepted
WASM build. The 500 ms gate may fail while setup, convergence and cleanup pass. No hosted
accounts, provider mail, runtime changes or production load are involved.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import time
import unittest
from urllib.parse import urlparse
import uuid

from benchmark_collaboration import distribution, local_database
from paint_latency import PaintObserver, PixelOracle


def canvas_rectangle(image):
    """Locate the fixture's actual 320x240 white canvas at pinned 100%/DPR1."""
    image = image.convert('RGB')
    rows = []
    for y in range(180, min(900, image.height)):
        start = None
        for x in range(88, min(1096, image.width)):
            white = min(image.getpixel((x, y))) >= 250
            if white and start is None:
                start = x
            if start is not None and (not white or x == min(1096, image.width)-1):
                end = x if not white else x+1
                if end-start == 320:
                    rows.append((start, y, end))
                start = None
    if len(rows) != 240 or len({(left, right) for left, _, right in rows}) != 1:
        raise AssertionError(f'Expected one actual 320x240 white canvas: {len(rows)} rows')
    left, top, right = rows[0]
    if [y for _, y, _ in rows] != list(range(top, top+240)):
        raise AssertionError('Canvas rows are not contiguous')
    return left, top, right, top+240


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', default=os.environ.get('PHOTOCRAFT_TEST_ORIGIN', 'http://127.0.0.1:8876'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--context', required=True)
    parser.add_argument('--samples', type=int, default=5, choices=range(1, 21))
    args = parser.parse_args()
    origin = urlparse(args.origin)
    if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or origin.username
            or origin.password or origin.query or origin.fragment or origin.path not in {'', '/'}):
        raise ValueError('Benchmark requires a literal loopback HTTP origin')
    local_database(os.environ.get('PHOTOCRAFT_TEST_DATABASE_URL', 'postgresql://photocraft_test@127.0.0.1:55438/postgres'))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    artifacts = args.output.parent / (args.output.stem+'-screenshots')
    os.environ['PHOTOCRAFT_TEST_ORIGIN'] = args.origin.rstrip('/')
    os.environ['PHOTOCRAFT_TEST_ARTIFACTS'] = str(artifacts)
    from test_browser import BrowserAcceptance, BASE, Image

    report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'status': 'running',
        'scope': 'Two synthetic accounts; local headless Chromium; native Pencil input to first observed matching collaborator compositor frame.',
        'environment': {'system': platform.platform(), 'cpus': os.cpu_count(),
            'load_average_start': os.getloadavg(), 'context': args.context, 'origin': BASE,
            'total_clients': 2, 'viewport': [1440, 960], 'device_scale_factor': 1, 'zoom': 1,
            'browser_launch_flags': ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader'],
            'gpu_adapter': 'not measured; headless browser is not physical-device GPU evidence',
            'network': 'loopback; no configured network delay; RTT not separately measured'},
        'target_ms': 500, 'samples': [], 'cursor_delivery': {'status': 'not_measured',
            'reason': 'This journey measures Pencil pixels; benchmark_live_collaboration.py measures cursor delivery.'},
        'timing_policy': 'Trusted pointerdown to first matching swapped pixels, including a transient preview when enabled. Native persistence is verified separately outside this visual timer; runtime timer values are not inferred.',
        'quantiles': 'Small bounded sample; nearest-rank p95/p99 are maxima, not stable tail estimates.'}

    class PaintLatency(BrowserAcceptance):
        def test_trusted_pencil_to_remote_pixels(self):
            self.signed_in()
            self.new(320, 240)
            self.command('ui.set', {'tool': 'pencil', 'zoom': 1, 'center': [160, 120]})
            self.execute('tools.setBrush', {'reset': True, 'size': 24, 'opacity': 1, 'flow': 1, 'hardness': 1})
            self.execute('tools.setColors', {'foreground': '#19c563', 'background': '#ffffff'})
            self.page.mouse.move(0, 0)
            self.auth_save(bound=False)
            project = self.wait_revision(1)
            meta = self.context.request.get(BASE+f'/api/projects/{project["id"]}').json()
            report['fixture'] = {'width': 320, 'height': 240, 'layers': 1,
                'initial_bytes': meta['content']['bytes'], 'initial_sha256': meta['content']['sha256']}
            report['environment']['browser'] = self.browser.version
            report['environment']['wasm_assets'] = sorted(set(re.findall(r'photocraft-web-[a-z0-9]+(?:_bg\.wasm|\.js)', self.page.content())))

            peer_id, token = str(uuid.uuid4()), secrets.token_hex(32)
            email = peer_id+'@example.invalid'
            self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)', (peer_id, email, 'Paint latency peer'))
            self.accounts.append(peer_id)
            self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)', (hashlib.sha256(token.encode()).hexdigest(), peer_id))
            granted = self.context.request.put(BASE+f'/api/projects/{project["id"]}/members', headers={'Origin': BASE}, data={'email': email, 'role': 'edit'})
            self.assertTrue(granted.ok, granted.text())
            _, peer = self.context_page('?project='+project['id'], token=token)
            self.command('ui.set', {'zoom': 1, 'center': [160, 120]}, peer)
            peer.mouse.move(0, 0)
            peer.wait_for_timeout(200)
            owner_rect = canvas_rectangle(Image.open(io.BytesIO(self.page.screenshot())))
            peer_rect = canvas_rectangle(Image.open(io.BytesIO(peer.screenshot())))
            report['fixture']['owner_canvas_rect'] = owner_rect
            report['fixture']['peer_canvas_rect'] = peer_rect

            for index in range(args.samples):
                x, y = 40+60*(index % 4), 40+35*(index // 4)
                ox, oy = owner_rect[0]+x, owner_rect[1]+y
                px, py = peer_rect[0]+x, peer_rect[1]+y
                oracle = PixelOracle.solid((px-3, py-3, px+4, py+4), (25, 197, 99), tolerance=3)

                def pencil_click():
                    self.page.mouse.click(ox, oy)
                    self.page.mouse.move(0, 0)

                # Capture only this sample; canonical waits below must not fill the frame queue.
                with PaintObserver(peer) as observer:
                    sample = observer.measure(self.page, pencil_click, oracle,
                        event_type='pointerdown', target_ms=500, timeout_ms=12000)
                sample.update(sample=index+1, document_point=[x, y], cloud_revision=index+2)
                report['samples'].append(sample)
                print(json.dumps({'sample': index+1, 'status': sample['status'],
                    'latency_upper_ms': sample.get('latency_upper_ms')}), flush=True)
                # Correctness checks are outside the timed path. A timeout/invalid capture
                # is retained, never converted into a fast timing result by state polling.
                self.wait_revision(index+2)
                expected = self.execute('document.pixel', {'x': x, 'y': y})
                self.assertEqual(len(expected), 4)
                self.assertGreater(expected[1], expected[0]+.1)
                self.assertGreater(expected[1], expected[2]+.1)
                # A live preview can paint before either peer adopts the committed
                # native archive. Owner commit is not receiver canonical readiness.
                started = time.monotonic()
                deadline = started+10
                pixel = self.execute('document.pixel', {'x': x, 'y': y}, peer)
                while pixel != expected and time.monotonic() < deadline:
                    peer.wait_for_timeout(40)
                    pixel = self.execute('document.pixel', {'x': x, 'y': y}, peer)
                sample['native_convergence_wait_ms'] = (time.monotonic()-started)*1000
                self.assertEqual(pixel, expected, 'Peer native composite pixel did not converge within10s')
                sample['native_pixel'] = expected
                self.assertEqual(self.inspect(peer)['document']['width'], 320)
                self.assertEqual(len(self.inspect(peer)['document']['layers']), 1)
                if observer.last_matching_png:
                    (artifacts/f'peer-match-{index+1:02}.png').write_bytes(observer.last_matching_png)
                self.page.wait_for_timeout(100)

            latest = self.auth_revision(project['id'], args.samples+1)
            manifest = self.auth_cloud_document(project['id'])
            self.assertEqual((manifest['size']['width'], manifest['size']['height']), (320, 240))
            report['final_revision'] = latest['revision']
            report['final_native_sha256'] = latest['content']['sha256']
            self.load(peer, '?project='+project['id'])
            for index in range(args.samples):
                point = {'x': 40+60*(index % 4), 'y': 40+35*(index // 4)}
                self.assertEqual(self.execute('document.pixel', point), self.execute('document.pixel', point, peer))
            report['native_convergence_and_reload'] = 'passed'
            self.page.screenshot(path=str(artifacts/'owner-final.png'))
            peer.screenshot(path=str(artifacts/'peer-final.png'))

        def tearDown(self):
            ids = list(self.accounts)
            super().tearDown()
            remaining = self.db.execute('SELECT count(*) FROM photocraft.accounts WHERE id=ANY(%s::uuid[])', (ids,)).fetchone()[0]
            self.assertEqual(remaining, 0)
            report['cleanup'] = {'synthetic_accounts_removed': len(ids), 'remaining_accounts': remaining}

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([PaintLatency('test_trusted_pencil_to_remote_pixels')]))
    report['correctness_status'] = 'passed' if result.wasSuccessful() else 'failed'
    report['failures'] = [detail for _, detail in result.failures+result.errors]
    report['paint_gate'] = 'passed' if len(report['samples']) == args.samples and all(s['status'] == 'passed' for s in report['samples']) else 'failed'
    timings = [s['latency_upper_ms'] for s in report['samples']
               if s['status'] in {'passed', 'over_budget'} and s.get('latency_upper_ms') is not None]
    report['summary'] = distribution(timings) if timings else None
    report['invalid_or_missing_samples'] = args.samples-len(timings)
    report['status'] = 'passed' if result.wasSuccessful() and report['paint_gate'] == 'passed' else 'failed'
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    report['environment']['load_average_end'] = os.getloadavg()
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(f'Paint benchmark {report["status"]}: {args.output}', flush=True)
    raise SystemExit(0 if report['status'] == 'passed' else 1)


if __name__ == '__main__':
    main()
