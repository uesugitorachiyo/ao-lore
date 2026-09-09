import copy
import hashlib
import json
import os
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import ao_lore.batch_ingestion as batch_ingestion
from ao_lore.batch_ingestion import (
    BatchDependencies,
    BatchIngestionError,
    append_terminal_item,
    ingest_batch,
    ingest_verified_batch,
    load_batch_manifest,
    load_final_batch_readback,
    load_or_initialize_checkpoint,
    validate_batch_manifest,
    validate_checkpoint,
    validate_final_batch_readback,
)
from ao_lore.benchmark import BenchmarkError, build_manifest, canonical_digest
from ao_lore.candidates import CandidateError
from ao_lore.docling_pdf import configuration_digest
from ao_lore.docx_ooxml import (
    DOCX_MIME,
    DOCX_PARSER_ID,
    DOCX_PARSER_VERSION,
    DocxLimits,
    docx_configuration_digest,
)
from ao_lore.distillation import DistillationError
from ao_lore.home import repository_root
from ao_lore.ingestion import (
    DocxProvenanceOrigin,
    IngestionError,
    ingest_docx_document,
    read_verified_docx_source,
)
from ao_lore.knowledge_contracts import citations_digest, claims_digest
from ao_lore.parsing import ParsingError
from ao_lore.private_docx_domain import (
    DOCX_PRIVATE_CORPUS_ID,
    DOCX_TRANSFORMATION_ID,
    validate_docx_expectation,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
QUALIFIED_CORPUS_DIGEST = "sha256:0a4a255050de73593319d61152470fba3c71bd7a82bf132272e05d6b77b66f5e"
DOCX_FIXTURE = (
    repository_root()
    / "tests"
    / "fixtures"
    / "ao_lore"
    / "docx"
    / "minimal-paragraph.docx"
)


def qualified_benchmark(corpus_digest=QUALIFIED_CORPUS_DIGEST):
    return build_manifest(
        fixture_corpus_digest=corpus_digest,
        parser_id="docling",
        parser_version="2.118.1",
        parser_configuration_digest=configuration_digest(
            50 * 1024 * 1024, 500, 100_000
        ),
        runtime="python-3.12",
        platform="linux-x86_64",
        document_ir_version="ao.lore.document-ir.v0.1",
        commands=[["ao-lore", "benchmark", "pdf"]],
        raw_metrics={
            "structural_fidelity": 0.9,
            "text_fidelity": 0.9,
            "source_location_fidelity": 0.9,
            "reliability": 1.0,
            "deterministic_repeatability": 1.0,
        },
        normalized_scores={
            "structural_fidelity": 0.9,
            "text_fidelity": 0.9,
            "source_location_fidelity": 0.9,
            "reliability": 1.0,
            "deterministic_repeatability": 1.0,
        },
        failures=[],
        exclusions=[],
    )


def manifest(documents=None, batch_id="batch-guides-01"):
    return {
        "schema_version": "ao.lore.ingest-batch-manifest.v0.1",
        "batch_id": batch_id,
        "documents": documents or [{
            "item_id": "guide",
            "source": "sources/guides/guide.pdf",
            "source_digest": DIGEST_A,
        }],
        "continue_on_error": True,
    }


def docx_manifest(documents=None, batch_id="batch-guides-01"):
    return {
        "schema_version": "ao.lore.ingest-batch-manifest.v0.2",
        "batch_id": batch_id,
        "format_id": "docx",
        "media_type": DOCX_MIME,
        "parser_id": "native-docx-ooxml",
        "parser_version": "1.0.0",
        "documents": documents or [{
            "item_id": "guide",
            "source": "sources/guides/guide.docx",
            "source_digest": DIGEST_A,
        }],
        "continue_on_error": True,
    }


def docx_batch_qualification(fixture_corpus_digest):
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


def docx_expectation(derived_digest, *, source_digest="sha256:" + "8" * 64):
    items = []
    for index in range(1, 101):
        items.append(
            {
                "item_id": f"docx-{index:04d}",
                "source_digest": (
                    source_digest if index == 1 else f"sha256:{index:064x}"
                ),
                "derived_digest": (
                    derived_digest if index == 1 else f"sha256:{index + 1000:064x}"
                ),
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


def success(item_id="guide", source_digest=DIGEST_A):
    return {
        "item_id": item_id,
        "source_digest": source_digest,
        "status": "created",
        "candidate_id": "candidate-guide",
        "candidate_digest": DIGEST_B,
        "provenance_digest": DIGEST_C,
        "review_status": "unreviewed",
    }


def canonical_bytes(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def verified_candidate(item=None, *, parser_id="docling", parser_version="2.118.1"):
    item = item or success()
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.1",
        "candidate_id": item["candidate_id"],
        "document_ir_digest": DIGEST_A,
        "concepts": [],
        "claim_mappings": [],
        "links": [],
        "contradiction_warnings": [],
        "canonical": False,
        "promotion_authority": False,
    }
    item["candidate_digest"] = canonical_digest(candidate)
    provenance = {
        "schema_version": "ao.lore.candidate-provenance.v0.1",
        "candidate_id": item["candidate_id"],
        "candidate_digest": item["candidate_digest"],
        "document_ir_digest": DIGEST_A,
        "source_digest": item["source_digest"],
        "parser_id": parser_id,
        "parser_version": parser_version,
        "parse_quality_report_digest": DIGEST_A,
        "parser_selection_report_digest": DIGEST_B,
        "distillation_trace_digest": DIGEST_C,
        "created_at": "2026-08-09T00:00:00Z",
    }
    item["provenance_digest"] = canonical_digest(provenance)
    return {
        "candidate": candidate,
        "provenance": provenance,
        "inspection": {
            "schema_version": "ao.lore.candidate-inspection.v0.1",
            "candidate_id": item["candidate_id"],
            "candidate_digest": item["candidate_digest"],
            "provenance_digest": item["provenance_digest"],
            "verified_review_events": 0,
            "review_status": item["review_status"],
            "latest_event_digest": None,
            "canonical": False,
            "promotion_authority": False,
        },
    }


def single_readback(
    item_id="guide",
    source_digest=DIGEST_A,
    *,
    status="created",
    parser_id="docling",
    parser_version="2.118.1",
):
    candidate_id = f"candidate-{item_id}"
    return {
        "schema_version": "ao.lore.single-document-ingest-readback.v0.1",
        "status": status,
        "candidate_id": candidate_id,
        "candidate_digest": DIGEST_B,
        "provenance_digest": DIGEST_C,
        "source_digest": source_digest,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "document_ir_digest": DIGEST_A,
        "parser_selection_report_digest": DIGEST_B,
        "parse_quality_report_digest": DIGEST_C,
        "distillation_trace_digest": DIGEST_A,
        "review_status": "unreviewed",
        "next_commands": [
            f"ao-lore candidate inspect --candidate-id {candidate_id}",
            f"ao-lore candidate review --candidate-id {candidate_id} --decision accept --reviewer <reviewer-id>",
        ],
        "canonical": False,
        "promotion_authority": False,
    }


def candidate_for_readback(readback):
    terminal = {
        "item_id": readback["candidate_id"].removeprefix("candidate-"),
        "source_digest": readback["source_digest"],
        "status": readback["status"],
        "candidate_id": readback["candidate_id"],
        "candidate_digest": readback["candidate_digest"],
        "provenance_digest": readback["provenance_digest"],
        "review_status": readback["review_status"],
    }
    loaded = verified_candidate(
        terminal,
        parser_id=readback["parser_id"],
        parser_version=readback["parser_version"],
    )
    readback["candidate_digest"] = terminal["candidate_digest"]
    readback["provenance_digest"] = terminal["provenance_digest"]
    return loaded


class BatchCase(unittest.TestCase):
    def setUp(self):
        base = repository_root() / "working"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="batch-test-", dir=base)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()


class BatchManifestTests(BatchCase):
    def test_v0_1_pdf_manifest_has_stable_canonical_bytes(self):
        self.assertEqual(
            b'{"batch_id":"batch-guides-01","continue_on_error":true,"documents":[{"item_id":"guide","source":"sources/guides/guide.pdf","source_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],"schema_version":"ao.lore.ingest-batch-manifest.v0.1"}',
            canonical_bytes(validate_batch_manifest(manifest())),
        )

    def test_valid_manifest_is_detached_and_duplicate_digest_is_allowed(self):
        value = manifest([
            {"item_id": "one", "source": "sources/one.pdf", "source_digest": DIGEST_A},
            {"item_id": "two", "source": "sources/two.pdf", "source_digest": DIGEST_A,
             "candidate_context": {
                 "schema_version": "ao.lore.candidate-comparison-context.v0.1",
                 "candidates": [{"candidate_id": "candidate-old", "candidate_digest": DIGEST_B}],
             }},
        ])
        validated = validate_batch_manifest(value)
        value["documents"][0]["item_id"] = "changed"
        self.assertEqual("one", validated["documents"][0]["item_id"])

    def test_manifest_rejects_invalid_shape_paths_and_keyed_duplicates(self):
        invalid = []
        for source in ("/sources/a.pdf", r"sources\a.pdf", "sources/../a.pdf",
                       "sources/./a.pdf", "sources//a.pdf", "sources/a.txt"):
            value = manifest(); value["documents"][0]["source"] = source; invalid.append(value)
        value = manifest(); value["continue_on_error"] = 1; invalid.append(value)
        value = manifest(); value["extra"] = True; invalid.append(value)
        value = manifest(); value["documents"] = []; invalid.append(value)
        value = manifest(); value["documents"][0]["source_digest"] = "a" * 64; invalid.append(value)
        value = manifest(); value["documents"].append({"item_id": "guide", "source": "sources/b.pdf", "source_digest": DIGEST_B}); invalid.append(value)
        value = manifest(); value["documents"].append({"item_id": "other", "source": "sources/guides/guide.pdf", "source_digest": DIGEST_B}); invalid.append(value)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(BatchIngestionError):
                validate_batch_manifest(value)

    def test_docx_manifest_requires_exact_v0_2_binding(self):
        value = docx_manifest([
            {
                "item_id": "one",
                "source": "sources/one.docx",
                "source_digest": DIGEST_A,
            },
            {
                "item_id": "two",
                "source": "sources/two.docx",
                "source_digest": DIGEST_B,
                "candidate_context": {
                    "schema_version": "ao.lore.candidate-comparison-context.v0.1",
                    "candidates": [],
                },
            },
        ])
        validated = validate_batch_manifest(value)
        value["documents"][0]["item_id"] = "changed"
        self.assertEqual("one", validated["documents"][0]["item_id"])

        invalid = []
        for field, replacement in (
            ("format_id", "pdf"),
            ("media_type", "application/pdf"),
            ("parser_id", "docling"),
            ("parser_version", "2.118.1"),
            ("continue_on_error", False),
        ):
            bad = docx_manifest()
            bad[field] = replacement
            invalid.append(bad)
        for source in (
            "sources/guides/guide.pdf",
            "/sources/guides/guide.docx",
            r"sources\guides\guide.docx",
            "sources/../guide.docx",
            "sources/./guide.docx",
            "sources//guide.docx",
        ):
            bad = docx_manifest()
            bad["documents"][0]["source"] = source
            invalid.append(bad)
        bad = docx_manifest()
        bad["schema_version"] = "ao.lore.ingest-batch-manifest.v0.1"
        invalid.append(bad)
        bad = docx_manifest()
        bad["documents"].append(
            {
                "item_id": "other",
                "source": "sources/guides/other.pdf",
                "source_digest": DIGEST_B,
            }
        )
        invalid.append(bad)
        bad = docx_manifest()
        bad["unknown"] = True
        invalid.append(bad)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(BatchIngestionError):
                validate_batch_manifest(value)

    def test_candidate_context_is_closed_bounded_and_unique(self):
        context = {"schema_version": "ao.lore.candidate-comparison-context.v0.1", "candidates": []}
        value = manifest(); value["documents"][0]["candidate_context"] = context
        self.assertEqual(context, validate_batch_manifest(value)["documents"][0]["candidate_context"])
        for mutation in (
            {"extra": 1},
            {"candidates": [{"candidate_id": "wrong", "candidate_digest": DIGEST_A}]},
            {"candidates": [{"candidate_id": "candidate-a", "candidate_digest": DIGEST_A}] * 2},
        ):
            bad = manifest(); bad_context = copy.deepcopy(context); bad_context.update(mutation)
            bad["documents"][0]["candidate_context"] = bad_context
            with self.assertRaises(BatchIngestionError): validate_batch_manifest(bad)

    def test_pdf_items_accept_only_explicit_closed_knowledge_policy(self):
        valid_policies = (
            {"sensitivity": "public", "stale_after": None},
            {"sensitivity": "internal", "stale_after": "2027-01-01T00:00:00Z"},
            {"sensitivity": "restricted", "stale_after": None},
        )
        for policy in valid_policies:
            with self.subTest(policy=policy):
                value = manifest()
                value["documents"][0]["knowledge_policy"] = policy
                validated = validate_batch_manifest(value)
                self.assertEqual(policy, validated["documents"][0]["knowledge_policy"])
                policy["sensitivity"] = "changed"
                self.assertNotEqual(policy, validated["documents"][0]["knowledge_policy"])

        invalid_policies = (
            {},
            {"sensitivity": "public"},
            {"sensitivity": "public", "stale_after": None, "extra": True},
            {"sensitivity": "secret", "stale_after": None},
            {"sensitivity": "public", "stale_after": "2027-01-01T00:00:00+00:00"},
            {"sensitivity": "public", "stale_after": "not-a-timestampZ"},
            {"sensitivity": "public", "stale_after": "2" * 32 + "Z"},
        )
        for policy in invalid_policies:
            with self.subTest(policy=policy), self.assertRaises(BatchIngestionError):
                value = manifest()
                value["documents"][0]["knowledge_policy"] = policy
                validate_batch_manifest(value)

    def test_non_pdf_manifests_do_not_accept_knowledge_policy(self):
        value = docx_manifest()
        value["documents"][0]["knowledge_policy"] = {
            "sensitivity": "public", "stale_after": None,
        }
        with self.assertRaises(BatchIngestionError):
            validate_batch_manifest(value)

    def test_loader_is_strict_contained_bounded_and_path_free(self):
        path = self.root / "manifest.json"
        path.write_text(json.dumps(manifest()), encoding="utf-8")
        self.assertEqual("batch-guides-01", load_batch_manifest(path)[0]["batch_id"])
        path.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
        with self.assertRaises(BatchIngestionError) as caught: load_batch_manifest(path)
        self.assertNotIn(str(path), str(caught.exception))
        path.write_text('{"x":NaN}', encoding="utf-8")
        with self.assertRaises(BatchIngestionError): load_batch_manifest(path)
        path.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaises(BatchIngestionError): load_batch_manifest(path)
        outside = Path(tempfile.gettempdir()) / "ao-lore-outside-manifest.json"
        outside.write_text(json.dumps(manifest()), encoding="utf-8")
        try:
            with self.assertRaises(BatchIngestionError): load_batch_manifest(outside)
        finally: outside.unlink()
        path.write_text(
            '{"schema_version":"ao.lore.ingest-batch-manifest.v0.2","schema_version":"ao.lore.ingest-batch-manifest.v0.2"}',
            encoding="utf-8",
        )
        with self.assertRaises(BatchIngestionError): load_batch_manifest(path)
        target = self.root / "real.json"; target.write_text(json.dumps(manifest()), encoding="utf-8")
        link = self.root / "link.json"; link.symlink_to(target)
        with self.assertRaises(BatchIngestionError): load_batch_manifest(link)
        directory = self.root / "directory.json"; directory.mkdir()
        with self.assertRaises(BatchIngestionError): load_batch_manifest(directory)

    def test_manifest_ancestor_substitution_is_rejected(self):
        ancestor = self.root / "manifest-parent"
        ancestor.mkdir()
        target = ancestor / "manifest.json"
        target.write_text(json.dumps(manifest()), encoding="utf-8")
        detached = self.root / "manifest-parent-detached"
        outside = Path(tempfile.mkdtemp(prefix="batch-manifest-outside-"))
        os.link(target, outside / "manifest.json")
        original_open = os.open
        swapped = False

        def swap_then_open(path, flags, *args, **kwargs):
            nonlocal swapped
            dir_fd = kwargs.get("dir_fd")
            path_text = os.fspath(path)
            old_path_open = dir_fd is None and Path(path_text) == target
            anchored_ancestor_open = (
                dir_fd is not None and path_text == ancestor.name
            )
            if not swapped and (old_path_open or anchored_ancestor_open):
                os.rename(ancestor, detached)
                ancestor.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        try:
            with patch("ao_lore._strict_io.os.open", side_effect=swap_then_open):
                with self.assertRaises(BatchIngestionError):
                    ingest_batch(
                        target,
                        dependencies=BatchDependencies(
                            benchmark_manifest={"result_digest": DIGEST_A},
                            selection_profile={"profile_id": "selection"},
                            quality_profile={"profile_id": "quality"},
                            ingest_one=lambda *args, **kwargs: self.fail("ingest"),
                            inspect_candidate=lambda _: self.fail("inspect"),
                            read_source=lambda *args: self.fail("source read"),
                        ),
                        batch_root=self.root / "manifest-state",
                    )
            self.assertTrue(swapped)
            self.assertFalse((self.root / "manifest-state").exists())
        finally:
            if swapped:
                ancestor.unlink()
                os.rename(detached, ancestor)
            shutil.rmtree(outside)


class CheckpointTests(BatchCase):
    def _init(self, value=None, loader=None):
        value = validate_batch_manifest(value or manifest())
        digest = canonical_digest(value)
        return value, digest, load_or_initialize_checkpoint(
            value, digest, batch_root=self.root,
            inspect_candidate_fn=loader or (lambda _: self.fail("unexpected candidate load")),
        )

    def test_initial_checkpoint_and_reopen_are_exact(self):
        value, digest, checkpoint = self._init()
        expected = {
            "schema_version": "ao.lore.ingest-batch-checkpoint.v0.1",
            "batch_id": value["batch_id"], "manifest_digest": digest,
            "manifest_items": [{"item_id": "guide", "source_digest": DIGEST_A}],
            "items": [], "created": 0, "unchanged": 0, "rejected": 0,
            "processed": 0, "next_item_index": 0,
            "previous_checkpoint_digest": None,
            "canonical": False, "promotion_authority": False,
        }
        self.assertEqual(canonical_digest(expected), checkpoint["checkpoint_digest"])
        self.assertEqual(checkpoint, load_or_initialize_checkpoint(value, digest, batch_root=self.root))
        self.assertEqual(
            b'{"batch_id":"batch-guides-01","canonical":false,"checkpoint_digest":"sha256:f6250bc95bcfd9aa8ad8719e4ffe0d4eed1004a62858237389e38d75864d371b","created":0,"items":[],"manifest_digest":"sha256:f0b367b845e16a45349271c8e91f8898a8dc9625e65054d7f25c1382fb5475bc","manifest_items":[{"item_id":"guide","source_digest":"sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}],"next_item_index":0,"previous_checkpoint_digest":null,"processed":0,"promotion_authority":false,"rejected":0,"schema_version":"ao.lore.ingest-batch-checkpoint.v0.1","unchanged":0}',
            canonical_bytes(checkpoint),
        )

    def test_append_transition_and_verified_candidate_reopen_once(self):
        value, digest, checkpoint = self._init()
        item = success(); candidate = verified_candidate(item); calls = []
        updated = append_terminal_item(
            checkpoint, item, manifest=value, manifest_digest=digest,
            batch_root=self.root, inspect_candidate_fn=lambda cid: calls.append(cid) or candidate,
        )
        self.assertEqual(checkpoint["checkpoint_digest"], updated["previous_checkpoint_digest"])
        self.assertEqual((1, 1, 1), (updated["created"], updated["processed"], updated["next_item_index"]))
        self.assertEqual(["candidate-guide"], calls)
        calls.clear()
        self.assertEqual(updated, load_or_initialize_checkpoint(
            value, digest, batch_root=self.root,
            inspect_candidate_fn=lambda cid: calls.append(cid) or candidate,
        ))
        self.assertEqual(["candidate-guide"], calls)

    def test_rejected_transition_does_not_load_candidate(self):
        value, digest, checkpoint = self._init()
        rejected = {"item_id": "guide", "source_digest": DIGEST_A,
                    "status": "rejected", "error_code": "parser_rejected"}
        updated = append_terminal_item(checkpoint, rejected, manifest=value,
            manifest_digest=digest, batch_root=self.root,
            inspect_candidate_fn=lambda _: self.fail("candidate loaded"))
        self.assertEqual(1, updated["rejected"])

    def test_validator_rejects_corrupt_identity_order_arithmetic_chain_authority_union(self):
        value, digest, checkpoint = self._init()
        corruptions = []
        for field, replacement in (("batch_id", "batch-other"), ("manifest_digest", DIGEST_A),
                ("created", 1), ("processed", 1), ("next_item_index", 1),
                ("previous_checkpoint_digest", DIGEST_A), ("canonical", True),
                ("promotion_authority", True), ("checkpoint_digest", DIGEST_A)):
            bad = copy.deepcopy(checkpoint); bad[field] = replacement; corruptions.append(bad)
        bad = copy.deepcopy(checkpoint); bad["manifest_items"][0]["item_id"] = "other"; corruptions.append(bad)
        bad = copy.deepcopy(checkpoint); bad["items"] = [{"item_id": "guide", "source_digest": DIGEST_A, "status": "rejected", "error_code": "bad"}]; corruptions.append(bad)
        for bad in corruptions:
            with self.assertRaises(BatchIngestionError): validate_checkpoint(bad, value, digest)

    def test_reconstructed_chain_rejects_reshaped_previous_digest(self):
        value, digest, checkpoint = self._init()
        rejected = {"item_id": "guide", "source_digest": DIGEST_A,
                    "status": "rejected", "error_code": "document_rejected"}
        processed = append_terminal_item(
            checkpoint, rejected, manifest=value, manifest_digest=digest,
            batch_root=self.root,
        )
        forged = copy.deepcopy(processed)
        forged["previous_checkpoint_digest"] = DIGEST_A
        forged["checkpoint_digest"] = canonical_digest({
            key: child for key, child in forged.items()
            if key != "checkpoint_digest"
        })

        with self.assertRaises(BatchIngestionError):
            validate_checkpoint(forged, value, digest)

        (self.root / "checkpoint.json").write_text(
            json.dumps(forged), encoding="utf-8"
        )
        with self.assertRaises(BatchIngestionError):
            load_or_initialize_checkpoint(value, digest, batch_root=self.root)

    def test_reconstructed_chain_binds_multi_item_prefix(self):
        value = manifest([
            {"item_id": "one", "source": "sources/one.pdf", "source_digest": DIGEST_A},
            {"item_id": "two", "source": "sources/two.pdf", "source_digest": DIGEST_B},
        ])
        value, digest, initial = self._init(value)
        first = append_terminal_item(
            initial,
            {"item_id": "one", "source_digest": DIGEST_A,
             "status": "rejected", "error_code": "parser_rejected"},
            manifest=value, manifest_digest=digest, batch_root=self.root,
        )
        second = append_terminal_item(
            first,
            {"item_id": "two", "source_digest": DIGEST_B,
             "status": "rejected", "error_code": "document_rejected"},
            manifest=value, manifest_digest=digest, batch_root=self.root,
        )
        forged = copy.deepcopy(second)
        forged["previous_checkpoint_digest"] = initial["checkpoint_digest"]
        forged["checkpoint_digest"] = canonical_digest({
            key: child for key, child in forged.items()
            if key != "checkpoint_digest"
        })

        with self.assertRaises(BatchIngestionError):
            validate_checkpoint(forged, value, digest)

    def test_reopen_rejects_candidate_binding_drift(self):
        value, digest, checkpoint = self._init()
        item = success(); candidate = verified_candidate(item)
        append_terminal_item(checkpoint, item, manifest=value, manifest_digest=digest,
            batch_root=self.root, inspect_candidate_fn=lambda _: candidate)
        for field in ("candidate_digest", "source_digest", "review_status"):
            drift = copy.deepcopy(candidate)
            target = drift["provenance"] if field == "source_digest" else drift["inspection"]
            target[field] = (
                DIGEST_B if field == "source_digest" else
                DIGEST_A if field == "candidate_digest" else
                "accepted"
            )
            with self.assertRaises(BatchIngestionError): load_or_initialize_checkpoint(
                value, digest, batch_root=self.root, inspect_candidate_fn=lambda _, d=drift: d)

    def test_reopen_rejects_forged_or_authority_widened_candidate_loader_output(self):
        value, digest, checkpoint = self._init()
        item = success(); candidate = verified_candidate(item)
        append_terminal_item(
            checkpoint, item, manifest=value, manifest_digest=digest,
            batch_root=self.root, inspect_candidate_fn=lambda _: candidate,
        )
        forged_values = []
        for section, field, replacement in (
            ("candidate", "canonical", True),
            ("candidate", "unknown", False),
            ("provenance", "parser_version", "forged"),
            ("inspection", "promotion_authority", True),
            ("inspection", "verified_review_events", True),
        ):
            forged = copy.deepcopy(candidate)
            forged[section][field] = replacement
            forged_values.append(forged)
        for forged in forged_values:
            with self.subTest(forged=forged):
                with self.assertRaises(BatchIngestionError):
                    load_or_initialize_checkpoint(
                        value, digest, batch_root=self.root,
                        inspect_candidate_fn=lambda _, f=forged: f,
                    )

    def test_batch_id_reuse_out_of_order_terminal_and_stale_append_fail(self):
        value, digest, checkpoint = self._init()
        changed = manifest(); changed["documents"][0]["source_digest"] = DIGEST_B
        changed = validate_batch_manifest(changed)
        with self.assertRaises(BatchIngestionError): load_or_initialize_checkpoint(
            changed, canonical_digest(changed), batch_root=self.root)
        with self.assertRaises(BatchIngestionError): append_terminal_item(
            checkpoint, success("wrong"), manifest=value, manifest_digest=digest, batch_root=self.root)
        rejected = {"item_id": "guide", "source_digest": DIGEST_A,
                    "status": "rejected", "error_code": "document_rejected"}
        append_terminal_item(checkpoint, rejected, manifest=value, manifest_digest=digest, batch_root=self.root)
        with self.assertRaises(BatchIngestionError): append_terminal_item(
            checkpoint, rejected, manifest=value, manifest_digest=digest, batch_root=self.root)

    def test_unknown_entries_and_symlink_state_fail_closed(self):
        value, digest, _ = self._init()
        batch_dir = self.root
        (batch_dir / "unexpected").write_text("x", encoding="utf-8")
        with self.assertRaises(BatchIngestionError): load_or_initialize_checkpoint(value, digest, batch_root=self.root)
        (batch_dir / "unexpected").unlink()
        checkpoint_path = batch_dir / "checkpoint.json"
        real = batch_dir / "real.json"; checkpoint_path.replace(real); checkpoint_path.symlink_to(real)
        with self.assertRaises(BatchIngestionError): load_or_initialize_checkpoint(value, digest, batch_root=self.root)
        other = self.root / "other"; other.mkdir(); symlink_root = self.root / "linked"; symlink_root.symlink_to(other, target_is_directory=True)
        with self.assertRaises(BatchIngestionError): load_or_initialize_checkpoint(value, digest, batch_root=symlink_root)

    def test_concurrent_initialization_converges(self):
        value = validate_batch_manifest(manifest()); digest = canonical_digest(value)
        results = []; errors = []
        def run():
            try: results.append(load_or_initialize_checkpoint(value, digest, batch_root=self.root))
            except Exception as exc: errors.append(exc)
        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(8, len(results)); self.assertTrue(all(item == results[0] for item in results))

    def test_atomic_failure_leaves_prior_checkpoint(self):
        value, digest, checkpoint = self._init()
        rejected = {"item_id": "guide", "source_digest": DIGEST_A,
                    "status": "rejected", "error_code": "document_rejected"}
        with patch("ao_lore.batch_ingestion.os.replace", side_effect=OSError("boom")):
            with self.assertRaises(BatchIngestionError): append_terminal_item(
                checkpoint, rejected, manifest=value, manifest_digest=digest, batch_root=self.root)
        self.assertEqual(checkpoint, load_or_initialize_checkpoint(value, digest, batch_root=self.root))
        self.assertEqual({"checkpoint.json"}, {p.name for p in self.root.iterdir()})

    def test_owned_crash_temporary_is_recovered_but_unsafe_residue_fails(self):
        value, digest, checkpoint = self._init()
        owned = [
            self.root / (".checkpoint.json-" + "1" * 32 + ".tmp"),
            self.root / (".final-readback.json-" + "2" * 32 + ".tmp"),
        ]
        for path in owned:
            path.write_bytes(b"partial")
        self.assertEqual(
            checkpoint,
            load_or_initialize_checkpoint(value, digest, batch_root=self.root),
        )
        self.assertTrue(all(not path.exists() for path in owned))

        unsafe = self.root / (".final-readback.json-" + "3" * 32 + ".tmp")
        unsafe.symlink_to(self.root / "checkpoint.json")
        with self.assertRaises(BatchIngestionError):
            load_or_initialize_checkpoint(value, digest, batch_root=self.root)
        unsafe.unlink()
        (self.root / ".checkpoint.json-deadbeef.tmp").write_bytes(b"partial")
        with self.assertRaises(BatchIngestionError):
            load_or_initialize_checkpoint(value, digest, batch_root=self.root)

    def test_checkpoint_name_replacement_during_parse_fails_closed(self):
        value, digest, _ = self._init()
        checkpoint_path = self.root / "checkpoint.json"
        replacement = self.root.parent / (self.root.name + "-replacement.json")
        replacement.write_bytes(checkpoint_path.read_bytes())
        original = batch_ingestion.parse_strict_json
        replaced = False

        def replace_during_parse(body, label):
            nonlocal replaced
            parsed = original(body, label)
            if label == "batch checkpoint" and not replaced:
                os.replace(replacement, checkpoint_path)
                replaced = True
            return parsed

        with patch(
            "ao_lore.batch_ingestion.parse_strict_json",
            side_effect=replace_during_parse,
        ):
            with self.assertRaises(BatchIngestionError):
                load_or_initialize_checkpoint(value, digest, batch_root=self.root)
        self.assertTrue(replaced)
        replacement.unlink(missing_ok=True)

    def test_directory_swap_after_lock_fails_without_advancing_state(self):
        value, digest, checkpoint = self._init()
        original_bytes = (self.root / "checkpoint.json").read_bytes()
        substitute = Path(tempfile.mkdtemp(
            prefix="batch-substitute-", dir=self.root.parent
        ))
        load_or_initialize_checkpoint(value, digest, batch_root=substitute)
        substitute_bytes = (substitute / "checkpoint.json").read_bytes()
        detached = self.root.with_name(self.root.name + "-detached")
        original_validate = batch_ingestion._validate_batch_entries
        swapped = False

        def swap_after_lock(directory):
            nonlocal swapped
            if not swapped:
                os.rename(self.root, detached)
                os.rename(substitute, self.root)
                swapped = True
            return original_validate(directory)

        rejected = {"item_id": "guide", "source_digest": DIGEST_A,
                    "status": "rejected", "error_code": "document_rejected"}
        try:
            with patch(
                "ao_lore.batch_ingestion._validate_batch_entries",
                side_effect=swap_after_lock,
            ):
                with self.assertRaises(BatchIngestionError):
                    append_terminal_item(
                        checkpoint, rejected, manifest=value,
                        manifest_digest=digest, batch_root=self.root,
                    )
            self.assertEqual(
                substitute_bytes, (self.root / "checkpoint.json").read_bytes()
            )
            self.assertEqual(
                original_bytes, (detached / "checkpoint.json").read_bytes()
            )
        finally:
            if swapped:
                os.rename(self.root, substitute)
                os.rename(detached, self.root)
            shutil.rmtree(substitute, ignore_errors=True)

    def test_state_ancestor_substitution_cannot_escape_repository(self):
        value = validate_batch_manifest(manifest())
        digest = canonical_digest(value)
        ancestor = self.root / "state-parent"
        batch = ancestor / "batch"
        detached = self.root / "state-parent-detached"
        outside = Path(tempfile.mkdtemp(prefix="batch-state-outside-"))
        (outside / "batch").mkdir()
        original_ensure = batch_ingestion._ensure_real_directory
        swapped = False

        def ensure_then_swap(path, root):
            nonlocal swapped
            selected = original_ensure(path, root)
            if Path(path) == batch and not swapped:
                os.rename(ancestor, detached)
                ancestor.symlink_to(outside, target_is_directory=True)
                swapped = True
            return selected

        try:
            with patch(
                "ao_lore.batch_ingestion._ensure_real_directory",
                side_effect=ensure_then_swap,
            ):
                with self.assertRaises(BatchIngestionError):
                    load_or_initialize_checkpoint(
                        value, digest, batch_root=batch,
                        inspect_candidate_fn=lambda _: self.fail("candidate load"),
                    )
            self.assertTrue(swapped)
            self.assertFalse((outside / "batch" / "checkpoint.json").exists())
            self.assertFalse((detached / "batch" / "checkpoint.json").exists())
        finally:
            if swapped:
                ancestor.unlink()
                os.rename(detached, ancestor)
            shutil.rmtree(outside)

    def test_real_directory_clone_substitution_in_acquire_lock_gap_fails(self):
        value = validate_batch_manifest(manifest())
        digest = canonical_digest(value)
        ancestor = self.root / "handoff-parent"
        batch = ancestor / "batch"
        detached = self.root / "handoff-parent-detached"
        replacement = self.root / "handoff-replacement"
        replacement_batch = replacement / "batch"
        checkpoint = load_or_initialize_checkpoint(
            value, digest, batch_root=batch,
            inspect_candidate_fn=lambda _: self.fail("candidate load"),
        )
        replacement_batch.mkdir(parents=True)
        original_bytes = (batch / "checkpoint.json").read_bytes()
        (replacement_batch / "checkpoint.json").write_bytes(original_bytes)
        original_ensure = batch_ingestion._ensure_real_directory
        swapped = False

        def ensure_then_replace(path, root):
            nonlocal swapped
            acquired = original_ensure(path, root)
            if Path(path) == batch and not swapped:
                os.rename(ancestor, detached)
                os.rename(replacement, ancestor)
                swapped = True
            return acquired

        rejected = {
            "item_id": "guide", "source_digest": DIGEST_A,
            "status": "rejected", "error_code": "document_rejected",
        }
        try:
            with patch(
                "ao_lore.batch_ingestion._ensure_real_directory",
                side_effect=ensure_then_replace,
            ):
                with self.assertRaises(BatchIngestionError):
                    append_terminal_item(
                        checkpoint, rejected, manifest=value,
                        manifest_digest=digest, batch_root=batch,
                    )
            self.assertTrue(swapped)
            self.assertEqual(
                original_bytes, (detached / "batch" / "checkpoint.json").read_bytes()
            )
            self.assertEqual(
                original_bytes, (ancestor / "batch" / "checkpoint.json").read_bytes()
            )
            self.assertFalse((detached / "batch" / "final-readback.json").exists())
            self.assertFalse((ancestor / "batch" / "final-readback.json").exists())
        finally:
            if swapped:
                os.rename(ancestor, replacement)
                os.rename(detached, ancestor)

    def test_directory_acquisition_interrupt_closes_owned_descriptor(self):
        batch = self.root / "interrupt-acquire"
        batch.mkdir()
        for interruption in (KeyboardInterrupt(), SystemExit(7)):
            with self.subTest(interruption=type(interruption).__name__):
                baseline = len(os.listdir("/proc/self/fd"))
                descriptor = os.open(
                    batch,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                with patch(
                    "ao_lore.batch_ingestion._open_anchored_directory",
                    return_value=descriptor,
                ), patch(
                    "ao_lore.batch_ingestion.os.fstat",
                    side_effect=interruption,
                ):
                    with self.assertRaises(type(interruption)) as caught:
                        batch_ingestion._ensure_real_directory(
                            batch, repository_root()
                        )

                self.assertIs(caught.exception, interruption)
                self.assertEqual(baseline, len(os.listdir("/proc/self/fd")))
                with self.assertRaises(OSError):
                    os.fstat(descriptor)

    def test_unlock_failure_closes_descriptor_and_preserves_primary_error(self):
        batch = self.root / "unlock-failure"
        handle = batch_ingestion._ensure_real_directory(
            batch, repository_root()
        )
        descriptor = handle.descriptor
        primary = RuntimeError("primary body failure")
        original_flock = batch_ingestion.fcntl.flock

        def fail_unlock(fd, operation):
            if operation == batch_ingestion.fcntl.LOCK_UN:
                raise OSError("private unlock detail")
            return original_flock(fd, operation)

        with patch(
            "ao_lore.batch_ingestion.fcntl.flock",
            side_effect=fail_unlock,
        ):
            with self.assertRaises(RuntimeError) as caught:
                with batch_ingestion._locked_batch(handle):
                    raise primary

        self.assertIs(caught.exception, primary)
        with self.assertRaises(OSError):
            os.fstat(descriptor)

        handle = batch_ingestion._ensure_real_directory(
            batch, repository_root()
        )
        descriptor = handle.descriptor
        with patch(
            "ao_lore.batch_ingestion.fcntl.flock",
            side_effect=fail_unlock,
        ):
            with self.assertRaisesRegex(
                BatchIngestionError, "batch state lock cleanup failed"
            ) as cleanup:
                with batch_ingestion._locked_batch(handle):
                    pass
        self.assertNotIn("private", str(cleanup.exception))
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_post_lock_ancestor_replacement_fails_before_checkpoint_write(self):
        value = validate_batch_manifest(manifest())
        digest = canonical_digest(value)
        ancestor = self.root / "locked-parent"
        batch = ancestor / "batch"
        detached = self.root / "locked-parent-detached"
        outside = Path(tempfile.mkdtemp(prefix="batch-locked-outside-"))
        (outside / "batch").mkdir()
        checkpoint = load_or_initialize_checkpoint(
            value, digest, batch_root=batch,
            inspect_candidate_fn=lambda _: self.fail("candidate load"),
        )
        before = (batch / "checkpoint.json").read_bytes()
        original_validate = batch_ingestion._validate_batch_entries
        swapped = False

        def swap_after_lock(descriptor):
            nonlocal swapped
            if not swapped:
                os.rename(ancestor, detached)
                ancestor.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original_validate(descriptor)

        rejected = {
            "item_id": "guide", "source_digest": DIGEST_A,
            "status": "rejected", "error_code": "document_rejected",
        }
        try:
            with patch(
                "ao_lore.batch_ingestion._validate_batch_entries",
                side_effect=swap_after_lock,
            ):
                with self.assertRaises(BatchIngestionError):
                    append_terminal_item(
                        checkpoint, rejected, manifest=value,
                        manifest_digest=digest, batch_root=batch,
                    )
            self.assertTrue(swapped)
            self.assertFalse((outside / "batch" / "checkpoint.json").exists())
            self.assertEqual(before, (detached / "batch" / "checkpoint.json").read_bytes())
        finally:
            if swapped:
                ancestor.unlink()
                os.rename(detached, ancestor)
            shutil.rmtree(outside)

    def test_existing_candidate_drift_does_not_advance_checkpoint(self):
        value = manifest([
            {"item_id": "one", "source": "sources/one.pdf", "source_digest": DIGEST_A},
            {"item_id": "two", "source": "sources/two.pdf", "source_digest": DIGEST_B},
        ])
        value, digest, checkpoint = self._init(value)
        item = success("one", DIGEST_A)
        candidate = verified_candidate(item)
        processed = append_terminal_item(
            checkpoint, item, manifest=value, manifest_digest=digest,
            batch_root=self.root, inspect_candidate_fn=lambda _: candidate,
        )
        checkpoint_path = self.root / "checkpoint.json"
        before = checkpoint_path.read_bytes()
        drift = copy.deepcopy(candidate)
        drift["inspection"]["review_status"] = "accepted"
        rejected = {"item_id": "two", "source_digest": DIGEST_B,
                    "status": "rejected", "error_code": "document_rejected"}

        with self.assertRaises(BatchIngestionError):
            append_terminal_item(
                processed, rejected, manifest=value, manifest_digest=digest,
                batch_root=self.root, inspect_candidate_fn=lambda _: drift,
            )

        self.assertEqual(before, checkpoint_path.read_bytes())

    def test_candidate_inspection_entry_drift_does_not_advance_checkpoint(self):
        value, digest, checkpoint = self._init()
        item = success(); candidate = verified_candidate(item)
        checkpoint_path = self.root / "checkpoint.json"
        before = checkpoint_path.read_bytes()

        def drifting_inspection(_):
            (self.root / "unexpected-from-inspection").write_bytes(b"drift")
            return candidate

        with self.assertRaises(BatchIngestionError):
            append_terminal_item(
                checkpoint, item, manifest=value, manifest_digest=digest,
                batch_root=self.root,
                inspect_candidate_fn=drifting_inspection,
            )
        self.assertEqual(before, checkpoint_path.read_bytes())

    def test_final_readback_loader_is_absent_or_strict(self):
        value, digest, _ = self._init()
        self.assertIsNone(load_final_batch_readback(value["batch_id"], batch_root=self.root))
        path = self.root / "final-readback.json"
        path.write_text('{"x":1}', encoding="utf-8")
        with self.assertRaises(BatchIngestionError):
            load_final_batch_readback(value["batch_id"], batch_root=self.root)


class BatchIngestionServiceTests(BatchCase):
    def _write_manifest(self, documents, batch_id="batch-service-01"):
        value = manifest(documents, batch_id=batch_id)
        path = self.root / f"{batch_id}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, value

    def _write_docx_manifest(self, documents, batch_id="batch-service-01"):
        value = docx_manifest(documents, batch_id=batch_id)
        path = self.root / f"{batch_id}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path, value

    def _dependencies(
        self,
        outcomes,
        calls,
        candidates,
        *,
        reader=None,
        appender=None,
        format_id="pdf",
        media_type="application/pdf",
        extension=".pdf",
        parser_id="docling",
        parser_version="2.118.1",
        signature_check=None,
        expected_corpus_digest=None,
        origin_for=None,
    ):
        def read_source(path, expected_digest):
            calls.append(("read", Path(path).name, expected_digest))
            payload = f"%PDF-{Path(path).stem}".encode()
            if reader is not None:
                return reader(path, expected_digest)
            return payload

        def ingest_one(path, **kwargs):
            item_id = Path(path).stem
            actual_context = kwargs["candidate_context"]
            calls.append((
                "ingest", item_id, copy.deepcopy(actual_context),
                copy.deepcopy(kwargs["benchmark_manifest"]),
                copy.deepcopy(kwargs["selection_profile"]),
                copy.deepcopy(kwargs["quality_profile"]),
                kwargs["expected_source_digest"],
                actual_context,
            ))
            if item_id == "one":
                actual_context["candidates"].append({
                    "candidate_id": "candidate-mutated",
                    "candidate_digest": DIGEST_A,
                })
            outcome = outcomes[item_id]
            if isinstance(outcome, BaseException):
                raise outcome
            report = single_readback(
                item_id,
                kwargs["expected_source_digest"],
                status=outcome,
                parser_id=parser_id,
                parser_version=parser_version,
            )
            candidates[report["candidate_id"]] = candidate_for_readback(report)
            return report

        return BatchDependencies(
            benchmark_manifest={"result_digest": DIGEST_A, "fixture_corpus_digest": DIGEST_B},
            selection_profile={"profile_id": "pdf-select", "bound": DIGEST_A},
            quality_profile={"profile_id": "pdf-quality", "bound": DIGEST_B},
            format_id=format_id,
            media_type=media_type,
            extension=extension,
            parser_id=parser_id,
            parser_version=parser_version,
            signature_check=(
                signature_check
                or (
                    batch_ingestion._docx_signature_matches
                    if format_id == "docx"
                    else batch_ingestion._pdf_signature_matches
                )
            ),
            expected_corpus_digest=expected_corpus_digest,
            origin_for=origin_for,
            ingest_one=ingest_one,
            inspect_candidate=lambda candidate_id: candidates[candidate_id],
            read_source=read_source,
            append_checkpoint=appender or append_terminal_item,
        )

    @staticmethod
    def _documents(*item_ids):
        return [
            {
                "item_id": item_id,
                "source": f"sources/{item_id}.pdf",
                "source_digest": "sha256:" + hashlib.sha256(
                    f"%PDF-{item_id}".encode()
                ).hexdigest(),
            }
            for item_id in item_ids
        ]

    @staticmethod
    def _docx_documents(*item_ids):
        fixture = DOCX_FIXTURE.read_bytes()
        digest = "sha256:" + hashlib.sha256(fixture).hexdigest()
        return [
            {
                "item_id": item_id,
                "source": f"sources/{item_id}.docx",
                "source_digest": digest,
            }
            for item_id in item_ids
        ]

    def test_declared_order_profiles_and_contexts_are_isolated(self):
        documents = self._documents("one", "two", "three")
        documents[0]["candidate_context"] = {
            "schema_version": "ao.lore.candidate-comparison-context.v0.1",
            "candidates": [],
        }
        path, _ = self._write_manifest(documents)
        calls, candidates = [], {}
        dependencies = self._dependencies(
            {"one": "created", "two": "created", "three": "unchanged"},
            calls, candidates,
        )

        report = ingest_batch(
            path, dependencies=dependencies, batch_root=self.root / "state"
        )

        ingests = [call for call in calls if call[0] == "ingest"]
        self.assertEqual(["one", "two", "three"], [call[1] for call in ingests])
        empty = {
            "schema_version": "ao.lore.candidate-comparison-context.v0.1",
            "candidates": [],
        }
        self.assertEqual([empty, empty, empty], [call[2] for call in ingests])
        self.assertIsNot(ingests[1][7], ingests[2][7])
        self.assertEqual([], ingests[1][7]["candidates"])
        self.assertEqual([], ingests[2][7]["candidates"])
        self.assertEqual((2, 1, 0, "completed"), (
            report["created"], report["unchanged"], report["rejected"], report["status"]
        ))
        self.assertFalse(report["canonical"])
        self.assertFalse(report["promotion_authority"])

    def test_answerability_policy_is_propagated_only_to_its_document(self):
        documents = self._documents("one", "two")
        policy = {"sensitivity": "public", "stale_after": None}
        documents[0]["knowledge_policy"] = policy
        path, original = self._write_manifest(documents)
        calls, candidates = [], {}

        report = ingest_batch(
            path,
            dependencies=self._dependencies(
                {"one": "created", "two": "created"}, calls, candidates
            ),
            batch_root=self.root / "state",
        )

        ingests = [call for call in calls if call[0] == "ingest"]
        self.assertEqual(policy, ingests[0][2]["knowledge_policy"])
        self.assertNotIn("knowledge_policy", ingests[1][2])
        self.assertEqual(policy, original["documents"][0]["knowledge_policy"])
        self.assertEqual((2, 0, "completed"), (
            report["created"], report["rejected"], report["status"]
        ))

    def test_checkpoint_verification_accepts_answerable_candidate(self):
        claims = [{
            "claim_id": "claim-1", "text": "Exact evidence.",
            "source_block_ids": ["b1"], "citation_id": "citation-1",
        }]
        citations = [{
            "citation_id": "citation-1", "render_text": "Exact evidence.",
            "source_block_ids": ["b1"],
        }]
        candidate = {
            "schema_version": "ao.lore.okf-candidate.v0.2",
            "candidate_id": "candidate-answerable-batch",
            "document_ir_digest": DIGEST_A,
            "concepts": [],
            "claim_mappings": [{"claim_id": "claim-1", "block_ids": ["b1"]}],
            "links": [], "contradiction_warnings": [],
            "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
            "claims_digest": claims_digest(claims), "claims": claims,
            "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
            "citations_digest": citations_digest(citations), "citations": citations,
            "knowledge_policy": {"sensitivity": "public", "stale_after": None},
            "canonical": False, "promotion_authority": False,
        }
        candidate_digest = canonical_digest(candidate)
        provenance = {
            "schema_version": "ao.lore.candidate-provenance.v0.1",
            "candidate_id": candidate["candidate_id"],
            "candidate_digest": candidate_digest,
            "document_ir_digest": DIGEST_A, "source_digest": DIGEST_A,
            "parser_id": "docling", "parser_version": "2.118.1",
            "parse_quality_report_digest": DIGEST_A,
            "parser_selection_report_digest": DIGEST_B,
            "distillation_trace_digest": DIGEST_C,
            "created_at": "2026-08-12T18:00:00Z",
        }
        inspection = {
            "schema_version": "ao.lore.candidate-inspection.v0.1",
            "candidate_id": candidate["candidate_id"],
            "candidate_digest": candidate_digest,
            "provenance_digest": canonical_digest(provenance),
            "verified_review_events": 0, "review_status": "unreviewed",
            "latest_event_digest": None, "canonical": False,
            "promotion_authority": False,
        }
        checkpoint = {"items": [{
            "item_id": "answerable", "source_digest": DIGEST_A,
            "status": "created", "candidate_id": candidate["candidate_id"],
            "candidate_digest": candidate_digest,
            "provenance_digest": canonical_digest(provenance),
            "review_status": "unreviewed",
        }]}

        batch_ingestion._verify_candidates(
            checkpoint,
            lambda _: {"candidate": candidate, "provenance": provenance,
                       "inspection": inspection},
        )

    def test_verified_batch_uses_detached_manifest_without_path_reload(self):
        path, value = self._write_manifest(self._documents("one", "two"))
        path.write_text('{"foreign":true}', encoding="utf-8")
        calls, candidates = [], {}
        report = ingest_verified_batch(
            value,
            canonical_digest(value),
            dependencies=self._dependencies(
                {"one": "created", "two": "created"}, calls, candidates
            ),
            batch_root=self.root / "state",
        )
        self.assertEqual(2, report["created"])
        self.assertEqual(["one", "two"], [
            call[1] for call in calls if call[0] == "ingest"
        ])

    def test_all_success_mixed_and_all_rejected_derive_exact_aggregates(self):
        cases = (
            ("all-success", {"one": "created", "two": "unchanged"}, (1, 1, 0, "completed")),
            ("mixed", {"one": "created", "two": ParsingError("private")}, (1, 0, 1, "partial")),
            ("all-rejected", {"one": DistillationError("private"), "two": CandidateError("private")}, (0, 0, 2, "partial")),
        )
        for suffix, outcomes, expected in cases:
            with self.subTest(suffix=suffix):
                path, _ = self._write_manifest(
                    self._documents("one", "two"), batch_id=f"batch-{suffix}"
                )
                calls, candidates = [], {}
                report = ingest_batch(
                    path,
                    dependencies=self._dependencies(outcomes, calls, candidates),
                    batch_root=self.root / f"state-{suffix}",
                )
                self.assertEqual(expected, (
                    report["created"], report["unchanged"], report["rejected"], report["status"]
                ))
                self.assertEqual(2, report["processed"])

    def test_source_digest_is_checked_before_ingest(self):
        path, _ = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}

        def changed_source(source, expected):
            if Path(source).stem == "one":
                return b"%PDF-drifted"
            return b"%PDF-two"

        report = ingest_batch(
            path,
            dependencies=self._dependencies(
                {"one": "created", "two": "created"}, calls, candidates,
                reader=changed_source,
            ),
            batch_root=self.root / "state",
        )
        self.assertEqual("source_digest_mismatch", report["items"][0]["error_code"])
        self.assertEqual(["two"], [call[1] for call in calls if call[0] == "ingest"])

    def test_source_invalid_and_qualification_failure_are_distinct(self):
        path, _ = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}

        def source_reader(source, expected):
            if Path(source).stem == "one":
                raise IngestionError("private source locator")
            return b"%PDF-two"

        report = ingest_batch(
            path,
            dependencies=self._dependencies(
                {"one": "created", "two": BenchmarkError("private evidence")},
                calls, candidates, reader=source_reader,
            ),
            batch_root=self.root / "state",
        )
        self.assertEqual(
            ["source_invalid", "qualification_invalid"],
            [item["error_code"] for item in report["items"]],
        )
        self.assertNotIn("private", json.dumps(report))

    def test_expected_failures_map_to_closed_redacted_codes(self):
        def caused(wrapper, cause):
            try:
                raise cause
            except Exception as exc:
                error = wrapper("private source/path detail")
                error.__cause__ = exc
                return error

        cases = (
            (ParsingError("secret parser"), "parser_rejected"),
            (DistillationError("secret distiller"), "distillation_rejected"),
            (CandidateError("secret candidate"), "candidate_conflict"),
            (caused(IngestionError, ParsingError("secret nested parser")), "parser_rejected"),
            (caused(IngestionError, DistillationError("secret nested distiller")), "distillation_rejected"),
            (caused(IngestionError, CandidateError("secret nested candidate")), "candidate_conflict"),
            (IngestionError("secret generic"), "document_rejected"),
        )
        for index, (error, code) in enumerate(cases):
            with self.subTest(code=code, index=index):
                item_id = f"item-{index}"
                path, _ = self._write_manifest(
                    self._documents(item_id), batch_id=f"batch-errors-{index}"
                )
                calls, candidates = [], {}
                report = ingest_batch(
                    path,
                    dependencies=self._dependencies({item_id: error}, calls, candidates),
                    batch_root=self.root / f"state-errors-{index}",
                )
                self.assertEqual(code, report["items"][0]["error_code"])
                serialized = json.dumps(report)
                self.assertNotIn("secret", serialized)
                self.assertNotIn("sources/", serialized)

    def test_unexpected_exception_and_interrupt_do_not_fabricate_terminal_item(self):
        for index, error in enumerate((RuntimeError("private"), KeyboardInterrupt())):
            with self.subTest(error=type(error).__name__):
                item_id = f"abort-{index}"
                path, value = self._write_manifest(
                    self._documents(item_id), batch_id=f"batch-abort-{index}"
                )
                calls, candidates = [], {}
                state = self.root / f"state-abort-{index}"
                with self.assertRaises(type(error)):
                    ingest_batch(
                        path,
                        dependencies=self._dependencies({item_id: error}, calls, candidates),
                        batch_root=state,
                    )
                checkpoint = load_or_initialize_checkpoint(
                    value, canonical_digest(value), batch_root=state,
                    inspect_candidate_fn=lambda _: self.fail("candidate inspection"),
                )
                self.assertEqual((0, []), (checkpoint["processed"], checkpoint["items"]))

    def test_unexpected_top_level_or_chain_member_propagates_without_state_change(self):
        def chained(top, cause):
            top.__cause__ = cause
            return top

        mixed_chain = IngestionError("mixed wrapper")
        mixed_chain.__cause__ = ParsingError("expected cause")
        mixed_chain.__context__ = RuntimeError("unexpected context")
        cases = (
            chained(RuntimeError("private runtime"), ParsingError("expected nested")),
            chained(KeyboardInterrupt(), ParsingError("expected nested")),
            chained(SystemExit(9), ParsingError("expected nested")),
            chained(IngestionError("production wrapper"), RuntimeError("private backend")),
            mixed_chain,
        )
        for index, error in enumerate(cases):
            with self.subTest(error=type(error).__name__, index=index):
                item_id = f"chain-{index}"
                path, value = self._write_manifest(
                    self._documents(item_id), batch_id=f"batch-chain-{index}"
                )
                state = self.root / f"state-chain-{index}"
                digest = canonical_digest(value)
                initial = load_or_initialize_checkpoint(
                    value, digest, batch_root=state,
                    inspect_candidate_fn=lambda _: self.fail("candidate inspection"),
                )
                before = (state / "checkpoint.json").read_bytes()
                calls, candidates = [], {}

                with self.assertRaises(type(error)) as caught:
                    ingest_batch(
                        path,
                        dependencies=self._dependencies(
                            {item_id: error}, calls, candidates
                        ),
                        batch_root=state,
                    )

                self.assertIs(caught.exception, error)
                self.assertEqual(before, (state / "checkpoint.json").read_bytes())
                self.assertEqual(initial, load_or_initialize_checkpoint(
                    value, digest, batch_root=state,
                    inspect_candidate_fn=lambda _: self.fail("candidate inspection"),
                ))
                self.assertFalse((state / "final-readback.json").exists())

    def test_unexpected_source_error_chain_propagates_without_state_change(self):
        item_id = "source-chain"
        path, value = self._write_manifest(
            self._documents(item_id), batch_id="batch-source-chain"
        )
        state = self.root / "state-source-chain"
        digest = canonical_digest(value)
        load_or_initialize_checkpoint(
            value, digest, batch_root=state,
            inspect_candidate_fn=lambda _: self.fail("candidate inspection"),
        )
        before = (state / "checkpoint.json").read_bytes()
        error = IngestionError("source wrapper")
        error.__cause__ = RuntimeError("private source backend")
        calls, candidates = [], {}

        def broken_source(path, expected):
            raise error

        with self.assertRaises(IngestionError) as caught:
            ingest_batch(
                path,
                dependencies=self._dependencies(
                    {item_id: "created"}, calls, candidates,
                    reader=broken_source,
                ),
                batch_root=state,
            )

        self.assertIs(caught.exception, error)
        self.assertEqual(before, (state / "checkpoint.json").read_bytes())
        self.assertFalse((state / "final-readback.json").exists())

    def test_resume_skips_terminal_items_and_completed_rerun_is_exact(self):
        path, _ = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}
        dependencies = self._dependencies(
            {"one": "created", "two": "unchanged"}, calls, candidates
        )
        first = ingest_batch(path, dependencies=dependencies, batch_root=self.root / "state")
        calls.clear()
        second = ingest_batch(path, dependencies=dependencies, batch_root=self.root / "state")
        self.assertEqual(first, second)
        self.assertEqual([], [call for call in calls if call[0] in {"read", "ingest"}])
        final = load_final_batch_readback("batch-service-01", batch_root=self.root / "state")
        self.assertEqual(first, final)

    def test_docx_resume_skips_terminal_items_and_completed_rerun_is_exact(self):
        fixture = DOCX_FIXTURE.read_bytes()
        source_digest = "sha256:" + hashlib.sha256(fixture).hexdigest()
        expectation = validate_docx_expectation(docx_expectation(source_digest))

        def read_docx(_path, _expected_digest):
            return fixture

        def origin_for(_source, body):
            self.assertEqual(body, fixture)
            return DocxProvenanceOrigin(
                corpus_id=DOCX_PRIVATE_CORPUS_ID,
                original_digest=expectation.documents[0]["source_digest"],
                derived_digest=source_digest,
                transformation_id=DOCX_TRANSFORMATION_ID,
                expectation_digest=expectation.corpus_digest,
            )

        path, _ = self._write_docx_manifest(self._docx_documents("one", "two"))
        calls, candidates = [], {}
        dependencies = self._dependencies(
            {"one": "created", "two": "unchanged"},
            calls,
            candidates,
            reader=read_docx,
            format_id="docx",
            media_type=DOCX_MIME,
            extension=".docx",
            parser_id="native-docx-ooxml",
            parser_version="1.0.0",
            expected_corpus_digest=expectation.corpus_digest,
            origin_for=origin_for,
        )
        first = ingest_batch(path, dependencies=dependencies, batch_root=self.root / "state")
        calls.clear()
        second = ingest_batch(path, dependencies=dependencies, batch_root=self.root / "state")
        self.assertEqual(first, second)
        self.assertEqual([], [call for call in calls if call[0] in {"read", "ingest"}])
        final = load_final_batch_readback("batch-service-01", batch_root=self.root / "state")
        self.assertEqual(first, final)
        self.assertEqual(
            b'{"batch_id":"batch-service-01","canonical":false,"created":1,"items":[{"candidate_digest":"sha256:4699aa39e456cb4e0cf66245346ffff726f51b8b6173b057f71ea907d889f5bd","candidate_id":"candidate-one","item_id":"one","provenance_digest":"sha256:948efbc7056dcbcf28a77549478c6e4c76ba89ce0512201acc30f445b69085cb","review_status":"unreviewed","source_digest":"sha256:85d40f59ff35af0fa98879704bdde4a69def9578cb0c606e776a2bd30332b6fc","status":"created"},{"candidate_digest":"sha256:15be441a3a76c7c2f52f5f9954cf59ec3ff86507813b8af573c96febccca92b8","candidate_id":"candidate-two","item_id":"two","provenance_digest":"sha256:3ce6ab4f229c600ecd7410f61ff5280181089bbae449fa7e43a7221353f3ee61","review_status":"unreviewed","source_digest":"sha256:85d40f59ff35af0fa98879704bdde4a69def9578cb0c606e776a2bd30332b6fc","status":"unchanged"}],"manifest_digest":"sha256:35ba5ff7d09190ad4bce79d83d202d76d898f1c0478614de9983d397f777394a","next_commands":["ao-lore candidate inspect --candidate-id candidate-one","ao-lore candidate review --candidate-id candidate-one --decision accept --reviewer <reviewer-id>","ao-lore candidate inspect --candidate-id candidate-two","ao-lore candidate review --candidate-id candidate-two --decision accept --reviewer <reviewer-id>","ao-lore candidate list --json"],"processed":2,"promotion_authority":false,"rejected":0,"schema_version":"ao.lore.ingest-batch-readback.v0.1","status":"completed","total":2,"unchanged":1}',
            canonical_bytes(first),
        )

    def test_manifest_and_dependency_policy_contradictions_fail_closed(self):
        path, _ = self._write_manifest(self._documents("one"))
        calls, candidates = [], {}
        with self.assertRaises(BatchIngestionError):
            ingest_batch(
                path,
                dependencies=self._dependencies(
                    {"one": "created"},
                    calls,
                    candidates,
                    format_id="docx",
                    media_type=DOCX_MIME,
                    extension=".docx",
                    parser_id="native-docx-ooxml",
                    parser_version="1.0.0",
                ),
                batch_root=self.root / "state-pdf-manifest-docx-deps",
            )
        self.assertEqual([], calls)

        docx_path, _ = self._write_docx_manifest(self._docx_documents("one"))
        with self.assertRaises(BatchIngestionError):
            ingest_batch(
                docx_path,
                dependencies=BatchDependencies(
                    benchmark_manifest={"result_digest": DIGEST_A, "fixture_corpus_digest": DIGEST_B},
                    selection_profile={"profile_id": "pdf-select", "bound": DIGEST_A},
                    quality_profile={"profile_id": "pdf-quality", "bound": DIGEST_B},
                    format_id="docx",
                    media_type=DOCX_MIME,
                    extension=".docx",
                    parser_id="native-docx-ooxml",
                    parser_version="1.0.0",
                ),
                batch_root=self.root / "state-docx-manifest-pdf-defaults",
            )
        self.assertEqual([], calls)

    def test_docx_single_readback_requires_declared_native_identity(self):
        fixture = DOCX_FIXTURE.read_bytes()

        def read_docx(_path, _expected_digest):
            return fixture

        path, _ = self._write_docx_manifest(self._docx_documents("one"))
        for field, value in (
            ("parser_id", "docling"),
            ("parser_version", "2.118.1"),
        ):
            with self.subTest(field=field):
                calls, candidates = [], {}
                dependencies = self._dependencies(
                    {"one": "created"},
                    calls,
                    candidates,
                    reader=read_docx,
                    format_id="docx",
                    media_type=DOCX_MIME,
                    extension=".docx",
                    parser_id="native-docx-ooxml",
                    parser_version="1.0.0",
                )
                original = dependencies.ingest_one

                def forged_parser(*args, **kwargs):
                    return {**original(*args, **kwargs), field: value}

                state = self.root / f"state-docx-{field}"
                with self.assertRaises(BatchIngestionError):
                    ingest_batch(
                        path,
                        dependencies=replace(dependencies, ingest_one=forged_parser),
                        batch_root=state,
                    )
                self.assertFalse((state / "final-readback.json").exists())

    def test_docx_real_ingest_requires_bound_origin_dependency_before_candidate_mutation(self):
        fixture = DOCX_FIXTURE.read_bytes()
        source_digest = "sha256:" + hashlib.sha256(fixture).hexdigest()
        expectation = validate_docx_expectation(docx_expectation(source_digest))
        qualification = docx_batch_qualification(expectation.corpus_digest)
        candidate_root = self.root / "candidates"
        with tempfile.TemporaryDirectory(dir=repository_root() / "sources") as source_name:
            source_root = Path(source_name)
            source_path = source_root / "private-customer-name.docx"
            source_path.write_bytes(fixture)
            path, _ = self._write_docx_manifest(
                [
                    {
                        "item_id": "one",
                        "source": source_path.relative_to(repository_root()).as_posix(),
                        "source_digest": source_digest,
                    }
                ]
            )

            missing = BatchDependencies(
                benchmark_manifest=qualification,
                selection_profile={"profile_id": "docx-native-v1"},
                quality_profile={"profile_id": "docx-quality-v1"},
                format_id="docx",
                media_type=DOCX_MIME,
                extension=".docx",
                parser_id=DOCX_PARSER_ID,
                parser_version=DOCX_PARSER_VERSION,
                signature_check=batch_ingestion._docx_signature_matches,
                ingest_one=ingest_docx_document,
                read_source=read_verified_docx_source,
                candidate_root=candidate_root,
            )
            with self.assertRaises(BatchIngestionError):
                ingest_batch(path, dependencies=missing, batch_root=self.root / "state-missing-docx-origin")
            self.assertFalse(candidate_root.exists())

            def foreign_origin(_source, body):
                self.assertEqual(body, fixture)
                return DocxProvenanceOrigin(
                    corpus_id=DOCX_PRIVATE_CORPUS_ID,
                    original_digest="sha256:" + "9" * 64,
                    derived_digest=source_digest,
                    transformation_id=DOCX_TRANSFORMATION_ID,
                    expectation_digest="sha256:" + "f" * 64,
                )

            foreign = replace(
                missing,
                expected_corpus_digest=expectation.corpus_digest,
                origin_for=foreign_origin,
            )
            with self.assertRaises(BatchIngestionError):
                ingest_batch(path, dependencies=foreign, batch_root=self.root / "state-foreign-docx-origin")
            self.assertFalse(candidate_root.exists())

    def test_candidate_persisted_checkpoint_crash_recovers_as_unchanged(self):
        path, _ = self._write_manifest(self._documents("one"))
        calls, candidates, persisted = [], {}, set()

        def ingest_one(path, **kwargs):
            item_id = Path(path).stem
            status = "unchanged" if item_id in persisted else "created"
            persisted.add(item_id)
            calls.append(status)
            report = single_readback(item_id, kwargs["expected_source_digest"], status=status)
            candidates[report["candidate_id"]] = candidate_for_readback(report)
            return report

        attempts = 0
        def append_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("simulated process loss")
            return append_terminal_item(*args, **kwargs)

        base = self._dependencies({"one": "created"}, [], candidates, appender=append_once)
        dependencies = BatchDependencies(
            benchmark_manifest=base.benchmark_manifest,
            selection_profile=base.selection_profile,
            quality_profile=base.quality_profile,
            ingest_one=ingest_one,
            inspect_candidate=base.inspect_candidate,
            read_source=base.read_source,
            append_checkpoint=base.append_checkpoint,
        )
        with self.assertRaises(RuntimeError):
            ingest_batch(path, dependencies=dependencies, batch_root=self.root / "state")
        report = ingest_batch(path, dependencies=dependencies, batch_root=self.root / "state")
        self.assertEqual(["created", "unchanged"], calls)
        self.assertEqual((0, 1), (report["created"], report["unchanged"]))

    def test_injected_appender_must_durably_advance_before_next_document(self):
        path, value = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}

        def decoy_appender(checkpoint, terminal, **kwargs):
            return batch_ingestion._reconstruct_checkpoint(
                value, canonical_digest(value), [*checkpoint["items"], terminal]
            )

        dependencies = self._dependencies(
            {"one": "created", "two": "created"}, calls, candidates,
            appender=decoy_appender,
        )
        with self.assertRaises(BatchIngestionError):
            ingest_batch(path, dependencies=dependencies, batch_root=self.root / "state")
        self.assertEqual(["one"], [call[1] for call in calls if call[0] == "ingest"])
        durable = load_or_initialize_checkpoint(
            value, canonical_digest(value), batch_root=self.root / "state",
            inspect_candidate_fn=lambda _: self.fail("candidate inspection"),
        )
        self.assertEqual(0, durable["processed"])

    def test_same_batch_runs_are_serialized_across_document_ingestion(self):
        path, _ = self._write_manifest(self._documents("one"))
        calls, candidates = [], {}
        entered = threading.Event()
        release = threading.Event()
        entries = []
        base = self._dependencies({"one": "created"}, calls, candidates)
        original = base.ingest_one

        def blocked_ingest(*args, **kwargs):
            entries.append(Path(args[0]).stem)
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)

        dependencies = replace(base, ingest_one=blocked_ingest)
        results, errors = [], []

        def run():
            try:
                results.append(ingest_batch(
                    path, dependencies=dependencies,
                    batch_root=self.root / "state",
                ))
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=run)
        second = threading.Thread(target=run)
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        self.assertFalse(release.wait(0.1))
        self.assertEqual(["one"], entries)
        release.set()
        first.join(5); second.join(5)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual(1, len([call for call in calls if call[0] == "ingest"]))
        self.assertEqual(results[0], results[1])

    def test_batch_directory_replacement_after_commit_stops_before_next_document(self):
        path, _ = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}
        state = self.root / "state"
        detached = self.root / "state-detached"
        substitute = self.root / "state-substitute"
        substitute.mkdir()
        swapped = False

        def append_then_swap(*args, **kwargs):
            nonlocal swapped
            result = append_terminal_item(*args, **kwargs)
            if not swapped:
                os.rename(state, detached)
                os.rename(substitute, state)
                swapped = True
            return result

        dependencies = self._dependencies(
            {"one": "created", "two": "created"}, calls, candidates,
            appender=append_then_swap,
        )
        try:
            with self.assertRaises(BatchIngestionError):
                ingest_batch(path, dependencies=dependencies, batch_root=state)
            self.assertEqual(
                ["one"], [call[1] for call in calls if call[0] == "ingest"]
            )
            self.assertFalse((state / "checkpoint.json").exists())
            durable = json.loads(
                (detached / "checkpoint.json").read_text(encoding="utf-8")
            )
            self.assertEqual(1, durable["processed"])
        finally:
            if swapped:
                os.rename(state, substitute)
                os.rename(detached, state)

    def test_checkpoint_inode_swap_during_ingest_stops_before_terminal_commit(self):
        path, _ = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}
        state = self.root / "state"
        base = self._dependencies(
            {"one": "created", "two": "created"}, calls, candidates
        )
        original = base.ingest_one

        def swap_checkpoint(source, **kwargs):
            checkpoint = state / "checkpoint.json"
            replacement = self.root / "byte-identical-checkpoint.json"
            replacement.write_bytes(checkpoint.read_bytes())
            os.replace(replacement, checkpoint)
            return original(source, **kwargs)

        with self.assertRaises(BatchIngestionError):
            ingest_batch(
                path,
                dependencies=replace(base, ingest_one=swap_checkpoint),
                batch_root=state,
            )
        self.assertEqual(["one"], [call[1] for call in calls if call[0] == "ingest"])
        durable = json.loads((state / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual((0, []), (durable["processed"], durable["items"]))
        self.assertFalse((state / "final-readback.json").exists())

    def test_checkpoint_inode_swap_during_source_read_stops_before_ingest(self):
        path, _ = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}
        state = self.root / "state"

        def swap_checkpoint(source, expected):
            checkpoint = state / "checkpoint.json"
            replacement = self.root / "source-read-replacement.json"
            replacement.write_bytes(checkpoint.read_bytes())
            os.replace(replacement, checkpoint)
            return f"%PDF-{Path(source).stem}".encode()

        dependencies = self._dependencies(
            {"one": "created", "two": "created"}, calls, candidates,
            reader=swap_checkpoint,
        )
        with self.assertRaises(BatchIngestionError):
            ingest_batch(path, dependencies=dependencies, batch_root=state)
        self.assertEqual([], [call for call in calls if call[0] == "ingest"])
        durable = json.loads((state / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual(0, durable["processed"])
        self.assertFalse((state / "final-readback.json").exists())

    def test_unknown_entry_during_ingest_stops_before_terminal_commit(self):
        path, _ = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}
        state = self.root / "state"
        base = self._dependencies(
            {"one": "created", "two": "created"}, calls, candidates
        )
        original = base.ingest_one

        def add_unknown_entry(source, **kwargs):
            (state / "unexpected-during-ingest").write_bytes(b"untrusted")
            return original(source, **kwargs)

        with self.assertRaises(BatchIngestionError):
            ingest_batch(
                path,
                dependencies=replace(base, ingest_one=add_unknown_entry),
                batch_root=state,
            )
        self.assertEqual(["one"], [call[1] for call in calls if call[0] == "ingest"])
        durable = json.loads((state / "checkpoint.json").read_text(encoding="utf-8"))
        self.assertEqual((0, []), (durable["processed"], durable["items"]))
        self.assertFalse((state / "final-readback.json").exists())

    def test_corrupt_checkpoint_or_final_readback_stops_before_ingestion(self):
        path, _ = self._write_manifest(self._documents("one"))
        calls, candidates = [], {}
        dependencies = self._dependencies({"one": "created"}, calls, candidates)
        state = self.root / "state"
        first = ingest_batch(path, dependencies=dependencies, batch_root=state)
        (state / "final-readback.json").write_text(
            json.dumps({**first, "canonical": True}), encoding="utf-8"
        )
        calls.clear()
        with self.assertRaises(BatchIngestionError):
            ingest_batch(path, dependencies=dependencies, batch_root=state)
        self.assertEqual([], [call for call in calls if call[0] in {"read", "ingest"}])
        duplicate = copy.deepcopy(first)
        duplicate.update(total=2, created=2, processed=2, items=first["items"] * 2)
        (state / "final-readback.json").write_text(
            json.dumps(duplicate), encoding="utf-8"
        )
        with self.assertRaises(BatchIngestionError):
            load_final_batch_readback("batch-service-01", batch_root=state)
        (state / "final-readback.json").unlink()
        checkpoint = json.loads((state / "checkpoint.json").read_text(encoding="utf-8"))
        checkpoint["created"] = 0
        (state / "checkpoint.json").write_text(json.dumps(checkpoint), encoding="utf-8")
        with self.assertRaises(BatchIngestionError):
            ingest_batch(path, dependencies=dependencies, batch_root=state)

    def test_next_commands_are_candidate_bound_and_brain_is_unchanged(self):
        def brain_snapshot():
            brain = repository_root() / "brain"
            return [
                (str(path.relative_to(brain)), hashlib.sha256(path.read_bytes()).hexdigest())
                for path in sorted(brain.rglob("*")) if path.is_file()
            ]

        before = brain_snapshot()
        path, _ = self._write_manifest(self._documents("one", "two"))
        calls, candidates = [], {}
        report = ingest_batch(
            path,
            dependencies=self._dependencies(
                {"one": "created", "two": ParsingError("private")}, calls, candidates
            ),
            batch_root=self.root / "state",
        )
        self.assertEqual(before, brain_snapshot())
        self.assertEqual([
            "ao-lore candidate inspect --candidate-id candidate-one",
            "ao-lore candidate review --candidate-id candidate-one --decision accept --reviewer <reviewer-id>",
            "ao-lore candidate list --json",
        ], report["next_commands"])

    def test_single_readback_requires_exact_docling_identity(self):
        path, _ = self._write_manifest(self._documents("one"))
        for field, value in (
            ("parser_id", "evil-parser"),
            ("parser_version", "2.118.2"),
        ):
            with self.subTest(field=field):
                calls, candidates = [], {}
                dependencies = self._dependencies(
                    {"one": "created"}, calls, candidates
                )
                original = dependencies.ingest_one

                def evil_parser(*args, **kwargs):
                    return {**original(*args, **kwargs), field: value}

                dependencies = replace(dependencies, ingest_one=evil_parser)
                state = self.root / f"state-{field}"
                with self.assertRaises(BatchIngestionError):
                    ingest_batch(path, dependencies=dependencies, batch_root=state)
                self.assertFalse((state / "final-readback.json").exists())


class BatchIngestCliTests(unittest.TestCase):
    def _report(self):
        return {
            "schema_version": "ao.lore.ingest-batch-readback.v0.1",
            "batch_id": "batch-cli-01",
            "manifest_digest": DIGEST_A,
            "status": "partial",
            "total": 2,
            "created": 1,
            "unchanged": 0,
            "rejected": 1,
            "processed": 2,
            "items": [
                success("one"),
                {
                    "item_id": "two",
                    "source_digest": DIGEST_C,
                    "status": "rejected",
                    "error_code": "parser_rejected",
                },
            ],
            "next_commands": [
                "ao-lore candidate inspect --candidate-id candidate-guide",
                "ao-lore candidate review --candidate-id candidate-guide --decision accept --reviewer <reviewer-id>",
                "ao-lore candidate list --json",
            ],
            "canonical": False,
            "promotion_authority": False,
        }

    def _invoke(self, report, *, json_output=False):
        from ao_lore.__main__ import main

        argv = ["ingest-batch", "--manifest", "sources/batch.json"]
        if json_output:
            argv.append("--json")
        stdout, stderr = StringIO(), StringIO()
        with patch(
            "ao_lore.batch_ingestion.load_batch_manifest",
            return_value=(manifest(), canonical_digest(manifest())),
        ), patch(
            "ao_lore.batch_ingestion.ingest_verified_batch",
            return_value=report,
        ) as run:
            with patch("ao_lore.__main__.strict_read_json", return_value=({
                "fixture_corpus_digest": DIGEST_B,
                "result_digest": DIGEST_C,
            }, DIGEST_A)), patch(
                "ao_lore.__main__._validate_batch_qualification",
                side_effect=lambda value: value,
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(argv)
        return status, stdout.getvalue(), stderr.getvalue(), run

    def test_parser_exposes_only_manifest_and_json(self):
        from ao_lore.__main__ import _parser

        args = vars(_parser().parse_args([
            "ingest-batch", "--manifest", "sources/batch.json", "--json"
        ]))
        self.assertEqual(args, {
            "surface": "ingest-batch",
            "manifest": "sources/batch.json",
            "json": True,
        })
        for forbidden in (
            "--root", "--parser-version", "--ocr", "--concurrency",
            "--retry-count", "--checkpoint",
        ):
            with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                _parser().parse_args([
                    "ingest-batch", "--manifest", "sources/batch.json", forbidden, "x"
                ])

    def test_cli_rejects_non_repository_relative_posix_manifest_locator(self):
        from ao_lore.__main__ import main

        invalid = (
            "", ".", "../batch.json", "sources/../batch.json",
            "/absolute/batch.json", r"sources\batch.json", "sources//batch.json",
        )
        for locator in invalid:
            with self.subTest(locator=locator):
                stdout, stderr = StringIO(), StringIO()
                with patch("ao_lore.batch_ingestion.ingest_batch") as run, \
                     redirect_stdout(stdout), redirect_stderr(stderr):
                    status = main([
                        "ingest-batch", "--manifest", locator, "--json"
                    ])
                self.assertEqual(status, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(
                    stderr.getvalue(), "ao-lore: batch ingest rejected\n"
                )
                run.assert_not_called()

    def test_json_is_exact_sorted_and_service_receives_bound_dependencies(self):
        report = self._report()
        status, stdout, stderr, run = self._invoke(report, json_output=True)
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, json.dumps(report, sort_keys=True) + "\n")
        args, kwargs = run.call_args
        self.assertEqual(args, (manifest(), canonical_digest(manifest())))
        self.assertEqual(set(kwargs), {"dependencies"})
        dependencies = kwargs["dependencies"]
        self.assertEqual(dependencies.benchmark_manifest["result_digest"], DIGEST_C)
        self.assertEqual(dependencies.selection_profile["fixture_corpus_digest"], DIGEST_B)
        self.assertEqual(dependencies.quality_profile["calibration_result_digest"], DIGEST_C)

    def test_human_output_is_compact_and_public_safe(self):
        status, stdout, stderr, _ = self._invoke(self._report())
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, (
            "STATUS\tpartial\n"
            "BATCH_ID\tbatch-cli-01\n"
            "COUNTS\tcreated=1\tunchanged=0\trejected=1\ttotal=2\n"
            "ITEM\tone\tcreated\tcandidate-guide\n"
            "ITEM\ttwo\trejected\tparser_rejected\n"
            "NEXT\tao-lore candidate inspect --candidate-id candidate-guide\n"
            "NEXT\tao-lore candidate review --candidate-id candidate-guide --decision accept --reviewer <reviewer-id>\n"
            "NEXT\tao-lore candidate list --json\n"
        ))
        self.assertNotIn("sha256:", stdout)
        self.assertNotIn("sources/", stdout)

    def test_invalid_readback_or_serialization_emits_no_stdout(self):
        invalid = {**self._report(), "promotion_authority": True}
        status, stdout, stderr, _ = self._invoke(invalid, json_output=True)
        self.assertEqual((status, stdout, stderr), (
            2, "", "ao-lore: batch ingest rejected\n"
        ))

        from ao_lore.__main__ import main
        stdout, stderr = StringIO(), StringIO()
        with patch("ao_lore.batch_ingestion.ingest_batch", return_value=self._report()), \
             patch("ao_lore.__main__.strict_read_json", return_value=({
                 "fixture_corpus_digest": DIGEST_B, "result_digest": DIGEST_C,
             }, DIGEST_A)), \
             patch("ao_lore.batch_ingestion.load_batch_manifest", return_value=(manifest(), canonical_digest(manifest()))), \
             patch("ao_lore.__main__._validate_batch_qualification", side_effect=lambda value: value), \
             patch("ao_lore.__main__.json.dumps", side_effect=TypeError("private")), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(["ingest-batch", "--manifest", "sources/batch.json", "--json"])
        self.assertEqual((status, stdout.getvalue(), stderr.getvalue()), (
            2, "", "ao-lore: batch ingest rejected\n"
        ))

    def test_allowed_failures_share_one_redacted_boundary(self):
        from ao_lore.__main__ import main

        chained = RuntimeError("private runtime")
        chained.__cause__ = OSError("/secret/source.pdf")
        for error in (
            BatchIngestionError("secret/path.pdf"), OSError("private"), chained,
        ):
            with self.subTest(error=type(error).__name__):
                stdout, stderr = StringIO(), StringIO()
                with patch("ao_lore.batch_ingestion.load_batch_manifest", return_value=(manifest(), canonical_digest(manifest()))), \
                     patch("ao_lore.batch_ingestion.ingest_verified_batch", side_effect=error), \
                     patch("ao_lore.__main__.strict_read_json", return_value=({
                         "fixture_corpus_digest": DIGEST_B, "result_digest": DIGEST_C,
                     }, DIGEST_A)), \
                     patch("ao_lore.__main__._validate_batch_qualification", side_effect=lambda value: value), \
                     redirect_stdout(stdout), redirect_stderr(stderr):
                    status = main([
                        "ingest-batch", "--manifest", "sources/private.json", "--json"
                    ])
                self.assertEqual(status, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "ao-lore: batch ingest rejected\n")

    def test_interrupts_are_not_reduced_to_cli_rejections(self):
        from ao_lore.__main__ import main

        for error in (KeyboardInterrupt(), SystemExit(19)):
            with self.subTest(error=type(error).__name__):
                with patch("ao_lore.batch_ingestion.load_batch_manifest", return_value=(manifest(), canonical_digest(manifest()))), \
                     patch("ao_lore.batch_ingestion.ingest_verified_batch", side_effect=error), \
                     patch("ao_lore.__main__.strict_read_json", return_value=({
                         "fixture_corpus_digest": DIGEST_B,
                         "result_digest": DIGEST_C,
                     }, DIGEST_A)), \
                     patch("ao_lore.__main__._validate_batch_qualification", side_effect=lambda value: value), \
                     redirect_stdout(StringIO()), redirect_stderr(StringIO()), \
                     self.assertRaises(type(error)):
                    main([
                        "ingest-batch", "--manifest", "sources/private.json", "--json"
                    ])

    def test_invalid_qualification_is_rejected_before_service(self):
        from ao_lore.__main__ import main

        stdout, stderr = StringIO(), StringIO()
        with patch("ao_lore.batch_ingestion.load_batch_manifest", return_value=(manifest(), canonical_digest(manifest()))), \
             patch("ao_lore.batch_ingestion.ingest_verified_batch") as run, \
             patch("ao_lore.__main__.strict_read_json", return_value=([], DIGEST_A)), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            status = main([
                "ingest-batch", "--manifest", "sources/private.json", "--json"
            ])
        self.assertEqual(status, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "ao-lore: batch ingest rejected\n")
        run.assert_not_called()

    def test_qualification_is_bound_to_expanded_corpus(self):
        from ao_lore.__main__ import _validate_batch_qualification

        self.assertEqual(
            _validate_batch_qualification(qualified_benchmark())["fixture_corpus_digest"],
            QUALIFIED_CORPUS_DIGEST,
        )
        foreign = qualified_benchmark(DIGEST_A)
        with self.assertRaises(ParsingError):
            _validate_batch_qualification(foreign)

    def test_duplicate_item_identity_is_rejected_before_cli_output(self):
        duplicate = self._report()
        duplicate["items"][1]["item_id"] = "one"
        status, stdout, stderr, _ = self._invoke(duplicate, json_output=True)
        self.assertEqual((status, stdout, stderr), (
            2, "", "ao-lore: batch ingest rejected\n"
        ))
        with self.assertRaises(BatchIngestionError):
            validate_final_batch_readback(duplicate)

    def test_docx_manifest_routes_to_trusted_docx_batch_dependencies(self):
        from ao_lore.__main__ import main

        with tempfile.TemporaryDirectory(dir=repository_root() / "working") as runtime_name, \
                tempfile.TemporaryDirectory(dir=repository_root() / "sources") as source_name:
            runtime_root = Path(runtime_name)
            source_root = Path(source_name)
            source_path = source_root / "private-customer-name.docx"
            fixture = DOCX_FIXTURE.read_bytes()
            source_path.write_bytes(fixture)
            source_digest = "sha256:" + hashlib.sha256(fixture).hexdigest()
            manifest_value = docx_manifest(
                [{
                    "item_id": "guide",
                    "source": source_path.relative_to(repository_root()).as_posix(),
                    "source_digest": source_digest,
                }],
                batch_id="batch-docx-cli-01",
            )
            manifest_digest = canonical_digest(manifest_value)
            expectation = docx_expectation(source_digest)
            validated = validate_docx_expectation(expectation)
            qualification = docx_batch_qualification(validated.corpus_digest)
            report = self._report()
            stdout, stderr = StringIO(), StringIO()
            with patch.dict(
                os.environ, {"AO_LORE_HOME": str(runtime_root)}, clear=False
            ), patch(
                "ao_lore.batch_ingestion.load_batch_manifest",
                return_value=(manifest_value, manifest_digest),
            ), patch(
                "ao_lore.batch_ingestion.ingest_verified_batch",
                return_value=report,
            ) as run, patch(
                "ao_lore.__main__.strict_read_json",
                side_effect=[
                    (expectation, canonical_digest(expectation)),
                    (qualification, qualification["result_digest"]),
                ],
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                status = main(["ingest-batch", "--manifest", "sources/batch.json", "--json"])

            self.assertEqual((status, stderr.getvalue()), (0, ""))
            self.assertEqual(json.loads(stdout.getvalue()), report)
            args, kwargs = run.call_args
            self.assertEqual(args, (manifest_value, manifest_digest))
            dependencies = kwargs["dependencies"]
            self.assertEqual(dependencies.format_id, "docx")
            self.assertEqual(dependencies.media_type, DOCX_MIME)
            self.assertEqual(dependencies.extension, ".docx")
            self.assertEqual(dependencies.parser_id, DOCX_PARSER_ID)
            self.assertEqual(dependencies.parser_version, DOCX_PARSER_VERSION)
            self.assertEqual(dependencies.expected_corpus_digest, validated.corpus_digest)
            self.assertIs(dependencies.ingest_one, ingest_docx_document)
            self.assertIs(dependencies.read_source, read_verified_docx_source)
            self.assertTrue(dependencies.signature_check(fixture))
            self.assertFalse(dependencies.signature_check(b"%PDF-fake"))
            origin = dependencies.origin_for(source_path, fixture)
            self.assertEqual(
                origin,
                DocxProvenanceOrigin(
                    corpus_id=DOCX_PRIVATE_CORPUS_ID,
                    original_digest=expectation["items"][0]["source_digest"],
                    derived_digest=source_digest,
                    transformation_id=DOCX_TRANSFORMATION_ID,
                    expectation_digest=validated.corpus_digest,
                ),
            )
            with self.assertRaises(IngestionError):
                dependencies.origin_for(source_path, fixture + b"drift")


if __name__ == "__main__":
    unittest.main()
