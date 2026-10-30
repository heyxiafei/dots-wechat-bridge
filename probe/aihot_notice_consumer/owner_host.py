"""Offline integration shim for an EXISTING owner-only WeixinStore.

This code is not an installed host or proof that production integration works.
Bridge Work must verify the exact original local queue/status implementations,
their complete commit bounds, and every successful bind/rebind writer before
passing local_commit_verified=True. A valid SQLite busy_timeout bounds some
SQLite waits only; it does NOT establish that proof. The default is disabled.

The existing binding writer MUST call binding_changed() while already holding
the original RLock, on every successful bind/rebind (including same-owner ABA).
No identity hash can replace this per-worker UUID plus in-memory epoch.

The five-second deadline is a SOFT admission/check boundary, not interruption or a
hard execution limit. An overrun preserves the original receipt/send_id, trips
the host contract off, and never starts a replacement send. Existing committed
notifications still have no cancel/expiry guarantee and may send after a later
context/auth recovery. This shim never calls a network client or sender loop.
"""

import contextlib
import hashlib
import json
import math
import re
import threading
import time
import uuid


IDENTITY_QUERY = 'SELECT id,owner,status FROM weixin_accounts LIMIT 1'
SOFT_DEADLINE_SECONDS = 5.0
_RLOCK_TYPE = type(threading.RLock())
_PIN = re.compile(r'[0-9a-f]{64}')
_SEND_ID = re.compile(r'notice_[0-9a-f]{32}')


class OwnerHostError(RuntimeError):
    """Fixed diagnostic category; never includes an underlying exception."""

    def __init__(self, reason, *, outcome='blocked', send_id=None):
        super().__init__(reason)
        self.reason = reason
        self.outcome = outcome
        self.send_id = send_id
        self.resend_allowed = False


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


