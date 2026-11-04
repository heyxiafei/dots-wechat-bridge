"""Explicit production admission for an approved future-only cutover.

This module enforces local facts. Operator evidence references are audit records,
not machine-verifiable proof of remote fencing or platform permission. The same
authorized operator boundary as retained_state remains a prerequisite.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat

if __package__:
    from .weixin import WeixinError, encoded
    from .diagnostic_state import EMPTY_STOPPED_TRIAL, EmptyDiagnosticError, validate_empty_stopped_trial
else:
    from weixin import WeixinError, encoded
    from diagnostic_state import EMPTY_STOPPED_TRIAL, EmptyDiagnosticError, validate_empty_stopped_trial

MODE = 'fresh_after_approved_cutover'
TABLE = 'weixin_fresh_cutover'
ADMISSIONS = 'weixin_fresh_admissions'
REFERENCES = ('platform_route_evidence_reference',
              'subscription_continuity_evidence_reference', 'fresh_cutover_evidence_reference')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def fail(reason):
    raise WeixinError('fresh_' + reason)


def read_approval_file(path):
    """One exact operator file; no discovery, credentials or automatic approval."""
    path = Path(path)
    try:
        if any(p.is_symlink() for p in (path, *path.parents)):
            fail('approval_file_unsafe')
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_nlink != 1 or info.st_size > 32768):
                fail('approval_file_unsafe')
            raw = handle.read(32769)
        value = json.loads(raw)
        if not isinstance(value, dict):
            fail('approval_invalid')
        return value
    except (OSError, ValueError, TypeError):
        fail('approval_unavailable')


def binding_fingerprint(db):
    rows = db.execute('SELECT id,owner,status FROM weixin_accounts').fetchall()
    epoch = db.execute('SELECT epoch FROM weixin_binding_epoch WHERE id=1').fetchone()
    if len(rows) != 1 or rows[0][2] != 'ready' or not epoch or not epoch[0]:
        fail('binding_not_ready')
    return digest([rows[0][0], rows[0][1], epoch[0]])


def validate_approval(approval, now):
    handoff = approval.get('handoff')
    if not isinstance(handoff, dict) or handoff.get('continuity_mode') != MODE:
        fail('approval_mode_required')
    # A known existing route must not be relabelled as an empty first install.
    if approval.get('isolation_mode') != 'old_writer_fenced':
        fail('writer_isolation_required')
    for key in ('approval_reference', 'isolation_evidence_reference'):
        if not isinstance(approval.get(key), str) or not approval[key].strip():
            fail('operator_evidence_required')
    for key in REFERENCES:
        if not isinstance(handoff.get(key), str) or not handoff[key].strip():
            fail('operator_evidence_required')
    for key, pattern in (('generation', r'[0-9a-f]{32}'),
                         ('binding_sha256', r'[0-9a-f]{64}'),
                         ('route_sha256', r'[0-9a-f]{64}'),
                         ('subscription_id', r'sub_[0-9a-f]{32}'),
                         ('tunnel_id', r'tunnel_[0-9a-f]{32}')):
        if not isinstance(handoff.get(key), str) or not re.fullmatch(pattern, handoff[key]):
            fail('approval_scope_invalid')
    if handoff['subscription_id'] != 'sub_' + handoff['route_sha256'][:32]:
        fail('subscription_scope_invalid')
    cut, approved = handoff.get('cutoff_ms'), handoff.get('approved_at_ms')
    if (type(cut) is not int or type(approved) is not int or approved <= 0
            or cut < approved or cut > int(now * 1000) + 300000):
        fail('cutoff_invalid')
    spec = dict(handoff)
    if 'subscription_receipt' in spec:
        seed = spec['subscription_receipt']
        fields = {'subscription_id','route_sha256','binding_sha256','verified_at','revision','expires_at'}
        if (not isinstance(seed, dict) or set(seed) != fields
                or any(seed.get(key) != spec[key] for key in ('subscription_id','route_sha256','binding_sha256'))
                or not isinstance(seed.get('revision'), str) or not re.fullmatch(r'[0-9a-f]{32}',seed['revision'])
                or type(seed.get('verified_at')) not in (int,float)
                or not math.isfinite(seed['verified_at'])
                or not approved / 1000 <= seed['verified_at'] <= now):
            fail('subscription_seed_invalid')
        expiry = seed.get('expires_at')
        if expiry is not None and (type(expiry) not in (int,float) or not math.isfinite(expiry)
                                   or expiry <= seed['verified_at']):
            fail('subscription_seed_invalid')
    return spec


def persisted_subscription_matches(db, spec, now, *, exact):
    """Inspect the normal challenge-written subscription, never modify it.

    First activation pins the actual approved receipt exactly. On restart the
    same normal table may contain a later legitimate same-ID refresh; no second
    ledger, stale request replay or synthesized extension is involved.
    """
    seed = spec.get('subscription_receipt')
    if seed is None:
        return False
    row = db.execute('''SELECT id,url,name,verified_at,revision,expires
        FROM subscriptions WHERE id=?''', (spec['subscription_id'],)).fetchone()
    if row is None:
        return False
    identity, url, name, verified, revision, expiry = row
    if (name != 'weixin.message' or identity != spec['subscription_id']
            or hashlib.sha256(encoded(['owner-only-tunnel',url,name,{}]).encode()).hexdigest() != spec['route_sha256']
            or type(verified) not in (int,float) or not math.isfinite(verified)
            or not max(seed['verified_at'],spec['approved_at_ms']/1000) <= verified <= now
            or not isinstance(revision,str) or not re.fullmatch(r'[0-9a-f]{32}',revision)
            or expiry is not None and (type(expiry) not in (int,float) or not math.isfinite(expiry)
                                       or expiry <= now or expiry <= verified)):
        return False
    return not exact or (verified == seed['verified_at'] and revision == seed['revision']
                         and expiry == seed['expires_at'])


def inspect_state(db, approval, now):
    """Read-only activation preflight; empty history is permitted, never deleted."""
    spec = validate_approval(approval, now)
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'weixin_bounded_dot_check' in tables:
        fail('diagnostic_state_not_promotable')
    if EMPTY_STOPPED_TRIAL in tables:
        try:
            validate_empty_stopped_trial(db, now)
        except EmptyDiagnosticError:
            fail('diagnostic_state_not_promotable')
    if binding_fingerprint(db) != spec['binding_sha256']:
        fail('binding_changed')
    if TABLE in tables:
        rows = db.execute('SELECT approval_sha256,config_json FROM ' + TABLE).fetchall()
        if len(rows) != 1 or rows[0][0] != digest(approval) or json.loads(rows[0][1]) != spec:
            fail('immutable_cutover_mismatch')
    else:
        # The mode starts only from the newly bound, unused state. It never
        # selectively imports, deletes, promotes or replays an old queue.
        for table in ('weixin_inbox', 'weixin_outbox', 'pings', 'weixin_peers',
                      'weixin_attachments', 'weixin_uploads'):
            if table in tables and db.execute('SELECT 1 FROM ' + table + ' LIMIT 1').fetchone():
                fail('new_state_required')
        if db.execute("SELECT 1 FROM weixin_accounts WHERE cursor!='' OR last_poll IS NOT NULL").fetchone():
            fail('new_state_required')
        if 'subscription_receipt' in spec and not persisted_subscription_matches(db,spec,now,exact=True):
            fail('subscription_seed_mismatch_or_expired')
    for row in db.execute('SELECT id,url,name FROM subscriptions'):
        route = hashlib.sha256(encoded(['owner-only-tunnel', row[1], row[2], {}]).encode()).hexdigest()
        if (row[0] != spec['subscription_id'] or row[2] != 'weixin.message'
                or route != spec['route_sha256']):
            fail('original_subscription_required')
    return spec


class FreshAdmission:
    def __init__(self, probe, spec, approval_hash):
        self.probe, self.db = probe, probe.db
        self.spec, self.approval_hash = spec, approval_hash
        self.subscription_id = spec['subscription_id']
        self.handshaken = persisted_subscription_matches(self.db,spec,probe.clock(),exact=False)

    @classmethod
    def restore(cls, probe):
        table = probe.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
        if not table:
            return None
        approval = read_approval_file(probe.directory.parent / 'setup-approval.json')
        spec = inspect_state(probe.db, approval, probe.clock())
        return cls(probe, spec, digest(approval))

    @classmethod
    def activate(cls, probe, approval):
        """Only the explicit production entry calls this, under its worker lock.

        A lock prevents two local workers; it does not prove remote isolation.
        No approval file is produced here. The operator must first verify the
        existing writer and original platform route and record the real action.
        """
        with probe.lock, probe.db:
            probe.db.execute('BEGIN IMMEDIATE')
            actual = read_approval_file(probe.directory.parent / 'setup-approval.json')
            if digest(actual) != digest(approval):
                fail('approval_changed')
            spec = inspect_state(probe.db, approval, probe.clock())
            probe.db.execute('''CREATE TABLE IF NOT EXISTS weixin_fresh_cutover (
                id INTEGER PRIMARY KEY CHECK(id=1), approval_sha256 TEXT NOT NULL,
                config_json TEXT NOT NULL, activated_at REAL NOT NULL)''')
            probe.db.execute('''CREATE TABLE IF NOT EXISTS weixin_fresh_admissions (
                message_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
                source_id TEXT NOT NULL, source_ms INTEGER NOT NULL,
                UNIQUE(generation,source_id))''')
            probe.db.execute('''CREATE TABLE IF NOT EXISTS weixin_fresh_contexts (
                message_id TEXT PRIMARY KEY, generation TEXT NOT NULL,
                source_ms INTEGER NOT NULL, token_sha256 TEXT NOT NULL)''')
            probe.db.execute('INSERT OR IGNORE INTO weixin_fresh_cutover VALUES(1,?,?,?)',
                             (digest(approval), json.dumps(spec, sort_keys=True), probe.clock()))
            for table in (TABLE, ADMISSIONS, 'weixin_fresh_contexts'):
                for action in ('UPDATE', 'DELETE'):
                    probe.db.execute('CREATE TRIGGER IF NOT EXISTS ' + table + '_' + action.lower() +
                        ' BEFORE ' + action + ' ON ' + table +
                        " BEGIN SELECT RAISE(ABORT,'fresh admission is immutable'); END")
        result = cls(probe, spec, digest(approval))
        probe.fresh_admission = result
        return result

    def _identity(self):
        if binding_fingerprint(self.db) != self.spec['binding_sha256']:
            fail('binding_changed')
        approval = read_approval_file(self.probe.directory.parent / 'setup-approval.json')
        if digest(approval) != self.approval_hash:
            fail('approval_changed')
        row = self.db.execute('SELECT approval_sha256,config_json FROM ' + TABLE + ' WHERE id=1').fetchone()
        if not row or row[0] != self.approval_hash or json.loads(row[1]) != self.spec:
            fail('immutable_cutover_mismatch')

    def before_subscribe(self, params):
        self._identity()
        delivery = params.get('delivery', {})
        route = hashlib.sha256(encoded(['owner-only-tunnel', delivery.get('url'), params.get('name'), {}]).encode()).hexdigest()
        if (params.get('name') != 'weixin.message' or params.get('arguments', {}) != {}
                or route != self.spec['route_sha256']
                or self.probe.subscription_id(params) != self.subscription_id):
            fail('original_subscription_required')

    def after_subscribe(self, result):
        self._identity()
        if result.get('id') != self.subscription_id:
            fail('original_subscription_required')
        self._subscription()
        self.handshaken = True

    def _subscription(self):
        row = self.db.execute('SELECT id,url,name,expires,verified_at FROM subscriptions WHERE id=?',
                              (self.subscription_id,)).fetchone()
        if (not row or row[2] != 'weixin.message' or row[4] <= 0
                or row[3] is not None and row[3] <= self.probe.clock()):
            fail('original_subscription_inactive')
        route = hashlib.sha256(encoded(['owner-only-tunnel', row[1], row[2], {}]).encode()).hexdigest()
        if route != self.spec['route_sha256']:
            fail('original_subscription_required')
        if ('subscription_receipt' in self.spec
                and not persisted_subscription_matches(self.db,self.spec,self.probe.clock(),exact=False)):
            fail('subscription_seed_not_current')

    def require_ready(self):
        self._identity()
        if not self.handshaken:
            fail('original_handshake_required')
        self._subscription()

    def inspect_batch(self, account, msgs, now):
        self.require_ready()
        bound = self.db.execute('SELECT id,owner FROM weixin_accounts').fetchone()
        if account.get('id') != bound[0] or account.get('owner') != bound[1]:
            fail('binding_changed')
        result = []
        for msg in msgs:
            if (not isinstance(msg, dict) or msg.get('from_user_id') != bound[1]
                    or msg.get('group_id') or msg.get('room_id')
                    or type(msg.get('message_type')) is not int or msg['message_type'] != 1
                    or msg.get('message_state') != 2 or msg.get('delete_time_ms')
                    or msg.get('to_user_id') not in (None, '', bound[0])):
                result.append(None)
                continue
            stamp = msg.get('create_time_ms')
            # No synthesized receive time, bool, fractional/NaN time or fallback.
            if (type(stamp) not in (int, float) or not 0 < stamp <= (now + 300) * 1000
                    or not math.isfinite(stamp) or int(stamp) != stamp):
                fail('source_time_invalid')
            # Official wire type is uint64 / lossless decimal string. An
            # outgoing client_id is not an authoritative incoming source ID.
            source = msg.get('message_id')
            if (type(source) not in (str, int) or not re.fullmatch(r'[1-9][0-9]{0,19}', str(source))
                    or int(source) > 18446744073709551615):
                fail('source_identity_invalid')
            if stamp <= self.spec['cutoff_ms']:
                result.append(None)
                continue
            identity = 'wx_' + hashlib.sha256(encoded([bound[0], bound[1], str(source)]).encode()).hexdigest()[:32]
            result.append({'identity': identity, 'source_id': str(source), 'source_ms': int(stamp)})
        return result

    def admit_in_transaction(self, identity, source_id, source_ms):
        self.require_ready()
        if not self.db.in_transaction or source_ms <= self.spec['cutoff_ms']:
            fail('admission_transaction_required')
        self.validate_candidate_in_transaction(identity, source_id, source_ms)
        values = (self.spec['generation'], source_id, source_ms)
        self.db.execute('INSERT OR IGNORE INTO ' + ADMISSIONS + ' VALUES(?,?,?,?)', (identity, *values))

    def validate_candidate_in_transaction(self, identity, source_id, source_ms):
        self.require_ready()
        old = self.db.execute('SELECT generation,source_id,source_ms FROM ' + ADMISSIONS + ' WHERE message_id=?', (identity,)).fetchone()
        values = (self.spec['generation'], source_id, source_ms)
        if old is not None and tuple(old) != values:
            fail('source_identity_conflict')
        existing = self.db.execute('SELECT source_id,created_at FROM weixin_inbox WHERE id=?', (identity,)).fetchone()
        if existing is not None and (existing[0], existing[1]) != (source_id, source_ms / 1000):
            fail('source_identity_conflict')

    def record_context_in_transaction(self, identity, token, source_ms):
        self.require_source(identity)
        if not self.db.in_transaction:
            fail('admission_transaction_required')
        self.db.execute('INSERT INTO weixin_fresh_contexts VALUES(?,?,?,?)',
                        (identity, self.spec['generation'], source_ms, hashlib.sha256(token.encode()).hexdigest()))

    def require_context(self, context):
        self.require_ready()
        bound = self.db.execute('SELECT id,owner FROM weixin_accounts').fetchone()
        peer = self.db.execute('SELECT token,source_ms,received_at FROM weixin_peers WHERE account=? AND peer=?', tuple(bound)).fetchone()
        if (not peer or context['token'] != peer[0] or context['received_at'] != peer[2]):
            fail('context_not_admitted')
        row = self.db.execute('SELECT message_id FROM weixin_fresh_contexts WHERE generation=? AND source_ms=? AND token_sha256=?',
                              (self.spec['generation'], peer[1], hashlib.sha256(peer[0].encode()).hexdigest())).fetchone()
        if not row:
            fail('context_not_admitted')
        self.require_source(row[0])

    def require_source(self, message_id):
        self.require_ready()
        row = self.db.execute('''SELECT a.generation,a.source_id,a.source_ms,
                i.account,i.peer,i.source_id,i.created_at FROM weixin_fresh_admissions a
                JOIN weixin_inbox i ON i.id=a.message_id WHERE a.message_id=?''', (message_id,)).fetchone()
        bound = self.db.execute('SELECT id,owner FROM weixin_accounts').fetchone()
        if (not row or row[0] != self.spec['generation'] or row[2] <= self.spec['cutoff_ms']
                or (row[3], row[4]) != tuple(bound) or row[1] != row[5] or row[2] / 1000 != row[6]):
            fail('source_not_admitted')

    def allows_source(self, message_id):
        try:
            self.require_source(message_id)
            return True
        except (WeixinError, ValueError, TypeError):
            return False
