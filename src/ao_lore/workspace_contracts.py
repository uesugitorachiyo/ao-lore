"""Strict, dependency-free contracts for isolated AO Lore workspaces."""

from __future__ import annotations

import hmac
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from ._strict_io import ContractError
from .benchmark import BenchmarkError, canonical_digest
from .evidence_graph_contracts import validate_graph_manifest


WORKSPACE_SCHEMA_VERSIONS = (
    "ao.lore.workspace-definition.v0.1",
    "ao.lore.workspace-registry-generation.v0.1",
    "ao.lore.workspace-registry-inspection.v0.1",
    "ao.lore.workspace-operation-readback.v0.1",
    "ao.lore.workspace-query-readback.v0.1",
)
WORKSPACE_DEFINITION_VERSIONS = (
    "ao.lore.workspace-definition.v0.1",
    "ao.lore.workspace-definition.v0.2",
)
WORKSPACE_QUERY_READBACK_VERSIONS = (
    "ao.lore.workspace-query-readback.v0.1",
    "ao.lore.workspace-query-readback.v0.2",
)
WORKSPACE_TYPES = ("reference", "property", "matter", "operations")
WORKSPACE_STATUSES = ("active", "inactive", "investigate")
WORKSPACE_REASON_CODES = (
    "ok", "workspace_unknown", "workspace_inactive", "registry_invalid",
    "identity_collision", "reference_not_declared", "reference_cycle",
    "workspace_binding_drift", "freshness_investigation_required",
    "recovery_pending",
)
AUTHORITY_FIELDS = (
    "legal_advice", "property_decision", "candidate_review", "candidate_decision",
    "promotion", "canonical_query", "provider", "credential", "private_data",
    "publication", "release", "deployment", "authority_advanced",
)

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DOCUMENT_ID = re.compile(r"^(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._-]{0,127})$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SUCCESS_STATUSES = ("active", "empty", "complete", "success")
_OPERATION_STATUSES = _SUCCESS_STATUSES + ("inactive", "investigate")


def _object(value: Any, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys) or any(type(key) is not str for key in value):
        raise ContractError(f"{label} keys differ")
    return value


def _array(value: Any, label: str, minimum: int, maximum: int) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ContractError(f"{label} must be a bounded array")
    return value


def _identifier(value: Any, label: str) -> str:
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


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"{label} is invalid")
    return value


def _timestamp(value: Any, label: str) -> str:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise ContractError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{label} is invalid") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError(f"{label} is not UTC")
    return value


def _public_text(value: Any, label: str, maximum: int = 512) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ContractError(f"{label} must be bounded text")
    lowered = value.casefold()
    if (any(ord(character) < 32 for character in value)
            or lowered.startswith(("/", "file:"))
            or "/home/" in lowered or "/opt/" in lowered or "/tmp/" in lowered
            or "\\" in value):
        raise ContractError(f"{label} is unsafe")
    return value


def _unique(values: Iterable[str], label: str) -> list[str]:
    result = list(values)
    if len(result) != len(set(result)):
        raise ContractError(f"{label} contains duplicates")
    return result


def _ids(value: Any, label: str, minimum: int, maximum: int) -> list[str]:
    return _unique((_identifier(item, label) for item in _array(value, label, minimum, maximum)), label)


def _authority(body: dict[str, Any]) -> dict[str, bool]:
    for field in AUTHORITY_FIELDS:
        if body[field] is not False:
            raise ContractError(f"{field} must remain false")
    return {field: False for field in AUTHORITY_FIELDS}


def _result_reason(result: str, reason: str, successful: tuple[str, ...], label: str) -> None:
    if (result in successful) != (reason == "ok"):
        raise ContractError(f"{label} reason differs")


def _self_digest(body: dict[str, Any], field: str, label: str) -> None:
    supplied = _digest(body[field], field)
    try:
        expected = canonical_digest({key: item for key, item in body.items() if key != field})
    except BenchmarkError as exc:
        raise ContractError(f"{label} is not strict JSON") from exc
    if not hmac.compare_digest(supplied, expected):
        raise ContractError(f"{label} digest differs")


