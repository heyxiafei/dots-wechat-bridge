"""Offline consumer v2; disabled until a trusted LOCAL owner-fenced host exists.

No live host currently implements this contract. An ordinary two-argument store
queue is deliberately insufficient. Contract v1 requires owner_fence() to pin
the expected owner generation and provide a bounded local transaction session;
queue_notification and send_status must not do network I/O or choose a receiver.
The host must enforce the transaction bound: an unsafe timeout thread cannot
cancel a commit. FakeHost tests do not establish support in the actual host.
"""
import contextlib
import fcntl
import os
from pathlib import Path
import re
import stat
import threading
import time

from .relay import FRESHNESS, finite, validate_intent
from .store_helpers import Invalid, lock, strict_json


@contextlib.contextmanager
def existing_monitor_lock(data):
    """Nonblocking: never create, replace or chmod the producer monitor lock."""
    parent = Path(data).lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise Invalid('private_data_directory')
    fd = os.open(Path(data) / 'monitor.lock', os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise Invalid('private_monitor_lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def private_json(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4 * 1024 * 1024:
        raise Invalid('private_control_file')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'r') as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise Invalid('control_replaced')
        return strict_json(handle.read(4 * 1024 * 1024 + 1))


class WechatAdapter:
    def __init__(self, relay, host=None, enabled=False, *, expected_generation=None,
                 consumer_activation_at=None, now_callable=time.time, resume_after=None):
        if type(enabled) is not bool:
            raise ValueError('enabled must be a boolean')
        self.relay, self.host, self.enabled = relay, host, enabled
        self.expected_generation = expected_generation
        self.consumer_activation_at = consumer_activation_at
        self.resume_after = resume_after
        self.clock = now_callable
        self._lock = threading.Lock()
        self._cursor = ''

    def _configured(self):
        contract = getattr(self.host, 'contract', None)
        if (not isinstance(contract, dict) or type(contract.get('version')) is not int
                or contract['version'] != 1 or contract.get('local_only') is not True
                or contract.get('bounded_commit') is not True
                or not callable(getattr(self.host, 'owner_fence', None))):
            return 'host_contract_missing'
        generation = self.expected_generation
        if (not isinstance(generation, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', generation)
                or not finite(self.consumer_activation_at) or not callable(self.clock)
                or self.resume_after is not None and not finite(self.resume_after)):
            return 'config_missing'
        return None

    def _session_ready(self, session, generation):
        return (getattr(session, 'ready', None) is True
                and getattr(session, 'generation', None) == generation
                and callable(getattr(session, 'queue_notification', None))
                and callable(getattr(session, 'send_status', None)))

    def _controls_enabled(self):
        data = self.relay.path
        control = private_json(data / 'control.json')
        dual = private_json(data / 'dual-control.json')
        state = private_json(data / 'state.json')
        return (isinstance(control, dict) and set(control) == {'enabled', 'mode'}
                and control['enabled'] is True and control['mode'] == 'monitor-only'
                and isinstance(dual, dict) and set(dual) == {'capture_enabled'}
                and dual['capture_enabled'] is True and isinstance(state, dict)
                and state.get('mode') == 'monitor-only' and state.get('dispatch_enabled') is False
                and state.get('route_stopped') is False)

    def _host_calls_allowed(self, now, generation, activation, session):
        return not (self.enabled is not True or self._configured() is not None
                or self.expected_generation != generation or self.consumer_activation_at != activation
                or not self._session_ready(session, generation) or not finite(now)
                or not self._controls_enabled())

    def _queue_allowed(self, key, now, generation, activation, session, selected_intent):
        if not self._host_calls_allowed(now, generation, activation, session):
            return False
        state = self.relay._load(now)
        item = state['entries'].get(key)
        current = state['snapshot'].get(key)
        if not item or not current:
            return False
        original = validate_intent(item['intent'])
        current = validate_intent(current)
        fields = ('event_id', 'stage', 'source_at', 'link', 'text', 'display')
        if any(current.get(field) != original.get(field) or original.get(field) != selected_intent.get(field) for field in fields):
            return False
        terminal = state['terminal'].get(original['event_id'])
        return (state['healthy'] is True and state['baseline_complete'] is True
                and finite(state['checked_at']) and state['checked_at'] <= now + 120
                and now - state['checked_at'] <= FRESHNESS
                and activation < original['source_at'] <= state['checked_at']
                and (self.resume_after is None or original['source_at'] > self.resume_after)
                and selected_intent['eligible'] is True and original['eligible'] is True and current['eligible'] is True
                and now < min(selected_intent['expires_at'], original['expires_at'], current['expires_at'])
                and terminal not in ('withdrawn', 'unknown')
                and not (terminal == 'confirmed' and original['stage'] == 'announced'))

    def _record(self, intent, receipt, now):
        try:
            return self.relay._record_locked(intent['event_id'], intent['stage'], 'wechat', receipt, now)
        except Exception:
            # The durable claim remains the recovery boundary. Never requeue
            # after a receipt failure, and never expose arbitrary error text.
            return {'result': 'record_failed', 'query_only': True}

    @staticmethod
    def _activation_diagnostics(state, activation):
        """Describe pending exclusions only; attempted sends remain query-only."""
        blocked = []
        count = 0
        for key in sorted(state['entries']):
            item = state['entries'][key]
            intent = item['intent']
            if (item['channels']['wechat']['state'] == 'pending'
                    and intent['source_at'] <= activation):
                count += 1
                if len(blocked) < 5:
                    blocked.append({'event_id': intent['event_id'], 'stage': intent['stage'],
                                    'source_at': intent['source_at'], 'activation_at': activation})
        return {'blocked_by_activation_count': count, 'blocked_by_activation': blocked}

    def _step_fenced(self, session, generation, activation, key, selected_intent, selected_channel):
        now = self.clock()
        if not finite(now) or not self._session_ready(session, generation):
            return {'result': 'owner_not_ready'}
        controls_enabled = self._controls_enabled()
        state = self.relay._load(now)
        item = state['entries'].get(key)
        if item is None:
            return {'result': 'source_blocked'}
        intent, channel = item['intent'], item['channels']['wechat']
        if (any(intent.get(field) != selected_intent.get(field) for field in ('event_id', 'stage', 'source_at', 'link', 'text', 'display'))
                or channel['request_id'] != selected_channel['request_id']
                or channel['provider_id'] != selected_channel['provider_id']):
            return {'result': 'source_blocked'}
        if channel['state'] != 'pending':
            if channel['provider_id'] is None:
                return {'result': 'unknown_without_send_id', 'request_id': channel['request_id'], 'query_only': True}
            if not controls_enabled or not self._host_calls_allowed(self.clock(), generation, activation, session):
                return {'result': 'source_blocked'}
            try:
                receipt = session.send_status(channel['provider_id'])
            except Exception:
                return {'result': 'query_failed', 'provider_id': channel['provider_id'], 'query_only': True}
            return self._record(intent, receipt, self.clock())
        if not controls_enabled:
            return {'result': 'source_blocked'}
        if intent['source_at'] <= activation:
            return {'result': 'blocked_by_activation'}
        if not self._queue_allowed(key, now, generation, activation, session, selected_intent):
            return {'result': 'source_blocked'}
        claim = self.relay._claim_locked(intent['event_id'], intent['stage'], 'wechat', now)
        if claim['result'] != 'prepared':
            return claim
        if not self._queue_allowed(key, self.clock(), generation, activation, session, selected_intent):
            return {'result': 'commit_blocked', 'query_only': True, 'request_id': claim['request_id']}
        try:
            receipt = session.queue_notification(claim['payload']['notification_id'], claim['payload']['text'])
        except Exception:
            return self._record(intent, {}, self.clock())
        return self._record(intent, receipt, self.clock())

    def step(self, now=None):
        if self.enabled is not True:
            return {'result': 'disabled'}
        try:
            problem = self._configured()
        except Exception:
            return {'result': 'host_contract_failed'}
        if problem is not None:
            return {'result': problem}
        if now is not None and not finite(now):
            return {'result': 'config_missing'}
        if not self._lock.acquire(False):
            return {'result': 'busy'}
        try:
            generation, activation = self.expected_generation, self.consumer_activation_at
            try:
                with existing_monitor_lock(self.relay.path) as monitor_acquired:
                    if not monitor_acquired:
                        return {'result': 'monitor_lock_busy'}
                    with lock(self.relay.path) as dual_acquired:
                        if not dual_acquired:
                            return {'result': 'dual_lock_busy'}
                        selected_at = self.clock()
                        if not finite(selected_at):
                            return {'result': 'config_missing'}
                        selected_state = self.relay._load(selected_at)
                        diagnostics = self._activation_diagnostics(selected_state, activation)
                        active = sorted(key for key, item in selected_state['entries'].items()
                                        if item['channels']['wechat']['state'] in ('pending', 'attempting', 'waiting', 'uncertain'))
                        if not active:
                            return {'result': 'idle', **diagnostics}
                        key = next((key for key in active if key > self._cursor), active[0])
                        self._cursor = key
                        selected = selected_state['entries'][key]
                        selected_intent = dict(selected['intent'])
                        selected_channel = dict(selected['channels']['wechat'])
                        entered_fence = False
                        try:
                            with self.host.owner_fence(generation) as session:
                                entered_fence = True
                                return {**self._step_fenced(session, generation, activation, key,
                                                          selected_intent, selected_channel), **diagnostics}
                        except BlockingIOError:
                            return {'result': 'host_contract_failed' if entered_fence else 'host_lock_busy',
                                    **diagnostics}
            except (Invalid, OSError, ValueError, TypeError, KeyError):
                return {'result': 'local_or_source_blocked'}
            except Exception:
                return {'result': 'host_contract_failed'}
        finally:
            self._lock.release()
