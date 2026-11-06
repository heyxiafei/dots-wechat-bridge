"""Single offline operator entry. This CLI never connects to WeChat or models."""
import argparse
import json
from pathlib import Path
import sqlite3

from .config import load_config
from .core import BridgeCore
from .echo import EchoBackend
from .store import FoundationStore


class _DemoChannel:
    def __init__(self, message_id, text):
        self.message_id, self.text, self.replies = message_id, text, []

    def read(self, message_id):
        if message_id != self.message_id:
            raise ValueError("unknown_demo_message")
        return {"message_id": message_id, "text": self.text, "source": "owner_weixin_dm",
                "owner_id": "synthetic-owner", "owner_verified": True,
                "conversation_id": "synthetic-conversation", "complete": True,
                "quotes": [], "attachments": [],
                "reply_state": {"final": {"committed": bool(self.replies),
                    "status": "accepted" if self.replies else "not_committed"}}}

    def send(self, message_id, text):
        self.replies.append(text)
        return {"status": "accepted"}


def readonly_status(path):
    """Do not construct the writer or perform recovery while viewing status."""
    return {"mode": "read_only", **FoundationStore.read_summary(path)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Validate configuration; no credentials or network")
    doctor.add_argument("--config", required=True)
    demo = commands.add_parser("demo", help="Persistent synthetic echo; no real WeChat/model calls")
    demo.add_argument("--state", required=True, help="A new isolated demo database path")
    demo.add_argument("--text", default="你好")
    demo.add_argument("--message-id", default="synthetic-demo-message")
    status = commands.add_parser("status", help="View only task metadata without writer recovery")
    status.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            default, backends = load_config(args.config)
            result = {"default_backend": default, "production_connected": False,
                      "default_backend_enabled": default in backends,
                      "credentials_read": False, "network_checked": False,
                      "backends": {k: dict(v.health()) for k,v in backends.items()}}
        elif args.command == "status":
            result = readonly_status(args.state)
        else:
            channel = _DemoChannel(args.message_id, args.text)
            with FoundationStore(args.state) as store:
                core = BridgeCore(store, {"echo": EchoBackend()}, channel, "synthetic-owner")
                core.set_default("echo")
                outcome = core.handle(args.message_id)
                result = {"mode": "simulation", "official_reads": 0, "wechat_sends": 0,
                          "outcome": outcome, "simulated_replies_this_run": channel.replies}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (ValueError, TypeError, OSError, RuntimeError, sqlite3.Error):
        # Local config and SQL errors can contain secrets/paths; fixed diagnostic.
        print(json.dumps({"ok": False, "reason": "configuration_or_state_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
