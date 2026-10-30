"""OFFLINE CANDIDATE. Durable tool requests only; no sender or auth capability.

An approved platform handoff is still required. This file does not start a
service, call a model, open a socket, read secrets, or modify bridge storage.
"""
import hashlib
import html
import math
import os
from pathlib import Path
import re
import stat
import time
from email.utils import getaddresses
from .operator_config import require_operator_config
from .store_helpers import Invalid, atomic_json, lock, notification_text, strict_json
from .text_b import validate_display, format_b, legacy_display, RICH_STYLE, UNAVAILABLE_STYLE

CHANNELS = ('wechat', 'email')
FRESHNESS = 1800
LIMIT = 10000
STATES = ('pending', 'attempting', 'waiting', 'accepted', 'uncertain', 'suppressed', 'rejected')


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def key_for(eid, stage):
    if not isinstance(eid, str) or not re.fullmatch(r'reset-[0-9]{5,25}-[1-9][0-9]{0,2}-[1-9][0-9]{0,2}', eid):
        raise Invalid('event_id')
    if stage not in ('announced', 'confirmed'):
        raise Invalid('stage')
    return eid + '/' + stage


def validate_intent(value):
    required = {'event_id', 'stage', 'source_at', 'expires_at', 'text', 'link', 'eligible'}
    if not isinstance(value, dict) or set(value) not in (required, required | {'display'}):
        raise Invalid('intent_shape')
    key_for(value['event_id'], value['stage'])
    if type(value['eligible']) is not bool or not all(finite(value[n]) for n in ('source_at', 'expires_at')):
        raise Invalid('intent_time_or_flag')
    if not isinstance(value['link'], str) or not re.fullmatch(r'https://x\.com/thsottiaux/status/[0-9]{5,25}', value['link']):
        raise Invalid('public_source')
    if not isinstance(value['text'], str) or len(value['text'].encode()) > 2048 or value['text'] != notification_text(value['stage'], value['source_at'], value['link']):
        raise Invalid('fixed_public_template')
    unavailable = value.get('display') == {'version': 1, 'style': UNAVAILABLE_STYLE}
    if value.get('display') is not None and not unavailable:
        try:
            validate_display(value['display'], value)
            format_b(value['display'])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise Invalid('invalid_wechat_display') from None
    return value


def wechat_text(intent):
    if 'display' in intent and (intent['display'] is None or intent['display'] == {'version': 1, 'style': UNAVAILABLE_STYLE}):
        raise Invalid('wechat_display_unavailable')
    return format_b(intent['display']) if 'display' in intent else intent['text']


def identity(key, channel):
    return 'aihot-dual-v1-' + channel + '-' + hashlib.sha256(key.encode()).hexdigest()[:32]


def email_payload(intent, operator_config=None):
    operator = require_operator_config(operator_config)
    key = key_for(intent['event_id'], intent['stage'])
    display = intent.get('display')
    if display == {'version': 1, 'style': UNAVAILABLE_STYLE}:
        raise Invalid('presentation_unavailable')
    if isinstance(display, dict) and display.get('style') == RICH_STYLE:
        label = '重置预告' if intent['stage'] == 'announced' else '重置确认'
        return {'to': operator.recipient, 'subject': '🔔 Codex ' + label, 'payload': {'mime_type': 'text/html',
            'charset': 'UTF-8', 'body': {'content': '<p>' + html.escape(wechat_text(intent)).replace('\n', '<br>') + '</p>'}}}
    marker = 'AIHOT-' + hashlib.sha256(key.encode()).hexdigest()[:16]
    label = '新重置预告' if intent['stage'] == 'announced' else '新重置确认'
    return {'to': operator.recipient, 'subject': marker + ' · Codex ' + label,
            'payload': {'mime_type': 'text/html', 'charset': 'UTF-8',
                        'body': {'content': '<p>' + html.escape(intent['text']).replace('\n', '<br>') + '</p>'}}}


