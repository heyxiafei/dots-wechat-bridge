"""Single-writer outbox dispatch, including preparation and uncertain outcomes.

The dispatcher borrows the existing WeixinStore at each operation. It creates no
connection, account, worker or parallel queue, and reads its client/hold policy
live so runtime guards and injected transports remain authoritative. Queue slot
commitments and read-only presentation continue to belong to WeixinStore.
"""
from contextlib import nullcontext
import json

try:
    from .weixin_protocol import WeixinError, encoded
    from .quotes import server_message_id
except ImportError:
    from weixin_protocol import WeixinError, encoded
    from quotes import server_message_id


class OutboundDispatcher:
    def __init__(self, store):
        self.store = store

    def pending_receipt(self, fresh):
        if not self.store.probe.receipts.pending_receipt():
            return False
        if fresh is None:
            return True
        # Historical receipt intents must not stall a newly admitted attachment.
        waiting = self.store.db.execute("SELECT message_id FROM owner_receipt_intents WHERE activation_id=? AND state='waiting' AND decision NOT IN ('queue_full','uncertain_source')",
                                  (self.store.probe.receipts.activation_id,))
        return any(fresh.allows_source(row['message_id']) for row in waiting)


    def send_once(self):
        store = self.store
        account = store.account()
        if not account or account["status"] != "ready":
            return False
        fresh = getattr(store.probe, 'fresh_admission', None)
        if fresh is not None:
            fresh.require_ready()
        now = store.clock()
        with store.lock, store.db:
            # Waiting finals do not monopolize the queue. Only their own ack
            # blocks them, including its bounded backoff/context/auth wait.
            # An uncertain ack is never resent; the independent final may proceed.
            rows = store.db.execute("""SELECT outgoing.* FROM weixin_outbox outgoing
                WHERE outgoing.status='queued' AND outgoing.next_at<=? AND outgoing.account=?
                AND NOT (outgoing.kind='reply' AND EXISTS (
                    SELECT 1 FROM weixin_outbox ack WHERE ack.source_message_id=outgoing.message_id
                    AND ack.kind='reply_ack' AND ack.status IN ('queued','preparing','sending','context_required','auth_required')))
                ORDER BY CASE
                    WHEN outgoing.kind='reply_ack' AND outgoing.ack_origin='system_receipt' THEN 0
                    WHEN outgoing.kind IN ('reply','reply_ack') AND outgoing.upload_id IS NULL THEN 1
                    ELSE 2 END,outgoing.created_at,outgoing.rowid""", (now, account["id"]))
            row = next((item for item in rows
                        if (fresh is None or item['kind'] != 'notification' and fresh.allows_source(item['source_message_id']))
                        and (item['kind'] != 'notification' or
                             not store.aihot_hold_all_notifications and item['message_id'] not in store.aihot_held_notification_ids)), None)
            if not row:
                return False
            if fresh is not None:
                fresh.require_source(row['source_message_id'])
            if row['attempts'] >= 3:
                store.db.execute("UPDATE weixin_outbox SET status='rejected',error_kind='retry_limit' WHERE message_id=?", (row['message_id'],))
                return True
            if row['peer'] != account['owner']:
                store.db.execute("UPDATE weixin_outbox SET status='rejected',error_kind='owner_identity_mismatch' WHERE message_id=?", (row['message_id'],))
                return True
            context = store.db.execute("SELECT token,received_at FROM weixin_peers WHERE account=? AND peer=?", (account["id"], row["peer"])).fetchone()
            if not context:
                store.db.execute("UPDATE weixin_outbox SET status='context_required',error_kind='fresh_inbound_required' WHERE message_id=?", (row["message_id"],))
                return True
            if fresh is not None:
                fresh.require_context(context)
            if row['upload_id'] and not row['item_json'] and store._pending_receipt(fresh):
                return False
            if row['upload_id'] and not row['item_json']:
                store.db.execute("UPDATE weixin_outbox SET status='preparing' WHERE message_id=?", (row['message_id'],))
        if row['upload_id'] and not row['item_json']:
            try:
                item = store.probe.outbound.prepare(row['upload_id'], account)
                with store.lock, store.db:
                    store.db.execute("UPDATE weixin_outbox SET item_json=?,status='queued',error_kind=NULL WHERE message_id=?", (encoded(item), row['message_id']))
            except Exception as exc:
                kind = exc.kind if isinstance(exc, WeixinError) else 'media_upload_error'
                with store.lock, store.db:
                    upload = store.db.execute('SELECT upload_attempts FROM weixin_uploads WHERE id=?', (row['upload_id'],)).fetchone()
                    retry = upload['upload_attempts'] < 3 and (kind in ('media_upload_timeout','media_upload_network_error','api_timeout','api_network_error') or
                        (kind in ('media_upload_http_error','api_http_error') and isinstance(exc,WeixinError) and type(exc.code) is int and exc.code >= 500))
                    state = 'auth_required' if kind == 'auth_required' else 'queued' if retry else 'rejected'
                    store.db.execute("UPDATE weixin_outbox SET status=?,error_kind=?,next_at=? WHERE message_id=?", (state,kind,store.clock()+5*upload['upload_attempts'],row['message_id']))
                    if state == 'auth_required':
                        store.db.execute("UPDATE weixin_accounts SET status='auth_required',last_error='bot_auth_expired' WHERE id=?", (account['id'],))
                return True
        # CDN preparation may take time. Re-read identity, expiry and the latest
        # peer context immediately before committing a message POST.
        account = store.account()
        with store.lock, store.db:
            store.db.execute('BEGIN IMMEDIATE')
            row = store.db.execute('SELECT * FROM weixin_outbox WHERE message_id=?', (row['message_id'],)).fetchone()
            if not account or account['status'] != 'ready' or row['status'] != 'queued':
                return True
            if fresh is not None:
                fresh.require_source(row['source_message_id'])
                if row['account'] != account['id'] or row['peer'] != account['owner']:
                    raise WeixinError('fresh_source_owner_mismatch')
            if row['kind'] == 'notification' and (store.aihot_hold_all_notifications or row['message_id'] in store.aihot_held_notification_ids):
                return False
            if row['upload_id']:
                try:
                    store.probe.outbound.authorized(row['upload_id'])
                    store.read_message(row['source_message_id'])
                except WeixinError as exc:
                    store.db.execute("UPDATE weixin_outbox SET status='rejected',error_kind=?,item_json=NULL WHERE message_id=?", (exc.kind,row['message_id']))
                    return True
            context = store.db.execute("SELECT token,received_at FROM weixin_peers WHERE account=? AND peer=?", (account['id'],row['peer'])).fetchone()
            if not context:
                store.db.execute("UPDATE weixin_outbox SET status='context_required',error_kind='fresh_inbound_required' WHERE message_id=?", (row['message_id'],))
                return True
            if fresh is not None:
                fresh.require_context(context)
            changed = store.db.execute("UPDATE weixin_outbox SET status='sending',attempts=attempts+1,context_at=?,post_started_at=? WHERE message_id=? AND status='queued' AND attempts=?",
                                      (context["received_at"], store.clock(), row["message_id"], row['attempts'])).rowcount
            if changed != 1:
                return True
        outcome, kind, code = "delivery_unknown", "uncertain_api_result", None
        diagnostic = {"response_received": False}
        response_server_id = None
        post_attempted = False
        try:
            with store.lock if fresh is not None else nullcontext():
                if fresh is not None:
                    if fresh is not getattr(store.probe, 'fresh_admission', None):
                        raise WeixinError('fresh_policy_changed_before_post')
                    fresh.require_source(row['source_message_id'])
                    fresh.require_context(context)
                post_attempted = True
                if row['upload_id']:
                    result = store.client.send_items(account, row['peer'], [json.loads(row['item_json'])], row['client_id'], context['token'])
                else:
                    result = store.client.send(account, row["peer"], row["text"], row["client_id"], context["token"])
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
            if not post_attempted and fresh is not None:
                # The committed slot remains terminal: no API invocation took
                # place, and a later policy change must not replay this slot.
                outcome, kind = 'rejected', 'fresh_prepost_gate_failed'
                diagnostic = {"response_received": False, "post_attempted": False,
                              "failure_kind": kind}
            if error.kind == "api_http_error" and type(error.code) is int:
                # An HTTP response is evidence, not proof of message acceptance.
                # Keep delivery_unknown and the existing no-resend behavior.
                diagnostic["response_received"] = True
                diagnostic["http_status"] = error.code
        except Exception:
            # Timeout/network errors after POST may have delivered; never blindly resend.
            kind = "unexpected_send_error"
            diagnostic = {"response_received": False, "failure_kind": kind}
            if not post_attempted and fresh is not None:
                outcome, kind = 'rejected', 'fresh_prepost_gate_failed'
                diagnostic = {"response_received": False, "post_attempted": False,
                              "failure_kind": kind}
        if fresh is not None and outcome in ('queued', 'context_required', 'auth_required'):
            # This mode has no retained history to authorize replay of a
            # refused commitment. Keep its original slot terminal even when
            # the provider suggests a recoverable auth/context/rate condition.
            outcome, kind = 'rejected', 'fresh_send_rejected'
        if row['ack_origin'] == 'system_receipt' and outcome in ('queued','context_required','auth_required'):
            outcome, kind = 'rejected', 'system_receipt_post_rejected'
        with store.lock, store.db:
            if fresh is not None and not post_attempted:
                store.db.execute('UPDATE weixin_outbox SET attempts=?,post_started_at=NULL WHERE message_id=?',
                                (row['attempts'], row['message_id']))
            if outcome == "context_required":
                latest = store.db.execute("SELECT received_at FROM weixin_peers WHERE account=? AND peer=?", (account["id"], row["peer"])).fetchone()
                if latest and latest["received_at"] > context["received_at"] and row["attempts"] + 1 < 3:
                    outcome, kind = "queued", "fresh_context_available"
            store.db.execute("UPDATE weixin_outbox SET status=?,error_kind=?,server_code=?,accepted_at=?,next_at=?,api_result=? WHERE message_id=?",
                            (outcome, kind, code, store.clock() if outcome == "accepted" else None, now + 5 * (row["attempts"] + 1), encoded(diagnostic), row["message_id"]))
            if outcome == 'accepted' and response_server_id is not None:
                store.db.execute('INSERT OR IGNORE INTO weixin_server_message_ids VALUES(?,?,?,?,?)',
                                (account['id'], row['peer'], 'outgoing', row['message_id'], response_server_id))
            if outcome == "auth_required" or code == -14:
                store.db.execute("UPDATE weixin_accounts SET status='auth_required',last_error='bot_auth_expired' WHERE id=?", (account["id"],))
            if row['upload_id'] and outcome in ('accepted','rejected','delivery_unknown'):
                store.db.execute('UPDATE weixin_outbox SET item_json=NULL WHERE message_id=?', (row['message_id'],))
        return True

