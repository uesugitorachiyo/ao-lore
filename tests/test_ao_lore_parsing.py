import tempfile
import unittest
from pathlib import Path

from ao_lore.benchmark import build_manifest, canonical_digest
from ao_lore.docx_ooxml import DocxLimits, docx_configuration_digest
from ao_lore.parsing import (
    NativeMarkdownAdapter,
    ParseOutput,
    ParserRegistry,
    ParsingError,
    ProductionParser,
    ScriptedParserAdapter,
    default_capability_profiles,
    read_bounded_source,
)


DIGEST = "sha256:" + "d" * 64


def capability(parser_id, score, *, media_type="application/pdf", extension=".pdf"):
    return {
        "parser_id": parser_id,
        "version": "1.0",
        "supported_mime_types": [media_type],
        "supported_extensions": [extension],
        "structural_elements": ["headings", "links"],
        "format_status": {media_type: "native"},
        "deterministic_offline": True,
        "frontier_service_required": False,
        "runtime_available": True,
        "license_id": "Apache-2.0",
        "sandbox_supported": True,
        "max_file_bytes": 100000,
        "canonical_ir_versions": ["ao.lore.document-ir.v0.1"],
        "selection_metrics": {
            "structural_fidelity": score,
            "text_fidelity": score,
            "source_location_fidelity": score,
            "reliability": 1,
            "deterministic_repeatability": 1,
            "latency": 0.1,
            "memory": 0.1,
            "external_cost": 0,
        },
        "benchmark_result_refs": [DIGEST],
    }


def selection_profile():
    return {
        "profile_id": "pdf-technical-v1",
        "weights": {"structural_fidelity": 0.5, "text_fidelity": 0.5},
        "penalty_weights": {},
        "normalization_bounds": {},
        "missing_optional_policy": "ineligible",
        "tie_break_order": ["required_feature_coverage", "structural_fidelity", "parser_id"],
        "fixture_corpus_digest": DIGEST,
    }


def quality_profile():
    return {
        "profile_id": "pdf-quality-v1",
        "component_weights": {"text_coverage": 0.5, "structural_completeness": 0.3, "source_span_coverage": 0.1, "document_ir_valid": 0.1},
        "accept_threshold": 0.8,
        "fallback_threshold": 0.7,
        "maximum_fallbacks": 1,
        "critical_failures": ["invalid_ir", "source_digest_mismatch", "parser_crash", "no_readable_content", "lost_provenance"],
        "calibration_result_digest": DIGEST,
        "intermediate_decision": "quarantine",
        "exhausted_decision": "reject",
    }


def source_ir(source, parser_id, *, text="evidence", quality=1.0):
    return ParseOutput(
        document_ir={
            "schema_version": "ao.lore.document-ir.v0.1",
            "document_id": source["digest"],
            "source": {"resource": source["resource"], "digest": source["digest"], "media_type": source["media_type"]},
            "parser": {"parser_id": parser_id, "parser_version": "1.0", "configuration_digest": canonical_digest({})},
            "blocks": [{"id": "b1", "type": "paragraph", "text": text, "source_span": {"start": 0, "end": len(text)}}],
            "metadata": {},
        },
        quality_components={"text_coverage": quality, "structural_completeness": quality, "source_span_coverage": 1, "document_ir_valid": 1},
    )


def docx_benchmark(
    *,
    decision="hold",
    fixture_corpus_digest=DIGEST,
    result_digest=None,
    metrics=None,
    stable_failures=None,
):
    manifest = {
        "schema_version": "ao.lore.docx-benchmark-result.v0.1",
        "fixture_corpus_digest": fixture_corpus_digest,
        "parser_id": "native-docx-ooxml",
        "parser_version": "1.0.0",
        "parser_configuration_digest": docx_configuration_digest(DocxLimits()),
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
        or {"invalid_package": 1, "unsupported_active_content": 11},
        "repeatability": {"runs": 2, "identical": True, "score": 1.0},
        "unexpected_failures": [],
        "decision": decision,
    }
    manifest["result_digest"] = result_digest or canonical_digest(manifest)
    return manifest


