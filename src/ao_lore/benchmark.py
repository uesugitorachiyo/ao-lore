"""Reproducible, digest-bound parser benchmark helpers.

This module compares benchmark evidence. It is intentionally not imported by
the production ingestion path and never selects or invokes production parsers.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections import defaultdict
from typing import Any, Callable, Mapping, Sequence


class BenchmarkError(ValueError):
    """Raised when benchmark evidence is incomplete, unmatched, or altered."""


def _finite(value: Any, field: str, *, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchmarkError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise BenchmarkError(f"{field} must be finite")
    if low is not None and result < low:
        raise BenchmarkError(f"{field} must be >= {low}")
    if high is not None and result > high:
        raise BenchmarkError(f"{field} must be <= {high}")
    return result


def canonical_digest(value: Any) -> str:
    """Return a SHA-256 digest over strict canonical JSON bytes."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BenchmarkError("value is not strict canonical JSON") from exc
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_metrics(
    raw_metrics: Mapping[str, Any], normalized_scores: Mapping[str, Any], exclusions: Sequence[str]
) -> None:
    exclusion_set = set(exclusions)
    if len(exclusion_set) != len(exclusions):
        raise BenchmarkError("exclusions must be unique")
    for name, value in raw_metrics.items():
        if value is None:
            if name not in exclusion_set:
                raise BenchmarkError(f"non-applicable raw metric {name} must be excluded")
        else:
            _finite(value, f"raw_metrics.{name}")
    for name, value in normalized_scores.items():
        if name in exclusion_set:
            raise BenchmarkError(f"excluded metric {name} cannot have a normalized score")
        _finite(value, f"normalized_scores.{name}", low=0, high=1)


def build_manifest(
    *,
    fixture_corpus_digest: str,
    parser_id: str,
    parser_version: str,
    parser_configuration_digest: str,
    runtime: str,
    platform: str,
    document_ir_version: str,
    commands: Sequence[Sequence[str]],
    raw_metrics: Mapping[str, float | int | None],
    normalized_scores: Mapping[str, float],
    failures: Sequence[str],
    exclusions: Sequence[str],
) -> dict[str, Any]:
    """Build a self-digesting benchmark manifest after strict input checks."""

    for field, value in {
        "fixture_corpus_digest": fixture_corpus_digest,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "parser_configuration_digest": parser_configuration_digest,
        "runtime": runtime,
        "platform": platform,
        "document_ir_version": document_ir_version,
    }.items():
        if not isinstance(value, str) or not value:
            raise BenchmarkError(f"{field} must be a non-empty string")
    for field, digest in {
        "fixture_corpus_digest": fixture_corpus_digest,
        "parser_configuration_digest": parser_configuration_digest,
    }.items():
        if len(digest) != 71 or not digest.startswith("sha256:") or any(ch not in "0123456789abcdef" for ch in digest[7:]):
            raise BenchmarkError(f"{field} must be a lowercase SHA-256 digest")
    command_vectors = [list(vector) for vector in commands]
    if not command_vectors or any(not vector or any(not isinstance(arg, str) or not arg for arg in vector) for vector in command_vectors):
        raise BenchmarkError("commands must be non-empty shell-free argument vectors")
    if not isinstance(raw_metrics, Mapping) or not isinstance(normalized_scores, Mapping):
        raise BenchmarkError("metrics must be objects")
    _validate_metrics(raw_metrics, normalized_scores, exclusions)
    manifest = {
        "schema_version": "ao.lore.parser-benchmark-manifest.v0.1",
        "fixture_corpus_digest": fixture_corpus_digest,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "parser_configuration_digest": parser_configuration_digest,
        "runtime": runtime,
        "platform": platform,
        "document_ir_version": document_ir_version,
        "commands": command_vectors,
        "raw_metrics": dict(sorted(raw_metrics.items())),
        "normalized_scores": dict(sorted(normalized_scores.items())),
        "failures": sorted(set(failures)),
        "exclusions": sorted(set(exclusions)),
    }
    manifest["result_digest"] = canonical_digest(manifest)
    return manifest


