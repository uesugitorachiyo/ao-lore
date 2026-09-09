"""Deterministic, excerpt-bound semantic checks for evidence-graph claims.

This module validates projections from document IR.  It does not write candidate
state, review candidates, query canonical knowledge, or mutate the brain.
"""

from __future__ import annotations

import copy
import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from .benchmark import BenchmarkError, canonical_digest
from .evidence_graph_contracts import canonical_evidence_digest


class EvidenceSemanticError(ValueError):
    """A semantic review is malformed or is not bound to its source excerpt."""


_REVIEW_KEYS = {
    "claim_id", "document_ir_digest", "block_id", "source_span",
    "excerpt_digest", "rationale",
}
_IR_KEYS = {"schema_version", "document_id", "source", "parser", "blocks", "metadata"}
_PAGE_NUMBER = re.compile(r"^(?:page\s+)?\d+(?:\s+(?:of|/)\s*\d+)?$", re.IGNORECASE)


def _tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    parts: list[str] = []
    current: list[str] = []
    for character in normalized:
        if character.isalnum():
            current.append(character)
        elif current:
            token = "".join(current)
            if len(token) >= 3:
                parts.append(token)
            current = []
    if current:
        token = "".join(current)
        if len(token) >= 3:
            parts.append(token)
    return tuple(parts)


