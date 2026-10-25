"""Per-part mirror intents. Metadata only; no remote writer or real fence adapter.

A local permit is single-use coordination for compliant callers, not a capability
that the dot API enforces. The default epoch verifier is absent, so first sends
remain blocked. Positive receipts never authorize a resend.
"""
import hashlib
import json
import re
import uuid

try:
    from .weixin import WeixinError, encoded
    from .dotepoch import DotMirrorEpochStore
    from .dotcutover import DotCutoverStore
except ImportError:
    from weixin import WeixinError, encoded
    from dotepoch import DotMirrorEpochStore
    from dotcutover import DotCutoverStore

JOURNAL_VERSION = 'mirror-journal-v1'
SOURCE = 'owner_weixin_dm'


def journal_tools():
    text = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    jid = {'type': 'string', 'pattern': '^djm_[a-f0-9]{32}$'}
    attempt = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
    definitions = [
        ('prepare_weixin_dot_journal',
         'Persist one owner source/message/part/attachment intent. Never grants a dot write, even on first preparation. An empty journal does not prove remote absence. Authorization requires either verified-fence admission or the explicitly risk-accepted bounded local new-source policy. Local ownership never proves remote old writers stopped. Live duplicate preparations preserve the attempt. Only an expired or restart-invalidated prepared intent that never received authorization may recover with a new attempt; all old attempts then fail. Authorized or explicitly unknown intents never restart. No bodies or credentials are stored.',
         {'source': {'type': 'string', 'enum': [SOURCE]}, 'message_id': text,
          'event_id': text, 'part': {'type': 'string', 'enum': ['original', 'ack', 'final']},
          'target_scope': text, 'attachment_id': {'type': 'string', 'pattern': '^att_[a-f0-9]{32}$'},
          'lease_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 300}},
         ['source', 'message_id', 'event_id', 'part', 'target_scope']),
        ('authorize_weixin_dot_journal',
         'Atomically issue at most one local send permit for the exact prepared attempt, before its lease expires and only after either verified-fence admission or enabled bounded local ownership admits this new source. A duplicate or lost authorization response never grants another permit. This does not fence the remote dot API or override user/platform communication restrictions. Bounded mode retains old remote direct-write risk; no real remote fence adapter is included.',
         {'journal_id': jid, 'attempt_id': attempt, 'event_id': text},
         ['journal_id', 'attempt_id', 'event_id']),
        ('commit_weixin_dot_journal',
         'Record a real dot receipt for the same attempt. API response evidence requires an authorized non-expired attempt; unknown requires a positive correlated supported history readback with partial=false. A missing result is never evidence for resend. Same target is idempotent, conflicting targets fail. Evidence contains IDs and exact mirror metadata only, never bodies. The bridge records caller reports, not remote verification.',
         {'journal_id': jid, 'attempt_id': attempt, 'event_id': text,
          'target_dot_message_id': {'type': 'string', 'pattern': '^Sentinel_[a-f0-9]{32}$'},
          'receipt_source': {'type': 'string', 'enum': ['api_response', 'history_readback']},
          'evidence': {'type': 'object', 'properties': {
              'method': {'type': 'string', 'enum': ['user_message.send_message', 'user_message.read_messages']},
              'target_scope': text,
              'target_dot_message_id': {'type': 'string', 'pattern': '^Sentinel_[a-f0-9]{32}$'},
              'message_metadata': {'type': 'object'}, 'partial': {'type': 'boolean'}},
              'required': ['method', 'target_scope', 'target_dot_message_id', 'message_metadata'],
              'additionalProperties': False}},
         ['journal_id', 'attempt_id', 'event_id', 'target_dot_message_id', 'receipt_source', 'evidence']),
        ('mark_weixin_dot_journal_unknown',
         'Persist an uncertain intent without granting a retry. Keeps its original attempt and any late receipt correlation. A committed receipt cannot be downgraded.',
         {'journal_id': jid, 'attempt_id': attempt}, ['journal_id', 'attempt_id']),
        ('get_weixin_dot_journal_status',
         'Read exact owner message intent/receipt metadata. Never claims, renews leases or grants writes. A preparation_expired effective state means a never-authorized prepared intent needs an explicit prepare call with a new attempt before authorization. Unknown or ever-authorized intents are never recoverable. Positive caller-reported readback is shown separately; tracked does not mean every part is visible. Legacy receipt rows remain separate and are not an absence proof.',
         {'message_id': text}, ['message_id']),
    ]
    return [{'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': properties,
                             'required': required, 'additionalProperties': False},
             'annotations': {'readOnlyHint': name.startswith('get_'),
                             'destructiveHint': False, 'idempotentHint': True,
                             'openWorldHint': False}}
            for name, description, properties, required in definitions]


