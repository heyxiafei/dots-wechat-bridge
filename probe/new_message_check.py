"""One explicitly authorized, finite Weixin transport check; never a daemon.

Only an unused, already-bound installation is eligible. Authorization and old
writer isolation are verified by the human/operator outside this module. Their
references are audit breadcrumbs, never permission grants or a remote fence.
"""
import hashlib
import math
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from .probe import acquire_worker_lock
from .weixin import IlinkClient, WeixinError, api_base, encoded


ROUND = re.compile(r'[0-9a-f]{32}\Z')
LOCAL_ID = re.compile(r'wx_[0-9a-f]{32}\Z')
TABLE = 'weixin_new_message_check'
PHASES = {'waiting', 'stopped', 'sending', 'accepted', 'rejected', 'delivery_unknown'}
ERRORS = {
    'none', 'test_window_expired', 'interrupted_no_restart', 'poll_failed',
    'invalid_update_batch', 'poll_rejected', 'ambiguous_matching_messages',
    'unmatched_batch_stopped_no_cursor_advance', 'binding_changed',
    'state_changed', 'send_rejected', 'invalid_send_status_type',
    'unexpected_send_response_shape', 'send_outcome_unknown',
    'receipt_state_mismatch',
}
API_ERRORS = {'api_timeout', 'api_network_error', 'api_http_error', 'invalid_api_json',
              'invalid_api_response', 'api_response_too_large', 'invalid_api_destination',
              'non_public_api_address', 'invalid_api_endpoint'}
ERRORS.update('poll_' + value for value in API_ERRORS)
ERRORS.update('send_' + value for value in API_ERRORS)
# Exact R7 built-in schema, audited in an empty synthetic Probe. Never remove
# triggers or treat an arbitrary trigger with a familiar name as safe.
TRIGGERS = {
    'weixin_dot_cutover_enable_guard': 'a1a54ae21c76b80ef9e36cf53c633cc00fe569d0a6f4e5050c8895aedd6dcf55',
    'weixin_dot_cutover_ingress': '8d40187df69d6f89e6657b6c9fdd14fd9ee9e8748563855834757c1997fab5a3',
    'weixin_dot_cutover_scope_guard': '058f7d17aa7bca3a06a701921dba2222f98aec96a49fa3fb475342c3cd2ad19f',
    'weixin_dot_journal_attempt_guard': 'a6b0a420ead3bd297d6b2f47381c231ec28ca16384591c050c7620edf8f07cc5',
    'weixin_dot_journal_authorize_guard': '5534456c823976d867a0cff314d044db42080815de5cbb64f3b5c285badce5bb',
    'weixin_dot_journal_history_guard': 'f039da94bdbd94d335ad8e404ada30b66aaf263edacc8ac15a01bd49ffa56d41',
    'weixin_dot_journal_steady_marker_guard': '9c10bb1ff0a8dad014f535d9448a1d27117ee12ea977d60485e555bcbd7951ab',
    'weixin_dot_journal_steady_marker_insert_guard': '1bf34d2e1ea57997b87d55e73feaa4f7d03d6e181d38a76d6805036936139544',
    'weixin_dot_mirror_epoch_ingress': '87e294b4fe73e706512d0c8a2b6e2580f03a397695245bb3e1216e5d148eff6e',
    'weixin_dot_steady_future_ingress': '214f433a97ec359759cd4f35f4b223fda76141ab608dee4ec1a9b78118cdaa0d',
    'weixin_dot_steady_generation_guard': '87020005e57bed9fa0961cc4e50d8c2a688301d705fb674bea14f4eb29089bcf',
    'weixin_dot_steady_generation_no_delete': '653723f2a18de95d7fdad7d3ab387c5210c14df8c7cc92f9bd07614a537aadb7',
    'weixin_dot_steady_source_no_delete': '2e2778c3e93e90d66b90a8b34f5091de1c9ccf6a02921fe67d2178468f83bfd6',
    'weixin_dot_steady_source_no_update': '5dc5e5c8c2b85398ee20bf3f8324df116dd5779cfba55129bd0214ad8fc06261',
    'weixin_dot_trial_authorize_guard': '8914444e1bd73ea3c3b63465ed227cfde4bcc67bd71fa193a791af9c23acbc8f',
    'weixin_dot_trial_authorized_insert_guard': 'fcae7e3e6567d55094c796392a0b38bea1d15f8db3edc9066c6b21bea56549a4',
    'weixin_dot_trial_bulk_enable_guard': '55575797e9bacaec6bd8565e872f4a17b82722fc36677ce6c2ad3e663b5fe899',
    'weixin_dot_trial_future_ingress': '0016147927c8345843315e46ab93d1d936259adb678869c0fcae6b5c8c479ec2',
    'weixin_dot_trial_immutable': '9be069ce5685ac5f15e6db9255d6b197fb2cefe4f41df7d0d70da7dcd4e6d83e',
    'weixin_dot_trial_no_delete': 'b4cb3124dab0900a1a49d9443254bfaa20934997a62fa4affda1de81692ff93d',
}


