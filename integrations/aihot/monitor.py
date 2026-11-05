#!/usr/bin/python3
"""Public AIHOT polling/capture only. No sender or task/model executor."""
import argparse
import contextlib
import datetime as dt
import email.utils
import fcntl
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import stat
import uuid
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

ENDPOINT = 'https://aihot.news/api/v1/codex-resets/recent'
INTERVAL = 600
FRESHNESS = 1800
MAX_BODY = 1024 * 1024
MAX_SEEN = 10000
# Bound container nesting before parsing, independently of Python's recursion
# limit. Source snapshots and local ledgers use far fewer than 64 levels.
MAX_JSON_DEPTH = 64
TASKS = ()
ROOT = Path(__file__).resolve().parent


class Invalid(Exception):
    pass


def strict_json(raw):
    """Parse bounded nesting without relying on the interpreter recursion limit."""
    # Preserve json.loads' accepted byte encodings while scanning characters.
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode(json.detect_encoding(raw), 'surrogatepass')
    if isinstance(raw, str):
        depth = 0
        in_string = escaped = False
        for character in raw:
            if in_string:
                if escaped:
                    escaped = False
                elif character == '\\':
                    escaped = True
                elif character == '"':
                    in_string = False
            elif character == '"':
                in_string = True
            elif character in '[{':
                depth += 1
                if depth > MAX_JSON_DEPTH:
                    raise Invalid('response_too_deep')
            elif character in ']}':
                # Syntax and matching delimiters remain the JSON parser's job.
                depth = max(0, depth - 1)

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Invalid('duplicate_json_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(Invalid('nonfinite')))


_MISSING = object()


@contextlib.contextmanager
def private_directory(data, create=False):
    """Pin a private owned directory; never follow a directory symlink."""
    data = Path(data)
    if create:
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(data, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise Invalid('private_data_directory')
        yield fd
    finally:
        os.close(fd)


def private_file(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
        raise Invalid('private_data_file')


def checked_existing(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    private_file(info)
    return info


def read_private_json(path, limit=32 * 1024 * 1024):
    """Read bounded JSON from a private regular file, or report absence."""
    path = Path(path)
    try:
        with private_directory(path.parent) as directory:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory)
            with os.fdopen(fd, 'rb') as handle:
                info = os.fstat(handle.fileno())
                private_file(info)
                if info.st_size > limit:
                    raise Invalid('private_file_size')
                raw = handle.read(limit + 1)
                if len(raw) > limit:
                    raise Invalid('private_file_size')
                return strict_json(raw)
    except FileNotFoundError:
        return _MISSING


def atomic_json(path, value):
    path = Path(path)
    with private_directory(path.parent) as directory:
        checked_existing(directory, path.name)
        name = '.' + path.name + '.' + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as out:
                json.dump(value, out, ensure_ascii=False, sort_keys=True,
                          indent=2, allow_nan=False)
                out.write('\n')
                out.flush()
                os.fsync(out.fileno())
            # Check again before replacing an existing private state file.
            checked_existing(directory, path.name)
            os.replace(name, path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass


@contextlib.contextmanager
def lock(data):
    with private_directory(data, create=True) as directory:
        fd = os.open('monitor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, 'r+') as handle:
            private_file(os.fstat(handle.fileno()))
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def stamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise Invalid('invalid_timestamp')
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.timestamp()
    except (ValueError, OverflowError):
        raise Invalid('invalid_timestamp')


def utc(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()


def new_state(now):
    return dict(version=1, activation_at=now, baseline_complete=False, seen={},
                candidates={}, snapshot={}, etag=None, snapshot_checked_at=None,
                last_success_at=None, last_run_at=None, failures=0, next_poll_at=0,
                poll_count=0, network_count=0, edge_denials=0, route_stopped=False,
                mode='monitor-only', dispatch_enabled=False)


def load_state(data, now):
    path = data / 'state.json'
    state = read_private_json(path)
    if state is _MISSING:
        return new_state(now)
    if not isinstance(state, dict) or state.get('version') != 1:
        raise Invalid('state_version')
    if state.get('mode') != 'monitor-only' or state.get('dispatch_enabled') is not False:
        raise Invalid('state_mode')
    required = set(new_state(now))
    if set(state) != required or not isinstance(state['seen'], dict) or not isinstance(state['candidates'], dict):
        raise Invalid('state_shape')
    if len(state['seen']) > MAX_SEEN or not isinstance(state['snapshot'], dict):
        raise Invalid('state_capacity')
    for name in ('activation_at', 'next_poll_at'):
        if type(state[name]) not in (int, float) or not math.isfinite(state[name]):
            raise Invalid('state_time')
    for name in ('snapshot_checked_at', 'last_success_at', 'last_run_at'):
        if state[name] is not None and (type(state[name]) not in (int, float) or not math.isfinite(state[name])):
            raise Invalid('state_time')
    for name in ('poll_count', 'network_count', 'failures', 'edge_denials'):
        if type(state[name]) is not int or state[name] < 0:
            raise Invalid('state_count')
    if type(state['baseline_complete']) is not bool or type(state['route_stopped']) is not bool:
        raise Invalid('state_flag')
    for eid, seen in state['seen'].items():
        event_id(eid)
        if not isinstance(seen, dict) or set(seen) != {'first_seen_at', 'disposition'} or type(seen['first_seen_at']) not in (int, float) or not isinstance(seen['disposition'], str):
            raise Invalid('state_seen')
    for eid, candidate in state['candidates'].items():
        event_id(eid)
        if not isinstance(candidate, dict) or candidate.get('state') not in ('active', 'stopped') or candidate.get('event_id') != eid:
            raise Invalid('state_candidate')
        for name in ('starts_at', 'ends_at', 'created_at'):
            if type(candidate.get(name)) not in (int, float) or not math.isfinite(candidate[name]):
                raise Invalid('state_candidate_time')
        if candidate.get('task_id') is not None or candidate.get('runtime_cap_enforced') is not False:
            raise Invalid('state_candidate_dispatch')
    for eid, event in state['snapshot'].items():
        event_id(eid)
        if not isinstance(event, dict) or event.get('id') != eid or type(event.get('valid')) is not bool:
            raise Invalid('state_snapshot')
    return state


def log(data, now, action, **values):
    # Only code-defined categories, numeric counts, and sanitized identifiers.
    row = dict(at=utc(now), action=action, mode='monitor-only', dispatch_enabled=False, **values)
    with private_directory(data) as directory:
        info = checked_existing(directory, 'monitor.jsonl')
        if info is not None and info.st_size > 2 * 1024 * 1024:
            checked_existing(directory, 'monitor.previous.jsonl')
            os.replace('monitor.jsonl', 'monitor.previous.jsonl',
                       src_dir_fd=directory, dst_dir_fd=directory)
        fd = os.open('monitor.jsonl', os.O_CREAT | os.O_APPEND | os.O_WRONLY
                     | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'a', encoding='utf-8') as out:
            private_file(os.fstat(out.fileno()))
            out.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + '\n')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(etag):
    headers = {'Accept': 'application/json', 'User-Agent': 'aihot-monitor-only/1.0 (+https://aihot.news/)'}
    if isinstance(etag, str) and re.fullmatch(r'[\x20-\x7e]{1,512}', etag):
        headers['If-None-Match'] = etag
    # No auth/cookies, inherited proxy credentials, redirects, or non-AIHOT endpoints.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(ENDPOINT, headers=headers, method='GET')
    try:
        with opener.open(request, timeout=25) as response:
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise Invalid('response_too_large')
            if 'application/json' not in response.headers.get('Content-Type', '').lower():
                raise Invalid('content_type')
            return response.status, dict(response.headers), strict_json(raw)
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), None


def stop_all(state, reason, now):
    for candidate in state['candidates'].values():
        if candidate['state'] == 'active':
            candidate.update(state='stopped', stop_reason=reason, stopped_at=now)


def retry_seconds(headers, now):
    value = next((v for k, v in headers.items() if k.lower() == 'retry-after'), '')
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        try:
            return max(0, int(email.utils.parsedate_to_datetime(value).timestamp() - now))
        except (ValueError, TypeError, OverflowError):
            return 0


def failure(state, now, reason, headers=None):
    # A 304 cannot revalidate the body or recover suspended capture ledgers.
    # Retry once on the existing schedule with a full response after a failure.
    state['etag'] = None
    state['failures'] += 1
    delay = max(min(21600, INTERVAL * (2 ** min(state['failures'] - 1, 6))),
                retry_seconds(headers or {}, now))
    if reason in ('http_566', 'http_567'):
        state['edge_denials'] += 1
        if state['edge_denials'] >= 2:
            state['route_stopped'] = True
    if delay > 7 * 86400:
        state['route_stopped'] = True
        delay = 7 * 86400
    state['next_poll_at'] = now + delay
    stop_all(state, reason, now)
    return dict(result=reason, retry_in_seconds=delay)


def task_gate():
    # A current app UI usage snapshot cannot be queried safely by this standalone poller.
    # No token/time approximation and no readiness assertion from stale project files.
    return dict(task_id=None, blocked_reasons=['monitor_only', 'quota_reserve_unverifiable',
                'task_readiness_requires_current_evidence', 'runtime_cancel_control_unverified',
                'account_scope_applicability_unverified'],
                allowed_task_ids=list(TASKS), proposed_runtime_cap_seconds=None,
                runtime_cap_enforced=False)


def event_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value):
        raise Invalid('event_id')
    return value


def envelope(payload, now):
    """Validate the official snapshot before any event can create a local candidate."""
    if not isinstance(payload, dict) or type(payload.get('schemaVersion')) is not int or payload['schemaVersion'] != 1:
        raise Invalid('schema_version')
    if not {'timezone', 'checkedAt', 'historyFrom', 'count', 'events', 'activities', 'monitor', 'outage'} <= set(payload):
        raise Invalid('envelope_missing_fields')
    stamp(payload['historyFrom'])
    checked = stamp(payload.get('checkedAt'))
    if checked > now + 120 or now - checked > FRESHNESS:
        raise Invalid('stale_or_future_snapshot')
    if not isinstance(payload.get('events'), list) or len(payload['events']) > 1000:
        raise Invalid('events_shape')
    if type(payload.get('count')) is not int or payload['count'] != len(payload['events']):
        raise Invalid('count_mismatch')
    monitor = payload.get('monitor')
    if not isinstance(monitor, dict) or monitor.get('status') != 'healthy':
        raise Invalid('monitor_not_healthy')
    if type(monitor.get('heldWindowCount')) is not int or type(monitor.get('reviewCount')) is not int or monitor['heldWindowCount'] != 0 or monitor['reviewCount'] != 0:
        raise Invalid('held_or_review_pending')
    if type(monitor.get('pendingCount')) is not int or monitor['pendingCount'] < 0:
        raise Invalid('monitor_counts_unknown')
    verified = stamp(monitor.get('lastVerifiedAt'))
    collected = stamp(monitor.get('lastCollectedAt'))
    attempted = stamp(monitor.get('lastAttemptAt'))
    if not checked == verified <= collected <= attempted <= now + 120:
        raise Invalid('monitor_timestamp_conflict')
    if payload.get('timezone') != 'Asia/Shanghai':
        raise Invalid('timezone_unknown')
    if payload.get('outage') is not None:
        raise Invalid('outage_or_unknown')
    activities = payload.get('activities')
    if not isinstance(activities, list):
        raise Invalid('activities_shape')
    blockers = {}
    terminal = set()
    announcements = {}
    for activity in activities:
        if not isinstance(activity, dict) or activity.get('action') not in ('announce', 'confirm', 'amend', 'withdraw', None):
            raise Invalid('activity_unknown')
        if activity.get('action') is None and activity.get('statusChanged') is not False:
            # The official schema permits null action on historical changed posts.
            # Keep their referenced events permanently blocked without rejecting an
            # otherwise coherent snapshot of unrelated, already confirmed history.
            ids = activity.get('eventIds')
            if activity.get('statusChanged') is not True or activity.get('kind') != 'event_update' or not isinstance(ids, list) or not ids:
                raise Invalid('unknown_status_change')
            published = stamp(activity.get('publishedAt'))
            if published > now + 120:
                raise Invalid('future_activity')
            ids = [event_id(x) for x in ids]
            blockers.update({eid: 'unknown_activity_requires_review' for eid in ids})
            terminal.update(ids)
        if activity.get('action') in ('withdraw', 'confirm', 'announce', 'amend'):
            ids = activity.get('eventIds')
            if not isinstance(ids, list):
                raise Invalid('activity_ids_shape')
            ids = [event_id(x) for x in ids]
            if activity.get('statusChanged') is not True or activity.get('kind') != 'event_update':
                raise Invalid('activity_status_conflict')
            published = stamp(activity.get('publishedAt'))
            if published > now + 120:
                raise Invalid('future_activity')
            if activity['action'] == 'withdraw':
                blockers.update({eid: 'withdrawn' for eid in ids})
                terminal.update(ids)
            elif activity['action'] == 'confirm':
                blockers.update({eid: 'confirmation_activity' for eid in ids if eid not in blockers})
                terminal.update(ids)
            elif activity['action'] == 'amend':
                blockers.update({eid: 'amendment_requires_review' for eid in ids if eid not in blockers})
                terminal.update(ids)
            else:
                for eid in ids:
                    announcements[eid] = max(published, announcements.get(eid, 0))
    events = {}
    for raw in payload['events']:
        if not isinstance(raw, dict):
            raise Invalid('event_shape')
        eid = event_id(raw.get('id'))
        if eid in events:
            raise Invalid('duplicate_event_conflict')
        events[eid] = normalize(raw, now, blockers)
        events[eid]['source_announcement_at'] = announcements.get(eid)
        if events[eid]['valid'] and (events[eid]['updated_at'] > checked or announcements.get(eid, 0) > checked):
            events[eid].update(valid=False, reason='event_newer_than_verification')
    # Two active forecasts with different windows can refer to an unresolved regrouping.
    windows = {(e['starts_at'], e['ends_at']) for e in events.values() if e['valid']}
    if len(windows) > 1:
        for event in events.values():
            if event['valid']:
                event.update(valid=False, reason='conflicting_forecasts')
    return checked, events, terminal


def normalize(raw, now, blockers):
    # Filled only from documented structured fields; titles, notes and URLs are ignored.
    eid = event_id(raw.get('id'))
    item = dict(id=eid, valid=False, reason='unknown', starts_at=None, ends_at=None,
                announced_at=None, updated_at=None)
    if not {'id', 'type', 'label', 'status', 'title', 'scope', 'createdAt', 'updatedAt',
            'confirmedAt', 'occurredOn', 'confirmationBasis', 'schedule', 'posts', 'url'} <= set(raw):
        item['reason'] = 'event_missing_fields'
        return item
    if eid in blockers:
        item['reason'] = blockers[eid]
        return item
    if raw.get('status') != 'announced':
        item['reason'] = 'confirmed_or_unknown_status'
        return item
    if raw.get('type') != 'direct_reset' or raw.get('confirmedAt') is not None or raw.get('confirmationBasis') is not None or raw.get('occurredOn') is not None:
        item['reason'] = 'unknown_form_or_confirmation_conflict'
        return item
    presentation = raw.get('presentation')
    if not isinstance(presentation, dict) or presentation.get('status') != 'announced':
        item['reason'] = 'expired_in_progress_or_unknown'
        return item
    if presentation.get('scopeKnown') is not True:
        item['reason'] = 'unknown_scope'
        return item
    if presentation.get('kindExplicit') is not True or not isinstance(presentation.get('scopeLabel'), str) or not presentation['scopeLabel'].strip():
        item['reason'] = 'unknown_scope_or_form'
        return item
    # Strict contract mapping is deliberately fail-closed on missing/ambiguous times.
    try:
        item['announced_at'] = stamp(raw.get('createdAt'))
        item['updated_at'] = stamp(raw.get('updatedAt'))
        schedule = raw.get('schedule')
        if not isinstance(schedule, dict):
            raise Invalid('schedule_missing')
        if schedule.get('precision') not in ('exact', 'window'):
            raise Invalid('ambiguous_precision')
        if presentation.get('timeInferred') is not False:
            raise Invalid('inferred_time')
        item['starts_at'] = stamp(schedule.get('from'))
        item['ends_at'] = stamp(schedule.get('through'))
    except Invalid:
        item['reason'] = 'unknown_or_estimated_window'
        return item
    start, end = item['starts_at'], item['ends_at']
    if not item['announced_at'] <= item['updated_at'] <= now + 120:
        item['reason'] = 'invalid_event_chronology'
    elif not now < start <= end or end - start > 86400 or start - now > 7 * 86400:
        item['reason'] = 'expired_or_ambiguous_window'
    else:
        item.update(valid=True, reason='valid_source_forecast')
    return item


def forecast_deactivation_only(previous, current, payload, now):
    """Same-watermark clock/display changes may only remove actionability."""
    if set(previous) != set(current):
        return False
    raw_by_id = {raw['id']: raw for raw in payload['events']}
    for eid, item in current.items():
        old, raw = previous[eid], raw_by_id[eid]
        if old == item:
            continue
        if old.get('valid') is not True or item.get('valid') is not False:
            return False
        if (raw.get('status') != 'announced' or raw.get('type') != 'direct_reset'
                or any(raw.get(n) is not None for n in ('confirmedAt', 'confirmationBasis', 'occurredOn'))
                or stamp(raw.get('createdAt')) != old.get('announced_at')
                or stamp(raw.get('updatedAt')) != old.get('updated_at')
                or item.get('source_announcement_at') != old.get('source_announcement_at')):
            return False
        schedule = raw.get('schedule')
        if not isinstance(schedule, dict) or (stamp(schedule.get('from')), stamp(schedule.get('through'))) != (old.get('starts_at'), old.get('ends_at')):
            return False
        presentation = raw.get('presentation')
        display = presentation.get('status') if isinstance(presentation, dict) else None
        display_stop = item['reason'] == 'expired_in_progress_or_unknown' and display in ('in_progress', 'expired_unconfirmed', 'likely_completed')
        clock_stop = item['reason'] == 'expired_or_ambiguous_window' and now >= old['starts_at']
        if not (display_stop or clock_stop):
            return False
    return True


def apply_snapshot(state, payload, now):
    checked, events, terminal = envelope(payload, now)
    if state['snapshot_checked_at'] is not None and checked < state['snapshot_checked_at']:
        raise Invalid('out_of_order_snapshot')
    # Changes at the same watermark remain conflicts except one-way clock/display stops.
    if state['snapshot_checked_at'] == checked and state['snapshot'] != events and not forecast_deactivation_only(state['snapshot'], events, payload, now):
        raise Invalid('same_timestamp_conflict')
    for eid, event in events.items():
        previous = state['snapshot'].get(eid)
        if previous and previous.get('updated_at') is not None and event.get('updated_at') is not None and event['updated_at'] < previous['updated_at']:
            raise Invalid('out_of_order_event')
    if len(set(state['seen']) | set(events) | terminal) > MAX_SEEN:
        raise Invalid('dedup_capacity')
    baseline = not state['baseline_complete']
    created = 0
    for eid in terminal:
        state['seen'].setdefault(eid, dict(first_seen_at=now, disposition='terminal_activity'))
    for eid, candidate in state['candidates'].items():
        event = events.get(eid)
        if candidate['state'] == 'active' and (event is None or not event['valid']):
            candidate.update(state='stopped', stop_reason=event['reason'] if event else 'missing_or_withdrawn', stopped_at=now)
        elif candidate['state'] == 'active' and (event['starts_at'], event['ends_at']) != (candidate['starts_at'], candidate['ends_at']):
            candidate.update(state='stopped', stop_reason='amended_window', stopped_at=now)
    for eid, event in events.items():
        if eid in state['seen']:
            continue
        state['seen'][eid] = dict(first_seen_at=now, disposition='startup_baseline' if baseline else event['reason'])
        source_time = event.get('source_announcement_at')
        if not baseline and event['valid'] and event['announced_at'] > state['activation_at'] and source_time is not None and source_time > state['activation_at']:
            state['candidates'][eid] = dict(event_id=eid, created_at=now, state='active',
                starts_at=event['starts_at'], ends_at=event['ends_at'], **task_gate())
            created += 1
    state.update(snapshot=events, snapshot_checked_at=checked, baseline_complete=True)
    return dict(result='baseline' if baseline else 'snapshot', events=len(events), new_candidates=created)


def expire(state, now):
    if state['snapshot_checked_at'] is not None and now - state['snapshot_checked_at'] > FRESHNESS:
        stop_all(state, 'snapshot_stale', now)
    for candidate in state['candidates'].values():
        if candidate['state'] == 'active' and now >= candidate['starts_at']:
            candidate.update(state='stopped', stop_reason='window_started_or_expired', stopped_at=now)


# Notifications have their own ledger. This never grants model/task execution.
# The existing bridge exposes MCP only; this Python service has NO send transport.
# prepare/record are operator tools for a separately authorized MCP acceptance test.
NOTICE_STATES = ('pending', 'attempting', 'accepted', 'uncertain', 'waiting',
                 'rejected', 'suppressed')
BRIDGE_STATES = ('queued', 'preparing', 'sending', 'accepted', 'context_required',
                 'auth_required', 'rejected', 'delivery_unknown', 'uncertain')


def notification_load(data):
    path = data / 'notifications.json'
    ledger = read_private_json(path)
    if ledger is _MISSING:
        return None
    if not isinstance(ledger, dict) or ledger.get('version') != 1 or type(ledger.get('enabled')) is not bool:
        raise Invalid('notification_ledger')
    if set(ledger) != {'version', 'enabled', 'activation_at', 'baseline_complete', 'checked_at',
                      'events', 'entries', 'source_health', 'accept_event', 'transport'}:
        raise Invalid('notification_ledger_shape')
    if not isinstance(ledger.get('entries'), dict) or not isinstance(ledger.get('events'), dict):
        raise Invalid('notification_ledger')
    if len(ledger['entries']) > 2 * MAX_SEEN or len(ledger['events']) > MAX_SEEN:
        raise Invalid('notification_capacity')
    if type(ledger.get('activation_at')) not in (int, float) or not math.isfinite(ledger['activation_at']) or type(ledger.get('baseline_complete')) is not bool:
        raise Invalid('notification_ledger_time')
    if ledger.get('checked_at') is not None and (type(ledger['checked_at']) not in (int, float) or not math.isfinite(ledger['checked_at'])):
        raise Invalid('notification_ledger_time')
    if ledger.get('transport') != 'transport_blocked' or not isinstance(ledger.get('source_health'), str):
        raise Invalid('notification_ledger_transport')
    if ledger['baseline_complete'] and ledger['checked_at'] is None:
        raise Invalid('notification_ledger_time')
    if ledger['accept_event'] is not None:
        event_id(ledger['accept_event'])
    for eid, view in ledger['events'].items():
        event_id(eid)
        if not isinstance(view, dict) or not {'updated_at', 'stage', 'eligible', 'reason'} <= set(view) or not set(view) <= {'updated_at', 'stage', 'eligible', 'reason', 'source_at', 'text', 'expires_at'}:
            raise Invalid('notification_view_shape')
        if type(view['eligible']) is not bool or not isinstance(view['reason'], str):
            raise Invalid('notification_view_shape')
        for name in ('updated_at', 'source_at', 'expires_at'):
            if name in view and (type(view[name]) not in (int, float) or not math.isfinite(view[name])):
                raise Invalid('notification_view_time')
        if view['eligible']:
            if view['stage'] not in ('announced', 'confirmed') or not {'source_at', 'text', 'expires_at'} <= set(view) or not isinstance(view['text'], str):
                raise Invalid('notification_view_shape')
            url = view['text'].splitlines()[-1] if view['text'] else ''
            if not re.fullmatch(r'https://x\.com/thsottiaux/status/[0-9]{5,25}', url) or view['text'] != notification_text(view['stage'], view['source_at'], url):
                raise Invalid('notification_public_template')
    for key, entry in ledger['entries'].items():
        if not isinstance(entry, dict) or entry.get('stage') not in ('announced', 'confirmed'):
            raise Invalid('notification_entry')
        required = {'event_id', 'stage', 'state', 'notification_id', 'text', 'source_at',
                    'expires_at', 'created_at', 'send_id', 'bridge_status', 'delivered', 'reason'}
        if not required <= set(entry) or not set(entry) <= required | {'prepared_at', 'receipt_at'}:
            raise Invalid('notification_entry_shape')
        if key != event_id(entry.get('event_id')) + '/' + entry['stage'] or entry.get('state') not in NOTICE_STATES:
            raise Invalid('notification_entry')
        expected = 'aihot-v1-' + hashlib.sha256(key.encode()).hexdigest()[:40]
        if entry.get('notification_id') != expected or not isinstance(entry.get('text'), str) or len(entry['text'].encode()) > 2048:
            raise Invalid('notification_identity')
        for name in ('source_at', 'created_at', 'expires_at', 'prepared_at', 'receipt_at'):
            if name not in entry:
                continue
            if type(entry.get(name)) not in (int, float) or not math.isfinite(entry[name]):
                raise Invalid('notification_entry_time')
        source = entry['text'].splitlines()[-1] if entry['text'] else ''
        if not re.fullmatch(r'https://x\.com/thsottiaux/status/[0-9]{5,25}', source) or entry['text'] != notification_text(entry['stage'], entry['source_at'], source):
            raise Invalid('notification_public_template')
        if entry.get('send_id') is not None and (not isinstance(entry['send_id'], str) or not re.fullmatch(r'notice_[a-f0-9]{32}', entry['send_id'])):
            raise Invalid('notification_send_id')
        if entry['delivered'] is not None or entry['bridge_status'] not in BRIDGE_STATES + ('unknown', None):
            raise Invalid('notification_receipt')
        if entry['state'] in ('accepted', 'waiting', 'rejected') and entry['send_id'] is None:
            raise Invalid('notification_receipt')
        if entry['state'] == 'accepted' and entry['bridge_status'] != 'accepted':
            raise Invalid('notification_receipt')
    return ledger


def notification_status(data):
    ledger = notification_load(data)
    return ledger if ledger is not None else {'enabled': False, 'transport': 'transport_blocked', 'entries': {}}


def notification_enable(data, now=None, accept_event=None):
    now = time.time() if now is None else now
    if accept_event is not None:
        event_id(accept_event)
    with lock(data) as acquired:
        if not acquired:
            return {'result': 'lock_busy'}
        ledger = notification_load(data)
        if ledger is None:
            ledger = dict(version=1, enabled=True, activation_at=now,
                          baseline_complete=False, checked_at=None, events={}, entries={},
                          source_health='awaiting_baseline', accept_event=accept_event,
                          transport='transport_blocked')
        elif ledger['enabled']:
            # Repeating enable must not rearm the one-time acceptance exception.
            return {'result': 'already_enabled', 'transport': 'transport_blocked'}
        else:
            ledger.update(enabled=True, activation_at=now, baseline_complete=False,
                          source_health='awaiting_baseline', accept_event=None)
        atomic_json(data / 'notifications.json', ledger)
        # Force the next normal poll to obtain a body instead of a 304 baseline.
        if (data / 'state.json').exists():
            state = load_state(data, now)
            state.update(etag=None)
            atomic_json(data / 'state.json', state)
        return {'result': 'enabled_capture_only', 'transport': 'transport_blocked'}


def notification_disable(data):
    with lock(data) as acquired:
        if not acquired:
            return {'result': 'lock_busy'}
        ledger = notification_load(data)
        if ledger is not None:
            ledger.update(enabled=False, source_health='paused')
            atomic_json(data / 'notifications.json', ledger)
        return {'result': 'disabled', 'transport': 'transport_blocked'}


def notification_suspend(data, reason):
    ledger = notification_load(data)
    if ledger is not None:
        ledger['source_health'] = reason
        atomic_json(data / 'notifications.json', ledger)


def notification_text(stage, source_at, source):
    beijing = dt.datetime.fromtimestamp(source_at, dt.timezone(dt.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    if stage == 'announced':
        return 'AIHOT：Codex 新重置预告。\n来源发布时间：' + beijing + '（北京时间）。\n具体生效以官方确认及账号实际为准。\n' + source
    return 'AIHOT：Codex 新重置确认（来源公告）。\n来源发布时间：' + beijing + '（北京时间）。\n个人账号到账及适用范围请以账号实际为准。\n' + source


def notification_view(payload, checked):
    views = {}
    for raw in payload['events']:
        eid = event_id(raw['id'])
        stage = raw.get('status')
        created, updated = stamp(raw.get('createdAt')), stamp(raw.get('updatedAt'))
        if not created <= updated <= checked:
            raise Invalid('notification_event_chronology')
        view = dict(updated_at=updated, stage=stage, eligible=False, reason='unknown_or_conflicting')
        views[eid] = view
        presentation = raw.get('presentation')
        if stage not in ('announced', 'confirmed') or raw.get('type') != 'direct_reset' or not isinstance(presentation, dict):
            continue
        if stage == 'announced' and any(raw.get(name) is not None for name in ('confirmedAt', 'confirmationBasis', 'occurredOn')):
            view['reason'] = 'source_confirmation_conflict'
            continue
        if presentation.get('status') != stage:
            view['reason'] = ({'likely_completed': 'estimated_completion_unconfirmed',
                               'expired_unconfirmed': 'expired_unconfirmed',
                               'in_progress': 'in_progress_unconfirmed'}.get(presentation.get('status'), 'status_conflict')
                              if stage == 'announced' else 'status_conflict')
            continue
        if stage == 'announced':
            source_at, action = created, 'announce'
            expires_at = created + 7 * 86400
            # Source deadline is used only as a conservative expiry, never displayed
            # as an exact promise. AIHOT estimates and source prose are not executed.
            schedule = raw.get('schedule')
            if isinstance(schedule, dict) and schedule.get('through') is not None:
                expires_at = min(expires_at, stamp(schedule['through']))
            if expires_at <= checked:
                view['reason'] = 'expired'
                continue
        else:
            if raw.get('confirmationBasis') != 'source_post':
                view['reason'] = 'confirmation_not_source_post'
                continue
            source_at, action = stamp(raw.get('confirmedAt')), 'confirm'
            expires_at = source_at + 7 * 86400
            if not created <= source_at <= updated:
                continue
        sources = []
        unsafe = False
        for a in payload['activities']:
            if eid not in a.get('eventIds', []):
                continue
            if a.get('action') == 'withdraw' or (a.get('action') is None and a.get('statusChanged') is True):
                unsafe = True
            if stage == 'announced' and a.get('action') == 'confirm':
                unsafe = True
            if a.get('action') == action and stamp(a.get('publishedAt')) == source_at:
                url = a.get('url')
                if isinstance(url, str) and re.fullmatch(r'https://x\.com/thsottiaux/status/[0-9]{5,25}', url):
                    sources.append(url)
        if unsafe or len(set(sources)) != 1:
            view['reason'] = 'withdrawn_or_unknown_source'
            continue
        source = sources[0]
        text = notification_text(stage, source_at, source)
        view.update(eligible=True, reason='source_' + stage, source_at=source_at, text=text,
                    expires_at=expires_at)
    return views


SOURCE_CLASSES = ('source_announced', 'source_confirmed', 'estimated_completion_unconfirmed',
                  'expired_unconfirmed', 'in_progress_unconfirmed', 'conflicting_or_unverified')
PRESENTATION_STATES = ('announced', 'confirmed', 'likely_completed', 'expired_unconfirmed', 'in_progress', 'unknown')
NOTICE_REASONS = ('source_announced', 'source_confirmed', 'unknown_or_conflicting', 'source_confirmation_conflict',
                 'estimated_completion_unconfirmed', 'expired_unconfirmed', 'in_progress_unconfirmed',
                 'status_conflict', 'expired', 'confirmation_not_source_post', 'withdrawn_or_unknown_source', 'conflicting_forecasts')


def source_observation_status(data):
    """Diagnostic evidence only; never read by the notification eligibility gate."""
    path = data / 'source-observations.json'
    value = read_private_json(path, limit=MAX_BODY)
    if value is _MISSING:
        return {'version': 1, 'checked_at': None, 'events': {}}
    if not isinstance(value, dict) or set(value) != {'version', 'checked_at', 'events'} or value['version'] != 1:
        raise Invalid('observation_shape')
    if not isinstance(value['events'], dict) or len(value['events']) > 1000:
        raise Invalid('observation_capacity')
    if type(value['checked_at']) not in (int, float) or not math.isfinite(value['checked_at']):
        raise Invalid('observation_time')
    for eid, item in value['events'].items():
        event_id(eid)
        if not isinstance(item, dict) or set(item) != {'source_status', 'presentation_status', 'classification',
                'notification_eligible', 'source_updated_at', 'confirmed_at', 'confirmation_basis',
                'ineligibility_reason', 'first_observed_at', 'changed_at'}:
            raise Invalid('observation_entry')
        if item['source_status'] not in ('announced', 'confirmed', 'unknown') or item['presentation_status'] not in PRESENTATION_STATES or item['classification'] not in SOURCE_CLASSES or type(item['notification_eligible']) is not bool:
            raise Invalid('observation_class')
        if item['confirmation_basis'] not in (None, 'source_post', 'receipt_review', 'unknown') or item['ineligibility_reason'] not in NOTICE_REASONS:
            raise Invalid('observation_class')
        for name in ('source_updated_at', 'first_observed_at', 'changed_at', 'confirmed_at'):
            if name == 'confirmed_at' and item[name] is None:
                continue
            if type(item[name]) not in (int, float) or not math.isfinite(item[name]):
                raise Invalid('observation_time')
    return value


def observe_source(data, payload, now):
    """Caller holds monitor.lock; record display changes without creating notices.

    likely_completed is an AIHOT estimate, never an official confirmation or
    evidence about this owner's account. No new notification category is enabled.
    """
    checked, normalized, _ = envelope(payload, now)
    views = notification_view(payload, checked)
    for eid, event in normalized.items():
        if event['reason'] == 'conflicting_forecasts':
            views[eid].update(eligible=False, reason='conflicting_forecasts')
    previous = source_observation_status(data)
    if previous['checked_at'] is not None and checked < previous['checked_at']:
        raise Invalid('observation_out_of_order')
    events, changed = {}, []
    for raw in payload['events']:
        eid, view = raw['id'], views[raw['id']]
        stage = raw['status'] if raw.get('status') in ('announced', 'confirmed') else 'unknown'
        presentation = raw.get('presentation')
        display = presentation.get('status') if isinstance(presentation, dict) else None
        display = display if display in PRESENTATION_STATES else 'unknown'
        category = 'conflicting_or_unverified'
        if stage == 'announced' and all(raw.get(n) is None for n in ('confirmedAt', 'confirmationBasis', 'occurredOn')):
            category = {'likely_completed': 'estimated_completion_unconfirmed',
                        'expired_unconfirmed': 'expired_unconfirmed',
                        'in_progress': 'in_progress_unconfirmed'}.get(display, category)
        if view['eligible']:
            category = 'source_' + stage
        # This cache records evidence, not source prose, scope claims or links.
        item = dict(source_status=stage, presentation_status=display, classification=category,
                    notification_eligible=view['eligible'], source_updated_at=view['updated_at'],
                    confirmed_at=stamp(raw['confirmedAt']) if raw.get('confirmedAt') is not None else None,
                    confirmation_basis=raw.get('confirmationBasis') if raw.get('confirmationBasis') in (None, 'source_post', 'receipt_review') else 'unknown',
                    ineligibility_reason=view['reason'] if view['reason'] in NOTICE_REASONS else 'unknown_or_conflicting')
        old = previous['events'].get(eid)
        if old and item['source_updated_at'] < old['source_updated_at']:
            raise Invalid('observation_out_of_order')
        same = old and all(old[name] == value for name, value in item.items())
        item.update(first_observed_at=old['first_observed_at'] if old else now,
                    changed_at=old['changed_at'] if same else now)
        events[eid] = item
        if not same:
            changed.append((eid, item))
    atomic_json(data / 'source-observations.json', {'version': 1, 'checked_at': checked, 'events': events})
    for eid, item in changed:
        log(data, now, 'source_state', event_id=eid, source_checked_at=checked, **item)
    for eid in set(previous['events']) - set(events):
        log(data, now, 'source_removed', event_id=eid, source_checked_at=checked)
    return dict(source_observation='recorded', source_state_changes=len(changed),
                source_announced=sum(v['classification'] == 'source_announced' for v in events.values()),
                source_confirmed=sum(v['classification'] == 'source_confirmed' for v in events.values()),
                source_estimated_unconfirmed=sum(v['classification'] == 'estimated_completion_unconfirmed' for v in events.values()))


def local_failure_kind(exc):
    # Exceptions may contain private paths or provider payloads: log no messages.
    if isinstance(exc, OSError):
        return 'local_io_error'
    if isinstance(exc, (ImportError, AttributeError)):
        return 'package_contract_error'
    code = exc.args[0] if len(exc.args) == 1 else None
    if isinstance(code, str) and code in ('out_of_order_event_version', 'out_of_order', 'same_timestamp_conflict',
            'snapshot_time_or_health', 'private_data_directory', 'private_ledger_file', 'private_lock_directory',
            'private_lock_file', 'ledger_shape', 'ledger_watermarks', 'observation_out_of_order'):
        return code
    return 'validation_or_contract_error'


def notification_sync(data, payload, now):
    """Caller holds monitor.lock. Capture public notice intents; never send."""
    ledger = notification_load(data)
    if ledger is None or not ledger['enabled']:
        return {'notification_result': 'disabled'}
    checked, normalized, _ = envelope(payload, now)
    if ledger['checked_at'] is not None and checked < ledger['checked_at']:
        raise Invalid('notification_out_of_order_snapshot')
    views = notification_view(payload, checked)
    for eid, event in normalized.items():
        if event['reason'] == 'conflicting_forecasts':
            views[eid].update(eligible=False, reason='conflicting_forecasts')
    if ledger['checked_at'] == checked and ledger['events'] != views:
        # The official presentation can advance with wall time while its scan
        # watermark and raw announcement stay fixed. Only a one-way stop is safe.
        raw_by_id = {raw['id']: raw for raw in payload['events']}
        stop_only = set(ledger['events']) == set(views)
        for eid, view in views.items():
            old, raw = ledger['events'].get(eid, {}), raw_by_id[eid]
            if old == view:
                continue
            stopped_reasons = ('estimated_completion_unconfirmed', 'expired_unconfirmed', 'in_progress_unconfirmed', 'expired')
            if (old.get('eligible') is False and view['eligible'] is False
                    and old.get('stage') == view['stage'] == raw.get('status') == 'announced'
                    and old.get('updated_at') == view['updated_at']
                    and old.get('reason') in stopped_reasons and view['reason'] in stopped_reasons
                    and all(raw.get(n) is None for n in ('confirmedAt', 'confirmationBasis', 'occurredOn'))):
                # A later estimate label still cannot restore a pending notice.
                continue
            schedule = raw.get('schedule')
            expiry = min(stamp(raw['createdAt']) + 7 * 86400, stamp(schedule['through'])) if isinstance(schedule, dict) and schedule.get('through') is not None else stamp(raw['createdAt']) + 7 * 86400
            source = old.get('text', '').splitlines()[-1:]
            matching = {a.get('url') for a in payload['activities'] if eid in a.get('eventIds', [])
                        and a.get('action') == 'announce' and stamp(a['publishedAt']) == old.get('source_at')}
            presentation = raw.get('presentation')
            display = presentation.get('status') if isinstance(presentation, dict) else None
            stop_only = stop_only and (old.get('eligible') is True and view['eligible'] is False
                and old.get('stage') == view['stage'] == raw.get('status') == 'announced'
                and view['updated_at'] == old['updated_at']
                and stamp(raw['createdAt']) == old.get('source_at') and expiry == old.get('expires_at')
                and source and matching == {source[0]}
                and display in ('in_progress', 'expired_unconfirmed', 'likely_completed'))
        if not stop_only:
            raise Invalid('notification_same_timestamp_conflict')
    for eid, view in views.items():
        previous = ledger['events'].get(eid)
        if previous and view['updated_at'] < previous['updated_at']:
            raise Invalid('notification_out_of_order_event')
    baseline = not ledger['baseline_complete']
    # Withdrawals, missing/expired/unknown events and confirmation stop unsent forecasts.
    for entry in ledger['entries'].values():
        view = views.get(entry['event_id'])
        if entry['state'] == 'pending' and (not view or not view['eligible'] or view['stage'] != entry['stage']
                or view['text'] != entry['text'] or view['source_at'] != entry['source_at'] or now >= view['expires_at']):
            entry.update(state='suppressed', reason='source_no_longer_actionable')
    added = 0
    for eid, view in views.items():
        if not view['eligible']:
            continue
        key = eid + '/' + view['stage']
        if key in ledger['entries']:
            continue
        explicit_accept = baseline and ledger['accept_event'] == eid and view['stage'] == 'announced'
        historical = baseline or view['source_at'] <= ledger['activation_at']
        state = 'pending' if explicit_accept or not historical else 'suppressed'
        ledger['entries'][key] = dict(event_id=eid, stage=view['stage'], state=state,
            notification_id='aihot-v1-' + hashlib.sha256(key.encode()).hexdigest()[:40],
            text=view['text'], source_at=view['source_at'], expires_at=view['expires_at'], created_at=now,
            send_id=None, bridge_status=None, delivered=None,
            reason='explicit_current_acceptance' if explicit_accept else ('startup_or_historical_baseline' if historical else 'new_source_status'))
        added += state == 'pending'
    if len(ledger['entries']) > 2 * MAX_SEEN or len(views) > MAX_SEEN:
        raise Invalid('notification_capacity')
    ledger.update(events=views, checked_at=checked, baseline_complete=True,
                  source_health='healthy', accept_event=None)
    atomic_json(data / 'notifications.json', ledger)
    return dict(notification_result='capture_only', new_notifications=added, notification_transport='transport_blocked')


def notification_prepare(data, event_id, stage, now=None):
    now = time.time() if now is None else now
    with lock(data) as acquired:
        if not acquired:
            return {'result': 'lock_busy'}
        ledger = notification_load(data)
        entry = ledger['entries'].get(event_id + '/' + stage) if ledger else None
        if entry is None:
            return {'result': 'not_found'}
        view = ledger['events'].get(event_id)
        source_ok = (ledger['enabled'] and ledger['source_health'] == 'healthy'
                     and ledger['checked_at'] is not None
                     and ledger['checked_at'] <= now + 120
                     and now - ledger['checked_at'] <= FRESHNESS
                     and now < entry['expires_at'] and view
                     and view['eligible'] and view['stage'] == stage
                     and view['text'] == entry['text'] and view['source_at'] == entry['source_at']
                     and now < view['expires_at'])
        if entry['state'] in ('attempting', 'uncertain', 'waiting'):
            result = dict(result='reconcile_required', query_only=entry['send_id'] is not None or not bool(source_ok),
                          recovery_allowed=entry['send_id'] is None and bool(source_ok),
                          notification_id=entry['notification_id'], send_id=entry['send_id'])
            if source_ok:
                result['text'] = entry['text']
            return result
        if entry['state'] != 'pending':
            return {'result': 'not_sendable', 'state': entry['state'], 'send_id': entry['send_id']}
        if now >= entry['expires_at']:
            entry.update(state='suppressed', reason='expired_before_submission')
            atomic_json(data / 'notifications.json', ledger)
            return {'result': 'not_sendable', 'state': 'suppressed', 'send_id': entry['send_id']}
        if not source_ok:
            return {'result': 'source_blocked'}
        entry.update(state='attempting', prepared_at=now)
        atomic_json(data / 'notifications.json', ledger)
        return dict(result='prepared', **{k: entry[k] for k in ('notification_id', 'text', 'send_id')})


def notification_record(data, event_id, stage, receipt, now=None):
    now = time.time() if now is None else now
    with lock(data) as acquired:
        if not acquired:
            return {'result': 'lock_busy'}
        ledger = notification_load(data)
        entry = ledger['entries'].get(event_id + '/' + stage) if ledger else None
        if entry is None or entry['state'] not in ('attempting', 'uncertain', 'waiting', 'accepted'):
            return {'result': 'not_recordable'}
        if not isinstance(receipt, dict):
            raise Invalid('notification_receipt')
        sid, status = receipt.get('send_id'), receipt.get('status')
        if not isinstance(sid, str) or not re.fullmatch(r'notice_[a-f0-9]{32}', sid):
            raise Invalid('notification_send_id')
        if entry['send_id'] is not None and entry['send_id'] != sid:
            raise Invalid('notification_send_id_conflict')
        if 'outgoing_text' in receipt and receipt['outgoing_text'] != entry['text']:
            raise Invalid('notification_text_conflict')
        if status == 'accepted' and receipt.get('accepted') is True and receipt.get('outgoing_text') == entry['text'] and 'delivered' in receipt and receipt['delivered'] is None and receipt.get('delivery_receipt_available') is False:
            state = 'accepted'
        elif status in ('queued', 'preparing', 'sending', 'context_required', 'auth_required'):
            state = 'waiting'
        elif status == 'rejected':
            state = 'rejected'
        else:
            state = 'uncertain'
        if entry['state'] == 'accepted' and state != 'accepted':
            return {'result': 'already_accepted', 'send_id': entry['send_id'], 'delivered': None}
        entry.update(state=state, send_id=sid, bridge_status=status if status in BRIDGE_STATES else 'unknown',
                     receipt_at=now, delivered=None)
        atomic_json(data / 'notifications.json', ledger)
        return {'result': 'recorded', 'state': state, 'send_id': sid, 'accepted': state == 'accepted', 'delivered': None}


def dual_control(data):
    path = data / 'dual-control.json'
    value = read_private_json(path)
    if value is _MISSING:
        value = {'capture_enabled': False}
    if not isinstance(value, dict) or set(value) != {'capture_enabled'} or type(value['capture_enabled']) is not bool:
        raise Invalid('dual_control')
    return value


def dual_package():
    # A fixed repository package; announcement data never controls imports.
    # Run the CLI as ``python -m integrations.aihot.monitor`` from this project.
    return importlib.import_module('integrations.aihot.notice_relay')


def dual_capture(data, payload, now):
    if not dual_control(data)['capture_enabled']:
        return {'dual_capture': 'disabled', 'sender_installed': False, 'automatic_delivery_ready': False}
    package = dual_package()
    relay = package.Relay(data, now_callable=lambda: now)
    validators = SimpleNamespace(envelope=envelope, notification_view=notification_view, stamp=stamp)
    result = package.capture_verified(relay, validators, payload, now)
    state = relay.status()
    counts = {name: {kind: sum(item['channels'][name]['state'] == kind for item in state['entries'].values())
                     for kind in package.relay.STATES} for name in ('wechat', 'email')}
    return dict(dual_capture=result['result'], dual_transport='platform_handoff_unavailable', sender_installed=False,
                automatic_delivery_ready=False, dual_channel_states=counts,
                dual_pending=sum(c['state'] == 'pending' for item in state['entries'].values() for c in item['channels'].values()))


def dual_suspend(data):
    if (data / 'dual-notifications.json').exists():
        try:
            return dual_package().Relay(data).suspend()
        except Exception:
            # Preserve corrupt/missing-package evidence; never reset its ledger.
            return {'result': 'dual_local_state_error'}
    return {'result': 'disabled'}


def dual_status(data):
    result = dict(**dual_control(data), transport='platform_handoff_unavailable', sender_installed=False,
                  automatic_delivery_ready=False,
                  routes={'wechat': {'state': 'adapter_not_installed'},
                          'email': {'state': 'handoff_contract_missing'}})
    if (data / 'dual-notifications.json').exists():
        state = dual_package().Relay(data).status()
        result.update(baseline_complete=state['baseline_complete'], healthy=state['healthy'], checked_at=state['checked_at'],
                      channels={name: {kind: sum(item['channels'][name]['state'] == kind for item in state['entries'].values())
                                       for kind in ('suppressed', 'pending', 'attempting', 'waiting', 'uncertain', 'accepted', 'rejected')}
                                for name in ('wechat', 'email')})
    else:
        result['baseline_complete'] = False
    result['source_observation'] = source_observation_status(data)
    return result


def dual_toggle(data, enabled, now=None):
    if type(enabled) is not bool:
        raise Invalid('dual_control')
    now = time.time() if now is None else now
    with lock(data) as acquired:
        if not acquired:
            return {'result': 'lock_busy'}
        previous = dual_control(data)['capture_enabled']
        if enabled and not previous:
            reset = dual_package().Relay(data).rebaseline(now)
            if reset['result'] == 'lock_busy':
                return reset
        atomic_json(data / 'dual-control.json', {'capture_enabled': enabled})
        if (data / 'state.json').exists():
            state = load_state(data, now)
            if enabled:
                state['etag'] = None  # Next scheduled poll obtains a body for baseline.
            atomic_json(data / 'state.json', state)
        suspended = dual_suspend(data) if not enabled else {'result': 'not_requested'}
        return {'result': 'capture_enabled' if enabled else 'capture_disabled',
                'source_suspension': suspended['result'], 'sender_installed': False}


def poll(data, now=None, transport=fetch, origin='manual'):
    now = time.time() if now is None else now
    with lock(data) as acquired:
        if not acquired:
            return dict(result='lock_busy', dispatch_enabled=False)
        try:
            state = load_state(data, now)
            control_path = data / 'control.json'
            control = read_private_json(control_path)
            if control is _MISSING:
                control = {'enabled': False, 'mode': 'monitor-only'}
            if not isinstance(control, dict) or set(control) != {'enabled', 'mode'} or type(control['enabled']) is not bool or control['mode'] != 'monitor-only':
                raise Invalid('control_invalid')
            expire(state, now)
            state['last_run_at'] = now
            state['poll_count'] += 1
            if not control['enabled']:
                stop_all(state, 'paused', now)
                result = dict(result='paused')
            elif state['route_stopped']:
                stop_all(state, 'route_stopped', now)
                result = dict(result='route_stopped')
            elif now < state['next_poll_at']:
                result = dict(result='backoff', next_poll_at=state['next_poll_at'])
            else:
                state['network_count'] += 1
                try:
                    # Also recover state persisted by older producers, which
                    # retained an ETag after failing validation or transport.
                    request_etag = state['etag'] if state['failures'] == 0 else None
                    code, headers, payload = transport(request_etag)
                    if code == 304:
                        if not state['baseline_complete'] or request_etag is None:
                            raise Invalid('304_without_baseline')
                        result = dict(result='not_modified', new_candidates=0)
                    elif code == 200:
                        result = apply_snapshot(state, payload, now)
                        try:
                            result.update(observe_source(data, payload, now))
                        except Exception as exc:
                            result.update(source_observation='blocked', observation_error=local_failure_kind(exc))
                        try:
                            result.update(notification_sync(data, payload, now))
                        except (Invalid, ValueError, TypeError, KeyError, OSError):
                            result['notification_result'] = 'local_or_source_blocked'
                            notification_suspend(data, 'notification_validation_failed')
                        try:
                            result.update(dual_capture(data, payload, now))
                        except Exception as exc:
                            result['dual_capture'] = 'blocked'
                            result['dual_error'] = local_failure_kind(exc)
                            dual_suspend(data)
                        etag = next((v for k, v in headers.items() if k.lower() == 'etag'), None)
                        # HTTP success alone does not prove every local capture
                        # committed. Include non-exception outcomes like lock_busy.
                        capture_complete = (result.get('source_observation') == 'recorded'
                            and result.get('notification_result') in ('capture_only', 'disabled')
                            and result.get('dual_capture') in ('captured', 'disabled'))
                        state['etag'] = etag if capture_complete and isinstance(etag, str) and re.fullmatch(r'[\x20-\x7e]{1,512}', etag) else None
                    else:
                        result = failure(state, now, 'http_' + str(int(code)), headers)
                    if code in (200, 304):
                        state.update(failures=0, next_poll_at=now + INTERVAL, last_success_at=now)
                except Invalid as exc:
                    result = failure(state, now, str(exc))
                except (urllib.error.URLError, socket.timeout, TimeoutError, OSError):
                    result = failure(state, now, 'network_failure')
                except (ValueError, TypeError, KeyError, RecursionError):
                    result = failure(state, now, 'malformed_response')
            if result['result'] not in ('snapshot', 'baseline', 'not_modified', 'backoff'):
                notification_suspend(data, 'source_' + result['result'])
                dual_suspend(data)
            atomic_json(data / 'state.json', state)
            log(data, now, 'poll', origin=origin, poll_count=state['poll_count'],
                network_count=state['network_count'], **result)
            return dict(**result, mode='monitor-only', dispatch_enabled=False)
        except (Invalid, ValueError, TypeError, KeyError, OverflowError, RecursionError, OSError):
            log(data, now, 'local_state_error', origin=origin)
            return dict(result='local_state_error', mode='monitor-only', dispatch_enabled=False)


def toggle(data, enabled, now=None):
    if type(enabled) is not bool:
        raise Invalid('control_invalid')
    now = time.time() if now is None else now
    with lock(data) as acquired:
        if not acquired:
            return {'result': 'lock_busy'}
        if enabled and dual_control(data)['capture_enabled']:
            reset = dual_package().Relay(data).rebaseline(now)
            if reset['result'] == 'lock_busy':
                return reset
        atomic_json(data / 'control.json', {'enabled': enabled, 'mode': 'monitor-only'})
        notification_suspend(data, 'monitor_reenabled_requires_fresh_snapshot' if enabled else 'monitor_paused')
        dual_suspension = dual_suspend(data)
        if (data / 'state.json').exists():
            state = load_state(data, now)
            stop_all(state, 'paused' if not enabled else 'operator_reenabled', now)
            if enabled:
                # A resumed monitor must not catch up announcements from its paused interval.
                state.update(activation_at=now, baseline_complete=False, etag=None)
            atomic_json(data / 'state.json', state)
        return {'enabled': enabled, 'mode': 'monitor-only', 'dispatch_enabled': False,
                'dual_source_suspension': dual_suspension['result']}


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('poll', 'status', 'on', 'off',
        'notifications-on', 'notifications-off', 'notifications-status',
        'notification-prepare', 'notification-record', 'dual-capture-on', 'dual-capture-off', 'dual-status'))
    parser.add_argument('--data-dir', type=Path, required=True,
                        help='Explicit private state directory; missing controls default to disabled')
    parser.add_argument('--origin', choices=('manual', 'launchd'), default='manual')
    parser.add_argument('--event-id')
    parser.add_argument('--stage', choices=('announced', 'confirmed'))
    parser.add_argument('--receipt-file', type=Path)
    args = parser.parse_args()
    if args.command == 'poll':
        result = poll(args.data_dir, origin=args.origin)
    elif args.command in ('on', 'off'):
        result = toggle(args.data_dir, args.command == 'on')
    elif args.command == 'notifications-on':
        result = notification_enable(args.data_dir, accept_event=args.event_id)
    elif args.command == 'notifications-off':
        result = notification_disable(args.data_dir)
    elif args.command == 'notifications-status':
        result = notification_status(args.data_dir)
    elif args.command in ('dual-capture-on', 'dual-capture-off'):
        result = dual_toggle(args.data_dir, args.command == 'dual-capture-on')
    elif args.command == 'dual-status':
        result = dual_status(args.data_dir)
    elif args.command in ('notification-prepare', 'notification-record'):
        if not args.event_id or not args.stage:
            parser.error('--event-id and --stage required')
        event_id(args.event_id)
        if args.command == 'notification-prepare':
            result = notification_prepare(args.data_dir, args.event_id, args.stage)
        else:
            if args.receipt_file is None:
                parser.error('bounded private --receipt-file required')
            receipt = read_private_json(args.receipt_file, limit=16384)
            if receipt is _MISSING:
                parser.error('bounded private --receipt-file required')
            result = notification_record(args.data_dir, args.event_id, args.stage, receipt)
    else:
        state = load_state(args.data_dir, time.time())
        result = {k: state[k] for k in ('mode', 'dispatch_enabled', 'baseline_complete', 'poll_count', 'network_count', 'last_run_at', 'last_success_at', 'next_poll_at', 'failures')}
        result.update(seen_events=len(state['seen']), candidates=len(state['candidates']))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
