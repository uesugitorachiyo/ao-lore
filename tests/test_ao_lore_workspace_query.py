import builtins
import socket
import unittest
from copy import deepcopy
from unittest.mock import patch

from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_graph import claim_record, relationship_edge_record
from ao_lore.evidence_graph_contracts import canonical_evidence_digest
from ao_lore.evidence_query import query_evidence_graph
from ao_lore.workspace_query import (
    WorkspaceGraphSnapshot,
    WorkspaceQueryError,
    WorkspaceQuerySnapshot,
    query_workspace,
)
from ao_lore.workspace_contracts import validate_workspace_query_readback
from ao_lore.workspace_registry import (
    WorkspaceRegistryError,
    WorkspaceRegistrySnapshot,
    WorkspaceSelection,
    select_workspace,
)
from tests import test_ao_lore_evidence_graph as evidence_graph_tests
from tests import test_ao_lore_evidence_freshness_query as freshness_query_tests
from tests import test_ao_lore_evidence_query as evidence_query_tests
from tests.test_ao_lore_workspace_registry import generation, workspace


def rebind(value, field):
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def graph_variant(graph_id, excerpt_suffix="", *, conflict=False, restricted=False):
    graph = deepcopy(evidence_graph_tests.EvidenceGraphTests().build())
    graph["graph_id"] = graph_id
    old_excerpt_digests = {
        claim["claim_id"]: claim["excerpt_digest"] for claim in graph["claims"]
    }
    if excerpt_suffix:
        for claim in graph["claims"]:
            claim["excerpt"] += excerpt_suffix
            claim["excerpt_digest"] = canonical_evidence_digest(claim["excerpt"], "excerpt")
    if restricted:
        restricted_source_id = graph["claims"][0]["source_id"]
        next(
            source for source in graph["sources"]
            if source["source_id"] == restricted_source_id
        )["authority_role"] = "case_evidence"
        for claim in graph["claims"]:
            if claim["source_id"] == restricted_source_id:
                claim["authority_role"] = "case_evidence"
        graph["operational_questions"][0]["required_authority_roles"] = sorted({
            source["authority_role"] for source in graph["sources"]
        })
        rebind(graph["operational_questions"][0], "question_digest")
    claim_by_id = {claim["claim_id"]: claim for claim in graph["claims"]}
    for edge in graph["edges"]:
        if edge["source_kind"] == "claim":
            edge["source_evidence_digest"] = claim_by_id[edge["source_id"]]["excerpt_digest"]
            if restricted and claim_by_id[edge["source_id"]]["authority_role"] != "primary_law" and edge["edge_type"] == "defines":
                edge["edge_type"] = "explains"
                edge["reason_code"] = "official_explanation"
        if edge["target_kind"] == "claim":
            edge["target_evidence_digest"] = claim_by_id[edge["target_id"]]["excerpt_digest"]
        for claim_id, old_digest in old_excerpt_digests.items():
            if edge["supporting_excerpt_digest"] == old_digest:
                edge["supporting_excerpt_digest"] = claim_by_id[claim_id]["excerpt_digest"]
        rebind(edge, "edge_digest")
    if conflict:
        first, second = graph["claims"][:2]
        edge = relationship_edge_record(
            edge_type="conflicts_with", source_kind="claim", source_id=first["claim_id"],
            source_evidence_digest=first["excerpt_digest"], target_kind="claim",
            target_id=second["claim_id"], target_evidence_digest=second["excerpt_digest"],
            supporting_excerpt_digest=first["excerpt_digest"], reason_code="documented_conflict",
        )
        graph["edges"].append(edge)
        for source in graph["sources"]:
            source["relationship_edge_ids"].append(edge["edge_id"])
    return rebind(graph, "graph_digest")


