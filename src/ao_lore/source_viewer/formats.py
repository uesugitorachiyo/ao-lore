"""Literal text projections and optional bounded PDF subprocess rendering."""
from __future__ import annotations

import base64
import binascii
import io
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .contracts import MAX_EXCERPT, MAX_SOURCE, MAX_TEXT, canonical_json, reject, require, strict_json

MAX_RENDER_OUTPUT = 16 * 1024 * 1024


def exact_segments(content: str, excerpt: str, span: dict[str, Any]) -> dict[str, Any]:
    """Never reinterpret IR character offsets as source offsets without checking text."""
    if not excerpt:
        return {"status": "empty_excerpt", "before": content, "match": "", "after": ""}
    start, end = span.get("start"), span.get("end")
    if (type(start) is int and type(end) is int and 0 <= start <= end <= len(content)
            and content[start:end] == excerpt):
        return {"status": "exact_verified_span", "before": content[:start],
                "match": content[start:end], "after": content[end:]}
    first = content.find(excerpt)
    if first < 0:
        return {"status": "exact_match_unavailable", "before": content, "match": "", "after": ""}
    if content.find(excerpt, first + 1) >= 0:
        return {"status": "ambiguous_exact_match", "before": content, "match": "", "after": ""}
    return {"status": "exact_unique_text", "before": content[:first], "match": excerpt,
            "after": content[first + len(excerpt):]}


def docx_projection(data: bytes) -> str:
    require(data.startswith(b"PK"), "invalid_document")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            require(len(infos) <= 512, "resource_limit")
            names = [entry.filename for entry in infos]
            require(len(set(names)) == len(names), "invalid_document")
            require("word/document.xml" in names and "[Content_Types].xml" in names,
                    "invalid_document")
            entry = archive.getinfo("word/document.xml")
            require(not entry.flag_bits & 1, "invalid_document")
            require(entry.file_size <= MAX_TEXT and entry.file_size <= max(1, entry.compress_size) * 100,
                    "resource_limit")
            with archive.open(entry) as stream:
                xml = stream.read(MAX_TEXT + 1)
            require(len(xml) <= MAX_TEXT, "resource_limit")
            xml_text = xml.decode("utf-8-sig", errors="strict")
            require("\x00" not in xml_text, "invalid_document")
            upper = xml_text.upper()
            require("<!DOCTYPE" not in upper and "<!ENTITY" not in upper, "invalid_document")
            root = ElementTree.fromstring(xml_text)
            namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            require(root.tag == namespace + "document", "invalid_document")
            paragraphs: list[str] = []
            for paragraph in root.iter(namespace + "p"):
                parts: list[str] = []
                for item in paragraph.iter():
                    if item.tag == namespace + "t":
                        parts.append(item.text or "")
                    elif item.tag == namespace + "tab":
                        parts.append("\t")
                    elif item.tag in {namespace + "br", namespace + "cr"}:
                        parts.append("\n")
                paragraphs.append("".join(parts))
            result = "\n".join(paragraphs)
            require(len(result.encode("utf-8")) <= MAX_TEXT, "resource_limit")
            return result
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, RuntimeError,
            NotImplementedError, UnicodeError, ValueError):
        reject("invalid_document")


def text_projection(data: bytes, record: dict[str, Any]) -> dict[str, Any]:
    require(len(data) <= MAX_SOURCE, "resource_limit")
    kind = record["format"]
    if kind == "text":
        require(len(data) <= MAX_TEXT, "resource_limit")
        try:
            content = data.decode("utf-8", errors="strict")
        except UnicodeError:
            reject("unsupported_text_encoding")
        require("\x00" not in content, "invalid_document")
        label = "Original UTF-8 text; literal display"
    elif kind == "docx":
        content = docx_projection(data)
        label = "DOCX main-body text projection; not Word pagination or layout"
    else:
        reject("unsupported_format")
    evidence = record["evidence"]
    # DOCX offsets are IR offsets, not XML offsets. Require a unique exact paragraph-text
    # occurrence instead of claiming that arbitrary IR offsets address this projection.
    span = evidence["source_span"] if kind == "text" else {}
    return {"format": kind, "display_label": label,
            "segments": exact_segments(content, evidence["render_text"], span)}


def render_pdf(data: bytes, page: int, excerpt: str = "") -> dict[str, Any]:
    require(type(page) is int and 1 <= page <= 2000, "invalid_page")
    require(isinstance(data, bytes) and len(data) <= MAX_SOURCE, "resource_limit")
    require(data.startswith(b"%PDF-"), "invalid_document")
    require(isinstance(excerpt, str) and len(excerpt) <= MAX_EXCERPT, "resource_limit")
    request = canonical_json({"source": base64.b64encode(data).decode("ascii"),
                              "page": page, "excerpt": excerpt})
    worker = Path(__file__).with_name("_pdf_worker.py")
    try:
        # -I drops cwd/PYTHONPATH/user-site injection; credentials and inherited provider
        # environment are not propagated. Output is an unnamed, private temporary file.
        with tempfile.TemporaryFile(mode="w+b") as output:
            completed = subprocess.run(
                [sys.executable, "-I", "-B", str(worker)], input=request, stdout=output,
                stderr=subprocess.DEVNULL, timeout=20, check=False,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
                close_fds=True,
            )
            output.seek(0)
            raw = output.read(MAX_RENDER_OUTPUT + 1)
        require(len(raw) <= MAX_RENDER_OUTPUT, "resource_limit")
        if completed.returncode == 10:
            reject("pdf_renderer_unavailable", 503)
        require(completed.returncode == 0, "pdf_render_failed")
        result = strict_json(raw, MAX_RENDER_OUTPUT)
        if result.get("error"):
            codes = {"invalid_page", "invalid_document", "resource_limit", "pdf_render_failed"}
            reject(result["error"] if result["error"] in codes else "pdf_render_failed")
        require(type(result.get("page_count")) is int and 1 <= result["page_count"] <= 2000,
                "pdf_render_failed")
        require(type(result.get("width")) is int and type(result.get("height")) is int,
                "pdf_render_failed")
        require(0 < result["width"] * result["height"] <= 10_000_000, "resource_limit")
        png = base64.b64decode(result.pop("png"), validate=True)
        require(png.startswith(b"\x89PNG\r\n\x1a\n"), "pdf_render_failed")
        result["png"] = png
        return result
    except subprocess.TimeoutExpired:
        reject("pdf_render_timeout", 503)
    except (OSError, KeyError, ValueError, TypeError, binascii.Error):
        reject("pdf_render_failed")
