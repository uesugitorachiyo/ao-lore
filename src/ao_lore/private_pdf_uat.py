"""Strict contracts for AO Lore private PDF UAT manifests and readbacks."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
import pwd
import re
import selectors
import secrets
import signal
import socket
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time
import tracemalloc
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from ._strict_io import (
    ContractError,
    ensure_contained,
    parse_strict_json,
    reject_nonfinite,
    require_bool,
    require_exact_keys,
    require_identifier,
    require_int,
    require_text,
    strict_read_json,
)
from .home import repository_root, runtime_home
from .benchmark import canonical_digest, verify_manifest
from .docling_pdf import DoclingPdfAdapter, configuration_digest
from .parsing import ParseOutput, ParsingError
from .pdf_benchmark import run_pdf_benchmark
from .private_document_uat import (
    PrivateDocumentCampaignPolicy,
    PrivateDocumentPreparedRun,
    PrivateDocumentUatDependencies,
    private_document_policy_digest,
    private_document_state_body,
    run_private_document_uat,
)
from .private_pdf_domain import (
    DOMAIN_CORPUS_ID,
    PdfQualification,
    PreparedDomainDocument,
    ReviewedDomainCorpus,
    load_reviewed_domain_sources,
)
from .batch_ingestion import (
    load_final_batch_readback,
    load_or_initialize_checkpoint,
    validate_batch_manifest,
    validate_checkpoint,
    validate_final_batch_readback,
)
from .candidate_queue import list_candidates
from .candidates import MAX_RECORD_BYTES, _candidate_root, load_verified_candidate


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_FILE_RE = re.compile(
    r"^(?!/)(?!.*\\)(?!.*//)(?!.*(?:^|/)\.{1,2}(?:/|$))[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\.pdf$"
)
_BLOCK_TYPES = (
    "heading",
    "paragraph",
    "list",
    "table",
    "code",
    "image",
    "caption",
    "footnote",
    "link",
)
_METRIC_NAMES = (
    "structural_fidelity",
    "text_fidelity",
    "source_location_fidelity",
)
_FAILURE_CODES = (
    "conversion_failed",
    "digest_mismatch",
    "encrypted_document",
    "invalid_pdf",
    "no_readable_content",
)

MAX_PRIVATE_PDF_BYTES = 50 * 1024 * 1024
_PRIVATE_CALIBRATION_MAX_TOTAL_BYTES = 4 * MAX_PRIVATE_PDF_BYTES
_MAX_TOOL_OUTPUT_BYTES = 8 * 1024 * 1024
_TOOL_TIMEOUT_SECONDS = 30.0
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_QUALIFICATION_BYTES = 1024 * 1024
PRIVATE_PDF_UAT_CORPUS_ID = "ubuntu-pdf-seed-v1"
PRIVATE_PDF_UAT_ITEM_IDS = ("seed-01", "seed-02", "seed-03", "seed-04")
_CORPUS_ID = PRIVATE_PDF_UAT_CORPUS_ID
_MANIFEST_NAME = "manifest.json"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_RENAME_NOREPLACE = 1
_LOCK_NAME = ".ubuntu-pdf-seed.lock"
_CLEANUP_QUARANTINE = ".ubuntu-pdf-seed.cleanup-quarantine"
_RECOVERY_PREFIX = ".ubuntu-pdf-seed-recovery-"
_RECOVERY_STAGING = ".ubuntu-pdf-seed-recovery-staging"
_PREPARATION_RECOVERY = ".ubuntu-pdf-seed-preparation-recovery"
_RECOVERY_SCHEMA_NAME = "recovery.json"
_RECOVERY_SCHEMA_VERSION = "ao.lore.private-pdf-cleanup-recovery.v0.1"
_RECLAIM_PREFIX = ".ubuntu-pdf-seed-reclaim-"
_RECLAIM_STAGING = ".ubuntu-pdf-seed-reclaim-staging"
_DOMAIN_STAGE = ".pdf-nomagic-domain-preparation"
_DOMAIN_PREPARATION_RECOVERY = ".pdf-nomagic-domain-preparation-recovery"
_DOMAIN_CLEANUP_QUARANTINE = ".pdf-nomagic-domain-cleanup-quarantine"
_DOMAIN_RECOVERY_PREFIX = ".pdf-nomagic-domain-recovery-"
_DOMAIN_RECOVERY_STAGING = ".pdf-nomagic-domain-recovery-staging"
_DOMAIN_RECLAIM_PREFIX = ".pdf-nomagic-domain-reclaim-"
_DOMAIN_RECLAIM_STAGING = ".pdf-nomagic-domain-reclaim-staging"
_LOCK_TIMEOUT_SECONDS = 2.0
_CALIBRATION_GUARD_LOCK = threading.Lock()
_OFFLINE_ENVIRONMENT = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
}
PRIVATE_PDF_MAX_PAGES = 500
PRIVATE_PDF_MAX_BLOCKS = 100_000
PRIVATE_PDF_CACHE_MAX_DEPTH = 24
PRIVATE_PDF_CACHE_MAX_FILES = 20_000
PRIVATE_PDF_CACHE_MAX_FILE_BYTES = 768 * 1024 * 1024
PRIVATE_PDF_CACHE_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
_CACHE_DIRECTORY_MODE = 0o555
_CACHE_FILE_MODE = 0o444
_FICLONE = 0x40049409
_PRIVATE_PDF_REQUIRED_CACHE_MODELS = (
    "models--docling-project--docling-layout-heron",
    "models--docling-project--docling-models",
)
PRIVATE_PDF_CONFIGURATION_DIGEST = configuration_digest(
    MAX_PRIVATE_PDF_BYTES, PRIVATE_PDF_MAX_PAGES, PRIVATE_PDF_MAX_BLOCKS
)
_PRIVATE_FIDELITY_MINIMUMS = {
    "structural_fidelity": 0.5,
    "text_fidelity": 0.5,
    "source_location_fidelity": 0.5,
}
_QUALIFIED_PDF_CORPUS_DIGEST = (
    "sha256:0a4a255050de73593319d61152470fba3c71bd7a82bf132272e05d6b77b66f5e"
)


@dataclass(frozen=True)
class _PrivatePdfCorpusSpec:
    corpus_id: str
    item_ids: tuple[str, ...]
    preparation_recovery: str
    cleanup_quarantine: str
    recovery_prefix: str
    recovery_staging: str
    reclaim_prefix: str
    reclaim_staging: str
    representative_domain: bool
    provenance_digest: str | None
    transformation_id: str | None


_UBUNTU_CORPUS_SPEC = _PrivatePdfCorpusSpec(
    corpus_id=_CORPUS_ID,
    item_ids=PRIVATE_PDF_UAT_ITEM_IDS,
    preparation_recovery=_PREPARATION_RECOVERY,
    cleanup_quarantine=_CLEANUP_QUARANTINE,
    recovery_prefix=_RECOVERY_PREFIX,
    recovery_staging=_RECOVERY_STAGING,
    reclaim_prefix=_RECLAIM_PREFIX,
    reclaim_staging=_RECLAIM_STAGING,
    representative_domain=False,
    provenance_digest=None,
    transformation_id=None,
)
_DOMAIN_CORPUS_SPEC = _PrivatePdfCorpusSpec(
    corpus_id=DOMAIN_CORPUS_ID,
    item_ids=("domain-01", "domain-02", "domain-03", "domain-04"),
    preparation_recovery=_DOMAIN_PREPARATION_RECOVERY,
    cleanup_quarantine=_DOMAIN_CLEANUP_QUARANTINE,
    recovery_prefix=_DOMAIN_RECOVERY_PREFIX,
    recovery_staging=_DOMAIN_RECOVERY_STAGING,
    reclaim_prefix=_DOMAIN_RECLAIM_PREFIX,
    reclaim_staging=_DOMAIN_RECLAIM_STAGING,
    representative_domain=True,
    provenance_digest=(
        "sha256:adbf22aeb0a01e677cadc3805687f21b30ac4e4c70ed7694cdb72afb8101be65"
    ),
    transformation_id="pdf-nomagic-restore-five-byte-header-v1",
)
_PRIVATE_PDF_CORPUS_SPECS = {
    _UBUNTU_CORPUS_SPEC.corpus_id: _UBUNTU_CORPUS_SPEC,
    _DOMAIN_CORPUS_SPEC.corpus_id: _DOMAIN_CORPUS_SPEC,
}


@dataclass(frozen=True)
class SeedDocument:
    """One explicitly reviewed public Ubuntu input and its bounded annotations."""

    item_id: str
    source: Path
    expected_text: tuple[str, ...]
    required_block_types: tuple[str, ...]


UBUNTU_PDF_SEED = (
    SeedDocument(
        "seed-01",
        Path("/usr/share/doc/shared-mime-info/shared-mime-info-spec.pdf"),
        ("Shared MIME-info Database",),
        ("heading", "paragraph"),
    ),
    SeedDocument(
        "seed-02",
        Path("/usr/share/doc/printer-driver-foo2zjs/manual.pdf"),
        ("General Commands Manual",),
        ("heading", "paragraph", "list"),
    ),
    SeedDocument(
        "seed-03",
        Path("/usr/share/cups/data/form_english.pdf"),
        ("TASK INFORMATION",),
        ("heading", "table"),
    ),
    SeedDocument(
        "seed-04",
        Path("/usr/share/cups/data/default-testpage.pdf"),
        ("Printer test page",),
        ("heading", "paragraph", "image"),
    ),
)

if tuple(seed.item_id for seed in UBUNTU_PDF_SEED) != PRIVATE_PDF_UAT_ITEM_IDS:
    raise RuntimeError("private PDF seed identity binding is invalid")


class PrivatePdfUatError(ContractError):
    """Raised when private PDF UAT evidence violates its exact contract."""


@dataclass(frozen=True)
class PrivatePdfSealedModelCache:
    """Exact run-owned immutable copy of the qualified model cache."""

    root: Path
    container: Path
    root_identity: tuple[int, int]
    tree_manifest: Mapping[str, Any]
    tree_digest: str
    file_count: int
    total_bytes: int


@dataclass(frozen=True)
class _PrivatePdfCacheInspection:
    root: Path
    root_identity: tuple[int, int]
    tree_manifest: Mapping[str, Any]
    sources: Mapping[str, tuple[str, ...]]


def _fail(message: str = "private PDF UAT contract is invalid") -> PrivatePdfUatError:
    return PrivatePdfUatError(message)


def _strict_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    if not isinstance(decoded, dict):
        raise _fail()
    return decoded


def _open_runtime_root(runtime_root: Path | None) -> tuple[Path, int]:
    selected = runtime_home() if runtime_root is None else Path(runtime_root)
    absolute, _ = ensure_contained(selected, repository_root(), "private PDF runtime root")
    descriptor = _open_directory(absolute, repository_root(), create=True)
    return absolute, descriptor


def _open_directory(path: Path, root: Path, *, create: bool) -> int:
    """Open a contained directory through a held, no-follow descriptor chain."""

    selected, absolute_root = ensure_contained(path, root, "private PDF runtime state")
    parts = selected.relative_to(absolute_root).parts
    current: int | None = None
    try:
        current = os.open(os.path.sep, _DIRECTORY_FLAGS)
        for component in absolute_root.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        for component in parts:
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise OSError("invalid directory")
        return current
    except BaseException:
        if current is not None:
            os.close(current)
        raise


def _open_relative_directory(
    parent_descriptor: int, parts: Sequence[str], *, create: bool
) -> int:
    """Traverse only bounded child names beneath one held directory descriptor."""

    current = os.dup(parent_descriptor)
    try:
        for component in parts:
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", component):
                raise OSError("invalid private PDF directory component")
            try:
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise OSError("invalid private PDF directory")
        return current
    except BaseException:
        os.close(current)
        raise


def _hash_cache_file_descriptor(descriptor: int, maximum: int) -> tuple[str, int]:
    """Hash one held cache file without allocating a model-sized byte string."""

    digest = hashlib.sha256()
    total = 0
    offset = 0
    while True:
        chunk = os.pread(descriptor, min(1024 * 1024, maximum + 1 - total), offset)
        if not chunk:
            break
        total += len(chunk)
        if total > maximum:
            raise OSError("cache file exceeds bound")
        digest.update(chunk)
        offset += len(chunk)
    return "sha256:" + digest.hexdigest(), total


def _open_cache_directory_at(
    parent_descriptor: int, parts: Sequence[str]
) -> int:
    current = os.dup(parent_descriptor)
    try:
        for component in parts:
            if (
                not component
                or component in {".", ".."}
                or "/" in component
                or "\x00" in component
                or len(os.fsencode(component)) > 255
            ):
                raise OSError("invalid cache directory component")
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_cache_file_at(root_descriptor: int, parts: Sequence[str]) -> int:
    if not parts:
        raise OSError("cache file path is empty")
    parent = _open_cache_directory_at(root_descriptor, parts[:-1])
    try:
        return os.open(
            parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent
        )
    finally:
        os.close(parent)


def _cache_file_binding(
    root: Path,
    root_descriptor: int,
    logical_parts: tuple[str, ...],
    supplied: os.stat_result,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    logical = PurePosixPath(*logical_parts).as_posix()
    source_parts = logical_parts
    link_before: tuple[int, int, int, int] | None = None
    if stat.S_ISLNK(supplied.st_mode):
        parent = _open_cache_directory_at(root_descriptor, logical_parts[:-1])
        try:
            target_text = os.readlink(logical_parts[-1], dir_fd=parent)
        finally:
            os.close(parent)
        if os.path.isabs(target_text) or "\x00" in target_text:
            raise OSError("cache link is invalid")
        target = (root.joinpath(*logical_parts[:-1]) / target_text).resolve(strict=True)
        root_resolved = root.resolve(strict=True)
        if not target.is_relative_to(root_resolved):
            raise OSError("cache link escapes")
        source_parts = target.relative_to(root_resolved).parts
        link_before = (
            supplied.st_dev,
            supplied.st_ino,
            supplied.st_mtime_ns,
            supplied.st_ctime_ns,
        )
    elif not stat.S_ISREG(supplied.st_mode):
        raise OSError("cache entry is not regular")

    descriptor = _open_cache_file_at(root_descriptor, source_parts)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > PRIVATE_PDF_CACHE_MAX_FILE_BYTES
        ):
            raise OSError("cache file binding is invalid")
        digest, size = _hash_cache_file_descriptor(
            descriptor, PRIVATE_PDF_CACHE_MAX_FILE_BYTES
        )
        after = os.fstat(descriptor)
        source_parent = _open_cache_directory_at(root_descriptor, source_parts[:-1])
        try:
            public = os.stat(
                source_parts[-1], dir_fd=source_parent, follow_symlinks=False
            )
        finally:
            os.close(source_parent)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (public.st_dev, public.st_ino) != (after.st_dev, after.st_ino)
            or size != after.st_size
        ):
            raise OSError("cache file mutated")
    finally:
        os.close(descriptor)
    if link_before is not None:
        parent = _open_cache_directory_at(root_descriptor, logical_parts[:-1])
        try:
            link_after = os.stat(
                logical_parts[-1], dir_fd=parent, follow_symlinks=False
            )
        finally:
            os.close(parent)
        if link_before != (
            link_after.st_dev,
            link_after.st_ino,
            link_after.st_mtime_ns,
            link_after.st_ctime_ns,
        ):
            raise OSError("cache link mutated")
    return {
        "path": logical,
        "type": "file",
        "mode": _CACHE_FILE_MODE,
        "size": size,
        "digest": digest,
    }, tuple(source_parts)


def _scan_private_pdf_model_cache(
    root: Path, *, require_sealed_modes: bool = False
) -> _PrivatePdfCacheInspection:
    selected, _ = ensure_contained(root, repository_root(), "private PDF model cache")
    before = os.lstat(selected)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise OSError("cache root is invalid")
    root_descriptor = _open_directory(selected, repository_root(), create=False)
    entries: list[dict[str, Any]] = []
    sources: dict[str, tuple[str, ...]] = {}
    file_count = 0
    total_bytes = 0
    try:
        held = os.fstat(root_descriptor)
        if (before.st_dev, before.st_ino) != (held.st_dev, held.st_ino):
            raise OSError("cache root changed")

        def walk(descriptor: int, prefix: tuple[str, ...], depth: int) -> None:
            nonlocal file_count, total_bytes
            if depth > PRIVATE_PDF_CACHE_MAX_DEPTH:
                raise OSError("cache depth exceeds bound")
            names = sorted(os.listdir(descriptor))
            for name in names:
                if not name or "/" in name or "\x00" in name or len(os.fsencode(name)) > 255:
                    raise OSError("cache name is invalid")
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                parts = (*prefix, name)
                logical = PurePosixPath(*parts).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    if (
                        require_sealed_modes
                        and stat.S_IMODE(info.st_mode) != _CACHE_DIRECTORY_MODE
                    ):
                        raise OSError("sealed cache directory mode drifted")
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                    try:
                        child_info = os.fstat(child)
                        if (info.st_dev, info.st_ino) != (
                            child_info.st_dev, child_info.st_ino
                        ):
                            raise OSError("cache directory changed")
                        entries.append({
                            "path": logical,
                            "type": "directory",
                            "mode": _CACHE_DIRECTORY_MODE,
                        })
                        walk(child, parts, depth + 1)
                        public = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        if (public.st_dev, public.st_ino) != (
                            child_info.st_dev, child_info.st_ino
                        ):
                            raise OSError("cache directory changed")
                    finally:
                        os.close(child)
                else:
                    if require_sealed_modes and (
                        stat.S_ISLNK(info.st_mode)
                        or stat.S_IMODE(info.st_mode) != _CACHE_FILE_MODE
                    ):
                        raise OSError("sealed cache file mode drifted")
                    entry, source_parts = _cache_file_binding(
                        selected, root_descriptor, parts, info
                    )
                    file_count += 1
                    total_bytes += entry["size"]
                    if (
                        file_count > PRIVATE_PDF_CACHE_MAX_FILES
                        or total_bytes > PRIVATE_PDF_CACHE_MAX_TOTAL_BYTES
                    ):
                        raise OSError("cache tree exceeds bound")
                    entries.append(entry)
                    sources[logical] = source_parts

        walk(root_descriptor, (), 1)
        after = os.lstat(selected)
        held_after = os.fstat(root_descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns)
            or (held.st_dev, held.st_ino) != (held_after.st_dev, held_after.st_ino)
        ):
            raise OSError("cache root changed")
    finally:
        os.close(root_descriptor)
    if file_count == 0:
        raise OSError("cache is empty")
    manifest = {
        "schema_version": "ao.lore.private-pdf-model-cache-tree.v0.1",
        "entries": entries,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }
    manifest_body = (
        json.dumps(
            manifest, allow_nan=False, sort_keys=True, separators=(",", ":")
        ) + "\n"
    ).encode()
    if len(manifest_body) > _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES:
        raise OSError("cache manifest exceeds bound")
    file_paths = {
        entry["path"]
        for entry in entries
        if entry["type"] == "file"
    }
    for model in _PRIVATE_PDF_REQUIRED_CACHE_MODELS:
        prefix = f"hub/{model}/snapshots/"
        if not any(
            path.startswith(prefix) and len(PurePosixPath(path).parts) >= 5
            for path in file_paths
        ):
            raise OSError("qualified cache model is absent")
    return _PrivatePdfCacheInspection(
        selected,
        (before.st_dev, before.st_ino),
        manifest,
        sources,
    )


def _inspect_private_pdf_model_cache(root: Path) -> dict[str, Any]:
    """Return the deterministic dereferenced content tree for one qualified cache."""

    try:
        return _strict_copy(_scan_private_pdf_model_cache(Path(root)).tree_manifest)
    except (ContractError, OSError, RuntimeError, ValueError) as exc:
        raise _fail("private PDF model cache is invalid") from exc


def _copy_cache_file(source: int, destination: int) -> None:
    """Copy a held cache file, using a reflink when the filesystem supports it."""

    try:
        fcntl.ioctl(destination, _FICLONE, source)
        return
    except OSError as exc:
        if exc.errno not in {
            errno.EBADF, errno.EINVAL, errno.ENOTTY, errno.EOPNOTSUPP,
            errno.EXDEV,
        }:
            raise
    os.ftruncate(destination, 0)
    offset = 0
    while True:
        chunk = os.pread(source, 1024 * 1024, offset)
        if not chunk:
            return
        view = memoryview(chunk)
        while view:
            written = os.write(destination, view)
            if written <= 0:
                raise OSError("cache copy short write")
            view = view[written:]
        offset += len(chunk)


def _remove_owned_cache_tree_at(
    parent_descriptor: int,
    name: str,
    identity: tuple[int, int],
) -> bool:
    """Remove one directory only while its public binding remains call-owned."""

    try:
        public = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISDIR(public.st_mode)
        or stat.S_ISLNK(public.st_mode)
        or (public.st_dev, public.st_ino) != identity
    ):
        return False
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != identity:
            return False
        os.fchmod(descriptor, 0o700)
        for child_name in os.listdir(descriptor):
            child = os.stat(
                child_name, dir_fd=descriptor, follow_symlinks=False
            )
            child_identity = (child.st_dev, child.st_ino)
            if stat.S_ISDIR(child.st_mode) and not stat.S_ISLNK(child.st_mode):
                if not _remove_owned_cache_tree_at(
                    descriptor, child_name, child_identity
                ):
                    raise OSError("owned cache cleanup binding drifted")
            elif stat.S_ISREG(child.st_mode) and not stat.S_ISLNK(child.st_mode):
                rebound = os.stat(
                    child_name, dir_fd=descriptor, follow_symlinks=False
                )
                if (rebound.st_dev, rebound.st_ino) != child_identity:
                    raise OSError("owned cache cleanup binding drifted")
                os.unlink(child_name, dir_fd=descriptor)
            else:
                raise OSError("owned cache cleanup entry is invalid")
    finally:
        os.close(descriptor)
    rebound = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (rebound.st_dev, rebound.st_ino) != identity:
        return False
    os.rmdir(name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
    return True


def _seal_private_pdf_model_cache(
    original_root: Path, state_directory: Path
) -> PrivatePdfSealedModelCache:
    """Create and verify one random run-owned immutable cache snapshot."""

    inspection = _scan_private_pdf_model_cache(Path(original_root))
    state, _ = ensure_contained(
        Path(state_directory), repository_root(), "private PDF UAT state"
    )
    state_descriptor = _open_directory(state, repository_root(), create=True)
    token = secrets.token_hex(12)
    temporary_name = f"sealed-cache-{token}.partial"
    final_name = f"sealed-cache-{token}"
    temporary = state / temporary_name
    final = state / final_name
    source_descriptor: int | None = None
    owned_identity: tuple[int, int] | None = None
    owned_name: str | None = None
    try:
        if _binding_exists_at(state_descriptor, temporary_name) or _binding_exists_at(
            state_descriptor, final_name
        ):
            raise OSError("sealed cache collision")
        os.mkdir(temporary_name, 0o700, dir_fd=state_descriptor)
        temporary_info = os.stat(
            temporary_name, dir_fd=state_descriptor, follow_symlinks=False
        )
        owned_identity = (temporary_info.st_dev, temporary_info.st_ino)
        owned_name = temporary_name
        container_descriptor = os.open(temporary_name, _DIRECTORY_FLAGS, dir_fd=state_descriptor)
        try:
            os.mkdir("huggingface", 0o700, dir_fd=container_descriptor)
            os.fsync(container_descriptor)
        finally:
            os.close(container_descriptor)
        cache_root = temporary / "huggingface"
        cache_descriptor = _open_directory(cache_root, state, create=False)
        source_descriptor = _open_directory(
            inspection.root, repository_root(), create=False
        )
        if (
            os.fstat(source_descriptor).st_dev,
            os.fstat(source_descriptor).st_ino,
        ) != inspection.root_identity:
            raise OSError("original cache root drifted")
        try:
            for entry in inspection.tree_manifest["entries"]:
                parts = PurePosixPath(entry["path"]).parts
                if entry["type"] == "directory":
                    parent = _open_cache_directory_at(cache_descriptor, parts[:-1])
                    try:
                        os.mkdir(parts[-1], 0o700, dir_fd=parent)
                        os.fsync(parent)
                    finally:
                        os.close(parent)
                    continue
                parent = _open_cache_directory_at(cache_descriptor, parts[:-1])
                src = _open_cache_file_at(
                    source_descriptor, inspection.sources[entry["path"]]
                )
                dest: int | None = None
                try:
                    dest = os.open(
                        parts[-1],
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=parent,
                    )
                    _copy_cache_file(src, dest)
                    os.fchmod(dest, _CACHE_FILE_MODE)
                    os.fsync(dest)
                finally:
                    if dest is not None:
                        os.close(dest)
                    os.close(src)
                    os.fsync(parent)
                    os.close(parent)
            for entry in reversed(inspection.tree_manifest["entries"]):
                if entry["type"] != "directory":
                    continue
                child = _open_cache_directory_at(
                    cache_descriptor, PurePosixPath(entry["path"]).parts
                )
                try:
                    os.fchmod(child, _CACHE_DIRECTORY_MODE)
                    os.fsync(child)
                finally:
                    os.close(child)
            os.fchmod(cache_descriptor, _CACHE_DIRECTORY_MODE)
            os.fsync(cache_descriptor)
        finally:
            os.close(cache_descriptor)
            os.close(source_descriptor)
            source_descriptor = None
        original_after = _scan_private_pdf_model_cache(inspection.root)
        if (
            original_after.root_identity != inspection.root_identity
            or original_after.tree_manifest != inspection.tree_manifest
        ):
            raise OSError("original cache drifted during sealing")
        sealed_inspection = _scan_private_pdf_model_cache(
            cache_root, require_sealed_modes=True
        )
        if sealed_inspection.tree_manifest != inspection.tree_manifest:
            raise OSError("sealed cache differs")
        _rename_noreplace_at(
            state_descriptor, temporary_name, state_descriptor, final_name
        )
        owned_name = final_name
        os.fsync(state_descriptor)
        sealed_root = final / "huggingface"
        root_info = os.stat(sealed_root, follow_symlinks=False)
        sealed = PrivatePdfSealedModelCache(
            root=sealed_root,
            container=final,
            root_identity=(root_info.st_dev, root_info.st_ino),
            tree_manifest=_strict_copy(inspection.tree_manifest),
            tree_digest=canonical_digest(inspection.tree_manifest),
            file_count=inspection.tree_manifest["file_count"],
            total_bytes=inspection.tree_manifest["total_bytes"],
        )
        _validate_sealed_private_pdf_model_cache(sealed)
        return sealed
    except BaseException:
        if owned_name is not None and owned_identity is not None:
            _remove_owned_cache_tree_at(
                state_descriptor, owned_name, owned_identity
            )
        raise
    finally:
        if source_descriptor is not None:
            os.close(source_descriptor)
        os.close(state_descriptor)


def _validate_sealed_private_pdf_model_cache(
    sealed: PrivatePdfSealedModelCache,
) -> PrivatePdfSealedModelCache:
    try:
        if not isinstance(sealed, PrivatePdfSealedModelCache):
            raise OSError("sealed cache type is invalid")
        root, _ = ensure_contained(
            sealed.root, sealed.container, "private PDF sealed model cache"
        )
        info = os.lstat(root)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != _CACHE_DIRECTORY_MODE
            or (info.st_dev, info.st_ino) != sealed.root_identity
        ):
            raise OSError("sealed cache root drifted")
        inspected = _scan_private_pdf_model_cache(
            root, require_sealed_modes=True
        )
        manifest = _strict_copy(sealed.tree_manifest)
        if (
            inspected.tree_manifest != manifest
            or canonical_digest(manifest) != sealed.tree_digest
            or manifest.get("file_count") != sealed.file_count
            or manifest.get("total_bytes") != sealed.total_bytes
        ):
            raise OSError("sealed cache tree drifted")
        return sealed
    except (ContractError, OSError, RuntimeError, ValueError) as exc:
        raise _fail("private PDF sealed model cache drifted") from exc


def _remove_sealed_private_pdf_model_cache(
    sealed: PrivatePdfSealedModelCache,
) -> None:
    if not re.fullmatch(r"sealed-cache-[0-9a-f]{24}", sealed.container.name):
        raise _fail("private PDF sealed model cache drifted")
    parent_descriptor = _open_directory(
        sealed.container.parent, repository_root(), create=False
    )
    try:
        before = os.stat(
            sealed.container.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise _fail("private PDF sealed model cache drifted")
        identity = (before.st_dev, before.st_ino)
        _validate_sealed_private_pdf_model_cache(sealed)
        if not _remove_owned_cache_tree_at(
            parent_descriptor, sealed.container.name, identity
        ):
            raise _fail("private PDF sealed model cache drifted")
    except OSError as exc:
        raise _fail("private PDF sealed model cache drifted") from exc
    finally:
        os.close(parent_descriptor)


@contextmanager
def _private_pdf_sealed_cache_environment(sealed: PrivatePdfSealedModelCache):
    """Expose only one validated sealed cache for adapter construction/use."""

    _validate_sealed_private_pdf_model_cache(sealed)
    previous = os.environ.get("HF_HOME")
    os.environ["HF_HOME"] = str(sealed.root)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HF_HOME", None)
        else:
            os.environ["HF_HOME"] = previous
        _validate_sealed_private_pdf_model_cache(sealed)


def _open_private_lock(private_descriptor: int, *, exclusive: bool) -> int:
    descriptor = os.open(
        _LOCK_NAME,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=private_descriptor,
    )
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("private PDF lock binding is invalid")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("private PDF lock acquisition timed out")
                time.sleep(0.01)
        opened = os.fstat(descriptor)
        public = os.stat(
            _LOCK_NAME, dir_fd=private_descriptor, follow_symlinks=False
        )
        if (
            not stat.S_ISREG(public.st_mode)
            or public.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (public.st_dev, public.st_ino)
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("private PDF lock binding changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _rename_noreplace_at(
    source_descriptor: int,
    source_name: str,
    destination_descriptor: int,
    destination_name: str,
) -> None:
    """Atomically rename one held-descriptor binding without replacement."""

    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_descriptor,
        os.fsencode(source_name),
        destination_descriptor,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _opaque_quarantine_name(directory_descriptor: int, prefix: str) -> str:
    for _ in range(32):
        name = f".{prefix}-{secrets.token_hex(16)}.quarantine"
        try:
            os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return name
    raise OSError("private PDF quarantine collision")


def _quarantine_verified_file_at(
    directory_descriptor: int,
    name: str,
    binding: os.stat_result,
    expected_body: bytes,
    maximum: int,
    *,
    retained_descriptor: int | None = None,
    retained_name: str | None = None,
) -> None:
    current = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if _stable_metadata(current) != _stable_metadata(binding):
        raise OSError("private PDF cleanup binding drifted")
    destination_descriptor = (
        directory_descriptor if retained_descriptor is None else retained_descriptor
    )
    quarantine = retained_name or _opaque_quarantine_name(
        destination_descriptor, "delete"
    )
    _rename_noreplace_at(
        directory_descriptor,
        name,
        destination_descriptor,
        quarantine,
    )
    if destination_descriptor != directory_descriptor:
        os.fsync(directory_descriptor)
    os.fsync(destination_descriptor)
    moved = os.stat(
        quarantine, dir_fd=destination_descriptor, follow_symlinks=False
    )
    if _renamed_file_metadata(moved) != _renamed_file_metadata(binding):
        raise OSError("private PDF cleanup quarantine binding drifted")
    if _read_file_at(destination_descriptor, quarantine, maximum) != expected_body:
        raise OSError("private PDF cleanup quarantine digest drifted")
    final = os.stat(
        quarantine, dir_fd=destination_descriptor, follow_symlinks=False
    )
    if _renamed_file_metadata(final) != _renamed_file_metadata(binding):
        raise OSError("private PDF cleanup tombstone binding drifted")


def _open_source(path: Path) -> tuple[int, int, os.stat_result]:
    if not path.is_absolute() or len(path.parts) < 2:
        raise OSError("invalid reviewed source")
    parent = Path(*path.parts[:-1])
    parent_descriptor = _open_directory(parent, Path(os.path.sep), create=False)
    descriptor: int | None = None
    try:
        before = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > MAX_PRIVATE_PDF_BYTES
        ):
            raise OSError("invalid reviewed source")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(descriptor)
        if _stable_metadata(before) != _stable_metadata(opened):
            raise OSError("reviewed source changed")
        transferred = descriptor
        descriptor = None
        return transferred, parent_descriptor, before
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)
        raise


def _stable_metadata(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_nlink,
    )


def _renamed_file_metadata(
    value: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_nlink,
        stat.S_IFMT(value.st_mode),
    )


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    remaining = maximum + 1
    while remaining:
        chunk = os.read(descriptor, min(65536, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    body = b"".join(chunks)
    if len(body) > maximum:
        raise OSError("bounded input exceeded")
    return body


def _read_reviewed_source(path: Path) -> bytes:
    descriptor, parent_descriptor, before = _open_source(path)
    try:
        body = _read_bounded(descriptor, MAX_PRIVATE_PDF_BYTES)
        after = os.fstat(descriptor)
        public = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _stable_metadata(before) != _stable_metadata(after) or _stable_metadata(before) != _stable_metadata(public):
            raise OSError("reviewed source changed")
        if not body.startswith(b"%PDF-"):
            raise OSError("invalid PDF signature")
        return body
    finally:
        os.close(descriptor)
        os.close(parent_descriptor)


def _run_pdf_tool(arguments: Sequence[str], body: bytes) -> bytes:
    if list(arguments) not in (["pdfinfo", "-"], ["pdftotext", "-", "-"]):
        raise OSError("invalid PDF verification command")
    environment = {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
    }
    with tempfile.TemporaryFile() as input_file:
        input_file.write(body)
        input_file.seek(0)
        process = subprocess.Popen(
            list(arguments),
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=environment,
            close_fds=True,
            start_new_session=True,
        )
        try:
            if process.stdout is None:
                raise OSError("PDF verification command failed")
            selector = selectors.DefaultSelector()
            try:
                selector.register(process.stdout, selectors.EVENT_READ)
                output = bytearray()
                deadline = time.monotonic() + _TOOL_TIMEOUT_SECONDS
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(
                            list(arguments), _TOOL_TIMEOUT_SECONDS
                        )
                    events = selector.select(remaining)
                    if not events:
                        raise subprocess.TimeoutExpired(
                            list(arguments), _TOOL_TIMEOUT_SECONDS
                        )
                    chunk = os.read(
                        process.stdout.fileno(),
                        min(65536, _MAX_TOOL_OUTPUT_BYTES + 1 - len(output)),
                    )
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > _MAX_TOOL_OUTPUT_BYTES:
                        raise OSError("PDF verification command failed")
                return_code = process.wait(
                    timeout=max(0.001, deadline - time.monotonic())
                )
                if return_code != 0:
                    raise OSError("PDF verification command failed")
                return bytes(output)
            finally:
                selector.close()
        except BaseException as exc:
            _terminate_process_group(process)
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, OSError) and str(exc) == "PDF verification command failed":
                raise
            raise OSError("PDF verification command failed") from exc
        finally:
            if process.stdout is not None:
                process.stdout.close()


def _bound_process_group(process: subprocess.Popen[bytes]) -> int | None:
    """Return only the exact child-led process group created by this module."""

    pid = getattr(process, "pid", None)
    if (
        type(pid) is not int
        or pid <= 1
        or pid == os.getpid()
        or pid == os.getpgrp()
    ):
        raise OSError("process group identity is invalid")
    try:
        group = os.getpgid(pid)
    except ProcessLookupError:
        return None
    if group != pid or group <= 1 or group == os.getpgrp():
        raise OSError("process group identity is invalid")
    return group


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    group = _bound_process_group(process)
    if group is None:
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.terminate()
    try:
        process.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        if process.poll() is None:
            process.kill()
    if process.poll() is None:
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired as exc:
            raise OSError("process group could not be terminated") from exc


def _verify_pdf(body: bytes) -> None:
    information = _run_pdf_tool(("pdfinfo", "-"), body)
    if len(information) > _MAX_TOOL_OUTPUT_BYTES:
        raise OSError("PDF metadata output exceeded")
    text = information.decode("utf-8", errors="replace")
    page_match = re.search(r"(?m)^Pages:\s*([0-9]+)\s*$", text)
    encrypted_match = re.search(r"(?mi)^Encrypted:\s*(yes|no)\s*$", text)
    if page_match is None or int(page_match.group(1)) < 1:
        raise OSError("PDF pages are invalid")
    if encrypted_match is None or encrypted_match.group(1).lower() != "no":
        raise OSError("PDF encryption state is invalid")
    extracted = _run_pdf_tool(("pdftotext", "-", "-"), body)
    if len(extracted) > _MAX_TOOL_OUTPUT_BYTES or not extracted.strip():
        raise OSError("PDF has no readable text")


def _qualify_domain_pdf(body: bytes) -> PdfQualification:
    information = _run_pdf_tool(("pdfinfo", "-"), body)
    text = information.decode("utf-8", errors="replace")
    page_match = re.search(r"(?m)^Pages:\s*([0-9]+)\s*$", text)
    encrypted_match = re.search(r"(?mi)^Encrypted:\s*(yes|no)\s*$", text)
    extracted = _run_pdf_tool(("pdftotext", "-", "-"), body)
    if page_match is None or encrypted_match is None:
        raise OSError("representative PDF qualification is invalid")
    return PdfQualification(
        page_count=int(page_match.group(1)),
        encrypted=encrypted_match.group(1).lower() == "yes",
        readable_text=bool(extracted.strip()),
    )


def _read_file_at(directory_descriptor: int, name: str, maximum: int) -> bytes:
    before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum
    ):
        raise OSError("invalid private PDF state")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if _stable_metadata(before) != _stable_metadata(opened):
            raise OSError("private PDF state changed")
        body = _read_bounded(descriptor, maximum)
        after = os.fstat(descriptor)
        public = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if _stable_metadata(opened) != _stable_metadata(after) or _stable_metadata(opened) != _stable_metadata(public):
            raise OSError("private PDF state changed")
        return body
    finally:
        os.close(descriptor)


def _hash_file_at(
    directory_descriptor: int, name: str, maximum: int
) -> tuple[os.stat_result, int, str]:
    before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size < 0
        or before.st_size > maximum
    ):
        raise OSError("invalid private PDF state")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=directory_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if _stable_metadata(before) != _stable_metadata(opened):
            raise OSError("private PDF state changed")
        digest, size = _hash_cache_file_descriptor(descriptor, maximum)
        after = os.fstat(descriptor)
        public = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            _stable_metadata(opened) != _stable_metadata(after)
            or _stable_metadata(opened) != _stable_metadata(public)
            or size != opened.st_size
        ):
            raise OSError("private PDF state changed")
        return opened, size, digest
    finally:
        os.close(descriptor)


def _unlink_verified_digest_at(
    directory_descriptor: int,
    name: str,
    expected_size: int,
    expected_digest: str,
    maximum: int,
) -> None:
    before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    opened, size, digest = _hash_file_at(directory_descriptor, name, maximum)
    after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        size != expected_size
        or digest != expected_digest
        or _stable_metadata(before) != _stable_metadata(opened)
        or _stable_metadata(before) != _stable_metadata(after)
    ):
        raise OSError("private PDF reclaim digest drifted")
    os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)


def _persist_bytes_at(directory_descriptor: int, name: str, body: bytes) -> None:
    if "/" in name or name in {"", ".", ".."}:
        raise OSError("invalid private PDF state name")
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise OSError("private PDF state collision")
    temporary = f".{name}-{secrets.token_hex(16)}.tmp"
    descriptor: int | None = None
    identity: tuple[int, int] | None = None
    publication_may_exist = False
    succeeded = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("invalid private PDF temporary")
        identity = (info.st_dev, info.st_ino)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short private PDF write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current = os.stat(temporary, dir_fd=directory_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            raise OSError("private PDF temporary changed")
        publication_may_exist = True
        os.link(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        os.fsync(directory_descriptor)
        os.unlink(temporary, dir_fd=directory_descriptor)
        temporary = ""
        os.fsync(directory_descriptor)
        if _read_file_at(directory_descriptor, name, max(1, len(body))) != body:
            raise OSError("private PDF persisted bytes drifted")
        succeeded = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary:
            try:
                current = os.stat(temporary, dir_fd=directory_descriptor, follow_symlinks=False)
                if stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == identity:
                    os.unlink(temporary, dir_fd=directory_descriptor)
            except FileNotFoundError:
                pass
        if publication_may_exist and not succeeded and identity is not None:
            try:
                published = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
                if (
                    stat.S_ISREG(published.st_mode)
                    and (published.st_dev, published.st_ino) == identity
                ):
                    rollback = _opaque_quarantine_name(
                        directory_descriptor, "rollback"
                    )
                    _rename_noreplace_at(
                        directory_descriptor,
                        name,
                        directory_descriptor,
                        rollback,
                    )
                    moved = os.stat(
                        rollback,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    if (
                        stat.S_ISREG(moved.st_mode)
                        and (moved.st_dev, moved.st_ino) == identity
                    ):
                        try:
                            rollback_matches = _read_file_at(
                                directory_descriptor,
                                rollback,
                                max(1, len(body)),
                            ) == body
                            # No unlink follows: POSIX cannot condition unlink on this result.
                            del rollback_matches
                        except OSError:
                            pass
                    os.fsync(directory_descriptor)
            except FileNotFoundError:
                pass


def _discard_preparation_stage(
    private_descriptor: int,
    stage_name: str,
    stage_identity: tuple[int, int],
    corpus_descriptor: int,
    input_descriptor: int,
    prepared: Sequence[tuple[SeedDocument, bytes, str]],
    manifest: Mapping[str, Any],
) -> None:
    """Reclaim an exact unpublished tree under the cooperative held-lock boundary."""

    try:
        public = os.stat(
            stage_name, dir_fd=private_descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        return
    if (
        not stat.S_ISDIR(public.st_mode)
        or (public.st_dev, public.st_ino) != stage_identity
    ):
        return
    try:
        for seed, body, _ in prepared:
            name = f"{seed.item_id}.pdf"
            if _binding_exists_at(input_descriptor, name):
                _unlink_verified_at(
                    input_descriptor, name, body, MAX_PRIVATE_PDF_BYTES
                )
        if _binding_exists_at(corpus_descriptor, _MANIFEST_NAME):
            _unlink_verified_at(
                corpus_descriptor,
                _MANIFEST_NAME,
                _manifest_body(manifest),
                _MAX_MANIFEST_BYTES,
            )
        if os.listdir(input_descriptor) or set(os.listdir(corpus_descriptor)) != {"input"}:
            raise OSError("private PDF preparation staging drifted")
        os.rmdir("input", dir_fd=corpus_descriptor)
        os.fsync(corpus_descriptor)
        os.rmdir(stage_name, dir_fd=private_descriptor)
        os.fsync(private_descriptor)
    except BaseException:
        if (
            _binding_exists_at(private_descriptor, stage_name)
            and not _binding_exists_at(private_descriptor, _PREPARATION_RECOVERY)
        ):
            _rename_noreplace_at(
                private_descriptor,
                stage_name,
                private_descriptor,
                _PREPARATION_RECOVERY,
            )
            os.fsync(private_descriptor)
        raise


def _unlink_verified_at(
    directory_descriptor: int,
    name: str,
    expected_body: bytes,
    maximum: int,
) -> None:
    """Reclaim a stable owned name within the documented cooperative boundary."""

    before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if _read_file_at(directory_descriptor, name, maximum) != expected_body:
        raise OSError("private PDF reclaim digest drifted")
    after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if _stable_metadata(before) != _stable_metadata(after):
        raise OSError("private PDF reclaim binding drifted")
    # Linux cannot condition unlink on inode; the held-lock same-UID boundary applies.
    os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)


def _manifest_body(
    value: Mapping[str, Any], maximum: int = _MAX_MANIFEST_BYTES
) -> bytes:
    body = (json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(body) > maximum:
        raise OSError("private PDF manifest exceeded")
    return body


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise _fail(f"{label} must be an exact sha256 digest")
    return value


def _require_relative_pdf(value: Any, label: str) -> str:
    if not isinstance(value, str) or _FILE_RE.fullmatch(value) is None:
        raise _fail(f"{label} must be a private-root-relative PDF locator")
    path = PurePosixPath(value)
    if path.name != value.split("/")[-1] or path.name == ".pdf":
        raise _fail(f"{label} must be a concrete PDF filename")
    return value


def _require_closed_text_list(value: Any, label: str, *, max_items: int) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= max_items:
        raise _fail(f"{label} must be a bounded non-empty list")
    items: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = require_text(item, f"{label}[{index}]", maximum=256)
        if text in seen:
            raise _fail(f"{label} must not contain duplicates")
        seen.add(text)
        items.append(text)
    return items


def _require_block_types(value: Any, label: str) -> list[str]:
    items = _require_closed_text_list(value, label, max_items=len(_BLOCK_TYPES))
    if any(item not in _BLOCK_TYPES for item in items):
        raise _fail(f"{label} contains an unknown block type")
    return items


def _require_metric_score(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise _fail(f"{label} must be a number")
    score = reject_nonfinite(value, label)
    if score < 0 or score > 1:
        raise _fail(f"{label} must be between 0 and 1")
    return float(score)


def _require_summary(value: Any, label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise _fail(f"{label} must be an object")
    require_exact_keys(value, ("minimum", "maximum", "mean"), label)
    for field in ("minimum", "maximum", "mean"):
        if isinstance(value[field], bool):
            raise _fail(f"{label}.{field} must be a number")
    minimum = reject_nonfinite(value["minimum"], f"{label}.minimum")
    maximum = reject_nonfinite(value["maximum"], f"{label}.maximum")
    mean = reject_nonfinite(value["mean"], f"{label}.mean")
    if minimum < 0 or maximum < 0 or mean < 0 or minimum > mean or mean > maximum:
        raise _fail(f"{label} arithmetic is invalid")
    return {"minimum": float(minimum), "maximum": float(maximum), "mean": float(mean)}


def _require_expected_item_ids(
    value: Sequence[str] | None, label: str
) -> list[str]:
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 20:
        raise _fail(f"{label} must be a bounded list or tuple of item identifiers")
    seen: set[str] = set()
    result: list[str] = []
    for index, item in enumerate(value):
        identifier = require_identifier(item, f"{label}[{index}]")
        if identifier in seen:
            raise _fail(f"{label} must not contain duplicates")
        seen.add(identifier)
        result.append(identifier)
    return result


def _require_success_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    expected = {
        "item_id",
        "source_digest",
        "status",
        "candidate_id",
        "candidate_digest",
        "provenance_digest",
        "review_status",
    }
    if set(value) != expected:
        return None
    item_id = require_identifier(value["item_id"], "results.item_id")
    source_digest = _require_digest(value["source_digest"], "results.source_digest")
    if value["status"] not in {"created", "unchanged"}:
        raise _fail("results.status must be created or unchanged")
    candidate_id = require_identifier(value["candidate_id"], "results.candidate_id")
    candidate_digest = _require_digest(value["candidate_digest"], "results.candidate_digest")
    provenance_digest = _require_digest(value["provenance_digest"], "results.provenance_digest")
    review_status = value["review_status"]
    if review_status not in {"unreviewed", "accepted", "rejected"}:
        raise _fail("results.review_status must be a closed candidate review status")
    return {
        "item_id": item_id,
        "source_digest": source_digest,
        "status": value["status"],
        "candidate_id": candidate_id,
        "candidate_digest": candidate_digest,
        "provenance_digest": provenance_digest,
        "review_status": review_status,
    }


def _require_rejected_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    expected = {"item_id", "source_digest", "status", "error_code"}
    if set(value) != expected:
        return None
    item_id = require_identifier(value["item_id"], "results.item_id")
    source_digest = _require_digest(value["source_digest"], "results.source_digest")
    if value["status"] != "rejected":
        raise _fail("results.status must be rejected")
    if value["error_code"] not in _FAILURE_CODES:
        raise _fail("results.error_code must be closed and path-free")
    return {
        "item_id": item_id,
        "source_digest": source_digest,
        "status": "rejected",
        "error_code": value["error_code"],
    }


def validate_private_pdf_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached strict runtime-only corpus manifest."""

    try:
        if not isinstance(value, Mapping):
            raise _fail("private PDF manifest must be an object")
        manifest = _strict_copy(value)
        require_exact_keys(
            manifest,
            (
                "schema_version",
                "corpus_id",
                "parser_id",
                "parser_version",
                "ocr_enabled",
                "documents",
                "provider_calls",
                "promotion_authority",
                "claims_authority_advance",
            ),
            "private PDF manifest",
        )
        if manifest["schema_version"] != "ao.lore.private-pdf-uat-manifest.v0.1":
            raise _fail("private PDF manifest schema_version is invalid")
        require_identifier(manifest["corpus_id"], "private PDF manifest corpus_id")
        if manifest["parser_id"] != "docling" or manifest["parser_version"] != "2.118.1":
            raise _fail("private PDF manifest parser binding is invalid")
        for field in (
            "ocr_enabled",
            "provider_calls",
            "promotion_authority",
            "claims_authority_advance",
        ):
            if require_bool(manifest[field], f"private PDF manifest {field}") is not False:
                raise _fail(f"private PDF manifest {field} must be false")
        documents = manifest["documents"]
        if not isinstance(documents, list) or not 1 <= len(documents) <= 20:
            raise _fail("private PDF manifest documents are invalid")
        item_ids: set[str] = set()
        files: set[str] = set()
        anchors: set[str] = set()
        required_type_sets: set[tuple[str, ...]] = set()
        validated: list[dict[str, Any]] = []
        for index, document in enumerate(documents):
            if not isinstance(document, Mapping):
                raise _fail(f"private PDF manifest documents[{index}] must be an object")
            require_exact_keys(
                document,
                ("item_id", "file", "source_digest", "expected_text", "required_block_types"),
                f"private PDF manifest documents[{index}]",
            )
            item_id = require_identifier(document["item_id"], f"private PDF manifest documents[{index}].item_id")
            file_value = _require_relative_pdf(document["file"], f"private PDF manifest documents[{index}].file")
            if item_id in item_ids or file_value in files:
                raise _fail("private PDF manifest contains duplicate keyed identities")
            item_ids.add(item_id)
            files.add(file_value)
            expected_text = _require_closed_text_list(
                document["expected_text"],
                f"private PDF manifest documents[{index}].expected_text",
                max_items=32,
            )
            for anchor in expected_text:
                if anchor in anchors:
                    raise _fail("private PDF manifest contains duplicate expected_text anchors across documents")
                anchors.add(anchor)
            required_block_types = _require_block_types(
                document["required_block_types"],
                f"private PDF manifest documents[{index}].required_block_types",
            )
            normalized_type_set = tuple(sorted(required_block_types))
            if normalized_type_set in required_type_sets:
                raise _fail("private PDF manifest contains duplicate required_block_types sets across documents")
            required_type_sets.add(normalized_type_set)
            validated.append(
                {
                    "item_id": item_id,
                    "file": file_value,
                    "source_digest": _require_digest(
                        document["source_digest"],
                        f"private PDF manifest documents[{index}].source_digest",
                    ),
                    "expected_text": expected_text,
                    "required_block_types": required_block_types,
                }
            )
        manifest["documents"] = validated
        return manifest
    except ContractError as exc:
        if isinstance(exc, PrivatePdfUatError):
            raise
        raise _fail(str(exc)) from exc


