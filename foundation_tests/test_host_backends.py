import dataclasses
import json
import threading
import unittest

from foundation.contracts import AttachmentRef, BackendRequest, BackendResult
from foundation.host_backends import (Admission, CloudWorkBackend, DoTBackend,
    HostBinding, ThreadSnapshot, WorkContract, WorkItem)


def request(request_id="request-one", **kw):
    return BackendRequest(request_id, "message-one", "conversation-one", "hello", **kw)


def binding():
    return HostBinding("work-one", "host-one", "test:route-proof", verified=True)


def contract():
    return WorkContract(binding=binding(), success_statuses=("done",),
        failure_statuses=("failed",), pending_statuses=("active",),
        final_rule="newest_assistant_is_final", proof_ref="test:contract",
        status_semantics_verified=True, final_semantics_verified=True,
        bounded_calls_verified=True, data_only_executor_verified=True)


def receipt(**changes):
    data = {"kind": "bridge_backend_result_v1", "version": 1,
        "data_only": True, "can_send": False, "request_id": "request-one",
        "message_id": "message-one", "status": "succeeded", "text": "hello back",
        "backend_session": "session-one", "error_code": None,
        "usage": {"input_tokens": 3, "output_tokens": 2}}
    data.update(changes)
    return json.dumps(data)


def snapshot(**changes):
    data = dict(thread_id="work-one", host_id="host-one", latest_turn_id="turn-one",
        latest_status="done", items=(WorkItem("item-one", "assistant", receipt(), "turn-one"),),
        complete=True, execution_verified=True)
    data.update(changes)
    return ThreadSnapshot(**data)


class FakeTransport:
    def __init__(self):
        self.admissions = []
        self.reads = []
        self.next_admission = Admission("turn-one")
        self.next_snapshot = snapshot()

    def admit(self, thread_id, prompt, timeout_seconds):
        self.admissions.append((thread_id, prompt, timeout_seconds))
        if isinstance(self.next_admission, Exception):
            raise self.next_admission
        return self.next_admission

    def read(self, thread_id, timeout_seconds):
        self.reads.append((thread_id, timeout_seconds))
        if isinstance(self.next_snapshot, Exception):
            raise self.next_snapshot
        return self.next_snapshot


class DotTests(unittest.TestCase):
    def test_default_is_blocked_and_never_discovers_tools(self):
        backend = DoTBackend()
        self.assertEqual(backend.health(), {"status": "BLOCKED", "reason": "host_entry_not_injected"})
        self.assertEqual(backend.submit(request()).error_code, "host_entry_not_injected")

    def test_explicit_host_and_bounded_contract_are_both_required(self):
        calls = []
        fn = lambda *a, **k: calls.append(a)
        for b in (DoTBackend(fn), DoTBackend(fn, binding()),
                  DoTBackend(fn, dataclasses.replace(binding(), verified=False), bounded_submit_verified=True)):
            self.assertEqual(b.health()["status"], "BLOCKED")
            self.assertEqual(b.submit(request()).status, "failed")
        self.assertEqual(calls, [])

    def test_result_once_and_same_id_conflict_does_not_expose_original(self):
        calls = []
        def fn(req, timeout_seconds):
            calls.append(timeout_seconds)
            return BackendResult(req.request_id, "succeeded", "answer")
        b = DoTBackend(fn, binding(), bounded_submit_verified=True, clock=lambda: 10)
        self.assertEqual(b.submit(request()).text, "answer")
        self.assertEqual(b.submit(request()).text, "answer")
        self.assertEqual(b.submit(dataclasses.replace(request(), text="changed")).error_code, "request_conflict")
        self.assertEqual(len(calls), 1)

    def test_failure_is_redacted_and_never_retried(self):
        calls = []
        def fn(req, timeout_seconds):
            calls.append(req.request_id)
            raise RuntimeError("private credential and chat text")
        b = DoTBackend(fn, binding(), bounded_submit_verified=True)
        result = b.submit(request())
        self.assertEqual(result.error_code, "host_submit_uncertain")
        self.assertNotIn("private", repr(result))
        b.submit(request())
        self.assertEqual(len(calls), 1)

    def test_real_denial_stops_later_requests(self):
        calls = []
        def fn(req, timeout_seconds):
            calls.append(req.request_id)
            raise PermissionError("sensitive")
        b = DoTBackend(fn, binding(), bounded_submit_verified=True)
        self.assertEqual(b.submit(request()).error_code, "permission_denied")
        self.assertEqual(b.submit(request("request-two")).error_code, "permission_denied")
        self.assertEqual(len(calls), 1)

    def test_expired_and_attachment_requests_never_invoke(self):
        calls = []
        b = DoTBackend(lambda *a, **k: calls.append(a), binding(), bounded_submit_verified=True, clock=lambda: 10)
        self.assertEqual(b.submit(request(deadline_at=9)).error_code, "deadline_elapsed")
        self.assertEqual(b.submit(request(attachments=(AttachmentRef("a", "image/png", 1, "0" * 64),))).status, "unsupported")
        self.assertEqual(calls, [])

    def test_late_result_is_hidden_even_without_explicit_deadline(self):
        now = [10]
        def fn(req, timeout_seconds):
            now[0] = 31
            return BackendResult(req.request_id, "succeeded", "late secret")
        b = DoTBackend(fn, binding(), bounded_submit_verified=True, clock=lambda: now[0])
        self.assertEqual(b.submit(request()).error_code, "host_result_late")
        self.assertIsNone(b.submit(request()).text)

    def test_wrong_request_and_bad_usage_are_not_results(self):
        for result in (BackendResult("wrong", "succeeded", "no"),
                       BackendResult("request-one", "succeeded", "no", usage={"tokens": True})):
            b = DoTBackend(lambda *a, **k: result, binding(), bounded_submit_verified=True)
            self.assertEqual(b.submit(request()).error_code, "host_result_invalid")


