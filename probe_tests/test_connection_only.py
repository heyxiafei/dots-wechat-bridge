"""Synthetic original-route handshake, with all business/network paths guarded."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from probe import connection_only as module
from probe.probe import Probe, canonical, EMAIL_EVENT


class ConnectionOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve()
        self.now = 1000.
        self.host = 'callback.example.com'
        self.params = {'name':'weixin.message','arguments':{},'ttlMs':86400000,
                       'delivery':{'mode':'webhook','url':'https://callback.example.com/original',
                                   'secret':'whsec_' + base64.b64encode(b'z'*32).decode()}}
        self.route = hashlib.sha256(canonical(['owner-only-tunnel',self.params['delivery']['url'],
                                               'weixin.message',{}]).encode()).hexdigest()
        self.sid = 'sub_' + self.route[:32]
        p = Probe(self.directory, allowed_hosts=[self.host], clock=lambda:self.now)
        p.weixin.bind_confirmed({'status':'confirmed','ilink_bot_id':'fixture-bot',
                                'ilink_user_id':'fixture-owner','bot_token':'synthetic-token-fixture'})
        self.tools = p.rpc({'jsonrpc':'2.0','id':1,'method':'tools/list'})['result']
        self.events = p.rpc({'jsonrpc':'2.0','id':1,'method':'events/list'})['result']
        self.discovery = {method:p.rpc({'jsonrpc':'2.0','id':1,'method':method})['result']
                          for method in ('initialize','server/discover')}
        p.close()
        self.calls = []
        self.connections = []
        self.addCleanup(lambda:[p.close() for p in self.connections])
        for target in ('socket.create_connection','socket.socket.connect','threading.Thread',
                       'probe.probe.Probe.__init__','probe.weixin.WeixinStore.__init__',
                       'probe.media.MediaStore.__init__','probe.dotjournal.DotMirrorJournal.__init__'):
            guard = patch(target, side_effect=AssertionError('business/network path forbidden'))
            guard.start()
            self.addCleanup(guard.stop)

    def post(self, url, body, headers):
        data = json.loads(body)
        self.assertEqual(set(data), {'type','challenge'})
        self.assertEqual(data['type'], 'verification')
        self.assertEqual(url, self.params['delivery']['url'])
        self.calls.append(data['challenge'])
        return 200, json.dumps({'challenge':data['challenge']}).encode()

    def connect(self):
        p = module.SubscriptionConnection(self.directory,[self.host],self.route,self.sid,
                                           post=self.post,clock=lambda:self.now)
        self.connections.append(p)
        return p

    def rpc(self, p, method, params=None):
        return p.rpc({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})

    def snapshot(self):
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='subscriptions'")]
            return {table:db.execute('SELECT * FROM '+table).fetchall() for table in tables}

    def test_catalog_and_discovery_are_exact_original_structures(self):
        p = self.connect()
        self.assertEqual(canonical(self.rpc(p,'tools/list')['result']),canonical(self.tools))
        self.assertEqual(canonical(self.rpc(p,'events/list')['result']),canonical(self.events))
        for method, expected in self.discovery.items():
            self.assertEqual(canonical(self.rpc(p,method)['result']), canonical(expected))
        self.assertEqual(self.calls, [])

    def test_only_original_live_challenge_updates_subscription(self):
        before = self.snapshot()
        p = self.connect()
        result = self.rpc(p,'events/subscribe',self.params)
        self.assertEqual(result['result']['id'], self.sid)
        self.assertEqual(len(self.calls), 1)
        row = p.db.execute('SELECT id,verified_at,expires FROM subscriptions').fetchone()
        self.assertEqual(tuple(row), (self.sid,self.now,self.now+86400))
        self.assertEqual(self.snapshot(), before)
        status = self.rpc(p,'tools/call',{'name':'get_weixin_status','arguments':{}})['result']['structuredContent']
        self.assertTrue(status['callback_challenge_verified_this_process'])
        self.assertFalse(status['connected'])
        self.assertFalse(status['message_sending_enabled'])

    def test_refresh_forces_new_challenge_and_keeps_one_original_id(self):
        p = self.connect()
        for _ in range(2):
            self.assertIn('result', self.rpc(p,'events/subscribe',self.params))
        self.assertEqual(len(set(self.calls)), 2)
        self.assertEqual(p.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 1)

    def test_wrong_route_event_and_noid_are_noops(self):
        p = self.connect()
        changed = json.loads(json.dumps(self.params))
        changed['delivery']['url'] += '-other'
        self.assertIn('error',self.rpc(p,'events/subscribe',changed))
        changed = dict(self.params,name='test.ping')
        self.assertIn('error',self.rpc(p,'events/subscribe',changed))
        self.assertIsNone(p.rpc({'jsonrpc':'2.0','method':'events/subscribe','params':self.params}))
        self.assertEqual(self.calls, [])
        self.assertEqual(p.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)

    def test_all_catalog_tools_except_two_status_are_blocked(self):
        before = self.snapshot()
        p = self.connect()
        for tool in self.tools['tools']:
            name = tool['name']
            if name in ('get_weixin_status','get_probe_status'):
                self.assertIn('result',self.rpc(p,'tools/call',{'name':name,'arguments':{}}))
            else:
                with self.subTest(name=name):
                    self.assertIn('error',self.rpc(p,'tools/call',{'name':name,'arguments':{}}))
        self.assertIn('error',self.rpc(p,'events/unsubscribe',self.params))
        self.assertEqual(self.calls, [])
        self.assertEqual(before, self.snapshot())

    def test_stateful_modes_and_business_history_refused_unchanged(self):
        for table in ('weixin_fresh_cutover','weixin_bounded_dot_check','weixin_new_message_check'):
            with sqlite3.connect(self.directory/'probe.sqlite') as db:
                db.execute('CREATE TABLE '+table+'(id INTEGER)')
            with self.subTest(table=table),self.assertRaises(ValueError):
                self.connect()
            with sqlite3.connect(self.directory/'probe.sqlite') as db:
                db.execute('DROP TABLE '+table)
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            db.execute("INSERT INTO aihot_auto_resume VALUES(1,'synthetic-policy')")
        with self.assertRaises(ValueError):
            self.connect()

    def stopped_trial(self, **changes):
        values = {'singleton':1,'round_id':'a'*32,'phase':'stopped','started_at':800.,
                  'expires_at':920.,'binding_hash':'b'*64,'approval_hash':'c'*64,
                  'isolation_hash':'d'*64,'source_message_id':None,'send_id':None,
                  'error_kind':'poll_api_timeout','updated_at':920.}
        values.update(changes)
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            db.execute('''CREATE TABLE weixin_new_message_check (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), round_id TEXT NOT NULL,
                phase TEXT NOT NULL, started_at REAL NOT NULL, expires_at REAL NOT NULL,
                binding_hash TEXT NOT NULL, approval_hash TEXT NOT NULL, isolation_hash TEXT NOT NULL,
                source_message_id TEXT, send_id TEXT, error_kind TEXT NOT NULL,
                updated_at REAL NOT NULL)''')
            db.execute('INSERT INTO weixin_new_message_check VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',tuple(values.values()))

    def test_empty_expired_stopped_trial_allows_handshake_without_row_changes(self):
        self.stopped_trial()
        before=self.snapshot()
        p=self.connect()
        self.assertIn('result',self.rpc(p,'events/subscribe',self.params))
        self.assertEqual(len(self.calls),1)
        self.assertEqual(self.snapshot(),before)
        for sql in ('UPDATE weixin_new_message_check SET phase=phase',
                    'DELETE FROM weixin_new_message_check'):
            with self.assertRaises(sqlite3.DatabaseError):
                p.db.execute(sql)
        self.assertEqual(self.snapshot(),before)

    def test_trial_active_unexpired_admitted_or_sent_refused_unchanged(self):
        cases=({'phase':'waiting'}, {'phase':'accepted'}, {'expires_at':1001.,'started_at':900.},
               {'source_message_id':'synthetic-source'}, {'send_id':'synthetic-send'},
               {'source_message_id':''}, {'send_id':''}, {'started_at':0.},
               {'expires_at':800.}, {'started_at':700.}, {'updated_at':799.},
               {'updated_at':1001.}, {'expires_at':float('inf')})
        for changes in cases:
            with self.subTest(changes=changes):
                self.stopped_trial(**changes)
                before=self.snapshot()
                with self.assertRaises(ValueError):
                    self.connect()
                self.assertEqual(self.snapshot(),before)
                self.assertEqual(self.calls,[])
                with sqlite3.connect(self.directory/'probe.sqlite') as db:
                    db.execute('DROP TABLE weixin_new_message_check')

    def test_trial_extra_column_or_empty_rows_refused(self):
        self.stopped_trial()
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            db.execute('ALTER TABLE weixin_new_message_check ADD COLUMN extra TEXT')
        with self.assertRaises(ValueError):
            self.connect()

    def test_trial_multiple_rows_refused_even_if_schema_check_was_bypassed_elsewhere(self):
        self.stopped_trial()
        # This deliberately malformed synthetic DB tests a read-side gate;
        # no production code changes a constraint or repairs such a database.
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            db.execute('PRAGMA ignore_check_constraints=ON')
            db.execute('''INSERT INTO weixin_new_message_check
                SELECT 2,round_id,phase,started_at,expires_at,binding_hash,
                approval_hash,isolation_hash,source_message_id,send_id,error_kind,updated_at
                FROM weixin_new_message_check''')
        before=self.snapshot()
        with self.assertRaises(ValueError):
            self.connect()
        self.assertEqual(self.snapshot(),before)
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            db.execute('DROP TABLE weixin_new_message_check')
        self.stopped_trial()
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            db.execute('DELETE FROM weixin_new_message_check')
        with self.assertRaises(ValueError):
            self.connect()

    def test_existing_original_expired_subscription_is_preserved_until_live_request(self):
        p = self.connect()
        self.rpc(p,'events/subscribe',self.params)
        p.db.execute('UPDATE subscriptions SET expires=1')
        p.db.commit()
        before = p.db.execute('SELECT revision FROM subscriptions').fetchone()[0]
        second = self.connect()
        self.assertFalse(second.status()['subscription_active'])
        self.assertEqual(second.db.execute('SELECT revision FROM subscriptions').fetchone()[0], before)
        self.assertIn('result',self.rpc(second,'events/subscribe',self.params))
        self.assertTrue(second.status()['subscription_active'])

    def test_callback_failure_does_not_create_subscription(self):
        p = self.connect()
        p.post = lambda *args: (403,b'{}')
        self.assertIn('error',self.rpc(p,'events/subscribe',self.params))
        self.assertFalse(p.challenge_verified)
        self.assertEqual(p.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)

    def test_sql_fence_denies_business_writes_and_account_token_reads(self):
        p = self.connect()
        for sql in ('SELECT token FROM weixin_accounts','DELETE FROM weixin_inbox',
                    'UPDATE weixin_accounts SET cursor=cursor','CREATE TABLE bypass(id INTEGER)'):
            with self.subTest(sql=sql),self.assertRaises(sqlite3.DatabaseError):
                p.db.execute(sql)

    def test_subscription_trigger_is_rejected_without_execution(self):
        with sqlite3.connect(self.directory/'probe.sqlite') as db:
            db.execute('CREATE TRIGGER unsafe_sub AFTER INSERT ON subscriptions BEGIN DELETE FROM weixin_accounts; END')
        with self.assertRaises(ValueError):
            self.connect()
        self.assertEqual(self.snapshot()['weixin_accounts'][0][0], 'fixture-bot')

    def test_stdio_eof_releases_lock_and_does_not_construct_business_runtime(self):
        readfd, writefd = os.pipe()
        os.write(writefd,b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
        os.close(writefd)
        out, err = io.StringIO(), io.StringIO()
        try:
            self.assertEqual(module.serve(self.directory,[self.host],self.route,self.sid,
                                          duration_seconds=1,stdin_fd=readfd,stdout=out,stderr=err),0)
        finally:
            os.close(readfd)
        self.assertEqual(json.loads(out.getvalue())['result'], self.tools)
        self.assertNotIn(self.params['delivery']['url'],err.getvalue())
        self.assertNotIn(self.params['delivery']['secret'],err.getvalue())
        lock=module.acquire_worker_lock(self.directory)
        lock.close()

    def test_explicit_renderer_and_production_installer_reject_connection_profile(self):
        root = Path(__file__).resolve().parents[1]
        def load(name, filename):
            spec=importlib.util.spec_from_file_location(name,root/'scripts'/filename)
            value=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(value)
            return value
        render=load('connection_config_fixture','render-config.py')
        install=load('connection_install_fixture','install-project.py')
        config=self.directory/'config'
        config.mkdir()
        for name in ('tunnel-profile.yaml.example','dots-wechat-bridge.service.example'):
            (config/name).write_bytes((root/'config'/name).read_bytes())
        runtime=render.render(self.directory,'tunnel_'+'0'*32,[self.host],
            connection_only=True,connection_route_digest=self.route,connection_subscription_id=self.sid)
        profile=(runtime/'profile.yaml').read_text()
        self.assertIn('--connection-only',profile)
        self.assertNotIn('--fresh-production',profile)
        self.assertFalse((runtime/'dots-wechat-bridge.service').exists())
        with self.assertRaisesRegex(ValueError,'connection_only_profile_not_production'):
            install.require_state_continuity(self.directory,{'isolation_mode':'no_existing_writer_verified'})
        with self.assertRaises(ValueError):
            render.render(self.directory,'tunnel_'+'0'*32,[self.host],replace=True,
                fresh_production=True,connection_only=True,
                connection_route_digest=self.route,connection_subscription_id=self.sid)
        self.assertEqual((runtime/'profile.yaml').read_text(),profile)

    def test_bounded_stdio_timeout_without_any_request(self):
        readfd,writefd=os.pipe()
        try:
            self.assertEqual(module.serve(self.directory,[self.host],self.route,self.sid,
                duration_seconds=1,stdin_fd=readfd,stdout=io.StringIO(),stderr=io.StringIO()),0)
        finally:
            os.close(writefd)
            os.close(readfd)
        lock=module.acquire_worker_lock(self.directory)
        lock.close()


if __name__ == '__main__':
    unittest.main()
