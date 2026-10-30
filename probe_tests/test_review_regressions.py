"""Review regressions: synthetic evidence only, no network or production DB."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from probe.fixed_commands import prepare_weekly_usage
from probe.usage_display import UsageBinding, UsageObservation
from probe.deliverable_contract import DeliveryJournal, preview_commitment


class QuotaStatusTests(unittest.TestCase):
    def test_missing_tool_is_not_a_failed_official_call_and_stays_four_lines(self):
        r = prepare_weekly_usage(None, UsageBinding('owner','account',True), now=1900000000)
        self.assertEqual(len(r['text'].splitlines()), 4)
        self.assertFalse(r['official_call_attempted'])
        self.assertIn('未调用', r['text'])
        self.assertIn('未接通', r['text'])

    def test_failed_reader_is_distinct_and_called_once(self):
        reader = Mock(side_effect=RuntimeError('DO_NOT_LEAK'))
        r = prepare_weekly_usage(reader, UsageBinding('owner','account',True), now=1900000000)
        reader.assert_called_once()
        self.assertTrue(r['official_call_attempted'])
        self.assertEqual(len(r['text'].splitlines()), 4)
        self.assertIn('官方读取失败', r['text'])
        self.assertNotIn('DO_NOT_LEAK', r['text'])

    def test_unverified_account_does_not_call_reader(self):
        reader=Mock()
        r=prepare_weekly_usage(reader,UsageBinding('owner','account',False),now=1900000000)
        reader.assert_not_called()
        self.assertFalse(r['official_call_attempted'])
        self.assertEqual(len(r['text'].splitlines()),4)
        self.assertIn('账户未核验',r['text'])

    def test_noncallable_reader_is_unavailable_not_official_failure(self):
        r=prepare_weekly_usage('not-a-tool',UsageBinding('owner','account',True),now=1900000000)
        self.assertFalse(r['official_call_attempted'])
        self.assertEqual(r['status'],'unavailable')


class DurableDenialTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'test.sqlite')
        self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        self.j=DeliveryJournal(self.db)
        self.c=preview_commitment('owner','request','artifact-v1','front','a'*64)
        self.e=dict(owner_verified=True,explicit_delivery_request=True,
            non_sensitive_verified=True,drive_authorized_private=True,
            archive=dict(verified=True,file_id='archive',sha256='a'*64))
        self.j.register(self.c,self.e)
        self.j.update_before_submission(self.c['key'],self.e,upload_id='upload',upload_state='ready')

    def test_denial_after_claim_survives_restart_without_send_id(self):
        self.j.claim_submission(self.c['key'],caller_send_key='stable-key')
        self.j.record_denial(self.c['key'],evidence_ref='approval-review-1',known_not_executed=True)
        db2=sqlite3.connect(self.path);self.addCleanup(db2.close)
        restored=DeliveryJournal(db2).read(self.c['key'])
        self.assertEqual(restored['next_step'],'stop_permission_denied')
        self.assertEqual(restored['record']['caller_send_key'],'stable-key')
        self.assertTrue(restored['record']['denial']['known_not_executed'])
        with self.assertRaises(ValueError):self.j.claim_submission(self.c['key'],caller_send_key='new-key')

    def test_denial_cannot_be_cleared_by_new_evidence_or_registration(self):
        self.j.record_denial(self.c['key'],evidence_ref='approval-review-1',known_not_executed=True)
        self.j.update_before_submission(self.c['key'],self.e)
        self.j.register(self.c,self.e)
        self.assertEqual(self.j.read(self.c['key'])['next_step'],'stop_permission_denied')

    def test_unknown_outcome_cannot_be_relabeled_not_executed(self):
        self.j.claim_submission(self.c['key'],caller_send_key='stable-key')
        self.j.record_send(self.c['key'],send_id='send',api_state='uncertain')
        with self.assertRaises(ValueError):
            self.j.record_denial(self.c['key'],evidence_ref='approval-review-1',known_not_executed=True)
        self.assertEqual(self.j.read(self.c['key'])['next_step'],'query_original_send')

    def test_denial_requires_reference_and_exact_boolean(self):
        for evidence,known in [('',True),('x',1),('x','true')]:
            with self.assertRaises(ValueError):
                self.j.record_denial(self.c['key'],evidence_ref=evidence,known_not_executed=known)

    def test_second_attachment_does_not_inherit_first_denial_or_receipt(self):
        self.j.record_denial(self.c['key'],evidence_ref='approval-review-1',known_not_executed=True)
        other=preview_commitment('owner','request','artifact-v1','back','a'*64)
        self.assertEqual(self.j.register(other,self.e)['next_step'],'prepare_native_image_upload')

