"""Original-subscription MCP connection, without any Weixin business runtime.

Full protocol catalogs are reused, but operations are separately allowlisted.
Only a real same-route subscription request may issue callback verification and
update that subscription. No credentials are read from Weixin account rows.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import signal
import sqlite3
import stat
import sys
import threading
import time

if __package__:
    from .probe import (Probe, RpcError, VERSION, BUILD_VERSION, EMAIL_EVENT, canonical,
                        acquire_worker_lock, post_https)
    from .connection_probe import BoundedOutput, DeadlineExpired, valid_id
else:
    from probe import (Probe, RpcError, VERSION, BUILD_VERSION, EMAIL_EVENT, canonical,
                       acquire_worker_lock, post_https)
    from connection_probe import BoundedOutput, DeadlineExpired, valid_id

MODE = 'original_subscription_connection_only'
POLICY_TABLES = ('aihot_auto_resume', 'owner_receipt_activations', 'owner_receipt_baseline',
    'owner_receipt_intents', 'weixin_dot_mirror_epochs', 'weixin_dot_mirror_epoch_members',
    'weixin_dot_cutover', 'weixin_dot_cutover_sources', 'weixin_dot_cutover_closures',
    'weixin_dot_single_trial', 'weixin_dot_trial_future_sources',
    'weixin_dot_steady_generations', 'weixin_dot_steady_sources', 'weixin_dot_steady_pauses',
    'weixin_dot_journal', 'weixin_dot_mirrors')
EMPTY_TABLES = POLICY_TABLES + ('weixin_inbox', 'weixin_outbox', 'pings', 'weixin_peers',
                              'weixin_attachments', 'weixin_uploads')
MARKER_TABLES = {'weixin_fresh_cutover', 'weixin_fresh_admissions', 'weixin_fresh_contexts',
                 'weixin_bounded_dot_check'}
EMPTY_STOPPED_TRIAL = 'weixin_new_message_check'
TRIAL_COLUMNS = (
    ('singleton','INTEGER',0,1), ('round_id','TEXT',1,0), ('phase','TEXT',1,0),
    ('started_at','REAL',1,0), ('expires_at','REAL',1,0), ('binding_hash','TEXT',1,0),
    ('approval_hash','TEXT',1,0), ('isolation_hash','TEXT',1,0),
    ('source_message_id','TEXT',0,0), ('send_id','TEXT',0,0),
    ('error_kind','TEXT',1,0), ('updated_at','REAL',1,0))
SUB_COLUMNS = {'id', 'url', 'secret', 'old_secret', 'rotate_until', 'expires',
               'verified_at', 'name', 'revision'}


def validate_scope(hosts, route_digest, subscription_id):
    if (not isinstance(hosts, (list, tuple)) or len(hosts) != 1
            or not isinstance(hosts[0], str)
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', hosts[0])
            or '.' not in hosts[0] or '..' in hosts[0]
            or not isinstance(route_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', route_digest)
            or subscription_id != 'sub_' + route_digest[:32]):
        raise ValueError('connection_only_scope_invalid')


class SubscriptionConnection(Probe):
    """Reuse only reviewed subscription methods; intentionally no super init."""
    def __init__(self, directory, allowed_hosts, route_digest, subscription_id,
                 *, post=None, clock=time.time):
        validate_scope(allowed_hosts, route_digest, subscription_id)
        self.directory = Path(directory)
        self.allowed_hosts = set(allowed_hosts)
        self.route_digest, self.original_subscription_id = route_digest, subscription_id
        self.clock, self.lock = clock, threading.RLock()
        self.fresh_admission = None  # No generation is created, restored or bypassed.
        self.instance_id = 'connection-only-' + secrets.token_hex(16)
        self.challenge_verified = False
        self.post = post or (lambda url, body, headers: post_https(url, body, headers, self.allowed_hosts))
        database = self.directory/'probe.sqlite'
        if any(path.is_symlink() for path in (database, *database.parents)):
            raise ValueError('connection_only_database_unsafe')
        info = database.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError('connection_only_database_unsafe')
        # rw forbids silently creating a replacement database. No schema/store
        # constructors, migrations, journal changes or credential reads occur.
        self.db = sqlite3.connect(database.resolve().as_uri() + '?mode=rw', uri=True)
        self.db.row_factory = sqlite3.Row
        try:
            self._validate_state()
            self.db.set_authorizer(self._authorize_sql)
        except BaseException:
            self.db.close()
            raise

    def _validate_state(self):
        schema = self.db.execute('SELECT type,name,tbl_name FROM sqlite_master').fetchall()
        tables = {r['name'] for r in schema if r['type'] == 'table'}
        if (tables & MARKER_TABLES or any(r['type'] == 'view' for r in schema)
                or any(r['type'] == 'trigger' and r['tbl_name'] == 'subscriptions' for r in schema)
                or not {'subscriptions','weixin_accounts','weixin_inbox','weixin_outbox','pings'} <= tables):
            raise ValueError('connection_only_state_not_supported')
        if EMPTY_STOPPED_TRIAL in tables:
            self._validate_empty_stopped_trial()
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(subscriptions)')}
        if columns != SUB_COLUMNS:
            raise ValueError('connection_only_subscription_schema_mismatch')
        # Existing business/policy state is not silently interpreted as an
        # unused replacement. Only counts are inspected; no message is read.
        for table in EMPTY_TABLES:
            if table in tables and self.db.execute('SELECT count(*) FROM '+table).fetchone()[0]:
                raise ValueError('connection_only_business_or_policy_state_present')
        if any((self.directory/name).exists() or (self.directory/name).is_symlink()
               for name in ('owner-receipts.json', 'aihot-operator-arm.json')):
            raise ValueError('connection_only_policy_file_present')
        if self.db.execute('SELECT count(*) FROM weixin_accounts').fetchone()[0] != 1:
            raise ValueError('connection_only_binding_required')
        for row in self.db.execute('SELECT id,url,name FROM subscriptions'):
            route = hashlib.sha256(canonical(['owner-only-tunnel', row['url'], row['name'], {}]).encode()).hexdigest()
            if (row['id'] != self.original_subscription_id or row['name'] != 'weixin.message'
                    or route != self.route_digest):
                raise ValueError('connection_only_original_subscription_required')

    def _validate_empty_stopped_trial(self):
        """Read-only exception for an expired trial that admitted/sent nothing.

        It is not a production promotion, authorization, cleanup or a fence.
        The original row, including all opaque evidence hashes, stays untouched.
        """
        columns = self.db.execute('PRAGMA table_xinfo(weixin_new_message_check)').fetchall()
        expected = [(index,name,kind,required,None,primary,0)
                    for index,(name,kind,required,primary) in enumerate(TRIAL_COLUMNS)]
        if [tuple(row) for row in columns] != expected:
            raise ValueError('connection_only_trial_schema_mismatch')
        # Deliberately do not select the old binding/approval/isolation hashes.
        rows = self.db.execute('''SELECT singleton,phase,started_at,expires_at,
            source_message_id,send_id,updated_at FROM weixin_new_message_check''').fetchall()
        now = self.clock()
        if len(rows) != 1 or type(now) not in (int,float) or not math.isfinite(now):
            raise ValueError('connection_only_trial_not_empty_stopped')
        row = rows[0]
        times = (row['started_at'],row['expires_at'],row['updated_at'])
        if (row['singleton'] != 1 or row['phase'] != 'stopped'
                or row['source_message_id'] is not None or row['send_id'] is not None
                or any(type(value) not in (int,float) or not math.isfinite(value) for value in times)
                or not 0 < times[0] < times[1] <= now
                or not 0 < times[1]-times[0] <= 180
                or not times[0] <= times[2] <= now):
            raise ValueError('connection_only_trial_not_empty_stopped')

    @staticmethod
    def _authorize_sql(action, table, column, database, source):
        # Defense in depth: even a trigger attached after initialization cannot
        # mutate business state or read an account credential during handshake.
        if source is not None or database not in (None, 'main'):
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_OK if column == 'count' else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_READ:
            if table == 'subscriptions' or table in EMPTY_TABLES and column == '':
                return sqlite3.SQLITE_OK
            if table == 'weixin_accounts' and column in ('', 'status'):
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            return sqlite3.SQLITE_OK if table == 'subscriptions' else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_DENY

    def notify_events(self):
        pass  # No event worker, wakeup, lease recovery or queue exists here.

    def subscribe(self, params):
        if (params.get('name') != 'weixin.message' or params.get('arguments', {}) != {}
                or self.subscription_id(params) != self.original_subscription_id):
            raise RpcError(-32602, 'Original subscription required')
        route = hashlib.sha256(canonical(['owner-only-tunnel', params['delivery']['url'],
                                         'weixin.message', {}]).encode()).hexdigest()
        if route != self.route_digest:
            raise RpcError(-32602, 'Original subscription required')
        result = super().subscribe(params, force_verification=True)
        self.challenge_verified = True
        return result

    def status(self):
        with self.lock:
            bound = self.db.execute('SELECT count(*) FROM weixin_accounts').fetchone()[0]
            rows = self.db.execute('SELECT expires,verified_at FROM subscriptions WHERE id=?',
                                   (self.original_subscription_id,)).fetchall()
        return {'mode': MODE, 'instance_id': self.instance_id,
                'mcp_request_received': True, 'bound': bound == 1, 'connected': False,
                'binding_scope': 'local_metadata_only', 'subscription_present': len(rows) == 1,
                'subscription_active': bool(rows and rows[0]['verified_at'] > 0
                                            and (rows[0]['expires'] is None or rows[0]['expires'] > self.clock())),
                'callback_challenge_verified_this_process': self.challenge_verified,
                'weixin_polling_enabled': False, 'message_sending_enabled': False,
                'media_processing_enabled': False, 'event_delivery_enabled': False,
                'cross_host_writer_isolation_verified': False, 'phone_delivery': 'not_tested'}

    def rpc(self, request):
        # Never use Probe.rpc: it dispatches reads with timing writes, queues,
        # mirror writes and activation even when no worker thread is running.
        request_id = request.get('id') if isinstance(request, dict) else None
        safe_id = request_id if valid_id(request_id) else None
        def error(code=-32000):
            return {'jsonrpc':'2.0','id':safe_id,'error':{'code':code,'message':'Connection-only operation unavailable'}}
        if (not isinstance(request, dict) or request.get('jsonrpc') != '2.0'
                or not isinstance(request.get('method'), str)
                or not isinstance(request.get('params', {}), dict)):
            return error(-32600)
        if 'id' not in request:
            return None  # No notification, including subscribe, can mutate state.
        if not valid_id(request_id):
            return error(-32600)
        method, params = request['method'], dict(request.get('params', {}))
        if '_meta' in params:
            if not isinstance(params.pop('_meta'), dict):
                return error(-32602)
        try:
            info = {'name':'dots-wechat-bridge','version':BUILD_VERSION}
            if method == 'server/discover':
                result = {'resultType':'complete','supportedVersions':[VERSION],
                          'serverInfo':info,'capabilities':{'tools':{},'events':{}}}
            elif method == 'initialize':
                result = {'protocolVersion':VERSION,'serverInfo':info,'capabilities':{'tools':{},'events':{}}}
            elif method == 'ping' and params == {}:
                result = {}
            elif method == 'tools/list' and params == {}:
                result = {'tools':Probe.tools()}
            elif method == 'events/list' and params == {}:
                result = {'events':[Probe.event(),Probe.event('weixin.message'),Probe.event(EMAIL_EVENT)]}
            elif method == 'events/subscribe':
                if not set(params) <= {'name','arguments','delivery','ttlMs','cursor'}:
                    return error(-32602)
                result = self.subscribe(params)
            elif (method == 'tools/call' and set(params) <= {'name','arguments'}
                    and params.get('name') in ('get_weixin_status','get_probe_status')
                    and params.get('arguments', {}) == {}):
                content = self.status()
                result = {'content':[{'type':'text','text':canonical(content)}], 'structuredContent':content}
            else:
                return error()
            return {'jsonrpc':'2.0','id':request_id,'result':result}
        except (RpcError, ValueError, TypeError, sqlite3.Error, KeyError):
            return error()


def serve(state_dir, allowed_hosts, route_digest, subscription_id, duration_seconds=180,
          *, stdin_fd=0, stdout=None, stderr=None):
    """Bounded foreground stdio only. Tunnel lifecycle remains caller-owned."""
    validate_scope(allowed_hosts, route_digest, subscription_id)
    if (type(duration_seconds) is not int or not 1 <= duration_seconds <= 180
            or os.name != 'posix' or signal.getitimer(signal.ITIMER_REAL) != (0., 0.)):
        raise ValueError('connection_only_duration_invalid')
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    deadline = time.monotonic() + duration_seconds
    lock = acquire_worker_lock(state_dir)
    probe, outputs, previous = None, [], None
    def expired(*_):
        raise DeadlineExpired()
    try:
        probe = SubscriptionConnection(state_dir, allowed_hosts, route_digest, subscription_id)
        output = BoundedOutput(stdout)
        outputs.append(output)
        diagnostic = BoundedOutput(stderr)
        outputs.append(diagnostic)
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, max(.001, deadline-time.monotonic()))
        diagnostic.emit({'event':'connection_only_started','mode':MODE,
                         'instance_id':probe.instance_id,'duration_seconds':duration_seconds}, deadline)
        buffer, count = b'', 0
        while time.monotonic() < deadline:
            ready, _, _ = select.select([stdin_fd], [], [], min(.1, max(0., deadline-time.monotonic())))
            if not ready:
                continue
            chunk = os.read(stdin_fd, 4096)
            if not chunk:
                return 0
            buffer += chunk
            if len(buffer) > 65536:
                return 1
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                count += 1
                if count > 256:
                    return 1
                try:
                    response = probe.rpc(json.loads(line))
                except (ValueError, UnicodeError, RecursionError):
                    response = {'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}}
                if response is not None:
                    output.emit(response, deadline)
        return 0
    except (DeadlineExpired, BrokenPipeError):
        return 0
    finally:
        if previous is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        for output in reversed(outputs):
            try:
                output.close()
            except OSError:
                pass
        if probe is not None:
            probe.close()
        lock.close()
