from contextlib import redirect_stdout
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from foundation.__main__ import main, readonly_status
from foundation.config import load_config
from foundation.store import FoundationStore


class OperatorTests(unittest.TestCase):
    def call(self, arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(arguments)
        return code, json.loads(output.getvalue())

    def test_demo_duplicate_after_process_lifetime_does_not_resend(self):
        with tempfile.TemporaryDirectory() as directory:
            args = ["demo", "--state", str(Path(directory) / "demo.sqlite3"), "--text", "sample"]
            code, first = self.call(args)
            self.assertEqual(code, 0)
            self.assertEqual(len(first["simulated_replies_this_run"]), 1)
            code, second = self.call(args)
            self.assertEqual(code, 0)
            self.assertTrue(second["outcome"]["duplicate"])
            self.assertEqual(second["simulated_replies_this_run"], [])
            self.assertEqual(second["wechat_sends"], 0)

    def test_status_does_not_recover_another_active_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "demo.sqlite3"
            with FoundationStore(path) as store:
                store.claim("message", "conversation", "digest", 9999999999)
                self.assertEqual(readonly_status(path)["task_states"]["running"], 1)
                self.assertEqual(store.task("message")["backend_status"], "running")

    def test_doctor_never_resolves_a_credential_or_network(self):
        example = Path(__file__).resolve().parents[1] / "config/foundation.json.example"
        with patch("foundation.providers.os.getenv", side_effect=AssertionError("no credential lookup")), \
                patch("urllib.request.build_opener", side_effect=AssertionError("no network")):
            code, result = self.call(["doctor", "--config", str(example)])
        self.assertEqual(code, 0)
        self.assertEqual(result["default_backend"], "dot")
        self.assertEqual(result["backends"]["dot"]["status"], "BLOCKED")
        self.assertFalse(result["production_connected"])

    def test_inline_secrets_and_duplicate_settings_are_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text('{"version":1,"version":1,"default_backend":"dot","backends":{}}')
            with self.assertRaises(ValueError):
                load_config(path)
            value = {"version": 1, "default_backend": "openai", "backends": {"openai": {
                "type": "api", "enabled": True, "provider": "openai", "model": "configured-model",
                "credential_value": "generated-fixture-only"}}}
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_config(path)

    def test_all_ai_disabled_still_self_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"version": 1, "default_backend": "dot", "backends": {
                "dot": {"type": "dot", "enabled": False}}}))
            code, result = self.call(["doctor", "--config", str(path)])
            self.assertEqual(code, 0)
            self.assertFalse(result["default_backend_enabled"])
            self.assertEqual(result["backends"], {})

    def test_existing_unrelated_database_is_never_initialized_by_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "other.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE existing_transport(value TEXT)")
            db.commit()
            db.close()
            path.chmod(0o600)
            code, result = self.call(["demo", "--state", str(path)])
            self.assertEqual(code, 2)
            with sqlite3.connect(path) as check:
                self.assertEqual(check.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("existing_transport",)])
