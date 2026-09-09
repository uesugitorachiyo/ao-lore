import json
import unittest
from pathlib import Path

from ao_lore.benchmark import (
    BenchmarkError,
    build_manifest,
    canonical_digest,
    compare_manifests,
    evaluate_regressions,
    run_fixture_corpus,
    verify_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS_PATH = ROOT / "tests" / "fixtures" / "ao_lore" / "parser-corpus-v0.1.json"


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        self.corpus_digest = canonical_digest(self.corpus)
        self.config_digest = canonical_digest({"device": "cpu", "ocr": False})

    def manifest(self, parser_id, version, structural, text, *, excluded=None):
        exclusions = list(excluded or [])
        normalized = {"structural_fidelity": structural, "text_fidelity": text, "repeatability": 1.0}
        raw = {**normalized, "table_accuracy": None if "table_accuracy" in exclusions else 0.8}
        if "table_accuracy" not in exclusions:
            normalized["table_accuracy"] = 0.8
        return build_manifest(
            fixture_corpus_digest=self.corpus_digest,
            parser_id=parser_id,
            parser_version=version,
            parser_configuration_digest=self.config_digest,
            runtime="python-3.11",
            platform="linux-amd64",
            document_ir_version="ao.lore.document-ir.v0.1",
            commands=[["python", "-m", "ao_lore.benchmark", "--fixture", "public-safe"]],
            raw_metrics=raw,
            normalized_scores=normalized,
            failures=[],
            exclusions=exclusions,
        )

    def test_docling_and_challenger_compare_with_visible_evidence(self):
        docling = self.manifest("docling", "2.0", 0.90, 0.93)
        challenger = self.manifest("challenger", "1.0", 0.95, 0.94)
        report = compare_manifests(
            docling,
            challenger,
            {"structural_fidelity": 0.5, "text_fidelity": 0.4, "table_accuracy": 0.1},
        )
        self.assertEqual(report["winner"], "challenger@1.0")
        self.assertEqual(report["fixture_corpus_digest"], self.corpus_digest)
        self.assertGreater(report["attempts"][1]["applicable_score"], report["attempts"][0]["applicable_score"])

    def test_result_digest_detects_metric_tampering(self):
        manifest = self.manifest("docling", "2.0", 0.9, 0.9)
        verify_manifest(manifest)
        manifest["normalized_scores"]["text_fidelity"] = 0.1
        with self.assertRaises(BenchmarkError):
            verify_manifest(manifest)

    def test_corpus_and_configuration_drift_fail_closed(self):
        left = self.manifest("docling", "2.0", 0.9, 0.9)
        right = self.manifest("challenger", "1.0", 0.9, 0.9)
        right["fixture_corpus_digest"] = "sha256:" + "b" * 64
        right["result_digest"] = canonical_digest({k: v for k, v in right.items() if k != "result_digest"})
        with self.assertRaises(BenchmarkError):
            compare_manifests(left, right, {"text_fidelity": 1})
        with self.assertRaises(BenchmarkError):
            verify_manifest(left, expected_configuration_digest="sha256:" + "c" * 64)

    def test_non_applicable_metrics_are_excluded_not_zeroed(self):
        left = self.manifest("plain-reader", "1", 1, 1, excluded=["table_accuracy"])
        right = self.manifest("other-reader", "1", 0.9, 0.9, excluded=["table_accuracy"])
        report = compare_manifests(left, right, {"text_fidelity": 0.5, "table_accuracy": 0.5})
        self.assertNotIn("table_accuracy", report["metric_deltas"])
        self.assertEqual(report["attempts"][0]["applicable_metrics"], ["text_fidelity"])
        self.assertNotEqual(report["attempts"][0]["applicable_score"], 0.5)

    def test_version_regression_thresholds_are_machine_readable(self):
        baseline = self.manifest("docling", "2.0", 0.9, 0.9)
        current = self.manifest("docling", "2.1", 0.7, 0.89)
        report = evaluate_regressions(baseline, current, {"structural_fidelity": 0.05, "text_fidelity": 0.02})
        self.assertFalse(report["passed"])
        self.assertEqual(report["regressions"][0]["metric"], "structural_fidelity")

    def test_fixture_runner_is_deterministic_and_records_failures(self):
        def adapter(fixture):
            if fixture["media_type"] == "image/png":
                raise RuntimeError("scripted OCR failure")
            return {"text_fidelity": 1.0, "latency_ms": float(len(fixture["id"]))}

        first = run_fixture_corpus(self.corpus, adapter)
        second = run_fixture_corpus(self.corpus, adapter)
        self.assertEqual(first, second)
        self.assertEqual(first["failures"], ["image-ocr-layout:RuntimeError"])
        self.assertEqual(first["raw_metrics"]["fixture_count"], 7)


if __name__ == "__main__":
    unittest.main()
