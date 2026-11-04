"""CLI routing only: no real connection, credential, business worker or runtime."""
import base64
from contextlib import ExitStack
import io
import json
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from probe import probe as module


ROUTE = 'c' * 64
SUBSCRIPTION = 'sub_' + ROUTE[:32]
HOST = 'connection-only.example.test'


class ConnectionOnlyCliTests(unittest.TestCase):
    def arguments(self, *extra):
        return ['probe.py', '--connection-only', '--state-dir', '/unused-fixture-state',
                '--connection-route-digest', ROUTE,
                '--connection-subscription-id', SUBSCRIPTION,
                '--allow-callback-host', HOST, *extra]

    def invoke(self, argv, *, result=0, failure=None):
        transport_module = types.ModuleType('probe.connection_only')
        transport_module.serve = Mock(return_value=result, side_effect=failure)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {'probe.connection_only': transport_module}))
            stack.enter_context(patch.object(sys, 'argv', argv))
            stderr = stack.enter_context(patch.object(sys, 'stderr', new_callable=io.StringIO))
            stack.enter_context(patch.object(module.os, 'umask'))
            constructor = stack.enter_context(patch.object(module, 'Probe', side_effect=AssertionError('business constructor forbidden')))
            worker_lock = stack.enter_context(patch.object(module, 'acquire_worker_lock', side_effect=AssertionError('serve owns its lock')))
            threads = stack.enter_context(patch.object(module.threading, 'Thread', side_effect=AssertionError('business threads forbidden')))
            database = stack.enter_context(patch.object(module.sqlite3, 'connect', side_effect=AssertionError('main must not open state')))
            try:
                returned = module.main()
            except SystemExit as stopped:
                returned = ('parse_exit', stopped.code)
        constructor.assert_not_called()
        worker_lock.assert_not_called()
        threads.assert_not_called()
        database.assert_not_called()
        return returned, transport_module.serve, stderr.getvalue()

    def test_connection_branch_calls_only_bounded_serve(self):
        returned, serve, _ = self.invoke(self.arguments(), result=7)
        self.assertEqual(returned, 7)
        serve.assert_called_once_with('/unused-fixture-state', [HOST], ROUTE, SUBSCRIPTION,
                                      duration_seconds=180)

    def test_explicit_supported_duration_reaches_serve(self):
        for duration in (1, 90, 180):
            with self.subTest(duration=duration):
                returned, serve, _ = self.invoke(self.arguments('--connection-duration-seconds', str(duration)))
                self.assertEqual(returned, 0)
                self.assertEqual(serve.call_args.kwargs['duration_seconds'], duration)

    def test_unsafe_duration_is_rejected_before_any_state(self):
        for duration in ('0', '-1', '181', '1.5', 'unbounded'):
            with self.subTest(duration=duration):
                returned, serve, _ = self.invoke(self.arguments('--connection-duration-seconds', duration))
                self.assertEqual(returned, ('parse_exit', 2))
                serve.assert_not_called()

    def test_all_business_and_diagnostic_modes_are_mutually_exclusive(self):
        for option in ('--fresh-production', '--offline', '--status', '--emit'):
            with self.subTest(option=option):
                returned, serve, _ = self.invoke(self.arguments(option))
                self.assertEqual(returned, ('parse_exit', 2))
                serve.assert_not_called()

    def test_scoping_options_cannot_be_used_without_connection_mode(self):
        for flags in (['--connection-route-digest', ROUTE],
                      ['--connection-subscription-id', SUBSCRIPTION],
                      ['--connection-duration-seconds', '180']):
            with self.subTest(option=flags[0]):
                returned, serve, _ = self.invoke(['probe.py', *flags])
                self.assertEqual(returned, ('parse_exit', 2))
                serve.assert_not_called()

    def test_original_route_identity_is_required_and_must_match(self):
        cases = [
            ['probe.py', '--connection-only', '--allow-callback-host', HOST],
            self.arguments('--connection-route-digest', 'not-a-sha256'),
            self.arguments('--connection-subscription-id', 'sub_' + 'd' * 32),
            self.arguments('--connection-route-digest', ROUTE.upper()),
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                returned, serve, _ = self.invoke(argv)
                self.assertEqual(returned, ('parse_exit', 2))
                serve.assert_not_called()

    def test_exactly_one_callback_host_is_required(self):
        no_host = self.arguments()
        del no_host[-2:]
        for argv in (no_host, self.arguments('--allow-callback-host', HOST),
                     self.arguments('--allow-callback-host', 'another.example.test')):
            with self.subTest(argv=argv):
                returned, serve, _ = self.invoke(argv)
                self.assertEqual(returned, ('parse_exit', 2))
                serve.assert_not_called()

    def test_validation_error_is_redacted_and_never_retried(self):
        for failure in (OSError('private fixture detail'), ValueError('private fixture detail'),
                        module.WeixinError('private_fixture_detail'),
                        module.RpcError(-32000, 'private fixture detail')):
            with self.subTest(kind=type(failure).__name__):
                returned, serve, stderr = self.invoke(self.arguments(), failure=failure)
                self.assertEqual(returned, 1)
                serve.assert_called_once()
                self.assertIn('no business workers started', stderr)
                self.assertNotIn('private', stderr)

    def test_normal_status_path_remains_the_existing_probe_path(self):
        ordinary = Mock()
        ordinary.fresh_admission = None
        ordinary.status.return_value = {'ordinary_status': True}
        with patch.object(module, 'Probe', return_value=ordinary) as constructor, \
                patch.object(sys, 'argv', ['probe.py', '--status', '--state-dir', '/unused-fixture-state']), \
                patch.object(sys, 'stdout', new_callable=io.StringIO) as output, \
                patch.object(module.os, 'umask'), \
                patch.object(module.threading, 'Thread') as threads:
            self.assertEqual(module.main(), 0)
        constructor.assert_called_once_with('/unused-fixture-state', allowed_hosts=[])
        ordinary.status.assert_called_once()
        ordinary.close.assert_called_once()
        threads.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {'ordinary_status': True})


class ForcedSubscriptionChallengeTests(unittest.TestCase):
    def test_explicit_force_verification_skips_cache_without_changing_default(self):
        calls = []
        def receiver(url, body, headers):
            challenge = json.loads(body)
            calls.append(challenge)
            return 200, json.dumps({'challenge': challenge['challenge']}).encode()
        params = {'name': 'weixin.message', 'arguments': {}, 'ttlMs': None,
                  'delivery': {'mode': 'webhook', 'url': 'https://' + HOST + '/events',
                               'secret': 'whsec_' + base64.b64encode(b'c' * 32).decode()}}
        with tempfile.TemporaryDirectory() as directory:
            probe = module.Probe(directory, allowed_hosts=[HOST], post=receiver,
                                 clock=lambda: 1800000000.0)
            try:
                original = probe.subscribe(params)
                self.assertEqual(probe.subscribe(params), original)
                self.assertEqual(len(calls), 1)
                self.assertEqual(probe.subscribe(params, force_verification=True), original)
                self.assertEqual(len(calls), 2)
                self.assertNotEqual(calls[0]['challenge'], calls[1]['challenge'])
            finally:
                probe.close()


if __name__ == '__main__':
    unittest.main()
