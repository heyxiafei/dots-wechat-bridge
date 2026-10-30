import unittest
from unittest.mock import AsyncMock, Mock

from probe.official_usage_provider import OfficialUsageProvider
from probe.usage_display import UsageBinding


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


if __name__ == '__main__':
    unittest.main()
