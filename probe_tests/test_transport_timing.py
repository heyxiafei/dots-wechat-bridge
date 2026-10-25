"""Synthetic host-observation metadata; no HTTP, worker or real account data."""
import json
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from probe.transport_timing import DeliveryTimingStore, TIME_FIELDS
from probe.weixin import WeixinError


class DeliveryTimingTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo','socket.create_connection','socket.socket.connect','socket.socket.connect_ex'):
            guard=patch(target,side_effect=AssertionError('timing fixture: network forbidden'))
            guard.start(); self.addCleanup(guard.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db=sqlite3.connect(self.tmp.name+'/synthetic.sqlite')
        self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE weixin_accounts(id TEXT PRIMARY KEY,owner TEXT,token TEXT);
            INSERT INTO weixin_accounts VALUES('bot-fixture','owner-fixture','TOKEN_CANARY');
            CREATE TABLE weixin_inbox(id TEXT PRIMARY KEY,account TEXT,peer TEXT,text TEXT,created_at REAL);
            INSERT INTO weixin_inbox VALUES('owned','bot-fixture','owner-fixture','BODY_CANARY',1234);
            INSERT INTO weixin_inbox VALUES('historical','bot-fixture','owner-fixture','HISTORY_BODY_CANARY',4321);
            INSERT INTO weixin_inbox VALUES('foreign','bot-fixture','another-owner','FOREIGN_BODY_CANARY',5678);
            CREATE TABLE weixin_outbox(message_id TEXT PRIMARY KEY,account TEXT,peer TEXT,kind TEXT,
                created_at REAL,post_started_at REAL,accepted_at REAL,status TEXT,text TEXT);
        ''')
        self.p=SimpleNamespace(db=self.db,lock=threading.RLock())
        self.store=DeliveryTimingStore(self.p)

    def mark(self, method, mid='owned', now=10.):
        with self.db:
            self.db.execute('BEGIN')
            return getattr(self.store,method)(mid,now)

    def row(self, mid='owned'):
        row=self.db.execute('SELECT * FROM weixin_delivery_timing WHERE message_id=?',(mid,)).fetchone()
        return dict(row) if row else None

    def test_schema_and_unknown_historical_fields_are_nullable(self):
        columns={r['name']:r for r in self.db.execute('PRAGMA table_info(weixin_delivery_timing)')}
        self.assertEqual(set(columns),{'message_id','agent_read_count',*TIME_FIELDS})
        for field in TIME_FIELDS: self.assertFalse(columns[field]['notnull'])
        status=self.store.for_message('owned')
        self.assertTrue(all(value is None for value in status['timestamps'].values()))
        self.assertTrue(all(value is None for value in status['durations_seconds'].values()))
        self.assertEqual(status['agent_read_count'],0)
        self.assertIsNone(self.row())

    def test_all_embedded_hooks_require_transaction_and_never_commit(self):
        for method in ('ingested','queued','attempt','accepted'):
            with self.subTest(method=method):
                with self.assertRaisesRegex(WeixinError,'requires_transaction'):
                    getattr(self.store,method)('owned',10.)
                with self.assertRaisesRegex(RuntimeError,'rollback fixture'):
                    with self.db:
                        self.db.execute('BEGIN')
                        getattr(self.store,method)('owned',10.)
                        self.assertTrue(self.db.in_transaction)
                        raise RuntimeError('rollback fixture')
                self.assertIsNone(self.row())

    def test_failed_ingest_rolls_back_message_and_timing_together(self):
        with self.assertRaises(RuntimeError):
            with self.db:
                self.db.execute("INSERT INTO weixin_inbox VALUES('new','bot-fixture','owner-fixture','BODY_CANARY',9999)")
                self.store.ingested('new',10.)
                raise RuntimeError('synthetic ingest failure')
        self.assertIsNone(self.row('new'))
        self.assertIsNone(self.db.execute("SELECT id FROM weixin_inbox WHERE id='new'").fetchone())

    def test_first_observations_stay_fixed_last_attempt_tracks_retry(self):
        for method in ('ingested','queued','attempt','accepted'):
            self.mark(method,now=10.)
            self.mark(method,now=20.)
        row=self.row()
        for field in ('ingest_observed_at','event_queued_at','first_webhook_attempt_at','webhook_accepted_at'):
            self.assertEqual(row[field],10.)
        self.assertEqual(row['last_webhook_attempt_at'],20.)
        self.assertIsNone(row['ingest_commit_observed_at'])

    def test_committed_only_updates_registered_ingest_and_does_not_create_history(self):
        self.mark('ingested')
        self.mark('queued',mid='historical')
        self.assertTrue(self.store.committed(['owned','owned','historical','foreign','missing'],11.))
        self.assertFalse(self.store.committed(['owned'],12.))
        self.assertEqual(self.row()['ingest_commit_observed_at'],11.)
        self.assertIsNone(self.row('historical')['ingest_commit_observed_at'])
        self.assertIsNone(self.row('historical')['ingest_observed_at'])
        self.assertIsNone(self.row('foreign'))

    def test_committed_refuses_outer_transaction_without_committing_it(self):
        self.mark('ingested')
        self.db.execute('BEGIN')
        self.db.execute("UPDATE weixin_inbox SET text='UNCOMMITTED_CANARY' WHERE id='owned'")
        self.assertFalse(self.store.committed(['owned'],11.))
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertIsNone(self.row()['ingest_commit_observed_at'])
        self.assertEqual(self.db.execute("SELECT text FROM weixin_inbox WHERE id='owned'").fetchone()[0],'BODY_CANARY')

    def test_committed_failure_is_soft_atomic_and_keeps_successful_ingest(self):
        self.mark('ingested')
        self.mark('ingested',mid='historical')
        underlying=self.db
        class FailSecondCommitUpdate:
            calls=0
            def __getattr__(self,name): return getattr(underlying,name)
            def __enter__(self): underlying.__enter__(); return self
            def __exit__(self,*args): return underlying.__exit__(*args)
            def execute(self,sql,args=()):
                if 'SET ingest_commit_observed_at=' in sql:
                    self.calls+=1
                    if self.calls==2: raise sqlite3.OperationalError('SECRET_ERROR_CANARY')
                return underlying.execute(sql,args)
        self.store.db=FailSecondCommitUpdate()
        self.assertFalse(self.store.committed(['owned','historical'],11.))
        self.store.db=underlying
        for mid in ('owned','historical'):
            self.assertEqual(self.row(mid)['ingest_observed_at'],10.)
            self.assertIsNone(self.row(mid)['ingest_commit_observed_at'])
            self.assertIsNotNone(self.db.execute('SELECT id FROM weixin_inbox WHERE id=?',(mid,)).fetchone())

    def test_committed_with_closed_db_or_invalid_input_never_raises(self):
        for mids,now in ((True,10.),(['owned'],True),(['owned'],float('nan')),(['owned',None],10.)):
            self.assertFalse(self.store.committed(mids,now))
        self.db.close()
        self.assertFalse(self.store.committed(['owned'],10.))

    def test_owner_scope_and_ambiguous_binding_are_rejected(self):
        for mid in ('foreign','missing'):
            with self.subTest(mid=mid):
                with self.assertRaisesRegex(WeixinError,'unknown_owner_message'): self.store.owned(mid)
                with self.assertRaisesRegex(WeixinError,'unknown_owner_message'): self.store.for_message(mid)
                with self.assertRaisesRegex(WeixinError,'unknown_owner_message'): self.store.agent_read(mid,10.)
                for method in ('ingested','queued','attempt','accepted'):
                    with self.assertRaisesRegex(WeixinError,'unknown_owner_message'): self.mark(method,mid)
        with self.db:
            self.db.execute("INSERT INTO weixin_accounts VALUES('other-bot','other-owner','OTHER_TOKEN_CANARY')")
        with self.assertRaisesRegex(WeixinError,'owner_unavailable'): self.store.owned('owned')

    def test_historical_agent_read_counts_only_explicit_hook_without_backfill(self):
        self.store.owned('historical')
        self.store.for_message('historical')
        self.assertIsNone(self.row('historical'))
        self.assertTrue(self.store.agent_read('historical',20.))
        self.assertTrue(self.store.agent_read('historical',25.))
        result=self.store.for_message('historical')
        self.assertEqual(result['agent_read_count'],2)
        self.assertEqual(result['timestamps']['first_agent_read_at'],20.)
        self.assertEqual(result['timestamps']['last_agent_read_at'],25.)
        for field in TIME_FIELDS:
            if 'agent_read' not in field: self.assertIsNone(result['timestamps'][field])
        self.assertFalse(result['dot_processing_confirmed'])
        self.assertFalse(result['webhook_accepted_is_model_start'])
        self.assertFalse(result['phone_delivery_observed'])

    def test_agent_read_refuses_outer_transaction_and_rolls_back_failed_write(self):
        self.db.execute('BEGIN')
        self.assertFalse(self.store.agent_read('owned',10.))
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertIsNone(self.row())
        with patch.object(self.store,'owned',side_effect=[{'id':'bot-fixture','owner':'owner-fixture'},sqlite3.OperationalError('PRIVATE_CANARY')]):
            self.assertFalse(self.store.agent_read('owned',10.))
        self.assertIsNone(self.row())

    def test_only_exact_owner_final_slot_contributes_existing_outbox_times(self):
        with self.db:
            self.db.execute("INSERT INTO weixin_outbox VALUES('ack-owned','bot-fixture','owner-fixture','reply_ack',30,31,32,'accepted','ACK_BODY_CANARY')")
            self.db.execute("INSERT INTO weixin_outbox VALUES('owned','bot-fixture','owner-fixture','notification',30,31,32,'accepted','NOTICE_BODY_CANARY')")
        self.assertIsNone(self.store.for_message('owned')['timestamps']['final_queued_at'])
        with self.db:
            self.db.execute("UPDATE weixin_outbox SET kind='reply' WHERE message_id='owned'")
        result=self.store.for_message('owned')
        self.assertEqual(result['timestamps']['final_queued_at'],30.)
        self.assertEqual(result['durations_seconds']['final_queued_to_api_accepted'],2.)
        self.assertIsNone(result['durations_seconds']['commit_observed_to_final_api_accepted'])
        self.assertIsNone(result['timestamps']['ingest_observed_at'])
        with self.db:
            self.db.execute("UPDATE weixin_outbox SET peer='another-owner' WHERE message_id='owned'")
        self.assertIsNone(self.store.for_message('owned')['timestamps']['final_queued_at'])

    def test_known_durations_missing_values_and_overlapping_stages_are_explicit(self):
        self.mark('ingested',now=10.)
        self.store.committed(['owned'],11.)
        self.mark('queued',now=10.5)  # A post-commit observer may run after the pump.
        self.mark('attempt',now=12.)
        self.mark('attempt',now=15.)
        self.mark('accepted',now=16.)
        self.store.agent_read('owned',14.)  # Read can race the callback's response.
        result=self.store.for_message('owned')
        duration=result['durations_seconds']
        self.assertEqual(duration['ingest_to_commit_observed'],1.)
        self.assertEqual(duration['first_webhook_attempt_to_accepted'],4.)
        self.assertEqual(duration['last_webhook_attempt_to_accepted'],1.)
        self.assertIsNone(duration['commit_observed_to_event_queued'])
        self.assertIsNone(duration['webhook_accepted_to_first_agent_read'])
        self.assertIn('commit_observed_to_event_queued',result['unordered_or_overlapping_stages'])
        self.assertIn('webhook_accepted_to_first_agent_read',result['unordered_or_overlapping_stages'])
        self.assertFalse(result['source_clock_comparable'])

    def test_secret_canaries_and_source_timestamps_never_selected_or_returned(self):
        self.mark('ingested')
        self.store.agent_read('owned',12.)
        traces=[]
        self.db.set_trace_callback(traces.append)
        safe=json.dumps(self.store.for_message('owned'))
        for canary in ('TOKEN_CANARY','BODY_CANARY','owner-fixture','bot-fixture','1234','4321'):
            self.assertNotIn(canary,safe)
        for sql in traces:
            lower=sql.lower()
            self.assertNotIn('token',lower)
            self.assertNotIn('select text',lower)
            self.assertNotIn('select * from weixin_accounts',lower)
            self.assertNotIn('select * from weixin_inbox',lower)
        self.db.set_trace_callback(None)

    def test_restart_retains_first_observations_and_invalid_time_is_not_saved(self):
        self.mark('ingested',now=10.)
        self.store.agent_read('owned',12.)
        second=DeliveryTimingStore(self.p)
        self.assertEqual(second.for_message('owned')['timestamps']['ingest_observed_at'],10.)
        self.assertTrue(second.agent_read('owned',13.))
        self.assertEqual(second.for_message('owned')['agent_read_count'],2)
        for value in (True,None,'now',float('inf'),float('nan')):
            with self.assertRaisesRegex(WeixinError,'invalid_transport_timing_timestamp'):
                self.mark('attempt',now=value)
        self.assertIsNone(self.row()['first_webhook_attempt_at'])


if __name__=='__main__':
    unittest.main()
