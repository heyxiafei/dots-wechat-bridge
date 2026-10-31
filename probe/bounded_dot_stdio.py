"""Finite, original-subscription DOT acceptance using the existing Probe.

No background workers, history import, automatic acknowledgement, notification,
or local echo. The operator's private approval record documents externally
verified authorization; it is not a permission grant or cross-host fence.
"""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import select
import signal
import stat
import sys
import time

from .probe import Probe, RpcError, acquire_worker_lock, canonical, headers, MAX_RPC_BYTES
from .new_message_check import (CheckError, ROUND, binding, candidate, checked_database,
                                classify_send, open_database, phrase, unused_state)
from .weixin import IlinkClient, WeixinError

TABLE = 'weixin_bounded_dot_check'
PHASES = {'waiting_subscription', 'subscription_unknown', 'waiting_source', 'poll_stopped',
          'ambiguous_source', 'unmatched_or_ambiguous_stopped', 'admitting_source',
          'event_unknown', 'awaiting_dot_final', 'sending', 'accepted', 'rejected', 'delivery_unknown'}
TOOLS = {'get_weixin_status', 'read_weixin_message', 'reply_weixin_message', 'get_weixin_send_status'}
REFERENCES = ('approval_reference', 'platform_request_evidence_reference',
              'accepted_transport_evidence_reference', 'private_tunnel_access_reference')


def plan():
    return {'mode': 'plan', 'live_started': False, 'authorization_granted': False,
            'required': ['operator_verified_private_approval_record', 'existing_ready_binding',
                         'accepted_authenticated_original_tunnel',
                         'original_platform_subscription_request_and_exact_callback'],
            'max_duration_seconds': 180, 'source_limit': 1, 'send_post_limit': 1,
            'cross_host_single_writer_verified': False, 'phone_received': None}


def validate_approval(value, now):
    if not isinstance(value, dict):
        raise CheckError('private_approval_record_required')
    if (not isinstance(value.get('round_id'), str) or not ROUND.fullmatch(value['round_id'])
            or type(value.get('duration_seconds')) is not int or not 1 <= value['duration_seconds'] <= 180
            or type(value.get('valid_until')) not in (float, int)
            or not now < value['valid_until'] <= now + 300):
        raise CheckError('invalid_or_expired_round')
    for key in REFERENCES:
        if not isinstance(value.get(key), str) or not 1 <= len(value[key]) <= 512 or '\n' in value[key]:
            raise CheckError('external_authorization_evidence_required')
    for key in ('binding_sha256', 'subscription_request_digest'):
        if not isinstance(value.get(key), str) or not re.fullmatch('[a-f0-9]{64}', value[key]):
            raise CheckError('verified_binding_and_original_request_required')
    if not isinstance(value.get('subscription_id'), str) or not re.fullmatch('sub_[a-f0-9]{32}', value['subscription_id']):
        raise CheckError('original_subscription_id_required')
    host = value.get('callback_host')
    if not isinstance(host, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,251}[a-z0-9]', host) or '.' not in host or '..' in host:
        raise CheckError('exact_callback_host_required')
    return dict(value)


def load_approval(path, now):
    path = Path(path)
    if not path.is_absolute() or any(part.is_symlink() for part in (path, *path.parents)):
        raise CheckError('private_approval_path_required')
    mode = path.stat()
    if not stat.S_ISREG(mode.st_mode) or mode.st_uid != os.getuid() or mode.st_mode & 0o077 or mode.st_size > 16384:
        raise CheckError('private_approval_file_required')
    return validate_approval(json.loads(path.read_text()), now)


