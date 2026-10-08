"""Pure paint-oracle tests plus an explicitly opted-in, in-memory Chrome probe.

Run: ../browser-venv/bin/python -m unittest discover -s tests/web -p test_paint_latency.py -v
Opt in: PHOTOCRAFT_PAINT_CALIBRATION=1 (PHOTOCRAFT_CHROME optionally selects Chrome).
No HTTP server, database, PhotoCraft runtime, or external network is needed.
"""

import base64
from collections import deque
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from paint_latency import ClockCalibration, PaintObserver, PixelOracle, _frame_swap_ms, _ms, classify_latency, summarize_results


class PaintOracleTests(unittest.TestCase):
    def test_exact_patch_rejects_old_pixels_and_wrong_location(self):
        image = Image.new('RGB', (20, 20), 'white')
        oracle = PixelOracle.solid((2, 2, 6, 6), (255, 0, 0))
        self.assertFalse(oracle.matches(image))
        image.paste('red', (10, 10, 14, 14))
        self.assertFalse(oracle.matches(image))
        image.paste('red', oracle.box)
        self.assertTrue(oracle.matches(image))

    def test_tolerance_requires_declared_fraction_and_geometry(self):
        image = Image.new('RGB', (2, 2), (251, 1, 0))
        self.assertFalse(PixelOracle.solid((0, 0, 2, 2), (255, 0, 0)).matches(image))
        self.assertTrue(PixelOracle.solid((0, 0, 2, 2), (255, 0, 0), tolerance=4).matches(image))
        image.putpixel((0, 0), (0, 0, 0))
        self.assertFalse(PixelOracle.solid((0, 0, 2, 2), (255, 0, 0), tolerance=4).matches(image))
        self.assertTrue(PixelOracle.solid((0, 0, 2, 2), (255, 0, 0), tolerance=4, minimum_fraction=.75).matches(image))
        with self.assertRaises(ValueError):
            PixelOracle.solid((0, 0, 3, 3), (0, 0, 0)).matches(image)

    def test_invalid_geometry_fails_before_allocating_an_image(self):
        with patch('paint_latency.Image.new') as allocate:
            for box in [(-1, 0, 2, 2), (0, 0, 0, 1), (0, 0, 1), (0, 0, 1.5, 2), (0, 0, 10**9, 10**9)]:
                with self.subTest(box=box), self.assertRaises(ValueError):
                    PixelOracle.solid(box, (0, 0, 0))
            allocate.assert_not_called()

    def test_frame_timestamps_must_be_present_and_plausible_at_receipt(self):
        clock = ClockCalibration(0, 0, 2, 2)
        self.assertEqual(_frame_swap_ms({'timestamp': 99.999}, clock, 100000), 99999)
        for metadata in [{}, {'timestamp': None}, {'timestamp': True}, {'timestamp': float('nan')},
                         {'timestamp': 0}, {'timestamp': 100.003}, {'timestamp': 1}, {'timestamp': 1e308}]:
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                _frame_swap_ms(metadata, clock, 100000)

    def test_uncertainty_and_delay_cannot_pass_500ms(self):
        self.assertEqual(classify_latency(100, 3, 500), 'passed')
        self.assertEqual(classify_latency(499, 3, 500), 'over_budget')
        self.assertEqual(classify_latency(600, 3, 500), 'over_budget')
        for latency, uncertainty in [(10, 16), (float('nan'), 1), (-10, 1)]:
            self.assertEqual(classify_latency(latency, uncertainty, 500), 'invalid')
        self.assertEqual(classify_latency(1, 1, 500, ['capture_queue_overflow']), 'invalid')

    def test_summary_preserves_timeouts_invalid_and_slow_samples(self):
        results = [{'status': 'passed', 'latency_upper_ms': 20},
                   {'status': 'over_budget', 'latency_upper_ms': 603},
                   {'status': 'timeout', 'latency_upper_ms': None},
                   {'status': 'invalid', 'latency_upper_ms': None}]
        summary = summarize_results(results)
        self.assertFalse(summary['all_passed'])
        self.assertEqual(summary['total'], 4)
        self.assertEqual(summary['counts'], {'passed': 1, 'over_budget': 1, 'timeout': 1, 'invalid': 1})
        self.assertEqual(summary['observed_upper_bounds']['p99_ms'], 603)
        self.assertFalse(summarize_results([])['all_passed'])

    def test_capture_queue_is_bounded_and_acknowledges_rejected_frame(self):
        class Session:
            acknowledgements = []

            def send(self, method, params):
                self.acknowledgements.append((method, params))

        # Exercise the callback without requiring a browser or hiding queue errors.
        observer = PaintObserver.__new__(PaintObserver)
        observer.cdp = Session()
        observer.frames, observer.errors = deque(), []
        observer.queue_bytes = observer.error_count = observer.frame_count = 0
        observer.max_frames, observer.max_queue_bytes, observer.max_frame_bytes = 1, 20, 10
        for sequence, encoded in enumerate(['AAAA', 'BBBB', 'X'*50]):
            observer._frame({'sessionId': sequence, 'data': encoded, 'metadata': {'timestamp': 1}})
        observer._frame({'sessionId': 3, 'data': 'CCCC', 'metadata': {}})
        self.assertEqual(len(observer.frames), 1)
        self.assertEqual(observer.queue_bytes, 4)
        self.assertEqual(observer.errors, ['capture_queue_overflow', 'frame_too_large', 'missing_or_invalid_frame_timestamp'])
        self.assertEqual(len(observer.cdp.acknowledgements), 4)

    def test_bad_callback_timestamps_invalidate_the_complete_measurement(self):
        buffer = io.BytesIO()
        Image.new('RGB', (20, 20), 'white').save(buffer, format='PNG')
        png = buffer.getvalue()

        class Page:
            url = 'about:blank'
            input_epoch = None

            def screenshot(self, **_):
                return png

            def evaluate(self, script, _):
                if script == '(key) => window[key].event':
                    return {'trusted': True, 'epoch_ms': self.input_epoch}

        class Session:
            def send(self, method, *_):
                if method == 'Page.startScreencast':
                    observer._frame({'sessionId': 0, 'data': base64.b64encode(png).decode(),
                                     'metadata': {'timestamp': _ms()/1000}})

        for timestamp, expected_error in [
            (None, 'missing_or_invalid_frame_timestamp'),
            (1e20, 'frame_timestamp_after_receive'),
            (1, 'frame_timestamp_too_old'),
        ]:
            with self.subTest(timestamp=timestamp):
                page = Page()
                observer = PaintObserver.__new__(PaintObserver)
                observer.page, observer.cdp = page, Session()
                observer.frames, observer.errors = deque(), []
                observer.queue_bytes = observer.error_count = observer.frame_count = 0
                observer.max_frames, observer.max_queue_bytes, observer.max_frame_bytes = 4, 10000, 10000
                observer.max_frame_pixels = 10000

                def trigger():
                    page.input_epoch = _ms()
                    observer._frame({'sessionId': 1, 'data': base64.b64encode(png).decode(),
                                     'metadata': {} if timestamp is None else {'timestamp': timestamp}})

                with patch.object(ClockCalibration, 'capture', return_value=ClockCalibration(0, 0, 1, 0)):
                    result = observer.measure(page, trigger, PixelOracle.solid((0, 0, 10, 10), (255, 0, 0)))
                self.assertEqual(result['status'], 'invalid', result)
                self.assertIn(expected_error, result['errors'])
                self.assertIsNone(result['latency_ms'])
                self.assertEqual(result['capture_errors'], 1)

    def test_unready_capture_is_invalid_and_never_dispatches_input(self):
        clock = {'now': 100000}

        class Page:
            url = 'about:blank'

            def evaluate(self, *_):
                pass

            def wait_for_timeout(self, delay):
                clock['now'] += delay

        observer = PaintObserver.__new__(PaintObserver)
        observer.page, observer.cdp = Page(), Mock()
        observer.frames, observer.errors = deque(), []
        observer.queue_bytes = observer.error_count = observer.frame_count = 0
        trigger = Mock()
        with patch.object(ClockCalibration, 'capture', return_value=ClockCalibration(0, 0, 1, 0)), \
                patch('paint_latency._ms', side_effect=lambda: clock['now']):
            result = observer.measure(observer.page, trigger, PixelOracle.solid((0, 0, 10, 10), (255, 0, 0)))
        self.assertEqual(result['status'], 'invalid', result)
        self.assertEqual(result['errors'], ['capture_not_ready'])
        self.assertIsNone(result['input'])
        self.assertIsNone(result['timed_captured_frames'])
        trigger.assert_not_called()
        self.assertEqual(clock['now'], 102000, 'Readiness wait must be bounded')


