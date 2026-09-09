import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _running_under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


class CleanCheckoutGateTests(unittest.TestCase):
    def test_nested_gate_pytest_guard_uses_only_exact_runtime_marker(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(_running_under_pytest())
        with patch.dict(os.environ, {"PYTEST_CURRENT_TEST": "collection::case (call)"}, clear=True):
            self.assertTrue(_running_under_pytest())

    def test_makefile_separates_clean_and_extended_gates(self):
        body = (ROOT / "Makefile").read_text()
        self.assertIn("check-extended:", body)
        self.assertIn("verify-private-calibration-assets.py --check", body)
        self.assertIn("AO_LORE_PRIVATE_CALIBRATION=1", body)

    def test_clean_test_runner_uses_unique_runtime_per_invocation(self):
        body = (ROOT / "scripts/run-clean-tests.py").read_text(encoding="utf-8")
        self.assertIn("TemporaryDirectory", body)
        self.assertIn('"AO_LORE_HOME"', body)
        self.assertNotIn('RUNTIME = ROOT / ".ao-lore"', body)

    def test_extended_gate_refuses_without_sealed_asset_manifest(self):
        with tempfile.TemporaryDirectory() as name:
            environment = os.environ.copy()
            environment["AO_LORE_HOME"] = name
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/verify-private-calibration-assets.py"), "--check"],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
            )
        self.assertEqual(2, result.returncode)
        self.assertEqual("private calibration assets are not installed\n", result.stderr)

    def test_private_calibration_is_an_explicit_skip_when_assets_are_absent(self):
        environment = os.environ.copy()
        environment.pop("AO_LORE_PRIVATE_CALIBRATION", None)
        result = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_ao_lore_candidate_quality_transaction", "-v"],
            cwd=ROOT,
            env={**environment, "PYTHONPATH": "src"},
            text=True,
            capture_output=True,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("private calibration assets are not installed", result.stderr)

    @unittest.skipIf(os.environ.get("AO_LORE_CLEAN_GATE_CHILD") == "1", "nested clean gate")
    def test_make_check_passes_without_preexisting_runtime_and_removes_unique_runtime(self):
        if _running_under_pytest():
            self.skipTest("authoritative nested clean-copy gate executes under unittest make check")
        with tempfile.TemporaryDirectory() as name:
            clone = Path(name) / "clone"
            clone.mkdir()
            listed = subprocess.run(
                ["/usr/bin/git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                cwd=ROOT,
                capture_output=True,
                check=True,
            ).stdout.split(b"\0")
            for raw in filter(None, listed):
                relative = Path(os.fsdecode(raw))
                source = ROOT / relative
                if source.is_file():
                    target = clone / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            subprocess.run(["/usr/bin/git", "init", "-q"], cwd=clone, check=True)
            subprocess.run(["/usr/bin/git", "add", "."], cwd=clone, check=True)
            subprocess.run(
                ["/usr/bin/git", "-c", "user.name=AO Lore Test", "-c", "user.email=ao-lore-test@tests.invalid", "commit", "-q", "-m", "test snapshot"],
                cwd=clone,
                check=True,
            )
            environment = {
                **os.environ,
                "AO_LORE_CLEAN_GATE_CHILD": "1",
                "AO_LORE_HOME": str(clone / ".ao-lore"),
            }
            result = subprocess.run(["make", "check"], cwd=clone, env=environment, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            runtime_parent = clone / ".ao-lore"
            self.assertEqual([], list(runtime_parent.glob("clean-tests-*")) if runtime_parent.exists() else [])


if __name__ == "__main__":
    unittest.main()
