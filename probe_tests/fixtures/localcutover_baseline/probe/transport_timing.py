"""Owner-scoped host observations only; no message bodies or network activity.

Transaction hooks record when the bridge observes a stage, not proof of remote
execution. Wall-clock differences are estimates, not monotonic-clock timings.
"""
import math

try:
    from .weixin import WeixinError
except ImportError:
    from weixin import WeixinError

TIMING_VERSION = 'transport-timing-v1'
TIME_FIELDS = ('ingest_observed_at', 'ingest_commit_observed_at', 'event_queued_at',
               'first_webhook_attempt_at', 'last_webhook_attempt_at', 'webhook_accepted_at',
               'first_agent_read_at', 'last_agent_read_at')


class DeliveryTimingStore:
    def __init__(self, probe):
        self.probe, self.db, self.lock = probe, probe.db, probe.lock
        self.db.execute('''CREATE TABLE IF NOT EXISTS weixin_delivery_timing (
            message_id TEXT PRIMARY KEY,
            ingest_observed_at REAL, ingest_commit_observed_at REAL,
            event_queued_at REAL, first_webhook_attempt_at REAL,
            last_webhook_attempt_at REAL, webhook_accepted_at REAL,
            first_agent_read_at REAL, last_agent_read_at REAL,
            agent_read_count INTEGER NOT NULL DEFAULT 0
        )''')
        self.db.commit()

    @staticmethod
    def _time(now):
        if type(now) not in (int, float) or not math.isfinite(now):
            raise WeixinError('invalid_transport_timing_timestamp')
        return float(now)

    def owned(self, message_id):
        if not isinstance(message_id, str) or not 0 < len(message_id) <= 256:
            raise WeixinError('unknown_owner_message')
        with self.lock:
            owners = self.db.execute('SELECT id,owner FROM weixin_accounts LIMIT 2').fetchall()
            if len(owners) != 1:
                raise WeixinError('transport_timing_owner_unavailable')
            owner = owners[0]
            if not self.db.execute('SELECT id FROM weixin_inbox WHERE id=? AND account=? AND peer=?',
                                   (message_id, owner['id'], owner['owner'])).fetchone():
                raise WeixinError('unknown_owner_message')
            return owner

    def _mark(self, message_id, now, assignments, values):
        now = self._time(now)
        with self.lock:
            if not self.db.in_transaction:
                raise WeixinError('transport_timing_requires_transaction')
            self.owned(message_id)
            self.db.execute('INSERT OR IGNORE INTO weixin_delivery_timing(message_id) VALUES(?)', (message_id,))
            self.db.execute('UPDATE weixin_delivery_timing SET ' + assignments + ' WHERE message_id=?',
                            (*values(now), message_id))
        return True

    def ingested(self, message_id, now):
        return self._mark(message_id, now, 'ingest_observed_at=COALESCE(ingest_observed_at,?)', lambda n:(n,))

    def queued(self, message_id, now):
        return self._mark(message_id, now, 'event_queued_at=COALESCE(event_queued_at,?)', lambda n:(n,))

    def attempt(self, message_id, now):
        return self._mark(message_id, now,
                          'first_webhook_attempt_at=COALESCE(first_webhook_attempt_at,?),last_webhook_attempt_at=?',
                          lambda n:(n,n))

    def accepted(self, message_id, now):
        # The caller records this only in the successful delivery's CAS transaction.
        return self._mark(message_id, now, 'webhook_accepted_at=COALESCE(webhook_accepted_at,?)', lambda n:(n,))

    def committed(self, message_ids, now):
        """Best-effort post-commit observation. Never commit a caller transaction.

        Only records registered by ingested() may receive a commit observation.
        Failure here must not turn an already-successful ingest into a failure.
        """
        try:
            now = self._time(now)
            if (not isinstance(message_ids, (list, tuple)) or len(message_ids) > 1000
                    or not all(isinstance(mid, str) and 0 < len(mid) <= 256 for mid in message_ids)):
                return False
            with self.lock:
                if self.db.in_transaction:
                    return False
                with self.db:
                    self.db.execute('BEGIN IMMEDIATE')
                    changed = 0
                    for mid in dict.fromkeys(message_ids):
                        try:
                            self.owned(mid)
                        except WeixinError:
                            continue
                        changed += self.db.execute('''UPDATE weixin_delivery_timing
                            SET ingest_commit_observed_at=? WHERE message_id=?
                            AND ingest_observed_at IS NOT NULL AND ingest_commit_observed_at IS NULL''',
                            (now, mid)).rowcount
                return changed > 0
        except Exception:
            return False

    def agent_read(self, message_id, now):
        """Called only after a successful exact MCP read, never internal reads.

        This observes server-side success, not client receipt or model start.
        The hook is best effort and refuses to commit a surrounding transaction.
        """
        now = self._time(now)
        with self.lock:
            self.owned(message_id)
            if self.db.in_transaction:
                return False
            try:
                with self.db:
                    self.db.execute('BEGIN IMMEDIATE')
                    # Recheck under the write transaction against a binding change.
                    self.owned(message_id)
                    self.db.execute('INSERT OR IGNORE INTO weixin_delivery_timing(message_id) VALUES(?)', (message_id,))
                    self.db.execute('''UPDATE weixin_delivery_timing SET
                        first_agent_read_at=COALESCE(first_agent_read_at,?),last_agent_read_at=?,
                        agent_read_count=agent_read_count+1 WHERE message_id=?''', (now,now,message_id))
                return True
            except Exception:
                return False

    def for_message(self, message_id):
        with self.lock:
            owner = self.owned(message_id)
            row = self.db.execute('SELECT * FROM weixin_delivery_timing WHERE message_id=?', (message_id,)).fetchone()
            times = {field:row[field] if row else None for field in TIME_FIELDS}
            final = self.db.execute('''SELECT created_at,post_started_at,accepted_at,status
                FROM weixin_outbox WHERE message_id=? AND account=? AND peer=? AND kind='reply' ''',
                (message_id,owner['id'],owner['owner'])).fetchone()
            times.update({'final_queued_at':final['created_at'] if final else None,
                          'final_post_started_at':final['post_started_at'] if final else None,
                          'final_api_accepted_at':final['accepted_at'] if final else None})
            pairs = {
                'ingest_to_commit_observed':('ingest_observed_at','ingest_commit_observed_at'),
                'commit_observed_to_event_queued':('ingest_commit_observed_at','event_queued_at'),
                'event_queued_to_first_webhook_attempt':('event_queued_at','first_webhook_attempt_at'),
                'first_webhook_attempt_to_accepted':('first_webhook_attempt_at','webhook_accepted_at'),
                'last_webhook_attempt_to_accepted':('last_webhook_attempt_at','webhook_accepted_at'),
                'webhook_accepted_to_first_agent_read':('webhook_accepted_at','first_agent_read_at'),
                'first_agent_read_to_final_queued':('first_agent_read_at','final_queued_at'),
                'final_queued_to_post_started':('final_queued_at','final_post_started_at'),
                'final_post_started_to_api_accepted':('final_post_started_at','final_api_accepted_at'),
                'final_queued_to_api_accepted':('final_queued_at','final_api_accepted_at'),
                'commit_observed_to_final_api_accepted':('ingest_commit_observed_at','final_api_accepted_at')}
            durations, unordered = {}, []
            for name,(start,end) in pairs.items():
                a,b = times[start],times[end]
                valid = all(type(value) in (int,float) and math.isfinite(value) for value in (a,b))
                durations[name] = b-a if valid and b >= a else None
                if valid and b < a:
                    unordered.append(name)
            return {'message_id':message_id,'timing_version':TIMING_VERSION,
                    'timestamps':times,'agent_read_count':row['agent_read_count'] if row else 0,
                    'durations_seconds':durations,'unordered_or_overlapping_stages':unordered,
                    'final_status':final['status'] if final else None,
                    'clock_basis':'host_wall_clock_observations',
                    'duration_basis':'wall_clock_difference_estimates_not_monotonic',
                    'source_clock_comparable':False,
                    'attempt_basis':'delivery_attempt_committed_before_io',
                    'agent_read_basis':'successful_exact_mcp_read_server_side',
                    'webhook_accepted_is_model_start':False,'dot_processing_confirmed':False,
                    'phone_delivery_observed':False}
