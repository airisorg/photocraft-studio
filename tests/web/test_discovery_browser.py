"""Read-only rendered discovery checks, with fresh contexts and no editor bootstrap.

Defaults to the disposable loopback service. An explicitly supplied HTTPS origin
also permits operator-owned public-page acceptance; no cookies or accounts are reused.
"""
import json
import os
from pathlib import Path
import re
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


BASE = os.environ.get('PHOTOCRAFT_TEST_ORIGIN', 'http://127.0.0.1:8876').rstrip('/')
ARTIFACTS = Path(os.environ.get('PHOTOCRAFT_TEST_ARTIFACTS', 'test-results/discovery'))
WIDTHS = (320, 390, 768, 1440)


def contrast(first, second):
    def luminance(value):
        channels = [float(part) / 255 for part in re.findall(r'[\d.]+', value)[:3]]
        if len(channels) != 3:
            raise AssertionError('Expected an opaque resolved CSS RGB color')
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
        return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + .05) / (low + .05)


class DiscoveryBrowser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        origin = urlsplit(BASE)
        if (origin.scheme not in {'http', 'https'} or not origin.hostname
                or origin.username or origin.password or origin.path or origin.query or origin.fragment
                or (origin.scheme != 'https' and origin.hostname not in {'localhost', '127.0.0.1', '::1'})):
            raise RuntimeError('Use an origin-only HTTPS URL or an isolated HTTP loopback origin')
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch(
                headless=True, executable_path=os.environ.get('PHOTOCRAFT_CHROME'))
        except BaseException:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            cls.browser.close()
        finally:
            cls.pw.stop()

    def readable(self, locator, width, *, action=False):
        locator.scroll_into_view_if_needed()
        self.assertTrue(locator.is_visible())
        measured = locator.evaluate("""el => {
          const style = getComputedStyle(el), box = el.getBoundingClientRect();
          let background = 'rgb(255, 255, 255)';
          for (let p = el; p; p = p.parentElement) {
            const color = getComputedStyle(p).backgroundColor;
            if (color !== 'rgba(0, 0, 0, 0)' && color !== 'transparent') { background = color; break; }
          }
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT), text = [];
          for (let node; (node = walker.nextNode());) {
            if (!node.textContent.trim()) continue;
            const range = document.createRange(); range.selectNodeContents(node);
            for (const r of range.getClientRects()) text.push({left:r.left, right:r.right, top:r.top, bottom:r.bottom});
          }
          const hit = document.elementFromPoint(box.x + box.width/2, box.y + box.height/2);
          let unclipped = true, opaque = style.opacity === '1';
          for (let p = el.parentElement; p; p = p.parentElement) {
            const s = getComputedStyle(p), r = p.getBoundingClientRect();
            opaque &&= s.opacity === '1';
            if (/(hidden|clip|scroll|auto)/.test(s.overflowX) && (box.left < r.left-.5 || box.right > r.right+.5)) unclipped = false;
            if (/(hidden|clip|scroll|auto)/.test(s.overflowY) && (box.top < r.top-.5 || box.bottom > r.bottom+.5)) unclipped = false;
          }
          return {left:box.left, right:box.right, top:box.top, bottom:box.bottom,
            width:box.width, height:box.height, font:parseFloat(style.fontSize),
            line:parseFloat(style.lineHeight), color:style.color, background,
            opacity:style.opacity, visibility:style.visibility, opaque, unclipped, hit:!!hit && el.contains(hit), text};
        }""")
        self.assertGreaterEqual(measured['left'], -0.5, measured)
        self.assertLessEqual(measured['right'], width + 0.5, measured)
        self.assertGreaterEqual(measured['top'], -0.5, measured)
        self.assertLessEqual(measured['bottom'], 960.5, measured)
        self.assertTrue(measured['hit'], 'Text/control is covered by another painted element')
        self.assertTrue(measured['unclipped'], 'Text/control exceeds an ancestor clipping boundary')
        self.assertTrue(measured['opaque'], 'Text/control is faded through an ancestor')
        self.assertEqual(measured['opacity'], '1')
        self.assertEqual(measured['visibility'], 'visible')
        self.assertGreaterEqual(measured['font'], 14)
        self.assertGreaterEqual(measured['line'], measured['font'] * 1.35)
        self.assertGreaterEqual(contrast(measured['color'], measured['background']), 4.5)
        self.assertTrue(measured['text'])
        for line in measured['text']:
            self.assertGreaterEqual(line['left'], measured['left'] - .5)
            self.assertLessEqual(line['right'], measured['right'] + .5)
            self.assertGreaterEqual(line['top'], measured['top'] - .5)
            self.assertLessEqual(line['bottom'], measured['bottom'] + .5)
        if action:
            self.assertGreaterEqual(measured['height'], 44)
            self.assertGreaterEqual(measured['width'], 44)
        return measured

    def journey(self, width, javascript):
        label = f'discovery-{width}-' + ('script-enabled' if javascript else 'no-script')
        record = {'width': width, 'javascript_enabled': javascript, 'passed': False,
                  'browser': self.browser.version, 'origin': BASE, 'context_closed': False,
                  'requests': [], 'blocked': [], 'page_errors': [], 'request_failures': [], 'responses': []}
        context = self.browser.new_context(viewport={'width': width, 'height': 960},
            device_scale_factor=1, java_script_enabled=javascript, service_workers='block')
        page = None
        try:
            self.assertEqual(context.cookies(), [])

            def request_guard(route):
                request = route.request
                parsed = urlsplit(request.url)
                entry = {'path': parsed.path, 'method': request.method, 'type': request.resource_type}
                record['requests'].append(entry)
                # Browser-initiated favicon lookup is also a public GET, never an account call.
                allowed = (f'{parsed.scheme}://{parsed.netloc}' == BASE and request.method == 'GET'
                           and not parsed.query and parsed.path in {'/about.html', '/media/editor.png',
                                                                  '/media/collaboration-preview.png', '/favicon.ico'}
                           and request.resource_type in {'document', 'image', 'other'})
                if allowed:
                    route.continue_()
                else:
                    record['blocked'].append(entry)
                    route.abort()

            context.route('**/*', request_guard)
            page = context.new_page()
            page.on('pageerror', lambda error: record['page_errors'].append(str(error)))
            page.on('requestfailed', lambda request: record['request_failures'].append(
                {'path': urlsplit(request.url).path, 'failure': request.failure}))
            page.on('response', lambda response: record['responses'].append(
                {'path': urlsplit(response.url).path, 'status': response.status}))
            response = page.goto(BASE + '/about.html', wait_until='load', timeout=30000)
            self.assertIsNotNone(response)
            self.assertEqual(response.status, 200)
            self.assertEqual(page.url, BASE + '/about.html')
            self.assertIn('PhotoCraft Studio', page.title())
            self.assertEqual(page.locator('script, canvas, iframe, form').count(), 0)
            self.assertEqual(page.evaluate('typeof window.photocraftCommand'), 'undefined')
            videos = page.locator('video')
            self.assertEqual(videos.count(), 2)
            for i in range(videos.count()):
                self.assertEqual(videos.nth(i).evaluate('v=>[v.paused,v.preload,v.autoplay,v.loop,v.controls]'),
                                 [True, 'none', False, False, True])
            self.assertIn('Local test workspace with disposable accounts', page.locator('main').inner_text())
            image = page.locator('main figure img')
            self.assertEqual(image.evaluate('el=>[el.complete, el.naturalWidth, el.naturalHeight]'), [True, 1440, 960])
            self.assertLessEqual(page.evaluate('Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)'), width)
            self.assertGreater(page.locator('main').bounding_box()['height'], 300)
            essential = [
                page.get_by_text('This working Rust and WebAssembly app is hosted on Tofu.', exact=False),
                page.get_by_text('The ArtCraft team and PhotoCraft contributors created', exact=False),
                page.get_by_text('It is not affiliated with, sponsored by or endorsed', exact=False),
                page.get_by_text('PhotoCraft Studio is early-alpha software', exact=False),
                page.get_by_text('Live previews support painting and layer moves.', exact=False),
            ]
            record['essential_text'] = [self.readable(item, width) for item in essential]
            actions = page.locator('a.button')
            self.assertEqual(actions.count(), 5)
            record['actions'] = [dict(name=actions.nth(i).inner_text(), **self.readable(actions.nth(i), width, action=True))
                                 for i in range(actions.count())]
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(ARTIFACTS / f'{label}.png'), full_page=True)
            if javascript:
                # Trusted keyboard events, without activating editor/external links.
                page.locator('body').click(position={'x': 1, 'y': 1})
                reached = set()
                for _ in range(page.locator('a').count() + 2):
                    page.keyboard.press('Tab')
                    focused = page.locator('a.button:focus-visible')
                    if focused.count():
                        self.readable(focused, width, action=True)
                        outline = focused.evaluate('el=>{const s=getComputedStyle(el);return [s.outlineStyle,parseFloat(s.outlineWidth)];}')
                        self.assertNotEqual(outline[0], 'none')
                        self.assertGreaterEqual(outline[1], 2)
                        reached.add(focused.inner_text())
                        if len(reached) == actions.count():
                            break
                self.assertEqual(reached, set(actions.all_inner_texts()))
                record['keyboard_actions'] = sorted(reached)
                page.screenshot(path=str(ARTIFACTS / f'{label}-focus.png'))
            self.assertEqual(record['blocked'], [])
            self.assertEqual(record['page_errors'], [])
            self.assertEqual(record['request_failures'], [])
            self.assertTrue({'/about.html', '/media/editor.png'} <= {r['path'] for r in record['requests']})
            self.assertTrue(all(r['status'] == 200 for r in record['responses'] if r['path'] != '/favicon.ico'))
            record['passed'] = True
        finally:
            try:
                if page and not record['passed']:
                    page.screenshot(path=str(ARTIFACTS / f'{label}-failed.png'), full_page=True)
            except Exception as error:
                record['failure_capture_error'] = str(error)
            finally:
                try:
                    context.close()
                    record['context_closed'] = True
                except Exception as error:
                    record['passed'] = False
                    record['cleanup_error'] = str(error)
                    raise
                finally:
                    (ARTIFACTS / f'{label}.json').write_text(json.dumps(record, indent=2) + '\n')

    def test_01_no_javascript_public_content_and_responsive_actions(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                self.journey(width, javascript=False)

    def test_02_static_page_keyboard_actions_at_all_widths(self):
        for width in WIDTHS:
            with self.subTest(width=width):
                self.journey(width, javascript=True)

    def test_03_real_mp4_playback_starts_only_on_user_request(self):
        record = {'origin': BASE, 'passed': False, 'requests': [], 'blocked': [], 'errors': [], 'videos': []}
        context = self.browser.new_context(viewport={'width': 1440, 'height': 960}, service_workers='block')
        allowed = {'/about.html', '/media/editor.png', '/media/collaboration-preview.png', '/favicon.ico',
                   '/media/editing-walkthrough.mp4', '/media/collaboration-demo.mp4'}
        page = None
        try:
            def guard(route):
                request = route.request
                parsed = urlsplit(request.url)
                record['requests'].append({'path': parsed.path, 'method': request.method, 'type': request.resource_type})
                if (f'{parsed.scheme}://{parsed.netloc}' == BASE and request.method == 'GET'
                        and not parsed.query and parsed.path in allowed):
                    route.continue_()
                else:
                    record['blocked'].append(parsed.path)
                    route.abort()
            context.route('**/*', guard)
            page = context.new_page()
            page.on('pageerror', lambda error: record['errors'].append(str(error)))
            self.assertEqual(page.goto(BASE + '/about.html', wait_until='load').status, 200)
            self.assertFalse([request for request in record['requests'] if request['path'].endswith('.mp4')],
                             'Videos must not download on page load')
            videos = page.locator('video')
            self.assertEqual(videos.count(), 2)
            for i in range(videos.count()):
                video = videos.nth(i)
                source = video.locator('source').get_attribute('src')
                self.assertFalse([request for request in record['requests'] if request['path'] == source],
                                 'Each video must stay unloaded until its own Play action')
                video.scroll_into_view_if_needed()
                box = video.bounding_box()
                self.assertIsNotNone(box)
                # Activate the browser's actual Play control with trusted input.
                page.mouse.move(box['x'] + 24, box['y'] + box['height'] - 38)
                page.mouse.click(box['x'] + 24, box['y'] + box['height'] - 38)
                handle = video.element_handle()
                page.wait_for_function('v=>!v.paused && v.readyState>=2 && v.currentTime>.15', arg=handle, timeout=20000)
                first = video.evaluate('v=>v.currentTime')
                page.wait_for_timeout(250)
                measured = video.evaluate('v=>({time:v.currentTime,width:v.videoWidth,height:v.videoHeight,duration:v.duration,error:v.error&&v.error.code})')
                self.assertGreater(measured['time'], first)
                self.assertGreaterEqual(measured['width'], 1280)
                self.assertGreaterEqual(measured['height'], 720)
                self.assertGreater(measured['duration'], 15)
                self.assertLess(measured['duration'], 90)
                self.assertIsNone(measured['error'])
                decoded_box = video.bounding_box()
                self.assertIsNotNone(decoded_box)
                for dimension in ('width', 'height'):
                    self.assertLessEqual(abs(decoded_box[dimension] - box[dimension]), 1,
                                         'Playback must not shift the reserved video layout')
                measured['layout_before_play'] = box
                measured['layout_after_decode'] = decoded_box
                # Decode can change the intrinsic dimensions; use current geometry.
                video.scroll_into_view_if_needed()
                box = video.bounding_box()
                self.assertIsNotNone(box)
                page.mouse.move(box['x'] + 24, box['y'] + box['height'] - 38)
                page.mouse.click(box['x'] + 24, box['y'] + box['height'] - 38)
                page.wait_for_function('v=>v.paused', arg=handle, timeout=3000)
                self.assertTrue(video.evaluate('v=>v.paused'))
                record['videos'].append(measured)
                page.screenshot(path=str(ARTIFACTS / f'discovery-playback-{i}.png'))
            self.assertEqual(record['blocked'], [])
            self.assertEqual(record['errors'], [])
            record['passed'] = True
        finally:
            try:
                context.close()
                record['context_closed'] = True
            finally:
                (ARTIFACTS / 'discovery-video-playback.json').write_text(json.dumps(record, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
