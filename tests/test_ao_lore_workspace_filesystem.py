import os
import stat
import tempfile
import unittest
from pathlib import Path

from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    WorkspaceRegistryError,
    load_workspace_registry,
    publish_workspace_registry_generation,
)
from tests.test_ao_lore_workspace_registry import definitions, generation, write_generation


class WorkspaceRegistryFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.deps = WorkspaceRegistryDependencies(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def generations(self):
        return self.root / "workspaces" / "registry" / "generations"

    def inventory(self):
        result = []
        for parent, directories, files in os.walk(self.root, followlinks=False):
            relative_parent = Path(parent).relative_to(self.root)
            for name in sorted((*directories, *files)):
                path = Path(parent) / name
                info = os.lstat(path)
                kind = "directory" if stat.S_ISDIR(info.st_mode) else "symlink" if stat.S_ISLNK(info.st_mode) else "file"
                result.append((str(relative_parent / name), kind, info.st_size))
        return result

    def test_generation_exact_allowlists_reject_extra_alias_and_future_version(self):
        published = generation()
        write_generation(self.root, published)
        generation_root = next(self.generations().iterdir())
        (generation_root / "extra.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(WorkspaceRegistryError):
            load_workspace_registry(self.deps)
        (generation_root / "extra.json").unlink()
        manifest = generation_root / "manifest.json"
        body = manifest.read_text(encoding="utf-8").replace(
            published["schema_version"], "ao.lore.workspace-registry-generation.v9.9")
        manifest.write_text(body, encoding="utf-8")
        with self.assertRaises(WorkspaceRegistryError):
            load_workspace_registry(self.deps)

    def test_symlink_hardlink_and_fifo_manifest_are_rejected(self):
        for attack in ("symlink", "hardlink", "fifo"):
            with self.subTest(attack=attack):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    deps = WorkspaceRegistryDependencies(root)
                    write_generation(root, generation())
                    manifest = next((root / "workspaces/registry/generations").iterdir()) / "manifest.json"
                    saved = manifest.with_name("saved")
                    manifest.rename(saved)
                    if attack == "symlink": os.symlink(saved.name, manifest)
                    elif attack == "hardlink": os.link(saved, manifest)
                    else: os.mkfifo(manifest)
                    with self.assertRaises(WorkspaceRegistryError):
                        load_workspace_registry(deps)

    def test_sequence_alias_gap_and_casefold_alias_fail_closed(self):
        write_generation(self.root, generation())
        original = next(self.generations().iterdir())
        original.rename(self.generations() / original.name.replace("0000000001", "0000000002"))
        with self.assertRaises(WorkspaceRegistryError):
            load_workspace_registry(self.deps)
        original = next(self.generations().iterdir())
        original.rename(self.generations() / original.name.upper())
        with self.assertRaises(WorkspaceRegistryError):
            load_workspace_registry(self.deps)

    def test_entry_and_byte_budgets_fail_closed(self):
        write_generation(self.root, generation())
        manifest = next(self.generations().iterdir()) / "manifest.json"
        with manifest.open("ab") as stream:
            stream.write(b" " * (1024 * 1024 + 1))
        with self.assertRaises(WorkspaceRegistryError):
            load_workspace_registry(self.deps)

    def test_fixed_lock_recovery_and_staging_entry_types_are_validated(self):
        for name, attack in (("registry.lock", "fifo"), ("recovery", "symlink"), ("staging", "file")):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    write_generation(root, generation())
                    registry = root / "workspaces/registry"
                    target = registry / name
                    if attack == "fifo": os.mkfifo(target)
                    elif attack == "file": target.write_bytes(b"")
                    else: os.symlink("generations", target)
                    with self.assertRaises(WorkspaceRegistryError):
                        load_workspace_registry(WorkspaceRegistryDependencies(root))

    def test_generation_directory_replacement_during_read_is_rejected(self):
        write_generation(self.root, generation())
        original = self.generations()
        moved = original.with_name("generations-old")
        def replace(name):
            if name == "before_registry_revalidation":
                original.rename(moved)
                original.mkdir()
        with self.assertRaises(WorkspaceRegistryError):
            load_workspace_registry(WorkspaceRegistryDependencies(self.root, replace))
        self.assertTrue(moved.exists())

    def test_publisher_rejects_every_preexisting_partial_workspace_tree_without_mutation(self):
        layouts = (
            ("state-without-registry", ("workspaces/state",)),
            ("empty-workspaces", ("workspaces",)),
            ("empty-registry", ("workspaces/registry",)),
            ("generations-only", ("workspaces/registry/generations",)),
            ("missing-recovery", ("workspaces/registry/generations", "workspaces/registry/staging", "workspaces/registry/registry.lock")),
        )
        for label, entries in layouts:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                for entry in entries:
                    path = root / entry
                    if path.name == "registry.lock":
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"")
                    else:
                        path.mkdir(parents=True, exist_ok=True)
                before = self._inventory_at(root)
                with self.assertRaises(WorkspaceRegistryError):
                    publish_workspace_registry_generation(definitions(), WorkspaceRegistryDependencies(root))
                self.assertEqual(before, self._inventory_at(root))

    def _inventory_at(self, root):
        original = self.root
        try:
            self.root = root
            return self.inventory()
        finally:
            self.root = original


if __name__ == "__main__":
    unittest.main()
