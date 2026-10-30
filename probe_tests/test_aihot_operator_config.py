"""Offline public-release gates; all routing identities are synthetic fixtures."""
from dataclasses import FrozenInstanceError
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from probe.aihot_email import AihotEmailEvents, email_tools
from probe.aihot_notice_consumer.bridge_hook import BridgeHook
from probe.aihot_notice_consumer.operator_config import AihotOperatorConfig
from probe.aihot_notice_consumer.relay import email_payload
from probe.probe import Probe
from probe import probe as bridge


def fixture_values():
    approval_id = 'wx_' + 'a' * 32
    return {
        'primary_link': 'link_' + 'b' * 32,
        'recipient': 'owner@example.invalid',
        'profile_emails': frozenset(('owner@example.invalid', 'alias@example.invalid')),
        'approval_id': approval_id,
        'reboot_approval': 'synthetic-reboot-approval',
        'approval_message_sha256': hashlib.sha256(approval_id.encode('utf-8')).hexdigest(),
        'sender_test_subject': 'Synthetic sender verification',
        'sender_test_marker': 'SYNTHETIC-SENDER-CHECK',
    }


def fixture_config(**changes):
    return AihotOperatorConfig(**{**fixture_values(), **changes})


class NeverNetwork:
    def __getattr__(self, name):
        def forbidden(*args, **kwargs):
            raise AssertionError('synthetic test attempted a transport operation')
        return forbidden


class AihotOperatorConfigTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.socket.connect', 'socket.socket.connect_ex',
                       'socket.create_connection', 'socket.getaddrinfo',
                       'subprocess.run', 'subprocess.Popen'):
            guard = patch(target, side_effect=AssertionError('offline fixture only'))
            guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='aihot-config-fixture-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_complete_synthetic_configuration_is_immutable(self):
        config = fixture_config()
        self.assertEqual(config.recipient, 'owner@example.invalid')
        self.assertIsInstance(config.profile_emails, frozenset)
        self.assertNotIn(config.recipient, repr(config))
        self.assertNotIn(config.primary_link, repr(config))
        with self.assertRaises(FrozenInstanceError):
            config.recipient = 'different@example.invalid'

    def test_every_required_field_rejects_omission_none_or_blank(self):
        original = fixture_values()
        for field in original:
            with self.subTest(field=field, invalid='omitted'):
                values = original.copy(); values.pop(field)
                with self.assertRaises((TypeError, ValueError)):
                    AihotOperatorConfig(**values)
            invalid = (None, '', ' \t\n')
            if field in ('sender_test_subject', 'sender_test_marker', 'reboot_approval'):
                invalid += ('\ud800',)
            for value in invalid:
                with self.subTest(field=field, invalid=repr(value)):
                    with self.assertRaisesRegex(ValueError, '^invalid_aihot_operator_configuration$'):
                        fixture_config(**{field: value})

    def test_profile_set_must_be_nonempty_and_contain_only_valid_addresses(self):
        for value in (frozenset(), frozenset((None,)), frozenset(('',)),
                      frozenset((' \t',)), frozenset(('not-an-email',)),
                      ['owner@example.invalid'], {'owner@example.invalid'}):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaisesRegex(ValueError, '^invalid_aihot_operator_configuration$'):
                    fixture_config(profile_emails=value)

    def test_invalid_approval_shape_or_mismatched_digest_never_constructs(self):
        for value in ('synthetic-message', 'wx_' + 'a' * 31, 'wx_' + 'g' * 32):
            with self.subTest(invalid='approval_shape'):
                with self.assertRaisesRegex(ValueError, '^invalid_aihot_operator_configuration$'):
                    fixture_config(approval_id=value)
        for value in ('f' * 63, 'g' * 64, 'f' * 64):
            with self.subTest(invalid='approval_digest'):
                with self.assertRaisesRegex(ValueError, '^invalid_aihot_operator_configuration$'):
                    fixture_config(approval_message_sha256=value)

    def test_configuration_does_not_implicitly_read_environment_or_files(self):
        with patch('os.getenv', side_effect=AssertionError('implicit environment read')), \
                patch.object(Path, 'read_text', side_effect=AssertionError('implicit file read')), \
                patch.object(Path, 'read_bytes', side_effect=AssertionError('implicit file read')), \
                patch.object(Path, 'open', side_effect=AssertionError('implicit file open')):
            config = fixture_config()
        self.assertEqual(config.approval_id, fixture_values()['approval_id'])

    def new_probe(self, **options):
        original_exists = Path.exists

        def only_temporary_exists(path):
            if path != self.root and self.root not in path.parents:
                raise AssertionError('probe examined a path outside its synthetic state')
            return original_exists(path)

        with patch('probe.aihot_notice_consumer.bridge_hook.BridgeHook') as hook, \
                patch('probe.probe.AihotEmailEvents') as email, \
                patch.object(Path, 'exists', only_temporary_exists):
            probe = Probe(self.root / 'state', weixin_client=NeverNetwork(), **options)
            self.addCleanup(probe.close)
            hook.assert_not_called()
            email.assert_not_called()
        self.assertIsNone(probe.aihot_notices)
        self.assertIsNone(probe.aihot_email)
        self.assertEqual(probe.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)
        return probe

    def test_probe_defaults_leave_private_consumers_off_without_private_path_scan(self):
        probe = self.new_probe()
        self.assertFalse(probe.aihot_guard_required)
        tools = {tool['name'] for tool in probe.tools()}
        self.assertTrue({'get_weixin_status', 'read_weixin_message',
                         'reply_weixin_message', 'reply_weixin_message_phase'} <= tools)

    def test_explicit_enable_without_config_keeps_notification_guard_and_owner_reply_path(self):
        probe = self.new_probe(aihot_enabled=True, email_enabled=True, aihot_operator_config=None)
        self.assertTrue(probe.aihot_guard_required)
        probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                                     'ilink_user_id': 'fixture-owner', 'bot_token': 'fixture-token'})
        probe.weixin.ingest(probe.weixin.account(), {'msgs': [{
            'message_id': 'fixture-source', 'message_type': 1, 'message_state': 2,
            'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-bot',
            'context_token': 'fixture-context',
            'item_list': [{'type': 1, 'text_item': {'text': 'Synthetic owner request'}}]}]})
        message_id = probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?',
                                      ('fixture-source',)).fetchone()[0]
        probe.weixin.queue_reply(message_id, 'Synthetic owner answer')
        row = probe.db.execute('SELECT kind,status FROM weixin_outbox WHERE message_id=?',
                               (message_id,)).fetchone()
        self.assertEqual(tuple(row), ('reply', 'queued'))

    def test_supplied_configuration_does_not_enable_or_arm_by_itself(self):
        probe = self.new_probe(aihot_operator_config=fixture_config())
        self.assertFalse(probe.aihot_guard_required)

    def run_synthetic_cli(self, *, retained_policy=False):
        """Exercise the actual CLI and one sender pass without any thread/network."""
        observed = {'sent': [], 'constructor_options': None}

        class SyntheticSender:
            def send(self, account, peer, text, client_id, context):
                observed['sent'].append((text, client_id))
                return {'ret': 0}

        def construct(directory, **options):
            observed['constructor_options'] = options.copy()
            probe = Probe(directory, weixin_client=SyntheticSender(), **options)
            probe.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
                'ilink_user_id': 'fixture-owner', 'bot_token': 'fixture-token'})
            probe.weixin.ingest(probe.weixin.account(), {'msgs': [{
                'message_id': 'fixture-cli-source', 'message_type': 1, 'message_state': 2,
                'from_user_id': 'fixture-owner', 'to_user_id': 'fixture-bot',
                'context_token': 'fixture-context',
                'item_list': [{'type': 1, 'text_item': {'text': 'Synthetic CLI request'}}]}]})
            source = probe.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?',
                                      ('fixture-cli-source',)).fetchone()[0]
            probe.weixin.queue_reply(source, 'Synthetic owner answer')
            probe.weixin.queue_notification('fixture-explicit-notice', 'Synthetic approved notice')
            observed['initial_queue'] = [tuple(row) for row in probe.db.execute(
                'SELECT kind,status FROM weixin_outbox ORDER BY rowid')]
            if retained_policy:
                # A retained marker must hold notifications even without a hook.
                with probe.db:
                    probe.db.execute("INSERT INTO aihot_auto_resume(id,policy) VALUES(1,'{}')")
            return probe

        class NoBackgroundThread:
            def __init__(self, *, target, args=(), **unused):
                self.target, self.args = target, args

            def start(self):
                if self.target is bridge.run_weixin_sender:
                    probe, stop = self.args
                    with patch.object(probe.sender_wakeup, 'wait', side_effect=lambda *_: stop.set()):
                        self.target(probe, stop)
                    observed['held'] = probe.weixin.aihot_hold_all_notifications
                    observed['final_queue'] = [tuple(row) for row in probe.db.execute(
                        'SELECT kind,status,attempts FROM weixin_outbox ORDER BY rowid')]

            def join(self, **unused):
                pass

            def is_alive(self):
                return False

        with patch.object(bridge, 'Probe', side_effect=construct), \
                patch.object(bridge, 'acquire_worker_lock', return_value=object()), \
                patch.object(bridge.threading, 'Thread', NoBackgroundThread), \
                patch.object(bridge.signal, 'signal'), \
                patch.object(bridge.sys, 'argv', ['probe', '--state-dir', str(self.root / 'cli-state')]), \
                patch.object(bridge.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b''))):
            self.assertEqual(bridge.main(), 0)
        return observed

    def test_standard_cli_keeps_private_consumers_off_and_allows_approved_ordinary_notice(self):
        observed = self.run_synthetic_cli()
        self.assertFalse(observed['constructor_options'].get('aihot_enabled', False))
        self.assertFalse(observed['constructor_options'].get('email_enabled', False))
        self.assertEqual(observed['initial_queue'], [('reply', 'queued'), ('notification', 'queued')])
        self.assertFalse(observed['held'])
        self.assertEqual(observed['final_queue'], [('reply', 'accepted', 1), ('notification', 'accepted', 1)])
        self.assertEqual([text for text, _ in observed['sent']],
                         ['Synthetic owner answer', 'Synthetic approved notice'])
        self.assertEqual(len({send_id for _, send_id in observed['sent']}), 2)

    def test_standard_cli_preserves_retained_policy_guard_without_blocking_owner_reply(self):
        observed = self.run_synthetic_cli(retained_policy=True)
        self.assertTrue(observed['held'])
        self.assertEqual(observed['final_queue'], [('reply', 'accepted', 1), ('notification', 'queued', 0)])
        self.assertEqual([text for text, _ in observed['sent']], ['Synthetic owner answer'])

    def test_missing_email_configuration_fails_before_probe_access_or_state_io(self):
        with patch('probe.aihot_email.Relay', side_effect=AssertionError('relay constructed')), \
                patch.object(Path, 'open', side_effect=AssertionError('file opened')), \
                patch.object(Path, 'read_text', side_effect=AssertionError('file read')), \
                patch.object(Path, 'exists', side_effect=AssertionError('path examined')):
            with self.assertRaisesRegex(ValueError, '^aihot_operator_configuration_required$'):
                AihotEmailEvents(object(), self.root / 'absent-data', operator_config=None)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_hook_configuration_fails_before_host_or_state_io(self):
        with patch('probe.aihot_notice_consumer.bridge_hook.ExistingOwnerHost',
                   side_effect=AssertionError('owner host constructed')), \
                patch('probe.aihot_notice_consumer.bridge_hook.Controller',
                      side_effect=AssertionError('controller constructed')), \
                patch.object(Path, 'open', side_effect=AssertionError('file opened')), \
                patch.object(Path, 'read_text', side_effect=AssertionError('file read')), \
                patch.object(Path, 'exists', side_effect=AssertionError('path examined')):
            with self.assertRaisesRegex(ValueError, '^aihot_operator_configuration_required$'):
                BridgeHook(object(), data_dir=self.root / 'absent-data', operator_config=None)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_email_payload_configuration_fails_before_inspecting_intent(self):
        with self.assertRaisesRegex(ValueError, '^aihot_operator_configuration_required$'):
            email_payload(object(), operator_config=None)

    def test_email_payload_uses_only_the_explicit_synthetic_recipient(self):
        intent = {'event_id': 'reset-1900000000000000001-1-1', 'stage': 'confirmed',
                  'text': 'Synthetic public-source fixture'}
        config = fixture_config()
        payload = email_payload(intent, operator_config=config)
        self.assertEqual(payload['to'], config.recipient)
        other = fixture_config(recipient='another@example.invalid',
                               profile_emails=frozenset(('another@example.invalid',)))
        self.assertEqual(email_payload(intent, operator_config=other)['to'], other.recipient)

    def test_public_email_schema_has_generic_route_fields_and_no_personal_literals(self):
        definitions = {tool['name']: tool for tool in email_tools()}
        proof = definitions['arm_aihot_email_notifications']['inputSchema']['properties']['sender_test']['properties']
        for field in ('link_id', 'from_address', 'to'):
            self.assertEqual(proof[field].get('type'), 'string')
            self.assertNotIn('const', proof[field])
            self.assertNotIn('enum', proof[field])
        route = definitions['claim_aihot_email']['inputSchema']['properties']['profile_link_id']
        self.assertEqual(route.get('type'), 'string')
        self.assertNotIn('const', route)
        self.assertNotIn('enum', route)
        raw = json.dumps(definitions, ensure_ascii=False)
        self.assertIsNone(re.search(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', raw))
        self.assertNotIn('/Users/', raw)
        self.assertNotIn('/home/', raw)
        self.assertIsNone(re.search(r'\b(?:link_|wx_)[0-9a-f]{32}\b', raw))
        for value in fixture_values().values():
            if isinstance(value, str):
                self.assertNotIn(value, raw)


if __name__ == '__main__':
    unittest.main()
