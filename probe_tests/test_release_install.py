"""Verify the actual export and isolated Skill installation without network or services."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


INSTALL = load("release_skill_installer", "install-skill.py")
BUILD = load("release_actual_export", "build-public-bundle.py")


class ReleaseInstallationTests(unittest.TestCase):
    def test_real_export_contains_dependency_lock_and_trial_and_skill(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp).resolve() / "export"
            BUILD.build(ROOT, ROOT / "probe_tests/fixtures/rollback_baseline", output)
            self.assertEqual([], BUILD.SCAN.scan_tree(output))
            manifest = json.loads((output / "PUBLIC-BUNDLE-MANIFEST.json").read_text())
            entries = {item["path"]: item for item in manifest["files"]}
            for line in (output / "requirements.txt").read_text().splitlines():
                if line.startswith("-r "):
                    lock = output / line[3:].strip()
                    self.assertTrue(lock.is_file(), "referenced dependency lock omitted")
                    pins = [s for s in lock.read_text().splitlines() if s and not s.startswith("#")]
                    self.assertTrue(pins)
                    self.assertTrue(all("==" in pin for pin in pins))
            for rel in ("docs/SINGLE-SOURCE-TRIAL.md", "probe_tests/test_single_source_trial.py",
                        "skills/dot-wechat-bridge/SKILL.md", "scripts/install-skill.py"):
                self.assertIn(rel, entries)
                self.assertEqual(hashlib.sha256((ROOT / rel).read_bytes()).hexdigest(), entries[rel]["sha256"])

    def test_preview_is_readonly_and_install_matches_source_then_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            preview = INSTALL.install(base)
            self.assertFalse(preview["applied"])
            self.assertEqual([], list(base.iterdir()))
            result = INSTALL.install(base, apply=True)
            self.assertTrue(result["applied"])
            target = base / INSTALL.NAME
            for item in result["files"]:
                self.assertEqual(item["sha256"], hashlib.sha256((target / item["path"]).read_bytes()).hexdigest())
            marker = target / "keep.txt"
            marker.write_text("keep installed edits")
            with self.assertRaisesRegex(ValueError, "existing_skill_not_overwritten"):
                INSTALL.install(base, apply=True)
            self.assertEqual("keep installed edits", marker.read_text())

    def test_symlink_destination_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            dest = root / "real"
            dest.mkdir()
            alias = root / "alias"
            alias.symlink_to(dest, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "existing_real_directory"):
                INSTALL.install(alias, apply=True)
            self.assertEqual([], list(dest.iterdir()))


if __name__ == "__main__":
    unittest.main()
