"""Strict, dependency-free contracts for public evidence-graph artifacts."""

from __future__ import annotations

import copy
import hmac
import ipaddress
import re
import weakref
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ._strict_io import ContractError
from .benchmark import BenchmarkError, canonical_digest


SCHEMA_VERSIONS = (
    "ao.lore.evidence-source-registry.v0.1",
    "ao.lore.evidence-acquisition-record.v0.1",
    "ao.lore.evidence-authority-role.v0.1",
    "ao.lore.evidence-relationship-edge.v0.1",
    "ao.lore.evidence-operational-question.v0.1",
    "ao.lore.evidence-graph-manifest.v0.1",
    "ao.lore.evidence-graph-inspection.v0.1",
    "ao.lore.evidence-query-readback.v0.1",
    "ao.lore.evidence-campaign-summary.v0.1",
    "ao.lore.evidence-recovery.v0.1",
    "ao.lore.evidence-freshness-policy.v0.1",
    "ao.lore.evidence-freshness-observation.v0.1",
    "ao.lore.evidence-freshness-comparison.v0.1",
    "ao.lore.evidence-freshness-summary.v0.1",
)
AUTHORITY_ROLES = (
    "primary_law", "official_interpretation", "local_enforcement_guidance",
    "technical_guidance", "operator_procedure", "case_evidence",
)
ROLE_PRECEDENCE = {role: 5 - index for index, role in enumerate(AUTHORITY_ROLES)}
EDGE_TYPES = (
    "cites", "defines", "explains", "implements", "applies_locally_to",
    "provides_remediation_for", "requires_record_of", "qualifies",
    "supersedes", "effective_from", "conflicts_with",
)
EDGE_REASON_CODES = (
    "source_citation", "statutory_definition", "official_explanation",
    "workflow_implementation", "local_application", "technical_remediation",
    "recordkeeping_requirement", "scope_qualification", "version_transition",
    "documented_conflict",
)
SEMANTIC_REASON_CODES = (
    "topic_match", "topic_mismatch", "copied_rationale", "layout_artifact",
    "authority_mismatch", "scope_qualification_required", "citation_supported",
    "citation_unsupported",
)
FRESHNESS_CLASSIFICATIONS = (
    "unchanged", "updated", "superseded", "unavailable", "investigate",
)
FRESHNESS_REASON_CODES = (
    "content_unchanged", "content_changed", "declared_successor_verified",
    "terminal_unavailable", "redirect_drift", "media_type_drift", "locator_drift",
    "version_ambiguous", "effective_date_ambiguous", "prior_binding_drift",
    "unsafe_observation",
)
AUTHORITY_FIELDS = (
    "legal_advice", "property_decision", "candidate_review", "candidate_decision",
    "promotion", "canonical_query", "provider", "credential", "private_data",
    "publication", "release", "deployment", "authority_advanced",
)
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DATE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_MEDIA = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,127}$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_AMBIGUOUS_IPV4 = re.compile(
    r"^(?:(?:0x[0-9a-f]+|[0-9]+)\.)*(?:0x[0-9a-f]+|[0-9]+)$"
)
MAX_LOCATOR_LENGTH = 2048
MAX_LOCATOR_HOSTNAME_LENGTH = 253
MAX_LOCATOR_PATH_LENGTH = 1024


def _object(value: Any, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys) or any(type(key) is not str for key in value):
        raise ContractError(f"{label} keys differ")
    return value


def _array(value: Any, label: str, minimum: int, maximum: int) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ContractError(f"{label} must be a bounded array")
    return value


def _text(value: Any, label: str, maximum: int, *, minimum: int = 1) -> str:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        raise ContractError(f"{label} must be bounded text")
    if any(ord(char) < 32 for char in value):
        raise ContractError(f"{label} contains control characters")
    return value


def _public_text(value: Any, label: str, maximum: int, *, minimum: int = 1) -> str:
    result = _text(value, label, maximum, minimum=minimum)
    lowered = result.casefold()
    if lowered.startswith(("/", "file:")) or "/home/" in lowered or "/opt/" in lowered or "\\" in result:
        raise ContractError(f"{label} leaks a path")
    return result


