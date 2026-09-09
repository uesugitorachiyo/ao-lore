import json
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path

from ao_lore.benchmark import canonical_digest
from ao_lore.workspace_contracts import AUTHORITY_FIELDS, WORKSPACE_SCHEMA_VERSIONS
from ao_lore.workspace_contracts import validate_workspace_registry_inspection
from ao_lore.workspace_registry import (
    _WorkspaceRegistryProofDependencies,
    WorkspaceRegistryDependencies,
    WorkspaceRegistryError,
    inspect_workspace_registry,
    load_workspace_registry,
    publish_workspace_registry_generation,
    select_workspace,
)


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _bind(value: dict, field: str) -> dict:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def workspace(workspace_id: str, kind: str = "reference", references=(), status="active") -> dict:
    return _bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[0], "workspace_id": workspace_id,
        "workspace_version": 1, "workspace_type": kind, "domain": "synthetic-guidance",
        "jurisdiction": "test-jurisdiction", "lifecycle_status": status,
        "root_workflow_id": "workflow-" + workspace_id, "root_workflow_digest": _digest("1"),
        "source_registry_id": "sources-" + workspace_id, "source_registry_digest": _digest("2"),
        "graph_id": "graph-" + workspace_id, "graph_digest": _digest("3"),
        "freshness_policy_id": None, "freshness_policy_digest": None,
        "freshness_summary_status": "not_observed", "freshness_summary_id": None,
        "freshness_summary_digest": None, "reference_workspace_ids": list(references),
        "definition_digest": _digest("0"), **{field: False for field in AUTHORITY_FIELDS},
    }, "definition_digest")


def definitions() -> list[dict]:
    return [
        workspace("primary-fixture-a", "property", ("shared-reference-fixture",)),
        workspace("secondary-fixture-b", "property"),
        workspace("shared-reference-fixture"),
    ]


def generation(defs=None, sequence=1, predecessor=None) -> dict:
    value = {
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[1], "registry_id": f"registry-{sequence:02d}",
        "sequence": sequence, "predecessor_registry_digest": predecessor,
        "generated_at": f"2026-08-13T12:00:{sequence:02d}Z",
        "workspaces": definitions() if defs is None else defs, "registry_digest": _digest("0"),
        **{field: False for field in AUTHORITY_FIELDS},
    }
    return _bind(value, "registry_digest")


