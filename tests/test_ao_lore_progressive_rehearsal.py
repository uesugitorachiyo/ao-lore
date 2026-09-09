import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rehearse-progressive-evidence.py"
CAMPAIGN = ROOT / ".ao-lore" / "progressive-evidence-rehearsal"


def inventory(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


class ProgressiveEvidenceRehearsalTests(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(CAMPAIGN, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(CAMPAIGN, ignore_errors=True)

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("AO_LORE_HOME", None)
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments], cwd=ROOT,
            env=environment, capture_output=True, text=True, timeout=45,
        )

    def test_rehearsal_compares_plain_and_enriched_results_byte_exactly(self):
        direct = self.run_script()
        self.assertEqual(0, direct.returncode, direct.stderr)
        before = inventory(CAMPAIGN)
        checked = self.run_script("--check")
        self.assertEqual(0, checked.returncode, checked.stderr)
        self.assertEqual(direct.stdout, checked.stdout)
        self.assertEqual(before, inventory(CAMPAIGN))

        report = json.loads(direct.stdout)
        self.assertEqual("ao.lore.progressive-evidence-rehearsal.v0.1", report["schema_version"])
        self.assertEqual(
            ["combined", "document-only", "graph-only"], report["workspace_modes"],
        )
        self.assertTrue(report["network_denied"])
        self.assertTrue(report["unrelated_workspace_preserved"])
        self.assertTrue(report["enrichment_success"])
        self.assertNotIn("graph_size", report)
        expected_metrics = {
            "citation_precision", "applicability", "conflict_detection",
            "supersession_handling", "qualification", "refusal_correctness",
            "evidence_identities", "latency_budget",
        }
        self.assertEqual(
            {"combined", "document-only", "graph-only"}, set(report["metrics"]),
        )
        self.assertTrue(all(
            set(values) == expected_metrics for values in report["metrics"].values()
        ))
        self.assertGreater(
            report["metrics"]["combined"]["applicability"],
            report["metrics"]["document-only"]["applicability"],
        )
        self.assertGreaterEqual(
            report["metrics"]["combined"]["citation_precision"],
            report["metrics"]["document-only"]["citation_precision"],
        )
        self.assertGreaterEqual(
            report["metrics"]["combined"]["refusal_correctness"],
            report["metrics"]["document-only"]["refusal_correctness"],
        )
        self.assertRegex(report["rehearsal_digest"], r"^sha256:[0-9a-f]{64}$")

    def test_every_rehearsal_query_runs_inside_network_denial(self):
        spec = importlib.util.spec_from_file_location("progressive_rehearsal", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        actual_query = module.query_workspace
        query_count = 0

        def guarded_query(*args, **kwargs):
            nonlocal query_count
            query_count += 1
            with self.assertRaises(AssertionError):
                module.socket.socket()
            return actual_query(*args, **kwargs)

        with patch.object(module, "query_workspace", side_effect=guarded_query):
            report = module._report()
        self.assertEqual(9, query_count)
        self.assertTrue(report["network_denied"])
        self.assertTrue(report["unrelated_workspace_preserved"])


if __name__ == "__main__":
    unittest.main()
