"""Strict contracts for immutable workspace document evidence."""

from __future__ import annotations

import copy
import hmac
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from ._strict_io import ContractError
from .benchmark import BenchmarkError, canonical_digest
from .evidence_graph_contracts import AUTHORITY_ROLES
from .workspace_contracts import AUTHORITY_FIELDS


DOCUMENT_EVIDENCE_SCHEMA_VERSIONS = (
    "ao.lore.workspace-document-generation.v0.1",
    "ao.lore.workspace-document-ingest-readback.v0.1",
    "ao.lore.workspace-document-query-readback.v0.1",
)
SENSITIVITIES = ("public", "internal", "restricted")
FRESHNESS_STATUSES = ("current", "stale", "superseded", "unavailable", "unknown")

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOCUMENT_ID = re.compile(r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._-]{0,127})$")
_MEDIA = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}$")
_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_SOURCE_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_MAX_IR_BYTES = 16 * 1024 * 1024
_MAX_GENERATION_BYTES = 256 * 1024 * 1024
_MAX_BLOCKS = 50_000
_MAX_BLOCK_TEXT = 1024 * 1024


def _object(value: Any, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys) or any(type(key) is not str for key in value):
        raise ContractError(f"{label} keys differ")
    return value


def _id(value: Any, label: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _document_id(value: Any, label: str) -> str:
    if type(value) is not str or _DOCUMENT_ID.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _text(value: Any, label: str, maximum: int, *, allow_empty: bool = False, public: bool = False) -> str:
    minimum = 0 if allow_empty else 1
    if type(value) is not str or not minimum <= len(value) <= maximum or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ContractError(f"{label} is invalid")
    lowered = value.casefold()
    if public and (lowered.startswith(("/", "file:")) or "/home/" in lowered or "/opt/" in lowered or "/tmp/" in lowered or "\\" in value):
        raise ContractError(f"{label} is unsafe")
    return value


def _public_source_resource(value: Any, label: str, maximum: int) -> str:
    result = _text(value, label, maximum)
    if "\\" in result or result.startswith("/"):
        raise ContractError(f"{label} is unsafe")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9+.-]*:.*", result) is not None:
        raise ContractError(f"{label} is unsafe")
    segments = result.split("/")
    if (
        not segments
        or len(segments) > 2
        or any(segment in {"", ".", ".."} for segment in segments)
        or any(_SOURCE_SEGMENT.fullmatch(segment) is None for segment in segments)
        or segments[0] == "private"
        or segments[0].startswith("private-")
    ):
        raise ContractError(f"{label} is unsafe")
    return result


