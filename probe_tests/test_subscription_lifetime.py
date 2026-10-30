"""Synthetic lifetime fixtures only; no DNS, connections or real messages."""
import base64
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from probe.probe import (Probe, RpcError, migrate_subscription_lifetimes,
                         record_rpc_diagnostic, subscription_diagnostic)

HOST = "lifetime.example.test"
URL = "https://" + HOST + "/synthetic-callback"
SECRET = "whsec_" + base64.b64encode(b"a" * 32).decode()
ROTATED = "whsec_" + base64.b64encode(b"b" * 32).decode()
OMITTED = object()


class SubscriptionLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for target in ("socket.getaddrinfo", "socket.create_connection", "socket.socket.connect"):
            guard = patch(target, side_effect=AssertionError("real network forbidden"))
            guard.start()
            self.addCleanup(guard.stop)
        self.now = 1700000000.25
        self.calls, self.event_hook, self.verification_hook = [], None, None
        self.probe = self.open_probe()
        self.addCleanup(lambda: self.probe.close())

    def open_probe(self):
        return Probe(self.directory.name, post=self.receiver, allowed_hosts=[HOST], clock=lambda: self.now)

    def receiver(self, url, body, headers):
        value = json.loads(body)
        self.calls.append((url, value, headers.copy()))
        if value.get("type") == "verification":
            if self.verification_hook:
                hook, self.verification_hook = self.verification_hook, None
                hook()
            return 200, json.dumps({"challenge": value["challenge"]}).encode()
        return self.event_hook() if self.event_hook else (200, b"{}")

    def params(self, ttl=OMITTED, name="test.ping", url=URL, secret=SECRET):
        result = {"name": name, "arguments": {}, "delivery": {
            "mode": "webhook", "url": url, "secret": secret}}
        if ttl is not OMITTED:
            result["ttlMs"] = ttl
        return result

    def row(self, sid):
        return dict(self.probe.db.execute("SELECT * FROM subscriptions WHERE id=?", (sid,)).fetchone())

    def test_omitted_null_and_finite_lifetimes_are_distinct(self):
        for ttl, seconds in ((OMITTED, 86400), (None, None), (1500, 1.5),
                             (1, 1), (172800000, 86400)):
            with self.subTest(ttl=ttl):
                result = self.probe.subscribe(self.params(ttl))
                expiry = self.row(result["id"])["expires"]
                if seconds is None:
                    self.assertIsNone(expiry)
                    self.assertIsNone(result["refreshBefore"])
                else:
                    self.assertEqual(expiry, self.now + seconds)
                    self.assertEqual(result["refreshBefore"], self.probe.iso(expiry))

    def test_invalid_finite_lifetime_preserves_existing_grant(self):
        sid = self.probe.subscribe(self.params(None))["id"]
        before = self.row(sid)
        for ttl in (False, True, 0, -1, 1.5, float("inf"), "86400000", {}):
            with self.subTest(ttl=ttl), self.assertRaises(RpcError):
                self.probe.subscribe(self.params(ttl))
            self.assertEqual(self.row(sid), before)

    def test_finite_nonexpiring_finite_same_id_and_legacy_finite_compatibility(self):
        sid = self.probe.subscribe(self.params(60000))["id"]
        revisions = {self.row(sid)["revision"]}
        for ttl in (None, 30000):
            self.now += 1
            self.assertEqual(self.probe.subscribe(self.params(ttl))["id"], sid)
            revisions.add(self.row(sid)["revision"])
        self.assertEqual(len(revisions), 3)
        self.assertEqual(self.probe.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 1)
        self.assertEqual(self.probe.db.execute("SELECT id FROM subscriptions WHERE expires>?", (self.now,)).fetchone()[0], sid)
        # Legacy finite-only writes omit revision; its default remains compatible.
        row = self.row(sid)
        columns = ("id", "url", "secret", "old_secret", "rotate_until", "expires", "verified_at", "name")
        with self.probe.db:
            self.probe.db.execute("INSERT OR REPLACE INTO subscriptions(" + ",".join(columns) +
                ") VALUES(?,?,?,?,?,?,?,?)", tuple(row[k] for k in columns))
        self.assertEqual(self.row(sid)["expires"], row["expires"])

    def test_nonexpiring_restart_and_owner_event_deduplication(self):
        sid = self.probe.subscribe(self.params(None, name="weixin.message"))["id"]
        self.probe.weixin.bind_confirmed({"status": "confirmed", "ilink_bot_id": "fixture-bot",
            "ilink_user_id": "fixture-owner", "bot_token": "fixture-token",
            "baseurl": "https://ilinkai.weixin.qq.com"})
        self.probe.weixin.ingest(self.probe.weixin.account(), {"msgs": [{
            "message_id": "fixture-source", "from_user_id": "fixture-owner", "to_user_id": "fixture-bot",
            "message_type": 1, "message_state": 2, "create_time_ms": self.now * 1000,
            "context_token": "synthetic-context", "item_list": [{"type": 1, "text_item": {"text": "fixture"}}]
        }], "get_updates_buf": "synthetic-cursor"})
        self.assertEqual(self.probe.queue_weixin_messages(), 1)
        event = dict(self.probe.db.execute("SELECT id,sub_id,message_id FROM pings").fetchone())
        self.probe.close()
        self.now += 365 * 86400
        self.probe = self.open_probe()
        self.assertEqual(self.probe.subscribe(self.params(None, name="weixin.message"))["id"], sid)
        self.assertEqual(self.probe.status()["active_subscriptions"], 1)
        self.assertEqual(self.probe.queue_weixin_messages(), 0)
        self.assertEqual(event, dict(self.probe.db.execute("SELECT id,sub_id,message_id FROM pings").fetchone()))
        self.assertTrue(self.probe.deliver_once())
        self.assertFalse(self.probe.deliver_once())

    def test_expired_owner_subscription_preserves_queue_until_verified_same_id_refresh(self):
        params = self.params(1000, name='weixin.message')
        sid = self.probe.subscribe(params)['id']
        self.probe.weixin.bind_confirmed({'status':'confirmed', 'ilink_bot_id':'fixture-bot',
            'ilink_user_id':'fixture-owner', 'bot_token':'fixture-token',
            'baseurl':'https://ilinkai.weixin.qq.com'})
        def ingest(source):
            self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs':[{
                'message_id':source, 'from_user_id':'fixture-owner', 'to_user_id':'fixture-bot',
                'message_type':1, 'message_state':2, 'create_time_ms':self.now * 1000,
                'item_list':[{'type':1,'text_item':{'text':'synthetic'}}]}]})
            self.assertEqual(self.probe.queue_weixin_messages(), 1)
        ingest('original-source')
        original = dict(self.probe.db.execute('SELECT * FROM pings').fetchone())
        self.probe.close()
        self.now += 2  # Natural TTL expiry, still inside the old challenge cache.
        self.probe = self.open_probe()
        self.probe.recover_event_deliveries()
        calls = len(self.calls)
        self.assertFalse(self.probe.deliver_once())
        self.assertEqual(self.probe.event_next_wait(), .5)
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(dict(self.probe.db.execute('SELECT * FROM pings').fetchone()), original)
        # An unrelated event is not blocked behind the paused owner event.
        self.probe.subscribe(self.params(None))
        unrelated = self.probe.emit()
        self.assertTrue(self.probe.deliver_once())
        self.assertEqual(self.calls[-1][1]['eventId'], unrelated['event_id'])
        self.assertFalse(self.probe.deliver_once())
        self.assertEqual(dict(self.probe.db.execute('SELECT * FROM pings WHERE id=?', (original['id'],)).fetchone()), original)
        verifications = sum(body.get('type') == 'verification' for _, body, _ in self.calls)
        self.assertEqual(self.probe.subscribe(params)['id'], sid)
        self.assertEqual(sum(body.get('type') == 'verification' for _, body, _ in self.calls), verifications + 1)
        self.assertTrue(self.probe.deliver_once())
        self.assertFalse(self.probe.deliver_once())
        self.assertEqual(sum(body.get('eventId') == original['id'] for _, body, _ in self.calls), 1)
        ingest('explicitly-unsubscribed-source')
        self.probe.unsubscribe(params)
        calls = len(self.calls)
        self.assertFalse(self.probe.deliver_once())
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.probe.db.execute("SELECT status FROM pings WHERE name='weixin.message' AND id!=?", (original['id'],)).fetchone()[0], 'cancelled')
        self.assertEqual(self.probe.subscribe(params)['id'], sid)
        self.assertFalse(self.probe.deliver_once())  # Cancelled work never revives.
        ingest('revocation-source-one')
        ingest('revocation-source-two')
        self.event_hook = lambda: (410, b'{}')
        self.assertTrue(self.probe.deliver_once())
        self.assertEqual(self.row(sid)['expires'], 0)
        calls = len(self.calls)
        self.assertTrue(self.probe.deliver_once())
        self.assertEqual(len(self.calls), calls)
        self.assertEqual(self.probe.db.execute("SELECT status FROM pings WHERE name='weixin.message' ORDER BY rowid DESC LIMIT 1").fetchone()[0], 'cancelled')

    def test_nonexpiring_synthetic_events_and_signature_rotation(self):
        sid = self.probe.subscribe(self.params(None))["id"]
        self.now += 365 * 86400
        self.assertEqual(self.probe.subscribe(self.params(None, secret=ROTATED))["id"], sid)
        event = self.probe.emit()
        self.probe.deliver_once()
        self.assertEqual(self.calls[-1][1]["eventId"], event["event_id"])
        self.assertEqual(len(self.calls[-1][2]["webhook-signature"].split()), 2)
        self.now += 301
        self.probe.emit()
        self.probe.deliver_once()
        self.assertEqual(len(self.calls[-1][2]["webhook-signature"].split()), 1)

    def test_active_nonexpiring_conflict_checks_preserve_name_filter(self):
        self.probe.subscribe(self.params(None))
        self.probe.subscribe(self.params(None, name="weixin.message"))
        self.assertEqual(self.probe.status()["active_subscriptions"], 2)
        with self.assertRaises(RpcError):
            self.probe.subscribe(self.params(None, url=URL + "-different"))

    def test_current_410_revokes_and_cancels_queued_events(self):
        sid = self.probe.subscribe(self.params(None))["id"]
        self.probe.emit()
        second = self.probe.emit()
        self.event_hook = lambda: (410, b"{}")
        self.probe.deliver_once()
        self.assertEqual(self.row(sid)["expires"], 0)
        before = len(self.calls)
        self.probe.deliver_once()
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.probe.db.execute("SELECT status FROM pings WHERE id=?", (second["event_id"],)).fetchone()[0], "cancelled")
        with self.assertRaises(RpcError): self.probe.emit()

    def test_old_410_cannot_revoke_identical_nonexpiring_renewal(self):
        sid = self.probe.subscribe(self.params(None))["id"]
        self.probe.emit()
        old = self.row(sid)
        def delayed_410():
            self.probe.subscribe(self.params(None))
            return 410, b"{}"
        self.event_hook = delayed_410
        self.probe.deliver_once()
        self.assertIsNone(self.row(sid)["expires"])
        self.assertNotEqual(self.row(sid)["revision"], old["revision"])
        for field in ("secret", "verified_at", "expires"):
            self.assertEqual(self.row(sid)[field], old[field])

    def test_old_410_cannot_revoke_unsubscribe_and_recreate(self):
        sid = self.probe.subscribe(self.params(None))["id"]
        self.probe.emit()
        def delayed_410():
            self.probe.unsubscribe(self.params(None))
            self.probe.subscribe(self.params(None))
            return 410, b"{}"
        self.event_hook = delayed_410
        self.probe.deliver_once()
        self.assertIsNone(self.row(sid)["expires"])

    def test_unsubscribe_stops_nonexpiring_delivery(self):
        self.probe.subscribe(self.params(None))
        self.probe.emit()
        self.probe.unsubscribe(self.params(None))
        before = len(self.calls)
        self.assertFalse(self.probe.deliver_once())
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.probe.status()["active_subscriptions"], 0)

    def test_failed_verification_preserves_grant(self):
        sid = self.probe.subscribe(self.params(60000))["id"]
        before = self.row(sid)
        self.now += 301
        self.probe.post = lambda *args: (200, b'{"challenge":"wrong"}')
        with self.assertRaises(RpcError): self.probe.subscribe(self.params(None))
        self.assertEqual(self.row(sid), before)

    def test_inflight_verification_cannot_undo_unsubscribe(self):
        sid = self.probe.subscribe(self.params(None))["id"]
        self.now += 301
        self.verification_hook = lambda: self.probe.unsubscribe(self.params(None))
        with self.assertRaises(RpcError): self.probe.subscribe(self.params(None))
        self.assertIsNone(self.probe.db.execute("SELECT id FROM subscriptions WHERE id=?", (sid,)).fetchone())

    def test_inflight_verification_cannot_undo_410_or_newer_renewal(self):
        sid = self.probe.subscribe(self.params(None))["id"]
        self.probe.emit()
        self.now += 301
        self.event_hook = lambda: (410, b"{}")
        self.verification_hook = self.probe.deliver_once
        with self.assertRaises(RpcError): self.probe.subscribe(self.params(None))
        self.assertEqual(self.row(sid)["expires"], 0)
        self.now += 301
        self.verification_hook = lambda: self.probe.subscribe(self.params(30000))
        with self.assertRaises(RpcError): self.probe.subscribe(self.params(None))
        self.assertEqual(self.row(sid)["expires"], self.now + 30)

    def test_final_write_rechecks_different_destination_after_callback(self):
        barrier = threading.Barrier(2)
        def receiver(url, body, headers):
            payload = json.loads(body)
            barrier.wait(timeout=5)
            return 200, json.dumps({"challenge": payload["challenge"]}).encode()
        self.probe.post = receiver
        results = []
        def subscribe(url):
            try: results.append(self.probe.subscribe(self.params(None, url=url)))
            except RpcError as exc: results.append(exc)
        workers = [threading.Thread(target=subscribe, args=(url,)) for url in (URL, URL + "-other")]
        for worker in workers: worker.start()
        for worker in workers:
            worker.join(timeout=8)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(sum(isinstance(result, RpcError) for result in results), 1)
        self.assertEqual(self.probe.db.execute("SELECT count(*) FROM subscriptions").fetchone()[0], 1)

    def test_diagnostics_record_fixed_ttl_kind_and_only_explicit_null_result(self):
        for ttl, kind in ((OMITTED, "omitted"), (None, "null"), (5000, "finite"), (True, "invalid")):
            request = {"method": "events/subscribe", "params": self.params(ttl)}
            received = json.loads(subscription_diagnostic(request))
            self.assertEqual(received["ttl_kind"], kind)
            for result, granted in (({}, False), ({"refreshBefore": None}, True),
                                    ({"refreshBefore": "2026-01-01T00:00:00Z"}, False)):
                response = {"result": result}
                completed = json.loads(subscription_diagnostic(request, response))
                self.assertEqual(completed["non_expiring_granted"], granted)
                record_rpc_diagnostic(self.directory.name, request, response, completed=True)
            self.assertNotIn(SECRET, json.dumps(received))
            self.assertNotIn("/synthetic-callback", json.dumps(received))
            self.assertNotIn("ttlMs", received)
        records = self.probe.status()["recent_rpc"]
        self.assertTrue(any(r.get("non_expiring_granted") is True for r in records))
        self.assertTrue(all(r.get("ttl_kind") in ("omitted", "null", "finite", "invalid") for r in records))


