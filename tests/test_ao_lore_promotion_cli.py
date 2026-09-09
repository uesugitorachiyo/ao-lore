import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ao_lore.__main__ import _parser, main
from ao_lore.promotion import _PromotionDependencies
from tests.test_ao_lore_promotion_authorization import authorization
from tests.test_ao_lore_promotion_prepare import FIXED_NOW, SOURCE_HEAD, build_fixture


ROOT = Path(__file__).resolve().parents[1]


class PromotionCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        root = Path(self.temp.name); self.candidate_id, candidates, brain, runtime = build_fixture(root)
        self.deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)

    def tearDown(self): self.temp.cleanup()

    def invoke(self, argv):
        out, err = StringIO(), StringIO()
        with patch("ao_lore.__main__._promotion_dependencies", return_value=self.deps), redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_parser_exposes_only_locked_surface(self):
        parser = _parser()
        for argv in (
            ["promotion", "prepare", "--candidate-id", "candidate-x", "--out", "x"],
            ["promotion", "apply", "--proposal", "p", "--authorization", "a", "--json"],
            ["promotion", "inspect", "--promotion-id", "promotion-x", "--json"],
            ["promotion", "rollback", "--promotion-id", "promotion-x", "--authorization", "a", "--json"],
            ["promotion", "recover", "--json"],
        ): parser.parse_args(argv)
        for forbidden in ("--brain-root", "--candidate-root", "--runtime-root", "--force", "--network", "--failpoint", "--source-head"):
            with self.assertRaises(SystemExit): parser.parse_args(["promotion", "recover", forbidden, "x"])

    def test_prepare_and_inspect_use_bounded_output(self):
        output = self.deps.proposals_root / "proposal.json"
        code, out, err = self.invoke(["promotion", "prepare", "--candidate-id", self.candidate_id, "--out", str(output)])
        self.assertEqual((0, ""), (code, err)); self.assertIn("STATUS\tprepared", out)
        proposal = json.loads(output.read_text())
        code, out, err = self.invoke(["promotion", "inspect", "--promotion-id", proposal["promotion_id"], "--json"])
        self.assertEqual((0, ""), (code, err)); self.assertEqual("prepared", json.loads(out)["status"])

    def test_errors_are_fixed_and_process_control_propagates(self):
        secret = str(self.deps.brain_root / "PRIVATE-CONTENT")
        code, out, err = self.invoke(["promotion", "inspect", "--promotion-id", secret, "--json"])
        self.assertEqual(2, code); self.assertEqual("", out); self.assertEqual("ao-lore: promotion operation rejected\n", err)
        with patch("ao_lore.__main__._run_promotion", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): main(["promotion", "recover"])
        with patch("ao_lore.__main__._run_promotion", side_effect=SystemExit(7)):
            with self.assertRaises(SystemExit): main(["promotion", "recover"])

    def test_stdout_failure_does_not_change_prepared_state(self):
        output = self.deps.proposals_root / "proposal.json"
        with patch("ao_lore.__main__._promotion_dependencies", return_value=self.deps), patch("sys.stdout.write", side_effect=OSError("private path")), redirect_stderr(StringIO()) as err:
            self.assertEqual(2, main(["promotion", "prepare", "--candidate-id", self.candidate_id, "--out", str(output)]))
        self.assertTrue(output.is_file()); self.assertEqual("ao-lore: promotion operation rejected\n", err.getvalue())

    def test_stdout_failure_after_apply_keeps_committed_state_inspectable(self):
        from ao_lore.promotion import inspect_promotion, prepare_promotion

        proposal_path = self.deps.proposals_root / "proposal.json"
        proposal = prepare_promotion(self.candidate_id, proposal_path, dependencies=self.deps)
        auth_path = self.deps.promotions_root.parent.parent / "authorization.json"
        auth_path.write_text(json.dumps(authorization(proposal), sort_keys=True) + "\n")
        with patch("ao_lore.__main__._promotion_dependencies", return_value=self.deps), patch("sys.stdout.write", side_effect=OSError("private output failure")), redirect_stderr(StringIO()) as err:
            code = main(["promotion", "apply", "--proposal", str(proposal_path), "--authorization", str(auth_path), "--json"])
        self.assertEqual(2, code); self.assertEqual("ao-lore: promotion operation rejected\n", err.getvalue())
        self.assertEqual("committed", inspect_promotion(proposal["promotion_id"], dependencies=self.deps)["status"])

    def test_rehearsal_script_runs_directly_without_pythonpath(self):
        root = Path(self.temp.name) / "direct-check"
        root.mkdir()
        (root / "rehearsal-evidence.json").write_text(json.dumps({
            "schema_version": "ao.lore.promotion-rehearsal.v0.1",
            "real_brain_unchanged": True,
            "scenarios_passed": 5,
        }) + "\n")
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [sys.executable, "scripts/rehearse-promotion-controls.py", "--check", "--root", str(root)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__": unittest.main()
