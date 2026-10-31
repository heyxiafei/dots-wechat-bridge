"""Synthetic local state only; these tests never start a process or call WeChat."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ActivationContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location('install_review', ROOT/'scripts/install-project.py')
        self.install = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.install)
        state = self.root/'runtime/state'
        state.mkdir(parents=True)
        self.db = sqlite3.connect(state/'probe.sqlite')
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE weixin_accounts (id TEXT, owner TEXT, status TEXT);
            INSERT INTO weixin_accounts VALUES ('synthetic-bot','synthetic-owner','ready');
            CREATE TABLE subscriptions (id TEXT, name TEXT, expires REAL, verified_at REAL);
            CREATE TABLE weixin_inbox (id TEXT, account TEXT, peer TEXT, source_id TEXT,
                text TEXT, created_at REAL, kind TEXT);
            CREATE TABLE weixin_outbox (message_id TEXT, account TEXT, peer TEXT, text TEXT,
                client_id TEXT, created_at REAL, kind TEXT, upload_id TEXT,
                source_message_id TEXT, ack_origin TEXT, status TEXT);
        ''')
        self.approval = {'isolation_mode':'old_writer_fenced', 'startup_mode':'foreground'}

    def history(self, table):
        if table == 'weixin_inbox':
            columns = 'id,account,peer,source_id,text,created_at,kind'
        else:
            columns = 'message_id,account,peer,text,client_id,created_at,kind,upload_id,source_message_id,ack_origin'
        rows = self.db.execute('SELECT rowid,'+columns+' FROM '+table+' ORDER BY rowid').fetchall()
        raw = ''.join(json.dumps(row, ensure_ascii=True, separators=(',', ':'))+'\n' for row in rows)
        checkpoint = {'through_rowid':rows[-1][0] if rows else 0, 'count':len(rows),
                      'sha256':hashlib.sha256(raw.encode()).hexdigest()}
        if table == 'weixin_outbox':
            checkpoint['submission_states'] = [list(row) for row in self.db.execute(
                'SELECT rowid,status FROM weixin_outbox ORDER BY rowid')]
        return checkpoint

    def retained(self):
        self.db.execute("INSERT INTO subscriptions VALUES ('original-sub','weixin.message',NULL,1)")
        self.db.execute("INSERT INTO weixin_inbox VALUES ('original-message','synthetic-bot','synthetic-owner','upstream-id','original',1,'text')")
        self.db.execute("INSERT INTO weixin_outbox VALUES ('original-message','synthetic-bot','synthetic-owner','answer','stable-client',1,'reply',NULL,'original-message',NULL,'queued')")
        self.db.commit()
        binding = json.dumps(['synthetic-bot','synthetic-owner'], separators=(',', ':')).encode()
        self.approval['handoff'] = {
            'continuity_mode':'retained_state',
            'owner_binding_sha256':hashlib.sha256(binding).hexdigest(),
            'subscription_id':'original-sub',
            'platform_route_evidence_reference':'trusted-route-receipt',
            'subscription_continuity_evidence_reference':'trusted-subscription-receipt',
            'history_retention_evidence_reference':'trusted-backup-receipt',
            'history':{table:self.history(table) for table in ('weixin_inbox','weixin_outbox')},
        }

    def start(self):
        with patch.object(self.install, 'activation_prerequisites', return_value=self.approval), \
             patch.object(self.install.os, 'execv') as execute:
            self.install.start(self.root)
            execute.assert_called_once()

    def test_bound_empty_database_cannot_start_existing_service_cutover(self):
        with self.assertRaisesRegex(ValueError, 'handoff_required'):
            self.start()

    def test_retained_original_ids_allow_normal_state_progress_and_new_messages(self):
        self.retained()
        self.start()
        self.db.execute("UPDATE weixin_outbox SET status='accepted'")
        self.db.execute("UPDATE subscriptions SET expires=9999999999")
        self.db.execute("INSERT INTO weixin_inbox VALUES ('new-message','synthetic-bot','synthetic-owner','new-upstream','new',2,'text')")
        self.db.commit()
        self.start()

    def test_lost_history_or_changed_commitment_blocks_start(self):
        self.retained()
        self.db.execute("UPDATE weixin_outbox SET text='rewritten answer'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'history_mismatch'):
            self.start()

    def test_wrong_owner_or_missing_original_subscription_blocks_start(self):
        self.retained()
        self.db.execute("UPDATE weixin_accounts SET owner='different-owner'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'owner_binding_mismatch'):
            self.start()
        self.db.execute("UPDATE weixin_accounts SET owner='synthetic-owner'")
        self.db.execute('DELETE FROM subscriptions')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'subscription_continuity_unconfirmed'):
            self.start()

    def test_new_source_cutpoint_is_explicitly_unsupported(self):
        self.retained()
        self.approval['handoff']['continuity_mode'] = 'new_source_cutpoint'
        with self.assertRaisesRegex(ValueError, 'cutpoint_mode_not_supported'):
            self.start()

    def test_expired_subscription_start_reports_pending_same_id_refresh(self):
        self.retained()
        self.db.execute('UPDATE subscriptions SET expires=1')
        self.db.commit()
        before = self.db.execute('SELECT * FROM subscriptions').fetchall()
        output = io.StringIO()
        with redirect_stdout(output):
            self.start()
        report = json.loads(output.getvalue())
        self.assertEqual(report['retained_subscription_state'], 'expired_requires_same_id_refresh')
        self.assertFalse(report['subscription_refresh_performed'])
        self.assertEqual(self.db.execute('SELECT * FROM subscriptions').fetchall(), before)

    def test_retired_history_is_preserved_but_active_competitor_blocks_restart(self):
        self.retained()
        self.db.execute('UPDATE subscriptions SET expires=1')
        self.db.execute("INSERT INTO subscriptions VALUES ('retired-history','weixin.message',1,1)")
        self.db.commit()
        before = self.db.execute('SELECT * FROM subscriptions').fetchall()
        output = io.StringIO()
        with redirect_stdout(output):
            self.start()
        self.assertEqual(json.loads(output.getvalue())['retained_subscription_state'], 'expired_requires_same_id_refresh')
        self.assertEqual(self.db.execute('SELECT * FROM subscriptions').fetchall(), before)
        self.db.execute("UPDATE subscriptions SET expires=NULL WHERE id='retired-history'")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'subscription_continuity_unconfirmed'):
            self.start()

    def test_revoked_or_duplicate_subscription_blocks_without_rewriting(self):
        self.retained()
        self.db.execute('UPDATE subscriptions SET expires=0')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'subscription_continuity_unconfirmed'):
            self.start()
        self.db.execute('UPDATE subscriptions SET expires=NULL')
        self.db.execute("INSERT INTO subscriptions VALUES ('second-sub','weixin.message',NULL,1)")
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'subscription_continuity_unconfirmed'):
            self.start()
        self.assertEqual(self.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 2)

    def test_missing_external_evidence_and_deleted_original_id_block(self):
        self.retained()
        self.approval['handoff']['platform_route_evidence_reference'] = ''
        with self.assertRaisesRegex(ValueError, 'evidence_reference_required'):
            self.start()
        self.approval['handoff']['platform_route_evidence_reference'] = 'real-route-reference'
        self.db.execute('DELETE FROM weixin_outbox')
        self.db.commit()
        with self.assertRaisesRegex(ValueError, 'history_mismatch'):
            self.start()

    def test_empty_replacement_cannot_self_attest_retained_history(self):
        self.retained()
        self.db.execute('DELETE FROM weixin_inbox')
        self.db.commit()
        self.approval['handoff']['history']['weixin_inbox'] = self.history('weixin_inbox')
        with self.assertRaisesRegex(ValueError, 'retained_inbox_history_required'):
            self.start()

    def test_verified_first_install_does_not_require_existing_history(self):
        self.approval['isolation_mode'] = 'no_existing_writer_verified'
        self.start()

    def test_unknown_or_accepted_cannot_be_reset_to_queued(self):
        self.retained()
        for status in ('delivery_unknown', 'accepted', 'sending'):
            with self.subTest(original_status=status):
                self.db.execute('UPDATE weixin_outbox SET status=?', (status,))
                self.db.commit()
                self.approval['handoff']['history']['weixin_outbox'] = self.history('weixin_outbox')
                self.db.execute("UPDATE weixin_outbox SET status='queued'")
                self.db.commit()
                with self.assertRaisesRegex(ValueError, 'submission_state_regression'):
                    self.start()

    def test_scan_success_and_missing_handoff_reports_bound_without_starting(self):
        with patch.object(self.install, 'check'), \
             patch.object(self.install, 'preflight', return_value={'local_blockers': [], 'binding': {'status': 'not_confirmed_locally'}}), \
             patch.object(self.install, 'activation_prerequisites', return_value=self.approval), \
             patch.object(self.install, 'run') as run, \
             patch.object(self.install, 'start') as start:
            result = self.install.connect(self.root)
        self.assertEqual(result['status'], 'bound_but_not_started')
        self.assertFalse(result['production_started'])
        run.assert_called_once()
        start.assert_not_called()

    def test_missing_binding_after_helper_success_never_claims_confirmed(self):
        self.db.execute('DELETE FROM weixin_accounts')
        self.db.commit()
        with patch.object(self.install, 'check'), \
             patch.object(self.install, 'preflight', return_value={'local_blockers': [], 'binding': {'status': 'not_confirmed_locally'}}), \
             patch.object(self.install, 'activation_prerequisites', return_value=self.approval), \
             patch.object(self.install, 'run'), patch.object(self.install, 'start') as start:
            result = self.install.connect(self.root)
        self.assertEqual(result['status'], 'binding_unconfirmed')
        self.assertFalse(result['production_started'])
        self.assertNotIn('binding confirmed', result['note'])
        start.assert_not_called()

    def test_changed_profile_after_scan_preserves_verified_binding_receipt(self):
        with patch.object(self.install, 'check'), \
             patch.object(self.install, 'preflight', return_value={'local_blockers': [], 'binding': {'status': 'not_confirmed_locally'}}), \
             patch.object(self.install, 'activation_prerequisites', side_effect=[self.approval, ValueError('approved_profile_mismatch')]), \
             patch.object(self.install, 'run'), patch.object(self.install, 'start') as start:
            result = self.install.connect(self.root)
        self.assertEqual(result['status'], 'bound_but_not_started')
        self.assertFalse(result['production_started'])
        self.assertEqual(result['activation_blocker'], 'approved_profile_mismatch')
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
