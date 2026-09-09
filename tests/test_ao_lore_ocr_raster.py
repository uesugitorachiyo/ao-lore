import hashlib
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from ao_lore import ocr_raster
from ao_lore._strict_io import ContractError
from ao_lore.ocr_raster import (
    OCR_RASTER_CONFIGURATION_DIGEST,
    OcrRasterRejected,
    RasterPageOutput,
    ReviewedOcrSource,
    render_in_ocr_sandbox,
    rasterize_ocr_source,
    validate_raster_bundle,
)


def digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def png(width: int, height: int) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))

    def chunk(kind: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF)

    return signature + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


class OcrRasterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "state"
        self.source = self.root / "scan.png"
        self.source.write_bytes(png(2, 3))
        self.reviewed = ReviewedOcrSource(
            source_id="scan-01",
            path=self.source,
            media_type="image/png",
            source_digest=digest(self.source.read_bytes()),
            explicit_ocr=True,
        )
        self.calls = 0

    def tearDown(self):
        self.temp.cleanup()

    def render(self, body, media_type, configuration):
        self.calls += 1
        self.assertEqual(body, self.source.read_bytes())
        self.assertEqual(media_type, "image/png")
        self.assertEqual(configuration["dpi"], 300)
        return (RasterPageOutput(width=2, height=3, body=png(2, 3)),)

    def test_rasterize_publishes_exact_ordered_png_bundle_and_reopens(self):
        first = rasterize_ocr_source(
            self.reviewed,
            state_root=self.state,
            renderer_digest="sha256:" + "a" * 64,
            renderer=self.render,
        )
        self.assertEqual(first.configuration_digest, OCR_RASTER_CONFIGURATION_DIGEST)
        self.assertEqual([page.page_id for page in first.pages], ["page-0001"])
        self.assertEqual((first.pages[0].width, first.pages[0].height), (2, 3))
        self.assertEqual(first.pages[0].digest, digest(png(2, 3)))
        self.assertEqual(first.root_identity, (first.root.stat().st_dev, first.root.stat().st_ino))
        self.assertEqual(self.calls, 1)

        second = rasterize_ocr_source(
            self.reviewed,
            state_root=self.state,
            renderer_digest="sha256:" + "a" * 64,
            renderer=lambda *_: self.fail("idempotent reopen rendered again"),
        )
        self.assertEqual(second, first)
        self.assertEqual(validate_raster_bundle(second), second)
        self.assertEqual(self.calls, 1)

    def test_pdf_requires_explicit_ocr_selection_before_renderer(self):
        body = b"%PDF-1.7\n%%EOF\n"
        self.source.write_bytes(body)
        reviewed = ReviewedOcrSource("scan-01", self.source, "application/pdf", digest(body), False)
        with self.assertRaisesRegex(ContractError, "explicit OCR selection"):
            rasterize_ocr_source(reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=self.render)
        self.assertEqual(self.calls, 0)

    def test_source_digest_and_identity_drift_fail_before_publication(self):
        wrong = ReviewedOcrSource("scan-01", self.source, "image/png", "sha256:" + "f" * 64, True)
        with self.assertRaises(ContractError):
            rasterize_ocr_source(wrong, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=self.render)
        self.assertFalse(self.state.exists())

    def test_page_and_pixel_budgets_fail_closed_without_final_tree(self):
        self.assertEqual(ocr_raster._MAX_PIXELS_TOTAL, 1_000_000_000)

        def too_many(*_):
            return tuple(RasterPageOutput(1, 1, png(1, 1)) for _ in range(101))

        with self.assertRaises(ContractError):
            rasterize_ocr_source(self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=too_many)
        self.assertFalse(any(self.state.glob("raster-*")))

        def too_large(*_):
            return (RasterPageOutput(25_000_001, 1, png(1, 1)),)

        with self.assertRaises(ContractError):
            rasterize_ocr_source(self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=too_large)
        self.assertFalse(any(self.state.glob("raster-*")))

        outputs = tuple(RasterPageOutput(1, 3, png(1, 3)) for _ in range(2))
        with patch.object(ocr_raster, "_MAX_PIXELS_TOTAL", 6):
            accepted = rasterize_ocr_source(
                self.reviewed, state_root=self.state,
                renderer_digest="sha256:" + "a" * 64,
                renderer=lambda *_: outputs,
            )
        self.assertEqual(len(accepted.pages), 2)
        with patch.object(ocr_raster, "_MAX_PIXELS_TOTAL", 5), self.assertRaises(ContractError):
            rasterize_ocr_source(
                self.reviewed, state_root=self.root / "over-aggregate",
                renderer_digest="sha256:" + "a" * 64,
                renderer=lambda *_: outputs,
            )

    def test_invalid_source_and_rendered_png_are_rejected(self):
        self.source.write_bytes(b"not a png")
        reviewed = ReviewedOcrSource("scan-01", self.source, "image/png", digest(b"not a png"), True)
        with self.assertRaises(ContractError):
            rasterize_ocr_source(reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=self.render)

        self.source.write_bytes(png(2, 3))
        with self.assertRaises(ContractError):
            rasterize_ocr_source(self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=lambda *_: (RasterPageOutput(2, 3, b"bad"),))

    def test_png_header_resource_limits_win_before_rendering(self):
        def chunk(kind, value):
            return (
                struct.pack(">I", len(value)) + kind + value
                + struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF)
            )

        for width, height in ((25_000_001, 1), (25_000_000, 25_000_000)):
            with self.subTest(width=width, height=height):
                body = (
                    b"\x89PNG\r\n\x1a\n"
                    + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                    + chunk(b"IEND", b"")
                )
                self.source.write_bytes(body)
                reviewed = ReviewedOcrSource(
                    "scan-01", self.source, "image/png", digest(body), True
                )
                with self.assertRaises(OcrRasterRejected) as caught:
                    rasterize_ocr_source(
                        reviewed,
                        state_root=self.state,
                        renderer_digest="sha256:" + "a" * 64,
                        renderer=lambda *_: self.fail("resource-limited source rendered"),
                    )
                self.assertEqual(caught.exception.category, "resource_limit")
                self.assertFalse(self.state.exists())

    def test_actual_sandbox_renders_png_and_pdf_offline(self):
        runtime = Path(__file__).parents[1] / ".ao-lore" / "ocr-runtime" / "ocr-runtime-v0.1"
        if not runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        rendered = render_in_ocr_sandbox(
            self.source.read_bytes(), "image/png", {"dpi": 300}, runtime_root=runtime
        )
        self.assertEqual(len(rendered), 1)
        self.assertEqual((rendered[0].width, rendered[0].height), (2, 3))
        self.assertEqual(rendered[0].body[:8], b"\x89PNG\r\n\x1a\n")

        fixture = Path(__file__).parents[1] / "tests" / "fixtures" / "ao_lore" / "pdf" / "simple-structure.pdf"
        rendered = render_in_ocr_sandbox(
            fixture.read_bytes(), "application/pdf", {"dpi": 300}, runtime_root=runtime
        )
        self.assertGreaterEqual(len(rendered), 1)
        self.assertTrue(all(page.body.startswith(b"\x89PNG\r\n\x1a\n") for page in rendered))

    def test_source_links_and_renderer_time_swap_are_rejected(self):
        linked = self.root / "linked.png"
        linked.hardlink_to(self.source)
        reviewed = ReviewedOcrSource("scan-02", linked, "image/png", self.reviewed.source_digest, True)
        with self.assertRaises(ContractError):
            rasterize_ocr_source(reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=self.render)

        linked.unlink()
        linked.symlink_to(self.source)
        with self.assertRaises(ContractError):
            rasterize_ocr_source(reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=self.render)

        linked.unlink()
        original = self.source.read_bytes()

        def swap(*_):
            replacement = self.root / "replacement.png"
            replacement.write_bytes(original)
            replacement.replace(self.source)
            return (RasterPageOutput(2, 3, png(2, 3)),)

        with self.assertRaisesRegex(ContractError, "identity drifted"):
            rasterize_ocr_source(self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=swap)
        self.assertFalse(any(self.state.glob("raster-*")))
        self.assertFalse(any(self.state.glob(".*.partial")))

    def test_partial_collision_and_foreign_entries_are_preserved(self):
        self.state.mkdir()
        partial = self.state / f".raster-scan-01-{self.reviewed.source_digest[7:23]}.partial"
        partial.mkdir()
        sentinel = partial / "foreign"
        sentinel.write_text("preserve", encoding="utf-8")
        with self.assertRaisesRegex(ContractError, "intent is invalid"):
            rasterize_ocr_source(self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=self.render)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_publication_race_and_baseexception_leave_no_owned_partial(self):
        def collision(*_):
            raise ContractError("OCR raster publication collision")

        with patch("ao_lore.ocr_raster._rename_noreplace", side_effect=collision):
            with self.assertRaisesRegex(ContractError, "publication collision"):
                rasterize_ocr_source(self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64, renderer=self.render)
        self.assertFalse(any(self.state.glob(".*.partial")))

        with self.assertRaises(KeyboardInterrupt):
            rasterize_ocr_source(
                self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64,
                renderer=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
            )
        self.assertFalse(any(self.state.glob(".*.partial")))

    def test_encrypted_and_truncated_pdf_fail_inside_sandbox(self):
        runtime = Path(__file__).parents[1] / ".ao-lore" / "ocr-runtime" / "ocr-runtime-v0.1"
        if not runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        fixtures = Path(__file__).parents[1] / "tests" / "fixtures" / "ao_lore" / "pdf"
        for name in ("encrypted-document.pdf", "truncated-document.pdf"):
            with self.subTest(name=name), self.assertRaises(ContractError):
                render_in_ocr_sandbox((fixtures / name).read_bytes(), "application/pdf", {"dpi": 300}, runtime_root=runtime)

    def test_incomplete_and_complete_partial_publications_recover(self):
        self.state.mkdir()
        partial = self.state / f".raster-scan-01-{self.reviewed.source_digest[7:23]}.partial"
        partial.mkdir()
        (partial / "intent.json").write_bytes(
            ocr_raster._intent_bytes(self.reviewed, "sha256:" + "a" * 64)
        )
        (partial / "page-0001.png").write_bytes(png(2, 3))
        recovered = rasterize_ocr_source(
            self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64,
            renderer=self.render,
        )
        self.assertEqual(self.calls, 1)
        self.assertTrue(recovered.root.is_dir())

        final = recovered.root
        final.chmod(0o755)
        partial = final.with_name("." + final.name + ".partial")
        final.rename(partial)
        reopened = rasterize_ocr_source(
            self.reviewed, state_root=self.state, renderer_digest="sha256:" + "a" * 64,
            renderer=lambda *_: self.fail("complete partial rendered again"),
        )
        self.assertEqual(reopened.root, final)
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
