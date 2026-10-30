import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from probe.official_usage_provider import OfficialCapability, OfficialReadDenied, OfficialUsageProvider
from probe.usage_display import UsageBinding, CreditInfo
from probe.owner_coordinator import OwnerCoordinator, PlatformDenied, from_host_configuration


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.binding=UsageBinding('synthetic-owner','synthetic-account',True)
        self.raw={'rateLimits':{'limitId':'codex','primary':{'usedPercent':25,'windowDurationMins':10080,'resetsAt':1000},'secondary':None}}

    def test_registered_official_tool_once_four_lines_and_no_invented_five_hour_window(self):
        read=Mock(return_value=self.raw)
        p=OfficialUsageProvider.from_host_tools({'mcp__codex_app__get_usage_limits':read},self.binding,'fixture-host-account-proof',clock=lambda:100)
        out=p.prepare(self.binding)
        read.assert_called_once_with({})
        self.assertEqual(out['status'],'reported');self.assertEqual(len(out['text'].splitlines()),4)
        self.assertIn('剩余75%',out['text']);self.assertNotIn('5小时',out['text'])

    def test_missing_tool_is_not_an_api_failure_and_never_called(self):
        read=Mock()
        p=OfficialUsageProvider.from_host_tools({'unrelated_tool':read},self.binding,'proof',clock=lambda:100)
        out=p.prepare(self.binding)
        self.assertFalse(out['official_call_attempted']);self.assertIn('未调用',out['text']);read.assert_not_called()

    def test_mismatched_account_never_invokes_reader(self):
        read=Mock()
        p=OfficialUsageProvider(OfficialCapability('get_usage_limits',UsageBinding('other','other',True),'proof',read),clock=lambda:100)
        self.assertFalse(p.prepare(self.binding)['official_call_attempted']);read.assert_not_called()

    def test_actual_failure_and_permission_denial_are_distinct_without_retry(self):
        for error,status in [(TimeoutError(),'read_failed'),(OfficialReadDenied(),'permission_denied')]:
            read=Mock(side_effect=error)
            p=OfficialUsageProvider.from_host_tools({'mcp__codex_app__get_usage_limits':read},self.binding,'proof',clock=lambda:100)
            out=p.prepare(self.binding)
            self.assertEqual(out['status'],status);self.assertEqual(len(out['text'].splitlines()),4)
            self.assertTrue(out['official_call_attempted']);self.assertEqual(read.call_count,1)

    def test_error_envelope_is_actual_failure(self):
        p=OfficialUsageProvider.from_host_tools({'mcp__codex_app__get_usage_limits':lambda _: {'isError':True}},self.binding,'proof',clock=lambda:100)
        self.assertEqual(p.prepare(self.binding)['status'],'read_failed')

    def test_existing_session_uses_rpc_method_not_url_or_new_login(self):
        rpc=Mock(return_value={'result':self.raw})
        p=OfficialUsageProvider.from_existing_app_server(rpc,self.binding,'existing-session-proof',clock=lambda:100)
        self.assertEqual(p.prepare(self.binding)['status'],'reported')
        rpc.assert_called_once_with('account/rateLimits/read',{})

    def test_verified_credit_mapper_preserves_real_zero_not_unknown(self):
        p=OfficialUsageProvider.from_host_tools({'mcp__codex_app__get_usage_limits':lambda _:self.raw},
                self.binding,'proof',credit_mapper=lambda _:CreditInfo(balance='0',unlimited=False,mapping_verified=True,scope='reported'),clock=lambda:100)
        self.assertEqual(p.prepare(self.binding)['text'].splitlines()[3],'💳 信用点余额：0')


class AsyncProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_host_tool_is_awaited_once(self):
        calls=[]
        async def read(args):
            calls.append(args)
            return {'rateLimits':{'primary':{'usedPercent':20,'windowDurationMins':10080,'resetsAt':1000}}}
        binding=UsageBinding('owner','account',True)
        p=OfficialUsageProvider.from_host_tools({'mcp__codex_app__get_usage_limits':read},binding,'proof',clock=lambda:100)
        result=await p.prepare_async(binding)
        self.assertEqual(result['status'],'reported');self.assertEqual(calls,[{}])


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger';self.calls=[];self.failure=None
        self.state={'ack':{'committed':False,'send_id':'ack-original'},'final':{'committed':False,'send_id':'final-original'}}
        self.scope={'verified_scope':True,'request_ref':'message','evidence_origin':'direct_owner_message'}
        self.coordinator=None;self.addCleanup(self.close)

    def close(self):
        if self.coordinator:self.coordinator.close();self.coordinator=None

    def host(self,name,args):
        self.calls.append((name,args))
        if name=='read_weixin_message':
            return {'source':'owner_weixin_dm','message_id':args['message_id'],'reply_state':copy.deepcopy(self.state)}
        if name=='get_weixin_upload_status':
            return {'sha256':'a'*64,'kind':self.kind,'status':getattr(self,'upload_status','ready')}
        if self.failure:raise self.failure
        return {'send_id':args.get('send_id',args.get('phase','final')+'-original'),'status':'accepted'}

    def open(self,mode='execute',permit=lambda *_:True):
        self.coordinator=OwnerCoordinator(self.path,coordinator_id='fixture-coordinator',caller_id='fixture-coordinator',
                                          call_tool=self.host,mode=mode,permit=permit)
        return self.coordinator

    def reply(self,**kw):
        return self.coordinator.reply('message','synthetic answer',task_complete=kw.get('complete',True),needs_work=kw.get('work',False),scope=kw.get('scope',self.scope))

    def writes(self):return [x for x in self.calls if x[0].startswith(('reply_','send_'))]

    def test_dry_run_prepares_direct_final_without_write_or_claim(self):
        c=self.open('dry_run');r=self.reply()
        self.assertEqual(r['tool'],'reply_weixin_message');self.assertEqual(r['action'],'dry_run')
        self.assertEqual(self.writes(),[]);self.assertEqual(c.db.execute('SELECT count(*) FROM actions').fetchone()[0],0)

    def test_long_progress_then_final_and_existing_final_query(self):
        self.open();self.reply(complete=False,work=True)
        self.state['ack']['committed']=True
        self.reply(complete=True,work=True)
        self.assertEqual([x[1]['phase'] for x in self.writes()],['ack','final'])
        self.state['final']['committed']=True
        self.assertEqual(self.reply()['action'],'query_original_send');self.assertEqual(len(self.writes()),2)

    def test_no_host_write_permit_never_claims_or_calls_send(self):
        c=self.open(permit=None)
        self.assertEqual(self.reply()['action'],'await_current_writer_and_owner_permission')
        self.assertEqual(self.writes(),[]);self.assertEqual(c.db.execute('SELECT count(*) FROM actions').fetchone()[0],0)

    def test_timeout_persists_across_restart_no_new_submission(self):
        self.open();self.failure=TimeoutError()
        self.assertEqual(self.reply()['action'],'reconcile_original_submission')
        self.close();self.open();self.failure=None
        self.assertEqual(self.reply()['action'],'reconcile_original_submission');self.assertEqual(len(self.writes()),1)

    def test_denial_persists_and_no_alternate_tool(self):
        self.open();self.failure=PlatformDenied()
        self.assertEqual(self.reply()['action'],'stop_permission_denied')
        self.failure=None
        self.assertEqual(self.reply()['action'],'stop_permission_denied');self.assertEqual(len(self.writes()),1)

    def test_same_key_duplicate_only_one_submit_even_with_stale_remote_read(self):
        self.open();a=self.reply();b=self.reply()
        self.assertEqual(a['send_id'],b['send_id']);self.assertEqual(len(self.writes()),1)

    def test_quote_forward_attachment_and_agent_summary_are_not_authority(self):
        self.open()
        for origin in ('quoted_text','forwarded_message','attachment','agent_summary'):
            self.assertEqual(self.reply(scope=dict(self.scope,evidence_origin=origin))['action'],'await_owner_verification')
        self.assertEqual(self.writes(),[])

    def test_second_local_coordinator_is_fenced(self):
        self.open()
        with self.assertRaises(BlockingIOError):self.open()

    def test_wrong_coordinator_id_rejected_before_database(self):
        with self.assertRaises(ValueError):
            OwnerCoordinator(self.path,coordinator_id='expected',caller_id='other',call_tool=self.host)
        self.assertFalse(self.path.exists())

    def test_configuration_wires_missing_provider_to_not_called_and_keeps_dry_run(self):
        config={'version':1,'mode':'dry_run','coordinator_id':'fixture-coordinator',
                'usage_provider':'get_usage_limits','system_ack':False,'old_dot_mirror_writes':False}
        self.coordinator=from_host_configuration(config,self.path,caller_id='fixture-coordinator',call_tool=self.host,
                                                 binding=UsageBinding('owner','account',True),binding_evidence_ref='proof')
        result=self.coordinator.prepare_usage(UsageBinding('owner','account',True))
        self.assertFalse(result['official_call_attempted'])
        self.assertEqual(self.reply()['action'],'dry_run');self.assertEqual(self.writes(),[])

    def test_configuration_cannot_enable_old_dot_mirror_or_system_ack(self):
        for field in ('system_ack','old_dot_mirror_writes'):
            config={'version':1,'mode':'dry_run','coordinator_id':'fixture-coordinator',
                    'usage_provider':'get_usage_limits','system_ack':False,'old_dot_mirror_writes':False}
            config[field]=True
            with self.assertRaises(ValueError):
                from_host_configuration(config,self.path,caller_id='fixture-coordinator',call_tool=self.host)
        self.assertFalse(self.path.exists())

    def test_four_media_parts_after_text_final_are_independent_and_not_replayed(self):
        c=self.open();self.state['final']['committed']=True
        for part,kind,channel in [('png','preview_image','image'),('pdf','requested_document','file'),('docx','requested_document','file'),('mp4','requested_video','video')]:
            self.kind=channel;self.upload_status='ready'
            item={'owner_ref':'owner','request_ref':'message','artifact_key':'v1','attachment_key':part,'sha256':'a'*64,'kind':kind}
            scope=dict(item,verified_scope=True,evidence_origin='trusted_parent_transcript',evidence_ref='actual-owner-evidence',media_kind=channel)
            r=c.media(item,scope,archive_verified=True,upload_id='upload-'+part)
            self.assertFalse(r['owner_received'])
            self.upload_status='committed'
            again=c.media(item,scope,archive_verified=True,upload_id='upload-'+part)
            self.assertEqual(r['send_id'],again['send_id'])
        self.assertEqual(len(self.writes()),4)
        self.assertEqual(len({x[1]['send_id'] for x in self.writes()}),4)

    def test_accepted_does_not_become_phone_receipt_without_explicit_evidence(self):
        c=self.open();r=self.reply();self.assertFalse(r['owner_received'])
        with self.assertRaises(ValueError):c.record_owner_receipt(r['key'],send_id='wrong',evidence_ref='phone')
        c.record_owner_receipt(r['key'],send_id=r['send_id'],evidence_ref='verified-owner-receipt')
        self.assertTrue(self.reply()['owner_received'])


if __name__=='__main__':unittest.main()
