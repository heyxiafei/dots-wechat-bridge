"""ledger-v1: owner/event-scoped dot receipt metadata; never posts or reads bodies."""
import hashlib
import re
import uuid

LEDGER_VERSION = 'ledger-v1'
RECONCILIATION_VERSION = 'ledger-reconciliation-v1'

try:
    from .weixin import WeixinError, MESSAGE_EVENT, encoded
except ImportError:
    from weixin import WeixinError, MESSAGE_EVENT, encoded


def ledger_tools():
    string = {'type': 'string', 'minLength': 1, 'maxLength': 256}
    identity = {'message_id': string, 'event_id': string,
                'phase': {'type': 'string', 'enum': ['original', 'ack', 'final']},
                'attachment_id': {'type': 'string', 'pattern': '^att_[a-f0-9]{32}$'}}
    definitions = [
        ('prepare_weixin_dot_mirror',
         'Prepare metadata for one already-authorized owner event mirror. Sends nothing. An empty ledger or first claim is NOT proof that dot lacks a historical display: reconcile existing/backfill events through actual supported history before any write; never resend old attachments. Only a newly created preparation grants the first local claim; every duplicate prepared/unknown/committed record forbids another dot write. A crash or lost response requires history reconciliation, never automatic resend. original may identify an existing attachment; ack/final require the committed Weixin reply phase. This local claim is not communication authorization.',
         identity, ['message_id', 'event_id', 'phase']),
        ('commit_weixin_dot_mirror',
         'Record a real target dot message ID obtained from the supported dot API response or explicitly correlated lawful history readback. Sends nothing and does not verify remote existence, role, room, content or visibility. unknown may resolve only with that actual existing receipt; never resend to obtain a new ID. Same receipt is idempotent; a different receipt conflicts.',
         {'mirror_id': string, 'attempt_id': string, 'event_id': string,
          'target_dot_message_id': {'type': 'string', 'pattern': '^Sentinel_[a-f0-9]{32}$'},
          'receipt_source': {'type': 'string', 'enum': ['api_response', 'history_readback']}},
         ['mirror_id', 'attempt_id', 'event_id', 'target_dot_message_id', 'receipt_source']),
        ('mark_weixin_dot_mirror_unknown',
         'Mark an uncertain prepared mirror as unknown. Sends nothing, never enables retry, and cannot downgrade a committed receipt.',
         {'mirror_id': string, 'attempt_id': string}, ['mirror_id', 'attempt_id']),
        ('get_weixin_dot_mirror_status',
         'Read owner-scoped mirror receipt metadata and original attempt identities for reconciliation only. Never grants a first claim, permits an external write or authorizes resending. Recovered identities may only record an already-existing real target receipt; unknown still requires correlated history_readback and partial history cannot justify another send. tracked means some records exist, not all parts/phases mirrored or dot-visible. Caller-reported IDs are never remote-verified by this bridge.',
         {'message_id': string}, ['message_id']),
    ]
    return [{'name': name, 'description': description,
             'inputSchema': {'type': 'object', 'properties': properties, 'required': required,
                             'additionalProperties': False},
             'annotations': {'readOnlyHint': name == 'get_weixin_dot_mirror_status',
                             'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False}}
            for name, description, properties, required in definitions]


class DotMirrorLedger:
    def __init__(self, probe):
        self.probe, self.db, self.lock, self.clock = probe, probe.db, probe.lock, probe.clock
        self.db.executescript('''CREATE TABLE IF NOT EXISTS weixin_dot_mirrors (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL, event_id TEXT NOT NULL,
            attachment_id TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL
                CHECK(phase IN ('original','ack','final')),
            state TEXT NOT NULL CHECK(state IN ('prepared','committed','unknown')),
            target_dot_message_id TEXT, attempt_id TEXT NOT NULL, receipt_source TEXT
                CHECK(receipt_source IN ('api_response','history_readback')),
            direction TEXT NOT NULL DEFAULT 'weixin_to_dot' CHECK(direction='weixin_to_dot'),
            suppress_reflection INTEGER NOT NULL DEFAULT 1 CHECK(suppress_reflection=1),
            created_at REAL NOT NULL, updated_at REAL NOT NULL,
            UNIQUE(message_id,attachment_id,phase),
            CHECK((state='committed' AND target_dot_message_id IS NOT NULL AND receipt_source IS NOT NULL)
               OR (state IN ('prepared','unknown') AND target_dot_message_id IS NULL AND receipt_source IS NULL))
        );''')
        self.db.commit()

    def owner_message(self, message_id, event_id=None, require_event=True):
        # Do not use account(): this metadata path never selects bot/context tokens.
        account = self.db.execute('SELECT id,owner FROM weixin_accounts LIMIT 1').fetchone()
        if not account:
            raise WeixinError('not_bound')
        row = self.db.execute('SELECT id,event_id FROM weixin_inbox WHERE id=? AND account=? AND peer=?',
                              (message_id, account['id'], account['owner'])).fetchone()
        if not row:
            raise WeixinError('unknown_owner_message')
        if require_event:
            if not row['event_id'] or (event_id is not None and event_id != row['event_id']):
                raise WeixinError('dot_mirror_event_mismatch')
            linked = self.db.execute('SELECT 1 FROM pings WHERE id=? AND name=? AND message_id=?',
                                     (row['event_id'], MESSAGE_EVENT, message_id)).fetchone()
            if not linked:
                raise WeixinError('dot_mirror_event_mismatch')
        return account, row

    def validate_part(self, account, message_id, phase, attachment_id):
        if phase not in ('original', 'ack', 'final'):
            raise WeixinError('invalid_dot_mirror_phase')
        aid = '' if attachment_id is None else attachment_id
        if aid and (not isinstance(aid, str) or not re.fullmatch(r'att_[a-f0-9]{32}', aid)):
            raise WeixinError('invalid_attachment_id')
        if attachment_id is not None and not aid:
            raise WeixinError('invalid_attachment_id')
        if aid:
            if phase != 'original':
                raise WeixinError('dot_mirror_attachment_requires_original')
            owned = self.db.execute('SELECT 1 FROM weixin_attachments WHERE id=? AND message_id=? AND account=? AND peer=?',
                                    (aid, message_id, account['id'], account['owner'])).fetchone()
            if not owned:
                raise WeixinError('unknown_owner_attachment')
        if phase in ('ack', 'final'):
            sid = self.probe.weixin.ack_send_id(message_id) if phase == 'ack' else message_id
            kind = 'reply_ack' if phase == 'ack' else 'reply'
            reply = self.db.execute("SELECT status,ack_origin FROM weixin_outbox WHERE message_id=? AND account=? AND peer=? AND kind=? AND (source_message_id=? OR (kind='reply' AND source_message_id IS NULL))",
                                    (sid, account['id'], account['owner'], kind, message_id)).fetchone()
            if not reply:
                raise WeixinError('dot_mirror_reply_not_committed')
            if phase == 'ack' and reply['ack_origin'] == 'system_receipt':
                if reply['status'] == 'superseded':
                    raise WeixinError('dot_mirror_ack_superseded')
                if reply['status'] not in ('sending','accepted','delivery_unknown'):
                    raise WeixinError('dot_mirror_ack_not_sent_yet')
        return aid

    @staticmethod
    def metadata(row):
        return {'mirror_id': row['id'], 'message_id': row['message_id'], 'event_id': row['event_id'],
                'attachment_id': row['attachment_id'] or None, 'phase': row['phase'],
                'state': row['state'], 'target_dot_message_id': row['target_dot_message_id'],
                'direction': 'weixin_to_dot', 'suppress_reflection': True,
                'automatic_resend_allowed': False, 'receipt_basis': 'caller_report',
                'receipt_source': row['receipt_source'], 'attempt_id_recorded': True,
                'target_id_verified_by_bridge': False, 'dot_visible_verified_by_bridge': False}

    def prepare(self, message_id, event_id, phase, attachment_id=None):
        with self.lock, self.db:
            self.db.execute('BEGIN IMMEDIATE')
            account, source = self.owner_message(message_id, event_id)
            aid = self.validate_part(account, message_id, phase, attachment_id)
            identity = 'dm_' + hashlib.sha256(encoded([message_id, aid, phase]).encode()).hexdigest()[:32]
            now = self.clock()
            # A staged journal or an existing new intent must not be bypassed by
            # a legacy tool. Existing v1 records remain readable/reconcilable.
            journal = getattr(self.probe, 'dot_journal', None)
            if journal is not None and not self.db.execute(
                    'SELECT 1 FROM weixin_dot_mirrors WHERE id=?', (identity,)).fetchone():
                journal.guard_legacy_prepare(message_id, aid, phase)
            # INSERT OR IGNORE is the cross-connection claim: no select-then-insert race.
            created = self.db.execute('''INSERT OR IGNORE INTO weixin_dot_mirrors
                (id,message_id,event_id,attachment_id,phase,state,attempt_id,created_at,updated_at)
                VALUES(?,?,?,?,?,'prepared',?,?,?)''',
                (identity, message_id, source['event_id'], aid, phase, uuid.uuid4().hex, now, now)).rowcount == 1
            row = self.db.execute('SELECT * FROM weixin_dot_mirrors WHERE id=?', (identity,)).fetchone()
            if not row or (row['message_id'], row['event_id'], row['attachment_id'], row['phase']) != (message_id, event_id, aid, phase):
                raise WeixinError('dot_mirror_identity_conflict')
            return {**self.metadata(row), 'first_prepare': created, 'claim_granted': created,
                    'attempt_id': row['attempt_id'] if created else None}

    def authorized(self, mirror_id):
        if not isinstance(mirror_id, str) or not re.fullmatch(r'dm_[a-f0-9]{32}', mirror_id):
            raise WeixinError('invalid_dot_mirror_id')
        row = self.db.execute('SELECT * FROM weixin_dot_mirrors WHERE id=?', (mirror_id,)).fetchone()
        if not row:
            raise WeixinError('unknown_dot_mirror')
        account, _ = self.owner_message(row['message_id'], row['event_id'])
        self.validate_part(account, row['message_id'], row['phase'], row['attachment_id'] or None)
        return row

    def commit(self, mirror_id, attempt_id, event_id, target_dot_message_id, receipt_source):
        # Syntactic check only. IDs must be copied from a real supported API result.
        if not isinstance(target_dot_message_id, str) or not re.fullmatch(r'Sentinel_[a-f0-9]{32}', target_dot_message_id):
            raise WeixinError('invalid_target_dot_message_id')
        with self.lock, self.db:
            row = self.authorized(mirror_id)
            if attempt_id != row['attempt_id'] or event_id != row['event_id']:
                raise WeixinError('dot_mirror_attempt_mismatch')
            if receipt_source not in ('api_response', 'history_readback'):
                raise WeixinError('invalid_dot_mirror_receipt_source')
            if row['state'] == 'unknown' and receipt_source != 'history_readback':
                raise WeixinError('dot_mirror_history_required')
            self.db.execute("UPDATE weixin_dot_mirrors SET state='committed',target_dot_message_id=?,receipt_source=?,updated_at=? WHERE id=? AND attempt_id=? AND (state='prepared' OR (state='unknown' AND ?='history_readback'))",
                            (target_dot_message_id, receipt_source, self.clock(), mirror_id, attempt_id, receipt_source))
            row = self.db.execute('SELECT * FROM weixin_dot_mirrors WHERE id=?', (mirror_id,)).fetchone()
            if row['state'] == 'unknown':
                raise WeixinError('dot_mirror_history_required')
            if row['target_dot_message_id'] != target_dot_message_id:
                raise WeixinError('dot_mirror_receipt_conflict')
            return self.metadata(row)

    def mark_unknown(self, mirror_id, attempt_id):
        with self.lock, self.db:
            row = self.authorized(mirror_id)
            if attempt_id != row['attempt_id']:
                raise WeixinError('dot_mirror_attempt_mismatch')
            self.db.execute("UPDATE weixin_dot_mirrors SET state='unknown',updated_at=? WHERE id=? AND attempt_id=? AND state='prepared'", (self.clock(), mirror_id, attempt_id))
            row = self.db.execute('SELECT * FROM weixin_dot_mirrors WHERE id=?', (mirror_id,)).fetchone()
            if row['state'] == 'committed':
                raise WeixinError('dot_mirror_already_committed')
            return self.metadata(row)

    def for_message(self, message_id):
        with self.lock:
            # A SAVEPOINT gives all reads one snapshot, including across other
            # connections. RELEASE never commits an enclosing caller transaction.
            # This path changes no rows, claims, subscriptions or recovery state.
            self.db.execute('SAVEPOINT weixin_dot_mirror_status_read')
            try:
                if len(self.db.execute('SELECT id FROM weixin_accounts LIMIT 2').fetchall()) > 1:
                    raise WeixinError('dot_mirror_owner_unavailable')
                _, source = self.owner_message(message_id, require_event=False)
                associated = bool(source['event_id'] and self.db.execute('SELECT 1 FROM pings WHERE id=? AND name=? AND message_id=?',
                                   (source['event_id'], MESSAGE_EVENT, message_id)).fetchone())
                rows = self.db.execute('SELECT * FROM weixin_dot_mirrors WHERE message_id=? ORDER BY created_at,id', (message_id,)).fetchall()
                reconciliation = []
                for row in rows:
                    # Recheck the original event, phase and attachment in this
                    # snapshot. A stale/broken association must not disclose its
                    # attempt identity, even if the old metadata row still exists.
                    reason = None
                    try:
                        self.authorized(row['id'])
                    except WeixinError as error:
                        reason = error.kind
                    reconciliation.append({**self.metadata(row),
                        'attempt_id': row['attempt_id'] if reason is None else None,
                        'available': reason is None, 'unavailable_reason': reason,
                        'reconciliation_only': True, 'claim_granted': False,
                        'external_write_allowed': False,
                        'existing_target_receipt_required': True})
                result = {'dot_mirror_status': 'tracked' if rows else 'not_tracked',
                    'bridge_version': self.probe.bridge_version, 'dot_mirror_ledger_version': LEDGER_VERSION,
                    'source_event_id': source['event_id'] if associated else None, 'source_event_associated': associated,
                    'all_parts_verified': False, 'records': [self.metadata(row) for row in rows],
                    'reconciliation': {'version': RECONCILIATION_VERSION,
                        'reconciliation_only': True, 'claim_granted': False,
                        'external_write_allowed': False, 'automatic_resend_allowed': False,
                        'records': reconciliation}}
            except BaseException:
                self.db.execute('ROLLBACK TO SAVEPOINT weixin_dot_mirror_status_read')
                self.db.execute('RELEASE SAVEPOINT weixin_dot_mirror_status_read')
                raise
            self.db.execute('RELEASE SAVEPOINT weixin_dot_mirror_status_read')
            return result

    def call(self, name, arguments):
        methods = {'prepare_weixin_dot_mirror': self.prepare, 'commit_weixin_dot_mirror': self.commit,
                   'mark_weixin_dot_mirror_unknown': self.mark_unknown, 'get_weixin_dot_mirror_status': self.for_message}
        spec = next((tool['inputSchema'] for tool in ledger_tools() if tool['name'] == name), None)
        if (not spec or not set(spec['required']) <= set(arguments) <= set(spec['properties'])
                or not all(isinstance(value, str) for value in arguments.values())):
            raise WeixinError('invalid_dot_mirror_arguments')
        return methods[name](**arguments)

    def recover_inflight(self):
        # Only the sole production daemon calls this; status readers never recover.
        # Prepared may already have caused an external write. Never re-claim it.
        with self.lock, self.db:
            self.db.execute("UPDATE weixin_dot_mirrors SET state='unknown',updated_at=? WHERE state='prepared'", (self.clock(),))
