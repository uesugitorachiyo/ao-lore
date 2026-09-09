import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

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


class EvidenceSelectionAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory()
        self.candidates = tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates")
        self.root = Path(self.runtime.name)
        self.candidate_root = Path(self.candidates.name)
        self.snapshot = fixture_snapshot(self.root)
        from ao_lore.evidence_selection import EvidenceSelectionDependencies, authorize_evidence_selection, prepare_evidence_selection

        self.dependencies = EvidenceSelectionDependencies(
            runtime_root=self.root,
            candidate_root=self.candidate_root,
            source_head=lambda: ACTUAL_HEAD,
            now=lambda: "2026-08-14T12:00:00Z",
        )
        self.proposal = prepare_evidence_selection(
            self.snapshot,
            "workspace-a",
            (
                document_block_from_snapshot(self.snapshot)["evidence_id"],
                graph_claim_from_snapshot(self.snapshot)["evidence_id"],
            ),
            now="2026-08-14T12:00:00Z",
        )
        self.authorization = authorize_evidence_selection(
            self.dependencies,
            self.proposal,
            "fixture-operator",
        )

    def tearDown(self):
        self.runtime.cleanup()
        self.candidates.cleanup()

    def test_symlink_hardlink_fifo_and_corrupt_audit_preserve_investigation(self):
        from ao_lore.evidence_selection import (
            EvidenceSelectionError,
            inspect_evidence_selection,
            recover_evidence_selections,
        )

        selection_root = self.root / "workspaces" / "state" / "workspace-a" / "selection"
        (selection_root / "audits").mkdir(parents=True, exist_ok=True)
        audit = selection_root / "audits" / "foreign.json"
        audit.write_text("{}\n", encoding="utf-8")
        report = recover_evidence_selections(self.dependencies, "workspace-a")
        self.assertEqual("investigate", report["status"])
        self.assertTrue(audit.exists())

        auth_path = selection_root / "authorizations" / f"{self.authorization['authorization_id']}.json"
        saved = auth_path.with_suffix(".saved")
        auth_path.rename(saved)
        auth_path.symlink_to(saved.name)
        with self.assertRaises(EvidenceSelectionError):
            inspect_evidence_selection(self.dependencies, self.proposal["proposal_id"])
        auth_path.unlink()
        saved.rename(auth_path)

        reserve_root = selection_root / "reservations"
        reserve_root.mkdir(parents=True, exist_ok=True)
        fifo = reserve_root / "pipe"
        os.mkfifo(fifo)
        try:
            report = recover_evidence_selections(self.dependencies, "workspace-a")
            self.assertEqual("investigate", report["status"])
        finally:
            fifo.unlink()


if __name__ == "__main__":
    unittest.main()
