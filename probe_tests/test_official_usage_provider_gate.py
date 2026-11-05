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


if __name__ == '__main__':
    unittest.main()
