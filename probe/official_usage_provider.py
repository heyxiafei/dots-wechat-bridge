"""Host capability adapter, not an HTTP client or credential discovery mechanism.

The trusted host supplies a callable for an actually available official tool or
an already-authorized App Server session. Never construct this from owner text,
quoted content, arbitrary JSON, a URL, or an agent's claimed tool availability.
"""
from dataclasses import dataclass
import asyncio
import inspect
import math
import threading
import time
from typing import Callable, Optional
from .usage_display import (UsageBinding, UsageObservation, UsageView, CreditInfo,
                            normalize_usage, DEFAULT_MAX_AGE_SECONDS)
from .command_presentation import render_weekly_usage
from .fixed_commands import prepare_weekly_usage


class OfficialReadDenied(Exception):
    """Host classified an actual permission refusal; no fallback or retry."""


@dataclass(frozen=True)
class OfficialCapability:
    source: str
    binding: UsageBinding
    binding_evidence_ref: str
    read: Callable
    credit_mapper: Optional[Callable] = None


@dataclass
class _PendingRead:
    capability: OfficialCapability
    binding: UsageBinding
    request_ref: Optional[str]
    payload: object
    started: float
    started_monotonic: float
    deadline: float
    finished: Optional[float] = None
    finished_monotonic: Optional[float] = None
    claimed: bool = False
    timed_out: bool = False


@dataclass
class _TimedOutRead:
    capability: OfficialCapability
    binding: UsageBinding
    request_ref: Optional[str]
    result: dict


