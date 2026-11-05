"""Synthetic protocol and current provider integration; never launches Codex."""
import io
import json
import os
import selectors
import signal
import subprocess
import time
import unittest
from unittest.mock import Mock, patch

from probe.app_server_usage_rpc import (
    AppServerUsageError, AppServerUsageRpc, StdioAppServerSession,
)
from probe.official_usage_provider import OfficialUsageProvider, OfficialReadDenied
from probe.usage_display import UsageBinding
from probe_tests.test_fixed_command_handler import HandlerFixture
from probe.owner_coordinator import OwnerCoordinator


BINDING = UsageBinding('synthetic-owner', 'synthetic-account', True)
ACCOUNT = {'type': 'chatgpt', 'email': 'fixture@example.invalid',
           'planType': 'pro', 'workspaceName': None}
BUCKET = {'limitId': 'codex', 'primary': {
    'usedPercent': 30, 'windowDurationMins': 10080, 'resetsAt': 1000},
    'secondary': None}
QUOTA = {'rateLimits': BUCKET, 'rateLimitsByLimitId': {'codex': BUCKET},
         'accountId': 'fixture-official-account', 'ordinaryUsageAllowed': True}


class SyntheticSession:
    def __init__(self, account=ACCOUNT, result=QUOTA, after=ACCOUNT):
        self.account, self.after, self.result = account, after, result
        self.calls, self.closed = [], False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'account/read':
            return {'account': self.account if len(self.calls) == 1 else self.after,
                    'requiresOpenaiAuth': True}
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class UsageRpcTests(unittest.TestCase):
    def setUp(self):
        self.network = patch('socket.socket.connect', side_effect=AssertionError('offline test'))
        self.network.start(); self.addCleanup(self.network.stop)
        self.popen = patch('subprocess.Popen', side_effect=AssertionError('no process in unit tests'))
        self.popen.start(); self.addCleanup(self.popen.stop)

    def adapter(self, session, verifier=None, **kwargs):
        return AppServerUsageRpc(lambda: session, binding=BINDING,
            account_matches=verifier or (lambda binding, account: account == ACCOUNT), **kwargs)

    def test_current_provider_receives_windows_without_identity_or_unrelated_fields(self):
        session = SyntheticSession()
        rpc = self.adapter(session, official_account_id='fixture-official-account')
        provider = OfficialUsageProvider.from_existing_app_server(
            rpc, BINDING, 'synthetic-independent-binding-proof', clock=lambda: 100)
        result = provider.prepare(BINDING)
        self.assertEqual(result['status'], 'reported')
        self.assertIn('剩余70%，已用30%', result['text'])
        self.assertEqual(len(result['text'].splitlines()), 4)
        self.assertEqual(session.calls, [
            ('account/read', {'refreshToken': False}), ('account/rateLimits/read', {}),
            ('account/read', {'refreshToken': False})])
        self.assertTrue(session.closed)
        clean = self.adapter(SyntheticSession())('account/rateLimits/read', {})
        self.assertEqual(set(clean), {'rateLimits', 'rateLimitsByLimitId'})
        self.assertNotIn('fixture@example.invalid', json.dumps(result))

    def test_invalid_binding_and_untrusted_verifier_stop_before_session(self):
        factory = Mock()
        for binding in (None, UsageBinding('owner', 'account', False)):
            with self.assertRaisesRegex(AppServerUsageError, 'host_binding_required'):
                AppServerUsageRpc(factory, binding=binding, account_matches=Mock())
        factory.assert_not_called()

    def test_other_methods_or_params_never_open_session(self):
        factory = Mock()
        rpc = AppServerUsageRpc(factory, binding=BINDING, account_matches=Mock())
        for method, params in [('account/login/start', {}), ('thread/start', {}),
                               ('account/read', {}), ('account/rateLimits/read', {'force': True}),
                               ('account/rateLimits/read', None)]:
            with self.assertRaises(AppServerUsageError):
                rpc(method, params)
        factory.assert_not_called()

    def test_missing_apikey_or_wrong_account_never_reads_quota(self):
        for account in (None, {'type': 'apiKey'}, {**ACCOUNT, 'email': 'other@example.invalid'}):
            session = SyntheticSession(account=account)
            with self.assertRaises(AppServerUsageError):
                self.adapter(session)('account/rateLimits/read', {})
            self.assertEqual(len(session.calls), 1)
            self.assertTrue(session.closed)

    def test_verifier_requires_literal_true(self):
        for value in (False, 1, 'true', None):
            session = SyntheticSession()
            with self.assertRaisesRegex(AppServerUsageError, 'account_mismatch'):
                self.adapter(session, lambda *_: value)('account/rateLimits/read', {})
            self.assertEqual(len(session.calls), 1)

    def test_known_usage_account_id_missing_or_changed_drops_result(self):
        for identity in (None, 'other'):
            session = SyntheticSession(result={**QUOTA, 'accountId': identity})
            with self.assertRaisesRegex(AppServerUsageError, 'usage_account_mismatch'):
                self.adapter(session, official_account_id='fixture-official-account')(
                    'account/rateLimits/read', {})
            self.assertTrue(session.closed)

    def test_visible_account_change_drops_result(self):
        session = SyntheticSession(after={**ACCOUNT, 'workspaceName': 'different'})
        with self.assertRaisesRegex(AppServerUsageError, 'account_changed'):
            self.adapter(session)('account/rateLimits/read', {})
        self.assertTrue(session.closed)

    def test_failure_is_sanitized_no_fallback_or_second_read(self):
        session = SyntheticSession(result=RuntimeError('PRIVATE UPSTREAM VALUE'))
        rpc = self.adapter(session)
        with self.assertRaisesRegex(AppServerUsageError, '^app_server_read_failed$'):
            rpc('account/rateLimits/read', {})
        self.assertEqual(len(session.calls), 2)
        self.assertTrue(session.closed)

    def test_trusted_permission_refusal_preserves_existing_provider_stop(self):
        session = SyntheticSession(result=OfficialReadDenied('fixture-denied'))
        provider = OfficialUsageProvider.from_existing_app_server(
            self.adapter(session), BINDING, 'synthetic-proof', clock=lambda: 100)
        self.assertEqual(provider.prepare(BINDING)['status'], 'permission_denied')
        self.assertEqual(len(session.calls), 2)

    def test_cleanup_error_or_suppression_cannot_downgrade_actual_denial(self):
        for cleanup_fails in (True, False):
            class DeniedSession(SyntheticSession):
                def __exit__(self, *_):
                    self.closed = True
                    if cleanup_fails:
                        raise subprocess.TimeoutExpired('synthetic', 1)
                    return True
            session = DeniedSession(result=OfficialReadDenied('fixture-denied'))
            provider = OfficialUsageProvider.from_existing_app_server(
                self.adapter(session), BINDING, 'synthetic-proof', clock=lambda: 100)
            self.assertEqual(provider.prepare(BINDING)['status'], 'permission_denied')
            self.assertTrue(session.closed)
            self.assertEqual(len(session.calls), 2)

    def test_cleanup_failure_discards_successful_usage_result(self):
        class CleanupFailure(SyntheticSession):
            def __exit__(self, *_):
                self.closed = True
                raise subprocess.TimeoutExpired('synthetic', 1)
        provider = OfficialUsageProvider.from_existing_app_server(
            self.adapter(CleanupFailure()), BINDING, 'synthetic-proof', clock=lambda: 100)
        result = provider.prepare(BINDING)
        self.assertEqual(result['status'], 'read_failed')
        self.assertNotIn('70%', result['text'])

    def test_missing_windows_are_unknown_not_replaced_from_cache(self):
        provider = OfficialUsageProvider.from_existing_app_server(
            self.adapter(SyntheticSession(result={'rateLimitsByLimitId': {'other': BUCKET}})),
            BINDING, 'synthetic-proof', clock=lambda: 100)
        result = provider.prepare(BINDING)
        self.assertNotEqual(result['status'], 'reported')
        self.assertNotIn('70%', result['text'])

    def test_error_result_cannot_be_hidden_by_quota_field_projection(self):
        for error in ({'error': {'message': 'PRIVATE'}}, {'isError': True}):
            session = SyntheticSession(result={**QUOTA, **error})
            with self.assertRaisesRegex(AppServerUsageError, '^app_server_invalid_result$'):
                self.adapter(session)('account/rateLimits/read', {})
            self.assertTrue(session.closed)
            self.assertEqual(len(session.calls), 2)


