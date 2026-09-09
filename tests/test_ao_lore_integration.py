from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from tests.private_calibration import private_calibration
from unittest.mock import patch

from ao_lore.benchmark import build_manifest, canonical_digest
from ao_lore.__main__ import (
    _docx_ingest_profiles,
    _ingest_profiles,
    _validate_ingest_readback,
)
from ao_lore.batch_ingestion import (
    BatchDependencies,
    ingest_batch,
    load_batch_manifest,
    load_or_initialize_checkpoint,
    validate_final_batch_readback,
)
from ao_lore.candidate_queue import list_candidates
from ao_lore.candidates import (
    CandidateError,
    append_review,
    build_candidate_provenance,
    inspect_candidate,
    load_verified_candidate,
    persist_candidate,
)
from ao_lore.docling_pdf import DoclingBackend, DoclingPdfAdapter
from ao_lore import docx_benchmark as docx_benchmark_module
from ao_lore.distillation import DeterministicDistiller, distill_document_ir
from ao_lore.docx_ooxml import (
    DOCX_PARSER_ID,
    DOCX_PARSER_VERSION,
    DocxLimits,
    docx_configuration_digest,
)
from ao_lore.ingestion import (
    IngestionError,
    ingest_docx_document,
    ingest_single_document,
    read_verified_docx_source,
    resolve_docx_origin,
)
from ao_lore.model_roles import RoleResult, RoleRuntime, ScriptedRoleAdapter
from ao_lore.navigation import CoverageNavigator, build_requirement_plan
from ao_lore.parsing import NativeMarkdownAdapter, ParserRegistry, ProductionParser
from ao_lore.private_docx_domain import (
    DOCX_MIME,
    DOCX_PRIVATE_CORPUS_ID,
    DOCX_TRANSFORMATION_ID,
    prepare_private_docx_corpus,
    validate_docx_expectation,
)
from ao_lore.private_docx_uat import (
    _launch_private_docx_worker,
    _signal_private_docx_worker,
    _terminate_worker,
    _wait_private_docx_worker,
    prepare_private_docx_uat_run,
)
from ao_lore.private_pdf_uat import (
    PRIVATE_PDF_CONFIGURATION_DIGEST,
    PrivatePdfCalibrationEvidence,
    _QUALIFIED_PDF_CORPUS_DIGEST,
    _default_private_pdf_uat_dependencies,
    _launch_private_pdf_worker,
    _manifest_body,
    run_private_pdf_uat,
)
import ao_lore.private_pdf_uat as private_pdf_uat
from ao_lore.synthesis import DeterministicSynthesizer, synthesize_from_evidence


ROOT = Path(__file__).resolve().parents[1]
SHA = "sha256:" + "7" * 64
NATIVE_MARKDOWN_BENCHMARK = NativeMarkdownAdapter().capability["benchmark_result_refs"][0]
ROLES = ("parser", "distiller", "navigator", "synthesizer")
DOCX_FIXTURE = ROOT / "tests" / "fixtures" / "ao_lore" / "docx" / "minimal-paragraph.docx"


