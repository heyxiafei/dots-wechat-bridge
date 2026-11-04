"""Offline integration of transport admission hooks, using synthetic policy data.

The policy fixture only exercises hook ordering and transaction boundaries. The
real FreshAdmission tests own approval, binding and original-subscription proof.
"""
import hashlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from probe.probe import Probe
from probe.weixin import WeixinError, encoded


OWNER, ACCOUNT = 'fixture-owner@im.wechat', 'fixture-fresh-bot'


def identity(source):
    return 'wx_' + hashlib.sha256(encoded([ACCOUNT, OWNER, str(source)]).encode()).hexdigest()[:32]


class TransportClient:
    def __init__(self):
        self.sent, self.results, self.polls = [], [], []

    def send(self, account, peer, text, client_id, context):
        self.sent.append((peer, text, client_id, context))
        result = self.results.pop(0) if self.results else {'ret': 0}
        if isinstance(result, Exception):
            raise result
        return result

    def poll(self, account):
        self.polls.append(account['cursor'])
        return {'msgs': [], 'get_updates_buf': 'fixture-next-cursor'}


class HookPolicy:
    """In-memory control with transactional synthetic admissions; not authorization."""
    def __init__(self, probe):
        self.probe = probe
        self.ready = True
        self.skip = set()
        self.denied = set()
        probe.db.execute('CREATE TABLE test_transport_admissions(id TEXT PRIMARY KEY,source_id TEXT,source_ms INTEGER)')
        probe.db.commit()

    def require_ready(self):
        if not self.ready:
            raise WeixinError('fresh_fixture_not_ready')

    def inspect_batch(self, account, msgs, now):
        self.require_ready()
        return [None if str(msg['message_id']) in self.skip else {
            'identity': identity(msg['message_id']), 'source_id': str(msg['message_id']),
            'source_ms': msg['create_time_ms']} for msg in msgs]

    def admit_in_transaction(self, mid, source, source_ms):
        self.probe.db.execute('INSERT INTO test_transport_admissions VALUES(?,?,?)', (mid, source, source_ms))

    def validate_candidate_in_transaction(self, mid, source, source_ms):
        row = self.probe.db.execute('SELECT source_id,source_ms FROM test_transport_admissions WHERE id=?', (mid,)).fetchone()
        if row is not None and tuple(row) != (source, source_ms):
            raise WeixinError('fresh_fixture_source_conflict')

    def record_context_in_transaction(self, mid, context, source_ms):
        self.require_source(mid)
        self.context = context

    def require_context(self, context):
        self.require_ready()
        if getattr(self, 'context', None) != context['token']:
            raise WeixinError('fresh_fixture_context_not_admitted')

    def allows_source(self, mid):
        return bool(self.ready and mid not in self.denied and self.probe.db.execute(
            'SELECT 1 FROM test_transport_admissions WHERE id=?', (mid,)).fetchone())

    def require_source(self, mid):
        if not self.allows_source(mid):
            raise WeixinError('fresh_fixture_source_not_admitted')


class FreshTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000.0
        self.client = TransportClient()
        self.probe = Probe(self.temp.name, clock=lambda: self.now, weixin_client=self.client)
        self.addCleanup(self.probe.close)
        self.binding = {'status': 'confirmed', 'ilink_bot_id': ACCOUNT,
                        'ilink_user_id': OWNER, 'bot_token': 'fixture-only-token'}
        self.probe.weixin.bind_confirmed(self.binding)
        # Deny accidental real transport rather than relying on fixture coverage.
        for target in ('socket.getaddrinfo', 'socket.create_connection',
                       'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def enable(self):
        self.policy = HookPolicy(self.probe)
        self.probe.fresh_admission = self.policy
        return self.policy

    def message(self, source='1001', **changes):
        msg = {'message_id': source, 'from_user_id': OWNER, 'to_user_id': ACCOUNT,
               'message_type': 1, 'message_state': 2, 'create_time_ms': int(self.now * 1000),
               'context_token': 'fixture-context',
               'item_list': [{'type': 1, 'text_item': {'text': 'synthetic owner input'}}]}
        msg.update(changes)
        return msg

    def ingest(self, *msgs, cursor='fixture-cursor'):
        return self.probe.weixin.ingest(self.probe.weixin.account(),
            {'ret': 0, 'msgs': list(msgs), 'get_updates_buf': cursor})

    def outbox(self, mid):
        return self.probe.db.execute('SELECT * FROM weixin_outbox WHERE message_id=?', (mid,)).fetchone()

    def test_invalid_raw_time_stops_entire_batch_before_body_media_or_cursor(self):
        policy = self.enable()
        # Production policy supplies the classification; transport must not use
        # its historical local-clock fallback to override that rejection.
        for value in (None, True, float('nan'), '1800000000000', int((self.now + 9999) * 1000)):
            with self.subTest(value=repr(value)):
                policy.inspect_batch = Mock(side_effect=WeixinError('fresh_invalid_source_time'))
                with patch('probe.weixin.describe_inbound') as describe, patch.object(self.probe.media, 'add') as media:
                    with self.assertRaisesRegex(WeixinError, 'fresh_invalid_source_time'):
                        self.ingest(self.message('1001'), self.message('1002', create_time_ms=value))
                    describe.assert_not_called()
                    media.assert_not_called()
                self.assertEqual(self.probe.weixin.account()['cursor'], '')
                self.assertIsNone(self.probe.weixin.account()['last_poll'])
                self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
                self.assertEqual(self.probe.db.execute('SELECT count(*) FROM test_transport_admissions').fetchone()[0], 0)

    def test_old_raw_source_is_skipped_without_body_and_safe_cursor_can_advance(self):
        self.enable().skip.add('1001')
        with patch('probe.weixin.describe_inbound') as describe, patch.object(self.probe.media, 'add') as media:
            self.assertEqual(self.ingest(self.message()), 0)
            describe.assert_not_called()
            media.assert_not_called()
        self.assertEqual(self.probe.weixin.account()['cursor'], 'fixture-cursor')
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)

    def test_new_admission_inbox_and_cursor_commit_together_and_duplicates_keep_one(self):
        self.enable()
        self.assertEqual(self.ingest(self.message()), 1)
        self.assertEqual(self.ingest(self.message()), 0)
        row = self.probe.db.execute('SELECT * FROM test_transport_admissions').fetchone()
        self.assertEqual(tuple(row), (identity('1001'), '1001', int(self.now * 1000)))
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 1)
        self.assertEqual(self.probe.weixin.account()['cursor'], 'fixture-cursor')

    def test_media_failure_rolls_back_admission_inbox_and_cursor(self):
        self.enable()
        with patch.object(self.probe.media, 'add', side_effect=RuntimeError('fixture media failure')):
            with self.assertRaisesRegex(RuntimeError, 'fixture media failure'):
                self.ingest(self.message())
        self.assertEqual(self.probe.weixin.account()['cursor'], '')
        for table in ('weixin_inbox', 'test_transport_admissions', 'weixin_inbound_manifests'):
            self.assertEqual(self.probe.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_conflicting_duplicate_time_stops_before_body_and_cursor_update(self):
        self.enable()
        self.ingest(self.message(), cursor='first-cursor')
        with patch('probe.weixin.describe_inbound') as describe:
            with self.assertRaisesRegex(WeixinError, 'fresh_fixture_source_conflict'):
                self.ingest(self.message(create_time_ms=int(self.now * 1000) + 1), cursor='bad-cursor')
            describe.assert_not_called()
        self.assertEqual(self.probe.weixin.account()['cursor'], 'first-cursor')

    def test_nonadmitted_reply_and_attachment_refuse_before_source_or_upload_read(self):
        self.ingest(self.message())
        self.enable()
        with patch.object(self.probe.weixin, 'read_message') as read, patch.object(self.probe.outbound, 'authorized') as upload:
            with self.assertRaisesRegex(WeixinError, 'fresh_fixture_source_not_admitted'):
                self.probe.weixin.queue_reply(identity('1001'), 'synthetic reply')
            with self.assertRaisesRegex(WeixinError, 'fresh_fixture_source_not_admitted'):
                self.probe.outbound.queue('fixture-send', 'fixture-upload', identity('1001'))
            read.assert_not_called()
            upload.assert_not_called()
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_old_queued_and_notification_do_not_block_or_send_with_new_reply(self):
        self.ingest(self.message('1001'))
        self.probe.weixin.queue_reply(identity('1001'), 'old synthetic reply')
        notice = self.probe.weixin.queue_notification('fixture-notice', 'synthetic notification')['send_id']
        self.enable()
        self.now += 1
        self.ingest(self.message('1002'))
        self.probe.weixin.queue_reply(identity('1002'), 'new synthetic reply')
        self.assertTrue(self.probe.weixin.send_once())
        self.assertEqual([item[1] for item in self.client.sent], ['new synthetic reply'])
        self.assertEqual(self.outbox(identity('1001'))['status'], 'queued')
        self.assertEqual(self.outbox(notice)['status'], 'queued')
        self.assertFalse(self.probe.weixin.send_once())
        with self.assertRaisesRegex(WeixinError, 'fresh_notifications_disabled'):
            self.probe.weixin.queue_notification('fixture-new-notice', 'synthetic notification')

    def test_context_refresh_does_not_revive_nonadmitted_context_wait(self):
        self.ingest(self.message('1001'))
        self.probe.weixin.queue_reply(identity('1001'), 'old synthetic reply')
        self.enable()
        self.ingest(self.message('1002'))
        self.probe.weixin.queue_reply(identity('1002'), 'new synthetic reply')
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_outbox SET status='context_required'")
        self.now += 1
        self.ingest(self.message('1003', context_token='fresh-context'))
        self.assertEqual(self.outbox(identity('1001'))['status'], 'context_required')
        self.assertEqual(self.outbox(identity('1002'))['status'], 'queued')

    def test_rebinding_cannot_rotate_epoch_or_revive_auth_wait(self):
        self.ingest(self.message())
        self.probe.weixin.queue_reply(identity('1001'), 'synthetic reply')
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_outbox SET status='auth_required'")
        epoch = self.probe.db.execute('SELECT epoch FROM weixin_binding_epoch').fetchone()[0]
        self.enable()
        with self.assertRaisesRegex(WeixinError, 'fresh_rebinding_requires_reactivation'):
            self.probe.weixin.bind_confirmed(self.binding)
        self.assertEqual(self.probe.db.execute('SELECT epoch FROM weixin_binding_epoch').fetchone()[0], epoch)
        self.assertEqual(self.outbox(identity('1001'))['status'], 'auth_required')

    def test_generation_change_after_selection_blocks_post_and_preserves_slot(self):
        policy = self.enable()
        self.ingest(self.message())
        self.probe.weixin.queue_reply(identity('1001'), 'synthetic reply')
        original_account = self.probe.weixin.account
        calls = []

        def account():
            calls.append(1)
            if len(calls) == 2:
                policy.denied.add(identity('1001'))
            return original_account()

        with patch.object(self.probe.weixin, 'account', side_effect=account):
            with self.assertRaisesRegex(WeixinError, 'fresh_fixture_source_not_admitted'):
                self.probe.weixin.send_once()
        self.assertEqual(self.client.sent, [])
        self.assertEqual((self.outbox(identity('1001'))['status'], self.outbox(identity('1001'))['attempts']), ('queued', 0))

    def test_last_check_after_claim_refuses_without_post_or_retry(self):
        policy = self.enable()
        self.ingest(self.message())
        self.probe.weixin.queue_reply(identity('1001'), 'synthetic reply')
        original = policy.require_context
        checks = []

        def check(context):
            checks.append(1)
            if len(checks) == 3:
                raise WeixinError('fresh_fixture_context_changed')
            original(context)

        with patch.object(policy, 'require_context', side_effect=check):
            self.assertTrue(self.probe.weixin.send_once())
        row = self.outbox(identity('1001'))
        self.assertEqual((row['status'], row['attempts'], row['post_started_at']), ('rejected', 0, None))
        self.assertEqual(self.client.sent, [])
        self.assertFalse(self.probe.weixin.send_once())

    def test_nonadmitted_old_queue_does_not_exhaust_fresh_reply_capacity(self):
        for index in range(32):
            source = str(1000 + index)
            self.ingest(self.message(source))
            self.probe.weixin.queue_reply(identity(source), 'old synthetic reply')
        self.enable()
        self.ingest(self.message('2000'))
        self.probe.weixin.queue_reply(identity('2000'), 'new synthetic reply')
        self.assertEqual(self.outbox(identity('2000'))['status'], 'queued')
        self.assertTrue(self.probe.weixin.send_once())
        self.assertEqual([item[1] for item in self.client.sent], ['new synthetic reply'])

    def test_unknown_and_rejected_keep_original_slots_without_replay(self):
        self.enable()
        for source, result, expected in (
                ('1001', WeixinError('api_timeout'), 'delivery_unknown'),
                ('1002', {'ret': 5}, 'rejected')):
            self.ingest(self.message(source))
            self.probe.weixin.queue_reply(identity(source), 'synthetic reply')
            self.client.results.append(result)
            self.assertTrue(self.probe.weixin.send_once())
            self.assertEqual(self.outbox(identity(source))['status'], expected)
            self.probe.weixin.queue_reply(identity(source), 'synthetic reply')
            self.probe.weixin.recover_inflight()
            self.assertFalse(self.probe.weixin.send_once())
            self.assertEqual(self.outbox(identity(source))['attempts'], 1)
        self.assertEqual(len(self.client.sent), 2)

    def test_explicit_context_rate_and_auth_refusals_are_terminal_in_fresh_mode(self):
        self.enable()
        cases = (
            ('1001', {'ret': -2, 'errmsg': 'prepare failed'}),
            ('1002', {'ret': -2, 'errmsg': 'rate limit'}),
            ('1003', {'ret': -14}),
        )
        for source, result in cases:
            self.ingest(self.message(source))
            self.probe.weixin.queue_reply(identity(source), 'synthetic reply')
            self.client.results.append(result)
            self.assertTrue(self.probe.weixin.send_once())
            row = self.outbox(identity(source))
            self.assertEqual((row['status'], row['error_kind'], row['attempts']), ('rejected', 'fresh_send_rejected', 1))
            self.now += 60
            self.assertFalse(self.probe.weixin.send_once())
        self.assertEqual(len(self.client.sent), 3)
        self.assertEqual(self.probe.weixin.account()['status'], 'auth_required')

    def test_unadmitted_peer_context_does_not_reach_post(self):
        policy = self.enable()
        self.ingest(self.message())
        self.probe.weixin.queue_reply(identity('1001'), 'synthetic reply')
        policy.context = 'different-synthetic-context'
        with self.assertRaisesRegex(WeixinError, 'fresh_fixture_context_not_admitted'):
            self.probe.weixin.send_once()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.outbox(identity('1001'))['status'], 'queued')

    def test_poll_is_not_started_before_current_ready_gate(self):
        self.enable().ready = False
        with self.assertRaisesRegex(WeixinError, 'fresh_fixture_not_ready'):
            self.probe.weixin.poll_once()
        self.assertEqual(self.client.polls, [])
        self.assertEqual(self.probe.weixin.account()['cursor'], '')
        self.assertIsNone(self.probe.weixin.account()['last_poll'])


if __name__ == '__main__':
    unittest.main()
