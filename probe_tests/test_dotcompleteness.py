"""Expected-parts fixtures: no remote write, production data or verification."""
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_dotjournal as fixtures
from probe.weixin import WeixinError


class DotCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DotJournalTests('test_prepare_only_persists_one_unique_intent_never_a_send_permit')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.p = self.f.p

    def view(self):
        return self.p.dot_completeness.for_message(self.f.mid)

    def parts(self):
        return {p['part_key']: p for p in self.view()['expected_parts']}

    def media_message(self):
        self.f.mid, self.f.event = self.f.inbound('two-files', item_list=[
            {'type': 1, 'text_item': {'text': 'BODY_CANARY'}},
            {'type': 4, 'file_item': {'file_name': 'same.txt'}},
            {'type': 4, 'file_item': {'file_name': 'same.txt'}}])
        with self.p.db:
            self.p.db.execute("UPDATE weixin_attachments SET status='cached',error_kind=NULL,size=1,sha256=?,expires_at=? WHERE message_id=?",
                              ('a' * 64, self.f.now + 1000, self.f.mid))
        return [r[0] for r in self.p.db.execute('SELECT id FROM weixin_attachments WHERE message_id=? ORDER BY id',
                                              (self.f.mid,))]

    def record(self, digit, **changes):
        prepared = self.f.prepare(**changes)
        self.f.commit(prepared, source='history_readback', target='Sentinel_' + digit * 32)
        return prepared

    def test_missing_unprepared_attachment_detected_even_all_journals_committed(self):
        first, second = self.media_message()
        self.record('1')
        self.record('2', attachment_id=first)
        view = self.view()
        self.assertEqual(self.parts()['attachment:' + second]['state'], 'missing')
        self.assertEqual(view['expected_part_count'], 3)
        self.assertFalse(view['all_expected_parts_receipted'])
        self.assertFalse(view['all_parts_verified'])
        self.assertEqual({r[0] for r in self.p.db.execute('SELECT state FROM weixin_dot_journal')}, {'committed'})

    def test_committed_api_response_never_verified_or_positive_readback(self):
        prepared = self.f.admitted()
        self.f.authorize(prepared)
        self.f.commit(prepared)
        view = self.view()
        self.assertEqual(view['expected_parts'][0]['state'], 'receipt_recorded')
        self.assertTrue(view['all_expected_parts_receipted'])
        self.assertFalse(view['all_expected_parts_readback_reported'])
        self.assertFalse(view['all_parts_verified'])
        self.assertFalse(view['dot_visible_verified_by_bridge'])

    def test_positive_metadata_readback_report_is_not_content_verification(self):
        self.record('1')
        view = self.view()
        self.assertTrue(view['all_expected_parts_readback_reported'])
        self.assertEqual(view['expected_parts'][0]['state'], 'readback_reported')
        self.assertFalse(view['target_content_verified_by_bridge'])
        self.assertFalse(view['all_parts_verified'])

    def test_manifest_attachment_row_missing_still_expected(self):
        first, _ = self.media_message()
        with self.p.db:
            self.p.db.execute('DELETE FROM weixin_attachments WHERE id=?', (first,))
        part = self.parts()['attachment:' + first]
        self.assertEqual(part['state'], 'missing')
        self.assertEqual(part['source_error_kind'], 'source_attachment_record_missing')
        self.assertFalse(self.view()['source_content_ready_recorded'])

    def test_attachment_expired_error_and_unknown_remain_separate(self):
        first, second = self.media_message()
        prepared = self.f.prepare(attachment_id=first)
        self.p.dot_journal.mark_unknown(prepared['journal_id'], prepared['attempt_id'])
        with self.p.db:
            self.p.db.execute('UPDATE weixin_attachments SET expires_at=? WHERE id=?', (self.f.now - 1, first))
            self.p.db.execute("UPDATE weixin_attachments SET status='error',error_kind='synthetic_failure' WHERE id=?", (second,))
        parts = self.parts()
        self.assertEqual(parts['attachment:' + first]['state'], 'unknown')
        self.assertEqual(parts['attachment:' + first]['source_state'], 'expired')
        self.assertEqual(parts['attachment:' + second]['state'], 'error')
        self.assertFalse(self.view()['all_parts_verified'])

    def test_simple_reply_only_final_and_later_slots_change_snapshot(self):
        before = self.view()
        self.p.weixin.queue_reply(self.f.mid, 'fixture answer')
        after = self.view()
        self.assertEqual({p['part'] for p in after['expected_parts']}, {'original', 'final'})
        self.assertNotEqual(before['snapshot_sha256'], after['snapshot_sha256'])
        self.assertEqual(self.parts()['final']['state'], 'missing')

    def test_complex_actual_ack_and_final_are_both_expected(self):
        self.p.weixin.queue_reply_phase(self.f.mid, 'ack', 'fixture progress')
        self.p.weixin.queue_reply_phase(self.f.mid, 'final', 'fixture result')
        self.assertEqual({p['part'] for p in self.view()['expected_parts']}, {'original', 'ack', 'final'})

    def test_unsent_or_superseded_system_ack_is_not_actual_reply(self):
        self.p.weixin.queue_reply_phase(self.f.mid, 'ack', 'fixture progress')
        sid = self.p.weixin.ack_send_id(self.f.mid)
        with self.p.db:
            self.p.db.execute("UPDATE weixin_outbox SET ack_origin='system_receipt',status='superseded' WHERE message_id=?", (sid,))
        self.assertNotIn('ack', self.parts())
        with self.p.db:
            self.p.db.execute("UPDATE weixin_outbox SET status='delivery_unknown' WHERE message_id=?", (sid,))
        self.assertIn('ack', self.parts())

    def test_unknown_and_overlimit_items_have_explicit_non_delivery_parts(self):
        self.f.mid, self.f.event = self.f.inbound('issues', item_list=[{'type': 799}] + [
            {'type': 4, 'file_item': {'file_name': 'same.txt'}} for _ in range(9)])
        view = self.view()
        issues = [p for p in view['expected_parts'] if not p['mirror_required']]
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(p['state'] == 'unsupported' for p in issues))
        self.assertFalse(view['source_inventory_complete'])
        self.assertFalse(view['all_expected_parts_readback_reported'])
        self.assertFalse(view['all_parts_verified'])

    def test_old_source_without_capture_inventory_never_inferred_complete(self):
        with self.p.db:
            self.p.db.execute('DELETE FROM weixin_inbound_manifests WHERE message_id=?', (self.f.mid,))
        view = self.view()
        self.assertIn('inbound:legacy-unrecorded', self.parts())
        self.assertFalse(view['source_inventory_complete'])
        self.assertFalse(view['all_parts_accounted_for'])

    def test_read_only_no_claim_no_body_and_no_transaction_leak(self):
        prepared = self.f.prepare()
        changes = self.p.db.total_changes
        view = self.view()
        self.assertEqual(self.p.db.total_changes, changes)
        self.assertFalse(self.p.db.in_transaction)
        self.assertFalse(view['external_write_allowed'])
        text = json.dumps(view)
        for secret in ('BODY_CANARY', 'TOKEN_CANARY', 'CONTEXT_CANARY', 'fixture-owner', 'fixture-bot'):
            self.assertNotIn(secret, text)
        self.assertEqual(self.p.db.execute('SELECT attempt_id FROM weixin_dot_journal WHERE id=?', (prepared['journal_id'],)).fetchone()[0], prepared['attempt_id'])

    def test_unknown_source_and_foreign_owner_rejected(self):
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.p.dot_completeness.for_message('not-owner-fixture')
        with self.p.db:
            self.p.db.execute("UPDATE weixin_accounts SET owner='another-fixture'")
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.view()
        self.assertFalse(self.p.db.in_transaction)

    def test_view_snapshot_excludes_concurrently_committed_reply_until_next_read(self):
        peer = self.f.open_probe()
        barrier = threading.Barrier(2)
        done = threading.Event()
        original = self.p.weixin.inbound_completeness
        def source(mid):
            result = original(mid)
            barrier.wait(timeout=3)
            self.assertTrue(done.wait(3))
            return result
        def writer():
            barrier.wait(timeout=3)
            try:
                peer.weixin.queue_reply(self.f.mid, 'concurrent fixture')
            finally:
                done.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer)
            with patch.object(self.p.weixin, 'inbound_completeness', side_effect=source):
                snapshot = self.view()
            future.result(timeout=3)
        self.assertNotIn('final', {p['part_key'] for p in snapshot['expected_parts']})
        self.assertIn('final', self.parts())

    def test_rpc_routes_inventory_and_owner_read_without_granting_writes(self):
        result = self.p.rpc({'jsonrpc': '2.0', 'id': 77, 'method': 'tools/call',
            'params': {'name': 'get_weixin_dot_completeness', 'arguments': {'message_id': self.f.mid}}})
        self.assertFalse(result['result']['structuredContent']['all_parts_verified'])
        owner = self.p.rpc({'jsonrpc': '2.0', 'id': 78, 'method': 'tools/call',
            'params': {'name': 'read_weixin_message', 'arguments': {'message_id': self.f.mid}}})
        self.assertEqual(owner['result']['structuredContent']['dot_sync_completeness']['version'], 'dot-completeness-v1')

    def test_corrupt_auxiliary_manifest_does_not_block_owner_read_or_fake_reply(self):
        with self.p.db:
            self.p.db.execute("UPDATE weixin_inbound_manifests SET metadata_json='{' WHERE message_id=?", (self.f.mid,))
        read = self.p.weixin.read_message(self.f.mid)
        self.assertEqual(read['text'], 'BODY_CANARY')
        self.assertFalse(read['inbound_completeness']['complete'])
        self.assertFalse(read['inbound_completeness']['available'])
        with self.assertRaisesRegex(WeixinError, 'invalid_inbound_manifest'):
            self.view()
        self.p.weixin.queue_reply(self.f.mid, 'fixture answer despite inventory failure')
        with patch.object(self.p.weixin.client, 'send', return_value={'ret': 0}) as fake_send:
            self.assertTrue(self.p.weixin.send_once())
            self.assertEqual(fake_send.call_count, 1)
        self.assertTrue(self.p.weixin.send_status(self.f.mid)['accepted'])
        rpc = self.p.rpc({'jsonrpc': '2.0', 'id': 79, 'method': 'tools/call',
            'params': {'name': 'read_weixin_message', 'arguments': {'message_id': self.f.mid}}})
        self.assertFalse(rpc['result']['structuredContent']['dot_sync_completeness']['all_parts_verified'])
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.p.weixin.queue_reply('foreign-fixture', 'not permitted')
