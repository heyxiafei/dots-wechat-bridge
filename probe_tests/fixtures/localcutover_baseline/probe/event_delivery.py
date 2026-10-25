"""Exact owner/event metadata projection. SELECT-only; no recovery or network I/O."""
import math

try:
    from .weixin import WeixinError, MESSAGE_EVENT
except ImportError:
    from weixin import WeixinError, MESSAGE_EVENT


EVENT_DELIVERY_VERSION = 'event-delivery-status-v1'
STATES = {'queued', 'delivered', 'acknowledged', 'cancelled', 'failed'}
ERRORS = {'subscription_inactive', 'retry_limit', 'webhook_transport_error',
          'webhook_retryable_http', 'webhook_permanent_http'}


def finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


class EventDeliveryStatus:
    def __init__(self, probe):
        self.probe, self.db, self.lock = probe, probe.db, probe.lock

    def for_message(self, message_id):
        if not isinstance(message_id, str) or not 0 < len(message_id) <= 256:
            raise WeixinError('unknown_owner_message')
        # SAVEPOINT gives all SELECTs one snapshot even across different SQLite
        # connections. Releasing it never commits an existing caller transaction.
        with self.lock:
            self.db.execute('SAVEPOINT event_delivery_status_read')
            try:
                owners = self.db.execute('SELECT id,owner FROM weixin_accounts LIMIT 2').fetchall()
                if not owners:
                    raise WeixinError('not_bound')
                if len(owners) != 1:
                    raise WeixinError('owner_binding_not_unique')
                owner = owners[0]
                source = self.db.execute('SELECT id,event_id FROM weixin_inbox WHERE id=? AND account=? AND peer=?',
                                         (message_id, owner['id'], owner['owner'])).fetchone()
                if not source:
                    raise WeixinError('unknown_owner_message')
                now = self.probe.clock()
                result = {'event_delivery_version': EVENT_DELIVERY_VERSION,
                          'event_id': source['event_id'],
                          'assignment_status': 'unassigned', 'status': None,
                          'attempts': None, 'next_at': None,
                          'last_http_status': None, 'last_error_kind': None,
                          'lease_until': None, 'lease_active': False,
                          'subscription_scope': 'associated_event',
                          'subscription': {'present': False, 'active': False, 'expires_at': None},
                          'dispatch_blocked_reason': 'event_unassigned',
                          'observed_at': now, 'read_consistency': 'sqlite_snapshot',
                          'claim_granted': False, 'external_write_allowed': False,
                          'automatic_replay_allowed': False,
                          'transport_ready_confirmed': False,
                          'phone_delivery_observed': False}
                if not source['event_id']:
                    return result
                # Never project fields from a row merely referenced by an owner
                # inbox ID: both the event type and reverse link must match.
                event = self.db.execute('''SELECT id,sub_id,status,attempts,next_at,
                    last_http_status,last_error_kind,lease_until FROM pings
                    WHERE id=? AND name=? AND message_id=?''',
                    (source['event_id'], MESSAGE_EVENT, message_id)).fetchone()
                if not event:
                    result.update(assignment_status='association_invalid',
                                  dispatch_blocked_reason='association_invalid')
                    return result
                sub = self.db.execute('SELECT expires FROM subscriptions WHERE id=? AND name=?',
                                      (event['sub_id'], MESSAGE_EVENT)).fetchone()
                expires = finite(sub['expires']) if sub else None
                active = bool(sub and (sub['expires'] is None or
                                       (expires is not None and expires > now)))
                status = event['status'] if event['status'] in STATES else 'invalid'
                lease = finite(event['lease_until'])
                leased = bool(status == 'queued' and lease is not None and lease > now)
                next_at = finite(event['next_at'])
                code = event['last_http_status']
                reason = event['last_error_kind']
                blocked = None
                if status in ('failed', 'cancelled'):
                    blocked = 'event_' + status
                elif status == 'invalid':
                    blocked = 'invalid_event_status'
                elif status == 'queued':
                    if not sub:
                        blocked = 'subscription_missing'
                    elif not active:
                        blocked = 'subscription_expired'
                    elif leased:
                        blocked = 'event_leased'
                    elif next_at is not None and next_at > now:
                        blocked = 'retry_backoff'
                result.update(assignment_status='associated', status=status,
                              attempts=event['attempts'] if type(event['attempts']) is int and event['attempts'] >= 0 else None,
                              next_at=next_at,
                              last_http_status=code if type(code) is int and 100 <= code <= 599 else None,
                              last_error_kind=reason if reason in ERRORS else None,
                              lease_until=lease, lease_active=leased,
                              subscription={'present': sub is not None, 'active': active, 'expires_at': expires},
                              dispatch_blocked_reason=blocked)
                return result
            finally:
                self.db.execute('RELEASE SAVEPOINT event_delivery_status_read')