def verify_manifest(
    manifest: Mapping[str, Any],
    *,
    expected_corpus_digest: str | None = None,
    expected_configuration_digest: str | None = None,
) -> None:
    """Fail closed when a result or an optional expected binding drifts."""

    required = {
        "schema_version",
        "fixture_corpus_digest",
        "parser_id",
        "parser_version",
        "parser_configuration_digest",
        "runtime",
        "platform",
        "document_ir_version",
        "commands",
        "raw_metrics",
        "normalized_scores",
        "failures",
        "exclusions",
        "result_digest",
    }
    if set(manifest) != required:
        raise BenchmarkError("benchmark manifest has missing or unknown fields")
    if manifest["schema_version"] != "ao.lore.parser-benchmark-manifest.v0.1":
        raise BenchmarkError("unsupported benchmark manifest version")
    _validate_metrics(manifest["raw_metrics"], manifest["normalized_scores"], manifest["exclusions"])
    body = {key: value for key, value in manifest.items() if key != "result_digest"}
    expected_result = canonical_digest(body)
    supplied_result = manifest["result_digest"]
    if not isinstance(supplied_result, str) or not hmac.compare_digest(supplied_result, expected_result):
        raise BenchmarkError("benchmark result digest mismatch")
    if expected_corpus_digest is not None and not hmac.compare_digest(manifest["fixture_corpus_digest"], expected_corpus_digest):
        raise BenchmarkError("fixture corpus digest mismatch")
    if expected_configuration_digest is not None and not hmac.compare_digest(
        manifest["parser_configuration_digest"], expected_configuration_digest
    ):
        raise BenchmarkError("parser configuration digest mismatch")


def _applicable_score(manifest: Mapping[str, Any], weights: Mapping[str, Any]) -> tuple[float, list[str]]:
    numerator = 0.0
    denominator = 0.0
    applicable: list[str] = []
    excluded = set(manifest["exclusions"])
    for name, raw_weight in weights.items():
        weight = _finite(raw_weight, f"weights.{name}", low=0)
        if weight == 0 or name in excluded or name not in manifest["normalized_scores"]:
            continue
        score = _finite(manifest["normalized_scores"][name], name, low=0, high=1)
        numerator += weight * score
        denominator += weight
        applicable.append(name)
    if denominator <= 0:
        raise BenchmarkError("no weighted metrics apply to benchmark attempt")
    return round(numerator / denominator, 12), sorted(applicable)


