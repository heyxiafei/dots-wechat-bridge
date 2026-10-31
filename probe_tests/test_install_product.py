import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/name)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.install = module('install-project.py')

    def offline(self, state, requests):
        return subprocess.run([sys.executable, '-B', str(ROOT/'probe/probe.py'), '--offline', '--state-dir', str(state)],
                              input=''.join(json.dumps(r)+'\n' for r in requests), text=True, capture_output=True, timeout=15)

    def test_offline_real_process_discovers_and_rejects_all_write_paths(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'server/discover'}]
        for i, method in enumerate(('events/subscribe', 'events/unsubscribe', 'tools/call'), 2):
            requests.append({'jsonrpc': '2.0', 'id': i, 'method': method,
                             'params': {'name': 'reply_weixin_message', 'arguments': {}}})
        r = self.offline(self.root/'state', requests)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = [json.loads(x) for x in r.stdout.splitlines()]
        self.assertIn('result', rows[0])
        self.assertTrue(all(x['error']['message'] == 'offline_mode_operation_disabled' for x in rows[1:]))
        import sqlite3
        with sqlite3.connect(self.root/'state/probe.sqlite') as db:
            for table in ('weixin_accounts', 'weixin_inbox', 'weixin_outbox', 'subscriptions'):
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)

    def test_offline_refuses_existing_unmarked_state_without_creating_db(self):
        state = self.root/'state'; state.mkdir(); (state/'existing.txt').write_text('existing')
        self.assertNotEqual(self.offline(state, []).returncode, 0)
        self.assertFalse((state/'probe.sqlite').exists())
        self.assertEqual((state/'existing.txt').read_text(), 'existing')

    def test_offline_restart_keeps_separate_test_namespace(self):
        state = self.root/'state'
        for _ in range(2):
            self.assertEqual(self.offline(state, [{'jsonrpc':'2.0','id':1,'method':'ping'}]).returncode, 0)

    def test_install_never_overwrites_existing_destination(self):
        with self.assertRaisesRegex(ValueError, 'destination_exists'):
            self.install.install(ROOT, self.root)

    def test_install_refuses_recursive_destination(self):
        with self.assertRaisesRegex(ValueError, 'outside_source'):
            self.install.install(ROOT, ROOT/'nested-install')

    def test_source_copy_excludes_runtime_credentials_and_cache(self):
        for name in ('probe/ok.py', 'runtime/secrets/runtime.key', 'probe/__pycache__/cached.pyc'):
            p=self.root/name; p.parent.mkdir(parents=True,exist_ok=True); p.write_text('fixture')
        self.assertEqual([str(p.relative_to(self.root)) for p in self.install.source_files(self.root)], ['probe/ok.py'])

    def test_source_copy_preserves_public_manifest_and_only_bundled_skill(self):
        expected = {'.gitignore', 'PUBLIC-BUNDLE-MANIFEST.json',
                    'skills/dot-wechat-bridge/SKILL.md',
                    'skills/dot-wechat-bridge/LICENSE',
                    'skills/dot-wechat-bridge/references/setup.md'}
        for name in expected | {'skills/unrelated/SKILL.md',
                                 'skills/dot-wechat-bridge/runtime/private.txt',
                                 'skills/dot-wechat-bridge/__pycache__/cached.pyc'}:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('synthetic packaging fixture')
        copied = {str(p.relative_to(self.root)) for p in self.install.source_files(self.root)}
        self.assertEqual(copied, expected)

    def test_activation_requires_exact_approval_and_isolation_before_any_login(self):
        (self.root/'runtime').mkdir()
        (self.root/'runtime/setup-approval.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'isolation_unconfirmed'):
            self.install.activation_prerequisites(self.root)

    def test_profile_change_invalidates_activation(self):
        (self.root/'runtime').mkdir()
        (self.root/'runtime/profile.yaml').write_text('fixture')
        approval={'isolation_mode':'old_writer_fenced','approval_reference':'fixture-approval',
                  'isolation_evidence_reference':'fixture-stop-receipt','profile_sha256':'0'*64}
        (self.root/'runtime/setup-approval.json').write_text(json.dumps(approval))
        with self.assertRaisesRegex(ValueError, 'profile_mismatch'):
            self.install.activation_prerequisites(self.root)

    def test_launch_agent_is_bound_to_exact_installation_without_secret_arguments(self):
        a=self.install.launch_agent_spec(self.root/'a')
        b=self.install.launch_agent_spec(self.root/'b')
        self.assertNotEqual(a['Label'], b['Label'])
        self.assertEqual(a['ProgramArguments'][-1], str(self.root/'a'))
        self.assertEqual(a['KeepAlive'], {'SuccessfulExit':False})
        self.assertNotIn('runtime.key', json.dumps(a))

    def test_missing_binding_blocks_before_any_process_or_launchagent_mutation(self):
        with patch.object(self.install, 'activation_prerequisites', return_value={'startup_mode':'launch_agent'}), \
             patch.object(self.install, 'run') as run, patch.object(self.install.os, 'execv') as execute:
            with self.assertRaisesRegex(ValueError, 'scan_not_confirmed'):
                self.install.start(self.root, register=True)
            run.assert_not_called(); execute.assert_not_called()

    def test_launch_agent_existing_file_is_not_overwritten_or_bootstrapped(self):
        directory=self.root/'Library/LaunchAgents'; directory.mkdir(parents=True)
        spec=self.install.launch_agent_spec(self.root/'installation')
        target=directory/(spec['Label']+'.plist'); target.write_text('existing')
        with patch.object(self.install, 'activation_prerequisites', return_value={'startup_mode':'launch_agent','isolation_mode':'no_existing_writer_verified'}), \
             patch.object(self.install, 'require_owner_binding'), \
             patch.object(self.install.Path, 'home', return_value=self.root), \
             patch.object(self.install.sys, 'platform', 'darwin'), patch.object(self.install, 'run') as run:
            with self.assertRaises(FileExistsError):
                self.install.start(self.root/'installation', register=True)
            run.assert_not_called()
            self.assertEqual(target.read_text(), 'existing')


class ScanSessionTests(unittest.TestCase):
    def setUp(self):
        self.helper = module('owner-wechat-login.py')

    def response(self, data, code=0):
        return subprocess.CompletedProcess([], code, stdout=json.dumps(data), stderr='')

    def test_scan_confirmation_returns_ready_once(self):
        replies=[self.response({'status':'wait'}), self.response({}),
                 self.response({'status':'scaned'}), self.response({'status':'confirmed'})]
        with patch.object(self.helper, 'run_private', side_effect=replies) as run, \
             patch.object(self.helper.sys, 'platform', 'linux'):
            self.assertEqual(self.helper.connect_session(Path('/synthetic'), clock=lambda:0, pause=lambda _:None), 0)
            self.assertEqual(run.call_count, 4)

    def test_expired_scan_does_not_regenerate_qr_or_start(self):
        with patch.object(self.helper, 'run_private', side_effect=[self.response({'status':'wait'}),self.response({}),self.response({'status':'expired'})]) as run, \
             patch.object(self.helper.sys, 'platform', 'linux'):
            self.assertEqual(self.helper.connect_session(Path('/synthetic'), clock=lambda:0,pause=lambda _:None), 1)
            self.assertEqual(run.call_count, 3)

    def test_ambiguous_scan_operation_is_not_retried(self):
        with patch.object(self.helper, 'run_private', side_effect=subprocess.TimeoutExpired('synthetic', 1)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.helper.connect_session(Path('/synthetic'))
            self.assertEqual(run.call_count, 1)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.install = module('install-project.py')

    def file(self, relative, value, mode=0o600):
        path = self.root/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        path.chmod(mode)
        return path

    def ready_files(self):
        self.file('README.md', 'synthetic installed source')
        self.file('requirements.lock', (ROOT/'requirements.lock').read_text())
        self.file('INSTALL-SOURCE.json', json.dumps({'files': [
            {'path': relative, 'sha256': hashlib.sha256((self.root/relative).read_bytes()).hexdigest()}
            for relative in ('README.md', 'requirements.lock')]}))
        for name in ('runtime/profile.yaml', 'runtime/secrets/runtime.key', 'runtime/setup-approval.json'):
            self.file(name, 'synthetic-secret-fixture-' + '0'*32)
        self.file('bin/tunnel-client', 'synthetic non-client executable placeholder', 0o700)

    def binding(self, status='ready', last_poll=None):
        path = self.root/'runtime/state/probe.sqlite'
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE weixin_accounts(status TEXT, last_poll, token TEXT, owner TEXT)')
            db.execute('INSERT INTO weixin_accounts VALUES(?,?,?,?)',
                       (status, last_poll, 'synthetic-secret-fixture-'+'0'*32, 'synthetic-owner'))
        return path

    def distribution(self, name):
        versions = dict(self.install.re.findall(r'^([A-Za-z0-9_-]+)==([^\s]+)$', (ROOT/'requirements.lock').read_text(), self.install.re.M))
        return SimpleNamespace(version=versions[name], locate_file=lambda _: self.root/'.venv/lib/python3.11/site-packages')

    def test_preflight_missing_setup_aggregates_all_local_gaps_without_writes(self):
        with patch.object(self.install.subprocess, 'run', side_effect=AssertionError('no process')), \
             patch.object(self.install.os, 'execv', side_effect=AssertionError('no client')), \
             patch('socket.create_connection', side_effect=AssertionError('no network')):
            result = self.install.preflight(self.root)
        self.assertTrue(result['read_only'])
        self.assertFalse(result['production_started'])
        self.assertFalse(result['startup_authorized'])
        self.assertTrue({'profile_not_ready', 'runtime_key_not_ready', 'client_not_ready', 'setup_approval_not_ready'}.issubset(result['local_blockers']))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_connection_check_does_not_require_or_read_history(self):
        self.binding()
        with patch.object(self.install, 'require_state_continuity', side_effect=AssertionError('no history')):
            result = self.install.preflight(self.root, scope='connection_check')
        self.assertFalse(result['history']['required_for_this_scope'])
        self.assertEqual(result['history']['status'], 'out_of_scope_not_read')
        self.assertEqual(result['single_fresh_message_mode'], 'not_installed')
        self.assertEqual(result['next_binding_action'], 'preserve_existing_binding')

    def test_ready_binding_preserved_without_credentials_or_private_config_reads(self):
        self.ready_files(); database = self.binding(last_poll=1)
        old_bytes = database.read_bytes()
        original_read = Path.read_text
        def read_text(path, *args, **kwargs):
            if 'runtime' in path.parts:
                raise AssertionError('no private file contents')
            return original_read(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read_text), \
             patch.object(self.install.sys, 'prefix', str(self.root/'.venv')), \
             patch.object(self.install.importlib.metadata, 'distribution', side_effect=self.distribution):
            result = self.install.preflight(self.root)
        self.assertEqual(result['local_blockers'], [])
        self.assertEqual(result['binding']['status'], 'confirmed_locally')
        self.assertFalse(result['binding']['rescan_recommended'])
        self.assertEqual(database.read_bytes(), old_bytes)
        self.assertNotIn('synthetic-owner', json.dumps(result))
        self.assertNotIn('synthetic-secret-fixture', json.dumps(result))
        self.assertEqual(result['external_checks']['client_os_acceptance'], 'not_verified')
        self.assertEqual(result['external_checks']['phone_receipt'], 'not_tested')

    def test_malformed_binding_schema_does_not_create_or_migrate_tables(self):
        database = self.file('runtime/state/probe.sqlite', 'not a sqlite database')
        before = database.read_bytes()
        self.assertEqual(self.install.preflight_binding(self.root)['status'], 'binding_schema_or_read_unavailable')
        self.assertEqual(database.read_bytes(), before)

    def test_view_and_sensitive_columns_are_not_followed(self):
        path = self.root/'runtime/state/probe.sqlite'; path.parent.mkdir(parents=True)
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE private_data(secret TEXT)')
            db.execute('CREATE VIEW weixin_accounts AS SELECT secret AS status, secret AS last_poll FROM private_data')
        self.assertEqual(self.install.preflight_binding(self.root)['status'], 'binding_schema_unavailable')

    def test_binding_timestamp_text_is_redacted(self):
        self.binding(last_poll='synthetic-private-value')
        result = self.install.preflight_binding(self.root)
        self.assertIsNone(result['last_poll_utc'])
        self.assertNotIn('synthetic-private-value', json.dumps(result))

    def test_symlinked_database_and_configuration_are_refused(self):
        target = self.file('public.txt', 'synthetic')
        for relative in ('runtime/state/probe.sqlite', 'runtime/secrets/runtime.key'):
            path = self.root/relative; path.parent.mkdir(parents=True, exist_ok=True); path.symlink_to(target)
        result = self.install.preflight(self.root)
        self.assertEqual(result['binding']['status'], 'binding_database_symlink_refused')
        self.assertEqual(result['configuration_metadata']['runtime_key']['status'], 'symlink_refused')

    def test_wal_without_shm_refuses_read_that_could_create_sidecar(self):
        self.binding(); self.file('runtime/state/probe.sqlite-wal', 'synthetic')
        with patch.object(self.install.sqlite3, 'connect', side_effect=AssertionError('no opening WAL')):
            result = self.install.preflight_binding(self.root)
        self.assertEqual(result['status'], 'binding_wal_requires_existing_shm')
        self.assertFalse((self.root/'runtime/state/probe.sqlite-shm').exists())

    def test_manifest_cannot_request_a_runtime_secret_or_parent_path(self):
        for relative in ('runtime/secrets/runtime.key', '../outside.txt'):
            self.file('INSTALL-SOURCE.json', json.dumps({'files': [{'path': relative, 'sha256': '0'*64}]}))
            self.assertEqual(self.install.preflight_source(self.root)['status'], 'installation_receipt_invalid')

    def test_dependency_check_requires_actual_installation_interpreter(self):
        self.file('requirements.lock', (ROOT/'requirements.lock').read_text())
        with patch.object(self.install.importlib.metadata, 'distribution', side_effect=AssertionError('wrong interpreter')):
            self.assertEqual(self.install.preflight_dependencies(self.root)['status'], 'use_installation_python_to_verify')

    def test_client_presence_and_approval_presence_never_grant_startup(self):
        self.ready_files(); self.binding()
        result = self.install.preflight(self.root, scope='production')
        self.assertFalse(result['startup_authorized'])
        self.assertEqual(result['external_checks']['writer_isolation'], 'not_verified')
        self.assertEqual(result['external_checks']['platform_authorization'], 'not_verified')

    def test_connect_returns_aggregate_before_any_subprocess_or_scan(self):
        self.binding()
        with patch.object(self.install, 'check', side_effect=AssertionError('no check subprocess')), \
             patch.object(self.install, 'run', side_effect=AssertionError('no QR')), \
             patch.object(self.install, 'start', side_effect=AssertionError('no activation')):
            result = self.install.connect(self.root)
        self.assertEqual(result['status'], 'setup_incomplete')
        self.assertGreaterEqual(len(result['local_blockers']), 4)
        self.assertEqual(result['next_binding_action'], 'preserve_existing_binding')

    def test_connect_preserves_confirmed_binding_without_qr_helper(self):
        diagnosis = {'local_blockers': [], 'binding': {'status': 'confirmed_locally'}}
        with patch.object(self.install, 'preflight', return_value=diagnosis), \
             patch.object(self.install, 'check'), patch.object(self.install, 'activation_prerequisites', return_value={'startup_mode': 'foreground'}), \
             patch.object(self.install, 'require_owner_binding'), patch.object(self.install, 'require_state_continuity', side_effect=ValueError('retained_state_handoff_required')), \
             patch.object(self.install, 'run') as run, patch.object(self.install, 'start') as start:
            result = self.install.connect(self.root)
        self.assertEqual(result['status'], 'bound_but_not_started')
        run.assert_not_called(); start.assert_not_called()

    def test_connect_pre_scan_activation_error_keeps_aggregate_diagnosis(self):
        diagnosis = {'local_blockers': [], 'binding': {'status': 'confirmed_locally'}, 'read_only': True}
        with patch.object(self.install, 'preflight', return_value=diagnosis), \
             patch.object(self.install, 'activation_prerequisites', side_effect=ValueError('approved_profile_mismatch')), \
             patch.object(self.install, 'check') as check, patch.object(self.install, 'run') as run:
            result = self.install.connect(self.root)
        self.assertEqual(result['activation_blocker'], 'approved_profile_mismatch')
        self.assertEqual(result['status'], 'setup_incomplete')
        check.assert_not_called(); run.assert_not_called()

    def diagnostic_files(self, register=True):
        for name in self.install.FRESH_MESSAGE_FILES:
            self.file(name, 'synthetic diagnostic source, never executed')
        if register:
            path = self.root/'INSTALL-SOURCE.json'
            receipt = json.loads(path.read_text())
            receipt['files'].extend({'path': name, 'sha256': hashlib.sha256((self.root/name).read_bytes()).hexdigest()}
                                    for name in self.install.FRESH_MESSAGE_FILES)
            path.write_text(json.dumps(receipt))

    def transport_preflight(self):
        with patch.object(self.install.sys, 'prefix', str(self.root/'.venv')), \
             patch.object(self.install.importlib.metadata, 'distribution', side_effect=self.distribution), \
             patch.object(self.install, 'require_state_continuity', side_effect=AssertionError('no history')), \
             patch.object(self.install.subprocess, 'run', side_effect=AssertionError('no process')):
            return self.install.preflight(self.root, scope='weixin_transport_check')

    def test_weixin_transport_scope_does_not_need_mcp_files_or_history(self):
        self.ready_files(); self.binding(); self.diagnostic_files()
        for name in ('runtime/profile.yaml', 'runtime/secrets/runtime.key', 'runtime/setup-approval.json', 'bin/tunnel-client'):
            (self.root/name).unlink()
        result = self.transport_preflight()
        self.assertEqual(result['local_blockers'], [])
        self.assertTrue(all(item['scope_requirement'] == 'not_required_for_this_scope'
                            for item in result['configuration_metadata'].values()))
        self.assertFalse(result['history']['required_for_this_scope'])
        self.assertEqual(result['single_fresh_message_mode'], 'available_requires_operator_verification')
        self.assertEqual(result['external_checks']['writer_isolation'], 'not_verified')
        self.assertEqual(result['external_checks']['test_round_authorization'], 'not_verified')
        self.assertFalse(result['startup_authorized'])

    def test_weixin_transport_requires_exactly_one_ready_binding(self):
        self.ready_files(); self.diagnostic_files()
        self.assertIn('unique_ready_binding_required', self.transport_preflight()['local_blockers'])
        path = self.binding()
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO weixin_accounts(status) VALUES('ready')")
        self.assertIn('unique_ready_binding_required', self.transport_preflight()['local_blockers'])

    def test_diagnostic_files_must_be_covered_by_verified_install_receipt(self):
        self.ready_files(); self.binding(); self.diagnostic_files(register=False)
        result = self.transport_preflight()
        self.assertEqual(result['single_fresh_message_mode'], 'installed_but_unverified')
        self.assertIn('single_message_diagnostic_not_verified', result['local_blockers'])

    def test_diagnostic_mismatch_or_symlink_does_not_enable_transport_check(self):
        self.ready_files(); self.binding(); self.diagnostic_files()
        module_path = self.root/self.install.FRESH_MESSAGE_FILES[0]
        module_path.write_text('modified unverified module')
        result = self.transport_preflight()
        self.assertEqual(result['single_fresh_message_mode'], 'installed_but_unverified')
        self.assertIn('installation_integrity_unverified', result['local_blockers'])
        module_path.unlink(); module_path.symlink_to(self.root/'README.md')
        result = self.transport_preflight()
        self.assertEqual(result['single_fresh_message_mode'], 'not_installed')
        self.assertIn('single_message_diagnostic_not_verified', result['local_blockers'])

    def test_weixin_scope_does_not_relax_mcp_or_production_checks(self):
        self.ready_files(); self.binding(); self.diagnostic_files()
        (self.root/'bin/tunnel-client').unlink()
        with patch.object(self.install.sys, 'prefix', str(self.root/'.venv')), \
             patch.object(self.install.importlib.metadata, 'distribution', side_effect=self.distribution):
            for scope in ('connection_check', 'production'):
                result = self.install.preflight(self.root, scope=scope)
                self.assertIn('client_not_ready', result['local_blockers'])
                self.assertFalse(result['startup_authorized'])


if __name__ == '__main__':
    unittest.main()
