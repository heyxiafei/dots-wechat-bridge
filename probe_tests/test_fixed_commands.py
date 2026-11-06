"""Offline fixed-command and idempotent final integration; no sending."""
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock
from probe.probe import Probe
from probe.weixin import WeixinError
from probe.fixed_commands import (COMMAND_CONTEXT_REASONS, capture_command_context,
                                  inspect_command_context, prepare_weekly_usage,
                                  OFFICIAL_PROTOCOL_FIELDS, OFFICIAL_PROTOCOL_FIELD_CODES,
                                  unknown_command_item_diagnostic)
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

    def test_official_text_item_metadata_preserves_help_and_quota_hints(self):
        metadata = dict(create_time_ms=NOW*1000, update_time_ms=NOW*1000,
                        is_completed=True, msg_id='fixture-text-item-id')
        variants = [{key: value} for key, value in metadata.items()] + [metadata]
        for text in ('/help', '额度', '查额度'):
            for extra in variants:
                with self.subTest(text=text, fields=sorted(extra)):
                    items = [dict(type=1, text_item={'text': text}, **extra)]
                    read = self.p.weixin.read_message(self.ingest(text, items))
                    self.assertTrue(read['inbound_completeness']['command_context_v1'])
                    hint = read['fixed_command']
                    self.assertEqual(hint['kind'], 'help' if text == '/help' else 'weekly_usage')
                    self.assertFalse(hint['automatic_send'])
                    self.assertTrue(hint['owner_intent_verification_required'])
                    self.assertEqual(hint['forwarding_provenance'], 'not_provided_by_protocol')
                    if text == '/help':
                        self.assertEqual(hint['prepared_text'], read['help_menu']['text'])
                        self.assertIn('额度：查询当前 Codex 周额度', hint['prepared_text'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM pings').fetchone()[0], 0)

    def test_official_metadata_does_not_accept_invalid_or_authority_fields(self):
        invalid = [
            {key: value} for key in ('create_time_ms', 'update_time_ms')
            for value in (None, True, -1, 10**15+1, 10**1000, float('nan'), float('inf'), '1000', {})
        ] + [
            {'is_completed': value} for value in (None, False, 0, 1, 'true', {})
        ] + [{'msg_id': value} for value in (None, '', 1, True, 'x'*257, {})]
        invalid += [{key: {}} for key in ('ref_msg', 'quote', 'forward', 'forwarded',
                                         'forward_info', 'unknown_field')]
        for extra in invalid:
            with self.subTest(fields=sorted(extra)):
                self.assertFalse(capture_command_context({'item_list': [dict(
                    type=1, text_item={'text': '/help'}, **extra)]}))
        for key in ('ref_msg', 'quote', 'quoted_message', 'forward', 'forwarded', 'forward_info'):
            self.assertFalse(capture_command_context({key: False, 'item_list': [dict(
                type=1, text_item={'text': '/help'}, is_completed=True)]}))
        self.assertFalse(capture_command_context({'item_list': [dict(type=1,
            text_item={'text': '/help', 'unknown_field': None}, is_completed=True)]}))

    def test_duplicate_does_not_promote_stored_false_command_context(self):
        mid = self.ingest('/help', items=[dict(type=1, text_item={'text': '/help'}, unknown_field=None)])
        before = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
        self.assertIsNone(self.p.weixin.read_message(mid)['help_menu'])
        self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [dict(
            message_id=str(self.n), from_user_id='fixture-owner', to_user_id='fixture-bot',
            message_type=1, message_state=2, create_time_ms=NOW*1000,
            context_token='fixture-context', item_list=[dict(type=1,
                text_item={'text': '/help'}, is_completed=True, msg_id='fixture-item')])]})
        after = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertIsNone(self.p.weixin.read_message(mid)['help_menu'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_new_ingress_reason_is_static_and_visible_in_both_projections(self):
        private = 'synthetic-private-input-not-for-diagnostics'
        mid = self.ingest('/help', items=[dict(type=1, text_item={'text': '/help'}, **{private: private})])
        for projection in ('full', 'reply'):
            result = self.p.rpc(dict(jsonrpc='2.0', id='diagnostic-fixture', method='tools/call',
                params=dict(name='read_weixin_message', arguments=dict(message_id=mid, projection=projection))))
            source = result['result']['structuredContent']
            capture = source['inbound_completeness']
            self.assertIs(capture['command_context_v1'], False)
            self.assertEqual(capture['command_context_reason_v1'], 'item_unknown_fields')
            diagnostic = capture['command_item_diagnostic_v1']
            self.assertEqual(diagnostic['fields'], [])
            self.assertTrue(diagnostic['unlisted'])
            self.assertFalse(diagnostic['truncated'])
            self.assertNotIn(private, json.dumps(capture))
            self.assertIsNone(source['help_menu'])
            self.assertIsNone(source['fixed_command'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_duplicate_never_backfills_legacy_diagnostic_or_promotes_false(self):
        mid = self.ingest('/help')
        raw = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
        capture = json.loads(raw)
        capture.pop('command_context_reason_v1')
        capture.pop('command_item_diagnostic_v1', None)
        capture['command_context_v1'] = False
        before = json.dumps(capture)
        with self.p.db:
            self.p.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?', (before, mid))
        self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [dict(message_id=str(self.n),
            from_user_id='fixture-owner', to_user_id='fixture-bot', message_type=1,
            message_state=2, create_time_ms=NOW*1000, context_token='fixture-context',
            item_list=[dict(type=1, text_item={'text': '/help'})])]})
        after = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
        self.assertEqual(before, after)
        source = self.p.weixin.read_message(mid)
        self.assertNotIn('command_context_reason_v1', source['inbound_completeness'])
        self.assertNotIn('command_item_diagnostic_v1', source['inbound_completeness'])
        self.assertIsNone(source['fixed_command'])

    def test_reason_alone_never_enables_command_or_ignores_boolean(self):
        for boolean in (False, None):
            mid = self.ingest('/help')
            raw = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
            capture = json.loads(raw)
            if boolean is None:
                capture.pop('command_context_v1')
            else:
                capture['command_context_v1'] = boolean
            capture['command_context_reason_v1'] = 'standalone_text_shape'
            with self.p.db:
                self.p.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?', (json.dumps(capture), mid))
            source = self.p.weixin.read_message(mid)
            self.assertIsNone(source['help_menu'])
            self.assertIsNone(source['fixed_command'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_reason_and_inbox_cursor_share_existing_transaction(self):
        cursor = self.p.db.execute('SELECT cursor FROM weixin_accounts').fetchone()[0]
        with self.p.db:
            self.p.db.execute("CREATE TRIGGER fixture_abort_manifest BEFORE INSERT ON weixin_inbound_manifests BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        msg = dict(message_id='fixture-atomic-reason', from_user_id='fixture-owner',
            to_user_id='fixture-bot', message_type=1, message_state=2,
            create_time_ms=NOW*1000, context_token='fixture-context',
            item_list=[dict(type=1, text_item={'text': '/help'})])
        with self.assertRaises(sqlite3.IntegrityError):
            self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [msg],
                'get_updates_buf': 'fixture-new-cursor'})
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_inbound_manifests').fetchone()[0], 0)
        self.assertEqual(self.p.db.execute('SELECT cursor FROM weixin_accounts').fetchone()[0], cursor)

    def test_closed_diagnostic_only_for_new_owner_supported_text_commands(self):
        for text, expected in (('/help', True), ('额度', True), ('查额度', True),
                               ('普通聊天', False), ('额度并发邮件', False)):
            mid = self.ingest(text, items=[dict(type=1, text_item={'text': text},
                tool_call_start_item=None, is_completed=False)])
            source = self.p.weixin.read_message(mid)
            capture = source['inbound_completeness']
            self.assertFalse(capture['command_context_v1'])
            self.assertEqual(capture['command_context_reason_v1'], 'item_unknown_fields')
            self.assertEqual('command_item_diagnostic_v1' in capture, expected)
            if expected:
                self.assertEqual(capture['command_item_diagnostic_v1']['fields'],
                    [{'code': 'official_tool_call_start_item', 'json_type': 'null'}])
                self.assertEqual(capture['command_item_diagnostic_v1']['metadata']['is_completed'], 'false')
            self.assertIsNone(source['fixed_command'])
        for extra in ({'forwarded': False}, {'quote': None}):
            mid = self.ingest('/help', items=[dict(type=1, text_item={'text': '/help'},
                tool_call_result_item={})], **extra)
            self.assertNotIn('command_item_diagnostic_v1', self.p.weixin.read_message(mid)['inbound_completeness'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_duplicate_never_replaces_or_backfills_unknown_item_diagnostic(self):
        mid = self.ingest('/help', items=[dict(type=1, text_item={'text': '/help'}, private_unknown=None)])
        raw = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
        for missing in (False, True):
            capture = json.loads(raw)
            if missing:
                capture.pop('command_item_diagnostic_v1')
            before = json.dumps(capture)
            with self.p.db:
                self.p.db.execute('UPDATE weixin_inbound_manifests SET metadata_json=? WHERE message_id=?', (before, mid))
            self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [dict(message_id=str(self.n),
                from_user_id='fixture-owner', to_user_id='fixture-bot', message_type=1,
                message_state=2, create_time_ms=NOW*1000, context_token='fixture-context',
                item_list=[dict(type=1, text_item={'text': '/help'}, tool_call_start_item=None)])]})
            after = self.p.db.execute('SELECT metadata_json FROM weixin_inbound_manifests WHERE message_id=?', (mid,)).fetchone()[0]
            self.assertEqual(before, after)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

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

class CommandContextDiagnosticTests(unittest.TestCase):
    @staticmethod
    def message(item=None, **extra):
        return dict(item_list=[item if item is not None else {'type': 1, 'text_item': {'text': '/help'}}], **extra)

    def test_every_diagnostic_is_a_static_bounded_first_failure_code(self):
        base = {'type': 1, 'text_item': {'text': '/help'}}
        cases = [
            (self.message(), True, 'standalone_text_shape'),
            ([], False, 'message_not_object'),
            (self.message(forward=False), False, 'message_forward_marker_false'),
            ({'item_list': {}}, False, 'item_list_type_invalid'),
            ({'item_list': []}, False, 'item_count_not_one'),
            ({'item_list': [None]}, False, 'item_not_object'),
            (self.message({'type': 1}), False, 'item_required_fields_missing'),
            (self.message(dict(base, unknown_private_field='private-value')), False, 'item_unknown_fields'),
            (self.message(dict(base, type=True)), False, 'item_content_type_invalid'),
            (self.message(dict(base, type=2)), False, 'item_not_text'),
            (self.message(dict(base, create_time_ms=True)), False, 'item_time_type_invalid'),
            (self.message(dict(base, update_time_ms=10**1000)), False, 'item_time_value_invalid'),
            (self.message(dict(base, is_completed=1)), False, 'item_completion_type_invalid'),
            (self.message(dict(base, is_completed=False)), False, 'item_not_completed'),
            (self.message(dict(base, msg_id=1)), False, 'item_id_type_invalid'),
            (self.message(dict(base, msg_id='')), False, 'item_id_size_invalid'),
            (self.message(dict(base, text_item=[])), False, 'text_item_not_object'),
            (self.message(dict(base, text_item={})), False, 'text_item_required_field_missing'),
            (self.message(dict(base, text_item={'text': '/help', 'private-key': 'private-value'})), False, 'text_item_unknown_fields'),
            (self.message(dict(base, text_item={'text': 1})), False, 'text_type_invalid'),
            (self.message(dict(base, text_item={'text': 'x'*129})), False, 'text_too_long'),
        ]
        marker_categories = {
            'ref_msg': 'reference', 'quote': 'quote', 'quoted_message': 'quoted_message',
            'forward': 'forward', 'forwarded': 'forwarded', 'forward_info': 'forward_info',
        }
        for key, category in marker_categories.items():
            for value, suffix in ((None, 'null'), (False, 'false'), ({'synthetic-private-key': 'private-value'}, 'other')):
                cases.append((self.message(**{key: value}), False,
                              'message_' + category + '_marker_' + suffix))
        for key, category in (('ref_msg', 'reference'), ('image_item', 'image'),
                              ('voice_item', 'voice'), ('file_item', 'file'), ('video_item', 'video')):
            # Mere presence is diagnostic and refused, including null/false.
            for value in (None, False, {'synthetic-private-key': 'private-value'}):
                cases.append((self.message(dict(base, **{key: value})), False,
                              'item_' + category + '_field_present'))
        seen = set()
        for message, expected, reason in cases:
            with self.subTest(reason=reason):
                decision = inspect_command_context(message)
                self.assertEqual(decision, (expected, reason))
                self.assertIs(capture_command_context(message), decision[0])
                self.assertIs(type(decision[0]), bool)
                self.assertTrue(reason.isascii())
                self.assertLessEqual(len(reason), 48)
                self.assertLessEqual(len(json.dumps(decision)), 64)
                seen.add(decision[1])
        self.assertEqual(seen, COMMAND_CONTEXT_REASONS)

    def test_reason_never_contains_any_input_value_or_unknown_key(self):
        sentinel = 'synthetic-private-input-not-for-diagnostics'
        base = {'type': 1, 'text_item': {'text': sentinel}}
        for message in (self.message(base, context_token=sentinel, message_id=sentinel),
                        self.message(dict(base, **{sentinel: sentinel})),
                        self.message(dict(base, text_item={'text': sentinel, sentinel: sentinel})),
                        self.message(dict(base, msg_id=sentinel)),
                        self.message(base, forward_info={sentinel: sentinel})):
            decision = inspect_command_context(message)
            self.assertIn(decision[1], COMMAND_CONTEXT_REASONS)
            self.assertNotIn(sentinel, json.dumps(decision))


class ClosedItemDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.capture = dict(complete=True, source_item_count=1,
            parts=[dict(kind='text', state='captured', item_type=1)])

    def diagnose(self, extra, text='/help', capture=None):
        item = dict(type=1, text_item={'text': text}, **extra)
        message = {'item_list': [item]}
        decision = inspect_command_context(message)
        return decision, unknown_command_item_diagnostic(
            message, text, self.capture if capture is None else capture, decision)

    def test_catalog_is_fixed_unique_and_never_an_acceptance_allowlist(self):
        self.assertEqual(len(OFFICIAL_PROTOCOL_FIELDS), 87)
        self.assertEqual(tuple(sorted(set(OFFICIAL_PROTOCOL_FIELDS))), OFFICIAL_PROTOCOL_FIELDS)
        self.assertEqual(len(set(code for _, code in OFFICIAL_PROTOCOL_FIELD_CODES)), 87)
        for key in ('tool_call_start_item', 'tool_call_result_item', 'context_token', 'status'):
            decision, diagnostic = self.diagnose({key: None})
            self.assertEqual(decision, (False, 'item_unknown_fields'))
            self.assertEqual(diagnostic['fields'], [{'code': 'official_' + key, 'json_type': 'null'}])
            self.assertFalse(diagnostic['unlisted'])
            self.assertFalse(diagnostic['truncated'])

    def test_unlisted_keys_and_values_are_not_read_or_emitted(self):
        sentinel = 'synthetic-private-input-never-a-diagnostic'
        class NoUnknownRead(dict):
            def __getitem__(self, key):
                if key == sentinel:
                    raise AssertionError('unlisted value must not be accessed')
                return super().__getitem__(key)
        item = NoUnknownRead(type=1, text_item={'text': '/help'}, **{sentinel: sentinel})
        message = {'item_list': [item]}
        diagnostic = unknown_command_item_diagnostic(message, '/help', self.capture,
                                                     inspect_command_context(message))
        self.assertEqual(diagnostic['fields'], [])
        self.assertTrue(diagnostic['unlisted'])
        self.assertFalse(diagnostic['truncated'])
        self.assertNotIn(sentinel, json.dumps(diagnostic))
        self.assertEqual(set(diagnostic), {'version', 'fields', 'unlisted', 'truncated', 'metadata'})

    def test_eight_limit_constant_order_and_only_fixed_json_types(self):
        fields = ['status', 'message_id', 'context_token', 'client_id', 'bot_agent',
                  'base_info', 'aeskey', 'aes_key', 'bits_per_sample', 'channel_version']
        sentinel = 'synthetic-private-input-never-a-diagnostic'
        values = [None, False, 42, sentinel, [sentinel], {'secret': sentinel}, {}, [], True, 0]
        _, first = self.diagnose(dict(zip(fields, values)))
        _, second = self.diagnose(dict(reversed(list(zip(fields, values)))))
        self.assertEqual(first, second)
        self.assertEqual(len(first['fields']), 8)
        self.assertTrue(first['truncated'])
        self.assertFalse(first['unlisted'])
        self.assertEqual([x['code'] for x in first['fields']],
                         ['official_' + key for key in sorted(fields)[:8]])
        self.assertTrue({x['json_type'] for x in first['fields']} <=
                        {'null', 'boolean', 'number', 'string', 'array', 'object', 'non_json'})
        self.assertNotIn(sentinel, json.dumps(first))
        self.assertLessEqual(len(json.dumps(first)), 1600)
        _, both = self.diagnose(dict(zip(fields, values), **{sentinel: sentinel}))
        self.assertTrue(both['unlisted'])
        self.assertTrue(both['truncated'])

    def test_metadata_states_report_all_checks_without_changing_primary_failure(self):
        variants = [
            ({}, dict(create_time_ms='absent', update_time_ms='absent', is_completed='absent', msg_id='absent')),
            (dict(create_time_ms=0, update_time_ms=10**15, is_completed=True, msg_id='synthetic-id'),
             dict(create_time_ms='valid', update_time_ms='valid', is_completed='true', msg_id='valid')),
            (dict(create_time_ms=True, update_time_ms=10**1000, is_completed=False, msg_id=1),
             dict(create_time_ms='type_invalid', update_time_ms='value_invalid', is_completed='false', msg_id='type_invalid')),
            (dict(create_time_ms=float('nan'), update_time_ms='123', is_completed=1, msg_id='x'*257),
             dict(create_time_ms='value_invalid', update_time_ms='type_invalid', is_completed='type_invalid', msg_id='value_invalid')),
        ]
        for values, expected in variants:
            decision, diagnostic = self.diagnose(dict(tool_call_start_item=None, **values))
            self.assertEqual(decision, (False, 'item_unknown_fields'))
            self.assertEqual(diagnostic['metadata'], expected)
            self.assertEqual(diagnostic['fields'], [{'code': 'official_tool_call_start_item', 'json_type': 'null'}])

    def test_diagnostic_scope_excludes_other_failures_or_incomplete_noncommands(self):
        for text in ('普通聊天', '/help 内容', '额度并发邮件', 'x'*129):
            self.assertIsNone(self.diagnose({'status': None}, text=text)[1])
        for capture in (dict(self.capture, complete=False), dict(self.capture, source_item_count=2),
                        dict(self.capture, parts=[dict(kind='image', state='captured', item_type=2)])):
            self.assertIsNone(self.diagnose({'status': None}, capture=capture)[1])
        for extra in ({}, {'is_completed': False}, {'ref_msg': None}, {'image_item': None}):
            self.assertIsNone(self.diagnose(extra)[1])


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
