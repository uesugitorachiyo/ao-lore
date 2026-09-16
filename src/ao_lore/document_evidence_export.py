"""Exact text-passage export for integrations; never a second retriever."""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from .document_evidence_contracts import (
    validate_workspace_document_generation,
    validate_workspace_document_query_readback,
)


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")


class DocumentEvidenceExportError(ValueError):
    pass


def export_document_passages(generation: dict[str, Any], readback: dict[str, Any],
                             workspace_id: str, source_records: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Export retrieved blocks only when exact original text bindings verify."""
    try:
        current = validate_workspace_document_generation(generation)
        result = validate_workspace_document_query_readback(readback, generation=current, workspace_id=workspace_id)
        if type(source_records) is not list or len(source_records) > 1024:
            raise ValueError
        native = {(item["source_id"], item["source_digest"][7:]) for item in current["documents"]}
        bindings = {}
        external = {}
        total = 0
        for record in source_records:
            if type(record) is not dict or set(record) != {"native_source_id", "source_id", "sha256", "text"}:
                raise ValueError
            nid, sid, sha, text = (record[key] for key in ("native_source_id", "source_id", "sha256", "text"))
            if not isinstance(nid, str) or not _ID.fullmatch(nid) or not isinstance(sid, str) or not _ID.fullmatch(sid):
                raise ValueError
            if not isinstance(sha, str) or not _HASH.fullmatch(sha) or not isinstance(text, str):
                raise ValueError
            raw = text.encode("utf-8")
            total += len(raw)
            if len(raw) > 16 * 1024 * 1024 or total > 256 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != sha:
                raise ValueError
            key = (nid, sha)
            if key not in native or key in bindings or (nid in external and external[nid] != sid):
                raise ValueError
            bindings[key] = record
            external[nid] = sid
        packets = []
        for item in result["evidence"]:
            if item["sensitivity"] == "restricted":
                raise ValueError
            record = bindings[(item["source_id"], item["source_digest"][7:])]
            span = item["source_span"]
            if type(span) is not dict or set(span) != {"start", "end"} or not all(type(span[key]) is int for key in span):
                raise ValueError
            start, end = span["start"], span["end"]
            if not 0 <= start < end <= len(record["text"]) or record["text"][start:end] != item["render_text"]:
                raise ValueError
            packets.append({"source_id": record["source_id"], "sha256": record["sha256"], "text": item["render_text"],
                            "char_start": start, "char_end": end,
                            "native": {"evidence_id": item["evidence_id"], "block_id": item["block_id"],
                                       "source_span": copy.deepcopy(span), "native_source_id": item["source_id"],
                                       "workspace_id": workspace_id, "generation_digest": item["generation_digest"],
                                       "document_ir_digest": item["document_ir_digest"],
                                       "freshness_status": item["freshness_status"], "authority_role": item["authority_role"],
                                       "native_outcome": result["outcome"], "native_reason_code": result["reason_code"],
                                       "qualifications": copy.deepcopy(result["qualifications"])}})
        return packets
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise DocumentEvidenceExportError("Native evidence export requires valid seals, unique identities and exact original text offsets.") from exc