class Relay:
    def __init__(self, path, now_callable=time.time, *, operator_config=None):
        self.operator = require_operator_config(operator_config) if operator_config is not None else None
        self.path = Path(path)
        self.clock = now_callable

    def _load(self, now):
        path = self.path / 'dual-notifications.json'
        if self.path.exists():
            parent = self.path.lstat()
            if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
                raise Invalid('private_data_directory')
        if path.is_symlink():
            raise Invalid('ledger_symlink')
        if not path.exists():
            return dict(version=1, activation_at=now, baseline_complete=False,
                        checked_at=None, healthy=False, snapshot={}, entries={}, terminal={}, versions={})
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise Invalid('private_ledger_file')
        if info.st_size > 32 * 1024 * 1024:
            raise Invalid('ledger_capacity')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'r') as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise Invalid('ledger_replaced_during_read')
            value = strict_json(handle.read(32 * 1024 * 1024 + 1))
        if not isinstance(value, dict) or set(value) != {'version', 'activation_at', 'baseline_complete', 'checked_at', 'healthy', 'snapshot', 'entries', 'terminal', 'versions'} or value['version'] != 1:
            raise Invalid('ledger_shape')
        if not finite(value['activation_at']) or type(value['baseline_complete']) is not bool or type(value['healthy']) is not bool:
            raise Invalid('ledger_flags')
        if value['checked_at'] is not None and not finite(value['checked_at']):
            raise Invalid('ledger_time')
        if not isinstance(value['snapshot'], dict) or not isinstance(value['entries'], dict) or len(value['entries']) > LIMIT:
            raise Invalid('ledger_capacity')
        if not isinstance(value['terminal'], dict) or not isinstance(value['versions'], dict) or len(value['terminal']) > LIMIT or len(value['versions']) > LIMIT:
            raise Invalid('ledger_watermarks')
        for eid, version in value['versions'].items():
            key_for(eid, 'announced')
            if not finite(version):
                raise Invalid('ledger_watermarks')
        for eid, reason in value['terminal'].items():
            key_for(eid, 'announced')
            if reason not in ('confirmed', 'withdrawn', 'unknown'):
                raise Invalid('ledger_terminal')
        ids = {channel: set() for channel in CHANNELS}
        for key, item in value['entries'].items():
            if not isinstance(item, dict) or set(item) != {'intent', 'channels'}:
                raise Invalid('ledger_entry')
            intent = validate_intent(item['intent'])
            if key != key_for(intent['event_id'], intent['stage']) or set(item['channels']) != set(CHANNELS):
                raise Invalid('ledger_identity')
            for channel, entry in item['channels'].items():
                if not isinstance(entry, dict) or set(entry) != {'state', 'request_id', 'provider_id', 'provider_status'} or entry['state'] not in STATES or entry['request_id'] != identity(key, channel):
                    raise Invalid('ledger_channel')
                pid = entry['provider_id']
                status = entry['provider_status']
                if pid is not None:
                    pattern = r'notice_[a-f0-9]{32}' if channel == 'wechat' else r'[a-fA-F0-9]{10,64}'
                    if not isinstance(pid, str) or not re.fullmatch(pattern, pid) or pid in ids[channel]:
                        raise Invalid('ledger_provider_identity')
                    ids[channel].add(pid)
                allowed = ('accepted', 'queued', 'preparing', 'sending', 'context_required', 'auth_required', 'rejected', 'delivery_unknown', 'uncertain', 'unknown') if channel == 'wechat' else ('sent', 'unverified', 'unknown')
                if status is not None and status not in allowed:
                    raise Invalid('ledger_provider_status')
                if entry['state'] in ('pending', 'attempting', 'suppressed') and (pid is not None or status is not None):
                    raise Invalid('ledger_attempt_identity')
                if entry['state'] in ('accepted', 'waiting', 'rejected') and pid is None:
                    raise Invalid('ledger_receipt')
                if entry['state'] == 'accepted' and status != ('accepted' if channel == 'wechat' else 'sent'):
                    raise Invalid('ledger_receipt')
                if entry['state'] == 'waiting' and (channel != 'wechat' or status not in ('queued', 'preparing', 'sending', 'context_required', 'auth_required')):
                    raise Invalid('ledger_receipt')
                if entry['state'] == 'rejected' and (channel != 'wechat' or status != 'rejected'):
                    raise Invalid('ledger_receipt')
        for key, intent in value['snapshot'].items():
            validate_intent(intent)
            if key != key_for(intent['event_id'], intent['stage']):
                raise Invalid('snapshot_identity')
        return value

    def _save(self, value):
        atomic_json(self.path / 'dual-notifications.json', value)

    def status(self):
        return self._load(self.clock())

    def baseline(self, events, now, checked_at=None):
        # Existing claims/receipts must never be reset by another baseline call.
        if self._load(now)['baseline_complete']:
            return {'result': 'already_baseline'}
        return self.sync(events, now, checked_at=checked_at)

    def suspend(self):
        with lock(self.path) as acquired:
            if not acquired:
                return {'result': 'lock_busy'}
            state = self._load(self.clock())
            state['healthy'] = False
            self._save(state)
            return {'result': 'source_blocked'}

    def rebaseline(self, now):
        if not finite(now):
            raise Invalid('baseline_time')
        with lock(self.path) as acquired:
            if not acquired:
                return {'result': 'lock_busy'}
            state = self._load(now)
            for item in state['entries'].values():
                for channel in item['channels'].values():
                    if channel['state'] == 'pending':
                        channel['state'] = 'suppressed'
            state.update(activation_at=now, baseline_complete=False, checked_at=None,
                         healthy=False, snapshot={})
            self._save(state)
            return {'result': 'awaiting_new_baseline'}

    def sync(self, events, now, checked_at=None, healthy=True, terminal=None, versions=None):
        checked = now if checked_at is None else checked_at
        with lock(self.path) as acquired:
            if not acquired:
                return {'result': 'lock_busy'}
            state = self._load(now)
            try:
                if not finite(now) or not finite(checked) or checked > now + 120 or now - checked > FRESHNESS or type(healthy) is not bool:
                    raise Invalid('snapshot_time_or_health')
                if not isinstance(events, list) or len(events) > 1000:
                    raise Invalid('snapshot_shape')
                snapshot = {}
                update_versions = {} if versions is None else dict(versions)
                update_terminal = {} if terminal is None else dict(terminal)
                for item in events:
                    validate_intent(item)
                    key = key_for(item['event_id'], item['stage'])
                    if key in snapshot or item['source_at'] > checked:
                        raise Invalid('snapshot_conflict')
                    snapshot[key] = item
                    update_versions.setdefault(item['event_id'], item['source_at'])
                    if item['stage'] == 'confirmed':
                        update_terminal.setdefault(item['event_id'], 'confirmed')
                # Preserve every legacy entry and its original immutable text.
                for key, item in snapshot.items():
                    original = state['entries'].get(key, {}).get('intent', {})
                    if original and 'display' not in original:
                        item.pop('display', None)
                    elif original and original.get('display') is None:
                        item['display'] = None
                    elif original and original.get('display', {}).get('style') == 'wechat-text-b-v1':
                        item['display'] = legacy_display(item.get('display'))
                for eid, version in update_versions.items():
                    key_for(eid, 'announced')
                    if not finite(version) or version > checked or version < state['versions'].get(eid, version):
                        raise Invalid('out_of_order_event_version')
                for eid, reason in update_terminal.items():
                    key_for(eid, 'announced')
                    if reason not in ('confirmed', 'withdrawn', 'unknown'):
                        raise Invalid('terminal_shape')
                if state['checked_at'] is not None and checked < state['checked_at']:
                    raise Invalid('out_of_order')
                if state['checked_at'] == checked and state['snapshot'] != snapshot:
                    # A display-only stop can remove an intent without advancing
                    # upstream verification. Permit only deactivation: every
                    # retained intent must be identical and no key may return.
                    prior = state['snapshot']
                    removals_only = (set(snapshot) < set(prior)
                                     and all(item == prior[key] for key, item in snapshot.items()))
                    if not removals_only:
                        raise Invalid('same_timestamp_conflict')
                # Terminal stage may advance; an old source version never rekeys.
                for key, item in snapshot.items():
                    prior = state['snapshot'].get(key)
                    if prior and item['source_at'] < prior['source_at']:
                        raise Invalid('out_of_order_source')
                if len(set(state['entries']) | set(snapshot)) > LIMIT:
                    raise Invalid('ledger_capacity')
                if len(set(state['versions']) | set(update_versions)) > LIMIT or len(set(state['terminal']) | set(update_terminal)) > LIMIT:
                    raise Invalid('ledger_capacity')
            except (Invalid, ValueError, TypeError, KeyError, OverflowError):
                state['healthy'] = False
                self._save(state)
                raise
            for eid, reason in update_terminal.items():
                if state['terminal'].get(eid) not in ('withdrawn', 'unknown'):
                    state['terminal'][eid] = reason
            state['versions'].update(update_versions)
            def stopped(intent):
                reason = state['terminal'].get(intent['event_id'])
                return reason in ('withdrawn', 'unknown') or (reason == 'confirmed' and intent['stage'] == 'announced')
            for key, item in state['entries'].items():
                current = snapshot.get(key)
                valid = current and not stopped(current) and current['eligible'] and now < current['expires_at'] and current['text'] == item['intent']['text']
                for name, channel in item['channels'].items():
                    rich = isinstance(item['intent'].get('display'), dict) and item['intent']['display'].get('style') == RICH_STYLE
                    safe_display = (name != 'wechat' and not rich) or current and current.get('display') == item['intent'].get('display')
                    if channel['state'] == 'pending' and not (valid and safe_display):
                        channel['state'] = 'suppressed'
            for key, intent in snapshot.items():
                if key in state['entries']:
                    continue
                historic = not state['baseline_complete'] or intent['source_at'] <= state['activation_at']
                pending = not historic and healthy and not stopped(intent) and intent['eligible'] and now < intent['expires_at']
                state['entries'][key] = {'intent': intent, 'channels': {
                    channel: dict(state='pending' if pending else 'suppressed', request_id=identity(key, channel), provider_id=None, provider_status=None)
                    for channel in CHANNELS}}
            state.update(baseline_complete=True, checked_at=checked, healthy=healthy, snapshot=snapshot)
            self._save(state)
            return {'result': 'captured', 'transport': 'platform_handoff_unavailable'}

    def claim(self, event_id, stage, channel, now):
        key = key_for(event_id, stage)
        if channel not in CHANNELS:
            raise Invalid('channel')
        with lock(self.path) as acquired:
            if not acquired:
                return {'result': 'lock_busy'}
            return self._claim_locked(event_id, stage, channel, now)

    def _claim_locked(self, event_id, stage, channel, now):
        """Caller must hold the existing dual-notifications lock."""
        key = key_for(event_id, stage)
        if channel not in CHANNELS:
            raise Invalid('channel')
        state = self._load(now)
        item = state['entries'].get(key)
        if item is None:
            return {'result': 'not_found'}
        entry = item['channels'][channel]
        if entry['state'] in ('attempting', 'waiting', 'uncertain'):
            return dict(result='reconcile_required', query_only=True, provider_id=entry['provider_id'], request_id=entry['request_id'])
        if entry['state'] != 'pending':
            return {'result': 'not_sendable', 'state': entry['state']}
        if channel == 'email':
            require_operator_config(self.operator)
        current = state['snapshot'].get(key)
        terminal = state['terminal'].get(event_id)
        if terminal in ('withdrawn', 'unknown') or terminal == 'confirmed' and stage == 'announced':
            return {'result': 'source_blocked'}
        if not state['healthy'] or state['checked_at'] is None or not state['checked_at'] <= now + 120 or now - state['checked_at'] > FRESHNESS or not current or not current['eligible']:
            return {'result': 'source_blocked'}
        if now >= min(current['expires_at'], item['intent']['expires_at']):
            entry['state'] = 'suppressed'
            self._save(state)
            return {'result': 'not_sendable', 'state': 'suppressed'}
        if (current['text'] != item['intent']['text'] or
            (channel == 'wechat' or isinstance(item['intent'].get('display'), dict) and item['intent']['display'].get('style') == RICH_STYLE)
            and current.get('display') != item['intent'].get('display')):
            return {'result': 'source_blocked'}
        if item['intent'].get('display') == {'version': 1, 'style': UNAVAILABLE_STYLE}:
            return {'result': 'presentation_unavailable'}
        if channel == 'wechat' and 'display' in item['intent'] and item['intent']['display'] is None:
            return {'result': 'wechat_display_unavailable'}
        entry['state'] = 'attempting'
        self._save(state)  # Must succeed BEFORE returning any send request.
        payload = {'notification_id': entry['request_id'], 'text': wechat_text(item['intent'])} if channel == 'wechat' else email_payload(item['intent'], self.operator)
        return dict(result='prepared', request_id=entry['request_id'], payload=payload)

    def record(self, event_id, stage, channel, receipt, now):
        key = key_for(event_id, stage)
        if channel not in CHANNELS or not isinstance(receipt, dict):
            raise Invalid('channel_or_receipt')
        with lock(self.path) as acquired:
            if not acquired:
                return {'result': 'lock_busy'}
            return self._record_locked(event_id, stage, channel, receipt, now)

    def _record_locked(self, event_id, stage, channel, receipt, now):
        """Caller must hold the existing dual-notifications lock."""
        key = key_for(event_id, stage)
        if channel not in CHANNELS or not isinstance(receipt, dict):
            raise Invalid('channel_or_receipt')
        state = self._load(now)
        item = state['entries'].get(key)
        if item is None:
            return {'result': 'not_recordable'}
        entry = item['channels'][channel]
        if entry['state'] not in ('attempting', 'waiting', 'uncertain', 'accepted'):
            return {'result': 'not_recordable'}
        incoming_pid = receipt.get('send_id' if channel == 'wechat' else 'message_id')
        pattern = r'notice_[a-f0-9]{32}' if channel == 'wechat' else r'[a-fA-F0-9]{10,64}'
        if incoming_pid is not None and (not isinstance(incoming_pid, str) or not re.fullmatch(pattern, incoming_pid)):
            raise Invalid('provider_id')
        if incoming_pid is not None and entry['provider_id'] is not None and entry['provider_id'] != incoming_pid:
            raise Invalid('provider_id_conflict')
        # A failed lookup may omit its ID. Preserve the original correlation;
        # absence is not a replacement ID or fresh proof of acceptance.
        pid = incoming_pid if incoming_pid is not None else entry['provider_id']
        if pid is not None and any(otherkey != key and other['channels'][channel]['provider_id'] == pid for otherkey, other in state['entries'].items()):
            raise Invalid('provider_id_reused')
        if channel == 'wechat':
            if 'notification_id' in receipt and receipt['notification_id'] != entry['request_id']:
                raise Invalid('notification_id_conflict')
            if 'outgoing_text' in receipt and receipt['outgoing_text'] != wechat_text(item['intent']):
                raise Invalid('receipt_text_conflict')
            status = receipt.get('status')
            matched = receipt.get('outgoing_text') == wechat_text(item['intent'])
            accepted = incoming_pid is not None and matched and status == 'accepted' and receipt.get('accepted') is True and 'delivered' in receipt and receipt['delivered'] is None and receipt.get('delivery_receipt_available') is False
            target = 'accepted' if accepted else 'waiting' if incoming_pid and matched and status in ('queued', 'preparing', 'sending', 'context_required', 'auth_required') else 'rejected' if incoming_pid and matched and status == 'rejected' else 'uncertain'
        else:
            expected = email_payload(item['intent'], self.operator)
            addrs = getaddresses([receipt['to']]) if isinstance(receipt.get('to'), str) else []
            # An absent readback can be retried as a query. Explicit content
            # from another message must never pin its ID to this attempt,
            # even when a different readback field is missing.
            if receipt.get('readback_id') is not None and pid is not None and receipt['readback_id'] != pid:
                raise Invalid('receipt_readback_conflict')
            if receipt.get('to') is not None and (len(addrs) != 1 or addrs[0][1].lower() != self.operator.recipient):
                raise Invalid('receipt_recipient_conflict')
            for field, value in (('subject', expected['subject']),
                                 ('body_html', expected['payload']['body']['content'])):
                if receipt.get(field) is not None and receipt[field] != value:
                    raise Invalid('receipt_content_conflict')
            if any(receipt.get(field) is not None and receipt[field] != '' for field in ('cc', 'bcc')):
                raise Invalid('receipt_recipient_conflict')
            accepted = ({'message_id', 'readback_id', 'to', 'subject', 'body_html', 'label_ids', 'cc', 'bcc'} <= set(receipt)
                and incoming_pid is not None and receipt.get('readback_id') == pid
                and len(addrs) == 1 and addrs[0][1].lower() == self.operator.recipient
                and receipt.get('subject') == expected['subject']
                and receipt.get('body_html') == expected['payload']['body']['content']
                and isinstance(receipt.get('label_ids'), list) and all(isinstance(x, str) for x in receipt['label_ids']) and 'SENT' in receipt['label_ids']
                and not set(receipt['label_ids']) & {'DRAFT', 'TRASH'}
                and receipt['cc'] == '' and receipt['bcc'] == '')
            target, status = ('accepted', 'sent') if accepted else ('uncertain', 'unverified')
        if entry['state'] == 'accepted':
            return {'result': 'already_accepted', 'provider_id': entry['provider_id'], 'delivered': None}
        entry.update(state=target, provider_id=pid, provider_status=status if isinstance(status, str) and status in ('accepted', 'queued', 'preparing', 'sending', 'context_required', 'auth_required', 'rejected', 'delivery_unknown', 'uncertain', 'sent', 'unverified') else 'unknown')
        self._save(state)
        return dict(result='recorded', state=target, provider_id=pid, accepted=target == 'accepted', delivered=None)
