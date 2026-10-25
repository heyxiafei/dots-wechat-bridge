"""Bounded local ownership, explicitly not a remote old-writer fence."""
import hashlib
import re
import uuid

try:
    from .weixin import WeixinError, encoded
except ImportError:
    from weixin import WeixinError, encoded

CUTOVER_VERSION = 'local-ownership-v3-singletrial'
TRIAL_TEXT_MAX_BYTES = 256
STEADY_VERSION = 'future-admission-v1'
STEADY_MAX_SOURCES = 1000

# Compare the persisted SQL before replacing an admission guard. An unknown
# newer or locally hardened guard must remain intact and stop this migration.
SINGLETRIAL_AUTHORIZE_GUARD_SQL = '''CREATE TRIGGER IF NOT EXISTS weixin_dot_trial_authorize_guard
                BEFORE UPDATE ON weixin_dot_journal
                WHEN NEW.state='authorized' AND OLD.state!='authorized'
                 AND EXISTS(SELECT 1 FROM weixin_dot_single_trial)
                 AND NOT EXISTS(SELECT 1 FROM weixin_dot_single_trial t
                    JOIN weixin_dot_cutover c ON c.id=t.boundary_id
                    WHERE t.selected_message_id=NEW.message_id AND t.target_scope=NEW.target_scope
                      AND c.target_scope=t.target_scope AND c.state='closed'
                      AND c.admission_revision=t.selection_revision)
                BEGIN SELECT RAISE(ABORT,'dot_single_trial_admission_pending'); END'''

STEADY_AUTHORIZE_GUARD_SQL = '''CREATE TRIGGER weixin_dot_trial_authorize_guard
            BEFORE UPDATE ON weixin_dot_journal
            WHEN NEW.state='authorized' AND OLD.state!='authorized'
             AND EXISTS(SELECT 1 FROM weixin_dot_single_trial)
             AND NOT EXISTS(SELECT 1 FROM weixin_dot_single_trial t
                JOIN weixin_dot_cutover c ON c.id=t.boundary_id
                WHERE NEW.steady_generation_id IS NULL
                  AND t.selected_message_id=NEW.message_id
                  AND t.target_scope=NEW.target_scope
                  AND c.target_scope=t.target_scope AND c.state='closed'
                  AND c.admission_revision=t.selection_revision)
             AND NOT EXISTS(SELECT 1 FROM weixin_dot_steady_generations g
                JOIN weixin_dot_cutover c ON c.id=g.boundary_id
                JOIN weixin_dot_steady_sources m ON m.generation_id=g.id
                JOIN weixin_dot_cutover_sources s ON s.boundary_id=c.id
                   AND s.message_id=m.message_id AND s.classification='reserved_new'
                WHERE NEW.steady_generation_id=g.id
                  AND m.message_id=NEW.message_id AND g.target_scope=NEW.target_scope
                  AND g.state='active' AND c.state='closed'
                  AND c.target_scope=g.target_scope
                  AND c.admission_revision=g.activation_revision)
            BEGIN SELECT RAISE(ABORT,'dot_single_trial_admission_pending'); END'''


def single_trial_tools():
    common = {'boundary_id': {'type': 'string', 'pattern': '^djc_[a-f0-9]{32}$'},
              'request_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{1,256}$'},
              'expected_revision': {'type': 'integer', 'minimum': 0}}
    definitions = [
        ('arm_weixin_dot_single_source_trial',
         'While bulk admission stays closed, persist one irreversible trial restriction and a future-ingress boundary. Bind the immutable target scope and SHA-256 of one unique short synthetic text. Never sends or authorizes. Existing reserved sources are excluded; retries only read the same decision. This trial cannot be replaced or reset by this API.',
         {'target_scope': {'type': 'string', 'minLength': 1, 'maxLength': 256},
          'expected_text_sha256': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}}),
        ('select_weixin_dot_single_source_trial',
         'Select exactly one verified owner message first ingested after trial arm, with matching short text hash and complete text-only capture. Atomically enable journal admission only for this message while bulk state remains closed. Does not send, revoke permits or fence remote writers. Close invalidates this selection permanently; retries never reopen it.',
         {'message_id': {'type': 'string', 'minLength': 1, 'maxLength': 256}}),
    ]
    return [{'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': dict(common, **extra),
                             'required': list(common) + list(extra), 'additionalProperties': False},
             'annotations': {'readOnlyHint': False, 'destructiveHint': False,
                             'idempotentHint': True, 'openWorldHint': False}}
            for name, description, extra in definitions]


