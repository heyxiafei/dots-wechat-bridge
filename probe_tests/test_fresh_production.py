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

    def pin_subscription(self):
        self.probe.subscribe(self.params)
        row=self.probe.db.execute('SELECT id,verified_at,revision,expires FROM subscriptions').fetchone()
        spec=self.approval['handoff']
        spec['subscription_receipt']={'subscription_id':row[0],'route_sha256':spec['route_sha256'],
            'binding_sha256':spec['binding_sha256'],'verified_at':row[1],'revision':row[2],'expires_at':row[3]}
        self.write_approval()

    def test_approved_persisted_subscription_activates_and_restarts_without_network(self):
        self.pin_subscription()
        original=tuple(self.probe.db.execute('SELECT * FROM subscriptions').fetchone())
        self.activate().require_ready()
        mid=self.ingest()
        self.assertEqual(self.challenges,1)
        self.assertEqual(tuple(self.probe.db.execute('SELECT * FROM subscriptions').fetchone()),original)
        self.probe.close()
        self.probe=Probe(self.state,post=self.post,allowed_hosts=['callback.example.com'],clock=lambda:self.now)
        self.assertTrue(self.probe.fresh_admission.allows_source(mid))
        self.assertEqual(self.challenges,1)
        self.assertEqual(tuple(self.probe.db.execute('SELECT * FROM subscriptions').fetchone()),original)

    def test_initial_seed_requires_exact_receipt_from_after_current_approval(self):
        self.pin_subscription()
        original=copy.deepcopy(self.approval['handoff']['subscription_receipt'])
        for updates in ({'revision':'f'*32},{'expires_at':1100.},{'verified_at':997.},
                        {'route_sha256':'f'*64},{'binding_sha256':'f'*64}):
            with self.subTest(updates=updates):
                self.approval['handoff']['subscription_receipt']={**original,**updates}
                self.write_approval()
                with self.assertRaises(WeixinError):
                    self.activate()
                self.assertIsNone(self.probe.db.execute("SELECT 1 FROM sqlite_master WHERE name='weixin_fresh_cutover'").fetchone())
        self.assertEqual(self.challenges,1)

    def test_expired_seed_blocks_but_real_same_id_renewal_survives_restart(self):
        self.params['ttlMs']=1000
        self.pin_subscription()
        seed=copy.deepcopy(self.approval['handoff']['subscription_receipt'])
        self.activate().require_ready()
        self.now=1002.
        self.probe.close()
        self.probe=Probe(self.state,post=self.post,allowed_hosts=['callback.example.com'],clock=lambda:self.now)
        with self.assertRaises(WeixinError):
            self.probe.fresh_admission.require_ready()
        self.assertEqual(self.probe.db.execute('SELECT expires FROM subscriptions').fetchone()[0],1001.)
        self.probe.subscribe(self.params)
        self.probe.fresh_admission.require_ready()
        self.assertEqual(self.challenges,2)
        renewed=tuple(self.probe.db.execute('SELECT verified_at,revision,expires FROM subscriptions').fetchone())
        self.assertGreater(renewed[0],seed['verified_at'])
        self.probe.close()
        self.probe=Probe(self.state,post=self.post,allowed_hosts=['callback.example.com'],clock=lambda:self.now)
        self.probe.fresh_admission.require_ready()
        self.assertEqual(self.challenges,2)
        self.assertEqual(tuple(self.probe.db.execute('SELECT verified_at,revision,expires FROM subscriptions').fetchone()),renewed)
        self.assertEqual(self.approval['handoff']['subscription_receipt'],seed)

    def test_persisted_seed_never_accepts_regressed_verification_or_changed_binding(self):
        self.pin_subscription()
        self.activate().require_ready()
        with self.probe.db:
            self.probe.db.execute('UPDATE subscriptions SET verified_at=997')
        with self.assertRaises(WeixinError):
            self.probe.fresh_admission.require_ready()
        with self.probe.db:
            self.probe.db.execute('UPDATE subscriptions SET verified_at=1000')
            self.probe.db.execute("UPDATE weixin_binding_epoch SET epoch='changed-binding'")
        with self.assertRaises(WeixinError):
            self.probe.fresh_admission.require_ready()

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

    def test_fresh_help_metadata_keeps_hint_without_sending_or_replay(self):
        self.ready()
        msg = self.message(item_list=[dict(type=1, text_item={'text': '/help'},
            create_time_ms=1000000, update_time_ms=1000000,
            is_completed=True, msg_id='synthetic-text-item')])
        mid = self.ingest(msg)
        source = self.probe.weixin.read_message(mid)
        self.assertTrue(source['inbound_completeness']['command_context_v1'])
        self.assertEqual(source['inbound_completeness']['command_context_reason_v1'], 'standalone_text_shape')
        self.assertEqual(source['fixed_command']['kind'], 'help')
        self.assertEqual(source['fixed_command']['prepared_text'], source['help_menu']['text'])
        self.assertFalse(source['fixed_command']['automatic_send'])
        self.assertTrue(source['fixed_command']['owner_intent_verification_required'])
        original = self.probe.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
        self.ingest(msg)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_fresh_admissions').fetchone()[0], 1)
        self.assertEqual(self.probe.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0], original)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM pings').fetchone()[0], 0)

    def test_fresh_closed_diagnostic_is_future_only_and_never_grants_hint(self):
        self.ready()
        msg = self.message(item_list=[dict(type=1, text_item={'text': '/help'},
            tool_call_start_item=None, is_completed=False)])
        mid = self.ingest(msg)
        source = self.probe.weixin.read_message(mid)
        capture = source['inbound_completeness']
        self.assertFalse(capture['command_context_v1'])
        self.assertEqual(capture['command_context_reason_v1'], 'item_unknown_fields')
        self.assertEqual(capture['command_item_diagnostic_v1']['fields'],
                         [{'code': 'official_tool_call_start_item', 'json_type': 'null'}])
        self.assertEqual(capture['command_item_diagnostic_v1']['metadata']['is_completed'], 'false')
        self.assertIsNone(source['help_menu'])
        self.assertIsNone(source['fixed_command'])
        original = self.probe.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
        msg['item_list'][0] = dict(type=1, text_item={'text': '/help'}, status='synthetic-private-value')
        self.ingest(msg)
        self.assertEqual(self.probe.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0], original)
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

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

    def stopped_trial(self, **changes):
        row = {'singleton':1,'round_id':'e'*32,'phase':'stopped','started_at':800.,
               'expires_at':920.,'binding_hash':'f'*64,'approval_hash':'a'*64,
               'isolation_hash':'b'*64,'source_message_id':None,'send_id':None,
               'error_kind':'poll_api_timeout','updated_at':920.}
        row.update(changes)
        with self.probe.db:
            self.probe.db.execute('''CREATE TABLE weixin_new_message_check (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), round_id TEXT NOT NULL,
                phase TEXT NOT NULL, started_at REAL NOT NULL, expires_at REAL NOT NULL,
                binding_hash TEXT NOT NULL, approval_hash TEXT NOT NULL, isolation_hash TEXT NOT NULL,
                source_message_id TEXT, send_id TEXT, error_kind TEXT NOT NULL,
                updated_at REAL NOT NULL)''')
            self.probe.db.execute('INSERT INTO weixin_new_message_check VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',tuple(row.values()))
        return tuple(self.probe.db.execute('SELECT * FROM weixin_new_message_check').fetchone())

    def test_closed_empty_trial_retained_through_fresh_activation_and_restart(self):
        before = self.stopped_trial()
        policy = self.activate()
        with self.assertRaisesRegex(WeixinError, 'fresh_original_handshake_required'):
            policy.require_ready()
        self.probe.subscribe(self.params)
        mid = self.ingest()
        self.assertTrue(policy.allows_source(mid))
        self.assertEqual(tuple(self.probe.db.execute('SELECT * FROM weixin_new_message_check').fetchone()),before)
        self.probe.close()
        self.probe = Probe(self.state,post=self.post,allowed_hosts=['callback.example.com'],clock=lambda:self.now)
        self.assertEqual(tuple(self.probe.db.execute('SELECT * FROM weixin_new_message_check').fetchone()),before)
        self.assertFalse(self.probe.fresh_admission.allows_source(mid))
        self.probe.subscribe(self.params)
        self.assertTrue(self.probe.fresh_admission.allows_source(mid))
        self.assertEqual(self.probe.fresh_admission.spec['cutoff_ms'],999000)

    def test_closed_trial_is_not_writer_authorization(self):
        before = self.stopped_trial()
        self.approval['isolation_mode']='no_existing_writer_verified'
        self.write_approval()
        with self.assertRaisesRegex(WeixinError,'fresh_writer_isolation_required'):
            self.activate()
        self.assertEqual(tuple(self.probe.db.execute('SELECT * FROM weixin_new_message_check').fetchone()),before)

    def test_trial_with_source_send_or_nonterminal_state_still_rejected(self):
        cases=({'phase':'waiting'}, {'phase':'delivery_unknown'}, {'phase':'rejected'},
               {'expires_at':1010.,'started_at':900.}, {'source_message_id':'synthetic-source'},
               {'send_id':'synthetic-send'}, {'source_message_id':''}, {'send_id':''})
        for changes in cases:
            with self.subTest(changes=changes):
                before=self.stopped_trial(**changes)
                with self.assertRaisesRegex(WeixinError,'fresh_diagnostic_state_not_promotable'):
                    self.activate()
                self.assertEqual(tuple(self.probe.db.execute('SELECT * FROM weixin_new_message_check').fetchone()),before)
                with self.probe.db:
                    self.probe.db.execute('DROP TABLE weixin_new_message_check')

    def test_empty_trial_does_not_allow_imported_messages_or_used_cursor(self):
        before=self.stopped_trial()
        self.ingest()
        with self.assertRaisesRegex(WeixinError,'fresh_new_state_required'):
            self.activate()
        self.assertEqual(tuple(self.probe.db.execute('SELECT * FROM weixin_new_message_check').fetchone()),before)

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
