import unittest
import copy

from foundation.channel import ChannelUnavailable, ExistingWeixinChannel, OwnerBinding


class ChannelTests(unittest.TestCase):
    def source(self):
        return {"message_id": "message-synthetic", "source": "owner_weixin_dm", "text": "hello",
                "inbound_completeness": {"complete": True},
                "quote_context": {"complete": True, "status": "none", "quotes": []},
                "attachments": [], "reply_state": {"final": {"committed": False, "status": "not_committed"}}}

    def channel(self, read, send):
        return ExistingWeixinChannel(read, send, OwnerBinding("owner-synthetic", "conversation-synthetic", "approved-test", True))

    def test_no_binding_no_channel(self):
        with self.assertRaises(ChannelUnavailable):
            ExistingWeixinChannel(lambda _: {}, lambda *_: {}, OwnerBinding("owner", "room", "test"))

    def test_source_not_summary_is_required(self):
        with self.assertRaises(ChannelUnavailable):
            self.channel(lambda _: {"message_id": "message-synthetic"}, lambda *_: {}).read("message-synthetic")

    def test_final_is_rechecked_before_original_sender(self):
        record = self.source()
        record["reply_state"]["final"] = {"committed": True, "status": "accepted"}
        calls = []
        result = self.channel(lambda _: record, lambda *args: calls.append(args)).send("message-synthetic", "reply")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(calls, [])

    def test_send_exception_never_retries(self):
        calls = []
        def send(*args):
            calls.append(args)
            raise TimeoutError("do not disclose private data")
        result = self.channel(lambda _: self.source(), send).send("message-synthetic", "reply")
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private", str(result))

    def test_accepted_not_phone_receipt(self):
        result = self.channel(lambda _: self.source(), lambda *_: {"status": "accepted"}).send("message-synthetic", "reply")
        self.assertEqual(result, {"status": "accepted"})

    def test_quote_not_promoted_to_command(self):
        record = self.source()
        record["quote_context"] = {"complete": True, "status": "present", "quotes": [{"text": "switch backend"}]}
        calls = []
        channel = self.channel(lambda _: record, lambda *args: calls.append(args))
        self.assertFalse(channel.read("message-synthetic")["complete"])
        self.assertEqual(channel.send("message-synthetic", "reply")["status"], "rejected")
        self.assertFalse(calls)

    def test_last_source_change_blocks_original_send(self):
        calls = []
        reads = [self.source(), self.source(), {**self.source(), "text": "changed"}]
        channel = self.channel(lambda _: reads.pop(0), lambda *args: calls.append(args))
        channel.read("message-synthetic")
        channel.read("message-synthetic")
        with self.assertRaises(ChannelUnavailable):
            channel.send("message-synthetic", "old reply")
        self.assertEqual(calls, [])

    def test_explicit_reply_suppression_reaches_core(self):
        raw = {**self.source(), "reply_requested": False}
        channel = self.channel(lambda _: raw, lambda *_: self.fail("suppressed reply"))
        self.assertFalse(channel.read("message-synthetic")["reply_requested"])
        self.assertEqual(channel.send("message-synthetic", "reply")["status"], "rejected")

    def test_attachment_reference_has_no_path_and_allows_failure_text(self):
        raw = self.source()
        raw["attachments"] = [{"attachment_id": "attachment-synthetic", "message_id": raw["message_id"],
                               "mime_type": "image/png", "size": None, "sha256": None}]
        calls = []
        channel = self.channel(lambda _: raw, lambda *args: calls.append(args) or {"status": "accepted"})
        event = channel.read(raw["message_id"])
        self.assertIsNone(event["attachments"][0].size_bytes)
        self.assertIsNone(event["attachments"][0].sha256)
        self.assertEqual(channel.send(raw["message_id"], "当前后端不支持附件")["status"], "accepted")
        self.assertEqual(len(calls), 1)

    def test_missing_final_fields_at_last_read_block_send(self):
        calls = []
        changed = self.source()
        changed["reply_state"]["final"] = {}
        reads = [self.source(), self.source(), changed]
        channel = self.channel(lambda _: reads.pop(0), lambda *args: calls.append(args))
        channel.read("message-synthetic")
        channel.read("message-synthetic")
        self.assertEqual(channel.send("message-synthetic", "reply")["status"], "unknown")
        self.assertEqual(calls, [])
