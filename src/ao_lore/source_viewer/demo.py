"""Deterministic PUBLIC SYNTHETIC fixtures; no third-party corpus or private evidence."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .contracts import canonical_json, digest
from .store import provision_snapshot

PDF_EXCERPT = "The inspection interval is 30 days."
TEXT_EXCERPT = "A source must be revalidated after a digest changes."
DOCX_EXCERPT = "A workspace may read only explicitly declared direct references."


def synthetic_pdf(pages: list[list[str]], *, rotation: int = 0,
                  crop: tuple[int, int, int, int] | None = None) -> bytes:
    """Minimal deterministic PDF writer for tests only, not a production document writer."""
    page_ids = [4 + 2 * i for i in range(len(pages))]
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               ("<< /Type /Pages /Count " + str(len(pages)) + " /Kids [" +
                " ".join(str(i) + " 0 R" for i in page_ids) + "] >>").encode("ascii"),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    for page_index, lines in enumerate(pages):
        commands = ["0.12 0.19 0.27 rg"]
        for index, line in enumerate(lines):
            literal = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            size = 21 if index == 1 else 11
            commands.append(f"BT /F1 {size} Tf 64 {728 - index * 39} Td ({literal}) Tj ET")
        commands.append(f"BT /F1 9 Tf 64 48 Td (PUBLIC SYNTHETIC FIXTURE / physical page {page_index+1}) Tj ET")
        stream = "\n".join(commands).encode("ascii")
        extra = f" /Rotate {rotation}"
        if crop:
            extra += " /CropBox [" + " ".join(str(n) for n in crop) + "]"
        objects.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]" + extra +
                        " /Resources << /Font << /F1 3 0 R >> >> /Contents " +
                        str(page_ids[page_index] + 1) + " 0 R >>").encode("ascii"))
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream")
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    start = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode("ascii"))
    return bytes(result)


def synthetic_docx(paragraphs: list[str]) -> bytes:
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body>' + "".join('<w:p><w:r><w:t xml:space="preserve">' + escape(text) +
                                   '</w:t></w:r></w:p>' for text in paragraphs) + '</w:body></w:document>')
    types = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/word/document.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
             '</Types>')
    relationships = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                     'Target="word/document.xml"/></Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in (("[Content_Types].xml", types), ("word/document.xml", document),
                            ("_rels/.rels", relationships)):
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, value.encode("utf-8"))
    return buffer.getvalue()


def synthetic_record(data: bytes, kind: str, excerpt: str, *, workspace: str = "viewer-demo",
                     source_id: str = "synthetic-source", page: int | None = None,
                     start: int = 0, sensitivity: str = "public") -> dict[str, Any]:
    span: dict[str, Any] = {"start": start, "end": start + len(excerpt)}
    if page is not None:
        span["page"] = page
    evidence = {
        "workspace_id": workspace, "generation_digest": digest(b"synthetic-generation:" + kind.encode()),
        "document_id": "synthetic-document-" + kind,
        "document_ir_digest": digest(b"synthetic-ir:" + data),
        "source_id": source_id, "source_digest": digest(data), "block_id": "synthetic-block-1",
        "block_digest": digest(canonical_json({"fixture_block": excerpt})),
        "render_text": excerpt, "source_span": span, "authority_role": "operator_procedure",
        "sensitivity": sensitivity, "freshness_status": "current",
        "qualification_codes": ["synthetic_fixture", "not_engineering_guidance"],
    }
    # This is a synthetic fixture identity, NOT an imitation of upstream's digest algorithm.
    evidence["evidence_id"] = digest(canonical_json(evidence))
    return {"evidence": evidence, "format": kind, "original_sensitivity": sensitivity,
            "pdf_page_basis": "physical-one-based" if kind == "pdf" else "not-applicable"}


def demo_material() -> tuple[dict[str, Any], dict[str, bytes]]:
    pdf = synthetic_pdf([
        ["AO LORE / SOURCE VIEWER", "Public demonstration", "This is an invented source document.",
         "It contains no customer information and is not engineering guidance.",
         "The cited passage appears on physical page 2."],
        ["AO LORE / PUBLIC SYNTHETIC SOURCE", "Inspection procedure", "Printed label: A-1 / physical PDF page 2",
         "This fictional procedure is used only to exercise source verification.",
         PDF_EXCERPT, "Record the date and the observation in the local inspection log.",
         "This fixture must not be used as a real inspection requirement."],
        ["AO LORE / SOURCE VIEWER", "Evidence boundaries", "Source viewing does not change canonical knowledge.",
         "Exact excerpts remain separate from permissions and source authority.",
         "End of synthetic demonstration."],
    ])
    content = ("PUBLIC SYNTHETIC TEXT / AO LORE SOURCE VIEWER\n\n" + TEXT_EXCERPT +
               "\n\nThis fixture is local-only and contains no customer information.\n").encode()
    docx = synthetic_docx(["PUBLIC SYNTHETIC DOCX / REFERENCE RULE", DOCX_EXCERPT,
                           "This is an XML text projection test, not a Word layout test."])
    records = [synthetic_record(pdf, "pdf", PDF_EXCERPT, source_id="inspection-procedure", page=2),
               synthetic_record(content, "text", TEXT_EXCERPT, source_id="source-integrity-note",
                                start=content.decode().index(TEXT_EXCERPT)),
               synthetic_record(docx, "docx", DOCX_EXCERPT, workspace="demo-reference",
                                source_id="reference-access-rule")]
    manifest = {"schema_version": "ao.lore.source-viewer-bindings.v0.1",
                "primary_workspace_id": "viewer-demo", "reference_workspace_ids": ["demo-reference"],
                "provenance_mode": "operator-approved-snapshot", "records": records}
    return manifest, {digest(value): value for value in (pdf, content, docx)}


def create_demo(home: Path) -> str:
    manifest, originals = demo_material()
    provision_snapshot(home, manifest, originals, grant_id="synthetic-viewer-demo")
    return "synthetic-viewer-demo"
