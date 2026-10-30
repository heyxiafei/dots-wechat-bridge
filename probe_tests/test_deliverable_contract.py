"""Synthetic offline delivery evidence; no network or real sending."""
import unittest
from probe.deliverable_contract import preview_commitment, next_step

class DeliverableTests(unittest.TestCase):
    def setUp(self):
        self.c = preview_commitment('fixture-owner', 'fixture-request', 'result-v1', 'front', 'a'*64)
        self.e = dict(owner_verified=True, explicit_delivery_request=True, non_sensitive_verified=True,
                      drive_authorized_private=True, archive=dict(verified=True, file_id='fixture-file', sha256='a'*64))
    def record(self, **kw):
        return dict(commitment=self.c, **kw)
    def test_link_cannot_replace_verified_archive(self):
        self.e['archive'] = {'link':'https://example.com/fixture'}
        self.assertEqual(next_step(self.c,self.e), 'archive_and_verify_private_drive')
    def test_archive_must_match_image_hash(self):
        self.e['archive']['sha256']='b'*64
        self.assertEqual(next_step(self.c,self.e), 'archive_and_verify_private_drive')
    def test_verified_archive_requires_native_image(self):
        self.assertEqual(next_step(self.c,self.e), 'prepare_native_image_upload')
    def test_missing_scope_or_sensitive_or_public_fails_closed(self):
        for key in ('owner_verified','explicit_delivery_request','non_sensitive_verified','drive_authorized_private'):
            self.assertEqual(next_step(self.c,dict(self.e,**{key:False})), 'await_verified_scope')
    def test_final_text_does_not_cancel_requested_preview(self):
        self.e['text_final_committed']=True
        self.assertEqual(next_step(self.c,self.e), 'prepare_native_image_upload')
    def test_upload_ready_and_other_states(self):
        for state in ('pending','error','expired','unknown'):
            self.assertEqual(next_step(self.c,self.e,self.record(upload_id='fixture-up',upload_state=state)), 'query_original_upload')
        self.assertEqual(next_step(self.c,self.e,self.record(upload_id='fixture-up',upload_state='ready')), 'prepare_single_media_commitment')
    def test_each_picture_stable_and_independent(self):
        other=preview_commitment('fixture-owner','fixture-request','result-v1','back','a'*64)
        self.assertNotEqual(other['key'],self.c['key'])
        self.assertEqual(preview_commitment('fixture-owner','fixture-request','result-v1','front','a'*64),self.c)
        self.assertEqual(next_step(other,self.e,self.record(send_id='fixture-send',owner_received_evidence_ref='fixture-receipt')), 'stop_commitment_conflict')
    def test_changed_bytes_same_key_is_conflict(self):
        other=preview_commitment('fixture-owner','fixture-request','result-v1','front','b'*64)
        self.assertEqual(other['key'],self.c['key'])
        self.assertEqual(next_step(other,self.e,self.record()), 'stop_commitment_conflict')
    def test_unknown_or_accepted_never_resubmits(self):
        for state in ('queued','accepted','uncertain','delivery_unknown','rejected'):
            self.assertEqual(next_step(self.c,self.e,self.record(send_id='fixture-send',status=state)), 'query_original_send')
    def test_timeout_without_send_id_requires_reconciliation(self):
        self.assertEqual(next_step(self.c,self.e,self.record(submission_started=True)), 'reconcile_original_submission')
    def test_owner_receipt_is_separate_from_api_acceptance(self):
        self.assertEqual(next_step(self.c,self.e,self.record(send_id='fixture-send',status='accepted')), 'query_original_send')
        self.assertEqual(next_step(self.c,self.e,self.record(send_id='fixture-send',owner_received_evidence_ref='fixture-receipt')), 'owner_receipt_recorded')
    def test_denial_stops_ready_delivery(self):
        self.e['permission_denied']=True
        self.assertEqual(next_step(self.c,self.e,self.record(upload_id='fixture-up',upload_state='ready')), 'stop_permission_denied')
    def test_tampered_key_and_bad_hash(self):
        self.assertEqual(next_step(dict(self.c,key='other'),self.e), 'invalid_commitment')
        with self.assertRaises(ValueError):preview_commitment('owner','request','result','front','bad')

class JournalTests(unittest.TestCase):
    def setUp(self):
        import sqlite3, tempfile
        from pathlib import Path
        from probe.deliverable_contract import DeliveryJournal
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/'fixture.sqlite')
        self.db=sqlite3.connect(self.path);self.addCleanup(self.db.close)
        self.j=DeliveryJournal(self.db)
        self.c=preview_commitment('fixture-owner','fixture-request','result','front','a'*64)
        self.e=dict(owner_verified=True,explicit_delivery_request=True,non_sensitive_verified=True,drive_authorized_private=True,archive=dict(verified=True,file_id='fixture-file',sha256='a'*64))
        self.j.register(self.c,self.e)
    def ready(self):
        self.j.update_before_submission(self.c['key'],self.e,upload_id='fixture-up',upload_state='ready')
    def test_duplicate_registration_and_content_conflict(self):
        self.j.register(self.c,self.e)
        self.assertEqual(self.db.execute('select count(*) from deliverable_previews').fetchone()[0],1)
        with self.assertRaises(ValueError):self.j.register(dict(self.c,sha256='b'*64),self.e)
    def test_claim_once_and_restart_unknown(self):
        import sqlite3
        from probe.deliverable_contract import DeliveryJournal
        self.ready();self.j.claim_submission(self.c['key'],caller_send_key='fixture-send-key')
        db2=sqlite3.connect(self.path);self.addCleanup(db2.close);j2=DeliveryJournal(db2)
        self.assertEqual(j2.read(self.c['key'])['next_step'],'reconcile_original_submission')
        with self.assertRaises(ValueError):j2.claim_submission(self.c['key'],caller_send_key='other')
    def test_claim_compare_and_swap_rejects_stale_writer(self):
        self.ready();old=self.j.read(self.c['key'])
        self.j.claim_submission(self.c['key'],caller_send_key='fixture-send-key')
        with self.assertRaises(ValueError):self.j._cas(self.c['key'],old,self.e,old['record'])
    def test_accepted_and_owner_receipt_are_distinct(self):
        self.ready();self.j.claim_submission(self.c['key'],caller_send_key='fixture-send-key')
        self.assertEqual(self.j.record_send(self.c['key'],send_id='fixture-send',api_state='accepted')['next_step'],'query_original_send')
        with self.assertRaises(ValueError):self.j.record_send(self.c['key'],send_id='other',api_state='accepted')
        self.assertEqual(self.j.record_send(self.c['key'],send_id='fixture-send',api_state='accepted',owner_received_evidence_ref='fixture-owner-receipt')['next_step'],'owner_receipt_recorded')
    def test_denial_before_claim_and_upload_identity(self):
        self.ready()
        with self.assertRaises(ValueError):self.j.update_before_submission(self.c['key'],self.e,upload_id='other',upload_state='ready')
        self.j.update_before_submission(self.c['key'],dict(self.e,permission_denied=True))
        with self.assertRaises(ValueError):self.j.claim_submission(self.c['key'],caller_send_key='fixture-send-key')