class OfficialUsageProvider:
    MAX_TIMEOUT_RECORDS = 256

    def __init__(self, capability=None, clock=time.time, *, read_timeout_seconds=20,
                 monotonic=time.monotonic):
        if (type(read_timeout_seconds) not in (int, float)
                or not 0 < read_timeout_seconds <= 60 or not math.isfinite(read_timeout_seconds)):
            raise ValueError('invalid_official_read_timeout')
        self.capability = capability
        self.clock = clock
        self.monotonic = monotonic
        self.read_timeout_seconds = float(read_timeout_seconds)
        # One unresolved read, scoped to the original capability/account/request.
        # A sync caller must not discard a Future which may already have run.
        self._pending = None
        self._pending_lock = threading.RLock()
        # Never evict a timed-out request and accidentally read it again. This
        # bounded process-local fence supplements, not replaces, the host journal.
        self._timed_out_reads = []
        self._late_denied_capabilities = []

    @classmethod
    def from_host_tools(cls, tools, binding, evidence_ref, credit_mapper=None, **options):
        # tools is the host's actual callable registry, never a chat-supplied map.
        reader = tools.get('mcp__codex_app__get_usage_limits')
        capability = (OfficialCapability('get_usage_limits', binding, evidence_ref, lambda: reader({}), credit_mapper)
                      if callable(reader) else None)
        return cls(capability, **options)

    @classmethod
    def from_existing_app_server(cls, authorized_rpc, binding, evidence_ref, credit_mapper=None, **options):
        # The host must already own an authorized session. No process/session is
        # opened here, and no login, account refresh, HTTP request or token read.
        capability = (OfficialCapability('account/rateLimits/read', binding, evidence_ref,
                                        lambda: authorized_rpc('account/rateLimits/read', {}), credit_mapper)
                      if callable(authorized_rpc) else None)
        return cls(capability, **options)

    def _preflight_result(self, capability, binding):
        """Reject invalid host evidence before either entry point calls a reader."""
        valid_binding = (isinstance(binding, UsageBinding) and binding.verified is True
                         and all(isinstance(x,str) and 0 < len(x) <= 256 for x in (binding.owner_ref,binding.account_ref)))
        if not valid_binding:
            return prepare_weekly_usage(None, binding, now=self.clock())
        if (not isinstance(capability, OfficialCapability) or not callable(capability.read)
                or capability.source not in ('get_usage_limits', 'account/rateLimits/read')
                or not isinstance(capability.binding_evidence_ref, str) or not capability.binding_evidence_ref):
            return prepare_weekly_usage(None, binding, now=self.clock())
        if capability.binding != binding:
            return prepare_weekly_usage(None, UsageBinding(binding.owner_ref,binding.account_ref,False), now=self.clock())
        return None

    def _metadata(self, result, capability, started, finished=None):
        result.update(official_source=capability.source, read_started_at=started,
                      read_finished_at=finished, binding_evidence_ref=capability.binding_evidence_ref)
        return result

    def _failure(self, status, capability, started, *, attempted=True):
        return self._metadata({'status': status, 'text': render_weekly_usage(UsageView(status)),
                               'official_call_attempted': attempted}, capability, started, self.clock())

    def _wait_result(self, capability, started, *, attempted, busy=False):
        result = self._failure('requires_async', capability, started, attempted=attempted)
        result.update(adapter_requires_async=True, read_finished_at=None,
                      official_result_pending=attempted, adapter_busy=busy)
        return result

    @staticmethod
    def _same_request(pending, capability, binding, request_ref):
        return (pending.capability is capability and pending.binding == binding
                and pending.request_ref == request_ref)

    def _track_pending(self, capability, binding, request_ref, payload, started, started_monotonic):
        pending = _PendingRead(capability, binding, request_ref, payload, started,
                               started_monotonic, started_monotonic + self.read_timeout_seconds)
        self._pending = pending
        if isinstance(payload, asyncio.Future):
            # Preserve the first observed completion, not a later resume time.
            if payload.done():
                pending.finished = self.clock()
                pending.finished_monotonic = self.monotonic()
            else:
                payload.add_done_callback(lambda _: self._mark_finished(pending))
        return pending

    def _mark_finished(self, pending):
        with self._pending_lock:
            if pending.finished is None:
                pending.finished = self.clock()
                pending.finished_monotonic = self.monotonic()
            if pending.timed_out:
                self._drain_timed_out(pending)

    def _drain_timed_out(self, pending):
        # Called only for this exact completed source. Never cancel, await,
        # schedule on another loop, or release a source which is still running.
        source = pending.payload
        if isinstance(source, asyncio.Future):
            if not source.done():
                return
            try:
                source.result()
            except OfficialReadDenied:
                if not any(item is pending.capability for item in self._late_denied_capabilities):
                    self._late_denied_capabilities.append(pending.capability)
            except BaseException:
                pass  # Observe late failures/cancellation, never log their data.
        elif inspect.isawaitable(source):
            return
        pending.payload = None  # Do not retain the late result or exception.
        if self._pending is pending:
            self._pending = None

    def _elapsed_metadata(self, result, started_monotonic, finished_monotonic=None):
        end = self.monotonic() if finished_monotonic is None else finished_monotonic
        result.update(read_elapsed_seconds=max(0.0, end - started_monotonic),
                      read_timeout_seconds=self.read_timeout_seconds)
        return result

    def _timeout_result(self, pending):
        # A timeout is a terminal observation for this request, not proof that
        # the official operation stopped. Preserve its unresolved source fence.
        result = self._metadata({'status': 'read_timeout',
            'text': render_weekly_usage(UsageView('read_timeout')),
            'official_call_attempted': True, 'read_deadline_expired': True,
            'read_timed_out_at': self.clock(),
            'official_result_pending': pending.finished is None},
            pending.capability, pending.started, None)
        self._elapsed_metadata(result, pending.started_monotonic)
        pending.timed_out = True
        pending.claimed = True
        self._timed_out_reads.append(_TimedOutRead(
            pending.capability, pending.binding, pending.request_ref, dict(result)))
        self._drain_timed_out(pending)
        return result

    def _original_timeout(self, capability, binding, request_ref):
        with self._pending_lock:
            for terminal in self._timed_out_reads:
                if (self._same_request(terminal, capability, binding, request_ref)
                        and self._preflight_result(capability, binding) is None):
                    return dict(terminal.result)
        return None

    def _terminal_gate(self, capability, binding, request_ref):
        with self._pending_lock:
            if any(item is capability for item in self._late_denied_capabilities):
                result = self._failure('permission_denied', capability, self.clock(), attempted=False)
                result['prior_read_permission_denied'] = True
                result['read_finished_at'] = None
                return result
            if len(self._timed_out_reads) >= self.MAX_TIMEOUT_RECORDS:
                # No eviction, reset, fallback or speculative retry on overflow.
                result = self._failure('read_history_full', capability, self.clock(), attempted=False)
                result['read_finished_at'] = None
                return result
        return None

    def _format_completed(self, payload, binding, capability, started, finished):
        if self.capability is not capability:
            return self._context_changed(capability, started)
        # Error envelopes remain failures; only the host can classify an actual
        # permission denial. Never infer authorization from result text.
        value = payload
        for _ in range(4):
            if not isinstance(value, dict):
                break
            if value.get('isError') is True or 'error' in value:
                return self._failure('read_failed', capability, started)
            if 'structuredContent' in value:
                value = value['structuredContent']
            elif 'result' in value:
                value = value['result']
            else:
                break
        credits = None
        if callable(capability.credit_mapper):
            try:
                mapped = capability.credit_mapper(payload)
                credits = mapped if isinstance(mapped, CreditInfo) else None
            except Exception:
                pass  # unknown credits do not erase valid usage windows
        observation = UsageObservation(binding.owner_ref, binding.account_ref, finished,
                                       payload, capability.source, credits)
        # Mapping may take time. Revalidate age/reset against the actual time
        # now, while keeping the original read completion on the observation.
        view = normalize_usage(observation, binding, now=self.clock())
        if self.capability is not capability:
            return self._context_changed(capability, started)
        result = self._metadata({'status': view.status, 'text': render_weekly_usage(view),
                                 'official_call_attempted': True}, capability, started, finished)
        weekly_resets = [window.resets_at for window in view.windows
                         if window.minutes == 10080 and window.resets_at is not None]
        result['weekly_reset_at'] = min(weekly_resets) if weekly_resets else None
        return result

    def revalidate_prepared(self, prepared):
        """Hide expired values at submission; never reread or refresh their age."""
        if prepared.get('status') not in ('reported', 'no_windows'):
            return prepared
        now = self.clock()
        finished = prepared.get('read_finished_at')
        reset = prepared.get('weekly_reset_at')
        try:
            timestamps_valid = all(type(value) in (int, float) and value > 0 and math.isfinite(value)
                                   for value in (now, finished))
        except (ValueError, OverflowError):
            timestamps_valid = False
        if not timestamps_valid:
            return {**prepared, 'status': 'timestamp_invalid',
                    'text': render_weekly_usage(UsageView('timestamp_invalid'))}
        if now < finished or now - finished > DEFAULT_MAX_AGE_SECONDS or (reset is not None and now >= reset):
            return {**prepared, 'status': 'stale', 'text': render_weekly_usage(UsageView('stale'))}
        return prepared

    def _context_changed(self, capability, started):
        result = self._failure('source_unverified', capability, started)
        result['adapter_context_changed'] = True
        return result

    def _pending_context_gate(self, capability, binding, request_ref):
        with self._pending_lock:
            pending = self._pending
            if pending is not None and not self._same_request(pending, capability, binding, request_ref):
                if (pending.timed_out and pending.capability is capability and pending.binding == binding
                        and self._preflight_result(capability, binding) is None):
                    result = self._failure('read_busy', capability, None, attempted=False)
                    result.update(read_finished_at=None, prior_read_pending=True)
                    return result
                return self._wait_result(pending.capability, pending.started, attempted=False, busy=True)
        return None

    def _read_gate(self, capability, binding, request_ref):
        # The caller holds _pending_lock through this gate and read admission.
        # Otherwise another thread could establish a terminal/denial between
        # this check and starting a replacement read.
        original = self._original_timeout(capability, binding, request_ref)
        if original is not None:
            return original
        pending_gate = self._pending_context_gate(capability, binding, request_ref)
        if pending_gate is not None:
            return pending_gate
        rejected = self._preflight_result(capability, binding)
        if rejected is not None:
            return rejected
        terminal = self._terminal_gate(capability, binding, request_ref)
        if terminal is not None:
            return terminal

    def prepare(self, binding, *, request_ref=None):
        with self._pending_lock:
            capability = self.capability
            rejected = self._read_gate(capability, binding, request_ref)
            if rejected is not None:
                return rejected
            if self._pending is not None:
                pending = self._pending
                busy = not self._same_request(pending, capability, binding, request_ref) or pending.claimed
                return self._wait_result(capability, pending.started, attempted=not busy, busy=busy)
            started = self.clock()
            started_monotonic = self.monotonic()
            try:
                payload = capability.read()  # one invocation, no fallback
            except OfficialReadDenied:
                return self._failure('permission_denied', capability, started)
            except Exception:
                return self._failure('read_failed', capability, started)
            if inspect.isawaitable(payload):
                if inspect.iscoroutine(payload) and inspect.getcoroutinestate(payload) == inspect.CORO_CREATED:
                    payload.close()  # native coroutine has not started
                    return self._wait_result(capability, started, attempted=False)
                self._track_pending(capability, binding, request_ref, payload, started, started_monotonic)
                return self._wait_result(capability, started, attempted=True)
            finished = self.clock()
        return self._format_completed(payload, binding, capability, started, finished)

    async def prepare_async(self, binding, *, request_ref=None):
        """Bound the await of a cooperative host reader, without cancelling it.

        The host's callable itself and its event loop must be bounded/schedulable.
        No thread wrapper can make an arbitrary blocking callable interruptible.
        """
        with self._pending_lock:
            capability = self.capability
            rejected = self._read_gate(capability, binding, request_ref)
            if rejected is not None:
                return rejected
            pending = self._pending
            if pending is not None:
                if not self._same_request(pending, capability, binding, request_ref) or pending.claimed:
                    return self._wait_result(capability, pending.started, attempted=False, busy=True)
                started, payload = pending.started, pending.payload
                started_monotonic = pending.started_monotonic
            else:
                started = self.clock()
                started_monotonic = self.monotonic()
                try:
                    payload = capability.read()
                except OfficialReadDenied:
                    return self._elapsed_metadata(self._failure('permission_denied', capability, started), started_monotonic)
                except Exception:
                    return self._elapsed_metadata(self._failure('read_failed', capability, started), started_monotonic)
            if inspect.isawaitable(payload):
                if pending is None:
                    pending = self._track_pending(capability, binding, request_ref, payload, started, started_monotonic)
                try:
                    pending.payload = asyncio.ensure_future(payload)
                    if pending.payload.get_loop() is not asyncio.get_running_loop():
                        return self._wait_result(capability, started, attempted=True)
                    if pending.finished is None:
                        if pending.payload.done():
                            self._mark_finished(pending)
                        else:
                            pending.payload.add_done_callback(lambda _: self._mark_finished(pending))
                except Exception:
                    # An incompatible event loop is a host wiring problem, not
                    # a reason to discard or restart the original read.
                    return self._wait_result(capability, started, attempted=True)
                pending.claimed = True
            else:
                finished, finished_monotonic = self.clock(), self.monotonic()
                if finished_monotonic - started_monotonic >= self.read_timeout_seconds:
                    pending = self._track_pending(capability, binding, request_ref, payload, started, started_monotonic)
                    pending.finished, pending.finished_monotonic = finished, finished_monotonic
                    return self._timeout_result(pending)
                return self._elapsed_metadata(self._format_completed(payload, binding, capability, started, finished),
                                              started_monotonic, finished_monotonic)
        try:
            # asyncio.wait does not cancel or await cancellation of the source;
            # a cancellation-resistant reader cannot extend this wait's budget.
            remaining = max(0.0, pending.deadline - self.monotonic())
            if not pending.payload.done() and remaining:
                await asyncio.wait((pending.payload,), timeout=remaining)
            with self._pending_lock:
                if pending.payload.done():
                    self._mark_finished(pending)
                if (pending.finished_monotonic is None
                        or pending.finished_monotonic > pending.deadline):
                    return self._timeout_result(pending)
            payload = pending.payload.result()
        except asyncio.CancelledError:
            if pending.payload.cancelled():
                # A cancelled source has no result to resume. It must not keep
                # all future requests behind an impossible pending read.
                with self._pending_lock:
                    if self._pending is pending:
                        self._pending = None
                task = asyncio.current_task()
                if getattr(task, 'cancelling', lambda: 0)():
                    raise
                return self._elapsed_metadata(self._failure('read_failed', capability, started),
                                              started_monotonic, pending.finished_monotonic)
            # Cancellation of this waiter must not cancel an already-started
            # official read. Only this same request may resume the original.
            with self._pending_lock:
                pending.claimed = False
            raise
        except OfficialReadDenied:
            result = self._failure('permission_denied', capability, started)
        except Exception:
            result = self._failure('read_failed', capability, started)
        else:
            finished = pending.finished if pending.finished is not None else self.clock()
            result = self._format_completed(payload, binding, capability, started, finished)
        with self._pending_lock:
            if self._pending is pending:
                self._pending = None
        return self._elapsed_metadata(result, started_monotonic, pending.finished_monotonic)
