import json
import os
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rehearse-evidence-selection.py"


class EvidenceSelectionRehearsalTests(unittest.TestCase):
    @staticmethod
    def environment() -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("AO_LORE_HOME", None)
        return environment

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            env=self.environment(),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_rehearsal_runs_directly_and_check_is_byte_identical(self):
        first = self.run_script()
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertNotIn("ModuleNotFoundError", first.stderr)
        second = self.run_script("--check")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        report = json.loads(first.stdout)
        self.assertEqual("ao.lore.evidence-selection-rehearsal.v0.1", report["schema_version"])
        self.assertEqual(1, report["candidate_count_delta"])
        self.assertTrue(report["review_inventory_unchanged"])
        self.assertTrue(report["brain_inventory_unchanged"])
        self.assertTrue(report["byte_identical_check"])
        self.assertEqual(
            report["candidate_inventory_before"] + report["candidate_count_delta"],
            report["candidate_inventory_after"],
        )
        self.assertFalse(report["network_used"])
        self.assertFalse(report["provider_used"])

    def test_check_detects_drift_without_rewriting_existing_report(self):
        first = self.run_script()
        self.assertEqual(0, first.returncode, first.stderr)
        report_path = ROOT / ".ao-lore" / "evidence-selection-rehearsal" / "rehearsal-evidence.json"
        original = report_path.read_bytes()
        drifted = json.loads(original)
        drifted["candidate_count_delta"] = 999
        report_path.write_text(json.dumps(drifted, sort_keys=True) + "\n", encoding="utf-8")

        checked = self.run_script("--check")

        self.assertNotEqual(0, checked.returncode)
        self.assertEqual(json.dumps(drifted, sort_keys=True) + "\n", report_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
