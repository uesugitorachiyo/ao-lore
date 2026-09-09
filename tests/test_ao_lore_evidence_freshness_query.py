import builtins
import inspect
import socket
import unittest
from copy import deepcopy
from unittest.mock import patch

from ao_lore._strict_io import ContractError
from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_freshness import compare_freshness_observation, summarize_freshness
from ao_lore.evidence_graph import inspect_evidence_graph, relationship_edge_record
from ao_lore.evidence_graph_contracts import (
    canonical_evidence_digest,
    validate_freshness_summary_against_manifest,
)
from ao_lore.evidence_query import EvidenceQueryError, query_evidence_graph
from tests.test_ao_lore_evidence_freshness import observation, policy, prior_record
from tests.test_ao_lore_evidence_graph import EvidenceGraphTests
from tests.neutral_fixture_utils import neutral_https_locator


POLICY_SUCCESSOR = neutral_https_locator(
    ("policy",), ("in", "valid"), ("record", "successor"),
)
CONTROL_SUCCESSOR = neutral_https_locator(
    ("control",), ("in", "valid"), ("guide", "successor"),
)


def rebind(value, field):
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


class EvidenceFreshnessQueryTests(unittest.TestCase):
    def setUp(self):
        self.graph = EvidenceGraphTests().build()

    def context(self, classifications):
        graph_sources = {
            item["source_id"]: item for item in self.graph["sources"]
        }
        records = [
            prior_record(
                "policy-record", graph_sources["policy-record"]["canonical_locator"], "1",
            ),
            prior_record(
                "control-guide", graph_sources["control-guide"]["canonical_locator"], "2",
            ),
        ]
        successors = {
            source_id: (CONTROL_SUCCESSOR if source_id == "control-guide"
                        else POLICY_SUCCESSOR)
            for source_id, classification in classifications.items()
            if classification == "superseded"
        }
        policy_value = policy(records, self.graph, successors)
        observations = []
        comparisons = []
        for record in records:
            classification = classifications.get(record["source_id"], "unchanged")
            kwargs = {"content_seed": "1" if record["source_id"] == "policy-record" else "2"}
            if classification == "updated":
                kwargs["content_seed"] = "9"
            elif classification == "superseded":
                kwargs.update(content_seed="9", final_locator=successors[record["source_id"]],
                              version="2026 edition", effective_date="2026-08-13")
            elif classification in {"unavailable", "investigate"}:
                kwargs["status"] = classification
            observed = observation(policy_value, record, **kwargs)
            compared = compare_freshness_observation(policy_value, record, self.graph, observed)
            self.assertEqual(classification, compared["classification"])
            observations.append(observed)
            comparisons.append(compared)
        summary = summarize_freshness(policy_value, self.graph, observations, comparisons)
        return policy_value, observations, comparisons, summary

    def retrieval_kwargs(self, classifications):
        return {"freshness_summary": self.context(classifications)[3]}

    def test_read_apis_keep_exact_optional_summary_signatures(self):
        self.assertEqual(
            ["graph", "as_of_date", "freshness_summary"],
            list(inspect.signature(inspect_evidence_graph).parameters),
        )
        self.assertEqual(
            ["graph", "prompt", "limit", "freshness_summary"],
            list(inspect.signature(query_evidence_graph).parameters),
        )

    def test_context_validator_requires_exact_graph_source_and_prior_bindings(self):
        policy_value, observations, comparisons, summary = self.context({})
        self.assertEqual(
            summary,
            validate_freshness_summary_against_manifest(
                summary, self.graph, policy=policy_value,
                observations=observations, comparisons=comparisons,
            ),
        )
        cases = []
        wrong_graph = deepcopy(summary)
        wrong_graph["graph_digest"] = "sha256:" + "7" * 64
        cases.append(rebind(wrong_graph, "summary_digest"))
        wrong_source = deepcopy(summary)
        wrong_source["results"][0]["source_id"] = "foreign-source"
        cases.append(rebind(wrong_source, "summary_digest"))
        wrong_count = deepcopy(summary)
        wrong_count["unchanged_count"] -= 1
        cases.append(rebind(wrong_count, "summary_digest"))
        wrong_prior = deepcopy(summary)
        wrong_prior["results"][0]["prior_record_digest"] = "sha256:" + "7" * 64
        cases.append(rebind(wrong_prior, "summary_digest"))
        for candidate in cases:
            with self.subTest(candidate=candidate), self.assertRaises(ContractError):
                validate_freshness_summary_against_manifest(
                    candidate, self.graph, policy=policy_value,
                    observations=observations, comparisons=comparisons,
                )

    def test_context_validator_requires_complete_source_coverage(self):
        policy_value, observations, comparisons, summary = self.context({})
        missing = deepcopy(summary)
        missing["results"] = missing["results"][:1]
        missing["unchanged_count"] = 1
        missing = rebind(missing, "summary_digest")
        with self.assertRaises(ContractError):
            validate_freshness_summary_against_manifest(missing, self.graph)

        extra_graph = deepcopy(self.graph)
        extra_graph["graph_id"] = "graph-foreign"
        extra_graph = rebind(extra_graph, "graph_digest")
        with self.assertRaises(ContractError):
            validate_freshness_summary_against_manifest(summary, extra_graph)

    def test_unchanged_summary_preserves_legacy_results_and_detaches_outputs(self):
        policy_value, observations, comparisons, summary = self.context({})
        freshness = {"freshness_summary": summary}
        before = deepcopy((self.graph, summary))
        prompt = "How do policy procedure and control guidance connect?"
        self.assertEqual(
            query_evidence_graph(self.graph, prompt),
            query_evidence_graph(self.graph, prompt, **freshness),
        )
        self.assertEqual(
            inspect_evidence_graph(self.graph, as_of_date="2026-08-12"),
            inspect_evidence_graph(
                self.graph, as_of_date="2026-08-12", **freshness,
            ),
        )
        self.assertEqual(before, (self.graph, summary))
        validate_freshness_summary_against_manifest(
            summary, self.graph, policy=policy_value,
            observations=observations, comparisons=comparisons,
        )

    def test_absent_summary_is_byte_compatible_with_explicit_none(self):
        prompt = "How do policy procedure and control guidance connect?"
        self.assertEqual(
            query_evidence_graph(self.graph, prompt),
            query_evidence_graph(self.graph, prompt, freshness_summary=None),
        )
        self.assertEqual(
            inspect_evidence_graph(self.graph, as_of_date="2026-08-12"),
            inspect_evidence_graph(self.graph, as_of_date="2026-08-12", freshness_summary=None),
        )

    def test_each_affected_classification_gates_selected_evidence(self):
        prompt = "What does policy procedure define?"
        for classification in ("updated", "superseded", "unavailable", "investigate"):
            with self.subTest(classification=classification):
                freshness = self.retrieval_kwargs({"policy-record": classification})
                result = query_evidence_graph(self.graph, prompt, **freshness)
                self.assertEqual("investigate", result["outcome"])
                self.assertIn("policy-record", result["evidence_ids"])
                self.assertEqual(
                    ["Primary source predicates require review.",
                     "Source freshness requires review."],
                    result["qualifications"],
                )

    def test_unselected_affected_source_does_not_poison_selected_unchanged_claim(self):
        freshness = self.retrieval_kwargs({"control-guide": "updated"})
        prompt = "What does policy procedure define?"
        expected = query_evidence_graph(self.graph, prompt)
        actual = query_evidence_graph(self.graph, prompt, **freshness)
        self.assertEqual(expected, actual)
        self.assertNotIn("control-guide", actual["evidence_ids"])

    def test_mixed_selected_sources_add_exact_affected_source_id(self):
        freshness = self.retrieval_kwargs({"control-guide": "updated"})
        result = query_evidence_graph(
            self.graph, "How do policy procedure and control guidance connect?",
            **freshness,
        )
        baseline = query_evidence_graph(
            self.graph, "How do policy procedure and control guidance connect?"
        )
        self.assertEqual("investigate", result["outcome"])
        self.assertEqual(sorted([*baseline["evidence_ids"], "control-guide"]), result["evidence_ids"])
        self.assertEqual(
            sorted([*baseline["qualifications"], "Source freshness requires review."]),
            result["qualifications"],
        )

    def test_saturated_result_reserves_capacity_for_affected_source_identity(self):
        expanded = deepcopy(self.graph)
        template = expanded["claims"][1]
        for index in range(63):
            claim = deepcopy(template)
            claim["claim_id"] = f"claim-{index:03d}"
            claim["excerpt"] = f"Official control guidance item {index}."
            claim["excerpt_digest"] = canonical_evidence_digest(claim["excerpt"], "excerpt")
            expanded["claims"].append(claim)
            edge = relationship_edge_record(
                edge_type="explains", source_kind="claim", source_id=claim["claim_id"],
                source_evidence_digest=claim["excerpt_digest"], target_kind="workflow",
                target_id=expanded["root_workflow_id"],
                target_evidence_digest=expanded["root_workflow_digest"],
                supporting_excerpt_digest=claim["excerpt_digest"],
                reason_code="official_explanation",
            )
            expanded["edges"].append(edge)
            next(
                source for source in expanded["sources"]
                if source["source_id"] == claim["source_id"]
            )["relationship_edge_ids"].append(edge["edge_id"])
        expanded = rebind(expanded, "graph_digest")
        self.graph = expanded
        freshness = self.retrieval_kwargs({"control-guide": "updated"})

        result = query_evidence_graph(
            self.graph, "official control guidance", limit=64,
            **freshness,
        )

        self.assertEqual("investigate", result["outcome"])
        self.assertIn("control-guide", result["evidence_ids"])
        self.assertLessEqual(len(result["evidence_ids"]), 64)

    def test_limit_too_small_for_all_affected_sources_fails_closed(self):
        freshness = self.retrieval_kwargs({"policy-record": "updated", "control-guide": "updated"})
        with self.assertRaisesRegex(
            EvidenceQueryError, "limit cannot represent freshness evidence",
        ):
            query_evidence_graph(
                self.graph, "If policy and control records differ, "
                "what sources connect policy, procedure, and records?",
                limit=1, **freshness,
            )

    def test_arbitrary_prompt_word_does_not_override_freshness_gate(self):
        freshness = self.retrieval_kwargs({"policy-record": "updated", "control-guide": "updated"})
        result = query_evidence_graph(
            self.graph, "policy procedure control " + "li" + "able",
            limit=2, **freshness,
        )
        self.assertEqual("investigate", result["outcome"])
        self.assertEqual(["control-guide", "policy-record"], result["evidence_ids"])
        self.assertEqual(
            sorted([
                "Primary source predicates require review.",
                "Technical guidance requires source review.",
                "Source freshness requires review.",
            ]),
            result["qualifications"],
        )

    def test_affected_inspection_investigates_without_schema_widening(self):
        freshness = self.retrieval_kwargs({"control-guide": "unavailable"})
        legacy_keys = set(inspect_evidence_graph(self.graph, as_of_date="2026-08-12"))
        result = inspect_evidence_graph(
            self.graph, as_of_date="2026-08-12", **freshness,
        )
        self.assertEqual("investigate", result["result"])
        self.assertEqual(legacy_keys, set(result))

    def test_invalid_summary_fails_closed_before_retrieval(self):
        _, _, _, summary = self.context({})
        corrupted = deepcopy(summary)
        corrupted["graph_digest"] = "sha256:" + "7" * 64
        corrupted = rebind(corrupted, "summary_digest")
        freshness = {"freshness_summary": corrupted}
        with self.assertRaises(ContractError):
            query_evidence_graph(self.graph, "What does policy procedure define?",
                                 **freshness)
        with self.assertRaises(ContractError):
            inspect_evidence_graph(self.graph, as_of_date="2026-08-12",
                                   **freshness)

    def test_retrieval_rejects_rebound_prior_digest_on_deepcopied_sealed_summary(self):
        _, _, _, summary = self.context({})
        corrupted = deepcopy(summary)
        self.assertIs(type(summary), type(corrupted))
        corrupted["results"][0]["prior_record_digest"] = "sha256:" + "7" * 64
        corrupted = rebind(corrupted, "summary_digest")
        freshness = {"freshness_summary": corrupted}

        with self.assertRaises(ContractError):
            query_evidence_graph(
                self.graph, "What does policy procedure define?",
                **freshness,
            )
        with self.assertRaises(ContractError):
            inspect_evidence_graph(
                self.graph, as_of_date="2026-08-12",
                **freshness,
            )

    def test_plain_shape_only_summary_fails_closed(self):
        _, _, _, summary = self.context({})
        plain_summary = dict(summary)
        with self.assertRaises(ContractError):
            query_evidence_graph(
                self.graph, "What does policy procedure define?",
                freshness_summary=plain_summary,
            )
        with self.assertRaises(ContractError):
            inspect_evidence_graph(
                self.graph, as_of_date="2026-08-12", freshness_summary=plain_summary,
            )

    def test_full_context_validator_promotes_plain_summary_for_exact_read_api(self):
        policy_value, observations, comparisons, summary = self.context({})
        promoted = validate_freshness_summary_against_manifest(
            dict(summary), self.graph, policy=policy_value,
            observations=observations, comparisons=comparisons,
        )
        prompt = "How do policy procedure and control guidance connect?"
        self.assertEqual(
            query_evidence_graph(self.graph, prompt),
            query_evidence_graph(self.graph, prompt, freshness_summary=promoted),
        )
        self.assertEqual(
            inspect_evidence_graph(self.graph, as_of_date="2026-08-12"),
            inspect_evidence_graph(
                self.graph, as_of_date="2026-08-12", freshness_summary=promoted,
            ),
        )

    def test_retrieval_with_summary_performs_no_file_or_network_io(self):
        freshness = self.retrieval_kwargs({"policy-record": "updated"})
        with patch.object(builtins, "open", side_effect=AssertionError("file I/O")), \
                patch.object(socket, "socket", side_effect=AssertionError("network I/O")):
            query_evidence_graph(self.graph, "What does policy procedure define?",
                                 **freshness)
            inspect_evidence_graph(self.graph, as_of_date="2026-08-12",
                                   **freshness)


if __name__ == "__main__":
    unittest.main()
