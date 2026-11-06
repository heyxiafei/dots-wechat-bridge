"""Use the existing owner-scoped reader/final sender; never create consumers.

The hosting coordinator supplies actual trusted callables and binding evidence.
This adapter does not log in, poll, read credentials, install services or send on
import. It is not automatically connected to the current production instance.
"""
from dataclasses import dataclass
import hashlib
import json
import threading

from .contracts import AttachmentRef


class ChannelUnavailable(Exception):
    pass


@dataclass(frozen=True)
class OwnerBinding:
    owner_id: str
    conversation_id: str
    evidence_ref: str
    verified: bool = False


class ExistingWeixinChannel:
    """Adapt unpacked existing MCP read/reply contracts, not arbitrary chat JSON."""

    def __init__(self, read_message, send_final, binding):
        if (not isinstance(binding, OwnerBinding) or binding.verified is not True
                or not all(isinstance(v, str) and v for v in
                           (binding.owner_id, binding.conversation_id, binding.evidence_ref))
                or not callable(read_message) or not callable(send_final)):
            raise ChannelUnavailable("trusted_owner_channel_unavailable")
        self._read = read_message
        self._send = send_final
        self._binding = binding
        self._sources = {}
        self._lock = threading.RLock()

    def read(self, message_id):
        try:
            raw = self._read(message_id)
        except Exception:
            raise ChannelUnavailable("original_read_failed") from None
        if (not isinstance(raw, dict) or raw.get("message_id") != message_id
                or raw.get("source") != "owner_weixin_dm" or not isinstance(raw.get("text"), str)
                or not isinstance(raw.get("reply_state"), dict)):
            raise ChannelUnavailable("original_source_unverified")
        capture = raw.get("inbound_completeness", {})
        quotes = raw.get("quote_context", {})
        complete = (isinstance(capture, dict) and capture.get("complete") is True
                    and isinstance(quotes, dict) and quotes.get("complete") is True
                    and quotes.get("status") == "none" and quotes.get("quotes") == [])
        attachments = raw.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 8:
            raise ChannelUnavailable("original_attachments_invalid")
        refs = []
        for item in attachments:
            if (not isinstance(item, dict) or item.get("message_id") != message_id
                    or not isinstance(item.get("attachment_id"), str) or not item["attachment_id"]):
                raise ChannelUnavailable("original_attachments_invalid")
            refs.append(AttachmentRef(item["attachment_id"], item.get("mime_type") or "application/octet-stream",
                                      item.get("size"), item.get("sha256")))
        requested = raw.get("reply_requested", True)
        if type(requested) is not bool:
            raise ChannelUnavailable("reply_intent_unverified")
        # Keep only the first source fingerprint. Final slot changes are checked
        # separately; changing original content cannot replace this snapshot.
        digest = hashlib.sha256(json.dumps([raw["text"], [x.attachment_id for x in refs],
            quotes.get("status"), quotes.get("quotes")], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with self._lock:
            first = self._sources.setdefault(message_id, digest)
            if first != digest:
                raise ChannelUnavailable("original_source_changed")
        return {"message_id": message_id, "source": "owner_weixin_dm", "text": raw["text"],
                "owner_id": self._binding.owner_id, "owner_verified": True,
                "conversation_id": self._binding.conversation_id,
                "complete": complete, "quotes": quotes.get("quotes", [None]),
                "attachments": refs, "reply_state": raw["reply_state"], "reply_requested": requested,
                "fixed_command": raw.get("fixed_command")}

    def send(self, message_id, text):
        # Reuse the exact original message/phase idempotency; no notification or
        # mirror APIs, alternative send IDs, retries, or backend-owned senders.
        latest = self.read(message_id)
        final = latest["reply_state"].get("final", {})
        if (not isinstance(final, dict) or final.get("committed") is not False
                or final.get("status") != "not_committed"
                or latest["reply_state"].get("next_phase") == "none"):
            return {"status": "unknown", "reason": "original_final_not_free"}
        if not latest["complete"] or latest["reply_requested"] is not True:
            return {"status": "rejected", "reason": "unsupported_source_shape"}
        try:
            result = self._send(message_id, text)
        except Exception:
            return {"status": "unknown", "reason": "original_send_result_unknown"}
        if not isinstance(result, dict):
            return {"status": "unknown", "reason": "invalid_original_send_receipt"}
        state = result.get("status")
        # Accepted is upstream acceptance only, never phone delivery.
        if state not in ("accepted", "queued", "rejected", "unknown"):
            return {"status": "unknown", "reason": "unsupported_original_send_state"}
        return {"status": state}
