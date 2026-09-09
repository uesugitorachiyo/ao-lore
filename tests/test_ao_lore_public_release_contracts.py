import hashlib
import json
import unittest
from copy import deepcopy

from ao_lore._strict_io import ContractError, parse_strict_json
from ao_lore.public_release_contracts import (
    validate_public_release_manifest,
    validate_public_release_policy,
    validate_public_release_readiness,
)


def digest(value):
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


class PublicReleaseContractTests(unittest.TestCase):
    def policy(self):
        value = {
            "schema_version": "ao.lore.public-release-policy.v0.1",
            "included_files": [".gitignore", "LICENSE", "Makefile", "README.md"],
            "included_prefixes": ["docs/architecture/", "schemas/", "scripts/", "src/", "tests/"],
            "excluded_prefixes": [".ao-lore/", ".git/", ".worktrees/", "docs/superpowers/"],
            "executable_prefixes": ["scripts/"],
            "binary_files": [],
            "exceptions": [],
            "limits": {"max_files": 4096, "max_file_bytes": 16777216, "max_total_bytes": 268435456, "max_depth": 12},
            "authority": {name: False for name in ("github_create", "github_push", "visibility_change", "hosted_ci", "release", "deployment")},
        }
        value["policy_digest"] = digest(value)
        return value

    def manifest(self):
        policy = self.policy()
        value = {
            "schema_version": "ao.lore.public-release-manifest.v0.1",
            "source_head": "a" * 40,
            "policy_digest": policy["policy_digest"],
            "entries": [
                {"path": "README.md", "mode": "100644", "size": 3, "sha256": "b" * 64},
                {"path": "src/ao_lore/__init__.py", "mode": "100644", "size": 0, "sha256": "c" * 64},
            ],
        }
        value["file_count"] = len(value["entries"])
        value["total_bytes"] = sum(item["size"] for item in value["entries"])
        value["manifest_digest"] = digest(value)
        return policy, value

    def test_policy_is_allowlist_only_and_denies_authority(self):
        validated = validate_public_release_policy(self.policy())
        self.assertIn("src/", validated["included_prefixes"])
        self.assertIn("docs/superpowers/", validated["excluded_prefixes"])
        self.assertTrue(all(value is False for value in validated["authority"].values()))

    def test_policy_rejects_duplicate_keys_unknown_fields_and_digest_drift(self):
        with self.assertRaises(ContractError):
            parse_strict_json(b'{"a":1,"a":2}', "policy")
        for mutation in (lambda p: p.update(extra=True), lambda p: p.update(policy_digest="0" * 64)):
            policy = self.policy()
            mutation(policy)
            with self.assertRaises(ContractError):
                validate_public_release_policy(policy)

    def test_manifest_rejects_unsafe_paths_unordered_entries_and_budget_drift(self):
        policy, manifest = self.manifest()
        validate_public_release_manifest(manifest, policy=policy)
        mutations = []
        reversed_entries = deepcopy(manifest)
        reversed_entries["entries"].reverse()
        mutations.append(reversed_entries)
        runtime = deepcopy(manifest)
        runtime["entries"][0]["path"] = ".ao-lore/private.json"
        mutations.append(runtime)
        absolute = deepcopy(manifest)
        absolute["entries"][0]["path"] = "/home/user/private"
        mutations.append(absolute)
        wrong_count = deepcopy(manifest)
        wrong_count["file_count"] = 9
        mutations.append(wrong_count)
        for value in mutations:
            with self.assertRaises(ContractError):
                validate_public_release_manifest(value, policy=policy)

    def test_policy_rejects_collisions_controls_and_nonfalse_authority(self):
        for mutate in (
            lambda p: p["included_files"].extend(["readme.md"]),
            lambda p: p["included_files"].append("bad\u0001name"),
            lambda p: p["included_files"].append(".git/config"),
            lambda p: p["authority"].update(github_push=True),
        ):
            policy = self.policy()
            mutate(policy)
            policy["policy_digest"] = digest({k: v for k, v in policy.items() if k != "policy_digest"})
            with self.assertRaises(ContractError):
                validate_public_release_policy(policy)

    def test_exception_is_exact_digest_bound_and_test_only(self):
        policy = self.policy()
        policy["exceptions"] = [{"path": "tests/fixtures/inert.txt", "sha256": "d" * 64, "reason_code": "inert_private_path", "test_only": True}]
        policy["policy_digest"] = digest({k: v for k, v in policy.items() if k != "policy_digest"})
        validate_public_release_policy(policy)
        policy["exceptions"][0]["test_only"] = False
        policy["policy_digest"] = digest({k: v for k, v in policy.items() if k != "policy_digest"})
        with self.assertRaises(ContractError):
            validate_public_release_policy(policy)

    def test_readiness_binds_manifest_counts_digests_and_false_authority(self):
        policy, manifest = self.manifest()
        manifest = validate_public_release_manifest(manifest, policy=policy)
        readiness = {
            "schema_version": "ao.lore.public-release-readiness.v0.1",
            "status": "ready",
            "source_head": manifest["source_head"],
            "clean_commit": "d" * 40,
            "policy_digest": manifest["policy_digest"],
            "manifest_digest": manifest["manifest_digest"],
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
            "gates": {"contracts": "pass", "safety": "pass", "clean_checkout": "pass", "git_fsck": "pass"},
            "authority": {name: False for name in ("github_created", "github_push", "network_used", "release", "deployment")},
        }
        validate_public_release_readiness(readiness, manifest=manifest)
        for field in ("manifest_digest", "file_count"):
            invalid = deepcopy(readiness)
            invalid[field] = "0" * 64 if field.endswith("digest") else 99
            with self.assertRaises(ContractError):
                validate_public_release_readiness(invalid, manifest=manifest)


if __name__ == "__main__":
    unittest.main()
