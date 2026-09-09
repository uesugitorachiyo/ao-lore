import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_acquisition import AcquisitionLimits, HTTPResponse
from ao_lore.workspace_contracts import validate_workspace_operation_readback
from ao_lore.workspace_registry import WorkspaceRegistryDependencies, WorkspaceRegistryError
from ao_lore.workspace_runtime import (
    inspect_workspace,
    recover_workspace,
    refresh_workspace,
    replay_workspace,
)
from tests import test_ao_lore_evidence_graph as graph_tests
from tests import test_ao_lore_evidence_freshness_query as freshness_tests
from tests.test_ao_lore_evidence_freshness_refresh import (
    EvidenceFreshnessRefreshTests,
    FakeHTTP,
)
from tests.test_ao_lore_workspace_registry import generation, workspace, write_generation


def _bind(value, field):
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


class _NoNetwork:
    def fetch(self, _locator, **_limits):
        raise AssertionError("network is outside this test")


class WorkspaceOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.graph = graph_tests.EvidenceGraphTests().build()
        helper = freshness_tests.EvidenceFreshnessQueryTests()
        helper.graph = self.graph
        self.policy, _observations, _comparisons, self.summary = helper.context({})
        primary = workspace("primary-fixture-a", "property", ("shared-reference-fixture",))
        primary.update({
            "root_workflow_id": self.graph["root_workflow_id"],
            "root_workflow_digest": self.graph["root_workflow_digest"],
            "source_registry_digest": self.graph["source_registry_digest"],
            "graph_id": self.graph["graph_id"],
            "graph_digest": self.graph["graph_digest"],
            "freshness_policy_id": self.policy["policy_id"],
            "freshness_policy_digest": self.policy["policy_digest"],
        })
        _bind(primary, "definition_digest")
        self.definitions = [
            primary,
            workspace("secondary-fixture-b", "property"),
            workspace("shared-reference-fixture"),
        ]
        self.generation = generation(self.definitions)
        write_generation(self.root, self.generation)
        self.state = self.root / "workspaces/state"
        for item in self.definitions:
            for child in ("sources", "graph", "freshness", "recovery"):
                (self.state / item["workspace_id"] / child).mkdir(parents=True, exist_ok=True)
        self.deps = WorkspaceRegistryDependencies(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def assert_bound_readback(self, result, operation):
        self.assertEqual(operation, result["operation"])
        self.assertEqual("primary-fixture-a", result["workspace_id"])
        self.assertEqual(["primary-fixture-a"], result["affected_workspace_ids"])
        self.assertEqual(
            result,
            validate_workspace_operation_readback(
                result,
                generation=self.generation,
                selected_workspace_ids={"primary-fixture-a"},
            ),
        )

    def test_inspect_opens_only_named_workspace_and_context_validates_graph(self):
        sibling_identities = {
            (os.stat(self.state / name).st_dev, os.stat(self.state / name).st_ino)
            for name in ("secondary-fixture-b", "shared-reference-fixture")
        }
        original_open = os.open

        def reject_sibling_open(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) in sibling_identities:
                os.close(descriptor)
                raise AssertionError("operation opened a sibling workspace")
            return descriptor

        with patch("ao_lore.workspace_runtime.os.open", reject_sibling_open):
            result = inspect_workspace(
                self.deps, "primary-fixture-a", self.graph,
                as_of_date="2026-08-13",
            )
        self.assert_bound_readback(result, "inspect")
        self.assertEqual("active", result["status"])

        changed = deepcopy(self.graph)
        changed["graph_id"] = "graph-foreign"
        _bind(changed, "graph_digest")
        with self.assertRaisesRegex(WorkspaceRegistryError, "binding"):
            inspect_workspace(
                self.deps, "primary-fixture-a", changed,
                as_of_date="2026-08-13",
            )

    def test_refresh_never_cascades_to_declared_reference(self):
        with patch(
            "ao_lore.evidence_freshness.refresh_official_evidence",
            return_value=self.summary,
        ) as refresh:
            result = refresh_workspace(
                self.deps, "primary-fixture-a", self.policy, self.graph, (),
                http=_NoNetwork(), clock=lambda: "2026-08-13T12:00:00Z",
                monotonic=lambda: 0.0, limits=AcquisitionLimits(),
            )
        self.assert_bound_readback(result, "refresh")
        refresh.assert_called_once()
        acquisition = refresh.call_args.args[3]
        freshness = refresh.call_args.kwargs["freshness_dependencies"]
        self.assertTrue(os.path.samefile(
            self.state / "primary-fixture-a/sources", acquisition.source_root,
        ))
        self.assertTrue(os.path.samefile(
            self.state / "primary-fixture-a/freshness", freshness.source_root,
        ))
        self.assertFalse(str(acquisition.source_root).endswith("shared-reference-fixture/sources"))

    def test_replay_publishes_exact_graph_only_under_named_workspace(self):
        report = {
            "status": "completed", "classification": "no_op",
            "graph_digest": self.graph["graph_digest"],
        }
        with patch("ao_lore.evidence_graph.publish_evidence_graph", return_value=report) as publish:
            result = replay_workspace(self.deps, "primary-fixture-a", self.graph)
        self.assert_bound_readback(result, "replay")
        publish.assert_called_once()
        self.assertEqual(self.graph, publish.call_args.args[0])
        self.assertTrue(os.path.samefile(
            self.state / "primary-fixture-a/graph", publish.call_args.args[1].root,
        ))

    def test_recovery_never_cascades_to_reference(self):
        graph_result = [{"classification": "complete", "graph_digest": self.graph["graph_digest"]}]
        with patch(
            "ao_lore.evidence_graph.recover_evidence_graph", return_value=graph_result,
        ) as graph_recover, patch(
            "ao_lore.evidence_freshness.recover_freshness_transactions", return_value=[],
        ) as freshness_recover:
            result = recover_workspace(self.deps, "primary-fixture-a")
        self.assert_bound_readback(result, "recover")
        self.assertTrue(os.path.samefile(
            self.state / "primary-fixture-a/graph", graph_recover.call_args.args[0].root,
        ))
        self.assertTrue(os.path.samefile(
            self.state / "primary-fixture-a/freshness",
            freshness_recover.call_args.args[0].source_root,
        ))
        for call in (graph_recover.call_args, freshness_recover.call_args):
            self.assertNotIn("shared-reference-fixture", str(call))
            self.assertNotIn("secondary-fixture-b", str(call))

    def test_real_workspace_refresh_and_recovery_share_freshness_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "workspaces/state"
            source_root = state / "primary-fixture-a/sources"
            source_root.mkdir(parents=True)
            helper = EvidenceFreshnessRefreshTests()
            helper.setUp()
            try:
                helper.root = source_root
                specs, _records, graph_value, policy_value = helper.prepare()
                primary = workspace(
                    "primary-fixture-a", "property",
                    ("shared-reference-fixture",),
                )
                primary.update({
                    "root_workflow_id": graph_value["root_workflow_id"],
                    "root_workflow_digest": graph_value["root_workflow_digest"],
                    "source_registry_digest": graph_value["source_registry_digest"],
                    "graph_id": graph_value["graph_id"],
                    "graph_digest": graph_value["graph_digest"],
                    "freshness_policy_id": policy_value["policy_id"],
                    "freshness_policy_digest": policy_value["policy_digest"],
                })
                _bind(primary, "definition_digest")
                definitions = [
                    primary,
                    workspace("secondary-fixture-b", "property"),
                    workspace("shared-reference-fixture"),
                ]
                write_generation(root, generation(definitions))
                for item in definitions:
                    for child in ("sources", "graph", "freshness", "recovery"):
                        (state / item["workspace_id"] / child).mkdir(
                            parents=True, exist_ok=True,
                        )
                dependencies = WorkspaceRegistryDependencies(root)
                result = refresh_workspace(
                    dependencies, "primary-fixture-a", policy_value,
                    graph_value, specs,
                    http=FakeHTTP([HTTPResponse(
                        200, (("Content-Type", "text/html"),), b"baseline",
                    )]),
                    clock=lambda: helper.now, monotonic=lambda: 1.0,
                    limits=helper.limits,
                )
                self.assertEqual("complete", result["status"])
                freshness_root = state / "primary-fixture-a/freshness"
                self.assertFalse((source_root / "freshness").exists())
                self.assertEqual(1, len(list((freshness_root / "summaries").iterdir())))
                recovered = recover_workspace(dependencies, "primary-fixture-a")
                self.assertEqual("complete", recovered["status"])
                self.assertEqual(["primary-fixture-a"], recovered["affected_workspace_ids"])
            finally:
                helper.tearDown()


if __name__ == "__main__":
    unittest.main()
