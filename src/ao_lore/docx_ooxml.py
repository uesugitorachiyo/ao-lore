"""Fail-closed validation for bounded native DOCX OOXML packages."""

from __future__ import annotations

import copy
import hmac
import os
import posixpath
import re
import stat
import struct
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from types import MappingProxyType
from typing import Any, Mapping

from .benchmark import canonical_digest
from .docx_semantic_contract import (
    A_NS as _A_NS,
    DGM_NS as _DGM_NS,
    DIAGRAM_RELATIONSHIPS,
    DIAGRAM_GRAPHIC_URI,
    HYPERLINK_HISTORY_TRUE,
    HYPERLINK_REL as _HYPERLINK_REL,
    IMAGE_REL as _IMAGE_REL,
    MAXIMUM_ANCHOR_CHARACTERS,
    PICTURE_GRAPHIC_URI,
    REL_NS as _REL_NS,
    R_NS as _R_NS,
    WP_NS as _WP_NS,
    WORDPROCESSING_SHAPE_GRAPHIC_URI,
    W_NS as _W_NS,
    WPS_NS as _WPS_NS,
)

_EOCD = b"PK\x05\x06"
_CENTRAL = b"PK\x01\x02"
_LOCAL = b"PK\x03\x04"
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MAX_DOCX_BYTES = 50 * 1024 * 1024
_MAX_RELATIONSHIP_TARGET_CHARACTERS = 64 * 1024
_MAX_EXTERNAL_CID_CHARACTERS = 512
_ZIP_DEFLATE_OPTION_FLAGS = 0x0006
_ZIP_ALLOWED_GENERAL_PURPOSE_FLAGS = 0x0800 | _ZIP_DEFLATE_OPTION_FLAGS
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_EP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_RELATIONSHIP_CT = "application/vnd.openxmlformats-package.relationships+xml"
_DOCUMENT_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_STYLES_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"
_NUMBERING_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"
_FOOTNOTES_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"
_ENDNOTES_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"
_CORE_CT = "application/vnd.openxmlformats-package.core-properties+xml"
_APP_CT = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
_HEADER_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"
_FOOTER_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"
_SETTINGS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"
_FONT_TABLE_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"
_WEB_SETTINGS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.webSettings+xml"
_THEME_CT = "application/vnd.openxmlformats-officedocument.theme+xml"
_CUSTOM_XML_PROPS_CT = "application/vnd.openxmlformats-officedocument.customXmlProperties+xml"
_STYLES_WITH_EFFECTS_CT = "application/vnd.ms-word.stylesWithEffects+xml"
_CUSTOM_PROPERTIES_CT = "application/vnd.openxmlformats-officedocument.custom-properties+xml"
_GLOSSARY_DOCUMENT_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.glossary+xml"
_COMMENTS_CT = "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"
_COMMENTS_EXTENDED_CT = "application/vnd.ms-word.commentsExtended+xml"
_COMMENTS_IDS_CT = "application/vnd.ms-word.commentsIds+xml"
_COMMENTS_EXTENSIBLE_CT = "application/vnd.ms-word.commentsExtensible+xml"
_PEOPLE_CT = "application/vnd.ms-word.people+xml"
_OBFUSCATED_FONT_CT = "application/vnd.openxmlformats-officedocument.obfuscatedFont"
_DIAGRAM_CTS = {
    "data": "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
    "layout": "application/vnd.openxmlformats-officedocument.drawingml.diagramLayout+xml",
    "quickStyle": "application/vnd.openxmlformats-officedocument.drawingml.diagramStyle+xml",
    "colors": "application/vnd.openxmlformats-officedocument.drawingml.diagramColors+xml",
    "drawing": "application/vnd.ms-office.drawingml.diagramDrawing+xml",
}
_UTF8_BOM = b"\xef\xbb\xbf"
_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_STYLES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
_NUMBERING_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
_FOOTNOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
_ENDNOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"
_HEADER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
_FOOTER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer"
_SETTINGS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
_FONT_TABLE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable"
_WEB_SETTINGS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/webSettings"
_THEME_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme"
_CUSTOM_XML_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml"
_CUSTOM_XML_PROPS_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXmlProps"
)
_STYLES_WITH_EFFECTS_REL = "http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects"
_CUSTOM_PROPERTIES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
_THUMBNAIL_REL = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
_GLOSSARY_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/glossaryDocument"
_COMMENTS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
_COMMENTS_EXTENDED_REL = "http://schemas.microsoft.com/office/2011/relationships/commentsExtended"
_COMMENTS_IDS_REL = "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds"
_COMMENTS_EXTENSIBLE_REL = "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible"
_PEOPLE_REL = "http://schemas.microsoft.com/office/2011/relationships/people"
_FONT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/font"
_DIAGRAM_RELS = {
    "data": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData",
    "layout": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramLayout",
    "quickStyle": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramQuickStyle",
    "colors": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramColors",
}
_DIAGRAM_DRAWING_REL = "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing"
_ALLOWED_ROOT_RELS = frozenset(
    {
        _OFFICE_DOCUMENT_REL,
        "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
        _CUSTOM_PROPERTIES_REL,
        _THUMBNAIL_REL,
    }
)
_ALLOWED_DOC_RELS = frozenset(
    {
        _HYPERLINK_REL,
        _IMAGE_REL,
        _STYLES_REL,
        _NUMBERING_REL,
        _FOOTNOTES_REL,
        _ENDNOTES_REL,
        _HEADER_REL,
        _FOOTER_REL,
        _SETTINGS_REL,
        _FONT_TABLE_REL,
        _WEB_SETTINGS_REL,
        _THEME_REL,
        _CUSTOM_XML_REL,
        _STYLES_WITH_EFFECTS_REL,
        _GLOSSARY_REL,
        _COMMENTS_REL,
        _COMMENTS_EXTENDED_REL,
        _COMMENTS_IDS_REL,
        _COMMENTS_EXTENSIBLE_REL,
        _PEOPLE_REL,
        _DIAGRAM_DRAWING_REL,
        *_DIAGRAM_RELS.values(),
    }
)
_ALLOWED_AUXILIARY_WORD_RELS = frozenset({_HYPERLINK_REL, _IMAGE_REL})
_REJECTED_REL_TYPES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/control",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/controlProperties",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package",
        "http://schemas.microsoft.com/office/2006/relationships/activeXControlBinary",
    }
)
_ALLOWED_PI_PREFIX = b"<?xml "
_CONTROL_BYTES = frozenset(range(0, 32)) | {127}
_REJECTED_WORDPROCESSINGML_TAGS = frozenset(
    {
        f"{{{_W_NS}}}altChunk",
    }
)
_HEADER_PART_RE = re.compile(r"^word/header([1-9][0-9]*)\.xml$")
_FOOTER_PART_RE = re.compile(r"^word/footer([1-9][0-9]*)\.xml$")
_THEME_PART_RE = re.compile(r"^word/theme/theme([1-9][0-9]*)\.xml$")
_AUXILIARY_RELS_RE = re.compile(
    r"^word/_rels/(?:(?:header|footer)[1-9][0-9]*|settings|fontTable|webSettings|numbering|footnotes|endnotes)\.xml\.rels$"
)
_THEME_RELS_RE = re.compile(r"^word/theme/_rels/theme[1-9][0-9]*\.xml\.rels$")
_INERT_WORD_RELS_RE = re.compile(
    r"^word/_rels/(?:stylesWithEffects|comments|commentsExtended|commentsIds|commentsExtensible|people)\.xml\.rels$"
)
_CUSTOM_XML_ITEM_RE = re.compile(r"^customXml/item([1-9][0-9]*)\.xml$")
_CUSTOM_XML_PROPS_RE = re.compile(r"^customXml/itemProps([1-9][0-9]*)\.xml$")
_CUSTOM_XML_RELS_RE = re.compile(
    r"^customXml/_rels/item([1-9][0-9]*)\.xml\.rels$"
)
_CUSTOM_XML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
_CUSTOM_PROPERTIES_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_DSP_NS = "http://schemas.microsoft.com/office/drawing/2008/diagram"
_W15_NS = "http://schemas.microsoft.com/office/word/2012/wordml"
_W16CID_NS = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
_W16CEX_NS = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
_GLOSSARY_ROLE_RE = re.compile(
    r"^word/glossary/(document|styles|stylesWithEffects|settings|fontTable|webSettings)\.xml$"
)
_GLOSSARY_RELS_RE = re.compile(
    r"^word/glossary/_rels/(document|styles|stylesWithEffects|settings|fontTable|webSettings)\.xml\.rels$"
)
_DIAGRAM_PART_RE = re.compile(r"^word/diagrams/(data|layout|quickStyle|colors|drawing)([1-9][0-9]*)\.xml$")
_DIAGRAM_RELS_RE = re.compile(r"^word/diagrams/_rels/(data|layout|quickStyle|colors|drawing)([1-9][0-9]*)\.xml\.rels$")
_FONT_PART_RE = re.compile(r"^word/fonts/font([1-9][0-9]*)\.odttf$")
_EXTERNAL_CID_RE = re.compile(
    r"^cid:[A-Za-z0-9][A-Za-z0-9_%+\-]{0,62}"
    r"(?:\.[A-Za-z0-9][A-Za-z0-9_%+\-]{0,62})*@"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$"
)
_HYPERLINK_RSID_RE = re.compile(r"[0-9A-F]{8}\Z")
_HYPERLINK_TOKEN_RE = re.compile(r"[A-Za-z0-9_.:-]{1,1024}\Z")
_HYPERLINK_UNSIGNED_RE = re.compile(r"(?:0|[1-9][0-9]{0,9})\Z")
_HYPERLINK_SIGNED_RE = re.compile(r"-?(?:0|[1-9][0-9]{0,9})\Z")
_HYPERLINK_HEX_COLOR_RE = re.compile(r"(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})\Z")
_HYPERLINK_LANGUAGE_RE = re.compile(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9]{1,8})*\Z")
_HYPERLINK_PAGEREF_RE = re.compile(
    r" *PAGEREF ([A-Za-z_][A-Za-z0-9_]{0,1011}) \\h *\Z"
)
_COMMENTS_PARTS = {
    "word/comments.xml": (frozenset({_COMMENTS_CT}), f"{{{_W_NS}}}comments", _COMMENTS_REL),
    "word/commentsExtended.xml": (
        frozenset(
            {
                _COMMENTS_EXTENDED_CT,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml",
            }
        ),
        f"{{{_W15_NS}}}commentsEx",
        _COMMENTS_EXTENDED_REL,
    ),
    "word/commentsIds.xml": (
        frozenset(
            {
                _COMMENTS_IDS_CT,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml",
            }
        ),
        f"{{{_W16CID_NS}}}commentsIds",
        _COMMENTS_IDS_REL,
    ),
    "word/commentsExtensible.xml": (
        frozenset(
            {
                _COMMENTS_EXTENSIBLE_CT,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml",
            }
        ),
        f"{{{_W16CEX_NS}}}commentsExtensible",
        _COMMENTS_EXTENSIBLE_REL,
    ),
    "word/people.xml": (
        frozenset(
            {
                _PEOPLE_CT,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml",
            }
        ),
        f"{{{_W15_NS}}}people",
        _PEOPLE_REL,
    ),
}
DOCX_MIME = _DOCX_MIME
DOCX_PARSER_ID = "native-docx-ooxml"
DOCX_PARSER_VERSION = "1.0.0"
DOCX_BENCHMARK_SCHEMA_VERSION = "ao.lore.docx-benchmark-result.v0.1"
DOCX_DOCUMENT_COUNT = 100
DOCX_ACTIVE_CONTENT_REJECTION_COUNT = 11
DOCX_INVALID_PACKAGE_REJECTION_COUNT = 1
DOCX_EXPECTED_REJECTION_COUNT = 12
DOCX_ACCEPTED_DOCUMENT_COUNT = 88
DOCX_STABLE_FAILURE_CATEGORIES = frozenset(
    {"invalid_package", "unsupported_active_content"}
)
DOCX_ACTIVE_CONTENT_REJECTION_CODE = "unsupported_active_content"
DOCX_INVALID_PACKAGE_REJECTION_CODE = "invalid_package"
DOCX_FLOORS = MappingProxyType(
    {
        "text_fidelity": 0.5,
        "structural_fidelity": 0.5,
        "source_location_fidelity": 0.5,
        "expected_outcome_accuracy": 1.0,
    }
)


class DocxPackageError(RuntimeError):
    """Raised at the stable OOXML package boundary."""


