"""Safety gates for the local opt-in benchmark; these tests create no database/load."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import asyncio
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import benchmark_scale as benchmark


class BenchmarkGuards(unittest.TestCase):
    def argv(self, *extra):
        return ["benchmark_scale.py", "--output", "uncreated-evidence/report.json", "--context", "synthetic guard test", *extra]

    def test_default_dry_run_has_no_external_side_effects(self):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", self.argv("--binary", "does-not-exist", "--fixture", "does-not-exist")))
            guards = [stack.enter_context(patch.object(obj, name, side_effect=AssertionError("Dry-run crossed an external boundary")))
                      for obj, name in [(benchmark.psycopg, "connect"), (benchmark.requests, "Session"),
                                        (benchmark.subprocess, "Popen"), (benchmark.asyncio, "open_connection"),
                                        (benchmark.socket, "socket"), (benchmark, "fixture"),
                                        (Path, "read_bytes"), (Path, "mkdir"), (Path, "write_text")]]
            with redirect_stdout(output):
                benchmark.main()
            for guard in guards:
                guard.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["plan"]["clients"], 10)
        self.assertEqual(report["plan"]["workers"], 1)
        self.assertIn("No connections", report["scope"])

    def test_exchange_dry_run_reports_one_lane_without_external_effects(self):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", self.argv("--pacing", "exchange")))
            for obj, name in [(benchmark.psycopg, "connect"), (benchmark.requests, "Session"),
                              (benchmark.subprocess, "Popen"), (benchmark.asyncio, "open_connection"),
                              (benchmark.socket, "socket"), (benchmark, "fixture"),
                              (Path, "read_bytes"), (Path, "mkdir"), (Path, "write_text")]:
                stack.enter_context(patch.object(obj, name, side_effect=AssertionError("Exchange dry-run performed IO")))
            with redirect_stdout(output):
                benchmark.main()
        report = json.loads(output.getvalue())
        self.assertTrue(report['dry_run'])
        self.assertIsNone(report['plan']['read_ms'])
        self.assertEqual(report['plan']['write_ms'], 80)
        self.assertEqual(report['plan']['live_snapshot_response'], 'PUT')
        self.assertEqual(report['plan']['nominal_requests_per_client_second'], 12.5)
        self.assertEqual(report['plan']['snapshot_validation_limit_bytes'], 65536)

    def exchange_model(self):
        args = SimpleNamespace(pacing='exchange', payload='cursor', max_inflight=2, workers=1,
                               warmup=0, duration=1, clients=2, max_requests=100, max_response_mib=1)
        actors = [{'id': 'actor-a', 'tab': 'tab-a', 'project': 'room', 'worker': 0, 'token': 'synthetic-a'},
                  {'id': 'actor-b', 'tab': 'tab-b', 'project': 'room', 'worker': 0, 'token': 'synthetic-b'}]
        return benchmark.Load(args, 'http://127.0.0.1:1', actors, {}, MagicMock(), [{'port': 1}])

    def test_exchange_model_creates_one_put_task_per_actor_and_correct_denominator(self):
        model = self.exchange_model()
        model.a.clients = 1000  # Mixed callers may retain the total actor count.
        calls = []
        async def client(actor, method, index):
            calls.append((actor['id'], method, index))
            model.statuses[f'{method} 200'] += 1
        model.client = client
        with patch.object(benchmark.asyncio, 'to_thread', new=AsyncMock(return_value={'failure': None})), \
                patch.object(benchmark.subprocess, 'check_output', side_effect=AssertionError('No process sampling in model test')), \
                patch.object(benchmark.asyncio, 'open_connection', side_effect=AssertionError('No network in model test')):
            report = asyncio.run(model.run())
        self.assertEqual(calls, [('actor-a', 'PUT', 0), ('actor-b', 'PUT', 1)])
        self.assertEqual(report['ideal_request_opportunities_active'], 25)
        self.assertEqual(report['observed_completion_ratio'], 2/25)
        self.assertEqual(report['successful_completion_ratio'], 2/25)
        self.assertEqual(report['http_client_count'], 2)
        self.assertFalse(report['http_peer_coverage_active']['all_clients_covered'])
        model.db.execute.assert_not_called()

    def test_exchange_slow_responses_coalesce_without_a_catch_up_burst(self):
        model = self.exchange_model()
        model.active_start, model.active_end = 0, .5
        clock, writes = {'now': 0}, []
        async def sleep(delay):
            clock['now'] += delay
        async def http(method, actor, body):
            writes.append({'at': clock['now'], 'method': method, 'body': json.loads(body)})
            clock['now'] += .19  # Slower than two nominal exchange ticks.
            return 200, 190
        model.http = http
        with patch.object(benchmark, 'time', SimpleNamespace(monotonic=lambda: clock['now'])), \
                patch.object(benchmark.asyncio, 'sleep', new=sleep):
            asyncio.run(model.client(model.actors[0], 'PUT', 0))
        self.assertEqual([w['method'] for w in writes], ['PUT']*3)
        self.assertEqual([w['body']['seq'] for w in writes], [1, 2, 3])
        self.assertEqual([round(w['at'], 3) for w in writes], [0, .19, .38])
        self.assertEqual(model.peak, 1)
        self.assertEqual(model.coalesced, 3)

    def test_stage_requires_every_actor_coverage_and_rejects_warmup_failures(self):
        for missing_peer, warmup_failure in [(False,None),(True,None),(False,'http'),(False,'error')]:
            model=self.exchange_model()
            async def client(actor,method,index):
                model.statuses[f'{method} 200'] += 1
                model.all_statuses[f'{method} 200'] += 1
                if not (missing_peer and index == 1):
                    model.coverage[index].update(model.expected_coverage[index])
            model.client=client
            if warmup_failure == 'http':
                model.all_statuses['PUT 503'] += 1
            elif warmup_failure == 'error':
                model.all_errors['TimeoutError'] += 1
            with self.subTest(missing_peer=missing_peer,warmup_failure=warmup_failure), \
                    patch.object(benchmark.asyncio,'to_thread',new=AsyncMock(return_value={'failure':None})), \
                    patch.object(benchmark.asyncio,'open_connection',side_effect=AssertionError('No network')):
                report=asyncio.run(model.run())
                self.assertEqual(report['http_peer_coverage_active']['all_clients_covered'],not missing_peer)
                self.assertEqual(report['http_peer_coverage_active']['complete_clients'],1 if missing_peer else 2)
                self.assertEqual(report['all_requests_succeeded'],warmup_failure is None)
                passes=report['all_requests_succeeded'] and report['http_peer_coverage_active']['all_clients_covered']
                self.assertEqual(passes,not missing_peer and warmup_failure is None)

    def test_exchange_snapshot_rejects_missing_or_unauthorized_peers(self):
        actor = {'id': 'owner', 'tab': 'own-tab'}
        roster = {('owner', 'own-tab'), ('editor', 'peer-tab')}
        indices = {('owner', 'own-tab'):0, ('editor', 'peer-tab'):1}
        row = {'actor': 'editor', 'tab': 'peer-tab','seq':7,'baseRevision':1,
               'cursor':{'x':7,'y':1},'ttlMs':500,'gesture':None}
        self.assertEqual(benchmark.live_snapshot({'role': 'owner', 'peers': [row]}, actor, roster, indices),
                         (1,{('editor','peer-tab')}))
        self.assertEqual(benchmark.live_snapshot({'role': 'edit', 'peers': []}, actor, roster, indices), (0,set()))
        invalid = [({'accepted': True, 'revision': 1}, 'missing_exchange_snapshot'),
                   ({'role': 'view', 'peers': []}, 'missing_exchange_snapshot'),
                   ({'role': 'owner', 'peers': {}}, 'missing_exchange_snapshot'),
                   ({'role': 'owner', 'peers': [{'actor': 'owner', 'tab': 'own-tab'}]}, 'unauthorized_exchange_peer'),
                   ({'role': 'owner', 'peers': [{'actor': 'other-room', 'tab': 'peer-tab'}]}, 'unauthorized_exchange_peer'),
                   ({'role': 'owner', 'peers': [{'actor': 'editor', 'tab': 'wrong-tab'}]}, 'unauthorized_exchange_peer'),
                   ({'role': 'owner', 'peers': [row, row]}, 'unbounded_exchange_peers'),
                   ({'role': 'owner', 'peers': [row]*3}, 'unbounded_exchange_peers')]
        for value, category in invalid:
            with self.subTest(category=category), self.assertRaisesRegex(ValueError, '^'+category+'$'):
                benchmark.live_snapshot(value, actor, roster, indices)

    def test_peer_shape_and_deterministic_fixture_payload_are_required(self):
        actor = {'id':'a','tab':'a'}
        peers = {('a','a'),('b','b'),('c','c')}
        indices = {('a','a'):0,('b','b'):1,('c','c'):2}
        row = {'actor':'b','tab':'b','seq':7,'baseRevision':1,'ttlMs':500,'cursor':{'x':7,'y':1},'gesture':None}
        invalid = [{'actor':'b','tab':'b'}, dict(row,seq=True),dict(row,seq=0),dict(row,seq=2**63),
                   dict(row,baseRevision=True),dict(row,baseRevision=2),dict(row,ttlMs=True),
                   dict(row,ttlMs=-1),dict(row,ttlMs=2001),dict(row,ttlMs=float('nan')),
                   dict(row,cursor=None),dict(row,cursor={'x':7,'y':2}),dict(row,cursor={'x':8,'y':1}),
                   dict(row,cursor={'x':True,'y':1}),dict(row,cursor={'x':float('inf'),'y':1}),
                   dict(row,cursor={'x':7,'y':1,'extra':0}),dict(row,gesture={'events':[]})]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                benchmark.live_snapshot({'role':'owner','peers':[value]},actor,peers,indices)
        with self.assertRaisesRegex(ValueError,'unauthorized_exchange_peer'):
            benchmark.live_snapshot({'role':'owner','peers':[row,row]},actor,peers,indices)
        for ttl in [0,99,100]:
            self.assertEqual(benchmark.live_snapshot({'role':'edit','peers':[dict(row,ttlMs=ttl)]},
                actor,peers,indices,elapsed_ms=100), (1,set()))
        self.assertEqual(benchmark.live_snapshot({'role':'edit','peers':[dict(row,ttlMs=101)]},
            actor,peers,indices,elapsed_ms=100), (1,{('b','b')}))
        points={'events':[{'kind':'points','points':[[1,2,1]]}]}
        self.assertEqual(benchmark.live_snapshot({'role':'edit','peers':[dict(row,gesture=points)]},
            actor,peers,indices,expected_gesture=points), (1,{('b','b')}))
        with self.assertRaisesRegex(ValueError,'incorrect_fixture_gesture'):
            benchmark.live_snapshot({'role':'edit','peers':[row]},actor,peers,indices,expected_gesture=points)

    def test_declared_native_peer_is_bounded_but_not_a_required_http_publisher(self):
        model = self.exchange_model()
        native = {'id':'native','tab':'native-tab','project':'room'}
        model = benchmark.Load(model.a,model.base,model.actors,{},model.db,model.workers,native_peers=[native])
        row = {'actor':'native','tab':'native-tab','seq':20,'baseRevision':1,'ttlMs':1000,
               'cursor':{'x':24.5,'y':72},'gesture':None}
        self.assertEqual(benchmark.live_snapshot({'role':'edit','peers':[row]},model.actors[0],
            model.room_peers['room'],model.http_indices,model.native_peers), (1,set()))
        self.assertEqual(model.expected_coverage[0],{('actor-b','tab-b')})
        self.assertEqual(benchmark.live_snapshot({'role':'edit','peers':[dict(row,cursor=None)]},model.actors[0],
            model.room_peers['room'],model.http_indices,model.native_peers), (1,set()))
        with self.assertRaises(ValueError):
            benchmark.Load(model.a,model.base,model.actors,{},model.db,model.workers,native_peers=[native,native])
        with self.assertRaises(ValueError):
            benchmark.live_snapshot({'role':'edit','peers':[dict(row,cursor={'x':1_000_001,'y':0})]},
                model.actors[0],model.room_peers['room'],model.http_indices,model.native_peers)

    def http_reply(self, model, value, *, method='PUT', sequence=7, started=11, ended=11.1):
        payload = json.dumps(value).encode()
        reader,writer = MagicMock(),MagicMock()
        reader.readuntil = AsyncMock(return_value=(f'HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n'
            'Content-Type: application/json\r\nConnection: close\r\n\r\n').encode())
        clock = {'now':started}
        async def readexactly(size):
            self.assertEqual(size,len(payload))
            clock['now']=ended
            return payload
        reader.readexactly=readexactly
        writer.drain,writer.wait_closed=AsyncMock(),AsyncMock()
        model.connection=AsyncMock(return_value=(reader,writer))
        model.active_start,model.active_end=10,20
        with patch.object(benchmark,'time',SimpleNamespace(monotonic=lambda:clock['now'])):
            return asyncio.run(model.http(method,model.actors[0],json.dumps({'seq':sequence}).encode()))

    def test_every_pacing_requires_exact_successful_put_ack(self):
        for pacing in benchmark.PACINGS:
            for change in [{'accepted':False},{'accepted':1},{'seq':6},{'seq':True}]:
                model=self.exchange_model();model.a.pacing=pacing
                value={'revision':1,'accepted':True,'seq':7,'role':'owner','peers':[]}|change
                with self.subTest(pacing=pacing,change=change), self.assertRaisesRegex(ValueError,'invalid_live_ack'):
                    self.http_reply(model,value)
        model=self.exchange_model();model.a.pacing='current'
        self.assertEqual(self.http_reply(model,{'revision':1,'accepted':True,'seq':7})[0],200)
        model=self.exchange_model()
        with self.assertRaisesRegex(ValueError,'missing_exchange_snapshot'):
            self.http_reply(model,{'revision':1,'accepted':True,'seq':7})

    def test_http_actor_origin_follows_its_worker_without_changing_legacy_default(self):
        for explicit in [False,True]:
            with self.subTest(explicit=explicit):
                model=self.exchange_model()
                model.workers[0]['port']=45679
                if explicit:
                    model.workers[0]['origin']='http://127.0.0.1:45679'
                self.http_reply(model,{'revision':1,'accepted':True,'seq':7,'role':'owner','peers':[]})
                writer=model.connection.return_value[1]
                sent=writer.write.call_args.args[0]
                expected='http://127.0.0.1:45679' if explicit else model.base
                self.assertIn(('Origin: '+expected+'\r\n').encode(),sent)
                self.assertIn(b'Host: 127.0.0.1:45679\r\n',sent)

    def test_coverage_requires_usable_peer_observed_entirely_during_active_window(self):
        row={'actor':'actor-b','tab':'tab-b','seq':7,'baseRevision':1,'ttlMs':500,'cursor':{'x':7,'y':1},'gesture':None}
        response={'revision':1,'accepted':True,'seq':7,'role':'owner','peers':[row]}
        for method,pacing in [('PUT','exchange'),('GET','current')]:
            for started,ended,ttl,credited in [(11,11.1,500,True),(9,11,2000,False),
                                            (19,21,2000,False),(11,11.1,0,False),(11,11.2,100,False)]:
                model=self.exchange_model();model.a.pacing=pacing
                self.http_reply(model,response|{'peers':[dict(row,ttlMs=ttl)]},method=method,started=started,ended=ended)
                self.assertEqual(bool(model.coverage[0]),credited)
        model=self.exchange_model()
        self.http_reply(model,response|{'peers':[]})
        self.assertEqual(model.coverage[0],set(),'Empty200 must not earn any delivery coverage')

    def test_exchange_large_200_body_cannot_bypass_snapshot_validation(self):
        model = self.exchange_model()
        payload = json.dumps({'revision': 1, 'accepted': True, 'padding': 'x'*66000}).encode()
        reader, writer = MagicMock(), MagicMock()
        reader.readuntil = AsyncMock(return_value=(f'HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n'
                                                  'Content-Type: application/json\r\nConnection: close\r\n\r\n').encode())
        offset = 0
        async def readexactly(size):
            nonlocal offset
            result = payload[offset:offset+size]
            offset += size
            return result
        reader.readexactly = readexactly
        writer.drain, writer.wait_closed = AsyncMock(), AsyncMock()
        model.connection = AsyncMock(return_value=(reader, writer))
        with self.assertRaisesRegex(ValueError, '^exchange_snapshot_validation_limit$'):
            asyncio.run(model.http('PUT', model.actors[0], b'{}'))
        self.assertEqual(model.peer_counts, {})
        writer.close.assert_called_once()
        writer.wait_closed.assert_awaited_once()

    def test_remote_credentialed_and_option_bearing_database_urls_are_rejected(self):
        urls = ["postgresql://fixture@example.invalid/postgres", "postgresql://fixture@localhost/postgres",
                "postgresql://fixture@127.0.0.1/application", "postgresql://fixture:synthetic-password@127.0.0.1/postgres",
                "postgresql://fixture@127.0.0.1/postgres?host=example.invalid", "postgresql://fixture@127.0.0.1/postgres#fragment",
                "postgresql://127.0.0.1/postgres", "https://fixture@127.0.0.1/postgres"]
        for url in urls:
            with self.subTest(category=url.split(":", 1)[0]), patch.object(sys, "argv", self.argv("--execute", "--database", url)):
                errors = io.StringIO()
                with redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
                    benchmark.arguments()
                self.assertEqual(stopped.exception.code, 2)
                self.assertNotIn("synthetic-password", errors.getvalue())
                self.assertNotIn(url, errors.getvalue())

    def test_invalid_resource_limits_are_rejected_before_execution(self):
        invalid = [("--clients", "1001"), ("--duration", "0"), ("--duration", "21"), ("--duration", "nan"),
                   ("--warmup", "4"), ("--cooldown", "11"), ("--max-inflight", "0"), ("--max-inflight", "1001"),
                   ("--max-requests", "1000001"), ("--max-response-mib", "1025")]
        invalid.extend([("--workers", "3"), ("--workers", "16"), ("--db-pool-size", "0"), ("--db-pool-size", "9")])
        for option, value in invalid:
            with self.subTest(option=option, value=value), patch.object(sys, "argv", self.argv("--execute", option, value)):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                    benchmark.arguments()
                self.assertEqual(stopped.exception.code, 2)

    def test_fleet_pool_product_is_bounded_before_execution(self):
        with patch.object(sys, "argv", self.argv("--execute", "--workers", "8", "--db-pool-size", "5")):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                benchmark.arguments()
            self.assertEqual(stopped.exception.code, 2)
        with patch.object(sys, "argv", self.argv("--workers", "8", "--db-pool-size", "4")):
            args = benchmark.arguments()
            self.assertEqual(args.workers * args.db_pool_size, 32)

    def test_worker_start_failure_drops_only_the_database_it_created(self):
        account = MagicMock()
        account.info.server_version = 160000
        account.__enter__.return_value = account
        socket = MagicMock()
        socket.__enter__.return_value = socket
        socket.getsockname.return_value = ("127.0.0.1", 45678)
        args = SimpleNamespace(database="postgresql://fixture@127.0.0.1:55438/postgres", tls_ca=None, workers=1, db_pool_size=5,
                               binary=Path("synthetic-worker"), output=Path("synthetic/report.json"))
        report = {}
        with patch.object(benchmark.psycopg, "connect", return_value=account), patch.object(benchmark.socket, "socket", return_value=socket), \
                patch.object(Path, "open", return_value=io.StringIO()), \
                patch.object(benchmark.subprocess, "Popen", side_effect=RuntimeError("Synthetic worker start failure")):
            with self.assertRaisesRegex(RuntimeError, "Synthetic worker"):
                with benchmark.isolated(args, report):
                    self.fail("A failed worker must never admit the benchmark body")
        statements = [call.args[0].as_string() for call in account.execute.call_args_list]
        self.assertEqual(len(statements), 2)
        own_name = report["disposable_database"]
        self.assertRegex(own_name, r"^photocraft_scale_[0-9a-f]{32}$")
        self.assertEqual(statements, [f'CREATE DATABASE "{own_name}"', f'DROP DATABASE "{own_name}" WITH (FORCE)'])
        self.assertEqual(report["cleanup"], {"own_worker_stopped": True, "workers_started": 0, "workers_stopped": 0,
                                            "own_database_dropped": True, "shared_application_rows_touched": 0})

    def test_partial_fleet_start_stops_existing_workers_before_dropping_own_database(self):
        for browser_workers in [(0,), (0,1)]:
            with self.subTest(browser_workers=browser_workers):
                self.partial_fleet_start(browser_workers)

    def partial_fleet_start(self,browser_workers):
        admin = MagicMock()
        admin.info.server_version = 160000
        admin.__enter__.return_value = admin
        reserved = []
        for port in [45678, 45679]:
            item = MagicMock()
            item.__enter__.return_value = item
            item.getsockname.return_value = ("127.0.0.1", port)
            reserved.append(item)
        process = MagicMock()
        process.pid = 98765
        process.poll.return_value = None
        events = []
        process.terminate.side_effect = lambda: events.append("terminate")
        def wait(**_):
            events.append("wait")
            process.poll.return_value = 0
            return 0
        process.wait.side_effect = wait
        def execute(statement):
            events.append(statement.as_string())
        admin.execute.side_effect = execute
        args = SimpleNamespace(database="postgresql://fixture@127.0.0.1:55438/postgres", tls_ca=None, workers=2, db_pool_size=5,
                               binary=Path("synthetic-worker"), output=Path("synthetic/report.json"))
        report = {}
        with patch.object(benchmark.psycopg, "connect", return_value=admin), \
                patch.object(benchmark.socket, "socket", side_effect=reserved), \
                patch.object(Path, "open", side_effect=lambda *_args, **_kwargs: io.StringIO()), \
                patch.object(benchmark.requests, "Session", side_effect=AssertionError("Failed startup must not reach readiness")), \
                patch.object(benchmark.subprocess, "Popen", side_effect=[process, RuntimeError("Synthetic second worker failure")]) as spawn:
            with self.assertRaisesRegex(RuntimeError, "second worker"):
                with benchmark.isolated(args, report,public_dir=Path('synthetic-public'),browser_workers=browser_workers):
                    self.fail("Partial startup must not begin load")
        own_name = report["disposable_database"]
        self.assertRegex(own_name, r"^photocraft_scale_[0-9a-f]{32}$")
        self.assertEqual(events, [f'CREATE DATABASE "{own_name}"', "terminate", "wait", f'DROP DATABASE "{own_name}" WITH (FORCE)'])
        self.assertEqual(report["cleanup"], {"own_worker_stopped": True, "workers_started": 1, "workers_stopped": 1,
                                            "own_database_dropped": True, "shared_application_rows_touched": 0})
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5)
        process.kill.assert_not_called()
        self.assertEqual(spawn.call_count, 2)
        for index,call in enumerate(spawn.call_args_list):
            self.assertIn('/'+own_name+'?', call.kwargs['env']['DATABASE_URL'])
            expected_port=45678+index if index in browser_workers else 45678
            self.assertEqual(call.kwargs['env']['APP_ORIGIN'], f'http://127.0.0.1:{expected_port}')
            expected_public=Path('synthetic-public').resolve() if index in browser_workers else args.output.parent/'no-public'
            self.assertEqual(call.kwargs['env']['PUBLIC_DIR'],str(expected_public))

    def test_invalid_browser_worker_topology_fails_before_provisioning(self):
        args=SimpleNamespace(workers=2)
        for topology in [(),(1,),(-1,0),(0,2),(0,0),(0,True),(0,'1')]:
            with self.subTest(topology=topology),patch.object(benchmark.psycopg,'connect') as connect:
                with self.assertRaises(ValueError):
                    with benchmark.isolated(args,{},browser_workers=topology):
                        self.fail('Invalid topology admitted database provisioning')
                connect.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
