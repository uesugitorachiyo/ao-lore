import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore._strict_io import ContractError
from ao_lore.public_release_contracts import canonical_digest
from ao_lore.public_release_export import PublicReleaseDependencies, prepare_sanitized_repository


ROOT = Path(__file__).resolve().parents[1]


class PublicReleaseExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.parent = self.base / "output"
        self.source.mkdir(); self.parent.mkdir()
        (self.source / "README.md").write_text("safe product\n")
        (self.source / "src/pkg").mkdir(parents=True)
        (self.source / "src/pkg/__init__.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.source, check=True)
        subprocess.run(["git", "add", "."], cwd=self.source, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "fixture"], cwd=self.source, check=True)
        self.policy = {
            "schema_version": "ao.lore.public-release-policy.v0.1",
            "included_files": ["README.md"], "included_prefixes": ["src/"],
            "excluded_prefixes": [".ao-lore/", ".git/", ".worktrees/", "docs/superpowers/"],
            "executable_prefixes": [], "binary_files": [], "exceptions": [],
            "limits": {"max_files": 20, "max_file_bytes": 10000, "max_total_bytes": 50000, "max_depth": 8},
            "authority": {name: False for name in ("github_create", "github_push", "visibility_change", "hosted_ci", "release", "deployment")},
        }
        self.policy["policy_digest"] = canonical_digest(self.policy)

    def tearDown(self): self.temp.cleanup()

    @property
    def destination(self): return self.parent / "ao-lore-sanitized-staging"

    def test_export_contains_only_policy_files_and_one_root_commit(self):
        report = prepare_sanitized_repository(PublicReleaseDependencies(self.source, self.parent), self.policy)
        self.assertEqual("ready", report["status"])
        roots = subprocess.run(["git", "rev-list", "--max-parents=0", "--all"], cwd=self.destination, text=True, capture_output=True, check=True).stdout.splitlines()
        count = subprocess.run(["git", "rev-list", "--all", "--count"], cwd=self.destination, text=True, capture_output=True, check=True).stdout.strip()
        self.assertEqual([report["commit"]], roots)
        self.assertEqual("1", count)
        self.assertEqual({"README.md", "src/pkg/__init__.py"}, {p.relative_to(self.destination).as_posix() for p in self.destination.rglob("*") if p.is_file() and ".git" not in p.parts})
        self.assertFalse((self.destination / ".git/objects/info/alternates").exists())
        self.assertNotIn(self.temp.name, json.dumps(report))

    def test_fixture_policy_without_hosted_workflow_remains_valid(self):
        """Generic fixture policies do not require the real-origin contract."""
        report = prepare_sanitized_repository(
            PublicReleaseDependencies(self.source, self.parent), self.policy
        )
        self.assertEqual("ready", report["status"])

    def test_export_commit_identity_is_neutral_and_parent_independent(self):
        first = prepare_sanitized_repository(
            PublicReleaseDependencies(self.source, self.parent), self.policy
        )
        other_parent = self.base / "other-output"
        other_parent.mkdir()
        second = prepare_sanitized_repository(
            PublicReleaseDependencies(self.source, other_parent), self.policy
        )
        self.assertEqual(first["commit"], second["commit"])
        metadata = subprocess.run(
            ["git", "show", "-s", "--format=%an%n%ae%n%cn%n%ce", first["commit"]],
            cwd=self.destination, text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(
            [
                "AO Lore", "sanitized-export@ao-lore.invalid",
                "AO Lore", "sanitized-export@ao-lore.invalid",
            ],
            metadata,
        )

    def test_export_commit_dates_match_source_head_and_remain_reproducible(self):
        env = os.environ.copy()
        env["GIT_AUTHOR_DATE"] = "@1735689600 +0000"
        env["GIT_COMMITTER_DATE"] = "@1735689600 +0000"
        subprocess.run(
            [
                "git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                "commit", "--amend", "--no-edit", "-q",
            ],
            cwd=self.source, env=env, check=True,
        )
        first = prepare_sanitized_repository(
            PublicReleaseDependencies(self.source, self.parent), self.policy
        )
        other_parent = self.base / "other-output"
        other_parent.mkdir()
        second = prepare_sanitized_repository(
            PublicReleaseDependencies(self.source, other_parent), self.policy
        )
        self.assertEqual(first["commit"], second["commit"])
        dates = subprocess.run(
            ["git", "show", "-s", "--format=%at%n%ct", first["commit"]],
            cwd=self.destination, text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        self.assertEqual(["1735689600", "1735689600"], dates)

    def test_export_ignores_source_replacement_refs(self):
        git = ["git", "--no-replace-objects"]
        original_head = subprocess.run(
            [*git, "rev-parse", "HEAD"], cwd=self.source, text=True,
            capture_output=True, check=True,
        ).stdout.strip()
        original_tree = subprocess.run(
            [*git, "rev-parse", f"{original_head}^{{tree}}"], cwd=self.source,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        original_epoch = subprocess.run(
            [*git, "show", "-s", "--format=%ct", original_head], cwd=self.source,
            text=True, capture_output=True, check=True,
        ).stdout.strip()
        replacement_env = os.environ.copy()
        replacement_env.update({
            "GIT_AUTHOR_NAME": "Replacement", "GIT_AUTHOR_EMAIL": "replacement@example.invalid",
            "GIT_COMMITTER_NAME": "Replacement", "GIT_COMMITTER_EMAIL": "replacement@example.invalid",
            "GIT_AUTHOR_DATE": "@1893456000 +0000", "GIT_COMMITTER_DATE": "@1893456000 +0000",
        })
        replacement = subprocess.run(
            [*git, "commit-tree", original_tree, "-p", original_head], cwd=self.source,
            input="replacement\n", text=True, env=replacement_env,
            capture_output=True, check=True,
        ).stdout.strip()
        subprocess.run([*git, "replace", original_head, replacement], cwd=self.source, check=True)
        first = prepare_sanitized_repository(
            PublicReleaseDependencies(self.source, self.parent), self.policy
        )
        self.assertEqual(original_head, first["source_head"])
        first_dates = subprocess.run(
            ["git", "show", "-s", "--format=%at%n%ct", first["commit"]],
            cwd=self.destination, text=True, capture_output=True, check=True,
        ).stdout.splitlines()
        self.assertEqual([original_epoch, original_epoch], first_dates)
        subprocess.run([*git, "replace", "-d", original_head], cwd=self.source, check=True)
        other_parent = self.base / "other-output"
        other_parent.mkdir()
        second = prepare_sanitized_repository(
            PublicReleaseDependencies(self.source, other_parent), self.policy
        )
        self.assertEqual(original_head, second["source_head"])
        self.assertEqual(first["commit"], second["commit"])

    def test_export_uses_committed_blobs_and_rejects_tracked_drift(self):
        (self.source / "README.md").write_text("uncommitted private material\n")
        with self.assertRaisesRegex(ContractError, "tracked source tree is dirty"):
            prepare_sanitized_repository(PublicReleaseDependencies(self.source, self.parent), self.policy)
        self.assertFalse(self.destination.exists())

    def test_destination_preexistence_and_gitlinks_fail_closed(self):
        self.destination.mkdir()
        with self.assertRaises(ContractError):
            prepare_sanitized_repository(PublicReleaseDependencies(self.source, self.parent), self.policy)
        self.destination.rmdir()
        subprocess.run(["git", "update-index", "--add", "--cacheinfo", "160000," + "a" * 40 + ",src/submodule"], cwd=self.source, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "gitlink"], cwd=self.source, check=True)
        (self.source / "src/submodule").mkdir()
        with self.assertRaisesRegex(ContractError, "gitlink"):
            prepare_sanitized_repository(PublicReleaseDependencies(self.source, self.parent), self.policy)

    def test_untracked_and_excluded_material_is_not_exported(self):
        (self.source / "private.txt").write_text("not read")
        (self.source / "docs/superpowers").mkdir(parents=True)
        (self.source / "docs/superpowers/plan.md").write_text("excluded")
        subprocess.run(["git", "add", "docs/superpowers/plan.md"], cwd=self.source, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "excluded"], cwd=self.source, check=True)
        report = prepare_sanitized_repository(PublicReleaseDependencies(self.source, self.parent), self.policy)
        self.assertEqual(2, report["file_count"])
        self.assertFalse((self.destination / "private.txt").exists())
        self.assertFalse((self.destination / "docs").exists())

    def test_directory_replacement_during_write_fails_closed(self):
        outside = self.base / "outside"
        outside.mkdir()
        real_mkdir = os.mkdir
        replaced = False

        def replace_directory(path, mode=0o777, *, dir_fd=None):
            nonlocal replaced
            real_mkdir(path, mode, dir_fd=dir_fd)
            if not replaced and (path == "pkg" or Path(path) == self.destination / "src/pkg"):
                replaced = True
                original = self.destination / "src/pkg"
                original.rename(self.base / "displaced")
                original.symlink_to(outside, target_is_directory=True)

        with patch("ao_lore.public_release_export.os.mkdir", side_effect=replace_directory):
            with self.assertRaises(ContractError):
                prepare_sanitized_repository(PublicReleaseDependencies(self.source, self.parent), self.policy)
        self.assertTrue(replaced)
        self.assertFalse((outside / "__init__.py").exists())

    def test_release_policy_exactly_excepts_sanitized_lifecycle_negative_fixtures(self):
        target_path = "tests/test_ao_lore_sanitized_lifecycle_contracts.py"
        target_digest = hashlib.sha256((ROOT / target_path).read_bytes()).hexdigest()
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        bindings = {
            (item["path"], item["sha256"], item["reason_code"], item["test_only"])
            for item in policy["exceptions"]
            if item["path"] == target_path
        }
        self.assertEqual(
            {
                (target_path, target_digest, "inert_network_locator", True),
                (target_path, target_digest, "inert_private_path", True),
            },
            bindings,
        )


if __name__ == "__main__": unittest.main()