def validate_workspace_definition(value: Any) -> dict[str, Any]:
    common_keys = (
        "schema_version", "workspace_id", "workspace_version", "workspace_type",
        "domain", "jurisdiction", "lifecycle_status", "root_workflow_id",
        "root_workflow_digest", "source_registry_id", "source_registry_digest",
        "graph_id", "graph_digest", "freshness_policy_id", "freshness_policy_digest",
        "freshness_summary_status", "freshness_summary_id", "freshness_summary_digest",
        "reference_workspace_ids", "definition_digest",
    )
    if type(value) is not dict or value.get("schema_version") not in WORKSPACE_DEFINITION_VERSIONS:
        raise ContractError("workspace definition version differs")
    version = value["schema_version"]
    keys = common_keys + (() if version == WORKSPACE_DEFINITION_VERSIONS[0] else (
        "document_store_id", "document_generation_digest",
    )) + AUTHORITY_FIELDS
    body = _object(value, keys, "workspace definition")
    if body["workspace_type"] not in WORKSPACE_TYPES:
        raise ContractError("workspace type is invalid")
    if body["lifecycle_status"] not in WORKSPACE_STATUSES:
        raise ContractError("workspace lifecycle status is invalid")
    policy_id = None if body["freshness_policy_id"] is None else _identifier(body["freshness_policy_id"], "freshness_policy_id")
    policy_digest = None if body["freshness_policy_digest"] is None else _digest(body["freshness_policy_digest"], "freshness_policy_digest")
    if (policy_id is None) != (policy_digest is None):
        raise ContractError("freshness policy binding is partial")
    summary_id = None if body["freshness_summary_id"] is None else _identifier(body["freshness_summary_id"], "freshness_summary_id")
    summary_digest = None if body["freshness_summary_digest"] is None else _digest(body["freshness_summary_digest"], "freshness_summary_digest")
    if body["freshness_summary_status"] not in {"observed", "not_observed"}:
        raise ContractError("freshness summary status is invalid")
    if body["freshness_summary_status"] == "observed":
        if policy_id is None or summary_id is None or summary_digest is None:
            raise ContractError("observed freshness summary lacks a complete binding")
    elif summary_id is not None or summary_digest is not None:
        raise ContractError("unobserved freshness summary has a binding")
    references = _ids(body["reference_workspace_ids"], "reference workspace ids", 0, 16)
    if references != sorted(references):
        raise ContractError("reference workspace ids are not ordered")
    workspace_id = _identifier(body["workspace_id"], "workspace_id")
    if workspace_id in references:
        raise ContractError("workspace references itself")
    if body["workspace_type"] == "reference" and references:
        raise ContractError("reference workspace declares references")
    graph_id = _identifier(body["graph_id"], "graph_id") if body["graph_id"] is not None else None
    graph_digest = _digest(body["graph_digest"], "graph_digest") if body["graph_digest"] is not None else None
    if version == WORKSPACE_DEFINITION_VERSIONS[0] and (graph_id is None or graph_digest is None):
        raise ContractError("v0.1 workspace requires a graph binding")
    if (graph_id is None) != (graph_digest is None):
        raise ContractError("workspace graph binding is partial")
    result = {
        "schema_version": body["schema_version"], "workspace_id": workspace_id,
        "workspace_version": _integer(body["workspace_version"], "workspace_version", 1, 2147483647),
        "workspace_type": body["workspace_type"],
        "domain": _identifier(body["domain"], "domain"),
        "jurisdiction": _identifier(body["jurisdiction"], "jurisdiction"),
        "lifecycle_status": body["lifecycle_status"],
        "root_workflow_id": _identifier(body["root_workflow_id"], "root_workflow_id"),
        "root_workflow_digest": _digest(body["root_workflow_digest"], "root_workflow_digest"),
        "source_registry_id": _identifier(body["source_registry_id"], "source_registry_id"),
        "source_registry_digest": _digest(body["source_registry_digest"], "source_registry_digest"),
        "graph_id": graph_id, "graph_digest": graph_digest,
        "freshness_policy_id": policy_id, "freshness_policy_digest": policy_digest,
        "freshness_summary_status": body["freshness_summary_status"],
        "freshness_summary_id": summary_id, "freshness_summary_digest": summary_digest,
        "reference_workspace_ids": references, "definition_digest": body["definition_digest"],
        **_authority(body),
    }
    if version == WORKSPACE_DEFINITION_VERSIONS[1]:
        result["document_store_id"] = _identifier(body["document_store_id"], "document_store_id")
        result["document_generation_digest"] = (
            None if body["document_generation_digest"] is None
            else _digest(body["document_generation_digest"], "document_generation_digest")
        )
        # Restore exact input key order; digests are order-independent but callers expect byte-compatible dictionaries.
        result = {key: result[key] for key in body}
    _self_digest(result, "definition_digest", "workspace definition")
    return result


