"""Synthetic quota presentation contracts; no real account, I/O or sends."""
import copy
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from probe.usage_display import (CreditInfo, MAX_REPLY_BYTES, UsageBinding,
    UsageObservation, UsageView, format_usage, normalize_usage,
    read_and_format_usage, render_usage)

NOW = 1_900_000_000.
ALPHA = UsageBinding('fixture-owner-alpha', 'fixture-account-alpha', verified=True)
BETA = UsageBinding('fixture-owner-beta', 'fixture-account-beta', verified=True)
SHANGHAI = ZoneInfo('Asia/Shanghai')


def window(used=24, minutes=300, reset=NOW + 3600):
    return dict(usedPercent=used, windowDurationMins=minutes, resetsAt=reset)


def bucket(primary=None, secondary=None, identity='codex'):
    result = {'limitId': identity}
    if primary is not None:
        result['primary'] = primary
    if secondary is not None:
        result['secondary'] = secondary
    return result


def payload(primary=None, secondary=None):
    return {'rateLimitsByLimitId': {'codex': bucket(
        window() if primary is None else primary, secondary)}}


def observation(raw=None, *, binding=ALPHA, read_at=NOW, credits=None, source='get_usage_limits'):
    return UsageObservation(binding.owner_ref, binding.account_ref, read_at,
        payload() if raw is None else raw, source=source, credits=credits)


def normalized(raw=None, **kwargs):
    return normalize_usage(observation(raw, **kwargs), ALPHA, now=NOW)


