import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from ao_lore.evidence_selection_contracts import validate_selection_authorization
from ao_lore.workspace_documents import (
    WorkspaceDocumentDependencies,
    load_workspace_documents,
    publish_workspace_documents,
)
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
from ao_lore.evidence_graph import GraphDependencies, publish_evidence_graph
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
    generation = load_workspace_documents(
        WorkspaceDocumentDependencies(root), "workspace-a"
    ).generation
    return WorkspaceQuerySnapshot(
        WorkspaceRegistrySnapshot(registry.generations, registry.workspaces),
        selection,
        (WorkspaceGraphSnapshot("workspace-a", graph),),
        documents=(WorkspaceDocumentQuerySnapshot("workspace-a", generation),),
    )


class EvidenceSelectionAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory()
        self.candidates = tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates")
        self.root = Path(self.runtime.name)
        self.candidate_root = Path(self.candidates.name)
        self.snapshot = fixture_snapshot(self.root)

    def tearDown(self):
        self.runtime.cleanup()
        self.candidates.cleanup()

    def deps(self, *, source_head=ACTUAL_HEAD, now="2026-08-14T12:00:00Z"):
        from ao_lore.evidence_selection import EvidenceSelectionDependencies

        return EvidenceSelectionDependencies(
            runtime_root=self.root,
            candidate_root=self.candidate_root,
            source_head=lambda: source_head,
            now=lambda: now,
        )

    def test_authorization_persists_exact_source_head_and_closed_authority(self):
        from ao_lore.evidence_selection import (
            authorize_evidence_selection,
            prepare_evidence_selection,
        )

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
            self.deps(),
            proposal,
            "fixture-operator",
        )
        self.assertEqual(ACTUAL_HEAD, authorization["source_head"])
        self.assertEqual(authorization, validate_selection_authorization(authorization))

    def test_authorization_rejects_fake_source_head_and_expired_proposal(self):
        from ao_lore.evidence_selection import (
            authorize_evidence_selection,
            prepare_evidence_selection,
            EvidenceSelectionError,
        )

        proposal = prepare_evidence_selection(
            self.snapshot,
            "workspace-a",
            (
                document_block_from_snapshot(self.snapshot)["evidence_id"],
                graph_claim_from_snapshot(self.snapshot)["evidence_id"],
            ),
            now="2026-08-14T12:00:00Z",
        )
        with self.assertRaises(EvidenceSelectionError):
            authorize_evidence_selection(
                self.deps(source_head="0" * 40),
                proposal,
                "fixture-operator",
            )

        with self.assertRaises(EvidenceSelectionError):
            authorize_evidence_selection(
                self.deps(now="2026-08-14T12:06:00Z"),
                proposal,
                "fixture-operator",
            )

    def test_authorization_rejects_parent_symlink_escape(self):
        from ao_lore.evidence_selection import (
            authorize_evidence_selection,
            prepare_evidence_selection,
            EvidenceSelectionError,
        )

        proposal = prepare_evidence_selection(
            self.snapshot,
            "workspace-a",
            (
                document_block_from_snapshot(self.snapshot)["evidence_id"],
                graph_claim_from_snapshot(self.snapshot)["evidence_id"],
            ),
            now="2026-08-14T12:00:00Z",
        )
        workspace = self.root / "workspaces" / "state" / "workspace-a"
        selection = workspace / "selection"
        target = self.root / "external-selection"
        target.mkdir()
        selection.symlink_to(target, target_is_directory=True)
        with self.assertRaises(EvidenceSelectionError):
            authorize_evidence_selection(
                self.deps(),
                proposal,
                "fixture-operator",
            )


if __name__ == "__main__":
    unittest.main()
