"""Offline release guards. Canary strings are generated, never real credentials."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCAN = load_script("scan_public_bundle_tests", "scan-public-bundle.py")
BUILD = load_script("build_public_bundle_tests", "build-public-bundle.py")


def categories(findings):
    return {item["category"] for item in findings}


class PublicScannerTests(unittest.TestCase):
    def test_sensitive_canaries_are_rejected_without_echo(self):
        canaries = {
            "personal_absolute_path": "/" + "Users/synthetic-person/private/file.txt",
            "private_key": "-----BEGIN " + "PRIVATE KEY-----",
            "api_credential": "sk-" + "CANARY_ONLY_NEVER_VALID_" + "A" * 24,
            "webhook_credential": "whsec_" + "CANARY_ONLY_NEVER_VALID_" + "B" * 24,
            "jwt": "eyJ" + "A" * 15 + "." + "B" * 20 + "." + "C" * 20,
            "authorization_literal": "Bearer " + "CANARY_ONLY_" + "D" * 24,
            "cookie_literal": "Cookie: session=" + "CANARY_ONLY_" + "E" * 24,
            "credential_literal": "api_key = '" + "CANARY_ONLY_" + "F" * 24 + "'",
            "credential_query": "https://example.invalid/?X-Amz-Signature=" + "a" * 48,
            "wechat_identifier": "wxid_" + "canary_never_real_123456789",
            "message_identifier": "wx_" + "a" * 32,
            "tunnel_identifier": "tunnel_" + "a" * 32,
            "subscription_identifier": "sub_" + "canary_1234567890123456",
            "account_identifier": "org-" + "A" * 20,
            "uuid_identifier": "abcdefab-" + "cdef-abcd-efab-cdefabcdefab",
        }
        for category, canary in canaries.items():
            with self.subTest(category=category):
                findings = SCAN.scan_text("README.md", "safe\n" + canary)
                self.assertIn(category, categories(findings))
                self.assertTrue(all(set(item) == {"path", "line", "category"} for item in findings))
                self.assertNotIn(canary, json.dumps(findings))
                self.assertTrue(all(item["line"] == 2 for item in findings))

    def test_all_message_prefixes_and_signed_query_names(self):
        for prefix in ("wx", "Sentinel", "evt", "ack"):
            self.assertIn("message_identifier", categories(SCAN.scan_text("README.md", prefix + "_" + "b" * 32)))
        for parameter in ("X-Amz-Signature", "X-Goog-Signature", "x-oss-signature", "AWSAccessKeyId"):
            self.assertIn("credential_query", categories(SCAN.scan_text("README.md", "?" + parameter + "=" + "C" * 24)))

    def test_exact_synthetic_values_only(self):
        for value in SCAN.SYNTHETIC_IDENTIFIERS:
            self.assertEqual([], SCAN.scan_text("README.md", value), value)
        value = "tunnel_" + "0" * 32
        self.assertIn("tunnel_identifier", categories(SCAN.scan_text("README.md", value + "x")))
        self.assertTrue(SCAN.scan_text("probe_tests/test_weixin.py", "# synthetic fixture\nwxid_" + "unreviewed_123456789"))

    def test_encoded_literals_and_line_numbers(self):
        value = "/" + "home/synthetic-person/private"
        encoded = quote(value, safe="")
        for text in (encoded, quote(encoded, safe=""), value.replace("/", "&#47;"), value.replace("/", "\\u002f")):
            self.assertIn("personal_absolute_path", categories(SCAN.scan_text("README.md", text)))
        findings = SCAN.scan_text("README.md", "safe\r\nsafe\r\n" + value)
        self.assertEqual(3, findings[0]["line"])

    def test_windows_paths_and_diagnostic_path_redaction(self):
        for value in ("C:" + "\\Users\\synthetic-person\\file", "\\\\host\\Users\\synthetic-person"):
            self.assertIn("personal_absolute_path", categories(SCAN.scan_text("README.md", value)))
        value = "tunnel_" + "c" * 32
        result = SCAN.finding(value + ".txt", 0, "not_allowlisted")
        self.assertEqual("<redacted-path>", result["path"])
        self.assertNotIn(value, json.dumps(result))
        encoded_path = value.replace("t", "%74", 1)
        self.assertEqual("<redacted-path>", SCAN.finding(encoded_path, 0, "not_allowlisted")["path"])

    def test_binary_encoding_and_size_guards(self):
        for data in (b"a\x00b", b"SQLite format 3", b"\x89PNGabc", b"PK\x03\x04abc", b"\x1f\x8babc"):
            self.assertEqual({"binary_content"}, categories(SCAN.scan_bytes("README.md", data)))
        self.assertEqual({"non_utf8_content"}, categories(SCAN.scan_bytes("README.md", b"\xff")))
        with mock.patch.object(SCAN, "MAX_FILE_BYTES", 4):
            self.assertEqual({"file_too_large"}, categories(SCAN.scan_bytes("README.md", b"abcde")))

    def test_private_or_unexpected_files_are_not_opened(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in (".env", "credentials.json", "unexpected.txt"):
                (root / name).write_text("deliberately not read")
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected read")):
                found = SCAN.scan_tree(root)
            self.assertIn("private_path", categories(found))
            self.assertIn("hidden_path", categories(found))
            self.assertIn("not_allowlisted", categories(found))

    def test_symlink_file_directory_root_and_hardlink(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            root = parent / "bundle"
            root.mkdir()
            (root / "README.md").symlink_to(parent / "missing")
            (root / "docs").symlink_to(parent, target_is_directory=True)
            self.assertEqual({"symlink"}, categories(SCAN.scan_tree(root)))
            link = parent / "link"
            link.symlink_to(root, target_is_directory=True)
            self.assertEqual({"symlink"}, categories(SCAN.scan_tree(link)))
            (root / "README.md").unlink()
            (root / "docs").unlink()
            original = parent / "plain"
            original.write_text("safe")
            os.link(original, root / "README.md")
            self.assertEqual({"hardlink"}, categories(SCAN.scan_tree(root)))

    def test_empty_private_and_unexpected_directories_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "runtime").mkdir()
            (root / "unexpected").mkdir()
            self.assertEqual({"private_directory", "not_allowlisted_directory"}, categories(SCAN.scan_tree(root)))

    def test_walk_errors_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(SCAN.os, "walk", side_effect=PermissionError("synthetic permission failure")):
                self.assertEqual({"filesystem_error"}, categories(SCAN.scan_tree(temp)))

    def test_invalid_allowlist_paths_rejected(self):
        for value in ("../README.md", "/README.md", "docs\\file.md", "docs//file.md", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SCAN.validate_allowed_paths((value,))

    def test_r7_required_source_and_operator_configuration_are_exported(self):
        required = {
            "probe/owner_coordinator.py", "probe/official_usage_provider.py",
            "probe/workflow_contract.py", "probe/read_projection.py",
            "probe/aihot_notice_consumer/operator_config.py",
            "probe_tests/aihot_fixture.py", "probe_tests/test_aihot_operator_config.py",
            "probe_tests/test_attachment_scope_boundaries.py",
            "probe_tests/test_submission_preflight.py",
            "config/owner-coordinator.json.example", "config/setup-approval.json.example",
            "scripts/install-project.py", "docs/INSTALLATION.md",
            "docs/COORDINATOR-WIRING.md", "docs/QUOTA-DISPLAY.md",
            "docs/POCKET-DESIGN-REVIEW.md",
        }
        self.assertTrue(required <= set(SCAN.SOURCE_PATHS))
        self.assertEqual(len(SCAN.SOURCE_PATHS), len(set(SCAN.SOURCE_PATHS)))
        self.assertEqual((), SCAN.OPTIONAL_PATHS)
        for relative in ("runtime/profile.yaml", "evidence/report.json", "bin/tunnel-client",
                         "probe/aihot_notice_consumer/unreviewed.py"):
            self.assertTrue(SCAN.path_findings(relative))


class PublicBuilderTests(unittest.TestCase):
    def setUp(self):
        # Resolve the host temporary directory once (macOS may alias its parent).
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.source = self.parent / "source"
        self.baseline = self.source / SCAN.BASELINE_PREFIX
        self.source.mkdir()
        self.baseline.mkdir(parents=True)
        for relative in SCAN.SOURCE_PATHS + SCAN.OPTIONAL_PATHS:
            self.write(self.source, relative, b"# synthetic public source fixture\n")
        for relative in SCAN.HISTORICAL_FIXTURE_PATHS:
            self.write(self.source, relative, (ROOT / relative).read_bytes())
        # The builder pins these historical bytes in FIXTURE-PROVENANCE.json.
        # A synthetic stand-in would fail the hash check before the test reaches
        # the behavior it intends to exercise.
        for relative in SCAN.BASELINE_PATHS:
            self.write(self.source, relative, (ROOT / relative).read_bytes())

    @staticmethod
    def write(root, relative, data):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_dry_run_has_no_output_and_no_runtime_reads(self):
        self.write(self.source, "runtime/secret.key", b"not part of the fixed source allowlist")
        output = self.parent / "public"
        result = BUILD.build(self.source, self.baseline, output, dry_run=True)
        self.assertTrue(result["ok"])
        self.assertFalse(output.exists())

    def test_export_preserves_license_and_baseline_and_rebuilds_public_tree(self):
        original = (self.source / "LICENSE").read_bytes()
        output = self.parent / "public"
        result = BUILD.build(self.source, self.baseline, output)
        self.assertTrue(result["ok"])
        self.assertEqual([], SCAN.scan_tree(output))
        self.assertEqual(original, (output / "LICENSE").read_bytes())
        fixture = output / SCAN.BASELINE_PREFIX
        self.assertEqual((self.baseline / "probe/probe.py").read_bytes(), (fixture / "probe/probe.py").read_bytes())
        self.assertTrue(BUILD.build(output, fixture, dry_run=True)["ok"])
        manifest = json.loads((output / "PUBLIC-BUNDLE-MANIFEST.json").read_text())
        self.assertNotIn(str(self.parent), json.dumps(manifest))
        self.assertEqual("48709d9fc72e9bdf4037c0ae907a75a376fd6ff5",
                         manifest["release_basis"]["public_main_commit"])
        expected_paths = set(SCAN.SOURCE_PATHS) | set(SCAN.BASELINE_PATHS)
        self.assertEqual(expected_paths, {item["path"] for item in manifest["files"]})
        for item in manifest["files"]:
            data = (output / item["path"]).read_bytes()
            self.assertEqual(len(data), item["bytes"], item["path"])
            self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"], item["path"])
        entry = next(item for item in manifest["files"] if item["path"] == "LICENSE")
        self.assertEqual(hashlib.sha256(original).hexdigest(), entry["sha256"])

    def test_output_existing_or_overlapping_is_never_written(self):
        output = self.parent / "public"
        output.mkdir()
        marker = output / "marker"
        marker.write_text("keep")
        for target, category in ((output, "output_already_exists"), (self.source / "public", "overlapping_output")):
            with self.assertRaises(BUILD.BuildFailure) as caught:
                BUILD.build(self.source, self.baseline, target)
            self.assertIn(category, categories(caught.exception.findings))
        self.assertEqual("keep", marker.read_text())

    def test_sibling_output_with_lexical_parent_is_supported(self):
        output = self.source / ".." / "public"
        self.assertTrue(BUILD.build(self.source, self.baseline, output)["ok"])
        self.assertEqual([], SCAN.scan_tree(self.parent / "public"))

    def test_source_symlink_is_rejected(self):
        path = self.source / "README.md"
        path.unlink()
        path.symlink_to(self.baseline / "LICENSE")
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.baseline, dry_run=True)
        self.assertEqual({"symlink"}, categories(caught.exception.findings))

    def test_external_baseline_is_rejected(self):
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.parent / "unreviewed-baseline", dry_run=True)
        self.assertEqual({"fixture_external_source"}, categories(caught.exception.findings))

    def test_new_credential_in_known_test_is_not_normalized(self):
        relative = "probe_tests/test_dotledger.py"
        canary = "sk-" + "CANARY_ONLY_NEVER_VALID_" + "Q" * 24
        self.write(self.source, relative, b"# reviewed-looking filename is not authorization\n" + ("api_key = '" + canary + "'\n").encode())
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.baseline, dry_run=True)
        self.assertEqual({"unreviewed_fixture_content"}, categories(caught.exception.findings))
        self.assertNotIn(canary, json.dumps(caught.exception.findings))

    def test_new_private_binding_in_known_test_is_not_normalized(self):
        relative = "probe_tests/test_receipts.py"
        value = "/" + "Users/synthetic-person/private"
        self.write(self.source, relative, ("PRIVATE = Path('" + value + "')\n").encode())
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.baseline, dry_run=True)
        self.assertEqual({"unreviewed_fixture_content"}, categories(caught.exception.findings))

    def test_core_sensitive_canary_fails_without_normalization(self):
        canary = "whsec_" + "CANARY_ONLY_NEVER_VALID_" + "R" * 24
        self.write(self.source, "probe/probe.py", ("webhook_secret = '" + canary + "'\n").encode())
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.baseline, dry_run=True)
        self.assertIn("webhook_credential", categories(caught.exception.findings))
        self.assertNotIn(canary, json.dumps(caught.exception.findings))

    def test_reviewed_legacy_fixtures_and_public_forms(self):
        state = {}
        visited = set()
        for relative in SCAN.SOURCE_PATHS + SCAN.OPTIONAL_PATHS:
            if relative not in BUILD.REVIEWED_FIXTURES:
                continue
            visited.add(relative)
            raw = (ROOT / relative).read_bytes()
            public, changes = BUILD.normalize_fixture(relative, raw, state)
            self.assertEqual([], SCAN.scan_bytes(relative, public))
            again, repeated = BUILD.normalize_fixture(relative, public, {})
            self.assertEqual(public, again)
            self.assertEqual([], repeated)
            if changes:
                self.assertEqual(BUILD.REVIEWED_FIXTURES[relative]["sha256"], hashlib.sha256(raw).hexdigest())
            injected = raw + ("\napi_key = '" + "sk-" + "CANARY_ONLY_NEVER_VALID_" + "S" * 24 + "'\n").encode()
            with self.assertRaises(BUILD.BuildFailure):
                BUILD.normalize_fixture(relative, injected, {})
        self.assertEqual(set(BUILD.REVIEWED_FIXTURES), visited)

    def test_reviewed_fixture_expected_counts_are_enforced(self):
        relative = "probe_tests/test_dotledger.py"
        raw = ("value = '" + "sk-" + "CANARY_ONLY_NEVER_VALID_" + "T" * 24 + "'\n").encode()
        wrong = {"sha256": hashlib.sha256(raw).hexdigest(),
                 "findings": {"api_credential": 1}, "changes": {"api_credential": 99}}
        with mock.patch.dict(BUILD.REVIEWED_FIXTURES, {relative: wrong}):
            with self.assertRaises(BUILD.BuildFailure) as caught:
                BUILD.normalize_fixture(relative, raw, {})
        self.assertEqual({"fixture_normalization_count_mismatch"}, categories(caught.exception.findings))

    def test_historical_source_mutation_cannot_be_exported(self):
        relative = "probe_tests/fixtures/localcutover_baseline/probe/probe.py"
        self.write(self.source, relative, (self.source / relative).read_bytes() + b"\n# changed\n")
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.baseline, dry_run=True)
        self.assertEqual({"fixture_hash_mismatch"}, categories(caught.exception.findings))

    def test_missing_r7_runtime_module_is_not_silently_omitted(self):
        relative = "probe/owner_coordinator.py"
        path = self.source / relative
        if path.exists():
            path.unlink()
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.baseline, dry_run=True)
        self.assertEqual([{"path": relative, "line": 0,
                           "category": "missing_or_unreadable_source"}], caught.exception.findings)

    def test_p0_fixture_mutation_cannot_be_exported(self):
        relative = "probe_tests/fixtures/p0_receipt_baseline/probe/probe.py"
        self.write(self.source, relative, b"# changed historical input\n")
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.baseline, dry_run=True)
        self.assertEqual({"fixture_hash_mismatch"}, categories(caught.exception.findings))

    def test_provenance_cannot_authorize_replaced_historical_bytes(self):
        relative = "probe_tests/fixtures/p0_receipt_baseline/probe/probe.py"
        data = b"# substituted historical input\n"
        self.write(self.source, relative, data)
        metadata = json.loads((self.source / BUILD.HISTORICAL_PROVENANCE).read_text())
        for entry in metadata["files"]:
            if entry["path"] == relative:
                entry.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.write(self.source, BUILD.HISTORICAL_PROVENANCE,
                   (json.dumps(metadata, sort_keys=True) + "\n").encode())
        with self.assertRaises(BUILD.BuildFailure) as caught:
            BUILD.build(self.source, self.baseline, dry_run=True)
        self.assertEqual({"fixture_provenance_changed"}, categories(caught.exception.findings))


if __name__ == "__main__":
    unittest.main()
