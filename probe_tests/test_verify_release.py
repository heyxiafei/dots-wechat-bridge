"""Tampered release manifests must fail before their paths are opened."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_release_tests", ROOT / "scripts/verify-release.py")
VERIFY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFY)


class VerifyReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.data = b"public synthetic document\n"
        (self.root / "README.md").write_bytes(self.data)
        self.manifest = {"format_version": 1, "files": [{"path": "README.md",
            "bytes": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest()}]}
        self.write_manifest()
        self.addCleanup(patch.stopall)
        patch.object(VERIFY.SCAN, "PUBLIC_PATHS", frozenset({"README.md", "PUBLIC-BUNDLE-MANIFEST.json"})).start()
        # These unit cases isolate the manifest check; scanner adversarial cases
        # are independently covered by test_public_bundle and every release run.
        self.scan = patch.object(VERIFY.SCAN, "scan_tree", return_value=[]).start()

    def write_manifest(self):
        (self.root / "PUBLIC-BUNDLE-MANIFEST.json").write_text(json.dumps(self.manifest))

    def test_exact_manifest_and_bytes(self):
        self.assertEqual(VERIFY.verify_manifest(self.root), {"ok": True, "stage": "structure", "files": 2})
        (self.root / "README.md").write_bytes(b"changed\n")
        self.assertFalse(VERIFY.verify_manifest(self.root)["ok"])

    def test_paths_duplicates_and_missing_entries_are_rejected(self):
        original = self.manifest["files"][0].copy()
        for paths in (["../private"], ["/private"], ["docs/../../private"], ["README.md", "README.md"], []):
            with self.subTest(paths=paths):
                self.manifest["files"] = [{**original, "path": p} for p in paths]
                self.write_manifest()
                with patch.object(Path, "read_bytes", side_effect=AssertionError("must validate paths before content")):
                    self.assertFalse(VERIFY.verify_manifest(self.root)["ok"])

    def test_wrong_types_and_digests_do_not_reach_payload_read(self):
        original = self.manifest["files"][0].copy()
        for changes in ({"bytes": True}, {"bytes": -1}, {"sha256": None}, {"sha256": "not-a-digest"}, {"path": []}):
            with self.subTest(changes=changes):
                self.manifest["files"] = [{**original, **changes}]
                self.write_manifest()
                with patch.object(Path, "read_bytes", side_effect=AssertionError("invalid metadata opened file")):
                    self.assertFalse(VERIFY.verify_manifest(self.root)["ok"])

    def test_symlink_is_not_followed_even_after_scanner_check(self):
        (self.root / "README.md").unlink()
        (self.root / "README.md").symlink_to("PUBLIC-BUNDLE-MANIFEST.json")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("symlink content opened")):
            self.assertFalse(VERIFY.verify_manifest(self.root)["ok"])

    def test_unexpected_file_is_not_included(self):
        (self.root / "private.txt").write_text("synthetic private fixture")
        with patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected file opened")):
            self.assertFalse(VERIFY.verify_manifest(self.root)["ok"])

    def test_privacy_failure_stops_before_manifest_read(self):
        self.scan.return_value = [{"path": ".", "line": 0, "category": "private_directory"}]
        with patch.object(Path, "read_text", side_effect=AssertionError("manifest read after privacy refusal")):
            self.assertEqual(VERIFY.verify_manifest(self.root)["stage"], "privacy")

    def test_structure_failure_never_runs_schema_or_tests(self):
        with patch.object(VERIFY, "verify_manifest", return_value={"ok": False}), \
             patch.object(VERIFY.subprocess, "run") as schema, patch.object(VERIFY, "run_tests") as tests:
            self.assertEqual(VERIFY.main([str(self.root)]), 1)
            schema.assert_not_called()
            tests.assert_not_called()

    def test_empty_test_discovery_cannot_report_success(self):
        with patch.object(VERIFY.unittest.defaultTestLoader, "discover", return_value=unittest.TestSuite()), \
             patch.object(VERIFY.os, "chdir"), patch.object(VERIFY.sys, "path", list(VERIFY.sys.path)):
            result = VERIFY.run_tests(self.root)
        self.assertEqual(result["tests"], 0)
        self.assertFalse(result["ok"])

    def test_skip_is_not_full_verification(self):
        @unittest.skip("synthetic unavailable capability")
        def skipped():
            pass
        suite = unittest.TestSuite([unittest.FunctionTestCase(skipped)])
        with patch.object(VERIFY.unittest.defaultTestLoader, "discover", return_value=suite), \
             patch.object(VERIFY.os, "chdir"), patch.object(VERIFY.sys, "path", list(VERIFY.sys.path)):
            result = VERIFY.run_tests(self.root)
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