class CheckError(Exception):
    """Only fixed safe error categories may reach the CLI."""


def phrase(round_id):
    if not isinstance(round_id, str) or not ROUND.fullmatch(round_id):
        raise CheckError('invalid_round_id')
    return '新机测试 ' + round_id


def plan():
    round_id = secrets.token_hex(16)
    return {'mode': 'plan', 'round_id': round_id, 'test_phrase': phrase(round_id),
            'authorization_granted': False, 'old_writer_isolation_verified': False,
            'requires': ['explicit_current_round_authorization', 'actual_old_writer_isolation',
                         'unused_new_installation_with_one_ready_binding'],
            'max_duration_seconds': 180, 'starts_service': False,
            'requires_tunnel': False, 'proves_cloud_dot_connection': False}


def checked_database(directory):
    directory = Path(directory)
    if not directory.is_absolute() or '..' in directory.parts:
        raise CheckError('absolute_state_path_required')
    path = directory / 'probe.sqlite'
    for part in tuple(reversed(path.parents)) + (path,):
        try:
            mode = part.lstat().st_mode
        except OSError:
            raise CheckError('existing_state_required') from None
        if stat.S_ISLNK(mode):
            raise CheckError('state_symlink_refused')
    if not stat.S_ISREG(path.stat().st_mode):
        raise CheckError('existing_state_required')
    for suffix in ('-wal', '-shm', '-journal'):
        if Path(str(path) + suffix).is_symlink():
            raise CheckError('state_symlink_refused')
    return path


def open_database(path, readonly=False):
    db = sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
                         uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA trusted_schema=OFF')
    if readonly:
        db.execute('PRAGMA query_only=ON')
    return db


def binding(db, credentials=False):
    fields = 'id,owner,status,cursor' + (',token,base_url' if credentials else '')
    rows = db.execute('SELECT ' + fields + ' FROM weixin_accounts').fetchall()
    epoch = db.execute('SELECT epoch FROM weixin_binding_epoch WHERE id=1').fetchall()
    if len(rows) != 1 or rows[0]['status'] != 'ready' or len(epoch) != 1:
        raise CheckError('one_ready_binding_required')
    account = dict(rows[0])
    if any(not isinstance(account[key], str) or not 0 < len(account[key]) <= 8192 for key in ('id', 'owner')):
        raise CheckError('one_ready_binding_required')
    if account['cursor'] != '':
        raise CheckError('unused_new_binding_required')
    if credentials:
        if not isinstance(account['token'], str) or not 0 < len(account['token']) <= 8192:
            raise CheckError('one_ready_binding_required')
        api_base(account['base_url'])
    digest = hashlib.sha256(encoded([account['id'], account['owner'], epoch[0]['epoch']]).encode()).hexdigest()
    return account, digest


