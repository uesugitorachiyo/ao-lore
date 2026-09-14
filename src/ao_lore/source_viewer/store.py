"""Fixed-root retained snapshot bridge; no browser-controlled path reaches os.open."""
from __future__ import annotations

import copy
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .contracts import (
    LEVELS, MAX_MANIFEST, MAX_SOURCE, ViewerError, canonical_json, digest,
    evidence_key, format_time, reject, require, strict_json, timestamp,
    valid_digest, valid_id, validate_grant, validate_manifest,
)

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceStore(Protocol):
    """Native adapter seam. Implementations must authorize and verify before returning bytes.

    SnapshotStore is runnable now. No native AO Lore registry/IR adapter is claimed.
    """
    grant: dict[str, Any]
    manifest: dict[str, Any]

    def authorize(self) -> None: ...
    def list_records(self) -> list[dict[str, Any]]: ...
    def get_record(self, key: tuple[str, str, str]) -> dict[str, Any]: ...
    def source_bytes(self, record: dict[str, Any]) -> bytes: ...


class SafeRoot:
    """POSIX no-follow descriptor walk, file ownership/mode/type checks and bounded reads."""
    def __init__(self, root: Path):
        require(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "platform_unsupported")
        self.path = Path(os.path.abspath(root))
        self.identity: tuple[int, int] | None = None
        fd = self._open_root()
        os.close(fd)

    @staticmethod
    def _safe_owned(fd: int, *, directory: bool) -> None:
        info = os.fstat(fd)
        require((stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
                "retained_state_unavailable")
        require(info.st_uid == os.geteuid() and info.st_mode & 0o022 == 0,
                "retained_state_unavailable")
        if not directory:
            require(info.st_nlink == 1, "retained_state_unavailable")

    def _open_root(self) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open("/", flags)
        try:
            for part in self.path.parts[1:]:
                new_fd = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = new_fd
            self._safe_owned(fd, directory=True)
            info = os.fstat(fd)
            identity = info.st_dev, info.st_ino
            require(self.identity in (None, identity), "binding_drift")
            self.identity = identity
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, parts: tuple[str, ...], limit: int) -> bytes:
        require(bool(parts) and all(isinstance(x, str) and bool(re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,254}", x)) and x not in {".", ".."} for x in parts))
        root_fd: int | None = None
        file_fd: int | None = None
        try:
            root_fd = self._open_root()
            for part in parts[:-1]:
                new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                 os.O_CLOEXEC, dir_fd=root_fd)
                try:
                    self._safe_owned(new_fd, directory=True)
                except BaseException:
                    os.close(new_fd)
                    raise
                os.close(root_fd)
                root_fd = new_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK |
                              os.O_CLOEXEC, dir_fd=root_fd)
            self._safe_owned(file_fd, directory=False)
            before = os.fstat(file_fd)
            require(before.st_size <= limit, "resource_limit")
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(file_fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            after = os.fstat(file_fd)
            require(len(data) <= limit, "resource_limit")
            require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) ==
                    (after.st_size, after.st_mtime_ns, after.st_ctime_ns), "binding_drift")
            return data
        except (OSError, ValueError):
            reject("retained_state_unavailable")
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if root_fd is not None:
                os.close(root_fd)


class SnapshotStore:
    """An expiring, immutable operator-approved bridge, not live registry authorization."""
    provenance_mode = "operator-approved-snapshot"
    native_binding_revalidated = False

    def __init__(self, home: Path, grant_id: str, *, clock: Clock = utcnow):
        require(valid_id(grant_id))
        try:
            self.root = SafeRoot(home)
        except (OSError, ValueError):
            reject("retained_state_unavailable")
        self.clock = clock
        self.grant_id = grant_id
        grant_bytes = self.root.read(("source-viewer", "approvals", grant_id + ".json"), 8192)
        self.grant = strict_json(grant_bytes, 8192)
        validate_grant(self.grant)
        require(self.grant["grant_id"] == grant_id)
        self._grant_digest = digest(grant_bytes)
        self._binding_hex = self.grant["binding_digest"].split(":", 1)[1]
        self.manifest = self._load_manifest()
        require(self.manifest["primary_workspace_id"] == self.grant["primary_workspace_id"],
                "workspace_denied")
        maximum = LEVELS[self.grant["max_sensitivity"]]
        for record in self.manifest["records"]:
            require(LEVELS[record["original_sensitivity"]] <= maximum, "sensitivity_denied")
        self.records = {evidence_key(r["evidence"]): r for r in self.manifest["records"]}
        self.authorize()

    def _load_manifest(self) -> dict[str, Any]:
        data = self.root.read(("source-viewer", "bindings", self._binding_hex + ".json"),
                              MAX_MANIFEST)
        require(digest(data) == self.grant["binding_digest"], "binding_drift")
        value = strict_json(data)
        validate_manifest(value)
        return value

    def authorize(self) -> None:
        raw = self.root.read(("source-viewer", "approvals", self.grant_id + ".json"), 8192)
        require(digest(raw) == self._grant_digest, "approval_revoked")
        now = self.clock()
        require(timestamp(self.grant["created_at"]) <= now < timestamp(self.grant["expires_at"]),
                "approval_expired")
        self._load_manifest()

    def list_records(self) -> list[dict[str, Any]]:
        self.authorize()
        return copy.deepcopy(list(self.records.values()))

    def get_record(self, key: tuple[str, str, str]) -> dict[str, Any]:
        self.authorize()
        record = self.records.get(key)
        if record is None:
            reject("evidence_unavailable", 404)
        return copy.deepcopy(record)

    def source_bytes(self, record: dict[str, Any]) -> bytes:
        retained = self.get_record(evidence_key(record["evidence"]))
        require(retained == record, "binding_drift")
        source_digest = record["evidence"]["source_digest"]
        require(valid_digest(source_digest))
        data = self.root.read(("source-viewer", "objects", source_digest[7:]), MAX_SOURCE)
        require(digest(data) == source_digest, "source_integrity_failed")
        return data


