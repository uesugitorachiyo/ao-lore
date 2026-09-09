import builtins
import unittest
from copy import deepcopy
from unittest.mock import patch

from ao_lore._strict_io import ContractError
from ao_lore.benchmark import canonical_digest
from ao_lore.document_evidence_query import query_workspace_documents
from ao_lore.workspace_contracts import AUTHORITY_FIELDS, validate_workspace_query_readback
from ao_lore.workspace_query import (
    WorkspaceDocumentQuerySnapshot,
    WorkspaceGraphSnapshot,
    WorkspaceQueryError,
    WorkspaceQuerySnapshot,
    query_workspace,
)
from ao_lore.workspace_registry import WorkspaceRegistrySnapshot, select_workspace
from tests.test_ao_lore_document_evidence_query import generation as document_generation
from tests.test_ao_lore_workspace_query import graph_variant, rebind


_DOCUMENT_FIXTURE = document_generation()
DOCUMENT_PROMPT = " ".join(
    _DOCUMENT_FIXTURE["documents"][0]["document_ir"]["blocks"][0]["text"].split()[:2]
)


def definition(workspace_id, document=None, graph=None, references=(), *, workspace_version=1):
    value = {
        "schema_version": "ao.lore.workspace-definition.v0.2", "workspace_id": workspace_id,
        "workspace_version": workspace_version, "workspace_type": "property" if workspace_id == "workspace-a" else "reference",
        "domain": "synthetic-guidance", "jurisdiction": "fixture-scope", "lifecycle_status": "active",
        "root_workflow_id": "workflow-" + workspace_id, "root_workflow_digest": canonical_digest("workflow-" + workspace_id),
        "source_registry_id": "sources-" + workspace_id, "source_registry_digest": canonical_digest("sources-" + workspace_id),
        "graph_id": None if graph is None else graph["graph_id"], "graph_digest": None if graph is None else graph["graph_digest"],
        "freshness_policy_id": None, "freshness_policy_digest": None, "freshness_summary_status": "not_observed",
        "freshness_summary_id": None, "freshness_summary_digest": None, "reference_workspace_ids": list(references),
        "document_store_id": "documents-" + workspace_id,
        "document_generation_digest": None if document is None else document["generation_digest"],
        "definition_digest": canonical_digest("placeholder"), **{field: False for field in AUTHORITY_FIELDS},
    }
    return rebind(value, "definition_digest")


def documents(workspace_id="workspace-a", *, restricted=False, stale=False):
    value = document_generation(sensitivity="restricted" if restricted else "internal", freshness="stale" if stale else "current")
    value["workspace_id"] = workspace_id; value["document_store_id"] = "documents-" + workspace_id
    value["generation_id"] = "documents-" + workspace_id + "-0000000001"
    value["generation_digest"] = canonical_digest({key: item for key, item in value.items() if key != "generation_digest"})
    return value


def snapshot(*, combined=False, graph_only=False, restricted=False, stale=False, references=False):
    primary_docs = None if graph_only else documents(restricted=restricted, stale=stale)
    primary_graph = graph_variant("graph-workspace-a") if combined or graph_only else None
    definitions = [definition("workspace-a", primary_docs, primary_graph, ("reference-a",) if references else ())]
    doc_items = [] if primary_docs is None else [WorkspaceDocumentQuerySnapshot("workspace-a", primary_docs)]
    graph_items = [] if primary_graph is None else [WorkspaceGraphSnapshot("workspace-a", primary_graph)]
    if references:
        reference_docs = documents("reference-a")
        definitions.append(definition("reference-a", reference_docs))
        doc_items.append(WorkspaceDocumentQuerySnapshot("reference-a", reference_docs))
    definitions.sort(key=lambda item: item["workspace_id"])
    registry_value = {
        "schema_version": "ao.lore.workspace-registry-generation.v0.1",
        "registry_id": "registry-01",
        "sequence": 1 if not doc_items else 2,
        "predecessor_registry_digest": None if not doc_items else primary_docs["registry_digest"],
        "generated_at": "2026-08-13T12:00:00Z",
        "workspaces": definitions,
        "registry_digest": canonical_digest("placeholder"),
        **{field: False for field in AUTHORITY_FIELDS},
    }
    registry_value["registry_digest"] = canonical_digest({key: item for key, item in registry_value.items() if key != "registry_digest"})
    registry = WorkspaceRegistrySnapshot((registry_value,), tuple(definitions))
    return WorkspaceQuerySnapshot(registry, select_workspace(registry, "workspace-a"), graph_items, documents=doc_items)


