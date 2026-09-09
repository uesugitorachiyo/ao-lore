"""Canonical-IR-only knowledge candidate distillation.

The public entry point accepts a validated in-memory document IR object and
returns a non-canonical candidate envelope. It has no filesystem, parser, or
canonical mutation capability.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .benchmark import BenchmarkError, canonical_digest
from .knowledge_contracts import (
    KnowledgeContractError,
    citations_digest,
    claims_digest,
    validate_distillation_trace,
    validate_knowledge_payload,
    validate_knowledge_policy,
)


class DistillationError(ValueError):
    """Raised when IR or candidate output violates the stage boundary."""


@dataclass(frozen=True)
class DistillationProposal:
    concepts: Sequence[Mapping[str, Any]]
    claim_mappings: Sequence[Mapping[str, Any]]
    links: Sequence[Mapping[str, Any]]
    contradiction_warnings: Sequence[str]
    trace: Mapping[str, Any]
    claims: Sequence[Mapping[str, Any]] | None = None
    citations: Sequence[Mapping[str, Any]] | None = None
    knowledge_policy: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        supplied = (self.claims is not None, self.citations is not None, self.knowledge_policy is not None)
        if any(supplied) and not all(supplied):
            raise DistillationError("answerable proposal payload must be supplied completely")


class DistillerAdapter(Protocol):
    def distill(
        self, document_ir: Mapping[str, Any], candidate_context: Mapping[str, Any]
    ) -> DistillationProposal: ...


class ScriptedDistiller:
    """Deterministic adapter for offline contract and integration tests."""

    def __init__(
        self,
        handler: Callable[[Mapping[str, Any], Mapping[str, Any]], DistillationProposal],
    ) -> None:
        self._handler = handler

    def distill(
        self, document_ir: Mapping[str, Any], candidate_context: Mapping[str, Any]
    ) -> DistillationProposal:
        result = self._handler(copy.deepcopy(document_ir), copy.deepcopy(candidate_context))
        if not isinstance(result, DistillationProposal):
            raise DistillationError("distiller adapter must return DistillationProposal")
        return result


class DeterministicDistiller:
    """Small local baseline that maps headings and paragraphs to proposals."""

    def distill(
        self, document_ir: Mapping[str, Any], candidate_context: Mapping[str, Any]
    ) -> DistillationProposal:
        policy: dict[str, Any] | None = None
        if "knowledge_policy" in candidate_context:
            try:
                policy = validate_knowledge_policy(candidate_context["knowledge_policy"])
            except KnowledgeContractError as exc:
                raise DistillationError("deterministic answerability policy is invalid") from exc
        concepts: list[dict[str, Any]] = []
        mappings: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        citations: list[dict[str, Any]] = []
        links: list[dict[str, Any]] = []
        active_concept: str | None = None
        for block in document_ir["blocks"]:
            if block["type"] == "heading":
                active_concept = f"candidate-concept-{len(concepts) + 1}"
                concepts.append(
                    {
                        "candidate_concept_id": active_concept,
                        "title": block["text"].strip(),
                        "source_block_ids": [block["id"]],
                        "status": "proposed",
                    }
                )
            elif block["type"] in {"paragraph", "list", "table", "code", "footnote"} and block["text"].strip():
                claim_id = f"candidate-claim-{len(mappings) + 1}"
                mappings.append({"claim_id": claim_id, "block_ids": [block["id"]]})
                if active_concept is not None:
                    concepts[-1].setdefault("candidate_claim_ids", []).append(claim_id)
                if policy is not None and len(block["text"]) <= 512:
                    citation_id = f"candidate-citation-{len(citations) + 1}"
                    claims.append(
                        {
                            "claim_id": claim_id,
                            "text": block["text"],
                            "source_block_ids": [block["id"]],
                            "citation_id": citation_id,
                        }
                    )
                    citations.append(
                        {
                            "citation_id": citation_id,
                            "render_text": block["text"],
                            "source_block_ids": [block["id"]],
                        }
                    )
            elif block["type"] == "link":
                links.append(
                    {
                        "source_block_id": block["id"],
                        "label": block["text"],
                        "target": block.get("attributes", {}).get("target", ""),
                        "status": "proposed",
                    }
                )
        trace = {
                "adapter": "deterministic-distiller",
                "policy_version": "v0.1" if policy is None else "answerable-v0.2",
                "input_block_count": len(document_ir["blocks"]),
                "candidate_concept_count": len(concepts),
                "candidate_claim_count": len(mappings) if policy is None else len(claims),
                "private_reasoning_persisted": False,
            }
        if policy is not None:
            trace["candidate_citation_count"] = len(citations)
        return DistillationProposal(
            concepts=concepts,
            claim_mappings=mappings,
            links=links,
            contradiction_warnings=[],
            trace=trace,
            claims=None if policy is None else claims,
            citations=None if policy is None else citations,
            knowledge_policy=policy,
        )


def _validate_span(span: Any, block_id: str) -> None:
    if not isinstance(span, Mapping):
        raise DistillationError(f"block {block_id} lacks a source span")
    start = span.get("start")
    end = span.get("end")
    if isinstance(start, bool) or isinstance(end, bool) or not isinstance(start, int) or not isinstance(end, int):
        raise DistillationError(f"block {block_id} has a malformed source span")
    if start < 0 or end < start:
        raise DistillationError(f"block {block_id} has an invalid source span")


def _validate_document_ir(document_ir: Any) -> dict[str, str]:
    if not isinstance(document_ir, Mapping):
        raise DistillationError("distillation input must be a document IR object")
    allowed = {"schema_version", "document_id", "source", "parser", "blocks", "metadata"}
    required = {"schema_version", "document_id", "source", "blocks", "metadata"}
    if not required.issubset(document_ir) or set(document_ir) - allowed:
        raise DistillationError("document IR has missing or unknown top-level fields")
    if document_ir["schema_version"] != "ao.lore.document-ir.v0.1":
        raise DistillationError("unsupported document IR version")
    source = document_ir["source"]
    if not isinstance(source, Mapping) or set(source) != {"resource", "digest", "media_type"}:
        raise DistillationError("document IR source provenance is malformed")
    digest = source.get("digest")
    if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:"):
        raise DistillationError("document IR source digest is malformed")
    blocks = document_ir["blocks"]
    if not isinstance(blocks, list):
        raise DistillationError("document IR blocks must be an array")
    block_ids: dict[str, str] = {}
    for block in blocks:
        if not isinstance(block, Mapping):
            raise DistillationError("document IR block must be an object")
        block_id = block.get("id")
        if not isinstance(block_id, str) or not block_id or block_id in block_ids:
            raise DistillationError("document IR block identities must be unique strings")
        if not isinstance(block.get("text"), str) or not isinstance(block.get("type"), str):
            raise DistillationError(f"block {block_id} is malformed")
        _validate_span(block.get("source_span"), block_id)
        block_ids[block_id] = block["text"]
    if not isinstance(document_ir["metadata"], Mapping):
        raise DistillationError("document IR metadata must be an object")
    return block_ids


def _validated_proposal(proposal: DistillationProposal, block_ids: Mapping[str, str]) -> dict[str, Any]:
    concepts = [copy.deepcopy(dict(item)) for item in proposal.concepts]
    mappings = [copy.deepcopy(dict(item)) for item in proposal.claim_mappings]
    links = [copy.deepcopy(dict(item)) for item in proposal.links]
    warnings = list(proposal.contradiction_warnings)
    if any(not isinstance(item, Mapping) for item in proposal.concepts):
        raise DistillationError("candidate concepts must be objects")
    if any(not isinstance(item, Mapping) for item in proposal.links):
        raise DistillationError("candidate links must be objects")
    if any(not isinstance(item, str) for item in warnings):
        raise DistillationError("contradiction warnings must be strings")
    claim_ids: set[str] = set()
    for mapping in mappings:
        claim_id = mapping.get("claim_id")
        refs = mapping.get("block_ids")
        if not isinstance(claim_id, str) or not claim_id or claim_id in claim_ids:
            raise DistillationError("claim identities must be unique non-empty strings")
        if not isinstance(refs, list) or not refs or any(ref not in block_ids for ref in refs):
            raise DistillationError(f"claim {claim_id} references an unknown document IR block")
        claim_ids.add(claim_id)
    try:
        canonical_digest(proposal.trace)
        canonical_digest({"concepts": concepts, "claim_mappings": mappings, "links": links, "warnings": warnings})
    except BenchmarkError as exc:
        raise DistillationError("candidate output must be strict JSON data") from exc
    return {
        "concepts": concepts,
        "claim_mappings": mappings,
        "links": links,
        "contradiction_warnings": warnings,
    }


def _answerable_payload(proposal: DistillationProposal, blocks: Mapping[str, str]) -> dict[str, Any] | None:
    if proposal.claims is None:
        return None
    claims = [copy.deepcopy(dict(item)) for item in proposal.claims]
    citations = [copy.deepcopy(dict(item)) for item in proposal.citations or ()]
    for item, text_field in ((claim, "text") for claim in claims):
        refs = item.get("source_block_ids")
        if not isinstance(refs, list) or any(ref not in blocks for ref in refs):
            raise DistillationError("answerable claim references an unknown document IR block")
        if item.get(text_field) != "\n".join(blocks[ref] for ref in refs):
            raise DistillationError("answerable claim text differs from supplied document IR")
    for item in citations:
        refs = item.get("source_block_ids")
        if not isinstance(refs, list) or any(ref not in blocks for ref in refs):
            raise DistillationError("answerable citation references an unknown document IR block")
        if item.get("render_text") != "\n".join(blocks[ref] for ref in refs):
            raise DistillationError("answerable citation text differs from supplied document IR")
    try:
        payload = {
            "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
            "claims_digest": claims_digest(claims), "claims": claims,
            "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
            "citations_digest": citations_digest(citations), "citations": citations,
            "knowledge_policy": copy.deepcopy(dict(proposal.knowledge_policy or {})),
        }
    except (KnowledgeContractError, TypeError, ValueError) as exc:
        raise DistillationError("answerable proposal payload is invalid") from exc
    return payload


def distill_document_ir(
    document_ir: Mapping[str, Any],
    adapter: DistillerAdapter,
    candidate_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Distill one immutable canonical IR into a non-canonical envelope."""

    block_ids = _validate_document_ir(document_ir)
    context = {} if candidate_context is None else candidate_context
    if not isinstance(context, Mapping):
        raise DistillationError("candidate context must be an explicit object")
    immutable_ir = copy.deepcopy(dict(document_ir))
    proposal = adapter.distill(immutable_ir, copy.deepcopy(dict(context)))
    if not isinstance(proposal, DistillationProposal):
        raise DistillationError("distiller adapter returned an invalid proposal")
    content = _validated_proposal(proposal, block_ids)
    ir_digest = canonical_digest(document_ir)
    answerable = _answerable_payload(proposal, block_ids)
    candidate_seed = canonical_digest({
        "document_ir_digest": ir_digest,
        **content,
        **({} if answerable is None else answerable),
    })
    candidate_version = "ao.lore.okf-candidate.v0.2" if answerable is not None else "ao.lore.okf-candidate.v0.1"
    result_version = "ao.lore.distillation-result.v0.2" if answerable is not None else "ao.lore.distillation-result.v0.1"
    candidate = {
        "schema_version": candidate_version,
        "candidate_id": "candidate-" + candidate_seed[7:23],
        "document_ir_digest": ir_digest,
        **content,
        **({} if answerable is None else answerable),
        "canonical": False,
        "promotion_authority": False,
    }
    if answerable is not None:
        try:
            validate_knowledge_payload(candidate_version, candidate)
            validate_distillation_trace(proposal.trace, candidate)
        except KnowledgeContractError as exc:
            raise DistillationError("answerable candidate payload is invalid") from exc
    return {
        "schema_version": result_version,
        "candidate": candidate,
        "candidate_digest": canonical_digest(candidate),
        "distillation_trace": copy.deepcopy(dict(proposal.trace)),
    }
