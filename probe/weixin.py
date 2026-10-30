"""Owner-only Tencent iLink text transport; no model or ChatGPT history access.

Protocol reference: https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md
Credentials stay in the private server database. Exceptions contain fixed categories only.
"""
import base64
import hashlib
import http.client
import ipaddress
import json
import re
import secrets
import socket
import ssl
import time
from urllib.parse import urlencode, urlsplit
try:
    from .quotes import capture_quotes, server_message_id, quote_shape
    from .fixed_commands import capture_command_context, classify as classify_fixed_command
except ImportError:
    from quotes import capture_quotes, server_message_id, quote_shape
    from fixed_commands import capture_command_context, classify as classify_fixed_command

BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.9"
MESSAGE_EVENT = "weixin.message"
MAX_INBOUND_TEXT_BYTES = 65536
MAX_INBOUND_ATTACHMENTS = 8
MAX_INBOUND_MANIFEST_ITEMS = 64
INBOUND_COMPLETENESS_VERSION = "inbound-completeness-v1"


def inbound_part_id(message_id, item_index):
    return "wip_" + hashlib.sha256(encoded([message_id, item_index]).encode()).hexdigest()[:32]


def inbound_type_metadata(value):
    """Retain a bounded protocol type identifier, never an arbitrary payload."""
    if type(value) is int:
        if -(2 ** 63) <= value < 2 ** 63:
            return {"item_type": value, "item_type_kind": "integer"}
        return {"item_type_kind": "integer", "item_type_out_of_range": True}
    if isinstance(value, str):
        if len(value) <= 64 and re.fullmatch(r'[A-Za-z0-9_.:-]+', value):
            return {"item_type": value, "item_type_kind": "string"}
        return {"item_type_kind": "string", "item_type_omitted": True}
    kind = ("null" if value is None else "boolean" if type(value) is bool else
            "number" if type(value) is float else "array" if isinstance(value, list) else
            "object" if isinstance(value, dict) else "invalid")
    return {"item_type_kind": kind}


def media_ingest_items(raw_items):
    """Keep original indices while excluding invalid protocol types/transcripts.

    MediaStore is also used independently and historically accepts equal-valued
    float item types. Here the recorded strict source inventory is authoritative.
    Never pass invalid or unbounded voice text into its provenance encoder.
    """
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict) or type(item.get('type')) is not int:
            yield {}
            continue
        if item['type'] == 3 and isinstance(item.get('voice_item'), dict):
            voice = item['voice_item']
            transcript = voice.get('text')
            valid = isinstance(transcript, str) and len(transcript) <= MAX_INBOUND_TEXT_BYTES
            if valid:
                try:
                    valid = len(transcript.encode('utf-8')) <= MAX_INBOUND_TEXT_BYTES
                except UnicodeError:
                    valid = False
            if not valid and transcript is not None:
                item = {**item, 'voice_item': {**voice, 'text': ''}}
        yield item


