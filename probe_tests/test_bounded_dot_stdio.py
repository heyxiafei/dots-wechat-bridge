import base64
import copy
import hashlib
import json
import contextlib
import io
import os
import threading
import time
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from probe.probe import Probe, canonical
from probe.new_message_check import binding, phrase, CheckError
from probe import bounded_dot_stdio as bounded
from probe.weixin import WeixinError


class Clock:
    value = 1000.
    def __call__(self):
        return self.value


class Client:
    def __init__(self):
        self.batches, self.posts, self.polls = [], [], 0
        self.response, self.failure = {}, None
    def poll(self, account):
        self.polls += 1
        if self.batches:
            result = self.batches.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return {'ret': 0, 'msgs': []}
    def send(self, account, peer, text, client_id, context):
        self.posts.append((peer, text, client_id))
        if self.failure:
            raise self.failure
        return self.response


class BoundedDotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.clock, self.client = Clock(), Client()
        self.events = []
        self.event_code = 200
        def post(url, body, hdrs):
            data = json.loads(body)
            if data.get('type') == 'verification':
                return 200, json.dumps({'challenge': data['challenge']}).encode()
            self.events.append(data)
            return self.event_code, b'{}'
        self.probe = Probe(self.tmp.name, post=post, allowed_hosts=['callback.example.com'],
                           clock=self.clock, weixin_client=self.client)
        self.addCleanup(self.probe.close)
        self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                                         'ilink_user_id': 'fixture-owner', 'bot_token': 'fixture-token'})
        self.params = {'name': 'weixin.message', 'arguments': {}, 'ttlMs': 180000,
                       'delivery': {'mode': 'webhook', 'url': 'https://callback.example.com/fixture',
                                    'secret': 'whsec_' + base64.b64encode(b'e' * 32).decode()}}
        self.approval = {'round_id': 'a' * 32, 'duration_seconds': 120, 'valid_until': 1180,
                         'callback_host': 'callback.example.com',
                         'binding_sha256': binding(self.probe.db)[1],
                         'subscription_id': self.probe.subscription_id(self.params),
                         'subscription_request_digest': hashlib.sha256(canonical(self.params).encode()).hexdigest(),
                         **{key: 'synthetic-test-evidence' for key in bounded.REFERENCES}}
        self.session = bounded.BoundedDot(self.probe, self.approval, self.clock, self.clock)
        self.network = patch('socket.create_connection', side_effect=AssertionError('real network prohibited'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def msg(self, **changes):
        result = {'message_id': 'real-fixture-source', 'from_user_id': 'fixture-owner',
                  'to_user_id': 'fixture-bot', 'message_type': 1, 'message_state': 2,
                  'create_time_ms': 1000000, 'context_token': 'synthetic-context',
                  'item_list': [{'type': 1, 'text_item': {'text': phrase('a' * 32)}}]}
        result.update(changes)
        return result

    def rpc(self, method, params=None):
        return self.session.rpc({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})

    def call(self, name, **args):
        return self.rpc('tools/call', {'name': name, 'arguments': args})

    def ready(self):
        self.assertIn('result', self.rpc('events/subscribe', self.params))
        self.client.batches = [{'ret': 0, 'msgs': [self.msg()]}]
        self.session.tick()
        mid = self.session.receipt()['message_id']
        result = self.call('read_weixin_message', message_id=mid)
        self.assertNotIn('error', result)
        return mid

    def test_real_probe_event_read_final_and_original_status(self):
        mid = self.ready()
        data = self.probe.weixin.read_message(mid)
        self.assertTrue(data['inbound_completeness']['complete'])
        self.assertTrue(data['quote_context']['complete'])
        self.assertEqual(self.events[0]['data'], {'message_id': mid})
        result = self.call('reply_weixin_message', message_id=mid, text='DOT synthetic reply')
        self.assertNotIn('error', result)
        self.assertEqual(self.client.posts[0][1], 'DOT synthetic reply')
        self.assertEqual(self.session.receipt()['phase'], 'accepted')
        self.assertEqual(self.probe.weixin.account()['cursor'], '')
        self.assertIsNone(bounded.read_receipt(self.directory, 'a' * 32)['phone_received'])

    def test_discovery_and_blocked_write_methods(self):
        self.assertEqual({x['name'] for x in self.rpc('tools/list')['result']['tools']}, bounded.TOOLS)
        self.assertEqual([x['name'] for x in self.rpc('events/list')['result']['events']], ['weixin.message'])
        for name in ('reply_weixin_message_phase', 'send_weixin_notification', 'begin_weixin_upload', 'get_weixin_login_qrcode'):
            self.assertIn('error', self.call(name))
        self.assertIn('error', self.rpc('events/unsubscribe', self.params))
        self.assertEqual(self.client.polls, 0)

    def test_wrong_original_subscription_never_polls(self):
        wrong = copy.deepcopy(self.params)
        wrong['delivery']['url'] += '/other'
        self.assertIn('error', self.rpc('events/subscribe', wrong))
        self.session.tick()
        self.assertEqual(self.client.polls, 0)

    def test_old_foreign_missing_time_and_forwarded_never_ingested(self):
        self.rpc('events/subscribe', self.params)
        self.client.batches = [{'msgs': [self.msg(create_time_ms=999000), self.msg(from_user_id='other'),
                                        self.msg(create_time_ms=None), self.msg(forward=True)]}]
        self.session.tick()
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
        self.assertFalse(self.events)
        self.assertFalse(self.client.posts)

    def test_timeout_is_read_only_wait_and_later_message_works(self):
        self.rpc('events/subscribe', self.params)
        self.client.batches = [WeixinError('api_timeout'), {'msgs': [self.msg()]}]
        self.session.tick()
        self.assertEqual(self.session.receipt()['phase'], 'waiting_source')
        self.clock.value += .5
        self.session.tick()
        self.assertEqual(self.session.receipt()['phase'], 'awaiting_dot_final')

    def test_denial_and_unknown_never_repost_or_change_text(self):
        mid = self.ready()
        self.client.failure = RuntimeError('PRIVATE_PROVIDER_BODY')
        result = self.call('reply_weixin_message', message_id=mid, text='first')
        self.assertNotIn('PRIVATE_PROVIDER_BODY', canonical(result))
        duplicate = self.call('reply_weixin_message', message_id=mid, text='different')
        self.assertTrue(duplicate['result']['structuredContent']['content_conflict'])
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(bounded.read_receipt(self.directory, 'a' * 32)['phase'], 'delivery_unknown')
        self.assertEqual(self.probe.db.execute('SELECT text FROM weixin_outbox').fetchone()[0], 'first')

    def test_explicit_rejection_terminal(self):
        mid = self.ready()
        self.client.response = {'ret': 123}
        self.call('reply_weixin_message', message_id=mid, text='first')
        self.call('reply_weixin_message', message_id=mid, text='first')
        self.assertEqual(len(self.client.posts), 1)
        self.assertEqual(self.session.receipt()['phase'], 'rejected')

    def test_event_failure_not_retried_and_cannot_send(self):
        self.event_code = 503
        mid = self.ready()
        self.session.tick()
        self.assertIn('error', self.call('reply_weixin_message', message_id=mid, text='reply'))
        self.assertEqual(len(self.events), 1)
        self.assertFalse(self.client.posts)

    def test_changed_binding_refuses_before_send(self):
        mid = self.ready()
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_binding_epoch SET epoch='changed'")
        self.assertIn('error', self.call('reply_weixin_message', message_id=mid, text='reply'))
        self.assertFalse(self.client.posts)

    def test_expiry_refuses_and_receipt_available(self):
        mid = self.ready()
        self.clock.value = 1121
        self.assertIn('error', self.call('reply_weixin_message', message_id=mid, text='reply'))
        self.assertEqual(bounded.read_receipt(self.directory, 'a' * 32)['phase'], 'stopped_no_restart')

    def test_sending_commit_survives_process_death_without_resend(self):
        mid = self.ready()
        class Crash(BaseException):
            pass
        def crash(*args):
            row = self.probe.db.execute('SELECT status,attempts FROM weixin_outbox').fetchone()
            self.assertEqual(tuple(row), ('sending', 1))
            raise Crash()
        self.client.send = crash
        with self.assertRaises(Crash):
            self.call('reply_weixin_message', message_id=mid, text='reply')
        self.assertEqual(bounded.read_receipt(self.directory, 'a' * 32)['phase'], 'delivery_unknown')
        with self.assertRaises(CheckError):
            bounded.BoundedDot(self.probe, self.approval, self.clock, self.clock)

    def test_plain_main_refuses_marker_before_recovery(self):
        from probe import probe as module
        with patch.object(module.sys, 'argv', ['probe', '--state-dir', self.tmp.name]), \
             patch.object(module.WeixinStore, 'recover_inflight') as recover, \
             patch.object(module.threading.Thread, 'start') as start:
            self.assertEqual(module.main(), 1)
            recover.assert_not_called()
            start.assert_not_called()

    def test_read_receipt_mismatch_never_reports_accepted(self):
        mid = self.ready()
        self.call('reply_weixin_message', message_id=mid, text='reply')
        with self.probe.db:
            self.probe.db.execute('UPDATE weixin_outbox SET attempts=2')
        self.assertEqual(bounded.read_receipt(self.directory, 'a' * 32)['phase'], 'delivery_unknown')

    def test_notifications_cannot_subscribe_or_send(self):
        self.assertIn('error', self.session.rpc({'jsonrpc': '2.0', 'method': 'events/subscribe', 'params': self.params}))
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)
        mid = self.ready()
        self.assertIn('error', self.session.rpc({'jsonrpc': '2.0', 'method': 'tools/call',
            'params': {'name': 'reply_weixin_message', 'arguments': {'message_id': mid, 'text': 'reply'}}}))
        self.assertFalse(self.client.posts)

    def test_ambiguous_sources_not_persisted(self):
        self.rpc('events/subscribe', self.params)
        self.client.batches = [{'msgs': [self.msg(), self.msg(message_id='other-source')]}]
        self.session.tick()
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
        self.assertFalse(self.events)

    def test_empty_poll_throttled(self):
        self.rpc('events/subscribe', self.params)
        self.session.tick()
        self.session.tick()
        self.assertEqual(self.client.polls, 1)
        self.clock.value += .5
        self.session.tick()
        self.assertEqual(self.client.polls, 2)

    def test_late_poll_and_monotonic_expiry_do_not_ingest(self):
        self.rpc('events/subscribe', self.params)
        def late(account):
            self.clock.value = 1121
            return {'msgs': [self.msg()]}
        self.client.poll = late
        with self.assertRaises(CheckError):
            self.session.tick()
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
        self.clock.value = 999
        self.session.monotonic = lambda: 1121
        with self.assertRaises(CheckError):
            self.session.active()

    def test_corrupt_receipt_ids_are_not_exposed(self):
        with self.probe.db:
            self.probe.db.execute('UPDATE weixin_bounded_dot_check SET source_id=?,event_id=?,phase=?,reply_digest=?',
                                  ('PRIVATE_DATA', 'PRIVATE_DATA', 'PRIVATE_DATA', 'invalid'))
        receipt = bounded.read_receipt(self.directory, 'a' * 32)
        self.assertNotIn('PRIVATE_DATA', json.dumps(receipt))
        self.assertEqual(receipt['phase'], 'delivery_unknown')

    def test_empty_marker_still_blocks_production(self):
        from probe import probe as module
        with self.probe.db:
            self.probe.db.execute('DELETE FROM weixin_bounded_dot_check')
        with patch.object(module.sys, 'argv', ['probe', '--state-dir', self.tmp.name]), \
             patch.object(module.WeixinStore, 'recover_inflight') as recover, \
             patch.object(module.threading.Thread, 'start') as start:
            self.assertEqual(module.main(), 1)
            recover.assert_not_called()
            start.assert_not_called()

    def test_readonly_wal_without_shm_is_refused_before_open(self):
        original = Path.exists
        def exists(path):
            if str(path).endswith('-wal'):
                return True
            if str(path).endswith('-shm'):
                return False
            return original(path)
        with patch.object(Path, 'exists', exists), patch.object(bounded, 'open_database') as connect:
            with self.assertRaisesRegex(CheckError, 'readonly_wal_without_shm'):
                bounded.read_receipt(self.directory, 'a' * 32)
            connect.assert_not_called()

    def test_actual_stdio_pipe_roundtrip_without_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            delivered = threading.Event()
            def post(url, body, hdrs):
                data = json.loads(body)
                if data.get('type') == 'verification':
                    return 200, json.dumps({'challenge': data['challenge']}).encode()
                delivered.set()
                return 200, b'{}'
            client = Client()
            def poll(account):
                return {'msgs': [self.msg(create_time_ms=time.time() * 1000)]}
            client.poll = poll
            p = Probe(directory, post=post, allowed_hosts=['callback.example.com'], weixin_client=client)
            p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                                    'ilink_user_id': 'fixture-owner', 'bot_token': 'fixture-token'})
            approval = {**self.approval, 'valid_until': time.time() + 180,
                        'binding_sha256': binding(p.db)[1]}
            p.close()
            path = directory / 'approval.json'
            path.write_text(json.dumps(approval))
            path.chmod(0o600)
            source = 'wx_' + hashlib.sha256(canonical(['fixture-bot', 'fixture-owner', 'real-fixture-source']).encode()).hexdigest()[:32]
            def request(i, method, params):
                return (json.dumps({'jsonrpc': '2.0', 'id': i, 'method': method, 'params': params}) + '\n').encode()
            readfd, writefd = os.pipe()
            def writer():
                try:
                    os.write(writefd, request(1, 'initialize', {}) + request(2, 'events/subscribe', self.params))
                    if delivered.wait(2):
                        os.write(writefd, request(3, 'tools/call', {'name': 'read_weixin_message', 'arguments': {'message_id': source}})
                                 + request(4, 'tools/call', {'name': 'reply_weixin_message', 'arguments': {'message_id': source, 'text': 'real stdio DOT reply'}}))
                finally:
                    os.close(writefd)
            worker = threading.Thread(target=writer)
            worker.start()
            output = io.StringIO()
            real_probe = Probe
            def create_probe(root, **kwargs):
                return real_probe(root, post=post, **kwargs)
            try:
                with patch.object(bounded, 'Probe', create_probe), patch.object(bounded, 'TimedClient', return_value=client), contextlib.redirect_stdout(output):
                    bounded.run(directory, path, readfd)
            finally:
                os.close(readfd)
                worker.join(3)
            responses = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([x['id'] for x in responses], [1, 2, 3, 4])
            self.assertTrue(all('result' in x for x in responses))
            self.assertEqual(client.posts[0][1], 'real stdio DOT reply')
            self.assertEqual(bounded.read_receipt(directory, 'a' * 32)['phase'], 'accepted')
            with self.assertRaises(CheckError):
                bounded.run(directory, path, readfd)


if __name__ == '__main__':
    unittest.main()
