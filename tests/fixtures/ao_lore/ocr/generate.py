#!/usr/bin/env python3
"""Generate AO Lore's deterministic public English OCR fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import zlib
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "fixture-spec.json"
KINDS = {
    "clean", "degraded", "faint", "uneven", "rotated-90", "rotated-180",
    "rotated-270", "skewed", "one-column", "two-column", "dense",
    "punctuation", "blank", "nontext", "mixed-pdf", "malformed-pdf",
    "encrypted-pdf", "oversized", "decompression-bomb", "excessive-pages",
}
FAILURES = {"no_readable_content", "invalid_source", "encrypted_source", "resource_limit"}
FIXTURE_KEYS = {
    "id", "file", "kind", "sha256", "media_type", "expected_outcome",
    "expected_failure", "pages",
}
PAGE_KEYS = {"page", "width", "height", "lines"}
LINE_KEYS = {"id", "text", "polygon"}
CONFIGURATION = {
    "language": "en", "dpi": 300, "attempts_per_candidate": 2,
    "maximum_pages": 100, "maximum_source_bytes": 52428800,
    "maximum_lines": 10000,
}
ORIGINS = {
    "clean-1": (40, 40), "clean-2": (40, 130), "degraded-1": (40, 70),
    "faint-1": (40, 90), "uneven-1": (40, 90), "skewed-1": (40, 80),
    "one-1": (40, 40), "one-2": (40, 140), "one-3": (40, 240),
    "left-1": (30, 40), "left-2": (30, 150), "right-1": (500, 40),
    "right-2": (500, 150), "dense-1": (30, 30), "dense-2": (30, 90),
    "dense-3": (30, 150), "dense-4": (30, 210), "dense-5": (30, 270),
    "punctuation-1": (30, 80),
}

# Public-domain-style 5x7 bitmap glyphs encoded row-wise. The generated corpus
# deliberately uses only this closed alphabet so fixture bytes do not depend on
# host fonts, locale, rendering libraries, or antialiasing versions.
FONT = {
    "A":("01110","10001","10001","11111","10001","10001","10001"),"B":("11110","10001","10001","11110","10001","10001","11110"),
    "C":("01111","10000","10000","10000","10000","10000","01111"),"D":("11110","10001","10001","10001","10001","10001","11110"),
    "E":("11111","10000","10000","11110","10000","10000","11111"),"F":("11111","10000","10000","11110","10000","10000","10000"),
    "G":("01111","10000","10000","10111","10001","10001","01111"),"H":("10001","10001","10001","11111","10001","10001","10001"),
    "I":("11111","00100","00100","00100","00100","00100","11111"),"J":("00111","00010","00010","00010","10010","10010","01100"),
    "K":("10001","10010","10100","11000","10100","10010","10001"),"L":("10000","10000","10000","10000","10000","10000","11111"),
    "M":("10001","11011","10101","10101","10001","10001","10001"),"N":("10001","11001","10101","10011","10001","10001","10001"),
    "O":("01110","10001","10001","10001","10001","10001","01110"),"P":("11110","10001","10001","11110","10000","10000","10000"),
    "Q":("01110","10001","10001","10001","10101","10010","01101"),"R":("11110","10001","10001","11110","10100","10010","10001"),
    "S":("01111","10000","10000","01110","00001","00001","11110"),"T":("11111","00100","00100","00100","00100","00100","00100"),
    "U":("10001","10001","10001","10001","10001","10001","01110"),"V":("10001","10001","10001","10001","10001","01010","00100"),
    "W":("10001","10001","10001","10101","10101","10101","01010"),"X":("10001","10001","01010","00100","01010","10001","10001"),
    "Y":("10001","10001","01010","00100","00100","00100","00100"),"Z":("11111","00001","00010","00100","01000","10000","11111"),
    "0":("01110","10001","10011","10101","11001","10001","01110"),"1":("00100","01100","00100","00100","00100","00100","01110"),
    "2":("01110","10001","00001","00010","00100","01000","11111"),"3":("11110","00001","00001","01110","00001","00001","11110"),
    "4":("00010","00110","01010","10010","11111","00010","00010"),"5":("11111","10000","10000","11110","00001","00001","11110"),
    "6":("01110","10000","10000","11110","10001","10001","01110"),"7":("11111","00001","00010","00100","01000","01000","01000"),
    "8":("01110","10001","10001","01110","10001","10001","01110"),"9":("01110","10001","10001","01111","00001","00001","01110"),
    " ":("00000",)*7,".":("00000","00000","00000","00000","00000","00110","00110"),",":("00000","00000","00000","00000","00110","00110","00100"),
    "%":("11001","11010","00100","01000","10110","00110","00000"),"-":("00000","00000","00000","11111","00000","00000","00000"),
}


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def _png(width: int, height: int, pixels: bytearray) -> bytes:
    raw = b"".join(
        b"\0" + b"".join(bytes((value, value, value)) for value in pixels[row * width:(row + 1) * width])
        for row in range(height)
    )
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + _chunk(b"IDAT", zlib.compress(raw, 9)) + _chunk(b"IEND", b"")


def _draw(pixels: bytearray, width: int, height: int, text: str, x: int, y: int, scale: int, ink: int, rotation: int = 0) -> None:
    for column, character in enumerate(text):
        glyph = FONT[character]
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px = x + (column * 6 + gx) * scale + dx
                        py = y + gy * scale + dy
                        if rotation == 90:
                            px, py = width - 1 - py, px
                        elif rotation == 180:
                            px, py = width - 1 - px, height - 1 - py
                        elif rotation == 270:
                            px, py = py, height - 1 - px
                        if 0 <= px < width and 0 <= py < height:
                            pixels[py * width + px] = ink


def _line_parameters(fixture: dict, line: dict) -> tuple[int, int, int, int]:
    rotation = {"rotated-90": 90, "rotated-180": 180, "rotated-270": 270}.get(fixture["kind"], 0)
    glyph_width = len(line["text"]) * 6 - 1
    page = fixture["pages"][0]
    if rotation == 90:
        return 40, 39, min(6, (page["height"] - 50) // glyph_width), rotation
    if rotation in {180, 270}:
        available = page["width"] - 59 if rotation == 180 else page["height"] - 59
        return 49, 39, min(6, available // glyph_width), rotation
    if fixture["kind"] == "skewed":
        return 40, 80, min(6, (page["width"] - 55) // glyph_width), 0
    try:
        x, y = ORIGINS[line["id"]]
    except KeyError as exc:
        raise ValueError("OCR fixture render origin differs") from exc
    default = 4 if fixture["kind"] == "dense" else 6
    maximum = max(1, (page["width"] - x - 10) // (len(line["text"]) * 6 - 1))
    return x, y, min(default, maximum), 0


def _expected_polygon(fixture: dict, line: dict) -> list[list[int]]:
    x, y, scale, rotation = _line_parameters(fixture, line)
    text_width = (len(line["text"]) * 6 - 1) * scale
    text_height = 7 * scale
    width = fixture["pages"][0]["width"]
    height = fixture["pages"][0]["height"]
    if rotation == 90:
        raw = [[width - y - text_height, x], [width - y, x], [width - y, x + text_width], [width - y - text_height, x + text_width]]
        return [[max(0, raw[0][0]-scale), max(0, raw[0][1]-scale)], [min(width, raw[1][0]+scale), max(0, raw[1][1]-scale)], [min(width, raw[2][0]+scale), min(height, raw[2][1]+2*scale)], [max(0, raw[3][0]-scale), min(height, raw[3][1]+2*scale)]]
    if rotation == 180:
        raw = [[width - x - text_width, height - y - text_height], [width - x, height - y - text_height], [width - x, height - y], [width - x - text_width, height - y]]
        return [[max(0, raw[0][0]-scale), max(0, raw[0][1]-scale)], [min(width, raw[1][0]+scale), max(0, raw[1][1]-scale)], [min(width, raw[2][0]+scale), min(height, raw[2][1]+2*scale)], [max(0, raw[3][0]-scale), min(height, raw[3][1]+2*scale)]]
    if rotation == 270:
        raw = [[y, height - x - text_width], [y + text_height, height - x - text_width], [y + text_height, height - x], [y, height - x]]
        return [[max(0, raw[0][0]-scale), max(0, raw[0][1]-scale)], [min(width, raw[1][0]+scale), max(0, raw[1][1]-scale)], [min(width, raw[2][0]+scale), min(height, raw[2][1]+2*scale)], [max(0, raw[3][0]-scale), min(height, raw[3][1]+2*scale)]]
    if fixture["kind"] == "skewed":
        top_shift = max(0, (y - 60) // 11)
        bottom_shift = max(0, (y + text_height - 1 - 60) // 11)
        return [[max(0,x + top_shift-scale), max(0,y-scale)], [min(width,x + text_width + top_shift+scale), max(0,y-scale)], [min(width,x + text_width + bottom_shift+scale), min(height,y + text_height+2*scale)], [max(0,x + bottom_shift-scale), min(height,y + text_height+2*scale)]]
    return [[max(0,x-scale), max(0,y-scale)], [min(width,x+text_width+scale), max(0,y-scale)], [min(width,x+text_width+scale), min(height,y+text_height+2*scale)], [max(0,x-scale), min(height,y+text_height+2*scale)]]


def _image(fixture: dict) -> bytes:
    page = fixture["pages"][0]
    width, height = page["width"], page["height"]
    pixels = bytearray([255]) * (width * height)
    if fixture["kind"] == "nontext":
        for y in range(50, 250):
            for x in range(100, 500):
                if (x - 300) ** 2 + (y - 150) ** 2 < 10000:
                    pixels[y * width + x] = 80
        return _png(width, height, pixels)
    if fixture["kind"] == "uneven":
        for y in range(height):
            for x in range(width):
                pixels[y * width + x] = 220 + (x * 35 // width)
    ink = 170 if fixture["kind"] == "faint" else 0
    rotation = {"rotated-90": 90, "rotated-180": 180, "rotated-270": 270}.get(fixture["kind"], 0)
    for line in page["lines"]:
        x, y, scale, rotation = _line_parameters(fixture, line)
        _draw(pixels, width, height, line["text"], x, y, scale, ink, rotation)
    if fixture["kind"] == "skewed":
        shifted = bytearray([255]) * len(pixels)
        for py in range(height):
            shift = max(0, (py - 60) // 11)
            for px in range(width - shift):
                shifted[py * width + px + shift] = pixels[py * width + px]
        pixels = shifted
    if fixture["kind"] == "degraded":
        for index in range(0, len(pixels), 37):
            pixels[index] = min(255, pixels[index] + 45)
    return _png(width, height, pixels)


def _assemble_pdf(objects: list[bytes]) -> bytes:
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode("ascii"))
    body.extend(b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]))
    body.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(body)


def _pdf(fixture: dict) -> bytes:
    if fixture["kind"] == "malformed-pdf":
        return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog"
    if fixture["kind"] == "encrypted-pdf":
        return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R /Encrypt 2 0 R >>\n%%EOF\n"
    if fixture["kind"] == "excessive-pages":
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            ("<< /Type /Pages /Count 101 /Kids [" + " ".join(f"{index} 0 R" for index in range(3, 104)) + "] >>").encode("ascii"),
        ]
        objects.extend(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 300] /Resources << >> >>" for _ in range(101))
        return _assemble_pdf(objects)
    if fixture["kind"] == "mixed-pdf":
        first = fixture["pages"][0]["lines"][0]["text"]
        second = fixture["pages"][1]["lines"][0]["text"]
        pixels = bytearray([255]) * (600 * 300)
        second_scale = min(6, 550 // (len(second) * 6 - 1))
        _draw(pixels, 600, 300, second, 40, 80, second_scale, 0)
        compressed = zlib.compress(bytes(pixels), 9)
        content_one = f"BT\n/F1 10 Tf\n1 0 0 1 10 45 Tm\n({first}) Tj\nET\n".encode("ascii")
        content_two = b"q\n144 0 0 72 0 0 cm\n/Im1 Do\nQ\n"
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 2 /Kids [3 0 R 4 0 R] >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 144 72] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 144 72] /Resources << /XObject << /Im1 7 0 R >> >> /Contents 8 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
            f"<< /Length {len(content_one)} >>\nstream\n".encode("ascii") + content_one + b"endstream",
            (f"<< /Type /XObject /Subtype /Image /Width 600 /Height 300 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length {len(compressed)} >>\nstream\n".encode("ascii") + compressed + b"\nendstream"),
            f"<< /Length {len(content_two)} >>\nstream\n".encode("ascii") + content_two + b"endstream",
        ]
        return _assemble_pdf(objects)
    streams = []
    for page in fixture["pages"]:
        commands = ["BT", "/F1 28 Tf"]
        for index, line in enumerate(page["lines"]):
            commands.extend([f"1 0 0 1 40 {220 - index * 70} Tm", f"({line['text']}) Tj"])
        commands.append("ET")
        streams.append(("\n".join(commands) + "\n").encode("ascii"))
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{3 + index} 0 R" for index in range(len(streams)))
    objects.append(f"<< /Type /Pages /Count {len(streams)} /Kids [{kids}] >>".encode("ascii"))
    font_number = 3 + len(streams)
    for index in range(len(streams)):
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 300] /Resources << /Font << /F1 {font_number} 0 R >> >> /Contents {font_number + 1 + index} 0 R >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.extend(f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream" for stream in streams)
    return _assemble_pdf(objects)


def validate_corpus(corpus: object) -> None:
    if type(corpus) is not dict or set(corpus) != {"schema_version", "configuration", "fixtures"}:
        raise ValueError("OCR fixture corpus fields differ")
    if corpus["schema_version"] != "ao.lore.ocr-fixture-corpus.v0.1" or corpus["configuration"] != CONFIGURATION:
        raise ValueError("OCR fixture corpus header differs")
    fixtures = corpus["fixtures"]
    if type(fixtures) is not list or len(fixtures) != 20:
        raise ValueError("OCR fixture count differs")
    ids, files, kinds = set(), set(), set()
    for fixture in fixtures:
        if type(fixture) is not dict or set(fixture) != FIXTURE_KEYS:
            raise ValueError("OCR fixture fields differ")
        if fixture["kind"] not in KINDS or fixture["id"] in ids or fixture["file"] in files:
            raise ValueError("OCR fixture identity differs")
        path = PurePosixPath(fixture["file"])
        if path.name != fixture["file"] or path.suffix not in {".png", ".pdf"}:
            raise ValueError("OCR fixture path differs")
        if (path.suffix == ".pdf") != (fixture["media_type"] == "application/pdf") or fixture["media_type"] not in {"application/pdf", "image/png"}:
            raise ValueError("OCR fixture media type differs")
        digest = fixture["sha256"]
        if type(digest) is not str or len(digest) != 71 or not digest.startswith("sha256:"):
            raise ValueError("OCR fixture digest differs")
        outcome = fixture["expected_outcome"]
        failure = fixture["expected_failure"]
        if outcome not in {"accepted", "rejected"} or (outcome == "accepted") != (failure is None):
            raise ValueError("OCR fixture outcome differs")
        if failure is not None and failure not in FAILURES:
            raise ValueError("OCR fixture failure differs")
        if type(fixture["pages"]) is not list:
            raise ValueError("OCR fixture pages differ")
        line_ids = set()
        for expected_page, page in enumerate(fixture["pages"], 1):
            if type(page) is not dict or set(page) != PAGE_KEYS or page["page"] != expected_page:
                raise ValueError("OCR fixture page differs")
            if type(page["width"]) is not int or type(page["height"]) is not int or not 1 <= page["width"] <= 25000000 or not 1 <= page["height"] <= 25000000:
                raise ValueError("OCR fixture dimensions differ")
            if type(page["lines"]) is not list:
                raise ValueError("OCR fixture lines differ")
            for line in page["lines"]:
                if type(line) is not dict or set(line) != LINE_KEYS or type(line["text"]) is not str or not line["text"]:
                    raise ValueError("OCR fixture line differs")
                if type(line["id"]) is not str or not line["id"] or line["id"] in line_ids:
                    raise ValueError("OCR fixture line identity differs")
                if type(line["polygon"]) is not list or len(line["polygon"]) != 4 or any(type(pair) is not list or len(pair) != 2 or any(type(coordinate) is not int for coordinate in pair) for pair in line["polygon"]):
                    raise ValueError("OCR fixture polygon differs")
                line_ids.add(line["id"])
        ids.add(fixture["id"]); files.add(fixture["file"]); kinds.add(fixture["kind"])
    if kinds != KINDS:
        raise ValueError("OCR fixture kind matrix differs")


def generate_fixture_bytes(fixture: dict) -> bytes:
    if fixture["media_type"] == "application/pdf":
        return _pdf(fixture)
    if fixture["kind"] == "oversized":
        return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 25000001, 1, 8, 2, 0, 0, 0)) + _chunk(b"IEND", b"")
    if fixture["kind"] == "decompression-bomb":
        return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 25000000, 25000000, 8, 2, 0, 0, 0)) + _chunk(b"IEND", b"")
    return _image(fixture)


def generate(*, check: bool = False, root: Path = ROOT) -> dict:
    spec_path = root / "fixture-spec.json"
    corpus = json.loads(spec_path.read_text(encoding="utf-8"))
    validate_corpus(corpus)
    changed = False
    for fixture in corpus["fixtures"]:
        if fixture["media_type"] == "image/png" and fixture["expected_outcome"] == "accepted":
            for line in fixture["pages"][0]["lines"]:
                expected_polygon = _expected_polygon(fixture, line)
                if line["polygon"] != expected_polygon:
                    if check:
                        raise ValueError("OCR fixture annotation differs")
                    line["polygon"] = expected_polygon
                    changed = True
        if fixture["kind"] == "mixed-pdf":
            first_polygon = [[42, 78], [468, 78], [468, 122], [42, 122]]
            second = fixture["pages"][1]["lines"][0]
            scale = min(6, 550 // (len(second["text"]) * 6 - 1))
            second_polygon = [[40, 80], [40 + (len(second["text"]) * 6 - 1) * scale, 80], [40 + (len(second["text"]) * 6 - 1) * scale, 80 + 7 * scale], [40, 80 + 7 * scale]]
            for line, polygon in ((fixture["pages"][0]["lines"][0], first_polygon), (second, second_polygon)):
                if line["polygon"] != polygon:
                    if check:
                        raise ValueError("OCR fixture annotation differs")
                    line["polygon"] = polygon
                    changed = True
        body = generate_fixture_bytes(fixture)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        path = root / fixture["file"]
        if check:
            if not path.is_file() or path.read_bytes() != body or fixture["sha256"] != digest:
                raise ValueError("OCR fixture bytes differ")
        else:
            path.write_bytes(body)
            if fixture["sha256"] != digest:
                fixture["sha256"] = digest; changed = True
    if not check and changed:
        spec_path.write_text(json.dumps(corpus, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return corpus


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generate(check=args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
