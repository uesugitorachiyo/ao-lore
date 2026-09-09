import importlib
import unittest
from copy import deepcopy
from unittest.mock import patch

from ao_lore.benchmark import canonical_digest
from ao_lore.workspace_query import (
    WorkspaceDocumentQuerySnapshot,
    WorkspaceGraphSnapshot,
    WorkspaceQuerySnapshot,
)
from ao_lore.workspace_registry import WorkspaceRegistrySnapshot, select_workspace
from ao_lore.workspace_contracts import AUTHORITY_FIELDS
from tests.test_ao_lore_evidence_freshness_query import EvidenceFreshnessQueryTests
from tests.test_ao_lore_document_evidence_query import generation as document_generation
from tests.test_ao_lore_evidence_selection_contracts import selection_proposal
from tests.test_ao_lore_workspace_query import graph_variant, rebind


def definition(
    workspace_id,
    document=None,
    graph=None,
    *,
    workspace_version=1,
    freshness_summary=None,
):
    value = {
        "schema_version": "ao.lore.workspace-definition.v0.2",
        "workspace_id": workspace_id,
        "workspace_version": workspace_version,
        "workspace_type": "property",
        "domain": "synthetic-guidance",
        "jurisdiction": "test-jurisdiction",
        "lifecycle_status": "active",
        "root_workflow_id": "workflow-" + workspace_id,
        "root_workflow_digest": canonical_digest("workflow-" + workspace_id),
        "source_registry_id": "sources-" + workspace_id,
        "source_registry_digest": canonical_digest("sources-" + workspace_id),
        "graph_id": None if graph is None else graph["graph_id"],
        "graph_digest": None if graph is None else graph["graph_digest"],
        "freshness_policy_id": None if freshness_summary is None else freshness_summary["policy_id"],
        "freshness_policy_digest": None if freshness_summary is None else freshness_summary["policy_digest"],
        "freshness_summary_status": "not_observed" if freshness_summary is None else "observed",
        "freshness_summary_id": None if freshness_summary is None else freshness_summary["summary_id"],
        "freshness_summary_digest": None if freshness_summary is None else freshness_summary["summary_digest"],
        "reference_workspace_ids": [],
        "document_store_id": "documents-" + workspace_id,
        "document_generation_digest": None if document is None else document["generation_digest"],
        "definition_digest": canonical_digest("placeholder"),
        **{field: False for field in AUTHORITY_FIELDS},
    }
    return rebind(value, "definition_digest")


def combined_snapshot(*, restricted=False, freshness=None):
    documents = document_generation()
    graph = graph_variant("graph-workspace-a", restricted=restricted)
    summary = None
    if freshness is not None:
        helper = EvidenceFreshnessQueryTests()
        helper.graph = graph
        summary = helper.context(freshness)[3]
    registry_value = {
        "schema_version": "ao.lore.workspace-registry-generation.v0.1",
        "registry_id": "registry-01",
        "sequence": 2,
        "predecessor_registry_digest": documents["registry_digest"],
        "generated_at": "2026-08-14T12:00:00Z",
        "workspaces": [definition("workspace-a", documents, graph, freshness_summary=summary)],
        "registry_digest": canonical_digest("placeholder"),
        **{field: False for field in AUTHORITY_FIELDS},
    }
    registry_value["registry_digest"] = canonical_digest(
        {key: item for key, item in registry_value.items() if key != "registry_digest"}
    )
    registry = WorkspaceRegistrySnapshot((registry_value,), tuple(registry_value["workspaces"]))
    return WorkspaceQuerySnapshot(
        registry,
        select_workspace(registry, "workspace-a"),
        (WorkspaceGraphSnapshot("workspace-a", graph, summary),),
        documents=(WorkspaceDocumentQuerySnapshot("workspace-a", documents),),
    )


def document_block_from_snapshot(snapshot, *, block_id="z"):
    generation = snapshot.documents[0]._open()
    document = generation["documents"][0]
    block = next(
        item for item in document["document_ir"]["blocks"] if item["id"] == block_id
    )
    return {
        "workspace_id": "workspace-a",
        "document_store_id": generation["document_store_id"],
        "generation_digest": generation["generation_digest"],
        "document_id": document["document_id"],
        "document_ir_digest": document["document_ir_digest"],
        "source_id": document["source_id"],
        "source_digest": document["source_digest"],
        "evidence_kind": "document_block",
        "evidence_id": canonical_digest(
            {
                "workspace_id": "workspace-a",
                "generation_digest": generation["generation_digest"],
                "document_id": document["document_id"],
                "block_id": block["id"],
            }
        ),
        "evidence_digest": canonical_digest(block),
        "block_id": block["id"],
        "render_text": block["text"],
        "source_span": deepcopy(block["source_span"]),
        "authority_role": document["authority_role"],
        "sensitivity": document["sensitivity"],
        "freshness_status": document["freshness_status"],
        "qualification_codes": deepcopy(document["qualification_codes"]),
    }


