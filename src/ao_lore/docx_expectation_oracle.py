"""Independent semantic oracle for validated DOCX qualification fixtures."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Mapping

from .benchmark import canonical_digest
from .docx_ooxml import DocxLimits, ValidatedDocxPackage
from .docx_semantic_contract import (
    A_NS,
    COUNT_KEYS,
    DGM_NS,
    DIAGRAM_RELATIONSHIPS,
    DIAGRAM_GRAPHIC_URI,
    HYPERLINK_HISTORY_TRUE,
    HYPERLINK_REL,
    IMAGE_REL,
    MAXIMUM_ANCHOR_CHARACTERS,
    PICTURE_GRAPHIC_URI,
    R_NS,
    W_NS,
    WORDPROCESSING_SHAPE_GRAPHIC_URI,
    WPS_NS,
)


class DocxExpectationOracleError(RuntimeError):
    """Raised when validated bytes cannot satisfy the independent oracle."""


_ORACLE_RSID = re.compile(r"[0-9A-F]{8}\Z")
_ORACLE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,1024}\Z")
_ORACLE_UNSIGNED = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
_ORACLE_SIGNED = re.compile(r"-?(?:0|[1-9][0-9]{0,9})\Z")
_ORACLE_HEX_COLOR = re.compile(r"(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})\Z")
_ORACLE_LANGUAGE = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*\Z")
_ORACLE_PAGEREF = re.compile(
    r" *PAGEREF ([A-Za-z_][A-Za-z0-9_]{0,1011}) \\h *\Z"
)


@dataclass(frozen=True)
class DocxSemanticExpectation:
    counts: Mapping[str, int]
    text_tokens: tuple[str, ...]
    structural_events: tuple[str, ...]
    normalized_text_digest: str
    structural_event_digest: str


@dataclass
class _Budget:
    elements: int = 0
    text_characters: int = 0
    blocks: int = 0


@dataclass
class _TableValue:
    text: str
    rows: int
    cells: int
    nested: list["_TableValue"]


def _fail() -> DocxExpectationOracleError:
    return DocxExpectationOracleError("private DOCX expectation is invalid")


def _q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _w(name: str) -> str:
    return _q(W_NS, name)


def _normalized(value: str) -> str:
    return " ".join(value.split())


def _root(
    package: ValidatedDocxPackage,
    name: str,
    limits: DocxLimits,
    budget: _Budget,
) -> ET.Element:
    body = package.parts.get(name)
    if type(body) is not bytes or len(body) > limits.maximum_member_bytes:
        raise _fail()
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise _fail() from exc
    stack = [(root, 1)]
    local_elements = 0
    local_text = 0
    local_blocks = 0
    maximum_depth = 0
    while stack:
        node, depth = stack.pop()
        local_elements += 1
        local_text += len(node.text or "") + len(node.tail or "")
        local_text += sum(len(key) + len(value) for key, value in node.attrib.items())
        if node.tag in {_w("p"), _w("tbl")}:
            local_blocks += 1
        maximum_depth = max(maximum_depth, depth)
        stack.extend((child, depth + 1) for child in reversed(list(node)))
    if (
        local_elements > limits.maximum_xml_elements
        or local_text > limits.maximum_text_characters
        or local_blocks > limits.maximum_blocks
        or maximum_depth > limits.maximum_xml_depth
    ):
        raise _fail()
    budget.elements += local_elements
    budget.text_characters += local_text
    budget.blocks += local_blocks
    if (
        budget.elements > limits.maximum_xml_elements
        or budget.text_characters > limits.maximum_aggregate_text_characters
        or budget.blocks > limits.maximum_blocks
    ):
        raise _fail()
    return root


def _relationship_map(
    package: ValidatedDocxPackage, limits: DocxLimits
) -> dict[str, Mapping[str, str]]:
    records = package.relationships.get("word/document.xml")
    if records is None:
        raise _fail()
    if len(records) > limits.maximum_relationships:
        raise _fail()
    result: dict[str, Mapping[str, str]] = {}
    for record in records:
        relation_id = record.get("Id")
        if type(relation_id) is not str or not relation_id or relation_id in result:
            raise _fail()
        result[relation_id] = record
    return result


def _style_levels(
    package: ValidatedDocxPackage, limits: DocxLimits, budget: _Budget
) -> dict[str, int]:
    if "word/styles.xml" not in package.parts:
        return {}
    root = _root(package, "word/styles.xml", limits, budget)
    result: dict[str, int] = {}
    for style in root.findall(_w("style")):
        if style.attrib.get(_w("type")) != "paragraph":
            continue
        style_id = style.attrib.get(_w("styleId"))
        if type(style_id) is not str or not style_id:
            continue
        outline = style.find(_w("outlineLvl"))
        raw = None if outline is None else outline.attrib.get(_w("val"))
        if type(raw) is str and raw.isdigit():
            result[style_id] = max(1, min(6, int(raw) + 1))
            continue
        suffix = style_id[len("Heading") :] if style_id.startswith("Heading") else ""
        if suffix.isdigit():
            result[style_id] = max(1, min(6, int(suffix)))
    return result


def _numbering(
    package: ValidatedDocxPackage, limits: DocxLimits, budget: _Budget
) -> dict[tuple[str, int], str]:
    if "word/numbering.xml" not in package.parts:
        return {}
    root = _root(package, "word/numbering.xml", limits, budget)
    abstracts: dict[str, dict[int, str]] = {}
    for abstract in root.findall(_w("abstractNum")):
        abstract_id = abstract.attrib.get(_w("abstractNumId"))
        if type(abstract_id) is not str:
            continue
        levels: dict[int, str] = {}
        for level in abstract.findall(_w("lvl")):
            raw_level = level.attrib.get(_w("ilvl"))
            fmt = level.find(_w("numFmt"))
            raw_format = None if fmt is None else fmt.attrib.get(_w("val"))
            if (
                type(raw_level) is str
                and raw_level.isdigit()
                and type(raw_format) is str
                and raw_format
            ):
                levels[int(raw_level)] = raw_format
        abstracts[abstract_id] = levels
    result: dict[tuple[str, int], str] = {}
    for number in root.findall(_w("num")):
        number_id = number.attrib.get(_w("numId"))
        abstract = number.find(_w("abstractNumId"))
        abstract_id = None if abstract is None else abstract.attrib.get(_w("val"))
        if type(number_id) is str and type(abstract_id) is str:
            for level, fmt in abstracts.get(abstract_id, {}).items():
                result[(number_id, level)] = fmt
    return result


def _notes(
    package: ValidatedDocxPackage,
    part: str,
    container: str,
    note_name: str,
    limits: DocxLimits,
    budget: _Budget,
) -> dict[str, str]:
    if part not in package.parts:
        return {}
    root = _root(package, part, limits, budget)
    if root.tag != _w(container):
        raise _fail()
    result: dict[str, str] = {}
    for note in root.findall(_w(note_name)):
        note_id = note.attrib.get(_w("id"))
        if type(note_id) is not str or not note_id.lstrip("-").isdigit():
            continue
        if int(note_id) <= 0:
            continue
        paragraphs: list[str] = []
        for paragraph in note.findall(f".//{_w('p')}"):
            text = "".join(node.text or "" for node in paragraph.iter(_w("t"))).strip()
            if text:
                paragraphs.append(text)
        text = "\n".join(paragraphs)
        if text:
            result[note_id] = text
    return result


def _classification(
    paragraph: ET.Element,
    styles: Mapping[str, int],
    numbering: Mapping[tuple[str, int], str],
) -> tuple[str, int | None]:
    props = paragraph.find(_w("pPr"))
    style_id = None
    list_level = None
    if props is not None:
        style = props.find(_w("pStyle"))
        style_id = None if style is None else style.attrib.get(_w("val"))
        number_props = props.find(_w("numPr"))
        if number_props is not None:
            level = number_props.find(_w("ilvl"))
            number = number_props.find(_w("numId"))
            raw_level = None if level is None else level.attrib.get(_w("val"))
            raw_number = None if number is None else number.attrib.get(_w("val"))
            if (
                type(raw_level) is str
                and raw_level.isdigit()
                and type(raw_number) is str
            ):
                list_level = int(raw_level)
                numbering.get((raw_number, list_level), "unknown")
    if list_level is not None:
        return "list", list_level
    if type(style_id) is str and style_id in styles:
        return "heading", styles[style_id]
    return "paragraph", None


def _oracle_color(value: str) -> bool:
    return (
        value == "auto"
        or _ORACLE_HEX_COLOR.fullmatch(value) is not None
        or _ORACLE_TOKEN.fullmatch(value) is not None
    )


def _oracle_font_name(value: str) -> bool:
    return (
        1 <= len(value) <= 1024
        and bool(value.strip())
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _oracle_run_properties(props: ET.Element) -> None:
    if props.tag != _w("rPr") or props.attrib or props.text:
        raise _fail()
    seen: set[str] = set()
    empty_tags = {
        _w(name)
        for name in ("bCs", "i", "iCs", "noProof", "snapToGrid", "webHidden")
    }
    boolean_tags = {_w("b"), _w("caps")}
    unsigned_tags = {_w("sz"), _w("szCs")}
    val = _w("val")
    for child in list(props):
        if child.tag in seen or list(child) or child.text or child.tail:
            raise _fail()
        seen.add(child.tag)
        attrs = child.attrib
        if child.tag in empty_tags:
            if attrs:
                raise _fail()
        elif child.tag in boolean_tags:
            if set(attrs) not in (set(), {val}) or (
                val in attrs and attrs[val] not in {"0", "1", "on", "off"}
            ):
                raise _fail()
        elif child.tag == _w("bdr"):
            allowed = {_w("color"), _w("frame"), _w("space"), _w("sz"), val}
            if not set(attrs).issubset(allowed):
                raise _fail()
            for name, value in attrs.items():
                if name == _w("color") and not _oracle_color(value):
                    raise _fail()
                if name == _w("frame") and value not in {"0", "1", "on", "off"}:
                    raise _fail()
                if name in {_w("space"), _w("sz")} and _ORACLE_UNSIGNED.fullmatch(value) is None:
                    raise _fail()
                if name == val and _ORACLE_TOKEN.fullmatch(value) is None:
                    raise _fail()
        elif child.tag == _w("color"):
            if set(attrs) != {val} or not _oracle_color(attrs[val]):
                raise _fail()
        elif child.tag == _w("lang"):
            allowed = {_w("eastAsia"), val}
            if not attrs or not set(attrs).issubset(allowed) or any(
                len(value) > 255 or _ORACLE_LANGUAGE.fullmatch(value) is None
                for value in attrs.values()
            ):
                raise _fail()
        elif child.tag == _w("rFonts"):
            names = {_w("ascii"), _w("cs"), _w("eastAsia"), _w("hAnsi")}
            themes = {
                _w("asciiTheme"),
                _w("cstheme"),
                _w("eastAsiaTheme"),
                _w("hAnsiTheme"),
            }
            if not attrs or not set(attrs).issubset(names | themes):
                raise _fail()
            for name, value in attrs.items():
                if name in names and not _oracle_font_name(value):
                    raise _fail()
                if name in themes and _ORACLE_TOKEN.fullmatch(value) is None:
                    raise _fail()
        elif child.tag == _w("rStyle"):
            if set(attrs) != {val} or _ORACLE_TOKEN.fullmatch(attrs[val]) is None:
                raise _fail()
        elif child.tag == _w("shd"):
            allowed = {_w("color"), _w("fill"), val}
            if not attrs or not set(attrs).issubset(allowed):
                raise _fail()
            for name, value in attrs.items():
                if name in {_w("color"), _w("fill")} and not _oracle_color(value):
                    raise _fail()
                if name == val and _ORACLE_TOKEN.fullmatch(value) is None:
                    raise _fail()
        elif child.tag == _w("spacing"):
            if set(attrs) != {val} or _ORACLE_SIGNED.fullmatch(attrs[val]) is None:
                raise _fail()
        elif child.tag in unsigned_tags:
            if set(attrs) != {val} or _ORACLE_UNSIGNED.fullmatch(attrs[val]) is None:
                raise _fail()
        elif child.tag == _w("u"):
            if set(attrs) != {val} or _ORACLE_TOKEN.fullmatch(attrs[val]) is None:
                raise _fail()
        else:
            raise _fail()


def _hyperlink(
    element: ET.Element, relationships: Mapping[str, Mapping[str, str]]
) -> tuple[str, str]:
    id_key = _q(R_NS, "id")
    anchor_key = _w("anchor")
    history_key = _w("history")
    tooltip_key = _w("tooltip")
    frame_key = _w("tgtFrame")
    if not set(element.attrib).issubset(
        {id_key, anchor_key, history_key, tooltip_key, frame_key}
    ):
        raise _fail()
    relation_id = element.attrib.get(id_key)
    anchor = element.attrib.get(anchor_key)
    history = element.attrib.get(history_key)
    tooltip = element.attrib.get(tooltip_key)
    frame = element.attrib.get(frame_key)
    if history is not None and history != HYPERLINK_HISTORY_TRUE:
        raise _fail()
    if relation_id is None and anchor is None:
        raise _fail()
    if tooltip is not None and (
        not 1 <= len(tooltip) <= 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in tooltip)
    ):
        raise _fail()
    if frame is not None and _ORACLE_TOKEN.fullmatch(frame) is None:
        raise _fail()
    if anchor is not None and (
        not anchor.strip()
        or len(anchor) > MAXIMUM_ANCHOR_CHARACTERS
        or any(ord(character) < 32 or ord(character) == 127 for character in anchor)
    ):
        raise _fail()
    event = "link"
    if relation_id is not None:
        relation = relationships.get(relation_id)
        if (
            relation is None
            or relation.get("Type") != HYPERLINK_REL
            or relation.get("TargetMode") != "External"
        ):
            raise _fail()
        event = "link:external"
    texts: list[str] = []
    state = "normal"
    result_visible = False
    xml_space = "{http://www.w3.org/XML/1998/namespace}space"
    for run in list(element):
        allowed_run_attrs = {_w("rsidR"), _w("rsidRPr")}
        if (
            run.tag != _w("r")
            or not set(run.attrib).issubset(allowed_run_attrs)
            or any(_ORACLE_RSID.fullmatch(value) is None for value in run.attrib.values())
        ):
            raise _fail()
        children = list(run)
        has_presentation = False
        if children and children[0].tag == _w("rPr"):
            props = children.pop(0)
            _oracle_run_properties(props)
            has_presentation = True
        if not children:
            if has_presentation:
                continue
            raise _fail()
        for token in children:
            if token.tag == _w("fldChar"):
                kind_attribute = _w("fldCharType")
                if (
                    set(token.attrib) != {kind_attribute}
                    or list(token)
                    or token.text
                    or token.tail
                ):
                    raise _fail()
                kind = token.attrib[kind_attribute]
                if state == "normal" and kind == "begin":
                    state = "instruction"
                elif state == "separator" and kind == "separate":
                    state = "result"
                    result_visible = False
                elif state == "result" and kind == "end" and result_visible:
                    state = "normal"
                else:
                    raise _fail()
                continue
            if token.tag == _w("instrText"):
                if (
                    state != "instruction"
                    or token.attrib != {xml_space: "preserve"}
                    or list(token)
                    or token.tail
                    or type(token.text) is not str
                    or len(token.text) > 1024
                    or _ORACLE_PAGEREF.fullmatch(token.text) is None
                ):
                    raise _fail()
                state = "separator"
                continue
            if state in {"instruction", "separator"}:
                raise _fail()
            if token.tag == _w("t"):
                if (
                    list(token)
                    or set(token.attrib) not in (set(), {xml_space})
                    or (
                        xml_space in token.attrib
                        and token.attrib[xml_space] != "preserve"
                    )
                ):
                    raise _fail()
                value = token.text or ""
                texts.append(value)
                if state == "result" and value.strip():
                    result_visible = True
                continue
            if token.tag in {_w("tab"), _w("lastRenderedPageBreak")}:
                if token.attrib or list(token) or token.text or token.tail:
                    raise _fail()
                if token.tag == _w("tab"):
                    texts.append("\t")
                continue
            raise _fail()
    if state != "normal":
        raise _fail()
    text = "".join(texts).strip()
    if not text:
        raise _fail()
    return text, event


def _drawing(
    element: ET.Element,
    relationships: Mapping[str, Mapping[str, str]],
    package: ValidatedDocxPackage,
    limits: DocxLimits,
) -> tuple[str, str]:
    graphic_data = element.findall(f".//{_q(A_NS, 'graphicData')}")
    if len(graphic_data) != 1 or set(graphic_data[0].attrib) != {"uri"}:
        raise _fail()
    role = graphic_data[0].attrib["uri"]
    if role == DIAGRAM_GRAPHIC_URI:
        children = list(graphic_data[0])
        if children:
            if (
                len(children) != 1
                or children[0].tag != _q(DGM_NS, "relIds")
                or list(children[0])
                or children[0].text
            ):
                raise _fail()
            expected = {
                _q(R_NS, name): relation_type
                for name, relation_type in DIAGRAM_RELATIONSHIPS
            }
            if set(children[0].attrib) != set(expected):
                raise _fail()
            for attribute, relation_type in expected.items():
                relation_id = children[0].attrib[attribute]
                relation = relationships.get(relation_id)
                if (
                    not relation_id
                    or relation is None
                    or relation.get("Type") != relation_type
                    or relation.get("TargetMode") != ""
                    or relation.get("Target") not in package.parts
                ):
                    raise _fail()
        return "inventory", ""
    if role == WORDPROCESSING_SHAPE_GRAPHIC_URI:
        children = list(graphic_data[0])
        if len(children) != 1 or children[0].tag != _q(WPS_NS, "wsp"):
            raise _fail()
        contents = children[0].findall(f".//{_w('txbxContent')}")
        if len(contents) != 1:
            raise _fail()
        paragraphs: list[str] = []
        for paragraph in list(contents[0]):
            if paragraph.tag != _w("p"):
                raise _fail()
            parts: list[str] = []
            for run in list(paragraph):
                if run.tag == _w("pPr"):
                    continue
                if run.tag != _w("r"):
                    raise _fail()
                for token in list(run):
                    if token.tag == _w("rPr"):
                        continue
                    if token.tag != _w("t") or list(token):
                        raise _fail()
                    parts.append(token.text or "")
            text = "".join(parts).strip()
            if text:
                paragraphs.append(text)
        text = "\n".join(paragraphs)
        if len(text) > limits.maximum_text_characters:
            raise _fail()
        return ("inline" if text else "inventory"), text
    if role != PICTURE_GRAPHIC_URI:
        raise _fail()
    blips = graphic_data[0].findall(f".//{_q(A_NS, 'blip')}")
    if len(blips) != 1:
        raise _fail()
    embed_key = _q(R_NS, "embed")
    link_key = _q(R_NS, "link")
    cstate_key = "cstate"
    blip = blips[0]
    if (
        not set(blip.attrib).issubset({embed_key, link_key, cstate_key})
        or (cstate_key in blip.attrib and blip.attrib[cstate_key] != "print")
    ):
        raise _fail()
    embed_id = blip.attrib.get(embed_key)
    link_id = blip.attrib.get(link_key)
    if embed_id is None and link_id is None:
        raise _fail()
    if embed_id is not None:
        relation = relationships.get(embed_id)
        if (
            relation is None
            or relation.get("Type") != IMAGE_REL
            or relation.get("TargetMode") != ""
            or relation.get("Target") not in package.parts
        ):
            raise _fail()
    if link_id is not None:
        relation = relationships.get(link_id)
        if (
            relation is None
            or relation.get("Type") != IMAGE_REL
            or relation.get("TargetMode") != "External"
            or relation.get("Target") != ""
        ):
            raise _fail()
    return "image", ""


def _cell(table_cell: ET.Element, package: ValidatedDocxPackage, limits: DocxLimits) -> tuple[str, list[_TableValue]]:
    text_parts: list[str] = []
    nested: list[_TableValue] = []
    for child in list(table_cell):
        if child.tag == _w("p"):
            text = "".join(node.text or "" for node in child.iter(_w("t"))).strip()
            if text:
                text_parts.append(text)
        elif child.tag == _w("tbl"):
            value = _table(child, package, limits)
            nested.append(value)
            if value.text:
                text_parts.append(value.text)
    return "\n".join(text_parts), nested


def _table(table: ET.Element, package: ValidatedDocxPackage, limits: DocxLimits) -> _TableValue:
    rows = 0
    visible_cells = 0
    text_rows: list[str] = []
    nested_tables: list[_TableValue] = []
    open_merges: dict[int, object] = {}
    for row in table.findall(_w("tr")):
        rows += 1
        row_text: list[str] = []
        next_merges: dict[int, object] = {}
        column = 0
        for cell in row.findall(_w("tc")):
            props = cell.find(_w("tcPr"))
            col_span = 1
            continuation = False
            restart = False
            if props is not None:
                grid = props.find(_w("gridSpan"))
                raw_span = None if grid is None else grid.attrib.get(_w("val"))
                if type(raw_span) is str and raw_span.isdigit():
                    col_span = max(1, int(raw_span))
                merge = props.find(_w("vMerge"))
                if merge is not None:
                    if merge.attrib.get(_w("val"), "") == "restart":
                        restart = True
                    else:
                        continuation = True
            text, nested = _cell(cell, package, limits)
            if continuation and column in open_merges:
                for position in range(column, column + col_span):
                    next_merges[position] = open_merges[column]
                column += col_span
                continue
            visible_cells += 1
            nested_tables.extend(nested)
            if text:
                row_text.append(text)
            marker = object()
            if restart:
                for position in range(column, column + col_span):
                    next_merges[position] = marker
            column += col_span
        if row_text:
            text_rows.append("\t".join(row_text))
        open_merges = next_merges
    return _TableValue("\n".join(text_rows), rows, visible_cells, nested_tables)


def _append_table(value: _TableValue, counts: dict[str, int], events: list[str]) -> None:
    counts["tables"] += 1
    counts["rows"] += value.rows
    counts["cells"] += value.cells
    events.append("table")
    for nested in value.nested:
        _append_table(nested, counts, events)


def derive_docx_semantic_expectation(
    package: ValidatedDocxPackage, limits: DocxLimits
) -> DocxSemanticExpectation:
    """Derive benchmark-visible semantics without calling production parser helpers."""

    if type(package) is not ValidatedDocxPackage or type(limits) is not DocxLimits:
        raise _fail()
    budget = _Budget()
    styles = _style_levels(package, limits, budget)
    numbering = _numbering(package, limits, budget)
    footnotes = _notes(package, "word/footnotes.xml", "footnotes", "footnote", limits, budget)
    endnotes = _notes(package, "word/endnotes.xml", "endnotes", "endnote", limits, budget)
    relationships = _relationship_map(package, limits)
    root = _root(package, "word/document.xml", limits, budget)
    body = root.find(_w("body"))
    if body is None:
        raise _fail()
    counts = {key: 0 for key in COUNT_KEYS}
    texts: list[str] = []
    events: list[str] = []

    def emit(kind: str, level: int | None, text: str) -> None:
        normalized = _normalized(text)
        if not normalized:
            return
        if kind == "heading":
            counts["headings"] += 1
            events.append(f"heading:{level}")
        elif kind == "list":
            counts["lists"] += 1
            events.append(f"list:{level}")
        else:
            counts["paragraphs"] += 1
            events.append("paragraph")
        texts.append(normalized)

    for child in list(body):
        if child.tag == _w("p"):
            kind, level = _classification(child, styles, numbering)
            plain: list[str] = []

            def flush() -> None:
                emit(kind, level, "".join(plain).strip())
                plain.clear()

            for run in list(child):
                if run.tag == _w("pPr"):
                    continue
                if run.tag == _w("hyperlink"):
                    flush()
                    text, event = _hyperlink(run, relationships)
                    counts["links"] += 1
                    texts.append(_normalized(text))
                    events.append(event)
                    continue
                if run.tag != _w("r"):
                    continue
                for token in list(run):
                    if token.tag == _w("t"):
                        plain.append(token.text or "")
                    elif token.tag in {_w("footnoteReference"), _w("endnoteReference")}:
                        flush()
                        note_id = token.attrib.get(_w("id"))
                        note_kind = "footnotes" if token.tag == _w("footnoteReference") else "endnotes"
                        notes = footnotes if note_kind == "footnotes" else endnotes
                        if type(note_id) is not str or note_id not in notes:
                            raise _fail()
                        counts[note_kind] += 1
                        events.append("footnote" if note_kind == "footnotes" else "endnote")
                        texts.append(_normalized(notes[note_id]))
                    elif token.tag == _w("drawing"):
                        drawing_kind, drawing_text = _drawing(
                            token, relationships, package, limits
                        )
                        if drawing_kind == "inline":
                            plain.append(drawing_text)
                        elif drawing_kind == "image":
                            flush()
                            counts["drawings"] += 1
                            counts["media"] += 1
                            events.append("image")
            flush()
        elif child.tag == _w("tbl"):
            table = _table(child, package, limits)
            _append_table(table, counts, events)
            normalized = _normalized(table.text)
            if normalized:
                texts.append(normalized)
    if len(events) > limits.maximum_blocks:
        raise _fail()
    owned_counts = {key: counts[key] for key in COUNT_KEYS}
    owned_texts = tuple(texts)
    owned_events = tuple(events)
    return DocxSemanticExpectation(
        counts=owned_counts,
        text_tokens=owned_texts,
        structural_events=owned_events,
        normalized_text_digest=canonical_digest(list(owned_texts)),
        structural_event_digest=canonical_digest(list(owned_events)),
    )