def read_receipt(directory, round_id):
    if not isinstance(round_id, str) or not ROUND.fullmatch(round_id):
        raise CheckError('original_round_id_required')
    with closing(open_readonly(directory)) as db:
        row = db.execute('SELECT * FROM ' + TABLE + ' WHERE singleton=1').fetchone()
        if not row or row['round_id'] != round_id:
            raise CheckError('original_round_id_required')
        phase = row['phase'] if row['phase'] in PHASES else 'delivery_unknown'
        source_id = row['source_id'] if isinstance(row['source_id'], str) and re.fullmatch('wx_[a-f0-9]{32}', row['source_id']) else None
        event_id = row['event_id'] if isinstance(row['event_id'], str) and re.fullmatch('evt_[a-f0-9]{32}', row['event_id']) else None
        digest_valid = isinstance(row['reply_digest'], str) and re.fullmatch('[a-f0-9]{64}', row['reply_digest']) is not None
        if row['reply_digest'] is not None and (not digest_valid or source_id is None):
            phase = 'delivery_unknown'
        send_id = source_id if digest_valid else None
        if send_id:
            outgoing = db.execute('SELECT status,attempts,source_message_id,client_id,kind,text FROM weixin_outbox WHERE message_id=?', (send_id,)).fetchone()
            if (not outgoing or outgoing['attempts'] != 1 or outgoing['source_message_id'] != send_id
                    or outgoing['client_id'] != 'dots_' + send_id[3:] or outgoing['status'] != phase
                    or outgoing['kind'] != 'reply' or not isinstance(outgoing['text'], str)
                    or hashlib.sha256(outgoing['text'].encode('utf-8', errors='surrogatepass')).hexdigest() != row['reply_digest']):
                phase = 'delivery_unknown'
            elif phase == 'sending':
                phase = 'delivery_unknown'
        elif phase not in {'delivery_unknown', 'subscription_unknown', 'event_unknown', 'poll_stopped',
                           'ambiguous_source', 'unmatched_or_ambiguous_stopped'}:
            phase = 'stopped_no_restart'
        return {'round_id': round_id, 'phase': phase, 'message_id': source_id,
                'send_id': send_id, 'event_id': event_id, 'retry_allowed': False,
                'receipts_only': True, 'phone_received': None}


def open_readonly(directory):
    path = checked_database(directory)
    if Path(str(path) + '-wal').exists() and not Path(str(path) + '-shm').exists():
        raise CheckError('readonly_wal_without_shm_refused')
    return open_database(path, readonly=True)


class DeadlineExpired(BaseException):
    pass


class TimedClient(IlinkClient):
    def request(self, base, endpoint, payload=None, token=None, timeout=20):
        # Short polls keep stdio responsive; the process alarm is the hard cap.
        if endpoint == '/ilink/bot/getupdates':
            timeout = min(timeout, 2)
        return super().request(base, endpoint, payload, token, timeout=timeout)


