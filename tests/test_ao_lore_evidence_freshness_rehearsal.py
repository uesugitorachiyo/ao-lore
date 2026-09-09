import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rehearse-evidence-freshness.py"
GENERATOR = ROOT / "tests" / "fixtures" / "ao_lore" / "evidence_freshness" / "generate.py"
DIGEST = "sha256:" + "a" * 64
SCENARIOS = {
    "unchanged",
    "updated",
    "superseded",
    "unavailable",
    "investigate",
    "crash_retry",
    "query_downgrade",
    "redaction",
    "protected_inventory",
}


class EvidenceFreshnessRehearsalTests(unittest.TestCase):
    def test_redaction_probe_uses_the_generic_workspace_query_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")

        self.assertIn('patch("ao_lore.__main__._run_workspace"', source)
        self.assertIn(
            '["workspace", "query", "--workspace", "rehearsal", '
            '"--prompt", "private", "--json"]',
            source,
        )
        self.assertNotIn("_run_" + "evidence_graph", source)

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
            timeout=60,
        )

    def run_generator(self, *arguments):
        return subprocess.run(
            [sys.executable, str(GENERATOR), *map(str, arguments)],
            cwd=ROOT,
            env=self.environment(),
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_private_interpreter_is_used_but_public_ledger_redacts_it(self):
        import importlib.util

        specification = importlib.util.spec_from_file_location("freshness_rehearsal", SCRIPT)
        module = importlib.util.module_from_spec(specification)
        assert specification.loader is not None
        specification.loader.exec_module(module)
        actual_argv = []

        def run(arguments, **kwargs):
            actual_argv.append(arguments)
            return CompletedProcess(arguments, 0, "", "")

        private_python = "/private/builds/user/.venv/bin/python"
        with patch.object(module.sys, "executable", private_python), patch.object(module.subprocess, "run", side_effect=run):
            _, entry = module._run_command(
                [private_python, "generator.py", "--out", "FIXTURE_ROOT"],
                public_argv=["PYTHON", "generator.py", "--out", "FIXTURE_ROOT"],
            )

        self.assertEqual([private_python, "generator.py", "--out", "FIXTURE_ROOT"], actual_argv[0])
        self.assertEqual(["PYTHON", "generator.py", "--out", "FIXTURE_ROOT"], entry["argv"])
        self.assertNotIn(private_python, json.dumps(entry, sort_keys=True))

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
            self.assertEqual("ao.lore.evidence-freshness-rehearsal-fixture.v0.1", manifest["schema_version"])
            self.assertEqual(
                ["crash_retry", "investigate", "query_downgrade", "redaction", "superseded", "unchanged", "unavailable", "updated"],
                manifest["scenario_ids"],
            )
            self.assertEqual(2, manifest["fixture_file_count"])
            self.assertEqual(["fixture-manifest.json", "fixture-spec.json"], manifest["fixture_files"])
            self.assertFalse(manifest["canonical_repository_sources"])
            self.assertFalse(manifest["network_used"])
            rendered = json.dumps(manifest, sort_keys=True)
            self.assertNotIn(str(ROOT), rendered)
            self.assertNotIn("token", rendered.casefold())

    def test_script_runs_without_pythonpath_and_check_is_exact_rerun(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            root = Path(temporary) / "campaign"
            result = self.run_script("--root", root)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
            report = json.loads(result.stdout)
            stored = json.loads((root / "rehearsal-evidence.json").read_text())
            self.assertEqual(stored, report)
            self.assertEqual("ao.lore.evidence-freshness-rehearsal.v0.1", report["schema_version"])
            self.assertEqual(SCENARIOS, set(report["scenario_statuses"]))
            self.assertTrue(all(value == "passed" for value in report["scenario_statuses"].values()))
            self.assertEqual(len(SCENARIOS), report["scenario_count"])
            self.assertRegex(report["source_head"], r"^[0-9a-f]{40}$")
            self.assertTrue(report["canonical_brain_unchanged"])
            self.assertTrue(report["candidate_inventory_unchanged"])
            self.assertTrue(report["source_inventory_unchanged"])
            self.assertFalse(report["default_brain_mutated"])
            self.assertFalse(report["default_candidates_mutated"])
            self.assertFalse(report["default_sources_mutated"])
            self.assertFalse(report["live_refresh_executed"])
            self.assertFalse(report["network_used"])
            self.assertFalse(report["credentials_used"])
            expected_source_inventories = {
                f"source_{path.name}"
                for path in (ROOT / "sources").iterdir()
                if path.is_dir()
            }
            self.assertEqual(
                {"brain", "candidates", "reviews", *expected_source_inventories},
                set(report["protected_inventory_before"]),
            )
            self.assertEqual(report["protected_inventory_before"], report["protected_inventory_after"])
            self.assertTrue(
                all(
                    isinstance(digest, str)
                    and digest.startswith("sha256:")
                    and digest != DIGEST
                    for digest in report["protected_inventory_before"].values()
                )
            )
            self.assertGreaterEqual(len(report["command_ledger"]), 3)
            self.assertTrue(all(item["exit_code"] == 0 for item in report["command_ledger"]))
            self.assertTrue(all("cwd" not in item and "root" not in item for item in report["command_ledger"]))
            self.assertEqual(
                [
                    ["git", "rev-parse", "HEAD"],
                    ["PYTHON", "tests/fixtures/ao_lore/evidence_freshness/generate.py", "--out", "FIXTURE_ROOT"],
                    ["PYTHON", "tests/fixtures/ao_lore/evidence_freshness/generate.py", "--check", "--out", "FIXTURE_ROOT"],
                ],
                [item["argv"] for item in report["command_ledger"]],
            )
            public = json.dumps(report, sort_keys=True)
            for forbidden in (str(root), str(ROOT), ".ao-lore/evidence-freshness-rehearsal", "baseline", "changed-body", "private"):
                self.assertNotIn(forbidden, public)

            before = hashlib.sha256((root / "rehearsal-evidence.json").read_bytes()).hexdigest()
            checked = self.run_script(
                "--check",
                "--root", root,
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
            ROOT / "sources",
            ROOT / ".ao-lore",
            Path(tempfile.gettempdir()) / "ao-lore-unowned-freshness-rehearsal",
        )
        for root in forbidden:
            with self.subTest(root=root):
                result = self.run_script(
                    "--root", root,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual("evidence freshness rehearsal rejected\n", result.stderr)

    def test_generator_refuses_forbidden_and_unowned_roots_without_writing(self):
        forbidden = (
            ROOT,
            ROOT / "brain",
            ROOT / "working" / "candidates",
            ROOT / "sources",
            ROOT / ".ao-lore",
            Path(tempfile.gettempdir()) / "ao-lore-unowned-freshness-fixture",
        )
        for root in forbidden:
            with self.subTest(root=root):
                before = root.exists()
                result = self.run_generator("--out", root)
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual("evidence freshness fixture rejected\n", result.stderr)
                self.assertEqual(before, root.exists())

    def test_script_rejects_expected_inventory_assertion_drift_before_fixture_work(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            root = Path(temporary) / "must-not-exist"
            result = self.run_script(
                "--root", root,
                "--expected-brain-digest", DIGEST,
            )
            self.assertEqual(2, result.returncode)
            self.assertEqual("evidence freshness rehearsal rejected\n", result.stderr)
            self.assertFalse(root.exists())

    def test_script_detects_actual_inventory_drift_via_failpoint_and_preserves_rehearsal_root(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            root = Path(temporary) / "campaign"
            environment = self.environment()
            environment["AO_LORE_EVIDENCE_FRESHNESS_REHEARSAL_FAILPOINT"] = "after_before_inventory"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--root", str(root)],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual("evidence freshness rehearsal rejected\n", result.stderr)
            self.assertTrue(root.exists())
            self.assertFalse((root / "rehearsal-evidence.json").exists())


if __name__ == "__main__":
    unittest.main()
