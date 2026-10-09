"""Actual native workspace paint while a real authenticated list is in flight.

Only this one case is loaded. It owns no services and inherits the existing
loopback account/browser cleanup; the transport fixture holds real empty-list
responses without replacing native canvas or editor state.
"""
import hashlib
import io
import json
import time
import unittest

from PIL import Image, ImageChops

import test_browser as browser_tests
from visual_assertions import workspace_controls


class WorkspaceLoading(browser_tests.BrowserAcceptance):
    def test_authenticated_list_loading_clears_on_success_and_error(self):
        self.signed_in()
        self.assertEqual(self.projects(), [])
        self.page.mouse.move(0, 0)
        report = {'phases': [], 'scope': 'Native raster category/visibility; no OCR claim for exact words'}
        held = []

        def picture():
            return Image.open(io.BytesIO(self.page.screenshot(scale='css'))).convert('RGB')

        def crop(image):
            search = workspace_controls(image)['search']
            # Fixed desktop fixture: content immediately below the measured
            # search/Recent-projects row. Header, footer, hover and recovery list
            # are outside the crop; its position is independent of cloud state.
            region = (search[0]+20, search[3]+76, min(image.width-36, search[0]+640), search[3]+178)
            return image.convert('RGB').crop(region), region

        def bands(image):
            pixels = image.load()
            rows = [y for y in range(image.height)
                    if sum(max(pixels[x, y]) < 170 for x in range(image.width)) >= 3]
            runs = []
            for y in rows:
                if not runs or y-runs[-1][-1] > 2:
                    runs.append([y])
                else:
                    runs[-1].append(y)
            return [(r[0], r[-1]+1) for r in runs]

        def genuine_empty(image):
            value, region = crop(image)
            self.assertEqual(len(bands(value)), 2, 'The actual empty title and help must both be visible')
            return value, region

        empty, region = self.wait_rendered(genuine_empty, 'workspace-empty-reference')
        empty_bytes = empty.tobytes()
        report['empty_region'] = list(region)
        report['empty_rgb_sha256'] = hashlib.sha256(empty_bytes).hexdigest()

        def hold_list(route):
            response = route.fetch()
            self.assertEqual(response.status, 200, 'The fixture must hold a real successful list')
            self.assertEqual(response.json(), [], 'The account must genuinely have no cloud projects')
            held.append((route, response, time.monotonic()))

        def await_held():
            deadline = time.monotonic()+10
            while not held and time.monotonic() < deadline:
                self.page.wait_for_timeout(25)
            self.assertEqual(len(held), 1, 'Expected exactly one held browser project-list request')

        def loading(image):
            value, observed_region = crop(image)
            self.assertEqual(observed_region, region)
            self.assertIsNotNone(ImageChops.difference(empty, value).getbbox(),
                                 'Pending list falsely shows the genuine first-project empty state')
            # Spinner::ui inherits the 40px workspace interact height, also
            # visible in the measured search control. Its animated arc can be
            # taller than the 18px caption; measure caption ink separately.
            # The card has 24px padding (4px after this crop's 20px inset),
            # followed by the spinner slot and native RELATED_GAP=8. Split in
            # the middle of that gap, preserving blank space before the text.
            search = workspace_controls(image)['search']
            text_left = 4 + (search[3]-search[1]) + 4
            text = value.crop((text_left, 0, value.width, value.height))
            visible = bands(text)
            self.assertEqual(len(visible), 1, 'Loading must retain one readable native line instead of empty-state title/help')
            self.assertGreaterEqual(visible[0][1]-visible[0][0], 10, 'Loading text is not visibly painted')
            self.assertLessEqual(visible[0][1]-visible[0][0], 26, 'Loading text is clipped or wrapped unexpectedly')
            pixels = text.load()
            columns = [x for x in range(text.width)
                       if any(max(pixels[x, y]) < 170 for y in range(text.height))]
            self.assertGreaterEqual(columns[0], 2, 'Caption reaches the spinner/text split')
            self.assertLess(columns[-1], text.width-2, 'Caption reaches the content boundary')
            self.assertGreaterEqual(columns[-1]-columns[0]+1, 160, 'Loading caption is missing or substantially clipped')
            self.assertLessEqual(columns[-1]-columns[0]+1, 280, 'Unexpected loading caption layout')
            return {'region': list(region), 'text_left': text_left,
                    'text_ink_bands': visible, 'text_ink_columns': [columns[0], columns[-1]+1]}

        def restored_empty(image):
            value, observed_region = crop(image)
            self.assertEqual(observed_region, region)
            self.assertEqual(value.tobytes(), empty_bytes,
                             'Completed list did not replace loading with the exact genuine empty-state content')
            return True

        self.context.route('**/api/projects', hold_list)
        try:
            self.load(self.page)
            await_held()
            self.assertIsNone(self.inspect()['document'])
            report['phases'].append({'kind': 'held-reload-success', 'paint':
                                    self.wait_rendered(loading, 'workspace-held-list-loading')})
            route, response, started = held.pop()
            report['phases'][-1]['held_ms'] = (time.monotonic()-started)*1000
            route.fulfill(response=response)
            self.wait_rendered(restored_empty, 'workspace-empty-after-success')
            self.context.unroute('**/api/projects', hold_list)

            # The error refresh is ordinary Projects navigation, not another
            # reload: a real edited native document must remain exactly intact.
            self.new()
            self.stroke()
            before = self.inspect()['document']
            self.assertTrue(before['canUndo'])
            account_cookies = self.context.cookies()
            self.context.route('**/api/projects', hold_list)
            self.return_to_workspace()
            self.page.mouse.move(0, 0)
            await_held()
            report['phases'].append({'kind': 'held-navigation-error', 'paint':
                                    self.wait_rendered(loading, 'workspace-held-list-before-error')})
            self.assertEqual(self.inspect()['document'], before)
            route, response, started = held.pop()
            report['phases'][-1]['held_ms'] = (time.monotonic()-started)*1000
            with self.page.expect_response(lambda r: r.url == browser_tests.BASE+'/api/projects'
                                           and r.request.method == 'GET' and r.status == 503, timeout=10000):
                route.fulfill(status=503, content_type='application/json',
                              body=json.dumps({'error':'Workspace list unavailable'}))

            def error_completed(image):
                restored_empty(image)
                rgb = image.convert('RGB')
                # Require visible native warning ink in the cloud footer. Its
                # controlled503 response is separately observed above; don't
                # pretend this color test reads the exact error sentence.
                warning = sum(r > b+35 and g > b+20 for r, g, b in
                              rgb.crop((8, rgb.height-27, min(600, rgb.width-20), rgb.height-3)).getdata())
                self.assertGreater(warning, 40, 'The failed project refresh has no visible warning')
                return {'warning_pixels': warning}

            report['error_paint'] = self.wait_rendered(error_completed, 'workspace-list-error-cleared-loading')
            self.assertEqual(self.inspect()['document'], before, 'List failure changed original native work/history')
            self.assertEqual(self.context.cookies(), account_cookies, 'List failure changed the signed-in account session')
            self.context.unroute('**/api/projects', hold_list)
            self.assertEqual(self.projects(), [])
            report['native_document_and_session_preserved'] = True
        finally:
            for route, response, _ in held:
                route.fulfill(response=response)
            self.context.unroute('**/api/projects', hold_list)
            (browser_tests.ARTIFACTS/'workspace-loading-observations.json').write_text(json.dumps(report, indent=2)+'\n')


def load_tests(loader, standard_tests, pattern):
    return unittest.TestSuite([WorkspaceLoading('test_authenticated_list_loading_clears_on_success_and_error')])


if __name__ == '__main__':
    unittest.main(verbosity=2)