class ProgressiveQueryTests(unittest.TestCase):
    def test_document_only_answers_without_graph_using_exact_kind(self):
        result = query_workspace(snapshot(), "workspace-a", DOCUMENT_PROMPT)
        self.assertEqual("ao.lore.workspace-query-readback.v0.2", result["schema_version"])
        self.assertEqual("answer", result["outcome"])
        self.assertEqual({"document_block"}, {item["evidence_kind"] for item in result["evidence"]})

    def test_combined_preserves_document_and_graph_kinds(self):
        result = query_workspace(
            snapshot(combined=True), "workspace-a", DOCUMENT_PROMPT + " policy control",
        )
        self.assertEqual({"document_block", "graph_claim"}, {item["evidence_kind"] for item in result["evidence"]})

    def test_v0_2_graph_only_uses_explicit_graph_kinds(self):
        result = query_workspace(snapshot(graph_only=True), "workspace-a", "How do policy control and control guidance connect?")
        self.assertEqual("answer", result["outcome"])
        self.assertEqual({"graph_claim"}, {item["evidence_kind"] for item in result["evidence"]})

    def test_direct_reference_origins_and_same_local_ids_remain_distinct(self):
        result = query_workspace(snapshot(references=True), "workspace-a", DOCUMENT_PROMPT)
        self.assertEqual({"workspace-a", "reference-a"}, {item["workspace_id"] for item in result["evidence"]})
        document_id = next(
            item["document_id"] for item in result["evidence"]
            if item["evidence_kind"] == "document_block"
        )
        same_local_document = [
            item for item in result["evidence"]
            if item["evidence_kind"] == "document_block"
            and item["document_id"] == document_id
        ]
        self.assertEqual(6, len(same_local_document))
        self.assertEqual(6, len({tuple(item.values()) for item in same_local_document}))

    def test_arbitrary_prompt_words_do_not_override_progressive_evidence(self):
        current = snapshot()
        baseline = query_workspace(current, "workspace-a", DOCUMENT_PROMPT)
        with_arbitrary_word = query_workspace(
            current, "workspace-a", DOCUMENT_PROMPT + " " + "li" + "able",
        )
        for field in ("outcome", "evidence", "qualifications"):
            self.assertEqual(baseline[field], with_arbitrary_word[field])
        for field in AUTHORITY_FIELDS:
            self.assertIs(False, with_arbitrary_word[field])
        with_qualification_word = query_workspace(
            current,
            "workspace-a",
            DOCUMENT_PROMPT + " " + "with" + "out",
        )
        for field in ("outcome", "evidence", "qualifications"):
            self.assertEqual(baseline[field], with_qualification_word[field])

    def test_restriction_and_freshness_precedence(self):
        restricted = query_workspace(snapshot(restricted=True), "workspace-a", DOCUMENT_PROMPT)
        self.assertEqual(("refuse", []), (restricted["outcome"], restricted["evidence"]))
        stale = query_workspace(snapshot(stale=True), "workspace-a", DOCUMENT_PROMPT)
        self.assertEqual("investigate", stale["outcome"])

    def test_optional_graph_failure_falls_back_to_documents_and_query_has_zero_io(self):
        current = snapshot(combined=True)
        with patch("ao_lore.workspace_query.query_evidence_graph", side_effect=ValueError("graph unavailable")), patch.object(builtins, "open", side_effect=AssertionError("I/O")):
            result = query_workspace(current, "workspace-a", DOCUMENT_PROMPT)
        self.assertEqual("partial", result["outcome"])
        self.assertEqual({"document_block"}, {item["evidence_kind"] for item in result["evidence"]})

    def test_snapshot_rejects_detached_document_generation_without_head_lineage(self):
        detached = documents()
        detached["registry_digest"] = canonical_digest("unrelated-registry-head")
        detached["generation_digest"] = canonical_digest({
            key: item for key, item in detached.items() if key != "generation_digest"
        })
        current = snapshot()
        changed_registry = deepcopy(current.registry.generations[-1])
        changed_definition = next(
            item for item in changed_registry["workspaces"]
            if item["workspace_id"] == "workspace-a"
        )
        changed_definition["document_generation_digest"] = detached["generation_digest"]
        changed_definition["definition_digest"] = canonical_digest({
            key: item for key, item in changed_definition.items()
            if key != "definition_digest"
        })
        changed_registry["registry_digest"] = canonical_digest({
            key: item for key, item in changed_registry.items()
            if key != "registry_digest"
        })
        registry = WorkspaceRegistrySnapshot(
            (changed_registry,), tuple(changed_registry["workspaces"]),
        )
        with self.assertRaisesRegex(WorkspaceQueryError, "document lineage differs"):
            WorkspaceQuerySnapshot(
                registry,
                select_workspace(registry, "workspace-a"),
                (),
                documents=[WorkspaceDocumentQuerySnapshot("workspace-a", detached)],
            )

    def test_readback_validation_rejects_detached_document_generation_without_head_lineage(self):
        detached = documents()
        detached["registry_digest"] = canonical_digest("unrelated-registry-head")
        detached["generation_digest"] = canonical_digest({
            key: item for key, item in detached.items() if key != "generation_digest"
        })
        changed_definition = definition(
            "workspace-a", detached, None, (), workspace_version=2,
        )
        registry_value = {
            "schema_version": "ao.lore.workspace-registry-generation.v0.1",
            "registry_id": "registry-01",
            "sequence": 2,
            "predecessor_registry_digest": canonical_digest("expected-predecessor"),
            "generated_at": "2026-08-13T12:00:00Z",
            "workspaces": [changed_definition],
            "registry_digest": canonical_digest("placeholder"),
            **{field: False for field in AUTHORITY_FIELDS},
        }
        registry_value["registry_digest"] = canonical_digest({
            key: item for key, item in registry_value.items()
            if key != "registry_digest"
        })
        document_result = query_workspace_documents(detached, "workspace-a", DOCUMENT_PROMPT)
        result = {
            "schema_version": "ao.lore.workspace-query-readback.v0.2",
            "query_id": "query-" + canonical_digest("workspace-query-lineage")[-24:],
            "prompt_digest": canonical_digest(DOCUMENT_PROMPT),
            "registry_id": registry_value["registry_id"],
            "registry_digest": registry_value["registry_digest"],
            "primary_workspace_id": "workspace-a",
            "consulted_workspace_ids": ["workspace-a"],
            "outcome": document_result["outcome"],
            "reason_code": "ok",
            "evidence": [
                {
                    "workspace_id": "workspace-a",
                    "document_store_id": detached["document_store_id"],
                    "generation_digest": detached["generation_digest"],
                    "document_id": item["document_id"],
                    "source_id": item["source_id"],
                    "evidence_kind": "document_block",
                    "evidence_id": item["evidence_id"],
                    "evidence_digest": item["block_digest"],
                }
                for item in document_result["evidence"]
            ],
            "qualifications": [],
            "readback_digest": canonical_digest("placeholder"),
            **{field: False for field in AUTHORITY_FIELDS},
        }
        result["evidence"].sort(key=lambda item: tuple(item.values()))
        result["readback_digest"] = canonical_digest({
            key: item for key, item in result.items() if key != "readback_digest"
        })
        with self.assertRaisesRegex(ContractError, "document context lineage differs"):
            validate_workspace_query_readback(
                result,
                generation=registry_value,
                selected_workspace_ids={"workspace-a"},
                document_generations=[detached],
            )

    def test_progressive_query_rejects_unicode_category_c_prompt(self):
        with self.assertRaises(WorkspaceQueryError):
            query_workspace(snapshot(), "workspace-a", "procedure\u202erecord")

    def test_no_hit_refuses(self):
        result = query_workspace(snapshot(), "workspace-a", "unmatched")
        self.assertEqual(("refuse", []), (result["outcome"], result["evidence"]))


if __name__ == "__main__": unittest.main()
