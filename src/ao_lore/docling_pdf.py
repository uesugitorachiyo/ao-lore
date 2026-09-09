"""Optional, evidence-bound Docling adapter for born-digital PDFs."""

from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, Mapping, Protocol, Sequence

from .benchmark import canonical_digest
from .parsing import (
    ParseOutput,
    ParsingError,
    base_capability,
    build_document_ir,
    validate_docling_benchmark,
)


DOCLING_VERSION = "2.118.1"
SOURCE_KEYS = {"resource", "digest", "media_type", "data"}
BLOCK_TYPES = {
    "heading", "paragraph", "list", "table", "code", "image", "caption",
    "footnote", "link",
}
BLOCK_KEYS = {"type", "text", "page", "coordinates", "level", "attributes"}


@dataclass(frozen=True)
class ConvertedPdf:
    status: str
    blocks: Sequence[Mapping[str, Any]]
    page_count: int
    errors: Sequence[str] = field(default_factory=tuple)


class PdfBackend(Protocol):
    version: str
    allowed_formats: Sequence[str]
    ocr_enabled: bool

    def convert(
        self,
        data: bytes,
        name: str,
        *,
        max_file_size: int,
        max_num_pages: int,
    ) -> ConvertedPdf: ...


def configuration_digest(max_file_size: int, max_num_pages: int, max_blocks: int) -> str:
    return canonical_digest({
        "allowed_formats": ["pdf"],
        "do_ocr": False,
        "max_file_size": max_file_size,
        "max_num_pages": max_num_pages,
        "max_blocks": max_blocks,
    })


class DoclingBackend:
    """Translate the optional Docling API into primitive conversion records."""

    allowed_formats = ("pdf",)
    ocr_enabled = False

    def __init__(self) -> None:
        try:
            from importlib.metadata import version
            from docling.datamodel.base_models import DocumentStream, InputFormat
            from docling.datamodel.pipeline_options import PdfPipelineOptions
            from docling.document_converter import DocumentConverter, PdfFormatOption
        except (ImportError, ModuleNotFoundError) as exc:
            raise ParsingError("Docling PDF support is unavailable") from exc
        installed = version("docling")
        if installed != DOCLING_VERSION:
            raise ParsingError("Docling PDF support has an unsupported version")
        options = PdfPipelineOptions()
        options.do_ocr = False
        self.version = installed
        self._document_stream = DocumentStream
        self._converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=options),
            },
        )

    @staticmethod
    def _status(value: Any) -> str:
        raw = getattr(value, "value", None) or getattr(value, "name", None) or str(value)
        return str(raw).lower().rsplit(".", 1)[-1]

    @staticmethod
    def _coordinates(provenance: Any) -> tuple[int | None, list[float] | None]:
        entries = provenance if isinstance(provenance, (list, tuple)) else ()
        if not entries:
            return None, None
        item = entries[0]
        page = getattr(item, "page_no", None)
        bbox = getattr(item, "bbox", None)
        if bbox is None:
            return page, None
        values = []
        for names in (("l", "left"), ("t", "top"), ("r", "right"), ("b", "bottom")):
            value = next((getattr(bbox, name) for name in names if hasattr(bbox, name)), None)
            if isinstance(value, (int, float)) and math.isfinite(value):
                values.append(float(value))
        return page, values if len(values) == 4 else None

    @staticmethod
    def _text(item: Any, label: str) -> str:
        if label == "table" and hasattr(item, "export_to_markdown"):
            try:
                return str(item.export_to_markdown())
            except Exception:
                return ""
        return str(getattr(item, "text", "") or "")

    def _blocks(self, document: Any) -> tuple[dict[str, Any], ...]:
        label_map = {
            "title": "heading", "section_header": "heading",
            "text": "paragraph", "paragraph": "paragraph",
            "list_item": "list", "table": "table", "code": "code",
            "picture": "image", "caption": "caption",
            "footnote": "footnote", "link": "link",
        }
        blocks: list[dict[str, Any]] = []
        for entry in document.iterate_items():
            item = entry[0] if isinstance(entry, tuple) else entry
            raw_label = getattr(item, "label", "text")
            label = str(getattr(raw_label, "value", raw_label)).lower().rsplit(".", 1)[-1]
            block_type = label_map.get(label)
            text = self._text(item, label)
            if block_type is None:
                if not text.strip():
                    continue
                block_type = "paragraph"
            block: dict[str, Any] = {"type": block_type, "text": text}
            page, coordinates = self._coordinates(getattr(item, "prov", ()))
            if isinstance(page, int) and page >= 1:
                block["page"] = page
            if coordinates is not None:
                block["coordinates"] = coordinates
            if block_type == "heading":
                block["level"] = 1
            blocks.append(block)
        return tuple(blocks)

    def convert(
        self,
        data: bytes,
        name: str,
        *,
        max_file_size: int,
        max_num_pages: int,
    ) -> ConvertedPdf:
        stream = self._document_stream(name=name, stream=BytesIO(data))
        result = self._converter.convert(
            stream,
            max_file_size=max_file_size,
            max_num_pages=max_num_pages,
            raises_on_error=False,
        )
        document = getattr(result, "document", None)
        pages = getattr(document, "pages", {}) if document is not None else {}
        return ConvertedPdf(
            status=self._status(getattr(result, "status", "failure")),
            blocks=self._blocks(document) if document is not None else (),
            page_count=len(pages),
        )


