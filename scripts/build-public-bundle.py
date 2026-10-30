#!/usr/bin/env python3
"""Build an offline public-source directory from a fixed, reviewed allowlist.

No network, deployment, archive discovery, Git history, or runtime reads occur.
Run --dry-run before a frozen-source export. Existing outputs are never replaced.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("public_bundle_scanner", ROOT / "scripts/scan-public-bundle.py")
SCAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCAN)

# Each legacy fixture is approved as a complete byte sequence, never by directory
# or pattern alone. Hashes are review gates, not hashes of individual secrets.
REVIEWED_FIXTURES = {
    "probe_tests/test_dotledger.py": {
        "sha256": "c9daf5121a459cce47bfaa01a140a6d3e22274859379a5d0688b9492008a0a48",
        "findings": {"api_credential": 1}, "changes": {"api_credential": 1}},
    "probe_tests/test_p0_receipt_integration.py": {
        "sha256": "3a30d0edaa524dccad3839138986e2562f3bf031fdb676becd8420cee787f544",
        "findings": {"credential_literal": 3}, "changes": {"credential_literal": 3}},
    "probe_tests/test_p0_rollback.py": {
        "sha256": "1b5099dfb47f13777537f796a176c6bb2bc811ea336a6fd53370898f59909b1e",
        "findings": {"uuid_identifier": 1}, "changes": {"uuid_identifier": 1}},
    "probe_tests/test_receipts.py": {
        "sha256": "d804920c5f5bee6c74d419e1c978c0db4cd84480d01b35e0848bc10e84843d12",
        "findings": {"message_identifier": 1, "uuid_identifier": 2},
        "changes": {"message_identifier": 2, "uuid_identifier": 2}},
    "probe_tests/test_subscription_lifetime.py": {
        "sha256": "9030ef55d7826885238942d6a10826cba23eba92a163d812390ca338978c7458",
        "findings": {"credential_literal": 1}, "changes": {"credential_literal": 1}},
    "probe_tests/test_weixin.py": {
        "sha256": "ab9333e6e16803532f87da1966df977dfcfd72d088829041ef30791c853e8a43",
        "findings": {"wechat_identifier": 1}, "changes": {"wechat_identifier": 1}},
    "probe_tests/test_event_pump.py": {
        "sha256": "9f678563ed263695d7639c4213842613bdae93e040b7a7f90e1ba612c1e37516",
        "findings": {"credential_literal": 2}, "changes": {"credential_literal": 2}},
    "probe_tests/test_transport_timing_rpc.py": {
        "sha256": "79e27a59fa161542e1dc8f803b09bf1145bfdd14c39c7b4ba10b78e1e1fa4499",
        "findings": {"credential_literal": 2}, "changes": {"credential_literal": 2}},
}
FIXTURE_NORMALIZATION_PATHS = frozenset(REVIEWED_FIXTURES)
SYNTHETIC_UUIDS = (
    "00000000-0000-0000-0000-000000000000",
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
)
NORMALIZED_CREDENTIAL = "synthetic-secret-fixture-0000000000000000"
HISTORICAL_PROVENANCE = "probe_tests/fixtures/FIXTURE-PROVENANCE.json"
REVIEWED_HISTORICAL_PROVENANCE_SHA256 = "a6a666c54d095f5b73f36d976768a75915f2be848c3ca1a358737df01197791c"


class BuildFailure(Exception):
    def __init__(self, findings):
        self.findings = findings


def checked_source(root, relative):
    SCAN.validate_allowed_paths((relative,))
    path = root
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise BuildFailure([SCAN.finding(relative, 0, "symlink")])
    try:
        info = path.stat()
    except OSError:
        raise BuildFailure([SCAN.finding(relative, 0, "missing_or_unreadable_source")])
    if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
        raise BuildFailure([SCAN.finding(relative, 0, "unsafe_source_type")])
    if info.st_size > SCAN.MAX_FILE_BYTES:
        raise BuildFailure([SCAN.finding(relative, 0, "file_too_large")])
    try:
        return path.read_bytes()
    except OSError:
        raise BuildFailure([SCAN.finding(relative, 0, "source_read_failed")])


def normalize_fixture(relative, data, state):
    """An export-only migration of a finite set of reviewed legacy test files."""
    if relative not in FIXTURE_NORMALIZATION_PATHS:
        return data, []
    raw_findings = SCAN.scan_bytes(relative, data)
    if not raw_findings:
        return data, []  # An already-public, clean fixture can be rebuilt verbatim.
    review = REVIEWED_FIXTURES[relative]
    counts = {}
    for item in raw_findings:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    if hashlib.sha256(data).hexdigest() != review["sha256"] or counts != review["findings"]:
        raise BuildFailure([SCAN.finding(relative, 0, "unreviewed_fixture_content")])
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return data, []  # scanner supplies a non-content diagnostic
    changes = []

    def replacement(category, value):
        if value in SCAN.SYNTHETIC_IDENTIFIERS:
            return value
        mapping = state.setdefault(category, {})
        if value in mapping:
            return mapping[value]
        if category == "uuid_identifier":
            if len(mapping) >= len(SYNTHETIC_UUIDS):
                raise BuildFailure([SCAN.finding(relative, 0, "fixture_normalization_limit")])
            result = SYNTHETIC_UUIDS[len(mapping)]
        elif category == "message_identifier":
            prefix = value.split("_", 1)[0]
            same_prefix = sum(v.startswith(prefix + "_") for v in mapping.values())
            if same_prefix >= 2:
                raise BuildFailure([SCAN.finding(relative, 0, "fixture_normalization_limit")])
            result = prefix + "_" + str(same_prefix) * 32
        elif category == "wechat_identifier":
            if len(mapping) >= 2:
                raise BuildFailure([SCAN.finding(relative, 0, "fixture_normalization_limit")])
            result = ("wxid_fixture_owner", "wxid_fixture_other")[len(mapping)]
        elif category == "api_credential":
            result = "sk-" + "SYNTHETIC_FIXTURE_00000000"
        else:
            raise BuildFailure([SCAN.finding(relative, 0, "unsupported_fixture_normalization")])
        mapping[value] = result
        return result

    for category, pattern in SCAN.CONTENT_RULES:
        if category in {"uuid_identifier", "message_identifier", "wechat_identifier", "api_credential"}:
            count = 0
            def replace_identifier(match):
                nonlocal count
                result = replacement(category, match.group(0))
                count += result != match.group(0)
                return result
            text = pattern.sub(replace_identifier, text)
            if count:
                changes.append({"category": category, "count": count})
        elif category == "credential_literal":
            count = 0
            def replace_credential(match):
                nonlocal count
                index = 3 if len(match.groups()) == 3 else 2
                value = match.group(index)
                if value in {"synthetic-token-fixture", "synthetic-secret-fixture", NORMALIZED_CREDENTIAL}:
                    return match.group(0)
                count += 1
                a, b = match.span(index)
                return match.group(0)[:a-match.start()] + NORMALIZED_CREDENTIAL + match.group(0)[b-match.start():]
            text = pattern.sub(replace_credential, text)
            if count:
                changes.append({"category": category, "count": count})
    # This exact legacy PRIVATE binding is the only personal-path normalization.
    private = re.compile(r"(?m)^(\s*PRIVATE\s*=\s*Path\()([\"'])(/(?:Users|home)/[^\"'\n]+)\2(\)\s*)$")
    text, count = private.subn(lambda m: m.group(1) + '"/tmp/dots-bridge-synthetic-private"' + m.group(4), text)
    if count:
        changes.append({"category": "legacy_private_fixture_path", "count": count})
    change_counts = {}
    for change in changes:
        category = change["category"]
        change_counts[category] = change_counts.get(category, 0) + change["count"]
    if change_counts != review["changes"]:
        raise BuildFailure([SCAN.finding(relative, 0, "fixture_normalization_count_mismatch")])
    return text.encode("utf-8"), changes


def check_historical_fixtures(payloads):
    """Pin the reviewed historical test inputs, not merely their filenames."""
    raw = payloads[HISTORICAL_PROVENANCE]
    if hashlib.sha256(raw).hexdigest() != REVIEWED_HISTORICAL_PROVENANCE_SHA256:
        raise BuildFailure([SCAN.finding(HISTORICAL_PROVENANCE, 0, "fixture_provenance_changed")])
    try:
        entries = json.loads(raw)["files"]
        paths = {item["path"] for item in entries}
        expected = (set(SCAN.HISTORICAL_FIXTURE_PATHS) - {HISTORICAL_PROVENANCE}) | set(SCAN.BASELINE_PATHS)
        if len(entries) != len(paths) or paths != expected:
            raise ValueError("fixture list mismatch")
        for item in entries:
            data = payloads[item["path"]]
            if len(data) != item["bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise ValueError("fixture bytes mismatch")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise BuildFailure([SCAN.finding(HISTORICAL_PROVENANCE, 0, "fixture_hash_mismatch")])


def collect(source, baseline):
    if source.is_symlink() or baseline.is_symlink():
        raise BuildFailure([SCAN.finding(".", 0, "symlink_root")])
    payloads, normalizations, fixture_state = {}, [], {}
    for relative in SCAN.SOURCE_PATHS + SCAN.OPTIONAL_PATHS:
        path = source / relative
        if relative == ".gitignore" or relative in SCAN.OPTIONAL_PATHS:
            if not path.exists() and not path.is_symlink():
                continue
        data = checked_source(source, relative)
        data, changes = normalize_fixture(relative, data, fixture_state)
        payloads[relative] = data
        for change in changes:
            normalizations.append({"path": relative, **change})
    for name in SCAN.BASELINE_NAMES:
        relative = SCAN.BASELINE_PREFIX + "probe/" + name
        payloads[relative] = checked_source(baseline, "probe/" + name)
    for name in ("LICENSE", "NOTICE.md"):
        payloads[SCAN.BASELINE_PREFIX + name] = checked_source(baseline, name)
    check_historical_fixtures(payloads)
    failures = []
    for relative, data in payloads.items():
        failures.extend(SCAN.path_findings(relative))
        failures.extend(SCAN.scan_bytes(relative, data))
    if sum(len(value) for value in payloads.values()) > SCAN.MAX_TOTAL_BYTES:
        failures.append(SCAN.finding(".", 0, "bundle_too_large"))
    if failures:
        raise BuildFailure(failures)
    manifest = {
        "format_version": 1,
        "purpose": "public source bundle; no deployment or end-to-end success claim",
        "release_basis": {
            "public_repository": "heyxiafei/dots-wechat-bridge",
            "public_main_commit": "48709d9fc72e9bdf4037c0ae907a75a376fd6ff5",
            "candidate": "r7 offline candidate plus reviewed public integration changes",
            "scope": "Explicit union of reviewed public main and r7 source paths, plus public operator configuration and tests",
        },
        "source_policy": "fixed allowlist; no Git history, runtime, state, credentials, binaries or network",
        "fixture_policy": "whole-file reviewed SHA256 and counts required for legacy replacement; clean public fixtures unchanged; core unchanged",
        "normalizations": normalizations,
        "files": [{"path": key, "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}
                  for key, value in sorted(payloads.items())],
    }
    payloads["PUBLIC-BUNDLE-MANIFEST.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    failures = SCAN.scan_bytes("PUBLIC-BUNDLE-MANIFEST.json", payloads["PUBLIC-BUNDLE-MANIFEST.json"])
    if failures:
        raise BuildFailure(failures)
    return payloads, manifest


def build(source, baseline, output=None, dry_run=False):
    # Normalize lexical '..' before overlap checks without following symlinks.
    source, baseline = Path(os.path.abspath(source)), Path(os.path.abspath(baseline))
    if baseline != source / SCAN.BASELINE_PREFIX:
        raise BuildFailure([SCAN.finding(".", 0, "fixture_external_source")])
    if not dry_run:
        if output is None:
            raise BuildFailure([SCAN.finding(".", 0, "output_required")])
        output = Path(os.path.abspath(output))
        if output.exists() or output.is_symlink():
            raise BuildFailure([SCAN.finding(".", 0, "output_already_exists")])
        # Refuse overlapping source trees and symbolic-link destination ancestors.
        for protected in (source, baseline):
            if output == protected or output in protected.parents or protected in output.parents:
                raise BuildFailure([SCAN.finding(".", 0, "overlapping_output")])
        if any(parent.is_symlink() for parent in (output.parent, *output.parent.parents)):
            raise BuildFailure([SCAN.finding(".", 0, "symlink_output_parent")])
        if not output.parent.is_dir():
            raise BuildFailure([SCAN.finding(".", 0, "missing_output_parent")])
    payloads, manifest = collect(source, baseline)
    if not dry_run:
        staging = Path(tempfile.mkdtemp(prefix=".public-bundle-", dir=output.parent))
        try:
            for relative, data in sorted(payloads.items()):
                target = staging / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                target.chmod(0o755 if relative.startswith("scripts/") and relative.endswith(".sh") else 0o644)
            failures = SCAN.scan_tree(staging)
            if failures:
                raise BuildFailure(failures)
            staging.rename(output)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return {"ok": True, "dry_run": dry_run, "file_count": len(payloads),
            "normalizations": manifest["normalizations"], "findings": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = build(args.source_root, args.source_root / SCAN.BASELINE_PREFIX,
                       args.output, args.dry_run)
    except BuildFailure as error:
        report = {"ok": False, "dry_run": args.dry_run, "findings": error.findings}
    except (OSError, ValueError):
        report = {"ok": False, "dry_run": args.dry_run,
                  "findings": [SCAN.finding(".", 0, "build_failed")]}
    if args.report:
        try:
            fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump(report, handle, indent=2, sort_keys=True)
                handle.write("\n")
        except OSError:
            report = {"ok": False, "findings": [SCAN.finding(".", 0, "report_write_failed")]}
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