class UsageBucketAndWindowTests(unittest.TestCase):
    def test_map_takes_precedence_over_conflicting_legacy_bucket(self):
        raw = payload(window(24), window(58, 10080))
        raw['rateLimits'] = bucket(window(99))
        view = normalized(raw)
        self.assertEqual((view.status, view.bucket, view.source_view),
                         ('reported', 'codex', 'rateLimitsByLimitId'))
        self.assertEqual([(w.minutes, w.used, w.remaining) for w in view.windows],
                         [(300, 24., 76.), (10080, 58., 42.)])
        text = format_usage(view)
        self.assertIn('剩余76%，已用24%', text)
        self.assertIn('剩余42%，已用58%', text)
        self.assertNotIn('99%', text)

    def test_missing_or_null_map_allows_documented_legacy_fallback(self):
        for raw in ({'rateLimits': bucket(window(31))},
                    {'rateLimitsByLimitId': None, 'rateLimits': bucket(window(31))}):
            with self.subTest(has_map='rateLimitsByLimitId' in raw):
                view = normalized(raw)
                self.assertEqual((view.status, view.source_view, view.windows[0].used),
                                 ('reported', 'rateLimits', 31.))
                self.assertIn('legacy_single_bucket', view.issues)

    def test_empty_or_missing_requested_bucket_never_falls_back(self):
        for entries in ({}, {'another': bucket(window(91), identity='another')}, {'codex': None}):
            with self.subTest(keys=list(entries)):
                view = normalized({'rateLimitsByLimitId': entries, 'rateLimits': bucket(window(99))})
                self.assertEqual(view.status, 'no_windows')
                self.assertEqual(view.source_view, 'rateLimitsByLimitId')
                self.assertIn('requested_bucket_missing', view.issues)
                self.assertNotIn('99%', format_usage(view))

    def test_nonnull_damaged_map_never_falls_back(self):
        for entries in ([], '', 0, False, 'private-untrusted-map'):
            with self.subTest(kind=type(entries).__name__):
                view = normalized({'rateLimitsByLimitId': entries, 'rateLimits': bucket(window(99))})
                self.assertEqual(view.status, 'invalid_result')
                self.assertIn('bucket_map_invalid', view.issues)
                self.assertNotIn('99%', format_usage(view))
                self.assertNotIn('private-untrusted-map', format_usage(view))

    def test_multiple_buckets_select_codex_only_without_combining(self):
        raw = payload(window(24), window(58, 10080))
        raw['rateLimitsByLimitId']['separate-fixture'] = bucket(window(97), window(96, 10080), 'separate-fixture')
        view = normalized(raw)
        self.assertEqual(len(view.windows), 2)
        self.assertEqual([w.used for w in view.windows], [24., 58.])
        self.assertIn('other_buckets_not_combined', view.issues)
        self.assertNotIn('separate-fixture', format_usage(view))

    def test_bucket_declared_identity_mismatch_is_not_displayed(self):
        view = normalized({'rateLimitsByLimitId': {'codex': bucket(window(92), identity='another')}})
        self.assertEqual(view.status, 'invalid_result')
        self.assertIn('bucket_identity_mismatch', view.issues)
        self.assertNotIn('92%', format_usage(view))

    def test_legacy_identity_unreported_is_explicit_and_no_windows_are_invented(self):
        view = normalized({'rateLimits': {'primary': window(34)}})
        self.assertEqual(len(view.windows), 1)
        self.assertIn('legacy_bucket_identity_unreported', view.issues)
        self.assertIn('周窗口：本次未返回，未知', format_usage(view))
        self.assertEqual(normalized({'rateLimits': {}}).status, 'no_windows')

    def test_duration_identifies_five_hour_and_week_when_slots_are_swapped(self):
        view = normalized(payload(window(58, 10080), window(24, 300)))
        self.assertEqual([(w.slot, w.minutes) for w in view.windows], [('secondary', 300), ('primary', 10080)])
        text = format_usage(view)
        self.assertIn('Codex 5小时额度：剩余76%，已用24%', text)
        self.assertIn('Codex 周额度：剩余42%，已用58%', text)

    def test_missing_invalid_and_custom_durations_are_not_assumed_from_slots(self):
        for duration in (None, True, False, 0, -1, 300., '300', 2 ** 1000):
            with self.subTest(kind=type(duration).__name__):
                view = normalized(payload(window(24, duration)))
                self.assertIsNone(view.windows[0].minutes)
                self.assertIn('未知时长窗口', format_usage(view))
                self.assertIn('5小时窗口：本次未返回，未知', format_usage(view))
        for duration, label in ((45, '45分钟额度'), (120, '2小时额度'), (2880, '2天额度')):
            with self.subTest(duration=duration):
                self.assertIn(label, format_usage(normalized(payload(window(24, duration)))))

    def test_duplicate_known_and_unknown_durations_keep_slot_labels(self):
        for duration, label in ((300, '5小时额度'), (None, '未知时长窗口')):
            with self.subTest(duration=duration):
                text = format_usage(normalized(payload(window(24, duration), window(58, duration))))
                self.assertIn(label + '（primary）', text)
                self.assertIn(label + '（secondary）', text)

    def test_missing_null_and_damaged_windows_do_not_become_zero_usage(self):
        for value in (None, [], 'bad-window', False):
            with self.subTest(kind=type(value).__name__):
                view = normalized({'rateLimitsByLimitId': {'codex': {'primary': value}}})
                self.assertNotIn('剩余100%', format_usage(view))
                self.assertNotIn('已用0%', format_usage(view))
                self.assertIn('未知', format_usage(view))

    def test_unknown_percentages_remain_unknown_instead_of_guessing_zero(self):
        invalid = [None, True, False, '24', float('nan'), float('inf'), -float('inf'),
                   -1, 1_000_001, 10 ** 5000]
        for value in invalid:
            with self.subTest(kind=type(value).__name__):
                view = normalized(payload(window(value)))
                self.assertIsNone(view.windows[0].used)
                self.assertIsNone(view.windows[0].remaining)
                text = format_usage(view)
                self.assertIn('本次未返回有效用量', text)
                self.assertNotIn('剩余100%', text)
        missing = window()
        del missing['usedPercent']
        self.assertIsNone(normalized(payload(missing)).windows[0].used)

    def test_valid_percentages_and_overreported_usage_keep_correct_direction(self):
        for value, remaining in ((0, 100.), (24.5, 75.5), (100, 0.), (125, 0.)):
            with self.subTest(value=value):
                w = normalized(payload(window(value))).windows[0]
                self.assertEqual((w.used, w.remaining), (value, remaining))
        self.assertIn('接口报告超出100%', format_usage(normalized(payload(window(125)))))


