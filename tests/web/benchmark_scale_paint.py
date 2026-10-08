"""Opt-in local mixed workload: 998 HTTP actors plus two native browser clients.

Default prints a plan without opening files, sockets, browsers, workers or databases.
Execution owns one UUID database and a bounded worker fleet. Both actual browser
clients use worker0; HTTP actors are sticky across workers in rooms of ten. This is
not 1000 browsers, a hosted capacity test, or a durable-save latency measurement.
"""
import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import resource
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import zipfile

import benchmark_scale as scale

ROOT = Path(__file__).resolve().parents[2]
TOTAL_ACTORS = 1000
HTTP_ACTORS = 998


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--binary', type=Path, default=ROOT.parent/'photocraft/target/cloud/debug/photocraft-cloud')
    p.add_argument('--public-dir', type=Path, default=ROOT/'dist/web')
    p.add_argument('--chrome', type=Path, default=os.environ.get('PHOTOCRAFT_CHROME'))
    p.add_argument('--database', default='postgresql://photocraft_test@127.0.0.1:55438/postgres')
    p.add_argument('--tls-ca', type=Path, help='Existing operator-owned local TLS CA; never provisions PostgreSQL')
    p.add_argument('--fixture', type=Path, default=ROOT.parent/'fixture.pcraft')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--context', required=True)
    p.add_argument('--workers', type=int, choices=[1, 2, 4, 8], default=8)
    p.add_argument('--db-pool-size', type=int, choices=range(1, 9), default=4)
    p.add_argument('--duration', type=float, default=12)
    p.add_argument('--warmup', type=float, default=3)
    p.add_argument('--cursor-samples', type=int, choices=[1, 2, 3], default=2)
    p.add_argument('--max-inflight', type=int, default=1000)
    p.add_argument('--max-requests', type=int, default=300000)
    p.add_argument('--max-response-mib', type=int, default=512)
    a = p.parse_args(argv)
    u = urlparse(a.database)
    if (u.scheme not in {'postgres', 'postgresql'} or u.hostname != '127.0.0.1' or u.password
            or not u.username or u.path != '/postgres' or u.query or u.fragment):
        p.error('Only a password-free literal127.0.0.1 maintenance /postgres URL is accepted')
    if not (6 <= a.duration <= 20 and 0 <= a.warmup <= 3 and 1 <= a.max_inflight <= 1000
            and 1 <= a.max_requests <= 1000000 and 1 <= a.max_response_mib <= 1024):
        p.error('Exceeds bounded duration/concurrency/request/byte limits')
    if a.workers*a.db_pool_size > 32:
        p.error('Worker database pool budget must not exceed32')
    return a


def plan(a):
    return {'total_actors': TOTAL_ACTORS, 'http_actors': HTTP_ACTORS, 'native_browser_clients': 2,
        'rooms': 100, 'actors_per_room': 10, 'workers': a.workers,
        'database_pool_per_worker': a.db_pool_size, 'worker_database_pool_budget': a.workers*a.db_pool_size,
        'browser_routing': 'Both independent browser contexts use worker0; no cross-worker browser claim',
        'http_routing': 'Sticky actor index modulo workers; eight HTTP actors share the native pair room',
        'http_pacing': 'exchange', 'http_put_ms': 80, 'http_nominal_requests_per_actor_second': 12.5,
        'http_snapshot_validation_limit_bytes': 65536,
        'warmup_seconds': a.warmup, 'active_seconds': a.duration,
        'cursor_samples': a.cursor_samples, 'held_pencil_samples': 1,
        'paint_target_ms': 500, 'paint_max_uncertainty_ms': 15,
        'max_inflight_http': a.max_inflight, 'max_requests_http': a.max_requests,
        'max_response_mib_http': a.max_response_mib,
        'database_transport': 'TLS verify-full' if a.tls_ca else 'local plaintext debug-only',
        'scope': '998 modeled HTTP cursor actors plus2 actual native browser clients; not1000 browsers or hosted capacity'}


def asset_hashes(directory):
    files = sorted(directory.glob('*_bg.wasm'))
    if len(files) != 1 or not (directory/'index.html').is_file():
        raise ValueError('Require exactly one built WASM and index.html in public directory')
    if not 0 < files[0].stat().st_size <= 64*1024*1024:
        raise ValueError('Built WASM exceeds the64MiB artifact read bound')
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in [directory/'index.html', files[0]]}


