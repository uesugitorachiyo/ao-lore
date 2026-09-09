"""Strict contracts for governed evidence selection artifacts."""

from __future__ import annotations

import copy
import hmac
import json
import re
from datetime import datetime, timezone
from typing import Any

from ._strict_io import ContractError
from .benchmark import BenchmarkError, canonical_digest
from .evidence_graph_contracts import AUTHORITY_FIELDS, AUTHORITY_ROLES, EDGE_TYPES


SELECTION_SCHEMA_VERSIONS = (
    "ao.lore.evidence-selection-request.v0.1",
    "ao.lore.evidence-selection-proposal.v0.1",
    "ao.lore.evidence-selection-authorization.v0.1",
    "ao.lore.evidence-selection-consumption.v0.1",
    "ao.lore.evidence-selection-transaction.v0.1",
    "ao.lore.evidence-selection-inspection.v0.1",
    "ao.lore.evidence-selection-recovery.v0.1",
)

CANDIDATE_SCHEMA_VERSION = "ao.lore.okf-candidate.v0.3"
PROVENANCE_SCHEMA_VERSION = "ao.lore.candidate-provenance.v0.3"

SELECTION_AUTHORITY_FIELDS = ("candidate_creation",) + AUTHORITY_FIELDS
EVIDENCE_KINDS = ("document_block", "graph_claim", "graph_edge")
SENSITIVITIES = ("public", "internal", "restricted")
FRESHNESS_STATUSES = ("current", "stale", "superseded", "unavailable", "unknown")
CONFLICT_STATUSES = ("clear", "conflicted", "investigate")
SEMANTIC_REVIEW = ("verified",)

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_ID = re.compile(r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._-]{0,127})$")
_SOURCE_HEAD = re.compile(r"^[0-9a-f]{40}$")
_MAX_PROPOSAL_BYTES = 4 * 1024 * 1024


def _object(value: Any, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys) or any(type(key) is not str for key in value):
        raise ContractError(f"{label} keys differ")
    return value


def _identifier(value: Any, label: str) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _evidence_id(value: Any, label: str) -> str:
    if type(value) is not str or _EVIDENCE_ID.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _source_head(value: Any, label: str) -> str:
    if type(value) is not str or _SOURCE_HEAD.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    return value


def _text(value: Any, label: str, maximum: int, *, minimum: int = 1) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum or any(ord(char) < 32 for char in value):
        raise ContractError(f"{label} is invalid")
    return value


def _public_text(value: Any, label: str, maximum: int, *, minimum: int = 1) -> str:
    result = _text(value, label, maximum, minimum=minimum)
    lowered = result.casefold()
    if lowered.startswith(("/", "file:")) or "/home/" in lowered or "/opt/" in lowered or "\\" in result:
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


def _ordered_times(prepared_at: str, expires_at: str, label: str) -> tuple[str, str]:
    prepared = _timestamp(prepared_at, f"{label} prepared_at")
    expires = _timestamp(expires_at, f"{label} expires_at")
    if datetime.fromisoformat(expires[:-1] + "+00:00") <= datetime.fromisoformat(prepared[:-1] + "+00:00"):
        raise ContractError(f"{label} expires_at must be later than prepared_at")
    return prepared, expires


def _array(value: Any, label: str, minimum: int, maximum: int) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ContractError(f"{label} is invalid")
    return value


def _ids(value: Any, label: str, minimum: int, maximum: int) -> list[str]:
    result = [_evidence_id(item, label) for item in _array(value, label, minimum, maximum)]
    if len(result) != len(set(result)):
        raise ContractError(f"{label} contains duplicates")
    return result


def _qualifications(value: Any, label: str) -> list[str]:
    items = [_identifier(item, label) for item in _array(value, label, 0, 32)]
    if len(items) != len(set(items)):
        raise ContractError(f"{label} contains duplicates")
    return items


def _authority(body: dict[str, Any], *, candidate_creation: bool) -> dict[str, bool]:
    result = {}
    if body["candidate_creation"] is not candidate_creation:
        expected = "true" if candidate_creation else "false"
        raise ContractError(f"candidate_creation must remain {expected}")
    result["candidate_creation"] = candidate_creation
    for field in AUTHORITY_FIELDS:
        if body[field] is not False:
            raise ContractError(f"{field} must remain false")
        result[field] = False
    return result