def validate_workspace_registry_generation(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "registry_id", "sequence", "predecessor_registry_digest",
            "generated_at", "workspaces", "registry_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "workspace registry generation")
    if body["schema_version"] != WORKSPACE_SCHEMA_VERSIONS[1]:
        raise ContractError("workspace registry generation version differs")
    sequence = _integer(body["sequence"], "sequence", 1, 2147483647)
    predecessor = body["predecessor_registry_digest"]
    if predecessor is not None:
        predecessor = _digest(predecessor, "predecessor_registry_digest")
    if (sequence == 1) != (predecessor is None):
        raise ContractError("registry predecessor binding differs")
    workspaces = [validate_workspace_definition(item) for item in _array(body["workspaces"], "workspaces", 0, 64)]
    if [item["workspace_id"] for item in workspaces] != sorted(item["workspace_id"] for item in workspaces):
        raise ContractError("workspace definitions are not ordered")
    for field in ("workspace_id", "root_workflow_id", "source_registry_id", "graph_id", "document_store_id"):
        _unique((item[field] for item in workspaces if item.get(field) is not None), f"{field} identities")
    by_id = {item["workspace_id"]: item for item in workspaces}
    for workspace in workspaces:
        for reference_id in workspace["reference_workspace_ids"]:
            reference = by_id.get(reference_id)
            if reference is None:
                raise ContractError("workspace reference is not declared")
            if reference["workspace_type"] != "reference":
                raise ContractError("workspace reference does not target a reference workspace")
            if reference["reference_workspace_ids"]:
                raise ContractError("transitive workspace reference is forbidden")
    result = {
        "schema_version": body["schema_version"],
        "registry_id": _identifier(body["registry_id"], "registry_id"),
        "sequence": sequence, "predecessor_registry_digest": predecessor,
        "generated_at": _timestamp(body["generated_at"], "generated_at"),
        "workspaces": workspaces, "registry_digest": body["registry_digest"],
        **_authority(body),
    }
    _self_digest(result, "registry_digest", "workspace registry generation")
    return result


def _validated_context(generation: Any | None) -> dict[str, Any] | None:
    return None if generation is None else validate_workspace_registry_generation(generation)


def validate_workspace_registry_inspection(value: Any, *, generation: Any | None = None) -> dict[str, Any]:
    keys = ("schema_version", "inspection_id", "registry_id", "registry_digest", "sequence",
            "status", "reason_code", "workspace_ids", "workspace_count", "active_count",
            "inactive_count", "investigate_count", "inspection_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "workspace registry inspection")
    if body["schema_version"] != WORKSPACE_SCHEMA_VERSIONS[2]:
        raise ContractError("workspace registry inspection version differs")
    if body["status"] not in (*WORKSPACE_STATUSES, "empty") or body["reason_code"] not in WORKSPACE_REASON_CODES:
        raise ContractError("workspace registry inspection status differs")
    _result_reason(body["status"], body["reason_code"], ("active", "empty"), "workspace registry inspection")
    workspace_ids = _ids(body["workspace_ids"], "workspace ids", 0, 64)
    if workspace_ids != sorted(workspace_ids):
        raise ContractError("workspace ids are not ordered")
    counts = [_integer(body[field], field, 0, 64) for field in ("active_count", "inactive_count", "investigate_count")]
    workspace_count = _integer(body["workspace_count"], "workspace_count", 0, 64)
    if workspace_count != len(workspace_ids) or workspace_count != sum(counts):
        raise ContractError("workspace inspection counts differ")
    empty = body["status"] == "empty"
    registry_id = body["registry_id"]
    registry_digest = body["registry_digest"]
    sequence = body["sequence"]
    if empty:
        if registry_id is not None or registry_digest is not None or sequence != 0 or workspace_count != 0:
            raise ContractError("empty workspace registry inspection binding differs")
    else:
        registry_id = _identifier(registry_id, "registry_id")
        registry_digest = _digest(registry_digest, "registry_digest")
        sequence = _integer(sequence, "sequence", 1, 2147483647)
    result = {
        "schema_version": body["schema_version"], "inspection_id": _identifier(body["inspection_id"], "inspection_id"),
        "registry_id": registry_id, "registry_digest": registry_digest, "sequence": sequence,
        "status": body["status"], "reason_code": body["reason_code"], "workspace_ids": workspace_ids,
        "workspace_count": workspace_count, "active_count": counts[0], "inactive_count": counts[1],
        "investigate_count": counts[2], "inspection_digest": body["inspection_digest"], **_authority(body),
    }
    _self_digest(result, "inspection_digest", "workspace registry inspection")
    context = _validated_context(generation)
    if empty and context is not None:
        raise ContractError("empty workspace registry inspection has a generation")
    if context is not None:
        expected_ids = sorted(item["workspace_id"] for item in context["workspaces"])
        expected_counts = [sum(item["lifecycle_status"] == status for item in context["workspaces"]) for status in WORKSPACE_STATUSES]
        if (result["registry_id"], result["registry_digest"], result["sequence"]) != (context["registry_id"], context["registry_digest"], context["sequence"]) or workspace_ids != expected_ids or counts != expected_counts:
            raise ContractError("workspace registry inspection binding differs")
    return result


def _selected_ids(value: Iterable[str] | None) -> set[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise ContractError("selected workspace ids are invalid")
    result = {_identifier(item, "selected workspace id") for item in value}
    return result


def validate_workspace_operation_readback(value: Any, *, generation: Any | None = None,
                                          selected_workspace_ids: Iterable[str] | None = None) -> dict[str, Any]:
    keys = ("schema_version", "operation_id", "operation", "workspace_id", "registry_id",
            "registry_digest", "status", "reason_code", "affected_workspace_ids", "readback_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "workspace operation readback")
    if body["schema_version"] != WORKSPACE_SCHEMA_VERSIONS[3]:
        raise ContractError("workspace operation readback version differs")
    if body["operation"] not in {"list", "inspect", "refresh", "replay", "recover"}:
        raise ContractError("workspace operation is invalid")
    if body["status"] not in _OPERATION_STATUSES or body["reason_code"] not in WORKSPACE_REASON_CODES:
        raise ContractError("workspace operation result is invalid")
    _result_reason(body["status"], body["reason_code"], _SUCCESS_STATUSES, "workspace operation")
    workspace_id = None if body["workspace_id"] is None else _identifier(body["workspace_id"], "workspace_id")
    affected = _ids(body["affected_workspace_ids"], "affected workspace ids", 0, 64)
    if affected != sorted(affected):
        raise ContractError("affected workspace ids are not ordered")
    if body["operation"] == "list" and workspace_id is not None:
        raise ContractError("list operation must not bind one workspace")
    if body["operation"] != "list" and workspace_id is None:
        raise ContractError("workspace operation lacks workspace identity")
    result = {
        "schema_version": body["schema_version"], "operation_id": _identifier(body["operation_id"], "operation_id"),
        "operation": body["operation"], "workspace_id": workspace_id,
        "registry_id": _identifier(body["registry_id"], "registry_id"),
        "registry_digest": _digest(body["registry_digest"], "registry_digest"),
        "status": body["status"], "reason_code": body["reason_code"],
        "affected_workspace_ids": affected, "readback_digest": body["readback_digest"], **_authority(body),
    }
    _self_digest(result, "readback_digest", "workspace operation readback")
    context = _validated_context(generation)
    selected = _selected_ids(selected_workspace_ids)
    if context is not None and (result["registry_id"], result["registry_digest"]) != (context["registry_id"], context["registry_digest"]):
        raise ContractError("workspace operation registry binding differs")
    if context is not None:
        known = {item["workspace_id"] for item in context["workspaces"]}
        if set(affected) - known or (workspace_id is not None and workspace_id not in known):
            raise ContractError("workspace operation contains an unknown workspace")
    if selected is not None and (set(affected) - selected or (workspace_id is not None and workspace_id not in selected)):
        raise ContractError("workspace operation selection differs")
    return result


def validate_workspace_query_readback(value: Any, *, generation: Any | None = None,
                                      selected_workspace_ids: Iterable[str] | None = None,
                                      graph_manifests: Iterable[Any] | None = None,
                                      document_generations: Iterable[Any] | None = None) -> dict[str, Any]:
    keys = ("schema_version", "query_id", "prompt_digest", "registry_id", "registry_digest",
            "primary_workspace_id", "consulted_workspace_ids", "outcome", "reason_code",
            "evidence", "qualifications", "readback_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "workspace query readback")
    if body["schema_version"] not in WORKSPACE_QUERY_READBACK_VERSIONS:
        raise ContractError("workspace query readback version differs")
    if body["outcome"] not in {"answer", "partial", "refuse", "investigate"} or body["reason_code"] not in WORKSPACE_REASON_CODES:
        raise ContractError("workspace query outcome is invalid")
    _result_reason(body["outcome"], body["reason_code"], ("answer", "partial"), "workspace query")
    consulted = _ids(body["consulted_workspace_ids"], "consulted workspace ids", 1, 17)
    if consulted != sorted(consulted):
        raise ContractError("consulted workspace ids are not ordered")
    primary = _identifier(body["primary_workspace_id"], "primary_workspace_id")
    if primary not in consulted:
        raise ContractError("primary workspace was not consulted")
    evidence = []
    for raw in _array(body["evidence"], "evidence", 0, 128):
        kind = raw.get("evidence_kind") if type(raw) is dict else None
        if kind == "document_block" and body["schema_version"] == WORKSPACE_QUERY_READBACK_VERSIONS[1]:
            item = _object(raw, ("workspace_id", "document_store_id", "generation_digest", "document_id", "source_id", "evidence_kind", "evidence_id", "evidence_digest"), "query evidence")
            evidence.append({
                "workspace_id": _identifier(item["workspace_id"], "evidence workspace_id"),
                "document_store_id": _identifier(item["document_store_id"], "document_store_id"),
                "generation_digest": _digest(item["generation_digest"], "generation_digest"),
                "document_id": _document_id(item["document_id"], "document_id"),
                "source_id": _identifier(item["source_id"], "evidence source_id"),
                "evidence_kind": kind, "evidence_id": _digest(item["evidence_id"], "evidence_id"),
                "evidence_digest": _digest(item["evidence_digest"], "evidence_digest"),
            })
        else:
            item = _object(raw, ("workspace_id", "graph_id", "source_id", "evidence_kind", "evidence_id", "evidence_digest"), "query evidence")
            allowed_graph_kinds = ({"graph_claim", "graph_edge"}
                                   if body["schema_version"] == WORKSPACE_QUERY_READBACK_VERSIONS[1]
                                   else {"claim", "edge"})
            if kind not in allowed_graph_kinds:
                raise ContractError("query evidence kind is invalid")
            evidence.append({"workspace_id": _identifier(item["workspace_id"], "evidence workspace_id"),
                             "graph_id": _identifier(item["graph_id"], "evidence graph_id"),
                             "source_id": _identifier(item["source_id"], "evidence source_id"),
                             "evidence_kind": kind,
                             "evidence_id": _identifier(item["evidence_id"], "evidence_id"),
                             "evidence_digest": _digest(item["evidence_digest"], "evidence_digest")})
    identities = [tuple(item.values()) for item in evidence]
    if len(identities) != len(set(identities)):
        raise ContractError("query evidence contains duplicates")
    if identities != sorted(identities):
        raise ContractError("query evidence is not origin ordered")
    qualifications = _unique((_public_text(item, "qualification") for item in _array(body["qualifications"], "qualifications", 0, 16)), "qualifications")
    result = {
        "schema_version": body["schema_version"], "query_id": _identifier(body["query_id"], "query_id"),
        "prompt_digest": _digest(body["prompt_digest"], "prompt_digest"),
        "registry_id": _identifier(body["registry_id"], "registry_id"),
        "registry_digest": _digest(body["registry_digest"], "registry_digest"),
        "primary_workspace_id": primary, "consulted_workspace_ids": consulted,
        "outcome": body["outcome"], "reason_code": body["reason_code"], "evidence": evidence,
        "qualifications": qualifications, "readback_digest": body["readback_digest"], **_authority(body),
    }
    _self_digest(result, "readback_digest", "workspace query readback")
    context = _validated_context(generation)
    selected = _selected_ids(selected_workspace_ids)
    if context is not None:
        if (result["registry_id"], result["registry_digest"]) != (context["registry_id"], context["registry_digest"]):
            raise ContractError("workspace query registry binding differs")
        definitions = {item["workspace_id"]: item for item in context["workspaces"]}
        primary_definition = definitions.get(primary)
        if primary_definition is None:
            raise ContractError("primary workspace is unknown")
        expected = {primary, *primary_definition["reference_workspace_ids"]}
        if set(consulted) != expected:
            raise ContractError("consulted workspace binding differs")
        for item in evidence:
            origin = definitions.get(item["workspace_id"])
            origin_id = item.get("graph_id", item.get("document_store_id"))
            expected_id = None if origin is None else (origin.get("graph_id") if "graph_id" in item else origin.get("document_store_id"))
            if item["workspace_id"] not in expected or origin is None or origin_id != expected_id:
                raise ContractError("query evidence origin binding differs")
    if selected is not None and set(consulted) != selected:
        raise ContractError("workspace query selection differs")
    if graph_manifests is not None:
        if type(graph_manifests) not in {list, tuple}:
            raise ContractError("workspace query graph context is invalid")
        graphs = [validate_graph_manifest(item) for item in graph_manifests]
        by_graph = {item["graph_id"]: item for item in graphs}
        graph_evidence = [item for item in evidence if "graph_id" in item]
        expected_graph_ids = {item["graph_id"] for item in graph_evidence}
        if context is not None:
            definitions = {item["workspace_id"]: item for item in context["workspaces"]}
            expected_graph_ids = {definitions[item]["graph_id"] for item in consulted if definitions[item].get("graph_id") is not None}
        if len(by_graph) != len(graphs) or set(by_graph) != expected_graph_ids:
            raise ContractError("workspace query graph context differs")
        allowed = set()
        for item in graph_evidence:
            graph = by_graph[item["graph_id"]]
            workspace_id = item["workspace_id"]
            claims = {claim["claim_id"]: claim for claim in graph["claims"]}
            for claim in claims.values():
                allowed.add((
                    workspace_id, graph["graph_id"], claim["source_id"],
                    "graph_claim" if body["schema_version"] == WORKSPACE_QUERY_READBACK_VERSIONS[1] else "claim",
                    claim["claim_id"], claim["excerpt_digest"],
                ))
            for edge in graph["edges"]:
                source_id = None
                if edge["source_kind"] == "source":
                    source_id = edge["source_id"]
                elif edge["source_kind"] == "claim" and edge["source_id"] in claims:
                    source_id = claims[edge["source_id"]]["source_id"]
                elif edge["target_kind"] == "source":
                    source_id = edge["target_id"]
                elif edge["target_kind"] == "claim" and edge["target_id"] in claims:
                    source_id = claims[edge["target_id"]]["source_id"]
                if source_id is not None:
                    allowed.add((
                        workspace_id, graph["graph_id"], source_id,
                        "graph_edge" if body["schema_version"] == WORKSPACE_QUERY_READBACK_VERSIONS[1] else "edge",
                        edge["edge_id"], edge["edge_digest"],
                    ))
        if any(tuple(item.values()) not in allowed for item in graph_evidence):
            raise ContractError("workspace query evidence context binding differs")
    if document_generations is not None:
        if type(document_generations) not in {list, tuple}:
            raise ContractError("workspace query document context is invalid")
        from .document_evidence_contracts import validate_workspace_document_generation
        values = [validate_workspace_document_generation(item) for item in document_generations]
        by_digest = {item["generation_digest"]: item for item in values}
        if len(by_digest) != len(values):
            raise ContractError("workspace query document context differs")
        by_workspace = {item["workspace_id"]: item for item in values}
        if len(by_workspace) != len(values):
            raise ContractError("workspace query document context differs")
        document_evidence = [item for item in evidence if "document_store_id" in item]
        expected_digests = {item["generation_digest"] for item in document_evidence}
        if context is not None:
            definitions = {item["workspace_id"]: item for item in context["workspaces"]}
            expected_digests = {definitions[item]["document_generation_digest"] for item in consulted
                                if definitions[item].get("document_generation_digest") is not None}
        if set(by_digest) != expected_digests:
            raise ContractError("workspace query document context differs")
        if context is not None:
            predecessor = context["predecessor_registry_digest"]
            if predecessor is None and by_workspace:
                raise ContractError("workspace query document context lineage differs")
            for workspace_id in consulted:
                definition = definitions[workspace_id]
                expected_digest = definition.get("document_generation_digest")
                if expected_digest is None:
                    continue
                value = by_workspace.get(workspace_id)
                if (
                    value is None
                    or value["document_store_id"] != definition["document_store_id"]
                    or value["generation_digest"] != expected_digest
                    or value["registry_digest"] != predecessor
                ):
                    raise ContractError("workspace query document context lineage differs")
        allowed_documents = set()
        for workspace_id in consulted:
            definition = None if context is None else definitions[workspace_id]
            candidates = values if definition is None else [item for item in values
                if item["generation_digest"] == definition.get("document_generation_digest")]
            for value in candidates:
                for document in value["documents"]:
                    blocks = []
                    def collect(items):
                        for block in items:
                            blocks.append(block); collect(block.get("children", []))
                    collect(document["document_ir"]["blocks"])
                    for block in blocks:
                        block_digest = canonical_digest(block)
                        evidence_id = canonical_digest({
                            "workspace_id": workspace_id, "generation_digest": value["generation_digest"],
                            "document_id": document["document_id"], "document_ir_digest": document["document_ir_digest"],
                            "block_id": block["id"], "block_digest": block_digest,
                            "source_span": block["source_span"], "text_digest": canonical_digest(block["text"]),
                        })
                        allowed_documents.add((workspace_id, value["document_store_id"], value["generation_digest"],
                            document["document_id"], document["source_id"], "document_block", evidence_id, block_digest))
        if any(tuple(item.values()) not in allowed_documents for item in document_evidence):
            raise ContractError("workspace query document evidence binding differs")
    return result
