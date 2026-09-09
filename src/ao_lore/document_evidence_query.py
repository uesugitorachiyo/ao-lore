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


def query_workspace_documents(
    generation: dict[str, object],
    workspace_id: str,
    prompt: str,
    *,
    limit: int = 20,
) -> dict[str, object]:
    """Return exact block evidence from one already-verified generation."""

    if (
        type(workspace_id) is not str
        or type(prompt) is not str
        or not 1 <= len(prompt) <= 1024
        or _contains_category_c(prompt)
        or type(limit) is not int
        or not 1 <= limit <= 128
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

    evidence = []
    for _rank, document, block in scored[:limit]:
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
    if any(item["sensitivity"] == "restricted" for item in evidence):
        outcome, reason_code, evidence = "refuse", "restricted_evidence", []
        qualifications = ["Restricted evidence cannot be rendered."]
    elif any(item["freshness_status"] != "current" for item in evidence):
        outcome, reason_code = "investigate", "freshness_investigation_required"
        qualifications = ["Document freshness requires review.", *qualifications]
    elif evidence:
        outcome, reason_code = "answer", "ok"
    else:
        outcome, reason_code = "refuse", "no_evidence"

    result = {
        "schema_version": "ao.lore.workspace-document-query-readback.v0.1",
        "query_id": "query-" + canonical_digest({"prompt": prompt, "workspace_id": workspace_id, "generation_digest": current["generation_digest"], "limit": limit}).split(":", 1)[1][:24],
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
