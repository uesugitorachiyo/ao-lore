"""Internal fixed-boundary worker for private PDF conversion accounting."""

from __future__ import annotations

import json
import hashlib
import os
import signal
import socket
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ._strict_io import parse_strict_json, strict_read_json
from .benchmark import canonical_digest, verify_manifest
from .batch_ingestion import (
    BatchDependencies,
    ingest_verified_batch,
    validate_batch_manifest,
    validate_checkpoint,
)
from .ingestion import ingest_verified_document
from .home import repository_root, runtime_home
from .private_pdf_uat import (
    PrivatePdfSealedModelCache,
    _MAX_QUALIFICATION_BYTES,
    _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
    _BWRAP,
    _SANDBOX_SCHEMA_VERSION,
    _private_pdf_sandbox_foundation_mounts,
    _sandbox_mount,
    _campaign_origin_digest,
    _validate_campaign_origin,
    _validate_sealed_private_pdf_model_cache,
    validate_private_pdf_manifest,
)


_MANIFEST = "sources/.private-pdf-uat/run-manifest.json"
_MAX_JOURNAL_BYTES = 64 * 1024
_CONTRACT_NAME = "worker-run-contract.json"
_BARRIER_NAME = "barrier-ready.json"
_REQUIRED_OFFLINE_MODELS = (
    "models--docling-project--docling-layout-heron",
    "models--docling-project--docling-models",
)


@dataclass
class _HeldRun:
    contract: dict[str, Any]
    batch: dict[str, Any]
    qualification: dict[str, Any]
    sources: dict[str, bytes]
    journal_parent_descriptor: int
    journal_descriptor: int
    network_attempts: list[str]
    descriptors: list[int]
    sealed_cache: PrivatePdfSealedModelCache

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


def _identity(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(part, int) and not isinstance(part, bool) and part >= 0 for part in value)
    )


