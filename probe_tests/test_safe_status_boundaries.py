"""Local diagnostic boundaries only: synthetic SQLite and fake loopback HTTP."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import runpy
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


NOW = 1800000000.0
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'safe-status.py'


class SafeStatusBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='safe-status-boundaries-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / 'probe.sqlite'
        with sqlite3.connect(self.database) as db:
            db.executescript('''
                CREATE TABLE weixin_accounts(status TEXT,last_poll REAL,last_error TEXT);
                CREATE TABLE weixin_outbox(status TEXT);
                CREATE TABLE subscriptions(name TEXT,expires REAL);
                INSERT INTO weixin_accounts VALUES('ready',1799999995,NULL);
                INSERT INTO weixin_outbox VALUES('accepted');
                INSERT INTO weixin_outbox VALUES('delivery_unknown');
                INSERT INTO subscriptions VALUES('weixin.message',NULL);
                INSERT INTO subscriptions VALUES('expired',1);
            ''')
        for target in ('socket.getaddrinfo', 'socket.create_connection',
                       'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('offline diagnostics only'))
            guard.start()
            self.addCleanup(guard.stop)
        self.calls = []
        self.closed = []

    def run_status(self, *, url='http://127.0.0.1:43210', outcomes=None):
        path = self.root / 'service-health.url'
        if url is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(url + '\n')
        responses = outcomes or {'/healthz': 200, '/readyz': 200}
        outer = self

        class FakeConnection:
            def __init__(self, host, port, timeout):
                outer.calls.append((host, port, timeout))
                self.endpoint = None

            def request(self, method, endpoint):
                if method != 'GET' or endpoint not in ('/healthz', '/readyz'):
                    raise AssertionError('unexpected diagnostic request')
                self.endpoint = endpoint

            def getresponse(self):
                result = responses[self.endpoint]
                if isinstance(result, Exception):
                    raise result
                return SimpleNamespace(status=result)

            def close(self):
                outer.closed.append(self.endpoint)

        output = io.StringIO()
        with patch('sys.argv', [str(SCRIPT), '--state-dir', str(self.root)]), \
                patch('time.time', return_value=NOW), \
                patch('http.client.HTTPConnection', FakeConnection), \
                contextlib.redirect_stdout(output):
            runpy.run_path(str(SCRIPT), run_name='__main__')
        self.output = output.getvalue()
        return json.loads(self.output)

    def test_snapshot_scope_and_poll_observation_do_not_claim_remote_identity_or_freshness(self):
        result = self.run_status()
        self.assertEqual(result['scope'], 'local_snapshot')
        self.assertEqual(result['observed_at'], NOW)
        self.assertEqual(result['observed_at_clock'], 'host_wall_clock')
        self.assertEqual(result['remote_host_mapping'], 'unknown')
        self.assertEqual(result['poll_age_seconds'], 5.0)
        self.assertEqual(result['poll_observation']['age_status'], 'observed')
        self.assertEqual(result['poll_observation']['freshness'], 'not_evaluated')
        self.assertEqual(result['account_status'], 'ready')
        self.assertEqual(result['active_subscriptions'], {'weixin.message': 1})
        self.assertEqual(result['send_counts'], {'accepted': 1, 'delivery_unknown': 1})
        self.assertEqual(result['health'], {'/healthz': 200, '/readyz': 200})
        self.assertEqual(self.calls, [('127.0.0.1', 43210, 2)] * 2)

    def test_healthz_timeout_does_not_suppress_readyz_probe_or_leak_error(self):
        result = self.run_status(outcomes={'/healthz': TimeoutError('private-error-value'), '/readyz': 200})
        self.assertEqual(result['health'], {'/healthz': 'unavailable', '/readyz': 200})
        self.assertEqual(result['health_observations']['/healthz']['error_kind'], 'timeout')
        self.assertEqual(result['health_observations']['/readyz']['state'], 'http_response')
        self.assertEqual(self.closed, ['/healthz', '/readyz'])
        self.assertNotIn('private-error-value', self.output)

    def test_readyz_failure_preserves_healthz_and_local_state(self):
        result = self.run_status(outcomes={'/healthz': 200, '/readyz': ConnectionError('private-error-value')})
        self.assertEqual(result['health'], {'/healthz': 200, '/readyz': 'unavailable'})
        self.assertEqual(result['health_observations']['/readyz']['error_kind'], 'connection_error')
        self.assertEqual(result['send_counts']['accepted'], 1)
        self.assertEqual(self.closed, ['/healthz', '/readyz'])
        self.assertNotIn('private-error-value', self.output)

    def test_http_non_success_is_preserved_without_remote_ready_claim(self):
        result = self.run_status(outcomes={'/healthz': 503, '/readyz': 401})
        self.assertEqual(result['health'], {'/healthz': 503, '/readyz': 401})
        self.assertEqual(result['remote_host_mapping'], 'unknown')
        self.assertEqual(result['health_observations']['/healthz']['state'], 'http_response')

    def test_invalid_http_status_is_bounded_and_other_endpoint_survives(self):
        for invalid in (True, '200', None, 0, 1000):
            with self.subTest(status=invalid):
                result = self.run_status(outcomes={'/healthz': invalid, '/readyz': 200})
                self.assertEqual(result['health']['/healthz'], 'unavailable')
                self.assertEqual(result['health']['/readyz'], 200)
                self.assertEqual(result['health_observations']['/healthz']['error_kind'], 'invalid_response')

    def test_future_invalid_missing_poll_times_are_unknown_without_guessing_threshold(self):
        for timestamp, status in ((NOW + 1, 'future_timestamp'), ('invalid', 'invalid_timestamp'),
                                  (float('inf'), 'invalid_timestamp'), (-1, 'invalid_timestamp'),
                                  (None, 'unavailable')):
            with self.subTest(timestamp=timestamp):
                with sqlite3.connect(self.database) as db:
                    db.execute('UPDATE weixin_accounts SET last_poll=?', (timestamp,))
                result = self.run_status(url=None)
                self.assertIsNone(result['poll_age_seconds'])
                self.assertEqual(result['poll_observation']['age_status'], status)
                self.assertEqual(result['poll_observation']['freshness'], 'not_evaluated')
        with sqlite3.connect(self.database) as db:
            db.execute('UPDATE weixin_accounts SET last_poll=0')
        result = self.run_status(url=None)
        self.assertEqual(result['poll_age_seconds'], NOW)
        self.assertEqual(result['poll_observation']['age_status'], 'observed')

    def test_missing_or_untrusted_health_destination_never_opens_http(self):
        urls = (None, 'https://127.0.0.1:43210', 'http://example.invalid:43210',
                'http://localhost:43210', 'http://[::1]:43210', 'http://127.0.0.1:0',
                'http://user:secret@127.0.0.1:43210', 'http://127.0.0.1:43210/private',
                'http://127.0.0.1:43210?secret=value', 'http://127.0.0.1:43210#fragment',
                'http://127.0.0.1:43210\n/private', 'http://127.0.0.1:99999', 'x' * 2049)
        for url in urls:
            with self.subTest(url=url):
                self.calls.clear()
                result = self.run_status(url=url)
                self.assertEqual(result['health'], 'unavailable')
                self.assertEqual(self.calls, [])
                self.assertNotIn('secret', self.output)
                self.assertEqual(result['remote_host_mapping'], 'unknown')

    def test_diagnostic_does_not_write_database_or_create_runtime_artifacts(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.run_status(url=None)
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['probe.sqlite'])


if __name__ == '__main__':
    unittest.main()
