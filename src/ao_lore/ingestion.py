"""Fail-closed orchestration for one local document into the review queue."""

from __future__ import annotations

import copy
import hashlib
import math
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ._strict_io import ContractError, strict_read_json
from .benchmark import BenchmarkError, canonical_digest
from .candidates import (
    CandidateError,
    build_candidate_provenance,
    load_verified_candidate_if_present,
    persist_candidate,
    load_verified_candidate,
)
from .distillation import (
    DeterministicDistiller,
    DistillerAdapter,
    distill_document_ir,
)
from .home import repository_root
from .parsing import (
    ParserRegistry,
    ParsingError,
    ProductionParser,
    validate_docling_benchmark,
)


MAXIMUM_PDF_BYTES = 50 * 1024 * 1024
MAXIMUM_PDF_PAGES = 500
MAXIMUM_DOCUMENT_BLOCKS = 100_000
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")


class IngestionError(ValueError):
    """Raised when single-document ingestion fails closed."""


class SourceDigestMismatchError(IngestionError):
    """Raised when safely read source bytes differ from an authorized digest."""


def build_empty_candidate_comparison_context() -> dict[str, Any]:
    """Return a detached versioned context for an empty candidate corpus."""

    return {
        "schema_version": "ao.lore.candidate-comparison-context.v0.1",
        "candidates": [],
    }


@dataclass(frozen=True)
class IngestionDependencies:
    parser: ProductionParser
    distiller: DistillerAdapter
    now: Callable[[], str]
    ocr_activation: object | None = None
    benchmark_manifest: Mapping[str, Any] | None = None
    selection_profile: Mapping[str, Any] | None = None
    quality_profile: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class VerifiedDocumentPolicy:
    format_id: str
    media_type: str
    extension: str
    parser_id: str
    parser_version: str
    required_structural_elements: tuple[str, ...]
    validate_benchmark: Callable[[Mapping[str, Any]], None]
    build_dependencies: Callable[[Mapping[str, Any]], "IngestionDependencies"]


@dataclass(frozen=True)
class DocxProvenanceOrigin:
    corpus_id: str
    original_digest: str
    derived_digest: str
    transformation_id: str
    expectation_digest: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_benchmark(manifest: Mapping[str, Any]) -> None:
    if not isinstance(manifest, Mapping):
        raise IngestionError("benchmark evidence is invalid")
    try:
        from .docling_pdf import configuration_digest

        validate_docling_benchmark(
            manifest,
            expected_configuration_digest=configuration_digest(
                MAXIMUM_PDF_BYTES,
                MAXIMUM_PDF_PAGES,
                MAXIMUM_DOCUMENT_BLOCKS,
            ),
        )
    except (BenchmarkError, ParsingError, TypeError, ValueError) as exc:
        raise IngestionError("benchmark evidence is invalid") from exc


def _validate_docx_benchmark(manifest: Mapping[str, Any]) -> None:
    raise IngestionError("benchmark evidence is invalid")


def _validate_docx_benchmark_with_expected_corpus(
    manifest: Mapping[str, Any], *, expected_corpus_digest: str
) -> None:
    if not isinstance(manifest, Mapping) or type(expected_corpus_digest) is not str:
        raise IngestionError("benchmark evidence is invalid")
    try:
        from .docx_ooxml import (
            DOCX_PARSER_VERSION,
            DocxLimits,
            docx_configuration_digest,
            validate_docx_benchmark,
        )

        validated = validate_docx_benchmark(
            manifest,
            expected_configuration_digest=docx_configuration_digest(DocxLimits()),
            expected_corpus_digest=expected_corpus_digest,
        )
        if (
            validated["decision"] != "hold"
            or validated["parser_version"] != DOCX_PARSER_VERSION
        ):
            raise ParsingError("DOCX benchmark evidence is invalid")
    except (BenchmarkError, ParsingError, TypeError, ValueError, KeyError) as exc:
        raise IngestionError("benchmark evidence is invalid") from exc


