"""Synthetic protocol tests; no real key lookup, HTTP, quota or WeChat calls."""
import io
import json
import socket
import unittest
import urllib.error
from dataclasses import replace
from unittest.mock import patch

from foundation.contracts import AttachmentRef, BackendRequest
from foundation.providers import (ApiProvider, ENDPOINTS, MAX_RESPONSE_BYTES,
                                  ProviderConfig, ProviderTransportError,
                                  TransportResponse, UrllibTransport, _NoRedirect)


def request(**kwargs):
    return BackendRequest("request-1", "message-1", "conversation-1", "hello", **kwargs)


def chat_result(text="reply", **kwargs):
    body = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}}
    body.update(kwargs)
    return body


def openai_result(**kwargs):
    body = {"status": "completed", "output": [{"type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": "reply"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}
    body.update(kwargs)
    return body


def anthropic_result(**kwargs):
    body = {"type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "reply"}],
            "usage": {"input_tokens": 3, "output_tokens": 4}}
    body.update(kwargs)
    return body


class Recorder:
    def __init__(self, body=None, status=200, error=None):
        self.body = body if body is not None else chat_result()
        self.status, self.error, self.calls = status, error, []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append((url, dict(headers), payload, timeout))
        if self.error:
            raise self.error
        return TransportResponse(self.status, self.body)


class ProviderTests(unittest.TestCase):
    def test_large_valid_provider_text_is_a_failure_result_not_unhandled_storage_error(self):
        transport = Recorder(chat_result("x" * 65537))
        result = self.provider("deepseek", transport=transport).submit(request())
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.error_code, "output_too_large")
        self.assertIsNone(result.text)
        self.assertEqual(len(transport.calls), 1)

    def provider(self, vendor, transport=None, resolver=None, **kwargs):
        config = ProviderConfig(vendor + "-test", vendor, "configured-model", **kwargs)
        return ApiProvider(config, transport=transport or Recorder(),
                           credential_resolver=resolver or (lambda _: "synthetic-token"), clock=lambda: 100)

    def test_constructor_and_health_do_not_resolve_credentials_or_network(self):
        lookup = lambda _: self.fail("credential read")
        with patch("urllib.request.build_opener", side_effect=AssertionError("network constructor")):
            backend = ApiProvider(ProviderConfig("o", "openai", "configured-model"), credential_resolver=lookup)
            self.assertEqual(backend.health()["state"], "blocked")
            self.assertEqual(backend.submit(request()).error_code, "network_not_authorized")

    def test_model_and_provider_are_required(self):
        for vendor, model in (("other", "model"), ("openai", ""), ("openai", "model\ninvalid")):
            with self.subTest(vendor=vendor, model=model), self.assertRaises(ValueError):
                ProviderConfig("backend", vendor, model)

    def test_credentials_are_distinct_provider_namespaces(self):
        expected = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "kimi": "MOONSHOT_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "ark": "ARK_API_KEY"}
        for vendor, name in expected.items():
            with self.subTest(vendor=vendor):
                self.assertEqual(ProviderConfig(vendor, vendor, "model").credential_env, name)
                if vendor != "openai":
                    with self.assertRaisesRegex(ValueError, "namespace"):
                        ProviderConfig(vendor, vendor, "model", credential_env="OPENAI_API_KEY")

    def test_bad_limits_are_rejected(self):
        for kwargs in ({"timeout": 0}, {"timeout": float("nan")}, {"timeout": True}, {"timeout": 121},
                       {"max_output_tokens": True}, {"max_output_tokens": 0}, {"max_input_bytes": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ProviderConfig("o", "openai", "model", **kwargs)

    def test_openai_responses_mapping_and_store_false(self):
        capture = Recorder(openai_result())
        result = self.provider("openai", capture).submit(request(history=(("user", "before"), ("assistant", "answer"))))
        url, headers, body, timeout = capture.calls[0]
        self.assertEqual(url, ENDPOINTS["openai"])
        self.assertEqual(body["input"][1]["content"], "answer")
        self.assertEqual(body["input"][1]["phase"], "final_answer")
        self.assertEqual(body["input"][2]["content"], "hello")
        self.assertFalse(body["store"])
        self.assertNotIn("messages", body)
        self.assertEqual(body["max_output_tokens"], 1024)
        self.assertEqual(result.text, "reply")
        self.assertEqual(dict(result.usage), {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7})

    def test_anthropic_messages_version_workspace_and_cache_usage(self):
        capture = Recorder(anthropic_result(usage={"input_tokens": 3, "output_tokens": 4,
                           "cache_read_input_tokens": 5, "cache_creation_input_tokens": 6}))
        backend = self.provider("anthropic", capture, anthropic_workspace_id="wrkspc_synthetic")
        result = backend.submit(request())
        url, headers, body, timeout = capture.calls[0]
        self.assertEqual(url, "https://api.anthropic.com/v1/messages")
        self.assertEqual(headers["anthropic-version"], "2026-10-21")
        self.assertEqual(headers["anthropic-workspace-id"], "wrkspc_synthetic")
        self.assertEqual(body["max_tokens"], 1024)
        self.assertEqual(body["messages"][0]["content"][0]["type"], "text")
        self.assertEqual(result.usage["total_tokens"], 18)

    def test_anthropic_version_and_workspace_validation(self):
        for kwargs in ({"anthropic_version": "bad"}, {"anthropic_workspace_id": "bad\nheader"}):
            with self.assertRaises(ValueError):
                ProviderConfig("a", "anthropic", "model", **kwargs)

    def test_kimi_has_current_token_parameter_and_cache_mapping(self):
        capture = Recorder(chat_result(usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                           "prompt_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 3}}))
        result = self.provider("kimi", capture).submit(request())
        self.assertIn("max_completion_tokens", capture.calls[0][2])
        self.assertNotIn("max_tokens", capture.calls[0][2])
        self.assertEqual(result.usage["cached_input_tokens"], 5)
        self.assertEqual(result.usage["cache_write_input_tokens"], 3)

    def test_deepseek_parameter_and_cache_mapping(self):
        capture = Recorder(chat_result(usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                           "prompt_cache_hit_tokens": 7, "prompt_cache_miss_tokens": 3}))
        result = self.provider("deepseek", capture).submit(request())
        self.assertIn("max_tokens", capture.calls[0][2])
        self.assertEqual(result.usage["uncached_input_tokens"], 3)
        self.assertEqual(result.usage["total_tokens"], 12)

    def test_ark_total_completion_cap_and_reasoning_usage(self):
        capture = Recorder(chat_result(usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                           "completion_tokens_details": {"reasoning_tokens": 1}}))
        result = self.provider("ark", capture).submit(request())
        self.assertIn("max_completion_tokens", capture.calls[0][2])
        self.assertEqual(result.usage["reasoning_output_tokens"], 1)
        self.assertEqual(capture.calls[0][0], ENDPOINTS["ark"])

    def test_ark_server_fallback_is_not_reported_as_requested_model(self):
        result = self.provider("ark", Recorder(chat_result(service_status={"model_fallback": {"fallback_triggered": True}}))).submit(request())
        self.assertEqual(result.error_code, "provider_model_changed")
        self.assertIsNone(result.text)

    def test_no_tools_media_stream_or_remote_control_claims(self):
        for vendor in ENDPOINTS:
            backend = self.provider(vendor)
            for key in ("images", "files", "audio", "video", "streaming", "tools", "approvals", "cancellation", "task_query", "callbacks"):
                self.assertIs(getattr(backend.capabilities, key), False)
            self.assertEqual(backend.status("task").status, "unsupported")
            self.assertEqual(backend.cancel("task").status, "unsupported")

    def test_attachments_are_rejected_before_credentials_and_transport(self):
        capture = Recorder()
        backend = self.provider("kimi", capture, resolver=lambda _: self.fail("credential read"))
        result = backend.submit(request(attachments=(AttachmentRef("a", "image/png", 1, "0" * 64),)))
        self.assertEqual(result.status, "unsupported")
        self.assertEqual(capture.calls, [])

    def test_expired_deadline_does_not_read_key_or_submit(self):
        capture = Recorder()
        backend = self.provider("kimi", capture, resolver=lambda _: self.fail("key read"))
        result = backend.submit(request(deadline_at=99))
        self.assertEqual(result.error_code, "deadline_elapsed_not_called")
        self.assertEqual(capture.calls, [])

    def test_deadline_caps_single_transport_timeout(self):
        capture = Recorder()
        result = self.provider("kimi", capture).submit(request(deadline_at=103))
        self.assertEqual(capture.calls[0][3], 3)
        self.assertEqual(result.status, "succeeded")

    def test_expiry_during_key_resolution_prevents_call(self):
        times = iter((100, 105))
        capture = Recorder()
        backend = self.provider("kimi", capture)
        backend._clock = lambda: next(times)
        self.assertEqual(backend.submit(request(deadline_at=103)).error_code, "deadline_elapsed_not_called")
        self.assertEqual(capture.calls, [])

    def test_late_result_is_hidden_without_second_call(self):
        times = iter((100, 101, 104))
        capture = Recorder()
        backend = self.provider("kimi", capture)
        backend._clock = lambda: next(times)
        result = backend.submit(request(deadline_at=103))
        self.assertEqual((result.status, result.error_code, result.text), ("unknown", "late_result_not_deliverable", None))
        self.assertEqual(len(capture.calls), 1)

    def test_input_budget_enforces_history_as_well_as_current_text(self):
        capture = Recorder()
        backend = self.provider("kimi", capture, max_input_bytes=5)
        self.assertEqual(backend.submit(request(history=(("user", "before"),))).error_code, "input_budget_exceeded")
        self.assertEqual(capture.calls, [])

    def test_missing_and_header_injection_key_values_do_not_submit(self):
        for key in (None, "", "bad\r\nheader", "token with space"):
            capture = Recorder()
            result = self.provider("kimi", capture, resolver=lambda _, k=key: k).submit(request())
            self.assertEqual(result.error_code, "credential_missing_or_invalid")
            self.assertEqual(capture.calls, [])

    def test_resolver_exception_does_not_escape(self):
        def resolve(_):
            raise RuntimeError("synthetic-private-value")
        result = self.provider("kimi", resolver=resolve).submit(request())
        self.assertNotIn("synthetic-private-value", repr(result))
        self.assertEqual(result.error_code, "credential_resolution_failed")

    def test_http_error_categories_never_retry(self):
        cases = ((401, {}, "authentication_failed"), (403, {}, "permission_denied"),
                 (404, {}, "model_or_endpoint_unavailable"), (429, {}, "rate_limited"),
                 (500, {}, "provider_unavailable"), (302, {}, "redirect_refused"),
                 (429, {"error": {"type": "exceeded_current_quota_error", "message": "private"}}, "quota_unavailable"),
                 (402, {}, "quota_unavailable"), (413, {}, "request_too_large"))
        for status, body, code in cases:
            with self.subTest(status=status, code=code):
                capture = Recorder(body, status)
                result = self.provider("kimi", capture).submit(request())
                self.assertEqual(result.error_code, code)
                self.assertEqual(len(capture.calls), 1)

    def test_timeout_and_opaque_transport_exceptions_are_unknown(self):
        for error, code in ((TimeoutError("private"), "timeout_unknown"),
                            (ProviderTransportError("network_unknown"), "network_unknown"),
                            (RuntimeError("private"), "transport_unknown")):
            capture = Recorder(error=error)
            result = self.provider("kimi", capture).submit(request())
            self.assertEqual((result.status, result.error_code), ("unknown", code))
            self.assertNotIn("private", repr(result))
            self.assertEqual(len(capture.calls), 1)

    def test_custom_transport_cannot_export_arbitrary_error_code(self):
        result = self.provider("kimi", Recorder(error=ProviderTransportError("synthetic-private"))).submit(request())
        self.assertEqual(result.error_code, "transport_unknown")
        self.assertNotIn("synthetic-private", repr(result))

    def test_truncated_and_tool_output_are_not_success(self):
        cases = [chat_result(), chat_result()]
        cases[0]["choices"][0]["finish_reason"] = "length"
        cases[1]["choices"][0]["message"]["tool_calls"] = [{"id": "tool"}]
        for body, code in zip(cases, ("output_limit", "tool_output_unsupported")):
            result = self.provider("kimi", Recorder(body)).submit(request())
            self.assertEqual(result.error_code, code)
            self.assertIsNone(result.text)

    def test_openai_reasoning_text_not_exposed_and_tool_items_rejected(self):
        body = openai_result()
        body["output"].insert(0, {"type": "reasoning", "summary": "not exported"})
        result = self.provider("openai", Recorder(body)).submit(request())
        self.assertEqual(result.text, "reply")
        body["output"].append({"type": "function_call", "arguments": "private"})
        self.assertEqual(self.provider("openai", Recorder(body)).submit(request()).error_code, "tool_output_unsupported")

    def test_openai_commentary_is_not_returned_as_final(self):
        body = openai_result()
        body["output"].insert(0, {"type": "message", "role": "assistant", "phase": "commentary",
            "content": [{"type": "output_text", "text": "working"}]})
        self.assertEqual(self.provider("openai", Recorder(body)).submit(request()).text, "reply")

    def test_opaque_error_code_does_not_raise_or_escape(self):
        result = self.provider("kimi", Recorder({"error": {"code": {"private": "data"}}}, 429)).submit(request())
        self.assertEqual(result.error_code, "rate_limited")

    def test_invalid_usage_is_omitted_not_zero_filled(self):
        body = chat_result(usage={"prompt_tokens": True, "completion_tokens": -1, "total_tokens": None})
        result = self.provider("kimi", Recorder(body)).submit(request())
        self.assertEqual(dict(result.usage), {})

    def test_credential_not_added_to_payload_and_private_metadata_not_exported(self):
        capture = Recorder(chat_result(secret="do-not-export", id="private-response"))
        result = self.provider("kimi", capture, resolver=lambda _: "synthetic-sensitive-token").submit(request())
        self.assertNotIn("synthetic-sensitive-token", json.dumps(capture.calls[0][2]))
        self.assertNotIn("private-response", repr(result))
        self.assertNotIn("do-not-export", repr(result))


class Response(io.BytesIO):
    def __init__(self, body, code=200):
        super().__init__(body)
        self.code = code


class Opener:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class TransportTests(unittest.TestCase):
    def test_official_https_only(self):
        opener = Opener()
        transport = UrllibTransport(opener=opener)
        for url in ("http://api.openai.com/v1/responses", "https://other.invalid/", ENDPOINTS["openai"] + "?redirect=1"):
            with self.assertRaises(ProviderTransportError):
                transport(url, {}, {}, 1)
        self.assertEqual(opener.calls, [])

    def test_post_json_and_finite_timeout_with_bounded_read(self):
        opener = Opener(Response(b'{"ok":true}'))
        response = UrllibTransport(opener=opener)(ENDPOINTS["openai"], {"Authorization": "Bearer synthetic"}, {"input": "hello"}, 2)
        self.assertEqual(response.body, {"ok": True})
        req, timeout = opener.calls[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(timeout, 2)
        self.assertEqual(json.loads(req.data), {"input": "hello"})

    def test_redirect_policy_never_follows_target(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://other.invalid"))
        opener = Opener(Response(b'<html>redirect</html>', 302))
        result = UrllibTransport(opener=opener)(ENDPOINTS["openai"], {}, {}, 2)
        self.assertEqual(result.status_code, 302)
        self.assertEqual(opener.calls[0][0].full_url, ENDPOINTS["openai"])
        self.assertEqual(len(opener.calls), 1)

    def test_oversized_response_is_bounded(self):
        opener = Opener(Response(b'x' * (MAX_RESPONSE_BYTES + 1)))
        with self.assertRaisesRegex(ProviderTransportError, "response_too_large"):
            UrllibTransport(opener=opener)(ENDPOINTS["kimi"], {}, {}, 1)

    def test_malformed_success_response_is_unknown_and_errors_keep_status(self):
        for body in (b'bad', b'[]', b'{"x":NaN}', b'\xff'):
            with self.subTest(body=body), self.assertRaises(ProviderTransportError):
                UrllibTransport(opener=Opener(Response(body)))(ENDPOINTS["kimi"], {}, {}, 1)
        response = UrllibTransport(opener=Opener(Response(b'<html>bad</html>', 503)))(ENDPOINTS["kimi"], {}, {}, 1)
        self.assertEqual((response.status_code, response.body), (503, {}))

    def test_socket_timeout_and_network_error_have_no_raw_details(self):
        for error, code in ((socket.timeout("private"), "timeout_unknown"),
                            (urllib.error.URLError("private"), "network_unknown")):
            with self.subTest(error=type(error).__name__), self.assertRaisesRegex(ProviderTransportError, code) as caught:
                UrllibTransport(opener=Opener(error=error))(ENDPOINTS["kimi"], {}, {}, 1)
            self.assertNotIn("private", str(caught.exception))

    def test_elapsed_read_budget_expires(self):
        times = iter((100, 102))
        with self.assertRaisesRegex(ProviderTransportError, "timeout_unknown"):
            UrllibTransport(opener=Opener(Response(b'{}')), clock=lambda: next(times))(ENDPOINTS["kimi"], {}, {}, 1)


if __name__ == "__main__":
    unittest.main()