def describe_inbound(message_id, raw_items):
    """Capture a bounded text prefix and sanitized source-item inventory.

    No raw item objects, media locators, credentials or text bodies enter the
    inventory. The existing 64KiB text and eight-attachment limits stay intact.
    The first 64 item identities plus one range placeholder bound metadata even
    for a response containing very many small or malformed items.
    """
    items = raw_items if isinstance(raw_items, list) else []
    issues, parts, text_chunks = set(), [], []
    text_bytes, text_source_bytes, text_count, attachment_count = 0, 0, 0, 0
    text_closed, text_truncated = False, False

    def append_text(value):
        nonlocal text_bytes, text_source_bytes, text_count, text_closed, text_truncated
        source_bytes, stored_bytes, part_issues = 0, 0, []
        if text_count:
            if text_source_bytes is not None:
                text_source_bytes += 1
            if not text_closed and text_bytes < MAX_INBOUND_TEXT_BYTES:
                text_chunks.append("\n")
                text_bytes += 1
            else:
                text_closed = text_truncated = True
                part_issues.append("text_too_large")
        text_count += 1
        # Encode small slices, never a second unbounded joined/encoded payload.
        for start in range(0, len(value), 4096):
            try:
                chunk = value[start:start + 4096].encode("utf-8")
            except UnicodeError:
                source_bytes = text_source_bytes = None
                text_closed = text_truncated = True
                part_issues.append("invalid_text_encoding")
                break
            source_bytes += len(chunk)
            if text_source_bytes is not None:
                text_source_bytes += len(chunk)
            if text_closed:
                continue
            available = MAX_INBOUND_TEXT_BYTES - text_bytes
            prefix = chunk[:available].decode("utf-8", errors="ignore")
            kept = len(prefix.encode("utf-8"))
            if prefix:
                text_chunks.append(prefix)
                stored_bytes += kept
                text_bytes += kept
            if len(chunk) > available:
                # Never fill unused bytes with later items after a split UTF-8
                # character: the retained text must remain a source prefix.
                text_closed = text_truncated = True
        if source_bytes is not None and stored_bytes < source_bytes:
            if "text_too_large" not in part_issues:
                part_issues.append("text_too_large")
            text_truncated = True
        issues.update(part_issues)
        return {"text_source_bytes": source_bytes, "text_stored_bytes": stored_bytes}, part_issues

    if not isinstance(raw_items, list) or not items:
        reason = "invalid_item_list" if not isinstance(raw_items, list) else "empty_item_list"
        issues.add(reason)
        parts.append({"part_id": inbound_part_id(message_id, -1), "item_index": -1,
                      "kind": "malformed", "state": "invalid", "issue_codes": [reason]})
    for index, item in enumerate(items):
        part = {"part_id": inbound_part_id(message_id, index), "item_index": index,
                "kind": "malformed", "state": "invalid", "issue_codes": []}
        item_type = item.get("type") if isinstance(item, dict) else None
        part.update(inbound_type_metadata(item_type))
        if not isinstance(item, dict):
            part["issue_codes"].append("invalid_item")
        elif type(item_type) is not int:
            part["issue_codes"].append("invalid_item_type")
        elif item_type == 1:
            part["kind"] = "text"
            data = item.get("text_item")
            value = data.get("text") if isinstance(data, dict) else None
            if not isinstance(value, str):
                part["issue_codes"].append("invalid_text_item")
            else:
                fields, text_issues = append_text(value)
                part.update(fields, state="truncated" if text_issues else "captured")
                part["issue_codes"].extend(text_issues)
        elif item_type in (2, 3, 4, 5):
            attachment_count += 1
            kind = {2: "image", 3: "voice", 4: "file", 5: "video"}[item_type]
            part.update(kind=kind, state="captured")
            data = item.get(kind + "_item")
            if attachment_count > MAX_INBOUND_ATTACHMENTS:
                part.update(state="omitted")
                part["issue_codes"].append("attachment_limit_exceeded")
            else:
                # Identical to MediaStore.add's original-item-index identity,
                # including malformed media that becomes an explicit error row.
                part["attachment_id"] = "att_" + hashlib.sha256(encoded([message_id, index]).encode()).hexdigest()[:32]
            if not isinstance(data, dict):
                if part["state"] != "omitted":
                    part["state"] = "invalid"
                part["issue_codes"].append("invalid_media_item")
            elif kind == "voice":
                transcript = data.get("text")
                if isinstance(transcript, str) and transcript:
                    fields, text_issues = append_text(transcript)
                    part.update(fields)
                    if text_issues and part["state"] != "omitted":
                        part["state"] = "truncated"
                    part["issue_codes"].extend(text_issues)
                elif transcript is not None and not isinstance(transcript, str):
                    part["issue_codes"].append("invalid_voice_transcript")
                    if part["state"] != "omitted":
                        part["state"] = "invalid"
        else:
            part.update(kind="unknown", state="unsupported")
            part["issue_codes"].append("unsupported_item_type")
        issues.update(part["issue_codes"])
        if index < MAX_INBOUND_MANIFEST_ITEMS:
            parts.append(part)
    omitted = max(0, len(items) - MAX_INBOUND_MANIFEST_ITEMS)
    if omitted:
        issues.add("item_manifest_limit_exceeded")
        parts.append({"part_id": inbound_part_id(message_id, "overflow_64"),
                      "item_index": None, "item_index_start": MAX_INBOUND_MANIFEST_ITEMS,
                      "item_index_end": len(items) - 1, "item_count": omitted,
                      "kind": "overflow", "state": "omitted",
                      "issue_codes": ["item_manifest_limit_exceeded"]})
    if text_truncated:
        issues.add("text_incomplete")
    manifest = {"version": INBOUND_COMPLETENESS_VERSION, "recorded": True,
                "scope": "bounded_inbound_capture_not_dot_visibility",
                "complete": not issues, "source_item_count": len(items) if isinstance(raw_items, list) else None,
                "text_source_bytes": text_source_bytes, "text_stored_bytes": text_bytes,
                "text_truncated": text_truncated, "text_limit_bytes": MAX_INBOUND_TEXT_BYTES,
                "attachment_source_count": attachment_count, "attachment_limit": MAX_INBOUND_ATTACHMENTS,
                "item_manifest_limit": MAX_INBOUND_MANIFEST_ITEMS, "items_omitted_count": omitted,
                "issue_codes": sorted(issues), "parts": parts}
    return "".join(text_chunks), bool(attachment_count), manifest


