"""Offline ingress → MCP read → host command handler → original final queue."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from probe.owner_coordinator import OwnerCoordinator
from probe.official_usage_provider import OfficialUsageProvider
from probe.probe import Probe
from probe.usage_display import UsageBinding


class CommandPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch('socket.socket.connect', side_effect=AssertionError('offline test attempted network')).start()
        self.clock = 1900000000
        self.probe = Probe(Path(self.tmp.name) / 'bridge', clock=lambda: self.clock)
        self.addCleanup(self.probe.close)
        self.probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                                         'ilink_user_id': 'fixture-owner', 'bot_token': 'fixture-token'})
        self.binding = UsageBinding('fixture-owner', 'fixture-account', True)
        self.reader = Mock(return_value={'rateLimits': {'primary': {
            'usedPercent': 25, 'windowDurationMins': 10080, 'resetsAt': self.clock + 1000}}})
        self.provider = OfficialUsageProvider.from_host_tools(
            {'mcp__codex_app__get_usage_limits': self.reader}, self.binding, 'fixture-host-proof',
            clock=lambda: self.clock)
        self.calls = []
        self.coordinator = OwnerCoordinator(Path(self.tmp.name) / 'host',
            coordinator_id='fixture-coordinator', caller_id='fixture-coordinator',
            call_tool=self.tool, mode='execute', permit=lambda *_: True,
            usage_provider=self.provider, reply_projection_supported=True)
        self.addCleanup(self.coordinator.close)
        self.number = 0

    def tool(self, name, arguments):
        self.calls.append(name)
        response = self.probe.rpc({'jsonrpc': '2.0', 'id': 'fixture-rpc', 'method': 'tools/call',
                                   'params': {'name': name, 'arguments': arguments}})
        self.assertNotIn('error', response)
        self.assertFalse(response['result'].get('isError'))
        return response['result']['structuredContent']

    def ingest(self, text, **item_extra):
        self.number += 1
        source_id = str(self.number)
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [{
            'message_id': source_id, 'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-bot',
            'message_type': 1, 'message_state': 2, 'create_time_ms': self.clock * 1000,
            'context_token': 'fixture-context', 'item_list': [dict(
                type=1, text_item={'text': text}, **item_extra)]}]})
        return self.probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?',
                                     (source_id,)).fetchone()[0]

    def handle(self, message_id):
        return self.coordinator.handle_fixed_command(message_id, binding=self.binding,
            owner_intent_verified=True, scope={'verified_scope': True,
                'request_ref': message_id, 'evidence_origin': 'direct_owner_message',
                'owner_ref': self.binding.owner_ref, 'account_ref': self.binding.account_ref})

    def test_help_normalization_reaches_original_final_once_without_quota_read(self):
        for text in ('/help', ' /HELP ', '/help?', ' /HeLp？ '):
            with self.subTest(text=text):
                message_id = self.ingest(text, is_completed=True, msg_id='fixture-item')
                first = self.handle(message_id)
                self.assertEqual(first['fixed_command'], 'help')
                again = self.handle(message_id)
                self.assertEqual(again['send_id'], first['send_id'])
                state = self.probe.weixin.read_message(message_id)['reply_state']
                self.assertFalse(state['ack']['committed'])
                self.assertTrue(state['final']['committed'])
                self.assertIn('微信助手 · 使用指南', state['final']['outgoing_text'])
                self.assertIn('额度：查询当前 Codex 周额度', state['final']['outgoing_text'])
        self.assertEqual(self.calls.count('reply_weixin_message'), 4)
        self.reader.assert_not_called()

    def test_weekly_alias_reaches_official_adapter_and_idempotent_four_line_final(self):
        message_id = self.ingest(' 查一下额度？ ')
        first = self.handle(message_id)
        self.assertEqual(first['usage_status'], 'reported')
        final = self.probe.weixin.read_message(message_id)['reply_state']['final']
        self.assertEqual(len(final['outgoing_text'].splitlines()), 4)
        self.assertIn('剩余75%', final['outgoing_text'])
        self.assertEqual(self.handle(message_id)['send_id'], first['send_id'])
        self.reader.assert_called_once_with({})
        self.assertEqual(self.calls.count('reply_weixin_message'), 1)

    def test_normalized_help_with_unknown_field_stays_rejected_and_no_history_promotion(self):
        message_id = self.ingest(' /HELP? ', unknown_field=None)
        before = self.probe.db.execute('SELECT metadata_json FROM weixin_inbound_manifests '
                                      'WHERE message_id=?', (message_id,)).fetchone()[0]
        self.assertEqual(self.handle(message_id)['action'], 'not_fixed_command')
        self.assertEqual(self.handle(message_id)['action'], 'not_fixed_command')
        after = self.probe.db.execute('SELECT metadata_json FROM weixin_inbound_manifests '
                                     'WHERE message_id=?', (message_id,)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertNotIn('reply_weixin_message', self.calls)
        self.reader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
