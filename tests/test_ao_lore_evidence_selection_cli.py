import json
import os
import subprocess
import sys
import tempfile
import shutil
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ao_lore.__main__ import _parser, main
from ao_lore.candidates import inspect_candidate
from ao_lore.evidence_selection import (
    EvidenceSelectionDependencies,
    authorize_evidence_selection,
)
from tests.test_ao_lore_evidence_selection_apply import fixture_snapshot, ACTUAL_HEAD


ROOT = Path(__file__).resolve().parents[1]


class EvidenceSelectionCliTests(unittest.TestCase):
    def invoke(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_actual_main_dispatch_prepare_apply_inspect_recover(self):
        runtime = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        root = Path(runtime.name)
        snapshot = fixture_snapshot(root)
        candidate_id = None
        candidate_lock = ROOT / "working" / "candidates" / ".candidate.lock"
        candidate_lock_preexisted = candidate_lock.exists()
        from tests.test_ao_lore_evidence_candidate import (
            document_block_from_snapshot,
            graph_claim_from_snapshot,
        )
        document_evidence_id = document_block_from_snapshot(snapshot)["evidence_id"]
        graph_evidence_id = graph_claim_from_snapshot(snapshot)["evidence_id"]
        try:
            with patch.dict(os.environ, {"AO_LORE_HOME": str(root)}):
                code, out, err = self.invoke([
                    "workspace", "candidate", "prepare",
                    "--workspace", "workspace-a",
                    "--evidence", document_evidence_id,
                    "--evidence", graph_evidence_id,
                    "--json",
                ])
                self.assertEqual((0, ""), (code, err))
                proposal = json.loads(out)
                candidate_id = proposal["candidate"]["candidate_id"]
                authorization = authorize_evidence_selection(
                    EvidenceSelectionDependencies(
                        runtime_root=root,
                        candidate_root=ROOT / "working" / "candidates",
                        source_head=lambda: ACTUAL_HEAD,
                        now=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    ),
                    proposal,
                    "fixture-operator",
                )
                code, out, err = self.invoke([
                    "workspace", "candidate", "inspect",
                    "--workspace", "workspace-a",
                    "--proposal-id", proposal["proposal_id"],
                    "--json",
                ])
                self.assertEqual((0, ""), (code, err))
                self.assertEqual("authorized", json.loads(out)["status"])
                code, out, err = self.invoke([
                    "workspace", "candidate", "apply",
                    "--workspace", "workspace-a",
                    "--proposal-id", proposal["proposal_id"],
                    "--authorization-id", authorization["authorization_id"],
                    "--json",
                ])
                self.assertEqual((0, ""), (code, err))
                report = json.loads(out)
                self.assertEqual("committed", report["status"])
                candidate = inspect_candidate(report["candidate_id"])
                self.assertEqual("unreviewed", candidate["review_status"])
                self.assertFalse(candidate["canonical"])
                self.assertFalse(candidate["promotion_authority"])
                code, out, err = self.invoke([
                    "workspace", "candidate", "recover",
                    "--workspace", "workspace-a",
                    "--json",
                ])
                self.assertEqual((0, ""), (code, err))
                recovered = json.loads(out)
                self.assertIn(recovered["status"], {"no_op", "recovered"})
                code, out, err = self.invoke([
                    "workspace", "candidate", "inspect",
                    "--workspace", "workspace-a",
                    "--proposal-id", "proposal-missing",
                    "--json",
                ])
                self.assertEqual((2, "", "ao-lore: workspace operation rejected\n"), (code, out, err))
        finally:
            if candidate_id is not None:
                shutil.rmtree(ROOT / "working" / "candidates" / candidate_id, ignore_errors=True)
            if not candidate_lock_preexisted:
                candidate_lock.unlink(missing_ok=True)
            runtime.cleanup()

    def test_candidate_surface_exposes_only_fixed_ids_and_explicit_evidence(self):
        parser = _parser()
        args = parser.parse_args([
            "workspace", "candidate", "prepare", "--workspace", "workspace-a",
            "--evidence", "sha256:" + "1" * 64, "--json",
        ])
        self.assertEqual(args.workspace, "workspace-a")
        self.assertEqual(args.evidence, ["sha256:" + "1" * 64])
        for argv in (
            ["workspace", "candidate", "apply", "--workspace", "workspace-a", "--proposal-id", "proposal-a", "--authorization-id", "authorization-a", "--json"],
            ["workspace", "candidate", "inspect", "--workspace", "workspace-a", "--proposal-id", "proposal-a", "--json"],
            ["workspace", "candidate", "recover", "--workspace", "workspace-a", "--json"],
        ):
            parser.parse_args(argv)

    def test_candidate_surface_rejects_forbidden_flags(self):
        parser = _parser()
        for flag in (
            "--root", "--path", "--url", "--manifest", "--query", "--provider",
            "--model", "--network", "--policy", "--force", "--concurrency",
            "--reviewer", "--decision", "--promotion-id",
        ):
            argv = [
                "workspace", "candidate", "recover",
                "--workspace", "workspace-a", "--json", flag,
            ]
            if flag not in {"--network", "--force"}:
                argv.append("x")
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_candidate_errors_are_fixed_and_process_controls_propagate(self):
        runtime = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        try:
            with patch.dict(os.environ, {"AO_LORE_HOME": runtime.name}):
                code, out, err = self.invoke([
                    "workspace", "candidate", "inspect", "--workspace", "workspace-a", "--proposal-id", "proposal-missing", "--json",
                ])
            self.assertEqual((2, "", "ao-lore: workspace operation rejected\n"), (code, out, err))
        finally:
            runtime.cleanup()
        with patch("ao_lore.__main__._run_workspace", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                main(["workspace", "candidate", "recover", "--workspace", "workspace-a", "--json"])
        with patch("ao_lore.__main__._run_workspace", side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit):
                main(["workspace", "candidate", "recover", "--workspace", "workspace-a", "--json"])


if __name__ == "__main__":
    unittest.main()
