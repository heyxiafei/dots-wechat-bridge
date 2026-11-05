"""Offline private-storage regressions; every path is disposable test data."""
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import Mock

from integrations.aihot import monitor
from probe_tests.test_aihot_producer import OfflineProducerSuite


NOW = 1800000000.0


class ProducerStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='aihot-private-storage-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.data = self.root / 'data'
        self.data.mkdir(mode=0o700)
        self.outside = self.root / 'outside'
        self.outside.mkdir(mode=0o700)

    def target(self, name, contents=b'outside fixture\n', mode=0o600):
        path = self.outside / name
        path.write_bytes(contents)
        path.chmod(mode)
        return path

    @staticmethod
    def snapshot(path):
        metadata = path.stat()
        return path.read_bytes(), stat.S_IMODE(metadata.st_mode), metadata.st_nlink

    @staticmethod
    def acquire(path):
        with monitor.lock(path) as acquired:
            return acquired

    @staticmethod
    def rejected(operation):
        try:
            outcome = operation()
        except (monitor.Invalid, OSError):
            return True
        return isinstance(outcome, dict) and outcome.get('result') == 'local_state_error'

    def test_existing_public_directory_is_rejected_without_chmod_or_files(self):
        self.data.chmod(0o755)
        blocked = self.rejected(lambda: self.acquire(self.data))
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o755)
        self.assertEqual(list(self.data.iterdir()), [])
        self.assertTrue(blocked)

    def test_symlink_data_directory_is_rejected_without_touching_target(self):
        link = self.root / 'data-link'
        link.symlink_to(self.outside, target_is_directory=True)
        blocked = self.rejected(lambda: self.acquire(link))
        self.assertTrue(link.is_symlink())
        self.assertEqual(list(self.outside.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.outside.stat().st_mode), 0o700)
        self.assertTrue(blocked)

    def test_symlink_lock_does_not_modify_external_contents_or_mode(self):
        target = self.target('lock-target', mode=0o640)
        before = self.snapshot(target)
        (self.data / 'monitor.lock').symlink_to(target)
        blocked = self.rejected(lambda: self.acquire(self.data))
        self.assertEqual(self.snapshot(target), before)
        self.assertTrue((self.data / 'monitor.lock').is_symlink())
        self.assertTrue(blocked)

    def test_symlink_log_does_not_append_to_external_file(self):
        target = self.target('log-target')
        before = self.snapshot(target)
        (self.data / 'monitor.jsonl').symlink_to(target)
        blocked = self.rejected(lambda: monitor.log(self.data, NOW, 'fixture'))
        self.assertEqual(self.snapshot(target), before)
        self.assertTrue((self.data / 'monitor.jsonl').is_symlink())
        self.assertTrue(blocked)

    def test_symlink_state_is_rejected_and_preserved(self):
        target = self.target('state-target', json.dumps(monitor.new_state(NOW)).encode())
        before = self.snapshot(target)
        (self.data / 'state.json').symlink_to(target)
        blocked = self.rejected(lambda: monitor.load_state(self.data, NOW))
        self.assertEqual(self.snapshot(target), before)
        self.assertTrue((self.data / 'state.json').is_symlink())
        self.assertTrue(blocked)

    def test_symlink_control_is_rejected_before_any_transport(self):
        target = self.target('control-target', b'{"enabled": false, "mode": "monitor-only"}')
        before = self.snapshot(target)
        (self.data / 'control.json').symlink_to(target)
        transport = Mock(side_effect=AssertionError('transport must not run'))
        blocked = self.rejected(lambda: monitor.poll(self.data, now=NOW, transport=transport))
        transport.assert_not_called()
        self.assertEqual(self.snapshot(target), before)
        self.assertTrue((self.data / 'control.json').is_symlink())
        self.assertTrue(blocked)

    def test_hardlinked_lock_is_rejected_without_chmod(self):
        target = self.target('hard-lock-target')
        os.link(target, self.data / 'monitor.lock')
        before = self.snapshot(target)
        blocked = self.rejected(lambda: self.acquire(self.data))
        self.assertEqual(self.snapshot(target), before)
        self.assertTrue(blocked)

    def test_hardlinked_log_is_rejected_without_append(self):
        target = self.target('hard-log-target')
        os.link(target, self.data / 'monitor.jsonl')
        before = self.snapshot(target)
        blocked = self.rejected(lambda: monitor.log(self.data, NOW, 'fixture'))
        self.assertEqual(self.snapshot(target), before)
        self.assertTrue(blocked)

    def test_hardlinked_state_is_rejected(self):
        target = self.target('hard-state-target', json.dumps(monitor.new_state(NOW)).encode())
        os.link(target, self.data / 'state.json')
        before = self.snapshot(target)
        blocked = self.rejected(lambda: monitor.load_state(self.data, NOW))
        self.assertEqual(self.snapshot(target), before)
        self.assertTrue(blocked)

    def test_atomic_json_rejects_linked_destinations_without_replacement(self):
        for link_kind in ('symlink', 'hardlink'):
            with self.subTest(link_kind=link_kind):
                target = self.target('atomic-' + link_kind)
                destination = self.data / ('atomic-' + link_kind + '.json')
                if link_kind == 'symlink':
                    destination.symlink_to(target)
                else:
                    os.link(target, destination)
                before = self.snapshot(target)
                blocked = self.rejected(lambda: monitor.atomic_json(destination, {'fixture': True}))
                self.assertEqual(self.snapshot(target), before)
                self.assertEqual(destination.is_symlink(), link_kind == 'symlink')
                self.assertTrue(blocked)

    def test_log_rotation_rejects_linked_previous_log_without_replacement(self):
        for link_kind in ('symlink', 'hardlink'):
            with self.subTest(link_kind=link_kind):
                case = self.data / link_kind
                case.mkdir(mode=0o700)
                log = case / 'monitor.jsonl'
                original = b'x' * (2 * 1024 * 1024 + 1)
                log.write_bytes(original)
                log.chmod(0o600)
                target = self.target('rotation-' + link_kind)
                destination = case / 'monitor.previous.jsonl'
                if link_kind == 'symlink':
                    destination.symlink_to(target)
                else:
                    os.link(target, destination)
                before = self.snapshot(target)
                blocked = self.rejected(lambda: monitor.log(case, NOW, 'fixture'))
                self.assertEqual(self.snapshot(target), before)
                self.assertEqual(destination.is_symlink(), link_kind == 'symlink')
                self.assertEqual(log.read_bytes(), original)
                self.assertTrue(blocked)

    def test_private_directory_and_files_keep_poll_disabled_by_default(self):
        monitor.atomic_json(self.data / 'state.json', monitor.new_state(NOW))
        self.assertEqual(stat.S_IMODE((self.data / 'state.json').stat().st_mode), 0o600)
        transport = Mock(side_effect=AssertionError('default-off must not access network'))
        result = monitor.poll(self.data, now=NOW, transport=transport)
        self.assertEqual(result['result'], 'paused')
        self.assertFalse(result['dispatch_enabled'])
        transport.assert_not_called()
        self.assertFalse((self.data / 'control.json').exists())
        self.assertEqual(stat.S_IMODE(self.data.stat().st_mode), 0o700)
        for path in self.data.iterdir():
            self.assertFalse(path.is_symlink())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


def load_tests(loader, tests, pattern):
    return OfflineProducerSuite(tests)