class StdioProtocolTests(unittest.TestCase):
    def setUp(self):
        self.proc = Mock(pid=424242, stdin=io.BytesIO(), stdout=io.BytesIO())
        self.proc.wait.return_value = 0
        self.launch = patch('probe.app_server_usage_rpc.subprocess.Popen', return_value=self.proc)
        self.launch_mock = self.launch.start(); self.addCleanup(self.launch.stop)
        self.kill = patch('probe.app_server_usage_rpc.os.killpg')
        self.kill_mock = self.kill.start(); self.addCleanup(self.kill.stop)

    def session(self, messages):
        session = StdioAppServerSession('/synthetic/trusted/codex', start_permit=lambda _: True)
        session.process = self.proc
        session.deadline = time.monotonic() + 5
        session._line = Mock(side_effect=[json.dumps(x).encode() for x in messages])
        self.addCleanup(session.close)
        return session

    def test_constructing_adapter_has_no_process_side_effect(self):
        permit = Mock(return_value=True)
        StdioAppServerSession('/synthetic/trusted/codex', start_permit=permit)
        self.launch_mock.assert_not_called()
        permit.assert_not_called()

    def test_start_requires_live_literal_true_permit_before_process(self):
        for value in (False, None, 0, 1, 'true', {}):
            permit = Mock(return_value=value)
            session = StdioAppServerSession('/synthetic/trusted/codex', start_permit=permit)
            with self.assertRaisesRegex(AppServerUsageError, 'start_not_authorized'):
                session.__enter__()
            permit.assert_called_once_with('/synthetic/trusted/codex')
        with self.assertRaisesRegex(AppServerUsageError, 'start_permit_required'):
            StdioAppServerSession('/synthetic/trusted/codex', start_permit=True)
        with self.assertRaisesRegex(AppServerUsageError, 'must_be_absolute'):
            StdioAppServerSession('codex', start_permit=lambda _: True)
        self.launch_mock.assert_not_called()

    def test_actual_start_denial_preserves_provider_stop_without_spawn(self):
        permit = Mock(side_effect=OfficialReadDenied('fixture-denied'))
        rpc = AppServerUsageRpc(lambda: StdioAppServerSession('/synthetic/trusted/codex',
            start_permit=permit), binding=BINDING, account_matches=lambda *_: True)
        provider = OfficialUsageProvider.from_existing_app_server(rpc, BINDING,
            'synthetic-host-proof', clock=lambda: 100)
        self.assertEqual(provider.prepare(BINDING)['status'], 'permission_denied')
        self.launch_mock.assert_not_called()

    def test_initialize_is_fixed_and_total_request_count_is_bounded(self):
        session = self.session([{'id': n, 'result': {}} for n in range(1, 5)])
        for params in ({}, {'arbitrary': 'x' * 1048576}, {'clientInfo': {
                'name': 'dot_wechat_usage_reader', 'version': '1.0.0'},
                'capabilities': {'experimentalApi': 0}}):
            with self.assertRaisesRegex(AppServerUsageError, 'params_not_allowed'):
                session.request('initialize', params)
        self.assertEqual(self.proc.stdin.getvalue(), b'')
        for _ in range(4):
            session.request('account/read', {'refreshToken': False})
        before = self.proc.stdin.getvalue()
        with self.assertRaisesRegex(AppServerUsageError, 'request_limit'):
            session.request('account/read', {'refreshToken': False})
        self.assertEqual(self.proc.stdin.getvalue(), before)

    def test_handshake_is_jsonl_without_jsonrpc_no_login_or_extra_args(self):
        session = StdioAppServerSession('/synthetic/trusted/codex', start_permit=lambda _: True)
        with patch('probe.app_server_usage_rpc.selectors.DefaultSelector'), \
             patch.object(session, '_line', return_value=b'{"id":1,"result":{}}'):
            session.__enter__()
        sent = [json.loads(x) for x in self.proc.stdin.getvalue().splitlines()]
        self.assertEqual([x['method'] for x in sent], ['initialize', 'initialized'])
        self.assertTrue(all('jsonrpc' not in x for x in sent))
        self.assertEqual(self.launch_mock.call_args.args[0], ['/synthetic/trusted/codex', 'app-server'])
        self.assertIs(self.launch_mock.call_args.kwargs['stderr'], subprocess.DEVNULL)
        session.close()
        self.assertEqual([call.args for call in self.kill_mock.call_args_list],
                         [(424242, signal.SIGTERM), (424242, signal.SIGKILL)])
        with self.assertRaisesRegex(AppServerUsageError, 'session_already_started'):
            session.__enter__()
        self.launch_mock.assert_called_once()

    def test_startup_account_notification_is_followed_by_verified_account_read(self):
        session = self.session([{'method': 'account/updated', 'params': {'authMode': 'chatgpt'}},
                                {'id': 1, 'result': {'account': ACCOUNT}}])
        result = session.request('account/read', {'refreshToken': False})
        self.assertEqual(result['account'], ACCOUNT)
        self.assertFalse(session.account_verified)

    def test_notifications_skipped_but_account_change_and_server_requests_stop(self):
        session = self.session([{'method': 'some/notification'}, {'id': 1, 'result': QUOTA}])
        self.assertEqual(session.request('account/rateLimits/read', {}), QUOTA)
        for payload, error in [({'method': 'account/updated'}, 'account_update_observed'),
                               ({'method': 'approval/request', 'id': 'server'}, 'server_request')]:
            other = self.session([payload])
            with self.assertRaisesRegex(AppServerUsageError, error):
                other.request('account/rateLimits/read', {})

    def test_mismatched_id_rpc_error_and_invalid_result_are_sanitized(self):
        for payload, error in [({'id': 2, 'result': {}}, 'id_mismatch'),
                               ({'id': True, 'result': {}}, 'id_mismatch'),
                               ({'id': 1, 'error': {'code': -32603, 'message': 'PRIVATE'}}, 'rpc_error'),
                               ({'id': 1, 'result': None}, 'invalid_result')]:
            session = self.session([payload])
            with self.assertRaisesRegex(AppServerUsageError, error) as caught:
                session.request('account/rateLimits/read', {})
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_account_read_does_not_allow_truthy_or_integer_refresh_flag(self):
        session = self.session([])
        for value in (True, 0, None, 'false'):
            with self.assertRaisesRegex(AppServerUsageError, 'params_not_allowed'):
                session.request('account/read', {'refreshToken': value})
        self.assertEqual(self.proc.stdin.getvalue(), b'')

    def test_handshake_failure_cleans_up_and_timeout_kills_owned_group(self):
        session = StdioAppServerSession('/synthetic/trusted/codex', start_permit=lambda _: True)
        self.proc.wait.side_effect = [subprocess.TimeoutExpired('synthetic', 1), 0]
        with patch('probe.app_server_usage_rpc.selectors.DefaultSelector'), \
             patch.object(session, '_line', side_effect=AppServerUsageError('app_server_read_timeout')):
            with self.assertRaisesRegex(AppServerUsageError, 'read_timeout'):
                session.__enter__()
        self.assertIsNone(session.process)
        self.assertEqual(self.kill_mock.call_args_list[0].args, (424242, signal.SIGTERM))
        self.assertEqual(self.kill_mock.call_args_list[1].args, (424242, signal.SIGKILL))

    def test_real_pipe_line_bound_and_timeout_without_process_or_network(self):
        for payload, error in [(b'x' * 1025, 'response_too_large'), (b'', 'read_timeout')]:
            read_fd, write_fd = os.pipe()
            stream = os.fdopen(read_fd, 'rb', buffering=0)
            session = StdioAppServerSession('/synthetic/trusted/codex', start_permit=lambda _: True, timeout=.02, max_line_bytes=1024)
            session.process = Mock(stdout=stream)
            session.selector = selectors.DefaultSelector()
            session.selector.register(stream, selectors.EVENT_READ)
            session.deadline = time.monotonic() + .02
            try:
                if payload:
                    os.write(write_fd, payload)
                with self.assertRaisesRegex(AppServerUsageError, error):
                    session._line()
            finally:
                session.selector.close(); stream.close(); os.close(write_fd)
                session.process = None