def _timestamp(value: Any, label: str) -> str:
    result = _text(value, label, 32)
    if not result.endswith("Z"):
        raise ContractError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(result[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{label} is invalid") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError(f"{label} is invalid")
    return result


def _date(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _DATE.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{label} is invalid") from exc
    return value


def _authority(body: dict[str, Any]) -> dict[str, bool]:
    for field in AUTHORITY_FIELDS:
        if body[field] is not False:
            raise ContractError(f"{field} must remain false")
    return {field: False for field in AUTHORITY_FIELDS}


def _self_digest(body: dict[str, Any], field: str, label: str) -> None:
    supplied = _digest(body[field], field)
    try:
        expected = canonical_digest({key: value for key, value in body.items() if key != field})
    except BenchmarkError as exc:
        raise ContractError(f"{label} is not strict JSON") from exc
    if not hmac.compare_digest(supplied, expected):
        raise ContractError(f"{label} digest differs")


def _strict_json_size(value: Any, maximum: int, label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} is not strict JSON") from exc
    if len(encoded) > maximum:
        raise ContractError(f"{label} exceeds its byte budget")


def _span(value: Any) -> dict[str, Any]:
    allowed = {"page", "start", "end", "coordinates"}
    if type(value) is not dict or not {"start", "end"}.issubset(value) or set(value) - allowed:
        raise ContractError("document block source span is invalid")
    start, end = value["start"], value["end"]
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        raise ContractError("document block source span is invalid")
    result: dict[str, Any] = {"start": start, "end": end}
    if "page" in value:
        if type(value["page"]) is not int or value["page"] < 1:
            raise ContractError("document block page is invalid")
        result["page"] = value["page"]
    if "coordinates" in value:
        coords = value["coordinates"]
        if type(coords) is not list or len(coords) != 4 or any(type(item) not in {int, float} or not math.isfinite(item) for item in coords):
            raise ContractError("document block coordinates are invalid")
        result["coordinates"] = copy.deepcopy(coords)
    return {key: result[key] for key in value}


def _block(value: Any, seen: set[str], count: list[int]) -> dict[str, Any]:
    allowed = {"id", "type", "text", "level", "source_span", "attributes", "children"}
    required = {"id", "type", "text", "source_span"}
    if type(value) is not dict or not required.issubset(value) or set(value) - allowed:
        raise ContractError("document block keys differ")
    block_id = _text(value["id"], "block id", 256)
    if block_id in seen:
        raise ContractError("document block identities contain duplicates")
    seen.add(block_id)
    count[0] += 1
    if count[0] > _MAX_BLOCKS:
        raise ContractError("document block budget exceeded")
    if value["type"] not in {"heading", "paragraph", "list", "table", "code", "image", "caption", "footnote", "link"}:
        raise ContractError("document block type is invalid")
    result: dict[str, Any] = {
        "id": block_id, "type": value["type"],
        "text": _text(value["text"], "block text", _MAX_BLOCK_TEXT, allow_empty=True),
        "source_span": _span(value["source_span"]),
    }
    if "level" in value:
        if type(value["level"]) is not int or value["level"] < 1:
            raise ContractError("document block level is invalid")
        result["level"] = value["level"]
    if "attributes" in value:
        if type(value["attributes"]) is not dict:
            raise ContractError("document block attributes are invalid")
        _strict_json_size(value["attributes"], _MAX_IR_BYTES, "document block attributes")
        result["attributes"] = copy.deepcopy(value["attributes"])
    if "children" in value:
        if type(value["children"]) is not list or len(value["children"]) > _MAX_BLOCKS:
            raise ContractError("document block children are invalid")
        result["children"] = [_block(item, seen, count) for item in value["children"]]
    return {key: result[key] for key in value}


def _document_ir(value: Any) -> dict[str, Any]:
    allowed = {"schema_version", "document_id", "source", "parser", "blocks", "metadata"}
    required = {"schema_version", "document_id", "source", "blocks", "metadata"}
    if type(value) is not dict or not required.issubset(value) or set(value) - allowed:
        raise ContractError("document IR keys differ")
    if value["schema_version"] != "ao.lore.document-ir.v0.1":
        raise ContractError("document IR version differs")
    source = _object(value["source"], ("resource", "digest", "media_type"), "document IR source")
    result: dict[str, Any] = {
        "schema_version": value["schema_version"],
        "document_id": _document_id(value["document_id"], "document_id"),
        "source": {
            "resource": _public_source_resource(source["resource"], "source resource", 2048),
            "digest": _digest(source["digest"], "source digest"),
            "media_type": source["media_type"],
        },
    }
    if type(source["media_type"]) is not str or _MEDIA.fullmatch(source["media_type"]) is None:
        raise ContractError("source media type is invalid")
    if "parser" in value:
        parser = _object(value["parser"], ("parser_id", "parser_version", "configuration_digest"), "document IR parser")
        result["parser"] = {
            "parser_id": _id(parser["parser_id"], "parser_id"),
            "parser_version": _text(parser["parser_version"], "parser_version", 128),
            "configuration_digest": _digest(parser["configuration_digest"], "parser configuration digest"),
        }
    if type(value["blocks"]) is not list:
        raise ContractError("document IR blocks are invalid")
    all_seen: set[str] = set(); total = [0]
    result["blocks"] = [_block(item, all_seen, total) for item in value["blocks"]]
    if type(value["metadata"]) is not dict:
        raise ContractError("document IR metadata is invalid")
    _strict_json_size(value["metadata"], _MAX_IR_BYTES, "document IR metadata")
    result["metadata"] = copy.deepcopy(value["metadata"])
    result = {key: result[key] for key in value}
    _strict_json_size(result, _MAX_IR_BYTES, "document IR")
    return result


def validate_workspace_document_generation(value: object) -> dict[str, object]:
    keys = ("schema_version", "generation_id", "sequence", "prior_generation_digest", "workspace_id",
            "document_store_id", "registry_digest", "created_at", "documents", "generation_digest")
    body = _object(value, keys, "workspace document generation")
    if body["schema_version"] != DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[0]:
        raise ContractError("workspace document generation version differs")
    sequence = body["sequence"]
    if type(sequence) is not int or not 1 <= sequence <= 2_147_483_647:
        raise ContractError("workspace document generation sequence is invalid")
    prior = None if body["prior_generation_digest"] is None else _digest(body["prior_generation_digest"], "prior_generation_digest")
    if (sequence == 1) != (prior is None):
        raise ContractError("workspace document generation lineage differs")
    raw_documents = body["documents"]
    if type(raw_documents) is not list or len(raw_documents) > 1024:
        raise ContractError("workspace documents are invalid")
    documents = []
    binding_keys = ("document_id", "document_ir_digest", "source_id", "source_digest", "source_record_digest",
                    "media_type", "authority_role", "sensitivity", "version", "effective_date",
                    "freshness_status", "qualification_codes", "document_ir")
    for raw in raw_documents:
        item = _object(raw, binding_keys, "workspace document binding")
        ir = _document_ir(item["document_ir"])
        qualifications = item["qualification_codes"]
        if type(qualifications) is not list or len(qualifications) > 32:
            raise ContractError("qualification codes are invalid")
        qualifications = [_id(code, "qualification code") for code in qualifications]
        if qualifications != sorted(set(qualifications)):
            raise ContractError("qualification codes are not ordered and unique")
        document_id = _document_id(item["document_id"], "document_id")
        if document_id != ir["document_id"] or item["document_ir_digest"] != canonical_digest(ir):
            raise ContractError("document IR binding differs")
        if item["source_digest"] != ir["source"]["digest"] or item["media_type"] != ir["source"]["media_type"]:
            raise ContractError("document source binding differs")
        if item["authority_role"] not in AUTHORITY_ROLES or item["sensitivity"] not in SENSITIVITIES or item["freshness_status"] not in FRESHNESS_STATUSES:
            raise ContractError("document evidence classification is invalid")
        documents.append({
            "document_id": document_id, "document_ir_digest": _digest(item["document_ir_digest"], "document_ir_digest"),
            "source_id": _id(item["source_id"], "source_id"), "source_digest": _digest(item["source_digest"], "source_digest"),
            "source_record_digest": _digest(item["source_record_digest"], "source_record_digest"),
            "media_type": item["media_type"], "authority_role": item["authority_role"], "sensitivity": item["sensitivity"],
            "version": _text(item["version"], "document version", 128, public=True),
            "effective_date": _date(item["effective_date"], "effective_date"), "freshness_status": item["freshness_status"],
            "qualification_codes": qualifications, "document_ir": ir,
        })
    ids = [item["document_id"] for item in documents]
    if ids != sorted(set(ids)):
        raise ContractError("workspace documents are not identity ordered and unique")
    result = {
        "schema_version": body["schema_version"], "generation_id": _id(body["generation_id"], "generation_id"),
        "sequence": sequence, "prior_generation_digest": prior, "workspace_id": _id(body["workspace_id"], "workspace_id"),
        "document_store_id": _id(body["document_store_id"], "document_store_id"),
        "registry_digest": _digest(body["registry_digest"], "registry_digest"),
        "created_at": _timestamp(body["created_at"], "created_at"), "documents": documents,
        "generation_digest": body["generation_digest"],
    }
    _strict_json_size(result, _MAX_GENERATION_BYTES, "workspace document generation")
    _self_digest(result, "generation_digest", "workspace document generation")
    return copy.deepcopy(result)


def validate_workspace_document_ingest_readback(value: object, *, generation: dict[str, object], workspace_id: str) -> dict[str, object]:
    keys = ("schema_version", "ingest_id", "workspace_id", "status", "document_id", "source_digest",
            "document_ir_digest", "generation_id", "generation_digest", "readback_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "workspace document ingest readback")
    if body["schema_version"] != DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[1] or body["status"] not in {"published", "unchanged"}:
        raise ContractError("workspace document ingest readback result differs")
    current = validate_workspace_document_generation(generation)
    expected_workspace = _id(workspace_id, "workspace_id")
    documents = {item["document_id"]: item for item in current["documents"]}
    document_id = _document_id(body["document_id"], "document_id")
    document = documents.get(document_id)
    if document is None or body["workspace_id"] != expected_workspace or current["workspace_id"] != expected_workspace:
        raise ContractError("workspace document ingest origin differs")
    if (body["generation_id"], body["generation_digest"], body["source_digest"], body["document_ir_digest"]) != (
            current["generation_id"], current["generation_digest"], document["source_digest"], document["document_ir_digest"]):
        raise ContractError("workspace document ingest binding differs")
    result = {
        "schema_version": body["schema_version"], "ingest_id": _id(body["ingest_id"], "ingest_id"),
        "workspace_id": expected_workspace, "status": body["status"], "document_id": document_id,
        "source_digest": _digest(body["source_digest"], "source_digest"),
        "document_ir_digest": _digest(body["document_ir_digest"], "document_ir_digest"),
        "generation_id": _id(body["generation_id"], "generation_id"),
        "generation_digest": _digest(body["generation_digest"], "generation_digest"),
        "readback_digest": body["readback_digest"], **_authority(body),
    }
    _self_digest(result, "readback_digest", "workspace document ingest readback")
    return copy.deepcopy(result)


def validate_workspace_document_query_readback(value: object, *, generation: dict[str, object], workspace_id: str) -> dict[str, object]:
    keys = ("schema_version", "query_id", "prompt_digest", "workspace_id", "generation_digest", "outcome",
            "reason_code", "evidence", "qualifications", "readback_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "workspace document query readback")
    if body["schema_version"] != DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[2] or body["outcome"] not in {"answer", "partial", "refuse", "investigate"}:
        raise ContractError("workspace document query outcome differs")
    if (body["outcome"] in {"answer", "partial"}) != (body["reason_code"] == "ok"):
        raise ContractError("workspace document query reason differs")
    current = validate_workspace_document_generation(generation)
    expected_workspace = _id(workspace_id, "workspace_id")
    if body["workspace_id"] != expected_workspace or current["workspace_id"] != expected_workspace or body["generation_digest"] != current["generation_digest"]:
        raise ContractError("workspace document query generation binding differs")
    documents = {item["document_id"]: item for item in current["documents"]}
    evidence = []
    evidence_keys = ("evidence_id", "workspace_id", "generation_digest", "document_id", "document_ir_digest",
                     "source_id", "source_digest", "block_id", "block_digest", "render_text", "source_span",
                     "authority_role", "sensitivity", "freshness_status", "qualification_codes")
    raw_evidence = body["evidence"]
    if type(raw_evidence) is not list or len(raw_evidence) > 128:
        raise ContractError("workspace document query evidence is invalid")
    for raw in raw_evidence:
        item = _object(raw, evidence_keys, "workspace document query evidence")
        document = documents.get(item["document_id"])
        if document is None:
            raise ContractError("workspace document query evidence document is unknown")
        blocks: dict[str, dict[str, Any]] = {}
        def collect(values: list[dict[str, Any]]) -> None:
            for block in values:
                blocks[block["id"]] = block
                collect(block.get("children", []))
        collect(document["document_ir"]["blocks"])
        block = blocks.get(item["block_id"])
        if block is None:
            raise ContractError("workspace document query block is unknown")
        expected = (
            expected_workspace, current["generation_digest"], document["document_ir_digest"], document["source_id"],
            document["source_digest"], canonical_digest(block), block["text"], block["source_span"], document["authority_role"],
            document["sensitivity"], document["freshness_status"], document["qualification_codes"],
        )
        actual = (item["workspace_id"], item["generation_digest"], item["document_ir_digest"], item["source_id"],
                  item["source_digest"], item["block_digest"], item["render_text"], item["source_span"], item["authority_role"],
                  item["sensitivity"], item["freshness_status"], item["qualification_codes"])
        if actual != expected:
            raise ContractError("workspace document query evidence binding differs")
        evidence.append({
            "evidence_id": _digest(item["evidence_id"], "evidence_id"),
            "workspace_id": expected_workspace,
            "generation_digest": current["generation_digest"],
            "document_id": document["document_id"],
            "document_ir_digest": document["document_ir_digest"],
            "source_id": document["source_id"],
            "source_digest": document["source_digest"],
            "block_id": item["block_id"],
            "block_digest": _digest(item["block_digest"], "block_digest"),
            "render_text": _text(item["render_text"], "render_text", _MAX_BLOCK_TEXT, allow_empty=True),
            "source_span": copy.deepcopy(block["source_span"]),
            "authority_role": document["authority_role"],
            "sensitivity": document["sensitivity"],
            "freshness_status": document["freshness_status"],
            "qualification_codes": copy.deepcopy(document["qualification_codes"]),
        })
    identities = [(item["document_id"], item["block_id"], item["evidence_id"]) for item in evidence]
    if len(identities) != len(set(identities)):
        raise ContractError("workspace document query evidence contains duplicates")
    qualifications = body["qualifications"]
    if type(qualifications) is not list or len(qualifications) > 32:
        raise ContractError("workspace document query qualifications are invalid")
    qualifications = [_text(item, "qualification", 512, public=True) for item in qualifications]
    if len(qualifications) != len(set(qualifications)):
        raise ContractError("workspace document query qualifications contain duplicates")
    result = {
        "schema_version": body["schema_version"], "query_id": _id(body["query_id"], "query_id"),
        "prompt_digest": _digest(body["prompt_digest"], "prompt_digest"), "workspace_id": expected_workspace,
        "generation_digest": current["generation_digest"], "outcome": body["outcome"],
        "reason_code": _id(body["reason_code"], "reason_code"), "evidence": evidence,
        "qualifications": qualifications, "readback_digest": body["readback_digest"], **_authority(body),
    }
    _self_digest(result, "readback_digest", "workspace document query readback")
    return copy.deepcopy(result)
