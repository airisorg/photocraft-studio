"""Safety gates for the local opt-in benchmark; these tests create no database/load."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

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
                with benchmark.isolated(args, report):
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
        for call in spawn.call_args_list:
            self.assertIn('/'+own_name+'?', call.kwargs['env']['DATABASE_URL'])
            self.assertEqual(call.kwargs['env']['APP_ORIGIN'], 'http://127.0.0.1:45678')


if __name__ == "__main__":
    unittest.main(verbosity=2)