class ProductionParsingTests(unittest.TestCase):
    def request(self):
        return {
            "media_type": "application/pdf", "extension": ".pdf", "required_structural_elements": ["headings"],
            "canonical_ir_version": "ao.lore.document-ir.v0.1", "network_allowed": False,
            "license_allowlist": ["Apache-2.0"], "sandbox_required": True, "file_bytes": 8,
        }

    def test_docling_default_is_evidence_bound_and_replaceable(self):
        unbound = {item["parser_id"]: item for item in default_capability_profiles()}
        self.assertFalse(unbound["docling"]["runtime_available"])
        self.assertEqual(unbound["docling"]["benchmark_result_refs"], [])
        benchmark = build_manifest(
            fixture_corpus_digest=DIGEST,
            parser_id="docling",
            parser_version="2.118.1",
            parser_configuration_digest=canonical_digest({}),
            runtime="python-3.11",
            platform="linux-amd64",
            document_ir_version="ao.lore.document-ir.v0.1",
            commands=[["python", "benchmark.py"]],
            raw_metrics={
                "structural_fidelity": 0.8, "text_fidelity": 0.9,
                "source_location_fidelity": 0.9, "reliability": 1.0,
                "deterministic_repeatability": 1.0,
            },
            normalized_scores={
                "structural_fidelity": 0.8, "text_fidelity": 0.9,
                "source_location_fidelity": 0.9, "reliability": 1.0,
                "deterministic_repeatability": 1.0,
            },
            failures=[],
            exclusions=[],
        )
        defaults = {item["parser_id"]: item for item in default_capability_profiles(benchmark)}
        self.assertEqual(defaults["docling"]["format_status"]["application/pdf"], "native")
        self.assertEqual(defaults["docling"]["benchmark_result_refs"], [benchmark["result_digest"]])
        wrong_version = build_manifest(
            fixture_corpus_digest=DIGEST,
            parser_id="docling",
            parser_version="2.0",
            parser_configuration_digest=canonical_digest({}),
            runtime="python-3.11",
            platform="linux-amd64",
            document_ir_version="ao.lore.document-ir.v0.1",
            commands=[["python", "benchmark.py"]],
            raw_metrics=benchmark["raw_metrics"],
            normalized_scores=benchmark["normalized_scores"],
            failures=[],
            exclusions=[],
        )
        with self.assertRaises(ParsingError):
            default_capability_profiles(wrong_version)

        calls = []
        registry = ParserRegistry()
        for parser_id, score in (("docling", 0.8), ("measured-challenger", 0.95)):
            registry.register(ScriptedParserAdapter(capability(parser_id, score), lambda src, pid=parser_id: calls.append(pid) or source_ir(src, pid)))
        result = ProductionParser(registry).parse_bytes(b"PDF data", "sources/a.pdf", self.request(), selection_profile(), quality_profile())
        self.assertEqual(result["selected_parser"], "measured-challenger")
        self.assertEqual(calls, ["measured-challenger"])
        self.assertEqual(result["decision"], "accept")

    def test_quality_failure_invokes_only_next_ranked_adapter(self):
        calls = []
        registry = ParserRegistry()
        for parser_id, score, quality in (("primary", 1.0, 0.2), ("fallback", 0.9, 1.0), ("unused", 0.8, 1.0)):
            registry.register(ScriptedParserAdapter(capability(parser_id, score), lambda src, p=parser_id, q=quality: calls.append(p) or source_ir(src, p, quality=q)))
        result = ProductionParser(registry).parse_bytes(b"PDF data", "sources/a.pdf", self.request(), selection_profile(), quality_profile())
        self.assertEqual(calls, ["primary", "fallback"])
        self.assertEqual([a["parser_id"] for a in result["attempts"]], calls)
        self.assertEqual(result["selected_parser"], "fallback")

    def test_critical_failure_never_passes_numerically(self):
        registry = ParserRegistry()
        profile = capability("bad", 1)
        output = lambda src: ParseOutput(source_ir(src, "bad").document_ir, {"text_coverage": 1, "structural_completeness": 1, "source_span_coverage": 1, "document_ir_valid": 1}, ["parser_crash"])
        registry.register(ScriptedParserAdapter(profile, output))
        result = ProductionParser(registry).parse_bytes(b"PDF data", "sources/a.pdf", self.request(), selection_profile(), quality_profile())
        self.assertEqual(result["decision"], "reject")
        self.assertIsNone(result["document_ir"])

    def test_source_digest_mismatch_is_a_critical_failure(self):
        registry = ParserRegistry()
        registry.register(ScriptedParserAdapter(capability("liar", 1), lambda src: source_ir({**src, "digest": "sha256:" + "0" * 64}, "liar")))
        result = ProductionParser(registry).parse_bytes(b"PDF data", "sources/a.pdf", self.request(), selection_profile(), quality_profile())
        self.assertIn("source_digest_mismatch", result["attempts"][0]["quality_report"]["critical_failures"])
        self.assertEqual(result["decision"], "reject")

    def test_selection_score_and_actual_quality_remain_separate(self):
        registry = ParserRegistry()
        registry.register(ScriptedParserAdapter(capability("one", 1), lambda src: source_ir(src, "one")))
        result = ProductionParser(registry).parse_bytes(b"PDF data", "sources/a.pdf", self.request(), selection_profile(), quality_profile())
        self.assertIn("selection_score", result["selection_report"]["candidates"][0])
        self.assertNotIn("overall_quality_score", result["selection_report"]["candidates"][0])
        self.assertIn("overall_quality_score", result["quality_report"])
        self.assertNotIn("selection_score", result["quality_report"])

    def test_docx_default_is_qualification_bound(self):
        unbound = {item["parser_id"]: item for item in default_capability_profiles()}
        self.assertFalse(unbound["native-docx-ooxml"]["runtime_available"])
        self.assertEqual(unbound["native-docx-ooxml"]["benchmark_result_refs"], [])
        self.assertEqual(
            unbound["native-docx-ooxml"]["implementation_status"],
            "qualification-required",
        )

        with self.assertRaises(ParsingError):
            default_capability_profiles(docx_benchmark=docx_benchmark())

        qualified = {
            item["parser_id"]: item
            for item in default_capability_profiles(
                docx_benchmark=docx_benchmark(),
                docx_expected_corpus_digest=DIGEST,
            )
        }
        self.assertTrue(qualified["native-docx-ooxml"]["runtime_available"])
        self.assertEqual(
            qualified["native-docx-ooxml"]["benchmark_result_refs"],
            [docx_benchmark()["result_digest"]],
        )
        self.assertEqual(
            qualified["native-docx-ooxml"]["selection_metrics"]["text_fidelity"],
            1.0,
        )

        candidate = {
            item["parser_id"]: item
            for item in default_capability_profiles(
                docx_benchmark=docx_benchmark(decision="candidate_change"),
                docx_expected_corpus_digest=DIGEST,
            )
        }
        self.assertFalse(candidate["native-docx-ooxml"]["runtime_available"])
        self.assertEqual(candidate["native-docx-ooxml"]["benchmark_result_refs"], [])

        with self.assertRaises(ParsingError):
            default_capability_profiles(
                docx_benchmark=docx_benchmark(
                    fixture_corpus_digest="sha256:" + "0" * 64,
                ),
                docx_expected_corpus_digest=DIGEST,
            )

        with self.assertRaises(ParsingError):
            default_capability_profiles(
                docx_benchmark=docx_benchmark(
                    result_digest="sha256:" + "0" * 64,
                ),
                docx_expected_corpus_digest=DIGEST,
            )

        with self.assertRaises(ParsingError):
            default_capability_profiles(
                docx_benchmark=docx_benchmark(
                    stable_failures={"invalid_package": 1, "unsupported_active_content": 10},
                ),
                docx_expected_corpus_digest=DIGEST,
            )


