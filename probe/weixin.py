"""Owner-only WeixinStore facade: durable commitments and read projections.

InboundCapture owns atomic input capture, OutboundDispatcher owns the existing
single sender's state machine, and IlinkClient owns the bounded HTTP protocol.
The facade preserves the historical API, database schema and stable identities.
"""
import hashlib
import json
import secrets
try:
    from .weixin_protocol import (BASE_URL, CHANNEL_VERSION, MESSAGE_EVENT,
                                 WeixinError, IlinkClient, encoded, api_base,
                                 public_ip, parse_api_json)
    from .weixin_inbound import (InboundCapture, describe_inbound, inbound_part_id,
                                inbound_type_metadata, media_ingest_items,
                                MAX_INBOUND_TEXT_BYTES, MAX_INBOUND_ATTACHMENTS,
                                MAX_INBOUND_MANIFEST_ITEMS, INBOUND_COMPLETENESS_VERSION)
    from .weixin_dispatch import OutboundDispatcher
    from .quotes import server_message_id
    from .fixed_commands import classify as classify_fixed_command
    from .command_catalog import is_help_request
    from .command_presentation import render_help_menu
except ImportError:
    from weixin_protocol import (BASE_URL, CHANNEL_VERSION, MESSAGE_EVENT,
                                WeixinError, IlinkClient, encoded, api_base,
                                public_ip, parse_api_json)
    from weixin_inbound import (InboundCapture, describe_inbound, inbound_part_id,
                               inbound_type_metadata, media_ingest_items,
                               MAX_INBOUND_TEXT_BYTES, MAX_INBOUND_ATTACHMENTS,
                               MAX_INBOUND_MANIFEST_ITEMS, INBOUND_COMPLETENESS_VERSION)
    from weixin_dispatch import OutboundDispatcher
    from quotes import server_message_id
    from fixed_commands import classify as classify_fixed_command
    from command_catalog import is_help_request
    from command_presentation import render_help_menu