def query_snapshot(*, references=("shared-reference-fixture",), reference_status="active",
                   primary_conflict=False, restricted=False, reorder=False,
                   freshness=None):
    graphs = {
        "primary-fixture-a": graph_variant("graph-primary-fixture-a", conflict=primary_conflict,
                                             restricted=restricted),
        "secondary-fixture-b": graph_variant("graph-secondary-fixture-b", " Hostile unrelated text."),
        "shared-reference-fixture": graph_variant("graph-shared-reference-fixture", " Shared reference text."),
    }
    definitions = [
        workspace("primary-fixture-a", "property", references),
        workspace("secondary-fixture-b", "property"),
        workspace("shared-reference-fixture", status=reference_status),
    ]
    summaries = {}
    for workspace_id, classifications in (freshness or {}).items():
        helper = freshness_query_tests.EvidenceFreshnessQueryTests()
        helper.graph = graphs[workspace_id]
        summaries[workspace_id] = helper.context(classifications)[3]
    for definition in definitions:
        graph = graphs[definition["workspace_id"]]
        definition["graph_id"] = graph["graph_id"]
        definition["graph_digest"] = graph["graph_digest"]
        summary = summaries.get(definition["workspace_id"])
        if summary is not None:
            definition["freshness_policy_id"] = summary["policy_id"]
            definition["freshness_policy_digest"] = summary["policy_digest"]
            definition["freshness_summary_status"] = "observed"
            definition["freshness_summary_id"] = summary["summary_id"]
            definition["freshness_summary_digest"] = summary["summary_digest"]
        rebind(definition, "definition_digest")
    definitions.sort(key=lambda item: item["workspace_id"])
    current = generation(definitions)
    registry = WorkspaceRegistrySnapshot((current,), tuple(current["workspaces"]))
    selection = select_workspace(registry, "primary-fixture-a")
    selected_ids = {
        selection.primary["workspace_id"],
        *(item["workspace_id"] for item in selection.references),
    }
    items = [WorkspaceGraphSnapshot(workspace_id, graph, summaries.get(workspace_id))
             for workspace_id, graph in graphs.items() if workspace_id in selected_ids]
    if reorder:
        items.reverse()
    return WorkspaceQuerySnapshot(registry, selection, items)


