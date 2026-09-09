"""Fail-closed deterministic scoring for AO Lore.

Selection estimates which eligible parser should run. Parse quality measures
the result after it runs. Evidence coverage measures whether the ledger can
support an answer. The three values intentionally never share a score field.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence


class ScoringError(ValueError):
    """Raised when a scoring input cannot be evaluated safely."""


_EXTERNAL_GATES = (
    "provenance_present",
    "citations_present",
    "freshness_met",
    "trust_met",
    "no_hard_contradiction",
    "deterministic_validation_passed",
)


def _number(value: Any, field: str, *, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScoringError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ScoringError(f"{field} must be finite")
    if low is not None and result < low:
        raise ScoringError(f"{field} must be >= {low}")
    if high is not None and result > high:
        raise ScoringError(f"{field} must be <= {high}")
    return result


def _required_string(mapping: Mapping[str, Any], field: str) -> str:
    value = mapping.get(field)
    if not isinstance(value, str) or not value:
        raise ScoringError(f"{field} must be a non-empty string")
    return value


def evaluate_eligibility(capability: Mapping[str, Any], request: Mapping[str, Any]) -> list[str]:
    """Return stable rejection reasons; an empty result means eligible."""

    reasons: list[str] = []
    media_type = _required_string(request, "media_type")
    extension = _required_string(request, "extension")
    ir_version = _required_string(request, "canonical_ir_version")

    if media_type not in capability.get("supported_mime_types", []):
        reasons.append(f"unsupported_media_type:{media_type}")
    if extension.lower() not in {str(item).lower() for item in capability.get("supported_extensions", [])}:
        reasons.append(f"unsupported_extension:{extension}")

    available_elements = set(capability.get("structural_elements", []))
    for element in request.get("required_structural_elements", []):
        if element not in available_elements:
            reasons.append(f"missing_structural_element:{element}")
    if ir_version not in capability.get("canonical_ir_versions", []):
        reasons.append(f"unsupported_ir_version:{ir_version}")

    network_allowed = request.get("network_allowed") is True
    if capability.get("frontier_service_required") is True and not network_allowed:
        reasons.append("network_policy_violation")
    if "frontier_service_required" not in capability:
        reasons.append("network_requirement_unknown")

    if capability.get("runtime_available") is False:
        reasons.append("runtime_unavailable")
    elif capability.get("runtime_available") is not True:
        reasons.append("runtime_availability_unknown")

    allowlist = request.get("license_allowlist")
    license_id = capability.get("license_id")
    if isinstance(allowlist, list) and license_id not in allowlist:
        reasons.append(f"license_not_allowed:{license_id or 'unknown'}")

    if request.get("sandbox_required") is True and capability.get("sandbox_supported") is not True:
        reasons.append("sandbox_requirement_unsatisfied")

    file_bytes = request.get("file_bytes")
    maximum = capability.get("max_file_bytes")
    if file_bytes is not None:
        file_size = _number(file_bytes, "file_bytes", low=0)
        if maximum is None:
            reasons.append("file_limit_unknown")
        elif file_size > _number(maximum, "max_file_bytes", low=0):
            reasons.append(f"file_limit_exceeded:{int(file_size)}>{int(maximum)}")

    return reasons


def _normalize(value: Any, component: str, bounds: Mapping[str, Any]) -> float:
    raw = _number(value, component)
    if component not in bounds:
        return _number(raw, component, low=0, high=1)
    interval = bounds[component]
    if not isinstance(interval, Sequence) or isinstance(interval, (str, bytes)) or len(interval) != 2:
        raise ScoringError(f"normalization bound for {component} must contain two values")
    lower = _number(interval[0], f"{component}.minimum")
    upper = _number(interval[1], f"{component}.maximum")
    if upper <= lower:
        raise ScoringError(f"normalization bound for {component} is not increasing")
    return min(1.0, max(0.0, (raw - lower) / (upper - lower)))


def _selection_components(capability: Mapping[str, Any], request: Mapping[str, Any]) -> dict[str, float]:
    required = list(request.get("required_structural_elements", []))
    available = set(capability.get("structural_elements", []))
    feature_coverage = 1.0 if not required else sum(item in available for item in required) / len(required)
    metrics = capability.get("selection_metrics", {})
    if not isinstance(metrics, Mapping):
        raise ScoringError("selection_metrics must be an object")
    components = {
        "format_compatibility": 1.0,
        "required_feature_coverage": feature_coverage,
        "privacy_offline_fit": 1.0
        if capability.get("deterministic_offline") is True and capability.get("frontier_service_required") is False
        else 0.0,
        "operational_availability": 1.0 if capability.get("runtime_available") is True else 0.0,
    }
    for name, value in metrics.items():
        components[str(name)] = value
    return components


def _tie_key(candidate: Mapping[str, Any], order: Iterable[str]) -> tuple[Any, ...]:
    components = candidate["normalized_components"]
    values: list[Any] = []
    for name in order:
        if name == "parser_id":
            values.append(candidate["parser_id"])
        elif name.startswith("lower_"):
            values.append(_number(components.get(name[6:], 1.0), name, low=0, high=1))
        else:
            values.append(-_number(components.get(name, 0.0), name, low=0, high=1))
    if "parser_id" not in order:
        values.append(candidate["parser_id"])
    return tuple(values)


def select_parser(
    document_digest: str,
    capabilities: Sequence[Mapping[str, Any]],
    request: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    """Filter, score, and rank parsers without executing any adapter."""

    _required_string({"document_digest": document_digest}, "document_digest")
    profile_id = _required_string(profile, "profile_id")
    weights = profile.get("weights")
    penalties = profile.get("penalty_weights", {})
    bounds = profile.get("normalization_bounds", {})
    if not isinstance(weights, Mapping) or not weights:
        raise ScoringError("weights must be a non-empty object")
    if not isinstance(penalties, Mapping) or not isinstance(bounds, Mapping):
        raise ScoringError("penalty weights and normalization bounds must be objects")

    rejected: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    benchmark_refs: set[str] = set()
    for capability in capabilities:
        parser_id = _required_string(capability, "parser_id")
        reasons = evaluate_eligibility(capability, request)
        if reasons:
            rejected.append({"parser_id": parser_id, "reasons": reasons})
            continue

        raw_components = _selection_components(capability, request)
        normalized: dict[str, float] = {}
        numerator = 0.0
        denominator = 0.0
        missing_policy = profile.get("missing_optional_policy", "ineligible")
        for name, raw_weight in weights.items():
            weight = _number(raw_weight, f"weights.{name}", low=0)
            if weight == 0:
                continue
            if name not in raw_components:
                if missing_policy == "ignore_and_renormalize":
                    continue
                if missing_policy == "conservative_prior":
                    raw_components[name] = 0.5
                else:
                    raise ScoringError(f"{parser_id} lacks scored component {name}")
            normalized[name] = _normalize(raw_components[name], name, bounds)
            numerator += weight * normalized[name]
            denominator += weight
        if denominator <= 0:
            raise ScoringError("applicable selection weights sum to zero")
        benefit = numerator / denominator

        penalty_numerator = 0.0
        penalty_denominator = 0.0
        for name, raw_weight in penalties.items():
            weight = _number(raw_weight, f"penalty_weights.{name}", low=0)
            if weight == 0 or name not in raw_components:
                continue
            normalized[name] = _normalize(raw_components[name], name, bounds)
            penalty_numerator += weight * normalized[name]
            penalty_denominator += weight
        penalty = penalty_numerator / penalty_denominator if penalty_denominator else 0.0
        score = round(benefit - penalty, 12)
        candidates.append(
            {
                "parser_id": parser_id,
                "normalized_components": dict(sorted(normalized.items())),
                "selection_score": score,
            }
        )
        for ref in capability.get("benchmark_result_refs", []):
            if isinstance(ref, str):
                benchmark_refs.add(ref)

    if not candidates:
        raise ScoringError("no eligible parser")

    tie_order = profile.get("tie_break_order", ["parser_id"])
    if not isinstance(tie_order, list) or not tie_order:
        raise ScoringError("tie_break_order must be a non-empty array")
    candidates.sort(key=lambda item: (-item["selection_score"],) + _tie_key(item, tie_order))
    tied = len(candidates) > 1 and candidates[0]["selection_score"] == candidates[1]["selection_score"]
    rejected.sort(key=lambda item: item["parser_id"])
    return {
        "schema_version": "ao.lore.parser-selection-report.v0.1",
        "document_digest": document_digest,
        "weight_profile": profile_id,
        "eligible_parsers": sorted(item["parser_id"] for item in candidates),
        "rejected_parsers": rejected,
        "candidates": candidates,
        "selected_parser": candidates[0]["parser_id"],
        "tie_break": {
            "applied": tied,
            "reason": "configured tie-break order applied" if tied else "highest numeric selection score",
        },
        "benchmark_evidence": sorted(benchmark_refs),
    }


def compute_parse_quality(
    document_digest: str,
    parser_id: str,
    components: Mapping[str, float | None],
    profile: Mapping[str, Any],
    fallback_attempts: int = 0,
    critical_failures: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Score an actual parse, excluding non-applicable components."""

    weights = profile.get("component_weights")
    if not isinstance(weights, Mapping) or not weights:
        raise ScoringError("component_weights must be a non-empty object")
    attempts = int(_number(fallback_attempts, "fallback_attempts", low=0))
    maximum = int(_number(profile.get("maximum_fallbacks"), "maximum_fallbacks", low=0))
    accept_threshold = _number(profile.get("accept_threshold"), "accept_threshold", low=0, high=1)
    fallback_threshold = _number(profile.get("fallback_threshold"), "fallback_threshold", low=0, high=1)
    if fallback_threshold > accept_threshold:
        raise ScoringError("fallback_threshold cannot exceed accept_threshold")

    weighted_sum = 0.0
    applicable_weight = 0.0
    report_components: dict[str, dict[str, Any]] = {}
    for name, raw_weight in weights.items():
        weight = _number(raw_weight, f"component_weights.{name}", low=0)
        value = components.get(name)
        if value is None:
            report_components[name] = {"score": None, "applicable": False, "weight": weight}
            continue
        score = _number(value, f"components.{name}", low=0, high=1)
        report_components[name] = {"score": score, "applicable": True, "weight": weight}
        weighted_sum += score * weight
        applicable_weight += weight
    if applicable_weight <= 0:
        raise ScoringError("no applicable parse-quality components")
    overall = round(weighted_sum / applicable_weight, 12)

    failures = sorted(set(critical_failures or []))
    configured_failures = set(profile.get("critical_failures", []))
    unknown_failures = [item for item in failures if item not in configured_failures]
    if unknown_failures:
        raise ScoringError(f"unrecognized critical failures: {', '.join(unknown_failures)}")
    if failures or overall < fallback_threshold:
        decision = "fallback" if attempts < maximum else profile.get("exhausted_decision", "quarantine")
    elif overall >= accept_threshold:
        decision = "accept"
    else:
        decision = profile.get("intermediate_decision", "quarantine")
    if decision not in {"accept", "fallback", "quarantine", "reject"}:
        raise ScoringError("invalid quality decision policy")

    return {
        "schema_version": "ao.lore.parse-quality-report.v0.1",
        "document_digest": document_digest,
        "parser_id": parser_id,
        "profile_id": _required_string(profile, "profile_id"),
        "overall_quality_score": overall,
        "components": report_components,
        "critical_failures": failures,
        "decision": decision,
        "passed": decision == "accept",
        "fallback_attempts": attempts,
        "thresholds": {"accept": accept_threshold, "fallback": fallback_threshold},
    }


