"""Rollback opens the SAME temporary DB with old Probe; never restores a DB.

All sends terminate in an in-process synthetic client. The old Probe is loaded
in a separate, network-disabled interpreter to avoid Python import-cache reuse.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from probe.probe import Probe, run_weixin_sender

BASELINE = Path(__file__).resolve().parent / 'fixtures/p0_receipt_baseline'
RESULTS = {}
ACTIVATION = '00000000-0000-0000-0000-000000000000'

OLD_PROBE_SCRIPT = r'''
import socket
def offline(*args, **kwargs): raise AssertionError('rollback child: network prohibited')
socket.getaddrinfo=offline
socket.create_connection=offline
socket.socket.connect=offline
socket.socket.connect_ex=offline
import json, sys
from pathlib import Path
baseline, state, expected = Path(sys.argv[1]).resolve(), sys.argv[2], set(json.loads(sys.argv[3]))
expected_version, expected_sha256 = sys.argv[4], sys.argv[5]
sys.path.insert(0,str(baseline))
from probe import probe as module
assert Path(module.__file__).resolve()==baseline/'probe'/'probe.py'
assert module.BUILD_VERSION==expected_version
import hashlib
assert hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()==expected_sha256
class FakeClient:
    def __init__(self): self.ids=[]
    def send(self, account, peer, text, client_id, context):
        assert client_id in expected, 'old Probe attempted a forbidden receipt'
        self.ids.append(client_id)
        return {'ret':0}
client=FakeClient()
p=module.Probe(state,clock=lambda:1800000000.,weixin_client=client)
try:
    p.weixin.recover_inflight()
    for _ in range(20):
        if not p.weixin.send_once(): break
    assert set(client.ids)==expected and len(client.ids)==len(expected)
    print(json.dumps({'version':module.BUILD_VERSION,'source':str(Path(module.__file__).resolve()),
                      'sent_client_ids':client.ids,'network_disabled':True,'worker_started':False}))
finally: p.close()
'''


class FakeClient:
    def __init__(self):
        self.sent = []
        self.accepted = threading.Event()

    def send(self, account, peer, text, client_id, context):
        self.sent.append((client_id,time.monotonic()))
        self.accepted.set()
        return {'ret':0}


class P0RollbackTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo','socket.create_connection','socket.socket.connect','socket.socket.connect_ex'):
            guard=patch(target,side_effect=AssertionError('rollback fixture: network prohibited'))
            guard.start(); self.addCleanup(guard.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.client=FakeClient()
        self.p=Probe(self.tmp.name,clock=lambda:1800000000.,weixin_client=self.client)
        self.addCleanup(lambda:self.p.close() if self.p else None)
        self.p.weixin.bind_confirmed({'status':'confirmed','ilink_bot_id':'fixture-bot',
                                     'ilink_user_id':'fixture-owner','bot_token':'synthetic-token'})

    def config(self, enabled=True, grace=0):
        file=self.directory/'owner-receipts.json'
        file.write_text(json.dumps({'version':1,'enabled':enabled,'activation_id':ACTIVATION,'grace_ms':grace}))
        file.chmod(0o600)
        return self.p.receipts.start_policy()

    def inbound(self, source):
        self.p.weixin.ingest(self.p.weixin.account(),{'msgs':[{
            'message_id':source,'message_type':1,'message_state':2,'from_user_id':'fixture-owner',
            'to_user_id':'fixture-bot','create_time_ms':self.p.clock()*1000,
            'context_token':'synthetic-context','item_list':[{'type':1,'text_item':{'text':'synthetic fixture'}}]
        }]})
        return self.p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?',(source,)).fetchone()[0]

    def snapshot(self, table):
        return [tuple(r) for r in self.p.db.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def test_old_probe_preserves_metadata_and_never_sends_disabled_or_unknown_receipts(self):
        model=self.inbound('old-model-pair')
        model_ack=self.p.weixin.queue_reply_phase(model,'ack','existing model ACK')['send_id']
        self.p.weixin.queue_reply(model,'existing model final')
        with self.p.db:
            self.p.db.execute("UPDATE weixin_outbox SET status='accepted',attempts=1,accepted_at=? WHERE source_message_id=?",(self.p.clock(),model))
            self.p.db.execute('''INSERT INTO weixin_dot_mirrors
                (id,message_id,event_id,attachment_id,phase,state,target_dot_message_id,attempt_id,receipt_source,created_at,updated_at)
                VALUES('dm_fixture',?,'evt_fixture','','original','committed',?,'synthetic-attempt','history_readback',?,?)''',
                (model,'Sentinel_'+'1'*32,self.p.clock(),self.p.clock()))
        original_final=self.inbound('old-final-still-queued')
        self.p.weixin.queue_reply(original_final,'original final remains sendable')
        self.config()
        system={}
        for source,status,attempts in (('queued','queued',0),('context','context_required',0),
                                       ('auth','auth_required',0),('inflight','sending',1)):
            mid=self.inbound('system-'+source)
            self.assertTrue(self.p.receipts.materialize_due())
            sid=self.p.weixin.ack_send_id(mid)
            with self.p.db:
                self.p.db.execute('UPDATE weixin_outbox SET status=?,attempts=? WHERE message_id=?',(status,attempts,sid))
            system[source]=sid
        quick=self.inbound('quick-final')
        self.assertTrue(self.p.receipts.materialize_due())
        self.p.weixin.queue_reply(quick,'quick final replaces unattempted receipt')
        system['superseded']=self.p.weixin.ack_send_id(quick)
        waiting=self.inbound('waiting-only')
        self.p.weixin.recover_inflight()
        self.assertEqual(self.p.weixin.send_status(system['inflight'])['status'],'delivery_unknown')
        self.config(enabled=False)
        self.assertEqual(self.p.receipts.for_message(waiting)['intent']['state'],'cancelled')
        for key in ('queued','context','auth'):
            status=self.p.weixin.send_status(system[key])
            self.assertEqual((status['status'],status['attempts']),('cancelled',0))
        self.assertEqual(self.p.weixin.send_status(system['superseded'])['status'],'superseded')
        preserved_tables=('owner_receipt_activations','owner_receipt_baseline','owner_receipt_intents','weixin_dot_mirrors','weixin_delivery_timing')
        snapshots={table:self.snapshot(table) for table in preserved_tables}
        protected=[dict(r) for r in self.p.db.execute('SELECT * FROM weixin_outbox WHERE message_id NOT IN (?,?)',(original_final,quick))]
        columns=[r['name'] for r in self.p.db.execute('PRAGMA table_info(weixin_outbox)')]
        expected=['dots_'+original_final[3:],'dots_'+quick[3:]]
        self.p_version=self.p.bridge_version
        self.p.close(); self.p=None
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH='')
        baseline_bytes=(BASELINE/'probe'/'probe.py').read_bytes()
        baseline_version=re.search(r"BUILD_VERSION = '([^']+)'",baseline_bytes.decode()).group(1)
        self.assertNotEqual(baseline_version,self.p_version, 'rollback fixture must be an earlier build')
        result=subprocess.run([sys.executable,'-c',OLD_PROBE_SCRIPT,str(BASELINE),self.tmp.name,json.dumps(expected),baseline_version,hashlib.sha256(baseline_bytes).hexdigest()],
                              cwd=self.tmp.name,env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        child=json.loads(result.stdout)
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            db.row_factory=sqlite3.Row
            for table in preserved_tables:
                self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM '+table+' ORDER BY rowid')],snapshots[table])
            self.assertEqual([dict(r) for r in db.execute('SELECT * FROM weixin_outbox WHERE message_id NOT IN (?,?)',(original_final,quick))],protected)
            self.assertEqual([r['name'] for r in db.execute('PRAGMA table_info(weixin_outbox)')],columns)
            for mid in (original_final,quick):
                self.assertEqual(db.execute('SELECT status FROM weixin_outbox WHERE message_id=?',(mid,)).fetchone()[0],'accepted')
        RESULTS['rollback']={'passed':True,'same_database':True,'database_restored':False,
                             'preserved_tables':list(preserved_tables),'old_version':child['version'],
                             'old_probe_source':child['source'],'fake_api_posts':len(child['sent_client_ids']),
                             'cancelled_superseded_unknown_posts':0,'network_disabled':True}

    def test_real_clock_event_sender_150ms_grace_only_fake_api(self):
        self.p.clock=self.p.weixin.clock=self.p.receipts.clock=time.time
        self.config(grace=150)
        stop=threading.Event()
        worker=threading.Thread(target=run_weixin_sender,args=(self.p,stop),daemon=True)
        worker.start()
        samples=[]
        try:
            for n in range(3):
                self.client.accepted.clear()
                self.inbound('timing-'+str(n))
                # ingest has returned: its DB transaction is committed.
                committed=time.monotonic()
                self.assertTrue(self.client.accepted.wait(timeout=2))
                acceptance=self.client.sent[-1][1]
                samples.append(acceptance-committed)
                self.assertGreaterEqual(samples[-1],.10)
                self.assertLess(samples[-1],1.5)
        finally:
            stop.set(); self.p.notify_sender(); worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.client.sent),3)
        RESULTS['timing']={'samples_seconds':samples,'grace_ms':150,
                           'basis':'real monotonic clock; ingest commit return to fake API entry',
                           'real_network':False,'phone_visibility_measured':False,
                           'model_or_platform_called':False,'sla_proven':False}


if __name__=='__main__':
    unittest.main()
