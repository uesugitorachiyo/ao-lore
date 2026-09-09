import json
import struct
import unittest
import zipfile
from hashlib import sha256, shake_256
from io import BytesIO
from pathlib import Path

from ao_lore import docx_ooxml as docx_ooxml_module
from ao_lore import private_docx_domain as private_docx_domain_module
from ao_lore.docx_ooxml import (
    DOCX_MIME,
    DocxLimits,
    DocxPackageError,
    NativeDocxOoxmlAdapter,
    ValidatedDocxPackage,
    docx_configuration_digest,
    validate_docx_package,
)
from ao_lore.parsing import ParsingError


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "ao_lore" / "docx"
SPEC_PATH = FIXTURE_ROOT / "fixture-spec.json"
FIXED_DATE = (2020, 1, 1, 0, 0, 0)
_EOCD = b"PK\x05\x06"
_CENTRAL = b"PK\x01\x02"
_LOCAL = b"PK\x03\x04"
_HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
_OFFICE_DOCUMENT_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
)
_ATTACHED_TEMPLATE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"
)
_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_ALTCHUNK_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"
)
_ACTIVEX_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/control"
)
_HEADER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header"
_STYLES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles"
_NUMBERING_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering"
_FOOTNOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes"
_ENDNOTES_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/endnotes"
_SETTINGS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings"
_FONT_TABLE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable"
_WEB_SETTINGS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/webSettings"
_CUSTOM_XML_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml"
_CUSTOM_XML_PROPS_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXmlProps"
)
_CUSTOM_XML_PROPS_CT = (
    "application/vnd.openxmlformats-officedocument.customXmlProperties+xml"
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
_DIAGRAM_RELATIONSHIPS = {
    "data": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramData",
    "layout": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramLayout",
    "quickStyle": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramQuickStyle",
    "colors": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/diagramColors",
}
_DIAGRAM_DRAWING_REL = "http://schemas.microsoft.com/office/2007/relationships/diagramDrawing"


def generator_module():
    import importlib.util

    path = FIXTURE_ROOT / "generate.py"
    spec = importlib.util.spec_from_file_location("ao_lore_docx_fixture_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _minimal_docx() -> bytes:
    generator = generator_module()
    return generator.build_docx_fixture(paragraphs=({"text": "evidence"},))


def _hyperlink_docx() -> bytes:
    generator = generator_module()
    return generator.build_docx_fixture(
        paragraphs=({"text": "evidence"},),
        hyperlinks=(("AO", "https://example.com/private"),),
    )


def _hyperlink_variant(attributes: bytes, children: bytes | None = None) -> bytes:
    body = _hyperlink_docx()
    document = _member_text(body, "word/document.xml")
    original_children = (
        b'<w:r><w:rPr><w:rStyle w:val="Hyperlink" /></w:rPr>'
        b'<w:t> Hyperlink</w:t></w:r>'
    )
    replacement_children = original_children if children is None else children
    document = document.replace(
        b'<w:hyperlink r:id="rIdLink1">' + original_children + b'</w:hyperlink>',
        b"<w:hyperlink " + attributes + b">" + replacement_children + b"</w:hyperlink>",
    )
    return _replace_member(body, "word/document.xml", new_data=document)


_VALID_HYPERLINK_RPR_LEAVES = (
    b"<w:b />",
    b'<w:b w:val="on" />',
    b"<w:bCs />",
    b'<w:bdr w:color="FF00AA" w:frame="1" w:space="2" w:sz="8" w:val="single" />',
    b'<w:caps w:val="off" />',
    b'<w:color w:val="auto" />',
    b"<w:i />",
    b"<w:iCs />",
    b'<w:lang w:eastAsia="zh-CN" w:val="en-US" />',
    b"<w:noProof />",
    (
        b'<w:rFonts w:ascii="Arial" w:asciiTheme="minorHAnsi" '
        b'w:cs="Arial" w:cstheme="minorBidi" w:eastAsia="SimSun" '
        b'w:eastAsiaTheme="minorEastAsia" w:hAnsi="Arial" '
        b'w:hAnsiTheme="minorHAnsi" />'
    ),
    b'<w:rStyle w:val="Hyperlink" />',
    b'<w:shd w:color="auto" w:fill="FFFFFF" w:val="clear" />',
    b"<w:snapToGrid />",
    b'<w:spacing w:val="-20" />',
    b'<w:sz w:val="22" />',
    b'<w:szCs w:val="22" />',
    b'<w:u w:val="single" />',
    b"<w:webHidden />",
)


def _hyperlink_run(
    *,
    properties: bytes = b'<w:rStyle w:val="Hyperlink" />',
    tokens: bytes = b"<w:t>Visible Link</w:t>",
    attributes: bytes = b"",
) -> bytes:
    run_attributes = b" " + attributes if attributes else b""
    properties_xml = b"<w:rPr>" + properties + b"</w:rPr>" if properties else b""
    return b"<w:r" + run_attributes + b">" + properties_xml + tokens + b"</w:r>"


def _formatting_only_run() -> bytes:
    return (
        b'<w:r w:rsidR="ABCDEF12" w:rsidRPr="1234ABCD">'
        b"<w:rPr><w:b /></w:rPr></w:r>"
    )


def _invalid_hyperlink_presentation_cases() -> dict[str, bytes]:
    invalid_rpr = {
        "duplicate-rpr": b"<w:b /><w:b />",
        "unknown-rpr": b"<w:emboss />",
        "nested-rpr": b"<w:b><w:t>hidden</w:t></w:b>",
        "rpr-text": b"text<w:b />",
        "rpr-attribute": b'<w:rPr w:val="x"><w:b /></w:rPr>',
        "bad-boolean": b'<w:b w:val="true" />',
        "bad-border-attr": b'<w:bdr w:themeColor="accent1" />',
        "bad-border-number": b'<w:bdr w:sz="01" />',
        "missing-color": b"<w:color />",
        "bad-lang": b'<w:lang w:val="en--US" />',
        "empty-fonts": b"<w:rFonts />",
        "bad-font-theme": b'<w:rFonts w:asciiTheme="minor HAnsi" />',
        "missing-style": b"<w:rStyle />",
        "empty-shading": b"<w:shd />",
        "bad-spacing": b'<w:spacing w:val="+2" />',
        "bad-size": b'<w:sz w:val="-1" />',
        "unknown-underline": b'<w:u w:color="FF0000" />',
    }
    cases = {
        "empty-tooltip": _hyperlink_variant(
            b'w:anchor="Bookmark" w:tooltip=""', _hyperlink_run()
        ),
        "control-tooltip": _hyperlink_variant(
            b'w:anchor="Bookmark" w:tooltip="private&#10;text"', _hyperlink_run()
        ),
        "overlong-tooltip": _hyperlink_variant(
            b'w:anchor="Bookmark" w:tooltip="' + b"x" * 1025 + b'"',
            _hyperlink_run(),
        ),
        "bad-frame-space": _hyperlink_variant(
            b'w:anchor="Bookmark" w:tgtFrame="new frame"', _hyperlink_run()
        ),
        "bad-frame-nonascii": _hyperlink_variant(
            'w:anchor="Bookmark" w:tgtFrame="främe"'.encode(), _hyperlink_run()
        ),
        "overlong-frame": _hyperlink_variant(
            b'w:anchor="Bookmark" w:tgtFrame="' + b"x" * 1025 + b'"',
            _hyperlink_run(),
        ),
        "lower-rsid": _hyperlink_variant(
            b'w:anchor="Bookmark"',
            _hyperlink_run(attributes=b'w:rsidR="abcdef12"'),
        ),
        "short-rsid": _hyperlink_variant(
            b'w:anchor="Bookmark"', _hyperlink_run(attributes=b'w:rsidR="ABCDEF1"')
        ),
        "nonhex-rsid": _hyperlink_variant(
            b'w:anchor="Bookmark"', _hyperlink_run(attributes=b'w:rsidRPr="ABCDEFGH"')
        ),
        "unknown-run-attr": _hyperlink_variant(
            b'w:anchor="Bookmark"', _hyperlink_run(attributes=b'w:hint="x"')
        ),
    }
    for label, properties in invalid_rpr.items():
        if label == "rpr-attribute":
            run = b"<w:r>" + properties + b"<w:t>Visible</w:t></w:r>"
        else:
            run = _hyperlink_run(properties=properties)
        cases[label] = _hyperlink_variant(b'w:anchor="Bookmark"', run)
    cases["misplaced-rpr"] = _hyperlink_variant(
        b'w:anchor="Bookmark"',
        b"<w:r><w:t>Visible</w:t><w:rPr><w:b /></w:rPr></w:r>",
    )
    return cases


def _pageref_runs(
    identifier: str,
    *,
    instruction: str | None = None,
    result: bytes = b"<w:t>12</w:t>",
) -> bytes:
    value = instruction if instruction is not None else f"PAGEREF {identifier} \\h"
    return (
        b'<w:r><w:fldChar w:fldCharType="begin" /></w:r>'
        + b'<w:r><w:instrText xml:space="preserve">'
        + value.encode("ascii")
        + b"</w:instrText></w:r>"
        + b'<w:r><w:fldChar w:fldCharType="separate" /></w:r>'
        + b"<w:r>"
        + result
        + b"</w:r>"
        + b'<w:r><w:fldChar w:fldCharType="end" /></w:r>'
    )


def _pageref_hyperlink(
    identifier: str = "_Toc12345678",
    *,
    instruction: str | None = None,
    result: bytes = b"<w:t>12</w:t>",
    prefix: bytes = b"",
    suffix: bytes = b"",
) -> bytes:
    return _hyperlink_variant(
        b'w:anchor="ReviewedBookmark" w:history="1"',
        prefix
        + _pageref_runs(identifier, instruction=instruction, result=result)
        + suffix,
    )


def _invalid_pageref_cases() -> dict[str, bytes]:
    begin = b'<w:r><w:fldChar w:fldCharType="begin" /></w:r>'
    instruction = (
        b'<w:r><w:instrText xml:space="preserve">'
        b"PAGEREF _Toc12345678 \\h"
        b"</w:instrText></w:r>"
    )
    separate = b'<w:r><w:fldChar w:fldCharType="separate" /></w:r>'
    result = b"<w:r><w:t>12</w:t></w:r>"
    end = b'<w:r><w:fldChar w:fldCharType="end" /></w:r>'

    def body(children: bytes) -> bytes:
        return _hyperlink_variant(b'w:anchor="ReviewedBookmark"', children)

    return {
        "orphan-instruction": body(instruction),
        "orphan-separate": body(separate),
        "orphan-end": body(end),
        "missing-instruction": body(begin + separate + result + end),
        "missing-separate": body(begin + instruction + result + end),
        "missing-result": body(begin + instruction + separate + end),
        "missing-end": body(begin + instruction + separate + result),
        "nested-begin": body(begin + begin + instruction + separate + result + end),
        "split-instruction": body(
            begin
            + b'<w:r><w:instrText xml:space="preserve">PAGEREF _Toc</w:instrText></w:r>'
            + b'<w:r><w:instrText xml:space="preserve">12345678 \\h</w:instrText></w:r>'
            + separate
            + result
            + end
        ),
        "wrong-keyword": _pageref_hyperlink(instruction="REF _Toc12345678 \\h"),
        "wrong-case": _pageref_hyperlink(instruction="pageref _Toc12345678 \\h"),
        "wrong-switch": _pageref_hyperlink(instruction="PAGEREF _Toc12345678 \\p"),
        "missing-switch": _pageref_hyperlink(instruction="PAGEREF _Toc12345678"),
        "bad-identifier-start": _pageref_hyperlink(identifier="1Bookmark"),
        "bad-identifier-hyphen": _pageref_hyperlink(identifier="Bad-Bookmark"),
        "quoted-identifier": _pageref_hyperlink(instruction='PAGEREF "Bookmark" \\h'),
        "extra-term": _pageref_hyperlink(instruction="PAGEREF Bookmark \\h extra"),
        "newline": _pageref_hyperlink(
            instruction="PAGEREF\n_Toc12345678 \\h"
        ),
        "identifier-1013": _pageref_hyperlink(identifier="A" * 1013),
        "instruction-1025": _pageref_hyperlink(
            identifier="A" * 1012,
            instruction=" PAGEREF " + "A" * 1012 + " \\h ",
        ),
        "fld-extra-attr": body(
            b'<w:r><w:fldChar w:fldCharType="begin" w:dirty="true" /></w:r>'
            + instruction
            + separate
            + result
            + end
        ),
        "fld-unknown-type": body(
            b'<w:r><w:fldChar w:fldCharType="locked" /></w:r>'
        ),
        "instr-missing-space": body(
            begin
            + b"<w:r><w:instrText>PAGEREF Bookmark \\h</w:instrText></w:r>"
            + separate
            + result
            + end
        ),
        "instr-extra-attr": body(
            begin
            + b'<w:r><w:instrText xml:space="preserve" w:dirty="1">PAGEREF Bookmark \\h</w:instrText></w:r>'
            + separate
            + result
            + end
        ),
        "instr-child": body(
            begin
            + b'<w:r><w:instrText xml:space="preserve"><w:t>hidden</w:t></w:instrText></w:r>'
            + separate
            + result
            + end
        ),
        "instr-tail": body(
            begin
            + b'<w:r><w:instrText xml:space="preserve">PAGEREF Bookmark \\h</w:instrText>tail</w:r>'
            + separate
            + result
            + end
        ),
        "text-before-instruction": body(
            begin
            + b"<w:r><w:t>hidden</w:t></w:r>"
            + instruction
            + separate
            + result
            + end
        ),
        "text-before-separate": body(
            begin
            + instruction
            + b"<w:r><w:t>hidden</w:t></w:r>"
            + separate
            + result
            + end
        ),
        "empty-visible-result": _pageref_hyperlink(result=b"<w:t>   </w:t>"),
        "inert-only-result": _pageref_hyperlink(
            result=b"<w:tab /><w:lastRenderedPageBreak />"
        ),
        "unknown-result-token": _pageref_hyperlink(result=b"<w:br />"),
    }


def _drawing_variant(uri: str, content: bytes) -> bytes:
    body = (FIXTURE_ROOT / "media-and-drawing.docx").read_bytes()
    document = _member_text(body, "word/document.xml")
    start = document.index(b"<a:graphicData ")
    end = document.index(b"</a:graphicData>", start) + len(b"</a:graphicData>")
    replacement = (
        f'<a:graphicData uri="{uri}">'.encode("ascii")
        + content
        + b"</a:graphicData>"
    )
    document = document[:start] + replacement + document[end:]
    return _replace_member(body, "word/document.xml", new_data=document)


def _blip_variant(kind: str, attributes: bytes) -> bytes:
    body = (FIXTURE_ROOT / "media-and-drawing.docx").read_bytes()
    document = _member_text(body, "word/document.xml").replace(
        b'r:embed="rIdMedia1"', attributes
    )
    relationships = _member_text(body, "word/_rels/document.xml.rels")
    if kind == "external":
        relationships = relationships.replace(
            b'Target="media/pixel.bin"',
            b'Target="cid:image001@example.invalid" TargetMode="External"',
        )
        body = _drop_member(body, "word/media/pixel.bin")
    elif kind == "dual":
        relationships = relationships.replace(
            b"</Relationships>",
            (
                b'<Relationship Id="rIdExternalImage" Type="'
                + _IMAGE_REL.encode("ascii")
                + b'" Target="cid:image001@example.invalid" '
                b'TargetMode="External" /></Relationships>'
            ),
        )
    elif kind != "embedded":
        raise AssertionError("invalid synthetic blip kind")
    body = _replace_member(body, "word/document.xml", new_data=document)
    return _replace_member(
        body, "word/_rels/document.xml.rels", new_data=relationships
    )


def _members(body: bytes) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    with zipfile.ZipFile(BytesIO(body)) as archive:
        for info in archive.infolist():
            result.append(
                {
                    "name": info.filename,
                    "data": archive.read(info.filename),
                    "compress_type": info.compress_type,
                    "external_attr": info.external_attr,
                    "flag_bits": info.flag_bits,
                }
            )
    return result


def _build_zip(entries: list[dict[str, object]]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for entry in entries:
            info = zipfile.ZipInfo(entry["name"], FIXED_DATE)
            info.compress_type = entry.get("compress_type", zipfile.ZIP_DEFLATED)
            info.external_attr = entry.get("external_attr", 0)
            info.flag_bits = entry.get("flag_bits", 0)
            info.create_system = 3
            archive.writestr(info, entry["data"], compress_type=info.compress_type)
    return buffer.getvalue()


def _replace_member(
    body: bytes,
    member_name: str,
    *,
    new_name: str | None = None,
    new_data: bytes | None = None,
    compress_type: int | None = None,
    external_attr: int | None = None,
    flag_bits: int | None = None,
) -> bytes:
    entries = _members(body)
    for entry in entries:
        if entry["name"] == member_name:
            if new_name is not None:
                entry["name"] = new_name
            if new_data is not None:
                entry["data"] = new_data
            if compress_type is not None:
                entry["compress_type"] = compress_type
            if external_attr is not None:
                entry["external_attr"] = external_attr
            if flag_bits is not None:
                entry["flag_bits"] = flag_bits
            break
    return _build_zip(entries)


def _drop_member(body: bytes, member_name: str) -> bytes:
    return _build_zip([entry for entry in _members(body) if entry["name"] != member_name])


def _add_member(
    body: bytes,
    member_name: str,
    data: bytes,
    *,
    compress_type: int = zipfile.ZIP_DEFLATED,
    external_attr: int = 0,
    flag_bits: int = 0,
) -> bytes:
    entries = _members(body)
    entries.append(
        {
            "name": member_name,
            "data": data,
            "compress_type": compress_type,
            "external_attr": external_attr,
            "flag_bits": flag_bits,
        }
    )
    return _build_zip(entries)


def _member_text(body: bytes, member_name: str) -> bytes:
    with zipfile.ZipFile(BytesIO(body)) as archive:
        return archive.read(member_name)


def _add_content_type_override(body: bytes, part_name: str, content_type: str) -> bytes:
    content_types = _member_text(body, "[Content_Types].xml")
    return _replace_member(
        body,
        "[Content_Types].xml",
        new_data=content_types.replace(
            b"</ns0:Types>",
            (
                f'<ns0:Override PartName="/{part_name}" ContentType="{content_type}" />'
                "</ns0:Types>"
            ).encode(),
        ),
    )


def _add_relationship(body: bytes, rels_name: str, relationship: bytes) -> bytes:
    relationships = _member_text(body, rels_name)
    return _replace_member(
        body,
        rels_name,
        new_data=relationships.replace(b"</Relationships>", relationship + b"</Relationships>"),
    )


def _benign_custom_xml_docx() -> bytes:
    body = _minimal_docx()
    body = _add_content_type_override(
        body, "customXml/itemProps1.xml", _CUSTOM_XML_PROPS_CT
    )
    body = _add_relationship(
        body,
        "word/_rels/document.xml.rels",
        (
            b'<Relationship Id="rIdCustom1" Type="'
            + _CUSTOM_XML_REL.encode()
            + b'" Target="../customXml/item1.xml" />'
        ),
    )
    body = _add_member(body, "customXml/item1.xml", b"<record><private-sentinel/></record>")
    body = _add_member(
        body,
        "customXml/itemProps1.xml",
        (
            b'<ds:datastoreItem ds:itemID="{00000000-0000-0000-0000-000000000001}" '
            b'xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml">'
            b'<ds:schemaRefs><ds:schemaRef ds:uri="urn:ao-lore:synthetic"/>'
            b"</ds:schemaRefs></ds:datastoreItem>"
        ),
    )
    body = _add_member(
        body,
        "customXml/_rels/item1.xml.rels",
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rIdProps1" Type="'
            + _CUSTOM_XML_PROPS_REL.encode()
            + b'" Target="itemProps1.xml" /></Relationships>'
        ),
    )
    return body


def _owned_obfuscated_font_docx(font_body: bytes) -> bytes:
    body = _minimal_docx()
    body = _add_content_type_override(
        body,
        "word/fontTable.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml",
    )
    body = _add_member(
        body,
        "word/fontTable.xml",
        b'<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
    )
    body = _add_relationship(
        body,
        "word/_rels/document.xml.rels",
        f'<Relationship Id="rIdFontTable" Type="{_FONT_TABLE_REL}" Target="fontTable.xml" />'.encode(),
    )
    body = _add_content_type_override(
        body,
        "word/fonts/font1.odttf",
        "application/vnd.openxmlformats-officedocument.obfuscatedFont",
    )
    body = _add_member(body, "word/fonts/font1.odttf", font_body)
    return _add_member(
        body,
        "word/_rels/fontTable.xml.rels",
        (
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            b'<Relationship Id="rIdFont1" Type="'
            + _FONT_REL.encode()
            + b'" Target="fonts/font1.odttf" /></Relationships>'
        ),
    )


def _synthetic_emf(*, declared_bytes: int = 88, signature: int = 0x464D4520) -> bytes:
    body = bytearray(88)
    struct.pack_into("<II", body, 0, 1, 88)
    struct.pack_into("<I", body, 40, signature)
    struct.pack_into("<II", body, 48, declared_bytes, 1)
    return bytes(body)


def _source(body: bytes, *, resource: str = "source-0123456789abcdef.docx") -> dict[str, object]:
    return {
        "resource": resource,
        "digest": "sha256:" + sha256(body).hexdigest(),
        "media_type": DOCX_MIME,
        "data": body,
    }


def qualification_manifest(
    *,
    decision: str = "hold",
    fixture_corpus_digest: str = "sha256:" + "a" * 64,
    configuration_digest: str | None = None,
    result_digest: str | None = None,
    metrics: dict[str, object] | None = None,
    repeatability: dict[str, object] | None = None,
    unexpected_failures: list[str] | None = None,
    stable_failures: dict[str, object] | None = None,
) -> dict[str, object]:
    manifest = {
        "schema_version": "ao.lore.docx-benchmark-result.v0.1",
        "fixture_corpus_digest": fixture_corpus_digest,
        "parser_id": "native-docx-ooxml",
        "parser_version": "1.0.0",
        "parser_configuration_digest": configuration_digest or docx_configuration_digest(DocxLimits()),
        "document_ir_version": "ao.lore.document-ir.v0.1",
        "attempts_per_document": 2,
        "document_count": 100,
        "metrics": metrics
        or {
            "text_fidelity": 1.0,
            "structural_fidelity": 1.0,
            "source_location_fidelity": 1.0,
            "expected_outcome_accuracy": 1.0,
            "expected_rejection_count": 12,
            "accepted_document_count": 88,
        },
        "exclusions": [],
        "stable_failures": stable_failures
        or {
            "invalid_package": 1,
            "unsupported_active_content": 11,
        },
        "repeatability": repeatability or {"runs": 2, "identical": True, "score": 1.0},
        "unexpected_failures": unexpected_failures or [],
        "decision": decision,
    }
    manifest["result_digest"] = result_digest or "sha256:" + sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return manifest


def _merged_table_docx() -> bytes:
    generator = generator_module()
    base = generator.build_docx_fixture(
        paragraphs=({"text": "Merged Table Fixture", "style": "Heading1"},),
        tables=((("placeholder",),),),
    )
    document_xml = (
        "<?xml version='1.0' encoding='utf-8'?>"
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        "<w:p><w:pPr><w:pStyle w:val=\"Heading1\" /></w:pPr><w:r><w:t>Merged Table Fixture</w:t></w:r></w:p>"
        "<w:tbl>"
        "<w:tblPr><w:tblStyle w:val=\"TableGrid\" /></w:tblPr>"
        "<w:tblGrid><w:gridCol w:w=\"2400\" /><w:gridCol w:w=\"2400\" /><w:gridCol w:w=\"2400\" /></w:tblGrid>"
        "<w:tr>"
        "<w:tc><w:p><w:r><w:t>Merged cell</w:t></w:r></w:p>"
        "<w:tcPr><w:tcW w:w=\"4800\" w:type=\"dxa\" /><w:gridSpan w:val=\"2\" /><w:vMerge w:val=\"restart\" /></w:tcPr></w:tc>"
        "<w:tc><w:p><w:r><w:t>Top right</w:t></w:r></w:p><w:tcPr><w:tcW w:w=\"2400\" w:type=\"dxa\" /></w:tcPr></w:tc>"
        "</w:tr>"
        "<w:tr>"
        "<w:tc><w:p /><w:tcPr><w:tcW w:w=\"4800\" w:type=\"dxa\" /><w:gridSpan w:val=\"2\" /><w:vMerge /></w:tcPr></w:tc>"
        "<w:tc><w:p><w:r><w:t>Bottom right</w:t></w:r></w:p><w:tcPr><w:tcW w:w=\"2400\" w:type=\"dxa\" /></w:tcPr></w:tc>"
        "</w:tr>"
        "</w:tbl>"
        "<w:sectPr><w:pgSz w:w=\"12240\" w:h=\"15840\" /></w:sectPr>"
        "</w:body>"
        "</w:document>"
    ).encode("utf-8")
    return _replace_member(base, "word/document.xml", new_data=document_xml)


def _custom_outline_level_docx() -> bytes:
    generator = generator_module()
    base = generator.build_docx_fixture(
        paragraphs=({"text": "Custom outline heading", "style": "CustomHeading"},),
    )
    styles_xml = (
        "<?xml version='1.0' encoding='utf-8'?>"
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal" /></w:style>'
        '<w:style w:type="paragraph" w:styleId="CustomHeading"><w:name w:val="Custom Heading" /><w:outlineLvl w:val="0" /></w:style>'
        "</w:styles>"
    ).encode("utf-8")
    document_xml = (
        "<?xml version='1.0' encoding='utf-8'?>"
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="CustomHeading" /></w:pPr><w:r><w:t>Custom outline heading</w:t></w:r></w:p>'
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840" /></w:sectPr>'
        "</w:body>"
        "</w:document>"
    ).encode("utf-8")
    return _replace_member(
        _replace_member(base, "word/styles.xml", new_data=styles_xml),
        "word/document.xml",
        new_data=document_xml,
    )


def _central_entries(body: bytes) -> list[dict[str, int]]:
    eocd = body.rfind(_EOCD)
    if eocd < 0:
        raise AssertionError("EOCD missing in test helper")
    count = struct.unpack_from("<H", body, eocd + 10)[0]
    offset = struct.unpack_from("<L", body, eocd + 16)[0]
    entries: list[dict[str, int]] = []
    cursor = offset
    for _ in range(count):
        if body[cursor : cursor + 4] != _CENTRAL:
            raise AssertionError("central header missing in test helper")
        fields = struct.unpack_from("<4s6H3L5H2L", body, cursor)
        name_length = fields[10]
        extra_length = fields[11]
        comment_length = fields[12]
        entries.append(
            {
                "central_offset": cursor,
                "flag_offset": cursor + 8,
                "method_offset": cursor + 10,
                "crc_offset": cursor + 16,
                "compressed_offset": cursor + 20,
                "uncompressed_offset": cursor + 24,
                "local_offset_offset": cursor + 42,
                "local_offset": fields[16],
            }
        )
        cursor += 46 + name_length + extra_length + comment_length
    return entries


def _patch_u16(body: bytes, offset: int, value: int) -> bytes:
    mutated = bytearray(body)
    struct.pack_into("<H", mutated, offset, value)
    return bytes(mutated)


def _patch_u32(body: bytes, offset: int, value: int) -> bytes:
    mutated = bytearray(body)
    struct.pack_into("<L", mutated, offset, value)
    return bytes(mutated)


def _patch_flag_bits(body: bytes, *, entry_index: int, mask: int) -> bytes:
    entries = _central_entries(body)
    local = entries[entry_index]["local_offset"]
    body = _patch_u16(body, entries[entry_index]["flag_offset"], mask)
    return _patch_u16(body, local + 6, mask)


def _patch_method(body: bytes, *, entry_index: int, method: int) -> bytes:
    entries = _central_entries(body)
    local = entries[entry_index]["local_offset"]
    body = _patch_u16(body, entries[entry_index]["method_offset"], method)
    return _patch_u16(body, local + 8, method)


def _patch_local_offset(body: bytes, *, entry_index: int, local_offset: int) -> bytes:
    entries = _central_entries(body)
    return _patch_u32(body, entries[entry_index]["local_offset_offset"], local_offset)


def _patch_local_uncompressed_size(body: bytes, *, entry_index: int, size: int) -> bytes:
    entries = _central_entries(body)
    local = entries[entry_index]["local_offset"]
    return _patch_u32(body, local + 22, size)


def _patch_local_crc(body: bytes, *, entry_index: int, crc: int) -> bytes:
    entries = _central_entries(body)
    local = entries[entry_index]["local_offset"]
    return _patch_u32(body, local + 14, crc)


def _patch_eocd_entries_on_disk(body: bytes, count: int) -> bytes:
    eocd = body.rfind(_EOCD)
    if eocd < 0:
        raise AssertionError("EOCD missing in test helper")
    return _patch_u16(body, eocd + 8, count)


def _patch_central_disk_start(body: bytes, *, entry_index: int, disk_start: int) -> bytes:
    entries = _central_entries(body)
    return _patch_u16(body, entries[entry_index]["central_offset"] + 34, disk_start)


def _prefix_zip(body: bytes, prefix: bytes) -> bytes:
    eocd = body.rfind(_EOCD)
    if eocd < 0:
        raise AssertionError("EOCD missing in test helper")
    entry_count = struct.unpack_from("<H", body, eocd + 10)[0]
    central_offset = struct.unpack_from("<L", body, eocd + 16)[0]
    shifted = bytearray(prefix + body)
    cursor = len(prefix) + central_offset
    for _ in range(entry_count):
        if shifted[cursor : cursor + 4] != _CENTRAL:
            raise AssertionError("central header missing in test helper")
        local_offset = struct.unpack_from("<L", shifted, cursor + 42)[0]
        struct.pack_into("<L", shifted, cursor + 42, local_offset + len(prefix))
        name_length = struct.unpack_from("<H", shifted, cursor + 28)[0]
        extra_length = struct.unpack_from("<H", shifted, cursor + 30)[0]
        comment_length = struct.unpack_from("<H", shifted, cursor + 32)[0]
        cursor += 46 + name_length + extra_length + comment_length
    struct.pack_into("<L", shifted, len(prefix) + eocd + 16, central_offset + len(prefix))
    return bytes(shifted)


class DocxSemanticContractTests(unittest.TestCase):
    def test_semantic_contract_is_closed_and_shared_without_parser_helpers(self):
        from ao_lore import docx_semantic_contract as contract

        self.assertEqual(
            (
                "paragraphs",
                "headings",
                "lists",
                "tables",
                "rows",
                "cells",
                "links",
                "footnotes",
                "endnotes",
                "drawings",
                "media",
            ),
            contract.COUNT_KEYS,
        )
        self.assertEqual("1", contract.HYPERLINK_HISTORY_TRUE)
        self.assertEqual(
            frozenset(
                {
                    contract.PICTURE_GRAPHIC_URI,
                    contract.DIAGRAM_GRAPHIC_URI,
                    contract.WORDPROCESSING_SHAPE_GRAPHIC_URI,
                }
            ),
            contract.GRAPHIC_URIS,
        )
        self.assertFalse(
            any(
                name.startswith(("parse", "normalize", "extract", "build"))
                for name in vars(contract)
            )
        )


class DocxPackageBoundaryTests(unittest.TestCase):
    def test_task1_fixtures_validate_and_result_is_detached(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        for fixture in corpus["fixtures"]:
            with self.subTest(fixture=fixture["id"]):
                body = (FIXTURE_ROOT / fixture["file"]).read_bytes()
                first = validate_docx_package(body)
                second = validate_docx_package(body)
                self.assertIsInstance(first, ValidatedDocxPackage)
                self.assertIn("[Content_Types].xml", first.parts)
                self.assertIn("_rels/.rels", first.parts)
                self.assertIn("word/document.xml", first.parts)
                self.assertGreater(first.expanded_bytes, 0)
                self.assertEqual(first.inventory_digest, second.inventory_digest)
                self.assertEqual(first.parts["word/document.xml"], second.parts["word/document.xml"])
                with self.assertRaises(TypeError):
                    first.parts["extra"] = b"x"  # type: ignore[index]
                with self.assertRaises(TypeError):
                    first.relationships["word/document.xml"] = ()  # type: ignore[index]

    def test_external_hyperlinks_are_allowed_as_inert_metadata_only(self):
        validated = validate_docx_package(_hyperlink_docx())
        links = [
            relation
            for relation in validated.relationships["word/document.xml"]
            if relation["Type"] == _HYPERLINK_REL
        ]
        self.assertEqual(1, len(links))
        self.assertEqual("External", links[0]["TargetMode"])
        self.assertEqual("https://example.com/private", links[0]["Target"])

    def test_inexact_inputs_and_invalid_limits_reject(self):
        valid = _minimal_docx()
        for body in (bytearray(valid), memoryview(valid), "not-bytes"):
            with self.subTest(body_type=type(body)):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)  # type: ignore[arg-type]

        invalid_limits = (
            DocxLimits(maximum_archive_bytes=0),
            DocxLimits(maximum_entries=True),  # type: ignore[arg-type]
            DocxLimits(maximum_member_bytes=0),
            DocxLimits(maximum_expanded_bytes=1, maximum_member_bytes=2),
            DocxLimits(maximum_compression_ratio=0),
            DocxLimits(maximum_xml_depth=0),
            DocxLimits(maximum_xml_elements=0),
            DocxLimits(maximum_text_characters=0),
            DocxLimits(maximum_aggregate_text_characters=0),
            DocxLimits(maximum_relationships=0),
            DocxLimits(maximum_blocks=0),
        )
        for limits in invalid_limits:
            with self.subTest(limits=limits):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(valid, limits)

    def test_duplicate_casefold_and_unsafe_names_reject(self):
        base = _minimal_docx()
        cases = {
            "duplicate": _add_member(base, "word/document.xml", b"duplicate"),
            "casefold": _add_member(base, "Word/Document.xml", b"duplicate"),
            "absolute": _replace_member(base, "word/document.xml", new_name="/word/document.xml"),
            "traversal": _replace_member(base, "word/document.xml", new_name="../document.xml"),
            "dot-segment": _replace_member(base, "word/document.xml", new_name="word/./document.xml"),
            "backslash": _replace_member(base, "word/document.xml", new_name=r"word\document.xml"),
            "control": _replace_member(base, "word/document.xml", new_name="word/\x00document.xml"),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_encrypted_special_modes_unsupported_compression_and_data_descriptor_reject(self):
        base = _minimal_docx()
        cases = {
            "encrypted": _patch_flag_bits(base, entry_index=0, mask=0x0001),
            "data-descriptor": _patch_flag_bits(base, entry_index=0, mask=0x0008),
            "unsupported-compression": _patch_method(base, entry_index=0, method=99),
            "symlink": _replace_member(
                base,
                "word/document.xml",
                external_attr=(0o120777 << 16),
            ),
            "special": _replace_member(
                base,
                "word/document.xml",
                external_attr=(0o020666 << 16),
            ),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_corrupt_offsets_overlap_and_header_inconsistency_reject(self):
        base = _hyperlink_docx()
        overlap = _patch_local_offset(base, entry_index=1, local_offset=_central_entries(base)[0]["local_offset"])
        impossible_offset = _patch_local_offset(base, entry_index=0, local_offset=len(base) + 128)
        bad_size = _patch_local_uncompressed_size(base, entry_index=0, size=1)
        bad_crc = _patch_local_crc(base, entry_index=0, crc=0)
        split_entry_count = _patch_eocd_entries_on_disk(base, 1)
        split_disk_start = _patch_central_disk_start(base, entry_index=0, disk_start=1)
        prefixed = _prefix_zip(base, b"MZPOLYGLOT")
        for label, body in (
            ("overlap", overlap),
            ("offset", impossible_offset),
            ("size", bad_size),
            ("crc", bad_crc),
            ("split-entry-count", split_entry_count),
            ("split-disk-start", split_disk_start),
            ("prefixed", prefixed),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_archive_member_expansion_ratio_xml_and_block_limits_reject(self):
        generator = generator_module()
        large_media = generator.build_docx_fixture(
            paragraphs=({"text": "evidence"},),
            media=(("pixel.bin", b"A" * 8192, "application/octet-stream"),),
        )
        two_blocks = generator.build_docx_fixture(
            paragraphs=({"text": "first"}, {"text": "second"}),
        )
        cases = (
            ("archive-bytes", _minimal_docx(), DocxLimits(maximum_archive_bytes=128)),
            ("entries", _minimal_docx(), DocxLimits(maximum_entries=3)),
            ("member-bytes", large_media, DocxLimits(maximum_member_bytes=32)),
            ("expanded", large_media, DocxLimits(maximum_expanded_bytes=256)),
            ("ratio", large_media, DocxLimits(maximum_compression_ratio=1)),
            ("xml-depth", _minimal_docx(), DocxLimits(maximum_xml_depth=2)),
            ("xml-elements", _minimal_docx(), DocxLimits(maximum_xml_elements=4)),
            ("text", _minimal_docx(), DocxLimits(maximum_text_characters=4)),
            ("relationships", _hyperlink_docx(), DocxLimits(maximum_relationships=1)),
            ("relationships-aggregate", _hyperlink_docx(), DocxLimits(maximum_relationships=4)),
            ("blocks", two_blocks, DocxLimits(maximum_blocks=1)),
        )
        for label, body, limits in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body, limits)

    def test_reviewed_per_part_text_limit_accepts_exact_boundary_only(self):
        per_part_limit = 24 * 1024 * 1024
        self.assertEqual(per_part_limit, DocxLimits().maximum_text_characters)
        prefix = (
            b'<w:document xmlns:w="http://schemas.openxmlformats.org/'
            b'wordprocessingml/2006/main">'
        )
        suffix = b"</w:document>"
        limits = DocxLimits(maximum_text_characters=per_part_limit)
        root, stats = docx_ooxml_module._xml_root(
            prefix + b"x" * per_part_limit + suffix,
            limits,
        )
        self.assertEqual("document", root.tag.rsplit("}", 1)[-1])
        self.assertEqual(per_part_limit, stats.text_characters)
        with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
            docx_ooxml_module._xml_root(
                prefix + b"x" * (per_part_limit + 1) + suffix,
                limits,
            )

    def test_reviewed_aggregate_text_limit_is_distinct_and_exact(self):
        per_part_limit = 24 * 1024 * 1024
        aggregate_limit = 28 * 1024 * 1024
        limits = DocxLimits()
        self.assertEqual(per_part_limit, limits.maximum_text_characters)
        self.assertEqual(
            aggregate_limit, limits.maximum_aggregate_text_characters
        )
        half = aggregate_limit // 2
        first = docx_ooxml_module._XmlStats(
            elements=1, text_characters=half, blocks=0
        )
        second = docx_ooxml_module._XmlStats(
            elements=1, text_characters=half, blocks=0
        )
        exact = docx_ooxml_module._accumulate_xml_stats(first, second, limits)
        self.assertEqual(aggregate_limit, exact.text_characters)
        self.assertLess(first.text_characters, per_part_limit)
        self.assertLess(second.text_characters, per_part_limit)
        with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
            docx_ooxml_module._accumulate_xml_stats(
                first,
                docx_ooxml_module._XmlStats(
                    elements=1, text_characters=half + 1, blocks=0
                ),
                limits,
            )
        self.assertNotEqual(
            docx_configuration_digest(limits),
            docx_configuration_digest(
                DocxLimits(
                    maximum_aggregate_text_characters=aggregate_limit - 1
                )
            ),
        )

    def test_missing_required_parts_content_types_and_office_document_binding_reject(self):
        base = _minimal_docx()
        missing_content_types = _drop_member(base, "[Content_Types].xml")
        missing_root_rels = _drop_member(base, "_rels/.rels")
        missing_document = _drop_member(base, "word/document.xml")
        duplicate_casefold_default = _replace_member(
            base,
            "[Content_Types].xml",
            new_data=_member_text(base, "[Content_Types].xml").replace(
                b"</ns0:Types>",
                b'<Default Extension="RELS" ContentType="application/x-bad"/></ns0:Types>',
            ),
        )
        duplicate_casefold_override = _replace_member(
            base,
            "[Content_Types].xml",
            new_data=_member_text(base, "[Content_Types].xml").replace(
                b"</ns0:Types>",
                b'<Override PartName="/WORD/STYLES.XML" ContentType="application/x-bad"/></ns0:Types>',
            ),
        )
        wrong_root_relationship = _replace_member(
            base,
            "_rels/.rels",
            new_data=_member_text(base, "_rels/.rels").replace(
                _OFFICE_DOCUMENT_REL.encode("utf-8"),
                b"http://example.com/not-office-document",
            ),
        )
        wrong_document_content_type = _replace_member(
            base,
            "[Content_Types].xml",
            new_data=_member_text(base, "[Content_Types].xml").replace(
                b"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                b"application/octet-stream",
            ),
        )
        for label, body in (
            ("content-types", missing_content_types),
            ("root-rels", missing_root_rels),
            ("document", missing_document),
            ("duplicate-casefold-default", duplicate_casefold_default),
            ("duplicate-casefold-override", duplicate_casefold_override),
            ("root-rel", wrong_root_relationship),
            ("document-type", wrong_document_content_type),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_relationship_target_resolution_and_active_content_reject(self):
        base = _hyperlink_docx()
        relationships = _member_text(base, "word/_rels/document.xml.rels")
        duplicate_id = _replace_member(
            base,
            "word/_rels/document.xml.rels",
            new_data=relationships.replace(
                b'Id="rIdLink1"',
                b'Id="rIdLink1"',
                1,
            ).replace(
                b"</Relationships>",
                (
                    b'<Relationship Id="rIdLink1" Type="'
                    + _HYPERLINK_REL.encode("utf-8")
                    + b'" Target="https://example.com/else" TargetMode="External"/></Relationships>'
                ),
            ),
        )
        duplicate_target = _replace_member(
            base,
            "word/_rels/document.xml.rels",
            new_data=relationships.replace(
                b"</Relationships>",
                (
                    b'<Relationship Id="rIdStyle2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
                    b'Target="styles.xml"/></Relationships>'
                ),
            ),
        )
        escaping_target = _replace_member(
            base,
            "word/_rels/document.xml.rels",
            new_data=relationships.replace(
                b'Target="https://example.com/private"',
                b'Target="../escape.xml"',
            ).replace(b'TargetMode="External"', b""),
        )
        missing_target = _replace_member(
            base,
            "word/_rels/document.xml.rels",
            new_data=relationships.replace(
                b'Target="https://example.com/private"',
                b'Target="media/missing.bin"',
            ).replace(b'TargetMode="External"', b""),
        )
        external_template = _replace_member(
            base,
            "word/_rels/document.xml.rels",
            new_data=relationships.replace(
                b"</Relationships>",
                (
                    b'<Relationship Id="rIdTemplate" Type="'
                    + _ATTACHED_TEMPLATE_REL.encode("utf-8")
                    + b'" Target="https://example.com/template.dotm" TargetMode="External"/></Relationships>'
                ),
            ),
        )
        external_image = _replace_member(
            base,
            "word/_rels/document.xml.rels",
            new_data=relationships.replace(
                b"</Relationships>",
                (
                    b'<Relationship Id="rIdImageExternal" Type="'
                    + _IMAGE_REL.encode("utf-8")
                    + b'" Target="https://example.com/image.png" TargetMode="External"/></Relationships>'
                ),
            ),
        )
        for label, body in (
            ("duplicate-id", duplicate_id),
            ("duplicate-target", duplicate_target),
            ("escaping-target", escaping_target),
            ("missing-target", missing_target),
            ("external-template", external_template),
            ("external-image", external_image),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_package_root_target_allows_one_leading_slash_only(self):
        base = _minimal_docx()
        root_rels = _member_text(base, "_rels/.rels")
        absolute = _replace_member(
            base,
            "_rels/.rels",
            new_data=root_rels.replace(
                b'Target="word/document.xml"', b'Target="/word/document.xml"'
            ),
        )
        package = validate_docx_package(absolute)
        office = [
            relation
            for relation in package.relationships["/"]
            if relation["Type"] == _OFFICE_DOCUMENT_REL
        ]
        self.assertEqual("word/document.xml", office[0]["Target"])

        unsafe_targets = (
            "//word/document.xml",
            "/word/./document.xml",
            "/word/../document.xml",
            "/word\\document.xml",
            "/word/document.xml?query",
            "/word/document.xml#fragment",
            "/word/\x01document.xml",
            "https://example.invalid/document.xml",
        )
        for target in unsafe_targets:
            with self.subTest(target=target):
                body = _replace_member(
                    base,
                    "_rels/.rels",
                    new_data=root_rels.replace(
                        b'Target="word/document.xml"',
                        f'Target="{target}"'.encode(),
                    ),
                )
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

        external = _replace_member(
            base,
            "_rels/.rels",
            new_data=root_rels.replace(
                b'Target="word/document.xml"',
                b'Target="/word/document.xml" TargetMode="External"',
            ),
        )
        with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
            validate_docx_package(external)

    def test_attached_template_in_header_relationship_part_has_closed_active_code(self):
        body = _minimal_docx()
        body = _add_content_type_override(
            body,
            "word/header1.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
        )
        body = _add_relationship(
            body,
            "word/_rels/document.xml.rels",
            (
                b'<Relationship Id="rIdHeader1" Type="'
                + _HEADER_REL.encode()
                + b'" Target="header1.xml" />'
            ),
        )
        body = _add_member(
            body,
            "word/header1.xml",
            b'<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        )
        body = _add_member(
            body,
            "word/_rels/header1.xml.rels",
            (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rIdTemplate" Type="'
                + _ATTACHED_TEMPLATE_REL.encode()
                + b'" Target="https://example.com/template.dotm" TargetMode="External"/>'
                b"</Relationships>"
            ),
        )

        with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid") as raised:
            validate_docx_package(body)
        self.assertEqual("unsupported_active_content", raised.exception.rejection_code)
        adapter = NativeDocxOoxmlAdapter(
            qualification_manifest(), expected_corpus_digest="sha256:" + "a" * 64
        )
        with self.assertRaises(ParsingError) as public_error:
            adapter.parse(_source(body))
        self.assertEqual("DOCX package is invalid", str(public_error.exception))
        self.assertEqual(
            "unsupported_active_content",
            getattr(public_error.exception, "rejection_code", None),
        )
        self.assertTrue(
            private_docx_domain_module._active_content(
                private_docx_domain_module._raw_parts(body)
            )
        )

        embedded = _add_member(_minimal_docx(), "word/embeddings/package1.docx", b"PK")
        self.assertTrue(
            private_docx_domain_module._active_content(
                private_docx_domain_module._raw_parts(embedded)
            )
        )
        with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid") as embedded_error:
            validate_docx_package(embedded)
        self.assertEqual(
            "unsupported_active_content", embedded_error.exception.rejection_code
        )

    def test_benign_custom_xml_datastore_is_inventory_only(self):
        body = _benign_custom_xml_docx()
        package = validate_docx_package(body)
        self.assertIn("customXml/item1.xml", package.parts)
        self.assertFalse(
            private_docx_domain_module._active_content(
                private_docx_domain_module._raw_parts(body)
            )
        )
        output = NativeDocxOoxmlAdapter(
            qualification_manifest(), expected_corpus_digest="sha256:" + "a" * 64
        ).parse(_source(body))
        self.assertNotIn("private-sentinel", json.dumps(output.document_ir, sort_keys=True))

    def test_custom_xml_allows_only_exact_leading_mso_content_type_pi(self):
        base = _benign_custom_xml_docx()
        safe_item = (
            b'<?xml version="1.0"?>'
            b"<?mso-contentType?>"
            b"<record><private-sentinel/></record>"
        )
        safe = _replace_member(base, "customXml/item1.xml", new_data=safe_item)
        package = validate_docx_package(safe)
        self.assertEqual(safe_item, package.parts["customXml/item1.xml"])
        output = NativeDocxOoxmlAdapter(
            qualification_manifest(), expected_corpus_digest="sha256:" + "a" * 64
        ).parse(_source(safe))
        self.assertNotIn("private-sentinel", json.dumps(output.document_ir, sort_keys=True))

        unsafe_items = {
            "stylesheet": b'<?xml-stylesheet href="file:///tmp/x"?><record/>',
            "unknown": b"<?unknown?><record/>",
            "data": b"<?mso-contentType value?><record/>",
            "attribute": b'<?mso-contentType value="x"?><record/>',
            "multiple": b"<?mso-contentType?><?mso-contentType?><record/>",
            "post-root": b"<record/><?mso-contentType?>",
            "overlong": b"<?mso-contentType " + b"x" * 4096 + b"?><record/>",
        }
        for label, item in unsafe_items.items():
            with self.subTest(label=label):
                body = _replace_member(base, "customXml/item1.xml", new_data=item)
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_custom_xml_item_properties_schema_refs_are_optional_but_exact(self):
        base = _benign_custom_xml_docx()
        props = _member_text(base, "customXml/itemProps1.xml")
        populated_container = (
            b'<ds:schemaRefs><ds:schemaRef ds:uri="urn:ao-lore:synthetic"/>'
            b"</ds:schemaRefs>"
        )
        absent = _replace_member(
            base,
            "customXml/itemProps1.xml",
            new_data=props.replace(populated_container, b""),
        )
        empty = _replace_member(
            base,
            "customXml/itemProps1.xml",
            new_data=props.replace(populated_container, b"<ds:schemaRefs/>"),
        )
        self.assertIn("customXml/itemProps1.xml", validate_docx_package(absent).parts)
        self.assertIn("customXml/itemProps1.xml", validate_docx_package(empty).parts)
        self.assertIn("customXml/itemProps1.xml", validate_docx_package(base).parts)

        malicious_containers = {
            "duplicate-containers": populated_container + populated_container,
            "container-attribute": b'<ds:schemaRefs extra="x"><ds:schemaRef ds:uri="urn:a"/></ds:schemaRefs>',
            "container-content": b"<ds:schemaRefs>content</ds:schemaRefs>",
            "container-tail": b"<ds:schemaRefs/>content",
            "other-child": b"<ds:other/>",
            "empty-uri": b'<ds:schemaRefs><ds:schemaRef ds:uri=""/></ds:schemaRefs>',
            "duplicate-uri": b'<ds:schemaRefs><ds:schemaRef ds:uri="urn:a"/><ds:schemaRef ds:uri="urn:a"/></ds:schemaRefs>',
            "non-uri-attribute": b'<ds:schemaRefs><ds:schemaRef ds:uri="urn:a" extra="x"/></ds:schemaRefs>',
            "nested-child": b'<ds:schemaRefs><ds:schemaRef ds:uri="urn:a"><ds:other/></ds:schemaRef></ds:schemaRefs>',
            "leaf-content": b'<ds:schemaRefs><ds:schemaRef ds:uri="urn:a">content</ds:schemaRef></ds:schemaRefs>',
        }
        for label, container in malicious_containers.items():
            with self.subTest(label=label):
                body = _replace_member(
                    base,
                    "customXml/itemProps1.xml",
                    new_data=props.replace(populated_container, container),
                )
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_external_custom_xml_relationship_rejects_without_widening(self):
        body = _benign_custom_xml_docx()
        rels = _member_text(body, "customXml/_rels/item1.xml.rels")
        body = _replace_member(
            body,
            "customXml/_rels/item1.xml.rels",
            new_data=rels.replace(
                b'Target="itemProps1.xml"',
                b'Target="https://example.com/itemProps1.xml" TargetMode="External"',
            ),
        )
        with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
            validate_docx_package(body)

    def test_custom_xml_allowlist_rejects_unpaired_unknown_and_wrong_content(self):
        base = _minimal_docx()
        unpaired = _add_member(base, "customXml/item1.xml", b"<record/>")
        unknown = _add_member(base, "customXml/unknown.bin", b"foreign")
        wrong_content = _replace_member(
            _benign_custom_xml_docx(),
            "[Content_Types].xml",
            new_data=_member_text(
                _benign_custom_xml_docx(), "[Content_Types].xml"
            ).replace(_CUSTOM_XML_PROPS_CT.encode(), b"application/xml"),
        )
        external_document_rel = _replace_member(
            _benign_custom_xml_docx(),
            "word/_rels/document.xml.rels",
            new_data=_member_text(
                _benign_custom_xml_docx(), "word/_rels/document.xml.rels"
            ).replace(
                b'Target="../customXml/item1.xml"',
                b'Target="https://example.com/item1.xml" TargetMode="External"',
            ),
        )
        for label, body in (
            ("unpaired", unpaired),
            ("unknown", unknown),
            ("wrong-content", wrong_content),
            ("external-document-rel", external_document_rel),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_closed_inert_word_roles_and_relationship_parts_accept(self):
        body = _minimal_docx()
        roles = (
            (
                "word/header1.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
                "header",
                b'<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
            ),
            (
                "word/footer1.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
                "footer",
                b'<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
            ),
            (
                "word/settings.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml",
                "settings",
                b'<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
            ),
            (
                "word/fontTable.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml",
                "fontTable",
                b'<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
            ),
            (
                "word/webSettings.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.webSettings+xml",
                "webSettings",
                b'<w:webSettings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
            ),
            (
                "word/theme/theme1.xml",
                "application/vnd.openxmlformats-officedocument.theme+xml",
                "theme",
                b'<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Public"/>',
            ),
        )
        for index, (name, content_type, relation_role, xml) in enumerate(roles, start=10):
            body = _add_content_type_override(body, name, content_type)
            body = _add_member(body, name, xml)
            body = _add_relationship(
                body,
                "word/_rels/document.xml.rels",
                (
                    f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/'
                    f'officeDocument/2006/relationships/{relation_role}" Target="{name.removeprefix("word/")}" />'
                ).encode(),
            )
        body = _add_content_type_override(body, "word/media/header.png", "image/png")
        body = _add_member(body, "word/media/header.png", b"synthetic-image")
        for relationship_part in (
            "word/_rels/header1.xml.rels",
            "word/_rels/footer1.xml.rels",
            "word/_rels/settings.xml.rels",
            "word/_rels/fontTable.xml.rels",
            "word/_rels/webSettings.xml.rels",
            "word/theme/_rels/theme1.xml.rels",
        ):
            relationship = b""
            if relationship_part == "word/_rels/header1.xml.rels":
                relationship = (
                    b'<Relationship Id="rIdImage1" Type="'
                    + _IMAGE_REL.encode()
                    + b'" Target="media/header.png" />'
                )
            body = _add_member(
                body,
                relationship_part,
                (
                    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    + relationship
                    + b"</Relationships>"
                ),
            )
        package = validate_docx_package(body)
        self.assertTrue(all(name in package.parts for name, *_ in roles))

    def test_styles_custom_properties_and_thumbnail_roles_are_inventory_only(self):
        body = _minimal_docx()
        roles = (
            (
                "word/stylesWithEffects.xml",
                "application/vnd.ms-word.stylesWithEffects+xml",
                b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:type="paragraph" w:styleId="sentinel"/></w:styles>',
                "word/_rels/document.xml.rels",
                _STYLES_WITH_EFFECTS_REL,
                "stylesWithEffects.xml",
            ),
            (
                "docProps/custom.xml",
                "application/vnd.openxmlformats-officedocument.custom-properties+xml",
                b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"/>',
                "_rels/.rels",
                _CUSTOM_PROPERTIES_REL,
                "docProps/custom.xml",
            ),
            (
                "docProps/thumbnail.jpeg",
                "image/jpeg",
                b"\xff\xd8\xff\xe0synthetic-thumbnail\xff\xd9",
                "_rels/.rels",
                _THUMBNAIL_REL,
                "docProps/thumbnail.jpeg",
            ),
        )
        for index, (name, content_type, content, owner, relation_type, target) in enumerate(roles, 20):
            body = _add_content_type_override(body, name, content_type)
            body = _add_member(body, name, content)
            body = _add_relationship(
                body,
                owner,
                f'<Relationship Id="rId{index}" Type="{relation_type}" Target="{target}" />'.encode(),
            )
        body = _add_member(
            body,
            "word/_rels/stylesWithEffects.xml.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        package = validate_docx_package(body)
        self.assertTrue(all(name in package.parts for name, *_ in roles))
        output = NativeDocxOoxmlAdapter(
            qualification_manifest(), expected_corpus_digest="sha256:" + "a" * 64
        ).parse(_source(body))
        self.assertNotIn("sentinel", json.dumps(output.document_ir, sort_keys=True))

    def test_jpeg_thumbnail_allows_only_bounded_zero_padding_after_unique_eoi(self):
        jpeg = b"\xff\xd8\xff\xe0synthetic-thumbnail\xff\xd9"

        def thumbnail(payload: bytes) -> bytes:
            body = _add_content_type_override(
                _minimal_docx(), "docProps/thumbnail.jpeg", "image/jpeg"
            )
            body = _add_member(body, "docProps/thumbnail.jpeg", payload)
            return _add_relationship(
                body,
                "_rels/.rels",
                f'<Relationship Id="rIdThumb" Type="{_THUMBNAIL_REL}" Target="docProps/thumbnail.jpeg"/>'.encode(),
            )

        for zero_padding in range(4):
            with self.subTest(zero_padding=zero_padding):
                package = validate_docx_package(
                    thumbnail(jpeg + b"\x00" * zero_padding)
                )
                self.assertIn("docProps/thumbnail.jpeg", package.parts)

        malicious = {
            "four-zero-padding": jpeg + b"\x00" * 4,
            "nonzero-tail": jpeg + b"\x00\x01",
            "multiple-eoi": jpeg + b"payload\xff\xd9",
            "zip-polyglot": jpeg + b"PK\x03\x04payload\xff\xd9",
            "executable-polyglot": jpeg + b"MZpayload\xff\xd9",
            "missing-eoi": jpeg[:-2],
        }
        for label, payload in malicious.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(thumbnail(payload))

    def test_glossary_tree_is_closed_and_inventory_only(self):
        body = _minimal_docx()
        glossary_roles = (
            ("word/glossary/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.glossary+xml", b'<w:glossaryDocument xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>', _GLOSSARY_REL, "glossary/document.xml"),
            ("word/glossary/styles.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml", b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>', _STYLES_REL, "styles.xml"),
            ("word/glossary/settings.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml", b'<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>', _SETTINGS_REL, "settings.xml"),
            ("word/glossary/fontTable.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml", b'<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>', _FONT_TABLE_REL, "fontTable.xml"),
            ("word/glossary/webSettings.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.webSettings+xml", b'<w:webSettings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>', _WEB_SETTINGS_REL, "webSettings.xml"),
        )
        for name, content_type, content, _, _ in glossary_roles:
            body = _add_content_type_override(body, name, content_type)
            body = _add_member(body, name, content)
        body = _add_relationship(
            body,
            "word/_rels/document.xml.rels",
            f'<Relationship Id="rIdGlossary" Type="{_GLOSSARY_REL}" Target="glossary/document.xml" />'.encode(),
        )
        glossary_rels = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        glossary_rels += b"".join(
            f'<Relationship Id="rIdGlossary{index}" Type="{relation_type}" Target="{target}" />'.encode()
            for index, (*_, relation_type, target) in enumerate(glossary_roles[1:], 1)
        )
        glossary_rels += b"</Relationships>"
        body = _add_member(body, "word/glossary/_rels/document.xml.rels", glossary_rels)
        package = validate_docx_package(body)
        self.assertTrue(all(name in package.parts for name, *_ in glossary_roles))

    def test_diagram_suite_is_closed_and_inventory_only(self):
        body = _minimal_docx()
        diagram_roles = (
            ("data", "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml", "dataModel"),
            ("layout", "application/vnd.openxmlformats-officedocument.drawingml.diagramLayout+xml", "layoutDef"),
            ("quickStyle", "application/vnd.openxmlformats-officedocument.drawingml.diagramStyle+xml", "styleDef"),
            ("colors", "application/vnd.openxmlformats-officedocument.drawingml.diagramColors+xml", "colorsDef"),
        )
        for index, (role, content_type, root_name) in enumerate(diagram_roles, 1):
            name = f"word/diagrams/{role}1.xml"
            body = _add_content_type_override(body, name, content_type)
            body = _add_member(
                body,
                name,
                f'<dgm:{root_name} xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"/>'.encode(),
            )
            body = _add_relationship(
                body,
                "word/_rels/document.xml.rels",
                f'<Relationship Id="rIdDiagram{index}" Type="{_DIAGRAM_RELATIONSHIPS[role]}" Target="diagrams/{role}1.xml" />'.encode(),
            )
        body = _add_content_type_override(
            body, "word/diagrams/drawing1.xml", "application/vnd.ms-office.drawingml.diagramDrawing+xml"
        )
        body = _add_member(
            body,
            "word/diagrams/drawing1.xml",
            b'<dsp:drawing xmlns:dsp="http://schemas.microsoft.com/office/drawing/2008/diagram"/>',
        )
        body = _add_relationship(
            body,
            "word/_rels/document.xml.rels",
            (
                b'<Relationship Id="rIdDiagramDrawing1" Type="'
                + _DIAGRAM_DRAWING_REL.encode()
                + b'" Target="diagrams/drawing1.xml" />'
            ),
        )
        body = _add_member(
            body,
            "word/diagrams/_rels/data1.xml.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        package = validate_docx_package(body)
        self.assertIn("word/diagrams/drawing1.xml", package.parts)

    def test_comments_extensions_ids_extensible_and_people_are_inventory_only(self):
        body = _minimal_docx()
        roles = (
            ("word/comments.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml", _COMMENTS_REL, b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>'),
            ("word/commentsExtended.xml", "application/vnd.ms-word.commentsExtended+xml", _COMMENTS_EXTENDED_REL, b'<w15:commentsEx xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"/>'),
            ("word/commentsIds.xml", "application/vnd.ms-word.commentsIds+xml", _COMMENTS_IDS_REL, b'<w16cid:commentsIds xmlns:w16cid="http://schemas.microsoft.com/office/word/2016/wordml/cid"/>'),
            ("word/commentsExtensible.xml", "application/vnd.ms-word.commentsExtensible+xml", _COMMENTS_EXTENSIBLE_REL, b'<w16cex:commentsExtensible xmlns:w16cex="http://schemas.microsoft.com/office/word/2018/wordml/cex"/>'),
            ("word/people.xml", "application/vnd.ms-word.people+xml", _PEOPLE_REL, b'<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"/>'),
        )
        for index, (name, content_type, relation_type, content) in enumerate(roles, 30):
            body = _add_content_type_override(body, name, content_type)
            body = _add_member(body, name, content)
            body = _add_relationship(
                body,
                "word/_rels/document.xml.rels",
                f'<Relationship Id="rId{index}" Type="{relation_type}" Target="{name.removeprefix("word/")}" />'.encode(),
            )
            body = _add_member(
                body,
                f'word/_rels/{name.removeprefix("word/")}.rels',
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
            )
        package = validate_docx_package(body)
        self.assertTrue(all(name in package.parts for name, *_ in roles))

    def test_comment_metadata_accepts_only_exact_openxml_alias_by_role(self):
        roles = (
            (
                "word/commentsExtended.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtended+xml",
                _COMMENTS_EXTENDED_REL,
                b'<w15:commentsEx xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"/>',
            ),
            (
                "word/commentsIds.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsIds+xml",
                _COMMENTS_IDS_REL,
                b'<w16cid:commentsIds xmlns:w16cid="http://schemas.microsoft.com/office/word/2016/wordml/cid"/>',
            ),
            (
                "word/commentsExtensible.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.commentsExtensible+xml",
                _COMMENTS_EXTENSIBLE_REL,
                b'<w16cex:commentsExtensible xmlns:w16cex="http://schemas.microsoft.com/office/word/2018/wordml/cex"/>',
            ),
            (
                "word/people.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.people+xml",
                _PEOPLE_REL,
                b'<w15:people xmlns:w15="http://schemas.microsoft.com/office/word/2012/wordml"/>',
            ),
        )

        def package_for(role: tuple[str, str, str, bytes], content_type: str) -> bytes:
            name, _, relationship_type, root = role
            body = _add_content_type_override(_minimal_docx(), name, content_type)
            body = _add_member(body, name, root)
            return _add_relationship(
                body,
                "word/_rels/document.xml.rels",
                (
                    f'<Relationship Id="rIdAlias" Type="{relationship_type}" '
                    f'Target="{name.removeprefix("word/")}" />'
                ).encode(),
            )

        for role in roles:
            with self.subTest(role=role[0]):
                self.assertIn(role[0], validate_docx_package(package_for(role, role[1])).parts)

        malicious = {
            "cross-role": package_for(roles[0], roles[1][1]),
            "unknown-alias": package_for(roles[0], "application/vnd.openxmlformats-officedocument.wordprocessingml.unknown+xml"),
            "base-comments-widened": package_for(
                (
                    "word/comments.xml",
                    "",
                    _COMMENTS_REL,
                    b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
                ),
                "application/vnd.ms-word.comments+xml",
            ),
        }
        for label, body in malicious.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_obfuscated_font_requires_font_table_ownership(self):
        body = _owned_obfuscated_font_docx(bytes(range(32)) + b"synthetic-font")
        self.assertIn("word/fonts/font1.odttf", validate_docx_package(body).parts)

    def test_note_and_numbering_relationship_parts_are_closed_by_source(self):
        body = _minimal_docx()
        roles = (
            (
                "word/footnotes.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
                b'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
                _FOOTNOTES_REL,
                "footnotes.xml",
            ),
            (
                "word/endnotes.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
                b'<w:endnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
                _ENDNOTES_REL,
                "endnotes.xml",
            ),
            (
                "word/numbering.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml",
                b'<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
                _NUMBERING_REL,
                "numbering.xml",
            ),
        )
        for index, (name, content_type, root, relation_type, target) in enumerate(roles, 1):
            body = _add_content_type_override(body, name, content_type)
            body = _add_member(body, name, root)
            body = _add_relationship(
                body,
                "word/_rels/document.xml.rels",
                f'<Relationship Id="rIdRole{index}" Type="{relation_type}" Target="{target}" />'.encode(),
            )
        body = _add_content_type_override(body, "word/media/note.bin", "application/octet-stream")
        body = _add_content_type_override(body, "word/media/bullet.bin", "application/octet-stream")
        body = _add_member(body, "word/media/note.bin", b"note-image")
        body = _add_member(body, "word/media/bullet.bin", b"bullet-image")
        body = _add_member(
            body,
            "word/_rels/footnotes.xml.rels",
            (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rIdFootLink" Type="'
                + _HYPERLINK_REL.encode()
                + b'" Target="https://example.invalid/footnote" TargetMode="External"/>'
                b"</Relationships>"
            ),
        )
        for source, target in (("endnotes", "media/note.bin"), ("numbering", "media/bullet.bin")):
            body = _add_member(
                body,
                f"word/_rels/{source}.xml.rels",
                (
                    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    b'<Relationship Id="rIdImage" Type="'
                    + _IMAGE_REL.encode()
                    + f'" Target="{target}"/>'.encode()
                    + b"</Relationships>"
                ),
            )
        package = validate_docx_package(body)
        self.assertIn("word/_rels/footnotes.xml.rels", package.parts)
        self.assertIn("word/_rels/endnotes.xml.rels", package.parts)
        self.assertIn("word/_rels/numbering.xml.rels", package.parts)

    def test_glossary_styles_effects_and_emf_thumbnail_are_inventory_only(self):
        glossary = _minimal_docx()
        glossary = _add_content_type_override(
            glossary,
            "word/glossary/document.xml",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.glossary+xml",
        )
        glossary = _add_content_type_override(
            glossary,
            "word/glossary/stylesWithEffects.xml",
            "application/vnd.ms-word.stylesWithEffects+xml",
        )
        glossary = _add_member(
            glossary,
            "word/glossary/document.xml",
            b'<w:glossaryDocument xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        )
        glossary = _add_member(
            glossary,
            "word/glossary/stylesWithEffects.xml",
            b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:style w:styleId="private-sentinel"/></w:styles>',
        )
        glossary = _add_relationship(
            glossary,
            "word/_rels/document.xml.rels",
            f'<Relationship Id="rIdGlossary" Type="{_GLOSSARY_REL}" Target="glossary/document.xml"/>'.encode(),
        )
        glossary = _add_member(
            glossary,
            "word/glossary/_rels/document.xml.rels",
            (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rIdEffects" Type="'
                + _STYLES_WITH_EFFECTS_REL.encode()
                + b'" Target="stylesWithEffects.xml"/></Relationships>'
            ),
        )
        self.assertIn(
            "word/glossary/stylesWithEffects.xml", validate_docx_package(glossary).parts
        )
        output = NativeDocxOoxmlAdapter(
            qualification_manifest(), expected_corpus_digest="sha256:" + "a" * 64
        ).parse(_source(glossary))
        self.assertNotIn("private-sentinel", json.dumps(output.document_ir, sort_keys=True))

        emf = _minimal_docx()
        emf = _add_content_type_override(emf, "docProps/thumbnail.emf", "image/x-emf")
        emf = _add_member(emf, "docProps/thumbnail.emf", _synthetic_emf())
        emf = _add_relationship(
            emf,
            "_rels/.rels",
            f'<Relationship Id="rIdEmf" Type="{_THUMBNAIL_REL}" Target="docProps/thumbnail.emf"/>'.encode(),
        )
        self.assertIn("docProps/thumbnail.emf", validate_docx_package(emf).parts)

    def test_auxiliary_relationship_and_emf_malicious_matrix_rejects(self):
        base = _minimal_docx()
        orphan_rels = _add_member(
            base,
            "word/_rels/footnotes.xml.rels",
            b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
        )
        def footnotes_with_relationship(relationship: bytes) -> bytes:
            body = _add_content_type_override(
                base,
                "word/footnotes.xml",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
            )
            body = _add_member(
                body,
                "word/footnotes.xml",
                b'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
            )
            body = _add_relationship(
                body,
                "word/_rels/document.xml.rels",
                f'<Relationship Id="rIdFootnotes" Type="{_FOOTNOTES_REL}" Target="footnotes.xml"/>'.encode(),
            )
            return _add_member(
                body,
                "word/_rels/footnotes.xml.rels",
                (
                    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    + relationship
                    + b"</Relationships>"
                ),
            )

        wrong_rel_type = footnotes_with_relationship(
            b'<Relationship Id="rIdBad" Type="'
            + _STYLES_REL.encode()
            + b'" Target="../styles.xml"/>'
        )
        wrong_mode = footnotes_with_relationship(
            b'<Relationship Id="rIdBad" Type="'
            + _HYPERLINK_REL.encode()
            + b'" Target="https://example.invalid/footnote"/>'
        )
        wrong_target = footnotes_with_relationship(
            b'<Relationship Id="rIdBad" Type="'
            + _IMAGE_REL.encode()
            + b'" Target="../styles.xml"/>'
        )
        glossary_cross_role = _minimal_docx()
        glossary_cross_role = _add_member(
            glossary_cross_role,
            "word/glossary/_rels/document.xml.rels",
            (
                b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                b'<Relationship Id="rIdBad" Type="'
                + _STYLES_WITH_EFFECTS_REL.encode()
                + b'" Target="../styles.xml"/></Relationships>'
            ),
        )

        def emf_package(payload: bytes) -> bytes:
            body = _add_content_type_override(
                _minimal_docx(), "docProps/thumbnail.emf", "image/x-emf"
            )
            body = _add_member(body, "docProps/thumbnail.emf", payload)
            return _add_relationship(
                body,
                "_rels/.rels",
                f'<Relationship Id="rIdEmf" Type="{_THUMBNAIL_REL}" Target="docProps/thumbnail.emf"/>'.encode(),
            )

        opaque_orphan = _add_member(base, "opaque.bin", b"unowned-root-payload")
        cases = {
            "orphan-rels": orphan_rels,
            "wrong-rel-type": wrong_rel_type,
            "wrong-mode": wrong_mode,
            "wrong-target": wrong_target,
            "glossary-cross-role": glossary_cross_role,
            "emf-truncated": emf_package(_synthetic_emf()[:40]),
            "emf-signature": emf_package(_synthetic_emf(signature=0)),
            "emf-size": emf_package(_synthetic_emf(declared_bytes=92)),
            "emf-polyglot": emf_package(b"MZ" + _synthetic_emf()[2:]),
            "opaque-orphan": opaque_orphan,
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_safe_role_malicious_boundaries_remain_closed(self):
        base = _minimal_docx()
        cases = []
        external_custom = _add_relationship(
            base,
            "_rels/.rels",
            f'<Relationship Id="rIdCustom" Type="{_CUSTOM_PROPERTIES_REL}" Target="https://example.invalid/custom.xml" TargetMode="External" />'.encode(),
        )
        cases.append(("external-custom", external_custom))
        orphan_font = _add_member(base, "word/fonts/font1.odttf", bytes(range(32)))
        cases.append(("orphan-font", orphan_font))
        executable_font = _owned_obfuscated_font_docx(b"MZ" + b"x" * 40)
        cases.append(("executable-font", executable_font))
        orphan_styles = _add_content_type_override(
            base, "word/stylesWithEffects.xml", "application/vnd.ms-word.stylesWithEffects+xml"
        )
        orphan_styles = _add_member(
            orphan_styles,
            "word/stylesWithEffects.xml",
            b'<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        )
        cases.append(("orphan-styles", orphan_styles))
        unknown_glossary = _add_member(
            base,
            "word/glossary/unknown.xml",
            b'<w:unknown xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"/>',
        )
        cases.append(("unknown-glossary", unknown_glossary))
        external_comments = _add_relationship(
            base,
            "word/_rels/document.xml.rels",
            f'<Relationship Id="rIdComments" Type="{_COMMENTS_REL}" Target="https://example.invalid/comments.xml" TargetMode="External" />'.encode(),
        )
        cases.append(("external-comments", external_comments))
        orphan_diagram = _add_content_type_override(
            base,
            "word/diagrams/data1.xml",
            "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
        )
        orphan_diagram = _add_member(
            orphan_diagram,
            "word/diagrams/data1.xml",
            b'<dgm:dataModel xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"/>',
        )
        cases.append(("orphan-diagram", orphan_diagram))
        mismatched_diagram = base
        for index, (role, content_type, root_name) in enumerate(
            (
                ("data", "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml", "dataModel"),
                ("layout", "application/vnd.openxmlformats-officedocument.drawingml.diagramLayout+xml", "layoutDef"),
                ("quickStyle", "application/vnd.openxmlformats-officedocument.drawingml.diagramStyle+xml", "styleDef"),
                ("colors", "application/vnd.openxmlformats-officedocument.drawingml.diagramColors+xml", "colorsDef"),
            ),
            1,
        ):
            part_index = 2 if role == "layout" else 1
            part_name = f"word/diagrams/{role}{part_index}.xml"
            mismatched_diagram = _add_content_type_override(
                mismatched_diagram, part_name, content_type
            )
            mismatched_diagram = _add_member(
                mismatched_diagram,
                part_name,
                f'<dgm:{root_name} xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram"/>'.encode(),
            )
            mismatched_diagram = _add_relationship(
                mismatched_diagram,
                "word/_rels/document.xml.rels",
                f'<Relationship Id="rIdMismatch{index}" Type="{_DIAGRAM_RELATIONSHIPS[role]}" Target="diagrams/{role}{part_index}.xml" />'.encode(),
            )
        cases.append(("mismatched-diagram-suite", mismatched_diagram))
        bad_thumbnail = _add_content_type_override(base, "docProps/thumbnail.jpeg", "image/jpeg")
        bad_thumbnail = _add_member(bad_thumbnail, "docProps/thumbnail.jpeg", b"not-a-jpeg")
        bad_thumbnail = _add_relationship(
            bad_thumbnail,
            "_rels/.rels",
            f'<Relationship Id="rIdThumb" Type="{_THUMBNAIL_REL}" Target="docProps/thumbnail.jpeg" />'.encode(),
        )
        cases.append(("bad-thumbnail", bad_thumbnail))
        wrong_diagram_root = _add_content_type_override(
            base,
            "word/diagrams/data1.xml",
            "application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml",
        )
        wrong_diagram_root = _add_member(wrong_diagram_root, "word/diagrams/data1.xml", b"<wrong/>")
        wrong_diagram_root = _add_relationship(
            wrong_diagram_root,
            "word/_rels/document.xml.rels",
            f'<Relationship Id="rIdDiagram" Type="{_DIAGRAM_RELATIONSHIPS["data"]}" Target="diagrams/data1.xml" />'.encode(),
        )
        cases.append(("wrong-diagram-root", wrong_diagram_root))
        for label, body in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid") as caught:
                    validate_docx_package(body)
                self.assertEqual("invalid_package", caught.exception.rejection_code)

    def test_macros_embeddings_activex_altchunk_and_unknown_active_parts_reject(self):
        base = _minimal_docx()
        rels = _member_text(base, "word/_rels/document.xml.rels")
        macros = _add_member(
            _replace_member(
                base,
                "[Content_Types].xml",
                new_data=_member_text(base, "[Content_Types].xml").replace(
                    b"</Types>",
                    b'<Override PartName="/word/vbaProject.bin" ContentType="application/vnd.ms-office.vbaProject"/></Types>',
                ),
            ),
            "word/vbaProject.bin",
            b"macro",
        )
        embeddings = _add_member(base, "word/embeddings/oleObject1.bin", b"embedded")
        activex = _add_member(
            _replace_member(
                base,
                "word/_rels/document.xml.rels",
                new_data=rels.replace(
                    b"</Relationships>",
                    (
                        b'<Relationship Id="rIdAx1" Type="'
                        + _ACTIVEX_REL.encode("utf-8")
                        + b'" Target="activeX/activeX1.bin"/></Relationships>'
                    ),
                ),
            ),
            "word/activeX/activeX1.bin",
            b"activex",
        )
        altchunk = _add_member(
            _replace_member(
                base,
                "word/_rels/document.xml.rels",
                new_data=rels.replace(
                    b"</Relationships>",
                    (
                        b'<Relationship Id="rIdChunk1" Type="'
                        + _ALTCHUNK_REL.encode("utf-8")
                        + b'" Target="afchunk1.html"/></Relationships>'
                    ),
                ),
            ),
            "word/afchunk1.html",
            b"<html></html>",
        )
        altchunk_element = _replace_member(
            base,
            "word/document.xml",
            new_data=(
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                b"<w:body><w:altChunk/></w:body></w:document>"
            ),
        )
        unknown_active = _add_member(
            _replace_member(
                _replace_member(
                    base,
                    "[Content_Types].xml",
                    new_data=_member_text(base, "[Content_Types].xml").replace(
                        b"</Types>",
                        b'<Override PartName="/word/evil.bin" ContentType="application/x-msdownload"/></Types>',
                    ),
                ),
                "word/_rels/document.xml.rels",
                new_data=rels.replace(
                    b"</Relationships>",
                    b'<Relationship Id="rIdEvil" Type="http://example.com/active" Target="evil.bin"/></Relationships>',
                ),
            ),
            "word/evil.bin",
            b"MZ",
        )
        for label, body in (
            ("macros", macros),
            ("embeddings", embeddings),
            ("activex", activex),
            ("altchunk", altchunk),
            ("altchunk-element", altchunk_element),
            ("unknown-active", unknown_active),
        ):
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)

    def test_dtd_entity_processing_instruction_and_malformed_xml_reject(self):
        base = _minimal_docx()
        xml = _member_text(base, "word/document.xml")
        cases = (
            (
                "doctype",
                _replace_member(
                    base,
                    "word/document.xml",
                    new_data=b'<?xml version="1.0" encoding="utf-8"?><!DOCTYPE w:document>' + xml.split(b"?>", 1)[1],
                ),
            ),
            (
                "entity",
                _replace_member(
                    base,
                    "[Content_Types].xml",
                    new_data=(
                        b'<?xml version="1.0" encoding="utf-8"?><!ENTITY boom "x">'
                        + _member_text(base, "[Content_Types].xml").split(b"?>", 1)[1]
                    ),
                ),
            ),
            (
                "utf16-doctype",
                _replace_member(
                    base,
                    "word/document.xml",
                    new_data='<?xml version="1.0" encoding="utf-16"?><!DOCTYPE w:document><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>'.encode(
                        "utf-16"
                    ),
                ),
            ),
            (
                "processing-instruction",
                _replace_member(
                    base,
                    "word/document.xml",
                    new_data=xml.replace(
                        b"?>",
                        b'?><?xml-stylesheet href="https://example.com/x.xsl"?>',
                        1,
                    ),
                ),
            ),
            (
                "malformed",
                _replace_member(
                    base,
                    "word/document.xml",
                    new_data=b"<w:document",
                ),
            ),
            (
                "wrong-root",
                _replace_member(
                    base,
                    "word/document.xml",
                    new_data=b'<?xml version="1.0" encoding="utf-8"?><root/>',
                ),
            ),
        )
        for label, body in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)


class NativeDocxOoxmlAdapterTests(unittest.TestCase):
    def fixture_bytes(self, name: str) -> bytes:
        return (FIXTURE_ROOT / name).read_bytes()

    def adapter(
        self,
        *,
        manifest: dict[str, object] | None = None,
        expected_corpus_digest: str = "sha256:" + "a" * 64,
    ) -> NativeDocxOoxmlAdapter:
        return NativeDocxOoxmlAdapter(
            manifest or qualification_manifest(fixture_corpus_digest=expected_corpus_digest),
            expected_corpus_digest=expected_corpus_digest,
        )

    def test_minimal_fixture_maps_closed_contract_and_quality(self):
        body = self.fixture_bytes("minimal-paragraph.docx")
        output = self.adapter().parse(_source(body))
        self.assertEqual(
            output.document_ir["parser"],
            {
                "parser_id": "native-docx-ooxml",
                "parser_version": "1.0.0",
                "configuration_digest": docx_configuration_digest(DocxLimits()),
            },
        )
        self.assertEqual(
            output.document_ir["source"],
            {
                "resource": "source-0123456789abcdef.docx",
                "digest": "sha256:" + sha256(body).hexdigest(),
                "media_type": DOCX_MIME,
            },
        )
        self.assertEqual(
            [block["id"] for block in output.document_ir["blocks"]],
            ["b1", "b2"],
        )
        self.assertEqual(
            [block["type"] for block in output.document_ir["blocks"]],
            ["heading", "paragraph"],
        )
        self.assertEqual(output.document_ir["blocks"][0]["text"], "Public DOCX Evidence")
        self.assertEqual(output.document_ir["blocks"][0]["level"], 2)
        self.assertEqual(output.document_ir["blocks"][1]["text"], "Deterministic local parsing only.")
        self.assertEqual(
            output.quality_components,
            {
                "text_coverage": 1.0,
                "structural_completeness": 1.0,
                "source_span_coverage": 1.0,
                "document_ir_valid": 1.0,
            },
        )
        for block in output.document_ir["blocks"]:
            self.assertNotIn("page", block["source_span"])
            self.assertNotIn("coordinates", block["source_span"])
            self.assertEqual(set(block["source_span"]), {"start", "end", "part", "paragraph"})

    def test_lists_links_notes_media_and_nested_tables_are_mapped_in_order(self):
        hierarchy = self.adapter().parse(_source(self.fixture_bytes("hierarchy-and-list.docx")))
        self.assertEqual(
            [block["type"] for block in hierarchy.document_ir["blocks"]],
            ["heading", "heading", "list", "list"],
        )
        self.assertEqual(
            [block.get("level") for block in hierarchy.document_ir["blocks"][:2]],
            [2, 3],
        )
        self.assertEqual(
            [block["attributes"] for block in hierarchy.document_ir["blocks"][2:]],
            [
                {"list_level": 0, "list_id": "1", "list_format": "bullet"},
                {"list_level": 1, "list_id": "1", "list_format": "bullet"},
            ],
        )

        links = self.adapter().parse(_source(self.fixture_bytes("links-and-notes.docx")))
        self.assertEqual(
            [block["type"] for block in links.document_ir["blocks"]],
            ["heading", "paragraph", "link", "paragraph", "footnote", "paragraph", "footnote"],
        )
        self.assertEqual(
            links.document_ir["blocks"][2]["attributes"],
            {"relationship": "external-hyperlink"},
        )
        self.assertEqual(
            links.document_ir["blocks"][4]["attributes"],
            {"note_kind": "footnote", "note_id": "1", "reference_paragraph": 2},
        )
        self.assertEqual(
            links.document_ir["blocks"][6]["attributes"],
            {"note_kind": "endnote", "note_id": "1", "reference_paragraph": 3},
        )

        media = self.adapter().parse(_source(self.fixture_bytes("media-and-drawing.docx")))
        image_block = media.document_ir["blocks"][2]
        self.assertEqual(image_block["type"], "image")
        self.assertEqual(
            image_block["attributes"],
            {
                "relationship": "embedded-image",
                "content_type": "application/octet-stream",
                "digest": "sha256:ad91235e882292469812e16da0b8fc77075a7c6d6f8760c24be14a5c792508cf",
                "width": 190500,
                "height": 190500,
            },
        )
        self.assertEqual(image_block["text"], "")
        self.assertNotIn("media/pixel.bin", json.dumps(image_block))

        nested = self.adapter().parse(_source(self.fixture_bytes("nested-table.docx")))
        table = nested.document_ir["blocks"][1]
        self.assertEqual(table["type"], "table")
        self.assertEqual(table["attributes"]["column_count"], 2)
        self.assertEqual(table["attributes"]["rows"][0]["cells"][1]["nested_tables"][0]["rows"][1]["cells"][1]["text"], "Inner 4")
        self.assertEqual(
            table["source_span"],
            {
                "start": table["source_span"]["start"],
                "end": table["source_span"]["end"],
                "part": "word/document.xml",
                "table": 0,
                "row_start": 0,
                "row_end": 1,
                "cell_start": 0,
                "cell_end": 1,
            },
        )

    def test_anchor_only_history_link_emits_text_without_locator(self):
        anchor = "ReviewedBookmark"
        body = _hyperlink_variant(
            f'w:anchor="{anchor}" w:history="1"'.encode("ascii")
        )

        output = self.adapter().parse(_source(body))

        link = next(
            block for block in output.document_ir["blocks"] if block["type"] == "link"
        )
        self.assertEqual("Hyperlink", link["text"])
        self.assertEqual({"relationship": "internal-anchor"}, link["attributes"])
        serialized = json.dumps(output.document_ir, sort_keys=True)
        self.assertNotIn(anchor, serialized)
        self.assertNotIn("rIdLink1", serialized)
        self.assertNotIn("https://example.com/private", serialized)

    def test_hyperlink_rejects_unknown_attrs_history_values_and_bad_locator_shapes(self):
        cases = {
            "unknown": _hyperlink_variant(
                b'w:anchor="Bookmark" w:docLocation="private"'
            ),
            "history-zero": _hyperlink_variant(
                b'w:anchor="Bookmark" w:history="0"'
            ),
            "history-true": _hyperlink_variant(
                b'w:anchor="Bookmark" w:history="true"'
            ),
            "empty-anchor": _hyperlink_variant(b'w:anchor=""'),
            "no-locator": _hyperlink_variant(b'w:history="1"'),
            "direct-text": _hyperlink_variant(
                b'w:anchor="Bookmark"', b"<w:t>private</w:t>"
            ),
            "empty-text": _hyperlink_variant(
                b'w:anchor="Bookmark"', b"<w:r><w:t>   </w:t></w:r>"
            ),
            "run-without-text": _hyperlink_variant(
                b'w:anchor="Bookmark"', b"<w:r><w:rPr /></w:r>"
            ),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ParsingError, "DOCX package is invalid"):
                    self.adapter().parse(_source(body))

    def test_mixed_hyperlink_validates_both_locators_and_hides_them(self):
        anchor = "MixedBookmark"
        body = _hyperlink_variant(
            f'r:id="rIdLink1" w:anchor="{anchor}" w:history="1"'.encode(
                "ascii"
            )
        )

        output = self.adapter().parse(_source(body))

        link = next(
            block for block in output.document_ir["blocks"] if block["type"] == "link"
        )
        self.assertEqual({"relationship": "external-hyperlink"}, link["attributes"])
        serialized = json.dumps(output.document_ir, sort_keys=True)
        for locator in (anchor, "rIdLink1", "https://example.com/private"):
            self.assertNotIn(locator, serialized)

    def test_hyperlink_metadata_rsids_and_closed_presentation_leaves_are_discarded(self):
        metadata = (
            b'w:anchor="Bookmark" w:history="1" '
            b'w:tooltip="Reviewed tooltip" w:tgtFrame="_blank"'
        )
        for index, properties in enumerate(_VALID_HYPERLINK_RPR_LEAVES):
            with self.subTest(index=index):
                body = _hyperlink_variant(
                    metadata,
                    _hyperlink_run(
                        properties=properties,
                        attributes=b'w:rsidR="ABCDEF12" w:rsidRPr="1234ABCD"',
                    ),
                )
                output = self.adapter().parse(_source(body))
                link = next(
                    block
                    for block in output.document_ir["blocks"]
                    if block["type"] == "link"
                )
                self.assertEqual("Visible Link", link["text"])
                self.assertEqual({"relationship": "internal-anchor"}, link["attributes"])
                serialized = json.dumps(output.document_ir, sort_keys=True)
                for discarded in (
                    "Reviewed tooltip",
                    "_blank",
                    "ABCDEF12",
                    "1234ABCD",
                    "Bookmark",
                ):
                    self.assertNotIn(discarded, serialized)

    def test_hyperlink_presentation_grammar_rejects_every_malicious_boundary(self):
        for label, body in _invalid_hyperlink_presentation_cases().items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ParsingError, "DOCX package is invalid"):
                    self.adapter().parse(_source(body))

    def test_formatting_only_runs_are_inert_in_visible_and_pageref_positions(self):
        formatting = _formatting_only_run()
        visible = _hyperlink_run(properties=b"", tokens=b"<w:t>Visible</w:t>")
        ordinary = {
            "before": formatting + visible,
            "between": visible + formatting + visible,
            "after": visible + formatting,
        }
        for label, children in ordinary.items():
            with self.subTest(kind="ordinary", position=label):
                body = _hyperlink_variant(b'w:anchor="Bookmark"', children)
                output = self.adapter().parse(_source(body))
                link = next(
                    block
                    for block in output.document_ir["blocks"]
                    if block["type"] == "link"
                )
                self.assertEqual(
                    "VisibleVisible" if label == "between" else "Visible",
                    link["text"],
                )
                self.assertNotIn("ABCDEF12", json.dumps(output.document_ir))

        field = _pageref_runs("_Toc12345678")
        field_positions = {
            "before-begin": formatting + field,
            "after-begin": field.replace(b"</w:r>", b"</w:r>" + formatting, 1),
            "after-instruction": field.replace(
                b"</w:instrText></w:r>",
                b"</w:instrText></w:r>" + formatting,
                1,
            ),
            "after-separate": field.replace(
                b'<w:fldChar w:fldCharType="separate" /></w:r>',
                b'<w:fldChar w:fldCharType="separate" /></w:r>' + formatting,
                1,
            ),
            "before-end": field.replace(
                b'<w:r><w:fldChar w:fldCharType="end" /></w:r>',
                formatting + b'<w:r><w:fldChar w:fldCharType="end" /></w:r>',
                1,
            ),
            "after-end": field + formatting,
        }
        for label, children in field_positions.items():
            with self.subTest(kind="pageref", position=label):
                body = _hyperlink_variant(b'w:anchor="Bookmark"', children)
                output = self.adapter().parse(_source(body))
                link = next(
                    block
                    for block in output.document_ir["blocks"]
                    if block["type"] == "link"
                )
                self.assertEqual("12", link["text"])

    def test_formatting_only_runs_preserve_empty_and_visibility_rejections(self):
        cases = {
            "no-rpr": b"<w:r />",
            "empty-rpr": b"<w:r><w:rPr /></w:r>",
            "invalid-rpr": b"<w:r><w:rPr><w:unknown /></w:rPr></w:r>",
            "all-formatting": _formatting_only_run(),
            "field-result-formatting": _pageref_runs(
                "_Toc12345678",
                result=b"<w:rPr><w:b /></w:rPr>",
            ),
        }
        for label, children in cases.items():
            with self.subTest(label=label):
                body = _hyperlink_variant(b'w:anchor="Bookmark"', children)
                with self.assertRaisesRegex(ParsingError, "DOCX package is invalid"):
                    self.adapter().parse(_source(body))

    def test_pageref_field_emits_only_visible_result_tokens_in_order(self):
        longest = "A" * 1012
        cases = {
            "short": _pageref_hyperlink(
                identifier="A",
                prefix=_hyperlink_run(properties=b"", tokens=b"<w:t>Before </w:t>"),
                result=(
                    b"<w:t>Page</w:t><w:tab /><w:lastRenderedPageBreak />"
                    b"<w:t>12</w:t>"
                ),
                suffix=_hyperlink_run(properties=b"", tokens=b"<w:t> After</w:t>"),
            ),
            "identifier-1012": _pageref_hyperlink(identifier=longest),
            "instruction-1024": _pageref_hyperlink(
                identifier=longest,
                instruction=" PAGEREF " + longest + " \\h",
            ),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                output = self.adapter().parse(_source(body))
                link = next(
                    block
                    for block in output.document_ir["blocks"]
                    if block["type"] == "link"
                )
                expected = "Before Page\t12 After" if label == "short" else "12"
                self.assertEqual(expected, link["text"])
                serialized = json.dumps(output.document_ir, sort_keys=True)
                for discarded in ("PAGEREF", "\\h", "ReviewedBookmark", longest):
                    self.assertNotIn(discarded, serialized)

    def test_pageref_field_rejects_unbalanced_split_oversized_and_unknown_forms(self):
        for label, body in _invalid_pageref_cases().items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ParsingError, "DOCX package is invalid"):
                    self.adapter().parse(_source(body))

    def test_external_cid_image_is_classification_only_and_never_echoed(self):
        cid = "cid:image001@example.invalid"
        body = self.fixture_bytes("media-and-drawing.docx")
        document = _member_text(body, "word/document.xml").replace(
            b'r:embed="rIdMedia1"', b'r:link="rIdMedia1"'
        )
        relationships = _member_text(body, "word/_rels/document.xml.rels").replace(
            b'Target="media/pixel.bin"',
            f'Target="{cid}" TargetMode="External"'.encode(),
        )
        body = _replace_member(body, "word/document.xml", new_data=document)
        body = _replace_member(
            body, "word/_rels/document.xml.rels", new_data=relationships
        )
        body = _drop_member(body, "word/media/pixel.bin")

        package = validate_docx_package(body)
        self.assertNotIn(cid, json.dumps(package.relationships, default=dict))
        output = self.adapter().parse(_source(body))
        image = next(
            block for block in output.document_ir["blocks"] if block["type"] == "image"
        )
        self.assertEqual(
            {"relationship": "external-linked-image"}, image["attributes"]
        )
        self.assertNotIn(cid, json.dumps(output.document_ir, sort_keys=True))

    def test_dual_blip_prefers_valid_embedded_payload_and_hides_external_link(self):
        cid = "cid:image001@example.invalid"
        body = self.fixture_bytes("media-and-drawing.docx")
        document = _member_text(body, "word/document.xml").replace(
            b'r:embed="rIdMedia1"',
            b'r:embed="rIdMedia1" r:link="rIdExternalImage"',
        )
        relationships = _member_text(body, "word/_rels/document.xml.rels").replace(
            b"</Relationships>",
            (
                b'<Relationship Id="rIdExternalImage" Type="'
                + _IMAGE_REL.encode("ascii")
                + f'" Target="{cid}" TargetMode="External" />'.encode("ascii")
                + b"</Relationships>"
            ),
        )
        body = _replace_member(body, "word/document.xml", new_data=document)
        body = _replace_member(
            body, "word/_rels/document.xml.rels", new_data=relationships
        )

        output = self.adapter().parse(_source(body))

        image = next(
            block for block in output.document_ir["blocks"] if block["type"] == "image"
        )
        self.assertEqual("embedded-image", image["attributes"]["relationship"])
        serialized = json.dumps(output.document_ir, sort_keys=True)
        self.assertNotIn(cid, serialized)
        self.assertNotIn("rIdExternalImage", serialized)

    def test_print_cstate_is_discarded_for_embedded_external_and_dual_blips(self):
        bodies = {
            "embedded": _blip_variant(
                "embedded", b'r:embed="rIdMedia1" cstate="print"'
            ),
            "external": _blip_variant(
                "external", b'r:link="rIdMedia1" cstate="print"'
            ),
            "dual": _blip_variant(
                "dual",
                b'r:embed="rIdMedia1" r:link="rIdExternalImage" cstate="print"',
            ),
        }
        for kind, body in bodies.items():
            with self.subTest(kind=kind):
                output = self.adapter().parse(_source(body))
                image = next(
                    block
                    for block in output.document_ir["blocks"]
                    if block["type"] == "image"
                )
                expected = (
                    "external-linked-image" if kind == "external" else "embedded-image"
                )
                self.assertEqual(expected, image["attributes"]["relationship"])
                serialized = json.dumps(output.document_ir, sort_keys=True)
                self.assertNotIn("cstate", serialized)
                self.assertNotIn("image001@example.invalid", serialized)

    def test_blip_cstate_rejects_nonprint_namespaced_duplicate_and_unknown_forms(self):
        cases = {
            "empty": b'r:embed="rIdMedia1" cstate=""',
            "case": b'r:embed="rIdMedia1" cstate="Print"',
            "other": b'r:embed="rIdMedia1" cstate="screen"',
            "drawing-namespaced": b'r:embed="rIdMedia1" a:cstate="print"',
            "relationship-namespaced": b'r:embed="rIdMedia1" r:cstate="print"',
            "unknown": b'r:embed="rIdMedia1" dpi="300"',
            "duplicate": b'r:embed="rIdMedia1" cstate="print" cstate="print"',
        }
        for label, attributes in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ParsingError, "DOCX package is invalid"):
                    self.adapter().parse(
                        _source(_blip_variant("embedded", attributes))
                    )

    def test_recognized_diagram_is_inventory_only_and_shape_text_is_inline(self):
        diagram = _drawing_variant(
            "http://schemas.openxmlformats.org/drawingml/2006/diagram", b""
        )
        diagram_output = self.adapter().parse(_source(diagram))
        self.assertEqual(
            ["heading", "paragraph"],
            [block["type"] for block in diagram_output.document_ir["blocks"]],
        )
        self.assertEqual("Drawing 1", diagram_output.document_ir["blocks"][1]["text"])

        shape = _drawing_variant(
            "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
            (
                b"<wps:wsp><wps:txbx><w:txbxContent><w:p><w:r>"
                b"<w:t>Visible Shape</w:t></w:r></w:p></w:txbxContent>"
                b"</wps:txbx></wps:wsp>"
            ),
        )
        document = _member_text(shape, "word/document.xml").replace(
            b'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"',
            (
                b'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
                b'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"'
            ),
        )
        shape = _replace_member(shape, "word/document.xml", new_data=document)
        shape_output = self.adapter().parse(_source(shape))
        self.assertEqual(
            "Drawing 1Visible Shape", shape_output.document_ir["blocks"][1]["text"]
        )
        self.assertNotIn("wps:", json.dumps(shape_output.document_ir))

    def test_drawing_semantics_reject_unknown_missing_multiple_and_wrong_relationships(self):
        picture_uri = "http://schemas.openxmlformats.org/drawingml/2006/picture"
        cases = {
            "unknown-role": _drawing_variant("urn:unknown:graphic", b""),
            "missing-blip": _drawing_variant(picture_uri, b"<pic:pic />"),
            "multiple-blips": _drawing_variant(
                picture_uri,
                (
                    b'<pic:pic><pic:blipFill><a:blip r:embed="rIdMedia1" />'
                    b'<a:blip r:embed="rIdMedia1" /></pic:blipFill></pic:pic>'
                ),
            ),
        }
        wrong_relationship = self.fixture_bytes("media-and-drawing.docx")
        rels = _member_text(wrong_relationship, "word/_rels/document.xml.rels").replace(
            _IMAGE_REL.encode("ascii"), _HYPERLINK_REL.encode("ascii")
        ).replace(b'Target="media/pixel.bin"', b'Target="https://example.invalid" TargetMode="External"')
        cases["wrong-relationship"] = _replace_member(
            wrong_relationship, "word/_rels/document.xml.rels", new_data=rels
        )
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(ParsingError, "DOCX package is invalid"):
                    self.adapter().parse(_source(body))

    def test_external_image_cid_contract_rejects_unsafe_or_unbound_forms(self):
        base = self.fixture_bytes("media-and-drawing.docx")
        base_document = _member_text(base, "word/document.xml")
        base_relationships = _member_text(base, "word/_rels/document.xml.rels")

        def external_image(target: str, reference: bytes) -> bytes:
            body = _replace_member(
                base,
                "word/document.xml",
                new_data=base_document.replace(
                    b'r:embed="rIdMedia1"', reference
                ),
            )
            body = _replace_member(
                body,
                "word/_rels/document.xml.rels",
                new_data=base_relationships.replace(
                    b'Target="media/pixel.bin"',
                    f'Target="{target}" TargetMode="External"'.encode(),
                ),
            )
            return _drop_member(body, "word/media/pixel.bin")

        cases = {
            "http": external_image(
                "https://example.invalid/image.png", b'r:link="rIdMedia1"'
            ),
            "file": external_image("file:///tmp/image.png", b'r:link="rIdMedia1"'),
            "data": external_image("data:image/png;base64,AA==", b'r:link="rIdMedia1"'),
            "malformed-cid": external_image("cid:no-at-sign", b'r:link="rIdMedia1"'),
            "malformed-cid-local": external_image(
                "cid:image..one@example.invalid", b'r:link="rIdMedia1"'
            ),
            "malformed-cid-domain": external_image(
                "cid:image001@example..invalid", b'r:link="rIdMedia1"'
            ),
            "overlong-cid": external_image(
                "cid:" + "x" * 600 + "@example.invalid", b'r:link="rIdMedia1"'
            ),
            "external-embed": external_image(
                "cid:image001@example.invalid", b'r:embed="rIdMedia1"'
            ),
            "missing-link-ref": external_image(
                "cid:image001@example.invalid", b'r:link="rIdMissing"'
            ),
            "embed-and-link": external_image(
                "cid:image001@example.invalid",
                b'r:embed="rIdMedia1" r:link="rIdMedia1"',
            ),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaisesRegex(DocxPackageError, "DOCX package is invalid"):
                    validate_docx_package(body)
                with self.assertRaisesRegex(ParsingError, "DOCX package is invalid"):
                    self.adapter().parse(_source(body))

        custom_heading = self.adapter().parse(_source(_custom_outline_level_docx()))
        self.assertEqual(custom_heading.document_ir["blocks"][0]["level"], 1)

    def test_table_spans_monotonic_spans_and_deterministic_output(self):
        adapter = self.adapter()
        body = _merged_table_docx()
        first = adapter.parse(_source(body))
        second = adapter.parse(_source(body))
        self.assertEqual(first.document_ir, second.document_ir)
        spans = [block["source_span"] for block in first.document_ir["blocks"]]
        self.assertEqual(
            [(item["start"], item["end"]) for item in spans],
            sorted((item["start"], item["end"]) for item in spans),
        )
        table = first.document_ir["blocks"][1]
        self.assertEqual(table["attributes"]["rows"][0]["cells"][0]["col_span"], 2)
        self.assertEqual(table["attributes"]["rows"][0]["cells"][0]["row_span"], 2)
        self.assertEqual(table["attributes"]["rows"][1]["cells"][0]["text"], "Bottom right")
        self.assertEqual(
            table["text"],
            "Merged cell\tTop right\nBottom right",
        )
        self.assertEqual(
            set(table["source_span"]),
            {"start", "end", "part", "table", "row_start", "row_end", "cell_start", "cell_end"},
        )
        self.assertEqual(
            {
                key: table["source_span"][key]
                for key in ("part", "table", "row_start", "row_end", "cell_start", "cell_end")
            },
            {
                "part": "word/document.xml",
                "table": 0,
                "row_start": 0,
                "row_end": 1,
                "cell_start": 0,
                "cell_end": 1,
            },
        )

    def test_source_contract_and_benchmark_binding_fail_closed(self):
        adapter = self.adapter()
        body = self.fixture_bytes("minimal-paragraph.docx")
        bad_sources = (
            {**_source(body), "resource": "../private.docx"},
            {**_source(body), "resource": "https://example.invalid/a.docx"},
            {**_source(body), "digest": "sha256:" + "0" * 64},
            {**_source(body), "media_type": "application/pdf"},
            {**_source(body), "data": b"x"},
            {**_source(body), "unknown": True},
        )
        for value in bad_sources:
            with self.subTest(value=set(value)):
                with self.assertRaisesRegex(ParsingError, "DOCX"):
                    adapter.parse(value)

        with self.assertRaisesRegex(ParsingError, "DOCX"):
            NativeDocxOoxmlAdapter(
                qualification_manifest(fixture_corpus_digest="sha256:" + "a" * 64)
            )

        with self.assertRaisesRegex(ParsingError, "DOCX"):
            self.adapter(
                manifest=qualification_manifest(fixture_corpus_digest="sha256:" + "b" * 64),
                expected_corpus_digest="sha256:" + "a" * 64,
            )

        for manifest in (
            qualification_manifest(decision="candidate_change"),
            qualification_manifest(decision="investigate"),
            qualification_manifest(configuration_digest="sha256:" + "0" * 64),
            qualification_manifest(result_digest="sha256:" + "0" * 64),
            qualification_manifest(metrics={"text_fidelity": 0.49, "structural_fidelity": 1.0, "source_location_fidelity": 1.0, "expected_outcome_accuracy": 1.0, "expected_rejection_count": 12, "accepted_document_count": 88}),
            qualification_manifest(metrics={"text_fidelity": 1.0, "structural_fidelity": 1.0, "source_location_fidelity": 1.0, "expected_outcome_accuracy": 1.0, "expected_rejection_count": 11, "accepted_document_count": 88}),
            qualification_manifest(metrics={"text_fidelity": 1.0, "structural_fidelity": 1.0, "source_location_fidelity": 1.0, "expected_outcome_accuracy": 1.0, "expected_rejection_count": 12, "accepted_document_count": 89}),
            qualification_manifest(stable_failures={"invalid_package": 1, "unsupported_active_content": 10}),
            qualification_manifest(stable_failures={"invalid_package": 1, "unsupported_active_content": 11, "other": 0}),
        ):
            with self.subTest(decision=manifest["decision"]):
                with self.assertRaisesRegex(ParsingError, "DOCX"):
                    self.adapter(manifest=manifest)

    def test_active_content_rejection_carries_closed_private_code(self):
        base = _hyperlink_docx()
        relationships = _member_text(base, "word/_rels/document.xml.rels")
        active = _replace_member(
            base,
            "word/_rels/document.xml.rels",
            new_data=relationships.replace(
                b"</Relationships>",
                (
                    b'<Relationship Id="rIdTemplate" Type="'
                    + _ATTACHED_TEMPLATE_REL.encode("utf-8")
                    + b'" Target="https://example.invalid/template.dotm" '
                    b'TargetMode="External"/></Relationships>'
                ),
            ),
        )

        with self.assertRaises(ParsingError) as caught:
            self.adapter().parse(_source(active))

        self.assertEqual("DOCX package is invalid", str(caught.exception))
        self.assertEqual(
            "unsupported_active_content",
            getattr(caught.exception, "rejection_code", None),
        )

    def test_oversized_relationship_target_never_reaches_document_ir(self):
        base = _hyperlink_docx()
        relationships = _member_text(base, "word/_rels/document.xml.rels")
        oversized = _replace_member(
            base,
            "word/_rels/document.xml.rels",
            new_data=relationships.replace(
                b"https://example.com/private",
                b"https://example.invalid/"
                + shake_256(b"oversized-relationship-target").hexdigest(
                    512 * 1024
                ).encode("ascii"),
            ),
        )

        with self.assertRaisesRegex(ParsingError, "DOCX package is invalid"):
            self.adapter().parse(_source(oversized))

        safe = self.adapter().parse(_source(base))
        link = next(block for block in safe.document_ir["blocks"] if block["type"] == "link")
        self.assertEqual({"relationship": "external-hyperlink"}, link["attributes"])
        self.assertNotIn("https://example.com/private", json.dumps(safe.document_ir))


if __name__ == "__main__":
    unittest.main()
