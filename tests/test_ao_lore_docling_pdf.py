import copy
import hashlib
import sys
import unittest

from ao_lore.benchmark import build_manifest, canonical_digest
from ao_lore.docling_pdf import ConvertedPdf, DoclingPdfAdapter
from ao_lore.parsing import ParsingError


VERSION = "2.118.1"


def benchmark(*, version=VERSION, failures=None, configuration_digest=None):
    return build_manifest(
        fixture_corpus_digest="sha256:" + "a" * 64,
        parser_id="docling",
        parser_version=version,
        parser_configuration_digest=configuration_digest or canonical_digest({
            "allowed_formats": ["pdf"], "do_ocr": False,
            "max_file_size": 1024, "max_num_pages": 10, "max_blocks": 10,
        }),
        runtime="python-3.12",
        platform="linux-x86_64",
        document_ir_version="ao.lore.document-ir.v0.1",
        commands=[["python3", "-m", "ao_lore", "benchmark", "pdf"]],
        raw_metrics={
            "structural_fidelity": 0.9, "text_fidelity": 0.95,
            "source_location_fidelity": 0.9, "reliability": 1.0,
            "deterministic_repeatability": 1.0,
        },
        normalized_scores={
            "structural_fidelity": 0.9, "text_fidelity": 0.95,
            "source_location_fidelity": 0.9, "reliability": 1.0,
            "deterministic_repeatability": 1.0,
        },
        failures=failures or [],
        exclusions=[],
    )


class FakeBackend:
    version = VERSION
    allowed_formats = ("pdf",)
    ocr_enabled = False

    def __init__(self, converted=None, error=None):
        self.converted = converted or ConvertedPdf(
            status="success",
            page_count=1,
            blocks=(
                {"type": "heading", "text": "Evidence", "page": 1,
                 "coordinates": [10.0, 20.0, 30.0, 40.0], "level": 1},
                {"type": "paragraph", "text": "Bounded local PDF."},
            ),
        )
        self.error = error
        self.calls = []

    def convert(self, data, name, *, max_file_size, max_num_pages):
        self.calls.append({
            "data": data, "name": name, "max_file_size": max_file_size,
            "max_num_pages": max_num_pages,
        })
        if self.error:
            raise self.error
        return self.converted


def source(data=b"%PDF-safe"):
    return {
        "resource": "sources/public-safe.pdf",
        "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
        "media_type": "application/pdf",
        "data": data,
    }


class DoclingPdfAdapterTests(unittest.TestCase):
    def adapter(self, backend=None, manifest=None):
        return DoclingPdfAdapter(
            manifest or benchmark(), backend=backend or FakeBackend(),
            installed_version=VERSION, max_file_size=1024,
            max_num_pages=10, max_blocks=10,
        )

    def test_fake_boundary_is_pdf_only_ocr_off_bounded_and_canonical(self):
        backend = FakeBackend()
        adapter = self.adapter(backend)
        output = adapter.parse(source())
        self.assertEqual(backend.allowed_formats, ("pdf",))
        self.assertFalse(backend.ocr_enabled)
        self.assertEqual(backend.calls[0]["data"], b"%PDF-safe")
        self.assertEqual(backend.calls[0]["name"], "public-safe.pdf")
        self.assertEqual(backend.calls[0]["max_file_size"], 1024)
        self.assertEqual(backend.calls[0]["max_num_pages"], 10)
        ir = output.document_ir
        self.assertEqual(ir["parser"]["parser_id"], "docling")
        self.assertEqual(ir["parser"]["parser_version"], VERSION)
        self.assertEqual(ir["blocks"][0]["source_span"]["page"], 1)
        self.assertEqual(ir["blocks"][0]["source_span"]["coordinates"], [10.0, 20.0, 30.0, 40.0])
        self.assertEqual(output.critical_failures, ())
        self.assertEqual(set(output.quality_components), {
            "text_coverage", "structural_completeness",
            "source_span_coverage", "document_ir_valid",
        })
        capability = adapter.capability
        self.assertFalse(capability["ocr_capable"])
        self.assertEqual(capability["benchmark_result_refs"], [benchmark()["result_digest"]])

    def test_manifest_version_configuration_and_failure_drift_are_rejected(self):
        for manifest, installed in (
            (benchmark(version="2.118.0"), VERSION),
            (benchmark(), "2.118.0"),
            (benchmark(failures=["fixture-failed"]), VERSION),
            (benchmark(configuration_digest="sha256:" + "0" * 64), VERSION),
        ):
            with self.subTest(manifest=manifest["parser_version"], installed=installed):
                with self.assertRaises(ParsingError):
                    DoclingPdfAdapter(
                        manifest, backend=FakeBackend(), installed_version=installed,
                        max_file_size=1024, max_num_pages=10, max_blocks=10,
                    )

    def test_source_contract_media_digest_size_and_resource_fail_closed(self):
        adapter = self.adapter()
        mutations = []
        mutations.append({**source(), "unknown": True})
        mutations.append({**source(), "media_type": "text/plain"})
        mutations.append({**source(), "digest": "sha256:" + "0" * 64})
        mutations.append({**source(), "data": b"x" * 1025,
                          "digest": "sha256:" + hashlib.sha256(b"x" * 1025).hexdigest()})
        mutations.append({**source(), "resource": "../private.pdf"})
        mutations.append({**source(), "resource": "https://example.invalid/a.pdf"})
        for value in mutations:
            with self.subTest(value=set(value)):
                with self.assertRaises(ParsingError):
                    adapter.parse(value)

    def test_partial_failed_empty_and_excessive_outputs_fail_closed(self):
        cases = (
            ConvertedPdf(status="partial", page_count=1, blocks=({"type": "paragraph", "text": "x"},)),
            ConvertedPdf(status="failure", page_count=0, blocks=()),
            ConvertedPdf(status="success", page_count=1, blocks=()),
            ConvertedPdf(status="success", page_count=11, blocks=({"type": "paragraph", "text": "x"},)),
            ConvertedPdf(status="success", page_count=1, blocks=tuple({"type": "paragraph", "text": "x"} for _ in range(11))),
        )
        for converted in cases:
            with self.subTest(status=converted.status, blocks=len(converted.blocks)):
                with self.assertRaises(ParsingError):
                    self.adapter(FakeBackend(converted=converted)).parse(source())

    def test_backend_errors_are_redacted(self):
        private = "/private/path secret source text"
        adapter = self.adapter(FakeBackend(error=RuntimeError(private)))
        with self.assertRaisesRegex(ParsingError, "PDF conversion failed") as raised:
            adapter.parse(source())
        self.assertNotIn(private, str(raised.exception))

    def test_optional_module_does_not_import_docling_at_module_import(self):
        self.assertNotIn("docling.document_converter", sys.modules)


if __name__ == "__main__":
    unittest.main()
