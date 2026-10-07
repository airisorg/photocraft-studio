"""Two local synthetic browser accounts: edit-to-commit and edit-to-peer-state latency.

Exactly ten sequential layer renames use automatic autosave after one initial manual save.
This measures observed document state, not remote paint, hosted performance, or capacity.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import secrets
import time
import unittest
from urllib.parse import urlparse
import uuid

from benchmark_collaboration import distribution, local_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin',default=os.environ.get('PHOTOCRAFT_TEST_ORIGIN','http://127.0.0.1:8876'))
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--context',required=True)
    args = parser.parse_args()
    origin = urlparse(args.origin)
    if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or origin.username
            or origin.password or origin.query or origin.fragment or origin.path not in {'','/'}):
        raise ValueError('Browser benchmark requires the existing literal loopback HTTP service')
    local_database(os.environ.get('PHOTOCRAFT_TEST_DATABASE_URL','postgresql://photocraft_test@127.0.0.1:55438/postgres'))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    artifacts = args.output.parent / (args.output.stem+'-screenshots')
    os.environ['PHOTOCRAFT_TEST_ORIGIN'] = args.origin.rstrip('/')
    os.environ['PHOTOCRAFT_TEST_ARTIFACTS'] = str(artifacts)
    # Import after setting the existing harness's environment; no test harness is modified.
    from test_browser import BrowserAcceptance, BASE, Image
    from visual_assertions import assert_header_geometry

    report = {'started_utc':datetime.now(timezone.utc).isoformat(),'status':'running',
        'scope':'Contended local headless browsers; remote engine document state, not remote paint or hosted capacity.',
        'environment':{'system':platform.platform(),'cpus':os.cpu_count(),'load_average_start':os.getloadavg(),
            'context':args.context,'origin':BASE},
        'samples':[],'measurement_poll_ms':100,'nominal_autosave_delay_ms':3500,'nominal_peer_poll_ms':1500,
        'quantiles':'nearest-rank across 10 samples; p95/p99 are the maximum, not a tail-SLO estimate'}

    class BrowserLatency(BrowserAcceptance):
        def test_automatic_save_and_remote_state(self):
            self.signed_in()
            self.new(320,240)
            controls = assert_header_geometry(self,Image.open(io.BytesIO(self.page.screenshot(scale='css'))),['More','Save'])
            x0,y0,x1,y1 = controls[-2]
            self.page.mouse.click((x0+x1)/2,(y0+y1)/2)
            project = self.wait_revision(1)
            initial = self.inspect()['document']
            self.assertEqual((initial['width'],initial['height'],len(initial['layers'])),(320,240,1))
            layer_id = initial['layers'][0]['id']
            metadata = self.context.request.get(BASE+f'/api/projects/{project["id"]}').json()
            report['fixture'] = {'width':320,'height':240,'layers':1,'layer_id':layer_id,
                'initial_bytes':metadata['content']['bytes'],'initial_sha256':metadata['content']['sha256']}
            report['environment']['browser'] = self.browser.version
            report['environment']['wasm_assets'] = sorted(set(re.findall(r'photocraft-web-[a-z0-9]+(?:_bg\.wasm|\.js)',self.page.content())))

            ident,token = str(uuid.uuid4()),secrets.token_hex(32)
            email = ident+'@example.invalid'
            self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',(ident,email,'Latency peer'))
            self.accounts.append(ident)
            self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                (hashlib.sha256(token.encode()).hexdigest(),ident))
            granted = self.context.request.put(BASE+f'/api/projects/{project["id"]}/members',
                headers={'Origin':BASE},data={'email':email,'role':'edit'})
            self.assertTrue(granted.ok,granted.text())
            _,second = self.context_page('?project='+project['id'],token=token)
            self.assertEqual(self.inspect(second)['document']['layers'],initial['layers'])
            acknowledgements = []

            def commit_response(response):
                if response.request.method == 'POST' and re.search(r'/api/uploads/[^/]+/commit$',response.url):
                    observed = time.perf_counter_ns()
                    body = response.json()
                    acknowledgements.append({'observed_ns':observed,'status':response.status,
                        'revision':body.get('revision'),'merged':body.get('merged')})

            self.page.on('response',commit_response)
            for index in range(10):
                name = f'Latency background {index+1:02}'
                revision = project['revision']+index+1
                started = time.perf_counter_ns()
                self.execute('layer.renameLayer',{'layer':layer_id,'name':name})
                owner = self.inspect()['document']
                self.assertEqual([(layer['id'],layer['name']) for layer in owner['layers']],[(layer_id,name)])
                deadline = time.monotonic()+25
                peer_observed = None
                acknowledgement = None
                polls = 0
                while time.monotonic() < deadline:
                    self.page.wait_for_timeout(100)
                    peer = self.inspect(second)['document']
                    polls += 1
                    self.assertIsNotNone(peer)
                    self.assertEqual((peer['width'],peer['height']),(320,240))
                    self.assertEqual([layer['id'] for layer in peer['layers']],[layer_id])
                    if peer_observed is None and peer['layers'][0]['name'] == name:
                        peer_observed = time.perf_counter_ns()
                    acknowledgement = next((value for value in acknowledgements if value['revision'] == revision),None)
                    if acknowledgement is not None and peer_observed is not None:
                        break
                self.assertIsNotNone(acknowledgement,f'Automatic commit {revision} was not acknowledged')
                self.assertEqual((acknowledgement['status'],acknowledgement['merged']),(200,False))
                self.assertIsNotNone(peer_observed,f'Peer did not receive exact layer name {name}')
                self.assertEqual(len(acknowledgements),index+1,'Unexpected duplicate or extra automatic commits')
                report['samples'].append({'sample':index+1,'cloud_revision':revision,'layer_id':layer_id,'expected_name':name,
                    'edit_to_commit_ack_ms':round((acknowledgement['observed_ns']-started)/1e6,3),
                    'edit_to_peer_state_ms':round((peer_observed-started)/1e6,3),'peer_state_polls':polls})
                print(f'Rename {index+1}/10: commit {report["samples"][-1]["edit_to_commit_ack_ms"]} ms; '
                      f'peer state {report["samples"][-1]["edit_to_peer_state_ms"]} ms',flush=True)
                self.page.wait_for_timeout(100)
            self.page.screenshot(path=str(artifacts/'owner-final.png'))
            second.screenshot(path=str(artifacts/'peer-final.png'))
            final = self.projects()
            self.assertEqual(len(final),1)
            self.assertEqual(final[0]['revision'],11)
            report['final_revision'] = final[0]['revision']
            report['summary'] = {name:distribution([sample[name] for sample in report['samples']])
                for name in ['edit_to_commit_ack_ms','edit_to_peer_state_ms']}
            report['screenshots'] = [str(artifacts/'owner-final.png'),str(artifacts/'peer-final.png')]

        def tearDown(self):
            ids = list(self.accounts)
            super().tearDown()
            remaining = self.db.execute('SELECT count(*) FROM photocraft.accounts WHERE id=ANY(%s::uuid[])',(ids,)).fetchone()[0]
            self.assertEqual(remaining,0)
            report['cleanup'] = {'synthetic_accounts_removed':len(ids),'remaining_accounts':remaining}

    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([BrowserLatency('test_automatic_save_and_remote_state')]))
    report['status'] = 'passed' if result.wasSuccessful() else 'failed'
    report['failures'] = [detail for _,detail in result.failures+result.errors]
    report['finished_utc'] = datetime.now(timezone.utc).isoformat()
    report['environment']['load_average_end'] = os.getloadavg()
    args.output.write_text(json.dumps(report,indent=2)+'\n')
    print(f'Browser benchmark {report["status"]}: {args.output}',flush=True)
    raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == '__main__':
    main()
