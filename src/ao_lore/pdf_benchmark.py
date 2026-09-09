"""Calibration runner for public-safe local PDF fixtures."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import time
import tracemalloc
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Callable, Mapping

from ._strict_io import ContractError, strict_read_json
from .benchmark import build_manifest, canonical_digest
from .docling_pdf import configuration_digest
from .parsing import (
    ParseOutput,
    ParserAdapter,
    ParsingError,
    base_capability,
    build_document_ir,
)


CORPUS_KEYS = {"schema_version", "configuration", "metrics", "fixtures"}
CONFIG_KEYS = {
    "max_file_size", "max_num_pages", "max_blocks", "latency_max_seconds",
    "memory_max_bytes", "minimum_scores",
}
FIXTURE_KEYS = {
    "id", "file", "sha256", "kind", "title", "lines", "link",
    "expected_text", "required_block_types", "applicable_metrics",
    "expected_outcome", "expected_failure",
}
METRICS = {
    "structural_fidelity", "text_fidelity", "source_location_fidelity",
    "reliability", "deterministic_repeatability", "latency", "memory",
    "external_cost",
}
FIXTURE_METRICS = {
    "structural_fidelity", "text_fidelity", "source_location_fidelity"
}
FIXTURE_KINDS = {
    "text", "multi_page", "two_column", "table", "unicode", "long",
    "empty", "truncated", "encrypted_stub", "image_only",
}
EXPECTED_FAILURES = {
    "conversion_failed", "no_readable_content", "encrypted_document", "invalid_pdf",
}
PRIVATE_ACCEPTED_KEYS = {"item_id", "source_digest", "status", "metrics"}
PRIVATE_REJECTED_KEYS = {"item_id", "source_digest", "status", "error_code"}
PUBLIC_FAILURE_MESSAGES = {
    "PDF conversion produced no readable content": "no_readable_content",
    "limited PDF has no readable generated text": "no_readable_content",
    "PDF conversion failed": "conversion_failed",
}


class PdfBenchmarkRejection(ParsingError):
    """Structured benchmark rejection with a closed, public failure code."""

    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or code not in EXPECTED_FAILURES:
            raise ValueError("PDF benchmark rejection code is invalid")
        self.code = code
        super().__init__(code)


class PdfBenchmarkContractError(ParsingError):
    """Raised when an adapter mutates or contradicts benchmark request state."""


def _parse_with_fresh_source(
    adapter: ParserAdapter, expected_source: Mapping[str, Any]
) -> Any:
    request = dict(expected_source)
    output = adapter.parse(request)
    if request != expected_source:
        raise PdfBenchmarkContractError("PDF benchmark adapter mutated source")
    return output


def _parse_output_digest(output: Any) -> str:
    """Seal the complete ParseOutput shape crossing the measurement boundary."""

    if not isinstance(output, ParseOutput):
        raise PdfBenchmarkContractError(
            "PDF benchmark operation result shape is invalid"
        )
    try:
        return canonical_digest({
            "document_ir": output.document_ir,
            "quality_components": output.quality_components,
            "critical_failures": list(output.critical_failures),
        })
    except Exception as exc:
        raise PdfBenchmarkContractError(
            "PDF benchmark operation result is not canonical"
        ) from exc


def _detach_parse_output(output: ParseOutput, expected_digest: str) -> ParseOutput:
    """Create the unexposed canonical snapshot retained for scoring."""

    try:
        payload = json.loads(json.dumps(
            {
                "document_ir": output.document_ir,
                "quality_components": output.quality_components,
                "critical_failures": list(output.critical_failures),
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ))
        if set(payload) != {
            "document_ir", "quality_components", "critical_failures"
        } or type(payload["critical_failures"]) is not list:
            raise ValueError("detached ParseOutput shape differs")
        detached = ParseOutput(
            payload["document_ir"],
            payload["quality_components"],
            tuple(payload["critical_failures"]),
        )
        if _parse_output_digest(detached) != expected_digest:
            raise ValueError("detached ParseOutput digest differs")
        return detached
    except Exception as exc:
        raise PdfBenchmarkContractError(
            "PDF benchmark operation result could not be detached"
        ) from exc


def _validate_private_results(results: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy and validate the path-free private calibration item union."""

    if type(results) is not list or not results:
        raise ParsingError("private PDF calibration results must be a non-empty list")
    validated: list[dict[str, Any]] = []
    item_ids: set[str] = set()
    for item in results:
        if not isinstance(item, Mapping):
            raise ParsingError("private PDF calibration item must be an object")
        status = item.get("status")
        expected_keys = (
            PRIVATE_ACCEPTED_KEYS if status == "accepted"
            else PRIVATE_REJECTED_KEYS if status == "rejected"
            else None
        )
        if expected_keys is None or set(item) != expected_keys:
            raise ParsingError("private PDF calibration item union is invalid")
        item_id = item["item_id"]
        if (
            not isinstance(item_id, str)
            or not item_id
            or len(item_id) > 1024
            or any(0xD800 <= ord(character) <= 0xDFFF for character in item_id)
            or item_id in item_ids
        ):
            raise ParsingError("private PDF calibration item ID is invalid")
        digest = item["source_digest"]
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        ):
            raise ParsingError("private PDF calibration source digest is invalid")
        item_ids.add(item_id)
        if status == "accepted":
            metrics = item["metrics"]
            if (
                not isinstance(metrics, Mapping)
                or not metrics
                or any(not isinstance(name, str) or name not in METRICS for name in metrics)
            ):
                raise ParsingError("private PDF calibration metrics are invalid")
            copied_metrics: dict[str, float] = {}
            for name, value in metrics.items():
                score = _finite(value, f"private PDF calibration metric {name}")
                if not 0 <= score <= 1:
                    raise ParsingError("private PDF calibration metric is outside 0..1")
                copied_metrics[name] = score
            validated.append({
                "item_id": item_id,
                "source_digest": digest,
                "status": status,
                "metrics": copied_metrics,
            })
        else:
            error_code = item["error_code"]
            if not isinstance(error_code, str) or error_code not in EXPECTED_FAILURES:
                raise ParsingError("private PDF calibration error code is invalid")
            validated.append({
                "item_id": item_id,
                "source_digest": digest,
                "status": status,
                "error_code": error_code,
            })
    return validated


