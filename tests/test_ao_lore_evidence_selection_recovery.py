import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore.candidates import inspect_candidate
from ao_lore.evidence_graph import GraphDependencies, publish_evidence_graph
from ao_lore.workspace_documents import WorkspaceDocumentDependencies, publish_workspace_documents
from ao_lore.workspace_query import (
    WorkspaceDocumentQuerySnapshot,
    WorkspaceGraphSnapshot,
    WorkspaceQuerySnapshot,
)
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    WorkspaceRegistrySnapshot,
    load_workspace_registry,
    publish_workspace_registry_generation,
    select_workspace,
)
from tests.test_ao_lore_document_evidence_query import generation as document_generation
from tests.test_ao_lore_evidence_candidate import (
    definition,
    document_block_from_snapshot,
    graph_claim_from_snapshot,
)
from tests.test_ao_lore_workspace_query import graph_variant


ROOT = Path(__file__).resolve().parents[1]
ACTUAL_HEAD = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()


def fixture_snapshot(root: Path) -> WorkspaceQuerySnapshot:
    graph = graph_variant("graph-workspace-a")
    publish_workspace_registry_generation(
        (definition("workspace-a", None, graph),),
        WorkspaceRegistryDependencies(root),
    )
    ir = document_generation()["documents"][0]["document_ir"]
    publish_workspace_documents(
        WorkspaceDocumentDependencies(root),
        "workspace-a",
        (ir,),
    )
    graph_root = root / "workspaces" / "state" / "workspace-a" / "graph"
    graph_root.mkdir(parents=True, exist_ok=True)
    publish_evidence_graph(graph, GraphDependencies(graph_root))
    registry = load_workspace_registry(WorkspaceRegistryDependencies(root))
    selection = select_workspace(registry, "workspace-a")
    from ao_lore.workspace_documents import load_workspace_documents

    current_generation = load_workspace_documents(
        WorkspaceDocumentDependencies(root), "workspace-a"
    ).generation
    return WorkspaceQuerySnapshot(
        WorkspaceRegistrySnapshot(registry.generations, registry.workspaces),
        selection,
        (WorkspaceGraphSnapshot("workspace-a", graph),),
        documents=(WorkspaceDocumentQuerySnapshot("workspace-a", current_generation),),
    )


class SimulatedCrash(RuntimeError):
    pass


class EvidenceSelectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory()
        self.candidates = tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates")
        self.root = Path(self.runtime.name)
        self.candidate_root = Path(self.candidates.name)
        self.snapshot = fixture_snapshot(self.root)

    def tearDown(self):
        self.runtime.cleanup()
        self.candidates.cleanup()

    def deps(self, *, failpoint=lambda _name: None, now="2026-08-14T12:00:00Z"):
        from ao_lore.evidence_selection import EvidenceSelectionDependencies

        return EvidenceSelectionDependencies(
            runtime_root=self.root,
            candidate_root=self.candidate_root,
            source_head=lambda: ACTUAL_HEAD,
            now=lambda: now,
            failpoint=failpoint,
        )

    def prepare_authorized(self, *, failpoint=lambda _name: None):
        from ao_lore.evidence_selection import authorize_evidence_selection, prepare_evidence_selection

        proposal = prepare_evidence_selection(
            self.snapshot,
            "workspace-a",
            (
                document_block_from_snapshot(self.snapshot)["evidence_id"],
                graph_claim_from_snapshot(self.snapshot)["evidence_id"],
            ),
            now="2026-08-14T12:00:00Z",
        )
        authorization = authorize_evidence_selection(
            self.deps(failpoint=failpoint),
            proposal,
            "fixture-operator",
        )
        return proposal, authorization

    def test_every_apply_boundary_recovers_to_one_exact_candidate(self):
        from ao_lore.evidence_selection import (
            SELECTION_APPLY_FAILPOINTS,
            apply_evidence_selection,
            recover_evidence_selections,
        )

        for failpoint_name in SELECTION_APPLY_FAILPOINTS:
            with self.subTest(failpoint=failpoint_name):
                root = Path(tempfile.mkdtemp(dir=ROOT / ".ao-lore"))
                candidate_root = Path(tempfile.mkdtemp(dir=ROOT / "working" / "candidates"))
                try:
                    snapshot = fixture_snapshot(root)

                    def stop(name):
                        if name == failpoint_name:
                            raise SimulatedCrash(name)

                    from ao_lore.evidence_selection import EvidenceSelectionDependencies, authorize_evidence_selection, prepare_evidence_selection

                    deps = EvidenceSelectionDependencies(
                        runtime_root=root,
                        candidate_root=candidate_root,
                        source_head=lambda: ACTUAL_HEAD,
                        now=lambda: "2026-08-14T12:00:00Z",
                        failpoint=stop,
                    )
                    proposal = prepare_evidence_selection(
                        snapshot,
                        "workspace-a",
                        (
                            document_block_from_snapshot(snapshot)["evidence_id"],
                            graph_claim_from_snapshot(snapshot)["evidence_id"],
                        ),
                        now="2026-08-14T12:00:00Z",
                    )
                    authorization = authorize_evidence_selection(
                        deps, proposal, "fixture-operator"
                    )
                    with self.assertRaises(SimulatedCrash):
                        apply_evidence_selection(
                            deps, proposal["proposal_id"], authorization["authorization_id"]
                        )
                    resumed = EvidenceSelectionDependencies(
                        runtime_root=root,
                        candidate_root=candidate_root,
                        source_head=lambda: ACTUAL_HEAD,
                        now=lambda: "2026-08-14T12:00:00Z",
                    )
                    result = recover_evidence_selections(resumed, "workspace-a")
                    self.assertIn(result["status"], {"recovered", "no_op"})
                    inspection = inspect_candidate(
                        proposal["candidate"]["candidate_id"],
                        candidate_root=candidate_root,
                    )
                    self.assertEqual("unreviewed", inspection["review_status"])
                finally:
                    # Preserve fixture-owned runtime bytes only for this test.
                    import shutil
                    shutil.rmtree(root, ignore_errors=True)
                    shutil.rmtree(candidate_root, ignore_errors=True)

    def test_foreign_or_ambiguous_recovery_state_is_preserved(self):
        from ao_lore.evidence_selection import (
            recover_evidence_selections,
            EvidenceSelectionDependencies,
        )

        selection_root = (
            self.root / "workspaces" / "state" / "workspace-a" / "selection" / "reservations"
        )
        selection_root.mkdir(parents=True, exist_ok=True)
        (selection_root / "foreign.txt").write_text("keep", encoding="utf-8")
        deps = EvidenceSelectionDependencies(
            runtime_root=self.root,
            candidate_root=self.candidate_root,
            source_head=lambda: ACTUAL_HEAD,
            now=lambda: "2026-08-14T12:00:00Z",
        )
        result = recover_evidence_selections(deps, "workspace-a")
        self.assertEqual("investigate", result["status"])
        self.assertTrue((selection_root / "foreign.txt").exists())

    def test_authorized_only_recover_executes_apply_and_creates_one_unreviewed_candidate(self):
        from ao_lore.evidence_selection import recover_evidence_selections

        proposal, _authorization = self.prepare_authorized()
        result = recover_evidence_selections(self.deps(), "workspace-a")

        self.assertEqual("recovered", result["status"])
        self.assertEqual("recovered_exact_state", result["reason_code"])
        self.assertEqual([proposal["proposal_id"]], result["proposal_ids"])
        self.assertEqual(
            [proposal["candidate"]["candidate_id"]], result["candidate_ids"]
        )
        candidate = inspect_candidate(
            proposal["candidate"]["candidate_id"], candidate_root=self.candidate_root
        )
        self.assertEqual("unreviewed", candidate["review_status"])
        self.assertFalse(candidate["canonical"])
        self.assertFalse(candidate["promotion_authority"])
        self.assertEqual(0, candidate["verified_review_events"])

    def test_authorized_only_recover_investigates_when_authorization_inventory_changes_between_checks(self):
        import ao_lore.evidence_selection as module

        proposal, authorization = self.prepare_authorized()
        duplicate = dict(authorization)
        duplicate["authorization_id"] = "authorization-second-fixture"
        duplicate["nonce"] = "nonce-second-fixture"
        duplicate["authorization_digest"] = ""
        duplicate["authorization_digest"] = __import__(
            "ao_lore.benchmark", fromlist=["canonical_digest"]
        ).canonical_digest(
            {
                key: value
                for key, value in duplicate.items()
                if key != "authorization_digest"
            }
        )

        with patch.object(
            module,
            "_load_matching_authorizations",
            side_effect=([authorization], []),
        ):
            result = module.recover_evidence_selections(self.deps(), "workspace-a")
        self.assertEqual("investigate", result["status"])
        self.assertEqual("ambiguous_selection_state", result["reason_code"])
        self.assertEqual([proposal["proposal_id"]], result["proposal_ids"])
        self.assertEqual([], result["candidate_ids"])

        with patch.object(
            module,
            "_load_matching_authorizations",
            side_effect=([authorization], [authorization, duplicate]),
        ):
            result = module.recover_evidence_selections(self.deps(), "workspace-a")
        self.assertEqual("investigate", result["status"])
        self.assertEqual("ambiguous_selection_state", result["reason_code"])
        self.assertEqual([proposal["proposal_id"]], result["proposal_ids"])
        self.assertEqual([], result["candidate_ids"])

    def test_inspection_reopens_exact_bound_state_without_changing_review(self):
        from ao_lore.evidence_selection import (
            apply_evidence_selection,
            inspect_evidence_selection,
        )

        proposal, authorization = self.prepare_authorized()
        apply_evidence_selection(
            self.deps(),
            proposal["proposal_id"],
            authorization["authorization_id"],
        )
        inspection = inspect_evidence_selection(self.deps(), proposal["proposal_id"])
        self.assertEqual("committed", inspection["status"])
        candidate = inspect_candidate(
            proposal["candidate"]["candidate_id"], candidate_root=self.candidate_root
        )
        self.assertEqual(0, candidate["verified_review_events"])

    def test_inspection_is_read_only_and_multiple_matching_authorizations_investigate(self):
        from ao_lore.evidence_selection import (
            authorize_evidence_selection,
            inspect_evidence_selection,
        )

        proposal, authorization = self.prepare_authorized()
        selection_root = self.root / "workspaces" / "state" / "workspace-a" / "selection"
        before = sorted(
            path.relative_to(selection_root).as_posix()
            for path in selection_root.rglob("*")
        )
        inspection = inspect_evidence_selection(self.deps(), proposal["proposal_id"])
        self.assertEqual("authorized", inspection["status"])
        after = sorted(
            path.relative_to(selection_root).as_posix()
            for path in selection_root.rglob("*")
        )
        self.assertEqual(before, after)

        duplicate = dict(authorization)
        duplicate["authorization_id"] = "authorization-second-fixture"
        duplicate["nonce"] = "nonce-second-fixture"
        duplicate["authorization_digest"] = ""
        duplicate["authorization_digest"] = __import__("ao_lore.benchmark", fromlist=["canonical_digest"]).canonical_digest(
            {key: value for key, value in duplicate.items() if key != "authorization_digest"}
        )
        auth_root = selection_root / "authorizations"
        (auth_root / f"{duplicate['authorization_id']}.json").write_text(
            json.dumps(duplicate, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        inspection = inspect_evidence_selection(self.deps(), proposal["proposal_id"])
        self.assertEqual("investigate", inspection["status"])

    def test_corrupt_or_missing_terminal_artifacts_investigate(self):
        from ao_lore.evidence_selection import (
            apply_evidence_selection,
            inspect_evidence_selection,
            recover_evidence_selections,
        )

        proposal, authorization = self.prepare_authorized()
        apply_evidence_selection(
            self.deps(),
            proposal["proposal_id"],
            authorization["authorization_id"],
        )
        selection_root = self.root / "workspaces" / "state" / "workspace-a" / "selection"
        inspection = inspect_evidence_selection(self.deps(), proposal["proposal_id"])
        transaction_id = inspection["transaction_id"]
        for relative in (
            ("consumed", f"{authorization['authorization_id']}.json"),
            ("audits", f"{transaction_id}.json"),
            ("completions", f"{transaction_id}.json"),
            ("audits", "foreign.json"),
            ("completions", "foreign.json"),
        ):
            with self.subTest(relative=relative):
                directory, name = relative
                target = selection_root / directory / name
                original = None
                if target.exists():
                    original = target.read_bytes()
                    if directory in {"audits", "completions"}:
                        target.write_text("{}\n", encoding="utf-8")
                else:
                    target.write_text("{}\n", encoding="utf-8")
                if target.exists() and directory == "consumed":
                    target.unlink()
                result = recover_evidence_selections(self.deps(), "workspace-a")
                self.assertEqual("investigate", result["status"])
                inspect = inspect_evidence_selection(self.deps(), proposal["proposal_id"])
                self.assertEqual("investigate", inspect["status"])
                if original is not None:
                    target.write_bytes(original)
                else:
                    target.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
