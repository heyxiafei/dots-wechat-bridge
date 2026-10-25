"""Exact owner/event observations only; synthetic SQLite, no outbound I/O."""
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import test_event_pump as fixtures
from probe.weixin import MESSAGE_EVENT, WeixinError

ACCOUNT, OWNER = fixtures.ACCOUNT, fixtures.OWNER


class EventDeliveryStatusTests(unittest.TestCase):
    # Reuse setup helpers, not the existing test class's 24 test cases.
    setUp_fixture = fixtures.EventDeliveryLeaseTests.setUp
    close_peers = fixtures.EventDeliveryLeaseTests.close_peers
    open_probe = fixtures.EventDeliveryLeaseTests.open_probe
    receiver = fixtures.EventDeliveryLeaseTests.receiver
    params = staticmethod(fixtures.EventDeliveryLeaseTests.params)
    bind = fixtures.EventDeliveryLeaseTests.bind
    inbound = fixtures.EventDeliveryLeaseTests.inbound
    row = fixtures.EventDeliveryLeaseTests.row

    def setUp(self):
        self.setUp_fixture()
        for target in ('socket.getaddrinfo', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('network forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.bind()

    def linked(self, source='status-fixture'):
        self.p.subscribe(self.params(MESSAGE_EVENT))
        mid, added = self.inbound(source)
        self.assertEqual(added, 1)
        self.assertEqual(self.p.queue_weixin_messages(), 1)
        row = self.p.db.execute('SELECT event_id FROM weixin_inbox WHERE id=?', (mid,)).fetchone()
        return mid, row['event_id']

    def query(self, mid, probe=None):
        return (probe or self.p).event_delivery.for_message(mid)

    def assert_no_authority(self, result):
        for flag in ('claim_granted', 'external_write_allowed', 'automatic_replay_allowed'):
            self.assertIs(result[flag], False, flag)

    def assert_no_foreign_event_details(self, result):
        self.assertEqual(result['assignment_status'], 'association_invalid')
        self.assertEqual(result['dispatch_blocked_reason'], 'association_invalid')
        for field in ('status', 'attempts', 'last_http_status', 'last_error_kind', 'lease_until'):
            self.assertIsNone(result[field], field)
        self.assertFalse(result['lease_active'])
        self.assertFalse(result['subscription']['present'])

    def test_exact_queued_event_projection_has_no_secret_or_write_authority(self):
        mid, eid = self.linked()
        result = self.query(mid)
        self.assertTrue(result['event_delivery_version'])
        self.assertEqual((result['event_id'], result['assignment_status'], result['status']),
                         (eid, 'associated', 'queued'))
        self.assertEqual(result['attempts'], 0)
        self.assertTrue(result['subscription']['present'])
        self.assertTrue(result['subscription']['active'])
        self.assertIsNone(result['subscription']['expires_at'])
        self.assertIsNone(result['dispatch_blocked_reason'])
        self.assert_no_authority(result)
        serialized = json.dumps(result)
        for value in (ACCOUNT, OWNER, '合成原文', 'synthetic-secret-fixture-0000000000000000'):
            self.assertNotIn(value, serialized)
        forbidden = {'lease_token', 'url', 'secret', 'old_secret', 'body', 'nonce', 'account', 'owner'}
        self.assertTrue(forbidden.isdisjoint(result))
        self.assertTrue(forbidden.isdisjoint(result['subscription']))

    def test_old_failed_event_is_exactly_visible_after_recent_window_eviction(self):
        mid, eid = self.linked()
        self.handler = lambda event: (413, b'{}')
        self.p.deliver_once()
        self.p.subscribe(self.params())
        for _ in range(11):
            self.p.emit()
            self.p.deliver_once()
        self.assertNotIn(eid, [r['id'] for r in self.p.status()['events']])
        result = self.query(mid)
        self.assertEqual((result['event_id'], result['status'], result['attempts']), (eid, 'failed', 1))
        self.assertEqual(result['last_http_status'], 413)
        self.assertEqual(result['last_error_kind'], 'webhook_permanent_http')
        self.assertEqual(result['dispatch_blocked_reason'], 'event_failed')
        self.assert_no_authority(result)

    def test_cancelled_event_preserves_original_identity_and_terminal_reason(self):
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute("UPDATE pings SET status='cancelled' WHERE id=?", (eid,))
        result = self.query(mid)
        self.assertEqual((result['event_id'], result['status']), (eid, 'cancelled'))
        self.assertEqual(result['dispatch_blocked_reason'], 'event_cancelled')
        self.assert_no_authority(result)

    def test_expired_subscription_leaves_queued_event_observable_without_cancelling(self):
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute('UPDATE subscriptions SET expires=?', (self.now - 1,))
        before = self.p.db.total_changes
        result = self.query(mid)
        self.assertEqual(result['status'], 'queued')
        self.assertTrue(result['subscription']['present'])
        self.assertFalse(result['subscription']['active'])
        self.assertEqual(result['subscription']['expires_at'], self.now - 1)
        self.assertEqual(result['dispatch_blocked_reason'], 'subscription_expired')
        self.assertEqual(self.row(eid)['status'], 'queued')
        self.assertEqual(self.p.db.total_changes, before)

    def test_missing_subscription_is_distinct_from_expired_subscription(self):
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute('DELETE FROM subscriptions')
        result = self.query(mid)
        self.assertEqual((result['event_id'], result['status']), (eid, 'queued'))
        self.assertFalse(result['subscription']['present'])
        self.assertFalse(result['subscription']['active'])
        self.assertIsNone(result['subscription']['expires_at'])
        self.assertEqual(result['dispatch_blocked_reason'], 'subscription_missing')

    def test_unassigned_message_without_subscription_stays_unassigned(self):
        mid, _ = self.inbound('unassigned-no-sub')
        result = self.query(mid)
        self.assertEqual(result['assignment_status'], 'unassigned')
        self.assertIsNone(result['event_id'])
        self.assertIsNone(result['status'])
        self.assertEqual(result['dispatch_blocked_reason'], 'event_unassigned')
        self.assertFalse(result['subscription']['present'])
        self.assertFalse(result['subscription']['active'])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM pings').fetchone()[0], 0)
        self.assert_no_authority(result)

    def test_unassigned_message_does_not_borrow_current_subscription_or_dispatch(self):
        self.p.subscribe(self.params(MESSAGE_EVENT))
        mid, _ = self.inbound('unassigned-with-sub')
        result = self.query(mid)
        self.assertEqual(result['assignment_status'], 'unassigned')
        self.assertFalse(result['subscription']['present'])
        self.assertFalse(result['subscription']['active'])
        self.assertIsNone(self.p.db.execute('SELECT event_id FROM weixin_inbox WHERE id=?', (mid,)).fetchone()[0])
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM pings').fetchone()[0], 0)

    def test_future_queued_lease_is_reported_without_returning_lease_token(self):
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute('UPDATE pings SET lease_until=?,lease_token=? WHERE id=?',
                              (self.now + 30, 'LEASE_CANARY', eid))
        result = self.query(mid)
        self.assertTrue(result['lease_active'])
        self.assertEqual(result['lease_until'], self.now + 30)
        self.assertEqual(result['dispatch_blocked_reason'], 'event_leased')
        self.assertNotIn('LEASE_CANARY', json.dumps(result))
        self.assertNotIn('lease_token', result)

    def test_expired_lease_is_observed_without_releasing_or_reclaiming(self):
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute('UPDATE pings SET lease_until=?,lease_token=?,attempts=2 WHERE id=?',
                              (self.now - 1, 'LEASE_CANARY', eid))
        before = self.row(eid)
        result = self.query(mid)
        self.assertFalse(result['lease_active'])
        self.assertEqual(result['attempts'], 2)
        self.assertIsNone(result['dispatch_blocked_reason'])
        self.assertEqual(self.row(eid), before)
        self.assert_no_authority(result)

    def test_terminal_event_cannot_report_an_active_lease(self):
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute("UPDATE pings SET status='delivered',lease_until=?,lease_token=? WHERE id=?",
                              (self.now + 30, 'LEASE_CANARY', eid))
        result = self.query(mid)
        self.assertEqual(result['status'], 'delivered')
        self.assertFalse(result['lease_active'])
        self.assertIsNone(result['dispatch_blocked_reason'])

    def test_retry_backoff_is_visible_without_advancing_retry_deadline(self):
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute('UPDATE pings SET next_at=?,attempts=1 WHERE id=?', (self.now + 12, eid))
        result = self.query(mid)
        self.assertEqual(result['next_at'], self.now + 12)
        self.assertEqual(result['dispatch_blocked_reason'], 'retry_backoff')
        self.assertEqual(self.row(eid)['next_at'], self.now + 12)

    def test_foreign_message_event_pointer_does_not_expose_its_details(self):
        mid, _ = self.linked()
        other, _ = self.inbound('foreign-event-source')
        self.p.queue_weixin_messages()
        foreign = self.p.db.execute('SELECT event_id FROM weixin_inbox WHERE id=?', (other,)).fetchone()[0]
        with self.p.db:
            self.p.db.execute("UPDATE weixin_inbox SET peer='foreign-owner' WHERE id=?", (other,))
            self.p.db.execute('UPDATE weixin_inbox SET event_id=? WHERE id=?', (foreign, mid))
            self.p.db.execute("UPDATE pings SET attempts=5,last_http_status=429,last_error_kind='FOREIGN_CANARY',lease_until=? WHERE id=?",
                              (self.now + 100, foreign))
        result = self.query(mid)
        self.assert_no_foreign_event_details(result)
        self.assertNotIn('FOREIGN_CANARY', json.dumps(result))

    def test_wrong_event_name_or_missing_event_is_invalid_not_unassigned(self):
        mid, eid = self.linked()
        with self.p.db:
            self.p.db.execute("UPDATE pings SET name='test.ping',last_error_kind='FOREIGN_CANARY' WHERE id=?", (eid,))
        self.assert_no_foreign_event_details(self.query(mid))
        with self.p.db:
            self.p.db.execute('DELETE FROM pings WHERE id=?', (eid,))
        result = self.query(mid)
        self.assert_no_foreign_event_details(result)
        self.assertNotIn('FOREIGN_CANARY', json.dumps(result))

    def test_unknown_and_cross_owner_message_queries_are_rejected(self):
        mid, _ = self.linked()
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.query('fixture-missing-message')
        with self.p.db:
            self.p.db.execute("UPDATE weixin_inbox SET peer='foreign-owner' WHERE id=?", (mid,))
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.query(mid)

    def test_binding_owner_change_cannot_read_previous_owners_event(self):
        mid, _ = self.linked()
        with self.p.db:
            self.p.db.execute("UPDATE weixin_accounts SET owner='new-fixture-owner'")
        with self.assertRaisesRegex(WeixinError, 'unknown_owner_message'):
            self.query(mid)

    def test_repeated_lookup_is_select_only_and_never_wakes_or_changes_state(self):
        mid, _ = self.linked()
        self.p.event_wakeup.clear()
        self.p.sender_wakeup.clear()
        before = (self.p.db.total_changes, tuple(self.p.db.iterdump()))
        trace = []
        self.p.db.set_trace_callback(trace.append)
        try:
            first = self.query(mid)
            self.assertEqual(self.query(mid), first)
        finally:
            self.p.db.set_trace_callback(None)
        self.assertEqual((self.p.db.total_changes, tuple(self.p.db.iterdump())), before)
        self.assertFalse(self.p.event_wakeup.is_set())
        self.assertFalse(self.p.sender_wakeup.is_set())
        self.assertEqual(self.calls, [])
        for sql in trace:
            self.assertFalse(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'REPLACE', 'ALTER', 'CREATE', 'DROP')), sql)

    def test_reopen_and_parallel_readers_never_create_new_events_or_claims(self):
        mid, eid = self.linked()
        peers = [self.open_probe() for _ in range(4)]
        barrier = threading.Barrier(len(peers))
        before = (self.p.db.execute('SELECT count(*) FROM pings').fetchone()[0],
                  self.p.db.execute('SELECT count(*) FROM weixin_dot_mirrors').fetchone()[0])
        def read(peer):
            barrier.wait()
            return self.query(mid, peer)
        with ThreadPoolExecutor(max_workers=len(peers)) as pool:
            results = list(pool.map(read, peers))
        self.assertTrue(all(r['event_id'] == eid and r['status'] == 'queued' for r in results))
        for result in results:
            self.assert_no_authority(result)
        self.assertEqual((self.p.db.execute('SELECT count(*) FROM pings').fetchone()[0],
                          self.p.db.execute('SELECT count(*) FROM weixin_dot_mirrors').fetchone()[0]), before)
        self.assertEqual(self.calls, [])

    def test_cross_connection_event_and_subscription_snapshot_is_coherent(self):
        mid, eid = self.linked()
        peer = self.open_probe()
        before_expiry, after_expiry = self.now + 90, self.now + 190
        with self.p.db:
            self.p.db.execute('UPDATE subscriptions SET expires=?', (before_expiry,))
        triggered = []
        def trace(sql):
            upper = sql.upper()
            if not triggered and upper.lstrip().startswith('SELECT') and 'FROM SUBSCRIPTIONS' in upper:
                triggered.append(True)
                with peer.db:
                    peer.db.execute('UPDATE pings SET attempts=1 WHERE id=?', (eid,))
                    peer.db.execute('UPDATE subscriptions SET expires=?', (after_expiry,))
        self.p.db.set_trace_callback(trace)
        try:
            result = self.query(mid)
        finally:
            self.p.db.set_trace_callback(None)
        self.assertEqual(triggered, [True])
        self.assertEqual(peer.db.execute('SELECT attempts FROM pings WHERE id=?', (eid,)).fetchone()[0], 1)
        self.assertEqual(peer.db.execute('SELECT expires FROM subscriptions').fetchone()[0], after_expiry)
        self.assertIn((result['attempts'], result['subscription']['expires_at']),
                      ((0, before_expiry), (1, after_expiry)))
        self.assert_no_authority(result)

    def test_lookup_does_not_commit_or_rollback_an_existing_caller_transaction(self):
        mid, eid = self.linked()
        peer = self.open_probe()
        self.p.db.execute('BEGIN IMMEDIATE')
        self.p.db.execute('UPDATE pings SET attempts=3 WHERE id=?', (eid,))
        try:
            result = self.query(mid)
            self.assertEqual(result['attempts'], 3)
            self.assertTrue(self.p.db.in_transaction)
            self.assertEqual(peer.db.execute('SELECT attempts FROM pings WHERE id=?', (eid,)).fetchone()[0], 0)
        finally:
            self.p.db.rollback()
        self.assertEqual(self.row(eid)['attempts'], 0)

    def test_exact_mcp_owner_read_adds_event_status_without_new_tool_or_claim(self):
        mid, eid = self.linked()
        before = self.p.db.execute('SELECT count(*) FROM weixin_dot_mirrors').fetchone()[0]
        rpc = self.p.rpc({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                          'params': {'name': 'read_weixin_message', 'arguments': {'message_id': mid}}})
        self.assertNotIn('error', rpc)
        result = rpc['result']['structuredContent']['event_delivery']
        self.assertEqual(result['event_id'], eid)
        self.assert_no_authority(result)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_dot_mirrors').fetchone()[0], before)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