@dataclass(frozen=True)
class DocxLimits:
    maximum_archive_bytes: int = 50 * 1024 * 1024
    maximum_entries: int = 4096
    maximum_member_bytes: int = 32 * 1024 * 1024
    maximum_expanded_bytes: int = 200 * 1024 * 1024
    maximum_compression_ratio: int = 200
    maximum_xml_depth: int = 128
    maximum_xml_elements: int = 1_000_000
    maximum_text_characters: int = 24 * 1024 * 1024
    maximum_aggregate_text_characters: int = 28 * 1024 * 1024
    maximum_relationships: int = 100_000
    maximum_blocks: int = 200_000


@dataclass(frozen=True)
class ValidatedDocxPackage:
    parts: Mapping[str, bytes]
    content_types: Mapping[str, str]
    relationships: Mapping[str, tuple[Mapping[str, str], ...]]
    inventory_digest: str
    expanded_bytes: int


@dataclass(frozen=True)
class _XmlStats:
    elements: int
    text_characters: int
    blocks: int


def _invalid(cause: BaseException | None = None) -> DocxPackageError:
    error = DocxPackageError("DOCX package is invalid")
    error.rejection_code = DOCX_INVALID_PACKAGE_REJECTION_CODE
    if cause is not None:
        error.__cause__ = cause
    return error


def _unsupported_active_content() -> DocxPackageError:
    error = DocxPackageError("DOCX package is invalid")
    error.rejection_code = DOCX_ACTIVE_CONTENT_REJECTION_CODE
    return error


def _require_limits(limits: DocxLimits) -> DocxLimits:
    if type(limits) is not DocxLimits:
        raise _invalid()
    values = (
        limits.maximum_archive_bytes,
        limits.maximum_entries,
        limits.maximum_member_bytes,
        limits.maximum_expanded_bytes,
        limits.maximum_compression_ratio,
        limits.maximum_xml_depth,
        limits.maximum_xml_elements,
        limits.maximum_text_characters,
        limits.maximum_aggregate_text_characters,
        limits.maximum_relationships,
        limits.maximum_blocks,
    )
    if any(type(value) is not int or value <= 0 for value in values):
        raise _invalid()
    if (
        limits.maximum_archive_bytes > _MAX_DOCX_BYTES
        or limits.maximum_member_bytes > limits.maximum_expanded_bytes
        or limits.maximum_archive_bytes > limits.maximum_expanded_bytes
    ):
        raise _invalid()
    return limits


def _decode_name(raw: bytes, flags: int) -> str:
    encoding = "utf-8" if flags & 0x0800 else "cp437"
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise _invalid(exc) from exc


def _normalize_name(name: str) -> str:
    if (
        type(name) is not str
        or not name
        or "\\" in name
        or name.startswith("/")
        or name.endswith("/")
        or any(ord(character) in _CONTROL_BYTES for character in name)
    ):
        raise _invalid()
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise _invalid()
    normalized = posixpath.normpath(name)
    if normalized != name or normalized.startswith("../") or normalized == "..":
        raise _invalid()
    return normalized


def _resolve_target(source_part: str, target: str) -> str:
    if (
        type(target) is not str
        or not target
        or "\\" in target
        or target.startswith("/")
        or target.endswith("/")
        or any(ord(character) in _CONTROL_BYTES for character in target)
        or any(part in {"", "."} for part in target.split("/"))
        or posixpath.normpath(target) != target
    ):
        raise _invalid()
    base = posixpath.dirname(source_part)
    if base:
        resolved = posixpath.normpath(posixpath.join(base, target))
    else:
        resolved = posixpath.normpath(target)
    if (
        resolved.startswith("../")
        or resolved == ".."
        or resolved.startswith("/")
        or "\\" in resolved
        or any(segment in {"", ".", ".."} for segment in resolved.split("/"))
    ):
        raise _invalid()
    return _normalize_name(resolved)


def _resolve_root_target(target: str) -> str:
    if (
        type(target) is not str
        or "?" in target
        or "#" in target
        or "://" in target
        or target.startswith("//")
    ):
        raise _invalid()
    relative = target[1:] if target.startswith("/") else target
    return _resolve_target("", relative)