class ExistingOwnerHost:
    def __init__(self, existing_store, local_commit_verified=False,
                 monotonic=time.monotonic, instance_id=None):
        if type(local_commit_verified) is not bool:
            raise ValueError('local_commit_verified_must_be_bool')
        if not callable(monotonic):
            raise ValueError('monotonic_required')
        if instance_id is None:
            instance_id = uuid.uuid4()
        try:
            worker_id = uuid.UUID(str(instance_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('instance_id_must_be_uuid') from None
        if worker_id.int == 0:
            raise ValueError('instance_id_must_be_nonzero_uuid')
        self._store = existing_store
        self._probe = getattr(existing_store, 'probe', None)
        self._lock = getattr(existing_store, 'lock', None)
        self._db = getattr(existing_store, 'db', None)
        self._queue = getattr(existing_store, 'queue_notification', None)
        self._status = getattr(existing_store, 'send_status', None)
        self._verified = local_commit_verified
        self._monotonic = monotonic
        self._instance_id = worker_id.hex
        self._epoch = 0
        self._approved_pin = None
        self._faulted = False
        self._last_monotonic = None
        if not self._structure_valid():
            raise ValueError('existing_store_contract_invalid')
        if not self._lock.acquire(False):
            raise BlockingIOError('owner_lock_busy')
        try:
            if not self._busy_timeout_valid():
                raise ValueError('sqlite_busy_timeout_out_of_range')
        finally:
            self._lock.release()

    @property
    def contract(self):
        # Return a fresh dict: callers cannot flip the internal proof by editing
        # an advertised contract. Only external reviewed Work can supply proof.
        return {'version': 1, 'local_only': True,
                'bounded_commit': self._verified is True and not self._faulted}

    @property
    def approved_owner_pin(self):
        """Cached noncredential hash only; no database or AIHOT lock access.

        Coordinators compare this while holding the original owner fence. Once
        approved it cannot be replaced with a different owner's pin.
        """
        return self._approved_pin

    def _structure_valid(self):
        return (self._probe is not None and isinstance(self._lock, _RLOCK_TYPE)
                and getattr(self._store, 'probe', None) is self._probe
                and getattr(self._probe, 'lock', None) is self._lock
                and getattr(self._store, 'lock', None) is self._lock
                and self._db is not None
                and getattr(self._probe, 'db', None) is self._db
                and getattr(self._store, 'db', None) is self._db
                and callable(self._queue) and callable(self._status)
                and getattr(self._store, 'queue_notification', None) == self._queue
                and getattr(self._store, 'send_status', None) == self._status)

    def _generation(self):
        return self._instance_id + ':' + str(self._epoch)

    def _busy_timeout_valid(self):
        try:
            row = self._db.execute('PRAGMA busy_timeout').fetchone()
            return row is not None and type(row[0]) is int and 1 <= row[0] <= 5000
        except Exception:
            return False

    def _identity_locked(self):
        """Only the three noncredential columns; never calls store.account()."""
        if not self._structure_valid() or not self._busy_timeout_valid():
            self._faulted = True
            return None, False
        try:
            row = self._db.execute(IDENTITY_QUERY).fetchone()
            if row is None:
                return None, False
            account_id, owner, status = row[0], row[1], row[2]
            if (not all(isinstance(x, str) and 0 < len(x) <= 8192
                        for x in (account_id, owner)) or not isinstance(status, str)):
                return None, False
            raw = json.dumps([account_id, owner], ensure_ascii=True,
                             separators=(',', ':')).encode('utf-8')
            pin = hashlib.sha256(b'aihot-owner-pin-v1\0' + raw).hexdigest()
            return pin, status == 'ready'
        except Exception:
            # Storage errors must not reveal account data or exception bodies.
            return None, False

    def _ready_locked(self, expected_generation):
        pin, bound_ready = self._identity_locked()
        return (bound_ready and self._approved_pin is not None
                and pin == self._approved_pin
                and expected_generation == self._generation()
                and self.contract['bounded_commit'] is True)

    def sample_binding(self):
        if not self._lock.acquire(False):
            raise BlockingIOError('owner_lock_busy')
        try:
            pin, bound_ready = self._identity_locked()
            return {'owner_pin': pin, 'generation': self._generation(),
                    'bound_ready': bound_ready is True,
                    'ready': (bound_ready and self._approved_pin is not None
                              and pin == self._approved_pin
                              and self.contract['bounded_commit'] is True)}
        finally:
            self._lock.release()

    def approve_owner_pin(self, pin):
        if not isinstance(pin, str) or _PIN.fullmatch(pin) is None:
            return False
        if not self._lock.acquire(False):
            raise BlockingIOError('owner_lock_busy')
        try:
            current, bound_ready = self._identity_locked()
            if not bound_ready or current != pin:
                return False
            if self._approved_pin is not None and self._approved_pin != pin:
                return False
            self._approved_pin = pin
            return True
        finally:
            self._lock.release()

    def binding_changed(self):
        """Work calls only from a successful bind/rebind's original RLock.

        No acquisition, SQL, filesystem access, monotonic read, or AIHOT lock.
        Merely sampling present identity cannot detect an unobserved ABA.
        """
        if not self._lock._is_owned():
            raise OwnerHostError('binding_change_requires_original_lock')
        if not self._structure_valid():
            self._faulted = True
            raise OwnerHostError('existing_store_contract_changed')
        self._epoch += 1

    def _clock(self):
        try:
            now = self._monotonic()
        except Exception:
            self._faulted = True
            raise OwnerHostError('monotonic_unavailable') from None
        if not _finite(now):
            self._faulted = True
            raise OwnerHostError('monotonic_invalid')
        if self._last_monotonic is not None and now < self._last_monotonic:
            self._faulted = True
            raise OwnerHostError('monotonic_reversed')
        self._last_monotonic = now
        return now

    @contextlib.contextmanager
    def owner_fence(self, expected_generation):
        if not self._lock.acquire(False):
            raise BlockingIOError('owner_lock_busy')
        session = None
        try:
            start = self._clock()
            session = _OwnerSession(self, expected_generation, start)
            yield session
        finally:
            if session is not None:
                session._active = False
            self._lock.release()


class _OwnerSession:
    def __init__(self, host, expected_generation, start):
        self._host = host
        self._expected_generation = expected_generation
        self._start = start
        self._deadline = start + SOFT_DEADLINE_SECONDS
        self._thread = threading.get_ident()
        self._active = True
        self._call_started = False

    def _held(self):
        return (self._active and threading.get_ident() == self._thread
                and self._host._lock._is_owned())

    @property
    def generation(self):
        return self._host._generation() if self._held() else None

    @property
    def ready(self):
        if not self._held():
            return False
        try:
            now = self._host._clock()
            if not self._start <= now < self._deadline:
                return False
            if not self._host._ready_locked(self._expected_generation):
                return False
            return self._start <= self._host._clock() < self._deadline
        except OwnerHostError:
            return False

    def _clock_for_call(self, send_id):
        try:
            return self._host._clock()
        except OwnerHostError as error:
            raise OwnerHostError(error.reason, send_id=send_id) from None

    def _before_call(self, send_id):
        if not self._held():
            raise OwnerHostError('owner_session_inactive', send_id=send_id)
        if self._call_started:
            raise OwnerHostError('one_local_call_per_fence', send_id=send_id)
        now = self._clock_for_call(send_id)
        if now < self._start:
            self._host._faulted = True
            raise OwnerHostError('monotonic_reversed', send_id=send_id)
        if now >= self._deadline:
            raise OwnerHostError('owner_deadline_elapsed', send_id=send_id)
        if not self._host._ready_locked(self._expected_generation):
            raise OwnerHostError('owner_binding_not_ready', send_id=send_id)
        now = self._clock_for_call(send_id)
        if now < self._start or now >= self._deadline:
            raise OwnerHostError('owner_deadline_elapsed', send_id=send_id)
        self._call_started = True

    def _call(self, method, args, send_id=None):
        self._before_call(send_id)
        try:
            receipt = method(*args)
        except Exception:
            self._host._faulted = True
            raise OwnerHostError('local_call_unknown', outcome='unknown',
                                 send_id=send_id) from None
        if not isinstance(receipt, dict):
            self._host._faulted = True
            raise OwnerHostError('local_receipt_unknown', outcome='unknown',
                                 send_id=send_id)
        try:
            end = self._host._clock()
            overrun = not self._start <= end < self._deadline
        except OwnerHostError:
            overrun = True
        changed = self._host._generation() != self._expected_generation
        if overrun or changed:
            self._host._faulted = True
            result = dict(receipt)  # Never discard an original known send_id.
            if overrun:
                result['host_overrun'] = True
            if changed:
                result['host_binding_changed'] = True
            return result
        return receipt

    def queue_notification(self, notification_id, text):
        if not isinstance(notification_id, str) or not 0 < len(notification_id) <= 128:
            raise OwnerHostError('invalid_notification_identity')
        try:
            valid_text = isinstance(text, str) and bool(text.strip()) and len(text.encode('utf-8')) <= 8192
        except UnicodeError:
            valid_text = False
        if not valid_text:
            raise OwnerHostError('reply_text_must_be_1_to_8192_bytes')
        return self._call(self._host._queue, (notification_id, text))

    def send_status(self, send_id):
        if not isinstance(send_id, str) or _SEND_ID.fullmatch(send_id) is None:
            raise OwnerHostError('invalid_notification_send_id')
        return self._call(self._host._status, (send_id,), send_id=send_id)
