"""Bounded iLink HTTP protocol and shared transport primitives.

This module owns endpoint validation, TLS, bounded API responses and fixed error
categories. It opens no connection until an explicit client method is called.
The public names remain re-exported by ``weixin`` for existing integrations.
"""
import base64
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
from urllib.parse import urlencode, urlsplit

BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.9"
MESSAGE_EVENT = "weixin.message"


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