def graph_claim_from_snapshot(snapshot):
    graph, freshness = snapshot.graphs[0]._open()
    claim = graph["claims"][0]
    source = next(item for item in graph["sources"] if item["source_id"] == claim["source_id"])
    freshness_status = "current"
    if freshness is not None:
        compared = next(
            item for item in freshness["results"] if item["source_id"] == claim["source_id"]
        )
        freshness_status = "current" if compared["classification"] == "unchanged" else compared["classification"]
    return {
        "workspace_id": "workspace-a",
        "graph_id": graph["graph_id"],
        "graph_digest": graph["graph_digest"],
        "source_id": claim["source_id"],
        "source_digest": source["source_digest"],
        "evidence_kind": "graph_claim",
        "evidence_id": claim["claim_id"],
        "evidence_digest": claim["excerpt_digest"],
        "claim_id": claim["claim_id"],
        "excerpt": claim["excerpt"],
        "citation_anchor": claim["citation_anchor"],
        "authority_role": claim["authority_role"],
        "freshness_status": freshness_status,
        "qualification_codes": deepcopy(claim["semantic_reason_codes"]),
        "conflict_status": "clear",
        "semantic_review": "verified",
    }


def graph_edge_from_snapshot(snapshot):
    graph, freshness = snapshot.graphs[0]._open()
    edge = graph["edges"][0]
    if edge["source_kind"] == "claim":
        claim = next(item for item in graph["claims"] if item["claim_id"] == edge["source_id"])
        source_id = claim["source_id"]
    elif edge["source_kind"] == "source":
        source_id = edge["source_id"]
    else:
        source_id = graph["claims"][0]["source_id"]
    source = next(item for item in graph["sources"] if item["source_id"] == source_id)
    freshness_status = "current"
    if freshness is not None:
        compared = next(
            item for item in freshness["results"] if item["source_id"] == source_id
        )
        freshness_status = "current" if compared["classification"] == "unchanged" else compared["classification"]
    return {
        "workspace_id": "workspace-a",
        "graph_id": graph["graph_id"],
        "graph_digest": graph["graph_digest"],
        "source_id": source["source_id"],
        "source_digest": source["source_digest"],
        "evidence_kind": "graph_edge",
        "evidence_id": edge["edge_id"],
        "evidence_digest": edge["edge_digest"],
        "edge_id": edge["edge_id"],
        "source_claim_id": edge["source_id"],
        "target_claim_id": edge["target_id"],
        "edge_type": edge["edge_type"],
        "supporting_excerpt_digest": edge["supporting_excerpt_digest"],
        "authority_role": source["authority_role"],
        "freshness_status": freshness_status,
        "qualification_codes": [edge["reason_code"]],
        "conflict_status": "clear",
        "semantic_review": "verified",
    }


class EvidenceCandidateTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = combined_snapshot()
        self.document_block = document_block_from_snapshot(self.snapshot)
        self.graph_claim = graph_claim_from_snapshot(self.snapshot)
        self.graph_edge = graph_edge_from_snapshot(self.snapshot)
        self.proposal = selection_proposal(
            evidence=(self.document_block, self.graph_claim)
        )
        self.edge_only_proposal = selection_proposal(evidence=(self.graph_edge,))

    def test_selected_blocks_and_claims_copy_exact_text(self):
        module = importlib.import_module("ao_lore.evidence_candidate")
        candidate, provenance = module.materialize_evidence_candidate(
            self.proposal, self.snapshot
        )
        self.assertEqual(
            candidate["claims"][0]["text"], self.document_block["render_text"]
        )
        self.assertEqual(
            candidate["citations"][0]["render_text"], self.graph_claim["excerpt"]
        )
        self.assertFalse(candidate["canonical"])
        self.assertFalse(candidate["promotion_authority"])
        self.assertEqual(
            candidate["evidence_selection_digest"],
            provenance["evidence_selection_digest"],
        )

    def test_edge_only_selection_cannot_invent_claim_text(self):
        module = importlib.import_module("ao_lore.evidence_candidate")
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(
                self.edge_only_proposal, self.snapshot
            )

    def test_materialization_rejects_drift_and_ineligible_selected_evidence(self):
        module = importlib.import_module("ao_lore.evidence_candidate")

        drifted = deepcopy(self.proposal)
        drifted["evidence"][0]["evidence_digest"] = "sha256:" + "f" * 64
        drifted["proposal_digest"] = canonical_digest(
            {key: value for key, value in drifted.items() if key != "proposal_digest"}
        )
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(drifted, self.snapshot)

        stale = deepcopy(self.proposal)
        stale["evidence"][0]["freshness_status"] = "stale"
        stale["proposal_digest"] = canonical_digest(
            {key: value for key, value in stale.items() if key != "proposal_digest"}
        )
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(stale, self.snapshot)

        conflicting = deepcopy(self.proposal)
        conflicting["evidence"][1]["conflict_status"] = "conflicted"
        conflicting["proposal_digest"] = canonical_digest(
            {
                key: value
                for key, value in conflicting.items()
                if key != "proposal_digest"
            }
        )
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(conflicting, self.snapshot)

        restricted_snapshot = combined_snapshot(restricted=True)
        restricted_graph_claim = selection_proposal(
            evidence=(
                document_block_from_snapshot(restricted_snapshot),
                graph_claim_from_snapshot(restricted_snapshot),
            )
        )
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(
                restricted_graph_claim, restricted_snapshot
            )

        edge_restricted = selection_proposal(
            evidence=(self.document_block, self.graph_claim, self.graph_edge)
        )
        edge_restricted["evidence"][2]["authority_role"] = "case_evidence"
        edge_restricted["proposal_digest"] = canonical_digest(
            {key: value for key, value in edge_restricted.items() if key != "proposal_digest"}
        )
        patched_records = module._snapshot_evidence(self.snapshot)
        patched_records[(
            "workspace-a",
            "graph_edge",
            edge_restricted["evidence"][2]["evidence_id"],
        )] = deepcopy(edge_restricted["evidence"][2])
        with patch.object(module, "_snapshot_evidence", return_value=patched_records):
            with self.assertRaises(module.EvidenceCandidateError):
                module.materialize_evidence_candidate(edge_restricted, self.snapshot)

        stale_snapshot = combined_snapshot(freshness={"policy-record": "updated"})
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(self.proposal, stale_snapshot)

    def test_materialization_requires_proposal_target_digest_match(self):
        module = importlib.import_module("ao_lore.evidence_candidate")
        mismatched = deepcopy(self.proposal)
        mismatched["candidate"]["candidate_digest"] = "sha256:" + "f" * 64
        mismatched["proposal_digest"] = canonical_digest(
            {key: value for key, value in mismatched.items() if key != "proposal_digest"}
        )
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(mismatched, self.snapshot)

        mismatched_provenance = deepcopy(self.proposal)
        mismatched_provenance["candidate"]["provenance_digest"] = "sha256:" + "e" * 64
        mismatched_provenance["proposal_digest"] = canonical_digest(
            {
                key: value
                for key, value in mismatched_provenance.items()
                if key != "proposal_digest"
            }
        )
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(
                mismatched_provenance, self.snapshot
            )

        drifted_proposal_id = deepcopy(self.proposal)
        drifted_proposal_id["proposal_id"] = "proposal-selection-02"
        drifted_proposal_id["proposal_digest"] = canonical_digest(
            {
                key: value
                for key, value in drifted_proposal_id.items()
                if key != "proposal_digest"
            }
        )
        with self.assertRaises(module.EvidenceCandidateError):
            module.materialize_evidence_candidate(drifted_proposal_id, self.snapshot)

    def test_materialization_is_deterministic_and_detached(self):
        module = importlib.import_module("ao_lore.evidence_candidate")
        candidate_one, provenance_one = module.materialize_evidence_candidate(
            self.proposal, self.snapshot
        )
        candidate_two, provenance_two = module.materialize_evidence_candidate(
            self.proposal, self.snapshot
        )
        self.assertEqual(candidate_one, candidate_two)
        self.assertEqual(provenance_one, provenance_two)

        candidate_one["claims"][0]["text"] = "changed"
        provenance_one["evidence_origins"][0]["block_id"] = "mutated"
        rerun_candidate, rerun_provenance = module.materialize_evidence_candidate(
            self.proposal, self.snapshot
        )
        self.assertEqual(self.document_block["render_text"], rerun_candidate["claims"][0]["text"])
        self.assertEqual(self.document_block["block_id"], rerun_provenance["evidence_origins"][0]["block_id"])


if __name__ == "__main__":
    unittest.main()