class UsageTimeTests(unittest.TestCase):
    def test_reset_timestamp_is_unix_seconds_displayed_in_beijing_time(self):
        reset = NOW + 7200
        text = format_usage(normalized(payload(window(24, reset=reset))))
        expected = datetime.fromtimestamp(reset, SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')
        self.assertIn(expected + '（北京时间）', text)

    def test_millisecond_invalid_and_missing_resets_are_not_guessed(self):
        for reset in (None, 0, -1, True, False, '1900003600', float('nan'), float('inf'),
                      10 ** 5000, (NOW + 3600) * 1000):
            with self.subTest(kind=type(reset).__name__):
                view = normalized(payload(window(24, reset=reset)))
                self.assertIsNone(view.windows[0].resets_at)
                self.assertEqual(view.windows[0].used, 24.)
                self.assertIn('下次重置：未知', format_usage(view))
        value = window()
        del value['resetsAt']
        self.assertIsNone(normalized(payload(value)).windows[0].resets_at)

    def test_elapsed_reset_hides_old_used_and_remaining_percentages(self):
        for reset in (NOW, NOW - 1):
            with self.subTest(reset=reset):
                view = normalized(payload(window(24, reset=reset)))
                self.assertEqual(view.windows[0].state, 'reset_elapsed')
                self.assertIsNone(view.windows[0].used)
                self.assertIsNone(view.windows[0].remaining)
                text = format_usage(view)
                self.assertIn('重置时间已过，需重新读取', text)
                self.assertNotIn('已用24%', text)
                self.assertNotIn('剩余76%', text)

    def test_invalid_or_future_observation_time_never_exposes_snapshot(self):
        for read_at in (None, 0, -1, True, '1900000000', float('nan'), float('inf'),
                        10 ** 5000, NOW + .001):
            with self.subTest(kind=type(read_at).__name__):
                reply = render_usage(observation(read_at=read_at), ALPHA, now=NOW)
                self.assertEqual(reply.status, 'timestamp_invalid')
                self.assertNotIn('已用24%', reply.text)

    def test_invalid_now_and_max_age_fail_closed(self):
        for now in (False, 0, -1, float('nan'), float('inf'), 10 ** 5000):
            with self.subTest(option='now', kind=type(now).__name__):
                self.assertEqual(normalize_usage(observation(), ALPHA, now=now).status, 'timestamp_invalid')
        for age in (None, False, 0, -1, '300', float('nan'), float('inf'), 10 ** 5000):
            with self.subTest(option='max_age', kind=type(age).__name__):
                self.assertEqual(normalize_usage(observation(), ALPHA, now=NOW, max_age_seconds=age).status, 'timestamp_invalid')

    def test_freshness_boundary_and_stale_snapshot_hide_usage_and_credit(self):
        self.assertEqual(normalize_usage(observation(read_at=NOW - 300), ALPHA, now=NOW).status, 'reported')
        reply = render_usage(observation(read_at=NOW - 300.001,
            credits=CreditInfo(balance='17.25', mapping_verified=True)), ALPHA, now=NOW)
        self.assertEqual(reply.status, 'stale')
        self.assertIn('快照已过期', reply.text)
        self.assertNotIn('24%', reply.text)
        self.assertNotIn('76%', reply.text)
        self.assertNotIn('17.25', reply.text)


class UsageCreditTests(unittest.TestCase):
    def test_only_explicit_verified_collector_mapping_displays_zero_or_balance(self):
        for info in (None, CreditInfo(balance=0), CreditInfo(balance='17.25'),
                     CreditInfo(balance=0, mapping_verified=1)):
            with self.subTest(kind=type(info).__name__):
                self.assertIn('未知', normalized(credits=info).credit_text)
        self.assertEqual(normalized(credits=CreditInfo(balance=0, mapping_verified=True)).credit_text, '0')
        self.assertEqual(normalized(credits=CreditInfo(balance='17.2500', mapping_verified=True)).credit_text, '17.25')
        self.assertEqual(normalized(credits=CreditInfo(balance=Decimal('3.125'), mapping_verified=True)).credit_text, '3.125')

    def test_raw_unknown_credit_subfields_are_never_inferred(self):
        raw = payload()
        raw['credits'] = {'balance': 0, 'unlimited': True, 'hasCredits': False, 'private_unknown': 'sensitive-fixture'}
        raw['rateLimitsByLimitId']['codex']['credits'] = {'balance': 0}
        view = normalized(raw)
        self.assertIn('未知', view.credit_text)
        self.assertNotIn('不限额', view.credit_text)
        self.assertNotIn('sensitive-fixture', format_usage(view))

    def test_unlimited_and_workspace_scope_require_exact_verified_flags(self):
        self.assertIn('未知', normalized(credits=CreditInfo(unlimited=True)).credit_text)
        self.assertIn('未知', normalized(credits=CreditInfo(unlimited=1, mapping_verified=True)).credit_text)
        self.assertEqual(normalized(credits=CreditInfo(unlimited=True, mapping_verified=True)).credit_text, '不限额（接口标记）')
        self.assertEqual(normalized(credits=CreditInfo(balance=0, scope='workspace', mapping_verified=True)).credit_text, '0（工作区）')
        self.assertEqual(normalized(credits=CreditInfo(unlimited=True, scope='workspace', mapping_verified=True)).credit_text, '不限额（接口标记）（工作区）')
        self.assertIn('范围未核实', normalized(credits=CreditInfo(balance=0, scope='private-scope', mapping_verified=True)).credit_text)

    def test_invalid_nonfinite_excessive_and_huge_integer_credits_do_not_escape(self):
        invalid = [None, True, False, -1, 'not a balance', float('nan'), float('inf'),
                   Decimal('NaN'), Decimal('Infinity'), Decimal('1e999999'),
                   '1000000000000000001', '0.000000001', '9' * 100000, 10 ** 5000]
        for value in invalid:
            with self.subTest(kind=type(value).__name__):
                reply = render_usage(observation(credits=CreditInfo(balance=value, mapping_verified=True)), ALPHA, now=NOW)
                self.assertEqual(reply.status, 'reported')
                self.assertIn('信用点余额：未知', reply.text)
                self.assertNotIn('NaN', reply.text)
                self.assertNotIn('Infinity', reply.text)
                self.assertLessEqual(len(reply.text.encode('utf-8')), MAX_REPLY_BYTES)

    def test_credits_can_be_shown_without_inventing_absent_windows(self):
        view = normalized({'rateLimits': {}}, credits=CreditInfo(balance='2.5', mapping_verified=True))
        self.assertEqual(view.status, 'no_windows')
        self.assertEqual(view.credit_text, '2.5')
        self.assertIn('本次未返回可识别窗口', format_usage(view))


class UsageIdentityAndReaderTests(unittest.TestCase):
    def test_both_owner_and_account_are_isolated_and_refs_never_display(self):
        for wrong in (replace(observation(), owner_ref=BETA.owner_ref),
                      replace(observation(), account_ref=BETA.account_ref),
                      observation(binding=BETA)):
            with self.subTest(owner=wrong.owner_ref == ALPHA.owner_ref):
                reply = render_usage(wrong, ALPHA, now=NOW)
                self.assertEqual(reply.status, 'account_mismatch')
                for ref in (ALPHA.owner_ref, ALPHA.account_ref, BETA.owner_ref, BETA.account_ref):
                    self.assertNotIn(ref, reply.text)
                self.assertNotIn('24%', reply.text)
        first = render_usage(observation(payload(window(24))), ALPHA, now=NOW)
        second = render_usage(observation(payload(window(67)), binding=BETA), BETA, now=NOW)
        self.assertIn('剩余76%', first.text)
        self.assertIn('剩余33%', second.text)
        self.assertNotIn('24%', second.text)

    def test_unverified_or_invalid_binding_does_not_call_reader(self):
        for binding in (UsageBinding(ALPHA.owner_ref, ALPHA.account_ref),
                        UsageBinding(ALPHA.owner_ref, ALPHA.account_ref, verified=1),
                        UsageBinding('', ALPHA.account_ref, verified=True),
                        UsageBinding('x' * 257, ALPHA.account_ref, verified=True), None):
            with self.subTest(kind=type(binding).__name__):
                reader = Mock(side_effect=AssertionError('must not read'))
                reply = read_and_format_usage(reader, binding, now=NOW)
                self.assertEqual(reply.status, 'binding_unverified')
                reader.assert_not_called()

    def test_reader_runs_once_with_request_binding_and_no_retry(self):
        reader = Mock(return_value=observation())
        reply = read_and_format_usage(reader, ALPHA, now=NOW)
        reader.assert_called_once_with(ALPHA)
        self.assertEqual(reply.status, 'reported')
        reader = Mock(return_value=observation(binding=BETA))
        self.assertEqual(read_and_format_usage(reader, ALPHA, now=NOW).status, 'account_mismatch')
        reader.assert_called_once_with(ALPHA)

    def test_unavailable_and_failed_reader_never_leak_exception_or_retry(self):
        self.assertEqual(read_and_format_usage(None, ALPHA, now=NOW).status, 'unavailable')
        reader = Mock(side_effect=RuntimeError('private-reader-token-fixture'))
        reply = read_and_format_usage(reader, ALPHA, now=NOW)
        self.assertEqual(reply.status, 'read_failed')
        self.assertNotIn('private-reader-token-fixture', reply.text)
        reader.assert_called_once_with(ALPHA)

    def test_unverified_source_and_invalid_result_are_not_exposed(self):
        reply = render_usage(observation(source='untrusted-private-source'), ALPHA, now=NOW)
        self.assertEqual(reply.status, 'source_unverified')
        self.assertNotIn('untrusted-private-source', reply.text)
        self.assertEqual(render_usage({'rateLimits': {}}, ALPHA, now=NOW).status, 'account_mismatch')
        for limit_id in ('', 'x' * 65, '../codex', '合成窗口', None, True):
            with self.subTest(kind=type(limit_id).__name__):
                self.assertEqual(normalize_usage(observation(), ALPHA, now=NOW, limit_id=limit_id).status, 'invalid_result')

    def test_presentation_has_no_network_file_or_sender_dependency(self):
        reader = Mock(return_value=observation())
        with patch('socket.create_connection', side_effect=AssertionError('no network')), \
             patch('pathlib.Path.read_text', side_effect=AssertionError('no credential files')), \
             patch('pathlib.Path.read_bytes', side_effect=AssertionError('no credential files')):
            reply = read_and_format_usage(reader, ALPHA, now=NOW)
        self.assertEqual(reply.status, 'reported')
        reader.assert_called_once_with(ALPHA)


class UsageWrapperAndOutputTests(unittest.TestCase):
    def test_supported_mcp_structured_result_and_single_json_text_wrappers(self):
        raw = payload(window(24), window(58, 10080))
        expected = normalized(raw)
        wrappers = [raw, {'structuredContent': raw}, {'result': raw},
                    {'result': {'structuredContent': raw}},
                    {'content': [{'type': 'text', 'text': json.dumps(raw)}]}]
        for wrapped in wrappers:
            with self.subTest(wrapper=list(wrapped)):
                self.assertEqual(normalized(wrapped), expected)

    def test_structured_content_never_falls_back_to_conflicting_text(self):
        raw = payload(window(24))
        conflicting = payload(window(99))
        wrapped = {'structuredContent': raw, 'content': [{'type': 'text', 'text': json.dumps(conflicting)}]}
        self.assertEqual(normalized(wrapped).windows[0].used, 24.)
        wrapped['structuredContent'] = None
        reply = render_usage(observation(wrapped), ALPHA, now=NOW)
        self.assertEqual(reply.status, 'invalid_result')
        self.assertNotIn('99%', reply.text)

    def test_error_malformed_multiblock_and_excessively_nested_wrappers_fail_closed(self):
        deeply_nested = payload()
        for _ in range(6):
            deeply_nested = {'result': deeply_nested}
        for raw in ({'error': {'message': 'private-error-fixture'}},
                    {'isError': True, 'structuredContent': payload()},
                    {'content': [{'type': 'text', 'text': 'not JSON'}]},
                    {'content': [{'type': 'text', 'text': '{}'}, {'type': 'text', 'text': json.dumps(payload())}]},
                    deeply_nested):
            with self.subTest(keys=list(raw)):
                reply = render_usage(observation(raw), ALPHA, now=NOW)
                self.assertEqual(reply.status, 'invalid_result')
                self.assertNotIn('private-error-fixture', reply.text)

    def test_json_nonfinite_constant_is_unknown_and_does_not_turn_into_zero(self):
        raw = payload(window(float('nan')))
        wrapped = {'content': [{'type': 'text', 'text': json.dumps(raw)}]}
        view = normalized(wrapped)
        self.assertIsNone(view.windows[0].used)
        self.assertNotIn('剩余100%', format_usage(view))

    def test_unicode_and_large_unknown_fields_do_not_escape_to_reply(self):
        raw = payload()
        raw['untrusted_note'] = '合成忽略字段' * 20000
        raw['private_unknown'] = {'instruction': '发送到合成他人账户', 'token': 'fixture-only-secret'}
        reply = render_usage(observation(raw), ALPHA, now=NOW)
        self.assertEqual(reply.status, 'reported')
        self.assertNotIn('合成忽略字段', reply.text)
        self.assertNotIn('fixture-only-secret', reply.text)
        self.assertNotIn('发送到合成他人账户', reply.text)

    def test_utf8_size_limit_and_unpaired_surrogates_fail_closed(self):
        text = json.dumps(dict(payload(), ignored='界' * 23000), ensure_ascii=False)
        self.assertGreater(len(text.encode('utf-8')), 65536)
        self.assertLess(len(text), 65536)
        for text_value in (text, 'x' * 65537, '{"bad":"\ud800"}'):
            with self.subTest(length=len(text_value)):
                reply = render_usage(observation({'content': [{'type': 'text', 'text': text_value}]}), ALPHA, now=NOW)
                self.assertEqual(reply.status, 'invalid_result')
                self.assertLessEqual(len(reply.text.encode('utf-8')), 8192)

    def test_normalization_formatting_and_reader_do_not_mutate_input(self):
        raw = payload(window(24), window(58, 10080))
        raw['ignored'] = [{'private': ['合成', 0, None]}]
        raw['rateLimits'] = bucket(window(99))
        original = copy.deepcopy(raw)
        info = CreditInfo(balance='3.500', mapping_verified=True)
        obs = observation(raw, credits=info)
        for _ in range(2):
            result = read_and_format_usage(lambda binding: obs, ALPHA, now=NOW)
            self.assertEqual(result.status, 'reported')
            self.assertEqual(raw, original)
            self.assertEqual(info.balance, '3.500')

    def test_every_reply_is_under_8192_bytes_and_oversized_formatter_fails_closed(self):
        for raw in (payload(), {'rateLimits': {}}, {'error': 'private'}):
            result = render_usage(observation(raw), ALPHA, now=NOW)
            self.assertLessEqual(len(result.text.encode('utf-8')), 8192)
            self.assertEqual(format_usage(normalize_usage(observation(raw), ALPHA, now=NOW)), result.text)
        oversized = UsageView('reported', read_at=NOW, credit_text='界' * 4000)
        result = format_usage(oversized)
        self.assertIn('超出展示范围', result)
        self.assertLessEqual(len(result.encode('utf-8')), 8192)
        self.assertEqual(MAX_REPLY_BYTES, 8192)


if __name__ == '__main__':
    unittest.main()