def _self_digest(body: dict[str, Any], field: str, label: str) -> str:
    supplied = _digest(body[field], field)
    try:
        expected = canonical_digest({key: value for key, value in body.items() if key != field})
    except BenchmarkError as exc:
        raise ContractError(f"{label} is not strict JSON") from exc
    if not hmac.compare_digest(supplied, expected):
        raise ContractError(f"{label} digest differs")
    return supplied


def _strict_json_size(value: Any, maximum: int, label: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{label} is not strict JSON") from exc
    if len(encoded) > maximum:
        raise ContractError(f"{label} exceeds its byte budget")


def _selection_digest(evidence: list[dict[str, Any]]) -> str:
    try:
        return canonical_digest(
            {"domain": "ao.lore.evidence-selection.v0.1", "evidence": evidence}
        )
    except BenchmarkError as exc:
        raise ContractError("selection evidence is not strict JSON") from exc


def _write_set_digest(paths: list[str]) -> str:
    try:
        return canonical_digest(
            {"domain": "ao.lore.evidence-selection.write-set.v0.1", "paths": paths}
        )
    except BenchmarkError as exc:
        raise ContractError("selection write set is not strict JSON") from exc


def _source_span(value: Any) -> dict[str, Any]:
    item = _object(value, ("page", "start", "end"), "source span")
    page = item["page"]
    start = item["start"]
    end = item["end"]
    if type(page) is not int or page < 1:
        raise ContractError("source span page is invalid")
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        raise ContractError("source span offsets are invalid")
    return copy.deepcopy(item)


def _document_block(value: Any) -> dict[str, Any]:
    keys = (
        "workspace_id", "document_store_id", "generation_digest", "document_id",
        "document_ir_digest", "source_id", "source_digest", "evidence_kind",
        "evidence_id", "evidence_digest", "block_id", "render_text", "source_span",
        "authority_role", "sensitivity", "freshness_status", "qualification_codes",
    )
    item = _object(value, keys, "document block evidence")
    if item["evidence_kind"] != "document_block":
        raise ContractError("document block evidence kind differs")
    if item["authority_role"] not in AUTHORITY_ROLES:
        raise ContractError("document block authority role is invalid")
    if item["sensitivity"] not in SENSITIVITIES:
        raise ContractError("document block sensitivity is invalid")
    if item["freshness_status"] not in FRESHNESS_STATUSES:
        raise ContractError("document block freshness is invalid")
    return {
        "workspace_id": _identifier(item["workspace_id"], "document block workspace_id"),
        "document_store_id": _identifier(item["document_store_id"], "document block store"),
        "generation_digest": _digest(item["generation_digest"], "document block generation"),
        "document_id": _evidence_id(item["document_id"], "document block document_id"),
        "document_ir_digest": _digest(item["document_ir_digest"], "document block document_ir_digest"),
        "source_id": _identifier(item["source_id"], "document block source_id"),
        "source_digest": _digest(item["source_digest"], "document block source_digest"),
        "evidence_kind": item["evidence_kind"],
        "evidence_id": _evidence_id(item["evidence_id"], "document block evidence_id"),
        "evidence_digest": _digest(item["evidence_digest"], "document block evidence_digest"),
        "block_id": _identifier(item["block_id"], "document block block_id"),
        "render_text": _public_text(item["render_text"], "document block render_text", 1024),
        "source_span": _source_span(item["source_span"]),
        "authority_role": item["authority_role"],
        "sensitivity": item["sensitivity"],
        "freshness_status": item["freshness_status"],
        "qualification_codes": _qualifications(item["qualification_codes"], "document block qualification_codes"),
    }


def _graph_claim(value: Any) -> dict[str, Any]:
    keys = (
        "workspace_id", "graph_id", "graph_digest", "source_id", "source_digest",
        "evidence_kind", "evidence_id", "evidence_digest", "claim_id", "excerpt",
        "citation_anchor", "authority_role", "freshness_status", "qualification_codes",
        "conflict_status", "semantic_review",
    )
    item = _object(value, keys, "graph claim evidence")
    if item["evidence_kind"] != "graph_claim":
        raise ContractError("graph claim evidence kind differs")
    if item["authority_role"] not in AUTHORITY_ROLES:
        raise ContractError("graph claim authority role is invalid")
    if item["freshness_status"] not in FRESHNESS_STATUSES:
        raise ContractError("graph claim freshness is invalid")
    if item["conflict_status"] not in CONFLICT_STATUSES:
        raise ContractError("graph claim conflict status is invalid")
    if item["semantic_review"] not in SEMANTIC_REVIEW:
        raise ContractError("graph claim semantic review is invalid")
    return {
        "workspace_id": _identifier(item["workspace_id"], "graph claim workspace_id"),
        "graph_id": _identifier(item["graph_id"], "graph claim graph_id"),
        "graph_digest": _digest(item["graph_digest"], "graph claim graph_digest"),
        "source_id": _identifier(item["source_id"], "graph claim source_id"),
        "source_digest": _digest(item["source_digest"], "graph claim source_digest"),
        "evidence_kind": item["evidence_kind"],
        "evidence_id": _evidence_id(item["evidence_id"], "graph claim evidence_id"),
        "evidence_digest": _digest(item["evidence_digest"], "graph claim evidence_digest"),
        "claim_id": _identifier(item["claim_id"], "graph claim claim_id"),
        "excerpt": _public_text(item["excerpt"], "graph claim excerpt", 1024),
        "citation_anchor": _public_text(item["citation_anchor"], "graph claim citation_anchor", 1024),
        "authority_role": item["authority_role"],
        "freshness_status": item["freshness_status"],
        "qualification_codes": _qualifications(item["qualification_codes"], "graph claim qualification_codes"),
        "conflict_status": item["conflict_status"],
        "semantic_review": item["semantic_review"],
    }


def _graph_edge(value: Any) -> dict[str, Any]:
    keys = (
        "workspace_id", "graph_id", "graph_digest", "source_id", "source_digest",
        "evidence_kind", "evidence_id", "evidence_digest", "edge_id", "source_claim_id",
        "target_claim_id", "edge_type", "supporting_excerpt_digest", "authority_role",
        "freshness_status", "qualification_codes", "conflict_status", "semantic_review",
    )
    item = _object(value, keys, "graph edge evidence")
    if item["evidence_kind"] != "graph_edge":
        raise ContractError("graph edge evidence kind differs")
    if item["edge_type"] not in EDGE_TYPES:
        raise ContractError("graph edge type is invalid")
    if item["authority_role"] not in AUTHORITY_ROLES:
        raise ContractError("graph edge authority role is invalid")
    if item["freshness_status"] not in FRESHNESS_STATUSES:
        raise ContractError("graph edge freshness is invalid")
    if item["conflict_status"] not in CONFLICT_STATUSES:
        raise ContractError("graph edge conflict status is invalid")
    if item["semantic_review"] not in SEMANTIC_REVIEW:
        raise ContractError("graph edge semantic review is invalid")
    return {
        "workspace_id": _identifier(item["workspace_id"], "graph edge workspace_id"),
        "graph_id": _identifier(item["graph_id"], "graph edge graph_id"),
        "graph_digest": _digest(item["graph_digest"], "graph edge graph_digest"),
        "source_id": _identifier(item["source_id"], "graph edge source_id"),
        "source_digest": _digest(item["source_digest"], "graph edge source_digest"),
        "evidence_kind": item["evidence_kind"],
        "evidence_id": _evidence_id(item["evidence_id"], "graph edge evidence_id"),
        "evidence_digest": _digest(item["evidence_digest"], "graph edge evidence_digest"),
        "edge_id": _identifier(item["edge_id"], "graph edge edge_id"),
        "source_claim_id": _identifier(item["source_claim_id"], "graph edge source_claim_id"),
        "target_claim_id": _identifier(item["target_claim_id"], "graph edge target_claim_id"),
        "edge_type": item["edge_type"],
        "supporting_excerpt_digest": _digest(item["supporting_excerpt_digest"], "graph edge supporting_excerpt_digest"),
        "authority_role": item["authority_role"],
        "freshness_status": item["freshness_status"],
        "qualification_codes": _qualifications(item["qualification_codes"], "graph edge qualification_codes"),
        "conflict_status": item["conflict_status"],
        "semantic_review": item["semantic_review"],
    }


def validate_selection_evidence(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError("selection evidence must be an object")
    kind = value.get("evidence_kind")
    if kind == "document_block":
        return _document_block(value)
    if kind == "graph_claim":
        return _graph_claim(value)
    if kind == "graph_edge":
        return _graph_edge(value)
    raise ContractError("selection evidence kind is invalid")


def validate_selection_request(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "request_id", "primary_workspace_id", "registry_digest",
        "requested_at", "evidence_ids", "request_digest",
    ) + SELECTION_AUTHORITY_FIELDS
    body = _object(value, keys, "selection request")
    if body["schema_version"] != SELECTION_SCHEMA_VERSIONS[0]:
        raise ContractError("selection request version differs")
    result = {
        "schema_version": body["schema_version"],
        "request_id": _identifier(body["request_id"], "request_id"),
        "primary_workspace_id": _identifier(body["primary_workspace_id"], "primary_workspace_id"),
        "registry_digest": _digest(body["registry_digest"], "registry_digest"),
        "requested_at": _timestamp(body["requested_at"], "requested_at"),
        "evidence_ids": _ids(body["evidence_ids"], "evidence_ids", 1, 32),
        "request_digest": body["request_digest"],
        **_authority(body, candidate_creation=False),
    }
    _self_digest(result, "request_digest", "selection request")
    return result


def validate_selection_proposal(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "proposal_id", "request_digest", "primary_workspace_id",
        "registry_digest", "definition_digest", "prepared_at", "expires_at",
        "evidence_selection_digest", "candidate", "allowed_write_set",
        "allowed_write_set_digest", "evidence", "proposal_digest",
    ) + SELECTION_AUTHORITY_FIELDS
    body = _object(value, keys, "selection proposal")
    if body["schema_version"] != SELECTION_SCHEMA_VERSIONS[1]:
        raise ContractError("selection proposal version differs")
    prepared_at, expires_at = _ordered_times(body["prepared_at"], body["expires_at"], "selection proposal")
    candidate = _object(body["candidate"], ("candidate_id", "candidate_digest", "provenance_digest"), "selection proposal candidate")
    candidate_id = _identifier(candidate["candidate_id"], "candidate_id")
    if not candidate_id.startswith("candidate-"):
        raise ContractError("candidate_id must use the candidate- prefix")
    allowed_write_set = [_public_text(item, "allowed write path", 256) for item in _array(body["allowed_write_set"], "allowed_write_set", 2, 32)]
    if len(allowed_write_set) != len(set(allowed_write_set)):
        raise ContractError("allowed_write_set contains duplicates")
    evidence = [validate_selection_evidence(item) for item in _array(body["evidence"], "selection evidence", 1, 32)]
    seen = set()
    for item in evidence:
        identity = (
            item["workspace_id"],
            item["evidence_kind"],
            item["evidence_id"],
            item["evidence_digest"],
        )
        if identity in seen:
            raise ContractError("selection evidence contains duplicates")
        seen.add(identity)
    result = {
        "schema_version": body["schema_version"],
        "proposal_id": _identifier(body["proposal_id"], "proposal_id"),
        "request_digest": _digest(body["request_digest"], "request_digest"),
        "primary_workspace_id": _identifier(body["primary_workspace_id"], "primary_workspace_id"),
        "registry_digest": _digest(body["registry_digest"], "registry_digest"),
        "definition_digest": _digest(body["definition_digest"], "definition_digest"),
        "prepared_at": prepared_at,
        "expires_at": expires_at,
        "evidence_selection_digest": _digest(body["evidence_selection_digest"], "evidence_selection_digest"),
        "candidate": {
            "candidate_id": candidate_id,
            "candidate_digest": _digest(candidate["candidate_digest"], "candidate_digest"),
            "provenance_digest": _digest(candidate["provenance_digest"], "provenance_digest"),
        },
        "allowed_write_set": allowed_write_set,
        "allowed_write_set_digest": _digest(body["allowed_write_set_digest"], "allowed_write_set_digest"),
        "evidence": evidence,
        "proposal_digest": body["proposal_digest"],
        **_authority(body, candidate_creation=False),
    }
    if result["evidence_selection_digest"] != _selection_digest(evidence):
        raise ContractError("selection evidence digest differs")
    if result["allowed_write_set_digest"] != _write_set_digest(allowed_write_set):
        raise ContractError("selection write set digest differs")
    _strict_json_size(result, _MAX_PROPOSAL_BYTES, "selection proposal")
    _self_digest(result, "proposal_digest", "selection proposal")
    return result


def validate_selection_authorization(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "authorization_id", "proposal_id", "proposal_digest",
        "operation", "operator_id", "nonce", "source_head", "issued_at", "expires_at",
        "one_use_only", "product_self_issued", "authorization_digest",
    ) + SELECTION_AUTHORITY_FIELDS
    body = _object(value, keys, "selection authorization")
    if body["schema_version"] != SELECTION_SCHEMA_VERSIONS[2]:
        raise ContractError("selection authorization version differs")
    issued_at, expires_at = _ordered_times(body["issued_at"], body["expires_at"], "selection authorization")
    if body["operation"] != "apply":
        raise ContractError("selection authorization operation differs")
    if body["one_use_only"] is not True:
        raise ContractError("selection authorization one_use_only must remain true")
    if body["product_self_issued"] is not False:
        raise ContractError("selection authorization product_self_issued must remain false")
    result = {
        "schema_version": body["schema_version"],
        "authorization_id": _identifier(body["authorization_id"], "authorization_id"),
        "proposal_id": _identifier(body["proposal_id"], "proposal_id"),
        "proposal_digest": _digest(body["proposal_digest"], "proposal_digest"),
        "operation": body["operation"],
        "operator_id": _public_text(body["operator_id"], "operator_id", 256),
        "nonce": _identifier(body["nonce"], "nonce"),
        "source_head": _source_head(body["source_head"], "source_head"),
        "issued_at": issued_at,
        "expires_at": expires_at,
        "one_use_only": True,
        "product_self_issued": False,
        "authorization_digest": body["authorization_digest"],
        **_authority(body, candidate_creation=True),
    }
    _self_digest(result, "authorization_digest", "selection authorization")
    return result


def validate_selection_consumption(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "consumption_id", "proposal_id", "proposal_digest",
        "authorization_id", "authorization_digest", "candidate_id", "nonce",
        "consumed_at", "status", "consumption_digest",
    ) + SELECTION_AUTHORITY_FIELDS
    body = _object(value, keys, "selection consumption")
    if body["schema_version"] != SELECTION_SCHEMA_VERSIONS[3]:
        raise ContractError("selection consumption version differs")
    if body["status"] != "consumed":
        raise ContractError("selection consumption status differs")
    result = {
        "schema_version": body["schema_version"],
        "consumption_id": _identifier(body["consumption_id"], "consumption_id"),
        "proposal_id": _identifier(body["proposal_id"], "proposal_id"),
        "proposal_digest": _digest(body["proposal_digest"], "proposal_digest"),
        "authorization_id": _identifier(body["authorization_id"], "authorization_id"),
        "authorization_digest": _digest(body["authorization_digest"], "authorization_digest"),
        "candidate_id": _identifier(body["candidate_id"], "candidate_id"),
        "nonce": _identifier(body["nonce"], "nonce"),
        "consumed_at": _timestamp(body["consumed_at"], "consumed_at"),
        "status": body["status"],
        "consumption_digest": body["consumption_digest"],
        **_authority(body, candidate_creation=False),
    }
    _self_digest(result, "consumption_digest", "selection consumption")
    return result


def validate_selection_transaction(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "transaction_id", "proposal_id", "proposal_digest",
        "authorization_id", "authorization_digest", "candidate_id",
        "candidate_digest", "provenance_digest", "allowed_write_set_digest",
        "status", "committed_at", "transaction_digest",
    ) + SELECTION_AUTHORITY_FIELDS
    body = _object(value, keys, "selection transaction")
    if body["schema_version"] != SELECTION_SCHEMA_VERSIONS[4]:
        raise ContractError("selection transaction version differs")
    if body["status"] != "committed":
        raise ContractError("selection transaction status differs")
    result = {
        "schema_version": body["schema_version"],
        "transaction_id": _identifier(body["transaction_id"], "transaction_id"),
        "proposal_id": _identifier(body["proposal_id"], "proposal_id"),
        "proposal_digest": _digest(body["proposal_digest"], "proposal_digest"),
        "authorization_id": _identifier(body["authorization_id"], "authorization_id"),
        "authorization_digest": _digest(body["authorization_digest"], "authorization_digest"),
        "candidate_id": _identifier(body["candidate_id"], "candidate_id"),
        "candidate_digest": _digest(body["candidate_digest"], "candidate_digest"),
        "provenance_digest": _digest(body["provenance_digest"], "provenance_digest"),
        "allowed_write_set_digest": _digest(body["allowed_write_set_digest"], "allowed_write_set_digest"),
        "status": body["status"],
        "committed_at": _timestamp(body["committed_at"], "committed_at"),
        "transaction_digest": body["transaction_digest"],
        **_authority(body, candidate_creation=False),
    }
    _self_digest(result, "transaction_digest", "selection transaction")
    return result


def validate_selection_inspection(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "proposal_id", "proposal_digest", "authorization_id",
        "transaction_id", "candidate_id", "status", "reason_code",
        "inspection_digest",
    ) + SELECTION_AUTHORITY_FIELDS
    body = _object(value, keys, "selection inspection")
    if body["schema_version"] != SELECTION_SCHEMA_VERSIONS[5]:
        raise ContractError("selection inspection version differs")
    if body["status"] not in {"prepared", "authorized", "committed", "expired", "investigate"}:
        raise ContractError("selection inspection status is invalid")
    result = {
        "schema_version": body["schema_version"],
        "proposal_id": None if body["proposal_id"] is None else _identifier(body["proposal_id"], "proposal_id"),
        "proposal_digest": None if body["proposal_digest"] is None else _digest(body["proposal_digest"], "proposal_digest"),
        "authorization_id": None if body["authorization_id"] is None else _identifier(body["authorization_id"], "authorization_id"),
        "transaction_id": None if body["transaction_id"] is None else _identifier(body["transaction_id"], "transaction_id"),
        "candidate_id": None if body["candidate_id"] is None else _identifier(body["candidate_id"], "candidate_id"),
        "status": body["status"],
        "reason_code": _identifier(body["reason_code"], "reason_code"),
        "inspection_digest": body["inspection_digest"],
        **_authority(body, candidate_creation=False),
    }
    _self_digest(result, "inspection_digest", "selection inspection")
    return result


def validate_selection_recovery(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "workspace_id", "status", "reason_code", "proposal_ids",
        "candidate_ids", "recovery_digest",
    ) + SELECTION_AUTHORITY_FIELDS
    body = _object(value, keys, "selection recovery")
    if body["schema_version"] != SELECTION_SCHEMA_VERSIONS[6]:
        raise ContractError("selection recovery version differs")
    if body["status"] not in {"empty", "no_op", "recovered", "investigate"}:
        raise ContractError("selection recovery status is invalid")
    proposal_ids = [_identifier(item, "proposal_ids") for item in _array(body["proposal_ids"], "proposal_ids", 0, 32)]
    candidate_ids = [_identifier(item, "candidate_ids") for item in _array(body["candidate_ids"], "candidate_ids", 0, 32)]
    if len(proposal_ids) != len(set(proposal_ids)):
        raise ContractError("proposal_ids contains duplicates")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ContractError("candidate_ids contains duplicates")
    result = {
        "schema_version": body["schema_version"],
        "workspace_id": _identifier(body["workspace_id"], "workspace_id"),
        "status": body["status"],
        "reason_code": _identifier(body["reason_code"], "reason_code"),
        "proposal_ids": proposal_ids,
        "candidate_ids": candidate_ids,
        "recovery_digest": body["recovery_digest"],
        **_authority(body, candidate_creation=False),
    }
    _self_digest(result, "recovery_digest", "selection recovery")
    return result
