import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ao_lore import pdf_benchmark as pdf_benchmark_module
from ao_lore.benchmark import canonical_digest, verify_manifest
from ao_lore.parsing import ParseOutput, ParsingError, validate_docling_benchmark
from ao_lore.pdf_benchmark import (
    LimitedPdfAdapter,
    build_private_pdf_aggregate,
    load_pdf_corpus,
    private_pdf_calibration_not_supplied,
    run_pdf_benchmark,
)
from ao_lore.__main__ import main


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "ao_lore" / "pdf"
SPEC_PATH = FIXTURE_ROOT / "fixture-spec.json"


def generator_module():
    path = FIXTURE_ROOT / "generate.py"
    spec = importlib.util.spec_from_file_location("ao_lore_pdf_fixture_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ScriptedPdfAdapter:
    def __init__(self, parser_id="docling", version="2.118.1", *, stable=True,
                 outcome_overrides=None, forced_failures=None):
        self.capability = {"parser_id": parser_id, "version": version}
        self.stable = stable
        self.outcome_overrides = outcome_overrides or {}
        self.forced_failures = forced_failures or {}
        self.calls = 0

    def parse(self, source):
        self.calls += 1
        fixture_id = Path(source["resource"]).stem
        fixture = next(
            item for item in json.loads(SPEC_PATH.read_text(encoding="utf-8"))["fixtures"]
            if item["id"] == fixture_id
        )
        if fixture_id in self.forced_failures:
            raise ParsingError(self.forced_failures[fixture_id])
        outcome = self.outcome_overrides.get(fixture_id, fixture["expected_outcome"])
        if outcome == "rejected":
            message = (
                "PDF conversion produced no readable content"
                if fixture["expected_failure"] in (None, "no_readable_content")
                else "PDF conversion failed"
            )
            raise ParsingError(message)
        suffix = "" if self.stable or self.calls % 2 else " drift"
        joined = " ".join(fixture["expected_text"]) + suffix
        blocks = []
        for index, block_type in enumerate(fixture["required_block_types"], 1):
            blocks.append({
                "id": f"b{index}", "type": block_type, "text": joined,
                "source_span": {"start": 0, "end": len(joined), "page": index,
                                "coordinates": [10.0, 10.0, 100.0, 20.0]},
            })
        return ParseOutput(
            {
                "schema_version": "ao.lore.document-ir.v0.1",
                "document_id": source["digest"],
                "source": {key: source[key] for key in ("resource", "digest", "media_type")},
                "parser": {"parser_id": self.capability["parser_id"],
                           "parser_version": self.capability["version"],
                           "configuration_digest": canonical_digest({})},
                "blocks": blocks,
                "metadata": {"format": "pdf", "ocr_used": False},
            },
            {"text_coverage": 1.0, "structural_completeness": 1.0,
             "source_span_coverage": 1.0, "document_ir_valid": 1.0},
        )


class PdfFixtureTests(unittest.TestCase):
    EXPECTED_IDS = {
        "simple-structure", "table-and-link", "multi-page-sections",
        "two-column-reading-order", "complex-table", "unicode-links-footnotes",
        "repeated-header-footer", "long-boundary-document", "empty-document",
        "truncated-document", "encrypted-document", "image-only-document",
    }

    def test_corpus_has_exact_public_fixture_matrix_and_stable_outcomes(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual({fixture["id"] for fixture in corpus["fixtures"]}, self.EXPECTED_IDS)
        rejected = 0
        for fixture in corpus["fixtures"]:
            self.assertNotIn("ocr", fixture)
            serialized = json.dumps(fixture, sort_keys=True).lower()
            self.assertNotIn("private", serialized)
            self.assertNotIn("file://", serialized)
            if fixture["expected_outcome"] == "accepted":
                self.assertIsNone(fixture["expected_failure"])
                self.assertTrue(fixture["expected_text"])
                self.assertTrue(fixture["required_block_types"])
            else:
                rejected += 1
                self.assertEqual(fixture["expected_text"], [])
                self.assertEqual(fixture["required_block_types"], [])
                self.assertIn(fixture["expected_failure"], {
                    "conversion_failed", "no_readable_content", "encrypted_document",
                    "invalid_pdf",
                })
        self.assertEqual(rejected, 4)

    def test_generation_is_deterministic_and_spec_binds_exact_bytes(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        generator = generator_module()
        for fixture in corpus["fixtures"]:
            first = generator.generate_fixture_bytes(fixture)
            second = generator.generate_fixture_bytes(fixture)
            self.assertEqual(first, second)
            self.assertEqual("sha256:" + hashlib.sha256(first).hexdigest(), fixture["sha256"])
            self.assertEqual(first, (FIXTURE_ROOT / fixture["file"]).read_bytes())

    def test_generated_fixtures_expose_declared_page_and_layout_structures(self):
        by_id = {
            fixture["id"]: (FIXTURE_ROOT / fixture["file"]).read_bytes()
            for fixture in json.loads(SPEC_PATH.read_text(encoding="utf-8"))["fixtures"]
        }
        self.assertIn(b"/Count 3", by_id["multi-page-sections"])
        self.assertGreaterEqual(by_id["multi-page-sections"].count(b"/Type /Page "), 3)
        self.assertIn(b"% AO-LORE-LAYOUT two-column", by_id["two-column-reading-order"])
        self.assertIn(b"% AO-LORE-TABLE complex", by_id["complex-table"])
        self.assertIn(b"/Subtype /Link", by_id["unicode-links-footnotes"])
        self.assertIn(b"<FEFF", by_id["unicode-links-footnotes"])
        self.assertGreaterEqual(by_id["repeated-header-footer"].count(b"Repeated Public Header"), 2)
        self.assertIn(b"/Count 8", by_id["long-boundary-document"])
        self.assertNotIn(b" Tj", by_id["empty-document"])
        self.assertFalse(by_id["truncated-document"].endswith(b"%%EOF\n"))
        self.assertIn(b"/Encrypt", by_id["encrypted-document"])
        self.assertIn(b"/Subtype /Image", by_id["image-only-document"])

    def test_generator_rejects_unknown_keys_types_and_nonlocal_targets(self):
        fixture = json.loads(SPEC_PATH.read_text(encoding="utf-8"))["fixtures"][0]
        generator = generator_module()
        for changed in (
            {**fixture, "unknown": True},
            {**fixture, "lines": "not-a-list"},
            {**fixture, "file": "../escaped.pdf"},
            {**fixture, "expected_outcome": "rejected", "expected_text": [],
             "required_block_types": [], "expected_failure": []},
        ):
            with self.assertRaises(ValueError):
                generator.generate_fixture_bytes(changed)

    def test_generator_validates_the_complete_corpus_contract(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        generator = generator_module()
        generator.validate_corpus(corpus)
        for changed in (
            {**corpus, "unknown": True},
            {**corpus, "fixtures": "not-a-list"},
            {**corpus, "configuration": {**corpus["configuration"], "max_blocks": True}},
        ):
            with self.assertRaises(ValueError):
                generator.validate_corpus(changed)

    def test_generator_rejects_a_corpus_without_an_accepted_fixture(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        generator = generator_module()
        for fixture in corpus["fixtures"]:
            fixture["expected_outcome"] = "rejected"
            fixture["expected_failure"] = "conversion_failed"
            fixture["expected_text"] = []
            fixture["required_block_types"] = []
        with self.assertRaises(ValueError):
            generator.validate_corpus(corpus)

    def test_corpus_loader_rejects_digest_and_unknown_field_drift(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        load_pdf_corpus(SPEC_PATH)
        for mutation in ("digest", "unknown", "inconsistent_outcome", "invalid_types"):
            changed = json.loads(json.dumps(corpus))
            if mutation == "digest":
                changed["fixtures"][0]["sha256"] = "sha256:" + "0" * 64
            elif mutation == "unknown":
                changed["unknown"] = True
            elif mutation == "inconsistent_outcome":
                changed["fixtures"][0]["expected_failure"] = "invalid_pdf"
            else:
                changed["fixtures"][0]["kind"] = []
            with tempfile.TemporaryDirectory(dir=ROOT / "working") as name:
                path = Path(name) / "fixture-spec.json"
                path.write_text(json.dumps(changed), encoding="utf-8")
                with self.assertRaises(ParsingError):
                    load_pdf_corpus(path, fixture_root=FIXTURE_ROOT)


class PdfBenchmarkTests(unittest.TestCase):
    def test_default_benchmark_accepts_parse_output_subclasses_without_leaking_state(self):
        class SubclassOutput(ParseOutput):
            pass

        class SubclassAdapter(ScriptedPdfAdapter):
            def __init__(self, *, extra_state=False):
                super().__init__()
                self.extra_state = extra_state

            def parse(self, source):
                output = super().parse(source)
                subclass = SubclassOutput(
                    output.document_ir,
                    output.quality_components,
                    output.critical_failures,
                )
                if self.extra_state:
                    object.__setattr__(
                        subclass,
                        "private_state",
                        "/private/subclass-state-sentinel",
                    )
                return subclass

        corpus = load_pdf_corpus(SPEC_PATH)
        for extra_state in (False, True):
            with self.subTest(extra_state=extra_state):
                manifest = run_pdf_benchmark(
                    SubclassAdapter(extra_state=extra_state),
                    corpus,
                    fixture_root=FIXTURE_ROOT,
                    runtime="python-test",
                    platform="test-platform",
                )
                verify_manifest(manifest)
                serialized = json.dumps(manifest, sort_keys=True)
                self.assertNotIn("private_state", serialized)
                self.assertNotIn("subclass-state-sentinel", serialized)

    def test_private_calibration_absence_is_explicit_and_nonqualifying(self):
        report = private_pdf_calibration_not_supplied()
        self.assertEqual(report, {
            "schema_version": "ao.lore.private-pdf-calibration-status.v0.1",
            "status": "not_supplied",
            "qualified": False,
            "ocr_used": False,
            "provider_calls": False,
            "claims_authority_advance": False,
        })
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn("path", serialized)
        self.assertNotIn("content", serialized)
        self.assertNotIn("digest", serialized)

    def test_limited_rejection_category_depends_on_structure_not_filename(self):
        adapter = LimitedPdfAdapter()
        cases = (
            ("empty-document.pdf", "renamed-image.pdf", "conversion_failed"),
            ("image-only-document.pdf", "renamed-empty.pdf", "no_readable_content"),
        )
        for fixture_name, resource_name, expected_code in cases:
            data = (FIXTURE_ROOT / fixture_name).read_bytes()
            source = {
                "resource": f"fixtures/{resource_name}",
                "digest": "sha256:" + hashlib.sha256(data).hexdigest(),
                "media_type": "application/pdf",
                "data": data,
            }
            with self.subTest(fixture=fixture_name, resource=resource_name):
                with self.assertRaises(ParsingError) as caught:
                    adapter.parse(source)
                self.assertEqual(
                    pdf_benchmark_module._failure_code(caught.exception),
                    expected_code,
                )

    def test_private_aggregate_is_path_content_and_authority_free(self):
        private_results = [
            {
                "item_id": (
                    "secret-client.pdf:/private/calibration:"
                    "sentinel document content"
                ),
                "source_digest": "sha256:" + "a" * 64,
                "status": "accepted",
                "metrics": {"text_fidelity": 1.0, "structural_fidelity": 0.5},
            },
            {
                "item_id": "opaque-rejected",
                "source_digest": "sha256:" + "b" * 64,
                "status": "rejected",
                "error_code": "invalid_pdf",
            },
        ]

        report = build_private_pdf_aggregate(private_results)
        serialized = json.dumps(
            report, allow_nan=False, sort_keys=True, separators=(",", ":")
        )

        self.assertEqual(
            set(report),
            {
                "schema_version", "corpus_digest", "total", "successful",
                "rejected", "metrics", "exclusions", "failure_counts",
                "ocr_used", "provider_calls", "claims_authority_advance",
            },
        )
        self.assertEqual(
            report["schema_version"],
            "ao.lore.private-pdf-calibration-aggregate.v0.1",
        )
        self.assertEqual((report["total"], report["successful"], report["rejected"]), (2, 1, 1))
        self.assertEqual(report["metrics"], {"structural_fidelity": 0.5, "text_fidelity": 1.0})
        self.assertIn("source_location_fidelity", report["exclusions"])
        self.assertEqual(report["failure_counts"]["invalid_pdf"], 1)
        self.assertIs(report["ocr_used"], False)
        self.assertIs(report["provider_calls"], False)
        self.assertIs(report["claims_authority_advance"], False)
        for sentinel in (
            "secret-client.pdf", "/private/calibration",
            "sentinel document content", "opaque-rejected", "sha256:" + "a" * 64,
        ):
            self.assertNotIn(sentinel, serialized)

    def test_private_aggregate_is_deterministic_order_bound_and_detached(self):
        results = [
            {
                "item_id": "opaque-a", "source_digest": "sha256:" + "1" * 64,
                "status": "accepted", "metrics": {"text_fidelity": 0.25},
            },
            {
                "item_id": "opaque-b", "source_digest": "sha256:" + "1" * 64,
                "status": "accepted", "metrics": {"text_fidelity": 0.75},
            },
        ]
        first = build_private_pdf_aggregate(results)
        self.assertEqual((first["total"], first["successful"], first["rejected"]), (2, 2, 0))
        self.assertEqual(first["metrics"]["text_fidelity"], 0.5)
        self.assertEqual(first["corpus_digest"], canonical_digest([
            {"item_id": "opaque-a", "source_digest": "sha256:" + "1" * 64},
            {"item_id": "opaque-b", "source_digest": "sha256:" + "1" * 64},
        ]))
        self.assertEqual(first, build_private_pdf_aggregate(results))
        self.assertNotEqual(
            first["corpus_digest"], build_private_pdf_aggregate(list(reversed(results)))["corpus_digest"]
        )
        results[0]["item_id"] = "mutated"
        results[0]["metrics"]["text_fidelity"] = 1.0
        self.assertEqual(first["metrics"]["text_fidelity"], 0.5)

    def test_private_aggregate_rejects_malformed_or_leaky_input(self):
        accepted = {
            "item_id": "opaque-a", "source_digest": "sha256:" + "a" * 64,
            "status": "accepted", "metrics": {"text_fidelity": 1.0},
        }
        rejected = {
            "item_id": "opaque-b", "source_digest": "sha256:" + "b" * 64,
            "status": "rejected", "error_code": "invalid_pdf",
        }
        invalid = [
            [],
            [{**accepted, "unknown": True}],
            [{**accepted, "path": "/private/calibration"}],
            [{**accepted, "content": "sentinel document content"}],
            [{**accepted, "authority": True}],
            [{**accepted, "item_id": ""}],
            [{**accepted, "source_digest": "sha256:bad"}],
            [accepted, {**rejected, "item_id": accepted["item_id"]}],
            [{**accepted, "metrics": {"unknown": 1.0}}],
            [{**accepted, "metrics": {"text_fidelity": float("nan")}}],
            [{**accepted, "metrics": {"text_fidelity": 1.01}}],
            [{**accepted, "error_code": "invalid_pdf"}],
            [{**rejected, "metrics": {"text_fidelity": 0.0}}],
            [{**rejected, "error_code": "exception: secret-client.pdf"}],
        ]
        for results in invalid:
            with self.subTest(results=results):
                with self.assertRaises(ParsingError):
                    build_private_pdf_aggregate(results)

    def test_runner_is_repeatable_strict_and_manifest_verified(self):
        corpus = load_pdf_corpus(SPEC_PATH)
        manifest = run_pdf_benchmark(
            ScriptedPdfAdapter(), corpus, fixture_root=FIXTURE_ROOT,
            runtime="python-3.12", platform="linux-x86_64",
        )
        verify_manifest(manifest)
        self.assertEqual(manifest["parser_id"], "docling")
        self.assertEqual(manifest["parser_version"], "2.118.1")
        self.assertEqual(manifest["failures"], [])
        self.assertEqual(manifest["normalized_scores"]["deterministic_repeatability"], 1.0)
        self.assertIn("source_location_fidelity", manifest["raw_metrics"])
        self.assertIn("latency", manifest["raw_metrics"])
        self.assertIn("memory", manifest["raw_metrics"])
        expected_rejections = {
            item for item in manifest["exclusions"] if item.startswith("expected_rejection:")
        }
        self.assertEqual(expected_rejections, {
            "expected_rejection:empty-document:conversion_failed",
            "expected_rejection:truncated-document:conversion_failed",
            "expected_rejection:encrypted-document:conversion_failed",
            "expected_rejection:image-only-document:no_readable_content",
        })
        self.assertEqual(manifest["raw_metrics"]["reliability"], 1.0)

    def test_repeatability_failure_is_not_hidden(self):
        corpus = load_pdf_corpus(SPEC_PATH)
        manifest = run_pdf_benchmark(
            ScriptedPdfAdapter(stable=False), corpus, fixture_root=FIXTURE_ROOT,
            runtime="python-3.12", platform="linux-x86_64",
        )
        self.assertIn("repeatability_below_minimum", manifest["failures"])

    def test_unexpected_acceptance_and_rejection_are_critical_failures(self):
        corpus = load_pdf_corpus(SPEC_PATH)
        unexpectedly_accepted = run_pdf_benchmark(
            ScriptedPdfAdapter(outcome_overrides={"empty-document": "accepted"}), corpus,
            fixture_root=FIXTURE_ROOT, runtime="python-3.12", platform="linux-x86_64",
        )
        self.assertIn("empty-document:unexpected_acceptance", unexpectedly_accepted["failures"])
        unexpectedly_rejected = run_pdf_benchmark(
            ScriptedPdfAdapter(outcome_overrides={"simple-structure": "rejected"}), corpus,
            fixture_root=FIXTURE_ROOT, runtime="python-3.12", platform="linux-x86_64",
        )
        self.assertIn("simple-structure:no_readable_content", unexpectedly_rejected["failures"])
        with self.assertRaises(ParsingError):
            validate_docling_benchmark(unexpectedly_accepted)
        with self.assertRaises(ParsingError):
            validate_docling_benchmark(unexpectedly_rejected)

    def test_untrusted_exception_text_cannot_select_rejection_codes(self):
        corpus = load_pdf_corpus(SPEC_PATH)
        for message in (
            "invalid_pdf",
            "fixtures/encrypted-document.pdf was empty, truncated, and password protected",
        ):
            manifest = run_pdf_benchmark(
                ScriptedPdfAdapter(forced_failures={"simple-structure": message}), corpus,
                fixture_root=FIXTURE_ROOT, runtime="python-3.12", platform="linux-x86_64",
            )
            self.assertIn("simple-structure:conversion_failed", manifest["failures"])
            self.assertNotIn("simple-structure:invalid_pdf", manifest["failures"])
            self.assertNotIn("simple-structure:encrypted_document", manifest["failures"])

    def test_structured_rejection_is_the_only_source_of_closed_code_strings(self):
        rejection_type = getattr(pdf_benchmark_module, "PdfBenchmarkRejection", None)
        self.assertIsNotNone(rejection_type)
        for code in (
            "conversion_failed", "invalid_pdf", "encrypted_document", "no_readable_content",
        ):
            self.assertEqual(pdf_benchmark_module._failure_code(rejection_type(code)), code)
            self.assertEqual(
                pdf_benchmark_module._failure_code(ParsingError(code)),
                "conversion_failed",
            )
        with self.assertRaises(ValueError):
            rejection_type([])

    def test_limited_baseline_uses_the_same_numeric_runner(self):
        corpus = load_pdf_corpus(SPEC_PATH)
        manifest = run_pdf_benchmark(
            ScriptedPdfAdapter(parser_id="limited-pdf", version="0.1.0"),
            corpus, fixture_root=FIXTURE_ROOT,
            runtime="python-3.12", platform="linux-x86_64",
        )
        verify_manifest(manifest)
        self.assertEqual(manifest["parser_id"], "limited-pdf")
        self.assertEqual(set(manifest["normalized_scores"]), set(corpus["metrics"]))

    def test_cli_can_emit_shipped_limited_baseline_evidence(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            runtime_home = Path(name)
            out = runtime_home / "limited.json"
            stdout = StringIO()
            stderr = StringIO()
            with patch.dict(os.environ, {"AO_LORE_HOME": str(runtime_home)}), \
                    redirect_stdout(stdout), redirect_stderr(stderr):
                status = main([
                    "benchmark", "pdf", "--adapter", "limited",
                    "--corpus", str(SPEC_PATH), "--out", str(out),
                ])
            self.assertEqual((status, stderr.getvalue()), (0, ""))
            manifest = json.loads(out.read_text(encoding="utf-8"))
            verify_manifest(manifest)
            self.assertEqual(manifest["parser_id"], "limited-pdf")
            self.assertEqual(manifest["parser_version"], "0.1.0")
            self.assertEqual(manifest["failures"], [])
            self.assertEqual({
                item for item in manifest["exclusions"]
                if item.startswith("expected_rejection:")
            }, {
                "expected_rejection:empty-document:conversion_failed",
                "expected_rejection:truncated-document:conversion_failed",
                "expected_rejection:encrypted-document:conversion_failed",
                "expected_rejection:image-only-document:no_readable_content",
            })

    def test_non_applicable_fixture_metrics_are_excluded_not_zeroed(self):
        corpus = load_pdf_corpus(SPEC_PATH)
        for fixture in corpus["fixtures"]:
            fixture["applicable_metrics"].remove("source_location_fidelity")
        manifest = run_pdf_benchmark(
            ScriptedPdfAdapter(), corpus, fixture_root=FIXTURE_ROOT,
            runtime="python-3.12", platform="linux-x86_64",
        )
        self.assertIn("source_location_fidelity", manifest["exclusions"])
        self.assertIsNone(manifest["raw_metrics"]["source_location_fidelity"])
        self.assertNotIn("source_location_fidelity", manifest["normalized_scores"])

    def test_cli_writes_exclusive_runtime_manifest(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as name:
            runtime_home = Path(name)
            out = runtime_home / "benchmarks" / "docling.json"
            out.parent.mkdir()
            stdout = StringIO()
            stderr = StringIO()
            with patch.dict(os.environ, {"AO_LORE_HOME": str(runtime_home)}), patch(
                "ao_lore.__main__.create_docling_calibration_adapter",
                return_value=ScriptedPdfAdapter(),
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                status = main([
                    "benchmark", "pdf", "--corpus", str(SPEC_PATH),
                    "--out", str(out),
                ])
            self.assertEqual((status, stderr.getvalue()), (0, ""))
            manifest = json.loads(out.read_text(encoding="utf-8"))
            verify_manifest(manifest)
            self.assertEqual(json.loads(stdout.getvalue())["result_digest"], manifest["result_digest"])

            with patch.dict(os.environ, {"AO_LORE_HOME": str(runtime_home)}), patch(
                "ao_lore.__main__.create_docling_calibration_adapter",
                return_value=ScriptedPdfAdapter(),
            ), redirect_stdout(StringIO()), redirect_stderr(stderr := StringIO()):
                status = main([
                    "benchmark", "pdf", "--corpus", str(SPEC_PATH),
                    "--out", str(out),
                ])
            self.assertEqual(status, 2)
            self.assertIn("benchmark rejected", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
