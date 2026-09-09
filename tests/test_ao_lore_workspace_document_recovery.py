import json
import tempfile
import unittest
from pathlib import Path

from ao_lore.benchmark import canonical_digest
from ao_lore.workspace_documents import (
    DOCUMENT_PUBLICATION_FAILPOINTS,
    WorkspaceDocumentDependencies,
    WorkspaceDocumentError,
    load_workspace_documents,
    publish_workspace_documents,
    recover_workspace_documents,
)
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    load_workspace_registry,
    publish_workspace_registry_generation,
    select_workspace,
)
from tests.test_ao_lore_workspace_documents import (
    document_ir,
    workspace_definition,
)


class SimulatedCrash(RuntimeError):
    pass


class WorkspaceDocumentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def publish_registry(self, root: Path | None = None, *, workspace_version: int = 1, status: str = "active") -> dict:
        return publish_workspace_registry_generation(
            (workspace_definition(workspace_version=workspace_version, status=status),),
            WorkspaceRegistryDependencies(self.root if root is None else root),
        )

    def test_recovery_resumes_every_durable_boundary_without_republishing(self):
        for failpoint in DOCUMENT_PUBLICATION_FAILPOINTS:
            with self.subTest(failpoint=failpoint):
                root = self.root / failpoint
                self.publish_registry(root)
                def crash(name):
                    if name == failpoint:
                        raise SimulatedCrash(name)
                with self.assertRaises(SimulatedCrash):
                    publish_workspace_documents(WorkspaceDocumentDependencies(root, crash), "workspace-a", (document_ir("a"),))
                recovered = recover_workspace_documents(WorkspaceDocumentDependencies(root), "workspace-a")
                self.assertIn(recovered["status"], {"recovered", "complete"})
                snapshot = load_workspace_documents(WorkspaceDocumentDependencies(root), "workspace-a")
                registry = load_workspace_registry(WorkspaceRegistryDependencies(root))
                current = registry.generations[-1]
                selection = select_workspace(registry, "workspace-a")
                self.assertEqual(1, snapshot.generation["sequence"])
                self.assertEqual(2, selection.primary["workspace_version"])
                self.assertEqual(
                    snapshot.generation["generation_digest"],
                    selection.primary["document_generation_digest"],
                )
                self.assertEqual(
                    current["predecessor_registry_digest"],
                    snapshot.generation["registry_digest"],
                )
                generations = root / "workspaces/state/workspace-a/documents/generations"
                self.assertEqual(1, len(list(generations.iterdir())))

    def test_recovery_without_state_is_empty_and_creates_nothing(self):
        self.publish_registry()
        report = recover_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a")
        self.assertEqual("empty", report["status"])
        self.assertFalse((self.root / "workspaces" / "state" / "workspace-a" / "documents").exists())

    def test_foreign_recovery_and_staging_entries_are_preserved_and_fail_closed(self):
        self.publish_registry()
        publish_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a", (document_ir("a"),))
        documents = self.root / "workspaces/state/workspace-a/documents"
        foreign_recovery = documents / "recovery/foreign"; foreign_recovery.write_text("keep", encoding="utf-8")
        foreign_staging = documents / "staging/foreign"; foreign_staging.mkdir()
        with self.assertRaises(WorkspaceDocumentError):
            recover_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a")
        self.assertTrue(foreign_recovery.exists()); self.assertTrue(foreign_staging.exists())

    def test_history_and_recovery_inventory_are_bounded(self):
        self.publish_registry()
        publish_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a", (document_ir("a"),))
        recovery = self.root / "workspaces/state/workspace-a/documents/recovery"
        for index in range(130):
            (recovery / f"foreign-{index:03d}").write_text("x", encoding="utf-8")
        with self.assertRaises(WorkspaceDocumentError):
            recover_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a")

    def test_recovery_rejects_registry_drift_before_resuming_active_intent(self):
        self.publish_registry(workspace_version=1)

        def crash(name):
            if name == "after_document_intent":
                raise SimulatedCrash(name)

        with self.assertRaises(SimulatedCrash):
            publish_workspace_documents(
                WorkspaceDocumentDependencies(self.root, crash),
                "workspace-a",
                (document_ir("a"),),
            )

        self.publish_registry(workspace_version=2)
        with self.assertRaises(WorkspaceDocumentError):
            recover_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a")

    def test_exact_retry_after_recovery_reports_unchanged_when_generation_was_reused(self):
        self.publish_registry()

        def crash(name):
            if name == "after_document_publication":
                raise SimulatedCrash(name)

        with self.assertRaises(SimulatedCrash):
            publish_workspace_documents(
                WorkspaceDocumentDependencies(self.root, crash),
                "workspace-a",
                (document_ir("a"),),
            )

        recovered = recover_workspace_documents(
            WorkspaceDocumentDependencies(self.root),
            "workspace-a",
        )
        self.assertEqual("recovered", recovered["status"])

        retry = publish_workspace_documents(
            WorkspaceDocumentDependencies(self.root),
            "workspace-a",
            (document_ir("a"),),
        )
        self.assertEqual("unchanged", retry["status"])
        self.assertEqual(1, retry["sequence"])

    def test_completed_recovery_requires_matching_published_generation(self):
        self.publish_registry()
        publish_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a", (document_ir("a"),))
        generations = self.root / "workspaces/state/workspace-a/documents/generations"
        for child in generations.iterdir():
            for nested in child.iterdir():
                nested.unlink()
            child.rmdir()
        with self.assertRaises(WorkspaceDocumentError):
            recover_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a")

    def test_completed_recovery_reopens_and_rejects_drifted_published_generation(self):
        self.publish_registry()
        publish_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a", (document_ir("a"),))
        manifest = next((self.root / "workspaces/state/workspace-a/documents/generations").iterdir()) / "manifest.json"
        value = json.loads(manifest.read_text(encoding="utf-8"))
        value["created_at"] = "2026-08-14T00:00:01Z"
        value["generation_digest"] = canonical_digest({key: item for key, item in value.items() if key != "generation_digest"})
        manifest.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        with self.assertRaises(WorkspaceDocumentError):
            recover_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a")

    def test_successive_publications_compact_terminal_recovery_runs(self):
        self.publish_registry()
        for index in range(80):
            publish_workspace_documents(
                WorkspaceDocumentDependencies(self.root),
                "workspace-a",
                (document_ir(f"doc-{index:03d}"),),
            )
        recovery = self.root / "workspaces/state/workspace-a/documents/recovery"
        self.assertGreater(len(list(recovery.iterdir())), 0)
        self.assertLess(len(list(recovery.iterdir())), 80)
        report = recover_workspace_documents(WorkspaceDocumentDependencies(self.root), "workspace-a")
        self.assertEqual("complete", report["status"])


if __name__ == "__main__":
    unittest.main()
