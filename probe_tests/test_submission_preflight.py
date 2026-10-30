"""Synthetic submission boundaries; no live host, accounts or network."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from probe.owner_coordinator import OwnerCoordinator, PlatformDenied


class SubmissionPreflightTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.create_connection', 'socket.getaddrinfo',
                       'subprocess.run', 'subprocess.Popen'):
            guard = patch(target, side_effect=AssertionError('offline fixture only'))
            guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='submission-preflight-')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'ledger'
        self.coordinator = None
        self.addCleanup(self.close)
        self.calls = []
        self.failure = None
        self.permit = Mock(return_value=True)
        self.message = {'source': 'owner_weixin_dm', 'message_id': 'synthetic-request',
            'kind': 'text', 'text': '额度', 'attachments': [],
            'quote_context': {'recorded': True, 'complete': True, 'status': 'none'},
            'inbound_completeness': {'complete': True, 'command_context_v1': True,
                'parts': [{'kind': 'text', 'state': 'captured', 'item_type': 1}]},
            'reply_state': {'ack': {'committed': False, 'send_id': 'synthetic-ack'},
                            'final': {'committed': False, 'send_id': 'synthetic-final'}}}
        self.scope = {'verified_scope': True, 'request_ref': 'synthetic-request',
                      'evidence_origin': 'direct_owner_message'}
        self.item = {'owner_ref': 'synthetic-owner', 'request_ref': 'synthetic-request',
            'artifact_key': 'synthetic-artifact', 'attachment_key': 'synthetic-attachment',
            'sha256': hashlib.sha256(b'synthetic file').hexdigest(), 'kind': 'requested_document'}
        self.media_scope = {**self.scope, **{k: self.item[k] for k in
            ('owner_ref', 'artifact_key', 'attachment_key', 'sha256', 'kind')},
            'evidence_ref': 'synthetic-evidence', 'media_kind': 'file'}
        self.open()

    def close(self):
        if self.coordinator:
            self.coordinator.close(); self.coordinator = None

    def open(self, mode='execute'):
        self.coordinator = OwnerCoordinator(self.directory, coordinator_id='synthetic-coordinator',
            caller_id='synthetic-coordinator', call_tool=self.host, mode=mode, permit=self.permit)

    def host(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == 'read_weixin_message':
            return copy.deepcopy(self.message)
        if name == 'get_weixin_upload_status':
            return {'status': 'ready', 'kind': 'file', 'sha256': self.item['sha256']}
        if name in ('reply_weixin_message', 'reply_weixin_message_phase', 'send_weixin_media'):
            if self.failure:
                raise self.failure
            if name != 'send_weixin_media':
                # Reproduce the existing bridge transport boundary locally.
                text = args['text']
                if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 8192:
                    raise ValueError('synthetic transport rejected reply')
            return {'send_id': 'synthetic-final', 'status': 'accepted'}
        raise AssertionError('unexpected tool')

    def reply(self, text, *, task_complete=True, needs_work=False):
        return self.coordinator.reply(self.message['message_id'], text,
            task_complete=task_complete, needs_work=needs_work, scope=self.scope)

    def media(self):
        return self.coordinator.media(self.item, self.media_scope,
                                      archive_verified=True, upload_id='synthetic-upload')

    def writes(self):
        return [x for x in self.calls if x[0] in
                ('reply_weixin_message', 'reply_weixin_message_phase', 'send_weixin_media')]

    def rows(self):
        return self.coordinator.db.execute('SELECT * FROM actions ORDER BY key').fetchall()

    def assert_unclaimed(self):
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.writes(), [])
        self.permit.assert_not_called()

    def test_exact_ascii_byte_limit_is_submitted_without_rewriting(self):
        text = ' ' + 'a' * 8190 + ' '
        result = self.reply(text)
        self.assertEqual(result['action'], 'query_original_send')
        self.assertEqual(self.writes()[0][1]['text'], text)
        self.assertEqual(len(self.writes()), 1)
        self.permit.assert_called_once()

    def test_exact_multibyte_limit_is_submitted_without_splitting(self):
        text = '中' * 2730 + 'ab'
        self.assertEqual(len(text.encode('utf-8')), 8192)
        self.assertEqual(self.reply(text)['action'], 'query_original_send')
        self.assertEqual(self.writes()[0][1]['text'], text)
        self.assertEqual(len(self.writes()), 1)

    def test_8193_ascii_and_multibyte_are_rejected_before_permit_or_claim(self):
        for text in ('a' * 8193, '中' * 2731):
            with self.subTest(byte_count=len(text.encode('utf-8'))):
                with self.assertRaisesRegex(ValueError, 'reply_text_must_be_1_to_8192_bytes'):
                    self.reply(text)
                self.assert_unclaimed()

    def test_unpaired_surrogates_are_rejected_before_permit_or_claim(self):
        for text in ('\ud800', 'prefix\udfff'):
            with self.subTest(position=len(text)):
                with self.assertRaisesRegex(ValueError, 'reply_text_must_be_1_to_8192_bytes'):
                    self.reply(text)
                self.assert_unclaimed()

    def test_empty_whitespace_and_nonstring_are_not_submission_claims(self):
        for text in ('', ' \t\n', None, False, 7, b'bytes', {}, [], object()):
            with self.subTest(kind=type(text).__name__):
                with self.assertRaisesRegex(ValueError, 'reply_text_required'):
                    self.reply(text)
                self.assert_unclaimed()

    def test_unsubmitted_invalid_text_can_be_corrected_under_original_identity_after_reopen(self):
        with self.assertRaises(ValueError):
            self.reply('中' * 2731)
        self.close(); self.open()
        result = self.reply('corrected synthetic answer')
        self.assertEqual(result['action'], 'query_original_send')
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0][1]['message_id'], 'synthetic-request')
        self.assertEqual(self.rows()[0][2], 'submitted')

    def test_durable_denied_unknown_and_submitted_precede_invalid_replacement_text(self):
        for failure, action in ((PlatformDenied(), 'stop_permission_denied'),
                                (TimeoutError(), 'reconcile_original_submission'),
                                (None, 'query_original_send')):
            with self.subTest(action=action):
                with self.coordinator.db:
                    self.coordinator.db.execute('DELETE FROM actions')
                self.failure = failure
                first = self.reply('original synthetic answer')
                self.assertEqual(first['action'], action)
                before = self.rows()
                count = len(self.writes()); permits = self.permit.call_count
                self.close(); self.open(); self.failure = None
                for bad in ('', None, object(), 'a' * 8193, '\ud800'):
                    result = self.reply(bad)
                    self.assertEqual(result['action'], action)
                    self.assertEqual(result.get('send_id'), first.get('send_id'))
                    self.assertEqual(result['key'], first['key'])
                    self.assertTrue(result['content_conflict'])
                    self.assertEqual(self.rows(), before)
                self.assertEqual(len(self.writes()), count)
                self.assertEqual(self.permit.call_count, permits)

    def test_claimed_record_precedes_invalid_text_without_becoming_denied(self):
        key = hashlib.sha256(json.dumps(['text', 'synthetic-request', 'final'],
                                       separators=(',', ':')).encode()).hexdigest()
        with self.coordinator.db:
            self.coordinator.db.execute('INSERT INTO actions VALUES(?,?,?,NULL,NULL,NULL)',
                                        (key, 'synthetic-original-digest', 'claimed'))
        before = self.rows()
        self.permit.side_effect = PlatformDenied()
        result = self.reply(None)
        self.assertEqual(result['action'], 'reconcile_original_submission')
        self.assertEqual(result['key'], key)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.writes(), [])
        self.permit.assert_not_called()

    def test_legacy_invalid_text_keeps_its_original_digest_and_conflict_meaning(self):
        key = hashlib.sha256(json.dumps(['text', 'synthetic-request', 'final'],
                                       separators=(',', ':')).encode()).hexdigest()
        for text in ('a' * 8193, '\ud800'):
            with self.subTest(kind='oversized' if len(text) > 8192 else 'surrogate'):
                digest = hashlib.sha256(json.dumps({'text': text}, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
                with self.coordinator.db:
                    self.coordinator.db.execute('DELETE FROM actions')
                    self.coordinator.db.execute('INSERT INTO actions VALUES(?,?,?,NULL,NULL,NULL)',
                                                (key, digest, 'unknown'))
                before = self.rows()
                same = self.reply(text)
                self.assertEqual(same['action'], 'reconcile_original_submission')
                self.assertFalse(same['content_conflict'])
                self.assertTrue(self.reply(text + 'changed')['content_conflict'])
                self.assertEqual(self.rows(), before)
        self.assertEqual(self.writes(), [])
        self.permit.assert_not_called()

    def test_remote_committed_final_precedes_invalid_text(self):
        slot = self.message['reply_state']['final']
        slot['committed'] = True
        for bad in (None, '\ud800', 'a' * 8193):
            self.assertEqual(self.reply(bad), {'action': 'query_original_send', 'send_id': slot['send_id']})
        slot['send_id'] = None
        self.assertEqual(self.reply(None)['action'], 'reconcile_original_submission')
        self.assert_unclaimed()

    def test_progress_phase_uses_same_preflight_and_original_slot(self):
        with self.assertRaises(ValueError):
            self.reply('a' * 8193, task_complete=False, needs_work=True)
        self.assert_unclaimed()
        self.reply('中' * 2730 + 'ab', task_complete=False, needs_work=True)
        self.assertEqual(self.writes()[0][0], 'reply_weixin_message_phase')
        self.assertEqual(self.writes()[0][1]['phase'], 'ack')
        self.assertEqual(len(self.writes()), 1)

    def test_dry_run_validates_new_text_without_permit_or_claim(self):
        self.close(); self.open(mode='dry_run')
        for bad in ('a' * 8193, '\ud800'):
            with self.assertRaises(ValueError):
                self.reply(bad)
        self.assert_unclaimed()
        self.assertEqual(self.reply('valid synthetic answer')['action'], 'dry_run')
        self.assert_unclaimed()

    def test_fixed_command_sync_reuses_preflight_and_clears_inflight_marker(self):
        self.coordinator.prepare_usage = Mock(return_value={'text': 'a' * 8193, 'status': 'reported'})
        with self.assertRaises(ValueError):
            self.coordinator.handle_fixed_command('synthetic-request', scope=self.scope, owner_intent_verified=True)
        self.assert_unclaimed()
        self.coordinator.prepare_usage.return_value = {'text': 'corrected synthetic quota', 'status': 'reported'}
        result = self.coordinator.handle_fixed_command('synthetic-request', scope=self.scope, owner_intent_verified=True)
        self.assertEqual(result['action'], 'query_original_send')
        self.assertEqual(len(self.writes()), 1)

    def test_fixed_command_async_reuses_preflight_and_clears_inflight_marker(self):
        self.coordinator.prepare_usage_async = AsyncMock(return_value={'text': '\ud800', 'status': 'reported'})
        with self.assertRaises(ValueError):
            asyncio.run(self.coordinator.handle_fixed_command_async('synthetic-request',
                scope=self.scope, owner_intent_verified=True))
        self.assert_unclaimed()
        self.coordinator.prepare_usage_async.return_value = {'text': 'corrected synthetic quota', 'status': 'reported'}
        result = asyncio.run(self.coordinator.handle_fixed_command_async('synthetic-request',
            scope=self.scope, owner_intent_verified=True))
        self.assertEqual(result['action'], 'query_original_send')
        self.assertEqual(len(self.writes()), 1)

    def test_explicit_permit_denial_for_text_is_durable_before_tool_boundary(self):
        self.permit.side_effect = PlatformDenied()
        first = self.reply('original synthetic answer')
        self.assertEqual(first['action'], 'stop_permission_denied')
        before = self.rows()
        expected = hashlib.sha256(json.dumps({'text': 'original synthetic answer'},
                                  sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(before[0][1:4], (expected, 'denied', None))
        self.close(); self.open(); self.permit.side_effect = None
        result = self.reply('different synthetic answer')
        self.assertEqual(result['action'], 'stop_permission_denied')
        self.assertEqual(result['key'], first['key'])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.writes(), [])
        self.permit.assert_called_once()

    def test_explicit_permit_denial_for_media_is_durable_and_keeps_original_identity(self):
        self.permit.side_effect = PlatformDenied()
        first = self.media()
        self.assertEqual(first['action'], 'stop_permission_denied')
        before = self.rows()
        expected = hashlib.sha256(json.dumps({'sha256': self.item['sha256'], 'kind': 'file'},
                                  sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(before[0][1:4], (expected, 'denied', None))
        self.close(); self.open(); self.permit.side_effect = None
        self.item['sha256'] = self.media_scope['sha256'] = hashlib.sha256(b'changed synthetic file').hexdigest()
        result = self.media()
        self.assertEqual(result['action'], 'stop_permission_denied')
        self.assertEqual(result['key'], first['key'])
        self.assertTrue(result['content_conflict'])
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.writes(), [])
        self.permit.assert_called_once()

    def test_missing_or_false_permit_waits_without_recording_a_denial(self):
        self.coordinator.permit = None
        self.assertEqual(self.reply('valid synthetic answer')['action'], 'await_current_writer_and_owner_permission')
        self.assert_unclaimed()
        self.coordinator.permit = self.permit
        self.permit.return_value = False
        self.assertEqual(self.reply('valid synthetic answer')['action'], 'await_current_writer_and_owner_permission')
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.writes(), [])
        self.permit.return_value = True
        self.assertEqual(self.reply('valid synthetic answer')['action'], 'query_original_send')
        self.assertEqual(len(self.writes()), 1)

    def test_other_permit_exception_does_not_claim_or_submit(self):
        for failure in (TimeoutError(), RuntimeError('synthetic host failure')):
            self.permit.side_effect = failure
            with self.assertRaises(type(failure)):
                self.reply('valid synthetic answer')
            self.assertEqual(self.rows(), [])
            self.assertEqual(self.writes(), [])


if __name__ == '__main__':
    unittest.main()
