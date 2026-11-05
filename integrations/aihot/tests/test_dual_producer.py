#!/usr/bin/python3
"""Offline tests for the optional capture-only dual-channel producer.

No provider, sender, MCP, credential, service or real network access is permitted.
Each test uses its own disposable data directory.
"""
import builtins
import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from .fixtures import OPERATOR
from unittest import mock

from .test_monitor import SOURCE, T, activity, event, iso, m, payload

sys.dont_write_bytecode = True
EID = 'reset-1900000000000000001-1-1'
OLD_CONFIRMED = 'reset-1900000000000000002-1-1'
LINK = 'https://x.com/thsottiaux/status/1900000000000000001'
CONFIRM_LINK = 'https://x.com/thsottiaux/status/1900000000000000002'


def source_event(eid=EID, created=T + 10, updated=T + 20, confirmed=None):
    e = event(eid, created=created, updated=updated)
    e['posts'][0].update(id='1900000000000000001', url=LINK)
    if confirmed is not None:
        e.update(status='confirmed', confirmedAt=iso(confirmed),
                 confirmationBasis='source_post', updatedAt=iso(confirmed + 1),
                 occurredOn='2026-10-18')
        e['presentation']['status'] = 'confirmed'
        e['posts'].insert(0, dict(id='1900000000000000002', publishedAt=iso(confirmed),
                                stage='confirmation', text='Synthetic confirmation',
                                originalText='Synthetic confirmation', url=CONFIRM_LINK))
    return e


def source_activity(eid=EID, action='announce', published=T + 10):
    a = activity(eid, action=action, published=published)
    a.update(id='1900000000000000002' if action == 'confirm' else '1900000000000000001',
             url=CONFIRM_LINK if action == 'confirm' else LINK)
    return a


def source_payload(now, events=(), activities=None):
    if activities is None:
        activities = [source_activity(e['id'],
                                     'confirm' if e['status'] == 'confirmed' else 'announce',
                                     m.stamp(e['confirmedAt'] if e['status'] == 'confirmed' else e['createdAt']))
                      for e in events]
    return payload(now, events, activities)


class DualProducerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aihot-dual-producer-test-')
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        m.toggle(self.data, True, now=T)
        self.ledger_path = self.data / 'dual-notifications.json'
        for target, name in ((m.urllib.request.OpenerDirector, 'open'),
                             (subprocess, 'Popen'), (os, 'system')):
            patch = mock.patch.object(target, name, side_effect=AssertionError(
                'network/sender/MCP/model execution forbidden in producer tests'))
            patch.start()
            self.addCleanup(patch.stop)
        for name in ('dual_toggle', 'dual_capture', 'dual_suspend', 'dual_status'):
            self.assertTrue(callable(getattr(m, name, None)), 'missing producer API: ' + name)

    def ledger(self):
        return json.loads(self.ledger_path.read_text())

    def channel(self, stage='announced', channel='wechat', eid=EID):
        return self.ledger()['entries'][eid + '/' + stage]['channels'][channel]

    def relay(self):
        return m.dual_package().Relay(self.data, now_callable=lambda: T + 601, operator_config=OPERATOR)

    def poll(self, now, body=None, code=200, transport=None, module=m):
        transport = transport or mock.Mock(return_value=(code, {'ETag': '"dual-fixture"'}, body))
        result = module.poll(self.data, now=now, transport=transport)
        self.assertFalse(result['dispatch_enabled'])
        state = json.loads((self.data / 'state.json').read_text())
        self.assertFalse(state['dispatch_enabled'])
        self.assertEqual(state['mode'], 'monitor-only')
        return result

    def pending(self):
        m.dual_toggle(self.data, True, now=T)
        self.poll(T, source_payload(T))
        self.poll(T + 600, source_payload(T + 600, [source_event()]))
        for name in ('wechat', 'email'):
            self.assertEqual(self.channel(channel=name)['state'], 'pending')

    def assert_claim_blocked(self, now=T + 1201):
        relay = self.relay()
        for name in ('wechat', 'email'):
            self.assertNotEqual(relay.claim(EID, 'announced', name, now)['result'], 'prepared')

    def no_relay_imports(self):
        original_import = builtins.__import__
        original_import_module = importlib.import_module
        def checked_import(name, *args, **kwargs):
            if name == 'integrations.aihot.notice_relay' or name.startswith('integrations.aihot.notice_relay.'):
                raise AssertionError('disabled producer must not import notice_relay')
            return original_import(name, *args, **kwargs)
        def checked_import_module(name, *args, **kwargs):
            if name == 'integrations.aihot.notice_relay' or name.startswith('integrations.aihot.notice_relay.'):
                raise AssertionError('disabled producer must not import notice_relay')
            return original_import_module(name, *args, **kwargs)
        return mock.patch.object(builtins, '__import__', side_effect=checked_import), \
            mock.patch.object(importlib, 'import_module', side_effect=checked_import_module)

    def test_default_off_does_not_import_package_or_create_dual_ledger(self):
        import_guard, module_guard = self.no_relay_imports()
        with import_guard, module_guard, mock.patch.object(m, 'dual_package', side_effect=AssertionError('disabled producer loaded package')):
            m.dual_status(self.data)
            with m.lock(self.data) as acquired:
                self.assertTrue(acquired)
                m.dual_capture(self.data, source_payload(T), T)
            self.poll(T, source_payload(T))
        self.assertFalse(self.ledger_path.exists())
        self.assertFalse((self.data / 'dual-control.json').exists())

    def test_explicit_off_does_not_load_package_or_create_dual_ledger(self):
        import_guard, module_guard = self.no_relay_imports()
        with import_guard, module_guard, mock.patch.object(m, 'dual_package', side_effect=AssertionError('disabled producer loaded package')):
            m.dual_toggle(self.data, False, now=T)
            self.poll(T, source_payload(T))
        self.assertEqual(json.loads((self.data / 'dual-control.json').read_text()), {'capture_enabled': False})
        self.assertFalse(self.ledger_path.exists())

    def test_enable_sets_only_capture_control_and_clears_cached_etag(self):
        self.poll(T, source_payload(T))
        state = json.loads((self.data / 'state.json').read_text())
        self.assertEqual(state['etag'], '"dual-fixture"')
        m.dual_toggle(self.data, True, now=T + 1)
        self.assertEqual(json.loads((self.data / 'dual-control.json').read_text()), {'capture_enabled': True})
        state = json.loads((self.data / 'state.json').read_text())
        self.assertIsNone(state['etag'])
        self.assertFalse(state['dispatch_enabled'])
        self.assertTrue(self.ledger_path.exists())
        self.assertFalse(self.ledger()['baseline_complete'])
        self.assertEqual(self.ledger()['entries'], {})

    def test_first_capture_suppresses_historical_announced_confirmed_for_both_channels(self):
        m.dual_toggle(self.data, True, now=T)
        events = [source_event(created=T - 1000, updated=T - 900),
                  source_event(OLD_CONFIRMED, created=T - 1000, confirmed=T - 800)]
        self.poll(T, source_payload(T, events))
        for eid, stage in ((EID, 'announced'), (OLD_CONFIRMED, 'confirmed')):
            for name in ('wechat', 'email'):
                self.assertEqual(self.channel(stage, name, eid)['state'], 'suppressed')

    def test_new_event_after_empty_baseline_creates_independent_two_channel_pending(self):
        self.pending()
        entry = self.ledger()['entries'][EID + '/announced']
        self.assertNotEqual(entry['channels']['wechat']['request_id'], entry['channels']['email']['request_id'])
        self.assertIsNone(entry['channels']['wechat']['provider_id'])
        self.assertIsNone(entry['channels']['email']['provider_id'])
        self.assertEqual(set(entry['channels']), {'wechat', 'email'})

    def test_capture_uses_separate_lock_while_original_monitor_lock_is_held(self):
        m.dual_toggle(self.data, True, now=T)
        with m.lock(self.data) as acquired:
            self.assertTrue(acquired)
            m.dual_capture(self.data, source_payload(T), T)
        self.assertTrue(self.ledger_path.exists())
        self.assertTrue((self.data / 'dual-notifications.lock').exists())
        self.assertTrue(self.ledger()['baseline_complete'])

    def test_304_never_refreshes_dual_watermark_or_rearms_entries(self):
        self.pending()
        before = self.ledger()
        for offset in (1200, 1800, 2400, 3000):
            self.poll(T + offset, code=304)
        self.assertEqual(self.ledger()['checked_at'], before['checked_at'])
        self.assertEqual(self.ledger()['entries'], before['entries'])
        self.assert_claim_blocked(now=T + 3001)

    def test_global_off_immediately_blocks_both_channels(self):
        self.pending()
        m.toggle(self.data, False)
        self.assertFalse(self.ledger()['healthy'])
        self.assert_claim_blocked()
        result = self.poll(T + 1200, source_payload(T + 1200, [source_event()]))
        self.assertEqual(result['result'], 'paused')
        self.assertFalse(self.ledger()['healthy'])

    def test_dual_off_preserves_dedup_and_blocks_both_channels(self):
        self.pending()
        before = copy.deepcopy(self.ledger()['entries'])
        m.dual_toggle(self.data, False, now=T + 700)
        self.assertFalse(self.ledger()['healthy'])
        self.assertEqual(self.ledger()['entries'], before)
        self.assert_claim_blocked()

    def test_off_on_requires_new_baseline_and_suppresses_pause_period_history(self):
        self.pending()
        m.dual_toggle(self.data, False, now=T + 700)
        m.dual_toggle(self.data, True, now=T + 1100)
        self.assertFalse(self.ledger()['baseline_complete'])
        paused_event = source_event(created=T + 900, updated=T + 901)
        self.poll(T + 1200, source_payload(T + 1200, [paused_event]))
        self.assertTrue(self.ledger()['baseline_complete'])
        for name in ('wechat', 'email'):
            self.assertEqual(self.channel(channel=name)['state'], 'suppressed')
        self.assert_claim_blocked()

    def test_off_on_retains_attempt_request_and_provider_ids(self):
        self.pending()
        relay = self.relay()
        prepared = relay.claim(EID, 'announced', 'wechat', T + 601)
        self.assertEqual(prepared['result'], 'prepared')
        send_id = 'notice_' + '1' * 32
        relay.record(EID, 'announced', 'wechat', {
            'send_id': send_id, 'status': 'queued',
            'outgoing_text': prepared['payload']['text'],
            'accepted': False, 'delivered': None, 'delivery_receipt_available': False,
        }, T + 602)
        m.dual_toggle(self.data, False, now=T + 700)
        m.dual_toggle(self.data, True, now=T + 1100)
        self.poll(T + 1200, source_payload(T + 1200, [source_event()]))
        state = self.channel()
        self.assertEqual(state['state'], 'waiting')
        self.assertEqual(state['request_id'], prepared['request_id'])
        self.assertEqual(state['provider_id'], send_id)
        result = self.relay().claim(EID, 'announced', 'wechat', T + 1201)
        self.assertEqual(result['result'], 'reconcile_required')
        self.assertTrue(result['query_only'])
        self.assertEqual(result['request_id'], prepared['request_id'])
        self.assertEqual(result['provider_id'], send_id)
        self.assertNotIn('payload', result)

    def test_display_projection_failure_blocks_both_channels(self):
        capture = m.dual_package().capture_verified
        with mock.patch.object(sys.modules[capture.__module__], 'build_display', side_effect=ValueError('fixture')):
            self.pending()
        self.assertEqual(self.ledger()['entries'][EID + '/announced']['intent']['display']['style'], 'rich-unavailable-v2')
        self.assertEqual(self.relay().claim(EID, 'announced', 'wechat', T + 601)['result'], 'presentation_unavailable')
        self.assertEqual(self.relay().claim(EID, 'announced', 'email', T + 602)['result'], 'presentation_unavailable')

    def test_formatting_failure_does_not_suspend_email_capture(self):
        capture = m.dual_package().capture_verified
        with mock.patch.object(sys.modules[capture.__module__], 'format_b', side_effect=ValueError('fixture')):
            self.pending()
        self.assertEqual(self.relay().claim(EID, 'announced', 'wechat', T + 601)['result'], 'presentation_unavailable')
        self.assertEqual(self.relay().claim(EID, 'announced', 'email', T + 602)['result'], 'presentation_unavailable')

    def test_new_event_uses_b_style_and_unchanged_email_contract(self):
        self.pending()
        ledger = self.ledger()['entries'][EID + '/announced']
        self.assertIn('display', ledger['intent'])
        wx = self.relay().claim(EID, 'announced', 'wechat', T + 601)
        self.assertTrue(wx['payload']['text'].startswith('🔔 Codex 重置预告'))
        self.assertIn('📡 来源：AI Hot', wx['payload']['text'])
        self.assertIn('Synthetic forecast', wx['payload']['text'])
        self.assertNotEqual(wx['payload']['text'], ledger['intent']['text'])
        mail = self.relay().claim(EID, 'announced', 'email', T + 602)
        self.assertEqual(mail['payload']['subject'], '🔔 Codex 重置预告')
        self.assertIn('Synthetic forecast', mail['payload']['payload']['body']['content'])
        self.assertNotIn('AIHOT-', mail['payload']['subject'])

    def test_display_changes_stop_both_new_rich_channels(self):
        self.pending()
        changed = source_event(updated=T + 610)
        changed['posts'][0]['originalText'] = 'An updated source statement'
        self.poll(T + 1200, source_payload(T + 1200, [changed]))
        self.assertEqual(self.channel(channel='wechat')['state'], 'suppressed')
        self.assertEqual(self.channel(channel='email')['state'], 'suppressed')
        self.assertNotEqual(self.relay().claim(EID, 'announced', 'email', T + 1201)['result'], 'prepared')

    def test_legacy_record_remains_legacy_and_cannot_requeue(self):
        self.pending()
        ledger = self.ledger()
        entry = ledger['entries'][EID + '/announced']
        entry['intent'].pop('display')
        ledger['snapshot'][EID + '/announced'].pop('display')
        entry['channels']['wechat'].update(state='accepted', provider_id='notice_'+'a'*32, provider_status='accepted')
        self.ledger_path.write_text(json.dumps(ledger))
        self.poll(T + 1200, source_payload(T + 1200, [source_event()]))
        entry = self.ledger()['entries'][EID + '/announced']
        self.assertNotIn('display', entry['intent'])
        self.assertEqual(entry['channels']['wechat']['state'], 'accepted')
        self.assertEqual(entry['channels']['wechat']['provider_id'], 'notice_'+'a'*32)
        self.assertNotEqual(self.relay().claim(EID, 'announced', 'wechat', T + 1201)['result'], 'prepared')

    def test_existing_v1_email_commitment_does_not_change_when_v2_source_is_seen(self):
        self.pending()
        from ..notice_relay.text_b import legacy_display
        ledger=self.ledger();key=EID+'/announced'
        for item in (ledger['entries'][key]['intent'],ledger['snapshot'][key]):
            item['display']=legacy_display(item['display'])
        entry=ledger['entries'][key];entry['channels']['email'].update(state='accepted',provider_id='123456abcdef',provider_status='sent')
        original=copy.deepcopy(entry)
        self.ledger_path.write_text(json.dumps(ledger))
        self.poll(T+1200,source_payload(T+1200,[source_event()]))
        self.assertEqual(self.ledger()['entries'][key],original)
        self.assertEqual(self.relay().claim(EID,'announced','email',T+1201)['result'],'not_sendable')

    def test_expired_new_event_never_prepares_a_send(self):
        self.pending()
        ledger=self.ledger();key=EID+'/announced'
        expiry=ledger['entries'][key]['intent']['expires_at']
        self.assertNotEqual(self.relay().claim(EID,'announced','wechat',expiry)['result'],'prepared')
        self.assertNotEqual(self.relay().claim(EID,'announced','email',expiry)['result'],'prepared')

    def test_invalid_display_cannot_be_used_for_another_event(self):
        self.pending()
        ledger = self.ledger()
        ledger['entries'][EID + '/announced']['intent']['display']['event_id'] = OLD_CONFIRMED
        self.ledger_path.write_text(json.dumps(ledger))
        with self.assertRaises(Exception):
            self.relay().claim(EID, 'announced', 'wechat', T + 601)

    def test_corrupt_provider_state_cannot_reprepare_or_claim_acceptance(self):
        self.pending()
        clean = self.ledger_path.read_text()
        cases = (
            dict(state='pending', provider_id='notice_' + 'a' * 32, provider_status=None),
            dict(state='accepted', provider_id='notice_' + 'a' * 32, provider_status=None),
            dict(state='waiting', provider_id='wrong-provider', provider_status='queued'),
            dict(state='uncertain', provider_id='notice_' + 'a' * 32, provider_status='unexpected'),
        )
        for corrupt in cases:
            with self.subTest(corrupt=corrupt):
                ledger = json.loads(clean)
                ledger['entries'][EID + '/announced']['channels']['wechat'].update(corrupt)
                self.ledger_path.write_text(json.dumps(ledger))
                before = self.ledger_path.read_bytes()
                with self.assertRaises(Exception):
                    self.relay().claim(EID, 'announced', 'wechat', T + 601)
                self.assertEqual(self.ledger_path.read_bytes(), before)
        self.ledger_path.write_text(clean)

    def test_repeated_on_does_not_reset_existing_pending_or_baseline(self):
        self.pending()
        before = self.ledger()
        m.dual_toggle(self.data, True, now=T + 700)
        after = self.ledger()
        self.assertTrue(after['baseline_complete'])
        self.assertEqual(after['checked_at'], before['checked_at'])
        self.assertEqual(after['entries'], before['entries'])
        for name in ('wechat', 'email'):
            self.assertEqual(self.channel(channel=name)['state'], 'pending')

    def test_network_failure_suspends_dual_without_sender_or_model_execution(self):
        self.pending()
        result = self.poll(T + 1200, transport=mock.Mock(side_effect=OSError('synthetic network failure')))
        self.assertEqual(result['result'], 'network_failure')
        self.assertFalse(self.ledger()['healthy'])
        self.assert_claim_blocked()

    def test_unknown_schema_suspends_existing_dual_snapshot(self):
        self.pending()
        before = copy.deepcopy(self.ledger()['snapshot'])
        invalid = source_payload(T + 1200, [source_event()])
        invalid['schemaVersion'] = 999
        self.poll(T + 1200, invalid)
        self.assertFalse(self.ledger()['healthy'])
        self.assertEqual(self.ledger()['snapshot'], before)
        self.assert_claim_blocked()

    def test_unknown_status_change_cancels_existing_pending(self):
        self.pending()
        a = source_activity(published=T + 700)
        a.update(action=None, statusChanged=True)
        self.poll(T + 1200, source_payload(T + 1200, [source_event()], [a]))
        for name in ('wechat', 'email'):
            self.assertEqual(self.channel(channel=name)['state'], 'suppressed')
        self.assert_claim_blocked()

    def test_status_is_read_only(self):
        self.pending()
        before = {path.name: path.read_bytes() for path in self.data.iterdir() if path.is_file()}
        m.dual_status(self.data)
        after = {path.name: path.read_bytes() for path in self.data.iterdir() if path.is_file()}
        self.assertEqual(before, after)

    def test_restart_and_duplicate_snapshot_preserve_independent_claim_history(self):
        self.pending()
        first = self.relay().claim(EID, 'announced', 'wechat', T + 601)
        self.assertEqual(first['result'], 'prepared')
        spec = importlib.util.spec_from_file_location('dual_producer_restart', SOURCE)
        restarted = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(restarted)
        self.poll(T + 1200, source_payload(T + 1200, [source_event()]), module=restarted)
        self.assertEqual(self.channel()['state'], 'attempting')
        self.assertEqual(self.channel()['request_id'], first['request_id'])
        self.assertEqual(self.channel(channel='email')['state'], 'pending')
        result = self.relay().claim(EID, 'announced', 'wechat', T + 1201)
        self.assertEqual(result['result'], 'reconcile_required')
        self.assertNotIn('payload', result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