class SubscriptionMigrationTests(unittest.TestCase):
    def legacy(self, db):
        db.execute("""CREATE TABLE subscriptions (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, secret TEXT NOT NULL,
            old_secret TEXT, rotate_until REAL NOT NULL, expires REAL NOT NULL,
            verified_at REAL NOT NULL, name TEXT NOT NULL DEFAULT 'test.ping')""")
        db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?,?,?,?)",
            ("sub-fixture", URL, SECRET, ROTATED, 1700000300, 1700086400.25, 1700000000, "weixin.message"))
        for table in ("pings", "weixin_inbox", "weixin_outbox"):
            db.execute("CREATE TABLE " + table + "(id TEXT PRIMARY KEY,sub_id TEXT,value TEXT)")
            db.execute("INSERT INTO " + table + " VALUES('fixture','sub-fixture','synthetic')")
        db.commit()

    def test_migration_retains_all_finite_fields_and_related_rows(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        self.legacy(db)
        before = db.execute("SELECT * FROM subscriptions").fetchone()
        related = {t: db.execute("SELECT * FROM " + t).fetchall() for t in ("pings", "weixin_inbox", "weixin_outbox")}
        migrate_subscription_lifetimes(db)
        self.assertEqual(db.execute("SELECT * FROM subscriptions").fetchone()[:-1], before)
        self.assertEqual(dict((r[1], r[3]) for r in db.execute("PRAGMA table_info(subscriptions)"))["expires"], 0)
        for table, rows in related.items(): self.assertEqual(db.execute("SELECT * FROM " + table).fetchall(), rows)
        migrate_subscription_lifetimes(db)
        self.assertEqual(db.execute("SELECT * FROM subscriptions").fetchone()[:-1], before)

    def test_failed_migration_rolls_back_schema_and_records(self):
        class FailDrop(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql == "DROP TABLE subscriptions":
                    raise sqlite3.OperationalError("synthetic migration failure")
                return super().execute(sql, parameters)
        db = sqlite3.connect(":memory:", factory=FailDrop)
        self.addCleanup(db.close)
        self.legacy(db)
        before = db.execute("SELECT * FROM subscriptions").fetchall()
        with self.assertRaises(sqlite3.OperationalError): migrate_subscription_lifetimes(db)
        info = {r[1]: r for r in db.execute("PRAGMA table_info(subscriptions)")}
        self.assertEqual(info["expires"][3], 1)
        self.assertNotIn("revision", info)
        self.assertEqual(db.execute("SELECT * FROM subscriptions").fetchall(), before)
        self.assertFalse(db.in_transaction)
        self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='subscriptions_lifetime_migration'").fetchone())


if __name__ == "__main__":
    unittest.main()
