"""Pure deterministic retrieval over one verified document generation."""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any

from ._strict_io import ContractError
from .benchmark import canonical_digest
from .document_evidence_contracts import validate_workspace_document_generation
from .workspace_contracts import AUTHORITY_FIELDS


class DocumentEvidenceQueryError(ValueError):
    """The supplied generation or query cannot be retrieved safely."""


_NON_ALNUM = re.compile(r"[^\w]+", flags=re.UNICODE)


def _contains_category_c(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("_", " ")
    return " ".join(_NON_ALNUM.sub(" ", normalized).split())


def _blocks(values: list[dict[str, Any]]):
    for block in values:
        yield block
        yield from _blocks(block.get("children", []))


def _context_windows(values: list[dict[str, Any]], radius: int):
    """Index bounded sibling windows; headings and parent changes stop expansion."""
    windows = {}
    for index, block in enumerate(values):
        neighbors = []
        if block["type"] != "heading":
            for direction in (1, -1):
                for distance in range(1, radius + 1):
                    position = index + direction * distance
                    if not 0 <= position < len(values):
                        break
                    neighbor = values[position]
                    if neighbor["type"] == "heading":
                        break
                    neighbors.append(neighbor)
        windows[block["id"]] = neighbors
        windows.update(_context_windows(block.get("children", []), radius))
    return windows


def query_workspace_documents(
    generation: dict[str, object],
    workspace_id: str,
    prompt: str,
    *,
    limit: int = 20,
    adjacent_blocks: int = 0,
    max_context_chars: int = 65536,
    context_limit: int = 128,
) -> dict[str, object]:
    """Return exact block evidence from one already-verified generation.

    ``limit`` selects ranked seeds. Opt-in ``adjacent_blocks`` expands by up to
    two siblings per direction, within heading/parent boundaries. Expanded
    output is capped by ``context_limit`` and ``max_context_chars`` (Unicode
    code points, not model tokens). Defaults preserve the original readback.
    """

    if (
        type(workspace_id) is not str
        or type(prompt) is not str
        or not 1 <= len(prompt) <= 1024
        or _contains_category_c(prompt)
        or type(limit) is not int
        or not 1 <= limit <= 128
        or type(adjacent_blocks) is not int
        or not 0 <= adjacent_blocks <= 2
        or type(max_context_chars) is not int
        or not 1 <= max_context_chars <= 1048576
        or type(context_limit) is not int
        or not 1 <= context_limit <= 128
    ):
        raise DocumentEvidenceQueryError("document query is invalid")
    try:
        current = validate_workspace_document_generation(generation)
    except (ContractError, TypeError, ValueError) as exc:
        raise DocumentEvidenceQueryError("document generation is invalid") from exc
    if current["workspace_id"] != workspace_id:
        raise DocumentEvidenceQueryError("document workspace binding differs")

    normalized_prompt = _normalize(prompt)
    if not normalized_prompt:
        raise DocumentEvidenceQueryError("document query is invalid")
    prompt_tokens = normalized_prompt.split()
    prompt_set = set(prompt_tokens)
    scored: list[tuple[tuple[int, int, int, str, str], dict[str, Any], dict[str, Any]]] = []
    for document in current["documents"]:
        for block in _blocks(document["document_ir"]["blocks"]):
            normalized_text = _normalize(block["text"])
            block_tokens = normalized_text.split()
            matched_prompt = len(prompt_set.intersection(block_tokens))
            if not matched_prompt:
                continue
            matched_block = sum(token in prompt_set for token in block_tokens)
            rank = (
                -int(normalized_prompt in normalized_text),
                -matched_prompt,
                -matched_block,
                document["document_id"],
                block["id"],
            )
            scored.append((rank, document, block))
    scored.sort(key=lambda item: item[0])

    selected = [(document, block) for _rank, document, block in scored[:limit]]
    context_limited = False
    expanded_mode = adjacent_blocks or max_context_chars != 65536 or context_limit != 128
    if expanded_mode:
        windows = {
            document["document_id"]: _context_windows(document["document_ir"]["blocks"], adjacent_blocks)
            for document in current["documents"]
        }
        selected = []
        seen = set()
        used_chars = 0
        # Preserve ranked hits first; use remaining capacity for local context.
        # Each added block retains its own provenance and verbatim text.
        seeds = [(document, block) for _rank, document, block in scored[:limit]]
        def admit(document, candidate):
            nonlocal used_chars, context_limited
            identity = (document["document_id"], candidate["id"])
            if identity in seen:
                return
            size = len(candidate["text"])
            if len(selected) >= context_limit or used_chars + size > max_context_chars:
                context_limited = True
                return
            selected.append((document, candidate))
            seen.add(identity)
            used_chars += size

        for document, block in seeds:
            admit(document, block)
        admitted_seeds = list(selected)
        for document, block in admitted_seeds:
            for candidate in windows[document["document_id"]][block["id"]]:
                admit(document, candidate)

    evidence = []
    for document, block in selected:
        block_digest = canonical_digest(block)
        text_digest = canonical_digest(block["text"])
        evidence_id = canonical_digest({
            "workspace_id": workspace_id,
            "generation_digest": current["generation_digest"],
            "document_id": document["document_id"],
            "document_ir_digest": document["document_ir_digest"],
            "block_id": block["id"],
            "block_digest": block_digest,
            "source_span": block["source_span"],
            "text_digest": text_digest,
        })
        evidence.append({
            "evidence_id": evidence_id,
            "workspace_id": workspace_id,
            "generation_digest": current["generation_digest"],
            "document_id": document["document_id"],
            "document_ir_digest": document["document_ir_digest"],
            "source_id": document["source_id"],
            "source_digest": document["source_digest"],
            "block_id": block["id"],
            "block_digest": block_digest,
            "render_text": block["text"],
            "source_span": copy.deepcopy(block["source_span"]),
            "authority_role": document["authority_role"],
            "sensitivity": document["sensitivity"],
            "freshness_status": document["freshness_status"],
            "qualification_codes": copy.deepcopy(document["qualification_codes"]),
        })

    qualifications = sorted({code for item in evidence for code in item["qualification_codes"]})
    # Packing must not hide a restriction or freshness gate on a ranked seed.
    gate_documents = [document for _rank, document, _block in scored[:limit]]
    if any(item["sensitivity"] == "restricted" for item in gate_documents):
        outcome, reason_code, evidence = "refuse", "restricted_evidence", []
        qualifications = ["Restricted evidence cannot be rendered."]
    elif any(item["freshness_status"] != "current" for item in gate_documents):
        outcome, reason_code = "investigate", "freshness_investigation_required"
        qualifications = ["Document freshness requires review.", *qualifications]
    elif evidence:
        outcome, reason_code = "answer", "ok"
    else:
        outcome, reason_code = "refuse", "no_evidence"

    if context_limited and reason_code != "restricted_evidence":
        if outcome == "answer":
            outcome = "partial"
        qualifications = ["Context expansion was limited; evidence may be incomplete.", *qualifications]

    query_binding = {"prompt": prompt, "workspace_id": workspace_id, "generation_digest": current["generation_digest"], "limit": limit}
    if expanded_mode:
        query_binding.update({"adjacent_blocks": adjacent_blocks, "max_context_chars": max_context_chars, "context_limit": context_limit})

    result = {
        "schema_version": "ao.lore.workspace-document-query-readback.v0.1",
        "query_id": "query-" + canonical_digest(query_binding).split(":", 1)[1][:24],
        "prompt_digest": canonical_digest(prompt),
        "workspace_id": workspace_id,
        "generation_digest": current["generation_digest"],
        "outcome": outcome,
        "reason_code": reason_code,
        "evidence": evidence,
        "qualifications": qualifications[:32],
        "readback_digest": "sha256:" + "0" * 64,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    result["readback_digest"] = canonical_digest({key: value for key, value in result.items() if key != "readback_digest"})
    return copy.deepcopy(result)
