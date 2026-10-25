"""Offline P0 receipt integration: synthetic owner, temp SQLite, fake POST only.

These tests establish local arbitration, not phone delivery or dot processing.
No epoch is activated, and no live configuration, message or database is read.
"""
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from probe.probe import Probe
from probe.receipts import RECEIPT_TEXT
from probe.weixin import MESSAGE_EVENT, WeixinError


ACCOUNT, OWNER = 'p0-fixture-bot', 'p0-fixture-owner'


class FakeClient:
    def __init__(self):
        self.sent, self.results, self.on_send = [], [], None

    def send(self, account, peer, text, client_id, context):
        self.sent.append((peer, text, client_id, context))
        if self.on_send:
            callback, self.on_send = self.on_send, None
            callback()
        result = self.results.pop(0) if self.results else {'ret': 0}
        if isinstance(result, Exception):
            raise result
        return result


class P0ReceiptIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1800000000.0
        self.client, self.peers = FakeClient(), []
        self.p = self.open_probe()
        self.addCleanup(self.close_peers)
        self.p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': ACCOUNT,
            'ilink_user_id': OWNER, 'bot_token': 'synthetic-secret-fixture-0000000000000000'})
        self.activation = str(uuid.uuid4())
        # A network or subprocess call is a test failure even if a fake is missed.
        for target in ('socket.create_connection', 'socket.socket.connect',
                       'subprocess.Popen', 'os.system'):
            guard = patch(target, side_effect=AssertionError('offline test forbids IO'))
            guard.start()
            self.addCleanup(guard.stop)

    def open_probe(self):
        peer = Probe(self.tmp.name, clock=lambda: self.now, weixin_client=self.client)
        self.peers.append(peer)
        return peer

    def close_peers(self):
        for peer in self.peers:
            peer.close()

    def policy(self, **changes):
        value = dict(version=1, enabled=True, activation_id=self.activation, grace_ms=150)
        value.update(changes)
        path = Path(self.tmp.name) / 'owner-receipts.json'
        path.write_text(json.dumps(value), encoding='utf-8')
        path.chmod(0o600)
        return path

    def activate(self):
        self.policy()
        return self.p.receipts.start_policy()

    def inbound(self, source, p=None, **changes):
        p = p or self.p
        msg = dict(message_id=source, from_user_id=OWNER, to_user_id=ACCOUNT,
                   message_type=1, message_state=2, create_time_ms=self.now * 1000,
                   context_token='synthetic-secret-fixture-0000000000000000',
                   item_list=[{'type': 1, 'text_item': {'text': '同一合成正文'}}])
        msg.update(changes)
        changed = p.weixin.ingest(p.weixin.account(), {'msgs': [msg]})
        row = p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()
        return (row['id'] if row else None), changed

    def fresh(self, source='future'):
        mid, changed = self.inbound(source)
        self.assertEqual(changed, 1)
        return mid

    def due(self):
        self.now += .151
        return self.p.receipts.materialize_due()

    def ack(self, mid, p=None):
        p = p or self.p
        return p.db.execute('SELECT * FROM weixin_outbox WHERE message_id=?',
                            (p.weixin.ack_send_id(mid),)).fetchone()

    def linked(self, mid, source):
        event = 'evt_p0_' + source
        with self.p.db:
            self.p.db.execute("INSERT INTO pings(id,nonce,sub_id,body,status,name,message_id) VALUES(?,'','offline','{}','delivered',?,?)",
                              (event, MESSAGE_EVENT, mid))
            self.p.db.execute('UPDATE weixin_inbox SET event_id=? WHERE id=?', (event, mid))
        return event

    def test_default_disabled_even_with_policy_until_explicit_start(self):
        self.policy()
        second = self.open_probe()
        self.assertFalse(second.receipts.status()['running'])
        mid, _ = self.inbound('constructor-off', p=second)
        self.now += 10
        self.assertFalse(second.receipts.materialize_due())
        self.assertIsNone(second.receipts.for_message(mid)['intent'])
        self.assertIsNone(self.ack(mid))

    def test_policy_strict_types_permissions_and_unknown_fields(self):
        for change in ({'version': True}, {'enabled': 1}, {'grace_ms': True},
                       {'grace_ms': -1}, {'grace_ms': 1001}, {'activation_id': 'not-uuid'},
                       {'extra': 'not-allowed'}):
            with self.subTest(change=change):
                self.policy(**change)
                with self.assertRaisesRegex(WeixinError, 'unsafe_receipt_policy'):
                    self.p.receipts.start_policy()
                self.assertFalse(self.p.receipts.running)
        self.policy().chmod(0o644)
        with self.assertRaisesRegex(WeixinError, 'unsafe_receipt_policy'):
            self.p.receipts.start_policy()

    def test_first_activation_baselines_two_old_sources_and_pending_attachment(self):
        first = self.fresh('old-partial-one')
        second = self.fresh('old-partial-two')
        with self.p.db:
            self.p.db.execute("INSERT INTO weixin_attachments(id,message_id,account,peer,kind,filename,status,created_at,expires_at,provenance) VALUES(?,?,?,?,'image','fixture.png','pending',0,1,'{}')",
                              ('att_' + 'a' * 32, second, ACCOUNT, OWNER))
        status = self.activate()
        self.assertEqual(status['activation']['baseline_count'], 2)
        for source, mid in (('old-partial-one', first), ('old-partial-two', second)):
            self.assertEqual(self.inbound(source)[1], 0)
            result = self.p.receipts.for_message(mid)
            self.assertTrue(result['baseline'])
            self.assertIsNone(result['intent'])
            self.assertIsNone(result['ack'])
        self.now += 100
        self.assertFalse(self.p.receipts.materialize_due())
        self.assertEqual(self.client.sent, [])

    def test_new_durable_owner_input_grace_canonical_slot_and_truthful_projection(self):
        self.activate()
        mid = self.fresh()
        state = self.p.receipts.for_message(mid)
        self.assertEqual(state['intent']['state'], 'waiting')
        self.assertFalse(state['baseline'])
        self.assertFalse(self.p.receipts.materialize_due())
        self.now += .149
        self.assertFalse(self.p.receipts.materialize_due())
        self.now += .002
        self.assertTrue(self.p.receipts.materialize_due())
        row = self.ack(mid)
        self.assertEqual((row['message_id'], row['client_id']),
                         (self.p.weixin.ack_send_id(mid), 'dots_' + self.p.weixin.ack_send_id(mid)))
        self.assertEqual((row['kind'], row['ack_origin'], row['text'], row['attempts']),
                         ('reply_ack', 'system_receipt', '已收到。（系统收件确认）', 0))
        self.assertEqual(RECEIPT_TEXT, row['text'])
        state = self.p.weixin.read_message(mid)['reply_state']['system_receipt']
        self.assertEqual(state['receipt_basis'], 'durable_local_ingest')
        self.assertFalse(state['dot_processing_confirmed'])
        self.assertFalse(state['phone_delivery_confirmed'])

    def test_duplicate_no_new_intent_same_body_different_source_and_old_timestamp_are_independent(self):
        self.activate()
        first = self.fresh('same-body-one')
        self.assertEqual(self.inbound('same-body-one')[1], 0)
        second, changed = self.inbound('same-body-two', create_time_ms=1000)
        self.assertEqual(changed, 1)
        self.assertNotEqual(first, second)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM owner_receipt_intents').fetchone()[0], 2)
        self.due()
        self.p.receipts.materialize_due()
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 2)

    def test_repeat_and_restart_same_activation_keep_due_and_do_not_expand_baseline(self):
        self.activate()
        mid = self.fresh()
        due = self.p.receipts.for_message(mid)['intent']['due_at']
        self.now += .1
        self.p.receipts.start_policy()
        second = self.open_probe()
        second.receipts.start_policy()
        state = second.receipts.for_message(mid)
        self.assertFalse(state['baseline'])
        self.assertEqual(state['intent']['due_at'], due)
        self.now += .051
        self.assertTrue(second.receipts.materialize_due())
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM owner_receipt_activations').fetchone()[0], 1)

    def test_concurrent_same_activation_start_captures_baseline_once(self):
        self.fresh('old-before-activation')
        self.policy()
        second = self.open_probe()
        barrier = threading.Barrier(2)
        def start(p):
            barrier.wait()
            return p.receipts.start_policy()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(start, (self.p, second)))
        self.assertEqual([r['activation']['baseline_count'] for r in results], [1, 1])
        mid = self.fresh()
        second.receipts.start_policy()
        self.assertFalse(second.receipts.for_message(mid)['baseline'])

    def test_ingest_observe_failure_rolls_back_durable_input_and_intent_without_wake(self):
        self.activate()
        self.p.sender_wakeup.clear()
        with patch.object(self.p.receipts, 'observe_new_in_transaction', side_effect=sqlite3.IntegrityError('offline rollback')):
            with self.assertRaises(sqlite3.IntegrityError):
                self.inbound('rollback-source')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM owner_receipt_intents').fetchone()[0], 0)
        self.assertFalse(self.p.sender_wakeup.is_set())

    def test_non_owner_group_and_wrong_recipient_cannot_create_receipt(self):
        self.activate()
        for i, changes in enumerate(({'from_user_id': 'another-owner'}, {'group_id': 'group'},
                                      {'room_id': 'room'}, {'to_user_id': 'another-bot'})):
            self.assertEqual(self.inbound('filtered-' + str(i), **changes), (None, 0))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM owner_receipt_intents').fetchone()[0], 0)
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.p.receipts.for_message('wx_' + 'f' * 32)

    def test_model_ack_and_fallback_concurrent_connections_share_one_immutable_slot(self):
        self.activate()
        mid = self.fresh()
        self.now += .151
        second = self.open_probe()
        second.receipts.start_policy()
        barrier = threading.Barrier(2)
        def fallback():
            barrier.wait()
            return self.p.receipts.materialize_due()
        def model():
            barrier.wait()
            return second.weixin.queue_reply_phase(mid, 'ack', '模型确认')
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(fallback), pool.submit(model)
            a.result()
            result = b.result()
        row = self.ack(mid)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 1)
        self.assertEqual(result['send_id'], row['message_id'])
        self.assertEqual(result['outgoing_text'], row['text'])
        self.assertEqual(result['requested_text_used'], row['ack_origin'] == 'model')
        if row['ack_origin'] == 'model':
            with self.assertRaisesRegex(WeixinError, 'different_text'):
                self.p.weixin.queue_reply_phase(mid, 'ack', '改变的模型确认')
        else:
            replay = self.p.weixin.queue_reply_phase(mid, 'ack', '改变的模型确认')
            self.assertFalse(replay['requested_text_used'])
            self.assertEqual(replay['outgoing_text'], RECEIPT_TEXT)
        self.p.weixin.queue_reply(mid, '最终答复')
        while self.p.weixin.send_once():
            pass
        self.assertLessEqual(len(self.client.sent), 2)
        self.assertEqual(self.client.sent[-1][1], '最终答复')

    def test_model_ack_selected_first_is_existing_and_text_is_immutable(self):
        self.activate()
        mid = self.fresh()
        selected = self.p.weixin.queue_reply_phase(mid, 'ack', '模型先确认')
        self.assertTrue(selected['requested_text_used'])
        self.assertTrue(self.due())
        result = self.p.receipts.for_message(mid)
        self.assertEqual((result['intent']['state'], result['intent']['decision']),
                         ('existing', 'ack_already_committed'))
        with self.assertRaisesRegex(WeixinError, 'different_text'):
            self.p.weixin.queue_reply_phase(mid, 'ack', '另一模型文字')
        self.assertEqual(self.ack(mid)['text'], '模型先确认')
        self.p.weixin.queue_reply(mid, '详细最终答复')
        self.p.weixin.send_once()
        self.p.weixin.send_once()
        self.assertEqual([r[1] for r in self.client.sent], ['模型先确认', '详细最终答复'])

    def test_system_ack_selected_first_returns_actual_text_without_overwrite(self):
        self.activate()
        mid = self.fresh()
        self.due()
        original = dict(self.ack(mid))
        result = self.p.weixin.queue_reply_phase(mid, 'ack', '模型后确认')
        self.assertFalse(result['requested_text_used'])
        self.assertEqual(result['outgoing_text'], RECEIPT_TEXT)
        self.assertEqual((result['send_id'], self.ack(mid)['client_id']),
                         (original['message_id'], original['client_id']))
        self.assertEqual(dict(self.ack(mid)), original)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 1)

    def test_observer_without_durable_ingest_transaction_cannot_grant_receipt(self):
        self.activate()
        with self.assertRaisesRegex(WeixinError, 'receipt_observe_requires_ingest_transaction'):
            self.p.receipts.observe_new_in_transaction('wx_' + '0' * 32)
        with self.p.db:
            self.p.db.execute('BEGIN IMMEDIATE')
            self.assertFalse(self.p.receipts.observe_new_in_transaction('wx_' + '0' * 32))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM owner_receipt_intents').fetchone()[0], 0)

    def test_fast_final_before_due_suppresses_fallback(self):
        self.activate()
        mid = self.fresh()
        self.p.weixin.queue_reply(mid, '快速最终答复')
        self.assertTrue(self.due())
        self.assertEqual(self.p.receipts.for_message(mid)['intent']['state'], 'suppressed')
        self.assertIsNone(self.ack(mid))
        self.p.weixin.send_once()
        self.assertEqual([r[1] for r in self.client.sent], ['快速最终答复'])
        with self.assertRaisesRegex(WeixinError, 'final_already_committed'):
            self.p.weixin.queue_reply_phase(mid, 'ack', '迟到确认')

    def test_final_and_fallback_race_before_post_only_final_sends(self):
        self.activate()
        mid = self.fresh()
        self.now += .151
        second = self.open_probe()
        second.receipts.start_policy()
        barrier = threading.Barrier(2)
        def materialize():
            barrier.wait()
            return self.p.receipts.materialize_due()
        def final():
            barrier.wait()
            return second.weixin.queue_reply(mid, '竞争最终答复')
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(materialize), pool.submit(final)
            a.result(); b.result()
        ack = self.ack(mid)
        self.assertTrue(ack is None or ack['status'] == 'superseded')
        self.assertTrue(self.p.weixin.send_once())
        self.assertFalse(self.p.weixin.send_once())
        self.assertEqual([r[1] for r in self.client.sent], ['竞争最终答复'])

    def test_final_between_sender_selection_and_post_cas_prevents_late_ack(self):
        self.activate()
        mid = self.fresh()
        self.due()
        second = self.open_probe()
        original, calls = self.p.weixin.account, []
        def between_transactions():
            account = original()
            calls.append(True)
            if len(calls) == 2:
                second.weixin.queue_reply(mid, 'CAS之前最终答复')
            return account
        with patch.object(self.p.weixin, 'account', side_effect=between_transactions):
            self.p.weixin.send_once()
        self.assertEqual(self.ack(mid)['status'], 'superseded')
        self.assertEqual(self.client.sent, [])
        self.p.weixin.send_once()
        self.assertEqual([r[1] for r in self.client.sent], ['CAS之前最终答复'])

    def test_post_intent_is_irreversible_final_after_post_means_at_most_two(self):
        self.activate()
        mid = self.fresh()
        self.due()
        def after_post_intent():
            self.assertEqual((self.ack(mid)['status'], self.ack(mid)['attempts']), ('sending', 1))
            self.assertIsNotNone(self.ack(mid)['post_started_at'])
            self.p.weixin.queue_reply(mid, 'POST之后最终答复')
            self.assertEqual(self.ack(mid)['status'], 'sending')
        self.client.on_send = after_post_intent
        self.p.weixin.send_once()
        self.p.weixin.send_once()
        self.assertFalse(self.p.weixin.send_once())
        self.assertEqual([r[1] for r in self.client.sent], [RECEIPT_TEXT, 'POST之后最终答复'])
        self.assertEqual(len({r[2] for r in self.client.sent}), 2)
        result = self.p.weixin.queue_reply_phase(mid, 'ack', '不得出现第三条')
        self.assertFalse(result['requested_text_used'])
        self.assertFalse(self.p.weixin.send_once())

    def test_system_rate_context_and_auth_rejection_never_retry(self):
        self.activate()
        for i, response in enumerate(({'ret': -2, 'errmsg': 'rate limit'},
                                      {'ret': -2, 'errmsg': 'prepare failed'}, {'ret': -14})):
            with self.subTest(response=response):
                mid = self.fresh('single-post-' + str(i))
                self.due()
                before = len(self.client.sent)
                self.client.results = [response]
                self.p.weixin.send_once()
                row = self.ack(mid)
                self.assertEqual((row['status'], row['attempts']), ('rejected', 1))
                self.assertEqual(row['error_kind'], 'system_receipt_post_rejected')
                self.now += 100
                self.assertFalse(self.p.weixin.send_once())
                self.p.weixin.queue_reply(mid, '失败后最终答复')
                if response['ret'] == -14:
                    self.assertEqual(self.p.weixin.account()['status'], 'auth_required')
                    self.assertFalse(self.p.weixin.send_once())
                    # Fixture-only recovery of the existing owner identity.
                    self.p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': ACCOUNT,
                        'ilink_user_id': OWNER, 'bot_token': 'synthetic-secret-fixture-0000000000000000'})
                self.p.weixin.send_once()
                self.assertEqual(len(self.client.sent) - before, 2)
                self.assertEqual(self.ack(mid)['attempts'], 1)

    def test_timeout_and_restart_do_not_resend_unknown_ack(self):
        self.activate()
        mid = self.fresh()
        self.due()
        self.client.results = [WeixinError('api_timeout')]
        self.p.weixin.send_once()
        self.assertEqual(self.ack(mid)['status'], 'delivery_unknown')
        self.p.weixin.queue_reply(mid, '未知ACK后最终答复')
        second = self.open_probe()
        second.receipts.start_policy()
        second.weixin.recover_inflight()
        second.weixin.send_once()
        self.now += 100
        self.assertFalse(second.weixin.send_once())
        self.assertEqual([r[1] for r in self.client.sent], [RECEIPT_TEXT, '未知ACK后最终答复'])
        self.assertFalse(second.receipts.for_message(mid)['phone_delivery_confirmed'])

    def test_crash_after_post_intent_recovers_unknown_without_resending(self):
        self.activate()
        mid = self.fresh()
        self.due()
        with self.p.db:
            self.p.db.execute("UPDATE weixin_outbox SET status='sending',attempts=1,post_started_at=? WHERE message_id=?",
                              (self.now, self.p.weixin.ack_send_id(mid)))
        second = self.open_probe()
        self.assertEqual(second.weixin.send_status(second.weixin.ack_send_id(mid))['status'], 'sending')
        second.weixin.recover_inflight()
        second.receipts.start_policy()
        self.assertFalse(second.weixin.send_once())
        self.assertEqual(self.ack(mid)['status'], 'delivery_unknown')
        self.assertEqual(self.client.sent, [])

    def test_ledger_rejects_queued_superseded_system_ack_and_no_resurrection(self):
        self.activate()
        mid = self.fresh()
        event = self.linked(mid, 'ledger-queued')
        self.due()
        with self.assertRaisesRegex(WeixinError, 'dot_mirror_ack_not_sent_yet'):
            self.p.dot_mirrors.prepare(mid, event, 'ack')
        self.p.weixin.queue_reply(mid, '最终答复')
        with self.assertRaisesRegex(WeixinError, 'dot_mirror_ack_superseded'):
            self.p.dot_mirrors.prepare(mid, event, 'ack')
        replay = self.p.weixin.queue_reply_phase(mid, 'ack', '模型迟到确认')
        self.assertEqual(replay['status'], 'superseded')
        self.assertFalse(replay['requested_text_used'])
        self.p.weixin.send_once()
        self.now += 100
        self.assertFalse(self.p.receipts.materialize_due())
        self.assertFalse(self.p.weixin.send_once())
        self.assertEqual([r[1] for r in self.client.sent], ['最终答复'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_mirrors').fetchone()[0], 0)

    def test_ledger_sent_system_ack_only_reports_metadata_not_delivery_proof(self):
        self.activate()
        for state in ('sending', 'accepted', 'delivery_unknown'):
            with self.subTest(state=state):
                mid = self.fresh('ledger-' + state)
                event = self.linked(mid, state)
                self.due()
                with self.p.db:
                    self.p.db.execute('UPDATE weixin_outbox SET status=?,attempts=1 WHERE message_id=?',
                                      (state, self.p.weixin.ack_send_id(mid)))
                receipt = self.p.dot_mirrors.prepare(mid, event, 'ack')
                self.assertTrue(receipt['claim_granted'])
                self.assertFalse(receipt['target_id_verified_by_bridge'])
                self.assertFalse(receipt['dot_visible_verified_by_bridge'])
                self.assertTrue(receipt['suppress_reflection'])
                self.assertEqual(receipt['direction'], 'weixin_to_dot')
        self.assertEqual(self.client.sent, [])

    def test_prepared_unknown_dot_source_and_unknown_outbox_hold_fallback(self):
        self.activate()
        for i, state in enumerate(('prepared', 'unknown')):
            mid = self.fresh('held-ledger-' + state)
            with self.p.db:
                self.p.db.execute("INSERT INTO weixin_dot_mirrors(id,message_id,event_id,attachment_id,phase,state,attempt_id,created_at,updated_at) VALUES(?,?,?,'','final',?,?,?,?)",
                                  ('dm_' + str(i) * 32, mid, 'evt-held-' + state,
                                   state, uuid.uuid4().hex, self.now, self.now))
            self.due()
            result = self.p.receipts.for_message(mid)
            self.assertEqual((result['intent']['state'], result['intent']['decision']), ('waiting', 'uncertain_source'))
            self.assertIsNone(result['ack'])
        mid = self.fresh('held-outbox')
        notice = self.p.weixin.queue_notification('synthetic-unknown', 'fixture')['send_id']
        with self.p.db:
            self.p.db.execute("UPDATE weixin_outbox SET status='delivery_unknown',source_message_id=? WHERE message_id=?", (mid, notice))
        self.due()
        # Previously held rows are retried fairly, still without granting a send.
        for _ in range(4):
            self.now += .51
            self.p.receipts.materialize_due()
        self.assertEqual(self.p.receipts.for_message(mid)['intent']['decision'], 'uncertain_source')
        self.assertIsNone(self.ack(mid))
        self.assertEqual(self.client.sent, [])

    def test_full_queue_defers_and_later_capacity_materializes_exactly_once(self):
        self.activate()
        mid = self.fresh()
        for i in range(32):
            self.p.weixin.queue_notification('capacity-' + str(i), 'fixture')
        self.assertFalse(self.due())
        self.assertEqual(self.p.receipts.for_message(mid)['intent']['decision'], 'queue_full')
        self.assertIsNone(self.ack(mid))
        with self.p.db:
            self.p.db.execute("UPDATE weixin_outbox SET status='rejected' WHERE message_id=(SELECT message_id FROM weixin_outbox LIMIT 1)")
        self.now += .51
        self.assertTrue(self.p.receipts.materialize_due())
        self.assertEqual(self.p.db.execute("SELECT count(*) FROM weixin_outbox WHERE status='queued'").fetchone()[0], 32)
        self.assertFalse(self.p.receipts.materialize_due())

    def test_materialize_insert_failure_rolls_back_and_retry_uses_same_ack_identity(self):
        self.activate()
        mid = self.fresh()
        self.now += .151
        self.p.db.execute("CREATE TEMP TRIGGER reject_fixture_receipt BEFORE INSERT ON weixin_outbox BEGIN SELECT RAISE(ABORT,'offline insertion failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.p.receipts.materialize_due()
        self.assertEqual(self.p.receipts.for_message(mid)['intent']['state'], 'waiting')
        self.assertIsNone(self.ack(mid))
        self.p.db.execute('DROP TRIGGER reject_fixture_receipt')
        self.assertTrue(self.p.receipts.materialize_due())
        self.assertEqual(self.ack(mid)['message_id'], self.p.weixin.ack_send_id(mid))

    def test_final_failed_capacity_transaction_does_not_leave_ack_superseded(self):
        self.activate()
        mid = self.fresh()
        self.due()
        for i in range(31):
            self.p.weixin.queue_notification('full-' + str(i), 'fixture')
        # Superseding frees one slot, so force another check failure inside the
        # transaction to verify rollback of both the status change and final.
        with patch.object(self.p.weixin, 'check_queue_capacity', side_effect=WeixinError('send_queue_busy')):
            with self.assertRaisesRegex(WeixinError, 'send_queue_busy'):
                self.p.weixin.queue_reply(mid, '不能提交的最终答复')
        self.assertEqual(self.ack(mid)['status'], 'queued')
        self.assertIsNone(self.p.db.execute('SELECT 1 FROM weixin_outbox WHERE message_id=?', (mid,)).fetchone())

    def test_policy_disable_cancels_only_unposted_receipts_and_preserves_accepted(self):
        self.activate()
        accepted = self.fresh('accepted-before-disable')
        self.due()
        self.p.weixin.send_once()
        queued = self.fresh('queued-before-disable')
        self.due()
        waiting = self.fresh('waiting-before-disable')
        self.policy(enabled=False)
        status = self.p.receipts.start_policy()
        self.assertFalse(status['running'])
        self.assertEqual(self.ack(accepted)['status'], 'accepted')
        self.assertEqual(self.ack(queued)['status'], 'cancelled')
        self.assertEqual(self.p.receipts.for_message(waiting)['intent']['state'], 'cancelled')
        self.assertFalse(self.p.weixin.send_once())
        self.assertEqual(len(self.client.sent), 1)


if __name__ == '__main__':
    unittest.main()
