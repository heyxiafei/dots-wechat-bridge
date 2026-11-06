#!/usr/bin/env python3
"""Verify a source export, without opening runtime state or contacting services."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_scanner", ROOT / "scripts/scan-public-bundle.py")
SCAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SCAN)


def verify_manifest(root):
    """Validate paths against the source allowlist before opening any entry."""
    root = Path(root).absolute()
    findings = SCAN.scan_tree(root)
    if findings:
        return {"ok": False, "stage": "privacy", "findings": findings}
    try:
        manifest_path = root / "PUBLIC-BUNDLE-MANIFEST.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("manifest_missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int
                or manifest["format_version"] != 1):
            raise ValueError("manifest_format")
        rows = manifest.get("files")
        if not isinstance(rows, list) or len(rows) > len(SCAN.PUBLIC_PATHS):
            raise ValueError("manifest_file_list")
        expected = set(SCAN.PUBLIC_PATHS) - {"PUBLIC-BUNDLE-MANIFEST.json"}
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"path", "bytes", "sha256"}:
                raise ValueError("manifest_entry")
            relative = row["path"]
            if not isinstance(relative, str) or relative not in expected or relative in seen:
                raise ValueError("manifest_path")
            if type(row["bytes"]) is not int or not 0 <= row["bytes"] <= SCAN.MAX_FILE_BYTES:
                raise ValueError("manifest_size")
            if not isinstance(row["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"]):
                raise ValueError("manifest_digest")
            seen.add(relative)
        if seen != expected:
            raise ValueError("manifest_incomplete")
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        if actual != set(SCAN.PUBLIC_PATHS):
            raise ValueError("file_set_mismatch")
        for row in rows:
            path = root / row["path"]
            for part in (path, *path.parents):
                if part == root:
                    break
                if part.is_symlink():
                    raise ValueError("manifest_symlink")
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("manifest_file_type")
            data = path.read_bytes()
            if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise ValueError("manifest_content_mismatch")
    except (OSError, ValueError, TypeError, KeyError):
        # A malformed manifest may contain private data; never echo its values.
        return {"ok": False, "stage": "manifest", "reason": "invalid_manifest_or_content"}
    return {"ok": True, "stage": "structure", "files": len(actual)}


def run_tests(root):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    os.chdir(root)
    suite = unittest.defaultTestLoader.discover(str(root / "probe_tests"))
    if (root / "foundation_tests").is_dir():
        optional = unittest.TestLoader().discover(str(root / "foundation_tests"), top_level_dir=str(root))
        suite = unittest.TestSuite([suite, optional])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    return {"ok": result.wasSuccessful() and result.testsRun > 0 and not result.skipped,
            "stage": "tests", "tests": result.testsRun, "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=ROOT)
    parser.add_argument("--structure-only", action="store_true", help="Check files, hashes and privacy only")
    args = parser.parse_args(argv)
    root = args.root.resolve() if not args.root.is_symlink() else args.root.absolute()
    result = verify_manifest(root)
    print(json.dumps(result), flush=True)
    if not result["ok"]:
        return 1
    if args.structure_only:
        return 0
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    schema = subprocess.run([sys.executable, "-B", str(root / "scripts/generate-schema.py"), "--check"],
                            cwd=root, env=env, check=False)
    print(json.dumps({"ok": schema.returncode == 0, "stage": "schema"}), flush=True)
    if schema.returncode:
        return 1
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    result = run_tests(root)
    print(json.dumps(result), flush=True)
    if not result["ok"]:
        return 1
    final = verify_manifest(root)
    print(json.dumps({**final, "stage": "post_test_structure"}), flush=True)
    return 0 if final["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
