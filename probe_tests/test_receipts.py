"""Offline receipt policy/transaction fixtures; no real account, HTTP or worker."""
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from probe.receipts import OwnerReceiptStore, RECEIPT_TEXT
from probe.weixin import WeixinError, WeixinStore

ACTIVATION = '00000000-0000-0000-0000-000000000000'
NEXT = '11111111-1111-1111-1111-111111111111'


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo','socket.create_connection','socket.socket.connect','socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.now = 1800000000.
        db = sqlite3.connect(self.directory / 'fixture.sqlite')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript('''
            CREATE TABLE weixin_accounts(id TEXT PRIMARY KEY,owner TEXT,token TEXT);
            INSERT INTO weixin_accounts VALUES('fixture-bot','fixture-owner','TOKEN_CANARY');
            CREATE TABLE weixin_inbox(id TEXT PRIMARY KEY,account TEXT,peer TEXT,text TEXT,event_id TEXT);
            CREATE TABLE weixin_outbox(message_id TEXT PRIMARY KEY,account TEXT,peer TEXT,text TEXT,
                client_id TEXT UNIQUE,status TEXT,created_at REAL,kind TEXT,source_message_id TEXT,
                ack_origin TEXT DEFAULT 'model',attempts INTEGER DEFAULT 0,accepted_at REAL,error_kind TEXT);
            CREATE TABLE weixin_dot_mirrors(message_id TEXT,state TEXT,phase TEXT);
        ''')
        self.p = SimpleNamespace(directory=self.directory,db=db,lock=threading.RLock(),
                                 clock=lambda:self.now,weixin=SimpleNamespace(ack_send_id=WeixinStore.ack_send_id))
        self.store = OwnerReceiptStore(self.p)

    def config(self, enabled=True, activation=ACTIVATION, grace=0, **changes):
        value = {'version':1,'enabled':enabled,'activation_id':activation,'grace_ms':grace}
        value.update(changes)
        path = self.directory / 'owner-receipts.json'
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return path

    def enable(self, **changes):
        self.config(**changes)
        return self.store.start_policy()

    def inbound(self, mid, observe=True, owner='fixture-owner'):
        with self.p.db:
            changed = self.p.db.execute('INSERT OR IGNORE INTO weixin_inbox VALUES(?,?,?,?,NULL)',
                                       (mid,'fixture-bot',owner,'BODY_CANARY')).rowcount
            return bool(changed and observe and self.store.observe_new_in_transaction(mid))

    def outbox(self, source, phase='ack', origin='model', status='queued', attempts=0):
        sid = self.p.weixin.ack_send_id(source) if phase == 'ack' else source
        with self.p.db:
            self.p.db.execute('''INSERT INTO weixin_outbox
                (message_id,account,peer,text,client_id,status,created_at,kind,source_message_id,ack_origin,attempts)
                VALUES(?,'fixture-bot','fixture-owner','MODEL_CANARY',?,?,?,?,?,?,?)''',
                (sid,'dots_'+sid,status,self.now,'reply_ack' if phase=='ack' else 'reply',source,origin,attempts))
        return sid

    def test_constructor_and_missing_policy_are_disabled_without_old_backfill(self):
        self.assertFalse(self.store.status()['running'])
        self.inbound('old',observe=False)
        self.assertFalse(self.store.start_policy()['running'])
        self.assertFalse(self.store.materialize_due())
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM owner_receipt_intents').fetchone()[0],0)

    def test_private_policy_validation_strict_types_size_duplicates_and_symlink(self):
        for changes in ({'version':True},{'enabled':1},{'grace_ms':True},{'grace_ms':-1},
                        {'grace_ms':1001},{'activation_id':'not-uuid'},{'extra':True}):
            self.config(**changes)
            with self.subTest(changes=changes), self.assertRaisesRegex(WeixinError,'unsafe_receipt_policy'):
                self.store.start_policy()
        path=self.config()
        path.chmod(0o644)
        with self.assertRaisesRegex(WeixinError,'unsafe_receipt_policy'): self.store.start_policy()
        path=self.config()
        path.write_text(' ' * 4097)
        with self.assertRaisesRegex(WeixinError,'unsafe_receipt_policy'): self.store.start_policy()
        path=self.config()
        path.write_text('{"version":1,"version":1}')
        with self.assertRaisesRegex(WeixinError,'unsafe_receipt_policy'): self.store.start_policy()
        path.unlink()
        target=self.directory/'synthetic-target'
        target.write_text('{}'); target.chmod(0o600)
        path.symlink_to(target)
        with self.assertRaisesRegex(WeixinError,'unsafe_receipt_policy'): self.store.start_policy()
        self.assertFalse(self.store.running)

    def test_baseline_includes_known_old_ids_no_event_and_media_without_ack(self):
        old = ('wx_00000000000000000000000000000000','wx_11111111111111111111111111111111','old-no-event-media')
        for mid in old: self.inbound(mid,observe=False)
        self.inbound('foreign',observe=False,owner='foreign-owner')
        result=self.enable()
        self.assertEqual(result['activation']['baseline_count'],3)
        for mid in old:
            with self.p.db:
                self.p.db.execute('UPDATE weixin_inbox SET event_id=NULL WHERE id=?',(mid,))
                self.assertFalse(self.store.observe_new_in_transaction(mid))
            self.assertTrue(self.store.for_message(mid)['baseline'])
        self.assertFalse(self.store.materialize_due())
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0],0)

    def test_observe_joins_ingest_transaction_and_rollback_leaves_no_receipt(self):
        self.enable()
        with self.assertRaisesRegex(WeixinError,'requires_ingest_transaction'):
            self.store.observe_new_in_transaction('missing')
        self.p.db.execute('BEGIN')
        self.p.db.execute("INSERT INTO weixin_inbox VALUES('rolled-back','fixture-bot','fixture-owner','BODY_CANARY',NULL)")
        self.assertTrue(self.store.observe_new_in_transaction('rolled-back'))
        self.assertTrue(self.p.db.in_transaction)
        self.p.db.rollback()
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM owner_receipt_intents').fetchone()[0],0)

    def test_grace_due_and_shared_ack_identity_with_fixed_transport_wording(self):
        self.enable(grace=200)
        self.assertTrue(self.inbound('new'))
        self.assertFalse(self.inbound('new'))
        self.assertTrue(self.store.pending_receipt())
        self.assertAlmostEqual(self.store.next_wait(),.2,places=5)
        self.assertFalse(self.store.materialize_due())
        self.now += .2
        self.assertTrue(self.store.materialize_due())
        row=self.p.db.execute('SELECT * FROM weixin_outbox').fetchone()
        ack=self.p.weixin.ack_send_id('new')
        self.assertEqual((row['message_id'],row['client_id'],row['kind'],row['ack_origin'],row['text']),
                         (ack,'dots_'+ack,'reply_ack','system_receipt',RECEIPT_TEXT))
        self.assertFalse(self.store.materialize_due())
        self.assertFalse(self.store.pending_receipt())

    def test_restart_same_activation_preserves_boundary_and_pending_no_scan(self):
        self.inbound('old',observe=False)
        self.enable(grace=100)
        self.inbound('pending')
        self.inbound('unobserved-while-stopped',observe=False)
        before=self.store.status()['activation']
        second=OwnerReceiptStore(self.p)
        self.assertFalse(second.running)
        self.store=second
        self.assertEqual(self.store.start_policy()['activation'],before)
        self.assertIsNone(self.store.for_message('unobserved-while-stopped')['intent'])
        self.now += .1
        self.assertTrue(self.store.materialize_due())
        self.assertEqual(self.store.for_message('pending')['intent']['state'],'queued')

    def test_existing_final_any_status_suppresses_receipt_and_existing_ack_is_immutable(self):
        self.enable()
        for n,status in enumerate(('queued','accepted','rejected','delivery_unknown','cancelled')):
            mid='final-'+str(n); self.inbound(mid); self.outbox(mid,phase='final',status=status)
            self.assertTrue(self.store.materialize_due())
            self.assertEqual(self.store.for_message(mid)['intent']['state'],'suppressed')
        self.inbound('model-ack'); sid=self.outbox('model-ack')
        before=dict(self.p.db.execute('SELECT * FROM weixin_outbox WHERE message_id=?',(sid,)).fetchone())
        self.assertTrue(self.store.materialize_due())
        self.assertEqual(self.store.for_message('model-ack')['intent']['state'],'existing')
        self.assertEqual(dict(self.p.db.execute('SELECT * FROM weixin_outbox WHERE message_id=?',(sid,)).fetchone()),before)

    def test_unknown_and_prepared_ledger_defer_without_spin_or_new_ack(self):
        self.enable()
        self.inbound('ledger')
        with self.p.db: self.p.db.execute("INSERT INTO weixin_dot_mirrors VALUES('ledger','prepared','ack')")
        self.assertFalse(self.store.materialize_due())
        self.assertEqual(self.store.for_message('ledger')['intent']['decision'],'uncertain_source')
        self.assertGreater(self.store.next_wait(),0)
        self.assertFalse(self.store.pending_receipt())
        with self.p.db: self.p.db.execute('DELETE FROM weixin_dot_mirrors')
        self.now += .5
        self.assertTrue(self.store.materialize_due())
        self.inbound('unknown')
        self.outbox('unknown',phase='final',status='delivery_unknown')
        # A final commitment, even uncertain, suppresses rather than generates ACK.
        self.assertTrue(self.store.materialize_due())
        self.assertEqual(self.store.for_message('unknown')['intent']['state'],'suppressed')

    def test_queue_full_stays_waiting_and_does_not_deadlock_media(self):
        self.enable()
        self.inbound('waiting')
        for n in range(32): self.outbox('other-'+str(n),phase='final')
        self.assertFalse(self.store.materialize_due())
        self.assertEqual(self.store.for_message('waiting')['intent']['state'],'waiting')
        self.assertFalse(self.store.pending_receipt())
        self.assertGreater(self.store.next_wait(),0)
        with self.p.db: self.p.db.execute("UPDATE weixin_outbox SET status='accepted'")
        self.now += .5
        self.assertTrue(self.store.materialize_due())

    def test_original_mirror_uncertainty_does_not_block_transport_only_receipt(self):
        self.enable()
        for state in ('prepared','unknown'):
            mid='original-'+state
            self.inbound(mid)
            with self.p.db:
                self.p.db.execute('INSERT INTO weixin_dot_mirrors VALUES(?,?,?)',(mid,state,'original'))
            self.assertTrue(self.store.materialize_due())
            self.assertEqual(self.store.for_message(mid)['intent']['state'],'queued')
            self.assertFalse(self.store.for_message(mid)['dot_processing_confirmed'])
        for phase in ('ack','final'):
            for state in ('prepared','unknown'):
                mid=phase+'-'+state
                self.inbound(mid)
                with self.p.db:
                    self.p.db.execute('INSERT INTO weixin_dot_mirrors VALUES(?,?,?)',(mid,state,phase))
                self.assertFalse(self.store.materialize_due())
                self.assertEqual(self.store.for_message(mid)['intent']['decision'],'uncertain_source')

    def test_source_linked_unknown_outbox_blocks_without_final_slot(self):
        self.enable()
        self.inbound('uncertain-source')
        with self.p.db:
            self.p.db.execute("""INSERT INTO weixin_outbox
                (message_id,account,peer,text,client_id,status,created_at,kind,source_message_id)
                VALUES('notice-fixture','fixture-bot','fixture-owner','BODY_CANARY',
                    'dots-notice-fixture','delivery_unknown',?,'notification','uncertain-source')""",(self.now,))
        self.assertFalse(self.store.materialize_due())
        self.assertEqual(self.store.for_message('uncertain-source')['intent']['decision'],'uncertain_source')

    def test_disable_cancels_only_waiting_and_unattempted_queued_system_ack(self):
        self.enable()
        cases=(('unsent','queued',0),('context','context_required',0),('auth','auth_required',0),
               ('sending','sending',1),('unknown','delivery_unknown',1),('retry','queued',1))
        for mid,status,attempt in cases:
            self.inbound(mid); self.store.materialize_due()
            with self.p.db:
                self.p.db.execute('UPDATE weixin_outbox SET status=?,attempts=? WHERE source_message_id=?',(status,attempt,mid))
        self.inbound('waiting')
        self.inbound('model'); model=self.outbox('model')
        self.outbox('ordinary-final',phase='final')
        self.config(enabled=False)
        self.assertFalse(self.store.start_policy()['running'])
        states={r['source_message_id']:r['status'] for r in self.p.db.execute('SELECT source_message_id,status FROM weixin_outbox')}
        self.assertEqual(states['unsent'],'cancelled')
        self.assertEqual(states['context'],'cancelled')
        self.assertEqual(states['auth'],'cancelled')
        self.assertEqual(states['sending'],'sending')
        self.assertEqual(states['unknown'],'delivery_unknown')
        self.assertEqual(states['retry'],'queued')
        self.assertEqual(states['model'],'queued')
        self.assertEqual(states['ordinary-final'],'queued')
        self.assertEqual(self.store.for_message('waiting')['intent']['state'],'cancelled')

    def test_new_activation_requires_quiescence_and_owner_never_changes_silently(self):
        self.enable()
        self.inbound('waiting')
        self.config(activation=NEXT)
        with self.assertRaisesRegex(WeixinError,'not_quiescent'): self.store.start_policy()
        self.config(enabled=False); self.store.start_policy()
        self.config(activation=NEXT)
        self.assertEqual(self.store.start_policy()['activation']['id'],NEXT)
        self.assertTrue(self.store.for_message('waiting')['baseline'])
        self.config(activation=ACTIVATION)
        with self.assertRaisesRegex(WeixinError,'retired'): self.store.start_policy()
        self.config(activation=NEXT)
        with self.p.db: self.p.db.execute("UPDATE weixin_accounts SET owner='different-owner'")
        with self.assertRaisesRegex(WeixinError,'binding_changed'): self.store.start_policy()
        self.assertFalse(self.store.running)

    def test_other_owner_ignored_and_metadata_contains_no_body_token_or_owner(self):
        self.enable()
        self.assertFalse(self.inbound('foreign',owner='different-owner'))
        self.inbound('owned')
        safe=json.dumps(self.store.status())+json.dumps(self.store.for_message('owned'))
        for canary in ('BODY_CANARY','TOKEN_CANARY','fixture-owner','fixture-bot',RECEIPT_TEXT):
            self.assertNotIn(canary,safe)
        with self.assertRaisesRegex(WeixinError,'unknown_owner_message'):
            self.store.for_message('foreign')
        for limit in (0,-1,True,float('inf')):
            with self.assertRaises(ValueError): self.store.next_wait(limit)


if __name__ == '__main__':
    unittest.main()
