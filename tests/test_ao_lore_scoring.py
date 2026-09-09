import math
import unittest

from ao_lore.scoring import (
    ScoringError,
    branch_priority,
    compute_evidence_coverage,
    compute_parse_quality,
    evaluate_eligibility,
    select_parser,
)


DIGEST = "sha256:" + "a" * 64


def capability(parser_id="native-markdown", **overrides):
    value = {
        "parser_id": parser_id,
        "version": "1.0.0",
        "supported_mime_types": ["text/markdown"],
        "supported_extensions": [".md"],
        "structural_elements": ["headings", "links", "code"],
        "format_status": {"text/markdown": "native"},
        "deterministic_offline": True,
        "frontier_service_required": False,
        "runtime_available": True,
        "license_id": "MIT",
        "sandbox_supported": True,
        "max_file_bytes": 1000,
        "canonical_ir_versions": ["ao.lore.document-ir.v0.1"],
        "selection_metrics": {
            "structural_fidelity": 0.9,
            "text_fidelity": 0.95,
            "source_location_fidelity": 0.8,
            "reliability": 0.99,
            "deterministic_repeatability": 1.0,
            "latency": 0.1,
            "memory": 0.1,
            "external_cost": 0.0,
        },
        "benchmark_result_refs": [DIGEST],
    }
    value.update(overrides)
    return value


def request(**overrides):
    value = {
        "media_type": "text/markdown",
        "extension": ".md",
        "required_structural_elements": ["headings", "links"],
        "canonical_ir_version": "ao.lore.document-ir.v0.1",
        "network_allowed": False,
        "license_allowlist": ["MIT", "Apache-2.0"],
        "sandbox_required": True,
        "file_bytes": 500,
    }
    value.update(overrides)
    return value


def selection_profile():
    return {
        "profile_id": "markdown-general-v1",
        "version": "1",
        "weights": {
            "format_compatibility": 0.1,
            "required_feature_coverage": 0.2,
            "structural_fidelity": 0.2,
            "text_fidelity": 0.2,
            "source_location_fidelity": 0.1,
            "reliability": 0.1,
            "deterministic_repeatability": 0.1,
        },
        "penalty_weights": {"latency": 0.1, "memory": 0.05, "external_cost": 0.2},
        "normalization_bounds": {
            "latency": [0, 1], "memory": [0, 1], "external_cost": [0, 1]
        },
        "missing_optional_policy": "ignore_and_renormalize",
        "tie_break_order": [
            "required_feature_coverage",
            "structural_fidelity",
            "source_location_fidelity",
            "deterministic_repeatability",
            "lower_external_cost",
            "lower_latency",
            "parser_id",
        ],
        "fixture_corpus_digest": DIGEST,
    }


class EligibilityTests(unittest.TestCase):
    def test_hard_requirements_reject_missing_and_unsafe_capabilities(self):
        profile = capability(
            supported_mime_types=["application/pdf"],
            supported_extensions=[".pdf"],
            structural_elements=["headings"],
            deterministic_offline=False,
            frontier_service_required=True,
            runtime_available=False,
            license_id="Proprietary",
            sandbox_supported=False,
            max_file_bytes=100,
            canonical_ir_versions=[],
        )
        reasons = evaluate_eligibility(profile, request())
        self.assertEqual(
            reasons,
            [
                "unsupported_media_type:text/markdown",
                "unsupported_extension:.md",
                "missing_structural_element:links",
                "unsupported_ir_version:ao.lore.document-ir.v0.1",
                "network_policy_violation",
                "runtime_unavailable",
                "license_not_allowed:Proprietary",
                "sandbox_requirement_unsatisfied",
                "file_limit_exceeded:500>100",
            ],
        )

    def test_missing_required_capability_is_not_neutral(self):
        profile = capability()
        del profile["runtime_available"]
        self.assertIn("runtime_availability_unknown", evaluate_eligibility(profile, request()))


class SelectionTests(unittest.TestCase):
    def test_selects_highest_numeric_score_and_explains_rejections(self):
        weaker = capability(
            "weaker",
            selection_metrics={
                **capability()["selection_metrics"],
                "structural_fidelity": 0.4,
                "text_fidelity": 0.5,
            },
        )
        rejected = capability("online-only", frontier_service_required=True)
        report = select_parser(DIGEST, [weaker, capability(), rejected], request(), selection_profile())
        self.assertEqual(report["selected_parser"], "native-markdown")
        self.assertEqual(report["eligible_parsers"], ["native-markdown", "weaker"])
        self.assertEqual(report["rejected_parsers"][0]["parser_id"], "online-only")
        self.assertIn("selection_score", report["candidates"][0])
        self.assertNotIn("overall_quality_score", report["candidates"][0])
        self.assertEqual(report["benchmark_evidence"], [DIGEST])

    def test_ties_are_deterministic_and_docling_has_no_special_priority(self):
        a = capability("aaa-challenger")
        docling = capability("docling")
        report = select_parser(DIGEST, [docling, a], request(), selection_profile())
        self.assertEqual(report["selected_parser"], "aaa-challenger")
        self.assertTrue(report["tie_break"]["applied"])

    def test_non_finite_and_unbounded_values_fail_closed(self):
        bad = capability(selection_metrics={**capability()["selection_metrics"], "reliability": math.nan})
        with self.assertRaises(ScoringError):
            select_parser(DIGEST, [bad], request(), selection_profile())


