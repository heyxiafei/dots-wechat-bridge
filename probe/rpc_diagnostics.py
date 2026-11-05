"""Bounded RPC metadata projections; never retain message bodies or callback secrets."""
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

try:
    from .rpc_protocol import RPC_METHODS, canonical
except ImportError:
    from rpc_protocol import RPC_METHODS, canonical

def subscription_diagnostic(request, response=None):
    """Fixed, secret-free diagnostics for subscription setup; never log RPC data."""
    if not isinstance(request, dict) or request.get("method") not in ("events/subscribe", "events/unsubscribe"):
        return None
    record = {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "method": request["method"], "stage": "received" if response is None else "completed"}
    if request["method"] == "events/subscribe":
        params = request.get("params", {})
        if not isinstance(params, dict):
            kind = "invalid"
        elif "ttlMs" not in params:
            kind = "omitted"
        elif params["ttlMs"] is None:
            kind = "null"
        else:
            ttl = params["ttlMs"]
            kind = "finite" if type(ttl) is int and ttl > 0 else "invalid"
        record["ttl_kind"] = kind
    if response is None:
        try:
            host = urlsplit(request.get("params", {}).get("delivery", {}).get("url", "")).hostname
            if host and len(host) <= 253 and re.fullmatch(r"[a-zA-Z0-9.-]+", host):
                record["callback_host"] = host
        except (AttributeError, TypeError, ValueError):
            pass
    else:
        error = response.get("error", {})
        record["success"] = "error" not in response
        if record["success"] and request["method"] == "events/subscribe":
            result = response.get("result")
            record["non_expiring_granted"] = (isinstance(result, dict) and
                "refreshBefore" in result and result["refreshBefore"] is None)
        if error.get("code") in (-32600, -32602, -32603, -32015):
            record["error_code"] = error["code"]
        reason = error.get("data", {}).get("reason")
        if reason in ("invalid_destination", "challenge_failed", "timeout", "response_too_large"):
            record["reason"] = reason
    return canonical(record)



def record_rpc_diagnostic(directory, request, response=None, completed=False):
    """Persist only fixed metadata; tunnel supervisors may discard child stderr."""
    if not isinstance(request, dict) or request.get("method") not in RPC_METHODS:
        return
    text = subscription_diagnostic(request, response) if response is not None or not completed else None
    record = json.loads(text) if text else {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "method": request["method"]}
    record["stage"] = "completed" if completed else "received"
    if completed and isinstance(response, dict):
        record["success"] = "error" not in response
        error = response.get("error", {})
        kinds = {"Only test.ping with empty arguments is supported": "invalid_event_arguments",
                 "Webhook delivery required": "webhook_delivery_required",
                 "Invalid subscription signing secret": "invalid_signing_secret",
                 "ttlMs must be positive": "invalid_expiration",
                 "Stop the existing subscription before choosing another chat": "existing_subscription",
                 "Callback endpoint rejected": "invalid_destination",
                 "Callback verification failed": "callback_verification_failed"}
        if error.get("message") in kinds:
            record["error_kind"] = kinds[error["message"]]
    fd = os.open(Path(directory) / "rpc-diagnostics.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (canonical(record) + "\n").encode())
    finally:
        os.close(fd)



def read_rpc_diagnostics(directory):
    """Return bounded, projected diagnostics, never raw file content."""
    records = []
    try:
        with open(Path(directory) / "rpc-diagnostics.jsonl", "rb") as source:
            source.seek(0, os.SEEK_END)
            source.seek(max(source.tell() - 65536, 0))
            lines = source.read(65536).decode(errors="replace").splitlines()
    except OSError:
        return records
    for line in lines:
        try:
            item = json.loads(line)
            if not isinstance(item, dict) or item.get("method") not in RPC_METHODS:
                continue
            safe = {"method": item["method"]}
            if isinstance(item.get("utc"), str) and re.fullmatch(r"[0-9TZ:.-]{20,35}", item["utc"]):
                safe["utc"] = item["utc"]
            if item.get("stage") in ("received", "completed"):
                safe["stage"] = item["stage"]
            if type(item.get("success")) is bool:
                safe["success"] = item["success"]
            if item.get("ttl_kind") in ("omitted", "null", "finite", "invalid"):
                safe["ttl_kind"] = item["ttl_kind"]
            if type(item.get("non_expiring_granted")) is bool:
                safe["non_expiring_granted"] = item["non_expiring_granted"]
            if item.get("error_code") in (-32600, -32602, -32603, -32015):
                safe["error_code"] = item["error_code"]
            if item.get("reason") in ("invalid_destination", "challenge_failed", "timeout", "response_too_large"):
                safe["reason"] = item["reason"]
            if item.get("error_kind") in ("invalid_event_arguments", "webhook_delivery_required", "invalid_signing_secret",
                                          "invalid_expiration", "existing_subscription", "invalid_destination", "callback_verification_failed"):
                safe["error_kind"] = item["error_kind"]
            host = item.get("callback_host")
            if isinstance(host, str) and len(host) <= 253 and re.fullmatch(r"[a-zA-Z0-9.-]+", host):
                safe["callback_host"] = host
            records.append(safe)
        except (ValueError, TypeError):
            continue
    return records[-25:]

