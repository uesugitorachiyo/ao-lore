import json
import unittest
from copy import deepcopy

from ao_lore._strict_io import ContractError
from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_freshness import compare_freshness_observation, summarize_freshness
from ao_lore.evidence_graph_contracts import AUTHORITY_FIELDS, canonical_evidence_digest
from tests.neutral_fixture_utils import neutral_public_https_locator


SOURCE_LOCATOR = neutral_public_https_locator(("source",), ("guide",))
SUCCESSOR_LOCATOR = neutral_public_https_locator(("source",), ("guide", "successor"))
OTHER_LOCATOR = neutral_public_https_locator(("source",), ("other",))


def digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def authority() -> dict[str, bool]:
    return {field: False for field in AUTHORITY_FIELDS}


def bind(value: dict, field: str) -> dict:
    result = deepcopy(value)
    result[field] = canonical_digest({key: item for key, item in result.items() if key != field})
    return result


def source(source_id="source-one", locator=SOURCE_LOCATOR, seed="a") -> dict:
    return {
        "source_id": source_id, "source_digest": digest(seed),
        "canonical_locator": locator, "retrieved_at": "2026-08-12T18:00:00Z",
        "publisher": "Synthetic Register", "jurisdiction": "Synthetic scope",
        "authority_role": "technical_guidance", "version": "2025 edition",
        "effective_date": "2025-01-01", "operational_question_ids": ["question-one"],
        "relationship_edge_ids": ["edge-one"], "status": "current",
        "media_type": "text/html", "retained_artifact_digests": [digest(seed)],
    }


def graph(sources=None) -> dict:
    sources = deepcopy(sources or [source()])
    excerpt = "Official retained guidance."
    excerpt_digest = canonical_evidence_digest(excerpt, "excerpt")
    claims = []
    edges = []
    for index, claim_source in enumerate(sources):
        suffix = "one" if index == 0 else claim_source["source_id"]
        claim_id = f"claim-{suffix}"
        claim = {
            "claim_id": claim_id, "source_id": claim_source["source_id"],
            "source_digest": claim_source["source_digest"],
            "authority_role": claim_source["authority_role"], "excerpt": excerpt,
            "excerpt_digest": excerpt_digest, "citation_anchor": "section one",
            "operational_question_ids": ["question-one"],
            "subject_terms": ["guidance"], "semantic_reason_codes": ["topic_match"],
        }
        claims.append(claim)
        workflow_edge_id = f"edge-{suffix}"
        source_edge_id = f"edge-{suffix}-source"
        edges.extend((
            bind({
                "schema_version": "ao.lore.evidence-relationship-edge.v0.1",
                "edge_id": workflow_edge_id, "edge_type": "explains",
                "source_kind": "claim", "source_id": claim_id,
                "source_evidence_digest": excerpt_digest,
                "target_kind": "workflow", "target_id": "workflow-one",
                "target_evidence_digest": digest("9"),
                "supporting_excerpt_digest": excerpt_digest,
                "reason_code": "official_explanation", "qualification": None,
            }, "edge_digest"),
            bind({
                "schema_version": "ao.lore.evidence-relationship-edge.v0.1",
                "edge_id": source_edge_id, "edge_type": "cites",
                "source_kind": "source", "source_id": claim_source["source_id"],
                "source_evidence_digest": claim_source["source_digest"],
                "target_kind": "claim", "target_id": claim_id,
                "target_evidence_digest": excerpt_digest,
                "supporting_excerpt_digest": excerpt_digest,
                "reason_code": "source_citation", "qualification": None,
            }, "edge_digest"),
        ))
        claim_source["relationship_edge_ids"] = [workflow_edge_id, source_edge_id]
    question = bind({
        "schema_version": "ao.lore.evidence-operational-question.v0.1",
        "question_id": "question-one", "prompt": "What does the guidance require?",
        "expected_outcome": "answer", "required_authority_roles": ["technical_guidance"],
        "required_evidence_ids": [item["claim_id"] for item in claims],
        "forbidden_evidence_ids": [],
        "qualifications": [],
    }, "question_digest")
    return bind({
        "schema_version": "ao.lore.evidence-graph-manifest.v0.1", "graph_id": "graph-one",
        "root_workflow_id": "workflow-one", "root_workflow_digest": digest("9"),
        "source_registry_digest": digest("8"), "sources": sources,
        "claims": claims, "edges": edges, "operational_questions": [question],
        **authority(),
    }, "graph_digest")