class ParseQualityTests(unittest.TestCase):
    def profile(self):
        return {
            "profile_id": "markdown-quality-v1",
            "component_weights": {"text_coverage": 0.5, "structure": 0.3, "tables": 0.2},
            "accept_threshold": 0.8,
            "fallback_threshold": 0.6,
            "maximum_fallbacks": 1,
            "critical_failures": ["invalid_ir", "source_digest_mismatch", "no_readable_content"],
            "calibration_result_digest": DIGEST,
            "intermediate_decision": "quarantine",
            "exhausted_decision": "reject",
        }

    def test_non_applicable_components_are_renormalized(self):
        report = compute_parse_quality(
            DIGEST, "native-markdown", {"text_coverage": 0.9, "structure": 0.8, "tables": None}, self.profile()
        )
        self.assertAlmostEqual(report["overall_quality_score"], 0.8625)
        self.assertEqual(report["decision"], "accept")
        self.assertEqual(report["components"]["tables"]["applicable"], False)
        self.assertNotIn("selection_score", report)

    def test_low_score_falls_back_once_then_rejects(self):
        components = {"text_coverage": 0.3, "structure": 0.4, "tables": None}
        self.assertEqual(compute_parse_quality(DIGEST, "a", components, self.profile(), 0)["decision"], "fallback")
        self.assertEqual(compute_parse_quality(DIGEST, "a", components, self.profile(), 1)["decision"], "reject")

    def test_critical_failure_overrides_a_high_numeric_score(self):
        report = compute_parse_quality(
            DIGEST,
            "a",
            {"text_coverage": 1, "structure": 1, "tables": None},
            self.profile(),
            critical_failures=["invalid_ir"],
        )
        self.assertEqual(report["decision"], "fallback")
        self.assertFalse(report["passed"])


class CoverageTests(unittest.TestCase):
    def requirements(self):
        return [
            {"id": "current-policy", "importance_weight": 3, "mandatory": True, "status": "satisfied", "evidence_refs": ["brain/policy.md"]},
            {"id": "comparison", "importance_weight": 1, "mandatory": False, "status": "partially_satisfied", "evidence_refs": ["brain/a.md"]},
        ]

    def test_coverage_uses_declared_mapping_and_all_hard_gates(self):
        report = compute_evidence_coverage(
            DIGEST,
            self.requirements(),
            {
                "profile_id": "decision-v1",
                "target": 0.8,
                "satisfaction_values": {"satisfied": 1, "partially_satisfied": 0.5, "missing": 0, "contradictory": 0, "stale_only": 0, "disqualified": 0},
            },
            {
                "provenance_present": True,
                "citations_present": True,
                "freshness_met": True,
                "trust_met": True,
                "no_hard_contradiction": True,
                "deterministic_validation_passed": True,
            },
        )
        self.assertEqual(report["coverage"], 0.875)
        self.assertEqual(report["decision"], "answer")
        self.assertTrue(all(report["gates"].values()))

    def test_depth_exhaustion_does_not_turn_weak_coverage_into_success(self):
        reqs = self.requirements()
        reqs[0]["status"] = "missing"
        report = compute_evidence_coverage(
            DIGEST,
            reqs,
            {"profile_id": "x", "target": 0.8, "satisfaction_values": {"satisfied": 1, "partially_satisfied": 0.5, "missing": 0, "contradictory": 0, "stale_only": 0, "disqualified": 0}},
            {"provenance_present": True, "citations_present": True, "freshness_met": True, "trust_met": True, "no_hard_contradiction": True, "deterministic_validation_passed": True},
            budget_exhausted=True,
        )
        self.assertEqual(report["decision"], "partial")
        self.assertFalse(report["gates"]["mandatory_requirements_met"])

    def test_missing_gate_fails_closed(self):
        with self.assertRaises(ScoringError):
            compute_evidence_coverage(DIGEST, self.requirements(), {"profile_id": "x", "target": 0.8, "satisfaction_values": {"satisfied": 1}}, {})

    def test_branch_priority_is_gain_per_cost(self):
        self.assertEqual(branch_priority(0.5, 2), 0.25)
        with self.assertRaises(ScoringError):
            branch_priority(1, 0)


if __name__ == "__main__":
    unittest.main()
