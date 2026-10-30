"""Fixed AIHOT owner-only control. No service control, network or credentials.

All changes coordinate the original monitor and dual locks. Arm is an explicit
host action after its identity/isolation proof, never an automatic constructor
or restart action. Existing ledger, claims, IDs and email state are untouched.
"""
import re
import threading
import time

from .relay import FRESHNESS, Relay, finite
from .store_helpers import Invalid, atomic_json, lock
from .wechat_adapter import WechatAdapter, existing_monitor_lock, private_json

CONTROL_NAME = 'wechat-consumer.json'
FIELDS = {'version', 'enabled', 'expected_generation', 'owner_pin',
          'consumer_activation_at', 'revision', 'email_enabled'}


def _generation_parts(value):
    if not isinstance(value, str):
        return None
    matched = re.fullmatch(r'([0-9a-f]{32}):(0|[1-9][0-9]*)', value)
    if matched is None or int(matched[1], 16) == 0:
        return None
    return matched[1], matched[2]


def authorized_cutoff_reason(generation, activation, now):
    """Validate a retained authorization; never repair or invent its cutoff."""
    if not finite(now) or not finite(activation) or not 0 < activation <= now:
        return 'resume_cutoff_invalid'
    if _generation_parts(generation) is None:
        return 'resume_generation_invalid'
    return None


def maintenance_resume_reason(old_generation, current_generation, activation, now):
    """Only an explicit, same-route process restart may retain a valid cutoff.

    This is not proof of no offline rebinding. The unique maintenance writer
    must establish that separately before invoking the existing arm operation.
    Same-worker epoch changes and a new worker that has rebound fail closed.
    """
    reason = authorized_cutoff_reason(old_generation, activation, now)
    current = _generation_parts(current_generation)
    if reason is not None:
        return reason
    if current is None:
        return 'resume_generation_invalid'
    previous = _generation_parts(old_generation)
    if previous[0] == current[0] or current[1] != '0':
        return 'resume_binding_changed'
    return None


def disabled_control():
    return dict(version=1, enabled=False, expected_generation=None, owner_pin=None,
                consumer_activation_at=None, revision=0, email_enabled=False)


def read_control(data):
    value = private_json(data / CONTROL_NAME)
    if (not isinstance(value, dict) or set(value) != FIELDS or type(value['version']) is not int
            or value['version'] != 1 or type(value['enabled']) is not bool
            or value['email_enabled'] is not False or type(value['revision']) is not int
            or value['revision'] < 0):
        raise Invalid('wechat_consumer_control')
    for key in ('expected_generation', 'owner_pin'):
        if value[key] is not None and (not isinstance(value[key], str) or not 0 < len(value[key]) <= 160):
            raise Invalid('wechat_consumer_identity')
    if value['consumer_activation_at'] is not None and not finite(value['consumer_activation_at']):
        raise Invalid('wechat_consumer_cutoff')
    if value['enabled'] and any(value[k] is None for k in ('expected_generation', 'owner_pin', 'consumer_activation_at')):
        raise Invalid('wechat_consumer_incomplete')
    return value


class ConfiguredAdapter(WechatAdapter):
    def __init__(self, relay, host, config, clock, resume_after=None):
        self._expected_config = dict(config)
        super().__init__(relay, host, enabled=config['enabled'],
            expected_generation=config['expected_generation'],
            consumer_activation_at=config['consumer_activation_at'], now_callable=clock, resume_after=resume_after)

    def _configured(self):
        problem = super()._configured()
        if problem is not None:
            return problem
        # Called again under both AIHOT locks immediately before every host use.
        if read_control(self.relay.path) != self._expected_config:
            return 'consumer_configuration_changed'
        if getattr(self.host, 'approved_owner_pin', None) != self._expected_config['owner_pin']:
            return 'consumer_owner_pin_changed'
        return None


