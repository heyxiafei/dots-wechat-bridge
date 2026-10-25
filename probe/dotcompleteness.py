"""Read-only expected mirror parts, derived from durable inbound/response facts.

The inventory exposes missing work without claiming remote visibility or
granting a retry. Journal history/readback is still caller-reported evidence.
"""
from collections import Counter
import hashlib
import json


COMPLETENESS_VERSION = 'dot-completeness-v1'


def completeness_tools():
    return [{
        'name': 'get_weixin_dot_completeness',
        'description': 'Read the expected original, attachments, inbound failure placeholders and actually committed reply stages for one exact owner message. Derives missing parts from durable source facts even when no journal was prepared. Readback is caller-reported metadata, never independent content or visibility verification. Historical sources without an inbound inventory remain explicitly unverified. Does not prepare, authorize, replay, send or change admission.',
        'inputSchema': {'type': 'object', 'properties': {
            'message_id': {'type': 'string', 'minLength': 1, 'maxLength': 256}},
            'required': ['message_id'], 'additionalProperties': False},
        'annotations': {'readOnlyHint': True, 'destructiveHint': False,
                        'idempotentHint': True, 'openWorldHint': False}}]


class DotCompleteness:
    def __init__(self, probe):
        self.probe, self.db, self.lock = probe, probe.db, probe.lock

    @staticmethod
    def _receipt(part, journals, legacy):
        key = (part['part'], part.get('attachment_id') or '')
        row = journals.get(key)
        if row is not None:
            if not row.get('available', True):
                return {'mirror_state': 'unavailable', 'receipt_basis': 'caller_report',
                        'reason': row.get('unavailable_reason'), 'journal_id': row['journal_id']}
            state = row['effective_state']
            mirror = ('readback_reported' if row.get('positive_readback_recorded') else
                      'receipt_recorded') if state == 'committed' else (
                      'unknown' if state in ('unknown', 'authorization_history_unknown') else 'pending')
            return {'mirror_state': mirror, 'journal_state': state,
                    'journal_id': row['journal_id'], 'target_dot_message_id': row.get('target_dot_message_id'),
                    'receipt_basis': 'caller_report',
                    'preparation_recovery_required': row.get('preparation_recovery_required', False)}
        row = legacy.get(key)
        if row is not None:
            return {'mirror_state': ('unavailable' if not row.get('available', True) else
                    'receipt_recorded' if row['state'] == 'committed' else 'unknown'),
                    'legacy_state': row['state'], 'legacy_mirror_id': row['mirror_id'],
                    'target_dot_message_id': row.get('target_dot_message_id'),
                    'receipt_basis': 'legacy_caller_report',
                    'reason': 'legacy_record_requires_reconciliation'}
        return {'mirror_state': 'missing', 'receipt_basis': None}

    def for_message(self, message_id):
        with self.lock:
            # A coherent snapshot across inbox, attachments, reply slots and
            # journal, without creating claims or populating missing journal rows.
            self.db.execute('SAVEPOINT dot_completeness_status')
            try:
                result = self._snapshot(message_id)
            except BaseException:
                self.db.execute('ROLLBACK TO SAVEPOINT dot_completeness_status')
                self.db.execute('RELEASE SAVEPOINT dot_completeness_status')
                raise
            self.db.execute('RELEASE SAVEPOINT dot_completeness_status')
            return result

    def _snapshot(self, message_id):
        account, _ = self.probe.dot_mirrors.owner_message(message_id, require_event=False)
        capture = self.probe.weixin.inbound_completeness(message_id)
        journal = self.probe.dot_journal.for_message(message_id)
        journals = {(r.get('part'), r.get('attachment_id') or ''): r
                    for r in journal['records'] if r.get('available', True)}
        # Unavailable journal rows still must not look absent/retryable. Resolve
        # identity locally without selecting message bodies or credentials.
        for r in journal['records']:
            if not r.get('available', True):
                identity = self.db.execute('SELECT part,attachment_id FROM weixin_dot_journal WHERE id=?',
                                           (r['journal_id'],)).fetchone()
                if identity:
                    journals[(identity['part'], identity['attachment_id'])] = r
        legacy = {(r['phase'], r.get('attachment_id') or ''): r
                  for r in journal['legacy_receipts'].get('reconciliation', {}).get('records', [])}
        expected = [{'part_key': 'original', 'part': 'original', 'attachment_id': None,
                     'kind': 'original', 'source_state': 'captured', 'mirror_required': True}]
        attachments = self.db.execute('''SELECT id,kind,status,error_kind,expires_at,size,sha256
            FROM weixin_attachments WHERE message_id=? AND account=? AND peer=? ORDER BY id''',
            (message_id, account['id'], account['owner'])).fetchall()
        for a in attachments:
            source_state = a['status']
            # A read never changes stored media status or deletes its cache.
            if a['expires_at'] <= self.probe.clock():
                source_state = 'expired'
            expected.append({'part_key': 'attachment:' + a['id'], 'part': 'original',
                'attachment_id': a['id'], 'kind': a['kind'], 'source_state': source_state,
                'source_error_kind': a['error_kind'], 'size': a['size'], 'sha256': a['sha256'],
                'mirror_required': True})
        stored_attachments = {a['id'] for a in attachments}
        for item in capture.get('parts', []):
            aid = item.get('attachment_id')
            if aid and aid not in stored_attachments:
                expected.append({'part_key': 'attachment:' + aid, 'part': 'original',
                    'attachment_id': aid, 'kind': item['kind'], 'source_state': 'missing',
                    'source_error_kind': 'source_attachment_record_missing', 'mirror_required': True})
                stored_attachments.add(aid)
        for phase, sid, kind in [('ack', self.probe.weixin.ack_send_id(message_id), 'reply_ack'),
                                  ('final', message_id, 'reply')]:
            slot = self.db.execute('''SELECT status,ack_origin FROM weixin_outbox
                WHERE message_id=? AND account=? AND peer=? AND kind=?
                  AND (source_message_id=? OR (kind='reply' AND source_message_id IS NULL))''',
                (sid, account['id'], account['owner'], kind, message_id)).fetchone()
            if not slot:
                continue
            # A superseded/never-sent automatic receipt is not an actual answer.
            if phase == 'ack' and slot['ack_origin'] == 'system_receipt' and slot['status'] not in (
                    'sending', 'accepted', 'delivery_unknown'):
                continue
            expected.append({'part_key': phase, 'part': phase, 'attachment_id': None,
                'kind': 'reply', 'source_state': 'committed', 'send_id': sid,
                'reply_transport_status': slot['status'], 'mirror_required': True})
        for part in expected:
            part.update(self._receipt(part, journals, legacy))
            source = part['source_state']
            if part['mirror_state'] in ('unknown', 'unavailable'):
                part['state'] = part['mirror_state']
            elif source == 'missing':
                part['state'] = 'missing'
            elif source in ('expired', 'error', 'transcript_unavailable', 'transcript_only'):
                part['state'] = 'unsupported' if source.startswith('transcript_') else source
            elif source in ('queued', 'downloading'):
                part['state'] = 'pending'
            else:
                part['state'] = part['mirror_state']
            part['automatic_resend_allowed'] = False
        # The source inventory records even parts that have no downloadable
        # attachment row. These are visible blockers, never fabricated send IDs.
        for item in capture.get('parts', []):
            issues = item.get('issue_codes', [])
            if not issues and item.get('state') == 'captured':
                continue
            expected.append({'part_key': 'inbound:' + item['part_id'], 'part': None,
                'attachment_id': item.get('attachment_id'), 'kind': item['kind'],
                'source_state': item['state'], 'state': 'unsupported',
                'issue_codes': issues, 'item_index': item.get('item_index'),
                'mirror_required': False, 'mirror_state': 'not_applicable',
                'automatic_resend_allowed': False})
        represented = {code for item in capture.get('parts', []) for code in item.get('issue_codes', [])}
        summary_issues = sorted(set(capture.get('issue_codes', [])) - represented)
        if summary_issues:
            expected.append({'part_key': 'inbound:summary', 'part': None, 'attachment_id': None,
                'kind': 'source_inventory', 'source_state': 'incomplete', 'state': 'unsupported',
                'issue_codes': summary_issues, 'mirror_required': False,
                'mirror_state': 'not_applicable', 'automatic_resend_allowed': False})
        if not capture.get('recorded'):
            expected.append({'part_key': 'inbound:legacy-unrecorded', 'part': None,
                'attachment_id': None, 'kind': 'source_inventory', 'source_state': 'legacy_unrecorded',
                'state': 'unknown', 'issue_codes': ['historical_capture_inventory_unavailable'],
                'mirror_required': False, 'mirror_state': 'not_applicable',
                'automatic_resend_allowed': False})
        mirrored = [p for p in expected if p['mirror_required']]
        source_complete = bool(capture.get('recorded') and capture.get('complete'))
        result = {'version': COMPLETENESS_VERSION, 'message_id': message_id,
            'expected_parts': expected, 'expected_part_count': len(expected),
            'state_counts': dict(sorted(Counter(p['state'] for p in expected).items())),
            'source_inventory_complete': source_complete,
            'source_content_ready_recorded': source_complete and all(
                p['source_state'] in ('captured', 'cached', 'committed') for p in mirrored),
            'inbound_completeness': capture,
            'all_parts_accounted_for': bool(capture.get('recorded')) and all(
                p['state'] not in ('missing', 'pending', 'unknown', 'unavailable') for p in expected),
            'all_expected_parts_receipted': source_complete and all(
                p['mirror_state'] in ('receipt_recorded', 'readback_reported') for p in mirrored),
            'all_expected_parts_readback_reported': source_complete and all(
                p['mirror_state'] == 'readback_reported' for p in mirrored),
            'all_parts_verified': False, 'target_content_verified_by_bridge': False,
            'dot_visible_verified_by_bridge': False, 'receipt_basis': 'caller_report',
            'claim_granted': False, 'external_write_allowed': False, 'automatic_resend_allowed': False,
            'local_cutover': journal['local_cutover']}
        # Describes this metadata snapshot, not the hash of remote message bytes.
        result['snapshot_sha256'] = hashlib.sha256(json.dumps(result, sort_keys=True,
            ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
        return result
