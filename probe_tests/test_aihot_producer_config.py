"""Default-off and explicit-state boundaries for the optional collector."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from integrations.aihot import monitor
from integrations.aihot.notice_relay.relay import Relay
from probe.aihot_notice_consumer.relay import Relay as ConsumerRelay
from probe_tests.test_aihot_producer import OfflineProducerSuite


class ProducerConfigurationTests(unittest.TestCase):
    def test_missing_control_never_calls_public_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with patch.object(monitor, 'fetch', side_effect=AssertionError('network forbidden')):
                transport = unittest.mock.Mock(side_effect=AssertionError('network forbidden'))
                result = monitor.poll(data, now=1000, transport=transport)
            self.assertEqual(result['result'], 'paused')
            transport.assert_not_called()
            self.assertEqual(monitor.load_state(data, 1000)['network_count'], 0)
            self.assertFalse((data / 'dual-notifications.json').exists())

    def test_explicit_off_stops_poll_without_erasing_seen_or_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            state = monitor.new_state(900)
            state['seen'] = {'fixture-event': {'first_seen_at': 901, 'disposition': 'baseline'}}
            monitor.atomic_json(data / 'state.json', state)
            monitor.toggle(data, True, now=1000)
            self.assertTrue(json.loads((data / 'control.json').read_text())['enabled'])
            self.assertFalse(monitor.dual_control(data)['capture_enabled'])
            monitor.toggle(data, False, now=1001)
            transport = unittest.mock.Mock(side_effect=AssertionError('network forbidden'))
            self.assertEqual(monitor.poll(data, now=1002, transport=transport)['result'], 'paused')
            transport.assert_not_called()
            self.assertEqual(monitor.load_state(data, 1002)['seen'], state['seen'])

    def test_cli_has_no_implicit_state_directory(self):
        with patch('sys.argv', ['aihot-monitor', 'poll']):
            with patch.object(monitor, 'poll') as poll:
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        monitor.main()
            self.assertEqual(raised.exception.code, 2)
            poll.assert_not_called()

    def test_capture_shares_canonical_ledger_and_no_private_task_defaults(self):
        self.assertIs(Relay, ConsumerRelay)
        self.assertIs(monitor.dual_package().Relay, ConsumerRelay)
        self.assertEqual(monitor.task_gate()['allowed_task_ids'], [])
        self.assertIsNone(monitor.task_gate()['task_id'])
        self.assertIsNone(monitor.task_gate()['proposed_runtime_cap_seconds'])
        self.assertFalse(monitor.task_gate()['runtime_cap_enforced'])

    def test_toggle_requires_literal_boolean(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            with self.assertRaises(monitor.Invalid):
                monitor.toggle(data, 1, now=1000)
            self.assertEqual(list(data.iterdir()), [])

    def test_test_entry_blocks_unmocked_network_calls(self):
        import socket
        import urllib.request
        self.assertIsInstance(socket.socket, unittest.mock.Mock)
        self.assertIsInstance(urllib.request.urlopen, unittest.mock.Mock)
        with self.assertRaisesRegex(AssertionError, 'network_forbidden'):
            socket.socket()
        with self.assertRaisesRegex(AssertionError, 'network_forbidden'):
            urllib.request.urlopen('https://example.invalid/')


def load_tests(loader, tests, pattern):
    return OfflineProducerSuite(loader.loadTestsFromTestCase(ProducerConfigurationTests))


if __name__ == '__main__':
    unittest.main()