def _workspace_profiles(
    format_id: str,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return fixed local parse profiles for the two qualified adapters."""

    if format_id == "pdf":
        selection = {
            "profile_id": "workspace-pdf-selection-v1",
            "weights": {
                "structural_fidelity": 0.4,
                "text_fidelity": 0.4,
                "source_location_fidelity": 0.2,
            },
            "penalty_weights": {},
            "normalization_bounds": {},
            "missing_optional_policy": "ineligible",
            "tie_break_order": [
                "required_feature_coverage",
                "structural_fidelity",
                "parser_id",
            ],
            "fixture_corpus_digest": manifest.get("fixture_corpus_digest"),
        }
        quality = {
            "profile_id": "workspace-pdf-quality-v1",
            "component_weights": {
                "text_coverage": 0.4,
                "structural_completeness": 0.3,
                "source_span_coverage": 0.2,
                "document_ir_valid": 0.1,
            },
            "accept_threshold": 0.8,
            "fallback_threshold": 0.7,
            "maximum_fallbacks": 0,
            "critical_failures": [
                "invalid_ir",
                "source_digest_mismatch",
                "parser_crash",
                "no_readable_content",
                "lost_provenance",
            ],
            "calibration_result_digest": manifest.get("result_digest"),
            "intermediate_decision": "quarantine",
            "exhausted_decision": "reject",
        }
        return selection, quality
    if format_id == "docx":
        selection = {
            "profile_id": "workspace-docx-selection-v1",
            "weights": {"structural_fidelity": 0.5, "text_fidelity": 0.5},
            "penalty_weights": {},
            "normalization_bounds": {},
            "missing_optional_policy": "ineligible",
            "tie_break_order": [
                "required_feature_coverage",
                "structural_fidelity",
                "parser_id",
            ],
            "fixture_corpus_digest": manifest.get("fixture_corpus_digest"),
        }
        quality = {
            "profile_id": "workspace-docx-quality-v1",
            "component_weights": {
                "text_coverage": 0.5,
                "structural_completeness": 0.3,
                "document_ir_valid": 0.2,
            },
            "accept_threshold": 0.8,
            "fallback_threshold": 0.0,
            "maximum_fallbacks": 0,
            "critical_failures": [
                "invalid_ir",
                "source_digest_mismatch",
                "parser_crash",
                "no_readable_content",
                "lost_provenance",
            ],
            "calibration_result_digest": manifest.get("result_digest"),
            "intermediate_decision": "reject",
            "exhausted_decision": "reject",
        }
        return selection, quality
    raise IngestionError("document format is unsupported")


def default_ingestion_dependencies(
    benchmark_manifest: Mapping[str, Any],
) -> IngestionDependencies:
    """Construct the qualified offline runtime only when ingestion is invoked."""

    _validate_benchmark(benchmark_manifest)
    try:
        from .docling_pdf import DoclingPdfAdapter

        registry = ParserRegistry()
        registry.register(DoclingPdfAdapter(benchmark_manifest))
    except Exception as exc:
        raise IngestionError("qualified PDF parser is unavailable") from exc
    selection_profile, quality_profile = _workspace_profiles("pdf", benchmark_manifest)
    return IngestionDependencies(
        parser=ProductionParser(registry),
        distiller=DeterministicDistiller(),
        now=_utc_now,
        benchmark_manifest=copy.deepcopy(dict(benchmark_manifest)),
        selection_profile=selection_profile,
        quality_profile=quality_profile,
    )


def default_docx_ingestion_dependencies(
    benchmark_manifest: Mapping[str, Any],
    *,
    expected_corpus_digest: str,
) -> IngestionDependencies:
    """Construct the qualified native DOCX runtime only when ingestion runs."""

    _validate_docx_benchmark_with_expected_corpus(
        benchmark_manifest, expected_corpus_digest=expected_corpus_digest
    )
    try:
        from .docx_ooxml import NativeDocxOoxmlAdapter

        registry = ParserRegistry()
        registry.register(
            NativeDocxOoxmlAdapter(
                benchmark_manifest,
                expected_corpus_digest=expected_corpus_digest,
            )
        )
    except Exception as exc:
        raise IngestionError("qualified DOCX parser is unavailable") from exc
    selection_profile, quality_profile = _workspace_profiles("docx", benchmark_manifest)
    return IngestionDependencies(
        parser=ProductionParser(registry),
        distiller=DeterministicDistiller(),
        now=_utc_now,
        benchmark_manifest=copy.deepcopy(dict(benchmark_manifest)),
        selection_profile=selection_profile,
        quality_profile=quality_profile,
    )


def default_ocr_ingestion_dependencies(
    activation: object,
    execute: Callable[[Mapping[str, Any]], object],
) -> IngestionDependencies:
    """Construct one activated OCR runtime around the sandbox execution seam."""

    try:
        from .parsing import QualifiedOcrAdapter

        adapter = QualifiedOcrAdapter(activation, execute)
        registry = ParserRegistry()
        registry.register(adapter)
    except Exception as exc:
        raise IngestionError("qualified OCR parser is unavailable") from exc
    return IngestionDependencies(
        parser=ProductionParser(registry),
        distiller=DeterministicDistiller(),
        now=_utc_now,
        ocr_activation=activation,
    )


PDF_POLICY = VerifiedDocumentPolicy(
    format_id="pdf",
    media_type="application/pdf",
    extension=".pdf",
    parser_id="docling",
    parser_version="2.118.1",
    required_structural_elements=(),
    validate_benchmark=_validate_benchmark,
    build_dependencies=default_ingestion_dependencies,
)


DOCX_POLICY = VerifiedDocumentPolicy(
    format_id="docx",
    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    extension=".docx",
    parser_id="native-docx-ooxml",
    parser_version="1.0.0",
    required_structural_elements=("headings", "links", "tables", "footnotes", "images"),
    validate_benchmark=_validate_docx_benchmark,
    build_dependencies=default_docx_ingestion_dependencies,
)


def _read_anchored_source(
    source_path: str | Path, *, policy: VerifiedDocumentPolicy
) -> bytes:
    """Read through held directory descriptors without following symlinks."""

    path = Path(source_path)
    if path.suffix.lower() != policy.extension:
        if policy.format_id == "pdf":
            raise IngestionError("source must be a PDF document")
        raise IngestionError("source must be a DOCX document")
    source_root = repository_root() / "sources"
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        relative = path.relative_to(source_root)
    except ValueError as exc:
        raise IngestionError("source input is invalid") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise IngestionError("source input is invalid")

    directory_flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
    )
    descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        current = os.open(source_root, directory_flags)
        descriptors.append(current)
        for component in relative.parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            descriptors.append(current)
        file_descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=current,
        )
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise IngestionError("source input is invalid")
        if metadata.st_size > MAXIMUM_PDF_BYTES:
            raise IngestionError("source input is invalid")
        chunks: list[bytes] = []
        remaining = MAXIMUM_PDF_BYTES + 1
        while remaining > 0:
            chunk = os.read(file_descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > MAXIMUM_PDF_BYTES:
            raise IngestionError("source input is invalid")
    except IngestionError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise IngestionError("source input is invalid") from exc
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    if policy.format_id == "pdf" and not data.startswith(b"%PDF-"):
        raise IngestionError("source media does not match PDF")
    return data


def read_verified_pdf_source(
    source_path: str | Path, expected_source_digest: str
) -> bytes:
    """Read a contained PDF and bind the exact bytes to an expected digest."""

    if not isinstance(expected_source_digest, str) or _SHA256_RE.fullmatch(
        expected_source_digest
    ) is None:
        raise IngestionError("expected source digest is invalid")
    data = _read_anchored_source(source_path, policy=PDF_POLICY)
    observed = "sha256:" + hashlib.sha256(data).hexdigest()
    if observed != expected_source_digest:
        raise SourceDigestMismatchError("source digest does not match manifest")
    return data


def read_verified_docx_source(
    source_path: str | Path, expected_source_digest: str
) -> bytes:
    """Read a contained DOCX and bind the exact bytes to an expected digest."""

    if not isinstance(expected_source_digest, str) or _SHA256_RE.fullmatch(
        expected_source_digest
    ) is None:
        raise IngestionError("expected source digest is invalid")
    data = _read_anchored_source(source_path, policy=DOCX_POLICY)
    try:
        from .docx_ooxml import validate_docx_package

        validate_docx_package(data)
    except Exception as exc:
        raise IngestionError("source media does not match DOCX") from exc
    observed = "sha256:" + hashlib.sha256(data).hexdigest()
    if observed != expected_source_digest:
        raise SourceDigestMismatchError("source digest does not match manifest")
    return data


def resolve_docx_origin(
    source_path: str | Path,
    *,
    expectation: Any,
) -> tuple[bytes, DocxProvenanceOrigin, str]:
    """Read one native DOCX once and derive its trusted reviewed origin."""

    from .private_docx_domain import DocxExpectationCorpus, DOCX_TRANSFORMATION_ID

    if type(expectation) is not DocxExpectationCorpus:
        raise IngestionError("verified document binding is invalid")
    data = _read_anchored_source(source_path, policy=DOCX_POLICY)
    try:
        from .docx_ooxml import validate_docx_package

        validate_docx_package(data)
    except Exception as exc:
        raise IngestionError("source media does not match DOCX") from exc
    derived_digest = "sha256:" + hashlib.sha256(data).hexdigest()
    matches = [
        document
        for document in expectation.documents
        if document["derived_digest"] == derived_digest
    ]
    if len(matches) != 1:
        raise IngestionError("verified document binding is invalid")
    match = matches[0]
    origin = DocxProvenanceOrigin(
        corpus_id=expectation.corpus_id,
        original_digest=match["source_digest"],
        derived_digest=match["derived_digest"],
        transformation_id=DOCX_TRANSFORMATION_ID,
        expectation_digest=expectation.corpus_digest,
    )
    return data, origin, expectation.corpus_digest


def _strict_keys(value: Any, expected: set[str]) -> bool:
    return isinstance(value, Mapping) and set(value) == expected


def _valid_number(value: Any, *, low: float = 0.0, high: float = 1.0) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and low <= value <= high
    )


def _validate_parser_result(
    result: Any,
    *,
    policy: VerifiedDocumentPolicy,
    data: bytes,
    resource: str,
    benchmark_manifest: Mapping[str, Any],
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Bind every accepted parser surface to the qualified source and policy."""

    if not isinstance(selection_profile, Mapping) or not isinstance(
        quality_profile, Mapping
    ):
        raise IngestionError("parser result binding is invalid")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    parse_keys = {
        "schema_version", "document_digest", "selection_report", "attempts",
        "selected_parser", "quality_report", "decision", "document_ir",
    }
    selection_keys = {
        "schema_version", "document_digest", "weight_profile",
        "eligible_parsers", "rejected_parsers", "candidates",
        "selected_parser", "tie_break", "benchmark_evidence",
    }
    quality_keys = {
        "schema_version", "document_digest", "parser_id", "parser_version",
        "threshold_profile", "components", "overall_quality_score",
        "critical_failures", "decision",
    }
    if not _strict_keys(result, parse_keys):
        raise IngestionError("parser result binding is invalid")
    selection = result["selection_report"]
    quality = result["quality_report"]
    document_ir = result["document_ir"]
    if not _strict_keys(selection, selection_keys) or not _strict_keys(
        quality, quality_keys
    ):
        raise IngestionError("parser result binding is invalid")
    if (
        result["schema_version"] != "ao.lore.production-parse-result.v0.1"
        or result["document_digest"] != digest
        or result["selected_parser"] != policy.parser_id
        or result["decision"] != "accept"
        or selection["schema_version"] != "ao.lore.parser-selection-report.v0.1"
        or selection["document_digest"] != digest
        or selection["selected_parser"] != policy.parser_id
        or selection["weight_profile"] != selection_profile.get("profile_id")
        or selection["eligible_parsers"] != [policy.parser_id]
        or selection["rejected_parsers"] != []
        or selection["benchmark_evidence"]
        != [benchmark_manifest["result_digest"]]
        or quality["schema_version"] != "ao.lore.parse-quality-report.v0.1"
        or quality["document_digest"] != digest
        or quality["parser_id"] != policy.parser_id
        or quality["parser_version"] != policy.parser_version
        or quality["threshold_profile"] != quality_profile.get("profile_id")
        or quality["critical_failures"] != []
        or quality["decision"] != "accept"
        or not _valid_number(quality["overall_quality_score"])
    ):
        raise IngestionError("parser result binding is invalid")

    candidates = selection["candidates"]
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise IngestionError("parser result binding is invalid")
    candidate = candidates[0]
    if (
        not _strict_keys(
            candidate,
            {"parser_id", "normalized_components", "selection_score"},
        )
        or candidate["parser_id"] != policy.parser_id
        or not isinstance(candidate["normalized_components"], Mapping)
        or not all(
            isinstance(name, str) and _valid_number(score)
            for name, score in candidate["normalized_components"].items()
        )
        or not _valid_number(candidate["selection_score"], low=-1.0)
    ):
        raise IngestionError("parser result binding is invalid")
    tie_break = selection["tie_break"]
    if (
        not _strict_keys(tie_break, {"applied", "reason"})
        or not isinstance(tie_break["applied"], bool)
        or not isinstance(tie_break["reason"], str)
        or not tie_break["reason"]
    ):
        raise IngestionError("parser result binding is invalid")
    components = quality["components"]
    if not isinstance(components, list) or not components or any(
        not _strict_keys(item, {"name", "applicable", "score", "reason"})
        or not isinstance(item["name"], str)
        or not item["name"]
        or not isinstance(item["applicable"], bool)
        or (
            item["score"] is not None
            and not _valid_number(item["score"])
        )
        or (item["applicable"] is True and item["score"] is None)
        or (item["applicable"] is False and item["score"] is not None)
        or not isinstance(item["reason"], str)
        for item in components
    ) or len({item["name"] for item in components}) != len(components):
        raise IngestionError("parser result binding is invalid")
    attempts = result["attempts"]
    if (
        not isinstance(attempts, list)
        or len(attempts) != 1
        or not _strict_keys(
            attempts[0], {"parser_id", "selection_score", "quality_report"}
        )
        or attempts[0]["parser_id"] != policy.parser_id
        or attempts[0]["selection_score"] != candidate["selection_score"]
        or attempts[0]["quality_report"] != quality
    ):
        raise IngestionError("parser result binding is invalid")

    if not _strict_keys(
        document_ir,
        {"schema_version", "document_id", "source", "parser", "blocks", "metadata"},
    ):
        raise IngestionError("parser result binding is invalid")
    ir_source = document_ir["source"]
    ir_parser = document_ir["parser"]
    if (
        document_ir["schema_version"] != "ao.lore.document-ir.v0.1"
        or document_ir["document_id"] != digest
        or not _strict_keys(ir_source, {"resource", "digest", "media_type"})
        or ir_source
        != {"resource": resource, "digest": digest, "media_type": policy.media_type}
        or not _strict_keys(
            ir_parser, {"parser_id", "parser_version", "configuration_digest"}
        )
        or ir_parser["parser_id"] != policy.parser_id
        or ir_parser["parser_version"] != policy.parser_version
        or ir_parser["configuration_digest"]
        != benchmark_manifest["parser_configuration_digest"]
        or not isinstance(document_ir["blocks"], list)
        or not isinstance(document_ir["metadata"], Mapping)
    ):
        raise IngestionError("parser result binding is invalid")
    try:
        canonical_digest(selection)
        canonical_digest(quality)
        canonical_digest(document_ir)
    except BenchmarkError as exc:
        raise IngestionError("parser result binding is invalid") from exc
    return result


def _parse_document(
    policy: VerifiedDocumentPolicy,
    dependencies: IngestionDependencies,
    data: bytes,
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    benchmark_manifest: Mapping[str, Any],
    *,
    resource: str | None = None,
) -> Mapping[str, Any]:
    source_digest = hashlib.sha256(data).hexdigest()
    resource = resource or ("source-" + source_digest[:16] + policy.extension)
    request = {
        "media_type": policy.media_type,
        "extension": policy.extension,
        "required_structural_elements": list(policy.required_structural_elements),
        "canonical_ir_version": "ao.lore.document-ir.v0.1",
        "network_allowed": False,
        "license_allowlist": ["Apache-2.0"],
        "sandbox_required": True,
    }
    try:
        result = dependencies.parser.parse_bytes(
            data,
            resource,
            request,
            selection_profile,
            quality_profile,
        )
    except Exception as exc:
        raise IngestionError("document parsing failed") from exc
    return _validate_parser_result(
        result,
        policy=policy,
        data=data,
        resource=resource,
        benchmark_manifest=benchmark_manifest,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
    )


def _read_absolute_source(source: Path, policy: VerifiedDocumentPolicy) -> bytes:
    path = Path(os.path.abspath(source))
    if path.suffix.lower() != policy.extension or not path.name:
        raise IngestionError("source input is invalid")
    descriptors: list[tuple[int, int, str, tuple[int, int]]] = []
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    source_fd: int | None = None
    try:
        parent = current
        for component in path.parts[1:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent,
            )
            info = os.fstat(child)
            descriptors.append((parent, child, component, (info.st_dev, info.st_ino)))
            parent = child
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAXIMUM_PDF_BYTES
        ):
            raise IngestionError("source input is invalid")
        source_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(source_fd)
        identity = lambda info: (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        if identity(before) != identity(opened):
            raise IngestionError("source input changed")
        chunks: list[bytes] = []
        remaining = MAXIMUM_PDF_BYTES + 1
        while remaining:
            chunk = os.read(source_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(source_fd)
        rebound = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if (
            len(data) > MAXIMUM_PDF_BYTES
            or len(data) != opened.st_size
            or identity(opened) != identity(after)
            or identity(after) != identity(rebound)
        ):
            raise IngestionError("source input changed")
        for parent_fd, child_fd, name, expected in reversed(descriptors):
            held = os.fstat(child_fd)
            current_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(current_info.st_mode)
                or (held.st_dev, held.st_ino) != expected
                or (current_info.st_dev, current_info.st_ino) != expected
            ):
                raise IngestionError("source input changed")
        return data
    except IngestionError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise IngestionError("source input is invalid") from exc
    finally:
        if source_fd is not None:
            os.close(source_fd)
        for _parent, child, _name, _identity_value in reversed(descriptors):
            os.close(child)
        os.close(current)


def _parse_verified_bytes(
    data: bytes,
    *,
    format_id: str,
    dependencies: IngestionDependencies,
    resource: str | None = None,
) -> dict[str, object]:
    if type(dependencies) is not IngestionDependencies or type(data) is not bytes:
        raise IngestionError("verified parse dependencies are invalid")
    if format_id == "pdf":
        policy = PDF_POLICY
        if not data.startswith(b"%PDF-"):
            raise IngestionError("source media does not match PDF")
    elif format_id == "docx":
        policy = DOCX_POLICY
        try:
            from .docx_ooxml import validate_docx_package

            validate_docx_package(data)
        except Exception as exc:
            raise IngestionError("source media does not match DOCX") from exc
    else:
        raise IngestionError("document format is unsupported")
    manifest = dependencies.benchmark_manifest
    selection_profile = dependencies.selection_profile
    quality_profile = dependencies.quality_profile
    if not all(isinstance(value, Mapping) for value in (
        manifest, selection_profile, quality_profile,
    )):
        raise IngestionError("verified parse dependencies are invalid")
    assert manifest is not None
    if policy.format_id == "pdf":
        _validate_benchmark(manifest)
    else:
        corpus_digest = manifest.get("fixture_corpus_digest")
        if type(corpus_digest) is not str:
            raise IngestionError("benchmark evidence is invalid")
        _validate_docx_benchmark_with_expected_corpus(
            manifest, expected_corpus_digest=corpus_digest,
        )
    result = _parse_document(
        policy,
        dependencies,
        data,
        selection_profile,
        quality_profile,
        manifest,
        resource=resource,
    )
    return copy.deepcopy(dict(result))


def parse_verified_document(
    source: Path,
    *,
    format_id: str,
    dependencies: IngestionDependencies,
) -> dict[str, object]:
    """Return an accepted parser result and IR; do not create a candidate."""

    if format_id == "pdf":
        policy = PDF_POLICY
    elif format_id == "docx":
        policy = DOCX_POLICY
    else:
        raise IngestionError("document format is unsupported")
    data = _read_absolute_source(source, policy)
    return _parse_verified_bytes(
        data, format_id=format_id, dependencies=dependencies,
    )


def _workspace_parser_dependencies(
    runtime_root: Path,
    format_id: str,
) -> IngestionDependencies:
    if format_id == "pdf":
        manifest, _digest = strict_read_json(
            runtime_root / "benchmarks" / "docling-2.118.1.json",
            "Docling qualification manifest",
            max_bytes=1024 * 1024,
            root=runtime_root,
        )
        if not isinstance(manifest, Mapping):
            raise IngestionError("benchmark evidence is invalid")
        return default_ingestion_dependencies(manifest)
    if format_id == "docx":
        from .docx_benchmark import fixed_docx_benchmark_paths

        manifest, _digest = strict_read_json(
            fixed_docx_benchmark_paths(runtime_root)["qualification"],
            "DOCX qualification manifest",
            max_bytes=1024 * 1024,
            root=runtime_root,
        )
        if not isinstance(manifest, Mapping):
            raise IngestionError("benchmark evidence is invalid")
        corpus_digest = manifest.get("fixture_corpus_digest")
        if type(corpus_digest) is not str:
            raise IngestionError("benchmark evidence is invalid")
        return default_docx_ingestion_dependencies(
            manifest, expected_corpus_digest=corpus_digest,
        )
    raise IngestionError("document format is unsupported")


def ingest_workspace_document(
    dependencies: object,
    workspace_id: str,
    source_locator: str,
    format_id: str,
    authority_role: str,
    sensitivity: str,
) -> dict[str, object]:
    """Parse one contained inbox source and append one document generation."""

    from .document_evidence_contracts import (
        validate_workspace_document_ingest_readback,
    )
    from .evidence_graph_contracts import AUTHORITY_ROLES
    from .workspace_contracts import AUTHORITY_FIELDS
    from .workspace_documents import (
        WorkspaceDocumentDependencies,
        WorkspaceDocumentError,
        publish_workspace_documents,
    )
    from .workspace_registry import WorkspaceRegistryDependencies
    from .workspace_runtime import (
        read_workspace_inbox_source,
        revalidate_workspace_inbox_source,
    )

    if type(dependencies) is not WorkspaceRegistryDependencies:
        raise IngestionError("workspace ingestion dependencies are invalid")
    if format_id not in {"pdf", "docx"}:
        raise IngestionError("document format is unsupported")
    if authority_role not in AUTHORITY_ROLES:
        raise IngestionError("document authority role is invalid")
    if sensitivity not in {"public", "internal", "restricted"}:
        raise IngestionError("document sensitivity is invalid")
    suffix = ".pdf" if format_id == "pdf" else ".docx"
    source = read_workspace_inbox_source(
        dependencies,
        workspace_id,
        source_locator,
        expected_suffix=suffix,
    )
    parser_dependencies = _workspace_parser_dependencies(
        Path(dependencies.runtime_root), format_id,
    )
    parsed = _parse_verified_bytes(
        source.data,
        format_id=format_id,
        dependencies=parser_dependencies,
        resource=source.locator,
    )
    document_ir = copy.deepcopy(parsed["document_ir"])
    if type(document_ir) is not dict or type(document_ir.get("metadata")) is not dict:
        raise IngestionError("verified document IR is invalid")
    source_digest = "sha256:" + hashlib.sha256(source.data).hexdigest()
    source_hex = source_digest.removeprefix("sha256:")
    source_record_digest = canonical_digest({
        "workspace_id": workspace_id,
        "source_locator": source.locator,
        "source_digest": source_digest,
        "media_type": document_ir["source"]["media_type"],
        "authority_role": authority_role,
        "sensitivity": sensitivity,
    })
    document_ir["metadata"] = {
        **document_ir["metadata"],
        "source_id": "source-" + source_hex[:24],
        "source_record_digest": source_record_digest,
        "authority_role": authority_role,
        "sensitivity": sensitivity,
        "version": "source-" + source_hex[:24],
        "effective_date": None,
        "freshness_status": "current",
        "qualification_codes": ["qualified-parser"],
    }
    dependencies.failpoint("before_workspace_ingest_publication")
    revalidate_workspace_inbox_source(dependencies, source)
    document_dependencies = WorkspaceDocumentDependencies(
        Path(dependencies.runtime_root),
    )
    try:
        publication = publish_workspace_documents(
            document_dependencies,
            workspace_id,
            (document_ir,),
            expected_registry_digest=source.registry_digest,
            expected_definition_digest=source.definition_digest,
        )
    except WorkspaceDocumentError as exc:
        if str(exc) == "workspace document registry binding differs":
            raise IngestionError("workspace registry changed") from exc
        raise
    status = publication["status"]
    generation = {
        key: copy.deepcopy(value)
        for key, value in publication.items()
        if key != "status"
    }
    document_id = document_ir["document_id"]
    documents = {
        item["document_id"]: item for item in generation["documents"]
    }
    published = documents.get(document_id)
    if type(published) is not dict or published["document_ir"] != document_ir:
        raise IngestionError("workspace document publication differs")
    readback = {
        "schema_version": "ao.lore.workspace-document-ingest-readback.v0.1",
        "ingest_id": "ingest-" + canonical_digest({
            "workspace_id": workspace_id,
            "source_digest": source_digest,
            "document_ir_digest": published["document_ir_digest"],
            "generation_digest": generation["generation_digest"],
        }).split(":", 1)[1][:24],
        "workspace_id": workspace_id,
        "status": status,
        "document_id": document_id,
        "source_digest": source_digest,
        "document_ir_digest": published["document_ir_digest"],
        "generation_id": generation["generation_id"],
        "generation_digest": generation["generation_digest"],
        "readback_digest": "sha256:" + "0" * 64,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    readback["readback_digest"] = canonical_digest({
        key: value for key, value in readback.items() if key != "readback_digest"
    })
    try:
        return validate_workspace_document_ingest_readback(
            readback, generation=generation, workspace_id=workspace_id,
        )
    except (ContractError, BenchmarkError, TypeError, ValueError) as exc:
        raise IngestionError("workspace ingest readback is invalid") from exc


def _distill(
    dependencies: IngestionDependencies,
    document_ir: Mapping[str, Any],
    candidate_context: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    try:
        if candidate_context is None:
            effective_context = build_empty_candidate_comparison_context()
        elif isinstance(candidate_context, Mapping):
            effective_context = copy.deepcopy(dict(candidate_context))
        else:
            raise BenchmarkError("value is not strict canonical JSON")
        canonical_digest(effective_context)
        return distill_document_ir(
            document_ir,
            dependencies.distiller,
            effective_context,
        )
    except Exception as exc:
        raise IngestionError("document distillation failed") from exc


def _reconcile_existing(
    distillation_result: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    candidate_root: Path | None,
) -> Mapping[str, Any] | None:
    candidate = distillation_result.get("candidate")
    candidate_digest = distillation_result.get("candidate_digest")
    if not isinstance(candidate, Mapping) or not isinstance(
        candidate_digest, str
    ):
        raise CandidateError("distillation candidate binding is malformed")
    candidate_id = candidate.get("candidate_id")
    if not isinstance(candidate_id, str):
        raise CandidateError("distillation candidate identity is malformed")
    retained = load_verified_candidate_if_present(
        candidate_id, candidate_root=candidate_root
    )
    if retained is None:
        return None
    retained_provenance = retained["provenance"]
    retained_inspection = retained["inspection"]
    comparable = {**proposed, "created_at": retained_provenance["created_at"]}
    if (
        retained["candidate"] != candidate
        or retained_inspection["candidate_digest"] != candidate_digest
        or comparable != retained_provenance
    ):
        raise CandidateError(
            "existing candidate conflicts with ingestion bindings"
        )
    return retained_provenance


def _persist_with_collision_reconciliation(
    distillation_result: Mapping[str, Any],
    proposed: Mapping[str, Any],
    *,
    candidate_root: Path | None,
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    provenance = _reconcile_existing(
        distillation_result, proposed, candidate_root=candidate_root
    ) or proposed
    try:
        persistence = persist_candidate(
            distillation_result,
            provenance,
            candidate_root=candidate_root,
        )
    except CandidateError:
        retained = _reconcile_existing(
            distillation_result, proposed, candidate_root=candidate_root
        )
        if retained is None:
            raise
        provenance = retained
        persistence = persist_candidate(
            distillation_result,
            provenance,
            candidate_root=candidate_root,
        )
    return persistence, provenance


def ingest_single_document(
    source_path: str | Path,
    *,
    benchmark_manifest: Mapping[str, Any],
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    candidate_context: Mapping[str, Any] | None = None,
    expected_source_digest: str | None = None,
    dependencies: IngestionDependencies | None = None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """Ingest exactly one contained PDF and return a digest-only readback."""

    PDF_POLICY.validate_benchmark(benchmark_manifest)
    data = (
        _read_anchored_source(source_path, policy=PDF_POLICY)
        if expected_source_digest is None
        else read_verified_pdf_source(source_path, expected_source_digest)
    )
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return _ingest_verified_document_for_policy(
        PDF_POLICY,
        data,
        source_digest=digest,
        resource="source-" + digest.removeprefix("sha256:")[:16] + ".pdf",
        benchmark_manifest=benchmark_manifest,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
        origin=None,
        candidate_context=candidate_context,
        dependencies=dependencies,
        candidate_root=candidate_root,
        benchmark_validated=True,
    )


def ingest_verified_document(
    data: bytes,
    *,
    source_digest: str,
    resource: str,
    benchmark_manifest: Mapping[str, Any],
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    candidate_context: Mapping[str, Any] | None = None,
    dependencies: IngestionDependencies | None = None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """Ingest already-verified immutable PDF bytes without reopening a path."""

    return _ingest_verified_document_for_policy(
        PDF_POLICY,
        data,
        source_digest=source_digest,
        resource=resource,
        benchmark_manifest=benchmark_manifest,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
        origin=None,
        candidate_context=candidate_context,
        dependencies=dependencies,
        candidate_root=candidate_root,
        benchmark_validated=False,
    )


def _validated_docx_origin(
    origin: DocxProvenanceOrigin, *, derived_digest: str
) -> dict[str, Any]:
    if type(origin) is not DocxProvenanceOrigin:
        raise IngestionError("verified document binding is invalid")
    if (
        origin.corpus_id != "docx-nomagic-uk-public-sector-v1"
        or origin.transformation_id != "restore-ooxml-local-header-v1"
        or not isinstance(origin.original_digest, str)
        or _SHA256_RE.fullmatch(origin.original_digest) is None
        or not isinstance(origin.expectation_digest, str)
        or _SHA256_RE.fullmatch(origin.expectation_digest) is None
        or not isinstance(origin.derived_digest, str)
        or _SHA256_RE.fullmatch(origin.derived_digest) is None
        or origin.original_digest == origin.derived_digest
        or origin.derived_digest != derived_digest
    ):
        raise IngestionError("verified document binding is invalid")
    return {
        "format_id": "docx",
        "corpus_id": origin.corpus_id,
        "original_digest": origin.original_digest,
        "derived_digest": origin.derived_digest,
        "transformation_id": origin.transformation_id,
        "expectation_digest": origin.expectation_digest,
    }


def ingest_docx_document(
    source_path: str | Path,
    *,
    benchmark_manifest: Mapping[str, Any],
    expected_corpus_digest: str,
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    origin: DocxProvenanceOrigin | None = None,
    candidate_context: Mapping[str, Any] | None = None,
    expected_source_digest: str | None = None,
    dependencies: IngestionDependencies | None = None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """Ingest exactly one contained DOCX and return a digest-only readback."""

    if origin is None or type(expected_corpus_digest) is not str:
        raise IngestionError("verified document binding is invalid")
    _validate_docx_benchmark_with_expected_corpus(
        benchmark_manifest, expected_corpus_digest=expected_corpus_digest
    )
    data = (
        read_verified_docx_source(source_path, origin.derived_digest)
        if expected_source_digest is None
        else read_verified_docx_source(source_path, expected_source_digest)
    )
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return _ingest_verified_document_for_policy(
        DOCX_POLICY,
        data,
        source_digest=digest,
        resource="source-" + digest.removeprefix("sha256:")[:16] + ".docx",
        benchmark_manifest=benchmark_manifest,
        expected_corpus_digest=expected_corpus_digest,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
        origin=origin,
        candidate_context=candidate_context,
        dependencies=dependencies,
        candidate_root=candidate_root,
        benchmark_validated=True,
    )


def ingest_verified_docx(
    data: bytes,
    *,
    source_digest: str,
    resource: str,
    benchmark_manifest: Mapping[str, Any],
    expected_corpus_digest: str,
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    origin: DocxProvenanceOrigin | None = None,
    candidate_context: Mapping[str, Any] | None = None,
    dependencies: IngestionDependencies | None = None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """Ingest already-verified immutable DOCX bytes without reopening a path."""

    try:
        from .docx_ooxml import validate_docx_package

        validate_docx_package(data)
    except Exception as exc:
        raise IngestionError("source media does not match DOCX") from exc
    return _ingest_verified_document_for_policy(
        DOCX_POLICY,
        data,
        source_digest=source_digest,
        resource=resource,
        benchmark_manifest=benchmark_manifest,
        expected_corpus_digest=expected_corpus_digest,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
        origin=origin,
        candidate_context=candidate_context,
        dependencies=dependencies,
        candidate_root=candidate_root,
        benchmark_validated=False,
    )


_OCR_MEDIA = {
    "application/pdf": (".pdf", b"%PDF-"),
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": (".jpg", b"\xff\xd8"),
}


def ingest_verified_ocr_document(
    data: bytes,
    *,
    source_digest: str,
    resource: str,
    media_type: str,
    activation: object,
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    candidate_context: Mapping[str, Any] | None = None,
    dependencies: IngestionDependencies | None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """Ingest immutable OCR bytes only through one reviewed activation."""

    from .model_roles import ActivatedOcrParser
    from .ocr_ir import OCR_IR_CONFIGURATION_DIGEST

    if (
        type(activation) is not ActivatedOcrParser
        or activation.capability != "ocr-layout"
        or activation.parser_id != "paddle-ocr-english"
        or activation.parser_version != "0.1.0"
        or activation.provider_enabled
        or activation.fallback_enabled
        or dependencies is None
    ):
        raise IngestionError("qualified OCR parser is unavailable")
    if type(data) is not bytes or not 1 <= len(data) <= MAXIMUM_PDF_BYTES:
        raise IngestionError("verified OCR document bytes are invalid")
    media = _OCR_MEDIA.get(media_type)
    if media is None or not data.startswith(media[1]):
        raise IngestionError("source media does not match OCR input")
    extension = media[0]
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    expected_resource = "source-" + digest[7:23] + extension
    if source_digest != digest or resource != expected_resource:
        raise IngestionError("verified document binding is invalid")
    policy = VerifiedDocumentPolicy(
        format_id="ocr",
        media_type=media_type,
        extension=extension,
        parser_id=activation.parser_id,
        parser_version=activation.parser_version,
        required_structural_elements=("ocr", "layout"),
        validate_benchmark=lambda value: None,
        build_dependencies=lambda value: dependencies,
    )
    qualification_binding = {
        "result_digest": activation.qualification_digest,
        "parser_configuration_digest": OCR_IR_CONFIGURATION_DIGEST,
    }
    return _ingest_verified_document_for_policy(
        policy,
        data,
        source_digest=digest,
        resource=resource,
        benchmark_manifest=qualification_binding,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
        origin=None,
        candidate_context=candidate_context,
        dependencies=dependencies,
        candidate_root=candidate_root,
        benchmark_validated=True,
    )


def ingest_ocr_document(
    source_path: str | Path,
    *,
    benchmark_manifest: Mapping[str, Any],
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    candidate_context: Mapping[str, Any] | None = None,
    expected_source_digest: str | None = None,
    dependencies: IngestionDependencies | None = None,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """Ingest one reviewed scanned PDF through the activated OCR parser."""

    from .model_roles import ActivatedOcrParser

    if dependencies is None or type(dependencies.ocr_activation) is not ActivatedOcrParser:
        raise IngestionError("qualified OCR parser is unavailable")
    activation = dependencies.ocr_activation
    if (
        type(benchmark_manifest) is not dict
        or set(benchmark_manifest) != {
            "qualification_digest", "selected_candidate_id", "model_set_digest"
        }
        or benchmark_manifest != {
            "qualification_digest": activation.qualification_digest,
            "selected_candidate_id": activation.candidate_id,
            "model_set_digest": activation.model_set_digest,
        }
    ):
        raise IngestionError("OCR qualification binding is invalid")
    policy = VerifiedDocumentPolicy(
        format_id="ocr", media_type="application/pdf", extension=".pdf",
        parser_id=activation.parser_id, parser_version=activation.parser_version,
        required_structural_elements=("ocr", "layout"),
        validate_benchmark=lambda value: None,
        build_dependencies=lambda value: dependencies,
    )
    data = (
        _read_anchored_source(source_path, policy=policy)
        if expected_source_digest is None
        else read_verified_pdf_source(source_path, expected_source_digest)
    )
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return ingest_verified_ocr_document(
        data,
        source_digest=digest,
        resource="source-" + digest[7:23] + ".pdf",
        media_type="application/pdf",
        activation=activation,
        selection_profile=selection_profile,
        quality_profile=quality_profile,
        candidate_context=candidate_context,
        dependencies=dependencies,
        candidate_root=candidate_root,
    )


def _ingest_verified_document_for_policy(
    policy: VerifiedDocumentPolicy,
    data: bytes,
    *,
    source_digest: str,
    resource: str,
    benchmark_manifest: Mapping[str, Any],
    expected_corpus_digest: str | None = None,
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    origin: DocxProvenanceOrigin | None,
    candidate_context: Mapping[str, Any] | None,
    dependencies: IngestionDependencies | None,
    candidate_root: Path | None,
    benchmark_validated: bool,
) -> dict[str, Any]:
    if not benchmark_validated:
        if policy.format_id == "docx":
            if type(expected_corpus_digest) is not str:
                raise IngestionError("verified document binding is invalid")
            _validate_docx_benchmark_with_expected_corpus(
                benchmark_manifest, expected_corpus_digest=expected_corpus_digest
            )
        else:
            policy.validate_benchmark(benchmark_manifest)
    if type(data) is not bytes:
        raise IngestionError("verified document bytes are invalid")
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    expected_resource = (
        "source-" + digest.removeprefix("sha256:")[:16] + policy.extension
    )
    if (
        type(source_digest) is not str
        or source_digest != digest
        or type(resource) is not str
        or resource != expected_resource
    ):
        raise IngestionError("verified document binding is invalid")
    source_origin = None
    if policy.format_id == "docx":
        if type(expected_corpus_digest) is not str or origin is None:
            raise IngestionError("verified document binding is invalid")
        source_origin = _validated_docx_origin(origin, derived_digest=digest)
    if policy.format_id != "docx" and origin is not None:
        raise IngestionError("verified document binding is invalid")
    if dependencies is None and policy.format_id == "docx":
        runtime = default_docx_ingestion_dependencies(
            benchmark_manifest, expected_corpus_digest=expected_corpus_digest
        )
    else:
        runtime = dependencies or policy.build_dependencies(benchmark_manifest)
    parse_result = _parse_document(
        policy,
        runtime,
        data,
        selection_profile,
        quality_profile,
        benchmark_manifest,
    )
    document_ir = parse_result["document_ir"]
    distillation_result = _distill(runtime, document_ir, candidate_context)
    try:
        proposed = build_candidate_provenance(
            parse_result,
            distillation_result,
            created_at=runtime.now(),
            source_origin=source_origin,
        )
        candidate_id = distillation_result["candidate"]["candidate_id"]
        persistence, provenance = _persist_with_collision_reconciliation(
            distillation_result,
            proposed,
            candidate_root=candidate_root,
        )
        reopened = load_verified_candidate(
            candidate_id, candidate_root=candidate_root
        )
        inspection = reopened["inspection"]
        bound = reopened["provenance"]
    except (BenchmarkError, CandidateError, KeyError, TypeError, ValueError) as exc:
        raise IngestionError("candidate persistence failed") from exc
    return {
        "schema_version": "ao.lore.single-document-ingest-readback.v0.1",
        "status": persistence["status"],
        "candidate_id": candidate_id,
        "candidate_digest": inspection["candidate_digest"],
        "provenance_digest": canonical_digest(bound),
        "source_digest": bound["source_digest"],
        "parser_id": bound["parser_id"],
        "parser_version": bound["parser_version"],
        "document_ir_digest": bound["document_ir_digest"],
        "parser_selection_report_digest": bound[
            "parser_selection_report_digest"
        ],
        "parse_quality_report_digest": bound["parse_quality_report_digest"],
        "distillation_trace_digest": bound["distillation_trace_digest"],
        "review_status": inspection["review_status"],
        "next_commands": [
            f"ao-lore candidate inspect --candidate-id {candidate_id}",
            f"ao-lore candidate review --candidate-id {candidate_id} --decision accept --reviewer <reviewer-id>",
        ],
        "canonical": False,
        "promotion_authority": False,
    }
