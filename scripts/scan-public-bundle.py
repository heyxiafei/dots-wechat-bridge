#!/usr/bin/env python3
"""Fail-closed, offline checks for the deliberately small public source bundle.

Diagnostics contain a relative path, line number, and fixed category only.
This is a release guard, not proof that arbitrary source is safe to publish.
"""
import argparse
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import unquote


HISTORICAL_FIXTURE_MODULES = (
    "weixin.py", "login.py", "dotledger.py", "probe.py", "outbound.py",
    "event_delivery.py", "dotcutover.py", "transport_timing.py",
    "dotjournal.py", "dotepoch.py", "receipts.py", "media.py",
)
P0_RECEIPT_FIXTURE_PATHS = tuple(
    "probe_tests/fixtures/p0_receipt_baseline/probe/" + module
    for module in ("dotledger.py", "login.py", "media.py", "outbound.py", "probe.py", "weixin.py")
)
HISTORICAL_FIXTURE_PATHS = (
    "probe_tests/fixtures/FIXTURE-PROVENANCE.json",
) + tuple(
    f"probe_tests/fixtures/{version}/probe/{module}"
    for version in ("localcutover_baseline", "guarded_rollback")
    for module in HISTORICAL_FIXTURE_MODULES
) + tuple(
    f"probe_tests/fixtures/{version}/{name}"
    for version in ("localcutover_baseline", "guarded_rollback")
    for name in ("LICENSE", "NOTICE.md")
) + P0_RECEIPT_FIXTURE_PATHS


