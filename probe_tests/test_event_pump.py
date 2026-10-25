"""Synthetic event-pump/lease tests: temp SQLite and fake webhook callbacks only.

Webhook acceptance here is local fixture evidence, never dot or phone delivery.
No worker daemon, real account, network connection or subprocess is launched.
"""
import base64
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from probe import probe as module
from probe.weixin import MESSAGE_EVENT, WeixinError


class StepWake:
    """Deterministic Event facade; wait callbacks advance the fake worker."""
    def __init__(self, log, on_wait):
        self.log, self.on_wait, self.flag = log, on_wait, False

    def clear(self):
        self.log.append('clear')
        self.flag = False

    def set(self):
        self.flag = True

    def is_set(self):
        return self.flag

    def wait(self, timeout):
        self.log.append(('wait', timeout, self.flag))
        self.on_wait(self)
        return self.flag


class EventPumpWorkerTests(unittest.TestCase):
    def run_pump(self, fake, stop, **kwargs):
        if not hasattr(fake, 'outbound'):
            fake.outbound = SimpleNamespace(cleanup=lambda: None)
        if not hasattr(fake, 'event_next_wait'):
            fake.event_next_wait = lambda maximum=.5: maximum
        module.run_event_pump(fake, stop, **kwargs)

    def test_clear_precedes_queue_query_and_delivery_then_fallback_wait(self):
        stop, log = threading.Event(), []
        wake = StepWake(log, lambda event: stop.set())
        wake.set()
        def queue():
            log.append('queue')
            self.assertFalse(wake.is_set())
            return 0
        fake = SimpleNamespace(event_wakeup=wake, queue_weixin_messages=queue,
                               deliver_once=lambda: log.append('deliver') or False)
        self.run_pump(fake, stop)
        self.assertEqual(log, ['clear', 'queue', 'deliver', ('wait', .5, False)])

    def test_notification_after_empty_query_survives_until_wait(self):
        stop, log, calls = threading.Event(), [], []
        def on_wait(wake):
            self.assertTrue(wake.is_set(), 'clear after query would lose this notification')
        wake = StepWake(log, on_wait)
        queries = []
        def queue():
            queries.append(True)
            return 0
        def deliver():
            calls.append(True)
            if len(calls) == 2:
                stop.set()
                return False
            wake.set()  # durable commit after the worker's empty queue query
            return False
        fake = SimpleNamespace(event_wakeup=wake, queue_weixin_messages=queue,
                               deliver_once=deliver)
        self.run_pump(fake, stop)
        self.assertEqual(len(calls), 2)
        self.assertIn(('wait', .5, True), log)
        self.assertEqual(len(queries), 2)

    def test_burst_is_bounded_even_when_more_events_remain(self):
        stop, log, calls = threading.Event(), [], []
        wake = StepWake(log, lambda event: stop.set())
        fake = SimpleNamespace(event_wakeup=wake, queue_weixin_messages=lambda: 0,
                               deliver_once=lambda: calls.append(True) or True)
        self.run_pump(fake, stop, burst=8)
        self.assertEqual(len(calls), 8)
        self.assertEqual(sum(isinstance(item, tuple) for item in log), 1)

    def test_stop_is_checked_between_deliveries(self):
        stop, log, calls = threading.Event(), [], []
        wake = StepWake(log, lambda event: self.fail('stopped worker must not wait'))
        def deliver():
            calls.append(True)
            if len(calls) == 3:
                stop.set()
            return True
        fake = SimpleNamespace(event_wakeup=wake, queue_weixin_messages=lambda: 0,
                               deliver_once=deliver)
        self.run_pump(fake, stop, burst=8)
        self.assertEqual(len(calls), 3)

    def test_already_stopped_worker_never_queries_or_waits(self):
        stop = threading.Event()
        stop.set()
        fake = SimpleNamespace(event_wakeup=None,
            queue_weixin_messages=lambda: self.fail('unexpected queue query'),
            deliver_once=lambda: self.fail('unexpected delivery'))
        self.run_pump(fake, stop)

    def test_idle_notification_wakes_without_waiting_for_long_fallback(self):
        stop, entered_wait, sent, available = (threading.Event() for _ in range(4))
        class ObservedWake(threading.Event):
            def wait(self, timeout=None):
                entered_wait.set()
                return super().wait(timeout)
        wake = ObservedWake()
        def deliver():
            if available.is_set():
                sent.set()
                stop.set()
                return True
            return False
        fake = SimpleNamespace(event_wakeup=wake, queue_weixin_messages=lambda: 0,
                               deliver_once=deliver)
        worker = threading.Thread(target=self.run_pump, args=(fake, stop),
                                  kwargs={'fallback': 5}, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered_wait.wait(1), 'worker did not enter its wait')
            available.set()
            wake.set()
            self.assertTrue(sent.wait(1), 'notification failed to interrupt fallback')
        finally:
            stop.set()
            wake.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())

    def test_no_notification_still_rechecks_on_fallback(self):
        stop, log, queries = threading.Event(), [], []
        def on_wait(wake):
            if len(queries) == 2:
                stop.set()
        wake = StepWake(log, on_wait)
        def queue():
            queries.append(True)
            return 0
        fake = SimpleNamespace(event_wakeup=wake, queue_weixin_messages=queue,
                               deliver_once=lambda: False)
        self.run_pump(fake, stop, fallback=.125)
        self.assertEqual(len(queries), 2)
        self.assertEqual([x[1] for x in log if isinstance(x, tuple)], [.125, .125])

    def test_exception_uses_bounded_fallback_and_redacts_error_content(self):
        stop, log, diagnostic = threading.Event(), [], io.StringIO()
        wake = StepWake(log, lambda event: stop.set())
        def broken_queue():
            raise RuntimeError('PRIVATE_FIXTURE_PAYLOAD')
        fake = SimpleNamespace(event_wakeup=wake, queue_weixin_messages=broken_queue,
            deliver_once=lambda: self.fail('delivery after failed query'),
            event_next_wait=lambda maximum: self.fail('exception must use fallback'))
        with redirect_stderr(diagnostic):
            self.run_pump(fake, stop, fallback=.25)
        self.assertEqual(log[-1], ('wait', .25, False))
        self.assertNotIn('PRIVATE_FIXTURE_PAYLOAD', diagnostic.getvalue())