class WorkspaceQueryTests(unittest.TestCase):
    prompt = "How do policy control and control guidance connect?"

    def test_primary_query_uses_only_declared_reference_and_exact_origins(self):
        result = query_workspace(query_snapshot(), "primary-fixture-a", self.prompt)
        self.assertEqual("primary-fixture-a", result["primary_workspace_id"])
        self.assertEqual(
            ["primary-fixture-a", "shared-reference-fixture"],
            result["consulted_workspace_ids"],
        )
        self.assertNotIn("secondary-fixture-b", repr(result))
        self.assertTrue(result["evidence"])
        self.assertTrue(all(set(item) == {
            "workspace_id", "graph_id", "source_id", "evidence_kind",
            "evidence_id", "evidence_digest",
        } for item in result["evidence"]))
        origins = [tuple(item.values()) for item in result["evidence"]]
        self.assertEqual(sorted(origins), origins)

    def test_no_references_queries_only_primary_and_empty_hits_refuse(self):
        snapshot = query_snapshot(references=())
        answered = query_workspace(snapshot, "primary-fixture-a", self.prompt)
        self.assertEqual(["primary-fixture-a"], answered["consulted_workspace_ids"])
        empty = query_workspace(snapshot, "primary-fixture-a", "unrelated synthetic question")
        self.assertEqual("refuse", empty["outcome"])
        self.assertEqual([], empty["evidence"])

    def test_unknown_primary_and_inactive_reference_fail_closed(self):
        with self.assertRaisesRegex(WorkspaceQueryError, "workspace selection differs"):
            query_workspace(query_snapshot(), "unknown-workspace", self.prompt)
        with self.assertRaisesRegex(WorkspaceRegistryError, "reference is unavailable"):
            query_snapshot(reference_status="inactive")

    def test_legacy_graph_only_query_rejects_unicode_category_c_prompt(self):
        with self.assertRaises(WorkspaceQueryError):
            query_workspace(
                query_snapshot(references=()),
                "primary-fixture-a",
                "procedure\u202erecord",
            )

    def test_reordered_graph_snapshots_are_byte_deterministic(self):
        first = query_workspace(query_snapshot(), "primary-fixture-a", self.prompt)
        second = query_workspace(query_snapshot(reorder=True), "primary-fixture-a", self.prompt)
        self.assertEqual(first, second)

    def test_hostile_same_local_ids_remain_origin_qualified(self):
        result = query_workspace(query_snapshot(), "primary-fixture-a", self.prompt)
        local_ids = [item["evidence_id"] for item in result["evidence"]]
        self.assertLess(len(set(local_ids)), len(local_ids))
        self.assertEqual(len(result["evidence"]), len({tuple(item.values()) for item in result["evidence"]}))

    def test_conflict_and_restricted_evidence_cannot_answer(self):
        conflict = query_workspace(
            query_snapshot(primary_conflict=True), "primary-fixture-a",
            self.prompt,
        )
        self.assertEqual("investigate", conflict["outcome"])
        self.assertIn("Conflicting or stale evidence remains unresolved.", conflict["qualifications"])
        restricted = query_workspace(
            query_snapshot(restricted=True, references=()), "primary-fixture-a",
            "What does policy control define?",
        )
        self.assertEqual("refuse", restricted["outcome"])
        self.assertEqual([], restricted["evidence"])
        self.assertIn("Restricted evidence cannot support a workspace answer.", restricted["qualifications"])

    def test_graph_only_workspace_projects_one_sided_conflict_endpoints(self):
        snapshot = query_snapshot(primary_conflict=True, references=())
        graph = snapshot._open()[1]["primary-fixture-a"][0]
        conflict = next(
            edge for edge in graph["edges"]
            if edge["edge_type"] == "conflicts_with"
        )
        result = query_workspace(snapshot, "primary-fixture-a", "procedure?")
        self.assertEqual("investigate", result["outcome"])
        self.assertEqual(
            {conflict["source_id"], conflict["target_id"]},
            {item["evidence_id"] for item in result["evidence"]},
        )
        self.assertEqual(
            [
                "Conflicting or stale evidence remains unresolved.",
                "Primary source predicates require review.",
                "Technical guidance requires source review.",
            ],
            result["qualifications"],
        )

    def test_graph_only_workspace_projects_all_relevant_typed_conflicts(self):
        helper = evidence_query_tests.EvidenceQueryTests()
        for kinds in (
            ("source", "source"),
            ("source", "claim"),
            ("workflow", "claim"),
        ):
            graph, edge = helper.typed_conflict_graph(*kinds)
            definition = workspace("primary-fixture-a", "property", ())
            definition["graph_id"] = graph["graph_id"]
            definition["graph_digest"] = graph["graph_digest"]
            rebind(definition, "definition_digest")
            current = generation([definition])
            registry = WorkspaceRegistrySnapshot(
                (current,), tuple(current["workspaces"]),
            )
            snapshot = WorkspaceQuerySnapshot(
                registry,
                select_workspace(registry, "primary-fixture-a"),
                (WorkspaceGraphSnapshot("primary-fixture-a", graph),),
            )
            result = query_workspace(snapshot, "primary-fixture-a", "policy?")
            claims = {item["source_id"]: item for item in graph["claims"]}
            if "workflow" in kinds:
                expected = {
                    ("claim", edge["target_id"]),
                    ("edge", edge["edge_id"]),
                }
            else:
                expected = {
                    ("claim", claims["source-policy-01"]["claim_id"]),
                    ("claim", claims["source-procedure-01"]["claim_id"]),
                }
            with self.subTest(kinds=kinds):
                self.assertEqual("investigate", result["outcome"])
                self.assertEqual(
                    expected,
                    {
                        (item["evidence_kind"], item["evidence_id"])
                        for item in result["evidence"]
                    },
                )
                self.assertIn(
                    "Conflicting or stale evidence remains unresolved.",
                    result["qualifications"],
                )

    def test_explicit_conflict_split_across_primary_and_reference_investigates(self):
        primary = graph_variant("graph-primary-fixture-a")
        reference = graph_variant("graph-shared-reference-fixture")
        question_id = primary["operational_questions"][0]["question_id"]
        for graph, source_id in ((primary, "policy-record"), (reference, "control-guide")):
            next(item for item in graph["sources"] if item["source_id"] == source_id)[
                "authority_role"
            ] = "official_interpretation"
            for existing in graph["claims"]:
                if existing["source_id"] == source_id:
                    existing["authority_role"] = "official_interpretation"
            changed_claim_ids = {
                item["claim_id"] for item in graph["claims"]
                if item["source_id"] == source_id
            }
            for existing_edge in graph["edges"]:
                if (existing_edge["source_kind"] == "claim"
                        and existing_edge["source_id"] in changed_claim_ids
                        and existing_edge["edge_type"] == "defines"):
                    existing_edge["edge_type"] = "explains"
                    existing_edge["reason_code"] = "official_explanation"
                    rebind(existing_edge, "edge_digest")
            graph["operational_questions"][0]["required_authority_roles"] = sorted({
                item["authority_role"] for item in graph["sources"]
            })
            rebind(graph["operational_questions"][0], "question_digest")

        def claim(source_id, role, excerpt, anchor, terms):
            source = next(item for item in primary["sources"] if item["source_id"] == source_id)
            return claim_record(
                source_id=source_id, source_digest=source["source_digest"],
                authority_role=role, excerpt=excerpt, citation_anchor=anchor,
                operational_question_ids=[question_id], subject_terms=terms,
                semantic_reason_codes=["topic_match", "citation_supported"],
            )

        positive = "The synthetic valve state is enabled."
        negative = "The synthetic valve state is disabled."
        positive_proxy = claim("policy-record", "official_interpretation", positive, "Positive marker", ["marker"])
        negative_proxy = claim("control-guide", "technical_guidance", negative, "Negative marker", ["marker"])
        selected_positive = claim("policy-record", "official_interpretation", positive, "Selected positive", ["crosscheck"])
        reference_positive_proxy = claim("policy-record", "primary_law", positive, "Positive marker", ["marker"])
        selected_negative = claim("control-guide", "official_interpretation", negative, "Selected negative", ["crosscheck"])
        primary_conflict = relationship_edge_record(
            edge_type="conflicts_with", source_kind="claim",
            source_id=selected_positive["claim_id"],
            source_evidence_digest=selected_positive["excerpt_digest"],
            target_kind="claim", target_id=negative_proxy["claim_id"],
            target_evidence_digest=negative_proxy["excerpt_digest"],
            supporting_excerpt_digest=selected_positive["excerpt_digest"],
            reason_code="documented_conflict",
        )
        reference_conflict = relationship_edge_record(
            edge_type="conflicts_with", source_kind="claim",
            source_id=selected_negative["claim_id"],
            source_evidence_digest=selected_negative["excerpt_digest"],
            target_kind="claim", target_id=reference_positive_proxy["claim_id"],
            target_evidence_digest=reference_positive_proxy["excerpt_digest"],
            supporting_excerpt_digest=selected_negative["excerpt_digest"],
            reason_code="documented_conflict",
        )
        primary["claims"].extend([positive_proxy, negative_proxy, selected_positive])
        primary["edges"].append(primary_conflict)
        for added_claim in (positive_proxy, negative_proxy, selected_positive):
            attachment = relationship_edge_record(
                edge_type="explains", source_kind="claim",
                source_id=added_claim["claim_id"],
                source_evidence_digest=added_claim["excerpt_digest"],
                target_kind="workflow", target_id=primary["root_workflow_id"],
                target_evidence_digest=primary["root_workflow_digest"],
                supporting_excerpt_digest=added_claim["excerpt_digest"],
                reason_code="official_explanation",
            )
            primary["edges"].append(attachment)
            next(source for source in primary["sources"] if source["source_id"] == added_claim["source_id"])["relationship_edge_ids"].append(attachment["edge_id"])
        for source_id in {positive_proxy["source_id"], negative_proxy["source_id"]}:
            next(source for source in primary["sources"] if source["source_id"] == source_id)["relationship_edge_ids"].append(primary_conflict["edge_id"])
        primary = rebind(primary, "graph_digest")
        reference["claims"].extend([reference_positive_proxy, selected_negative])
        reference["edges"].append(reference_conflict)
        for added_claim in (reference_positive_proxy, selected_negative):
            attachment = relationship_edge_record(
                edge_type="explains", source_kind="claim",
                source_id=added_claim["claim_id"],
                source_evidence_digest=added_claim["excerpt_digest"],
                target_kind="workflow", target_id=reference["root_workflow_id"],
                target_evidence_digest=reference["root_workflow_digest"],
                supporting_excerpt_digest=added_claim["excerpt_digest"],
                reason_code="official_explanation",
            )
            reference["edges"].append(attachment)
            next(source for source in reference["sources"] if source["source_id"] == added_claim["source_id"])["relationship_edge_ids"].append(attachment["edge_id"])
        for source_id in {selected_negative["source_id"], reference_positive_proxy["source_id"]}:
            next(source for source in reference["sources"] if source["source_id"] == source_id)["relationship_edge_ids"].append(reference_conflict["edge_id"])
        reference = rebind(reference, "graph_digest")

        primary_alone = query_evidence_graph(primary, "crosscheck")
        reference_alone = query_evidence_graph(reference, "crosscheck")
        self.assertEqual("investigate", primary_alone["outcome"])
        self.assertEqual("investigate", reference_alone["outcome"])
        self.assertIn(selected_positive["claim_id"], primary_alone["evidence_ids"])
        self.assertIn(selected_negative["claim_id"], reference_alone["evidence_ids"])

        definitions = [
            workspace("primary-fixture-a", "property", ("shared-reference-fixture",)),
            workspace("shared-reference-fixture"),
        ]
        graphs = {
            "primary-fixture-a": primary,
            "shared-reference-fixture": reference,
        }
        for definition in definitions:
            graph = graphs[definition["workspace_id"]]
            definition["graph_id"] = graph["graph_id"]
            definition["graph_digest"] = graph["graph_digest"]
            rebind(definition, "definition_digest")
        definitions.sort(key=lambda item: item["workspace_id"])
        current = generation(definitions)
        primary_endpoint = (
            "primary-fixture-a", primary["graph_id"],
            selected_positive["claim_id"], selected_positive["excerpt_digest"],
        )
        reference_endpoint = (
            "shared-reference-fixture", reference["graph_id"],
            selected_negative["claim_id"], selected_negative["excerpt_digest"],
        )
        binding = tuple(sorted((primary_endpoint, reference_endpoint)))
        registry = WorkspaceRegistrySnapshot((current,), tuple(current["workspaces"]))
        selection = select_workspace(registry, "primary-fixture-a")
        snapshot = WorkspaceQuerySnapshot(
            registry, selection,
            [WorkspaceGraphSnapshot(item, graph) for item, graph in graphs.items()],
            cross_workspace_conflicts=(binding,),
        )

        result = query_workspace(snapshot, "primary-fixture-a", "crosscheck")

        self.assertEqual("investigate", result["outcome"])
        self.assertEqual("workspace_binding_drift", result["reason_code"])
        self.assertIn("Conflicting or stale evidence remains unresolved.", result["qualifications"])
        selected_origins = {
            (item["workspace_id"], item["evidence_id"]) for item in result["evidence"]
        }
        self.assertIn(("primary-fixture-a", selected_positive["claim_id"]), selected_origins)
        self.assertIn(("shared-reference-fixture", selected_negative["claim_id"]), selected_origins)

        no_binding = WorkspaceQuerySnapshot(
            registry, selection,
            [WorkspaceGraphSnapshot(item, graph) for item, graph in graphs.items()],
        )
        self.assertEqual(
            "investigate",
            query_workspace(no_binding, "primary-fixture-a", "crosscheck")["outcome"],
        )

        proxy_endpoint = (
            "primary-fixture-a", primary["graph_id"],
            positive_proxy["claim_id"], positive_proxy["excerpt_digest"],
        )
        proxy_binding = tuple(sorted((proxy_endpoint, reference_endpoint)))
        with self.assertRaisesRegex(WorkspaceQueryError, "conflict endpoint differs"):
            WorkspaceQuerySnapshot(
                registry, selection,
                [WorkspaceGraphSnapshot(item, graph) for item, graph in graphs.items()],
                cross_workspace_conflicts=(proxy_binding,),
            )

        forged_binding = (binding[0], (
            binding[1][0], binding[1][1], binding[1][2], "sha256:" + "9" * 64,
        ))
        with self.assertRaisesRegex(WorkspaceQueryError, "conflict endpoint differs"):
            WorkspaceQuerySnapshot(
                registry, selection,
                [WorkspaceGraphSnapshot(item, graph) for item, graph in graphs.items()],
                cross_workspace_conflicts=(forged_binding,),
            )

    def test_arbitrary_prompt_words_do_not_override_graph_evidence(self):
        snapshot = query_snapshot()
        baseline = query_workspace(snapshot, "primary-fixture-a", self.prompt)
        with_arbitrary_word = query_workspace(
            snapshot, "primary-fixture-a", self.prompt + " " + "li" + "able",
        )
        for field in ("outcome", "evidence", "qualifications"):
            self.assertEqual(baseline[field], with_arbitrary_word[field])
        for field in (
            "legal_advice", "property_decision", "candidate_review",
            "candidate_decision", "promotion", "canonical_query", "provider",
            "credential", "private_data", "publication", "release",
            "deployment", "authority_advanced",
        ):
            self.assertIs(False, with_arbitrary_word[field])

        with_qualification_word = query_workspace(
            snapshot,
            "primary-fixture-a",
            self.prompt + " " + "with" + "out",
        )
        for field in ("outcome", "evidence", "qualifications"):
            self.assertEqual(baseline[field], with_qualification_word[field])

    def test_affected_reference_freshness_is_mandatory_and_investigates(self):
        result = query_workspace(
            query_snapshot(freshness={"shared-reference-fixture": {"control-guide": "updated"}}),
            "primary-fixture-a", "What does official control guidance recommend?",
        )
        self.assertEqual("investigate", result["outcome"])
        self.assertEqual("freshness_investigation_required", result["reason_code"])
        self.assertIn("Source freshness requires review.", result["qualifications"])
        self.assertTrue(any(
            item["workspace_id"] == "shared-reference-fixture"
            and item["source_id"] == "control-guide"
            for item in result["evidence"]
        ))

    def test_snapshot_requires_exact_definition_freshness_binding(self):
        snapshot = query_snapshot(
            freshness={"shared-reference-fixture": {"control-guide": "updated"}},
        )
        query_workspace(snapshot, "primary-fixture-a", "official control guidance")
        generation_value, graph_map = snapshot._open()
        observed_id = "shared-reference-fixture"

        def registry_with(**changes):
            changed = deepcopy(generation_value)
            definition = next(
                item for item in changed["workspaces"]
                if item["workspace_id"] == observed_id
            )
            definition.update(changes)
            rebind(definition, "definition_digest")
            rebind(changed, "registry_digest")
            return WorkspaceRegistrySnapshot((changed,), tuple(changed["workspaces"]))

        with self.assertRaisesRegex(WorkspaceQueryError, "freshness summary is not declared"):
            changed_registry = registry_with(
                freshness_policy_id=None, freshness_policy_digest=None,
                freshness_summary_status="not_observed",
                freshness_summary_id=None, freshness_summary_digest=None,
            )
            WorkspaceQuerySnapshot(
                changed_registry,
                select_workspace(changed_registry, "primary-fixture-a"),
                snapshot.graphs,
            )

        graph, _summary = graph_map[observed_id]
        missing = tuple(
            WorkspaceGraphSnapshot(item.workspace_id, graph)
            if item.workspace_id == observed_id else item
            for item in snapshot.graphs
        )
        with self.assertRaisesRegex(WorkspaceQueryError, "freshness summary is required"):
            WorkspaceQuerySnapshot(
                snapshot.registry,
                select_workspace(snapshot.registry, "primary-fixture-a"),
                missing,
            )

        definition = next(
            item for item in generation_value["workspaces"]
            if item["workspace_id"] == observed_id
        )
        mismatches = (
            {"freshness_summary_id": "wrong-summary"},
            {"freshness_summary_digest": "sha256:" + "8" * 64},
            {"freshness_policy_id": "wrong-policy"},
            {"freshness_policy_digest": "sha256:" + "7" * 64},
            {"graph_id": "wrong-graph"},
            {"graph_digest": "sha256:" + "6" * 64},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=next(iter(mismatch))):
                with self.assertRaisesRegex(WorkspaceQueryError, "binding differs"):
                    changed_registry = registry_with(**mismatch)
                    WorkspaceQuerySnapshot(
                        changed_registry,
                        select_workspace(changed_registry, "primary-fixture-a"),
                        snapshot.graphs,
                    )

        self.assertEqual("observed", definition["freshness_summary_status"])

    def test_snapshot_detaches_observed_summary_from_caller_alias_mutation(self):
        graph = graph_variant("graph-observed-alias")
        helper = freshness_query_tests.EvidenceFreshnessQueryTests()
        helper.graph = graph
        summary = helper.context({"control-guide": "updated"})[3]
        with self.assertRaisesRegex(WorkspaceQueryError, "snapshot is invalid"):
            WorkspaceGraphSnapshot("observed-alias", graph, dict(summary))
        item = WorkspaceGraphSnapshot("observed-alias", graph, summary)
        summary["summary_id"] = "mutated-after-snapshot"
        opened_graph, opened_summary = item._open()
        self.assertEqual(graph["graph_digest"], opened_graph["graph_digest"])
        self.assertNotEqual("mutated-after-snapshot", opened_summary["summary_id"])

    def test_mandatory_cross_workspace_saturation_stays_qualified_investigate(self):
        graphs = {}
        for workspace_id in ("primary-fixture-a", "reference-one", "reference-two"):
            graph = graph_variant("graph-" + workspace_id)
            for source in graph["sources"]:
                source["status"] = "historical"
            template = graph["claims"][1]
            for index in range(63):
                claim = deepcopy(template)
                claim["claim_id"] = f"claim-saturation-{index:03d}"
                claim["excerpt"] = f"Official control guidance saturation item {index}."
                claim["excerpt_digest"] = canonical_evidence_digest(claim["excerpt"], "excerpt")
                graph["claims"].append(claim)
                attachment = relationship_edge_record(
                    edge_type="explains", source_kind="claim",
                    source_id=claim["claim_id"],
                    source_evidence_digest=claim["excerpt_digest"],
                    target_kind="workflow", target_id=graph["root_workflow_id"],
                    target_evidence_digest=graph["root_workflow_digest"],
                    supporting_excerpt_digest=claim["excerpt_digest"],
                    reason_code="official_explanation",
                )
                graph["edges"].append(attachment)
                next(
                    source for source in graph["sources"]
                    if source["source_id"] == claim["source_id"]
                )["relationship_edge_ids"].append(attachment["edge_id"])
            graphs[workspace_id] = rebind(graph, "graph_digest")
        definitions = [
            workspace("primary-fixture-a", "property", ("reference-one", "reference-two")),
            workspace("reference-one"), workspace("reference-two"),
        ]
        for definition in definitions:
            graph = graphs[definition["workspace_id"]]
            definition["graph_id"] = graph["graph_id"]
            definition["graph_digest"] = graph["graph_digest"]
            rebind(definition, "definition_digest")
        definitions.sort(key=lambda item: item["workspace_id"])
        current = generation(definitions)
        registry = WorkspaceRegistrySnapshot((current,), tuple(current["workspaces"]))
        snapshot = WorkspaceQuerySnapshot(
            registry, select_workspace(registry, "primary-fixture-a"),
            [WorkspaceGraphSnapshot(item, graph) for item, graph in graphs.items()],
        )
        result = query_workspace(
            snapshot, "primary-fixture-a", "What does official control guidance recommend?",
        )
        self.assertEqual("investigate", result["outcome"])
        self.assertEqual([], result["evidence"])
        self.assertIn(
            "Evidence budget cannot represent every required gate identity.",
            result["qualifications"],
        )

    def test_query_is_pure_after_validated_snapshot_is_received(self):
        snapshot = query_snapshot()
        with patch.object(builtins, "open", side_effect=AssertionError("filesystem opened")), \
                patch.object(socket, "socket", side_effect=AssertionError("network opened")):
            result = query_workspace(snapshot, "primary-fixture-a", self.prompt)
        self.assertIn(result["outcome"], {"answer", "partial"})

    def test_snapshot_rejects_missing_or_duplicate_graph_bindings(self):
        snapshot = query_snapshot()
        selection = select_workspace(snapshot.registry, "primary-fixture-a")
        with self.assertRaisesRegex(WorkspaceQueryError, "graph coverage differs"):
            WorkspaceQuerySnapshot(snapshot.registry, selection, snapshot.graphs[:-1])
        with self.assertRaisesRegex(WorkspaceQueryError, "graph identities differ"):
            WorkspaceQuerySnapshot(
                snapshot.registry, selection, (*snapshot.graphs, snapshot.graphs[0]),
            )

    def test_snapshot_accepts_exact_selected_subset_and_rejects_coverage_or_selection_forgery(self):
        complete = query_snapshot()
        registry = complete.registry
        selection = select_workspace(registry, "primary-fixture-a")
        selected_ids = {
            selection.primary["workspace_id"],
            *(item["workspace_id"] for item in selection.references),
        }
        selected_graphs = tuple(
            item for item in complete.graphs if item.workspace_id in selected_ids
        )

        exact = WorkspaceQuerySnapshot(registry, selection, selected_graphs)
        self.assertEqual(selected_ids, {item.workspace_id for item in exact.graphs})
        with self.assertRaisesRegex(WorkspaceQueryError, "graph coverage differs"):
            WorkspaceQuerySnapshot(registry, selection, selected_graphs[:-1])
        unrelated = WorkspaceGraphSnapshot(
            "secondary-fixture-b",
            graph_variant("graph-secondary-fixture-b", " Hostile unrelated text."),
        )
        with self.assertRaisesRegex(WorkspaceQueryError, "graph coverage differs"):
            WorkspaceQuerySnapshot(registry, selection, (*selected_graphs, unrelated))

        forged_primary = deepcopy(selection.primary)
        forged_primary["graph_digest"] = "sha256:" + "9" * 64
        forged = WorkspaceSelection(
            selection.generation, forged_primary, selection.references,
        )
        with self.assertRaisesRegex(WorkspaceQueryError, "workspace selection differs"):
            WorkspaceQuerySnapshot(registry, forged, selected_graphs)

    def test_context_validation_rejects_tampered_source_and_evidence_bindings(self):
        snapshot = query_snapshot()
        result = query_workspace(snapshot, "primary-fixture-a", self.prompt)
        current, graph_map = snapshot._open()
        graphs = [item[0] for item in graph_map.values()
                  if item[0]["graph_id"] in {evidence["graph_id"] for evidence in result["evidence"]}]
        for field, replacement in (
            ("source_id", "foreign-source"),
            ("evidence_digest", "sha256:" + "9" * 64),
        ):
            changed = deepcopy(result)
            changed["evidence"][0][field] = replacement
            changed["evidence"].sort(key=lambda item: tuple(item.values()))
            rebind(changed, "readback_digest")
            with self.assertRaisesRegex(Exception, "evidence .* binding differs"):
                validate_workspace_query_readback(
                    changed, generation=current,
                    selected_workspace_ids=set(result["consulted_workspace_ids"]),
                    graph_manifests=graphs,
                )

    def test_context_validation_accepts_all_consulted_graphs_for_empty_refusal(self):
        snapshot = query_snapshot()
        result = query_workspace(snapshot, "primary-fixture-a", "unrelated synthetic question")
        current, graph_map = snapshot._open()
        self.assertEqual(
            result,
            validate_workspace_query_readback(
                result, generation=current,
                selected_workspace_ids=set(result["consulted_workspace_ids"]),
                graph_manifests=[graph_map[item][0]
                                 for item in result["consulted_workspace_ids"]],
            ),
        )


if __name__ == "__main__":
    unittest.main()
