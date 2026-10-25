"""Synthetic receipt reconciliation only: no external sends or remote-ID proof."""
import json
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from probe.probe import Probe
from probe.weixin import WeixinError
from probe_tests import test_dotledger as fixtures


class LedgerReconciliationTests(unittest.TestCase):
    def setUp(self):
        for name in ('socket.getaddrinfo', 'socket.create_connection',
                     'socket.socket.connect', 'subprocess.Popen', 'os.system'):
            guard = patch(name, side_effect=AssertionError('offline reconciliation forbids IO'))
            guard.start()
            self.addCleanup(guard.stop)
        self.fixture = fixtures.DotLedgerTests('test_prepare_duplicate_claim_and_metadata_projection_not_visibility')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.p, self.mid, self.event = self.fixture.p, self.fixture.mid, self.fixture.event

    def status(self, probe=None):
        return (probe or self.p).dot_mirrors.for_message(self.mid)

    def recovery(self, probe=None):
        return self.status(probe)['reconciliation']['records'][0]

    def receipt(self, record, target='Sentinel_' + '1' * 32, source='history_readback', probe=None):
        return (probe or self.p).dot_mirrors.commit(record['mirror_id'], record['attempt_id'],
            record['event_id'], target, source)

    def peer(self):
        peer = Probe(self.fixture.tmp.name)
        self.addCleanup(peer.close)
        return peer

    def assert_not_authorized_to_send(self, projection):
        self.assertTrue(projection['reconciliation_only'])
        self.assertFalse(projection['claim_granted'])
        self.assertFalse(projection['external_write_allowed'])
        self.assertFalse(projection['automatic_resend_allowed'])

    def test_empty_query_creates_no_claim_and_no_attempt(self):
        changes = self.p.db.total_changes
        with patch('probe.dotledger.uuid.uuid4', side_effect=AssertionError('query generated attempt')):
            status = self.status()
        self.assertEqual(status['records'], [])
        self.assertEqual(status['reconciliation']['records'], [])
        self.assert_not_authorized_to_send(status['reconciliation'])
        self.assertEqual(self.p.db.total_changes, changes)
        self.assertFalse(self.p.db.in_transaction)

    def test_lost_prepare_response_recovers_same_attempt_without_reclaim(self):
        original = self.fixture.prepare()
        duplicate = self.fixture.prepare()
        self.assertFalse(duplicate['claim_granted'])
        self.assertIsNone(duplicate['attempt_id'])
        recovered = self.recovery()
        self.assertEqual(recovered['attempt_id'], original['attempt_id'])
        self.assertEqual(recovered['event_id'], original['event_id'])
        self.assertEqual(recovered['mirror_id'], original['mirror_id'])
        self.assert_not_authorized_to_send(recovered)
        self.assertTrue(recovered['existing_target_receipt_required'])
        self.assertTrue(recovered['available'])
        self.assertIsNone(recovered['unavailable_reason'])
        self.assertNotIn('attempt_id', self.status()['records'][0])
        self.assertEqual(self.receipt(recovered)['state'], 'committed')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_lost_commit_response_reads_existing_target_and_conflict_stays_rejected(self):
        prepared = self.fixture.prepare()
        self.fixture.commit(prepared)
        recovered = self.recovery()
        self.assertEqual(recovered['state'], 'committed')
        self.assertEqual(recovered['target_dot_message_id'], 'Sentinel_' + '1' * 32)
        self.assertEqual(recovered['attempt_id'], prepared['attempt_id'])
        first = self.receipt(recovered)
        self.assertEqual(first, self.receipt(recovered))
        with self.assertRaisesRegex(WeixinError, 'receipt_conflict'):
            self.receipt(recovered, target='Sentinel_' + '2' * 32)
        self.assertEqual(self.recovery()['target_dot_message_id'], 'Sentinel_' + '1' * 32)
        self.assertFalse(self.fixture.prepare()['claim_granted'])

    def test_unknown_recovered_attempt_still_requires_existing_history_receipt(self):
        original = self.fixture.prepare()
        self.p.dot_mirrors.mark_unknown(original['mirror_id'], original['attempt_id'])
        changes = self.p.db.total_changes
        recovered = self.recovery()
        self.assertEqual(self.p.db.total_changes, changes)
        self.assertEqual(recovered['state'], 'unknown')
        self.assert_not_authorized_to_send(recovered)
        with self.assertRaisesRegex(WeixinError, 'history_required'):
            self.receipt(recovered, source='api_response')
        self.assertFalse(self.fixture.prepare()['claim_granted'])
        self.assertEqual(self.receipt(recovered)['receipt_source'], 'history_readback')

    def test_restart_query_preserves_prepared_until_explicit_daemon_recovery(self):
        original = self.fixture.prepare()
        peer = self.peer()
        self.assertEqual(self.recovery(peer)['state'], 'prepared')
        peer.dot_mirrors.recover_inflight()
        recovered = self.recovery(peer)
        self.assertEqual(recovered['state'], 'unknown')
        self.assertEqual(recovered['attempt_id'], original['attempt_id'])
        self.assertFalse(peer.dot_mirrors.prepare(self.mid, self.event, 'original')['claim_granted'])
        self.assertEqual(self.receipt(recovered, probe=peer)['state'], 'committed')

    def test_query_keeps_outer_transaction_and_rollback_intact(self):
        self.fixture.prepare()
        self.p.db.execute('BEGIN IMMEDIATE')
        try:
            self.p.db.execute('UPDATE pings SET attempts=17 WHERE id=?', (self.event,))
            changes = self.p.db.total_changes
            self.status()
            self.assertTrue(self.p.db.in_transaction)
            self.assertEqual(self.p.db.total_changes, changes)
        finally:
            self.p.db.rollback()
        self.assertEqual(self.p.db.execute('SELECT attempts FROM pings WHERE id=?', (self.event,)).fetchone()[0], 0)

    def test_query_error_keeps_outer_transaction_and_no_savepoint_leak(self):
        self.p.db.execute('BEGIN IMMEDIATE')
        try:
            self.p.db.execute('UPDATE pings SET attempts=19 WHERE id=?', (self.event,))
            with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
                self.p.dot_mirrors.for_message('unowned-fixture-message')
            self.assertTrue(self.p.db.in_transaction)
            self.assertEqual(self.p.db.execute('SELECT attempts FROM pings WHERE id=?', (self.event,)).fetchone()[0], 19)
        finally:
            self.p.db.rollback()
        self.assertEqual(self.status()['reconciliation']['records'], [])

    def test_foreign_owner_query_never_returns_attempt(self):
        self.fixture.prepare()
        with self.p.db:
            self.p.db.execute("UPDATE weixin_accounts SET owner='other-fixture-owner'")
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.status()
        self.assertFalse(self.p.db.in_transaction)

    def test_ambiguous_multiple_bindings_fail_closed_and_empty_stays_not_bound(self):
        self.fixture.prepare()
        with self.p.db:
            self.p.db.execute("""INSERT INTO weixin_accounts(id,owner,token,base_url)
                SELECT 'second-fixture-bot','second-fixture-owner',token,base_url
                FROM weixin_accounts LIMIT 1""")
        with self.assertRaisesRegex(WeixinError, 'dot_mirror_owner_unavailable'):
            self.status()
        self.assertFalse(self.p.db.in_transaction)
        with self.p.db:
            self.p.db.execute('DELETE FROM weixin_accounts')
        with self.assertRaisesRegex(WeixinError, 'not_bound'):
            self.status()
        self.assertFalse(self.p.db.in_transaction)

    def test_broken_event_association_preserves_old_metadata_without_attempt(self):
        original = self.fixture.prepare()
        with self.p.db:
            self.p.db.execute("UPDATE weixin_dot_mirrors SET event_id='unrelated-fixture-event'")
        recovered = self.recovery()
        self.assertFalse(recovered['available'])
        self.assertIsNone(recovered['attempt_id'])
        self.assertEqual(recovered['unavailable_reason'], 'dot_mirror_event_mismatch')
        self.assertEqual(self.status()['records'][0]['mirror_id'], original['mirror_id'])

    def test_deleted_source_ping_never_exposes_recovery_attempt(self):
        self.fixture.prepare()
        with self.p.db:
            self.p.db.execute('DELETE FROM pings WHERE id=?', (self.event,))
        status = self.status()
        self.assertFalse(status['source_event_associated'])
        self.assertFalse(status['reconciliation']['records'][0]['available'])
        self.assertIsNone(status['reconciliation']['records'][0]['attempt_id'])

    def add_attachment(self):
        aid = 'att_' + 'a' * 32
        with self.p.db:
            self.p.db.execute("INSERT INTO weixin_attachments(id,message_id,account,peer,kind,filename,status,created_at,expires_at,provenance) VALUES(?,?,?,?,'image','synthetic.png','error',0,1,'{}')",
                (aid, self.mid, 'fixture-bot', 'fixture-owner'))
        return aid

    def test_attachment_and_reply_phases_keep_their_own_attempts(self):
        originals = [self.fixture.prepare(), self.fixture.prepare(attachment_id=self.add_attachment())]
        self.p.weixin.queue_reply_phase(self.mid, 'ack', 'synthetic acknowledgement')
        originals.append(self.fixture.prepare('ack'))
        self.p.weixin.queue_reply_phase(self.mid, 'final', 'synthetic final')
        originals.append(self.fixture.prepare('final'))
        status = self.status()
        recovered = {r['mirror_id']: r for r in status['reconciliation']['records']}
        self.assertEqual(len(recovered), 4)
        self.assertEqual(len({r['attempt_id'] for r in recovered.values()}), 4)
        for old in originals:
            now = recovered[old['mirror_id']]
            self.assertEqual((now['attempt_id'], now['phase'], now['attachment_id']),
                             (old['attempt_id'], old['phase'], old['attachment_id']))
            self.assert_not_authorized_to_send(now)
        ack = next(r for r in recovered.values() if r['phase'] == 'ack')
        final = next(r for r in recovered.values() if r['phase'] == 'final')
        with self.assertRaisesRegex(WeixinError, 'attempt_mismatch'):
            self.p.dot_mirrors.commit(final['mirror_id'], ack['attempt_id'], self.event,
                                     'Sentinel_' + '3' * 32, 'history_readback')

    def test_attachment_reassociation_hides_the_old_attempt(self):
        aid = self.add_attachment()
        self.fixture.prepare(attachment_id=aid)
        other, _ = self.fixture.inbound('second')
        with self.p.db:
            self.p.db.execute('UPDATE weixin_attachments SET message_id=? WHERE id=?', (other, aid))
        recovered = self.recovery()
        self.assertFalse(recovered['available'])
        self.assertIsNone(recovered['attempt_id'])
        self.assertEqual(recovered['unavailable_reason'], 'unknown_owner_attachment')

    def test_terminal_event_and_missing_subscription_do_not_block_reconciliation(self):
        original = self.fixture.prepare()
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0], 0)
        for state in ('failed', 'cancelled', 'delivered'):
            with self.p.db:
                self.p.db.execute('UPDATE pings SET status=? WHERE id=?', (state, self.event))
            recovered = self.recovery()
            self.assertTrue(recovered['available'])
            self.assertEqual(recovered['attempt_id'], original['attempt_id'])
            self.assertFalse(recovered['automatic_resend_allowed'])

    def test_read_snapshot_stays_coherent_across_concurrent_commit(self):
        original = self.fixture.prepare()
        peer = self.peer()
        authorized = self.p.dot_mirrors.authorized
        def concurrent(mirror_id):
            row = authorized(mirror_id)
            self.receipt(original, probe=peer)
            return row
        with patch.object(self.p.dot_mirrors, 'authorized', side_effect=concurrent):
            old = self.recovery()
        self.assertEqual(old['state'], 'prepared')
        self.assertIsNone(old['target_dot_message_id'])
        new = self.recovery()
        self.assertEqual(new['state'], 'committed')
        self.assertEqual(new['target_dot_message_id'], 'Sentinel_' + '1' * 32)
        self.assertEqual(old['attempt_id'], new['attempt_id'])

    def test_read_snapshot_does_not_mix_a_new_owner_with_an_old_attempt(self):
        original = self.fixture.prepare()
        peer = self.peer()
        owner_message = self.p.dot_mirrors.owner_message
        changed = False
        def concurrent(*args, **kwargs):
            nonlocal changed
            row = owner_message(*args, **kwargs)
            if not changed:
                changed = True
                with peer.db:
                    peer.db.execute("UPDATE weixin_accounts SET owner='other-fixture-owner'")
            return row
        with patch.object(self.p.dot_mirrors, 'owner_message', side_effect=concurrent):
            snapshot = self.recovery()
        self.assertEqual(snapshot['attempt_id'], original['attempt_id'])
        self.assertTrue(snapshot['available'])
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.status()
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.receipt(snapshot)

    def test_parallel_queries_and_commit_keep_same_attempt_and_single_ledger_row(self):
        original = self.fixture.prepare()
        peers = [self.peer() for _ in range(6)]
        barrier = threading.Barrier(len(peers))
        def worker(item):
            index, peer = item
            barrier.wait()
            if index == 0:
                self.receipt(original, probe=peer)
            return self.recovery(peer)
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            records = list(pool.map(worker, enumerate(peers)))
        self.assertEqual({r['attempt_id'] for r in records}, {original['attempt_id']})
        self.assertTrue(all(r['state'] in ('prepared', 'committed') for r in records))
        self.assertTrue(all((r['target_dot_message_id'] is None) == (r['state'] == 'prepared') for r in records))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_mirrors').fetchone()[0], 1)

    def test_query_never_selects_body_credentials_or_callback_material(self):
        self.fixture.prepare()
        forbidden = {'text', 'body', 'token', 'bot_token', 'context_token', 'secret', 'url'}
        def authorize(action, arg1, arg2, db, trigger):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and arg2 in forbidden else sqlite3.SQLITE_OK
        self.p.db.set_authorizer(authorize)
        try:
            status = self.status()
        finally:
            self.p.db.set_authorizer(None)
        for canary in ('BODY_CANARY', 'TOKEN_CANARY', 'CONTEXT_CANARY', 'fixture-owner'):
            self.assertNotIn(canary, json.dumps(status))
        self.assertFalse(status['reconciliation']['records'][0]['target_id_verified_by_bridge'])

    def test_rpc_read_contract_adds_metadata_without_any_new_input_authority(self):
        self.fixture.prepare()
        def rpc(args):
            return self.p.rpc({'jsonrpc':'2.0', 'id':1, 'method':'tools/call',
                'params': {'name':'get_weixin_dot_mirror_status', 'arguments':args}})
        result = rpc({'message_id':self.mid})['result']['structuredContent']
        self.assertEqual(result['reconciliation']['version'], 'ledger-reconciliation-v1')
        self.assert_not_authorized_to_send(result['reconciliation'])
        for extra in ({'allow_resend':'true'}, {'attempt_id':'new'}, {'owner':'other'}):
            self.assertIn('error', rpc({'message_id':self.mid, **extra}))
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
