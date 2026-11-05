"""Offline source-to-both-consumer compatibility, entirely synthetic."""
import copy
import sys
import unittest
import tempfile
from pathlib import Path
from .test_dual_producer import EID, T, source_event, source_payload
from .test_monitor import iso, m
from ..notice_relay.text_b import build_display, format_b
from ..notice_relay.relay import validate_intent, Relay
from probe.aihot_notice_consumer import relay as bridge_relay

class TextBContractTests(unittest.TestCase):
    def make(self, event):
        payload = source_payload(T + 600, [event])
        checked, _, _ = m.envelope(payload, T + 600)
        view = m.notification_view(payload, checked)[EID]
        self.assertTrue(view['eligible'])
        display = build_display(payload, event, view)
        intent = dict(event_id=EID, stage=view['stage'], source_at=view['source_at'],
                      expires_at=view['expires_at'], text=view['text'],
                      link=view['text'].splitlines()[-1], eligible=True, display=display)
        validate_intent(intent);bridge_relay.validate_intent(intent)
        return intent

    def test_today_unknown_scope_and_estimate_have_requested_style(self):
        e = source_event()
        e['posts'][0]['originalText'] = 'A reset will happen today.'
        e['presentation'].update(scopeKnown=False, scopeLabel=None, timeInferred=True, displayLabel='待生效')
        e['schedule'].update(precision='day')
        e['estimate'] = {'from': iso(T + 600), 'through': iso(T + 3600), 'label':'estimate'}
        intent = self.make(e); text = format_b(intent['display'])
        self.assertTrue(text.startswith('📣 Codex 今天有重置预告'))
        self.assertIn('⏳ AI Hot 预计：今天 ', text)
        self.assertIn('原帖只说“今天”，没有给具体时间，适用范围也未明确', text)
        self.assertIn('预计窗口来自 AI Hot', text)
        self.assertEqual(text, bridge_relay.wechat_text(intent))

    def test_missing_estimate_does_not_invent_window_or_scope(self):
        e = source_event();e['presentation'].update(scopeKnown=False, scopeLabel=None)
        text = format_b(self.make(e)['display'])
        self.assertNotIn('AI Hot 预计：', text)
        self.assertIn('适用范围也未明确', text)
        self.assertIn('原帖发布：', text)

    def test_explicit_source_time_is_not_described_as_missing(self):
        e = source_event();e['posts'][0]['originalText'] = 'Reset today at 14:00 UTC.'
        e['presentation']['timeInferred'] = False
        text = format_b(self.make(e)['display'])
        self.assertNotIn('没有给具体时间', text)
        self.assertIn('14:00 UTC', text)

    def test_matching_original_is_required_in_both_consumers(self):
        intent = self.make(source_event());bad=copy.deepcopy(intent)
        bad['display']['link'] = 'https://x.com/thsottiaux/status/1900000000000000002'
        for validate in (validate_intent,bridge_relay.validate_intent):
            with self.assertRaises(Exception): validate(bad)

    def test_producer_to_bridge_claim_and_restart_preserve_identity(self):
        intent = self.make(source_event())
        with tempfile.TemporaryDirectory() as directory:
            producer = Relay(Path(directory), now_callable=lambda:T + 601)
            self.assertEqual(producer.baseline([], T)['result'], 'captured')
            self.assertEqual(producer.sync([intent], T + 600)['result'], 'captured')
            consumer = bridge_relay.Relay(Path(directory), now_callable=lambda:T + 601)
            claim = consumer.claim(EID, 'announced', 'wechat', T + 601)
            self.assertEqual(claim['result'], 'prepared')
            self.assertTrue(claim['payload']['text'].startswith('📣 Codex'))
            receipt = dict(send_id='notice_'+'a'*32, status='accepted', accepted=True,
                           outgoing_text=claim['payload']['text'], delivered=None,
                           delivery_receipt_available=False)
            self.assertEqual(consumer.record(EID, 'announced', 'wechat', receipt, T + 602)['state'], 'accepted')
            restarted = bridge_relay.Relay(Path(directory), now_callable=lambda:T + 603)
            self.assertNotEqual(restarted.claim(EID, 'announced', 'wechat', T + 603)['result'], 'prepared')

    def test_legacy_text_is_readable_by_both_consumers(self):
        intent = self.make(source_event());intent.pop('display')
        validate_intent(intent);bridge_relay.validate_intent(intent)
        self.assertEqual(bridge_relay.wechat_text(intent), intent['text'])

if __name__ == '__main__': unittest.main()
