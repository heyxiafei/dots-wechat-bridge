"""Offline attachment scope tests; only synthetic data and explicit fake tools."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from probe.owner_coordinator import OwnerCoordinator
from probe.workflow_contract import attachment_plan


def source_item(**extra):
    return dict(owner_ref='synthetic-owner', request_ref='synthetic-request',
                artifact_key='synthetic-v1', attachment_key='source',
                sha256='a' * 64, kind='model_source', **extra)


def trusted_scope(item, **extra):
    return dict(item, verified_scope=True, evidence_origin='direct_owner_message',
                evidence_ref='synthetic-original-request-proof', media_kind='file', **extra)


class AttachmentScopeBoundaryTests(unittest.TestCase):
    def test_kind_must_match_trusted_scope_even_with_same_identity_hash_and_channel(self):
        original = source_item(original_requested=False)
        scope = trusted_scope(original)
        for kind in ('requested_document', 'requested_video', 'preview_image'):
            with self.subTest(kind=kind):
                changed = dict(original, kind=kind)
                self.assertEqual(attachment_plan(changed, scope, archive_verified=True)['action'],
                                 'await_trusted_authorization_context')
        for replacement in (None, '', 1, 'requested_document'):
            with self.subTest(scope_kind=replacement):
                self.assertEqual(attachment_plan(original, dict(scope, kind=replacement),
                                                archive_verified=True)['action'],
                                 'await_trusted_authorization_context')
        del scope['kind']
        self.assertEqual(attachment_plan(original, scope, archive_verified=True)['action'],
                         'await_trusted_authorization_context')

    def test_requested_source_requires_literal_true_in_original_scope(self):
        item = source_item(original_requested=True)
        for option in (False, None, 0, 1, 'true', 'false'):
            with self.subTest(option=option):
                scope = trusted_scope(item)
                scope['original_requested'] = option
                self.assertEqual(attachment_plan(item, scope, archive_verified=True)['action'],
                                 'await_trusted_authorization_context')
        scope = trusted_scope(item)
        del scope['original_requested']
        self.assertEqual(attachment_plan(item, scope, archive_verified=True)['action'],
                         'await_trusted_authorization_context')

    def test_unrequested_source_still_uses_drive_link_without_truthy_promotion(self):
        for option in (False, None, 0, 1, 'true', 'false'):
            with self.subTest(option=option):
                item = source_item(original_requested=option)
                self.assertEqual(attachment_plan(item, trusted_scope(item), archive_verified=True)['action'],
                                 'include_private_drive_link')
        item = source_item()
        self.assertEqual(attachment_plan(item, trusted_scope(item), archive_verified=True)['action'],
                         'include_private_drive_link')

    def test_matching_explicit_original_and_document_use_existing_file_path(self):
        for item in (source_item(original_requested=True), dict(source_item(), kind='requested_document')):
            with self.subTest(kind=item['kind']):
                result = attachment_plan(item, trusted_scope(item), archive_verified=True)
                self.assertEqual(result['action'], 'prepare_existing_media_path')
                self.assertEqual(result['media_kind'], 'file')
                self.assertFalse(result['authorization_granted'])
                self.assertTrue(result['platform_approval_still_required'])

    def test_denial_precedes_format_mismatch_and_archive_state(self):
        item = source_item(original_requested=True)
        scope = trusted_scope(item)
        scope.update(kind='requested_document', original_requested=False)
        self.assertEqual(attachment_plan(item, scope, archive_verified=False,
                                        permission_denied=True)['action'], 'stop_permission_denied')

    def test_identity_archive_and_channel_guards_remain_required(self):
        item = source_item(original_requested=True)
        scope = trusted_scope(item)
        for field in ('owner_ref', 'request_ref', 'artifact_key', 'attachment_key', 'sha256', 'media_kind'):
            with self.subTest(field=field):
                self.assertEqual(attachment_plan(item, dict(scope, **{field: 'other'}),
                                                archive_verified=True)['action'],
                                 'await_trusted_authorization_context')
        self.assertEqual(attachment_plan(item, scope, archive_verified=False)['action'],
                         'archive_and_verify_private_drive')


class AttachmentScopeCoordinatorTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo', 'socket.create_connection',
                       'socket.socket.connect', 'socket.socket.connect_ex',
                       'subprocess.run', 'subprocess.Popen'):
            guard = patch(target, side_effect=AssertionError('offline test forbids external action'))
            guard.start()
            self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='attachment-scope-boundaries-')
        self.addCleanup(self.temp.cleanup)
        self.calls = []
        self.permits = []
        self.permit_allowed = True
        self.coordinator = OwnerCoordinator(Path(self.temp.name) / 'coordinator',
            coordinator_id='synthetic-coordinator', caller_id='synthetic-coordinator',
            call_tool=self.tool, mode='execute', permit=self.permit)
        self.addCleanup(self.coordinator.close)

    def tool(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == 'read_weixin_message':
            return dict(message_id=args['message_id'], source='owner_weixin_dm',
                        reply_state={'ack': {'committed': False},
                                     'final': {'committed': True, 'send_id': 'synthetic-original-final'}})
        if name == 'get_weixin_upload_status':
            return dict(sha256='a' * 64, kind='file', status='ready')
        if name == 'send_weixin_media':
            return dict(send_id=args['send_id'], status='accepted')
        raise AssertionError('unexpected tool: ' + name)

    def permit(self, scope, tool):
        self.permits.append((copy.deepcopy(scope), tool))
        return self.permit_allowed

    def dispatch(self, item, scope):
        return self.coordinator.media(item, scope, archive_verified=True,
                                      upload_id='synthetic-upload')

    def test_tampered_format_or_original_option_never_reads_upload_sends_or_claims(self):
        original = source_item(original_requested=False)
        scope = trusted_scope(original)
        altered = [dict(original, original_requested=True),
                   dict(original, kind='requested_document')]
        for item in altered:
            with self.subTest(item=item):
                self.assertEqual(self.dispatch(item, scope)['action'], 'await_trusted_authorization_context')
                self.assertEqual(self.calls, [])
                self.assertEqual(self.permits, [])
                self.assertEqual(self.coordinator.db.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    def test_default_source_remains_link_without_any_tool_or_claim(self):
        item = source_item()
        self.assertEqual(self.dispatch(item, trusted_scope(item))['action'], 'include_private_drive_link')
        self.assertEqual(self.calls, [])
        self.assertEqual(self.permits, [])
        self.assertEqual(self.coordinator.db.execute('SELECT count(*) FROM actions').fetchone()[0], 0)

    def test_authorized_files_remain_independent_of_text_final_with_original_ids(self):
        results = []
        for attachment in ('source', 'source-second'):
            item = source_item(original_requested=True)
            item['attachment_key'] = attachment
            scope = trusted_scope(item)
            result = self.dispatch(item, scope)
            self.assertEqual(result['action'], 'query_original_send')
            self.assertEqual(result['api_state'], 'accepted')
            self.assertFalse(result['owner_received'])
            duplicate = self.dispatch(item, scope)
            self.assertEqual(duplicate['send_id'], result['send_id'])
            results.append(result)
        self.assertNotEqual(results[0]['send_id'], results[1]['send_id'])
        self.assertEqual(sum(name == 'send_weixin_media' for name, _ in self.calls), 2)
        self.assertEqual(sum(name == 'get_weixin_upload_status' for name, _ in self.calls), 2)
        self.assertEqual(self.coordinator.db.execute('SELECT count(*) FROM actions').fetchone()[0], 2)

    def test_matching_scope_still_cannot_replace_platform_permit(self):
        item = source_item(original_requested=True)
        self.permit_allowed = False
        result = self.dispatch(item, trusted_scope(item))
        self.assertEqual(result['action'], 'await_current_writer_and_owner_permission')
        self.assertFalse(any(name == 'send_weixin_media' for name, _ in self.calls))
        self.assertEqual(self.coordinator.db.execute('SELECT count(*) FROM actions').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
