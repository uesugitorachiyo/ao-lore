"""Independent public-fixture oracle for English OCR qualification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from ._strict_io import parse_strict_json, reject_symlink_ancestors


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MEDIA_TYPES = {"image/png", "application/pdf"}
_OUTCOMES = {"accepted", "rejected"}
_FAILURES = {"no_readable_content", "invalid_source", "encrypted_source", "resource_limit"}
_CONFIGURATION = {
    "language": "en", "dpi": 300, "attempts_per_candidate": 2,
    "maximum_pages": 100, "maximum_source_bytes": 52428800,
    "maximum_lines": 10000,
}


@dataclass(frozen=True)
class OcrLineAnnotation:
    line_id: str
    text: str
    polygon: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class OcrPageAnnotation:
    page_number: int
    width: int
    height: int
    lines: tuple[OcrLineAnnotation, ...]


@dataclass(frozen=True)
class OcrFixture:
    fixture_id: str
    file_name: str
    kind: str
    source_digest: str
    media_type: str
    expected_outcome: str
    expected_failure: str | None
    pages: tuple[OcrPageAnnotation, ...]


@dataclass(frozen=True)
class OcrFixtureCorpus:
    corpus_digest: str
    configuration_digest: str
    fixtures: tuple[OcrFixture, ...]


@dataclass(frozen=True)
class OcrAttemptMetrics:
    character_accuracy: Fraction
    word_accuracy: Fraction
    detection_precision: Fraction
    detection_recall: Fraction
    reading_order_pair_accuracy: Fraction
    mean_polygon_iou: Fraction
    page_coverage: Fraction
    hallucinated_lines: int
    expected_outcome_accuracy: Fraction
    semantic_digest: str


def ocr_oracle_implementation_digest() -> str:
    """Bind qualification to the exact independent oracle source bytes."""

    body = _read_regular_file(Path(__file__), 1024 * 1024, "OCR oracle implementation")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _read_regular_file(path: Path, maximum_bytes: int, label: str) -> bytes:
    reject_symlink_ancestors(path.parent, include_self=True)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValueError(f"{label} could not be opened") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum_bytes:
            raise ValueError(f"{label} differs")
        chunks, remaining = [], maximum_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk); remaining -= len(chunk)
        body = b"".join(chunks)
        after = os.fstat(descriptor)
        identity_before = (before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        if len(body) > maximum_bytes or identity_before != identity_after or len(body) != before.st_size:
            raise ValueError(f"{label} drifted")
        return body
    finally:
        os.close(descriptor)


def _exact_int(value: object, minimum: int, maximum: int, label: str) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} differs")
    return value


def _polygon(value: object, width: int, height: int) -> tuple[tuple[int, int], ...]:
    if type(value) is not list or len(value) != 4:
        raise ValueError("OCR annotation polygon differs")
    points = []
    for pair in value:
        if type(pair) is not list or len(pair) != 2:
            raise ValueError("OCR annotation polygon point differs")
        points.append((_exact_int(pair[0], 0, width, "x"), _exact_int(pair[1], 0, height, "y")))
    if len(set(points)) != 4:
        raise ValueError("OCR annotation polygon is degenerate")
    turns = []
    for index in range(4):
        first, second, third = points[index], points[(index + 1) % 4], points[(index + 2) % 4]
        turns.append((second[0] - first[0]) * (third[1] - second[1]) - (second[1] - first[1]) * (third[0] - second[0]))
    if any(turn <= 0 for turn in turns):
        raise ValueError("OCR annotation polygon orientation differs")
    return tuple(points)


def load_ocr_fixture_corpus(spec_path: str | Path) -> OcrFixtureCorpus:
    path = Path(os.path.abspath(os.fspath(spec_path)))
    body = _read_regular_file(path, 2 * 1024 * 1024, "OCR fixture specification")
    try:
        value = parse_strict_json(body, "OCR fixture specification")
    except Exception as exc:
        raise ValueError("OCR fixture specification is invalid") from exc
    if type(value) is not dict or set(value) != {"schema_version", "configuration", "fixtures"}:
        raise ValueError("OCR fixture corpus fields differ")
    if value["schema_version"] != "ao.lore.ocr-fixture-corpus.v0.1" or value["configuration"] != _CONFIGURATION:
        raise ValueError("OCR fixture corpus header differs")
    fixtures_value = value["fixtures"]
    if type(fixtures_value) is not list or len(fixtures_value) != 20:
        raise ValueError("OCR fixture count differs")
    fixtures: list[OcrFixture] = []
    ids: set[str] = set()
    names: set[str] = set()
    line_ids: set[str] = set()
    for item in fixtures_value:
        keys = {"id", "file", "kind", "sha256", "media_type", "expected_outcome", "expected_failure", "pages"}
        if type(item) is not dict or set(item) != keys:
            raise ValueError("OCR fixture fields differ")
        fixture_id, file_name = item["id"], item["file"]
        if type(fixture_id) is not str or not fixture_id or fixture_id in ids:
            raise ValueError("OCR fixture identity differs")
        if type(file_name) is not str or Path(file_name).name != file_name or file_name in names:
            raise ValueError("OCR fixture filename differs")
        digest = item["sha256"]
        media_type = item["media_type"]
        if type(digest) is not str or not _DIGEST.fullmatch(digest) or media_type not in _MEDIA_TYPES:
            raise ValueError("OCR fixture source binding differs")
        fixture_path = path.parent / file_name
        fixture_body = _read_regular_file(fixture_path, 52_428_800, "OCR fixture source")
        if "sha256:" + hashlib.sha256(fixture_body).hexdigest() != digest:
            raise ValueError("OCR fixture digest differs")
        outcome, failure = item["expected_outcome"], item["expected_failure"]
        if outcome not in _OUTCOMES or (outcome == "accepted") != (failure is None) or failure not in _FAILURES | {None}:
            raise ValueError("OCR fixture outcome differs")
        pages_value = item["pages"]
        if type(pages_value) is not list or len(pages_value) > 100:
            raise ValueError("OCR fixture pages differ")
        pages: list[OcrPageAnnotation] = []
        for page_number, page in enumerate(pages_value, 1):
            if type(page) is not dict or set(page) != {"page", "width", "height", "lines"} or page["page"] != page_number:
                raise ValueError("OCR fixture page fields differ")
            width = _exact_int(page["width"], 1, 25_000_000, "page width")
            height = _exact_int(page["height"], 1, 25_000_000, "page height")
            lines_value = page["lines"]
            if type(lines_value) is not list or len(lines_value) > 10_000:
                raise ValueError("OCR fixture lines differ")
            lines: list[OcrLineAnnotation] = []
            for line in lines_value:
                if type(line) is not dict or set(line) != {"id", "text", "polygon"}:
                    raise ValueError("OCR annotation line fields differ")
                line_id, text = line["id"], line["text"]
                if type(line_id) is not str or not line_id or type(text) is not str or not text or len(text) > 4096:
                    raise ValueError("OCR annotation line differs")
                if line_id in line_ids:
                    raise ValueError("OCR annotation line identity is duplicated")
                lines.append(OcrLineAnnotation(line_id, unicodedata.normalize("NFC", text), _polygon(line["polygon"], width, height)))
                line_ids.add(line_id)
            pages.append(OcrPageAnnotation(page_number, width, height, tuple(lines)))
        if outcome == "accepted" and not any(page.lines for page in pages):
            raise ValueError("accepted OCR fixture has no expected lines")
        fixtures.append(OcrFixture(fixture_id, file_name, item["kind"], digest, media_type, outcome, failure, tuple(pages)))
        ids.add(fixture_id); names.add(file_name)
    configuration_digest = _canonical_digest(value["configuration"])
    corpus_digest = _canonical_digest(value)
    return OcrFixtureCorpus(corpus_digest, configuration_digest, tuple(fixtures))


def _edit_distance(left: list[Any] | str, right: list[Any] | str) -> int:
    previous = list(range(len(right) + 1))
    for row, left_value in enumerate(left, 1):
        current = [row]
        for column, right_value in enumerate(right, 1):
            current.append(min(current[-1] + 1, previous[column] + 1, previous[column - 1] + (left_value != right_value)))
        previous = current
    return previous[-1]


def _accuracy(expected: list[Any] | str, actual: list[Any] | str) -> Fraction:
    denominator = max(1, len(expected), len(actual))
    return Fraction(max(0, denominator - _edit_distance(expected, actual)), denominator)


def _cross(a: tuple[Fraction, Fraction], b: tuple[Fraction, Fraction], p: tuple[Fraction, Fraction]) -> Fraction:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def _intersection(p: tuple[Fraction, Fraction], q: tuple[Fraction, Fraction], a: tuple[Fraction, Fraction], b: tuple[Fraction, Fraction]) -> tuple[Fraction, Fraction]:
    direction = (q[0] - p[0], q[1] - p[1])
    edge = (b[0] - a[0], b[1] - a[1])
    denominator = direction[0] * edge[1] - direction[1] * edge[0]
    if denominator == 0:
        return q
    numerator = (a[0] - p[0]) * edge[1] - (a[1] - p[1]) * edge[0]
    ratio = numerator / denominator
    return p[0] + ratio * direction[0], p[1] + ratio * direction[1]


def _area(points: list[tuple[Fraction, Fraction]]) -> Fraction:
    if len(points) < 3:
        return Fraction(0)
    return abs(sum(points[index][0] * points[(index + 1) % len(points)][1] - points[(index + 1) % len(points)][0] * points[index][1] for index in range(len(points)))) / 2


def _iou(left: tuple[tuple[int, int], ...], right: tuple[tuple[int, int], ...]) -> Fraction:
    subject = [(Fraction(x), Fraction(y)) for x, y in left]
    clip = [(Fraction(x), Fraction(y)) for x, y in right]
    output = subject
    for index, edge_start in enumerate(clip):
        edge_end = clip[(index + 1) % len(clip)]
        input_points, output = output, []
        if not input_points:
            break
        previous = input_points[-1]
        for current in input_points:
            current_inside = _cross(edge_start, edge_end, current) >= 0
            previous_inside = _cross(edge_start, edge_end, previous) >= 0
            if current_inside:
                if not previous_inside:
                    output.append(_intersection(previous, current, edge_start, edge_end))
                output.append(current)
            elif previous_inside:
                output.append(_intersection(previous, current, edge_start, edge_end))
            previous = current
    intersection = _area(output)
    union = _area(subject) + _area(clip) - intersection
    return intersection / union if union else Fraction(0)


def _actual_lines(document_ir: object, fixture: OcrFixture) -> list[tuple[str, int, tuple[tuple[int, int], ...]]]:
    if type(document_ir) is not dict or set(document_ir) != {"schema_version", "document_id", "source", "parser", "blocks", "metadata"}:
        raise ValueError("OCR candidate IR fields differ")
    if document_ir["schema_version"] != "ao.lore.document-ir.v0.1" or document_ir["document_id"] != fixture.source_digest:
        raise ValueError("OCR candidate IR identity differs")
    source = document_ir["source"]
    if type(source) is not dict or source != {"resource": fixture.file_name, "digest": fixture.source_digest, "media_type": fixture.media_type}:
        raise ValueError("OCR candidate provenance differs")
    parser = document_ir["parser"]
    metadata = document_ir["metadata"]
    if (
        type(parser) is not dict
        or set(parser) != {"parser_id", "parser_version", "configuration_digest"}
        or parser["parser_id"] != "paddle-ocr-english"
        or parser["parser_version"] != "0.1.0"
        or type(parser["configuration_digest"]) is not str
        or not _DIGEST.fullmatch(parser["configuration_digest"])
        or type(metadata) is not dict
        or metadata != {"format": "ocr", "page_count": len(fixture.pages), "authority_advanced": False}
    ):
        raise ValueError("OCR candidate metadata differs")
    blocks = document_ir["blocks"]
    if type(blocks) is not list or len(blocks) > 10_000:
        raise ValueError("OCR candidate blocks differ")
    actual = []
    seen_polygons: set[tuple[int, tuple[tuple[int, int], ...]]] = set()
    offset = 0
    for block_number, block in enumerate(blocks, 1):
        if type(block) is not dict or set(block) != {"id", "type", "text", "source_span", "location"} or block["type"] != "paragraph":
            raise ValueError("OCR candidate block differs")
        text = block["text"]
        span = block["source_span"]
        location = block["location"]
        if (
            block["id"] != f"ocr-{block_number:06d}"
            or type(text) is not str
            or not text.strip()
            or len(text) > 4096
            or any(ord(character) < 32 and character not in "\n\t" for character in text)
            or type(span) is not dict
            or set(span) != {"start", "end"}
            or span != {"start": offset, "end": offset + len(text)}
            or type(location) is not dict
            or set(location) != {"page", "polygon", "confidence_millionths"}
        ):
            raise ValueError("OCR candidate line differs")
        page = _exact_int(location["page"], 1, max(1, len(fixture.pages)), "candidate page")
        confidence = _exact_int(location["confidence_millionths"], 0, 1_000_000, "confidence")
        del confidence
        expected_page = fixture.pages[page - 1]
        polygon = _polygon(location["polygon"], expected_page.width, expected_page.height)
        identity = (page, polygon)
        if identity in seen_polygons:
            raise ValueError("OCR candidate detection is duplicated")
        seen_polygons.add(identity)
        actual.append((unicodedata.normalize("NFC", text), page, polygon))
        offset += len(text) + 1
    return actual


def score_accepted_attempt(fixture: OcrFixture, document_ir: object) -> OcrAttemptMetrics:
    if fixture.expected_outcome != "accepted":
        raise ValueError("accepted scorer received a rejection fixture")
    actual = _actual_lines(document_ir, fixture)
    expected = [(line.text, page.page_number, line.polygon, line.line_id) for page in fixture.pages for line in page.lines]
    available = set(range(len(actual)))
    matches: list[tuple[int, int]] = []
    for expected_index, (text, page, polygon, _) in enumerate(expected):
        candidates = [index for index in available if actual[index][1] == page]
        if not candidates:
            continue
        best = min(candidates, key=lambda index: (_edit_distance(text, actual[index][0]), -_iou(polygon, actual[index][2]), index))
        matches.append((expected_index, best)); available.remove(best)
    expected_text = "\n".join(item[0] for item in expected)
    actual_text = "\n".join(item[0] for item in actual)
    expected_words = expected_text.split()
    actual_words = actual_text.split()
    matched_expected = {left for left, _ in matches}
    matched_actual = {right for _, right in matches}
    expected_pairs = [(left, right) for left in range(len(expected)) for right in range(left + 1, len(expected)) if left in matched_expected and right in matched_expected]
    actual_position = {expected_index: actual_index for expected_index, actual_index in matches}
    correct_pairs = sum(actual_position[left] < actual_position[right] for left, right in expected_pairs)
    ious = [_iou(expected[left][2], actual[right][2]) for left, right in matches]
    expected_pages = {item[1] for item in expected}
    actual_pages = {actual[index][1] for index in matched_actual}
    semantic = {
        "text": [item[0] for item in actual],
        "pages": [item[1] for item in actual],
        "polygons": [[list(point) for point in item[2]] for item in actual],
    }
    return OcrAttemptMetrics(
        _accuracy(expected_text, actual_text),
        _accuracy(expected_words, actual_words),
        Fraction(len(matches), max(1, len(actual))),
        Fraction(len(matches), max(1, len(expected))),
        Fraction(correct_pairs, len(expected_pairs)) if expected_pairs else Fraction(1),
        sum(ious, Fraction(0)) / len(ious) if ious else Fraction(0),
        Fraction(len(expected_pages & actual_pages), max(1, len(expected_pages))),
        len(available), Fraction(1), _canonical_digest(semantic),
    )
