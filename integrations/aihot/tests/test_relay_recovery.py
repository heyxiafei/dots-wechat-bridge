#!/usr/bin/python3
"""Offline receipt recovery regressions; every ledger is disposable test data."""
import concurrent.futures
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from .fixtures import OPERATOR
from unittest import mock

from ..notice_relay import relay as module
from ..notice_relay.store_helpers import Invalid, notification_text

T = 1792332000.0
EID = 'reset-1900000000000000001-1-1'
OTHER_EID = 'reset-1900000000000000002-1-1'
LINK = 'https://x.com/thsottiaux/status/1900000000000000001'
SEND_ID = 'notice_' + '1' * 32
OTHER_SEND_ID = 'notice_' + '2' * 32
MESSAGE_ID = '1234567890abcdef'
OTHER_MESSAGE_ID = 'fedcba0987654321'


def intent(eid=EID, stage='announced', source_at=T + 10, expires_at=T + 10000):
    link = LINK if eid == EID else LINK[:-1] + '2'
    return dict(event_id=eid, stage=stage, source_at=source_at,
                expires_at=expires_at, link=link, eligible=True,
                text=notification_text(stage, source_at, link))


class RelayRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aihot-relay-recovery-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.serial = 0
        for target, name in ((socket, 'socket'), (subprocess, 'Popen'), (os, 'system')):
            patch = mock.patch.object(target, name, side_effect=AssertionError(
                'network/sender/model/credential access forbidden in offline tests'))
            patch.start()
            self.addCleanup(patch.stop)
        self.fresh()

    def fresh(self):
        self.serial += 1
        self.path = self.root / ('case-' + str(self.serial))
        self.ledger = self.path / 'dual-notifications.json'
        self.r = module.Relay(self.path, now_callable=lambda: T + 602, operator_config=OPERATOR)
        self.r.baseline([], T)
        self.r.sync([intent(), intent(OTHER_EID)], T + 600)

    def ch(self, channel='wechat', eid=EID):
        return self.r.status()['entries'][eid + '/announced']['channels'][channel]

    def prepare(self, channel='wechat', eid=EID):
        result = self.r.claim(eid, 'announced', channel, T + 601)
        self.assertEqual(result['result'], 'prepared')
        return result

    def receipt(self, channel='wechat', eid=EID, **changes):
        if channel == 'wechat':
            result = dict(send_id=SEND_ID, status='accepted',
                          outgoing_text=intent(eid)['text'], accepted=True,
                          delivered=None, delivery_receipt_available=False)
        else:
            expected = module.email_payload(intent(eid), OPERATOR)
            result = dict(message_id=MESSAGE_ID, readback_id=MESSAGE_ID,
                          to=OPERATOR.recipient, subject=expected['subject'],
                          body_html=expected['payload']['body']['content'],
                          label_ids=['SENT'], cc='', bcc='')
        result.update(changes)
        return result

    def record(self, receipt, channel='wechat', eid=EID):
        return self.r.record(eid, 'announced', channel, receipt, T + 602)

    def assert_query_only(self, channel='wechat', eid=EID, provider_id=None):
        result = self.r.claim(eid, 'announced', channel, T + 1201)
        self.assertEqual(result['result'], 'reconcile_required')
        self.assertTrue(result['query_only'])
        self.assertEqual(result['provider_id'], provider_id)
        self.assertNotIn('payload', result)
        return result

    def test_wechat_incomplete_followup_retains_known_id_and_can_reconcile(self):
        self.prepare()
        self.record(self.receipt(status='queued', accepted=False))
        self.record(dict(status='delivery_unknown'))
        self.record(dict(send_id=None, status='delivery_unknown'))
        acceptance_looking = self.receipt()
        del acceptance_looking['send_id']
        self.record(acceptance_looking)
        self.assertEqual(self.ch()['state'], 'uncertain')
        self.assertEqual(self.ch()['provider_id'], SEND_ID)
        self.assert_query_only(provider_id=SEND_ID)
        result = self.record(self.receipt())
        self.assertTrue(result['accepted'])
        self.assertEqual(self.ch()['state'], 'accepted')

    def test_email_incomplete_followup_retains_known_id_and_can_reconcile(self):
        self.prepare('email')
        self.record({'message_id': MESSAGE_ID}, 'email')
        self.record({}, 'email')
        self.record({'message_id': None}, 'email')
        acceptance_looking = self.receipt('email')
        del acceptance_looking['message_id']
        self.record(acceptance_looking, 'email')
        self.assertEqual(self.ch('email')['state'], 'uncertain')
        self.assertEqual(self.ch('email')['provider_id'], MESSAGE_ID)
        self.assert_query_only('email', provider_id=MESSAGE_ID)
        self.assertTrue(self.record(self.receipt('email'), 'email')['accepted'])

    def test_accepted_receipt_survives_followup_without_provider_id(self):
        for channel in ('wechat', 'email'):
            with self.subTest(channel=channel):
                self.fresh()
                self.prepare(channel)
                self.record(self.receipt(channel), channel)
                before = self.ledger.read_bytes()
                result = self.record({}, channel)
                self.assertEqual(result['result'], 'already_accepted')
                self.assertEqual(self.ch(channel)['state'], 'accepted')
                self.assertEqual(self.ledger.read_bytes(), before)
                self.assertIsNone(result['delivered'])

    def test_explicit_wrong_wechat_text_cannot_poison_provider_binding(self):
        self.prepare()
        before = self.ledger.read_bytes()
        with self.assertRaises(Invalid):
            self.record(self.receipt(send_id=OTHER_SEND_ID, outgoing_text='another notification'))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertIsNone(self.ch()['provider_id'])
        self.assertTrue(self.record(self.receipt())['accepted'])

    def test_complete_conflicting_email_readback_cannot_poison_provider_binding(self):
        for conflict in ({'to': 'other@example.invalid'}, {'subject': 'another notice'},
                         {'body_html': '<p>another body</p>'},
                         {'readback_id': MESSAGE_ID}, {'cc': 'other@example.invalid'},
                         {'bcc': 'other@example.invalid'}):
            with self.subTest(conflict=conflict):
                self.fresh()
                self.prepare('email')
                before = self.ledger.read_bytes()
                wrong = self.receipt('email', message_id=OTHER_MESSAGE_ID,
                                     readback_id=OTHER_MESSAGE_ID)
                wrong.update(conflict)
                with self.assertRaises(Invalid):
                    self.record(wrong, 'email')
                self.assertEqual(self.ledger.read_bytes(), before)
                self.assertIsNone(self.ch('email')['provider_id'])
                self.assertTrue(self.record(self.receipt('email'), 'email')['accepted'])

    def test_partial_conflicting_email_readback_cannot_poison_provider_binding(self):
        for field, value in (('subject', 'foreign subject'), ('body_html', '<p>foreign body</p>'),
                             ('to', 'other@example.invalid')):
            with self.subTest(field=field):
                self.fresh()
                self.prepare('email')
                receipt = dict(message_id=OTHER_MESSAGE_ID, readback_id=OTHER_MESSAGE_ID)
                receipt[field] = value
                before = self.ledger.read_bytes()
                with self.assertRaises(Invalid):
                    self.record(receipt, 'email')
                self.assertEqual(self.ledger.read_bytes(), before)
                self.assertTrue(self.record(self.receipt('email'), 'email')['accepted'])

    def test_optional_wechat_notification_id_must_match_current_request(self):
        prepared = self.prepare()
        before = self.ledger.read_bytes()
        with self.assertRaises(Invalid):
            self.record(self.receipt(notification_id='another-notification'))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertTrue(self.record(self.receipt(notification_id=prepared['request_id']))['accepted'])

    def test_explicit_malformed_provider_id_is_not_treated_as_missing(self):
        for channel in ('wechat', 'email'):
            for invalid in ('', False, 1, 'not-a-provider-id'):
                with self.subTest(channel=channel, invalid=invalid):
                    self.fresh()
                    self.prepare(channel)
                    self.record(self.receipt(channel), channel)
                    before = self.ledger.read_bytes()
                    field = 'send_id' if channel == 'wechat' else 'message_id'
                    with self.assertRaises(Invalid):
                        self.record({field: invalid}, channel)
                    self.assertEqual(self.ledger.read_bytes(), before)

    def test_wrong_email_labels_remain_uncertain_with_same_lookup_id(self):
        self.prepare('email')
        self.record(self.receipt('email', label_ids=['DRAFT']), 'email')
        self.assertEqual(self.ch('email')['state'], 'uncertain')
        self.assert_query_only('email', provider_id=MESSAGE_ID)
        self.assertTrue(self.record(self.receipt('email'), 'email')['accepted'])

    def test_explicit_provider_swap_or_reuse_is_rejected_without_mutation(self):
        self.prepare()
        self.record(self.receipt(status='queued', accepted=False))
        before = self.ledger.read_bytes()
        with self.assertRaises(Invalid):
            self.record(self.receipt(send_id=OTHER_SEND_ID))
        self.assertEqual(self.ledger.read_bytes(), before)
        self.prepare(eid=OTHER_EID)
        before = self.ledger.read_bytes()
        with self.assertRaises(Invalid):
            self.record(self.receipt(eid=OTHER_EID), eid=OTHER_EID)
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_unknown_no_id_never_authorizes_another_attempt_after_restart(self):
        prepared = self.prepare('email')
        self.record({}, 'email')
        self.r = module.Relay(self.path, now_callable=lambda: T + 1201, operator_config=OPERATOR)
        result = self.assert_query_only('email')
        self.assertEqual(result['request_id'], prepared['request_id'])
        self.assertEqual(self.ch('email')['state'], 'uncertain')

    def test_receipt_for_missing_notification_is_not_recordable(self):
        before = self.ledger.read_bytes()
        result = self.r.record('reset-1900000000000000003-1-1', 'announced',
                               'email', {}, T + 602)
        self.assertEqual(result['result'], 'not_recordable')
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_wechat_uncertainty_does_not_block_independent_email_attempt(self):
        self.prepare()
        self.record({})
        result = self.prepare('email')
        self.assertTrue(result['request_id'])
        self.assertEqual(self.ch()['state'], 'uncertain')
        self.assertEqual(self.ch('email')['state'], 'attempting')

    def test_duplicate_matching_receipts_are_idempotent_and_accepted_is_monotonic(self):
        for channel in ('wechat', 'email'):
            with self.subTest(channel=channel):
                self.fresh()
                self.prepare(channel)
                receipt = self.receipt(channel)
                self.record(receipt, channel)
                before = self.ledger.read_bytes()
                self.record(receipt, channel)
                later = self.receipt(status='uncertain', accepted=False) if channel == 'wechat' else {'message_id': MESSAGE_ID}
                self.record(later, channel)
                self.assertEqual(self.ledger.read_bytes(), before)
                self.assertEqual(self.ch(channel)['state'], 'accepted')

    def test_accepted_state_reports_explicit_content_conflicts_without_mutation(self):
        cases = (('wechat', {'notification_id': 'another-notification'}),
                 ('wechat', {'outgoing_text': 'another text'}),
                 ('email', {'subject': 'another subject'}),
                 ('email', {'to': 'other@example.invalid'}))
        for channel, changes in cases:
            with self.subTest(channel=channel, changes=changes):
                self.fresh()
                self.prepare(channel)
                self.record(self.receipt(channel), channel)
                before = self.ledger.read_bytes()
                with self.assertRaises(Invalid):
                    self.record(self.receipt(channel, **changes), channel)
                self.assertEqual(self.ledger.read_bytes(), before)
                self.assertEqual(self.ch(channel)['state'], 'accepted')

    def test_concurrent_claims_preserve_single_attempt_authorization(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda unused: self.r.claim(EID, 'announced', 'email', T + 601), range(2)))
        self.assertEqual(sum(r['result'] == 'prepared' for r in results), 1)
        self.assertEqual(self.ch('email')['state'], 'attempting')
        self.assertEqual(self.ch()['state'], 'pending')

    def test_concurrent_matching_receipts_cannot_lose_acceptance(self):
        self.prepare('email')
        receipt = self.receipt('email')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda unused: self.record(receipt, 'email'), range(2)))
        self.assertTrue(any(result.get('accepted') is True for result in results))
        self.assertEqual(self.ch('email')['state'], 'accepted')

    def test_failed_receipt_commit_preserves_claim_and_can_later_reconcile(self):
        prepared = self.prepare()
        before = self.ledger.read_bytes()
        with mock.patch(module.Relay.__module__ + '.atomic_json',
                        side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.record(self.receipt())
        self.assertEqual(self.ledger.read_bytes(), before)
        result = self.assert_query_only()
        self.assertEqual(result['request_id'], prepared['request_id'])
        self.assertTrue(self.record(self.receipt())['accepted'])

    def test_expiry_withdrawal_or_missing_source_never_releases_attempt(self):
        for stop in ('expired', 'withdrawn', 'missing'):
            with self.subTest(stop=stop):
                self.fresh()
                self.prepare()
                self.record(self.receipt(status='queued', accepted=False))
                if stop == 'withdrawn':
                    self.r.sync([], T + 1200, terminal={EID: 'withdrawn'})
                elif stop == 'missing':
                    self.r.sync([], T + 1200)
                result = self.r.claim(EID, 'announced', 'wechat', T + (10001 if stop == 'expired' else 1201))
                self.assertEqual(result['result'], 'reconcile_required')
                self.assertTrue(result['query_only'])
                self.assertEqual(result['provider_id'], SEND_ID)
                self.assertNotIn('payload', result)
                # Recording the outcome of an existing attempt is not a new send.
                self.assertTrue(self.record(self.receipt())['accepted'])

    def test_current_schema1_ledger_status_is_read_only_and_semantics_preserved(self):
        self.prepare()
        self.record(self.receipt(status='queued', accepted=False))
        before = self.ledger.read_bytes()
        state = module.Relay(self.path).status()
        self.assertEqual(state['version'], 1)
        self.assertEqual(set(state), {'version', 'activation_at', 'baseline_complete',
                                    'checked_at', 'healthy', 'snapshot', 'entries',
                                    'terminal', 'versions'})
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertEqual(self.ch()['provider_id'], SEND_ID)

    def test_missing_terminal_watermarks_remain_corrupt_and_are_not_migrated(self):
        stored = json.loads(self.ledger.read_text())
        del stored['terminal']
        del stored['versions']
        self.ledger.write_text(json.dumps(stored))
        before = self.ledger.read_bytes()
        with self.assertRaises(Invalid):
            self.r.status()
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_same_watermark_only_removal_suppresses_pending_and_keeps_retained_intents(self):
        before = self.r.status()
        self.r.sync([intent()], T + 1200, checked_at=T + 600)
        after = self.r.status()
        self.assertEqual(after['checked_at'], before['checked_at'])
        self.assertEqual(after['snapshot'], {EID + '/announced': intent()})
        self.assertEqual(after['entries'][EID + '/announced'], before['entries'][EID + '/announced'])
        for channel in ('wechat', 'email'):
            self.assertEqual(self.ch(channel, OTHER_EID)['state'], 'suppressed')

    def test_same_watermark_cannot_readd_removed_intent_or_modify_retained_intent(self):
        self.r.sync([intent()], T + 1200, checked_at=T + 600)
        before = copy.deepcopy(self.r.status()['entries'])
        with self.assertRaises(Invalid):
            self.r.sync([intent(), intent(OTHER_EID)], T + 1201, checked_at=T + 600)
        self.assertEqual(self.r.status()['entries'], before)
        for changes in ({'eligible': False}, {'expires_at': T + 20000}):
            with self.subTest(changes=changes):
                self.fresh()
                changed = intent()
                changed.update(changes)
                with self.assertRaises(Invalid):
                    self.r.sync([changed], T + 1200, checked_at=T + 600)


if __name__ == '__main__':
    unittest.main(verbosity=2)