def provision_snapshot(
    home: Path, manifest: dict[str, Any], originals: dict[str, bytes], *, grant_id: str,
    max_sensitivity: str = "public", allow_original_download: bool = False,
    lifetime_minutes: int = 30, clock: Clock = utcnow,
) -> dict[str, Any]:
    """TRUSTED LOCAL API, never exposed over HTTP.

    The caller attests native evidence custody, direct-reference eligibility and the
    whole-original sensitivity. Input originals are bytes, not viewer-resolved paths.
    No automatic conversion from an arbitrary query readback is provided.
    Creates new immutable files only; never overwrites an existing approval/object.
    """
    validate_manifest(manifest)
    require(valid_id(grant_id) and type(lifetime_minutes) is int and 1 <= lifetime_minutes <= 60)
    expected = {r["evidence"]["source_digest"] for r in manifest["records"]}
    require(isinstance(originals, dict) and set(originals) == expected)
    for identity, data in originals.items():
        require(isinstance(data, bytes) and len(data) <= MAX_SOURCE, "resource_limit")
        require(digest(data) == identity, "source_integrity_failed")
    manifest_bytes = canonical_json(manifest)
    require(len(manifest_bytes) <= MAX_MANIFEST, "resource_limit")
    now = clock()
    grant = {
        "schema_version": "ao.lore.source-viewer-grant.v0.1", "grant_id": grant_id,
        "primary_workspace_id": manifest["primary_workspace_id"],
        "binding_digest": digest(manifest_bytes), "created_at": format_time(now),
        "expires_at": format_time(now + timedelta(minutes=lifetime_minutes)),
        "max_sensitivity": max_sensitivity, "authorized_scope": "whole-source",
        "allow_original_download": allow_original_download,
    }
    validate_grant(grant)
    for record in manifest["records"]:
        require(LEVELS[record["original_sensitivity"]] <= LEVELS[max_sensitivity],
                "sensitivity_denied")
    # Provisioning is a trusted single-writer operation on an explicitly chosen private
    # root, never an HTTP endpoint. Existing runtime roots are inspected before writes.
    home = Path(os.path.abspath(home))
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    secure = SafeRoot(home)
    for name in ("source-viewer", "source-viewer/objects", "source-viewer/bindings",
                 "source-viewer/approvals"):
        directory = home / name
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            require(not directory.is_symlink() and directory.is_dir(), "retained_state_unavailable")
        info = directory.stat(follow_symlinks=False)
        require(info.st_uid == os.geteuid() and info.st_mode & 0o022 == 0,
                "retained_state_unavailable")

    def write_new(parts: tuple[str, ...], data: bytes, *, allow_identical: bool) -> None:
        # Descriptor walk prevents symlink races even at this trusted provisioning seam.
        fd = secure._open_root()
        out: int | None = None
        try:
            for part in parts[:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    secure._safe_owned(new, directory=True)
                except BaseException:
                    os.close(new)
                    raise
                os.close(fd)
                fd = new
            try:
                out = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                              0o600, dir_fd=fd)
            except FileExistsError:
                require(allow_identical and secure.read(parts, max(len(data), 1)) == data,
                        "already_exists")
                return
            view = memoryview(data)
            while view:
                written = os.write(out, view)
                require(written > 0, "retained_state_unavailable")
                view = view[written:]
            os.fsync(out)
        finally:
            if out is not None:
                os.close(out)
            os.close(fd)

    # Approval is written last. Partial failed staging never authorizes a source.
    for identity, data in originals.items():
        write_new(("source-viewer", "objects", identity[7:]), data, allow_identical=True)
    write_new(("source-viewer", "bindings", digest(manifest_bytes)[7:] + ".json"),
              manifest_bytes, allow_identical=True)
    write_new(("source-viewer", "approvals", grant_id + ".json"),
              canonical_json(grant), allow_identical=False)
    return grant
