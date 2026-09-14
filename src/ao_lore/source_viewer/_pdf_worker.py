"""Isolated PDF raster worker. Private stdin/stdout protocol, not a public CLI.

Resource-limited subprocess, not a complete native-code exploit sandbox.
No PDF scripting/form environment is initialized; output is raster PNG plus geometry.
"""
from __future__ import annotations

import base64
import ctypes
import io
import json
import math
import sys


def limits() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (12, 12))
    resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024, 1024 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform.startswith("linux"):
        # Prevent gaining privileges through subsequent execution. This is not seccomp.
        if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:
            raise RuntimeError("sandbox_setup_failed")


def output(value: dict) -> None:
    raw = json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > 16 * 1024 * 1024:
        raw = b'{"error":"resource_limit"}'
    sys.stdout.buffer.write(raw)


def highlight(page, bitmap, excerpt: str) -> dict:
    empty = {"status": "exact_match_unavailable", "basis": "renderer-exact-text", "boxes": []}
    if not excerpt:
        return dict(empty, status="no_cited_excerpt_on_this_page")
    if len(excerpt) > 16384:
        return dict(empty, status="highlight_excerpt_limit")
    text = page.get_textpage()
    searcher = None
    try:
        if text.count_chars() > 1_000_000:
            return dict(empty, status="highlight_text_limit")
        searcher = text.search(excerpt, match_case=True, consecutive=True)
        exact = []
        exhausted = False
        for _ in range(64):
            match = searcher.get_next()
            if match is None:
                exhausted = True
                break
            index, count = match
            # PDFium search can normalize whitespace. Verify exact retrieved characters;
            # substring indexes from a different extracted representation are never used.
            if text.get_text_range(index, count, errors="strict") == excerpt:
                exact.append(match)
                if len(exact) > 1:
                    return dict(empty, status="ambiguous_exact_match")
        if not exhausted:
            return dict(empty, status="highlight_search_limit")
        if len(exact) != 1:
            return empty
        count = text.count_rects(*exact[0])
        if count > 256:
            return dict(empty, status="highlight_geometry_limit")
        transform = bitmap.get_posconv(page)
        boxes = []
        for i in range(count):
            left, bottom, right, top = text.get_rect(i)
            corners = [transform.to_bitmap(x, y) for x, y in
                       ((left, bottom), (left, top), (right, bottom), (right, top))]
            xs, ys = [c[0] / bitmap.width for c in corners], [c[1] / bitmap.height for c in corners]
            x1, x2, y1, y2 = min(xs), max(xs), min(ys), max(ys)
            if not all(math.isfinite(n) and -0.002 <= n <= 1.002 for n in (x1, x2, y1, y2)):
                return dict(empty, status="highlight_outside_page")
            x1, x2, y1, y2 = [min(1.0, max(0.0, n)) for n in (x1, x2, y1, y2)]
            if x2 > x1 and y2 > y1:
                boxes.append([x1, y1, x2 - x1, y2 - y1])
        return {"status": "exact_unique_text" if boxes else "highlight_geometry_unavailable",
                "basis": "renderer-exact-text", "boxes": boxes}
    except Exception:
        return dict(empty, status="highlight_geometry_unavailable")
    finally:
        if searcher is not None:
            searcher.close()
        text.close()


def main() -> None:
    limits()
    try:
        import pypdfium2 as pdfium
        from PIL import Image  # noqa: F401 — validates the optional PNG encoder dependency
    except ImportError:
        sys.exit(10)
    raw = sys.stdin.buffer.read(47 * 1024 * 1024 + 1)
    if len(raw) > 47 * 1024 * 1024:
        output({"error": "resource_limit"})
        return
    request = json.loads(raw)
    data = base64.b64decode(request["source"], validate=True)
    if len(data) > 32 * 1024 * 1024 or not data.startswith(b"%PDF-"):
        output({"error": "invalid_document"})
        return
    number = request["page"]
    with pdfium.PdfDocument(data) as document:
        if not 1 <= len(document) <= 2000:
            output({"error": "resource_limit"})
            return
        if type(number) is not int or not 1 <= number <= len(document):
            output({"error": "invalid_page"})
            return
        page = document[number - 1]
        bitmap = None
        try:
            width, height = page.get_size()
            if not all(math.isfinite(n) and 1 <= n <= 20000 for n in (width, height)):
                output({"error": "resource_limit"})
                return
            scale = min(1000 / width, 1600 / height, 2.0)
            if math.ceil(width * scale) * math.ceil(height * scale) > 10_000_000:
                output({"error": "resource_limit"})
                return
            bitmap = page.render(scale=scale, may_draw_forms=False)
            geometry = highlight(page, bitmap, request["excerpt"])
            png = io.BytesIO()
            image = bitmap.to_pil()
            try:
                image.save(png, format="PNG")
            finally:
                image.close()
            output({"page": number, "page_count": len(document), "width": bitmap.width,
                    "height": bitmap.height, "highlight": geometry,
                    "png": base64.b64encode(png.getvalue()).decode("ascii")})
        finally:
            if bitmap is not None:
                bitmap.close()
            page.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        output({"error": "pdf_render_failed"})
