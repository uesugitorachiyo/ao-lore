"""Build a deterministic, disconnected one-commit repository from Git blobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._strict_io import ContractError
from .public_release_contracts import canonical_digest, policy_allows_path, validate_public_release_manifest, validate_public_release_policy
from .public_release_safety import scan_public_tree


@dataclass(frozen=True)
class PublicReleaseDependencies:
    source_root: Path
    staging_parent: Path
    git_binary: Path = Path("/usr/bin/git")


_TREE_LINE = re.compile(rb"^(100644|100755|120000|160000) (blob|commit) ([0-9a-f]{40,64}) +(?:-|([0-9]+))\t(.+)$", re.DOTALL)
_COMMIT_EPOCH = re.compile(rb"^(0|[1-9][0-9]{0,9})\n$")
_DESTINATION_NAME = "ao-lore-sanitized-staging"
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _environment() -> dict[str, str]:
    result = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1", "LC_ALL": "C", "TZ": "UTC"})
    return result


def _git(dependencies: PublicReleaseDependencies, *arguments: str, cwd: Path | None = None, input_bytes: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            [str(dependencies.git_binary), *arguments], cwd=cwd or dependencies.source_root,
            env=_environment(), input=input_bytes, capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContractError("local Git operation failed") from exc
    if result.returncode != 0:
        raise ContractError("local Git operation failed")
    return result


def _validate_roots(dependencies: PublicReleaseDependencies) -> Path:
    source = dependencies.source_root
    parent = dependencies.staging_parent
    for path, label in ((source, "source"), (parent, "staging parent"), (dependencies.git_binary, "Git binary")):
        try: info = os.lstat(path)
        except OSError as exc: raise ContractError(f"{label} is unavailable") from exc
        if stat.S_ISLNK(info.st_mode): raise ContractError(f"{label} cannot be a symlink")
        if label == "Git binary":
            if not stat.S_ISREG(info.st_mode): raise ContractError("Git binary must be regular")
        elif not stat.S_ISDIR(info.st_mode): raise ContractError(f"{label} must be a directory")
    if source.resolve() != source or parent.resolve() != parent:
        raise ContractError("release roots must be absolute canonical paths")
    destination = parent / _DESTINATION_NAME
    if destination.exists() or destination.is_symlink():
        raise ContractError("sanitized staging destination already exists")
    return destination


def _inventory(dependencies: PublicReleaseDependencies, policy: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, bytes]]:
    status = _git(dependencies, "status", "--porcelain=v1", "--untracked-files=no").stdout
    if status:
        raise ContractError("tracked source tree is dirty")
    head = _git(dependencies, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode("ascii").strip()
    records = _git(dependencies, "ls-tree", "-rz", "-l", "--full-tree", head).stdout.split(b"\0")
    entries: list[dict[str, Any]] = []
    bodies: dict[str, bytes] = {}
    for raw in filter(None, records):
        match = _TREE_LINE.fullmatch(raw)
        if match is None:
            raise ContractError("tracked tree entry is malformed")
        mode, kind, object_id, size_raw, path_raw = match.groups()
        if mode == b"160000" or kind == b"commit":
            raise ContractError("gitlink is not allowed")
        if mode == b"120000":
            raise ContractError("tracked link is not allowed")
        try: path = path_raw.decode("utf-8")
        except UnicodeError as exc: raise ContractError("tracked path is not UTF-8") from exc
        if not policy_allows_path(path, policy):
            continue
        body = _git(dependencies, "cat-file", "blob", object_id.decode("ascii")).stdout
        size = int(size_raw)
        if len(body) != size:
            raise ContractError("tracked blob size drift")
        digest = hashlib.sha256(body).hexdigest()
        entries.append({"path": path, "mode": mode.decode("ascii"), "size": size, "sha256": digest})
        bodies[path] = body
    entries.sort(key=lambda item: item["path"])
    return head, entries, bodies


def _source_commit_date(dependencies: PublicReleaseDependencies, head: str) -> str:
    raw = _git(dependencies, "show", "-s", "--format=%ct", head).stdout
    if _COMMIT_EPOCH.fullmatch(raw) is None:
        raise ContractError("source commit timestamp is invalid")
    return f"@{raw[:-1].decode('ascii')} +0000"


def _open_child_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OSError("destination component is not a directory")
    return descriptor


def _write_export_tree(parent: Path, entries: list[dict[str, Any]], bodies: dict[str, bytes]) -> tuple[int, tuple[int, int]]:
    parent_fd = os.open(parent, _DIRECTORY_FLAGS)
    destination_fd: int | None = None
    try:
        os.mkdir(_DESTINATION_NAME, 0o700, dir_fd=parent_fd)
        destination_fd = os.open(_DESTINATION_NAME, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        identity = os.fstat(destination_fd)
        for entry in entries:
            parts = entry["path"].split("/")
            current = os.dup(destination_fd)
            try:
                for part in parts[:-1]:
                    child = _open_child_directory(current, part)
                    os.close(current)
                    current = child
                descriptor = os.open(
                    parts[-1],
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    int(entry["mode"][-3:], 8),
                    dir_fd=current,
                )
                try:
                    view = memoryview(bodies[entry["path"]])
                    while view:
                        view = view[os.write(descriptor, view):]
                finally:
                    os.close(descriptor)
            finally:
                os.close(current)
        visible = os.stat(_DESTINATION_NAME, dir_fd=parent_fd, follow_symlinks=False)
        if (visible.st_dev, visible.st_ino) != (identity.st_dev, identity.st_ino):
            raise OSError("destination root changed")
        return destination_fd, (identity.st_dev, identity.st_ino)
    except BaseException:
        if destination_fd is not None:
            os.close(destination_fd)
        raise
    finally:
        os.close(parent_fd)


def prepare_sanitized_repository(dependencies: PublicReleaseDependencies, policy: dict[str, object]) -> dict[str, object]:
    """Create one detached clean repository; never configure or call a remote."""
    validated_policy = validate_public_release_policy(policy)
    destination = _validate_roots(dependencies)
    head, entries, bodies = _inventory(dependencies, validated_policy)
    commit_date = _source_commit_date(dependencies, head)
    manifest: dict[str, Any] = {
        "schema_version": "ao.lore.public-release-manifest.v0.1", "source_head": head,
        "policy_digest": validated_policy["policy_digest"], "entries": entries,
        "file_count": len(entries), "total_bytes": sum(item["size"] for item in entries),
    }
    manifest["manifest_digest"] = canonical_digest(manifest)
    validate_public_release_manifest(manifest, policy=validated_policy)
    complete = False
    destination_fd: int | None = None
    try:
        try:
            destination_fd, _ = _write_export_tree(dependencies.staging_parent, entries, bodies)
        except OSError as exc:
            raise ContractError("sanitized tree write rejected") from exc
        os.close(destination_fd)
        destination_fd = None
        safety = scan_public_tree(destination, validated_policy)
        if safety["status"] != "accepted" or safety["manifest_digest"] != hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
            raise ContractError("sanitized tree safety scan rejected")
        empty_template = destination / ".git-template-empty"
        empty_template.mkdir()
        _git(dependencies, "init", "-q", "-b", "main", "--template", str(empty_template), cwd=destination)
        empty_template.rmdir()
        _git(dependencies, "-c", "core.hooksPath=/dev/null", "add", "--all", cwd=destination)
        commit_env = _environment()
        commit_env.update({
            "GIT_AUTHOR_NAME": "AO Lore", "GIT_AUTHOR_EMAIL": "sanitized-export@ao-lore.invalid",
            "GIT_COMMITTER_NAME": "AO Lore", "GIT_COMMITTER_EMAIL": "sanitized-export@ao-lore.invalid",
            "GIT_AUTHOR_DATE": commit_date, "GIT_COMMITTER_DATE": commit_date,
        })
        result = subprocess.run([str(dependencies.git_binary), "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "Initial sanitized AO Lore repository"], cwd=destination, env=commit_env, capture_output=True, timeout=120)
        if result.returncode != 0: raise ContractError("local Git commit failed")
        _git(dependencies, "fsck", "--full", "--no-dangling", cwd=destination)
        commit = _git(dependencies, "rev-parse", "HEAD", cwd=destination).stdout.decode().strip()
        if _git(dependencies, "rev-list", "--all", "--count", cwd=destination).stdout.strip() != b"1":
            raise ContractError("sanitized repository has unexpected history")
        if _git(dependencies, "remote", cwd=destination).stdout or _git(dependencies, "tag", cwd=destination).stdout:
            raise ContractError("sanitized repository has unexpected refs")
        complete = True
        return {"status": "ready", "source_head": head, "commit": commit, "manifest_digest": manifest["manifest_digest"], "file_count": manifest["file_count"], "total_bytes": manifest["total_bytes"], "reachable_commit_count": 1, "network_used": False, "github_created": False, "github_push": False}
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if not complete and destination.exists():
            shutil.rmtree(destination)