@unittest.skipUnless(os.environ.get('PHOTOCRAFT_PAINT_CALIBRATION') == '1', 'opt-in in-memory Chromium calibration')
class ChromiumPaintCalibration(unittest.TestCase):
    def test_trusted_input_swapped_pixels_and_negative_controls(self):
        from playwright.sync_api import sync_playwright

        results = []
        artifacts = Path(os.environ.get('PHOTOCRAFT_PAINT_ARTIFACTS', 'test-results/paint-calibration'))
        artifacts.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get('PHOTOCRAFT_CHROME'), headless=True)
            try:
                context = browser.new_context(viewport={'width': 320, 'height': 240}, device_scale_factor=1)
                context.route('**/*', lambda route: route.abort())
                page = context.new_page()
                page.set_content('''<style>body{margin:0}canvas{display:block}</style>
                    <canvas width="320" height="240"></canvas><script>
                    const canvas=document.querySelector('canvas'), ctx=canvas.getContext('2d');
                    window.mode='immediate'; window.revision=0;
                    window.clear=()=>{ctx.fillStyle='white';ctx.fillRect(0,0,320,240)}; clear();
                    canvas.addEventListener('pointerdown',()=>{
                        window.revision++;
                        const paint=()=>{ctx.fillStyle='#ef1735';ctx.fillRect(80,70,40,40)};
                        if(mode==='immediate')paint(); else if(mode==='delayed')setTimeout(paint,600);
                    });</script>''')
                oracle = PixelOracle.solid((85, 75, 95, 85), (239, 23, 53))
                with PaintObserver(page) as observer:
                    for mode in ['immediate', 'delayed', 'frozen']:
                        page.evaluate('(mode)=>{window.mode=mode;clear()}', mode)
                        page.wait_for_timeout(50)
                        result = observer.measure(page, lambda: page.mouse.click(90, 80), oracle,
                                                  target_ms=500, timeout_ms=1500 if mode != 'frozen' else 700)
                        result['scenario'] = mode
                        results.append(result)
                        if observer.last_matching_png:
                            (artifacts/f'{mode}.png').write_bytes(observer.last_matching_png)
                    # An already visible target is not evidence that this input painted it.
                    page.evaluate("()=>{ctx.fillStyle='#ef1735';ctx.fillRect(80,70,40,40)}")
                    page.wait_for_timeout(50)
                    unchanged = observer.measure(page, lambda: page.mouse.click(90, 80), oracle)
                    unchanged['scenario'] = 'unchanged'
                    results.append(unchanged)
                    self.assertEqual(page.evaluate('revision'), 3, 'Frozen pixels must still allow semantic state to change')
                    for scenario, trigger in [
                        ('synthetic_input', lambda: page.dispatch_event('canvas', 'pointerdown', {'clientX': 90, 'clientY': 80})),
                        ('missing_input', lambda: None),
                    ]:
                        page.evaluate("()=>{window.mode='immediate';clear()}")
                        result = observer.measure(page, trigger, oracle)
                        result['scenario'] = scenario
                        results.append(result)
                # The receiver remains behind a separate active sender context.
                # This is an in-memory relay calibration, not a collaboration transport.
                sender_context = browser.new_context(viewport={'width': 320, 'height': 240})
                sender_context.route('**/*', lambda route: route.abort())
                sender = sender_context.new_page()
                sender.set_content('<button style="width:200px;height:150px">Input</button>')
                page.evaluate('clear()')
                sender.bring_to_front()

                def relay():
                    sender.mouse.click(90, 80)
                    page.evaluate("()=>{ctx.fillStyle='#ef1735';ctx.fillRect(80,70,40,40)}")
                    sender.wait_for_timeout(50)  # frames must survive a trigger that pumps events

                with PaintObserver(page) as observer:
                    remote = observer.measure(sender, relay, oracle)
                    remote['scenario'] = 'separate_background_receiver'
                    results.append(remote)
                    if observer.last_matching_png:
                        (artifacts/'background-receiver.png').write_bytes(observer.last_matching_png)
                report = {'browser_version': browser.version, 'scope': 'in-memory calibration, no PhotoCraft/network performance claim',
                          'samples': results, 'summary': summarize_results(results)}
                (artifacts/'calibration.json').write_text(json.dumps(report, indent=2)+'\n')
                self.assertEqual([r['status'] for r in results],
                                 ['passed', 'over_budget', 'timeout', 'invalid', 'invalid', 'invalid', 'passed'], results)
                self.assertTrue(results[0]['input']['trusted'])
                for result in results:
                    self.assertIn('capture_baseline', result, 'No input result is valid without CDP capture readiness')
                    if result['status'] in {'passed', 'over_budget'}:
                        self.assertGreaterEqual(result['timed_captured_frames'], 1)
                        self.assertEqual((result['frame']['width'], result['frame']['height']),
                                         (result['capture_baseline']['width'], result['capture_baseline']['height']))
                self.assertGreater(results[1]['latency_lower_ms'], 550)
                self.assertIn('expected_pixels_already_visible_before_input', results[3]['errors'])
                self.assertFalse(results[4]['input']['trusted'])
                self.assertIsNone(results[5]['input'])
                for result in results[4:6]:
                    self.assertIn('missing_or_untrusted_input', result['errors'])
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
