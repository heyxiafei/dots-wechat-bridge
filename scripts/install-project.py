#!/usr/bin/env python3
"""Install this project; keep platform setup and account activation explicit.

install/check never start a service, read credentials, or contact WeChat.
connect requires a caller-recorded, exact setup approval; that record is a
precondition, not a replacement for platform permission checks.
"""
import argparse
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import plistlib
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import venv
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {'AGENTS.md', 'README.md', 'LICENSE', 'NOTICE.md', 'requirements.lock', 'requirements.txt',
              '.gitignore', 'PUBLIC-BUNDLE-MANIFEST.json'}
SOURCE_DIRS = {'probe', 'probe_tests', 'scripts', 'config', 'docs', 'licenses', 'skills'}
FRESH_MESSAGE_FILES = ('probe/new_message_check.py', 'scripts/check-new-message.py')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as out:
        json.dump(value, out, indent=2, ensure_ascii=False)
        out.write('\n')


def source_files(root):
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if rel.parts[0] not in SOURCE_DIRS and str(rel) not in ROOT_FILES:
            continue
        if rel.parts[0] == 'skills' and rel.parts[:2] != ('skills', 'dot-wechat-bridge'):
            continue
        if any(p in ('__pycache__', '.git', '.venv', 'runtime', 'secrets') for p in rel.parts):
            continue
        if path.is_symlink():
            raise ValueError('source_symlink_not_allowed')
        if path.is_file() and path.suffix not in ('.pyc', '.sqlite', '.db'):
            yield path


def run(args, cwd, timeout=180, stage='subprocess'):
    result = subprocess.run([str(x) for x in args], cwd=cwd, capture_output=True,
                            text=True, timeout=timeout, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=''))
    if result.returncode:
        # No command output is exposed: later subprocesses may handle private state.
        raise RuntimeError(stage + '_failed_' + str(result.returncode))
    return result


def python(root):
    return root / '.venv/bin/python'


def preflight_file(root, relative):
    """Metadata only; reject symlinks before opening any declared source file."""
    path = root / relative
    try:
        for item in tuple(reversed(path.parents)) + (path,):
            if stat.S_ISLNK(item.lstat().st_mode):
                return {'status': 'symlink_refused'}
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            return {'status': 'not_regular_file'}
        return {'status': 'present', 'size_bytes': info.st_size,
                'mode': format(stat.S_IMODE(info.st_mode), '04o'),
                'executable_bit': bool(info.st_mode & 0o111)}
    except FileNotFoundError:
        return {'status': 'missing'}
    except OSError:
        return {'status': 'unreadable'}


def preflight_source(root):
    receipt = preflight_file(root, 'INSTALL-SOURCE.json')
    if receipt['status'] != 'present':
        return {'status': 'installation_receipt_' + receipt['status']}
    try:
        if receipt['size_bytes'] > 2 * 1024 * 1024:
            raise ValueError('receipt_too_large')
        rows = json.loads((root/'INSTALL-SOURCE.json').read_text())['files']
        if not isinstance(rows, list) or not 1 <= len(rows) <= 3000:
            raise ValueError('invalid_receipt')
        seen, mismatches = set(), 0
        for row in rows:
            relative = row['path']
            path = Path(relative)
            if (not isinstance(relative, str) or path.is_absolute() or '..' in path.parts
                    or relative in seen or not path.parts
                    or (path.parts[0] not in SOURCE_DIRS and relative not in ROOT_FILES)
                    or any(part in ('runtime', 'secrets', '.venv', '.git', '__pycache__') for part in path.parts)
                    or path.suffix in ('.db', '.sqlite', '.pyc')
                    or (path.parts[0] == 'skills' and path.parts[:2] != ('skills', 'dot-wechat-bridge'))
                    or not isinstance(row['sha256'], str)
                    or not re.fullmatch('[0-9a-f]{64}', row['sha256'])):
                raise ValueError('invalid_receipt')
            seen.add(relative)
            item = preflight_file(root, relative)
            if item['status'] != 'present' or item['size_bytes'] > 16 * 1024 * 1024:
                mismatches += 1
            elif hashlib.sha256((root/path).read_bytes()).hexdigest() != row['sha256']:
                mismatches += 1
        return {'status': 'matches_install_receipt' if not mismatches else 'source_mismatch',
                'checked_files': len(rows), 'mismatch_count': mismatches,
                'fresh_message_files_verified': not mismatches and set(FRESH_MESSAGE_FILES).issubset(seen),
                'publisher_provenance': 'not_verified_by_local_receipt'}
    except (OSError, ValueError, TypeError, KeyError):
        return {'status': 'installation_receipt_invalid'}