def _xml_root(
    body: bytes,
    limits: DocxLimits,
    *,
    allow_mso_content_type_pi: bool = False,
) -> tuple[ET.Element, _XmlStats]:
    if type(body) is not bytes:
        raise _invalid()
    if body.startswith((b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
        raise _invalid()
    if b"\x00" in body:
        raise _invalid()
    xml_bytes = body[len(_UTF8_BOM) :] if body.startswith(_UTF8_BOM) else body
    try:
        xml_text = xml_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid(exc) from exc
    stripped = xml_text.lstrip()
    if "<!DOCTYPE" in stripped or "<!ENTITY" in stripped:
        raise _invalid()
    remainder = stripped
    if remainder.startswith(_ALLOWED_PI_PREFIX.decode("ascii")):
        declaration_end = stripped.find("?>")
        if declaration_end < 0:
            raise _invalid()
        remainder = stripped[declaration_end + 2 :].lstrip()
    if allow_mso_content_type_pi and remainder.startswith("<?mso-contentType?>"):
        remainder = remainder[len("<?mso-contentType?>") :].lstrip()
    if "<?" in remainder:
        raise _invalid()
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise _invalid(exc) from exc
    nodes = [(root, 1)]
    elements = 0
    text_characters = 0
    maximum_depth = 0
    blocks = 0
    while nodes:
        node, depth = nodes.pop()
        elements += 1
        text_characters += (
            len(node.text or "")
            + len(node.tail or "")
            + sum(len(name) + len(value) for name, value in node.attrib.items())
        )
        maximum_depth = max(maximum_depth, depth)
        for child in reversed(list(node)):
            nodes.append((child, depth + 1))
        if node.tag in {f"{{{_W_NS}}}p", f"{{{_W_NS}}}tbl"}:
            blocks += 1
        if (
            elements > limits.maximum_xml_elements
            or text_characters > limits.maximum_text_characters
            or blocks > limits.maximum_blocks
        ):
            raise _invalid()
    if maximum_depth > limits.maximum_xml_depth:
        raise _invalid()
    return root, _XmlStats(elements=elements, text_characters=text_characters, blocks=blocks)


def _parse_content_types(
    body: bytes,
    limits: DocxLimits,
) -> tuple[dict[str, str], dict[str, str], _XmlStats]:
    root, stats = _xml_root(body, limits)
    if root.tag != f"{{{_CT_NS}}}Types":
        raise _invalid()
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    override_casefolds: set[str] = set()
    for child in root:
        if child.tag == f"{{{_CT_NS}}}Default":
            extension = child.attrib.get("Extension")
            content_type = child.attrib.get("ContentType")
            folded_extension = extension.casefold() if type(extension) is str else None
            if (
                set(child.attrib) != {"Extension", "ContentType"}
                or type(extension) is not str
                or type(content_type) is not str
                or not extension
                or not content_type
                or folded_extension in defaults
            ):
                raise _invalid()
            defaults[folded_extension] = content_type
        elif child.tag == f"{{{_CT_NS}}}Override":
            part_name = child.attrib.get("PartName")
            content_type = child.attrib.get("ContentType")
            if (
                set(child.attrib) != {"PartName", "ContentType"}
                or type(part_name) is not str
                or type(content_type) is not str
                or not part_name.startswith("/")
            ):
                raise _invalid()
            normalized = _normalize_name(part_name[1:])
            if normalized in overrides or normalized.casefold() in override_casefolds:
                raise _invalid()
            overrides[normalized] = content_type
            override_casefolds.add(normalized.casefold())
        else:
            raise _invalid()
    return defaults, overrides, stats


def _relationship_record(source_part: str, child: ET.Element) -> dict[str, str]:
    allowed = {"Id", "Type", "Target"}
    if "TargetMode" in child.attrib:
        allowed.add("TargetMode")
    if child.tag != f"{{{_REL_NS}}}Relationship" or set(child.attrib) != allowed:
        raise _invalid()
    relation_id = child.attrib.get("Id")
    relation_type = child.attrib.get("Type")
    target = child.attrib.get("Target")
    if type(relation_id) is not str or type(relation_type) is not str or type(target) is not str:
        raise _invalid()
    if (
        not relation_id
        or not relation_type
        or not target
        or len(target) > _MAX_RELATIONSHIP_TARGET_CHARACTERS
    ):
        raise _invalid()
    target_mode = child.attrib.get("TargetMode", "")
    if target_mode not in {"", "External"}:
        raise _invalid()
    if relation_type in _REJECTED_REL_TYPES:
        raise _unsupported_active_content()
    if source_part == "/":
        if relation_type not in _ALLOWED_ROOT_RELS or target_mode:
            raise _invalid()
        resolved = _resolve_root_target(target)
        return {
            "Id": relation_id,
            "Type": relation_type,
            "Target": resolved,
            "TargetMode": "",
        }
    if source_part == "word/document.xml":
        allowed_relationships = _ALLOWED_DOC_RELS
    elif (
        _HEADER_PART_RE.fullmatch(source_part)
        or _FOOTER_PART_RE.fullmatch(source_part)
        or source_part
        in {"word/numbering.xml", "word/footnotes.xml", "word/endnotes.xml"}
    ):
        allowed_relationships = _ALLOWED_AUXILIARY_WORD_RELS
    elif source_part == "word/glossary/document.xml":
        allowed_relationships = frozenset(
            {
                _HYPERLINK_REL,
                _IMAGE_REL,
                _STYLES_REL,
                _SETTINGS_REL,
                _FONT_TABLE_REL,
                _WEB_SETTINGS_REL,
                _STYLES_WITH_EFFECTS_REL,
            }
        )
    elif source_part in {"word/fontTable.xml", "word/glossary/fontTable.xml"}:
        allowed_relationships = frozenset({_FONT_REL})
    elif (diagram_match := _DIAGRAM_PART_RE.fullmatch(source_part)) is not None:
        allowed_relationships = (
            frozenset({_HYPERLINK_REL, _IMAGE_REL})
            if diagram_match.group(1) == "data"
            else frozenset()
        )
    elif _CUSTOM_XML_ITEM_RE.fullmatch(source_part):
        allowed_relationships = frozenset({_CUSTOM_XML_PROPS_REL})
    else:
        allowed_relationships = frozenset()
    if relation_type not in allowed_relationships:
        raise _invalid()
    if relation_type == _HYPERLINK_REL:
        if target_mode != "External":
            raise _invalid()
        return {
            "Id": relation_id,
            "Type": relation_type,
            "Target": target,
            "TargetMode": "External",
        }
    if relation_type == _IMAGE_REL and target_mode == "External":
        if (
            len(target) > _MAX_EXTERNAL_CID_CHARACTERS
            or _EXTERNAL_CID_RE.fullmatch(target) is None
        ):
            raise _invalid()
        return {
            "Id": relation_id,
            "Type": relation_type,
            "Target": "",
            "TargetMode": "External",
        }
    if target_mode:
        raise _invalid()
    resolved = _resolve_target(source_part, target)
    return {
        "Id": relation_id,
        "Type": relation_type,
        "Target": resolved,
        "TargetMode": "",
    }


def _relationship_source_part(name: str) -> str:
    if name == "_rels/.rels":
        return "/"
    match = re.fullmatch(r"(.+)/_rels/([^/]+)\.rels", name)
    if match is None:
        raise _invalid()
    return f"{match.group(1)}/{match.group(2)}"


def _pre_scan_active_relationships(parts: Mapping[str, bytes], limits: DocxLimits) -> None:
    for name in sorted(part for part in parts if part.endswith(".rels")):
        root, _ = _xml_root(parts[name], limits)
        if root.tag != f"{{{_REL_NS}}}Relationships":
            raise _invalid()
        for child in root:
            if child.tag != f"{{{_REL_NS}}}Relationship":
                raise _invalid()
            relation_type = child.attrib.get("Type")
            if type(relation_type) is not str or not relation_type:
                raise _invalid()
            if relation_type in _REJECTED_REL_TYPES:
                raise _unsupported_active_content()


def _pre_scan_active_parts(parts: Mapping[str, bytes], limits: DocxLimits) -> None:
    for name in parts:
        folded = name.casefold()
        if (
            folded.startswith("word/embeddings/")
            or folded.startswith("word/activex/")
            or "vbaproject" in folded
            or "oleobject" in folded
            or "afchunk" in folded
        ):
            raise _unsupported_active_content()
    for name in sorted(
        part for part in parts if part.startswith("word/") and part.endswith(".xml")
    ):
        root, _ = _xml_root(parts[name], limits)
        if any(node.tag in _REJECTED_WORDPROCESSINGML_TAGS for node in root.iter()):
            raise _unsupported_active_content()


def _parse_relationships(
    source_part: str,
    body: bytes,
    limits: DocxLimits,
) -> tuple[tuple[dict[str, str], ...], _XmlStats]:
    root, stats = _xml_root(body, limits)
    if root.tag != f"{{{_REL_NS}}}Relationships":
        raise _invalid()
    records: list[dict[str, str]] = []
    ids: set[str] = set()
    seen_targets: set[tuple[str, str, str]] = set()
    for child in root:
        record = _relationship_record(source_part, child)
        identity = record["Id"]
        if identity in ids:
            raise _invalid()
        ids.add(identity)
        if record["Type"] != _HYPERLINK_REL and record["TargetMode"] != "External":
            target_key = (record["Type"], record["TargetMode"], record["Target"])
            if target_key in seen_targets:
                raise _invalid()
            seen_targets.add(target_key)
        records.append(record)
    if len(records) > limits.maximum_relationships:
        raise _invalid()
    return tuple(records), stats


def _validate_external_image_relationships(
    parts: Mapping[str, bytes],
    relationships: Mapping[str, tuple[dict[str, str], ...]],
    limits: DocxLimits,
) -> None:
    link_attribute = f"{{{_R_NS}}}link"
    embed_attribute = f"{{{_R_NS}}}embed"
    blip_tag = f"{{{_A_NS}}}blip"
    for source_part, records in relationships.items():
        external_ids = {
            record["Id"]
            for record in records
            if record["Type"] == _IMAGE_REL
            and record["TargetMode"] == "External"
        }
        if not external_ids:
            continue
        body = parts.get(source_part)
        if body is None:
            raise _invalid()
        root, _ = _xml_root(body, limits)
        link_ids: set[str] = set()
        embed_ids: set[str] = set()
        for node in root.iter():
            link_id = node.attrib.get(link_attribute)
            embed_id = node.attrib.get(embed_attribute)
            if type(link_id) is str:
                if node.tag != blip_tag or not link_id:
                    raise _invalid()
                link_ids.add(link_id)
            if type(embed_id) is str and embed_id:
                embed_ids.add(embed_id)
        if not external_ids.issubset(link_ids) or external_ids & embed_ids:
            raise _invalid()
        external_records = {
            record["Id"]
            for record in records
            if record["Type"] == _IMAGE_REL
            and record["TargetMode"] == "External"
        }
        if any(identifier not in external_records for identifier in link_ids):
            raise _invalid()


def _expected_part_content_type(name: str, declared: str) -> None:
    if name == "word/document.xml" and declared != _DOCUMENT_CT:
        raise _invalid()
    if name == "word/styles.xml" and declared != _STYLES_CT:
        raise _invalid()
    if name == "word/numbering.xml" and declared != _NUMBERING_CT:
        raise _invalid()
    if name == "word/footnotes.xml" and declared != _FOOTNOTES_CT:
        raise _invalid()
    if name == "word/endnotes.xml" and declared != _ENDNOTES_CT:
        raise _invalid()
    if name == "docProps/core.xml" and declared != _CORE_CT:
        raise _invalid()
    if name == "docProps/app.xml" and declared != _APP_CT:
        raise _invalid()
    if _HEADER_PART_RE.fullmatch(name) and declared != _HEADER_CT:
        raise _invalid()
    if _FOOTER_PART_RE.fullmatch(name) and declared != _FOOTER_CT:
        raise _invalid()
    if name == "word/settings.xml" and declared != _SETTINGS_CT:
        raise _invalid()
    if name == "word/fontTable.xml" and declared != _FONT_TABLE_CT:
        raise _invalid()
    if name == "word/webSettings.xml" and declared != _WEB_SETTINGS_CT:
        raise _invalid()
    if _THEME_PART_RE.fullmatch(name) and declared != _THEME_CT:
        raise _invalid()
    if _CUSTOM_XML_ITEM_RE.fullmatch(name) and declared != "application/xml":
        raise _invalid()
    if _CUSTOM_XML_PROPS_RE.fullmatch(name) and declared != _CUSTOM_XML_PROPS_CT:
        raise _invalid()
    if name == "word/stylesWithEffects.xml" and declared != _STYLES_WITH_EFFECTS_CT:
        raise _invalid()
    if name == "docProps/custom.xml" and declared != _CUSTOM_PROPERTIES_CT:
        raise _invalid()
    if name == "docProps/thumbnail.jpeg" and declared != "image/jpeg":
        raise _invalid()
    if name == "docProps/thumbnail.emf" and declared != "image/x-emf":
        raise _invalid()
    glossary_match = _GLOSSARY_ROLE_RE.fullmatch(name)
    if glossary_match is not None:
        glossary_content_types = {
            "document": _GLOSSARY_DOCUMENT_CT,
            "styles": _STYLES_CT,
            "stylesWithEffects": _STYLES_WITH_EFFECTS_CT,
            "settings": _SETTINGS_CT,
            "fontTable": _FONT_TABLE_CT,
            "webSettings": _WEB_SETTINGS_CT,
        }
        if declared != glossary_content_types[glossary_match.group(1)]:
            raise _invalid()
    diagram_match = _DIAGRAM_PART_RE.fullmatch(name)
    if diagram_match is not None and declared != _DIAGRAM_CTS[diagram_match.group(1)]:
        raise _invalid()
    if name in _COMMENTS_PARTS and declared not in _COMMENTS_PARTS[name][0]:
        raise _invalid()
    if _FONT_PART_RE.fullmatch(name) and declared != _OBFUSCATED_FONT_CT:
        raise _invalid()
    if name.endswith(".rels") and declared != _RELATIONSHIP_CT:
        raise _invalid()
    if name.startswith("word/media/") and not declared:
        raise _invalid()


def _allowed_part_name(name: str) -> bool:
    return (
        name in {
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/styles.xml",
            "word/numbering.xml",
            "word/footnotes.xml",
            "word/endnotes.xml",
            "word/_rels/document.xml.rels",
            "docProps/core.xml",
            "docProps/app.xml",
            "word/settings.xml",
            "word/fontTable.xml",
            "word/webSettings.xml",
            "word/stylesWithEffects.xml",
            "docProps/custom.xml",
            "docProps/thumbnail.jpeg",
            "docProps/thumbnail.emf",
        }
        or name.startswith("word/media/")
        or _HEADER_PART_RE.fullmatch(name) is not None
        or _FOOTER_PART_RE.fullmatch(name) is not None
        or _THEME_PART_RE.fullmatch(name) is not None
        or _AUXILIARY_RELS_RE.fullmatch(name) is not None
        or _THEME_RELS_RE.fullmatch(name) is not None
        or _INERT_WORD_RELS_RE.fullmatch(name) is not None
        or _CUSTOM_XML_ITEM_RE.fullmatch(name) is not None
        or _CUSTOM_XML_PROPS_RE.fullmatch(name) is not None
        or _CUSTOM_XML_RELS_RE.fullmatch(name) is not None
        or _GLOSSARY_ROLE_RE.fullmatch(name) is not None
        or _GLOSSARY_RELS_RE.fullmatch(name) is not None
        or _DIAGRAM_PART_RE.fullmatch(name) is not None
        or _DIAGRAM_RELS_RE.fullmatch(name) is not None
        or _FONT_PART_RE.fullmatch(name) is not None
        or name in _COMMENTS_PARTS
    )


def _content_type_for(name: str, defaults: Mapping[str, str], overrides: Mapping[str, str]) -> str:
    if name in overrides:
        return overrides[name]
    extension = name.rsplit(".", 1)[-1].casefold() if "." in name else ""
    if extension and extension in defaults:
        return defaults[extension]
    raise _invalid()


def _validate_xml_part_root(name: str, root: ET.Element) -> None:
    expected = {
        "word/document.xml": f"{{{_W_NS}}}document",
        "word/styles.xml": f"{{{_W_NS}}}styles",
        "word/numbering.xml": f"{{{_W_NS}}}numbering",
        "word/footnotes.xml": f"{{{_W_NS}}}footnotes",
        "word/endnotes.xml": f"{{{_W_NS}}}endnotes",
        "docProps/core.xml": f"{{{_CP_NS}}}coreProperties",
        "docProps/app.xml": f"{{{_EP_NS}}}Properties",
    }.get(name)
    if _HEADER_PART_RE.fullmatch(name):
        expected = f"{{{_W_NS}}}hdr"
    elif _FOOTER_PART_RE.fullmatch(name):
        expected = f"{{{_W_NS}}}ftr"
    elif name == "word/settings.xml":
        expected = f"{{{_W_NS}}}settings"
    elif name == "word/fontTable.xml":
        expected = f"{{{_W_NS}}}fonts"
    elif name == "word/webSettings.xml":
        expected = f"{{{_W_NS}}}webSettings"
    elif _THEME_PART_RE.fullmatch(name):
        expected = f"{{{_A_NS}}}theme"
    elif _CUSTOM_XML_PROPS_RE.fullmatch(name):
        expected = f"{{{_CUSTOM_XML_NS}}}datastoreItem"
    elif name == "word/stylesWithEffects.xml":
        expected = f"{{{_W_NS}}}styles"
    elif name == "docProps/custom.xml":
        expected = f"{{{_CUSTOM_PROPERTIES_NS}}}Properties"
    elif (glossary_match := _GLOSSARY_ROLE_RE.fullmatch(name)) is not None:
        expected = {
            "document": f"{{{_W_NS}}}glossaryDocument",
            "styles": f"{{{_W_NS}}}styles",
            "stylesWithEffects": f"{{{_W_NS}}}styles",
            "settings": f"{{{_W_NS}}}settings",
            "fontTable": f"{{{_W_NS}}}fonts",
            "webSettings": f"{{{_W_NS}}}webSettings",
        }[glossary_match.group(1)]
    elif (diagram_match := _DIAGRAM_PART_RE.fullmatch(name)) is not None:
        expected = {
            "data": f"{{{_DGM_NS}}}dataModel",
            "layout": f"{{{_DGM_NS}}}layoutDef",
            "quickStyle": f"{{{_DGM_NS}}}styleDef",
            "colors": f"{{{_DGM_NS}}}colorsDef",
            "drawing": f"{{{_DSP_NS}}}drawing",
        }[diagram_match.group(1)]
    elif name in _COMMENTS_PARTS:
        expected = _COMMENTS_PARTS[name][1]
    if expected is not None and root.tag != expected:
        raise _invalid()
    if name == "word/document.xml":
        for node in root.iter():
            if node.tag in _REJECTED_WORDPROCESSINGML_TAGS:
                raise _unsupported_active_content()
    if _CUSTOM_XML_PROPS_RE.fullmatch(name):
        item_id = f"{{{_CUSTOM_XML_NS}}}itemID"
        if set(root.attrib) != {item_id} or not root.attrib[item_id]:
            raise _invalid()
        children = list(root)
        if len(children) > 1:
            raise _invalid()
        if children:
            schema_refs = children[0]
            if (
                schema_refs.tag != f"{{{_CUSTOM_XML_NS}}}schemaRefs"
                or schema_refs.attrib
                or (schema_refs.text or "").strip()
                or (schema_refs.tail or "").strip()
            ):
                raise _invalid()
            schema_uri = f"{{{_CUSTOM_XML_NS}}}uri"
            seen_uris: set[str] = set()
            for schema_ref in schema_refs:
                uri = schema_ref.attrib.get(schema_uri)
                if (
                    schema_ref.tag != f"{{{_CUSTOM_XML_NS}}}schemaRef"
                    or set(schema_ref.attrib) != {schema_uri}
                    or type(uri) is not str
                    or not uri
                    or uri in seen_uris
                    or list(schema_ref)
                    or (schema_ref.text or "").strip()
                    or (schema_ref.tail or "").strip()
                ):
                    raise _invalid()
                seen_uris.add(uri)


def _accumulate_xml_stats(
    current: _XmlStats,
    added: _XmlStats,
    limits: DocxLimits,
) -> _XmlStats:
    total = _XmlStats(
        elements=current.elements + added.elements,
        text_characters=current.text_characters + added.text_characters,
        blocks=current.blocks + added.blocks,
    )
    if (
        total.elements > limits.maximum_xml_elements
        or total.text_characters > limits.maximum_aggregate_text_characters
        or total.blocks > limits.maximum_blocks
    ):
        raise _invalid()
    return total


def _inventory_digest(
    names: list[str],
    parts: Mapping[str, bytes],
    content_types: Mapping[str, str],
) -> str:
    binding = bytearray()
    for name in names:
        body = parts[name]
        binding.extend(name.encode("utf-8"))
        binding.extend(b"\0")
        binding.extend(content_types[name].encode("utf-8"))
        binding.extend(b"\0")
        binding.extend(str(len(body)).encode("ascii"))
        binding.extend(b"\0")
        binding.extend(sha256(body).hexdigest().encode("ascii"))
        binding.extend(b"\n")
    return sha256(bytes(binding)).hexdigest()


def validate_docx_package(
    data: bytes,
    limits: DocxLimits = DocxLimits(),
) -> ValidatedDocxPackage:
    """Detach and validate one canonical inactive OOXML package."""

    try:
        if type(data) is not bytes:
            raise _invalid()
        limits = _require_limits(limits)
        if not data or len(data) > limits.maximum_archive_bytes or len(data) > _MAX_DOCX_BYTES:
            raise _invalid()

        eocd = data.rfind(_EOCD)
        if eocd < 0 or eocd + 22 > len(data):
            raise _invalid()
        if any(struct.unpack_from("<H", data, eocd + offset)[0] != 0 for offset in (4, 6)):
            raise _invalid()
        entries_on_disk = struct.unpack_from("<H", data, eocd + 8)[0]
        entry_count = struct.unpack_from("<H", data, eocd + 10)[0]
        central_size = struct.unpack_from("<L", data, eocd + 12)[0]
        central_offset = struct.unpack_from("<L", data, eocd + 16)[0]
        comment_length = struct.unpack_from("<H", data, eocd + 20)[0]
        if (
            entry_count <= 0
            or entries_on_disk != entry_count
            or entry_count > limits.maximum_entries
            or central_offset + central_size > eocd
            or eocd + 22 + comment_length != len(data)
        ):
            raise _invalid()

        names_in_order: list[str] = []
        casefold_names: set[str] = set()
        member_records: list[dict[str, int | str]] = []
        expanded_bytes = 0
        aggregate_xml_stats = _XmlStats(elements=0, text_characters=0, blocks=0)
        cursor = central_offset
        ranges: list[tuple[int, int]] = []
        while len(member_records) < entry_count:
            if cursor + 46 > len(data) or data[cursor : cursor + 4] != _CENTRAL:
                raise _invalid()
            fields = struct.unpack_from("<4s6H3L5H2L", data, cursor)
            flags = fields[3]
            method = fields[4]
            crc = fields[7]
            compressed_size = fields[8]
            uncompressed_size = fields[9]
            name_length = fields[10]
            extra_length = fields[11]
            comment_length = fields[12]
            disk_start = fields[13]
            external_attr = fields[15]
            local_offset = fields[16]
            header_end = cursor + 46 + name_length + extra_length + comment_length
            if header_end > len(data):
                raise _invalid()
            name = _normalize_name(_decode_name(data[cursor + 46 : cursor + 46 + name_length], flags))
            if name in names_in_order or name.casefold() in casefold_names:
                raise _invalid()
            names_in_order.append(name)
            casefold_names.add(name.casefold())
            if (
                flags & ~_ZIP_ALLOWED_GENERAL_PURPOSE_FLAGS
                or method not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                or (flags & _ZIP_DEFLATE_OPTION_FLAGS and method != zipfile.ZIP_DEFLATED)
            ):
                raise _invalid()
            if compressed_size > limits.maximum_archive_bytes or uncompressed_size > limits.maximum_member_bytes:
                raise _invalid()
            if compressed_size == 0 and uncompressed_size:
                raise _invalid()
            if compressed_size and uncompressed_size > compressed_size * limits.maximum_compression_ratio:
                raise _invalid()
            if disk_start != 0:
                raise _invalid()
            expanded_bytes += uncompressed_size
            if expanded_bytes > limits.maximum_expanded_bytes:
                raise _invalid()
            mode = external_attr >> 16
            if mode:
                kind = stat.S_IFMT(mode)
                if kind in {stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK}:
                    raise _invalid()
            if local_offset + 30 > central_offset or local_offset < 0:
                raise _invalid()
            if data[local_offset : local_offset + 4] != _LOCAL:
                raise _invalid()
            local_fields = struct.unpack_from("<4s5H3L2H", data, local_offset)
            local_name_length = local_fields[9]
            local_extra_length = local_fields[10]
            local_name = _decode_name(
                data[local_offset + 30 : local_offset + 30 + local_name_length], local_fields[2]
            )
            local_data_offset = local_offset + 30 + local_name_length + local_extra_length
            local_end = local_data_offset + compressed_size
            if (
                local_name != name
                or local_fields[2] != flags
                or local_fields[3] != method
                or local_fields[6] != crc
                or local_fields[7] != compressed_size
                or local_fields[8] != uncompressed_size
                or local_end > central_offset
            ):
                raise _invalid()
            ranges.append((local_offset, local_end))
            member_records.append(
                {
                    "name": name,
                    "crc": crc,
                    "compressed_size": compressed_size,
                    "uncompressed_size": uncompressed_size,
                }
            )
            cursor = header_end
        if cursor != central_offset + central_size:
            raise _invalid()
        sorted_ranges = sorted(ranges)
        if not sorted_ranges or sorted_ranges[0][0] != 0 or sorted_ranges[-1][1] != central_offset:
            raise _invalid()
        for previous, current in zip(sorted_ranges, sorted_ranges[1:]):
            if current[0] < previous[1]:
                raise _invalid()
            if current[0] != previous[1]:
                raise _invalid()

        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) != entry_count:
                raise _invalid()
            parts_raw: dict[str, bytes] = {}
            for info, record in zip(infos, member_records):
                if info.filename != record["name"]:
                    raise _invalid()
                body = archive.read(info.filename)
                if len(body) != record["uncompressed_size"]:
                    raise _invalid()
                parts_raw[info.filename] = bytes(body)

        required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels"}
        if not required.issubset(parts_raw):
            raise _invalid()
        _pre_scan_active_relationships(parts_raw, limits)
        _pre_scan_active_parts(parts_raw, limits)
        if any(not _allowed_part_name(name) for name in parts_raw):
            raise _invalid()

        defaults, overrides, content_type_stats = _parse_content_types(parts_raw["[Content_Types].xml"], limits)
        aggregate_xml_stats = _accumulate_xml_stats(aggregate_xml_stats, content_type_stats, limits)
        content_types_raw: dict[str, str] = {}
        for name in names_in_order:
            declared = _content_type_for(name, defaults, overrides)
            _expected_part_content_type(name, declared)
            content_types_raw[name] = declared
        if content_types_raw["word/document.xml"] != _DOCUMENT_CT or _DOCX_MIME != "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
            raise _invalid()

        relationships_raw: dict[str, tuple[dict[str, str], ...]] = {}
        total_relationships = 0
        for relationship_part in sorted(
            name for name in parts_raw if name.endswith(".rels")
        ):
            source_part = _relationship_source_part(relationship_part)
            if source_part != "/" and source_part not in parts_raw:
                raise _invalid()
            if source_part != "/" and source_part not in parts_raw:
                raise _invalid()
            if source_part in relationships_raw:
                raise _invalid()
            relationships_raw[source_part], relationship_stats = _parse_relationships(
                source_part, parts_raw[relationship_part], limits
            )
            aggregate_xml_stats = _accumulate_xml_stats(
                aggregate_xml_stats,
                relationship_stats,
                limits,
            )
            total_relationships += len(relationships_raw[source_part])
            if total_relationships > limits.maximum_relationships:
                raise _invalid()
        _validate_external_image_relationships(parts_raw, relationships_raw, limits)

        office_document_targets = [
            relation["Target"]
            for relation in relationships_raw["/"]
            if relation["Type"] == _OFFICE_DOCUMENT_REL
        ]
        if office_document_targets != ["word/document.xml"]:
            raise _invalid()

        for source_part, relations in relationships_raw.items():
            for relation in relations:
                if relation["Type"] == _HYPERLINK_REL:
                    continue
                if (
                    relation["Type"] == _IMAGE_REL
                    and relation["TargetMode"] == "External"
                ):
                    continue
                target = relation["Target"]
                if target not in parts_raw:
                    raise _invalid()
                if relation["Type"] == _IMAGE_REL and not target.startswith("word/media/"):
                    raise _invalid()
                if relation["Type"] == _STYLES_REL:
                    expected_styles = (
                        "word/glossary/styles.xml"
                        if source_part == "word/glossary/document.xml"
                        else "word/styles.xml"
                    )
                    if target != expected_styles:
                        raise _invalid()
                if relation["Type"] == _NUMBERING_REL and target != "word/numbering.xml":
                    raise _invalid()
                if relation["Type"] == _FOOTNOTES_REL and target != "word/footnotes.xml":
                    raise _invalid()
                if relation["Type"] == _ENDNOTES_REL and target != "word/endnotes.xml":
                    raise _invalid()
                if relation["Type"] == _HEADER_REL and _HEADER_PART_RE.fullmatch(target) is None:
                    raise _invalid()
                if relation["Type"] == _FOOTER_REL and _FOOTER_PART_RE.fullmatch(target) is None:
                    raise _invalid()
                if relation["Type"] == _SETTINGS_REL:
                    expected_settings = (
                        "word/glossary/settings.xml"
                        if source_part == "word/glossary/document.xml"
                        else "word/settings.xml"
                    )
                    if target != expected_settings:
                        raise _invalid()
                if relation["Type"] == _FONT_TABLE_REL:
                    expected_font_table = (
                        "word/glossary/fontTable.xml"
                        if source_part == "word/glossary/document.xml"
                        else "word/fontTable.xml"
                    )
                    if target != expected_font_table:
                        raise _invalid()
                if relation["Type"] == _WEB_SETTINGS_REL:
                    expected_web_settings = (
                        "word/glossary/webSettings.xml"
                        if source_part == "word/glossary/document.xml"
                        else "word/webSettings.xml"
                    )
                    if target != expected_web_settings:
                        raise _invalid()
                if relation["Type"] == _THEME_REL and _THEME_PART_RE.fullmatch(target) is None:
                    raise _invalid()
                if relation["Type"] == _CUSTOM_XML_REL:
                    if source_part != "word/document.xml" or _CUSTOM_XML_ITEM_RE.fullmatch(target) is None:
                        raise _invalid()
                if relation["Type"] == _CUSTOM_XML_PROPS_REL:
                    item_match = _CUSTOM_XML_ITEM_RE.fullmatch(source_part)
                    props_match = _CUSTOM_XML_PROPS_RE.fullmatch(target)
                    if (
                        item_match is None
                        or props_match is None
                        or item_match.group(1) != props_match.group(1)
                    ):
                        raise _invalid()
                if relation["Type"] == _STYLES_WITH_EFFECTS_REL:
                    expected_styles_with_effects = {
                        "word/document.xml": "word/stylesWithEffects.xml",
                        "word/glossary/document.xml": "word/glossary/stylesWithEffects.xml",
                    }.get(source_part)
                    if target != expected_styles_with_effects:
                        raise _invalid()
                if relation["Type"] == _CUSTOM_PROPERTIES_REL and (
                    source_part != "/" or target != "docProps/custom.xml"
                ):
                    raise _invalid()
                if relation["Type"] == _THUMBNAIL_REL and (
                    source_part != "/"
                    or target
                    not in {"docProps/thumbnail.jpeg", "docProps/thumbnail.emf"}
                ):
                    raise _invalid()
                if relation["Type"] == _GLOSSARY_REL and (
                    source_part != "word/document.xml"
                    or target != "word/glossary/document.xml"
                ):
                    raise _invalid()
                for comment_name, (_, _, comment_relation) in _COMMENTS_PARTS.items():
                    if relation["Type"] == comment_relation and (
                        source_part != "word/document.xml" or target != comment_name
                    ):
                        raise _invalid()
                for diagram_role, diagram_relation in _DIAGRAM_RELS.items():
                    if relation["Type"] == diagram_relation:
                        diagram_match = _DIAGRAM_PART_RE.fullmatch(target)
                        if (
                            source_part != "word/document.xml"
                            or diagram_match is None
                            or diagram_match.group(1) != diagram_role
                        ):
                            raise _invalid()
                if relation["Type"] == _DIAGRAM_DRAWING_REL:
                    target_match = _DIAGRAM_PART_RE.fullmatch(target)
                    if (
                        source_part != "word/document.xml"
                        or target_match is None
                        or target_match.group(1) != "drawing"
                    ):
                        raise _invalid()
                if relation["Type"] == _FONT_REL and (
                    source_part not in {
                        "word/fontTable.xml",
                        "word/glossary/fontTable.xml",
                    }
                    or _FONT_PART_RE.fullmatch(target) is None
                ):
                    raise _invalid()

        custom_items = {
            match.group(1)
            for name in parts_raw
            if (match := _CUSTOM_XML_ITEM_RE.fullmatch(name)) is not None
        }
        custom_props = {
            match.group(1)
            for name in parts_raw
            if (match := _CUSTOM_XML_PROPS_RE.fullmatch(name)) is not None
        }
        custom_rels = {
            match.group(1)
            for name in parts_raw
            if (match := _CUSTOM_XML_RELS_RE.fullmatch(name)) is not None
        }
        if custom_items != custom_props or custom_items != custom_rels:
            raise _invalid()
        linked_custom_items = {
            relation["Target"].removeprefix("customXml/item").removesuffix(".xml")
            for relation in relationships_raw["word/document.xml"]
            if relation["Type"] == _CUSTOM_XML_REL
        }
        if linked_custom_items != custom_items:
            raise _invalid()
        for index in custom_items:
            relations = relationships_raw.get(f"customXml/item{index}.xml")
            if (
                relations is None
                or len(relations) != 1
                or relations[0]["Type"] != _CUSTOM_XML_PROPS_REL
                or relations[0]["Target"] != f"customXml/itemProps{index}.xml"
            ):
                raise _invalid()

        owner_contracts = (
            ("word/stylesWithEffects.xml", "word/document.xml", _STYLES_WITH_EFFECTS_REL),
            ("docProps/custom.xml", "/", _CUSTOM_PROPERTIES_REL),
            ("word/glossary/document.xml", "word/document.xml", _GLOSSARY_REL),
        )
        for part_name, owner, relation_type in owner_contracts:
            linked = {
                relation["Target"]
                for relation in relationships_raw.get(owner, ())
                if relation["Type"] == relation_type
            }
            if (part_name in parts_raw) != (linked == {part_name}):
                raise _invalid()
        for comment_name, (_, _, comment_relation) in _COMMENTS_PARTS.items():
            linked = {
                relation["Target"]
                for relation in relationships_raw["word/document.xml"]
                if relation["Type"] == comment_relation
            }
            if (comment_name in parts_raw) != (linked == {comment_name}):
                raise _invalid()
        thumbnail_parts = {
            name
            for name in parts_raw
            if name in {"docProps/thumbnail.jpeg", "docProps/thumbnail.emf"}
        }
        linked_thumbnails = {
            relation["Target"]
            for relation in relationships_raw["/"]
            if relation["Type"] == _THUMBNAIL_REL
        }
        if thumbnail_parts != linked_thumbnails or len(thumbnail_parts) > 1:
            raise _invalid()
        glossary_children = {
            name
            for name in parts_raw
            if (match := _GLOSSARY_ROLE_RE.fullmatch(name)) is not None
            and match.group(1) != "document"
        }
        linked_glossary_children = {
            relation["Target"]
            for relation in relationships_raw.get("word/glossary/document.xml", ())
            if relation["Type"] in {
                _STYLES_REL,
                _SETTINGS_REL,
                _FONT_TABLE_REL,
                _WEB_SETTINGS_REL,
                _STYLES_WITH_EFFECTS_REL,
            }
        }
        if glossary_children != linked_glossary_children:
            raise _invalid()
        for role, relation_type in _DIAGRAM_RELS.items():
            role_parts = {
                name
                for name in parts_raw
                if (match := _DIAGRAM_PART_RE.fullmatch(name)) is not None
                and match.group(1) == role
            }
            linked = {
                relation["Target"]
                for relation in relationships_raw["word/document.xml"]
                if relation["Type"] == relation_type
            }
            if role_parts != linked:
                raise _invalid()
        diagram_indices = {
            role: {
                match.group(2)
                for name in parts_raw
                if (match := _DIAGRAM_PART_RE.fullmatch(name)) is not None
                and match.group(1) == role
            }
            for role in _DIAGRAM_RELS
        }
        if len({frozenset(indices) for indices in diagram_indices.values()}) != 1:
            raise _invalid()
        drawing_parts = {
            name
            for name in parts_raw
            if (match := _DIAGRAM_PART_RE.fullmatch(name)) is not None
            and match.group(1) == "drawing"
        }
        linked_drawings = {
            relation["Target"]
            for relation in relationships_raw["word/document.xml"]
            if relation["Type"] == _DIAGRAM_DRAWING_REL
        }
        drawing_indices = {
            match.group(2)
            for name in drawing_parts
            if (match := _DIAGRAM_PART_RE.fullmatch(name)) is not None
        }
        if (
            drawing_parts != linked_drawings
            or not drawing_indices.issubset(diagram_indices["data"])
        ):
            raise _invalid()
        font_parts = {name for name in parts_raw if _FONT_PART_RE.fullmatch(name)}
        linked_fonts = {
            relation["Target"]
            for source in ("word/fontTable.xml", "word/glossary/fontTable.xml")
            for relation in relationships_raw.get(source, ())
            if relation["Type"] == _FONT_REL
        }
        if font_parts != linked_fonts:
            raise _invalid()
        for name in font_parts:
            body = parts_raw[name]
            if (
                len(body) < 32
                or body.startswith((b"MZ", b"\x7fELF", b"#!", b"PK\x03\x04"))
            ):
                raise _invalid()
        if "docProps/thumbnail.jpeg" in parts_raw:
            thumbnail = parts_raw["docProps/thumbnail.jpeg"]
            eoi = b"\xff\xd9"
            if (
                len(thumbnail) < 4
                or not thumbnail.startswith(b"\xff\xd8\xff")
                or thumbnail.count(eoi) != 1
            ):
                raise _invalid()
            tail = thumbnail[thumbnail.index(eoi) + len(eoi) :]
            if len(tail) > 3 or any(byte != 0 for byte in tail):
                raise _invalid()
        if "docProps/thumbnail.emf" in parts_raw:
            thumbnail = parts_raw["docProps/thumbnail.emf"]
            if (
                len(thumbnail) < 88
                or thumbnail.startswith((b"MZ", b"\x7fELF", b"#!", b"PK\x03\x04"))
                or struct.unpack_from("<I", thumbnail, 0)[0] != 1
                or struct.unpack_from("<I", thumbnail, 4)[0] < 88
                or struct.unpack_from("<I", thumbnail, 4)[0] > len(thumbnail)
                or struct.unpack_from("<I", thumbnail, 40)[0] != 0x464D4520
                or struct.unpack_from("<I", thumbnail, 48)[0] != len(thumbnail)
                or struct.unpack_from("<I", thumbnail, 52)[0] < 1
            ):
                raise _invalid()

        xml_parts = sorted(
            name
            for name in parts_raw
            if name.endswith(".xml") and name != "[Content_Types].xml"
        )
        for xml_part in xml_parts:
            root, stats = _xml_root(
                parts_raw[xml_part],
                limits,
                allow_mso_content_type_pi=_CUSTOM_XML_ITEM_RE.fullmatch(xml_part)
                is not None,
            )
            _validate_xml_part_root(xml_part, root)
            aggregate_xml_stats = _accumulate_xml_stats(aggregate_xml_stats, stats, limits)

        immutable_parts = MappingProxyType({name: parts_raw[name] for name in names_in_order})
        immutable_content_types = MappingProxyType(
            {name: content_types_raw[name] for name in names_in_order}
        )
        immutable_relationships = MappingProxyType(
            {
                name: tuple(MappingProxyType(dict(item)) for item in items)
                for name, items in relationships_raw.items()
            }
        )
        return ValidatedDocxPackage(
            parts=immutable_parts,
            content_types=immutable_content_types,
            relationships=immutable_relationships,
            inventory_digest=_inventory_digest(names_in_order, parts_raw, content_types_raw),
            expanded_bytes=expanded_bytes,
        )
    except DocxPackageError:
        raise
    except (KeyError, OSError, TypeError, ValueError, zipfile.BadZipFile, struct.error) as exc:
        raise _invalid(exc) from exc


def docx_configuration_digest(limits: DocxLimits) -> str:
    checked = _require_limits(limits)
    return canonical_digest(
        {
            "maximum_archive_bytes": checked.maximum_archive_bytes,
            "maximum_entries": checked.maximum_entries,
            "maximum_member_bytes": checked.maximum_member_bytes,
            "maximum_expanded_bytes": checked.maximum_expanded_bytes,
            "maximum_compression_ratio": checked.maximum_compression_ratio,
            "maximum_xml_depth": checked.maximum_xml_depth,
            "maximum_xml_elements": checked.maximum_xml_elements,
            "maximum_text_characters": checked.maximum_text_characters,
            "maximum_aggregate_text_characters": checked.maximum_aggregate_text_characters,
            "maximum_relationships": checked.maximum_relationships,
            "maximum_blocks": checked.maximum_blocks,
        }
    )


def _parsing_error(
    message: str,
    cause: BaseException | None = None,
    *,
    rejection_code: str | None = None,
) -> Exception:
    from .parsing import ParsingError

    error = ParsingError(message)
    if rejection_code is not None:
        error.rejection_code = rejection_code
    if cause is not None:
        error.__cause__ = cause
    return error


def _strict_sha256_digest(value: Any) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    return value


def _strict_int(value: Any, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise _parsing_error("DOCX benchmark evidence is invalid")
    return value


def _strict_score(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    numeric = float(value)
    if not (0.0 <= numeric <= 1.0):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    return numeric


def validate_docx_benchmark(
    manifest: Mapping[str, Any],
    *,
    expected_configuration_digest: str,
    expected_corpus_digest: str | None = None,
) -> Mapping[str, Any]:
    required = {
        "schema_version",
        "fixture_corpus_digest",
        "parser_id",
        "parser_version",
        "parser_configuration_digest",
        "document_ir_version",
        "attempts_per_document",
        "document_count",
        "metrics",
        "exclusions",
        "stable_failures",
        "repeatability",
        "unexpected_failures",
        "decision",
        "result_digest",
    }
    if not isinstance(manifest, Mapping) or set(manifest) != required:
        raise _parsing_error("DOCX benchmark evidence is invalid")
    if (
        manifest["schema_version"] != DOCX_BENCHMARK_SCHEMA_VERSION
        or manifest["parser_id"] != DOCX_PARSER_ID
        or manifest["parser_version"] != DOCX_PARSER_VERSION
        or manifest["document_ir_version"] != "ao.lore.document-ir.v0.1"
        or not hmac.compare_digest(
            _strict_sha256_digest(manifest["parser_configuration_digest"]),
            _strict_sha256_digest(expected_configuration_digest),
        )
    ):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    fixture_corpus_digest = _strict_sha256_digest(manifest["fixture_corpus_digest"])
    if expected_corpus_digest is not None and not hmac.compare_digest(
        fixture_corpus_digest,
        _strict_sha256_digest(expected_corpus_digest),
    ):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    supplied_result = _strict_sha256_digest(manifest["result_digest"])
    attempts_per_document = _strict_int(manifest["attempts_per_document"], minimum=1)
    document_count = _strict_int(manifest["document_count"], minimum=1)
    if attempts_per_document != 2 or document_count != DOCX_DOCUMENT_COUNT:
        raise _parsing_error("DOCX benchmark evidence is invalid")
    metrics = manifest["metrics"]
    metric_keys = {
        "text_fidelity",
        "structural_fidelity",
        "source_location_fidelity",
        "expected_outcome_accuracy",
        "expected_rejection_count",
        "accepted_document_count",
    }
    if not isinstance(metrics, Mapping) or set(metrics) != metric_keys:
        raise _parsing_error("DOCX benchmark evidence is invalid")
    normalized_metrics = {
        "text_fidelity": _strict_score(metrics["text_fidelity"]),
        "structural_fidelity": _strict_score(metrics["structural_fidelity"]),
        "source_location_fidelity": _strict_score(metrics["source_location_fidelity"]),
        "expected_outcome_accuracy": _strict_score(metrics["expected_outcome_accuracy"]),
        "expected_rejection_count": _strict_int(metrics["expected_rejection_count"], minimum=0),
        "accepted_document_count": _strict_int(metrics["accepted_document_count"], minimum=0),
    }
    exclusions = manifest["exclusions"]
    if type(exclusions) is not list or len({value for value in exclusions if type(value) is str}) != len(exclusions):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    stable_failures = manifest["stable_failures"]
    if (
        not isinstance(stable_failures, Mapping)
        or set(stable_failures) != DOCX_STABLE_FAILURE_CATEGORIES
    ):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    normalized_failures: dict[str, int] = {}
    for key, value in stable_failures.items():
        normalized_failures[key] = _strict_int(value, minimum=0)
    if (
        normalized_metrics["expected_rejection_count"] != DOCX_EXPECTED_REJECTION_COUNT
        or normalized_metrics["accepted_document_count"] != DOCX_ACCEPTED_DOCUMENT_COUNT
        or normalized_metrics["expected_rejection_count"]
        + normalized_metrics["accepted_document_count"]
        != DOCX_DOCUMENT_COUNT
        or normalized_failures["unsupported_active_content"]
        != DOCX_ACTIVE_CONTENT_REJECTION_COUNT
        or normalized_failures["invalid_package"]
        != DOCX_INVALID_PACKAGE_REJECTION_COUNT
    ):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    repeatability = manifest["repeatability"]
    if not isinstance(repeatability, Mapping) or set(repeatability) != {"runs", "identical", "score"}:
        raise _parsing_error("DOCX benchmark evidence is invalid")
    normalized_repeatability = {
        "runs": _strict_int(repeatability["runs"], minimum=1),
        "identical": repeatability["identical"],
        "score": _strict_score(repeatability["score"]),
    }
    if type(normalized_repeatability["identical"]) is not bool:
        raise _parsing_error("DOCX benchmark evidence is invalid")
    unexpected_failures = manifest["unexpected_failures"]
    if type(unexpected_failures) is not list or len({value for value in unexpected_failures if type(value) is str}) != len(unexpected_failures):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    decision = manifest["decision"]
    if decision not in {"hold", "candidate_change", "investigate"}:
        raise _parsing_error("DOCX benchmark evidence is invalid")
    detached = {
        "schema_version": DOCX_BENCHMARK_SCHEMA_VERSION,
        "fixture_corpus_digest": fixture_corpus_digest,
        "parser_id": DOCX_PARSER_ID,
        "parser_version": DOCX_PARSER_VERSION,
        "parser_configuration_digest": expected_configuration_digest,
        "document_ir_version": "ao.lore.document-ir.v0.1",
        "attempts_per_document": attempts_per_document,
        "document_count": document_count,
        "metrics": normalized_metrics,
        "exclusions": list(exclusions),
        "stable_failures": normalized_failures,
        "repeatability": normalized_repeatability,
        "unexpected_failures": list(unexpected_failures),
        "decision": decision,
    }
    expected_result = canonical_digest(detached)
    if not hmac.compare_digest(supplied_result, expected_result):
        raise _parsing_error("DOCX benchmark evidence is invalid")
    if decision == "hold":
        if detached["unexpected_failures"]:
            raise _parsing_error("DOCX benchmark evidence is invalid")
        if detached["stable_failures"] != {
            "invalid_package": DOCX_INVALID_PACKAGE_REJECTION_COUNT,
            "unsupported_active_content": DOCX_ACTIVE_CONTENT_REJECTION_COUNT,
        }:
            raise _parsing_error("DOCX benchmark evidence is invalid")
        if detached["repeatability"] != {"runs": 2, "identical": True, "score": 1.0}:
            raise _parsing_error("DOCX benchmark evidence is invalid")
        for name, minimum in DOCX_FLOORS.items():
            if detached["metrics"][name] < minimum:
                raise _parsing_error("DOCX benchmark evidence is invalid")
    return MappingProxyType(copy.deepcopy({**detached, "result_digest": supplied_result}))


def _xml_part_root(package: ValidatedDocxPackage, name: str, limits: DocxLimits) -> ET.Element:
    try:
        body = package.parts[name]
    except KeyError as exc:
        raise _parsing_error("DOCX package is invalid", exc) from exc
    try:
        root, _ = _xml_root(body, limits)
    except DocxPackageError as exc:
        raise _parsing_error("DOCX package is invalid", exc) from exc
    return root


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _w_attr(name: str) -> str:
    return _q(_W_NS, name)


def _text_from_runs(element: ET.Element) -> str:
    return "".join(node.text or "" for node in element.iter(_q(_W_NS, "t")))


def _style_levels(package: ValidatedDocxPackage, limits: DocxLimits) -> dict[str, int]:
    if "word/styles.xml" not in package.parts:
        return {}
    root = _xml_part_root(package, "word/styles.xml", limits)
    levels: dict[str, int] = {}
    for style in root.findall(_q(_W_NS, "style")):
        if style.attrib.get(_w_attr("type")) != "paragraph":
            continue
        style_id = style.attrib.get(_w_attr("styleId"))
        if type(style_id) is not str or not style_id:
            continue
        outline = style.find(_q(_W_NS, "outlineLvl"))
        if outline is not None:
            raw = outline.attrib.get(_w_attr("val"))
            if type(raw) is str and raw.isdigit():
                levels[style_id] = max(1, min(6, int(raw) + 1))
                continue
        suffix = style_id[len("Heading") :] if style_id.startswith("Heading") else ""
        if suffix.isdigit():
            levels[style_id] = max(1, min(6, int(suffix)))
    return levels


def _numbering_formats(package: ValidatedDocxPackage, limits: DocxLimits) -> dict[tuple[str, int], str]:
    if "word/numbering.xml" not in package.parts:
        return {}
    root = _xml_part_root(package, "word/numbering.xml", limits)
    abstract_levels: dict[str, dict[int, str]] = {}
    for abstract in root.findall(_q(_W_NS, "abstractNum")):
        abstract_id = abstract.attrib.get(_w_attr("abstractNumId"))
        if type(abstract_id) is not str:
            continue
        levels: dict[int, str] = {}
        for level in abstract.findall(_q(_W_NS, "lvl")):
            raw_level = level.attrib.get(_w_attr("ilvl"))
            num_format = level.find(_q(_W_NS, "numFmt"))
            raw_format = None if num_format is None else num_format.attrib.get(_w_attr("val"))
            if type(raw_level) is str and raw_level.isdigit() and type(raw_format) is str and raw_format:
                levels[int(raw_level)] = raw_format
        abstract_levels[abstract_id] = levels
    num_to_abstract: dict[str, str] = {}
    for num in root.findall(_q(_W_NS, "num")):
        num_id = num.attrib.get(_w_attr("numId"))
        abstract = num.find(_q(_W_NS, "abstractNumId"))
        abstract_id = None if abstract is None else abstract.attrib.get(_w_attr("val"))
        if type(num_id) is str and type(abstract_id) is str:
            num_to_abstract[num_id] = abstract_id
    resolved: dict[tuple[str, int], str] = {}
    for num_id, abstract_id in num_to_abstract.items():
        for level, num_format in abstract_levels.get(abstract_id, {}).items():
            resolved[(num_id, level)] = num_format
    return resolved


def _notes_by_kind(
    package: ValidatedDocxPackage, part_name: str, container_tag: str, note_tag: str, limits: DocxLimits
) -> dict[str, str]:
    if part_name not in package.parts:
        return {}
    root = _xml_part_root(package, part_name, limits)
    if root.tag != container_tag:
        raise _parsing_error("DOCX package is invalid")
    notes: dict[str, str] = {}
    for note in root.findall(note_tag):
        note_id = note.attrib.get(_w_attr("id"))
        if type(note_id) is not str or not note_id or not note_id.lstrip("-").isdigit():
            continue
        if int(note_id) <= 0:
            continue
        text = "\n".join(
            candidate for candidate in (_text_from_runs(paragraph).strip() for paragraph in note.findall(f".//{_q(_W_NS, 'p')}")) if candidate
        )
        if text:
            notes[note_id] = text
    return notes


def _document_relationships(package: ValidatedDocxPackage) -> dict[str, Mapping[str, str]]:
    relationships = package.relationships.get("word/document.xml")
    if relationships is None:
        raise _parsing_error("DOCX package is invalid")
    by_id: dict[str, Mapping[str, str]] = {}
    for record in relationships:
        relation_id = record.get("Id")
        if type(relation_id) is not str or relation_id in by_id:
            raise _parsing_error("DOCX package is invalid")
        by_id[relation_id] = record
    return by_id


def _paragraph_style_and_numbering(
    paragraph: ET.Element, styles: Mapping[str, int], numbering: Mapping[tuple[str, int], str]
) -> tuple[str, int | None, dict[str, Any] | None]:
    props = paragraph.find(_q(_W_NS, "pPr"))
    style_id = None
    list_attrs = None
    if props is not None:
        style = props.find(_q(_W_NS, "pStyle"))
        style_id = None if style is None else style.attrib.get(_w_attr("val"))
        num_props = props.find(_q(_W_NS, "numPr"))
        if num_props is not None:
            num_id = num_props.find(_q(_W_NS, "numId"))
            level = num_props.find(_q(_W_NS, "ilvl"))
            raw_num_id = None if num_id is None else num_id.attrib.get(_w_attr("val"))
            raw_level = None if level is None else level.attrib.get(_w_attr("val"))
            if type(raw_num_id) is str and type(raw_level) is str and raw_level.isdigit():
                list_attrs = {
                    "list_level": int(raw_level),
                    "list_id": raw_num_id,
                    "list_format": numbering.get((raw_num_id, int(raw_level)), "unknown"),
                }
    if list_attrs is not None:
        return "list", None, list_attrs
    if type(style_id) is str and style_id in styles:
        return "heading", styles[style_id], None
    return "paragraph", None, None


def _hyperlink_invalid() -> None:
    raise _parsing_error("DOCX package is invalid")


def _hyperlink_color(value: str) -> bool:
    return (
        value == "auto"
        or _HYPERLINK_HEX_COLOR_RE.fullmatch(value) is not None
        or _HYPERLINK_TOKEN_RE.fullmatch(value) is not None
    )


def _hyperlink_font_name(value: str) -> bool:
    return (
        1 <= len(value) <= 1024
        and bool(value.strip())
        and all(ord(character) not in _CONTROL_BYTES for character in value)
    )


def _validate_hyperlink_run_properties(props: ET.Element) -> None:
    if props.tag != _q(_W_NS, "rPr") or props.attrib or props.text:
        _hyperlink_invalid()
    seen: set[str] = set()
    no_attributes = {
        _q(_W_NS, name)
        for name in (
            "bCs",
            "i",
            "iCs",
            "noProof",
            "snapToGrid",
            "webHidden",
        )
    }
    boolean_tags = {_q(_W_NS, "b"), _q(_W_NS, "caps")}
    decimal_tags = {_q(_W_NS, "sz"), _q(_W_NS, "szCs")}
    val_attribute = _w_attr("val")
    for child in list(props):
        if (
            child.tag in seen
            or list(child)
            or child.text
            or child.tail
        ):
            _hyperlink_invalid()
        seen.add(child.tag)
        attributes = child.attrib
        if child.tag in no_attributes:
            if attributes:
                _hyperlink_invalid()
        elif child.tag in boolean_tags:
            if set(attributes) not in (set(), {val_attribute}) or (
                val_attribute in attributes
                and attributes[val_attribute] not in {"0", "1", "on", "off"}
            ):
                _hyperlink_invalid()
        elif child.tag == _q(_W_NS, "bdr"):
            allowed = {
                _w_attr("color"),
                _w_attr("frame"),
                _w_attr("space"),
                _w_attr("sz"),
                val_attribute,
            }
            if not set(attributes).issubset(allowed):
                _hyperlink_invalid()
            for name, value in attributes.items():
                if name == _w_attr("color") and not _hyperlink_color(value):
                    _hyperlink_invalid()
                if name == _w_attr("frame") and value not in {"0", "1", "on", "off"}:
                    _hyperlink_invalid()
                if name in {_w_attr("space"), _w_attr("sz")} and _HYPERLINK_UNSIGNED_RE.fullmatch(value) is None:
                    _hyperlink_invalid()
                if name == val_attribute and _HYPERLINK_TOKEN_RE.fullmatch(value) is None:
                    _hyperlink_invalid()
        elif child.tag == _q(_W_NS, "color"):
            if set(attributes) != {val_attribute} or not _hyperlink_color(attributes[val_attribute]):
                _hyperlink_invalid()
        elif child.tag == _q(_W_NS, "lang"):
            allowed = {_w_attr("eastAsia"), val_attribute}
            if not attributes or not set(attributes).issubset(allowed) or any(
                _HYPERLINK_LANGUAGE_RE.fullmatch(value) is None
                or len(value) > 255
                for value in attributes.values()
            ):
                _hyperlink_invalid()
        elif child.tag == _q(_W_NS, "rFonts"):
            font_names = {
                _w_attr("ascii"),
                _w_attr("cs"),
                _w_attr("eastAsia"),
                _w_attr("hAnsi"),
            }
            themes = {
                _w_attr("asciiTheme"),
                _w_attr("cstheme"),
                _w_attr("eastAsiaTheme"),
                _w_attr("hAnsiTheme"),
            }
            if not attributes or not set(attributes).issubset(font_names | themes):
                _hyperlink_invalid()
            for name, value in attributes.items():
                if name in font_names and not _hyperlink_font_name(value):
                    _hyperlink_invalid()
                if name in themes and _HYPERLINK_TOKEN_RE.fullmatch(value) is None:
                    _hyperlink_invalid()
        elif child.tag == _q(_W_NS, "rStyle"):
            if set(attributes) != {val_attribute} or _HYPERLINK_TOKEN_RE.fullmatch(attributes[val_attribute]) is None:
                _hyperlink_invalid()
        elif child.tag == _q(_W_NS, "shd"):
            allowed = {_w_attr("color"), _w_attr("fill"), val_attribute}
            if not attributes or not set(attributes).issubset(allowed):
                _hyperlink_invalid()
            for name, value in attributes.items():
                if name in {_w_attr("color"), _w_attr("fill")} and not _hyperlink_color(value):
                    _hyperlink_invalid()
                if name == val_attribute and _HYPERLINK_TOKEN_RE.fullmatch(value) is None:
                    _hyperlink_invalid()
        elif child.tag == _q(_W_NS, "spacing"):
            if set(attributes) != {val_attribute} or _HYPERLINK_SIGNED_RE.fullmatch(attributes[val_attribute]) is None:
                _hyperlink_invalid()
        elif child.tag in decimal_tags:
            if set(attributes) != {val_attribute} or _HYPERLINK_UNSIGNED_RE.fullmatch(attributes[val_attribute]) is None:
                _hyperlink_invalid()
        elif child.tag == _q(_W_NS, "u"):
            if set(attributes) != {val_attribute} or _HYPERLINK_TOKEN_RE.fullmatch(attributes[val_attribute]) is None:
                _hyperlink_invalid()
        else:
            _hyperlink_invalid()


@dataclass(frozen=True)
class _HyperlinkValue:
    text: str
    relationship: str


def _hyperlink_value(
    element: ET.Element,
    relationships: Mapping[str, Mapping[str, str]],
) -> _HyperlinkValue:
    id_attribute = _q(_R_NS, "id")
    anchor_attribute = _w_attr("anchor")
    history_attribute = _w_attr("history")
    tooltip_attribute = _w_attr("tooltip")
    frame_attribute = _w_attr("tgtFrame")
    if element.tag != _q(_W_NS, "hyperlink"):
        raise _parsing_error("DOCX package is invalid")
    allowed = {
        id_attribute,
        anchor_attribute,
        history_attribute,
        tooltip_attribute,
        frame_attribute,
    }
    if not set(element.attrib).issubset(allowed):
        raise _parsing_error("DOCX package is invalid")
    relation_id = element.attrib.get(id_attribute)
    anchor = element.attrib.get(anchor_attribute)
    history = element.attrib.get(history_attribute)
    tooltip = element.attrib.get(tooltip_attribute)
    frame = element.attrib.get(frame_attribute)
    if history is not None and history != HYPERLINK_HISTORY_TRUE:
        raise _parsing_error("DOCX package is invalid")
    if relation_id is None and anchor is None:
        raise _parsing_error("DOCX package is invalid")
    if tooltip is not None and (
        not 1 <= len(tooltip) <= 1024
        or any(ord(character) in _CONTROL_BYTES for character in tooltip)
    ):
        raise _parsing_error("DOCX package is invalid")
    if frame is not None and _HYPERLINK_TOKEN_RE.fullmatch(frame) is None:
        raise _parsing_error("DOCX package is invalid")
    if anchor is not None and (
        not anchor.strip()
        or len(anchor) > MAXIMUM_ANCHOR_CHARACTERS
        or any(ord(character) in _CONTROL_BYTES for character in anchor)
    ):
        raise _parsing_error("DOCX package is invalid")
    relationship = "internal-anchor"
    if relation_id is not None:
        if not relation_id or relation_id not in relationships:
            raise _parsing_error("DOCX package is invalid")
        relation = relationships[relation_id]
        if (
            relation.get("Type") != _HYPERLINK_REL
            or relation.get("TargetMode") != "External"
        ):
            raise _parsing_error("DOCX package is invalid")
        relationship = "external-hyperlink"
    runs = list(element)
    if not runs:
        raise _parsing_error("DOCX package is invalid")
    texts: list[str] = []
    xml_space = "{http://www.w3.org/XML/1998/namespace}space"
    state = "normal"
    field_result_visible = False
    for run in runs:
        run_attributes = {_w_attr("rsidR"), _w_attr("rsidRPr")}
        if (
            run.tag != _q(_W_NS, "r")
            or not set(run.attrib).issubset(run_attributes)
            or any(
                _HYPERLINK_RSID_RE.fullmatch(value) is None
                for value in run.attrib.values()
            )
        ):
            raise _parsing_error("DOCX package is invalid")
        children = list(run)
        has_properties = False
        if children and children[0].tag == _q(_W_NS, "rPr"):
            props = children.pop(0)
            _validate_hyperlink_run_properties(props)
            has_properties = True
        if not children:
            if has_properties:
                continue
            raise _parsing_error("DOCX package is invalid")
        for token in children:
            if token.tag == _q(_W_NS, "fldChar"):
                type_attribute = _w_attr("fldCharType")
                if (
                    set(token.attrib) != {type_attribute}
                    or list(token)
                    or token.text
                    or token.tail
                ):
                    raise _parsing_error("DOCX package is invalid")
                field_type = token.attrib[type_attribute]
                if state == "normal" and field_type == "begin":
                    state = "expect-instruction"
                elif state == "expect-separate" and field_type == "separate":
                    state = "result"
                    field_result_visible = False
                elif state == "result" and field_type == "end" and field_result_visible:
                    state = "normal"
                else:
                    raise _parsing_error("DOCX package is invalid")
                continue
            if token.tag == _q(_W_NS, "instrText"):
                if (
                    state != "expect-instruction"
                    or token.attrib != {xml_space: "preserve"}
                    or list(token)
                    or token.tail
                    or type(token.text) is not str
                    or len(token.text) > 1024
                    or _HYPERLINK_PAGEREF_RE.fullmatch(token.text) is None
                ):
                    raise _parsing_error("DOCX package is invalid")
                state = "expect-separate"
                continue
            if state in {"expect-instruction", "expect-separate"}:
                raise _parsing_error("DOCX package is invalid")
            if token.tag == _q(_W_NS, "t"):
                if (
                    set(token.attrib) not in (set(), {xml_space})
                    or (
                        xml_space in token.attrib
                        and token.attrib[xml_space] != "preserve"
                    )
                    or list(token)
                ):
                    raise _parsing_error("DOCX package is invalid")
                value = token.text or ""
                texts.append(value)
                if state == "result" and value.strip():
                    field_result_visible = True
                continue
            if token.tag in {
                _q(_W_NS, "tab"),
                _q(_W_NS, "lastRenderedPageBreak"),
            }:
                if token.attrib or list(token) or token.text or token.tail:
                    raise _parsing_error("DOCX package is invalid")
                if token.tag == _q(_W_NS, "tab"):
                    texts.append("\t")
                continue
            else:
                raise _parsing_error("DOCX package is invalid")
    if state != "normal":
        raise _parsing_error("DOCX package is invalid")
    text = "".join(texts).strip()
    if not text:
        raise _parsing_error("DOCX package is invalid")
    return _HyperlinkValue(text=text, relationship=relationship)


@dataclass(frozen=True)
class _DrawingValue:
    kind: str
    text: str = ""
    attributes: Mapping[str, Any] | None = None


def _drawing_value(
    drawing: ET.Element,
    relationships: Mapping[str, Mapping[str, str]],
    package: ValidatedDocxPackage,
    limits: DocxLimits,
) -> _DrawingValue:
    graphic_data_parts = drawing.findall(f".//{_q(_A_NS, 'graphicData')}")
    if len(graphic_data_parts) != 1:
        raise _parsing_error("DOCX package is invalid")
    graphic_data = graphic_data_parts[0]
    if set(graphic_data.attrib) != {"uri"}:
        raise _parsing_error("DOCX package is invalid")
    role = graphic_data.attrib["uri"]
    if role == DIAGRAM_GRAPHIC_URI:
        children = list(graphic_data)
        if children:
            if (
                len(children) != 1
                or children[0].tag != _q(_DGM_NS, "relIds")
                or list(children[0])
                or children[0].text
            ):
                raise _parsing_error("DOCX package is invalid")
            expected_attributes = {
                _q(_R_NS, name): relation_type
                for name, relation_type in DIAGRAM_RELATIONSHIPS
            }
            if set(children[0].attrib) != set(expected_attributes):
                raise _parsing_error("DOCX package is invalid")
            for attribute, relation_type in expected_attributes.items():
                relation_id = children[0].attrib[attribute]
                relation = relationships.get(relation_id)
                if (
                    not relation_id
                    or relation is None
                    or relation.get("Type") != relation_type
                    or relation.get("TargetMode") != ""
                    or relation.get("Target") not in package.parts
                ):
                    raise _parsing_error("DOCX package is invalid")
        return _DrawingValue(kind="inventory")
    if role == WORDPROCESSING_SHAPE_GRAPHIC_URI:
        shape_children = list(graphic_data)
        if len(shape_children) != 1 or shape_children[0].tag != _q(_WPS_NS, "wsp"):
            raise _parsing_error("DOCX package is invalid")
        contents = shape_children[0].findall(f".//{_q(_W_NS, 'txbxContent')}")
        if len(contents) != 1:
            raise _parsing_error("DOCX package is invalid")
        text_parts: list[str] = []
        for paragraph in list(contents[0]):
            if paragraph.tag != _q(_W_NS, "p"):
                raise _parsing_error("DOCX package is invalid")
            paragraph_parts: list[str] = []
            for run in list(paragraph):
                if run.tag == _q(_W_NS, "pPr"):
                    continue
                if run.tag != _q(_W_NS, "r"):
                    raise _parsing_error("DOCX package is invalid")
                for token in list(run):
                    if token.tag == _q(_W_NS, "rPr"):
                        continue
                    if token.tag != _q(_W_NS, "t") or list(token):
                        raise _parsing_error("DOCX package is invalid")
                    paragraph_parts.append(token.text or "")
            paragraph_text = "".join(paragraph_parts).strip()
            if paragraph_text:
                text_parts.append(paragraph_text)
        text = "\n".join(text_parts)
        if len(text) > limits.maximum_text_characters:
            raise _parsing_error("DOCX package is invalid")
        return _DrawingValue(
            kind="inline-text" if text else "inventory", text=text
        )
    if role != PICTURE_GRAPHIC_URI:
        raise _parsing_error("DOCX package is invalid")

    blips = graphic_data.findall(f".//{_q(_A_NS, 'blip')}")
    if len(blips) != 1:
        raise _parsing_error("DOCX package is invalid")
    blip = blips[0]
    embed_attribute = _q(_R_NS, "embed")
    link_attribute = _q(_R_NS, "link")
    cstate_attribute = "cstate"
    if (
        not set(blip.attrib).issubset(
            {embed_attribute, link_attribute, cstate_attribute}
        )
        or (
            cstate_attribute in blip.attrib
            and blip.attrib[cstate_attribute] != "print"
        )
    ):
        raise _parsing_error("DOCX package is invalid")
    embed_id = blip.attrib.get(embed_attribute)
    link_id = blip.attrib.get(link_attribute)
    if embed_id is None and link_id is None:
        raise _parsing_error("DOCX package is invalid")

    def relationship(relation_id: str | None, *, external: bool) -> Mapping[str, str]:
        if type(relation_id) is not str or not relation_id or relation_id not in relationships:
            raise _parsing_error("DOCX package is invalid")
        relation = relationships[relation_id]
        if relation.get("Type") != _IMAGE_REL:
            raise _parsing_error("DOCX package is invalid")
        expected_mode = "External" if external else ""
        if relation.get("TargetMode") != expected_mode:
            raise _parsing_error("DOCX package is invalid")
        if external and relation.get("Target") != "":
            raise _parsing_error("DOCX package is invalid")
        return relation

    embedded_relation = (
        relationship(embed_id, external=False) if embed_id is not None else None
    )
    if link_id is not None:
        relationship(link_id, external=True)
    if embedded_relation is None:
        return _DrawingValue(
            kind="image", attributes={"relationship": "external-linked-image"}
        )
    target = embedded_relation.get("Target")
    if type(target) is not str or target not in package.parts:
        raise _parsing_error("DOCX package is invalid")
    extent = drawing.find(f".//{_q(_WP_NS, 'extent')}")
    attributes: dict[str, Any] = {
        "relationship": "embedded-image",
        "content_type": package.content_types[target],
        "digest": "sha256:" + sha256(package.parts[target]).hexdigest(),
    }
    if extent is not None:
        raw_width = extent.attrib.get("cx")
        raw_height = extent.attrib.get("cy")
        if type(raw_width) is str and raw_width.isdigit():
            attributes["width"] = int(raw_width)
        if type(raw_height) is str and raw_height.isdigit():
            attributes["height"] = int(raw_height)
    return _DrawingValue(kind="image", attributes=attributes)


def _cell_text_and_tables(
    cell: ET.Element,
    package: ValidatedDocxPackage,
    limits: DocxLimits,
) -> tuple[str, list[dict[str, Any]]]:
    text_parts: list[str] = []
    nested_tables: list[dict[str, Any]] = []
    nested_index = 0
    for child in list(cell):
        if child.tag == _q(_W_NS, "p"):
            text = _text_from_runs(child).strip()
            if text:
                text_parts.append(text)
        elif child.tag == _q(_W_NS, "tbl"):
            table_text, table_attrs, _ = _table_text_and_attributes(
                child, package, limits
            )
            nested_index += 1
            nested_tables.append(table_attrs)
            if table_text:
                text_parts.append(table_text)
    return "\n".join(text_parts), nested_tables


def _table_text_and_attributes(
    table: ET.Element,
    package: ValidatedDocxPackage,
    limits: DocxLimits,
) -> tuple[str, dict[str, Any], dict[str, int]]:
    grid = table.find(_q(_W_NS, "tblGrid"))
    column_count = 0 if grid is None else len(grid.findall(_q(_W_NS, "gridCol")))
    rows: list[dict[str, Any]] = []
    text_rows: list[str] = []
    open_merges: dict[int, dict[str, Any]] = {}
    for row_element in table.findall(_q(_W_NS, "tr")):
        row_cells: list[dict[str, Any]] = []
        row_texts: list[str] = []
        next_merges: dict[int, dict[str, Any]] = {}
        column_index = 0
        for cell in row_element.findall(_q(_W_NS, "tc")):
            props = cell.find(_q(_W_NS, "tcPr"))
            col_span = 1
            row_continue = False
            row_restart = False
            if props is not None:
                grid_span = props.find(_q(_W_NS, "gridSpan"))
                if grid_span is not None:
                    raw_span = grid_span.attrib.get(_w_attr("val"))
                    if type(raw_span) is str and raw_span.isdigit():
                        col_span = max(1, int(raw_span))
                vmerge = props.find(_q(_W_NS, "vMerge"))
                if vmerge is not None:
                    raw_merge = vmerge.attrib.get(_w_attr("val"), "")
                    if raw_merge == "restart":
                        row_restart = True
                    else:
                        row_continue = True
            cell_text, nested_tables = _cell_text_and_tables(cell, package, limits)
            if row_continue and column_index in open_merges:
                origin = open_merges[column_index]
                origin["row_span"] += 1
                for position in range(column_index, column_index + col_span):
                    next_merges[position] = origin
                column_index += col_span
                continue
            cell_record: dict[str, Any] = {
                "text": cell_text,
                "col_span": col_span,
                "row_span": 1,
            }
            if nested_tables:
                cell_record["nested_tables"] = nested_tables
            row_cells.append(cell_record)
            if cell_text:
                row_texts.append(cell_text)
            if row_restart:
                for position in range(column_index, column_index + col_span):
                    next_merges[position] = cell_record
            column_index += col_span
        rows.append({"cells": row_cells})
        if row_texts:
            text_rows.append("\t".join(row_texts))
        open_merges = next_merges
        column_count = max(column_count, column_index)
    nested_table_count = sum(
        len(cell.get("nested_tables", []))
        for row in rows
        for cell in row["cells"]
    )
    return (
        "\n".join(text_rows),
        {
            "column_count": column_count,
            "rows": rows,
            "nested_table_count": nested_table_count,
        },
        {
            "row_start": 0,
            "row_end": max(0, len(rows) - 1),
            "cell_start": 0,
            "cell_end": max(0, max((len(row["cells"]) for row in rows), default=0) - 1),
        },
    )


def _source_contract(source: Mapping[str, Any], maximum_bytes: int) -> dict[str, Any]:
    if not isinstance(source, Mapping) or set(source) != {"resource", "digest", "media_type", "data"}:
        raise _parsing_error("DOCX source contract is invalid")
    resource = source.get("resource")
    digest = source.get("digest")
    media_type = source.get("media_type")
    data = source.get("data")
    if (
        type(resource) is not str
        or not resource
        or "://" in resource
        or "\\" in resource
        or "\x00" in resource
        or resource.startswith("/")
        or "/../" in f"/{resource}"
        or not resource.endswith(".docx")
        or media_type != DOCX_MIME
        or type(data) is not bytes
        or not data
        or len(data) > maximum_bytes
    ):
        raise _parsing_error("DOCX source contract is invalid")
    actual_digest = "sha256:" + sha256(data).hexdigest()
    if not hmac.compare_digest(_strict_sha256_digest(digest), actual_digest):
        raise _parsing_error("DOCX source contract is invalid")
    return {
        "resource": resource,
        "digest": actual_digest,
        "media_type": DOCX_MIME,
        "data": data,
    }


class _IrBuilder:
    def __init__(self, source: Mapping[str, Any], configuration_digest: str, inventory_digest: str) -> None:
        self._source = source
        self._configuration_digest = configuration_digest
        self._blocks: list[dict[str, Any]] = []
        self._offset = 0
        self._inventory_digest = inventory_digest

    def add_block(
        self,
        block_type: str,
        text: str,
        span_fields: Mapping[str, Any],
        *,
        level: int | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        span = {"start": self._offset, "end": self._offset + len(text), **dict(span_fields)}
        block: dict[str, Any] = {
            "id": f"b{len(self._blocks) + 1}",
            "type": block_type,
            "text": text,
            "source_span": span,
        }
        if level is not None:
            block["level"] = level
        if attributes is not None:
            block["attributes"] = dict(attributes)
        self._blocks.append(block)
        self._offset = span["end"] + 1

    def document_ir(self) -> dict[str, Any]:
        return {
            "schema_version": "ao.lore.document-ir.v0.1",
            "document_id": self._source["digest"],
            "source": {
                "resource": self._source["resource"],
                "digest": self._source["digest"],
                "media_type": self._source["media_type"],
            },
            "parser": {
                "parser_id": DOCX_PARSER_ID,
                "parser_version": DOCX_PARSER_VERSION,
                "configuration_digest": self._configuration_digest,
            },
            "blocks": copy.deepcopy(self._blocks),
            "metadata": {
                "format": "docx",
                "inventory_digest": self._inventory_digest,
            },
        }


class NativeDocxOoxmlAdapter:
    def __init__(
        self,
        benchmark: Mapping[str, Any],
        *,
        limits: DocxLimits = DocxLimits(),
        expected_corpus_digest: str | None = None,
    ) -> None:
        checked_limits = _require_limits(limits)
        configuration = docx_configuration_digest(checked_limits)
        if expected_corpus_digest is None:
            raise _parsing_error("DOCX benchmark evidence is invalid")
        validated = validate_docx_benchmark(
            benchmark,
            expected_configuration_digest=configuration,
            expected_corpus_digest=expected_corpus_digest,
        )
        if validated["decision"] != "hold":
            raise _parsing_error("DOCX benchmark evidence is not qualifying")
        from .parsing import base_capability

        capability = base_capability(
            DOCX_PARSER_ID,
            DOCX_PARSER_VERSION,
            [DOCX_MIME],
            [".docx"],
            ["headings", "links", "tables", "footnotes", "images"],
        )
        capability.update(
            {
                "table_extraction": "structured",
                "footnote_preservation": True,
                "image_handling": True,
                "ocr_capable": False,
                "layout_aware": False,
                "source_coordinates": False,
                "max_file_bytes": checked_limits.maximum_archive_bytes,
                "benchmark_result_refs": [validated["result_digest"]],
                "benchmark_evidence_status": "verified",
            }
        )
        capability["selection_metrics"].update(
            {
                "text_fidelity": validated["metrics"]["text_fidelity"],
                "structural_fidelity": validated["metrics"]["structural_fidelity"],
                "source_location_fidelity": validated["metrics"]["source_location_fidelity"],
                "reliability": 1.0,
                "deterministic_repeatability": validated["repeatability"]["score"],
            }
        )
        self._limits = checked_limits
        self._benchmark = validated
        self._configuration_digest = configuration
        self._capability = capability

    @property
    def capability(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._capability)

    def parse(self, source: Mapping[str, Any]) -> Any:
        from .parsing import ParseOutput

        bound_source = _source_contract(source, self._limits.maximum_archive_bytes)
        try:
            package = validate_docx_package(bound_source["data"], self._limits)
        except DocxPackageError as exc:
            rejection_code = getattr(exc, "rejection_code", None)
            if rejection_code not in DOCX_STABLE_FAILURE_CATEGORIES:
                rejection_code = None
            raise _parsing_error(
                "DOCX package is invalid",
                exc,
                rejection_code=rejection_code,
            ) from exc
        styles = _style_levels(package, self._limits)
        numbering = _numbering_formats(package, self._limits)
        relationships = _document_relationships(package)
        footnotes = _notes_by_kind(
            package,
            "word/footnotes.xml",
            _q(_W_NS, "footnotes"),
            _q(_W_NS, "footnote"),
            self._limits,
        )
        endnotes = _notes_by_kind(
            package,
            "word/endnotes.xml",
            _q(_W_NS, "endnotes"),
            _q(_W_NS, "endnote"),
            self._limits,
        )
        root = _xml_part_root(package, "word/document.xml", self._limits)
        body = root.find(_q(_W_NS, "body"))
        if body is None:
            raise _parsing_error("DOCX package is invalid")
        builder = _IrBuilder(bound_source, self._configuration_digest, package.inventory_digest)
        paragraph_index = 0
        table_index = 0
        for child in list(body):
            if child.tag == _q(_W_NS, "p"):
                block_type, heading_level, list_attributes = _paragraph_style_and_numbering(
                    child, styles, numbering
                )
                plain_parts: list[str] = []

                def flush_plain() -> None:
                    text = "".join(plain_parts).strip()
                    plain_parts.clear()
                    if not text:
                        return
                    builder.add_block(
                        block_type,
                        text,
                        {"part": "word/document.xml", "paragraph": paragraph_index},
                        level=heading_level,
                        attributes=list_attributes,
                    )

                for run in list(child):
                    if run.tag == _q(_W_NS, "pPr"):
                        continue
                    if run.tag == _q(_W_NS, "hyperlink"):
                        flush_plain()
                        hyperlink = _hyperlink_value(run, relationships)
                        builder.add_block(
                            "link",
                            hyperlink.text,
                            {"part": "word/document.xml", "paragraph": paragraph_index},
                            attributes={"relationship": hyperlink.relationship},
                        )
                        continue
                    if run.tag != _q(_W_NS, "r"):
                        continue
                    for token in list(run):
                        if token.tag == _q(_W_NS, "t"):
                            plain_parts.append(token.text or "")
                        elif token.tag == _q(_W_NS, "footnoteReference"):
                            flush_plain()
                            note_id = token.attrib.get(_w_attr("id"))
                            if type(note_id) is not str or note_id not in footnotes:
                                raise _parsing_error("DOCX package is invalid")
                            builder.add_block(
                                "footnote",
                                footnotes[note_id],
                                {
                                    "part": "word/footnotes.xml",
                                    "paragraph": paragraph_index,
                                    "note_kind": "footnote",
                                    "note_id": note_id,
                                },
                                attributes={
                                    "note_kind": "footnote",
                                    "note_id": note_id,
                                    "reference_paragraph": paragraph_index,
                                },
                            )
                        elif token.tag == _q(_W_NS, "endnoteReference"):
                            flush_plain()
                            note_id = token.attrib.get(_w_attr("id"))
                            if type(note_id) is not str or note_id not in endnotes:
                                raise _parsing_error("DOCX package is invalid")
                            builder.add_block(
                                "footnote",
                                endnotes[note_id],
                                {
                                    "part": "word/endnotes.xml",
                                    "paragraph": paragraph_index,
                                    "note_kind": "endnote",
                                    "note_id": note_id,
                                },
                                attributes={
                                    "note_kind": "endnote",
                                    "note_id": note_id,
                                    "reference_paragraph": paragraph_index,
                                },
                            )
                        elif token.tag == _q(_W_NS, "drawing"):
                            drawing = _drawing_value(
                                token, relationships, package, self._limits
                            )
                            if drawing.kind == "inline-text":
                                plain_parts.append(drawing.text)
                            elif drawing.kind == "image":
                                flush_plain()
                                builder.add_block(
                                    "image",
                                    "",
                                    {
                                        "part": "word/document.xml",
                                        "paragraph": paragraph_index,
                                    },
                                    attributes=drawing.attributes,
                                )
                flush_plain()
                paragraph_index += 1
            elif child.tag == _q(_W_NS, "tbl"):
                table_text, table_attributes, table_span = _table_text_and_attributes(
                    child,
                    package,
                    self._limits,
                )
                builder.add_block(
                    "table",
                    table_text,
                    {"part": "word/document.xml", "table": table_index, **table_span},
                    attributes=table_attributes,
                )
                table_index += 1
        document_ir = builder.document_ir()
        return ParseOutput(
            document_ir,
            {
                "text_coverage": 1.0 if document_ir["blocks"] else 0.0,
                "structural_completeness": 1.0 if document_ir["blocks"] else 0.0,
                "source_span_coverage": 1.0 if document_ir["blocks"] else 0.0,
                "document_ir_valid": 1.0 if document_ir["blocks"] else 0.0,
            },
        )