SOURCE_PATHS = (
    ".gitignore", "AGENTS.md", "LICENSE", "NOTICE.md", "README.md", "requirements.txt", "requirements.lock",
    "docs/SINGLE-SOURCE-TRIAL.md", "probe_tests/test_single_source_trial.py",
    "docs/STEADY-FUTURE-ADMISSION.md", "probe_tests/test_steady_future_admission.py",
    "docs/SKILL-INSTALLATION.md", "docs/VALIDATION.md", "scripts/install-skill.py", "probe_tests/test_release_install.py",
    "skills/dot-wechat-bridge/SKILL.md", "skills/dot-wechat-bridge/LICENSE",
    "skills/dot-wechat-bridge/references/setup.md",
    "skills/dot-wechat-bridge/references/operations.md",
    "skills/dot-wechat-bridge/references/single-source-trial.md",
    "licenses/Tencent-openclaw-weixin-MIT.txt", "licenses/wx-bot-MIT.txt",
    "config/dots-wechat-bridge.service.example", "config/tunnel-profile.yaml.example",
    "docs/DOT-MIRROR-LEDGER.md", "docs/MEDIA.md", "docs/OPERATIONS.md",
    "docs/P0-RECEIPTS.md", "docs/PUBLISHING.md", "docs/REPLY-WORKFLOW.md",
    "docs/SUBSCRIPTION-LIFETIME.md", "docs/mcp-schema.json",
    "probe/dotledger.py", "probe/event_delivery.py", "probe/login.py", "probe/media.py", "probe/outbound.py",
    "probe/probe.py", "probe/receipts.py", "probe/weixin.py", "probe/fixed_commands.py",
    "scripts/bootstrap.sh", "scripts/build-public-bundle.py", "scripts/generate-schema.py",
    "scripts/install-service.sh", "scripts/media-resource-check.py",
    "scripts/outbound-resource-check.py", "scripts/owner-wechat-login.py",
    "scripts/render-config.py", "scripts/render-qr.py", "scripts/safe-status.py",
    "scripts/scan-public-bundle.py", "scripts/set-runtime-key.py",
    "probe_tests/test_dotledger.py", "probe_tests/test_media.py",
    "probe_tests/test_outbound.py", "probe_tests/test_p0_receipt_integration.py",
    "probe_tests/test_p0_rollback.py", "probe_tests/test_probe.py",
    "probe_tests/test_public_bundle.py", "probe_tests/test_receipts.py",
    "probe_tests/test_reply_phases.py", "probe_tests/test_safe_status_lifetime.py",
    "probe_tests/test_setup.py", "probe_tests/test_subscription_lifetime.py",
    "probe_tests/test_video.py", "probe_tests/test_weixin.py", "probe_tests/test_fixed_commands.py",
    "probe_tests/test_fixture_integrity.py",
    "probe_tests/test_journal_receipt_projection.py",
) + HISTORICAL_FIXTURE_PATHS
# These previously optional modules are present in the reviewed public main.
# This integrated release requires them; a partial source tree must fail closed.
MERGED_MAIN_PATHS = (
    "probe/transport_timing.py", "probe_tests/test_event_pump.py",
    "probe_tests/test_transport_timing.py", "probe_tests/test_transport_timing_rpc.py", "docs/EVENT-TRANSPORT.md",
    "probe_tests/test_event_delivery_status.py", "probe_tests/test_ledger_reconciliation.py",
    "docs/EVENT-RECONCILIATION.md",
    "probe/dotjournal.py", "probe/dotepoch.py", "probe_tests/test_dotjournal.py",
    "probe/dotcutover.py", "probe_tests/test_dotcutover.py",
    "probe/dotcompleteness.py", "probe_tests/test_dotcompleteness.py",
    "probe_tests/test_inbound_completeness.py", "docs/RELIABILITY.md",
    "probe_tests/test_reliability_compatibility.py",
    "docs/RELIABILITY-DEPLOYMENT.md",
    "docs/DOT-MIRROR-JOURNAL.md",
)
# Explicit union with the frozen r7 candidate, plus the separately reviewed
# public operator configuration. Never discover exportable files with a glob.
R7_ADDITION_PATHS = (
    "probe/connection_probe.py", "scripts/connection-probe.py",
    "probe_tests/test_connection_probe.py", "docs/CONNECTION-PROBE.md",
    "probe/bounded_dot_stdio.py", "scripts/bounded-dot-stdio.py",
    "probe_tests/test_bounded_dot_stdio.py", "docs/BOUNDED-DOT-STDIO.md",
    "probe/new_message_check.py", "scripts/check-new-message.py",
    "probe_tests/test_new_message_check.py", "docs/NEW-MESSAGE-CHECK.md",
    "config/owner-coordinator.json.example", "config/setup-approval.json.example",
    "docs/AUTHORIZATION-HANDOFF.md", "docs/COORDINATOR-WIRING.md",
    "docs/DELIVERABLES.md", "docs/HELP-APPROVED.txt", "docs/IMAGE-PAGES.md",
    "docs/INSTALLATION.md", "docs/POCKET-DESIGN-REVIEW.md", "docs/QUOTA-DISPLAY.md",
    "probe/aihot_email.py", "probe/aihot_notice_consumer/__init__.py",
    "probe/aihot_notice_consumer/auto_resume.py", "probe/aihot_notice_consumer/bridge_hook.py",
    "probe/aihot_notice_consumer/control.py", "probe/aihot_notice_consumer/operator_config.py",
    "probe/aihot_notice_consumer/owner_host.py", "probe/aihot_notice_consumer/relay.py",
    "probe/aihot_notice_consumer/store_helpers.py", "probe/aihot_notice_consumer/text_b.py",
    "probe/aihot_notice_consumer/wechat_adapter.py", "probe/deliverable_contract.py",
    "probe/official_usage_provider.py", "probe/owner_coordinator.py", "probe/quotes.py",
    "probe/read_projection.py", "probe/usage_display.py", "probe/workflow_contract.py",
    "probe_tests/aihot_fixture.py", "probe_tests/test_activation_continuity.py",
    "probe_tests/test_aihot_auto_resume.py", "probe_tests/test_aihot_operator_config.py",
    "probe_tests/test_aihot_shared_mail.py", "probe_tests/test_attachment_scope_boundaries.py",
    "probe_tests/test_coordinator_wiring.py", "probe_tests/test_deliverable_contract.py",
    "probe_tests/test_fixed_command_handler.py", "probe_tests/test_help_menu_copy.py",
    "probe_tests/test_image_diagnostics.py", "probe_tests/test_image_page_rpc.py",
    "probe_tests/test_image_pages.py", "probe_tests/test_install_product.py",
    "probe_tests/test_official_usage_provider_gate.py", "probe_tests/test_original_deliverables.py",
    "probe_tests/test_read_projection.py", "probe_tests/test_read_projection_safety.py",
    "probe_tests/test_reply_adapter_boundaries.py", "probe_tests/test_review_regressions.py",
    "probe_tests/test_safe_status_boundaries.py", "probe_tests/test_send_class_boundaries.py",
    "probe_tests/test_submission_preflight.py", "probe_tests/test_usage_display.py",
    "probe_tests/test_workflow_contract.py", "scripts/install-project.py",
)
SOURCE_PATHS += MERGED_MAIN_PATHS + R7_ADDITION_PATHS
OPTIONAL_PATHS = ()  # Compatibility name; all reviewed sources are required in r7.
BASELINE_NAMES = ("dotledger.py", "login.py", "media.py", "outbound.py", "probe.py", "receipts.py", "weixin.py")
BASELINE_PREFIX = "probe_tests/fixtures/rollback_baseline/"
BASELINE_PATHS = tuple(BASELINE_PREFIX + "probe/" + name for name in BASELINE_NAMES) + (
    BASELINE_PREFIX + "LICENSE", BASELINE_PREFIX + "NOTICE.md",
)
GENERATED_PATHS = ("PUBLIC-BUNDLE-MANIFEST.json",)
PUBLIC_PATHS = frozenset(SOURCE_PATHS + OPTIONAL_PATHS + BASELINE_PATHS + GENERATED_PATHS)
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024

