"""Deterministic canonical document IR from detached OCR observations."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ._strict_io import ContractError
from .benchmark import canonical_digest
from .ocr_contracts import validate_worker_result


class OcrIrError(ValueError):
    """Raised when detached OCR observations cannot form canonical IR."""


OCR_PARSER_ID = "paddle-ocr-english"
OCR_PARSER_VERSION = "0.1.0"
MAXIMUM_OCR_IR_BLOCKS = 10000
OCR_IR_CONFIGURATION_DIGEST = canonical_digest({
    "canonicalizer": "ao-lore-ocr-ir-v0.2",
    "line_overlap_ratio": "one-half-shorter-height",
    "line_fragment_merge": "strict-positive-horizontal-overlap",
    "column_rule": "positive-horizontal-overlap-components",
    "normalization": "unicode-nfc-lf",
    "maximum_blocks": MAXIMUM_OCR_IR_BLOCKS,
})

_RESOURCE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MEDIA_TYPES = {"application/pdf", "image/png", "image/jpeg"}


@dataclass(frozen=True)
class _Detection:
    page: int
    original_index: int
    text: str
    confidence: int
    polygon: tuple[tuple[int, int], ...]
    left: int
    top: int
    right: int
    bottom: int


def _source(value: object) -> dict[str, str]:
    if type(value) is not dict or set(value) != {"resource", "digest", "media_type"}:
        raise OcrIrError("OCR source provenance is invalid")
    resource = value["resource"]
    digest = value["digest"]
    media_type = value["media_type"]
    if type(resource) is not str or not _RESOURCE.fullmatch(resource):
        raise OcrIrError("OCR source resource is not an opaque identifier")
    if type(digest) is not str or not _DIGEST.fullmatch(digest):
        raise OcrIrError("OCR source digest is invalid")
    if type(media_type) is not str or media_type not in _MEDIA_TYPES:
        raise OcrIrError("OCR source media type is invalid")
    return {"resource": resource, "digest": digest, "media_type": media_type}


def _cross(first: tuple[int, int], second: tuple[int, int], third: tuple[int, int]) -> int:
    return (second[0] - first[0]) * (third[1] - second[1]) - (second[1] - first[1]) * (third[0] - second[0])


def _polygon(value: list[list[int]]) -> tuple[tuple[int, int], ...]:
    points = tuple((point[0], point[1]) for point in value)
    if len(set(points)) != 4:
        raise OcrIrError("OCR polygon has duplicate points")
    start = min(range(4), key=lambda index: (points[index][1], points[index][0], index))
    points = points[start:] + points[:start]
    turns = tuple(_cross(points[index], points[(index + 1) % 4], points[(index + 2) % 4]) for index in range(4))
    if any(turn <= 0 for turn in turns):
        raise OcrIrError("OCR polygon must be a strict clockwise quadrilateral")
    area_twice = sum(
        points[index][0] * points[(index + 1) % 4][1]
        - points[(index + 1) % 4][0] * points[index][1]
        for index in range(4)
    )
    if area_twice <= 0:
        raise OcrIrError("OCR polygon has invalid area")
    return points


def _text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if not normalized.strip() or len(normalized) > 4096:
        raise OcrIrError("OCR text is empty or excessive")
    if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
        raise OcrIrError("OCR text contains a control character")
    return normalized


def _detached(result: object) -> tuple[list[_Detection], int]:
    try:
        checked = validate_worker_result(result)
    except ContractError as exc:
        raise OcrIrError("OCR worker result is invalid") from exc
    detections: list[_Detection] = []
    seen: set[tuple[int, tuple[tuple[int, int], ...]]] = set()
    pages = checked["pages"]
    if sum(len(page["detections"]) for page in pages) > MAXIMUM_OCR_IR_BLOCKS:
        raise OcrIrError("OCR result contains excessive detections")
    for page_number, page in enumerate(pages, 1):
        for item in page["detections"]:
            polygon = _polygon(item["polygon"])
            identity = (page_number, polygon)
            if identity in seen:
                raise OcrIrError("OCR result contains a duplicate detection")
            seen.add(identity)
            xs = tuple(point[0] for point in polygon)
            ys = tuple(point[1] for point in polygon)
            detections.append(_Detection(
                page=page_number,
                original_index=item["index"],
                text=_text(item["text"]),
                confidence=item["confidence_millionths"],
                polygon=polygon,
                left=min(xs), top=min(ys), right=max(xs), bottom=max(ys),
            ))
    if not detections:
        raise OcrIrError("OCR result contains no readable content")
    if not any(any(character.isalnum() for character in item.text) for item in detections):
        raise OcrIrError("OCR result contains no readable content")
    return detections, len(pages)


def _same_line(left: _Detection, right: _Detection) -> bool:
    overlap = min(left.bottom, right.bottom) - max(left.top, right.top)
    shorter = min(left.bottom - left.top, right.bottom - right.top)
    return overlap > 0 and overlap * 2 >= shorter


def _column_components(items: list[_Detection]) -> list[list[_Detection]]:
    pending = sorted(items, key=lambda item: (item.left, item.right, item.top, item.polygon, item.original_index))
    components: list[list[_Detection]] = []
    current: list[_Detection] = []
    right = -1
    for item in pending:
        if current and item.left >= right:
            components.append(current)
            current = []
            right = -1
        current.append(item)
        right = max(right, item.right)
    if current:
        components.append(current)
    return components


def _column_order(items: list[_Detection]) -> list[_Detection]:
    pending = sorted(items, key=lambda item: (item.top, item.left, item.polygon, item.original_index))
    lines: list[list[_Detection]] = []
    for item in pending:
        if not lines or not _same_line(item, lines[-1][0]):
            lines.append([item])
        else:
            lines[-1].append(item)
    lines.sort(key=lambda line: (
        min(item.top for item in line), min(item.left for item in line),
        min(item.polygon for item in line), min(item.original_index for item in line),
    ))
    ordered: list[_Detection] = []
    for line in lines:
        fragments = sorted(line, key=lambda item: (item.left, item.top, item.polygon, item.original_index))
        merged: list[_Detection] = []
        for item in fragments:
            if not merged or item.left >= merged[-1].right:
                merged.append(item)
                continue
            prior = merged[-1]
            left, top = min(prior.left, item.left), min(prior.top, item.top)
            right, bottom = max(prior.right, item.right), max(prior.bottom, item.bottom)
            merged[-1] = _Detection(
                page=prior.page,
                original_index=min(prior.original_index, item.original_index),
                text=prior.text.rstrip() + " " + item.text.lstrip(),
                confidence=min(prior.confidence, item.confidence),
                polygon=((left, top), (right, top), (right, bottom), (left, bottom)),
                left=left, top=top, right=right, bottom=bottom,
            )
        ordered.extend(merged)
    return ordered


def _reading_order(detections: list[_Detection]) -> list[_Detection]:
    ordered: list[_Detection] = []
    for page_number in sorted({item.page for item in detections}):
        page = [item for item in detections if item.page == page_number]
        columns = _column_components(page)
        columns.sort(key=lambda column: (
            min(item.left for item in column), min(item.top for item in column),
            min(item.polygon for item in column), min(item.original_index for item in column),
        ))
        for column in columns:
            ordered.extend(_column_order(column))
    return ordered


def canonicalize_ocr_result(result: object, source: object) -> dict[str, Any]:
    """Validate detached observations and emit path-free canonical document IR."""

    provenance = _source(source)
    detections, page_count = _detached(result)
    blocks: list[dict[str, Any]] = []
    offset = 0
    for index, item in enumerate(_reading_order(detections), 1):
        end = offset + len(item.text)
        blocks.append({
            "id": f"ocr-{index:06d}",
            "type": "paragraph",
            "text": item.text,
            "source_span": {"start": offset, "end": end},
            "location": {
                "page": item.page,
                "polygon": [list(point) for point in item.polygon],
                "confidence_millionths": item.confidence,
            },
        })
        offset = end + 1
    return {
        "schema_version": "ao.lore.document-ir.v0.1",
        "document_id": provenance["digest"],
        "source": provenance,
        "parser": {
            "parser_id": OCR_PARSER_ID,
            "parser_version": OCR_PARSER_VERSION,
            "configuration_digest": OCR_IR_CONFIGURATION_DIGEST,
        },
        "blocks": blocks,
        "metadata": {"format": "ocr", "page_count": page_count, "authority_advanced": False},
    }