def unused_state(db, allow_round=False):
    """No historical data is read, migrated, cleared, or made sendable."""
    triggers = {row['name']: hashlib.sha256(row['sql'].encode()).hexdigest()
                for row in db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")}
    if triggers != TRIGGERS or db.execute("SELECT 1 FROM sqlite_master WHERE type='view' LIMIT 1").fetchone():
        raise CheckError('custom_schema_refused')
    required = {'weixin_accounts', 'weixin_binding_epoch', 'weixin_inbox',
                'weixin_outbox', 'subscriptions', 'pings'}
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required.issubset(tables):
        raise CheckError('installed_schema_required')
    for name in tables:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
            raise CheckError('custom_schema_refused')
        if name in {'weixin_accounts', 'weixin_binding_epoch', 'sqlite_sequence'}:
            continue
        if name == TABLE and allow_round:
            continue
        if name == 'weixin_quote_shape_once':
            rows = db.execute('SELECT id,armed_at,binding_hash,message_id,shape_json,captured_at FROM weixin_quote_shape_once').fetchall()
            account = db.execute('SELECT id,owner FROM weixin_accounts').fetchall()
            expected = hashlib.sha256(encoded([account[0]['id'], account[0]['owner']]).encode()).hexdigest() if len(account) == 1 else None
            if (not rows or len(rows) == 1 and rows[0]['id'] == 1
                    and type(rows[0]['armed_at']) in (int, float) and math.isfinite(rows[0]['armed_at'])
                    and rows[0]['binding_hash'] == expected
                    and all(rows[0][key] is None for key in ('message_id', 'shape_json', 'captured_at'))):
                continue
        if db.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]:
            raise CheckError('non_diagnostic_state_present')


def round_exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone())


def receipt(db, round_id):
    if not round_exists(db):
        raise CheckError('round_not_found')
    row = db.execute('SELECT * FROM ' + TABLE + ' WHERE singleton=1').fetchone()
    if not row or row['round_id'] != round_id:
        raise CheckError('different_round_already_recorded')
    state = row['phase'] if row['phase'] in PHASES else 'stopped'
    # A fresh process never re-enters waiting/sending, even if no HTTP receipt was
    # committed. The original outbox slot remains non-sendable.
    effective = 'delivery_unknown' if state == 'sending' else 'stopped' if state == 'waiting' else state
    error = row['error_kind'] if row['error_kind'] in ERRORS else 'none'
    if state == 'waiting':
        error = 'interrupted_no_restart'
    consistent = True
    if row['send_id']:
        outgoing = db.execute('SELECT message_id,source_message_id,status,attempts,kind,client_id FROM weixin_outbox WHERE message_id=?', (row['send_id'],)).fetchone()
        consistent = bool(outgoing and outgoing['source_message_id'] == row['source_message_id'] == row['send_id']
                          and outgoing['attempts'] == 1 and outgoing['kind'] == 'reply'
                          and outgoing['client_id'] == 'dots_' + row['send_id'][3:]
                          and outgoing['status'] == state)
        if not consistent:
            effective, error = 'delivery_unknown', 'receipt_state_mismatch'
    return {'mode': 'local_receipt', 'round_id': round_id,
            'status': effective, 'recorded_phase': state, 'error_kind': error,
            'source_message_id': row['source_message_id'] if isinstance(row['source_message_id'], str) and LOCAL_ID.fullmatch(row['source_message_id']) else None,
            'send_id': row['send_id'] if isinstance(row['send_id'], str) and LOCAL_ID.fullmatch(row['send_id']) else None,
            'attempts': 1 if row['send_id'] else 0,
            'attempts_are_durable_submission_markers': True,
            'api_accepted': state == 'accepted' and consistent, 'phone_received': None,
            'cloud_dot_connection_verified': False, 'remote_send_status_query_available': False,
            'old_machine_accessed': False, 'history_imported': False,
            'account_cursor_advanced': False, 'restart_will_send': False}


def read_receipt(directory, round_id):
    phrase(round_id)
    db = open_database(checked_database(directory), readonly=True)
    try:
        return receipt(db, round_id)
    finally:
        db.close()


