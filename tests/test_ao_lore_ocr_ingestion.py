import hashlib
import tempfile
import unittest
from pathlib import Path

from ao_lore.ingestion import (
    IngestionDependencies,
    IngestionError,
    default_ocr_ingestion_dependencies,
    ingest_ocr_document,
    ingest_verified_ocr_document,
)
from ao_lore.batch_ingestion import (
    BatchDependencies, BatchIngestionError, ingest_verified_batch,
    validate_batch_manifest,
)
from ao_lore.benchmark import canonical_digest
from ao_lore.model_roles import ActivatedOcrParser
from ao_lore.ocr_ir import OCR_IR_CONFIGURATION_DIGEST
from ao_lore.paddle_ocr import OCR_WORKER_CONFIGURATION_DIGEST
from ao_lore.parsing import ParserRegistry, ProductionParser, QualifiedOcrAdapter
from tests.test_ao_lore_ingestion import FakeDistiller, FakeParser


DIGEST = "sha256:" + "d" * 64


def activation():
    return ActivatedOcrParser(
        capability="ocr-layout", parser_id="paddle-ocr-english",
        parser_version="0.1.0", candidate_id="candidate-1",
        runtime_digest=DIGEST,
        model_set_digest="sha256:" + "1" * 64,
        qualification_digest="sha256:" + "2" * 64,
        selected_result_digest="sha256:" + "3" * 64,
        character_accuracy_millionths=1_000_000,
        detection_recall_millionths=1_000_000,
        reading_order_pair_accuracy_millionths=1_000_000,
        mean_polygon_iou_millionths=900_000,
        page_coverage_millionths=1_000_000,
        provider_enabled=False, fallback_enabled=False,
    )


def worker_result():
    from ao_lore.ocr_contracts import OCR_POLICY_DIGEST
    return {
        "schema_version": "ao.lore.private-ocr-worker-result.v0.1",
        "policy_digest": OCR_POLICY_DIGEST,
        "run_id": "run-ingest", "candidate_id": "candidate-1",
        "runtime_digest": DIGEST, "model_set_digest": "sha256:" + "1" * 64,
        "configuration_digest": OCR_WORKER_CONFIGURATION_DIGEST,
        "latency_microseconds": 100, "peak_gpu_memory_bytes": 200,
        "pages": [{
            "page_id": "page-0001", "width": 100, "height": 50,
            "detections": [{
                "index": 0, "text": "Visible English text",
                "confidence_millionths": 999_000,
                "polygon": [[1, 1], [90, 1], [90, 20], [1, 20]],
            }],
        }],
        "network_accessed": False, "provider_calls": False,
        "promotion": False, "publication": False, "release": False,
        "deployment": False, "authority_advanced": False,
    }