def _contains(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    return bool(needle) and any(haystack[index:index + len(needle)] == needle
                                for index in range(len(haystack) - len(needle) + 1))


def _topic_key(subject_terms: list[str]) -> str:
    normalized = sorted(" ".join(_tokens(term)) for term in subject_terms)
    return canonical_evidence_digest(normalized, "semantic-subject-terms")


def _rationale_key(rationale: str) -> str:
    return canonical_evidence_digest(" ".join(_tokens(rationale)), "semantic-rationale")


def _layout_artifact(block: Mapping[str, Any]) -> bool:
    text = block.get("text")
    block_type = block.get("type")
    if not isinstance(text, str) or not isinstance(block_type, str):
        return True
    if block_type.casefold() in {"header", "footer", "page_number", "page-number"}:
        return True
    normalized = unicodedata.normalize("NFKC", text).strip()
    if _PAGE_NUMBER.fullmatch(normalized) or "\ufffd" in normalized:
        return True
    if block_type.casefold() == "table":
        rows = [line for line in normalized.splitlines() if line.strip()]
        widths = [len(line.split("|")) for line in rows]
        if len(rows) < 2 or not widths or min(widths) < 2 or len(set(widths)) != 1:
            return True
    return False


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or isinstance(value, bool) or not value or len(value) > maximum:
        raise EvidenceSemanticError(f"{label} is invalid")
    if any(ord(character) < 32 for character in value):
        raise EvidenceSemanticError(f"{label} is invalid")
    return value


def _source_span(value: Any) -> dict[str, Any]:
    allowed = {"start", "end", "page", "coordinates"}
    if type(value) is not dict or not {"start", "end"}.issubset(value) or set(value) - allowed:
        raise EvidenceSemanticError("source span is invalid")
    start, end = value["start"], value["end"]
    if (type(start) is not int or type(end) is not int
            or not 0 <= start < end <= 2**63 - 1):
        raise EvidenceSemanticError("source span is invalid")
    if "page" in value and (type(value["page"]) is not int or not 1 <= value["page"] <= 1_000_000):
        raise EvidenceSemanticError("source span is invalid")
    if "coordinates" in value:
        coordinates = value["coordinates"]
        if (type(coordinates) is not list or len(coordinates) != 4
                or any(type(item) not in {int, float} or not math.isfinite(item)
                       or abs(item) > 1_000_000_000 for item in coordinates)):
            raise EvidenceSemanticError("source span is invalid")
    try:
        canonical_digest(value)
    except BenchmarkError as exc:
        raise EvidenceSemanticError("source span is invalid") from exc
    return copy.deepcopy(value)


def review_claim_semantics(claims: list[dict[str, Any]], reviews: list[dict[str, Any]],
                           document_irs: Mapping[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate semantic reviews and return deterministic, non-authoritative readbacks."""
    if (type(claims) is not list or type(reviews) is not list or not 1 <= len(claims) <= 512
            or len(reviews) != len(claims) or not isinstance(document_irs, Mapping)
            or not 1 <= len(document_irs) <= 16):
        raise EvidenceSemanticError("semantic review budget differs")
    claim_by_id = {item.get("claim_id"): item for item in claims if type(item) is dict}
    if len(claim_by_id) != len(claims):
        raise EvidenceSemanticError("claim identity differs")
    seen_claims: set[str] = set()
    rationale_topics: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    for supplied in reviews:
        if type(supplied) is not dict or set(supplied) != _REVIEW_KEYS:
            raise EvidenceSemanticError("review keys differ")
        claim_id = _bounded_text(supplied["claim_id"], "claim identity", 128)
        if claim_id in seen_claims or claim_id not in claim_by_id:
            raise EvidenceSemanticError("claim identity differs")
        seen_claims.add(claim_id)
        claim = claim_by_id[claim_id]
        source_id = claim.get("source_id")
        ir = document_irs.get(source_id)
        if type(ir) is not dict or set(ir) != _IR_KEYS:
            raise EvidenceSemanticError("document IR binding differs")
        try:
            if supplied["document_ir_digest"] != canonical_digest(ir):
                raise EvidenceSemanticError("document IR binding differs")
        except BenchmarkError as exc:
            raise EvidenceSemanticError("document IR binding differs") from exc
        source = ir.get("source")
        if (ir.get("schema_version") != "ao.lore.document-ir.v0.1" or type(source) is not dict
                or source.get("digest") != claim.get("source_digest")
                or ir.get("document_id") != claim.get("source_digest")):
            raise EvidenceSemanticError("source binding differs")
        blocks = ir.get("blocks")
        block_id = _bounded_text(supplied["block_id"], "block identity", 128)
        if type(blocks) is not list or len(blocks) > 65536:
            raise EvidenceSemanticError("document IR binding differs")
        matches = [block for block in blocks if type(block) is dict and block.get("id") == block_id]
        if len(matches) != 1:
            raise EvidenceSemanticError("excerpt binding differs")
        block = matches[0]
        span = _source_span(supplied["source_span"])
        block_span = _source_span(block.get("source_span"))
        if canonical_digest(block_span) != canonical_digest(span):
            raise EvidenceSemanticError("excerpt binding differs")
        excerpt = claim.get("excerpt")
        if (block.get("text") != excerpt or claim.get("citation_anchor") != block_id
                or supplied["excerpt_digest"] != claim.get("excerpt_digest")
                or canonical_evidence_digest(excerpt, "excerpt") != claim.get("excerpt_digest")):
            raise EvidenceSemanticError("excerpt binding differs")
        if _layout_artifact(block):
            raise EvidenceSemanticError("layout artifact")
        terms = claim.get("subject_terms")
        if type(terms) is not list or not 1 <= len(terms) <= 8:
            raise EvidenceSemanticError("subject terms differ")
        term_tokens = [_tokens(_bounded_text(term, "subject term", 64)) for term in terms]
        rationale = _bounded_text(supplied["rationale"], "rationale", 1024)
        excerpt_tokens, rationale_tokens = _tokens(excerpt), _tokens(rationale)
        if any(not _contains(excerpt_tokens, term) for term in term_tokens) or not any(
                _contains(rationale_tokens, term) for term in term_tokens):
            raise EvidenceSemanticError("topic mismatch")
        rationale_key, topic_key = _rationale_key(rationale), _topic_key(terms)
        prior_topic = rationale_topics.setdefault(rationale_key, topic_key)
        if prior_topic != topic_key:
            raise EvidenceSemanticError("copied rationale")
        declared = claim.get("semantic_reason_codes")
        if declared != ["topic_match", "citation_supported"]:
            raise EvidenceSemanticError("semantic reason binding differs")
        results.append({
            "claim_id": claim_id, "document_ir_digest": supplied["document_ir_digest"],
            "block_id": block_id, "source_span": span,
            "excerpt_digest": supplied["excerpt_digest"], "rationale_digest": rationale_key,
            "subject_terms_digest": topic_key,
            "reason_codes": ["topic_match", "citation_supported"],
        })
    if seen_claims != set(claim_by_id):
        raise EvidenceSemanticError("claim identity differs")
    return sorted(results, key=lambda item: item["claim_id"])