def preflight_dependencies(root):
    """Inspect metadata from the selected installation interpreter; never spawn it."""
    lock = preflight_file(root, 'requirements.lock')
    if lock['status'] != 'present' or lock['size_bytes'] > 65536:
        return {'status': 'dependency_lock_unavailable'}
    if Path(sys.prefix).absolute() != (root/'.venv').absolute():
        return {'status': 'use_installation_python_to_verify'}
    try:
        content = (root/'requirements.lock').read_text()
        expected = dict(re.findall(r'^([A-Za-z0-9_-]+)==([^\s]+)$', content, re.M))
        required = {'cryptography', 'Pillow', 'cffi', 'pycparser', 'qrcode'}
        if set(expected) != required:
            return {'status': 'dependency_lock_invalid'}
        missing, mismatched = [], []
        for name in sorted(required):
            try:
                distribution = importlib.metadata.distribution(name)
                location = Path(distribution.locate_file('')).resolve()
                if (root/'.venv') not in location.parents:
                    missing.append(name)
                elif distribution.version != expected[name]:
                    mismatched.append(name)
            except importlib.metadata.PackageNotFoundError:
                missing.append(name)
        return {'status': 'locked_dependencies_present' if not missing and not mismatched else 'dependency_mismatch',
                'missing': missing, 'mismatched': mismatched}
    except (OSError, ValueError, TypeError):
        return {'status': 'dependency_metadata_unavailable'}


def preflight_binding(root):
    """Count the selected installation's binding; never load Probe or a token."""
    database = root/'runtime/state/probe.sqlite'
    info = preflight_file(root, 'runtime/state/probe.sqlite')
    if info['status'] != 'present':
        return {'status': 'binding_database_' + info['status']}
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = preflight_file(root, 'runtime/state/probe.sqlite' + suffix)
        if sidecar['status'] not in ('present', 'missing'):
            return {'status': 'binding_sidecar_refused'}
    if Path(str(database)+'-wal').exists() and not Path(str(database)+'-shm').exists():
        return {'status': 'binding_wal_requires_existing_shm'}

    def authorize(action, table, column, _database, _trigger):
        if action == sqlite3.SQLITE_READ:
            allowed = {'sqlite_master': {'name', 'type'}, 'weixin_accounts': {'', 'status', 'last_poll'}}
            return sqlite3.SQLITE_OK if column in allowed.get(table, set()) else sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and column in ('count', 'sum', 'max'):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    try:
        db = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=2)
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA trusted_schema=OFF')
            db.execute('BEGIN')
            db.set_authorizer(authorize)
            deadline = time.monotonic() + 2
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            row = db.execute("SELECT type FROM sqlite_master WHERE name='weixin_accounts'").fetchone()
            if row != ('table',):
                return {'status': 'binding_schema_unavailable'}
            total, ready, last_poll = db.execute("SELECT count(*), sum(CASE WHEN status='ready' THEN 1 ELSE 0 END), max(last_poll) FROM weixin_accounts").fetchone()
        finally:
            db.close()
        valid_poll = type(last_poll) in (int, float) and math.isfinite(last_poll) and 0 <= last_poll <= 4102444800
        return {'status': 'confirmed_locally' if total == 1 and ready == 1 else 'not_confirmed_locally',
                'account_count': total, 'ready_count': ready or 0,
                'last_poll_utc': datetime.fromtimestamp(last_poll, timezone.utc).isoformat() if valid_poll else None,
                'live_authentication': 'not_verified', 'rescan_recommended': False}
    except (sqlite3.DatabaseError, OSError, ValueError, OverflowError):
        return {'status': 'binding_schema_or_read_unavailable'}


