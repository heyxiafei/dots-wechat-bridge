"""Offline delivery-plan validation only. No storage, network or sending.

Inputs are coordinator-verified evidence, not self-authorizing model output.
Persist the immutable commitment and original IDs before any real operation.
"""
import hashlib
import json
import re


def preview_commitment(owner_ref, request_ref, artifact_key, attachment_key, sha256):
    values = (owner_ref, request_ref, artifact_key, attachment_key)
    if any(not isinstance(v, str) or not 0 < len(v) <= 256 for v in values):
        raise ValueError('invalid_delivery_identity')
    if not isinstance(sha256, str) or re.fullmatch(r'[a-f0-9]{64}', sha256) is None:
        raise ValueError('invalid_preview_hash')
    key = hashlib.sha256(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    return dict(zip(('owner_ref', 'request_ref', 'artifact_key', 'attachment_key'), values),
                key='preview_' + key, sha256=sha256)


def next_step(commitment, evidence, record=None):
    """Return an advisory action, never a send authorization or receipt.

    A claimed final text slot intentionally has no role in preview eligibility.
    The coordinator serializes actions, preserves IDs and honors tool denials.
    """
    if not isinstance(commitment, dict) or not isinstance(evidence, dict):
        return 'invalid_evidence'
    try:
        expected = preview_commitment(*(commitment.get(k) for k in
            ('owner_ref', 'request_ref', 'artifact_key', 'attachment_key', 'sha256')))
    except ValueError:
        return 'invalid_commitment'
    if commitment != expected:
        return 'invalid_commitment'
    if evidence.get('permission_denied') is True:
        return 'stop_permission_denied'
    if (evidence.get('owner_verified') is not True or evidence.get('explicit_delivery_request') is not True
            or evidence.get('non_sensitive_verified') is not True
            or evidence.get('drive_authorized_private') is not True):
        return 'await_verified_scope'
    if record is not None:
        if not isinstance(record, dict) or record.get('commitment') != commitment:
            return 'stop_commitment_conflict'
        if record.get('denial'):
            return 'stop_permission_denied'
        if record.get('owner_received_evidence_ref') and record.get('send_id'):
            return 'owner_receipt_recorded'
        if record.get('send_id'):
            # Includes accepted, queued, rejected and uncertain: never new send.
            return 'query_original_send'
        if record.get('submission_started') is True:
            return 'reconcile_original_submission'
    archive = evidence.get('archive')
    if (not isinstance(archive, dict) or archive.get('verified') is not True
            or not archive.get('file_id') or archive.get('sha256') != commitment['sha256']):
        return 'archive_and_verify_private_drive'
    if not record or not record.get('upload_id'):
        return 'prepare_native_image_upload'
    if record.get('upload_state') != 'ready':
        return 'query_original_upload'
    return 'prepare_single_media_commitment'


class DeliveryJournal:
    """Caller-owned local SQLite journal; never discovers or opens production DB.

    The coordinator supplies its existing authorized connection and verified
    evidence. This is not wired to an MCP route or a background sender.
    """
    def __init__(self, connection):
        self.db = connection
        self.db.execute('CREATE TABLE IF NOT EXISTS deliverable_previews (key TEXT PRIMARY KEY, commitment TEXT NOT NULL, evidence TEXT NOT NULL, record TEXT NOT NULL)')

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))

    def register(self, commitment, evidence):
        decision = next_step(commitment, evidence)
        if decision in ('invalid_evidence', 'invalid_commitment', 'await_verified_scope', 'stop_permission_denied'):
            raise ValueError(decision)
        encoded = self._json(commitment)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO deliverable_previews VALUES(?,?,?,?)',
                            (commitment['key'], encoded, self._json(evidence), self._json({'commitment': commitment})))
            row = self.db.execute('SELECT commitment FROM deliverable_previews WHERE key=?', (commitment['key'],)).fetchone()
            if row[0] != encoded:
                raise ValueError('delivery_commitment_conflict')
        return self.read(commitment['key'])

    def read(self, key):
        row = self.db.execute('SELECT commitment,evidence,record FROM deliverable_previews WHERE key=?', (key,)).fetchone()
        if row is None:
            raise ValueError('unknown_delivery')
        c, e, r = map(json.loads, row)
        return {'commitment': c, 'evidence': e, 'record': r, 'next_step': next_step(c,e,r)}

    def update_before_submission(self, key, evidence, *, upload_id=None, upload_state=None):
        # Compare-and-swap prevents two coordinators changing one record silently.
        old = self.read(key)
        record = dict(old['record'])
        if record.get('submission_started') or record.get('send_id'):
            raise ValueError('submission_already_started')
        if record.get('upload_id') and upload_id not in (None, record['upload_id']):
            raise ValueError('upload_identity_conflict')
        if upload_id is not None:
            record.update(upload_id=upload_id, upload_state=upload_state)
        self._cas(key,old,evidence,record)
        return self.read(key)

    def claim_submission(self, key, *, caller_send_key):
        if not isinstance(caller_send_key,str) or not caller_send_key:
            raise ValueError('send_key_required')
        old = self.read(key)
        if old['next_step'] != 'prepare_single_media_commitment':
            raise ValueError('delivery_not_ready_or_already_claimed')
        record = dict(old['record'], submission_started=True, caller_send_key=caller_send_key)
        self._cas(key,old,old['evidence'],record)
        return self.read(key)

    def record_send(self, key, *, send_id, api_state, owner_received_evidence_ref=None):
        old = self.read(key)
        if (old['record'].get('submission_started') is not True or not isinstance(send_id,str) or not send_id
                or old['record'].get('send_id') not in (None,send_id)):
            raise ValueError('send_identity_conflict')
        record = dict(old['record'], send_id=send_id, api_state=api_state)
        if owner_received_evidence_ref:
            record['owner_received_evidence_ref'] = owner_received_evidence_ref
        self._cas(key,old,old['evidence'],record)
        return self.read(key)

    def record_denial(self, key, *, evidence_ref, known_not_executed):
        """Persist a real approval denial, including after a local claim.

        This does not authorize a retry or convert an unknown send into failure.
        References must come from the trusted coordinator's actual tool result.
        There is deliberately no automatic clearing or alternate-send path.
        """
        if (not isinstance(evidence_ref, str) or not 0 < len(evidence_ref) <= 256
                or type(known_not_executed) is not bool):
            raise ValueError('invalid_denial_evidence')
        old = self.read(key)
        if known_not_executed and old['record'].get('send_id'):
            raise ValueError('existing_send_requires_reconciliation')
        denial = dict(evidence_ref=evidence_ref, known_not_executed=known_not_executed)
        if old['record'].get('denial'):
            if old['record']['denial'] != denial:
                raise ValueError('denial_evidence_conflict')
            return old
        record = dict(old['record'], denial=denial)
        self._cas(key,old,old['evidence'],record)
        return self.read(key)

    def _cas(self,key,old,evidence,record):
        with self.db:
            changed = self.db.execute('UPDATE deliverable_previews SET evidence=?,record=? WHERE key=? AND evidence=? AND record=?',
                (self._json(evidence),self._json(record),key,self._json(old['evidence']),self._json(old['record']))).rowcount
            if changed != 1:
                raise ValueError('delivery_revision_conflict')