def check_configuration(directory, client=None):
    """One read-only official getconfig call for this new installation.

    Normal client authentication stays internal. No history, polling, send,
    login, subscription, diagnostic round, or database mutation is involved.
    Optional typing tickets and all other response payloads are discarded.
    """
    path = checked_database(directory)
    if Path(str(path) + '-wal').exists() and not Path(str(path) + '-shm').exists():
        raise CheckError('readonly_wal_without_shm_refused')
    db = open_database(path, readonly=True)
    try:
        db.execute('BEGIN')
        account, _ = binding(db, credentials=True)
    finally:
        db.close()
    report = {'mode': 'check_config', 'status': 'unknown', 'error_kind': None,
              'configuration_readable': None, 'strict_ret_zero': False,
              'messages_polled': False, 'message_sent': False,
              'database_mutated': False, 'response_payload_saved': False,
              'cloud_dot_connection_verified': False, 'phone_received': None}
    try:
        response = (client or IlinkClient()).request(account['base_url'], '/ilink/bot/getconfig',
                    {'ilink_user_id': account['owner']}, account['token'], timeout=10)
        if not isinstance(response, dict) or type(response.get('ret')) is not int:
            report['error_kind'] = 'config_ret_not_verified'
        elif response['ret'] != 0:
            report.update(status='rejected', error_kind='config_rejected', configuration_readable=False)
        elif response.get('errcode') is not None and (type(response['errcode']) is not int or response['errcode'] != 0):
            report['error_kind'] = 'config_response_conflict'
        else:
            report.update(status='verified', configuration_readable=True, strict_ret_zero=True)
    except Exception as error:
        report['error_kind'] = error.kind if isinstance(error, WeixinError) and error.kind in API_ERRORS else 'config_request_failed'
    return report


def candidate(account, message, wanted, started, expires, observed):
    """Reject metadata first; never inspect historic/non-owner bodies."""
    if not isinstance(message, dict):
        return None
    source = message.get('message_id')
    created = message.get('create_time_ms')
    if (message.get('from_user_id') != account['owner']
            or message.get('to_user_id') not in (None, '', account['id'])
            or message.get('group_id') or message.get('room_id')
            or type(message.get('message_type')) is not int or message['message_type'] != 1
            or (message.get('message_state') is not None
                and (type(message['message_state']) is not int or message['message_state'] not in (0, 2)))
            or message.get('delete_time_ms')
            or type(created) not in (int, float) or not math.isfinite(created)
            or not started * 1000 <= created <= min(expires, observed) * 1000
            or type(source) not in (str, int) or not source
            or type(source) is int and source < 1
            or len(str(source)) > 256):
        return None
    if any(key in message for key in ('ref_msg', 'quote', 'quoted_message', 'forward', 'forwarded', 'forward_info')):
        return None
    items = message.get('item_list')
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        return None
    item = items[0]
    metadata = {'create_time_ms', 'update_time_ms', 'is_completed', 'msg_id'}
    if (not {'type', 'text_item'}.issubset(item)
            or not set(item).issubset({'type', 'text_item'} | metadata)
            or type(item['type']) is not int or item['type'] != 1):
        return None
    for key in ('create_time_ms', 'update_time_ms'):
        if key in item and (type(item[key]) not in (int, float)
                            or not math.isfinite(item[key]) or not 0 <= item[key] <= 10**15):
            return None
    if 'is_completed' in item and item['is_completed'] is not True:
        return None
    if 'msg_id' in item and (not isinstance(item['msg_id'], str) or not 0 < len(item['msg_id']) <= 256):
        return None
    text = item['text_item']
    if not isinstance(text, dict) or set(text) != {'text'} or text['text'] != wanted:
        return None
    context = message.get('context_token')
    if not isinstance(context, str) or not 0 < len(context) <= 8192:
        return None
    identity = 'wx_' + hashlib.sha256(encoded([account['id'], account['owner'], str(source)]).encode()).hexdigest()[:32]
    return {'source': str(source), 'local_id': identity, 'created_at': created / 1000,
            'context': context}


