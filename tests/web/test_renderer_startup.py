"""Real-pixel startup coverage for native eframe WebGPU/WebGL backend selection.

Synthetic local pages only; no authentication, mail or hosted requests. The native
editor must actually paint, rather than merely exposing its command bridge.
"""
import io
import json
import os
from pathlib import Path
import time
import unittest
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import sync_playwright
from visual_assertions import workspace_controls

BASE = os.environ.get('PHOTOCRAFT_TEST_ORIGIN', 'http://127.0.0.1:8876')
ARTIFACTS = Path(os.environ.get('PHOTOCRAFT_TEST_ARTIFACTS', 'test-results/renderer-startup'))
CHROME = os.environ.get('PHOTOCRAFT_CHROME', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')


class RendererStartup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if urlparse(BASE).hostname not in {'localhost', '127.0.0.1', '::1'}:
            raise RuntimeError('Renderer tests require an isolated loopback service')
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.pw = sync_playwright().start()

    @classmethod
    def tearDownClass(cls):
        cls.pw.stop()

    def setUp(self):
        self.browsers = []
        self.errors = []

    def tearDown(self):
        for browser in self.browsers:
            browser.close()
        self.assertEqual(self.errors, [], 'Uncaught browser error')

    def page(self, chrome=False, failure=False):
        options = {'headless': True, 'args': ['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader']}
        if chrome:
            if not Path(CHROME).is_file():
                self.skipTest('Installed hardware-capable Chrome is unavailable')
            options['executable_path'] = CHROME
        browser = self.pw.chromium.launch(**options)
        self.browsers.append(browser)
        page = browser.new_page(viewport={'width': 1440, 'height': 960})
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        messages = []
        page.on('console', lambda message: messages.append(message.text))
        if failure:
            page.add_init_script("""// Exercise device creation failure even on a software test adapter.
            Object.defineProperty(GPUAdapterInfo.prototype, 'isFallbackAdapter', {get() {return false;}});
            GPUAdapter.prototype.requestDevice = function() {
                return Promise.reject(new Error('Synthetic renderer <img src=x onerror=alert(1)> failure'));
            };""")
        return page, messages

    def workspace(self, page, messages, query=''):
        page.goto(BASE+'/'+query, wait_until='domcontentloaded')
        page.wait_for_function('typeof window.photocraftCommand === "function"', timeout=60000)
        self.painted_workspace(page, messages)

    def painted_workspace(self, page, messages):
        deadline = time.monotonic()+8
        while True:
            image = Image.open(io.BytesIO(page.screenshot(scale='css')))
            try:
                controls = workspace_controls(image)
                break
            except AssertionError:
                if time.monotonic() >= deadline:
                    image.save(ARTIFACTS/(self._testMethodName+'-failed.png'))
                    (ARTIFACTS/(self._testMethodName+'-console.json')).write_text(json.dumps(messages, indent=2))
                    raise
                page.wait_for_timeout(100)
        image.save(ARTIFACTS/(self._testMethodName+'.png'))
        self.assertFalse(any('device lost' in message.lower() for message in messages), messages)
        left, top, right, bottom = controls['actions'][1]
        page.mouse.click((left+right)/2, (top+bottom)/2)
        page.wait_for_timeout(200)
        result = page.evaluate('async()=>JSON.parse(await window.photocraftCommand("ui.inspect", "{}"))')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['result']['dialogs'][0]['kind'], 'NewDocument')
        (ARTIFACTS/(self._testMethodName+'-console.json')).write_text(json.dumps(messages, indent=2))

    def test_01_bundled_default_paints_workspace(self):
        page, messages = self.page()
        self.workspace(page, messages)
        fallback = page.evaluate('async()=>{const a=await navigator.gpu?.requestAdapter();return a?.info?.isFallbackAdapter ?? null;}')
        if fallback:
            self.assertTrue(any('software WebGPU adapter; selecting the compatible WebGL2 renderer' in m for m in messages), messages)
            self.assertTrue(any('wgpu backend Gl' in m for m in messages), messages)

    def test_02_explicit_webgl_paints_workspace(self):
        page, messages = self.page()
        self.workspace(page, messages, '?webgl')
        self.assertTrue(any('wgpu backend Gl' in m for m in messages), messages)
        self.assertFalse(any('software WebGPU adapter; selecting' in m for m in messages), messages)

    def test_03_hardware_webgpu_remains_available(self):
        page, messages = self.page(chrome=True)
        self.workspace(page, messages)
        fallback = page.evaluate('async()=>{const a=await navigator.gpu?.requestAdapter();return a?.info?.isFallbackAdapter ?? null;}')
        if fallback is False:
            self.assertTrue(any('wgpu backend BrowserWebGpu' in m for m in messages), messages)
            self.assertFalse(any('software WebGPU adapter; selecting' in m for m in messages), messages)
        else:
            self.skipTest('Installed Chrome has no hardware WebGPU adapter in this environment')

    def test_04_startup_failure_is_text_with_safe_compatible_action(self):
        page, messages = self.page(failure=True)
        page.goto(BASE+'/?project=renderer-fixture#preserved', wait_until='domcontentloaded')
        status = page.locator('#photocraft_loading')
        status.get_by_role('link', name='Try the compatible renderer').wait_for(timeout=60000)
        self.assertNotIn('Synthetic renderer', status.inner_text(), 'Technical stack obscures the recovery action')
        self.assertIn('Synthetic renderer <img src=x onerror=alert(1)> failure', status.text_content())
        self.assertEqual(status.locator('img').count(), 0, 'Renderer error became executable markup')
        link = status.get_by_role('link')
        href = link.get_attribute('href')
        self.assertEqual(urlparse(href).query, 'project=renderer-fixture&webgl')
        self.assertEqual(urlparse(href).fragment, 'preserved')
        for _ in range(3):
            page.keyboard.press('Tab')
            if link.evaluate('el=>el.matches(":focus-visible")'):
                break
        self.assertTrue(link.evaluate('el=>el.matches(":focus-visible")'))
        self.assertEqual(link.evaluate('el=>getComputedStyle(el).outlineWidth'), '3px')
        page.screenshot(path=str(ARTIFACTS/(self._testMethodName+'-error.png')))
        page.set_viewport_size({'width':390,'height':844})
        box = link.bounding_box()
        self.assertGreaterEqual(box['height'],40)
        self.assertGreaterEqual(box['x'],16)
        self.assertLessEqual(box['x']+box['width'],390-16)
        self.assertEqual(link.evaluate('el=>getComputedStyle(el).borderRadius'), '8px')
        status.locator('summary').click()
        self.assertTrue(status.locator('pre').is_visible())
        self.assertLessEqual(status.locator('pre').bounding_box()['height'],160)
        page.screenshot(path=str(ARTIFACTS/(self._testMethodName+'-phone-details.png')))
        status.locator('summary').click()
        page.set_viewport_size({'width':1440,'height':960})
        link.click()
        page.wait_for_function('typeof window.photocraftCommand === "function"', timeout=60000)
        # Explicit GL bypasses the failing WebGPU requestDevice without changing the route.
        deadline = time.monotonic()+8
        while not any('wgpu backend Gl' in m for m in messages) and time.monotonic()<deadline:
            page.wait_for_timeout(100)
        self.assertTrue(any('wgpu backend Gl' in m for m in messages), messages)
        self.painted_workspace(page, messages)


if __name__ == '__main__':
    unittest.main(verbosity=2)
