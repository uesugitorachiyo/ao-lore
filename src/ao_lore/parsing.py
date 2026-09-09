"""Bounded production parsing into the canonical AO Lore document IR.

The runtime selects exactly one eligible adapter from benchmark-backed
capability evidence. It may invoke only the next-ranked adapter after a
recorded numeric quality failure. No code in this module creates OKF concepts
or writes to the canonical brain.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import os
import re
import stat
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .benchmark import BenchmarkError, canonical_digest, verify_manifest
from .scoring import ScoringError, compute_parse_quality, select_parser


class ParsingError(ValueError):
    """Raised when a source or parser boundary fails closed."""


def validate_docling_benchmark(
    manifest: Mapping[str, Any],
    *,
    expected_configuration_digest: str | None = None,
) -> None:
    """Require exact identity, configuration, and measured qualification floors."""

    try:
        verify_manifest(
            manifest,
            expected_configuration_digest=expected_configuration_digest,
        )
    except BenchmarkError as exc:
        raise ParsingError("Docling benchmark evidence is invalid") from exc
    if (
        manifest.get("parser_id") != "docling"
        or manifest.get("parser_version") != "2.118.1"
        or manifest.get("document_ir_version") != "ao.lore.document-ir.v0.1"
        or manifest.get("failures")
    ):
        raise ParsingError("Docling benchmark evidence does not qualify the adapter")
    minimum_scores = {
        "structural_fidelity": 0.5,
        "text_fidelity": 0.5,
        "source_location_fidelity": 0.5,
        "reliability": 1.0,
        "deterministic_repeatability": 1.0,
    }
    scores = manifest.get("normalized_scores")
    if not isinstance(scores, Mapping) or any(
        isinstance(scores.get(name), bool)
        or not isinstance(scores.get(name), (int, float))
        or scores[name] < minimum
        for name, minimum in minimum_scores.items()
    ):
        raise ParsingError("Docling benchmark evidence is below qualification minimums")


@dataclass(frozen=True)
class ParseOutput:
    document_ir: Mapping[str, Any]
    quality_components: Mapping[str, float | None]
    critical_failures: Sequence[str] = field(default_factory=tuple)


def build_ocr_parse_output(source: Mapping[str, Any], worker_result: object) -> ParseOutput:
    """Build a parse result from a detached, already-sandboxed OCR readback."""

    from .ocr_ir import canonicalize_ocr_result

    provenance = {
        "resource": source.get("resource"),
        "digest": source.get("digest"),
        "media_type": source.get("media_type"),
    }
    document_ir = canonicalize_ocr_result(worker_result, provenance)
    return ParseOutput(
        document_ir=document_ir,
        quality_components={
            "text_coverage": 1.0,
            "structural_completeness": 1.0,
            "source_span_coverage": 1.0,
            "document_ir_valid": 1.0,
        },
    )


class ParserAdapter(Protocol):
    @property
    def capability(self) -> Mapping[str, Any]: ...

    def parse(self, source: Mapping[str, Any]) -> ParseOutput: ...


class ScriptedParserAdapter:
    """Offline deterministic adapter for tests and local policy fixtures."""

    def __init__(self, capability: Mapping[str, Any], handler: Callable[[Mapping[str, Any]], ParseOutput]):
        self._capability = copy.deepcopy(dict(capability))
        self._handler = handler

    @property
    def capability(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._capability)

    def parse(self, source: Mapping[str, Any]) -> ParseOutput:
        result = self._handler(copy.deepcopy(dict(source)))
        if not isinstance(result, ParseOutput):
            raise ParsingError("scripted parser must return ParseOutput")
        return result


class QualifiedOcrAdapter:
    """Bind one explicit OCR activation to one sandboxed execution seam."""

    def __init__(
        self,
        activation: object,
        execute: Callable[[Mapping[str, Any]], object],
    ) -> None:
        from .model_roles import ActivatedOcrParser

        if (
            type(activation) is not ActivatedOcrParser
            or activation.capability != "ocr-layout"
            or activation.parser_id != "paddle-ocr-english"
            or activation.parser_version != "0.1.0"
            or activation.provider_enabled
            or activation.fallback_enabled
            or not callable(execute)
        ):
            raise ParsingError("OCR activation evidence is invalid")
        profiles = default_capability_profiles(ocr_activation=activation)
        matches = [
            item for item in profiles
            if item.get("parser_id") == "paddle-ocr-english"
        ]
        if len(matches) != 1:
            raise ParsingError("OCR activation evidence is invalid")
        self._activation = activation
        self._capability = copy.deepcopy(matches[0])
        self._execute = execute

    @property
    def capability(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._capability)

    def parse(self, source: Mapping[str, Any]) -> ParseOutput:
        from .paddle_ocr import OCR_WORKER_CONFIGURATION_DIGEST

        try:
            result = self._execute(copy.deepcopy(dict(source)))
            if (
                type(result) is not dict
                or result.get("candidate_id") != self._activation.candidate_id
                or result.get("model_set_digest")
                != self._activation.model_set_digest
                or result.get("runtime_digest") != self._activation.runtime_digest
                or result.get("configuration_digest")
                != OCR_WORKER_CONFIGURATION_DIGEST
                or any(result.get(field) is not False for field in (
                    "network_accessed", "provider_calls", "promotion",
                    "publication", "release", "deployment",
                    "authority_advanced",
                ))
            ):
                raise ParsingError("OCR worker result binding differs")
            return build_ocr_parse_output(source, result)
        except ParsingError:
            raise
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise ParsingError("qualified OCR parser failed") from exc


class ParserRegistry:
    """Identity-keyed registry that retains extension capability fields."""

    def __init__(self) -> None:
        self._adapters: dict[str, ParserAdapter] = {}

    def register(self, adapter: ParserAdapter) -> None:
        capability = adapter.capability
        parser_id = capability.get("parser_id")
        if not isinstance(parser_id, str) or not parser_id:
            raise ParsingError("parser capability requires parser_id")
        if parser_id in self._adapters:
            raise ParsingError(f"duplicate parser identity: {parser_id}")
        self._adapters[parser_id] = adapter

    def adapter(self, parser_id: str) -> ParserAdapter:
        try:
            return self._adapters[parser_id]
        except KeyError as exc:
            raise ParsingError(f"unknown parser: {parser_id}") from exc

    def capabilities(self) -> list[Mapping[str, Any]]:
        return [self._adapters[key].capability for key in sorted(self._adapters)]


def register_docling_adapter(
    registry: ParserRegistry, benchmark: Mapping[str, Any]
) -> None:
    """Register the optional real adapter only from supplied benchmark evidence."""

    from .docling_pdf import DoclingPdfAdapter

    registry.register(DoclingPdfAdapter(benchmark))


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read_bounded_source(path: str | Path, source_root: str | Path, maximum_bytes: int) -> bytes:
    """Open one contained regular file without following a final symlink."""

    candidate = Path(path)
    root = Path(source_root).resolve(strict=True)
    if maximum_bytes < 0:
        raise ParsingError("maximum_bytes must be non-negative")
    try:
        resolved_parent = candidate.parent.resolve(strict=True)
        resolved_parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise ParsingError("source path is outside the configured root") from exc
    if candidate.is_symlink():
        raise ParsingError("source symlinks are not allowed")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise ParsingError("source could not be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ParsingError("source must be a regular file")
        if metadata.st_size > maximum_bytes:
            raise ParsingError("source exceeds configured byte limit")
        chunks: list[bytes] = []
        remaining = maximum_bytes + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum_bytes:
            raise ParsingError("source exceeds configured byte limit")
        return data
    finally:
        os.close(descriptor)


def base_capability(
    parser_id: str,
    version: str,
    media_types: Sequence[str],
    extensions: Sequence[str],
    elements: Sequence[str],
) -> dict[str, Any]:
    return {
        "parser_id": parser_id,
        "version": version,
        "supported_mime_types": list(media_types),
        "supported_extensions": list(extensions),
        "structural_elements": list(elements),
        "format_status": {media_type: "native" for media_type in media_types},
        "ocr_capable": False,
        "layout_aware": False,
        "table_extraction": "none",
        "link_preservation": True,
        "footnote_preservation": False,
        "source_coordinates": False,
        "image_handling": False,
        "caption_handling": False,
        "language_coverage": ["und"],
        "deterministic_offline": True,
        "frontier_service_required": False,
        "expected_latency_class": "low",
        "expected_memory_class": "low",
        "expected_monetary_cost": 0,
        "runtime_available": True,
        "license_id": "Apache-2.0",
        "redistribution_allowed": True,
        "sandbox_supported": True,
        "security_requirements": ["regular-file-containment", "bounded-input"],
        "max_file_bytes": 50 * 1024 * 1024,
        "canonical_ir_versions": ["ao.lore.document-ir.v0.1"],
        "selection_metrics": {
            "structural_fidelity": 1.0,
            "text_fidelity": 1.0,
            "source_location_fidelity": 0.8,
            "reliability": 1.0,
            "deterministic_repeatability": 1.0,
            "latency": 0.05,
            "memory": 0.05,
            "external_cost": 0.0,
        },
        "benchmark_result_refs": [],
    }


class NativeMarkdownAdapter:
    def __init__(self) -> None:
        self._capability = base_capability(
            "native-markdown", "1.0.0", ["text/markdown"], [".md", ".markdown"], ["headings", "links", "code", "footnotes"]
        )
        self._capability["benchmark_result_refs"] = [canonical_digest({"adapter": "native-markdown", "version": "1.0.0", "fixture_policy": "v0.1"})]

    @property
    def capability(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._capability)

    def parse(self, source: Mapping[str, Any]) -> ParseOutput:
        data = source.get("data")
        if not isinstance(data, bytes):
            raise ParsingError("source data must be bytes")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ParsingError("Markdown source is not valid UTF-8") from exc
        blocks: list[dict[str, Any]] = []
        in_code = False
        code_start = 0
        code_lines: list[str] = []
        offset = 0
        for line in text.splitlines(keepends=True):
            stripped = line.rstrip("\r\n")
            if stripped.startswith("```"):
                if in_code:
                    code_text = "".join(code_lines).rstrip("\r\n")
                    blocks.append(_block(len(blocks), "code", code_text, code_start, offset + len(stripped)))
                    code_lines = []
                    in_code = False
                else:
                    in_code = True
                    code_start = offset
                offset += len(line)
                continue
            if in_code:
                code_lines.append(line)
                offset += len(line)
                continue
            heading_match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
            if heading_match:
                block = _block(len(blocks), "heading", heading_match.group(2), offset, offset + len(stripped))
                block["level"] = len(heading_match.group(1))
                blocks.append(block)
            elif stripped.strip():
                blocks.append(_block(len(blocks), "paragraph", stripped, offset, offset + len(stripped)))
                for link in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", stripped):
                    link_block = _block(
                        len(blocks), "link", link.group(1), offset + link.start(), offset + link.end()
                    )
                    link_block["attributes"] = {"target": link.group(2)}
                    blocks.append(link_block)
            offset += len(line)
        if in_code:
            blocks.append(_block(len(blocks), "code", "".join(code_lines).rstrip("\r\n"), code_start, len(text)))
        document_ir = build_document_ir(source, self._capability, blocks, {"format": "markdown"})
        return ParseOutput(document_ir, _native_quality(text, blocks))


class _StructuralHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._tag: str | None = None
        self._attrs: dict[str, str] = {}
        self._parts: list[str] = []
        self._offset = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "pre", "code", "figcaption", "li"}:
            self._tag = tag
            self._attrs = {key: value or "" for key, value in attrs}
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._tag is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != self._tag:
            return
        text = "".join(self._parts).strip()
        if text:
            if tag.startswith("h") and len(tag) == 2:
                block_type = "heading"
            else:
                block_type = {"a": "link", "pre": "code", "code": "code", "figcaption": "caption", "li": "list"}.get(tag, "paragraph")
            block = _block(len(self.blocks), block_type, text, self._offset, self._offset + len(text))
            if block_type == "heading":
                block["level"] = int(tag[1])
            if block_type == "link":
                block["attributes"] = {"target": self._attrs.get("href", "")}
            self.blocks.append(block)
            self._offset += len(text)
        self._tag = None
        self._attrs = {}
        self._parts = []


class NativeHTMLAdapter:
    def __init__(self) -> None:
        self._capability = base_capability(
            "native-html", "1.0.0", ["text/html", "application/xhtml+xml"], [".html", ".htm", ".xhtml"], ["headings", "links", "code", "images", "captions"]
        )
        self._capability["layout_aware"] = True
        self._capability["benchmark_result_refs"] = [canonical_digest({"adapter": "native-html", "version": "1.0.0", "fixture_policy": "v0.1"})]

    @property
    def capability(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._capability)

    def parse(self, source: Mapping[str, Any]) -> ParseOutput:
        data = source.get("data")
        if not isinstance(data, bytes):
            raise ParsingError("source data must be bytes")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ParsingError("HTML source is not valid UTF-8") from exc
        parser = _StructuralHTMLParser()
        parser.feed(text)
        document_ir = build_document_ir(source, self._capability, parser.blocks, {"format": "html"})
        return ParseOutput(document_ir, _native_quality(text, parser.blocks))


class PlainTextAdapter:
    def __init__(self) -> None:
        self._capability = base_capability("direct-text", "1.0.0", ["text/plain"], [".txt"], ["text"])
        self._capability["benchmark_result_refs"] = [canonical_digest({"adapter": "direct-text", "version": "1.0.0", "fixture_policy": "v0.1"})]

    @property
    def capability(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._capability)

    def parse(self, source: Mapping[str, Any]) -> ParseOutput:
        data = source.get("data")
        if not isinstance(data, bytes):
            raise ParsingError("source data must be bytes")
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ParsingError("plain-text source is not valid UTF-8") from exc
        blocks: list[dict[str, Any]] = []
        offset = 0
        for line in text.splitlines(keepends=True):
            value = line.rstrip("\r\n")
            if value:
                blocks.append(_block(len(blocks), "paragraph", value, offset, offset + len(value)))
            offset += len(line)
        document_ir = build_document_ir(source, self._capability, blocks, {"format": "text"})
        return ParseOutput(document_ir, _native_quality(text, blocks))


def _block(index: int, block_type: str, text: str, start: int, end: int) -> dict[str, Any]:
    return {"id": f"b{index + 1}", "type": block_type, "text": text, "source_span": {"start": start, "end": end}}


def build_document_ir(
    source: Mapping[str, Any], capability: Mapping[str, Any], blocks: Sequence[Mapping[str, Any]], metadata: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": "ao.lore.document-ir.v0.1",
        "document_id": source["digest"],
        "source": {"resource": source["resource"], "digest": source["digest"], "media_type": source["media_type"]},
        "parser": {
            "parser_id": capability["parser_id"],
            "parser_version": capability["version"],
            "configuration_digest": canonical_digest({}),
        },
        "blocks": [copy.deepcopy(dict(block)) for block in blocks],
        "metadata": dict(metadata),
    }


def _native_quality(text: str, blocks: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    readable = sum(len(str(block.get("text", ""))) for block in blocks)
    source_span_count = sum(isinstance(block.get("source_span"), Mapping) for block in blocks)
    return {
        "text_coverage": min(1.0, readable / max(1, len(text.strip()))),
        "structural_completeness": 1.0 if blocks else 0.0,
        "source_span_coverage": source_span_count / max(1, len(blocks)),
        "document_ir_valid": 1.0,
    }


def _validate_ir(ir: Mapping[str, Any], source: Mapping[str, Any], parser_id: str) -> list[str]:
    failures: list[str] = []
    if ir.get("schema_version") != "ao.lore.document-ir.v0.1" or not isinstance(ir.get("blocks"), list):
        failures.append("invalid_ir")
        return failures
    ir_source = ir.get("source")
    if not isinstance(ir_source, Mapping):
        failures.extend(["invalid_ir", "lost_provenance"])
        return sorted(set(failures))
    if ir_source.get("digest") != source["digest"]:
        failures.append("source_digest_mismatch")
    if ir_source.get("resource") != source["resource"] or ir_source.get("media_type") != source["media_type"]:
        failures.append("lost_provenance")
    parser = ir.get("parser")
    if not isinstance(parser, Mapping) or parser.get("parser_id") != parser_id:
        failures.append("invalid_ir")
    allowed_types = {"heading", "paragraph", "list", "table", "code", "image", "caption", "footnote", "link"}
    for block in ir["blocks"]:
        if not isinstance(block, Mapping) or block.get("type") not in allowed_types or not isinstance(block.get("text"), str):
            failures.append("invalid_ir")
            continue
        span = block.get("source_span")
        if not isinstance(span, Mapping) or not isinstance(span.get("start"), int) or not isinstance(span.get("end"), int):
            failures.extend(["invalid_ir", "lost_provenance"])
        elif span["start"] < 0 or span["end"] < span["start"]:
            failures.append("invalid_ir")
    if not any(str(block.get("text", "")).strip() for block in ir["blocks"] if isinstance(block, Mapping)):
        failures.append("no_readable_content")
    return sorted(set(failures))


def _contract_quality_report(
    internal: Mapping[str, Any], capability: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "schema_version": "ao.lore.parse-quality-report.v0.1",
        "document_digest": internal["document_digest"],
        "parser_id": internal["parser_id"],
        "parser_version": capability["version"],
        "threshold_profile": internal["profile_id"],
        "components": [
            {
                "name": name,
                "applicable": item["applicable"],
                "score": item["score"],
                "reason": "measured" if item["applicable"] else "not applicable to format profile",
            }
            for name, item in sorted(internal["components"].items())
        ],
        "overall_quality_score": internal["overall_quality_score"],
        "critical_failures": internal["critical_failures"],
        "decision": internal["decision"],
    }


class ProductionParser:
    def __init__(self, registry: ParserRegistry):
        self._registry = registry

    def parse_bytes(
        self,
        data: bytes,
        resource: str,
        request: Mapping[str, Any],
        selection_profile: Mapping[str, Any],
        quality_profile: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(data, bytes):
            raise ParsingError("production parser accepts bytes")
        source = {
            "resource": resource,
            "digest": _sha256(data),
            "media_type": request.get("media_type"),
            "data": data,
        }
        selection_request = dict(request)
        selection_request["file_bytes"] = len(data)
        try:
            selection = select_parser(source["digest"], self._registry.capabilities(), selection_request, selection_profile)
        except ScoringError as exc:
            raise ParsingError(str(exc)) from exc
        ranked = [item["parser_id"] for item in selection["candidates"]]
        maximum_attempts = min(2, 1 + int(quality_profile.get("maximum_fallbacks", 0)), len(ranked))
        attempts: list[dict[str, Any]] = []
        final_ir: Mapping[str, Any] | None = None
        final_report: dict[str, Any] | None = None
        for attempt_index, parser_id in enumerate(ranked[:maximum_attempts]):
            adapter = self._registry.adapter(parser_id)
            capability = adapter.capability
            output: ParseOutput | None = None
            failures: list[str] = []
            components: Mapping[str, float | None] = {
                name: 0.0 for name in quality_profile.get("component_weights", {})
            }
            try:
                output = adapter.parse(source)
                components = dict(output.quality_components)
                failures.extend(output.critical_failures)
                failures.extend(_validate_ir(output.document_ir, source, parser_id))
                if failures and "document_ir_valid" in components:
                    components = {**components, "document_ir_valid": 0.0}
            except Exception:
                failures.append("parser_crash")
            failures = sorted(set(failures))
            try:
                internal_report = compute_parse_quality(
                    source["digest"], parser_id, components, quality_profile, attempt_index, failures
                )
            except ScoringError as exc:
                raise ParsingError(str(exc)) from exc
            quality_report = _contract_quality_report(internal_report, capability)
            attempts.append(
                {
                    "parser_id": parser_id,
                    "selection_score": next(item["selection_score"] for item in selection["candidates"] if item["parser_id"] == parser_id),
                    "quality_report": quality_report,
                }
            )
            final_report = quality_report
            if quality_report["decision"] == "accept" and output is not None:
                final_ir = output.document_ir
                break
            if quality_report["decision"] != "fallback":
                break
        if final_report is None:
            raise ParsingError("no parser attempt was made")
        if final_ir is None and final_report["decision"] == "fallback":
            final_report = {**final_report, "decision": quality_profile.get("exhausted_decision", "quarantine")}
            attempts[-1] = {**attempts[-1], "quality_report": final_report}
        return {
            "schema_version": "ao.lore.production-parse-result.v0.1",
            "document_digest": source["digest"],
            "selection_report": selection,
            "attempts": attempts,
            "selected_parser": attempts[-1]["parser_id"],
            "quality_report": final_report,
            "decision": final_report["decision"],
            "document_ir": copy.deepcopy(final_ir) if final_ir is not None else None,
        }


def default_capability_profiles(
    docling_benchmark: Mapping[str, Any] | None = None,
    docx_benchmark: Mapping[str, Any] | None = None,
    docx_expected_corpus_digest: str | None = None,
    ocr_activation: object | None = None,
) -> list[dict[str, Any]]:
    """Return versioned initial profiles; Docling activates only with verified evidence."""

    markdown = NativeMarkdownAdapter().capability
    html = NativeHTMLAdapter().capability
    text = PlainTextAdapter().capability
    docx = base_capability(
        "native-docx-ooxml", "1.0.0", ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"], [".docx"], ["headings", "links", "tables", "footnotes", "images"]
    )
    docx.update(
        {
            "table_extraction": "structured",
            "footnote_preservation": True,
            "image_handling": True,
            "runtime_available": False,
            "benchmark_result_refs": [],
            "implementation_status": "qualification-required",
            "benchmark_evidence_status": "required",
        }
    )
    image = base_capability("ocr-layout", "0.1.0", ["image/png", "image/jpeg"], [".png", ".jpg", ".jpeg"], ["ocr", "layout", "images", "captions"])
    image.update({"runtime_available": False, "ocr_capable": True, "layout_aware": True, "benchmark_result_refs": [], "implementation_status": "adapter-required"})
    if ocr_activation is not None:
        from .model_roles import ActivatedOcrParser

        if type(ocr_activation) is not ActivatedOcrParser or (
            ocr_activation.capability != "ocr-layout"
            or ocr_activation.parser_id != "paddle-ocr-english"
            or ocr_activation.provider_enabled
            or ocr_activation.fallback_enabled
        ):
            raise ParsingError("OCR activation evidence is invalid")
        image["parser_id"] = ocr_activation.parser_id
        image["version"] = ocr_activation.parser_version
        image["supported_mime_types"] = [
            "application/pdf", "image/png", "image/jpeg",
        ]
        image["supported_extensions"] = [".pdf", ".png", ".jpg", ".jpeg"]
        image["runtime_available"] = True
        image["benchmark_result_refs"] = [ocr_activation.qualification_digest]
        image["implementation_status"] = "qualified"
        image["capability_id"] = ocr_activation.capability
        image["selected_candidate_id"] = ocr_activation.candidate_id
        image["selected_model_set_digest"] = ocr_activation.model_set_digest
        image["selected_result_digest"] = ocr_activation.selected_result_digest
        image["provider_enabled"] = False
        image["fallback_enabled"] = False
        image["selection_metrics"].update(
            {
                "text_fidelity": ocr_activation.character_accuracy_millionths / 1_000_000,
                "structural_fidelity": ocr_activation.detection_recall_millionths / 1_000_000,
                "source_location_fidelity": min(
                    ocr_activation.mean_polygon_iou_millionths,
                    ocr_activation.page_coverage_millionths,
                ) / 1_000_000,
                "deterministic_repeatability": 1.0,
            }
        )
    docling = base_capability("docling", "external", ["application/pdf"], [".pdf"], ["headings", "links", "tables", "code", "images", "captions", "footnotes", "source_coordinates"])
    docling.update({"layout_aware": True, "table_extraction": "structured", "image_handling": True, "caption_handling": True, "source_coordinates": True})
    if docling_benchmark is None:
        docling.update({"runtime_available": False, "benchmark_result_refs": [], "benchmark_evidence_status": "required"})
    else:
        validate_docling_benchmark(docling_benchmark)
        docling["version"] = docling_benchmark["parser_version"]
        docling["benchmark_result_refs"] = [docling_benchmark["result_digest"]]
        docling["selection_metrics"].update(docling_benchmark["normalized_scores"])
        docling["runtime_available"] = importlib.util.find_spec("docling") is not None
        docling["benchmark_evidence_status"] = "verified"
    if docx_benchmark is not None:
        from .docx_ooxml import validate_docx_benchmark, DocxLimits, docx_configuration_digest

        if docx_expected_corpus_digest is None:
            raise ParsingError("DOCX benchmark evidence is invalid")
        validated = validate_docx_benchmark(
            docx_benchmark,
            expected_configuration_digest=docx_configuration_digest(DocxLimits()),
            expected_corpus_digest=docx_expected_corpus_digest,
        )
        if validated["decision"] == "hold":
            docx["benchmark_result_refs"] = [validated["result_digest"]]
            docx["selection_metrics"].update(
                {
                    "text_fidelity": validated["metrics"]["text_fidelity"],
                    "structural_fidelity": validated["metrics"]["structural_fidelity"],
                    "source_location_fidelity": validated["metrics"]["source_location_fidelity"],
                    "reliability": 1.0,
                    "deterministic_repeatability": validated["repeatability"]["score"],
                }
            )
            docx["runtime_available"] = True
            docx["benchmark_evidence_status"] = "verified"
    return [copy.deepcopy(item) for item in (docling, markdown, html, docx, text, image)]
