#!/usr/bin/env python3
"""Generate deterministic, public-safe DOCX fixtures for AO Lore."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "fixture-spec.json"
FIXED_DATE = (2020, 1, 1, 0, 0, 0)
CORPUS_KEYS = {"schema_version", "fixtures"}
FIXTURE_KEYS = {
    "id",
    "file",
    "sha256",
    "paragraphs",
    "tables",
    "nested_tables",
    "hyperlinks",
    "footnotes",
    "endnotes",
    "media",
}
PARAGRAPH_KEYS = {"text", "style", "list_level"}
NESTED_TABLE_KEYS = {"table_index", "row_index", "cell_index", "rows"}
MEDIA_KEYS = {"name", "hex", "content_type"}
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_RE = re.compile(r"^[0-9A-Fa-f]*$")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
CP = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC = "http://purl.org/dc/elements/1.1/"
DCTERMS = "http://purl.org/dc/terms/"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
APP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"

ET.register_namespace("", REL)
ET.register_namespace("w", W)
ET.register_namespace("r", R)
ET.register_namespace("cp", CP)
ET.register_namespace("dc", DC)
ET.register_namespace("dcterms", DCTERMS)
ET.register_namespace("xsi", XSI)
ET.register_namespace("a", A)
ET.register_namespace("wp", WP)
ET.register_namespace("pic", PIC)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_load(path: Path) -> dict[str, Any]:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid fixture spec JSON: {exc}") from exc


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _xml(root: ET.Element) -> bytes:
    return ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=True)


def _require_exact_tuple(value: object, label: str) -> tuple[Any, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{label} must be an exact tuple")
    return value


def _require_exact_dict(value: object, label: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an exact dict")
    return value


def _require_exact_str(value: object, label: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} must be a non-empty exact string")
    return value


def _require_exact_int(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an exact integer >= {minimum}")
    return value


def _reject_control_text(value: str, label: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains forbidden control characters")
    return value


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _ensure_real_directory(path: Path, *, create: bool) -> Path:
    absolute = _absolute(path)
    if absolute.parent != absolute:
        _ensure_real_directory(absolute.parent, create=create)
    try:
        info = os.lstat(absolute)
    except FileNotFoundError:
        if not create:
            raise ValueError(f"destination directory is missing: {absolute}") from None
        os.mkdir(absolute, 0o755)
        info = os.lstat(absolute)
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"destination directory is a symlink: {absolute}")
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"destination path is not a directory: {absolute}")
    return absolute


def _open_regular_nofollow(path: Path, *, flags: int, mode: int = 0o644) -> tuple[int, os.stat_result]:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"target is a symlink: {path.name}")
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"target is not a regular file: {path.name}")
    if before.st_nlink != 1:
        raise ValueError(f"target must be a single-link file: {path.name}")
    descriptor = os.open(path, flags | nofollow, mode)
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
    ):
        os.close(descriptor)
        raise ValueError(f"target changed during validation: {path.name}")
    return descriptor, opened


def _read_target_bytes(path: Path) -> bytes:
    try:
        descriptor, _ = _open_regular_nofollow(path, flags=os.O_RDONLY)
    except FileNotFoundError:
        raise ValueError(f"fixture file is missing: {path.name}") from None
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_target_bytes(path: Path, body: bytes) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        descriptor, _ = _open_regular_nofollow(path, flags=os.O_WRONLY | os.O_TRUNC)
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_paragraphs(paragraphs: tuple[dict[str, object], ...]) -> None:
    for index, paragraph in enumerate(paragraphs):
        value = _require_exact_dict(paragraph, f"paragraphs[{index}]")
        if set(value) - PARAGRAPH_KEYS or "text" not in value:
            raise ValueError("paragraph keys differ")
        _require_exact_str(value["text"], f"paragraphs[{index}].text")
        if "style" in value:
            _require_exact_str(value["style"], f"paragraphs[{index}].style")
        if "list_level" in value:
            _require_exact_int(value["list_level"], f"paragraphs[{index}].list_level")


def _validate_tables(
    tables: tuple[tuple[tuple[str, ...], ...], ...], label: str = "tables"
) -> None:
    for table_index, table in enumerate(_require_exact_tuple(tables, label)):
        rows = _require_exact_tuple(table, f"{label}[{table_index}]")
        if not rows:
            raise ValueError("table must contain at least one row")
        width = None
        for row_index, row in enumerate(rows):
            cells = _require_exact_tuple(row, f"{label}[{table_index}][{row_index}]")
            if not cells:
                raise ValueError("table row must contain at least one cell")
            if width is None:
                width = len(cells)
            elif width != len(cells):
                raise ValueError("table rows must have a stable width")
            for cell_index, cell in enumerate(cells):
                _require_exact_str(cell, f"{label}[{table_index}][{row_index}][{cell_index}]")


def _validate_hyperlinks(hyperlinks: tuple[tuple[str, str], ...]) -> None:
    for index, link in enumerate(_require_exact_tuple(hyperlinks, "hyperlinks")):
        pair = _require_exact_tuple(link, f"hyperlinks[{index}]")
        if len(pair) != 2:
            raise ValueError("hyperlink entries must contain display text and target")
        _require_exact_str(pair[0], f"hyperlinks[{index}][0]")
        _require_exact_str(pair[1], f"hyperlinks[{index}][1]")


def _validate_notes(values: tuple[str, ...], label: str) -> None:
    for index, value in enumerate(_require_exact_tuple(values, label)):
        _require_exact_str(value, f"{label}[{index}]")


def _validate_media(media: tuple[tuple[str, bytes, str], ...]) -> None:
    for index, entry in enumerate(_require_exact_tuple(media, "media")):
        item = _require_exact_tuple(entry, f"media[{index}]")
        if len(item) != 3:
            raise ValueError("media entries must contain name, bytes, and content type")
        _require_local_filename(item[0], f"media[{index}][0]")
        if type(item[1]) is not bytes or not item[1]:
            raise ValueError("media bytes must be exact non-empty bytes")
        _require_exact_str(item[2], f"media[{index}][2]")


def _require_local_filename(value: object, label: str, *, suffix: str | None = None) -> str:
    name = _require_exact_str(value, label)
    _reject_control_text(name, label)
    path = PurePosixPath(name)
    if (
        name in {".", ".."}
        or path.name != name
        or "/" in name
        or "\\" in name
        or "//" in name
        or name.startswith(".")
    ):
        raise ValueError(f"{label} must be a strict local filename")
    if suffix is not None and path.suffix.lower() != suffix:
        raise ValueError(f"{label} must end with {suffix}")
    return name


def _body_paragraph(
    text: str,
    *,
    style: str | None = None,
    list_level: int | None = None,
    hyperlink_rid: str | None = None,
    footnote_id: int | None = None,
    endnote_id: int | None = None,
    drawing_rid: str | None = None,
) -> ET.Element:
    paragraph = ET.Element(_q(W, "p"))
    if style is not None or list_level is not None:
        properties = ET.SubElement(paragraph, _q(W, "pPr"))
        if style is not None:
            ET.SubElement(properties, _q(W, "pStyle"), {_q(W, "val"): style})
        if list_level is not None:
            numbering = ET.SubElement(properties, _q(W, "numPr"))
            ET.SubElement(numbering, _q(W, "ilvl"), {_q(W, "val"): str(list_level)})
            ET.SubElement(numbering, _q(W, "numId"), {_q(W, "val"): "1"})
    run = ET.SubElement(paragraph, _q(W, "r"))
    text_node = ET.SubElement(run, _q(W, "t"))
    if text.startswith(" ") or text.endswith(" "):
        text_node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    text_node.text = text
    if hyperlink_rid is not None:
        hyperlink = ET.SubElement(paragraph, _q(W, "hyperlink"), {_q(R, "id"): hyperlink_rid})
        link_run = ET.SubElement(hyperlink, _q(W, "r"))
        link_props = ET.SubElement(link_run, _q(W, "rPr"))
        ET.SubElement(link_props, _q(W, "rStyle"), {_q(W, "val"): "Hyperlink"})
        link_text = ET.SubElement(link_run, _q(W, "t"))
        link_text.text = " Hyperlink"
    if footnote_id is not None:
        footnote_run = ET.SubElement(paragraph, _q(W, "r"))
        ET.SubElement(footnote_run, _q(W, "footnoteReference"), {_q(W, "id"): str(footnote_id)})
    if endnote_id is not None:
        endnote_run = ET.SubElement(paragraph, _q(W, "r"))
        ET.SubElement(endnote_run, _q(W, "endnoteReference"), {_q(W, "id"): str(endnote_id)})
    if drawing_rid is not None:
        drawing_run = ET.SubElement(paragraph, _q(W, "r"))
        drawing = ET.SubElement(drawing_run, _q(W, "drawing"))
        inline = ET.SubElement(drawing, _q(WP, "inline"))
        extent = ET.SubElement(inline, _q(WP, "extent"))
        extent.set("cx", "190500")
        extent.set("cy", "190500")
        graphic = ET.SubElement(inline, _q(A, "graphic"))
        graphic_data = ET.SubElement(graphic, _q(A, "graphicData"))
        graphic_data.set("uri", "http://schemas.openxmlformats.org/drawingml/2006/picture")
        picture = ET.SubElement(graphic_data, _q(PIC, "pic"))
        blip_fill = ET.SubElement(picture, _q(PIC, "blipFill"))
        ET.SubElement(blip_fill, _q(A, "blip"), {_q(R, "embed"): drawing_rid})
    return paragraph


def _table(rows: tuple[tuple[str, ...], ...], nested: dict[tuple[int, int], tuple[tuple[str, ...], ...]]) -> ET.Element:
    table = ET.Element(_q(W, "tbl"))
    properties = ET.SubElement(table, _q(W, "tblPr"))
    ET.SubElement(properties, _q(W, "tblStyle"), {_q(W, "val"): "TableGrid"})
    grid = ET.SubElement(table, _q(W, "tblGrid"))
    for _ in rows[0]:
        ET.SubElement(grid, _q(W, "gridCol"), {_q(W, "w"): "2400"})
    for row_index, row in enumerate(rows):
        row_node = ET.SubElement(table, _q(W, "tr"))
        for cell_index, cell in enumerate(row):
            cell_node = ET.SubElement(row_node, _q(W, "tc"))
            cell_node.append(_body_paragraph(cell))
            nested_rows = nested.get((row_index, cell_index))
            if nested_rows is not None:
                cell_node.append(_table(nested_rows, {}))
            cell_props = ET.SubElement(cell_node, _q(W, "tcPr"))
            ET.SubElement(cell_props, _q(W, "tcW"), {_q(W, "w"): "2400", _q(W, "type"): "dxa"})
    return table


def _footnotes(values: tuple[str, ...], tag: str) -> bytes:
    root = ET.Element(_q(W, tag))
    for index, value in enumerate(values, start=1):
        note = ET.SubElement(root, _q(W, tag[:-1]), {_q(W, "id"): str(index)})
        note.append(_body_paragraph(value))
    return _xml(root)


def _styles() -> bytes:
    root = ET.Element(_q(W, "styles"))
    styles = (
        ("Normal", "Normal", None),
        ("Heading1", "Heading 1", "1"),
        ("Heading2", "Heading 2", "2"),
        ("Hyperlink", "Hyperlink", None),
        ("TableGrid", "Table Grid", None),
    )
    for style_id, name, outline in styles:
        style = ET.SubElement(
            root,
            _q(W, "style"),
            {_q(W, "type"): "paragraph", _q(W, "styleId"): style_id},
        )
        ET.SubElement(style, _q(W, "name"), {_q(W, "val"): name})
        if outline is not None:
            ET.SubElement(style, _q(W, "outlineLvl"), {_q(W, "val"): outline})
    return _xml(root)


def _numbering() -> bytes:
    root = ET.Element(_q(W, "numbering"))
    abstract = ET.SubElement(root, _q(W, "abstractNum"), {_q(W, "abstractNumId"): "0"})
    for level in (0, 1):
        current = ET.SubElement(abstract, _q(W, "lvl"), {_q(W, "ilvl"): str(level)})
        ET.SubElement(current, _q(W, "start"), {_q(W, "val"): "1"})
        ET.SubElement(current, _q(W, "numFmt"), {_q(W, "val"): "bullet"})
        ET.SubElement(current, _q(W, "lvlText"), {_q(W, "val"): "•"})
    number = ET.SubElement(root, _q(W, "num"), {_q(W, "numId"): "1"})
    ET.SubElement(number, _q(W, "abstractNumId"), {_q(W, "val"): "0"})
    return _xml(root)


def _document_xml(
    paragraphs: tuple[dict[str, object], ...],
    tables: tuple[tuple[tuple[str, ...], ...], ...],
    hyperlinks: tuple[tuple[str, str], ...],
    footnotes: tuple[str, ...],
    endnotes: tuple[str, ...],
    media: tuple[tuple[str, bytes, str], ...],
    *,
    hyperlink_ids: tuple[str, ...],
    drawing_ids: tuple[str, ...],
    nested_tables: tuple[dict[str, object], ...],
) -> bytes:
    document = ET.Element(_q(W, "document"))
    body = ET.SubElement(document, _q(W, "body"))
    for paragraph in paragraphs:
        body.append(
            _body_paragraph(
                paragraph["text"],
                style=paragraph.get("style"),
                list_level=paragraph.get("list_level"),
            )
        )
    for index, (display, _) in enumerate(hyperlinks, start=1):
        body.append(_body_paragraph(display, hyperlink_rid=hyperlink_ids[index - 1]))
    for index, value in enumerate(footnotes, start=1):
        body.append(_body_paragraph(f"Footnote {index}", footnote_id=index))
    for index, value in enumerate(endnotes, start=1):
        body.append(_body_paragraph(f"Endnote {index}", endnote_id=index))
    for index, _ in enumerate(media, start=1):
        body.append(_body_paragraph(f"Drawing {index}", drawing_rid=drawing_ids[index - 1]))
    nested_lookup: dict[int, dict[tuple[int, int], tuple[tuple[str, ...], ...]]] = {}
    for descriptor in nested_tables:
        nested_lookup.setdefault(descriptor["table_index"], {})[
            (descriptor["row_index"], descriptor["cell_index"])
        ] = descriptor["rows"]
    for table_index, table_rows in enumerate(tables):
        body.append(_table(table_rows, nested_lookup.get(table_index, {})))
    section = ET.SubElement(body, _q(W, "sectPr"))
    ET.SubElement(section, _q(W, "pgSz"), {_q(W, "w"): "12240", _q(W, "h"): "15840"})
    return _xml(document)


def _document_relationships(
    hyperlinks: tuple[tuple[str, str], ...],
    footnotes: tuple[str, ...],
    endnotes: tuple[str, ...],
    media: tuple[tuple[str, bytes, str], ...],
    has_numbering: bool,
    *,
    hyperlink_ids: tuple[str, ...],
    drawing_ids: tuple[str, ...],
) -> bytes:
    root = ET.Element(_q(REL, "Relationships"))
    relationship_id = 1

    def add(
        target: str,
        relationship_type: str,
        *,
        mode: str | None = None,
        identifier: str | None = None,
    ) -> None:
        nonlocal relationship_id
        attributes = {
            "Id": identifier or f"rId{relationship_id}",
            "Type": relationship_type,
            "Target": target,
        }
        if mode is not None:
            attributes["TargetMode"] = mode
        ET.SubElement(root, _q(REL, "Relationship"), attributes)
        if identifier is None:
            relationship_id += 1

    add("styles.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles")
    if has_numbering:
        add("numbering.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering")
    if footnotes:
        add("footnotes.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes")
    if endnotes:
        add("endnotes.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes")
    for identifier, (_, target) in zip(hyperlink_ids, hyperlinks):
        add(
            target,
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            mode="External",
            identifier=identifier,
        )
    for identifier, (name, _, _) in zip(drawing_ids, media):
        add(
            f"media/{name}",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
            identifier=identifier,
        )
    return _xml(root)


def _root_relationships() -> bytes:
    root = ET.Element(_q(REL, "Relationships"))
    relationships = (
        ("rId1", "docProps/app.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties"),
        ("rId2", "docProps/core.xml", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"),
        ("rId3", "word/document.xml", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"),
    )
    for identifier, target, relationship_type in relationships:
        ET.SubElement(
            root,
            _q(REL, "Relationship"),
            {"Id": identifier, "Type": relationship_type, "Target": target},
        )
    return _xml(root)


def _content_types(
    *,
    has_numbering: bool,
    has_footnotes: bool,
    has_endnotes: bool,
    media: tuple[tuple[str, bytes, str], ...],
) -> bytes:
    root = ET.Element(_q(CT, "Types"))
    defaults = (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("xml", "application/xml"),
    )
    if media:
        defaults += (("bin", "application/octet-stream"),)
    for extension, content_type in defaults:
        ET.SubElement(root, _q(CT, "Default"), {"Extension": extension, "ContentType": content_type})
    overrides = (
        ("/docProps/app.xml", "application/vnd.openxmlformats-officedocument.extended-properties+xml"),
        ("/docProps/core.xml", "application/vnd.openxmlformats-package.core-properties+xml"),
        ("/word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"),
        ("/word/styles.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"),
    )
    for part_name, content_type in overrides:
        ET.SubElement(root, _q(CT, "Override"), {"PartName": part_name, "ContentType": content_type})
    if has_numbering:
        ET.SubElement(
            root,
            _q(CT, "Override"),
            {
                "PartName": "/word/numbering.xml",
                "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
            },
        )
    if has_footnotes:
        ET.SubElement(
            root,
            _q(CT, "Override"),
            {
                "PartName": "/word/footnotes.xml",
                "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
            },
        )
    if has_endnotes:
        ET.SubElement(
            root,
            _q(CT, "Override"),
            {
                "PartName": "/word/endnotes.xml",
                "ContentType": "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
            },
        )
    return _xml(root)


def _app_properties() -> bytes:
    root = ET.Element(_q(APP, "Properties"))
    ET.SubElement(root, _q(APP, "Application")).text = "AO Lore"
    ET.SubElement(root, _q(APP, "DocSecurity")).text = "0"
    heading_pairs = ET.SubElement(root, _q(APP, "HeadingPairs"))
    variant = ET.SubElement(heading_pairs, _q(VT, "vector"), {"size": "2", "baseType": "variant"})
    heading = ET.SubElement(variant, _q(VT, "variant"))
    ET.SubElement(heading, _q(VT, "lpstr")).text = "Title"
    count = ET.SubElement(variant, _q(VT, "variant"))
    ET.SubElement(count, _q(VT, "i4")).text = "1"
    titles = ET.SubElement(root, _q(APP, "TitlesOfParts"))
    titles_vector = ET.SubElement(titles, _q(VT, "vector"), {"size": "1", "baseType": "lpstr"})
    ET.SubElement(titles_vector, _q(VT, "lpstr")).text = "Document"
    return _xml(root)


def _core_properties() -> bytes:
    root = ET.Element(_q(CP, "coreProperties"))
    ET.SubElement(root, _q(DC, "title")).text = "AO Lore DOCX Fixture"
    ET.SubElement(root, _q(DC, "creator")).text = "AO Lore"
    created = ET.SubElement(root, _q(DCTERMS, "created"))
    created.set(_q(XSI, "type"), "dcterms:W3CDTF")
    created.text = "2020-01-01T00:00:00Z"
    modified = ET.SubElement(root, _q(DCTERMS, "modified"))
    modified.set(_q(XSI, "type"), "dcterms:W3CDTF")
    modified.text = "2020-01-01T00:00:00Z"
    return _xml(root)


def _zip_bytes(parts: dict[str, bytes]) -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(parts):
            info = zipfile.ZipInfo(name, FIXED_DATE)
            info.create_system = 0
            info.external_attr = 0
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, parts[name], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def build_docx_fixture(
    *,
    paragraphs: tuple[dict[str, object], ...],
    tables: tuple[tuple[tuple[str, ...], ...], ...] = (),
    hyperlinks: tuple[tuple[str, str], ...] = (),
    footnotes: tuple[str, ...] = (),
    endnotes: tuple[str, ...] = (),
    media: tuple[tuple[str, bytes, str], ...] = (),
) -> bytes:
    paragraphs = _require_exact_tuple(paragraphs, "paragraphs")
    tables = _require_exact_tuple(tables, "tables")
    hyperlinks = _require_exact_tuple(hyperlinks, "hyperlinks")
    footnotes = _require_exact_tuple(footnotes, "footnotes")
    endnotes = _require_exact_tuple(endnotes, "endnotes")
    media = _require_exact_tuple(media, "media")
    _validate_paragraphs(paragraphs)
    _validate_tables(tables)
    _validate_hyperlinks(hyperlinks)
    _validate_notes(footnotes, "footnotes")
    _validate_notes(endnotes, "endnotes")
    _validate_media(media)
    has_numbering = any("list_level" in paragraph for paragraph in paragraphs)
    hyperlink_ids = tuple(f"rIdLink{index}" for index in range(1, len(hyperlinks) + 1))
    drawing_ids = tuple(f"rIdMedia{index}" for index in range(1, len(media) + 1))
    parts = {
        "[Content_Types].xml": _content_types(
            has_numbering=has_numbering,
            has_footnotes=bool(footnotes),
            has_endnotes=bool(endnotes),
            media=media,
        ),
        "_rels/.rels": _root_relationships(),
        "docProps/app.xml": _app_properties(),
        "docProps/core.xml": _core_properties(),
        "word/_rels/document.xml.rels": _document_relationships(
            hyperlinks,
            footnotes,
            endnotes,
            media,
            has_numbering,
            hyperlink_ids=hyperlink_ids,
            drawing_ids=drawing_ids,
        ),
        "word/document.xml": _document_xml(
            paragraphs,
            tables,
            hyperlinks,
            footnotes,
            endnotes,
            media,
            hyperlink_ids=hyperlink_ids,
            drawing_ids=drawing_ids,
            nested_tables=(),
        ),
        "word/styles.xml": _styles(),
    }
    if has_numbering:
        parts["word/numbering.xml"] = _numbering()
    if footnotes:
        parts["word/footnotes.xml"] = _footnotes(footnotes, "footnotes")
    if endnotes:
        parts["word/endnotes.xml"] = _footnotes(endnotes, "endnotes")
    for name, body, _ in media:
        parts[f"word/media/{name}"] = body
    return _zip_bytes(parts)


def _to_tuple_rows(rows: list[list[str]], label: str) -> tuple[tuple[str, ...], ...]:
    if type(rows) is not list or not rows:
        raise ValueError(f"{label} must be a non-empty list")
    converted: list[tuple[str, ...]] = []
    width = None
    for row_index, row in enumerate(rows):
        if type(row) is not list or not row:
            raise ValueError(f"{label}[{row_index}] must be a non-empty list")
        values: list[str] = []
        if width is None:
            width = len(row)
        elif width != len(row):
            raise ValueError(f"{label} rows must have a stable width")
        for cell_index, cell in enumerate(row):
            if type(cell) is not str or not cell:
                raise ValueError(f"{label}[{row_index}][{cell_index}] must be a non-empty string")
            values.append(cell)
        converted.append(tuple(values))
    return tuple(converted)


def _fixture_to_inputs(fixture: dict[str, Any]) -> tuple[
    tuple[dict[str, object], ...],
    tuple[tuple[tuple[str, ...], ...], ...],
    tuple[tuple[str, str], ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[tuple[str, bytes, str], ...],
    tuple[dict[str, object], ...],
]:
    paragraphs_raw = fixture["paragraphs"]
    if type(paragraphs_raw) is not list:
        raise ValueError("paragraphs must be a list")
    paragraphs: list[dict[str, object]] = []
    for index, paragraph in enumerate(paragraphs_raw):
        if type(paragraph) is not dict or set(paragraph) - PARAGRAPH_KEYS or "text" not in paragraph:
            raise ValueError(f"paragraphs[{index}] keys differ")
        converted: dict[str, object] = {"text": _require_exact_str(paragraph["text"], f"paragraphs[{index}].text")}
        if "style" in paragraph:
            converted["style"] = _require_exact_str(paragraph["style"], f"paragraphs[{index}].style")
        if "list_level" in paragraph:
            converted["list_level"] = _require_exact_int(paragraph["list_level"], f"paragraphs[{index}].list_level")
        paragraphs.append(converted)
    tables_raw = fixture["tables"]
    if type(tables_raw) is not list:
        raise ValueError("tables must be a list")
    tables = tuple(_to_tuple_rows(table, f"tables[{index}]") for index, table in enumerate(tables_raw))
    nested_raw = fixture["nested_tables"]
    if type(nested_raw) is not list:
        raise ValueError("nested_tables must be a list")
    nested_tables: list[dict[str, object]] = []
    for index, descriptor in enumerate(nested_raw):
        if type(descriptor) is not dict or set(descriptor) != NESTED_TABLE_KEYS:
            raise ValueError(f"nested_tables[{index}] keys differ")
        nested_tables.append(
            {
                "table_index": _require_exact_int(descriptor["table_index"], f"nested_tables[{index}].table_index"),
                "row_index": _require_exact_int(descriptor["row_index"], f"nested_tables[{index}].row_index"),
                "cell_index": _require_exact_int(descriptor["cell_index"], f"nested_tables[{index}].cell_index"),
                "rows": _to_tuple_rows(descriptor["rows"], f"nested_tables[{index}].rows"),
            }
        )
    hyperlinks_raw = fixture["hyperlinks"]
    if type(hyperlinks_raw) is not list:
        raise ValueError("hyperlinks must be a list")
    hyperlinks: list[tuple[str, str]] = []
    for index, pair in enumerate(hyperlinks_raw):
        if type(pair) is not list or len(pair) != 2:
            raise ValueError(f"hyperlinks[{index}] must contain display text and target")
        hyperlinks.append(
            (
                _require_exact_str(pair[0], f"hyperlinks[{index}][0]"),
                _require_exact_str(pair[1], f"hyperlinks[{index}][1]"),
            )
        )
    def convert_notes(name: str) -> tuple[str, ...]:
        values = fixture[name]
        if type(values) is not list:
            raise ValueError(f"{name} must be a list")
        return tuple(_require_exact_str(value, f"{name}[{index}]") for index, value in enumerate(values))
    media_raw = fixture["media"]
    if type(media_raw) is not list:
        raise ValueError("media must be a list")
    media: list[tuple[str, bytes, str]] = []
    for index, entry in enumerate(media_raw):
        if type(entry) is not dict or set(entry) != MEDIA_KEYS:
            raise ValueError(f"media[{index}] keys differ")
        name = _require_local_filename(entry["name"], f"media[{index}].name")
        hex_value = _require_exact_str(entry["hex"], f"media[{index}].hex")
        if len(hex_value) % 2 or HEX_RE.fullmatch(hex_value) is None:
            raise ValueError("media hex must contain an even-length hexadecimal string")
        media.append(
            (
                name,
                bytes.fromhex(hex_value),
                _require_exact_str(entry["content_type"], f"media[{index}].content_type"),
            )
        )
    return (
        tuple(paragraphs),
        tables,
        tuple(hyperlinks),
        convert_notes("footnotes"),
        convert_notes("endnotes"),
        tuple(media),
        tuple(nested_tables),
    )


def validate_corpus(corpus: dict[str, Any]) -> None:
    if type(corpus) is not dict or set(corpus) != CORPUS_KEYS:
        raise ValueError("fixture corpus keys differ")
    if corpus["schema_version"] != "ao.lore.docx-fixture-corpus.v0.1":
        raise ValueError("fixture corpus schema version differs")
    fixtures = corpus["fixtures"]
    if type(fixtures) is not list or not fixtures:
        raise ValueError("fixture corpus fixtures must be a non-empty list")
    seen_ids: set[str] = set()
    seen_files: set[str] = set()
    for fixture in fixtures:
        if type(fixture) is not dict or set(fixture) != FIXTURE_KEYS:
            raise ValueError("fixture keys differ")
        fixture_id = _require_exact_str(fixture["id"], "fixture.id")
        if fixture_id in seen_ids:
            raise ValueError("fixture identity is duplicated")
        file_name = _require_local_filename(fixture["file"], "fixture.file", suffix=".docx")
        if file_name in seen_files:
            raise ValueError("fixture filename is duplicated")
        digest = _require_exact_str(fixture["sha256"], "fixture.sha256")
        if SHA256_RE.fullmatch(digest) is None:
            raise ValueError("fixture digest is invalid")
        _fixture_to_inputs(fixture)
        seen_ids.add(fixture_id)
        seen_files.add(file_name)


def _validate_fixture_mapping(fixture: dict[str, Any]) -> None:
    if type(fixture) is not dict or set(fixture) != FIXTURE_KEYS:
        raise ValueError("fixture keys differ")
    _require_exact_str(fixture["id"], "fixture.id")
    _require_local_filename(fixture["file"], "fixture.file", suffix=".docx")
    digest = _require_exact_str(fixture["sha256"], "fixture.sha256")
    if SHA256_RE.fullmatch(digest) is None:
        raise ValueError("fixture digest is invalid")


def load_fixture_spec(path: Path = SPEC) -> dict[str, Any]:
    corpus = _strict_json_load(path)
    validate_corpus(corpus)
    return corpus


def generate_fixture_bytes(fixture: dict[str, Any]) -> bytes:
    _validate_fixture_mapping(fixture)
    (
        paragraphs,
        tables,
        hyperlinks,
        footnotes,
        endnotes,
        media,
        nested_tables,
    ) = _fixture_to_inputs(fixture)
    body = build_docx_fixture(
        paragraphs=paragraphs,
        tables=tables,
        hyperlinks=hyperlinks,
        footnotes=footnotes,
        endnotes=endnotes,
        media=media,
    )
    if not nested_tables:
        return body
    parts = {
        "[Content_Types].xml": None,
        "_rels/.rels": None,
        "docProps/app.xml": None,
        "docProps/core.xml": None,
        "word/_rels/document.xml.rels": None,
        "word/document.xml": None,
        "word/styles.xml": None,
    }
    has_numbering = any("list_level" in paragraph for paragraph in paragraphs)
    hyperlink_ids = tuple(f"rIdLink{index}" for index in range(1, len(hyperlinks) + 1))
    drawing_ids = tuple(f"rIdMedia{index}" for index in range(1, len(media) + 1))
    parts["[Content_Types].xml"] = _content_types(
        has_numbering=has_numbering,
        has_footnotes=bool(footnotes),
        has_endnotes=bool(endnotes),
        media=media,
    )
    parts["_rels/.rels"] = _root_relationships()
    parts["docProps/app.xml"] = _app_properties()
    parts["docProps/core.xml"] = _core_properties()
    parts["word/_rels/document.xml.rels"] = _document_relationships(
        hyperlinks,
        footnotes,
        endnotes,
        media,
        has_numbering,
        hyperlink_ids=hyperlink_ids,
        drawing_ids=drawing_ids,
    )
    parts["word/document.xml"] = _document_xml(
        paragraphs,
        tables,
        hyperlinks,
        footnotes,
        endnotes,
        media,
        hyperlink_ids=hyperlink_ids,
        drawing_ids=drawing_ids,
        nested_tables=nested_tables,
    )
    parts["word/styles.xml"] = _styles()
    if has_numbering:
        parts["word/numbering.xml"] = _numbering()
    if footnotes:
        parts["word/footnotes.xml"] = _footnotes(footnotes, "footnotes")
    if endnotes:
        parts["word/endnotes.xml"] = _footnotes(endnotes, "endnotes")
    for name, body_bytes, _ in media:
        parts[f"word/media/{name}"] = body_bytes
    return _zip_bytes({name: value for name, value in parts.items() if value is not None})


def _generate_with_spec(destination: Path, *, check: bool, spec_path: Path) -> None:
    corpus = load_fixture_spec(spec_path)
    root = _ensure_real_directory(destination, create=not check)
    for fixture in corpus["fixtures"]:
        body = generate_fixture_bytes(fixture)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if digest != fixture["sha256"]:
            raise ValueError(f"fixture digest drifted: {fixture['id']}")
        target = root / fixture["file"]
        if check:
            if _read_target_bytes(target) != body:
                raise ValueError(f"fixture bytes drifted: {target.name}")
        else:
            _write_target_bytes(target, body)


def generate(destination: Path, *, check: bool) -> None:
    _generate_with_spec(destination, check=check, spec_path=SPEC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Verify tracked bytes without rewriting.")
    parser.add_argument(
        "--destination",
        type=Path,
        default=ROOT,
        help="Output directory for generated DOCX fixtures.",
    )
    args = parser.parse_args(argv)
    generate(args.destination, check=args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
