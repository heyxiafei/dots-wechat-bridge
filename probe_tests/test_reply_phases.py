"""Offline fixed-slot reply tests. No real account, callback or network send."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from probe.probe import Probe
from probe.weixin import WeixinError
from test_weixin import ACCOUNT, OWNER, FakeIlink


class ReplyPhaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1800000000.0
        self.client = FakeIlink()
        self.probe = self.open_probe()
        self.addCleanup(lambda: self.probe.close())
        self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': ACCOUNT,
            'ilink_user_id': OWNER, 'bot_token': 'fixture-bot-token'})
        self.mid = self.ingest('first')

    def open_probe(self):
        return Probe(self.directory.name, clock=lambda: self.now, weixin_client=self.client)

    def ingest(self, source, context='fixture-context'):
        self.probe.weixin.ingest(self.probe.weixin.account(), {'ret': 0, 'msgs': [{
            'message_id': source, 'from_user_id': OWNER, 'to_user_id': ACCOUNT,
            'message_type': 1, 'message_state': 2, 'create_time_ms': self.now * 1000,
            'context_token': context, 'item_list': [{'type': 1, 'text_item': {'text': '精确原文'}}]}]})
        return self.probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()['id']

    def commit_pair(self):
        ack = self.probe.weixin.queue_reply_phase(self.mid, 'ack', '快速回复')
        final = self.probe.weixin.queue_reply_phase(self.mid, 'final', '详细回答')
        return ack['send_id'], final['send_id']

    def state(self):
        return self.probe.weixin.read_message(self.mid)['reply_state']

    def test_pair_same_clock_order_ids_and_replay(self):
        ack, final = self.commit_pair()
        self.assertNotEqual(ack, final)
        self.assertEqual(final, self.mid)
        self.assertTrue(self.probe.weixin.send_status(final)['waiting_for_ack'])
        self.assertTrue(self.probe.weixin.send_once())
        self.assertTrue(self.probe.weixin.send_once())
        for _ in range(2):
            self.assertEqual(self.probe.weixin.queue_reply_phase(self.mid, 'ack', '快速回复')['send_id'], ack)
            self.assertEqual(self.probe.weixin.queue_reply_phase(self.mid, 'final', '详细回答')['send_id'], final)
        self.assertFalse(self.probe.weixin.send_once())
        self.assertEqual([s[1] for s in self.client.sent], ['快速回复', '详细回答'])
        self.assertEqual(len({s[2] for s in self.client.sent}), 2)
        self.assertIsNone(self.state()['final']['delivered'])

    def test_read_reports_new_pending_and_complete_slots_without_dot_claim(self):
        state = self.state()
        self.assertEqual(state['next_phase'], 'ack')
        self.assertFalse(state['ack']['committed'])
        self.assertFalse(state['final']['committed'])
        self.assertEqual(state['dot_mirror_status'], 'not_tracked')
        self.assertEqual(state['original_mirror_key'], 'weixin_original:' + self.mid)
        self.probe.weixin.queue_reply_phase(self.mid, 'ack', '快速回复')
        self.assertEqual(self.state()['next_phase'], 'final')
        self.assertEqual(self.state()['ack']['outgoing_text'], '快速回复')
        self.probe.weixin.queue_reply_phase(self.mid, 'final', '详细回答')
        self.assertEqual(self.state()['next_phase'], 'none')
        self.assertEqual(self.state()['final']['status'], 'queued')

    def test_legacy_final_same_slot_and_no_retroactive_ack(self):
        self.probe.weixin.queue_reply(self.mid, '旧回答')
        self.assertTrue(self.state()['final']['committed'])
        self.assertEqual(self.state()['next_phase'], 'none')
        self.assertEqual(self.probe.weixin.queue_reply_phase(self.mid, 'final', '旧回答')['send_id'], self.mid)
        with self.assertRaisesRegex(WeixinError, 'reply_final_already_committed'):
            self.probe.weixin.queue_reply_phase(self.mid, 'ack', '快速回复')
        with self.assertRaisesRegex(WeixinError, 'different_text'):
            self.probe.weixin.queue_reply_phase(self.mid, 'final', '新回答')
        self.probe.weixin.send_once()
        self.assertEqual(len(self.client.sent), 1)

    def test_legacy_tool_after_ack_obeys_order_and_shares_final(self):
        ack = self.probe.weixin.queue_reply_phase(self.mid, 'ack', '快速回复')
        self.probe.weixin.queue_reply(self.mid, '详细回答')
        self.probe.weixin.queue_reply_phase(self.mid, 'final', '详细回答')
        self.probe.weixin.send_once(); self.probe.weixin.send_once()
        self.assertEqual([s[1] for s in self.client.sent], ['快速回复', '详细回答'])
        self.assertEqual(self.state()['ack']['send_id'], ack['send_id'])

    def test_validation_and_each_slot_conflict_is_immutable(self):
        with self.assertRaisesRegex(WeixinError, 'reply_ack_required'):
            self.probe.weixin.queue_reply_phase(self.mid, 'final', '详细回答')
        for phase in ('other', None, [], {}):
            with self.assertRaisesRegex(WeixinError, 'invalid_reply_phase'):
                self.probe.weixin.queue_reply_phase(self.mid, phase, '内容')
        for text in ('', ' ', '中' * 2731, None):
            with self.assertRaises(WeixinError):
                self.probe.weixin.queue_reply_phase(self.mid, 'ack', text)
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.probe.weixin.queue_reply_phase('other-message', 'ack', '内容')
        self.commit_pair()
        for phase in ('ack', 'final'):
            with self.assertRaisesRegex(WeixinError, 'different_text'):
                self.probe.weixin.queue_reply_phase(self.mid, phase, '不同内容')
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 2)

    def test_ack_rate_backoff_blocks_only_own_final_and_retries_same_id(self):
        ack, final = self.commit_pair()
        other = self.ingest('other')
        self.probe.weixin.queue_reply(other, '其他回复')
        self.client.results = [{'ret': -2, 'errmsg': 'rate limit'}, {'ret': 0}, {'ret': 0}, {'ret': 0}]
        self.probe.weixin.send_once()
        self.assertTrue(self.probe.weixin.send_once())
        self.assertEqual(self.client.sent[-1][1], '其他回复')
        self.assertFalse(self.probe.weixin.send_once())
        self.now += 6
        self.probe.weixin.send_once(); self.probe.weixin.send_once()
        self.assertEqual([s[1] for s in self.client.sent], ['快速回复', '其他回复', '快速回复', '详细回答'])
        self.assertEqual(self.client.sent[0][2], self.client.sent[2][2])
        self.assertEqual(self.probe.weixin.send_status(final)['status'], 'accepted')

    def test_ack_three_definite_failures_release_final(self):
        ack, final = self.commit_pair()
        self.client.results = [{'ret': -2, 'errmsg': 'rate limit'}] * 3
        for _ in range(3):
            self.probe.weixin.send_once(); self.now += 30
        self.assertEqual(self.probe.weixin.send_status(ack)['status'], 'rejected')
        self.probe.weixin.send_once()
        self.assertTrue(self.probe.weixin.send_status(final)['accepted'])
        self.assertEqual(len(self.client.sent), 4)

    def test_uncertain_ack_never_retries_but_final_is_independent(self):
        ack, final = self.commit_pair()
        self.client.results = [WeixinError('api_timeout'), {'ret': 0}]
        self.probe.weixin.send_once(); self.probe.weixin.send_once()
        self.now += 100
        self.probe.weixin.queue_reply_phase(self.mid, 'ack', '快速回复')
        self.assertFalse(self.probe.weixin.send_once())
        self.assertEqual(self.probe.weixin.send_status(ack)['status'], 'delivery_unknown')
        self.assertTrue(self.probe.weixin.send_status(final)['accepted'])
        self.assertEqual(len(self.client.sent), 2)

    def test_failed_final_keeps_accepted_ack_no_regeneration(self):
        ack, final = self.commit_pair()
        self.client.results = [{'ret': 0}, {'ret': 7}]
        self.probe.weixin.send_once(); self.probe.weixin.send_once()
        self.assertEqual(self.state()['next_phase'], 'none')
        self.assertEqual(self.probe.weixin.send_status(ack)['status'], 'accepted')
        self.assertEqual(self.probe.weixin.send_status(final)['status'], 'rejected')
        self.assertFalse(self.probe.weixin.send_once())

    def test_context_wait_refresh_and_latest_context_for_each_phase(self):
        self.commit_pair()
        self.client.results = [{'ret': -2, 'errmsg': 'prepare failed'}]
        self.probe.weixin.send_once()
        self.assertFalse(self.probe.weixin.send_once())
        self.assertTrue(self.state()['final']['waiting_for_ack'])
        self.now += 10
        self.ingest('fresh', context='fresh-context')
        self.probe.weixin.send_once()
        self.now += 10
        self.ingest('freshest', context='freshest-context')
        self.probe.weixin.send_once()
        self.assertEqual(self.client.sent[-2][3], 'fresh-context')
        self.assertEqual(self.client.sent[-1][3], 'freshest-context')

    def test_restart_inflight_ack_becomes_uncertain_and_final_stays_once(self):
        ack, final = self.commit_pair()
        self.probe.db.execute("UPDATE weixin_outbox SET status='sending',attempts=1 WHERE message_id=?", (ack,))
        self.probe.db.commit()
        self.probe.close(); self.probe = self.open_probe()
        self.assertEqual(self.state()['ack']['status'], 'sending')
        self.probe.weixin.recover_inflight()
        self.probe.weixin.send_once()
        self.assertEqual([s[1] for s in self.client.sent], ['详细回答'])
        self.probe.close(); self.probe = self.open_probe()
        self.assertEqual(self.state()['final']['send_id'], final)
        self.assertFalse(self.probe.weixin.send_once())

    def test_concurrent_duplicate_commits_have_exactly_two_rows(self):
        barrier = threading.Barrier(8)
        def commit(_):
            barrier.wait()
            return self.commit_pair()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(commit, range(8)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 2)

    def test_queue_bound_applies_to_each_phase_but_not_replays(self):
        ack = self.probe.weixin.queue_reply_phase(self.mid, 'ack', '快速回复')
        for i in range(31):
            self.probe.weixin.queue_notification(str(i), 'fixture notification')
        with self.assertRaisesRegex(WeixinError, 'send_queue_busy'):
            self.probe.weixin.queue_reply_phase(self.mid, 'final', '详细回答')
        self.assertEqual(self.probe.weixin.queue_reply_phase(self.mid, 'ack', '快速回复')['send_id'], ack['send_id'])

    def test_auth_rebindings_cannot_exceed_three_posts_for_ack(self):
        ack, final = self.commit_pair()
        self.client.results = [{'ret': -14}] * 3
        for _ in range(3):
            self.probe.weixin.send_once()
            self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': ACCOUNT,
                'ilink_user_id': OWNER, 'bot_token': 'fixture-bot-token'})
        self.probe.weixin.send_once()
        self.assertEqual(self.probe.weixin.send_status(ack)['status'], 'rejected')
        self.probe.weixin.send_once()
        self.assertEqual(len(self.client.sent), 4)
        self.assertEqual(self.probe.weixin.send_status(final)['status'], 'accepted')

    def test_rpc_schema_roundtrip_strict_arguments_and_safe_read(self):
        tool = next(t for t in self.probe.tools() if t['name'] == 'reply_weixin_message_phase')
        self.assertEqual(tool['inputSchema']['properties']['phase']['enum'], ['ack', 'final'])
        self.assertFalse(tool['inputSchema']['additionalProperties'])
        call = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'reply_weixin_message_phase', 'arguments': {
                'message_id': self.mid, 'phase': 'ack', 'text': '快速回复'}}}
        result = self.probe.rpc(call)['result']
        self.assertEqual(result['structuredContent']['reply_phase'], 'ack')
        call['params']['arguments']['extra'] = True
        self.assertIn('error', self.probe.rpc(call))
        self.assertNotIn('fixture-bot-token', json.dumps(self.state()))
        self.assertNotIn('fixture-context', json.dumps(self.state()))
        self.assertNotIn(OWNER, json.dumps(self.state()))

    def test_040_database_and_legacy_client_id_survive_upgrade(self):
        self.probe.weixin.queue_reply(self.mid, '旧回答')
        before = self.probe.db.execute('SELECT client_id FROM weixin_outbox WHERE message_id=?', (self.mid,)).fetchone()[0]
        # 0.4.0 rows have no source_message_id value and no ack rows/index.
        self.probe.db.execute('UPDATE weixin_outbox SET source_message_id=NULL')
        self.probe.db.execute('DROP INDEX weixin_reply_order'); self.probe.db.commit()
        self.probe.close(); self.probe = self.open_probe()
        self.assertEqual(self.state()['final']['outgoing_text'], '旧回答')
        self.probe.weixin.queue_reply(self.mid, '旧回答')
        self.probe.weixin.send_once()
        self.assertEqual(self.client.sent[0][2], before)
        self.assertEqual(self.state()['ack']['status'], 'not_committed')


if __name__ == '__main__':
    unittest.main()
