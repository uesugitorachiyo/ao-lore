import importlib
import json
import unittest
from copy import deepcopy
from pathlib import Path

from ao_lore._strict_io import ContractError
from ao_lore.benchmark import canonical_digest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"


def bind(value, field):
    value[field] = canonical_digest(
        {key: item for key, item in value.items() if key != field}
    )
    return value


def document_evidence(block_id="block-a"):
    return {
        "workspace_id": "workspace-a",
        "document_store_id": "documents-workspace-a",
        "generation_digest": "sha256:" + "1" * 64,
        "document_id": "policy-record",
        "document_ir_digest": "sha256:" + "2" * 64,
        "source_id": "source-policy-record",
        "source_digest": "sha256:" + "3" * 64,
        "evidence_kind": "document_block",
        "evidence_id": canonical_digest(
            {
                "workspace_id": "workspace-a",
                "document_id": "policy-record",
                "block_id": block_id,
            }
        ),
        "evidence_digest": canonical_digest({"block_id": block_id, "text": "control record"}),
        "block_id": block_id,
        "render_text": "control record",
        "source_span": {"page": 1, "start": 0, "end": 13},
        "authority_role": "operator_procedure",
        "sensitivity": "internal",
        "freshness_status": "current",
        "qualification_codes": ["qualified-parser"],
    }


def graph_evidence(claim_id="claim-a"):
    excerpt = "The policy requires a documented control."
    return {
        "workspace_id": "workspace-a",
        "graph_id": "graph-workspace-a",
        "graph_digest": "sha256:" + "4" * 64,
        "source_id": "source-policy",
        "source_digest": "sha256:" + "5" * 64,
        "evidence_kind": "graph_claim",
        "evidence_id": claim_id,
        "evidence_digest": canonical_digest({"claim_id": claim_id, "excerpt": excerpt}),
        "claim_id": claim_id,
        "excerpt": excerpt,
        "citation_anchor": "control-record-01",
        "authority_role": "operator_procedure",
        "freshness_status": "current",
        "qualification_codes": ["topic_match", "citation_supported"],
        "conflict_status": "clear",
        "semantic_review": "verified",
    }


def graph_edge(edge_id="edge-a"):
    return {
        "workspace_id": "workspace-a",
        "graph_id": "graph-workspace-a",
        "graph_digest": "sha256:" + "4" * 64,
        "source_id": "source-policy",
        "source_digest": "sha256:" + "5" * 64,
        "evidence_kind": "graph_edge",
        "evidence_id": edge_id,
        "evidence_digest": canonical_digest({"edge_id": edge_id}),
        "edge_id": edge_id,
        "source_claim_id": "claim-a",
        "target_claim_id": "claim-b",
        "edge_type": "implements",
        "supporting_excerpt_digest": canonical_digest("supporting excerpt"),
        "authority_role": "official_interpretation",
        "freshness_status": "current",
        "qualification_codes": ["technical_remediation"],
        "conflict_status": "clear",
        "semantic_review": "verified",
    }


def selection_evidence_digest(evidence):
    return canonical_digest(
        {
            "domain": "ao.lore.evidence-selection.v0.1",
            "evidence": list(evidence),
        }
    )


def selection_write_set_digest(paths):
    return canonical_digest(
        {
            "domain": "ao.lore.evidence-selection.write-set.v0.1",
            "paths": list(paths),
        }
    )


