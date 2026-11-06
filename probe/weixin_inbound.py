"""Owner-admitted inbound capture and its atomic inbox/cursor transaction.

Capture is bounded and first-write wins. Quotes and fixed-command diagnostics are
stored as data, never execution authority. This component shares the existing
Probe connection and lock; it neither creates a reader worker nor sends replies.
"""
import hashlib
import re

try:
    from .weixin_protocol import WeixinError, encoded
    from .quotes import capture_quotes, server_message_id, quote_shape
    from .fixed_commands import inspect_command_context, unknown_command_item_diagnostic
except ImportError:
    from weixin_protocol import WeixinError, encoded
    from quotes import capture_quotes, server_message_id, quote_shape
    from fixed_commands import inspect_command_context, unknown_command_item_diagnostic

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


class InboundCapture:
    """Persist each accepted batch through the original single database owner."""

    def __init__(self, probe, *, clock, notify_sender):
        self.probe = probe
        self.db, self.lock = probe.db, probe.lock
        # These callbacks resolve the facade's current clock and wake hook.
        # No sender or extra queue is constructed by the capture component.
        self.clock, self.notify_sender = clock, notify_sender

    def ingest(self, account, response, *, describe=describe_inbound):
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
            fresh = getattr(self.probe, 'fresh_admission', None)
            # Validate the entire raw batch before parsing any body or media.
            # The normal historical timestamp fallback is not admission proof.
            admissions = fresh.inspect_batch(account, msgs, now) if fresh is not None else None
            for position, msg in enumerate(msgs):
                admission = admissions[position] if admissions is not None else None
                if fresh is not None and admission is None:
                    continue
                if (not isinstance(msg, dict) or msg.get("from_user_id") != account["owner"]
                        or msg.get("group_id") or msg.get("room_id") or msg.get("message_type") != 1
                        or msg.get("message_state") == 1 or msg.get("delete_time_ms")
                        or msg.get("to_user_id") not in (None, "", account["id"])):
                    continue
                source = admission['source_id'] if admission is not None else msg.get("message_id") or msg.get("client_id")
                if not isinstance(source, (str, int)) or isinstance(source, bool) or not source:
                    continue
                identity = (admission['identity'] if admission is not None else
                            "wx_" + hashlib.sha256(encoded([account["id"], account["owner"], str(source)]).encode()).hexdigest()[:32])
                created_ms = admission['source_ms'] if admission is not None else msg.get("create_time_ms")
                if fresh is not None:
                    fresh.validate_candidate_in_transaction(identity, str(source), created_ms)
                items = msg.get("item_list", [])
                content, has_media, manifest = describe(identity, items)
                command_decision = inspect_command_context(msg)
                (manifest['command_context_v1'],
                 manifest['command_context_reason_v1']) = command_decision
                unsupported = manifest['issue_codes']
                if admission is None:
                    created_ms = created_ms if isinstance(created_ms, (float, int)) and 0 < created_ms <= (now + 300) * 1000 else now * 1000
                changed = self.db.execute("""INSERT OR IGNORE INTO weixin_inbox
                    (id,account,peer,source_id,text,created_at,kind,unsupported,event_id)
                    VALUES(?,?,?,?,?,?,?,?,NULL)""",
                    (identity, account["id"], account["owner"], str(source), content, created_ms / 1000,
                     "mixed" if content and has_media else "media" if has_media else "text" if content else "unsupported", encoded(unsupported))).rowcount
                if not changed:
                    continue
                if fresh is not None:
                    fresh.admit_in_transaction(identity, str(source), created_ms)
                # Diagnose only a newly inserted, owner-admitted command.
                # Duplicate/history paths never compute or backfill this field.
                command_diagnostic = unknown_command_item_diagnostic(msg, content, manifest, command_decision)
                if command_diagnostic is not None:
                    manifest['command_item_diagnostic_v1'] = command_diagnostic
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
                        if fresh is not None:
                            fresh.record_context_in_transaction(identity, context, created_ms)
                        if fresh is None:
                            self.db.execute("UPDATE weixin_outbox SET status='queued',next_at=0 WHERE account=? AND peer=? AND status='context_required' AND attempts<3",
                                            (account["id"], account["owner"]))
                        else:
                            held = self.db.execute("SELECT message_id,source_message_id FROM weixin_outbox WHERE account=? AND peer=? AND status='context_required' AND attempts<3",
                                                   (account['id'], account['owner'])).fetchall()
                            for outgoing in held:
                                if fresh.allows_source(outgoing['source_message_id']):
                                    self.db.execute("UPDATE weixin_outbox SET status='queued',next_at=0 WHERE message_id=? AND status='context_required' AND attempts<3",
                                                    (outgoing['message_id'],))
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

