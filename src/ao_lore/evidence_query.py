"""Deterministic, authority-aware retrieval over one validated evidence graph."""

from __future__ import annotations

import re
import unicodedata

from .benchmark import canonical_digest
from .evidence_graph_contracts import (
    AUTHORITY_FIELDS,
    AUTHORITY_ROLES,
    validate_detached_freshness_summary_against_manifest,
    validate_graph_manifest,
    validate_query_readback_against_manifest,
)


class EvidenceQueryError(ValueError):
    pass


def _tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return {token for token in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE) if len(token) >= 3}


def _allocate_freshness_evidence(
    evidence_ids: list[str], affected_source_ids: set[str], limit: int,
) -> list[str]:
    mandatory = sorted(affected_source_ids)
    if len(mandatory) > limit:
        raise EvidenceQueryError("limit cannot represent freshness evidence")
    remaining = sorted(set(evidence_ids) - affected_source_ids)
    return sorted([*mandatory, *remaining[:limit - len(mandatory)]])


def _selected_evidence_records(graph: dict, evidence_ids: list[str]) -> list[dict]:
    """Resolve one graph readback to claim/edge records without widening it.

    Source identities used by the single-graph freshness gate resolve to the
    claims whose excerpts are the renderable evidence.  This helper performs
    no I/O and intentionally retains the legacy query readback shape.
    """

    graph = validate_graph_manifest(graph)
    if type(evidence_ids) is not list:
        raise EvidenceQueryError("evidence identities are invalid")
    claims = {item["claim_id"]: item for item in graph["claims"]}
    claims_by_source: dict[str, list[dict]] = {}
    for claim in graph["claims"]:
        claims_by_source.setdefault(claim["source_id"], []).append(claim)
    edges = {item["edge_id"]: item for item in graph["edges"]}

    def edge_source(edge: dict) -> str:
        if edge["source_kind"] == "source":
            return edge["source_id"]
        if edge["source_kind"] == "claim":
            return claims[edge["source_id"]]["source_id"]
        if edge["target_kind"] == "source":
            return edge["target_id"]
        if edge["target_kind"] == "claim":
            return claims[edge["target_id"]]["source_id"]
        raise EvidenceQueryError("edge lacks a source evidence binding")

    records = []
    for evidence_id in evidence_ids:
        if evidence_id in claims:
            claim = claims[evidence_id]
            records.append({
                "source_id": claim["source_id"], "evidence_kind": "claim",
                "evidence_id": claim["claim_id"],
                "evidence_digest": claim["excerpt_digest"],
                "authority_role": claim["authority_role"],
            })
        elif evidence_id in edges:
            edge = edges[evidence_id]
            records.append({
                "source_id": edge_source(edge), "evidence_kind": "edge",
                "evidence_id": edge["edge_id"],
                "evidence_digest": edge["edge_digest"],
                "authority_role": None,
            })
        elif evidence_id in claims_by_source:
            for claim in sorted(claims_by_source[evidence_id], key=lambda item: item["claim_id"]):
                records.append({
                    "source_id": claim["source_id"], "evidence_kind": "claim",
                    "evidence_id": claim["claim_id"],
                    "evidence_digest": claim["excerpt_digest"],
                    "authority_role": claim["authority_role"],
                })
        else:
            raise EvidenceQueryError("query evidence identity is absent from graph")
    return sorted(
        {tuple(item.values()): item for item in records}.values(),
        key=lambda item: tuple("" if value is None else value for value in item.values()),
    )