class WeixinStore:
    def __init__(self, probe, client=None):
        self.probe, self.db, self.lock = probe, probe.db, probe.lock
        self.clock, self.client = probe.clock, client or IlinkClient()
        self.poll_after = 0
        # Runtime-only send guard; set by the existing AIHot hook after restart.
        self.aihot_held_notification_ids = frozenset()
        self.aihot_hold_all_notifications = False
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS weixin_accounts (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, token TEXT NOT NULL, base_url TEXT NOT NULL,
                cursor TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ready',
                last_poll REAL, last_error TEXT);
            CREATE TABLE IF NOT EXISTS weixin_peers (
                account TEXT NOT NULL, peer TEXT NOT NULL, token TEXT NOT NULL,
                source_ms REAL NOT NULL, received_at REAL NOT NULL, PRIMARY KEY(account,peer));
            CREATE TABLE IF NOT EXISTS weixin_inbox (
                id TEXT PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL, source_id TEXT NOT NULL,
                text TEXT NOT NULL, created_at REAL NOT NULL, kind TEXT NOT NULL,
                unsupported TEXT NOT NULL, event_id TEXT);
            CREATE TABLE IF NOT EXISTS weixin_inbound_manifests (
                message_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS weixin_binding_epoch (
                id INTEGER PRIMARY KEY CHECK(id=1), epoch TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aihot_auto_resume (
                id INTEGER PRIMARY KEY CHECK(id=1), policy TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS weixin_outbox (
                message_id TEXT PRIMARY KEY, account TEXT NOT NULL, peer TEXT NOT NULL,
                text TEXT NOT NULL, client_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
                context_at REAL, error_kind TEXT, server_code INTEGER, accepted_at REAL,
                created_at REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'reply');
        """)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(weixin_outbox)")}
        for name, sql_type in (("api_result", "TEXT"), ("recipient_confirmed_at", "REAL"),
                               ("upload_id", "TEXT"), ("source_message_id", "TEXT"), ("item_json", "TEXT"),
                               ("ack_origin", "TEXT"), ("post_started_at", "REAL")):
            if name not in columns:
                self.db.execute("ALTER TABLE weixin_outbox ADD COLUMN " + name + " " + sql_type)
        self.db.execute("CREATE INDEX IF NOT EXISTS weixin_reply_order ON weixin_outbox(source_message_id,kind,status)")
        # Additive side tables preserve old inbox/outbox rows and code rollback.
        # Historical captures/IDs are deliberately not inferred or backfilled.
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS weixin_quote_captures (
                message_id TEXT PRIMARY KEY, metadata_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS weixin_server_message_ids (
                account TEXT NOT NULL, peer TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
                local_id TEXT NOT NULL, server_id TEXT NOT NULL,
                PRIMARY KEY(account,peer,direction,local_id));
            CREATE INDEX IF NOT EXISTS weixin_server_id_lookup
                ON weixin_server_message_ids(account,peer,server_id);
            CREATE TABLE IF NOT EXISTS weixin_quote_shape_once (
                id INTEGER PRIMARY KEY CHECK(id=1),
                armed_at REAL NOT NULL, binding_hash TEXT NOT NULL,
                message_id TEXT, shape_json TEXT, captured_at REAL,
                CHECK((message_id IS NULL AND shape_json IS NULL AND captured_at IS NULL)
                      OR (message_id IS NOT NULL AND shape_json IS NOT NULL AND captured_at IS NOT NULL
                          AND length(CAST(shape_json AS BLOB))<=4096)));
        ''')
        # A private, fixed diagnostic for one explicitly requested future sample.
        # The durable row is never rearmed on restart or binding changes.
        bound = self.db.execute("SELECT id,owner FROM weixin_accounts WHERE status='ready' LIMIT 2").fetchall()
        if len(bound) == 1:
            self.db.execute('''INSERT OR IGNORE INTO weixin_quote_shape_once
                (id,armed_at,binding_hash) VALUES(1,?,?)''',
                (self.clock(), hashlib.sha256(encoded([bound[0]['id'], bound[0]['owner']]).encode()).hexdigest()))
        self.db.commit()

        self._inbound = InboundCapture(probe, clock=lambda: self.clock(),
                                       notify_sender=lambda: self.notify_sender())
        self._dispatcher = OutboundDispatcher(self)

    def notify_sender(self):
        notify = getattr(self.probe, "notify_sender", None)
        if notify:
            notify()

    def recover_inflight(self):
        # Only the single daemon calls this, never a read-only status process.
        with self.db:
            self.db.execute("UPDATE weixin_outbox SET status='delivery_unknown',error_kind='restart_during_send' WHERE status='sending'")

    def bind_confirmed(self, result):
        # A fresh-production generation is pinned to the successful binding
        # epoch. A login must never silently rotate it or revive held sends.
        if getattr(self.probe, 'fresh_admission', None) is not None:
            raise WeixinError('fresh_rebinding_requires_reactivation')
        values = [result.get(k) for k in ("ilink_bot_id", "ilink_user_id", "bot_token")]
        if result.get("status") != "confirmed" or not all(isinstance(x, str) and 0 < len(x) <= 8192 for x in values):
            raise WeixinError("incomplete_owner_binding")
        account, owner, token = values
        base = api_base(result.get("baseurl") or BASE_URL)
        with self.lock:
            with self.db:
                previous = self.db.execute("SELECT id,owner FROM weixin_accounts LIMIT 1").fetchone()
                if previous and (previous["id"] != account or previous["owner"] != owner):
                    raise WeixinError("binding_identity_changed")
                self.db.execute("INSERT INTO weixin_accounts(id,owner,token,base_url) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET token=excluded.token,base_url=excluded.base_url,status='ready',last_error=NULL,last_poll=NULL",
                                (account, owner, token, base))
                # The durable epoch changes on every supported successful bind,
                # including an offline QR login to the same identity.
                self.db.execute("INSERT INTO weixin_binding_epoch(id,epoch) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET epoch=excluded.epoch", (secrets.token_hex(16),))
                self.db.execute("UPDATE weixin_outbox SET status='queued',next_at=0 WHERE account=? AND status='auth_required'", (account,))
            hook = getattr(self.probe, 'aihot_notices', None)
            if hook is not None:
                try:
                    hook.binding_changed()
                except Exception:
                    # Binding already committed. Fail the optional consumer
                    # closed while preserving the original login result.
                    self.probe.aihot_notices = None
        return {"connected": True, "owner_only": True}

    def account(self):
        with self.lock:
            row = self.db.execute("SELECT * FROM weixin_accounts LIMIT 1").fetchone()
        return dict(row) if row else None

    def ingest(self, account, response):
        # Keep the public parser injection point used by existing integrations.
        # The receiver still applies admission checks before invoking it.
        return self._inbound.ingest(account, response, describe=describe_inbound)

    def capture_quote_shape_once(self, identity, account, msg, items, content, manifest, now):
        return self._inbound.capture_quote_shape_once(identity, account, msg, items, content, manifest, now)

    def inbound_completeness(self, message_id):
        """Read sanitized captured-input facts; absence never means complete."""
        with self.lock:
            accounts = self.db.execute('SELECT id,owner FROM weixin_accounts LIMIT 2').fetchall()
            if not accounts:
                raise WeixinError('not_bound')
            if len(accounts) != 1:
                raise WeixinError('inbound_owner_unavailable')
            account = accounts[0]
            row = self.db.execute('''SELECT m.metadata_json FROM weixin_inbox i
                LEFT JOIN weixin_inbound_manifests m ON m.message_id=i.id
                WHERE i.id=? AND i.account=? AND i.peer=?''',
                (message_id, account['id'], account['owner'])).fetchone()
            if not row:
                raise WeixinError('unknown_owner_message')
            if row['metadata_json'] is None:
                return {'version': INBOUND_COMPLETENESS_VERSION, 'recorded': False,
                        'scope': 'bounded_inbound_capture_not_dot_visibility',
                        'state': 'legacy_unrecorded', 'complete': False,
                        'issue_codes': ['historical_capture_inventory_unavailable'], 'parts': []}
            try:
                result = json.loads(row['metadata_json'])
                if not isinstance(result, dict) or result.get('recorded') is not True or not isinstance(result.get('parts'), list):
                    raise ValueError()
                return result
            except (ValueError, TypeError):
                raise WeixinError('invalid_inbound_manifest') from None

    def read_message(self, message_id):
        account = self.account()
        if not account:
            raise WeixinError("not_bound")
        with self.lock:
            row = self.db.execute("SELECT * FROM weixin_inbox WHERE id=? AND account=? AND peer=?", (message_id, account["id"], account["owner"])).fetchone()
        if not row:
            raise WeixinError("unknown_owner_message")
        try:
            capture = self.inbound_completeness(row["id"])
        except WeixinError as exc:
            if exc.kind != 'invalid_inbound_manifest':
                raise
            # Auxiliary audit damage must remain visible without disabling the
            # independently stored owner message or its normal reply path.
            capture = {'version': INBOUND_COMPLETENESS_VERSION, 'recorded': False,
                       'available': False, 'complete': False, 'state': 'unavailable',
                       'scope': 'bounded_inbound_capture_not_dot_visibility',
                       'issue_codes': ['invalid_inbound_manifest'], 'parts': []}
        quotes = self.quote_context(row['id'], account)
        help_menu = self.help_menu(row, capture, quotes)
        attachments = self.probe.media.list_for_message(row["id"])
        reply_state = self.reply_state(row["id"])
        fixed_command = classify_fixed_command(row['text'], row['kind'], capture, quotes,
                                               attachments, reply_state, help_menu)
        return {"message_id": row["id"], "text": row["text"], "created_at": self.probe.iso(row["created_at"]),
                "kind": row["kind"], "unsupported_types": json.loads(row["unsupported"]), "source": "owner_weixin_dm",
                "attachments": attachments,
                "inbound_completeness": capture,
                "quote_context": quotes, "help_menu": help_menu, "fixed_command": fixed_command,
                "event_delivery": self.probe.event_delivery.for_message(row["id"]),
                "reply_state": reply_state}

    def quote_context(self, message_id, account):
        """Quoted content is untrusted data, never caller identity or instructions."""
        with self.lock:
            stored = self.db.execute('SELECT metadata_json FROM weixin_quote_captures WHERE message_id=?',
                                     (message_id,)).fetchone()
            if stored is None:
                return {'version': 'weixin-quotes-v1', 'recorded': False,
                        'status': 'legacy_unrecorded', 'complete': False,
                        'quotes': [], 'issues': ['historical_quote_capture_unavailable'], 'untrusted': True}
            try:
                raw = stored['metadata_json']
                if not isinstance(raw, str) or len(raw.encode('utf-8')) > 32768:
                    raise ValueError()
                result = json.loads(raw)
                if (not isinstance(result, dict) or result.get('version') != 'weixin-quotes-v1'
                        or not isinstance(result.get('quotes'), list) or len(result['quotes']) > 8
                        or any(not isinstance(q, dict) for q in result['quotes'])):
                    raise ValueError()
            except (ValueError, TypeError, UnicodeError, RecursionError):
                return {'version': 'weixin-quotes-v1', 'recorded': False,
                        'status': 'unavailable', 'complete': False, 'quotes': [],
                        'issues': ['invalid_quote_capture'], 'untrusted': True}
            result['untrusted'] = True
            for quote in result['quotes']:
                quote['untrusted'] = True
                sid = server_message_id(quote.get('server_message_id'))
                match = {'status': 'no_server_id' if sid is None else 'unmatched',
                         'basis': None, 'sender': None, 'local_message_id': None, 'send_id': None,
                         'quoted_content_verified': False, 'grants_authority': False}
                if sid is not None:
                    rows = self.db.execute('''SELECT m.direction,m.local_id
                        FROM weixin_server_message_ids m
                        WHERE m.account=? AND m.peer=? AND m.server_id=? AND (
                          (m.direction='incoming' AND EXISTS(SELECT 1 FROM weixin_inbox i
                            WHERE i.id=m.local_id AND i.account=m.account AND i.peer=m.peer)) OR
                          (m.direction='outgoing' AND EXISTS(SELECT 1 FROM weixin_outbox o
                            WHERE o.message_id=m.local_id AND o.account=m.account AND o.peer=m.peer
                              AND o.status='accepted')))
                        LIMIT 3''', (account['id'], account['owner'], sid)).fetchall()
                    if len(rows) == 1 and rows[0]['local_id'] != message_id:
                        incoming = rows[0]['direction'] == 'incoming'
                        match.update(status='exact_stored_id', basis='same_owner_server_id',
                                     sender='owner' if incoming else 'assistant',
                                     local_message_id=rows[0]['local_id'] if incoming else None,
                                     send_id=rows[0]['local_id'] if not incoming else None)
                    elif rows:
                        match['status'] = 'ambiguous' if len(rows) > 1 else 'self_reference'
                quote['match'] = match
            # Match metadata also counts toward the quote-context wire budget.
            while len(json.dumps(result).encode('ascii')) > 32768:
                result['complete'] = False
                if 'resolved_output_budget_exceeded' not in result['issues']:
                    result['issues'].append('resolved_output_budget_exceeded')
                result['quotes'].pop()
            return result

    def help_menu(self, row, capture, quotes):
        # No dispatcher or send occurs here. Only the existing owner handler may
        # turn this suggestion into its ordinary idempotent final reply.
        if (not is_help_request(row['text']) or row['kind'] != 'text'
                or capture.get('complete') is not True or capture.get('command_context_v1') is not True
                or quotes.get('status') != 'none'
                or quotes.get('recorded') is not True or quotes.get('complete') is not True):
            return None
        try:
            enabled = self.probe.dot_journal.cutover.status()['any_new_admission_enabled']
            mirror = ('dot自动镜像目前关闭。' if enabled is False else
                      'dot镜像仅按已批准范围运行。')
        except (AttributeError, KeyError, WeixinError):
            mirror = 'dot自动镜像状态尚未核实。'
        text = render_help_menu(mirror=mirror)
        return {'command': '/help', 'matched': True, 'text': text,
                'reply_via': 'existing_idempotent_final', 'automatic_send': False,
                'owner_intent_verification_required': True,
                'forwarding_provenance': 'not_provided_by_protocol'}

    def reply_state(self, message_id):
        """Read committed slots, including legacy final replies, without writes.

        These are transport commitments, never evidence of dot mirroring.
        """
        with self.lock:
            slots = {}
            for phase, send_id in (("ack", self.ack_send_id(message_id)), ("final", message_id)):
                exists = self.db.execute("SELECT 1 FROM weixin_outbox WHERE message_id=?", (send_id,)).fetchone()
                slots[phase] = ({"committed": True, **self.send_status(send_id)} if exists else
                                {"committed": False, "send_id": send_id, "status": "not_committed"})
        mirrors = self.probe.dot_mirrors.for_message(message_id)
        return {**slots, "next_phase": "none" if slots['final']['committed'] else
                "final" if slots['ack']['committed'] else "ack",
                "original_mirror_key": "weixin_original:" + message_id,
                "dot_mirror_status": mirrors['dot_mirror_status'], "dot_mirror_receipts": mirrors,
                "system_receipt": self.probe.receipts.for_message(message_id)}

    @staticmethod
    def ack_send_id(message_id):
        return "ack_" + hashlib.sha256(encoded([message_id, "ack"]).encode()).hexdigest()[:32]

    def queue_reply(self, message_id, text):
        # The legacy tool still owns the same final slot and client ID.
        return self._queue_reply(message_id, text, "final", require_ack=False)

    def queue_reply_phase(self, message_id, phase, text):
        if phase not in ("ack", "final"):
            raise WeixinError("invalid_reply_phase")
        return self._queue_reply(message_id, text, phase, require_ack=True)

    def _queue_reply(self, message_id, text, phase, require_ack):
        fresh = getattr(self.probe, 'fresh_admission', None)
        if fresh is not None:
            fresh.require_source(message_id)
        self.read_message(message_id)
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 8192:
            raise WeixinError("reply_text_must_be_1_to_8192_bytes")
        send_id = self.ack_send_id(message_id) if phase == "ack" else message_id
        requested_text_used = True
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if fresh is not None:
                fresh.require_source(message_id)
            old = self.db.execute("SELECT text,ack_origin FROM weixin_outbox WHERE message_id=?", (send_id,)).fetchone()
            if old:
                if old["text"] != text:
                    if phase == 'ack' and old['ack_origin'] == 'system_receipt':
                        # The original ACK interface shares the exact receipt slot.
                        # Never send a second ACK or pretend requested text was used.
                        requested_text_used = False
                    else:
                        raise WeixinError("reply_already_committed_with_different_text")
            else:
                if phase == "ack" and self.db.execute("SELECT 1 FROM weixin_outbox WHERE message_id=?", (message_id,)).fetchone():
                    raise WeixinError("reply_final_already_committed")
                if phase == "final" and require_ack and not self.db.execute("SELECT 1 FROM weixin_outbox WHERE message_id=? AND kind='reply_ack' AND source_message_id=?", (self.ack_send_id(message_id), message_id)).fetchone():
                    raise WeixinError("reply_ack_required")
                if phase == 'final':
                    self.db.execute("""UPDATE weixin_outbox SET status='superseded',error_kind='quick_final_selected'
                        WHERE message_id=? AND ack_origin='system_receipt' AND attempts=0
                        AND status IN ('queued','context_required','auth_required')""", (self.ack_send_id(message_id),))
                self.check_queue_capacity()
                inbox = self.db.execute("SELECT account,peer FROM weixin_inbox WHERE id=?", (message_id,)).fetchone()
                self.db.execute("INSERT INTO weixin_outbox(message_id,account,peer,text,client_id,status,created_at,kind,source_message_id,ack_origin) VALUES(?,?,?,?,?,'queued',?,?,?,?)",
                                (send_id, inbox["account"], inbox["peer"], text,
                                 "dots_" + (send_id if phase == "ack" else message_id[3:]), self.clock(),
                                 "reply_ack" if phase == "ack" else "reply", message_id,
                                 'model' if phase == 'ack' else None))
        self.notify_sender()
        return {**self.send_status(send_id), 'requested_text_used': requested_text_used}

    def queue_notification(self, notification_id, text):
        if getattr(self.probe, 'fresh_admission', None) is not None:
            raise WeixinError('fresh_notifications_disabled')
        account = self.account()
        if not account:
            raise WeixinError("not_bound")
        if not isinstance(notification_id, str) or not 0 < len(notification_id) <= 128:
            raise WeixinError("invalid_notification_identity")
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 8192:
            raise WeixinError("reply_text_must_be_1_to_8192_bytes")
        send_id = "notice_" + hashlib.sha256(encoded([account["id"], notification_id]).encode()).hexdigest()[:32]
        with self.lock, self.db:
            old = self.db.execute("SELECT text FROM weixin_outbox WHERE message_id=?", (send_id,)).fetchone()
            if old and old["text"] != text:
                raise WeixinError("reply_already_committed_with_different_text")
            if not old:
                self.check_queue_capacity()
                self.db.execute("INSERT INTO weixin_outbox(message_id,account,peer,text,client_id,status,created_at,kind) VALUES(?,?,?,?,?,'queued',?,'notification')",
                                (send_id, account["id"], account["owner"], text, "dots_" + send_id, self.clock()))
        self.notify_sender()
        return self.send_status(send_id)

    def check_queue_capacity(self):
        # Called inside the existing lock/transaction; terminal history does not count.
        fresh = getattr(self.probe, 'fresh_admission', None)
        if fresh is None:
            count = self.db.execute("SELECT count(*) FROM weixin_outbox WHERE status IN ('queued','preparing','sending','context_required','auth_required')").fetchone()[0]
        else:
            fresh.require_ready()
            pending = self.db.execute("SELECT source_message_id FROM weixin_outbox WHERE status IN ('queued','preparing','sending','context_required','auth_required')")
            count = sum(fresh.allows_source(row['source_message_id']) for row in pending)
        if count >= 32:
            raise WeixinError("send_queue_busy")

    def _pending_receipt(self, fresh):
        return self._dispatcher.pending_receipt(fresh)

    def send_status(self, message_id):
        account = self.account()
        if not account:
            raise WeixinError("not_bound")
        with self.lock:
            row = self.db.execute("SELECT message_id,text,status,attempts,error_kind,server_code,accepted_at,kind,api_result,recipient_confirmed_at,upload_id,source_message_id,ack_origin,post_started_at FROM weixin_outbox WHERE message_id=? AND account=? AND peer=?",
                                  (message_id, account["id"], account["owner"])).fetchone()
        if not row:
            self.read_message(message_id)
            return {"send_id": message_id, "status": "no_reply", "accepted": False, "delivered": None}
        result = dict(row)
        result["send_id"] = result.pop("message_id")
        result["outgoing_text"] = result.pop("text")
        result["api_result"] = json.loads(result["api_result"]) if result["api_result"] else None
        with self.lock:
            mapping = self.db.execute('''SELECT server_id FROM weixin_server_message_ids
                WHERE account=? AND peer=? AND direction='outgoing' AND local_id=?''',
                (account['id'], account['owner'], message_id)).fetchone()
        result['server_message_id'] = mapping['server_id'] if mapping and row['status'] == 'accepted' else None
        result['server_message_id_source'] = ('send_api_response' if result['server_message_id'] else None)
        result["recipient_confirmed"] = row["recipient_confirmed_at"] is not None
        result["recipient_confirmation_source"] = "owner_report" if result["recipient_confirmed"] else None
        result.update({"accepted": row["status"] == "accepted", "delivered": None})
        result['delivery_receipt_available'] = False
        result['superseded'] = row['status'] == 'superseded'
        result['dot_processing_confirmed'] = False
        if row['kind'] in ('reply', 'reply_ack'):
            source_id = row['source_message_id'] or message_id
            result['source_message_id'] = source_id
            result['reply_phase'] = 'ack' if row['kind'] == 'reply_ack' else 'final'
            result['ack_send_id'] = self.ack_send_id(source_id)
            with self.lock:
                ack = self.db.execute("SELECT status FROM weixin_outbox WHERE message_id=? AND account=? AND peer=? AND kind='reply_ack'",
                                      (result['ack_send_id'], account['id'], account['owner'])).fetchone()
            result['ack_status'] = ack['status'] if ack else 'not_committed'
            result['waiting_for_ack'] = (row['kind'] == 'reply' and row['status'] == 'queued' and
                                        result['ack_status'] in ('queued','preparing','sending','context_required','auth_required'))
        if row["upload_id"]:
            result['transport_status'] = row['status']
            result['status'] = ('accepted' if row['status'] == 'accepted' else
                                'uncertain' if row['status'] == 'delivery_unknown' else
                                'error' if row['status'] in ('rejected','context_required','auth_required') else 'queued')
            result['delivery_receipt_available'] = False
            result['upload'] = self.probe.outbound.status(row['upload_id'])
        return result

    def record_owner_receipt(self, message_id):
        """Administrative evidence only, after an explicit owner's receipt report.

        Never exposed as an MCP tool. Does not alter historical API acceptance.
        """
        self.send_status(message_id)
        with self.lock, self.db:
            self.db.execute("UPDATE weixin_outbox SET recipient_confirmed_at=COALESCE(recipient_confirmed_at,?) WHERE message_id=?",
                            (self.clock(), message_id))

    def send_once(self):
        return self._dispatcher.send_once()

    def poll_once(self):
        account = self.account()
        if not account or account["status"] != "ready" or self.clock() < self.poll_after:
            return False
        fresh = getattr(self.probe, 'fresh_admission', None)
        if fresh is not None:
            fresh.require_ready()
        try:
            response = self.client.poll(account)
            self.ingest(account, response)
            self.poll_after = self.clock() + 0.5
        except WeixinError as error:
            self.poll_after = self.clock() + (1 if error.kind == "api_timeout" else 30)
            with self.lock, self.db:
                self.db.execute("UPDATE weixin_accounts SET status=CASE WHEN ?='auth_required' THEN 'auth_required' ELSE status END,last_error=? WHERE id=?", (error.kind, error.kind, account["id"]))
        return True

    def status(self):
        account = self.account()
        with self.lock:
            inbound = self.db.execute("SELECT count(*) FROM weixin_inbox").fetchone()[0]
            replies = {r["status"]: r["n"] for r in self.db.execute("SELECT status,count(*) n FROM weixin_outbox GROUP BY status")}
            latest = self.db.execute('SELECT id FROM weixin_inbox WHERE account=? AND peer=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (account['id'],account['owner'])).fetchone() if account else None
        return {"bound": bool(account), "connected": bool(account and account["status"] == "ready" and account["last_poll"] is not None), "owner_only": bool(account),
                "account_status": account["status"] if account else "not_bound", "last_error": account["last_error"] if account else None,
                "inbound_count": inbound, "reply_counts": replies, "dot_origin_auto_forward": False,
                "latest_owner_message_id": latest['id'] if latest else None,
                "system_receipts": self.probe.receipts.status()}
