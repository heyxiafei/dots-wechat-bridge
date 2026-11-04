"""Synthetic cutover state only: no credentials, real network or services."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from probe.fresh_production import (FreshAdmission, MODE, binding_fingerprint,
                                    inspect_state, read_approval_file)
from probe.probe import Probe, canonical
from probe.weixin import WeixinError


class FreshProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root/'runtime/state'
        self.now = 1000.
        self.challenges = 0
        def post(url, body, headers):
            data = json.loads(body)
            if data.get('type') == 'verification':
                self.challenges += 1
                return 200, json.dumps({'challenge': data['challenge']}).encode()
            return 200, b'{}'
        self.post = post
        self.probe = Probe(self.state, post=post, allowed_hosts=['callback.example.com'], clock=lambda: self.now)
        self.addCleanup(lambda: self.probe.close())
        self.probe.weixin.bind_confirmed({'status':'confirmed','ilink_bot_id':'fixture-bot',
            'ilink_user_id':'fixture-owner','bot_token':'synthetic-token-fixture'})
        self.params = {'name':'weixin.message','arguments':{},'ttlMs':None,
            'delivery':{'mode':'webhook','url':'https://callback.example.com/fixture',
                        'secret':'whsec_' + base64.b64encode(b'f' * 32).decode()}}
        route = hashlib.sha256(canonical(['owner-only-tunnel',self.params['delivery']['url'],
                                          'weixin.message',{}]).encode()).hexdigest()
        self.approval = {'isolation_mode':'old_writer_fenced',
            'approval_reference':'synthetic-operator-approval',
            'isolation_evidence_reference':'synthetic-external-fence-receipt',
            'handoff':{'continuity_mode':MODE,'generation':'a'*32,
                'binding_sha256':binding_fingerprint(self.probe.db),
                'subscription_id':'sub_'+route[:32],'route_sha256':route,
                'tunnel_id':'tunnel_'+'0'*32,'approved_at_ms':998000,'cutoff_ms':999000,
                'platform_route_evidence_reference':'synthetic-original-route-receipt',
                'subscription_continuity_evidence_reference':'synthetic-original-subscription-receipt',
                'fresh_cutover_evidence_reference':'synthetic-future-only-approval'}}
        self.write_approval()
        self.net = patch('socket.create_connection', side_effect=AssertionError('network prohibited'))
        self.net.start()
        self.addCleanup(self.net.stop)

    def write_approval(self):
        path = self.root/'runtime/setup-approval.json'
        path.write_text(json.dumps(self.approval))
        path.chmod(0o600)

    def activate(self):
        self.policy = FreshAdmission.activate(self.probe, self.approval)
        return self.policy

    def ready(self):
        self.activate()
        self.probe.subscribe(self.params)
        self.policy.require_ready()

    def message(self, **changes):
        msg = {'message_id':'18446744073709551615','from_user_id':'fixture-owner',
               'to_user_id':'fixture-bot','message_type':1,'message_state':2,
               'create_time_ms':1000000,'context_token':'synthetic-context',
               'item_list':[{'type':1,'text_item':{'text':'synthetic future message'}}]}
        msg.update(changes)
        return msg

    def ingest(self, msg=None):
        self.probe.weixin.ingest(self.probe.weixin.account(),
            {'ret':0,'msgs':[msg or self.message()],'get_updates_buf':'synthetic-next-cursor'})
        return self.probe.db.execute('SELECT id FROM weixin_inbox').fetchone()[0]

    def test_explicit_approval_without_old_history_and_real_handshake(self):
        policy = self.activate()
        with self.assertRaisesRegex(WeixinError, 'fresh_original_handshake_required'):
            policy.require_ready()
        self.probe.subscribe(self.params)
        self.assertEqual(self.challenges, 1)
        policy.require_ready()
        mid = self.ingest()
        self.assertTrue(policy.allows_source(mid))
        self.assertEqual(self.probe.db.execute('SELECT source_id FROM weixin_fresh_admissions').fetchone()[0], '18446744073709551615')

    def test_restart_preserves_cutoff_and_requires_new_challenge(self):
        self.ready()
        mid = self.ingest()
        self.probe.close()
        self.probe = Probe(self.state, post=self.post, allowed_hosts=['callback.example.com'], clock=lambda:self.now)
        self.assertEqual(self.probe.fresh_admission.spec['cutoff_ms'], 999000)
        self.assertFalse(self.probe.fresh_admission.allows_source(mid))
        self.probe.subscribe(self.params)
        self.assertEqual(self.challenges, 2)
        self.assertTrue(self.probe.fresh_admission.allows_source(mid))

    def test_immutable_cutoff_and_admission(self):
        self.ready()
        mid = self.ingest()
        for sql in ('UPDATE weixin_fresh_cutover SET activated_at=0',
                    'DELETE FROM weixin_fresh_cutover',
                    'UPDATE weixin_fresh_admissions SET source_ms=0',
                    'DELETE FROM weixin_fresh_admissions'):
            with self.assertRaises(sqlite3.IntegrityError), self.probe.db:
                self.probe.db.execute(sql)
        self.assertTrue(self.policy.allows_source(mid))
        changed = copy.deepcopy(self.approval)
        changed['handoff']['cutoff_ms'] -= 1
        with self.assertRaises(WeixinError):
            FreshAdmission.activate(self.probe, changed)

    def test_old_sources_not_parsed_or_context_used(self):
        self.ready()
        old = self.message(create_time_ms=999000, item_list={'not':'parsed'})
        with patch('probe.weixin.describe_inbound', side_effect=AssertionError('old body parsed')):
            self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs':[old],'get_updates_buf':'old-skipped'})
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_peers').fetchone()[0], 0)
        self.assertEqual(self.probe.weixin.account()['cursor'], 'old-skipped')

    def test_invalid_whole_batch_rolls_back_and_keeps_cursor(self):
        self.ready()
        bad_values = (None, True, float('nan'), float('inf'), -1, 1000000.5, 2000000)
        for value in bad_values:
            with self.subTest(value=value), self.assertRaises(WeixinError):
                self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs':[
                    self.message(message_id='1'),self.message(create_time_ms=value)],
                    'get_updates_buf':'must-not-commit'})
            self.assertEqual(self.probe.weixin.account()['cursor'], '')
            self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
            self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_fresh_admissions').fetchone()[0], 0)

    def test_missing_source_never_falls_back_to_client_id(self):
        self.ready()
        for value in (None, True, 1.0, '18446744073709551616'):
            with self.subTest(value=value), self.assertRaises(WeixinError):
                self.ingest(self.message(message_id=value,client_id='123'))
        self.assertEqual(self.probe.weixin.account()['cursor'], '')

    def test_other_owner_and_group_skip_without_body_read(self):
        self.ready()
        for changes in ({'from_user_id':'other-owner'}, {'group_id':'synthetic-group'}):
            self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs':[self.message(
                item_list=object(),create_time_ms=None,**changes)]})
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)

    def test_actual_binding_epoch_and_approval_changes_block(self):
        self.ready()
        mid = self.ingest()
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_binding_epoch SET epoch='changed'")
        self.assertFalse(self.policy.allows_source(mid))
        self.approval['approval_reference'] = 'different-approval'
        self.write_approval()
        with self.assertRaises(WeixinError):
            self.policy.require_ready()

    def test_non_private_approval_not_authority(self):
        (self.root/'runtime/setup-approval.json').chmod(0o644)
        with self.assertRaises(WeixinError):
            self.activate()

    def test_legacy_rows_or_bounded_marker_cannot_be_promoted(self):
        self.probe.db.execute('CREATE TABLE weixin_bounded_dot_check(id INTEGER)')
        self.probe.db.commit()
        with self.assertRaisesRegex(WeixinError, 'fresh_diagnostic_state_not_promotable'):
            self.activate()

    def test_self_declared_first_install_cannot_bypass_fencing(self):
        self.approval['isolation_mode'] = 'no_existing_writer_verified'
        self.write_approval()
        with self.assertRaisesRegex(WeixinError, 'fresh_writer_isolation_required'):
            self.activate()

    def test_wrong_subscription_denied_without_challenge(self):
        self.activate()
        changed = copy.deepcopy(self.params)
        changed['delivery']['url'] += '-other'
        with self.assertRaises(WeixinError):
            self.probe.subscribe(changed)
        self.assertEqual(self.challenges, 0)

    def test_real_policy_local_reply_and_original_receipt_once(self):
        self.ready()
        mid = self.ingest()
        client = Mock()
        client.send.return_value = {}
        self.probe.weixin.client = client
        self.probe.weixin.queue_reply(mid, 'synthetic final')
        self.assertTrue(self.probe.weixin.send_once())
        self.assertEqual(self.probe.weixin.send_status(mid)['status'], 'accepted')
        self.assertFalse(self.probe.weixin.send_once())
        self.assertEqual(client.send.call_count, 1)

    def test_current_context_requires_immutable_generation_provenance(self):
        self.ready()
        mid = self.ingest()
        self.probe.weixin.queue_reply(mid, 'synthetic final')
        with self.probe.db:
            self.probe.db.execute("UPDATE weixin_peers SET token='different-context'")
        client = Mock()
        self.probe.weixin.client = client
        with self.assertRaisesRegex(WeixinError, 'fresh_context_not_admitted'):
            self.probe.weixin.send_once()
        client.send.assert_not_called()

    def test_duplicate_id_changed_time_does_not_advance_cursor(self):
        self.ready()
        self.ingest()
        with patch('probe.weixin.describe_inbound', side_effect=AssertionError('conflict body parsed')):
            with self.assertRaisesRegex(WeixinError, 'fresh_source_identity_conflict'):
                self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs':[
                    self.message(create_time_ms=1000001)], 'get_updates_buf':'conflict-cursor'})
        self.assertEqual(self.probe.weixin.account()['cursor'], 'synthetic-next-cursor')

    def test_fresh_unknown_event_projection_is_terminal(self):
        self.ready()
        mid = self.ingest()
        self.probe.queue_weixin_messages()
        self.probe.post = lambda *args: (500, b'{}')
        self.probe.deliver_once()
        status = self.probe.event_delivery.for_message(mid)
        self.assertEqual(status['status'], 'delivery_unknown')
        self.assertEqual(status['dispatch_blocked_reason'], 'event_delivery_unknown')
        self.assertFalse(self.probe.deliver_once())

    def test_fixed_receipt_policy_does_not_rearm(self):
        self.ready()
        with patch.object(self.probe.receipts, '_config', side_effect=AssertionError('must not rearm')):
            self.probe.receipts.start_policy()
        self.assertFalse(self.probe.receipts.running)

    def test_attachment_access_cannot_use_unadmitted_source(self):
        self.ready()
        with self.assertRaisesRegex(WeixinError, 'fresh_source_not_admitted'):
            self.probe.media.authorized('not-admitted', 'not-an-attachment')

    def test_installer_readonly_future_entry_and_rollback_guard(self):
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('fresh_install_review', root/'scripts/install-project.py')
        install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(install)
        command = f'{self.root}/.venv/bin/python {self.root}/probe/probe.py --state-dir {self.state} --fresh-production --allow-callback-host callback.example.com'
        (self.root/'runtime/profile.yaml').write_text('control_plane:\n  tunnel_id: '+self.approval['handoff']['tunnel_id']+'\nmcp:\n  commands:\n    - channel: main\n      command: '+command+'\n')
        with patch.object(install.time, 'time', return_value=self.now):
            receipt = install.require_state_continuity(self.root, self.approval)
        self.assertFalse(receipt['history_migrated'])
        profile = self.root/'runtime/profile.yaml'
        original = profile.read_text()
        profile.write_text(original.rstrip() + ' --state-dir /tmp/other-state\n')
        with self.assertRaisesRegex(ValueError, 'fresh_explicit_entry_required'):
            install.require_state_continuity(self.root, self.approval)
        profile.write_text(original)
        self.assertIsNone(self.probe.db.execute("SELECT 1 FROM sqlite_master WHERE name='weixin_fresh_cutover'").fetchone())
        self.activate()
        with self.assertRaisesRegex(ValueError, 'fresh_generation_requires_explicit_entry'):
            install.require_state_continuity(self.root, {'isolation_mode':'no_existing_writer_verified'})


if __name__ == '__main__':
    unittest.main()