def preflight(root, scope='connection_check'):
    """Read-only diagnosis, never a startup permit or a connectivity test."""
    if scope not in ('connection_check', 'weixin_transport_check', 'production'):
        raise ValueError('invalid_preflight_scope')
    root = Path(root).absolute()
    files = {name: preflight_file(root, path) for name, path in {
        'profile': 'runtime/profile.yaml', 'runtime_key': 'runtime/secrets/runtime.key',
        'client': 'bin/tunnel-client', 'setup_approval': 'runtime/setup-approval.json'}.items()}
    source, dependencies, binding = preflight_source(root), preflight_dependencies(root), preflight_binding(root)
    transport_only = scope == 'weixin_transport_check'
    diagnostic_files = {name: preflight_file(root, name) for name in FRESH_MESSAGE_FILES}
    diagnostic_present = all(item['status'] == 'present' for item in diagnostic_files.values())
    diagnostic_verified = diagnostic_present and source.get('fresh_message_files_verified') is True
    fresh_mode = ('available_requires_operator_verification' if diagnostic_verified else
                  'installed_but_unverified' if diagnostic_present else 'not_installed')
    blockers = []
    if sys.version_info < (3, 10):
        blockers.append('python_3_10_or_newer_required')
    if source['status'] != 'matches_install_receipt':
        blockers.append('installation_integrity_unverified')
    if dependencies['status'] != 'locked_dependencies_present':
        blockers.append('installed_dependencies_unverified')
    for name, info in files.items():
        info['scope_requirement'] = 'not_required_for_this_scope' if transport_only else 'required_for_this_scope'
        if not transport_only and info['status'] != 'present':
            blockers.append(name + '_not_ready')
    key = files['runtime_key']
    if not transport_only and key['status'] == 'present' and (int(key['mode'], 8) & 0o077 or key['size_bytes'] == 0):
        blockers.append('runtime_key_not_private_or_empty')
    if not transport_only and files['client']['status'] == 'present' and not files['client']['executable_bit']:
        blockers.append('client_not_executable')
    if transport_only and binding['status'] != 'confirmed_locally':
        blockers.append('unique_ready_binding_required')
    if transport_only and not diagnostic_verified:
        blockers.append('single_message_diagnostic_not_verified')
    return {'status': 'preflight_complete', 'scope': scope, 'read_only': True,
            'production_started': False, 'startup_authorized': False,
            'local_setup_complete': not blockers, 'local_blockers': blockers,
            'python': {'version': '.'.join(map(str, sys.version_info[:3])),
                       'supported': sys.version_info >= (3, 10),
                       'installation_interpreter': Path(sys.prefix).absolute() == root/'.venv'},
            'installation': source, 'dependencies': dependencies, 'binding': binding,
            'configuration_metadata': files,
            'next_binding_action': 'preserve_existing_binding' if binding['status'] == 'confirmed_locally' else 'resolve_setup_before_considering_scan',
            'external_checks': {'client_os_acceptance': 'not_required_for_this_scope' if transport_only else 'not_verified',
                                'writer_isolation': 'not_verified', 'test_round_authorization': 'not_verified',
                                'platform_authorization': 'not_required_for_this_scope' if transport_only else 'not_verified',
                                'mcp_connectivity': 'not_required_for_this_scope' if transport_only else 'not_verified',
                                'fresh_inbound_message': 'not_tested', 'reply_api_acceptance': 'not_tested',
                                'phone_receipt': 'not_tested'},
            'history': {'required_for_this_scope': None if scope == 'production' else False,
                        'status': 'not_evaluated_production_rules_unchanged' if scope == 'production' else 'out_of_scope_not_read'},
            'single_fresh_message_mode': fresh_mode, 'diagnostic_file_metadata': diagnostic_files,
            'note': 'File presence and local binding do not prove live connectivity, OS acceptance or permission. No observations here authorize startup.'}