def query_evidence_graph(
    graph: dict,
    prompt: str,
    *,
    limit: int = 64,
    freshness_summary: dict | None = None,
) -> dict:
    try:
        graph = validate_graph_manifest(graph)
    except Exception as exc:
        raise EvidenceQueryError("graph is invalid") from exc
    if type(prompt) is not str or not 1 <= len(prompt) <= 1024 or any(ord(c) < 32 for c in prompt):
        raise EvidenceQueryError("prompt is invalid")
    if type(limit) is not int or not 1 <= limit <= 64:
        raise EvidenceQueryError("limit is invalid")
    freshness = None
    if freshness_summary is not None:
        freshness = validate_detached_freshness_summary_against_manifest(
            freshness_summary, graph,
        )
    query = _tokens(prompt)
    scored = []
    for claim in graph["claims"]:
        haystack = _tokens(" ".join((claim["excerpt"], claim["citation_anchor"], " ".join(claim["subject_terms"]))))
        score = len(query & haystack)
        if score:
            scored.append((score, claim["claim_id"], claim))
    scored.sort(key=lambda row: (-row[0], row[1]))
    matching_claims = [row[2] for row in scored]
    selected = matching_claims[:limit]
    selected_ids = [item["claim_id"] for item in selected]
    selected_roles = sorted({item["authority_role"] for item in selected})
    source_by = {item["source_id"]: item for item in graph["sources"]}
    selected_sources = [source_by[item["source_id"]] for item in selected]
    selected_roles = [role for role in AUTHORITY_ROLES if role in set(selected_roles)]
    matching_identities = {
        identity
        for claim in matching_claims
        for identity in (
            ("claim", claim["claim_id"]),
            ("source", claim["source_id"]),
        )
    }
    conflicts = [
        edge for edge in graph["edges"]
        if edge["edge_type"] == "conflicts_with" and (
            (edge["source_kind"], edge["source_id"]) in matching_identities
            or (edge["target_kind"], edge["target_id"]) in matching_identities
        )
    ]
    conflict_ids = set()
    for edge in conflicts:
        for kind, identity in (
            (edge["source_kind"], edge["source_id"]),
            (edge["target_kind"], edge["target_id"]),
        ):
            conflict_ids.add(edge["edge_id"] if kind == "workflow" else identity)
    if len(conflict_ids) > limit:
        raise EvidenceQueryError("limit cannot represent conflict evidence")
    selected_ids = [
        *sorted(conflict_ids),
        *sorted(set(selected_ids) - conflict_ids)[:limit - len(conflict_ids)],
    ]
    # Conflict and freshness projection may add claim/source endpoint identities
    # after lexical selection. Re-resolve the projected records so roles and
    # qualifications describe exactly the returned evidence identities.
    selected_ids = sorted(set(selected_ids))[:limit]
    claims_by_id = {claim["claim_id"]: claim for claim in graph["claims"]}
    selected_claims = [claims_by_id[item] for item in selected_ids if item in claims_by_id]
    selected_sources = [source_by[item] for item in selected_ids if item in source_by]
    selected_sources.extend(
        source_by[claim["source_id"]]
        for claim in selected_claims
        if claim["source_id"] in source_by and source_by[claim["source_id"]] not in selected_sources
    )
    selected_roles = [
        role for role in AUTHORITY_ROLES
        if role in {
            *[claim["authority_role"] for claim in selected_claims],
            *[source["authority_role"] for source in selected_sources],
        }
    ]
    stale = [item for item in selected_sources if item["status"] != "current"]
    affected_sources = set()
    if freshness is not None:
        source_for_claim = {item["claim_id"]: item["source_id"] for item in graph["claims"]}
        sources_for_evidence = {
            item["source_id"]: {item["source_id"]} for item in graph["sources"]
        }
        sources_for_evidence.update({
            claim_id: {source_id} for claim_id, source_id in source_for_claim.items()
        })
        for edge in graph["edges"]:
            related = set()
            if edge["source_kind"] == "source":
                related.add(edge["source_id"])
            elif edge["source_kind"] == "claim":
                related.add(source_for_claim[edge["source_id"]])
            if edge["target_kind"] == "source":
                related.add(edge["target_id"])
            elif edge["target_kind"] == "claim":
                related.add(source_for_claim[edge["target_id"]])
            sources_for_evidence[edge["edge_id"]] = related
        selected_source_ids = {claim["source_id"] for claim in matching_claims}
        for evidence_id in selected_ids:
            selected_source_ids.update(sources_for_evidence.get(evidence_id, ()))
        affected_sources = {
            item["source_id"] for item in freshness["results"]
            if item["classification"] != "unchanged"
            and item["source_id"] in selected_source_ids
        }
    qualifications = []
    if conflicts or stale:
        outcome = "investigate"
        mandatory_ids = {
            *conflict_ids,
            *(item["source_id"] for item in stale),
            *affected_sources,
        }
        if len(mandatory_ids) > limit:
            raise EvidenceQueryError("limit cannot represent required gate evidence")
        selected_ids = [
            *sorted(mandatory_ids),
            *sorted(set(selected_ids) - mandatory_ids)[:limit - len(mandatory_ids)],
        ]
        qualifications.append("Conflicting or stale evidence remains unresolved.")
        if affected_sources:
            qualifications.append("Source freshness requires review.")
    elif affected_sources:
        outcome = "investigate"
        selected_ids = _allocate_freshness_evidence(
            selected_ids, affected_sources, limit,
        )
        qualifications.append("Source freshness requires review.")
    elif not selected_ids:
        outcome = "refuse"
        qualifications = ["The connected public graph does not support this question."]
    else:
        outcome = "answer" if len(selected_roles) >= 2 else "partial"
    selected_ids = sorted(set(selected_ids))[:limit]
    selected_claims = [claims_by_id[item] for item in selected_ids if item in claims_by_id]
    selected_sources = [source_by[item] for item in selected_ids if item in source_by]
    selected_sources.extend(
        source_by[claim["source_id"]]
        for claim in selected_claims
        if claim["source_id"] in source_by and source_by[claim["source_id"]] not in selected_sources
    )
    selected_roles = [
        role for role in AUTHORITY_ROLES
        if role in {
            *[claim["authority_role"] for claim in selected_claims],
            *[source["authority_role"] for source in selected_sources],
        }
    ]
    qualifications = []
    if "primary_law" in selected_roles:
        qualifications.append("Primary source predicates require review.")
    if "official_interpretation" in selected_roles:
        qualifications.append("Interpretation does not replace the primary source.")
    if "local_enforcement_guidance" in selected_roles:
        qualifications.append("Source scope requires applicability review.")
    if "technical_guidance" in selected_roles:
        qualifications.append("Technical guidance requires source review.")
    if conflicts or stale:
        qualifications.append("Conflicting or stale evidence remains unresolved.")
    if affected_sources:
        qualifications.append("Source freshness requires review.")
    if not selected_ids:
        qualifications = ["The connected public graph does not support this question."]
    core = {
        "schema_version": "ao.lore.evidence-query-readback.v0.1",
        "query_id": "query-" + canonical_digest({"prompt": prompt, "graph": graph["graph_digest"]}).split(":", 1)[1][:24],
        "prompt_digest": canonical_digest(prompt), "graph_digest": graph["graph_digest"],
        "outcome": outcome, "evidence_ids": selected_ids,
        "authority_roles": selected_roles, "qualifications": sorted(set(qualifications)),
        "readback_digest": "", **{field: False for field in AUTHORITY_FIELDS},
    }
    core["readback_digest"] = canonical_digest({key: value for key, value in core.items() if key != "readback_digest"})
    return validate_query_readback_against_manifest(core, graph)