def write_generation(root: Path, value: dict) -> Path:
    target = root / "workspaces" / "registry" / "generations" / f"{value['sequence']:010d}-{value['registry_id']}"
    definitions_root = target / "definitions"
    definitions_root.mkdir(parents=True)
    encoded = lambda item: (json.dumps(item, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n")
    (target / "manifest.json").write_text(encoded(value), encoding="utf-8")
    for item in value["workspaces"]:
        (definitions_root / (item["workspace_id"] + ".json")).write_text(encoded(item), encoding="utf-8")
    return target


class WorkspaceRegistryReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.deps = WorkspaceRegistryDependencies(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_runtime_has_empty_registry_view_without_writes(self):
        snapshot = load_workspace_registry(self.deps)
        self.assertEqual((), snapshot.generations)
        self.assertEqual((), snapshot.workspaces)
        self.assertFalse((self.root / "workspaces").exists())
        inspection = inspect_workspace_registry(snapshot)
        self.assertEqual("empty", inspection["status"])
        self.assertEqual(inspection, validate_workspace_registry_inspection(inspection, generation=None))
        self.assertFalse((self.root / "workspaces").exists())

    def test_complete_registry_replay_and_selection_resolve_only_direct_references(self):
        first = generation()
        write_generation(self.root, first)
        snapshot = load_workspace_registry(self.deps)
        self.assertEqual((first,), snapshot.generations)
        self.assertEqual(3, len(snapshot.workspaces))
        selection = select_workspace(snapshot, "primary-fixture-a")
        self.assertEqual("primary-fixture-a", selection.primary["workspace_id"])
        self.assertEqual(("shared-reference-fixture",), tuple(x["workspace_id"] for x in selection.references))
        self.assertEqual("active", inspect_workspace_registry(snapshot)["status"])

    def test_loaded_and_selected_values_are_deep_detached(self):
        write_generation(self.root, generation())
        one = load_workspace_registry(self.deps)
        one.generations[0]["workspaces"][0]["workspace_id"] = "mutated"
        two = load_workspace_registry(self.deps)
        selection = select_workspace(two, "primary-fixture-a")
        selection.primary["workspace_id"] = "also-mutated"
        self.assertEqual("primary-fixture-a", select_workspace(two, "primary-fixture-a").primary["workspace_id"])

    def test_selection_rejects_unknown_inactive_and_investigate_workspaces(self):
        values = definitions()
        values[1] = workspace("secondary-fixture-b", "property", status="inactive")
        write_generation(self.root, generation(values))
        snapshot = load_workspace_registry(self.deps)
        for workspace_id in ("missing-workspace", "secondary-fixture-b"):
            with self.assertRaises(WorkspaceRegistryError):
                select_workspace(snapshot, workspace_id)

    def test_reader_rejects_cross_workspace_identity_collisions(self):
        for field in ("root_workflow_id", "source_registry_id", "graph_id"):
            values = definitions()
            values[1][field] = values[0][field]
            values[1] = _bind(values[1], "definition_digest")
            write_generation(self.root, generation(values))
            with self.subTest(field=field), self.assertRaises(WorkspaceRegistryError):
                load_workspace_registry(self.deps)
            for child in (self.root / "workspaces/registry/generations").iterdir():
                import shutil
                shutil.rmtree(child)

    def test_shared_freshness_identities_are_contract_valid_and_not_global_collisions(self):
        values = definitions()
        for index in (0, 1):
            values[index]["freshness_policy_id"] = "shared-policy"
            values[index]["freshness_policy_digest"] = _digest("4")
            values[index]["freshness_summary_status"] = "observed"
            values[index]["freshness_summary_id"] = "shared-summary"
            values[index]["freshness_summary_digest"] = _digest("5")
            values[index] = _bind(values[index], "definition_digest")
        published = publish_workspace_registry_generation(values, self.deps)
        self.assertEqual(values, published["workspaces"])
        self.assertEqual(values, list(load_workspace_registry(self.deps).workspaces))

    def test_second_generation_is_contiguous_and_binds_lineage(self):
        first = generation()
        write_generation(self.root, first)
        values = definitions()
        values[1]["workspace_version"] = 2
        values[1] = _bind(values[1], "definition_digest")
        second = generation(values, 2, first["registry_digest"])
        write_generation(self.root, second)
        self.assertEqual(2, second["sequence"])
        self.assertEqual(first["registry_digest"], second["predecessor_registry_digest"])
        self.assertEqual((1, 2), tuple(x["sequence"] for x in load_workspace_registry(self.deps).generations))

    def test_exact_publication_retry_returns_same_terminal_generation(self):
        first = publish_workspace_registry_generation(definitions(), self.deps)
        second = publish_workspace_registry_generation(deepcopy(definitions()), self.deps)
        self.assertEqual(first, second)
        self.assertEqual(1, len(load_workspace_registry(self.deps).generations))

    def test_private_dependency_clock_makes_publication_deterministic(self):
        fixed = "2026-08-14T19:20:21Z"
        with self.assertRaises(TypeError):
            WorkspaceRegistryDependencies(self.root, clock=lambda: fixed)
        deps = _WorkspaceRegistryProofDependencies(
            self.root, clock=lambda: fixed,
        )

        published = publish_workspace_registry_generation(definitions(), deps)

        self.assertEqual(fixed, published["generated_at"])
        self.assertEqual(
            published,
            publish_workspace_registry_generation(deepcopy(definitions()), deps),
        )

    def test_publication_materializes_an_absent_runtime_root_safely(self):
        runtime = self.root / "new-runtime"
        result = publish_workspace_registry_generation(
            definitions(), WorkspaceRegistryDependencies(runtime))
        self.assertEqual(1, result["sequence"])
        self.assertEqual(1, len(load_workspace_registry(WorkspaceRegistryDependencies(runtime)).generations))

    def test_two_competing_publishers_serialize_without_partial_state(self):
        barrier = threading.Barrier(2)
        results, failures = [], []
        def publish():
            try:
                barrier.wait()
                results.append(publish_workspace_registry_generation(definitions(), self.deps))
            except BaseException as exc:
                failures.append(exc)
        threads = [threading.Thread(target=publish) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(failures)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1])
        self.assertEqual(1, len(load_workspace_registry(self.deps).generations))


if __name__ == "__main__":
    unittest.main()
