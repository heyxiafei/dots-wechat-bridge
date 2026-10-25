"""Local ingress epochs. No network, remote history proof, or writer fence adapter.

Only trusted process construction may inject a verifier. An MCP caller supplies
an opaque lookup reference, never a fence assertion or a dot receipt as proof.
"""
import hashlib
import re
import secrets

try:
    from .weixin import WeixinError, encoded
except ImportError:
    from weixin import WeixinError, encoded

EPOCH_VERSION = 'epoch-v1'
_ID = r'dme_[a-f0-9]{32}'


def epoch_tools():
    text = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    identity = {'type': 'string', 'pattern': '^' + _ID + '$'}
    definitions = [
        ('stage_weixin_dot_mirror_epoch',
         'Stage one owner-scoped local ingress epoch and mark every existing owner inbox ID legacy, including messages awaiting media or events. Sends nothing. Staging grants no write permission. Repeated request_id is idempotent; another staged/active epoch conflicts.',
         {'target_scope': text, 'request_id': text}),
        ('activate_weixin_dot_mirror_epoch',
         'Request activation using an opaque proof lookup reference. A trusted injected verifier must establish the exact epoch/nonce/target scope, persistent fencing of all legacy entrypoints, complete source custody, and zero inflight writes. Caller booleans and new dot message IDs are not proof. No verifier is installed by default: activation remains pending. Sends nothing.',
         {'epoch_id': identity, 'proof_ref': text}),
        ('get_weixin_dot_mirror_epoch',
         'Read local epoch metadata, snapshot digest/count and verifier availability. No message bodies or credentials. Active/future are local admission metadata, never proof of remote dot visibility or absence of historical display.',
         {'epoch_id': identity}),
        ('get_weixin_dot_mirror_scope',
         'Classify one exact owner inbox ID against the current local epoch. future means first local ingestion after a verified fence, including possibly late upstream messages; it is not remote history verification or communication authorization. Inactive/legacy require history reconciliation.',
         {'message_id': text}),
    ]
    return [{'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': properties,
                             'required': list(properties), 'additionalProperties': False},
             'annotations': {'readOnlyHint': name.startswith('get_'),
                             'destructiveHint': False, 'idempotentHint': True,
                             'openWorldHint': False}}
            for name, description, properties in definitions]


