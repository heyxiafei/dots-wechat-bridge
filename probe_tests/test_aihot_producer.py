"""Include the isolated producer suite in the standard bridge test command."""
import importlib
from contextlib import ExitStack
import unittest
from unittest.mock import patch


_MODULES = (
    "test_banked_recovery",
    "test_cache_recovery",
    "test_dual_producer",
    "test_monitor",
    "test_notifications",
    "test_relay_recovery",
    "test_shared_format",
    "test_source_observability",
    "test_text_b_contract",
)


class OfflineProducerSuite(unittest.TestSuite):
    """Fail any unmocked network call while the producer tests are running."""

    def run(self, result, debug=False):
        with ExitStack() as guards:
            for target in ('socket.socket', 'socket.create_connection',
                           'socket.getaddrinfo', 'urllib.request.urlopen',
                           'urllib.request.OpenerDirector.open'):
                guards.enter_context(patch(target, side_effect=AssertionError('network_forbidden')))
            return super().run(result, debug=debug)


def load_tests(loader, tests, pattern):
    suite = OfflineProducerSuite()
    for name in _MODULES:
        module = importlib.import_module("integrations.aihot.tests." + name)
        suite.addTests(loader.loadTestsFromModule(module))
    return suite
