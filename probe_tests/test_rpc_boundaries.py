"""RPC layer boundaries exercised with synthetic state and blocked networking."""
import base64
import contextlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from probe import callback_transport, probe as bridge, rpc_protocol


HOST = 'callback.example.test'
URL = 'https://' + HOST + '/synthetic?cursor=fixture'


class CallbackBoundaryTests(unittest.TestCase):
    def test_pinned_tls_connects_to_validated_ip_with_original_sni(self):
        connection = callback_transport.PinnedHTTPS(HOST, '8.8.8.8')
        raw_socket, wrapped_socket = Mock(), Mock()
        connection._context = Mock()
        connection._context.wrap_socket.return_value = wrapped_socket
        with patch.object(socket, 'create_connection', return_value=raw_socket) as connect:
            connection.connect()
        connect.assert_called_once_with(('8.8.8.8', 443), timeout=10)
        connection._context.wrap_socket.assert_called_once_with(raw_socket, server_hostname=HOST)
        self.assertIs(connection.sock, wrapped_socket)
        connection.close()
        wrapped_socket.close.assert_called_once_with()

    def test_failed_tls_closes_unwrapped_socket(self):
        connection = callback_transport.PinnedHTTPS(HOST, '8.8.8.8')
        raw_socket = Mock()
        connection._context = Mock()
        connection._context.wrap_socket.side_effect = OSError('synthetic TLS failure')
        with patch.object(socket, 'create_connection', return_value=raw_socket):
            with self.assertRaises(OSError):
                connection.connect()
        raw_socket.close.assert_called_once_with()

    def test_public_connection_factory_seam_keeps_path_and_response_limit(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        response = Mock(status=200)
        connection = Mock()
        connection.getresponse.return_value = response
        for payload, oversized in ((b'{}', False), (b'x' * 262145, True)):
            connection.reset_mock()
            response.read.return_value = payload
            with self.subTest(oversized=oversized), \
                    patch.object(socket, 'getaddrinfo', return_value=answers), \
                    patch.object(bridge, 'PinnedHTTPS', return_value=connection) as factory:
                if oversized:
                    with self.assertRaises(bridge.RpcError) as error:
                        bridge.post_https(URL, b'{}', {'fixture': 'header'}, {HOST})
                    self.assertEqual(error.exception.reason, 'response_too_large')
                else:
                    self.assertEqual(bridge.post_https(URL, b'{}', {'fixture': 'header'}, {HOST}),
                                     (200, b'{}'))
                factory.assert_called_once_with(HOST, '8.8.8.8')
                connection.request.assert_called_once_with(
                    'POST', '/synthetic?cursor=fixture', body=b'{}', headers={'fixture': 'header'})
                connection.close.assert_called_once_with()
        self.assertIs(bridge.RpcError, rpc_protocol.RpcError)

    def test_default_probe_transport_uses_public_post_seam(self):
        secret = 'whsec_' + base64.b64encode(b'a' * 32).decode()

        def receive(url, body, headers, allowed_hosts):
            self.assertEqual((url, allowed_hosts), (URL, {HOST}))
            return 200, json.dumps({'challenge': json.loads(body)['challenge']}).encode()

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(socket, 'getaddrinfo', side_effect=AssertionError('network forbidden')), \
                patch.object(bridge, 'post_https', side_effect=receive) as post:
            probe = bridge.Probe(directory, allowed_hosts=[HOST])
            try:
                subscription = probe.subscribe({'name': 'test.ping', 'arguments': {},
                    'delivery': {'mode': 'webhook', 'url': URL, 'secret': secret}})
                self.assertTrue(subscription['id'].startswith('sub_'))
                post.assert_called_once()
            finally:
                probe.close()


class RuntimeBoundaryTests(unittest.TestCase):
    def test_imports_and_schema_do_not_construct_stores_start_workers_or_disable_network(self):
        script = '''
import socket, sqlite3, threading
def forbidden(*args, **kwargs):
    raise AssertionError('import performed runtime I/O')
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
sqlite3.connect = forbidden
threading.Thread = forbidden
from probe import probe, rpc_protocol, callback_transport, rpc_diagnostics, rpc_schema, rpc_dispatch, service_runtime
assert socket.create_connection is forbidden
assert socket.getaddrinfo is forbidden
assert probe.RpcError is rpc_protocol.RpcError
assert probe.Probe.tools()
assert probe.Probe.event()['name'] == 'test.ping'
print('import-only boundary verified')
'''
        run = subprocess.run([sys.executable, '-B', '-c', script],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), 'import-only boundary verified')

    def test_oversize_line_is_drained_and_public_worker_seams_are_preserved(self):
        workers = []

        class NoBackgroundThread:
            def __init__(self, *, target, args=(), **unused):
                workers.append((target, args))

            def start(self):
                pass

            def join(self, **unused):
                pass

            def is_alive(self):
                return False

        ping = {'jsonrpc': '2.0', 'id': 17, 'method': 'ping',
                'params': {'private': 'PRIVATE_BODY_CANARY'}}
        notice = {'jsonrpc': '2.0', 'method': 'notifications/initialized'}
        raw = (b'x' * (bridge.MAX_RPC_BYTES * 2 + 1) + b'\n{invalid\n' +
               json.dumps(ping).encode() + b'\n' + json.dumps(notice).encode() + b'\n')
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            stack.enter_context(patch.object(bridge.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw))))
            stack.enter_context(patch.object(bridge.sys, 'argv', ['probe.py', '--state-dir', directory]))
            stack.enter_context(patch.object(bridge.threading, 'Thread', NoBackgroundThread))
            stack.enter_context(patch.object(bridge.signal, 'signal'))
            stack.enter_context(patch.object(socket, 'getaddrinfo', side_effect=AssertionError('network forbidden')))
            stack.enter_context(patch.object(socket, 'create_connection', side_effect=AssertionError('network forbidden')))
            constructor = stack.enter_context(patch.object(bridge, 'Probe', wraps=bridge.Probe))
            lock = stack.enter_context(patch.object(bridge, 'acquire_worker_lock', return_value=Mock()))
            diagnostics = stack.enter_context(patch.object(bridge, 'record_rpc_diagnostic', wraps=bridge.record_rpc_diagnostic))
            self.assertEqual(bridge.main(), 0)
            constructor.assert_called_once_with(directory, allowed_hosts=[])
            lock.assert_called_once_with(directory)
            self.assertIs(workers[0][0], bridge.run_event_pump)
            self.assertIs(workers[1][0], bridge.run_weixin_sender)
            self.assertEqual(diagnostics.call_count, 4)
            replies = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([reply.get('error', {}).get('code') for reply in replies],
                             [-32600, -32700, None])
            self.assertEqual(replies[-1], {'jsonrpc': '2.0', 'id': 17, 'result': {}})
            diagnostic_text = (Path(directory) / 'rpc-diagnostics.jsonl').read_text()
            self.assertNotIn('PRIVATE_BODY_CANARY', diagnostic_text)

    def test_public_response_limit_bounds_large_image_and_request_id(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = bridge.Probe(directory)
            try:
                with patch.object(bridge, 'MAX_ATTACHMENT_RPC_BYTES', 512), \
                        patch.object(probe.media, 'read_image', return_value=(
                            {}, {'type': 'image', 'mimeType': 'image/jpeg', 'data': 'A' * 1024})):
                    result = probe.rpc({'jsonrpc': '2.0', 'id': 'r' * 1024, 'method': 'tools/call',
                        'params': {'name': 'read_attachment_image', 'arguments': {
                            'message_id': 'synthetic-message', 'attachment_id': 'synthetic-image'}}})
                self.assertIsNone(result['id'])
                self.assertEqual(result['error']['data']['reason'], 'attachment_response_too_large')
                self.assertLessEqual(len(bridge.canonical(result).encode()), 512)
            finally:
                probe.close()


if __name__ == '__main__':
    unittest.main()