class DotMirrorEpochStore:
    def __init__(self, probe, verifier=None):
        self.probe, self.db, self.lock, self.clock = probe, probe.db, probe.lock, probe.clock
        self.verifier = verifier
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS weixin_dot_mirror_epochs (
                generation INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE, account TEXT NOT NULL, owner TEXT NOT NULL,
                target_scope TEXT NOT NULL, request_id TEXT NOT NULL, nonce TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('staged','active','suspended')),
                snapshot_count INTEGER NOT NULL, snapshot_hash TEXT NOT NULL,
                created_at REAL NOT NULL, activated_at REAL, suspended_at REAL,
                evidence_ref_sha256 TEXT, proof_ref_sha256 TEXT,
                UNIQUE(account,owner,request_id));
            CREATE UNIQUE INDEX IF NOT EXISTS weixin_dot_mirror_epoch_current
                ON weixin_dot_mirror_epochs(account,owner)
                WHERE state IN ('staged','active');
            CREATE TABLE IF NOT EXISTS weixin_dot_mirror_epoch_members (
                epoch_id TEXT NOT NULL, message_id TEXT NOT NULL,
                classification TEXT NOT NULL CHECK(classification IN ('legacy','future')),
                PRIMARY KEY(epoch_id,message_id));
            CREATE INDEX IF NOT EXISTS weixin_dot_mirror_epoch_seen
                ON weixin_dot_mirror_epoch_members(message_id,epoch_id);
            CREATE TRIGGER IF NOT EXISTS weixin_dot_mirror_epoch_ingress
                AFTER INSERT ON weixin_inbox BEGIN
                INSERT OR IGNORE INTO weixin_dot_mirror_epoch_members
                    (epoch_id,message_id,classification)
                SELECT e.id,NEW.id,
                    CASE WHEN EXISTS (
                        SELECT 1 FROM weixin_dot_mirror_epoch_members m
                        JOIN weixin_dot_mirror_epochs old ON old.id=m.epoch_id
                        WHERE m.message_id=NEW.id AND old.account=NEW.account
                            AND old.owner=NEW.peer)
                    THEN 'legacy' ELSE 'future' END
                FROM weixin_dot_mirror_epochs e
                WHERE e.state='active' AND e.account=NEW.account AND e.owner=NEW.peer;
            END;
        ''')
        self.db.commit()

    @property
    def verifier_available(self):
        return callable(getattr(self.verifier, 'verify', None))

    @staticmethod
    def _text(value):
        return (isinstance(value, str) and 0 < len(value) <= 256
                and value.strip() == value and bool(value.strip())
                and not any(ord(c) < 32 or ord(c) == 127 for c in value))

    @staticmethod
    def _digest(value):
        return hashlib.sha256(encoded(value).encode()).hexdigest()

    def _owner(self):
        # Metadata-only: never select bot/context credentials or message text.
        rows = self.db.execute('SELECT id,owner FROM weixin_accounts LIMIT 2').fetchall()
        if not rows:
            raise WeixinError('not_bound')
        if len(rows) != 1:
            raise WeixinError('dot_mirror_epoch_owner_ambiguous')
        return rows[0]

    def _epoch(self, epoch_id, owner):
        if not isinstance(epoch_id, str) or not re.fullmatch(_ID, epoch_id):
            raise WeixinError('invalid_dot_mirror_epoch_id')
        row = self.db.execute('SELECT * FROM weixin_dot_mirror_epochs WHERE id=? AND account=? AND owner=?',
                              (epoch_id, owner['id'], owner['owner'])).fetchone()
        if not row:
            raise WeixinError('unknown_dot_mirror_epoch')
        return row

    def _capture_legacy(self, epoch_id, owner):
        self.db.execute('''INSERT OR IGNORE INTO weixin_dot_mirror_epoch_members
            (epoch_id,message_id,classification)
            SELECT ?,id,'legacy' FROM weixin_inbox WHERE account=? AND peer=?''',
            (epoch_id, owner['id'], owner['owner']))
        ids = [r['message_id'] for r in self.db.execute('''SELECT message_id
            FROM weixin_dot_mirror_epoch_members WHERE epoch_id=? AND classification='legacy'
            ORDER BY message_id''', (epoch_id,))]
        self.db.execute('UPDATE weixin_dot_mirror_epochs SET snapshot_count=?,snapshot_hash=? WHERE id=?',
                        (len(ids), self._digest(ids), epoch_id))

    def _metadata(self, row):
        future = self.db.execute("SELECT count(*) FROM weixin_dot_mirror_epoch_members WHERE epoch_id=? AND classification='future'",
                                 (row['id'],)).fetchone()[0]
        return {'epoch_id': row['id'], 'state': row['state'], 'target_scope': row['target_scope'],
                'request_id': row['request_id'], 'nonce': row['nonce'],
                'snapshot_count': row['snapshot_count'], 'snapshot_hash': row['snapshot_hash'],
                'future_count': future, 'verifier_available': self.verifier_available,
                'activation_pending': row['state'] == 'staged',
                'activation_evidence_recorded': row['evidence_ref_sha256'] is not None,
                'evidence_ref_sha256': row['evidence_ref_sha256'],
                'epoch_version': EPOCH_VERSION, 'remote_history_verified': False,
                'dot_visible_verified_by_bridge': False,
                'metadata_basis': 'local_first_ingest_after_verified_fence'}

    def stage(self, target_scope, request_id):
        if not all(self._text(x) for x in (target_scope, request_id)):
            raise WeixinError('invalid_dot_mirror_epoch_arguments')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            owner = self._owner()
            old = self.db.execute('SELECT * FROM weixin_dot_mirror_epochs WHERE account=? AND owner=? AND request_id=?',
                                  (owner['id'], owner['owner'], request_id)).fetchone()
            if old:
                if old['target_scope'] != target_scope:
                    raise WeixinError('dot_mirror_epoch_scope_conflict')
                return self._metadata(old)
            if self.db.execute("SELECT 1 FROM weixin_dot_mirror_epochs WHERE account=? AND owner=? AND state IN ('staged','active')",
                               (owner['id'], owner['owner'])).fetchone():
                raise WeixinError('dot_mirror_epoch_already_pending_or_active')
            epoch_id = 'dme_' + secrets.token_hex(16)
            self.db.execute('''INSERT INTO weixin_dot_mirror_epochs
                (id,account,owner,target_scope,request_id,nonce,state,snapshot_count,snapshot_hash,created_at)
                VALUES(?,?,?,?,?,?,'staged',0,?,?)''',
                (epoch_id, owner['id'], owner['owner'], target_scope, request_id,
                 secrets.token_hex(32), self._digest([]), self.clock()))
            self._capture_legacy(epoch_id, owner)
            return self._metadata(self._epoch(epoch_id, owner))

    def activate(self, epoch_id, proof_ref):
        if not self._text(proof_ref) or proof_ref.startswith('Sentinel_'):
            raise WeixinError('invalid_dot_mirror_epoch_proof_reference')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            owner = self._owner()
            row = self._epoch(epoch_id, owner)
            if row['state'] == 'suspended':
                raise WeixinError('dot_mirror_epoch_suspended')
            if row['state'] == 'active':
                if row['proof_ref_sha256'] != self._digest(proof_ref):
                    raise WeixinError('dot_mirror_epoch_proof_conflict')
                return self._metadata(row)
            if not self.verifier_available:
                raise WeixinError('dot_mirror_epoch_activation_pending')
            try:
                proof = self.verifier.verify(row['id'], row['nonce'], row['target_scope'], proof_ref)
            except Exception:
                raise WeixinError('dot_mirror_epoch_proof_unverified') from None
            required = {'epoch_id', 'nonce', 'target_scope', 'old_writers_fenced',
                        'fence_persistent', 'all_legacy_entrypoints_fenced',
                        'source_custody_complete', 'inflight_count', 'evidence_ref'}
            if (not isinstance(proof, dict) or set(proof) != required
                    or proof['epoch_id'] != row['id'] or proof['nonce'] != row['nonce']
                    or proof['target_scope'] != row['target_scope']
                    or proof['old_writers_fenced'] is not True
                    or proof['fence_persistent'] is not True
                    or proof['all_legacy_entrypoints_fenced'] is not True
                    or proof['source_custody_complete'] is not True
                    or type(proof['inflight_count']) is not int or proof['inflight_count'] != 0
                    or not self._text(proof['evidence_ref'])
                    or proof['evidence_ref'].startswith('Sentinel_')):
                raise WeixinError('dot_mirror_epoch_proof_unverified')
            # The write transaction spans verification, the gap snapshot and the
            # active commit. Other connections' INSERTs either precede this
            # snapshot or execute the already-installed AFTER INSERT trigger.
            self._capture_legacy(epoch_id, owner)
            self.db.execute("UPDATE weixin_dot_mirror_epochs SET state='active',activated_at=?,evidence_ref_sha256=?,proof_ref_sha256=? WHERE id=? AND state='staged'",
                            (self.clock(), self._digest(proof['evidence_ref']), self._digest(proof_ref), epoch_id))
            return self._metadata(self._epoch(epoch_id, owner))

    def get(self, epoch_id):
        with self.lock:
            return self._metadata(self._epoch(epoch_id, self._owner()))

    def classify(self, message_id):
        if not self._text(message_id):
            raise WeixinError('invalid_dot_mirror_epoch_arguments')
        with self.lock:
            owner = self._owner()
            if not self.db.execute('SELECT 1 FROM weixin_inbox WHERE id=? AND account=? AND peer=?',
                                   (message_id, owner['id'], owner['owner'])).fetchone():
                raise WeixinError('unknown_owner_message')
            row = self.db.execute('''SELECT * FROM weixin_dot_mirror_epochs
                WHERE account=? AND owner=? ORDER BY generation DESC LIMIT 1''',
                (owner['id'], owner['owner'])).fetchone()
            member = (self.db.execute('SELECT classification FROM weixin_dot_mirror_epoch_members WHERE epoch_id=? AND message_id=?',
                                      (row['id'], message_id)).fetchone() if row else None)
            classification = member['classification'] if member else 'inactive'
            eligible = bool(row and row['state'] == 'active' and classification == 'future')
            return {'epoch_id': row['id'] if row else None, 'state': row['state'] if row else 'inactive',
                    'classification': classification, 'history_scan_required': not eligible,
                    'new_write_eligible': eligible, 'epoch_version': EPOCH_VERSION,
                    'target_scope': row['target_scope'] if row else None,
                    'metadata_basis': 'local_first_ingest_after_verified_fence',
                    'remote_history_verified': False, 'communication_authorization_granted': False}

    def suspend_for_restart(self):
        # The daemon calls this before ingestion/claims. Read-only constructors
        # never suspend another process's epoch; a suspended epoch cannot revive.
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            count = self.db.execute("UPDATE weixin_dot_mirror_epochs SET state='suspended',suspended_at=? WHERE state IN ('active','staged')",
                                    (self.clock(),)).rowcount
        return {'suspended_count': count}

    def call(self, name, arguments):
        methods = {'stage_weixin_dot_mirror_epoch': self.stage,
                   'activate_weixin_dot_mirror_epoch': self.activate,
                   'get_weixin_dot_mirror_epoch': self.get,
                   'get_weixin_dot_mirror_scope': self.classify}
        spec = next((t['inputSchema'] for t in epoch_tools() if t['name'] == name), None)
        if (not spec or not isinstance(arguments, dict) or set(arguments) != set(spec['required'])
                or not all(self._text(value) for value in arguments.values())):
            raise WeixinError('invalid_dot_mirror_epoch_arguments')
        return methods[name](**arguments)
