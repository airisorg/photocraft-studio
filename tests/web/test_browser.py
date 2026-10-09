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
from collections import Counter

from PIL import Image
from playwright.sync_api import sync_playwright
import psycopg
from visual_assertions import (assert_header_geometry, assert_dialog_inside, assert_workspace_geometry,
                               workspace_controls, workspace_quick_actions, workspace_focus_changed,
                               workspace_template_previews, workspace_project_card, workspace_recovery_controls,
                               sharing_invitation_controls, assert_invitation_feedback_geometry, native_overlay_actions,
                               recovery_warning_action, session_auth_controls, preferences_general_paint,
                               assert_preferences_glyphs_complete, preferences_section_references,
                               compact_preferences_selector, native_preference_menu_choice)

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

    def context_page(self, query='', viewport=None, token=None, browser=None, device_scale_factor=1, expected_document=None):
        ctx = (browser or self.browser).new_context(viewport=viewport or {'width': 1440, 'height': 960}, accept_downloads=True, device_scale_factor=device_scale_factor)
        self.contexts.append(ctx)
        if token:
            ctx.add_cookies([{'name': 'pc_session', 'value': token, 'url': BASE, 'httpOnly': True, 'sameSite': 'Lax'}])
        page = ctx.new_page()
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.on('dialog', lambda dialog: dialog.accept())
        try:
            self.load(page, query, expected_document=expected_document)
        except BaseException:
            # unittest does not call tearDown when setUp fails. Do not leave a
            # live WASM app polling while subsequent tests try to diagnose it.
            ctx.close()
            self.contexts.remove(ctx)
            raise
        return ctx, page

    def load(self, page, query='', expected_document=None):
        # A collaborative editor keeps its live transport active. Readiness is
        # the actual native command bridge, rather than a quiet network.
        page.goto(BASE + '/' + query, wait_until='domcontentloaded', timeout=90000)
        page.wait_for_function('typeof window.photocraftCommand === "function"', timeout=60000)
        page.wait_for_timeout(400)
        if expected_document is not None:
            self.wait_opened_document(page, expected_document)

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

    def wait_rendered(self, inspect_image, label, page=None, timeout=10000):
        """Wait for the actual native control frame before sending its next input."""
        page = page or self.page
        deadline = time.monotonic()+timeout/1000
        failure = None
        while time.monotonic() < deadline:
            picture = page.screenshot(scale='css')
            try:
                result = inspect_image(Image.open(io.BytesIO(picture)))
                (ARTIFACTS/(label+'.png')).write_bytes(picture)
                return result
            except AssertionError as error:
                failure = error
            page.wait_for_timeout(50)
        (ARTIFACTS/(label+'-timeout.png')).write_bytes(picture)
        self.fail(f'{label} did not become ready: {failure}')

    def save_first_project(self):
        # Database revision visibility precedes delivery/processing of the save
        # ACK. The browser's post-save list is dispatched by Message::Saved.
        # Keep wait_revision a server-only helper for intentional delayed-ACK tests.
        with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'GET',
                                       timeout=20000) as listed:
            self.page.mouse.click(1320, 32)
            project = self.wait_revision(1)
        self.assertTrue(listed.value.ok)
        self.assertTrue(any(p['id'] == project['id'] and p['revision'] >= 1 for p in listed.value.json()))
        self.wait_rendered(lambda image: assert_header_geometry(self, image, ['More', 'Comments', 'Save', 'Share']),
                           'first-save-acknowledged')
        return project

    def open_more_copy(self, more, label, page=None):
        return self.open_more_target(more, label, 0, page)

    def open_more_target(self, more, label, row, page=None):
        page = page or self.page
        page.mouse.click((more[0]+more[2])/2, (more[1]+more[3])/2)
        # Require the first two native popup captions (Save a copy and Download
        # .pcraft) plus the requested caption, relative to the measured More
        # control. Input/response delivery does not mean the popup has painted.
        def ready(image):
            image = image.convert('RGB')
            captions = {}
            for index in sorted({0, 1, row}):
                rect = (more[0]+8, more[3]+5+index*30, more[0]+85, more[3]+29+index*30)
                crop = image.crop(rect)
                background = Counter(crop.getpixel((x,y)) for y in range(crop.height)
                                     for x in range(crop.width)).most_common(1)[0][0]
                ink = [(x,y) for y in range(crop.height) for x in range(crop.width)
                       if max(abs(a-b) for a,b in zip(crop.getpixel((x,y)), background)) > 20]
                self.assertGreater(len(ink), 70, 'The native More popup caption is not painted')
                self.assertLessEqual(max(y for x,y in ink)-min(y for x,y in ink), 16,
                                     'The sample does not isolate a native popup caption')
                captions[index] = ((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            return captions[row]
        return self.wait_rendered(ready, label, page)

    def open_sharing(self):
        controls = self.wait_rendered(lambda image: assert_header_geometry(self, image, ['More', 'Comments', 'Save', 'Share']),
                                      'sharing-header-ready')
        share = controls[3]
        with self.page.expect_response(lambda r: r.url.endswith('/members') and r.request.method == 'GET',
                                       timeout=10000) as members:
            self.page.mouse.click((share[0]+share[2])/2, (share[1]+share[3])/2)
        self.assertTrue(members.value.ok)
        expected_roles = 1+len(members.value.json())
        def ready(image):
            controls = sharing_invitation_controls(image)
            self.assertEqual(len(controls['roles']), expected_roles,
                             'The sharing form must paint its permission controls before input')
            return controls
        return self.wait_rendered(ready, 'sharing-dialog-ready')

    def auth_frame(self, label):
        data = self.page.screenshot(scale='css')
        (ARTIFACTS/f'session-{label}.png').write_bytes(data)
        return Image.open(io.BytesIO(data)).convert('RGB')

    def auth_wait(self, paused, label, timeout=10000):
        deadline = time.monotonic()+timeout/1000
        while time.monotonic() < deadline:
            try:
                controls = session_auth_controls(Image.open(io.BytesIO(self.page.screenshot(scale='css'))))
            except AssertionError:
                controls = None
            if bool(controls) == paused:
                self.auth_frame(label)
                return controls
            self.page.wait_for_timeout(100)
        self.auth_frame(label+'-timeout')
        self.fail(f'Session paused state did not become {paused}')

    def auth_click(self, action):
        controls = session_auth_controls(self.auth_frame('action'))
        rect = controls[['Sign in again', 'Check sign-in', 'Copy sign-in link'].index(action)]
        self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
        self.page.mouse.move(0, 0)

    def auth_save(self, bound=True, page=None):
        page = page or self.page
        actions = ['More', 'Comments', 'Save', 'Share'] if bound else ['More', 'Save']
        controls = assert_header_geometry(self, Image.open(io.BytesIO(page.screenshot(scale='css'))), actions)
        rect = controls[actions.index('Save')]
        page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
        page.mouse.move(0, 0)

    def auth_cookie(self, account):
        token = secrets.token_hex(32)
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                        (hashlib.sha256(token.encode()).hexdigest(), account))
        self.context.add_cookies([{'name':'pc_session','value':token,'url':BASE,'httpOnly':True,'sameSite':'Lax'}])
        return token

    def auth_revision(self, pid, revision):
        deadline = time.monotonic()+15
        while time.monotonic() < deadline:
            response = self.context.request.get(BASE+'/api/projects/'+pid)
            self.assertTrue(response.ok, response.text())
            value = response.json()
            if value['revision'] >= revision:
                return value
            self.page.wait_for_timeout(100)
        self.fail(f'Project {pid} did not reach revision {revision}')

    def auth_cloud_document(self, pid):
        meta = self.auth_revision(pid, 1)
        data = b''.join(self.context.request.get(BASE+f"/api/projects/{pid}/content?revision={meta['revision']}&part={part}").body()
                        for part in range((meta['content']['bytes']+524287)//524288))
        self.assertEqual(hashlib.sha256(data).hexdigest(), meta['content']['sha256'])
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            return json.loads(archive.read('manifest.json'))['document']

    def wait_opened_document(self, page, expected):
        # The native bridge is available before the asynchronous project download
        # and open finish. Expected-success fixtures require the actual native
        # document; delayed/denied/open-race tests deliberately omit this option.
        expected_layers = [(layer['id'], layer['name']) for layer in expected['layers']]
        deadline = time.monotonic()+10
        document = None
        while time.monotonic() < deadline:
            document = self.inspect(page)['document']
            if document is not None:
                self.assertEqual((document['width'], document['height']),
                                 (expected['width'], expected['height']))
                self.assertEqual([(layer['id'], layer['name']) for layer in document['layers']],
                                 expected_layers)
                self.assertIn(document['activeLayer'], [layer[0] for layer in expected_layers])
                return document
            page.wait_for_timeout(50)
        page.screenshot(path=str(ARTIFACTS/'session-project-open-timeout.png'))
        self.fail(f'The expected native cloud document did not open: {document}')

    def observe_cloud_body_reads(self, page=None):
        # gloo-net consumes JSON via Response.text and binary via arrayBuffer.
        # Observe the actual response methods, not a clone or response headers.
        (page or self.page).evaluate(r"""() => {
          if (window.__cloudBodyReads) return;
          const original = window.fetch.bind(window);
          window.__cloudBodyReads = [];
          window.fetch = async (...args) => {
            const input = args[0];
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            const method = (args[1]?.method || input.method || 'GET').toUpperCase();
            const response = await original(...args);
            if (method !== 'GET' || !/^\/api\/projects(?:\/[^/]+(?:\/content)?)?$/.test(url.pathname)) return response;
            const row = {path: url.pathname, status: response.status};
            window.__cloudBodyReads.push(row);
            for (const name of ['text', 'arrayBuffer']) {
              const read = response[name].bind(response);
              response[name] = async () => {
                const value = await read();
                row.bodyUsed = response.bodyUsed;
                row.bytes = typeof value === 'string' ? new TextEncoder().encode(value).length : value.byteLength;
                if (name === 'text') { try { row.value = JSON.parse(value); } catch (_) {} }
                row.consumedAt = performance.now();
                return value;
              };
            }
            return response;
          };
        }""")

    def cloud_body_count(self, page=None):
        return (page or self.page).evaluate('window.__cloudBodyReads.length')

    def wait_cloud_body(self, path, label, *, after=0, status=200, project=None, trashed=None, page=None):
        page = page or self.page
        predicate = """wanted => window.__cloudBodyReads.slice(wanted.after).some(row =>
          row.path === wanted.path && row.status === wanted.status && row.bodyUsed && row.consumedAt !== undefined &&
          (wanted.project === null || (Array.isArray(row.value) && row.value.some(project =>
            project.id === wanted.project && project.trashed === wanted.trashed))))"""
        wanted = {'path': path, 'after': after, 'status': status, 'project': project, 'trashed': trashed}
        deadline = time.monotonic()+10
        ready = page.evaluate(predicate, wanted)
        while not ready and time.monotonic() < deadline:
            page.wait_for_timeout(50)
            ready = page.evaluate(predicate, wanted)
        self.assertTrue(ready, 'The application did not consume the expected response body')
        # ui.inspect is drained in app.logic before cloud.update. Advance two
        # actual native frames after body completion before testing stale state.
        first = self.inspect(page)['frame']
        deadline = time.monotonic()+10
        current = first
        while current < first+2 and time.monotonic() < deadline:
            current = self.inspect(page)['frame']
        self.assertGreaterEqual(current, first+2, 'The native app did not process frames after the response body')
        rows = page.evaluate("""after => window.__cloudBodyReads.slice(after).map(({path, status, bytes, bodyUsed, consumedAt}) =>
          ({path, status, bytes, bodyUsed, consumedAt}))""", after)
        (ARTIFACTS/(label+'-body-read.json')).write_text(json.dumps(
            {'responses': rows, 'first_frame': first, 'assertion_frame': current}, indent=2)+'\n')

    def open_template_preview(self, rect, label):
        with self.page.expect_response(lambda response: '/templates/' in response.url
                                       and response.url.endswith('.pcraft') and response.request.method == 'GET') as opened:
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
        self.assertTrue(opened.value.ok, 'The actual template download failed')
        data = opened.value.body()
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            expected = json.loads(archive.read('manifest.json'))['document']
        # Engine inspection enumerates the native layer stack top-to-bottom,
        # the reverse of the file's bottom-to-top manifest order.
        layers = [(layer['id'], layer['name']) for layer in reversed(expected['layers'])]
        deadline = time.monotonic()+10
        document = None
        while time.monotonic() < deadline:
            document = self.inspect()['document']
            if (document is not None and document['name'] == expected['name']
                and (document['width'], document['height']) == (expected['size']['width'], expected['size']['height'])
                and [(layer['id'], layer['name']) for layer in document['layers']] == layers):
                self.assertIn(document['activeLayer'], [layer[0] for layer in layers])
                self.page.screenshot(path=str(ARTIFACTS/(label+'-opened.png')))
                return document
            self.page.wait_for_timeout(50)
        self.page.screenshot(path=str(ARTIFACTS/(label+'-timeout.png')))
        self.fail('The downloaded template did not become the expected active native document')

    def test_01_workspace_new_canvas_button(self):
        self.assertEqual(self.page.title(), 'PhotoCraft Studio — Browser Image Editor')
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
        more = self.wait_rendered(lambda image: assert_header_geometry(self, image, ['More', 'Save']),
                                  'native-download-header-ready')[0]
        target = self.open_more_target(more, 'native-download-menu-ready', 1)
        with self.page.expect_download() as event:
            self.page.mouse.click(*target)
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
        more = self.wait_rendered(lambda image: assert_header_geometry(self, image, ['More', 'Save']),
                                  'psd-export-header-ready')[0]
        target = self.open_more_target(more, 'psd-export-menu-ready', 3)
        with self.page.expect_download() as event:
            self.page.mouse.click(*target)
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
        self.load(self.page, '?project='+first['id'], expected_document=self.inspect()['document'])
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
        project = self.save_first_project()
        self.page.mouse.click(1320,32)
        project = self.wait_revision(1)
        other_context, second = self.context_page('?project='+project['id'], token=token,
                                                 expected_document=self.inspect()['document'])
        self.assertIsNotNone(self.inspect(second)['document'])
        # Both editors modify the same pixels from the same saved revision.
        other_context.set_offline(True)
        self.execute('edit.fill', {'color':'#0000ff'}, page=second)
        self.execute('edit.fill', {'color':'#ff0000'})
        self.wait_revision(2)
        with second.expect_response(lambda r: '/api/uploads/' in r.url and r.url.endswith('/commit')
                                    and r.request.method == 'POST' and r.status == 409, timeout=20000):
            other_context.set_offline(False)
            second.mouse.click(1258,32)
        def conflict_ready(image):
            controls = assert_header_geometry(self, image, ['More', 'Comments', 'Retry save', 'Share'])
            self.assertGreaterEqual(controls[2][2]-controls[2][0], 80, 'The conflict response has not painted Retry save')
            return controls
        more = self.wait_rendered(conflict_ready, 'conflict-ready', second)[0]
        local_copy = self.inspect(second)['document']
        self.assertTrue(local_copy['canUndo'])
        self.assertEqual(self.projects()[0]['revision'], 2, 'A stale browser overwrote the saved document')
        original = self.auth_revision(project['id'], 2)
        second.screenshot(path=str(ARTIFACTS/'conflict-preserved.png'))
        copy = self.open_more_copy(more, 'conflict-copy-menu-ready', second)
        second.mouse.click(*copy)
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
        copy_id = next(p['id'] for p in projects if p['id'] != project['id'])
        copy_meta = self.auth_revision(copy_id, 1)
        self.assertNotEqual(copy_meta['content']['sha256'], original['content']['sha256'])
        self.load(second, '?project='+copy_id, expected_document=local_copy)
        with second.expect_download() as exported:
            self.command('ui.menu.invoke', {'id':'file.export.quickExportAsPng'}, page=second)
        copy_path = ARTIFACTS/'conflict-copy-reloaded.png'
        exported.value.save_as(copy_path)
        self.assertIsNone(exported.value.failure())
        copy_pixels = Image.open(copy_path).convert('RGB')
        self.assertEqual(copy_pixels.size, (320, 240))
        self.assertEqual(copy_pixels.tobytes(), bytes((0, 0, 255))*320*240,
                         'The reloaded saved copy lost the conflicting browser pixels')
        original_pixels = Image.open(self.download('file.export.quickExportAsPng')).convert('RGB')
        self.assertEqual(original_pixels.size, (320, 240))
        self.assertEqual(original_pixels.tobytes(), bytes((255, 0, 0))*320*240)
        original_after = self.auth_revision(project['id'], 2)
        self.assertEqual(original_after['revision'], 2, 'Saving a copy advanced the original project')
        self.assertEqual(original_after['content']['sha256'], original['content']['sha256'])

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
        other_context,second=self.context_page('?project='+project['id'],token=token,
                                              expected_document=self.inspect()['document'])
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
        self.load(second,'?project='+project['id'], expected_document=self.inspect(second)['document'])
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



    def open_project_card_menu(self, card, label, page=None):
        """Open an owned completed-card menu and require all five painted rows."""
        page = page or self.page
        page.mouse.click(card['card'][2]-32, card['preview'][3]+28)
        def ready(image):
            # The owned completed-card popup has five native captions.
            # Measure their painted ink inside the popup, using the
            # pre-popup card/preview geometry (the popup can overlap
            # the card's outline). A closed card has no five rows.
            pixels = image.convert('RGB').load()
            left, right = card['card'][2]-38, card['card'][2]+68
            top, bottom = card['preview'][3]+50, card['preview'][3]+244
            rows = [y for y in range(top, bottom)
                    if sum(max(pixels[x, y]) < 110 for x in range(left, right)) >= 3]
            bands = []
            for y in rows:
                if not bands or y-bands[-1][-1] > 2:
                    bands.append([y])
                else:
                    bands[-1].append(y)
            self.assertEqual(len(bands), 5, 'Completed-card menu captions are not all painted')
            centers = []
            for band in bands:
                self.assertTrue(8 <= band[-1]-band[0]+1 <= 20, 'Menu caption is clipped or merged')
                centers.append((band[0]+band[-1])/2)
            self.assertTrue(all(32 <= b-a <= 44 for a, b in zip(centers, centers[1:])),
                            'Completed-card menu rows overlap or are missing')
            self.assertTrue(card['preview'][3]+206 < centers[-1] < card['preview'][3]+238,
                            'Trash/Restore caption is outside its click target')
            return [(band[0], band[-1]+1) for band in bands]

        return self.wait_rendered(ready, label, page)

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
                card = self.wait_rendered(workspace_project_card, f'project-menu-{field}-{expected}-card')
                self.open_project_card_menu(card, f'project-menu-{field}-{expected}')
                with self.page.expect_response(lambda r: r.url==BASE+'/api/projects/'+project['id'] and r.request.method=='PATCH') as event:
                    self.page.mouse.click(card['card'][2]+5, card['preview'][3]+row)
                self.assertTrue(event.value.ok)
                self.page.wait_for_timeout(150)
                state = self.context.request.get(BASE+'/api/projects').json()
                current = next(p for p in state if p['id']==project['id'])
                self.assertEqual(current[field],expected)
        self.page.mouse.click(100,249)
        self.page.wait_for_timeout(150)
        card = self.wait_rendered(workspace_project_card, 'project-details-card')
        self.open_project_card_menu(card, 'project-details-menu')
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
        project = self.save_first_project()
        self.open_sharing()
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
        more = self.wait_rendered(lambda image: assert_header_geometry(self, image, ['More', 'Comments', 'Save', 'Share']),
                                  'history-header-ready')[0]
        target = self.open_more_target(more, 'history-menu-ready', 4)
        with self.page.expect_response(lambda r: r.url.endswith('/versions')) as event:
            self.page.mouse.click(*target)
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
        self.save_first_project()
        self.open_sharing()
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
        project=self.save_first_project()
        self.page.mouse.click(1320,32)
        project=self.wait_revision(1)
        path=BASE+'/api/projects/'+project['id']+'/members'
        self.assertTrue(self.context.request.put(path,headers={'Origin':BASE},
            data={'email':'collaborator@example.invalid','role':'edit'}).ok)
        self.page.wait_for_timeout(350)
        self.open_sharing()
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
                card = self.wait_rendered(workspace_project_card, f'first-save-{outcome}-incomplete-card')
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
                    card = self.wait_rendered(workspace_project_card, 'first-save-reopened-card')
                    # The standard completed-card menu follows its preview/title row.
                    menu_bands = self.open_project_card_menu(card, 'first-save-completed-menu')
                    (ARTIFACTS/'first-save-completed-menu-bands.json').write_text(json.dumps(menu_bands)+'\n')
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
        with self.page.expect_response(lambda response: response.url == member_path
                                       and response.request.method == 'GET') as refreshed:
            held[0].fulfill(json={'ok': True})
        self.assertTrue(refreshed.value.ok)
        self.assertTrue(any(member['email'] == email and member['role'] == 'edit'
                            for member in refreshed.value.json()))

        previous_geometry = None

        def refreshed_members(image):
            nonlocal previous_geometry
            controls = sharing_invitation_controls(image)
            self.assertEqual(len(controls['roles']), 2,
                             'The refreshed member role has not been painted yet')
            geometry = (controls['email'], controls['send'], controls['roles'])
            previous, previous_geometry = previous_geometry, geometry
            self.assertEqual(geometry, previous, 'The sharing dialog is still recentering around its new member row')
            return image.convert('RGB'), controls

        # The stale invite's first GET is deliberately ignored. Returning to A
        # starts a fresh current-generation GET before its native row is painted.
        current, controls = self.wait_rendered(refreshed_members, 'invitation-returned-a-old-result-ignored')
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
                    other_context, second = self.context_page('?project='+pid, token=token,
                                                              expected_document=self.inspect()['document'])
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
                self.load(self.page, '?project='+pid, expected_document=self.inspect()['document'])
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
            self.open_template_preview(rect, 'same-session-template')
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
                autosave_failures = []
                upload_path = BASE+f'/api/projects/{pid}/uploads'

                def reject_original_autosave(route):
                    if route.request.method == 'POST':
                        autosave_failures.append(route.request.post_data_json)
                        route.fulfill(status=503, content_type='application/json',
                                      body=json.dumps({'error': 'Synthetic recovery fixture upload outage'}))
                    else:
                        route.continue_()

                if source == 'recovery':
                    self.page.route(upload_path, reject_original_autosave)
                try:
                    if source == 'recovery':
                        self.execute('edit.fill', {'color': '#cc5577'})
                        # Recovery must contain an unsaved edit regardless of the
                        # cloud autosave interval. Fail only this original's upload.
                        self.page.wait_for_timeout(2300)
                        self.assertTrue(autosave_failures, 'The fixture did not exercise a failed autosave')
                        unchanged = self.context.request.get(BASE+'/api/projects/'+pid).json()
                        self.assertEqual((unchanged['revision'], unchanged['content']['sha256']),
                                         (original['revision'], original['content']['sha256']))
                        (ARTIFACTS/'same-session-recovery-autosave-failure.json').write_text(json.dumps({
                            'project': pid, 'failed_uploads': len(autosave_failures),
                            'original_revision': original['revision'], 'original_sha256': original['content']['sha256'],
                            'revision_before_close': unchanged['revision'], 'sha256_before_close': unchanged['content']['sha256']}, indent=2))
                    expected = self.inspect()['document']
                    pixels = Image.open(self.download('file.export.quickExportAsPng')).convert('RGBA').tobytes()
                    # Use the existing native close command while keeping Cloud and
                    # its old binding alive in this same WASM session.
                    self.execute('file.close')
                    self.assertIsNone(self.inspect()['document'])
                finally:
                    if source == 'recovery':
                        self.page.unroute(upload_path, reject_original_autosave)
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
        expected_b = self.inspect()['document']
        self.assertEqual((expected_b['width'], expected_b['height']), (422, 280))

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
            rect = self.wait_rendered(workspace_project_card, 'project-open-search-card')['preview']
            rect = workspace_project_card(Image.open(io.BytesIO(self.page.screenshot())))['preview']
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.wait_for_timeout(150)
            return rect

        for outcome in ['success', 'error']:
            with self.subTest(late_response=outcome):
                self.context.unroute(BASE+'/api/projects/'+projects[0])
                self.load(self.page)
                self.observe_cloud_body_reads()
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
                self.wait_cloud_body('/api/projects/'+projects[0]+('/content' if outcome == 'success' else ''),
                                     f'project-open-{outcome}-late-a', after=body_count,
                                     status=200 if outcome == 'success' else 503)
                self.wait_opened_document(self.page, expected_b)
                self.page.wait_for_timeout(650)
                after = picture(f'project-open-{outcome}-a-ignored.png')
                self.assertEqual(self.inspect()['document']['width'], 422, 'An older open replaced the more recent project choice')
                footer = (0, before.height-27, 1100, before.height)
                self.assertEqual(before.crop(footer).tobytes(), after.crop(footer).tobytes(),
                                 'A superseded open changed the current project status')

    def capture_project_list_snapshot(self, route):
        # Overridden only by the adversarial readiness replay; normal journeys
        # return the real response immediately, without a synthetic delay.
        return route.fetch()

    def test_43_stale_project_lists_cannot_undo_visible_trash_or_restore(self):
        self.signed_in()
        self.new(455, 288)
        self.page.mouse.click(1320, 32)
        pid = self.wait_revision(1)['id']
        self.return_to_workspace()
        self.page.mouse.click(100, 249)
        self.page.wait_for_timeout(200)
        self.observe_cloud_body_reads()
        held, stale = [], []
        phase = {'hold': False}

        def listing(route):
            if phase['hold']:
                phase['hold'] = False
                stale.append(self.capture_project_list_snapshot(route).json())
                held.append(route)
            else:
                route.continue_()

        self.context.route(BASE+'/api/projects', listing)

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/name).write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def empty_workspace(image):
            # The two native empty-state captions occupy the first panel below
            # search. A pending-list spinner has only one caption and must not
            # satisfy the no-resurrection assertion merely by hiding all cards.
            search = workspace_controls(image)['search']
            crop = image.convert('RGB').crop((search[0]+20, search[3]+24,
                                               image.width-36, search[3]+120))
            pixels = crop.load()
            rows = [y for y in range(crop.height)
                    if sum(max(pixels[x, y]) < 170 for x in range(crop.width)) >= 3]
            bands = []
            for y in rows:
                if not bands or y-bands[-1][-1] > 2:
                    bands.append([y])
                else:
                    bands[-1].append(y)
            self.assertEqual(len(bands), 2, 'The completed native empty-state title and help must be painted')
            for band in bands:
                self.assertGreaterEqual(len(band), 8)
                self.assertLessEqual(band[-1]-band[0]+1, 24)
            with self.assertRaises(AssertionError):
                workspace_project_card(image)
            return crop.tobytes()

        def menu_action(offset):
            card = self.wait_rendered(workspace_project_card, 'project-list-menu-card')
            self.open_project_card_menu(card, f'project-list-menu-{offset}')
            with self.page.expect_response(lambda r: r.url == BASE+'/api/projects/'+pid and r.request.method == 'PATCH') as changed:
                self.page.mouse.click(card['card'][2]+5, card['preview'][3]+offset)
            self.assertTrue(changed.value.ok)
            self.page.wait_for_timeout(150)

        for index, trashed in enumerate([True, False]):
            with self.subTest(trashed=trashed):
                if not trashed:
                    self.load(self.page)  # Give this subcase its authoritative current trash state.
                    self.observe_cloud_body_reads()
                    self.page.mouse.click(100, 399)
                    self.page.wait_for_timeout(150)
                phase['hold'] = True
                menu_action(185)  # Star/unstar starts a real list refresh with the old trash state.
                # PATCH headers do not mean its following GET has finished
                # route.fetch()/JSON capture. Wait for the actual held snapshot.
                capture_deadline = time.monotonic()+10
                while len(held) < index+1 or len(stale) < index+1:
                    if time.monotonic() >= capture_deadline:
                        self.fail(f'Project list capture timed out: held={len(held)}, '
                                  f'snapshots={len(stale)}, expected={index+1}, '
                                  f'capture_requested={not phase["hold"]}')
                    self.page.wait_for_timeout(25)
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


    def blocked_sign_in_warning(self, image):
        # Actual native Studio caption mask, in CSS pixels at the desktop fixture
        # size: "Download your other unsaved documents before signing in. Browser
        # recovery keeps only the last visited document." This matches the whole
        # glyph shape, not merely an unrelated orange notice. It is not OCR;
        # different fonts/layouts fail closed. Allow one pixel of raster alignment.
        import base64
        import zlib
        packed = (
            'eNrdWItuAyEMs///pyetJbYD3LFHtUc10RY4kjjGyUqS2Lx4NLXcdLTvxir58We6ixdn9CXeBUvyC2Gdun+/mZdI89bK9gl+0GNe'
            'PPMfuPO9GX4ReV7HHR4Y/Bp33lFhocPnFXusxeh8e+yhPtfUOODxVp/HTh/zNN1t2g7f9zwRcZBthZleD3LL/LOox/cxw+SOGXx+'
            'b5543FhEYCBZEnqw/nBO2oPdItIAEQkT4AntDNsF4PTQBkjPMf4KHxshfmkclK0vmibKOV+35a0JKj1ghbI9CPQUN2/sasitXPKI'
            'ApaoWbowcnEX62LR4tDp7l6EzEZb89ulMMNvfqF7F7BkTnsyZsA93yx7aJ5xGCuGw/wLDMzHPECk7d7RrtOQo7yJdRP88JTGDFb8'
            'RYiJAixBkaMegu9ccceZBr9gOtxjrYgMGN1cGTChiGWrkeaZzKpmuUBKj1SGXAcqzwvAcQ945477SperOovo3MEZd6xAB3fahY8D'
            'TrnTVAZs+9DhYDQNn+EOGJJCyPQqVkzA5HyFJq1rQPSiU4o1cUdeqsZYPWBoABOsQ+5kFh0GhCDL3lxQYt+mZk1KOWtllPm8xMua'
            '5Tu4OM4kuRe2YsR1zcJ1zerFZoFC6M5cUaPgBd2bKKucsddhph5FadzULFh7QszwoZX1RdaXNcu7XKeRdN4bV3qb7S2rtXRNx4xw'
            '3mF6Rwgc9MoYIptNd2/XW9Pnjd+2Vw7uZK/sWZE8RPefvbLlahqRjWhcuZNeeeJOOg1Ep+BiFm16s7brlZeAs7cdr/2n8ne/+JPx'
            '8ZMA888mgP+GOstf+173G+A5jvwLCXgDih8GFQ=='
        )
        image = image.convert('RGB')
        pixels = image.load()
        points = {(x,y) for y in range(image.height-27,image.height-1) for x in range(min(700,image.width))
                  if pixels[x,y][0] > 150 and pixels[x,y][1] > 70
                  and pixels[x,y][0] > pixels[x,y][2]*1.5 and pixels[x,y][1] > pixels[x,y][2]*1.2}
        self.assertTrue(points, 'The blocked-sign-in warning is absent')
        left,top,right,bottom = min(x for x,y in points),min(y for x,y in points),max(x for x,y in points)+1,max(y for x,y in points)+1
        self.assertLessEqual(abs(right-left-572), 2, 'The complete blocked-sign-in caption is not visible')
        self.assertLessEqual(abs(bottom-top-10), 1, 'The blocked-sign-in caption has unexpected line height')
        actual = {(x-left,y-top) for x,y in points}
        bits = zlib.decompress(base64.b64decode(packed))
        expected = {(x,y) for y in range(10) for x in range(572) if bits[y*572+x]}
        scores = []
        for dy in (-1,0,1):
            for dx in (-1,0,1):
                shifted = {(x+dx,y+dy) for x,y in actual}
                scores.append(len(shifted & expected)/len(shifted | expected))
        self.assertGreaterEqual(max(scores), .75, 'The warning is not the blocked-sign-in caption')
        return {'rect': [left,top,right,bottom], 'glyph_iou': max(scores)}

    def open_guest_sign_in(self, label):
        controls = self.wait_rendered(lambda image: assert_header_geometry(self, image, ['More', 'Save']),
                                      label+'-header-ready')
        self.page.mouse.move(0, 0)
        before = Image.open(io.BytesIO(self.page.screenshot(scale='css')))
        avatar = controls[-1]
        self.page.mouse.click((avatar[0]+avatar[2])/2, (avatar[1]+avatar[3])/2)
        self.page.mouse.move(0, 0)
        def ready(image):
            actions = native_overlay_actions(before, image)
            self.assertEqual(len(actions), 1, 'The native guest menu must show its Google action')
            left,top,right,bottom = actions[0]
            self.assertAlmostEqual(right-left, 260, delta=2)
            self.assertAlmostEqual(bottom-top, 42, delta=2)
            crop = image.convert('RGB').crop((left+8,top+4,right-8,bottom-4))
            background = Counter(crop.getdata()).most_common(1)[0][0]
            ink = [(x,y) for y in range(crop.height) for x in range(crop.width)
                   if max(abs(a-b) for a,b in zip(crop.getpixel((x,y)), background)) > 35]
            self.assertGreater(len(ink), 70, 'The native Google action caption is not painted')
            self.assertLessEqual(max(y for x,y in ink)-min(y for x,y in ink), 16)
            return ((left+right)/2, (top+bottom)/2)
        return self.wait_rendered(ready, label+'-menu-ready')

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
        redirects = []
        self.context.route('**/auth/login', lambda route: (redirects.append(route.request.url),
                           route.fulfill(content_type='text/html', body='<h1>Unexpected sign-in redirect</h1>')))
        with self.page.expect_response(lambda response: response.url == BASE+'/api/config'
                                       and response.ok and response.json().get('signIn') is True,
                                       timeout=10000):
            self.context.route('**/api/config', lambda route: route.fulfill(json={'cloud': True, 'signIn': True}))
        before = Image.open(io.BytesIO(self.page.screenshot(scale='css')))
        before.save(ARTIFACTS/'one-recovery-sign-in-before.png')
        with self.assertRaises(AssertionError):
            self.blocked_sign_in_warning(before)
        target = self.open_guest_sign_in('one-recovery-sign-in')
        self.page.mouse.click(*target)
        warning = self.wait_rendered(self.blocked_sign_in_warning, 'one-recovery-sign-in-blocked-ready')
        (ARTIFACTS/'one-recovery-sign-in-warning.json').write_text(json.dumps(warning, indent=2)+'\n')
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
                    other_context, other = self.context_page('?project='+pid, token=token,
                                                             expected_document=self.inspect()['document'])
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
        self.open_template_preview(rect, 'inactive-save-template')
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


    def test_48_session_renewal_keeps_two_dirty_native_documents_open(self):
        self.signed_in()
        account = self.accounts[-1]
        projects = []
        for width in [501, 502]:
            self.new(width, 321)
            self.stroke()
            with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
                self.auth_save(bound=False)
            pid = created.value.json()['id']
            self.auth_revision(pid, 1)
            self.page.wait_for_timeout(300)
            projects.append(pid)
        self.db.execute('DELETE FROM photocraft.sessions WHERE account_id=%s', (account,))
        expected = []
        for index in range(2):
            self.execute('document.activate', {'document': index})
            self.execute('layer.duplicate')
            self.execute('layer.renameLayer', {'name': f'Unsaved document {index+1}'})
            self.execute('select.all')
            expected.append(self.inspect()['document'])
        self.auth_wait(True, 'two-documents-expired')
        self.assertEqual(len(self.inspect()['session']['documents']), 2)
        for width, height in [(1440, 960), (390, 844)]:
            self.page.set_viewport_size({'width': width, 'height': height})
            self.page.wait_for_timeout(200)
            controls = session_auth_controls(self.auth_frame(f'expired-{width}'))
            for box in controls:
                self.assertGreaterEqual(box[3]-box[1], 35)
                self.assertGreaterEqual(box[0], 15)
                self.assertLessEqual(box[2], width-15)
            for previous, current in zip(controls, controls[1:]):
                if current[1] == previous[1]:
                    self.assertGreaterEqual(current[0]-previous[2], 7, 'Session actions overlap')
                else:
                    self.assertGreaterEqual(current[1]-previous[3], 7, 'Wrapped session actions overlap')
        self.page.set_viewport_size({'width': 1440, 'height': 960})
        self.page.wait_for_timeout(200)
        blocked_requests = []
        self.page.on('request', lambda r: blocked_requests.append({'method':r.method,'path':urlparse(r.url).path})
                     if '/api/projects' in r.url or '/api/uploads' in r.url else None)
        self.page.wait_for_timeout(1800)
        self.assertEqual(blocked_requests, [], 'Expired session kept sending authenticated background requests')
        original_url = self.page.url
        self.context.route('**/auth/login', lambda route: route.fulfill(content_type='text/html', body='<title>Synthetic sign-in</title><p>Local test identity only</p>'))
        with self.context.expect_page() as opened:
            self.auth_click('Sign in again')
        popup = opened.value
        popup.wait_for_load_state('domcontentloaded')
        self.assertEqual(urlparse(popup.url).path, '/auth/login')
        self.assertEqual(self.page.url, original_url, 'Sign-in navigated away from the live editor')
        self.auth_cookie(account)
        popup.close()
        self.auth_click('Check sign-in')
        self.auth_wait(False, 'same-account-resumed')
        for index, pid in enumerate(projects):
            self.execute('document.activate', {'document': index})
            self.assertEqual(self.inspect()['document'], expected[index], 'Session renewal lost native edits, selection or undo history')
            saved = self.auth_revision(pid, 2)
            self.assertEqual(saved['revision'], 2)
            self.assertCountEqual([layer['name'] for layer in self.auth_cloud_document(pid)['layers']],
                                  [layer['name'] for layer in expected[index]['layers']])
        self.assertEqual(len(self.projects()), 2, 'Renewal created new cloud destinations')
        self.auth_frame('two-documents-saved-after-renewal')
        (ARTIFACTS/'session-two-documents.json').write_text(json.dumps({'project_ids':projects,
            'widths':[doc['width'] for doc in expected], 'native_revisions':[doc['revision'] for doc in expected],
            'undo_preserved':all(doc['canUndo'] for doc in expected)}, indent=2))

        # Renewing the same identity must also refresh a real shared-project role.
        collaborator, token = str(uuid.uuid4()), secrets.token_hex(32)
        self.accounts.append(collaborator)
        email = collaborator+'@example.invalid'
        self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                        (collaborator, email, 'Renewal collaborator'))
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                        (hashlib.sha256(token.encode()).hexdigest(), collaborator))
        pid = projects[0]
        member_path = BASE+'/api/projects/'+pid+'/members'
        self.assertTrue(self.context.request.put(member_path, headers={'Origin':BASE},
                                                data={'email':email,'role':'edit'}).ok)
        owner_context, owner_page = self.context, self.page
        other_context, other = self.context_page('?project='+pid, token=token, expected_document=expected[0])
        self.context, self.page = other_context, other
        try:
            self.db.execute('DELETE FROM photocraft.sessions WHERE account_id=%s', (collaborator,))
            self.execute('layer.renameLayer', {'name':'Retained after permission downgrade'})
            retained = self.inspect()['document']
            self.auth_wait(True, '48-collaborator-expired')
            self.assertTrue(owner_context.request.put(member_path, headers={'Origin':BASE},
                                                      data={'email':email,'role':'view'}).ok)
            original_meta = owner_context.request.get(BASE+'/api/projects/'+pid).json()
            self.auth_cookie(collaborator)
            self.auth_click('Check sign-in')
            self.auth_wait(False, '48-view-permission-renewed')
            self.assertEqual(self.inspect()['document'], retained)
            self.page.wait_for_timeout(4200)
            self.assertEqual(owner_context.request.get(BASE+'/api/projects/'+pid).json()['revision'], original_meta['revision'])
            with self.page.expect_download() as exported:
                self.command('ui.menu.invoke', {'id':'file.saveAs','params':{'path':'permission-copy.pcraft'}})
            native = ARTIFACTS/'session-permission-copy.pcraft'
            exported.value.save_as(native)
            controls = assert_header_geometry(self, self.auth_frame('48-save-copy-available'), ['More','Comments','Save'])
            box = controls[2]
            with self.page.expect_response(lambda r:r.url==BASE+'/api/projects' and r.request.method=='POST') as copied:
                self.page.mouse.click((box[0]+box[2])/2, (box[1]+box[3])/2)
            copy_id = copied.value.json()['id']
            self.assertNotEqual(copy_id, pid)
            copy_meta = self.auth_revision(copy_id, 1)
            data = b''.join(self.context.request.get(BASE+f"/api/projects/{copy_id}/content?revision=1&part={part}").body()
                            for part in range((copy_meta['content']['bytes']+524287)//524288))
            self.assertEqual(hashlib.sha256(data).hexdigest(), copy_meta['content']['sha256'])
            (ARTIFACTS/'session-cloud-permission-copy.pcraft').write_bytes(data)
            # File > Save embeds a composite preview; cloud save omits it. Compare
            # the entire native document and every non-preview payload byte.
            with zipfile.ZipFile(native) as local, zipfile.ZipFile(io.BytesIO(data)) as cloud:
                self.assertEqual(json.loads(local.read('manifest.json'))['document'],
                                 json.loads(cloud.read('manifest.json'))['document'])
                payloads = lambda z: {name:z.read(name) for name in z.namelist()
                                      if name not in {'manifest.json','thumb.png','composite/preview.png'}}
                self.assertEqual(payloads(local), payloads(cloud))
            self.assertEqual(owner_context.request.get(BASE+'/api/projects/'+pid).json()['content']['sha256'], original_meta['content']['sha256'])
            (ARTIFACTS/'session-permission-downgrade.json').write_text(json.dumps({
                'original_project':pid,'original_revision':original_meta['revision'],
                'copy_project':copy_id,'native_sha256':copy_meta['content']['sha256'],
                'retained_native_revision':retained['revision']}, indent=2))
        finally:
            other_context.close()
            self.context, self.page = owner_context, owner_page

    def test_49_session_renewal_rejects_switched_account_and_stale_auth_responses(self):
        token_a = self.signed_in()
        account_a = self.accounts[-1]
        account_b = str(uuid.uuid4())
        self.accounts.append(account_b)
        self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                        (account_b, account_b+'@example.invalid', 'Separate browser account'))
        self.new(417, 283)
        self.stroke()
        self.execute('shape.create', {'kind': 'rect', 'rect': [30, 40, 80, 50],
                                     'fill': '#ffaa00', 'name': 'Private account A layer'})
        observations, requests, held_me, held_logout, held_presence = [], [], [], [], []
        phase = {'name': 'ordinary503', 'reject_create': True, 'hold_me': False}

        def wait(predicate, description, timeout=10000):
            deadline = time.monotonic()+timeout/1000
            while time.monotonic() < deadline:
                if predicate():
                    return
                self.page.wait_for_timeout(100)
            self.fail(description)

        def signature():
            doc = self.inspect()['document']
            self.assertIsNotNone(doc)
            return (doc['revision'], doc['width'], doc['height'],
                    [(layer['id'], layer['name'], layer['kind']) for layer in doc['layers']])

        def counts(account):
            return [self.db.execute(sql, (account,)).fetchone()[0] for sql in [
                'SELECT count(*) FROM photocraft.projects WHERE owner_id=%s',
                'SELECT count(*) FROM photocraft.uploads WHERE author_id=%s',
                'SELECT count(*) FROM photocraft.comments WHERE author_id=%s']]

        def recovery_keys():
            return self.page.evaluate('''async () => await new Promise((resolve,reject) => {
              const request=indexedDB.open('photocraft-studio-recovery');
              request.onsuccess=()=>{const db=request.result;
                const read=db.transaction('drafts').objectStore('drafts').getAllKeys();
                read.onsuccess=()=>{resolve(read.result);db.close()};read.onerror=()=>reject(read.error)};
              request.onerror=()=>reject(request.error);
            })''')

        def trace(label):
            observations.append({'phase': label, 'document': signature(),
                                 'account_a_rows': counts(account_a), 'account_b_rows': counts(account_b),
                                 'recovery_keys': recovery_keys()})
            (ARTIFACTS/'session-account-switch-observations.json').write_text(json.dumps(
                {'observations': observations, 'requests': requests}, indent=2))

        def projects(route):
            request = route.request
            entry = {'phase': phase['name'], 'method': request.method, 'path': '/api/projects',
                     'expected_account': request.header_value('x-photocraft-account')}
            if request.method == 'POST' and phase['reject_create']:
                phase['reject_create'] = False
                entry['status'] = 503
                requests.append(entry)
                route.fulfill(status=503, json={'error': 'Synthetic temporary storage failure'})
            else:
                response = route.fetch()
                entry['status'] = response.status
                requests.append(entry)
                route.fulfill(response=response)

        def identity(route):
            if phase['hold_me']:
                phase['hold_me'] = False
                response = route.fetch()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.json()['id'], account_a)
                held_me.append((route, response))
            else:
                route.continue_()

        def logout(route):
            response = route.fetch()
            requests.append({'phase': phase['name'], 'method': 'POST', 'path': '/api/logout',
                             'expected_account': route.request.header_value('x-photocraft-account'),
                             'status': response.status})
            held_logout.append((route, response))

        self.context.route(BASE+'/api/projects', projects)
        self.context.route(BASE+'/api/me', identity)
        self.context.route(BASE+'/api/logout', logout)
        original = signature()
        try:
            self.auth_save(bound=False)
            wait(lambda: any(r['status'] == 503 for r in requests), 'Synthetic503 was not observed')
            self.page.wait_for_timeout(200)
            self.auth_wait(False, '49-ordinary503-is-not-expiry')
            self.assertEqual(signature(), original)

            phase['name'] = 'expired-session'
            self.db.execute("UPDATE photocraft.sessions SET expires_at=now()-interval '1 minute' WHERE hash=%s",
                            (hashlib.sha256(token_a.encode()).hexdigest(),))
            self.auth_save(bound=False)
            self.auth_wait(True, '49-expired-session')
            self.assertEqual(signature(), original)
            self.assertEqual(counts(account_a), [0, 0, 0])

            self.auth_cookie(account_a)
            phase.update(name='held-identity-cookie-switch', hold_me=True)
            self.auth_click('Check sign-in')
            wait(lambda: bool(held_me), 'Identity check was not held after authenticating accountA')
            token_b = self.auth_cookie(account_b)
            route, response = held_me.pop()
            route.fulfill(response=response)
            wait(lambda: any(r['phase'] == phase['name'] and r['method'] == 'GET' and r['status'] == 401
                             for r in requests), 'AccountB cookie bypassed expected-account precondition')
            self.page.wait_for_timeout(200)
            self.auth_wait(True, '49-switched-cookie-rejected')
            self.assertEqual(signature(), original)
            rejected = [r for r in requests if r['phase'] == phase['name'] and r['method'] == 'GET']
            self.assertTrue(rejected)
            self.assertTrue(all(r['expected_account'] == account_a and r['status'] == 401 for r in rejected))
            self.assertEqual(counts(account_b), [0, 0, 0])

            # A normal check now sees B directly. It must not adopt B or fetch B's list.
            phase['name'] = 'wrong-account'
            listings = len(requests)
            with self.page.expect_response(BASE+'/api/me') as checked:
                self.auth_click('Check sign-in')
            self.assertEqual(checked.value.json()['id'], account_b)
            self.page.wait_for_timeout(300)
            self.auth_wait(True, '49-wrong-account-keeps-native-editor')
            self.assertEqual(len(requests), listings)
            self.assertEqual(signature(), original)
            self.auth_save(bound=False)  # The visible Save control must be disabled.
            self.page.wait_for_timeout(200)
            self.assertEqual(len(requests), listings)

            layer = self.inspect()['document']['layers'][0]
            self.execute('layer.renameLayer', {'layer': layer['id'], 'name': 'Account A keeps this edit'})
            changed = signature()
            self.page.wait_for_timeout(2200)
            keys = recovery_keys()
            self.assertEqual(len([key for key in keys if key.startswith(account_a+':')]), 1)
            self.assertFalse(any(key.startswith(account_b+':') or key.startswith('guest:') for key in keys))
            trace('wrong-account-edits-and-recovery-preserved')

            # The actual confirmation must not revoke B or clear A's local editor.
            before = self.auth_frame('49-before-rejected-logout')
            self.page.mouse.click(1400, 32)
            self.page.mouse.move(0, 0)
            self.page.wait_for_timeout(200)
            actions = native_overlay_actions(before, self.auth_frame('49-account-menu'))
            self.assertEqual(len(actions), 2, 'Expired account menu needs Sign in again and Sign out')
            rect = actions[-1]
            self.page.mouse.click((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.mouse.move(0, 0)
            self.page.wait_for_timeout(200)
            actions = native_overlay_actions(before, self.auth_frame('49-logout-confirmation'))
            self.assertEqual(len(actions), 2)
            rect = actions[-1]
            phase['name'] = 'wrong-account-logout'
            point = ((rect[0]+rect[2])/2, (rect[1]+rect[3])/2)
            self.page.mouse.click(*point)
            wait(lambda: bool(held_logout), 'Actual logout request was not sent')
            self.page.mouse.click(*point)
            self.page.wait_for_timeout(200)
            self.assertEqual(len(held_logout), 1, 'Pending logout allowed a duplicate request')
            route, response = held_logout.pop()
            self.assertEqual(response.status, 401)
            self.assertNotIn('set-cookie', response.headers)
            route.fulfill(response=response)
            self.page.wait_for_timeout(300)
            self.assertEqual(signature(), changed)
            self.assertEqual(recovery_keys(), keys)
            actual = self.context.request.get(BASE+'/api/me')
            self.assertTrue(actual.ok)
            self.assertEqual(actual.json()['id'], account_b)
            self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.sessions WHERE hash=%s',
                                            (hashlib.sha256(token_b.encode()).hexdigest(),)).fetchone()[0], 1)
            self.page.keyboard.press('Escape')
            self.page.wait_for_timeout(100)
            self.auth_wait(True, '49-rejected-logout-preserves-original')
            self.assertEqual(counts(account_b), [0, 0, 0])
            trace('wrong-account-logout-rejected')

            phase['name'] = 'original-account-resumed'
            self.auth_cookie(account_a)
            self.auth_click('Check sign-in')
            self.auth_wait(False, '49-original-account-resumed')
            self.assertEqual(signature(), changed)
            self.auth_save(bound=False)
            project = self.wait_revision(1)
            pid = project['id']
            self.assertEqual(self.db.execute('SELECT owner_id::text FROM photocraft.projects WHERE id=%s', (pid,)).fetchone()[0], account_a)
            self.assertEqual(counts(account_b), [0, 0, 0])
            saved = self.auth_cloud_document(pid)
            self.assertTrue(any(layer['name'] == 'Account A keeps this edit' for layer in saved['layers']))

            # An old request's401 must not expire a successfully renewed generation.
            presence_count = {'value': 0}
            def presence(route):
                presence_count['value'] += 1
                if presence_count['value'] == 1:
                    held_presence.append(route)
                elif presence_count['value'] == 2:
                    route.fulfill(status=401, json={'error': 'Synthetic current-generation expiry'})
                else:
                    route.continue_()
            presence_path = BASE+f'/api/projects/{pid}/presence'
            self.context.route(presence_path, presence)
            try:
                wait(lambda: bool(held_presence), 'First presence request was not held')
                self.auth_wait(True, '49-second-request-expired-generation')
                self.auth_cookie(account_a)
                self.auth_click('Check sign-in')
                self.auth_wait(False, '49-new-auth-generation')
                held_presence.pop().fulfill(status=401, json={'error': 'Synthetic stale-generation expiry'})
                self.page.wait_for_timeout(500)
                self.auth_wait(False, '49-stale401-does-not-repause')
                self.assertEqual(signature(), changed)
                self.execute('layer.renameLayer', {'layer': layer['id'], 'name': 'Autosave after stale401'})
                self.auth_revision(pid, 2)
                self.assertTrue(any(item['name'] == 'Autosave after stale401' for item in self.auth_cloud_document(pid)['layers']))
                self.assertEqual(counts(account_b), [0, 0, 0])
                trace('stale401-ignored-and-autosave-resumed')
            finally:
                for route in held_presence:
                    route.abort()
                self.context.unroute(presence_path, presence)
        finally:
            for route, _ in held_me+held_logout:
                route.abort()
            self.context.unroute(BASE+'/api/projects', projects)
            self.context.unroute(BASE+'/api/me', identity)
            self.context.unroute(BASE+'/api/logout', logout)
            (ARTIFACTS/'session-account-switch-observations.json').write_text(json.dumps(
                {'observations': observations, 'requests': requests}, indent=2))

    def test_50_merged_save_acknowledgment_survives_concurrent_session_expiry(self):
        token = self.signed_in()
        self.new(503, 323)
        self.stroke()
        self.execute('shape.create', {'kind':'rect','rect':[50,50,100,80],'fill':'#cc66aa','name':'Original shape'})
        with self.page.expect_response(lambda r: r.url == BASE+'/api/projects' and r.request.method == 'POST') as created:
            self.auth_save(bound=False)
        pid = created.value.json()['id']
        self.auth_revision(pid, 1)
        self.page.wait_for_timeout(300)
        other_context, other = self.context_page('?project='+pid, token=token,
                                                 expected_document=self.inspect()['document'])
        layers = self.inspect()['document']['layers']
        self.context.set_offline(True)
        self.execute('layer.renameLayer', {'layer':layers[0]['id'],'name':'Owner local shape'})
        self.execute('layer.renameLayer', {'layer':layers[1]['id'],'name':'Remote background'}, page=other)
        self.auth_save(page=other)
        response = other_context.request.get(BASE+'/api/projects/'+pid)
        deadline = time.monotonic()+10
        while response.json()['revision'] < 2 and time.monotonic() < deadline:
            other.wait_for_timeout(100)
            response = other_context.request.get(BASE+'/api/projects/'+pid)
        self.assertEqual(response.json()['revision'], 2)
        other_context.close()
        held, attempts = [], []
        state = {'expire':False}

        def commit(route):
            response = route.fetch()
            self.assertTrue(response.ok, response.text())
            self.assertTrue(response.json()['merged'], 'The real server did not produce the required merge')
            held.append((route, response))

        def upload(route):
            attempts.append(route.request.post_data_json)
            route.continue_()

        def presence(route):
            if state['expire']:
                route.fulfill(status=401, content_type='text/html', body='<p>Session expired</p>')
            else:
                route.continue_()

        self.context.route('**/api/uploads/*/commit', commit)
        self.context.route('**/api/projects/*/uploads', upload)
        self.context.route('**/api/projects/*/presence', presence)
        self.context.set_offline(False)
        self.auth_save()
        deadline = time.monotonic()+10
        while not held and time.monotonic() < deadline:
            self.page.wait_for_timeout(100)
        self.assertEqual(len(held), 1)
        state['expire'] = True
        self.auth_wait(True, 'merged-ack-expired')
        before = self.inspect()['document']
        held[0][0].fulfill(response=held[0][1])
        self.page.wait_for_timeout(500)
        self.assertEqual(self.inspect()['document'], before, 'An expired session applied an unchecked cloud result')
        saved = self.auth_revision(pid, 3)
        self.assertNotEqual(saved['content']['sha256'], attempts[0]['sha256'], 'Merged content must differ from the submitted local snapshot')
        state['expire'] = False
        self.auth_click('Check sign-in')
        self.auth_wait(False, 'merged-ack-session-resumed')
        names = {'Owner local shape','Remote background'}
        deadline = time.monotonic()+10
        while {layer['name'] for layer in self.inspect()['document']['layers']} != names and time.monotonic() < deadline:
            self.page.wait_for_timeout(100)
        self.assertEqual({layer['name'] for layer in self.inspect()['document']['layers']}, names,
                         'The successful merged acknowledgment was lost while authentication paused')
        self.assertCountEqual([layer['name'] for layer in self.auth_cloud_document(pid)['layers']], names)
        self.assertEqual(self.auth_revision(pid, 3)['revision'], 3, 'Resuming a confirmed merge made a duplicate version')
        self.assertEqual(len(attempts), 1)
        self.auth_frame('merged-ack-applied-after-renewal')
        (ARTIFACTS/'session-merged-ack.json').write_text(json.dumps({'revision':3,'merged':True,
            'uploaded_sha256':attempts[0]['sha256'],'saved_sha256':saved['content']['sha256'],
            'upload_attempts':len(attempts),'layer_names':sorted(names)}, indent=2))


    def test_51_narrow_preferences_keeps_complete_rows_and_native_cancel(self):
        self.new(640,480)
        document=self.inspect()['document']
        evidence={'scope':'Native General row pixels at1440 and390px; trusted checkbox input and native Cancel.'}

        def opened(width,stacked):
            self.page.set_viewport_size({'width':width,'height':960 if width>390 else 844})
            self.page.mouse.move(0,0)
            self.page.wait_for_timeout(150)
            before=Image.open(io.BytesIO(self.page.screenshot()))
            self.command('ui.menu.invoke',{'id':'edit.preferences.general'})
            deadline=time.monotonic()+3;last=None
            while time.monotonic()<deadline:
                self.page.wait_for_timeout(50)
                picture=self.page.screenshot()
                after=Image.open(io.BytesIO(picture))
                try:
                    bounds=assert_dialog_inside(self,before,after)
                    bounds=(bounds[0],bounds[1]+64,bounds[2],bounds[3]+64)
                    controls=preferences_general_paint(after,bounds,stacked=stacked)
                    break
                except AssertionError as error:
                    last=str(error)
            else:
                (ARTIFACTS/f'preferences-rows-{width}-failed.png').write_bytes(picture)
                self.fail(last)
            (ARTIFACTS/f'preferences-rows-{width}.png').write_bytes(picture)
            dialogs=self.inspect()['dialogs']
            self.assertEqual(len(dialogs),1)
            self.assertEqual(dialogs[0]['fields']['section'],'general')
            evidence[str(width)]={'bounds':bounds,'checkboxes':controls['checkboxes'],'dropdown':controls['dropdown'],
                'glyph_sizes':{name:mask.size for name,mask in controls['masks'].items()}}
            return controls,dialogs[0]

        try:
            reference,wide=opened(1440,False)
            values=wide['fields']['values']
            self.command('ui.dialog.cancel',{'dialog':wide['id']})
            narrow,dialog=opened(390,True)
            assert_preferences_glyphs_complete(self,reference,narrow)
            self.assertEqual(dialog['fields']['values'],values)
            x0,y0,x1,y1=narrow['checkboxes'][0]
            self.page.mouse.click((x0+x1)/2,(y0+y1)/2)
            self.page.wait_for_timeout(100)
            pending=self.inspect()['dialogs'][0]
            expected=dict(values['general'])
            expected['zoomWithScrollWheel']=not expected['zoomWithScrollWheel']
            self.assertEqual(pending['fields']['values']['general'],expected,
                             'Visible native checkbox did not edit exactly its pending preference')
            self.page.keyboard.press('Escape')
            self.page.wait_for_timeout(100)
            self.assertEqual(self.inspect()['dialogs'],[])
            self.command('ui.menu.invoke',{'id':'edit.preferences.general'})
            self.page.wait_for_timeout(100)
            reopened=self.inspect()['dialogs'][0]
            self.assertEqual(reopened['fields']['values'],values,'Cancel committed a pending preference')
            self.command('ui.dialog.cancel',{'dialog':reopened['id']})
            for key in ('layers','history','revision'):
                self.assertEqual(self.inspect()['document'][key],document[key])
            evidence['complete_rows_and_native_cancel']=True
        finally:
            for dialog in self.inspect()['dialogs']:
                self.command('ui.dialog.cancel',{'dialog':dialog['id']})
            (ARTIFACTS/'preferences-rows.json').write_text(json.dumps(evidence,indent=2)+'\n')


    def test_52_compact_preferences_section_selector_preserves_pending_copy(self):
        self.new(640,480)
        document=self.inspect()['document']
        evidence={'scope':'Trusted clicks on rendered native section-menu glyphs; no dialog field setter.','choices':[]}

        def bounds(before,after):
            box=assert_dialog_inside(self,before,after)
            return box[0],box[1]+64,box[2],box[3]+64

        try:
            self.page.mouse.move(0,0)
            wide_before=Image.open(io.BytesIO(self.page.screenshot()))
            self.command('ui.menu.invoke',{'id':'edit.preferences.general'})
            self.page.wait_for_timeout(250)
            wide=Image.open(io.BytesIO(self.page.screenshot()))
            wide.save(ARTIFACTS/'preferences-reference-wide.png')
            references=preferences_section_references(wide,bounds(wide_before,wide))
            evidence['references']={name:{'size':mask.size} for name,mask in references.items()}
            for name,mask in references.items():
                mask.save(ARTIFACTS/f'preferences-reference-{name}.png')
            values=self.inspect()['dialogs'][0]['fields']['values']
            self.page.keyboard.press('Escape')
            self.page.set_viewport_size({'width':390,'height':844})
            self.page.wait_for_timeout(150)
            clean=Image.open(io.BytesIO(self.page.screenshot()))
            self.command('ui.menu.invoke',{'id':'edit.preferences.general'})
            self.page.wait_for_timeout(250)
            narrow=Image.open(io.BytesIO(self.page.screenshot()))
            controls=preferences_general_paint(narrow,bounds(clean,narrow),stacked=True)
            x0,y0,x1,y1=controls['checkboxes'][0]
            self.page.mouse.click((x0+x1)/2,(y0+y1)/2)
            self.page.wait_for_timeout(100)
            pending=self.inspect()['dialogs'][0]['fields']['values']
            self.assertNotEqual(pending['general']['zoomWithScrollWheel'],values['general']['zoomWithScrollWheel'])

            for section in ('interface','general'):
                self.page.mouse.move(0,0)
                self.page.wait_for_timeout(100)
                before=Image.open(io.BytesIO(self.page.screenshot()))
                before.save(ARTIFACTS/f'preferences-before-{section}.png')
                selector=compact_preferences_selector(before,bounds(clean,before))
                x0,y0,x1,y1=selector
                self.page.mouse.click((x0+x1)/2,(y0+y1)/2)
                self.page.mouse.move(0,0)
                deadline=time.monotonic()+3;last=None
                while time.monotonic()<deadline:
                    self.page.wait_for_timeout(50)
                    picture=self.page.screenshot()
                    try:
                        choice=native_preference_menu_choice(before,Image.open(io.BytesIO(picture)),selector,references[section])
                        break
                    except AssertionError as error:
                        last=str(error)
                else:
                    (ARTIFACTS/f'preferences-menu-{section}-failed.png').write_bytes(picture)
                    self.fail(last)
                (ARTIFACTS/f'preferences-menu-{section}.png').write_bytes(picture)
                evidence['choices'].append({'section':section,'selector':selector,**choice})
                x0,y0,x1,y1=choice['rect']
                self.page.mouse.click((x0+x1)/2,(y0+y1)/2)
                self.page.wait_for_timeout(100)
                fields=self.inspect()['dialogs'][0]['fields']
                evidence['choices'][-1]['observed_section']=fields['section']
                self.page.screenshot(path=str(ARTIFACTS/f'preferences-section-{section}.png'))
                self.assertEqual(fields['section'],section,'Painted native menu row did not select its section')
                self.assertEqual(fields['values'],pending,'Section selection lost/changed the pending native working copy')
            self.page.keyboard.press('Escape')
            self.page.wait_for_timeout(100)
            self.assertEqual(self.inspect()['dialogs'],[])
            self.command('ui.menu.invoke',{'id':'edit.preferences.general'})
            self.page.wait_for_timeout(100)
            reopened=self.inspect()['dialogs'][0]
            self.assertEqual(reopened['fields']['values'],values,'Cancel after section switching committed pending preferences')
            self.command('ui.dialog.cancel',{'dialog':reopened['id']})
            for key in ('layers','history','revision'):
                self.assertEqual(self.inspect()['document'][key],document[key])
            evidence['pending_survived_switch_and_cancel_discarded']=True
        finally:
            for dialog in self.inspect()['dialogs']:
                self.command('ui.dialog.cancel',{'dialog':dialog['id']})
            (ARTIFACTS/'preferences-section-selector.json').write_text(json.dumps(evidence,indent=2)+'\n')


    def test_53_native_clipped_shape_move_keeps_held_pixels_and_picked_outline(self):
        from PIL import ImageChops, ImageFilter

        evidence = {'scope': 'Original editable starter, trusted Type and Move input, held versus committed pixels.',
                    'checkpoints': []}

        def wait(check, message, seconds=10):
            deadline = time.monotonic()+seconds
            while time.monotonic() < deadline:
                result = check()
                if result:
                    return result
                self.page.wait_for_timeout(50)
            self.fail(message)

        def picture(name):
            data = self.page.screenshot(scale='css')
            (ARTIFACTS/f'move-preview-{name}.png').write_bytes(data)
            return Image.open(io.BytesIO(data)).convert('RGB')

        def layer_contents(document):
            return [{key: value for key, value in layer.items() if key != 'selected'}
                    for layer in document['layers']]

        def color_mask(image, color):
            bands = ImageChops.difference(image, Image.new('RGB', image.size, color)).split()
            return ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2]).point(
                lambda value: 255 if value <= 2 else 0)

        def outline_coverage(held, committed, rect):
            def ink(x, y):
                if not (0 <= x < held.width and 0 <= y < held.height):
                    return False
                a, b = held.getpixel((x, y)), committed.getpixel((x, y))
                return (sum(abs(v-w) for v, w in zip(a, b)) > 25
                        and a[2] > .7*a[0] and a[2] > .8*a[1])

            def edge(horizontal, at, start, end):
                points = list(range(round(start)+8, round(end)-7, 3))
                self.assertGreater(len(points), 20, 'A visible selected-layer edge is required')
                return sum(any(ink(pos, round(at)+d) if horizontal else ink(round(at)+d, pos)
                               for d in range(-2, 3)) for pos in points)/len(points)

            x0, y0, x1, y1 = rect
            return {'top': edge(True, y0, x0, x1), 'bottom': edge(True, y1, x0, x1),
                    'left': edge(False, x0, y0, y1), 'right': edge(False, x1, y0, y1)}

        try:
            # Wait for real gallery pixels, not merely an initialized command bridge.
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                try:
                    home = Image.open(io.BytesIO(self.page.screenshot(scale='css')))
                    workspace_controls(home)
                    cards = workspace_template_previews(home)
                    self.assertEqual(len(cards), 6)
                    break
                except AssertionError:
                    self.page.wait_for_timeout(50)
            else:
                self.fail('The original starter gallery did not render')
            x0, y0, x1, y1 = cards[2]
            self.page.mouse.click((x0+x1)/2, (y0+y1)/2)
            original = wait(lambda: self.inspect().get('document'), 'Native starter did not open')
            self.assertEqual((original['width'], original['height'], len(original['layers'])), (1920, 1080, 9))
            self.command('ui.set', {'fit': True})
            self.page.wait_for_timeout(250)
            state = self.inspect()
            image = picture('starter')
            box = color_mask(image, (35, 33, 55)).crop((90, 190, 1090, 900)).getbbox()
            self.assertIsNotNone(box, 'Original template background must be painted')
            left, top, right, bottom = box[0]+90, box[1]+190, box[2]+90, box[3]+190
            self.assertTrue(950 < right-left < 1000 and 520 < bottom-top < 560,
                            (left, top, right, bottom))
            zoom = state['views'][0]['zoom']
            point = lambda x, y: (left+x*zoom, top+y*zoom)
            headline = next(layer for layer in original['layers']
                            if layer.get('text', {}).get('text') == 'What\ncomes next.')
            self.page.keyboard.press('t')
            wait(lambda: self.inspect()['tool'] == 'Type', 'Type shortcut did not select the native tool')
            self.page.mouse.click(*point(250, 415))
            edit = wait(lambda: self.inspect().get('textEdit'), 'Native Type did not pick the headline')
            self.assertEqual(edit['layer'], headline['id'])
            self.page.keyboard.press('ControlOrMeta+A')
            self.page.keyboard.type('Make')
            self.page.keyboard.press('Enter')
            self.page.keyboard.type('ideas real.')
            self.page.keyboard.press('ControlOrMeta+Enter')
            wait(lambda: self.inspect().get('textEdit') is None, 'Native Type did not commit')
            typed = self.inspect()['document']
            self.assertEqual(next(layer for layer in typed['layers'] if layer['id'] == headline['id'])['text']['text'],
                             'Make\nideas real.')
            orbit = next(layer for layer in typed['layers'] if layer['name'] == 'Orbit two')
            shape = self.execute('shape.info', {'layer': orbit['id']})
            self.assertEqual(shape['kind'], 'ellipse')
            self.assertIsNone(shape['stroke'])
            geometry = shape['live']['rect']
            self.assertGreater(geometry[2], orbit['bounds'][2], 'Fixture must contain clipped vector geometry')
            self.page.keyboard.press('v')
            wait(lambda: self.inspect()['tool'] == 'Move', 'Move shortcut did not select the native tool')
            start = point(1740, 650)
            # The earliest checkpoint must expose all four true vector edges
            # inside the canvas viewport, not ask for a clipped offscreen edge.
            travel_x = 300
            crop = (left, top, right, bottom)

            for steps in (15, 30, 45):
                before = self.inspect()['document']
                self.page.mouse.move(*start)
                self.page.mouse.down()
                first_down = self.inspect()

                def picked_after_input_frame():
                    current = self.inspect()
                    return current if (current['frame'] > first_down['frame']
                                       and current['document']['activeLayer'] == orbit['id']) else None

                # The control queue drains before canvas pointer processing in
                # the same native frame. Wait for that trusted Down to be
                # consumed, including on later gestures already selecting Orbit.
                picked = wait(picked_after_input_frame, 'Move Auto-Select did not pick the actual orange shape')
                pressed = picked['document']
                evidence.setdefault('selectionFrames', []).append({
                    'steps': steps, 'firstFrame': first_down['frame'], 'pickedFrame': picked['frame'],
                    'firstLayer': first_down['document']['activeLayer'], 'pickedLayer': pressed['activeLayer']})
                self.assertEqual(pressed['activeLayer'], orbit['id'])
                self.assertEqual(pressed['history'], before['history'])
                for step in range(1, steps+1):
                    self.page.mouse.move(start[0]-travel_x*step/45, start[1]+30*step/45)
                    self.page.wait_for_timeout(40)
                held = picture(f'held-{steps}')
                native = self.inspect()
                self.assertIsNone(native.get('textEdit'), 'Committed Type overlay must be absent')
                self.assertEqual(native['document']['activeLayer'], orbit['id'])
                self.assertEqual(layer_contents(native['document']), layer_contents(before))
                self.assertEqual(native['document']['history'], before['history'])
                # Native Auto-Select changes revision before Move begins; it is
                # the held gesture after that selection which must stay inert.
                self.assertEqual(native['document']['revision'], pressed['revision'],
                                 'Held preview changed native revision')
                self.page.mouse.up()
                moved = wait(lambda: self.inspect()['document']
                             if self.inspect()['document']['revision'] != pressed['revision'] else None,
                             'Move release did not commit')
                self.page.wait_for_timeout(150)
                committed = picture(f'committed-{steps}')
                target = next(layer for layer in moved['layers'] if layer['id'] == orbit['id'])
                self.assertLess(target['bounds'][0], orbit['bounds'][0]-100)
                held_mask = color_mask(held.crop(crop), (250, 153, 116))
                committed_mask = color_mask(committed.crop(crop), (250, 153, 116))
                for mask in (held_mask, committed_mask):
                    self.assertGreater(sum(value != 0 for value in mask.get_flattened_data()), 20000,
                                       'Both frames must contain the actual orange shape')
                excess = ImageChops.subtract(held_mask, committed_mask.filter(ImageFilter.MaxFilter(3)))
                missing = ImageChops.subtract(committed_mask, held_mask.filter(ImageFilter.MaxFilter(3)))
                # Read original vector extents, not the narrower raster cache.
                rect = (left+target['bounds'][0]*zoom, top+target['bounds'][1]*zoom,
                        left+(target['bounds'][0]+geometry[2])*zoom,
                        top+(target['bounds'][1]+geometry[3])*zoom)
                coverage = outline_coverage(held, committed, rect)
                result = {'steps': steps, 'expectedOutline': rect, 'outlineCoverage': coverage,
                          'excessOrangePixels': sum(value != 0 for value in excess.get_flattened_data()),
                          'missingOrangePixels': sum(value != 0 for value in missing.get_flattened_data())}
                evidence['checkpoints'].append(result)
                self.assertEqual((result['excessOrangePixels'], result['missingOrangePixels']), (0, 0),
                                 'Held Move pixels differ from the same committed offset beyond a one-pixel edge')
                self.assertGreaterEqual(min(coverage.values()), .75, 'Move retained the previous layer outline')
                self.page.keyboard.press('ControlOrMeta+z')
                wait(lambda: layer_contents(self.inspect()['document']) == layer_contents(typed),
                     'Undo did not restore the complete typed layout')
                self.page.keyboard.press('ControlOrMeta+Shift+z')
                wait(lambda: layer_contents(self.inspect()['document']) == layer_contents(moved),
                     'Redo did not restore the complete moved layout')
                self.page.keyboard.press('ControlOrMeta+z')
                wait(lambda: layer_contents(self.inspect()['document']) == layer_contents(typed),
                     'Next gesture did not start from the original native layout')
            evidence['passed'] = True
        finally:
            self.page.mouse.up()
            (ARTIFACTS/'move-preview-pixels.json').write_text(json.dumps(evidence, indent=2)+'\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
