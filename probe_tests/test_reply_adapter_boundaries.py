"""Synthetic host checks: no real accounts, tools, services or outbound calls."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from probe.owner_coordinator import OwnerCoordinator, PlatformDenied, from_host_configuration


class ReplyAdapterBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'ledger'
        self.coordinator = None
        self.addCleanup(self.close)
        guard = patch('socket.create_connection', side_effect=AssertionError('offline only'))
        guard.start(); self.addCleanup(guard.stop)
        self.calls = []
        self.error = None
        self.message = {'source': 'owner_weixin_dm', 'message_id': 'synthetic-message',
            'text': 'hello', 'attachments': [], 'quote_context': {'status': 'none'},
            'inbound_completeness': {'complete': True},
            'reply_state': {'ack': {'committed': False, 'send_id': 'synthetic-ack'},
                            'final': {'committed': False, 'send_id': 'synthetic-message'}}}
        self.scope = {'verified_scope': True, 'request_ref': self.message['message_id'],
                      'evidence_origin': 'direct_owner_message'}

    def close(self):
        if self.coordinator:
            self.coordinator.close(); self.coordinator = None

    def host(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == 'read_weixin_message':
            return copy.deepcopy(self.message)
        if name == 'reply_weixin_message':
            if self.error:
                raise self.error
            return {'send_id': 'synthetic-message', 'status': 'accepted'}
        raise AssertionError('unexpected alternate route: ' + name)

    def open(self, **options):
        self.coordinator = OwnerCoordinator(self.directory, coordinator_id='fixture', caller_id='fixture',
            call_tool=self.host, mode='execute', permit=lambda *_: True, **options)

    def reply(self, text='first answer'):
        return self.coordinator.reply(self.message['message_id'], text,
            task_complete=True, needs_work=False, scope=self.scope)

    def writes(self):
        return [x for x in self.calls if x[0] == 'reply_weixin_message']

    def test_changed_text_cannot_hide_denial_after_reopen(self):
        self.open(); self.error = PlatformDenied()
        self.assertEqual(self.reply()['action'], 'stop_permission_denied')
        self.close(); self.open(); self.error = None
        result = self.reply('regenerated answer')
        self.assertEqual(result['action'], 'stop_permission_denied')
        self.assertTrue(result['content_conflict'])
        self.assertEqual(len(self.writes()), 1)

    def test_changed_text_cannot_hide_unknown_submission_after_reopen(self):
        self.open(); self.error = TimeoutError()
        self.assertEqual(self.reply()['action'], 'reconcile_original_submission')
        self.close(); self.open(); self.error = None
        result = self.reply('different answer')
        self.assertEqual(result['action'], 'reconcile_original_submission')
        self.assertTrue(result['content_conflict'])
        self.assertEqual(len(self.writes()), 1)

    def test_changed_text_preserves_original_accepted_id_and_status(self):
        self.open(); first = self.reply()
        result = self.reply('different answer')
        self.assertEqual(result['action'], 'query_original_send')
        self.assertEqual(result['send_id'], first['send_id'])
        self.assertEqual(result['api_state'], 'accepted')
        self.assertTrue(result['content_conflict'])
        self.assertFalse(result['owner_received'])
        self.assertEqual(len(self.writes()), 1)

    def test_claimed_record_is_reconciled_despite_new_text(self):
        self.open()
        key = hashlib.sha256(json.dumps(['text', 'synthetic-message', 'final'],
                            separators=(',', ':')).encode()).hexdigest()
        with self.coordinator.db:
            self.coordinator.db.execute('INSERT INTO actions VALUES(?,?,?,NULL,NULL,NULL)',
                                        (key, 'old-digest', 'claimed'))
        result = self.reply()
        self.assertEqual(result['action'], 'reconcile_original_submission')
        self.assertTrue(result['content_conflict'])
        self.assertEqual(self.writes(), [])

    def test_unrelated_notification_denial_does_not_suppress_owner_reply(self):
        self.open()
        key = hashlib.sha256(json.dumps(['notification', 'independent-request'],
                            separators=(',', ':')).encode()).hexdigest()
        with self.coordinator.db:
            self.coordinator.db.execute('INSERT INTO actions VALUES(?,?,?,NULL,NULL,NULL)',
                                        (key, 'unrelated-digest', 'denied'))
        self.assertEqual(self.reply()['action'], 'query_original_send')
        self.assertEqual([name for name, _ in self.calls], ['read_weixin_message', 'reply_weixin_message'])
        self.assertEqual(self.coordinator.db.execute('SELECT state FROM actions WHERE key=?', (key,)).fetchone()[0], 'denied')

    def test_compatible_default_does_not_assume_projection_support(self):
        self.open(); self.reply()
        self.assertEqual(self.calls[0][1], {'message_id': 'synthetic-message'})

    def test_verified_projection_uses_one_read_then_one_direct_final(self):
        self.open(reply_projection_supported=True); self.reply()
        self.assertEqual(self.calls, [
            ('read_weixin_message', {'message_id': 'synthetic-message', 'projection': 'reply'}),
            ('reply_weixin_message', {'message_id': 'synthetic-message', 'text': 'first answer'})])

    def test_summary_or_incomplete_projection_stops_without_full_fallback(self):
        self.open(reply_projection_supported=True)
        for key in ('text', 'attachments', 'quote_context', 'inbound_completeness', 'reply_state'):
            value = self.message.pop(key)
            with self.assertRaisesRegex(ValueError, 'reply_projection_incomplete'):
                self.reply()
            self.message[key] = value
        self.assertEqual(self.writes(), [])
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all(args.get('projection') == 'reply' for _, args in self.calls))

    def test_projection_flag_is_literal_boolean_and_host_injected(self):
        for value in (1, 'true', None, {}):
            with self.assertRaisesRegex(ValueError, 'invalid_reply_projection_capability'):
                self.open(reply_projection_supported=value)
        self.assertFalse(self.directory.exists())
        config = {'version': 1, 'mode': 'execute', 'coordinator_id': 'fixture',
                  'usage_provider': 'get_usage_limits', 'system_ack': False, 'old_dot_mirror_writes': False}
        self.coordinator = from_host_configuration(config, self.directory, caller_id='fixture',
            call_tool=self.host, permit=lambda *_: True, reply_projection_supported=True)
        self.reply()
        self.assertEqual(self.calls[0][1]['projection'], 'reply')


if __name__ == '__main__':
    unittest.main()
