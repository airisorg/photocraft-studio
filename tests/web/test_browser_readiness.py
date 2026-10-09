"""Replay native journeys with delayed delivery of real cloud responses.

Run against the disposable loopback service configured for test_browser.py. This
module owns no services and inherits BrowserAcceptance's account cleanup/safety.
Responses reach the server normally; only the browser's fetch result is delayed,
so a visible database revision cannot substitute for the native UI receiving its
save acknowledgment. The original journey assertions are reused unchanged.
"""
import hashlib
import json
from collections import Counter

from PIL import Image
import unittest

import test_browser as browser_tests


CASES = (
    'test_08_public_view_opens_in_separate_browser',
    'test_13_conflicting_browser_edits_preserve_a_copy',
    'test_22_sharing_comments_and_history_controls',
    'test_26_sharing_window_blocks_canvas_painting',
    'test_28_collaborator_permission_menu_and_escape',
    'test_save_copy_waits_for_pending_ack',
    'test_conflict_copy_waits_for_menu_frame',
)


class BrowserReadiness(browser_tests.BrowserAcceptance):
    def load(self, page, query='', expected_document=None):
        # Reload verification replaces the JS window; retain the real earlier
        # account/save/conflict delay evidence before navigating away.
        if page.url.startswith(browser_tests.BASE+'/'):
            self._previous_delivery_delays = getattr(self, '_previous_delivery_delays', []) + page.evaluate(
                'window.__ciDeliveryDelays || []')
        page.add_init_script(r'''(() => {
          if (window.__ciDeliveryDelays) return;
          const original = window.fetch.bind(window);
          window.__ciDeliveryDelays = [];
          window.fetch = async (...args) => {
            const input = args[0];
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            const response = await original(...args);
            const kind = url.pathname === '/api/me' ? 'account' :
              (/^\/api\/uploads\/[^/]+\/commit$/.test(url.pathname) ? 'commit' :
              (/^\/api\/share\/[^/]+\/content$/.test(url.pathname) ? 'public-content' : null));
            if (kind) {
              const ms = kind === 'commit' ? 2500 : 1200;
              const begin = performance.now();
              await new Promise(resolve => setTimeout(resolve, ms));
              window.__ciDeliveryDelays.push({kind, status: response.status,
                minimumMs: ms, elapsedMs: performance.now() - begin});
            }
            return response;
          };
        })()''')
        return super().load(page, query, expected_document)

    def open_more_copy(self, more, label, page=None):
        if self._testMethodName != 'test_conflict_copy_waits_for_menu_frame':
            return super().open_more_copy(more, label, page)
        page = page or self.page
        # Hold a real native frame before the More click. Both clicks in the old
        # fixed-200-ms sequence would arrive before the popup exists. Only RAF
        # delivery is delayed; native input and the original journey stay intact.
        page.evaluate('''() => {
          const original = window.requestAnimationFrame.bind(window);
          const held = [];
          const proof = window.__ciMenuFrame = {heldFrames: 0, releasedAfterMs: null};
          let restored = false;
          window.requestAnimationFrame = callback => original(() => {
            if (restored) original(callback);
            else { held.push(callback); proof.heldFrames++; }
          });
          window.__releaseMenuFrame = () => {
            if (restored) return;
            restored = true;
            window.requestAnimationFrame = original;
            if (proof.startedAt !== undefined)
              proof.releasedAfterMs = performance.now() - proof.startedAt;
            for (const callback of held.splice(0)) original(callback);
          };
        }''')
        try:
            page.mouse.move(0, 0)
            page.wait_for_function('window.__ciMenuFrame.heldFrames > 0', polling=25, timeout=3000)
            page.evaluate('''() => {
              window.__ciMenuFrame.startedAt = performance.now();
              setTimeout(window.__releaseMenuFrame, 750);
            }''')
            return super().open_more_copy(more, label, page)
        finally:
            observation = page.evaluate('''() => {
              window.__releaseMenuFrame();
              const {heldFrames, releasedAfterMs} = window.__ciMenuFrame;
              return {heldFrames, releasedAfterMs};
            }''')
            (browser_tests.ARTIFACTS/'conflict-menu-frame-delay.json').write_text(
                json.dumps(observation, indent=2)+'\n')
            self.assertGreater(observation['heldFrames'], 0, 'No native popup frame was held')
            self.assertIsNotNone(observation['releasedAfterMs'])
            self.assertGreaterEqual(observation['releasedAfterMs'], 750,
                                    'The native frame must remain delayed beyond the old 200 ms click')

    def test_conflict_copy_waits_for_menu_frame(self):
        super().test_13_conflicting_browser_edits_preserve_a_copy()

    def test_save_copy_waits_for_pending_ack(self):
        self.signed_in()
        self.new()
        original = self.save_first_project()
        pid = original['id']
        base = browser_tests.BASE
        artifacts = browser_tests.ARTIFACTS
        def metadata(project_id):
            response = self.context.request.get(base+'/api/projects/'+project_id)
            self.assertTrue(response.ok)
            return response.json()
        original = metadata(pid)

        def menu(label):
            controls = self.wait_rendered(
                lambda image: browser_tests.assert_header_geometry(self, image, ['More', 'Comments', 'Save', 'Share']),
                label+'-header')
            more = controls[0]
            self.page.mouse.click((more[0]+more[2])/2, (more[1]+more[3])/2)
            self.page.mouse.move(0, 0)
            # The first two native menu captions are Save a copy and Download
            # .pcraft. Read their actual glyph bands relative to the measured
            # More control; compare the same captions across pending/ready states.
            def captions(image):
                image = image.convert('RGB')
                regions = [(more[0]+8, more[3]+5+row*30, more[0]+85, more[3]+29+row*30)
                           for row in range(2)]
                values = []
                for rect in regions:
                    crop = image.crop(rect)
                    background = Counter(crop.getpixel((x,y)) for y in range(crop.height) for x in range(crop.width)).most_common(1)[0][0]
                    ink = [(x, y, max(abs(a-b) for a,b in zip(crop.getpixel((x,y)), background)))
                           for y in range(crop.height) for x in range(crop.width)
                           if max(abs(a-b) for a,b in zip(crop.getpixel((x,y)), background)) > 20]
                    self.assertGreater(len(ink), 70, 'Native menu caption is not painted')
                    self.assertLessEqual(max(y for x,y,v in ink)-min(y for x,y,v in ink), 16,
                                         'The sample does not isolate a native caption')
                    contrast = sorted(v for x,y,v in ink)[int(len(ink)*.9)]
                    values.append({'contrast':contrast, 'glyph_pixels':len(ink),
                                   'click':[(rect[0]+rect[2])/2, (rect[1]+rect[3])/2]})
                return values
            return self.wait_rendered(captions, label)

        enabled = menu('copy-ready-before-save')
        self.page.keyboard.press('Escape')
        held = []
        creations = []
        self.page.on('request', lambda request: creations.append(True)
                     if request.method == 'POST' and request.url == base+'/api/projects' else None)
        def hold_commit(route):
            response = route.fetch()
            self.assertEqual(response.status, 200, 'The real upload must commit before its ACK is held')
            held.append((route, response))
        self.context.route('**/api/uploads/*/commit', hold_commit)
        try:
            self.execute('edit.fill', {'color':'#0000ff'})
            committed = self.wait_revision(2)
            self.assertEqual(committed['id'], pid)
            self.assertEqual(len(held), 1)
            committed = metadata(pid)
            self.assertNotEqual(committed['content']['sha256'], original['content']['sha256'])
            native_before = self.inspect()['document']
            pending = menu('copy-pending-save')
            observation = {'enabled':enabled, 'pending':pending, 'committed_revision_while_ack_held':2}
            (artifacts/'copy-pending-affordance.json').write_text(json.dumps(observation, indent=2)+'\n')
            self.assertLess(pending[0]['contrast'], enabled[0]['contrast']*.8,
                            'Save a copy looks enabled while its pending save would silently ignore the click')
            self.assertGreaterEqual(pending[1]['contrast'], enabled[1]['contrast']*.9,
                                    'The unrelated Download action must stay visibly enabled')
            self.page.mouse.click(*pending[0]['click'])
            self.page.wait_for_timeout(200)
            self.assertEqual(creations, [], 'A disabled copy action started another save pipeline')
            self.assertEqual(len(self.projects()), 1)
            self.assertEqual(self.inspect()['document'], native_before)
            self.page.keyboard.press('Escape')

            # Releasing the real ACK still goes through the wrapper's delivery delay;
            # the post-save list proves that the application processed Message::Saved.
            with self.page.expect_response(lambda response: response.url == base+'/api/projects'
                                           and response.request.method == 'GET', timeout=20000) as listed:
                route, response = held.pop()
                route.fulfill(response=response)
                self.context.unroute('**/api/uploads/*/commit', hold_commit)
            self.assertTrue(listed.value.ok)
            ready = menu('copy-ready-after-save')
            self.assertGreaterEqual(ready[0]['contrast'], enabled[0]['contrast']*.9,
                                    'Save a copy did not become visibly enabled after the ACK')
            observation['ready'] = ready
            with self.page.expect_response(lambda response: response.url == base+'/api/projects'
                                           and response.request.method == 'GET', timeout=20000) as copied:
                self.page.mouse.click(*ready[0]['click'])
            self.assertTrue(copied.value.ok)
            projects = self.projects()
            self.assertEqual(len(creations), 1, 'One enabled copy click must create exactly one project')
            self.assertEqual(len(projects), 2)
            original_after = metadata(pid)
            copy = metadata(next(project['id'] for project in projects if project['id'] != pid))
            self.assertEqual(original_after['revision'], 2, 'Saving a copy advanced the original project')
            self.assertEqual(original_after['content']['sha256'], committed['content']['sha256'])
            self.assertEqual(copy['revision'], 1)
            self.assertEqual(copy['content']['sha256'], committed['content']['sha256'],
                             'The copy must contain the exact committed native document')
            data = b''.join(self.context.request.get(
                base+f"/api/projects/{copy['id']}/content?revision=1&part={part}").body()
                for part in range((copy['content']['bytes']+524287)//524288))
            self.assertEqual(hashlib.sha256(data).hexdigest(), committed['content']['sha256'])
            self.assertEqual(self.inspect()['document']['history'], native_before['history'])
            observation.update({'new_project_count':1, 'original_revision_after_copy':2,
                                'copy_revision':1, 'copy_native_sha256':hashlib.sha256(data).hexdigest()})
            (artifacts/'copy-pending-affordance.json').write_text(json.dumps(observation, indent=2)+'\n')
        finally:
            for route, response in held:
                route.fulfill(response=response)
            self.context.unroute('**/api/uploads/*/commit', hold_commit)


    def tearDown(self):
        try:
            receipt = list(getattr(self, '_previous_delivery_delays', []))
            for context in self.contexts:
                for page in context.pages:
                    if not page.is_closed():
                        receipt.extend(page.evaluate('window.__ciDeliveryDelays || []'))
            path = browser_tests.ARTIFACTS/(self._testMethodName+'-delivery-delays.json')
            path.write_text(json.dumps(receipt, indent=2)+'\n')
            self.assertTrue(any(r['kind'] == 'account' and r['status'] == 200 for r in receipt),
                            'Actual authenticated account delivery delay did not run')
            self.assertTrue(any(r['kind'] == 'commit' and r['status'] == 200 for r in receipt),
                            'Actual successful save ACK delivery delay did not run')
            if self._testMethodName.startswith('test_08'):
                self.assertTrue(any(r['kind'] == 'public-content' and r['status'] == 200 for r in receipt),
                                'Actual public download delivery delay did not run')
            if self._testMethodName.startswith('test_13') or self._testMethodName == 'test_conflict_copy_waits_for_menu_frame':
                self.assertTrue(any(r['kind'] == 'commit' and r['status'] == 409 for r in receipt),
                                'Actual conflict delivery delay did not run')
            for observation in receipt:
                self.assertGreaterEqual(observation['elapsedMs'], observation['minimumMs'],
                                        'The required response delivery delay was shortened')
        finally:
            super().tearDown()


def load_tests(loader, standard_tests, pattern):
    # Discovery must not replay all inherited journeys (or imported base tests).
    return unittest.TestSuite(BrowserReadiness(name) for name in CASES)


if __name__ == '__main__':
    unittest.main(verbosity=2)
