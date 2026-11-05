"""Offline cache recovery: every transport and provider is a local fixture."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from .fixtures import OPERATOR
from unittest import mock

from .test_monitor import T, m
from .test_dual_producer import EID, source_event, source_payload


class CacheRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aihot-cache-recovery-')
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        m.toggle(self.data, True, now=T)
        for target, name in ((m.urllib.request.OpenerDirector, 'open'),
                             (subprocess, 'Popen'), (os, 'system')):
            guard = mock.patch.object(target, name, side_effect=AssertionError(
                'real network/provider/process forbidden in cache recovery tests'))
            guard.start()
            self.addCleanup(guard.stop)
        m.notification_enable(self.data, now=T)
        m.dual_toggle(self.data, True, now=T)
        self.poll(T, source_payload(T))
        self.body = source_payload(T + 600, [source_event()])
        self.poll(T + 600, self.body)

    def read(self, name):
        return json.loads((self.data / name).read_text())

    def poll(self, now, body=None, code=200, transport=None):
        transport = transport or mock.Mock(return_value=(code, {'ETag': '"same"'}, body))
        result = m.poll(self.data, now=now, transport=transport)
        self.assertFalse(result['dispatch_enabled'])
        return result

    def cached_server(self, body):
        # Same representation ETag can survive a transient failure. A 304 has
        # no body with which the two local notification ledgers can recover.
        return mock.Mock(side_effect=lambda etag:
                         (304, {}, None) if etag is not None
                         else (200, {'ETag': '"same"'}, body))

    def assert_healthy(self):
        self.assertEqual(self.read('notifications.json')['source_health'], 'healthy')
        self.assertTrue(self.read('dual-notifications.json')['healthy'])

    def test_http_failure_recovers_with_full_body_at_next_original_retry(self):
        before = copy.deepcopy(self.read('dual-notifications.json')['entries'])
        result = self.poll(T + 1200, code=503)
        self.assertEqual(result['retry_in_seconds'], 600)
        self.assertFalse(self.read('dual-notifications.json')['healthy'])
        server = self.cached_server(self.body)
        result = self.poll(T + 1800, transport=server)
        self.assertEqual(result['result'], 'snapshot')
        server.assert_called_once_with(None)
        self.assert_healthy()
        self.assertEqual(self.read('dual-notifications.json')['entries'], before)
        self.assertEqual(self.read('state.json')['snapshot_checked_at'], T + 600)

    def test_unhealthy_source_recovers_without_rearm_or_new_baseline(self):
        unhealthy = copy.deepcopy(self.body)
        unhealthy['monitor']['status'] = 'degraded'
        result = self.poll(T + 1200, unhealthy)
        self.assertEqual(result['result'], 'monitor_not_healthy')
        server = self.cached_server(self.body)
        result = self.poll(T + 1800, transport=server)
        self.assertEqual(result['result'], 'snapshot')
        server.assert_called_once_with(None)
        self.assert_healthy()
        self.assertTrue(self.read('dual-notifications.json')['baseline_complete'])

    def test_local_dual_capture_failure_does_not_cache_successful_http_body(self):
        with mock.patch.object(m, 'dual_capture', side_effect=OSError('fixture failure')):
            result = self.poll(T + 1200, self.body)
        self.assertEqual(result['dual_capture'], 'blocked')
        server = self.cached_server(self.body)
        self.poll(T + 1800, transport=server)
        server.assert_called_once_with(None)
        self.assert_healthy()

    def test_local_notification_failure_does_not_cache_successful_http_body(self):
        with mock.patch.object(m, 'notification_sync', side_effect=OSError('fixture failure')):
            result = self.poll(T + 1200, self.body)
        self.assertEqual(result['notification_result'], 'local_or_source_blocked')
        server = self.cached_server(self.body)
        self.poll(T + 1800, transport=server)
        server.assert_called_once_with(None)
        self.assert_healthy()

    def test_source_observation_failure_requires_a_full_retry(self):
        with mock.patch.object(m, 'observe_source', side_effect=OSError('fixture failure')):
            result = self.poll(T + 1200, self.body)
        self.assertEqual(result['source_observation'], 'blocked')
        server = self.cached_server(self.body)
        self.poll(T + 1800, transport=server)
        server.assert_called_once_with(None)
        self.assertEqual(self.read('source-observations.json')['checked_at'], T + 600)

    def test_busy_dual_capture_retries_full_body(self):
        with mock.patch.object(m, 'dual_capture', return_value={'dual_capture': 'lock_busy'}):
            result = self.poll(T + 1200, self.body)
        self.assertEqual(result['dual_capture'], 'lock_busy')
        server = self.cached_server(self.body)
        self.poll(T + 1800, transport=server)
        server.assert_called_once_with(None)
        self.assert_healthy()

    def test_fresh_success_keeps_conditional_get_and_304_keeps_watermark(self):
        before = self.read('dual-notifications.json')
        server = self.cached_server(self.body)
        result = self.poll(T + 1200, transport=server)
        self.assertEqual(result['result'], 'not_modified')
        server.assert_called_once_with('"same"')
        self.assertEqual(self.read('state.json')['snapshot_checked_at'], T + 600)
        self.assertEqual(self.read('dual-notifications.json'), before)

    def test_unsolicited_304_cannot_recover_health_or_refresh_watermark(self):
        self.poll(T + 1200, code=503)
        server = mock.Mock(return_value=(304, {}, None))
        result = self.poll(T + 1800, transport=server)
        self.assertEqual(result['result'], '304_without_baseline')
        server.assert_called_once_with(None)
        self.assertEqual(self.read('state.json')['snapshot_checked_at'], T + 600)
        self.assertFalse(self.read('dual-notifications.json')['healthy'])
        self.assertEqual(self.read('state.json')['failures'], 2)

    def test_preupgrade_failed_state_with_cached_etag_is_revalidated(self):
        self.poll(T + 1200, code=503)
        # Compatibility fixture for state written by the previous producer.
        state = self.read('state.json')
        state['etag'] = '"same"'
        m.atomic_json(self.data / 'state.json', state)
        server = self.cached_server(self.body)
        result = self.poll(T + 1800, transport=server)
        self.assertEqual(result['result'], 'snapshot')
        server.assert_called_once_with(None)
        self.assert_healthy()

    def test_original_provider_ids_remain_query_only_after_recovery(self):
        relay = m.dual_package().Relay(self.data, operator_config=OPERATOR)
        original, provider_ids = {}, {'wechat': 'notice_' + 'a' * 32,
                                     'email': '1a00000000000001'}
        for channel in ('wechat', 'email'):
            original[channel] = relay.claim(EID, 'announced', channel, T + 601)
            self.assertEqual(original[channel]['result'], 'prepared')
            field = 'send_id' if channel == 'wechat' else 'message_id'
            relay.record(EID, 'announced', channel, {field: provider_ids[channel]}, T + 602)
        before = self.read('dual-notifications.json')['entries']
        self.poll(T + 1200, code=503)
        self.poll(T + 1800, transport=self.cached_server(self.body))
        self.assertEqual(self.read('dual-notifications.json')['entries'], before)
        for channel in ('wechat', 'email'):
            recovered = relay.claim(EID, 'announced', channel, T + 1801)
            self.assertTrue(recovered['query_only'])
            self.assertEqual(recovered['request_id'], original[channel]['request_id'])
            self.assertEqual(recovered['provider_id'], provider_ids[channel])
            self.assertNotIn('payload', recovered)

    def test_estimate_after_recovery_does_not_create_confirmation(self):
        self.poll(T + 1200, code=503)
        estimated = source_event()
        estimated['presentation']['status'] = 'likely_completed'
        body = source_payload(T + 1800, [estimated])
        result = self.poll(T + 1800, transport=self.cached_server(body))
        self.assertEqual(result['source_estimated_unconfirmed'], 1)
        ledger = self.read('dual-notifications.json')
        self.assertNotIn(EID + '/confirmed', ledger['entries'])
        self.assertTrue(all(c['state'] == 'suppressed'
                            for c in ledger['entries'][EID + '/announced']['channels'].values()))

    def test_real_confirmation_after_recovery_is_a_distinct_pending_stage(self):
        self.poll(T + 1200, code=503)
        body = source_payload(T + 1800, [source_event(confirmed=T + 1500)])
        result = self.poll(T + 1800, transport=self.cached_server(body))
        self.assertEqual(result['source_confirmed'], 1)
        ledger = self.read('dual-notifications.json')
        self.assertTrue(all(c['state'] == 'pending'
                            for c in ledger['entries'][EID + '/confirmed']['channels'].values()))
        self.assertTrue(all(c['state'] == 'suppressed'
                            for c in ledger['entries'][EID + '/announced']['channels'].values()))

    def test_backoff_and_off_still_make_no_network_request(self):
        self.poll(T + 1200, code=503)
        no_network = mock.Mock(side_effect=AssertionError('unexpected network'))
        self.assertEqual(self.poll(T + 1500, transport=no_network)['result'], 'backoff')
        m.toggle(self.data, False)
        self.assertEqual(self.poll(T + 1800, transport=no_network)['result'], 'paused')
        no_network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
