"""Offline regression cases for the Oct24 missed-notification diagnosis."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from .test_dual_producer import EID, source_event, source_payload, source_activity
from .test_monitor import T, iso, m


class SourceObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='aihot-observe-test-')
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)
        m.toggle(self.data, True, now=T)

    def observed(self, e, now=T + 600):
        body = source_payload(now, [e])
        return m.observe_source(self.data, body, now)

    def test_likely_completed_is_estimate_and_never_confirmation(self):
        e = source_event()
        e['presentation']['status'] = 'likely_completed'
        result = self.observed(e)
        row = m.source_observation_status(self.data)['events'][EID]
        self.assertEqual(row['classification'], 'estimated_completion_unconfirmed')
        self.assertEqual(row['source_status'], 'announced')
        self.assertIsNone(row['confirmed_at'])
        self.assertFalse(row['notification_eligible'])
        self.assertEqual(result['source_estimated_unconfirmed'], 1)
        self.assertFalse((self.data / 'dual-notifications.json').exists())

    def test_announced_with_verified_occurrence_cannot_create_announcement(self):
        e = source_event()
        e['occurredOn'] = '2026-10-18'
        views = m.notification_view(source_payload(T + 600, [e]), T + 600)
        self.assertFalse(views[EID]['eligible'])
        self.assertEqual(views[EID]['reason'], 'source_confirmation_conflict')

    def test_receipt_review_and_estimate_have_distinct_diagnostics(self):
        e = source_event(confirmed=T + 100)
        e['confirmationBasis'] = 'receipt_review'
        self.observed(e)
        row = m.source_observation_status(self.data)['events'][EID]
        self.assertFalse(row['notification_eligible'])
        self.assertEqual(row['confirmation_basis'], 'receipt_review')
        self.assertEqual(row['ineligibility_reason'], 'confirmation_not_source_post')

    def test_raw_confirmation_requires_matching_source_evidence(self):
        e = source_event(confirmed=T + 100)
        self.observed(e)
        self.assertEqual(m.source_observation_status(self.data)['events'][EID]['classification'], 'source_confirmed')
        body = source_payload(T + 1200, [e], activities=[])
        m.observe_source(self.data, body, T + 1200)
        self.assertEqual(m.source_observation_status(self.data)['events'][EID]['classification'], 'conflicting_or_unverified')

    def test_transition_time_survives_restart_and_duplicate_does_not_relog(self):
        e = source_event()
        self.observed(e)
        e['presentation']['status'] = 'likely_completed'
        self.observed(e, T + 1200)
        before = m.source_observation_status(self.data)
        self.observed(e, T + 1800)
        after = m.source_observation_status(self.data)
        self.assertEqual(after['events'][EID]['changed_at'], T + 1200)
        self.assertEqual(after['events'][EID]['first_observed_at'], T + 600)
        lines = [json.loads(s) for s in (self.data / 'monitor.jsonl').read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(before['events'][EID]['classification'], after['events'][EID]['classification'])

    def test_observation_does_not_store_source_prose_or_private_fields(self):
        e = source_event()
        e['title'] = 'UNTRUSTED_SECRET_OR_INSTRUCTION'
        e['presentation']['scopeLabel'] = e['title']
        self.observed(e)
        for p in self.data.iterdir():
            self.assertNotIn(e['title'], p.read_text())

    def test_observation_out_of_order_fails_without_replacing_evidence(self):
        self.observed(source_event(), T + 1200)
        old = (self.data / 'source-observations.json').read_bytes()
        with self.assertRaises(m.Invalid):
            self.observed(source_event(), T + 600)
        self.assertEqual(old, (self.data / 'source-observations.json').read_bytes())

    def test_history_and_estimate_changes_do_not_create_dual_notice(self):
        m.dual_toggle(self.data, True, now=T + 600)
        e = source_event()
        m.poll(self.data, now=T + 600, transport=lambda _: (200, {}, source_payload(T + 600, [e])))
        e['presentation']['status'] = 'likely_completed'
        result = m.poll(self.data, now=T + 1200, transport=lambda _: (200, {}, source_payload(T + 1200, [e])))
        self.assertEqual(result['source_estimated_unconfirmed'], 1)
        ledger = json.loads((self.data / 'dual-notifications.json').read_text())
        self.assertEqual(set(ledger['entries']), {EID + '/announced'})
        self.assertTrue(all(c['state'] == 'suppressed' for item in ledger['entries'].values() for c in item['channels'].values()))

    def test_poll_exposes_unimplemented_channels_even_with_zero_pending(self):
        m.dual_toggle(self.data, True, now=T)
        result = m.poll(self.data, now=T, transport=lambda _: (200, {}, source_payload(T)))
        self.assertFalse(result['sender_installed'])
        status = m.dual_status(self.data)
        self.assertEqual(status['routes']['wechat']['state'], 'adapter_not_installed')
        self.assertEqual(status['routes']['email']['state'], 'handoff_contract_missing')
        self.assertIn('rejected', status['channels']['email'])
        self.assertFalse(status['automatic_delivery_ready'])

    def test_local_relay_failure_has_specific_safe_diagnostic(self):
        m.dual_toggle(self.data, True, now=T)
        with mock.patch.object(m, 'dual_capture', side_effect=OSError('PRIVATE_CREDENTIAL_TEXT')):
            result = m.poll(self.data, now=T, transport=lambda _: (200, {}, source_payload(T)))
        self.assertEqual(result['dual_error'], 'local_io_error')
        self.assertNotIn('PRIVATE_CREDENTIAL_TEXT', (self.data / 'monitor.jsonl').read_text())

    def test_same_checked_display_deactivation_suppresses_without_false_conflict(self):
        m.notification_enable(self.data, now=T)
        m.dual_toggle(self.data, True, now=T)
        m.poll(self.data, now=T, transport=lambda _: (200, {}, source_payload(T)))
        e = source_event()
        body = source_payload(T + 600, [e])
        m.poll(self.data, now=T + 600, transport=lambda _: (200, {}, body))
        original = copy.deepcopy(body)
        e['presentation']['status'] = 'likely_completed'
        changed = copy.deepcopy(body)
        changed['events'] = [e]
        result = m.poll(self.data, now=T + 1200, transport=lambda _: (200, {}, changed))
        self.assertEqual(result['result'], 'snapshot')
        self.assertEqual(result['source_estimated_unconfirmed'], 1)
        ledger = json.loads((self.data / 'dual-notifications.json').read_text())
        self.assertTrue(all(c['state'] == 'suppressed' for item in ledger['entries'].values() for c in item['channels'].values()))
        # Same watermark must not restore source eligibility or manufacture a confirmation.
        result = m.poll(self.data, now=T + 1800, transport=lambda _: (200, {}, original))
        self.assertEqual(result['result'], 'same_timestamp_conflict')

    def test_same_checked_clock_expiry_can_only_stop(self):
        m.notification_enable(self.data, now=T)
        m.poll(self.data, now=T, transport=lambda _: (200, {}, source_payload(T)))
        e = source_event()
        e['schedule']['from'] = iso(T + 800)
        e['schedule']['through'] = iso(T + 1000)
        body = source_payload(T + 600, [e])
        m.poll(self.data, now=T + 600, transport=lambda _: (200, {}, body))
        result = m.poll(self.data, now=T + 1200, transport=lambda _: (200, {}, body))
        self.assertEqual(result['result'], 'snapshot')
        self.assertEqual(m.notification_load(self.data)['entries'][EID + '/announced']['state'], 'suppressed')

    def test_same_checked_sequential_estimates_stay_suppressed_without_false_conflicts(self):
        m.notification_enable(self.data, now=T)
        m.dual_toggle(self.data, True, now=T)
        m.poll(self.data, now=T, transport=lambda _: (200, {}, source_payload(T)))
        e = source_event()
        body = source_payload(T + 600, [e])
        m.poll(self.data, now=T + 600, transport=lambda _: (200, {}, body))
        for i, display in enumerate(('in_progress', 'expired_unconfirmed', 'likely_completed'), 2):
            e['presentation']['status'] = display
            result = m.poll(self.data, now=T + i * 600, transport=lambda _: (200, {}, body))
            self.assertEqual(result['notification_result'], 'capture_only')
            self.assertEqual(result['dual_capture'], 'captured')
        self.assertEqual(m.notification_load(self.data)['entries'][EID + '/announced']['state'], 'suppressed')

    def test_real_new_confirmation_after_estimate_is_a_separate_pending_stage(self):
        m.dual_toggle(self.data, True, now=T)
        m.poll(self.data, now=T, transport=lambda _: (200, {}, source_payload(T)))
        e = source_event()
        m.poll(self.data, now=T + 600, transport=lambda _: (200, {}, source_payload(T + 600, [e])))
        e['presentation']['status'] = 'likely_completed'
        m.poll(self.data, now=T + 1200, transport=lambda _: (200, {}, source_payload(T + 1200, [e])))
        confirmed = source_event(confirmed=T + 1500)
        result = m.poll(self.data, now=T + 1800, transport=lambda _: (200, {}, source_payload(T + 1800, [confirmed])))
        self.assertEqual(result['source_confirmed'], 1)
        ledger = json.loads((self.data / 'dual-notifications.json').read_text())
        for channel in ('wechat', 'email'):
            self.assertEqual(ledger['entries'][EID + '/announced']['channels'][channel]['state'], 'suppressed')
            self.assertEqual(ledger['entries'][EID + '/confirmed']['channels'][channel]['state'], 'pending')
        self.assertEqual(result['dual_channel_states']['wechat']['pending'], 1)

    def test_removed_event_is_observable_without_claiming_withdrawal_or_confirmation(self):
        self.observed(source_event())
        m.observe_source(self.data, source_payload(T + 1200), T + 1200)
        rows = [json.loads(s) for s in (self.data / 'monitor.jsonl').read_text().splitlines()]
        self.assertEqual(rows[-1]['action'], 'source_removed')
        self.assertNotIn('confirmed_at', rows[-1])

    def test_new_diagnostic_cache_cannot_overwrite_corrupt_previous_evidence(self):
        self.observed(source_event())
        p = self.data / 'source-observations.json'
        p.write_text('{bad json')
        with self.assertRaises(ValueError):
            self.observed(source_event(), T + 1200)
        self.assertEqual(p.read_text(), '{bad json')

    def test_activity_rollout_does_not_regress_unchanged_canonical_version(self):
        m.dual_toggle(self.data, True, now=T)
        m.poll(self.data, now=T, transport=lambda _: (200, {}, source_payload(T)))
        e = source_event()
        unknown = source_activity(published=T + 500)
        unknown['action'] = None
        body = source_payload(T + 600, [e], [source_activity(), unknown])
        m.poll(self.data, now=T + 600, transport=lambda _: (200, {}, body))
        result = m.poll(self.data, now=T + 1200, transport=lambda _: (200, {}, source_payload(T + 1200, [e])))
        self.assertEqual(result['dual_capture'], 'captured')
        ledger = json.loads((self.data / 'dual-notifications.json').read_text())
        self.assertTrue(ledger['healthy'])
        self.assertEqual(ledger['versions'][EID], T + 20)
        # Unknown remains a stop; disappearance does not authorize a new attempt.
        self.assertEqual(ledger['terminal'][EID], 'unknown')
        self.assertTrue(all(c['state'] == 'suppressed' for item in ledger['entries'].values() for c in item['channels'].values()))

    def test_preexisting_inflated_watermark_is_preserved_and_explicitly_blocked(self):
        m.dual_toggle(self.data, True, now=T)
        m.poll(self.data, now=T, transport=lambda _: (200, {}, source_payload(T)))
        e = source_event()
        m.poll(self.data, now=T + 600, transport=lambda _: (200, {}, source_payload(T + 600, [e])))
        p = self.data / 'dual-notifications.json'
        old = json.loads(p.read_text())
        old['versions'][EID] = T + 500
        m.atomic_json(p, old)
        result = m.poll(self.data, now=T + 1200, transport=lambda _: (200, {}, source_payload(T + 1200, [e])))
        self.assertEqual(result['dual_capture'], 'blocked')
        self.assertEqual(result['dual_error'], 'out_of_order_event_version')
        saved = json.loads(p.read_text())
        self.assertEqual(saved['versions'][EID], T + 500)
        self.assertEqual(saved['entries'], old['entries'])
        self.assertFalse(saved['healthy'])


if __name__ == '__main__':
    unittest.main()
