"""Historical compatibility inputs are complete, immutable test-only source."""
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "probe_tests/fixtures/FIXTURE-PROVENANCE.json"
SCAN_SPEC = importlib.util.spec_from_file_location("fixture_allowlist", ROOT / "scripts/scan-public-bundle.py")
SCAN = importlib.util.module_from_spec(SCAN_SPEC)
SCAN_SPEC.loader.exec_module(SCAN)


class FixtureIntegrityTests(unittest.TestCase):
    def test_bundled_compatibility_sources_match_reviewed_hashes(self):
        metadata = json.loads(MANIFEST.read_text())
        entries = metadata["files"]
        self.assertEqual(len(entries), 37)
        self.assertEqual(len({entry["path"] for entry in entries}), len(entries))
        self.assertEqual({entry["path"] for entry in entries},
                         (set(SCAN.HISTORICAL_FIXTURE_PATHS) - {"probe_tests/fixtures/FIXTURE-PROVENANCE.json"})
                         | set(SCAN.BASELINE_PATHS))
        for entry in entries:
            relative = Path(entry["path"])
            self.assertGreaterEqual(len(relative.parts), 4)
            self.assertFalse(relative.is_absolute() or ".." in relative.parts)
            self.assertEqual(relative.parts[:2], ("probe_tests", "fixtures"))
            self.assertIn(relative.parts[2], ("rollback_baseline", "localcutover_baseline", "guarded_rollback"))
            path = ROOT / relative
            self.assertTrue(path.is_file() and not path.is_symlink(), entry["path"])
            content = path.read_bytes()
            self.assertEqual(len(content), entry["bytes"], entry["path"])
            self.assertEqual(hashlib.sha256(content).hexdigest(), entry["sha256"], entry["path"])
        for version, expected in metadata["source_versions"].items():
            source = (ROOT / "probe_tests/fixtures" / version / "probe/probe.py").read_text()
            self.assertIn("BUILD_VERSION = '" + expected + "'", source)


if __name__ == "__main__":
    unittest.main()
