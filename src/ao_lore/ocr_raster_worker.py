"""Internal offline raster worker. The parent supplies a sealed launch contract."""

from __future__ import annotations

import json
import re
import sys
import warnings
from pathlib import Path


_MAX_PAGES = 100
_MAX_PIXELS_PAGE = 25_000_000


class _EncryptedPdf(Exception):
    pass


def _strict_json(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    if len(raw) > 4096:
        raise RuntimeError("contract")

    def pairs(items):
        value = dict(items)
        if len(value) != len(items):
            raise ValueError("duplicate")
        return value

    value = json.loads(raw, object_pairs_hook=pairs)
    if type(value) is not dict or set(value) != {"schema", "media_type", "dpi"}:
        raise RuntimeError("contract")
    if value["schema"] != "ao.lore.ocr-raster-worker.v0.1" or value["media_type"] not in (
        "application/pdf", "image/png", "image/jpeg"
    ) or type(value["dpi"]) is not int or value["dpi"] != 300:
        raise RuntimeError("contract")
    return value


def _save(image, path: Path) -> tuple[int, int, int]:
    image = image.convert("RGB")
    width, height = image.size
    if type(width) is not int or type(height) is not int or width < 1 or height < 1 or width * height > _MAX_PIXELS_PAGE:
        raise RuntimeError("dimensions")
    image.save(path, format="PNG", compress_level=9, optimize=False)
    size = path.stat().st_size
    return width, height, size


def _render_image(source: Path, output: Path):
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = _MAX_PIXELS_PAGE
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with Image.open(source) as image:
            image.load()
            return [_save(image, output / "page-0001.png")]


def _render_pdf(source: Path, output: Path, dpi: int):
    import pypdfium2

    raw = source.read_bytes()
    trailer = raw.rsplit(b"trailer", 1)[-1]
    if re.search(rb"/Encrypt\s+[0-9]+\s+[0-9]+\s+R(?:\s|>>)", trailer):
        raise _EncryptedPdf()
    document = pypdfium2.PdfDocument(source)
    try:
        count = len(document)
        if not 1 <= count <= _MAX_PAGES:
            raise RuntimeError("page count")
        pages = []
        for index in range(count):
            page = document[index]
            try:
                bitmap = page.render(scale=dpi / 72, rotation=0, may_draw_forms=True)
                try:
                    image = bitmap.to_pil()
                    pages.append(_save(image, output / f"page-{index + 1:04d}.png"))
                finally:
                    bitmap.close()
            finally:
                page.close()
        return pages
    finally:
        document.close()


def _failure_category(exc: Exception) -> str:
    if isinstance(exc, _EncryptedPdf):
        return "encrypted_source"
    try:
        import pypdfium2
        import pypdfium2.raw as pdfium_raw
        from PIL import Image
    except Exception:
        return "invalid_source"
    if isinstance(exc, pypdfium2.PdfiumError):
        return "encrypted_source" if getattr(exc, "err_code", None) in (
            pdfium_raw.FPDF_ERR_PASSWORD, pdfium_raw.FPDF_ERR_SECURITY,
        ) else "invalid_source"
    if isinstance(exc, (Image.DecompressionBombError, Image.DecompressionBombWarning)):
        return "resource_limit"
    if isinstance(exc, RuntimeError) and str(exc) in {"dimensions", "page count"}:
        return "resource_limit"
    return "invalid_source"


def main() -> int:
    contract = _strict_json(Path("/input/contract.json"))
    source = Path("/input/source")
    output = Path("/output")
    try:
        if contract["media_type"] == "application/pdf":
            pages = _render_pdf(source, output, contract["dpi"])
        else:
            pages = _render_image(source, output)
    except Exception as exc:
        body = {
            "schema": "ao.lore.ocr-raster-worker-rejection.v0.1",
            "category": _failure_category(exc),
            "network_accessed": False,
            "provider_calls": False,
            "authority_advanced": False,
        }
        sys.stdout.write(json.dumps(body, sort_keys=True, separators=(",", ":")))
        sys.stdout.flush()
        return 0
    body = {
        "schema": "ao.lore.ocr-raster-worker-result.v0.1",
        "pages": [
            {"page_id": f"page-{index:04d}", "width": width, "height": height, "size": size}
            for index, (width, height, size) in enumerate(pages, 1)
        ],
        "network_accessed": False,
        "provider_calls": False,
        "authority_advanced": False,
    }
    sys.stdout.write(json.dumps(body, sort_keys=True, separators=(",", ":")))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