def selection_candidate_targets(
    proposal_id: str,
    primary_workspace_id: str,
    registry_digest: str,
    evidence,
    *,
    candidate_id: str = "candidate-selection-01",
    created_at: str = "2026-08-14T12:00:00Z",
):
    evidence = list(evidence)
    selection_digest = selection_evidence_digest(evidence)
    document_blocks = [item for item in evidence if item["evidence_kind"] == "document_block"]
    graph_claims = [item for item in evidence if item["evidence_kind"] == "graph_claim"]
    graph_edges = [item for item in evidence if item["evidence_kind"] == "graph_edge"]

    def citation_id(item):
        return "citation-" + canonical_digest(
            {
                "domain": "ao.lore.evidence-candidate.citation-id.v0.1",
                "selection_digest": selection_digest,
                "workspace_id": item["workspace_id"],
                "evidence_id": item["evidence_id"],
            }
        )[7:31]

    def claim_id(item):
        return "claim-" + canonical_digest(
            {
                "domain": "ao.lore.evidence-candidate.claim-id.v0.1",
                "selection_digest": selection_digest,
                "workspace_id": item["workspace_id"],
                "evidence_id": item["evidence_id"],
            }
        )[7:31]

    citation_ids = {}
    citations = []
    for item in [*graph_claims, *document_blocks]:
        current = citation_id(item)
        citation_ids[(item["workspace_id"], item["evidence_kind"], item["evidence_id"])] = current
        citations.append(
            {
                "citation_id": current,
                "render_text": item["excerpt"] if item["evidence_kind"] == "graph_claim" else item["render_text"],
                "source_block_ids": [item["claim_id"] if item["evidence_kind"] == "graph_claim" else item["block_id"]],
            }
        )

    claims = []
    claim_mappings = []
    for item in [*document_blocks, *graph_claims]:
        source_block_id = item["claim_id"] if item["evidence_kind"] == "graph_claim" else item["block_id"]
        current = claim_id(item)
        claims.append(
            {
                "claim_id": current,
                "text": item["excerpt"] if item["evidence_kind"] == "graph_claim" else item["render_text"],
                "source_block_ids": [source_block_id],
                "citation_id": citation_ids[(item["workspace_id"], item["evidence_kind"], item["evidence_id"])],
            }
        )
        claim_mappings.append({"claim_id": current, "block_ids": [source_block_id]})

    selected_claim_ids = {item["claim_id"] for item in graph_claims}
    links = []
    for item in graph_edges:
        if item["source_claim_id"] in selected_claim_ids and item["target_claim_id"] in selected_claim_ids:
            links.append(
                {
                    "source_block_id": item["source_claim_id"],
                    "label": item["edge_type"],
                    "target": item["target_claim_id"],
                    "status": "proposed",
                }
            )

    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.3",
        "candidate_id": candidate_id,
        "evidence_selection_digest": selection_digest,
        "evidence_origins": evidence,
        "concepts": [],
        "claim_mappings": claim_mappings,
        "links": links,
        "contradiction_warnings": [],
        "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
        "claims_digest": canonical_digest(
            {"domain": "ao.lore.canonical-claim-set.v0.1", "claims": claims}
        ),
        "claims": claims,
        "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
        "citations_digest": canonical_digest(
            {"domain": "ao.lore.canonical-citation-set.v0.1", "citations": citations}
        ),
        "citations": citations,
        "knowledge_policy": {
            "sensitivity": "internal"
            if any(item.get("sensitivity", "internal") == "internal" for item in document_blocks)
            else "public",
            "stale_after": None,
        },
        "canonical": False,
        "promotion_authority": False,
    }
    candidate_digest = canonical_digest(candidate)
    provenance = {
        "schema_version": "ao.lore.candidate-provenance.v0.3",
        "candidate_id": candidate_id,
        "candidate_digest": candidate_digest,
        "proposal_id": proposal_id,
        "evidence_selection_digest": selection_digest,
        "primary_workspace_id": primary_workspace_id,
        "registry_digest": registry_digest,
        "evidence_origins": evidence,
        "created_at": created_at,
    }
    return {
        "selection_digest": selection_digest,
        "candidate_digest": candidate_digest,
        "provenance_digest": canonical_digest(provenance),
    }


