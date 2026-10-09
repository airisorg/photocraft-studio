"""Replay five native journeys with delayed delivery of real cloud responses.

Run against the disposable loopback service configured for test_browser.py. This
module owns no services and inherits BrowserAcceptance's account cleanup/safety.
Responses reach the server normally; only the browser's fetch result is delayed,
so a visible database revision cannot substitute for the native UI receiving its
save acknowledgment. The original journey assertions are reused unchanged.
"""
import json
import unittest

import test_browser as browser_tests


CASES = (
    'test_08_public_view_opens_in_separate_browser',
    'test_13_conflicting_browser_edits_preserve_a_copy',
    'test_22_sharing_comments_and_history_controls',
    'test_26_sharing_window_blocks_canvas_painting',
    'test_28_collaborator_permission_menu_and_escape',
)


class BrowserReadiness(browser_tests.BrowserAcceptance):
    def load(self, page, query='', expected_document=None):
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

    def tearDown(self):
        try:
            receipt = []
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
            if self._testMethodName.startswith('test_13'):
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
