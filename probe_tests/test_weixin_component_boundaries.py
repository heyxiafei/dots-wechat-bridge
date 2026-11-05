"""Cross-component transport contracts, using a disposable DB and fake iLink.

The refactor must keep live host controls, one transaction and original send
identities authoritative across the new receiver/protocol/dispatcher boundaries.
"""
import hashlib
import tempfile
import unittest
from unittest.mock import Mock, patch

from probe.probe import Probe
from probe.weixin import WeixinError, describe_inbound, encoded


OWNER, ACCOUNT = 'fixture-owner', 'fixture-component-bot'


def forbidden(*args, **kwargs):
    raise AssertionError('component tests prohibit network and worker startup')


class Client:
    def __init__(self, result=None):
        self.result = {} if result is None else result
        self.sent = []

    def send(self, account, peer, text, client_id, context):
        self.sent.append((peer, text, client_id, context))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class WeixinComponentBoundaryTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo', 'socket.create_connection',
                       'socket.socket.connect', 'socket.socket.connect_ex',
                       'threading.Thread.start'):
            guard = patch(target, side_effect=forbidden)
            guard.start()
            self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1800000000.0
        self.client = Client()
        self.probe = Probe(self.temp.name, clock=lambda: self.now,
                           post=forbidden, weixin_client=self.client)
        self.addCleanup(self.probe.close)
        self.store = self.probe.weixin
        self.store.bind_confirmed(dict(status='confirmed', ilink_bot_id=ACCOUNT,
            ilink_user_id=OWNER, bot_token='fixture-token'))

    def message(self, source='1001', text='fixture request', **changes):
        result = dict(message_id=source, from_user_id=OWNER, to_user_id=ACCOUNT,
                      message_type=1, message_state=2, create_time_ms=self.now * 1000,
                      context_token='fixture-context',
                      item_list=[dict(type=1, text_item=dict(text=text))])
        result.update(changes)
        return result

    def ingest(self, *messages, cursor='fixture-cursor'):
        return self.store.ingest(self.store.account(), dict(msgs=list(messages), get_updates_buf=cursor))

    def message_id(self, source='1001'):
        return 'wx_' + hashlib.sha256(encoded([ACCOUNT, OWNER, source]).encode()).hexdigest()[:32]

    def test_inbound_uses_live_facade_clock_parser_and_wakeup_after_commit(self):
        wakes = []
        self.store.clock = lambda: self.now + 20

        def wake():
            wakes.append((self.probe.db.in_transaction, self.store.account()['cursor']))

        with patch.object(self.store, 'notify_sender', side_effect=wake), \
             patch('probe.weixin.describe_inbound', wraps=describe_inbound) as describe:
            self.assertEqual(self.ingest(self.message(create_time_ms=None)), 1)
            describe.assert_called_once()
        self.assertEqual(wakes, [(False, 'fixture-cursor')])
        row = self.probe.db.execute('SELECT created_at FROM weixin_inbox').fetchone()
        self.assertEqual(row['created_at'], self.now + 20)
        self.assertEqual(self.store.account()['last_poll'], self.now + 20)
        self.assertFalse(self.store.read_message(self.message_id())['reply_state']['final']['committed'])
        self.assertEqual(self.client.sent, [])

    def test_late_capture_failure_rolls_back_all_input_facts_and_wakes_nothing(self):
        original = self.probe.receipts.observe_new_in_transaction
        calls = []

        def observe(mid):
            calls.append(mid)
            if len(calls) == 2:
                raise RuntimeError('fixture late capture failure')
            return original(mid)

        with patch.object(self.probe.receipts, 'observe_new_in_transaction', side_effect=observe), \
             patch.object(self.store, 'notify_sender') as sender, \
             patch.object(self.probe, 'notify_events') as events:
            with self.assertRaisesRegex(RuntimeError, 'late capture failure'):
                self.ingest(self.message(), self.message('1002'))
            sender.assert_not_called()
            events.assert_not_called()
        for table in ('weixin_inbox', 'weixin_inbound_manifests', 'weixin_quote_captures',
                      'weixin_server_message_ids', 'weixin_peers', 'weixin_outbox'):
            self.assertEqual(self.probe.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(self.store.account()['cursor'], '')
        self.assertIsNone(self.store.account()['last_poll'])
        self.assertEqual(self.ingest(self.message(), self.message('1002')), 2)
        self.assertEqual(self.store.read_message(self.message_id())['text'], 'fixture request')
        self.assertEqual(self.client.sent, [])

    def test_replaced_client_uses_original_commitment_and_unknown_never_replays(self):
        self.ingest(self.message())
        mid = self.message_id()
        committed = self.store.queue_reply(mid, 'fixture final')
        uncertain = Client(WeixinError('api_timeout'))
        self.store.client = uncertain
        self.assertTrue(self.store.send_once())
        self.assertEqual(self.client.sent, [])
        self.assertEqual(uncertain.sent[0][2], 'dots_' + mid[3:])
        self.assertEqual(self.store.send_status(mid)['status'], 'delivery_unknown')
        replacement = Client()
        self.store.client = replacement
        self.now += 100
        self.store.recover_inflight()
        repeated = self.store.queue_reply(mid, 'fixture final')
        self.assertEqual(repeated['send_id'], committed['send_id'])
        self.assertFalse(self.store.send_once())
        self.assertEqual(replacement.sent, [])
        self.assertEqual(self.store.send_status(mid)['attempts'], 1)

    def test_live_notification_hold_does_not_change_reply_priority_or_identity(self):
        self.ingest(self.message())
        notice = self.store.queue_notification('fixture-notice', 'fixture notice')
        self.store.aihot_held_notification_ids = frozenset((notice['send_id'],))
        final = self.store.queue_reply(self.message_id(), 'fixture final')
        self.assertTrue(self.store.send_once())
        self.assertEqual(self.client.sent[0][1], 'fixture final')
        self.assertEqual(self.store.send_status(final['send_id'])['status'], 'accepted')
        self.assertFalse(self.store.send_once())
        self.assertEqual(self.store.send_status(notice['send_id'])['attempts'], 0)
        self.store.aihot_held_notification_ids = frozenset()
        self.assertTrue(self.store.send_once())
        self.assertEqual(self.client.sent[-1][2], 'dots_' + notice['send_id'])

    def test_accepted_server_id_crosses_components_without_granting_quote_authority(self):
        self.ingest(self.message())
        mid = self.message_id()
        self.store.queue_reply(mid, 'fixture original final')
        self.store.client = Client(dict(message_id='9001'))
        self.assertTrue(self.store.send_once())
        quote = dict(type=1, text_item=dict(text='/help'), ref_msg=dict(
            svr_id='9001', message_item=dict(type=1, text_item=dict(text='untrusted quote'))))
        self.ingest(self.message('1002', item_list=[quote]))
        source = self.store.read_message(self.message_id('1002'))
        captured = source['quote_context']['quotes'][0]
        self.assertEqual(captured['match']['status'], 'exact_stored_id')
        self.assertEqual(captured['match']['send_id'], mid)
        self.assertTrue(captured['untrusted'])
        self.assertFalse(captured['match']['grants_authority'])
        self.assertFalse(captured['match']['quoted_content_verified'])
        self.assertIsNone(source['fixed_command'])
        self.assertIsNone(source['help_menu'])
        self.assertFalse(source['reply_state']['final']['committed'])
        self.assertEqual(len(self.store.client.sent), 1)


if __name__ == '__main__':
    unittest.main()