class Controller:
    def __init__(self, data, host, now_callable=time.time, *, operator_config=None):
        self.data, self.host, self.clock = data, host, now_callable
        self.relay = Relay(data, now_callable=now_callable, operator_config=operator_config)
        self._adapter = None
        self._revision = None
        self.resume_after = None
        self._mutex = threading.Lock()

    def _source_enabled(self, now):
        control = private_json(self.data / 'control.json')
        dual = private_json(self.data / 'dual-control.json')
        state = private_json(self.data / 'state.json')
        if (not isinstance(control, dict) or set(control) != {'enabled', 'mode'}
                or control['enabled'] is not True or control['mode'] != 'monitor-only'
                or not isinstance(dual, dict) or set(dual) != {'capture_enabled'}
                or dual['capture_enabled'] is not True or not isinstance(state, dict)
                or state.get('mode') != 'monitor-only' or state.get('dispatch_enabled') is not False
                or state.get('route_stopped') is not False):
            return False
        ledger = self.relay._load(now)
        return (ledger['baseline_complete'] is True and ledger['healthy'] is True
                and finite(ledger['checked_at']) and ledger['checked_at'] <= now + 120
                and now - ledger['checked_at'] <= FRESHNESS)

    def arm(self, expected_owner_pin=None, *, resume_only=False, expected_config=None, resume_guard=None):
        """Call only from the approved existing host, outside all its locks."""
        if not self._mutex.acquire(False):
            return {'result': 'busy'}
        try:
            with existing_monitor_lock(self.data) as acquired:
                if not acquired:
                    return {'result': 'monitor_lock_busy'}
                with lock(self.data) as acquired:
                    if not acquired:
                        return {'result': 'dual_lock_busy'}
                    now = self.clock()
                    if not finite(now) or not self._source_enabled(now):
                        return {'result': 'source_blocked'}
                    old = read_control(self.data)
                    if resume_only and (old['enabled'] is not True or old != expected_config or not callable(resume_guard)):
                        return {'result': 'auto_control_changed'}
                    # Initial approval must come from the trusted handoff that
                    # verified the owner, not from whatever account is present
                    # when this method eventually runs.
                    approved_pin = old['owner_pin']
                    if approved_pin is None:
                        if not isinstance(expected_owner_pin, str) or not expected_owner_pin:
                            return {'result': 'owner_pin_proof_required'}
                        approved_pin = expected_owner_pin
                    elif expected_owner_pin is not None and expected_owner_pin != approved_pin:
                        return {'result': 'owner_pin_proof_mismatch'}
                    contract = getattr(self.host, 'contract', None)
                    if (not isinstance(contract, dict) or type(contract.get('version')) is not int
                            or contract['version'] != 1 or contract.get('local_only') is not True
                            or contract.get('bounded_commit') is not True):
                        return {'result': 'host_contract_missing'}
                    sample = self.host.sample_binding()
                    if not isinstance(sample, dict) or sample.get('bound_ready') is not True:
                        return {'result': 'owner_not_ready'}
                    pin, generation = sample.get('owner_pin'), sample.get('generation')
                    if not isinstance(pin, str) or not isinstance(generation, str):
                        return {'result': 'owner_not_ready'}
                    if approved_pin != pin:
                        return {'result': 'owner_changed_requires_review'}
                    if self.host.approve_owner_pin(pin) is not True:
                        return {'result': 'owner_not_ready'}
                    with self.host.owner_fence(generation) as session:
                        if getattr(session, 'ready', None) is not True or getattr(session, 'generation', None) != generation:
                            return {'result': 'owner_not_ready'}
                        now = self.clock()  # Recheck time and source AFTER the fence.
                        if resume_only and resume_guard(old, now) is not True:
                            return {'result': 'auto_authorization_changed'}
                        if (not finite(now) or not self._source_enabled(now)
                                or old['consumer_activation_at'] is not None and now < old['consumer_activation_at']):
                            return {'result': 'source_blocked'}
                        reason = authorized_cutoff_reason(generation, now, now)
                        if reason is not None:
                            return {'result': reason}
                        if old['enabled']:
                            reason = authorized_cutoff_reason(old['expected_generation'],
                                old['consumer_activation_at'], now)
                            if reason is not None:
                                return {'result': reason}
                            if old['expected_generation'] == generation:
                                return {'result': 'already_armed', 'revision': old['revision'],
                                        'consumer_activation_at': old['consumer_activation_at']}
                            reason = maintenance_resume_reason(old['expected_generation'], generation,
                                old['consumer_activation_at'], now)
                            if reason is not None:
                                return {'result': reason}
                            value = dict(old, expected_generation=generation,
                                         revision=old['revision'] + 1)
                            atomic_json(self.data / CONTROL_NAME, value)
                            self._adapter = None
                            return {'result': 'maintenance_resumed', 'revision': value['revision'],
                                    'consumer_activation_at': value['consumer_activation_at'],
                                    'activation_preserved': True, 'email_enabled': False}
                        value = dict(version=1, enabled=True, expected_generation=generation,
                            owner_pin=pin, consumer_activation_at=now, revision=old['revision'] + 1,
                            email_enabled=False)
                        atomic_json(self.data / CONTROL_NAME, value)
                        self._adapter = None
                        return {'result': 'armed_future_only', 'revision': value['revision'],
                                'consumer_activation_at': now, 'email_enabled': False}
        except BlockingIOError:
            return {'result': 'host_lock_busy'}
        except Exception:
            return {'result': 'activation_failed_check_control'}
        finally:
            self._mutex.release()

    def off(self):
        if not self._mutex.acquire(False):
            return {'result': 'busy'}
        try:
            with existing_monitor_lock(self.data) as acquired:
                if not acquired:
                    return {'result': 'monitor_lock_busy'}
                with lock(self.data) as acquired:
                    if not acquired:
                        return {'result': 'dual_lock_busy'}
                    value = read_control(self.data)
                    value.update(enabled=False, revision=value['revision'] + 1)
                    atomic_json(self.data / CONTROL_NAME, value)
                    self._adapter = None
                    return {'result': 'consumer_off', 'revision': value['revision'],
                            'queued_notifications_cancelled': False}
        except Exception:
            return {'result': 'off_failed_check_control'}
        finally:
            self._mutex.release()

    def step(self):
        """At most one local enqueue or original-ID query; never implicitly arm."""
        if not self._mutex.acquire(False):
            return {'result': 'busy'}
        try:
            config = read_control(self.data)
            if config['enabled'] is not True:
                return {'result': 'disabled', 'email_enabled': False}
            # Pinning can be restored only for the SAME approved owner. It does
            # not refresh the generation or cutoff after restart/rebinding.
            if self.host.approve_owner_pin(config['owner_pin']) is not True:
                return {'result': 'owner_changed_requires_review'}
            sample = self.host.sample_binding()
            if (not isinstance(sample, dict) or sample.get('ready') is not True
                    or sample.get('generation') != config['expected_generation']
                    or sample.get('owner_pin') != config['owner_pin']):
                return {'result': 'owner_not_ready'}
            if self._adapter is None or self._revision != config['revision']:
                self._adapter = ConfiguredAdapter(self.relay, self.host, config, self.clock, resume_after=self.resume_after)
                self._revision = config['revision']
            return self._adapter.step()
        except Exception:
            return {'result': 'local_control_or_host_blocked'}
        finally:
            self._mutex.release()
