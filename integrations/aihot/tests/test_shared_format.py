"""Synthetic contract checks: new format, old commitments, and HTML safety."""
import copy
import hashlib
import html
import json
import sys
import unittest
from .fixtures import OPERATOR
from pathlib import Path
from .test_dual_producer import EID, T, source_event, source_payload
from .test_monitor import m
from ..notice_relay.text_b import build_display, format_b, RICH_STYLE
from ..notice_relay.relay import email_payload, validate_intent, wechat_text
from probe.aihot_notice_consumer import relay as consumer

class SharedFormatTests(unittest.TestCase):
    def intent(self, event, rich=True):
        payload=source_payload(T+600,[event]);checked,_,_=m.envelope(payload,T+600)
        view=m.notification_view(payload,checked)[EID];self.assertTrue(view['eligible'])
        args={'style':RICH_STYLE} if rich else {}
        display=build_display(payload,event,view,**args)
        return dict(event_id=EID,stage=view['stage'],source_at=view['source_at'],expires_at=view['expires_at'],
                    text=view['text'],link=view['text'].splitlines()[-1],eligible=True,display=display)

    def test_announced_and_confirmed_share_full_fields(self):
        for stage in ('announced','confirmed'):
            e=source_event(confirmed=T+100 if stage=='confirmed' else None)
            e['posts'][0]['originalText']="Original <raw> & 'quoted'\nnext line"
            e['posts'][0]['text']='中文 <翻译> & 更多'
            intent=self.intent(e);validate_intent(intent);consumer.validate_intent(intent)
            wx=wechat_text(intent);mail=email_payload(intent, OPERATOR);body=mail['payload']['body']['content']
            self.assertTrue(wx.startswith('🔔 Codex 重置'+('预告' if stage=='announced' else '确认')))
            self.assertIn(e['posts'][0]['originalText'],wx);self.assertIn(e['posts'][0]['text'],wx)
            self.assertIn('发布时间：',wx);self.assertIn('来源人物：Tibo',wx);self.assertIn(intent['link'],wx)
            self.assertEqual(mail['subject'],'🔔 Codex 重置'+('预告' if stage=='announced' else '确认'))
            self.assertNotIn('AIHOT-',mail['subject']);self.assertNotIn('<raw>',body)
            self.assertIn('&lt;raw&gt; &amp; &#x27;quoted&#x27;',body)
            self.assertEqual(html.unescape(body[3:-4].replace('<br>','\n')),wx)
            self.assertEqual(mail,consumer.email_payload(intent, OPERATOR))

    def test_absent_original_and_translation_are_not_invented(self):
        e=source_event();e['posts'][0]['originalText']=None;e['posts'][0]['text']=None
        text=wechat_text(self.intent(e))
        self.assertEqual(text.count('未提供或未核验'),2)
        self.assertNotIn('Synthetic forecast',text)

    def test_existing_legacy_payload_is_byte_for_byte_stable(self):
        old=self.intent(source_event(),rich=False);out=email_payload(old, OPERATOR)
        expected='<p>'+html.escape(old['text']).replace('\n','<br>')+'</p>'
        marker=hashlib.sha256((EID+'/announced').encode()).hexdigest()[:16]
        self.assertEqual(out['subject'],'AIHOT-'+marker+' · Codex 新重置预告')
        self.assertEqual(out['payload']['body']['content'],expected)
        self.assertEqual(out,consumer.email_payload(old, OPERATOR))
        legacy=copy.deepcopy(old);legacy.pop('display');self.assertEqual(email_payload(legacy, OPERATOR),out)

    def test_changed_stage_link_or_identity_rejected(self):
        i=self.intent(source_event());wrong=copy.deepcopy(i);wrong['display']['stage']='confirmed'
        for validate in (validate_intent,consumer.validate_intent):
            with self.assertRaises(Exception):validate(wrong)

    def test_display_unavailable_cannot_make_email_payload(self):
        i=self.intent(source_event());i['display']={'version':1,'style':'rich-unavailable-v2'}
        validate_intent(i);consumer.validate_intent(i)
        with self.assertRaises(Exception): email_payload(i, OPERATOR)
        with self.assertRaises(Exception): consumer.email_payload(i, OPERATOR)

if __name__=='__main__':unittest.main()
