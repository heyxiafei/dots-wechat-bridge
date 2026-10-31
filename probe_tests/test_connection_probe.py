import builtins
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from probe import connection_probe as check


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/connection-probe.py'


def request(method, params=None, request_id=1):
    value = {'jsonrpc': '2.0', 'id': request_id, 'method': method}
    if params is not None:
        value['params'] = params
    return value


def lines(*values):
    return ''.join(json.dumps(value) + '\n' for value in values)


class ConnectionProbeTests(unittest.TestCase):
    def cli(self, args, body=''):
        return subprocess.run([sys.executable, '-B', str(SCRIPT), *args],
                              input=body, text=True, capture_output=True, timeout=4,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))

    def test_default_is_plan_without_process_identity_or_state_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-B', str(SCRIPT)], cwd=directory,
                                    input='', text=True, capture_output=True, timeout=4)
            value = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(value['mode'], 'plan')
            self.assertFalse(value['stdio_started'])
            self.assertFalse(value['authorization_granted'])
            self.assertNotIn('instance_id', value)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertNotEqual(self.cli(['--stdio', '--state-dir', '/unused']).returncode, 0)
        rejected = self.cli(['--unused-secret', 'private-argument-fixture'])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertNotIn('private-argument-fixture', rejected.stdout + rejected.stderr)

    def test_real_pipe_discovery_status_and_eof(self):
        result = self.cli(['--stdio'], lines(
            request('initialize', {'protocolVersion': check.VERSION}),
            request('server/discover', request_id=2), request('tools/list', request_id=3),
            request('tools/call', {'name': 'get_weixin_status', 'arguments': {}}, 4)))
        self.assertEqual(result.returncode, 0)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 4)
        self.assertNotIn('events', rows[0]['result']['capabilities'])
        self.assertNotIn('events', rows[1]['result']['capabilities'])
        self.assertEqual([x['name'] for x in rows[2]['result']['tools']], ['get_weixin_status'])
        value = rows[3]['result']['structuredContent']
        self.assertEqual(value['mode'], 'read_only_mcp_connection_check')
        self.assertEqual(value['binding'], 'not_read')
        self.assertEqual(value['weixin_authentication'], 'not_checked')
        self.assertNotIn('connected', value)
        marker = json.loads(result.stderr)
        self.assertEqual(value['instance_id'], marker['instance_id'])
        self.assertRegex(value['instance_id'], r'^mcp-check-[a-f0-9]{32}$')

    def test_every_write_and_message_read_rejected_without_raw_data_echo(self):
        marker = 'private-payload-fixture'
        requests = [request('events/subscribe', {'delivery': {'url': marker}}, 1),
                    request('events/unsubscribe', {'delivery': {'url': marker}}, 2),
                    request('events/list', {}, 3)]
        for index, name in enumerate(('reply_weixin_message', 'reply_weixin_message_phase',
                                      'read_weixin_message', 'read_attachment_chunk',
                                      'send_weixin_media', 'acknowledge_test_ping'), 4):
            requests.append(request('tools/call', {'name': name, 'arguments': {'text': marker}}, index))
        result = self.cli(['--stdio'], lines(*requests))
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertTrue(all('error' in json.loads(line) for line in result.stdout.splitlines()))

    def test_notifications_invalid_ids_and_extra_arguments_cannot_enable_operations(self):
        value = request('events/subscribe', {'name': 'weixin.message'})
        del value['id']
        self.assertIsNone(check.rpc(value, 'fixture-process'))
        self.assertIsNone(check.rpc({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, 'fixture-process'))
        for value in (None, [], request('ping', request_id=True), request('ping', request_id=None),
                      request('tools/call', {'name': 'get_weixin_status', 'arguments': {'state': '/unused'}}),
                      request('tools/call', {'name': 'get_weixin_status', 'arguments': {}, 'approve': True})):
            self.assertIn('error', check.rpc(value, 'fixture-process'))

    def test_standard_metadata_is_inert_but_tool_arguments_stay_empty(self):
        marker = 'private-metadata-fixture'
        for method, params in (
                ('ping', {'_meta': {'progressToken': marker}}),
                ('tools/list', {'_meta': {'progressToken': marker}}),
                ('tools/call', {'name': 'get_weixin_status', 'arguments': {},
                                '_meta': {'progressToken': marker, 'approve': True}})):
            result = check.rpc(request(method, params), 'fixture-process')
            self.assertIn('result', result)
            self.assertNotIn(marker, json.dumps(result))
            self.assertIn('_meta', params)  # Original request not mutated.
        for params in ({'name': 'get_weixin_status', '_meta': 'not-an-object'},
                       {'name': 'get_weixin_status', '_meta': {}, 'arguments': {'approve': True}}):
            self.assertIn('error', check.rpc(request('tools/call', params), 'fixture-process'))

    @unittest.skipUnless(os.name == 'posix', 'POSIX finite pipe entry')
    def test_deadline_with_open_stdin_and_partial_frame(self):
        process = subprocess.Popen([sys.executable, '-B', str(SCRIPT), '--stdio', '--duration-seconds', '1'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        started = time.monotonic()
        try:
            process.stdin.write(b'{"unfinished":'); process.stdin.flush()
            self.assertEqual(process.wait(timeout=3), 0)
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(process.stdout.read(), b'')
        finally:
            if process.poll() is None:
                process.kill(); process.wait()
            process.stdin.close(); process.stdout.close(); process.stderr.close()

    @unittest.skipUnless(os.name == 'posix', 'POSIX finite pipe entry')
    def test_deadline_with_stdout_pipe_never_read_by_parent(self):
        # A real pipe fills while the child emits diagnostic status responses.
        # Do not use communicate(): it would drain stdout and conceal the bug.
        with tempfile.TemporaryFile() as fixture:
            fixture.write(lines(*[request('tools/call', {'name': 'get_weixin_status'}, n)
                                  for n in range(check.MAX_REQUESTS)]).encode())
            fixture.seek(0)
            process = subprocess.Popen(
                [sys.executable, '-B', str(SCRIPT), '--stdio', '--duration-seconds', '1'],
                stdin=fixture, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            started = time.monotonic()
            try:
                self.assertEqual(process.wait(timeout=3), 0)
                elapsed = time.monotonic() - started
                self.assertGreater(elapsed, .6)
                self.assertLess(elapsed, 3)
                self.assertIn(b'read_only_mcp_connection_check', process.stdout.read())
                self.assertNotIn(b'Traceback', process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
                process.stdout.close(); process.stderr.close()

    @unittest.skipUnless(os.name == 'posix', 'POSIX finite pipe entry')
    def test_serve_does_not_open_files_databases_network_or_subprocesses(self):
        read_fd, write_fd = os.pipe()
        os.write(write_fd, lines(request('tools/call', {'name': 'get_weixin_status'})).encode())
        os.close(write_fd)
        output, errors = io.StringIO(), io.StringIO()
        blocked = AssertionError('runtime or external operation forbidden')
        try:
            with patch.object(builtins, 'open', side_effect=blocked), \
                    patch.object(os, 'open', side_effect=blocked), \
                    patch.object(Path, 'open', side_effect=blocked), \
                    patch.object(sqlite3, 'connect', side_effect=blocked), \
                    patch.object(socket, 'socket', side_effect=blocked), \
                    patch.object(socket, 'create_connection', side_effect=blocked), \
                    patch.object(subprocess, 'Popen', side_effect=blocked):
                self.assertEqual(check.serve(1, read_fd, output, errors), 'eof')
            self.assertFalse(json.loads(output.getvalue())['result']['structuredContent']['credentials_read'])
            self.assertNotIn('Probe', check.__dict__)
            self.assertNotIn('sqlite3', check.__dict__)
            self.assertNotIn('socket', check.__dict__)
        finally:
            os.close(read_fd)

    def test_process_marker_changes_and_remains_non_authorizing(self):
        body = lines(request('tools/call', {'name': 'get_weixin_status'}),
                     request('tools/call', {'name': 'get_weixin_status'}, 2))
        first, second = self.cli(['--stdio'], body), self.cli(['--stdio'], body)
        values = [json.loads(x)['result']['structuredContent'] for x in first.stdout.splitlines()]
        self.assertEqual(values[0]['instance_id'], values[1]['instance_id'])
        other = json.loads(second.stdout.splitlines()[0])['result']['structuredContent']['instance_id']
        self.assertNotEqual(values[0]['instance_id'], other)
        self.assertIn('not_host_or_cross_client_fencing', values[0]['identity_scope'])

    def test_malformed_and_oversized_frames_are_bounded_and_redacted(self):
        result = self.cli(['--stdio'], 'not-json-private-fixture\n')
        self.assertEqual(json.loads(result.stdout)['error']['code'], -32700)
        self.assertNotIn('private-fixture', result.stdout + result.stderr)
        result = self.cli(['--stdio'], '[' * 1500 + '0' + ']' * 1500 + '\n')
        self.assertEqual(json.loads(result.stdout)['error']['code'], -32700)
        result = self.cli(['--stdio'], 'x' * (check.MAX_FRAME_BYTES + 1))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        result = self.cli(['--stdio', '--duration-seconds', '0'])
        self.assertNotEqual(result.returncode, 0)

    def test_request_limit_exits_without_responding_beyond_bound(self):
        result = self.cli(['--stdio'], lines(*[request('ping', request_id=n) for n in range(257)]))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout.splitlines()), check.MAX_REQUESTS)


if __name__ == '__main__':
    unittest.main()