def compute_evidence_coverage(
    query_digest: str,
    requirements: Sequence[Mapping[str, Any]],
    profile: Mapping[str, Any],
    external_gates: Mapping[str, Any],
    *,
    coverage_history: Sequence[Mapping[str, Any]] | None = None,
    budget_exhausted: bool = False,
) -> dict[str, Any]:
    """Compute coverage and deterministic answer sufficiency gates."""

    if not requirements:
        raise ScoringError("at least one evidence requirement is required")
    missing_gates = [name for name in _EXTERNAL_GATES if not isinstance(external_gates.get(name), bool)]
    if missing_gates:
        raise ScoringError(f"missing boolean gates: {', '.join(missing_gates)}")
    mapping = profile.get("satisfaction_values")
    if not isinstance(mapping, Mapping):
        raise ScoringError("satisfaction_values must be an object")
    target = _number(profile.get("target"), "target", low=0, high=1)

    total_weight = 0.0
    weighted_value = 0.0
    requirement_reports: list[dict[str, Any]] = []
    mandatory_met = True
    hard_unsatisfied = False
    for requirement in requirements:
        requirement_id = _required_string(requirement, "id")
        weight = _number(requirement.get("importance_weight"), f"{requirement_id}.importance_weight", low=0)
        if weight == 0:
            raise ScoringError("requirement weights must be positive")
        status = _required_string(requirement, "status")
        if status not in mapping:
            raise ScoringError(f"no satisfaction mapping for {status}")
        value = _number(mapping[status], f"satisfaction_values.{status}", low=0, high=1)
        total_weight += weight
        weighted_value += weight * value
        mandatory = requirement.get("mandatory") is True
        if mandatory and status != "satisfied":
            mandatory_met = False
        if mandatory and status in {"contradictory", "disqualified"}:
            hard_unsatisfied = True
        requirement_reports.append(
            {
                "id": requirement_id,
                "weight": weight,
                "status": status,
                "satisfaction_value": value,
                "evidence_refs": list(requirement.get("evidence_refs", [])),
                "coverage_gain": _number(requirement.get("coverage_gain", 0), f"{requirement_id}.coverage_gain", low=0, high=1),
            }
        )
    coverage = round(weighted_value / total_weight, 12)
    gates = {
        "coverage_target_met": coverage >= target,
        "mandatory_requirements_met": mandatory_met,
        **{name: external_gates[name] for name in _EXTERNAL_GATES},
    }
    sufficient = all(gates.values())
    if sufficient:
        decision = "answer"
        reason = "all evidence sufficiency gates passed"
    elif hard_unsatisfied or external_gates["no_hard_contradiction"] is False:
        decision = "refuse"
        reason = "a hard evidence contradiction or disqualification remains"
    elif budget_exhausted:
        decision = "partial"
        reason = "a hard traversal budget was exhausted before sufficiency"
    else:
        decision = "replan"
        reason = "target uncovered evidence requirements"
    return {
        "schema_version": "ao.lore.evidence-coverage-report.v0.1",
        "query_digest": query_digest,
        "profile_id": _required_string(profile, "profile_id"),
        "target": target,
        "coverage": coverage,
        "requirements": requirement_reports,
        "gates": gates,
        "coverage_history": list(coverage_history or []),
        "decision": decision,
        "reason": reason,
    }


def branch_priority(expected_uncovered_requirement_gain: Any, estimated_traversal_cost: Any) -> float:
    """Return expected marginal evidence gain per unit traversal cost."""

    gain = _number(expected_uncovered_requirement_gain, "expected_uncovered_requirement_gain", low=0)
    cost = _number(estimated_traversal_cost, "estimated_traversal_cost", low=0)
    if cost == 0:
        raise ScoringError("estimated_traversal_cost must be greater than zero")
    return gain / cost
