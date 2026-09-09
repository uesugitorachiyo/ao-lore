import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ao_lore._strict_io import ContractError
from ao_lore.monitoring import evaluate_monitoring


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def metrics():
        return {
            "parser_selection_score_basis_points": 9000,
            "parse_quality_threshold_basis_points": 8000,
            "evidence_coverage_basis_points": 8500,
            "wasted_traversal_nodes": 2,
            "nodes_per_satisfied_requirement_milli": 1500,
            "tokens_per_coverage_point_milli": 2000,
        }

    def baseline(self):
        return {
            "schema_version": "ao.lore.monitoring-baseline.v0.1",
            "baseline_id": "baseline-v1",
            "target_id": "ao-lore-local",
            "source_head_sha256": "a" * 64,
            "profile_sha256": "b" * 64,
            "generated_at_utc": "2026-08-01T00:00:00Z",
            "valid_until_utc": "2026-09-01T00:00:00Z",
            "metrics": self.metrics(),
            "tolerances": {
                "max_parser_score_drop_basis_points": 200,
                "max_threshold_change_basis_points": 100,
                "max_coverage_drop_basis_points": 200,
                "max_wasted_traversal_increase": 2,
                "max_nodes_per_requirement_increase_milli": 200,
                "max_tokens_per_coverage_increase_milli": 300,
            },
            "allowed_role_fallbacks": {
                "parser": ["docling->native-pdf"],
                "distiller": [],
                "navigator": ["navigator-local->navigator-backup"],
                "synthesizer": [],
            },
            "trace_policy_sha256": "c" * 64,
        }

    def write_json(self, name, value):
        body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
        path = self.root / name
        path.write_bytes(body)
        return path, hashlib.sha256(body).hexdigest()

    def observation(self, baseline_digest=""):
        return {
            "schema_version": "ao.lore.monitoring-observation.v0.1",
            "observation_id": "observation-v1",
            "target_id": "ao-lore-local",
            "baseline_id": "baseline-v1" if baseline_digest else "",
            "baseline_sha256": baseline_digest,
            "source_head_sha256": "d" * 64,
            "profile_sha256": "b" * 64,
            "observed_at_utc": "2026-08-08T00:00:00Z",
            "metrics": self.metrics(),
            "role_fallbacks": [],
            "trace_policy_sha256": "c" * 64,
            "trace_integrity": True,
        }

    def bound_inputs(self):
        baseline_path, digest = self.write_json("baseline.json", self.baseline())
        observation_path, _ = self.write_json("observation.json", self.observation(digest))
        return baseline_path, observation_path

    def test_clear_verdict_is_read_only_and_digest_bound(self):
        baseline, observation = self.bound_inputs()
        verdict = evaluate_monitoring(observation, baseline_path=baseline)
        self.assertEqual("clear", verdict["status"])
        self.assertEqual("present", verdict["baseline_status"])
        self.assertFalse(verdict["promoter_hold_required"])
        for field in ("mutates_live_state", "approves_work", "promotes_candidate", "releases_or_publishes"):
            self.assertFalse(verdict[field])
        self.assertEqual(hashlib.sha256(baseline.read_bytes()).hexdigest(), verdict["input_digests"]["baseline_sha256"])

    def test_missing_baseline_holds_without_inventing_bindings(self):
        observation, _ = self.write_json("observation.json", self.observation())
        verdict = evaluate_monitoring(observation)
        self.assertEqual("hold", verdict["status"])
        self.assertEqual("missing", verdict["baseline_status"])
        self.assertIsNone(verdict["baseline_id"])
        self.assertEqual("baseline_missing", verdict["findings"][0]["code"])

    def test_metric_drift_and_stale_baseline_hold(self):
        baseline_value = self.baseline()
        baseline_value["valid_until_utc"] = "2026-08-02T00:00:00Z"
        baseline_path, digest = self.write_json("baseline.json", baseline_value)
        observation_value = self.observation(digest)
        observation_value["metrics"]["parser_selection_score_basis_points"] = 8500
        observation_value["metrics"]["evidence_coverage_basis_points"] = 8100
        observation_value["metrics"]["wasted_traversal_nodes"] = 6
        observation_path, _ = self.write_json("observation.json", observation_value)
        verdict = evaluate_monitoring(observation_path, baseline_path=baseline_path)
        codes = {finding["code"] for finding in verdict["findings"]}
        self.assertEqual("hold", verdict["status"])
        self.assertTrue({"baseline_stale", "parser_score_drift", "coverage_regression", "wasted_traversal_regression"}.issubset(codes))

    def test_unauthorized_fallback_or_trace_failure_is_incident(self):
        baseline_path, digest = self.write_json("baseline.json", self.baseline())
        observation = self.observation(digest)
        observation["role_fallbacks"] = [{"role": "synthesizer", "from_adapter": "local", "to_adapter": "frontier"}]
        observation["trace_integrity"] = False
        observation["trace_policy_sha256"] = "e" * 64
        observation_path, _ = self.write_json("observation.json", observation)
        verdict = evaluate_monitoring(observation_path, baseline_path=baseline_path)
        codes = {finding["code"] for finding in verdict["findings"]}
        self.assertEqual("incident", verdict["status"])
        self.assertEqual({"trace_integrity_failure", "trace_policy_mismatch", "unauthorized_role_fallback"}, codes)
        self.assertTrue(verdict["promoter_hold_required"])

    def test_digest_identity_and_unknown_fields_fail_closed(self):
        baseline_path, digest = self.write_json("baseline.json", self.baseline())
        observation = self.observation("0" * 64)
        observation_path, _ = self.write_json("observation.json", observation)
        with self.assertRaisesRegex(ContractError, "SHA-256 mismatch"):
            evaluate_monitoring(observation_path, baseline_path=baseline_path)

        observation = self.observation(digest)
        observation["unexpected"] = True
        observation_path.write_text(json.dumps(observation))
        with self.assertRaisesRegex(ContractError, "unknown"):
            evaluate_monitoring(observation_path, baseline_path=baseline_path)

    def test_duplicate_json_symlink_input_and_output_overwrite_fail_closed(self):
        duplicate = self.root / "observation.json"
        duplicate.write_text('{"schema_version":"x","schema_version":"y"}')
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            evaluate_monitoring(duplicate)

        real = self.root / "real.json"
        real.write_text("{}")
        link = self.root / "link.json"
        link.symlink_to(real)
        with self.assertRaisesRegex(ContractError, "regular non-link"):
            evaluate_monitoring(link)

        baseline, observation = self.bound_inputs()
        output = self.root / "verdict.json"
        evaluate_monitoring(observation, baseline_path=baseline, output_path=output, output_root=self.root)
        with self.assertRaisesRegex(ContractError, "already exists"):
            evaluate_monitoring(observation, baseline_path=baseline, output_path=output, output_root=self.root)


if __name__ == "__main__":
    unittest.main()
