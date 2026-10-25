"""Actual-source compatibility using synthetic state and blocked networking.

The reviewed localcutover1 source is bundled as a test-only fixture. The test
stages only Python source files; it never copies dependencies, credentials or
runtime data.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


BASELINE_MODULES = (
    'weixin.py', 'login.py', 'dotledger.py', 'probe.py', 'outbound.py',
    'event_delivery.py', 'dotcutover.py', 'transport_timing.py',
    'dotjournal.py', 'dotepoch.py', 'receipts.py', 'media.py',
)
GUARDED_REPLACEMENTS = ('dotjournal.py', 'dotcutover.py')


# Separate interpreters prevent imports from one version contaminating another.
# Every stage blocks socket I/O before constructing Probe. FakeClient.send never
# forwards account data; it only counts synthetic calls and returns an empty API
# success object. No daemon/thread, subscriber or real upstream adapter is run.
STAGE_SCRIPT = r'''
import hashlib,json,socket,sqlite3,sys
def no_network(*args, **kwargs):
    raise AssertionError('network forbidden in compatibility tests')
for name in ('getaddrinfo','create_connection'):
    setattr(socket,name,no_network)
socket.socket.connect=no_network
socket.socket.connect_ex=no_network
from probe.probe import Probe,BUILD_VERSION
from probe.weixin import MESSAGE_EVENT,WeixinError
from probe.dotjournal import SOURCE
class FakeClient:
    def __init__(self): self.calls=0
    def send(self, account, peer, text, client_id, context):
        assert account['id']=='compat-bot' and peer=='compat-owner'
        self.calls+=1
        return {}
client=FakeClient()
p=Probe(sys.argv[1],clock=lambda:1800000000.,weixin_client=client)
stage=sys.argv[2]
scope='compat-synthetic-room'
def inbound(source):
    p.weixin.ingest(p.weixin.account(),{'get_updates_buf':'cursor-'+source,'msgs':[{
        'message_id':source,'message_type':1,'from_user_id':'compat-owner',
        'to_user_id':'compat-bot','context_token':'SYNTHETIC_CONTEXT',
        'item_list':[{'type':1,'text_item':{'text':'synthetic message'}}]}]})
    mid=p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?',(source,)).fetchone()[0]
    event='compat-event-'+source
    with p.db:
        p.db.execute("INSERT INTO pings(id,nonce,sub_id,body,status,name,message_id) VALUES(?,'','compat-sub','{}','delivered',?,?)",(event,MESSAGE_EVENT,mid))
        p.db.execute('UPDATE weixin_inbox SET event_id=? WHERE id=?',(event,mid))
    return mid,event
def existing(source):
    r=p.db.execute('SELECT id,event_id FROM weixin_inbox WHERE source_id=?',(source,)).fetchone()
    return r['id'],r['event_id']
def prepare(pair):
    return p.dot_journal.prepare(SOURCE,pair[0],pair[1],'original',scope)
def authorize(record):
    return p.dot_journal.authorize(record['journal_id'],record['attempt_id'],record['event_id'])
def commit(record, digit):
    target='Sentinel_'+digit*32
    evidence={'method':'user_message.send_message','target_scope':scope,
              'target_dot_message_id':target,'message_metadata':record['message_metadata']}
    return p.dot_journal.commit(record['journal_id'],record['attempt_id'],record['event_id'],target,'api_response',evidence)
def normal_reply(pair):
    reply=p.weixin.queue_reply(pair[0],'synthetic normal reply')
    assert reply['send_id']==pair[0]
    assert p.weixin.send_once()
    assert p.db.execute('SELECT status FROM weixin_outbox WHERE message_id=?',(pair[0],)).fetchone()[0]=='accepted'
    assert client.calls==1
def selected():
    cols={r['name'] for r in p.db.execute('PRAGMA table_info(weixin_dot_journal)')}
    projection=['id','message_id','event_id','attempt_id','state','authorization_id',
                'target_dot_message_id','receipt_source','receipt_evidence']
    projection += [k for k in ('authorization_history_version','authorization_ever_issued',
                               'preparation_recovery_required') if k in cols]
    return [dict(r) for r in p.db.execute('SELECT '+','.join(projection)+' FROM weixin_dot_journal ORDER BY id')]
def schema_tables():
    return {r[0] for r in p.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
if stage=='baseline':
    assert BUILD_VERSION.endswith('.mirrorjournal1.localcutover1'),BUILD_VERSION
    p.weixin.bind_confirmed({'status':'confirmed','ilink_bot_id':'compat-bot',
                            'ilink_user_id':'compat-owner','bot_token':'SYNTHETIC_TOKEN'})
    first=inbound('baseline-old')
    boundary=p.dot_journal.cutover.hold('compat-boundary')
    p.dot_journal.cutover.enable(boundary['boundary_id'],scope)
    r=prepare(inbound('baseline-committed'));assert authorize(r)['send_authorized'];commit(r,'1')
    r=prepare(inbound('baseline-unknown'));p.dot_journal.mark_unknown(r['journal_id'],r['attempt_id'])
    prepare(inbound('baseline-prepared'))
    r=prepare(inbound('baseline-inflight'));assert authorize(r)['send_authorized']
    normal_reply(first)
    assert 'weixin_inbound_manifests' not in schema_tables()
elif stage=='candidate':
    p.dot_journal.recover_inflight()
    rows=selected()
    by_source={p.db.execute('SELECT source_id FROM weixin_inbox WHERE id=?',(r['message_id'],)).fetchone()[0]:r for r in rows}
    assert by_source['baseline-committed']['state']=='committed'
    for s in ('baseline-unknown','baseline-prepared','baseline-inflight'):
        assert by_source[s]['state']=='unknown'
    assert all(r['authorization_history_version']==0 for r in rows)
    r=prepare(inbound('candidate-committed'));assert authorize(r)['send_authorized'];commit(r,'2')
    r=prepare(inbound('candidate-unknown'));p.dot_journal.mark_unknown(r['journal_id'],r['attempt_id'])
    prepare(inbound('candidate-prepared'))
    r=prepare(inbound('candidate-inflight'));assert authorize(r)['send_authorized']
    r=prepare(existing('candidate-committed'))
    assert r['authorization_history_known'] and r['authorization_ever_issued']
    s=p.dot_journal.cutover.status()
    closed=p.dot_journal.cutover.close(s['boundary_id'],'compat-close')
    assert closed['state']=='closed' and closed['admission_revision']==1
    assert not closed['previously_granted_permits_revoked']
    normal_reply(existing('candidate-committed'))
    assert p.weixin.inbound_completeness(existing('candidate-committed')[0])['recorded']
    assert p.weixin.inbound_completeness(existing('baseline-committed')[0])['state']=='legacy_unrecorded'
elif stage=='raw-baseline-guards':
    assert BUILD_VERSION.endswith('.mirrorjournal1.localcutover1'),BUILD_VERSION
    # The exact old APIs are denied while closed. Test their old UPDATE statements
    # as well, so this verifies database guards rather than only new Python code.
    boundary=p.dot_journal.cutover.status()['boundary_id']
    try:
        p.dot_journal.cutover.enable(boundary,scope)
    except sqlite3.IntegrityError as e:
        assert 'enable_revision_required' in str(e)
    except WeixinError as e:
        assert e.kind=='dot_cutover_authorized_intent_pending'
    else: raise AssertionError('old enable unexpectedly reopened admission')
    try:
        with p.db:
            p.db.execute("UPDATE weixin_dot_cutover SET state='new_only',target_scope=?,enabled_at=? WHERE singleton=1 AND state='closed'",(scope,1800000000.))
    except sqlite3.IntegrityError as e:
        assert 'enable_revision_required' in str(e)
    else: raise AssertionError('old enable SQL unexpectedly reopened admission')
    r=prepare(existing('candidate-prepared'))
    try: authorize(r)
    except WeixinError as e: assert e.kind=='dot_journal_fence_or_source_admission_pending'
    else: raise AssertionError('old authorize unexpectedly bypassed closed admission')
    try:
        with p.db:
            p.db.execute("UPDATE weixin_dot_journal SET state='authorized',authorization_id=?,updated_at=? WHERE id=? AND state='prepared' AND attempt_id=?",('f'*32,1800000000.,r['journal_id'],r['attempt_id']))
    except sqlite3.IntegrityError as e:
        assert 'authorization_history_required' in str(e)
    else: raise AssertionError('old authorization SQL unexpectedly issued a permit')
    assert p.dot_journal.cutover.status()['state']=='closed'
elif stage=='guarded-rollback':
    # Old business modules, current journal/cutover guards. No candidate import
    # is allowed to leak in through PYTHONPATH or sys.modules.
    assert BUILD_VERSION.endswith('.mirrorjournal1.localcutover1'),BUILD_VERSION
    assert not hasattr(p.weixin,'inbound_completeness')
    assert 'weixin_inbound_manifests' in schema_tables()
    p.dot_journal.recover_inflight()
    status=p.dot_journal.cutover.status()
    assert status['state']=='closed' and status['admission_revision']==1
    repeated=p.dot_journal.cutover.close(status['boundary_id'],'compat-close')
    assert repeated['close_request_replayed'] and repeated['admission_revision']==1
    names={t['name'] for t in p.tools()}
    assert 'close_weixin_dot_cutover_new_sources' in names
    rpc=p.rpc({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
        'name':'enable_weixin_dot_cutover_new_sources',
        'arguments':{'boundary_id':status['boundary_id'],'target_scope':scope}}})
    assert 'error' in rpc and rpc['error'].get('data',{}).get('reason')=='invalid_dot_cutover_arguments'
    normal_reply(inbound('rollback-inbound'))
    assert p.dot_journal.cutover.classify(existing('rollback-inbound')[0])['classification']=='reserved_new'
    assert p.db.execute('SELECT 1 FROM weixin_inbound_manifests WHERE message_id=?',existing('rollback-inbound')[:1]).fetchone() is None
    for source in ('baseline-unknown','baseline-prepared','baseline-inflight','candidate-unknown','candidate-inflight'):
        r=prepare(existing(source));assert r['state']=='unknown' and not authorize(r)['send_authorized']
elif stage=='reupgraded':
    p.dot_journal.recover_inflight()
    assert p.dot_journal.cutover.status()['state']=='closed'
    assert p.dot_journal.cutover.status()['admission_revision']==1
    old=p.weixin.inbound_completeness(existing('rollback-inbound')[0])
    assert old['recorded'] is False and old['state']=='legacy_unrecorded' and not old['complete']
    view=p.dot_completeness.for_message(existing('rollback-inbound')[0])
    assert view['all_parts_verified'] is False
    assert not view['all_expected_parts_readback_reported']
    assert p.weixin.inbound_completeness(existing('candidate-committed')[0])['recorded']
    assert p.db.execute('SELECT status FROM weixin_outbox WHERE message_id=?',existing('rollback-inbound')[:1]).fetchone()[0]=='accepted'
    normal_reply(inbound('after-reupgrade'))
    assert p.weixin.inbound_completeness(existing('after-reupgrade')[0])['recorded']
else: raise AssertionError('unknown test stage')
result={'stage':stage,'build_version':BUILD_VERSION,'cutover':p.dot_journal.cutover.status(),
        'journal':selected(),'sources':[dict(r) for r in p.db.execute('SELECT * FROM weixin_dot_cutover_sources ORDER BY message_id')],
        'tables':sorted(schema_tables()),'fake_send_calls':client.calls,
        'manifests':p.db.execute('SELECT count(*) FROM weixin_inbound_manifests').fetchone()[0] if 'weixin_inbound_manifests' in schema_tables() else None,
        'manifest_hashes':{r['message_id']:hashlib.sha256(r['metadata_json'].encode()).hexdigest() for r in p.db.execute('SELECT message_id,metadata_json FROM weixin_inbound_manifests')} if 'weixin_inbound_manifests' in schema_tables() else {}}
p.close()
print(json.dumps(result,sort_keys=True))
'''


class ActualBaselineCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.baseline = Path(__file__).resolve().parent / 'fixtures' / 'localcutover_baseline'
        cls.candidate = Path(__file__).resolve().parents[1]
        if cls.baseline == cls.candidate:
            raise AssertionError('compatibility baseline must be a distinct reviewed source directory')
        for name in BASELINE_MODULES:
            source = cls.baseline / 'probe' / name
            if not source.is_file() or source.is_symlink():
                raise AssertionError('missing or symlinked baseline source module: ' + name)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='weixin-compatibility-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'synthetic-state'
        self.guarded = self.root / 'guarded-source'
        (self.guarded / 'probe').mkdir(parents=True)
        for name in BASELINE_MODULES:
            source = self.candidate if name in GUARDED_REPLACEMENTS else self.baseline
            shutil.copyfile(source / 'probe' / name, self.guarded / 'probe' / name)
        # No venv, package cache or runtime data is in this source staging tree.
        self.assertEqual(sorted(p.name for p in (self.guarded / 'probe').iterdir()), sorted(BASELINE_MODULES))

    def run_stage(self, source, stage):
        env = dict(os.environ)
        env['PYTHONPATH'] = str(source)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        result = subprocess.run([sys.executable, '-B', '-c', STAGE_SCRIPT, str(self.state), stage],
                                cwd=source, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, stage + ': ' + result.stderr)
        return json.loads(result.stdout)

    def test_actual_baseline_upgrade_guarded_rollback_and_reupgrade(self):
        before = self.run_stage(self.baseline, 'baseline')
        upgraded = self.run_stage(self.candidate, 'candidate')
        rejected = self.run_stage(self.baseline, 'raw-baseline-guards')
        rollback = self.run_stage(self.guarded, 'guarded-rollback')
        final = self.run_stage(self.candidate, 'reupgraded')
        boundary = before['cutover']['boundary_id']
        for result in (upgraded, rejected, rollback, final):
            self.assertEqual(result['cutover']['boundary_id'], boundary)
            self.assertEqual(result['cutover']['snapshot_hash'], before['cutover']['snapshot_hash'])
            self.assertEqual(result['cutover']['state'], 'closed')
            self.assertFalse(result['cutover']['remote_legacy_writers_fenced'])
        self.assertEqual(upgraded['journal'], rejected['journal'])
        terminal = {r['id']: r for r in upgraded['journal'] if r['state'] in ('unknown', 'committed')}
        for result in (rollback, final):
            actual = {r['id']: r for r in result['journal']}
            for identity, row in terminal.items():
                self.assertEqual(actual[identity], row)
            self.assertTrue({'weixin_inbound_manifests', 'weixin_dot_cutover_closures'} <= set(result['tables']))
        # New rows added during rollback are explicitly unrecorded after upgrade,
        # and previously captured candidate manifests were retained byte-for-byte.
        self.assertEqual(upgraded['manifests'], rollback['manifests'])
        self.assertEqual(upgraded['manifest_hashes'], rollback['manifest_hashes'])
        self.assertTrue(upgraded['manifest_hashes'].items() <= final['manifest_hashes'].items())
        self.assertEqual(final['manifests'], upgraded['manifests'] + 1)
        for earlier, later in ((before, upgraded), (upgraded, rollback), (rollback, final)):
            old = {(r['boundary_id'], r['message_id']): r['classification'] for r in earlier['sources']}
            new = {(r['boundary_id'], r['message_id']): r['classification'] for r in later['sources']}
            self.assertTrue(old.items() <= new.items())
        self.assertEqual([before['fake_send_calls'], upgraded['fake_send_calls'],
                          rollback['fake_send_calls'], final['fake_send_calls']], [1, 1, 1, 1])
        # Verify the staged rollback recipe exactly, rather than relying on a
        # name that could accidentally point to the full new candidate.
        for name in BASELINE_MODULES:
            expected = self.candidate if name in GUARDED_REPLACEMENTS else self.baseline
            self.assertEqual(hashlib.sha256((self.guarded / 'probe' / name).read_bytes()).digest(),
                             hashlib.sha256((expected / 'probe' / name).read_bytes()).digest())


if __name__ == '__main__':
    unittest.main()
