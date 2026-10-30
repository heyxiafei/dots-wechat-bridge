"""Hook for the EXISTING worker, outside all host locks; no new worker/service.

Root Bridge Work owns installation and callsites. Construction never arms.
Successful bind/rebind must call binding_changed while holding its original
RLock; that callback never takes AIHOT locks. A continuously enabled same-owner
maintenance restart may explicitly resume its existing cutoff; off/on establishes
a new future-only cutoff. No Gmail or notification test is included.
"""
from pathlib import Path
import hashlib
import json
import threading
import time

from .operator_config import require_operator_config
from .control import Controller, read_control
from .owner_host import ExistingOwnerHost
from .relay import finite, key_for
from .wechat_adapter import existing_monitor_lock
from .store_helpers import lock
from .store_helpers import Invalid
from . import auto_resume

VERSION = 'aihot-wechat-b1'


class BridgeHook:
    def __init__(self, existing_store, *, local_commit_verified=False, data_dir=None,
                 now_callable=time.time, monotonic=time.monotonic, operator_config=None):
        self.operator = require_operator_config(operator_config)
        if data_dir is None:
            raise ValueError('aihot_data_directory_required')
        self.host = ExistingOwnerHost(existing_store, local_commit_verified=local_commit_verified,
                                      monotonic=monotonic)
        self.controller = Controller(data_dir, self.host, now_callable, operator_config=self.operator)
        self.clock, self.monotonic = now_callable, monotonic
        self._boot_wall, self._boot_mono = now_callable(), monotonic()
        # All post-restart resumes, including a later explicit one, remain future-only.
        self.controller.resume_after = self._boot_wall
        self._auto_guard_active = False
        self._binding_invalidated = False
        self._startup_guard_ready = False
        self._old_notice_ids = frozenset()
        self._tick_lock = threading.Lock()
        self._next_tick = 0.0
        self._cached = dict(version=VERSION, installed=True, enabled=False, email_enabled=False,
                            last_tick_at=None, last_result='not_ticked', requires_rearm=True,
                            blocked_by_activation_count=0, blocked_by_activation=[])

    def _cache_activation_diagnostics(self, result):
        """Keep only bounded public source IDs, stages and numeric timestamps."""
        count, blocked = result.get('blocked_by_activation_count', 0), result.get('blocked_by_activation', [])
        safe = []
        try:
            if type(count) is not int or not 0 <= count <= 10000 or not isinstance(blocked, list) or len(blocked) > 5 or count < len(blocked):
                raise ValueError('invalid_activation_diagnostics')
            for item in blocked:
                key_for(item['event_id'], item['stage'])
                if (not finite(item['source_at']) or not finite(item['activation_at'])
                        or item['source_at'] > item['activation_at']):
                    raise ValueError('invalid_activation_diagnostics')
                safe.append({key: item[key] for key in ('event_id', 'stage', 'source_at', 'activation_at')})
        except (Invalid, ValueError, TypeError, KeyError):
            count, safe = 0, []
        self._cached.update(blocked_by_activation_count=count, blocked_by_activation=safe)

    def arm(self, expected_owner_pin=None, **options):
        result = self.controller.arm(expected_owner_pin=expected_owner_pin, **options)
        self._cached['last_arm_result'] = result['result']
        if result['result'] in ('armed_future_only', 'already_armed', 'maintenance_resumed'):
            self._binding_invalidated = False
            self._cached.update(enabled=True, requires_rearm=False,
                                consumer_activation_at=result['consumer_activation_at'])
        return result

    def off(self):
        result = self.controller.off()
        if result['result'] == 'consumer_off':
            self._auto_guard_active = False
            self._cached.update(enabled=False, requires_rearm=True)
            self._cache_activation_diagnostics({})
        return result

    def binding_changed(self):
        # Called within the EXISTING binding lock; only host epoch/memory change.
        self.host.binding_changed()
        self._binding_invalidated = True
        self.host._store.aihot_hold_all_notifications = True
        self._cached.update(enabled=False, requires_rearm=True)
        self._cache_activation_diagnostics({})

    def _auto_resume(self):
        """Try only a reviewed opt-in same-binding maintenance resume.

        The guard is checked again inside the existing monitor/dual/owner locks.
        The email consumer is deliberately not armed by this method.
        """
        now, mono = self.clock(), self.monotonic()
        if (not all(finite(x) for x in (now, mono, self._boot_wall, self._boot_mono))
                or now < self._boot_wall or mono < self._boot_mono
                or abs((now-self._boot_wall)-(mono-self._boot_mono)) > 120):
            return {'result':'auto_clock_unverifiable'}
        try:
            config = read_control(self.controller.data)
            if config['enabled'] is not True:
                return {'result':'disabled'}
            sample = self.host.sample_binding()
            same_generation = sample.get('generation') == config['expected_generation']
            if same_generation and not self._auto_guard_active:
                return {'result':'already_current'}
            deadline = auto_resume.read_hard_stop()
            with self.host._lock:
                problem = auto_resume.check(self.host._db, config, now, deadline, self.operator)
            if problem != 'allowed':
                return {'result':problem}
            if same_generation:
                return {'result':'already_current'}
            # This fence is installed before arm; it remains even if a later
            # explicit same-process maintenance resume is needed.
            self.controller.resume_after = self._boot_wall
            def guarded(current, checked_at):
                current_deadline = auto_resume.read_hard_stop()
                if auto_resume.check(self.host._db, current, checked_at, current_deadline, self.operator) != 'allowed':
                    return False
                # Under the existing source locks, fence off everything already
                # observed at resume; retain the original authorization cutoff.
                observed = self.controller.relay._load(checked_at).get('checked_at')
                if not finite(observed):
                    return False
                self.controller.resume_after = max(self._boot_wall, observed)
                return True
            result = self.arm(expected_owner_pin=config['owner_pin'], resume_only=True,
                              expected_config=config, resume_guard=guarded)
            if result['result'] == 'maintenance_resumed':
                self._auto_guard_active = True
            return result
        except Exception:
            return {'result':'auto_authorization_unverifiable'}

    def sender_guard(self):
        """Return old AIHot notice IDs; never send/requeue or edit their state.

        Until the original ledger can be safely read, hold notification sends
        (ordinary owner replies remain available). Do not refresh by dropping IDs.
        """
        if self._startup_guard_ready:
            return self._old_notice_ids, False
        try:
            with existing_monitor_lock(self.controller.relay.path) as monitor:
                if not monitor: return frozenset(), True
                with lock(self.controller.relay.path) as dual:
                    if not dual: return frozenset(), True
                    state = self.controller.relay._load(self.clock())
                    with self.host._lock:
                        accounts = self.host._db.execute('SELECT id FROM weixin_accounts LIMIT 2').fetchall()
                        if len(accounts) != 1 or not finite(self._boot_wall):return frozenset(), True
                        account = accounts[0]['id']
                        if not isinstance(account,str) or not account:return frozenset(), True
                        queued_before_boot = {row['message_id'] for row in self.host._db.execute(
                            "SELECT message_id FROM weixin_outbox WHERE kind='notification' AND created_at<=?", (self._boot_wall,))}
                        # If an older queued notification has no surviving
                        # source correlation, hold it rather than guess it safe.
                        old = set(queued_before_boot)
                        for item in state['entries'].values():
                            request_id=item['channels']['wechat']['request_id']
                            raw=json.dumps([account,request_id],sort_keys=True,separators=(',',':'),ensure_ascii=False)
                            notice_id='notice_'+hashlib.sha256(raw.encode()).hexdigest()[:32]
                            # A committed outbox predating startup is historical
                            # even if source time is skewed into the future.
                            if item['intent']['source_at'] <= self._boot_wall or notice_id in queued_before_boot:
                                old.add(notice_id)
                        self._old_notice_ids=frozenset(old)
                        self._startup_guard_ready=True
                        return self._old_notice_ids, False
        except Exception:
            return frozenset(), True

    def tick(self):
        if not self._tick_lock.acquire(False):
            return {'result': 'busy'}
        try:
            now_mono = self.monotonic()
            if not finite(now_mono) or now_mono < 0:
                raise ValueError('invalid_monotonic')
            if now_mono < self._next_tick:
                return {'result': 'throttled'}
            self._next_tick = now_mono + 10
            auto = self._auto_resume()
            self._cached['last_auto_resume_result'] = auto['result']
            self._cached['auto_guard_blocked'] = (self._binding_invalidated or
                self._auto_guard_active and auto['result'] not in ('already_current','maintenance_resumed'))
            if self._cached['auto_guard_blocked']:
                self._cached.update(last_tick_at=self.clock(), last_result=auto['result'], enabled=False, requires_rearm=True)
                return {'result':auto['result']}
            result = self.controller.step()
            self._cache_activation_diagnostics(result)
            try:
                config = read_control(self.controller.data)
                enabled = config['enabled'] is True
            except Exception:
                enabled = False
            blocked = (result['result'] in ('owner_not_ready', 'owner_changed_requires_review',
                                           'host_contract_missing', 'host_contract_failed')
                       or self.host.contract.get('bounded_commit') is not True)
            # A fully checked adapter pass proves that the current owner and
            # generation still match; clear a transient startup/lock failure.
            # Ambiguous or failed steps must never clear the cached fence.
            rearm = self._cached['requires_rearm'] or blocked
            if enabled and not blocked and result['result'] in ('idle', 'blocked_by_activation'):
                rearm = False
            self._cached.update(last_tick_at=self.clock(), last_result=result['result'],
                                enabled=enabled and not blocked,
                                requires_rearm=rearm)
            # Only fixed result categories, original IDs and counters may be
            # logged by the caller; never copy account rows or message text.
            return result
        except Exception:
            self._cached.update(enabled=False, requires_rearm=True, last_result='hook_local_error')
            self._cache_activation_diagnostics({})
            return {'result': 'hook_local_error'}
        finally:
            self._tick_lock.release()

    def status_snapshot(self):
        """Safe cached evidence: no files/DB/locks, even under a host status lock."""
        return {**self._cached,
                'blocked_by_activation': [dict(item) for item in self._cached['blocked_by_activation']]}