def _load_private_pdf_manifest_at(
    corpus_id: str, corpus_descriptor: int, input_descriptor: int
) -> dict[str, Any]:
    if set(os.listdir(corpus_descriptor)) != {_MANIFEST_NAME, "input"}:
        raise OSError("private PDF corpus entries drifted")
    raw = _read_file_at(corpus_descriptor, _MANIFEST_NAME, _MAX_MANIFEST_BYTES)
    manifest = validate_private_pdf_manifest(parse_strict_json(raw, "private PDF manifest"))
    if manifest["corpus_id"] != corpus_id:
        raise OSError("private PDF corpus identity drifted")
    expected_names: list[str] = []
    for document in manifest["documents"]:
        relative = PurePosixPath(document["file"])
        if len(relative.parts) != 2 or relative.parts[0] != "input":
            raise OSError("private PDF input layout drifted")
        expected_names.append(relative.name)
    if set(os.listdir(input_descriptor)) != set(expected_names):
        raise OSError("private PDF input entries drifted")
    for document, name in zip(manifest["documents"], expected_names):
        body = _read_file_at(input_descriptor, name, MAX_PRIVATE_PDF_BYTES)
        if not body.startswith(b"%PDF-"):
            raise OSError("private PDF signature drifted")
        if "sha256:" + hashlib.sha256(body).hexdigest() != document["source_digest"]:
            raise OSError("private PDF digest drifted")
        _verify_pdf(body)
    return manifest


def load_private_pdf_manifest(
    corpus_id: str = _CORPUS_ID, *, runtime_root: Path | None = None
) -> dict[str, Any]:
    """Load a digest-bound manifest through held private-runtime descriptors."""

    root_descriptor: int | None = None
    private_descriptor: int | None = None
    lock_descriptor: int | None = None
    corpus_descriptor: int | None = None
    input_descriptor: int | None = None
    try:
        require_identifier(corpus_id, "private PDF corpus identifier")
        _, root_descriptor = _open_runtime_root(runtime_root)
        private_descriptor = _open_relative_directory(
            root_descriptor, ("calibration", "private-pdf"), create=False
        )
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=False)
        corpus_descriptor = _open_relative_directory(
            private_descriptor, (corpus_id,), create=False
        )
        input_descriptor = _open_relative_directory(
            corpus_descriptor, ("input",), create=False
        )
        return _load_private_pdf_manifest_at(
            corpus_id, corpus_descriptor, input_descriptor
        )
    except Exception as exc:
        raise PrivatePdfUatError("private PDF seed manifest could not be loaded") from exc
    finally:
        for descriptor in (
            input_descriptor,
            corpus_descriptor,
            lock_descriptor,
            private_descriptor,
            root_descriptor,
        ):
            if descriptor is not None:
                os.close(descriptor)


