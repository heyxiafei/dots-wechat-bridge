#!/usr/bin/env python3
"""Private owner-only Weixin bridge and retained synthetic MCP Events probe.

Stdio access inherits the configured tunnel's access boundary. Use a dedicated,
owner-only tunnel. This bridge contains no model runtime.

Probe assembles the stores and owns subscription/event transactions. Protocol,
schema, RPC adaptation, callback policy, diagnostics and worker execution live
in dedicated modules; this entrypoint retains the original public imports and
the CLI admission gates before any runtime loop is started.
"""
import argparse
# Keep shared standard-library module objects available at the historic import
# path for trusted hosts that inject clocks, networking and process primitives.
import base64
import fcntl
import hashlib
import hmac
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import ssl
import stat
import sys
import threading
import time
from urllib.parse import urlsplit
try:
    from .weixin import WeixinStore, WeixinError, MESSAGE_EVENT
    from .media import MediaStore
    from .outbound import OutboundStore, upload_tools, MAX_RPC_BYTES
    from .dotledger import DotMirrorLedger, ledger_tools, LEDGER_VERSION
    from .dotjournal import DotMirrorJournal, journal_tools, JOURNAL_VERSION
    from .dotcutover import cutover_tools
    from .dotcompleteness import DotCompleteness, completeness_tools
    from .receipts import OwnerReceiptStore
    from .transport_timing import DeliveryTimingStore
    from .event_delivery import EventDeliveryStatus
    from .read_projection import request_id_digest, elapsed_ms, reply_result
except ImportError:
    from weixin import WeixinStore, WeixinError, MESSAGE_EVENT
    from media import MediaStore
    from outbound import OutboundStore, upload_tools, MAX_RPC_BYTES
    from dotledger import DotMirrorLedger, ledger_tools, LEDGER_VERSION
    from dotjournal import DotMirrorJournal, journal_tools, JOURNAL_VERSION
    from dotcutover import cutover_tools
    from dotcompleteness import DotCompleteness, completeness_tools
    from receipts import OwnerReceiptStore
    from transport_timing import DeliveryTimingStore
    from event_delivery import EventDeliveryStatus
    from read_projection import request_id_digest, elapsed_ms, reply_result

try:
    from .aihot_notice_consumer.operator_config import AihotOperatorConfig
    from .aihot_email import AihotEmailEvents, EMAIL_EVENT, email_event_definition, email_tools
except ImportError:
    from aihot_notice_consumer.operator_config import AihotOperatorConfig
    from aihot_email import AihotEmailEvents, EMAIL_EVENT, email_event_definition, email_tools

try:
    from .rpc_protocol import VERSION, BUILD_VERSION, MAX_ATTACHMENT_RPC_BYTES, EVENT_NAME, RPC_METHODS, RpcError, canonical
    from .callback_transport import key_bytes, signature, headers, callback_parts, public_address, PinnedHTTPS, post_https as _post_https
    from .rpc_diagnostics import subscription_diagnostic, record_rpc_diagnostic, read_rpc_diagnostics
    from .rpc_schema import event_definition, tool_definitions
    from .rpc_dispatch import dispatch
    from .service_runtime import acquire_worker_lock, run_weixin_sender, run_event_pump, offline_rpc, offline_main as _offline_main, serve_stdio
except ImportError:
    from rpc_protocol import VERSION, BUILD_VERSION, MAX_ATTACHMENT_RPC_BYTES, EVENT_NAME, RPC_METHODS, RpcError, canonical
    from callback_transport import key_bytes, signature, headers, callback_parts, public_address, PinnedHTTPS, post_https as _post_https
    from rpc_diagnostics import subscription_diagnostic, record_rpc_diagnostic, read_rpc_diagnostics
    from rpc_schema import event_definition, tool_definitions
    from rpc_dispatch import dispatch
    from service_runtime import acquire_worker_lock, run_weixin_sender, run_event_pump, offline_rpc, offline_main as _offline_main, serve_stdio


# Only a retained, verified owner subscription with a natural positive TTL
# waits for same-ID renewal. Missing subscriptions and explicit 410 revocation
# (expires=0) still follow the existing cancellation path.
OWNER_EVENT_AWAITS_RENEWAL = """pings.name='weixin.message' AND EXISTS (
    SELECT 1 FROM subscriptions retained_sub WHERE retained_sub.id=pings.sub_id
    AND retained_sub.name=pings.name AND retained_sub.verified_at>0
    AND retained_sub.expires>0 AND retained_sub.expires<=?)"""


def post_https(url, body, request_headers, allowed_hosts):
    """Retain the public pinned-connection seam while transport owns policy."""
    return _post_https(url, body, request_headers, allowed_hosts,
                       connection_factory=PinnedHTTPS)


