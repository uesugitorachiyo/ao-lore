"""Read-only drift monitoring over supplied AO Lore evidence."""

from __future__ import annotations

import copy
import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ._strict_io import (
    ContractError,
    TRANSITION_RE,
    require_bool,
    require_exact_keys,
    require_identifier,
    require_int,
    require_sha256,
    strict_read_json,
    write_exclusive_json,
)


MAX_INPUT_BYTES = 128 * 1024
ROLES = ("parser", "distiller", "navigator", "synthesizer")
METRICS = (
    "parser_selection_score_basis_points",
    "parse_quality_threshold_basis_points",
    "evidence_coverage_basis_points",
    "wasted_traversal_nodes",
    "nodes_per_satisfied_requirement_milli",
    "tokens_per_coverage_point_milli",
)
TOLERANCES = (
    "max_parser_score_drop_basis_points",
    "max_threshold_change_basis_points",
    "max_coverage_drop_basis_points",
    "max_wasted_traversal_increase",
    "max_nodes_per_requirement_increase_milli",
    "max_tokens_per_coverage_increase_milli",
)


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{label} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"invalid {label}") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{label} must include a UTC offset")
    return parsed


def _validate_metrics(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ContractError("metrics must be an object")
    require_exact_keys(value, METRICS, "monitoring metrics")
    result: dict[str, int] = {}
    for name in METRICS[:3]:
        result[name] = require_int(value[name], name, 0, 10000)
    for name in METRICS[3:]:
        result[name] = require_int(value[name], name, 0, 1_000_000_000)
    return result


def _validate_baseline(value: dict[str, Any]) -> dict[str, Any]:
    required = (
        "schema_version",
        "baseline_id",
        "target_id",
        "source_head_sha256",
        "profile_sha256",
        "generated_at_utc",
        "valid_until_utc",
        "metrics",
        "tolerances",
        "allowed_role_fallbacks",
        "trace_policy_sha256",
    )
    require_exact_keys(value, required, "monitoring baseline")
    if value["schema_version"] != "ao.lore.monitoring-baseline.v0.1":
        raise ContractError("invalid monitoring baseline schema_version")
    require_identifier(value["baseline_id"], "baseline_id")
    require_identifier(value["target_id"], "target_id")
    for name in ("source_head_sha256", "profile_sha256", "trace_policy_sha256"):
        require_sha256(value[name], name)
    generated = _parse_time(value["generated_at_utc"], "generated_at_utc")
    valid_until = _parse_time(value["valid_until_utc"], "valid_until_utc")
    if valid_until <= generated:
        raise ContractError("valid_until_utc must be after generated_at_utc")
    _validate_metrics(value["metrics"])
    tolerances = value["tolerances"]
    if not isinstance(tolerances, dict):
        raise ContractError("tolerances must be an object")
    require_exact_keys(tolerances, TOLERANCES, "monitoring tolerances")
    for name in TOLERANCES:
        require_int(tolerances[name], name, 0, 1_000_000_000)
    fallbacks = value["allowed_role_fallbacks"]
    if not isinstance(fallbacks, dict):
        raise ContractError("allowed_role_fallbacks must be an object")
    require_exact_keys(fallbacks, ROLES, "allowed_role_fallbacks")
    for role in ROLES:
        transitions = fallbacks[role]
        if not isinstance(transitions, list) or len(transitions) > 20:
            raise ContractError(f"allowed fallbacks for {role} must be a bounded array")
        if len(set(transitions)) != len(transitions):
            raise ContractError(f"allowed fallbacks for {role} contain duplicates")
        for transition in transitions:
            if not isinstance(transition, str) or not TRANSITION_RE.fullmatch(transition):
                raise ContractError(f"allowed fallback for {role} is invalid")
    return value


def _validate_observation(value: dict[str, Any], *, baseline_provided: bool) -> dict[str, Any]:
    required = (
        "schema_version",
        "observation_id",
        "target_id",
        "baseline_id",
        "baseline_sha256",
        "source_head_sha256",
        "profile_sha256",
        "observed_at_utc",
        "metrics",
        "role_fallbacks",
        "trace_policy_sha256",
        "trace_integrity",
    )
    require_exact_keys(value, required, "monitoring observation")
    if value["schema_version"] != "ao.lore.monitoring-observation.v0.1":
        raise ContractError("invalid monitoring observation schema_version")
    require_identifier(value["observation_id"], "observation_id")
    require_identifier(value["target_id"], "target_id")
    require_identifier(value["baseline_id"], "baseline_id", allow_empty=not baseline_provided)
    require_sha256(value["baseline_sha256"], "baseline_sha256", allow_empty=not baseline_provided)
    if baseline_provided and (not value["baseline_id"] or not value["baseline_sha256"]):
        raise ContractError("provided baseline requires observation bindings")
    if not baseline_provided and (value["baseline_id"] or value["baseline_sha256"]):
        raise ContractError("missing baseline requires empty observation bindings")
    for name in ("source_head_sha256", "profile_sha256", "trace_policy_sha256"):
        require_sha256(value[name], name)
    _parse_time(value["observed_at_utc"], "observed_at_utc")
    _validate_metrics(value["metrics"])
    fallbacks = value["role_fallbacks"]
    if not isinstance(fallbacks, list) or len(fallbacks) > 20:
        raise ContractError("role_fallbacks must be a bounded array")
    for index, fallback in enumerate(fallbacks):
        if not isinstance(fallback, dict):
            raise ContractError(f"role_fallbacks[{index}] must be an object")
        require_exact_keys(
            fallback,
            ("role", "from_adapter", "to_adapter"),
            f"role_fallbacks[{index}]",
        )
        if fallback["role"] not in ROLES:
            raise ContractError("role fallback has unknown role")
        source = require_identifier(fallback["from_adapter"], "from_adapter")
        target = require_identifier(fallback["to_adapter"], "to_adapter")
        if source == target:
            raise ContractError("role fallback must change adapters")
    require_bool(value["trace_integrity"], "trace_integrity")
    return value


def _finding(code: str, severity: str, reason: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "reason": reason}


def _authority_flags(status: str) -> dict[str, bool]:
    return {
        "promoter_hold_required": status != "clear",
        "mutates_live_state": False,
        "approves_work": False,
        "promotes_candidate": False,
        "releases_or_publishes": False,
    }


def _missing_baseline(observation: Mapping[str, Any], observation_digest: str) -> dict[str, Any]:
    status = "hold"
    return {
        "schema_version": "ao.lore.monitoring-verdict.v0.1",
        "observation_id": observation["observation_id"],
        "target_id": observation["target_id"],
        "baseline_id": None,
        "baseline_status": "missing",
        "baseline_source_head_sha256": None,
        "observed_source_head_sha256": observation["source_head_sha256"],
        "input_digests": {
            "baseline_sha256": None,
            "observation_sha256": observation_digest,
        },
        "status": status,
        "findings": [
            _finding(
                "baseline_missing",
                "hold",
                "A versioned AO Lore monitoring baseline was not supplied.",
            )
        ],
        "metric_deltas": {},
        "observed_metrics": copy.deepcopy(observation["metrics"]),
        "role_fallbacks": copy.deepcopy(observation["role_fallbacks"]),
        "trace_integrity": observation["trace_integrity"],
        **_authority_flags(status),
    }


def _build_verdict(
    baseline: Mapping[str, Any],
    observation: Mapping[str, Any],
    baseline_digest: str,
    observation_digest: str,
) -> dict[str, Any]:
    observed_at = _parse_time(observation["observed_at_utc"], "observed_at_utc")
    valid_until = _parse_time(baseline["valid_until_utc"], "valid_until_utc")
    baseline_metrics = baseline["metrics"]
    observed_metrics = observation["metrics"]
    deltas = {name: observed_metrics[name] - baseline_metrics[name] for name in METRICS}
    tolerance = baseline["tolerances"]
    findings: list[dict[str, str]] = []
    stale = observed_at > valid_until
    if stale:
        findings.append(_finding("baseline_stale", "hold", "Observation time is after the baseline validity window."))
    if -deltas["parser_selection_score_basis_points"] > tolerance["max_parser_score_drop_basis_points"]:
        findings.append(_finding("parser_score_drift", "hold", "Parser selection score dropped beyond tolerance."))
    if abs(deltas["parse_quality_threshold_basis_points"]) > tolerance["max_threshold_change_basis_points"]:
        findings.append(_finding("threshold_drift", "hold", "Parse quality threshold changed beyond tolerance."))
    if -deltas["evidence_coverage_basis_points"] > tolerance["max_coverage_drop_basis_points"]:
        findings.append(_finding("coverage_regression", "hold", "Evidence coverage dropped beyond tolerance."))
    if deltas["wasted_traversal_nodes"] > tolerance["max_wasted_traversal_increase"]:
        findings.append(_finding("wasted_traversal_regression", "hold", "Traversal after sufficiency exceeded tolerance."))
    if deltas["nodes_per_satisfied_requirement_milli"] > tolerance["max_nodes_per_requirement_increase_milli"]:
        findings.append(_finding("nodes_per_requirement_regression", "hold", "Nodes per satisfied requirement regressed."))
    if deltas["tokens_per_coverage_point_milli"] > tolerance["max_tokens_per_coverage_increase_milli"]:
        findings.append(_finding("tokens_per_coverage_regression", "hold", "Tokens per coverage point regressed."))
    allowed = {
        f"{role}:{transition}"
        for role, transitions in baseline["allowed_role_fallbacks"].items()
        for transition in transitions
    }
    for fallback in observation["role_fallbacks"]:
        key = f"{fallback['role']}:{fallback['from_adapter']}->{fallback['to_adapter']}"
        if key not in allowed:
            findings.append(_finding("unauthorized_role_fallback", "incident", "Observed role fallback is absent from the fixed baseline policy."))
    if not observation["trace_integrity"]:
        findings.append(_finding("trace_integrity_failure", "incident", "AO Lore trace integrity verification failed."))
    if observation["trace_policy_sha256"] != baseline["trace_policy_sha256"]:
        findings.append(_finding("trace_policy_mismatch", "incident", "Observed trace policy digest does not match the baseline."))
    findings.sort(key=lambda finding: finding["code"])
    status = "clear"
    if any(finding["severity"] == "incident" for finding in findings):
        status = "incident"
    elif findings:
        status = "hold"
    return {
        "schema_version": "ao.lore.monitoring-verdict.v0.1",
        "observation_id": observation["observation_id"],
        "target_id": observation["target_id"],
        "baseline_id": baseline["baseline_id"],
        "baseline_status": "stale" if stale else "present",
        "baseline_source_head_sha256": baseline["source_head_sha256"],
        "observed_source_head_sha256": observation["source_head_sha256"],
        "input_digests": {
            "baseline_sha256": baseline_digest,
            "observation_sha256": observation_digest,
        },
        "status": status,
        "findings": findings,
        "metric_deltas": deltas,
        "observed_metrics": copy.deepcopy(observed_metrics),
        "role_fallbacks": copy.deepcopy(observation["role_fallbacks"]),
        "trace_integrity": observation["trace_integrity"],
        **_authority_flags(status),
    }


def evaluate_monitoring(
    observation_path: str | os.PathLike[str],
    *,
    baseline_path: str | os.PathLike[str] | None = None,
    output_path: str | os.PathLike[str] | None = None,
    output_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Evaluate supplied observation evidence without mutating live state."""

    observation, observation_body = strict_read_json(
        observation_path,
        "monitoring observation",
        max_bytes=MAX_INPUT_BYTES,
    )
    _validate_observation(observation, baseline_provided=baseline_path is not None)
    observation_digest = hashlib.sha256(observation_body).hexdigest()
    if baseline_path is None:
        verdict = _missing_baseline(observation, observation_digest)
    else:
        baseline, baseline_body = strict_read_json(
            baseline_path,
            "monitoring baseline",
            max_bytes=MAX_INPUT_BYTES,
        )
        _validate_baseline(baseline)
        baseline_digest = hashlib.sha256(baseline_body).hexdigest()
        if observation["baseline_sha256"] != baseline_digest:
            raise ContractError("monitoring baseline SHA-256 mismatch")
        if (
            observation["baseline_id"] != baseline["baseline_id"]
            or observation["target_id"] != baseline["target_id"]
            or observation["profile_sha256"] != baseline["profile_sha256"]
        ):
            raise ContractError("observation does not match baseline identity, target, or profile")
        verdict = _build_verdict(baseline, observation, baseline_digest, observation_digest)
    if output_path is not None:
        root = Path(output_root) if output_root is not None else Path(output_path).absolute().parent
        write_exclusive_json(output_path, verdict, root=root)
    return verdict
