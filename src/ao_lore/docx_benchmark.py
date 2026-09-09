"""Independent representative-domain DOCX benchmark runner."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
import stat
import time
import tracemalloc
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .benchmark import canonical_digest
from .docx_ooxml import (
    DOCX_BENCHMARK_SCHEMA_VERSION,
    DOCX_DOCUMENT_COUNT,
    DOCX_EXPECTED_REJECTION_COUNT,
    DOCX_ACCEPTED_DOCUMENT_COUNT,
    DOCX_ACTIVE_CONTENT_REJECTION_CODE,
    DOCX_INVALID_PACKAGE_REJECTION_CODE,
    DOCX_STABLE_FAILURE_CATEGORIES,
    DOCX_FLOORS,
    DOCX_PARSER_ID,
    DOCX_PARSER_VERSION,
    DocxLimits,
    docx_configuration_digest,
)
from .private_docx_domain import (
    DOCX_MIME,
    DOCX_PRIVATE_CORPUS_ID,
    EXPECTED_REJECTIONS,
    ReviewedDocxSource,
    _coerce_sha256,
    _stable_metadata,
    _read_bounded,
    prepare_private_docx_qualification_inputs,
    validate_docx_expectation,
)
from ._strict_io import reject_symlink_ancestors, strict_read_json


FIXED_DOCX_BENCHMARK_DIR = "private-docx"
FIXED_DOCX_INPUTS_DIR = "qualification-inputs"
FIXED_DOCX_EXPECTATION = "expectation.json"
FIXED_DOCX_QUALIFICATION = "qualification.json"
FIXED_DOCX_CORPUS_DIR = "corpus"
_FIXED_FILE_NAMES = tuple(f"{index:04d}-reviewed.docx" for index in range(1, DOCX_DOCUMENT_COUNT + 1))
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


class DocxBenchmarkContractError(RuntimeError):
    """Raised when an adapter or measurement seam violates the benchmark contract."""


def fixed_docx_benchmark_paths(runtime_root: Path) -> dict[str, Path]:
    root = runtime_root / FIXED_DOCX_BENCHMARK_DIR
    inputs = root / FIXED_DOCX_INPUTS_DIR
    return {
        "root": root,
        "inputs": inputs,
        "expectation": inputs / FIXED_DOCX_EXPECTATION,
        "qualification": root / FIXED_DOCX_QUALIFICATION,
        "corpus": inputs / FIXED_DOCX_CORPUS_DIR,
    }


def _raise_contract(message: str) -> None:
    raise DocxBenchmarkContractError(message)


def load_fixed_docx_fixture_data(corpus_root: Path) -> dict[str, bytes]:
    root_descriptor: int | None = None
    try:
        if not isinstance(corpus_root, Path):
            _raise_contract("DOCX benchmark fixture root is invalid")
        reject_symlink_ancestors(corpus_root, include_self=True)
        root_before = os.lstat(corpus_root)
        if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
            _raise_contract("DOCX benchmark fixture root is invalid")
        names = sorted(os.listdir(corpus_root))
        if names != list(_FIXED_FILE_NAMES):
            _raise_contract("DOCX benchmark fixture set is invalid")
        root_descriptor = os.open(corpus_root, _DIRECTORY_FLAGS)
        root_opened = os.fstat(root_descriptor)
        if (root_before.st_dev, root_before.st_ino) != (root_opened.st_dev, root_opened.st_ino):
            _raise_contract("DOCX benchmark fixture root changed")
        result: dict[str, bytes] = {}
        for index, name in enumerate(_FIXED_FILE_NAMES, 1):
            before = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1
                or before.st_size < 8
                or before.st_size > 50 * 1024 * 1024
            ):
                _raise_contract("DOCX benchmark fixture bytes are invalid")
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                if _stable_metadata(before) != _stable_metadata(opened):
                    _raise_contract("DOCX benchmark fixture changed")
                source = _read_bounded(descriptor, 50 * 1024 * 1024)
                if len(source) != before.st_size:
                    _raise_contract("DOCX benchmark fixture changed")
                after = os.fstat(descriptor)
                public = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                if (
                    _stable_metadata(opened) != _stable_metadata(after)
                    or _stable_metadata(opened) != _stable_metadata(public)
                ):
                    _raise_contract("DOCX benchmark fixture changed")
            finally:
                os.close(descriptor)
            result[f"docx-{index:04d}"] = b"PK\x03\x04" + source[4:]
        root_after = os.fstat(root_descriptor)
        root_public = os.lstat(corpus_root)
        if (
            (root_opened.st_dev, root_opened.st_ino) != (root_after.st_dev, root_after.st_ino)
            or (root_opened.st_dev, root_opened.st_ino) != (root_public.st_dev, root_public.st_ino)
        ):
            _raise_contract("DOCX benchmark fixture root changed")
        return result
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, DocxBenchmarkContractError):
            raise _parsing_error(str(exc)) from None
        raise _parsing_error("DOCX benchmark fixture bytes are invalid") from None
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _parsing_error(message: str) -> Exception:
    from .parsing import ParsingError

    return ParsingError(message)


def _expected_rejection_code(exc: Exception) -> str | None:
    from .parsing import ParsingError

    if (
        type(exc) is ParsingError
        and str(exc) == "DOCX package is invalid"
        and getattr(exc, "rejection_code", None)
        in DOCX_STABLE_FAILURE_CATEGORIES
    ):
        return getattr(exc, "rejection_code")
    return None


def _count_nested_rows(attributes: Mapping[str, Any]) -> tuple[int, int, int]:
    tables = 1
    rows = 0
    cells = 0
    table_rows = attributes.get("rows")
    if type(table_rows) is not list:
        raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    for row in table_rows:
        if type(row) is not dict or type(row.get("cells")) is not list:
            raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
        rows += 1
        for cell in row["cells"]:
            if type(cell) is not dict:
                raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
            cells += 1
            nested = cell.get("nested_tables", [])
            if type(nested) is not list:
                raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
            for table in nested:
                if type(table) is not dict:
                    raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
                nested_tables, nested_rows, nested_cells = _count_nested_rows(table)
                tables += nested_tables
                rows += nested_rows
                cells += nested_cells
    return tables, rows, cells


def _canonical_output(output: Any, source: Mapping[str, Any], parser_id: str, parser_version: str) -> dict[str, Any]:
    from .parsing import ParseOutput

    if not isinstance(output, ParseOutput):
        raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    ir = output.document_ir
    if type(ir) is not dict or set(ir) != {"schema_version", "document_id", "source", "parser", "blocks", "metadata"}:
        raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    if (
        ir["schema_version"] != "ao.lore.document-ir.v0.1"
        or ir["document_id"] != source["digest"]
        or ir["source"] != {
            "resource": source["resource"],
            "digest": source["digest"],
            "media_type": source["media_type"],
        }
    ):
        raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    parser = ir["parser"]
    if type(parser) is not dict or set(parser) != {"parser_id", "parser_version", "configuration_digest"}:
        raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    if parser["parser_id"] != parser_id or parser["parser_version"] != parser_version:
        raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    metadata = ir["metadata"]
    if type(metadata) is not dict or metadata.get("format") != "docx":
        raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    inventory_digest = metadata.get("inventory_digest")
    inventory_digest = _coerce_sha256(inventory_digest)
    blocks = ir["blocks"]
    if type(blocks) is not list:
        raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    counts = {
        "paragraphs": 0,
        "headings": 0,
        "lists": 0,
        "tables": 0,
        "rows": 0,
        "cells": 0,
        "links": 0,
        "footnotes": 0,
        "endnotes": 0,
        "drawings": 0,
        "media": 0,
    }
    texts: list[str] = []
    descriptors: list[str] = []
    spans: list[tuple[int, int]] = []
    for block in blocks:
        if type(block) is not dict:
            raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
        block_type = block.get("type")
        if type(block_type) is not str:
            raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
        text = block.get("text")
        if type(text) is not str:
            raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
        normalized = " ".join(text.split())
        if normalized:
            texts.append(normalized)
        span = block.get("source_span")
        if type(span) is not dict or type(span.get("start")) is not int or type(span.get("end")) is not int:
            raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
        spans.append((span["start"], span["end"]))
        attributes = block.get("attributes", {})
        if attributes is None:
            attributes = {}
        if type(attributes) is not dict:
            raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
        if block_type == "paragraph":
            counts["paragraphs"] += 1
            descriptors.append("paragraph")
        elif block_type == "heading":
            counts["headings"] += 1
            descriptors.append(f"heading:{block.get('level', 0)}")
        elif block_type == "list":
            counts["lists"] += 1
            descriptors.append(f"list:{attributes.get('list_level', -1)}")
        elif block_type == "table":
            table_count, row_count, cell_count = _count_nested_rows(attributes)
            counts["tables"] += table_count
            counts["rows"] += row_count
            counts["cells"] += cell_count
            descriptors.extend(["table"] * table_count)
        elif block_type == "link":
            counts["links"] += 1
            descriptors.append("link:external" if attributes.get("relationship") == "external-hyperlink" else "link")
        elif block_type == "footnote":
            kind = attributes.get("note_kind")
            if kind == "footnote":
                counts["footnotes"] += 1
                descriptors.append("footnote")
            elif kind == "endnote":
                counts["endnotes"] += 1
                descriptors.append("endnote")
            else:
                raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
        elif block_type == "image":
            counts["drawings"] += 1
            counts["media"] += 1
            descriptors.append("image")
        else:
            raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
    spans_ok = all(start >= 0 and end >= start for start, end in spans) and spans == sorted(spans)
    return {
        "inventory_digest": inventory_digest,
        "normalized_text_digest": canonical_digest(texts),
        "structural_event_digest": canonical_digest(descriptors),
        "counts": counts,
        "source_location_score": 1.0 if spans_ok else 0.0,
        "sealed_digest": canonical_digest(
            {
                "document_ir": output.document_ir,
                "quality_components": output.quality_components,
                "critical_failures": list(output.critical_failures),
            }
        ),
    }


def _detach_output(output: Any) -> Any:
    from .parsing import ParseOutput

    try:
        payload = json.loads(
            json.dumps(
                {
                    "document_ir": output.document_ir,
                    "quality_components": output.quality_components,
                    "critical_failures": list(output.critical_failures),
                },
                sort_keys=True,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
        return ParseOutput(
            payload["document_ir"],
            payload["quality_components"],
            tuple(payload["critical_failures"]),
        )
    except Exception as exc:
        raise DocxBenchmarkContractError("DOCX benchmark operation result could not be detached") from exc


def _measure(operation: Callable[[], Any], measurement: Callable[[Callable[[], Any]], tuple[Any, float, int]] | None) -> tuple[Any, float, int]:
    if measurement is None:
        tracemalloc.start()
        started = time.perf_counter()
        try:
            result = operation()
        finally:
            duration = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        return result, duration, peak
    return measurement(operation)


def derive_docx_tuning_decision(aggregate: Mapping[str, Any]) -> str:
    if not isinstance(aggregate, Mapping):
        raise _parsing_error("DOCX benchmark aggregate is invalid")
    repeatability = aggregate.get("repeatability")
    if type(repeatability) is not dict or type(repeatability.get("identical")) is not bool:
        raise _parsing_error("DOCX benchmark aggregate is invalid")
    unexpected = aggregate.get("unexpected_failures")
    if type(unexpected) is not list or any(type(item) is not str for item in unexpected):
        raise _parsing_error("DOCX benchmark aggregate is invalid")
    metrics = aggregate.get("metrics")
    if type(metrics) is not dict:
        raise _parsing_error("DOCX benchmark aggregate is invalid")
    real_failures = [item for item in unexpected if item not in DOCX_FLOORS]
    if real_failures or not repeatability["identical"]:
        return "investigate"
    weak = [
        name
        for name, minimum in DOCX_FLOORS.items()
        if isinstance(metrics.get(name), (int, float)) and float(metrics[name]) < minimum
    ]
    if weak:
        return "candidate_change"
    return "hold"


def run_docx_benchmark(
    adapter: Any,
    expectation: Mapping[str, Any],
    *,
    fixture_data: Mapping[str, bytes] | None = None,
    measurement: Callable[[Callable[[], Any]], tuple[Any, float, int]] | None = None,
) -> dict[str, Any]:
    try:
        validated = validate_docx_expectation(expectation)
        if validated.corpus_id != DOCX_PRIVATE_CORPUS_ID:
            raise DocxBenchmarkContractError("DOCX benchmark expectation is invalid")
        if fixture_data is None or not isinstance(fixture_data, Mapping):
            raise DocxBenchmarkContractError("DOCX benchmark fixture bytes are invalid")
        if set(fixture_data) != {document["item_id"] for document in validated.documents}:
            raise DocxBenchmarkContractError("DOCX benchmark fixture identities are invalid")
        if measurement is not None and not callable(measurement):
            raise DocxBenchmarkContractError("DOCX benchmark measurement seam is invalid")
        capability = getattr(adapter, "capability", None)
        if not isinstance(capability, Mapping):
            raise DocxBenchmarkContractError("DOCX benchmark adapter identity is invalid")
        parser_id = capability.get("parser_id")
        parser_version = capability.get("version")
        if type(parser_id) is not str or not parser_id or type(parser_version) is not str or not parser_version:
            raise DocxBenchmarkContractError("DOCX benchmark adapter identity is invalid")
        text_scores: list[float] = []
        structural_scores: list[float] = []
        location_scores: list[float] = []
        outcome_correct = 0
        stable_failures = {
            "invalid_package": 0,
            "unsupported_active_content": 0,
        }
        identical = True
        failure_labels: list[str] = []
        for document in validated.documents:
            data = fixture_data[document["item_id"]]
            if type(data) is not bytes:
                raise DocxBenchmarkContractError("DOCX benchmark fixture bytes are invalid")
            source = MappingProxyType(
                {
                    "resource": f"private-docx/{document['item_id']}.docx",
                    "digest": document["derived_digest"],
                    "media_type": DOCX_MIME,
                    "data": data,
                }
            )
            outputs: list[dict[str, Any]] = []
            outcome_failures: list[str] = []
            for _ in range(2):
                operation_calls = 0
                operation_output: Any = None
                operation_digest: str | None = None
                operation_error: BaseException | None = None

                def observed_parse() -> Any:
                    nonlocal operation_calls, operation_output, operation_digest, operation_error
                    operation_calls += 1
                    if operation_calls != 1:
                        raise DocxBenchmarkContractError("DOCX benchmark measurement invoked operation more than once")
                    try:
                        operation_output = adapter.parse(dict(source))
                        operation_digest = _canonical_output(operation_output, source, parser_id, parser_version)["sealed_digest"]
                    except BaseException as exc:
                        operation_error = exc
                        raise
                    return operation_output

                try:
                    measured_output, duration, peak = _measure(observed_parse, measurement)
                except BaseException as exc:
                    if tracemalloc.is_tracing():
                        tracemalloc.stop()
                    if operation_calls != 1 or exc is not operation_error:
                        raise DocxBenchmarkContractError("DOCX benchmark measurement changed operation semantics") from (exc if isinstance(exc, Exception) else None)
                    if not isinstance(exc, Exception):
                        raise
                    code = _expected_rejection_code(exc)
                    if code is None:
                        outcome_failures.append(f"unexpected:{type(exc).__name__}")
                    else:
                        outcome_failures.append(code)
                    continue
                if operation_calls != 1 or operation_error is not None or measured_output is not operation_output:
                    raise DocxBenchmarkContractError("DOCX benchmark measurement replaced operation result")
                if type(duration) not in (int, float) or not math.isfinite(duration) or float(duration) < 0:
                    raise DocxBenchmarkContractError("DOCX benchmark duration is invalid")
                if type(peak) is not int or peak < 0:
                    raise DocxBenchmarkContractError("DOCX benchmark memory result is invalid")
                if operation_digest is None:
                    raise DocxBenchmarkContractError("DOCX benchmark operation result shape is invalid")
                detached = _detach_output(measured_output)
                normalized = _canonical_output(detached, source, parser_id, parser_version)
                if not hmac.compare_digest(normalized["sealed_digest"], operation_digest):
                    raise DocxBenchmarkContractError("DOCX benchmark measurement mutated operation result")
                outputs.append(normalized)
            if document["expected_outcome"] == "reject":
                expected_code = (
                    DOCX_ACTIVE_CONTENT_REJECTION_CODE
                    if document["expected_rejection"] == "active-content"
                    else DOCX_INVALID_PACKAGE_REJECTION_CODE
                )
                if (
                    len(outcome_failures) == 2
                    and expected_code in EXPECTED_REJECTIONS
                    and outcome_failures == [expected_code, expected_code]
                    and outcome_failures[0] == outcome_failures[1]
                ):
                    outcome_correct += 1
                    stable_failures[expected_code] += 1
                else:
                    identical = False
                    failure_labels.append(f"{document['item_id']}:unexpected_rejection_outcome")
                continue
            if outcome_failures:
                identical = False
                failure_labels.append(f"{document['item_id']}:unexpected_rejection")
                text_scores.append(0.0)
                structural_scores.append(0.0)
                location_scores.append(0.0)
                continue
            first, second = outputs
            repeatable = (
                hmac.compare_digest(first["sealed_digest"], second["sealed_digest"])
                and first["counts"] == second["counts"]
            )
            identical = identical and repeatable
            if repeatable:
                outcome_correct += 1
            else:
                failure_labels.append(f"{document['item_id']}:nonrepeatable")
            if not hmac.compare_digest(first["inventory_digest"], document["package_inventory_digest"]):
                failure_labels.append(f"{document['item_id']}:inventory_drift")
            text_scores.append(
                1.0 if hmac.compare_digest(first["normalized_text_digest"], document["normalized_text_digest"]) else 0.0
            )
            matches = sum(first["counts"][name] == document["counts"][name] for name in document["counts"])
            structural_scores.append(
                (matches + int(hmac.compare_digest(first["structural_event_digest"], document["structural_event_digest"])))
                / (len(document["counts"]) + 1)
            )
            location_scores.append(first["source_location_score"])
        metrics = {
            "text_fidelity": sum(text_scores) / max(1, len(text_scores)),
            "structural_fidelity": sum(structural_scores) / max(1, len(structural_scores)),
            "source_location_fidelity": sum(location_scores) / max(1, len(location_scores)),
            "expected_outcome_accuracy": outcome_correct / DOCX_DOCUMENT_COUNT,
            "expected_rejection_count": DOCX_EXPECTED_REJECTION_COUNT,
            "accepted_document_count": DOCX_ACCEPTED_DOCUMENT_COUNT,
        }
        result = {
            "schema_version": DOCX_BENCHMARK_SCHEMA_VERSION,
            "fixture_corpus_digest": validated.corpus_digest,
            "parser_id": parser_id,
            "parser_version": parser_version,
            "parser_configuration_digest": docx_configuration_digest(DocxLimits()),
            "document_ir_version": "ao.lore.document-ir.v0.1",
            "attempts_per_document": 2,
            "document_count": DOCX_DOCUMENT_COUNT,
            "metrics": metrics,
            "exclusions": [],
            "stable_failures": stable_failures,
            "repeatability": {"runs": 2, "identical": identical, "score": 1.0 if identical else 0.0},
            "unexpected_failures": sorted(set(failure_labels)),
        }
        decision = derive_docx_tuning_decision(result)
        if decision == "candidate_change":
            result["unexpected_failures"] = sorted(
                name for name, minimum in DOCX_FLOORS.items() if metrics[name] < minimum
            )
        result["decision"] = decision
        result["result_digest"] = canonical_digest(result)
        return json.loads(json.dumps(result, sort_keys=True))
    except DocxBenchmarkContractError as exc:
        raise _parsing_error(str(exc)) from None


def run_private_docx_qualification(
    adapter_factory: Callable[[Mapping[str, Any]], Any],
    review: tuple[ReviewedDocxSource, ...],
    expectation: Mapping[str, Any],
    *,
    source_root: Path,
    runtime_root: Path,
    measurement: Callable[[Callable[[], Any]], tuple[Any, float, int]] | None = None,
) -> dict[str, Any]:
    """Prepare and consume one exact representative DOCX qualification set."""

    try:
        if not callable(adapter_factory) or not isinstance(runtime_root, Path):
            raise DocxBenchmarkContractError(
                "DOCX qualification execution contract is invalid"
            )
        prepared = prepare_private_docx_qualification_inputs(
            review,
            expectation,
            source_root=source_root,
            runtime_root=runtime_root,
        )
        detached = json.loads(
            json.dumps(
                prepared,
                allow_nan=False,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        paths = fixed_docx_benchmark_paths(runtime_root)
        persisted_expectation, _ = strict_read_json(
            paths["expectation"],
            "private DOCX expectation",
            max_bytes=8 * 1024 * 1024,
            root=runtime_root,
        )
        validated_expectation = validate_docx_expectation(persisted_expectation)
        if validated_expectation.corpus_digest != detached.get(
            "expectation_digest"
        ):
            raise DocxBenchmarkContractError(
                "DOCX qualification execution binding is invalid"
            )
        fixture_data = load_fixed_docx_fixture_data(paths["corpus"])
        documents = detached.get("documents")
        if type(documents) is not list or len(documents) != DOCX_DOCUMENT_COUNT:
            raise DocxBenchmarkContractError(
                "DOCX qualification execution binding is invalid"
            )
        for document in documents:
            body = fixture_data.get(document.get("item_id"))
            if (
                type(body) is not bytes
                or len(body) != document.get("derived_bytes")
                or "sha256:" + hashlib.sha256(body).hexdigest()
                != document.get("derived_digest")
            ):
                raise DocxBenchmarkContractError(
                    "DOCX qualification execution binding is invalid"
                )
        adapter = adapter_factory(MappingProxyType(copy.deepcopy(detached)))
        report = run_docx_benchmark(
            adapter,
            persisted_expectation,
            fixture_data=fixture_data,
            measurement=measurement,
        )
        if report["fixture_corpus_digest"] != detached["expectation_digest"]:
            raise DocxBenchmarkContractError(
                "DOCX qualification execution binding is invalid"
            )
        return report
    except DocxBenchmarkContractError as exc:
        raise _parsing_error(str(exc)) from None
