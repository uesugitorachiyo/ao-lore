import importlib
import unittest
from copy import deepcopy
from unittest.mock import patch

from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_selection_contracts import validate_selection_proposal
from tests.test_ao_lore_evidence_candidate import (
    combined_snapshot,
    document_block_from_snapshot,
    graph_claim_from_snapshot,
    graph_edge_from_snapshot,
)
from tests.test_ao_lore_progressive_query import snapshot as progressive_snapshot
from ao_lore.workspace_query import (
    WorkspaceGraphSnapshot,
    WorkspaceQuerySnapshot,
)
from ao_lore.workspace_registry import WorkspaceRegistrySnapshot, select_workspace
from tests.test_ao_lore_workspace_query import graph_variant


class EvidenceSelectionPrepareTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = combined_snapshot()
        self.document_evidence = document_block_from_snapshot(self.snapshot)
        self.graph_evidence = graph_claim_from_snapshot(self.snapshot)
        self.edge_evidence = graph_edge_from_snapshot(self.snapshot)
        self.document_evidence_id = self.document_evidence["evidence_id"]
        self.graph_evidence_id = self.graph_evidence["evidence_id"]
        self.edge_evidence_id = self.edge_evidence["evidence_id"]

    def test_prepare_binds_only_explicit_reachable_evidence(self):
        module = importlib.import_module("ao_lore.evidence_selection")
        proposal = module.prepare_evidence_selection(
            self.snapshot,
            "workspace-a",
            (self.document_evidence_id, self.graph_evidence_id),
            now="2026-08-14T12:00:00Z",
        )
        self.assertEqual("workspace-a", proposal["primary_workspace_id"])
        self.assertEqual(
            [self.document_evidence_id, self.graph_evidence_id],
            [item["evidence_id"] for item in proposal["evidence"]],
        )
        self.assertEqual(proposal, validate_selection_proposal(proposal))

    def test_prepare_rejects_stale_restricted_conflicting_and_unreachable(self):
        module = importlib.import_module("ao_lore.evidence_selection")
        stale_snapshot = combined_snapshot(freshness={"policy-record": "updated"})
        stale_graph_id = graph_claim_from_snapshot(stale_snapshot)["evidence_id"]
        restricted_snapshot = combined_snapshot(restricted=True)
        restricted_graph_id = graph_claim_from_snapshot(restricted_snapshot)["evidence_id"]
        for snapshot, evidence_id in (
            (stale_snapshot, stale_graph_id),
            (restricted_snapshot, restricted_graph_id),
            (self.snapshot, "absent-evidence-id"),
        ):
            with self.subTest(evidence_id=evidence_id), self.assertRaises(
                module.EvidenceSelectionError
            ):
                module.prepare_evidence_selection(
                    snapshot,
                    "workspace-a",
                    (evidence_id,),
                    now="2026-08-14T12:00:00Z",
                )

        conflict_graph = graph_claim_from_snapshot(self.snapshot)
        conflict_graph["conflict_status"] = "conflicted"
        with patch.object(
            module,
            "_resolve_selected_evidence",
            return_value=[conflict_graph],
        ):
            with self.assertRaises(module.EvidenceSelectionError):
                module.prepare_evidence_selection(
                    self.snapshot,
                    "workspace-a",
                    (self.graph_evidence_id,),
                    now="2026-08-14T12:00:00Z",
                )

    def test_prepare_rejects_duplicates_and_edge_without_selected_endpoints(self):
        module = importlib.import_module("ao_lore.evidence_selection")
        with self.assertRaises(module.EvidenceSelectionError):
            module.prepare_evidence_selection(
                self.snapshot,
                "workspace-a",
                (self.document_evidence_id, self.document_evidence_id),
                now="2026-08-14T12:00:00Z",
            )
        with self.assertRaises(module.EvidenceSelectionError):
            module.prepare_evidence_selection(
                self.snapshot,
                "workspace-a",
                (self.edge_evidence_id,),
                now="2026-08-14T12:00:00Z",
            )

    def test_prepare_is_pure_and_reordered_selection_changes_digest(self):
        module = importlib.import_module("ao_lore.evidence_selection")
        with patch("builtins.open", side_effect=AssertionError("I/O attempted")):
            first = module.prepare_evidence_selection(
                self.snapshot,
                "workspace-a",
                (self.document_evidence_id, self.graph_evidence_id),
                now="2026-08-14T12:00:00Z",
            )
        second = module.prepare_evidence_selection(
            self.snapshot,
            "workspace-a",
            (self.graph_evidence_id, self.document_evidence_id),
            now="2026-08-14T12:00:00Z",
        )
        self.assertNotEqual(
            first["evidence_selection_digest"], second["evidence_selection_digest"]
        )

    def test_resolve_selected_evidence_type_guard_and_direct_reference_reachability(self):
        module = importlib.import_module("ao_lore.evidence_selection")
        with self.assertRaises(module.EvidenceSelectionError):
            module._resolve_selected_evidence(object(), "workspace-a", (self.document_evidence_id,))
        with self.assertRaises(module.EvidenceSelectionError):
            module._resolve_selected_evidence(self.snapshot, 7, (self.document_evidence_id,))
        with self.assertRaises(module.EvidenceSelectionError):
            module._resolve_selected_evidence(self.snapshot, "workspace-a", [self.document_evidence_id])

        with_refs = progressive_snapshot(references=True)
        records = module._snapshot_evidence(with_refs)
        reference_id = next(
            record["evidence_id"]
            for record in records.values()
            if record["workspace_id"] == "reference-a"
        )
        without_refs = progressive_snapshot(references=False)
        with self.assertRaises(module.EvidenceSelectionError):
            module._resolve_selected_evidence(without_refs, "workspace-a", (reference_id,))

    def test_resolve_selected_evidence_ambiguity_without_patching(self):
        module = importlib.import_module("ao_lore.evidence_selection")
        graph_a = graph_variant("graph-workspace-a")
        graph_b = graph_variant("graph-reference-a")
        from tests.test_ao_lore_progressive_query import definition
        definitions = [
            definition("workspace-a", None, graph_a, ("reference-a",)),
            definition("reference-a", None, graph_b),
        ]
        definitions.sort(key=lambda item: item["workspace_id"])
        registry_value = {
            "schema_version": "ao.lore.workspace-registry-generation.v0.1",
            "registry_id": "registry-ambiguous",
            "sequence": 1,
            "predecessor_registry_digest": None,
            "generated_at": "2026-08-14T12:00:00Z",
            "workspaces": definitions,
            "registry_digest": canonical_digest("placeholder"),
            "legal_advice": False,
            "property_decision": False,
            "candidate_review": False,
            "candidate_decision": False,
            "promotion": False,
            "canonical_query": False,
            "provider": False,
            "credential": False,
            "private_data": False,
            "publication": False,
            "release": False,
            "deployment": False,
            "authority_advanced": False,
        }
        registry_value["registry_digest"] = canonical_digest(
            {key: item for key, item in registry_value.items() if key != "registry_digest"}
        )
        registry = WorkspaceRegistrySnapshot((registry_value,), tuple(definitions))
        snapshot = WorkspaceQuerySnapshot(
            registry,
            select_workspace(registry, "workspace-a"),
            (
                WorkspaceGraphSnapshot("workspace-a", graph_a),
                WorkspaceGraphSnapshot("reference-a", graph_b),
            ),
        )
        ambiguous_claim_id = graph_a["claims"][0]["claim_id"]
        with self.assertRaises(module.EvidenceSelectionError):
            module._resolve_selected_evidence(snapshot, "workspace-a", (ambiguous_claim_id,))


if __name__ == "__main__":
    unittest.main()
