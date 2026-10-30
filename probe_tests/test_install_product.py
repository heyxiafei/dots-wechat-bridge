import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
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


if __name__ == '__main__':
    unittest.main()
