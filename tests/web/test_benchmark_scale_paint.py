"""Zero-service safety/contract guards for the opt-in mixed native/load harness."""
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import io
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import benchmark_scale_paint as benchmark


class MixedPaintGuards(unittest.TestCase):
    def argv(self, *extra):
        return ['--output','uncreated-mixed-evidence/report.json','--context','Synthetic guard test',*extra]

    def test_dry_plan_has_exact_actor_split_and_no_external_io(self):
        output = io.StringIO()
        with ExitStack() as stack:
            for obj,name in [(benchmark.scale.psycopg,'connect'),(benchmark.scale.requests,'Session'),
                             (benchmark.scale.subprocess,'Popen'),(benchmark.scale.socket,'socket'),
                             (benchmark.scale.asyncio,'open_connection'),(benchmark.scale,'fixture'),
                             (benchmark,'mixed_browser'),(Path,'open'),(Path,'read_bytes'),
                             (Path,'read_text'),(Path,'mkdir'),(Path,'write_text'),(Path,'glob'),
                             (Path,'is_file'),(Path,'stat')]:
                stack.enter_context(patch.object(obj,name,side_effect=AssertionError('Dry plan crossed an external boundary')))
            with redirect_stdout(output):
                benchmark.main(self.argv('--binary','absent','--fixture','absent','--chrome','absent',
                                          '--public-dir','absent','--tls-ca','absent'))
        report=json.loads(output.getvalue())
        self.assertTrue(report['dry_run'])
        p=report['plan']
        self.assertEqual((p['total_actors'],p['http_actors'],p['native_browser_clients']),(1000,998,2))
        self.assertEqual((p['rooms'],p['actors_per_room']),(100,10))
        self.assertEqual((p['paint_target_ms'],p['paint_max_uncertainty_ms']),(500,15))
        self.assertEqual(p['http_put_ms'],80)
        self.assertIn('worker0',p['browser_routing'])
        self.assertIn('No files opened',report['scope'])

    def test_remote_or_credentialed_database_and_resource_excess_are_rejected(self):
        invalid=[('--database','postgresql://fixture@example.invalid/postgres'),
                 ('--database','postgresql://fixture@localhost/postgres'),
                 ('--database','postgresql://fixture:synthetic-secret@127.0.0.1/postgres'),
                 ('--database','postgresql://fixture@127.0.0.1/postgres?host=example.invalid'),
                 ('--database','postgresql://fixture@127.0.0.1/application'),
                 ('--duration','21'),('--duration','nan'),('--duration','0'),('--warmup','4'),
                 ('--max-inflight','1001'),('--max-requests','1000001'),('--max-response-mib','1025'),
                 ('--workers','3'),('--db-pool-size','9'),('--cursor-samples','4')]
        for pair in invalid:
            with self.subTest(option=pair[0]),redirect_stderr(io.StringIO()) as errors,self.assertRaises(SystemExit) as stopped:
                benchmark.arguments(self.argv('--execute',*pair))
            self.assertEqual(stopped.exception.code,2)
            self.assertNotIn('synthetic-secret',errors.getvalue())
        with redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
            benchmark.arguments(self.argv('--workers','8','--db-pool-size','5'))

    def test_native_room_replaces_only_the_owned_eight_actor_room(self):
        db=MagicMock()
        actors=[{'id':f'actor-{i}','email':f'actor-{i}@example.invalid','project':f'room-{i//10}'}
                for i in range(998)]
        projects=[f'room-{i}' for i in range(100)]
        output=benchmark.merge_native_room(db,actors,projects,'native-room')
        self.assertEqual(len(output),100)
        self.assertEqual(output[-1],'native-room')
        self.assertTrue(all(v['project']==f'room-{i//10}' for i,v in enumerate(actors[:990])))
        self.assertTrue(all(v['project']=='native-room' for v in actors[990:]))
        calls=db.execute.call_args_list
        self.assertEqual(len(calls),9)
        self.assertTrue(all(c.args[1][0]=='native-room' for c in calls[:8]))
        self.assertEqual(calls[-1].args,('DELETE FROM photocraft.projects WHERE id=%s',('room-99',)))
        with self.assertRaises(ValueError):
            benchmark.merge_native_room(db,actors[:-1],projects,'native-room')
        self.assertEqual(db.execute.call_count,9,'Invalid roster must fail before writes')

    def test_paint_interval_rejects_warmup_stop_and_insufficient_remaining_time(self):
        model=SimpleNamespace(active_start=10,active_end=20,reason=None)
        runner=benchmark.Background(model)
        with patch.object(benchmark.time,'monotonic',return_value=12):
            self.assertEqual(runner.require_active(reserve=3),12)
        for now,reserve in [(9,0),(20,0),(18,3)]:
            with patch.object(benchmark.time,'monotonic',return_value=now),self.assertRaises(RuntimeError):
                runner.require_active(reserve)
        model.reason='request_budget'
        with patch.object(benchmark.time,'monotonic',return_value=12),self.assertRaises(RuntimeError):
            runner.require_active()
        model.reason=None
        runner.finished.set()
        with patch.object(benchmark.time,'monotonic',return_value=12),self.assertRaises(RuntimeError):
            runner.require_active()

    def test_zero_http_errors_cannot_pass_without_peer_coverage_and_real_paint(self):
        good={'samples':[{}, {}, {}], 'paint_summary':{'all_passed':True},
              'preview_native_unchanged':True, 'http_rooms_native_unchanged':True,
              'http_load':{'all_requests_succeeded':True,'http_client_count':998,
                           'http_peer_coverage_active':{'all_clients_covered':True},
                           'errors_including_warmup':{},'status_counts_including_warmup':{'PUT 200':100}}}
        self.assertTrue(benchmark.result_passes(good,2))
        for scope,key,value in [('http_load','http_peer_coverage_active',{'all_clients_covered':False}),
                                ('http_load','http_client_count',997),
                                ('http_load','errors_including_warmup',{'TimeoutError':1}),
                                ('http_load','status_counts_including_warmup',{'PUT 503':1}),
                                (None,'paint_summary',{'all_passed':False}),
                                (None,'samples',[{},{}]),(None,'preview_native_unchanged',False)]:
            bad=copy.deepcopy(good)
            (bad[scope] if scope else bad)[key]=value
            with self.subTest(key=key):
                self.assertFalse(benchmark.result_passes(bad,2))

    def test_browser_failure_exits_owned_isolation_and_writes_failure_receipt(self):
        events=[]
        def fail_browser(*_args,**_kwargs):
            events.append('browser-fixture')
            raise RuntimeError('synthetic-secret must not enter report')
        @contextmanager
        def isolated(args,report,*,public_dir):
            self.assertEqual(public_dir,Path('synthetic-public'))
            events.append('isolated-enter')
            try:
                yield 'http://127.0.0.1:12345',MagicMock(),[{'index':0,'port':12345}]
            finally:
                events.append('isolated-cleanup')
                report['cleanup']={'own_database_dropped':True,'own_worker_stopped':True}
        written=[]
        with ExitStack() as stack:
            stack.enter_context(patch.object(Path,'is_file',return_value=True))
            stack.enter_context(patch.object(Path,'mkdir'))
            stack.enter_context(patch.object(Path,'read_bytes',return_value=b'synthetic-binary'))
            stack.enter_context(patch.object(Path,'write_text',side_effect=lambda text:written.append(text)))
            stack.enter_context(patch.object(benchmark,'asset_hashes',return_value={'synthetic.wasm':'hash'}))
            stack.enter_context(patch.object(benchmark.scale,'fixture',return_value=(b'fixture',{})))
            stack.enter_context(patch.object(benchmark.resource,'getrlimit',return_value=(65536,65536)))
            stack.enter_context(patch.object(benchmark.platform,'platform',return_value='synthetic-host'))
            stack.enter_context(patch.object(benchmark.scale,'isolated',side_effect=isolated))
            stack.enter_context(patch.object(benchmark,'mixed_browser',side_effect=fail_browser))
            stack.enter_context(patch.object(benchmark.scale.psycopg,'connect',side_effect=AssertionError('No database in fixture')))
            stack.enter_context(patch.object(benchmark.scale.subprocess,'Popen',side_effect=AssertionError('No process in fixture')))
            with redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as stopped:
                benchmark.main(self.argv('--execute','--chrome','synthetic-chrome','--public-dir','synthetic-public'))
        self.assertEqual(stopped.exception.code,1)
        self.assertEqual(events,['isolated-enter','browser-fixture','isolated-cleanup'])
        self.assertEqual(len(written),1)
        result=json.loads(written[0])
        self.assertEqual(result['status'],'error')
        self.assertEqual(result['error_type'],'RuntimeError')
        self.assertTrue(result['cleanup']['own_database_dropped'])
        self.assertNotIn('synthetic-secret',written[0])

    def test_checkpoint_keeps_failing_stage_without_exception_details(self):
        report = {}
        with benchmark.checkpoint(report, 'durable_wait_cloud_revision_2'):
            pass
        with self.assertRaises(AssertionError):
            with benchmark.checkpoint(report, 'durable_wait_reload_native_pixel'):
                raise AssertionError('synthetic-secret browser response')
        self.assertEqual(report['stage'], 'durable_wait_reload_native_pixel')
        self.assertEqual([item['status'] for item in report['checkpoints']], ['passed', 'failed'])
        self.assertEqual(report['checkpoints'][-1]['error_type'], 'AssertionError')
        self.assertNotIn('synthetic-secret', json.dumps(report))


if __name__=='__main__':
    unittest.main()
