"""Browser acceptance tests against a disposable local cloud service.

Uses PhotoCraft's existing command protocol for deterministic editor operations, plus real
mouse input, file choosers, downloads, IndexedDB, HTTP and independent browser contexts.
Never seeds or stress-tests a hosted app. Set PHOTOCRAFT_CHROME to use installed Chrome.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import time
import unittest
from urllib.parse import urlparse
import uuid
import zipfile

from PIL import Image
from playwright.sync_api import sync_playwright
import psycopg

BASE = os.environ.get('PHOTOCRAFT_TEST_ORIGIN', 'http://127.0.0.1:8876')
DATABASE = os.environ.get('PHOTOCRAFT_TEST_DATABASE_URL', 'postgresql://photocraft_test@127.0.0.1:55438/postgres')
ARTIFACTS = Path(os.environ.get('PHOTOCRAFT_TEST_ARTIFACTS', 'test-results/browser'))


class BrowserAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if any(urlparse(v).hostname not in {'localhost', '127.0.0.1', '::1'} for v in [BASE, DATABASE]):
            raise RuntimeError('Synthetic account tests require isolated loopback services')
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(
            executable_path=os.environ.get('PHOTOCRAFT_CHROME'), headless=True,
            args=['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader'])
        cls.db = psycopg.connect(DATABASE, autocommit=True)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.contexts = []
        self.accounts = []
        self.errors = []
        self.context, self.page = self.context_page()

    def tearDown(self):
        for i, context in enumerate(self.contexts):
            for j, page in enumerate(context.pages):
                if not page.is_closed():
                    page.screenshot(path=str(ARTIFACTS / f'{self._testMethodName}-{i}-{j}.png'))
            context.close()
        if self.accounts:
            self.db.execute('DELETE FROM photocraft.projects WHERE owner_id=ANY(%s::uuid[])', (self.accounts,))
            self.db.execute('DELETE FROM photocraft.sessions WHERE account_id=ANY(%s::uuid[])', (self.accounts,))
            self.db.execute('DELETE FROM photocraft.accounts WHERE id=ANY(%s::uuid[])', (self.accounts,))
        self.assertEqual(self.errors, [], 'Uncaught browser errors')

    def context_page(self, query='', viewport=None, token=None, browser=None):
        ctx = (browser or self.browser).new_context(viewport=viewport or {'width': 1440, 'height': 960}, accept_downloads=True)
        self.contexts.append(ctx)
        if token:
            ctx.add_cookies([{'name': 'pc_session', 'value': token, 'url': BASE, 'httpOnly': True, 'sameSite': 'Lax'}])
        page = ctx.new_page()
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        self.load(page, query)
        return ctx, page

    def load(self, page, query=''):
        page.goto(BASE + '/' + query, wait_until='networkidle', timeout=90000)
        page.wait_for_function('typeof window.photocraftCommand === "function"', timeout=60000)
        page.wait_for_timeout(400)

    def command(self, method, params=None, page=None):
        result = (page or self.page).evaluate(
            'async ([m,p]) => JSON.parse(await window.photocraftCommand(m, JSON.stringify(p)))', [method, params or {}])
        self.assertTrue(result['ok'], result)
        return result.get('result')

    def execute(self, command, params=None, page=None):
        return self.command('engine.execute', {'id': command, 'params': params or {}}, page)

    def inspect(self, page=None):
        return self.command('ui.inspect', page=page)

    def new(self, width=320, height=240, page=None):
        self.execute('file.new', {'width': width, 'height': height, 'name': 'Browser acceptance', 'background': 'white'}, page)
        (page or self.page).wait_for_timeout(200)

    def stroke(self, page=None):
        self.command('ui.pointer', {'events': [{'kind': 'down', 'x': 30, 'y': 30},
                     {'kind': 'move', 'x': 180, 'y': 150}, {'kind': 'up', 'x': 180, 'y': 150}]}, page)

    def download(self, command):
        with self.page.expect_download() as event:
            self.command('ui.menu.invoke', {'id': command})
        path = ARTIFACTS / event.value.suggested_filename
        event.value.save_as(path)
        self.assertIsNone(event.value.failure())
        return path

    def open_file(self, path):
        with self.page.expect_file_chooser() as event:
            self.command('ui.menu.invoke', {'id': 'file.open'})
        event.value.set_files(str(path))
        self.page.wait_for_timeout(600)

    def signed_in(self):
        ident, token = str(uuid.uuid4()), secrets.token_hex(32)
        self.accounts.append(ident)
        self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)', (ident, ident+'@example.invalid', 'Browser tester'))
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)', (hashlib.sha256(token.encode()).hexdigest(), ident))
        self.context.add_cookies([{'name': 'pc_session', 'value': token, 'url': BASE, 'httpOnly': True, 'sameSite': 'Lax'}])
        self.load(self.page)
        return token

    def projects(self):
        response = self.context.request.get(BASE+'/api/projects')
        self.assertTrue(response.ok)
        return response.json()

    def wait_revision(self, revision):
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            projects = self.projects()
            if projects and projects[0]['revision'] >= revision:
                return projects[0]
            self.page.wait_for_timeout(250)
        self.fail(f'Cloud revision {revision} never committed')

    def test_01_workspace_new_canvas_button(self):
        self.assertEqual(self.page.title(), 'PhotoCraft Studio')
        self.page.mouse.click(340, 375)
        self.page.wait_for_timeout(300)
        doc = self.inspect()['document']
        self.assertEqual((doc['width'], doc['height']), (1200, 900))
        self.assertEqual(len(doc['layers']), 1)

    def test_02_brush_undo_redo(self):
        self.new()
        self.stroke()
        self.assertIn('Brush Tool', self.inspect()['document']['history'])
        self.execute('edit.undo')
        self.assertTrue(self.inspect()['document']['canRedo'])
        self.execute('edit.redo')
        self.assertTrue(self.inspect()['document']['canUndo'])

    def test_03_native_file_roundtrip_layers_and_text(self):
        self.new()
        self.stroke()
        self.execute('shape.create', {'kind': 'ellipse', 'rect': [60, 40, 100, 90], 'fill': '#9278ff', 'name': 'Violet circle'})
        self.execute('type.create', {'x': 25, 'y': 210, 'text': 'Made in PhotoCraft', 'size': 18, 'color': '#221144'})
        before = self.inspect()['document']
        self.page.mouse.click(1130,28)
        self.page.wait_for_timeout(200)
        with self.page.expect_download() as event:
            self.page.mouse.click(1170,88)
        path = ARTIFACTS / 'native-roundtrip.pcraft'
        event.value.save_as(path)
        self.assertTrue(zipfile.is_zipfile(path))
        self.open_file(path)
        after = self.inspect()['document']
        self.assertEqual([(v['kind'], v['name']) for v in before['layers']], [(v['kind'], v['name']) for v in after['layers']])
        self.assertEqual((after['width'], after['height']), (320, 240))

    def test_04_png_export_contains_actual_pixels(self):
        self.new()
        self.execute('edit.fill', {'color': '#9278ff'})
        path = self.download('file.export.quickExportAsPng')
        with Image.open(path) as img:
            self.assertEqual(img.size, (320, 240))
            r,g,b = img.convert('RGB').getpixel((100,100))
            self.assertLess(abs(r-146)+abs(g-120)+abs(b-255), 8)
        self.open_file(path)
        self.assertEqual(self.inspect()['document']['width'], 320)

    def test_05_psd_export_and_reimport(self):
        self.new()
        self.execute('shape.create', {'kind': 'rect', 'rect': [40,40,100,80], 'fill': '#9278ff', 'name': 'Card'})
        self.page.mouse.click(1130,28)
        self.page.wait_for_timeout(200)
        with self.page.expect_download() as event:
            self.page.mouse.click(1170,148)
        path = ARTIFACTS / 'layered-roundtrip.psd'
        event.value.save_as(path)
        self.assertEqual(path.read_bytes()[:4], b'8BPS')
        self.open_file(path)
        self.assertEqual(len(self.inspect()['document']['layers']), 2)

    def test_06_browser_recovery_survives_reload_and_network_failure(self):
        self.new()
        self.stroke()
        self.context.set_offline(True)
        self.page.wait_for_timeout(2500)
        drafts = self.page.evaluate('''async () => await new Promise((resolve,reject) => {
          const request=indexedDB.open('photocraft-studio-recovery');
          request.onsuccess=()=>{const db=request.result; const read=db.transaction('drafts').objectStore('drafts').getAll();
            read.onsuccess=()=>{resolve(read.result);db.close()};read.onerror=()=>reject(read.error)};
          request.onerror=()=>reject(request.error);
        })''')
        self.assertEqual(len(drafts), 1)
        self.context.set_offline(False)
        self.load(self.page)
        self.page.mouse.click(100,254)
        self.page.wait_for_timeout(300)
        # Recovery lives below the empty workspace card, and is also persisted independently.
        self.assertIsNone(self.inspect()['document'])
        self.assertTrue(drafts[0])
        self.page.screenshot(path=str(ARTIFACTS/"recovery-controls.png"))
        self.page.mouse.click(410,330)
        self.page.wait_for_timeout(500)
        self.assertEqual(self.inspect()['document']['width'], 320)

    def test_07_cloud_save_autosave_reload(self):
        self.signed_in()
        self.new()
        self.stroke()
        self.page.mouse.click(1295,28)
        first = self.wait_revision(1)
        self.execute('layer.duplicate')
        self.wait_revision(2)
        self.load(self.page)
        self.page.mouse.click(100,254)
        self.page.wait_for_timeout(150)
        self.page.mouse.click(435,220)
        self.page.wait_for_timeout(700)
        doc = self.inspect()['document']
        self.assertIsNotNone(doc)
        self.assertEqual(len(doc['layers']), 2)
        versions = self.context.request.get(BASE+f"/api/projects/{first['id']}/versions").json()
        self.assertGreaterEqual(len(versions), 2)

    def test_08_public_view_opens_in_separate_browser(self):
        self.signed_in()
        self.new()
        self.page.mouse.click(1295,28)
        project = self.wait_revision(1)
        response = self.context.request.post(BASE+f"/api/projects/{project['id']}/share", headers={'Origin':BASE})
        self.assertTrue(response.ok)
        key = response.json()['url'].split('share=')[1]
        _, visitor = self.context_page('?share='+key)
        visitor.wait_for_timeout(500)
        self.assertEqual(self.inspect(visitor)['document']['width'], 320)
        self.assertEqual(self.projects()[0]['revision'], 1)

    def test_09_renderer_fallbacks_edit_and_undo(self):
        observations = []
        for query, expected in [('', None), ('?webgl', 'gl'), ('?cpu', None)]:
            with self.subTest(query=query):
                _, page = self.context_page(query)
                self.new(page=page)
                self.stroke(page)
                state = self.inspect(page)
                gpu = state['perf']['timings']['gpuInfo']
                observations.append({'query':query, 'gpu':gpu})
                if expected:
                    self.assertIn(gpu['backend'].lower(), ['gl','webgl','webgl2'])
                if query=='?cpu':
                    self.assertFalse(state['perf']['timings']['gpu'])
                    self.assertIn(gpu['canvas'], ['', 'cpu'])
                self.assertTrue(state['document']['canUndo'])
                self.execute('edit.undo', page=page)
        (ARTIFACTS/'renderers.json').write_text(json.dumps(observations, indent=2))

    def test_10_narrow_and_tablet_layouts(self):
        for width,height in [(390,844), (768,1024), (844,390)]:
            with self.subTest(width=width):
                _, page = self.context_page(viewport={'width':width,'height':height})
                page.screenshot(path=str(ARTIFACTS/f'home-{width}x{height}.png'))
                self.new(page=page)
                self.stroke(page)
                self.assertEqual(self.inspect(page)['window']['width'], width)
                page.screenshot(path=str(ARTIFACTS/f'editor-{width}x{height}.png'))
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))

    def test_11_malformed_open_preserves_existing_document(self):
        self.new()
        bad = ARTIFACTS/'corrupt.pcraft'
        bad.write_bytes(b'PK\x03\x04corrupt synthetic document')
        self.open_file(bad)
        self.assertEqual(self.inspect()['document']['width'], 320)
        self.stroke()
        self.assertTrue(self.inspect()['document']['canUndo'])

    def test_12_webkit_browser_engine(self):
        browser = self.pw.webkit.launch(headless=True)
        try:
            context, page = self.context_page('?webgl', browser=browser)
            self.new(page=page)
            self.stroke(page)
            self.assertTrue(self.inspect(page)['document']['canUndo'])
            page.screenshot(path=str(ARTIFACTS/'webkit-editor.png'))
            self.contexts.remove(context)
            context.close()
        finally:
            browser.close()

    def test_13_conflicting_browser_edits_preserve_a_copy(self):
        token = self.signed_in()
        self.new()
        self.stroke()
        self.page.mouse.click(1295,28)
        self.wait_revision(1)
        _, second = self.context_page(token=token)
        second.mouse.click(100,254)
        second.wait_for_timeout(150)
        second.mouse.click(435,220)
        second.wait_for_timeout(600)
        self.assertIsNotNone(self.inspect(second)['document'])
        self.stroke()
        self.wait_revision(2)
        self.execute('layer.duplicate', page=second)
        second.wait_for_timeout(4500)
        self.assertEqual(len(self.inspect(second)['document']['layers']), 2)
        self.assertEqual(self.projects()[0]['revision'], 2, 'A stale browser overwrote the saved document')
        second.screenshot(path=str(ARTIFACTS/'conflict-preserved.png'))
        second.mouse.click(1220,28)
        second.wait_for_timeout(200)
        second.mouse.click(1260,58)
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            projects=self.projects()
            if len(projects)==2 and all(p['revision']>=1 for p in projects):
                break
            second.wait_for_timeout(300)
        else:
            self.fail('Save a copy did not retain the conflicting browser document')

    def test_14_templates_keep_editable_text_and_shapes(self):
        for slug in ['noise', 'sunday', 'next', 'soul', 'softform', 'afterhours']:
            with self.subTest(template=slug):
                response = self.context.request.get(BASE+f'/templates/{slug}.pcraft')
                self.assertTrue(response.ok)
                path = ARTIFACTS/f'template-{slug}.pcraft'
                path.write_bytes(response.body())
                with zipfile.ZipFile(path) as z:
                    manifest = json.loads(z.read('manifest.json'))['document']
                self.open_file(path)
                doc = self.inspect()['document']
                self.assertEqual((doc['width'], doc['height']), (manifest['size']['width'], manifest['size']['height']))
                self.assertGreater(len(doc['layers']), 4)
                kinds = {layer['content']['kind'] for layer in manifest['layers']}
                self.assertTrue({'raster', 'text', 'shape'} <= kinds, kinds)
                text_layer = next(layer for layer in manifest['layers'] if layer['content']['kind']=='text')
                self.execute('type.edit', {'layer': text_layer['id'], 'text': 'My own design'})
                self.assertTrue(self.inspect()['document']['canUndo'])
                self.execute('edit.undo')


    def test_15_home_template_card_opens_native_document(self):
        self.page.mouse.click(330,710)
        self.page.wait_for_timeout(900)
        doc = self.inspect()['document']
        self.assertIsNotNone(doc)
        self.assertEqual((doc['width'],doc['height']),(1080,1350))
        self.assertGreater(len(doc['layers']), 4)
        self.page.screenshot(path=str(ARTIFACTS/'template-from-gallery.png'))



if __name__ == '__main__':
    unittest.main(verbosity=2)