# Finite, visibly synthetic values. Neither a comment nor a test directory exempts data.
SYNTHETIC_IDENTIFIERS = frozenset({
    "tunnel_" + "0" * 32,
    "wxid_fixture_owner", "wxid_fixture_other", "wxid_synthetic_owner",
    "fixture-owner@im.wechat", "fixture-other@im.wechat", "fixture-bot@im.bot",
    "sub_fixture_0000000000000000", "subscription_fixture_0000000000000000",
    "00000000-0000-0000-0000-000000000000",
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
    "wx_" + "0" * 32, "wx_" + "1" * 32,
    "Sentinel_" + "0" * 32, "Sentinel_" + "1" * 32,
    "evt_" + "0" * 32, "evt_" + "1" * 32,
    "ack_" + "0" * 32, "ack_" + "1" * 32,
    "sk-" + "SYNTHETIC_FIXTURE_00000000",
})
PRIVATE_COMPONENTS = frozenset({
    ".git", ".venv", "runtime", "state", "secrets", "logs", "bin", "__pycache__",
    ".aws", ".ssh", ".openclaw", "node_modules",
})
PRIVATE_NAMES = re.compile(r"(?i)(?:^\.env(?:\..*)?$|credentials?(?:\..*)?$|auth\.json$|runtime\.key$|weixin-login|weixin-qr|.*\.(?:sqlite3?|db|log|pem|p12|pfx|key|png|jpe?g|gif|webp|zip|tar|gz|dylib|so|pyc)$)")
CONTENT_RULES = (
    ("personal_absolute_path", re.compile(r"(?:/(?:Users|home)/[A-Za-z0-9._-]+(?:/[^\s\"'<>]*)?|[A-Za-z]:[\\/]Users[\\/][A-Za-z0-9._-]+|\\\\[A-Za-z0-9._-]+\\(?:Users|home)\\[A-Za-z0-9._-]+)")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("api_credential", re.compile(r"\b(?:sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("webhook_credential", re.compile(r"\bwhsec_[A-Za-z0-9_+/=-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{9,}\.[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\b")),
    ("authorization_literal", re.compile(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9_+/=-]{20,}")),
    ("cookie_literal", re.compile(r"(?i)\bCookie:\s*(?:session|auth|token)[A-Za-z0-9_-]*=[A-Za-z0-9_+/=.-]{16,}")),
    ("credential_literal", re.compile(r"(?i)([\"'])(?:api[_-]?key|access[_-]?token|bot[_-]?token|context[_-]?token|cookie|secret|password|ticket)\1\s*:\s*([\"'])([A-Za-z0-9_+/=.-]{20,})\2")),
    ("credential_literal", re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|bot[_-]?token|context[_-]?token|cookie|secret|password|ticket)\s*=\s*([\"'])([A-Za-z0-9_+/=.-]{20,})\1")),
    ("credential_query", re.compile(r"(?i)[?&](?:token|key|signature|sig|ticket|code|x-amz-signature|x-goog-signature|x-oss-signature|awsaccesskeyid)=[A-Za-z0-9_+/%=.-]{12,}")),
    ("wechat_identifier", re.compile(r"\b(?:wxid_[A-Za-z0-9_-]{6,}|gh_[A-Za-z0-9]{10,}|[A-Za-z0-9._-]+@im\.(?:wechat|bot)|[0-9]{6,}@chatroom)\b")),
    ("message_identifier", re.compile(r"\b(?:wx|Sentinel|evt|ack)_[a-fA-F0-9]{16,64}\b")),
    ("tunnel_identifier", re.compile(r"\btunnel_[A-Za-z0-9_-]{16,}\b")),
    ("subscription_identifier", re.compile(r"\b(?:sub|subscription|mcp_sub)_(?=[A-Za-z0-9_-]{12,}\b)(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]+\b")),
    ("account_identifier", re.compile(r"\b(?:org-[A-Za-z0-9]{16,}|proj_[A-Za-z0-9]{16,}|(?:libfile|file|plugin_asdk_app|asdk_app)_[a-fA-F0-9]{20,})\b")),
    ("uuid_identifier", re.compile(r"\b[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\b")),
)


def decode_for_scan(value):
    for _ in range(2):
        previous = value
        value = html.unescape(unquote(value))
        value = re.sub(r"\\u([0-9a-fA-F]{4})|\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1) or m.group(2), 16)), value)
        if value == previous:
            break
    return value


def safe_display_path(relative):
    decoded = decode_for_scan(relative)
    if any(pattern.search(decoded) for category, pattern in CONTENT_RULES):
        return "<redacted-path>"
    return relative.replace("\n", "<newline>").replace("\r", "<return>")


def finding(path, line, category):
    return {"path": safe_display_path(str(path)), "line": line, "category": category}


def validate_allowed_paths(paths):
    for path in paths:
        p = PurePosixPath(path)
        if not path or p.is_absolute() or ".." in p.parts or "\\" in path or str(p) != path:
            raise ValueError("invalid_allowlist")


def scan_text(relative, text):
    result = []
    for number, line in enumerate(text.splitlines(), 1):
        decoded = decode_for_scan(line)
        for category, pattern in CONTENT_RULES:
            matches = list(pattern.finditer(decoded))
            def synthetic(match):
                if category == "credential_literal":
                    value = next((g for g in reversed(match.groups()) if g not in (None, "'", '"')), "")
                    return value in {"synthetic-token-fixture", "synthetic-secret-fixture", "synthetic-secret-fixture-0000000000000000"}
                return match.group(0) in SYNTHETIC_IDENTIFIERS
            if matches and any(not synthetic(m) for m in matches):
                result.append(finding(relative, number, category))
    return result


def scan_bytes(relative, data):
    if len(data) > MAX_FILE_BYTES:
        return [finding(relative, 0, "file_too_large")]
    if b"\x00" in data or data.startswith((b"SQLite format 3", b"\x89PNG", b"PK\x03\x04", b"\x1f\x8b")):
        return [finding(relative, 0, "binary_content")]
    try:
        return scan_text(relative, data.decode("utf-8"))
    except UnicodeError:
        return [finding(relative, 0, "non_utf8_content")]


def path_findings(relative, allowed_paths=PUBLIC_PATHS):
    p = PurePosixPath(relative)
    result = []
    if any(part in PRIVATE_COMPONENTS for part in p.parts) or PRIVATE_NAMES.fullmatch(p.name):
        result.append(finding(relative, 0, "private_path"))
    if any(part.startswith(".") for part in p.parts) and relative != ".gitignore":
        result.append(finding(relative, 0, "hidden_path"))
    if relative not in allowed_paths:
        result.append(finding(relative, 0, "not_allowlisted"))
    return result


def scan_tree(root, allowed_paths=PUBLIC_PATHS):
    validate_allowed_paths(allowed_paths)
    root = Path(root).absolute()
    if root.is_symlink():
        return [finding(".", 0, "symlink")]
    if not root.is_dir():
        return [finding(".", 0, "missing_root")]
    result, total = [], 0
    try:
        def walk_error(error):
            raise error
        for current, directories, files in os.walk(root, followlinks=False, onerror=walk_error):
            for name in list(directories):
                p = Path(current) / name
                relative = p.relative_to(root).as_posix()
                if p.is_symlink():
                    result.append(finding(relative, 0, "symlink"))
                    directories.remove(name)
                elif name in PRIVATE_COMPONENTS or name.startswith("."):
                    result.append(finding(relative, 0, "private_directory"))
                    directories.remove(name)
                elif not any(item.startswith(relative + "/") for item in allowed_paths):
                    result.append(finding(relative, 0, "not_allowlisted_directory"))
                    directories.remove(name)
            for name in files:
                p = Path(current) / name
                relative = p.relative_to(root).as_posix()
                if p.is_symlink():
                    result.append(finding(relative, 0, "symlink"))
                    continue
                failures = path_findings(relative, allowed_paths)
                if failures:
                    result.extend(failures)
                    continue  # Do not open unexpected or credential files.
                metadata = p.stat()
                if not stat.S_ISREG(metadata.st_mode):
                    result.append(finding(relative, 0, "not_regular_file"))
                    continue
                if metadata.st_nlink > 1:
                    result.append(finding(relative, 0, "hardlink"))
                    continue
                if metadata.st_size > MAX_FILE_BYTES:
                    result.append(finding(relative, 0, "file_too_large"))
                    continue
                total += metadata.st_size
                if total > MAX_TOTAL_BYTES:
                    result.append(finding(".", 0, "bundle_too_large"))
                    return result
                result.extend(scan_bytes(relative, p.read_bytes()))
    except OSError:
        result.append(finding(".", 0, "filesystem_error"))
    return sorted(result, key=lambda f: (f["path"], f["line"], f["category"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    failures = scan_tree(args.root)
    if args.json:
        print(json.dumps({"ok": not failures, "findings": failures}, sort_keys=True))
    else:
        for item in failures:
            print("{path}:{line}:{category}".format(**item))
        if not failures:
            print("scan: ok")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