def classify_send(result):
    if not isinstance(result, dict):
        return 'delivery_unknown', 'send_outcome_unknown', None
    codes = (result.get('ret'), result.get('errcode'))
    if any(value is not None and type(value) is not int for value in codes):
        return 'delivery_unknown', 'invalid_send_status_type', None
    # All explicit nonzero responses are terminal for this one-attempt check.
    if any(value not in (0, None) for value in codes):
        return 'rejected', 'send_rejected', next(value for value in codes if value not in (0, None))
    if 'base_resp' in result:
        return 'delivery_unknown', 'unexpected_send_response_shape', None
    return 'accepted', 'none', None


def stop_round(db, reason, clock):
    with db:
        db.execute('UPDATE ' + TABLE + " SET phase='stopped',error_kind=?,updated_at=? WHERE singleton=1 AND phase='waiting'", (reason, clock()))


def execute(directory, round_id, approval_reference, isolation_reference, duration=120,
            client=None, clock=time.time, monotonic=time.monotonic, pause=time.sleep,
            announce=lambda value: None):
    """Operator-only bounded check. References are not authorization or fencing.

    No call starts a daemon, loads old state, advances the official cursor, or
    retries a send. Restarting this function for any recorded round is read-only.
    """
    wanted = phrase(round_id)
    if (type(duration) is not int or not 1 <= duration <= 180
            or any(not isinstance(value, str) or not 1 <= len(value) <= 512
                   or '\n' in value or '\r' in value
                   for value in (approval_reference, isolation_reference))):
        raise CheckError('exact_round_authorization_and_isolation_references_required')
    path = checked_database(directory)
    lock = acquire_worker_lock(directory)
    db = None
    try:
        db = open_database(path)
        if round_exists(db) and db.execute('SELECT 1 FROM ' + TABLE + ' LIMIT 1').fetchone():
            return receipt(db, round_id)
        unused_state(db)
        account, fingerprint = binding(db, credentials=True)
        start, mono_start = clock(), monotonic()
        expiry = start + duration
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS ' + TABLE + ''' (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1), round_id TEXT NOT NULL,
                phase TEXT NOT NULL, started_at REAL NOT NULL, expires_at REAL NOT NULL,
                binding_hash TEXT NOT NULL, approval_hash TEXT NOT NULL, isolation_hash TEXT NOT NULL,
                source_message_id TEXT, send_id TEXT, error_kind TEXT NOT NULL,
                updated_at REAL NOT NULL)''')
            db.execute('INSERT INTO ' + TABLE + ' VALUES(1,?,?,?,?,?,?,?,?,?,?,?)',
                       (round_id, 'waiting', start, expiry, fingerprint,
                        hashlib.sha256(approval_reference.encode()).hexdigest(),
                        hashlib.sha256(isolation_reference.encode()).hexdigest(),
                        None, None, 'none', start))
        client = client or IlinkClient()
        announce({'status': 'waiting_for_one_new_message', 'round_id': round_id,
                  'test_phrase': wanted, 'expires_at': expiry,
                  'old_writer_isolation': 'operator_verified_outside_this_script',
                  'authorization_granted_by_script': False})
        selected = None
        while clock() < expiry and monotonic() - mono_start < duration:
            try:
                # The original cursor is intentionally unchanged in memory and
                # on disk. No backlog is drained to reach a newer sample.
                result = client.poll(account)
            except Exception as error:
                reason = 'poll_' + error.kind if isinstance(error, WeixinError) and error.kind in API_ERRORS else 'poll_failed'
                stop_round(db, reason, clock)
                return receipt(db, round_id)
            if not isinstance(result, dict):
                stop_round(db, 'invalid_update_batch', clock)
                return receipt(db, round_id)
            codes = (result.get('ret'), result.get('errcode'))
            if any(value is not None and (type(value) is not int or value != 0) for value in codes):
                stop_round(db, 'poll_rejected', clock)
                return receipt(db, round_id)
            batch = result.get('msgs', [])
            if not isinstance(batch, list) or len(batch) > 1000:
                stop_round(db, 'invalid_update_batch', clock)
                return receipt(db, round_id)
            matches = {}
            for message in batch:
                found = candidate(account, message, wanted, start, expiry, clock())
                if found:
                    matches[found['local_id']] = found
            if len(matches) > 1:
                stop_round(db, 'ambiguous_matching_messages', clock)
                return receipt(db, round_id)
            if matches:
                selected = next(iter(matches.values()))
                break
            if batch:
                stop_round(db, 'unmatched_batch_stopped_no_cursor_advance', clock)
                return receipt(db, round_id)
            pause(min(.5, max(0, expiry - clock())))
        if selected is None or clock() >= expiry or monotonic() - mono_start >= duration:
            stop_round(db, 'test_window_expired', clock)
            return receipt(db, round_id)
        send_id = selected['local_id']
        outgoing = '新 Mac 已收到本条测试消息，且仅回复一次。测试编号：' + round_id
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                unused_state(db, allow_round=True)
                _, current = binding(db)
                if current != fingerprint:
                    raise CheckError('binding_changed')
                if clock() >= expiry or monotonic() - mono_start >= duration:
                    raise CheckError('test_window_expired')
                db.execute('''INSERT INTO weixin_inbox
                    (id,account,peer,source_id,text,created_at,kind,unsupported,event_id)
                    VALUES(?,?,?,?,?,?,'text','[]',NULL)''',
                           (send_id, account['id'], account['owner'], selected['source'], wanted, selected['created_at']))
                db.execute('''INSERT INTO weixin_outbox
                    (message_id,account,peer,text,client_id,status,attempts,created_at,
                     kind,source_message_id,post_started_at)
                    VALUES(?,?,?,?,?,'sending',1,?,'reply',?,?)''',
                           (send_id, account['id'], account['owner'], outgoing,
                            'dots_' + send_id[3:], clock(), send_id, clock()))
                db.execute('UPDATE ' + TABLE + " SET phase='sending',source_message_id=?,send_id=?,updated_at=? WHERE singleton=1", (send_id, send_id, clock()))
        except CheckError as error:
            stop_round(db, str(error) if str(error) in ERRORS else 'state_changed', clock)
            return receipt(db, round_id)
        # Durable sending + attempts=1 precedes the only possible message POST.
        # A process death at any later point is permanently non-retryable here.
        prepost_error = None
        try:
            _, current = binding(db)
            if current != fingerprint:
                prepost_error = 'binding_changed'
        except Exception:
            prepost_error = 'binding_changed'
        if clock() >= expiry or monotonic() - mono_start >= duration:
            prepost_error = 'test_window_expired'
        if prepost_error:
            with db:
                db.execute("UPDATE weixin_outbox SET status='rejected',error_kind=? WHERE message_id=? AND status='sending'", (prepost_error, send_id))
                db.execute('UPDATE ' + TABLE + " SET phase='rejected',error_kind=?,updated_at=? WHERE singleton=1", (prepost_error, clock()))
            return receipt(db, round_id)
        try:
            result = client.send(account, account['owner'], outgoing,
                                 'dots_' + send_id[3:], selected['context'])
            outcome, error, code = classify_send(result)
        except Exception as failure:
            error = 'send_' + failure.kind if isinstance(failure, WeixinError) and failure.kind in API_ERRORS else 'send_outcome_unknown'
            code = failure.code if isinstance(failure, WeixinError) and type(failure.code) is int else None
            outcome = 'delivery_unknown'
        with db:
            db.execute('UPDATE weixin_outbox SET status=?,error_kind=?,server_code=?,accepted_at=? WHERE message_id=? AND status=\'sending\' AND attempts=1',
                       (outcome, None if error == 'none' else error, code,
                        clock() if outcome == 'accepted' else None, send_id))
            db.execute('UPDATE ' + TABLE + ' SET phase=?,error_kind=?,updated_at=? WHERE singleton=1',
                       (outcome, error, clock()))
        return receipt(db, round_id)
    finally:
        if db is not None:
            db.close()
        lock.close()
