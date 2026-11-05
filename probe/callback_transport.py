"""Signed webhook transport with an exact host allowlist and pinned public DNS.

Callback authorization, signing and HTTPS are independent of stores and workers.
Every resolved address must pass policy before any connection is opened.
"""
import base64
import hmac
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

try:
    from .rpc_protocol import RpcError
except ImportError:
    from rpc_protocol import RpcError

def key_bytes(secret):
    try:
        if not isinstance(secret, str) or not secret.startswith("whsec_"):
            raise ValueError()
        encoded = secret[6:]
        key = base64.b64decode(encoded, validate=True)
        if not 24 <= len(key) <= 64 or base64.b64encode(key).decode() != encoded:
            raise ValueError()
        return key
    except (ValueError, TypeError):
        raise RpcError(-32602, "Invalid subscription signing secret") from None



def signature(secret, event_id, timestamp, body):
    content = event_id.encode() + b"." + str(timestamp).encode() + b"." + body
    return "v1," + base64.b64encode(hmac.digest(key_bytes(secret), content, "sha256")).decode()



def headers(sub, event_id, body, now):
    timestamp = int(now)
    signs = [signature(sub["secret"], event_id, timestamp, body)]
    if sub.get("old_secret") and sub.get("rotate_until", 0) > now:
        signs.append(signature(sub["old_secret"], event_id, timestamp, body))
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": str(timestamp), "webhook-signature": " ".join(signs),
            "X-MCP-Subscription-Id": sub["id"]}



def callback_parts(url, allowed_hosts):
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (parts.scheme != "https" or not host or host not in allowed_hosts
                or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            raise ValueError()
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError()
        return parts
    except (ValueError, TypeError):
        raise RpcError(-32015, "Callback endpoint rejected", "invalid_destination") from None



def public_address(value):
    ip = ipaddress.ip_address(value)
    if not ip.is_global or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False
    # Prevent transition/mapped addresses from hiding a non-public IPv4 target.
    if ip.version == 6 and (ip.ipv4_mapped or ip.sixtofour or ip.teredo):
        return False
    return True



class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, hostname, address):
        super().__init__(hostname, timeout=10, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), timeout=self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise



def post_https(url, body, request_headers, allowed_hosts, *, connection_factory=None):
    """Validate the entire DNS answer set before constructing a pinned connection."""
    parts = callback_parts(url, allowed_hosts)
    if len(body) > 262144:
        raise RpcError(-32602, "Event exceeds maximum size")
    answers = socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or not all(public_address(ip) for ip in addresses):
        raise RpcError(-32015, "Callback endpoint rejected", "invalid_destination")
    factory = PinnedHTTPS if connection_factory is None else connection_factory
    connection = factory(parts.hostname, addresses[0])
    try:
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        connection.request("POST", path, body=body, headers=request_headers)
        response = connection.getresponse()
        data = response.read(262145)
        if len(data) > 262144:
            raise RpcError(-32015, "Callback response rejected", "response_too_large")
        return response.status, data
    finally:
        connection.close()