def smoke(root):
    state = root / 'runtime/install-test-state'
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'get_weixin_status', 'arguments': {}}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'reply_weixin_message', 'arguments': {}}},
    ]
    result = subprocess.run([str(python(root)), '-B', str(root/'probe/probe.py'), '--offline', '--state-dir', str(state)],
                            input=''.join(json.dumps(x)+'\n' for x in requests), text=True,
                            capture_output=True, timeout=20, cwd=root,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=''))
    if result.returncode:
        raise RuntimeError('offline_start_failed')
    rows = [json.loads(x) for x in result.stdout.splitlines()]
    if len(rows) != 3 or 'result' not in rows[0] or 'result' not in rows[1] or rows[2].get('error', {}).get('message') != 'offline_mode_operation_disabled':
        raise RuntimeError('offline_protocol_check_failed')
    # Independent SQL read proves this is an empty test database, not migrated history.
    import sqlite3
    with sqlite3.connect((state/'probe.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        counts = {name: db.execute('SELECT count(*) FROM '+name).fetchone()[0]
                  for name in ('weixin_accounts', 'weixin_inbox', 'weixin_outbox', 'subscriptions')}
    if any(counts.values()):
        raise RuntimeError('offline_test_state_not_empty')
    return {'offline_stdio': 'passed', 'mutations_blocked': True, 'counts': counts,
            'production_started': False, 'phone_e2e': 'not_tested'}


def check(root, tests=False):
    root = root.resolve()
    receipt = json.loads((root/'INSTALL-SOURCE.json').read_text())
    for row in receipt['files']:
        path = root/row['path']
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('installed_source_changed')
    code = "import importlib.metadata as m,json;print(json.dumps({n:m.version(n) for n in ['cryptography','Pillow','cffi','pycparser','qrcode']}))"
    actual = json.loads(run([python(root), '-c', code], root, stage='dependencies').stdout)
    expected = dict(re.findall(r'^([A-Za-z0-9_-]+)==([^\s]+)$', (root/'requirements.lock').read_text(), re.M))
    if actual != expected:
        raise ValueError('dependency_version_mismatch')
    run([python(root), '-B', 'scripts/generate-schema.py', '--check'], root, stage='schema')
    result = smoke(root)
    result['dependencies'] = actual
    if tests:
        test = run([python(root), '-B', '-m', 'unittest', 'discover', '-s', 'probe_tests', '-q'], root, stage='full_tests')
        count = re.search(r'Ran (\d+) tests', test.stderr+test.stdout)
        result['tests_passed'] = int(count.group(1)) if count else None
    return result


def install(source, prefix, wheelhouse=None, tests=False):
    if sys.version_info < (3, 10):
        raise ValueError('python_3_10_or_newer_required')
    source = source.resolve()
    prefix = prefix.absolute()
    if source == prefix.resolve() or source in prefix.resolve().parents:
        raise ValueError('destination_must_be_outside_source')
    if prefix.exists() or prefix.is_symlink():
        raise ValueError('destination_exists_no_overwrite')
    files = list(source_files(source))
    if not files:
        raise ValueError('empty_source')
    prefix.mkdir(parents=True, mode=0o700)
    manifest = []
    for path in files:
        rel = path.relative_to(source)
        dest = prefix/rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        manifest.append({'path': str(rel), 'sha256': hashlib.sha256(dest.read_bytes()).hexdigest()})
    write_json(prefix/'INSTALL-SOURCE.json', {'format': 1, 'files': manifest})
    venv.EnvBuilder(with_pip=True).create(prefix/'.venv')
    command = [python(prefix), '-m', 'pip', '--isolated', 'install', '--disable-pip-version-check', '--no-input']
    if wheelhouse is not None:
        command += ['--no-index', '--find-links', wheelhouse.resolve()]
    command += ['-r', prefix/'requirements.lock']
    run(command, prefix, stage='dependency_install')
    result = check(prefix, tests)
    write_json(prefix/'INSTALL-RESULT.json', result)
    return result


def activation_prerequisites(root):
    """No credential contents are read here; approval cannot be auto-generated."""
    approval = json.loads((root/'runtime/setup-approval.json').read_text())
    if approval.get('isolation_mode') not in ('no_existing_writer_verified', 'old_writer_fenced'):
        raise ValueError('single_writer_isolation_unconfirmed')
    for key in ('approval_reference', 'isolation_evidence_reference'):
        if not isinstance(approval.get(key), str) or not approval[key].strip():
            raise ValueError('setup_approval_missing')
    profile = root/'runtime/profile.yaml'
    if profile.is_symlink() or hashlib.sha256(profile.read_bytes()).hexdigest() != approval.get('profile_sha256'):
        raise ValueError('approved_profile_mismatch')
    key = root/'runtime/secrets/runtime.key'
    if key.is_symlink() or not key.is_file() or key.stat().st_mode & 0o077 or key.stat().st_size == 0:
        raise ValueError('private_runtime_key_not_ready')
    client = root/'bin/tunnel-client'
    if client.is_symlink() or not client.is_file() or not os.access(client, os.X_OK):
        raise ValueError('official_tunnel_client_not_ready')
    if hashlib.sha256(client.read_bytes()).hexdigest() != approval.get('client_sha256'):
        raise ValueError('approved_client_mismatch')
    if approval.get('startup_mode') not in ('foreground', 'launch_agent'):
        raise ValueError('startup_mode_not_approved')
    return approval


def require_owner_binding(root):
    import sqlite3
    database = root/'runtime/state/probe.sqlite'
    if database.is_symlink() or not database.is_file():
        raise ValueError('owner_scan_not_confirmed')
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
        # Read only binding counts/status, never credential columns.
        total = db.execute('SELECT count(*) FROM weixin_accounts').fetchone()[0]
        ready = db.execute("SELECT count(*) FROM weixin_accounts WHERE status='ready'").fetchone()[0]
    if total != 1 or ready != 1:
        raise ValueError('owner_scan_not_confirmed')


def require_state_continuity(root, approval):
    """Check retained local facts; external evidence references are not authority.

    No credentials, callback URLs or signing secrets are selected. A trusted
    operator must have obtained the history checkpoints from the actual stopped
    old runtime. Computing them from an empty replacement cannot prove retention.
    Checkpoints cover immutable original rows; later rows and send/subscription
    status changes are deliberately allowed on normal restarts.
    """
    if approval.get('isolation_mode') == 'no_existing_writer_verified':
        return {'retained_subscription_state': 'not_applicable_first_install',
                'subscription_refresh_performed': False}
    handoff = approval.get('handoff')
    if not isinstance(handoff, dict):
        raise ValueError('retained_state_handoff_required')
    if handoff.get('continuity_mode') == 'new_source_cutpoint':
        raise ValueError('new_source_cutpoint_mode_not_supported')
    if handoff.get('continuity_mode') != 'retained_state':
        raise ValueError('retained_state_handoff_required')
    for key in ('platform_route_evidence_reference', 'subscription_continuity_evidence_reference',
                'history_retention_evidence_reference'):
        if not isinstance(handoff.get(key), str) or not handoff[key].strip():
            raise ValueError('handoff_evidence_reference_required')
    import sqlite3
    database = root/'runtime/state/probe.sqlite'
    if database.is_symlink() or not database.is_file():
        raise ValueError('retained_state_handoff_required')
    with sqlite3.connect(database.as_uri()+'?mode=ro', uri=True) as db:
        # A read transaction gives all checks a consistent SQLite snapshot.
        db.execute('BEGIN')
        accounts = db.execute('SELECT id,owner FROM weixin_accounts').fetchall()
        binding = hashlib.sha256(json.dumps(accounts[0], separators=(',', ':')).encode()).hexdigest() if len(accounts) == 1 else None
        if not binding or binding != handoff.get('owner_binding_sha256'):
            raise ValueError('handoff_owner_binding_mismatch')
        subscriptions = db.execute("SELECT id,expires FROM subscriptions WHERE id=? AND name='weixin.message' AND (expires IS NULL OR expires>0) AND verified_at>0",
                                   (handoff.get('subscription_id'),)).fetchall()
        competing = db.execute("SELECT 1 FROM subscriptions WHERE id!=? AND name='weixin.message' AND (expires IS NULL OR expires>?) LIMIT 1",
                               (handoff.get('subscription_id'), time.time())).fetchone()
        if len(subscriptions) != 1 or competing:
            raise ValueError('subscription_continuity_unconfirmed')
        expiry = subscriptions[0][1]
        continuity = {'retained_subscription_state': 'expired_requires_same_id_refresh'
                      if expiry is not None and expiry <= time.time() else 'active',
                      'subscription_refresh_performed': False}
        history = handoff.get('history')
        columns = {
            'weixin_inbox':'id,account,peer,source_id,text,created_at,kind',
            'weixin_outbox':'message_id,account,peer,text,client_id,created_at,kind,upload_id,source_message_id,ack_origin',
        }
        for table, fields in columns.items():
            checkpoint = history.get(table) if isinstance(history, dict) else None
            if (not isinstance(checkpoint, dict)
                    or type(checkpoint.get('through_rowid')) is not int or checkpoint['through_rowid'] < 0
                    or type(checkpoint.get('count')) is not int or checkpoint['count'] < 0
                    or not isinstance(checkpoint.get('sha256'), str)
                    or re.fullmatch(r'[0-9a-f]{64}', checkpoint['sha256']) is None):
                raise ValueError('retained_history_checkpoint_required')
            if table == 'weixin_inbox' and checkpoint['count'] == 0:
                raise ValueError('retained_inbox_history_required')
            digest, count = hashlib.sha256(), 0
            for row in db.execute('SELECT rowid,'+fields+' FROM '+table+' WHERE rowid<=? ORDER BY rowid', (checkpoint['through_rowid'],)):
                digest.update((json.dumps(row, ensure_ascii=True, separators=(',', ':'))+'\n').encode())
                count += 1
            if count != checkpoint['count'] or digest.hexdigest() != checkpoint['sha256']:
                raise ValueError('retained_history_mismatch')
            if table == 'weixin_outbox':
                states = checkpoint.get('submission_states')
                pending = {'queued', 'preparing', 'context_required', 'auth_required'}
                terminal = {'accepted', 'rejected', 'delivery_unknown', 'superseded', 'cancelled'}
                known = pending | terminal | {'sending'}
                if (not isinstance(states, list) or len(states) != count
                        or any(not isinstance(item, list) or len(item) != 2
                               or type(item[0]) is not int or not isinstance(item[1], str)
                               or item[1] not in known for item in states)):
                    raise ValueError('retained_submission_states_required')
                baseline = dict(states)
                current = dict(db.execute('SELECT rowid,status FROM weixin_outbox WHERE rowid<=?',
                                          (checkpoint['through_rowid'],)))
                if len(baseline) != count or baseline.keys() != current.keys():
                    raise ValueError('retained_submission_states_required')
                for rowid, original in baseline.items():
                    # A real unknown or terminal action cannot become sendable.
                    # An interrupted POST may only retain sending until the
                    # existing worker recovery marks it delivery_unknown.
                    allowed = known if original in pending else ({'sending', 'delivery_unknown'} if original == 'sending' else {original})
                    if current[rowid] not in allowed:
                        raise ValueError('retained_submission_state_regression')
        return continuity


def launch_agent_spec(root):
    label = 'com.xiafei.dot-wechat.' + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    return {'Label': label, 'WorkingDirectory': str(root),
            'ProgramArguments': [str(python(root)), str(root/'scripts/install-project.py'), 'run', '--prefix', str(root)],
            'RunAtLoad': True, 'KeepAlive': {'SuccessfulExit': False}, 'ThrottleInterval': 60,
            'StandardOutPath': '/dev/null', 'StandardErrorPath': '/dev/null'}


def start(root, register=False):
    approval = activation_prerequisites(root)
    require_owner_binding(root)
    continuity = require_state_continuity(root, approval)
    if register:
        if sys.platform != 'darwin' or approval['startup_mode'] != 'launch_agent':
            raise ValueError('launch_agent_not_approved')
        spec = launch_agent_spec(root)
        directory = Path.home()/'Library/LaunchAgents'
        if directory.is_symlink():
            raise ValueError('launch_agent_directory_symlink')
        directory.mkdir(parents=True, exist_ok=True)
        target = directory/(spec['Label']+'.plist')
        # Never overwrite, unload or retry an existing service. Reconcile first.
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as out:
            plistlib.dump(spec, out)
        run(['/bin/launchctl', 'bootstrap', 'gui/'+str(os.getuid()), target], root, stage='launch_agent')
        return {**continuity, 'launch_agent_registered': spec['Label'], 'online_mcp_verified': False,
                'phone_e2e': 'not_tested', 'note': 'registration is not a readiness receipt'}
    # Expose the actual pre-start observation; expired is not renewed or ready.
    print(json.dumps({'status': 'activation_preflight', **continuity,
                      'online_mcp_verified': False}, ensure_ascii=False), flush=True)
    os.execv(str(root/'bin/tunnel-client'), [str(root/'bin/tunnel-client'), 'run', '--profile-file', str(root/'runtime/profile.yaml')])


def connect(root):
    diagnosis = preflight(root, scope='production')
    if diagnosis['local_blockers']:
        return {**diagnosis, 'status': 'setup_incomplete',
                'note': 'Resolve the complete preflight report before scanning; an existing binding is preserved.'}
    try:
        activation_prerequisites(root)
        check(root)
    except Exception as error:
        reasons = {'single_writer_isolation_unconfirmed', 'setup_approval_missing',
                   'approved_profile_mismatch', 'private_runtime_key_not_ready',
                   'official_tunnel_client_not_ready', 'approved_client_mismatch',
                   'startup_mode_not_approved', 'installed_source_changed', 'dependency_version_mismatch'}
        reason = str(error) if str(error) in reasons else 'activation_or_installation_check_failed'
        return {**diagnosis, 'status': 'setup_incomplete', 'activation_blocker': reason,
                'note': 'Activation prerequisites failed before any QR request; preserve an existing binding.'}
    # Reuse reviewed owner-only login flow; never print the QR payload or tokens.
    helper = root/'scripts/owner-wechat-login.py'
    if diagnosis['binding']['status'] != 'confirmed_locally':
        run([python(root), helper, '--connect'], root, timeout=360, stage='owner_scan')
    try:
        require_owner_binding(root)
    except Exception:
        return {'status': 'binding_unconfirmed', 'production_started': False,
                'activation_blocker': 'owner_binding_not_verified', 'phone_e2e': 'not_tested',
                'note': 'scan helper completed; current binding not verified; preserve state and reconcile before another scan'}
    try:
        approval = activation_prerequisites(root)
        require_state_continuity(root, approval)
    except Exception as error:
        safe_reasons = {'single_writer_isolation_unconfirmed', 'setup_approval_missing',
                        'approved_profile_mismatch', 'private_runtime_key_not_ready',
                        'official_tunnel_client_not_ready', 'approved_client_mismatch',
                        'startup_mode_not_approved', 'retained_state_handoff_required',
                        'new_source_cutpoint_mode_not_supported', 'handoff_evidence_reference_required',
                        'handoff_owner_binding_mismatch', 'subscription_continuity_unconfirmed',
                        'retained_history_checkpoint_required', 'retained_inbox_history_required',
                        'retained_history_mismatch', 'retained_submission_states_required',
                        'retained_submission_state_regression'}
        return {'status': 'bound_but_not_started', 'production_started': False,
                'activation_blocker': str(error) if str(error) in safe_reasons else 'state_continuity_check_failed',
                'phone_e2e': 'not_tested', 'note': 'binding confirmed; preserve state and reconcile activation, do not rescan'}
    return start(root, register=approval['startup_mode'] == 'launch_agent')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'check', 'preflight', 'connect', 'run'))
    parser.add_argument('--prefix', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--tests', action='store_true')
    parser.add_argument('--scope', choices=('connection_check', 'weixin_transport_check', 'production'), default='connection_check',
                        help='Preflight diagnosis only; never changes startup or retained-state requirements')
    args = parser.parse_args()
    try:
        if args.action != 'preflight' and args.scope != 'connection_check':
            parser.error('--scope applies only to preflight')
        if args.action == 'preflight':
            result = preflight(args.prefix, args.scope)
        elif args.action == 'install':
            result = install(ROOT, args.prefix, args.wheelhouse, args.tests)
        elif args.action == 'check':
            result = check(args.prefix, args.tests)
        elif args.action == 'connect':
            result = connect(args.prefix.resolve())
        else:
            check(args.prefix)
            result = start(args.prefix.resolve())
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('status') == 'setup_incomplete' else 0
    except Exception as error:
        # Fixed errors only; no private exception values or logs.
        value = str(error)
        safe = value if re.fullmatch(r'(subprocess|dependencies|schema|full_tests|dependency_install|owner_scan|launch_agent)_failed_-?[0-9]+', value) else None
        print(json.dumps({'status': 'blocked', 'error_type': type(error).__name__, 'failed_stage': safe}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
