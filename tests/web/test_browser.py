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
from visual_assertions import assert_header_geometry, assert_dialog_inside

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

    def context_page(self, query='', viewport=None, token=None, browser=None, device_scale_factor=1):
        ctx = (browser or self.browser).new_context(viewport=viewport or {'width': 1440, 'height': 960}, accept_downloads=True, device_scale_factor=device_scale_factor)
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
        self.page.mouse.click(1340, 116)
        self.page.wait_for_timeout(200)
        dialog = self.inspect()['dialogs'][0]
        self.assertEqual(dialog['kind'], 'NewDocument')
        self.page.screenshot(path=str(ARTIFACTS/'native-new-document.png'))
        for field, value in [('width', 1200), ('height', 900)]:
            self.command('ui.dialog.set', {'dialog': dialog['id'], 'field': field, 'value': value})
        self.command('ui.dialog.confirm', {'dialog': dialog['id']})
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
        self.page.mouse.click(1238,32)
        self.page.wait_for_timeout(200)
        with self.page.expect_download() as event:
            self.page.mouse.click(1270,94)
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
        self.page.mouse.click(1238,32)
        self.page.wait_for_timeout(200)
        with self.page.expect_download() as event:
            self.page.mouse.click(1270,154)
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
        self.page.mouse.click(100,276)
        self.page.wait_for_timeout(300)
        # Recovery lives below the empty workspace card, and is also persisted independently.
        self.assertIsNone(self.inspect()['document'])
        self.assertTrue(drafts[0])
        self.page.screenshot(path=str(ARTIFACTS/"recovery-controls.png"))
        self.page.mouse.click(430,462)
        self.page.wait_for_timeout(500)
        self.assertEqual(self.inspect()['document']['width'], 320)

    def test_07_cloud_save_autosave_reload(self):
        self.signed_in()
        self.new()
        self.stroke()
        self.page.mouse.click(1320,32)
        first = self.wait_revision(1)
        self.execute('layer.duplicate')
        self.wait_revision(2)
        self.load(self.page, '?project='+first['id'])
        self.page.wait_for_timeout(700)
        doc = self.inspect()['document']
        self.assertIsNotNone(doc)
        self.assertEqual(len(doc['layers']), 2)
        versions = self.context.request.get(BASE+f"/api/projects/{first['id']}/versions").json()
        self.assertGreaterEqual(len(versions), 2)

    def test_08_public_view_opens_in_separate_browser(self):
        self.signed_in()
        self.new()
        self.page.mouse.click(1320,32)
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
        self.page.mouse.click(1320,32)
        project = self.wait_revision(1)
        other_context, second = self.context_page('?project='+project['id'], token=token)
        self.assertIsNotNone(self.inspect(second)['document'])
        # Both editors modify the same pixels from the same saved revision.
        other_context.set_offline(True)
        self.execute('edit.fill', {'color':'#0000ff'}, page=second)
        self.execute('edit.fill', {'color':'#ff0000'})
        self.wait_revision(2)
        other_context.set_offline(False)
        second.mouse.click(1258,32)
        second.wait_for_timeout(2000)
        self.assertTrue(self.inspect(second)['document']['canUndo'])
        self.assertEqual(self.projects()[0]['revision'], 2, 'A stale browser overwrote the saved document')
        second.screenshot(path=str(ARTIFACTS/'conflict-preserved.png'))
        second.mouse.click(1170,32)
        second.wait_for_timeout(200)
        second.mouse.click(1190,64)
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
        self.page.mouse.click(100,218)
        self.page.wait_for_timeout(200)
        self.page.mouse.click(330,410)
        self.page.wait_for_timeout(900)
        doc = self.inspect()['document']
        self.assertIsNotNone(doc)
        self.assertEqual((doc['width'],doc['height']),(1080,1350))
        self.assertGreater(len(doc['layers']), 4)
        self.page.screenshot(path=str(ARTIFACTS/'template-from-gallery.png'))


    def test_16_two_accounts_merge_and_receive_saved_changes(self):
        self.signed_in()
        self.new()
        self.execute('edit.fill', {'color':'#9278ff'})
        self.execute('shape.create', {'kind':'rect','rect':[40,40,100,80],'fill':'#ffaa00','name':'Card'})
        self.page.mouse.click(1320,32)
        project=self.wait_revision(1)
        ident, token=str(uuid.uuid4()), secrets.token_hex(32)
        self.accounts.append(ident)
        email=ident+'@example.invalid'
        self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)', (ident,email,'Second collaborator'))
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)', (hashlib.sha256(token.encode()).hexdigest(),ident))
        response=self.context.request.put(BASE+f"/api/projects/{project['id']}/members",headers={'Origin':BASE},data={'email':email,'role':'edit'})
        self.assertTrue(response.ok)
        other_context,second=self.context_page('?project='+project['id'],token=token)
        layers=self.inspect(second)['document']['layers']
        self.assertEqual(len(layers),2)
        other_context.set_offline(True)
        self.execute('layer.renameLayer',{'layer':layers[0]['id'],'name':'Collaborator background'},page=second)
        self.execute('layer.renameLayer',{'layer':layers[1]['id'],'name':'Owner card'})
        self.wait_revision(2)
        other_context.set_offline(False)
        second.mouse.click(1354,32)
        self.wait_revision(3)
        expected={'Collaborator background','Owner card'}
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            if all({l['name'] for l in self.inspect(p)['document']['layers']}==expected for p in [self.page,second]):
                break
            self.page.wait_for_timeout(300)
        else:
            self.fail('Independent signed-in collaborators did not converge after the merge')
        path=self.download('file.export.quickExportAsPng')
        with Image.open(path) as img:
            self.assertEqual(img.size,(320,240))
            self.assertEqual(img.convert('RGB').getpixel((80,70)),(255,170,0))
        response=self.context.request.put(BASE+f"/api/projects/{project['id']}/members",headers={'Origin':BASE},data={'email':email,'role':'view'})
        self.assertTrue(response.ok)
        second.wait_for_timeout(2000)
        second.screenshot(path=str(ARTIFACTS/'collaborator-view-role.png'))
        self.load(second,'?project='+project['id'])
        self.assertEqual({l['name'] for l in self.inspect(second)['document']['layers']},expected)


    def test_17_avatar_sign_in_preserves_guest_work(self):
        self.context.route('**/api/config',lambda route: route.fulfill(json={'cloud':True,'signIn':True,'version':'test'}))
        self.context.route('**/auth/login',lambda route: route.fulfill(content_type='text/html',body='<h1>Sign-in handoff</h1>'))
        self.load(self.page)
        self.new()
        self.stroke()
        self.page.mouse.click(1400,32)
        self.page.wait_for_timeout(200)
        self.page.screenshot(path=str(ARTIFACTS/'account-sign-in-menu.png'))
        self.page.mouse.click(1270,170)
        self.page.wait_for_url('**/auth/login',timeout=10000)
        self.assertEqual(self.page.locator('h1').inner_text(),'Sign-in handoff')
        self.signed_in()
        self.page.mouse.click(100,276)
        self.page.wait_for_timeout(250)
        self.page.screenshot(path=str(ARTIFACTS/'guest-work-after-sign-in.png'))
        self.page.mouse.click(430,462)
        self.page.wait_for_timeout(500)
        self.assertEqual(self.inspect()['document']['width'],320)


    def test_18_delayed_cloud_restores_existing_session(self):
        self.signed_in()
        phase={'ready':False}
        self.context.route('**/api/config',lambda route: route.fulfill(json={'cloud':phase['ready'],'signIn':phase['ready'],'version':'test'}))
        self.context.route('**/api/me',lambda route: route.continue_() if phase['ready'] else route.fulfill(status=503,json={'error':'Cloud starting'}))
        self.load(self.page)
        self.new()
        self.stroke()
        phase['ready']=True
        self.page.wait_for_timeout(2500)
        self.page.mouse.click(1320,32)
        self.wait_revision(1)
        self.assertEqual(self.inspect()['document']['width'],320)
        self.page.screenshot(path=str(ARTIFACTS/'startup-session-recovered.png'))


    def test_19_project_search_clear_and_responsive_cards(self):
        self.signed_in()
        self.new(640, 480)
        self.page.mouse.click(1320,32)
        project = self.wait_revision(1)
        response = self.context.request.patch(BASE+'/api/projects/'+project['id'],
            headers={'Origin':BASE},
            data={'title': 'Summer café — an intentionally long project title that must not displace its menu', 'folder':'Brand'})
        self.assertTrue(response.ok, response.text())
        self.page.mouse.click(80,32)
        self.page.mouse.click(100,276)
        self.page.wait_for_timeout(300)
        self.page.screenshot(path=str(ARTIFACTS/'project-long-title.png'))
        self.page.mouse.click(380,180)
        self.page.keyboard.type('no-match-at-all')
        self.page.wait_for_timeout(150)
        self.page.screenshot(path=str(ARTIFACTS/'project-search-empty.png'))
        opened=[]
        self.page.on('request',lambda request: opened.append(request.url) if request.method=='GET' and request.url==BASE+'/api/projects/'+project['id'] else None)
        self.page.mouse.click(350,320)
        self.page.wait_for_timeout(150)
        self.assertEqual(opened, [], 'Empty search must not leave a stale clickable project')
        self.page.mouse.click(700,180)  # Clear, alongside the full-width search field.
        self.page.wait_for_timeout(150)
        self.page.mouse.click(350,320)
        self.page.wait_for_timeout(600)
        self.assertEqual(len(opened),1)
        self.assertEqual(self.inspect()['document']['width'],640)
        self.page.mouse.click(80,32)
        for width,height in [(390,844),(768,1024),(1440,960),(2200,1100)]:
            with self.subTest(width=width):
                self.page.set_viewport_size({'width':width,'height':height})
                self.page.wait_for_timeout(250)
                self.page.screenshot(path=str(ARTIFACTS/f'project-grid-{width}.png'))
                self.assertEqual(self.page.locator('canvas').count(),1)
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'),width)

    def test_20_share_escape_preserves_canvas_and_native_dialog(self):
        self.signed_in()
        self.new(640,480)
        self.execute('edit.fill',{'color':'#ebe4fc'})
        self.page.mouse.click(1320,32)
        self.wait_revision(1)
        self.page.wait_for_timeout(300)
        before = Image.open(io.BytesIO(self.page.screenshot())).convert('RGB').crop((510,300,900,650))
        document = self.inspect()['document']
        self.page.mouse.click(1328,32)
        self.page.wait_for_timeout(200)
        opened = Image.open(io.BytesIO(self.page.screenshot())).convert('RGB').crop((510,300,900,650))
        self.assertNotEqual(before.tobytes(),opened.tobytes())
        self.page.screenshot(path=str(ARTIFACTS/'share-dialog.png'))
        self.page.keyboard.press('Escape')
        self.page.wait_for_timeout(200)
        closed = Image.open(io.BytesIO(self.page.screenshot())).convert('RGB').crop((510,300,900,650))
        self.assertEqual(before.tobytes(),closed.tobytes(), 'Escape must restore the canvas without the sharing overlay')
        self.assertEqual(self.inspect()['document']['layers'],document['layers'])
        self.command('ui.menu.invoke',{'id':'image.imageSize'})
        dialog = self.inspect()['dialogs'][0]
        self.assertEqual(dialog['title'],'Image Size')
        self.page.screenshot(path=str(ARTIFACTS/'native-image-size.png'))
        self.command('ui.dialog.cancel',{'dialog':dialog['id']})
        self.assertEqual(self.inspect()['document']['width'],640)



    def test_21_project_menu_star_trash_and_restore(self):
        self.signed_in()
        self.new(640,480)
        self.page.mouse.click(1320,32)
        project = self.wait_revision(1)
        self.page.mouse.click(80,32)
        self.page.mouse.click(100,276)
        self.page.wait_for_timeout(200)
        for nav, row, field, expected in [(276,613,'starred',True), (330,650,'trashed',True), (444,650,'trashed',False)]:
            with self.subTest(action=(field,expected)):
                self.page.mouse.click(100,nav)
                self.page.wait_for_timeout(150)
                self.page.mouse.click(493,455)
                self.page.wait_for_timeout(150)
                self.page.screenshot(path=str(ARTIFACTS/f'project-menu-{field}-{expected}.png'))
                with self.page.expect_response(lambda r: r.url==BASE+'/api/projects/'+project['id'] and r.request.method=='PATCH') as event:
                    self.page.mouse.click(535,row)
                self.assertTrue(event.value.ok)
                self.page.wait_for_timeout(150)
                state = self.context.request.get(BASE+'/api/projects').json()
                current = next(p for p in state if p['id']==project['id'])
                self.assertEqual(current[field],expected)
        self.page.mouse.click(100,276)
        self.page.wait_for_timeout(150)
        self.page.mouse.click(493,455)
        self.page.wait_for_timeout(150)
        self.page.mouse.click(535,574)
        self.page.wait_for_timeout(150)
        self.page.screenshot(path=str(ARTIFACTS/'project-details.png'))
        self.page.mouse.click(650,467)
        self.page.keyboard.press('ControlOrMeta+A')
        self.page.keyboard.type('Renamed native project')
        with self.page.expect_response(lambda r: r.url==BASE+'/api/projects/'+project['id'] and r.request.method=='PATCH') as event:
            self.page.mouse.click(590,590)
        self.assertTrue(event.value.ok)
        self.assertEqual(self.projects()[0]['title'],'Renamed native project')

    def test_22_sharing_comments_and_history_controls(self):
        self.signed_in()
        self.new(640,480)
        self.page.mouse.click(1320,32)
        project = self.wait_revision(1)
        self.page.mouse.click(1328,32)
        self.page.wait_for_timeout(200)
        self.page.mouse.click(850,406)
        self.page.wait_for_timeout(150)
        self.page.screenshot(path=str(ARTIFACTS/'share-role-dropdown.png'))
        self.page.mouse.click(850,443)  # Can view.
        self.page.wait_for_timeout(150)
        with self.page.expect_response(lambda r: r.url.endswith('/share') and r.request.method=='POST') as event:
            self.page.mouse.click(610,620)
        self.assertTrue(event.value.ok)
        key=event.value.json()['url'].split('share=')[1]
        self.assertTrue(self.context.request.get(BASE+'/api/share/'+key).ok)
        self.page.wait_for_timeout(150)
        self.page.screenshot(path=str(ARTIFACTS/'share-created-link.png'))
        with self.page.expect_response(lambda r: r.url.endswith('/share') and r.request.method=='DELETE') as event:
            self.page.mouse.click(610,708)
        self.assertTrue(event.value.ok)
        self.assertEqual(self.context.request.get(BASE+'/api/share/'+key).status,404)
        self.page.keyboard.press('Escape')
        self.page.wait_for_timeout(150)
        self.page.mouse.click(1214,32)
        self.page.wait_for_timeout(200)
        self.page.mouse.click(625,370)
        self.page.keyboard.type('Keep the native PhotoCraft controls.')
        with self.page.expect_response(lambda r: r.url.endswith('/comments') and r.request.method=='POST') as event:
            self.page.mouse.click(580,433)
        self.assertTrue(event.value.ok)
        self.page.wait_for_timeout(200)
        comments=self.context.request.get(BASE+'/api/projects/'+project['id']+'/comments').json()
        self.assertEqual(comments[0]['body'],'Keep the native PhotoCraft controls.')
        self.page.screenshot(path=str(ARTIFACTS/'comments-posted.png'))
        self.page.keyboard.press('Escape')
        self.page.wait_for_timeout(150)
        self.page.mouse.click(1170,32)
        self.page.wait_for_timeout(150)
        with self.page.expect_response(lambda r: r.url.endswith('/versions')) as event:
            self.page.mouse.click(1200,183)
        self.assertTrue(event.value.ok)
        self.assertEqual(len(event.value.json()),1)
        self.page.wait_for_timeout(150)
        self.page.screenshot(path=str(ARTIFACTS/'version-history.png'))
        self.page.keyboard.press('Escape')
        self.assertEqual(self.inspect()['document']['width'],640)



    def test_23_header_control_geometry(self):
        self.signed_in()
        self.new(640,480)
        self.page.mouse.click(1320,32)
        self.wait_revision(1)
        observations=[]
        for theme in ['studio','studioLight','pro','proMedium','classic']:
            self.command('ui.set',{'theme':theme})
            for width in [1440,768,390]:
                with self.subTest(theme=theme,width=width):
                    self.page.set_viewport_size({'width':width,'height':960})
                    self.page.mouse.move(10,850)
                    self.page.wait_for_timeout(150)
                    picture=self.page.screenshot()
                    (ARTIFACTS/f'header-owner-{theme}-{width}.png').write_bytes(picture)
                    actions=['More','Comments','Save','Share'] if width>=760 else ['More','Save','Share']
                    rects=assert_header_geometry(self,Image.open(io.BytesIO(picture)),actions)
                    observations.append({'theme':theme,'width':width,'rects':rects})
        (ARTIFACTS/'header-geometry.json').write_text(json.dumps(observations,indent=2))
        self.page.set_viewport_size({'width':1440,'height':960})
        self.command('ui.set',{'theme':'studio'})
        self.page.wait_for_timeout(150)
        self.page.screenshot(path=str(ARTIFACTS/'header-controls.png'),clip={'x':990,'y':0,'width':450,'height':64})

    def test_24_guest_header_geometry_with_long_title(self):
        self.execute('file.new',{'width':640,'height':480,'name':'A long document title '*20,'background':'white'})
        for width in [1440,768,390]:
            with self.subTest(width=width):
                self.page.set_viewport_size({'width':width,'height':960})
                self.page.mouse.move(10,850)
                self.page.wait_for_timeout(150)
                picture=self.page.screenshot()
                (ARTIFACTS/f'header-guest-long-title-{width}.png').write_bytes(picture)
                assert_header_geometry(self,Image.open(io.BytesIO(picture)),['More','Save'])

    def test_25_share_with_long_collaborator_addresses_stays_inside_viewport(self):
        self.signed_in()
        self.new(640,480)
        self.page.mouse.click(1320,32)
        project=self.wait_revision(1)
        email='a-very-long-collaborator-address-at-an-organization@example.invalid'
        response=self.context.request.put(BASE+'/api/projects/'+project['id']+'/members',
            headers={'Origin':BASE},data={'email':email,'role':'edit'})
        self.assertTrue(response.ok)
        for width,height in [(1440,960),(768,1024),(390,844),(844,390)]:
            with self.subTest(width=width,height=height):
                self.page.set_viewport_size({'width':width,'height':height})
                self.page.mouse.move(0,0)
                self.page.wait_for_timeout(250)
                before=Image.open(io.BytesIO(self.page.screenshot()))
                self.page.mouse.click(width-112,32)
                self.page.mouse.move(0,0)
                self.page.wait_for_timeout(350)
                picture=self.page.screenshot()
                (ARTIFACTS/f'share-long-member-{width}.png').write_bytes(picture)
                try:
                    assert_dialog_inside(self,before,Image.open(io.BytesIO(picture)))
                finally:
                    self.page.keyboard.press('Escape')
                    self.page.wait_for_timeout(150)

    def test_26_sharing_window_blocks_canvas_painting(self):
        self.signed_in()
        self.new(640,480)
        self.page.mouse.click(1320,32)
        self.wait_revision(1)
        self.page.mouse.click(1328,32)
        self.page.wait_for_timeout(300)
        before=self.inspect()['document']['history']
        self.page.mouse.move(200,400)
        self.page.mouse.down()
        self.page.mouse.move(300,450,steps=8)
        self.page.mouse.up()
        self.page.wait_for_timeout(200)
        self.assertEqual(self.inspect()['document']['history'],before,'Sharing must block accidental painting behind the window')
        self.page.keyboard.press('Escape')
        self.page.wait_for_timeout(200)
        self.page.mouse.move(200,400)
        self.page.mouse.down()
        self.page.mouse.move(300,450,steps=8)
        self.page.mouse.up()
        self.page.wait_for_timeout(200)
        self.assertNotEqual(self.inspect()['document']['history'],before,'Closing sharing must restore native canvas input')

    def test_27_native_dialogs_fit_and_cancel_without_changing_document(self):
        self.new(640,480)
        commands=['file.new','image.imageSize','image.canvasSize','file.export.exportAs',
                  'edit.fill','image.adjustments.levels','image.adjustments.curves',
                  'filter.blur.gaussianBlur','select.colorRange','edit.preferences.general','help.about']
        for width,height in [(1440,960),(768,1024),(390,844)]:
            self.page.set_viewport_size({'width':width,'height':height})
            for command in commands:
                with self.subTest(width=width,command=command):
                    self.page.mouse.move(0,0)
                    self.page.wait_for_timeout(150)
                    document=self.inspect()['document']
                    before=Image.open(io.BytesIO(self.page.screenshot()))
                    self.command('ui.menu.invoke',{'id':command})
                    self.page.wait_for_timeout(250)
                    dialogs=self.inspect()['dialogs']
                    self.assertEqual(len(dialogs),1,command)
                    picture=self.page.screenshot()
                    (ARTIFACTS/f'native-dialog-{command}-{width}.png').write_bytes(picture)
                    try:
                        assert_dialog_inside(self,before,Image.open(io.BytesIO(picture)))
                    finally:
                        self.command('ui.dialog.cancel',{'dialog':dialogs[0]['id']})
                        self.page.wait_for_timeout(100)
                    self.assertEqual(self.inspect()['document']['layers'],document['layers'])
                    self.assertEqual(self.inspect()['document']['history'],document['history'])

    def test_28_collaborator_permission_menu_and_escape(self):
        self.signed_in()
        self.new(640,480)
        self.page.mouse.click(1320,32)
        project=self.wait_revision(1)
        path=BASE+'/api/projects/'+project['id']+'/members'
        self.assertTrue(self.context.request.put(path,headers={'Origin':BASE},
            data={'email':'collaborator@example.invalid','role':'edit'}).ok)
        self.page.wait_for_timeout(350)
        self.page.mouse.click(1328,32)
        self.page.wait_for_timeout(300)
        self.page.mouse.click(850,470)
        self.page.wait_for_timeout(200)
        self.page.screenshot(path=str(ARTIFACTS/'member-access-menu.png'))
        self.page.keyboard.press('Escape')
        self.page.wait_for_timeout(150)
        self.page.mouse.click(850,470)  # Escape closes the dropdown, leaving sharing open.
        self.page.wait_for_timeout(200)
        with self.page.expect_response(lambda r:r.url==path and r.request.method=='PUT') as event:
            self.page.mouse.click(850,507)
        self.assertTrue(event.value.ok)
        self.page.wait_for_timeout(250)
        self.assertEqual(self.context.request.get(path).json()[0]['role'],'view')
        self.page.screenshot(path=str(ARTIFACTS/'member-now-viewer.png'))

    def test_29_canvas_resolution_tracks_display_density_and_resize(self):
        observations=[]
        for density in [1,1.25,1.5,2]:
            with self.subTest(density=density):
                # Chromium emulation changes devicePixelRatio but not ResizeObserver's physical
                # devicePixelContentBoxSize. Launch at the real scale as well; eframe uses
                # that physical box for sharp canvas sizing.
                browser=self.pw.chromium.launch(executable_path=os.environ.get('PHOTOCRAFT_CHROME'),
                    headless=True,args=['--enable-unsafe-webgpu','--enable-unsafe-swiftshader',
                                       '--force-device-scale-factor='+str(density)])
                self.addCleanup(browser.close)
                _,page=self.context_page(browser=browser,device_scale_factor=density)
                self.new(page=page)
                for width,height in [(1440,960),(831,900)]:
                    page.set_viewport_size({'width':width,'height':height})
                    page.wait_for_timeout(250)
                    metrics=page.evaluate('''() => { const c=document.querySelector('canvas'),r=c.getBoundingClientRect();
                        return {dpr:devicePixelRatio,width:c.width,height:c.height,cssWidth:r.width,cssHeight:r.height}; }''')
                    self.assertAlmostEqual(metrics['width'],metrics['cssWidth']*density,delta=1)
                    self.assertAlmostEqual(metrics['height'],metrics['cssHeight']*density,delta=1)
                    picture=page.screenshot(scale='css')
                    (ARTIFACTS/f'density-{density}-{width}.png').write_bytes(picture)
                    assert_header_geometry(self,Image.open(io.BytesIO(picture)),['More','Save'])
                    observations.append(metrics)
        (ARTIFACTS/'display-density.json').write_text(json.dumps(observations,indent=2))

    def test_30_rendering_preference_survives_browser_restart(self):
        self.new()
        for mode,accelerated in [('cpu',False),('auto',True)]:
            with self.subTest(mode=mode):
                self.execute('prefs.set',{'path':'performance.renderingMode','value':mode})
                self.page.wait_for_function('(mode)=>JSON.parse(localStorage.getItem("photocraft.preferences")||"{}").performance?.renderingMode===mode',arg=mode)
                self.load(self.page)
                self.new()
                self.stroke()
                state=self.inspect()
                self.assertEqual(state['perf']['timings']['gpu'],accelerated)
                self.assertTrue(state['document']['canUndo'])
                self.execute('edit.undo')

    def test_31_phone_creation_uses_visible_native_field_and_footer(self):
        self.page.set_viewport_size({'width':390,'height':844})
        self.page.wait_for_timeout(250)
        self.page.mouse.click(295,273)  # Workspace Create a design.
        self.page.wait_for_timeout(400)
        self.assertEqual(self.inspect()['dialogs'][0]['kind'],'NewDocument')
        self.page.screenshot(path=str(ARTIFACTS/'phone-new-document-fields.png'))
        self.page.mouse.dblclick(78,541)  # Native Width field, reached without protocol setters.
        self.page.keyboard.press('ControlOrMeta+A')
        self.page.keyboard.type('256')
        self.page.keyboard.press('Tab')
        self.page.wait_for_timeout(150)
        self.page.mouse.click(323,794)  # Native Create footer, above the hosting credit.
        self.page.wait_for_timeout(300)
        state=self.inspect()
        self.assertEqual(state['dialogs'],[])
        self.assertEqual((state['document']['width'],state['document']['height']),(256,1080))
        self.page.screenshot(path=str(ARTIFACTS/'phone-native-created.png'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
