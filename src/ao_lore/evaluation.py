"""Digest-bound, provider-free evaluation of matched AO Lore attempts."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ._strict_io import (
    ContractError,
    require_bool,
    require_exact_keys,
    require_identifier,
    require_int,
    require_sha256,
    require_text,
    strict_read_json,
    write_exclusive_json,
)


FORMATS = frozenset({"pdf", "markdown", "html", "docx", "text", "image"})
METRICS = (
    "parser_fidelity",
    "parser_cost_efficiency",
    "coverage_efficiency",
    "role_configuration_quality",
)
ROLES = ("parser", "distiller", "navigator", "synthesizer")
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ATTEMPT_BYTES = 128 * 1024
MAX_PAIRS = 50


def _require_list(value: Any, label: str, *, maximum: int = 100) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ContractError(f"{label} must be a bounded array")
    return value


def _validate_annotations(value: Any, label: str) -> list[str]:
    result = _require_list(value, label)
    for index, item in enumerate(result):
        require_text(item, f"{label}[{index}]")
    return result


def _validate_metric(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    require_exact_keys(value, ("applicable", "score_basis_points", "reason"), label)
    applicable = require_bool(value["applicable"], f"{label}.applicable")
    score = value["score_basis_points"]
    if applicable:
        require_int(score, f"{label}.score_basis_points", 0, 10000)
    elif score is not None:
        raise ContractError(f"non-applicable {label} must use a null score")
    require_text(value["reason"], f"{label}.reason")
    return value


def _validate_attempt(value: dict[str, Any]) -> dict[str, Any]:
    required = (
        "schema_version",
        "attempt_id",
        "system_id",
        "format",
        "fixture_corpus_sha256",
        "workload_profile_sha256",
        "source_input_sha256",
        "result_sha256",
        "metrics",
        "role_configuration",
        "eligible",
        "ineligibility_reasons",
        "failures",
        "exclusions",
        "trace_integrity",
    )
    require_exact_keys(value, required, "evaluation attempt")
    if value["schema_version"] != "ao.lore.evaluation-attempt.v0.1":
        raise ContractError("invalid evaluation attempt schema_version")
    require_identifier(value["attempt_id"], "attempt_id")
    require_identifier(value["system_id"], "system_id")
    if value["format"] not in FORMATS:
        raise ContractError("evaluation attempt has unsupported format")
    for field in (
        "fixture_corpus_sha256",
        "workload_profile_sha256",
        "source_input_sha256",
        "result_sha256",
    ):
        require_sha256(value[field], field)
    metrics = value["metrics"]
    if not isinstance(metrics, dict):
        raise ContractError("metrics must be an object")
    require_exact_keys(metrics, METRICS, "metrics")
    for metric in METRICS:
        _validate_metric(metrics[metric], f"metrics.{metric}")
    roles = value["role_configuration"]
    if not isinstance(roles, dict):
        raise ContractError("role_configuration must be an object")
    require_exact_keys(roles, ROLES, "role_configuration")
    for role in ROLES:
        require_identifier(roles[role], f"role_configuration.{role}")
    eligible = require_bool(value["eligible"], "eligible")
    reasons = _validate_annotations(value["ineligibility_reasons"], "ineligibility_reasons")
    _validate_annotations(value["failures"], "failures")
    _validate_annotations(value["exclusions"], "exclusions")
    require_bool(value["trace_integrity"], "trace_integrity")
    if eligible and reasons:
        raise ContractError("eligible attempt cannot have ineligibility reasons")
    if not eligible and not reasons:
        raise ContractError("ineligible attempt requires at least one reason")
    return value


def _validate_weights(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ContractError("metric weights must be an object")
    require_exact_keys(value, METRICS, "metric weights")
    weights = {metric: require_int(value[metric], metric, 0, 10000) for metric in METRICS}
    if sum(weights.values()) != 10000:
        raise ContractError("metric weights must total 10000 basis points")
    return weights


def _validate_reference(value: Any, label: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    require_exact_keys(value, ("path", "sha256"), label)
    relative = value["path"]
    if not isinstance(relative, str) or not relative.endswith(".json"):
        raise ContractError(f"{label}.path must name a JSON file")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or not pure.parts:
        raise ContractError(f"{label}.path must be a contained relative path")
    if any(not part or part.startswith(".") for part in pure.parts):
        raise ContractError(f"{label}.path contains a hidden or empty component")
    return relative, require_sha256(value["sha256"], f"{label}.sha256")


def _load_evaluation(manifest_path: str | os.PathLike[str]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_file = Path(manifest_path)
    root = manifest_file.absolute().parent
    manifest, _ = strict_read_json(
        manifest_file,
        "evaluation manifest",
        max_bytes=MAX_MANIFEST_BYTES,
        root=root,
    )
    require_exact_keys(
        manifest,
        ("schema_version", "evaluation_id", "metric_weights_basis_points", "pairs"),
        "evaluation manifest",
    )
    if manifest["schema_version"] != "ao.lore.evaluation-manifest.v0.1":
        raise ContractError("invalid evaluation manifest schema_version")
    require_identifier(manifest["evaluation_id"], "evaluation_id")
    _validate_weights(manifest["metric_weights_basis_points"])
    pairs = _require_list(manifest["pairs"], "pairs", maximum=MAX_PAIRS)
    if not pairs:
        raise ContractError("evaluation requires at least one pair")
    attempts: dict[str, dict[str, Any]] = {}
    attempt_ids: set[str] = set()
    for index, pair in enumerate(pairs):
        if not isinstance(pair, dict):
            raise ContractError(f"pair {index} must be an object")
        require_exact_keys(pair, ("format", "baseline", "challenger"), f"pair {index}")
        if pair["format"] not in FORMATS:
            raise ContractError(f"pair {index} has unsupported format")
        for side in ("baseline", "challenger"):
            relative, expected_digest = _validate_reference(pair[side], f"pair {index}.{side}")
            if relative in attempts:
                raise ContractError("attempt evidence path must be unique")
            attempt, body = strict_read_json(
                root / relative,
                f"pair {index} {side} attempt",
                max_bytes=MAX_ATTEMPT_BYTES,
                root=root,
            )
            actual_digest = hashlib.sha256(body).hexdigest()
            if actual_digest != expected_digest:
                raise ContractError(f"pair {index} {side} attempt SHA-256 mismatch")
            _validate_attempt(attempt)
            if attempt["format"] != pair["format"]:
                raise ContractError(f"pair {index} {side} format does not match manifest")
            if attempt["attempt_id"] in attempt_ids:
                raise ContractError(f"duplicate attempt_id: {attempt['attempt_id']}")
            attempt_ids.add(attempt["attempt_id"])
            attempts[relative] = attempt
    return manifest, attempts


def _composite(attempt: Mapping[str, Any], weights: Mapping[str, int]) -> int:
    numerator = 0
    denominator = 0
    for metric in METRICS:
        entry = attempt["metrics"][metric]
        weight = weights[metric]
        if not entry["applicable"] or weight == 0:
            continue
        numerator += entry["score_basis_points"] * weight
        denominator += weight
    if denominator == 0:
        raise ContractError("attempt has no applicable weighted metrics")
    return numerator // denominator


def _validate_pair(format_name: str, baseline: Mapping[str, Any], challenger: Mapping[str, Any]) -> None:
    bindings = ("format", "fixture_corpus_sha256", "workload_profile_sha256", "source_input_sha256")
    if baseline["format"] != format_name or challenger["format"] != format_name:
        raise ContractError("attempts are not a matched pair")
    if any(baseline[field] != challenger[field] for field in bindings[1:]):
        raise ContractError("attempts are not a matched pair")
    if baseline["system_id"] == challenger["system_id"]:
        raise ContractError("baseline and challenger system_id must differ")
    for metric in METRICS:
        if baseline["metrics"][metric]["applicable"] != challenger["metrics"][metric]["applicable"]:
            raise ContractError(f"matched metric applicability differs for {metric}")


def _outcome(attempt: Mapping[str, Any], weights: Mapping[str, int]) -> dict[str, Any]:
    raw = _composite(attempt, weights)
    comparison = raw
    if not attempt["eligible"] or not attempt["trace_integrity"] or attempt["failures"]:
        comparison = 0
    return {
        "attempt_id": attempt["attempt_id"],
        "system_id": attempt["system_id"],
        "result_sha256": attempt["result_sha256"],
        "metrics": copy.deepcopy(attempt["metrics"]),
        "role_configuration": copy.deepcopy(attempt["role_configuration"]),
        "eligible": attempt["eligible"],
        "ineligibility_reasons": list(attempt["ineligibility_reasons"]),
        "failures": list(attempt["failures"]),
        "exclusions": list(attempt["exclusions"]),
        "trace_integrity": attempt["trace_integrity"],
        "raw_composite_score": raw,
        "comparison_score": comparison,
    }


def compare_evaluation(
    manifest_path: str | os.PathLike[str],
    *,
    output_path: str | os.PathLike[str] | None = None,
    output_root: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Compare digest-bound matched attempts without launching providers."""

    manifest, attempts = _load_evaluation(manifest_path)
    weights = _validate_weights(manifest["metric_weights_basis_points"])
    report: dict[str, Any] = {
        "schema_version": "ao.lore.evaluation-comparison.v0.1",
        "evaluation_id": manifest["evaluation_id"],
        "metric_weights_basis_points": copy.deepcopy(weights),
        "pairs": [],
        "baseline_system_id": "",
        "challenger_system_id": "",
        "baseline_total": 0,
        "challenger_total": 0,
        "winner": "tie",
        "result": "tie",
        "authority": "evaluation-evidence-only",
    }
    for index, reference in enumerate(manifest["pairs"]):
        baseline = attempts[reference["baseline"]["path"]]
        challenger = attempts[reference["challenger"]["path"]]
        _validate_pair(reference["format"], baseline, challenger)
        if index == 0:
            report["baseline_system_id"] = baseline["system_id"]
            report["challenger_system_id"] = challenger["system_id"]
        elif (
            baseline["system_id"] != report["baseline_system_id"]
            or challenger["system_id"] != report["challenger_system_id"]
        ):
            raise ContractError("system roles must remain stable across pairs")
        left = _outcome(baseline, weights)
        right = _outcome(challenger, weights)
        winner, result = "tie", "tie"
        if left["comparison_score"] > right["comparison_score"]:
            winner, result = baseline["system_id"], "baseline_win"
        elif right["comparison_score"] > left["comparison_score"]:
            winner, result = challenger["system_id"], "challenger_win"
        report["pairs"].append(
            {
                "format": reference["format"],
                "fixture_corpus_sha256": baseline["fixture_corpus_sha256"],
                "workload_profile_sha256": baseline["workload_profile_sha256"],
                "source_input_sha256": baseline["source_input_sha256"],
                "baseline": left,
                "challenger": right,
                "winner": winner,
                "result": result,
            }
        )
        report["baseline_total"] += left["comparison_score"]
        report["challenger_total"] += right["comparison_score"]
    report["pairs"].sort(key=lambda pair: pair["format"])
    if report["baseline_total"] > report["challenger_total"]:
        report["winner"], report["result"] = report["baseline_system_id"], "win"
    elif report["challenger_total"] > report["baseline_total"]:
        report["winner"], report["result"] = report["challenger_system_id"], "win"
    if output_path is not None:
        root = Path(output_root) if output_root is not None else Path(manifest_path).absolute().parent
        write_exclusive_json(output_path, report, root=root)
    return report
