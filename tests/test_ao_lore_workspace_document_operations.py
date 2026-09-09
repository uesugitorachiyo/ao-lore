import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from ao_lore.benchmark import canonical_digest
from ao_lore.workspace_contracts import AUTHORITY_FIELDS
from ao_lore.workspace_documents import WorkspaceDocumentDependencies, publish_workspace_documents
from ao_lore.workspace_registry import WorkspaceRegistryDependencies, WorkspaceRegistryError, publish_workspace_registry_generation
from ao_lore.workspace_runtime import load_workspace_document_context, open_workspace_context


def bind(value):
    value["definition_digest"] = canonical_digest({key: item for key, item in value.items() if key != "definition_digest"})
    return value


def definition(workspace_id="workspace-a", *, graph=False, status="active"):
    return bind({
        "schema_version": "ao.lore.workspace-definition.v0.2", "workspace_id": workspace_id,
        "workspace_version": 1, "workspace_type": "property", "domain": "synthetic-guidance",
        "jurisdiction": "test-jurisdiction", "lifecycle_status": status,
        "root_workflow_id": "workflow-" + workspace_id, "root_workflow_digest": canonical_digest("workflow-" + workspace_id),
        "source_registry_id": "sources-" + workspace_id, "source_registry_digest": canonical_digest("sources-" + workspace_id),
        "graph_id": "graph-" + workspace_id if graph else None,
        "graph_digest": canonical_digest("graph-" + workspace_id) if graph else None,
        "freshness_policy_id": None, "freshness_policy_digest": None,
        "freshness_summary_status": "not_observed", "freshness_summary_id": None, "freshness_summary_digest": None,
        "reference_workspace_ids": [], "document_store_id": "documents-" + workspace_id,
        "document_generation_digest": None, "definition_digest": canonical_digest("placeholder"),
        **{field: False for field in AUTHORITY_FIELDS},
    })


def document_ir():
    return {
        "schema_version": "ao.lore.document-ir.v0.1", "document_id": "policy-record",
        "source": {"resource": "inbox/policy-record.pdf", "digest": canonical_digest("source"), "media_type": "application/pdf"},
        "blocks": [{"id": "block-1", "type": "paragraph", "text": "procedure record", "source_span": {"page": 1, "start": 0, "end": 16}}],
        "metadata": {"source_id": "source-policy-record", "authority_role": "operator_procedure", "sensitivity": "internal", "version": "1", "qualification_codes": []},
    }


class WorkspaceDocumentOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        publish_workspace_registry_generation((definition(),), WorkspaceRegistryDependencies(self.root))
        self.published = publish_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a", (document_ir(),))

    def tearDown(self):
        self.temp.cleanup()

    def test_document_only_context_opens_without_graph_and_binds_exact_head(self):
        context = open_workspace_context(WorkspaceRegistryDependencies(self.root), "workspace-a")
        self.assertIsNone(context.graph_id); self.assertIsNone(context.graph_digest)
        self.assertEqual("documents-workspace-a", context.document_store_id)
        self.assertEqual(self.published["generation_digest"], context.document_generation_digest)
        snapshot = load_workspace_document_context(WorkspaceRegistryDependencies(self.root), "workspace-a")
        self.assertEqual(self.published["generation_digest"], snapshot.generation["generation_digest"])

    def test_document_operation_does_not_enumerate_sibling_workspaces(self):
        state = self.root / "workspaces/state"
        original = os.listdir
        state_identity = os.stat(state)
        def guarded(path):
            info = os.fstat(path) if isinstance(path, int) else os.stat(path)
            if (info.st_dev, info.st_ino) == (state_identity.st_dev, state_identity.st_ino):
                raise AssertionError("sibling enumeration")
            return original(path)
        with patch("os.listdir", guarded):
            snapshot = load_workspace_document_context(WorkspaceRegistryDependencies(self.root), "workspace-a")
        self.assertEqual("workspace-a", snapshot.generation["workspace_id"])

    def test_document_context_ignores_unrelated_graph_corruption(self):
        artifact = self.root / "workspaces/state/workspace-a/graph/foreign.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({
            "workspace_id": "workspace-b",
            "graph_id": "graph-workspace-b",
            "graph_digest": canonical_digest("graph-workspace-b"),
        }), encoding="utf-8")
        snapshot = load_workspace_document_context(
            WorkspaceRegistryDependencies(self.root), "workspace-a",
        )
        self.assertEqual(
            self.published["generation_digest"],
            snapshot.generation["generation_digest"],
        )

    def test_v0_1_graph_only_has_no_invented_document_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = deepcopy(definition(graph=True)); legacy["schema_version"] = "ao.lore.workspace-definition.v0.1"
            legacy.pop("document_store_id"); legacy.pop("document_generation_digest"); bind(legacy)
            publish_workspace_registry_generation((legacy,), WorkspaceRegistryDependencies(root))
            for child in ("sources", "graph", "freshness", "recovery"):
                (root / "workspaces/state/workspace-a" / child).mkdir(parents=True, exist_ok=True)
            context = open_workspace_context(WorkspaceRegistryDependencies(root), "workspace-a")
            self.assertIsNone(context.document_store_id)
            with self.assertRaises(WorkspaceRegistryError):
                load_workspace_document_context(WorkspaceRegistryDependencies(root), "workspace-a")


if __name__ == "__main__": unittest.main()