def prior_record(source_id="source-one", locator=SOURCE_LOCATOR, seed="a") -> dict:
    return bind({
        "schema_version": "ao.lore.evidence-acquisition-record.v0.1",
        "acquisition_id": f"acquisition-{source_id}", "source_id": source_id,
        "requested_locator": locator, "final_locator": locator,
        "retrieved_at": "2026-08-12T18:00:00Z", "status": "verified",
        "http_status": 200, "media_type": "text/html", "byte_count": 1024,
        "content_digest": digest(seed), "redirect_chain": [], **authority(),
    }, "record_digest")


def policy(records=None, graph_value=None, successors=None) -> dict:
    records = records or [prior_record()]
    graph_value = graph_value or graph()
    successors = successors or {}
    return bind({
        "schema_version": "ao.lore.evidence-freshness-policy.v0.1",
        "policy_id": "policy-one", "bundle_id": "bundle-one",
        "graph_id": graph_value["graph_id"], "graph_digest": graph_value["graph_digest"],
        "source_registry_digest": graph_value["source_registry_digest"],
        "verification_interval_seconds": 86400,
        "maximum_observation_age_seconds": 2592000,
        "maximum_observation_bytes": 4096, "maximum_redirect_hops": 2,
        "allowed_media_types": ["application/pdf", "text/html"],
        "allowed_terminal_classifications": [
            "unchanged", "updated", "superseded", "unavailable", "investigate",
        ],
        "sources": [{
            "source_id": record["source_id"], "canonical_locator": record["requested_locator"],
            "prior_record_digest": record["record_digest"],
            "prior_content_digest": record["content_digest"],
            "prior_media_type": record["media_type"],
            "declared_successor_locator": successors.get(record["source_id"]),
        } for record in records], **authority(),
    }, "policy_digest")


def observation(policy_value, record, *, status="observed", content_seed="a",
                final_locator=None, media_type="text/html", version="2025 edition",
                effective_date="2025-01-01", observed_at="2026-08-13T18:00:00Z") -> dict:
    successful = status == "observed"
    final_locator = final_locator if final_locator is not None else record["requested_locator"]
    return bind({
        "schema_version": "ao.lore.evidence-freshness-observation.v0.1",
        "observation_id": f"observation-{record['source_id']}",
        "policy_id": policy_value["policy_id"], "bundle_id": policy_value["bundle_id"],
        "graph_id": policy_value["graph_id"], "graph_digest": policy_value["graph_digest"],
        "source_id": record["source_id"], "prior_record_digest": record["record_digest"],
        "requested_locator": record["requested_locator"],
        "final_locator": final_locator if successful else None, "observed_at": observed_at,
        "status": status, "http_status": 200 if successful else 410,
        "media_type": media_type if successful else None,
        "byte_count": 2048 if successful else 0,
        "content_digest": digest(content_seed) if successful else None,
        "redirect_chain": [] if final_locator == record["requested_locator"] else [final_locator],
        "version": version, "effective_date": effective_date, **authority(),
    }, "observation_digest")


class EvidenceFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.record = prior_record()
        self.graph = graph()
        self.policy = policy([self.record], self.graph)

    def compare(self, observed):
        return compare_freshness_observation(
            self.policy, self.record, self.graph, observed,
        )

    def test_comparison_binds_the_complete_graph_manifest(self):
        observed = observation(self.policy, self.record)
        self.assertEqual("unchanged", self.compare(observed)["classification"])
        mutations = (
            ("status", "historical"),
            ("version", "tampered edition"),
            ("effective_date", "2024-01-01"),
            ("operational_question_ids", ["question-other"]),
            ("relationship_edge_ids", ["edge-other"]),
        )
        for field, replacement in mutations:
            with self.subTest(field=field):
                tampered = deepcopy(self.graph)
                tampered["sources"][0][field] = replacement
                tampered = bind(tampered, "graph_digest")
                with self.assertRaises(ContractError):
                    compare_freshness_observation(
                        self.policy, self.record, tampered, observed,
                    )

    def test_policy_bound_noncurrent_graph_source_requires_investigation(self):
        historical = deepcopy(self.graph)
        historical["sources"][0]["status"] = "historical"
        historical = bind(historical, "graph_digest")
        bound_policy = policy([self.record], historical)
        observed = observation(bound_policy, self.record)
        result = compare_freshness_observation(
            bound_policy, self.record, historical, observed,
        )
        self.assertEqual("investigate", result["classification"])
        self.assertIn("prior_binding_drift", result["reason_codes"])

    def test_byte_identical_and_changed_content_have_closed_classifications(self):
        unchanged = self.compare(observation(self.policy, self.record))
        updated = self.compare(observation(self.policy, self.record, content_seed="b"))
        self.assertEqual(("unchanged", ["content_unchanged"]),
                         (unchanged["classification"], unchanged["reason_codes"]))
        self.assertEqual(("updated", ["content_changed"]),
                         (updated["classification"], updated["reason_codes"]))

    def test_verified_declared_successor_requires_unambiguous_bound_evidence(self):
        successor = SUCCESSOR_LOCATOR
        self.policy = policy([self.record], self.graph, {"source-one": successor})
        result = self.compare(observation(
            self.policy, self.record, final_locator=successor, content_seed="b",
            version="2026 edition", effective_date="2026-01-01",
        ))
        self.assertEqual("superseded", result["classification"])
        self.assertEqual(["declared_successor_verified", "redirect_drift", "locator_drift"], result["reason_codes"])

    def test_terminal_unavailable_precedes_other_nonbinding_conditions(self):
        result = self.compare(observation(self.policy, self.record, status="unavailable"))
        self.assertEqual(("unavailable", ["terminal_unavailable"]),
                         (result["classification"], result["reason_codes"]))

    def test_terminal_unavailable_ignores_partial_version_metadata(self):
        for version, effective_date in ((None, "2026-01-01"), ("2026 edition", None)):
            with self.subTest(version=version, effective_date=effective_date):
                result = self.compare(observation(
                    self.policy, self.record, status="unavailable",
                    version=version, effective_date=effective_date,
                ))
                self.assertEqual(("unavailable", ["terminal_unavailable"]),
                                 (result["classification"], result["reason_codes"]))

    def test_drift_reasons_remain_visible_even_when_bytes_match(self):
        drifted = observation(
            self.policy, self.record, final_locator=OTHER_LOCATOR, media_type="application/pdf",
        )
        result = self.compare(drifted)
        self.assertEqual("investigate", result["classification"])
        self.assertEqual(
            ["redirect_drift", "media_type_drift", "locator_drift"], result["reason_codes"],
        )

    def test_ambiguous_version_or_effective_date_requires_investigation(self):
        successor = SUCCESSOR_LOCATOR
        self.policy = policy([self.record], self.graph, {"source-one": successor})
        cases = ((None, "2026-01-01", "version_ambiguous"),
                 ("2026 edition", None, "effective_date_ambiguous"))
        for version, effective_date, reason in cases:
            with self.subTest(reason=reason):
                result = self.compare(observation(
                    self.policy, self.record, final_locator=successor,
                    version=version, effective_date=effective_date,
                ))
                self.assertEqual("investigate", result["classification"])
                self.assertIn(reason, result["reason_codes"])

    def test_each_prior_and_observation_binding_drifts_to_investigate(self):
        mutations = (
            ("prior", "content_digest", digest("b")),
            ("prior", "requested_locator", OTHER_LOCATOR),
            ("prior", "source_id", "source-other"),
            ("observation", "prior_record_digest", digest("7")),
            ("observation", "requested_locator", OTHER_LOCATOR),
            ("observation", "source_id", "source-other"),
            ("observation", "graph_digest", digest("7")),
        )
        for target, field, replacement in mutations:
            with self.subTest(target=target, field=field):
                record = deepcopy(self.record)
                observed = observation(self.policy, self.record)
                if target == "prior":
                    record[field] = replacement
                    record = bind(record, "record_digest")
                else:
                    observed[field] = replacement
                    observed = bind(observed, "observation_digest")
                result = compare_freshness_observation(
                    self.policy, record, self.graph, observed,
                )
                self.assertEqual("investigate", result["classification"])
                self.assertIn("prior_binding_drift", result["reason_codes"])

    def test_unsafe_observation_has_highest_precedence(self):
        observed = observation(self.policy, self.record, status="investigate")
        result = self.compare(observed)
        self.assertEqual(("investigate", ["unsafe_observation"]),
                         (result["classification"], result["reason_codes"]))

    def test_policy_bound_and_nonterminal_observations_are_unsafe(self):
        too_large = observation(self.policy, self.record)
        too_large["byte_count"] = 4097
        too_large = bind(too_large, "observation_digest")
        server_error = observation(self.policy, self.record, status="unavailable")
        server_error["http_status"] = 500
        server_error = bind(server_error, "observation_digest")
        for observed in (too_large, server_error):
            with self.subTest(status=observed["status"]):
                result = self.compare(observed)
                self.assertEqual("investigate", result["classification"])
                self.assertIn("unsafe_observation", result["reason_codes"])

    def test_invalid_inputs_that_cannot_form_a_comparison_fail_closed(self):
        observed = observation(self.policy, self.record)
        observed["extra"] = True
        with self.assertRaises(ContractError):
            self.compare(observed)

    def test_summary_is_exact_sorted_and_derives_counts_timestamp_and_rebuild(self):
        record_two = prior_record("source-two", OTHER_LOCATOR, "b")
        graph_value = graph([source(), source("source-two", OTHER_LOCATOR, "b")])
        policy_value = policy([self.record, record_two], graph_value)
        observations = [
            observation(policy_value, record_two, content_seed="c", observed_at="2026-08-13T19:00:00Z"),
            observation(policy_value, self.record, observed_at="2026-08-13T18:00:00Z"),
        ]
        comparisons = [
            compare_freshness_observation(policy_value, record_two, graph_value, observations[0]),
            compare_freshness_observation(policy_value, self.record, graph_value, observations[1]),
        ]
        result = summarize_freshness(policy_value, graph_value, observations, comparisons)
        self.assertEqual(["source-one", "source-two"], [item["source_id"] for item in result["results"]])
        self.assertEqual("2026-08-13T19:00:00Z", result["observed_at"])
        self.assertEqual((1, 1, True),
                         (result["unchanged_count"], result["updated_count"], result["rebuild_required"]))
        reversed_result = summarize_freshness(
            policy_value, graph_value, list(reversed(observations)), list(reversed(comparisons)),
        )
        self.assertEqual(json.dumps(result, sort_keys=True), json.dumps(reversed_result, sort_keys=True))

    def test_summary_requires_exact_coverage_graph_and_prior_bindings(self):
        observed = observation(self.policy, self.record)
        compared = self.compare(observed)
        with self.assertRaises(ContractError):
            summarize_freshness(self.policy, self.graph, [], [])
        foreign_graph = deepcopy(self.graph)
        foreign_graph["graph_id"] = "graph-other"
        foreign_graph = bind(foreign_graph, "graph_digest")
        with self.assertRaises(ContractError):
            summarize_freshness(self.policy, foreign_graph, [observed], [compared])

    def test_summary_rejects_drifted_observation_context_even_when_self_bound(self):
        for field, replacement in (
            ("graph_digest", digest("7")),
            ("requested_locator", OTHER_LOCATOR),
            ("prior_record_digest", digest("7")),
        ):
            with self.subTest(field=field):
                observed = observation(self.policy, self.record)
                observed[field] = replacement
                observed = bind(observed, "observation_digest")
                compared = self.compare(observed)
                with self.assertRaises(ContractError):
                    summarize_freshness(self.policy, self.graph, [observed], [compared])

    def test_repeat_is_deterministic_and_all_outputs_are_detached(self):
        observed = observation(self.policy, self.record)
        inputs = deepcopy((self.policy, self.record, self.graph, observed))
        first = self.compare(observed)
        second = self.compare(observed)
        self.assertEqual(first, second)
        first["reason_codes"].append("locator_drift")
        self.assertEqual(inputs, (self.policy, self.record, self.graph, observed))
        pristine = self.compare(observed)
        summary = summarize_freshness(self.policy, self.graph, [observed], [pristine])
        summary["results"][0]["reason_codes"].append("locator_drift")
        self.assertEqual(inputs, (self.policy, self.record, self.graph, observed))


if __name__ == "__main__":
    unittest.main()
