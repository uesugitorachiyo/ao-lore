"""Closed, twice-per-item benchmark for the English Paddle OCR candidates."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Mapping, Protocol, Sequence

from .ocr_contracts import AUTHORITY_FIELDS, OCR_POLICY_DIGEST, validate_qualification
from .ocr_fixture_oracle import OcrAttemptMetrics, OcrFixture, OcrFixtureCorpus, ocr_oracle_implementation_digest, score_accepted_attempt


class OcrBenchmarkError(ValueError):
    """Stable benchmark rejection or invalid adapter boundary."""


class OcrRunAdapter(Protocol):
    def run(self, candidate_id: str, fixture: OcrFixture, attempt: int) -> object: ...


_THRESHOLDS = {
    "character_accuracy_millionths": 980000,
    "word_accuracy_millionths": 950000,
    "detection_precision_millionths": 980000,
    "detection_recall_millionths": 950000,
    "reading_order_pair_accuracy_millionths": 1000000,
    "mean_polygon_iou_millionths": 800000,
    "page_coverage_millionths": 1000000,
    "expected_outcome_accuracy_millionths": 1000000,
}
_REJECTION_CATEGORIES = {"no_readable_content", "invalid_source", "encrypted_source", "resource_limit"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CANDIDATE_FIELDS = {
    "candidate_id", "result_digest", "character_accuracy_millionths",
    "word_accuracy_millionths", "detection_precision_millionths",
    "detection_recall_millionths", "reading_order_pair_accuracy_millionths",
    "mean_polygon_iou_millionths", "page_coverage_millionths",
    "expected_outcome_accuracy_millionths", "hallucinated_lines",
    "mean_latency_microseconds", "peak_gpu_memory_bytes",
    "repeatability_identical", "all_hard_gates_pass",
}
OCR_RANK_KEYS = (
    "character_error", "hallucinations", "detection_recall", "reading_order",
    "peak_gpu_memory", "mean_latency", "parser_id",
)


@dataclass(frozen=True)
class OcrCandidatePerformance:
    candidate_id: str
    peak_gpu_memory_bytes: int
    mean_latency_microseconds: int


@dataclass(frozen=True)
class OcrCandidateSelection:
    decision: str
    selected_candidate_id: str | None
    rank_keys: tuple[str, ...] = OCR_RANK_KEYS


def _performance_rows(
    performance: Sequence[OcrCandidatePerformance] | None,
) -> tuple[OcrCandidatePerformance, ...]:
    if performance is None:
        return tuple(OcrCandidatePerformance(f"candidate-{index}", 0, 0) for index in range(1, 4))
    if type(performance) not in (tuple, list) or len(performance) != 3:
        raise OcrBenchmarkError("OCR candidate performance differs")
    rows = tuple(performance)
    for index, row in enumerate(rows, 1):
        if (
            type(row) is not OcrCandidatePerformance
            or row.candidate_id != f"candidate-{index}"
            or type(row.peak_gpu_memory_bytes) is not int
            or row.peak_gpu_memory_bytes < 0
            or type(row.mean_latency_microseconds) is not int
            or row.mean_latency_microseconds < 0
        ):
            raise OcrBenchmarkError("OCR candidate performance differs")
    return rows


def select_ocr_candidate(
    results: Sequence[Mapping[str, object]],
    performance: Sequence[OcrCandidatePerformance] | None = None,
) -> OcrCandidateSelection:
    """Select only among hard-gate-passing candidates using the closed rank."""

    if type(results) not in (tuple, list) or len(results) != 3:
        raise OcrBenchmarkError("OCR candidate results differ")
    rows = tuple(results)
    for index, row in enumerate(rows, 1):
        if type(row) is not dict or set(row) != _CANDIDATE_FIELDS or row.get("candidate_id") != f"candidate-{index}":
            raise OcrBenchmarkError("OCR candidate results differ")
        for field in (
            "character_accuracy_millionths", "detection_recall_millionths",
            "reading_order_pair_accuracy_millionths", "hallucinated_lines",
        ):
            value = row[field]
            maximum = 1_000_000
            if type(value) is not int or not 0 <= value <= maximum:
                raise OcrBenchmarkError("OCR candidate results differ")
        if type(row["repeatability_identical"]) is not bool or type(row["all_hard_gates_pass"]) is not bool:
            raise OcrBenchmarkError("OCR candidate results differ")
        if (
            type(row["mean_latency_microseconds"]) is not int
            or not 0 <= row["mean_latency_microseconds"] <= 240_000_000
            or type(row["peak_gpu_memory_bytes"]) is not int
            or not 0 <= row["peak_gpu_memory_bytes"] <= 34_359_738_368
        ):
            raise OcrBenchmarkError("OCR candidate performance differs")
    measurements = _performance_rows(performance) if performance is not None else tuple(
        OcrCandidatePerformance(
            str(row["candidate_id"]),
            int(row["peak_gpu_memory_bytes"]),
            int(row["mean_latency_microseconds"]),
        )
        for row in rows
    )
    if tuple(row["candidate_id"] for row in rows) != tuple(item.candidate_id for item in measurements):
        raise OcrBenchmarkError("OCR candidate identities differ")
    passing = [(row, measurement) for row, measurement in zip(rows, measurements) if row["all_hard_gates_pass"]]
    if not passing or any(not row["repeatability_identical"] for row in rows):
        return OcrCandidateSelection("investigate", None)

    def rank(item: tuple[Mapping[str, object], OcrCandidatePerformance]) -> tuple[object, ...]:
        row, measured = item
        return (
            1_000_000 - row["character_accuracy_millionths"],
            row["hallucinated_lines"],
            -row["detection_recall_millionths"],
            -row["reading_order_pair_accuracy_millionths"],
            measured.peak_gpu_memory_bytes,
            measured.mean_latency_microseconds,
            row["candidate_id"],
        )

    selected = min(passing, key=rank)[0]["candidate_id"]
    decision = "hold" if len(passing) == len(rows) else "candidate_change"
    return OcrCandidateSelection(decision, str(selected))


def _digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def ocr_benchmark_configuration_digest(
    *, corpus_configuration_digest: str, model_digests: tuple[str, str, str]
) -> str:
    if (
        type(corpus_configuration_digest) is not str
        or not _DIGEST.fullmatch(corpus_configuration_digest)
        or type(model_digests) is not tuple
        or len(model_digests) != 3
        or any(type(item) is not str or not _DIGEST.fullmatch(item) for item in model_digests)
        or len(set(model_digests)) != 3
    ):
        raise OcrBenchmarkError("OCR benchmark configuration differs")
    return _digest({
        "corpus": corpus_configuration_digest,
        "model_digests": list(model_digests),
        "attempts": 2,
        "thresholds": _THRESHOLDS,
        "candidate_ids": ["candidate-1", "candidate-2", "candidate-3"],
    })


def _millionths(value: Fraction) -> int:
    return value.numerator * 1_000_000 // value.denominator


def _zero(outcome: bool, semantic: str) -> OcrAttemptMetrics:
    return OcrAttemptMetrics(*(Fraction(0) for _ in range(7)), 0, Fraction(int(outcome)), semantic)


def _candidate(candidate_id: str, corpus: OcrFixtureCorpus, runner: OcrRunAdapter) -> tuple[dict[str, object], bool]:
    attempts: dict[str, list[OcrAttemptMetrics]] = {fixture.fixture_id: [] for fixture in corpus.fixtures}
    unexpected = False
    semantic_rows = []
    performance_rows: list[tuple[int, int]] = []
    for fixture in corpus.fixtures:
        for attempt in (1, 2):
            try:
                output = runner.run(candidate_id, fixture, attempt)
                if type(output) is not dict:
                    raise TypeError("OCR runner output must be an exact object")
                if fixture.expected_outcome == "rejected":
                    unexpected = True
                    metric = _zero(False, _digest({"unexpected_accept": fixture.fixture_id}))
                else:
                    try:
                        metric = score_accepted_attempt(fixture, output)
                    except ValueError:
                        unexpected = True
                        metric = _zero(False, _digest({"invalid_output": fixture.fixture_id}))
            except OcrBenchmarkError as exc:
                if type(exc) is not OcrBenchmarkError or len(exc.args) != 1 or type(exc.args[0]) is not str or exc.args[0] not in _REJECTION_CATEGORIES:
                    raise OcrBenchmarkError("OCR benchmark adapter boundary failed") from None
                category = exc.args[0]
                correct = fixture.expected_outcome == "rejected" and category == fixture.expected_failure
                unexpected = unexpected or not correct
                metric = _zero(correct, _digest({"rejected": category}))
            except Exception as exc:
                del exc
                raise OcrBenchmarkError("OCR benchmark adapter boundary failed") from None
            attempts[fixture.fixture_id].append(metric)
            semantic_rows.append([fixture.fixture_id, attempt, metric.semantic_digest])
            measurement = getattr(runner, "measurement", None)
            if callable(measurement):
                observed = measurement(candidate_id, fixture, attempt)
                if observed is not None:
                    if (
                        type(observed) is not tuple or len(observed) != 2
                        or any(type(item) is not int or item < 0 for item in observed)
                    ):
                        raise OcrBenchmarkError("OCR benchmark measurement failed")
                    performance_rows.append(observed)
            else:
                performance_rows.append((0, 0))
    repeatability = all(values[0].semantic_digest == values[1].semantic_digest for values in attempts.values())
    accepted_metrics = [metric for fixture in corpus.fixtures if fixture.expected_outcome == "accepted" for metric in attempts[fixture.fixture_id]]
    all_metrics = [metric for values in attempts.values() for metric in values]
    def mean(field: str, values: list[OcrAttemptMetrics]) -> int:
        fractions = [getattr(item, field) for item in values]
        return _millionths(sum(fractions, Fraction(0)) / max(1, len(fractions)))
    result: dict[str, object] = {
        "candidate_id": candidate_id,
        "result_digest": _digest({
            "candidate_id": candidate_id,
            "attempts": semantic_rows,
            "performance": performance_rows,
        }),
        "character_accuracy_millionths": mean("character_accuracy", accepted_metrics),
        "word_accuracy_millionths": mean("word_accuracy", accepted_metrics),
        "detection_precision_millionths": mean("detection_precision", accepted_metrics),
        "detection_recall_millionths": mean("detection_recall", accepted_metrics),
        "reading_order_pair_accuracy_millionths": mean("reading_order_pair_accuracy", accepted_metrics),
        "mean_polygon_iou_millionths": mean("mean_polygon_iou", accepted_metrics),
        "page_coverage_millionths": mean("page_coverage", accepted_metrics),
        "hallucinated_lines": sum(item.hallucinated_lines for item in accepted_metrics),
        "expected_outcome_accuracy_millionths": mean("expected_outcome_accuracy", all_metrics),
        "mean_latency_microseconds": (
            sum(item[0] for item in performance_rows) // len(performance_rows)
            if performance_rows else 0
        ),
        "peak_gpu_memory_bytes": max((item[1] for item in performance_rows), default=0),
        "repeatability_identical": repeatability,
    }
    result["all_hard_gates_pass"] = (
        repeatability and not unexpected and result["hallucinated_lines"] == 0
        and all(result[field] >= threshold for field, threshold in _THRESHOLDS.items())
    )
    return result, not repeatability


def run_ocr_benchmark(
    corpus: OcrFixtureCorpus,
    runner: OcrRunAdapter,
    *,
    runtime_digest: str,
    model_digests: tuple[str, str, str],
    candidate_performance: Sequence[OcrCandidatePerformance] | None = None,
) -> dict[str, object]:
    if type(corpus) is not OcrFixtureCorpus or type(model_digests) is not tuple or len(model_digests) != 3:
        raise OcrBenchmarkError("OCR benchmark inputs differ")
    if (
        type(runtime_digest) is not str
        or not _DIGEST.fullmatch(runtime_digest)
        or any(type(item) is not str or not _DIGEST.fullmatch(item) for item in model_digests)
        or len(set(model_digests)) != 3
    ):
        raise OcrBenchmarkError("OCR benchmark digests differ")
    candidates = []
    investigate = False
    for index in range(1, 4):
        candidate, unstable = _candidate(f"candidate-{index}", corpus, runner)
        candidates.append(candidate); investigate = investigate or unstable
    selection = select_ocr_candidate(candidates, candidate_performance)
    selected = selection.selected_candidate_id or "candidate-1"
    if investigate or selection.selected_candidate_id is None:
        decision = "investigate"
    else:
        decision = selection.decision
    value: dict[str, object] = {
        "schema_version": "ao.lore.private-ocr-qualification.v0.1",
        "policy_digest": OCR_POLICY_DIGEST,
        "runtime_digest": runtime_digest,
        "corpus_digest": corpus.corpus_digest,
        "oracle_digest": ocr_oracle_implementation_digest(),
        "configuration_digest": ocr_benchmark_configuration_digest(
            corpus_configuration_digest=corpus.configuration_digest,
            model_digests=model_digests,
        ),
        "candidates": candidates,
        "selected_candidate_id": selected,
        "decision": decision,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    try:
        return validate_qualification(value)
    except Exception as exc:
        del exc
        raise OcrBenchmarkError("OCR qualification contract failed") from None
