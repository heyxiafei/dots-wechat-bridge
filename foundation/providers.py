"""Text-only vendor adapters. Construction/health never read keys or use network.

This optional module is not a tool-executing agent. It has no WeChat sender,
credential files, retries, provider fallback, streaming, or attachment uploads.
"""
import json
import math
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Mapping

from .contracts import BackendCapabilities, BackendResult, UnsupportedOperations


ENDPOINTS = {
    "openai": "https://api.openai.com/v1/responses",
    "anthropic": "https://api.anthropic.com/v1/messages",
    "kimi": "https://api.moonshot.cn/v1/chat/completions",
    "deepseek": "https://api.deepseek.com/chat/completions",
    "ark": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
}
CREDENTIAL_ENV = {
    "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "kimi": "MOONSHOT_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "ark": "ARK_API_KEY",
}
MAX_RESPONSE_BYTES = 1048576
TRANSPORT_CODES = frozenset({"endpoint_not_allowed", "invalid_timeout", "request_too_large",
                            "timeout_unknown", "network_unknown", "transport_unknown",
                            "response_too_large", "invalid_response"})


class ProviderTransportError(Exception):
    """A fixed public code only; raw transport errors are never retained."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProviderConfig:
    backend_id: str
    provider: str
    model: str
    credential_env: str = ""
    max_output_tokens: int = 1024
    timeout: float = 20.0
    max_input_bytes: int = 65536
    anthropic_version: str = "2026-10-21"
    anthropic_workspace_id: str = ""

    def __post_init__(self):
        if self.provider not in ENDPOINTS:
            raise ValueError("unsupported_provider")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,128}", self.backend_id):
            raise ValueError("invalid_backend_id")
        if not isinstance(self.model, str) or not self.model or len(self.model) > 256 or any(
                ord(c) < 32 for c in self.model):
            raise ValueError("model_must_be_configured")
        if type(self.max_output_tokens) is not int or not 16 <= self.max_output_tokens <= 65536:
            raise ValueError("invalid_output_limit")
        if (isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float))
                or not math.isfinite(self.timeout) or not 0 < self.timeout <= 120):
            raise ValueError("invalid_timeout")
        if type(self.max_input_bytes) is not int or not 1 <= self.max_input_bytes <= 262144:
            raise ValueError("invalid_input_limit")
        env = self.credential_env or CREDENTIAL_ENV[self.provider]
        prefix = CREDENTIAL_ENV[self.provider].split("_", 1)[0] + "_"
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", env) or not env.startswith(prefix):
            raise ValueError("provider_credential_namespace_mismatch")
        object.__setattr__(self, "credential_env", env)
        if self.provider == "anthropic":
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.anthropic_version):
                raise ValueError("invalid_anthropic_version")
            if self.anthropic_workspace_id and not re.fullmatch(r"wrkspc_[A-Za-z0-9_-]{1,128}", self.anthropic_workspace_id):
                raise ValueError("invalid_anthropic_workspace")


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    body: Mapping


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    """Approved official endpoints only, bounded bytes, no retry/redirect.

    Socket operations have a timeout; elapsed budget is checked between bounded
    reads. urllib cannot promise a hard whole-operation or DNS cancellation SLA.
    """
    def __init__(self, opener=None, clock=time.monotonic):
        self._opener = opener
        self._clock = clock

    def __call__(self, url, headers, payload, timeout):
        if url not in ENDPOINTS.values() or urllib.parse.urlsplit(url).scheme != "https":
            raise ProviderTransportError("endpoint_not_allowed")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ProviderTransportError("invalid_timeout")
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(data) > 1048576:
            raise ProviderTransportError("request_too_large")
        req = urllib.request.Request(url, data=data, headers=dict(headers), method="POST")
        opener = self._opener or urllib.request.build_opener(_NoRedirect())
        deadline = self._clock() + timeout
        response = None
        try:
            try:
                response = opener.open(req, timeout=timeout)
            except urllib.error.HTTPError as error:
                response = error
            status = response.code
            if 300 <= status < 400:
                return TransportResponse(status, {})
            chunks, count = [], 0
            while True:
                if self._clock() >= deadline:
                    raise ProviderTransportError("timeout_unknown")
                part = response.read(min(16384, MAX_RESPONSE_BYTES + 1 - count))
                if not part:
                    break
                count += len(part)
                if count > MAX_RESPONSE_BYTES:
                    raise ProviderTransportError("response_too_large")
                chunks.append(part)
            try:
                body = json.loads(b"".join(chunks).decode("utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError):
                if not 200 <= status < 300:
                    body = {}
                else:
                    raise ProviderTransportError("invalid_response") from None
            if not isinstance(body, dict):
                raise ProviderTransportError("invalid_response")
            return TransportResponse(status, body)
        except (socket.timeout, TimeoutError):
            raise ProviderTransportError("timeout_unknown") from None
        except urllib.error.URLError:
            raise ProviderTransportError("network_unknown") from None
        except ProviderTransportError:
            raise
        except Exception:
            raise ProviderTransportError("transport_unknown") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


def _tokens(value):
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _usage(body, input_key, output_key):
    raw = body.get("usage")
    if not isinstance(raw, dict):
        return {}
    result = {}
    for target, source in (("input_tokens", input_key), ("output_tokens", output_key), ("total_tokens", "total_tokens")):
        n = _tokens(raw.get(source))
        if n is not None:
            result[target] = n
    return result


def _chat_text(body):
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("invalid_response")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ValueError("invalid_response")
    if message.get("tool_calls") or message.get("function_call"):
        raise ValueError("tool_output_unsupported")
    if choice.get("finish_reason") != "stop":
        reason = choice.get("finish_reason")
        raise ValueError("output_limit" if reason == "length" else "provider_output_not_complete")
    text = message.get("content")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty_response")
    return text


class OpenAIProfile:
    provider = "openai"

    def build(self, request, config):
        # Only completed plain-chat finals may enter this portable history.
        # DoT/Work commentary, tool state and private threads are not imported.
        messages = []
        for role, text in request.history:
            message = {"role": role, "content": text}
            if role == "assistant":
                message["phase"] = "final_answer"
            messages.append(message)
        messages.append({"role": "user", "content": request.text})
        return {"model": request.model or config.model, "input": messages,
                "max_output_tokens": config.max_output_tokens, "store": False, "stream": False}

    def parse(self, body):
        if body.get("status") != "completed":
            raise ValueError("output_limit" if body.get("status") == "incomplete" else "provider_output_not_complete")
        output = body.get("output")
        if not isinstance(output, list):
            raise ValueError("invalid_response")
        parts = []
        for item in output:
            if not isinstance(item, dict):
                raise ValueError("invalid_response")
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                raise ValueError("tool_output_unsupported")
            if item.get("phase") == "commentary":
                continue
            if item.get("phase") not in (None, "final_answer"):
                raise ValueError("invalid_response")
            if item.get("status") not in (None, "completed") or not isinstance(item.get("content"), list):
                raise ValueError("invalid_response")
            for block in item["content"]:
                if not isinstance(block, dict) or block.get("type") != "output_text" or not isinstance(block.get("text"), str):
                    raise ValueError("provider_output_not_complete")
                parts.append(block["text"])
        text = "\n".join(parts)
        if not text.strip():
            raise ValueError("empty_response")
        return text, _usage(body, "input_tokens", "output_tokens")


class AnthropicProfile:
    provider = "anthropic"

    def build(self, request, config):
        messages = [{"role": role, "content": [{"type": "text", "text": text}]} for role, text in request.history]
        messages.append({"role": "user", "content": [{"type": "text", "text": request.text}]})
        return {"model": request.model or config.model, "messages": messages,
                "max_tokens": config.max_output_tokens, "stream": False}

    def parse(self, body):
        if body.get("type") != "message" or body.get("role") != "assistant":
            raise ValueError("invalid_response")
        if body.get("stop_reason") not in ("end_turn", "stop_sequence"):
            raise ValueError("output_limit" if body.get("stop_reason") == "max_tokens" else "provider_output_not_complete")
        blocks = body.get("content")
        if not isinstance(blocks, list) or any(not isinstance(b, dict) or b.get("type") != "text" or not isinstance(b.get("text"), str) for b in blocks):
            raise ValueError("tool_output_unsupported")
        text = "\n".join(block["text"] for block in blocks)
        if not text.strip():
            raise ValueError("empty_response")
        usage = _usage(body, "input_tokens", "output_tokens")
        raw = body.get("usage", {})
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            n = _tokens(raw.get(key)) if isinstance(raw, dict) else None
            if n is not None:
                usage[key] = n
        # Anthropic input_tokens excludes cache reads/writes; preserve components.
        if "input_tokens" in usage and "output_tokens" in usage:
            total = sum(usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if _tokens(total) is not None:
                usage["total_tokens"] = total
        return text, usage


class KimiProfile:
    provider = "kimi"

    def build(self, request, config):
        return {"model": request.model or config.model,
                "messages": [{"role": role, "content": text} for role, text in request.history] + [{"role": "user", "content": request.text}],
                "max_completion_tokens": config.max_output_tokens, "stream": False}

    def parse(self, body):
        usage = _usage(body, "prompt_tokens", "completion_tokens")
        raw = body.get("usage", {}).get("prompt_tokens_details", {}) if isinstance(body.get("usage"), dict) else {}
        if isinstance(raw, dict):
            for source, target in (("cached_tokens", "cached_input_tokens"), ("cache_write_tokens", "cache_write_input_tokens")):
                n = _tokens(raw.get(source))
                if n is not None:
                    usage[target] = n
        return _chat_text(body), usage


class DeepSeekProfile:
    provider = "deepseek"

    def build(self, request, config):
        return {"model": request.model or config.model,
                "messages": [{"role": role, "content": text} for role, text in request.history] + [{"role": "user", "content": request.text}],
                "max_tokens": config.max_output_tokens, "stream": False}

    def parse(self, body):
        usage = _usage(body, "prompt_tokens", "completion_tokens")
        raw = body.get("usage")
        if isinstance(raw, dict):
            for source, target in (("prompt_cache_hit_tokens", "cached_input_tokens"), ("prompt_cache_miss_tokens", "uncached_input_tokens")):
                n = _tokens(raw.get(source))
                if n is not None:
                    usage[target] = n
        return _chat_text(body), usage


class ArkProfile:
    provider = "ark"

    def build(self, request, config):
        # Total reasoning+answer cap; max_tokens alone only caps visible output.
        return {"model": request.model or config.model,
                "messages": [{"role": role, "content": text} for role, text in request.history] + [{"role": "user", "content": request.text}],
                "max_completion_tokens": config.max_output_tokens, "stream": False}

    def parse(self, body):
        service = body.get("service_status")
        if isinstance(service, dict) and isinstance(service.get("model_fallback"), dict) and service["model_fallback"].get("fallback_triggered") is True:
            raise ValueError("provider_model_changed")
        usage = _usage(body, "prompt_tokens", "completion_tokens")
        raw = body.get("usage")
        if isinstance(raw, dict):
            for details, key, target in (("prompt_tokens_details", "cached_tokens", "cached_input_tokens"),
                                         ("completion_tokens_details", "reasoning_tokens", "reasoning_output_tokens")):
                detail = raw.get(details)
                n = _tokens(detail.get(key)) if isinstance(detail, dict) else None
                if n is not None:
                    usage[target] = n
        return _chat_text(body), usage


PROFILES = {p.provider: p() for p in (OpenAIProfile, AnthropicProfile, KimiProfile, DeepSeekProfile, ArkProfile)}


def _http_error(status, body):
    error = body.get("error") if isinstance(body, dict) else None
    code = None
    if isinstance(error, dict):
        code = error.get("code") or error.get("type")
    quota_codes = {"insufficient_quota", "exceeded_current_quota_error", "insufficient_balance", "account_deactivated", "InsufficientBalance"}
    if (isinstance(code, str) and code in quota_codes) or status == 402:
        return "quota_unavailable"
    if status == 401:
        return "authentication_failed"
    if status == 403:
        return "permission_denied"
    if status == 404:
        return "model_or_endpoint_unavailable"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "provider_unavailable"
    if 300 <= status < 400:
        return "redirect_refused"
    if status == 413:
        return "request_too_large"
    return "provider_request_rejected"


class ApiProvider(UnsupportedOperations):
    capabilities = BackendCapabilities(text=True, usage=True)

    def __init__(self, config, transport=None, credential_resolver=None, clock=time.time, allow_network=False):
        if not isinstance(config, ProviderConfig):
            raise TypeError("provider_config_required")
        self.config = config
        self.backend_id = config.backend_id
        self._transport = transport or UrllibTransport()
        self._injected_transport = transport is not None
        self._credential_resolver = credential_resolver or os.environ.get
        self._clock = clock
        self._allow_network = allow_network is True

    def health(self):
        return {"state": "configured_unverified" if self._allow_network or self._injected_transport else "blocked",
                "provider": self.config.provider, "verification": "not_performed"}

    def submit(self, request):
        def result(status, code):
            return BackendResult(request.request_id, status, error_code=code)
        if request.attachments:
            return result("unsupported", "media_not_supported")
        if not self._allow_network and not self._injected_transport:
            return result("failed", "network_not_authorized")
        deadline = request.deadline_at
        if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline)):
            return result("failed", "invalid_deadline")
        now = self._clock()
        if deadline is not None and deadline <= now:
            return result("failed", "deadline_elapsed_not_called")
        timeout = self.config.timeout if deadline is None else min(self.config.timeout, deadline - now)
        if len(request.history) > 64 or sum(len(text.encode("utf-8")) for _, text in request.history) + len(request.text.encode("utf-8")) > self.config.max_input_bytes:
            return result("failed", "input_budget_exceeded")
        try:
            key = self._credential_resolver(self.config.credential_env)
        except Exception:
            return result("failed", "credential_resolution_failed")
        if not isinstance(key, str) or not key or len(key) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in key):
            return result("failed", "credential_missing_or_invalid")
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        if self.config.provider == "anthropic":
            headers["anthropic-version"] = self.config.anthropic_version
            if self.config.anthropic_workspace_id:
                headers["anthropic-workspace-id"] = self.config.anthropic_workspace_id
        try:
            profile = PROFILES[self.config.provider]
            payload = profile.build(request, self.config)
            # Recheck after credential resolution/build so a queued call cannot refresh its budget.
            if deadline is not None:
                timeout = min(timeout, deadline - self._clock())
                if timeout <= 0:
                    return result("failed", "deadline_elapsed_not_called")
            response = self._transport(ENDPOINTS[self.config.provider], headers, payload, timeout)
        except ProviderTransportError as error:
            code = error.code if isinstance(error.code, str) and error.code in TRANSPORT_CODES else "transport_unknown"
            return result("unknown", code)
        except (TimeoutError, socket.timeout):
            return result("unknown", "timeout_unknown")
        except Exception:
            return result("unknown", "transport_unknown")
        finally:
            # Drop our temporary references; no claim of secure Python memory erasure.
            key = None
            headers = None
        if not isinstance(response, TransportResponse) or type(response.status_code) is not int or not isinstance(response.body, dict):
            return result("unknown", "invalid_transport_result")
        if not 200 <= response.status_code < 300:
            return result("failed", _http_error(response.status_code, response.body))
        if deadline is not None and self._clock() >= deadline:
            return result("unknown", "late_result_not_deliverable")
        try:
            text, usage = profile.parse(response.body)
        except ValueError as error:
            fixed_codes = {"invalid_response", "tool_output_unsupported", "output_limit", "provider_output_not_complete", "empty_response", "provider_model_changed"}
            return result("failed", str(error) if str(error) in fixed_codes else "invalid_response")
        except Exception:
            return result("failed", "invalid_response")
        if len(text.encode("utf-8")) > 65536:
            return result("failed", "output_too_large")
        return BackendResult(request.request_id, "succeeded", text=text, backend_session=request.backend_session, usage=usage)