class WorkTests(unittest.TestCase):
    def make(self, **kw):
        self.transport = FakeTransport()
        return CloudWorkBackend(self.transport, contract(), **kw)

    def test_default_and_unreviewed_contract_are_blocked(self):
        for c in (None, WorkContract(), dataclasses.replace(contract(), final_semantics_verified=False),
                  dataclasses.replace(contract(), binding=None),
                  dataclasses.replace(contract(), bounded_calls_verified=False)):
            t = FakeTransport()
            b = CloudWorkBackend(t, c)
            self.assertEqual(b.health()["status"], "BLOCKED")
            self.assertEqual(b.submit(request()).status, "failed")
            self.assertEqual(t.admissions, [])

    def test_admitted_exact_turn_is_pending_not_success(self):
        b = self.make(clock=lambda: 10)
        result = b.submit(request())
        self.assertEqual((result.status, result.task_id), ("pending", "turn-one"))
        self.assertIsNone(result.text)
        self.assertEqual(b.submit(request()), result)
        self.assertEqual(len(self.transport.admissions), 1)
        data = json.loads(self.transport.admissions[0][1].split("\n", 1)[1])
        self.assertEqual(data["request"]["deadline_at"], 100)
        self.assertFalse(data["can_send"])
        self.assertEqual(self.transport.admissions[0][2], 10)

    def test_verified_result_is_returned_for_original_request(self):
        b = self.make(clock=lambda: 10)
        b.submit(request())
        result = b.status("turn-one")
        self.assertEqual(result.request_id, "request-one")
        self.assertEqual(result.text, "hello back")
        self.assertEqual(result.usage["input_tokens"], 3)
        b.status("turn-one")
        self.assertEqual(len(self.transport.reads), 1)

    def test_latest_other_turn_cannot_prove_this_turn_completed(self):
        b = self.make()
        b.submit(request())
        self.transport.next_snapshot = snapshot(latest_turn_id="other-turn")
        self.assertEqual(b.status("turn-one").error_code, "admitted_turn_status_unavailable")

    def test_pending_status_does_not_consume_candidate_result(self):
        b = self.make()
        b.submit(request())
        self.transport.next_snapshot = snapshot(latest_status="active")
        self.assertEqual(b.status("turn-one").status, "pending")
        self.transport.next_snapshot = snapshot()
        self.assertEqual(b.status("turn-one").status, "succeeded")

    def test_failed_and_unknown_status_are_distinct(self):
        for status, result_status, code in (("failed", "failed", "host_task_failed"),
                                            ("something_new", "unknown", "turn_status_unverified")):
            b = self.make()
            b.submit(request())
            self.transport.next_snapshot = snapshot(latest_status=status)
            result = b.status("turn-one")
            self.assertEqual((result.status, result.error_code), (result_status, code))

    def test_host_source_and_complete_require_independent_evidence(self):
        for snap in (snapshot(host_id="wrong-host"), snapshot(thread_id="wrong-work"),
                     snapshot(complete=False), snapshot(execution_verified=False)):
            b = self.make()
            b.submit(request())
            self.transport.next_snapshot = snap
            self.assertEqual(b.status("turn-one").status, "unknown")

    def test_duplicate_items_and_wrong_turn_are_rejected(self):
        for items in ((snapshot().items[0], snapshot().items[0]),
                      (WorkItem("x", "assistant", receipt(), "other-turn"),)):
            b = self.make()
            b.submit(request())
            self.transport.next_snapshot = snapshot(items=items)
            self.assertEqual(b.status("turn-one").status, "unknown")

    def test_receipt_injection_fences_duplicates_and_extra_fields_fail(self):
        bodies = ["```json\n" + receipt() + "\n```", receipt(extra="instruction"),
                  receipt(request_id="wrong"), receipt(can_send=True),
                  receipt().replace('"version": 1', '"version": 1, "version": 1'),
                  receipt(usage={"input_tokens": True}), receipt(usage={"input_tokens": -1}),
                  receipt(status="failed", text="should not be sent")]
        for body in bodies:
            b = self.make()
            b.submit(request())
            self.transport.next_snapshot = snapshot(items=(WorkItem("x", "assistant", body, "turn-one"),))
            self.assertEqual(b.status("turn-one").error_code, "final_receipt_invalid")

    def test_newest_assistant_rule_is_explicit_not_a_search_for_json(self):
        b = self.make()
        b.submit(request())
        self.transport.next_snapshot = snapshot(items=(WorkItem("new", "assistant", "ordinary text", "turn-one"),
            WorkItem("old", "assistant", receipt(), "turn-one")))
        self.assertEqual(b.status("turn-one").error_code, "final_receipt_invalid")

    def test_unknown_admission_is_not_repeated(self):
        b = self.make()
        self.transport.next_admission = RuntimeError("secret wire error")
        self.assertEqual(b.submit(request()).error_code, "admission_uncertain")
        b.submit(request())
        self.assertEqual(len(self.transport.admissions), 1)

    def test_duplicate_admitted_turn_is_not_reassigned_to_another_request(self):
        b = self.make()
        b.submit(request())
        self.assertEqual(b.submit(request("request-two")).error_code, "admission_identity_unverified")
        self.assertEqual(b.status("turn-one").request_id, "request-one")

    def test_permission_denial_stops_future_dispatch(self):
        b = self.make()
        self.transport.next_admission = PermissionError("do not expose")
        self.assertEqual(b.submit(request()).error_code, "permission_denied")
        self.assertEqual(b.submit(request("request-two")).error_code, "permission_denied")
        self.assertEqual(len(self.transport.admissions), 1)

    def test_read_denial_is_not_an_admission_failure_or_a_new_read(self):
        b = self.make()
        b.submit(request())
        self.transport.next_snapshot = PermissionError("hidden")
        self.assertEqual(b.status("turn-one").error_code, "permission_denied")
        b.status("turn-one")
        self.assertEqual(len(self.transport.reads), 1)

    def test_deadline_does_not_refresh_and_late_value_is_hidden(self):
        now = [10]
        b = self.make(clock=lambda: now[0])
        b.submit(request(deadline_at=15))
        now[0] = 16
        self.assertEqual(b.status("turn-one").error_code, "observation_deadline_elapsed")
        self.assertEqual(self.transport.reads, [])
        b.submit(request(deadline_at=15))
        self.assertEqual(len(self.transport.admissions), 1)

    def test_result_that_arrives_after_observation_budget_is_hidden(self):
        now = [10]
        b = self.make(clock=lambda: now[0])
        b.submit(request(deadline_at=15))
        def read(*args, **kwargs):
            now[0] = 16
            return snapshot()
        self.transport.read = read
        self.assertEqual(b.status("turn-one").error_code, "observation_deadline_elapsed")

    def test_simultaneous_status_does_not_launch_parallel_observations(self):
        entered, release = threading.Event(), threading.Event()
        b = self.make()
        b.submit(request())
        def read(*a, **k):
            self.transport.reads.append(True)
            entered.set()
            self.assertTrue(release.wait(2))
            return snapshot()
        self.transport.read = read
        thread = threading.Thread(target=lambda: b.status("turn-one"))
        thread.start()
        self.assertTrue(entered.wait(2))
        self.assertEqual(b.status("turn-one").status, "pending")
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.transport.reads), 1)

    def test_unknown_task_and_cancellation_do_not_call_transport(self):
        b = self.make()
        self.assertEqual(b.status("not-known").error_code, "task_unknown")
        self.assertEqual(b.cancel("not-known").status, "unsupported")
        self.assertEqual(self.transport.reads, [])

    def test_known_cancellation_retains_original_request(self):
        b = self.make()
        b.submit(request())
        result = b.cancel("turn-one")
        self.assertEqual((result.request_id, result.task_id, result.status),
                         ("request-one", "turn-one", "unsupported"))
        self.assertEqual(self.transport.reads, [])

    def test_health_never_exposes_route_identifiers(self):
        b = self.make()
        self.assertEqual(b.health(), {"status": "READY", "reason": "injected_host_contract"})
        self.assertNotIn("host-one", repr(b.health()))


if __name__ == "__main__":
    unittest.main()