class NativeAndContainmentTests(unittest.TestCase):
    def test_native_markdown_emits_canonical_ir_without_knowledge(self):
        adapter = NativeMarkdownAdapter()
        locator = ("https" + "://" + "example" + ".invalid").encode()
        source = {"resource": "sources/note.md", "digest": "sha256:" + "1" * 64, "media_type": "text/markdown", "data": b"# Title\n\nEvidence [link](" + locator + b").\n"}
        output = adapter.parse(source)
        self.assertEqual(output.document_ir["schema_version"], "ao.lore.document-ir.v0.1")
        self.assertEqual([block["type"] for block in output.document_ir["blocks"]], ["heading", "paragraph", "link"])
        self.assertNotIn("concepts", output.document_ir)

    def test_registry_rejects_duplicate_parser_identity(self):
        registry = ParserRegistry()
        adapter = ScriptedParserAdapter(capability("same", 1), lambda src: source_ir(src, "same"))
        registry.register(adapter)
        with self.assertRaises(ParsingError):
            registry.register(adapter)

    def test_bounded_reader_rejects_escape_symlink_and_oversize(self):
        with tempfile.TemporaryDirectory() as root_name, tempfile.TemporaryDirectory() as outside_name:
            root = Path(root_name)
            inside = root / "note.txt"
            inside.write_text("safe", encoding="utf-8")
            outside = Path(outside_name) / "outside.txt"
            outside.write_text("private", encoding="utf-8")
            link = root / "link.txt"
            link.symlink_to(outside)
            self.assertEqual(read_bounded_source(inside, root, 10), b"safe")
            with self.assertRaises(ParsingError):
                read_bounded_source(link, root, 10)
            with self.assertRaises(ParsingError):
                read_bounded_source(inside, root, 2)


if __name__ == "__main__":
    unittest.main()