class Background:
    """One owned event-loop thread; no Playwright calls cross thread boundaries."""
    def __init__(self, model):
        self.model, self.loop = model, None
        self.result, self.error = None, None
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._run, name='photocraft-owned-http-load', daemon=True)

    def _run(self):
        async def run():
            self.loop = asyncio.get_running_loop()
            return await self.model.run()
        try:
            self.result = asyncio.run(run())
        except BaseException as error:
            self.error = type(error).__name__
        finally:
            self.finished.set()

    def start(self):
        self.thread.start()

    def stop(self):
        if self.thread.ident is None:
            return
        if self.loop is not None and not self.finished.is_set():
            try:
                self.loop.call_soon_threadsafe(self.model.halt, 'browser_fixture_stopped')
            except RuntimeError:
                pass  # The loop completed between the event check and dispatch.
        self.thread.join(timeout=8)
        if self.thread.is_alive():
            raise RuntimeError('Owned load thread exceeded its bounded shutdown')

    def require_active(self, reserve=0):
        now = time.monotonic()
        if (self.finished.is_set() or self.model.reason or self.error or
                not self.model.active_start <= now < self.model.active_end-reserve):
            raise RuntimeError('Paint measurement is outside healthy active load')
        return now


def merge_native_room(db, actors, projects, pid):
    """Replace only the eight-member final synthetic room, keeping exactly100 rooms."""
    last = projects[-1]
    members = [a for a in actors if a['project'] == last]
    if len(actors) != HTTP_ACTORS or len(projects) != 100 or len(members) != 8:
        raise ValueError('Unexpected mixed-room fixture size')
    with db.transaction():
        for actor in members:
            db.execute("INSERT INTO photocraft.members(project_id,email,role) VALUES(%s,%s,'edit')",
                       (pid, actor['email']))
        db.execute('DELETE FROM photocraft.projects WHERE id=%s', (last,))
    for actor in members:
        actor['project'] = pid
    return projects[:-1]+[pid]


def result_passes(report, cursor_samples):
    load = report.get('http_load', {})
    return bool(report.get('paint_summary', {}).get('all_passed')
        and len(report.get('samples', [])) == cursor_samples+1
        and load.get('all_requests_succeeded')
        and load.get('http_client_count') == HTTP_ACTORS
        and load.get('http_peer_coverage_active', {}).get('all_clients_covered')
        and not load.get('errors_including_warmup')
        and all(key.endswith(' 200') for key in load.get('status_counts_including_warmup', {}))
        and report.get('preview_native_unchanged') and report.get('http_rooms_native_unchanged'))


@contextmanager
def checkpoint(report, name):
    """Record fixed stage names and outcomes, never arbitrary exception content."""
    report['stage'] = name
    entry = {'stage': name, 'status': 'started'}
    report.setdefault('checkpoints', []).append(entry)
    started = time.monotonic()
    try:
        yield entry
    except Exception as error:
        entry.update(status='failed', error_type=type(error).__name__)
        raise
    else:
        entry['status'] = 'passed'
    finally:
        entry['elapsed_ms'] = round((time.monotonic()-started)*1000, 3)


