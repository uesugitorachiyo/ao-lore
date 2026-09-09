"""Strict, dependency-free contracts for sanitized repository preparation."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any, Mapping

from ._strict_io import ContractError, require_bool, require_exact_keys, require_int, require_sha256


PUBLIC_RELEASE_SCHEMA_VERSIONS = (
    "ao.lore.public-release-policy.v0.1",
    "ao.lore.public-release-manifest.v0.1",
    "ao.lore.public-release-readiness.v0.1",
)
_POLICY_KEYS = {"schema_version", "included_files", "included_prefixes", "excluded_prefixes", "executable_prefixes", "binary_files", "exceptions", "limits", "authority", "policy_digest"}
_POLICY_AUTHORITIES = {"github_create", "github_push", "visibility_change", "hosted_ci", "release", "deployment"}
_READINESS_AUTHORITIES = {"github_created", "github_push", "network_used", "release", "deployment"}
_FORBIDDEN_PARTS = {".git", ".ao-lore", ".worktrees", ".."}


def canonical_digest(value: Mapping[str, Any], *, omit: str | None = None) -> str:
    detached = {key: item for key, item in value.items() if key != omit}
    body = json.dumps(detached, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _path(value: Any, label: str, *, prefix: bool = False, allow_forbidden: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 512:
        raise ContractError(f"{label} must be a bounded relative path")
    if "\\" in value or value.startswith("/") or (prefix and not value.endswith("/")):
        raise ContractError(f"{label} must be a normalized relative POSIX path")
    if any(unicodedata.category(char) == "Cc" or unicodedata.category(char) == "Cf" for char in value):
        raise ContractError(f"{label} contains a forbidden Unicode control character")
    path = PurePosixPath(value)
    if str(path) != value.rstrip("/") or (not allow_forbidden and any(part in _FORBIDDEN_PARTS for part in path.parts)):
        raise ContractError(f"{label} is unsafe")
    return value


def _ordered_unique_paths(values: Any, label: str, *, prefix: bool = False, allow_forbidden: bool = False) -> list[str]:
    if not isinstance(values, list):
        raise ContractError(f"{label} must be an array")
    checked = [_path(item, f"{label} item", prefix=prefix, allow_forbidden=allow_forbidden) for item in values]
    if checked != sorted(checked) or len({item.casefold() for item in checked}) != len(checked):
        raise ContractError(f"{label} must be sorted and case-insensitively unique")
    return checked


def _false_authority(value: Any, keys: set[str], label: str) -> dict[str, bool]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    require_exact_keys(value, keys, label)
    result = {key: require_bool(value[key], f"{label}.{key}") for key in sorted(keys)}
    if any(result.values()):
        raise ContractError(f"{label} cannot grant authority")
    return result


def validate_public_release_policy(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError("public release policy must be an object")
    require_exact_keys(value, _POLICY_KEYS, "public release policy")
    if value["schema_version"] != PUBLIC_RELEASE_SCHEMA_VERSIONS[0]:
        raise ContractError("unsupported public release policy version")
    included_files = _ordered_unique_paths(value["included_files"], "included_files")
    included_prefixes = _ordered_unique_paths(value["included_prefixes"], "included_prefixes", prefix=True)
    excluded_prefixes = _ordered_unique_paths(value["excluded_prefixes"], "excluded_prefixes", prefix=True, allow_forbidden=True)
    executable_prefixes = _ordered_unique_paths(value["executable_prefixes"], "executable_prefixes", prefix=True)
    binary_files = _ordered_unique_paths(value["binary_files"], "binary_files")
    all_paths = included_files + included_prefixes
    if len({item.casefold() for item in all_paths}) != len(all_paths):
        raise ContractError("included paths collide")
    if any(any(path == excluded.rstrip("/") or path.startswith(excluded) for excluded in excluded_prefixes) for path in included_files):
        raise ContractError("included file is excluded")
    limits = value["limits"]
    if not isinstance(limits, dict):
        raise ContractError("limits must be an object")
    require_exact_keys(limits, {"max_files", "max_file_bytes", "max_total_bytes", "max_depth"}, "limits")
    validated_limits = {
        "max_files": require_int(limits["max_files"], "limits.max_files", 1, 100000),
        "max_file_bytes": require_int(limits["max_file_bytes"], "limits.max_file_bytes", 1, 1073741824),
        "max_total_bytes": require_int(limits["max_total_bytes"], "limits.max_total_bytes", 1, 4294967296),
        "max_depth": require_int(limits["max_depth"], "limits.max_depth", 1, 64),
    }
    exceptions = value["exceptions"]
    if not isinstance(exceptions, list):
        raise ContractError("exceptions must be an array")
    checked_exceptions = []
    for index, exception in enumerate(exceptions):
        if not isinstance(exception, dict):
            raise ContractError("exception must be an object")
        require_exact_keys(exception, {"path", "sha256", "reason_code", "test_only"}, f"exceptions[{index}]")
        path = _path(exception["path"], f"exceptions[{index}].path")
        if not path.startswith("tests/") or exception["test_only"] is not True:
            raise ContractError("exceptions must be exact inert test-only files")
        reason = exception["reason_code"]
        if reason not in {"inert_private_path", "inert_secret_candidate", "inert_network_locator"}:
            raise ContractError("unsupported exception reason")
        checked_exceptions.append({"path": path, "sha256": require_sha256(exception["sha256"], "exception sha256"), "reason_code": reason, "test_only": True})
    exception_keys = [(item["path"], item["reason_code"]) for item in checked_exceptions]
    if exception_keys != sorted(exception_keys) or len(exception_keys) != len(set(exception_keys)):
        raise ContractError("exceptions must be sorted and unique")
    require_sha256(value["policy_digest"], "policy_digest")
    if canonical_digest(value, omit="policy_digest") != value["policy_digest"]:
        raise ContractError("public release policy digest mismatch")
    detached = deepcopy(value)
    detached.update(included_files=included_files, included_prefixes=included_prefixes, excluded_prefixes=excluded_prefixes, executable_prefixes=executable_prefixes, binary_files=binary_files, exceptions=checked_exceptions, limits=validated_limits, authority=_false_authority(value["authority"], _POLICY_AUTHORITIES, "authority"))
    return detached


def policy_allows_path(path: str, policy: Mapping[str, Any]) -> bool:
    if any(path == prefix.rstrip("/") or path.startswith(prefix) for prefix in policy["excluded_prefixes"]):
        return False
    return path in policy["included_files"] or any(path.startswith(prefix) for prefix in policy["included_prefixes"])


def validate_public_release_manifest(value: object, *, policy: dict[str, object]) -> dict[str, object]:
    policy = validate_public_release_policy(policy)
    if not isinstance(value, dict):
        raise ContractError("manifest must be an object")
    keys = {"schema_version", "source_head", "policy_digest", "entries", "file_count", "total_bytes", "manifest_digest"}
    require_exact_keys(value, keys, "public release manifest")
    if value["schema_version"] != PUBLIC_RELEASE_SCHEMA_VERSIONS[1]:
        raise ContractError("unsupported public release manifest version")
    if not isinstance(value["source_head"], str) or len(value["source_head"]) != 40 or any(c not in "0123456789abcdef" for c in value["source_head"]):
        raise ContractError("source_head must be a lowercase Git object id")
    if value["policy_digest"] != policy["policy_digest"]:
        raise ContractError("manifest policy digest mismatch")
    entries = value["entries"]
    if not isinstance(entries, list):
        raise ContractError("manifest entries must be an array")
    checked = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ContractError("manifest entry must be an object")
        require_exact_keys(entry, {"path", "mode", "size", "sha256"}, f"entries[{index}]")
        path = _path(entry["path"], f"entries[{index}].path")
        if not policy_allows_path(path, policy):
            raise ContractError("manifest path is not allowed by policy")
        mode = entry["mode"]
        executable_allowed = any(path.startswith(prefix) for prefix in policy["executable_prefixes"])
        if mode not in {"100644", "100755"} or (mode == "100755" and not executable_allowed):
            raise ContractError("manifest mode violates executable policy")
        size = require_int(entry["size"], "entry size", 0, policy["limits"]["max_file_bytes"])
        checked.append({"path": path, "mode": mode, "size": size, "sha256": require_sha256(entry["sha256"], "entry sha256")})
    paths = [item["path"] for item in checked]
    if paths != sorted(paths) or len({item.casefold() for item in paths}) != len(paths):
        raise ContractError("manifest entries must be sorted and collision-free")
    if len(checked) > policy["limits"]["max_files"] or sum(item["size"] for item in checked) > policy["limits"]["max_total_bytes"]:
        raise ContractError("manifest exceeds policy budget")
    if value["file_count"] != len(checked) or value["total_bytes"] != sum(item["size"] for item in checked):
        raise ContractError("manifest counts do not match entries")
    require_sha256(value["manifest_digest"], "manifest_digest")
    if canonical_digest(value, omit="manifest_digest") != value["manifest_digest"]:
        raise ContractError("manifest digest mismatch")
    return deepcopy(value)


def validate_public_release_readiness(value: object, *, manifest: dict[str, object]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ContractError("readiness must be an object")
    keys = {"schema_version", "status", "source_head", "clean_commit", "policy_digest", "manifest_digest", "file_count", "total_bytes", "gates", "authority"}
    require_exact_keys(value, keys, "public release readiness")
    if value["schema_version"] != PUBLIC_RELEASE_SCHEMA_VERSIONS[2] or value["status"] not in {"ready", "rejected"}:
        raise ContractError("unsupported readiness version or status")
    for field in ("source_head", "policy_digest", "manifest_digest", "file_count", "total_bytes"):
        if value[field] != manifest[field]:
            raise ContractError(f"readiness {field} mismatch")
    if not isinstance(value["clean_commit"], str) or len(value["clean_commit"]) != 40 or any(c not in "0123456789abcdef" for c in value["clean_commit"]):
        raise ContractError("clean_commit must be a lowercase Git object id")
    gates = value["gates"]
    if not isinstance(gates, dict):
        raise ContractError("gates must be an object")
    require_exact_keys(gates, {"contracts", "safety", "clean_checkout", "git_fsck"}, "gates")
    if any(result not in {"pass", "fail"} for result in gates.values()):
        raise ContractError("invalid gate result")
    _false_authority(value["authority"], _READINESS_AUTHORITIES, "authority")
    return deepcopy(value)
