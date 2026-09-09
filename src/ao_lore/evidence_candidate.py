"""Pure exact-text candidate materialization from governed evidence selections."""

from __future__ import annotations

import copy
import json
from typing import Any

from .benchmark import canonical_digest
from .evidence_selection_contracts import validate_selection_proposal
from .workspace_query import WorkspaceQuerySnapshot


class EvidenceCandidateError(ValueError):
    """Selected evidence cannot safely become a candidate."""


def _fail(message: str, exc: Exception | None = None) -> EvidenceCandidateError:
    error = EvidenceCandidateError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _document_blocks(blocks: list[dict[str, Any]]):
    for block in blocks:
        yield block
        yield from _document_blocks(block.get("children", []))


def _snapshot_evidence(snapshot: WorkspaceQuerySnapshot) -> dict[tuple[str, str, str], dict[str, Any]]:
    if type(snapshot) is not WorkspaceQuerySnapshot:
        raise _fail("workspace snapshot is invalid")
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    _generation, graphs, documents = snapshot._open_progressive()
    for workspace_id, generation in documents.items():
        for document in generation["documents"]:
            for block in _document_blocks(document["document_ir"]["blocks"]):
                record = {
                    "workspace_id": workspace_id,
                    "document_store_id": generation["document_store_id"],
                    "generation_digest": generation["generation_digest"],
                    "document_id": document["document_id"],
                    "document_ir_digest": document["document_ir_digest"],
                    "source_id": document["source_id"],
                    "source_digest": document["source_digest"],
                    "evidence_kind": "document_block",
                    "evidence_id": canonical_digest(
                        {
                            "workspace_id": workspace_id,
                            "generation_digest": generation["generation_digest"],
                            "document_id": document["document_id"],
                            "block_id": block["id"],
                        }
                    ),
                    "evidence_digest": canonical_digest(block),
                    "block_id": block["id"],
                    "render_text": block["text"],
                    "source_span": copy.deepcopy(block["source_span"]),
                    "authority_role": document["authority_role"],
                    "sensitivity": document["sensitivity"],
                    "freshness_status": document["freshness_status"],
                    "qualification_codes": copy.deepcopy(document["qualification_codes"]),
                }
                records[(workspace_id, "document_block", record["evidence_id"])] = record
    for workspace_id, (graph, freshness) in graphs.items():
        sources = {item["source_id"]: item for item in graph["sources"]}
        claims = {item["claim_id"]: item for item in graph["claims"]}
        freshness_by_source = {}
        if freshness is not None:
            freshness_by_source = {
                item["source_id"]: item["classification"] for item in freshness["results"]
            }
        conflicted_claim_ids = {
            edge["source_id"]
            for edge in graph["edges"]
            if edge["edge_type"] == "conflicts_with" and edge["source_kind"] == "claim"
        } | {
            edge["target_id"]
            for edge in graph["edges"]
            if edge["edge_type"] == "conflicts_with" and edge["target_kind"] == "claim"
        }
        for claim in graph["claims"]:
            freshness_status = freshness_by_source.get(claim["source_id"], "unchanged")
            record = {
                "workspace_id": workspace_id,
                "graph_id": graph["graph_id"],
                "graph_digest": graph["graph_digest"],
                "source_id": claim["source_id"],
                "source_digest": sources[claim["source_id"]]["source_digest"],
                "evidence_kind": "graph_claim",
                "evidence_id": claim["claim_id"],
                "evidence_digest": claim["excerpt_digest"],
                "claim_id": claim["claim_id"],
                "excerpt": claim["excerpt"],
                "citation_anchor": claim["citation_anchor"],
                "authority_role": claim["authority_role"],
                "freshness_status": "current" if freshness_status == "unchanged" else freshness_status,
                "qualification_codes": copy.deepcopy(claim.get("semantic_reason_codes", [])),
                "conflict_status": "conflicted" if claim["claim_id"] in conflicted_claim_ids else "clear",
                "semantic_review": "verified",
            }
            records[(workspace_id, "graph_claim", record["evidence_id"])] = record
        for edge in graph["edges"]:
            if edge["source_kind"] == "claim":
                source_id = claims[edge["source_id"]]["source_id"]
            elif edge["source_kind"] == "source":
                source_id = edge["source_id"]
            else:
                source_id = graph["sources"][0]["source_id"]
            freshness_status = freshness_by_source.get(source_id, "unchanged")
            record = {
                "workspace_id": workspace_id,
                "graph_id": graph["graph_id"],
                "graph_digest": graph["graph_digest"],
                "source_id": source_id,
                "source_digest": sources[source_id]["source_digest"],
                "evidence_kind": "graph_edge",
                "evidence_id": edge["edge_id"],
                "evidence_digest": edge["edge_digest"],
                "edge_id": edge["edge_id"],
                "source_claim_id": edge["source_id"],
                "target_claim_id": edge["target_id"],
                "edge_type": edge["edge_type"],
                "supporting_excerpt_digest": edge["supporting_excerpt_digest"],
                "authority_role": sources[source_id]["authority_role"],
                "freshness_status": "current" if freshness_status == "unchanged" else freshness_status,
                "qualification_codes": [edge["reason_code"]],
                "conflict_status": "conflicted" if edge["edge_type"] == "conflicts_with" else "clear",
                "semantic_review": "verified",
            }
            records[(workspace_id, "graph_edge", record["evidence_id"])] = record
    return records