def compare_manifests(
    left: Mapping[str, Any], right: Mapping[str, Any], weights: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare matched attempts while retaining failures and exclusions."""

    verify_manifest(left)
    verify_manifest(right)
    for field in ("fixture_corpus_digest", "runtime", "platform", "document_ir_version"):
        if left[field] != right[field]:
            raise BenchmarkError(f"benchmark attempts do not match on {field}")
    left_score, left_metrics = _applicable_score(left, weights)
    right_score, right_metrics = _applicable_score(right, weights)
    common = sorted(set(left_metrics) & set(right_metrics))
    deltas = {
        name: round(right["normalized_scores"][name] - left["normalized_scores"][name], 12)
        for name in common
    }
    left_name = f"{left['parser_id']}@{left['parser_version']}"
    right_name = f"{right['parser_id']}@{right['parser_version']}"
    if right_score > left_score:
        winner = right_name
    elif left_score > right_score:
        winner = left_name
    else:
        winner = "tie"
    return {
        "schema_version": "ao.lore.parser-benchmark-comparison.v0.1",
        "fixture_corpus_digest": left["fixture_corpus_digest"],
        "document_ir_version": left["document_ir_version"],
        "weights": dict(sorted(weights.items())),
        "attempts": [
            {
                "parser": left_name,
                "result_digest": left["result_digest"],
                "configuration_digest": left["parser_configuration_digest"],
                "applicable_score": left_score,
                "applicable_metrics": left_metrics,
                "failures": list(left["failures"]),
                "exclusions": list(left["exclusions"]),
            },
            {
                "parser": right_name,
                "result_digest": right["result_digest"],
                "configuration_digest": right["parser_configuration_digest"],
                "applicable_score": right_score,
                "applicable_metrics": right_metrics,
                "failures": list(right["failures"]),
                "exclusions": list(right["exclusions"]),
            },
        ],
        "metric_deltas": deltas,
        "winner": winner,
    }


def evaluate_regressions(
    baseline: Mapping[str, Any], current: Mapping[str, Any], allowed_drops: Mapping[str, Any]
) -> dict[str, Any]:
    """Compare two versions of one parser against per-metric thresholds."""

    verify_manifest(baseline)
    verify_manifest(current)
    for field in ("fixture_corpus_digest", "parser_id", "parser_configuration_digest", "runtime", "platform", "document_ir_version"):
        if baseline[field] != current[field]:
            raise BenchmarkError(f"regression attempts do not match on {field}")
    regressions = []
    for metric, raw_allowed_drop in sorted(allowed_drops.items()):
        if metric in baseline["exclusions"] or metric in current["exclusions"]:
            continue
        if metric not in baseline["normalized_scores"] or metric not in current["normalized_scores"]:
            continue
        allowed_drop = _finite(raw_allowed_drop, f"allowed_drops.{metric}", low=0, high=1)
        actual_drop = baseline["normalized_scores"][metric] - current["normalized_scores"][metric]
        if actual_drop > allowed_drop:
            regressions.append(
                {"metric": metric, "actual_drop": round(actual_drop, 12), "allowed_drop": allowed_drop}
            )
    return {
        "schema_version": "ao.lore.parser-benchmark-regression.v0.1",
        "parser_id": baseline["parser_id"],
        "baseline_result_digest": baseline["result_digest"],
        "current_result_digest": current["result_digest"],
        "passed": not regressions,
        "regressions": regressions,
    }


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        raise BenchmarkError("percentile requires values")
    ordered = sorted(values)
    index = math.ceil((percentile / 100) * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def run_fixture_corpus(
    corpus: Mapping[str, Any], adapter: Callable[[Mapping[str, Any]], Mapping[str, Any]]
) -> dict[str, Any]:
    """Run a scripted adapter over immutable case objects and aggregate metrics."""

    fixtures = corpus.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        raise BenchmarkError("corpus must contain fixtures")
    samples: dict[str, list[float]] = defaultdict(list)
    failures: list[str] = []
    for fixture in fixtures:
        fixture_id = fixture.get("id")
        if not isinstance(fixture_id, str) or not fixture_id:
            raise BenchmarkError("fixture ID is required")
        try:
            result = adapter(json.loads(json.dumps(fixture, sort_keys=True)))
        except Exception as exc:  # adapters are an explicit benchmark isolation boundary
            failures.append(f"{fixture_id}:{type(exc).__name__}")
            continue
        if not isinstance(result, Mapping):
            raise BenchmarkError(f"adapter result for {fixture_id} must be an object")
        for metric, value in result.items():
            samples[str(metric)].append(_finite(value, f"{fixture_id}.{metric}"))
    raw_metrics: dict[str, float | int] = {
        "fixture_count": len(fixtures),
        "successful_fixture_count": len(fixtures) - len(failures),
        "failure_rate": len(failures) / len(fixtures),
    }
    for metric, values in sorted(samples.items()):
        if metric == "latency_ms":
            raw_metrics["p50_latency_ms"] = _percentile(values, 50)
            raw_metrics["p95_latency_ms"] = _percentile(values, 95)
        else:
            raw_metrics[metric] = sum(values) / len(values)
    return {
        "fixture_corpus_digest": canonical_digest(corpus),
        "raw_metrics": raw_metrics,
        "failures": failures,
    }