def durable_after_load(bench, peer, pid, base, report, untouched):
    """Untimed persistence checks with individually attributable safe outcomes."""
    with checkpoint(report, 'durable_release_native_stroke'):
        bench.page.mouse.up()
        bench.page.mouse.move(0, 0)
    with checkpoint(report, 'durable_wait_cloud_revision_2') as outcome:
        meta = bench.auth_revision(pid, 2)
        outcome['revision'] = meta['revision']
    with checkpoint(report, 'durable_read_sender_pixel'):
        expected = bench.execute('document.pixel', {'x':72, 'y':72})
    with checkpoint(report, 'durable_assert_sender_pixel_changed'):
        bench.assertNotEqual(expected, untouched)
    with checkpoint(report, 'durable_wait_peer_native_pixel'):
        bench.wait_native(peer, {'x':72, 'y':72}, expected)
    with checkpoint(report, 'durable_read_expected_native_document'):
        expected_document = bench.inspect()['document']
        bench.assertIsNotNone(expected_document)
    with checkpoint(report, 'durable_reload_peer'):
        peer.reload(wait_until='domcontentloaded')
    with checkpoint(report, 'durable_wait_reload_bridge'):
        peer.wait_for_function('typeof window.photocraftCommand === "function"', timeout=60000)
    # The bridge exists before the asynchronous project download/open completes.
    # Reuse the ordinary successful-open fixture's native dimensions/layers/active
    # layer checks before issuing document.pixel, which rejects an absent document.
    with checkpoint(report, 'durable_wait_reload_native_document'):
        bench.wait_opened_document(peer, expected_document)
    with checkpoint(report, 'durable_wait_reload_native_pixel'):
        bench.wait_native(peer, {'x':72, 'y':72}, expected)
    with checkpoint(report, 'durable_fetch_metadata') as outcome:
        response = bench.context.request.get(base+f'/api/projects/{pid}')
        outcome['http_status'] = response.status
    with checkpoint(report, 'durable_assert_metadata_status'):
        bench.assertTrue(response.ok)
    with checkpoint(report, 'durable_decode_metadata') as outcome:
        meta = response.json()
        outcome['revision'] = meta['revision']
        outcome['bytes'] = meta['content']['bytes']
    chunks = []
    for part in range((meta['content']['bytes']+524287)//524288):
        with checkpoint(report, 'durable_download_part_'+str(part)) as outcome:
            response = bench.context.request.get(
                base+f"/api/projects/{pid}/content?revision={meta['revision']}&part={part}")
            outcome['http_status'] = response.status
            chunks.append(response.body())
            outcome['bytes'] = len(chunks[-1])
        with checkpoint(report, 'durable_assert_part_status_'+str(part)):
            bench.assertTrue(response.ok)
    with checkpoint(report, 'durable_assert_download_sha'):
        data = b''.join(chunks)
        bench.assertEqual(hashlib.sha256(data).hexdigest(), meta['content']['sha256'])
    with checkpoint(report, 'durable_decode_native_manifest'):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            manifest = json.loads(archive.read('manifest.json'))['document']
    with checkpoint(report, 'durable_assert_native_dimensions') as outcome:
        dimensions = (manifest['size']['width'], manifest['size']['height'])
        outcome['dimensions'] = list(dimensions)
        bench.assertEqual(dimensions, (320, 240))
    report['durable_after_load'] = {'revision':meta['revision'], 'sha256':meta['content']['sha256'],
        'peer_native_pixel_and_reload':'passed', 'download_sha_verified':True, 'timed_as_paint':False}


def mixed_browser(a, base, db, workers, report, data, info):
    # Import only after execution is explicitly authorized and the isolated origin
    # exists. BrowserAcceptance module globals never point at a shared service.
    os.environ['PHOTOCRAFT_TEST_ORIGIN'] = base
    artifacts = a.output.parent/(a.output.stem+'-screenshots')
    os.environ['PHOTOCRAFT_TEST_ARTIFACTS'] = str(artifacts)
    from playwright.sync_api import sync_playwright
    from test_live_browser import LiveBrowser
    from paint_latency import PaintObserver, summarize_results

    class NativePair(LiveBrowser):
        def runTest(self):
            pass

    artifacts.mkdir(parents=True, exist_ok=True)
    bench = NativePair()
    background = None
    setup_complete = False
    report['samples'] = []
    report['paint_gate'] = {'target_ms': 500, 'max_uncertainty_ms': 15}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=str(a.chrome), headless=True,
            args=['--enable-unsafe-webgpu', '--enable-unsafe-swiftshader'])
        # Share the existing monitor handle: fixture setup precedes load, and
        # psycopg serializes later monitor/native-check calls on this one handle.
        bench.browser, bench.db = browser, db
        try:
            report['stage'] = 'native_setup'
            bench.setUp()
            setup_complete = True
            pid, peer, owner_rect, peer_rect, _ = bench.pair()
            native_ids = list(bench.accounts)
            if len(native_ids) != 2:
                raise RuntimeError('Expected exactly two independent native accounts')
            # Persistence starts at the first publication, not necessarily at
            # command-bridge readiness. These setup moves are outside load/timing.
            for page,rect in [(bench.page,owner_rect),(peer,peer_rect)]:
                page.mouse.move(rect[0]+240,rect[1]+200)
                page.wait_for_function("""() => {
                    const raw=sessionStorage.getItem('photocraft.live.tab.v1');
                    return raw && typeof JSON.parse(raw).tab==='string';
                }""",timeout=5000)
                page.mouse.move(0,0)
            native_tabs = [page.evaluate("JSON.parse(sessionStorage.getItem('photocraft.live.tab.v1')).tab")
                           for page in [bench.page, peer]]
            report['stage'] = 'http_fixture_setup'
            load_args = SimpleNamespace(**vars(a), clients=HTTP_ACTORS, scenario='rooms10', pacing='exchange', payload='cursor')
            actors, projects = scale.seed(db, load_args, data, info)
            projects = merge_native_room(db, actors, projects, pid)
            count = db.execute('SELECT count(*) FROM photocraft.accounts').fetchone()[0]
            if count != TOTAL_ACTORS:
                raise RuntimeError('Mixed fixture must contain exactly1000 accounts')
            report['fixture'] = {'http_document': info, 'native_document': {'width':320, 'height':240},
                                 'account_count':count, 'project_count':len(projects), 'mixed_room_http_actors':8}
            report['environment']['browser'] = browser.version
            report['environment']['native_client_workers'] = [0, 0]
            report['environment']['viewport'] = [1440, 960]
            report['environment']['device_scale_factor'] = 1
            native_peers = [{'id':ident,'tab':tab,'project':pid} for ident,tab in zip(native_ids,native_tabs)]
            model = scale.Load(load_args, base, actors, report, db, workers, native_peers=native_peers)
            background = Background(model)
            before_native = bench.inspect(peer)['document']
            untouched = bench.execute('document.pixel', {'x':72, 'y':72}, peer)
            before_cloud = db.execute('SELECT revision FROM photocraft.projects WHERE id=%s',(pid,)).fetchone()[0]
            report['stage'] = 'active_load_readiness'
            background.start()
            deadline = time.monotonic()+a.warmup+5
            # Every background actor must actually succeed during the active
            # interval; a nominal task count alone is not proof of1000 actors.
            while time.monotonic() < deadline:
                if background.finished.is_set() or model.reason:
                    break
                if len(model.client_succeeded) == HTTP_ACTORS and time.monotonic() >= model.active_start:
                    break
                peer.wait_for_timeout(10)
            background.require_active(reserve=4)
            if len(model.client_succeeded) != HTTP_ACTORS:
                raise RuntimeError('Not all998 HTTP actors reached successful active requests')
            report['load_overlap'] = {'active_started_monotonic':model.active_start,
                'active_ends_monotonic':model.active_end, 'all_http_actors_observed_before_input':True,
                'active_http_actor_count_before_input':len(model.client_succeeded)}

            def measure(scenario, trigger, oracle):
                report['stage'] = 'timed_'+scenario
                started = background.require_active(reserve=3)
                requests_before = model.requests
                with PaintObserver(peer) as observer:
                    result = observer.measure(bench.page, trigger, oracle, event_type='pointermove',
                        target_ms=500, timeout_ms=1500, max_uncertainty_ms=15)
                    ended = time.monotonic()
                    result.update(scenario=scenario, load_measurement_started_monotonic=started,
                                  load_measurement_finished_monotonic=ended,
                                  http_requests_before=requests_before, http_requests_after=model.requests,
                                  successful_http_actors=len(model.client_succeeded))
                    report['samples'].append(result)
                    for suffix, image in [('baseline',observer.last_baseline_png),('match',observer.last_matching_png)]:
                        if image:
                            (artifacts/(scenario+'-'+suffix+'.png')).write_bytes(image)
                background.require_active()
                if not model.active_start <= started <= ended < model.active_end:
                    raise RuntimeError('Paint observation crossed the active load boundary')
                if model.requests <= requests_before:
                    raise RuntimeError('No background HTTP requests progressed during paint observation')
                return result

            for index in range(a.cursor_samples):
                x, y = 40+40*index, 20
                bench.page.mouse.move(0, 0)
                measure('cursor-'+str(index+1), lambda: bench.page.mouse.move(owner_rect[0]+x, owner_rect[1]+y),
                        bench.patch(peer_rect,x,y,(154,107,255)))
            bench.page.mouse.move(owner_rect[0]+24, owner_rect[1]+72)
            bench.page.mouse.down()
            peer.wait_for_timeout(30)
            def pencil():
                bench.page.mouse.move(owner_rect[0]+80, owner_rect[1]+72)
                for x in range(88,137,8):
                    peer.wait_for_timeout(1000/60)
                    bench.page.mouse.move(owner_rect[0]+x, owner_rect[1]+72)
            measure('held-pencil', pencil, bench.patch(peer_rect,72,72,(25,197,99)))
            # The stroke stays held until load ends: the eight HTTP peers in this
            # room continue on canonical base1, and the preview stays non-durable.
            bench.assertEqual(bench.execute('document.pixel', {'x':72,'y':72},peer), untouched)
            bench.assertEqual(bench.inspect(peer)['document']['history'], before_native['history'])
            bench.assertEqual(bench.inspect(peer)['document']['revision'], before_native['revision'])
            bench.assertEqual(db.execute('SELECT revision FROM photocraft.projects WHERE id=%s',(pid,)).fetchone()[0], before_cloud)
            report['stage'] = 'active_load_drain'
            while not background.finished.is_set() and time.monotonic() < model.active_end+5:
                peer.wait_for_timeout(25)
            if not background.finished.is_set():
                raise RuntimeError('HTTP load exceeded its bounded active/drain interval')
            background.thread.join(timeout=1)
            if background.error:
                raise RuntimeError('HTTP load thread failed: '+background.error)
            report['http_load'] = background.result
            report['preview_native_unchanged'] = True
            report['http_rooms_native_unchanged'] = db.execute(
                'SELECT count(*) FROM photocraft.projects WHERE revision<>1').fetchone()[0] == 0
            durable_after_load(bench, peer, pid, base, report, untouched)
            report['paint_summary'] = summarize_results(report['samples'])
            report['status'] = 'passed' if result_passes(report,a.cursor_samples) else 'failed'
        finally:
            try:
                if background is not None:
                    try:
                        background.stop()
                    finally:
                        report['load_thread_stopped'] = not background.thread.is_alive()
                        if background.result is not None:
                            report['http_load'] = background.result
            finally:
                try:
                    if setup_complete:
                        bench.page.mouse.up()
                        bench.tearDown()
                finally:
                    browser.close()


