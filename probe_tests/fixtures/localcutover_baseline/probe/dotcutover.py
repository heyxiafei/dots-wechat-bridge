"""Bounded local ownership, explicitly not a remote old-writer fence."""
import hashlib
import re
import uuid

try:
    from .weixin import WeixinError, encoded
except ImportError:
    from weixin import WeixinError, encoded

CUTOVER_VERSION = 'local-ownership-v1'


def cutover_tools():
    return [
        {'name': 'get_weixin_dot_cutover_status',
         'description': 'Read the exact local old-source snapshot and new-source reservations. Sends nothing. A local boundary does NOT prove remote old dot tasks stopped or revoke their direct API access.',
         'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
         'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
        {'name': 'enable_weixin_dot_cutover_new_sources',
         'description': 'Enable only atomically reserved new local owner source IDs in the existing cutover, bound to one target scope. This is the explicitly risk-accepted bounded mode, NOT verified remote fencing. All old IDs and unknown parts remain excluded; old remote direct writers can still race. Default is closed. Does not send, replay, renew or create a boundary.',
         'inputSchema': {'type': 'object', 'properties': {
             'boundary_id': {'type': 'string', 'pattern': '^djc_[a-f0-9]{32}$'},
             'target_scope': {'type': 'string', 'minLength': 1, 'maxLength': 256}},
             'required': ['boundary_id', 'target_scope'], 'additionalProperties': False},
         'annotations': {'readOnlyHint': False, 'destructiveHint': False,
                         'idempotentHint': True, 'openWorldHint': False}},
    ]


class DotCutoverStore:
    def __init__(self, probe):
        self.probe, self.db, self.lock, self.clock = probe, probe.db, probe.lock, probe.clock
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS weixin_dot_cutover (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL UNIQUE,
                account TEXT NOT NULL, owner TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL CHECK(state IN ('closed','new_only')),
                target_scope TEXT, snapshot_count INTEGER NOT NULL, snapshot_hash TEXT NOT NULL,
                created_at REAL NOT NULL, enabled_at REAL,
                CHECK(state='closed' OR target_scope IS NOT NULL));
            CREATE TABLE IF NOT EXISTS weixin_dot_cutover_sources (
                boundary_id TEXT NOT NULL, message_id TEXT NOT NULL,
                classification TEXT NOT NULL CHECK(classification IN ('legacy','reserved_new')),
                PRIMARY KEY(boundary_id,message_id));
            CREATE TRIGGER IF NOT EXISTS weixin_dot_cutover_ingress
                AFTER INSERT ON weixin_inbox BEGIN
                INSERT OR IGNORE INTO weixin_dot_cutover_sources
                    (boundary_id,message_id,classification)
                SELECT c.id,NEW.id,'reserved_new' FROM weixin_dot_cutover c
                WHERE c.account=NEW.account AND c.owner=NEW.peer;
            END;
        ''')
        self.db.commit()

    def _owner(self):
        rows = self.db.execute('SELECT id,owner FROM weixin_accounts LIMIT 2').fetchall()
        if len(rows) != 1:
            raise WeixinError('dot_cutover_owner_unavailable')
        return rows[0]

    def policy(self):
        row = self.db.execute('SELECT * FROM weixin_dot_cutover WHERE singleton=1').fetchone()
        if row:
            owner = self._owner()
            if row['account'] != owner['id'] or row['owner'] != owner['owner']:
                raise WeixinError('dot_cutover_owner_mismatch')
        return row

    def _metadata(self, row):
        if not row:
            return {'cutover_version': CUTOVER_VERSION, 'state': 'not_staged',
                    'new_admission_enabled': False, 'remote_legacy_writers_fenced': False}
        counts = {r[0]: r[1] for r in self.db.execute('''SELECT classification,count(*)
            FROM weixin_dot_cutover_sources WHERE boundary_id=? GROUP BY classification''', (row['id'],))}
        return {'cutover_version': CUTOVER_VERSION, 'boundary_id': row['id'],
                'state': row['state'], 'target_scope': row['target_scope'],
                'snapshot_count': row['snapshot_count'], 'snapshot_hash': row['snapshot_hash'],
                'legacy_count': counts.get('legacy', 0), 'reserved_new_count': counts.get('reserved_new', 0),
                'new_admission_enabled': row['state'] == 'new_only',
                'local_legacy_prepare_fenced': True, 'mode': 'bounded_local_ownership',
                'remote_legacy_writers_fenced': False,
                'remote_legacy_direct_write_risk': True, 'strict_exactly_once': False,
                'automatic_resend_allowed': False, 'communication_authorization_granted': False}

    def hold(self, request_id):
        """Trusted upgrade action, never an MCP caller-supplied fence proof."""
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', request_id):
            raise WeixinError('invalid_dot_cutover_request')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            owner = self._owner()
            old = self.policy()
            if old:
                if old['request_id'] != request_id:
                    raise WeixinError('dot_cutover_already_exists')
                return self._metadata(old)
            if self.db.execute("SELECT 1 FROM weixin_dot_journal WHERE state='authorized'").fetchone():
                raise WeixinError('dot_cutover_authorized_intent_pending')
            if self.db.execute("SELECT 1 FROM weixin_dot_mirror_epochs WHERE state IN ('staged','active')").fetchone():
                raise WeixinError('dot_cutover_epoch_conflict')
            boundary = 'djc_' + uuid.uuid4().hex
            ids = [r[0] for r in self.db.execute('SELECT id FROM weixin_inbox WHERE account=? AND peer=? ORDER BY id',
                                                (owner['id'], owner['owner']))]
            digest = hashlib.sha256(encoded(ids).encode()).hexdigest()
            self.db.execute('''INSERT INTO weixin_dot_cutover
                (singleton,id,account,owner,request_id,state,snapshot_count,snapshot_hash,created_at)
                VALUES(1,?,?,?,?,'closed',?,?,?)''',
                (boundary, owner['id'], owner['owner'], request_id, len(ids), digest, self.clock()))
            self.db.execute('''INSERT INTO weixin_dot_cutover_sources
                (boundary_id,message_id,classification)
                SELECT ?,id,'legacy' FROM weixin_inbox WHERE account=? AND peer=?''',
                (boundary, owner['id'], owner['owner']))
            return self._metadata(self.policy())

    def status(self):
        with self.lock:
            self.db.execute('SAVEPOINT dot_cutover_read')
            try:
                result = self._metadata(self.policy())
            except BaseException:
                self.db.execute('ROLLBACK TO SAVEPOINT dot_cutover_read')
                self.db.execute('RELEASE SAVEPOINT dot_cutover_read')
                raise
            self.db.execute('RELEASE SAVEPOINT dot_cutover_read')
            return result

    def classify(self, message_id):
        row = self.policy()
        if not row:
            return None
        member = self.db.execute('SELECT classification FROM weixin_dot_cutover_sources WHERE boundary_id=? AND message_id=?',
                                 (row['id'], message_id)).fetchone()
        classification = member[0] if member else 'unclassified'
        return {**self._metadata(row), 'classification': classification,
                'new_write_eligible': row['state'] == 'new_only' and classification == 'reserved_new'}

    def enable(self, boundary_id, target_scope):
        if (not isinstance(boundary_id, str) or not re.fullmatch(r'djc_[a-f0-9]{32}', boundary_id)
                or not isinstance(target_scope, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', target_scope)):
            raise WeixinError('invalid_dot_cutover_arguments')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.policy()
            if not row or row['id'] != boundary_id:
                raise WeixinError('dot_cutover_boundary_mismatch')
            if row['state'] == 'new_only':
                if row['target_scope'] != target_scope:
                    raise WeixinError('dot_cutover_scope_conflict')
                return self._metadata(row)
            if self.db.execute("SELECT 1 FROM weixin_dot_journal WHERE state='authorized'").fetchone():
                raise WeixinError('dot_cutover_authorized_intent_pending')
            if self.db.execute("SELECT 1 FROM weixin_dot_mirror_epochs WHERE state IN ('staged','active')").fetchone():
                raise WeixinError('dot_cutover_epoch_conflict')
            self.db.execute("UPDATE weixin_dot_cutover SET state='new_only',target_scope=?,enabled_at=? WHERE singleton=1 AND state='closed'",
                            (target_scope, self.clock()))
            return self._metadata(self.policy())

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise WeixinError('invalid_dot_cutover_arguments')
        if name == 'get_weixin_dot_cutover_status' and not arguments:
            return self.status()
        if name == 'enable_weixin_dot_cutover_new_sources' and set(arguments) == {'boundary_id', 'target_scope'}:
            return self.enable(**arguments)
        raise WeixinError('invalid_dot_cutover_arguments')