def _citation_id(selection_digest: str, evidence: dict[str, Any]) -> str:
    return "citation-" + canonical_digest(
        {
            "domain": "ao.lore.evidence-candidate.citation-id.v0.1",
            "selection_digest": selection_digest,
            "workspace_id": evidence["workspace_id"],
            "evidence_id": evidence["evidence_id"],
        }
    )[7:31]


def _claim_id(selection_digest: str, evidence: dict[str, Any]) -> str:
    return "claim-" + canonical_digest(
        {
            "domain": "ao.lore.evidence-candidate.claim-id.v0.1",
            "selection_digest": selection_digest,
            "workspace_id": evidence["workspace_id"],
            "evidence_id": evidence["evidence_id"],
        }
    )[7:31]


def materialize_evidence_candidate(
    proposal: dict[str, object],
    snapshot: WorkspaceQuerySnapshot,
) -> tuple[dict[str, object], dict[str, object]]:
    """Return exact v0.3 candidate and provenance without filesystem I/O."""

    try:
        validated = validate_selection_proposal(copy.deepcopy(proposal))
    except Exception as exc:
        raise _fail("selection proposal is invalid", exc)
    records = _snapshot_evidence(snapshot)
    selected = []
    for evidence in validated["evidence"]:
        key = (evidence["workspace_id"], evidence["evidence_kind"], evidence["evidence_id"])
        current = records.get(key)
        if current is None or current != evidence:
            raise _fail("selected evidence drifted from the sealed snapshot")
        if evidence["freshness_status"] != "current":
            raise _fail("selected evidence freshness requires review")
        if evidence["evidence_kind"] == "document_block" and evidence["sensitivity"] == "restricted":
            raise _fail("restricted document evidence cannot materialize a candidate")
        if evidence["evidence_kind"] in {"graph_claim", "graph_edge"}:
            if evidence["authority_role"] == "case_evidence":
                raise _fail("restricted graph evidence cannot materialize a candidate")
            if evidence["freshness_status"] != "current":
                raise _fail("graph evidence freshness requires review")
            if evidence["conflict_status"] != "clear":
                raise _fail("conflicting graph evidence cannot materialize a candidate")
            if evidence["semantic_review"] != "verified":
                raise _fail("ambiguous graph evidence cannot materialize a candidate")
        selected.append(copy.deepcopy(evidence))

    document_blocks = [item for item in selected if item["evidence_kind"] == "document_block"]
    graph_claims = [item for item in selected if item["evidence_kind"] == "graph_claim"]
    graph_edges = [item for item in selected if item["evidence_kind"] == "graph_edge"]
    if not document_blocks and not graph_claims:
        raise _fail("selection must include at least one document block or graph claim")

    selected_claim_ids = {item["claim_id"] for item in graph_claims}
    links = []
    for edge in graph_edges:
        if edge["source_claim_id"] not in selected_claim_ids or edge["target_claim_id"] not in selected_claim_ids:
            raise _fail("selected graph edges require selected endpoints")
        links.append(
            {
                "source_block_id": edge["source_claim_id"],
                "label": edge["edge_type"],
                "target": edge["target_claim_id"],
                "status": "proposed",
            }
        )

    selection_digest = validated["evidence_selection_digest"]
    citations = []
    citation_ids: dict[tuple[str, str, str], str] = {}
    for evidence in [*graph_claims, *document_blocks]:
        citation_id = _citation_id(selection_digest, evidence)
        citation_ids[(evidence["workspace_id"], evidence["evidence_kind"], evidence["evidence_id"])] = citation_id
        citations.append(
            {
                "citation_id": citation_id,
                "render_text": evidence["excerpt"] if evidence["evidence_kind"] == "graph_claim" else evidence["render_text"],
                "source_block_ids": [evidence["claim_id"] if evidence["evidence_kind"] == "graph_claim" else evidence["block_id"]],
            }
        )

    claims = []
    claim_mappings = []
    for evidence in [*document_blocks, *graph_claims]:
        key = (evidence["workspace_id"], evidence["evidence_kind"], evidence["evidence_id"])
        source_block_id = evidence["claim_id"] if evidence["evidence_kind"] == "graph_claim" else evidence["block_id"]
        claim_id = _claim_id(selection_digest, evidence)
        claims.append(
            {
                "claim_id": claim_id,
                "text": evidence["excerpt"] if evidence["evidence_kind"] == "graph_claim" else evidence["render_text"],
                "source_block_ids": [source_block_id],
                "citation_id": citation_ids[key],
            }
        )
        claim_mappings.append({"claim_id": claim_id, "block_ids": [source_block_id]})

    knowledge_sensitivity = (
        "internal"
        if any(item.get("sensitivity", "internal") == "internal" for item in document_blocks)
        else "public"
    )
    candidate: dict[str, object] = {
        "schema_version": "ao.lore.okf-candidate.v0.3",
        "candidate_id": validated["candidate"]["candidate_id"],
        "evidence_selection_digest": selection_digest,
        "evidence_origins": selected,
        "concepts": [],
        "claim_mappings": claim_mappings,
        "links": links,
        "contradiction_warnings": [],
        "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
        "claims_digest": canonical_digest({"domain": "ao.lore.canonical-claim-set.v0.1", "claims": claims}),
        "claims": claims,
        "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
        "citations_digest": canonical_digest({"domain": "ao.lore.canonical-citation-set.v0.1", "citations": citations}),
        "citations": citations,
        "knowledge_policy": {"sensitivity": knowledge_sensitivity, "stale_after": None},
        "canonical": False,
        "promotion_authority": False,
    }
    candidate_digest = canonical_digest(candidate)
    provenance: dict[str, object] = {
        "schema_version": "ao.lore.candidate-provenance.v0.3",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate_digest,
        "proposal_id": validated["proposal_id"],
        "evidence_selection_digest": selection_digest,
        "primary_workspace_id": validated["primary_workspace_id"],
        "registry_digest": validated["registry_digest"],
        "evidence_origins": selected,
        "created_at": validated["prepared_at"],
    }
    from .candidates import validate_candidate_document, validate_provenance

    validated_candidate, actual_digest = validate_candidate_document(candidate)
    if actual_digest != validated["candidate"]["candidate_digest"]:
        raise _fail("materialized candidate digest differs from proposal target")
    validated_provenance = validate_provenance(
        provenance, validated_candidate, actual_digest
    )
    actual_provenance_digest = canonical_digest(validated_provenance)
    if actual_provenance_digest != validated["candidate"]["provenance_digest"]:
        raise _fail("materialized provenance digest differs from proposal target")
    return (
        json.loads(json.dumps(validated_candidate)),
        json.loads(json.dumps(validated_provenance)),
    )
