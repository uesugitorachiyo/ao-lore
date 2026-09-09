import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ao_lore._strict_io import ContractError
from ao_lore.evaluation import compare_evaluation


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def attempt(attempt_id, system_id, score, *, format_name="pdf"):
        metric = {"applicable": True, "score_basis_points": score, "reason": "fixed benchmark"}
        return {
            "schema_version": "ao.lore.evaluation-attempt.v0.1",
            "attempt_id": attempt_id,
            "system_id": system_id,
            "format": format_name,
            "fixture_corpus_sha256": "a" * 64,
            "workload_profile_sha256": "b" * 64,
            "source_input_sha256": "c" * 64,
            "result_sha256": "d" * 64,
            "metrics": {
                "parser_fidelity": copy.deepcopy(metric),
                "parser_cost_efficiency": copy.deepcopy(metric),
                "coverage_efficiency": copy.deepcopy(metric),
                "role_configuration_quality": copy.deepcopy(metric),
            },
            "role_configuration": {
                "parser": "parser-local",
                "distiller": "distiller-local",
                "navigator": "navigator-local",
                "synthesizer": "synthesizer-local",
            },
            "eligible": True,
            "ineligibility_reasons": [],
            "failures": [],
            "exclusions": [],
            "trace_integrity": True,
        }

    def write_json(self, name, value):
        body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return {"path": name, "sha256": hashlib.sha256(body).hexdigest()}

    def manifest(self, baseline, challenger, *, format_name="pdf"):
        return {
            "schema_version": "ao.lore.evaluation-manifest.v0.1",
            "evaluation_id": "standalone-test",
            "metric_weights_basis_points": {
                "parser_fidelity": 4000,
                "parser_cost_efficiency": 2000,
                "coverage_efficiency": 3000,
                "role_configuration_quality": 1000,
            },
            "pairs": [{"format": format_name, "baseline": baseline, "challenger": challenger}],
        }

    def build_manifest(self, baseline_score=8000, challenger_score=9000):
        baseline = self.write_json("attempts/baseline.json", self.attempt("baseline-pdf", "baseline", baseline_score))
        challenger = self.write_json("attempts/challenger.json", self.attempt("challenger-pdf", "challenger", challenger_score))
        self.write_json("manifest.json", self.manifest(baseline, challenger))
        return self.root / "manifest.json"

    def test_matched_attempts_produce_evidence_only_comparison(self):
        report = compare_evaluation(self.build_manifest())
        self.assertEqual("challenger", report["winner"])
        self.assertEqual("win", report["result"])
        self.assertEqual("evaluation-evidence-only", report["authority"])
        self.assertEqual(9000, report["challenger_total"])
        self.assertEqual("navigator-local", report["pairs"][0]["challenger"]["role_configuration"]["navigator"])

    def test_non_applicable_metrics_are_renormalized_not_zeroed(self):
        baseline = self.attempt("baseline-text", "baseline", 8500, format_name="text")
        challenger = self.attempt("challenger-text", "challenger", 8500, format_name="text")
        for attempt in (baseline, challenger):
            attempt["metrics"]["parser_cost_efficiency"] = {
                "applicable": False,
                "score_basis_points": None,
                "reason": "not measured for direct text",
            }
        left = self.write_json("baseline.json", baseline)
        right = self.write_json("challenger.json", challenger)
        self.write_json("manifest.json", self.manifest(left, right, format_name="text"))
        report = compare_evaluation(self.root / "manifest.json")
        self.assertEqual("tie", report["winner"])
        self.assertIsNone(report["pairs"][0]["baseline"]["metrics"]["parser_cost_efficiency"]["score_basis_points"])

    def test_failure_ineligibility_or_trace_failure_zeroes_comparison_only(self):
        baseline = self.attempt("baseline-pdf", "baseline", 9000)
        challenger = self.attempt("challenger-pdf", "challenger", 8000)
        baseline["eligible"] = False
        baseline["ineligibility_reasons"] = ["trace contract unavailable"]
        challenger["failures"] = ["bounded timeout"]
        left = self.write_json("baseline.json", baseline)
        right = self.write_json("challenger.json", challenger)
        self.write_json("manifest.json", self.manifest(left, right))
        report = compare_evaluation(self.root / "manifest.json")
        pair = report["pairs"][0]
        self.assertEqual(9000, pair["baseline"]["raw_composite_score"])
        self.assertEqual(0, pair["baseline"]["comparison_score"])
        self.assertEqual(0, pair["challenger"]["comparison_score"])

    def test_digest_and_matched_binding_drift_fail_closed(self):
        manifest_path = self.build_manifest()
        manifest = json.loads(manifest_path.read_text())
        manifest["pairs"][0]["baseline"]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ContractError, "SHA-256 mismatch"):
            compare_evaluation(manifest_path)

        self.root.joinpath("attempts/baseline.json").unlink()
        baseline = self.attempt("baseline-pdf", "baseline", 8000)
        baseline["fixture_corpus_sha256"] = "e" * 64
        left = self.write_json("attempts/baseline.json", baseline)
        manifest["pairs"][0]["baseline"] = left
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ContractError, "matched pair"):
            compare_evaluation(manifest_path)

    def test_unsafe_reference_duplicate_key_and_unknown_field_fail_closed(self):
        manifest_path = self.build_manifest()
        manifest = json.loads(manifest_path.read_text())
        manifest["pairs"][0]["baseline"]["path"] = "../escape.json"
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ContractError, "contained relative"):
            compare_evaluation(manifest_path)

        manifest_path.write_text('{"schema_version":"x","schema_version":"y"}')
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            compare_evaluation(manifest_path)

    def test_output_is_exclusive_and_contained(self):
        manifest = self.build_manifest()
        output = self.root / "report.json"
        compare_evaluation(manifest, output_path=output, output_root=self.root)
        self.assertTrue(output.is_file())
        with self.assertRaisesRegex(ContractError, "already exists"):
            compare_evaluation(manifest, output_path=output, output_root=self.root)
        with self.assertRaisesRegex(ContractError, "escapes"):
            compare_evaluation(manifest, output_path=self.root.parent / "escape.json", output_root=self.root)


if __name__ == "__main__":
    unittest.main()
