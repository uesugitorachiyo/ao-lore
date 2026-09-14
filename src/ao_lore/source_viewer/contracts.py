"""Closed pilot contracts; upstream evidence identities are retained, not rederived."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

MAX_SOURCE = 32 * 1024 * 1024
MAX_MANIFEST = 8 * 1024 * 1024
MAX_TEXT = 2 * 1024 * 1024
MAX_EXCERPT = 1024 * 1024
MAX_RECORDS = 128
ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z", re.ASCII)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z", re.ASCII)
LEVELS = {"public": 0, "internal": 1, "restricted": 2}
ROLES = {"primary_law", "official_interpretation", "local_enforcement_guidance",
         "technical_guidance", "operator_procedure", "case_evidence"}
FRESHNESS = {"current", "stale", "superseded", "unavailable", "unknown"}
EVIDENCE_FIELDS = {
    "evidence_id", "workspace_id", "generation_digest", "document_id",
    "document_ir_digest", "source_id", "source_digest", "block_id", "block_digest",
    "render_text", "source_span", "authority_role", "sensitivity",
    "freshness_status", "qualification_codes",
}


class ViewerError(Exception):
    """Only fixed, path-free codes may cross the public boundary."""
    def __init__(self, code: str = "operation_rejected", status: int = 403):
        super().__init__(code)
        self.code = code
        self.status = status


def reject(code: str = "operation_rejected", status: int = 403) -> None:
    raise ViewerError(code, status)


def require(condition: bool, code: str = "invalid_record") -> None:
    if not condition:
        reject(code)


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        reject("invalid_record")


def strict_json(data: bytes, limit: int = MAX_MANIFEST) -> Any:
    require(isinstance(data, bytes) and len(data) <= limit)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        reject("invalid_record")

    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        reject("invalid_record")


def closed(value: Any, required: set[str], optional: set[str] | None = None) -> None:
    require(isinstance(value, dict))
    keys = set(value)
    require(required <= keys and keys <= required | (optional or set()))


def text(value: Any, maximum: int, minimum: int = 0) -> bool:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    return "\x00" not in value


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(ID.fullmatch(value)) and value not in {".", ".."}


def valid_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(DIGEST.fullmatch(value))


def timestamp(value: Any) -> datetime:
    require(isinstance(value, str) and bool(re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)))
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        reject("invalid_record")


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate_evidence(value: Any) -> None:
    closed(value, EVIDENCE_FIELDS)
    for key in ("evidence_id", "generation_digest", "document_ir_digest",
                "source_digest", "block_digest"):
        require(valid_digest(value[key]))
    for key in ("workspace_id", "source_id"):
        require(valid_id(value[key]))
    require(valid_id(value["document_id"]) or valid_digest(value["document_id"]))
    require(text(value["block_id"], 256, 1))
    require(text(value["render_text"], MAX_EXCERPT))
    require(isinstance(value["authority_role"], str) and value["authority_role"] in ROLES)
    require(isinstance(value["sensitivity"], str) and value["sensitivity"] in LEVELS)
    require(isinstance(value["freshness_status"], str) and value["freshness_status"] in FRESHNESS)
    codes = value["qualification_codes"]
    require(isinstance(codes, list) and len(codes) <= 32)
    require(all(valid_id(code) for code in codes) and len(set(codes)) == len(codes))
    span = value["source_span"]
    closed(span, {"start", "end"}, {"page", "coordinates"})
    require(type(span["start"]) is int and type(span["end"]) is int)
    require(0 <= span["start"] <= span["end"] <= 2**53 - 1)
    if "page" in span:
        require(type(span["page"]) is int and 1 <= span["page"] <= 2**31 - 1)
    if "coordinates" in span:
        box = span["coordinates"]
        require(isinstance(box, list) and len(box) == 4)
        require(all(type(n) in (int, float) and math.isfinite(n) and abs(n) <= 1e12
                    for n in box))


def evidence_key(value: dict[str, Any]) -> tuple[str, str, str]:
    return value["workspace_id"], value["generation_digest"], value["evidence_id"]


def validate_key(value: Any) -> tuple[str, str, str]:
    closed(value, {"workspace_id", "generation_digest", "evidence_id"})
    require(valid_id(value["workspace_id"]) and valid_digest(value["generation_digest"])
            and valid_digest(value["evidence_id"]))
    return evidence_key(value)


def validate_manifest(value: Any) -> None:
    closed(value, {"schema_version", "primary_workspace_id", "reference_workspace_ids",
                   "provenance_mode", "records"})
    require(value["schema_version"] == "ao.lore.source-viewer-bindings.v0.1")
    require(value["provenance_mode"] == "operator-approved-snapshot")
    primary = value["primary_workspace_id"]
    refs = value["reference_workspace_ids"]
    require(valid_id(primary))
    require(isinstance(refs, list) and len(refs) <= 32 and all(valid_id(x) for x in refs))
    require(primary not in refs and len(set(refs)) == len(refs))
    records = value["records"]
    require(isinstance(records, list) and 1 <= len(records) <= MAX_RECORDS)
    keys: set[tuple[str, str, str]] = set()
    sources: dict[tuple[str, str], tuple[str, str]] = {}
    for record in records:
        closed(record, {"evidence", "format", "original_sensitivity", "pdf_page_basis"})
        validate_evidence(record["evidence"])
        evidence = record["evidence"]
        require(evidence["workspace_id"] in {primary, *refs}, "workspace_denied")
        require(isinstance(record["format"], str) and record["format"] in {"pdf", "text", "docx"})
        require(isinstance(record["original_sensitivity"], str) and record["original_sensitivity"] in {"public", "internal"}, "sensitivity_denied")
        require(LEVELS[evidence["sensitivity"]] <= LEVELS[record["original_sensitivity"]],
                "sensitivity_denied")
        require(record["pdf_page_basis"] == (
            "physical-one-based" if record["format"] == "pdf" else "not-applicable"))
        key = evidence_key(evidence)
        require(key not in keys, "identity_collision")
        keys.add(key)
        source = evidence["workspace_id"], evidence["source_digest"]
        classification = record["format"], record["original_sensitivity"]
        require(source not in sources or sources[source] == classification, "binding_drift")
        sources[source] = classification


def validate_grant(value: Any) -> None:
    closed(value, {"schema_version", "grant_id", "primary_workspace_id", "binding_digest",
                   "created_at", "expires_at", "max_sensitivity", "authorized_scope",
                   "allow_original_download"})
    require(value["schema_version"] == "ao.lore.source-viewer-grant.v0.1")
    require(valid_id(value["grant_id"]) and valid_id(value["primary_workspace_id"]))
    require(valid_digest(value["binding_digest"]))
    require(isinstance(value["max_sensitivity"], str) and value["max_sensitivity"] in {"public", "internal"})
    require(value["authorized_scope"] == "whole-source")
    require(type(value["allow_original_download"]) is bool)
    created, expires = timestamp(value["created_at"]), timestamp(value["expires_at"])
    require(0 < (expires - created).total_seconds() <= 86400)


def validate_resolve(value: Any) -> None:
    closed(value, {"schema_version", "provenance_mode", "primary_workspace_id", "open_id",
                   "expires_in_seconds", "evidence", "original_sensitivity", "original_bytes_verified",
                   "native_binding_revalidated", "original_download_allowed", "display"})
    modes = {
        "ao.lore.source-viewer-resolve.v0.1": ("operator-approved-snapshot", False),
        "ao.lore.source-viewer-resolve.v0.2": ("native-retained-source", True),
    }
    require(value["schema_version"] in modes)
    mode, native = modes[value["schema_version"]]
    require(value["provenance_mode"] == mode)
    require(valid_id(value["primary_workspace_id"]))
    require(isinstance(value["open_id"], str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", value["open_id"])))
    require(type(value["expires_in_seconds"]) is int and 0 <= value["expires_in_seconds"] <= 120)
    validate_evidence(value["evidence"])
    require(isinstance(value["original_sensitivity"], str) and value["original_sensitivity"] in {"public", "internal"})
    require(value["original_bytes_verified"] is True
            and value["native_binding_revalidated"] is native)
    require(type(value["original_download_allowed"]) is bool)
    display = value["display"]
    closed(display, {"format", "cited_page", "highlight"},
           {"page", "page_count", "width", "height", "render_status", "display_label"})
    require(isinstance(display["format"], str) and display["format"] in {"pdf", "text", "docx"})
    require(display["cited_page"] is None or (type(display["cited_page"]) is int and 1 <= display["cited_page"] <= 2000))
    for field in ("page", "page_count", "width", "height"):
        if field in display and display[field] is not None:
            require(type(display[field]) is int and 1 <= display[field] <= 20000)
    if "display_label" in display:
        require(display["display_label"] in {"Original UTF-8 text; literal display",
                "DOCX main-body text projection; not Word pagination or layout"})
    if "render_status" in display:
        require(display["render_status"] in {"available", "pdf_renderer_unavailable"})
    highlight = display["highlight"]
    closed(highlight, {"status", "boxes"}, {"basis"})
    statuses = {"exact_unique_text", "exact_verified_span", "empty_excerpt", "exact_match_unavailable",
                "ambiguous_exact_match", "no_cited_excerpt_on_this_page", "highlight_excerpt_limit",
                "highlight_text_limit", "highlight_search_limit", "highlight_geometry_limit",
                "highlight_outside_page", "highlight_geometry_unavailable", "not_verified"}
    require(isinstance(highlight["status"], str) and highlight["status"] in statuses)
    if "basis" in highlight:
        require(highlight["basis"] == "renderer-exact-text")
    require(isinstance(highlight["boxes"], list) and len(highlight["boxes"]) <= 256)
    for box in highlight["boxes"]:
        require(isinstance(box, list) and len(box) == 4)
        require(all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in box))
        require(box[0] + box[2] <= 1.000001 and box[1] + box[3] <= 1.000001)