def migrate_subscription_lifetimes(db):
    """Change only the lifetime schema, atomically retaining existing grants.

    A NULL expiry is an explicit non-expiring grant. Existing finite values are
    copied unchanged; only events/subscribe may change an actual grant.
    """
    columns = ("id", "url", "secret", "old_secret", "rotate_until", "expires",
               "verified_at", "name", "revision")
    with db:
        db.execute("BEGIN IMMEDIATE")
        info = {row[1]: row for row in db.execute("PRAGMA table_info(subscriptions)")}
        if set(info) - set(columns):
            raise sqlite3.DatabaseError("Unsupported subscriptions schema")
        if "name" not in info:
            db.execute("ALTER TABLE subscriptions ADD COLUMN name TEXT NOT NULL DEFAULT 'test.ping'")
        if "revision" not in info:
            db.execute("ALTER TABLE subscriptions ADD COLUMN revision TEXT NOT NULL DEFAULT ''")
        if not info["expires"][3]:
            return
        # The shipped table has only its primary-key index. Fail closed rather
        # than silently discarding an unexpected custom index or trigger.
        if db.execute("SELECT 1 FROM sqlite_master WHERE tbl_name='subscriptions' "
                      "AND type IN ('index','trigger') AND sql IS NOT NULL").fetchone():
            raise sqlite3.DatabaseError("Unsupported subscriptions schema")
        db.execute("""CREATE TABLE subscriptions_lifetime_migration (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, secret TEXT NOT NULL,
            old_secret TEXT, rotate_until REAL NOT NULL, expires REAL,
            verified_at REAL NOT NULL, name TEXT NOT NULL DEFAULT 'test.ping',
            revision TEXT NOT NULL DEFAULT '')""")
        names = ",".join(columns)
        db.execute("INSERT INTO subscriptions_lifetime_migration(rowid," + names +
                   ") SELECT rowid," + names + " FROM subscriptions")
        db.execute("DROP TABLE subscriptions")
        db.execute("ALTER TABLE subscriptions_lifetime_migration RENAME TO subscriptions")


