import json
import os
import shutil
import tempfile
import unittest
from copy import copy
from dataclasses import fields
from pathlib import Path
from unittest.mock import patch

from tests import test_ao_lore_evidence_graph as evidence_graph_tests
from ao_lore.workspace_registry import WorkspaceRegistryDependencies, WorkspaceRegistryError
from ao_lore.evidence_graph import (
    EvidenceGraphError,
    publish_evidence_graph,
    recover_evidence_graph,
)
from ao_lore.evidence_freshness import FreshnessStorageError, _locked_freshness
from ao_lore.workspace_runtime import (
    WorkspaceContext,
    _workspace_acquisition_dependencies,
    _workspace_freshness_dependencies,
    _workspace_graph_dependencies,
    open_workspace_context,
)
from tests.test_ao_lore_workspace_registry import definitions, generation, workspace, write_generation


class _HTTP:
    def fetch(self, _locator, **_limits):
        raise AssertionError("network is outside this test")


class WorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "workspaces" / "state"
        self.definitions = definitions()
        self.generation = generation(self.definitions)
        write_generation(self.root, self.generation)
        for item in self.definitions:
            workspace_root = self.state / item["workspace_id"]
            for child in ("sources", "graph", "freshness", "recovery"):
                (workspace_root / child).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp.cleanup()

    def deps(self, failpoint=lambda _name: None):
        return WorkspaceRegistryDependencies(self.root, failpoint)

    def test_context_binds_exact_registry_and_workspace(self):
        context = open_workspace_context(self.deps(), "primary-fixture-a")
        definition = self.definitions[0]
        self.assertEqual(
            [
                "workspace_id", "workspace_type", "registry_generation_id",
                "registry_generation_digest", "definition_digest", "graph_id",
                "graph_digest", "document_store_id",
                "document_generation_digest", "freshness_policy_digest", "state_root",
            ],
            [field.name for field in fields(WorkspaceContext)],
        )
        self.assertEqual("primary-fixture-a", context.workspace_id)
        self.assertEqual("property", context.workspace_type)
        self.assertEqual(self.generation["registry_id"], context.registry_generation_id)
        self.assertEqual(self.generation["registry_digest"], context.registry_generation_digest)
        self.assertEqual(definition["definition_digest"], context.definition_digest)
        self.assertEqual(definition["graph_id"], context.graph_id)
        self.assertEqual(definition["graph_digest"], context.graph_digest)
        self.assertIsNone(context.document_store_id)
        self.assertIsNone(context.document_generation_digest)
        self.assertIsNone(context.freshness_policy_digest)
        self.assertEqual(self.state / "primary-fixture-a", context.state_root)

    def test_registry_selection_precedes_any_state_open(self):
        shutil.rmtree(self.state)
        with self.assertRaisesRegex(WorkspaceRegistryError, "unknown"):
            open_workspace_context(self.deps(), "missing-workspace")
        self.assertFalse(self.state.exists())

    def test_unknown_inactive_and_investigate_workspaces_fail_closed(self):
        for status in ("inactive", "investigate"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                value = workspace("selected-fixture", "property", status=status)
                write_generation(root, generation([value]))
                (root / "workspaces/state/selected-fixture").mkdir(parents=True)
                with self.assertRaises(WorkspaceRegistryError):
                    open_workspace_context(WorkspaceRegistryDependencies(root), "selected-fixture")

    def test_state_child_symlink_and_file_aliases_are_rejected(self):
        selected = self.state / "primary-fixture-a"
        moved = self.root / "moved-state"
        selected.rename(moved)
        os.symlink(moved, selected)
        with self.assertRaises(WorkspaceRegistryError):
            open_workspace_context(self.deps(), "primary-fixture-a")
        selected.unlink()
        (selected / "sources").mkdir(parents=True)
        artifact = selected / "sources" / "artifact.json"
        artifact.write_text("{}", encoding="utf-8")
        os.link(artifact, selected / "sources" / "artifact-alias.json")
        with self.assertRaises(WorkspaceRegistryError):
            open_workspace_context(self.deps(), "primary-fixture-a")

    def test_state_child_replacement_during_open_is_rejected(self):
        selected = self.state / "primary-fixture-a"
        moved = self.state / "primary-fixture-a-old"

        def replace(name):
            if name == "before_workspace_revalidation":
                selected.rename(moved)
                selected.mkdir()

        with self.assertRaises(WorkspaceRegistryError):
            open_workspace_context(self.deps(replace), "primary-fixture-a")

    def test_cross_workspace_bound_artifact_is_rejected(self):
        artifact = self.state / "primary-fixture-a" / "recovery" / "intent.json"
        artifact.write_text(json.dumps({
            "workspace_id": "secondary-fixture-b",
            "graph_id": self.definitions[1]["graph_id"],
            "graph_digest": self.definitions[1]["graph_digest"],
        }), encoding="utf-8")
        with self.assertRaises(WorkspaceRegistryError):
            open_workspace_context(self.deps(), "primary-fixture-a")

    def test_engine_dependencies_are_scoped_to_exact_workspace_children(self):
        context = open_workspace_context(self.deps(), "primary-fixture-a")
        acquisition = _workspace_acquisition_dependencies(
            context, _HTTP(), lambda: "2026-08-13T12:00:00Z", lambda: 0.0)
        graph = _workspace_graph_dependencies(context)
        freshness = _workspace_freshness_dependencies(context)
        self.assertTrue(os.path.samefile(context.state_root / "sources", acquisition.source_root))
        self.assertTrue(os.path.samefile(context.state_root / "graph", graph.root))
        self.assertTrue(os.path.samefile(context.state_root / "freshness", freshness.source_root))

    def test_dependency_creation_never_enumerates_state_siblings(self):
        context = open_workspace_context(self.deps(), "primary-fixture-a")
        state_identity = os.stat(self.state)
        original_listdir = os.listdir

        def reject_state_enumeration(path):
            info = os.fstat(path) if isinstance(path, int) else os.stat(path)
            if (info.st_dev, info.st_ino) == (state_identity.st_dev, state_identity.st_ino):
                raise AssertionError("workspace state siblings were enumerated")
            return original_listdir(path)

        with patch("os.listdir", reject_state_enumeration):
            _workspace_acquisition_dependencies(
                context, _HTTP(), lambda: "2026-08-13T12:00:00Z", lambda: 0.0)
            _workspace_graph_dependencies(context)
            _workspace_freshness_dependencies(context)

    def test_dependency_creation_rejects_workspace_replacement_after_context_open(self):
        constructors = (
            lambda context: _workspace_acquisition_dependencies(
                context, _HTTP(), lambda: "2026-08-13T12:00:00Z", lambda: 0.0),
            _workspace_graph_dependencies,
            _workspace_freshness_dependencies,
        )
        selected = self.state / "primary-fixture-a"
        displaced = self.state / "primary-fixture-a-displaced"
        for constructor in constructors:
            with self.subTest(constructor=constructor):
                context = open_workspace_context(self.deps(), "primary-fixture-a")
                selected.rename(displaced)
                for child in ("sources", "graph", "freshness", "recovery"):
                    (selected / child).mkdir(parents=True, exist_ok=True)
                try:
                    with self.assertRaises(WorkspaceRegistryError):
                        constructor(context)
                finally:
                    shutil.rmtree(selected)
                    displaced.rename(selected)

    def test_fabricated_and_copied_contexts_cannot_bind_engine_roots(self):
        context = open_workspace_context(self.deps(), "primary-fixture-a")
        fabricated = WorkspaceContext(**{
            field.name: getattr(context, field.name) for field in fields(WorkspaceContext)
        })
        for candidate in (fabricated, copy(context)):
            with self.subTest(candidate=candidate), self.assertRaises(WorkspaceRegistryError):
                _workspace_graph_dependencies(candidate)

    def test_frozen_context_mutation_invalidates_its_private_identity(self):
        context = open_workspace_context(self.deps(), "primary-fixture-a")
        object.__setattr__(context, "graph_id", "graph-secondary-fixture-b")
        with self.assertRaises(WorkspaceRegistryError):
            _workspace_graph_dependencies(context)

    def test_graph_operation_rejects_root_swap_between_lstat_and_open(self):
        operations = (
            lambda deps: publish_evidence_graph(evidence_graph_tests.EvidenceGraphTests().build(), deps),
            recover_evidence_graph,
        )
        graph_root = self.state / "primary-fixture-a" / "graph"
        displaced = graph_root.with_name("graph-displaced")
        original_open = os.open
        for operation in operations:
            with self.subTest(operation=operation):
                context = open_workspace_context(self.deps(), "primary-fixture-a")
                dependencies = _workspace_graph_dependencies(context)
                swapped = False

                def swap_before_open(path, flags, mode=0o777, *, dir_fd=None):
                    nonlocal swapped
                    if not swapped and dir_fd is None and os.fspath(path) == os.fspath(graph_root):
                        graph_root.rename(displaced)
                        graph_root.mkdir()
                        swapped = True
                    return original_open(path, flags, mode, dir_fd=dir_fd)

                try:
                    with patch("ao_lore.evidence_graph.os.open", swap_before_open):
                        with self.assertRaisesRegex(EvidenceGraphError, "graph root changed"):
                            operation(dependencies)
                    self.assertEqual([], list(graph_root.iterdir()))
                finally:
                    if graph_root.exists():
                        shutil.rmtree(graph_root)
                    if displaced.exists():
                        displaced.rename(graph_root)

    def test_freshness_operation_rejects_root_swap_before_any_write(self):
        context = open_workspace_context(self.deps(), "primary-fixture-a")
        dependencies = _workspace_freshness_dependencies(context)
        freshness_root = self.state / "primary-fixture-a" / "freshness"
        displaced = freshness_root.with_name("freshness-displaced")
        from ao_lore.evidence_freshness import _open_real_path

        def swap_before_open(path):
            freshness_root.rename(displaced)
            freshness_root.mkdir()
            return _open_real_path(path)

        try:
            with patch("ao_lore.evidence_freshness._open_real_path", swap_before_open):
                with self.assertRaisesRegex(FreshnessStorageError, "source root changed"):
                    with _locked_freshness(dependencies):
                        pass
            self.assertEqual([], list(freshness_root.iterdir()))
        finally:
            if freshness_root.exists():
                shutil.rmtree(freshness_root)
            if displaced.exists():
                displaced.rename(freshness_root)


if __name__ == "__main__":
    unittest.main()
