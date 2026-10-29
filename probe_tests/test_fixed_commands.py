"""Synthetic command integration. No network, real accounts or production sends."""
import json
import tempfile
import unittest
from probe.probe import Probe
from probe.weixin import WeixinError
from probe.fixed_commands import HELP_TEXT


class FixedCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.probe = Probe(self.tmp.name, clock=lambda: 1900000000.)
        self.addCleanup(self.probe.close)
        self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                                         'ilink_user_id': 'fixture-owner', 'bot_token': 'fixture-token'})
        self.n = 0

    def ingest(self, text, **changes):
        self.n += 1
        msg = {'message_id': str(self.n), 'from_user_id': 'fixture-owner',
               'to_user_id': 'fixture-bot', 'message_type': 1, 'message_state': 2,
               'create_time_ms': 1900000000000, 'context_token': 'fixture-context',
               'item_list': [{'type': 1, 'text_item': {'text': text}}]}
        msg.update(changes)
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [msg]})
        return self.probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (str(self.n),)).fetchone()[0]

    def hint(self, mid):
        return self.probe.weixin.read_message(mid)['fixed_command']

    def test_help_single_entry_and_honest_limit(self):
        hint = self.hint(self.ingest('/help'))
        self.assertEqual(hint['prepared_text'], HELP_TEXT)
        self.assertEqual(HELP_TEXT.count('额度：查询当前 Codex 周额度'), 1)
        self.assertIn('未接通插件自动读取', HELP_TEXT)
        self.assertNotIn('/quota', HELP_TEXT)
        self.assertFalse(hint['automatic_send'])

    def test_exact_aliases_only(self):
        for text in ('额度', '查额度', '查一下额度', '查一下我的周额度还剩多少'):
            hint = self.hint(self.ingest(text))
            self.assertEqual(hint['kind'], 'weekly_usage')
            self.assertTrue(hint['official_reader_required'])
            self.assertIsNone(hint['prepared_text'])
            self.assertFalse(hint['automatic_send'])
        for text in ('超额度', '/quota', '额度并发送邮件', '信用卡额度', '你好'):
            self.assertIsNone(self.hint(self.ingest(text)))
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_quotes_forward_and_attachments_rejected(self):
        cases = [
            {'forwarded': True}, {'ref_msg': {}},
            {'item_list': [{'type': 1, 'text_item': {'text': '额度'}, 'ref_msg': {}}]},
            {'item_list': [{'type': 1, 'text_item': {'text': '额度', 'quote': 'fixture'}}]},
            {'item_list': [{'type': 1, 'text_item': {'text': '额度'}}, {'type': 2, 'image_item': {}}]},
        ]
        for case in cases:
            self.assertIsNone(self.hint(self.ingest('额度', **case)))

    def test_old_capture_and_damage_do_not_enable(self):
        mid = self.ingest('额度')
        row = self.probe.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()
        capture = json.loads(row[0]); capture.pop('command_context_v1')
        with self.probe.db:
            self.probe.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?', (json.dumps(capture), mid))
        self.assertIsNone(self.hint(mid))
        with self.probe.db:
            self.probe.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?', ('bad json', mid))
        self.assertIsNone(self.hint(mid))

    def test_committed_final_immutable(self):
        mid = self.ingest('额度')
        self.probe.weixin.queue_reply(mid, 'fixture final')
        self.assertEqual(self.hint(mid)['state'], 'already_committed')
        self.probe.weixin.queue_reply(mid, 'fixture final')
        with self.assertRaises(WeixinError):
            self.probe.weixin.queue_reply(mid, 'fixture changed')
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 1)

    def test_existing_ack_prevents_second_preparation(self):
        mid = self.ingest('/help')
        self.probe.weixin.queue_reply_phase(mid, 'ack', 'fixture ack')
        self.assertEqual(self.hint(mid)['state'], 'already_committed')

    def test_unknown_send_not_requeued_by_read(self):
        mid = self.ingest('额度')
        self.probe.weixin.queue_reply(mid, 'fixture final')
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_outbox SET status='delivery_unknown',attempts=1 WHERE message_id=?", (mid,))
        self.assertEqual(self.hint(mid)['state'], 'already_committed')
        row = self.probe.db.execute('SELECT status,attempts FROM weixin_outbox WHERE message_id=?', (mid,)).fetchone()
        self.assertEqual(tuple(row), ('delivery_unknown', 1))
