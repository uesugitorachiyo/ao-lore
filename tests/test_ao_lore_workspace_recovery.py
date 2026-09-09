import os
import tempfile
import unittest
from pathlib import Path

from ao_lore.workspace_registry import (
    WorkspaceRegistryError,
    WorkspaceRegistryDependencies,
    load_workspace_registry,
    publish_workspace_registry_generation,
    recover_workspace_registry,
)
from tests.test_ao_lore_workspace_registry import definitions


class InjectedCrash(RuntimeError):
    pass


class CrashAt:
    def __init__(self, name, occurrence=1):
        self.name, self.occurrence, self.seen = name, occurrence, 0

    def __call__(self, name):
        if name == self.name:
            self.seen += 1
            if self.seen == self.occurrence:
                raise InjectedCrash(name)


class WorkspaceRegistryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def deps(self, failpoint=lambda _name: None):
        return WorkspaceRegistryDependencies(self.root, failpoint)

    def test_every_durable_failpoint_recovers_exact_generation(self):
        cases = [
            ("after_intent_fsync", 1),
            ("after_staged_file_fsync", 1),
            ("after_staged_file_fsync", 2),
            ("after_staged_file_fsync", 3),
            ("after_staged_file_fsync", 4),
            ("after_stage_directory_fsync", 1),
            ("after_generation_rename", 1),
            ("after_generations_fsync", 1),
            ("after_published_reopen", 1),
            ("after_completed_evidence", 1),
            ("after_staging_cleanup", 1),
            ("after_intent_cleanup", 1),
        ]
        for name, occurrence in cases:
            with self.subTest(name=name, occurrence=occurrence):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    with self.assertRaises(InjectedCrash):
                        publish_workspace_registry_generation(
                            definitions(), WorkspaceRegistryDependencies(root, CrashAt(name, occurrence)))
                    outcomes = recover_workspace_registry(WorkspaceRegistryDependencies(root))
                    self.assertTrue(outcomes)
                    self.assertEqual("complete", outcomes[0]["result"])
                    snapshot = load_workspace_registry(WorkspaceRegistryDependencies(root))
                    self.assertEqual(1, len(snapshot.generations))
                    self.assertEqual(definitions(), list(snapshot.workspaces))

    def test_foreign_staging_is_preserved_and_investigated(self):
        stage = self.root / "workspaces/registry/staging/foreign-run"
        stage.mkdir(parents=True)
        foreign = stage / "foreign.json"
        foreign.write_bytes(b"{}")
        result = recover_workspace_registry(self.deps())
        self.assertEqual("investigate", result[0]["result"])
        self.assertTrue(foreign.exists())

    def test_foreign_recovery_and_unsafe_staging_are_preserved(self):
        recovery = self.root / "workspaces/registry/recovery"
        staging = self.root / "workspaces/registry/staging"
        recovery.mkdir(parents=True)
        staging.mkdir(parents=True)
        (recovery / "unknown.intent.json").write_bytes(b"{}")
        os.mkfifo(staging / "foreign-fifo")
        result = recover_workspace_registry(self.deps())
        self.assertEqual("investigate", result[0]["result"])
        self.assertTrue((recovery / "unknown.intent.json").exists())
        self.assertTrue((staging / "foreign-fifo").exists())

    def test_exact_retry_after_crash_converges_without_new_sequence(self):
        with self.assertRaises(InjectedCrash):
            publish_workspace_registry_generation(definitions(), self.deps(CrashAt("after_generation_rename")))
        result = publish_workspace_registry_generation(definitions(), self.deps())
        self.assertEqual(1, result["sequence"])
        self.assertEqual(1, len(load_workspace_registry(self.deps()).generations))

    def test_ordinary_load_rejects_pending_exact_intent_until_recovery(self):
        with self.assertRaises(InjectedCrash):
            publish_workspace_registry_generation(
                definitions(), self.deps(CrashAt("after_intent_fsync")))
        with self.assertRaises(WorkspaceRegistryError):
            load_workspace_registry(self.deps())
        self.assertEqual("complete", recover_workspace_registry(self.deps())[0]["result"])
        self.assertEqual(1, len(load_workspace_registry(self.deps()).generations))

    def test_ordinary_load_rejects_and_preserves_foreign_staging(self):
        publish_workspace_registry_generation(definitions(), self.deps())
        foreign = self.root / "workspaces/registry/staging/foreign.json"
        foreign.write_bytes(b"{}")
        before = foreign.read_bytes()
        with self.assertRaises(WorkspaceRegistryError):
            load_workspace_registry(self.deps())
        self.assertEqual(before, foreign.read_bytes())

    def test_valid_terminal_completed_audit_history_does_not_block_load(self):
        published = publish_workspace_registry_generation(definitions(), self.deps())
        completed = list((self.root / "workspaces/registry/recovery").glob("*.completed.json"))
        self.assertEqual(1, len(completed))
        self.assertEqual(published, load_workspace_registry(self.deps()).generations[-1])


if __name__ == "__main__":
    unittest.main()