class Probe:
    def __init__(self, directory, post=None, allowed_hosts=(), clock=time.time, weixin_client=None,
                 mirror_fence_verifier=None, aihot_enabled=False, aihot_data_dir=None,
                 aihot_clock=None, aihot_monotonic=None, email_enabled=False,
                 email_data_dir=None, aihot_operator_config=None):
        directory = Path(directory)
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        self.lock = threading.RLock()
        self.sender_wakeup = threading.Event()
        self.aihot_arm_wakeup = threading.Event()
        self.aihot_operator_result = 'not_requested'
        self.event_wakeup = threading.Event()
        self.clock = clock
        # A fresh production generation is restored from its durable marker;
        # it is never inferred from an empty database or a local worker lock.
        self.fresh_admission = None
        self.bridge_version = BUILD_VERSION
        self.post = post or (lambda url, body, hdrs: post_https(url, body, hdrs, set(allowed_hosts)))
        self.allowed_hosts = set(allowed_hosts)
        self.db = sqlite3.connect(directory / "probe.sqlite", check_same_thread=False)
        os.chmod(directory / "probe.sqlite", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY, url TEXT NOT NULL, secret TEXT NOT NULL,
                old_secret TEXT, rotate_until REAL NOT NULL, expires REAL,
                verified_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS pings (
                id TEXT PRIMARY KEY, nonce TEXT NOT NULL, sub_id TEXT NOT NULL,
                body TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, ack_at REAL);
        """)
        try:
            migrate_subscription_lifetimes(self.db)
        except Exception:
            self.db.close()
            raise
        for table, name, definition in [("pings", "name", "TEXT NOT NULL DEFAULT 'test.ping'"),
                                         ("pings", "message_id", "TEXT")]:
            if name not in {row["name"] for row in self.db.execute("PRAGMA table_info(" + table + ")")}:
                self.db.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)
        for name, sql_type in (
                ('lease_token','TEXT'), ('lease_until','REAL'),
                ('last_http_status','INTEGER'), ('last_error_kind','TEXT'),
                ('queued_at','REAL'), ('last_attempt_started_at','REAL'),
                ('last_attempt_finished_at','REAL'), ('delivered_at','REAL')):
            if name not in {row['name'] for row in self.db.execute('PRAGMA table_info(pings)')}:
                self.db.execute('ALTER TABLE pings ADD COLUMN ' + name + ' ' + sql_type)
        self.db.commit()
        self.weixin = WeixinStore(self, weixin_client)
        self.aihot_notices = None
        self.aihot_operator_config = aihot_operator_config if isinstance(aihot_operator_config, AihotOperatorConfig) else None
        # Explicitly requested but unconfigured consumers fail closed without path discovery.
        self.aihot_guard_required = aihot_enabled is True
        if aihot_enabled is True and self.aihot_operator_config is not None and aihot_data_dir is not None:
            try:
                if __package__:
                    from .aihot_notice_consumer.bridge_hook import BridgeHook
                else:
                    from aihot_notice_consumer.bridge_hook import BridgeHook
                options = {'local_commit_verified': True, 'operator_config': self.aihot_operator_config}
                if aihot_data_dir is not None:
                    options['data_dir'] = Path(aihot_data_dir)
                if aihot_clock is not None:
                    options['now_callable'] = aihot_clock
                if aihot_monotonic is not None:
                    options['monotonic'] = aihot_monotonic
                self.aihot_notices = BridgeHook(self.weixin, **options)
            except Exception:
                # The optional consumer must never interrupt owner replies.
                self.aihot_notices = None
        self.media = MediaStore(self)
        self.outbound = OutboundStore(self)
        self.dot_mirrors = DotMirrorLedger(self)
        self.dot_journal = DotMirrorJournal(self, fence_verifier=mirror_fence_verifier)
        self.receipts = OwnerReceiptStore(self)
        self.delivery_timing = DeliveryTimingStore(self)
        self.event_delivery = EventDeliveryStatus(self)
        self.dot_completeness = DotCompleteness(self)
        self.aihot_email = None
        if email_enabled is True and self.aihot_operator_config is not None and email_data_dir is not None:
            try:
                self.aihot_email = AihotEmailEvents(self, data_dir=email_data_dir, operator_config=self.aihot_operator_config)
            except Exception:
                # Optional email initialization cannot break the existing
                # owner-only Weixin, receipts, subscriptions or B consumer.
                self.aihot_email = None
        if __package__:
            from .fresh_production import FreshAdmission
        else:
            from fresh_production import FreshAdmission
        try:
            self.fresh_admission = FreshAdmission.restore(self)
        except Exception:
            self.db.close()
            raise

    def notify_events(self):
        self.event_wakeup.set()

    def notify_sender(self):
        self.sender_wakeup.set()

    def close(self):
        self.db.close()

    @staticmethod
    def event(name=EVENT_NAME):
        return event_definition(name)

    def subscription_id(self, params):
        name = params.get("name")
        if name not in (EVENT_NAME, MESSAGE_EVENT, EMAIL_EVENT) or params.get("arguments", {}) != {}:
            raise RpcError(-32602, "Only test.ping with empty arguments is supported")
        delivery = params.get("delivery", {})
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook":
            raise RpcError(-32602, "Webhook delivery required")
        url = delivery.get("url")
        callback_parts(url, self.allowed_hosts)
        return "sub_" + hashlib.sha256(canonical(["owner-only-tunnel", url, name, {}]).encode()).hexdigest()[:32]

    def subscribe(self, params, *, force_verification=False):
        fresh = self.fresh_admission
        if fresh is not None:
            fresh.before_subscribe(params)
        sub_id = self.subscription_id(params)
        secret = params["delivery"].get("secret")
        key_bytes(secret)
        ttl = params.get("ttlMs", 86400000)
        if ttl is not None and (type(ttl) is not int or ttl <= 0):
            raise RpcError(-32602, "ttlMs must be positive")
        now = self.clock()
        expires = None if ttl is None else now + min(max(ttl, 1000), 86400000) / 1000
        with self.lock:
            old = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
            other = self.db.execute("SELECT id FROM subscriptions WHERE id!=? AND "
                "(expires IS NULL OR expires>?) AND name=?", (sub_id, now, params["name"])).fetchone()
        if other:
            raise RpcError(-32602, "Stop the existing subscription before choosing another chat")
        sub = {"id": sub_id, "url": params["delivery"]["url"], "secret": secret,
               "old_secret": old["secret"] if old and old["secret"] != secret else (old["old_secret"] if old else None),
               "rotate_until": now + 300 if old and old["secret"] != secret else (old["rotate_until"] if old else 0)}
        cached = (not force_verification and fresh is None and old and old["secret"] == secret and old["verified_at"] > now - 300
                  and (old['expires'] is None or old['expires'] > now))
        if not cached:
            challenge = secrets.token_urlsafe(24)
            body = canonical({"type": "verification", "challenge": challenge}).encode()
            try:
                status, data = self.post(sub["url"], body, headers(sub, "verify_" + secrets.token_hex(16), body, now))
                echoed = json.loads(data).get("challenge")
                if not 200 <= status < 300 or not isinstance(echoed, str) or not hmac.compare_digest(echoed, challenge):
                    raise ValueError()
            except RpcError:
                raise
            except (TimeoutError, socket.timeout):
                raise RpcError(-32015, "Callback verification failed", "timeout") from None
            except Exception:
                raise RpcError(-32015, "Callback verification failed", "challenge_failed") from None
        with self.lock, self.db:
            # Callback I/O releases the lock. Recheck under the write lock so
            # concurrent different destinations cannot both become active.
            self.db.execute("BEGIN IMMEDIATE")
            if fresh is not None:
                if fresh is not self.fresh_admission:
                    raise RpcError(-32602, "Fresh admission changed during verification")
                fresh.before_subscribe(params)
            if self.db.execute("SELECT id FROM subscriptions WHERE id!=? AND "
                    "(expires IS NULL OR expires>?) AND name=?",
                    (sub_id, self.clock(), params["name"])).fetchone():
                raise RpcError(-32602, "Stop the existing subscription before choosing another chat")
            current = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
            if ((old is None) != (current is None) or
                    (old is not None and old["revision"] != current["revision"])):
                raise RpcError(-32602, "Subscription changed during verification; retry")
            if current:
                sub["old_secret"] = current["secret"] if current["secret"] != secret else current["old_secret"]
                sub["rotate_until"] = now + 300 if current["secret"] != secret else current["rotate_until"]
            self.db.execute("INSERT OR REPLACE INTO subscriptions(id,url,secret,old_secret,rotate_until,expires,verified_at,name,revision) VALUES (?,?,?,?,?,?,?,?,?)",
                            (sub_id, sub["url"], secret, sub["old_secret"], sub["rotate_until"], expires,
                             old["verified_at"] if cached else now, params["name"], secrets.token_hex(16)))
        result = {"id": sub_id, "refreshBefore": None if expires is None else self.iso(expires), "cursor": None, "truncated": False}
        if fresh is not None:
            with self.lock:
                fresh.after_subscribe(result)
        self.notify_events()
        return result

    @staticmethod
    def iso(timestamp):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))

    def unsubscribe(self, params):
        if self.fresh_admission is not None:
            raise RpcError(-32602, "Fresh production preserves its original subscription")
        sub_id = self.subscription_id(params)
        with self.lock, self.db:
            self.db.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
            self.db.execute("UPDATE pings SET status='cancelled' WHERE sub_id=? AND status='queued'", (sub_id,))
        return {}

    def emit(self):
        if self.fresh_admission is not None:
            raise RpcError(-32602, "Fresh production admits only its approved owner message events")
        now = self.clock()
        with self.lock, self.db:
            sub = self.db.execute("SELECT id FROM subscriptions WHERE (expires IS NULL OR expires>?) AND name=?", (now, EVENT_NAME)).fetchone()
            if not sub:
                raise RpcError(-32003, "No verified active subscription; subscribe in the target dot first")
            if self.db.execute("SELECT count(*) FROM pings WHERE status='queued'").fetchone()[0] >= 3:
                raise RpcError(-32003, "Probe queue is full")
            event_id, nonce = "evt_" + secrets.token_hex(16), secrets.token_urlsafe(24)
            event = {"eventId": event_id, "name": EVENT_NAME, "timestamp": self.iso(now),
                     "data": {"nonce": nonce}, "cursor": None}
            self.db.execute("INSERT INTO pings(id,nonce,sub_id,body,status,queued_at) VALUES (?,?,?,?,'queued',?)",
                            (event_id, nonce, sub["id"], canonical(event), now))
        self.notify_events()
        return {"event_id": event_id, "status": "queued"}

    def acknowledge(self, args):
        if set(args) != {"event_id", "nonce"} or not all(isinstance(x, str) for x in args.values()):
            raise RpcError(-32602, "event_id and nonce are required")
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM pings WHERE id=?", (args["event_id"],)).fetchone()
            if not row or row["name"] != EVENT_NAME or not hmac.compare_digest(row["nonce"], args["nonce"]):
                raise RpcError(-32602, "Unknown event or incorrect nonce")
            if row["status"] in ("cancelled", "failed") or row["attempts"] == 0:
                raise RpcError(-32602, "Event has not been delivered for acknowledgement")
            already = row["ack_at"] is not None
            if not already:
                self.db.execute("UPDATE pings SET status='acknowledged',ack_at=? WHERE id=?", (self.clock(), row["id"]))
        return {"event_id": args["event_id"], "acknowledged": True, "already_acknowledged": already}

    def status(self):
        with self.lock:
            rows = self.db.execute("SELECT id,status,attempts,ack_at,queued_at,last_attempt_started_at,last_attempt_finished_at,delivered_at,lease_until,last_http_status,last_error_kind FROM pings ORDER BY rowid DESC LIMIT 10").fetchall()
            count = self.db.execute("SELECT count(*) FROM subscriptions WHERE (expires IS NULL OR expires>?)", (self.clock(),)).fetchone()[0]
        return {"active_subscriptions": count, "events": [dict(x) for x in rows], "weixin_connected": self.weixin.status()["connected"],
                "bridge_version": BUILD_VERSION, "dot_mirror_ledger_version": LEDGER_VERSION,
                "aihot_notifications": (self.aihot_notices.status_snapshot() if self.aihot_notices else
                    {"version": "aihot-wechat-b1", "installed": False, "enabled": False,
                     "email_enabled": False, "requires_rearm": True, "last_tick_at": None,
                     "last_result": "hook_unavailable"}),
                "aihot_operator_result": self.aihot_operator_result,
                "aihot_email": (self.aihot_email.status_snapshot() if self.aihot_email else
                    {"installed": False, "enabled": False, "result": "email_unavailable"}),
                "recent_rpc": read_rpc_diagnostics(self.directory)}

    def consume_aihot_arm_request(self):
        """One explicit local operator request in this worker, never on boot.

        The request is unlinked and its directory synced before any arm attempt.
        A crash can lose the request, but cannot replay it automatically.
        The original owner metadata and source gates remain in arm_aihot.
        """
        path = self.directory / 'aihot-arm-once.json'
        try:
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                    or before.st_size > 512):
                return {"result": "operator_request_invalid"}
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(fd)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                        before.st_dev, before.st_ino, before.st_size):
                    return {"result": "operator_request_invalid"}
                request = json.loads(os.read(fd, 513))
            finally:
                os.close(fd)
            now = time.time()
            if (not isinstance(request, dict) or set(request) != {
                    'version', 'pid', 'approval_message_id', 'request_id', 'created_at'}
                    or request['version'] != 1 or type(request['pid']) is not int
                    or request['pid'] != os.getpid()
                    or not isinstance(request['request_id'], str)
                    or not re.fullmatch(r'[0-9a-f]{32}', request['request_id'])
                    or type(request['created_at']) not in (int, float)
                    or not now - 120 <= request['created_at'] <= now + 5):
                return {"result": "operator_request_invalid"}
            latest = path.lstat()
            if (latest.st_dev, latest.st_ino) != (opened.st_dev, opened.st_ino):
                return {"result": "operator_request_invalid"}
            approval_id = request['approval_message_id']
            os.unlink(path)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return self.arm_aihot_notifications(approval_id)
        except FileNotFoundError:
            return {"result": "operator_request_missing"}
        except Exception:
            return {"result": "operator_request_invalid"}

    def arm_aihot_notifications(self, approval_message_id):
        """Use only a separately verified owner approval message; never read its text.

        The metadata proof and pin sample share the original owner RLock. The
        controller then checks source, owner and generation again under its
        own monitor/dual locks and the original owner fence.
        """
        hook = self.aihot_notices
        if hook is None:
            return {"result": "hook_unavailable"}
        if (self.aihot_operator_config is None or not isinstance(approval_message_id, str) or not re.fullmatch(r"wx_[0-9a-f]{32}", approval_message_id)
                or not hmac.compare_digest(hashlib.sha256(approval_message_id.encode('ascii')).hexdigest(),
                                           self.aihot_operator_config.approval_message_sha256)):
            return {"result": "approval_message_invalid"}
        try:
            with self.lock:
                accounts = self.db.execute("SELECT id,owner,status FROM weixin_accounts LIMIT 2").fetchall()
                message = self.db.execute("SELECT account,peer FROM weixin_inbox WHERE id=?", (approval_message_id,)).fetchone()
                if (len(accounts) != 1 or accounts[0]['status'] != 'ready' or message is None
                        or message['account'] != accounts[0]['id'] or message['peer'] != accounts[0]['owner']):
                    return {"result": "approval_owner_mismatch"}
                sample = hook.host.sample_binding()
                if sample.get('bound_ready') is not True or not isinstance(sample.get('owner_pin'), str):
                    return {"result": "owner_not_ready"}
                pin = sample['owner_pin']
            return hook.arm(expected_owner_pin=pin)
        except Exception:
            return {"result": "activation_failed_check_control"}

    def off_aihot_notifications(self):
        hook = self.aihot_notices
        return hook.off() if hook is not None else {"result": "hook_unavailable"}

    def queue_weixin_messages(self):
        account = self.weixin.account()
        if not account:
            return 0
        with self.lock, self.db:
            fresh = self.fresh_admission
            if fresh is None:
                sub = self.db.execute("SELECT id FROM subscriptions WHERE name=? AND (expires IS NULL OR expires>?)", (MESSAGE_EVENT, self.clock())).fetchone()
                queued = self.db.execute("SELECT count(*) FROM pings WHERE status='queued'").fetchone()[0]
            else:
                try:
                    fresh.require_ready()
                except WeixinError:
                    return 0
                sub = self.db.execute("SELECT id FROM subscriptions WHERE id=? AND name=? AND (expires IS NULL OR expires>?)",
                                      (fresh.subscription_id, MESSAGE_EVENT, self.clock())).fetchone()
                queued = sum(self._fresh_event_allowed(row) for row in self.db.execute(
                    "SELECT * FROM pings WHERE status='queued'"))
            if not sub or queued >= 64:
                return 0
            query = """SELECT id,created_at FROM weixin_inbox i WHERE account=? AND peer=? AND event_id IS NULL
                AND NOT EXISTS (SELECT 1 FROM weixin_attachments a WHERE a.message_id=i.id AND a.status IN ('queued','downloading'))
                ORDER BY created_at"""
            capacity = min(32, 64 - queued)
            if fresh is None:
                rows = self.db.execute(query + " LIMIT ?", (account["id"], account["owner"], capacity)).fetchall()
            else:
                rows = []
                for row in self.db.execute(query, (account["id"], account["owner"])):
                    if fresh.allows_source(row['id']):
                        rows.append(row)
                        if len(rows) == capacity:
                            break
            for row in rows:
                event_id = "evt_" + hashlib.sha256(canonical([MESSAGE_EVENT, sub["id"], row["id"]]).encode()).hexdigest()[:32]
                body = canonical({"eventId": event_id, "name": MESSAGE_EVENT, "timestamp": self.iso(row["created_at"]),
                                  "data": {"message_id": row["id"]}, "cursor": None})
                self.db.execute("INSERT OR IGNORE INTO pings(id,nonce,sub_id,body,status,name,message_id,queued_at) VALUES(?,'',?,?,'queued',?,?,?)", (event_id, sub["id"], body, MESSAGE_EVENT, row["id"], self.clock()))
                self.db.execute("UPDATE weixin_inbox SET event_id=? WHERE id=?", (event_id, row["id"]))
                self.delivery_timing.queued(row["id"], self.clock())
        return len(rows)

    def _fresh_event_allowed(self, row):
        """A local admission check, never proof of remote writer exclusion."""
        fresh = self.fresh_admission
        if (fresh is None or row['name'] != MESSAGE_EVENT
                or row['sub_id'] != fresh.subscription_id
                or not row['message_id'] or not fresh.allows_source(row['message_id'])):
            return False
        expected = "evt_" + hashlib.sha256(canonical(
            [MESSAGE_EVENT, fresh.subscription_id, row['message_id']]).encode()).hexdigest()[:32]
        source = self.db.execute('SELECT event_id,created_at FROM weixin_inbox WHERE id=?',
                                 (row['message_id'],)).fetchone()
        if row['id'] != expected or source is None or source['event_id'] != row['id']:
            return False
        expected_body = canonical({'eventId': expected, 'name': MESSAGE_EVENT,
                                   'timestamp': self.iso(source['created_at']),
                                   'data': {'message_id': row['message_id']}, 'cursor': None})
        return row['body'] == expected_body

    def recover_event_deliveries(self):
        # Sole daemon only, after its worker lock. The receiver still deduplicates
        # stable event IDs: a previous HTTP result may have been lost. Never
        # replay terminal events or change Weixin uncertain-send records.
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.fresh_admission is not None:
                # A prior POST may have succeeded before its local result was
                # committed. Keep the original event ID and never replay it.
                self.db.execute("""UPDATE pings SET status='delivery_unknown',
                    lease_token=NULL,lease_until=NULL,last_error_kind='fresh_restart_during_delivery'
                    WHERE status='queued' AND attempts>0 AND name=? AND sub_id=?""",
                    (MESSAGE_EVENT, self.fresh_admission.subscription_id))
                return
            self.db.execute("UPDATE pings SET lease_token=NULL,lease_until=NULL WHERE status='queued' AND lease_token IS NOT NULL")

    def event_next_wait(self, maximum=.5):
        with self.lock:
            if self.fresh_admission is not None:
                eligible = [max(row['next_at'], row['lease_until'] or 0) for row in self.db.execute(
                    "SELECT * FROM pings WHERE status='queued' AND attempts=0") if self._fresh_event_allowed(row)]
                return min(maximum, max(.01, min(eligible) - self.clock())) if eligible else maximum
            row = self.db.execute("SELECT min(max(next_at,coalesce(lease_until,0))) FROM pings WHERE status='queued' AND NOT ("+
                                  OWNER_EVENT_AWAITS_RENEWAL+")", (self.clock(),)).fetchone()
        return maximum if row[0] is None else min(maximum, max(.01, row[0] - self.clock()))

    def _defer_email_delivery(self, event_id, token):
        """Fail closed if the optional guard/finalizer is unavailable.

        This restores only the still-owned AIHOT lease; it never touches a
        Weixin event or records an attempt that did not issue an HTTP request.
        """
        with self.lock, self.db:
            self.db.execute("""UPDATE pings SET lease_token=NULL,lease_until=NULL,
                attempts=max(attempts-1,0),next_at=?,last_error_kind='aihot_email_deferred'
                WHERE id=? AND name=? AND status='queued' AND lease_token=?""",
                (self.clock() + 10, event_id, EMAIL_EVENT, token))

    def deliver_once(self):
        now = self.clock()
        fresh = self.fresh_admission
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            # Preserve an expired owner event exactly as queued, without
            # letting it block unrelated events or spin the pump. Only an
            # authorized protocol refresh of this same ID makes it eligible.
            query = "SELECT * FROM pings WHERE status='queued' AND next_at<=? AND (lease_until IS NULL OR lease_until<=?) AND NOT (" + OWNER_EVENT_AWAITS_RENEWAL + ") ORDER BY rowid"
            if fresh is None:
                row = self.db.execute(query + " LIMIT 1", (now, now, now)).fetchone()
            else:
                row = next((candidate for candidate in self.db.execute(query, (now, now, now))
                            if candidate['attempts'] == 0 and self._fresh_event_allowed(candidate)), None)
            if not row:
                return False
            sub_row = self.db.execute("SELECT * FROM subscriptions WHERE id=? AND (expires IS NULL OR expires>?)", (row["sub_id"], now)).fetchone()
            if not sub_row:
                self.db.execute("UPDATE pings SET status='cancelled',lease_token=NULL,lease_until=NULL,last_error_kind='subscription_inactive' WHERE id=?", (row['id'],))
                return True
            if row['attempts'] >= 6:
                self.db.execute("UPDATE pings SET status='failed',lease_token=NULL,lease_until=NULL,last_error_kind='retry_limit' WHERE id=?", (row['id'],))
                return True
            # A transport lease prevents concurrent workers claiming the same
            # ready event. It is not a model/run lease or exactly-once proof.
            token = secrets.token_hex(16)
            attempt = row['attempts'] + 1
            self.db.execute("UPDATE pings SET attempts=?,next_at=?,lease_token=?,lease_until=?,last_attempt_started_at=? WHERE id=? AND status='queued'",
                            (attempt, now + min(2 ** attempt, 60), token, now + 30, now, row['id']))
            if row['message_id']:
                self.delivery_timing.attempt(row['message_id'], now)
        body, sub = row['body'].encode(), dict(sub_row)
        if row['name'] == EMAIL_EVENT:
            email = self.aihot_email
            try:
                decision = email.delivery_guard(row['id']) if email is not None else 'defer'
            except Exception:
                decision = 'defer'
            if decision not in ('allow', 'defer', 'cancel'):
                decision = 'defer'
            if decision != 'allow':
                try:
                    if email is None:
                        raise RuntimeError('email_guard_unavailable')
                    email.finish_blocked_delivery(row['id'], token, decision)
                except Exception:
                    self._defer_email_delivery(row['id'], token)
                return True
        try:
            if fresh is None:
                code, _ = self.post(sub['url'], body, headers(sub, row['id'], body, now))
            else:
                # Keep the local identity stable until the POST starts. This
                # local lock does not claim to fence another host or writer.
                with self.lock:
                    current = self.db.execute('SELECT * FROM pings WHERE id=?', (row['id'],)).fetchone()
                    current_sub = self.db.execute('SELECT revision FROM subscriptions WHERE id=?', (sub['id'],)).fetchone()
                    if (fresh is not self.fresh_admission or current is None
                            or current['status'] != 'queued' or current['lease_token'] != token
                            or current_sub is None or current_sub['revision'] != sub['revision']
                            or not self._fresh_event_allowed(current)):
                        with self.db:
                            self.db.execute("""UPDATE pings SET status='cancelled',lease_token=NULL,
                                lease_until=NULL,last_error_kind='fresh_admission_changed_before_post'
                                WHERE id=? AND status='queued' AND lease_token=?""", (row['id'], token))
                        return True
                    code, _ = self.post(sub['url'], body, headers(sub, row['id'], body, now))
            if type(code) is not int or not 100 <= code <= 599:
                code = 0
        except Exception:
            code = 0
        finished = self.clock()
        ok = 200 <= code < 300
        state, reason = ('delivered', None) if ok else ('queued', 'webhook_transport_error' if not code else 'webhook_retryable_http')
        if not ok and (code in (410,413) or (300 <= code < 500 and code not in (408,429)) or attempt >= 6):
            state, reason = 'failed', 'retry_limit' if attempt >= 6 else 'webhook_permanent_http'
        if fresh is not None and not ok:
            # The new generation deliberately has no automatic event replay.
            # An HTTP error does not establish whether the remote run began.
            state, reason = ('failed', 'fresh_webhook_rejected') if 300 <= code < 500 and code not in (408,429) else ('delivery_unknown', 'fresh_webhook_result_unknown')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            changed = self.db.execute("""UPDATE pings SET status=?,lease_token=NULL,lease_until=NULL,
                last_http_status=?,last_error_kind=?,last_attempt_finished_at=?,
                delivered_at=CASE WHEN ? THEN coalesce(delivered_at,?) ELSE delivered_at END
                WHERE id=? AND status='queued' AND lease_token=?""",
                (state, code or None, reason, finished, ok, finished, row['id'], token)).rowcount
            if changed and ok and row['message_id']:
                self.delivery_timing.accepted(row['message_id'], finished)
            if changed and code == 410 and fresh is None:
                self.db.execute("UPDATE subscriptions SET expires=0,revision=? WHERE id=? AND revision=?",
                                (secrets.token_hex(16), sub['id'], sub['revision']))
        return True

    @staticmethod
    def tools():
        return tool_definitions()

    def rpc(self, request):
        return dispatch(self, request, attachment_response_limit=MAX_ATTACHMENT_RPC_BYTES)


def offline_main(directory):
    """Keep the public entrypoint's factory and worker-lock injection boundary."""
    return _offline_main(directory, probe_factory=Probe, lock_factory=acquire_worker_lock,
                         rpc_handler=offline_rpc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", default=str(Path(__file__).resolve().parent / "state"))
    parser.add_argument("--allow-callback-host", action="append", default=[])
    parser.add_argument("--emit", action="store_true", help="Queue one synthetic event for the existing subscription")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--offline", action="store_true", help="Isolated installation check: no network, subscriptions, receivers or senders")
    parser.add_argument("--fresh-production", action="store_true",
                        help="Use the explicitly approved durable future-message cutover; never migrate old queues")
    parser.add_argument("--connection-only", action="store_true",
                        help="Bounded original-route handshake only; no business workers or message operations")
    parser.add_argument("--connection-route-digest")
    parser.add_argument("--connection-subscription-id")
    parser.add_argument("--connection-duration-seconds", type=int, default=None,
                        help="Connection-only lifetime in seconds (1..180; default 180)")
    args = parser.parse_args()
    if args.connection_only:
        if args.fresh_production or args.offline or args.status or args.emit:
            parser.error('--connection-only cannot be combined with --fresh-production, --offline, --status or --emit')
        if (not isinstance(args.connection_route_digest, str)
                or not re.fullmatch(r'[0-9a-f]{64}', args.connection_route_digest)
                or args.connection_subscription_id != 'sub_' + args.connection_route_digest[:32]):
            parser.error('--connection-only requires the matching original route digest and subscription ID')
        if len(args.allow_callback_host) != 1:
            parser.error('--connection-only requires exactly one --allow-callback-host')
        if args.connection_duration_seconds is not None and not 1 <= args.connection_duration_seconds <= 180:
            parser.error('--connection-duration-seconds must be between 1 and 180')
    elif (args.connection_route_digest is not None or args.connection_subscription_id is not None
          or args.connection_duration_seconds is not None):
        parser.error('connection route, subscription and duration options require --connection-only')
    if args.fresh_production and (args.emit or args.status or args.offline):
        parser.error('--fresh-production cannot be combined with --emit, --status or --offline')
    os.umask(0o077)
    if args.connection_only:
        # This branch must precede Probe construction, any store migration,
        # worker-lock acquisition below, and every business recovery/thread.
        try:
            if __package__:
                from .connection_only import serve
            else:
                from connection_only import serve
            return serve(args.state_dir, args.allow_callback_host,
                         args.connection_route_digest, args.connection_subscription_id,
                         duration_seconds=args.connection_duration_seconds or 180)
        except (OSError, ValueError, WeixinError, RpcError, sqlite3.Error):
            print('connection-only validation or handshake unavailable; no business workers started', file=sys.stderr)
            return 1
    if args.offline:
        if args.emit or args.status:
            parser.error('--offline cannot be combined with --emit or --status')
        try:
            return offline_main(args.state_dir)
        except (OSError, ValueError):
            print('offline check refused; use a separate test-state directory', file=sys.stderr)
            return 1
    worker_lock = None
    if not (args.emit or args.status):
        try:
            worker_lock = acquire_worker_lock(args.state_dir)
        except BlockingIOError:
            print("bridge already running for this private state directory", file=sys.stderr)
            return 1
    # The public CLI has no trusted operator configuration injection. Owning a
    # worker lock is not permission to enable optional private consumers.
    # Retained policy markers still fail closed in the original sender guard.
    try:
        probe = Probe(args.state_dir, allowed_hosts=args.allow_callback_host)
    except (WeixinError, OSError, ValueError):
        print('production state validation failed; no workers started', file=sys.stderr)
        if worker_lock is not None:
            worker_lock.close()
        return 1
    # A bounded acceptance state is never a production queue. Its original
    # read-only receipt remains available through bounded-dot-stdio.py.
    if (not args.status and probe.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='weixin_bounded_dot_check'").fetchone()):
        print('bounded DOT diagnostic state cannot start production; read original receipt', file=sys.stderr)
        probe.close()
        if worker_lock is not None:
            worker_lock.close()
        return 1
    if not args.status and probe.fresh_admission is not None and not args.fresh_production:
        print('fresh production state requires its approved startup mode; rollback must stay stopped', file=sys.stderr)
        probe.close()
        if worker_lock is not None:
            worker_lock.close()
        return 1
    if args.fresh_production:
        try:
            if __package__:
                from .fresh_production import FreshAdmission, read_approval_file
            else:
                from fresh_production import FreshAdmission, read_approval_file
            approval = read_approval_file(Path(args.state_dir).parent / 'setup-approval.json')
            probe.fresh_admission = FreshAdmission.activate(probe, approval)
        except (WeixinError, OSError, ValueError):
            print('fresh production approval or durable cutover validation failed; no workers started', file=sys.stderr)
            probe.close()
            if worker_lock is not None:
                worker_lock.close()
            return 1
    if args.emit or args.status:
        try:
            print(canonical(probe.emit() if args.emit else probe.status()))
        except RpcError as error:
            print(canonical({"error": str(error)}))
            return 1
        finally:
            probe.close()
        return 0
    return serve_stdio(probe, event_pump=run_event_pump, sender_loop=run_weixin_sender,
                       record_diagnostic=record_rpc_diagnostic,
                       project_diagnostic=subscription_diagnostic)


if __name__ == "__main__":
    sys.exit(main())