def prepare_ubuntu_pdf_seed(*, runtime_root: Path | None = None) -> dict[str, Any]:
    """Copy the four reviewed public Ubuntu PDFs into ignored runtime state."""

    root_descriptor: int | None = None
    private_descriptor: int | None = None
    lock_descriptor: int | None = None
    corpus_descriptor: int | None = None
    input_descriptor: int | None = None
    stage_name = ""
    stage_identity: tuple[int, int] | None = None
    prepared: list[tuple[SeedDocument, bytes, str]] = []
    manifest: dict[str, Any] | None = None
    try:
        _, root_descriptor = _open_runtime_root(runtime_root)
        for seed in UBUNTU_PDF_SEED:
            body = _read_reviewed_source(seed.source)
            _verify_pdf(body)
            prepared.append((seed, body, "sha256:" + hashlib.sha256(body).hexdigest()))
        manifest = validate_private_pdf_manifest(
            {
                "schema_version": "ao.lore.private-pdf-uat-manifest.v0.1",
                "corpus_id": _CORPUS_ID,
                "parser_id": "docling",
                "parser_version": "2.118.1",
                "ocr_enabled": False,
                "documents": [
                    {
                        "item_id": seed.item_id,
                        "file": f"input/{seed.item_id}.pdf",
                        "source_digest": digest,
                        "expected_text": list(seed.expected_text),
                        "required_block_types": list(seed.required_block_types),
                    }
                    for seed, _, digest in prepared
                ],
                "provider_calls": False,
                "promotion_authority": False,
                "claims_authority_advance": False,
            }
        )
        private_descriptor = _open_relative_directory(
            root_descriptor, ("calibration", "private-pdf"), create=True
        )
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=True)
        if _binding_exists_at(private_descriptor, _PREPARATION_RECOVERY):
            raise OSError("private PDF preparation recovery is unresolved")
        if _binding_exists_at(private_descriptor, _RECLAIM_STAGING):
            raise OSError("private PDF reclamation marker staging is unresolved")
        if any(
            name.startswith(".rollback-") for name in os.listdir(private_descriptor)
        ):
            raise OSError("private PDF rollback artifact is unresolved")
        if _reclaim_names_at(private_descriptor):
            raise OSError("private PDF reclamation is unresolved")
        if _binding_exists_at(private_descriptor, _RECOVERY_STAGING):
            if _reclaim_recovery_staging_at(private_descriptor) != manifest:
                raise OSError("private PDF recovery staging manifest drifted")
        if _recovery_names_at(private_descriptor) or _binding_exists_at(
            private_descriptor, _CLEANUP_QUARANTINE
        ):
            raise OSError("private PDF cleanup recovery is unresolved")
        try:
            corpus_descriptor = _open_relative_directory(
                private_descriptor, (_CORPUS_ID,), create=False
            )
        except FileNotFoundError:
            pass
        else:
            input_descriptor = _open_relative_directory(
                corpus_descriptor, ("input",), create=False
            )
            if _load_private_pdf_manifest_at(
                _CORPUS_ID, corpus_descriptor, input_descriptor
            ) != manifest:
                raise OSError("private PDF manifest conflict")
            return manifest

        stage_name = f"ubuntu-pdf-seed-stage-{secrets.token_hex(16)}"
        os.mkdir(stage_name, 0o700, dir_fd=private_descriptor)
        corpus_descriptor = _open_relative_directory(
            private_descriptor, (stage_name,), create=False
        )
        stage_info = os.fstat(corpus_descriptor)
        stage_identity = (stage_info.st_dev, stage_info.st_ino)
        os.mkdir("input", 0o700, dir_fd=corpus_descriptor)
        input_descriptor = _open_relative_directory(
            corpus_descriptor, ("input",), create=False
        )
        for seed, body, _ in prepared:
            _persist_bytes_at(input_descriptor, f"{seed.item_id}.pdf", body)
        _persist_bytes_at(corpus_descriptor, _MANIFEST_NAME, _manifest_body(manifest))
        if _load_private_pdf_manifest_at(
            _CORPUS_ID, corpus_descriptor, input_descriptor
        ) != manifest:
            raise OSError("private PDF verified reopen drifted")
        os.fsync(input_descriptor)
        os.fsync(corpus_descriptor)
        _rename_noreplace_at(
            private_descriptor,
            stage_name,
            private_descriptor,
            _CORPUS_ID,
        )
        os.fsync(private_descriptor)
        canonical = os.stat(
            _CORPUS_ID, dir_fd=private_descriptor, follow_symlinks=False
        )
        if (canonical.st_dev, canonical.st_ino) != stage_identity:
            raise OSError("private PDF published corpus binding drifted")
        stage_name = ""
        return manifest
    except Exception as exc:
        raise PrivatePdfUatError("private PDF seed intake failed") from exc
    finally:
        if (
            stage_name
            and stage_identity is not None
            and private_descriptor is not None
            and corpus_descriptor is not None
            and input_descriptor is not None
            and manifest is not None
        ):
            try:
                canonical = os.stat(
                    _CORPUS_ID,
                    dir_fd=private_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                canonical = None
            if canonical is None or (canonical.st_dev, canonical.st_ino) != stage_identity:
                try:
                    _discard_preparation_stage(
                        private_descriptor,
                        stage_name,
                        stage_identity,
                        corpus_descriptor,
                        input_descriptor,
                        prepared,
                        manifest,
                    )
                except Exception:
                    pass
        for descriptor in (
            input_descriptor,
            corpus_descriptor,
            lock_descriptor,
            private_descriptor,
            root_descriptor,
        ):
            if descriptor is not None:
                os.close(descriptor)


def _domain_manifest(
    prepared: Sequence[PreparedDomainDocument],
) -> dict[str, Any]:
    return validate_private_pdf_manifest(
        {
            "schema_version": "ao.lore.private-pdf-uat-manifest.v0.1",
            "corpus_id": DOMAIN_CORPUS_ID,
            "parser_id": "docling",
            "parser_version": "2.118.1",
            "ocr_enabled": False,
            "documents": [
                {
                    "item_id": document.item_id,
                    "file": f"input/{document.item_id}.pdf",
                    "source_digest": document.derived_digest,
                    "expected_text": list(document.expected_text),
                    "required_block_types": list(document.required_block_types),
                }
                for document in prepared
            ],
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
    )


def _discard_domain_stage_at(
    private_descriptor: int,
    prepared: Sequence[PreparedDomainDocument],
    manifest: Mapping[str, Any],
    *,
    retain_on_failure: bool = False,
) -> None:
    stage_descriptor: int | None = None
    input_descriptor: int | None = None
    try:
        stage_descriptor = os.open(
            _DOMAIN_STAGE, _DIRECTORY_FLAGS, dir_fd=private_descriptor
        )
        entries = set(os.listdir(stage_descriptor))
        if not entries.issubset({_MANIFEST_NAME, "input"}):
            raise OSError("representative PDF preparation drifted")
        if "input" in entries:
            input_descriptor = os.open(
                "input", _DIRECTORY_FLAGS, dir_fd=stage_descriptor
            )
            expected = {f"{document.item_id}.pdf": document.body for document in prepared}
            if not set(os.listdir(input_descriptor)).issubset(expected):
                raise OSError("representative PDF preparation input drifted")
            for name, body in expected.items():
                if _binding_exists_at(input_descriptor, name):
                    _unlink_verified_at(
                        input_descriptor, name, body, MAX_PRIVATE_PDF_BYTES
                    )
            os.rmdir("input", dir_fd=stage_descriptor)
            os.fsync(stage_descriptor)
        if _binding_exists_at(stage_descriptor, _MANIFEST_NAME):
            _unlink_verified_at(
                stage_descriptor,
                _MANIFEST_NAME,
                _manifest_body(manifest),
                _MAX_MANIFEST_BYTES,
            )
        if os.listdir(stage_descriptor):
            raise OSError("representative PDF preparation remained")
        os.rmdir(_DOMAIN_STAGE, dir_fd=private_descriptor)
        os.fsync(private_descriptor)
    except BaseException:
        if (
            retain_on_failure
            and _binding_exists_at(private_descriptor, _DOMAIN_STAGE)
            and not _binding_exists_at(
                private_descriptor, _DOMAIN_PREPARATION_RECOVERY
            )
        ):
            _rename_noreplace_at(
                private_descriptor,
                _DOMAIN_STAGE,
                private_descriptor,
                _DOMAIN_PREPARATION_RECOVERY,
            )
            os.fsync(private_descriptor)
        raise
    finally:
        if input_descriptor is not None:
            os.close(input_descriptor)
        if stage_descriptor is not None:
            os.close(stage_descriptor)


def prepare_representative_pdf_corpus(
    review: ReviewedDomainCorpus,
    source_root: Path,
    *,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Prepare one exact reviewed representative-domain corpus."""

    descriptors: list[int] = []
    prepared: tuple[PreparedDomainDocument, ...] = ()
    manifest: dict[str, Any] | None = None
    published = False
    stage_owned = False
    try:
        prepared = load_reviewed_domain_sources(
            Path(source_root), review, qualify=_qualify_domain_pdf
        )
        manifest = _domain_manifest(prepared)
        _, root_descriptor = _open_runtime_root(runtime_root)
        descriptors.append(root_descriptor)
        private_descriptor = _open_relative_directory(
            root_descriptor, ("calibration", "private-pdf"), create=True
        )
        descriptors.append(private_descriptor)
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=True)
        descriptors.append(lock_descriptor)
        spec = _DOMAIN_CORPUS_SPEC
        if _binding_exists_at(private_descriptor, spec.preparation_recovery):
            raise OSError("representative PDF preparation recovery is unresolved")
        if _binding_exists_at(private_descriptor, spec.reclaim_staging):
            raise OSError("representative PDF reclamation marker staging is unresolved")
        if _reclaim_names_at(private_descriptor, spec):
            raise OSError("representative PDF reclamation is unresolved")
        if _binding_exists_at(private_descriptor, spec.recovery_staging):
            if _reclaim_recovery_staging_at(private_descriptor, spec) != manifest:
                raise OSError("representative PDF recovery staging manifest drifted")
        if _recovery_names_at(private_descriptor, spec) or _binding_exists_at(
            private_descriptor, spec.cleanup_quarantine
        ):
            raise OSError("representative PDF cleanup recovery is unresolved")
        if _binding_exists_at(private_descriptor, _DOMAIN_STAGE):
            _discard_domain_stage_at(private_descriptor, prepared, manifest)
        try:
            corpus_descriptor = os.open(
                DOMAIN_CORPUS_ID, _DIRECTORY_FLAGS, dir_fd=private_descriptor
            )
        except FileNotFoundError:
            pass
        else:
            descriptors.append(corpus_descriptor)
            input_descriptor = os.open(
                "input", _DIRECTORY_FLAGS, dir_fd=corpus_descriptor
            )
            descriptors.append(input_descriptor)
            if _load_private_pdf_manifest_at(
                DOMAIN_CORPUS_ID, corpus_descriptor, input_descriptor
            ) != manifest:
                raise OSError("representative PDF manifest conflict")
            return manifest
        os.mkdir(_DOMAIN_STAGE, 0o700, dir_fd=private_descriptor)
        stage_owned = True
        stage_descriptor = os.open(
            _DOMAIN_STAGE, _DIRECTORY_FLAGS, dir_fd=private_descriptor
        )
        descriptors.append(stage_descriptor)
        stage_info = os.fstat(stage_descriptor)
        os.mkdir("input", 0o700, dir_fd=stage_descriptor)
        input_descriptor = os.open(
            "input", _DIRECTORY_FLAGS, dir_fd=stage_descriptor
        )
        descriptors.append(input_descriptor)
        for document in prepared:
            _persist_bytes_at(
                input_descriptor, f"{document.item_id}.pdf", document.body
            )
        _persist_bytes_at(stage_descriptor, _MANIFEST_NAME, _manifest_body(manifest))
        if _load_private_pdf_manifest_at(
            DOMAIN_CORPUS_ID, stage_descriptor, input_descriptor
        ) != manifest:
            raise OSError("representative PDF verified reopen drifted")
        os.fsync(input_descriptor)
        os.fsync(stage_descriptor)
        _rename_noreplace_at(
            private_descriptor,
            _DOMAIN_STAGE,
            private_descriptor,
            DOMAIN_CORPUS_ID,
        )
        os.fsync(private_descriptor)
        canonical = os.stat(
            DOMAIN_CORPUS_ID, dir_fd=private_descriptor, follow_symlinks=False
        )
        if (canonical.st_dev, canonical.st_ino) != (
            stage_info.st_dev,
            stage_info.st_ino,
        ):
            raise OSError("representative PDF publication drifted")
        published = True
        return manifest
    except Exception as exc:
        raise PrivatePdfUatError("representative PDF corpus intake failed") from exc
    finally:
        if (
            not published
            and stage_owned
            and prepared
            and manifest is not None
            and "private_descriptor" in locals()
            and _binding_exists_at(private_descriptor, _DOMAIN_STAGE)
        ):
            try:
                _discard_domain_stage_at(
                    private_descriptor,
                    prepared,
                    manifest,
                    retain_on_failure=True,
                )
            except Exception:
                pass
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _binding_exists_at(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _recovery_names_at(
    private_descriptor: int, spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC
) -> list[str]:
    names = [
        name
        for name in os.listdir(private_descriptor)
        if name.startswith(spec.recovery_prefix) and name != spec.recovery_staging
    ]
    for name in names:
        suffix = name.removeprefix(spec.recovery_prefix)
        if re.fullmatch(r"[0-9a-f]{64}", suffix) is None:
            raise OSError("private PDF cleanup recovery name is invalid")
    if len(names) > 1:
        raise OSError("multiple private PDF cleanup recoveries exist")
    return names


def _reclaim_names_at(
    private_descriptor: int, spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC
) -> list[str]:
    names = [
        name
        for name in os.listdir(private_descriptor)
        if name.startswith(spec.reclaim_prefix) and name != spec.reclaim_staging
    ]
    for name in names:
        if re.fullmatch(
            r"[0-9a-f]{64}", name.removeprefix(spec.reclaim_prefix)
        ) is None:
            raise OSError("private PDF reclamation marker name is invalid")
    if len(names) > 1:
        raise OSError("multiple private PDF reclamation markers exist")
    return names


def _recovery_value(
    manifest: Mapping[str, Any],
    spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC,
) -> dict[str, Any]:
    manifest_copy = json.loads(json.dumps(manifest, allow_nan=False))
    manifest_digest = hashlib.sha256(_manifest_body(manifest)).hexdigest()
    entries = [
        {
            "source": document["file"],
            "bundle": f".delete-{document['item_id']}.pdf",
            "digest": document["source_digest"],
        }
        for document in manifest["documents"]
    ]
    entries.append(
        {
            "source": _MANIFEST_NAME,
            "bundle": ".delete-manifest.json",
            "digest": "sha256:" + manifest_digest,
        }
    )
    return {
        "schema_version": _RECOVERY_SCHEMA_VERSION,
        "corpus_id": spec.corpus_id,
        "manifest_digest": "sha256:" + manifest_digest,
        "manifest": manifest_copy,
        "entries": entries,
    }


def _recovery_body(value: Mapping[str, Any]) -> bytes:
    body = (
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    if len(body) > _MAX_MANIFEST_BYTES:
        raise OSError("private PDF cleanup recovery exceeded")
    return body


def _validate_recovery_value(
    value: Any,
    recovery_name: str,
    spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "corpus_id",
        "manifest_digest",
        "manifest",
        "entries",
    }:
        raise OSError("private PDF cleanup recovery contract drifted")
    if value["schema_version"] != _RECOVERY_SCHEMA_VERSION or value["corpus_id"] != spec.corpus_id:
        raise OSError("private PDF cleanup recovery identity drifted")
    manifest = validate_private_pdf_manifest(value["manifest"])
    expected = _recovery_value(manifest, spec)
    if value != expected:
        raise OSError("private PDF cleanup recovery mapping drifted")
    expected_name = spec.recovery_prefix + expected["manifest_digest"].removeprefix("sha256:")
    if recovery_name != expected_name:
        raise OSError("private PDF cleanup recovery digest drifted")
    return expected


def _open_recovery_at(
    private_descriptor: int,
    recovery_name: str,
    spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC,
) -> tuple[int, int, dict[str, Any]]:
    recovery_descriptor = os.open(
        recovery_name, _DIRECTORY_FLAGS, dir_fd=private_descriptor
    )
    files_descriptor: int | None = None
    try:
        if set(os.listdir(recovery_descriptor)) != {_RECOVERY_SCHEMA_NAME, "files"}:
            raise OSError("private PDF cleanup recovery entries drifted")
        files_descriptor = os.open(
            "files", _DIRECTORY_FLAGS, dir_fd=recovery_descriptor
        )
        raw = _read_file_at(
            recovery_descriptor, _RECOVERY_SCHEMA_NAME, _MAX_MANIFEST_BYTES
        )
        value = _validate_recovery_value(
            parse_strict_json(raw, "private PDF cleanup recovery"), recovery_name, spec
        )
        expected_files = {entry["bundle"] for entry in value["entries"]}
        if not set(os.listdir(files_descriptor)).issubset(expected_files):
            raise OSError("private PDF cleanup recovery files drifted")
        return recovery_descriptor, files_descriptor, value
    except BaseException:
        if files_descriptor is not None:
            os.close(files_descriptor)
        os.close(recovery_descriptor)
        raise


def _load_reclaim_marker_at(
    private_descriptor: int,
    marker_name: str,
    spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC,
) -> dict[str, Any]:
    raw = _read_file_at(private_descriptor, marker_name, _MAX_MANIFEST_BYTES)
    value = parse_strict_json(raw, "private PDF reclamation marker")
    recovery_name = spec.recovery_prefix + marker_name.removeprefix(spec.reclaim_prefix)
    recovery = _validate_recovery_value(value, recovery_name, spec)
    if marker_name != spec.reclaim_prefix + recovery["manifest_digest"].removeprefix("sha256:"):
        raise OSError("private PDF reclamation marker digest drifted")
    return recovery


def _publish_reclaim_marker_at(
    private_descriptor: int,
    recovery: Mapping[str, Any],
    spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC,
) -> str:
    """Publish the exact reclaim marker through one bounded staging binding."""

    body = _recovery_body(recovery)
    marker_name = spec.reclaim_prefix + recovery["manifest_digest"].removeprefix(
        "sha256:"
    )
    if _binding_exists_at(private_descriptor, marker_name):
        if _read_file_at(
            private_descriptor, marker_name, _MAX_MANIFEST_BYTES
        ) != body:
            raise OSError("private PDF reclamation marker drifted")
        return marker_name
    descriptor: int | None = None
    try:
        descriptor = os.open(
            spec.reclaim_staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=private_descriptor,
        )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short private PDF reclamation marker write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.fsync(private_descriptor)
        _rename_noreplace_at(
            private_descriptor,
            spec.reclaim_staging,
            private_descriptor,
            marker_name,
        )
        os.fsync(private_descriptor)
        if _read_file_at(
            private_descriptor, marker_name, _MAX_MANIFEST_BYTES
        ) != body:
            raise OSError("private PDF reclamation marker publication drifted")
        return marker_name
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def _reclaim_marker_staging_at(
    private_descriptor: int, spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC
) -> None:
    """Reclaim only an exact prefix of the canonical pending marker body."""

    if _reclaim_names_at(private_descriptor, spec):
        raise OSError("private PDF marker staging conflicts with final marker")
    recovery_names = _recovery_names_at(private_descriptor, spec)
    if len(recovery_names) != 1:
        raise OSError("private PDF marker staging lacks exact recovery")
    recovery_descriptor, files_descriptor, recovery = _open_recovery_at(
        private_descriptor, recovery_names[0], spec
    )
    try:
        expected = _recovery_body(recovery)
        body = _read_file_at(
            private_descriptor, spec.reclaim_staging, _MAX_MANIFEST_BYTES
        )
        if not expected.startswith(body):
            raise OSError("private PDF marker staging drifted")
        _unlink_verified_at(
            private_descriptor,
            spec.reclaim_staging,
            body,
            _MAX_MANIFEST_BYTES,
        )
    finally:
        os.close(files_descriptor)
        os.close(recovery_descriptor)


def _open_recovery_for_reclaim_at(
    private_descriptor: int,
    recovery_name: str,
    recovery: Mapping[str, Any],
    spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC,
) -> tuple[int, int | None]:
    recovery_descriptor = os.open(
        recovery_name, _DIRECTORY_FLAGS, dir_fd=private_descriptor
    )
    files_descriptor: int | None = None
    try:
        entries = set(os.listdir(recovery_descriptor))
        if not entries.issubset({_RECOVERY_SCHEMA_NAME, "files"}):
            raise OSError("private PDF reclamation entries drifted")
        if _RECOVERY_SCHEMA_NAME in entries:
            if _read_file_at(
                recovery_descriptor, _RECOVERY_SCHEMA_NAME, _MAX_MANIFEST_BYTES
            ) != _recovery_body(recovery):
                raise OSError("private PDF reclamation schema drifted")
        if "files" in entries:
            files_descriptor = os.open(
                "files", _DIRECTORY_FLAGS, dir_fd=recovery_descriptor
            )
            expected = {entry["bundle"] for entry in recovery["entries"]}
            if not set(os.listdir(files_descriptor)).issubset(expected):
                raise OSError("private PDF reclamation files drifted")
        return recovery_descriptor, files_descriptor
    except BaseException:
        if files_descriptor is not None:
            os.close(files_descriptor)
        os.close(recovery_descriptor)
        raise


def _create_recovery_at(
    private_descriptor: int,
    manifest: Mapping[str, Any],
    spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC,
) -> tuple[str, int, int, dict[str, Any]]:
    value = _recovery_value(manifest, spec)
    name = spec.recovery_prefix + value["manifest_digest"].removeprefix("sha256:")
    os.mkdir(spec.recovery_staging, 0o700, dir_fd=private_descriptor)
    recovery_descriptor = os.open(
        spec.recovery_staging, _DIRECTORY_FLAGS, dir_fd=private_descriptor
    )
    files_descriptor: int | None = None
    schema_descriptor: int | None = None
    try:
        os.mkdir("files", 0o700, dir_fd=recovery_descriptor)
        files_descriptor = os.open(
            "files", _DIRECTORY_FLAGS, dir_fd=recovery_descriptor
        )
        schema_descriptor = os.open(
            _RECOVERY_SCHEMA_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=recovery_descriptor,
        )
        schema_body = _recovery_body(value)
        view = memoryview(schema_body)
        while view:
            written = os.write(schema_descriptor, view)
            if written <= 0:
                raise OSError("short private PDF recovery write")
            view = view[written:]
        os.fsync(schema_descriptor)
        os.close(schema_descriptor)
        schema_descriptor = None
        os.fsync(files_descriptor)
        os.fsync(recovery_descriptor)
        os.fsync(private_descriptor)
        _rename_noreplace_at(
            private_descriptor,
            spec.recovery_staging,
            private_descriptor,
            name,
        )
        os.fsync(private_descriptor)
        published = os.stat(name, dir_fd=private_descriptor, follow_symlinks=False)
        opened = os.fstat(recovery_descriptor)
        if (published.st_dev, published.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("private PDF recovery publication drifted")
        return name, recovery_descriptor, files_descriptor, value
    except BaseException:
        if schema_descriptor is not None:
            os.close(schema_descriptor)
        if files_descriptor is not None:
            os.close(files_descriptor)
        os.close(recovery_descriptor)
        raise


def _reclaim_recovery_staging_at(
    private_descriptor: int, spec: _PrivatePdfCorpusSpec = _UBUNTU_CORPUS_SPEC
) -> dict[str, Any]:
    """Reclaim the one incomplete bundle only while canonical corpus is intact."""

    corpus_descriptor: int | None = None
    input_descriptor: int | None = None
    staging_descriptor: int | None = None
    files_descriptor: int | None = None
    try:
        corpus_descriptor = os.open(
            spec.corpus_id, _DIRECTORY_FLAGS, dir_fd=private_descriptor
        )
        input_descriptor = os.open(
            "input", _DIRECTORY_FLAGS, dir_fd=corpus_descriptor
        )
        manifest = _load_private_pdf_manifest_at(
            spec.corpus_id, corpus_descriptor, input_descriptor
        )
        expected_recovery_body = _recovery_body(_recovery_value(manifest, spec))
        staging_descriptor = os.open(
            spec.recovery_staging, _DIRECTORY_FLAGS, dir_fd=private_descriptor
        )
        entries = set(os.listdir(staging_descriptor))
        if not entries.issubset({_RECOVERY_SCHEMA_NAME, "files"}):
            raise OSError("private PDF recovery staging entries drifted")
        if "files" in entries:
            files_descriptor = os.open(
                "files", _DIRECTORY_FLAGS, dir_fd=staging_descriptor
            )
            if os.listdir(files_descriptor):
                raise OSError("private PDF recovery staging files drifted")
        if _RECOVERY_SCHEMA_NAME in entries:
            body = _read_file_at(
                staging_descriptor, _RECOVERY_SCHEMA_NAME, _MAX_MANIFEST_BYTES
            )
            if not expected_recovery_body.startswith(body):
                raise OSError("private PDF recovery staging schema drifted")
            _unlink_verified_at(
                staging_descriptor,
                _RECOVERY_SCHEMA_NAME,
                body,
                _MAX_MANIFEST_BYTES,
            )
        if files_descriptor is not None:
            os.rmdir("files", dir_fd=staging_descriptor)
            os.fsync(staging_descriptor)
        if os.listdir(staging_descriptor):
            raise OSError("private PDF recovery staging remained nonempty")
        os.rmdir(spec.recovery_staging, dir_fd=private_descriptor)
        os.fsync(private_descriptor)
        return manifest
    finally:
        for descriptor in (
            files_descriptor,
            staging_descriptor,
            input_descriptor,
            corpus_descriptor,
        ):
            if descriptor is not None:
                os.close(descriptor)


def cleanup_private_pdf_uat(
    corpus_id: str, *, runtime_root: Path | None = None
) -> dict[str, Any]:
    """Remove a verified corpus through one durable recovery bundle.

    AO writers are serialized by the validated lock.  Reclamation verifies each
    binding immediately before unlink and fails closed on detected drift.  Linux
    has no inode-conditional unlink, so the final syscall assumes no hostile
    same-UID process replaces the verified name inside that unobservable window.
    """

    descriptors: list[int] = []
    try:
        require_identifier(corpus_id, "private PDF corpus identifier")
        spec = _PRIVATE_PDF_CORPUS_SPECS.get(corpus_id)
        if spec is None:
            raise OSError("private PDF corpus is not harness-owned")
        _, root_descriptor = _open_runtime_root(runtime_root)
        descriptors.append(root_descriptor)
        try:
            private_descriptor = _open_relative_directory(
                root_descriptor, ("calibration", "private-pdf"), create=False
            )
        except FileNotFoundError:
            return {"corpus_id": corpus_id, "removed": False, "verified_documents": 0}
        descriptors.append(private_descriptor)
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=True)
        descriptors.append(lock_descriptor)

        if _binding_exists_at(private_descriptor, spec.preparation_recovery):
            raise OSError("private PDF preparation recovery is unresolved")
        if _binding_exists_at(private_descriptor, spec.recovery_staging):
            _reclaim_recovery_staging_at(private_descriptor, spec)
        if _binding_exists_at(private_descriptor, spec.reclaim_staging):
            _reclaim_marker_staging_at(private_descriptor, spec)

        reclaim_names = _reclaim_names_at(private_descriptor, spec)
        recovery_names = _recovery_names_at(private_descriptor, spec)
        reclaim_established = bool(reclaim_names)
        if reclaim_established:
            reclaim_name = reclaim_names[0]
            recovery = _load_reclaim_marker_at(private_descriptor, reclaim_name, spec)
            recovery_name = spec.recovery_prefix + recovery["manifest_digest"].removeprefix("sha256:")
            if recovery_names and recovery_names != [recovery_name]:
                raise OSError("private PDF reclamation recovery identity drifted")
            manifest = recovery["manifest"]
            if recovery_names:
                recovery_descriptor, files_descriptor = (
                    _open_recovery_for_reclaim_at(
                        private_descriptor, recovery_name, recovery, spec
                    )
                )
                descriptors.append(recovery_descriptor)
                if files_descriptor is not None:
                    descriptors.append(files_descriptor)
            else:
                if _binding_exists_at(
                    private_descriptor, spec.cleanup_quarantine
                ) or _binding_exists_at(private_descriptor, corpus_id):
                    raise OSError("private PDF reclamation source unexpectedly exists")
                _unlink_verified_at(
                    private_descriptor,
                    reclaim_name,
                    _recovery_body(recovery),
                    _MAX_MANIFEST_BYTES,
                )
                return {
                    "corpus_id": corpus_id,
                    "removed": True,
                    "verified_documents": len(manifest["documents"]),
                }
        elif recovery_names:
            recovery_name = recovery_names[0]
            recovery_descriptor, files_descriptor, recovery = _open_recovery_at(
                private_descriptor, recovery_name, spec
            )
            descriptors.extend((recovery_descriptor, files_descriptor))
            manifest = recovery["manifest"]
        else:
            try:
                corpus_descriptor = os.open(
                    corpus_id, _DIRECTORY_FLAGS, dir_fd=private_descriptor
                )
            except FileNotFoundError:
                if _binding_exists_at(private_descriptor, spec.cleanup_quarantine):
                    raise OSError("private PDF cleanup quarantine lacks recovery")
                return {"corpus_id": corpus_id, "removed": False, "verified_documents": 0}
            descriptors.append(corpus_descriptor)
            input_descriptor = os.open(
                "input", _DIRECTORY_FLAGS, dir_fd=corpus_descriptor
            )
            descriptors.append(input_descriptor)
            manifest = _load_private_pdf_manifest_at(
                corpus_id, corpus_descriptor, input_descriptor
            )
            recovery_name, recovery_descriptor, files_descriptor, recovery = (
                _create_recovery_at(private_descriptor, manifest, spec)
            )
            descriptors.extend((recovery_descriptor, files_descriptor))

        corpus_name: str | None
        if reclaim_established:
            if _binding_exists_at(
                private_descriptor, spec.cleanup_quarantine
            ) or _binding_exists_at(private_descriptor, corpus_id):
                raise OSError("private PDF reclamation source unexpectedly exists")
            corpus_name = None
        elif _binding_exists_at(private_descriptor, spec.cleanup_quarantine):
            corpus_name = spec.cleanup_quarantine
        elif _binding_exists_at(private_descriptor, corpus_id):
            _rename_noreplace_at(
                private_descriptor,
                corpus_id,
                private_descriptor,
                spec.cleanup_quarantine,
            )
            os.fsync(private_descriptor)
            corpus_name = spec.cleanup_quarantine
        else:
            corpus_name = None

        corpus_descriptor = None
        input_descriptor = None
        if corpus_name is not None:
            corpus_descriptor = os.open(
                corpus_name, _DIRECTORY_FLAGS, dir_fd=private_descriptor
            )
            descriptors.append(corpus_descriptor)
            try:
                input_descriptor = os.open(
                    "input", _DIRECTORY_FLAGS, dir_fd=corpus_descriptor
                )
            except FileNotFoundError:
                input_descriptor = None
            else:
                descriptors.append(input_descriptor)

        for entry in recovery["entries"]:
            bundle_name = entry["bundle"]
            maximum = (
                _MAX_MANIFEST_BYTES
                if entry["source"] == _MANIFEST_NAME
                else MAX_PRIVATE_PDF_BYTES
            )
            if files_descriptor is not None and _binding_exists_at(
                files_descriptor, bundle_name
            ):
                body = _read_file_at(files_descriptor, bundle_name, maximum)
                if "sha256:" + hashlib.sha256(body).hexdigest() != entry["digest"]:
                    raise OSError("private PDF cleanup recovery digest drifted")
                continue
            if reclaim_established:
                continue
            if corpus_descriptor is None:
                raise OSError("private PDF cleanup recovery source is missing")
            if entry["source"] == _MANIFEST_NAME:
                source_descriptor = corpus_descriptor
                source_name = _MANIFEST_NAME
                expected_body = _manifest_body(manifest)
            else:
                if input_descriptor is None:
                    raise OSError("private PDF cleanup recovery input is missing")
                source_descriptor = input_descriptor
                source_name = PurePosixPath(entry["source"]).name
                expected_body = _read_file_at(
                    source_descriptor, source_name, maximum
                )
            if "sha256:" + hashlib.sha256(expected_body).hexdigest() != entry["digest"]:
                raise OSError("private PDF cleanup source digest drifted")
            binding = os.stat(
                source_name, dir_fd=source_descriptor, follow_symlinks=False
            )
            _quarantine_verified_file_at(
                source_descriptor,
                source_name,
                binding,
                expected_body,
                maximum,
                retained_descriptor=files_descriptor,
                retained_name=bundle_name,
            )
            os.fsync(files_descriptor)

        expected_bundle_names = {entry["bundle"] for entry in recovery["entries"]}
        actual_bundle_names = (
            set() if files_descriptor is None else set(os.listdir(files_descriptor))
        )
        if (
            actual_bundle_names != expected_bundle_names
            and not (
                reclaim_established
                and actual_bundle_names.issubset(expected_bundle_names)
            )
        ):
            raise OSError("private PDF cleanup recovery files drifted")

        if corpus_descriptor is not None:
            if input_descriptor is not None:
                if os.listdir(input_descriptor):
                    raise OSError("private PDF cleanup input entries drifted")
                os.rmdir("input", dir_fd=corpus_descriptor)
                os.fsync(corpus_descriptor)
            if os.listdir(corpus_descriptor):
                raise OSError("private PDF cleanup corpus entries drifted")
            os.rmdir(spec.cleanup_quarantine, dir_fd=private_descriptor)
            os.fsync(private_descriptor)

        if not reclaim_established:
            reclaim_name = _publish_reclaim_marker_at(
                private_descriptor, recovery, spec
            )
            reclaim_established = True

        # Cooperative-writer boundary: after this final verification, unlinkat
        # is assumed not to race a hostile same-UID namespace replacement.
        for entry in recovery["entries"]:
            bundle_name = entry["bundle"]
            maximum = (
                _MAX_MANIFEST_BYTES
                if entry["source"] == _MANIFEST_NAME
                else MAX_PRIVATE_PDF_BYTES
            )
            if files_descriptor is not None and _binding_exists_at(
                files_descriptor, bundle_name
            ):
                before = os.stat(
                    bundle_name, dir_fd=files_descriptor, follow_symlinks=False
                )
                body = _read_file_at(files_descriptor, bundle_name, maximum)
                after = os.stat(
                    bundle_name, dir_fd=files_descriptor, follow_symlinks=False
                )
                if (
                    _stable_metadata(before) != _stable_metadata(after)
                    or "sha256:" + hashlib.sha256(body).hexdigest() != entry["digest"]
                ):
                    raise OSError("private PDF cleanup reclamation drifted")
                os.unlink(bundle_name, dir_fd=files_descriptor)
        if files_descriptor is not None:
            os.fsync(files_descriptor)
        schema_body = _recovery_body(recovery)
        if _binding_exists_at(recovery_descriptor, _RECOVERY_SCHEMA_NAME):
            _unlink_verified_at(
                recovery_descriptor,
                _RECOVERY_SCHEMA_NAME,
                schema_body,
                _MAX_MANIFEST_BYTES,
            )
        if files_descriptor is not None:
            if os.listdir(files_descriptor):
                raise OSError("private PDF reclamation files remained")
            os.rmdir("files", dir_fd=recovery_descriptor)
            os.fsync(recovery_descriptor)
        if os.listdir(recovery_descriptor):
            raise OSError("private PDF reclamation recovery remained")
        os.rmdir(recovery_name, dir_fd=private_descriptor)
        os.fsync(private_descriptor)
        _unlink_verified_at(
            private_descriptor,
            reclaim_name,
            _recovery_body(recovery),
            _MAX_MANIFEST_BYTES,
        )
        return {
            "corpus_id": corpus_id,
            "removed": True,
            "verified_documents": len(manifest["documents"]),
        }
    except Exception as exc:
        message = (
            "representative PDF corpus cleanup failed"
            if corpus_id == DOMAIN_CORPUS_ID
            else "private PDF seed cleanup failed"
        )
        raise PrivatePdfUatError(message) from exc
    finally:
        for descriptor in reversed(locals().get("descriptors", [])):
            os.close(descriptor)


def _default_calibration_measurement(
    operation: Callable[[], Any],
) -> tuple[Any, float, int]:
    tracemalloc.start()
    started = time.perf_counter()
    try:
        output = operation()
    finally:
        duration = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return output, duration, peak


def _load_private_calibration_bytes(
    manifest: Mapping[str, Any],
    corpus_root: Path,
    *,
    expected_source_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    validated = validate_private_pdf_manifest(manifest)
    if not 1 <= len(validated["documents"]) <= 4:
        raise OSError("private PDF calibration document count is invalid")
    if expected_source_bindings is not None and (
        not isinstance(expected_source_bindings, Sequence)
        or isinstance(expected_source_bindings, (str, bytes))
        or len(expected_source_bindings) != len(validated["documents"])
    ):
        raise OSError("private PDF calibration source bindings are invalid")
    selected, _ = ensure_contained(
        Path(corpus_root), repository_root(), "private PDF calibration corpus"
    )
    if (
        selected.name != validated["corpus_id"]
        or selected.parent.name != "private-pdf"
        or selected.parent.parent.name != "calibration"
    ):
        raise OSError("private PDF calibration corpus identity drifted")
    corpus_descriptor: int | None = None
    input_descriptor: int | None = None
    try:
        corpus_descriptor = _open_directory(
            selected, repository_root(), create=False
        )
        input_descriptor = _open_relative_directory(
            corpus_descriptor, ("input",), create=False
        )
        loaded = _load_private_pdf_manifest_at(
            validated["corpus_id"], corpus_descriptor, input_descriptor
        )
        if loaded != validated:
            raise OSError("private PDF calibration manifest binding drifted")
        bodies: dict[str, bytes] = {}
        total_bytes = 0
        for index, document in enumerate(loaded["documents"]):
            name = PurePosixPath(document["file"]).name
            before = os.stat(
                name, dir_fd=input_descriptor, follow_symlinks=False
            )
            body = _read_file_at(input_descriptor, name, MAX_PRIVATE_PDF_BYTES)
            after = os.stat(
                name, dir_fd=input_descriptor, follow_symlinks=False
            )
            if _stable_metadata(before) != _stable_metadata(after):
                raise OSError("private PDF calibration source binding drifted")
            if "sha256:" + hashlib.sha256(body).hexdigest() != document["source_digest"]:
                raise OSError("private PDF calibration source binding drifted")
            if expected_source_bindings is not None:
                supplied = expected_source_bindings[index]
                if supplied != {
                    "item_id": document["item_id"],
                    "file": document["file"],
                    "digest": document["source_digest"],
                    "size": len(body),
                    "identity": [before.st_dev, before.st_ino],
                }:
                    raise OSError("private PDF calibration source binding drifted")
            total_bytes += len(body)
            if total_bytes > _PRIVATE_CALIBRATION_MAX_TOTAL_BYTES:
                raise OSError("private PDF calibration corpus exceeds byte limit")
            bodies[f"{document['item_id']}.pdf"] = body
        return loaded, bodies
    finally:
        if input_descriptor is not None:
            os.close(input_descriptor)
        if corpus_descriptor is not None:
            os.close(corpus_descriptor)


def _private_benchmark_corpus(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "ao.lore.pdf-fixture-corpus.v0.1",
        "configuration": {
            "max_file_size": MAX_PRIVATE_PDF_BYTES,
            "max_num_pages": PRIVATE_PDF_MAX_PAGES,
            "max_blocks": PRIVATE_PDF_MAX_BLOCKS,
            "latency_max_seconds": 120.0,
            "memory_max_bytes": 4_294_967_296,
            "minimum_scores": {
                "text_fidelity": 0.5,
                "reliability": 1.0,
                "deterministic_repeatability": 1.0,
            },
        },
        "metrics": [
            "structural_fidelity",
            "text_fidelity",
            "source_location_fidelity",
            "reliability",
            "deterministic_repeatability",
            "latency",
            "memory",
            "external_cost",
        ],
        "fixtures": [
            {
                "id": document["item_id"],
                "file": f"{document['item_id']}.pdf",
                "sha256": document["source_digest"],
                "kind": "text",
                "title": "",
                "lines": [],
                "link": None,
                "expected_text": list(document["expected_text"]),
                "required_block_types": list(document["required_block_types"]),
                "applicable_metrics": list(_METRIC_NAMES),
                "expected_outcome": "accepted",
                "expected_failure": None,
            }
            for document in manifest["documents"]
        ],
    }


def _validate_private_parse_output(
    output: Any, source: Mapping[str, Any]
) -> ParseOutput:
    if not isinstance(output, ParseOutput):
        raise ParsingError("private PDF calibration output is invalid")
    if set(vars(output)) != {
        "document_ir",
        "quality_components",
        "critical_failures",
    }:
        raise ParsingError("private PDF calibration output shape is invalid")
    ir = output.document_ir
    if not isinstance(ir, Mapping) or set(ir) != {
        "schema_version",
        "document_id",
        "source",
        "parser",
        "blocks",
        "metadata",
    }:
        raise ParsingError("private PDF calibration document IR is invalid")
    source_binding = ir.get("source")
    parser = ir.get("parser")
    metadata = ir.get("metadata")
    blocks = ir.get("blocks")
    quality = output.quality_components
    if (
        ir.get("schema_version") != "ao.lore.document-ir.v0.1"
        or not isinstance(source_binding, Mapping)
        or set(source_binding) != {"resource", "digest", "media_type"}
        or source_binding != {
            key: source[key] for key in ("resource", "digest", "media_type")
        }
        or ir.get("document_id") != source["digest"]
        or not isinstance(parser, Mapping)
        or set(parser) != {
            "parser_id",
            "parser_version",
            "configuration_digest",
        }
        or parser.get("parser_id") != "docling"
        or parser.get("parser_version") != "2.118.1"
        or parser.get("configuration_digest") != PRIVATE_PDF_CONFIGURATION_DIGEST
        or not isinstance(metadata, Mapping)
        or set(metadata) != {
            "format",
            "page_count",
            "ocr_used",
            "configuration_digest",
        }
        or metadata.get("format") != "pdf"
        or isinstance(metadata.get("page_count"), bool)
        or not isinstance(metadata.get("page_count"), int)
        or not 1 <= metadata["page_count"] <= PRIVATE_PDF_MAX_PAGES
        or metadata.get("ocr_used") is not False
        or metadata.get("configuration_digest")
        != PRIVATE_PDF_CONFIGURATION_DIGEST
        or not isinstance(blocks, list)
        or not 1 <= len(blocks) <= PRIVATE_PDF_MAX_BLOCKS
        or not isinstance(quality, Mapping)
        or set(quality) != {
            "text_coverage",
            "structural_completeness",
            "source_span_coverage",
            "document_ir_valid",
        }
        or any(
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0 <= score <= 1
            for score in quality.values()
        )
        or type(output.critical_failures) is not tuple
        or bool(output.critical_failures)
    ):
        raise ParsingError("private PDF calibration result binding is invalid")
    for index, block in enumerate(blocks, 1):
        _validate_private_pdf_block(
            block, index=index, page_count=metadata["page_count"]
        )
    nonparagraph = sum(block["type"] != "paragraph" for block in blocks)
    located = sum(
        "page" in block["source_span"]
        or "coordinates" in block["source_span"]
        for block in blocks
    )
    expected_quality = {
        "text_coverage": 1.0,
        "structural_completeness": min(
            1.0, (nonparagraph + 1) / len(blocks)
        ),
        "source_span_coverage": located / len(blocks),
        "document_ir_valid": 1.0,
    }
    if any(quality[name] != expected for name, expected in expected_quality.items()):
        raise ParsingError("private PDF calibration result binding is invalid")
    return ParseOutput(
        _strict_copy(ir),
        {name: float(quality[name]) for name in sorted(quality)},
        (),
    )


def _validate_private_pdf_attribute(value: Any) -> None:
    forbidden_keys = {
        "authority",
        "claims_authority_advance",
        "network_access",
        "network_accessed",
        "ocr_enabled",
        "ocr_used",
        "promotion_authority",
        "provider_calls",
    }
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ParsingError("private PDF calibration block attribute is invalid")
        return
    if isinstance(value, str):
        if (
            value.startswith(("/", "\\"))
            or re.match(r"^[A-Za-z]:\\", value) is not None
            or "\x00" in value
        ):
            raise ParsingError("private PDF calibration block attribute is invalid")
        return
    if isinstance(value, list):
        for item in value:
            _validate_private_pdf_attribute(item)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or key in forbidden_keys
                or any(
                    token in key.lower()
                    for token in ("authority", "network", "ocr", "provider")
                )
            ):
                raise ParsingError("private PDF calibration block attribute is invalid")
            _validate_private_pdf_attribute(item)
        return
    raise ParsingError("private PDF calibration block attribute is invalid")


def _validate_private_pdf_block(
    block: Any, *, index: int, page_count: int
) -> None:
    required = {"id", "type", "text", "source_span"}
    allowed = required | {"level", "attributes"}
    if (
        not isinstance(block, Mapping)
        or not required.issubset(block)
        or set(block) - allowed
        or block.get("id") != f"b{index}"
        or block.get("type") not in _BLOCK_TYPES
        or not isinstance(block.get("text"), str)
    ):
        raise ParsingError("private PDF calibration block is invalid")
    span = block["source_span"]
    if not isinstance(span, Mapping) or not {"start", "end"}.issubset(span) or set(span) - {
        "start",
        "end",
        "page",
        "coordinates",
    }:
        raise ParsingError("private PDF calibration block span is invalid")
    start = span["start"]
    end = span["end"]
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end < start
    ):
        raise ParsingError("private PDF calibration block span is invalid")
    if "page" in span and (
        isinstance(span["page"], bool)
        or not isinstance(span["page"], int)
        or not 1 <= span["page"] <= page_count
    ):
        raise ParsingError("private PDF calibration block page is invalid")
    if "coordinates" in span and (
        not isinstance(span["coordinates"], list)
        or len(span["coordinates"]) != 4
        or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in span["coordinates"]
        )
    ):
        raise ParsingError("private PDF calibration block coordinates are invalid")
    if "level" in block and (
        isinstance(block["level"], bool)
        or not isinstance(block["level"], int)
        or block["level"] < 1
    ):
        raise ParsingError("private PDF calibration block level is invalid")
    if "attributes" in block:
        if not isinstance(block["attributes"], Mapping):
            raise ParsingError("private PDF calibration block attributes are invalid")
        _validate_private_pdf_attribute(block["attributes"])


def _validate_private_calibration_adapter(adapter: Any) -> Mapping[str, Any]:
    capability = adapter.capability
    if (
        not isinstance(capability, Mapping)
        or capability.get("parser_id") != "docling"
        or capability.get("version") != "2.118.1"
        or capability.get("supported_mime_types") != ["application/pdf"]
        or capability.get("supported_extensions") != [".pdf"]
        or capability.get("ocr_capable") is not False
        or capability.get("deterministic_offline") is not True
        or capability.get("frontier_service_required") is not False
        or capability.get("network_access", False) is not False
        or capability.get("provider_calls", False) is not False
        or isinstance(capability.get("expected_monetary_cost"), bool)
        or capability.get("expected_monetary_cost") != 0
        or isinstance(capability.get("max_file_bytes"), bool)
        or capability.get("max_file_bytes") != MAX_PRIVATE_PDF_BYTES
    ):
        raise ParsingError("private PDF calibration adapter safety is invalid")
    declared = getattr(adapter, "calibration_configuration", None)
    expected = {
        "configuration_digest": PRIVATE_PDF_CONFIGURATION_DIGEST,
        "offline": True,
        "network_access": False,
        "ocr_enabled": False,
        "provider_calls": False,
    }
    if declared is not None:
        if not isinstance(declared, Mapping) or dict(declared) != expected:
            raise ParsingError("private PDF calibration configuration is invalid")
    else:
        backend = getattr(adapter, "_backend", None)
        if (
            getattr(adapter, "_configuration_digest", None)
            != PRIVATE_PDF_CONFIGURATION_DIGEST
            or getattr(adapter, "_max_file_size", None) != MAX_PRIVATE_PDF_BYTES
            or getattr(adapter, "_max_num_pages", None) != PRIVATE_PDF_MAX_PAGES
            or getattr(adapter, "_max_blocks", None) != PRIVATE_PDF_MAX_BLOCKS
            or backend is None
            or getattr(backend, "ocr_enabled", None) is not False
            or tuple(getattr(backend, "allowed_formats", ())) != ("pdf",)
        ):
            raise ParsingError("private PDF calibration configuration is invalid")
    return _strict_copy(capability)


class _PrivateCalibrationDeadline(BaseException):
    """Unswallowable worker deadline used to abort the complete calibration."""


class _PrivateCalibrationAdapterSnapshot:
    """Delegate parsing while exposing only one detached validated capability."""

    def __init__(
        self,
        adapter: Any,
        capability: Mapping[str, Any],
        item_timeout_seconds: float | None,
    ) -> None:
        self._adapter = adapter
        self._capability = _strict_copy(capability)
        self._item_timeout_seconds = item_timeout_seconds

    @property
    def capability(self) -> Mapping[str, Any]:
        return _strict_copy(self._capability)

    def parse(self, source: Mapping[str, Any]) -> ParseOutput:
        if self._item_timeout_seconds is None:
            return self._adapter.parse(source)
        if threading.current_thread() is not threading.main_thread():
            raise ParsingError("private PDF calibration deadline context is invalid")
        previous_handler = signal.getsignal(signal.SIGALRM)
        previous_timer = signal.getitimer(signal.ITIMER_REAL)
        if previous_timer != (0.0, 0.0):
            raise ParsingError("private PDF calibration deadline is already active")

        def expired(_signum: int, _frame: Any) -> None:
            raise _PrivateCalibrationDeadline()

        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, self._item_timeout_seconds)
        try:
            return self._adapter.parse(source)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_handler)


@contextmanager
def _private_calibration_network_guard():
    """Own process-global network and measurement guards for one operator run."""

    attempts: list[str] = []

    def reject_connect(*_args: Any, **_kwargs: Any) -> Any:
        attempts.append("connect")
        raise OSError("private PDF calibration network is disabled")

    current = threading.current_thread()
    if tracemalloc.is_tracing():
        raise ParsingError("private PDF calibration tracing is already active")
    if any(thread is not current and thread.is_alive() for thread in threading.enumerate()):
        raise ParsingError("private PDF calibration requires an exclusive process")
    if not _CALIBRATION_GUARD_LOCK.acquire(blocking=False):
        raise ParsingError("private PDF calibration is already active")
    try:
        if any(thread is not current and thread.is_alive() for thread in threading.enumerate()):
            raise ParsingError("private PDF calibration requires an exclusive process")
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex
        original_create_connection = socket.create_connection
        socket.socket.connect = reject_connect
        socket.socket.connect_ex = reject_connect
        socket.create_connection = reject_connect
        try:
            if any(
                thread is not current and thread.is_alive()
                for thread in threading.enumerate()
            ):
                raise ParsingError(
                    "private PDF calibration requires an exclusive process"
                )
            yield attempts
        finally:
            socket.socket.connect = original_connect
            socket.socket.connect_ex = original_connect_ex
            socket.create_connection = original_create_connection
    finally:
        _CALIBRATION_GUARD_LOCK.release()


def _summary(values: Sequence[int | float]) -> dict[str, float | int]:
    if not values:
        return {"minimum": 0, "maximum": 0, "mean": 0.0}
    return {
        "minimum": min(values),
        "maximum": max(values),
        "mean": sum(values) / len(values),
    }


def _validate_calibration_aggregate(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail("private PDF calibration aggregate must be an object")
    aggregate = _strict_copy(value)
    require_exact_keys(
        aggregate,
        (
            "metrics",
            "exclusions",
            "stable_failures",
            "latency_seconds",
            "peak_memory_bytes",
            "repeatability",
        ),
        "private PDF calibration aggregate",
    )
    metrics = aggregate["metrics"]
    if not isinstance(metrics, Mapping) or set(metrics) - set(_METRIC_NAMES):
        raise _fail("private PDF calibration metrics are invalid")
    validated_metrics = {
        name: _require_metric_score(score, f"private PDF calibration metrics.{name}")
        for name, score in metrics.items()
    }
    exclusions = aggregate["exclusions"]
    if (
        not isinstance(exclusions, list)
        or any(not isinstance(item, str) for item in exclusions)
        or len(exclusions) != len(set(exclusions))
        or set(exclusions) - set(_METRIC_NAMES)
        or set(validated_metrics).intersection(exclusions)
        or set(validated_metrics).union(exclusions) != set(_METRIC_NAMES)
    ):
        raise _fail("private PDF calibration metric partition is invalid")
    failures = aggregate["stable_failures"]
    if not isinstance(failures, Mapping):
        raise _fail("private PDF calibration failures are invalid")
    require_exact_keys(failures, _FAILURE_CODES, "private PDF calibration failures")
    validated_failures = {
        code: require_int(failures[code], f"private PDF calibration failures.{code}", 0, 20)
        for code in _FAILURE_CODES
    }
    repeatability = aggregate["repeatability"]
    if not isinstance(repeatability, Mapping):
        raise _fail("private PDF calibration repeatability is invalid")
    require_exact_keys(
        repeatability,
        ("runs", "identical", "score"),
        "private PDF calibration repeatability",
    )
    validated_repeatability = {
        "runs": require_int(repeatability["runs"], "private PDF calibration runs", 2, 20),
        "identical": require_bool(repeatability["identical"], "private PDF calibration identical"),
        "score": _require_metric_score(repeatability["score"], "private PDF calibration repeatability score"),
    }
    if validated_repeatability["identical"] != (validated_repeatability["score"] == 1.0):
        raise _fail("private PDF calibration repeatability is contradictory")
    return {
        "metrics": validated_metrics,
        "exclusions": list(exclusions),
        "stable_failures": validated_failures,
        "latency_seconds": _require_summary(aggregate["latency_seconds"], "private PDF calibration latency"),
        "peak_memory_bytes": _require_summary(aggregate["peak_memory_bytes"], "private PDF calibration memory"),
        "repeatability": validated_repeatability,
    }


def evaluate_private_pdf_corpus(
    manifest: Mapping[str, Any],
    *,
    adapter: Any,
    corpus_root: Path,
    measurement: Callable[[Callable[[], Any]], tuple[Any, float, int]] | None = None,
    item_timeout_seconds: float | None = None,
    expected_source_bindings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Evaluate a contained private corpus into the exact path-free aggregate."""

    try:
        if any(os.environ.get(key) != value for key, value in _OFFLINE_ENVIRONMENT.items()):
            raise ParsingError("private PDF calibration offline environment is invalid")
        if item_timeout_seconds is not None and (
            isinstance(item_timeout_seconds, bool)
            or not isinstance(item_timeout_seconds, (int, float))
            or not math.isfinite(item_timeout_seconds)
            or item_timeout_seconds <= 0
        ):
            raise ParsingError("private PDF calibration deadline is invalid")
        validated, fixture_data = _load_private_calibration_bytes(
            manifest,
            Path(corpus_root),
            expected_source_bindings=expected_source_bindings,
        )
        corpus = _private_benchmark_corpus(validated)
        measurement_samples: list[tuple[float, int]] = []
        measure = measurement or _default_calibration_measurement
        if not callable(measure):
            raise ParsingError("private PDF calibration measurement seam is invalid")

        with _private_calibration_network_guard() as network_attempts:
            capability = _validate_private_calibration_adapter(adapter)
            bound_adapter = _PrivateCalibrationAdapterSnapshot(
                adapter, capability, item_timeout_seconds
            )
            benchmark = run_pdf_benchmark(
                bound_adapter,
                corpus,
                fixture_root=Path(corpus_root) / "input",
                runtime="python-3.12",
                platform="linux-x86_64",
                fixture_data=fixture_data,
                measurement=measure,
                measurement_samples=measurement_samples,
                result_validator=_validate_private_parse_output,
                complete_attempts_on_failure=True,
            )
        durations = [duration for duration, _ in measurement_samples]
        peaks = [peak for _, peak in measurement_samples]
        if network_attempts:
            raise ParsingError("private PDF calibration attempted network access")
        expected_corpus_digest = canonical_digest(corpus)
        verify_manifest(
            benchmark,
            expected_corpus_digest=expected_corpus_digest,
            expected_configuration_digest=PRIVATE_PDF_CONFIGURATION_DIGEST,
        )
        if (
            benchmark["parser_id"] != "docling"
            or benchmark["parser_version"] != "2.118.1"
            or benchmark["fixture_corpus_digest"] != expected_corpus_digest
            or benchmark["parser_configuration_digest"] != PRIVATE_PDF_CONFIGURATION_DIGEST
        ):
            raise ParsingError("private PDF calibration benchmark binding drifted")
        item_ids = [document["item_id"] for document in validated["documents"]]
        rejected: dict[str, str] = {}
        for failure in benchmark["failures"]:
            for code in _FAILURE_CODES:
                suffix = f":{code}"
                if failure.endswith(suffix) and failure[: -len(suffix)] in item_ids:
                    rejected.setdefault(failure[: -len(suffix)], code)
        stable_failures = {code: 0 for code in _FAILURE_CODES}
        for code in rejected.values():
            stable_failures[code] += 1
        successful = len(item_ids) - len(rejected)
        if successful:
            metrics = {
                name: benchmark["raw_metrics"][name]
                for name in _METRIC_NAMES
                if benchmark["raw_metrics"][name] is not None
            }
            exclusions = [name for name in _METRIC_NAMES if name not in metrics]
        else:
            metrics = {}
            exclusions = list(_METRIC_NAMES)
        repeatability_score = benchmark["raw_metrics"]["deterministic_repeatability"]
        aggregate = {
            "metrics": metrics,
            "exclusions": exclusions,
            "stable_failures": stable_failures,
            "latency_seconds": _summary(durations),
            "peak_memory_bytes": _summary(peaks),
            "repeatability": {
                "runs": 2,
                "identical": repeatability_score == 1.0,
                "score": repeatability_score,
            },
        }
        return _validate_calibration_aggregate(aggregate)
    except Exception as exc:
        if isinstance(exc, PrivatePdfUatError) and str(exc) == "private PDF calibration failed":
            raise
        raise PrivatePdfUatError("private PDF calibration failed") from exc


def derive_tuning_decision(
    aggregate: Mapping[str, Any], *, representative_domain: bool = False
) -> str:
    """Return the closed decision allowed by the validated campaign origin."""

    if type(representative_domain) is not bool:
        raise _fail("private PDF representative-domain flag is invalid")

    try:
        validated = _validate_calibration_aggregate(aggregate)
    except PrivatePdfUatError:
        raise
    except (ContractError, TypeError, ValueError) as exc:
        raise _fail("private PDF calibration aggregate is invalid") from exc
    failed = any(validated["stable_failures"].values())
    repeatable = validated["repeatability"] == {
        "runs": 2, "identical": True, "score": 1.0
    }
    fidelity_passed = all(
        name not in validated["metrics"]
        or validated["metrics"][name] >= minimum
        for name, minimum in _PRIVATE_FIDELITY_MINIMUMS.items()
    )
    if failed or not repeatable:
        return "investigate"
    if not fidelity_passed and representative_domain:
        return "candidate_change"
    return "hold" if fidelity_passed else "investigate"


def validate_private_pdf_readback(
    value: Mapping[str, Any], *, expected_item_ids: Sequence[str] | None = None
) -> dict[str, Any]:
    """Return a detached path-free no-authority UAT report."""

    try:
        if not isinstance(value, Mapping):
            raise _fail("private PDF readback must be an object")
        report = _strict_copy(value)
        if report.get("schema_version") != "ao.lore.private-pdf-uat-readback.v0.1":
            raise _fail("private PDF readback schema_version is invalid")
        lifecycle_status = report.get("lifecycle_status")
        if lifecycle_status == "not_supplied":
            require_exact_keys(
                report,
                (
                    "schema_version",
                    "lifecycle_status",
                    "qualified",
                    "ocr_used",
                    "provider_calls",
                    "promotion_authority",
                    "claims_authority_advance",
                ),
                "private PDF readback",
            )
            if require_bool(report["qualified"], "private PDF readback qualified") is not False:
                raise _fail("private PDF readback qualified must be false")
            if require_bool(report["ocr_used"], "private PDF readback ocr_used") is not False:
                raise _fail("private PDF readback ocr_used must be false")
            for field in ("provider_calls", "promotion_authority", "claims_authority_advance"):
                if require_bool(report[field], f"private PDF readback {field}") is not False:
                    raise _fail(f"private PDF readback {field} must be false")
            return report

        if expected_item_ids is None:
            raise _fail("private PDF readback expected_item_ids is required for terminal readbacks")
        validated_expected_item_ids = _require_expected_item_ids(
            expected_item_ids, "private PDF readback expected_item_ids"
        )

        require_exact_keys(
            report,
            (
                "schema_version",
                "corpus_id",
                "corpus_digest",
                "configuration_digest",
                "qualification_digest",
                "aggregate_digest",
                "uat_digest",
                "campaign_origin_digest",
                "lifecycle_status",
                "tuning_decision",
                "qualified",
                "counts",
                "conversion_counts",
                "brain_before_digest",
                "brain_after_digest",
                "results",
                "aggregate",
                "ocr_enabled",
                "ocr_used",
                "network_accessed",
                "provider_calls",
                "promotion_authority",
                "claims_authority_advance",
            ),
            "private PDF readback",
        )
        require_identifier(report["corpus_id"], "private PDF readback corpus_id")
        for field in (
            "corpus_digest",
            "configuration_digest",
            "qualification_digest",
            "aggregate_digest",
            "uat_digest",
            "campaign_origin_digest",
            "brain_before_digest",
            "brain_after_digest",
        ):
            _require_digest(report[field], f"private PDF readback {field}")
        if report["lifecycle_status"] not in {"completed", "partial"}:
            raise _fail("private PDF readback lifecycle_status is invalid")
        if report["tuning_decision"] not in {
            "hold", "candidate_change", "investigate"
        }:
            raise _fail("private PDF readback tuning_decision is invalid")
        if (
            report["tuning_decision"] == "candidate_change"
            and report["corpus_id"] != DOMAIN_CORPUS_ID
        ):
            raise _fail("private PDF readback tuning_decision is invalid")
        for field in (
            "qualified",
            "ocr_enabled",
            "ocr_used",
            "network_accessed",
            "provider_calls",
            "promotion_authority",
            "claims_authority_advance",
        ):
            if require_bool(report[field], f"private PDF readback {field}") is not False:
                raise _fail(f"private PDF readback {field} must be false")

        counts = report["counts"]
        if not isinstance(counts, Mapping):
            raise _fail("private PDF readback counts must be an object")
        require_exact_keys(
            counts,
            (
                "total",
                "created",
                "unchanged",
                "rejected",
                "processed",
                "successful",
                "candidate_queue",
                "interrupted",
                "resumed",
                "rerun",
            ),
            "private PDF readback counts",
        )
        validated_counts = {
            key: require_int(counts[key], f"private PDF readback counts.{key}", 0, 20)
            for key in (
                "total",
                "created",
                "unchanged",
                "rejected",
                "processed",
                "successful",
                "candidate_queue",
            )
        }
        for key in ("interrupted", "resumed", "rerun"):
            value_int = require_int(counts[key], f"private PDF readback counts.{key}", 1, 1)
            validated_counts[key] = value_int

        conversion_counts = report["conversion_counts"]
        if not isinstance(conversion_counts, Mapping):
            raise _fail("private PDF readback conversion_counts must be an object")
        require_exact_keys(
            conversion_counts,
            ("initial", "resumed", "rerun"),
            "private PDF readback conversion_counts",
        )
        validated_conversion_counts = {
            "initial": require_int(
                conversion_counts["initial"],
                "private PDF readback conversion_counts.initial",
                0,
                20,
            ),
            "resumed": require_int(
                conversion_counts["resumed"],
                "private PDF readback conversion_counts.resumed",
                0,
                20,
            ),
            "rerun": require_int(
                conversion_counts["rerun"],
                "private PDF readback conversion_counts.rerun",
                0,
                0,
            ),
        }

        results = report["results"]
        if not isinstance(results, list) or not 1 <= len(results) <= 20:
            raise _fail("private PDF readback results are invalid")
        validated_results: list[dict[str, Any]] = []
        item_ids: set[str] = set()
        candidate_ids: set[str] = set()
        created = unchanged = rejected = 0
        for item in results:
            success = _require_success_item(item)
            if success is not None:
                if success["candidate_id"] in candidate_ids:
                    raise _fail("private PDF readback contains duplicate candidate identities")
                candidate_ids.add(success["candidate_id"])
                created += success["status"] == "created"
                unchanged += success["status"] == "unchanged"
                validated_item = success
            else:
                rejected_item = _require_rejected_item(item)
                if rejected_item is None:
                    raise _fail("private PDF readback result union is invalid")
                rejected += 1
                validated_item = rejected_item
            if validated_item["item_id"] in item_ids:
                raise _fail("private PDF readback contains duplicate item identities")
            item_ids.add(validated_item["item_id"])
            validated_results.append(validated_item)
        successful = created + unchanged
        if [item["item_id"] for item in validated_results] != validated_expected_item_ids:
            raise _fail("private PDF readback results must match the caller-supplied manifest item order")

        if (
            validated_counts["total"] != len(validated_results)
            or validated_counts["processed"] != len(validated_results)
            or validated_counts["created"] != created
            or validated_counts["unchanged"] != unchanged
            or validated_counts["rejected"] != rejected
            or validated_counts["successful"] != successful
            or validated_counts["candidate_queue"] != successful
            or validated_conversion_counts["initial"] + validated_conversion_counts["resumed"]
            != len(validated_results)
            or report["brain_before_digest"] != report["brain_after_digest"]
            or report["lifecycle_status"] != ("completed" if rejected == 0 else "partial")
        ):
            raise _fail("private PDF readback arithmetic is invalid")

        aggregate = report["aggregate"]
        if not isinstance(aggregate, Mapping):
            raise _fail("private PDF readback aggregate must be an object")
        require_exact_keys(
            aggregate,
            (
                "metrics",
                "exclusions",
                "stable_failures",
                "latency_seconds",
                "peak_memory_bytes",
                "repeatability",
            ),
            "private PDF readback aggregate",
        )
        metrics = aggregate["metrics"]
        if not isinstance(metrics, Mapping):
            raise _fail("private PDF readback aggregate.metrics must be an object")
        unknown_metrics = set(metrics) - set(_METRIC_NAMES)
        if unknown_metrics:
            raise _fail("private PDF readback aggregate.metrics is invalid")
        validated_metrics = {
            name: _require_metric_score(value, f"private PDF readback aggregate.metrics.{name}")
            for name, value in metrics.items()
        }
        exclusions = aggregate["exclusions"]
        if not isinstance(exclusions, list) or len(exclusions) > len(_METRIC_NAMES):
            raise _fail("private PDF readback aggregate.exclusions is invalid")
        exclusion_values: list[str] = []
        exclusion_seen: set[str] = set()
        for item in exclusions:
            if item not in _METRIC_NAMES or item in exclusion_seen:
                raise _fail("private PDF readback aggregate.exclusions is invalid")
            exclusion_seen.add(item)
            exclusion_values.append(item)
        full_partition = set(validated_metrics) | exclusion_seen
        if set(validated_metrics).intersection(exclusion_seen) or full_partition != set(_METRIC_NAMES):
            raise _fail("private PDF readback aggregate metric partition is invalid")

        stable_failures = aggregate["stable_failures"]
        if not isinstance(stable_failures, Mapping):
            raise _fail("private PDF readback aggregate.stable_failures must be an object")
        require_exact_keys(
            stable_failures,
            _FAILURE_CODES,
            "private PDF readback aggregate.stable_failures",
        )
        validated_failures = {
            code: require_int(
                stable_failures[code],
                f"private PDF readback aggregate.stable_failures.{code}",
                0,
                20,
            )
            for code in _FAILURE_CODES
        }
        if sum(validated_failures.values()) != rejected:
            raise _fail("private PDF readback aggregate rejection counts drift")
        if not validated_metrics and exclusion_seen != set(_METRIC_NAMES):
            raise _fail("private PDF readback aggregate metric partition is invalid")
        if not validated_metrics and successful != 0:
            raise _fail("private PDF readback aggregate.metrics may be empty only for fully rejected runs")

        repeatability = aggregate["repeatability"]
        if not isinstance(repeatability, Mapping):
            raise _fail("private PDF readback aggregate.repeatability must be an object")
        require_exact_keys(
            repeatability,
            ("runs", "identical", "score"),
            "private PDF readback aggregate.repeatability",
        )
        validated_repeatability = {
            "runs": require_int(
                repeatability["runs"],
                "private PDF readback aggregate.repeatability.runs",
                2,
                20,
            ),
            "identical": require_bool(
                repeatability["identical"],
                "private PDF readback aggregate.repeatability.identical",
            ),
            "score": _require_metric_score(
                repeatability["score"],
                "private PDF readback aggregate.repeatability.score",
            ),
        }
        if validated_repeatability["identical"] and validated_repeatability["score"] != 1.0:
            raise _fail("private PDF readback repeatability must prove an exact rerun")

        report["counts"] = validated_counts
        report["conversion_counts"] = validated_conversion_counts
        report["results"] = validated_results
        report["aggregate"] = {
            "metrics": validated_metrics,
            "exclusions": exclusion_values,
            "stable_failures": validated_failures,
            "latency_seconds": _require_summary(
                aggregate["latency_seconds"],
                "private PDF readback aggregate.latency_seconds",
            ),
            "peak_memory_bytes": _require_summary(
                aggregate["peak_memory_bytes"],
                "private PDF readback aggregate.peak_memory_bytes",
            ),
            "repeatability": validated_repeatability,
        }
        return report
    except ContractError as exc:
        if isinstance(exc, PrivatePdfUatError):
            raise
        raise _fail(str(exc)) from exc


PRIVATE_PDF_UAT_POLICY = PrivateDocumentCampaignPolicy(
    format_id="pdf",
    corpus_id=PRIVATE_PDF_UAT_CORPUS_ID,
    media_type="application/pdf",
    extension=".pdf",
    parser_id="docling",
    parser_version="2.118.1",
    worker_module="ao_lore.private_pdf_uat_worker",
    calibration_worker_module="ao_lore.private_pdf_calibration_worker",
    evidence_namespace="private-pdf-uat",
    validate_manifest=validate_private_pdf_manifest,
    validate_qualification=lambda value: value,
    validate_calibration=lambda value: value,
)
PRIVATE_PDF_UAT_POLICY_DIGEST = private_document_policy_digest(
    PRIVATE_PDF_UAT_POLICY
)


@dataclass(frozen=True)
class PrivatePdfCampaignOrigin:
    """Detached campaign identity retained across private worker boundaries."""

    corpus_id: str
    corpus_digest: str
    representative_domain: bool
    provenance_digest: str | None
    transformation_id: str | None
    source_digests: tuple[str, ...]
    derived_digests: tuple[str, ...]
    page_counts: tuple[int, ...]


def _campaign_origin_value(origin: PrivatePdfCampaignOrigin) -> dict[str, Any]:
    if type(origin) is not PrivatePdfCampaignOrigin:
        raise _fail("private PDF campaign origin is invalid")
    return {
        "schema_version": "ao.lore.private-pdf-campaign-origin.v0.1",
        "corpus_id": origin.corpus_id,
        "corpus_digest": origin.corpus_digest,
        "representative_domain": origin.representative_domain,
        "provenance_digest": origin.provenance_digest,
        "transformation_id": origin.transformation_id,
        "source_digests": list(origin.source_digests),
        "derived_digests": list(origin.derived_digests),
        "page_counts": list(origin.page_counts),
        "authority": False,
    }


def _validate_campaign_origin(
    value: Mapping[str, Any],
    manifest: Mapping[str, Any],
    expected: PrivatePdfCampaignOrigin | None = None,
) -> PrivatePdfCampaignOrigin:
    try:
        supplied = _strict_copy(value)
        require_exact_keys(
            supplied,
            (
                "schema_version",
                "corpus_id",
                "corpus_digest",
                "representative_domain",
                "provenance_digest",
                "transformation_id",
                "source_digests",
                "derived_digests",
                "page_counts",
                "authority",
            ),
            "private PDF campaign origin",
        )
        corpus = validate_private_pdf_manifest(manifest)
        if (
            supplied["schema_version"]
            != "ao.lore.private-pdf-campaign-origin.v0.1"
            or supplied["corpus_id"] != corpus["corpus_id"]
            or supplied["corpus_digest"] != canonical_digest(corpus)
            or type(supplied["representative_domain"]) is not bool
            or supplied["authority"] is not False
        ):
            raise _fail("private PDF campaign origin is invalid")
        source_digests = supplied["source_digests"]
        derived_digests = supplied["derived_digests"]
        page_counts = supplied["page_counts"]
        if (
            not isinstance(source_digests, list)
            or not isinstance(derived_digests, list)
            or not isinstance(page_counts, list)
            or any(not _DIGEST_RE.fullmatch(str(item)) for item in source_digests)
            or any(not _DIGEST_RE.fullmatch(str(item)) for item in derived_digests)
            or len(source_digests) != len(set(source_digests))
            or len(derived_digests) != len(set(derived_digests))
        ):
            raise _fail("private PDF campaign origin is invalid")
        manifest_digests = [
            document["source_digest"] for document in corpus["documents"]
        ]
        if supplied["representative_domain"]:
            if (
                supplied["corpus_id"] != DOMAIN_CORPUS_ID
                or supplied["provenance_digest"]
                != _DOMAIN_CORPUS_SPEC.provenance_digest
                or supplied["transformation_id"]
                != _DOMAIN_CORPUS_SPEC.transformation_id
                or len(source_digests) != len(corpus["documents"])
                or derived_digests != manifest_digests
                or len(page_counts) != len(corpus["documents"])
                or any(type(item) is not int or item < 1 for item in page_counts)
                or any(
                    source == derived
                    for source, derived in zip(
                        source_digests, derived_digests, strict=True
                    )
                )
                or [item["item_id"] for item in corpus["documents"]]
                != list(_DOMAIN_CORPUS_SPEC.item_ids)
            ):
                raise _fail("private PDF campaign origin is invalid")
        elif (
            supplied["corpus_id"] != _CORPUS_ID
            or supplied["provenance_digest"] is not None
            or supplied["transformation_id"] is not None
            or source_digests != manifest_digests
            or derived_digests != manifest_digests
            or page_counts != []
        ):
            raise _fail("private PDF campaign origin is invalid")
        result = PrivatePdfCampaignOrigin(
            corpus_id=supplied["corpus_id"],
            corpus_digest=supplied["corpus_digest"],
            representative_domain=supplied["representative_domain"],
            provenance_digest=supplied["provenance_digest"],
            transformation_id=supplied["transformation_id"],
            source_digests=tuple(source_digests),
            derived_digests=tuple(derived_digests),
            page_counts=tuple(page_counts),
        )
        if expected is not None and result != expected:
            raise _fail("private PDF campaign origin is invalid")
        return result
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivatePdfUatError):
            raise
        raise _fail("private PDF campaign origin is invalid") from exc


def _campaign_origin_digest(origin: PrivatePdfCampaignOrigin) -> str:
    return canonical_digest(_campaign_origin_value(origin))


def _qualification_corpus_digest(
    manifest: Mapping[str, Any],
    origin: PrivatePdfCampaignOrigin | None = None,
) -> str:
    """Bind qualification to the exact benchmark projection of one corpus."""

    if origin is None or origin.representative_domain is not True:
        return _QUALIFIED_PDF_CORPUS_DIGEST
    return canonical_digest(_private_benchmark_corpus(manifest))


def _representative_campaign_origin(
    review: ReviewedDomainCorpus, manifest: Mapping[str, Any]
) -> PrivatePdfCampaignOrigin:
    if type(review) is not ReviewedDomainCorpus:
        raise _fail("private PDF campaign origin is invalid")
    value = {
        "schema_version": "ao.lore.private-pdf-campaign-origin.v0.1",
        "corpus_id": review.corpus_id,
        "corpus_digest": canonical_digest(manifest),
        "representative_domain": True,
        "provenance_digest": review.provenance_digest,
        "transformation_id": review.transformation_id,
        "source_digests": [item.source_digest for item in review.documents],
        "derived_digests": [item.derived_digest for item in review.documents],
        "page_counts": [item.page_count for item in review.documents],
        "authority": False,
    }
    return _validate_campaign_origin(value, manifest)


def _seed_campaign_origin(
    manifest: Mapping[str, Any],
) -> PrivatePdfCampaignOrigin:
    corpus = validate_private_pdf_manifest(manifest)
    digests = [item["source_digest"] for item in corpus["documents"]]
    return _validate_campaign_origin(
        {
            "schema_version": "ao.lore.private-pdf-campaign-origin.v0.1",
            "corpus_id": corpus["corpus_id"],
            "corpus_digest": canonical_digest(corpus),
            "representative_domain": False,
            "provenance_digest": None,
            "transformation_id": None,
            "source_digests": digests,
            "derived_digests": digests,
            "page_counts": [],
            "authority": False,
        },
        corpus,
    )


@dataclass(frozen=True)
class PrivatePdfUatPreparedRun:
    """Internal digest bindings for one isolated private operator run."""

    corpus_manifest: Mapping[str, Any]
    corpus_digest: str
    qualification_digest: str
    qualification_manifest: Mapping[str, Any]
    qualification_body: bytes
    qualification_locator: Path
    qualification_identity: tuple[int, int]
    batch_manifest: Mapping[str, Any]
    batch_manifest_digest: str
    manifest_locator: str
    runtime_root: Path
    corpus_root: Path
    corpus_root_identity: tuple[int, int]
    staging_control_identity: tuple[int, int]
    staging_source_identity: tuple[int, int]
    staging_name: str
    conversion_journal: Path
    conversion_journal_identity: tuple[int, int]
    sealed_cache_root: Path
    sealed_cache_container: Path
    sealed_cache_root_identity: tuple[int, int]
    sealed_cache_tree_manifest: Mapping[str, Any]
    sealed_cache_tree_digest: str
    sealed_cache_file_count: int
    sealed_cache_total_bytes: int
    origin: PrivatePdfCampaignOrigin


@dataclass(frozen=True)
class PrivatePdfCalibrationEvidence:
    """Internal origin bindings for a six-key private calibration aggregate."""

    manifest_digest: str
    corpus_digest: str
    configuration_digest: str
    qualification_digest: str
    result_digest: str
    aggregate: Mapping[str, Any]
    sealed_cache_tree_digest: str
    sealed_cache_file_count: int
    sealed_cache_total_bytes: int
    sandbox_digest: str
    sandbox_verified: bool
    network_isolated: bool
    campaign_origin_digest: str


def _sealed_cache_from_prepared_run(
    run: PrivatePdfUatPreparedRun,
) -> PrivatePdfSealedModelCache:
    return PrivatePdfSealedModelCache(
        root=run.sealed_cache_root,
        container=run.sealed_cache_container,
        root_identity=run.sealed_cache_root_identity,
        tree_manifest=run.sealed_cache_tree_manifest,
        tree_digest=run.sealed_cache_tree_digest,
        file_count=run.sealed_cache_file_count,
        total_bytes=run.sealed_cache_total_bytes,
    )


@dataclass(frozen=True)
class PrivatePdfUatDependencies:
    """Explicit orchestration seams used by the operator harness and its tests."""

    load_manifest: Callable[[Path], Mapping[str, Any]]
    prepare_run: Callable[[Mapping[str, Any], Path], PrivatePdfUatPreparedRun]
    brain_snapshot: Callable[[], str]
    candidate_snapshot: Callable[[], Mapping[str, Mapping[str, Any]]]
    conversion_snapshot: Callable[[], int]
    launch_process: Callable[[PrivatePdfUatPreparedRun, str], Any]
    process_poll: Callable[[Any], int | None]
    inspect_checkpoint: Callable[[PrivatePdfUatPreparedRun], Mapping[str, Any] | None]
    inspect_barrier: Callable[
        [PrivatePdfUatPreparedRun, Mapping[str, Any]], bool
    ]
    send_signal: Callable[[Any], None]
    terminate_process: Callable[[Any], None]
    wait_process: Callable[[Any], Mapping[str, Any]]
    load_final: Callable[[PrivatePdfUatPreparedRun], Mapping[str, Any] | None]
    load_queue: Callable[[], Sequence[Mapping[str, Any]]]
    evaluate_calibration: Callable[
        [Mapping[str, Any], Path], PrivatePdfCalibrationEvidence
    ]
    cleanup_run: Callable[[PrivatePdfUatPreparedRun], bool]
    monotonic: Callable[[], float]
    sleep: Callable[[float], None]
    checkpoint_timeout: float = 120.0
    poll_interval: float = 0.05


_UAT_STDOUT_MAX_BYTES = 1024 * 1024
_UAT_STDERR_MAX_BYTES = 1024 * 1024
_UAT_PROCESS_TIMEOUT_SECONDS = 1800.0
_CALIBRATION_PROCESS_TIMEOUT_SECONDS = 1500.0
_CALIBRATION_ITEM_TIMEOUT_SECONDS = 600.0
_UAT_BRAIN_MAX_FILES = 10_000
_UAT_BRAIN_MAX_FILE_BYTES = 16 * 1024 * 1024
_UAT_BRAIN_MAX_TOTAL_BYTES = 128 * 1024 * 1024
_UAT_BRAIN_MAX_DEPTH = 32
_UAT_MANIFEST_LOCATOR = "sources/.private-pdf-uat/run-manifest.json"
_UAT_RETAINED_READBACK = "private-pdf-uat-readback.json"
_UAT_CLEANED_STATE = "cleaned-state.json"
_UAT_EVIDENCE_SCHEMA_VERSION = "ao.lore.private-pdf-uat-cleaned-state.v0.1"
_UAT_EVIDENCE_FILES = frozenset({_UAT_RETAINED_READBACK, _UAT_CLEANED_STATE})
_BWRAP = "/usr/bin/bwrap"
_BWRAP_PROBE_ARGV = [
    _BWRAP,
    "--unshare-net",
    "--tmpfs", "/",
    "--ro-bind", "/usr", "/usr",
    "--ro-bind", "/etc", "/etc",
    "--symlink", "usr/bin", "/bin",
    "--symlink", "usr/lib", "/lib",
    "--symlink", "usr/lib64", "/lib64",
    "--symlink", "usr/sbin", "/sbin",
    "--dev", "/dev",
    "--proc", "/proc",
    "--tmpfs", "/tmp",
    "--",
    "/usr/bin/true",
]
_SANDBOX_SCHEMA_VERSION = "ao.lore.private-pdf-uat-sandbox.v0.1"
_CALIBRATION_CONTRACT_NAME = "calibration-run-contract.json"
_CALIBRATION_RESULT_NAME = "calibration-result.json"
_CALIBRATION_CONTRACT_SCHEMA_VERSION = (
    "ao.lore.private-pdf-calibration-run.v0.1"
)
_CALIBRATION_RESULT_SCHEMA_VERSION = (
    "ao.lore.private-pdf-calibration-result.v0.1"
)


@dataclass(frozen=True)
class _PrivatePdfOwnedArtifact:
    name: str
    identity: tuple[int, int]
    body: bytes


def _capture_calibration_artifact_at(
    parent_descriptor: int,
    name: str,
    maximum: int,
) -> _PrivatePdfOwnedArtifact:
    info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_nlink != 1
    ):
        raise OSError("private PDF calibration artifact binding is invalid")
    body = _read_file_at(parent_descriptor, name, maximum)
    rebound = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if _stable_metadata(info) != _stable_metadata(rebound):
        raise OSError("private PDF calibration artifact binding drifted")
    return _PrivatePdfOwnedArtifact(
        name=name,
        identity=(info.st_dev, info.st_ino),
        body=body,
    )


def _unlink_owned_calibration_artifact_at(
    parent_descriptor: int,
    artifact: _PrivatePdfOwnedArtifact | None,
    maximum: int,
) -> bool:
    if artifact is None:
        return False
    try:
        observed = _capture_calibration_artifact_at(
            parent_descriptor, artifact.name, maximum
        )
    except FileNotFoundError:
        return False
    if observed != artifact:
        return False
    os.unlink(artifact.name, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
    return True


def _publish_calibration_artifact_at(
    parent_descriptor: int,
    name: str,
    body: bytes,
    maximum: int,
) -> _PrivatePdfOwnedArtifact:
    if len(body) > maximum:
        raise OSError("private PDF calibration artifact exceeds bound")
    temporary = f".{name}-{secrets.token_hex(12)}.tmp"
    file_descriptor: int | None = None
    temporary_identity: tuple[int, int] | None = None
    published_artifact: _PrivatePdfOwnedArtifact | None = None
    try:
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(file_descriptor)
        temporary_identity = (opened.st_dev, opened.st_ino)
        view = memoryview(body)
        while view:
            written = os.write(file_descriptor, view)
            if written <= 0:
                raise OSError("private PDF calibration artifact short write")
            view = view[written:]
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        _rename_noreplace_at(
            parent_descriptor, temporary, parent_descriptor, name
        )
        if temporary_identity is None:
            raise OSError("private PDF calibration artifact identity is absent")
        published_artifact = _PrivatePdfOwnedArtifact(
            name=name,
            identity=temporary_identity,
            body=body,
        )
        temporary_identity = None
        os.fsync(parent_descriptor)
        artifact = _capture_calibration_artifact_at(
            parent_descriptor, name, maximum
        )
        if artifact.body != body:
            raise OSError("private PDF calibration artifact reopen drifted")
        return artifact
    except BaseException:
        if published_artifact is not None:
            try:
                _unlink_owned_calibration_artifact_at(
                    parent_descriptor, published_artifact, maximum
                )
            except BaseException:
                pass
        raise
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_identity is not None:
            try:
                current = os.stat(
                    temporary,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (current.st_dev, current.st_ino) == temporary_identity:
                    os.unlink(temporary, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass


def _validate_uat_dependencies(value: Any) -> PrivatePdfUatDependencies:
    if not isinstance(value, PrivatePdfUatDependencies):
        raise _fail("private PDF UAT dependencies are invalid")
    callbacks = (
        value.load_manifest,
        value.prepare_run,
        value.brain_snapshot,
        value.candidate_snapshot,
        value.conversion_snapshot,
        value.launch_process,
        value.process_poll,
        value.inspect_checkpoint,
        value.inspect_barrier,
        value.send_signal,
        value.terminate_process,
        value.wait_process,
        value.load_final,
        value.load_queue,
        value.evaluate_calibration,
        value.cleanup_run,
        value.monotonic,
        value.sleep,
    )
    if any(not callable(callback) for callback in callbacks):
        raise _fail("private PDF UAT dependencies are invalid")
    for number in (value.checkpoint_timeout, value.poll_interval):
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise _fail("private PDF UAT dependencies are invalid")
    if value.checkpoint_timeout <= 0 or not 0 < value.poll_interval <= value.checkpoint_timeout:
        raise _fail("private PDF UAT dependencies are invalid")
    return value


def _validate_prepared_run(
    value: Any, manifest: Mapping[str, Any], runtime: Path
) -> PrivatePdfUatPreparedRun:
    if not isinstance(value, PrivatePdfUatPreparedRun):
        raise _fail("private PDF UAT prepared run is invalid")
    corpus = validate_private_pdf_manifest(value.corpus_manifest)
    supplied = validate_private_pdf_manifest(manifest)
    batch = validate_batch_manifest(value.batch_manifest)
    qualification = _strict_copy(value.qualification_manifest)
    try:
        verify_manifest(
            qualification,
            expected_corpus_digest=_qualification_corpus_digest(
                corpus, value.origin
            ),
            expected_configuration_digest=PRIVATE_PDF_CONFIGURATION_DIGEST,
        )
    except Exception as exc:
        raise _fail("private PDF UAT qualification is invalid") from exc
    expected_qualification = runtime / "benchmarks" / "docling-2.118.1.json"
    if (
        value.qualification_locator != expected_qualification
        or type(value.qualification_body) is not bytes
        or not value.qualification_body
        or len(value.qualification_body) > _MAX_QUALIFICATION_BYTES
    ):
        raise _fail("private PDF UAT qualification locator is invalid")
    try:
        retained_qualification, retained_body = strict_read_json(
            expected_qualification,
            "private PDF UAT qualification",
            max_bytes=_MAX_QUALIFICATION_BYTES,
            root=runtime,
        )
        info = os.stat(expected_qualification, follow_symlinks=False)
        if (
            retained_qualification != qualification
            or retained_body != value.qualification_body
            or (info.st_dev, info.st_ino) != value.qualification_identity
        ):
            raise OSError("qualification binding drifted")
    except (ContractError, OSError) as exc:
        raise _fail("private PDF UAT qualification binding is invalid") from exc
    if (
        corpus != supplied
        or value.corpus_digest != canonical_digest(corpus)
        or value.batch_manifest_digest != canonical_digest(batch)
        or value.manifest_locator != _UAT_MANIFEST_LOCATOR
        or value.qualification_digest != qualification.get("result_digest")
        or qualification.get("parser_id") != "docling"
        or qualification.get("parser_version") != "2.118.1"
        or qualification.get("failures") != []
        or [item["item_id"] for item in batch["documents"]]
        != [item["item_id"] for item in corpus["documents"]]
        or [item["source_digest"] for item in batch["documents"]]
        != [item["source_digest"] for item in corpus["documents"]]
    ):
        raise _fail("private PDF UAT prepared run is invalid")
    runtime_root, _ = ensure_contained(
        Path(value.runtime_root), repository_root(), "private PDF UAT runtime root"
    )
    if runtime_root != runtime:
        raise _fail("private PDF UAT prepared run is invalid")
    root, _ = ensure_contained(Path(value.corpus_root), runtime_root, "private PDF UAT corpus")
    if root != runtime_root / "calibration" / "private-pdf" / corpus["corpus_id"]:
        raise _fail("private PDF UAT prepared run is invalid")
    if re.fullmatch(r"private-pdf-uat-[0-9a-f]{24}", value.staging_name) is None:
        raise _fail("private PDF UAT staging identity is invalid")
    expected_state = (
        runtime_root / "uat" / "private-pdf" / batch["batch_id"]
    )
    try:
        sealed = _validate_sealed_private_pdf_model_cache(
            _sealed_cache_from_prepared_run(value)
        )
        if (
            sealed.container.parent != expected_state
            or re.fullmatch(r"sealed-cache-[0-9a-f]{24}", sealed.container.name)
            is None
            or sealed.root != sealed.container / "huggingface"
        ):
            raise OSError("sealed cache locator drifted")
    except (ContractError, OSError) as exc:
        raise _fail("private PDF UAT sealed model cache is invalid") from exc
    expected_sources = [
        f"sources/{value.staging_name}/{item['item_id']}.pdf"
        for item in corpus["documents"]
    ]
    if (
        any(set(item) != {"item_id", "source", "source_digest"} for item in batch["documents"])
        or [item["source"] for item in batch["documents"]] != expected_sources
    ):
        raise _fail("private PDF UAT prepared source mapping is invalid")
    try:
        retained, retained_body = strict_read_json(
            repository_root() / value.manifest_locator,
            "private PDF UAT staged manifest",
            max_bytes=_MAX_MANIFEST_BYTES,
            root=repository_root(),
        )
        if (
            validate_batch_manifest(retained) != batch
            or retained_body != _manifest_body(batch)
        ):
            raise OSError("staged manifest drifted")
        root_info = os.stat(root, follow_symlinks=False)
        if (
            not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(root_info.st_mode)
            or (root_info.st_dev, root_info.st_ino) != value.corpus_root_identity
        ):
            raise OSError("corpus root is invalid")
        control_root = repository_root() / "sources" / ".private-pdf-uat"
        control_descriptor = _open_directory(
            control_root, repository_root(), create=False
        )
        source_root = repository_root() / "sources" / value.staging_name
        descriptor: int | None = None
        try:
            descriptor = _open_directory(source_root, repository_root(), create=False)
            if (
                (os.fstat(control_descriptor).st_dev, os.fstat(control_descriptor).st_ino)
                != value.staging_control_identity
                or (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
                != value.staging_source_identity
            ):
                raise OSError("staging directory identity drifted")
            if set(os.listdir(descriptor)) != {
                f"{item['item_id']}.pdf" for item in corpus["documents"]
            }:
                raise OSError("staged source entries drifted")
            for document in corpus["documents"]:
                name = f"{document['item_id']}.pdf"
                body = _read_file_at(descriptor, name, MAX_PRIVATE_PDF_BYTES)
                if "sha256:" + hashlib.sha256(body).hexdigest() != document["source_digest"]:
                    raise OSError("staged source digest drifted")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(control_descriptor)
    except (ContractError, OSError) as exc:
        raise _fail("private PDF UAT staging is invalid") from exc
    _read_conversion_journal(value)
    return PrivatePdfUatPreparedRun(
        corpus_manifest=corpus,
        corpus_digest=value.corpus_digest,
        qualification_digest=value.qualification_digest,
        qualification_manifest=qualification,
        qualification_body=value.qualification_body,
        qualification_locator=value.qualification_locator,
        qualification_identity=value.qualification_identity,
        batch_manifest=batch,
        batch_manifest_digest=value.batch_manifest_digest,
        manifest_locator=value.manifest_locator,
        runtime_root=runtime_root,
        corpus_root=root,
        corpus_root_identity=value.corpus_root_identity,
        staging_control_identity=value.staging_control_identity,
        staging_source_identity=value.staging_source_identity,
        staging_name=value.staging_name,
        conversion_journal=value.conversion_journal,
        conversion_journal_identity=value.conversion_journal_identity,
        sealed_cache_root=sealed.root,
        sealed_cache_container=sealed.container,
        sealed_cache_root_identity=sealed.root_identity,
        sealed_cache_tree_manifest=sealed.tree_manifest,
        sealed_cache_tree_digest=sealed.tree_digest,
        sealed_cache_file_count=sealed.file_count,
        sealed_cache_total_bytes=sealed.total_bytes,
        origin=_validate_campaign_origin(
            _campaign_origin_value(value.origin), corpus
        ),
    )


def _require_snapshot(value: Any, label: str) -> str:
    try:
        return _require_digest(value, label)
    except ContractError as exc:
        raise _fail(f"{label} is invalid") from exc


def _candidate_snapshot(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or len(value) > 10_000:
        raise _fail("private PDF UAT candidate snapshot is invalid")
    result: dict[str, dict[str, Any]] = {}
    exact_keys = {
        "candidate_id", "candidate_digest", "provenance_digest",
        "source_digest", "review_status",
    }
    for candidate_id, supplied in value.items():
        identifier = require_identifier(candidate_id, "private PDF UAT candidate identity")
        if (
            not identifier.startswith("candidate-")
            or not isinstance(supplied, Mapping)
            or set(supplied) != exact_keys
            or supplied.get("candidate_id") != identifier
        ):
            raise _fail("private PDF UAT candidate snapshot is invalid")
        item = _strict_copy(supplied)
        for field in ("candidate_digest", "provenance_digest", "source_digest"):
            _require_digest(item[field], f"private PDF UAT candidate {field}")
        if item["review_status"] not in {"unreviewed", "accepted", "rejected"}:
            raise _fail("private PDF UAT candidate snapshot is invalid")
        result[identifier] = item
    return result


def _process_result(value: Any) -> tuple[int, bytes, bytes]:
    if not isinstance(value, Mapping) or set(value) != {"returncode", "stdout", "stderr"}:
        raise _fail("private PDF UAT process result is invalid")
    returncode = value["returncode"]
    stdout = value["stdout"]
    stderr = value["stderr"]
    if (
        isinstance(returncode, bool)
        or not isinstance(returncode, int)
        or not isinstance(stdout, bytes)
        or not isinstance(stderr, bytes)
        or len(stdout) > _UAT_STDOUT_MAX_BYTES
        or len(stderr) > _UAT_STDERR_MAX_BYTES
    ):
        raise _fail("private PDF UAT process result is invalid")
    return returncode, stdout, stderr


def _conversion_snapshot(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise _fail("private PDF UAT conversion observation is invalid")
    return value


def _read_conversion_journal(run: PrivatePdfUatPreparedRun) -> int:
    expected = (
        run.runtime_root
        / "uat"
        / "private-pdf"
        / run.batch_manifest["batch_id"]
        / "conversion-journal.jsonl"
    )
    if run.conversion_journal != expected:
        raise _fail("private PDF UAT conversion journal locator is invalid")
    parent = _open_directory(expected.parent, run.runtime_root, create=False)
    try:
        info = os.stat(expected.name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != run.conversion_journal_identity
        ):
            raise OSError("conversion journal binding drifted")
        body = _read_file_at(parent, expected.name, _MAX_MANIFEST_BYTES)
    except (OSError, ContractError) as exc:
        raise _fail("private PDF UAT conversion journal is invalid") from exc
    finally:
        os.close(parent)
    if not body:
        return 0
    lines = body.splitlines(keepends=True)
    if len(lines) > 20 or any(not line.endswith(b"\n") for line in lines):
        raise _fail("private PDF UAT conversion journal is invalid")
    previous: str | None = None
    for sequence, line in enumerate(lines, 1):
        value = parse_strict_json(line, "private PDF UAT conversion journal")
        if not isinstance(value, Mapping) or set(value) != {
            "schema_version", "sequence", "previous_event_digest", "event_digest"
        }:
            raise _fail("private PDF UAT conversion journal is invalid")
        if (
            value["schema_version"] != "ao.lore.private-pdf-conversion-event.v0.1"
            or value["sequence"] != sequence
            or value["previous_event_digest"] != previous
        ):
            raise _fail("private PDF UAT conversion journal is invalid")
        body_value = {key: child for key, child in value.items() if key != "event_digest"}
        if value["event_digest"] != canonical_digest(body_value):
            raise _fail("private PDF UAT conversion journal is invalid")
        previous = value["event_digest"]
    return len(lines)


def _terminal_stdout(body: bytes) -> dict[str, Any]:
    if not body or len(body) > _UAT_STDOUT_MAX_BYTES:
        raise _fail("private PDF UAT terminal output is invalid")
    try:
        value = parse_strict_json(body, "private PDF UAT terminal output")
        if not isinstance(value, Mapping):
            raise _fail("private PDF UAT terminal output is invalid")
        return validate_final_batch_readback(value)
    except (ContractError, ValueError, TypeError) as exc:
        if isinstance(exc, PrivatePdfUatError):
            raise
        raise _fail("private PDF UAT terminal output is invalid") from exc


def _verify_terminal(
    run: PrivatePdfUatPreparedRun,
    stdout: bytes,
    loaded: Mapping[str, Any] | None,
) -> dict[str, Any]:
    emitted = _terminal_stdout(stdout)
    if loaded is None:
        raise _fail("private PDF UAT terminal readback is absent")
    retained = validate_final_batch_readback(loaded)
    if (
        emitted != retained
        or retained["batch_id"] != run.batch_manifest["batch_id"]
        or retained["manifest_digest"] != run.batch_manifest_digest
        or [item["item_id"] for item in retained["items"]]
        != [item["item_id"] for item in run.batch_manifest["documents"]]
        or [item["source_digest"] for item in retained["items"]]
        != [item["source_digest"] for item in run.batch_manifest["documents"]]
    ):
        raise _fail("private PDF UAT terminal readback drifted")
    return retained


def _load_retained_private_uat_readback(
    run: PrivatePdfUatPreparedRun, expected_digest: str
) -> dict[str, Any]:
    _require_digest(expected_digest, "private PDF UAT retained readback digest")
    evidence_directory = (
        run.runtime_root / "evidence" / "private-pdf-uat"
        / run.batch_manifest["batch_id"]
    )
    try:
        evidence_descriptor = _open_directory(
            evidence_directory, run.runtime_root, create=False
        )
    except FileNotFoundError:
        # Compatibility is deliberately read-only. New evidence is never
        # written into the cleanup-owned legacy run-state directory. Legacy
        # files predate the post-cleanup brain binding, so even byte-valid
        # content is not terminal evidence.
        try:
            value, body = strict_read_json(
                run.conversion_journal.parent / _UAT_RETAINED_READBACK,
                "private PDF UAT retained readback",
                max_bytes=_MAX_MANIFEST_BYTES,
                root=run.runtime_root,
            )
            if "sha256:" + hashlib.sha256(body).hexdigest() != expected_digest:
                raise OSError("legacy readback digest drifted")
            validate_private_pdf_readback(
                value,
                expected_item_ids=[
                    item["item_id"] for item in run.corpus_manifest["documents"]
                ],
            )
            raise _fail("private PDF UAT legacy readback is unverified")
        except (ContractError, OSError, ValueError, TypeError) as exc:
            if isinstance(exc, PrivatePdfUatError):
                raise
            raise _fail("private PDF UAT retained readback drifted") from exc
    except OSError as exc:
        raise _fail("private PDF UAT retained readback drifted") from exc
    try:
        return _load_canonical_private_uat_evidence_at(
            evidence_descriptor, run, expected_digest
        )
    except (ContractError, OSError, ValueError, TypeError) as exc:
        if isinstance(exc, PrivatePdfUatError):
            raise
        raise _fail("private PDF UAT retained readback drifted") from exc
    finally:
        os.close(evidence_descriptor)


def _validate_cleaned_evidence_state(
    value: Any,
    run: PrivatePdfUatPreparedRun,
    readback: Mapping[str, Any],
    readback_digest: str,
) -> dict[str, Any]:
    exact = {
        "schema_version", "batch_id", "batch_manifest_digest", "corpus_id",
        "corpus_digest", "readback_digest", "brain_before_digest",
        "brain_post_run_digest", "brain_post_cleanup_digest",
        "campaign_origin_digest", "lifecycle_status", "authority",
    }
    if not isinstance(value, Mapping) or set(value) != exact:
        raise OSError("private PDF UAT cleaned evidence contract drifted")
    state = _strict_copy(value)
    for field in (
        "batch_manifest_digest", "corpus_digest", "readback_digest",
        "brain_before_digest", "brain_post_run_digest",
        "brain_post_cleanup_digest", "campaign_origin_digest",
    ):
        _require_digest(state[field], f"private PDF UAT cleaned evidence {field}")
    brain = state["brain_before_digest"]
    if (
        state["schema_version"] != _UAT_EVIDENCE_SCHEMA_VERSION
        or state["batch_id"] != run.batch_manifest["batch_id"]
        or state["batch_manifest_digest"] != run.batch_manifest_digest
        or state["corpus_id"] != run.corpus_manifest["corpus_id"]
        or state["corpus_digest"] != run.corpus_digest
        or state["campaign_origin_digest"]
        != _campaign_origin_digest(run.origin)
        or state["readback_digest"] != readback_digest
        or state["lifecycle_status"] != "cleaned"
        or state["authority"] is not False
        or state["brain_post_run_digest"] != brain
        or state["brain_post_cleanup_digest"] != brain
        or readback["corpus_id"] != state["corpus_id"]
        or readback["corpus_digest"] != state["corpus_digest"]
        or readback["campaign_origin_digest"]
        != state["campaign_origin_digest"]
        or readback["brain_before_digest"] != brain
        or readback["brain_after_digest"] != brain
    ):
        raise OSError("private PDF UAT cleaned evidence binding drifted")
    return state


def _load_canonical_private_uat_evidence_at(
    directory_descriptor: int,
    run: PrivatePdfUatPreparedRun,
    expected_digest: str,
) -> dict[str, Any]:
    if set(os.listdir(directory_descriptor)) != _UAT_EVIDENCE_FILES:
        raise OSError("private PDF UAT evidence entries drifted")
    readback_body = _read_file_at(
        directory_descriptor, _UAT_RETAINED_READBACK, _MAX_MANIFEST_BYTES
    )
    if "sha256:" + hashlib.sha256(readback_body).hexdigest() != expected_digest:
        raise OSError("private PDF UAT evidence digest drifted")
    readback_value = parse_strict_json(
        readback_body, "private PDF UAT retained readback"
    )
    if not isinstance(readback_value, Mapping):
        raise OSError("private PDF UAT retained readback is invalid")
    readback = validate_private_pdf_readback(
        readback_value,
        expected_item_ids=[
            item["item_id"] for item in run.corpus_manifest["documents"]
        ],
    )
    cleaned_body = _read_file_at(
        directory_descriptor, _UAT_CLEANED_STATE, _MAX_MANIFEST_BYTES
    )
    cleaned_value = parse_strict_json(
        cleaned_body, "private PDF UAT cleaned evidence state"
    )
    _validate_cleaned_evidence_state(
        cleaned_value, run, readback, expected_digest
    )
    return readback


def _write_unpublished_evidence_file(
    directory_descriptor: int, name: str, body: bytes
) -> None:
    descriptor: int | None = None
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("private PDF UAT evidence file is invalid")
        identity = (info.st_dev, info.st_ino)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("private PDF UAT evidence short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if _read_file_at(directory_descriptor, name, _MAX_MANIFEST_BYTES) != body:
            raise OSError("private PDF UAT evidence reopen drifted")
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
            descriptor = None
        if identity is not None:
            try:
                current = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
                if (
                    stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == identity
                ):
                    os.unlink(name, dir_fd=directory_descriptor)
                    os.fsync(directory_descriptor)
            except FileNotFoundError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _persist_retained_private_uat_readback(
    run: PrivatePdfUatPreparedRun,
    report: Mapping[str, Any],
    *,
    brain_post_run_digest: str,
    brain_post_cleanup_digest: str,
) -> str:
    validated = validate_private_pdf_readback(
        report,
        expected_item_ids=[
            item["item_id"] for item in run.corpus_manifest["documents"]
        ],
    )
    if validated["corpus_id"] != run.corpus_manifest["corpus_id"]:
        raise _fail("private PDF UAT retained readback identity is invalid")
    readback_body = _manifest_body(validated)
    readback_digest = "sha256:" + hashlib.sha256(readback_body).hexdigest()
    cleaned_state = _validate_cleaned_evidence_state(
        {
            "schema_version": _UAT_EVIDENCE_SCHEMA_VERSION,
            "batch_id": run.batch_manifest["batch_id"],
            "batch_manifest_digest": run.batch_manifest_digest,
            "corpus_id": run.corpus_manifest["corpus_id"],
            "corpus_digest": run.corpus_digest,
            "readback_digest": readback_digest,
            "brain_before_digest": validated["brain_before_digest"],
            "brain_post_run_digest": brain_post_run_digest,
            "brain_post_cleanup_digest": brain_post_cleanup_digest,
            "campaign_origin_digest": _campaign_origin_digest(run.origin),
            "lifecycle_status": "cleaned",
            "authority": False,
        },
        run,
        validated,
        readback_digest,
    )
    cleaned_body = _manifest_body(cleaned_state)
    evidence_parent = run.runtime_root / "evidence" / "private-pdf-uat"
    parent_descriptor = _open_directory(
        evidence_parent, run.runtime_root, create=True
    )
    staging_name = f".pending-{secrets.token_hex(16)}"
    staging_descriptor: int | None = None
    staging_identity: tuple[int, int] | None = None
    published = False
    try:
        os.mkdir(staging_name, 0o700, dir_fd=parent_descriptor)
        staging_descriptor = os.open(
            staging_name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
        )
        staging_info = os.fstat(staging_descriptor)
        staging_identity = (staging_info.st_dev, staging_info.st_ino)
        _write_unpublished_evidence_file(
            staging_descriptor, _UAT_RETAINED_READBACK, readback_body
        )
        _write_unpublished_evidence_file(
            staging_descriptor, _UAT_CLEANED_STATE, cleaned_body
        )
        _load_canonical_private_uat_evidence_at(
            staging_descriptor, run, readback_digest
        )
        os.fsync(staging_descriptor)
        _rename_noreplace_at(
            parent_descriptor,
            staging_name,
            parent_descriptor,
            run.batch_manifest["batch_id"],
        )
        published = True
        os.fsync(parent_descriptor)
        final_info = os.stat(
            run.batch_manifest["batch_id"],
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(final_info.st_mode)
            or (final_info.st_dev, final_info.st_ino) != staging_identity
        ):
            raise OSError("private PDF UAT evidence publication drifted")
        _load_canonical_private_uat_evidence_at(
            staging_descriptor, run, readback_digest
        )
    except BaseException:
        if published and staging_identity is not None:
            try:
                final_info = os.stat(
                    run.batch_manifest["batch_id"],
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISDIR(final_info.st_mode)
                    and (final_info.st_dev, final_info.st_ino)
                    == staging_identity
                ):
                    rollback_name = f".rollback-{secrets.token_hex(16)}"
                    _rename_noreplace_at(
                        parent_descriptor,
                        run.batch_manifest["batch_id"],
                        parent_descriptor,
                        rollback_name,
                    )
                    staging_name = rollback_name
                    published = False
                    os.fsync(parent_descriptor)
            except FileNotFoundError:
                published = False
        if staging_descriptor is None and not published:
            try:
                staging_descriptor = os.open(
                    staging_name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor
                )
            except FileNotFoundError:
                pass
        if staging_descriptor is not None and not published:
            for name, body in (
                (_UAT_RETAINED_READBACK, readback_body),
                (_UAT_CLEANED_STATE, cleaned_body),
            ):
                try:
                    _unlink_verified_at(
                        staging_descriptor, name, body, _MAX_MANIFEST_BYTES
                    )
                except FileNotFoundError:
                    pass
            if not os.listdir(staging_descriptor):
                os.close(staging_descriptor)
                staging_descriptor = None
                os.rmdir(staging_name, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
        raise
    finally:
        if staging_descriptor is not None:
            os.close(staging_descriptor)
        os.close(parent_descriptor)
    return readback_digest


def _verify_queue(
    values: Any,
    terminal: Mapping[str, Any],
    before: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or len(values) > 10_000:
        raise _fail("private PDF UAT candidate queue is invalid")
    queue: dict[str, dict[str, Any]] = {}
    exact_keys = {
        "candidate_id", "candidate_digest", "provenance_digest", "source_digest", "review_status"
    }
    for supplied in values:
        if not isinstance(supplied, Mapping) or set(supplied) != exact_keys:
            raise _fail("private PDF UAT candidate queue is invalid")
        item = _strict_copy(supplied)
        candidate_id = require_identifier(item["candidate_id"], "private PDF UAT queue candidate")
        if candidate_id in queue or not candidate_id.startswith("candidate-"):
            raise _fail("private PDF UAT candidate queue is invalid")
        for field in ("candidate_digest", "provenance_digest", "source_digest"):
            _require_digest(item[field], f"private PDF UAT queue {field}")
        if item["review_status"] not in {"unreviewed", "accepted", "rejected"}:
            raise _fail("private PDF UAT queue review status is invalid")
        queue[candidate_id] = item
    results: list[dict[str, Any]] = []
    expected_success_ids: set[str] = set()
    for terminal_item in terminal["items"]:
        if terminal_item["status"] == "rejected":
            code = {
                "source_digest_mismatch": "digest_mismatch",
                "source_invalid_pdf": "invalid_pdf",
                "parser_rejected": "conversion_failed",
            }.get(terminal_item["error_code"], terminal_item["error_code"])
            if code not in _FAILURE_CODES:
                raise _fail("private PDF UAT batch rejection is not declared")
            results.append({
                "item_id": terminal_item["item_id"],
                "source_digest": terminal_item["source_digest"],
                "status": "rejected",
                "error_code": code,
            })
            continue
        candidate_id = terminal_item["candidate_id"]
        expected_success_ids.add(candidate_id)
        queued = queue.get(candidate_id)
        expected = {
            key: terminal_item[key]
            for key in ("candidate_id", "candidate_digest", "provenance_digest", "source_digest")
        }
        if not (
            queued is not None
            and {key: queued[key] for key in expected} == expected
            and queued["review_status"] == terminal_item["review_status"]
        ):
            raise _fail("private PDF UAT candidate queue drifted")
        prior = before.get(candidate_id)
        if (
            terminal_item["status"] == "created"
            and (prior is not None or queued["review_status"] != "unreviewed")
        ) or (
            terminal_item["status"] == "unchanged" and prior != queued
        ):
            raise _fail("private PDF UAT candidate outcome drifted")
        results.append({**terminal_item, "review_status": queued["review_status"]})
    newly_visible = set(queue) - set(before)
    created_ids = {
        item["candidate_id"]
        for item in terminal["items"]
        if item["status"] == "created"
    }
    if (
        newly_visible != created_ids
        or set(queue).intersection(before) != set(before)
        or any(queue[candidate_id] != item for candidate_id, item in before.items())
        or expected_success_ids != {
            item["candidate_id"]
            for item in terminal["items"]
            if item["status"] != "rejected"
        }
    ):
        raise _fail("private PDF UAT candidate queue contains foreign results")
    return results


def _assemble_private_uat_readback(
    run: PrivatePdfUatPreparedRun,
    terminal: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    brain_digest: str,
    calibration: PrivatePdfCalibrationEvidence,
    conversion_counts: Mapping[str, int],
) -> dict[str, Any]:
    results = terminal["items"]
    failures = {code: 0 for code in _FAILURE_CODES}
    for item in results:
        if item["status"] == "rejected":
            failures[item["error_code"]] += 1
    if not isinstance(calibration, PrivatePdfCalibrationEvidence):
        raise _fail("private PDF UAT calibration origin is invalid")
    aggregate_value = _validate_calibration_aggregate(calibration.aggregate)
    if (
        calibration.manifest_digest != canonical_digest(run.corpus_manifest)
        or calibration.corpus_digest != run.corpus_digest
        or calibration.configuration_digest != PRIVATE_PDF_CONFIGURATION_DIGEST
        or calibration.qualification_digest != run.qualification_digest
        or calibration.result_digest != canonical_digest(aggregate_value)
        or calibration.sealed_cache_tree_digest
        != run.sealed_cache_tree_digest
        or calibration.sealed_cache_file_count
        != run.sealed_cache_file_count
        or calibration.sealed_cache_total_bytes
        != run.sealed_cache_total_bytes
        or not _DIGEST_RE.fullmatch(calibration.sandbox_digest)
        or calibration.sandbox_verified is not True
        or calibration.network_isolated is not True
        or calibration.campaign_origin_digest
        != _campaign_origin_digest(run.origin)
    ):
        raise _fail("private PDF UAT calibration origin drifted")
    if aggregate_value["stable_failures"] != failures:
        raise _fail("private PDF UAT calibration rejection binding drifted")
    created = sum(item["status"] == "created" for item in results)
    unchanged = sum(item["status"] == "unchanged" for item in results)
    rejected = sum(item["status"] == "rejected" for item in results)
    value: dict[str, Any] = {
        "schema_version": "ao.lore.private-pdf-uat-readback.v0.1",
        "corpus_id": run.corpus_manifest["corpus_id"],
        "corpus_digest": run.corpus_digest,
        "configuration_digest": PRIVATE_PDF_CONFIGURATION_DIGEST,
        "qualification_digest": run.qualification_digest,
        "aggregate_digest": canonical_digest(aggregate_value),
        "campaign_origin_digest": _campaign_origin_digest(run.origin),
        "uat_digest": canonical_digest({
            "batch_manifest_digest": run.batch_manifest_digest,
            "terminal": terminal,
            "checkpoint_digest": checkpoint["checkpoint_digest"],
        }),
        "lifecycle_status": "completed" if rejected == 0 else "partial",
        "tuning_decision": derive_tuning_decision(
            aggregate_value,
            representative_domain=run.origin.representative_domain,
        ),
        "qualified": False,
        "counts": {
            "total": len(results), "created": created, "unchanged": unchanged,
            "rejected": rejected, "processed": len(results),
            "successful": created + unchanged, "candidate_queue": created + unchanged,
            "interrupted": 1, "resumed": 1, "rerun": 1,
        },
        "conversion_counts": dict(conversion_counts),
        "brain_before_digest": brain_digest,
        "brain_after_digest": brain_digest,
        "results": results,
        "aggregate": aggregate_value,
        "ocr_enabled": False,
        "ocr_used": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }
    return validate_private_pdf_readback(
        value,
        expected_item_ids=[item["item_id"] for item in run.corpus_manifest["documents"]],
    )


def _private_document_prepared_run(
    run: PrivatePdfUatPreparedRun,
) -> PrivateDocumentPreparedRun:
    manifest_path = repository_root() / run.manifest_locator
    manifest_info = os.stat(manifest_path, follow_symlinks=False)
    manifest_identity = (manifest_info.st_dev, manifest_info.st_ino)
    source_bindings = tuple(
        private_document_state_body(
            PRIVATE_PDF_UAT_POLICY,
            item_id=document["item_id"],
            source_digest=document["source_digest"],
        )
        for document in run.batch_manifest["documents"]
    )
    qualification_binding = private_document_state_body(
        PRIVATE_PDF_UAT_POLICY,
        manifest_digest=run.corpus_digest,
        manifest_identity=list(manifest_identity),
        qualification=_strict_copy(run.qualification_manifest),
    )
    expectation_binding = private_document_state_body(
        PRIVATE_PDF_UAT_POLICY,
        manifest_digest=run.corpus_digest,
        manifest_identity=list(manifest_identity),
        item_ids=[document["item_id"] for document in run.corpus_manifest["documents"]],
        source_digests=[
            document["source_digest"] for document in run.corpus_manifest["documents"]
        ],
    )
    return PrivateDocumentPreparedRun(
        format_id=PRIVATE_PDF_UAT_POLICY.format_id,
        policy_digest=PRIVATE_PDF_UAT_POLICY_DIGEST,
        batch_id=run.batch_manifest["batch_id"],
        manifest=_strict_copy(run.corpus_manifest),
        manifest_digest=run.corpus_digest,
        manifest_identity=manifest_identity,
        staging_name=run.staging_name,
        control_identity=run.staging_control_identity,
        source_bindings=source_bindings,
        qualification_binding=qualification_binding,
        expectation_binding=expectation_binding,
    )


def _private_document_runtime_dependencies(
    runtime: Path,
    dependencies: PrivatePdfUatDependencies,
    *,
    require_verified_process: Callable[[Any, str], None],
) -> PrivateDocumentUatDependencies:
    return PrivateDocumentUatDependencies(
        policy=PRIVATE_PDF_UAT_POLICY,
        prepare_run=dependencies.prepare_run,
        launch_process=dependencies.launch_process,
        calibration=dependencies.evaluate_calibration,
        cleanup=dependencies.cleanup_run,
        brain_snapshot=dependencies.brain_snapshot,
        candidate_snapshot=dependencies.candidate_snapshot,
        load_manifest=dependencies.load_manifest,
        validate_prepared_run=_validate_prepared_run,
        project_run=_private_document_prepared_run,
        conversion_snapshot=dependencies.conversion_snapshot,
        process_poll=dependencies.process_poll,
        inspect_checkpoint=dependencies.inspect_checkpoint,
        inspect_barrier=dependencies.inspect_barrier,
        send_signal=dependencies.send_signal,
        terminate_process=dependencies.terminate_process,
        wait_process=dependencies.wait_process,
        load_final=dependencies.load_final,
        load_queue=dependencies.load_queue,
        verify_terminal=_verify_terminal,
        verify_queue=_verify_queue,
        assemble_readback=_assemble_private_uat_readback,
        persist_readback=lambda run, report, brain_post_run, brain_post_cleanup: (
            _persist_retained_private_uat_readback(
                run,
                report,
                brain_post_run_digest=brain_post_run,
                brain_post_cleanup_digest=brain_post_cleanup,
            )
        ),
        require_verified_process=require_verified_process,
        expected_item_ids=lambda manifest: [
            item["item_id"] for item in validate_private_pdf_manifest(manifest)["documents"]
        ],
        monotonic=dependencies.monotonic,
        sleep=dependencies.sleep,
        checkpoint_timeout=dependencies.checkpoint_timeout,
        poll_interval=dependencies.poll_interval,
    )


def run_private_pdf_uat(
    *,
    runtime_root: Path | None = None,
    dependencies: PrivatePdfUatDependencies | None = None,
) -> dict[str, Any]:
    """Interrupt, resume, rerun, calibrate, verify, and clean one private corpus."""

    selected_runtime = runtime_home() if runtime_root is None else Path(runtime_root)
    selected_runtime, _ = ensure_contained(
        selected_runtime, repository_root(), "private PDF UAT runtime root"
    )
    deps = _validate_uat_dependencies(
        _default_private_pdf_uat_dependencies(selected_runtime)
        if dependencies is None
        else dependencies
    )
    try:
        runtime_deps = _private_document_runtime_dependencies(
            selected_runtime,
            deps,
            require_verified_process=(
                _require_verified_sandbox_process
                if dependencies is None
                else lambda _process, _phase: None
            ),
        )
        return run_private_document_uat(
            runtime_root=selected_runtime,
            dependencies=runtime_deps,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:
        if isinstance(exc, PrivatePdfUatError) and str(exc) == "private PDF UAT failed":
            raise
        raise PrivatePdfUatError("private PDF UAT failed") from exc


def _default_process_poll(process: Any) -> int | None:
    return process.poll()


def _require_verified_sandbox_process(process: Any, phase: str) -> None:
    if (
        getattr(process, "_ao_lore_sandbox_phase", None) != phase
        or getattr(process, "_ao_lore_sandbox_verified", False) is not True
        or not _DIGEST_RE.fullmatch(
            getattr(process, "_ao_lore_sandbox_digest", "")
        )
    ):
        raise _fail("private PDF UAT sandbox status is invalid")


def _worker_run_contract(
    run: PrivatePdfUatPreparedRun,
    phase: str,
    *,
    sandbox: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if phase not in {"interrupted", "resume", "rerun"}:
        raise _fail("private PDF UAT worker phase is invalid")
    _validate_sealed_private_pdf_model_cache(
        _sealed_cache_from_prepared_run(run)
    )
    evidence, _ = _load_prepared_run_evidence(
        run.conversion_journal.parent, run.runtime_root
    )
    if evidence != _prepared_run_evidence(run):
        raise _fail("private PDF UAT worker prepared evidence drifted")
    manifest_body = _manifest_body(run.batch_manifest)
    control_descriptor = _open_directory(
        repository_root() / "sources" / ".private-pdf-uat",
        repository_root(),
        create=False,
    )
    try:
        manifest_info = os.stat(
            "run-manifest.json", dir_fd=control_descriptor,
            follow_symlinks=False,
        )
    finally:
        os.close(control_descriptor)
    qualification_body = run.qualification_body
    contract = {
        "schema_version": "ao.lore.private-pdf-uat-worker-run.v0.1",
        "phase": phase,
        "batch_id": run.batch_manifest["batch_id"],
        "batch_manifest_digest": run.batch_manifest_digest,
        "manifest_locator": _UAT_MANIFEST_LOCATOR,
        "manifest_body_digest": "sha256:" + hashlib.sha256(manifest_body).hexdigest(),
        "manifest_identity": [manifest_info.st_dev, manifest_info.st_ino],
        "staging_name": run.staging_name,
        "staging_identity": list(run.staging_source_identity),
        "qualification_locator": "benchmarks/docling-2.118.1.json",
        "qualification_identity": list(run.qualification_identity),
        "qualification_body_digest": "sha256:" + hashlib.sha256(
            qualification_body
        ).hexdigest(),
        "qualification_configuration_digest": PRIVATE_PDF_CONFIGURATION_DIGEST,
        "qualification_result_digest": run.qualification_digest,
        "journal_locator": (
            f"uat/private-pdf/{run.batch_manifest['batch_id']}"
            "/conversion-journal.jsonl"
        ),
        "journal_identity": list(run.conversion_journal_identity),
        "sealed_cache_locator": (
            f"uat/private-pdf/{run.batch_manifest['batch_id']}/"
            f"{run.sealed_cache_container.name}/huggingface"
        ),
        "sealed_cache_root_identity": list(run.sealed_cache_root_identity),
        "sealed_cache_tree_manifest": _strict_copy(
            run.sealed_cache_tree_manifest
        ),
        "sealed_cache_tree_digest": run.sealed_cache_tree_digest,
        "sealed_cache_file_count": run.sealed_cache_file_count,
        "sealed_cache_total_bytes": run.sealed_cache_total_bytes,
        "prepared_evidence_digest": canonical_digest(evidence),
        "campaign_origin": _campaign_origin_value(run.origin),
        "campaign_origin_digest": _campaign_origin_digest(run.origin),
        "qualification_corpus_digest": _qualification_corpus_digest(
            run.corpus_manifest, run.origin
        ),
        "barrier_locator": (
            f"uat/private-pdf/{run.batch_manifest['batch_id']}"
            "/barrier-ready.json"
        ),
        "sandbox": _strict_copy(sandbox) if sandbox is not None else None,
        "authority": False,
    }
    contract["run_digest"] = canonical_digest(contract)
    return contract


def _default_launch_process(
    run: PrivatePdfUatPreparedRun, phase: str
) -> subprocess.Popen[bytes]:
    return _launch_private_pdf_worker(
        run,
        phase,
        module="ao_lore.private_pdf_uat_worker",
        pythonpath=str(repository_root() / "src"),
    )


def _probe_private_pdf_sandbox() -> str:
    """Prove the fixed Ubuntu bubblewrap boundary before persisting a run contract."""

    try:
        info = os.lstat(_BWRAP)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("invalid bubblewrap executable")
        version_result = subprocess.run(
            [_BWRAP, "--version"], cwd=repository_root(), env={},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=5.0, check=False,
        )
        if (
            version_result.returncode != 0
            or len(version_result.stdout) > 256
            or len(version_result.stderr) > 1024
        ):
            raise OSError("bubblewrap version probe failed")
        version = version_result.stdout.decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", version):
            raise OSError("bubblewrap version is invalid")
        probe_result = subprocess.run(
            _BWRAP_PROBE_ARGV, cwd=repository_root(), env={},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10.0, check=False,
        )
        if (
            probe_result.returncode != 0
            or len(probe_result.stdout) > 1024
            or len(probe_result.stderr) > 4096
        ):
            raise OSError("bubblewrap isolation probe failed")
        return version
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _fail("private PDF UAT sandbox is unavailable") from exc


def _sandbox_mount(
    path: Path, name: str, mode: str, *, validate_tree: bool = False
) -> dict[str, Any]:
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not (
            stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)
        ):
            raise OSError("sandbox mount is invalid")
        flags = _DIRECTORY_FLAGS if stat.S_ISDIR(before.st_mode) else (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(path, flags)
        try:
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            (before.st_dev, before.st_ino, stat.S_IFMT(before.st_mode))
            != (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode))
        ):
            raise OSError("sandbox mount drifted")
        if validate_tree and stat.S_ISDIR(after.st_mode):
            _require_socket_free_sandbox_tree(path)
    except OSError as exc:
        raise _fail("private PDF UAT sandbox mount is invalid") from exc
    return {
        "name": name,
        "target": str(path),
        "mode": mode,
        "identity": [after.st_dev, after.st_ino],
    }


def _require_socket_free_sandbox_tree(root: Path) -> None:
    entry_count = 0
    root_descriptor = os.open(root, _DIRECTORY_FLAGS)

    def walk(descriptor: int, depth: int) -> None:
        nonlocal entry_count
        if depth > 64:
            raise OSError("sandbox mount depth exceeds bound")
        for name in os.listdir(descriptor):
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise OSError("sandbox mount entry is invalid")
            entry_count += 1
            if entry_count > 100_000:
                raise OSError("sandbox mount entries exceed bound")
            info = os.stat(
                name, dir_fd=descriptor, follow_symlinks=False
            )
            if stat.S_ISSOCK(info.st_mode):
                raise OSError("sandbox mount contains a socket")
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (
                        info.st_dev, info.st_ino
                    ):
                        raise OSError("sandbox mount binding drifted")
                    walk(child, depth + 1)
                    rebound = os.stat(
                        name, dir_fd=descriptor, follow_symlinks=False
                    )
                    if (rebound.st_dev, rebound.st_ino) != (
                        info.st_dev, info.st_ino
                    ):
                        raise OSError("sandbox mount binding drifted")
                finally:
                    os.close(child)

    try:
        walk(root_descriptor, 0)
    finally:
        os.close(root_descriptor)


def _private_pdf_sandbox_foundation_mounts(
    *, include_tests: bool = False
) -> list[dict[str, Any]]:
    """Return only the fixed read-only roots needed by Python and AO Lore."""

    repository = repository_root()
    roots = [
        (Path("/usr"), "system-usr", False),
        (Path("/etc"), "system-etc", False),
        (repository / "src", "ao-lore-src", True),
    ]
    if include_tests:
        roots.append((repository / "tests", "ao-lore-tests", True))
    prefix = Path(sys.prefix)
    resolved_prefix = prefix.resolve(strict=True)
    if resolved_prefix != Path("/usr"):
        roots.append((prefix, "python-environment", True))
    user_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    user_site = Path(sysconfig.get_path(
        "purelib",
        scheme="posix_user",
        vars={"userbase": str(user_home / ".local")},
    ))
    if user_site.is_dir():
        resolved_user_site = user_site.resolve(strict=True)
        if not any(
            resolved_user_site == root or resolved_user_site.is_relative_to(root)
            for root in (
                Path("/usr").resolve(),
                (repository / "src").resolve(strict=True),
                resolved_prefix,
            )
        ):
            roots.append((user_site, "python-user-site", True))
    return [
        _sandbox_mount(path, name, "read-only", validate_tree=validate_tree)
        for path, name, validate_tree in roots
    ]


def _private_pdf_sandbox_argv_prefix(
    mounts: Sequence[Mapping[str, Any]],
) -> list[str]:
    argv = [_BWRAP, "--die-with-parent", "--unshare-net", "--tmpfs", "/"]
    for mount in mounts:
        flag = "--bind" if mount["mode"] == "read-write" else "--ro-bind"
        argv.extend((flag, mount["target"], mount["target"]))
    argv.extend((
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/sbin", "/sbin",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
    ))
    return argv


def _private_pdf_sandbox_spec(
    run: PrivatePdfUatPreparedRun,
    phase: str,
    *,
    command: Sequence[str],
    pythonpath: str,
    bwrap_version: str,
) -> dict[str, Any]:
    """Build the single reusable, fixed-mount ingest worker sandbox."""

    if phase not in {"interrupted", "resume", "rerun"}:
        raise _fail("private PDF UAT worker phase is invalid")
    if (
        not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or any(type(part) is not str or not part or "\x00" in part for part in command)
        or not re.fullmatch(r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", bwrap_version)
    ):
        raise _fail("private PDF UAT sandbox command is invalid")
    state = run.conversion_journal.parent
    batch = run.runtime_root / "batches" / run.batch_manifest["batch_id"]
    home = state / "sandbox-home"
    for path in (batch, home):
        descriptor = _open_directory(path, run.runtime_root, create=True)
        os.close(descriptor)
    candidates = repository_root() / "working" / "candidates"
    include_tests = command[-1] == "tests.private_pdf_uat_worker_fixture"
    mounts = _private_pdf_sandbox_foundation_mounts(
        include_tests=include_tests
    ) + [
        _sandbox_mount(
            repository_root() / "sources" / run.staging_name,
            "staged-sources", "read-only", validate_tree=True,
        ),
        _sandbox_mount(
            repository_root() / run.manifest_locator,
            "control-manifest", "read-only",
        ),
        _sandbox_mount(
            run.runtime_root / "benchmarks" / "docling-2.118.1.json",
            "qualification", "read-only",
        ),
        _sandbox_mount(batch, "batch-state", "read-write", validate_tree=True),
        _sandbox_mount(
            candidates, "candidate-state", "read-write", validate_tree=True
        ),
        _sandbox_mount(state, "run-state", "read-write", validate_tree=True),
        _sandbox_mount(
            run.sealed_cache_root, "sealed-cache", "read-only", validate_tree=True
        ),
    ]
    dependency_paths = [
        mount["target"] for mount in mounts
        if mount["name"] == "python-user-site"
    ]
    environment = {
        "AO_LORE_HOME": str(run.runtime_root),
        "HF_HOME": str(run.sealed_cache_root),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_OFFLINE": "1",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PWD": str(repository_root()),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join([pythonpath, *dependency_paths]),
        "TORCH_HOME": str(run.sealed_cache_root),
        "TRANSFORMERS_CACHE": str(run.sealed_cache_root / "hub"),
        "TRANSFORMERS_OFFLINE": "1",
        "XDG_CACHE_HOME": str(run.sealed_cache_root),
    }
    argv = _private_pdf_sandbox_argv_prefix(mounts)
    argv.append("--clearenv")
    for key in sorted(environment):
        if key != "PWD":
            argv.extend(("--setenv", key, environment[key]))
    argv.extend(("--chdir", str(repository_root()), "--", *command))
    return {
        "schema_version": _SANDBOX_SCHEMA_VERSION,
        "mechanism": _BWRAP,
        "version": bwrap_version,
        "argv": argv,
        "network_namespace": "unshared",
        "root_filesystem": "allowlisted",
        "mounts": mounts,
        "environment": environment,
        "sealed_cache": {
            "mode": "read-only",
            "tree_digest": run.sealed_cache_tree_digest,
            "identity": list(run.sealed_cache_root_identity),
        },
    }


def _private_pdf_calibration_sandbox_spec(
    run: PrivatePdfUatPreparedRun,
    *,
    bwrap_version: str,
) -> dict[str, Any]:
    """Build the fixed calibration-only sandbox with one writable result root."""

    if not re.fullmatch(
        r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", bwrap_version
    ):
        raise _fail("private PDF calibration sandbox version is invalid")
    state = run.conversion_journal.parent
    home = Path("/tmp/ao-lore-calibration-home")
    contract_locator = (
        f"uat/private-pdf/{run.batch_manifest['batch_id']}/"
        f"{_CALIBRATION_CONTRACT_NAME}"
    )
    mounts = _private_pdf_sandbox_foundation_mounts() + [
        _sandbox_mount(
            run.corpus_root, "calibration-corpus", "read-only", validate_tree=True
        ),
        _sandbox_mount(
            run.runtime_root / "benchmarks" / "docling-2.118.1.json",
            "qualification", "read-only",
        ),
        _sandbox_mount(state, "run-state", "read-write", validate_tree=True),
        _sandbox_mount(
            run.sealed_cache_root, "sealed-cache", "read-only", validate_tree=True
        ),
    ]
    dependency_paths = [
        mount["target"] for mount in mounts
        if mount["name"] == "python-user-site"
    ]
    environment = {
        "AO_LORE_CALIBRATION_CONTRACT": contract_locator,
        "AO_LORE_HOME": str(run.runtime_root),
        "HF_HOME": str(run.sealed_cache_root),
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "HF_HUB_OFFLINE": "1",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PWD": str(repository_root()),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": os.pathsep.join([
            str(repository_root() / "src"), *dependency_paths
        ]),
        "TORCH_HOME": str(run.sealed_cache_root),
        "TRANSFORMERS_CACHE": str(run.sealed_cache_root / "hub"),
        "TRANSFORMERS_OFFLINE": "1",
        "XDG_CACHE_HOME": str(run.sealed_cache_root),
    }
    command = [sys.executable, "-m", "ao_lore.private_pdf_calibration_worker"]
    argv = _private_pdf_sandbox_argv_prefix(mounts)
    argv.extend(("--dir", str(home)))
    argv.append("--clearenv")
    for key in sorted(environment):
        if key != "PWD":
            argv.extend(("--setenv", key, environment[key]))
    argv.extend(("--chdir", str(repository_root()), "--", *command))
    return {
        "schema_version": _SANDBOX_SCHEMA_VERSION,
        "mechanism": _BWRAP,
        "version": bwrap_version,
        "argv": argv,
        "network_namespace": "unshared",
        "root_filesystem": "allowlisted",
        "mounts": mounts,
        "environment": environment,
        "sealed_cache": {
            "mode": "read-only",
            "tree_digest": run.sealed_cache_tree_digest,
            "identity": list(run.sealed_cache_root_identity),
        },
    }


def _private_pdf_calibration_source_bindings(
    run: PrivatePdfUatPreparedRun,
) -> list[dict[str, Any]]:
    loaded, bodies = _load_private_calibration_bytes(
        run.corpus_manifest, run.corpus_root
    )
    corpus_descriptor = _open_directory(
        run.corpus_root, run.runtime_root, create=False
    )
    input_descriptor: int | None = None
    try:
        input_descriptor = _open_relative_directory(
            corpus_descriptor, ("input",), create=False
        )
        result: list[dict[str, Any]] = []
        for document in loaded["documents"]:
            name = PurePosixPath(document["file"]).name
            info = os.stat(name, dir_fd=input_descriptor, follow_symlinks=False)
            body = bodies[f"{document['item_id']}.pdf"]
            result.append({
                "item_id": document["item_id"],
                "file": document["file"],
                "digest": document["source_digest"],
                "size": len(body),
                "identity": [info.st_dev, info.st_ino],
            })
            del body
        return result
    finally:
        if input_descriptor is not None:
            os.close(input_descriptor)
        os.close(corpus_descriptor)


def _private_pdf_calibration_contract(
    run: PrivatePdfUatPreparedRun,
    sandbox: Mapping[str, Any],
) -> dict[str, Any]:
    sealed = _validate_sealed_private_pdf_model_cache(
        _sealed_cache_from_prepared_run(run)
    )
    manifest_body = _manifest_body(run.corpus_manifest)
    manifest_info = os.stat(
        run.corpus_root / _MANIFEST_NAME, follow_symlinks=False
    )
    output_parent = run.conversion_journal.parent
    output_info = os.stat(output_parent, follow_symlinks=False)
    value = {
        "schema_version": _CALIBRATION_CONTRACT_SCHEMA_VERSION,
        "phase": "calibration",
        "batch_id": run.batch_manifest["batch_id"],
        "manifest": _strict_copy(run.corpus_manifest),
        "manifest_digest": canonical_digest(run.corpus_manifest),
        "manifest_body_digest": "sha256:" + hashlib.sha256(manifest_body).hexdigest(),
        "manifest_identity": [manifest_info.st_dev, manifest_info.st_ino],
        "corpus_locator": (
            f"calibration/private-pdf/{run.corpus_manifest['corpus_id']}"
        ),
        "corpus_identity": list(run.corpus_root_identity),
        "sources": _private_pdf_calibration_source_bindings(run),
        "aggregate_source_byte_limit": _PRIVATE_CALIBRATION_MAX_TOTAL_BYTES,
        "qualification_locator": "benchmarks/docling-2.118.1.json",
        "qualification_identity": list(run.qualification_identity),
        "qualification_body_digest": "sha256:" + hashlib.sha256(
            run.qualification_body
        ).hexdigest(),
        "qualification_result_digest": run.qualification_digest,
        "configuration_digest": PRIVATE_PDF_CONFIGURATION_DIGEST,
        "campaign_origin": _campaign_origin_value(run.origin),
        "campaign_origin_digest": _campaign_origin_digest(run.origin),
        "qualification_corpus_digest": _qualification_corpus_digest(
            run.corpus_manifest, run.origin
        ),
        "sealed_cache_locator": (
            f"uat/private-pdf/{run.batch_manifest['batch_id']}/"
            f"{run.sealed_cache_container.name}/huggingface"
        ),
        "sealed_cache_identity": list(sealed.root_identity),
        "sealed_cache_tree_manifest": _strict_copy(sealed.tree_manifest),
        "sealed_cache_tree_digest": sealed.tree_digest,
        "sealed_cache_file_count": sealed.file_count,
        "sealed_cache_total_bytes": sealed.total_bytes,
        "output_locator": (
            f"uat/private-pdf/{run.batch_manifest['batch_id']}/"
            f"{_CALIBRATION_RESULT_NAME}"
        ),
        "output_parent_identity": [output_info.st_dev, output_info.st_ino],
        "item_timeout_seconds": _CALIBRATION_ITEM_TIMEOUT_SECONDS,
        "sandbox": _strict_copy(sandbox),
        "authority": False,
    }
    value["run_digest"] = canonical_digest(value)
    return value


def _launch_private_pdf_calibration_worker(
    run: PrivatePdfUatPreparedRun,
) -> subprocess.Popen[bytes]:
    version = _probe_private_pdf_sandbox()
    sandbox = _private_pdf_calibration_sandbox_spec(
        run, bwrap_version=version
    )
    contract = _private_pdf_calibration_contract(run, sandbox)
    state = run.conversion_journal.parent
    parent_descriptor = _open_directory(
        state, run.runtime_root, create=False
    )
    parent_info = os.fstat(parent_descriptor)
    owned_contract: _PrivatePdfOwnedArtifact | None = None
    try:
        if _binding_exists_at(
            parent_descriptor, _CALIBRATION_CONTRACT_NAME
        ) or _binding_exists_at(parent_descriptor, _CALIBRATION_RESULT_NAME):
            raise OSError("private PDF calibration artifact collision")
        owned_contract = _publish_calibration_artifact_at(
            parent_descriptor,
            _CALIBRATION_CONTRACT_NAME,
            _manifest_body(contract, _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES),
            _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
        )
        process = subprocess.Popen(
            sandbox["argv"],
            cwd=repository_root(),
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            shell=False,
        )
    except BaseException:
        _unlink_owned_calibration_artifact_at(
            parent_descriptor,
            owned_contract,
            _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
        )
        os.close(parent_descriptor)
        raise
    process._ao_lore_contract_path = state / _CALIBRATION_CONTRACT_NAME  # type: ignore[attr-defined]
    process._ao_lore_contract_body = owned_contract.body  # type: ignore[attr-defined]
    process._ao_lore_contract_artifact = owned_contract  # type: ignore[attr-defined]
    process._ao_lore_artifact_parent_descriptor = parent_descriptor  # type: ignore[attr-defined]
    process._ao_lore_artifact_parent_identity = (  # type: ignore[attr-defined]
        parent_info.st_dev,
        parent_info.st_ino,
    )
    process._ao_lore_runtime_root = run.runtime_root  # type: ignore[attr-defined]
    process._ao_lore_sandbox_digest = canonical_digest(sandbox)  # type: ignore[attr-defined]
    process._ao_lore_sandbox_phase = "calibration"  # type: ignore[attr-defined]
    return process


def _launch_private_pdf_worker(
    run: PrivatePdfUatPreparedRun,
    phase: str,
    *,
    module: str,
    pythonpath: str,
) -> subprocess.Popen[bytes]:
    if module not in {
        "ao_lore.private_pdf_uat_worker",
        "tests.private_pdf_uat_worker_fixture",
    }:
        raise _fail("private PDF UAT worker module is invalid")
    version = _probe_private_pdf_sandbox()
    sandbox = _private_pdf_sandbox_spec(
        run, phase,
        command=[sys.executable, "-m", module],
        pythonpath=pythonpath,
        bwrap_version=version,
    )
    contract = _worker_run_contract(run, phase, sandbox=sandbox)
    contract_path = run.conversion_journal.parent / "worker-run-contract.json"
    contract_body = _persist_uat_document(
        contract_path.parent,
        run.runtime_root,
        contract_path.name,
        contract,
    )
    if phase == "interrupted":
        barrier = run.conversion_journal.parent / "barrier-ready.json"
        if barrier.exists():
            raise _fail("private PDF UAT worker barrier is stale")
    process = subprocess.Popen(
        sandbox["argv"],
        cwd=repository_root(),
        env={},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        shell=False,
    )
    process._ao_lore_contract_path = contract_path  # type: ignore[attr-defined]
    process._ao_lore_contract_body = contract_body  # type: ignore[attr-defined]
    process._ao_lore_runtime_root = run.runtime_root  # type: ignore[attr-defined]
    process._ao_lore_sandbox_digest = canonical_digest(sandbox)  # type: ignore[attr-defined]
    process._ao_lore_sandbox_phase = phase  # type: ignore[attr-defined]
    return process


def _default_wait_process(process: subprocess.Popen[bytes]) -> dict[str, Any]:
    if process.stdout is None or process.stderr is None:
        raise _fail("private PDF UAT process pipes are unavailable")
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        timeout_seconds = (
            _CALIBRATION_PROCESS_TIMEOUT_SECONDS
            if getattr(process, "_ao_lore_sandbox_phase", None) == "calibration"
            else _UAT_PROCESS_TIMEOUT_SECONDS
        )
        deadline = time.monotonic() + timeout_seconds
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("ao-lore worker", timeout_seconds)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired("ao-lore worker", timeout_seconds)
            for key, _ in events:
                target = key.data
                maximum = (
                    _UAT_STDOUT_MAX_BYTES if target is stdout else _UAT_STDERR_MAX_BYTES
                )
                chunk = os.read(key.fileobj.fileno(), min(65536, maximum + 1 - len(target)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if len(target) > maximum:
                    raise _fail("private PDF UAT process output exceeded its bound")
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise _fail("private PDF UAT process timed out") from exc
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    contract_path = getattr(process, "_ao_lore_contract_path", None)
    contract_body = getattr(process, "_ao_lore_contract_body", None)
    contract_root = getattr(process, "_ao_lore_runtime_root", None)
    if contract_path is not None:
        if not isinstance(contract_path, Path) or not isinstance(contract_root, Path):
            raise _fail("private PDF UAT worker contract drifted")
        try:
            _, retained_body = strict_read_json(
                contract_path,
                "private PDF UAT retained worker contract",
                max_bytes=_MAX_MANIFEST_BYTES,
                root=contract_root,
            )
        except ContractError as exc:
            raise _fail("private PDF UAT worker contract drifted") from exc
        if not isinstance(contract_body, bytes) or retained_body != contract_body:
            raise _fail("private PDF UAT worker contract drifted")
        retained = parse_strict_json(
            retained_body, "private PDF UAT retained worker contract"
        )
        sandbox_digest = getattr(process, "_ao_lore_sandbox_digest", None)
        sandbox_phase = getattr(process, "_ao_lore_sandbox_phase", None)
        if (
            not isinstance(retained, Mapping)
            or retained.get("phase") != sandbox_phase
            or not isinstance(retained.get("sandbox"), Mapping)
            or canonical_digest(retained["sandbox"]) != sandbox_digest
        ):
            raise _fail("private PDF UAT sandbox status is invalid")
        process._ao_lore_sandbox_verified = True  # type: ignore[attr-defined]
    return {"returncode": returncode, "stdout": bytes(stdout), "stderr": bytes(stderr)}


def _calibration_artifact_path(
    runtime: Path, locator: str, expected_name: str
) -> Path:
    if (
        not isinstance(locator, str)
        or PurePosixPath(locator).name != expected_name
        or PurePosixPath(locator).is_absolute()
        or ".." in PurePosixPath(locator).parts
    ):
        raise OSError("private PDF calibration locator is invalid")
    selected, _ = ensure_contained(
        runtime / PurePosixPath(locator), runtime,
        "private PDF calibration locator",
    )
    return selected


def _validate_calibration_worker_contract(
    value: Any,
    *,
    runtime: Path,
) -> tuple[
    dict[str, Any], dict[str, Any], PrivatePdfSealedModelCache, Path, Path
]:
    keys = {
        "schema_version", "phase", "batch_id", "manifest",
        "manifest_digest", "manifest_body_digest", "manifest_identity",
        "corpus_locator", "corpus_identity", "sources",
        "aggregate_source_byte_limit", "qualification_locator",
        "qualification_identity", "qualification_body_digest",
        "qualification_result_digest", "configuration_digest",
        "sealed_cache_locator", "sealed_cache_identity",
        "sealed_cache_tree_manifest", "sealed_cache_tree_digest",
        "sealed_cache_file_count", "sealed_cache_total_bytes",
        "output_locator", "output_parent_identity", "item_timeout_seconds",
        "campaign_origin", "campaign_origin_digest",
        "qualification_corpus_digest", "sandbox", "authority", "run_digest",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or value.get("schema_version") != _CALIBRATION_CONTRACT_SCHEMA_VERSION
        or value.get("phase") != "calibration"
        or value.get("authority") is not False
        or value.get("configuration_digest")
        != PRIVATE_PDF_CONFIGURATION_DIGEST
        or value.get("aggregate_source_byte_limit")
        != _PRIVATE_CALIBRATION_MAX_TOTAL_BYTES
    ):
        raise OSError("private PDF calibration contract is invalid")
    contract = _strict_copy(value)
    supplied_digest = contract.pop("run_digest")
    if not _DIGEST_RE.fullmatch(str(supplied_digest)) or canonical_digest(
        contract
    ) != supplied_digest:
        raise OSError("private PDF calibration contract digest is invalid")
    contract["run_digest"] = supplied_digest
    manifest = validate_private_pdf_manifest(contract["manifest"])
    origin = _validate_campaign_origin(contract["campaign_origin"], manifest)
    if (
        canonical_digest(manifest) != contract["manifest_digest"]
        or contract["campaign_origin_digest"] != _campaign_origin_digest(origin)
        or contract["qualification_corpus_digest"]
        != _qualification_corpus_digest(manifest, origin)
        or len(manifest["documents"]) > 4
        or not _DIGEST_RE.fullmatch(contract["manifest_body_digest"])
        or not isinstance(contract["manifest_identity"], list)
        or len(contract["manifest_identity"]) != 2
        or not isinstance(contract["corpus_identity"], list)
        or len(contract["corpus_identity"]) != 2
    ):
        raise OSError("private PDF calibration manifest binding is invalid")
    corpus = _calibration_artifact_path(
        runtime, contract["corpus_locator"], manifest["corpus_id"]
    )
    corpus_info = os.stat(corpus, follow_symlinks=False)
    manifest_path = corpus / _MANIFEST_NAME
    retained_manifest, manifest_body = strict_read_json(
        manifest_path,
        "private PDF calibration worker manifest",
        max_bytes=_MAX_MANIFEST_BYTES,
        root=runtime,
    )
    manifest_info = os.stat(manifest_path, follow_symlinks=False)
    if (
        retained_manifest != manifest
        or "sha256:" + hashlib.sha256(manifest_body).hexdigest()
        != contract["manifest_body_digest"]
        or [manifest_info.st_dev, manifest_info.st_ino]
        != contract["manifest_identity"]
        or [corpus_info.st_dev, corpus_info.st_ino]
        != contract["corpus_identity"]
    ):
        raise OSError("private PDF calibration manifest binding drifted")
    sources = contract["sources"]
    if (
        not isinstance(sources, list)
        or len(sources) != len(manifest["documents"])
        or sum(
            item.get("size", _PRIVATE_CALIBRATION_MAX_TOTAL_BYTES + 1)
            for item in sources if isinstance(item, Mapping)
        ) > _PRIVATE_CALIBRATION_MAX_TOTAL_BYTES
    ):
        raise OSError("private PDF calibration sources are invalid")
    for supplied, document in zip(sources, manifest["documents"], strict=True):
        if (
            not isinstance(supplied, Mapping)
            or set(supplied) != {"item_id", "file", "digest", "size", "identity"}
            or supplied.get("item_id") != document["item_id"]
            or supplied.get("file") != document["file"]
            or supplied.get("digest") != document["source_digest"]
            or isinstance(supplied.get("size"), bool)
            or not isinstance(supplied.get("size"), int)
            or not 0 < supplied["size"] <= MAX_PRIVATE_PDF_BYTES
            or not isinstance(supplied.get("identity"), list)
            or len(supplied["identity"]) != 2
            or any(
                isinstance(part, bool) or not isinstance(part, int) or part < 0
                for part in supplied["identity"]
            )
        ):
            raise OSError("private PDF calibration sources are invalid")
    qualification_path = _calibration_artifact_path(
        runtime, contract["qualification_locator"], "docling-2.118.1.json"
    )
    qualification, qualification_body = strict_read_json(
        qualification_path,
        "private PDF calibration worker qualification",
        max_bytes=_MAX_QUALIFICATION_BYTES,
        root=runtime,
    )
    qualification_info = os.stat(qualification_path, follow_symlinks=False)
    verify_manifest(
        qualification,
        expected_corpus_digest=contract["qualification_corpus_digest"],
        expected_configuration_digest=PRIVATE_PDF_CONFIGURATION_DIGEST,
    )
    if (
        [qualification_info.st_dev, qualification_info.st_ino]
        != contract["qualification_identity"]
        or "sha256:" + hashlib.sha256(qualification_body).hexdigest()
        != contract["qualification_body_digest"]
        or qualification.get("result_digest")
        != contract["qualification_result_digest"]
        or qualification.get("parser_id") != "docling"
        or qualification.get("parser_version") != "2.118.1"
    ):
        raise OSError("private PDF calibration qualification drifted")
    sealed_root = _calibration_artifact_path(
        runtime, contract["sealed_cache_locator"], "huggingface"
    )
    sealed = PrivatePdfSealedModelCache(
        root=sealed_root,
        container=sealed_root.parent,
        root_identity=tuple(contract["sealed_cache_identity"]),
        tree_manifest=contract["sealed_cache_tree_manifest"],
        tree_digest=contract["sealed_cache_tree_digest"],
        file_count=contract["sealed_cache_file_count"],
        total_bytes=contract["sealed_cache_total_bytes"],
    )
    _validate_sealed_private_pdf_model_cache(sealed)
    output_path = _calibration_artifact_path(
        runtime, contract["output_locator"], _CALIBRATION_RESULT_NAME
    )
    output_parent_info = os.stat(output_path.parent, follow_symlinks=False)
    timeout = contract["item_timeout_seconds"]
    sandbox = contract["sandbox"]
    expected_mounts = _private_pdf_sandbox_foundation_mounts() + [
        _sandbox_mount(
            corpus, "calibration-corpus", "read-only", validate_tree=True
        ),
        _sandbox_mount(
            qualification_path, "qualification", "read-only"
        ),
        _sandbox_mount(
            output_path.parent, "run-state", "read-write", validate_tree=True
        ),
        _sandbox_mount(
            sealed.root, "sealed-cache", "read-only", validate_tree=True
        ),
    ]
    if (
        [output_parent_info.st_dev, output_parent_info.st_ino]
        != contract["output_parent_identity"]
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or not isinstance(sandbox, Mapping)
        or sandbox.get("mechanism") != _BWRAP
        or sandbox.get("network_namespace") != "unshared"
        or sandbox.get("root_filesystem") != "allowlisted"
        or sandbox.get("sealed_cache") != {
            "mode": "read-only",
            "tree_digest": sealed.tree_digest,
            "identity": list(sealed.root_identity),
        }
        or sandbox.get("mounts") != expected_mounts
        or sandbox.get("environment") != dict(os.environ)
        or not isinstance(sandbox.get("argv"), list)
        or sandbox["argv"][-3:] != [
            sys.executable, "-m", "ao_lore.private_pdf_calibration_worker"
        ]
    ):
        raise OSError("private PDF calibration sandbox binding is invalid")
    return contract, qualification, sealed, corpus, output_path


def _private_pdf_calibration_worker_entry() -> int:
    """Run exact Docling calibration inside the already-created bwrap."""

    output_path: Path | None = None
    try:
        runtime = runtime_home()
        locator = os.environ.get("AO_LORE_CALIBRATION_CONTRACT", "")
        contract_path = _calibration_artifact_path(
            runtime, locator, _CALIBRATION_CONTRACT_NAME
        )
        value, _ = strict_read_json(
            contract_path,
            "private PDF calibration worker contract",
            max_bytes=_PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
            root=runtime,
        )
        contract, qualification, sealed, corpus, output_path = (
            _validate_calibration_worker_contract(value, runtime=runtime)
        )
        # Keep third-party diagnostics off inherited pipes. The only output is
        # the bounded atomic result file named by the verified contract.
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        finally:
            os.close(devnull)
        with _private_pdf_sealed_cache_environment(sealed):
            adapter = DoclingPdfAdapter(qualification)
            aggregate = evaluate_private_pdf_corpus(
                contract["manifest"],
                adapter=adapter,
                corpus_root=corpus,
                item_timeout_seconds=contract["item_timeout_seconds"],
                expected_source_bindings=contract["sources"],
            )
        _validate_sealed_private_pdf_model_cache(sealed)
        result = {
            "schema_version": _CALIBRATION_RESULT_SCHEMA_VERSION,
            "run_digest": contract["run_digest"],
            "manifest_digest": contract["manifest_digest"],
            "configuration_digest": contract["configuration_digest"],
            "qualification_digest": contract["qualification_result_digest"],
            "sealed_cache_tree_digest": contract["sealed_cache_tree_digest"],
            "sealed_cache_file_count": contract["sealed_cache_file_count"],
            "sealed_cache_total_bytes": contract["sealed_cache_total_bytes"],
            "sandbox_digest": canonical_digest(contract["sandbox"]),
            "sandbox_verified": True,
            "network_isolated": True,
            "campaign_origin_digest": contract["campaign_origin_digest"],
            "aggregate": aggregate,
            "aggregate_digest": canonical_digest(aggregate),
            "authority": False,
        }
        _persist_calibration_result(output_path.parent, runtime, result)
        return 0
    except (_PrivateCalibrationDeadline, Exception):
        return 1


def _run_private_pdf_calibration_worker(
    run: PrivatePdfUatPreparedRun,
) -> PrivatePdfCalibrationEvidence:
    """Launch, bound, and verify one isolated calibration subprocess."""

    state = run.conversion_journal.parent
    contract_path = state / _CALIBRATION_CONTRACT_NAME
    result_path = state / _CALIBRATION_RESULT_NAME
    process: subprocess.Popen[bytes] | None = None
    parent_descriptor: int | None = None
    owned_contract: _PrivatePdfOwnedArtifact | None = None
    owned_result: _PrivatePdfOwnedArtifact | None = None
    try:
        sealed = _validate_sealed_private_pdf_model_cache(
            _sealed_cache_from_prepared_run(run)
        )
        process = _launch_private_pdf_calibration_worker(run)
        parent_descriptor = getattr(
            process, "_ao_lore_artifact_parent_descriptor", None
        )
        owned_contract = getattr(process, "_ao_lore_contract_artifact", None)
        outcome = _default_wait_process(process)
        _require_verified_sandbox_process(process, "calibration")
        process = None
        if (
            outcome["returncode"] != 0
            or outcome["stdout"] != b""
            or outcome["stderr"] != b""
        ):
            raise OSError("private PDF calibration worker outcome is invalid")
        _validate_sealed_private_pdf_model_cache(sealed)
        if parent_descriptor is None or owned_contract is None:
            raise OSError("private PDF calibration contract ownership is absent")
        retained_contract = _capture_calibration_artifact_at(
            parent_descriptor,
            _CALIBRATION_CONTRACT_NAME,
            _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
        )
        if retained_contract != owned_contract:
            raise OSError("private PDF calibration contract ownership drifted")
        owned_result = _capture_calibration_artifact_at(
            parent_descriptor, _CALIBRATION_RESULT_NAME, _MAX_MANIFEST_BYTES
        )
        contract = parse_strict_json(
            owned_contract.body, "private PDF calibration retained contract"
        )
        result = parse_strict_json(
            owned_result.body, "private PDF calibration result"
        )
        aggregate = _validate_calibration_aggregate(result.get("aggregate"))
        expected_keys = {
            "schema_version", "run_digest", "manifest_digest",
            "configuration_digest", "qualification_digest",
            "sealed_cache_tree_digest", "sealed_cache_file_count",
            "sealed_cache_total_bytes", "sandbox_digest",
            "sandbox_verified", "network_isolated", "aggregate",
            "aggregate_digest", "campaign_origin_digest", "authority",
        }
        if (
            not isinstance(contract, Mapping)
            or not isinstance(result, Mapping)
            or set(result) != expected_keys
            or result.get("schema_version")
            != _CALIBRATION_RESULT_SCHEMA_VERSION
            or result.get("run_digest") != contract.get("run_digest")
            or result.get("manifest_digest") != canonical_digest(run.corpus_manifest)
            or result.get("configuration_digest")
            != PRIVATE_PDF_CONFIGURATION_DIGEST
            or result.get("qualification_digest") != run.qualification_digest
            or result.get("sealed_cache_tree_digest") != sealed.tree_digest
            or result.get("sealed_cache_file_count") != sealed.file_count
            or result.get("sealed_cache_total_bytes") != sealed.total_bytes
            or result.get("sandbox_digest")
            != canonical_digest(contract.get("sandbox"))
            or result.get("sandbox_verified") is not True
            or result.get("network_isolated") is not True
            or result.get("campaign_origin_digest")
            != _campaign_origin_digest(run.origin)
            or result.get("aggregate_digest") != canonical_digest(aggregate)
            or result.get("authority") is not False
        ):
            raise OSError("private PDF calibration result drifted")
        evidence = PrivatePdfCalibrationEvidence(
            manifest_digest=result["manifest_digest"],
            corpus_digest=result["manifest_digest"],
            configuration_digest=result["configuration_digest"],
            qualification_digest=result["qualification_digest"],
            result_digest=result["aggregate_digest"],
            aggregate=aggregate,
            sealed_cache_tree_digest=result["sealed_cache_tree_digest"],
            sealed_cache_file_count=result["sealed_cache_file_count"],
            sealed_cache_total_bytes=result["sealed_cache_total_bytes"],
            sandbox_digest=result["sandbox_digest"],
            sandbox_verified=True,
            network_isolated=True,
            campaign_origin_digest=result["campaign_origin_digest"],
        )
        _unlink_owned_calibration_artifact_at(
            parent_descriptor, owned_result, _MAX_MANIFEST_BYTES
        )
        owned_result = None
        _unlink_owned_calibration_artifact_at(
            parent_descriptor,
            owned_contract,
            _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
        )
        owned_contract = None
        return evidence
    except BaseException as exc:
        if process is not None:
            try:
                _terminate_process_group(process)
            except Exception:
                pass
        if parent_descriptor is not None:
            _unlink_owned_calibration_artifact_at(
                parent_descriptor, owned_result, _MAX_MANIFEST_BYTES
            )
            _unlink_owned_calibration_artifact_at(
                parent_descriptor,
                owned_contract,
                _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
            )
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, PrivatePdfUatError) and str(exc) == "private PDF calibration failed":
            raise
        raise PrivatePdfUatError("private PDF calibration failed") from exc
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _default_send_signal(process: subprocess.Popen[bytes]) -> None:
    group = _bound_process_group(process)
    if group is None:
        raise ProcessLookupError(process.pid)
    os.killpg(group, signal.SIGINT)


def _default_brain_snapshot() -> str:
    root = repository_root() / "brain"
    descriptor = _open_directory(root, repository_root(), create=False)
    records: list[dict[str, str]] = []
    total_bytes = 0

    def walk(directory_descriptor: int, prefix: tuple[str, ...], depth: int) -> None:
        nonlocal total_bytes
        if depth > _UAT_BRAIN_MAX_DEPTH:
            raise OSError("brain depth exceeds its bound")
        for name in sorted(os.listdir(directory_descriptor)):
            if name in {".", ".."} or "/" in name or "\x00" in name:
                raise OSError("brain entry name is invalid")
            info = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_descriptor)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("brain directory binding changed")
                    walk(child, (*prefix, name), depth + 1)
                    public = os.stat(
                        name, dir_fd=directory_descriptor, follow_symlinks=False
                    )
                    if (public.st_dev, public.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("brain directory binding changed")
                finally:
                    os.close(child)
                continue
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_nlink != 1
                or not 0 <= info.st_size <= _UAT_BRAIN_MAX_FILE_BYTES
                or len(records) >= _UAT_BRAIN_MAX_FILES
                or total_bytes + info.st_size > _UAT_BRAIN_MAX_TOTAL_BYTES
            ):
                raise OSError("brain contains an unsafe entry")
            file_descriptor = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            try:
                opened = os.fstat(file_descriptor)
                if _stable_metadata(opened) != _stable_metadata(info):
                    raise OSError("brain file binding changed")
                body = _read_bounded(file_descriptor, _UAT_BRAIN_MAX_FILE_BYTES)
                after = os.fstat(file_descriptor)
                public = os.stat(
                    name, dir_fd=directory_descriptor, follow_symlinks=False
                )
                if (
                    _stable_metadata(after) != _stable_metadata(opened)
                    or _stable_metadata(public) != _stable_metadata(opened)
                ):
                    raise OSError("brain file binding changed")
            finally:
                os.close(file_descriptor)
            total_bytes += len(body)
            records.append({
                "file": PurePosixPath(*prefix, name).as_posix(),
                "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            })

    try:
        walk(descriptor, (), 0)
        return canonical_digest(records)
    except (OSError, ContractError) as exc:
        raise _fail("private PDF UAT brain snapshot failed") from exc
    finally:
        os.close(descriptor)


def _default_queue_items() -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        page = list_candidates(status="all", limit=200, after=after)
        for item in page["items"]:
            values.append({
                key: item[key]
                for key in ("candidate_id", "candidate_digest", "provenance_digest", "source_digest", "review_status")
            })
        after = page["next_after"]
        if after is None:
            return values


def _default_candidate_snapshot() -> dict[str, dict[str, Any]]:
    return {item["candidate_id"]: item for item in _default_queue_items()}


def _read_live_checkpoint_snapshot(
    run: PrivatePdfUatPreparedRun,
) -> dict[str, Any] | None:
    """Read one atomic checkpoint snapshot without taking the writer flock."""

    batch_root = run.runtime_root / "batches" / run.batch_manifest["batch_id"]
    descriptor: int | None = None
    try:
        try:
            descriptor = _open_directory(
                batch_root, run.runtime_root, create=False
            )
        except FileNotFoundError:
            return None
        try:
            body = _read_file_at(
                descriptor, "checkpoint.json", _MAX_MANIFEST_BYTES
            )
        except FileNotFoundError:
            return None
        value = parse_strict_json(body, "private PDF UAT live checkpoint")
        if not isinstance(value, Mapping):
            raise _fail("private PDF UAT live checkpoint is invalid")
        return validate_checkpoint(
            value, run.batch_manifest, run.batch_manifest_digest
        )
    except (ContractError, OSError, ValueError, TypeError) as exc:
        if isinstance(exc, PrivatePdfUatError):
            raise
        raise _fail("private PDF UAT live checkpoint is invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_worker_barrier(
    run: PrivatePdfUatPreparedRun, checkpoint: Mapping[str, Any]
) -> bool:
    directory = run.conversion_journal.parent
    descriptor = _open_directory(directory, run.runtime_root, create=False)
    try:
        try:
            body = _read_file_at(
                descriptor, "barrier-ready.json", _MAX_MANIFEST_BYTES
            )
        except FileNotFoundError:
            return False
        value = parse_strict_json(body, "private PDF UAT worker barrier")
        contract_body = _read_file_at(
            descriptor, "worker-run-contract.json", _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES
        )
        supplied_contract = parse_strict_json(
            contract_body, "private PDF UAT worker contract"
        )
        if not isinstance(supplied_contract, Mapping):
            raise OSError("private PDF UAT worker contract drifted")
        expected_contract = _worker_run_contract(
            run, "interrupted", sandbox=supplied_contract.get("sandbox")
        )
        if supplied_contract != expected_contract:
            raise OSError("private PDF UAT worker contract drifted")
        expected = {
            "schema_version": "ao.lore.private-pdf-uat-barrier.v0.1",
            "phase": "interrupted",
            "batch_id": run.batch_manifest["batch_id"],
            "manifest_digest": run.batch_manifest_digest,
            "checkpoint_digest": checkpoint["checkpoint_digest"],
            "processed": checkpoint["processed"],
            "journal_count": checkpoint["processed"],
            "run_digest": expected_contract["run_digest"],
            "authority": False,
        }
        if value != expected or _read_conversion_journal(run) != checkpoint["processed"]:
            raise OSError("private PDF UAT worker barrier drifted")
        return True
    except (ContractError, OSError, KeyError, TypeError) as exc:
        raise _fail("private PDF UAT worker barrier is invalid") from exc
    finally:
        os.close(descriptor)


def _prepare_orphan_uat_cleanup(
    runtime: Path, state_directory: Path
) -> None:
    """Authorize cleanup only for one exact abandoned prepared run."""

    evidence, _ = _load_prepared_run_evidence(state_directory, runtime)
    batch = validate_batch_manifest(evidence["batch_manifest"])
    batch_id = batch["batch_id"]
    if (
        state_directory.name != batch_id
        or re.fullmatch(r"batch-private-uat-[0-9a-f]{24}", batch_id) is None
        or evidence["cleanup"] is not None
    ):
        raise OSError("private PDF UAT orphan identity is invalid")
    corpus = validate_private_pdf_manifest(evidence["corpus_manifest"])
    origin = _validate_campaign_origin(evidence["campaign_origin"], corpus)
    qualification_path = runtime / "benchmarks" / "docling-2.118.1.json"
    qualification, qualification_body = strict_read_json(
        qualification_path, "private PDF UAT orphan qualification",
        max_bytes=1024 * 1024, root=runtime,
    )
    qualification_info = os.stat(qualification_path, follow_symlinks=False)
    verify_manifest(
        qualification,
        expected_corpus_digest=_qualification_corpus_digest(corpus, origin),
        expected_configuration_digest=PRIVATE_PDF_CONFIGURATION_DIGEST,
    )
    actual_qualification_digest = (
        "sha256:" + hashlib.sha256(qualification_body).hexdigest()
    )
    legacy_qualification_body = _manifest_body(qualification)
    legacy_qualification_digest = (
        "sha256:" + hashlib.sha256(legacy_qualification_body).hexdigest()
    )
    if (
        evidence["qualification_body_digest"]
        not in {actual_qualification_digest, legacy_qualification_digest}
        or evidence["qualification_identity"]
        != [qualification_info.st_dev, qualification_info.st_ino]
        or evidence["qualification_result_digest"] != qualification["result_digest"]
    ):
        raise OSError("private PDF UAT orphan qualification drifted")
    bound_qualification_body = (
        qualification_body
        if evidence["qualification_body_digest"] == actual_qualification_digest
        else legacy_qualification_body
    )
    corpus_root = runtime / "calibration" / "private-pdf" / corpus["corpus_id"]
    loaded, source_bodies = _load_private_calibration_bytes(corpus, corpus_root)
    corpus_info = os.stat(corpus_root, follow_symlinks=False)
    if (
        loaded != corpus
        or evidence["corpus_manifest_digest"] != canonical_digest(corpus)
        or evidence["corpus_root_identity"]
        != [corpus_info.st_dev, corpus_info.st_ino]
    ):
        raise OSError("private PDF UAT orphan corpus drifted")
    staging = repository_root() / "sources" / evidence["staging_name"]
    staging_descriptor = _open_directory(staging, repository_root(), create=False)
    try:
        staging_info = os.fstat(staging_descriptor)
        if evidence["staging_source_identity"] != [
            staging_info.st_dev, staging_info.st_ino
        ] or set(os.listdir(staging_descriptor)) != set(source_bodies):
            raise OSError("private PDF UAT orphan staging drifted")
        for name, body in source_bodies.items():
            if _read_file_at(staging_descriptor, name, MAX_PRIVATE_PDF_BYTES) != body:
                raise OSError("private PDF UAT orphan staging content drifted")
    finally:
        os.close(staging_descriptor)
    sealed_root = runtime / evidence["sealed_cache_locator"]
    sealed_container = sealed_root.parent
    sealed = PrivatePdfSealedModelCache(
        root=sealed_root,
        container=sealed_container,
        root_identity=tuple(evidence["sealed_cache_root_identity"]),
        tree_manifest=evidence["sealed_cache_tree_manifest"],
        tree_digest=evidence["sealed_cache_tree_digest"],
        file_count=evidence["sealed_cache_file_count"],
        total_bytes=evidence["sealed_cache_total_bytes"],
    )
    _validate_sealed_private_pdf_model_cache(sealed)
    journal = state_directory / "conversion-journal.jsonl"
    run = PrivatePdfUatPreparedRun(
        corpus_manifest=corpus,
        corpus_digest=evidence["corpus_manifest_digest"],
        qualification_digest=evidence["qualification_result_digest"],
        qualification_manifest=qualification,
        qualification_body=bound_qualification_body,
        qualification_locator=qualification_path,
        qualification_identity=tuple(evidence["qualification_identity"]),
        batch_manifest=batch,
        batch_manifest_digest=evidence["batch_manifest_digest"],
        manifest_locator=_UAT_MANIFEST_LOCATOR,
        runtime_root=runtime,
        corpus_root=corpus_root,
        corpus_root_identity=tuple(evidence["corpus_root_identity"]),
        staging_control_identity=tuple(evidence["staging_control_identity"]),
        staging_source_identity=tuple(evidence["staging_source_identity"]),
        staging_name=evidence["staging_name"],
        conversion_journal=journal,
        conversion_journal_identity=tuple(evidence["journal_identity"]),
        sealed_cache_root=sealed.root,
        sealed_cache_container=sealed.container,
        sealed_cache_root_identity=sealed.root_identity,
        sealed_cache_tree_manifest=sealed.tree_manifest,
        sealed_cache_tree_digest=sealed.tree_digest,
        sealed_cache_file_count=sealed.file_count,
        sealed_cache_total_bytes=sealed.total_bytes,
        origin=_validate_campaign_origin(
            evidence["campaign_origin"], corpus
        ),
    )
    _read_conversion_journal(run)
    checkpoint = _read_live_checkpoint_snapshot(run)
    if checkpoint is None:
        raise OSError("private PDF UAT orphan checkpoint is absent")
    items = checkpoint["items"]
    final = load_final_batch_readback(
        batch_id, batch_root=runtime / "batches" / batch_id
    )
    if final is not None:
        terminal = validate_final_batch_readback(final)
        if (
            terminal["items"] != items
            or terminal["processed"] != checkpoint["processed"]
        ):
            raise OSError("private PDF UAT orphan terminal drifted")
    targets: list[dict[str, Any]] = []
    for item in items:
        if item["status"] != "created":
            continue
        candidate_id = item["candidate_id"]
        verified = load_verified_candidate(candidate_id)
        inspection = verified["inspection"]
        if (
            inspection["candidate_digest"] != item["candidate_digest"]
            or inspection["provenance_digest"] != item["provenance_digest"]
            or inspection["review_status"] != "unreviewed"
            or verified["provenance"]["source_digest"] != item["source_digest"]
        ):
            raise OSError("private PDF UAT orphan candidate drifted")
        candidate_body = (
            json.dumps(verified["candidate"], indent=2, sort_keys=True) + "\n"
        ).encode()
        provenance_body = (
            json.dumps(verified["provenance"], indent=2, sort_keys=True) + "\n"
        ).encode()
        targets.append(_capture_uat_cleanup_target(
            run, "candidates", candidate_id,
            {
                "candidate.json": (MAX_RECORD_BYTES, "sha256:" + hashlib.sha256(candidate_body).hexdigest()),
                "provenance.json": (MAX_RECORD_BYTES, "sha256:" + hashlib.sha256(provenance_body).hexdigest()),
            },
            ("reviews",),
        ))
    batch_files = {
        "checkpoint.json": (
            _MAX_MANIFEST_BYTES,
            "sha256:" + hashlib.sha256(_manifest_body(checkpoint)).hexdigest(),
        )
    }
    if final is not None:
        batch_files["final-readback.json"] = (
            _MAX_MANIFEST_BYTES,
            "sha256:" + hashlib.sha256(_manifest_body(final)).hexdigest(),
        )
    targets.append(_capture_uat_cleanup_target(run, "batches", batch_id, batch_files))
    targets.append(_capture_uat_cleanup_target(
        run, "sources", run.staging_name,
        {
            name: (MAX_PRIVATE_PDF_BYTES, "sha256:" + hashlib.sha256(body).hexdigest())
            for name, body in source_bodies.items()
        },
    ))
    control = repository_root() / "sources" / ".private-pdf-uat"
    control_absent = not control.exists()
    if not control_absent:
        targets.append(_capture_uat_cleanup_target(
            run, "sources", ".private-pdf-uat",
            {
                "run-manifest.json": (
                    _MAX_MANIFEST_BYTES,
                    "sha256:" + hashlib.sha256(_manifest_body(batch)).hexdigest(),
                )
            },
        ))
    targets.append(_capture_sealed_cache_cleanup_target(run))
    _ensure_uat_cleanup_recovery(
        run, checkpoint["checkpoint_digest"], items, targets,
        control_absent=control_absent,
    )


def _completed_orphan_cleanup_is_absent(
    runtime: Path, state_directory: Path
) -> bool:
    evidence, _ = _load_prepared_run_evidence(state_directory, runtime)
    cleanup = evidence.get("cleanup")
    if not isinstance(cleanup, Mapping) or set(cleanup) not in ({
        "checkpoint_digest", "items", "targets", "quarantine_name",
    }, {
        "checkpoint_digest", "items", "targets", "quarantine_name",
        "control_absent",
    }):
        return False
    targets = cleanup.get("targets")
    if not isinstance(targets, list):
        return False
    for target in targets:
        if (
            not isinstance(target, Mapping)
            or target.get("root") not in {
                "sources", "candidates", "batches", "run-state"
            }
            or not isinstance(target.get("name"), str)
        ):
            return False
        root = _cleanup_root(
            runtime,
            target["root"],
            evidence["batch_manifest"]["batch_id"],
        )
        descriptor = _open_directory(root, repository_root(), create=False)
        try:
            if _binding_exists_at(descriptor, target["name"]):
                return False
        finally:
            os.close(descriptor)
    quarantine = cleanup.get("quarantine_name")
    return (
        isinstance(quarantine, str)
        and not (state_directory / quarantine).exists()
        and not (state_directory / "cleanup-recovery.json").exists()
    )


def _completed_legacy_orphan_cleanup_is_absent(
    runtime: Path, state_directory: Path
) -> bool:
    """Classify pre-cache cleanup as historical only; never authorize recovery."""

    value, _ = strict_read_json(
        state_directory / "prepared-run-evidence.json",
        "private PDF UAT legacy prepared evidence",
        max_bytes=_PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
        root=runtime,
    )
    keys = {
        "schema_version", "batch_manifest", "batch_manifest_digest",
        "corpus_manifest", "corpus_manifest_digest",
        "qualification_body_digest", "qualification_identity",
        "qualification_result_digest", "configuration_digest", "staging_name",
        "staging_control_identity", "staging_source_identity",
        "corpus_root_identity", "journal_identity", "cleanup", "authority",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or value.get("schema_version")
        != "ao.lore.private-pdf-uat-prepared-run.v0.1"
        or value.get("authority") is not False
    ):
        return False
    legacy = _strict_copy(value)
    batch = validate_batch_manifest(legacy["batch_manifest"])
    corpus = validate_private_pdf_manifest(legacy["corpus_manifest"])
    if (
        legacy["batch_manifest_digest"] != canonical_digest(batch)
        or legacy["corpus_manifest_digest"] != canonical_digest(corpus)
        or legacy["configuration_digest"] != PRIVATE_PDF_CONFIGURATION_DIGEST
        or re.fullmatch(
            r"private-pdf-uat-[0-9a-f]{24}", legacy["staging_name"]
        ) is None
        or [item["item_id"] for item in batch["documents"]]
        != [item["item_id"] for item in corpus["documents"]]
        or [item["source"] for item in batch["documents"]]
        != [
            f"sources/{legacy['staging_name']}/{item['item_id']}.pdf"
            for item in corpus["documents"]
        ]
    ):
        return False
    for field in (
        "qualification_body_digest", "qualification_result_digest",
        "configuration_digest",
    ):
        _require_digest(legacy[field], f"private PDF UAT legacy {field}")
    for field in (
        "qualification_identity", "staging_control_identity",
        "staging_source_identity", "corpus_root_identity", "journal_identity",
    ):
        identity = legacy[field]
        if (
            not isinstance(identity, list)
            or len(identity) != 2
            or any(
                isinstance(part, bool) or not isinstance(part, int) or part < 0
                for part in identity
            )
        ):
            return False
    cleanup = legacy["cleanup"]
    if (
        not isinstance(cleanup, Mapping)
        or set(cleanup) not in ({
            "checkpoint_digest", "items", "targets", "quarantine_name",
        }, {
            "checkpoint_digest", "items", "targets", "quarantine_name",
            "control_absent",
        })
        or not isinstance(cleanup.get("items"), list)
        or not isinstance(cleanup.get("targets"), list)
        or not isinstance(cleanup.get("control_absent", False), bool)
        or re.fullmatch(
            r"cleanup-quarantine-[0-9a-f]{24}",
            cleanup.get("quarantine_name", ""),
        ) is None
    ):
        return False
    _require_digest(
        cleanup["checkpoint_digest"],
        "private PDF UAT legacy cleanup checkpoint",
    )
    created: list[str] = []
    for item in cleanup["items"]:
        if (
            not isinstance(item, Mapping)
            or item.get("status") not in {"created", "unchanged", "rejected"}
        ):
            return False
        if item["status"] == "created":
            candidate_id = item.get("candidate_id")
            if (
                not isinstance(candidate_id, str)
                or re.fullmatch(r"candidate-[a-z0-9-]{1,64}", candidate_id)
                is None
            ):
                return False
            created.append(candidate_id)
    expected_names = (
        [("candidates", candidate_id) for candidate_id in created]
        + [("batches", batch["batch_id"])]
        + [("sources", legacy["staging_name"])]
        + (
            []
            if cleanup.get("control_absent", False)
            else [("sources", ".private-pdf-uat")]
        )
    )
    targets = cleanup["targets"]
    if [
        (target.get("root"), target.get("name"))
        for target in targets if isinstance(target, Mapping)
    ] != expected_names:
        return False
    for target in targets:
        if (
            not isinstance(target, Mapping)
            or set(target) != {
                "root", "root_identity", "name", "quarantine", "identity",
                "entries",
            }
            or not isinstance(target["entries"], list)
            or not isinstance(target["quarantine"], str)
        ):
            return False
        for field in ("root_identity", "identity"):
            identity = target[field]
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or any(
                    isinstance(part, bool) or not isinstance(part, int)
                    or part < 0 for part in identity
                )
            ):
                return False
        root = _cleanup_root(runtime, target["root"], batch["batch_id"])
        try:
            root_descriptor = _open_directory(
                root, repository_root(), create=False
            )
        except FileNotFoundError:
            continue
        try:
            if _binding_exists_at(root_descriptor, target["name"]):
                return False
        finally:
            os.close(root_descriptor)
    if (
        (state_directory / cleanup["quarantine_name"]).exists()
        or (state_directory / "cleanup-recovery.json").exists()
        or any(
            path.name.startswith("cleanup-quarantine-")
            for path in state_directory.iterdir()
        )
    ):
        return False
    return True


def _default_prepare_uat_run(
    manifest: Mapping[str, Any],
    runtime: Path,
    *,
    origin: PrivatePdfCampaignOrigin | None = None,
) -> PrivatePdfUatPreparedRun:
    state_root = runtime / "uat" / "private-pdf"
    if state_root.exists():
        state_descriptor = _open_directory(state_root, runtime, create=False)
        try:
            for name in sorted(os.listdir(state_descriptor)):
                if name == "preparation-intent.json":
                    continue
                child_descriptor = os.open(
                    name, _DIRECTORY_FLAGS, dir_fd=state_descriptor
                )
                try:
                    has_marker = _binding_exists_at(
                        child_descriptor, "cleanup-recovery.json"
                    )
                    quarantine_names = [
                        entry for entry in os.listdir(child_descriptor)
                        if entry.startswith("cleanup-quarantine-")
                    ]
                    if not has_marker:
                        if quarantine_names:
                            raise _fail("private PDF UAT cleanup recovery is invalid")
                        if not _binding_exists_at(
                            child_descriptor, "prepared-run-evidence.json"
                        ):
                            continue
                        try:
                            evidence, _ = _load_prepared_run_evidence(
                                state_root / name, runtime
                            )
                        except OSError:
                            if _completed_legacy_orphan_cleanup_is_absent(
                                runtime, state_root / name
                            ):
                                continue
                            raise
                        if evidence["cleanup"] is not None:
                            if _completed_orphan_cleanup_is_absent(
                                runtime, state_root / name
                            ):
                                continue
                            raise _fail(
                                "private PDF UAT completed cleanup is invalid"
                            )
                        _prepare_orphan_uat_cleanup(
                            runtime, state_root / name
                        )
                        has_marker = True
                    body = _read_file_at(
                        child_descriptor, "cleanup-recovery.json",
                        _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
                    )
                    recovery = parse_strict_json(
                        body, "private PDF UAT cleanup recovery"
                    )
                    if (
                        not isinstance(recovery, Mapping)
                        or set(recovery) != {
                            "schema_version", "batch_id", "manifest_digest",
                            "staging_name", "staging_control_identity",
                            "staging_source_identity", "corpus_root_identity",
                            "sealed_cache_locator", "sealed_cache_root_identity",
                            "sealed_cache_tree_digest", "sealed_cache_file_count",
                            "sealed_cache_total_bytes",
                            "quarantine_name", "quarantine_identity", "targets",
                            "states", "container_phase", "checkpoint_digest",
                            "prepared_evidence_digest", "authority",
                        }
                        or recovery.get("schema_version")
                        != "ao.lore.private-pdf-uat-cleanup-recovery.v0.1"
                        or recovery.get("batch_id") != name
                        or recovery.get("authority") is not False
                        or not isinstance(recovery.get("targets"), list)
                    ):
                        raise _fail("private PDF UAT cleanup recovery is invalid")
                    evidence, _ = _load_prepared_run_evidence(
                        state_root / name, runtime
                    )
                    _resume_uat_cleanup_value(
                        runtime, state_root / name, recovery, evidence
                    )
                    body = _read_file_at(
                        child_descriptor, "cleanup-recovery.json",
                        _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
                    )
                    _unlink_verified_at(
                        child_descriptor, "cleanup-recovery.json", body,
                        _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
                    )
                finally:
                    os.close(child_descriptor)
        except (OSError, ContractError, KeyError, TypeError) as exc:
            if isinstance(exc, PrivatePdfUatError):
                raise
            raise _fail("private PDF UAT cleanup recovery is invalid") from exc
        finally:
            os.close(state_descriptor)
    validated = validate_private_pdf_manifest(manifest)
    corpus_root = runtime / "calibration" / "private-pdf" / validated["corpus_id"]
    loaded, bodies = _load_private_calibration_bytes(validated, corpus_root)
    selected_origin = (
        _seed_campaign_origin(loaded)
        if origin is None
        else _validate_campaign_origin(
            _campaign_origin_value(origin), loaded, expected=origin
        )
    )
    qualification, qualification_body = strict_read_json(
        runtime / "benchmarks" / "docling-2.118.1.json",
        "private PDF UAT qualification",
        max_bytes=1024 * 1024,
        root=runtime,
    )
    verify_manifest(
        qualification,
        expected_corpus_digest=_qualification_corpus_digest(
            loaded, selected_origin
        ),
        expected_configuration_digest=PRIVATE_PDF_CONFIGURATION_DIGEST,
    )
    if (
        qualification.get("parser_id") != "docling"
        or qualification.get("parser_version") != "2.118.1"
        or qualification.get("failures") != []
    ):
        raise _fail("private PDF UAT qualification is invalid")
    qualification_path = runtime / "benchmarks" / "docling-2.118.1.json"
    qualification_info = os.stat(qualification_path, follow_symlinks=False)

    preparation_path = state_root / "preparation-intent.json"
    if preparation_path.exists():
        try:
            intent, intent_body = strict_read_json(
                preparation_path,
                "private PDF UAT preparation intent",
                max_bytes=_MAX_MANIFEST_BYTES,
                root=runtime,
            )
            if (
                not isinstance(intent, Mapping)
                or set(intent) != {
                    "schema_version", "phase", "staging_name",
                    "batch_manifest", "batch_manifest_digest",
                    "corpus_manifest_digest", "qualification_body_digest",
                    "qualification_identity", "states", "authority",
                }
                or intent.get("schema_version")
                != "ao.lore.private-pdf-uat-preparation.v0.1"
                or intent.get("phase") != "planned"
                or intent.get("authority") is not False
                or intent.get("corpus_manifest_digest") != canonical_digest(loaded)
                or intent.get("qualification_body_digest") not in {
                    "sha256:" + hashlib.sha256(qualification_body).hexdigest(),
                    # Recovery-only compatibility for a preparation intent
                    # written before qualification bytes were bound exactly.
                    "sha256:" + hashlib.sha256(
                        _manifest_body(qualification)
                    ).hexdigest(),
                }
                or intent.get("qualification_identity")
                != [qualification_info.st_dev, qualification_info.st_ino]
            ):
                raise OSError("preparation intent drifted")
            states = intent["states"]
            if (
                not isinstance(states, Mapping)
                or set(states) != {"staging", "control", "journal"}
            ):
                raise OSError("preparation state mapping drifted")
            for state in states.values():
                if (
                    not isinstance(state, Mapping)
                    or set(state) != {"phase", "identity", "pending", "created"}
                    or state["phase"]
                    not in {"planned", "creating", "created", "reclaiming", "completed"}
                    or (state["pending"] is not None and not isinstance(state["pending"], str))
                    or not isinstance(state["created"], list)
                    or any(not isinstance(name, str) for name in state["created"])
                    or (
                        state["identity"] is not None
                        and (
                            not isinstance(state["identity"], list)
                            or len(state["identity"]) != 2
                            or any(
                                isinstance(part, bool) or not isinstance(part, int)
                                or part < 0 for part in state["identity"]
                            )
                        )
                    )
                ):
                    raise OSError("preparation state drifted")

            def persist_recovery_intent() -> None:
                nonlocal intent_body
                intent_body = _persist_uat_document(
                    state_root, runtime, "preparation-intent.json", intent
                )
            prior_batch = validate_batch_manifest(intent["batch_manifest"])
            if (
                intent["batch_manifest_digest"] != canonical_digest(prior_batch)
                or re.fullmatch(
                    r"private-pdf-uat-[0-9a-f]{24}", intent["staging_name"]
                ) is None
                or [item["source"] for item in prior_batch["documents"]]
                != [
                    f"sources/{intent['staging_name']}/{item['item_id']}.pdf"
                    for item in loaded["documents"]
                ]
            ):
                raise OSError("preparation intent mapping drifted")
            prior_source = repository_root() / "sources" / intent["staging_name"]
            if prior_source.exists():
                source_descriptor = _open_directory(
                    prior_source, repository_root(), create=False
                )
                try:
                    info = os.fstat(source_descriptor)
                    if (
                        states["staging"]["identity"] is not None
                        and [info.st_dev, info.st_ino]
                        != states["staging"]["identity"]
                    ):
                        raise OSError("preparation source identity drifted")
                    states["staging"]["phase"] = "reclaiming"
                    persist_recovery_intent()
                    expected_names = set(bodies)
                    actual_names = set(os.listdir(source_descriptor))
                    if not actual_names.issubset(expected_names):
                        raise OSError("preparation source entries drifted")
                    for name in sorted(actual_names):
                        states["staging"]["pending"] = f"delete:{name}"
                        persist_recovery_intent()
                        _unlink_verified_at(
                            source_descriptor, name, bodies[name],
                            MAX_PRIVATE_PDF_BYTES,
                        )
                        states["staging"]["pending"] = None
                        persist_recovery_intent()
                finally:
                    os.close(source_descriptor)
                os.rmdir(prior_source)
            states["staging"].update({"phase": "completed", "pending": None})
            persist_recovery_intent()
            prior_control = repository_root() / "sources" / ".private-pdf-uat"
            if prior_control.exists():
                control_descriptor = _open_directory(
                    prior_control, repository_root(), create=False
                )
                try:
                    info = os.fstat(control_descriptor)
                    if (
                        states["control"]["identity"] is not None
                        and [info.st_dev, info.st_ino]
                        != states["control"]["identity"]
                    ):
                        raise OSError("preparation control identity drifted")
                    states["control"]["phase"] = "reclaiming"
                    persist_recovery_intent()
                    entries = set(os.listdir(control_descriptor))
                    if not entries.issubset({"run-manifest.json"}):
                        raise OSError("preparation control entries drifted")
                    if "run-manifest.json" in entries:
                        states["control"]["pending"] = "delete:run-manifest.json"
                        persist_recovery_intent()
                        _unlink_verified_at(
                            control_descriptor, "run-manifest.json",
                            _manifest_body(prior_batch), _MAX_MANIFEST_BYTES,
                        )
                        states["control"]["pending"] = None
                        persist_recovery_intent()
                finally:
                    os.close(control_descriptor)
                os.rmdir(prior_control)
            states["control"].update({"phase": "completed", "pending": None})
            persist_recovery_intent()
            prior_state = state_root / prior_batch["batch_id"]
            if prior_state.exists():
                prior_descriptor = _open_directory(
                    prior_state, runtime, create=False
                )
                try:
                    info = os.fstat(prior_descriptor)
                    if (
                        states["journal"]["identity"] is not None
                        and [info.st_dev, info.st_ino]
                        != states["journal"]["identity"]
                    ):
                        raise OSError("preparation journal identity drifted")
                    states["journal"]["phase"] = "reclaiming"
                    persist_recovery_intent()
                    entries = set(os.listdir(prior_descriptor))
                    if not entries.issubset({
                        "conversion-journal.jsonl", "prepared-run-evidence.json"
                    }):
                        raise OSError("preparation state entries drifted")
                    if "conversion-journal.jsonl" in entries:
                        states["journal"]["pending"] = "delete:conversion-journal.jsonl"
                        persist_recovery_intent()
                        _unlink_verified_at(
                            prior_descriptor, "conversion-journal.jsonl", b"",
                            _MAX_MANIFEST_BYTES,
                        )
                        states["journal"]["pending"] = None
                        persist_recovery_intent()
                    if "prepared-run-evidence.json" in entries:
                        evidence_body = _read_file_at(
                            prior_descriptor, "prepared-run-evidence.json",
                            _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
                        )
                        evidence = _validate_prepared_run_evidence(
                            parse_strict_json(
                                evidence_body, "private PDF UAT prepared evidence"
                            )
                        )
                        if (
                            evidence["batch_manifest_digest"]
                            != intent["batch_manifest_digest"]
                            or evidence["staging_name"] != intent["staging_name"]
                        ):
                            raise OSError("preparation evidence drifted")
                        _unlink_verified_at(
                            prior_descriptor, "prepared-run-evidence.json",
                            evidence_body,
                            _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
                        )
                finally:
                    os.close(prior_descriptor)
                os.rmdir(prior_state)
            states["journal"].update({"phase": "completed", "pending": None})
            persist_recovery_intent()
            intent_descriptor = _open_directory(state_root, runtime, create=False)
            try:
                _unlink_verified_at(
                    intent_descriptor, "preparation-intent.json", intent_body,
                    _MAX_MANIFEST_BYTES,
                )
            finally:
                os.close(intent_descriptor)
        except (ContractError, OSError, KeyError, TypeError) as exc:
            raise _fail("private PDF UAT preparation recovery is invalid") from exc

    staging_name = f"private-pdf-uat-{secrets.token_hex(12)}"
    source_root = repository_root() / "sources" / staging_name
    control_root = repository_root() / "sources" / ".private-pdf-uat"
    batch_documents = [
        {
            "item_id": document["item_id"],
            "source": f"sources/{staging_name}/{document['item_id']}.pdf",
            "source_digest": document["source_digest"],
        }
        for document in loaded["documents"]
    ]
    batch_manifest = validate_batch_manifest({
        "schema_version": "ao.lore.ingest-batch-manifest.v0.1",
        "batch_id": f"batch-private-uat-{secrets.token_hex(12)}",
        "documents": batch_documents,
        "continue_on_error": True,
    })
    preparation_intent = {
        "schema_version": "ao.lore.private-pdf-uat-preparation.v0.1",
        "phase": "planned",
        "staging_name": staging_name,
        "batch_manifest": batch_manifest,
        "batch_manifest_digest": canonical_digest(batch_manifest),
        "corpus_manifest_digest": canonical_digest(loaded),
        "qualification_body_digest": "sha256:" + hashlib.sha256(
            qualification_body
        ).hexdigest(),
        "qualification_identity": [
            qualification_info.st_dev, qualification_info.st_ino
        ],
        "states": {
            name: {
                "phase": "planned", "identity": None,
                "pending": None, "created": [],
            }
            for name in ("staging", "control", "journal")
        },
        "authority": False,
    }
    intent_directory = runtime / "uat" / "private-pdf"
    intent_body = _persist_uat_document(
        intent_directory,
        runtime,
        "preparation-intent.json",
        preparation_intent,
    )

    def persist_preparation() -> None:
        nonlocal intent_body
        intent_body = _persist_uat_document(
            intent_directory,
            runtime,
            "preparation-intent.json",
            preparation_intent,
        )
    source_descriptor: int | None = None
    control_descriptor: int | None = None
    journal_descriptor: int | None = None
    sealed_cache: PrivatePdfSealedModelCache | None = None
    published_sources: list[tuple[str, bytes]] = []
    manifest_published = False
    try:
        if source_root.exists() or control_root.exists():
            raise OSError("private PDF UAT staging already exists")
        preparation_intent["states"]["staging"]["phase"] = "creating"
        persist_preparation()
        source_descriptor = _open_directory(source_root, repository_root(), create=True)
        source_info = os.fstat(source_descriptor)
        preparation_intent["states"]["staging"].update({
            "phase": "created", "identity": [source_info.st_dev, source_info.st_ino]
        })
        persist_preparation()
        preparation_intent["states"]["control"]["phase"] = "creating"
        persist_preparation()
        control_descriptor = _open_directory(control_root, repository_root(), create=True)
        control_info = os.fstat(control_descriptor)
        preparation_intent["states"]["control"].update({
            "phase": "created", "identity": [control_info.st_dev, control_info.st_ino]
        })
        persist_preparation()
        for document in loaded["documents"]:
            name = f"{document['item_id']}.pdf"
            body = bodies[name]
            preparation_intent["states"]["staging"]["pending"] = name
            persist_preparation()
            _persist_bytes_at(source_descriptor, name, body)
            published_sources.append((name, body))
            preparation_intent["states"]["staging"]["pending"] = None
            preparation_intent["states"]["staging"]["created"].append(name)
            persist_preparation()
        body = _manifest_body(batch_manifest)
        preparation_intent["states"]["control"]["pending"] = "run-manifest.json"
        persist_preparation()
        _persist_bytes_at(control_descriptor, "run-manifest.json", body)
        manifest_published = True
        preparation_intent["states"]["control"]["pending"] = None
        preparation_intent["states"]["control"]["created"].append(
            "run-manifest.json"
        )
        persist_preparation()
        journal_root = (
            runtime / "uat" / "private-pdf" / batch_manifest["batch_id"]
        )
        preparation_intent["states"]["journal"]["phase"] = "creating"
        persist_preparation()
        journal_descriptor = _open_directory(
            journal_root, runtime, create=True
        )
        journal_root_info = os.fstat(journal_descriptor)
        preparation_intent["states"]["journal"].update({
            "phase": "created",
            "identity": [journal_root_info.st_dev, journal_root_info.st_ino],
            "pending": "conversion-journal.jsonl",
        })
        persist_preparation()
        _persist_bytes_at(
            journal_descriptor, "conversion-journal.jsonl", b""
        )
        preparation_intent["states"]["journal"]["pending"] = None
        preparation_intent["states"]["journal"]["created"].append(
            "conversion-journal.jsonl"
        )
        persist_preparation()
        journal_info = os.stat(
            "conversion-journal.jsonl",
            dir_fd=journal_descriptor,
            follow_symlinks=False,
        )
        sealed_cache = _seal_private_pdf_model_cache(
            runtime / "cache" / "huggingface", journal_root
        )
        prepared = PrivatePdfUatPreparedRun(
            corpus_manifest=loaded,
            corpus_digest=canonical_digest(loaded),
            qualification_digest=qualification["result_digest"],
            qualification_manifest=qualification,
            qualification_body=qualification_body,
            qualification_locator=qualification_path,
            qualification_identity=(
                qualification_info.st_dev, qualification_info.st_ino
            ),
            batch_manifest=batch_manifest,
            batch_manifest_digest=canonical_digest(batch_manifest),
            manifest_locator=_UAT_MANIFEST_LOCATOR,
            runtime_root=runtime,
            corpus_root=corpus_root,
            corpus_root_identity=(
                os.stat(corpus_root, follow_symlinks=False).st_dev,
                os.stat(corpus_root, follow_symlinks=False).st_ino,
            ),
            staging_control_identity=(
                os.fstat(control_descriptor).st_dev,
                os.fstat(control_descriptor).st_ino,
            ),
            staging_source_identity=(
                os.fstat(source_descriptor).st_dev,
                os.fstat(source_descriptor).st_ino,
            ),
            staging_name=staging_name,
            conversion_journal=journal_root / "conversion-journal.jsonl",
            conversion_journal_identity=(journal_info.st_dev, journal_info.st_ino),
            sealed_cache_root=sealed_cache.root,
            sealed_cache_container=sealed_cache.container,
            sealed_cache_root_identity=sealed_cache.root_identity,
            sealed_cache_tree_manifest=sealed_cache.tree_manifest,
            sealed_cache_tree_digest=sealed_cache.tree_digest,
            sealed_cache_file_count=sealed_cache.file_count,
            sealed_cache_total_bytes=sealed_cache.total_bytes,
            origin=selected_origin,
        )
        _persist_prepared_run_evidence(prepared)
        intent_descriptor = _open_directory(
            intent_directory, runtime, create=False
        )
        try:
            _unlink_verified_at(
                intent_descriptor,
                "preparation-intent.json",
                intent_body,
                _MAX_MANIFEST_BYTES,
            )
        finally:
            os.close(intent_descriptor)
        return prepared
    except BaseException:
        if sealed_cache is not None:
            try:
                _remove_sealed_private_pdf_model_cache(sealed_cache)
            except BaseException:
                pass
        if control_descriptor is not None and manifest_published:
            try:
                _unlink_verified_at(
                    control_descriptor,
                    "run-manifest.json",
                    _manifest_body(batch_manifest),
                    _MAX_MANIFEST_BYTES,
                )
            except BaseException:
                pass
        if source_descriptor is not None:
            for name, body in reversed(published_sources):
                try:
                    _unlink_verified_at(
                        source_descriptor, name, body, MAX_PRIVATE_PDF_BYTES
                    )
                except BaseException:
                    pass
        try:
            if control_root.exists():
                os.rmdir(control_root)
            if source_root.exists():
                os.rmdir(source_root)
        except OSError:
            pass
        raise
    finally:
        if journal_descriptor is not None:
            os.close(journal_descriptor)
        if control_descriptor is not None:
            os.close(control_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)


def _persist_uat_document(
    directory: Path,
    runtime: Path,
    name: str,
    value: Mapping[str, Any],
) -> bytes:
    maximum = (
        _PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES
        if name in {
            "prepared-run-evidence.json",
            "worker-run-contract.json",
            _CALIBRATION_CONTRACT_NAME,
            "cleanup-recovery.json",
        }
        else _MAX_MANIFEST_BYTES
    )
    body = _manifest_body(value, maximum)
    descriptor = _open_directory(directory, runtime, create=True)
    temporary = f".{name}-{secrets.token_hex(12)}.tmp"
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=descriptor,
        )
        view = memoryview(body)
        while view:
            written = os.write(file_descriptor, view)
            if written <= 0:
                raise OSError("private PDF UAT document short write")
            view = view[written:]
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
        if _read_file_at(descriptor, name, maximum) != body:
            raise OSError("private PDF UAT document reopen drifted")
        return body
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def _persist_calibration_result(
    directory: Path,
    runtime: Path,
    value: Mapping[str, Any],
) -> bytes:
    """Atomically publish the worker result without replacing any binding."""

    body = _manifest_body(value, _MAX_MANIFEST_BYTES)
    descriptor = _open_directory(directory, runtime, create=False)
    try:
        _publish_calibration_artifact_at(
            descriptor,
            _CALIBRATION_RESULT_NAME,
            body,
            _MAX_MANIFEST_BYTES,
        )
        return body
    finally:
        os.close(descriptor)


def _prepared_run_evidence(
    run: PrivatePdfUatPreparedRun,
    cleanup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    qualification_body = run.qualification_body
    return {
        "schema_version": "ao.lore.private-pdf-uat-prepared-run.v0.1",
        "batch_manifest": _strict_copy(run.batch_manifest),
        "batch_manifest_digest": run.batch_manifest_digest,
        "corpus_manifest": _strict_copy(run.corpus_manifest),
        "corpus_manifest_digest": run.corpus_digest,
        "qualification_body_digest": "sha256:" + hashlib.sha256(
            qualification_body
        ).hexdigest(),
        "qualification_identity": list(run.qualification_identity),
        "qualification_result_digest": run.qualification_digest,
        "configuration_digest": PRIVATE_PDF_CONFIGURATION_DIGEST,
        "staging_name": run.staging_name,
        "staging_control_identity": list(run.staging_control_identity),
        "staging_source_identity": list(run.staging_source_identity),
        "corpus_root_identity": list(run.corpus_root_identity),
        "journal_identity": list(run.conversion_journal_identity),
        "sealed_cache_locator": (
            f"uat/private-pdf/{run.batch_manifest['batch_id']}/"
            f"{run.sealed_cache_container.name}/huggingface"
        ),
        "sealed_cache_root_identity": list(run.sealed_cache_root_identity),
        "sealed_cache_tree_manifest": _strict_copy(
            run.sealed_cache_tree_manifest
        ),
        "sealed_cache_tree_digest": run.sealed_cache_tree_digest,
        "sealed_cache_file_count": run.sealed_cache_file_count,
        "sealed_cache_total_bytes": run.sealed_cache_total_bytes,
        "campaign_origin": _campaign_origin_value(run.origin),
        "campaign_origin_digest": _campaign_origin_digest(run.origin),
        "cleanup": None if cleanup is None else _strict_copy(cleanup),
        "authority": False,
    }


def _validate_prepared_run_evidence(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "batch_manifest", "batch_manifest_digest",
        "corpus_manifest", "corpus_manifest_digest",
        "qualification_body_digest", "qualification_identity",
        "qualification_result_digest", "configuration_digest", "staging_name",
        "staging_control_identity", "staging_source_identity",
        "corpus_root_identity", "journal_identity", "sealed_cache_locator",
        "sealed_cache_root_identity", "sealed_cache_tree_manifest",
        "sealed_cache_tree_digest", "sealed_cache_file_count",
        "sealed_cache_total_bytes", "campaign_origin",
        "campaign_origin_digest", "cleanup", "authority",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or value.get("schema_version")
        != "ao.lore.private-pdf-uat-prepared-run.v0.1"
        or value.get("authority") is not False
    ):
        raise OSError("private PDF UAT prepared evidence is invalid")
    result = _strict_copy(value)
    batch = validate_batch_manifest(result["batch_manifest"])
    corpus = validate_private_pdf_manifest(result["corpus_manifest"])
    origin = _validate_campaign_origin(result["campaign_origin"], corpus)
    if (
        result["batch_manifest_digest"] != canonical_digest(batch)
        or result["corpus_manifest_digest"] != canonical_digest(corpus)
        or result["configuration_digest"] != PRIVATE_PDF_CONFIGURATION_DIGEST
        or result["campaign_origin_digest"] != _campaign_origin_digest(origin)
        or re.fullmatch(r"private-pdf-uat-[0-9a-f]{24}", result["staging_name"])
        is None
        or [item["item_id"] for item in batch["documents"]]
        != [item["item_id"] for item in corpus["documents"]]
        or [item["source"] for item in batch["documents"]]
        != [
            f"sources/{result['staging_name']}/{item['item_id']}.pdf"
            for item in corpus["documents"]
        ]
    ):
        raise OSError("private PDF UAT prepared evidence drifted")
    for field in (
        "qualification_body_digest", "qualification_result_digest",
        "configuration_digest",
    ):
        _require_digest(result[field], f"private PDF UAT evidence {field}")
    for field in (
        "qualification_identity", "staging_control_identity",
        "staging_source_identity", "corpus_root_identity", "journal_identity",
        "sealed_cache_root_identity",
    ):
        identity = result[field]
        if (
            not isinstance(identity, list)
            or len(identity) != 2
            or any(isinstance(part, bool) or not isinstance(part, int) or part < 0 for part in identity)
        ):
            raise OSError("private PDF UAT prepared evidence identity is invalid")
    tree = result["sealed_cache_tree_manifest"]
    if (
        not isinstance(tree, Mapping)
        or result["sealed_cache_tree_digest"] != canonical_digest(tree)
        or result["sealed_cache_file_count"] != tree.get("file_count")
        or result["sealed_cache_total_bytes"] != tree.get("total_bytes")
        or isinstance(result["sealed_cache_file_count"], bool)
        or not isinstance(result["sealed_cache_file_count"], int)
        or isinstance(result["sealed_cache_total_bytes"], bool)
        or not isinstance(result["sealed_cache_total_bytes"], int)
        or re.fullmatch(
            rf"uat/private-pdf/{re.escape(batch['batch_id'])}/"
            r"sealed-cache-[0-9a-f]{24}/huggingface",
            result["sealed_cache_locator"],
        ) is None
    ):
        raise OSError("private PDF UAT prepared cache evidence is invalid")
    return result


def _load_prepared_run_evidence(
    directory: Path, runtime: Path
) -> tuple[dict[str, Any], bytes]:
    value, body = strict_read_json(
        directory / "prepared-run-evidence.json",
        "private PDF UAT prepared evidence",
        max_bytes=_PRIVATE_PDF_CACHE_MANIFEST_MAX_BYTES,
        root=runtime,
    )
    return _validate_prepared_run_evidence(value), body


def _persist_prepared_run_evidence(
    run: PrivatePdfUatPreparedRun,
    cleanup: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    value = _prepared_run_evidence(run, cleanup)
    _validate_prepared_run_evidence(value)
    _persist_uat_document(
        run.conversion_journal.parent,
        run.runtime_root,
        "prepared-run-evidence.json",
        value,
    )
    return value


def _cleanup_root(
    runtime: Path, kind: str, batch_id: str | None = None
) -> Path:
    roots = {
        "sources": repository_root() / "sources",
        "candidates": _candidate_root(None),
        "batches": runtime / "batches",
    }
    if kind == "run-state":
        if (
            not isinstance(batch_id, str)
            or re.fullmatch(r"batch-private-uat-[a-z0-9-]{1,64}", batch_id) is None
        ):
            raise OSError("private PDF UAT cleanup batch is invalid")
        return runtime / "uat" / "private-pdf" / batch_id
    if kind not in roots:
        raise OSError("private PDF UAT cleanup root is invalid")
    return roots[kind]


def _capture_uat_cleanup_target(
    run: PrivatePdfUatPreparedRun | Path,
    kind: str,
    name: str,
    expected_files: Mapping[str, tuple[int, str]],
    expected_directories: Sequence[str] = (),
    *,
    batch_id: str | None = None,
) -> dict[str, Any]:
    selected_runtime = run.runtime_root if isinstance(run, PrivatePdfUatPreparedRun) else run
    selected_batch_id = batch_id if batch_id is not None else (
        run.batch_manifest["batch_id"]
        if isinstance(run, PrivatePdfUatPreparedRun)
        else None
    )
    root = _cleanup_root(selected_runtime, kind, selected_batch_id)
    root_descriptor = _open_directory(root, repository_root(), create=False)
    target_descriptor: int | None = None
    try:
        root_info = os.fstat(root_descriptor)
        target_descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=root_descriptor)
        target_info = os.fstat(target_descriptor)
        entries: list[dict[str, Any]] = []
        expected_dir_set = set(expected_directories)
        expected_file_set = set(expected_files)
        if len(expected_dir_set) != len(expected_directories):
            raise OSError("private PDF UAT cleanup directory mapping is invalid")

        def walk(descriptor: int, prefix: tuple[str, ...]) -> None:
            actual = set(os.listdir(descriptor))
            direct_files = {
                PurePosixPath(path).name
                for path in expected_file_set
                if PurePosixPath(path).parts[:-1] == prefix
            }
            direct_directories = {
                PurePosixPath(path).name
                for path in expected_dir_set
                if PurePosixPath(path).parts[:-1] == prefix
            }
            if actual != direct_files | direct_directories:
                raise OSError("private PDF UAT cleanup target entries drifted")
            for child in sorted(direct_directories):
                relative = PurePosixPath(*prefix, child).as_posix()
                child_descriptor = os.open(child, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    info = os.fstat(child_descriptor)
                    entries.append({
                        "path": relative,
                        "type": "directory",
                        "identity": [info.st_dev, info.st_ino],
                    })
                    walk(child_descriptor, (*prefix, child))
                finally:
                    os.close(child_descriptor)
            for child in sorted(direct_files):
                relative = PurePosixPath(*prefix, child).as_posix()
                maximum, expected_digest = expected_files[relative]
                info, size, digest = _hash_file_at(
                    descriptor, child, maximum
                )
                if digest != expected_digest:
                    raise OSError("private PDF UAT cleanup target content drifted")
                entries.append({
                    "path": relative,
                    "type": "file",
                    "identity": [info.st_dev, info.st_ino],
                    "size": size,
                    "digest": digest,
                    "maximum": maximum,
                })

        walk(target_descriptor, ())
        return {
            "root": kind,
            "root_identity": [root_info.st_dev, root_info.st_ino],
            "name": name,
            "quarantine": f"{kind}-{len(expected_files)}-{secrets.token_hex(12)}",
            "identity": [target_info.st_dev, target_info.st_ino],
            "entries": entries,
        }
    finally:
        if target_descriptor is not None:
            os.close(target_descriptor)
        os.close(root_descriptor)


def _capture_sealed_cache_cleanup_target(
    run: PrivatePdfUatPreparedRun,
) -> dict[str, Any]:
    _validate_sealed_private_pdf_model_cache(
        _sealed_cache_from_prepared_run(run)
    )
    files: dict[str, tuple[int, str]] = {}
    directories = ["huggingface"]
    for entry in run.sealed_cache_tree_manifest["entries"]:
        path = f"huggingface/{entry['path']}"
        if entry["type"] == "directory":
            directories.append(path)
        else:
            files[path] = (entry["size"], entry["digest"])
    return _capture_uat_cleanup_target(
        run,
        "run-state",
        run.sealed_cache_container.name,
        files,
        directories,
    )


def _ensure_uat_cleanup_recovery(
    run: PrivatePdfUatPreparedRun,
    checkpoint_digest: str,
    items: Sequence[Mapping[str, Any]],
    targets: Sequence[Mapping[str, Any]],
    *,
    control_absent: bool = False,
) -> dict[str, Any]:
    created_ids = [
        item["candidate_id"] for item in items if item["status"] == "created"
    ]
    expected_names = (
        [("candidates", candidate_id) for candidate_id in created_ids]
        + [("batches", run.batch_manifest["batch_id"])]
        + [("sources", run.staging_name)]
        + ([] if control_absent else [("sources", ".private-pdf-uat")])
        + [("run-state", run.sealed_cache_container.name)]
    )
    if [
        (target.get("root"), target.get("name")) for target in targets
    ] != expected_names:
        raise OSError("private PDF UAT cleanup allowlist is invalid")
    quarantine_name = f"cleanup-quarantine-{secrets.token_hex(12)}"
    cleanup = {
        "checkpoint_digest": checkpoint_digest,
        "items": [_strict_copy(item) for item in items],
        "targets": [_strict_copy(target) for target in targets],
        "quarantine_name": quarantine_name,
        "control_absent": control_absent,
    }
    evidence = _persist_prepared_run_evidence(run, cleanup)
    recovery = {
        "schema_version": "ao.lore.private-pdf-uat-cleanup-recovery.v0.1",
        "batch_id": run.batch_manifest["batch_id"],
        "manifest_digest": run.batch_manifest_digest,
        "staging_name": run.staging_name,
        "staging_control_identity": list(run.staging_control_identity),
        "staging_source_identity": list(run.staging_source_identity),
        "corpus_root_identity": list(run.corpus_root_identity),
        "sealed_cache_locator": (
            f"uat/private-pdf/{run.batch_manifest['batch_id']}/"
            f"{run.sealed_cache_container.name}/huggingface"
        ),
        "sealed_cache_root_identity": list(run.sealed_cache_root_identity),
        "sealed_cache_tree_digest": run.sealed_cache_tree_digest,
        "sealed_cache_file_count": run.sealed_cache_file_count,
        "sealed_cache_total_bytes": run.sealed_cache_total_bytes,
        "quarantine_name": quarantine_name,
        "quarantine_identity": None,
        "targets": list(targets),
        "states": {
            target["quarantine"]: {
                "phase": "planned", "pending": None, "reclaimed": [],
            }
            for target in targets
        },
        "container_phase": "planned",
        "checkpoint_digest": checkpoint_digest,
        "prepared_evidence_digest": canonical_digest(evidence),
        "authority": False,
    }
    directory = run.conversion_journal.parent
    _persist_uat_document(
        directory, run.runtime_root, "cleanup-recovery.json", recovery
    )
    quarantine = directory / quarantine_name
    parent_descriptor = _open_directory(directory, run.runtime_root, create=False)
    try:
        if _binding_exists_at(parent_descriptor, quarantine_name):
            raise OSError("private PDF UAT cleanup quarantine collision")
    finally:
        os.close(parent_descriptor)
    quarantine_descriptor = _open_directory(
        quarantine, run.runtime_root, create=True
    )
    try:
        quarantine_info = os.fstat(quarantine_descriptor)
        recovery["quarantine_identity"] = [
            quarantine_info.st_dev, quarantine_info.st_ino
        ]
    finally:
        os.close(quarantine_descriptor)
    _persist_uat_document(
        directory, run.runtime_root, "cleanup-recovery.json", recovery
    )
    return recovery


def _validate_cleanup_recovery_authority(
    recovery: Mapping[str, Any], evidence: Mapping[str, Any]
) -> None:
    prepared = _validate_prepared_run_evidence(evidence)
    cleanup = prepared["cleanup"]
    if (
        not isinstance(cleanup, Mapping)
        or set(cleanup) not in ({
            "checkpoint_digest", "items", "targets", "quarantine_name",
        }, {
            "checkpoint_digest", "items", "targets", "quarantine_name",
            "control_absent",
        })
        or not isinstance(cleanup["items"], list)
        or not isinstance(cleanup["targets"], list)
    ):
        raise OSError("private PDF UAT cleanup evidence is invalid")
    created_ids = []
    for item in cleanup["items"]:
        if not isinstance(item, Mapping) or item.get("status") not in {
            "created", "unchanged", "rejected"
        }:
            raise OSError("private PDF UAT cleanup item evidence is invalid")
        if item["status"] == "created":
            created_ids.append(item.get("candidate_id"))
    expected_names = (
        [("candidates", candidate_id) for candidate_id in created_ids]
        + [("batches", prepared["batch_manifest"]["batch_id"])]
        + [("sources", prepared["staging_name"])]
        + (
            []
            if cleanup.get("control_absent", False) is True
            else [("sources", ".private-pdf-uat")]
        )
        + [(
            "run-state",
            PurePosixPath(prepared["sealed_cache_locator"]).parts[-2],
        )]
    )
    targets = cleanup["targets"]
    if (
        [(target.get("root"), target.get("name")) for target in targets]
        != expected_names
        or recovery.get("targets") != targets
        or recovery.get("quarantine_name") != cleanup["quarantine_name"]
        or recovery.get("checkpoint_digest") != cleanup["checkpoint_digest"]
        or not isinstance(cleanup.get("control_absent", False), bool)
        or recovery.get("prepared_evidence_digest")
        != canonical_digest(prepared)
        or recovery.get("batch_id")
        != prepared["batch_manifest"]["batch_id"]
        or recovery.get("manifest_digest")
        != prepared["batch_manifest_digest"]
        or recovery.get("staging_name") != prepared["staging_name"]
        or recovery.get("staging_control_identity")
        != prepared["staging_control_identity"]
        or recovery.get("staging_source_identity")
        != prepared["staging_source_identity"]
        or recovery.get("corpus_root_identity")
        != prepared["corpus_root_identity"]
        or recovery.get("sealed_cache_locator")
        != prepared["sealed_cache_locator"]
        or recovery.get("sealed_cache_root_identity")
        != prepared["sealed_cache_root_identity"]
        or recovery.get("sealed_cache_tree_digest")
        != prepared["sealed_cache_tree_digest"]
        or recovery.get("sealed_cache_file_count")
        != prepared["sealed_cache_file_count"]
        or recovery.get("sealed_cache_total_bytes")
        != prepared["sealed_cache_total_bytes"]
    ):
        raise OSError("private PDF UAT cleanup authority drifted")


def _resume_uat_cleanup_recovery(
    run: PrivatePdfUatPreparedRun, recovery: Mapping[str, Any]
) -> None:
    evidence, _ = _load_prepared_run_evidence(
        run.conversion_journal.parent, run.runtime_root
    )
    _resume_uat_cleanup_value(
        run.runtime_root, run.conversion_journal.parent, recovery, evidence
    )


def _resume_uat_cleanup_value(
    runtime: Path,
    state_directory: Path,
    recovery: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> None:
    _validate_cleanup_recovery_authority(recovery, evidence)
    working = json.loads(json.dumps(recovery, allow_nan=False))
    states = working.get("states")
    expected_quarantines = {
        target["quarantine"] for target in working["targets"]
    }
    if (
        not isinstance(states, dict)
        or set(states) != expected_quarantines
        or working.get("container_phase")
        not in {"planned", "reclaiming", "completed"}
    ):
        raise OSError("private PDF UAT cleanup progress is invalid")
    for state in states.values():
        if (
            not isinstance(state, dict)
            or set(state) != {"phase", "pending", "reclaimed"}
            or state["phase"]
            not in {"planned", "quarantined", "reclaiming", "completed"}
            or (state["pending"] is not None and not isinstance(state["pending"], str))
            or not isinstance(state["reclaimed"], list)
            or any(not isinstance(item, str) for item in state["reclaimed"])
            or len(set(state["reclaimed"])) != len(state["reclaimed"])
        ):
            raise OSError("private PDF UAT cleanup target progress is invalid")

    def persist_progress() -> None:
        _persist_uat_document(
            state_directory, runtime, "cleanup-recovery.json", working
        )

    quarantine = state_directory / working["quarantine_name"]
    try:
        quarantine_descriptor = _open_directory(
            quarantine, runtime, create=False
        )
    except FileNotFoundError:
        if (
            working["quarantine_identity"] is None
            and working["container_phase"] == "planned"
            and all(state["phase"] == "planned" for state in states.values())
        ):
            for target in working["targets"]:
                files = {
                    entry["path"]: (entry["maximum"], entry["digest"])
                    for entry in target["entries"] if entry["type"] == "file"
                }
                directories = [
                    entry["path"] for entry in target["entries"]
                    if entry["type"] == "directory"
                ]
                observed = _capture_uat_cleanup_target(
                    runtime,
                    target["root"],
                    target["name"],
                    files,
                    directories,
                    batch_id=working["batch_id"],
                )
                if any(
                    observed[key] != target[key]
                    for key in (
                        "root", "root_identity", "name", "identity", "entries"
                    )
                ):
                    raise OSError("private PDF UAT unstarted cleanup plan drifted")
            quarantine_descriptor = _open_directory(
                quarantine, runtime, create=True
            )
            qinfo = os.fstat(quarantine_descriptor)
            working["quarantine_identity"] = [qinfo.st_dev, qinfo.st_ino]
            persist_progress()
        elif (
            all(state["phase"] == "completed" for state in states.values())
            and working["container_phase"] in {"reclaiming", "completed"}
        ):
            if working["container_phase"] == "reclaiming":
                working["container_phase"] = "completed"
                persist_progress()
            return
        else:
            raise
    try:
        qinfo = os.fstat(quarantine_descriptor)
        if working["quarantine_identity"] is None:
            if os.listdir(quarantine_descriptor):
                raise OSError("private PDF UAT unbound quarantine is not empty")
            working["quarantine_identity"] = [qinfo.st_dev, qinfo.st_ino]
            persist_progress()
        elif [qinfo.st_dev, qinfo.st_ino] != working["quarantine_identity"]:
            raise OSError("private PDF UAT cleanup quarantine drifted")
        for target in working["targets"]:
            state = states[target["quarantine"]]
            root = _cleanup_root(
                runtime, target["root"], working["batch_id"]
            )
            root_descriptor = _open_directory(root, repository_root(), create=False)
            moved_descriptor: int | None = None
            try:
                root_info = os.fstat(root_descriptor)
                if [root_info.st_dev, root_info.st_ino] != target["root_identity"]:
                    raise OSError("private PDF UAT cleanup namespace drifted")
                source_exists = _binding_exists_at(root_descriptor, target["name"])
                retained_exists = _binding_exists_at(
                    quarantine_descriptor, target["quarantine"]
                )
                if source_exists and retained_exists:
                    raise OSError("private PDF UAT cleanup duplicate binding")
                if source_exists:
                    if state["phase"] != "planned":
                        raise OSError("private PDF UAT cleanup source reappeared")
                    current = os.stat(
                        target["name"], dir_fd=root_descriptor,
                        follow_symlinks=False,
                    )
                    if [current.st_dev, current.st_ino] != target["identity"]:
                        raise OSError("private PDF UAT cleanup binding drifted")
                    _rename_noreplace_at(
                        root_descriptor, target["name"],
                        quarantine_descriptor, target["quarantine"],
                    )
                    os.fsync(root_descriptor)
                    os.fsync(quarantine_descriptor)
                    retained_exists = True
                    state["phase"] = "quarantined"
                    persist_progress()
                if not retained_exists:
                    if (
                        state["phase"] == "reclaiming"
                        and state["pending"] == "target"
                        and set(state["reclaimed"])
                        == {
                            f"{entry['type']}:{entry['path']}"
                            for entry in target["entries"]
                        }
                    ):
                        state["pending"] = None
                        state["reclaimed"].append("target")
                        state["phase"] = "completed"
                        persist_progress()
                        continue
                    if state["phase"] == "completed" and "target" in state["reclaimed"]:
                        continue
                    raise OSError("private PDF UAT cleanup binding disappeared")
                if state["phase"] == "completed":
                    raise OSError("private PDF UAT cleanup binding reappeared")
                moved_descriptor = os.open(
                    target["quarantine"], _DIRECTORY_FLAGS,
                    dir_fd=quarantine_descriptor,
                )
                moved_info = os.fstat(moved_descriptor)
                if [moved_info.st_dev, moved_info.st_ino] != target["identity"]:
                    raise OSError("private PDF UAT cleanup quarantine binding drifted")
                if state["phase"] == "planned":
                    state["phase"] = "quarantined"
                    persist_progress()
                if state["phase"] == "quarantined":
                    state["phase"] = "reclaiming"
                    persist_progress()
                file_entries = {
                    entry["path"]: entry
                    for entry in target["entries"] if entry["type"] == "file"
                }
                directory_entries = {
                    entry["path"]: entry
                    for entry in target["entries"] if entry["type"] == "directory"
                }
                def open_target_directory(parts: Sequence[str]) -> int:
                    if target["root"] == "run-state":
                        return _open_cache_directory_at(moved_descriptor, parts)
                    return _open_relative_directory(
                        moved_descriptor, parts, create=False
                    )
                # Sealed cache directories are intentionally 0555. Cleanup
                # authority first verifies each retained binding, then makes
                # only those exact quarantined directories writable so their
                # digest-bound children can be reclaimed.
                for path, entry in sorted(
                    directory_entries.items(),
                    key=lambda pair: len(PurePosixPath(pair[0]).parts),
                ):
                    child = open_target_directory(PurePosixPath(path).parts)
                    try:
                        info = os.fstat(child)
                        if [info.st_dev, info.st_ino] != entry["identity"]:
                            raise OSError(
                                "private PDF UAT cleanup directory binding drifted"
                            )
                        os.fchmod(child, 0o700)
                        os.fsync(child)
                    finally:
                        os.close(child)
                expected_top = {
                    PurePosixPath(path).parts[0]
                    for path in (*file_entries, *directory_entries)
                }
                if not set(os.listdir(moved_descriptor)).issubset(expected_top):
                    raise OSError("private PDF UAT cleanup quarantine entries drifted")
                for path, entry in sorted(
                    file_entries.items(),
                    key=lambda pair: len(PurePosixPath(pair[0]).parts),
                    reverse=True,
                ):
                    token = f"file:{path}"
                    parts = PurePosixPath(path).parts
                    parent_descriptor = open_target_directory(
                        parts[:-1]
                    ) if len(parts) > 1 else os.dup(moved_descriptor)
                    try:
                        if not _binding_exists_at(parent_descriptor, parts[-1]):
                            if state["pending"] == token:
                                state["pending"] = None
                                state["reclaimed"].append(token)
                                persist_progress()
                                continue
                            if token in state["reclaimed"]:
                                continue
                            raise OSError("private PDF UAT cleanup file disappeared")
                        if token in state["reclaimed"]:
                            raise OSError("private PDF UAT cleanup file reappeared")
                        if state["pending"] not in {None, token}:
                            raise OSError("private PDF UAT cleanup pending entry drifted")
                        if state["pending"] is None:
                            state["pending"] = token
                            persist_progress()
                        info = os.stat(
                            parts[-1], dir_fd=parent_descriptor,
                            follow_symlinks=False,
                        )
                        if [info.st_dev, info.st_ino] != entry["identity"]:
                            raise OSError("private PDF UAT cleanup file binding drifted")
                        if target["root"] == "run-state":
                            _unlink_verified_digest_at(
                                parent_descriptor,
                                parts[-1],
                                entry["size"],
                                entry["digest"],
                                entry["maximum"],
                            )
                        else:
                            body = _read_file_at(
                                parent_descriptor,
                                parts[-1],
                                entry["maximum"],
                            )
                            if (
                                len(body) != entry["size"]
                                or "sha256:" + hashlib.sha256(body).hexdigest()
                                != entry["digest"]
                            ):
                                raise OSError(
                                    "private PDF UAT cleanup file drifted"
                                )
                            _unlink_verified_at(
                                parent_descriptor,
                                parts[-1],
                                body,
                                entry["maximum"],
                            )
                        state["pending"] = None
                        state["reclaimed"].append(token)
                        persist_progress()
                    finally:
                        os.close(parent_descriptor)
                for path, entry in sorted(
                    directory_entries.items(),
                    key=lambda pair: len(PurePosixPath(pair[0]).parts),
                    reverse=True,
                ):
                    token = f"directory:{path}"
                    parts = PurePosixPath(path).parts
                    parent_descriptor = open_target_directory(
                        parts[:-1]
                    ) if len(parts) > 1 else os.dup(moved_descriptor)
                    try:
                        if not _binding_exists_at(parent_descriptor, parts[-1]):
                            if state["pending"] == token:
                                state["pending"] = None
                                state["reclaimed"].append(token)
                                persist_progress()
                                continue
                            if token in state["reclaimed"]:
                                continue
                            raise OSError("private PDF UAT cleanup directory disappeared")
                        if token in state["reclaimed"]:
                            raise OSError("private PDF UAT cleanup directory reappeared")
                        if state["pending"] not in {None, token}:
                            raise OSError("private PDF UAT cleanup pending entry drifted")
                        if state["pending"] is None:
                            state["pending"] = token
                            persist_progress()
                        child_descriptor = os.open(
                            parts[-1], _DIRECTORY_FLAGS, dir_fd=parent_descriptor
                        )
                        try:
                            info = os.fstat(child_descriptor)
                            if (
                                [info.st_dev, info.st_ino] != entry["identity"]
                                or os.listdir(child_descriptor)
                            ):
                                raise OSError("private PDF UAT cleanup directory drifted")
                        finally:
                            os.close(child_descriptor)
                        os.rmdir(parts[-1], dir_fd=parent_descriptor)
                        os.fsync(parent_descriptor)
                        state["pending"] = None
                        state["reclaimed"].append(token)
                        persist_progress()
                    finally:
                        os.close(parent_descriptor)
                if os.listdir(moved_descriptor):
                    raise OSError("private PDF UAT cleanup target remains")
            finally:
                if moved_descriptor is not None:
                    os.close(moved_descriptor)
                os.close(root_descriptor)
            expected_reclaimed = {
                f"{entry['type']}:{entry['path']}" for entry in target["entries"]
            }
            if set(state["reclaimed"]) != expected_reclaimed:
                raise OSError("private PDF UAT cleanup ledger is incomplete")
            state["pending"] = "target"
            persist_progress()
            os.rmdir(target["quarantine"], dir_fd=quarantine_descriptor)
            os.fsync(quarantine_descriptor)
            state["pending"] = None
            state["reclaimed"].append("target")
            state["phase"] = "completed"
            persist_progress()
        if os.listdir(quarantine_descriptor):
            raise OSError("private PDF UAT cleanup quarantine remains")
    finally:
        os.close(quarantine_descriptor)
    working["container_phase"] = "reclaiming"
    persist_progress()
    os.rmdir(quarantine)
    working["container_phase"] = "completed"
    persist_progress()


def _cleanup_uat_stage_files(run: PrivatePdfUatPreparedRun) -> None:
    _, bodies = _load_private_calibration_bytes(run.corpus_manifest, run.corpus_root)
    source_root = repository_root() / "sources" / run.staging_name
    control_root = repository_root() / "sources" / ".private-pdf-uat"
    source_descriptor = _open_directory(source_root, repository_root(), create=False)
    control_descriptor = _open_directory(control_root, repository_root(), create=False)
    try:
        expected_names = {
            f"{item['item_id']}.pdf" for item in run.corpus_manifest["documents"]
        }
        if (
            set(os.listdir(source_descriptor)) != expected_names
            or set(os.listdir(control_descriptor)) != {"run-manifest.json"}
        ):
            raise OSError("private PDF UAT staging entries drifted")
        _unlink_verified_at(
            control_descriptor,
            "run-manifest.json",
            _manifest_body(run.batch_manifest),
            _MAX_MANIFEST_BYTES,
        )
        for item in run.corpus_manifest["documents"]:
            name = f"{item['item_id']}.pdf"
            _unlink_verified_at(
                source_descriptor, name, bodies[name], MAX_PRIVATE_PDF_BYTES
            )
    finally:
        os.close(control_descriptor)
        os.close(source_descriptor)
    os.rmdir(control_root)
    os.rmdir(source_root)


def _cleanup_partial_uat_state(
    run: PrivatePdfUatPreparedRun,
    before_candidates: Mapping[str, Mapping[str, Any]],
) -> None:
    checkpoint = load_or_initialize_checkpoint(
        run.batch_manifest,
        run.batch_manifest_digest,
        batch_root=run.runtime_root / "batches" / run.batch_manifest["batch_id"],
    )
    _verify_queue(
        _default_queue_items(), {"items": checkpoint["items"]}, before_candidates
    )
    targets: list[dict[str, Any]] = []
    for item in checkpoint["items"]:
        if item["status"] != "created":
            continue
        candidate_id = item["candidate_id"]
        verified = load_verified_candidate(candidate_id)
        if (
            candidate_id in before_candidates
            or verified["inspection"]["candidate_digest"] != item["candidate_digest"]
            or verified["inspection"]["provenance_digest"] != item["provenance_digest"]
            or verified["provenance"]["source_digest"] != item["source_digest"]
            or verified["inspection"]["review_status"] != "unreviewed"
        ):
            raise OSError("private PDF UAT partial candidate cleanup drifted")
        candidate_body = (
            json.dumps(verified["candidate"], indent=2, sort_keys=True) + "\n"
        ).encode()
        provenance_body = (
            json.dumps(verified["provenance"], indent=2, sort_keys=True) + "\n"
        ).encode()
        targets.append(_capture_uat_cleanup_target(
            run, "candidates", candidate_id,
            {
                "candidate.json": (MAX_RECORD_BYTES, "sha256:" + hashlib.sha256(candidate_body).hexdigest()),
                "provenance.json": (MAX_RECORD_BYTES, "sha256:" + hashlib.sha256(provenance_body).hexdigest()),
            },
            ("reviews",),
        ))
    checkpoint_body = _manifest_body(checkpoint)
    targets.append(_capture_uat_cleanup_target(
        run, "batches", run.batch_manifest["batch_id"],
        {"checkpoint.json": (_MAX_MANIFEST_BYTES, "sha256:" + hashlib.sha256(checkpoint_body).hexdigest())},
    ))
    _, source_bodies = _load_private_calibration_bytes(
        run.corpus_manifest, run.corpus_root
    )
    targets.append(_capture_uat_cleanup_target(
        run, "sources", run.staging_name,
        {
            name: (MAX_PRIVATE_PDF_BYTES, "sha256:" + hashlib.sha256(body).hexdigest())
            for name, body in source_bodies.items()
        },
    ))
    manifest_body = _manifest_body(run.batch_manifest)
    targets.append(_capture_uat_cleanup_target(
        run, "sources", ".private-pdf-uat",
        {"run-manifest.json": (_MAX_MANIFEST_BYTES, "sha256:" + hashlib.sha256(manifest_body).hexdigest())},
    ))
    targets.append(_capture_sealed_cache_cleanup_target(run))
    recovery = _ensure_uat_cleanup_recovery(
        run, checkpoint["checkpoint_digest"], checkpoint["items"], targets
    )
    _resume_uat_cleanup_recovery(run, recovery)


def _default_cleanup_uat_staging(
    run: PrivatePdfUatPreparedRun,
    before_candidates: Mapping[str, Mapping[str, Any]],
    final_report: Mapping[str, Any],
) -> bool:
    terminal = validate_final_batch_readback(final_report)
    current_queue = _default_queue_items()
    _verify_queue(current_queue, terminal, before_candidates)
    targets: list[dict[str, Any]] = []
    for item in terminal["items"]:
        if item["status"] != "created":
            continue
        candidate_id = item["candidate_id"]
        verified = load_verified_candidate(candidate_id)
        inspection = verified["inspection"]
        if (
            candidate_id in before_candidates
            or inspection["candidate_digest"] != item["candidate_digest"]
            or inspection["provenance_digest"] != item["provenance_digest"]
            or verified["provenance"]["source_digest"] != item["source_digest"]
            or inspection["review_status"] != "unreviewed"
        ):
            raise OSError("private PDF UAT candidate cleanup binding drifted")
        candidate_body = (
            json.dumps(verified["candidate"], indent=2, sort_keys=True) + "\n"
        ).encode()
        provenance_body = (
            json.dumps(verified["provenance"], indent=2, sort_keys=True) + "\n"
        ).encode()
        targets.append(_capture_uat_cleanup_target(
            run, "candidates", candidate_id,
            {
            "candidate.json": (MAX_RECORD_BYTES, "sha256:" + hashlib.sha256(candidate_body).hexdigest()),
            "provenance.json": (MAX_RECORD_BYTES, "sha256:" + hashlib.sha256(provenance_body).hexdigest()),
            }, ("reviews",),
        ))
    checkpoint = load_or_initialize_checkpoint(
        run.batch_manifest,
        run.batch_manifest_digest,
        batch_root=run.runtime_root / "batches" / run.batch_manifest["batch_id"],
    )
    if checkpoint["processed"] != len(run.batch_manifest["documents"]):
        raise OSError("private PDF UAT batch cleanup is not terminal")
    checkpoint_body = _manifest_body(checkpoint)
    final_body = _manifest_body(terminal)
    targets.append(_capture_uat_cleanup_target(
        run, "batches", run.batch_manifest["batch_id"], {
            "checkpoint.json": (_MAX_MANIFEST_BYTES, "sha256:" + hashlib.sha256(checkpoint_body).hexdigest()),
            "final-readback.json": (_MAX_MANIFEST_BYTES, "sha256:" + hashlib.sha256(final_body).hexdigest()),
        }
    ))
    _, source_bodies = _load_private_calibration_bytes(run.corpus_manifest, run.corpus_root)
    targets.append(_capture_uat_cleanup_target(
        run, "sources", run.staging_name, {
            name: (MAX_PRIVATE_PDF_BYTES, "sha256:" + hashlib.sha256(body).hexdigest())
            for name, body in source_bodies.items()
        }
    ))
    manifest_body = _manifest_body(run.batch_manifest)
    targets.append(_capture_uat_cleanup_target(
        run, "sources", ".private-pdf-uat", {
            "run-manifest.json": (_MAX_MANIFEST_BYTES, "sha256:" + hashlib.sha256(manifest_body).hexdigest())
        }
    ))
    targets.append(_capture_sealed_cache_cleanup_target(run))
    recovery = _ensure_uat_cleanup_recovery(
        run, checkpoint["checkpoint_digest"], terminal["items"], targets
    )
    _resume_uat_cleanup_recovery(run, recovery)
    return True


def _default_private_pdf_uat_dependencies(
    runtime: Path,
    *,
    manifest_loader: Callable[[Path], Mapping[str, Any]] | None = None,
    origin_provider: Callable[
        [Mapping[str, Any]], PrivatePdfCampaignOrigin
    ] | None = None,
) -> PrivatePdfUatDependencies:
    state: dict[str, Any] = {"before": None, "final": None, "run": None}

    def prepare(
        manifest: Mapping[str, Any], selected_runtime: Path
    ) -> PrivatePdfUatPreparedRun:
        selected_origin = (
            None if origin_provider is None else origin_provider(manifest)
        )
        run = _default_prepare_uat_run(
            manifest, selected_runtime, origin=selected_origin
        )
        state["run"] = run
        return run

    def conversions() -> int:
        if not isinstance(state["run"], PrivatePdfUatPreparedRun):
            raise _fail("private PDF UAT conversion journal is not prepared")
        return _read_conversion_journal(state["run"])

    def candidates() -> Mapping[str, Mapping[str, Any]]:
        snapshot = _default_candidate_snapshot()
        state["before"] = snapshot
        return snapshot

    def inspect(run: PrivatePdfUatPreparedRun) -> Mapping[str, Any] | None:
        return _read_live_checkpoint_snapshot(run)

    def final(run: PrivatePdfUatPreparedRun) -> Mapping[str, Any] | None:
        report = load_final_batch_readback(
            run.batch_manifest["batch_id"],
            batch_root=runtime / "batches" / run.batch_manifest["batch_id"],
        )
        if report is not None:
            state["final"] = report
        return report

    def cleanup(run: PrivatePdfUatPreparedRun) -> bool:
        if not isinstance(state["before"], Mapping):
            # Prepared-run validation failed before any worker launch. The
            # current exact queue is therefore the non-mutated cleanup baseline.
            state["before"] = _default_candidate_snapshot()
        if not isinstance(state["final"], Mapping):
            _cleanup_partial_uat_state(run, state["before"])
            return True
        return _default_cleanup_uat_staging(run, state["before"], state["final"])

    def calibrate(
        manifest: Mapping[str, Any], corpus_root: Path
    ) -> PrivatePdfCalibrationEvidence:
        if not isinstance(state["run"], PrivatePdfUatPreparedRun):
            raise _fail("private PDF UAT sealed cache is not prepared")
        run = state["run"]
        if (
            validate_private_pdf_manifest(manifest) != run.corpus_manifest
            or Path(corpus_root) != run.corpus_root
        ):
            raise _fail("private PDF calibration invocation drifted")
        return _run_private_pdf_calibration_worker(run)

    return PrivatePdfUatDependencies(
        load_manifest=(
            manifest_loader
            if manifest_loader is not None
            else lambda selected: load_private_pdf_manifest(runtime_root=selected)
        ),
        prepare_run=prepare,
        brain_snapshot=_default_brain_snapshot,
        candidate_snapshot=candidates,
        conversion_snapshot=conversions,
        launch_process=_default_launch_process,
        process_poll=_default_process_poll,
        inspect_checkpoint=inspect,
        inspect_barrier=_read_worker_barrier,
        send_signal=_default_send_signal,
        terminate_process=_terminate_process_group,
        wait_process=_default_wait_process,
        load_final=final,
        load_queue=_default_queue_items,
        evaluate_calibration=calibrate,
        cleanup_run=cleanup,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )


def representative_private_pdf_uat_dependencies(
    review: ReviewedDomainCorpus,
    source_root: Path,
    *,
    runtime_root: Path | None = None,
) -> PrivatePdfUatDependencies:
    """Compose reviewed representative intake with the default UAT services."""

    if type(review) is not ReviewedDomainCorpus or not isinstance(source_root, Path):
        raise _fail("private PDF representative campaign is invalid")
    runtime = runtime_home() if runtime_root is None else Path(runtime_root)
    runtime, _ = ensure_contained(
        runtime, repository_root(), "private PDF representative runtime root"
    )
    selected_origin: PrivatePdfCampaignOrigin | None = None

    def load(selected_runtime: Path) -> Mapping[str, Any]:
        nonlocal selected_origin
        if Path(selected_runtime) != runtime:
            raise _fail("private PDF representative runtime drifted")
        manifest = prepare_representative_pdf_corpus(
            review, source_root, runtime_root=runtime
        )
        selected_origin = _representative_campaign_origin(review, manifest)
        return manifest

    def origin_for(manifest: Mapping[str, Any]) -> PrivatePdfCampaignOrigin:
        if selected_origin is None:
            raise _fail("private PDF representative campaign is not prepared")
        return _validate_campaign_origin(
            _campaign_origin_value(selected_origin),
            manifest,
            expected=selected_origin,
        )

    return _default_private_pdf_uat_dependencies(
        runtime,
        manifest_loader=load,
        origin_provider=origin_for,
    )
