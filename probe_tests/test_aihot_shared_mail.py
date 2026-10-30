"""Offline production mail claim/receipt path; no real accounts or network."""
import contextlib
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from probe.aihot_email import AihotEmailEvents, canonical
from probe_tests.aihot_fixture import OPERATOR
PRIMARY_LINK, RECIPIENT = OPERATOR.primary_link, OPERATOR.recipient
from probe.aihot_notice_consumer.relay import Relay, email_payload
from probe.aihot_notice_consumer.store_helpers import atomic_json, notification_text

class FixtureProbe:
    def __init__(self,path):
        self.lock=threading.RLock();self.db=sqlite3.connect(path);self.db.row_factory=sqlite3.Row;self.clock=lambda:120.0
        self.db.executescript("CREATE TABLE subscriptions(id TEXT,name TEXT,expires REAL); INSERT INTO subscriptions VALUES('sub','aihot.codex_reset',NULL); CREATE TABLE pings(id TEXT,name TEXT,status TEXT);")

class LocalEvents(AihotEmailEvents):
    @contextlib.contextmanager
    def _fenced(self,require_enabled=True):
        yield ((self._config(),{'owner_pin':'fixture-owner'}),None)

class SharedEmailPathTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.data=Path(self.temp.name)/'data';self.data.mkdir(mode=0o700)
        atomic_json(self.data/'control.json',dict(enabled=True,mode='monitor-only'))
        atomic_json(self.data/'dual-control.json',dict(capture_enabled=True))
        atomic_json(self.data/'state.json',dict(mode='monitor-only',dispatch_enabled=False,route_stopped=False))
        self.probe=FixtureProbe(Path(self.temp.name)/'test.sqlite');self.addCleanup(self.probe.db.close)
        self.event=LocalEvents(self.probe,self.data,operator_config=OPERATOR)
        self.event._resume_after=105  # Fixture event 110 arrives after process start.
        with self.probe.db:self.probe.db.execute("UPDATE aihot_email_config SET enabled=1,activation=100,owner_pin='fixture-owner',host_generation='fixture-generation',route_generation='fixture-route' WHERE id=1")
        self.relay=Relay(self.data,now_callable=lambda:120,operator_config=OPERATOR);self.relay.baseline([],100,checked_at=100)
        self.intent=dict(event_id='reset-1900000000000000001-1-1',stage='confirmed',source_at=110,expires_at=1000,link='https://x.com/thsottiaux/status/1900000000000000001',eligible=True)
        self.intent['text']=notification_text(self.intent['stage'],110,self.intent['link'])
        self.intent['display']=dict(version=1,style='wechat-email-rich-v2',event_id=self.intent['event_id'],stage='confirmed',source_at=110,link=self.intent['link'],reference_date='1970-01-01',original_text='Done <safely> & exactly.',translated_text='完成 & 好',source_precision=None,time_inferred=None,source_from=None,source_through=None,estimate_from=None,estimate_through=None,estimate_label=None,scope_known=False,scope_label=None,display_label=None,confirmation_basis='source_post',payload_timezone=None)
        self.relay.sync([self.intent],120,checked_at=120,healthy=True)
    def claim(self):return self.event.claim(self.intent['event_id'],self.intent['stage'],RECIPIENT,PRIMARY_LINK)
    def test_full_new_claim_authorize_and_receipt_are_immutable(self):
        first=self.claim();self.assertEqual(first['result'],'prepared');self.assertEqual(first['request']['subject'],'🔔 Codex 重置确认')
        self.assertIn('Done &lt;safely&gt; &amp; exactly.',first['request']['payload']['body']['content'])
        stored=json.loads(self.probe.db.execute('SELECT commitment FROM aihot_email_commitments').fetchone()[0])
        expected=email_payload(self.intent,OPERATOR);self.assertEqual(stored['payload_sha256'],hashlib.sha256(canonical(expected).encode()).hexdigest())
        self.assertEqual(self.claim()['result'],'reconcile_required')
        send=self.event.authorize(first['request_id']);self.assertTrue(send['send_permit'])
        self.assertEqual(self.event.authorize(first['request_id'])['result'],'reconcile_required')
        receipt=dict(message_id='123456abcdef',readback_id='123456abcdef',link_id=PRIMARY_LINK,from_address=RECIPIENT,to=RECIPIENT,subject=send['request']['subject'],body_html=send['request']['payload']['body']['content'],label_ids=['SENT'],cc='',bcc='')
        self.assertTrue(self.event.record(first['request_id'],receipt)['accepted'])
        self.assertEqual(self.claim()['result'],'reconcile_required')
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM aihot_email_commitments').fetchone()[0],1)
    def test_source_display_change_blocks_authorization(self):
        first=self.claim();state=self.relay.status();key=self.intent['event_id']+'/confirmed';state['snapshot'][key]['display']['original_text']='Changed'
        atomic_json(self.data/'dual-notifications.json',state)
        result=self.event.authorize(first['request_id']);self.assertFalse(result['send_permit']);self.assertEqual(result['result'],'handoff_blocked')
    def test_pre_restart_email_is_deferred_without_new_commitment(self):
        self.event._resume_after=115
        self.assertEqual(self.claim()['result'],'source_or_subscription_blocked')
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM aihot_email_commitments').fetchone()[0],0)

    def test_late_original_email_receipt_reconciles_after_restart(self):
        first=self.claim();send=self.event.authorize(first['request_id']);self.assertTrue(send['send_permit'])
        restarted=LocalEvents(self.probe,self.data,operator_config=OPERATOR)
        receipt=dict(message_id='123456abcdef',readback_id='123456abcdef',link_id=PRIMARY_LINK,from_address=RECIPIENT,to=RECIPIENT,subject=send['request']['subject'],body_html=send['request']['payload']['body']['content'],label_ids=['SENT'],cc='',bcc='')
        self.assertTrue(restarted.record(first['request_id'],receipt)['accepted'])
        self.assertEqual(restarted.claim(self.intent['event_id'],self.intent['stage'],RECIPIENT,PRIMARY_LINK)['result'],'reconcile_required')

    def test_unavailable_display_never_creates_commitment(self):
        state=self.relay.status();key=self.intent['event_id']+'/confirmed';sentinel={'version':1,'style':'rich-unavailable-v2'}
        state['snapshot'][key]['display']=sentinel;state['entries'][key]['intent']['display']=sentinel;atomic_json(self.data/'dual-notifications.json',state)
        self.assertEqual(self.claim()['result'],'presentation_unavailable')
        self.assertEqual(self.probe.db.execute('SELECT count(*) FROM aihot_email_commitments').fetchone()[0],0)

if __name__=='__main__':unittest.main()
