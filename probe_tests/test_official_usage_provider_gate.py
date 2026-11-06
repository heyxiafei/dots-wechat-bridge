import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from probe.official_usage_provider import OfficialUsageProvider, OfficialReadDenied
from probe.usage_display import UsageBinding, CreditInfo


class OfficialUsageEvidenceGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_evidence_never_invokes_sync_or_async_official_reader(self):
        binding = UsageBinding('synthetic-owner', 'synthetic-account', True)
        raw = {'rateLimits': {'primary': {
            'usedPercent': 25, 'windowDurationMins': 10080, 'resetsAt': 1000}}}
        for evidence in ({'not': 'a reference'}, ['proof'], 1, True):
            with self.subTest(evidence_type=type(evidence).__name__):
                synchronous = Mock(return_value=raw)
                asynchronous = AsyncMock(return_value=raw)
                for reader in (synchronous, asynchronous):
                    provider = OfficialUsageProvider.from_host_tools(
                        {'mcp__codex_app__get_usage_limits': reader}, binding,
                        evidence, clock=lambda: 100)
                    result = await provider.prepare_async(binding)
                    self.assertEqual(result['status'], 'unavailable')
                    self.assertFalse(result['official_call_attempted'])
                    self.assertIn('未调用', result['text'])
                    reader.assert_not_called()

    def provider(self, reader, *, clock=lambda: 100, credit_mapper=None):
        self.binding = UsageBinding('synthetic-owner', 'synthetic-account', True)
        return OfficialUsageProvider.from_host_tools(
            {'mcp__codex_app__get_usage_limits': reader}, self.binding, 'fixture-proof',
            clock=clock, credit_mapper=credit_mapper)

    @staticmethod
    def quota(reset=1000):
        return {'rateLimits': {'primary': {
            'usedPercent': 25, 'windowDurationMins': 10080, 'resetsAt': reset}}}

    async def test_sync_future_handoff_waits_original_result_once(self):
        future = asyncio.get_running_loop().create_future()
        read = Mock(return_value=future)
        provider = self.provider(read)
        for _ in range(2):
            result = provider.prepare(self.binding, request_ref='original')
            self.assertTrue(result['adapter_requires_async'])
            self.assertTrue(result['official_call_attempted'])
            self.assertNotIn('75%', result['text'])
        other = await provider.prepare_async(self.binding, request_ref='another-request')
        self.assertTrue(other['adapter_busy'])
        self.assertFalse(other['official_call_attempted'])
        task = asyncio.create_task(provider.prepare_async(self.binding, request_ref='original'))
        await asyncio.sleep(0)
        duplicate = await provider.prepare_async(self.binding, request_ref='original')
        self.assertTrue(duplicate['adapter_busy'])
        future.set_result(self.quota())
        self.assertEqual((await task)['status'], 'reported')
        read.assert_called_once_with({})

    async def test_done_future_denial_is_not_formatted_as_invalid_or_lost(self):
        future = asyncio.get_running_loop().create_future()
        future.set_exception(OfficialReadDenied())
        read = Mock(return_value=future)
        provider = self.provider(read)
        self.assertTrue(provider.prepare(self.binding)['adapter_requires_async'])
        self.assertEqual((await provider.prepare_async(self.binding))['status'], 'permission_denied')
        read.assert_called_once_with({})

    async def test_custom_awaitable_and_running_task_are_handed_off_without_restart(self):
        raw = self.quota()
        class Result:
            def __await__(self):
                async def completed():
                    return raw
                return completed().__await__()
        async def completed():
            return raw
        for value in (Result(), asyncio.create_task(completed())):
            read = Mock(return_value=value)
            provider = self.provider(read)
            self.assertTrue(provider.prepare(self.binding)['adapter_requires_async'])
            self.assertEqual((await provider.prepare_async(self.binding))['status'], 'reported')
            read.assert_called_once_with({})

    async def test_cancel_waiter_keeps_original_read_for_same_request(self):
        future = asyncio.get_running_loop().create_future()
        read = Mock(return_value=future)
        provider = self.provider(read)
        task = asyncio.create_task(provider.prepare_async(self.binding, request_ref='original'))
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(future.cancelled())
        future.set_result(self.quota())
        result = await provider.prepare_async(self.binding, request_ref='original')
        self.assertEqual(result['status'], 'reported')
        read.assert_called_once_with({})

    async def test_finished_future_cannot_gain_freshness_when_resumed_later(self):
        now = [100]
        future = asyncio.get_running_loop().create_future()
        future.set_result(self.quota())
        provider = self.provider(Mock(return_value=future), clock=lambda: now[0])
        provider.prepare(self.binding)
        now[0] = 401
        result = await provider.prepare_async(self.binding)
        self.assertEqual(result['status'], 'stale')
        self.assertEqual(result['read_finished_at'], 100)
        self.assertNotIn('75%', result['text'])

    async def test_cancelled_source_finishes_as_failure_without_blocking_other_requests(self):
        future = asyncio.get_running_loop().create_future()
        read = Mock(side_effect=[future, self.quota()])
        provider = self.provider(read)
        task = asyncio.create_task(provider.prepare_async(self.binding, request_ref='original'))
        await asyncio.sleep(0)
        future.cancel()
        self.assertEqual((await task)['status'], 'read_failed')
        self.assertEqual((await provider.prepare_async(self.binding, request_ref='another'))['status'], 'reported')
        self.assertEqual(read.call_count, 2)

    async def test_capability_change_cannot_disclose_pending_result_or_skip_pending_gate(self):
        future = asyncio.get_running_loop().create_future()
        read = Mock(return_value=future)
        provider = self.provider(read)
        task = asyncio.create_task(provider.prepare_async(self.binding, request_ref='original'))
        await asyncio.sleep(0)
        provider.capability = None
        self.assertTrue(provider.prepare(self.binding, request_ref='another')['adapter_busy'])
        future.set_result(self.quota())
        result = await task
        self.assertTrue(result['adapter_context_changed'])
        self.assertNotIn('75%', result['text'])
        read.assert_called_once_with({})

    async def test_mapping_delay_rechecks_freshness_and_reset_at_actual_current_time(self):
        for finish, reset, status in ((401, 1000, 'stale'), (101, 101, 'reported')):
            for asynchronous in (False, True):
                now = [100]
                def mapper(_):
                    now[0] = finish
                    return CreditInfo(balance='0', mapping_verified=True)
                provider = self.provider(Mock(return_value=self.quota(reset)),
                    clock=lambda: now[0], credit_mapper=mapper)
                result = (await provider.prepare_async(self.binding) if asynchronous
                          else provider.prepare(self.binding))
                self.assertEqual(result['status'], status)
                self.assertNotIn('75%', result['text'])
                self.assertEqual(result['read_finished_at'], 100)


class OfficialUsageDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def provider(self, reader, **options):
        self.binding = UsageBinding('timeout-fixture-owner', 'timeout-fixture-account', True)
        return OfficialUsageProvider.from_host_tools(
            {'mcp__codex_app__get_usage_limits': reader}, self.binding, 'timeout-fixture-proof',
            clock=lambda: 100, read_timeout_seconds=options.pop('read_timeout_seconds', .02), **options)

    @staticmethod
    def quota():
        return {'rateLimits': {'primary': {
            'usedPercent': 25, 'windowDurationMins': 10080, 'resetsAt': 1000}}}

    def test_timeout_configuration_is_finite_bounded_and_not_message_data(self):
        self.assertEqual(OfficialUsageProvider().read_timeout_seconds, 20)
        for value in (True, False, None, '20', 0, -1, 61, float('nan'), float('inf'), 10 ** 1000):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, 'invalid_official_read_timeout'):
                    OfficialUsageProvider(read_timeout_seconds=value)

    async def test_timeout_is_terminal_but_source_is_fenced_until_late_success(self):
        future = asyncio.get_running_loop().create_future()
        read = Mock(side_effect=[future, self.quota()])
        provider = self.provider(read)
        result = await asyncio.wait_for(provider.prepare_async(self.binding, request_ref='first'), .5)
        self.assertEqual(result['status'], 'read_timeout')
        self.assertIn('官方读取超时', result['text'])
        self.assertEqual(len(result['text'].splitlines()), 4)
        self.assertNotIn('75%', result['text'])
        self.assertIsNone(result['read_finished_at'])
        self.assertGreaterEqual(result['read_elapsed_seconds'], .02)
        self.assertTrue(result['official_result_pending'])
        self.assertFalse(future.cancelled())
        self.assertEqual((await provider.prepare_async(self.binding, request_ref='first'))['status'], 'read_timeout')
        busy = await provider.prepare_async(self.binding, request_ref='second')
        self.assertEqual(busy['status'], 'read_busy')
        self.assertFalse(busy['official_call_attempted'])
        self.assertTrue(busy['prior_read_pending'])
        future.set_result(self.quota())
        await asyncio.sleep(0)
        self.assertIsNone(provider._pending)
        self.assertEqual(provider.prepare(self.binding, request_ref='first')['text'], result['text'])
        self.assertEqual(read.call_count, 1)
        self.assertEqual((await provider.prepare_async(self.binding, request_ref='third'))['status'], 'reported')
        self.assertEqual(read.call_count, 2)

    async def test_cancellation_resistant_source_does_not_extend_timeout(self):
        release, entered = asyncio.Event(), asyncio.Event()
        cancellation_requests = []
        async def source():
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancellation_requests.append(True)
            return self.quota()
        source_task = asyncio.create_task(source())
        await entered.wait()
        provider = self.provider(Mock(return_value=source_task))
        try:
            result = await asyncio.wait_for(provider.prepare_async(self.binding, request_ref='original'), .5)
            self.assertEqual(result['status'], 'read_timeout')
            self.assertEqual(cancellation_requests, [])
            self.assertFalse(source_task.done())
        finally:
            release.set()
            await source_task
            await asyncio.sleep(0)

    async def test_cancelled_waiter_and_sync_handoff_share_original_deadline(self):
        for sync_first in (False, True):
            with self.subTest(sync_first=sync_first):
                now = [0.0]
                future = asyncio.get_running_loop().create_future()
                read = Mock(return_value=future)
                provider = self.provider(read, monotonic=lambda: now[0], read_timeout_seconds=1)
                if sync_first:
                    self.assertTrue(provider.prepare(self.binding, request_ref='original')['adapter_requires_async'])
                waiter = asyncio.create_task(provider.prepare_async(self.binding, request_ref='original'))
                await asyncio.sleep(0)
                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertFalse(future.cancelled())
                now[0] = 2.0
                result = await asyncio.wait_for(provider.prepare_async(self.binding, request_ref='original'), .5)
                self.assertEqual(result['status'], 'read_timeout')
                self.assertEqual(result['read_elapsed_seconds'], 2.0)
                future.cancel()
                await asyncio.sleep(0)
                read.assert_called_once_with({})

    async def test_late_denial_blocks_capability_and_keeps_original_timeout(self):
        future = asyncio.get_running_loop().create_future()
        read = Mock(return_value=future)
        provider = self.provider(read)
        original = await provider.prepare_async(self.binding, request_ref='original')
        future.set_exception(OfficialReadDenied('synthetic private diagnostic must not be copied'))
        await asyncio.sleep(0)
        self.assertIsNone(provider._pending)
        self.assertEqual((await provider.prepare_async(self.binding, request_ref='original'))['text'], original['text'])
        for request in ('next-one', 'next-two'):
            denied = await provider.prepare_async(self.binding, request_ref=request)
            self.assertEqual(denied['status'], 'permission_denied')
            self.assertTrue(denied['prior_read_permission_denied'])
            self.assertFalse(denied['official_call_attempted'])
            self.assertNotIn('synthetic private', str(denied))
        read.assert_called_once_with({})

    async def test_invalid_binding_does_not_gain_timeout_or_busy_reply_authority(self):
        future = asyncio.get_running_loop().create_future()
        read = Mock(return_value=future)
        provider = self.provider(read)
        invalid = UsageBinding(self.binding.owner_ref, self.binding.account_ref, False)
        result = await provider.prepare_async(invalid, request_ref='invalid')
        self.assertEqual(result['status'], 'binding_unverified')
        read.assert_not_called()
        await provider.prepare_async(self.binding, request_ref='original')
        mismatch = UsageBinding('other-owner', 'other-account', True)
        result = await provider.prepare_async(mismatch, request_ref='other')
        self.assertTrue(result['adapter_requires_async'])
        self.assertTrue(result['adapter_busy'])
        future.set_result(self.quota())
        await asyncio.sleep(0)
        read.assert_called_once_with({})

    async def test_completed_after_deadline_cannot_be_relabelled_as_timely(self):
        now = [0.0]
        future = asyncio.get_running_loop().create_future()
        read = Mock(return_value=future)
        provider = self.provider(read, monotonic=lambda: now[0], read_timeout_seconds=1)
        provider.prepare(self.binding, request_ref='original')
        now[0] = 2.0
        future.set_result(self.quota())  # Completion callback has not run yet.
        result = await provider.prepare_async(self.binding, request_ref='original')
        self.assertEqual(result['status'], 'read_timeout')
        self.assertIsNone(result['read_finished_at'])
        self.assertNotIn('75%', result['text'])
        await asyncio.sleep(0)
        read.assert_called_once_with({})

    async def test_timeout_history_is_bounded_and_does_not_evict_old_requests(self):
        read = Mock()
        provider = self.provider(read, read_timeout_seconds=.001)
        provider.MAX_TIMEOUT_RECORDS = 2
        for request in ('first', 'second'):
            future = asyncio.get_running_loop().create_future()
            read.return_value = future
            self.assertEqual((await provider.prepare_async(self.binding, request_ref=request))['status'], 'read_timeout')
            future.set_result(self.quota())
            await asyncio.sleep(0)
        result = await provider.prepare_async(self.binding, request_ref='third')
        self.assertEqual(result['status'], 'read_history_full')
        self.assertFalse(result['official_call_attempted'])
        self.assertEqual((await provider.prepare_async(self.binding, request_ref='first'))['status'], 'read_timeout')
        self.assertEqual(read.call_count, 2)


if __name__ == '__main__':
    unittest.main()
