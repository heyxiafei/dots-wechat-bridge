"""Shared MCP wire primitives; this module performs no I/O or store construction."""
import json

VERSION = "2026-07-28"
BUILD_VERSION = '0.6.0+refactor1'
MAX_ATTACHMENT_RPC_BYTES = 8 * 1024 * 1024
EVENT_NAME = "test.ping"
RPC_METHODS = {"server/discover", "initialize", "ping", "events/list", "events/subscribe",
               "events/unsubscribe", "tools/list", "tools/call", "notifications/initialized",
               "notifications/cancelled"}

class RpcError(Exception):
    def __init__(self, code, message, reason=None):
        super().__init__(message)
        self.code, self.reason = code, reason



def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

