"""Offline fixed-command and idempotent final integration; no sending."""
import json
import tempfile
import unittest
from unittest.mock import Mock
from probe.probe import Probe
from probe.weixin import WeixinError
from probe.fixed_commands import prepare_weekly_usage
from probe.usage_display import UsageBinding, UsageObservation, CreditInfo

NOW=1900000000.
BINDING=UsageBinding('fixture-owner','fixture-account',verified=True)

def observed(payload, at=NOW, credits=None, binding=BINDING):
    return UsageObservation(binding.owner_ref,binding.account_ref,at,payload,credits=credits)

def payload(primary=None,secondary=None):
    return {'rateLimitsByLimitId':{'codex':{'limitId':'codex','primary':primary,'secondary':secondary}}}

class FixedCommandIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.p=Probe(self.tmp.name,clock=lambda:NOW);self.addCleanup(self.p.close)
        self.p.weixin.bind_confirmed({'status':'confirmed','ilink_bot_id':'fixture-bot','ilink_user_id':'fixture-owner','bot_token':'fixture-token'})
        self.n=0
    def ingest(self,text,items=None,**extra):
        self.n+=1;mid=str(self.n)
        msg={'message_id':mid,'from_user_id':'fixture-owner','to_user_id':'fixture-bot','message_type':1,'message_state':2,'create_time_ms':NOW*1000,'context_token':'fixture-context','item_list':items if items is not None else [{'type':1,'text_item':{'text':text}}]}
        msg.update(extra)
        self.p.weixin.ingest(self.p.weixin.account(),{'msgs':[msg]})
        return self.p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?',(mid,)).fetchone()[0]
    def test_help_prepared_from_existing_menu_never_sends(self):
        mid=self.ingest('/help');read=self.p.weixin.read_message(mid);hint=read['fixed_command']
        self.assertEqual(hint['kind'],'help');self.assertEqual(hint['prepared_text'],read['help_menu']['text'])
        self.assertFalse(hint['automatic_send']);self.assertTrue(hint['owner_intent_verification_required'])
        self.assertEqual(self.p.db.execute('select count(*) from weixin_outbox').fetchone()[0],0)
    def test_weekly_whitelist_and_unknown_fallback(self):
        mid=self.ingest('查一下我的周额度还剩多少');hint=self.p.weixin.read_message(mid)['fixed_command']
        self.assertEqual(hint['kind'],'weekly_usage');self.assertTrue(hint['official_reader_required']);self.assertFalse(hint['automatic_send'])
        mid=self.ingest('查询我的周额度并顺便发邮件');self.assertIsNone(self.p.weixin.read_message(mid)['fixed_command'])

    def test_short_quota_owner_requests_remain_suggestions_only(self):
        for phrase in ('额度', '查额度', '查一下额度'):
            with self.subTest(phrase=phrase):
                mid=self.ingest(phrase);hint=self.p.weixin.read_message(mid)['fixed_command']
                self.assertEqual(hint['kind'],'weekly_usage')
                self.assertTrue(hint['official_reader_required'])
                self.assertFalse(hint['automatic_send'])
                self.assertTrue(hint['owner_intent_verification_required'])
        self.assertEqual(self.p.db.execute('select count(*) from weixin_outbox').fetchone()[0],0)

    def test_short_quota_quote_attachment_and_mixed_requests_do_not_match(self):
        for text in ('额度','查额度'):
            quote=[{'type':1,'text_item':{'text':text},'ref_msg':{}}]
            self.assertIsNone(self.p.weixin.read_message(self.ingest(text,quote))['fixed_command'])
            mixed=[{'type':1,'text_item':{'text':text}},{'type':2,'image_item':{}}]
            self.assertIsNone(self.p.weixin.read_message(self.ingest('',mixed))['fixed_command'])
        for text in ('信用卡额度','额度并发邮件','请解释什么是额度','/quota'):
            self.assertIsNone(self.p.weixin.read_message(self.ingest(text))['fixed_command'])

    def test_short_quota_existing_final_remains_immutable(self):
        mid=self.ingest('额度');self.p.weixin.queue_reply(mid,'fixture prior final')
        self.assertEqual(self.p.weixin.read_message(mid)['fixed_command']['state'],'already_committed')
        with self.assertRaises(WeixinError):self.p.weixin.queue_reply(mid,'fixture different final')
        self.assertEqual(self.p.db.execute('select count(*) from weixin_outbox').fetchone()[0],1)

    def test_short_quota_with_explicit_forward_marker_is_not_a_command(self):
        mid=self.ingest('额度',forwarded=True);hint=self.p.weixin.read_message(mid)['fixed_command']
        self.assertIsNone(hint)
        self.assertEqual(self.p.db.execute('select count(*) from weixin_outbox').fetchone()[0],0)
    def test_quote_attachment_and_uncaptured_fallback(self):
        quote=[{'type':1,'text_item':{'text':'/help'},'ref_msg':{}}]
        self.assertIsNone(self.p.weixin.read_message(self.ingest('/help',quote))['fixed_command'])
        mixed=[{'type':1,'text_item':{'text':'查一下我的周额度'}},{'type':2,'image_item':{}}]
        self.assertIsNone(self.p.weixin.read_message(self.ingest('',mixed))['fixed_command'])
        mid=self.ingest('/help')
        with self.p.db:self.p.db.execute('DELETE FROM weixin_quote_captures WHERE message_id=?',(mid,))
        self.assertIsNone(self.p.weixin.read_message(mid)['fixed_command'])
    def test_forward_provenance_unknown_never_automatically_executes(self):
        # A clean protocol shape still cannot establish forwarding provenance.
        mid=self.ingest('/help')
        hint=self.p.weixin.read_message(mid)['fixed_command']
        self.assertEqual(hint['forwarding_provenance'],'not_provided_by_protocol')
        self.assertFalse(hint['automatic_send']);self.assertTrue(hint['owner_intent_verification_required'])
        self.assertEqual(self.p.db.execute('select count(*) from weixin_outbox').fetchone()[0],0)

    def test_explicit_reference_or_unknown_text_shape_is_not_a_command(self):
        for text in ('额度', '/help'):
            for key in ('ref_msg', 'quote', 'quoted_message', 'forwarded', 'forward_info'):
                for marker in ({}, False, None):
                    with self.subTest(text=text, key=key, marker=marker):
                        read=self.p.weixin.read_message(self.ingest(text, **{key:marker}))
                        self.assertIsNone(read['fixed_command'])
                        self.assertIsNone(read['help_menu'])
            for items in ([{'type':1,'text_item':{'text':text,'quote':'fixture'}}],
                          [{'type':1,'text_item':{'text':text},'unknown_field':None}],
                          [{'type':True,'text_item':{'text':text}}],
                          [{'type':1.0,'text_item':{'text':text}}]):
                with self.subTest(text=text, items=items):
                    self.assertIsNone(self.p.weixin.read_message(self.ingest(text,items))['fixed_command'])
        self.assertEqual(self.p.db.execute('select count(*) from weixin_outbox').fetchone()[0],0)

    def test_missing_false_or_nonboolean_capture_never_enables_command(self):
        for value in (None, False, 0, 1, 'true', {}, []):
            with self.subTest(value=value):
                mid=self.ingest('/help')
                raw=self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?',(mid,)).fetchone()[0]
                capture=json.loads(raw)
                if value is None:
                    capture.pop('command_context_v1')
                else:
                    capture['command_context_v1']=value
                with self.p.db:
                    self.p.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?',(json.dumps(capture),mid))
                read=self.p.weixin.read_message(mid)
                self.assertIsNone(read['fixed_command']);self.assertIsNone(read['help_menu'])
        with self.p.db:
            self.p.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?',('bad json',mid))
        self.assertIsNone(self.p.weixin.read_message(mid)['fixed_command'])

    def test_unknown_send_not_requeued_by_command_read(self):
        mid=self.ingest('额度');self.p.weixin.queue_reply(mid,'fixture committed final')
        with self.p.db:
            self.p.db.execute("UPDATE weixin_outbox SET status='delivery_unknown',attempts=1 WHERE message_id=?",(mid,))
        self.assertEqual(self.p.weixin.read_message(mid)['fixed_command']['state'],'already_committed')
        row=self.p.db.execute('SELECT status,attempts FROM weixin_outbox WHERE message_id=?',(mid,)).fetchone()
        self.assertEqual(tuple(row),('delivery_unknown',1))
    def test_duplicate_and_model_final_use_same_immutable_slot(self):
        mid=self.ingest('/help');first=self.p.weixin.read_message(mid)['fixed_command']['prepared_text']
        self.p.weixin.queue_reply(mid,first);self.assertEqual(self.p.weixin.read_message(mid)['fixed_command']['state'],'already_committed')
        self.p.weixin.queue_reply(mid,first)
        with self.assertRaises(WeixinError):self.p.weixin.queue_reply(mid,'different model final')
        self.assertEqual(self.p.db.execute('select count(*) from weixin_outbox').fetchone()[0],1)
    def test_existing_ack_prevents_command_final(self):
        mid=self.ingest('/help');self.p.weixin.queue_reply_phase(mid,'ack','fixture ack')
        self.assertEqual(self.p.weixin.read_message(mid)['fixed_command']['state'],'already_committed')

