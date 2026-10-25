"""Bounded local ownership, explicitly not a remote old-writer fence."""
import hashlib
import re
import uuid

try:
    from .weixin import WeixinError, encoded
except ImportError:
    from weixin import WeixinError, encoded

CUTOVER_VERSION = 'local-ownership-v2'


def cutover_tools():
    return [
        {'name': 'get_weixin_dot_cutover_status',
         'description': 'Read the exact local old-source snapshot and new-source reservations. Sends nothing. A local boundary does NOT prove remote old dot tasks stopped or revoke their direct API access.',
         'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
         'annotations': {'readOnlyHint': True, 'openWorldHint': False}},
        {'name': 'enable_weixin_dot_cutover_new_sources',
         'description': 'Enable only atomically reserved new local owner source IDs in the existing cutover, bound to one immutable target scope. Requires the current admission_revision from status; a close invalidates older enable requests. This bounded mode does NOT fence remote writers. Old and unknown parts stay excluded. Sends nothing.',
         'inputSchema': {'type': 'object', 'properties': {
             'boundary_id': {'type': 'string', 'pattern': '^djc_[a-f0-9]{32}$'},
             'target_scope': {'type': 'string', 'minLength': 1, 'maxLength': 256},
             'expected_revision': {'type': 'integer', 'minimum': 0}},
             'required': ['boundary_id', 'target_scope', 'expected_revision'], 'additionalProperties': False},
         'annotations': {'readOnlyHint': False, 'destructiveHint': False,
                         'idempotentHint': True, 'openWorldHint': False}},
        {'name': 'close_weixin_dot_cutover_new_sources',
         'description': 'Atomically and durably close new local journal admission using a stable request_id for this closing decision. Keeps its scope, source classifications, receipts and legacy prepare fence. Retrying the same request reports its applied revision and current state without undoing a later enable; a new closing decision requires a new request_id. Already granted permits are NOT revoked and remote writers are NOT fenced. Sends nothing.',
         'inputSchema': {'type': 'object', 'properties': {
             'boundary_id': {'type': 'string', 'pattern': '^djc_[a-f0-9]{32}$'},
             'request_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{1,256}$'}},
             'required': ['boundary_id', 'request_id'], 'additionalProperties': False},
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
                admission_revision INTEGER NOT NULL DEFAULT 0 CHECK(admission_revision>=0),
                closed_at REAL,
                CHECK(state='closed' OR target_scope IS NOT NULL));
            CREATE TABLE IF NOT EXISTS weixin_dot_cutover_sources (
                boundary_id TEXT NOT NULL, message_id TEXT NOT NULL,
                classification TEXT NOT NULL CHECK(classification IN ('legacy','reserved_new')),
                PRIMARY KEY(boundary_id,message_id));
            CREATE TABLE IF NOT EXISTS weixin_dot_cutover_closures (
                boundary_id TEXT NOT NULL, request_id TEXT NOT NULL,
                applied_revision INTEGER NOT NULL CHECK(applied_revision>0),
                created_at REAL NOT NULL, PRIMARY KEY(boundary_id,request_id));
            CREATE TRIGGER IF NOT EXISTS weixin_dot_cutover_ingress
                AFTER INSERT ON weixin_inbox BEGIN
                INSERT OR IGNORE INTO weixin_dot_cutover_sources
                    (boundary_id,message_id,classification)
                SELECT c.id,NEW.id,'reserved_new' FROM weixin_dot_cutover c
                WHERE c.account=NEW.account AND c.owner=NEW.peer;
            END;
        ''')
        self.db.commit()
        # Additive migration only. Serialize schema inspection across independent
        # connections, then persist the guard so an older worker's enable SQL
        # cannot reopen this database while ignoring the revision handshake.
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in self.db.execute('PRAGMA table_info(weixin_dot_cutover)')}
            if 'admission_revision' not in columns:
                self.db.execute('ALTER TABLE weixin_dot_cutover ADD COLUMN admission_revision INTEGER NOT NULL DEFAULT 0 CHECK(admission_revision>=0)')
            if 'closed_at' not in columns:
                self.db.execute('ALTER TABLE weixin_dot_cutover ADD COLUMN closed_at REAL')
            self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_cutover_enable_guard
                BEFORE UPDATE OF state ON weixin_dot_cutover
                WHEN OLD.state='closed' AND NEW.state='new_only'
                  AND (NEW.admission_revision != OLD.admission_revision+1 OR NEW.closed_at IS NOT NULL)
                BEGIN SELECT RAISE(ABORT,'dot_cutover_enable_revision_required'); END''')
            self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_cutover_scope_guard
                BEFORE UPDATE OF target_scope ON weixin_dot_cutover
                WHEN OLD.target_scope IS NOT NULL AND NEW.target_scope IS NOT OLD.target_scope
                BEGIN SELECT RAISE(ABORT,'dot_cutover_scope_conflict'); END''')

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
                'admission_revision': row['admission_revision'], 'closed_at': row['closed_at'],
                'previously_granted_permits_revoked': False,
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

    def enable(self, boundary_id, target_scope, expected_revision):
        if (not isinstance(boundary_id, str) or not re.fullmatch(r'djc_[a-f0-9]{32}', boundary_id)
                or not isinstance(target_scope, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', target_scope)
                or type(expected_revision) is not int or expected_revision < 0):
            raise WeixinError('invalid_dot_cutover_arguments')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.policy()
            if not row or row['id'] != boundary_id:
                raise WeixinError('dot_cutover_boundary_mismatch')
            if row['target_scope'] is not None and row['target_scope'] != target_scope:
                raise WeixinError('dot_cutover_scope_conflict')
            if row['state'] == 'new_only':
                # A retry of the immediately preceding successful enable may
                # read its result. It cannot perform a second transition.
                if expected_revision not in (row['admission_revision'], row['admission_revision'] - 1):
                    raise WeixinError('dot_cutover_revision_conflict')
                return self._metadata(row)
            if expected_revision != row['admission_revision']:
                raise WeixinError('dot_cutover_revision_conflict')
            if self.db.execute("SELECT 1 FROM weixin_dot_journal WHERE state='authorized'").fetchone():
                raise WeixinError('dot_cutover_authorized_intent_pending')
            if self.db.execute("SELECT 1 FROM weixin_dot_mirror_epochs WHERE state IN ('staged','active')").fetchone():
                raise WeixinError('dot_cutover_epoch_conflict')
            self.db.execute("""UPDATE weixin_dot_cutover SET state='new_only',target_scope=?,enabled_at=?,
                admission_revision=admission_revision+1,closed_at=NULL WHERE singleton=1 AND state='closed'""",
                            (target_scope, self.clock()))
            return self._metadata(self.policy())

    def close(self, boundary_id, request_id):
        if (not isinstance(boundary_id, str) or not re.fullmatch(r'djc_[a-f0-9]{32}', boundary_id)
                or not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', request_id)):
            raise WeixinError('invalid_dot_cutover_arguments')
        with self.lock, self.db:
            # Journal authorize uses the same write transaction boundary. If
            # close commits first, no subsequent authorize can grant a permit.
            # Earlier permits remain valid; close never rewrites journal rows.
            self.db.execute('BEGIN IMMEDIATE')
            row = self.policy()
            if not row or row['id'] != boundary_id:
                raise WeixinError('dot_cutover_boundary_mismatch')
            previous = self.db.execute('''SELECT applied_revision FROM weixin_dot_cutover_closures
                WHERE boundary_id=? AND request_id=?''', (boundary_id, request_id)).fetchone()
            if previous:
                # A lost response may be retried after a later explicit enable.
                # Preserve that newer decision, return current state separately
                # from the historical successful operation revision.
                return {**self._metadata(row), 'close_request_id': request_id,
                        'close_request_replayed': True,
                        'close_applied_revision': previous['applied_revision'],
                        'close_effect_is_current': row['state'] == 'closed' and
                            row['admission_revision'] == previous['applied_revision']}
            # Even the first close of an initially closed boundary invalidates
            # an enable request prepared before this explicit close. A distinct
            # close decision advances again; only the same request is a replay.
            now = self.clock()
            self.db.execute("""UPDATE weixin_dot_cutover SET state='closed',
                admission_revision=admission_revision+1,closed_at=? WHERE singleton=1""", (now,))
            row = self.policy()
            self.db.execute('''INSERT INTO weixin_dot_cutover_closures
                (boundary_id,request_id,applied_revision,created_at) VALUES(?,?,?,?)''',
                (boundary_id, request_id, row['admission_revision'], now))
            return {**self._metadata(row), 'close_request_id': request_id,
                    'close_request_replayed': False,
                    'close_applied_revision': row['admission_revision'], 'close_effect_is_current': True}

    def call(self, name, arguments):
        if not isinstance(arguments, dict):
            raise WeixinError('invalid_dot_cutover_arguments')
        if name == 'get_weixin_dot_cutover_status' and not arguments:
            return self.status()
        if name == 'enable_weixin_dot_cutover_new_sources' and set(arguments) == {'boundary_id', 'target_scope', 'expected_revision'}:
            return self.enable(**arguments)
        if name == 'close_weixin_dot_cutover_new_sources' and set(arguments) == {'boundary_id', 'request_id'}:
            return self.close(**arguments)
        raise WeixinError('invalid_dot_cutover_arguments')
