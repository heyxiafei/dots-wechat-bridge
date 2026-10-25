"""Synthetic metadata-only tests; no real dot/Weixin writes or remote ID proof."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from probe.probe import Probe, BUILD_VERSION
from probe.weixin import WeixinError, MESSAGE_EVENT


class DotLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Probe(self.tmp.name, clock=lambda: 1800000000.)
        self.addCleanup(lambda: self.p.close())
        self.p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': 'fixture-bot',
            'ilink_user_id': 'fixture-owner', 'bot_token': 'TOKEN_CANARY'})
        self.mid, self.event = self.inbound('one')

    def inbound(self, source, linked=True, **changes):
        msg = {'message_id': source, 'message_type': 1, 'from_user_id': 'fixture-owner',
            'to_user_id': 'fixture-bot', 'context_token': 'CONTEXT_CANARY',
            'item_list': [{'type': 1, 'text_item': {'text': 'BODY_CANARY'}}]}
        msg.update(changes)
        self.p.weixin.ingest(self.p.weixin.account(), {'msgs': [msg]})
        row = self.p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()
        if not row:
            return None, None
        mid, event = row['id'], 'evt_fixture_' + source
        if linked:
            with self.p.db:
                self.p.db.execute("INSERT INTO pings(id,nonce,sub_id,body,status,name,message_id) VALUES(?,'','fixture-sub','{}','delivered',?,?)", (event, MESSAGE_EVENT, mid))
                self.p.db.execute('UPDATE weixin_inbox SET event_id=? WHERE id=?', (event, mid))
        return mid, event

    def prepare(self, phase='original', **kwargs):
        return self.p.dot_mirrors.prepare(self.mid, self.event, phase, **kwargs)

    def commit(self, prepared, target='Sentinel_' + '1' * 32, receipt_source='api_response', **changes):
        arguments = {'mirror_id': prepared['mirror_id'], 'attempt_id': prepared['attempt_id'],
                     'event_id': self.event, 'target_dot_message_id': target, 'receipt_source': receipt_source}
        arguments.update(changes)
        return self.p.dot_mirrors.commit(**arguments)

    def test_prepare_duplicate_claim_and_metadata_projection_not_visibility(self):
        prepared = self.prepare()
        self.assertTrue(prepared['claim_granted'])
        self.assertEqual(len(prepared['attempt_id']), 32)
        duplicate = self.prepare()
        self.assertEqual(duplicate['mirror_id'], prepared['mirror_id'])
        self.assertFalse(duplicate['claim_granted'])
        self.assertIsNone(duplicate['attempt_id'])
        status = self.p.dot_mirrors.for_message(self.mid)
        self.assertEqual((status['source_event_id'], status['source_event_associated']), (self.event, True))
        self.assertEqual(status['dot_mirror_status'], 'tracked')
        self.assertFalse(status['all_parts_verified'])
        self.assertNotIn('attempt_id', status['records'][0])
        self.assertFalse(status['records'][0]['target_id_verified_by_bridge'])
        self.assertEqual(self.p.weixin.read_message(self.mid)['reply_state']['dot_mirror_status'], 'tracked')
        safe = json.dumps(prepared) + json.dumps(status)
        for secret in ('BODY_CANARY', 'TOKEN_CANARY', 'CONTEXT_CANARY', 'fixture-owner'):
            self.assertNotIn(secret, safe)
        columns = {x['name'] for x in self.p.db.execute('PRAGMA table_info(weixin_dot_mirrors)')}
        self.assertTrue(columns.isdisjoint({'text', 'body', 'payload', 'token', 'secret', 'url'}))

    def test_receipt_commit_idempotent_conflict_and_attempt_event_fence(self):
        prepared = self.prepare()
        for changes in ({'attempt_id': 'wrong_attempt'}, {'event_id': 'wrong_event'}):
            with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
                self.commit(prepared, **changes)
        with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
            self.p.dot_mirrors.mark_unknown(prepared['mirror_id'], 'wrong_attempt')
        first = self.commit(prepared)
        self.assertEqual(self.commit(prepared), first)
        with self.assertRaisesRegex(WeixinError, 'receipt_conflict'):
            self.commit(prepared, target='Sentinel_' + '2' * 32)
        with self.assertRaisesRegex(WeixinError, 'already_committed'):
            self.p.dot_mirrors.mark_unknown(prepared['mirror_id'], prepared['attempt_id'])
        self.assertEqual(first['state'], 'committed')
        self.assertTrue(first['suppress_reflection'])
        self.assertEqual(first['direction'], 'weixin_to_dot')
        self.assertFalse(self.prepare()['claim_granted'])

    def test_unknown_never_reclaims_and_requires_correlated_history(self):
        prepared = self.prepare()
        marked = self.p.dot_mirrors.mark_unknown(prepared['mirror_id'], prepared['attempt_id'])
        self.assertEqual(marked['state'], 'unknown')
        self.assertEqual(self.p.dot_mirrors.mark_unknown(prepared['mirror_id'], prepared['attempt_id']), marked)
        self.assertFalse(self.prepare()['claim_granted'])
        with self.assertRaisesRegex(WeixinError, 'history_required'):
            self.commit(prepared)
        with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
            self.commit(prepared, receipt_source='history_readback', attempt_id='stale_attempt')
        resolved = self.commit(prepared, receipt_source='history_readback')
        self.assertEqual((resolved['state'], resolved['receipt_source']), ('committed', 'history_readback'))
        self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_concurrent_unknown_transition_cannot_accept_stale_api_receipt(self):
        prepared = self.prepare()
        second = Probe(self.tmp.name)
        original = self.p.dot_mirrors.authorized
        def raced_authorization(mirror_id):
            row = original(mirror_id)
            second.dot_mirrors.mark_unknown(mirror_id, prepared['attempt_id'])
            return row
        try:
            with patch.object(self.p.dot_mirrors, 'authorized', side_effect=raced_authorization):
                with self.assertRaisesRegex(WeixinError, 'history_required'):
                    self.commit(prepared)
            self.assertEqual(self.p.dot_mirrors.for_message(self.mid)['records'][0]['state'], 'unknown')
        finally:
            second.close()

    def test_restart_read_does_not_recover_daemon_marks_prepared_unknown(self):
        prepared = self.prepare()
        second = Probe(self.tmp.name)
        try:
            self.assertEqual(second.dot_mirrors.for_message(self.mid)['records'][0]['state'], 'prepared')
            second.dot_mirrors.recover_inflight()
            self.assertEqual(second.dot_mirrors.for_message(self.mid)['records'][0]['state'], 'unknown')
            self.assertFalse(second.dot_mirrors.prepare(self.mid, self.event, 'original')['claim_granted'])
        finally:
            second.close()

    def test_owner_event_notice_and_attachment_scope_rejected(self):
        for fake in ('notice_fixture', 'Sentinel_fixture', 'wx_missing'):
            with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
                self.p.dot_mirrors.prepare(fake, self.event, 'original')
        unlinked, _ = self.inbound('no-event', linked=False)
        with self.assertRaisesRegex(WeixinError, 'event_mismatch'):
            self.p.dot_mirrors.prepare(unlinked, self.event, 'original')
        with self.assertRaisesRegex(WeixinError, 'event_mismatch'):
            self.p.dot_mirrors.prepare(self.mid, 'wrong', 'original')
        self.assertEqual(self.inbound('other', from_user_id='other-owner'), (None, None))
        aid = 'att_' + 'a' * 32
        other, _ = self.inbound('second')
        with self.p.db:
            self.p.db.execute("INSERT INTO weixin_attachments(id,message_id,account,peer,kind,filename,status,created_at,expires_at,provenance) VALUES(?,?,?,?,'image','fixture.png','error',0,1,'{}')", (aid, other, 'fixture-bot', 'fixture-owner'))
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_attachment'):
            self.prepare(attachment_id=aid)
        with self.p.db:
            self.p.db.execute('UPDATE weixin_attachments SET message_id=? WHERE id=?', (self.mid, aid))
        original = self.prepare(attachment_id=aid)
        self.assertEqual(original['attachment_id'], aid)
        with self.assertRaisesRegex(WeixinError, 'attachment_requires_original'):
            self.prepare(phase='final', attachment_id=aid)
        with self.p.db:
            self.p.db.execute("UPDATE weixin_accounts SET owner='changed-owner'")
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.p.dot_mirrors.mark_unknown(original['mirror_id'], original['attempt_id'])

    def test_committed_reply_slots_and_legacy_final_supported(self):
        with self.assertRaisesRegex(WeixinError, 'reply_not_committed'):
            self.prepare(phase='final')
        self.p.weixin.queue_reply_phase(self.mid, 'ack', 'synthetic ack')
        ack = self.prepare(phase='ack')
        self.p.weixin.queue_reply_phase(self.mid, 'final', 'synthetic final')
        final = self.prepare(phase='final')
        self.assertNotEqual(ack['mirror_id'], final['mirror_id'])
        self.assertNotEqual(ack['attempt_id'], final['attempt_id'])
        with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
            self.commit(final, attempt_id=ack['attempt_id'])
        with self.p.db:
            self.p.db.execute('UPDATE weixin_outbox SET source_message_id=NULL WHERE message_id=?', (self.mid,))
        self.assertEqual(self.prepare(phase='final')['mirror_id'], final['mirror_id'])

    def test_concurrent_connections_only_one_first_claim_and_one_receipt(self):
        peers = [Probe(self.tmp.name) for _ in range(6)]
        barrier = threading.Barrier(len(peers))
        def prepare(peer):
            barrier.wait()
            return peer.dot_mirrors.prepare(self.mid, self.event, 'original')
        try:
            with ThreadPoolExecutor(max_workers=len(peers)) as pool:
                results = list(pool.map(prepare, peers))
            winners = [r for r in results if r['claim_granted']]
            self.assertEqual(len(winners), 1)
            self.assertEqual(len({r['mirror_id'] for r in results}), 1)
            self.assertEqual(sum(r['attempt_id'] is not None for r in results), 1)
            prepared = winners[0]
            barrier = threading.Barrier(len(peers))
            def commit(peer):
                barrier.wait()
                return peer.dot_mirrors.commit(prepared['mirror_id'], prepared['attempt_id'], self.event, 'Sentinel_' + '3' * 32, 'api_response')
            with ThreadPoolExecutor(max_workers=len(peers)) as pool:
                receipts = list(pool.map(commit, peers))
            self.assertTrue(all(r['target_dot_message_id'] == 'Sentinel_' + '3' * 32 for r in receipts))
            self.assertEqual(self.p.db.execute('SELECT COUNT(*) FROM weixin_dot_mirrors').fetchone()[0], 1)
        finally:
            for peer in peers:
                peer.close()

    def test_strict_mcp_schema_local_version_and_no_body_or_reflection_override(self):
        def rpc(name, args):
            return self.p.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args}})
        args = {'message_id': self.mid, 'event_id': self.event, 'phase': 'original'}
        for extra in ({'body': 'forbidden'}, {'suppress_reflection': 'false'}, {'target_room': 'other'}):
            self.assertIn('error', rpc('prepare_weixin_dot_mirror', {**args, **extra}))
        self.assertIn('error', rpc('prepare_weixin_dot_mirror', {**args, 'attachment_id': None}))
        receipt = rpc('prepare_weixin_dot_mirror', args)['result']['structuredContent']
        self.assertTrue(receipt['claim_granted'])
        status = rpc('get_weixin_dot_mirror_status', {'message_id': self.mid})['result']['structuredContent']
        self.assertEqual(status['bridge_version'], BUILD_VERSION)
        self.assertEqual(status['dot_mirror_ledger_version'], 'ledger-v1')
        for method in ('initialize', 'server/discover'):
            result = self.p.rpc({'jsonrpc': '2.0', 'id': 2, 'method': method, 'params': {}})['result']
            self.assertEqual(result['serverInfo']['version'], BUILD_VERSION)
        self.assertEqual(self.p.status()['bridge_version'], BUILD_VERSION)
        for invalid in ('arbitrary_text', 'https://example.com/private', 'sk-SYNTHETIC_FIXTURE_00000000',
                        'Sentinel_' + 'A' * 32, 'Sentinel_' + '1' * 31):
            with self.assertRaisesRegex(WeixinError, 'invalid_target_dot_message_id'):
                self.commit(receipt, target=invalid)


if __name__ == '__main__':
    unittest.main()