class DoclingPdfAdapter:
    def __init__(
        self,
        benchmark: Mapping[str, Any],
        *,
        backend: PdfBackend | None = None,
        installed_version: str | None = None,
        max_file_size: int = 50 * 1024 * 1024,
        max_num_pages: int = 500,
        max_blocks: int = 100_000,
    ) -> None:
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (
            max_file_size, max_num_pages, max_blocks,
        )):
            raise ParsingError("Docling PDF limits must be positive integers")
        expected_configuration = configuration_digest(max_file_size, max_num_pages, max_blocks)
        validate_docling_benchmark(
            benchmark, expected_configuration_digest=expected_configuration
        )
        selected_backend = backend or DoclingBackend()
        actual_version = installed_version or selected_backend.version
        if actual_version != DOCLING_VERSION or selected_backend.version != actual_version:
            raise ParsingError("Docling PDF support has an unsupported version")
        if tuple(selected_backend.allowed_formats) != ("pdf",) or selected_backend.ocr_enabled is not False:
            raise ParsingError("Docling backend must be PDF-only with OCR disabled")
        self._backend = selected_backend
        self._max_file_size = max_file_size
        self._max_num_pages = max_num_pages
        self._max_blocks = max_blocks
        capability = base_capability(
            "docling", actual_version, ["application/pdf"], [".pdf"],
            ["headings", "links", "tables", "code", "images", "captions", "footnotes", "source_coordinates"],
        )
        capability.update({
            "layout_aware": True,
            "table_extraction": "structured",
            "image_handling": True,
            "caption_handling": True,
            "source_coordinates": True,
            "ocr_capable": False,
            "max_file_bytes": max_file_size,
            "benchmark_result_refs": [benchmark["result_digest"]],
            "benchmark_evidence_status": "verified",
        })
        capability["selection_metrics"].update(benchmark["normalized_scores"])
        self._capability = capability
        self._configuration_digest = expected_configuration

    @property
    def capability(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._capability)

    @staticmethod
    def _resource_name(resource: Any) -> str:
        if not isinstance(resource, str) or not resource or "://" in resource or "\\" in resource or "\x00" in resource:
            raise ParsingError("PDF resource is invalid")
        path = PurePosixPath(resource)
        if path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."} or path.suffix.lower() != ".pdf":
            raise ParsingError("PDF resource is invalid")
        return path.name

    def _blocks(self, converted: ConvertedPdf) -> list[dict[str, Any]]:
        if converted.status != "success" or converted.errors:
            raise ParsingError("PDF conversion did not complete")
        if not isinstance(converted.page_count, int) or not 1 <= converted.page_count <= self._max_num_pages:
            raise ParsingError("PDF conversion page count is invalid")
        if not converted.blocks or len(converted.blocks) > self._max_blocks:
            raise ParsingError("PDF conversion block count is invalid")
        output: list[dict[str, Any]] = []
        offset = 0
        for index, raw in enumerate(converted.blocks, start=1):
            if (
                not isinstance(raw, Mapping) or not set(raw).issubset(BLOCK_KEYS)
                or raw.get("type") not in BLOCK_TYPES or not isinstance(raw.get("text"), str)
            ):
                raise ParsingError("PDF conversion produced an invalid block")
            text = raw["text"]
            block: dict[str, Any] = {
                "id": f"b{index}", "type": raw["type"], "text": text,
                "source_span": {"start": offset, "end": offset + len(text)},
            }
            offset += len(text) + 1
            page = raw.get("page")
            if page is not None:
                if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= converted.page_count:
                    raise ParsingError("PDF conversion produced invalid coordinates")
                block["source_span"]["page"] = page
            coordinates = raw.get("coordinates")
            if coordinates is not None:
                if (
                    not isinstance(coordinates, (list, tuple)) or len(coordinates) != 4
                    or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in coordinates)
                ):
                    raise ParsingError("PDF conversion produced invalid coordinates")
                block["source_span"]["coordinates"] = [float(value) for value in coordinates]
            if "level" in raw:
                level = raw["level"]
                if isinstance(level, bool) or not isinstance(level, int) or level < 1:
                    raise ParsingError("PDF conversion produced an invalid heading level")
                block["level"] = level
            if "attributes" in raw:
                if not isinstance(raw["attributes"], Mapping):
                    raise ParsingError("PDF conversion produced invalid attributes")
                block["attributes"] = copy.deepcopy(dict(raw["attributes"]))
            output.append(block)
        if not any(block["text"].strip() for block in output):
            raise ParsingError("PDF conversion produced no readable content")
        return output

    def parse(self, source: Mapping[str, Any]) -> ParseOutput:
        if not isinstance(source, Mapping) or set(source) != SOURCE_KEYS:
            raise ParsingError("PDF source contract is invalid")
        if source["media_type"] != "application/pdf":
            raise ParsingError("Docling adapter accepts only application/pdf")
        data = source["data"]
        if type(data) is not bytes or len(data) > self._max_file_size:
            raise ParsingError("PDF source bytes are invalid")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if source["digest"] != digest:
            raise ParsingError("PDF source digest mismatch")
        name = self._resource_name(source["resource"])
        try:
            converted = self._backend.convert(
                data, name, max_file_size=self._max_file_size,
                max_num_pages=self._max_num_pages,
            )
        except Exception as exc:
            raise ParsingError("PDF conversion failed") from exc
        blocks = self._blocks(converted)
        located = sum("page" in block["source_span"] or "coordinates" in block["source_span"] for block in blocks)
        structural = sum(block["type"] != "paragraph" for block in blocks)
        ir = build_document_ir(
            source, self._capability, blocks,
            {"format": "pdf", "page_count": converted.page_count, "ocr_used": False,
             "configuration_digest": self._configuration_digest},
        )
        ir["parser"]["configuration_digest"] = self._configuration_digest
        return ParseOutput(
            ir,
            {
                "text_coverage": 1.0,
                "structural_completeness": min(1.0, (structural + 1) / len(blocks)),
                "source_span_coverage": located / len(blocks),
                "document_ir_valid": 1.0,
            },
        )


def create_docling_calibration_adapter(
    *,
    max_file_size: int,
    max_num_pages: int,
    max_blocks: int,
) -> DoclingPdfAdapter:
    """Create an explicitly unqualified adapter used only to measure a corpus."""

    backend = DoclingBackend()
    adapter = object.__new__(DoclingPdfAdapter)
    adapter._backend = backend
    adapter._max_file_size = max_file_size
    adapter._max_num_pages = max_num_pages
    adapter._max_blocks = max_blocks
    adapter._configuration_digest = configuration_digest(
        max_file_size, max_num_pages, max_blocks
    )
    capability = base_capability(
        "docling", backend.version, ["application/pdf"], [".pdf"],
        ["headings", "links", "tables", "code", "images", "captions", "footnotes", "source_coordinates"],
    )
    capability.update({
        "layout_aware": True,
        "table_extraction": "structured",
        "image_handling": True,
        "caption_handling": True,
        "source_coordinates": True,
        "ocr_capable": False,
        "max_file_bytes": max_file_size,
        "benchmark_result_refs": [],
        "benchmark_evidence_status": "calibration-only",
    })
    adapter._capability = capability
    return adapter