class WeixinError(Exception):
    def __init__(self, kind, code=None):
        super().__init__(kind)
        self.kind, self.code = kind, code


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def api_base(value):
    try:
        p = urlsplit(value)
        host = p.hostname
        if (p.scheme != "https" or not host or not (host == "weixin.qq.com" or host.endswith(".weixin.qq.com"))
                or p.port not in (None, 443) or p.username or p.password or p.query or p.fragment
                or p.path not in ("", "/")):
            raise ValueError()
        return "https://" + host
    except (TypeError, ValueError):
        raise WeixinError("invalid_api_destination") from None


def public_ip(value):
    ip = ipaddress.ip_address(value)
    return (ip.is_global and not ip.is_multicast and not ip.is_reserved
            and not (ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)))


def parse_api_json(raw):
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise WeixinError("invalid_api_json") from None
    if not isinstance(result, dict):
        raise WeixinError("invalid_api_response")
    return result


class IlinkClient:
    def __init__(self, clock=time.time):
        self.clock = clock

    def request(self, base, endpoint, payload=None, token=None, timeout=20):
        host = urlsplit(api_base(base)).hostname
        if not endpoint.startswith("/ilink/bot/") or "\r" in endpoint or "\n" in endpoint:
            raise WeixinError("invalid_api_endpoint")
        headers = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": str((2 << 16) | (4 << 8) | 9)}
        body = None
        if payload is not None:
            headers.update({"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                            "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode()})
            if token:
                headers["Authorization"] = "Bearer " + token
                payload = {**payload, "base_info": {"channel_version": CHANNEL_VERSION, "bot_agent": "DotsWechatBridge/0.2"}}
            body = encoded(payload).encode()
        try:
            addresses = list(dict.fromkeys(x[4][0] for x in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
            if not addresses or not all(public_ip(x) for x in addresses):
                raise WeixinError("non_public_api_address")
            conn = http.client.HTTPSConnection(host, timeout=timeout, context=ssl.create_default_context())
            sock = socket.create_connection((addresses[0], 443), timeout=timeout)
            try:
                conn.sock = conn._context.wrap_socket(sock, server_hostname=host)
            except BaseException:
                sock.close()
                raise
            try:
                conn.request("POST" if payload is not None else "GET", endpoint, body, headers)
                response = conn.getresponse()
                raw = response.read(1048577)
                if len(raw) > 1048576:
                    raise WeixinError("api_response_too_large")
                if not 200 <= response.status < 300:
                    # No automatic redirects and no logging response bodies or URLs.
                    raise WeixinError("api_http_error", response.status)
                return parse_api_json(raw)
            finally:
                conn.close()
        except WeixinError:
            raise
        except (socket.timeout, TimeoutError):
            raise WeixinError("api_timeout") from None
        except Exception:
            raise WeixinError("api_network_error") from None

    def begin_login(self):
        result = self.request(BASE_URL, "/ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []})
        if not all(isinstance(result.get(k), str) and 0 < len(result[k]) < 8192 for k in ("qrcode", "qrcode_img_content")):
            raise WeixinError("invalid_login_response")
        return {"qrcode": result["qrcode"], "display": result["qrcode_img_content"], "base": BASE_URL,
                "started_at": self.clock()}

    def poll_login(self, login, verification_code=None):
        if self.clock() - login["started_at"] > 300:
            return {"status": "expired"}
        query = {"qrcode": login["qrcode"]}
        if verification_code:
            query["verify_code"] = verification_code
        return self.request(login["base"], "/ilink/bot/get_qrcode_status?" + urlencode(query), timeout=40)

    def poll(self, account):
        return self.request(account["base_url"], "/ilink/bot/getupdates", {"get_updates_buf": account["cursor"]},
                            account["token"], timeout=40)

    def send(self, account, peer, text, client_id, context_token):
        return self.send_items(account, peer, [{"type": 1, "text_item": {"text": text}}], client_id, context_token)

    def get_upload_url(self, account, payload):
        return self.request(account["base_url"], "/ilink/bot/getuploadurl", payload, account["token"])

    def send_items(self, account, peer, items, client_id, context_token):
        return self.request(account["base_url"], "/ilink/bot/sendmessage", {"msg": {
            "from_user_id": "", "to_user_id": peer, "client_id": client_id, "message_type": 2,
            "message_state": 2, "context_token": context_token,
            "item_list": items}}, account["token"])


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
    def notify_sender(self):
        notify = getattr(self.probe, "notify_sender", None)
        if notify:
            notify()

    def recover_inflight(self):
        # Only the single daemon calls this, never a read-only status process.
        with self.db:
            self.db.execute("UPDATE weixin_outbox SET status='delivery_unknown',error_kind='restart_during_send' WHERE status='sending'")

    def bind_confirmed(self, result):
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
        codes = (response.get("ret", 0), response.get("errcode", 0))
        if any(x not in (0, None) for x in codes):
            raise WeixinError("auth_required" if -14 in codes else "poll_rejected", next(x for x in codes if x not in (0, None)))
        msgs = response.get("msgs", [])
        if not isinstance(msgs, list) or len(msgs) > 1000:
            raise WeixinError("invalid_update_batch")
        added = 0
        observed_ids = []
        now = self.clock()
        with self.lock, self.db:
            for msg in msgs:
                if (not isinstance(msg, dict) or msg.get("from_user_id") != account["owner"]
                        or msg.get("group_id") or msg.get("room_id") or msg.get("message_type") != 1
                        or msg.get("message_state") == 1 or msg.get("delete_time_ms")
                        or msg.get("to_user_id") not in (None, "", account["id"])):
                    continue
                source = msg.get("message_id") or msg.get("client_id")
                if not isinstance(source, (str, int)) or isinstance(source, bool) or not source:
                    continue
                identity = "wx_" + hashlib.sha256(encoded([account["id"], account["owner"], str(source)]).encode()).hexdigest()[:32]
                items = msg.get("item_list", [])
                content, has_media, manifest = describe_inbound(identity, items)
                manifest['command_context_v1'] = capture_command_context(msg)
                unsupported = manifest['issue_codes']
                created_ms = msg.get("create_time_ms")
                created_ms = created_ms if isinstance(created_ms, (float, int)) and 0 < created_ms <= (now + 300) * 1000 else now * 1000
                changed = self.db.execute("""INSERT OR IGNORE INTO weixin_inbox
                    (id,account,peer,source_id,text,created_at,kind,unsupported,event_id)
                    VALUES(?,?,?,?,?,?,?,?,NULL)""",
                    (identity, account["id"], account["owner"], str(source), content, created_ms / 1000,
                     "mixed" if content and has_media else "media" if has_media else "text" if content else "unsupported", encoded(unsupported))).rowcount
                if not changed:
                    continue
                # The source inventory shares the inbox/cursor transaction.
                # Duplicate retrievals preserve the first accepted source facts;
                # legacy inputs without an inventory remain explicitly unknown.
                self.db.execute('INSERT INTO weixin_inbound_manifests(message_id,metadata_json) VALUES(?,?)',
                                (identity, encoded(manifest)))
                self.db.execute('INSERT INTO weixin_quote_captures(message_id,metadata_json) VALUES(?,?)',
                                (identity, encoded(capture_quotes(items))))
                self.capture_quote_shape_once(identity, account, msg, items, content, manifest, now)
                sid = server_message_id(msg.get('message_id'))
                if sid is not None:
                    self.db.execute('INSERT OR IGNORE INTO weixin_server_message_ids VALUES(?,?,?,?,?)',
                                    (account['id'], account['owner'], 'incoming', identity, sid))
                added += 1
                observed_ids.append(identity)
                self.probe.delivery_timing.ingested(identity, now)
                self.probe.media.add(identity, account["id"], account["owner"], media_ingest_items(items))
                context = msg.get("context_token")
                if isinstance(context, str) and 0 < len(context) <= 16384:
                    old = self.db.execute("SELECT source_ms FROM weixin_peers WHERE account=? AND peer=?", (account["id"], account["owner"])).fetchone()
                    if not old or created_ms >= old["source_ms"]:
                        self.db.execute("INSERT OR REPLACE INTO weixin_peers VALUES(?,?,?,?,?)", (account["id"], account["owner"], context, created_ms, now))
                        self.db.execute("UPDATE weixin_outbox SET status='queued',next_at=0 WHERE account=? AND peer=? AND status='context_required' AND attempts<3",
                                        (account["id"], account["owner"]))
                self.probe.receipts.observe_new_in_transaction(identity)
            cursor = response.get("get_updates_buf")
            self.db.execute("UPDATE weixin_accounts SET cursor=CASE WHEN ?!='' THEN ? ELSE cursor END,last_poll=?,last_error=NULL WHERE id=?",
                            (cursor if isinstance(cursor, str) else "", cursor if isinstance(cursor, str) else "", now, account["id"]))
        if added:
            # Cursor, owner context, inbox and media references are already
            # committed. Timestamp failure must not undo successful ingestion.
            try:
                self.probe.delivery_timing.committed(observed_ids, self.clock())
            finally:
                self.notify_sender()
                self.probe.notify_events()
        return added

    def capture_quote_shape_once(self, identity, account, msg, items, content, manifest, now):
        """Called only after a newly inserted, authenticated owner inbox row.

        Shape evidence is private diagnostics, never quote matching evidence.
        No reads of old messages, sends, downloads or arbitrary field traversal.
        """
        if (content != '引用验收B20261026' or manifest.get('complete') is not True
                or not isinstance(items, list) or len(items) != 1
                or not isinstance(items[0], dict) or type(items[0].get('type')) is not int
                or items[0]['type'] != 1 or 'ref_msg' not in items[0]
                or not isinstance(items[0].get('text_item'), dict)
                or items[0]['text_item'].get('text') != '引用验收B20261026'
                or msg.get('message_state') != 2):
            return
        scope = hashlib.sha256(encoded([account['id'], account['owner']]).encode()).hexdigest()
        state = self.db.execute('SELECT * FROM weixin_quote_shape_once WHERE id=1').fetchone()
        source_ms = msg.get('create_time_ms')
        if (state is None or state['message_id'] is not None or state['binding_hash'] != scope
                or type(source_ms) not in (int, float)
                or not state['armed_at'] * 1000 <= source_ms <= now * 1000):
            return
        shape = quote_shape(items[0]['ref_msg'])
        self.db.execute('''UPDATE weixin_quote_shape_once
            SET message_id=?,shape_json=?,captured_at=?
            WHERE id=1 AND message_id IS NULL AND binding_hash=?''',
            (identity, encoded(shape), now, scope))

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
        if (row['text'].strip() != '/help' or row['kind'] != 'text'
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
        text = ('🤖 微信助手 · 使用指南\n\n'
                '💬 直接说需求\n'
                '查资料、整理任务、分析内容。\n\n'
                '🖼️ 图片 / 📄 文件 / 🎬 视频\n'
                '发来时说一句：希望我做什么。\n'
                '文件≤20 MiB；超长图建议在dot上传。\n\n'
                '↩️ 接着聊\n'
                '可引用消息补充，识别不全时贴上关键文字。\n\n'
                '⏳ 查看进度\n'
                '直接问“进度怎么样”；没收到先查，不盲目重发。\n\n'
                '🔒 仅本人私聊\n'
                '不支持群聊；' + mirror + '\n\n'
                '📊 额度：查询当前 Codex 周额度\n\n'
                '❔ 再看帮助：/help')
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
        self.read_message(message_id)
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 8192:
            raise WeixinError("reply_text_must_be_1_to_8192_bytes")
        send_id = self.ack_send_id(message_id) if phase == "ack" else message_id
        requested_text_used = True
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
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
        count = self.db.execute("SELECT count(*) FROM weixin_outbox WHERE status IN ('queued','preparing','sending','context_required','auth_required')").fetchone()[0]
        if count >= 32:
            raise WeixinError("send_queue_busy")

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
        account = self.account()
        if not account or account["status"] != "ready":
            return False
        now = self.clock()
        with self.lock, self.db:
            # Waiting finals do not monopolize the queue. Only their own ack
            # blocks them, including its bounded backoff/context/auth wait.
            # An uncertain ack is never resent; the independent final may proceed.
            rows = self.db.execute("""SELECT outgoing.* FROM weixin_outbox outgoing
                WHERE outgoing.status='queued' AND outgoing.next_at<=? AND outgoing.account=?
                AND NOT (outgoing.kind='reply' AND EXISTS (
                    SELECT 1 FROM weixin_outbox ack WHERE ack.source_message_id=outgoing.message_id
                    AND ack.kind='reply_ack' AND ack.status IN ('queued','preparing','sending','context_required','auth_required')))
                ORDER BY CASE
                    WHEN outgoing.kind='reply_ack' AND outgoing.ack_origin='system_receipt' THEN 0
                    WHEN outgoing.kind IN ('reply','reply_ack') AND outgoing.upload_id IS NULL THEN 1
                    ELSE 2 END,outgoing.created_at,outgoing.rowid""", (now, account["id"]))
            row = next((item for item in rows if item['kind'] != 'notification' or
                        not self.aihot_hold_all_notifications and item['message_id'] not in self.aihot_held_notification_ids), None)
            if not row:
                return False
            if row['attempts'] >= 3:
                self.db.execute("UPDATE weixin_outbox SET status='rejected',error_kind='retry_limit' WHERE message_id=?", (row['message_id'],))
                return True
            if row['peer'] != account['owner']:
                self.db.execute("UPDATE weixin_outbox SET status='rejected',error_kind='owner_identity_mismatch' WHERE message_id=?", (row['message_id'],))
                return True
            context = self.db.execute("SELECT token,received_at FROM weixin_peers WHERE account=? AND peer=?", (account["id"], row["peer"])).fetchone()
            if not context:
                self.db.execute("UPDATE weixin_outbox SET status='context_required',error_kind='fresh_inbound_required' WHERE message_id=?", (row["message_id"],))
                return True
            if row['upload_id'] and not row['item_json'] and self.probe.receipts.pending_receipt():
                return False
            if row['upload_id'] and not row['item_json']:
                self.db.execute("UPDATE weixin_outbox SET status='preparing' WHERE message_id=?", (row['message_id'],))
        if row['upload_id'] and not row['item_json']:
            try:
                item = self.probe.outbound.prepare(row['upload_id'], account)
                with self.lock, self.db:
                    self.db.execute("UPDATE weixin_outbox SET item_json=?,status='queued',error_kind=NULL WHERE message_id=?", (encoded(item), row['message_id']))
            except Exception as exc:
                kind = exc.kind if isinstance(exc, WeixinError) else 'media_upload_error'
                with self.lock, self.db:
                    upload = self.db.execute('SELECT upload_attempts FROM weixin_uploads WHERE id=?', (row['upload_id'],)).fetchone()
                    retry = upload['upload_attempts'] < 3 and (kind in ('media_upload_timeout','media_upload_network_error','api_timeout','api_network_error') or
                        (kind in ('media_upload_http_error','api_http_error') and isinstance(exc,WeixinError) and type(exc.code) is int and exc.code >= 500))
                    state = 'auth_required' if kind == 'auth_required' else 'queued' if retry else 'rejected'
                    self.db.execute("UPDATE weixin_outbox SET status=?,error_kind=?,next_at=? WHERE message_id=?", (state,kind,self.clock()+5*upload['upload_attempts'],row['message_id']))
                    if state == 'auth_required':
                        self.db.execute("UPDATE weixin_accounts SET status='auth_required',last_error='bot_auth_expired' WHERE id=?", (account['id'],))
                return True
        # CDN preparation may take time. Re-read identity, expiry and the latest
        # peer context immediately before committing a message POST.
        account = self.account()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT * FROM weixin_outbox WHERE message_id=?', (row['message_id'],)).fetchone()
            if not account or account['status'] != 'ready' or row['status'] != 'queued':
                return True
            if row['kind'] == 'notification' and (self.aihot_hold_all_notifications or row['message_id'] in self.aihot_held_notification_ids):
                return False
            if row['upload_id']:
                try:
                    self.probe.outbound.authorized(row['upload_id'])
                    self.read_message(row['source_message_id'])
                except WeixinError as exc:
                    self.db.execute("UPDATE weixin_outbox SET status='rejected',error_kind=?,item_json=NULL WHERE message_id=?", (exc.kind,row['message_id']))
                    return True
            context = self.db.execute("SELECT token,received_at FROM weixin_peers WHERE account=? AND peer=?", (account['id'],row['peer'])).fetchone()
            if not context:
                self.db.execute("UPDATE weixin_outbox SET status='context_required',error_kind='fresh_inbound_required' WHERE message_id=?", (row['message_id'],))
                return True
            changed = self.db.execute("UPDATE weixin_outbox SET status='sending',attempts=attempts+1,context_at=?,post_started_at=? WHERE message_id=? AND status='queued' AND attempts=?",
                                      (context["received_at"], self.clock(), row["message_id"], row['attempts'])).rowcount
            if changed != 1:
                return True
        outcome, kind, code = "delivery_unknown", "uncertain_api_result", None
        diagnostic = {"response_received": False}
        response_server_id = None
        try:
            if row['upload_id']:
                result = self.client.send_items(account, row['peer'], [json.loads(row['item_json'])], row['client_id'], context['token'])
            else:
                result = self.client.send(account, row["peer"], row["text"], row["client_id"], context["token"])
            if not isinstance(result, dict):
                raise WeixinError("invalid_api_response")
            codes = (result.get("ret"), result.get("errcode"))
            # The official API defines ret as optional; a successful HTTP JSON
            # object with omitted/null/zero codes is accepted, including {}.
            # Keep only controlled structural evidence, never response bodies.
            diagnostic = {"response_received": True, "valid_json_object": True, "empty_object": not result,
                          "ret_present": "ret" in result, "errcode_present": "errcode" in result,
                          "ret": codes[0] if type(codes[0]) is int else None,
                          "errcode": codes[1] if type(codes[1]) is int else None,
                          "has_server_message_id": isinstance(result.get("message_id"), (str, int)) and not isinstance(result.get("message_id"), bool),
                          "unexpected_base_resp": "base_resp" in result}
            if any(x is not None and type(x) is not int for x in codes):
                kind = "invalid_send_status_type"
            elif -2 in codes and any(x not in (0, None, -2) for x in codes):
                # A mixed explicit failure must not be hidden by a generic -2.
                # Preserve the original commitment and stop this send.
                outcome, kind = "rejected", "send_rejected"
                code = next(x for x in codes if x not in (0, None, -2))
            elif -14 in codes:
                outcome, kind, code = "auth_required", "bot_auth_expired", -14
            elif -2 in codes:
                code = -2
                error = str(result.get("errmsg") or result.get("msg") or "").lower()
                if error in ("prepare failed", "unknown error"):
                    outcome, kind = ("context_required", "fresh_inbound_required") if row["attempts"] + 1 < 3 else ("rejected", "retry_limit")
                elif error in ("rate limit", "rate limited"):
                    outcome, kind = ("queued", "rate_limited") if row["attempts"] + 1 < 3 else ("rejected", "retry_limit")
                else:
                    # Project retry policy is deliberately narrower than all
                    # possible upstream -2 causes. Unknown text grants no retry.
                    outcome, kind = "rejected", "send_rejected"
            elif any(x not in (0, None) for x in codes):
                outcome, kind = "rejected", "send_rejected"
                code = next((x for x in codes if type(x) is int and x != 0), None)
            elif "base_resp" in result:
                kind = "unexpected_send_response_shape"
            else:
                outcome, kind = "accepted", None
                diagnostic["acceptance_basis"] = "explicit_zero" if 0 in codes else "omitted_success_codes"
                response_server_id = server_message_id(result.get('message_id'))
        except WeixinError as error:
            known = {"api_timeout", "api_network_error", "api_http_error", "invalid_api_json", "invalid_api_response",
                     "api_response_too_large", "invalid_api_destination", "non_public_api_address", "invalid_api_endpoint"}
            kind = error.kind if error.kind in known else "unexpected_send_error"
            diagnostic = {"response_received": False, "failure_kind": kind}
            if error.kind == "api_http_error" and type(error.code) is int:
                # An HTTP response is evidence, not proof of message acceptance.
                # Keep delivery_unknown and the existing no-resend behavior.
                diagnostic["response_received"] = True
                diagnostic["http_status"] = error.code
        except Exception:
            # Timeout/network errors after POST may have delivered; never blindly resend.
            kind = "unexpected_send_error"
            diagnostic = {"response_received": False, "failure_kind": kind}
        if row['ack_origin'] == 'system_receipt' and outcome in ('queued','context_required','auth_required'):
            outcome, kind = 'rejected', 'system_receipt_post_rejected'
        with self.lock, self.db:
            if outcome == "context_required":
                latest = self.db.execute("SELECT received_at FROM weixin_peers WHERE account=? AND peer=?", (account["id"], row["peer"])).fetchone()
                if latest and latest["received_at"] > context["received_at"] and row["attempts"] + 1 < 3:
                    outcome, kind = "queued", "fresh_context_available"
            self.db.execute("UPDATE weixin_outbox SET status=?,error_kind=?,server_code=?,accepted_at=?,next_at=?,api_result=? WHERE message_id=?",
                            (outcome, kind, code, self.clock() if outcome == "accepted" else None, now + 5 * (row["attempts"] + 1), encoded(diagnostic), row["message_id"]))
            if outcome == 'accepted' and response_server_id is not None:
                self.db.execute('INSERT OR IGNORE INTO weixin_server_message_ids VALUES(?,?,?,?,?)',
                                (account['id'], row['peer'], 'outgoing', row['message_id'], response_server_id))
            if outcome == "auth_required" or code == -14:
                self.db.execute("UPDATE weixin_accounts SET status='auth_required',last_error='bot_auth_expired' WHERE id=?", (account["id"],))
            if row['upload_id'] and outcome in ('accepted','rejected','delivery_unknown'):
                self.db.execute('UPDATE weixin_outbox SET item_json=NULL WHERE message_id=?', (row['message_id'],))
        return True

    def poll_once(self):
        account = self.account()
        if not account or account["status"] != "ready" or self.clock() < self.poll_after:
            return False
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
