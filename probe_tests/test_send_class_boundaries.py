"""Offline send evidence and pending-queue policy. No real account or transport."""
import base64
import hashlib
import tempfile
import unittest
from unittest.mock import patch

from probe.probe import Probe
from probe.weixin import WeixinError


class SyntheticClient:
    def __init__(self):
        self.results = []
        self.sent = []

    def take(self, label, client_id):
        self.sent.append((label, client_id))
        result = self.results.pop(0) if self.results else {'ret': 0}
        if isinstance(result, Exception):
            raise result
        return result

    def send(self, account, peer, text, client_id, context):
        return self.take(text, client_id)

    def send_items(self, account, peer, items, client_id, context):
        return self.take('synthetic-media', client_id)

    def get_upload_url(self, account, payload):
        return {'upload_param': 'synthetic-only'}


class SendClassBoundaries(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket.connect', 'socket.create_connection', 'socket.getaddrinfo',
                     'subprocess.run', 'subprocess.Popen'):
            guard = patch(name, side_effect=AssertionError('offline test attempted external operation'))
            guard.start()
            self.addCleanup(guard.stop)
        self.directory = tempfile.TemporaryDirectory(prefix='send-class-review-')
        self.addCleanup(self.directory.cleanup)
        self.now = 1900000000.0
        self.client = SyntheticClient()
        self.probe = Probe(self.directory.name, clock=lambda: self.now, weixin_client=self.client)
        self.addCleanup(self.probe.close)
        self.probe.outbound.upload = lambda *_: 'synthetic-local-reference'
        self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'synthetic-bot',
                                         'ilink_user_id': 'synthetic-owner', 'bot_token': 'synthetic-token'})
        self.counter = 0

    def inbound(self):
        self.counter += 1
        source = 'synthetic-source-' + str(self.counter)
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [{
            'message_id': source, 'message_type': 1, 'message_state': 2,
            'from_user_id': 'synthetic-owner', 'to_user_id': 'synthetic-bot',
            'context_token': 'synthetic-context-' + str(self.counter),
            'item_list': [{'type': 1, 'text_item': {'text': 'synthetic request'}}]}]})
        return self.probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()[0]

    def queued_reply(self, text='synthetic reply'):
        mid = self.inbound()
        self.probe.weixin.queue_reply(mid, text)
        return mid

    def assert_terminal_does_not_retry(self, mid, text='synthetic reply'):
        before = tuple(self.probe.db.execute(
            'SELECT message_id,client_id,status,attempts,text FROM weixin_outbox WHERE message_id=?', (mid,)).fetchone())
        calls = len(self.client.sent)
        self.probe.weixin.queue_reply(mid, text)
        self.now += 60
        self.inbound()  # A fresh context must not revive rejected/unknown sends.
        self.assertFalse(self.probe.weixin.send_once())
        after = tuple(self.probe.db.execute(
            'SELECT message_id,client_id,status,attempts,text FROM weixin_outbox WHERE message_id=?', (mid,)).fetchone())
        self.assertEqual(after, before)
        self.assertEqual(len(self.client.sent), calls)

    def test_non2xx_response_is_received_but_delivery_stays_unknown(self):
        for code in (401, 403, 429, 500, 503):
            with self.subTest(http_status=code):
                mid = self.queued_reply()
                self.client.results = [WeixinError('api_http_error', code)]
                self.assertTrue(self.probe.weixin.send_once())
                status = self.probe.weixin.send_status(mid)
                self.assertEqual(status['status'], 'delivery_unknown')
                self.assertTrue(status['api_result']['response_received'])
                self.assertEqual(status['api_result']['http_status'], code)
                self.assertEqual(status['error_kind'], 'api_http_error')
                self.assertFalse(status['accepted'])
                self.assertIsNone(status['delivered'])
                self.assert_terminal_does_not_retry(mid)

    def test_timeout_and_network_remain_unknown_without_http_response_evidence(self):
        for kind in ('api_timeout', 'api_network_error'):
            mid = self.queued_reply()
            self.client.results = [WeixinError(kind)]
            self.probe.weixin.send_once()
            status = self.probe.weixin.send_status(mid)
            self.assertEqual((status['status'], status['error_kind']), ('delivery_unknown', kind))
            self.assertFalse(status['api_result']['response_received'])
            self.assertNotIn('http_status', status['api_result'])
            self.assert_terminal_does_not_retry(mid)

    def test_explicit_business_refusal_is_terminal_without_notification_fallback(self):
        mid = self.queued_reply()
        self.client.results = [{'ret': -5, 'errmsg': 'synthetic refusal'}]
        self.probe.weixin.send_once()
        status = self.probe.weixin.send_status(mid)
        self.assertEqual((status['status'], status['error_kind'], status['server_code']), ('rejected', 'send_rejected', -5))
        self.assertTrue(status['api_result']['response_received'])
        self.assert_terminal_does_not_retry(mid)
        self.assertEqual(self.probe.db.execute("SELECT count(*) FROM weixin_outbox WHERE kind='notification'").fetchone()[0], 0)

    def test_context_and_auth_states_are_distinct_from_timeout(self):
        mid = self.queued_reply()
        self.client.results = [{'ret': -2, 'errmsg': 'prepare failed'}]
        self.probe.weixin.send_once()
        status = self.probe.weixin.send_status(mid)
        self.assertEqual((status['status'], status['error_kind']), ('context_required', 'fresh_inbound_required'))
        self.now += 86400
        self.assertFalse(self.probe.weixin.send_once())
        # Preserve this established compatibility classification, not a claim
        # that generic upstream text proves the underlying context root cause.
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_outbox SET status='rejected' WHERE message_id=?", (mid,))
        auth = self.queued_reply()
        self.client.results = [{'ret': -14}]
        self.probe.weixin.send_once()
        state = self.probe.weixin.send_status(auth)
        self.assertEqual((state['status'], state['error_kind']), ('auth_required', 'bot_auth_expired'))
        self.assertFalse(self.probe.weixin.send_once())

    def test_mixed_error_codes_cannot_be_hidden_by_minus_two_retry(self):
        for payload in ({'ret': -2, 'errcode': -5, 'errmsg': 'rate limit'},
                        {'ret': -5, 'errcode': -2, 'errmsg': 'prepare failed'},
                        {'ret': -2, 'errcode': -14, 'errmsg': 'rate limited'}):
            with self.subTest(payload=payload):
                mid = self.queued_reply()
                self.client.results = [payload]
                self.probe.weixin.send_once()
                status = self.probe.weixin.send_status(mid)
                self.assertEqual((status['status'], status['error_kind']), ('rejected', 'send_rejected'))
                self.assertIn(status['server_code'], (-5, -14))
                self.assert_terminal_does_not_retry(mid)

    def test_unknown_or_empty_minus_two_is_terminal_not_guessed_rate_limit(self):
        for message in (None, '', 'synthetic service failure', 'permission denied'):
            mid = self.queued_reply()
            self.client.results = [{'ret': -2, 'errmsg': message}]
            self.probe.weixin.send_once()
            status = self.probe.weixin.send_status(mid)
            self.assertEqual((status['status'], status['error_kind'], status['server_code']), ('rejected', 'send_rejected', -2))
            self.assert_terminal_does_not_retry(mid)

    def test_only_known_rate_text_preserves_three_attempt_same_id_budget(self):
        for message in ('rate limit', 'rate limited'):
            mid = self.queued_reply()
            self.client.results = [{'ret': -2, 'errmsg': message}] * 3
            start = len(self.client.sent)
            for attempt in range(3):
                self.probe.weixin.send_once()
                status = self.probe.weixin.send_status(mid)
                self.assertEqual(status['attempts'], attempt + 1)
                self.assertEqual(status['status'], 'queued' if attempt < 2 else 'rejected')
                self.now += 30
            client_ids = [item[1] for item in self.client.sent[start:]]
            self.assertEqual(len(client_ids), 3)
            self.assertEqual(len(set(client_ids)), 1)
            self.assert_terminal_does_not_retry(mid)

    def test_pending_owner_text_precedes_older_notification_and_media_within_group_fifo(self):
        notice = self.probe.weixin.queue_notification('explicit-synthetic-notice', 'synthetic notice')['send_id']
        source = self.inbound()
        raw = b'synthetic file'
        uid = self.probe.outbound.begin('synthetic-upload', 'synthetic.bin', 'file', len(raw), hashlib.sha256(raw).hexdigest())['upload_id']
        self.probe.outbound.chunk(uid, 0, base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest())
        self.probe.outbound.finalize(uid)
        media = self.probe.outbound.queue('synthetic-media-send', uid, source)['send_id']
        self.now += 1
        final = self.queued_reply('synthetic owner final')
        pair = self.inbound()
        ack = self.probe.weixin.queue_reply_phase(pair, 'ack', 'synthetic owner ack')['send_id']
        self.probe.weixin.queue_reply_phase(pair, 'final', 'synthetic pair final')
        for _ in range(5):
            self.assertTrue(self.probe.weixin.send_once())
        self.assertEqual([item[0] for item in self.client.sent],
                         ['synthetic owner final', 'synthetic owner ack', 'synthetic pair final', 'synthetic notice', 'synthetic-media'])
        self.assertEqual(len({final, ack, pair, notice, media}), 5)
        self.assertFalse(self.probe.weixin.send_once())

    def test_priority_does_not_bypass_own_ack_wait_held_notice_or_unknown(self):
        unknown = self.queued_reply('synthetic unknown')
        self.client.results = [WeixinError('api_timeout')]
        self.probe.weixin.send_once()
        held = self.probe.weixin.queue_notification('held-notice', 'synthetic held')['send_id']
        self.probe.weixin.aihot_held_notification_ids = {held}
        self.probe.weixin.queue_notification('ready-notice', 'synthetic ready notice')
        pair = self.inbound()
        ack = self.probe.weixin.queue_reply_phase(pair, 'ack', 'synthetic waiting ack')['send_id']
        self.probe.weixin.queue_reply_phase(pair, 'final', 'synthetic waiting final')
        with self.probe.db:
            self.probe.db.execute('UPDATE weixin_outbox SET next_at=? WHERE message_id=?', (self.now + 60, ack))
        self.queued_reply('synthetic independent reply')
        self.assertTrue(self.probe.weixin.send_once())
        self.assertEqual(self.client.sent[-1][0], 'synthetic independent reply')
        self.assertTrue(self.probe.weixin.send_once())
        self.assertEqual(self.client.sent[-1][0], 'synthetic ready notice')
        self.assertFalse(self.probe.weixin.send_once())
        self.now += 60
        self.probe.weixin.send_once()
        self.probe.weixin.send_once()
        self.assertEqual([item[0] for item in self.client.sent[-2:]], ['synthetic waiting ack', 'synthetic waiting final'])
        self.assertEqual(self.probe.weixin.send_status(unknown)['status'], 'delivery_unknown')
        self.assertEqual(self.probe.weixin.send_status(held)['status'], 'queued')
        self.assertFalse(self.probe.weixin.send_once())

    def test_existing_system_receipt_priority_is_preserved_without_enabling_it(self):
        self.queued_reply('synthetic regular reply')
        receipt_source = self.inbound()
        receipt = self.probe.weixin.queue_reply_phase(receipt_source, 'ack', 'synthetic legacy receipt')['send_id']
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_outbox SET ack_origin='system_receipt' WHERE message_id=?", (receipt,))
        self.probe.weixin.send_once()
        self.assertEqual(self.client.sent[0][0], 'synthetic legacy receipt')
        self.probe.weixin.send_once()
        self.assertEqual(self.client.sent[1][0], 'synthetic regular reply')


if __name__ == '__main__':
    unittest.main()
