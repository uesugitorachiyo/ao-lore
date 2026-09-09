import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rehearse-canonical-knowledge-reader.py"
GENERATOR = ROOT / "tests" / "fixtures" / "ao_lore" / "knowledge" / "generate.py"
DIGEST = "sha256:" + "a" * 64
SCENARIOS = {
    "answerable_v0_2_promotion",
    "status_search_answer",
    "partial_refuse_investigate",
    "rollback_exclusion",
    "recover_interrupted_apply",
    "mixed_legacy_new",
    "empty_snapshot",
    "corruption_rejected",
    "reader_vs_apply",
    "reader_vs_rollback",
    "reader_vs_recover",
    "two_readers",
    "foreign_writer_preserved",
    "exact_rerun",
}


class KnowledgeIntegrationRehearsalTests(unittest.TestCase):
    def environment(self):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        return environment

    def run_script(self, *arguments):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, arguments)],
            cwd=ROOT,
            env=self.environment(),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_generator_is_deterministic_public_safe_and_checkable(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            output = Path(temporary) / "fixture"
            generated = subprocess.run(
                [sys.executable, str(GENERATOR), "--out", str(output)],
                cwd=ROOT,
                env=self.environment(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, generated.returncode, generated.stderr)
            checked = subprocess.run(
                [sys.executable, str(GENERATOR), "--check", "--out", str(output)],
                cwd=ROOT,
                env=self.environment(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, checked.returncode, checked.stderr)
            manifest = json.loads((output / "fixture-manifest.json").read_text())
            self.assertEqual("ao.lore.knowledge-rehearsal-fixture.v0.1", manifest["schema_version"])
            self.assertEqual(["candidate-answerable", "candidate-legacy", "candidate-stale"], manifest["candidate_ids"])
            self.assertFalse(manifest["canonical_repository_brain"])
            self.assertFalse(manifest["live_authorization"])
            rendered = json.dumps(manifest, sort_keys=True)
            self.assertNotIn(str(ROOT), rendered)
            self.assertNotIn("token", rendered.casefold())

    def test_script_runs_without_pythonpath_and_check_rehearses_exact_rerun(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            root = Path(temporary) / "campaign"
            result = self.run_script(
                "--root", root,
                "--canonical-brain-before-digest", DIGEST,
                "--canonical-brain-after-digest", DIGEST,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
            report = json.loads(result.stdout)
            stored = json.loads((root / "rehearsal-evidence.json").read_text())
            self.assertEqual(stored, report)
            self.assertEqual("ao.lore.knowledge-rehearsal.v0.1", report["schema_version"])
            self.assertEqual(SCENARIOS, set(report["scenario_statuses"]))
            self.assertTrue(all(value == "passed" for value in report["scenario_statuses"].values()))
            self.assertEqual(len(SCENARIOS), report["scenario_count"])
            self.assertTrue(report["canonical_brain_unchanged"])
            self.assertFalse(report["default_candidates_accessed"])
            self.assertFalse(report["default_brain_accessed"])
            self.assertFalse(report["live_authorization_used"])
            self.assertFalse(report["live_promotion_executed"])
            self.assertFalse(report["real_canonical_query_executed"])
            self.assertFalse(report["network_used"])
            self.assertFalse(report["credentials_used"])
            public = json.dumps(report, sort_keys=True)
            for forbidden in (str(root), str(ROOT), "Reviewed evidence", "Canonical rehearsal policy"):
                self.assertNotIn(forbidden, public)

            before = hashlib.sha256((root / "rehearsal-evidence.json").read_bytes()).hexdigest()
            checked = self.run_script(
                "--check", "--root", root,
                "--canonical-brain-before-digest", DIGEST,
                "--canonical-brain-after-digest", DIGEST,
            )
            self.assertEqual(0, checked.returncode, checked.stderr)
            self.assertEqual(report, json.loads(checked.stdout))
            after = hashlib.sha256((root / "rehearsal-evidence.json").read_bytes()).hexdigest()
            self.assertEqual(before, after)

    def test_script_refuses_canonical_default_and_unowned_roots(self):
        forbidden = (
            ROOT,
            ROOT / "brain",
            ROOT / "working" / "candidates",
            ROOT / ".ao-lore",
            Path(tempfile.gettempdir()) / "ao-lore-unowned-rehearsal",
        )
        for root in forbidden:
            with self.subTest(root=root):
                result = self.run_script(
                    "--root", root,
                    "--canonical-brain-before-digest", DIGEST,
                    "--canonical-brain-after-digest", DIGEST,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual("knowledge rehearsal rejected\n", result.stderr)

        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('REPOSITORY / "brain"', source)
        self.assertNotIn('REPOSITORY / "working"', source)

    def test_script_rejects_external_inventory_drift_before_fixture_work(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            root = Path(temporary) / "must-not-exist"
            result = self.run_script(
                "--root", root,
                "--canonical-brain-before-digest", DIGEST,
                "--canonical-brain-after-digest", "sha256:" + "b" * 64,
            )
            self.assertEqual(2, result.returncode)
            self.assertEqual("knowledge rehearsal rejected\n", result.stderr)
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