class ExistingCoordinatorIntegrationTests(HandlerFixture, unittest.TestCase):
    def open(self):
        self.rpc_sessions = []
        def session_factory():
            session = SyntheticSession()
            self.rpc_sessions.append(session)
            return session
        rpc = AppServerUsageRpc(session_factory, binding=self.binding,
            account_matches=lambda binding, account: binding == self.binding and account == ACCOUNT)
        provider = OfficialUsageProvider.from_existing_app_server(
            rpc, self.binding, 'synthetic-current-host-proof', clock=lambda: 100)
        self.c = OwnerCoordinator(self.directory, coordinator_id='fixture-coordinator',
            caller_id='fixture-coordinator', call_tool=self.host, mode='execute',
            permit=self.permit, usage_provider=provider)

    def test_current_fixed_handler_calls_rpc_once_and_keeps_original_final(self):
        self.open()
        first = self.run_handler()
        second = self.run_handler()
        self.assertEqual(first['usage_status'], 'reported')
        self.assertEqual(first['send_id'], second['send_id'])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(self.rpc_sessions), 1)
        self.assertEqual(len(self.rpc_sessions[0].calls), 3)

    def test_help_and_existing_unknown_never_open_app_server(self):
        self.open()
        self.message['text'] = '/help'
        self.assertEqual(self.run_handler()['fixed_command'], 'help')
        self.assertEqual(self.rpc_sessions, [])
        self.close(); self.open()
        self.message['message_id'] = 'fixture-unresolved-message'
        self.scope['request_ref'] = self.message['message_id']
        self.message['text'] = '额度'
        self.seed('final', 'unknown')
        self.assertEqual(self.run_handler()['action'], 'reconcile_original_submission')
        self.assertEqual(self.rpc_sessions, [])


if __name__ == '__main__':
    unittest.main()