class DotMirrorJournal:
    def __init__(self, probe, fence_verifier=None):
        self.probe, self.db, self.lock, self.clock = probe, probe.db, probe.lock, probe.clock
        # Reuse the audited exact-ID snapshot and default-deny activation gate.
        # Only trusted process construction can supply an adapter; MCP cannot.
        self.epochs = DotMirrorEpochStore(probe, verifier=fence_verifier)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS weixin_dot_journal (
            id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source='owner_weixin_dm'),
            account TEXT NOT NULL, owner TEXT NOT NULL, message_id TEXT NOT NULL,
            event_id TEXT NOT NULL, part TEXT NOT NULL CHECK(part IN ('original','ack','final')),
            attachment_id TEXT NOT NULL DEFAULT '', target_scope TEXT NOT NULL,
            attempt_id TEXT NOT NULL, state TEXT NOT NULL
                CHECK(state IN ('prepared','authorized','unknown','committed')),
            lease_until REAL NOT NULL, authorization_id TEXT,
            target_dot_message_id TEXT, receipt_source TEXT
                CHECK(receipt_source IN ('api_response','history_readback')),
            receipt_evidence TEXT, readback_evidence TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            direction TEXT NOT NULL DEFAULT 'weixin_to_dot' CHECK(direction='weixin_to_dot'),
            suppress_reflection INTEGER NOT NULL DEFAULT 1 CHECK(suppress_reflection=1),
            UNIQUE(source,account,owner,message_id,part,attachment_id),
            CHECK((state='committed' AND target_dot_message_id IS NOT NULL
                       AND receipt_source IS NOT NULL AND receipt_evidence IS NOT NULL)
               OR (state IN ('prepared','authorized','unknown') AND target_dot_message_id IS NULL
                       AND receipt_source IS NULL AND receipt_evidence IS NULL)),
            CHECK(state!='authorized' OR authorization_id IS NOT NULL)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS weixin_dot_journal_target
            ON weixin_dot_journal(target_scope,target_dot_message_id)
            WHERE target_dot_message_id IS NOT NULL;''')
        self.db.commit()
        # Additive migration preserves v1 rows and CHECK constraints. A legacy
        # NULL permit is not a complete authorization history. Only intents born
        # under the protected protocol have positive never-authorized evidence.
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in self.db.execute('PRAGMA table_info(weixin_dot_journal)')}
            if 'preparation_recovery_required' not in columns:
                self.db.execute('''ALTER TABLE weixin_dot_journal ADD COLUMN
                    preparation_recovery_required INTEGER NOT NULL DEFAULT 0
                    CHECK(preparation_recovery_required IN (0,1))''')
            if 'authorization_history_version' not in columns:
                self.db.execute('''ALTER TABLE weixin_dot_journal ADD COLUMN
                    authorization_history_version INTEGER NOT NULL DEFAULT 0
                    CHECK(authorization_history_version IN (0,1))''')
            if 'authorization_ever_issued' not in columns:
                self.db.execute('''ALTER TABLE weixin_dot_journal ADD COLUMN
                    authorization_ever_issued INTEGER NOT NULL DEFAULT 0
                    CHECK(authorization_ever_issued IN (0,1))''')
                # Existing positive evidence is retained; absence is not turned
                # into known history by this migration.
                self.db.execute('''UPDATE weixin_dot_journal
                    SET authorization_ever_issued=1 WHERE authorization_id IS NOT NULL''')
            self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_journal_history_guard
                BEFORE UPDATE ON weixin_dot_journal
                WHEN NEW.authorization_history_version != OLD.authorization_history_version
                  OR NEW.authorization_ever_issued < OLD.authorization_ever_issued
                  OR (OLD.authorization_id IS NOT NULL AND NEW.authorization_id IS NOT OLD.authorization_id)
                  OR (OLD.state IN ('authorized','unknown','committed') AND NEW.state='prepared')
                BEGIN SELECT RAISE(ABORT,'dot_journal_authorization_history_immutable'); END''')
            self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_journal_authorize_guard
                BEFORE UPDATE ON weixin_dot_journal
                WHEN (NEW.state='authorized' AND OLD.state!='authorized')
                  OR (NEW.authorization_id IS NOT NULL AND OLD.authorization_id IS NULL)
                BEGIN
                  SELECT CASE WHEN OLD.state!='prepared'
                    OR OLD.authorization_history_version!=1 OR OLD.authorization_ever_issued!=0
                    OR OLD.authorization_id IS NOT NULL OR OLD.preparation_recovery_required!=0
                    OR NEW.state!='authorized' OR NEW.authorization_id IS NULL
                    OR NEW.authorization_ever_issued!=1 OR NEW.attempt_id!=OLD.attempt_id
                    THEN RAISE(ABORT,'dot_journal_authorization_history_required') END;
                END''')
            self.db.execute('''CREATE TRIGGER IF NOT EXISTS weixin_dot_journal_attempt_guard
                BEFORE UPDATE OF attempt_id ON weixin_dot_journal
                WHEN NEW.attempt_id != OLD.attempt_id AND (
                    OLD.state!='prepared' OR NEW.state!='prepared'
                    OR OLD.authorization_history_version!=1 OR OLD.authorization_ever_issued!=0
                    OR OLD.authorization_id IS NOT NULL OR NEW.authorization_id IS NOT NULL
                    OR OLD.preparation_recovery_required!=1 OR NEW.preparation_recovery_required!=0)
                BEGIN SELECT RAISE(ABORT,'dot_journal_attempt_recovery_required'); END''')
        self.cutover = DotCutoverStore(probe)

    @staticmethod
    def _text(value):
        return isinstance(value, str) and bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}', value))

    def _source(self, source, message_id, event_id, part, attachment_id):
        if source != SOURCE:
            raise WeixinError('invalid_dot_journal_source')
        if not all(self._text(v) for v in (message_id, event_id)):
            raise WeixinError('invalid_dot_journal_identity')
        if len(self.db.execute('SELECT id FROM weixin_accounts LIMIT 2').fetchall()) != 1:
            raise WeixinError('dot_journal_owner_unavailable')
        account, linked = self.probe.dot_mirrors.owner_message(message_id, event_id)
        aid = self.probe.dot_mirrors.validate_part(account, message_id, part, attachment_id)
        return account, linked, aid

    def _load(self, journal_id, attempt_id=None, event_id=None):
        if not isinstance(journal_id, str) or not re.fullmatch(r'djm_[a-f0-9]{32}', journal_id):
            raise WeixinError('invalid_dot_journal_id')
        row = self.db.execute('SELECT * FROM weixin_dot_journal WHERE id=?', (journal_id,)).fetchone()
        if not row:
            raise WeixinError('unknown_dot_journal')
        account, _, _ = self._source(row['source'], row['message_id'], row['event_id'],
                                     row['part'], row['attachment_id'] or None)
        if row['account'] != account['id'] or row['owner'] != account['owner']:
            raise WeixinError('dot_journal_owner_mismatch')
        if ((attempt_id is not None and attempt_id != row['attempt_id'])
                or (event_id is not None and event_id != row['event_id'])):
            raise WeixinError('dot_journal_attempt_mismatch')
        return row

    @staticmethod
    def _mirror_metadata(row):
        return {'weixin_dot_journal': {'version': JOURNAL_VERSION, 'journal_id': row['id'],
            'attempt_id': row['attempt_id'], 'source': row['source'],
            'message_id': row['message_id'], 'part': row['part'],
            'attachment_id': row['attachment_id'] or None,
            'direction': 'weixin_to_dot', 'suppress_reflection': True}}

    def _metadata(self, row):
        expired = row['state'] in ('prepared', 'authorized') and self.clock() >= row['lease_until']
        history_known = row['authorization_history_version'] == 1
        never_authorized = self._never_authorized(row)
        preparation_expired = (row['state'] == 'prepared' and never_authorized
                               and (expired or bool(row['preparation_recovery_required'])))
        effective_state = ('authorization_history_unknown' if row['state'] == 'prepared' and not history_known
                           else 'preparation_expired' if preparation_expired
                           else 'unknown' if expired else row['state'])
        return {'journal_id': row['id'], 'source': row['source'], 'message_id': row['message_id'],
            'event_id': row['event_id'], 'part': row['part'], 'attachment_id': row['attachment_id'] or None,
            'target_scope': row['target_scope'], 'attempt_id': row['attempt_id'],
            'authorization_id': row['authorization_id'], 'state': row['state'],
            'effective_state': effective_state, 'lease_expired': expired,
            'preparation_recovery_required': preparation_expired,
            'authorization_history_known': history_known,
            'authorization_ever_issued': (True if row['authorization_ever_issued'] or row['authorization_id']
                                         else False if history_known else None),
            'preparation_recovery_eligible': row['state'] == 'prepared' and never_authorized,
            'lease_until': row['lease_until'], 'target_dot_message_id': row['target_dot_message_id'],
            'receipt_source': row['receipt_source'], 'positive_readback_recorded': bool(row['readback_evidence']),
            'receipt_evidence': json.loads(row['receipt_evidence']) if row['receipt_evidence'] else None,
            'readback_evidence': json.loads(row['readback_evidence']) if row['readback_evidence'] else None,
            'message_metadata': self._mirror_metadata(row), 'direction': 'weixin_to_dot',
            'suppress_reflection': True, 'claim_granted': False, 'external_write_allowed': False,
            'automatic_resend_allowed': False, 'receipt_basis': 'caller_report',
            'target_id_verified_by_bridge': False, 'dot_visible_verified_by_bridge': False}

    @staticmethod
    def _never_authorized(row):
        return (row['authorization_history_version'] == 1
                and row['authorization_ever_issued'] == 0 and row['authorization_id'] is None)

    def guard_legacy_prepare(self, message_id, attachment_id, part):
        # Called inside v1's same BEGIN IMMEDIATE before its INSERT. This blocks
        # concurrent local bypass, but cannot revoke a previously obtained API capability.
        if self.cutover.policy() is not None:
            raise WeixinError('dot_journal_legacy_prepare_fenced')
        if self.db.execute('SELECT 1 FROM weixin_dot_journal WHERE message_id=? AND attachment_id=? AND part=?',
                           (message_id, attachment_id, part)).fetchone():
            raise WeixinError('dot_journal_legacy_prepare_fenced')
        if self.db.execute('SELECT 1 FROM weixin_dot_mirror_epochs LIMIT 1').fetchone():
            raise WeixinError('dot_journal_legacy_prepare_fenced')

    def prepare(self, source, message_id, event_id, part, target_scope, attachment_id=None, lease_seconds=120):
        if not self._text(target_scope) or type(lease_seconds) is not int or not 1 <= lease_seconds <= 300:
            raise WeixinError('invalid_dot_journal_arguments')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            account, _, aid = self._source(source, message_id, event_id, part, attachment_id)
            if self.db.execute('SELECT 1 FROM weixin_dot_mirrors WHERE message_id=? AND attachment_id=? AND phase=?',
                               (message_id, aid, part)).fetchone():
                raise WeixinError('dot_journal_legacy_record_requires_reconciliation')
            identity = 'djm_' + hashlib.sha256(encoded([source, account['id'], account['owner'],
                                                       message_id, part, aid]).encode()).hexdigest()[:32]
            now = self.clock()
            created = self.db.execute('''INSERT OR IGNORE INTO weixin_dot_journal
                (id,source,account,owner,message_id,event_id,part,attachment_id,target_scope,
                 attempt_id,state,lease_until,created_at,updated_at,authorization_history_version)
                VALUES(?,?,?,?,?,?,?,?,?,?,'prepared',?,?,?,1)''',
                (identity, source, account['id'], account['owner'], message_id, event_id, part, aid,
                 target_scope, uuid.uuid4().hex, now + lease_seconds, now, now)).rowcount == 1
            row = self._load(identity)
            if row['event_id'] != event_id or row['target_scope'] != target_scope:
                raise WeixinError('dot_journal_scope_or_event_conflict')
            row = self._expire(row)
            recovered = False
            if (row['state'] == 'prepared' and self._never_authorized(row)
                    and row['preparation_recovery_required']):
                # No permit was ever issued for this attempt. BEGIN IMMEDIATE
                # serializes this rotation against every authorize/commit and
                # competing prepare. The stable intent key never changes; all
                # delayed operations with the retired attempt fail in _load.
                recovered = self.db.execute('''UPDATE weixin_dot_journal
                    SET attempt_id=?,lease_until=?,preparation_recovery_required=0,updated_at=?
                    WHERE id=? AND attempt_id=? AND state='prepared'
                        AND authorization_id IS NULL AND preparation_recovery_required=1
                        AND authorization_history_version=1 AND authorization_ever_issued=0''',
                    (uuid.uuid4().hex, now + lease_seconds, now, identity, row['attempt_id'])).rowcount == 1
                row = self._load(identity)
            return {**self._metadata(row), 'first_prepare': created,
                    'preparation_recovered': recovered,
                    'authorization_required': True, 'journal_version': JOURNAL_VERSION}

    def _expire(self, row):
        if row['state'] in ('prepared', 'authorized') and self.clock() >= row['lease_until']:
            if row['state'] == 'prepared' and self._never_authorized(row):
                # Latch the expiration so a backwards wall-clock adjustment
                # cannot revive this attempt. Status readers only project it;
                # prepare is the sole operation that may rotate an unsent key.
                self.db.execute('''UPDATE weixin_dot_journal
                    SET preparation_recovery_required=1,updated_at=?
                    WHERE id=? AND state='prepared' AND authorization_id IS NULL
                        AND preparation_recovery_required=0''', (self.clock(), row['id']))
            else:
                self.db.execute("UPDATE weixin_dot_journal SET state='unknown',updated_at=? WHERE id=? AND state IN ('prepared','authorized')",
                                (self.clock(), row['id']))
            return self.db.execute('SELECT * FROM weixin_dot_journal WHERE id=?', (row['id'],)).fetchone()
        return row

    def authorize(self, journal_id, attempt_id, event_id):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self._expire(self._load(journal_id, attempt_id, event_id))
            if (row['state'] != 'prepared' or not self._never_authorized(row)
                    or row['preparation_recovery_required']):
                return {**self._metadata(row), 'send_authorized': False}
            bounded = self.cutover.classify(row['message_id'])
            scope = bounded if bounded is not None else self.epochs.classify(row['message_id'])
            if (not scope['new_write_eligible'] or scope['target_scope'] != row['target_scope']):
                raise WeixinError('dot_journal_fence_or_source_admission_pending')
            permit = uuid.uuid4().hex
            self.db.execute("UPDATE weixin_dot_journal SET state='authorized',authorization_id=?,authorization_ever_issued=1,updated_at=? WHERE id=? AND state='prepared' AND attempt_id=?",
                            (permit, self.clock(), journal_id, attempt_id))
            row = self._load(journal_id, attempt_id, event_id)
            return {**self._metadata(row), 'send_authorized': True, 'claim_granted': True,
                    'external_write_allowed': True, 'communication_authorization_granted': False,
                    'remote_api_fenced_by_bridge': False, 'remote_legacy_writers_fenced': False,
                    'admission_mode': 'bounded_local_ownership' if bounded is not None else 'verified_fence',
                    'remote_legacy_direct_write_risk': bounded is not None}

    def _evidence(self, row, target, source, evidence):
        method = 'user_message.send_message' if source == 'api_response' else 'user_message.read_messages'
        required = {'method', 'target_scope', 'target_dot_message_id', 'message_metadata'}
        if source == 'history_readback':
            required.add('partial')
        if not isinstance(evidence, dict):
            raise WeixinError('dot_journal_receipt_evidence_mismatch')
        try:
            # Stable ordering, but strict JSON types (1 must not equal true).
            supplied_metadata = json.dumps(evidence.get('message_metadata'), sort_keys=True, allow_nan=False)
            expected_metadata = json.dumps(self._mirror_metadata(row), sort_keys=True, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise WeixinError('dot_journal_receipt_evidence_mismatch') from None
        if (set(evidence) != required or evidence.get('method') != method
                or evidence.get('target_scope') != row['target_scope']
                or evidence.get('target_dot_message_id') != target
                or supplied_metadata != expected_metadata
                or (source == 'history_readback' and evidence.get('partial') is not False)):
            raise WeixinError('dot_journal_receipt_evidence_mismatch')
        return encoded(evidence)

    def commit(self, journal_id, attempt_id, event_id, target_dot_message_id, receipt_source, evidence):
        if (not isinstance(target_dot_message_id, str)
                or not re.fullmatch(r'Sentinel_[a-f0-9]{32}', target_dot_message_id)
                or receipt_source not in ('api_response', 'history_readback')):
            raise WeixinError('invalid_dot_journal_receipt')
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self._expire(self._load(journal_id, attempt_id, event_id))
            # A valid caller that observes expiry must latch unknown even when
            # its subsequent receipt is rejected. Raising inside the connection
            # context would otherwise roll that safety transition back.
            failure = None
            try:
                result = self._commit_receipt(row, target_dot_message_id, receipt_source, evidence)
            except WeixinError as error:
                failure = error
        if failure is not None:
            raise failure
        return result

    def _commit_receipt(self, row, target_dot_message_id, receipt_source, evidence):
        if row['state'] == 'prepared' and row['preparation_recovery_required']:
            # An expired/restart-invalidated preparation never had a permit.
            # Do not accept a delayed receipt against its retired identity.
            raise WeixinError('dot_journal_preparation_recovery_required')
        proof = self._evidence(row, target_dot_message_id, receipt_source, evidence)
        if self.db.execute('SELECT 1 FROM weixin_dot_journal WHERE target_scope=? AND target_dot_message_id=? AND id!=?',
                           (row['target_scope'], target_dot_message_id, row['id'])).fetchone():
            raise WeixinError('dot_journal_target_already_associated')
        if row['state'] == 'committed':
            if row['target_dot_message_id'] != target_dot_message_id:
                raise WeixinError('dot_journal_receipt_conflict')
            if receipt_source == 'history_readback' and not row['readback_evidence']:
                self.db.execute('UPDATE weixin_dot_journal SET readback_evidence=?,updated_at=? WHERE id=?',
                                (proof, self.clock(), row['id']))
            return self._metadata(self._load(row['id']))
        if receipt_source == 'api_response' and row['state'] != 'authorized':
            raise WeixinError('dot_journal_positive_readback_required')
        self.db.execute("""UPDATE weixin_dot_journal SET state='committed',target_dot_message_id=?,
            receipt_source=?,receipt_evidence=?,readback_evidence=?,updated_at=? WHERE id=?""",
            (target_dot_message_id, receipt_source, proof,
             proof if receipt_source == 'history_readback' else None, self.clock(), row['id']))
        return self._metadata(self._load(row['id']))

    def mark_unknown(self, journal_id, attempt_id):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self._load(journal_id, attempt_id)
            if row['state'] == 'committed':
                raise WeixinError('dot_journal_already_committed')
            self.db.execute("UPDATE weixin_dot_journal SET state='unknown',updated_at=? WHERE id=? AND state IN ('prepared','authorized')",
                            (self.clock(), journal_id))
            return self._metadata(self._load(journal_id))

    def for_message(self, message_id):
        with self.lock:
            self.db.execute('SAVEPOINT dot_journal_status')
            try:
                if len(self.db.execute('SELECT id FROM weixin_accounts LIMIT 2').fetchall()) != 1:
                    raise WeixinError('dot_journal_owner_unavailable')
                account, _ = self.probe.dot_mirrors.owner_message(message_id, require_event=False)
                rows = self.db.execute('SELECT * FROM weixin_dot_journal WHERE message_id=? AND account=? AND owner=? ORDER BY part,attachment_id',
                                       (message_id, account['id'], account['owner'])).fetchall()
                records = []
                for row in rows:
                    try:
                        self._load(row['id'])
                        records.append({**self._metadata(row), 'available': True})
                    except WeixinError as error:
                        records.append({'journal_id': row['id'], 'available': False,
                                        'unavailable_reason': error.kind, 'claim_granted': False,
                                        'external_write_allowed': False, 'automatic_resend_allowed': False})
                legacy = self.probe.dot_mirrors.for_message(message_id)
                result = {'journal_version': JOURNAL_VERSION, 'records': records,
                          'legacy_receipts': legacy, 'claim_granted': False,
                          'external_write_allowed': False, 'automatic_resend_allowed': False,
                          'all_parts_verified': False,
                          'fence_verifier_available': self.epochs.verifier_available,
                          'local_cutover': self.cutover.classify(message_id)}
            except BaseException:
                self.db.execute('ROLLBACK TO SAVEPOINT dot_journal_status')
                self.db.execute('RELEASE SAVEPOINT dot_journal_status')
                raise
            self.db.execute('RELEASE SAVEPOINT dot_journal_status')
            return result

    def recover_inflight(self):
        # Only the sole daemon invokes recovery, before ingestion/worker threads.
        # A restart is not proof that remote old callers have stopped.
        self.epochs.suspend_for_restart()
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute("""UPDATE weixin_dot_journal SET state='unknown',updated_at=?
                WHERE state='authorized' OR (state='prepared' AND
                    (authorization_id IS NOT NULL OR authorization_history_version!=1 OR authorization_ever_issued!=0))""",
                            (self.clock(),))
            # Never-authorized preparation is definite non-send under this
            # protocol. Invalidate it without granting a new lease or permit;
            # the caller must re-prepare, which rotates and fences its attempt.
            self.db.execute("""UPDATE weixin_dot_journal
                SET preparation_recovery_required=1,updated_at=?
                WHERE state='prepared' AND authorization_id IS NULL
                    AND authorization_history_version=1 AND authorization_ever_issued=0""", (self.clock(),))

    def call(self, name, arguments):
        methods = {'prepare_weixin_dot_journal': self.prepare,
                   'authorize_weixin_dot_journal': self.authorize,
                   'commit_weixin_dot_journal': self.commit,
                   'mark_weixin_dot_journal_unknown': self.mark_unknown,
                   'get_weixin_dot_journal_status': self.for_message}
        schema = next((t['inputSchema'] for t in journal_tools() if t['name'] == name), None)
        if (not isinstance(arguments, dict) or not schema
                or not set(schema['required']) <= set(arguments) <= set(schema['properties'])):
            raise WeixinError('invalid_dot_journal_arguments')
        try:
            return methods[name](**arguments)
        except TypeError:
            raise WeixinError('invalid_dot_journal_arguments') from None
