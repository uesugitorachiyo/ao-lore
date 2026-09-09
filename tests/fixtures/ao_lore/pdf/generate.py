#!/usr/bin/env python3
"""Generate the deterministic, public-safe PDF benchmark corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "fixture-spec.json"
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
KINDS = {
    "text", "multi_page", "two_column", "table", "unicode", "long",
    "empty", "truncated", "encrypted_stub", "image_only",
}
OUTCOMES = {"accepted", "rejected"}
FAILURES = {
    "conversion_failed", "no_readable_content", "encrypted_document", "invalid_pdf",
}
FIXTURE_METRICS = {
    "structural_fidelity", "text_fidelity", "source_location_fidelity",
}
METRICS = FIXTURE_METRICS | {
    "reliability", "deterministic_repeatability", "latency", "memory", "external_cost",
}
DOCUMENT_ID = b"414F4C4F5245504446434F525055533031"


def _literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_operand(value: str) -> str:
    if value.isascii():
        return f"({_literal(value)})"
    return "<FEFF" + value.encode("utf-16-be").hex().upper() + ">"


def _object(number: int, body: bytes) -> bytes:
    return f"{number} 0 obj\n".encode("ascii") + body + b"\nendobj\n"


def _stream(data: bytes, extra: str = "") -> bytes:
    suffix = f" {extra}" if extra else ""
    return f"<< /Length {len(data)}{suffix} >>\nstream\n".encode("ascii") + data + b"\nendstream"


def _content(lines: Sequence[tuple[float, float, float, str]], marker: str | None = None) -> bytes:
    commands = [f"% AO-LORE-LAYOUT {marker}"] if marker else []
    commands.append("BT")
    for size, x, y, value in lines:
        commands.extend([f"/F1 {size:g} Tf", f"1 0 0 1 {x:g} {y:g} Tm", f"{_text_operand(value)} Tj"])
    commands.append("ET")
    return ("\n".join(commands) + "\n").encode("ascii")


def _assemble(objects: Sequence[bytes], *, trailer_extra: str = "") -> bytes:
    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(_object(number, value))
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    body.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        body.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    extra = f" {trailer_extra}" if trailer_extra else ""
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R{extra} "
        f"/ID [<{DOCUMENT_ID.decode()}> <{DOCUMENT_ID.decode()}>] >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(body)


def _pages_pdf(page_streams: Sequence[bytes], *, annotations: Sequence[tuple[int, str]] = (),
               image_only: bool = False) -> bytes:
    page_count = len(page_streams)
    page_numbers = list(range(3, 3 + page_count))
    font_number = 3 + page_count
    stream_numbers = list(range(font_number + 1, font_number + 1 + page_count))
    annotation_start = stream_numbers[-1] + 1 if stream_numbers else font_number + 1
    annotation_numbers = {
        page_index: annotation_start + index for index, (page_index, _) in enumerate(annotations)
    }
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{number} 0 R' for number in page_numbers)}] "
            f"/Count {page_count} >>"
        ).encode("ascii"),
    ]
    for page_index, (page_number, stream_number) in enumerate(zip(page_numbers, stream_numbers)):
        if image_only:
            resources = f"/XObject << /Im1 {font_number} 0 R >>"
        else:
            resources = f"/Font << /F1 {font_number} 0 R >>"
        annot = f" /Annots [{annotation_numbers[page_index]} 0 R]" if page_index in annotation_numbers else ""
        objects.append((
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << {resources} >> /Contents {stream_number} 0 R{annot} >>"
        ).encode("ascii"))
    if image_only:
        objects.append(_stream(b"\x00\x66\xcc", "/Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceRGB /BitsPerComponent 8"))
    else:
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.extend(_stream(stream) for stream in page_streams)
    for _, link in annotations:
        objects.append((
            "<< /Type /Annot /Subtype /Link /Rect [72 600 360 625] /Border [0 0 0] "
            f"/A << /S /URI /URI ({_literal(link)}) >> >>"
        ).encode("ascii"))
    return _assemble(objects)


def _validate_fixture(fixture: Mapping[str, Any]) -> None:
    if not isinstance(fixture, Mapping) or set(fixture) != FIXTURE_KEYS:
        raise ValueError("fixture keys differ")
    for field in ("id", "file", "sha256", "kind", "title", "expected_outcome"):
        if not isinstance(fixture[field], str):
            raise ValueError(f"fixture {field} must be a string")
    path = PurePosixPath(fixture["file"])
    if path.name != fixture["file"] or path.suffix.lower() != ".pdf":
        raise ValueError("fixture file must be a local PDF filename")
    if fixture["kind"] not in KINDS:
        raise ValueError("fixture kind is unsupported")
    for field in ("lines", "expected_text", "required_block_types", "applicable_metrics"):
        value = fixture[field]
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"fixture {field} must be a string list")
    link = fixture["link"]
    if link is not None and (not isinstance(link, str) or not link.isascii()):
        raise ValueError("fixture link must be null or ASCII")
    if fixture["expected_outcome"] not in OUTCOMES:
        raise ValueError("fixture outcome is unsupported")
    failure = fixture["expected_failure"]
    if fixture["expected_outcome"] == "accepted":
        if failure is not None or not fixture["expected_text"] or not fixture["required_block_types"]:
            raise ValueError("accepted fixture expectations are invalid")
    elif (
        not isinstance(failure, str) or failure not in FAILURES
        or fixture["expected_text"] or fixture["required_block_types"]
    ):
        raise ValueError("rejected fixture expectations are invalid")
    applicable = fixture["applicable_metrics"]
    if not applicable or len(applicable) != len(set(applicable)) or any(
        metric not in FIXTURE_METRICS for metric in applicable
    ):
        raise ValueError("fixture metric applicability is invalid")


def validate_corpus(corpus: Mapping[str, Any]) -> None:
    if not isinstance(corpus, Mapping) or set(corpus) != CORPUS_KEYS:
        raise ValueError("fixture corpus keys differ")
    if corpus["schema_version"] != "ao.lore.pdf-fixture-corpus.v0.1":
        raise ValueError("fixture corpus schema version differs")
    configuration = corpus["configuration"]
    if not isinstance(configuration, Mapping) or set(configuration) != CONFIG_KEYS:
        raise ValueError("fixture corpus configuration keys differ")
    for field in ("max_file_size", "max_num_pages", "max_blocks"):
        value = configuration[field]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"fixture corpus {field} must be a positive integer")
    for field in ("latency_max_seconds", "memory_max_bytes"):
        value = configuration[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"fixture corpus {field} must be positive numeric")
    minimums = configuration["minimum_scores"]
    if not isinstance(minimums, Mapping) or any(
        not isinstance(key, str) or key not in METRICS for key in minimums
    ):
        raise ValueError("fixture corpus minimum scores are invalid")
    for value in minimums.values():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError("fixture corpus minimum score is invalid")
    metrics = corpus["metrics"]
    if (
        not isinstance(metrics, list) or any(not isinstance(metric, str) for metric in metrics)
        or len(metrics) != len(METRICS) or set(metrics) != METRICS
    ):
        raise ValueError("fixture corpus metrics differ")
    fixtures = corpus["fixtures"]
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("fixture corpus fixtures must be a non-empty list")
    fixture_ids: set[str] = set()
    filenames: set[str] = set()
    for fixture in fixtures:
        _validate_fixture(fixture)
        if not fixture["id"] or fixture["id"] in fixture_ids:
            raise ValueError("fixture identity is invalid")
        if fixture["file"] in filenames:
            raise ValueError("fixture filename is duplicated")
        if (
            len(fixture["sha256"]) != 71 or not fixture["sha256"].startswith("sha256:")
            or any(character not in "0123456789abcdef" for character in fixture["sha256"][7:])
        ):
            raise ValueError("fixture digest is invalid")
        fixture_ids.add(fixture["id"])
        filenames.add(fixture["file"])
    if not any(fixture["expected_outcome"] == "accepted" for fixture in fixtures):
        raise ValueError("fixture corpus has no accepted fixtures")


def generate_fixture_bytes(fixture: Mapping[str, Any]) -> bytes:
    _validate_fixture(fixture)
    kind = fixture["kind"]
    title = fixture["title"]
    lines = fixture["lines"]
    link = fixture["link"]
    if kind in {"text", "table", "unicode"}:
        rows = [(18.0, 72.0, 740.0, title)]
        rows.extend((11.0, 72.0, 700.0 - index * 28.0, line) for index, line in enumerate(lines))
        marker = "table" if kind == "table" else None
        stream = _content(rows, marker)
        if kind == "table" and fixture["id"] == "complex-table":
            stream = b"% AO-LORE-TABLE complex\n" + stream
        return _pages_pdf([stream], annotations=[(0, link)] if link else [])
    if kind == "multi_page":
        streams = [
            _content([(18.0, 72.0, 740.0, f"{title} {index + 1}"),
                      (11.0, 72.0, 700.0, line)], "multi-page")
            for index, line in enumerate(lines)
        ]
        return _pages_pdf(streams)
    if kind == "two_column":
        split = (len(lines) + 1) // 2
        rows = [(18.0, 72.0, 740.0, title)]
        rows.extend((11.0, 72.0, 700.0 - index * 28.0, line) for index, line in enumerate(lines[:split]))
        rows.extend((11.0, 330.0, 700.0 - index * 28.0, line) for index, line in enumerate(lines[split:]))
        return _pages_pdf([_content(rows, "two-column")])
    if kind == "long":
        streams = []
        for page_index in range(8):
            page_lines = [line for index, line in enumerate(lines) if index % 8 == page_index]
            rows = [(18.0, 72.0, 740.0, f"{title} {page_index + 1}")]
            rows.extend((10.0, 72.0, 705.0 - index * 24.0, line) for index, line in enumerate(page_lines))
            streams.append(_content(rows, "long-boundary"))
        return _pages_pdf(streams)
    if kind == "empty":
        return _pages_pdf([b"% AO-LORE-LAYOUT empty\n"])
    if kind == "image_only":
        return _pages_pdf([b"q\n144 0 0 144 72 576 cm\n/Im1 Do\nQ\n"], image_only=True)
    if kind == "encrypted_stub":
        # Deliberately minimal: declare encryption without credentials or encrypted payload.
        return _assemble([
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            _stream(b"% AO-LORE-LAYOUT encrypted-stub\n"),
            b"<< /Filter /Standard /V 1 /R 2 /O <00> /U <00> /P -4 >>",
        ], trailer_extra="/Encrypt 6 0 R")
    if kind == "truncated":
        valid = _pages_pdf([_content([(18.0, 72.0, 740.0, title)])])
        return valid[: valid.index(b"xref\n")]
    raise AssertionError("validated PDF fixture kind is unhandled")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    corpus = json.loads(SPEC.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    mismatches = []
    for fixture in corpus["fixtures"]:
        data = generate_fixture_bytes(fixture)
        target = ROOT / fixture["file"]
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if digest != fixture["sha256"]:
            mismatches.append(f"{fixture['id']}: digest")
        if args.write:
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError("fixture target must be a regular non-link file")
            target.write_bytes(data)
        elif not target.is_file() or target.read_bytes() != data:
            mismatches.append(f"{fixture['id']}: bytes")
    if mismatches:
        print("; ".join(mismatches))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
