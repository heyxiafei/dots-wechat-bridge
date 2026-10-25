"""Safe status must not mistake a protocol-granted open lease for expiry."""
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest


class SafeStatusLifetimeTests(unittest.TestCase):
    def test_null_and_finite_leases_are_counted_without_writing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "probe.sqlite"
            with sqlite3.connect(db) as connection:
                connection.executescript("""
                    CREATE TABLE weixin_accounts(status TEXT,last_poll REAL,last_error TEXT);
                    CREATE TABLE weixin_outbox(status TEXT);
                    CREATE TABLE subscriptions(name TEXT,expires REAL);
                """)
                connection.executemany("INSERT INTO subscriptions VALUES(?,?)", [
                    ("weixin.message", None), ("test.ping", time.time() + 3600),
                    ("expired", 0), ("expired", time.time() - 3600),
                ])
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            script = Path(__file__).resolve().parents[1] / "scripts" / "safe-status.py"
            result = subprocess.run([sys.executable, str(script), "--state-dir", directory],
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(result.stdout)
            self.assertEqual(status["active_subscriptions"], {"weixin.message": 1, "test.ping": 1})
            self.assertEqual(status["health"], "unavailable")
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
            self.assertFalse((root / "service-health.url").exists())


if __name__ == "__main__":
    unittest.main()