class WeeklyPreparationTests(unittest.TestCase):
    def test_real_shape_selects_week_and_verified_credits(self):
        p=payload({'usedPercent':12,'windowDurationMins':300,'resetsAt':NOW+100}, {'usedPercent':21,'windowDurationMins':10080,'resetsAt':NOW+1000})
        reader=Mock(return_value=observed(p,credits=CreditInfo(balance='0',mapping_verified=True)))
        result=prepare_weekly_usage(reader,BINDING,now=NOW);reader.assert_called_once_with(BINDING)
        self.assertEqual(result['status'],'reported');self.assertIn('周额度：剩余79%，已用21%',result['text']);self.assertIn('💳 信用点余额：0',result['text']);self.assertIn('北京时间',result['text']);self.assertNotIn('88%',result['text'])
    def test_unavailable_failure_and_missing_data_explicitly_unknown(self):
        for reader in (None,Mock(side_effect=RuntimeError('private details')),Mock(return_value=observed(payload()))):
            result=prepare_weekly_usage(reader,BINDING,now=NOW)
            for label in ('📊 Codex 周额度：剩余未知，已用未知','🕒 读取：','🔄 下次重置：未知','💳 信用点余额：未知'):
                self.assertIn(label,result['text'])
            self.assertNotIn('private details',result['text'])
    def test_stale_or_wrong_account_cannot_be_current(self):
        p=payload({'usedPercent':21,'windowDurationMins':10080,'resetsAt':NOW+1000})
        for obs in (observed(p,NOW-301,credits=CreditInfo(balance='0',mapping_verified=True)),observed(p,binding=UsageBinding('other','other',True))):
            result=prepare_weekly_usage(lambda _:obs,BINDING,now=NOW)
            self.assertIn('剩余未知',result['text']);self.assertNotIn('79%',result['text']);self.assertNotIn('余额：0',result['text'])
    def test_unverified_binding_never_calls_reader_and_ambiguous_week_unknown(self):
        reader=Mock();r=prepare_weekly_usage(reader,UsageBinding('a','b',False),now=NOW);reader.assert_not_called();self.assertEqual(r['status'],'binding_unverified')
        w={'usedPercent':20,'windowDurationMins':10080,'resetsAt':NOW+1000};r=prepare_weekly_usage(lambda _:observed(payload(w,w)),BINDING,now=NOW);self.assertIn('剩余未知',r['text'])