class BoundedDot:
    def __init__(self, probe, approval, clock=time.time, monotonic=time.monotonic):
        self.probe, self.db = probe, probe.db
        self.clock, self.monotonic = clock, monotonic
        self.approval = validate_approval(approval, clock())
        self.round_id = approval['round_id']
        self.started, self.mono_started = clock(), monotonic()
        self.duration = approval['duration_seconds']
        self.deadline = min(self.started + self.duration, approval['valid_until'])
        self.read_id = None
        self.context = None
        self.next_poll = self.mono_started
        # Check an unused installation before adding our table. A restarted
        # invocation is handled by run(), without constructing a new Probe.
        unused_state(self.db)
        self.account, fingerprint = binding(self.db, credentials=True)
        if fingerprint != approval['binding_sha256']:
            raise CheckError('binding_changed')
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS weixin_bounded_dot_check (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), round_id TEXT NOT NULL,
                phase TEXT NOT NULL, source_id TEXT, event_id TEXT, reply_digest TEXT)''')
            self.db.execute('INSERT INTO ' + TABLE + '(singleton,round_id,phase) VALUES(1,?,?)',
                            (self.round_id, 'waiting_subscription'))

    def active(self):
        if self.clock() >= self.deadline or self.monotonic() - self.mono_started >= self.duration:
            raise CheckError('round_expired')
        _, fingerprint = binding(self.db)
        if fingerprint != self.approval['binding_sha256']:
            raise CheckError('binding_changed')

    def row(self):
        return self.db.execute('SELECT * FROM ' + TABLE + ' WHERE singleton=1').fetchone()

    def transition(self, expected, phase, **fields):
        if not set(fields) <= {'source_id', 'event_id', 'reply_digest'}:
            raise CheckError('internal_transition_refused')
        with self.db:
            changed = self.db.execute('UPDATE ' + TABLE + ' SET phase=?' + ''.join(',' + k + '=?' for k in fields)
                                     + ' WHERE singleton=1 AND phase=?',
                                     (phase, *fields.values(), expected)).rowcount
            if changed != 1:
                raise CheckError('original_action_already_claimed')

    def receipt(self):
        row = self.row()
        return {'round_id': row['round_id'], 'phase': 'delivery_unknown' if row['phase'] == 'sending' else row['phase'],
                'message_id': row['source_id'], 'send_id': row['source_id'] if row['reply_digest'] else None,
                'event_id': row['event_id'], 'retry_allowed': False, 'phone_received': None}

    def subscribe(self, params):
        self.active()
        if (params.get('name') != 'weixin.message' or params.get('arguments', {}) != {}
                or hashlib.sha256(canonical(params).encode()).hexdigest() != self.approval['subscription_request_digest']
                or self.probe.subscription_id(params) != self.approval['subscription_id']):
            raise CheckError('original_subscription_request_required')
        self.transition('waiting_subscription', 'subscription_unknown')
        result = self.probe.subscribe(params)
        self.transition('subscription_unknown', 'waiting_source')
        return result

    def tick(self):
        self.active()
        if self.row()['phase'] != 'waiting_source':
            return
        if self.monotonic() < self.next_poll:
            return
        self.next_poll = self.monotonic() + .5
        try:
            result = self.probe.weixin.client.poll(self.account)
        except WeixinError as error:
            if error.kind == 'api_timeout':
                # Read-only long-poll timeout is an empty wait, never a resend.
                return
            self.transition('waiting_source', 'poll_stopped')
            return
        except Exception:
            self.transition('waiting_source', 'poll_stopped')
            return
        self.active()
        if (not isinstance(result, dict) or any(v is not None and (type(v) is not int or v != 0)
                for v in (result.get('ret'), result.get('errcode')))
                or not isinstance(result.get('msgs', []), list) or len(result.get('msgs', [])) > 1000):
            self.transition('waiting_source', 'poll_stopped')
            return
        matches = {}
        for raw in result.get('msgs', []):
            found = candidate(self.account, raw, phrase(self.round_id), self.started, self.deadline, self.clock())
            if found:
                if found['local_id'] in matches and matches[found['local_id']][0] != found:
                    self.transition('waiting_source', 'ambiguous_source')
                    return
                matches[found['local_id']] = (found, raw)
        if len(matches) != 1:
            if matches or result.get('msgs'):
                self.transition('waiting_source', 'unmatched_or_ambiguous_stopped')
            return
        found, raw = next(iter(matches.values()))
        self.transition('waiting_source', 'admitting_source', source_id=found['local_id'])
        # Only this admitted raw message reaches the original ingestion path;
        # deliberately omit get_updates_buf, so the official cursor is unchanged.
        self.probe.weixin.ingest(self.account, {'ret': 0, 'msgs': [raw]})
        self.context = found['context']
        if self.probe.queue_weixin_messages() != 1:
            raise CheckError('single_source_event_required')
        event = self.db.execute('SELECT * FROM pings WHERE message_id=?', (found['local_id'],)).fetchone()
        if not event or event['sub_id'] != self.approval['subscription_id']:
            raise CheckError('original_event_route_required')
        self.transition('admitting_source', 'event_unknown', event_id=event['id'])
        with self.db:
            self.db.execute("UPDATE pings SET attempts=1,last_attempt_started_at=? WHERE id=? AND attempts=0", (self.clock(), event['id']))
            self.probe.delivery_timing.attempt(found['local_id'], self.clock())
        self.active()
        sub = self.db.execute('SELECT * FROM subscriptions WHERE id=?', (event['sub_id'],)).fetchone()
        if not sub or sub['expires'] is not None and sub['expires'] <= self.clock():
            raise CheckError('original_subscription_expired')
        body = event['body'].encode()
        try:
            status, _ = self.probe.post(sub['url'], body, headers(dict(sub), event['id'], body, self.clock()))
            accepted = type(status) is int and 200 <= status < 300
        except Exception:
            accepted = False
        with self.db:
            self.db.execute("UPDATE pings SET status=?,last_attempt_finished_at=?,delivered_at=? WHERE id=?",
                            ('delivered' if accepted else 'failed', self.clock(), self.clock() if accepted else None, event['id']))
            if accepted:
                self.probe.delivery_timing.accepted(found['local_id'], self.clock())
        if accepted:
            self.transition('event_unknown', 'awaiting_dot_final')

    def reply(self, message_id, text):
        self.active()
        row = self.row()
        if not message_id or message_id != row['source_id'] or self.read_id != message_id:
            raise CheckError('read_exact_source_first')
        if row['reply_digest']:
            result = self.probe.weixin.send_status(message_id)
            try:
                conflict = not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != row['reply_digest']
            except UnicodeError:
                conflict = True
            return {**result, 'content_conflict': conflict, 'requested_text_used': False}
        if row['phase'] != 'awaiting_dot_final' or self.context is None:
            raise CheckError('original_event_not_accepted')
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > 8192:
            raise CheckError('invalid_reply_text')
        self.active()
        # Existing final ID/client ID, durable sending + attempts=1 before POST.
        with self.db:
            changed = self.db.execute('UPDATE ' + TABLE + " SET phase='sending',reply_digest=? WHERE singleton=1 AND phase='awaiting_dot_final' AND reply_digest IS NULL",
                                     (hashlib.sha256(text.encode()).hexdigest(),)).rowcount
            if changed != 1:
                raise CheckError('original_action_already_claimed')
            self.db.execute('''INSERT INTO weixin_outbox
                (message_id,account,peer,text,client_id,status,attempts,created_at,kind,source_message_id,post_started_at)
                VALUES(?,?,?,?,?,'sending',1,?,'reply',?,?)''',
                (message_id, self.account['id'], self.account['owner'], text, 'dots_' + message_id[3:],
                 self.clock(), message_id, self.clock()))
        try:
            self.active()
            result = self.probe.weixin.client.send(self.account, self.account['owner'], text,
                                                  'dots_' + message_id[3:], self.context)
            phase, error, code = classify_send(result)
        except Exception:
            phase, error, code = 'delivery_unknown', 'send_outcome_unknown', None
        with self.db:
            self.db.execute("UPDATE weixin_outbox SET status=?,error_kind=?,server_code=?,accepted_at=? WHERE message_id=? AND status='sending' AND attempts=1",
                            (phase, None if error == 'none' else error, code,
                             self.clock() if phase == 'accepted' else None, message_id))
            self.db.execute('UPDATE ' + TABLE + ' SET phase=? WHERE singleton=1 AND phase=\'sending\'', (phase,))
        return self.probe.weixin.send_status(message_id)

    def rpc(self, request):
        request_id = request.get('id') if isinstance(request, dict) else None
        valid_id = (type(request_id) is int and -(2**63) <= request_id < 2**63
                    or isinstance(request_id, str) and 0 < len(request_id) <= 128)
        if not valid_id:
            request_id = None
        try:
            self.active()
            if not isinstance(request, dict) or request.get('jsonrpc') != '2.0':
                raise CheckError('invalid_rpc')
            method, params = request.get('method'), request.get('params', {})
            if not isinstance(params, dict):
                raise CheckError('invalid_rpc_params')
            if method != 'notifications/initialized' and not valid_id:
                raise CheckError('request_id_required')
            if method in {'initialize', 'server/discover', 'ping', 'notifications/initialized'}:
                return self.probe.rpc(request)
            if method == 'tools/list':
                allowed = [t for t in self.probe.tools() if t['name'] in TOOLS]
                for tool in allowed:
                    if tool['name'] == 'read_weixin_message':
                        tool['inputSchema'] = {'type': 'object', 'properties': {'message_id': {'type': 'string'}},
                                               'required': ['message_id'], 'additionalProperties': False}
                result = {'tools': allowed}
            elif method == 'events/list':
                result = {'events': [self.probe.event('weixin.message')]}
            elif method == 'events/subscribe':
                result = self.subscribe(params)
            elif method == 'tools/call':
                name, args = params.get('name'), params.get('arguments', {})
                if not isinstance(args, dict) or name not in TOOLS:
                    raise CheckError('tool_not_allowed')
                row = self.row()
                if name == 'get_weixin_status' and args == {}:
                    return self.probe.rpc(request)
                if name == 'read_weixin_message' and set(args) == {'message_id'} and args['message_id'] == row['source_id'] and row['source_id']:
                    result = self.probe.rpc(request)
                    if 'error' not in result:
                        self.read_id = row['source_id']
                    return result
                if name == 'reply_weixin_message' and set(args) == {'message_id', 'text'}:
                    value = self.reply(args['message_id'], args['text'])
                    result = {'content': [{'type': 'text', 'text': canonical(value)}], 'structuredContent': value}
                elif name == 'get_weixin_send_status' and set(args) == {'send_id'} and row['reply_digest'] and args['send_id'] == row['source_id']:
                    return self.probe.rpc(request)
                else:
                    raise CheckError('only_original_source_and_final_allowed')
            else:
                raise CheckError('method_not_allowed')
            return {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        except (CheckError, RpcError, ValueError, TypeError, UnicodeError):
            # No platform error body, callback URL, token or raw argument echo.
            return {'jsonrpc': '2.0', 'id': request_id,
                    'error': {'code': -32003, 'message': 'Bounded DOT operation refused; preserve original IDs'}}


def run(directory, approval_path, stdin_fd=0):
    approval = load_approval(approval_path, time.time())
    checked_database(directory)
    lock = acquire_worker_lock(directory)
    probe = None
    try:
        with closing(open_readonly(directory)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
                # No new worker or source after any previously recorded round.
                raise CheckError('recorded_round_requires_original_receipt_read')
            unused_state(db)
        probe = Probe(directory, allowed_hosts=[approval['callback_host']], weixin_client=TimedClient())
        session = BoundedDot(probe, approval)
        if not hasattr(signal, 'setitimer'):
            raise CheckError('bounded_posix_runtime_required')
        def expired(*unused):
            raise DeadlineExpired()
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(.001, session.deadline - time.time()))
        buffer = b''
        try:
            while time.time() < session.deadline:
                readable, _, _ = select.select([stdin_fd], [], [], .1)
                if readable:
                    chunk = os.read(stdin_fd, 4096)
                    if not chunk:
                        break
                    buffer += chunk
                    if len(buffer) > MAX_RPC_BYTES:
                        raise CheckError('bounded_rpc_size_exceeded')
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        try:
                            request = json.loads(line)
                        except (ValueError, UnicodeError):
                            raise CheckError('invalid_rpc_json') from None
                        response = session.rpc(request)
                        if response is not None:
                            print(canonical(response), flush=True)
                session.tick()
        except DeadlineExpired:
            pass
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    finally:
        if probe is not None:
            probe.close()
        lock.close()
