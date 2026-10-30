"""Synthetic restart policy tests: private temp DB, no network, no real sends."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from probe.probe import Probe
from probe.aihot_email import AihotEmailEvents
from probe.aihot_notice_consumer.bridge_hook import BridgeHook
from probe.aihot_notice_consumer import auto_resume
from probe_tests.aihot_fixture import OPERATOR, APPROVAL
ORIGINAL_READ_HARD_STOP = auto_resume.read_hard_stop
from probe.aihot_notice_consumer.relay import Relay
from probe.aihot_notice_consumer.store_helpers import atomic_json,notification_text

T=1000.
class NoNetwork:
    def __getattr__(self,name):
        def denied(*args,**kwargs):raise AssertionError('no network or sending allowed')
        return denied

class AutoResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.data=self.root/'data';self.data.mkdir(mode=0o700)
        for n in ('monitor.lock','dual-notifications.lock'):(self.data/n).touch(mode=0o600)
        atomic_json(self.data/'control.json',dict(enabled=True,mode='monitor-only'))
        atomic_json(self.data/'dual-control.json',dict(capture_enabled=True))
        atomic_json(self.data/'state.json',dict(mode='monitor-only',dispatch_enabled=False,route_stopped=False))
        self.wall=[T];self.mono=[100.]
        self.p=Probe(self.root/'state',clock=lambda:self.wall[0],weixin_client=NoNetwork());self.addCleanup(self.p.close)
        self.p.weixin.bind_confirmed(dict(status='confirmed',ilink_bot_id='fixture-bot',ilink_user_id='fixture-owner',bot_token='fixture-token'))
        self.email=AihotEmailEvents(self.p,self.data,operator_config=OPERATOR)
        proof={'message_id':'f'*16,'readback_id':'f'*16,'link_id':OPERATOR.primary_link,
               'from_address':OPERATOR.recipient,'to':OPERATOR.recipient,'subject':OPERATOR.sender_test_subject,
               'test_marker':OPERATOR.sender_test_marker,'sent_at':T-10,'label_ids':['SENT','INBOX'],'body_verified':True}
        with self.p.db:
            self.p.db.execute("INSERT INTO weixin_inbox(id,account,peer,source_id,text,created_at,kind,unsupported) VALUES(?,?,?,?,?,?,?,?)",(APPROVAL,'fixture-bot','fixture-owner','fixture-approval','approved',T,'text','[]'))
            self.p.db.execute("UPDATE aihot_email_config SET enabled=1,activation=?,owner_pin=?,host_generation=?,route_generation=?,sender_proof=?,revision=1 WHERE id=1",(T-50,'', '1'*32+':0','mail-route-'+'1'*32,json.dumps(proof,ensure_ascii=False,sort_keys=True,separators=(',',':'))))
            for name in ('weixin.message','aihot.codex_reset'):
                self.p.db.execute("INSERT INTO subscriptions(id,url,secret,rotate_until,expires,verified_at,name,revision) VALUES(?,?,?,?,?,?,?,?)",('fixture-'+name,'https://fixture.invalid','not-a-real-secret',0,T+500,T,name,'fixture'))
        self.relay=Relay(self.data,now_callable=lambda:self.wall[0],operator_config=OPERATOR);self.relay.baseline([],T,checked_at=T)
        old=BridgeHook(self.p.weixin,operator_config=OPERATOR,local_commit_verified=True,data_dir=self.data,now_callable=lambda:self.wall[0],monotonic=lambda:self.mono[0])
        sample=old.host.sample_binding();self.pin=sample['owner_pin'];self.assertTrue(old.host.approve_owner_pin(self.pin))
        self.control=dict(version=1,enabled=True,expected_generation=sample['generation'],owner_pin=self.pin,consumer_activation_at=T-100,revision=1,email_enabled=False)
        atomic_json(self.data/'wechat-consumer.json',self.control)
        with self.p.db:
            self.p.db.execute('UPDATE aihot_email_config SET owner_pin=?',(self.pin,))
            auto_resume.initialize_approved(self.p.db,self.control,T,T+1000,OPERATOR)
        self.deadline=mock.patch.object(auto_resume,'read_hard_stop',return_value=T+1000);self.deadline.start();self.addCleanup(self.deadline.stop)
    def restart(self):
        self.wall[0]+=10;self.mono[0]+=10
        self.hook=BridgeHook(self.p.weixin,operator_config=OPERATOR,local_commit_verified=True,data_dir=self.data,now_callable=lambda:self.wall[0],monotonic=lambda:self.mono[0]);self.p.aihot_notices=self.hook
        return self.hook
    def state(self):return json.loads((self.data/'wechat-consumer.json').read_text())
    def test_same_binding_resume_preserves_cutoff_and_email_stays_disabled(self):
        hook=self.restart();result=hook.tick();status=hook.status_snapshot()
        self.assertEqual(status['last_auto_resume_result'],'maintenance_resumed')
        self.assertTrue(status['enabled']);self.assertFalse(status['requires_rearm'])
        self.assertEqual(self.state()['consumer_activation_at'],T-100)
        self.assertEqual(self.state()['revision'],2)
        self.assertEqual(self.p.db.execute('SELECT revision FROM aihot_email_config').fetchone()[0],1)
        self.mono[0]+=11;self.wall[0]+=11;hook.tick();self.assertEqual(self.state()['revision'],2)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0],0)
    def test_offline_same_owner_rebind_changes_epoch_and_blocks(self):
        self.p.weixin.bind_confirmed(dict(status='confirmed',ilink_bot_id='fixture-bot',ilink_user_id='fixture-owner',bot_token='fixture-token'))
        hook=self.restart();hook.tick();self.assertFalse(hook.status_snapshot()['enabled']);self.assertTrue(hook.status_snapshot()['requires_rearm']);self.assertEqual(self.state()['revision'],1)
    def test_missing_or_corrupt_authorization_fails_closed(self):
        for bad in (None,'{invalid',json.dumps({'version':'wrong'})):
            with self.subTest(bad=bad):
                with self.p.db:
                    if bad is None:self.p.db.execute('DELETE FROM aihot_auto_resume')
                    else:self.p.db.execute('INSERT OR REPLACE INTO aihot_auto_resume(id,policy) VALUES(1,?)',(bad,))
                hook=self.restart();hook.tick();self.assertFalse(hook.status_snapshot()['enabled']);self.assertTrue(hook.status_snapshot()['requires_rearm']);self.assertEqual(self.state()['revision'],1)
    def test_missing_epoch_and_credential_or_route_change_fail_closed(self):
        original=self.p.db.execute('SELECT epoch FROM weixin_binding_epoch WHERE id=1').fetchone()[0]
        for mode in ('missing','credential','route'):
            with self.subTest(mode=mode):
                with self.p.db:
                    if mode=='missing':self.p.db.execute('DELETE FROM weixin_binding_epoch')
                    elif mode=='credential':self.p.db.execute("UPDATE weixin_accounts SET token='different-fixture-token'")
                    else:self.p.db.execute("UPDATE aihot_email_config SET route_generation=?",('mail-route-'+'2'*32,))
                hook=self.restart();hook.tick();self.assertFalse(hook.status_snapshot()['enabled']);self.assertEqual(self.state()['revision'],1)
                with self.p.db:
                    self.p.db.execute('INSERT OR REPLACE INTO weixin_binding_epoch(id,epoch) VALUES(1,?)',(original,))
                    self.p.db.execute("UPDATE weixin_accounts SET token='fixture-token'")
                    self.p.db.execute("UPDATE aihot_email_config SET route_generation=?",('mail-route-'+'1'*32,))
    def test_expired_subscription_and_hard_stop_fail_closed(self):
        with self.p.db:self.p.db.execute('UPDATE subscriptions SET expires=?',(T-1,))
        hook=self.restart();hook.tick();self.assertFalse(hook.status_snapshot()['enabled']);self.assertEqual(self.state()['revision'],1)
        with self.p.db:self.p.db.execute('UPDATE subscriptions SET expires=?',(T+500,))
        with mock.patch.object(auto_resume,'read_hard_stop',return_value=T+5):
            hook=self.restart();hook.tick();self.assertFalse(hook.status_snapshot()['enabled']);self.assertEqual(self.state()['revision'],1)
    def test_unknown_or_accepted_outgoing_is_not_replayed(self):
        with self.p.db:
            for i,status in enumerate(('delivery_unknown','accepted')):
                self.p.db.execute("INSERT INTO weixin_outbox(message_id,account,peer,text,client_id,status,attempts,created_at,kind) VALUES(?,?,?,?,?,?,?,?,?)",('fixture-'+str(i),'fixture-bot','fixture-owner','fixture','fixture-client-'+str(i),status,1,T,'notification'))
        before=[tuple(x) for x in self.p.db.execute('SELECT message_id,status,attempts FROM weixin_outbox ORDER BY message_id')]
        self.restart().tick();after=[tuple(x) for x in self.p.db.execute('SELECT message_id,status,attempts FROM weixin_outbox ORDER BY message_id')]
        self.assertEqual(before,after)
    def test_new_event_after_restart_is_queued_once_without_network(self):
        hook=self.restart();hook.tick()
        eid='reset-1900000000000000001-1-1';url='https://x.com/thsottiaux/status/1900000000000000001'
        self.wall[0]=T+30;self.mono[0]=130
        intent=dict(event_id=eid,stage='announced',source_at=T+20,expires_at=T+200,link=url,eligible=True,text=notification_text('announced',T+20,url))
        self.relay.sync([intent],T+30,checked_at=T+30)
        hook.tick();self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0],1)
        self.wall[0]+=11;self.mono[0]+=11;hook.tick()
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0],1)

    def test_preboot_queued_notification_is_held_but_reply_progresses(self):
        eid='reset-1900000000000000001-1-1';url='https://x.com/thsottiaux/status/1900000000000000001'
        intent=dict(event_id=eid,stage='announced',source_at=T+1,expires_at=T+200,link=url,eligible=True,text=notification_text('announced',T+1,url))
        self.relay.sync([intent],T+2,checked_at=T+2)
        request=self.relay.status()['entries'][eid+'/announced']['channels']['wechat']['request_id']
        old=self.p.weixin.queue_notification(request,intent['text'])['send_id']
        self.wall[0]=T+3;self.mono[0]=103
        hook=self.restart();hook.tick();held,hold=hook.sender_guard()
        self.assertFalse(hold);self.assertIn(old,held)
        self.p.weixin.aihot_held_notification_ids=held
        reply=self.p.weixin.queue_reply(APPROVAL,'fixture reply')['send_id']
        self.assertTrue(self.p.weixin.send_once())
        self.assertEqual(self.p.weixin.send_status(reply)['status'],'context_required')
        self.assertEqual(self.p.weixin.send_status(old)['status'],'queued')
        self.assertFalse(self.p.weixin.send_once())
        self.assertEqual(self.p.weixin.send_status(old)['attempts'],0)

    def test_guard_does_not_block_event_added_after_startup(self):
        hook=self.restart();hook.tick()
        eid='reset-1900000000000000001-1-1';url='https://x.com/thsottiaux/status/1900000000000000001'
        self.wall[0]=T+30;self.mono[0]=130
        intent=dict(event_id=eid,stage='announced',source_at=T+20,expires_at=T+200,link=url,eligible=True,text=notification_text('announced',T+20,url))
        self.relay.sync([intent],T+30,checked_at=T+30)
        held,hold=hook.sender_guard();self.assertFalse(hold)
        request=self.relay.status()['entries'][eid+'/announced']['channels']['wechat']['request_id']
        import hashlib,json
        nid='notice_'+hashlib.sha256(json.dumps(['fixture-bot',request],sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()[:32]
        self.assertNotIn(nid,held)
        self.p.weixin.aihot_held_notification_ids=held;hook.tick()
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox WHERE message_id=?',(nid,)).fetchone()[0],1)

    def test_marker_corruption_after_auto_resume_blocks_future_delivery(self):
        hook=self.restart();hook.tick();self.assertTrue(hook.status_snapshot()['enabled'])
        with self.p.db:self.p.db.execute("UPDATE aihot_auto_resume SET policy='{}' WHERE id=1")
        self.wall[0]+=11;self.mono[0]+=11;hook.tick()
        self.assertFalse(hook.status_snapshot()['enabled']);self.assertTrue(hook.status_snapshot()['requires_rearm'])
        self.assertTrue(hook.status_snapshot()['auto_guard_blocked'])

    def test_unidentified_preboot_notification_is_held_not_replayed(self):
        old=self.p.weixin.queue_notification('uncorrelated-fixture','fixture')['send_id']
        hook=self.restart();hook.tick();held,hold=hook.sender_guard()
        self.assertFalse(hold);self.assertIn(old,held)
        self.p.weixin.aihot_held_notification_ids=held
        self.assertFalse(self.p.weixin.send_once())
        self.assertEqual(self.p.weixin.send_status(old)['status'],'queued')
        self.assertEqual(self.p.weixin.send_status(old)['attempts'],0)

    def test_route_change_after_auto_resume_fails_closed(self):
        hook=self.restart();hook.tick();self.assertTrue(hook.status_snapshot()['enabled'])
        with self.p.db:self.p.db.execute("UPDATE aihot_email_config SET route_generation=?",('mail-route-'+'2'*32,))
        self.wall[0]+=11;self.mono[0]+=11;hook.tick()
        self.assertFalse(hook.status_snapshot()['enabled']);self.assertTrue(hook.status_snapshot()['requires_rearm'])
        self.assertTrue(hook.status_snapshot()['auto_guard_blocked'])

    def test_supported_rebind_after_auto_resume_immediately_holds_notifications(self):
        hook=self.restart();hook.tick();self.assertTrue(hook.status_snapshot()['enabled'])
        self.p.weixin.bind_confirmed(dict(status='confirmed',ilink_bot_id='fixture-bot',ilink_user_id='fixture-owner',bot_token='fixture-token'))
        self.assertTrue(self.p.weixin.aihot_hold_all_notifications)
        self.wall[0]+=11;self.mono[0]+=11;hook.tick()
        self.assertFalse(hook.status_snapshot()['enabled']);self.assertTrue(hook.status_snapshot()['requires_rearm'])

    def test_wall_clock_jump_blocks_resume(self):
        hook=self.restart();self.wall[0]+=300;hook.tick()
        self.assertFalse(hook.status_snapshot()['enabled']);self.assertEqual(hook.status_snapshot()['last_auto_resume_result'],'auto_clock_unverifiable')

    def test_explicit_off_never_autorearms(self):
        control=self.state();control['enabled']=False;atomic_json(self.data/'wechat-consumer.json',control)
        hook=self.restart();hook.tick();self.assertFalse(hook.status_snapshot()['enabled']);self.assertFalse(self.state()['enabled'])

    def test_off_after_auto_resume_releases_active_guard(self):
        hook=self.restart();hook.tick();self.assertTrue(hook.status_snapshot()['enabled'])
        self.assertEqual(hook.off()['result'],'consumer_off')
        self.wall[0]+=11;self.mono[0]+=11;hook.tick()
        self.assertFalse(hook.status_snapshot()['auto_guard_blocked'])

    def test_transient_owner_unready_then_verified_step_clears_status(self):
        hook=self.restart()
        with mock.patch.object(hook.controller,'step',side_effect=[{'result':'owner_not_ready'},{'result':'blocked_by_activation'}]):
            hook.tick();self.assertTrue(hook.status_snapshot()['requires_rearm'])
            self.wall[0]+=11;self.mono[0]+=11;hook.tick()
            self.assertFalse(hook.status_snapshot()['requires_rearm'])
            self.assertEqual(self.state()['revision'],2)

    def test_hard_stop_reads_only_valid_matching_service_configuration(self):
        repo=(self.root/'repo').resolve();source=repo/'probe'/'aihot_notice_consumer'/'auto_resume.py'
        source.parent.mkdir(parents=True);service=self.root/'wechat-tunnel-service';service.mkdir()
        config=service/'service-config.json'
        config.write_text(json.dumps({'repo':str(repo),'hard_stop_at':'2027-01-15T08:00:00+00:00'}))
        with mock.patch.object(auto_resume,'__file__',str(source)):
            self.assertEqual(ORIGINAL_READ_HARD_STOP(),1800000000.0)
            config.write_text(json.dumps({'repo':str(repo/'wrong'),'hard_stop_at':'2027-01-15T08:00:00+00:00'}))
            with self.assertRaises(ValueError): ORIGINAL_READ_HARD_STOP()
            config.unlink()
            with self.assertRaises(FileNotFoundError): ORIGINAL_READ_HARD_STOP()

    def test_missing_authorization_then_explicit_resume_still_blocks_old_pending(self):
        eid='reset-1900000000000000001-1-1';url='https://x.com/thsottiaux/status/1900000000000000001'
        intent=dict(event_id=eid,stage='announced',source_at=T+1,expires_at=T+200,link=url,eligible=True,text=notification_text('announced',T+1,url))
        self.relay.sync([intent],T+2,checked_at=T+2)
        with self.p.db:self.p.db.execute('DELETE FROM aihot_auto_resume')
        self.wall[0]=T+3;self.mono[0]=103
        hook=self.restart();hook.tick();self.assertFalse(hook.status_snapshot()['enabled'])
        self.assertEqual(hook.arm(expected_owner_pin=self.pin)['result'],'maintenance_resumed')
        self.wall[0]+=11;self.mono[0]+=11;hook.tick()
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0],0)

    def test_pre_restart_pending_is_not_enqueued(self):
        eid='reset-1900000000000000001-1-1';url='https://x.com/thsottiaux/status/1900000000000000001'
        intent=dict(event_id=eid,stage='announced',source_at=T+1,expires_at=T+200,link=url,eligible=True,text=notification_text('announced',T+1,url))
        self.relay.sync([intent],T+2,checked_at=T+2)
        self.wall[0]=T+3;self.mono[0]=103
        hook=self.restart();hook.tick()
        self.assertEqual(hook.status_snapshot()['last_auto_resume_result'],'maintenance_resumed')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0],0)
        self.assertEqual(self.relay.status()['entries'][eid+'/announced']['channels']['wechat']['state'],'pending')

if __name__=='__main__':unittest.main()
