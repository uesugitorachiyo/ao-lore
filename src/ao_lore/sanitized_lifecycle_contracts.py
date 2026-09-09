"""Strict path-free contract for the offline sanitized lifecycle rehearsal."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from ._strict_io import ContractError, require_exact_keys


_SCHEMA_VERSION = "ao.lore.sanitized-lifecycle-rehearsal.v0.1"
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ABSOLUTE_POSIX_PATH = re.compile(
    r"""(?:^|[\s(\[{'\"=:,;])/(?!/)[^\s)\]},;!?'"<>]+"""
)
_DIGEST_FIELDS = (
    "corpus_digest", "document_digest", "evidence_digest", "candidate_digest",
    "review_digest", "proposal_digest", "authorization_digest", "promotion_digest",
    "generation_digest", "search_digest", "answer_digest", "inventory_digest",
)
_IDENTITY_BOUNDS = {
    "document_ids": 16,
    "evidence_ids": 64,
    "candidate_ids": 16,
    "review_ids": 16,
    "canonical_entry_ids": 16,
}
_COUNT_BOUNDS = {
    "documents": 16,
    "evidence_identities": 64,
    "candidates": 16,
    "reviews": 16,
    "proposals": 16,
    "authorizations": 16,
    "promotions": 16,
    "canonical_entries": 16,
    "search_hits": 16,
    "answers": 16,
}
_COUNT_IDENTITIES = {
    "documents": "document_ids",
    "evidence_identities": "evidence_ids",
    "candidates": "candidate_ids",
    "reviews": "review_ids",
    "canonical_entries": "canonical_entry_ids",
}
_AUTHORITY_FIELDS = {
    "live_state_accessed", "customer_data_accessed", "github_accessed",
    "publication_executed", "release_executed", "deployment_executed",
    "credentials_used", "authority_advanced",
}
_REPORT_KEYS = {
    "schema_version", "rehearsal_id", "completed_at", "summary", *_DIGEST_FIELDS,
    *_IDENTITY_BOUNDS, "counts", "scenario_statuses", "byte_identical_check",
    "network_calls", "provider_calls", "external_authority", "report_digest",
}


def _identifier(value: Any, label: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise ContractError(f"{label} must be a bounded lowercase identifier")
    return value


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ContractError(f"{label} must be a lowercase sha256 digest")
    return value


def _timestamp(value: Any, label: str) -> str:
    if type(value) is not str or _TIMESTAMP.fullmatch(value) is None:
        raise ContractError(f"{label} must be an exact UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ContractError(f"{label} must be an exact UTC timestamp") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError(f"{label} must be UTC")
    return value


def _public_text(value: Any, label: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 512:
        raise ContractError(f"{label} must be bounded public text")
    lowered = value.casefold()
    if (
        any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)
        or "file:" in lowered
        or "/home/" in lowered
        or "/opt/" in lowered
        or "/tmp/" in lowered
        or "\\" in value
        or _ABSOLUTE_POSIX_PATH.search(value) is not None
    ):
        raise ContractError(f"{label} contains unsafe or private text")
    return value


def _bounded_ids(value: Any, label: str, maximum: int) -> list[str]:
    if type(value) is not list or not 0 <= len(value) <= maximum:
        raise ContractError(f"{label} must be a bounded array")
    result = [_identifier(item, f"{label} item") for item in value]
    if len(result) != len(set(result)):
        raise ContractError(f"{label} must contain unique identities")
    return result


def _bounded_integer(value: Any, label: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ContractError(f"{label} must be a bounded exact integer")
    return value


def _canonical_digest(value: dict[str, Any], *, omit: str) -> str:
    detached = {key: item for key, item in value.items() if key != omit}
    try:
        body = json.dumps(
            detached,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("rehearsal report must contain only strict JSON values") from exc
    return hashlib.sha256(body).hexdigest()


def validate_sanitized_lifecycle_rehearsal(value: object) -> dict[str, object]:
    """Return a deep validated report or raise ContractError."""

    if type(value) is not dict:
        raise ContractError("sanitized lifecycle rehearsal must be an object")
    require_exact_keys(value, _REPORT_KEYS, "sanitized lifecycle rehearsal")
    if value["schema_version"] != _SCHEMA_VERSION:
        raise ContractError("unsupported sanitized lifecycle rehearsal version")

    result: dict[str, object] = {
        "schema_version": value["schema_version"],
        "rehearsal_id": _identifier(value["rehearsal_id"], "rehearsal_id"),
        "completed_at": _timestamp(value["completed_at"], "completed_at"),
        "summary": _public_text(value["summary"], "summary"),
    }
    result.update({field: _digest(value[field], field) for field in _DIGEST_FIELDS})

    identities = {
        field: _bounded_ids(value[field], field, maximum)
        for field, maximum in _IDENTITY_BOUNDS.items()
    }
    result.update(identities)

    counts = value["counts"]
    if type(counts) is not dict:
        raise ContractError("counts must be an object")
    require_exact_keys(counts, _COUNT_BOUNDS, "counts")
    validated_counts = {
        field: _bounded_integer(counts[field], f"counts.{field}", maximum)
        for field, maximum in _COUNT_BOUNDS.items()
    }
    for count_field, identity_field in _COUNT_IDENTITIES.items():
        if validated_counts[count_field] != len(identities[identity_field]):
            raise ContractError(f"counts.{count_field} does not bind {identity_field}")
    result["counts"] = validated_counts

    scenarios = value["scenario_statuses"]
    if type(scenarios) is not list or not 1 <= len(scenarios) <= 32:
        raise ContractError("scenario_statuses must be a bounded non-empty array")
    validated_scenarios: list[dict[str, str]] = []
    scenario_ids: list[str] = []
    for index, scenario in enumerate(scenarios):
        if type(scenario) is not dict:
            raise ContractError("scenario status must be an object")
        require_exact_keys(scenario, {"scenario_id", "status"}, f"scenario_statuses[{index}]")
        scenario_id = _identifier(scenario["scenario_id"], "scenario_id")
        status = scenario["status"]
        if status not in {"pass", "fail", "recovered"}:
            raise ContractError("scenario status is unsupported")
        scenario_ids.append(scenario_id)
        validated_scenarios.append({"scenario_id": scenario_id, "status": status})
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ContractError("scenario IDs must be unique")
    result["scenario_statuses"] = validated_scenarios

    if value["byte_identical_check"] is not True:
        raise ContractError("byte_identical_check must be true")
    if type(value["network_calls"]) is not int or value["network_calls"] != 0:
        raise ContractError("network_calls must be exactly zero")
    if type(value["provider_calls"]) is not int or value["provider_calls"] != 0:
        raise ContractError("provider_calls must be exactly zero")
    result.update(byte_identical_check=True, network_calls=0, provider_calls=0)

    authority = value["external_authority"]
    if type(authority) is not dict:
        raise ContractError("external_authority must be an object")
    require_exact_keys(authority, _AUTHORITY_FIELDS, "external_authority")
    if any(authority[field] is not False for field in _AUTHORITY_FIELDS):
        raise ContractError("external authority flags must remain false")
    result["external_authority"] = {field: False for field in authority}

    supplied = _digest(value["report_digest"], "report_digest")
    expected = _canonical_digest(value, omit="report_digest")
    if not hmac.compare_digest(supplied, expected):
        raise ContractError("sanitized lifecycle rehearsal digest mismatch")
    result["report_digest"] = supplied
    return deepcopy(result)


__all__ = ["validate_sanitized_lifecycle_rehearsal"]
