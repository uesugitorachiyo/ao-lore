"""Strict dependency-free validators for answerable knowledge payloads."""

from __future__ import annotations

import copy
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .benchmark import BenchmarkError, canonical_digest
from ._strict_io import ContractError
from .evidence_selection_contracts import validate_selection_evidence


class KnowledgeContractError(ValueError):
    """Raised when an answerable knowledge payload violates its contract."""


_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _object(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise KnowledgeContractError(f"{label} keys differ")
    return value


def _identifier(value: Any, label: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise KnowledgeContractError(f"{label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise KnowledgeContractError(f"{label} is invalid")
    return value


def _ids(value: Any, label: str, *, maximum: int = 5000, nonempty: bool = True) -> list[str]:
    if type(value) is not list or len(value) > maximum or (nonempty and not value):
        raise KnowledgeContractError(f"{label} is invalid")
    result = [_identifier(item, label) for item in value]
    if len(result) != len(set(result)):
        raise KnowledgeContractError(f"{label} contains duplicates")
    return result


def _text(value: Any, label: str, maximum: int, *, nonempty: bool = True) -> str:
    if type(value) is not str or len(value) > maximum or (nonempty and not value):
        raise KnowledgeContractError(f"{label} is invalid")
    return value


def validate_claims(claims: Any) -> list[dict[str, Any]]:
    if type(claims) is not list or len(claims) > 5000:
        raise KnowledgeContractError("claims are invalid")
    result: list[dict[str, Any]] = []
    identities: set[str] = set()
    for raw in claims:
        claim = _object(raw, {"claim_id", "text", "source_block_ids", "citation_id"}, "claim")
        claim_id = _identifier(claim["claim_id"], "claim_id")
        if claim_id in identities:
            raise KnowledgeContractError("claim identities are duplicated")
        identities.add(claim_id)
        result.append({
            "claim_id": claim_id,
            "text": _text(claim["text"], "claim text", 1024),
            "source_block_ids": _ids(claim["source_block_ids"], "claim source blocks"),
            "citation_id": _identifier(claim["citation_id"], "citation_id"),
        })
    return result


def validate_citations(citations: Any) -> list[dict[str, Any]]:
    if type(citations) is not list or len(citations) > 5000:
        raise KnowledgeContractError("citations are invalid")
    result: list[dict[str, Any]] = []
    identities: set[str] = set()
    for raw in citations:
        citation = _object(raw, {"citation_id", "render_text", "source_block_ids"}, "citation")
        citation_id = _identifier(citation["citation_id"], "citation_id")
        if citation_id in identities:
            raise KnowledgeContractError("citation identities are duplicated")
        identities.add(citation_id)
        result.append({
            "citation_id": citation_id,
            "render_text": _text(citation["render_text"], "citation render text", 512),
            "source_block_ids": _ids(citation["source_block_ids"], "citation source blocks"),
        })
    return result


def validate_knowledge_policy(policy: Any) -> dict[str, Any]:
    value = _object(policy, {"sensitivity", "stale_after"}, "knowledge policy")
    if value["sensitivity"] not in {"public", "internal", "restricted"}:
        raise KnowledgeContractError("knowledge sensitivity is invalid")
    stale_after = value["stale_after"]
    if stale_after is not None:
        if type(stale_after) is not str or len(stale_after) > 32 or not stale_after.endswith("Z"):
            raise KnowledgeContractError("knowledge stale_after is invalid")
        try:
            parsed = datetime.fromisoformat(stale_after[:-1] + "+00:00")
        except ValueError as exc:
            raise KnowledgeContractError("knowledge stale_after is invalid") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise KnowledgeContractError("knowledge stale_after is invalid")
    return {"sensitivity": value["sensitivity"], "stale_after": stale_after}


def validate_distillation_trace(trace: Any, candidate: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "adapter", "policy_version", "input_block_count", "candidate_concept_count",
        "candidate_claim_count", "candidate_citation_count", "private_reasoning_persisted",
    }
    value = _object(trace, expected, "answerable distillation trace")
    for field in ("adapter", "policy_version"):
        _text(value[field], field, 128)
    for field, maximum in (("input_block_count", 100000), ("candidate_concept_count", 10000),
                           ("candidate_claim_count", 5000), ("candidate_citation_count", 5000)):
        if type(value[field]) is not int or not 0 <= value[field] <= maximum:
            raise KnowledgeContractError("answerable distillation trace count is invalid")
    if value["private_reasoning_persisted"] is not False:
        raise KnowledgeContractError("answerable distillation trace persists private reasoning")
    if (value["candidate_concept_count"] != len(candidate["concepts"])
            or value["candidate_claim_count"] != len(candidate["claims"])
            or value["candidate_citation_count"] != len(candidate["citations"])):
        raise KnowledgeContractError("answerable distillation trace counts differ")
    return copy.deepcopy(value)


def _validate_semantic_arrays(value: Mapping[str, Any]) -> None:
    concepts = value["concepts"]
    mappings = value["claim_mappings"]
    links = value["links"]
    if type(concepts) is not list or len(concepts) > 10000:
        raise KnowledgeContractError("concepts are invalid")
    if type(mappings) is not list or len(mappings) > 10000:
        raise KnowledgeContractError("claim mappings are invalid")
    if type(links) is not list or len(links) > 10000:
        raise KnowledgeContractError("links are invalid")
    for concept in concepts:
        if type(concept) is not dict or set(concept) not in (
            {"candidate_concept_id", "title", "source_block_ids", "status"},
            {"candidate_concept_id", "title", "source_block_ids", "status", "candidate_claim_ids"},
        ):
            raise KnowledgeContractError("concept keys differ")
        _identifier(concept["candidate_concept_id"], "concept identity")
        _text(concept["title"], "concept title", 256)
        _ids(concept["source_block_ids"], "concept source blocks", maximum=10000, nonempty=False)
        if concept["status"] != "proposed":
            raise KnowledgeContractError("concept status differs")
        if "candidate_claim_ids" in concept:
            _ids(concept["candidate_claim_ids"], "concept claims", maximum=10000, nonempty=False)
    mapping_ids: set[str] = set()
    for mapping in mappings:
        item = _object(mapping, {"claim_id", "block_ids"}, "claim mapping")
        claim_id = _identifier(item["claim_id"], "claim mapping identity")
        if claim_id in mapping_ids:
            raise KnowledgeContractError("claim mapping identities are duplicated")
        mapping_ids.add(claim_id)
        _ids(item["block_ids"], "claim mapping blocks", maximum=10000, nonempty=False)
    for link in links:
        item = _object(link, {"source_block_id", "label", "target", "status"}, "link")
        _identifier(item["source_block_id"], "link source block")
        _text(item["label"], "link label", 256, nonempty=False)
        _text(item["target"], "link target", 256, nonempty=False)
        if item["status"] != "proposed":
            raise KnowledgeContractError("link status differs")


def _cross_validate(claims: Sequence[Mapping[str, Any]], citations: Sequence[Mapping[str, Any]]) -> None:
    citation_index = {item["citation_id"]: item for item in citations}
    for claim in claims:
        citation_id = claim["citation_id"]
        citation = citation_index.get(citation_id)
        if citation is None:
            raise KnowledgeContractError("claim citation binding is invalid")
        if claim["source_block_ids"] != citation["source_block_ids"]:
            raise KnowledgeContractError("claim and citation anchors differ")


def claims_digest(claims: Any) -> str:
    value = validate_claims(claims)
    return canonical_digest({"domain": "ao.lore.canonical-claim-set.v0.1", "claims": value})


def citations_digest(citations: Any) -> str:
    value = validate_citations(citations)
    return canonical_digest({"domain": "ao.lore.canonical-citation-set.v0.1", "citations": value})


def semantic_key_v0_3(
    concepts: Any, claim_mappings: Any, links: Any, claims_digest_value: Any,
    citations_digest_value: Any, knowledge_policy: Any,
) -> str:
    _digest(claims_digest_value, "claims_digest")
    _digest(citations_digest_value, "citations_digest")
    policy = validate_knowledge_policy(knowledge_policy)
    try:
        return canonical_digest({
            "domain": "ao.lore.semantic-key.v0.3", "concepts": concepts,
            "claim_mappings": claim_mappings, "links": links,
            "claims_digest": claims_digest_value, "citations_digest": citations_digest_value,
            "knowledge_policy": policy,
        })
    except BenchmarkError as exc:
        raise KnowledgeContractError("semantic payload is not strict JSON") from exc


def origin_identity(origin: Mapping[str, Any]) -> dict[str, str]:
    """Return the closed identity tuple retained on origin-qualified readbacks."""

    return {
        "workspace_id": origin["workspace_id"],
        "evidence_kind": origin["evidence_kind"],
        "evidence_id": origin["evidence_id"],
        "evidence_digest": origin["evidence_digest"],
    }


def _validate_origin_identity(value: Any) -> dict[str, str]:
    item = _object(value, {"workspace_id", "evidence_kind", "evidence_id", "evidence_digest"}, "origin identity")
    _identifier(item["workspace_id"], "origin workspace identity")
    if item["evidence_kind"] not in {"document_block", "graph_claim", "graph_edge"}:
        raise KnowledgeContractError("origin evidence kind is invalid")
    evidence_id = item["evidence_id"]
    if type(evidence_id) is not str or not (_ID.fullmatch(evidence_id) or _DIGEST.fullmatch(evidence_id)):
        raise KnowledgeContractError("origin evidence identity is invalid")
    _digest(item["evidence_digest"], "origin evidence digest")
    return copy.deepcopy(dict(item))


def _origin_anchor(origin: Mapping[str, Any]) -> str:
    return origin[{"document_block": "block_id", "graph_claim": "claim_id", "graph_edge": "edge_id"}[origin["evidence_kind"]]]


def origin_for_anchors(origins: Sequence[Mapping[str, Any]], anchors: Sequence[str]) -> dict[str, Any]:
    """Bind every claim/citation anchor to one and the same promoted origin."""

    matches = [origin for origin in origins if all(_origin_anchor(origin) == anchor for anchor in anchors)]
    if len(matches) != 1:
        raise KnowledgeContractError("claim or citation origin binding is ambiguous")
    return copy.deepcopy(dict(matches[0]))


def validate_knowledge_payload(schema_version: str, value: Any) -> dict[str, Any]:
    """Validate one new answerable payload while leaving legacy validation elsewhere."""

    if schema_version == "ao.lore.canonical-claim-set.v0.1":
        obj = _object(value, {"schema_version", "claims"}, "claim set")
        if obj["schema_version"] != schema_version:
            raise KnowledgeContractError("claim set version differs")
        return {"schema_version": schema_version, "claims": validate_claims(obj["claims"])}
    if schema_version == "ao.lore.canonical-citation-set.v0.1":
        obj = _object(value, {"schema_version", "citations"}, "citation set")
        if obj["schema_version"] != schema_version:
            raise KnowledgeContractError("citation set version differs")
        return {"schema_version": schema_version, "citations": validate_citations(obj["citations"])}
    if schema_version not in {
        "ao.lore.okf-candidate.v0.2",
        "ao.lore.okf-candidate.v0.3",
        "ao.lore.okf-canonical-entry.v0.3",
        "ao.lore.okf-canonical-entry.v0.4",
    }:
        raise KnowledgeContractError("unsupported knowledge payload version")
    candidate = schema_version in {
        "ao.lore.okf-candidate.v0.2",
        "ao.lore.okf-candidate.v0.3",
    }
    common = {
        "schema_version", "candidate_id", "concepts", "claim_mappings", "links",
        "claims_schema_version", "claims_digest", "claims", "citations_schema_version",
        "citations_digest", "citations", "knowledge_policy",
    }
    if schema_version == "ao.lore.okf-candidate.v0.2":
        common = common | {"document_ir_digest"}
    elif schema_version == "ao.lore.okf-candidate.v0.3":
        common = common | {"evidence_selection_digest", "evidence_origins"}
    elif schema_version == "ao.lore.okf-canonical-entry.v0.3":
        common = common | {"document_ir_digest"}
    else:
        common = common | {"evidence_selection_digest", "evidence_origins"}
    canonical_keys = {
        "policy_version", "canonical_entry_id", "semantic_key", "candidate_digest",
        "provenance_digest", "accepted_review_head_digest",
    }
    if schema_version == "ao.lore.okf-canonical-entry.v0.3":
        canonical_keys |= {"source_digest", "parser"}
    keys = common | ({"contradiction_warnings", "canonical", "promotion_authority"} if candidate else canonical_keys)
    obj = _object(value, keys, "knowledge payload")
    if obj["schema_version"] != schema_version:
        raise KnowledgeContractError("knowledge payload version differs")
    claims = validate_claims(obj["claims"])
    citations = validate_citations(obj["citations"])
    _identifier(obj["candidate_id"], "candidate identity")
    if schema_version == "ao.lore.okf-candidate.v0.2":
        _digest(obj["document_ir_digest"], "document IR digest")
    elif schema_version in {"ao.lore.okf-candidate.v0.3", "ao.lore.okf-canonical-entry.v0.4"}:
        _digest(obj["evidence_selection_digest"], "evidence selection digest")
        origins = obj["evidence_origins"]
        if type(origins) is not list or not 1 <= len(origins) <= 32:
            raise KnowledgeContractError("evidence origins are invalid")
        try:
            validated_origins = [validate_selection_evidence(item) for item in origins]
        except ContractError as exc:
            raise KnowledgeContractError("evidence origin is invalid") from exc
        identities = {
            (
                item["workspace_id"],
                item["evidence_kind"],
                item["evidence_id"],
                item["evidence_digest"],
            )
            for item in validated_origins
        }
        if len(identities) != len(validated_origins):
            raise KnowledgeContractError("evidence origins contain duplicates")
        if schema_version == "ao.lore.okf-canonical-entry.v0.4":
            try:
                expected_selection_digest = canonical_digest({
                    "domain": "ao.lore.evidence-selection.v0.1", "evidence": validated_origins,
                })
            except BenchmarkError as exc:
                raise KnowledgeContractError("evidence origins are not strict JSON") from exc
            if obj["evidence_selection_digest"] != expected_selection_digest:
                raise KnowledgeContractError("evidence selection binding differs")
    else:
        _digest(obj["document_ir_digest"], "document IR digest")
    _validate_semantic_arrays(obj)
    _cross_validate(claims, citations)
    if schema_version == "ao.lore.okf-canonical-entry.v0.4":
        for claim in claims:
            if len(claim["source_block_ids"]) != 1:
                raise KnowledgeContractError("v0.4 claim must bind exactly one origin anchor")
            origin_for_anchors(validated_origins, claim["source_block_ids"])
        for citation in citations:
            if len(citation["source_block_ids"]) != 1:
                raise KnowledgeContractError("v0.4 citation must bind exactly one origin anchor")
            origin_for_anchors(validated_origins, citation["source_block_ids"])
    mapping_index = {item["claim_id"]: item["block_ids"] for item in obj["claim_mappings"]}
    if any(mapping_index.get(item["claim_id"]) != item["source_block_ids"] for item in claims):
        raise KnowledgeContractError("claim mapping and answerable claim anchors differ")
    anchor_ids = {anchor for item in claims for anchor in item["source_block_ids"]}
    for link in obj["links"]:
        if link["status"] != "proposed":
            raise KnowledgeContractError("link status differs")
        if link["source_block_id"] not in anchor_ids or link["target"] not in anchor_ids:
            raise KnowledgeContractError("link anchors differ")
    if obj["claims_schema_version"] != "ao.lore.canonical-claim-set.v0.1" or claims_digest(claims) != obj["claims_digest"]:
        raise KnowledgeContractError("claims binding differs")
    if obj["citations_schema_version"] != "ao.lore.canonical-citation-set.v0.1" or citations_digest(citations) != obj["citations_digest"]:
        raise KnowledgeContractError("citations binding differs")
    if candidate and (obj["canonical"] is not False or obj["promotion_authority"] is not False):
        raise KnowledgeContractError("candidate claims authority")
    if candidate:
        warnings = obj["contradiction_warnings"]
        if type(warnings) is not list or len(warnings) > 5000 or any(
            type(item) is not str or not 1 <= len(item) <= 1024 for item in warnings
        ):
            raise KnowledgeContractError("contradiction warnings are invalid")
    else:
        if obj["policy_version"] != "ao.lore.promotion-policy.v0.1":
            raise KnowledgeContractError("promotion policy version differs")
        _identifier(obj["canonical_entry_id"], "canonical entry identity")
        for field in ("semantic_key", "candidate_digest", "provenance_digest", "accepted_review_head_digest"):
            _digest(obj[field], field)
        if schema_version == "ao.lore.okf-canonical-entry.v0.3":
            _digest(obj["source_digest"], "source_digest")
            parser = _object(obj["parser"], {"parser_id", "parser_version"}, "parser")
            _identifier(parser["parser_id"], "parser identity")
            _text(parser["parser_version"], "parser version", 256)
        expected_semantic = semantic_key_v0_3(obj["concepts"], obj["claim_mappings"], obj["links"], obj["claims_digest"], obj["citations_digest"], obj["knowledge_policy"])
        if obj["semantic_key"] != expected_semantic:
            raise KnowledgeContractError("canonical semantic key differs")
    result = copy.deepcopy(obj)
    result["knowledge_policy"] = validate_knowledge_policy(obj["knowledge_policy"])
    try:
        canonical_digest(result)
    except BenchmarkError as exc:
        raise KnowledgeContractError("knowledge payload is not strict JSON") from exc
    return result


def validate_search_readback(value: Any) -> dict[str, Any]:
    """Validate the closed search projection without loading a schema engine."""

    version = value.get("schema_version") if type(value) is dict else None
    expected = {
        "schema_version", "status", "snapshot_digest", "query_digest", "hits",
        "legacy_v0_2_entry_count", "answerable_v0_3_entry_count",
    }
    if version == "ao.lore.knowledge-search-readback.v0.2":
        expected.add("answerable_v0_4_entry_count")
    obj = _object(value, expected, "knowledge search readback")
    if version not in {"ao.lore.knowledge-search-readback.v0.1", "ao.lore.knowledge-search-readback.v0.2"} or obj["status"] != "completed":
        raise KnowledgeContractError("knowledge search readback version differs")
    _digest(obj["snapshot_digest"], "snapshot digest")
    _digest(obj["query_digest"], "query digest")
    count_fields = ["legacy_v0_2_entry_count", "answerable_v0_3_entry_count"]
    if version.endswith("v0.2"):
        count_fields.append("answerable_v0_4_entry_count")
    for field in count_fields:
        count = obj[field]
        if type(count) is not int or not 0 <= count <= 10_000:
            raise KnowledgeContractError(f"{field} is invalid")
    if version.endswith("v0.2") and obj["answerable_v0_4_entry_count"] < 1:
        raise KnowledgeContractError("v0.4 answerable entry count is invalid")
    hits = obj["hits"]
    if type(hits) is not list or len(hits) > 200:
        raise KnowledgeContractError("knowledge search hits are invalid")
    for hit in hits:
        if type(hit) is not dict:
            raise KnowledgeContractError("knowledge search hit is invalid")
        classification = hit.get("classification")
        if classification == "answerable":
            answerable_keys = {
                "classification", "evidence_id", "canonical_entry_id", "claim_id", "citation_id",
                "claim_text", "citation", "source_ref", "score_components",
            }
            if "origin_identity" in hit:
                if version != "ao.lore.knowledge-search-readback.v0.2":
                    raise KnowledgeContractError("origin-qualified hit version differs")
                answerable_keys.add("origin_identity")
            item = _object(hit, answerable_keys, "answerable search hit")
            if type(item["evidence_id"]) is not str or re.fullmatch(r"evidence-[0-9a-f]{32}", item["evidence_id"]) is None:
                raise KnowledgeContractError("evidence identity is invalid")
            entry_id = _identifier(item["canonical_entry_id"], "canonical entry identity")
            _identifier(item["claim_id"], "claim identity")
            citation_id = _identifier(item["citation_id"], "citation identity")
            _text(item["claim_text"], "claim text", 1024)
            _text(item["citation"], "citation", 512)
            if item["source_ref"] != f"canonical:{entry_id}#{citation_id}" or len(item["source_ref"]) > 512:
                raise KnowledgeContractError("canonical source reference is invalid")
            if "origin_identity" in item:
                _validate_origin_identity(item["origin_identity"])
            score = _object(
                item["score_components"],
                {"claim_exact_match", "claim_substring_match", "matched_claim_token_count", "matched_citation_token_count"},
                "search score",
            )
            for field in ("claim_exact_match", "claim_substring_match"):
                if type(score[field]) is not bool:
                    raise KnowledgeContractError("search match flag is invalid")
            for field in ("matched_claim_token_count", "matched_citation_token_count"):
                if type(score[field]) is not int or not 0 <= score[field] <= 256:
                    raise KnowledgeContractError("search match count is invalid")
        elif classification == "metadata_only":
            item = _object(
                hit,
                {"classification", "canonical_entry_id", "concept_id", "concept_title"},
                "metadata search hit",
            )
            _identifier(item["canonical_entry_id"], "canonical entry identity")
            _identifier(item["concept_id"], "concept identity")
            _text(item["concept_title"], "concept title", 256)
        else:
            raise KnowledgeContractError("knowledge search classification is invalid")
    try:
        canonical_digest(obj)
    except BenchmarkError as exc:
        raise KnowledgeContractError("knowledge search readback is not strict JSON") from exc
    return copy.deepcopy(dict(obj))


def validate_snapshot_readback(value: Any) -> dict[str, Any]:
    """Validate the exact legacy or origin-aware snapshot projection."""

    version = value.get("snapshot_schema_version") if type(value) is dict else None
    keys = {
        "snapshot_schema_version", "latest_generation_id", "latest_generation_manifest_digest",
        "snapshot_digest", "effective_entries", "legacy_v0_2_entry_count",
        "answerable_v0_3_entry_count",
    }
    if version == "ao.lore.knowledge-snapshot.v0.2":
        keys.add("answerable_v0_4_entry_count")
    obj = _object(value, keys, "knowledge snapshot")
    if version not in {"ao.lore.knowledge-snapshot.v0.1", "ao.lore.knowledge-snapshot.v0.2"}:
        raise KnowledgeContractError("knowledge snapshot version differs")
    _digest(obj["snapshot_digest"], "snapshot digest")
    latest_id, latest_digest = obj["latest_generation_id"], obj["latest_generation_manifest_digest"]
    if (latest_id is None) != (latest_digest is None):
        raise KnowledgeContractError("snapshot latest generation binding differs")
    if latest_id is not None:
        _identifier(latest_id, "snapshot generation identity"); _digest(latest_digest, "snapshot generation digest")
    entries = obj["effective_entries"]
    if type(entries) is not list or len(entries) > 10_000:
        raise KnowledgeContractError("snapshot entries are invalid")
    counts = {"ao.lore.okf-canonical-entry.v0.2": 0, "ao.lore.okf-canonical-entry.v0.3": 0, "ao.lore.okf-canonical-entry.v0.4": 0}
    entry_ids: set[str] = set()
    for raw in entries:
        entry_version = raw.get("entry_schema_version") if type(raw) is dict else None
        entry_keys = {"canonical_entry_id", "canonical_entry_digest", "entry_schema_version", "answerability", "claim_count"}
        if entry_version == "ao.lore.okf-canonical-entry.v0.4":
            entry_keys.add("evidence_origin_identities")
        item = _object(raw, entry_keys, "snapshot entry")
        entry_id = _identifier(item["canonical_entry_id"], "snapshot entry identity")
        if entry_id in entry_ids:
            raise KnowledgeContractError("snapshot entry identities are duplicated")
        entry_ids.add(entry_id); _digest(item["canonical_entry_digest"], "snapshot entry digest")
        if entry_version not in counts or (entry_version == "ao.lore.okf-canonical-entry.v0.4" and version.endswith("v0.1")):
            raise KnowledgeContractError("snapshot entry version differs")
        expected_answerability = "metadata_only" if entry_version.endswith("v0.2") else "answerable"
        if item["answerability"] != expected_answerability:
            raise KnowledgeContractError("snapshot entry answerability differs")
        if type(item["claim_count"]) is not int or not 0 <= item["claim_count"] <= 10_000:
            raise KnowledgeContractError("snapshot claim count is invalid")
        if entry_version.endswith("v0.4"):
            origins = item["evidence_origin_identities"]
            if type(origins) is not list or not 1 <= len(origins) <= 32:
                raise KnowledgeContractError("snapshot origin identities are invalid")
            checked = [_validate_origin_identity(origin) for origin in origins]
            identities = {
                (origin["workspace_id"], origin["evidence_kind"], origin["evidence_id"], origin["evidence_digest"])
                for origin in checked
            }
            if len(identities) != len(checked):
                raise KnowledgeContractError("snapshot origin identities are duplicated")
        counts[entry_version] += 1
    for field in ("legacy_v0_2_entry_count", "answerable_v0_3_entry_count"):
        if type(obj[field]) is not int or not 0 <= obj[field] <= 10_000:
            raise KnowledgeContractError(f"{field} is invalid")
    if obj["legacy_v0_2_entry_count"] != counts["ao.lore.okf-canonical-entry.v0.2"] or obj["answerable_v0_3_entry_count"] != counts["ao.lore.okf-canonical-entry.v0.3"]:
        raise KnowledgeContractError("snapshot entry counts differ")
    if version.endswith("v0.2"):
        if type(obj["answerable_v0_4_entry_count"]) is not int or obj["answerable_v0_4_entry_count"] != counts["ao.lore.okf-canonical-entry.v0.4"] or obj["answerable_v0_4_entry_count"] < 1:
            raise KnowledgeContractError("snapshot v0.4 entry count differs")
    try:
        canonical_digest(obj)
    except BenchmarkError as exc:
        raise KnowledgeContractError("knowledge snapshot is not strict JSON") from exc
    return copy.deepcopy(dict(obj))


def validate_evidence_projection(value: Any) -> dict[str, Any]:
    """Validate one detached canonical evidence item with schema parity."""

    version = value.get("schema_version") if type(value) is dict else None
    keys = {
        "schema_version", "evidence_id", "requirement_ids", "supported_claims",
        "source_ref", "source_digest", "citation", "provenance",
        "freshness_met", "trust_met", "verified",
    }
    if version == "ao.lore.knowledge-evidence-projection.v0.2":
        keys.remove("source_digest"); keys.add("origin")
    obj = _object(value, keys, "knowledge evidence projection")
    if version not in {"ao.lore.knowledge-evidence-projection.v0.1", "ao.lore.knowledge-evidence-projection.v0.2"}:
        raise KnowledgeContractError("knowledge evidence projection version differs")
    if type(obj["evidence_id"]) is not str or re.fullmatch(r"evidence-[0-9a-f]{32}", obj["evidence_id"]) is None:
        raise KnowledgeContractError("evidence identity is invalid")
    requirement_ids = _ids(obj["requirement_ids"], "evidence requirement identities", maximum=200)
    claims = obj["supported_claims"]
    if type(claims) is not list or not 1 <= len(claims) <= 200:
        raise KnowledgeContractError("supported claims are invalid")
    checked_claims: list[dict[str, str]] = []
    claim_ids: set[str] = set()
    for raw in claims:
        claim = _object(raw, {"claim_id", "text"}, "supported claim")
        claim_id = _identifier(claim["claim_id"], "supported claim identity")
        if claim_id in claim_ids:
            raise KnowledgeContractError("supported claim identities are duplicated")
        claim_ids.add(claim_id)
        checked_claims.append({"claim_id": claim_id, "text": _text(claim["text"], "supported claim text", 1024)})
    source_ref = _text(obj["source_ref"], "source reference", 512)
    source_match = re.fullmatch(
        r"canonical:([a-z0-9][a-z0-9._-]{0,127})#([a-z0-9][a-z0-9._-]{0,127})",
        source_ref,
    )
    if source_match is None:
        raise KnowledgeContractError("source reference is invalid")
    if version.endswith("v0.1"):
        _digest(obj["source_digest"], "source digest")
    else:
        try:
            validated_origin = validate_selection_evidence(obj["origin"])
        except ContractError as exc:
            raise KnowledgeContractError("evidence projection origin is invalid") from exc
    _text(obj["citation"], "citation", 512)
    provenance_keys = {
        "generation_id", "generation_manifest_digest", "canonical_entry_id",
        "canonical_entry_digest", "candidate_id", "provenance_digest", "parser_id",
        "parser_version", "entry_schema_version",
    }
    if version.endswith("v0.2"):
        provenance_keys -= {"parser_id", "parser_version"}; provenance_keys.add("evidence_selection_digest")
    provenance = _object(obj["provenance"], provenance_keys, "knowledge evidence provenance")
    identity_fields = ["generation_id", "canonical_entry_id", "candidate_id"]
    if version.endswith("v0.1"):
        identity_fields.append("parser_id")
    for field in identity_fields:
        _identifier(provenance[field], field)
    for field in ("generation_manifest_digest", "canonical_entry_digest", "provenance_digest"):
        _digest(provenance[field], field)
    if version.endswith("v0.1"):
        _text(provenance["parser_version"], "parser version", 128)
    else:
        _digest(provenance["evidence_selection_digest"], "evidence selection digest")
    expected_entry_version = "ao.lore.okf-canonical-entry.v0.3" if version.endswith("v0.1") else "ao.lore.okf-canonical-entry.v0.4"
    if provenance["entry_schema_version"] != expected_entry_version:
        raise KnowledgeContractError("evidence entry version differs")
    if provenance["canonical_entry_id"] != source_match.group(1):
        raise KnowledgeContractError("evidence source and provenance differ")
    for field in ("freshness_met", "trust_met"):
        if type(obj[field]) is not bool:
            raise KnowledgeContractError(f"{field} is invalid")
    if obj["verified"] is not True:
        raise KnowledgeContractError("evidence projection is not verified")
    result = copy.deepcopy(dict(obj))
    result["requirement_ids"] = requirement_ids
    result["supported_claims"] = checked_claims
    if version.endswith("v0.2"):
        result["origin"] = validated_origin
    try:
        canonical_digest(result)
    except BenchmarkError as exc:
        raise KnowledgeContractError("knowledge evidence projection is not strict JSON") from exc
    return result


def validate_status_readback(value: Any) -> dict[str, Any]:
    """Validate the fixed canonical compatibility/status projection."""

    version = value.get("schema_version") if type(value) is dict else None
    keys = {
        "schema_version", "status", "snapshot_digest", "latest_generation_id",
        "latest_generation_manifest_digest", "generation_count", "effective_entry_count",
        "answerable_v0_3_entry_count", "legacy_v0_2_entry_count", "claim_count",
        "answerability_status",
    }
    if version == "ao.lore.knowledge-status-readback.v0.2":
        keys.add("answerable_v0_4_entry_count")
    obj = _object(value, keys, "knowledge status readback")
    if version not in {"ao.lore.knowledge-status-readback.v0.1", "ao.lore.knowledge-status-readback.v0.2"} or obj["status"] != "completed":
        raise KnowledgeContractError("knowledge status readback version differs")
    _digest(obj["snapshot_digest"], "snapshot digest")
    latest_id = obj["latest_generation_id"]
    latest_digest = obj["latest_generation_manifest_digest"]
    if (latest_id is None) != (latest_digest is None):
        raise KnowledgeContractError("latest generation binding differs")
    if latest_id is not None:
        _identifier(latest_id, "latest generation identity")
        _digest(latest_digest, "latest generation digest")
    count_fields = [
        "generation_count", "effective_entry_count", "answerable_v0_3_entry_count",
        "legacy_v0_2_entry_count",
    ]
    if version.endswith("v0.2"):
        count_fields.append("answerable_v0_4_entry_count")
    for field in count_fields:
        if type(obj[field]) is not int or not 0 <= obj[field] <= 10_000:
            raise KnowledgeContractError(f"{field} is invalid")
    if version.endswith("v0.2") and obj["answerable_v0_4_entry_count"] < 1:
        raise KnowledgeContractError("v0.4 answerable entry count is invalid")
    if type(obj["claim_count"]) is not int or not 0 <= obj["claim_count"] <= 50_000_000:
        raise KnowledgeContractError("claim_count is invalid")
    answerable = obj["answerable_v0_3_entry_count"] + obj.get("answerable_v0_4_entry_count", 0)
    expected_status = (
        "empty" if obj["effective_entry_count"] == 0 else
        "legacy_only" if answerable == 0 else
        "mixed_version_partial" if obj["legacy_v0_2_entry_count"] else
        "fully_answerable"
    )
    if obj["answerability_status"] != expected_status:
        raise KnowledgeContractError("knowledge answerability status differs")
    if obj["effective_entry_count"] != answerable + obj["legacy_v0_2_entry_count"]:
        raise KnowledgeContractError("knowledge status entry counts differ")
    return copy.deepcopy(dict(obj))


def validate_answer_readback(value: Any) -> dict[str, Any]:
    """Validate one closed evidence-bound answer outcome."""

    version = value.get("schema_version") if type(value) is dict else None
    keys = {
        "schema_version", "status", "snapshot_digest", "coverage_report_digest",
        "claim_ids", "evidence_ids", "citations", "legacy_v0_2_entry_count",
        "answer", "reason_code",
    }
    if version == "ao.lore.knowledge-answer-readback.v0.2":
        keys.add("answerable_v0_4_entry_count")
    obj = _object(value, keys, "knowledge answer readback")
    if version not in {"ao.lore.knowledge-answer-readback.v0.1", "ao.lore.knowledge-answer-readback.v0.2"}:
        raise KnowledgeContractError("knowledge answer readback version differs")
    if obj["status"] not in {"answer", "partial", "refuse", "investigate"}:
        raise KnowledgeContractError("knowledge answer status is invalid")
    _digest(obj["snapshot_digest"], "snapshot digest")
    _digest(obj["coverage_report_digest"], "coverage report digest")
    claim_ids = _ids(obj["claim_ids"], "answer claim identities", maximum=200, nonempty=False)
    evidence_ids = obj["evidence_ids"]
    if type(evidence_ids) is not list or len(evidence_ids) > 200 or len(evidence_ids) != len(set(evidence_ids)) or any(
        type(item) is not str or re.fullmatch(r"evidence-[0-9a-f]{32}", item) is None for item in evidence_ids
    ):
        raise KnowledgeContractError("answer evidence identities are invalid")
    citations = obj["citations"]
    if type(citations) is not list or len(citations) > 200:
        raise KnowledgeContractError("answer citations are invalid")
    citation_identities: set[tuple[str, ...]] = set()
    for item in citations:
        if type(item) is str:
            if len(item) > 512 or re.fullmatch(r"canonical:[a-z0-9][a-z0-9._-]{0,127}#[a-z0-9][a-z0-9._-]{0,127}", item) is None:
                raise KnowledgeContractError("answer citations are invalid")
            citation_identity = ("legacy", item)
        elif version.endswith("v0.2") and type(item) is dict:
            qualified = _object(item, {"source_ref", "origin_identity"}, "origin-qualified citation")
            if type(qualified["source_ref"]) is not str or len(qualified["source_ref"]) > 512 or re.fullmatch(r"canonical:[a-z0-9][a-z0-9._-]{0,127}#[a-z0-9][a-z0-9._-]{0,127}", qualified["source_ref"]) is None:
                raise KnowledgeContractError("answer citations are invalid")
            origin = _validate_origin_identity(qualified["origin_identity"])
            citation_identity = (
                "origin", qualified["source_ref"], origin["workspace_id"], origin["evidence_kind"],
                origin["evidence_id"], origin["evidence_digest"],
            )
        else:
            raise KnowledgeContractError("answer citations are invalid")
        citation_identities.add(citation_identity)
    if len(citation_identities) != len(citations):
        raise KnowledgeContractError("answer citations are invalid")
    if type(obj["legacy_v0_2_entry_count"]) is not int or not 0 <= obj["legacy_v0_2_entry_count"] <= 10_000:
        raise KnowledgeContractError("legacy entry count is invalid")
    if version.endswith("v0.2") and (type(obj["answerable_v0_4_entry_count"]) is not int or not 1 <= obj["answerable_v0_4_entry_count"] <= 10_000):
        raise KnowledgeContractError("v0.4 answerable entry count is invalid")
    answer = _text(obj["answer"], "knowledge answer", 32768, nonempty=False)
    if len(answer.encode("utf-8")) > 32768:
        raise KnowledgeContractError("knowledge answer byte budget exceeded")
    reasons = {
        "none", "no_answerable_entries", "legacy_metadata_only", "coverage_insufficient",
        "hard_contradiction", "freshness_gate_failed", "trust_gate_failed",
        "canonical_state_invalid", "unsupported_active_version", "snapshot_drift",
    }
    if obj["reason_code"] not in reasons:
        raise KnowledgeContractError("knowledge answer reason is invalid")
    if obj["status"] == "answer" and obj["reason_code"] != "none":
        raise KnowledgeContractError("successful answer reason differs")
    if obj["status"] in {"refuse", "investigate"} and (claim_ids or evidence_ids or citations or answer):
        raise KnowledgeContractError("non-answer outcome emitted claims")
    if obj["status"] in {"answer", "partial"} and (not claim_ids or not evidence_ids or not citations or not answer):
        raise KnowledgeContractError("answer outcome lacks evidence")
    return copy.deepcopy(dict(obj))