def selection_request(*, evidence=None):
    if evidence is None:
        evidence = (document_evidence()["evidence_id"],)
    value = {
        "schema_version": "ao.lore.evidence-selection-request.v0.1",
        "request_id": "selection-request-01",
        "primary_workspace_id": "workspace-a",
        "registry_digest": "sha256:" + "6" * 64,
        "requested_at": "2026-08-14T12:00:00Z",
        "evidence_ids": list(evidence),
        "request_digest": "sha256:" + "0" * 64,
        "candidate_creation": False,
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
    return bind(value, "request_digest")


def selection_proposal(*, evidence=None):
    if evidence is None:
        evidence = (document_evidence(),)
    evidence = list(evidence)
    proposal_id = "proposal-selection-01"
    registry_digest = "sha256:" + "6" * 64
    target = selection_candidate_targets(
        proposal_id,
        "workspace-a",
        registry_digest,
        evidence,
    )
    candidate = {
        "candidate_id": "candidate-selection-01",
        "candidate_digest": target["candidate_digest"],
        "provenance_digest": target["provenance_digest"],
    }
    write_set = [
        "candidates/candidate-selection-01/candidate.json",
        "candidates/candidate-selection-01/provenance.json",
        "candidates/candidate-selection-01/reviews",
    ]
    value = {
        "schema_version": "ao.lore.evidence-selection-proposal.v0.1",
        "proposal_id": proposal_id,
        "request_digest": canonical_digest(selection_request()),
        "primary_workspace_id": "workspace-a",
        "registry_digest": registry_digest,
        "definition_digest": "sha256:" + "9" * 64,
        "prepared_at": "2026-08-14T12:00:00Z",
        "expires_at": "2026-08-14T12:05:00Z",
        "evidence_selection_digest": target["selection_digest"],
        "candidate": candidate,
        "allowed_write_set": write_set,
        "allowed_write_set_digest": selection_write_set_digest(write_set),
        "evidence": evidence,
        "proposal_digest": "sha256:" + "0" * 64,
        "candidate_creation": False,
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
    return bind(value, "proposal_digest")


def selection_authorization(*, candidate_review=False):
    value = {
        "schema_version": "ao.lore.evidence-selection-authorization.v0.1",
        "authorization_id": "authorization-selection-01",
        "proposal_id": "proposal-selection-01",
        "proposal_digest": canonical_digest(selection_proposal()),
        "operation": "apply",
        "operator_id": "fixture-operator",
        "nonce": "nonce-selection-01",
        "source_head": "1" * 40,
        "issued_at": "2026-08-14T12:00:00Z",
        "expires_at": "2026-08-14T12:05:00Z",
        "candidate_creation": True,
        "one_use_only": True,
        "product_self_issued": False,
        "legal_advice": False,
        "property_decision": False,
        "candidate_review": candidate_review,
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
        "authorization_digest": "sha256:" + "0" * 64,
    }
    return bind(value, "authorization_digest")


class EvidenceSelectionContractTests(unittest.TestCase):
    def test_proposal_accepts_document_and_graph_evidence_together(self):
        contracts = importlib.import_module("ao_lore.evidence_selection_contracts")
        proposal = selection_proposal(
            evidence=(document_evidence("block-a"), graph_evidence("claim-a")),
        )
        validated = contracts.validate_selection_proposal(proposal)
        self.assertEqual(
            [item["evidence_kind"] for item in validated["evidence"]],
            ["document_block", "graph_claim"],
        )

    def test_authorization_cannot_grant_review_or_promotion(self):
        contracts = importlib.import_module("ao_lore.evidence_selection_contracts")
        authorization = selection_authorization(candidate_review=True)
        with self.assertRaises(ContractError):
            contracts.validate_selection_authorization(authorization)

    def test_request_requires_ordered_unique_evidence_and_closed_authority(self):
        contracts = importlib.import_module("ao_lore.evidence_selection_contracts")
        request = selection_request(
            evidence=(
                document_evidence("block-a")["evidence_id"],
                document_evidence("block-b")["evidence_id"],
            )
        )
        self.assertEqual(request, contracts.validate_selection_request(request))

        duplicated = selection_request(
            evidence=(document_evidence("block-a")["evidence_id"],) * 2
        )
        with self.assertRaises(ContractError):
            contracts.validate_selection_request(duplicated)

        elevated = selection_request()
        elevated["promotion"] = True
        elevated["request_digest"] = canonical_digest(
            {key: value for key, value in elevated.items() if key != "request_digest"}
        )
        with self.assertRaises(ContractError):
            contracts.validate_selection_request(elevated)

    def test_proposal_rejects_private_text_ambiguity_and_selection_budget_overflow(self):
        contracts = importlib.import_module("ao_lore.evidence_selection_contracts")
        private_text = selection_proposal(evidence=(document_evidence(),))
        private_text["evidence"][0]["render_text"] = "/" + "opt/private/path"
        private_text["proposal_digest"] = canonical_digest(
            {key: value for key, value in private_text.items() if key != "proposal_digest"}
        )
        with self.assertRaises(ContractError):
            contracts.validate_selection_proposal(private_text)

        ambiguous = selection_proposal(evidence=(graph_evidence(),))
        ambiguous["evidence"][0]["semantic_review"] = "ambiguous"
        ambiguous["proposal_digest"] = canonical_digest(
            {key: value for key, value in ambiguous.items() if key != "proposal_digest"}
        )
        with self.assertRaises(ContractError):
            contracts.validate_selection_proposal(ambiguous)

        overflow = selection_proposal(
            evidence=tuple(
                document_evidence(f"block-{index:02d}") for index in range(33)
            )
        )
        with self.assertRaises(ContractError):
            contracts.validate_selection_proposal(overflow)

    def test_v0_3_candidate_and_provenance_schemas_are_closed_and_parity_ready(self):
        candidate_schema = json.loads(
            (SCHEMA_ROOT / "okf-candidate-v0.3.schema.json").read_text(encoding="utf-8")
        )
        provenance_schema = json.loads(
            (SCHEMA_ROOT / "candidate-provenance-v0.3.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(candidate_schema["additionalProperties"])
        self.assertFalse(provenance_schema["additionalProperties"])
        self.assertIn("evidence_selection_digest", candidate_schema["required"])
        self.assertIn("evidence_origins", candidate_schema["required"])
        self.assertNotIn("document_ir_digest", candidate_schema["required"])
        self.assertEqual(
            "ao.lore.candidate-provenance.v0.3",
            provenance_schema["properties"]["schema_version"]["const"],
        )
        self.assertIn("proposal_id", provenance_schema["required"])

    def test_selection_schema_family_is_present(self):
        names = {
            "evidence-selection-request-v0.1.schema.json",
            "evidence-selection-proposal-v0.1.schema.json",
            "evidence-selection-authorization-v0.1.schema.json",
            "evidence-selection-consumption-v0.1.schema.json",
            "evidence-selection-transaction-v0.1.schema.json",
            "evidence-selection-inspection-v0.1.schema.json",
            "evidence-selection-recovery-v0.1.schema.json",
            "okf-candidate-v0.3.schema.json",
            "candidate-provenance-v0.3.schema.json",
        }
        self.assertTrue(
            names.issubset({path.name for path in SCHEMA_ROOT.glob("*.schema.json")})
        )


if __name__ == "__main__":
    unittest.main()