SECRET = 'whsec_' + base64.b64encode(b'e' * 32).decode()
HOST, URL = 'offline-callback.example.test', 'https://offline-callback.example.test/events'
ACCOUNT, OWNER = 'event-fixture-bot', 'event-fixture-owner'


class EventDeliveryLeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now, self.calls, self.peers = 1800000000., [], []
        self.handler = lambda event: (200, b'{}')
        self.p = self.open_probe()
        self.addCleanup(self.close_peers)
        for target in ('socket.create_connection', 'socket.socket.connect',
                       'subprocess.Popen', 'os.system'):
            guard = patch(target, side_effect=AssertionError('offline test forbids IO'))
            guard.start()
            self.addCleanup(guard.stop)

    def close_peers(self):
        for peer in self.peers:
            peer.close()

    def open_probe(self):
        p = module.Probe(self.tmp.name, post=self.receiver, allowed_hosts=[HOST],
                         clock=lambda: self.now)
        self.peers.append(p)
        return p

    def receiver(self, url, body, headers):
        self.assertEqual(url, URL)
        event = json.loads(body)
        if event.get('type') == 'verification':
            return 200, json.dumps({'challenge': event['challenge']}).encode()
        self.assertEqual(event['eventId'], headers['webhook-id'])
        self.assertIn('v1,', headers['webhook-signature'])
        self.calls.append((event, headers.copy()))
        return self.handler(event)

    @staticmethod
    def params(name='test.ping'):
        return {'name': name, 'arguments': {}, 'ttlMs': None,
                'delivery': {'mode': 'webhook', 'url': URL, 'secret': SECRET}}

    def event(self):
        self.p.subscribe(self.params())
        return self.p.emit()['event_id']

    def row(self, eid):
        return dict(self.p.db.execute('SELECT * FROM pings WHERE id=?', (eid,)).fetchone())

    def bind(self):
        self.p.weixin.bind_confirmed({'status': 'confirmed', 'ilink_bot_id': ACCOUNT,
            'ilink_user_id': OWNER, 'bot_token': 'synthetic-secret-fixture-0000000000000000'})

    def inbound(self, source='one', cursor='fixture-cursor'):
        added = self.p.weixin.ingest(self.p.weixin.account(), {'get_updates_buf': cursor, 'msgs': [{
            'message_id': source, 'from_user_id': OWNER, 'to_user_id': ACCOUNT,
            'message_type': 1, 'context_token': 'synthetic-secret-fixture-0000000000000000',
            'item_list': [{'type': 1, 'text_item': {'text': '合成原文'}}]}]})
        row = self.p.db.execute('SELECT id FROM weixin_inbox WHERE source_id=?', (source,)).fetchone()
        return (row['id'] if row else None), added

    def test_two_connections_during_post_have_only_one_claim_and_post(self):
        eid = self.event()
        second = self.open_probe()
        entered, release = threading.Event(), threading.Event()
        def blocking(event):
            entered.set()
            self.assertTrue(release.wait(2))
            return 200, b'{}'
        self.handler = blocking
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.p.deliver_once)
            try:
                self.assertTrue(entered.wait(1))
                self.assertFalse(pool.submit(second.deliver_once).result(1))
                during = self.row(eid)
                self.assertEqual(during['attempts'], 1)
                self.assertIsInstance(during['lease_token'], str)
                self.assertAlmostEqual(during['lease_until'], self.now + 30)
                self.assertIsNotNone(during['last_attempt_started_at'])
                self.assertEqual(len(self.calls), 1)
            finally:
                release.set()
            self.assertTrue(first.result(1))
        row = self.row(eid)
        self.assertEqual((row['status'], row['last_http_status']), ('delivered', 200))
        self.assertIsNone(row['lease_token'])
        self.assertIsNotNone(row['delivered_at'])

    def test_stale_success_does_not_overwrite_new_lease_failure(self):
        eid = self.event()
        second = self.open_probe()
        entered, release = threading.Event(), threading.Event()
        count = []
        def result(event):
            count.append(True)
            if len(count) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
                return 200, b'{}'
            return 503, b'{}'
        self.handler = result
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.p.deliver_once)
            try:
                self.assertTrue(entered.wait(1))
                original_token = self.row(eid)['lease_token']
                self.now += 31
                self.assertTrue(second.deliver_once())
                newer = self.row(eid)
                self.assertEqual((newer['status'], newer['attempts'], newer['last_http_status']), ('queued', 2, 503))
                self.assertNotEqual(newer['lease_token'], original_token)
            finally:
                release.set()
            first.result(1)
        self.assertEqual(self.row(eid), newer)
        self.assertEqual([event['eventId'] for event, _ in self.calls], [eid, eid])

    def test_stale_410_cannot_revoke_new_grant_or_overwrite_new_completion(self):
        eid = self.event()
        second = self.open_probe()
        entered, release = threading.Event(), threading.Event()
        count = []
        def result(event):
            count.append(True)
            if len(count) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
                return 410, b'{}'
            return 200, b'{}'
        self.handler = result
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.p.deliver_once)
            try:
                self.assertTrue(entered.wait(1))
                grant = second.subscribe(self.params())
                revision = second.db.execute('SELECT revision FROM subscriptions WHERE id=?', (grant['id'],)).fetchone()[0]
                self.now += 31
                self.assertTrue(second.deliver_once())
            finally:
                release.set()
            first.result(1)
        row = self.row(eid)
        self.assertEqual((row['status'], row['last_http_status'], row['attempts']), ('delivered', 200, 2))
        sub = self.p.db.execute('SELECT revision,expires FROM subscriptions WHERE id=?', (grant['id'],)).fetchone()
        self.assertEqual(sub['revision'], revision)
        self.assertIsNone(sub['expires'])

    def test_current_410_of_old_revision_preserves_refresh(self):
        eid = self.event()
        second = self.open_probe()
        def refresh_then_gone(event):
            second.subscribe(self.params())
            return 410, b'{}'
        self.handler = refresh_then_gone
        self.p.deliver_once()
        self.assertEqual(self.row(eid)['status'], 'failed')
        self.assertIsNone(self.p.db.execute('SELECT expires FROM subscriptions').fetchone()[0])

    def test_expired_lease_without_new_claim_may_finish(self):
        eid = self.event()
        def slow_success(event):
            self.now += 31
            return 200, b'{}'
        self.handler = slow_success
        self.assertTrue(self.p.deliver_once())
        self.assertEqual((self.row(eid)['status'], self.row(eid)['attempts']), ('delivered', 1))

    def test_failed_callback_does_not_block_later_due_event(self):
        first = self.event()
        second = self.p.emit()['event_id']
        self.handler = lambda event: (503 if event['eventId'] == first else 200, b'{}')
        self.assertTrue(self.p.deliver_once())
        self.assertTrue(self.p.deliver_once())
        self.assertEqual(self.row(first)['status'], 'queued')
        self.assertEqual(self.row(second)['status'], 'delivered')
        self.assertFalse(self.p.deliver_once())
        self.assertGreater(self.row(first)['next_at'], self.now)

    def test_deadline_wait_respects_backoff_and_active_lease_without_spin(self):
        eid = self.event()
        self.assertEqual(self.p.event_next_wait(.5), .01)
        with self.p.db:
            self.p.db.execute('UPDATE pings SET next_at=?,lease_token=?,lease_until=? WHERE id=?',
                              (self.now + .125, 'offline-lease', self.now + .25, eid))
        self.assertAlmostEqual(self.p.event_next_wait(.5), .25)
        self.now += .26
        self.assertEqual(self.p.event_next_wait(.5), .01)
        with self.p.db:
            self.p.db.execute("UPDATE pings SET status='delivered' WHERE id=?", (eid,))
        self.assertEqual(self.p.event_next_wait(.5), .5)

    def test_transport_exception_is_sanitized_and_retries_same_event_after_backoff(self):
        eid = self.event()
        def timeout(event):
            raise TimeoutError('PRIVATE_CALLBACK_BODY ' + SECRET)
        self.handler = timeout
        self.assertTrue(self.p.deliver_once())
        row = self.row(eid)
        self.assertEqual(row['status'], 'queued')
        self.assertIsNone(row['last_http_status'])
        self.assertEqual(row['last_error_kind'], 'webhook_transport_error')
        self.assertIsNone(row['lease_token'])
        self.assertEqual(row['attempts'], 1)
        self.assertGreater(row['next_at'], self.now)
        self.assertFalse(self.p.deliver_once())
        self.now = row['next_at'] + .001
        self.handler = lambda event: (204, b'')
        self.assertTrue(self.p.deliver_once())
        self.assertEqual((self.row(eid)['status'], self.row(eid)['last_http_status']), ('delivered', 204))
        self.assertEqual([event['eventId'] for event, _ in self.calls], [eid, eid])
        safe = json.dumps(self.p.status())
        self.assertNotIn(SECRET, safe)
        self.assertNotIn('PRIVATE_CALLBACK_BODY', safe)

    def test_crash_recovery_clears_only_leases_preserves_retry_deadline_and_weixin_unknown(self):
        eid = self.event()
        self.bind()
        mid, _ = self.inbound()
        self.p.weixin.queue_reply(mid, '合成待核回执')
        with self.p.db:
            self.p.db.execute("UPDATE weixin_outbox SET status='delivery_unknown',attempts=1 WHERE message_id=?", (mid,))
        def crash(event):
            raise SystemExit('synthetic process interruption')
        self.handler = crash
        with self.assertRaises(SystemExit):
            self.p.deliver_once()
        before = self.row(eid)
        self.assertIsNotNone(before['lease_token'])
        second = self.open_probe()
        self.assertEqual(second.db.execute('SELECT lease_token FROM pings WHERE id=?', (eid,)).fetchone()[0], before['lease_token'])
        second.recover_event_deliveries()
        after = self.row(eid)
        self.assertIsNone(after['lease_token'])
        self.assertEqual((after['id'], after['attempts'], after['next_at']), (eid, before['attempts'], before['next_at']))
        self.assertFalse(second.deliver_once())
        self.assertEqual(second.weixin.send_status(mid)['status'], 'delivery_unknown')
        self.now = after['next_at'] + .001
        self.handler = lambda event: (200, b'{}')
        self.assertTrue(second.deliver_once())
        self.assertEqual([event['eventId'] for event, _ in self.calls], [eid, eid])

    def test_exhausted_sixth_claim_after_restart_never_posts_seventh(self):
        eid = self.event()
        with self.p.db:
            self.p.db.execute('UPDATE pings SET attempts=6,next_at=?,lease_token=?,lease_until=? WHERE id=?',
                              (self.now - 1, 'offline-abandoned-lease', self.now + 30, eid))
        self.p.recover_event_deliveries()
        self.assertTrue(self.p.deliver_once())
        self.assertEqual((self.row(eid)['status'], self.row(eid)['attempts']), ('failed', 6))
        self.assertEqual(self.calls, [])
        self.assertFalse(self.p.deliver_once())

    def test_terminal_failure_and_unsubscribe_are_not_replayed_by_recovery(self):
        eid = self.event()
        self.handler = lambda event: (413, b'{}')
        self.p.deliver_once()
        self.p.recover_event_deliveries()
        self.now += 100
        self.assertFalse(self.p.deliver_once())
        self.assertEqual(self.row(eid)['status'], 'failed')
        self.p.emit()
        self.p.unsubscribe(self.params())
        self.p.recover_event_deliveries()
        self.assertFalse(self.p.deliver_once())
        self.assertEqual(len(self.calls), 1)

    def test_subscribe_and_emit_wake_only_after_durable_commit(self):
        second = self.open_probe()
        observations = []
        def notified():
            self.assertFalse(self.p.db.in_transaction)
            observations.append((second.db.execute('SELECT count(*) FROM subscriptions').fetchone()[0],
                                 second.db.execute('SELECT count(*) FROM pings').fetchone()[0]))
        with patch.object(self.p, 'notify_events', side_effect=notified):
            self.p.subscribe(self.params())
            self.p.emit()
        self.assertEqual(observations, [(1, 0), (1, 1)])

    def test_ingest_wakes_after_inbox_and_cursor_are_committed(self):
        self.bind()
        second = self.open_probe()
        observations = []
        def notified():
            self.assertFalse(self.p.db.in_transaction)
            observations.append((second.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0],
                                 second.db.execute('SELECT cursor FROM weixin_accounts').fetchone()[0]))
        with patch.object(self.p, 'notify_events', side_effect=notified):
            mid, count = self.inbound()
        self.assertEqual(count, 1)
        self.assertEqual(observations, [(1, 'fixture-cursor')])
        self.assertIsNotNone(mid)
        self.assertFalse(self.p.receipts.running)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_outbox').fetchone()[0], 0)

    def test_failed_ingest_rolls_back_cursor_inbox_and_never_wakes(self):
        self.bind()
        with patch.object(self.p, 'notify_events') as notified, patch.object(self.p.media, 'add', side_effect=sqlite3.IntegrityError('fixture rollback')):
            with self.assertRaises(sqlite3.IntegrityError):
                self.inbound(cursor='must-not-persist')
        notified.assert_not_called()
        self.assertEqual(self.p.db.execute('SELECT cursor FROM weixin_accounts').fetchone()[0], '')
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM weixin_inbox').fetchone()[0], 0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM pings').fetchone()[0], 0)

    def test_pending_media_not_published_until_terminal_commit_then_wakes(self):
        self.bind()
        self.p.subscribe(self.params(MESSAGE_EVENT))
        mid, _ = self.inbound()
        aid = 'att_' + 'e' * 32
        with self.p.db:
            self.p.db.execute("INSERT INTO weixin_attachments(id,message_id,account,peer,kind,filename,ref_json,status,created_at,expires_at,provenance) VALUES(?,?,?,?,'image','fixture.png',?,'queued',?,?,'{}')",
                              (aid, mid, ACCOUNT, OWNER, json.dumps({'url': 'https://offline-cdn.example.test/image'}), self.now, self.now + 600))
        self.assertEqual(self.p.queue_weixin_messages(), 0)
        self.assertIsNone(self.p.db.execute('SELECT event_id FROM weixin_inbox WHERE id=?', (mid,)).fetchone()[0])
        observations = []
        def notified():
            self.assertFalse(self.p.db.in_transaction)
            observations.append(self.p.db.execute('SELECT status FROM weixin_attachments WHERE id=?', (aid,)).fetchone()[0])
        with patch.object(self.p, 'notify_events', side_effect=notified), patch.object(self.p.media, 'download', return_value=b'offline-cipher'), patch('probe.media.decrypt', return_value=b'offline-plain'), patch('probe.media.validate_plain', return_value='image/png'):
            self.assertTrue(self.p.media.process_once())
        self.assertIn('cached', observations)
        self.assertEqual(self.p.queue_weixin_messages(), 1)
        self.assertEqual(self.p.queue_weixin_messages(), 0)
        self.assertEqual(self.p.db.execute('SELECT count(*) FROM pings').fetchone()[0], 1)

    def test_existing_ping_migration_preserves_history_and_null_new_timestamps(self):
        with tempfile.TemporaryDirectory() as legacy:
            db = sqlite3.connect(Path(legacy) / 'probe.sqlite')
            db.execute('CREATE TABLE pings(id TEXT PRIMARY KEY,nonce TEXT NOT NULL,sub_id TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_at REAL NOT NULL DEFAULT 0,ack_at REAL)')
            db.execute("INSERT INTO pings VALUES('evt_offline_old','','sub_old','{}','queued',4,123456,NULL)")
            db.commit(); db.close()
            p = module.Probe(legacy, post=self.receiver, allowed_hosts=[HOST], clock=lambda: self.now)
            try:
                row = dict(p.db.execute("SELECT * FROM pings WHERE id='evt_offline_old'").fetchone())
                self.assertEqual((row['attempts'], row['next_at'], row['status']), (4, 123456, 'queued'))
                for field in ('lease_token', 'lease_until', 'last_http_status', 'last_error_kind',
                              'queued_at', 'last_attempt_started_at', 'last_attempt_finished_at', 'delivered_at'):
                    self.assertIsNone(row[field], field)
            finally:
                p.close()


if __name__ == '__main__':
    unittest.main()