def main(argv=None):
    a = arguments(argv)
    report = {'dry_run':not a.execute, 'plan':plan(a)}
    if not a.execute:
        report['scope'] = 'No files opened, connections, databases, workers or browser/load activity.'
        print(json.dumps(report,indent=2))
        return
    # All filesystem/provisioning activity is behind --execute.
    if a.chrome is None or not a.chrome.is_file():
        raise ValueError('Explicit installed --chrome executable is required')
    if a.tls_ca and (not a.tls_ca.is_file() or a.tls_ca.stat().st_size>65536):
        raise ValueError('TLS CA must be an existing public PEM <=64KiB')
    data,info = scale.fixture(a.fixture)
    if len(data)>1024*1024:
        raise ValueError('HTTP fixture must be <=1MiB')
    soft,hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < a.max_inflight*2+128:
        raise ValueError('File descriptor limit insufficient; harness does not change it')
    hashes = asset_hashes(a.public_dir)
    binary_hash = hashlib.sha256(a.binary.read_bytes()).hexdigest()
    a.output.parent.mkdir(parents=True,exist_ok=True)
    report.update(started_utc=datetime.now(timezone.utc).isoformat(), status='running',
        environment={'system':platform.platform(), 'cpus':os.cpu_count(), 'load_start':os.getloadavg(),
            'context':a.context, 'binary_sha256':binary_hash, 'web_before':hashes,
            'process_fd_limit':{'soft':soft,'hard':hard,'changed_by_harness':False}})
    try:
        with scale.isolated(a,report,public_dir=a.public_dir) as (base,db,workers):
            mixed_browser(a,base,db,workers,report,data,info)
    except Exception as error:
        report['status']='error'
        report['error_type']=type(error).__name__
        # Do not copy arbitrary HTTP/DB/browser messages, tokens or account data.
    finally:
        try:
            report['environment']['web_after']=asset_hashes(a.public_dir)
            report['environment']['binary_sha256_after']=hashlib.sha256(a.binary.read_bytes()).hexdigest()
            report['artifacts_unchanged']=(hashes==report['environment']['web_after'] and
                binary_hash==report['environment']['binary_sha256_after'])
        except Exception as error:
            report['artifact_verification_error_type']=type(error).__name__
            report['artifacts_unchanged']=False
        if not report['artifacts_unchanged']:
            report['status']='failed'
        report['finished_utc']=datetime.now(timezone.utc).isoformat()
        report['environment']['load_end']=os.getloadavg()
        a.output.write_text(json.dumps(report,indent=2)+'\n')
        print(f"Mixed scale/paint benchmark {report['status']}: {a.output}",flush=True)
    raise SystemExit(0 if report['status']=='passed' else 1)


if __name__=='__main__':
    main()