def _validate_sandbox_contract(
    sandbox: Any,
    *,
    root: Path,
    state: Path,
    batch_id: str,
    staging_name: str,
    sealed: PrivatePdfSealedModelCache,
) -> None:
    keys = {
        "schema_version", "mechanism", "version", "argv",
        "network_namespace", "root_filesystem", "mounts", "environment",
        "sealed_cache",
    }
    if (
        not isinstance(sandbox, dict)
        or set(sandbox) != keys
        or sandbox["schema_version"] != _SANDBOX_SCHEMA_VERSION
        or sandbox["mechanism"] != _BWRAP
        or not isinstance(sandbox["version"], str)
        or sandbox["network_namespace"] != "unshared"
        or sandbox["root_filesystem"] != "allowlisted"
        or not isinstance(sandbox["argv"], list)
        or sandbox["argv"][:5]
        != [_BWRAP, "--die-with-parent", "--unshare-net", "--tmpfs", "/"]
        or sandbox["argv"][-3:-1] != [sys.executable, "-m"]
        or sandbox["argv"][-1]
        not in {"ao_lore.private_pdf_uat_worker", "tests.private_pdf_uat_worker_fixture"}
    ):
        raise OSError("private PDF UAT worker sandbox contract is invalid")
    dependency_paths = [
        mount["target"] for mount in sandbox["mounts"]
        if isinstance(mount, dict)
        and isinstance(mount.get("name"), str)
        and mount["name"] == "python-user-site"
    ]
    base_pythonpath = (
        str(repository_root() / "src")
        if sandbox["argv"][-1] == "ao_lore.private_pdf_uat_worker"
        else f"{repository_root() / 'src'}:{repository_root()}"
    )
    expected_environment = {
        "AO_LORE_HOME": str(root),
        "HF_HOME": str(sealed.root),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_OFFLINE": "1",
        "HOME": str(state / "sandbox-home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PWD": str(repository_root()),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join([base_pythonpath, *dependency_paths]),
        "TORCH_HOME": str(sealed.root),
        "TRANSFORMERS_CACHE": str(sealed.root / "hub"),
        "TRANSFORMERS_OFFLINE": "1",
        "XDG_CACHE_HOME": str(sealed.root),
    }
    if sandbox["environment"] != expected_environment or dict(os.environ) != expected_environment:
        raise OSError("private PDF UAT worker sandbox environment is invalid")
    include_tests = sandbox["argv"][-1] == "tests.private_pdf_uat_worker_fixture"
    expected_mounts = _private_pdf_sandbox_foundation_mounts(
        include_tests=include_tests
    ) + [
        _sandbox_mount(
            repository_root() / "sources" / staging_name,
            "staged-sources", "read-only", validate_tree=True,
        ),
        _sandbox_mount(
            repository_root() / _MANIFEST,
            "control-manifest", "read-only",
        ),
        _sandbox_mount(
            root / "benchmarks" / "docling-2.118.1.json",
            "qualification", "read-only",
        ),
        _sandbox_mount(
            root / "batches" / batch_id,
            "batch-state", "read-write", validate_tree=True,
        ),
        _sandbox_mount(
            repository_root() / "working/candidates",
            "candidate-state", "read-write", validate_tree=True,
        ),
        _sandbox_mount(state, "run-state", "read-write", validate_tree=True),
        _sandbox_mount(
            sealed.root, "sealed-cache", "read-only", validate_tree=True,
        ),
    ]
    mounts = sandbox["mounts"]
    if mounts != expected_mounts:
        raise OSError("private PDF UAT worker sandbox mounts are invalid")
    if sandbox["sealed_cache"] != {
        "mode": "read-only",
        "tree_digest": sealed.tree_digest,
        "identity": list(sealed.root_identity),
    }:
        raise OSError("private PDF UAT worker sandbox cache is invalid")


class _NetworkAttempt(BaseException):
    """Unswallowable worker network-policy violation."""


def _validate_offline_cache(root: Path) -> None:
    expected = root / "cache" / "huggingface"
    if os.environ.get("HF_HOME") != str(expected):
        raise OSError("private PDF UAT worker cache environment is invalid")
    hub = expected / "hub"
    descriptors: list[int] = []

    def open_root(path: Path) -> int:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise OSError("private PDF UAT worker cache is invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            os.close(descriptor)
            raise OSError("private PDF UAT worker cache is invalid")
        descriptors.append(descriptor)
        return descriptor

    def open_child(parent: int, name: str) -> int:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise OSError("private PDF UAT worker cache is invalid")
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            os.close(descriptor)
            raise OSError("private PDF UAT worker cache is invalid")
        descriptors.append(descriptor)
        return descriptor

    def walk_snapshot(descriptor: int, logical: Path, hub_root: Path) -> int:
        files = 0
        for name in sorted(os.listdir(descriptor)):
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            path = logical / name
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve(strict=True)
                if (
                    not target.is_relative_to(hub_root)
                    or not target.is_file()
                    or target.is_symlink()
                ):
                    raise OSError("private PDF UAT worker cache is invalid")
                files += 1
            elif stat.S_ISDIR(info.st_mode):
                if not path.resolve(strict=True).is_relative_to(hub_root):
                    raise OSError("private PDF UAT worker cache is invalid")
                child = open_child(descriptor, name)
                files += walk_snapshot(child, path, hub_root)
            elif stat.S_ISREG(info.st_mode):
                if not path.resolve(strict=True).is_relative_to(hub_root):
                    raise OSError("private PDF UAT worker cache is invalid")
                files += 1
            else:
                raise OSError("private PDF UAT worker cache is invalid")
        return files

    try:
        root_descriptor = open_root(root)
        cache_descriptor = open_child(root_descriptor, "cache")
        expected_descriptor = open_child(cache_descriptor, "huggingface")
        hub_descriptor = open_child(expected_descriptor, "hub")
        expected_resolved = expected.resolve(strict=True)
        hub_resolved = hub.resolve(strict=True)
        if not hub_resolved.is_relative_to(expected_resolved):
            raise OSError("private PDF UAT worker cache is invalid")
        for model_name in _REQUIRED_OFFLINE_MODELS:
            model = hub / model_name
            snapshots = model / "snapshots"
            model_descriptor = open_child(hub_descriptor, model_name)
            snapshots_descriptor = open_child(model_descriptor, "snapshots")
            if not model.resolve(strict=True).is_relative_to(hub_resolved) or not snapshots.resolve(
                strict=True
            ).is_relative_to(hub_resolved):
                raise OSError("private PDF UAT worker cache is invalid")
            snapshot_names = sorted(os.listdir(snapshots_descriptor))
            if not snapshot_names:
                raise OSError("private PDF UAT worker cache is invalid")
            for snapshot_name in snapshot_names:
                snapshot = snapshots / snapshot_name
                snapshot_descriptor = open_child(
                    snapshots_descriptor, snapshot_name
                )
                if (
                    not snapshot.resolve(strict=True).is_relative_to(hub_resolved)
                    or walk_snapshot(
                        snapshot_descriptor, snapshot, hub_resolved
                    ) == 0
                ):
                    raise OSError("private PDF UAT worker cache is invalid")
    except (OSError, RuntimeError) as exc:
        if isinstance(exc, OSError) and str(exc).startswith("private PDF UAT"):
            raise
        raise OSError("private PDF UAT worker cache is invalid") from exc
    finally:
        while descriptors:
            os.close(descriptors.pop())


@contextmanager
def _network_guard(held: _HeldRun):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def reject_connect(_socket: socket.socket, _address: Any) -> None:
        held.network_attempts.append("connect")
        raise _NetworkAttempt("private PDF UAT worker network attempt")

    def reject_connect_ex(_socket: socket.socket, _address: Any) -> int:
        held.network_attempts.append("connect_ex")
        raise _NetworkAttempt("private PDF UAT worker network attempt")

    def reject_create_connection(*_args: Any, **_kwargs: Any) -> socket.socket:
        held.network_attempts.append("create_connection")
        raise _NetworkAttempt("private PDF UAT worker network attempt")

    socket.socket.connect = reject_connect
    socket.socket.connect_ex = reject_connect_ex
    socket.create_connection = reject_create_connection
    try:
        yield
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.create_connection = original_create_connection


def _load_run_contract() -> _HeldRun:
    root = runtime_home()
    manifest, manifest_body = strict_read_json(
        repository_root() / _MANIFEST,
        "private PDF UAT worker manifest",
        max_bytes=64 * 1024,
        root=repository_root(),
    )
    batch = validate_batch_manifest(manifest)
    state = root / "uat" / "private-pdf" / batch["batch_id"]
    contract, _ = strict_read_json(
        state / _CONTRACT_NAME,
        "private PDF UAT worker contract",
        max_bytes=_PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
        root=root,
    )
    keys = {
        "schema_version", "phase", "batch_id", "batch_manifest_digest",
        "manifest_locator", "manifest_body_digest", "manifest_identity", "staging_name",
        "staging_identity", "qualification_locator", "qualification_identity",
        "qualification_body_digest", "qualification_configuration_digest",
        "qualification_result_digest", "journal_locator", "journal_identity",
        "sealed_cache_locator", "sealed_cache_root_identity",
        "sealed_cache_tree_manifest", "sealed_cache_tree_digest",
        "sealed_cache_file_count", "sealed_cache_total_bytes",
        "prepared_evidence_digest", "campaign_origin",
        "campaign_origin_digest", "qualification_corpus_digest",
        "barrier_locator", "sandbox", "authority", "run_digest",
    }
    if (
        not isinstance(contract, dict)
        or set(contract) != keys
        or contract["schema_version"] != "ao.lore.private-pdf-uat-worker-run.v0.1"
        or contract["phase"] not in {"interrupted", "resume", "rerun"}
        or contract["batch_id"] != batch["batch_id"]
        or contract["batch_manifest_digest"] != canonical_digest(batch)
        or contract["manifest_locator"] != _MANIFEST
        or contract["manifest_body_digest"]
        != "sha256:" + hashlib.sha256(manifest_body).hexdigest()
        or contract["authority"] is not False
        or not _identity(contract["manifest_identity"])
        or not _identity(contract["staging_identity"])
        or not _identity(contract["qualification_identity"])
        or not _identity(contract["journal_identity"])
    ):
        raise OSError("private PDF UAT worker contract is invalid")
    digest_body = {key: value for key, value in contract.items() if key != "run_digest"}
    if contract["run_digest"] != canonical_digest(digest_body):
        raise OSError("private PDF UAT worker contract digest drifted")
    sealed_root = root / contract["sealed_cache_locator"]
    sealed = PrivatePdfSealedModelCache(
        root=sealed_root,
        container=sealed_root.parent,
        root_identity=tuple(contract["sealed_cache_root_identity"]),
        tree_manifest=contract["sealed_cache_tree_manifest"],
        tree_digest=contract["sealed_cache_tree_digest"],
        file_count=contract["sealed_cache_file_count"],
        total_bytes=contract["sealed_cache_total_bytes"],
    )
    if os.environ.get("HF_HOME") != str(sealed.root):
        raise OSError("private PDF UAT worker cache environment is invalid")
    _validate_sandbox_contract(
        contract["sandbox"], root=root, state=state,
        batch_id=batch["batch_id"], staging_name=contract["staging_name"],
        sealed=sealed,
    )
    _validate_sealed_private_pdf_model_cache(sealed)
    manifest_info = os.stat(
        repository_root() / _MANIFEST, follow_symlinks=False
    )
    if [manifest_info.st_dev, manifest_info.st_ino] != contract["manifest_identity"]:
        raise OSError("private PDF UAT worker manifest identity drifted")
    staging = repository_root() / "sources" / contract["staging_name"]
    staging_descriptor = os.open(
        staging, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    staging_info = os.fstat(staging_descriptor)
    if [staging_info.st_dev, staging_info.st_ino] != contract["staging_identity"]:
        os.close(staging_descriptor)
        raise OSError("private PDF UAT worker staging drifted")
    expected_sources = [
        f"sources/{contract['staging_name']}/{item['item_id']}.pdf"
        for item in batch["documents"]
    ]
    if [item["source"] for item in batch["documents"]] != expected_sources:
        raise OSError("private PDF UAT worker source mapping drifted")
    try:
        expected_names = {f"{item['item_id']}.pdf" for item in batch["documents"]}
        if set(os.listdir(staging_descriptor)) != expected_names:
            raise OSError("private PDF UAT worker source entries drifted")
        for item in batch["documents"]:
            name = f"{item['item_id']}.pdf"
            descriptor = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=staging_descriptor,
            )
            try:
                before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size > 50 * 1024 * 1024
                ):
                    raise OSError("private PDF UAT worker source drifted")
                body = os.read(descriptor, 50 * 1024 * 1024 + 1)
                after = os.fstat(descriptor)
                public = os.stat(name, dir_fd=staging_descriptor, follow_symlinks=False)
                if (
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or (public.st_dev, public.st_ino) != (after.st_dev, after.st_ino)
                    or "sha256:" + hashlib.sha256(body).hexdigest()
                    != item["source_digest"]
                ):
                    raise OSError("private PDF UAT worker source drifted")
            finally:
                os.close(descriptor)
    finally:
        os.close(staging_descriptor)
    if contract["qualification_locator"] != "benchmarks/docling-2.118.1.json":
        raise OSError("private PDF UAT worker qualification locator drifted")
    qualification, qualification_body = strict_read_json(
        root / contract["qualification_locator"],
        "private PDF UAT worker qualification",
        max_bytes=_MAX_QUALIFICATION_BYTES,
        root=root,
    )
    qualification_info = os.stat(
        root / contract["qualification_locator"], follow_symlinks=False
    )
    verify_manifest(
        qualification,
        expected_configuration_digest=contract["qualification_configuration_digest"],
    )
    if (
        [qualification_info.st_dev, qualification_info.st_ino]
        != contract["qualification_identity"]
        or "sha256:" + hashlib.sha256(qualification_body).hexdigest()
        != contract["qualification_body_digest"]
        or qualification["result_digest"] != contract["qualification_result_digest"]
    ):
        raise OSError("private PDF UAT worker qualification drifted")
    expected_journal = (
        f"uat/private-pdf/{batch['batch_id']}/conversion-journal.jsonl"
    )
    expected_barrier = f"uat/private-pdf/{batch['batch_id']}/{_BARRIER_NAME}"
    if (
        contract["journal_locator"] != expected_journal
        or contract["barrier_locator"] != expected_barrier
    ):
        raise OSError("private PDF UAT worker state locator drifted")
    journal_info = os.stat(root / expected_journal, follow_symlinks=False)
    if [journal_info.st_dev, journal_info.st_ino] != contract["journal_identity"]:
        raise OSError("private PDF UAT worker journal drifted")
    evidence, _ = strict_read_json(
        state / "prepared-run-evidence.json",
        "private PDF UAT worker prepared evidence",
        max_bytes=_PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
        root=root,
    )
    if canonical_digest(evidence) != contract["prepared_evidence_digest"]:
        raise OSError("private PDF UAT worker prepared evidence drifted")
    corpus = validate_private_pdf_manifest(evidence.get("corpus_manifest"))
    origin = _validate_campaign_origin(
        contract["campaign_origin"], corpus
    )
    if (
        contract["campaign_origin_digest"] != _campaign_origin_digest(origin)
        or evidence.get("campaign_origin") != contract["campaign_origin"]
        or evidence.get("campaign_origin_digest")
        != contract["campaign_origin_digest"]
        or qualification.get("fixture_corpus_digest")
        != contract["qualification_corpus_digest"]
    ):
        raise OSError("private PDF UAT worker campaign origin drifted")
    descriptors: list[int] = []
    held_sources: dict[str, bytes] = {}
    try:
        manifest_descriptor = os.open(
            repository_root() / _MANIFEST,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(manifest_descriptor)
        if [os.fstat(manifest_descriptor).st_dev, os.fstat(manifest_descriptor).st_ino] != contract["manifest_identity"]:
            raise OSError("private PDF UAT worker held manifest drifted")
        held_manifest_body = os.pread(manifest_descriptor, 64 * 1024 + 1, 0)
        if (
            "sha256:" + hashlib.sha256(held_manifest_body).hexdigest()
            != contract["manifest_body_digest"]
            or validate_batch_manifest(parse_strict_json(
                held_manifest_body, "private PDF UAT held manifest"
            )) != batch
        ):
            raise OSError("private PDF UAT worker held manifest body drifted")
        qualification_descriptor = os.open(
            root / contract["qualification_locator"],
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(qualification_descriptor)
        if [os.fstat(qualification_descriptor).st_dev, os.fstat(qualification_descriptor).st_ino] != contract["qualification_identity"]:
            raise OSError("private PDF UAT worker held qualification drifted")
        held_qualification_body = os.pread(
            qualification_descriptor, _MAX_QUALIFICATION_BYTES + 1, 0
        )
        if (
            "sha256:" + hashlib.sha256(held_qualification_body).hexdigest()
            != contract["qualification_body_digest"]
            or parse_strict_json(
                held_qualification_body, "private PDF UAT held qualification"
            ) != qualification
        ):
            raise OSError("private PDF UAT worker held qualification body drifted")
        held_staging = os.open(
            staging,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(held_staging)
        if [os.fstat(held_staging).st_dev, os.fstat(held_staging).st_ino] != contract["staging_identity"]:
            raise OSError("private PDF UAT worker held staging drifted")
        for item in batch["documents"]:
            name = f"{item['item_id']}.pdf"
            source_descriptor = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=held_staging,
            )
            descriptors.append(source_descriptor)
            body = os.read(source_descriptor, 50 * 1024 * 1024 + 1)
            if "sha256:" + hashlib.sha256(body).hexdigest() != item["source_digest"]:
                raise OSError("private PDF UAT worker held source drifted")
            held_sources[item["source"]] = body
        journal_parent_descriptor = os.open(
            (root / contract["journal_locator"]).parent,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(journal_parent_descriptor)
        journal_descriptor = os.open(
            "conversion-journal.jsonl",
            os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=journal_parent_descriptor,
        )
        descriptors.append(journal_descriptor)
        held_journal_info = os.fstat(journal_descriptor)
        if (
            [held_journal_info.st_dev, held_journal_info.st_ino]
            != contract["journal_identity"]
            or not stat.S_ISREG(held_journal_info.st_mode)
            or held_journal_info.st_nlink != 1
            or held_journal_info.st_size > _MAX_JOURNAL_BYTES
        ):
            raise OSError("private PDF UAT worker held journal drifted")
        for path in (state / _CONTRACT_NAME, state / "prepared-run-evidence.json"):
            descriptors.append(os.open(
                path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            ))
        return _HeldRun(
            contract, batch, qualification, held_sources,
            journal_parent_descriptor, journal_descriptor, [], descriptors,
            sealed,
        )
    except BaseException:
        while descriptors:
            os.close(descriptors.pop())
        raise


def _verify_public_journal(held: _HeldRun) -> os.stat_result:
    info = os.fstat(held.journal_descriptor)
    public = os.stat(
        "conversion-journal.jsonl",
        dir_fd=held.journal_parent_descriptor,
        follow_symlinks=False,
    )
    expected = held.contract["journal_identity"]
    if (
        [info.st_dev, info.st_ino] != expected
        or [public.st_dev, public.st_ino] != expected
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > _MAX_JOURNAL_BYTES
    ):
        raise OSError("conversion journal binding drifted")
    return info


def _journal_snapshot(held: _HeldRun) -> tuple[int, str | None, os.stat_result]:
    before = _verify_public_journal(held)
    body = os.pread(held.journal_descriptor, _MAX_JOURNAL_BYTES + 1, 0)
    after = os.fstat(held.journal_descriptor)
    if (
        len(body) > _MAX_JOURNAL_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise OSError("conversion journal is invalid")
    _verify_public_journal(held)
    lines = body.splitlines(keepends=True)
    previous: str | None = None
    for sequence, line in enumerate(lines, 1):
        if not line.endswith(b"\n"):
            raise OSError("conversion journal is invalid")
        value = parse_strict_json(line, "private PDF UAT conversion journal")
        if (
            not isinstance(value, dict)
            or set(value) != {
                "schema_version", "sequence", "previous_event_digest",
                "event_digest",
            }
            or value["schema_version"]
            != "ao.lore.private-pdf-conversion-event.v0.1"
            or value["sequence"] != sequence
            or value["previous_event_digest"] != previous
        ):
            raise OSError("conversion journal is invalid")
        event_body = {
            key: child for key, child in value.items() if key != "event_digest"
        }
        if value["event_digest"] != canonical_digest(event_body):
            raise OSError("conversion journal is invalid")
        previous = value["event_digest"]
    return len(lines), previous, after


def _journal_count(held: _HeldRun) -> int:
    return _journal_snapshot(held)[0]


def _persist_barrier(
    contract: dict[str, Any], batch: dict[str, Any], count: int
) -> None:
    root = runtime_home()
    batch_root = root / "batches" / batch["batch_id"]
    deadline = time.monotonic() + 30.0
    checkpoint: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        try:
            supplied, _ = strict_read_json(
                batch_root / "checkpoint.json",
                "private PDF UAT worker live checkpoint",
                max_bytes=64 * 1024,
                root=root,
            )
            observed = validate_checkpoint(
                supplied, batch, contract["batch_manifest_digest"]
            )
        except FileNotFoundError:
            time.sleep(0.005)
            continue
        if observed["processed"] == count:
            checkpoint = observed
            break
        if observed["processed"] > count:
            raise OSError("private PDF UAT worker checkpoint passed barrier")
        time.sleep(0.005)
    if checkpoint is None:
        raise OSError("private PDF UAT worker barrier timed out")
    barrier = {
        "schema_version": "ao.lore.private-pdf-uat-barrier.v0.1",
        "phase": "interrupted",
        "batch_id": batch["batch_id"],
        "manifest_digest": contract["batch_manifest_digest"],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "processed": count,
        "journal_count": count,
        "run_digest": contract["run_digest"],
        "authority": False,
    }
    body = (json.dumps(barrier, sort_keys=True, separators=(",", ":")) + "\n").encode()
    state = root / "uat" / "private-pdf" / batch["batch_id"]
    descriptor = os.open(
        state, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    temporary = f".barrier-{os.getpid()}.tmp"
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=descriptor,
        )
        view = memoryview(body)
        while view:
            written = os.write(file_descriptor, view)
            if written <= 0:
                raise OSError("private PDF UAT worker barrier short write")
            view = view[written:]
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        os.replace(temporary, _BARRIER_NAME, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def _append_conversion_event(held: _HeldRun) -> None:
    count, previous, info = _journal_snapshot(held)
    try:
        event: dict[str, Any] = {
            "schema_version": "ao.lore.private-pdf-conversion-event.v0.1",
            "sequence": count + 1,
            "previous_event_digest": previous,
        }
        event["event_digest"] = canonical_digest(event)
        encoded = (
            json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if info.st_size + len(encoded) > _MAX_JOURNAL_BYTES:
            raise OSError("conversion journal is full")
        _verify_public_journal(held)
        view = memoryview(encoded)
        while view:
            written = os.write(held.journal_descriptor, view)
            if written <= 0:
                raise OSError("conversion journal short write")
            view = view[written:]
        os.fsync(held.journal_descriptor)
        os.fsync(held.journal_parent_descriptor)
        _verify_public_journal(held)
        if _journal_count(held) != count + 1:
            raise OSError("conversion journal append verification failed")
    except BaseException:
        _verify_public_journal(held)
        raise


def _validate_held_sealed_cache(held: _HeldRun) -> None:
    # Injected unit workers use small structural stand-ins. Every production
    # worker is constructed only by _load_run_contract and therefore carries
    # the exact sealed-cache evidence validated here.
    if isinstance(held, _HeldRun):
        _validate_sealed_private_pdf_model_cache(held.sealed_cache)


def main(
    *,
    after_validation: Callable[[_HeldRun], None] | None = None,
    after_ingest: Callable[[_HeldRun], None] | None = None,
) -> int:
    held = _load_run_contract()
    callback_started = False
    backend: type[Any] | None = None
    original: Callable[..., Any] | None = None
    try:
        with _network_guard(held):
            _validate_held_sealed_cache(held)
            callback_started = True
            if after_validation is not None:
                after_validation(held)
            contract, batch = held.contract, held.batch
            from .docling_pdf import DoclingBackend

            backend = DoclingBackend
            original = DoclingBackend.convert

            def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
                if held.network_attempts:
                    raise _NetworkAttempt(
                        "private PDF UAT worker network attempt was caught"
                    )
                _validate_held_sealed_cache(held)
                count = _journal_count(held)
                if contract["phase"] == "interrupted" and count >= 1:
                    _persist_barrier(contract, batch, count)
                    while True:
                        signal.pause()
                try:
                    converted = original(self, *args, **kwargs)
                except Exception:
                    if held.network_attempts:
                        raise _NetworkAttempt(
                            "private PDF UAT worker network attempt was caught"
                        )
                    _append_conversion_event(held)
                    raise
                except BaseException:
                    if held.network_attempts:
                        raise _NetworkAttempt(
                            "private PDF UAT worker network attempt was caught"
                        )
                    raise
                if held.network_attempts:
                    raise _NetworkAttempt(
                        "private PDF UAT worker network attempt was caught"
                    )
                _append_conversion_event(held)
                return converted

            DoclingBackend.convert = counted
            try:
                from .__main__ import _ingest_profiles

                selection, quality = _ingest_profiles(held.qualification)

                def source_key(path: str | Path) -> str:
                    return Path(path).relative_to(repository_root()).as_posix()

                def read_source(path: str | Path, expected_digest: str) -> bytes:
                    body = held.sources.get(source_key(path))
                    if (
                        body is None
                        or "sha256:" + hashlib.sha256(body).hexdigest()
                        != expected_digest
                    ):
                        raise OSError("private PDF UAT held source binding drifted")
                    return body

                def ingest_one(path: str | Path, **kwargs: Any) -> dict[str, Any]:
                    body = read_source(path, kwargs["expected_source_digest"])
                    digest = kwargs["expected_source_digest"]
                    return ingest_verified_document(
                        body,
                        source_digest=digest,
                        resource=(
                            "source-"
                            + digest.removeprefix("sha256:")[:16]
                            + ".pdf"
                        ),
                        benchmark_manifest=kwargs["benchmark_manifest"],
                        selection_profile=kwargs["selection_profile"],
                        quality_profile=kwargs["quality_profile"],
                        candidate_context=kwargs["candidate_context"],
                        dependencies=kwargs["dependencies"],
                        candidate_root=kwargs["candidate_root"],
                    )

                report = ingest_verified_batch(
                    batch,
                    contract["batch_manifest_digest"],
                    dependencies=BatchDependencies(
                        benchmark_manifest=held.qualification,
                        selection_profile=selection,
                        quality_profile=quality,
                        ingest_one=ingest_one,
                        read_source=read_source,
                    ),
                )
                if held.network_attempts:
                    raise _NetworkAttempt(
                        "private PDF UAT worker network attempt was caught"
                    )
            finally:
                if backend is not None and original is not None:
                    backend.convert = original
                if callback_started and after_ingest is not None:
                    after_ingest(held)
        if held.network_attempts:
            raise _NetworkAttempt("private PDF UAT worker network attempt was caught")
        _validate_held_sealed_cache(held)
    finally:
        held.close()
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