def steady_tools():
    common = {'boundary_id': {'type': 'string', 'pattern': '^djc_[a-f0-9]{32}$'},
              'request_id': {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{1,256}$'},
              'expected_revision': {'type': 'integer', 'minimum': 0}}
    definitions = [
        ('enable_weixin_dot_steady_future_sources',
         'After a completed and formally closed one-source trial, start one capped generation for owner messages first ingested after this decision. Previous, paused-gap and legacy messages stay excluded. Keeps bulk state closed. Local admission does not fence remote Dot writers, revoke permits, send or grant communication authorization.',
         {'target_scope': {'type': 'string', 'minLength': 1, 'maxLength': 256},
          'max_sources': {'type': 'integer', 'minimum': 1, 'maximum': STEADY_MAX_SOURCES}}),
        ('pause_weixin_dot_steady_future_sources',
         'Atomically stop new local journal permits for the current steady generation. Earlier permits and unknown results remain; the owner subscription and normal WeChat replies are unchanged. A later explicit enable starts a new future-only generation, never replays pause-gap sources.',
         {}),
    ]
    return [{'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': dict(common, **extra),
                             'required': list(common) + list(extra), 'additionalProperties': False},
             'annotations': {'readOnlyHint': False, 'destructiveHint': False,
                             'idempotentHint': True, 'openWorldHint': False}}
            for name, description, extra in definitions]


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
    ] + single_trial_tools() + steady_tools()


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
            self._init_trial_schema()
            self._init_steady_schema()

    def _init_trial_schema(self):
        # Called in the same serialized additive schema transaction. Persistent
        # SQL guards survive rollback to code which does not know about trials.
        statements = [
            '''CREATE TABLE IF NOT EXISTS weixin_dot_single_trial (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), boundary_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL, target_scope TEXT NOT NULL, expected_text_sha256 TEXT NOT NULL,
                arm_revision INTEGER NOT NULL, created_at REAL NOT NULL,
                selected_message_id TEXT, selection_revision INTEGER, selected_at REAL,
                CHECK((selected_message_id IS NULL AND selection_revision IS NULL AND selected_at IS NULL)
                   OR (selected_message_id IS NOT NULL AND selection_revision IS NOT NULL
                       AND selection_revision=arm_revision+1 AND selected_at IS NOT NULL)))''',
            '''CREATE TABLE IF NOT EXISTS weixin_dot_trial_future_sources (
                boundary_id TEXT NOT NULL, message_id TEXT NOT NULL,
                PRIMARY KEY(boundary_id,message_id))''',
            '''CREATE TRIGGER IF NOT EXISTS weixin_dot_trial_future_ingress
                AFTER INSERT ON weixin_inbox BEGIN
                INSERT OR IGNORE INTO weixin_dot_trial_future_sources(boundary_id,message_id)
                SELECT t.boundary_id,NEW.id FROM weixin_dot_single_trial t
                JOIN weixin_dot_cutover c ON c.id=t.boundary_id
                WHERE NEW.account=c.account AND NEW.peer=c.owner AND c.state='closed'
                    AND c.admission_revision=t.arm_revision AND t.selected_message_id IS NULL;
                END''',
            '''CREATE TRIGGER IF NOT EXISTS weixin_dot_trial_no_delete
                BEFORE DELETE ON weixin_dot_single_trial
                BEGIN SELECT RAISE(ABORT,'dot_trial_restriction_permanent'); END''',
            '''CREATE TRIGGER IF NOT EXISTS weixin_dot_trial_immutable
                BEFORE UPDATE ON weixin_dot_single_trial
                WHEN NEW.singleton IS NOT OLD.singleton OR NEW.boundary_id IS NOT OLD.boundary_id
                    OR NEW.request_id IS NOT OLD.request_id OR NEW.target_scope IS NOT OLD.target_scope
                    OR NEW.expected_text_sha256 IS NOT OLD.expected_text_sha256
                    OR NEW.arm_revision IS NOT OLD.arm_revision OR NEW.created_at IS NOT OLD.created_at
                    OR (OLD.selected_message_id IS NOT NULL AND
                        (NEW.selected_message_id IS NOT OLD.selected_message_id
                         OR NEW.selection_revision IS NOT OLD.selection_revision OR NEW.selected_at IS NOT OLD.selected_at))
                BEGIN SELECT RAISE(ABORT,'dot_trial_restriction_immutable'); END''',
            '''CREATE TRIGGER IF NOT EXISTS weixin_dot_trial_bulk_enable_guard
                BEFORE UPDATE ON weixin_dot_cutover
                WHEN NEW.state='new_only' AND EXISTS(SELECT 1 FROM weixin_dot_single_trial)
                BEGIN SELECT RAISE(ABORT,'dot_cutover_single_source_trial_required'); END''',
            SINGLETRIAL_AUTHORIZE_GUARD_SQL,
            '''CREATE TRIGGER IF NOT EXISTS weixin_dot_trial_authorized_insert_guard
                BEFORE INSERT ON weixin_dot_journal
                WHEN NEW.state='authorized' AND EXISTS(SELECT 1 FROM weixin_dot_single_trial)
                BEGIN SELECT RAISE(ABORT,'dot_single_trial_prepare_required'); END''',
        ]
        for statement in statements:
            self.db.execute(statement)

    def _init_steady_schema(self):
        # All additions share the schema write transaction. The old trial and
        # legacy guards stay present. A generation owns only future first local
        # INSERTs; an older worker cannot create a steady authorization marker.
        self.db.execute('''CREATE TABLE IF NOT EXISTS weixin_dot_steady_generations (
            generation INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE, boundary_id TEXT NOT NULL,
            request_id TEXT NOT NULL UNIQUE, target_scope TEXT NOT NULL,
            activation_revision INTEGER NOT NULL CHECK(activation_revision>0),
            state TEXT NOT NULL CHECK(state IN ('active','paused')),
            max_sources INTEGER NOT NULL CHECK(max_sources BETWEEN 1 AND 1000),
            created_at REAL NOT NULL, paused_at REAL,
            CHECK((state='active' AND paused_at IS NULL) OR
                  (state='paused' AND paused_at IS NOT NULL)))''')
        self.db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS weixin_dot_steady_one_active
            ON weixin_dot_steady_generations(boundary_id) WHERE state='active' ''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS weixin_dot_steady_sources (
            generation_id TEXT NOT NULL, message_id TEXT NOT NULL,
            PRIMARY KEY(generation_id,message_id))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS weixin_dot_steady_pauses (
            boundary_id TEXT NOT NULL, request_id TEXT NOT NULL,
            generation_id TEXT NOT NULL, applied_revision INTEGER NOT NULL,
            created_at REAL NOT NULL, PRIMARY KEY(boundary_id,request_id))''')
        self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_steady_future_ingress
            AFTER INSERT ON weixin_inbox BEGIN
            INSERT OR IGNORE INTO weixin_dot_steady_sources(generation_id,message_id)
            SELECT g.id,NEW.id FROM weixin_dot_steady_generations g
            JOIN weixin_dot_cutover c ON c.id=g.boundary_id
            WHERE g.state='active' AND c.state='closed'
              AND g.activation_revision=c.admission_revision
              AND g.target_scope=c.target_scope
              AND NEW.account=c.account AND NEW.peer=c.owner
              AND (SELECT count(*) FROM weixin_dot_steady_sources s
                   WHERE s.generation_id=g.id)<g.max_sources;
            END''')
        self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_steady_source_no_update
            BEFORE UPDATE ON weixin_dot_steady_sources
            BEGIN SELECT RAISE(ABORT,'dot_steady_source_immutable'); END''')
        self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_steady_source_no_delete
            BEFORE DELETE ON weixin_dot_steady_sources
            BEGIN SELECT RAISE(ABORT,'dot_steady_source_immutable'); END''')
        self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_steady_generation_no_delete
            BEFORE DELETE ON weixin_dot_steady_generations
            BEGIN SELECT RAISE(ABORT,'dot_steady_generation_immutable'); END''')
        self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_steady_generation_guard
            BEFORE UPDATE ON weixin_dot_steady_generations
            WHEN NEW.generation IS NOT OLD.generation OR NEW.id IS NOT OLD.id
              OR NEW.boundary_id IS NOT OLD.boundary_id
              OR NEW.request_id IS NOT OLD.request_id
              OR NEW.target_scope IS NOT OLD.target_scope
              OR NEW.activation_revision IS NOT OLD.activation_revision
              OR NEW.max_sources IS NOT OLD.max_sources
              OR NEW.created_at IS NOT OLD.created_at
              OR OLD.state!='active' OR NEW.state!='paused'
              OR NEW.paused_at IS NULL
            BEGIN SELECT RAISE(ABORT,'dot_steady_generation_immutable'); END''')
        # Keep the name so supported older code's CREATE IF NOT EXISTS cannot
        # downgrade this rule. Refuse to replace any unknown guard. The check
        # and replacement share the caller's BEGIN IMMEDIATE transaction.
        existing = self.db.execute("""SELECT sql FROM sqlite_master
            WHERE type='trigger' AND name='weixin_dot_trial_authorize_guard'""").fetchone()
        if existing is None:
            raise WeixinError('dot_steady_authorize_guard_missing')
        # sqlite_master removes IF NOT EXISTS from stored CREATE TRIGGER SQL.
        def normalize(sql):
            return ' '.join(sql.replace('CREATE TRIGGER IF NOT EXISTS ',
                                        'CREATE TRIGGER ', 1).split())
        current_sql = normalize(existing['sql'])
        if current_sql == normalize(SINGLETRIAL_AUTHORIZE_GUARD_SQL):
            self.db.execute('DROP TRIGGER weixin_dot_trial_authorize_guard')
            self.db.execute(STEADY_AUTHORIZE_GUARD_SQL)
        elif current_sql != normalize(STEADY_AUTHORIZE_GUARD_SQL):
            raise WeixinError('dot_steady_unknown_authorize_guard')

    def _trial(self):
        return self.db.execute('SELECT * FROM weixin_dot_single_trial WHERE singleton=1').fetchone()

    def _steady(self):
        return self.db.execute('''SELECT * FROM weixin_dot_steady_generations
            ORDER BY generation DESC LIMIT 1''').fetchone()

    def _steady_metadata(self, policy):
        row = self._steady()
        if not row:
            return None
        count = self.db.execute('''SELECT count(*) FROM weixin_dot_steady_sources
            WHERE generation_id=?''', (row['id'],)).fetchone()[0]
        active = bool(policy and policy['state'] == 'closed' and row['state'] == 'active'
                      and policy['id'] == row['boundary_id']
                      and policy['target_scope'] == row['target_scope']
                      and policy['admission_revision'] == row['activation_revision'])
        return {'generation_id': row['id'], 'boundary_id': row['boundary_id'],
                'request_id': row['request_id'], 'target_scope': row['target_scope'],
                'activation_revision': row['activation_revision'], 'state': row['state'],
                'admission_active': active, 'max_sources': row['max_sources'],
                'source_count': count, 'remaining': max(0, row['max_sources'] - count),
                'created_at': row['created_at'], 'paused_at': row['paused_at'],
                'future_basis': 'first_local_insert_during_active_generation',
                'automatic_resend_allowed': False, 'remote_legacy_writers_fenced': False}

    def _trial_metadata(self, policy):
        trial = self._trial()
        if not trial:
            return None
        active = (policy['state'] == 'closed' and trial['selected_message_id'] is not None
                  and policy['admission_revision'] == trial['selection_revision']
                  and policy['target_scope'] == trial['target_scope'])
        return {**dict(trial), 'selection_active': active, 'bulk_admission_permanently_blocked': True,
                'future_basis': 'first_local_insert_after_arm_not_upstream_timestamp',
                'automatic_selection': False, 'automatic_resend_allowed': False,
                'text_limit_bytes': TRIAL_TEXT_MAX_BYTES}

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
                    'steady_version': STEADY_VERSION, 'steady_admission_enabled': False,
                    'steady_generation': None, 'any_new_admission_enabled': False,
                    'new_admission_enabled': False, 'remote_legacy_writers_fenced': False}
        counts = {r[0]: r[1] for r in self.db.execute('''SELECT classification,count(*)
            FROM weixin_dot_cutover_sources WHERE boundary_id=? GROUP BY classification''', (row['id'],))}
        trial = self._trial_metadata(row)
        trial_active = bool(trial and trial['selection_active'])
        steady = self._steady_metadata(row)
        steady_active = bool(steady and steady['admission_active'])
        return {'cutover_version': CUTOVER_VERSION, 'boundary_id': row['id'],
                'steady_version': STEADY_VERSION,
                'state': row['state'], 'target_scope': row['target_scope'],
                'admission_revision': row['admission_revision'], 'closed_at': row['closed_at'],
                'previously_granted_permits_revoked': False,
                'snapshot_count': row['snapshot_count'], 'snapshot_hash': row['snapshot_hash'],
                'legacy_count': counts.get('legacy', 0), 'reserved_new_count': counts.get('reserved_new', 0),
                'new_admission_enabled': row['state'] == 'new_only',
                'single_source_admission_enabled': trial_active,
                'steady_admission_enabled': steady_active,
                'steady_generation': steady,
                'any_new_admission_enabled': row['state'] == 'new_only' or trial_active or steady_active,
                'single_source_trial': trial,
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
        metadata = self._metadata(row)
        trial = metadata['single_source_trial']
        steady = metadata['steady_generation']
        trial_eligible = bool(trial and trial['selection_active']
                              and trial['selected_message_id'] == message_id)
        steady_member = bool(steady and steady['admission_active'] and self.db.execute('''
            SELECT 1 FROM weixin_dot_steady_sources
            WHERE generation_id=? AND message_id=?''',
            (steady['generation_id'], message_id)).fetchone())
        # The ingress inventory can be complete before individual media bytes
        # are cached. Per-attachment readiness is checked at authorize.
        steady_complete = bool(steady_member and
                               self.probe.weixin.inbound_completeness(message_id).get('complete') is True)
        eligible = trial_eligible or steady_complete or (trial is None and row['state'] == 'new_only')
        return {**metadata, 'classification': classification,
                'new_write_eligible': eligible and classification == 'reserved_new',
                'steady_generation_id': (steady['generation_id'] if steady_complete and
                                          classification == 'reserved_new' else None),
                'steady_member': steady_member, 'steady_source_complete': steady_complete}

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
            if self._trial() is not None:
                raise WeixinError('dot_cutover_single_source_trial_required')
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

    @staticmethod
    def _trial_arguments(boundary_id, request_id, expected_revision):
        if (not isinstance(boundary_id, str) or not re.fullmatch(r'djc_[a-f0-9]{32}', boundary_id)
                or not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', request_id)
                or type(expected_revision) is not int or expected_revision < 0):
            raise WeixinError('invalid_dot_trial_arguments')

    @staticmethod
    def _steady_arguments(boundary_id, request_id, expected_revision):
        if (not isinstance(boundary_id, str) or not re.fullmatch(r'djc_[a-f0-9]{32}', boundary_id)
                or not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,256}', request_id)
                or type(expected_revision) is not int or expected_revision < 0):
            raise WeixinError('invalid_dot_steady_arguments')

    def _trial_readback_complete(self, trial):
        # This is positive caller-reported Dot evidence, not independent remote
        # content verification or a phone-delivery assertion.
        if self.db.execute('''SELECT 1 FROM weixin_dot_journal
            WHERE message_id=? AND state IN ('authorized','unknown') LIMIT 1''',
            (trial['selected_message_id'],)).fetchone():
            return False
        rows = self.db.execute('''SELECT part FROM weixin_dot_journal
            WHERE message_id=? AND target_scope=? AND attachment_id=''
              AND part IN ('original','final') AND state='committed'
              AND readback_evidence IS NOT NULL''',
            (trial['selected_message_id'], trial['target_scope'])).fetchall()
        return {r['part'] for r in rows} == {'original', 'final'}

    def enable_steady(self, boundary_id, target_scope, request_id, expected_revision, max_sources):
        self._steady_arguments(boundary_id, request_id, expected_revision)
        if (not isinstance(target_scope, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', target_scope)
                or type(max_sources) is not int or not 1 <= max_sources <= STEADY_MAX_SOURCES):
            raise WeixinError('invalid_dot_steady_arguments')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.policy()
            if not row or row['id'] != boundary_id:
                raise WeixinError('dot_cutover_boundary_mismatch')
            prior = self.db.execute('''SELECT * FROM weixin_dot_steady_generations
                WHERE request_id=?''', (request_id,)).fetchone()
            if prior:
                if (prior['boundary_id'] != boundary_id or prior['target_scope'] != target_scope
                        or prior['max_sources'] != max_sources):
                    raise WeixinError('dot_steady_request_conflict')
                return {**self._metadata(row), 'steady_enable_request_replayed': True,
                        'steady_enable_applied_revision': prior['activation_revision'],
                        'steady_enable_effect_is_current': bool(
                            prior['state'] == 'active' and
                            row['admission_revision'] == prior['activation_revision'])}
            trial = self._trial()
            if (not trial or trial['selected_message_id'] is None
                    or row['state'] != 'closed' or row['target_scope'] != target_scope
                    or trial['target_scope'] != target_scope
                    or row['admission_revision'] <= trial['selection_revision']):
                raise WeixinError('dot_steady_closed_trial_required')
            if not self.db.execute('''SELECT 1 FROM weixin_dot_cutover_closures
                WHERE boundary_id=? AND applied_revision>? LIMIT 1''',
                (boundary_id, trial['selection_revision'])).fetchone():
                raise WeixinError('dot_steady_closed_trial_required')
            if expected_revision != row['admission_revision']:
                raise WeixinError('dot_cutover_revision_conflict')
            if not self._trial_readback_complete(trial):
                raise WeixinError('dot_steady_trial_readback_required')
            if self.db.execute('''SELECT 1 FROM weixin_dot_steady_generations
                WHERE boundary_id=? AND state='active' ''', (boundary_id,)).fetchone():
                # An old worker may have closed the cutover without knowing how
                # to pause a steady generation. Revision mismatch fails closed,
                # but the stale generation still requires an explicit pause.
                raise WeixinError('dot_steady_prior_generation_requires_pause')
            if self.db.execute("SELECT 1 FROM weixin_dot_mirror_epochs WHERE state IN ('staged','active')").fetchone():
                raise WeixinError('dot_cutover_epoch_conflict')
            revision = row['admission_revision'] + 1
            generation_id = 'dsg_' + uuid.uuid4().hex
            self.db.execute('UPDATE weixin_dot_cutover SET admission_revision=? WHERE singleton=1',
                            (revision,))
            self.db.execute('''INSERT INTO weixin_dot_steady_generations
                (id,boundary_id,request_id,target_scope,activation_revision,state,max_sources,created_at)
                VALUES(?,?,?,?,?,'active',?,?)''',
                (generation_id, boundary_id, request_id, target_scope, revision, max_sources, self.clock()))
            return {**self._metadata(self.policy()), 'steady_enable_request_replayed': False,
                    'steady_enable_applied_revision': revision,
                    'steady_enable_effect_is_current': True}

    def pause_steady(self, boundary_id, request_id, expected_revision):
        self._steady_arguments(boundary_id, request_id, expected_revision)
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.policy()
            if not row or row['id'] != boundary_id:
                raise WeixinError('dot_cutover_boundary_mismatch')
            previous = self.db.execute('''SELECT * FROM weixin_dot_steady_pauses
                WHERE boundary_id=? AND request_id=?''', (boundary_id, request_id)).fetchone()
            if previous:
                return {**self._metadata(row), 'steady_pause_request_replayed': True,
                        'steady_pause_applied_revision': previous['applied_revision'],
                        'steady_pause_effect_is_current': bool(
                            row['admission_revision'] == previous['applied_revision'])}
            if expected_revision != row['admission_revision']:
                raise WeixinError('dot_cutover_revision_conflict')
            active = self.db.execute('''SELECT * FROM weixin_dot_steady_generations
                WHERE boundary_id=? AND state='active' ''', (boundary_id,)).fetchone()
            if not active:
                raise WeixinError('dot_steady_not_active')
            now = self.clock()
            revision = row['admission_revision'] + 1
            self.db.execute('''UPDATE weixin_dot_steady_generations
                SET state='paused',paused_at=? WHERE id=? AND state='active' ''',
                (now, active['id']))
            self.db.execute('''UPDATE weixin_dot_cutover SET admission_revision=?,closed_at=?
                WHERE singleton=1''', (revision, now))
            self.db.execute('''INSERT INTO weixin_dot_steady_pauses
                (boundary_id,request_id,generation_id,applied_revision,created_at)
                VALUES(?,?,?,?,?)''', (boundary_id, request_id, active['id'], revision, now))
            return {**self._metadata(self.policy()), 'steady_pause_request_replayed': False,
                    'steady_pause_applied_revision': revision,
                    'steady_pause_effect_is_current': True}

    def arm_trial(self, boundary_id, target_scope, request_id, expected_text_sha256, expected_revision):
        self._trial_arguments(boundary_id, request_id, expected_revision)
        if (not isinstance(target_scope, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', target_scope)
                or not isinstance(expected_text_sha256, str)
                or not re.fullmatch(r'[a-f0-9]{64}', expected_text_sha256)):
            raise WeixinError('invalid_dot_trial_arguments')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.policy()
            if not row or row['id'] != boundary_id:
                raise WeixinError('dot_cutover_boundary_mismatch')
            trial = self._trial()
            if trial:
                if (trial['request_id'] != request_id or trial['target_scope'] != target_scope
                        or trial['expected_text_sha256'] != expected_text_sha256):
                    raise WeixinError('dot_trial_already_exists')
                # An arm retry never moves its boundary or reactivates a closed trial.
                return self._metadata(row)
            if row['state'] != 'closed' or row['admission_revision'] != expected_revision:
                raise WeixinError('dot_cutover_revision_conflict')
            if row['target_scope'] is not None and row['target_scope'] != target_scope:
                raise WeixinError('dot_cutover_scope_conflict')
            if self.db.execute("SELECT 1 FROM weixin_dot_journal WHERE state='authorized'").fetchone():
                raise WeixinError('dot_cutover_authorized_intent_pending')
            if self.db.execute("SELECT 1 FROM weixin_dot_mirror_epochs WHERE state IN ('staged','active')").fetchone():
                raise WeixinError('dot_cutover_epoch_conflict')
            now = self.clock()
            revision = row['admission_revision'] + 1
            self.db.execute('''UPDATE weixin_dot_cutover SET target_scope=?,
                admission_revision=?,closed_at=? WHERE singleton=1''', (target_scope, revision, now))
            self.db.execute('''INSERT INTO weixin_dot_single_trial
                (singleton,boundary_id,request_id,target_scope,expected_text_sha256,arm_revision,created_at)
                VALUES(1,?,?,?,?,?,?)''', (boundary_id, request_id, target_scope, expected_text_sha256, revision, now))
            return self._metadata(self.policy())

    def select_trial(self, boundary_id, request_id, message_id, expected_revision):
        self._trial_arguments(boundary_id, request_id, expected_revision)
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 256:
            raise WeixinError('invalid_dot_trial_arguments')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.policy()
            trial = self._trial()
            if not row or row['id'] != boundary_id or not trial or trial['request_id'] != request_id:
                raise WeixinError('dot_trial_mismatch')
            if trial['selected_message_id'] is not None:
                if trial['selected_message_id'] != message_id:
                    raise WeixinError('dot_trial_selection_immutable')
                # A lost select response may be read after close. No transition,
                # new permit, or reactivation occurs on this path.
                return self._metadata(row)
            if (row['state'] != 'closed' or row['admission_revision'] != expected_revision
                    or expected_revision != trial['arm_revision']):
                raise WeixinError('dot_cutover_revision_conflict')
            future = self.db.execute('''SELECT 1 FROM weixin_dot_trial_future_sources
                WHERE boundary_id=? AND message_id=?''', (boundary_id, message_id)).fetchone()
            source = self.db.execute('''SELECT i.* FROM weixin_inbox i
                JOIN weixin_dot_cutover_sources s ON s.message_id=i.id AND s.boundary_id=?
                WHERE i.id=? AND i.account=? AND i.peer=? AND s.classification='reserved_new' ''',
                (boundary_id, message_id, row['account'], row['owner'])).fetchone()
            if not future or not source:
                raise WeixinError('dot_trial_future_source_required')
            text_bytes = source['text'].encode('utf-8')
            if (not text_bytes or len(text_bytes) > TRIAL_TEXT_MAX_BYTES
                    or hashlib.sha256(text_bytes).hexdigest() != trial['expected_text_sha256']
                    or self.db.execute('SELECT 1 FROM weixin_attachments WHERE message_id=?', (message_id,)).fetchone()
                    or not self.probe.weixin.inbound_completeness(message_id)['complete']):
                raise WeixinError('dot_trial_complete_matching_short_text_required')
            if self.db.execute('''SELECT 1 FROM weixin_dot_journal WHERE message_id=? AND
                    (state!='prepared' OR authorization_history_version!=1 OR authorization_ever_issued!=0
                     OR authorization_id IS NOT NULL OR target_scope!=?)''',
                    (message_id, trial['target_scope'])).fetchone():
                raise WeixinError('dot_trial_prior_intent_requires_reconciliation')
            if self.db.execute('SELECT 1 FROM weixin_dot_mirrors WHERE message_id=?', (message_id,)).fetchone():
                raise WeixinError('dot_trial_prior_intent_requires_reconciliation')
            revision = row['admission_revision'] + 1
            self.db.execute('''UPDATE weixin_dot_single_trial SET selected_message_id=?,
                selection_revision=?,selected_at=? WHERE singleton=1''', (message_id, revision, self.clock()))
            # Bulk state deliberately remains closed. Older code fails closed;
            # only trial-aware classify can grant a permit for this one source.
            self.db.execute('UPDATE weixin_dot_cutover SET admission_revision=? WHERE singleton=1', (revision,))
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
            # This legacy close tool must also close the newer future-only
            # generation. A supported older worker lacks this update, but its
            # revision change still makes steady classification fail closed.
            self.db.execute('''UPDATE weixin_dot_steady_generations
                SET state='paused',paused_at=?
                WHERE boundary_id=? AND state='active' ''', (now, boundary_id))
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
        if name == 'arm_weixin_dot_single_source_trial' and set(arguments) == {
                'boundary_id', 'target_scope', 'request_id', 'expected_text_sha256', 'expected_revision'}:
            return self.arm_trial(**arguments)
        if name == 'select_weixin_dot_single_source_trial' and set(arguments) == {
                'boundary_id', 'request_id', 'message_id', 'expected_revision'}:
            return self.select_trial(**arguments)
        if name == 'enable_weixin_dot_steady_future_sources' and set(arguments) == {
                'boundary_id', 'target_scope', 'request_id', 'expected_revision', 'max_sources'}:
            return self.enable_steady(**arguments)
        if name == 'pause_weixin_dot_steady_future_sources' and set(arguments) == {
                'boundary_id', 'request_id', 'expected_revision'}:
            return self.pause_steady(**arguments)
        raise WeixinError('invalid_dot_cutover_arguments')