def build_private_pdf_aggregate(
    results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a deterministic aggregate without private item-level material."""

    validated = _validate_private_results(results)
    samples: dict[str, list[float]] = {name: [] for name in METRICS}
    failure_counts = {code: 0 for code in sorted(EXPECTED_FAILURES)}
    for item in validated:
        if item["status"] == "accepted":
            for name, value in item["metrics"].items():
                samples[name].append(value)
        else:
            failure_counts[item["error_code"]] += 1
    metrics = {
        name: sum(values) / len(values)
        for name, values in sorted(samples.items())
        if values
    }
    exclusions = sorted(name for name, values in samples.items() if not values)
    successful = sum(item["status"] == "accepted" for item in validated)
    return {
        "schema_version": "ao.lore.private-pdf-calibration-aggregate.v0.1",
        "corpus_digest": canonical_digest([
            {"item_id": item["item_id"], "source_digest": item["source_digest"]}
            for item in validated
        ]),
        "total": len(validated),
        "successful": successful,
        "rejected": len(validated) - successful,
        "metrics": metrics,
        "exclusions": exclusions,
        "failure_counts": failure_counts,
        "ocr_used": False,
        "provider_calls": False,
        "claims_authority_advance": False,
    }


def private_pdf_calibration_not_supplied() -> dict[str, Any]:
    """Project optional private calibration absence without implying a pass."""

    return {
        "schema_version": "ao.lore.private-pdf-calibration-status.v0.1",
        "status": "not_supplied",
        "qualified": False,
        "ocr_used": False,
        "provider_calls": False,
        "claims_authority_advance": False,
    }


class LimitedPdfAdapter:
    """Deliberately limited local baseline for simple generated PDFs."""

    def __init__(self, *, max_file_size: int = 50 * 1024 * 1024) -> None:
        self._max_file_size = max_file_size
        self._capability = base_capability(
            "limited-pdf", "0.1.0", ["application/pdf"], [".pdf"], ["text"]
        )

    @property
    def capability(self) -> Mapping[str, Any]:
        return json.loads(json.dumps(self._capability))

    def parse(self, source: Mapping[str, Any]) -> ParseOutput:
        if not isinstance(source, Mapping) or set(source) != {"resource", "digest", "media_type", "data"}:
            raise ParsingError("limited PDF source contract is invalid")
        data = source["data"]
        if source["media_type"] != "application/pdf" or type(data) is not bytes or len(data) > self._max_file_size:
            raise ParsingError("limited PDF source is invalid")
        if source["digest"] != "sha256:" + hashlib.sha256(data).hexdigest():
            raise ParsingError("limited PDF source digest mismatch")
        stripped = data.rstrip()
        if (
            not data.startswith(b"%PDF-")
            or not stripped.endswith(b"%%EOF")
            or b"\nxref\n" not in data
            or b"\ntrailer\n<<" not in data
            or b"\nstartxref\n" not in data
            or b"/Encrypt" in data
        ):
            raise ParsingError("PDF conversion failed")
        stream_bodies = re.findall(
            rb"\bstream\r?\n(.*?)\r?\nendstream\b", data, flags=re.DOTALL
        )
        has_stream_content = any(
            line.strip() and not line.lstrip().startswith(b"%")
            for body in stream_bodies
            for line in body.splitlines()
        )
        if not has_stream_content:
            raise ParsingError("PDF conversion failed")
        blocks = []
        for match in re.finditer(rb"\(([^()]*)\)\s*Tj", data):
            try:
                text = match.group(1).replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\").decode("ascii")
            except UnicodeDecodeError as exc:
                raise ParsingError("limited PDF text is not ASCII") from exc
            blocks.append({
                "id": f"b{len(blocks) + 1}", "type": "paragraph", "text": text,
                "source_span": {"start": match.start(1), "end": match.end(1), "page": 1},
            })
        if not blocks:
            raise ParsingError("limited PDF has no readable generated text")
        ir = build_document_ir(source, self._capability, blocks, {
            "format": "pdf", "page_count": 1, "ocr_used": False,
            "baseline": "limited-literal-text",
        })
        return ParseOutput(ir, {
            "text_coverage": 1.0,
            "structural_completeness": 0.0,
            "source_span_coverage": 1.0,
            "document_ir_valid": 1.0,
        })


def _finite(value: Any, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ParsingError(f"{label} must be finite")
    result = float(value)
    if positive and result <= 0:
        raise ParsingError(f"{label} must be positive")
    return result


def _validate_corpus(
    corpus: Mapping[str, Any],
    fixture_root: Path,
    fixture_data: Mapping[str, bytes] | None = None,
) -> dict[str, Any]:
    if not isinstance(corpus, Mapping) or set(corpus) != CORPUS_KEYS:
        raise ParsingError("PDF fixture corpus keys differ")
    if corpus["schema_version"] != "ao.lore.pdf-fixture-corpus.v0.1":
        raise ParsingError("unsupported PDF fixture corpus")
    configuration = corpus["configuration"]
    if not isinstance(configuration, Mapping) or set(configuration) != CONFIG_KEYS:
        raise ParsingError("PDF benchmark configuration keys differ")
    for key in ("max_file_size", "max_num_pages", "max_blocks"):
        value = configuration[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ParsingError(f"PDF benchmark {key} must be a positive integer")
    _finite(configuration["latency_max_seconds"], "latency_max_seconds", positive=True)
    _finite(configuration["memory_max_bytes"], "memory_max_bytes", positive=True)
    minimums = configuration["minimum_scores"]
    if not isinstance(minimums, Mapping) or any(key not in METRICS for key in minimums):
        raise ParsingError("PDF benchmark minimum scores are invalid")
    for key, value in minimums.items():
        score = _finite(value, f"minimum_scores.{key}")
        if not 0 <= score <= 1:
            raise ParsingError("PDF benchmark minimum score is outside 0..1")
    if (
        not isinstance(corpus["metrics"], list)
        or any(not isinstance(metric, str) for metric in corpus["metrics"])
        or set(corpus["metrics"]) != METRICS
        or len(corpus["metrics"]) != len(METRICS)
    ):
        raise ParsingError("PDF benchmark metric set differs")
    fixtures = corpus["fixtures"]
    if not isinstance(fixtures, list) or not fixtures:
        raise ParsingError("PDF fixture corpus is empty")
    seen: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, Mapping) or set(fixture) != FIXTURE_KEYS:
            raise ParsingError("PDF fixture keys differ")
        fixture_id = fixture["id"]
        filename = fixture["file"]
        if not isinstance(fixture_id, str) or not fixture_id or fixture_id in seen:
            raise ParsingError("PDF fixture identity is invalid")
        seen.add(fixture_id)
        path = PurePosixPath(filename) if isinstance(filename, str) else PurePosixPath(".")
        if path.name != filename or path.suffix.lower() != ".pdf":
            raise ParsingError("PDF fixture filename is invalid")
        if fixture_data is None:
            target = fixture_root / filename
            try:
                metadata = os.lstat(target)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise ParsingError("PDF fixture must be a regular non-link file")
                data = target.read_bytes()
            except OSError as exc:
                raise ParsingError("PDF fixture is unavailable") from exc
        else:
            data = fixture_data.get(filename)
            if type(data) is not bytes:
                raise ParsingError("PDF fixture bytes are unavailable")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if digest != fixture["sha256"]:
            raise ParsingError("PDF fixture digest mismatch")
        if len(data) > configuration["max_file_size"]:
            raise ParsingError("PDF fixture exceeds configured byte limit")
        if not isinstance(fixture["kind"], str) or fixture["kind"] not in FIXTURE_KINDS:
            raise ParsingError("PDF fixture kind is invalid")
        if not isinstance(fixture["title"], str):
            raise ParsingError("PDF fixture title is invalid")
        if (
            not isinstance(fixture["lines"], list)
            or any(not isinstance(line, str) for line in fixture["lines"])
        ):
            raise ParsingError("PDF fixture lines are invalid")
        if fixture["link"] is not None and (
            not isinstance(fixture["link"], str) or not fixture["link"].isascii()
        ):
            raise ParsingError("PDF fixture link is invalid")
        if (
            not isinstance(fixture["expected_text"], list)
            or any(not isinstance(item, str) or not item for item in fixture["expected_text"])
        ):
            raise ParsingError("PDF fixture expected text is invalid")
        if (
            not isinstance(fixture["required_block_types"], list)
            or any(not isinstance(item, str) or not item for item in fixture["required_block_types"])
        ):
            raise ParsingError("PDF fixture structural expectations are invalid")
        outcome = fixture["expected_outcome"]
        failure = fixture["expected_failure"]
        if outcome == "accepted":
            if failure is not None or not fixture["expected_text"] or not fixture["required_block_types"]:
                raise ParsingError("accepted PDF fixture outcome is inconsistent")
        elif outcome == "rejected":
            if (
                not isinstance(failure, str) or failure not in EXPECTED_FAILURES
                or fixture["expected_text"] or fixture["required_block_types"]
            ):
                raise ParsingError("rejected PDF fixture outcome is inconsistent")
        else:
            raise ParsingError("PDF fixture expected outcome is invalid")
        applicable = fixture["applicable_metrics"]
        if (
            not isinstance(applicable, list) or not applicable
            or any(not isinstance(metric, str) for metric in applicable)
            or len(set(applicable)) != len(applicable)
            or any(metric not in FIXTURE_METRICS for metric in applicable)
        ):
            raise ParsingError("PDF fixture applicability is invalid")
    if not any(fixture["expected_outcome"] == "accepted" for fixture in fixtures):
        raise ParsingError("PDF fixture corpus has no accepted fixtures")
    if fixture_data is not None and set(fixture_data) != {
        fixture["file"] for fixture in fixtures
    }:
        raise ParsingError("PDF fixture byte identities differ")
    return json.loads(json.dumps(corpus))


def load_pdf_corpus(
    path: str | Path,
    *,
    fixture_root: str | Path | None = None,
) -> dict[str, Any]:
    target = Path(path)
    try:
        corpus, _ = strict_read_json(target, "PDF fixture corpus", max_bytes=1024 * 1024)
    except (ContractError, OSError) as exc:
        raise ParsingError("PDF fixture corpus is invalid") from exc
    return _validate_corpus(corpus, Path(fixture_root) if fixture_root is not None else target.parent)


def _score_fixture(
    output: Any, fixture: Mapping[str, Any]
) -> tuple[float | None, float | None, float | None]:
    applicable = set(fixture["applicable_metrics"])
    ir = output.document_ir
    blocks = ir.get("blocks") if isinstance(ir, Mapping) else None
    if not isinstance(blocks, list):
        return tuple(0.0 if metric in applicable else None for metric in (
            "structural_fidelity", "text_fidelity", "source_location_fidelity"
        ))
    text = "\n".join(str(block.get("text", "")) for block in blocks if isinstance(block, Mapping))
    expected = fixture["expected_text"]
    text_score = sum(item in text for item in expected) / len(expected)
    actual_types = {block.get("type") for block in blocks if isinstance(block, Mapping)}
    required = fixture["required_block_types"]
    structural = sum(item in actual_types for item in required) / len(required)
    located = sum(
        isinstance(block, Mapping)
        and isinstance(block.get("source_span"), Mapping)
        and ("page" in block["source_span"] or "coordinates" in block["source_span"])
        for block in blocks
    )
    return (
        structural if "structural_fidelity" in applicable else None,
        text_score if "text_fidelity" in applicable else None,
        located / max(1, len(blocks)) if "source_location_fidelity" in applicable else None,
    )


def _failure_code(exc: Exception) -> str:
    """Map an adapter failure to the small public benchmark failure vocabulary."""

    if isinstance(exc, PdfBenchmarkRejection):
        return exc.code
    return PUBLIC_FAILURE_MESSAGES.get(str(exc).strip(), "conversion_failed")


def run_pdf_benchmark(
    adapter: ParserAdapter,
    corpus: Mapping[str, Any],
    *,
    fixture_root: str | Path,
    runtime: str,
    platform: str,
    fixture_data: Mapping[str, bytes] | None = None,
    measurement: Callable[[Callable[[], Any]], tuple[Any, float, int]] | None = None,
    measurement_samples: list[tuple[float, int]] | None = None,
    result_validator: Callable[[Any, Mapping[str, Any]], Any] | None = None,
    complete_attempts_on_failure: bool = False,
) -> dict[str, Any]:
    if not isinstance(complete_attempts_on_failure, bool):
        raise ParsingError("PDF benchmark attempt policy is invalid")
    if measurement is not None and not callable(measurement):
        raise ParsingError("PDF benchmark measurement seam is invalid")
    if measurement_samples is not None and (
        type(measurement_samples) is not list or measurement_samples
    ):
        raise ParsingError("PDF benchmark measurement sample sink is invalid")
    if result_validator is not None and not callable(result_validator):
        raise ParsingError("PDF benchmark result validator is invalid")
    if fixture_data is not None and not isinstance(fixture_data, Mapping):
        raise ParsingError("PDF benchmark fixture byte mapping is invalid")
    validated = _validate_corpus(corpus, Path(fixture_root), fixture_data)
    capability = adapter.capability
    parser_id = capability.get("parser_id")
    parser_version = capability.get("version")
    if not isinstance(parser_id, str) or not parser_id or not isinstance(parser_version, str) or not parser_version:
        raise ParsingError("PDF benchmark adapter identity is invalid")
    config = validated["configuration"]
    structural_scores: list[float] = []
    text_scores: list[float] = []
    location_scores: list[float] = []
    repeatability: list[float] = []
    successful = 0
    durations: list[float] = []
    peaks: list[int] = []
    failures: list[str] = []
    exclusions: list[str] = []
    for fixture in validated["fixtures"]:
        data = (
            (Path(fixture_root) / fixture["file"]).read_bytes()
            if fixture_data is None
            else fixture_data[fixture["file"]]
        )
        source = MappingProxyType({
            "resource": f"fixtures/{fixture['file']}",
            "digest": fixture["sha256"],
            "media_type": "application/pdf",
            "data": data,
        })
        if fixture["expected_outcome"] == "rejected":
            try:
                _parse_with_fresh_source(adapter, source)
            except PdfBenchmarkContractError:
                raise
            except Exception as exc:
                actual = _failure_code(exc)
                if actual == fixture["expected_failure"]:
                    exclusions.append(
                        f"expected_rejection:{fixture['id']}:{fixture['expected_failure']}"
                    )
                else:
                    failures.append(f"{fixture['id']}:{actual}")
            else:
                failures.append(f"{fixture['id']}:unexpected_acceptance")
            continue
        outputs = []
        errors: list[Exception] = []
        for _ in range(2):
            operation_calls = 0
            operation_result: Any = None
            operation_result_digest: str | None = None
            operation_error: BaseException | None = None

            def observed_parse() -> Any:
                nonlocal operation_calls, operation_result
                nonlocal operation_result_digest, operation_error
                operation_calls += 1
                if operation_calls != 1:
                    raise PdfBenchmarkContractError(
                        "PDF benchmark measurement invoked operation more than once"
                    )
                try:
                    raw_output = _parse_with_fresh_source(adapter, source)
                    if result_validator is None:
                        operation_result = raw_output
                    else:
                        try:
                            operation_result = result_validator(
                                raw_output, MappingProxyType(dict(source))
                            )
                        except BaseException as exc:
                            if not isinstance(exc, Exception):
                                raise
                            raise PdfBenchmarkContractError(
                                "PDF benchmark result validation failed"
                            ) from exc
                    operation_result_digest = _parse_output_digest(operation_result)
                except BaseException as exc:
                    operation_error = exc
                    raise
                return operation_result

            try:
                if measurement is None:
                    def measured_parse() -> tuple[Any, float, int]:
                        tracemalloc.start()
                        started = time.perf_counter()
                        try:
                            parsed = observed_parse()
                        finally:
                            duration = time.perf_counter() - started
                            _, peak = tracemalloc.get_traced_memory()
                            tracemalloc.stop()
                        return parsed, duration, peak
                    measured = measured_parse()
                else:
                    measured = measurement(observed_parse)
            except BaseException as exc:
                if tracemalloc.is_tracing():
                    tracemalloc.stop()
                if (
                    operation_calls != 1
                    or operation_error is None
                    or exc is not operation_error
                ):
                    raise PdfBenchmarkContractError(
                        "PDF benchmark measurement changed operation semantics"
                    ) from (exc if isinstance(exc, Exception) else None)
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, PdfBenchmarkContractError):
                    raise
                errors.append(exc)
                if not complete_attempts_on_failure:
                    break
                continue
            if operation_calls != 1 or operation_error is not None:
                raise PdfBenchmarkContractError(
                    "PDF benchmark measurement did not propagate operation result"
                )
            if type(measured) is not tuple or len(measured) != 3:
                raise ParsingError("PDF benchmark measurement result is invalid")
            output, duration, peak = measured
            if output is not operation_result:
                raise PdfBenchmarkContractError(
                    "PDF benchmark measurement replaced operation result"
                )
            if type(duration) not in (int, float) or not math.isfinite(duration):
                raise ParsingError("PDF benchmark duration must be finite")
            duration = float(duration)
            if duration < 0:
                raise ParsingError("PDF benchmark duration must be non-negative")
            if type(peak) is not int or peak < 0:
                raise ParsingError("PDF benchmark peak memory must be a non-negative integer")
            peak = int(peak)
            if (
                operation_result_digest is None
                or _parse_output_digest(output) != operation_result_digest
            ):
                raise PdfBenchmarkContractError(
                    "PDF benchmark measurement mutated operation result"
                )
            stored_output = _detach_parse_output(output, operation_result_digest)
            durations.append(duration)
            peaks.append(peak)
            outputs.append(stored_output)
            if measurement_samples is not None:
                measurement_samples.append((duration, peak))
        if errors:
            error_codes = [_failure_code(error) for error in errors]
            if complete_attempts_on_failure and (
                len(errors) != 2
                or outputs
                or len(set(error_codes)) != 1
            ):
                raise PdfBenchmarkContractError(
                    "PDF benchmark repeated failure was unstable"
                )
            failures.append(f"{fixture['id']}:{error_codes[0]}")
            applicable = set(fixture["applicable_metrics"])
            if "structural_fidelity" in applicable:
                structural_scores.append(0.0)
            if "text_fidelity" in applicable:
                text_scores.append(0.0)
            if "source_location_fidelity" in applicable:
                location_scores.append(0.0)
            repeatability.append(0.0)
            continue
        successful += 1
        structural, text_score, locations = _score_fixture(outputs[0], fixture)
        if structural is not None:
            structural_scores.append(structural)
        if text_score is not None:
            text_scores.append(text_score)
        if locations is not None:
            location_scores.append(locations)
        repeatability.append(float(canonical_digest(outputs[0].document_ir) == canonical_digest(outputs[1].document_ir)))
    count = sum(
        fixture["expected_outcome"] == "accepted" for fixture in validated["fixtures"]
    )
    raw = {
        "structural_fidelity": sum(structural_scores) / len(structural_scores) if structural_scores else None,
        "text_fidelity": sum(text_scores) / len(text_scores) if text_scores else None,
        "source_location_fidelity": sum(location_scores) / len(location_scores) if location_scores else None,
        "reliability": successful / count,
        "deterministic_repeatability": sum(repeatability) / count,
        "latency": sum(durations) / max(1, len(durations)),
        "memory": max(peaks, default=0),
        "external_cost": 0.0,
    }
    normalized = {
        "reliability": raw["reliability"],
        "deterministic_repeatability": raw["deterministic_repeatability"],
        "latency": max(0.0, 1.0 - raw["latency"] / config["latency_max_seconds"]),
        "memory": max(0.0, 1.0 - raw["memory"] / config["memory_max_bytes"]),
        "external_cost": 1.0,
    }
    for metric in sorted(FIXTURE_METRICS):
        value = raw[metric]
        if value is None:
            exclusions.append(metric)
        else:
            normalized[metric] = value
    for metric, minimum in config["minimum_scores"].items():
        if metric in normalized and normalized[metric] < minimum:
            label = "repeatability" if metric == "deterministic_repeatability" else metric
            failures.append(f"{label}_below_minimum")
    limits_digest = configuration_digest(
        config["max_file_size"], config["max_num_pages"], config["max_blocks"]
    )
    return build_manifest(
        fixture_corpus_digest=canonical_digest(validated),
        parser_id=parser_id,
        parser_version=parser_version,
        parser_configuration_digest=limits_digest,
        runtime=runtime,
        platform=platform,
        document_ir_version="ao.lore.document-ir.v0.1",
        commands=[["ao-lore", "benchmark", "pdf"]],
        raw_metrics=raw,
        normalized_scores=normalized,
        failures=failures,
        exclusions=exclusions,
    )
