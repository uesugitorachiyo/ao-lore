import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ao_lore._strict_io import ContractError
from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_graph import (
    EvidenceGraphError,
    GraphDependencies,
    build_evidence_graph,
    claim_record,
    inspect_evidence_graph,
    operational_question_record,
    publish_evidence_graph,
    relationship_edge_record,
    recover_evidence_graph,
)
from ao_lore.evidence_graph_contracts import (
    AUTHORITY_FIELDS,
    canonical_evidence_digest,
    validate_graph_manifest,
)
from ao_lore.evidence_query import EvidenceQueryError, query_evidence_graph
from ao_lore.workspace_query import WorkspaceGraphSnapshot, WorkspaceQueryError
from tests.neutral_fixture_utils import neutral_https_locator


def false_authority():
    return {field: False for field in AUTHORITY_FIELDS}


def bind(value, field):
    result = deepcopy(value)
    result[field] = canonical_digest({key: item for key, item in result.items() if key != field})
    return result


class EvidenceGraphTests(unittest.TestCase):
    def fixture(self):
        workflow_id = "control-response"
        workflow_digest = canonical_evidence_digest(
            "policy procedure control records qualification", "root-workflow"
        )
        question = operational_question_record(
            prompt="How do policy and control guidance connect?",
            expected_outcome="answer",
            required_authority_roles=["primary_law", "technical_guidance"],
            required_evidence_ids=[],
            forbidden_evidence_ids=[],
            qualifications=["General public information only."],
        )
        policy_excerpt = "The policy record defines the required procedure."
        control_excerpt = "The control guide defines the supporting record."
        policy_claim = claim_record(
            source_id="policy-record", source_digest="sha256:" + "1" * 64,
            authority_role="primary_law", excerpt=policy_excerpt,
            citation_anchor="Policy record section", operational_question_ids=[question["question_id"]],
            subject_terms=["procedure"], semantic_reason_codes=["topic_match", "citation_supported"],
        )
        control_claim = claim_record(
            source_id="control-guide", source_digest="sha256:" + "2" * 64,
            authority_role="technical_guidance", excerpt=control_excerpt,
            citation_anchor="Control guide section", operational_question_ids=[question["question_id"]],
            subject_terms=["control"], semantic_reason_codes=["topic_match", "citation_supported"],
        )
        edges = [
            relationship_edge_record(
                edge_type="defines", source_kind="claim", source_id=policy_claim["claim_id"],
                source_evidence_digest=policy_claim["excerpt_digest"], target_kind="workflow",
                target_id=workflow_id, target_evidence_digest=workflow_digest,
                supporting_excerpt_digest=policy_claim["excerpt_digest"], reason_code="statutory_definition",
            ),
            relationship_edge_record(
                edge_type="provides_remediation_for", source_kind="claim", source_id=control_claim["claim_id"],
                source_evidence_digest=control_claim["excerpt_digest"], target_kind="workflow",
                target_id=workflow_id, target_evidence_digest=workflow_digest,
                supporting_excerpt_digest=control_claim["excerpt_digest"], reason_code="technical_remediation",
                qualification="Technical guidance requires source review.",
            ),
            relationship_edge_record(
                edge_type="cites", source_kind="source", source_id="policy-record",
                source_evidence_digest="sha256:" + "1" * 64, target_kind="claim",
                target_id=policy_claim["claim_id"], target_evidence_digest=policy_claim["excerpt_digest"],
                supporting_excerpt_digest=policy_claim["excerpt_digest"], reason_code="source_citation",
            ),
            relationship_edge_record(
                edge_type="cites", source_kind="source", source_id="control-guide",
                source_evidence_digest="sha256:" + "2" * 64, target_kind="claim",
                target_id=control_claim["claim_id"], target_evidence_digest=control_claim["excerpt_digest"],
                supporting_excerpt_digest=control_claim["excerpt_digest"], reason_code="source_citation",
            ),
        ]
        def source(source_id, digest, role, edge_ids):
            return {
                "source_id": source_id, "source_digest": digest,
                "canonical_locator": neutral_https_locator(
                    tuple(source_id.split("-")), ("in", "valid"), ("record",),
                ),
                "retrieved_at": "2026-08-12T20:00:00Z", "publisher": "Synthetic Register",
                "jurisdiction": "Synthetic scope",
                "authority_role": role, "version": "2026", "effective_date": "2026-01-01",
                "operational_question_ids": [question["question_id"]], "relationship_edge_ids": edge_ids,
                "status": "current", "media_type": "text/html", "retained_artifact_digests": [digest],
            }
        sources = [
            source("policy-record", "sha256:" + "1" * 64, "primary_law", [edges[0]["edge_id"], edges[2]["edge_id"]]),
            source("control-guide", "sha256:" + "2" * 64, "technical_guidance", [edges[1]["edge_id"], edges[3]["edge_id"]]),
        ]
        registry = bind({
            "schema_version": "ao.lore.evidence-source-registry.v0.1", "registry_id": "registry-01",
            "root_workflow_id": workflow_id, "sources": sources, **false_authority(),
        }, "registry_digest")
        question["required_evidence_ids"] = sorted([policy_claim["claim_id"], control_claim["claim_id"]])
        question = bind({k: v for k, v in question.items() if k != "question_digest"}, "question_digest")
        # Rebind source question IDs because the question identity is content-addressed.
        for item in registry["sources"]:
            item["operational_question_ids"] = [question["question_id"]]
        registry["registry_digest"] = canonical_digest({k: v for k, v in registry.items() if k != "registry_digest"})
        for claim in (policy_claim, control_claim):
            claim["operational_question_ids"] = [question["question_id"]]
        return registry, [policy_claim, control_claim], edges, [question], workflow_digest

    def build(self, **changes):
        registry, claims, edges, questions, workflow_digest = self.fixture()
        return build_evidence_graph(
            changes.get("registry", registry), changes.get("claims", claims),
            changes.get("edges", edges), changes.get("questions", questions),
            root_workflow_digest=changes.get("workflow_digest", workflow_digest),
            as_of_date=changes.get("as_of_date", "2026-08-12"),
        )

    def test_build_is_deterministic_under_input_reordering_and_inspects_clean(self):
        first = self.build()
        registry, claims, edges, questions, workflow_digest = self.fixture()
        registry["sources"].reverse()
        registry["registry_digest"] = canonical_digest({k: v for k, v in registry.items() if k != "registry_digest"})
        replay = build_evidence_graph(registry, list(reversed(claims)), list(reversed(edges)), questions,
                                      root_workflow_digest=workflow_digest, as_of_date="2026-08-12")
        self.assertEqual(first, replay)
        inspection = inspect_evidence_graph(first, as_of_date="2026-08-12")
        self.assertEqual("pass", inspection["result"])
        self.assertEqual((0, 0), (inspection["orphan_source_count"], inspection["orphan_claim_count"]))

    def test_explicit_absent_freshness_summary_preserves_inspection(self):
        graph = self.build()
        self.assertEqual(
            inspect_evidence_graph(graph, as_of_date="2026-08-12"),
            inspect_evidence_graph(
                graph, as_of_date="2026-08-12", freshness_summary=None,
            ),
        )

    def test_rejects_orphans_endpoint_and_excerpt_drift_and_duplicate_collision(self):
        registry, claims, edges, questions, workflow_digest = self.fixture()
        for mutation in ("orphan", "endpoint", "excerpt", "duplicate"):
            candidate_edges = deepcopy(edges)
            candidate_claims = deepcopy(claims)
            if mutation == "orphan": candidate_edges = candidate_edges[1:]
            if mutation == "endpoint": candidate_edges[0]["target_id"] = "unknown-workflow"
            if mutation == "excerpt": candidate_edges[0]["supporting_excerpt_digest"] = "sha256:" + "9" * 64
            if mutation == "duplicate": candidate_claims.append(deepcopy(candidate_claims[0]))
            with self.subTest(mutation=mutation), self.assertRaises(EvidenceGraphError):
                build_evidence_graph(registry, candidate_claims, candidate_edges, questions,
                                     root_workflow_digest=workflow_digest, as_of_date="2026-08-12")

    def test_rejects_incomplete_source_edge_and_unknown_question_evidence_bindings(self):
        registry, claims, edges, questions, workflow_digest = self.fixture()
        incomplete = deepcopy(registry)
        incomplete["sources"][0]["relationship_edge_ids"].remove(edges[0]["edge_id"])
        incomplete["registry_digest"] = canonical_digest(
            {key: value for key, value in incomplete.items() if key != "registry_digest"}
        )
        with self.assertRaisesRegex(EvidenceGraphError, "source edge binding"):
            build_evidence_graph(incomplete, claims, edges, questions,
                                 root_workflow_digest=workflow_digest, as_of_date="2026-08-12")

        unknown = deepcopy(questions)
        unknown[0]["required_evidence_ids"] = ["claim-does-not-exist"]
        unknown[0] = bind(
            {key: value for key, value in unknown[0].items() if key != "question_digest"},
            "question_digest",
        )
        with self.assertRaisesRegex(EvidenceGraphError, "question evidence binding"):
            build_evidence_graph(registry, claims, edges, unknown,
                                 root_workflow_digest=workflow_digest, as_of_date="2026-08-12")

    def test_rejects_authority_override_future_date_and_unsupported_supersession(self):
        registry, claims, edges, questions, workflow_digest = self.fixture()
        bad_edge = relationship_edge_record(
            edge_type="defines", source_kind="claim", source_id=claims[1]["claim_id"],
            source_evidence_digest=claims[1]["excerpt_digest"], target_kind="workflow",
            target_id="control-response", target_evidence_digest=workflow_digest,
            supporting_excerpt_digest=claims[1]["excerpt_digest"], reason_code="statutory_definition")
        with self.assertRaisesRegex(EvidenceGraphError, "authority"):
            build_evidence_graph(registry, claims, [bad_edge, *edges[2:]], questions,
                                 root_workflow_digest=workflow_digest, as_of_date="2026-08-12")
        future = deepcopy(registry); future["sources"][0]["effective_date"] = "2027-01-01"
        future["registry_digest"] = canonical_digest({k: v for k, v in future.items() if k != "registry_digest"})
        with self.assertRaisesRegex(EvidenceGraphError, "future"):
            build_evidence_graph(future, claims, edges, questions,
                                 root_workflow_digest=workflow_digest, as_of_date="2026-08-12")

        supersedes = relationship_edge_record(
            edge_type="supersedes", source_kind="source", source_id="control-guide",
            source_evidence_digest=registry["sources"][1]["source_digest"], target_kind="source",
            target_id="policy-record", target_evidence_digest=registry["sources"][0]["source_digest"],
            supporting_excerpt_digest=claims[1]["excerpt_digest"], reason_code="version_transition")
        with self.assertRaisesRegex(EvidenceGraphError, "supersession"):
            build_evidence_graph(registry, claims, [*edges, supersedes], questions,
                                 root_workflow_digest=workflow_digest, as_of_date="2026-08-12")

    def test_question_authority_gap_is_permitted_only_for_negative_outcomes(self):
        registry, claims, edges, questions, workflow_digest = self.fixture()
        for expected_outcome in ("investigate", "refuse"):
            negative = deepcopy(questions)
            negative[0]["expected_outcome"] = expected_outcome
            negative[0]["required_authority_roles"] = ["case_evidence"]
            negative[0] = bind(
                {key: value for key, value in negative[0].items() if key != "question_digest"},
                "question_digest",
            )
            graph = build_evidence_graph(
                registry, claims, edges, negative,
                root_workflow_digest=workflow_digest, as_of_date="2026-08-12",
            )
            with self.subTest(expected_outcome=expected_outcome):
                self.assertEqual(graph, validate_graph_manifest(graph))
                self.assertEqual(
                    graph["graph_digest"],
                    WorkspaceGraphSnapshot("workspace-1", graph)._open()[0]["graph_digest"],
                )

        answerable = deepcopy(graph)
        answerable["operational_questions"][0]["expected_outcome"] = "answer"
        answerable["operational_questions"][0] = bind(
            {
                key: value
                for key, value in answerable["operational_questions"][0].items()
                if key != "question_digest"
            },
            "question_digest",
        )
        answerable = bind(answerable, "graph_digest")
        with self.assertRaisesRegex(ContractError, "graph manifest relationships are invalid"):
            validate_graph_manifest(answerable)

    def test_stale_and_conflict_remain_visible_as_investigate(self):
        registry, claims, edges, questions, workflow_digest = self.fixture()
        registry["sources"][1]["status"] = "historical"
        conflict = relationship_edge_record(
            edge_type="conflicts_with", source_kind="claim", source_id=claims[0]["claim_id"],
            source_evidence_digest=claims[0]["excerpt_digest"], target_kind="claim",
            target_id=claims[1]["claim_id"], target_evidence_digest=claims[1]["excerpt_digest"],
            supporting_excerpt_digest=claims[0]["excerpt_digest"], reason_code="documented_conflict")
        registry["sources"][0]["relationship_edge_ids"].append(conflict["edge_id"])
        registry["sources"][1]["relationship_edge_ids"].append(conflict["edge_id"])
        registry["registry_digest"] = canonical_digest({k: v for k, v in registry.items() if k != "registry_digest"})
        graph = build_evidence_graph(registry, claims, [*edges, conflict], questions,
                                     root_workflow_digest=workflow_digest, as_of_date="2026-08-12")
        inspection = inspect_evidence_graph(graph, as_of_date="2026-08-12")
        self.assertEqual("investigate", inspection["result"])
        self.assertEqual((1, 1), (inspection["stale_source_count"], inspection["conflict_count"]))

    def test_every_detached_graph_consumer_rejects_self_rehashed_relationship_drift(self):
        graph = self.build()
        graph["claims"][0]["source_digest"] = "sha256:" + "9" * 64
        graph = bind(graph, "graph_digest")
        with self.assertRaisesRegex(EvidenceGraphError, "inspection input is invalid"):
            inspect_evidence_graph(graph, as_of_date="2026-08-12")
        with self.assertRaisesRegex(EvidenceQueryError, "graph is invalid"):
            query_evidence_graph(graph, "policy control")
        with self.assertRaisesRegex(WorkspaceQueryError, "snapshot is invalid"):
            WorkspaceGraphSnapshot("workspace-1", graph)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(EvidenceGraphError, "graph is invalid"):
                publish_evidence_graph(graph, GraphDependencies(root))
            (root / "generations").mkdir()
            staging = root / "staging"
            staging.mkdir()
            token = graph["graph_digest"].split(":", 1)[1]
            (staging / f"{token}.part").write_text(
                json.dumps(graph, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            self.assertEqual(
                [{"classification": "investigate"}],
                recover_evidence_graph(GraphDependencies(root)),
            )

    def test_immutable_publication_replays_and_rejects_destination_conflict(self):
        graph = self.build()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            deps = GraphDependencies(root)
            first = publish_evidence_graph(graph, deps)
            second = publish_evidence_graph(graph, deps)
            self.assertEqual("completed", first["status"])
            self.assertEqual("no_op", second["classification"])
            destination = root / "generations" / (graph["graph_digest"].split(":", 1)[1] + ".json")
            destination.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(EvidenceGraphError, "conflict"):
                publish_evidence_graph(graph, deps)

    def test_publication_recovery_and_symlink_root_rejection(self):
        graph = self.build()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "graphs"; root.mkdir()
            def stop(stage):
                if stage == "after_staging": raise RuntimeError("crash")
            with self.assertRaises(RuntimeError):
                publish_evidence_graph(graph, GraphDependencies(root, failpoint=stop))
            recovered = recover_evidence_graph(GraphDependencies(root))
            self.assertEqual("complete", recovered[0]["classification"])
            self.assertEqual("no_op", publish_evidence_graph(graph, GraphDependencies(root))["classification"])
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory); real = parent / "real"; real.mkdir(); link = parent / "link"; link.symlink_to(real)
            with self.assertRaises(EvidenceGraphError):
                publish_evidence_graph(graph, GraphDependencies(link))


if __name__ == "__main__":
    unittest.main()
