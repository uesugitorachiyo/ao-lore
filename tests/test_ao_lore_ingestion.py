import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ao_lore.__main__ import main
from ao_lore._strict_io import ContractError
from ao_lore.benchmark import build_manifest, canonical_digest
from ao_lore.distillation import DistillationError, DistillationProposal
from ao_lore.docling_pdf import configuration_digest
from ao_lore.docx_ooxml import DOCX_PARSER_ID, DOCX_PARSER_VERSION, DocxLimits, docx_configuration_digest
from ao_lore.candidates import CandidateError, load_verified_candidate_if_present
from ao_lore.ingestion import (
    MAXIMUM_DOCUMENT_BLOCKS,
    MAXIMUM_PDF_BYTES,
    MAXIMUM_PDF_PAGES,
    DocxProvenanceOrigin,
    IngestionDependencies,
    IngestionError,
    default_ingestion_dependencies,
    default_docx_ingestion_dependencies,
    ingest_docx_document,
    ingest_single_document,
    ingest_verified_document,
    ingest_verified_docx,
)
from ao_lore.parsing import (
    ParseOutput,
    ParsingError,
    ParserRegistry,
    ProductionParser,
    ScriptedParserAdapter,
    base_capability,
)
from ao_lore.private_docx_domain import (
    DOCX_MIME,
    DOCX_PRIVATE_CORPUS_ID,
    DOCX_TRANSFORMATION_ID,
    validate_docx_expectation,
)


ROOT = Path(__file__).resolve().parents[1]
DOCX_FIXTURE = ROOT / "tests" / "fixtures" / "ao_lore" / "docx" / "minimal-paragraph.docx"
NOW1 = "2026-08-09T15:00:00Z"
NOW2 = "2026-08-09T16:00:00Z"


def ingest_readback(**overrides):
    candidate_id = "candidate-cli-document"
    report = {
        "schema_version": "ao.lore.single-document-ingest-readback.v0.1",
        "status": "created",
        "candidate_id": candidate_id,
        "candidate_digest": "sha256:" + "1" * 64,
        "provenance_digest": "sha256:" + "2" * 64,
        "source_digest": "sha256:" + "3" * 64,
        "parser_id": "docling",
        "parser_version": "2.118.1",
        "document_ir_digest": "sha256:" + "4" * 64,
        "parser_selection_report_digest": "sha256:" + "5" * 64,
        "parse_quality_report_digest": "sha256:" + "6" * 64,
        "distillation_trace_digest": "sha256:" + "7" * 64,
        "review_status": "unreviewed",
        "next_commands": [
            f"ao-lore candidate inspect --candidate-id {candidate_id}",
            f"ao-lore candidate review --candidate-id {candidate_id} "
            "--decision accept --reviewer <reviewer-id>",
        ],
        "canonical": False,
        "promotion_authority": False,
    }
    report.update(overrides)
    return report


def benchmark(**overrides):
    manifest = build_manifest(
        fixture_corpus_digest="sha256:" + "a" * 64,
        parser_id="docling",
        parser_version="2.118.1",
        parser_configuration_digest=configuration_digest(
            MAXIMUM_PDF_BYTES, MAXIMUM_PDF_PAGES, MAXIMUM_DOCUMENT_BLOCKS
        ),
        runtime="python-3.12",
        platform="linux-x86_64",
        document_ir_version="ao.lore.document-ir.v0.1",
        commands=[["python3", "-m", "ao_lore", "benchmark", "pdf"]],
        raw_metrics={
            "structural_fidelity": 0.9,
            "text_fidelity": 0.95,
            "source_location_fidelity": 0.9,
            "reliability": 1.0,
            "deterministic_repeatability": 1.0,
        },
        normalized_scores={
            "structural_fidelity": 0.9,
            "text_fidelity": 0.95,
            "source_location_fidelity": 0.9,
            "reliability": 1.0,
            "deterministic_repeatability": 1.0,
        },
        failures=[],
        exclusions=[],
    )
    if overrides:
        manifest.update(overrides)
        body = {key: value for key, value in manifest.items() if key != "result_digest"}
        manifest["result_digest"] = canonical_digest(body)
    return manifest


def docx_benchmark(**overrides):
    manifest = {
        "schema_version": "ao.lore.docx-benchmark-result.v0.1",
        "fixture_corpus_digest": "sha256:" + "c" * 64,
        "parser_id": DOCX_PARSER_ID,
        "parser_version": DOCX_PARSER_VERSION,
        "parser_configuration_digest": docx_configuration_digest(DocxLimits()),
        "document_ir_version": "ao.lore.document-ir.v0.1",
        "attempts_per_document": 2,
        "document_count": 100,
        "metrics": {
            "text_fidelity": 1.0,
            "structural_fidelity": 1.0,
            "source_location_fidelity": 1.0,
            "expected_outcome_accuracy": 1.0,
            "expected_rejection_count": 12,
            "accepted_document_count": 88,
        },
        "exclusions": [],
        "stable_failures": {"invalid_package": 1, "unsupported_active_content": 11},
        "repeatability": {"runs": 2, "identical": True, "score": 1.0},
        "unexpected_failures": [],
        "decision": "hold",
    }
    manifest.update(overrides)
    manifest["result_digest"] = canonical_digest(manifest)
    return manifest


