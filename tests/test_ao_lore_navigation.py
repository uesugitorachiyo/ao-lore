import unittest

from ao_lore.navigation import (
    CoverageNavigator,
    NavigationError,
    build_requirement_plan,
)


DIGEST = "sha256:" + "f" * 64


def coverage_profile(target=0.8):
    return {
        "profile_id": "decision-support-v1",
        "target": target,
        "satisfaction_values": {
            "satisfied": 1.0,
            "partially_satisfied": 0.5,
            "missing": 0.0,
            "contradictory": 0.0,
            "stale_only": 0.0,
            "disqualified": 0.0,
        },
    }


def plan(max_depth=5, max_nodes=8, max_replans=2):
    return build_requirement_plan(
        DIGEST,
        "decision-support-v1",
        [
            {
                "id": "policy", "criterion": "identify current policy", "importance_weight": 3,
                "evidence_type": "policy", "lifecycle_condition": "current", "trust_tier": "reviewed",
                "citation_required": True, "provenance_required": True, "mandatory": True,
            },
            {
                "id": "procedure", "criterion": "find procedure steps", "importance_weight": 1,
                "evidence_type": "procedure", "lifecycle_condition": "non-stale", "trust_tier": None,
                "citation_required": True, "provenance_required": True, "mandatory": False,
            },
        ],
        {"max_depth": max_depth, "max_nodes": max_nodes, "max_tokens": 1000, "max_seconds": 30, "max_replans": max_replans},
    )


def evidence(requirement_id, status="satisfied", ref=None, **overrides):
    item = {
        "requirement_id": requirement_id,
        "status": status,
        "evidence_ref": ref or f"brain/{requirement_id}.md",
        "provenance_present": True,
        "citation_present": True,
        "freshness_met": True,
        "trust_met": True,
        "hard_contradiction": False,
    }
    item.update(overrides)
    return item


class RequirementPlanTests(unittest.TestCase):
    def test_plan_has_explicit_weighted_requirements_and_safety_budgets(self):
        result = plan()
        self.assertEqual(result["requirements"][0]["status"], "missing")
        self.assertTrue(result["requirements"][0]["mandatory"])
        self.assertEqual(result["budgets"]["max_depth"], 5)

    def test_duplicate_requirement_identity_fails_closed(self):
        duplicate = plan()["requirements"]
        duplicate[1]["id"] = duplicate[0]["id"]
        with self.assertRaises(NavigationError):
            build_requirement_plan(DIGEST, "x", duplicate, plan()["budgets"])


class CoverageNavigationTests(unittest.TestCase):
    def test_coverage_stops_before_maximum_depth(self):
        navigator = CoverageNavigator(plan(max_depth=9), coverage_profile())
        report = navigator.visit(
            "brain/policy.md",
            [evidence("policy"), evidence("procedure")],
            estimated_tokens=20,
            estimated_seconds=1,
        )
        self.assertEqual(report["decision"], "answer")
        self.assertTrue(navigator.should_stop)
        self.assertEqual(navigator.trace()["visited_nodes"], ["brain/policy.md"])
        self.assertEqual(navigator.trace()["remaining_budgets"]["depth"], 8)
        self.assertEqual(navigator.trace()["traversal_after_target"], 0)

    def test_depth_exhaustion_cannot_claim_success(self):
        navigator = CoverageNavigator(plan(max_depth=1), coverage_profile())
        report = navigator.visit("brain/partial.md", [evidence("procedure")], estimated_tokens=10, estimated_seconds=1)
        self.assertEqual(report["decision"], "partial")
        self.assertFalse(navigator.should_stop)
        self.assertFalse(report["gates"]["mandatory_requirements_met"])
        self.assertEqual(navigator.trace()["highest_weight_unresolved_requirement"], "policy")

    def test_branch_choice_uses_expected_gap_gain_per_cost(self):
        navigator = CoverageNavigator(plan(), coverage_profile())
        choice = navigator.choose_branch(
            [
                {"node_id": "deep-cheap", "estimated_cost": 2, "expected_requirement_gains": {"policy": 0.4}},
                {"node_id": "shallow-expensive", "estimated_cost": 10, "expected_requirement_gains": {"policy": 1.0, "procedure": 1.0}},
            ]
        )
        self.assertEqual(choice["node_id"], "deep-cheap")
        self.assertEqual(choice["priority"], 0.6)

    def test_delta_replan_preserves_memory_evidence_and_history(self):
        navigator = CoverageNavigator(plan(max_depth=5), coverage_profile())
        navigator.reject_branch("brain/stale.md", "stale-only")
        navigator.visit("brain/procedure.md", [evidence("procedure")], estimated_tokens=10, estimated_seconds=1)
        before = navigator.trace()
        delta = navigator.replan([{"node_id": "brain/current-policy.md", "targets": ["policy"]}])
        after = navigator.trace()
        self.assertEqual(delta["target_requirement_ids"], ["policy"])
        self.assertEqual(after["visited_nodes"], before["visited_nodes"])
        self.assertEqual(after["rejected_branches"], before["rejected_branches"])
        self.assertEqual(after["coverage_history"], before["coverage_history"])
        self.assertEqual(after["remaining_budgets"]["replans"], before["remaining_budgets"]["replans"] - 1)
        self.assertEqual(len(after["plan_versions"]), 2)

    def test_unknown_requirement_evidence_and_revisit_fail_closed(self):
        navigator = CoverageNavigator(plan(), coverage_profile())
        with self.assertRaises(NavigationError):
            navigator.visit("brain/x.md", [evidence("unknown")], estimated_tokens=1, estimated_seconds=1)
        navigator.visit("brain/x.md", [evidence("procedure")], estimated_tokens=1, estimated_seconds=1)
        with self.assertRaises(NavigationError):
            navigator.visit("brain/x.md", [evidence("procedure")], estimated_tokens=1, estimated_seconds=1)

    def test_hard_contradiction_refuses_even_when_numeric_target_is_met(self):
        navigator = CoverageNavigator(plan(), coverage_profile(target=0.5))
        report = navigator.visit(
            "brain/conflict.md",
            [evidence("policy", status="contradictory", hard_contradiction=True), evidence("procedure")],
            estimated_tokens=10,
            estimated_seconds=1,
        )
        self.assertEqual(report["decision"], "refuse")
        self.assertFalse(report["gates"]["no_hard_contradiction"])

    def test_trace_records_coverage_gain_and_yield(self):
        navigator = CoverageNavigator(plan(), coverage_profile())
        navigator.visit("brain/procedure.md", [evidence("procedure")], estimated_tokens=25, estimated_seconds=1)
        trace = navigator.trace()
        self.assertEqual(len(trace["coverage_history"]), 1)
        self.assertGreater(trace["coverage_history"][0]["gain"], 0)
        self.assertGreater(trace["evidence_yield_per_node"], 0)
        self.assertGreater(trace["tokens_per_coverage_point"], 0)


if __name__ == "__main__":
    unittest.main()