class CandidateQualityDocumentationTests(unittest.TestCase):
    def test_current_docs_preserve_quality_recommendation_non_authority(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        workflow = (ROOT / "docs/workflows/review-public-candidate-quality.md").read_text(encoding="utf-8")
        self.assertIn("rehearse-public-candidate-quality-review.py --check", readme)
        self.assertIn("486 claims and citations", readme)
        self.assertIn("[x] Produce and semantically annotate the deterministic 96-claim sample", roadmap)
        self.assertIn("[ ] Take any candidate accept/reject action", roadmap)
        self.assertIn("It is not acceptance", workflow)
        self.assertIn("additional exact one-shot authorization", workflow)


class EvidenceGraphDocumentationTests(unittest.TestCase):
    def test_connected_bundle_and_non_authority_are_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        workflow = (ROOT / "docs/workflows/workspaces.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("generic graph APIs", readme)
        self.assertIn("not canonical knowledge", readme)
        self.assertIn("[x] Run independently precommitted", roadmap)
        self.assertIn("[ ] Treat any graph claim as candidate or canonical knowledge", roadmap)
        self.assertIn("informational and non-canonical", workflow)
        self.assertNotIn("compatibility", workflow.lower())
        self.assertIn("Evidence-graph membership is informational retrieval state", agents)


def brain_snapshot() -> dict[str, str]:
    return {
        path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((ROOT / "brain").rglob("*"))
        if path.is_file()
    }


def selection_profile() -> dict:
    return {
        "profile_id": "markdown-local-v1",
        "weights": {"structural_fidelity": 0.5, "text_fidelity": 0.5},
        "penalty_weights": {},
        "normalization_bounds": {},
        "missing_optional_policy": "ineligible",
        "tie_break_order": ["required_feature_coverage", "structural_fidelity", "parser_id"],
        "fixture_corpus_digest": NATIVE_MARKDOWN_BENCHMARK,
    }


def quality_profile() -> dict:
    return {
        "profile_id": "markdown-quality-v1",
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
        "calibration_result_digest": NATIVE_MARKDOWN_BENCHMARK,
        "intermediate_decision": "quarantine",
        "exhausted_decision": "reject",
    }


def docx_batch_qualification(fixture_corpus_digest: str) -> dict:
    manifest = {
        "schema_version": "ao.lore.docx-benchmark-result.v0.1",
        "fixture_corpus_digest": fixture_corpus_digest,
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
    manifest["result_digest"] = canonical_digest(manifest)
    return manifest


def docx_expectation(derived_digest: str, *, source_digest: str = "sha256:" + "8" * 64) -> dict:
    items = []
    for index in range(1, 101):
        items.append(
            {
                "item_id": f"docx-{index:04d}",
                "source_digest": source_digest if index == 1 else f"sha256:{index:064x}",
                "derived_digest": derived_digest if index == 1 else f"sha256:{index + 1000:064x}",
                "source_bytes": 1024 + index,
                "derived_bytes": 2048 + index,
                "transformation_id": "restore-ooxml-local-header-v1",
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
        "corpus_id": "docx-nomagic-uk-public-sector-v1",
        "items": items,
        "ocr_enabled": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


def requirements(max_depth: int = 8) -> dict:
    return build_requirement_plan(
        canonical_digest({"query": "What policy and procedure apply?"}),
        "decision-support-v1",
        [
            {
                "id": "policy",
                "criterion": "identify the reviewed policy",
                "importance_weight": 3,
                "evidence_type": "policy",
                "lifecycle_condition": "current",
                "trust_tier": "reviewed",
                "citation_required": True,
                "provenance_required": True,
                "mandatory": True,
            },
            {
                "id": "procedure",
                "criterion": "identify the supporting procedure",
                "importance_weight": 1,
                "evidence_type": "procedure",
                "lifecycle_condition": "non-stale",
                "trust_tier": "reviewed",
                "citation_required": True,
                "provenance_required": True,
                "mandatory": False,
            },
        ],
        {
            "max_depth": max_depth,
            "max_nodes": 8,
            "max_tokens": 1000,
            "max_seconds": 30,
            "max_replans": 2,
        },
    )


def coverage_profile() -> dict:
    return {
        "profile_id": "decision-support-v1",
        "target": 1.0,
        "satisfaction_values": {
            "satisfied": 1.0,
            "partially_satisfied": 0.5,
            "missing": 0.0,
            "contradictory": 0.0,
            "stale_only": 0.0,
            "disqualified": 0.0,
        },
    }


def evidence(requirement_id: str, source_ref: str) -> dict:
    return {
        "requirement_id": requirement_id,
        "status": "satisfied",
        "evidence_ref": source_ref,
        "provenance_present": True,
        "citation_present": True,
        "freshness_met": True,
        "trust_met": True,
        "hard_contradiction": False,
    }


def ledger_entry(requirement_id: str, source_ref: str, claim: str) -> dict:
    return {
        "evidence_id": f"evidence-{requirement_id}",
        "requirement_ids": [requirement_id],
        "supported_claims": [{"claim_id": f"claim-{requirement_id}", "text": claim}],
        "source_ref": source_ref,
        "source_digest": SHA,
        "citation": f"[{requirement_id}]({source_ref})",
        "provenance": {"kind": "canonical-okf", "path": source_ref},
        "freshness_met": True,
        "trust_met": True,
        "verified": True,
    }


def role_config(role: str) -> dict:
    return {
        "schema_version": "ao.lore.model-role-config.v0.1",
        "role": role,
        "adapter": f"{role}-scripted",
        "provider": "local-runtime",
        "endpoint": None,
        "model_id": f"{role}-model-v1",
        "policy_version": f"{role}-policy-v1",
        "output_schema_version": f"ao.lore.{role}-output.v0.1",
        "context_limit": 4096,
        "output_limit": 512,
        "temperature": 0,
        "decoding_controls": {"seed": 17},
        "timeout_ms": 1000,
        "retries": 0,
        "network_policy": "offline",
        "privacy_policy": "local-only",
        "credential_environment_variable": None,
        "token_budget": 100,
        "monetary_budget": 0,
        "fallback_policy": {"enabled": False, "adapters": []},
        "cache_policy": {"enabled": False, "ttl_seconds": 0},
        "no_implicit_cross_role_inheritance": True,
    }


def role_input(role: str) -> dict:
    return {
        "parser": {"source_region": {"digest": SHA}, "parser_context": {}},
        "distiller": {
            "document_ir": {"schema_version": "ao.lore.document-ir.v0.1"},
            "candidate_context": {},
        },
        "navigator": {
            "normalized_query": "policy",
            "routing_metadata": {},
            "navigation_state": {},
            "visited_nodes": [],
            "evidence_summary": {},
            "coverage_gaps": [],
            "budgets": {},
        },
        "synthesizer": {
            "normalized_query": "policy",
            "evidence_ledger": [],
            "coverage_report": {},
            "answer_format": "text",
            "citation_requirements": {},
        },
    }[role]


class AOLoreIntegrationTests(unittest.TestCase):
    def test_docx_qualification_execution_has_one_product_input_writer(self):
        bodies = {
            f"docx-{index:04d}": b"PK\x03\x04" + bytes([index % 251]) * index
            for index in range(1, 101)
        }
        expectation = docx_expectation("sha256:" + "1" * 64)
        documents = []
        for item, body in zip(expectation["items"], bodies.values(), strict=True):
            item["derived_digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
            item["derived_bytes"] = len(body)
            documents.append(
                {
                    "item_id": item["item_id"],
                    "fixture_name": item["item_id"].removeprefix("docx-")
                    + "-reviewed.docx",
                    "source_digest": item["source_digest"],
                    "derived_digest": item["derived_digest"],
                    "source_bytes": item["source_bytes"],
                    "derived_bytes": item["derived_bytes"],
                    "transformation_id": DOCX_TRANSFORMATION_ID,
                }
            )
        validated = validate_docx_expectation(expectation)
        prepared = {
            "schema_version": "ao.lore.private-docx-qualification-inputs.v0.1",
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "expectation_digest": validated.corpus_digest,
            "source_set_digest": "sha256:" + "2" * 64,
            "derived_set_digest": "sha256:" + "3" * 64,
            "transformation_id": DOCX_TRANSFORMATION_ID,
            "documents": documents,
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        benchmark_result = {
            "fixture_corpus_digest": validated.corpus_digest,
            "decision": "hold",
        }
        consumed = []

        with patch.object(
            docx_benchmark_module,
            "prepare_private_docx_qualification_inputs",
            return_value=prepared,
        ) as prepare, patch.object(
            docx_benchmark_module,
            "strict_read_json",
            return_value=(expectation, b"expectation"),
        ), patch.object(
            docx_benchmark_module,
            "load_fixed_docx_fixture_data",
            return_value=bodies,
        ), patch.object(
            docx_benchmark_module,
            "run_docx_benchmark",
            return_value=benchmark_result,
        ) as benchmark, patch.object(
            Path,
            "write_bytes",
            side_effect=AssertionError("manual qualification writer forbidden"),
        ), patch.object(
            Path,
            "write_text",
            side_effect=AssertionError("manual qualification writer forbidden"),
        ):
            result = docx_benchmark_module.run_private_docx_qualification(
                lambda manifest: consumed.append(dict(manifest)) or object(),
                (),
                expectation,
                source_root=ROOT / "synthetic-reviewed",
                runtime_root=ROOT / "synthetic-runtime",
            )

        prepare.assert_called_once()
        benchmark.assert_called_once()
        self.assertEqual([prepared], consumed)
        self.assertEqual(benchmark_result, result)

    def test_private_docx_operator_contract_and_retained_evidence_are_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "docs/architecture/ao-lore.md").read_text(
            encoding="utf-8"
        )
        roadmap = (ROOT / "ROADMAP.md").read_text(encoding="utf-8")
        for action in ("prepare-seed", "run", "cleanup"):
            self.assertIn(f"scripts/private-docx-uat.py {action} --json", readme)
        self.assertIn("restore-ooxml-local-header-v1", readme)
        self.assertIn("88 accepted documents, 11 expected", readme)
        self.assertIn("one expected invalid-package rejection", readme)
        self.assertIn("private DOCX operator", architecture)
        self.assertIn("DOCX uses a dedicated OOXML baseline with no active fallback", architecture)
        self.assertIn("does not grant qualification or promotion authority", architecture)
        self.assertIn("[x] Execute and retain the exact private DOCX campaign evidence", roadmap)
        self.assertIn(
            "[x] Activate the DOCX adapter only if retained representative evidence yields `hold`",
            roadmap,
        )
        self.assertIn(
            "sha256:e36b132b49cc08b5815757912c0f77d68b2465095db40559608d878930d594fe",
            readme,
        )
        self.assertIn("[x] Implement and benchmark OCR/layout adapters", roadmap)
        self.assertIn("[x] Add separately authorized candidate promotion controls", roadmap)
        self.assertIn("Each such action requires", readme)
        self.assertIn("separate explicit authorization", readme)
        self.assertTrue((ROOT / "workflows/promote-candidate.md").is_file())
        self.assertIn(
            "[x] Evaluate PaddleOCR/OCR separately; keep any bounded Docling DOCX fallback separate",
            roadmap,
        )

    def test_exact_private_ocr_campaign_is_documented_without_authority(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "docs/architecture/ao-lore.md").read_text(
            encoding="utf-8"
        )
        for text in (readme, architecture):
            normalized = " ".join(text.split())
            self.assertIn("1 initial, 3 resumed, and 0 rerun", normalized)
            self.assertIn("sha256:c84108676f20b3703dcdc7ada4265e53198027bde2a53fcf883fd33bbebb28ad", normalized)
            self.assertIn("sha256:e8395e18fb970438b33640320646b9192b02bd4e6c719fe862b7da5de947344a", normalized)
            self.assertIn("promotion, release, publication, or deployment authority", normalized)

    @private_calibration
    def test_private_pdf_default_launcher_executes_worktree_module_offline(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            runtime = Path(temporary) / "runtime"
            corpus = runtime / "calibration/private-pdf/ubuntu-pdf-seed-v1"
            inputs = corpus / "input"
            inputs.mkdir(parents=True)
            bodies = {
                "seed-01": b"%PDF-1.7\nfirst integration fixture\n%%EOF\n",
                "seed-02": b"%PDF-1.7\nsecond integration fixture\n%%EOF\n",
            }
            documents = []
            for item_id, body in bodies.items():
                (inputs / f"{item_id}.pdf").write_bytes(body)
                documents.append({
                    "item_id": item_id,
                    "file": f"input/{item_id}.pdf",
                    "source_digest": "sha256:" + hashlib.sha256(body).hexdigest(),
                    "expected_text": [f"{item_id} integration"],
                    "required_block_types": (
                        ["heading", "paragraph"]
                        if item_id == "seed-01" else ["heading", "table"]
                    ),
                })
            private_manifest = {
                "schema_version": "ao.lore.private-pdf-uat-manifest.v0.1",
                "corpus_id": "ubuntu-pdf-seed-v1",
                "parser_id": "docling", "parser_version": "2.118.1",
                "ocr_enabled": False, "documents": documents,
                "provider_calls": False, "promotion_authority": False,
                "claims_authority_advance": False,
            }
            (corpus / "manifest.json").write_bytes(_manifest_body(private_manifest))
            qualification = build_manifest(
                fixture_corpus_digest=_QUALIFIED_PDF_CORPUS_DIGEST,
                parser_id="docling", parser_version="2.118.1",
                parser_configuration_digest=PRIVATE_PDF_CONFIGURATION_DIGEST,
                runtime="integration", platform="linux",
                document_ir_version="ao.lore.document-ir.v0.1",
                commands=[["internal", "fixture"]], raw_metrics={},
                normalized_scores={
                    "structural_fidelity": 1.0, "text_fidelity": 1.0,
                    "source_location_fidelity": 1.0, "reliability": 1.0,
                    "deterministic_repeatability": 1.0,
                }, failures=[], exclusions=[],
            )
            benchmark = runtime / "benchmarks/docling-2.118.1.json"
            benchmark.parent.mkdir(parents=True)
            benchmark.write_bytes(_manifest_body(qualification))
            for model in (
                "models--docling-project--docling-layout-heron",
                "models--docling-project--docling-models",
            ):
                snapshot = (
                    runtime / "cache/huggingface/hub" / model
                    / "snapshots/test-snapshot"
                )
                snapshot.mkdir(parents=True)
                (snapshot / "model.bin").write_bytes(b"contained fixture")
            aggregate = {
                "metrics": {
                    "structural_fidelity": 1.0, "text_fidelity": 1.0,
                    "source_location_fidelity": 1.0,
                },
                "exclusions": [],
                "stable_failures": {
                    "conversion_failed": 0, "digest_mismatch": 0,
                    "encrypted_document": 0, "invalid_pdf": 0,
                    "no_readable_content": 0,
                },
                "latency_seconds": {"minimum": 0.0, "maximum": 0.0, "mean": 0.0},
                "peak_memory_bytes": {"minimum": 0.0, "maximum": 0.0, "mean": 0.0},
                "repeatability": {"runs": 2, "identical": True, "score": 1.0},
            }
            defaults = _default_private_pdf_uat_dependencies(runtime)
            prepared_cache = {}
            def launch_fixture(run, phase):
                prepared_cache["run"] = run
                return _launch_private_pdf_worker(
                    run, phase,
                    module="tests.private_pdf_uat_worker_fixture",
                    pythonpath=f"{ROOT / 'src'}:{ROOT}",
                )

            dependencies = replace(
                defaults,
                launch_process=launch_fixture,
                evaluate_calibration=lambda manifest, root: PrivatePdfCalibrationEvidence(
                    manifest_digest=canonical_digest(manifest),
                    corpus_digest=canonical_digest(manifest),
                    configuration_digest=PRIVATE_PDF_CONFIGURATION_DIGEST,
                    qualification_digest=qualification["result_digest"],
                    result_digest=canonical_digest(aggregate),
                    aggregate=aggregate,
                    sealed_cache_tree_digest=(
                        prepared_cache["run"].sealed_cache_tree_digest
                    ),
                    sealed_cache_file_count=(
                        prepared_cache["run"].sealed_cache_file_count
                    ),
                    sealed_cache_total_bytes=(
                        prepared_cache["run"].sealed_cache_total_bytes
                    ),
                    sandbox_digest="sha256:" + "e" * 64,
                    sandbox_verified=True,
                    network_isolated=True,
                    campaign_origin_digest=(
                        private_pdf_uat._campaign_origin_digest(
                            prepared_cache["run"].origin
                        )
                    ),
                ),
            )
            with patch.object(private_pdf_uat, "_verify_pdf", return_value=None):
                report = run_private_pdf_uat(
                    runtime_root=runtime, dependencies=dependencies
                )
            self.assertFalse((runtime / "fixture-worker-swap.json").exists())
            self.assertEqual(
                {"initial": 1, "resumed": 1, "rerun": 0},
                report["conversion_counts"],
            )
            retained_path = next(
                (runtime / "evidence/private-pdf-uat").glob(
                    "batch-private-uat-*/private-pdf-uat-readback.json"
                )
            )
            retained_body = retained_path.read_bytes()
            retained = json.loads(retained_body)
            self.assertEqual(report, retained)
            self.assertEqual(
                {
                    "metrics", "exclusions", "stable_failures",
                    "latency_seconds", "peak_memory_bytes", "repeatability",
                },
                set(retained["aggregate"]),
            )
            state = json.loads((retained_path.parent / "cleaned-state.json").read_text())
            self.assertEqual(
                "sha256:" + hashlib.sha256(retained_body).hexdigest(),
                state["readback_digest"],
            )
            self.assertEqual("cleaned", state["lifecycle_status"])
            self.assertEqual(
                state["brain_before_digest"], state["brain_post_run_digest"]
            )
            self.assertEqual(
                state["brain_before_digest"], state["brain_post_cleanup_digest"]
            )
            self.assertFalse((ROOT / "sources/.private-pdf-uat").exists())
    def test_real_pdf_batch_ingest_resume_preserves_brain(self):
        manifest_path = ROOT / ".ao-lore" / "benchmarks" / "docling-2.118.1.json"
        if importlib.util.find_spec("docling") is None or not manifest_path.is_file():
            self.skipTest("real Docling calibration environment is not prepared")

        qualification = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            (qualification["parser_id"], qualification["parser_version"]),
            ("docling", "2.118.1"),
        )
        selection, quality = _ingest_profiles(qualification)
        before = brain_snapshot()
        original_backend_init = DoclingBackend.__init__
        original_backend_convert = DoclingBackend.convert
        offline_phases: list[str] = []
        conversion_calls: list[str] = []
        network_attempts: list[object] = []

        def assert_offline(phase: str) -> None:
            self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
            self.assertEqual(os.environ.get("TRANSFORMERS_OFFLINE"), "1")
            self.assertEqual(os.environ.get("HF_HUB_DISABLE_TELEMETRY"), "1")
            offline_phases.append(phase)

        def guarded_backend_init(backend: DoclingBackend) -> None:
            assert_offline("construction")
            original_backend_init(backend)
            self.assertIs(backend.ocr_enabled, False)
            self.assertEqual(backend.allowed_formats, ("pdf",))

        def guarded_backend_convert(backend: DoclingBackend, *args, **kwargs):
            assert_offline("conversion")
            conversion_calls.append("conversion")
            return original_backend_convert(backend, *args, **kwargs)

        def reject_network(_socket, address):
            network_attempts.append(address)
            raise AssertionError("network access is forbidden")

        fixtures = (
            ("simple", "simple-structure.pdf"),
            ("multipage", "multi-page-sections.pdf"),
            ("imageonly", "image-only-document.pdf"),
        )
        batch_parent = ROOT / ".ao-lore" / "batches"
        batch_parent.mkdir(parents=True, exist_ok=True)
        with (
            tempfile.TemporaryDirectory(prefix="integration-batch-", dir=ROOT / "sources") as source_name,
            tempfile.TemporaryDirectory(prefix="integration-candidates-", dir=ROOT / "working" / "candidates") as candidate_name,
            tempfile.TemporaryDirectory(prefix="integration-state-", dir=batch_parent) as state_name,
        ):
            source_root = Path(source_name)
            candidate_root = Path(candidate_name)
            batch_root = Path(state_name)
            documents = []
            for item_id, fixture_name in fixtures:
                source_path = source_root / fixture_name
                shutil.copyfile(ROOT / "tests/fixtures/ao_lore/pdf" / fixture_name, source_path)
                documents.append({
                    "item_id": item_id,
                    "source": source_path.relative_to(ROOT).as_posix(),
                    "source_digest": "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest(),
                })
            batch_manifest_path = source_root / "batch.json"
            batch_manifest_path.write_text(json.dumps({
                "schema_version": "ao.lore.ingest-batch-manifest.v0.1",
                "batch_id": "batch-real-pdf-01",
                "documents": documents,
                "continue_on_error": True,
            }), encoding="utf-8")
            dependencies = BatchDependencies(
                benchmark_manifest=qualification,
                selection_profile=selection,
                quality_profile=quality,
                candidate_root=candidate_root,
            )
            with patch.dict(os.environ, {
                "AO_LORE_HOME": str(ROOT / ".ao-lore"),
                "HF_HOME": str(ROOT / ".ao-lore" / "cache" / "huggingface"),
                "XDG_CACHE_HOME": str(ROOT / ".ao-lore" / "cache" / "xdg"),
                "TORCH_HOME": str(ROOT / ".ao-lore" / "cache" / "torch"),
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
            }), patch.object(DoclingBackend, "__init__", guarded_backend_init), \
                 patch.object(DoclingBackend, "convert", guarded_backend_convert), \
                 patch.object(socket.socket, "connect", reject_network), \
                 patch.object(socket.socket, "connect_ex", reject_network):
                first = validate_final_batch_readback(ingest_batch(
                    batch_manifest_path,
                    dependencies=dependencies,
                    batch_root=batch_root,
                ))
                first_conversion_count = len(conversion_calls)
                second = validate_final_batch_readback(ingest_batch(
                    batch_manifest_path,
                    dependencies=dependencies,
                    batch_root=batch_root,
                ))

            self.assertEqual(first, second)
            self.assertEqual(first_conversion_count, 3)
            self.assertEqual(len(conversion_calls), first_conversion_count)
            self.assertEqual((first["status"], first["created"], first["unchanged"], first["rejected"]), (
                "partial", 2, 0, 1,
            ))
            self.assertEqual(
                [item["error_code"] for item in first["items"] if item["status"] == "rejected"],
                ["document_rejected"],
            )
            self.assertEqual(offline_phases.count("construction"), 3)
            self.assertEqual(offline_phases.count("conversion"), 3)
            self.assertEqual(network_attempts, [])
            candidate_ids = [
                item["candidate_id"] for item in first["items"]
                if item["status"] in {"created", "unchanged"}
            ]
            self.assertEqual(
                [item["candidate_id"] for item in list_candidates(candidate_root=candidate_root)["items"]],
                sorted(candidate_ids),
            )
            loaded_manifest, digest = load_batch_manifest(batch_manifest_path)
            checkpoint = load_or_initialize_checkpoint(
                loaded_manifest,
                digest,
                batch_root=batch_root,
                inspect_candidate_fn=lambda candidate_id: load_verified_candidate(
                    candidate_id, candidate_root=candidate_root
                ),
            )
            self.assertEqual(checkpoint["processed"], 3)
            self.assertTrue((batch_root / "checkpoint.json").is_file())
            self.assertTrue((batch_root / "final-readback.json").is_file())

            self.assertFalse(source_root.exists())
            self.assertFalse(candidate_root.exists())
            self.assertFalse(batch_root.exists())
            self.assertEqual(brain_snapshot(), before)

    def test_real_docx_batch_ingest_resume_preserves_brain(self):
        before = brain_snapshot()
        fixture = DOCX_FIXTURE.read_bytes()
        source_digest = "sha256:" + hashlib.sha256(fixture).hexdigest()
        expectation = validate_docx_expectation(docx_expectation(source_digest))
        qualification = docx_batch_qualification(expectation.corpus_digest)
        selection, quality = _docx_ingest_profiles(qualification)
        batch_parent = ROOT / ".ao-lore" / "batches"
        batch_parent.mkdir(parents=True, exist_ok=True)

        with (
            tempfile.TemporaryDirectory(prefix="integration-docx-batch-", dir=ROOT / "sources") as source_name,
            tempfile.TemporaryDirectory(prefix="integration-docx-candidates-", dir=ROOT / "working" / "candidates") as candidate_name,
            tempfile.TemporaryDirectory(prefix="integration-docx-state-", dir=batch_parent) as state_name,
        ):
            source_root = Path(source_name)
            candidate_root = Path(candidate_name)
            batch_root = Path(state_name)
            source_path = source_root / "document.docx"
            source_path.write_bytes(fixture)
            manifest_path = source_root / "batch.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema_version": "ao.lore.ingest-batch-manifest.v0.2",
                        "batch_id": "batch-real-docx-01",
                        "format_id": "docx",
                        "media_type": DOCX_MIME,
                        "parser_id": DOCX_PARSER_ID,
                        "parser_version": DOCX_PARSER_VERSION,
                        "documents": [
                            {
                                "item_id": "docx-one",
                                "source": source_path.relative_to(ROOT).as_posix(),
                                "source_digest": source_digest,
                            }
                        ],
                        "continue_on_error": True,
                    }
                ),
                encoding="utf-8",
            )

            def origin_for(path: str | Path, body: bytes):
                if body != fixture:
                    raise IngestionError("verified document binding is invalid")
                data, origin, corpus_digest = resolve_docx_origin(
                    path, expectation=expectation
                )
                if data != body or corpus_digest != expectation.corpus_digest:
                    raise IngestionError("verified document binding is invalid")
                return origin

            dependencies = BatchDependencies(
                benchmark_manifest=qualification,
                selection_profile=selection,
                quality_profile=quality,
                format_id="docx",
                media_type=DOCX_MIME,
                extension=".docx",
                parser_id=DOCX_PARSER_ID,
                parser_version=DOCX_PARSER_VERSION,
                signature_check=lambda data: data == fixture,
                ingest_one=ingest_docx_document,
                read_source=read_verified_docx_source,
                expected_corpus_digest=expectation.corpus_digest,
                origin_for=origin_for,
                candidate_root=candidate_root,
            )
            first = validate_final_batch_readback(
                ingest_batch(
                    manifest_path,
                    dependencies=dependencies,
                    batch_root=batch_root,
                )
            )
            second = validate_final_batch_readback(
                ingest_batch(
                    manifest_path,
                    dependencies=dependencies,
                    batch_root=batch_root,
                )
            )

            self.assertEqual(first, second)
            self.assertEqual((first["status"], first["created"], first["unchanged"], first["rejected"]), ("completed", 1, 0, 0))
            self.assertEqual(
                [item["candidate_id"] for item in list_candidates(candidate_root=candidate_root)["items"]],
                [first["items"][0]["candidate_id"]],
            )
            loaded_manifest, digest = load_batch_manifest(manifest_path)
            checkpoint = load_or_initialize_checkpoint(
                loaded_manifest,
                digest,
                batch_root=batch_root,
                inspect_candidate_fn=lambda candidate_id: load_verified_candidate(
                    candidate_id, candidate_root=candidate_root
                ),
            )
            self.assertEqual(checkpoint["processed"], 1)
            self.assertTrue((batch_root / "checkpoint.json").is_file())
            self.assertTrue((batch_root / "final-readback.json").is_file())

        self.assertFalse(source_root.exists())
        self.assertFalse(candidate_root.exists())
        self.assertFalse(batch_root.exists())
        self.assertEqual(brain_snapshot(), before)

    def test_real_pdf_ingest_to_review_queue_preserves_brain(self):
        manifest_path = ROOT / ".ao-lore" / "benchmarks" / "docling-2.118.1.json"
        if (
            importlib.util.find_spec("docling") is None
            or not manifest_path.is_file()
        ):
            self.skipTest("real Docling calibration environment is not prepared")

        hub = ROOT / ".ao-lore" / "cache" / "huggingface" / "hub"
        required_models = (
            "models--docling-project--docling-layout-heron",
            "models--docling-project--docling-models",
        )
        hub_resolved = hub.resolve(strict=True)
        for model_name in required_models:
            model_root = hub / model_name
            snapshots = model_root / "snapshots"
            self.assertFalse(model_root.is_symlink())
            self.assertTrue(
                model_root.resolve(strict=True).is_relative_to(hub_resolved)
            )
            self.assertTrue(
                snapshots.is_dir() and not snapshots.is_symlink(),
                f"required offline Docling snapshots are missing: {model_name}",
            )
            snapshot_directories = [
                path
                for path in snapshots.iterdir()
                if path.is_dir()
                and not path.is_symlink()
                and path.resolve(strict=True).is_relative_to(hub_resolved)
            ]
            self.assertTrue(
                snapshot_directories,
                f"required offline Docling snapshots are empty: {model_name}",
            )
            for snapshot in snapshot_directories:
                files = [path for path in snapshot.rglob("*") if path.is_file()]
                self.assertTrue(
                    files,
                    f"required offline Docling snapshot content is incomplete: {model_name}",
                )
                self.assertTrue(
                    all(
                        path.resolve(strict=True).is_relative_to(hub_resolved)
                        for path in files
                    ),
                    f"offline Docling snapshot escapes cache root: {model_name}",
                )

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["parser_id"], "docling")
        self.assertEqual(manifest["parser_version"], "2.118.1")
        selection, quality = _ingest_profiles(manifest)
        before = brain_snapshot()
        fixture = ROOT / "tests/fixtures/ao_lore/pdf/simple-structure.pdf"
        original_backend_init = DoclingBackend.__init__
        original_backend_convert = DoclingBackend.convert
        offline_phases: list[str] = []

        def assert_offline(phase: str) -> None:
            self.assertEqual(os.environ.get("HF_HUB_OFFLINE"), "1")
            self.assertEqual(os.environ.get("TRANSFORMERS_OFFLINE"), "1")
            self.assertEqual(os.environ.get("HF_HUB_DISABLE_TELEMETRY"), "1")
            offline_phases.append(phase)

        def guarded_backend_init(backend: DoclingBackend) -> None:
            assert_offline("construction")
            original_backend_init(backend)

        def guarded_backend_convert(
            backend: DoclingBackend, *args, **kwargs
        ):
            assert_offline("conversion")
            return original_backend_convert(backend, *args, **kwargs)

        with (
            tempfile.TemporaryDirectory(
                prefix="integration-ingest-", dir=ROOT / "sources"
            ) as source_name,
            tempfile.TemporaryDirectory(
                prefix="integration-candidates-", dir=ROOT / "working" / "candidates"
            ) as candidate_name,
        ):
            source_path = Path(source_name) / "document.pdf"
            candidate_root = Path(candidate_name)
            shutil.copyfile(fixture, source_path)
            with patch.dict(
                os.environ,
                {
                    "HF_HOME": str(ROOT / ".ao-lore" / "cache" / "huggingface"),
                    "XDG_CACHE_HOME": str(ROOT / ".ao-lore" / "cache" / "xdg"),
                    "TORCH_HOME": str(ROOT / ".ao-lore" / "cache" / "torch"),
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "HF_HUB_DISABLE_TELEMETRY": "1",
                },
            ), patch.object(
                DoclingBackend, "__init__", guarded_backend_init
            ), patch.object(
                DoclingBackend, "convert", guarded_backend_convert
            ):
                readback = _validate_ingest_readback(
                    ingest_single_document(
                        source_path,
                        benchmark_manifest=manifest,
                        selection_profile=selection,
                        quality_profile=quality,
                        candidate_root=candidate_root,
                    )
                )
            self.assertEqual(offline_phases, ["construction", "conversion"])

            self.assertEqual(
                set(readback),
                {
                    "schema_version", "status", "candidate_id",
                    "candidate_digest", "provenance_digest", "source_digest",
                    "parser_id", "parser_version", "document_ir_digest",
                    "parser_selection_report_digest", "parse_quality_report_digest",
                    "distillation_trace_digest", "review_status", "next_commands",
                    "canonical", "promotion_authority",
                },
            )
            self.assertEqual(readback["status"], "created")
            self.assertEqual(readback["review_status"], "unreviewed")
            self.assertFalse(readback["canonical"])
            self.assertFalse(readback["promotion_authority"])
            candidate_id = readback["candidate_id"]

            pending = list_candidates(candidate_root=candidate_root)
            self.assertEqual(pending["requested_status"], "unreviewed")
            self.assertEqual(
                [item["candidate_id"] for item in pending["items"]],
                [candidate_id],
            )

            append_review(
                candidate_id,
                "accept",
                "integration-reviewer",
                candidate_root=candidate_root,
                recorded_at="2026-08-09T14:00:00Z",
            )
            self.assertEqual(list_candidates(candidate_root=candidate_root)["items"], [])
            accepted = list_candidates(
                status="accepted", candidate_root=candidate_root
            )
            all_candidates = list_candidates(
                status="all", candidate_root=candidate_root
            )
            self.assertEqual(
                [item["candidate_id"] for item in accepted["items"]],
                [candidate_id],
            )
            self.assertEqual(
                [item["candidate_id"] for item in all_candidates["items"]],
                [candidate_id],
            )
            self.assertEqual(accepted["items"][0]["review_status"], "accepted")

        self.assertEqual(brain_snapshot(), before)

    def test_candidate_provenance_requires_exact_accepted_parse_bindings(self):
        registry = ParserRegistry()
        registry.register(NativeMarkdownAdapter())
        parsed = ProductionParser(registry).parse_bytes(
            b"# Bound Evidence\n\nKeep exact provenance.\n",
            "sources/provenance.md",
            {
                "media_type": "text/markdown", "extension": ".md",
                "required_structural_elements": ["headings"],
                "canonical_ir_version": "ao.lore.document-ir.v0.1",
                "network_allowed": False, "license_allowlist": ["Apache-2.0"],
                "sandbox_required": True,
            },
            selection_profile(), quality_profile(),
        )
        distilled = distill_document_ir(parsed["document_ir"], DeterministicDistiller())
        provenance = build_candidate_provenance(
            parsed, distilled, created_at="2026-08-09T14:00:00Z"
        )
        self.assertEqual(provenance["candidate_digest"], distilled["candidate_digest"])
        self.assertEqual(provenance["source_digest"], parsed["document_digest"])
        self.assertEqual(provenance["parser_id"], "native-markdown")
        with self.assertRaises(CandidateError):
            build_candidate_provenance(
                {**parsed, "decision": "reject"}, distilled,
                created_at="2026-08-09T14:00:00Z",
            )

    def test_real_pdf_to_reviewed_candidate_preserves_brain(self):
        manifest_path = ROOT / ".ao-lore" / "benchmarks" / "docling-2.118.1.json"
        artifacts = ROOT / ".ao-lore" / "cache" / "docling"
        if importlib.util.find_spec("docling") is None or not manifest_path.is_file() or not artifacts.is_dir():
            self.skipTest("real Docling calibration environment is not prepared")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        before = brain_snapshot()
        with patch.dict(os.environ, {
            "DOCLING_ARTIFACTS_PATH": str(artifacts),
            "HF_HOME": str(ROOT / ".ao-lore" / "cache" / "huggingface"),
            "XDG_CACHE_HOME": str(ROOT / ".ao-lore" / "cache" / "xdg"),
            "TORCH_HOME": str(ROOT / ".ao-lore" / "cache" / "torch"),
        }):
            registry = ParserRegistry()
            registry.register(DoclingPdfAdapter(manifest))
            parsed = ProductionParser(registry).parse_bytes(
                (ROOT / "tests/fixtures/ao_lore/pdf/simple-structure.pdf").read_bytes(),
                "fixtures/simple-structure.pdf",
                {
                    "media_type": "application/pdf",
                    "extension": ".pdf",
                    "required_structural_elements": ["headings", "source_coordinates"],
                    "canonical_ir_version": "ao.lore.document-ir.v0.1",
                    "network_allowed": False,
                    "license_allowlist": ["Apache-2.0"],
                    "sandbox_required": True,
                },
                {
                    "profile_id": "pdf-docling-v1",
                    "weights": {
                        "structural_fidelity": 0.4,
                        "text_fidelity": 0.4,
                        "source_location_fidelity": 0.2,
                    },
                    "penalty_weights": {},
                    "normalization_bounds": {},
                    "missing_optional_policy": "ineligible",
                    "tie_break_order": ["required_feature_coverage", "structural_fidelity", "parser_id"],
                    "fixture_corpus_digest": manifest["fixture_corpus_digest"],
                },
                {
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
                        "invalid_ir", "source_digest_mismatch", "parser_crash",
                        "no_readable_content", "lost_provenance",
                    ],
                    "calibration_result_digest": manifest["result_digest"],
                    "intermediate_decision": "quarantine",
                    "exhausted_decision": "reject",
                },
            )
        self.assertEqual(parsed["decision"], "accept")
        distilled = distill_document_ir(parsed["document_ir"], DeterministicDistiller())
        provenance = build_candidate_provenance(
            parsed, distilled, created_at="2026-08-09T14:00:00Z"
        )
        with tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates") as name:
            candidate_root = Path(name)
            persist_candidate(distilled, provenance, candidate_root=candidate_root)
            append_review(
                distilled["candidate"]["candidate_id"], "accept", "test-reviewer",
                candidate_root=candidate_root, recorded_at="2026-08-09T14:00:00Z",
            )
            inspection = inspect_candidate(
                distilled["candidate"]["candidate_id"], candidate_root=candidate_root
            )
        self.assertEqual(inspection["review_status"], "accepted")
        self.assertFalse(inspection["canonical"])
        self.assertFalse(inspection["promotion_authority"])
        self.assertEqual(brain_snapshot(), before)

    def test_markdown_parser_to_ir_to_noncanonical_candidate_preserves_brain(self):
        before = brain_snapshot()
        registry = ParserRegistry()
        registry.register(NativeMarkdownAdapter())
        parsed = ProductionParser(registry).parse_bytes(
            b"# Evidence Policy\n\nUse reviewed evidence and cite the source.\n",
            "sources/integration-policy.md",
            {
                "media_type": "text/markdown",
                "extension": ".md",
                "required_structural_elements": ["headings", "links"],
                "canonical_ir_version": "ao.lore.document-ir.v0.1",
                "network_allowed": False,
                "license_allowlist": ["Apache-2.0"],
                "sandbox_required": True,
            },
            selection_profile(),
            quality_profile(),
        )
        self.assertEqual(parsed["decision"], "accept")
        self.assertEqual(parsed["selected_parser"], "native-markdown")
        self.assertEqual(parsed["selection_report"]["benchmark_evidence"], [NATIVE_MARKDOWN_BENCHMARK])
        self.assertEqual(parsed["document_ir"]["schema_version"], "ao.lore.document-ir.v0.1")
        self.assertNotIn("concepts", parsed["document_ir"])

        distilled = distill_document_ir(parsed["document_ir"], DeterministicDistiller())
        candidate = distilled["candidate"]
        self.assertEqual(candidate["document_ir_digest"], canonical_digest(parsed["document_ir"]))
        self.assertFalse(candidate["canonical"])
        self.assertFalse(candidate["promotion_authority"])
        self.assertEqual(brain_snapshot(), before)

    def test_coverage_ledger_to_synthesizer_answers_early_and_depth_exhaustion_stays_partial(self):
        policy_ref = "brain/policies/evidence.md"
        procedure_ref = "brain/playbooks/review.md"
        navigator = CoverageNavigator(requirements(), coverage_profile())
        report = navigator.visit(
            "brain/index.md",
            [evidence("policy", policy_ref), evidence("procedure", procedure_ref)],
            estimated_tokens=25,
            estimated_seconds=1,
        )
        entries = [
            ledger_entry("policy", policy_ref, "Reviewed policy requires cited evidence."),
            ledger_entry("procedure", procedure_ref, "The review procedure preserves provenance."),
        ]
        result = synthesize_from_evidence(
            "What policy and procedure apply?",
            entries,
            report,
            "text",
            {"required": True},
            {"include_limitations": True},
            DeterministicSynthesizer(),
        )
        self.assertEqual(report["decision"], "answer")
        self.assertEqual(result["status"], "answer")
        self.assertEqual(result["claim_ids"], ["claim-policy", "claim-procedure"])
        self.assertEqual(navigator.trace()["visited_nodes"], ["brain/index.md"])
        self.assertGreater(navigator.trace()["remaining_budgets"]["depth"], 0)

        exhausted = CoverageNavigator(requirements(max_depth=1), coverage_profile())
        partial_report = exhausted.visit(
            procedure_ref,
            [evidence("procedure", procedure_ref)],
            estimated_tokens=10,
            estimated_seconds=1,
        )
        partial = synthesize_from_evidence(
            "What policy and procedure apply?",
            [entries[1]],
            partial_report,
            "text",
            {"required": True},
            {},
            DeterministicSynthesizer(),
        )
        self.assertEqual(partial_report["decision"], "partial")
        self.assertFalse(exhausted.should_stop)
        self.assertEqual(partial["status"], "partial")
        self.assertNotEqual(partial["status"], "answer")

    def test_four_independent_offline_roles_emit_separate_traces_without_fallback(self):
        configs = {role: role_config(role) for role in ROLES}

        def adapter(role: str) -> ScriptedRoleAdapter:
            return ScriptedRoleAdapter(
                role,
                f"{role}-scripted",
                lambda payload, config: RoleResult(
                    output={"schema_version": config["output_schema_version"], "role": config["role"]},
                    tokens=1,
                    latency_ms=1,
                    monetary_cost=0,
                ),
            )

        runtime = RoleRuntime(configs, [adapter(role) for role in ROLES])
        traces = [runtime.execute(role, role_input(role), now_seconds=1)["trace"] for role in ROLES]
        self.assertEqual([trace["role"] for trace in traces], list(ROLES))
        self.assertEqual(len({trace["model_id"] for trace in traces}), 4)
        self.assertTrue(all(trace["fallback"]["used"] is False for trace in traces))
        self.assertTrue(all(trace["private_reasoning_persisted"] is False for trace in traces))
        self.assertTrue(all(configs[role]["network_policy"] == "offline" for role in ROLES))
        self.assertTrue(all(configs[role]["credential_environment_variable"] is None for role in ROLES))


@private_calibration
class PrivateDocxSandboxIntegrationTests(unittest.TestCase):
    def test_actual_four_document_worker_lifecycle_is_one_then_three_then_zero(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        spec = importlib.util.spec_from_file_location(
            "ao_lore_docx_integration_generator",
            ROOT / "tests/fixtures/ao_lore/docx/generate.py",
        )
        generator = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(generator)
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "working")
        root = Path(temporary.name)
        runtime = root / "runtime"
        reviewed = root / "reviewed"
        reviewed.mkdir()
        review = {
            "schema_version": "ao.lore.private-docx-domain-review.v0.1",
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "transformation_id": DOCX_TRANSFORMATION_ID,
            "documents": [],
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        prepared = None
        process = None
        for index in range(1, 5):
            canonical = generator.build_docx_fixture(
                paragraphs=({"text": f"Integration worker {index}", "style": "Heading1"},)
            )
            name = f"{index:04d}-integration.docx"
            (reviewed / name).write_bytes(b"\x00\x00\x00\x00" + canonical[4:])
            review["documents"].append(
                {"item_id": f"docx-domain-{index:02d}", "source_file": name}
            )
        try:
            prepare_private_docx_corpus(
                review, source_root=reviewed, runtime_root=runtime
            )
            prepared = prepare_private_docx_uat_run(runtime_root=runtime)
            qualification = docx_batch_qualification(prepared.expectation_digest)
            (runtime / "private-docx/qualification.json").write_text(
                json.dumps(qualification, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            process = _launch_private_docx_worker(
                prepared,
                "interrupted",
                qualification=qualification,
                runtime_root=runtime,
            )
            state = runtime / "uat/private-docx" / prepared.batch_manifest["batch_id"]
            barrier_path = state / "barrier-ready.json"
            deadline = time.monotonic() + 10.0
            while not barrier_path.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(barrier_path.exists())
            checkpoint = json.loads(
                (runtime / "batches" / prepared.batch_manifest["batch_id"] / "checkpoint.json").read_text()
            )
            barrier = json.loads(barrier_path.read_text())
            journal = state / "conversion-journal.jsonl"
            self.assertEqual((1, 1, 1), (checkpoint["processed"], len(journal.read_bytes().splitlines()), barrier["journal_count"]))
            _signal_private_docx_worker(process)
            interrupted = _wait_private_docx_worker(process)
            self.assertIn(interrupted["returncode"], {-signal.SIGTERM, 128 + signal.SIGTERM})

            process = _launch_private_docx_worker(
                prepared, "resume", qualification=qualification, runtime_root=runtime
            )
            resumed_result = _wait_private_docx_worker(process)
            self.assertEqual(0, resumed_result["returncode"], resumed_result["stderr"])
            self.assertEqual(4, len(journal.read_bytes().splitlines()))

            process = _launch_private_docx_worker(
                prepared, "rerun", qualification=qualification, runtime_root=runtime
            )
            rerun_result = _wait_private_docx_worker(process)
            self.assertEqual(0, rerun_result["returncode"], rerun_result["stderr"])
            self.assertEqual(4, len(journal.read_bytes().splitlines()))
            self.assertEqual(json.loads(resumed_result["stdout"]), json.loads(rerun_result["stdout"]))
        finally:
            if process is not None:
                _terminate_worker(process)
            if prepared is not None:
                shutil.rmtree(ROOT / "sources" / prepared.staging_name, ignore_errors=True)
                shutil.rmtree(
                    ROOT / "working/candidates" / prepared.staging_name,
                    ignore_errors=True,
                )
                shutil.rmtree(
                    ROOT / "sources" / ("." + prepared.staging_name),
                    ignore_errors=True,
                )
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