def docx_expectation(*, derived_digest: str, source_digest: str = "sha256:" + "8" * 64):
    items = []
    for index in range(1, 101):
        items.append(
            {
                "item_id": f"docx-{index:04d}",
                "source_digest": source_digest if index == 1 else f"sha256:{index:064x}",
                "derived_digest": derived_digest if index == 1 else f"sha256:{index + 1000:064x}",
                "source_bytes": 1024 + index,
                "derived_bytes": 2048 + index,
                "transformation_id": DOCX_TRANSFORMATION_ID,
                "package_inventory_digest": f"sha256:{index + 2000:064x}",
                "normalized_text_digest": f"sha256:{index + 3000:064x}",
                "structural_event_digest": f"sha256:{index + 4000:064x}",
                "counts": {
                    "paragraphs": index,
                    "headings": index % 5,
                    "lists": index % 4,
                    "tables": index % 3,
                    "rows": index + 1,
                    "cells": index + 2,
                    "links": index % 2,
                    "footnotes": index % 3,
                    "endnotes": index % 4,
                    "drawings": index % 2,
                    "media": index % 3,
                },
                "expected_outcome": "reject" if index == 40 or index >= 90 else "accept",
                "expected_rejection": (
                    "invalid-package" if index == 40
                    else "active-content" if index >= 90
                    else None
                ),
                "page_count": None,
                "page_count_reason": "ooxml-pagination-unavailable",
            }
        )
    return {
        "schema_version": "ao.lore.private-docx-expectation.v0.1",
        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
        "items": items,
        "ocr_enabled": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


def accepted_parse(
    data,
    resource,
    *,
    malformed_ir=False,
    media_type="application/pdf",
    parser_id="docling",
    parser_version="2.118.1",
    parser_configuration_digest=None,
    benchmark_result_digest=None,
    selection_profile_id="select-pdf",
    quality_profile_id="quality-pdf",
):
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if parser_configuration_digest is None:
        parser_configuration_digest = configuration_digest(
            MAXIMUM_PDF_BYTES, MAXIMUM_PDF_PAGES, MAXIMUM_DOCUMENT_BLOCKS
        )
    if benchmark_result_digest is None:
        benchmark_result_digest = benchmark()["result_digest"]
    ir = {
        "schema_version": "ao.lore.document-ir.v0.1",
        "document_id": digest,
        "source": {
            "resource": resource,
            "digest": digest,
            "media_type": media_type,
        },
        "parser": {
            "parser_id": parser_id,
            "parser_version": parser_version,
            "configuration_digest": parser_configuration_digest,
        },
        "blocks": [
            {
                "id": "b1",
                "type": "heading",
                "text": "Local Evidence",
                "source_span": {"start": 0, "end": 14},
            },
            {
                "id": "b2",
                "type": "paragraph",
                "text": "Digest-bound content.",
                "source_span": {"start": 15, "end": 36},
            },
        ],
        "metadata": {},
    }
    if malformed_ir:
        ir = {"private_path": "/secret/raw.pdf"}
    quality = {
        "schema_version": "ao.lore.parse-quality-report.v0.1",
        "document_digest": digest,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "threshold_profile": quality_profile_id,
        "components": [{
            "name": "document_ir_valid",
            "applicable": True,
            "score": 1.0,
            "reason": "measured",
        }],
        "overall_quality_score": 1.0,
        "critical_failures": [],
        "decision": "accept",
    }
    return {
        "schema_version": "ao.lore.production-parse-result.v0.1",
        "document_digest": digest,
        "selection_report": {
            "schema_version": "ao.lore.parser-selection-report.v0.1",
            "document_digest": digest,
            "weight_profile": selection_profile_id,
            "eligible_parsers": [parser_id],
            "rejected_parsers": [],
            "candidates": [{
                "parser_id": parser_id,
                "normalized_components": {"reliability": 1.0},
                "selection_score": 1.0,
            }],
            "selected_parser": parser_id,
            "tie_break": {"applied": False, "reason": "highest score"},
            "benchmark_evidence": [benchmark_result_digest],
        },
        "attempts": [{
            "parser_id": parser_id,
            "selection_score": 1.0,
            "quality_report": quality,
        }],
        "selected_parser": parser_id,
        "quality_report": quality,
        "decision": "accept",
        "document_ir": ir,
    }


class FakeParser:
    def __init__(
        self, calls, *, decision="accept", malformed_ir=False, error=None,
        mutate=None, media_type="application/pdf", parser_id="docling",
        parser_version="2.118.1", parser_configuration_digest=None,
        benchmark_result_digest=None, selection_profile_id="select-pdf",
        quality_profile_id="quality-pdf",
    ):
        self.calls = calls
        self.decision = decision
        self.malformed_ir = malformed_ir
        self.error = error
        self.mutate = mutate
        self.media_type = media_type
        self.parser_id = parser_id
        self.parser_version = parser_version
        self.parser_configuration_digest = parser_configuration_digest
        self.benchmark_result_digest = benchmark_result_digest
        self.selection_profile_id = selection_profile_id
        self.quality_profile_id = quality_profile_id

    def parse_bytes(self, data, resource, request, selection_profile, quality_profile):
        self.calls.append(("parse", data, resource, request, selection_profile, quality_profile))
        if self.error:
            raise self.error
        result = accepted_parse(
            data,
            resource,
            malformed_ir=self.malformed_ir,
            media_type=self.media_type,
            parser_id=self.parser_id,
            parser_version=self.parser_version,
            parser_configuration_digest=self.parser_configuration_digest,
            benchmark_result_digest=self.benchmark_result_digest,
            selection_profile_id=self.selection_profile_id,
            quality_profile_id=self.quality_profile_id,
        )
        if self.decision != "accept":
            result["decision"] = self.decision
            result["quality_report"] = {**result["quality_report"], "decision": self.decision}
            result["document_ir"] = None
        if self.mutate is not None:
            self.mutate(result)
        return result


class FakeDistiller:
    def __init__(self, calls, error=None):
        self.calls = calls
        self.error = error

    def distill(self, document_ir, candidate_context):
        self.calls.append(("distill", document_ir, candidate_context))
        if self.error:
            raise self.error
        return DistillationProposal(
            concepts=[{
                "candidate_concept_id": "candidate-concept-1",
                "title": "Local Evidence",
                "source_block_ids": ["b1"],
                "status": "proposed",
                "candidate_claim_ids": ["candidate-claim-1"],
            }],
            claim_mappings=[{"claim_id": "candidate-claim-1", "block_ids": ["b2"]}],
            links=[],
            contradiction_warnings=[],
            trace={"adapter": "fake", "private_reasoning_persisted": False},
        )


class SingleDocumentIngestionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "sources").mkdir(exist_ok=True)
        (ROOT / "working" / "candidates").mkdir(parents=True, exist_ok=True)
        self.source_temp = tempfile.TemporaryDirectory(dir=ROOT / "sources")
        self.candidate_temp = tempfile.TemporaryDirectory(
            dir=ROOT / "working" / "candidates"
        )
        self.source_root = Path(self.source_temp.name)
        self.candidate_root = Path(self.candidate_temp.name)
        self.source = self.source_root / "public.pdf"
        self.data = b"%PDF-1.7\nlocal public fixture"
        self.source.write_bytes(self.data)
        self.calls = []

    def tearDown(self):
        self.candidate_temp.cleanup()
        self.source_temp.cleanup()

    def dependencies(self, **parser_options):
        def now():
            self.calls.append(("now",))
            return NOW1

        return IngestionDependencies(
            parser=FakeParser(self.calls, **parser_options),
            distiller=FakeDistiller(self.calls),
            now=now,
        )

    def ingest(self, *, dependencies=None, manifest=None, context=None):
        return ingest_single_document(
            self.source,
            benchmark_manifest=benchmark() if manifest is None else manifest,
            selection_profile={"profile_id": "select-pdf"},
            quality_profile={"profile_id": "quality-pdf"},
            candidate_context=context,
            dependencies=dependencies or self.dependencies(),
            candidate_root=self.candidate_root,
        )

    def assert_candidate_tree_empty(self):
        self.assertEqual([], list(self.candidate_root.iterdir()))

    def test_dependency_injected_happy_path_binds_exact_digests_and_order(self):
        result = self.ingest(context={"known": "digest-only"})
        self.assertEqual([call[0] for call in self.calls], ["parse", "distill", "now"])
        parse_call = self.calls[0]
        source_hex = hashlib.sha256(self.data).hexdigest()
        self.assertEqual(parse_call[2], "source-" + source_hex[:16] + ".pdf")
        self.assertEqual(parse_call[3], {
            "media_type": "application/pdf",
            "extension": ".pdf",
            "required_structural_elements": [],
            "canonical_ir_version": "ao.lore.document-ir.v0.1",
            "network_allowed": False,
            "license_allowlist": ["Apache-2.0"],
            "sandbox_required": True,
        })
        self.assertEqual(self.calls[1][2], {"known": "digest-only"})
        candidate_dir = self.candidate_root / result["candidate_id"]
        provenance = json.loads((candidate_dir / "provenance.json").read_text())
        candidate = json.loads((candidate_dir / "candidate.json").read_text())
        self.assertEqual(result["candidate_digest"], canonical_digest(candidate))
        self.assertEqual(result["provenance_digest"], canonical_digest(provenance))
        self.assertEqual(result["source_digest"], "sha256:" + source_hex)
        self.assertEqual(result["document_ir_digest"], candidate["document_ir_digest"])

    def test_verified_document_consumes_detached_bytes_without_path_reopen(self):
        digest = "sha256:" + hashlib.sha256(self.data).hexdigest()
        self.source.write_bytes(b"%PDF-1.7\nforeign replacement")
        result = ingest_verified_document(
            self.data,
            source_digest=digest,
            resource="source-" + digest.removeprefix("sha256:")[:16] + ".pdf",
            benchmark_manifest=benchmark(),
            selection_profile={"profile_id": "select-pdf"},
            quality_profile={"profile_id": "quality-pdf"},
            dependencies=self.dependencies(),
            candidate_root=self.candidate_root,
        )
        self.assertEqual(digest, result["source_digest"])
        self.assertEqual(self.data, self.calls[0][1])

    def test_omitted_context_supplies_fresh_versioned_empty_comparison_context(self):
        expected = {
            "schema_version": "ao.lore.candidate-comparison-context.v0.1",
            "candidates": [],
        }

        self.ingest()
        first_context = self.calls[1][2]
        self.assertEqual(first_context, expected)
        first_context["candidates"].append({"candidate_id": "mutated-by-adapter"})

        self.calls.clear()
        self.ingest()
        second_context = self.calls[1][2]
        self.assertEqual(second_context, expected)
        self.assertIsNot(first_context, second_context)
        self.assertIsNot(first_context["candidates"], second_context["candidates"])

    def test_non_strict_candidate_context_is_rejected_before_distillation(self):
        with self.assertRaisesRegex(IngestionError, "document distillation failed"):
            self.ingest(context={"score": float("nan")})

        self.assertEqual([call[0] for call in self.calls], ["parse"])
        self.assert_candidate_tree_empty()

    def test_real_production_parser_seam_executes_qualified_adapter_and_persists(self):
        manifest = benchmark()
        capability = base_capability(
            "docling",
            "2.118.1",
            ["application/pdf"],
            [".pdf"],
            ["headings", "links", "tables"],
        )
        capability["benchmark_result_refs"] = [manifest["result_digest"]]
        adapter_calls = []

        def parse(source):
            adapter_calls.append(source["resource"])
            return ParseOutput(
                document_ir={
                    "schema_version": "ao.lore.document-ir.v0.1",
                    "document_id": source["digest"],
                    "source": {
                        "resource": source["resource"],
                        "digest": source["digest"],
                        "media_type": source["media_type"],
                    },
                    "parser": {
                        "parser_id": "docling",
                        "parser_version": "2.118.1",
                        "configuration_digest": manifest[
                            "parser_configuration_digest"
                        ],
                    },
                    "blocks": [
                        {
                            "id": "b1",
                            "type": "heading",
                            "text": "Qualified evidence",
                            "source_span": {"start": 0, "end": 18},
                        },
                        {
                            "id": "b2",
                            "type": "paragraph",
                            "text": "Local parser seam.",
                            "source_span": {"start": 19, "end": 37},
                        },
                    ],
                    "metadata": {},
                },
                quality_components={
                    "text_coverage": 1.0,
                    "structural_completeness": 1.0,
                    "source_span_coverage": 1.0,
                    "document_ir_valid": 1.0,
                },
            )

        registry = ParserRegistry()
        registry.register(ScriptedParserAdapter(capability, parse))
        dependencies = IngestionDependencies(
            parser=ProductionParser(registry),
            distiller=FakeDistiller(self.calls),
            now=lambda: NOW1,
        )
        selection = {
            "profile_id": "select-pdf",
            "weights": {"structural_fidelity": 1.0},
            "penalty_weights": {},
            "normalization_bounds": {},
            "missing_optional_policy": "ineligible",
            "tie_break_order": ["parser_id"],
        }
        quality = {
            "profile_id": "quality-pdf",
            "component_weights": {
                "text_coverage": 0.4,
                "structural_completeness": 0.3,
                "source_span_coverage": 0.2,
                "document_ir_valid": 0.1,
            },
            "accept_threshold": 0.8,
            "fallback_threshold": 0.7,
            "maximum_fallbacks": 0,
            "critical_failures": [
                "invalid_ir", "source_digest_mismatch", "parser_crash",
                "no_readable_content", "lost_provenance",
            ],
            "intermediate_decision": "quarantine",
            "exhausted_decision": "reject",
        }
        result = ingest_single_document(
            self.source,
            benchmark_manifest=manifest,
            selection_profile=selection,
            quality_profile=quality,
            dependencies=dependencies,
            candidate_root=self.candidate_root,
        )
        expected_resource = (
            "source-" + hashlib.sha256(self.data).hexdigest()[:16] + ".pdf"
        )
        self.assertEqual(adapter_calls, [expected_resource])
        self.assertEqual(result["status"], "created")
        self.assertEqual(len(list(self.candidate_root.iterdir())), 1)

    def test_automatic_persistence_reopens_unreviewed_without_authority(self):
        result = self.ingest()
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["review_status"], "unreviewed")
        self.assertFalse(result["canonical"])
        self.assertFalse(result["promotion_authority"])
        self.assertEqual(len(result["next_commands"]), 2)
        self.assertTrue(all(result["candidate_id"] in command for command in result["next_commands"]))

    def test_readback_is_digest_only_and_never_contains_source_path_or_content(self):
        result = self.ingest()
        encoded = json.dumps(result, sort_keys=True)
        self.assertNotIn(str(self.source), encoded)
        self.assertNotIn("local public fixture", encoded)
        self.assertNotIn("private", encoded)
        self.assertEqual(set(result), {
            "schema_version", "status", "candidate_id", "candidate_digest",
            "provenance_digest", "source_digest", "parser_id", "parser_version",
            "document_ir_digest", "parser_selection_report_digest",
            "parse_quality_report_digest", "distillation_trace_digest",
            "review_status", "next_commands", "canonical", "promotion_authority",
        })

    def test_identical_second_ingest_retains_original_timestamp(self):
        first = self.ingest()
        first_provenance = json.loads(
            (self.candidate_root / first["candidate_id"] / "provenance.json").read_text()
        )
        self.calls.clear()
        dependencies = self.dependencies()
        object.__setattr__(dependencies, "now", lambda: NOW2)
        second = self.ingest(dependencies=dependencies)
        second_provenance = json.loads(
            (self.candidate_root / first["candidate_id"] / "provenance.json").read_text()
        )
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(first_provenance, second_provenance)
        self.assertEqual(second_provenance["created_at"], NOW1)

    def test_source_escape_symlink_non_regular_wrong_extension_and_media_fail_closed(self):
        outside = Path(self.source_temp.name).parent.parent / "outside-private.pdf"
        outside.write_bytes(self.data)
        link = self.source_root / "link.pdf"
        link.symlink_to(self.source)
        directory = self.source_root / "directory.pdf"
        directory.mkdir()
        wrong = self.source_root / "wrong.txt"
        wrong.write_bytes(self.data)
        media = self.source_root / "not-pdf.pdf"
        media.write_bytes(b"plain text")
        try:
            for path in (outside, link, directory, wrong, media):
                with self.subTest(path=path.name):
                    with self.assertRaises(IngestionError):
                        ingest_single_document(
                            path,
                            benchmark_manifest=benchmark(),
                            selection_profile={},
                            quality_profile={},
                            dependencies=self.dependencies(),
                            candidate_root=self.candidate_root,
                        )
                    self.assert_candidate_tree_empty()
        finally:
            outside.unlink()

    def test_oversized_source_fails_before_parser_or_persistence(self):
        with self.source.open("wb") as handle:
            handle.truncate(MAXIMUM_PDF_BYTES + 1)
        with self.assertRaisesRegex(IngestionError, "source input is invalid"):
            self.ingest()
        self.assertEqual(self.calls, [])
        self.assert_candidate_tree_empty()

    def test_expected_source_digest_is_verified_before_parser_construction(self):
        expected = "sha256:" + "f" * 64
        with self.assertRaisesRegex(IngestionError, "source digest does not match"):
            ingest_single_document(
                self.source,
                benchmark_manifest=benchmark(),
                selection_profile={"profile_id": "select-pdf"},
                quality_profile={"profile_id": "quality-pdf"},
                expected_source_digest=expected,
                dependencies=self.dependencies(),
                candidate_root=self.candidate_root,
            )
        self.assertEqual([], self.calls)
        self.assert_candidate_tree_empty()

    def test_invalid_benchmark_version_configuration_and_failures_fail_first(self):
        variants = [
            benchmark(parser_version="2.118.0"),
            benchmark(parser_configuration_digest="sha256:" + "0" * 64),
            benchmark(failures=["private fixture failed"]),
            {},
            [],
        ]
        for manifest in variants:
            with self.subTest(
                version=manifest.get("parser_version") if isinstance(manifest, dict) else None
            ):
                with self.assertRaisesRegex(IngestionError, "benchmark evidence is invalid"):
                    self.ingest(manifest=manifest)
                self.assertEqual(self.calls, [])
                self.assert_candidate_tree_empty()

    def test_rejected_parse_malformed_ir_and_distillation_failure_do_not_persist(self):
        cases = [
            self.dependencies(decision="reject"),
            self.dependencies(malformed_ir=True),
            IngestionDependencies(
                parser=FakeParser(self.calls),
                distiller=FakeDistiller(self.calls, error=RuntimeError("/secret/raw.pdf")),
                now=lambda: NOW1,
            ),
        ]
        for dependencies in cases:
            with self.subTest(dependencies=type(dependencies.distiller).__name__):
                with self.assertRaises(IngestionError):
                    self.ingest(dependencies=dependencies)
                self.assert_candidate_tree_empty()

    def test_forged_parser_result_bindings_fail_before_persistence(self):
        bad_digest = "sha256:" + "f" * 64
        bad_config = "sha256:" + "e" * 64
        mutations = {
            "result digest": lambda value: value.update(document_digest=bad_digest),
            "quality digest": lambda value: value["quality_report"].update(document_digest=bad_digest),
            "selection digest": lambda value: value["selection_report"].update(document_digest=bad_digest),
            "source digest": lambda value: value["document_ir"]["source"].update(digest=bad_digest),
            "document identity": lambda value: value["document_ir"].update(document_id=bad_digest),
            "source resource": lambda value: value["document_ir"]["source"].update(resource="private.pdf"),
            "source media": lambda value: value["document_ir"]["source"].update(media_type="text/plain"),
            "parser id": lambda value: value["document_ir"]["parser"].update(parser_id="other"),
            "parser version": lambda value: value["document_ir"]["parser"].update(parser_version="2.118.0"),
            "parser config": lambda value: value["document_ir"]["parser"].update(configuration_digest=bad_config),
            "critical failure": lambda value: value["quality_report"].update(critical_failures=["lost_provenance"]),
            "quality decision": lambda value: value["quality_report"].update(decision="reject"),
            "selection parser": lambda value: value["selection_report"].update(selected_parser="other"),
            "result parser": lambda value: value.update(selected_parser="other"),
            "benchmark identity": lambda value: value["selection_report"].update(benchmark_evidence=[bad_digest]),
        }
        for label, mutation in mutations.items():
            with self.subTest(label=label):
                with self.assertRaises(IngestionError):
                    self.ingest(dependencies=self.dependencies(mutate=mutation))
                self.assert_candidate_tree_empty()

    def test_persistence_conflict_fails_closed(self):
        result = self.ingest()
        provenance_path = self.candidate_root / result["candidate_id"] / "provenance.json"
        provenance = json.loads(provenance_path.read_text())
        provenance["source_digest"] = "sha256:" + "f" * 64
        provenance_path.write_text(json.dumps(provenance) + "\n")
        with self.assertRaisesRegex(IngestionError, "candidate persistence failed"):
            self.ingest()

    def test_exceptions_redact_private_paths_and_content(self):
        private = "/private/customer/raw.pdf contains secret words"
        dependencies = self.dependencies(error=RuntimeError(private))
        with self.assertRaises(IngestionError) as raised:
            self.ingest(dependencies=dependencies)
        self.assertNotIn(private, str(raised.exception))
        self.assertNotIn(str(self.source), str(raised.exception))
        self.assert_candidate_tree_empty()

    def test_optional_load_returns_none_only_for_exact_absence(self):
        candidate_id = "candidate-0123456789abcdef"
        self.assertIsNone(load_verified_candidate_if_present(
            candidate_id, candidate_root=self.candidate_root
        ))
        target = self.candidate_root / candidate_id
        target.symlink_to(self.source_root, target_is_directory=True)
        try:
            with self.assertRaises(CandidateError):
                load_verified_candidate_if_present(
                    candidate_id, candidate_root=self.candidate_root
                )
        finally:
            target.unlink()
        target.write_text("not a directory")
        try:
            with self.assertRaises(CandidateError):
                load_verified_candidate_if_present(
                    candidate_id, candidate_root=self.candidate_root
                )
        finally:
            target.unlink()
        target.mkdir()
        try:
            with self.assertRaises(CandidateError):
                load_verified_candidate_if_present(
                    candidate_id, candidate_root=self.candidate_root
                )
        finally:
            target.rmdir()

    def test_default_factory_defers_docling_initialization_until_called(self):
        with patch(
            "ao_lore.docling_pdf.DoclingPdfAdapter",
            side_effect=ImportError("private runtime path"),
        ) as constructor:
            self.assertFalse(constructor.called)
            with self.assertRaisesRegex(
                IngestionError, "qualified PDF parser is unavailable"
            ) as raised:
                default_ingestion_dependencies(benchmark())
            constructor.assert_called_once()
            self.assertNotIn("private runtime path", str(raised.exception))

    def test_default_factory_rejects_docling_version_and_configuration_drift(self):
        variants = (
            benchmark(parser_version="2.118.0"),
            benchmark(parser_configuration_digest="sha256:" + "0" * 64),
        )
        for manifest in variants:
            with self.subTest(version=manifest["parser_version"]), patch(
                "ao_lore.docling_pdf.DoclingPdfAdapter"
            ) as constructor:
                with self.assertRaisesRegex(
                    IngestionError, "benchmark evidence is invalid"
                ):
                    default_ingestion_dependencies(manifest)
                constructor.assert_not_called()

    def test_default_factory_redacts_ordinary_backend_constructor_failure(self):
        private = "runtime crash at /private/model/cache"
        with patch(
            "ao_lore.docling_pdf.DoclingPdfAdapter",
            side_effect=RuntimeError(private),
        ):
            with self.assertRaises(IngestionError) as raised:
                default_ingestion_dependencies(benchmark())
        self.assertNotIn(private, str(raised.exception))

    def test_identical_concurrent_ingests_create_once_and_reconcile_collision(self):
        import ao_lore.ingestion as ingestion_module

        barrier = threading.Barrier(2)
        original = ingestion_module.load_verified_candidate_if_present
        initial_checks = 0
        check_lock = threading.Lock()

        def synchronized_load(candidate_id, *, candidate_root=None):
            nonlocal initial_checks
            result = original(candidate_id, candidate_root=candidate_root)
            if result is None:
                with check_lock:
                    initial_checks += 1
                barrier.wait(timeout=5)
            return result

        def run_ingest(index):
            calls = []
            dependencies = IngestionDependencies(
                parser=FakeParser(calls),
                distiller=FakeDistiller(calls),
                now=lambda: NOW1,
            )
            return self.ingest(dependencies=dependencies)

        with patch.object(
            ingestion_module,
            "load_verified_candidate_if_present",
            side_effect=synchronized_load,
        ), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run_ingest, range(2)))
        self.assertEqual(initial_checks, 2)
        self.assertEqual(sorted(item["status"] for item in results), ["created", "unchanged"])
        self.assertEqual(len(list(self.candidate_root.iterdir())), 1)

    def test_ancestor_swap_cannot_redirect_descriptor_anchored_read(self):
        import ao_lore.ingestion as ingestion_module

        nested = self.source_root / "nested"
        nested.mkdir()
        source = nested / "public.pdf"
        source.write_bytes(self.data)
        outside = Path(tempfile.mkdtemp(dir=ROOT / "working"))
        (outside / "public.pdf").write_bytes(b"%PDF-1.7\noutside private bytes")
        displaced = self.source_root / "nested-displaced"
        real_open = os.open
        swapped = False

        def swapping_open(path, flags, *args, **kwargs):
            nonlocal swapped
            trigger = path == "nested" or Path(path) == source
            if trigger and not swapped:
                swapped = True
                nested.rename(displaced)
                nested.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        try:
            with patch.object(ingestion_module.os, "open", side_effect=swapping_open):
                with self.assertRaises(IngestionError):
                    ingest_single_document(
                        source,
                        benchmark_manifest=benchmark(),
                        selection_profile={"profile_id": "select-pdf"},
                        quality_profile={"profile_id": "quality-pdf"},
                        dependencies=self.dependencies(),
                        candidate_root=self.candidate_root,
                    )
            self.assertTrue(swapped)
            self.assert_candidate_tree_empty()
        finally:
            if nested.is_symlink():
                nested.unlink()
            if displaced.exists():
                displaced.rename(nested)
            shutil.rmtree(outside)


class DocxSingleDocumentIngestionTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "sources").mkdir(exist_ok=True)
        (ROOT / "working" / "candidates").mkdir(parents=True, exist_ok=True)
        self.source_temp = tempfile.TemporaryDirectory(dir=ROOT / "sources")
        self.candidate_temp = tempfile.TemporaryDirectory(
            dir=ROOT / "working" / "candidates"
        )
        self.source_root = Path(self.source_temp.name)
        self.candidate_root = Path(self.candidate_temp.name)
        self.source = self.source_root / "public.docx"
        self.data = DOCX_FIXTURE.read_bytes()
        self.source.write_bytes(self.data)
        self.calls = []
        self.expected_corpus_digest = "sha256:" + "c" * 64
        self.origin = DocxProvenanceOrigin(
            corpus_id=DOCX_PRIVATE_CORPUS_ID,
            original_digest="sha256:" + "8" * 64,
            derived_digest="sha256:" + hashlib.sha256(self.data).hexdigest(),
            transformation_id=DOCX_TRANSFORMATION_ID,
            expectation_digest=self.expected_corpus_digest,
        )

    def tearDown(self):
        self.candidate_temp.cleanup()
        self.source_temp.cleanup()

    def dependencies(self, **parser_options):
        def now():
            self.calls.append(("now",))
            return NOW1

        return IngestionDependencies(
            parser=FakeParser(
                self.calls,
                media_type=DOCX_MIME,
                parser_id=DOCX_PARSER_ID,
                parser_version=DOCX_PARSER_VERSION,
                parser_configuration_digest=docx_configuration_digest(DocxLimits()),
                benchmark_result_digest=docx_benchmark()["result_digest"],
                selection_profile_id="docx-native-v1",
                quality_profile_id="docx-quality-v1",
                **parser_options,
            ),
            distiller=FakeDistiller(self.calls),
            now=now,
        )

    def ingest(self, *, dependencies=None, manifest=None, origin=None):
        return ingest_docx_document(
            self.source,
            benchmark_manifest=docx_benchmark() if manifest is None else manifest,
            expected_corpus_digest=self.expected_corpus_digest,
            selection_profile={"profile_id": "docx-native-v1"},
            quality_profile={"profile_id": "docx-quality-v1"},
            origin=self.origin if origin is None else origin,
            dependencies=dependencies or self.dependencies(),
            candidate_root=self.candidate_root,
        )

    def test_verified_docx_persists_v0_2_provenance_with_detached_origin(self):
        digest = "sha256:" + hashlib.sha256(self.data).hexdigest()
        result = ingest_verified_docx(
            self.data,
            source_digest=digest,
            resource="source-" + digest.removeprefix("sha256:")[:16] + ".docx",
            benchmark_manifest=docx_benchmark(),
            expected_corpus_digest=self.expected_corpus_digest,
            selection_profile={"profile_id": "docx-native-v1"},
            quality_profile={"profile_id": "docx-quality-v1"},
            origin=self.origin,
            dependencies=self.dependencies(),
            candidate_root=self.candidate_root,
        )
        candidate_dir = self.candidate_root / result["candidate_id"]
        provenance = json.loads((candidate_dir / "provenance.json").read_text())
        self.assertEqual("ao.lore.candidate-provenance.v0.2", provenance["schema_version"])
        self.assertEqual("docx", provenance["source_origin"]["format_id"])
        self.assertEqual(self.origin.corpus_id, provenance["source_origin"]["corpus_id"])
        self.assertEqual(digest, provenance["source_origin"]["derived_digest"])
        self.assertEqual(digest, result["source_digest"])
        self.assertEqual(DOCX_PARSER_ID, result["parser_id"])
        self.assertEqual(DOCX_PARSER_VERSION, result["parser_version"])
        self.assertEqual(self.data, self.calls[0][1])

    def test_path_ingest_reads_once_and_rejects_origin_forgery(self):
        result = self.ingest()
        self.assertEqual(result["status"], "created")
        self.assertEqual([call[0] for call in self.calls], ["parse", "distill", "now"])

        forged = DocxProvenanceOrigin(
            corpus_id=DOCX_PRIVATE_CORPUS_ID,
            original_digest="sha256:" + "8" * 64,
            derived_digest="sha256:" + "f" * 64,
            transformation_id=DOCX_TRANSFORMATION_ID,
            expectation_digest="sha256:" + "9" * 64,
        )
        with self.assertRaisesRegex(IngestionError, "verified document binding is invalid"):
            ingest_verified_docx(
                self.data,
                source_digest="sha256:" + hashlib.sha256(self.data).hexdigest(),
                resource="source-" + hashlib.sha256(self.data).hexdigest()[:16] + ".docx",
                benchmark_manifest=docx_benchmark(),
                expected_corpus_digest=self.expected_corpus_digest,
                selection_profile={"profile_id": "docx-native-v1"},
                quality_profile={"profile_id": "docx-quality-v1"},
                origin=forged,
                dependencies=self.dependencies(),
                candidate_root=self.candidate_root,
            )

    def test_origin_and_expected_corpus_digest_are_required_for_docx(self):
        with self.assertRaisesRegex(IngestionError, "verified document binding is invalid"):
            ingest_verified_docx(
                self.data,
                source_digest="sha256:" + hashlib.sha256(self.data).hexdigest(),
                resource="source-" + hashlib.sha256(self.data).hexdigest()[:16] + ".docx",
                benchmark_manifest=docx_benchmark(),
                expected_corpus_digest=self.expected_corpus_digest,
                selection_profile={"profile_id": "docx-native-v1"},
                quality_profile={"profile_id": "docx-quality-v1"},
                origin=None,
                dependencies=self.dependencies(),
                candidate_root=self.candidate_root,
            )
        with self.assertRaisesRegex(IngestionError, "verified document binding is invalid"):
            ingest_docx_document(
                self.source,
                benchmark_manifest=docx_benchmark(),
                expected_corpus_digest=None,  # type: ignore[arg-type]
                selection_profile={"profile_id": "docx-native-v1"},
                quality_profile={"profile_id": "docx-quality-v1"},
                origin=self.origin,
                dependencies=self.dependencies(),
                candidate_root=self.candidate_root,
            )

    def test_non_docx_bytes_fail_before_parser_or_persistence(self):
        self.source.write_bytes(b"plain text")
        digest = "sha256:" + hashlib.sha256(b"plain text").hexdigest()
        with self.assertRaisesRegex(IngestionError, "source media does not match DOCX"):
            ingest_docx_document(
                self.source,
                benchmark_manifest=docx_benchmark(),
                expected_corpus_digest=self.expected_corpus_digest,
                selection_profile={"profile_id": "docx-native-v1"},
                quality_profile={"profile_id": "docx-quality-v1"},
                origin=DocxProvenanceOrigin(
                    corpus_id=DOCX_PRIVATE_CORPUS_ID,
                    original_digest="sha256:" + "8" * 64,
                    derived_digest=digest,
                    transformation_id=DOCX_TRANSFORMATION_ID,
                    expectation_digest=self.expected_corpus_digest,
                ),
                dependencies=self.dependencies(),
                candidate_root=self.candidate_root,
            )
        self.assertEqual([], self.calls)
        self.assertEqual([], list(self.candidate_root.iterdir()))

    def test_verified_docx_bytes_fail_before_dependency_parse_or_persistence(self):
        body = b"plain text"
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        origin = DocxProvenanceOrigin(
            corpus_id=DOCX_PRIVATE_CORPUS_ID,
            original_digest="sha256:" + "8" * 64,
            derived_digest=digest,
            transformation_id=DOCX_TRANSFORMATION_ID,
            expectation_digest=self.expected_corpus_digest,
        )
        with self.assertRaisesRegex(IngestionError, "source media does not match DOCX"):
            ingest_verified_docx(
                body,
                source_digest=digest,
                resource="source-" + digest.removeprefix("sha256:")[:16] + ".docx",
                benchmark_manifest=docx_benchmark(),
                expected_corpus_digest=self.expected_corpus_digest,
                selection_profile={"profile_id": "docx-native-v1"},
                quality_profile={"profile_id": "docx-quality-v1"},
                origin=origin,
                dependencies=self.dependencies(),
                candidate_root=self.candidate_root,
            )
        self.assertEqual([], self.calls)
        self.assertEqual([], list(self.candidate_root.iterdir()))

    def test_docx_default_factory_is_qualification_bound_and_redacted(self):
        with patch(
            "ao_lore.docx_ooxml.NativeDocxOoxmlAdapter",
            side_effect=ImportError("/private/docx/runtime"),
        ) as constructor:
            with self.assertRaisesRegex(
                IngestionError, "qualified DOCX parser is unavailable"
            ) as raised:
                default_docx_ingestion_dependencies(
                    docx_benchmark(),
                    expected_corpus_digest=self.expected_corpus_digest,
                )
            constructor.assert_called_once()
            self.assertNotIn("/private/docx/runtime", str(raised.exception))

        with self.assertRaisesRegex(IngestionError, "benchmark evidence is invalid"):
            default_docx_ingestion_dependencies(
                docx_benchmark(decision="investigate"),
                expected_corpus_digest=self.expected_corpus_digest,
            )

        with self.assertRaisesRegex(IngestionError, "benchmark evidence is invalid"):
            default_docx_ingestion_dependencies(
                docx_benchmark(fixture_corpus_digest="sha256:" + "d" * 64),
                expected_corpus_digest=self.expected_corpus_digest,
            )


class SingleDocumentIngestCliTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "working").mkdir(exist_ok=True)
        self.runtime_temp = tempfile.TemporaryDirectory(dir=ROOT / "working")
        self.runtime_root = Path(self.runtime_temp.name)
        self.benchmark_path = (
            self.runtime_root / "benchmarks" / "docling-2.118.1.json"
        )
        self.benchmark_path.parent.mkdir()
        self.manifest = benchmark()
        self.benchmark_path.write_text(
            json.dumps(self.manifest) + "\n", encoding="utf-8"
        )
        self.context_path = self.runtime_root / "candidate-context.json"
        self.context = {"known_candidate_digest": "sha256:" + "8" * 64}
        self.context_path.write_text(
            json.dumps(self.context) + "\n", encoding="utf-8"
        )
        self.source_argument = "sources/private-customer-name.pdf"

    def tearDown(self):
        self.runtime_temp.cleanup()

    def invoke(self, argv):
        stdout = StringIO()
        stderr = StringIO()
        with patch.dict(
            os.environ, {"AO_LORE_HOME": str(self.runtime_root)}, clear=False
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                status = main(argv)
            except SystemExit as exc:
                status = int(exc.code)
        return status, stdout.getvalue(), stderr.getvalue()

    @patch("ao_lore.__main__.ingest_single_document", create=True)
    def test_ingest_parser_accepts_required_and_optional_arguments(self, ingest):
        ingest.return_value = ingest_readback()

        basic = self.invoke(["ingest", "--source", self.source_argument])
        detailed = self.invoke([
            "ingest", "--source", self.source_argument,
            "--candidate-context", str(self.context_path), "--json",
        ])

        self.assertEqual(basic[0], 0)
        self.assertEqual(detailed[0], 0)
        self.assertEqual(ingest.call_count, 2)

    @patch("ao_lore.__main__.ingest_single_document", create=True)
    def test_ingest_loads_strict_inputs_and_forwards_exact_profiles(self, ingest):
        ingest.return_value = ingest_readback()

        status, output, error = self.invoke([
            "ingest", "--source", self.source_argument,
            "--candidate-context", str(self.context_path), "--json",
        ])

        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output), ingest.return_value)
        ingest.assert_called_once_with(
            self.source_argument,
            benchmark_manifest=self.manifest,
            selection_profile={
                "profile_id": "pdf-docling-v1",
                "weights": {
                    "structural_fidelity": 0.4,
                    "text_fidelity": 0.4,
                    "source_location_fidelity": 0.2,
                },
                "penalty_weights": {},
                "normalization_bounds": {},
                "missing_optional_policy": "ineligible",
                "tie_break_order": [
                    "required_feature_coverage",
                    "structural_fidelity",
                    "parser_id",
                ],
                "fixture_corpus_digest": self.manifest[
                    "fixture_corpus_digest"
                ],
            },
            quality_profile={
                "profile_id": "pdf-quality-v1",
                "component_weights": {
                    "text_coverage": 0.4,
                    "structural_completeness": 0.3,
                    "source_span_coverage": 0.2,
                    "document_ir_valid": 0.1,
                },
                "accept_threshold": 0.8,
                "fallback_threshold": 0.7,
                "maximum_fallbacks": 0,
                "critical_failures": [
                    "invalid_ir",
                    "source_digest_mismatch",
                    "parser_crash",
                    "no_readable_content",
                    "lost_provenance",
                ],
                "calibration_result_digest": self.manifest["result_digest"],
                "intermediate_decision": "quarantine",
                "exhausted_decision": "reject",
            },
            candidate_context=self.context,
        )

    @patch("ao_lore.__main__.ingest_single_document", create=True)
    def test_json_output_is_the_exact_sorted_ingest_readback(self, ingest):
        ingest.return_value = ingest_readback()

        status, output, error = self.invoke([
            "ingest", "--source", self.source_argument, "--json"
        ])

        self.assertEqual((status, error), (0, ""))
        self.assertEqual(output, json.dumps(ingest.return_value, sort_keys=True) + "\n")
        self.assertNotIn(self.source_argument, output)

    @patch("ao_lore.__main__.ingest_single_document", create=True)
    def test_human_output_is_safe_projection_of_bound_commands(self, ingest):
        ingest.return_value = ingest_readback()

        status, output, error = self.invoke([
            "ingest", "--source", self.source_argument
        ])

        self.assertEqual((status, error), (0, ""))
        self.assertEqual(
            output,
            "STATUS\tcreated\n"
            "CANDIDATE_ID\tcandidate-cli-document\n"
            "REVIEW_STATUS\tunreviewed\n"
            "NEXT\tao-lore candidate inspect --candidate-id "
            "candidate-cli-document\n"
            "NEXT\tao-lore candidate review --candidate-id "
            "candidate-cli-document --decision accept --reviewer "
            "<reviewer-id>\n",
        )
        self.assertNotIn(self.source_argument, output)
        for private_digest in (
            ingest.return_value["source_digest"],
            ingest.return_value["candidate_digest"],
            ingest.return_value["provenance_digest"],
        ):
            self.assertNotIn(private_digest, output)

    @patch("ao_lore.__main__.ingest_single_document", create=True)
    def test_duplicate_or_nonobject_context_is_rejected_before_ingestion(self, ingest):
        invalid_bodies = (
            '{"key": 1, "key": 2}\n',
            '["not", "an", "object"]\n',
        )
        for body in invalid_bodies:
            with self.subTest(body=body):
                self.context_path.write_text(body, encoding="utf-8")
                status, output, error = self.invoke([
                    "ingest", "--source", self.source_argument,
                    "--candidate-context", str(self.context_path), "--json",
                ])
                self.assertEqual(status, 2)
                self.assertEqual(output, "")
                self.assertEqual(error, "ao-lore: document ingest rejected\n")
        ingest.assert_not_called()

    @patch("ao_lore.__main__.ingest_single_document", create=True)
    def test_missing_escaped_or_nonregular_context_is_redacted(self, ingest):
        source_root = ROOT / "sources"
        source_root.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=source_root, suffix="-context.json", delete=False
        ) as handle:
            handle.write(b"{}\n")
            outside = Path(handle.name)
        directory = self.runtime_root / "context-directory"
        directory.mkdir()
        missing = self.runtime_root / "missing-context.json"
        try:
            for context in (outside, directory, missing):
                with self.subTest(context=context.name):
                    status, output, error = self.invoke([
                        "ingest", "--source", self.source_argument,
                        "--candidate-context", str(context), "--json",
                    ])
                    self.assertEqual((status, output), (2, ""))
                    self.assertEqual(
                        error, "ao-lore: document ingest rejected\n"
                    )
                    self.assertNotIn(str(context), error)
        finally:
            outside.unlink()
        ingest.assert_not_called()

    def test_ingest_failures_share_one_redacted_boundary(self):
        private = "/private/customer/raw.pdf contains secret words"
        failures = (
            IngestionError(private),
            ParsingError(private),
            DistillationError(private),
            CandidateError(private),
            ContractError(private),
            OSError(private),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch(
                "ao_lore.__main__.ingest_single_document",
                create=True,
                side_effect=failure,
            ):
                status, output, error = self.invoke([
                    "ingest", "--source", self.source_argument, "--json"
                ])
                self.assertEqual(status, 2)
                self.assertEqual(output, "")
                self.assertEqual(error, "ao-lore: document ingest rejected\n")
                self.assertNotIn(private, error)
                self.assertNotIn(self.source_argument, error)


class DocxSingleDocumentIngestCliTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "working").mkdir(exist_ok=True)
        (ROOT / "sources").mkdir(exist_ok=True)
        self.runtime_temp = tempfile.TemporaryDirectory(dir=ROOT / "working")
        self.source_temp = tempfile.TemporaryDirectory(dir=ROOT / "sources")
        self.runtime_root = Path(self.runtime_temp.name)
        self.qualification_path = (
            self.runtime_root / "private-docx" / "qualification.json"
        )
        self.expectation_path = (
            self.runtime_root
            / "private-docx"
            / "qualification-inputs"
            / "expectation.json"
        )
        self.qualification_path.parent.mkdir(parents=True)
        self.expectation_path.parent.mkdir(parents=True)
        self.source_root = Path(self.source_temp.name)
        self.source = self.source_root / "private-customer-name.docx"
        self.data = DOCX_FIXTURE.read_bytes()
        self.source.write_bytes(self.data)
        self.source_digest = "sha256:" + hashlib.sha256(self.data).hexdigest()
        self.expectation = docx_expectation(derived_digest=self.source_digest)
        self.validated_expectation = validate_docx_expectation(self.expectation)
        self.expectation_path.write_text(
            json.dumps(self.expectation) + "\n", encoding="utf-8"
        )
        self.manifest = docx_benchmark(
            fixture_corpus_digest=self.validated_expectation.corpus_digest
        )
        self.qualification_path.write_text(
            json.dumps(self.manifest) + "\n", encoding="utf-8"
        )
        self.context_path = self.runtime_root / "candidate-context.json"
        self.context = {"known_candidate_digest": "sha256:" + "8" * 64}
        self.context_path.write_text(json.dumps(self.context) + "\n", encoding="utf-8")
        self.source_argument = str(self.source.relative_to(ROOT))

    def tearDown(self):
        self.runtime_temp.cleanup()
        self.source_temp.cleanup()

    def invoke(self, argv):
        stdout = StringIO()
        stderr = StringIO()
        with patch.dict(
            os.environ, {"AO_LORE_HOME": str(self.runtime_root)}, clear=False
        ), redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                status = main(argv)
            except SystemExit as exc:
                status = int(exc.code)
        return status, stdout.getvalue(), stderr.getvalue()

    @patch("ao_lore.__main__.ingest_verified_docx", create=True)
    def test_ingest_docx_loads_qualified_manifest_profiles_and_origin(self, ingest):
        ingest.return_value = ingest_readback(
            parser_id=DOCX_PARSER_ID,
            parser_version=DOCX_PARSER_VERSION,
        )

        status, output, error = self.invoke([
            "ingest-docx", "--source", self.source_argument,
            "--candidate-context", str(self.context_path), "--json",
        ])

        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output), ingest.return_value)
        ingest.assert_called_once_with(
            self.data,
            source_digest=self.source_digest,
            resource="source-" + self.source_digest.removeprefix("sha256:")[:16] + ".docx",
            benchmark_manifest=self.manifest,
            expected_corpus_digest=self.validated_expectation.corpus_digest,
            selection_profile={
                "profile_id": "docx-native-v1",
                "weights": {
                    "structural_fidelity": 0.5,
                    "text_fidelity": 0.5,
                },
                "penalty_weights": {},
                "normalization_bounds": {},
                "missing_optional_policy": "ineligible",
                "tie_break_order": [
                    "required_feature_coverage",
                    "structural_fidelity",
                    "parser_id",
                ],
                "fixture_corpus_digest": self.manifest["fixture_corpus_digest"],
            },
            quality_profile={
                "profile_id": "docx-quality-v1",
                "component_weights": {
                    "text_coverage": 0.5,
                    "structural_completeness": 0.3,
                    "document_ir_valid": 0.2,
                },
                "accept_threshold": 0.8,
                "fallback_threshold": 0.0,
                "maximum_fallbacks": 0,
                "critical_failures": [
                    "invalid_ir",
                    "source_digest_mismatch",
                    "parser_crash",
                    "no_readable_content",
                    "lost_provenance",
                ],
                "calibration_result_digest": self.manifest["result_digest"],
                "intermediate_decision": "reject",
                "exhausted_decision": "reject",
            },
            origin=DocxProvenanceOrigin(
                corpus_id=DOCX_PRIVATE_CORPUS_ID,
                original_digest=self.expectation["items"][0]["source_digest"],
                derived_digest=self.source_digest,
                transformation_id=DOCX_TRANSFORMATION_ID,
                expectation_digest=self.validated_expectation.corpus_digest,
            ),
            candidate_context=self.context,
        )

    @patch("ao_lore.__main__.ingest_verified_docx", create=True)
    def test_ingest_docx_has_redacted_failure_boundary(self, ingest):
        private = "/private/docx/customer.docx"
        ingest.side_effect = IngestionError(private)
        status, output, error = self.invoke(
            ["ingest-docx", "--source", self.source_argument, "--json"]
        )
        self.assertEqual((status, output), (2, ""))
        self.assertEqual(error, "ao-lore: document ingest rejected\n")
        self.assertNotIn(private, error)

    @patch("ao_lore.__main__.ingest_verified_docx", create=True)
    def test_missing_or_corrupt_manifest_is_redacted_and_service_is_not_called(self, ingest):
        variants = ("missing", "corrupt")
        for variant in variants:
            with self.subTest(variant=variant):
                if variant == "missing":
                    self.qualification_path.unlink(missing_ok=True)
                else:
                    self.qualification_path.write_text(
                        '{"private": "/customer/path", "private": 2}\n',
                        encoding="utf-8",
                    )
                status, output, error = self.invoke([
                    "ingest-docx", "--source", self.source_argument, "--json"
                ])
                self.assertEqual((status, output), (2, ""))
                self.assertEqual(error, "ao-lore: document ingest rejected\n")
        ingest.assert_not_called()

    @patch("ao_lore.__main__.ingest_verified_docx", create=True)
    def test_missing_corrupt_foreign_or_ambiguous_expectation_rejects_before_ingest(self, ingest):
        cases = ("missing", "corrupt", "foreign", "ambiguous")
        for case in cases:
            with self.subTest(case=case):
                self.expectation_path.write_text(
                    json.dumps(self.expectation) + "\n", encoding="utf-8"
                )
                self.source.write_bytes(self.data)
                if case == "missing":
                    self.expectation_path.unlink(missing_ok=True)
                elif case == "corrupt":
                    self.expectation_path.write_text(
                        '{"items": [], "items": []}\n', encoding="utf-8"
                    )
                elif case == "foreign":
                    foreign = DOCX_FIXTURE.read_bytes() + b" "
                    self.source.write_bytes(foreign)
                else:
                    changed = json.loads(json.dumps(self.expectation))
                    changed["items"][1]["derived_digest"] = changed["items"][0]["derived_digest"]
                    self.expectation_path.write_text(
                        json.dumps(changed) + "\n", encoding="utf-8"
                    )
                status, output, error = self.invoke([
                    "ingest-docx", "--source", self.source_argument, "--json"
                ])
                self.assertEqual((status, output), (2, ""))
                self.assertEqual(error, "ao-lore: document ingest rejected\n")
        ingest.assert_not_called()

    def test_ingest_help_does_not_expose_candidate_root_override(self):
        status, output, error = self.invoke(["ingest-docx", "--help"])
        self.assertEqual((status, error), (0, ""))
        self.assertNotIn("candidate-root", output)

    @patch("ao_lore.__main__.ingest_verified_docx")
    def test_unknown_ingest_readback_field_is_rejected_before_output(self, ingest):
        ingest.return_value = ingest_readback(
            parser_id=DOCX_PARSER_ID,
            parser_version=DOCX_PARSER_VERSION,
            private_path="/private/customer/source.docx",
        )

        status, output, error = self.invoke([
            "ingest-docx", "--source", self.source_argument, "--json"
        ])

        self.assertEqual((status, output), (2, ""))
        self.assertEqual(error, "ao-lore: document ingest rejected\n")
        self.assertNotIn("private", error)

    @patch("ao_lore.__main__.ingest_verified_docx")
    def test_malformed_or_authority_widened_readbacks_never_emit(self, ingest):
        digest_fields = (
            "candidate_digest",
            "provenance_digest",
            "source_digest",
            "document_ir_digest",
            "parser_selection_report_digest",
            "parse_quality_report_digest",
            "distillation_trace_digest",
        )
        cases = {
            "not object": [],
            "missing field": lambda value: value.pop("source_digest"),
            "schema": lambda value: value.update(schema_version="ao.lore.v9"),
            "status": lambda value: value.update(status="promoted"),
            "candidate prefix": lambda value: value.update(candidate_id="other-id"),
            "candidate type": lambda value: value.update(candidate_id=7),
            "parser id": lambda value: value.update(parser_id="other-parser"),
            "parser id type": lambda value: value.update(parser_id=[]),
            "parser version": lambda value: value.update(parser_version="2.0.0"),
            "review status": lambda value: value.update(review_status="approved"),
            "canonical true": lambda value: value.update(canonical=True),
            "canonical integer": lambda value: value.update(canonical=0),
            "authority true": lambda value: value.update(promotion_authority=True),
            "authority integer": lambda value: value.update(promotion_authority=0),
            "commands type": lambda value: value.update(next_commands="inspect"),
            "commands empty": lambda value: value.update(next_commands=[]),
            "commands excessive": lambda value: value.update(next_commands=["x"] * 9),
            "command empty": lambda value: value.update(next_commands=[""]),
            "command long": lambda value: value.update(next_commands=["x" * 1025]),
            "command wrong type": lambda value: value.update(next_commands=[1]),
        }
        for field in digest_fields:
            cases[f"{field} malformed"] = (
                lambda value, active=field: value.update(**{active: "sha256:ABC"})
            )
        for label, mutation in cases.items():
            with self.subTest(label=label):
                report = ingest_readback(
                    parser_id=DOCX_PARSER_ID,
                    parser_version=DOCX_PARSER_VERSION,
                )
                if callable(mutation):
                    mutation(report)
                else:
                    report = mutation
                ingest.return_value = report
                status, output, error = self.invoke([
                    "ingest-docx", "--source", self.source_argument, "--json"
                ])
                self.assertEqual((status, output), (2, ""))
                self.assertEqual(error, "ao-lore: document ingest rejected\n")

    @patch("ao_lore.__main__.ingest_verified_docx")
    def test_serialization_failure_occurs_before_any_output(self, ingest):
        ingest.return_value = ingest_readback(
            parser_id=DOCX_PARSER_ID, parser_version=DOCX_PARSER_VERSION
        )
        with patch("ao_lore.__main__.json.dumps", side_effect=TypeError("private")):
            status, output, error = self.invoke([
                "ingest-docx", "--source", self.source_argument, "--json"
            ])
        self.assertEqual((status, output), (2, ""))
        self.assertEqual(error, "ao-lore: document ingest rejected\n")

    @patch("ao_lore.__main__.ingest_verified_docx")
    def test_private_free_form_next_command_is_rejected_before_output(self, ingest):
        ingest.return_value = ingest_readback(
            parser_id=DOCX_PARSER_ID,
            parser_version=DOCX_PARSER_VERSION,
            next_commands=["cat /private/customer/source.docx"]
        )

        status, output, error = self.invoke([
            "ingest-docx", "--source", self.source_argument, "--json"
        ])

        self.assertEqual((status, output), (2, ""))
        self.assertEqual(error, "ao-lore: document ingest rejected\n")
        self.assertNotIn("private", error)

    @patch("ao_lore.__main__.ingest_verified_docx")
    def test_next_commands_reject_other_candidate_extra_and_reordering(self, ingest):
        expected = ingest_readback()["next_commands"]
        variants = (
            [command.replace("candidate-cli-document", "candidate-other") for command in expected],
            [*expected, "ao-lore candidate list --json"],
            list(reversed(expected)),
        )
        for commands in variants:
            with self.subTest(commands=commands):
                ingest.return_value = ingest_readback(
                    parser_id=DOCX_PARSER_ID,
                    parser_version=DOCX_PARSER_VERSION,
                    next_commands=commands,
                )
                status, output, error = self.invoke([
                    "ingest-docx", "--source", self.source_argument, "--json"
                ])
                self.assertEqual((status, output), (2, ""))
                self.assertEqual(error, "ao-lore: document ingest rejected\n")


if __name__ == "__main__":
    unittest.main()
