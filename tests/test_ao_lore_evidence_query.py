import json
import unittest
from copy import deepcopy
from pathlib import Path

from ao_lore.evidence_query import EvidenceQueryError, query_evidence_graph
import ao_lore.evidence_query as evidence_query
from ao_lore.evidence_graph import (
    build_evidence_graph, claim_record, operational_question_record,
    relationship_edge_record,
)
from ao_lore.benchmark import canonical_digest
from tests.test_ao_lore_evidence_graph import EvidenceGraphTests
from tests.neutral_fixture_utils import neutral_locator_from_spec


class EvidenceQueryTests(unittest.TestCase):
    def setUp(self):
        self.helper = EvidenceGraphTests()
        self.graph = self.helper.build()

    def variant_graph(self, *, conflict=False):
        registry, claims, edges, questions, workflow_digest = self.helper.fixture()
        registry["sources"][1]["status"] = "historical"
        if conflict:
            conflict_edge = relationship_edge_record(
                edge_type="conflicts_with", source_kind="claim", source_id=claims[0]["claim_id"],
                source_evidence_digest=claims[0]["excerpt_digest"], target_kind="claim",
                target_id=claims[1]["claim_id"], target_evidence_digest=claims[1]["excerpt_digest"],
                supporting_excerpt_digest=claims[0]["excerpt_digest"], reason_code="documented_conflict")
            edges.append(conflict_edge)
            for source in registry["sources"]: source["relationship_edge_ids"].append(conflict_edge["edge_id"])
        registry["registry_digest"] = canonical_digest({k: v for k, v in registry.items() if k != "registry_digest"})
        return build_evidence_graph(registry, claims, edges, questions,
                                    root_workflow_digest=workflow_digest, as_of_date="2026-08-12")

    def neutral_fixture(self):
        return json.loads(
            (Path(__file__).parent / "fixtures/ao_lore/neutral_evidence_graph/fixture-spec.json").read_text()
        )

    def neutral_graph(self, *, variant="base", reverse_inputs=False, fixture=None):
        fixture = deepcopy(fixture if fixture is not None else self.neutral_fixture())
        graph_spec = fixture["graph"]
        variant_spec = graph_spec["variants"][variant]
        prompts = [item["prompt"] for item in fixture["cases"]]
        questions = [operational_question_record(
            prompt=prompt, expected_outcome="answer", required_authority_roles=[],
            required_evidence_ids=[], forbidden_evidence_ids=[], qualifications=[],
        ) for prompt in prompts]
        question_ids = [item["question_id"] for item in questions]
        source_specs = {item["source_id"]: item for item in graph_spec["sources"]}
        self.assertEqual(set(source_specs), set(variant_spec["source_statuses"]))
        sources = [{
                **{key: value for key, value in spec.items() if key != "locator_components"},
                "canonical_locator": neutral_locator_from_spec(spec["locator_components"]),
                "operational_question_ids": question_ids, "relationship_edge_ids": [],
                "status": variant_spec["source_statuses"][spec["source_id"]],
            } for spec in graph_spec["sources"]]
        source_by = {item["source_id"]: item for item in sources}
        claims = []
        for spec in graph_spec["claims"]:
            claim = claim_record(
                source_id=spec["source_id"], source_digest=source_by[spec["source_id"]]["source_digest"],
                authority_role=source_by[spec["source_id"]]["authority_role"], excerpt=spec["excerpt"],
                citation_anchor=spec["citation_anchor"], operational_question_ids=question_ids,
                subject_terms=spec["subject_terms"], semantic_reason_codes=spec["semantic_reason_codes"],
            )
            self.assertEqual(spec["expected_claim_id"], claim["claim_id"])
            self.assertEqual(spec["expected_excerpt_digest"], claim["excerpt_digest"])
            claims.append(claim)
        workflow_digest = graph_spec["root_workflow_digest_input"]
        edges = []
        for claim in claims:
            edge = relationship_edge_record(
                edge_type="cites", source_kind="source", source_id=claim["source_id"],
                source_evidence_digest=claim["source_digest"], target_kind="claim",
                target_id=claim["claim_id"], target_evidence_digest=claim["excerpt_digest"],
                supporting_excerpt_digest=claim["excerpt_digest"], reason_code="source_citation",
            )
            edges.append(edge)
            edges.append(relationship_edge_record(
                edge_type="explains", source_kind="claim", source_id=claim["claim_id"],
                source_evidence_digest=claim["excerpt_digest"], target_kind="workflow",
                target_id=graph_spec["root_workflow_id"], target_evidence_digest=workflow_digest,
                supporting_excerpt_digest=claim["excerpt_digest"], reason_code="official_explanation",
            ))
        if variant_spec["include_conflict"]:
            conflict_spec = graph_spec["conflict_edge"]
            conflict = relationship_edge_record(**{
                key: value for key, value in conflict_spec.items() if not key.startswith("expected_")
            })
            self.assertEqual(conflict_spec["expected_edge_id"], conflict["edge_id"])
            self.assertEqual(conflict_spec["expected_edge_digest"], conflict["edge_digest"])
            edges.append(conflict)
        for source in sources:
            source["relationship_edge_ids"] = [
                edge["edge_id"] for edge in edges
                if (
                    (edge["source_kind"] == "source" and edge["source_id"] == source["source_id"])
                    or (edge["target_kind"] == "source" and edge["target_id"] == source["source_id"])
                    or (edge["source_kind"] == "claim" and next(item for item in claims if item["claim_id"] == edge["source_id"])["source_id"] == source["source_id"])
                    or (edge["target_kind"] == "claim" and next(item for item in claims if item["claim_id"] == edge["target_id"])["source_id"] == source["source_id"])
                )
            ]
        registry = {
            "schema_version": "ao.lore.evidence-source-registry.v0.1", "registry_id": graph_spec["registry_id"],
            "root_workflow_id": graph_spec["root_workflow_id"], "sources": sources,
            **{field: False for field in ("legal_advice", "property_decision", "candidate_review", "candidate_decision", "promotion", "canonical_query", "provider", "credential", "private_data", "publication", "release", "deployment", "authority_advanced")},
        }
        registry["registry_digest"] = canonical_digest({key: value for key, value in registry.items() if key != "registry_digest"})
        if reverse_inputs:
            sources.reverse(); claims.reverse(); edges.reverse(); questions.reverse()
            registry["sources"] = sources
            registry["registry_digest"] = canonical_digest({key: value for key, value in registry.items() if key != "registry_digest"})
        graph = build_evidence_graph(
            registry, claims, edges, questions,
            root_workflow_digest=workflow_digest, as_of_date=graph_spec["as_of_date"],
        )
        self.assertEqual(variant_spec["expected_graph_id"], graph["graph_id"])
        self.assertEqual(variant_spec["expected_source_registry_digest"], graph["source_registry_digest"])
        self.assertEqual(variant_spec["expected_graph_digest"], graph["graph_digest"])
        return graph

    def current_conflict_graph(self):
        graph = self.neutral_graph(variant="conflict")
        next(
            item for item in graph["sources"]
            if item["source_id"] == "source-procedure-01"
        )["status"] = "current"
        graph["graph_digest"] = canonical_digest({
            key: value for key, value in graph.items()
            if key != "graph_digest"
        })
        return graph

    def typed_conflict_graph(self, source_kind, target_kind):
        graph = self.current_conflict_graph()
        old = next(
            edge for edge in graph["edges"]
            if edge["edge_type"] == "conflicts_with"
        )
        claims = {item["source_id"]: item for item in graph["claims"]}
        sources = {item["source_id"]: item for item in graph["sources"]}
        sources["source-procedure-01"]["authority_role"] = "technical_guidance"
        claims["source-procedure-01"]["authority_role"] = "technical_guidance"
        endpoint = {
            ("source", "source"): (
                ("source", "source-policy-01", sources["source-policy-01"]["source_digest"]),
                ("source", "source-procedure-01", sources["source-procedure-01"]["source_digest"]),
            ),
            ("source", "claim"): (
                ("source", "source-policy-01", sources["source-policy-01"]["source_digest"]),
                ("claim", claims["source-procedure-01"]["claim_id"], claims["source-procedure-01"]["excerpt_digest"]),
            ),
            ("workflow", "claim"): (
                ("workflow", graph["root_workflow_id"], graph["root_workflow_digest"]),
                ("claim", claims["source-policy-01"]["claim_id"], claims["source-policy-01"]["excerpt_digest"]),
            ),
            ("workflow", "workflow"): (
                ("workflow", graph["root_workflow_id"], graph["root_workflow_digest"]),
                ("workflow", graph["root_workflow_id"], graph["root_workflow_digest"]),
            ),
        }[(source_kind, target_kind)]
        left, right = endpoint
        edge = relationship_edge_record(
            edge_type="conflicts_with",
            source_kind=left[0], source_id=left[1], source_evidence_digest=left[2],
            target_kind=right[0], target_id=right[1], target_evidence_digest=right[2],
            supporting_excerpt_digest=claims["source-policy-01"]["excerpt_digest"],
            reason_code="documented_conflict",
        )
        graph["edges"] = [
            edge if item["edge_id"] == old["edge_id"] else item
            for item in graph["edges"]
        ]
        claim_sources = {
            item["claim_id"]: item["source_id"] for item in graph["claims"]
        }
        incident_sources = set()
        for kind, identity, _digest in endpoint:
            if kind == "source":
                incident_sources.add(identity)
            elif kind == "claim":
                incident_sources.add(claim_sources[identity])
        for source in graph["sources"]:
            source["relationship_edge_ids"] = sorted({
                *(item for item in source["relationship_edge_ids"]
                  if item != old["edge_id"]),
                *([edge["edge_id"]] if source["source_id"] in incident_sources else []),
            })
        graph["graph_digest"] = canonical_digest({
            key: value for key, value in graph.items()
            if key != "graph_digest"
        })
        return graph, edge

    def test_neutral_fixture_uses_only_generic_evidence_vocabulary(self):
        path = Path(__file__).parent / "fixtures/ao_lore/neutral_evidence_graph/fixture-spec.json"
        value = json.loads(path.read_text())
        self.assertEqual(
            ["policy", "procedure", "control", "record", "source", "scope"],
            value["expected_vocabulary"],
        )
        self.assertEqual({"answer", "partial", "investigate", "refuse"}, {
            item["expected_outcome"] for item in value["cases"]
        })
        self.assertEqual(4, len(value["graph"]["sources"]))
        self.assertEqual(4, len(value["graph"]["claims"]))
        self.assertTrue(all(
            "canonical_locator" not in item and
            neutral_locator_from_spec(item["locator_components"]).startswith("https")
            for item in value["graph"]["sources"]
        ))
        for case in value["cases"]:
            self.assertIn("expected_evidence_ids", case)
            self.assertIn("expected_authority_roles", case)
            self.assertIn("expected_qualifications", case)
        self.assertFalse(value["expected_hidden_exact_source_anchors_are_retrieval_input"])

    def test_neutral_fixture_declares_exact_builder_oracles_and_scoped_guidance(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/ao_lore/neutral_evidence_graph/fixture-spec.json").read_text())
        graph = fixture["graph"]
        for variant in graph["variants"].values():
            self.assertIn("expected_graph_id", variant)
            self.assertIn("expected_source_registry_digest", variant)
            self.assertIn("expected_graph_digest", variant)
        self.assertTrue(all("expected_claim_id" in item for item in graph["claims"]))
        self.assertIn("expected_edge_id", graph["conflict_edge"])
        self.assertIn("expected_edge_digest", graph["conflict_edge"])
        scoped_cases = [
            item for item in fixture["cases"]
            if "local_enforcement_guidance" in item["expected_authority_roles"]
        ]
        self.assertEqual(1, len(scoped_cases))
        self.assertEqual(
            ["Source scope requires applicability review."],
            scoped_cases[0]["expected_qualifications"],
        )

    def test_every_neutral_prompt_executes_against_built_graph_and_matches_oracle(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/ao_lore/neutral_evidence_graph/fixture-spec.json").read_text())
        graphs = {name: self.neutral_graph(variant=name) for name in fixture["graph"]["variants"]}
        for case in fixture["cases"]:
            with self.subTest(prompt=case["prompt"]):
                graph = graphs[case["variant"]]
                result = query_evidence_graph(graph, case["prompt"])
                self.assertEqual(case["expected_outcome"], result["outcome"])
                self.assertEqual(case["expected_evidence_ids"], result["evidence_ids"])
                self.assertEqual(case["expected_authority_roles"], result["authority_roles"])
                self.assertEqual(case["expected_qualifications"], result["qualifications"])

    def test_neutral_fixture_binding_drift_fails_exact_oracle(self):
        mutations = (
            ("base", lambda fixture: fixture["graph"]["variants"]["base"]["source_statuses"].update(
                {"source-policy-01": "historical"}
            )),
            ("base", lambda fixture: fixture["graph"]["sources"][0].update(source_id="source-drift-01")),
            ("base", lambda fixture: fixture["graph"]["claims"][0].update(expected_claim_id="claim-drift")),
            ("conflict", lambda fixture: fixture["graph"]["conflict_edge"].update(target_id="claim-drift")),
            ("base", lambda fixture: fixture["graph"]["variants"]["base"].update(
                expected_source_registry_digest="sha256:" + "9" * 64
            )),
            ("base", lambda fixture: fixture["graph"]["variants"]["base"].update(
                expected_graph_digest="sha256:" + "9" * 64
            )),
        )
        for variant, mutate in mutations:
            fixture = self.neutral_fixture()
            mutate(fixture)
            with self.subTest(mutation=mutate.__code__.co_firstlineno):
                with self.assertRaises((AssertionError, KeyError)):
                    self.neutral_graph(variant=variant, fixture=fixture)

    def test_retrieval_has_no_closed_domain_intent_router(self):
        inventory = {
            name for name, value in vars(evidence_query).items()
            if callable(value) and getattr(value, "__module__", None) == evidence_query.__name__
        }
        self.assertEqual(
            {"EvidenceQueryError", "_tokens", "_allocate_freshness_evidence", "_selected_evidence_records", "query_evidence_graph"},
            inventory,
        )

    def test_neutral_embedded_question_oracle_never_drives_retrieval(self):
        graph = self.neutral_graph()
        expected = query_evidence_graph(graph, "policy procedure?")
        altered = deepcopy(graph)
        altered["operational_questions"][0]["expected_outcome"] = "refuse"
        altered["operational_questions"][0]["required_evidence_ids"] = []
        altered["operational_questions"][0]["question_digest"] = canonical_digest({
            key: value for key, value in altered["operational_questions"][0].items()
            if key != "question_digest"
        })
        altered["graph_digest"] = canonical_digest({
            key: value for key, value in altered.items() if key != "graph_digest"
        })
        actual = query_evidence_graph(altered, "policy procedure?")
        for field in ("outcome", "evidence_ids", "authority_roles", "qualifications"):
            self.assertEqual(expected[field], actual[field])

    def test_neutral_limits_and_reordering_remain_deterministic(self):
        graph = self.neutral_graph()
        prompt = "policy procedure?"
        self.assertEqual(query_evidence_graph(graph, prompt), query_evidence_graph(graph, prompt, freshness_summary=None))
        with self.assertRaises(EvidenceQueryError): query_evidence_graph(graph, "x" * 1025)
        with self.assertRaises(EvidenceQueryError): query_evidence_graph(graph, prompt, limit=65)

    def test_arbitrary_prompt_words_do_not_override_matching_evidence(self):
        graph = self.neutral_graph()
        baseline = query_evidence_graph(graph, "policy?")
        with_arbitrary_word = query_evidence_graph(graph, "policy " + "li" + "able?")
        for field in ("outcome", "evidence_ids", "authority_roles", "qualifications"):
            self.assertEqual(baseline[field], with_arbitrary_word[field])
        for field in (
            "legal_advice", "property_decision", "candidate_review",
            "candidate_decision", "promotion", "canonical_query", "provider",
            "credential", "private_data", "publication", "release",
            "deployment", "authority_advanced",
        ):
            self.assertIs(False, with_arbitrary_word[field])

    def test_arbitrary_qualification_words_do_not_override_final_roles(self):
        graph = self.neutral_graph()
        baseline = query_evidence_graph(graph, "policy procedure?")
        arbitrary_words = (
            "la" + "ck", "en" + "ough", "rec" + "ords",
            "rec" + "ord", "pre" + "serve", "with" + "out",
        )
        for word in arbitrary_words:
            with self.subTest(word=word):
                result = query_evidence_graph(
                    graph, "policy procedure " + word + "?",
                )
                for field in (
                    "outcome", "evidence_ids", "authority_roles", "qualifications",
                ):
                    self.assertEqual(baseline[field], result[field])

    def test_gate_words_without_matching_evidence_do_not_route_outcomes(self):
        cases = (
            (self.neutral_graph(variant="conflict"), "con" + "flict"),
            (self.variant_graph(), "new" + "er"),
            (self.variant_graph(), "sta" + "le"),
        )
        for graph, word in cases:
            with self.subTest(word=word):
                baseline = query_evidence_graph(graph, "qzeta unmatched")
                result = query_evidence_graph(
                    graph, "qzeta unmatched " + word,
                )
                for field in (
                    "outcome", "evidence_ids", "authority_roles", "qualifications",
                ):
                    self.assertEqual(baseline[field], result[field])

    def test_one_sided_documented_conflicts_project_both_endpoints(self):
        graph = self.current_conflict_graph()
        conflict = next(
            edge for edge in graph["edges"]
            if edge["edge_type"] == "conflicts_with"
        )
        expected_ids = sorted([
            conflict["source_id"], conflict["target_id"],
        ])
        for prompt in ("policy?", "procedure?"):
            with self.subTest(prompt=prompt):
                result = query_evidence_graph(graph, prompt)
                self.assertEqual("investigate", result["outcome"])
                self.assertEqual(expected_ids, result["evidence_ids"])
                self.assertEqual(
                    ["operator_procedure", "case_evidence"],
                    result["authority_roles"],
                )
                self.assertEqual(
                    ["Conflicting or stale evidence remains unresolved."],
                    result["qualifications"],
                )

    def test_one_sided_conflict_blocks_multi_role_answer(self):
        graph = self.current_conflict_graph()
        conflict = next(
            edge for edge in graph["edges"]
            if edge["edge_type"] == "conflicts_with"
        )
        result = query_evidence_graph(graph, "policy control?")
        self.assertEqual("investigate", result["outcome"])
        self.assertEqual(
            sorted([
                conflict["source_id"], conflict["target_id"],
                "claim-00f8d59734704cd75c607a29",
            ]),
            result["evidence_ids"],
        )
        self.assertEqual(
            ["local_enforcement_guidance", "operator_procedure", "case_evidence"],
            result["authority_roles"],
        )
        self.assertEqual(
            [
                "Conflicting or stale evidence remains unresolved.",
                "Source scope requires applicability review.",
            ],
            result["qualifications"],
        )

    def test_one_sided_conflict_capacity_is_checked_before_truncation(self):
        with self.assertRaisesRegex(
            EvidenceQueryError, "limit cannot represent conflict evidence",
        ):
            query_evidence_graph(
                self.current_conflict_graph(), "policy?", limit=1,
            )

    def test_unrelated_conflict_word_does_not_project_an_edge(self):
        result = query_evidence_graph(
            self.current_conflict_graph(), "qzeta " + "con" + "flict",
        )
        self.assertEqual("refuse", result["outcome"])
        self.assertEqual([], result["evidence_ids"])
        self.assertEqual([], result["authority_roles"])
        self.assertEqual(
            ["The connected public graph does not support this question."],
            result["qualifications"],
        )

    def test_all_relevant_typed_conflicts_project_representable_endpoints(self):
        cases = (
            (("source", "source"), "policy?", {
                "claim-86ff97215ba0fcd2f28a6594",
                "source-policy-01", "source-procedure-01",
            }),
            (("source", "claim"), "policy?", {
                "claim-86ff97215ba0fcd2f28a6594",
                "source-policy-01", "claim-a5de26d64e7dbf39b2acd4c7",
            }),
            (("workflow", "claim"), "policy?", None),
        )
        for kinds, prompt, expected in cases:
            graph, edge = self.typed_conflict_graph(*kinds)
            expected_ids = expected or {
                edge["edge_id"], edge["target_id"],
            }
            with self.subTest(kinds=kinds):
                result = query_evidence_graph(graph, prompt)
                self.assertEqual("investigate", result["outcome"])
                self.assertEqual(sorted(expected_ids), result["evidence_ids"])
                self.assertIn(
                    "Conflicting or stale evidence remains unresolved.",
                    result["qualifications"],
                )
                with self.assertRaisesRegex(
                    EvidenceQueryError,
                    "limit cannot represent conflict evidence",
                ):
                    query_evidence_graph(
                        graph, prompt, limit=1,
                    )

    def test_workflow_only_conflict_is_irrelevant_without_matching_evidence(self):
        graph, _edge = self.typed_conflict_graph("workflow", "workflow")
        result = query_evidence_graph(
            graph, "qzeta " + "con" + "flict",
        )
        self.assertEqual("refuse", result["outcome"])
        self.assertEqual([], result["evidence_ids"])
        self.assertEqual([], result["authority_roles"])
        self.assertEqual(
            ["The connected public graph does not support this question."],
            result["qualifications"],
        )

    def test_limit_one_recomputes_roles_and_qualifications_for_projected_evidence(self):
        graph = self.neutral_graph()
        policy = query_evidence_graph(graph, "policy?", limit=1)
        self.assertEqual(["claim-86ff97215ba0fcd2f28a6594"], policy["evidence_ids"])
        self.assertEqual(["operator_procedure"], policy["authority_roles"])
        self.assertEqual([], policy["qualifications"])
        with self.assertRaisesRegex(
            EvidenceQueryError, "limit cannot represent conflict evidence",
        ):
            query_evidence_graph(
                self.neutral_graph(variant="conflict"),
                "policy procedure?",
                limit=1,
            )

    def test_reversed_neutral_inputs_produce_identical_readbacks(self):
        first = self.neutral_graph()
        reversed_graph = self.neutral_graph(reverse_inputs=True)
        for case in json.loads((Path(__file__).parent / "fixtures/ao_lore/neutral_evidence_graph/fixture-spec.json").read_text())["cases"]:
            self.assertEqual(query_evidence_graph(first, case["prompt"]), query_evidence_graph(reversed_graph, case["prompt"]))


if __name__ == "__main__": unittest.main()
