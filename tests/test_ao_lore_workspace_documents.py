import os
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from ao_lore.benchmark import canonical_digest
import ao_lore.workspace_documents as workspace_documents_module
from ao_lore.workspace_contracts import AUTHORITY_FIELDS
from ao_lore.workspace_documents import (
    _WorkspaceDocumentProofDependencies,
    WorkspaceDocumentDependencies,
    WorkspaceDocumentError,
    load_workspace_documents,
    publish_workspace_documents,
    recover_workspace_documents,
)
from ao_lore.workspace_registry import (
    _WorkspaceRegistryProofDependencies,
    WorkspaceRegistryDependencies,
    load_workspace_registry,
    publish_workspace_registry_generation,
    select_workspace,
)


def document_ir(document_id: str, text: str = "verified text") -> dict:
    return {
        "schema_version": "ao.lore.document-ir.v0.1", "document_id": document_id,
        "source": {"resource": "inbox/manual.pdf", "digest": canonical_digest("source-" + document_id), "media_type": "application/pdf"},
        "parser": {"parser_id": "fixture-parser", "parser_version": "1", "configuration_digest": canonical_digest("parser")},
        "blocks": [{"id": "block-1", "type": "paragraph", "text": text, "source_span": {"page": 1, "start": 0, "end": len(text)}}],
        "metadata": {"source_id": "source-" + document_id, "authority_role": "operator_procedure", "sensitivity": "internal", "version": "1", "effective_date": "2026-08-13", "qualification_codes": ["fixture-only"]},
    }


def authority() -> dict:
    return {field: False for field in AUTHORITY_FIELDS}


def workspace_definition(*, workspace_id: str = "workspace-a", workspace_version: int = 1, status: str = "active",
                         document_generation_digest: str | None = None) -> dict:
    value = {
        "schema_version": "ao.lore.workspace-definition.v0.2",
        "workspace_id": workspace_id,
        "workspace_version": workspace_version,
        "workspace_type": "property",
        "domain": "synthetic-guidance",
        "jurisdiction": "test-jurisdiction",
        "lifecycle_status": status,
        "root_workflow_id": f"workflow-{workspace_id}",
        "root_workflow_digest": canonical_digest("workflow-" + workspace_id),
        "source_registry_id": f"sources-{workspace_id}",
        "source_registry_digest": canonical_digest("sources-" + workspace_id),
        "graph_id": f"graph-{workspace_id}",
        "graph_digest": canonical_digest("graph-" + workspace_id),
        "freshness_policy_id": None,
        "freshness_policy_digest": None,
        "freshness_summary_status": "not_observed",
        "freshness_summary_id": None,
        "freshness_summary_digest": None,
        "reference_workspace_ids": [],
        "document_store_id": "documents-" + workspace_id,
        "document_generation_digest": document_generation_digest,
        "definition_digest": "sha256:" + "0" * 64,
        **authority(),
    }
    value["definition_digest"] = canonical_digest({key: item for key, item in value.items() if key != "definition_digest"})
    return value


def seed_registry(root: Path, *, workspace_version: int = 1, status: str = "active",
                  document_generation_digest: str | None = None) -> dict:
    return publish_workspace_registry_generation(
        (workspace_definition(
            workspace_version=workspace_version,
            status=status,
            document_generation_digest=document_generation_digest,
        ),),
        WorkspaceRegistryDependencies(root),
    )


class WorkspaceDocumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.deps = WorkspaceDocumentDependencies(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_read_does_not_create_runtime_state(self):
        seed_registry(self.root)
        snapshot = load_workspace_documents(self.deps, "workspace-a")
        self.assertIsNone(snapshot.generation)
        self.assertFalse((self.root / "workspaces" / "state" / "workspace-a" / "documents").exists())

    def test_empty_load_requires_registered_active_workspace(self):
        with self.assertRaises(WorkspaceDocumentError):
            load_workspace_documents(self.deps, "workspace-a")
        seed_registry(self.root, status="inactive")
        with self.assertRaises(WorkspaceDocumentError):
            load_workspace_documents(self.deps, "workspace-a")
        seed_registry(self.root, workspace_version=2, status="investigate")
        with self.assertRaises(WorkspaceDocumentError):
            load_workspace_documents(self.deps, "workspace-a")
        self.assertFalse((self.root / "workspaces" / "state" / "workspace-a" / "documents").exists())

    def test_empty_recovery_requires_registered_active_workspace(self):
        with self.assertRaises(WorkspaceDocumentError):
            recover_workspace_documents(self.deps, "workspace-a")
        seed_registry(self.root, status="inactive")
        with self.assertRaises(WorkspaceDocumentError):
            recover_workspace_documents(self.deps, "workspace-a")
        seed_registry(self.root, workspace_version=2, status="investigate")
        with self.assertRaises(WorkspaceDocumentError):
            recover_workspace_documents(self.deps, "workspace-a")
        self.assertFalse((self.root / "workspaces" / "state" / "workspace-a" / "documents").exists())

    def test_generation_publish_and_reopen_are_exact_and_detached(self):
        initial_registry = seed_registry(self.root)
        report = publish_workspace_documents(self.deps, "workspace-a", (document_ir("a"),))
        self.assertEqual("published", report["status"])
        registry = load_workspace_registry(WorkspaceRegistryDependencies(self.root))
        current = registry.generations[-1]
        selection = select_workspace(registry, "workspace-a")
        self.assertEqual(initial_registry["registry_digest"], report["registry_digest"])
        self.assertEqual(initial_registry["registry_digest"], current["predecessor_registry_digest"])
        self.assertEqual(2, selection.primary["workspace_version"])
        self.assertEqual(report["generation_digest"], selection.primary["document_generation_digest"])
        snapshot = load_workspace_documents(self.deps, "workspace-a")
        self.assertEqual(report["generation_digest"], snapshot.generation["generation_digest"])
        self.assertEqual(selection.primary["document_store_id"], snapshot.generation["document_store_id"])
        self.assertEqual(current["predecessor_registry_digest"], snapshot.generation["registry_digest"])
        self.assertEqual("a", snapshot.generation["documents"][0]["document_id"])
        snapshot.generation["documents"][0]["document_ir"]["blocks"][0]["text"] = "changed"
        self.assertEqual("verified text", load_workspace_documents(self.deps, "workspace-a").generation["documents"][0]["document_ir"]["blocks"][0]["text"])

    def test_private_dependency_clock_binds_document_and_registry_generations(self):
        initial_time = "2026-08-14T19:20:21Z"
        publication_time = "2026-08-14T19:20:22Z"
        publish_workspace_registry_generation(
            (workspace_definition(),),
            _WorkspaceRegistryProofDependencies(
                self.root, clock=lambda: initial_time,
            ),
        )
        with self.assertRaises(TypeError):
            WorkspaceDocumentDependencies(
                self.root, clock=lambda: publication_time,
            )
        dependencies = _WorkspaceDocumentProofDependencies(
            self.root, clock=lambda: publication_time,
        )

        published = publish_workspace_documents(
            dependencies, "workspace-a", (document_ir("a"),),
        )
        registry = load_workspace_registry(WorkspaceRegistryDependencies(self.root))

        self.assertEqual(publication_time, published["created_at"])
        self.assertEqual(publication_time, registry.generations[-1]["generated_at"])

    def test_second_publication_is_contiguous_cumulative_and_exact_retry_is_idempotent(self):
        seed_registry(self.root)
        first = publish_workspace_documents(self.deps, "workspace-a", (document_ir("a"),))
        retry = publish_workspace_documents(
            self.deps,
            "workspace-a",
            (deepcopy(document_ir("a")),),
        )
        self.assertEqual("published", first["status"])
        self.assertEqual("unchanged", retry["status"])
        self.assertEqual(first["generation_digest"], retry["generation_digest"])
        second = publish_workspace_documents(self.deps, "workspace-a", (document_ir("b"),))
        registry = load_workspace_registry(WorkspaceRegistryDependencies(self.root))
        current = registry.generations[-1]
        selection = select_workspace(registry, "workspace-a")
        self.assertEqual("published", second["status"])
        self.assertEqual(2, second["sequence"])
        self.assertEqual(3, selection.primary["workspace_version"])
        self.assertEqual(first["generation_digest"], second["prior_generation_digest"])
        self.assertEqual(registry.generations[-2]["registry_digest"], second["registry_digest"])
        self.assertEqual(registry.generations[-2]["registry_digest"], current["predecessor_registry_digest"])
        self.assertEqual(second["generation_digest"], selection.primary["document_generation_digest"])
        self.assertEqual(["a", "b"], [item["document_id"] for item in second["documents"]])

    def test_steady_state_publish_does_not_revalidate_completed_terminal_runs(self):
        seed_registry(self.root)
        for document_id in ("a", "b", "c"):
            publish_workspace_documents(self.deps, "workspace-a", (document_ir(document_id),))
        with mock.patch.object(
            workspace_documents_module,
            "_validate_published_generation",
            side_effect=AssertionError("steady-state publish revalidated completed terminal run"),
        ):
            report = publish_workspace_documents(self.deps, "workspace-a", (document_ir("d"),))
        self.assertEqual("published", report["status"])
        self.assertEqual(4, report["sequence"])

    def test_competing_identical_publishers_serialize_to_one_generation(self):
        seed_registry(self.root)
        barrier = threading.Barrier(2); results = []; failures = []
        def run():
            try:
                barrier.wait(); results.append(publish_workspace_documents(self.deps, "workspace-a", (document_ir("a"),)))
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(failures)
        self.assertEqual(["published", "unchanged"], sorted(result["status"] for result in results))
        self.assertEqual(results[0]["generation_digest"], results[1]["generation_digest"])
        generations = self.root / "workspaces/state/workspace-a/documents/generations"
        self.assertEqual(1, len(list(generations.iterdir())))

    def test_expected_registry_binding_rejects_drift_before_document_state_write(self):
        published = seed_registry(self.root)
        selection = select_workspace(
            load_workspace_registry(WorkspaceRegistryDependencies(self.root)),
            "workspace-a",
        )
        self.assertEqual(published["registry_digest"], selection.generation["registry_digest"])
        seed_registry(self.root, workspace_version=2)
        with self.assertRaises(WorkspaceDocumentError):
            publish_workspace_documents(
                self.deps,
                "workspace-a",
                (document_ir("a"),),
                expected_registry_digest=selection.generation["registry_digest"],
                expected_definition_digest=selection.primary["definition_digest"],
            )
        self.assertFalse((self.root / "workspaces/state/workspace-a/documents").exists())

    def test_reader_rejects_symlink_hardlink_fifo_and_noncontiguous_history(self):
        seed_registry(self.root)
        publish_workspace_documents(self.deps, "workspace-a", (document_ir("a"),))
        manifest = next((self.root / "workspaces/state/workspace-a/documents/generations").iterdir()) / "manifest.json"
        replacement = manifest.with_name("replacement")
        os.link(manifest, replacement)
        with self.assertRaises(WorkspaceDocumentError):
            load_workspace_documents(self.deps, "workspace-a")

    def test_publication_requires_a_registered_active_workspace_before_creating_state(self):
        with self.assertRaises(WorkspaceDocumentError):
            publish_workspace_documents(self.deps, "workspace-a", (document_ir("a"),))
        self.assertFalse((self.root / "workspaces" / "state").exists())
        seed_registry(self.root, status="inactive")
        with self.assertRaises(WorkspaceDocumentError):
            publish_workspace_documents(self.deps, "workspace-a", (document_ir("a"),))
        seed_registry(self.root, workspace_version=2, status="investigate")
        with self.assertRaises(WorkspaceDocumentError):
            publish_workspace_documents(self.deps, "workspace-a", (document_ir("a"),))
        self.assertFalse((self.root / "workspaces" / "state" / "workspace-a" / "documents").exists())

    def test_reader_rejects_newer_registry_head_even_if_it_points_to_same_generation(self):
        seed_registry(self.root, workspace_version=1)
        published = publish_workspace_documents(self.deps, "workspace-a", (document_ir("a"),))
        seed_registry(
            self.root,
            workspace_version=3,
            document_generation_digest=published["generation_digest"],
        )
        with self.assertRaises(WorkspaceDocumentError):
            load_workspace_documents(self.deps, "workspace-a")

    def test_workspace_identity_is_strict_and_sibling_is_not_opened(self):
        for workspace_id in ("../sibling", "/tmp/workspace", "UPPER", ""):
            with self.subTest(workspace_id=workspace_id), self.assertRaises(WorkspaceDocumentError):
                load_workspace_documents(self.deps, workspace_id)


if __name__ == "__main__":
    unittest.main()