class OcrIngestionTests(unittest.TestCase):
    def test_ocr_batch_manifest_is_exact_and_cannot_select_backend_or_model(self):
        manifest = {
            "schema_version": "ao.lore.ingest-batch-manifest.v0.3",
            "batch_id": "batch-ocr-01", "format_id": "ocr",
            "media_type": "application/pdf", "parser_id": "paddle-ocr-english",
            "parser_version": "0.1.0", "continue_on_error": True,
            "documents": [{
                "item_id": "scan-01", "source": "sources/ocr/scan-01.pdf",
                "source_digest": "sha256:" + "a" * 64,
            }],
        }
        self.assertEqual(validate_batch_manifest(manifest), manifest)
        for field, value in (
            ("model", "candidate-2"), ("backend", "other"),
            ("provider", "remote"), ("retry", 3),
        ):
            changed = dict(manifest)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(BatchIngestionError):
                validate_batch_manifest(changed)

    def test_qualified_adapter_binds_activation_and_returns_canonical_ir(self):
        calls = []
        adapter = QualifiedOcrAdapter(activation(), lambda source: calls.append(source) or worker_result())
        capability = adapter.capability
        self.assertEqual(capability["parser_id"], "paddle-ocr-english")
        self.assertEqual(capability["benchmark_result_refs"], [activation().qualification_digest])
        self.assertIn("application/pdf", capability["supported_mime_types"])
        self.assertIn(".pdf", capability["supported_extensions"])
        source = {
            "resource": "source-0123456789abcdef.png",
            "digest": "sha256:" + "a" * 64,
            "media_type": "image/png", "data": b"png",
        }
        output = adapter.parse(source)
        self.assertEqual(len(calls), 1)
        self.assertEqual(output.document_ir["parser"]["configuration_digest"], OCR_IR_CONFIGURATION_DIGEST)
        self.assertEqual(output.document_ir["blocks"][0]["text"], "Visible English text")
        self.assertNotIn("model_set_digest", output.document_ir)
        registry = ParserRegistry()
        registry.register(adapter)
        parsed = ProductionParser(registry).parse_bytes(
            b"\x89PNG\r\n\x1a\nfixture", "source-0123456789abcdef.png",
            {
                "media_type": "image/png", "extension": ".png",
                "required_structural_elements": ["ocr", "layout"],
                "canonical_ir_version": "ao.lore.document-ir.v0.1",
                "network_allowed": False, "license_allowlist": ["Apache-2.0"],
                "sandbox_required": True,
            },
            {
                "profile_id": "ocr-select-v1",
                "weights": {"text_fidelity": 1.0}, "penalty_weights": {},
                "normalization_bounds": {}, "missing_optional_policy": "ineligible",
                "tie_break_order": ["parser_id"],
            },
            {
                "profile_id": "ocr-quality-v1",
                "component_weights": {
                    "text_coverage": 0.4, "structural_completeness": 0.3,
                    "source_span_coverage": 0.2, "document_ir_valid": 0.1,
                },
                "accept_threshold": 0.8, "fallback_threshold": 0.7,
                "maximum_fallbacks": 0,
                "critical_failures": [
                    "invalid_ir", "source_digest_mismatch", "parser_crash",
                    "no_readable_content", "lost_provenance",
                ],
                "intermediate_decision": "quarantine", "exhausted_decision": "reject",
            },
        )
        self.assertEqual(parsed["decision"], "accept")
        self.assertEqual(parsed["selected_parser"], "paddle-ocr-english")
        self.assertEqual(
            parsed["selection_report"]["benchmark_evidence"],
            [activation().qualification_digest],
        )
        built = default_ocr_ingestion_dependencies(
            activation(), lambda source: worker_result()
        )
        self.assertEqual(built.ocr_activation, activation())
        self.assertEqual(
            built.parser._registry.capabilities()[0]["parser_id"],
            "paddle-ocr-english",
        )

    def test_verified_ocr_ingestion_preserves_candidate_and_false_authority(self):
        data = b"\x89PNG\r\n\x1a\nlocal fixture"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        calls = []
        runtime = IngestionDependencies(
            parser=FakeParser(
                calls, media_type="image/png", parser_id="paddle-ocr-english",
                parser_version="0.1.0",
                parser_configuration_digest=OCR_IR_CONFIGURATION_DIGEST,
                benchmark_result_digest=activation().qualification_digest,
                selection_profile_id="ocr-select-v1", quality_profile_id="ocr-quality-v1",
            ),
            distiller=FakeDistiller(calls), now=lambda: "2026-08-11T00:00:00Z",
        )
        candidate_parent = Path(__file__).resolve().parents[1] / "working" / "candidates"
        candidate_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=candidate_parent) as temporary:
            root = Path(temporary)
            result = ingest_verified_ocr_document(
                data, source_digest=digest,
                resource="source-" + digest[7:23] + ".png", media_type="image/png",
                activation=activation(),
                selection_profile={"profile_id": "ocr-select-v1"},
                quality_profile={"profile_id": "ocr-quality-v1"},
                dependencies=runtime, candidate_root=root,
            )
        self.assertEqual(result["parser_id"], "paddle-ocr-english")
        self.assertEqual(result["parser_version"], "0.1.0")
        self.assertFalse(result["canonical"])
        self.assertFalse(result["promotion_authority"])

    def test_verified_ocr_ingestion_rejects_media_digest_and_activation_drift(self):
        data = b"\x89PNG\r\n\x1a\nfixture"
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        for media, supplied, active in (
            ("text/plain", digest, activation()),
            ("image/png", "sha256:" + "0" * 64, activation()),
            ("image/png", digest, object()),
        ):
            with self.subTest(media=media, supplied=supplied, active=type(active)):
                with self.assertRaises(IngestionError):
                    ingest_verified_ocr_document(
                        data, source_digest=supplied,
                        resource="source-" + digest[7:23] + ".png", media_type=media,
                        activation=active,
                        selection_profile={"profile_id": "ocr-select-v1"},
                        quality_profile={"profile_id": "ocr-quality-v1"},
                        dependencies=None,
                    )

    def test_ocr_batch_uses_only_activated_parser_and_preserves_queue_semantics(self):
        root = Path(__file__).resolve().parents[1]
        sources_parent = root / "sources"
        candidates_parent = root / "working" / "candidates"
        runtime_parent = root / ".ao-lore" / "test-ocr-batches"
        sources_parent.mkdir(exist_ok=True)
        candidates_parent.mkdir(parents=True, exist_ok=True)
        runtime_parent.mkdir(parents=True, exist_ok=True)
        source_temp = tempfile.TemporaryDirectory(dir=sources_parent)
        candidate_temp = tempfile.TemporaryDirectory(dir=candidates_parent)
        batch_temp = tempfile.TemporaryDirectory(dir=runtime_parent)
        try:
            source = Path(source_temp.name) / "scan.pdf"
            data = b"%PDF-1.7\nscanned fixture"
            source.write_bytes(data)
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            manifest = {
                "schema_version": "ao.lore.ingest-batch-manifest.v0.3",
                "batch_id": "batch-ocr-integration", "format_id": "ocr",
                "media_type": "application/pdf", "parser_id": "paddle-ocr-english",
                "parser_version": "0.1.0", "continue_on_error": True,
                "documents": [{
                    "item_id": "scan-01",
                    "source": source.relative_to(root).as_posix(),
                    "source_digest": digest,
                }],
            }
            calls = []
            active = activation()
            runtime = IngestionDependencies(
                parser=FakeParser(
                    calls, media_type="application/pdf",
                    parser_id="paddle-ocr-english", parser_version="0.1.0",
                    parser_configuration_digest=OCR_IR_CONFIGURATION_DIGEST,
                    benchmark_result_digest=active.qualification_digest,
                    selection_profile_id="ocr-select-v1",
                    quality_profile_id="ocr-quality-v1",
                ),
                distiller=FakeDistiller(calls),
                now=lambda: "2026-08-11T00:00:00Z",
                ocr_activation=active,
            )
            qualification = {
                "qualification_digest": active.qualification_digest,
                "selected_candidate_id": active.candidate_id,
                "model_set_digest": active.model_set_digest,
            }
            dependencies = BatchDependencies(
                benchmark_manifest=qualification,
                selection_profile={"profile_id": "ocr-select-v1"},
                quality_profile={"profile_id": "ocr-quality-v1"},
                format_id="ocr", media_type="application/pdf", extension=".pdf",
                parser_id="paddle-ocr-english", parser_version="0.1.0",
                ingest_one=ingest_ocr_document,
                ingestion_dependencies=runtime,
                candidate_root=Path(candidate_temp.name),
            )
            result = ingest_verified_batch(
                manifest, canonical_digest(manifest), dependencies=dependencies,
                batch_root=Path(batch_temp.name) / "batch-ocr-integration",
            )
            self.assertEqual(result["status"], "completed")
            self.assertEqual((result["processed"], result["created"], result["rejected"]), (1, 1, 0))
            self.assertFalse(result["canonical"])
            self.assertFalse(result["promotion_authority"])
            self.assertEqual(result["items"][0]["source_digest"], digest)
        finally:
            batch_temp.cleanup()
            candidate_temp.cleanup()
            source_temp.cleanup()
            if runtime_parent.exists() and not any(runtime_parent.iterdir()):
                runtime_parent.rmdir()


if __name__ == "__main__":
    unittest.main()
