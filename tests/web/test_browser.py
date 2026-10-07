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
from visual_assertions import (assert_header_geometry, assert_dialog_inside, assert_workspace_geometry,
                               workspace_controls, workspace_quick_actions, workspace_focus_changed,
                               workspace_template_previews, workspace_project_card, workspace_recovery_controls,
                               sharing_invitation_controls, assert_invitation_feedback_geometry, native_overlay_actions,
                               recovery_warning_action)

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
        try:
            self.load(page, query)
        except BaseException:
            # unittest does not call tearDown when setUp fails. Do not leave a
            # live WASM app polling while subsequent tests try to diagnose it.
            ctx.close()
            self.contexts.remove(ctx)
            raise
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

    def click_workspace_action(self, action, page=None):
        page = page or self.page
        controls = workspace_controls(Image.open(io.BytesIO(page.screenshot(scale='css'))))
        rect = controls['actions'][['Open file', 'Create a design'].index(action)]
        page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)

    def return_to_workspace(self):
        # The brand button toggles the workspace, so do not click it when a
        # cancelled file picker already left the workspace visible.
        try:
            workspace_controls(Image.open(io.BytesIO(self.page.screenshot(scale='css'))))
        except AssertionError:
            self.page.mouse.click(80, 32)
            self.page.wait_for_timeout(250)
            workspace_controls(Image.open(io.BytesIO(self.page.screenshot(scale='css'))))

    def click_browser_recovery(self):
        controls = workspace_recovery_controls(Image.open(io.BytesIO(self.page.screenshot(scale='css'))))
        self.assertTrue(controls, 'No visible browser recovery control')
        x0, y0, x1, y1 = controls[0]
        self.page.mouse.click((x0+x1)/2, (y0+y1)/2)

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
        self.click_workspace_action('Create a design')
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
        self.page.mouse.click(100,249)
        self.page.wait_for_timeout(300)
        # Recovery lives below the empty workspace card, and is also persisted independently.
        self.assertIsNone(self.inspect()['document'])
        self.assertTrue(drafts[0])
        self.page.screenshot(path=str(ARTIFACTS/"recovery-controls.png"))
        self.click_browser_recovery()
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

            def recovery():
                return page.evaluate('''async () => await new Promise((resolve,reject) => {
                    const request=indexedDB.open('photocraft-studio-recovery');
                    request.onerror=()=>reject(request.error);request.onsuccess=()=>{
                        const db=request.result;const rows=[];
                        const cursor=db.transaction('drafts').objectStore('drafts').openCursor();
                        cursor.onerror=()=>{db.close();reject(cursor.error)};
                        cursor.onsuccess=()=>{const row=cursor.result;
                            if(row){rows.push({key:row.key,data:Array.from(row.value.data)});row.continue()}
                            else{db.close();resolve(rows)}};
                    };
                })''')

            def wait_recovery(width, height):
                deadline = time.monotonic()+8
                while time.monotonic() < deadline:
                    rows = recovery()
                    if len(rows) == 1:
                        with zipfile.ZipFile(io.BytesIO(bytes(rows[0]['data']))) as archive:
                            document = json.loads(archive.read('manifest.json'))['document']
                        if document['size'] == {'width': width, 'height': height}:
                            self.assertTrue(rows[0]['key'].startswith('guest:'))
                            return rows[0]['key']
                    page.wait_for_timeout(100)
                self.fail('WebKit recovery did not retain exactly the most recently visited native document')

            first_key = wait_recovery(320, 240)
            self.new(486, 326, page=page)
            second_key = wait_recovery(486, 326)
            self.assertNotEqual(second_key, first_key)
            self.load(page, '?webgl')
            self.assertEqual(wait_recovery(486, 326), second_key)
            page.mouse.click(100, 249)
            page.wait_for_timeout(250)
            controls = workspace_recovery_controls(Image.open(io.BytesIO(page.screenshot(scale='css'))))
            self.assertEqual(len(controls), 1)
            rect = controls[0]
            page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            deadline = time.monotonic()+8
            while self.inspect(page)['document'] is None and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            restored = self.inspect(page)['document']
            self.assertIsNotNone(restored)
            self.assertEqual((restored['width'], restored['height']), (486, 326))
            page.screenshot(path=str(ARTIFACTS/'webkit-one-recovery-restored.png'))
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
        more = assert_header_geometry(self, Image.open(io.BytesIO(second.screenshot())),
                                      ['More', 'Comments', 'Retry save', 'Share'])[0]
        second.mouse.click((more[0]+more[2])/2, (more[1]+more[3])/2)
        second.wait_for_timeout(200)
        second.mouse.click(more[0]+30, 64)
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
        self.page.mouse.click(100,249)
        self.page.wait_for_timeout(250)
        self.page.screenshot(path=str(ARTIFACTS/'guest-work-after-sign-in.png'))
        self.click_browser_recovery()
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
        self.page.mouse.click(100,249)
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
        self.page.mouse.click(100,249)
        self.page.wait_for_timeout(200)
        for nav, row, field, expected in [(249,185,'starred',True), (299,223,'trashed',True), (399,223,'trashed',False)]:
            with self.subTest(action=(field,expected)):
                self.page.mouse.click(100,nav)
                self.page.wait_for_timeout(150)
                card = workspace_project_card(Image.open(io.BytesIO(self.page.screenshot())))
                self.page.mouse.click(card['card'][2]-32, card['preview'][3]+28)
                self.page.wait_for_timeout(150)
                self.page.screenshot(path=str(ARTIFACTS/f'project-menu-{field}-{expected}.png'))
                with self.page.expect_response(lambda r: r.url==BASE+'/api/projects/'+project['id'] and r.request.method=='PATCH') as event:
                    self.page.mouse.click(card['card'][2]+5, card['preview'][3]+row)
                self.assertTrue(event.value.ok)
                self.page.wait_for_timeout(150)
                state = self.context.request.get(BASE+'/api/projects').json()
                current = next(p for p in state if p['id']==project['id'])
                self.assertEqual(current[field],expected)
        self.page.mouse.click(100,249)
        self.page.wait_for_timeout(150)
        card = workspace_project_card(Image.open(io.BytesIO(self.page.screenshot())))
        self.page.mouse.click(card['card'][2]-32, card['preview'][3]+28)
        self.page.wait_for_timeout(150)
        self.page.mouse.click(card['card'][2]+5, card['preview'][3]+147)
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
        self.click_workspace_action('Create a design')
        self.page.wait_for_timeout(400)
        self.assertEqual(self.inspect()['dialogs'][0]['kind'],'NewDocument')
        self.page.screenshot(path=str(ARTIFACTS/'phone-new-document-fields.png'))
        self.page.mouse.dblclick(78,541)  # Native Width field, reached without protocol setters.
        self.page.wait_for_timeout(150)  # Let egui replace the drag control with its text editor.
        self.page.keyboard.press('ControlOrMeta+A')
        self.page.keyboard.type('256',delay=50)
        self.page.keyboard.press('Tab')
        self.page.wait_for_timeout(150)
        self.assertEqual(self.inspect()['dialogs'][0]['fields']['width'],256)
        self.page.mouse.click(323,794)  # Native Create footer, above the hosting credit.
        self.page.wait_for_timeout(300)
        state=self.inspect()
        self.assertEqual(state['dialogs'],[])
        self.assertEqual((state['document']['width'],state['document']['height']),(256,1080))
        self.page.screenshot(path=str(ARTIFACTS/'phone-native-created.png'))

    def test_32_workspace_action_spacing_and_corner_geometry(self):
        observations = []
        for density in [1, 2]:
            browser = self.browser
            if density != 1:
                browser = self.pw.chromium.launch(executable_path=os.environ.get('PHOTOCRAFT_CHROME'),
                    headless=True, args=['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader',
                                       '--force-device-scale-factor='+str(density)])
                self.addCleanup(browser.close)
            _, page = self.context_page(browser=browser, device_scale_factor=density)
            for width, height in [(1440, 960), (831, 900), (390, 844)]:
                page.set_viewport_size({'width': width, 'height': height})
                page.mouse.click(0, 70)
                page.mouse.move(0, 0)
                page.wait_for_timeout(250)
                metrics = page.evaluate('''() => { const c=document.querySelector('canvas');
                    return {width:c.width, height:c.height}; }''')
                self.assertAlmostEqual(metrics['width'], width*density, delta=1)
                self.assertAlmostEqual(metrics['height'], height*density, delta=1)
                neutral = Image.open(io.BytesIO(page.screenshot(scale='css')))
                baseline = assert_workspace_geometry(self, neutral)
                for state in ['idle', 'open-hover', 'open-pressed', 'create-hover', 'create-pressed',
                              'open-focus', 'create-focus']:
                    with self.subTest(density=density, width=width, state=state):
                        controls = workspace_controls(Image.open(io.BytesIO(page.screenshot(scale='css'))))
                        action = 0 if state.startswith('open') else 1
                        if state != 'idle':
                            rect = controls['actions'][action]
                            if state.endswith('focus'):
                                search = controls['search']
                                page.mouse.click(search[0]+30, (search[1]+search[3])/2)
                                page.mouse.move(0, 0)
                                for _ in range(3):
                                    page.keyboard.press('Shift+Tab')
                                    page.wait_for_timeout(100)
                                    focused = Image.open(io.BytesIO(page.screenshot(scale='css')))
                                    if workspace_focus_changed(neutral, focused, rect):
                                        break
                                self.assertTrue(workspace_focus_changed(neutral, focused, rect),
                                                f'{state}: no visible keyboard focus indicator')
                            else:
                                page.mouse.move((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
                        if state.endswith('pressed'):
                            page.mouse.down()
                        try:
                            page.wait_for_timeout(250)
                            picture = page.screenshot(scale='css')
                            (ARTIFACTS/f'workspace-{density}x-{width}-{state}.png').write_bytes(picture)
                            measured = assert_workspace_geometry(self, Image.open(io.BytesIO(picture)))
                            self.assertEqual(measured['actions'], baseline['actions'],
                                             f'{state} moves or resizes the controls')
                            if state.endswith('hover'):
                                self.assertNotEqual(measured['buttons'][action]['fill'],
                                                    baseline['buttons'][action]['fill'],
                                                    f'{state} has no visible hover feedback')
                            observations.append({'density': density, 'width': width, 'state': state, **measured})
                        finally:
                            # Release outside to inspect pressed styling without opening a file
                            # picker or creating a document. Each state starts from the same page.
                            page.mouse.move(0, 0)
                            if state.endswith('pressed'):
                                page.mouse.up()
                            if state.endswith('focus'):
                                page.mouse.click(0, 70)
                            page.wait_for_timeout(250)
                        self.assertEqual(self.inspect(page)['dialogs'], [])
                        self.assertIsNone(self.inspect(page)['document'])
            with page.expect_file_chooser() as chooser:
                self.click_workspace_action('Open file', page)
            chooser.value.set_files([])
            self.assertIsNone(self.inspect(page)['document'])
            page.emulate_media(reduced_motion='reduce')
            self.load(page)
            self.assertTrue(page.evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"))
            page.mouse.move(0, 0)
            page.wait_for_timeout(250)
            baseline = assert_workspace_geometry(self, Image.open(io.BytesIO(page.screenshot(scale='css'))))
            for action, name in enumerate(['open', 'create']):
                rect = baseline['actions'][action]
                page.mouse.move((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
                page.wait_for_timeout(250)
                picture = page.screenshot(scale='css')
                (ARTIFACTS/f'workspace-{density}x-390-{name}-reduced-motion-hover.png').write_bytes(picture)
                hovered = assert_workspace_geometry(self, Image.open(io.BytesIO(picture)))
                self.assertEqual(hovered['actions'], baseline['actions'])
                self.assertNotEqual(hovered['buttons'][action]['fill'], baseline['buttons'][action]['fill'])
                page.mouse.move(0, 0)
                page.wait_for_timeout(250)
                restored = assert_workspace_geometry(self, Image.open(io.BytesIO(page.screenshot(scale='css'))))
                self.assertEqual(restored['buttons'][action]['fill'], baseline['buttons'][action]['fill'])
                observations.append({'density': density, 'width': 390, 'state': name+'-reduced-motion-hover', **hovered})
        (ARTIFACTS/'workspace-geometry.json').write_text(json.dumps(observations, indent=2))

    def test_33_workspace_quick_actions_use_native_file_and_creation_flows(self):
        for width in [1440, 831, 620, 564, 390]:
            with self.subTest(quick_action_width=width):
                self.page.set_viewport_size({'width': width, 'height': 844})
                self.page.mouse.move(0, 0)
                self.page.wait_for_timeout(250)
                picture = self.page.screenshot(scale='css')
                (ARTIFACTS/f'quick-action-layout-{width}.png').write_bytes(picture)
                actions = workspace_quick_actions(Image.open(io.BytesIO(picture)))
                for rect in actions:
                    self.assertGreaterEqual(rect[2]-rect[0], 139,
                        f'Preset labels and icons need the 140px minimum tile width: {actions}')
        for width, height in [(1440, 960), (390, 844)]:
            self.page.set_viewport_size({'width': width, 'height': height})
            self.page.wait_for_timeout(250)
            actions = workspace_quick_actions(Image.open(io.BytesIO(self.page.screenshot(scale='css'))))
            photo, blank = actions[3:]
            with self.page.expect_file_chooser() as chooser:
                self.page.mouse.click((photo[0]+photo[2])/2, (photo[1]+photo[3])/2)
            chooser.value.set_files([])
            self.assertIsNone(self.inspect()['document'], 'Photo edit must not create a blank document')
            self.return_to_workspace()
            blank = workspace_quick_actions(Image.open(io.BytesIO(self.page.screenshot(scale='css'))))[4]
            self.page.mouse.click((blank[0]+blank[2])/2, (blank[1]+blank[3])/2)
            self.page.wait_for_timeout(250)
            dialog = self.inspect()['dialogs'][0]
            self.assertEqual(dialog['kind'], 'NewDocument')
            self.assertIsNone(self.inspect()['document'])
            self.page.screenshot(path=str(ARTIFACTS/f'quick-blank-native-dialog-{width}.png'))
            self.command('ui.dialog.cancel', {'dialog': dialog['id']})
            self.page.wait_for_timeout(150)
            self.return_to_workspace()
        self.page.set_viewport_size({'width': 1440, 'height': 960})
        for index, expected in enumerate([(1080, 1080), (1080, 1920), (1920, 1080)]):
            self.load(self.page)
            actions = workspace_quick_actions(Image.open(io.BytesIO(self.page.screenshot(scale='css'))))
            rect = actions[index]
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.wait_for_timeout(300)
            document = self.inspect()['document']
            self.assertEqual((document['width'], document['height']), expected)

    def test_34_phone_guest_search_keeps_matching_template_accessible(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.page.wait_for_timeout(250)
        before = Image.open(io.BytesIO(self.page.screenshot(scale='css')))
        previews = workspace_template_previews(before)
        self.assertTrue(previews, 'Phone home should show a template preview without scrolling')
        self.page.screenshot(path=str(ARTIFACTS/'phone-template-preview-above-fold.png'))
        search = workspace_controls(before)['search']
        self.page.mouse.click(search[0]+40, (search[1]+search[3])/2)
        self.page.keyboard.type('Sunday')
        self.page.mouse.move(0, 0)
        self.page.wait_for_timeout(300)
        picture = self.page.screenshot(scale='css')
        (ARTIFACTS/'phone-guest-search-sunday.png').write_bytes(picture)
        previews = workspace_template_previews(Image.open(io.BytesIO(picture)), has_quick_actions=False)
        self.assertEqual(len(previews), 1, 'Guest search should retain its one matching template preview')
        rect = previews[0]
        self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
        self.page.wait_for_timeout(500)
        document = self.inspect()['document']
        self.assertIsNotNone(document, 'The visible search result must open its native template')
        self.assertIn('Sunday', document['name'])
        self.assertEqual((document['width'], document['height']), (1080, 1350))

    def test_35_failed_first_upload_preserves_work_and_can_retry_or_trash(self):
        self.signed_in()
        failure = {'enabled': True, 'attempts': 0}
        requests = []
        self.page.on('request', lambda request: requests.append((request.method, request.url)))

        def upload(route):
            if route.request.method == 'PUT' and failure['enabled']:
                failure['attempts'] += 1
                route.fulfill(status=503, json={'error': 'Synthetic first upload failure'})
            else:
                route.continue_()

        self.context.route('**/api/uploads/*/*', upload)

        def click(rect):
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.mouse.move(0, 0)

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def project_state(pid):
            return next(p for p in self.projects() if p['id'] == pid)

        def trash(pid, rect):
            with self.page.expect_response(lambda r: r.url == BASE+'/api/projects/'+pid
                                           and r.request.method == 'PATCH') as response:
                click(rect)
            self.assertTrue(response.value.ok, response.value.text())
            self.page.wait_for_timeout(300)
            self.assertTrue(project_state(pid)['trashed'])

        for index, outcome in enumerate(['trash', 'retry']):
            with self.subTest(outcome=outcome):
                failure['enabled'] = True
                self.new(361+index, 247+index)
                self.stroke()
                before = self.inspect()['document']
                controls = assert_header_geometry(self, picture(f'first-save-{outcome}-before.png'), ['More', 'Save'])
                with self.page.expect_response(lambda r: '/api/uploads/' in r.url
                                               and r.request.method == 'PUT' and r.status == 503):
                    click(controls[-2])
                self.page.wait_for_timeout(300)
                pending = next(p for p in self.projects() if not p['trashed'])
                pid = pending['id']
                self.assertEqual(pending['revision'], 0)
                self.assertEqual(self.inspect()['document']['layers'], before['layers'])

                # A later browser recovery write must not replace the visible cloud error.
                self.execute('layer.duplicate')
                before = self.inspect()['document']
                error = picture(f'first-save-{outcome}-error.png')
                footer = (0, error.height-27, 900, error.height)
                pixels = error.load()
                warning_ink = sum(pixels[x, y][0] > pixels[x, y][2]+40 and pixels[x, y][1] > pixels[x, y][2]+30
                                  for y in range(footer[1], footer[3]) for x in range(footer[2]))
                self.assertGreater(warning_ink, 30, 'Failed cloud save needs a visible warning in the status row')
                self.page.wait_for_timeout(2300)
                after_recovery = picture(f'first-save-{outcome}-recovery.png')
                self.assertEqual(error.crop(footer).tobytes(), after_recovery.crop(footer).tobytes(),
                                 'Browser recovery dismissed or replaced the cloud save error')
                drafts = self.page.evaluate('''async () => await new Promise((resolve,reject) => {
                  const request=indexedDB.open('photocraft-studio-recovery');
                  request.onsuccess=()=>{const db=request.result;const rows=[];
                    const read=db.transaction('drafts').objectStore('drafts').openCursor();
                    read.onsuccess=()=>{const cursor=read.result;if(cursor){rows.push({key:cursor.key,
                      bytes:Array.from(cursor.value.data)});cursor.continue()}else{resolve(rows);db.close()}};
                    read.onerror=()=>reject(read.error)};
                  request.onerror=()=>reject(request.error);
                })''')
                current_drafts = []
                for draft in drafts:
                    with zipfile.ZipFile(io.BytesIO(bytes(draft['bytes']))) as archive:
                        document = json.loads(archive.read('manifest.json'))['document']
                    if (document['size']['width'], document['size']['height']) == (before['width'], before['height']):
                        current_drafts.append(draft['key'])
                        self.assertEqual({str(layer['id']) for layer in document['layers']},
                                         {str(layer['id']) for layer in before['layers']})
                self.assertEqual(len(current_drafts), 1, 'Recovery must contain this document, including its latest duplicate layer')
                self.assertTrue(current_drafts[0].startswith(self.accounts[-1]+':'), 'Recovery belongs to another account')
                self.assertEqual(self.inspect()['document']['layers'], before['layers'])
                self.assertEqual(project_state(pid)['revision'], 0)
                original_pixels = Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes()

                controls = assert_header_geometry(self, after_recovery, ['More', 'Comments', 'Retry save', 'Share'])
                click(controls[-2])  # Share is visibly present but disabled until a complete version exists.
                self.page.wait_for_timeout(200)
                self.assertFalse(any('/share' in url or '/invite' in url or '/members' in url
                                     for _, url in requests), 'Incomplete project offered cloud sharing')
                self.return_to_workspace()
                self.page.wait_for_timeout(300)
                card = workspace_project_card(picture(f'first-save-{outcome}-incomplete-card.png'))
                self.assertEqual(len(card['buttons']), 2, 'Incomplete card needs visible Retry save and Move to Trash')
                opened = [(method, url) for method, url in requests if method == 'GET' and url == BASE+'/api/projects/'+pid]
                click(card['preview'])
                self.page.mouse.click((card['preview'][0]+card['preview'][2])/2, card['preview'][3]+28)
                self.page.wait_for_timeout(200)
                self.assertEqual([(method, url) for method, url in requests if method == 'GET' and url == BASE+'/api/projects/'+pid], opened,
                                 'Incomplete preview/title attempted to open nonexistent cloud bytes')
                self.assertEqual(self.inspect()['document']['layers'], before['layers'])

                if outcome == 'trash':
                    trash(pid, card['buttons'][-1])
                    self.assertEqual(project_state(pid)['revision'], 0)
                    attempts = failure['attempts']
                    self.page.mouse.click(80, 32)  # Return to the native editor before editing the trashed document.
                    self.page.wait_for_timeout(200)
                    self.execute('edit.fill', {'color': '#3344cc'})
                    self.assertGreater(self.inspect()['document']['revision'], before['revision'])
                    self.page.wait_for_timeout(4000)
                    self.assertEqual(failure['attempts'], attempts, 'Trashed pending project tried to autosave')
                    self.assertIsNotNone(self.inspect()['document'], 'Trashing the card must preserve the local document')
                    picture('first-save-incomplete-trashed.png')
                else:
                    failure['enabled'] = False
                    click(card['buttons'][0])
                    saved = self.wait_revision(1)
                    self.assertEqual(saved['id'], pid, 'Retry created another project instead of completing the first one')
                    self.assertEqual(len(self.projects()), 2, 'Retry left an extra empty project behind')
                    self.assertEqual(self.inspect()['document']['layers'], before['layers'])
                    self.page.wait_for_timeout(250)
                    assert_header_geometry(self, picture('first-save-retry-completed.png'), ['More', 'Comments', 'Save', 'Share'])
                    # Reload clears the native session; reopen through the actual workspace card.
                    self.load(self.page)
                    self.assertIsNone(self.inspect()['document'])
                    card = workspace_project_card(picture('first-save-completed-list.png'))
                    click(card['preview'])
                    self.page.wait_for_timeout(700)
                    restored = self.inspect()['document']
                    self.assertIsNotNone(restored)
                    self.assertEqual((restored['width'], restored['height']), (362, 248))
                    self.assertEqual([(l['kind'], l['name']) for l in restored['layers']],
                                     [(l['kind'], l['name']) for l in before['layers']])
                    restored_pixels = Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes()
                    self.assertEqual(restored_pixels, original_pixels, 'Reopened cloud document pixels differ from the local work')
                    self.return_to_workspace()
                    self.page.wait_for_timeout(200)
                    card = workspace_project_card(picture('first-save-reopened-card.png'))
                    # The standard completed-card menu follows its preview/title row.
                    self.page.mouse.click(card['card'][2]-32, card['preview'][3]+28)
                    self.page.wait_for_timeout(200)
                    picture('first-save-completed-menu.png')
                    trash(pid, (card['card'][2]-42, card['preview'][3]+206,
                                card['card'][2]+78, card['preview'][3]+238))
                    picture('first-save-completed-trashed.png')


    def test_36_invitation_pending_failure_and_acceptance_are_visible(self):
        self.signed_in()
        self.new(640, 480)
        self.page.mouse.click(1320, 32)
        project = self.wait_revision(1)
        pid = project['id']
        member_path = BASE+'/api/projects/'+pid+'/members'
        held = []
        self.context.route(BASE+'/api/projects/'+pid+'/invite', lambda route: held.append(route))
        self.page.mouse.click(1328, 32)
        self.page.wait_for_timeout(250)

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def click(rect):
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.mouse.move(0, 0)
            self.page.wait_for_timeout(150)

        def feedback_ink(image, controls):
            bottom = controls['roles'][1][1] if len(controls['roles']) > 1 else controls['send'][3]+44
            pixels = image.load()
            return sum(max(abs(a-b) for a, b in zip(pixels[x,y], controls['background'])) > 40
                       for y in range(controls['send'][3]+4, bottom)
                       for x in range(controls['send'][0], controls['email'][2]+130))

        initial = picture('invitation-empty.png')
        controls = sharing_invitation_controls(initial)
        self.assertEqual(len(controls['roles']), 1)
        footer = (0, initial.height-27, 900, initial.height)
        email = 'invitation-fixture@example.invalid'
        click(controls['email'])
        self.page.keyboard.type(email)
        self.page.wait_for_timeout(150)  # Let egui enable Send after the native text edit commits its input frame.
        click(controls['send'])
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0].request.post_data_json, {'email': email, 'role': 'edit'})
        pending = picture('invitation-pending.png')
        pending_controls = sharing_invitation_controls(pending)
        assert_invitation_feedback_geometry(self, pending, pending_controls, pending=True)
        self.assertGreater(feedback_ink(pending, pending_controls), 80, 'Pending invitation needs inline feedback')
        self.assertNotEqual(initial.crop(controls['send']).tobytes(), pending.crop(controls['send']).tobytes(),
                            'Send invitation must visibly enter its pending state')
        click(pending_controls['send'])
        click(pending_controls['send'])
        self.assertEqual(len(held), 1, 'Pending invitation allowed duplicate submissions')
        self.assertEqual(initial.crop(footer).tobytes(), pending.crop(footer).tobytes(),
                         'Invitation pending state should stay in its sharing dialog')

        # Reproduce the server's partial outcome: access is committed before the
        # mail provider reports failure. Only the local route is fulfilled; no mail is sent.
        self.assertTrue(self.context.request.put(member_path, headers={'Origin': BASE},
                                               data={'email': email, 'role': 'edit'}).ok)
        delivery = str(uuid.uuid4())
        self.db.execute('INSERT INTO photocraft.invitation_deliveries(id,project_id,sender_id,email,status) VALUES(%s,%s,%s,%s,%s)',
                        (delivery, pid, self.accounts[-1], email, 'failed'))
        error = 'Access was granted, but the email service did not confirm sending. Retry the invitation in a minute, or share the project link.'
        with self.page.expect_response(lambda response: response.url == member_path and response.request.method == 'GET') as refreshed:
            held[0].fulfill(status=502, json={'error': error})
        self.assertEqual(refreshed.value.json()[0]['delivery'], 'failed')
        self.page.wait_for_timeout(250)
        failed = picture('invitation-failed-access-retained.png')
        failed_controls = sharing_invitation_controls(failed)
        assert_invitation_feedback_geometry(self, failed, failed_controls)
        self.assertEqual(len(failed_controls['roles']), 2, 'Granted membership must be rendered even when mail fails')
        region = (failed_controls['send'][0], failed_controls['send'][3],
                  failed_controls['email'][2]+130, failed_controls['roles'][1][1])
        pixels = failed.load()
        warning = sum(pixels[x,y][0] > pixels[x,y][2]+40 and pixels[x,y][1] > pixels[x,y][2]+30
                      for y in range(region[1], region[3]) for x in range(region[0], region[2]))
        self.assertGreater(warning, 50, 'Provider failure must be visible immediately below Send invitation')
        self.assertEqual(initial.crop(footer).tobytes(), failed.crop(footer).tobytes())

        # Retry without retyping: the failed address must still be in the native field.
        click(failed_controls['send'])
        self.assertEqual(len(held), 2)
        self.assertEqual(held[1].request.post_data_json['email'], email)
        self.db.execute('UPDATE photocraft.invitation_deliveries SET status=%s WHERE id=%s', ('sent', delivery))
        with self.page.expect_response(lambda response: response.url == member_path and response.request.method == 'GET') as refreshed:
            held[1].fulfill(json={'ok': True})
        self.assertEqual(refreshed.value.json()[0]['delivery'], 'sent')
        self.page.wait_for_timeout(250)
        accepted = picture('invitation-accepted-by-service.png')
        accepted_controls = sharing_invitation_controls(accepted)
        assert_invitation_feedback_geometry(self, accepted, accepted_controls)
        self.assertEqual(len(accepted_controls['roles']), 2)
        self.assertGreater(feedback_ink(accepted, accepted_controls), 80, 'Accepted invitation needs inline feedback')
        self.assertEqual(initial.crop(footer).tobytes(), accepted.crop(footer).tobytes())
        click(accepted_controls['send'])
        self.assertEqual(len(held), 2, 'Successful invitation should clear the address and disable another blank send')
        self.page.keyboard.press('Escape')
        self.assertEqual(self.inspect()['document']['width'], 640)


    def test_37_project_dialog_responses_ignore_stale_projects_and_generations(self):
        self.signed_in()
        projects = []
        for width in [320, 360]:
            self.new(width, 240)
            with self.page.expect_response(lambda response: response.url == BASE+'/api/projects'
                                           and response.request.method == 'POST') as created:
                self.page.mouse.click(1320, 32)
            project = self.wait_revision(1)
            self.assertEqual(project['id'], created.value.json()['id'])
            projects.append(project)
            self.assertTrue(self.context.request.put(BASE+'/api/projects/'+project['id']+'/members',
                headers={'Origin': BASE}, data={'email': f'project-{width}@example.invalid', 'role': 'edit'}).ok)
        first, second = projects
        a_members = BASE+'/api/projects/'+first['id']+'/members'
        b_members = BASE+'/api/projects/'+second['id']+'/members'
        held_a, held_share = [], []
        self.context.route(a_members, lambda route: held_a.append(route))
        self.context.route(BASE+'/api/projects/'+first['id']+'/share', lambda route: held_share.append(route))

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def click(rect):
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.mouse.move(0, 0)
            self.page.wait_for_timeout(200)

        self.execute('document.activate', {'document': 0})
        self.page.wait_for_timeout(250)
        self.page.mouse.click(1328, 32)
        self.page.wait_for_timeout(250)
        self.assertEqual(len(held_a), 1)
        controls = sharing_invitation_controls(picture('stale-dialog-project-a-loading.png'))
        click(controls['buttons'][1])  # Create a view link, while the initial member request is also delayed.
        self.assertEqual(len(held_share), 1)
        self.page.keyboard.press('Escape')
        self.execute('document.activate', {'document': 1})
        self.page.wait_for_timeout(250)
        self.page.mouse.click(1328, 32)
        self.page.mouse.move(0, 0)
        self.page.wait_for_timeout(350)
        baseline = picture('stale-dialog-project-b-current.png')
        self.assertEqual(len(sharing_invitation_controls(baseline)['roles']), 2)
        self.assertEqual(self.inspect()['document']['width'], 360)
        held_a[0].fulfill(json=[{'email': 'wrong-project-a@example.invalid', 'role': 'edit', 'joined': False, 'delivery': None}])
        held_share[0].fulfill(json={'url': BASE+'/?share=stale-project-a-must-not-appear'})
        self.page.wait_for_timeout(250)
        after = picture('stale-dialog-project-a-responses-ignored.png')
        modal = (500, 130, 940, 850)
        self.assertEqual(baseline.crop(modal).tobytes(), after.crop(modal).tobytes(),
                         'Project A response changed project B members or exposed A view link/Copy control')

        # A role edit is newer than the initial members fetch for the same project.
        # Its result must remain visible when that initial fetch eventually completes.
        self.page.keyboard.press('Escape')
        self.page.wait_for_timeout(150)  # Let egui release its modal input boundary before reopening Share.
        held_b, held_role = [], []

        def members(route):
            if route.request.method == 'GET' and not held_b:
                held_b.append(route)
            elif route.request.method == 'PUT':
                held_role.append(route)
            else:
                route.continue_()

        self.context.route(b_members, members)
        stale_members = self.context.request.get(b_members).json()
        self.page.mouse.click(1328, 32)
        self.page.wait_for_timeout(250)
        self.assertEqual(len(held_b), 1)
        controls = sharing_invitation_controls(picture('stale-members-initial-fetch-pending.png'))
        role = controls['roles'][1]
        click(role)
        self.page.mouse.click((role[0]+role[2])/2, role[3]+19)
        self.page.wait_for_timeout(200)
        self.assertEqual(len(held_role), 1)
        pending = sharing_invitation_controls(picture('member-role-write-pending.png'))
        click(pending['roles'][1])
        self.page.mouse.click((role[0]+role[2])/2, role[3]+19)
        self.page.wait_for_timeout(150)
        self.assertEqual(len(held_role), 1, 'Pending role mutation allowed a second write that could reorder access')
        with self.page.expect_response(lambda response: response.url == b_members
                                       and response.request.method == 'GET') as refreshed:
            with self.page.expect_response(lambda response: response.url == b_members
                                           and response.request.method == 'PUT') as changed:
                held_role[0].fulfill(response=held_role[0].fetch())
        self.assertTrue(changed.value.ok)
        self.assertEqual(refreshed.value.json()[0]['role'], 'view')
        self.page.mouse.move(0, 0)
        self.page.wait_for_timeout(250)
        current = picture('stale-members-newer-role-visible.png')
        held_b[0].fulfill(json=stale_members)
        self.page.wait_for_timeout(250)
        after = picture('stale-members-old-response-ignored.png')
        self.assertEqual(current.crop(modal).tobytes(), after.crop(modal).tobytes(),
                         'Initial members fetch overwrote the newer role edit')
        self.assertEqual(self.context.request.get(b_members).json()[0]['role'], 'view')


    def test_38_invitation_result_does_not_replace_new_draft_after_project_return(self):
        self.signed_in()
        projects = []
        for width in [320, 360]:
            self.new(width, 240)
            with self.page.expect_response(lambda response: response.url == BASE+'/api/projects'
                                           and response.request.method == 'POST') as created:
                self.page.mouse.click(1320, 32)
            project = self.wait_revision(1)
            self.assertEqual(project['id'], created.value.json()['id'])
            projects.append(project)
        first = projects[0]
        member_path = BASE+'/api/projects/'+first['id']+'/members'
        held = []
        self.context.route(BASE+'/api/projects/'+first['id']+'/invite', lambda route: held.append(route))

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def click(rect):
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.mouse.move(0, 0)
            self.page.wait_for_timeout(150)

        self.execute('document.activate', {'document': 0})
        self.page.wait_for_timeout(250)
        self.page.mouse.click(1328, 32)
        self.page.wait_for_timeout(250)
        controls = sharing_invitation_controls(picture('invitation-project-a-initial.png'))
        email = 'return-to-project@example.invalid'
        click(controls['email'])
        self.page.keyboard.type(email)
        self.page.wait_for_timeout(150)
        click(controls['send'])
        self.assertEqual(len(held), 1)
        self.page.keyboard.press('Escape')
        self.execute('document.activate', {'document': 1})
        self.page.wait_for_timeout(200)
        self.assertEqual(self.inspect()['document']['width'], 360)
        self.page.mouse.click(1328, 32)
        self.page.wait_for_timeout(200)
        picture('invitation-project-b-while-a-pending.png')
        self.page.keyboard.press('Escape')
        self.execute('document.activate', {'document': 0})
        self.page.wait_for_timeout(200)
        self.page.mouse.click(1328, 32)
        self.page.wait_for_timeout(250)
        controls = sharing_invitation_controls(picture('invitation-returned-a-pending.png'))
        click(controls['email'])
        self.page.keyboard.press('ControlOrMeta+A')
        # Retype the same address as a new draft. Equality alone cannot authorize
        # an older invitation completion to clear this later interaction.
        self.page.keyboard.type(email)
        self.page.wait_for_timeout(150)
        click(controls['send'])
        self.assertEqual(len(held), 1, 'Returning to a project lost its pending invitation write guard')

        self.assertTrue(self.context.request.put(member_path, headers={'Origin': BASE},
                                               data={'email': email, 'role': 'edit'}).ok)
        self.db.execute('INSERT INTO photocraft.invitation_deliveries(id,project_id,sender_id,email,status) VALUES(%s,%s,%s,%s,%s)',
                        (str(uuid.uuid4()), first['id'], self.accounts[-1], email, 'sent'))
        held[0].fulfill(json={'ok': True})
        self.page.wait_for_timeout(450)
        current = picture('invitation-returned-a-old-result-ignored.png')
        controls = sharing_invitation_controls(current)
        self.assertEqual(len(controls['roles']), 2, 'Completing the old write must refresh actual project membership')
        pixels = current.load()
        status_ink = sum(max(abs(a-b) for a, b in zip(pixels[x,y], controls['background'])) > 40
                         for y in range(controls['send'][3]+4, controls['roles'][1][1])
                         for x in range(controls['send'][0], controls['email'][2]+120))
        self.assertLess(status_ink, 10, 'An old invitation result appeared in the reopened project dialog')
        click(controls['send'])
        self.assertEqual(len(held), 2, 'An old success cleared the newly typed address')
        self.assertEqual(held[1].request.post_data_json['email'], email)
        held[1].fulfill(status=502, json={'error': 'Synthetic retry stopped; no email was sent'})
        self.page.wait_for_timeout(200)
        self.assertEqual(self.inspect()['document']['width'], 320)


    def test_39_startup_serializes_connection_and_retries_transient_failures(self):
        token = self.signed_in()
        user = self.context.request.get(BASE+'/api/me').json()
        observations = []

        for scenario in ['delayed', 'config-503', 'me-503', 'guest-401']:
            # A failed subtest can leave deliberately held fetches alive. Close
            # that page before installing the next scenario's route callbacks.
            if len(self.contexts) > 1:
                self.contexts[-1].close()
            with self.subTest(scenario=scenario):
                ctx = self.browser.new_context(viewport={'width': 1440, 'height': 960})
                self.contexts.append(ctx)
                ctx.add_cookies([{'name': 'pc_session', 'value': token, 'url': BASE,
                                 'httpOnly': True, 'sameSite': 'Lax'}])
                page = ctx.new_page()
                page.on('pageerror', lambda error: self.errors.append(str(error)))
                counts = {'config': 0, 'me': 0, 'projects': 0}
                finished = {'config': 0, 'me': 0, 'projects': 0}
                held = {'config': [], 'me': []}
                ready = {'cloud': True, 'signIn': True}

                def config(route):
                    counts['config'] += 1
                    if scenario == 'delayed' or (scenario == 'config-503' and counts['config'] == 1):
                        held['config'].append(route)
                    else:
                        route.fulfill(json=ready)

                def me(route):
                    counts['me'] += 1
                    if scenario == 'delayed' or (scenario == 'me-503' and counts['me'] == 1):
                        held['me'].append(route)
                    elif scenario == 'guest-401':
                        route.fulfill(status=401, json={'error': 'Sign in required'})
                    else:
                        route.fulfill(json=user)

                def projects(route):
                    counts['projects'] += 1
                    route.continue_()

                def checkpoint(stage):
                    page.screenshot(path=str(ARTIFACTS/f'startup-{scenario}-{stage}.png'))
                    observations.append({'scenario': scenario, 'stage': stage, 'counts': dict(counts), 'finished': dict(finished)})
                    (ARTIFACTS/'startup-request-counts.json').write_text(json.dumps(observations, indent=2))

                def wait_for_count(kind, count):
                    deadline = time.monotonic()+8
                    while counts[kind] < count and time.monotonic() < deadline:
                        page.wait_for_timeout(50)
                    self.assertGreaterEqual(counts[kind], count, f'{scenario}: no {kind} request {count}')

                def warning_pixels():
                    image = Image.open(io.BytesIO(page.screenshot(scale='css'))).convert('RGB')
                    return sum(r > b+40 and g > b+30 for r, g, b in
                               image.crop((0, image.height-28, 1100, image.height)).getdata())

                ctx.route(BASE+'/api/config', config)
                ctx.route(BASE+'/api/me', me)
                ctx.route(BASE+'/api/projects', projects)
                def request_finished(request):
                    for kind in finished:
                        if request.url == BASE+'/api/'+kind:
                            finished[kind] += 1
                page.on('requestfinished', request_finished)
                # networkidle would deadlock while the real browser requests are held.
                page.goto(BASE+'/', wait_until='domcontentloaded', timeout=90000)
                page.wait_for_function('typeof window.photocraftCommand === "function"', timeout=60000)
                wait_for_count('config', 1)
                if scenario == 'delayed':
                    page.wait_for_timeout(2200)
                    checkpoint('config-held')
                    self.assertEqual(counts, {'config': 1, 'me': 0, 'projects': 0},
                                     'Slow startup config allowed overlapping connection attempts')
                    held['config'][0].fulfill(json=ready)
                    wait_for_count('me', 1)
                    page.wait_for_timeout(2200)
                    checkpoint('me-held')
                    self.assertEqual(counts, {'config': 1, 'me': 1, 'projects': 0},
                                     'Connection guard ended before the session request completed')
                    held['me'][0].fulfill(json=user)
                elif scenario in {'config-503', 'me-503'}:
                    kind = 'config' if scenario == 'config-503' else 'me'
                    wait_for_count(kind, 1)
                    held[kind][0].fulfill(status=503, json={'error': 'Synthetic cloud startup unavailable'})
                    page.wait_for_timeout(250)
                    checkpoint('temporary-failure')
                    self.assertEqual(counts['projects'], 0, 'An unknown session must not load signed-in projects')
                    self.assertEqual(counts['me'], 0 if kind == 'config' else 1)
                    self.assertGreater(warning_pixels(), 40, 'Temporary connection failure was presented as a ready guest workspace')
                    wait_for_count('config', 2)
                else:
                    wait_for_count('me', 1)

                if scenario != 'guest-401':
                    wait_for_count('projects', 1)
                page.wait_for_timeout(2200)
                checkpoint('settled')
                expected = {'delayed': (1, 1, 1), 'config-503': (2, 1, 1),
                            'me-503': (2, 2, 1), 'guest-401': (1, 1, 0)}[scenario]
                self.assertEqual(counts, dict(zip(['config', 'me', 'projects'], expected)),
                                 'Startup did not settle after one successful connection')
                self.assertEqual(finished, counts, 'A completed connection left response bodies unread and browser requests unfinished')
                self.assertLess(warning_pixels(), 10, 'Successful retry left its connection error visible')


    def test_40_lost_commit_confirmation_reconciles_exact_saved_version(self):
        token = self.signed_in()
        observations = []

        def picture(name, page=None):
            data = (page or self.page).screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def save(actions, name='Save', page=None):
            page = page or self.page
            controls = assert_header_geometry(self, Image.open(io.BytesIO(page.screenshot())), actions)
            rect = controls[actions.index(name)]
            page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)

        def metadata(pid):
            response = self.context.request.get(BASE+'/api/projects/'+pid)
            self.assertTrue(response.ok)
            return response.json()

        def wait_revision(pid, expected):
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                value = metadata(pid)
                if value['revision'] >= expected:
                    return value
                self.page.wait_for_timeout(100)
            self.fail(f'{pid}: retry never reached revision {expected}')

        for index, scenario in enumerate(['unchanged', 'edited', 'collaborator']):
            with self.subTest(scenario=scenario):
                if index:
                    self.context.unroute('**/api/projects/*/uploads')
                    self.context.unroute('**/api/uploads/*/commit')
                    self.page.remove_listener('response', observe)
                    self.load(self.page)
                self.new(410+index, 290)
                self.stroke()  # Keep saved_local=0 from masking the failure through presence sync.
                self.execute('shape.create', {'kind': 'rect', 'rect': [45, 45, 90, 60],
                                              'fill': '#ffaa00', 'name': 'Original card'})
                attempts, commits = [], []
                state = {'lose_next': True}
                responses = []

                def trace():
                    (ARTIFACTS/f'lost-ack-{scenario}-requests.json').write_text(json.dumps(
                        {'attempts': attempts, 'durable_commits': commits, 'responses': responses}, indent=2))

                def observe(response):
                    if '/api/' in response.url:
                        responses.append({'method': response.request.method, 'path': urlparse(response.url).path,
                                          'status': response.status})
                        trace()

                self.page.on('response', observe)

                def upload(route):
                    attempts.append({'url': route.request.url, **route.request.post_data_json})
                    route.continue_()

                def commit(route):
                    if state['lose_next']:
                        state['lose_next'] = False
                        response = route.fetch()  # The real local server durably commits first.
                        self.assertTrue(response.ok, response.text())
                        commits.append(response.json())
                        route.fulfill(status=503, json={'error': 'Synthetic lost commit confirmation'})
                    else:
                        route.continue_()

                self.context.route('**/api/projects/*/uploads', upload)
                self.context.route('**/api/uploads/*/commit', commit)
                count_before = len(self.projects())
                with self.page.expect_response(lambda r: r.url.endswith('/commit') and r.status == 503):
                    save(['More', 'Save'])
                self.page.wait_for_timeout(250)
                self.assertEqual(len(commits), 1)
                self.assertEqual(commits[0]['revision'], 1)
                pid = attempts[0]['url'].split('/')[-2]
                first = metadata(pid)
                self.assertEqual(first['revision'], 1, 'The fixture must lose the ACK after a real durable commit')
                self.assertEqual(first['content']['sha256'], attempts[0]['sha256'])
                picture(f'lost-ack-{scenario}-server-saved-browser-uncertain.png')

                if scenario == 'edited':
                    self.execute('shape.create', {'kind': 'rect', 'rect': [170, 80, 70, 90],
                                                  'fill': '#3344cc', 'name': 'Later local edit'})
                elif scenario == 'collaborator':
                    other_context, second = self.context_page('?project='+pid, token=token)
                    layers = self.inspect(second)['document']['layers']
                    self.execute('layer.renameLayer', {'layer': layers[0]['id'], 'name': 'Remote background'}, page=second)
                    save(['More', 'Comments', 'Save', 'Share'], page=second)
                    wait_revision(pid, 2)
                    self.execute('layer.renameLayer', {'layer': layers[1]['id'], 'name': 'Later local card'})
                    other_context.close()

                before = self.inspect()['document']
                pixels = Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes()
                retry_start = len(responses)
                save(['More', 'Comments', 'Retry save', 'Share'], name='Retry save')
                # A normal save refreshes the project list; a server-merged save
                # instead downloads the merged native file into the editor.
                completion_path = '/api/projects/'+pid+'/content' if scenario == 'collaborator' else '/api/projects'
                deadline = time.monotonic()+10
                while time.monotonic() < deadline:
                    retry_responses = responses[retry_start:]
                    if any(r['status'] == 409 for r in retry_responses) or any(
                            r['method'] == 'GET' and r['path'] == completion_path and r['status'] == 200
                            for r in retry_responses):
                        break
                    self.page.wait_for_timeout(50)
                trace()
                picture(f'lost-ack-{scenario}-retry-result.png')
                self.assertFalse(any(r['status'] == 409 for r in responses[retry_start:]),
                                 f'A durable commit whose ACK was lost became a false conflict: {responses[retry_start:]}')
                self.assertTrue(any(r['method'] == 'GET' and r['path'] == completion_path and r['status'] == 200
                                    for r in responses[retry_start:]), 'Retry never completed its successful refresh or merged-file download')
                expected_revision = {'unchanged': 1, 'edited': 2, 'collaborator': 3}[scenario]
                current = wait_revision(pid, expected_revision)
                self.page.wait_for_timeout(650)
                assert_header_geometry(self, picture(f'lost-ack-{scenario}-retry-completed.png'),
                                       ['More', 'Comments', 'Save', 'Share'])
                self.assertEqual(len(self.projects()), count_before+1, 'Retry created a duplicate project')
                self.assertEqual(current['revision'], expected_revision)
                self.assertEqual(len(attempts), 1 if scenario == 'unchanged' else 2)
                if scenario != 'unchanged':
                    self.assertEqual(attempts[-1]['base_revision'], 1, 'Retry guessed a base instead of proving its own committed bytes')
                if scenario != 'collaborator':
                    self.assertEqual(current['content']['sha256'], attempts[-1]['sha256'])
                expected_names = ([l['name'] for l in before['layers']] if scenario != 'collaborator'
                                  else ['Remote background', 'Later local card'])
                self.assertEqual([l['name'] for l in self.inspect()['document']['layers']], expected_names)
                data = b''.join(self.context.request.get(BASE+f'/api/projects/{pid}/content?revision={expected_revision}&part={part}').body()
                                for part in range((current['content']['bytes']+524287)//524288))
                self.assertEqual(hashlib.sha256(data).hexdigest(), current['content']['sha256'])
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    # The layer panel reports top-to-bottom while the manifest
                    # stores the native stacking order. Pixel equality below
                    # verifies composition without conflating those two orders.
                    self.assertCountEqual([l['name'] for l in json.loads(archive.read('manifest.json'))['document']['layers']], expected_names)
                self.context.unroute('**/api/projects/*/uploads', upload)
                self.context.unroute('**/api/uploads/*/commit', commit)
                self.load(self.page, '?project='+pid)
                self.assertEqual(Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes(), pixels,
                                 'Save retry or reopen changed the document pixels')
                observations.append({'scenario': scenario, 'revision': current['revision'],
                                     'attempt_bases': [a['base_revision'] for a in attempts],
                                     'cloud_sha256': current['content']['sha256'], 'pixel_sha256': hashlib.sha256(pixels).hexdigest()})
                (ARTIFACTS/'lost-ack-reconciliation.json').write_text(json.dumps(observations, indent=2))

    def test_41_recovered_and_template_copies_drop_closed_document_bindings(self):
        self.signed_in()

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def open_template():
            self.return_to_workspace()
            self.page.mouse.click(100, 199)
            self.page.wait_for_timeout(250)
            rect = workspace_template_previews(picture('copy-template-gallery.png'), has_quick_actions=False)[0]
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.wait_for_timeout(650)
            self.assertIsNotNone(self.inspect()['document'])

        def wait_saved(pid):
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                response = self.context.request.get(BASE+'/api/projects/'+pid)
                if response.ok and response.json()['revision'] >= 1:
                    return response.json()
                self.page.wait_for_timeout(100)
            self.fail('The new project did not receive its first complete save')

        for index, source in enumerate(['recovery', 'template']):
            with self.subTest(source=source):
                if index:
                    self.load(self.page)
                    open_template()
                else:
                    self.new(433, 299)
                    self.stroke()
                with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
                    self.page.mouse.click(1320, 32)
                pid = created.value.json()['id']
                original = wait_saved(pid)
                self.page.wait_for_timeout(200)
                if source == 'recovery':
                    self.execute('edit.fill', {'color': '#cc5577'})
                    self.page.wait_for_timeout(2300)  # Persist browser recovery before the cloud autosave deadline.
                expected = self.inspect()['document']
                pixels = Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes()
                # Use the existing native close command while keeping Cloud and
                # its old binding alive in this same WASM session.
                self.execute('file.close')
                self.assertIsNone(self.inspect()['document'])
                if source == 'recovery':
                    self.return_to_workspace()
                    self.page.mouse.click(100, 249)
                    self.page.wait_for_timeout(200)
                    picture('same-session-recovery-available.png')
                    self.click_browser_recovery()
                    self.page.wait_for_timeout(500)
                else:
                    open_template()
                restored = self.inspect()['document']
                self.assertEqual((restored['width'], restored['height']), (expected['width'], expected['height']))
                self.assertEqual([l['name'] for l in restored['layers']], [l['name'] for l in expected['layers']])
                self.assertEqual(Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes(), pixels)
                controls = assert_header_geometry(self, picture(f'same-session-{source}-unbound-copy.png'), ['More', 'Save'])
                count = len(self.projects())
                rect = controls[1]
                with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
                    self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
                copy_id = created.value.json()['id']
                self.assertNotEqual(copy_id, pid)
                wait_saved(copy_id)
                self.assertEqual(len(self.projects()), count+1)
                old = self.context.request.get(BASE+'/api/projects/'+pid).json()
                self.assertEqual((old['revision'], old['content']['sha256']), (original['revision'], original['content']['sha256']),
                                 'Saving a recovered/template copy overwrote the closed original project')
                picture(f'same-session-{source}-saved-new-project.png')


    def test_42_project_open_ignores_duplicate_and_superseded_responses(self):
        self.signed_in()
        projects = []
        for width, title in [(421, 'Async project A'), (422, 'Async project B')]:
            self.new(width, 280)
            with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
                self.page.mouse.click(1320, 32)
            pid = created.value.json()['id']
            deadline = time.monotonic()+15
            while next(p for p in self.projects() if p['id'] == pid)['revision'] == 0 and time.monotonic() < deadline:
                self.page.wait_for_timeout(100)
            self.assertTrue(self.context.request.patch(BASE+'/api/projects/'+pid, headers={'Origin': BASE}, data={'title': title}).ok)
            projects.append(pid)

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def choose(title):
            controls = workspace_controls(Image.open(io.BytesIO(self.page.screenshot())))
            search = controls['search']
            self.page.mouse.click(search[0]+40, (search[1]+search[3])/2)
            self.page.keyboard.press('ControlOrMeta+A')
            self.page.keyboard.type(title)
            self.page.wait_for_timeout(150)
            rect = workspace_project_card(Image.open(io.BytesIO(self.page.screenshot())))['preview']
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.wait_for_timeout(150)
            return rect

        for outcome in ['success', 'error']:
            with self.subTest(late_response=outcome):
                self.context.unroute(BASE+'/api/projects/'+projects[0])
                self.load(self.page)
                held = []
                self.context.route(BASE+'/api/projects/'+projects[0], lambda route: held.append(route))
                rect = choose('Async project A')
                self.assertEqual(len(held), 1)
                self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
                self.page.wait_for_timeout(200)
                picture(f'project-open-{outcome}-duplicate-pending.png')
                self.assertEqual(len(held), 1, 'The same pending card started duplicate downloads')
                choose('Async project B')
                self.page.wait_for_timeout(650)
                self.assertEqual(self.inspect()['document']['width'], 422)
                before = picture(f'project-open-{outcome}-b-current.png')
                if outcome == 'success':
                    held[0].fulfill(response=held[0].fetch())
                else:
                    held[0].fulfill(status=503, json={'error': 'Synthetic superseded project failure'})
                self.page.wait_for_timeout(650)
                after = picture(f'project-open-{outcome}-a-ignored.png')
                self.assertEqual(self.inspect()['document']['width'], 422, 'An older open replaced the more recent project choice')
                footer = (0, before.height-27, 1100, before.height)
                self.assertEqual(before.crop(footer).tobytes(), after.crop(footer).tobytes(),
                                 'A superseded open changed the current project status')

    def test_43_stale_project_lists_cannot_undo_visible_trash_or_restore(self):
        self.signed_in()
        self.new(455, 288)
        self.page.mouse.click(1320, 32)
        pid = self.wait_revision(1)['id']
        self.return_to_workspace()
        self.page.mouse.click(100, 249)
        self.page.wait_for_timeout(200)
        held, stale = [], []
        phase = {'hold': False}

        def listing(route):
            if phase['hold']:
                phase['hold'] = False
                stale.append(route.fetch().json())
                held.append(route)
            else:
                route.continue_()

        self.context.route(BASE+'/api/projects', listing)

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def menu_action(offset):
            card = workspace_project_card(Image.open(io.BytesIO(self.page.screenshot())))
            self.page.mouse.click(card['card'][2]-32, card['preview'][3]+28)
            self.page.wait_for_timeout(100)
            with self.page.expect_response(lambda r: r.url == BASE+'/api/projects/'+pid and r.request.method == 'PATCH') as changed:
                self.page.mouse.click(card['card'][2]+5, card['preview'][3]+offset)
            self.assertTrue(changed.value.ok)
            self.page.wait_for_timeout(150)

        for index, trashed in enumerate([True, False]):
            with self.subTest(trashed=trashed):
                if not trashed:
                    self.load(self.page)  # Give this subcase its authoritative current trash state.
                    self.page.mouse.click(100, 399)
                    self.page.wait_for_timeout(150)
                phase['hold'] = True
                menu_action(185)  # Star/unstar starts a real list refresh with the old trash state.
                self.assertEqual(len(held), index+1)
                self.assertEqual(next(p for p in stale[-1] if p['id'] == pid)['trashed'], not trashed)
                with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'GET'):
                    menu_action(223)  # Trash/restore produces a newer list response.
                self.page.mouse.move(0, 0)
                self.page.wait_for_timeout(200)
                before = picture(f'project-list-trashed-{trashed}-current.png')
                with self.assertRaises(AssertionError):
                    workspace_project_card(before)
                held[-1].fulfill(json=stale[-1])
                self.page.wait_for_timeout(250)
                after = picture(f'project-list-trashed-{trashed}-stale-ignored.png')
                with self.assertRaises(AssertionError, msg='An old list response restored a card removed by a newer mutation'):
                    workspace_project_card(after)
                self.assertEqual(next(p for p in self.projects() if p['id'] == pid)['trashed'], trashed)


    def test_44_browser_recovery_keeps_one_last_visited_copy_per_scope(self):
        observations = []

        def drafts():
            return self.page.evaluate('''async () => await new Promise((resolve,reject) => {
              const request=indexedDB.open('photocraft-studio-recovery');
              request.onsuccess=()=>{const db=request.result;const rows=[];
                const cursor=db.transaction('drafts').objectStore('drafts').openCursor();
                cursor.onsuccess=()=>{const row=cursor.result;if(row){rows.push({key:row.key,
                  name:row.value.name,savedAt:row.value.savedAt,data:Array.from(row.value.data)});row.continue()}
                  else{resolve(rows);db.close()}};cursor.onerror=()=>reject(cursor.error)};
              request.onerror=()=>reject(request.error);
            })''')

        def width(row):
            with zipfile.ZipFile(io.BytesIO(bytes(row['data']))) as archive:
                return json.loads(archive.read('manifest.json'))['document']['size']['width']

        def last_visited(expected, scope='guest'):
            self.page.wait_for_timeout(2300)
            rows = [r for r in drafts() if r['key'].startswith(scope+':') or r['key'].startswith('guest:')]
            self.assertEqual(len(rows), 1, 'Browser recovery accumulated more than the last visited document')
            self.assertEqual(width(rows[0]), expected, 'Recovery points to a different document than the last visit')
            observations.append({'expected_width': expected, 'scope': scope, 'key': rows[0]['key'],
                                 'stored_width': width(rows[0]), 'physical_entry_count': len(drafts())})
            (ARTIFACTS/'one-recovery-observations.json').write_text(json.dumps(observations, indent=2))
            return rows[0]

        self.new(461, 301)  # Clean native documents must also become the last visited recovery.
        first = last_visited(461)
        self.new(462, 302)
        second = last_visited(462)
        self.execute('document.activate', {'document': 0})
        last_visited(461)

        # A newer visit from a peer can finish while this tab's edit is still
        # debouncing. Flush time must not make the older activity look newer.
        self.stroke()
        self.page.wait_for_timeout(200)
        self.page.evaluate('''async (peer) => await new Promise((resolve,reject) => {
          const request=indexedDB.open('photocraft-studio-recovery');request.onerror=()=>reject(request.error);
          request.onsuccess=()=>{const db=request.result;const tx=db.transaction('drafts','readwrite');
            const store=tx.objectStore('drafts');const keys=store.getAllKeys();keys.onsuccess=()=>{
              for(const key of keys.result)if(key.startsWith('guest:'))store.delete(key);
              store.put({name:'Newer peer visit',savedAt:Date.now(),data:new Uint8Array(peer.data)},'guest:newer-peer');
            };tx.oncomplete=()=>{db.close();resolve()};tx.onerror=()=>reject(tx.error);
          };
        })''', second)
        peer = last_visited(462)
        self.assertEqual(peer['key'], 'guest:newer-peer', 'An older debounced edit evicted the newer peer visit')
        self.assertEqual(self.inspect()['document']['width'], 461)
        self.execute('document.activate', {'document': 1})
        self.page.wait_for_timeout(150)
        self.execute('document.activate', {'document': 0})
        last_visited(461)
        self.execute('document.activate', {'document': 1})
        self.page.wait_for_timeout(100)
        self.execute('document.activate', {'document': 0})
        last_visited(461)

        # One recovery slot cannot safely cover multiple unsaved native tabs
        # across an authentication redirect. Exercise the actual Google control.
        self.execute('document.activate', {'document': 1})
        self.stroke()
        self.execute('document.activate', {'document': 0})
        self.context.route('**/api/config', lambda route: route.fulfill(json={'cloud': True, 'signIn': True}))
        redirects = []
        self.context.route('**/auth/login', lambda route: (redirects.append(route.request.url),
                           route.fulfill(content_type='text/html', body='<h1>Unexpected sign-in redirect</h1>')))
        self.page.wait_for_timeout(2200)
        self.page.mouse.click(1400, 32)
        self.page.wait_for_timeout(150)
        self.page.mouse.click(1270, 170)
        self.page.wait_for_timeout(300)
        self.assertEqual(redirects, [], 'Signing in discarded unsaved tabs that one recovery entry cannot preserve')
        self.assertEqual(self.inspect()['document']['width'], 461)
        self.page.screenshot(path=str(ARTIFACTS/'one-recovery-sign-in-unsaved-tabs-preserved.png'))
        self.page.keyboard.press('Escape')
        self.load(self.page)
        row = last_visited(461)
        self.assertEqual(row['key'], first['key'], 'Reload should preserve the document-specific recovery identity')

        foreign = str(uuid.uuid4())+':private-other-account'

        def seed_legacy(scope, newest):
            self.page.evaluate('''async ({scope,older,newest,foreign}) => await new Promise((resolve,reject) => {
              const request=indexedDB.open('photocraft-studio-recovery');request.onerror=()=>reject(request.error);
              request.onsuccess=()=>{const db=request.result;const tx=db.transaction('drafts','readwrite');
                const store=tx.objectStore('drafts');const keys=store.getAllKeys();keys.onsuccess=()=>{
                  for(const key of keys.result)if(key.startsWith(scope+':')||key.startsWith('guest:'))store.delete(key);
                  const stamp=Date.now();
                  store.put({name:'Older legacy copy',savedAt:stamp-2000,data:new Uint8Array(older.data)},scope+':legacy-older');
                  store.put({name:'Newest legacy copy',savedAt:stamp-1000,data:new Uint8Array(newest.data)},scope+':legacy-newer');
                  if(scope==='guest')store.put({name:'Private other account',savedAt:stamp,data:new Uint8Array(older.data)},foreign);
                };tx.oncomplete=()=>{db.close();resolve()};tx.onerror=()=>reject(tx.error);
              };
            })''', {'scope': scope, 'older': first if newest is second else second,
                     'newest': newest, 'foreign': foreign})

        seed_legacy('guest', second)
        self.load(self.page)
        guest = last_visited(462)
        self.assertEqual(guest['key'], 'guest:legacy-newer')
        private_before = next(r for r in drafts() if r['key'] == foreign)
        self.page.mouse.click(100, 249)
        self.page.wait_for_timeout(200)
        image = Image.open(io.BytesIO(self.page.screenshot()))
        self.assertEqual(len(workspace_recovery_controls(image)), 1, 'Legacy migration left multiple visible Recover actions')
        self.page.screenshot(path=str(ARTIFACTS/'one-recovery-legacy-guest-compacted.png'))

        self.signed_in()
        scope = self.accounts[-1]
        seed_legacy(scope, first)
        self.load(self.page)
        current = last_visited(461, scope)
        self.assertEqual(current['key'], scope+':legacy-newer')
        rows = drafts()
        self.assertEqual(len(rows), 2, 'Only the current recovery and isolated other-account entry should remain')
        self.assertEqual(next(r for r in rows if r['key'] == foreign), private_before,
                         'Compacting current-account recovery modified another account’s private snapshot')
        self.page.mouse.click(100, 249)
        self.page.wait_for_timeout(200)
        image = Image.open(io.BytesIO(self.page.screenshot()))
        self.assertEqual(len(workspace_recovery_controls(image)), 1, 'Another account recovery became visible')
        self.click_browser_recovery()
        self.page.wait_for_timeout(400)
        self.assertEqual(self.inspect()['document']['width'], 461)
        self.page.screenshot(path=str(ARTIFACTS/'one-recovery-current-account-restored.png'))

        # Logout revokes pending account writes, but a fresh guest session in
        # this same WASM instance must receive a new recovery permit.
        self.page.mouse.move(0, 0)
        before = Image.open(io.BytesIO(self.page.screenshot()))
        self.page.mouse.click(1400, 32)
        self.page.mouse.move(0, 0)
        self.page.wait_for_timeout(200)
        menu = Image.open(io.BytesIO(self.page.screenshot()))
        actions = native_overlay_actions(before, menu)
        self.assertEqual(len(actions), 1, 'The account menu should expose its Sign out action')
        rect = actions[0]
        self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
        self.page.mouse.move(0, 0)
        self.page.wait_for_timeout(200)
        confirmation = Image.open(io.BytesIO(self.page.screenshot()))
        confirmation.save(ARTIFACTS/'one-recovery-logout-confirmation.png')
        actions = native_overlay_actions(before, confirmation)
        self.assertEqual(len(actions), 2, 'Logout must show Download and Sign out controls')
        rect = actions[-1]
        with self.page.expect_response(lambda r: r.url == BASE+'/api/logout' and r.request.method == 'POST') as logged_out:
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
        self.assertTrue(logged_out.value.ok)
        self.page.wait_for_timeout(300)
        self.assertIsNone(self.inspect()['document'])
        self.assertEqual(drafts(), [], 'Sign out and clear must remove prior-account browser recovery')
        self.new(464, 304)
        last_visited(464)
        self.load(self.page)
        last_visited(464)
        self.assertFalse(any(r['key'].startswith(scope+':') for r in drafts()), 'An old account writer repopulated its cleared recovery')
        self.page.screenshot(path=str(ARTIFACTS/'one-recovery-new-guest-after-logout.png'))


    def test_45_initial_url_opens_once_when_connection_becomes_eligible(self):
        token = self.signed_in()
        self.new(481, 321)
        self.stroke()
        self.page.mouse.click(1320, 32)
        pid = self.wait_revision(1)['id']
        response = self.context.request.post(BASE+'/api/projects/'+pid+'/share', headers={'Origin': BASE})
        self.assertTrue(response.ok)
        share = response.json()['url'].split('share=')[1]
        observations = []

        for scenario in ['share-refresh', 'share-retry', 'project-retry', 'share-setup', 'project-setup']:
            if len(self.contexts) > 1:
                self.contexts[-1].close()
            with self.subTest(scenario=scenario):
                ctx = self.browser.new_context(viewport={'width': 1440, 'height': 960})
                self.contexts.append(ctx)
                project_link = scenario.startswith('project')
                if project_link:
                    ctx.add_cookies([{'name': 'pc_session', 'value': token, 'url': BASE, 'httpOnly': True, 'sameSite': 'Lax'}])
                page = ctx.new_page()
                page.on('pageerror', lambda error: self.errors.append(str(error)))
                phase = {'ready': not scenario.endswith('setup')}
                counts = {'config': 0, 'me': 0, 'open': 0}
                path = '/api/projects/'+pid if project_link else '/api/share/'+share

                def config(route):
                    counts['config'] += 1
                    route.fulfill(json={'cloud': phase['ready'], 'signIn': False})

                def me(route):
                    counts['me'] += 1
                    if scenario.endswith('retry') and counts['me'] == 1:
                        route.fulfill(status=503, json={'error': 'Synthetic initial session delay'})
                    elif not phase['ready'] or not project_link:
                        route.fulfill(status=401, json={'error': 'Sign in required'})
                    else:
                        route.continue_()

                def opened(route):
                    counts['open'] += 1
                    route.continue_()

                ctx.route(BASE+'/api/config', config)
                ctx.route(BASE+'/api/me', me)
                ctx.route(BASE+path, opened)
                query = '?project='+pid if project_link else '?share='+share
                page.goto(BASE+'/'+query, wait_until='domcontentloaded', timeout=90000)
                page.wait_for_function('typeof window.photocraftCommand === "function"', timeout=60000)
                page.wait_for_timeout(350)
                if scenario.endswith(('setup', 'retry')):
                    observations.append({'scenario': scenario, 'stage': 'not-ready', 'counts': dict(counts)})
                    (ARTIFACTS/'initial-url-request-counts.json').write_text(json.dumps(observations, indent=2))
                    page.screenshot(path=str(ARTIFACTS/f'initial-url-{scenario}-not-ready.png'))
                    self.assertEqual(counts['open'], 0, 'An ineligible initial URL was dispatched before connection readiness')
                    phase['ready'] = True
                deadline = time.monotonic()+8
                while time.monotonic() < deadline:
                    if self.inspect(page)['document'] is not None:
                        break
                    page.wait_for_timeout(100)
                document = self.inspect(page)['document']
                self.assertIsNotNone(document, 'The deferred initial URL never opened after the connection recovered')
                self.assertEqual((document['width'], document['height']), (481, 321))
                page.wait_for_timeout(3800)
                observations.append({'scenario': scenario, 'stage': 'settled', 'counts': dict(counts)})
                (ARTIFACTS/'initial-url-request-counts.json').write_text(json.dumps(observations, indent=2))
                page.screenshot(path=str(ARTIFACTS/f'initial-url-{scenario}-settled.png'))
                self.assertEqual(counts['open'], 1, 'Connection refresh replayed the URL and reopened the same document')
                if not project_link:
                    self.assertGreaterEqual(counts['config'], 3, 'The fixture did not exercise repeating guest connection refresh')
                self.assertEqual(self.inspect(page)['document']['width'], 481)
                ctx.close()


    def test_46_browser_quota_warning_does_not_pause_cloud_autosave(self):
        self.signed_in()
        observations = []
        cloud_failure = {'enabled': False, 'attempts': 0}

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def save(actions, name='Save'):
            controls = assert_header_geometry(self, picture('recovery-quota-save-control.png'), actions)
            rect = controls[actions.index(name)]
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.mouse.move(0, 0)

        def signature(document):
            return sorted((str(layer['id']), layer['name']) for layer in document['layers'])

        def metadata():
            response = self.context.request.get(BASE+'/api/projects/'+pid)
            self.assertTrue(response.ok, response.text())
            return response.json()

        def cloud_document(meta):
            chunks = []
            for part in range((meta['content']['bytes']+524287)//524288):
                response = self.context.request.get(BASE+f"/api/projects/{pid}/content?revision={meta['revision']}&part={part}")
                self.assertTrue(response.ok, response.text() if not response.ok else '')
                chunks.append(response.body())
            data = b''.join(chunks)
            self.assertEqual(hashlib.sha256(data).hexdigest(), meta['content']['sha256'])
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                return json.loads(archive.read('manifest.json'))['document']

        def recovery_rows():
            return self.page.evaluate('''async () => await new Promise((resolve,reject) => {
              const request=indexedDB.open('photocraft-studio-recovery');request.onerror=()=>reject(request.error);
              request.onsuccess=()=>{const db=request.result;const rows=[];
                const cursor=db.transaction('drafts').objectStore('drafts').openCursor();
                cursor.onsuccess=()=>{const row=cursor.result;if(row){rows.push({key:row.key,data:Array.from(row.value.data)});row.continue()}
                  else{db.close();resolve(rows)}};cursor.onerror=()=>{db.close();reject(cursor.error)};
              };
            })''')

        def recovery_matches(expected):
            rows = [r for r in recovery_rows() if r['key'].startswith(self.accounts[-1]+':')]
            if len(rows) != 1:
                return False
            with zipfile.ZipFile(io.BytesIO(bytes(rows[0]['data']))) as archive:
                document = json.loads(archive.read('manifest.json'))['document']
            return signature(document) == expected

        def wait_recovery(expected):
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                if recovery_matches(expected):
                    return
                self.page.wait_for_timeout(100)
            self.fail('The single browser snapshot did not catch up with the native document')

        def warning_ink(image, footer=False):
            pixels = image.load()
            top, bottom = ((image.height-27, image.height) if footer else (image.height-125, image.height-28))
            return sum(pixels[x, y][0] > pixels[x, y][2]+40 and pixels[x, y][1] > pixels[x, y][2]+30
                       for y in range(top, bottom) for x in range(min(image.width, 1000)))

        def wait_cloud_revision(expected, label):
            deadline = time.monotonic()+12
            while time.monotonic() < deadline:
                value = metadata()
                if value['revision'] >= expected:
                    break
                self.page.wait_for_timeout(100)
            value = metadata()
            picture(f'recovery-quota-{label}.png')
            observations.append({'phase': label, 'revision': value['revision'],
                                 'native_layers': signature(self.inspect()['document']),
                                 'quota_attempts': self.page.evaluate('window.__recoveryQuota.attempts'),
                                 'cloud_sha256': value.get('content', {}).get('sha256')})
            (ARTIFACTS/'recovery-quota-observations.json').write_text(json.dumps(observations, indent=2))
            self.assertGreaterEqual(value['revision'], expected,
                                    'An IndexedDB recovery failure paused healthy cloud autosave')
            return value

        def upload(route):
            if route.request.method == 'PUT' and cloud_failure['enabled']:
                cloud_failure['attempts'] += 1
                route.fulfill(status=503, json={'error': 'Synthetic cloud failure during local recovery warning'})
            else:
                route.continue_()

        self.new(477, 311)
        self.execute('shape.create', {'kind': 'ellipse', 'rect': [60, 40, 100, 90],
                                      'fill': '#9278ff', 'name': 'Initial persisted shape'})
        save(['More', 'Save'])
        pid = self.wait_revision(1)['id']
        baseline = signature(self.inspect()['document'])
        wait_recovery(baseline)
        self.assertEqual(signature(cloud_document(metadata())), baseline)
        clear = picture('recovery-quota-before.png')
        self.assertLess(warning_ink(clear), 20, 'The fixture already has a local recovery warning')

        self.page.evaluate('''() => {
          const original=IDBObjectStore.prototype.put;
          window.__recoveryQuota={enabled:true,attempts:0,restore:()=>{IDBObjectStore.prototype.put=original}};
          IDBObjectStore.prototype.put=function(...args){
            if(this.name==='drafts'&&window.__recoveryQuota.enabled){
              window.__recoveryQuota.attempts++;
              throw new DOMException('Synthetic browser storage quota exhausted','QuotaExceededError');
            }
            return original.apply(this,args);
          };
        }''')
        self.context.route('**/api/uploads/*/*', upload)
        try:
            # Exercise real native content, then let autosave run with no Save click.
            self.execute('layer.duplicate')
            self.execute('layer.renameLayer', {'name': 'Cloud survives local quota'})
            first_edit = signature(self.inspect()['document'])
            self.assertNotEqual(first_edit, baseline)
            self.page.wait_for_function('window.__recoveryQuota.attempts > 0', timeout=7000)
            saved = wait_cloud_revision(2, 'cloud-autosave-completed')
            self.assertEqual(saved['revision'], 2)
            self.assertEqual(signature(self.inspect()['document']), first_edit)
            self.assertEqual(signature(cloud_document(saved)), first_edit)
            self.assertFalse(recovery_matches(first_edit), 'The quota injection did not prevent local persistence')
            self.page.wait_for_timeout(350)
            warning = picture('recovery-quota-warning-with-saved-cloud.png')
            assert_header_geometry(self, warning, ['More', 'Comments', 'Save', 'Share'])
            self.assertGreater(warning_ink(warning), 30, 'Local recovery warning vanished after successful cloud autosave')
            recovery_action = recovery_warning_action(warning)
            self.assertGreaterEqual(recovery_action[3]-recovery_action[1], 27,
                                    'Retry recovery must render at least28px tall, allowing1px antialiasing')
            self.assertLess(warning_ink(warning, footer=True), 20, 'The healthy cloud save was presented as a cloud error')
            self.page.wait_for_timeout(2200)
            self.assertGreater(warning_ink(picture('recovery-quota-warning-still-visible.png')), 30)
            self.assertEqual(metadata()['revision'], 2, 'Recovery retries produced redundant cloud versions')

            # Inspect the actual narrow-screen warning rather than a mocked DOM.
            self.page.set_viewport_size({'width': 390, 'height': 844})
            self.page.wait_for_timeout(300)
            phone = picture('recovery-quota-warning-phone.png')
            self.assertGreater(warning_ink(phone), 20, 'Recovery warning is not visible on a phone viewport')
            recovery_action = recovery_warning_action(phone)
            self.assertGreaterEqual(recovery_action[3]-recovery_action[1], 27)
            self.assertGreaterEqual(recovery_action[0], 8)
            self.assertLessEqual(recovery_action[2], phone.width-8)
            self.assertLessEqual(recovery_action[3], phone.height-28,
                                 'Wrapped recovery action overlaps the cloud-status footer')
            self.assertEqual(signature(self.inspect()['document']), first_edit)
            self.page.set_viewport_size({'width': 1440, 'height': 960})
            self.page.wait_for_timeout(300)

            # Local recovery becoming healthy must not dismiss a real cloud error.
            cloud_failure['enabled'] = True
            with self.page.expect_response(lambda r: '/api/uploads/' in r.url
                                           and r.request.method == 'PUT' and r.status == 503, timeout=12000):
                self.execute('layer.duplicate')
                self.execute('layer.renameLayer', {'name': 'Cloud retry preserves this layer'})
            second_edit = signature(self.inspect()['document'])
            self.page.wait_for_timeout(350)
            both = picture('recovery-quota-cloud-and-local-failure.png')
            assert_header_geometry(self, both, ['More', 'Comments', 'Retry save', 'Share'])
            self.assertGreater(warning_ink(both), 30)
            self.assertGreater(warning_ink(both, footer=True), 30)
            footer = (0, both.height-27, 1000, both.height)
            self.page.evaluate('window.__recoveryQuota.enabled = false')
            wait_recovery(second_edit)
            self.page.wait_for_timeout(250)
            restored = picture('recovery-quota-local-restored-cloud-error-retained.png')
            self.assertLess(warning_ink(restored), 20, 'Local warning persisted after its snapshot succeeded')
            self.assertEqual(both.crop(footer).tobytes(), restored.crop(footer).tobytes(),
                             'Local recovery success replaced the cloud error')
            assert_header_geometry(self, restored, ['More', 'Comments', 'Retry save', 'Share'])
            self.assertEqual(signature(self.inspect()['document']), second_edit)
            self.assertEqual(metadata()['revision'], 2)

            cloud_failure['enabled'] = False
            save(['More', 'Comments', 'Retry save', 'Share'], name='Retry save')
            final = wait_cloud_revision(3, 'both-stores-restored')
            self.assertEqual(final['revision'], 3)
            self.assertEqual(signature(cloud_document(final)), second_edit)
            self.assertEqual(signature(self.inspect()['document']), second_edit)
            wait_recovery(second_edit)
            self.assertEqual(len(self.projects()), 1, 'Recovery or cloud retry created another project')
            self.assertGreaterEqual(cloud_failure['attempts'], 1)
        finally:
            self.page.evaluate('window.__recoveryQuota.restore()')
            self.context.unroute('**/api/uploads/*/*', upload)


    def test_47_imported_files_ignore_closed_cloud_bindings_and_late_results(self):
        token = self.signed_in()
        observations = []

        def picture(label):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/f'local-file-{label}.png').write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def save(actions, page=None):
            page = page or self.page
            controls = assert_header_geometry(self, Image.open(io.BytesIO(page.screenshot(scale='css'))), actions)
            rect = controls[actions.index('Save')]
            page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            page.mouse.move(0, 0)

        def metadata(pid):
            response = self.context.request.get(BASE+'/api/projects/'+pid)
            self.assertTrue(response.ok, response.text())
            return response.json()

        def wait_saved(pid, revision):
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                value = metadata(pid)
                if value['revision'] >= revision:
                    return value
                self.page.wait_for_timeout(100)
            self.fail(f'Project {pid} did not finish authorized save {revision}')

        for index, scenario in enumerate(['picker', 'drop', 'created', 'saved', 'save-error', 'synced', 'sync-error']):
            with self.subTest(scenario=scenario):
                if index:
                    self.load(self.page)
                self.new(489+index, 317)
                self.stroke()
                self.execute('shape.create', {'kind': 'ellipse', 'rect': [60, 40, 100, 90],
                                              'fill': '#9278ff', 'name': 'Original cloud shape'})
                original_document = self.inspect()['document']
                pixels = Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes()
                with self.page.expect_download() as downloaded:
                    self.command('ui.menu.invoke', {'id': 'file.saveAs', 'params': {'path': 'closed-cloud-document.pcraft'}})
                path = ARTIFACTS/f'closed-cloud-{scenario}.pcraft'
                downloaded.value.save_as(path)
                self.assertTrue(zipfile.is_zipfile(path), 'The local-copy fixture must preserve native document identity')
                native_bytes = path.read_bytes()
                held = []
                held_kind = ('created' if scenario == 'created' else
                             'commit' if scenario in ['saved', 'save-error'] else
                             'content' if scenario in ['synced', 'sync-error'] else None)

                def hold(route):
                    request = route.request
                    match = ((held_kind == 'created' and request.url == BASE+'/api/projects' and request.method == 'POST')
                             or (held_kind == 'commit' and request.url.endswith('/commit') and request.method == 'POST')
                             or (held_kind == 'content' and '/content?' in request.url and request.method == 'GET'))
                    if match and not held:
                        response = route.fetch()
                        self.assertTrue(response.ok, response.text() if not response.ok else '')
                        held.append((route, response))
                    else:
                        route.continue_()

                pattern = '**/api/**'
                if held_kind in ['created', 'commit']:
                    self.context.route(pattern, hold)
                if held_kind == 'created':
                    save(['More', 'Save'])
                    deadline = time.monotonic()+10
                    while not held and time.monotonic() < deadline:
                        self.page.wait_for_timeout(100)
                    self.assertEqual(len(held), 1, 'The real project creation was not held')
                    pid = held[0][1].json()['id']
                else:
                    with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
                        save(['More', 'Save'])
                    pid = created.value.json()['id']
                    wait_saved(pid, 1)
                    self.page.wait_for_timeout(250)

                other_context = None
                if held_kind == 'content':
                    self.context.route(pattern, hold)
                    other_context, other = self.context_page('?project='+pid, token=token)
                    self.execute('shape.create', {'kind': 'rect', 'rect': [220, 70, 80, 100],
                                                  'fill': '#ffbb22', 'name': 'Later cloud content'}, page=other)
                    save(['More', 'Comments', 'Save', 'Share'], page=other)
                    wait_saved(pid, 2)
                if held_kind and held_kind != 'created':
                    deadline = time.monotonic()+12
                    while not held and time.monotonic() < deadline:
                        self.page.wait_for_timeout(100)
                    self.assertEqual(len(held), 1, 'The authorized old save or sync did not reach its held response')

                self.execute('file.close')
                self.assertIsNone(self.inspect()['document'])
                self.page.wait_for_timeout(150)
                if scenario in ['drop', 'saved', 'sync-error']:
                    transfer = self.page.evaluate_handle('''([data,name]) => {
                        const transfer = new DataTransfer();
                        transfer.items.add(new File([new Uint8Array(data)], name, {type:'application/octet-stream'}));
                        return transfer;
                    }''', [list(native_bytes), path.name])
                    self.page.locator('canvas').dispatch_event('drop', {'dataTransfer': transfer})
                    transfer.dispose()
                else:
                    self.open_file(path)
                deadline = time.monotonic()+8
                while self.inspect()['document'] is None and time.monotonic() < deadline:
                    self.page.wait_for_timeout(100)
                imported = self.inspect()['document']
                self.assertIsNotNone(imported, 'The real local-file import never completed')
                self.assertEqual(imported['width'], original_document['width'])
                self.assertEqual(Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes(), pixels)
                if held_kind == 'content':
                    # Match the closed document's local revision using real,
                    # reversible edits. A DocId+revision check alone must not
                    # admit the old sync into this new local-file lifetime.
                    layer = original_document['layers'][0]
                    self.execute('layer.renameLayer', {'layer': layer['id'], 'name': 'Temporary import title'})
                    self.execute('layer.renameLayer', {'layer': layer['id'], 'name': layer['name']})
                    self.assertEqual(self.inspect()['document']['revision'], original_document['revision'])
                if held:
                    if scenario in ['save-error', 'sync-error']:
                        held[0][0].fulfill(status=503, json={'error': 'Synthetic response from a closed document'})
                    else:
                        held[0][0].fulfill(response=held[0][1])
                    self.page.wait_for_timeout(700)
                if held_kind:
                    self.context.unroute(pattern, hold)
                if other_context:
                    other_context.close()
                original = wait_saved(pid, 2 if held_kind == 'content' else 1)
                picture(scenario+'-after-late-response')
                self.assertEqual(Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes(), pixels,
                                 'A late cloud response replaced the imported local pixels')
                self.execute('shape.create', {'kind': 'rect', 'rect': [280, 180, 100, 70],
                                              'fill': '#11aa77', 'name': 'Independent local copy'})
                local = self.inspect()['document']
                self.page.wait_for_timeout(4400)  # Exercise the real cloud autosave deadline.
                after = metadata(pid)
                image = picture(scenario+'-before-explicit-save')
                observations.append({'scenario': scenario, 'original_id': pid,
                                     'before_revision': original['revision'], 'after_revision': after['revision'],
                                     'before_sha256': original['content']['sha256'], 'after_sha256': after['content']['sha256'],
                                     'native_layers': [layer['name'] for layer in self.inspect()['document']['layers']]})
                (ARTIFACTS/'local-file-binding-observations.json').write_text(json.dumps(observations, indent=2))
                self.assertEqual((after['revision'], after['content']['sha256']),
                                 (original['revision'], original['content']['sha256']),
                                 'A local file or late completion saved its edits into the closed cloud project')
                self.assertEqual([layer['name'] for layer in self.inspect()['document']['layers']],
                                 [layer['name'] for layer in local['layers']], 'Late sync replaced the imported local document')
                assert_header_geometry(self, image, ['More', 'Save'])
                if scenario in ['save-error', 'sync-error']:
                    warning = sum(r > b+40 and g > b+30 for r, g, b in image.crop((0, image.height-27, 1000, image.height)).getdata())
                    self.assertLess(warning, 20, 'A closed document response painted an error on the new local copy')
                with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
                    save(['More', 'Save'])
                copy_id = created.value.json()['id']
                self.assertNotEqual(copy_id, pid)
                copied = wait_saved(copy_id, 1)
                self.assertEqual(copied['width'], original_document['width'])
                final_original = metadata(pid)
                self.assertEqual((final_original['revision'], final_original['content']['sha256']),
                                 (original['revision'], original['content']['sha256']))
                picture(scenario+'-saved-new-project')

        # Closing invalidates a document's operation; merely switching tabs must
        # preserve it and keep a second save disabled until it completes.
        self.load(self.page)
        self.new(515, 329)
        self.stroke()
        first_pixels = Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes()
        pending = []

        def hold_first_save(route):
            if route.request.method == 'POST' and route.request.url.endswith('/commit') and not pending:
                response = route.fetch()
                self.assertTrue(response.ok, response.text())
                pending.append((route, response))
            else:
                route.continue_()

        self.context.route('**/api/uploads/*/commit', hold_first_save)
        with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
            save(['More', 'Save'])
        first_id = created.value.json()['id']
        first_saved = wait_saved(first_id, 1)
        self.assertEqual(len(pending), 1)
        self.return_to_workspace()
        self.page.mouse.click(100, 199)
        self.page.wait_for_timeout(250)
        rect = workspace_template_previews(picture('inactive-save-template-gallery'), has_quick_actions=False)[0]
        self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
        self.page.wait_for_timeout(650)
        second_document = self.inspect()['document']
        self.assertNotEqual(second_document['width'], 515)
        count = len(self.projects())
        second_creations = []
        self.page.on('request', lambda request: second_creations.append(request.url)
                     if request.url == BASE+'/api/projects' and request.method == 'POST' else None)
        save(['More', 'Save'])
        self.page.wait_for_timeout(500)
        picture('inactive-save-second-document-disabled')
        self.assertEqual(second_creations, [], 'A second save started while the first live document still owns a save operation')
        self.assertEqual(len(self.projects()), count)
        pending[0][0].fulfill(response=pending[0][1])
        self.page.wait_for_timeout(500)
        self.context.unroute('**/api/uploads/*/commit', hold_first_save)
        with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
            save(['More', 'Save'])
        second_id = created.value.json()['id']
        self.assertNotEqual(second_id, first_id)
        wait_saved(second_id, 1)
        self.page.wait_for_timeout(300)
        self.assertEqual(self.inspect()['document']['width'], second_document['width'])
        self.execute('document.activate', {'document': 0})
        self.page.wait_for_timeout(200)
        self.assertEqual(self.inspect()['document']['width'], 515)
        self.assertEqual(Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes(), first_pixels)
        assert_header_geometry(self, picture('inactive-save-completion-retains-binding'), ['More', 'Comments', 'Save', 'Share'])
        first_after = metadata(first_id)
        self.assertEqual((first_after['revision'], first_after['content']['sha256']),
                         (first_saved['revision'], first_saved['content']['sha256']))
        count = len(self.projects())
        self.execute('layer.renameLayer', {'name': 'Still bound after inactive completion'})
        save(['More', 'Comments', 'Save', 'Share'])
        wait_saved(first_id, 2)
        self.assertEqual(len(self.projects()), count, 'An inactive valid save lost its original project binding')
        self.assertEqual(metadata(second_id)['revision'], 1)
        observations.append({'scenario': 'inactive-still-open', 'first_id': first_id,
                             'second_id': second_id, 'first_final_revision': metadata(first_id)['revision']})
        (ARTIFACTS/'local-file-binding-observations.json').write_text(json.dumps(observations, indent=2))


if __name__ == '__main__':
    unittest.main(verbosity=2)