def _identifier(value: Any, label: str) -> str:
    if type(value) is not str or not _ID.fullmatch(value):
        raise ContractError(f"{label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ContractError(f"{label} is invalid")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"{label} is invalid")
    return value


def _timestamp(value: Any, label: str) -> str:
    result = _text(value, label, 32)
    if not result.endswith("Z"):
        raise ContractError(f"{label} is not UTC")
    try:
        parsed = datetime.fromisoformat(result[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{label} is invalid") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError(f"{label} is not UTC")
    return result


def _date(value: Any, label: str) -> str:
    if type(value) is not str or not _DATE.fullmatch(value):
        raise ContractError(f"{label} is invalid")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{label} is invalid") from exc
    return value


def _legacy_ipv4_component(value: str) -> int | None:
    if value.startswith("0x"):
        digits, base, maximum_length = value[2:], 16, 8
        valid = bool(digits) and all(char in "0123456789abcdef" for char in digits)
    elif len(value) > 1 and value.startswith("0"):
        digits, base, maximum_length = value[1:], 8, 11
        valid = all(char in "01234567" for char in digits)
    else:
        digits, base, maximum_length = value, 10, 10
        valid = bool(digits) and digits.isascii() and digits.isdecimal()
    if not valid or len(digits) > maximum_length:
        return None
    return int(digits, base)


def _is_legacy_ipv4_numeric(value: str) -> bool:
    parts = value.split(".")
    if not 1 <= len(parts) <= 4:
        return False
    parsed = [_legacy_ipv4_component(part) for part in parts]
    if any(part is None for part in parsed):
        return False
    limits = {
        1: (0xffffffff,),
        2: (0xff, 0xffffff),
        3: (0xff, 0xff, 0xffff),
        4: (0xff, 0xff, 0xff, 0xff),
    }[len(parts)]
    return all(part <= limit for part, limit in zip(parsed, limits, strict=True))


def validate_dns_hostname(value: Any, label: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= MAX_LOCATOR_HOSTNAME_LENGTH:
        raise ContractError(f"{label} is invalid")
    if value != value.casefold() or not value.isascii() or value.endswith("."):
        raise ContractError(f"{label} is invalid")
    if any(not _DNS_LABEL.fullmatch(part) for part in value.split(".")):
        raise ContractError(f"{label} is invalid")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ContractError(f"{label} is invalid")
    if _is_legacy_ipv4_numeric(value):
        raise ContractError(f"{label} is invalid")
    if _AMBIGUOUS_IPV4.fullmatch(value):
        raise ContractError(f"{label} is invalid")
    return value


def validate_https_locator(value: Any, label: str) -> str:
    result = _text(value, label, MAX_LOCATOR_LENGTH)
    if (not result.startswith("https://") or result != result.strip()
            or any(127 <= ord(char) <= 159 for char in result) or "#" in result):
        raise ContractError(f"{label} is unsafe")
    try:
        parsed = urlsplit(result)
    except ValueError as exc:
        raise ContractError(f"{label} is unsafe") from exc
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        raise ContractError(f"{label} is unsafe")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ContractError(f"{label} is unsafe") from exc
    if parsed.hostname is None:
        raise ContractError(f"{label} is unsafe")
    host = validate_dns_hostname(parsed.hostname, f"{label} hostname")
    if parsed.netloc not in {host, host + ":443"} or port not in (None, 443):
        raise ContractError(f"{label} is unsafe")
    if not parsed.path.startswith("/") or len(parsed.path) > MAX_LOCATOR_PATH_LENGTH:
        raise ContractError(f"{label} is unsafe")
    return result


def _locator(value: Any, label: str) -> str:
    return validate_https_locator(value, label)


def _unique(values: list[str], label: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ContractError(f"{label} contains duplicates")
    return values


def _ids(value: Any, label: str, minimum: int, maximum: int) -> list[str]:
    return _unique([_identifier(item, label) for item in _array(value, label, minimum, maximum)], label)


def _digests(value: Any, label: str, minimum: int, maximum: int) -> list[str]:
    return _unique([_digest(item, label) for item in _array(value, label, minimum, maximum)], label)


def _authority(body: dict[str, Any]) -> dict[str, bool]:
    result = {}
    for field in AUTHORITY_FIELDS:
        if body[field] is not False:
            raise ContractError(f"{field} must remain false")
        result[field] = False
    return result


def _self_digest(body: dict[str, Any], field: str, label: str) -> str:
    supplied = _digest(body[field], field)
    try:
        expected = canonical_digest({key: item for key, item in body.items() if key != field})
    except BenchmarkError as exc:
        raise ContractError(f"{label} is not strict JSON") from exc
    if not hmac.compare_digest(supplied, expected):
        raise ContractError(f"{label} digest differs")
    return supplied


def canonical_evidence_digest(value: Any, domain: str) -> str:
    _identifier(domain, "digest domain")
    try:
        return canonical_digest({"domain": f"ao.lore.evidence-graph.{domain}.v0.1", "value": value})
    except BenchmarkError as exc:
        raise ContractError("evidence value is not strict JSON") from exc


def _source(value: Any) -> dict[str, Any]:
    keys = ("source_id", "source_digest", "canonical_locator", "retrieved_at", "publisher",
            "jurisdiction", "authority_role", "version", "effective_date", "operational_question_ids",
            "relationship_edge_ids", "status", "media_type", "retained_artifact_digests")
    item = _object(value, keys, "source")
    role = item["authority_role"]
    if role not in AUTHORITY_ROLES:
        raise ContractError("source authority role is invalid")
    if item["status"] not in {"current", "historical", "historical_unavailable", "investigate"}:
        raise ContractError("source status is invalid")
    if type(item["media_type"]) is not str or not _MEDIA.fullmatch(item["media_type"]):
        raise ContractError("source media type is invalid")
    version = item["version"]
    if version is not None:
        version = _public_text(version, "source version", 128)
    return {
        "source_id": _identifier(item["source_id"], "source_id"),
        "source_digest": _digest(item["source_digest"], "source_digest"),
        "canonical_locator": _locator(item["canonical_locator"], "canonical_locator"),
        "retrieved_at": _timestamp(item["retrieved_at"], "retrieved_at"),
        "publisher": _public_text(item["publisher"], "publisher", 256),
        "jurisdiction": _public_text(item["jurisdiction"], "jurisdiction", 128),
        "authority_role": role, "version": version,
        "effective_date": None if item["effective_date"] is None else _date(item["effective_date"], "effective_date"),
        "operational_question_ids": _ids(item["operational_question_ids"], "operational question ids", 1, 32),
        "relationship_edge_ids": _ids(item["relationship_edge_ids"], "relationship edge ids", 1, 2048),
        "status": item["status"], "media_type": item["media_type"],
        "retained_artifact_digests": _digests(item["retained_artifact_digests"], "artifact digests", 0, 3),
    }


def validate_source_registry(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "registry_id", "root_workflow_id", "sources", "registry_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "source registry")
    if body["schema_version"] != SCHEMA_VERSIONS[0]: raise ContractError("source registry version differs")
    sources = [_source(item) for item in _array(body["sources"], "sources", 1, 16)]
    _unique([item["source_id"] for item in sources], "source ids")
    result = {"schema_version": body["schema_version"], "registry_id": _identifier(body["registry_id"], "registry_id"),
              "root_workflow_id": _identifier(body["root_workflow_id"], "root_workflow_id"), "sources": sources,
              "registry_digest": body["registry_digest"], **_authority(body)}
    _self_digest(result, "registry_digest", "source registry")
    return result


def validate_acquisition_record(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "acquisition_id", "source_id", "requested_locator", "final_locator", "retrieved_at",
            "status", "http_status", "media_type", "byte_count", "content_digest", "redirect_chain", "record_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "acquisition record")
    if body["schema_version"] != SCHEMA_VERSIONS[1]: raise ContractError("acquisition record version differs")
    status = body["status"]
    if status not in {"acquired", "verified", "unavailable", "investigate"}: raise ContractError("acquisition status is invalid")
    final = None if body["final_locator"] is None else _locator(body["final_locator"], "final_locator")
    media = None if body["media_type"] is None else _text(body["media_type"], "media_type", 192)
    if media is not None and not _MEDIA.fullmatch(media): raise ContractError("media_type is invalid")
    content = None if body["content_digest"] is None else _digest(body["content_digest"], "content_digest")
    if status in {"acquired", "verified"} and (final is None or content is None or body["byte_count"] == 0):
        raise ContractError("successful acquisition lacks retained identity")
    result = {"schema_version": body["schema_version"], "acquisition_id": _identifier(body["acquisition_id"], "acquisition_id"),
              "source_id": _identifier(body["source_id"], "source_id"), "requested_locator": _locator(body["requested_locator"], "requested_locator"),
              "final_locator": final, "retrieved_at": _timestamp(body["retrieved_at"], "retrieved_at"), "status": status,
              "http_status": _integer(body["http_status"], "http_status", 100, 599), "media_type": media,
              "byte_count": _integer(body["byte_count"], "byte_count", 0, 16 * 1024 * 1024), "content_digest": content,
              "redirect_chain": [_locator(item, "redirect locator") for item in _array(body["redirect_chain"], "redirect_chain", 0, 8)],
              "record_digest": body["record_digest"], **_authority(body)}
    _self_digest(result, "record_digest", "acquisition record")
    return result


def validate_authority_role(value: Any) -> dict[str, Any]:
    body = _object(value, ("schema_version", "role", "precedence", "enacted_law", "may_override_roles", "role_digest"), "authority role")
    if body["schema_version"] != SCHEMA_VERSIONS[2] or body["role"] not in AUTHORITY_ROLES: raise ContractError("authority role is invalid")
    role = body["role"]
    result = {"schema_version": body["schema_version"], "role": role,
              "precedence": _integer(body["precedence"], "precedence", 0, 5),
              "enacted_law": body["enacted_law"], "may_override_roles": _array(body["may_override_roles"], "may_override_roles", 0, 5),
              "role_digest": body["role_digest"]}
    if type(result["enacted_law"]) is not bool or result["precedence"] != ROLE_PRECEDENCE[role]: raise ContractError("authority role widens precedence")
    overrides = _unique(result["may_override_roles"], "may_override_roles")
    if any(item not in AUTHORITY_ROLES for item in overrides) or overrides != list(AUTHORITY_ROLES[AUTHORITY_ROLES.index(role) + 1:]):
        raise ContractError("authority role widens override authority")
    if result["enacted_law"] is not (role == "primary_law"): raise ContractError("enacted law marker differs")
    _self_digest(result, "role_digest", "authority role")
    return result


def validate_relationship_edge(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "edge_id", "edge_type", "source_kind", "source_id", "source_evidence_digest",
            "target_kind", "target_id", "target_evidence_digest", "supporting_excerpt_digest", "reason_code", "qualification", "edge_digest")
    body = _object(value, keys, "relationship edge")
    if body["schema_version"] != SCHEMA_VERSIONS[3] or body["edge_type"] not in EDGE_TYPES: raise ContractError("relationship edge type is invalid")
    if body["source_kind"] not in {"source", "claim", "workflow"} or body["target_kind"] not in {"source", "claim", "workflow"}: raise ContractError("relationship endpoint kind is invalid")
    if body["reason_code"] not in EDGE_REASON_CODES: raise ContractError("relationship reason code is invalid")
    result = {"schema_version": body["schema_version"], "edge_id": _identifier(body["edge_id"], "edge_id"), "edge_type": body["edge_type"],
              "source_kind": body["source_kind"], "source_id": _identifier(body["source_id"], "source_id"), "source_evidence_digest": _digest(body["source_evidence_digest"], "source_evidence_digest"),
              "target_kind": body["target_kind"], "target_id": _identifier(body["target_id"], "target_id"), "target_evidence_digest": _digest(body["target_evidence_digest"], "target_evidence_digest"),
              "supporting_excerpt_digest": _digest(body["supporting_excerpt_digest"], "supporting_excerpt_digest"), "reason_code": body["reason_code"],
              "qualification": None if body["qualification"] is None else _public_text(body["qualification"], "qualification", 512), "edge_digest": body["edge_digest"]}
    _self_digest(result, "edge_digest", "relationship edge")
    return result


def validate_operational_question(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "question_id", "prompt", "expected_outcome", "required_authority_roles", "required_evidence_ids", "forbidden_evidence_ids", "qualifications", "question_digest")
    body = _object(value, keys, "operational question")
    if body["schema_version"] != SCHEMA_VERSIONS[4] or body["expected_outcome"] not in {"answer", "partial", "refuse", "investigate"}: raise ContractError("operational question is invalid")
    roles = _unique(_array(body["required_authority_roles"], "required authority roles", 0, 6), "required authority roles")
    if any(role not in AUTHORITY_ROLES for role in roles): raise ContractError("required authority role is invalid")
    result = {"schema_version": body["schema_version"], "question_id": _identifier(body["question_id"], "question_id"), "prompt": _public_text(body["prompt"], "prompt", 1024),
              "expected_outcome": body["expected_outcome"], "required_authority_roles": roles,
              "required_evidence_ids": _ids(body["required_evidence_ids"], "required evidence ids", 0, 64),
              "forbidden_evidence_ids": _ids(body["forbidden_evidence_ids"], "forbidden evidence ids", 0, 64),
              "qualifications": _unique([_public_text(item, "qualification", 512) for item in _array(body["qualifications"], "qualifications", 0, 16)], "qualifications"),
              "question_digest": body["question_digest"]}
    if set(result["required_evidence_ids"]) & set(result["forbidden_evidence_ids"]): raise ContractError("question evidence sets overlap")
    _self_digest(result, "question_digest", "operational question")
    return result


def _claim(value: Any) -> dict[str, Any]:
    keys = ("claim_id", "source_id", "source_digest", "authority_role", "excerpt", "excerpt_digest", "citation_anchor", "operational_question_ids", "subject_terms", "semantic_reason_codes")
    item = _object(value, keys, "claim")
    if item["authority_role"] not in AUTHORITY_ROLES: raise ContractError("claim authority role is invalid")
    excerpt = _public_text(item["excerpt"], "excerpt", 4096)
    if canonical_evidence_digest(excerpt, "excerpt") != item["excerpt_digest"]: raise ContractError("claim excerpt digest differs")
    reasons = _unique(_array(item["semantic_reason_codes"], "semantic reason codes", 1, 8), "semantic reason codes")
    if any(reason not in SEMANTIC_REASON_CODES for reason in reasons): raise ContractError("semantic reason code is invalid")
    return {"claim_id": _identifier(item["claim_id"], "claim_id"), "source_id": _identifier(item["source_id"], "source_id"),
            "source_digest": _digest(item["source_digest"], "source_digest"), "authority_role": item["authority_role"], "excerpt": excerpt,
            "excerpt_digest": _digest(item["excerpt_digest"], "excerpt_digest"), "citation_anchor": _public_text(item["citation_anchor"], "citation_anchor", 256),
            "operational_question_ids": _ids(item["operational_question_ids"], "operational question ids", 1, 32),
            "subject_terms": _unique([_public_text(term, "subject term", 64) for term in _array(item["subject_terms"], "subject_terms", 1, 8)], "subject terms"),
            "semantic_reason_codes": reasons}


def _validate_graph_relationships(
    *, root_workflow_id: str, root_workflow_digest: str,
    sources: list[dict[str, Any]], claims: list[dict[str, Any]],
    edges: list[dict[str, Any]], questions: list[dict[str, Any]],
) -> None:
    """Validate graph-wide identity, authority, and reachability bindings."""

    source_by = {item["source_id"]: item for item in sources}
    claim_by = {item["claim_id"]: item for item in claims}
    question_ids = {item["question_id"] for item in questions}
    edge_ids = {item["edge_id"] for item in edges}
    excerpt_digests = {item["excerpt_digest"] for item in claims}
    endpoints = {
        "source": {item["source_id"]: item["source_digest"] for item in sources},
        "claim": {item["claim_id"]: item["excerpt_digest"] for item in claims},
        "workflow": {root_workflow_id: root_workflow_digest},
    }

    for source in sources:
        if not set(source["operational_question_ids"]).issubset(question_ids):
            raise ValueError("source question binding differs")
    for claim in claims:
        source = source_by.get(claim["source_id"])
        if source is None or source["source_digest"] != claim["source_digest"]:
            raise ValueError("claim source binding differs")
        if source["authority_role"] != claim["authority_role"]:
            raise ValueError("claim authority differs")
        if not set(claim["operational_question_ids"]).issubset(question_ids):
            raise ValueError("claim question binding differs")

    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    incident_by_source = {source_id: set() for source_id in source_by}
    for edge in edges:
        source_kind = edge["source_kind"]
        target_kind = edge["target_kind"]
        if (edge["source_id"] not in endpoints[source_kind]
                or edge["target_id"] not in endpoints[target_kind]):
            raise ValueError("edge endpoint differs")
        if (endpoints[source_kind][edge["source_id"]] != edge["source_evidence_digest"]
                or endpoints[target_kind][edge["target_id"]] != edge["target_evidence_digest"]):
            raise ValueError("edge endpoint digest differs")
        if edge["supporting_excerpt_digest"] not in excerpt_digests:
            raise ValueError("supporting excerpt differs")

        source_role = (
            source_by[edge["source_id"]]["authority_role"]
            if source_kind == "source"
            else claim_by[edge["source_id"]]["authority_role"]
            if source_kind == "claim"
            else None
        )
        if edge["edge_type"] == "defines" and source_role != "primary_law":
            raise ValueError("authority cannot define law")
        if edge["edge_type"] == "supersedes":
            if source_kind != "source" or target_kind != "source":
                raise ValueError("unsupported supersession")
            left = source_by[edge["source_id"]]
            right = source_by[edge["target_id"]]
            if (left["authority_role"] != right["authority_role"]
                    or left["publisher"] != right["publisher"]
                    or not left["version"] or not right["version"]):
                raise ValueError("unsupported supersession")

        source_node = (source_kind, edge["source_id"])
        target_node = (target_kind, edge["target_id"])
        adjacency.setdefault(source_node, set()).add(target_node)
        adjacency.setdefault(target_node, set()).add(source_node)
        related_sources = set()
        if source_kind == "source":
            related_sources.add(edge["source_id"])
        elif source_kind == "claim":
            related_sources.add(claim_by[edge["source_id"]]["source_id"])
        if target_kind == "source":
            related_sources.add(edge["target_id"])
        elif target_kind == "claim":
            related_sources.add(claim_by[edge["target_id"]]["source_id"])
        for source_id in related_sources:
            incident_by_source[source_id].add(edge["edge_id"])

    for source in sources:
        declared = set(source["relationship_edge_ids"])
        if not declared.issubset(edge_ids) or declared != incident_by_source[source["source_id"]]:
            raise ValueError("source edge binding differs")

    evidence_ids = set(source_by) | set(claim_by) | edge_ids
    for question in questions:
        if (not set(question["required_evidence_ids"]).issubset(evidence_ids)
                or not set(question["forbidden_evidence_ids"]).issubset(evidence_ids)):
            raise ValueError("question evidence binding differs")
        available_roles = {source["authority_role"] for source in sources}
        if (question["expected_outcome"] in {"answer", "partial"}
                and not set(question["required_authority_roles"]).issubset(available_roles)):
            raise ValueError("question authority binding differs")

    root = ("workflow", root_workflow_id)
    seen = {root}
    pending = [root]
    while pending:
        node = pending.pop()
        for next_node in adjacency.get(node, ()):
            if next_node not in seen:
                seen.add(next_node)
                pending.append(next_node)
    if (any(("source", source_id) not in seen for source_id in source_by)
            or any(("claim", claim_id) not in seen for claim_id in claim_by)):
        raise ValueError("orphan evidence")


def validate_graph_manifest(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "graph_id", "root_workflow_id", "root_workflow_digest", "source_registry_digest", "sources", "claims", "edges", "operational_questions", "graph_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "graph manifest")
    if body["schema_version"] != SCHEMA_VERSIONS[5]: raise ContractError("graph manifest version differs")
    sources = [_source(item) for item in _array(body["sources"], "sources", 1, 16)]
    claims = [_claim(item) for item in _array(body["claims"], "claims", 1, 512)]
    edges = [validate_relationship_edge(item) for item in _array(body["edges"], "edges", 1, 2048)]
    questions = [validate_operational_question(item) for item in _array(body["operational_questions"], "operational questions", 1, 32)]
    for label, values in (("source ids", [x["source_id"] for x in sources]), ("claim ids", [x["claim_id"] for x in claims]), ("edge ids", [x["edge_id"] for x in edges]), ("question ids", [x["question_id"] for x in questions])): _unique(values, label)
    result = {"schema_version": body["schema_version"], "graph_id": _identifier(body["graph_id"], "graph_id"), "root_workflow_id": _identifier(body["root_workflow_id"], "root_workflow_id"),
              "root_workflow_digest": _digest(body["root_workflow_digest"], "root_workflow_digest"), "source_registry_digest": _digest(body["source_registry_digest"], "source_registry_digest"),
              "sources": sources, "claims": claims, "edges": edges, "operational_questions": questions, "graph_digest": body["graph_digest"], **_authority(body)}
    _self_digest(result, "graph_digest", "graph manifest")
    try:
        _validate_graph_relationships(
            root_workflow_id=result["root_workflow_id"],
            root_workflow_digest=result["root_workflow_digest"],
            sources=sources, claims=claims, edges=edges, questions=questions,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("graph manifest relationships are invalid") from exc
    return result


def validate_graph_inspection(value: Any) -> dict[str, Any]:
    """Validate inspection shape only; product use requires manifest binding below."""

    keys = ("schema_version", "graph_id", "graph_digest", "result", "source_count", "claim_count", "edge_count", "question_count", "orphan_source_count", "orphan_claim_count", "conflict_count", "stale_source_count", "inspection_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "graph inspection")
    if body["schema_version"] != SCHEMA_VERSIONS[6] or body["result"] not in {"pass", "investigate", "refuse"}: raise ContractError("graph inspection is invalid")
    result = {"schema_version": body["schema_version"], "graph_id": _identifier(body["graph_id"], "graph_id"), "graph_digest": _digest(body["graph_digest"], "graph_digest"), "result": body["result"],
              **{field: _integer(body[field], field, 0, maximum) for field, maximum in (("source_count", 16), ("claim_count", 512), ("edge_count", 2048), ("question_count", 32), ("orphan_source_count", 16), ("orphan_claim_count", 512), ("conflict_count", 2048), ("stale_source_count", 16))},
              "inspection_digest": body["inspection_digest"], **_authority(body)}
    if result["result"] == "pass" and (result["orphan_source_count"] or result["orphan_claim_count"] or result["conflict_count"] or result["stale_source_count"]): raise ContractError("passing inspection hides unresolved evidence")
    _self_digest(result, "inspection_digest", "graph inspection")
    return result


def validate_graph_inspection_against_manifest(
    value: Any,
    manifest: Any,
    *,
    expected_orphan_source_count: int,
    expected_orphan_claim_count: int,
) -> dict[str, Any]:
    """Validate an inspection against independently validated graph context."""

    graph = validate_graph_manifest(manifest)
    inspection = validate_graph_inspection(value)
    expected_source_orphans = _integer(
        expected_orphan_source_count, "expected_orphan_source_count", 0, 16
    )
    expected_claim_orphans = _integer(
        expected_orphan_claim_count, "expected_orphan_claim_count", 0, 512
    )
    expected = {
        "graph_id": graph["graph_id"],
        "graph_digest": graph["graph_digest"],
        "source_count": len(graph["sources"]),
        "claim_count": len(graph["claims"]),
        "edge_count": len(graph["edges"]),
        "question_count": len(graph["operational_questions"]),
        "orphan_source_count": expected_source_orphans,
        "orphan_claim_count": expected_claim_orphans,
    }
    if any(inspection[field] != expected_value for field, expected_value in expected.items()):
        raise ContractError("graph inspection context binding differs")
    if inspection["result"] == "pass" and (
        expected_source_orphans != 0 or expected_claim_orphans != 0
    ):
        raise ContractError("passing inspection has contextual orphans")
    return inspection


def validate_query_readback(value: Any) -> dict[str, Any]:
    """Validate query-readback shape only; product use requires graph binding below."""

    keys = ("schema_version", "query_id", "prompt_digest", "graph_digest", "outcome", "evidence_ids", "authority_roles", "qualifications", "readback_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "query readback")
    if body["schema_version"] != SCHEMA_VERSIONS[7] or body["outcome"] not in {"answer", "partial", "refuse", "investigate"}: raise ContractError("query readback is invalid")
    roles = _unique(_array(body["authority_roles"], "authority_roles", 0, 6), "authority_roles")
    if any(role not in AUTHORITY_ROLES for role in roles): raise ContractError("query authority role is invalid")
    result = {"schema_version": body["schema_version"], "query_id": _identifier(body["query_id"], "query_id"), "prompt_digest": _digest(body["prompt_digest"], "prompt_digest"), "graph_digest": _digest(body["graph_digest"], "graph_digest"),
              "outcome": body["outcome"], "evidence_ids": _ids(body["evidence_ids"], "evidence_ids", 0, 64), "authority_roles": roles,
              "qualifications": _unique([_public_text(item, "qualification", 512) for item in _array(body["qualifications"], "qualifications", 0, 16)], "qualifications"),
              "readback_digest": body["readback_digest"], **_authority(body)}
    if result["outcome"] in {"answer", "partial"} and not result["evidence_ids"]: raise ContractError("answer lacks evidence")
    _self_digest(result, "readback_digest", "query readback")
    return result


def validate_query_readback_against_manifest(
    value: Any,
    manifest: Any,
    *,
    allowed_evidence_ids: set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """Validate a readback against one independently validated graph manifest."""

    graph = validate_graph_manifest(manifest)
    readback = validate_query_readback(value)
    if readback["graph_digest"] != graph["graph_digest"]:
        raise ContractError("query readback graph binding differs")
    graph_evidence_ids = {
        *(item["source_id"] for item in graph["sources"]),
        *(item["claim_id"] for item in graph["claims"]),
        *(item["edge_id"] for item in graph["edges"]),
    }
    if allowed_evidence_ids is None:
        allowed = graph_evidence_ids
    else:
        if type(allowed_evidence_ids) not in {set, frozenset}:
            raise ContractError("allowed evidence identities must be an exact set")
        allowed = {_identifier(item, "allowed evidence identity") for item in allowed_evidence_ids}
        if not allowed.issubset(graph_evidence_ids):
            raise ContractError("allowed evidence identity is absent from graph")
    if not set(readback["evidence_ids"]).issubset(allowed):
        raise ContractError("query readback contains unknown evidence identities")
    return readback


def validate_campaign_summary(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "campaign_id", "correlation_id", "registry_digest", "graph_digest", "inspection_digest", "uat_digest", "source_count", "claim_count", "edge_count", "question_count", "passed_question_count", "refused_question_count", "investigate_question_count", "zero_orphans", "summary_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "campaign summary")
    if body["schema_version"] != SCHEMA_VERSIONS[8]: raise ContractError("campaign summary version differs")
    result = {"schema_version": body["schema_version"], "campaign_id": _identifier(body["campaign_id"], "campaign_id"), "correlation_id": _identifier(body["correlation_id"], "correlation_id"),
              **{field: _digest(body[field], field) for field in ("registry_digest", "graph_digest", "inspection_digest", "uat_digest")},
              **{field: _integer(body[field], field, 0, maximum) for field, maximum in (("source_count", 16), ("claim_count", 512), ("edge_count", 2048), ("question_count", 32), ("passed_question_count", 32), ("refused_question_count", 32), ("investigate_question_count", 32))},
              "zero_orphans": body["zero_orphans"], "summary_digest": body["summary_digest"], **_authority(body)}
    if result["zero_orphans"] is not True or result["passed_question_count"] + result["refused_question_count"] + result["investigate_question_count"] != result["question_count"]: raise ContractError("campaign summary counts differ")
    _self_digest(result, "summary_digest", "campaign summary")
    return result


def validate_recovery(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "attempt_id", "phase", "status", "intended_digest", "staged_digest", "destination_digest", "classification", "recovery_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "evidence recovery")
    if body["schema_version"] != SCHEMA_VERSIONS[9] or body["phase"] not in {"acquisition", "graph_publication", "freshness"} or body["status"] not in {"staged", "interrupted", "completed", "conflict"} or body["classification"] not in {"no_op", "resume", "complete", "investigate"}: raise ContractError("evidence recovery is invalid")
    result = {"schema_version": body["schema_version"], "attempt_id": _identifier(body["attempt_id"], "attempt_id"), "phase": body["phase"], "status": body["status"], "intended_digest": _digest(body["intended_digest"], "intended_digest"),
              "staged_digest": None if body["staged_digest"] is None else _digest(body["staged_digest"], "staged_digest"), "destination_digest": None if body["destination_digest"] is None else _digest(body["destination_digest"], "destination_digest"),
              "classification": body["classification"], "recovery_digest": body["recovery_digest"], **_authority(body)}
    if result["classification"] == "complete" and result["destination_digest"] != result["intended_digest"]: raise ContractError("completed recovery digest differs")
    _self_digest(result, "recovery_digest", "evidence recovery")
    return result


def _media_type(value: Any, label: str) -> str:
    result = _text(value, label, 192)
    if not _MEDIA.fullmatch(result):
        raise ContractError(f"{label} is invalid")
    return result


def _freshness_locator(value: Any, label: str) -> str:
    result = _locator(value, label)
    parsed = urlsplit(result)
    expected_netloc = parsed.hostname if parsed.port is None else f"{parsed.hostname}:443"
    if not result.startswith("https://") or parsed.netloc != expected_netloc or not parsed.path.startswith("/"):
        raise ContractError(f"{label} is not an exact official locator")
    return result


def _freshness_source(value: Any) -> dict[str, Any]:
    keys = ("source_id", "canonical_locator", "prior_record_digest", "prior_content_digest",
            "prior_media_type", "declared_successor_locator")
    item = _object(value, keys, "freshness policy source")
    return {
        "source_id": _identifier(item["source_id"], "source_id"),
        "canonical_locator": _freshness_locator(item["canonical_locator"], "canonical_locator"),
        "prior_record_digest": _digest(item["prior_record_digest"], "prior_record_digest"),
        "prior_content_digest": _digest(item["prior_content_digest"], "prior_content_digest"),
        "prior_media_type": _media_type(item["prior_media_type"], "prior_media_type"),
        "declared_successor_locator": None if item["declared_successor_locator"] is None else
            _freshness_locator(item["declared_successor_locator"], "declared_successor_locator"),
    }


def validate_freshness_policy(value: Any) -> dict[str, Any]:
    keys = ("schema_version", "policy_id", "bundle_id", "graph_id", "graph_digest",
            "source_registry_digest", "verification_interval_seconds",
            "maximum_observation_age_seconds", "maximum_observation_bytes",
            "maximum_redirect_hops", "allowed_media_types",
            "allowed_terminal_classifications", "sources", "policy_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "freshness policy")
    if body["schema_version"] != SCHEMA_VERSIONS[10]:
        raise ContractError("freshness policy version differs")
    media_types = _unique([
        _media_type(item, "allowed media type")
        for item in _array(body["allowed_media_types"], "allowed_media_types", 1, 8)
    ], "allowed media types")
    terminal_classifications = _unique([
        item for item in _array(
            body["allowed_terminal_classifications"],
            "allowed_terminal_classifications",
            1,
            len(FRESHNESS_CLASSIFICATIONS),
        )
    ], "allowed terminal classifications")
    if any(item not in FRESHNESS_CLASSIFICATIONS for item in terminal_classifications):
        raise ContractError("freshness terminal classification is invalid")
    sources = [_freshness_source(item) for item in _array(body["sources"], "sources", 1, 16)]
    _unique([item["source_id"] for item in sources], "freshness policy source ids")
    _unique([item["canonical_locator"] for item in sources], "freshness policy locators")
    if any(item["prior_media_type"] not in media_types for item in sources):
        raise ContractError("freshness policy source media type is not allowed")
    result = {
        "schema_version": body["schema_version"],
        "policy_id": _identifier(body["policy_id"], "policy_id"),
        "bundle_id": _identifier(body["bundle_id"], "bundle_id"),
        "graph_id": _identifier(body["graph_id"], "graph_id"),
        "graph_digest": _digest(body["graph_digest"], "graph_digest"),
        "source_registry_digest": _digest(body["source_registry_digest"], "source_registry_digest"),
        "verification_interval_seconds": _integer(
            body["verification_interval_seconds"],
            "verification_interval_seconds",
            1,
            31536000,
        ),
        "maximum_observation_age_seconds": _integer(
            body["maximum_observation_age_seconds"],
            "maximum_observation_age_seconds",
            1,
            31536000,
        ),
        "maximum_observation_bytes": _integer(body["maximum_observation_bytes"], "maximum_observation_bytes", 1, 16 * 1024 * 1024),
        "maximum_redirect_hops": _integer(body["maximum_redirect_hops"], "maximum_redirect_hops", 0, 8),
        "allowed_media_types": media_types,
        "allowed_terminal_classifications": terminal_classifications,
        "sources": sources,
        "policy_digest": body["policy_digest"], **_authority(body),
    }
    _self_digest(result, "policy_digest", "freshness policy")
    return result


def validate_freshness_observation(value: Any, *, policy: Any | None = None) -> dict[str, Any]:
    keys = ("schema_version", "observation_id", "policy_id", "bundle_id", "graph_id",
            "graph_digest", "source_id", "prior_record_digest", "requested_locator",
            "final_locator", "observed_at", "status", "http_status", "media_type",
            "byte_count", "content_digest", "redirect_chain", "version", "effective_date",
            "observation_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "freshness observation")
    if body["schema_version"] != SCHEMA_VERSIONS[11] or body["status"] not in {"observed", "unavailable", "investigate"}:
        raise ContractError("freshness observation is invalid")
    result = {
        "schema_version": body["schema_version"],
        **{field: _identifier(body[field], field) for field in ("observation_id", "policy_id", "bundle_id", "graph_id", "source_id")},
        "graph_digest": _digest(body["graph_digest"], "graph_digest"),
        "prior_record_digest": _digest(body["prior_record_digest"], "prior_record_digest"),
        "requested_locator": _freshness_locator(body["requested_locator"], "requested_locator"),
        "final_locator": None if body["final_locator"] is None else _freshness_locator(body["final_locator"], "final_locator"),
        "observed_at": _timestamp(body["observed_at"], "observed_at"), "status": body["status"],
        "http_status": _integer(body["http_status"], "http_status", 100, 599),
        "media_type": None if body["media_type"] is None else _media_type(body["media_type"], "media_type"),
        "byte_count": _integer(body["byte_count"], "byte_count", 0, 16 * 1024 * 1024),
        "content_digest": None if body["content_digest"] is None else _digest(body["content_digest"], "content_digest"),
        "redirect_chain": [_freshness_locator(item, "redirect locator") for item in _array(body["redirect_chain"], "redirect_chain", 0, 8)],
        "version": None if body["version"] is None else _public_text(body["version"], "version", 128),
        "effective_date": None if body["effective_date"] is None else _date(body["effective_date"], "effective_date"),
        "observation_digest": body["observation_digest"], **_authority(body),
    }
    if result["status"] == "observed" and (result["final_locator"] is None or result["media_type"] is None or result["content_digest"] is None or result["byte_count"] == 0):
        raise ContractError("successful freshness observation lacks retained identity")
    if result["status"] != "observed" and result["content_digest"] is not None:
        raise ContractError("unsuccessful freshness observation claims content")
    if policy is not None:
        expected = validate_freshness_policy(policy)
        source_matches = [item for item in expected["sources"] if item["source_id"] == result["source_id"]]
        if len(source_matches) != 1 or any(result[field] != expected[field] for field in ("policy_id", "bundle_id", "graph_id", "graph_digest")):
            raise ContractError("freshness observation policy binding differs")
        source = source_matches[0]
        if result["prior_record_digest"] != source["prior_record_digest"] or result["requested_locator"] != source["canonical_locator"]:
            raise ContractError("freshness observation source binding differs")
        if result["byte_count"] > expected["maximum_observation_bytes"] or len(result["redirect_chain"]) > expected["maximum_redirect_hops"]:
            raise ContractError("freshness observation exceeds policy bounds")
        if result["media_type"] is not None and result["media_type"] not in expected["allowed_media_types"]:
            raise ContractError("freshness observation media type is not allowed")
    _self_digest(result, "observation_digest", "freshness observation")
    return result


def _freshness_reasons(value: Any) -> list[str]:
    reasons = _unique(_array(value, "reason_codes", 1, 8), "reason_codes")
    if any(reason not in FRESHNESS_REASON_CODES for reason in reasons):
        raise ContractError("freshness reason code is invalid")
    return reasons


def _validate_classification(classification: Any, reasons: list[str], *, prior_digest: str | None = None,
                             observed_digest: str | None = None, successor: str | None = None,
                             observed_locator: str | None = None,
                             prior_locator: str | None = None,
                             prior_media_type: str | None = None,
                             observed_media_type: str | None = None,
                             observed_version: str | None = None,
                             observed_effective_date: str | None = None,
                             check_observed_identity: bool = False) -> str:
    if classification not in FRESHNESS_CLASSIFICATIONS:
        raise ContractError("freshness classification is invalid")
    required = {
        "unchanged": "content_unchanged", "updated": "content_changed",
        "superseded": "declared_successor_verified", "unavailable": "terminal_unavailable",
    }
    if classification in required and required[classification] not in reasons:
        raise ContractError("freshness classification reason differs")
    if classification == "investigate" and not any(reason not in required.values() for reason in reasons):
        raise ContractError("investigate classification lacks investigation reason")
    if check_observed_identity and classification == "unchanged" and prior_digest != observed_digest:
        raise ContractError("unchanged freshness content differs")
    if check_observed_identity and classification == "unchanged" and prior_locator != observed_locator:
        raise ContractError("unchanged freshness locator differs")
    if check_observed_identity and classification == "unchanged" and prior_media_type != observed_media_type:
        raise ContractError("unchanged freshness media type differs")
    if check_observed_identity and classification == "updated" and (observed_digest is None or prior_digest == observed_digest):
        raise ContractError("updated freshness content does not differ")
    if check_observed_identity and classification == "superseded" and (successor is None or successor != observed_locator):
        raise ContractError("superseded freshness locator differs")
    if check_observed_identity and classification == "superseded" and (
        observed_version is None or observed_effective_date is None
    ):
        raise ContractError("superseded freshness comparison lacks version evidence")
    if classification == "superseded" and set(reasons) & {
        "version_ambiguous", "effective_date_ambiguous",
    }:
        raise ContractError("superseded freshness comparison has ambiguous version evidence")
    if check_observed_identity and classification == "unavailable" and observed_digest is not None:
        raise ContractError("unavailable freshness comparison has content")
    return classification


def validate_freshness_comparison(value: Any, *, policy: Any | None = None,
                                  observation: Any | None = None) -> dict[str, Any]:
    keys = ("schema_version", "comparison_id", "policy_id", "bundle_id", "graph_id",
            "graph_digest", "source_id", "prior_record_digest", "observation_id",
            "observation_digest", "classification", "reason_codes", "prior_content_digest",
            "observed_content_digest", "prior_locator", "observed_locator", "prior_media_type",
            "observed_media_type", "observed_version", "observed_effective_date",
            "declared_successor_locator", "qualification",
            "comparison_digest") + AUTHORITY_FIELDS
    body = _object(value, keys, "freshness comparison")
    if body["schema_version"] != SCHEMA_VERSIONS[12]:
        raise ContractError("freshness comparison version differs")
    reasons = _freshness_reasons(body["reason_codes"])
    nullable_digest = lambda field: None if body[field] is None else _digest(body[field], field)
    nullable_locator = lambda field: None if body[field] is None else _freshness_locator(body[field], field)
    nullable_media = lambda field: None if body[field] is None else _media_type(body[field], field)
    result = {
        "schema_version": body["schema_version"],
        **{field: _identifier(body[field], field) for field in ("comparison_id", "policy_id", "bundle_id", "graph_id", "source_id", "observation_id")},
        "graph_digest": _digest(body["graph_digest"], "graph_digest"),
        "prior_record_digest": _digest(body["prior_record_digest"], "prior_record_digest"),
        "observation_digest": _digest(body["observation_digest"], "observation_digest"),
        "classification": body["classification"], "reason_codes": reasons,
        "prior_content_digest": _digest(body["prior_content_digest"], "prior_content_digest"),
        "observed_content_digest": nullable_digest("observed_content_digest"),
        "prior_locator": _freshness_locator(body["prior_locator"], "prior_locator"),
        "observed_locator": nullable_locator("observed_locator"),
        "prior_media_type": _media_type(body["prior_media_type"], "prior_media_type"),
        "observed_media_type": nullable_media("observed_media_type"),
        "observed_version": None if body["observed_version"] is None else
            _public_text(body["observed_version"], "observed_version", 128),
        "observed_effective_date": None if body["observed_effective_date"] is None else
            _date(body["observed_effective_date"], "observed_effective_date"),
        "declared_successor_locator": nullable_locator("declared_successor_locator"),
        "qualification": None if body["qualification"] is None else _public_text(body["qualification"], "qualification", 512),
        "comparison_digest": body["comparison_digest"], **_authority(body),
    }
    result["classification"] = _validate_classification(
        result["classification"], reasons, prior_digest=result["prior_content_digest"],
        observed_digest=result["observed_content_digest"], successor=result["declared_successor_locator"],
        observed_locator=result["observed_locator"], prior_locator=result["prior_locator"],
        prior_media_type=result["prior_media_type"], observed_media_type=result["observed_media_type"],
        observed_version=result["observed_version"],
        observed_effective_date=result["observed_effective_date"], check_observed_identity=True,
    )
    expected_policy = validate_freshness_policy(policy) if policy is not None else None
    expected_observation = validate_freshness_observation(observation, policy=expected_policy) if observation is not None else None
    if expected_observation is not None:
        bindings = ("policy_id", "bundle_id", "graph_id", "graph_digest", "source_id", "prior_record_digest")
        if any(result[field] != expected_observation[field] for field in bindings) or result["observation_id"] != expected_observation["observation_id"] or result["observation_digest"] != expected_observation["observation_digest"]:
            raise ContractError("freshness comparison observation binding differs")
        if any(result[field] != expected_observation[observation_field] for field, observation_field in (
            ("observed_content_digest", "content_digest"), ("observed_locator", "final_locator"),
            ("observed_media_type", "media_type"), ("observed_version", "version"),
            ("observed_effective_date", "effective_date"),
        )):
            raise ContractError("freshness comparison observed identity differs")
    if expected_policy is not None:
        sources = [item for item in expected_policy["sources"] if item["source_id"] == result["source_id"]]
        if len(sources) != 1:
            raise ContractError("freshness comparison source is absent from policy")
        source = sources[0]
        for field in ("prior_record_digest", "prior_content_digest", "prior_media_type", "declared_successor_locator"):
            if result[field] != source[field]:
                raise ContractError("freshness comparison prior binding differs")
        if result["prior_locator"] != source["canonical_locator"]:
            raise ContractError("freshness comparison prior locator differs")
    _self_digest(result, "comparison_digest", "freshness comparison")
    return result


def _freshness_result(value: Any) -> dict[str, Any]:
    keys = ("source_id", "prior_record_digest", "observation_id", "observation_digest",
            "comparison_id", "comparison_digest", "classification", "reason_codes")
    item = _object(value, keys, "freshness summary result")
    reasons = _freshness_reasons(item["reason_codes"])
    classification = _validate_classification(item["classification"], reasons)
    return {
        "source_id": _identifier(item["source_id"], "source_id"),
        "prior_record_digest": _digest(item["prior_record_digest"], "prior_record_digest"),
        "observation_id": _identifier(item["observation_id"], "observation_id"),
        "observation_digest": _digest(item["observation_digest"], "observation_digest"),
        "comparison_id": _identifier(item["comparison_id"], "comparison_id"),
        "comparison_digest": _digest(item["comparison_digest"], "comparison_digest"),
        "classification": classification, "reason_codes": reasons,
    }


def validate_freshness_summary(value: Any, *, policy: Any | None = None,
                               observations: Any | None = None,
                               comparisons: Any | None = None) -> dict[str, Any]:
    count_fields = tuple(f"{item}_count" for item in FRESHNESS_CLASSIFICATIONS)
    keys = ("schema_version", "summary_id", "policy_id", "policy_digest", "bundle_id",
            "graph_id", "graph_digest", "observed_at", "results") + count_fields + (
                "rebuild_required", "summary_digest",
            ) + AUTHORITY_FIELDS
    body = _object(value, keys, "freshness summary")
    if body["schema_version"] != SCHEMA_VERSIONS[13]:
        raise ContractError("freshness summary version differs")
    results = [_freshness_result(item) for item in _array(body["results"], "results", 1, 16)]
    for field in ("source_id", "observation_id", "observation_digest", "comparison_id", "comparison_digest"):
        _unique([item[field] for item in results], f"freshness summary {field} values")
    result = {
        "schema_version": body["schema_version"],
        **{field: _identifier(body[field], field) for field in ("summary_id", "policy_id", "bundle_id", "graph_id")},
        "policy_digest": _digest(body["policy_digest"], "policy_digest"),
        "graph_digest": _digest(body["graph_digest"], "graph_digest"),
        "observed_at": _timestamp(body["observed_at"], "observed_at"), "results": results,
        **{field: _integer(body[field], field, 0, 16) for field in count_fields},
        "rebuild_required": body["rebuild_required"],
        "summary_digest": body["summary_digest"], **_authority(body),
    }
    if type(result["rebuild_required"]) is not bool or result["rebuild_required"] is not any(
        item["classification"] in {"updated", "superseded"} for item in results
    ):
        raise ContractError("freshness summary rebuild requirement differs")
    for classification in FRESHNESS_CLASSIFICATIONS:
        if result[f"{classification}_count"] != sum(item["classification"] == classification for item in results):
            raise ContractError("freshness summary counts differ")
    expected_policy = validate_freshness_policy(policy) if policy is not None else None
    if expected_policy is not None and any(result[field] != expected_policy[field] for field in ("policy_id", "policy_digest", "bundle_id", "graph_id", "graph_digest")):
        raise ContractError("freshness summary policy binding differs")
    if expected_policy is not None:
        policy_sources = {
            item["source_id"]: item["prior_record_digest"] for item in expected_policy["sources"]
        }
        result_sources = {item["source_id"]: item["prior_record_digest"] for item in results}
        if result_sources != policy_sources:
            raise ContractError("freshness summary policy source coverage differs")
    if observations is not None or comparisons is not None:
        if type(observations) is not list or type(comparisons) is not list:
            raise ContractError("freshness summary context must be exact arrays")
        observed_items = [validate_freshness_observation(item, policy=expected_policy) for item in observations]
        observed = {item["observation_id"]: item for item in observed_items}
        comparison_items = []
        for item in comparisons:
            if type(item) is not dict:
                raise ContractError("freshness comparison must be an object")
            comparison_items.append(validate_freshness_comparison(
                item, policy=expected_policy, observation=observed.get(item.get("observation_id"))
            ))
        compared = {item["comparison_id"]: item for item in comparison_items}
        if len(observed) != len(observations) or len(compared) != len(comparisons) or len(results) != len(observed) or len(results) != len(compared):
            raise ContractError("freshness summary context identities differ")
        for item in results:
            observation_item = observed.get(item["observation_id"])
            comparison_item = compared.get(item["comparison_id"])
            if observation_item is None or comparison_item is None or any(item[field] != comparison_item[field] for field in ("source_id", "prior_record_digest", "observation_id", "observation_digest", "comparison_id", "comparison_digest", "classification", "reason_codes")):
                raise ContractError("freshness summary result binding differs")
    _self_digest(result, "summary_digest", "freshness summary")
    return result


def _freshness_summary_context_digest(summary: dict[str, Any]) -> str:
    return canonical_digest({
        "policy_digest": summary["policy_digest"],
        "graph_digest": summary["graph_digest"],
        "results": [{field: item[field] for field in (
            "source_id", "prior_record_digest", "observation_digest",
            "comparison_digest", "classification", "reason_codes",
        )} for item in summary["results"]],
    })


def _freshness_summary_capability() -> tuple[type[dict], Any, Any]:
    seal = object()
    metadata: dict[int, tuple[weakref.ReferenceType[Any], str, str, str]] = {}

    class ValidatedFreshnessSummary(dict):
        """Dict-compatible capability whose trust metadata is held out of band."""

        __slots__ = ("__weakref__",)

        def __init__(self, value: dict[str, Any], *, capability_seal: object) -> None:
            if capability_seal is not seal:
                raise TypeError("validated freshness summaries are internally constructed")
            super().__init__(copy.deepcopy(value))

        def __deepcopy__(self, memo: dict[int, Any]) -> "ValidatedFreshnessSummary":
            current = metadata.get(id(self))
            if current is None or current[0]() is not self:
                raise ContractError("freshness summary lacks validated context")
            clone = mint(copy.deepcopy(dict(self), memo), *current[1:])
            memo[id(self)] = clone
            return clone

    def mint(value: dict[str, Any], summary_digest: str,
             graph_digest: str, context_digest: str) -> ValidatedFreshnessSummary:
        result = ValidatedFreshnessSummary(value, capability_seal=seal)
        identity = id(result)

        def discard(reference: weakref.ReferenceType[Any]) -> None:
            current = metadata.get(identity)
            if current is not None and current[0] is reference:
                metadata.pop(identity, None)

        reference = weakref.ref(result, discard)
        metadata[identity] = (reference, summary_digest, graph_digest, context_digest)
        return result

    def matches(value: Any, summary: dict[str, Any], graph_digest: str) -> bool:
        current = metadata.get(id(value))
        if current is None or current[0]() is not value:
            return False
        return (
            hmac.compare_digest(current[1], summary["summary_digest"])
            and hmac.compare_digest(current[2], graph_digest)
            and hmac.compare_digest(
                current[3], _freshness_summary_context_digest(summary),
            )
        )

    return ValidatedFreshnessSummary, mint, matches


(_ValidatedFreshnessSummary, _mint_validated_freshness_summary,
 _matches_validated_freshness_summary) = _freshness_summary_capability()


def validate_freshness_summary_against_manifest(
    summary: Any,
    graph: Any,
    *,
    policy: Any | None = None,
    observations: Any | None = None,
    comparisons: Any | None = None,
) -> dict[str, Any]:
    """Validate one detached freshness summary against its complete graph context."""

    if policy is None or type(observations) is not list or type(comparisons) is not list:
        raise ContractError("freshness summary requires complete validated context")
    expected_graph = validate_graph_manifest(graph)
    expected_policy = validate_freshness_policy(policy)
    candidate = dict(summary) if type(summary) is _ValidatedFreshnessSummary else summary
    result = validate_freshness_summary(
        candidate, policy=expected_policy,
        observations=observations, comparisons=comparisons,
    )
    if any(result[field] != expected_graph[field] for field in ("graph_id", "graph_digest")):
        raise ContractError("freshness summary graph binding differs")

    graph_sources = {item["source_id"]: item for item in expected_graph["sources"]}
    result_sources = {item["source_id"] for item in result["results"]}
    if result_sources != set(graph_sources) or len(result["results"]) != len(graph_sources):
        raise ContractError("freshness summary graph source coverage differs")

    if expected_policy["source_registry_digest"] != expected_graph["source_registry_digest"]:
        raise ContractError("freshness policy graph registry binding differs")
    policy_sources = {item["source_id"]: item for item in expected_policy["sources"]}
    if set(policy_sources) != set(graph_sources):
        raise ContractError("freshness policy graph source coverage differs")
    for source_id, baseline in policy_sources.items():
        source = graph_sources[source_id]
        if (
            baseline["canonical_locator"] != source["canonical_locator"]
            or baseline["prior_content_digest"] != source["source_digest"]
            or baseline["prior_media_type"] != source["media_type"]
        ):
            raise ContractError("freshness policy graph source binding differs")
    return _mint_validated_freshness_summary(
        result, result["summary_digest"], result["graph_digest"],
        _freshness_summary_context_digest(result),
    )


def validate_detached_freshness_summary_against_manifest(
    summary: Any, graph: Any,
) -> dict[str, Any]:
    """Consume a previously context-validated detached summary capability."""

    expected_graph = validate_graph_manifest(graph)
    if type(summary) is not _ValidatedFreshnessSummary:
        raise ContractError("freshness summary lacks validated context")
    result = validate_freshness_summary(dict(summary))
    if not _matches_validated_freshness_summary(
        summary, result, expected_graph["graph_digest"],
    ):
        raise ContractError("freshness summary validated context differs")
    if any(result[field] != expected_graph[field] for field in ("graph_id", "graph_digest")):
        raise ContractError("freshness summary graph binding differs")
    return result


_VALIDATORS = dict(zip(SCHEMA_VERSIONS, (validate_source_registry, validate_acquisition_record, validate_authority_role,
    validate_relationship_edge, validate_operational_question, validate_graph_manifest, validate_graph_inspection,
    validate_query_readback, validate_campaign_summary, validate_recovery, validate_freshness_policy,
    validate_freshness_observation, validate_freshness_comparison, validate_freshness_summary)))


def validate_evidence_payload(value: Any) -> dict[str, Any]:
    """Validate a standalone artifact shape.

    Inspection and query readbacks require their context-aware validators before
    product use; this dispatcher cannot establish cross-artifact authority.
    """

    if type(value) is not dict or value.get("schema_version") not in _VALIDATORS:
        raise ContractError("unsupported evidence graph schema version")
    result = _VALIDATORS[value["schema_version"]](value)
    try:
        canonical_digest(result)
    except BenchmarkError as exc:
        raise ContractError("evidence payload is not strict JSON") from exc
    return copy.deepcopy(result)
