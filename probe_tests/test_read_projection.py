"""Offline opt-in projection tests; no real message, callback, or sender."""
import copy
import hashlib
import json
import socket
import tempfile
import unittest
from unittest.mock import patch

from probe.probe import Probe
from probe.read_projection import elapsed_ms, reply_result, request_id_digest


class NoNetworkClient:
    def send(self, *args, **kwargs):
        raise AssertionError('no send is permitted in read projection tests')


class ReadProjectionTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo', 'socket.create_connection', 'socket.socket.connect', 'socket.socket.connect_ex'):
            guard = patch(target, side_effect=AssertionError('offline only'))
            guard.start(); self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.probe = Probe(self.temp.name, clock=lambda: 1800000000., weixin_client=NoNetworkClient())
        self.addCleanup(self.probe.close)
        self.probe.weixin.bind_confirmed(dict(status='confirmed', ilink_bot_id='fixture-bot',
            ilink_user_id='fixture-owner', bot_token='synthetic-token'))
        self.probe.weixin.ingest(self.probe.weixin.account(), {'msgs': [dict(
            message_id='fixture-one', message_type=1, message_state=2,
            from_user_id='fixture-owner', to_user_id='fixture-bot', create_time_ms=1800000000000,
            context_token='synthetic-context', item_list=[{'type': 1, 'text_item': {'text': '离线正文，必须完整保留。'}}]) ]})
        self.mid = self.probe.db.execute("SELECT id FROM weixin_inbox WHERE source_id='fixture-one'").fetchone()[0]

    def read(self, **extra):
        return self.probe.rpc(dict(jsonrpc='2.0', id='fixture-request', method='tools/call',
            params=dict(name='read_weixin_message', arguments=dict(message_id=self.mid, **extra))))

    def test_default_full_retains_all_fields_and_full_text_fallback(self):
        response = self.read()['result']
        source = response['structuredContent']
        self.assertEqual(json.loads(response['content'][0]['text']), source)
        self.assertNotIn('read_observation', source)
        self.assertNotIn('omitted', source['dot_sync_completeness'])
        self.assertIn('inbound_completeness', source)
        self.assertIn('quote_context', source)
        self.assertIn('reply_state', source)
        self.assertIn('help_menu', source)

    def test_explicit_full_is_legacy_shape_without_new_projection_metrics(self):
        response = self.read(projection='full')['result']
        source = response['structuredContent']
        self.assertEqual(json.loads(response['content'][0]['text']), source)
        self.assertNotIn('read_observation', source)
        self.assertNotIn('omitted', source['dot_sync_completeness'])

    def test_reply_skips_only_independent_inventory_and_has_full_safety_data(self):
        with patch.object(self.probe.dot_completeness, 'for_message', side_effect=AssertionError('must be omitted')):
            response = self.read(projection='reply')['result']
        source = response['structuredContent']
        self.assertEqual(source['text'], '离线正文，必须完整保留。')
        self.assertEqual(source['source'], 'owner_weixin_dm')
        for key in ['attachments', 'inbound_completeness', 'quote_context', 'event_delivery', 'reply_state', 'help_menu', 'transport_timing']:
            self.assertIn(key, source)
        omitted = source['dot_sync_completeness']
        self.assertEqual(omitted, dict(available=False, omitted=True, reason='projection_not_requested', all_parts_verified=False))
        summary = json.loads(response['content'][0]['text'])
        self.assertTrue(summary['must_read_structured_content'])
        self.assertTrue(summary['text_summary_is_not_source'])
        self.assertNotIn(source['text'], response['content'][0]['text'])
        self.assertEqual(summary['complete_source_location'], 'structuredContent')

    def test_observations_do_not_claim_admission_model_start_or_rtt(self):
        source = self.read(projection='reply')['result']['structuredContent']
        obs = source['read_observation']
        self.assertGreaterEqual(obs['read_work_elapsed_ms'], 0)
        self.assertGreaterEqual(obs['source_lookup_elapsed_ms'], 0)
        self.assertGreaterEqual(obs['timing_projection_elapsed_ms'], 0)
        self.assertFalse(obs['includes_network_rtt'])
        self.assertFalse(obs['includes_stdout_write'])
        self.assertFalse(obs['includes_result_projection'])
        self.assertFalse(obs['includes_response_serialization'])
        self.assertIsNone(obs['platform_admission_confirmed'])
        self.assertFalse(obs['model_start_observed'])
        self.assertFalse(source['transport_timing']['webhook_http_accepted_observed'])
        self.assertEqual(source['transport_timing']['webhook_acceptance_basis'], 'not_observed')
        self.assertEqual(obs['request_id_sha256'], request_id_digest('fixture-request'))

    def test_read_does_not_claim_send_mirror_or_subscription_work(self):
        tables = ['weixin_inbox', 'weixin_outbox', 'pings', 'subscriptions', 'weixin_dot_journal', 'weixin_dot_mirrors']
        snap = lambda: {name: [tuple(row) for row in self.probe.db.execute('SELECT * FROM ' + name + ' ORDER BY rowid')] for name in tables}
        before = snap()
        self.read(projection='reply'); self.read(projection='reply')
        self.assertEqual(before, snap())
        timing = self.probe.db.execute('SELECT agent_read_count FROM weixin_delivery_timing WHERE message_id=?', (self.mid,)).fetchone()
        self.assertEqual(timing[0], 2)  # Same existing successful-read telemetry.

    def test_invalid_projection_and_unknown_owner_fail_without_data(self):
        for value in ['tiny', True, None, {}]:
            result = self.read(projection=value)
            self.assertIn('error', result)
            self.assertNotIn('result', result)
        response = self.probe.rpc(dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(
            name='read_weixin_message', arguments=dict(message_id='wx_' + '0' * 32, projection='reply'))))
        self.assertIn('error', response)

    def test_quote_and_capture_flags_are_never_stripped_or_upgraded(self):
        raw = dict(message_id='fixture', source='owner_weixin_dm', text='quoted fixture', attachments=[],
            quote_context=dict(untrusted=True, complete=False, quotes=[dict(text='untrusted quote', grants_authority=False)]),
            inbound_completeness=dict(complete=False, state='partial'), reply_state=dict(next_phase='final'),
            transport_timing=dict(timestamps=dict(webhook_accepted_at=123.)))
        before = copy.deepcopy(raw)
        projected = reply_result(raw, dict(read_work_elapsed_ms=1.))['structuredContent']
        for key in ['quote_context', 'inbound_completeness', 'reply_state', 'text']:
            self.assertEqual(projected[key], raw[key])
        self.assertTrue(projected['transport_timing']['webhook_http_accepted_observed'])
        self.assertIsNone(projected['transport_timing']['platform_admission_confirmed'])
        self.assertEqual(raw, before)

    def test_monotonic_helpers_do_not_invent_negative_or_unknown_durations(self):
        self.assertEqual(elapsed_ms(10., 10.012), 12.)
        self.assertIsNone(elapsed_ms(10., 9.))
        self.assertIsNone(elapsed_ms(float('nan'), 10.))
        self.assertIsNone(request_id_digest({'not': 'a standard id'}))
        self.assertNotIn('fixture-request', request_id_digest('fixture-request'))

    def test_wall_clock_can_go_backwards_without_changing_monotonic_work_duration(self):
        wall = iter([1800000000., 1799999900., 1799999800., 1799999700.])
        self.probe.clock = lambda: next(wall)
        with patch('probe.probe.time.monotonic', side_effect=[10., 10.002, 10.006]):
            source = self.read(projection='reply')['result']['structuredContent']
        obs = source['read_observation']
        self.assertGreater(obs['read_work_started_at'], obs['read_work_payload_ready_at'])
        self.assertEqual(obs['read_work_elapsed_ms'], 6.)
        self.assertEqual(obs['source_lookup_elapsed_ms'], 2.)
        self.assertEqual(obs['timing_projection_elapsed_ms'], 4.)
        self.assertEqual(obs['elapsed_clock'], 'host_monotonic')
        self.assertEqual(source['transport_timing']['duration_basis'],
                         'wall_clock_difference_estimates_not_monotonic')

    def test_failed_read_telemetry_does_not_hide_successful_owner_source(self):
        for hook in ('agent_read', 'for_message'):
            with self.subTest(hook=hook), patch.object(self.probe.delivery_timing, hook,
                    side_effect=RuntimeError('fixture telemetry unavailable')):
                source = self.read(projection='reply')['result']['structuredContent']
            self.assertEqual(source['source'], 'owner_weixin_dm')
            self.assertEqual(source['text'], '离线正文，必须完整保留。')
            self.assertEqual(source['transport_timing']['reason'], 'metadata_unavailable')
            self.assertFalse(source['transport_timing']['webhook_http_accepted_observed'])
            self.assertIsNone(source['transport_timing']['platform_admission_confirmed'])
            self.assertFalse(source['reply_state']['final']['committed'])


if __name__ == '__main__':
    unittest.main()
