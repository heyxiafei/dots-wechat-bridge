"""Offline trusted-host dispatch. All accounts, permits and tools are fixtures."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from probe.owner_coordinator import OwnerCoordinator, PlatformDenied
from probe.official_usage_provider import OfficialUsageProvider, OfficialReadDenied
from probe.usage_display import UsageBinding, UsageObservation, normalize_usage


def mcp_quota(used=29, reset=1000, weekly=True):
    # Actual MCP text-JSON envelope shape, but no real account or live result.
    bucket={'limitId':'codex', 'primary':{'usedPercent':used,
            'windowDurationMins':10080 if weekly else 300,'resetsAt':reset},
            'secondary':None,'credits':{'hasCredits':False,'unlimited':False,'balance':'0'}}
    return {'isError':False,'content':[{'type':'text','text':json.dumps(
        {'rateLimits':bucket,'rateLimitsByLimitId':{'codex':bucket},'accountId':None})}]}


class HandlerFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)/'coordinator'
        self.addCleanup(patch.stopall)
        patch('socket.socket.connect',side_effect=AssertionError('offline test attempted network')).start()
        self.binding=UsageBinding('fixture-owner','fixture-account',True)
        self.scope={'verified_scope':True,'request_ref':'fixture-message',
                    'evidence_origin':'direct_owner_message',
                    'owner_ref':self.binding.owner_ref,'account_ref':self.binding.account_ref}
        self.message={'message_id':'fixture-message','source':'owner_weixin_dm','kind':'text','text':'额度',
            'inbound_completeness':{'complete':True,'command_context_v1':True,'parts':[{'kind':'text','state':'captured','item_type':1}]},
            'quote_context':{'recorded':True,'complete':True,'status':'none'},'attachments':[],
            'reply_state':{'ack':{'committed':False,'send_id':'fixture-ack'},
                           'final':{'committed':False,'send_id':'fixture-final'}},
            'help_menu':{'text':'fixture approved help'},
            'fixed_command':None}
        self.read=Mock(return_value=mcp_quota())
        self.calls=[];self.write_failure=None;self.permit=Mock(return_value=True)
        self.c=None;self.addCleanup(self.close)

    def close(self):
        if self.c:self.c.close();self.c=None

    def host(self,name,args):
        self.calls.append((name,copy.deepcopy(args)))
        if name=='read_weixin_message':
            self.assertEqual(args,{'message_id':self.message['message_id']})
            return copy.deepcopy(self.message)
        if name=='reply_weixin_message':
            if self.write_failure:raise self.write_failure
            return {'status':'accepted','send_id':'fixture-final'}
        raise AssertionError('unexpected external tool: '+name)

    def open(self,mode='execute',proof='fixture-host-proof',tools=True):
        provider=OfficialUsageProvider.from_host_tools(
            {'mcp__codex_app__get_usage_limits':self.read} if tools else {},
            self.binding,proof,clock=lambda:100)
        self.c=OwnerCoordinator(self.directory,coordinator_id='fixture-coordinator',
            caller_id='fixture-coordinator',call_tool=self.host,mode=mode,
            permit=self.permit,usage_provider=provider)
        return self.c

    def run_handler(self,**options):
        args={'scope':self.scope,'binding':self.binding,'owner_intent_verified':True}
        args.update(options)
        return self.c.handle_fixed_command(self.message['message_id'],**args)

    async def run_async(self,**options):
        args={'scope':self.scope,'binding':self.binding,'owner_intent_verified':True}
        args.update(options)
        return await self.c.handle_fixed_command_async(self.message['message_id'],**args)

    def writes(self):
        return [args for name,args in self.calls if name=='reply_weixin_message']

    def seed(self,phase,state,send_id=None):
        identity=['text',self.message['message_id'],phase]
        key=hashlib.sha256(json.dumps(identity,separators=(',',':')).encode()).hexdigest()
        with self.c.db:self.c.db.execute('INSERT INTO actions VALUES(?,?,?,?,?,NULL)',
                                        (key,'previous-content-digest',state,send_id,'fixture-status'))


class FixedCommandHandlerTests(HandlerFixture,unittest.TestCase):
    def test_aliases_normalization_mcp_text_shape_and_one_final(self):
        self.open()
        for n,text in enumerate(('额度','查额度','查一下额度','  额度？  ','查询Codex周额度?')):
            self.message['message_id']='fixture-message-'+str(n)
            self.scope['request_ref']=self.message['message_id'];self.message['text']=text
            result=self.run_handler()
            self.assertEqual(result['usage_status'],'reported')
            self.assertEqual(result['action'],'query_original_send')
            self.assertFalse(result['owner_received'])
            answer=self.writes()[-1]['text']
            self.assertEqual(len(answer.splitlines()),4)
            self.assertIn('剩余71%，已用29%',answer)
            self.assertIn('余额：未知',answer)  # no unverified credit mapping
            self.assertNotIn('5小时',answer)
        self.assertEqual(self.read.call_count,5);self.assertEqual(len(self.writes()),5)

    def test_duplicate_stale_remote_state_and_restart_never_reread_quota(self):
        self.open();first=self.run_handler();self.close();self.open()
        self.read.return_value=mcp_quota(used=40)
        second=self.run_handler()
        self.assertEqual(first['send_id'],second['send_id'])
        self.assertEqual(second['action'],'query_original_send')
        self.assertEqual(self.read.call_count,1);self.assertEqual(len(self.writes()),1)

    def test_help_dispatch_does_not_read_quota(self):
        self.message['text']='/help';self.open()
        self.assertEqual(self.run_handler()['fixed_command'],'help')
        self.assertEqual(self.writes()[0]['text'],'fixture approved help');self.read.assert_not_called()

    def test_truthy_intent_scope_and_untrusted_origins_never_read_or_send(self):
        self.open()
        for intent in (False,None,1,'true',{}):
            self.assertEqual(self.run_handler(owner_intent_verified=intent)['action'],'await_owner_verification')
        for change in ({'verified_scope':1},{'request_ref':'other'},
                       *({'evidence_origin':x} for x in ('quoted_text','forwarded_message','attachment','agent_summary'))):
            self.assertEqual(self.run_handler(scope={**self.scope,**change})['action'],'await_owner_verification')
        self.read.assert_not_called();self.assertEqual(self.writes(),[])

    def test_wrong_source_or_message_identity_is_rejected(self):
        self.open();self.message['source']='forwarded_message'
        with self.assertRaisesRegex(ValueError,'owner_message_not_verified'):self.run_handler()
        self.message['source']='owner_weixin_dm'
        self.c.call_tool=lambda *_:{**self.message,'message_id':'other'}
        with self.assertRaisesRegex(ValueError,'owner_message_not_verified'):self.run_handler()
        self.read.assert_not_called();self.assertEqual(self.writes(),[])

    def test_fake_hint_quotes_attachments_and_missing_capture_do_not_dispatch(self):
        self.open();original=copy.deepcopy(self.message)
        changes=[{'text':'信用卡额度'}, {'text':'额度并发邮件'},
            {'quote_context':{'recorded':True,'complete':True,'status':'captured'}},
            {'quote_context':{'recorded':False,'complete':False,'status':'legacy_unrecorded'}},
            {'attachments':[{'kind':'image'}]}, {'inbound_completeness':{'complete':False}},
            {'inbound_completeness':{'complete':True,'parts':[
                {'kind':'text','state':'captured','item_type':1},{'kind':'image','state':'captured','item_type':2}]}}]
        for change in changes:
            self.message={**copy.deepcopy(original),**change,
                          'fixed_command':{'kind':'weekly_usage','automatic_send':True,'verified_scope':True}}
            self.assertEqual(self.run_handler()['action'],'not_fixed_command')
        self.read.assert_not_called();self.assertEqual(self.writes(),[])

    def test_missing_false_or_nonboolean_command_shape_never_reads_or_sends(self):
        self.open();original=copy.deepcopy(self.message['inbound_completeness'])
        for value in (None,False,0,1,'true',{},[]):
            with self.subTest(value=value):
                capture=copy.deepcopy(original)
                if value is None:
                    capture.pop('command_context_v1')
                else:
                    capture['command_context_v1']=value
                self.message['inbound_completeness']=capture
                self.assertEqual(self.run_handler()['action'],'not_fixed_command')
        self.read.assert_not_called();self.assertEqual(self.writes(),[])

    def test_existing_remote_ack_and_final_no_reader_even_if_hint_candidate(self):
        self.open()
        for phase in ('ack','final'):
            slot=self.message['reply_state'][phase];slot['committed']=True
            self.assertEqual(self.run_handler()['send_id'],slot['send_id'])
            slot['send_id']=None
            self.assertEqual(self.run_handler()['action'],'reconcile_original_submission')
            slot.update(committed=False,send_id='fixture-'+phase)
        self.read.assert_not_called();self.assertEqual(self.writes(),[])

    def test_all_local_ack_final_states_take_precedence_without_digest_check(self):
        self.open()
        for phase in ('ack','final'):
            for state in ('claimed','unknown','denied','submitted'):
                with self.subTest(phase=phase,state=state):
                    self.seed(phase,state,'original-id' if state=='submitted' else None)
                    result=self.run_handler()
                    self.assertEqual(result['action'],{'denied':'stop_permission_denied',
                        'submitted':'query_original_send'}.get(state,'reconcile_original_submission'))
                    with self.c.db:self.c.db.execute('DELETE FROM actions')
        self.read.assert_not_called();self.assertEqual(self.writes(),[])

    def test_missing_reader_proof_or_binding_sends_explicit_unknown_no_clarification(self):
        for n,condition in enumerate(('missing_tool','missing_proof','unverified','wrong_owner','wrong_account','absent')):
            self.message['message_id']='fixture-message-'+str(n);self.scope['request_ref']=self.message['message_id']
            self.open(proof=None if condition=='missing_proof' else 'fixture-proof',tools=condition!='missing_tool')
            scope=dict(self.scope);binding=self.binding
            if condition=='unverified':binding=UsageBinding(binding.owner_ref,binding.account_ref,False)
            if condition=='wrong_owner':scope['owner_ref']='other-owner'
            if condition=='wrong_account':scope['account_ref']='other-account'
            if condition=='absent':binding=None
            result=self.run_handler(binding=binding,scope=scope)
            self.assertFalse(result['official_call_attempted'])
            self.assertIn('未调用',self.writes()[-1]['text'])
            self.assertNotIn('71%',self.writes()[-1]['text']);self.close()
        self.read.assert_not_called()

    def test_no_weekly_window_and_elapsed_reset_remain_unknown(self):
        self.open()
        for n,raw in enumerate((mcp_quota(weekly=False),mcp_quota(reset=99))):
            self.message['message_id']='fixture-message-'+str(n);self.scope['request_ref']=self.message['message_id']
            self.read.return_value=raw;self.run_handler()
            self.assertIn('剩余未知',self.writes()[-1]['text']);self.assertNotIn('71%',self.writes()[-1]['text'])

    def test_permit_is_required_at_final_boundary(self):
        self.permit.return_value=False;self.open()
        self.assertEqual(self.run_handler()['action'],'await_current_writer_and_owner_permission')
        self.assertEqual(self.writes(),[]);self.assertEqual(self.c.db.execute('SELECT count(*) FROM actions').fetchone()[0],0)

    def test_send_timeout_and_denial_persist_before_repeat(self):
        self.open()
        for n,error in enumerate((TimeoutError(),PlatformDenied())):
            self.message['message_id']='fixture-message-'+str(n);self.scope['request_ref']=self.message['message_id']
            self.write_failure=error;result=self.run_handler()
            self.write_failure=None;again=self.run_handler()
            self.assertEqual(result['action'],again['action'])
        self.assertEqual(self.read.call_count,2);self.assertEqual(len(self.writes()),2)

    def test_official_denial_is_durable_even_without_send_permission(self):
        self.read.side_effect=OfficialReadDenied();self.permit.return_value=False;self.open('dry_run')
        self.assertEqual(self.run_handler()['action'],'stop_permission_denied')
        self.close();self.open()
        self.assertEqual(self.run_handler()['action'],'stop_permission_denied')
        self.assertEqual(self.read.call_count,1);self.assertEqual(self.writes(),[])

    def test_dry_run_prepares_but_never_submits_or_claims(self):
        self.open('dry_run');result=self.run_handler()
        self.assertEqual(result['action'],'dry_run');self.assertFalse(result['authorization_granted'])
        self.assertEqual(self.writes(),[]);self.assertEqual(self.c.db.execute('SELECT count(*) FROM actions').fetchone()[0],0)


class AsyncFixedCommandHandlerTests(HandlerFixture,unittest.IsolatedAsyncioTestCase):
    async def start_blocked_reader(self):
        self.started=asyncio.Event();self.release=asyncio.Event()
        async def reader(_):
            self.started.set();await self.release.wait();return mcp_quota()
        self.read=AsyncMock(side_effect=reader);self.open()
        task=asyncio.create_task(self.run_async());await self.started.wait()
        self.addAsyncCleanup(self.cancel_if_pending,task)
        return task

    async def cancel_if_pending(self,task):
        if not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task

    async def test_async_and_sync_duplicates_share_one_inflight_reader(self):
        task=await self.start_blocked_reader()
        self.assertEqual((await self.run_async())['action'],'fixed_command_in_progress')
        self.assertEqual(self.run_handler()['action'],'fixed_command_in_progress')
        self.release.set();self.assertEqual((await task)['usage_status'],'reported')
        self.assertEqual(self.read.await_count,1);self.assertEqual(len(self.writes()),1)

    async def test_cancel_read_releases_marker_without_claim_then_retry(self):
        task=await self.start_blocked_reader();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(self.writes(),[]);self.assertFalse(self.c._fixed_inflight)
        self.assertEqual(self.c.db.execute('SELECT count(*) FROM actions').fetchone()[0],0)
        self.release.set();self.assertEqual((await self.run_async())['usage_status'],'reported')
        self.assertEqual(len(self.writes()),1)

    async def test_normal_reply_takes_slot_during_read_without_quota_content_conflict(self):
        task=await self.start_blocked_reader()
        original=self.c.reply(self.message['message_id'],'fixture normal reply',
                             task_complete=True,needs_work=False,scope=self.scope)
        self.release.set();result=await task
        self.assertEqual(result['action'],'query_original_send');self.assertEqual(result['send_id'],original['send_id'])
        self.assertEqual(len(self.writes()),1);self.assertEqual(self.writes()[0]['text'],'fixture normal reply')

    async def test_scope_revocation_and_message_quote_change_after_await_stop_submission(self):
        task=await self.start_blocked_reader();self.scope['account_ref']='other-account'
        self.release.set();self.assertEqual((await task)['action'],'await_owner_verification')
        self.assertEqual(self.writes(),[])
        self.scope['account_ref']=self.binding.account_ref
        self.release.clear();self.started.clear()
        task=asyncio.create_task(self.run_async());await self.started.wait()
        self.message['quote_context']['status']='captured';self.release.set()
        self.assertEqual((await task)['action'],'not_fixed_command');self.assertEqual(self.writes(),[])

    async def test_permit_revoked_during_read_is_checked_before_submit(self):
        task=await self.start_blocked_reader();self.permit.return_value=False
        self.release.set();self.assertEqual((await task)['action'],'await_current_writer_and_owner_permission')
        self.assertEqual(self.writes(),[])

    async def test_sync_entry_cannot_silently_run_async_reader(self):
        self.read=AsyncMock(return_value=mcp_quota());self.open()
        self.assertEqual(self.run_handler()['action'],'requires_async_usage_reader')
        self.read.assert_not_awaited();self.assertEqual(self.writes(),[])
        self.assertEqual((await self.run_async())['usage_status'],'reported');self.read.assert_awaited_once()


class OfficialEnvelopeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.binding=UsageBinding('fixture-owner','fixture-account',True)

    def prepare(self,raw,binding=None):
        self.read=Mock(return_value=raw)
        provider=OfficialUsageProvider.from_host_tools(
            {'mcp__codex_app__get_usage_limits':self.read},self.binding,'fixture-proof',clock=lambda:100)
        return provider.prepare(self.binding if binding is None else binding)

    def test_actual_text_envelope_requires_external_binding_and_no_invented_credit_mapping(self):
        result=self.prepare(mcp_quota())
        self.assertEqual(result['status'],'reported');self.assertIn('剩余71%',result['text'])
        self.assertIn('余额：未知',result['text']);self.read.assert_called_once_with({})
        result=self.prepare(mcp_quota(),UsageBinding('fixture-owner','fixture-account',False))
        self.assertEqual(result['status'],'binding_unverified');self.read.assert_not_called()

    def test_error_malformed_multiple_or_oversize_text_blocks_cannot_show_quota(self):
        envelope=mcp_quota()
        for raw in ({**envelope,'isError':True}, {'result':{**envelope,'error':'fixture error'}},
                    {'content':envelope['content']*2}, {'content':[{'type':'text','text':'not json'}]},
                    {'content':[{'type':'text','text':' '*65537}]}):
            result=self.prepare(raw)
            self.assertNotIn('71%',result['text']);self.assertIn('剩余未知',result['text'])

    def test_structured_error_cannot_hide_behind_top_level_quota(self):
        bare=json.loads(mcp_quota()['content'][0]['text'])
        for wrapped in ({**bare,'structuredContent':{'isError':True}}, {**bare,'result':{'error':'fixture error'}}):
            view=normalize_usage(UsageObservation('fixture-owner','fixture-account',100,wrapped),self.binding,now=100)
            self.assertEqual(view.status,'invalid_result')
            self.assertNotIn('71%',self.prepare(wrapped)['text'])

    def test_old_observation_stays_stale_and_does_not_gain_freshness_from_envelope(self):
        view=normalize_usage(UsageObservation('fixture-owner','fixture-account',100,mcp_quota()),self.binding,now=401)
        self.assertEqual(view.status,'stale');self.assertEqual(view.windows,())


if __name__=='__main__':unittest.main()
