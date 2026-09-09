"""Strict runtime-only contracts for held private DOCX NOMAGIC intake."""

from __future__ import annotations

import json
import ctypes
import errno
import fcntl
import os
import re
import secrets
import stat
import struct
import time
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from dataclasses import field
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping
from typing import Sequence

from ._strict_io import ensure_contained, parse_strict_json, reject_symlink_ancestors
from .benchmark import canonical_digest
from .docx_semantic_contract import COUNT_KEYS as _COUNT_KEYS
from .docx_semantic_contract import REL_NS as _REL_NS
from .docx_semantic_contract import W_NS as _W_NS
from .home import repository_root, runtime_home


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_PRIVATE_CORPUS_ID = "docx-nomagic-uk-public-sector-v1"
DOCX_TRANSFORMATION_ID = "restore-ooxml-local-header-v1"
MAX_DOCX_BYTES = 50 * 1024 * 1024
EXPECTED_REJECTIONS = frozenset({"invalid_package", "unsupported_active_content"})
DOCX_UAT_ITEM_IDS = tuple(f"docx-domain-{index:02d}" for index in range(1, 5))

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_ZIP_DEFLATE_OPTION_FLAGS = 0x0006
_ZIP_ALLOWED_GENERAL_PURPOSE_FLAGS = 0x0800 | _ZIP_DEFLATE_OPTION_FLAGS
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.docx$")
_DOCX_REVIEW_SCHEMA_VERSION = "ao.lore.private-docx-domain-review.v0.1"
_DOCX_CORPUS_SCHEMA_VERSION = "ao.lore.private-docx-corpus-manifest.v0.1"
_EXPECTATION_SCHEMA_VERSION = "ao.lore.private-docx-expectation.v0.1"
_QUALIFICATION_INPUT_SCHEMA_VERSION = (
    "ao.lore.private-docx-qualification-inputs.v0.1"
)
_EXPECTATION_REASON = "ooxml-pagination-unavailable"
_EXPECTATION_REJECTIONS = frozenset({"active-content", "invalid-package"})
_EXPECTED_ACCEPTED_COUNT = 88
_EXPECTED_ACTIVE_REJECTION_COUNT = 11
_EXPECTED_INVALID_REJECTION_COUNT = 1
_EXPECTATION_ITEM_IDS = tuple(f"docx-{index:04d}" for index in range(1, 101))
_QUALIFICATION_FIXTURE_NAMES = tuple(
    f"{index:04d}-reviewed.docx" for index in range(1, 101)
)
_REVIEW_KEYS = {
    "schema_version",
    "corpus_id",
    "transformation_id",
    "documents",
    "ocr_enabled",
    "network_accessed",
    "provider_calls",
    "promotion_authority",
    "claims_authority_advance",
}
_REVIEW_DOCUMENT_KEYS = {"item_id", "source_file"}
_CORPUS_KEYS = {
    "schema_version",
    "corpus_id",
    "review_digest",
    "expectation_digest",
    "documents",
    "ocr_enabled",
    "network_accessed",
    "provider_calls",
    "promotion_authority",
    "claims_authority_advance",
}
_CORPUS_DOCUMENT_KEYS = {
    "item_id",
    "source_name",
    "derived_name",
    "source_digest",
    "derived_digest",
    "source_bytes",
    "derived_bytes",
    "transformation_id",
}
_EXPECTATION_KEYS = {
    "schema_version",
    "corpus_id",
    "items",
    "ocr_enabled",
    "network_accessed",
    "provider_calls",
    "promotion_authority",
    "claims_authority_advance",
}
_EXPECTATION_ITEM_KEYS = {
    "item_id",
    "source_digest",
    "derived_digest",
    "source_bytes",
    "derived_bytes",
    "transformation_id",
    "package_inventory_digest",
    "normalized_text_digest",
    "structural_event_digest",
    "counts",
    "expected_outcome",
    "expected_rejection",
    "page_count",
    "page_count_reason",
}
_QUALIFICATION_INPUT_KEYS = {
    "schema_version",
    "corpus_id",
    "expectation_digest",
    "source_set_digest",
    "derived_set_digest",
    "transformation_id",
    "documents",
    "ocr_enabled",
    "network_accessed",
    "provider_calls",
    "promotion_authority",
    "claims_authority_advance",
}
_QUALIFICATION_INPUT_DOCUMENT_KEYS = {
    "item_id",
    "fixture_name",
    "source_digest",
    "derived_digest",
    "source_bytes",
    "derived_bytes",
    "transformation_id",
}
_CONTROL_BYTES = frozenset(range(0, 32)) | {127}
_ATTACHED_TEMPLATE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"
)
_PACKAGE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"
)
_ACTIVE_REL_TYPES = frozenset(
    {
        _ATTACHED_TEMPLATE_REL,
        _PACKAGE_REL,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/control",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/controlProperties",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject",
        "http://schemas.microsoft.com/office/2006/relationships/activeXControlBinary",
    }
)
_MAX_EXPECTATION_RELATIONSHIP_BYTES = 4 * 1024 * 1024
_MAX_EXPECTATION_RELATIONSHIPS = 100_000
_LOCK_NAME = ".private-docx.lock"
_QUALIFICATION_INPUT_FINAL = "qualification-inputs"
_QUALIFICATION_INPUT_STAGE = ".qualification-inputs-staging"
_QUALIFICATION_INPUT_INTENT = ".qualification-inputs-intent.json"
_QUALIFICATION_INPUT_RECLAIM = ".qualification-inputs-reclaim"
_LOCK_TIMEOUT_SECONDS = 2.0
_RENAME_NOREPLACE = 1


class PrivateDocxDomainError(RuntimeError):
    """Raised at the stable private DOCX reviewed-source boundary."""


@dataclass(frozen=True)
class ReviewedDocxSource:
    item_id: str
    source_name: str
    source_digest: str
    derived_digest: str
    source_bytes: int
    derived_bytes: int
    transformation_id: str


@dataclass(frozen=True)
class PreparedDocxSource:
    item_id: str
    source_digest: str
    derived_digest: str
    source_bytes: int
    derived_bytes: int
    transformation_id: str
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class DocxExpectationCorpus:
    corpus_id: str
    corpus_digest: str
    configuration_digest: str
    documents: tuple[Mapping[str, Any], ...]


def _sha256(body: bytes) -> str:
    return "sha256:" + sha256(body).hexdigest()


def _open_directory(path: Path, root: Path, *, create: bool) -> int:
    selected, absolute_root = ensure_contained(path, root, "private DOCX runtime state")
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


def _open_runtime_root(runtime_root: Path | None) -> tuple[Path, int]:
    selected = runtime_home() if runtime_root is None else Path(runtime_root)
    absolute, _ = ensure_contained(selected, repository_root(), "private DOCX runtime root")
    descriptor = _open_directory(absolute, repository_root(), create=True)
    return absolute, descriptor


def _open_relative_directory(parent_descriptor: int, parts: Sequence[str], *, create: bool) -> int:
    current = os.dup(parent_descriptor)
    try:
        for component in parts:
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", component):
                raise OSError("invalid private DOCX directory component")
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
            raise OSError("invalid private DOCX directory")
        return current
    except BaseException:
        os.close(current)
        raise


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
            raise OSError("private DOCX lock binding is invalid")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("private DOCX lock acquisition timed out")
                time.sleep(0.01)
        opened = os.fstat(descriptor)
        public = os.stat(_LOCK_NAME, dir_fd=private_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(public.st_mode)
            or public.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (public.st_dev, public.st_ino)
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("private DOCX lock binding changed")
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


def _binding_exists_at(directory_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _coerce_sha256(value: str) -> str:
    if type(value) is not str:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    if value.startswith("sha256:"):
        if _SHA256_RE.fullmatch(value) is None:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
        return value
    if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
        return f"sha256:{value}"
    raise PrivateDocxDomainError("private DOCX expectation is invalid")


def _stable_metadata(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
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
    if not body or len(body) > maximum:
        raise OSError("source size is invalid")
    return body


def _read_file_at(directory_descriptor: int, name: str, maximum: int) -> bytes:
    before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum
    ):
        raise OSError("invalid private DOCX state")
    descriptor = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_descriptor
    )
    try:
        opened = os.fstat(descriptor)
        if _stable_metadata(before) != _stable_metadata(opened):
            raise OSError("private DOCX state changed")
        body = _read_bounded(descriptor, maximum)
        after = os.fstat(descriptor)
        public = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            _stable_metadata(opened) != _stable_metadata(after)
            or _stable_metadata(opened) != _stable_metadata(public)
        ):
            raise OSError("private DOCX state changed")
        return body
    finally:
        os.close(descriptor)


def _open_bound_empty_directory_at(
    directory_descriptor: int,
    name: str,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, os.stat_result]:
    before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise OSError("private DOCX empty directory binding is invalid")
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_descriptor)
    try:
        opened = os.fstat(descriptor)
        public = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            _stable_metadata(before) != _stable_metadata(opened)
            or _stable_metadata(opened) != _stable_metadata(public)
            or (
                expected_identity is not None
                and (opened.st_dev, opened.st_ino) != expected_identity
            )
            or os.listdir(descriptor)
        ):
            raise OSError("private DOCX empty directory binding changed")
        return descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _remove_exact_empty_directory_at(directory_descriptor: int, name: str) -> None:
    """Remove one canonical empty directory under the cooperative-writer lock.

    A non-cooperative same-UID writer can race the final verified-name-to-rmdir
    syscall boundary. No same-UID-readable filesystem journal can remove that
    boundary or authenticate crash residue, so no durable cleanup intent is used.
    """

    if name not in {"corpus", ".corpus-staging"}:
        raise OSError("private DOCX empty directory name is invalid")
    descriptor, opened = _open_bound_empty_directory_at(directory_descriptor, name)
    try:
        public = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            (public.st_dev, public.st_ino) != (opened.st_dev, opened.st_ino)
            or os.listdir(descriptor)
        ):
            raise OSError("private DOCX empty directory binding changed")
        try:
            os.rmdir(name, dir_fd=directory_descriptor)
        except BaseException:
            try:
                os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.fsync(directory_descriptor)
                except Exception:
                    pass
            raise
    finally:
        os.close(descriptor)
    os.fsync(directory_descriptor)


def _rollback_name(name: str) -> str:
    return f".rollback-{sha256(name.encode('utf-8')).hexdigest()[:24]}.quarantine"


def _persist_bytes_at(directory_descriptor: int, name: str, body: bytes) -> None:
    if "/" in name or name in {"", ".", ".."}:
        raise OSError("invalid private DOCX state name")
    try:
        os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise OSError("private DOCX state collision")
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
            raise OSError("invalid private DOCX temporary")
        identity = (info.st_dev, info.st_ino)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short private DOCX write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        current = os.stat(temporary, dir_fd=directory_descriptor, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
            raise OSError("private DOCX temporary changed")
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
            raise OSError("private DOCX persisted bytes drifted")
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
                published = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
                if stat.S_ISREG(published.st_mode) and (published.st_dev, published.st_ino) == identity:
                    rollback = _rollback_name(name)
                    _rename_noreplace_at(directory_descriptor, name, directory_descriptor, rollback)
                    os.fsync(directory_descriptor)
            except FileNotFoundError:
                pass


def _unlink_verified_at(directory_descriptor: int, name: str, expected_body: bytes, maximum: int) -> None:
    before = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if _read_file_at(directory_descriptor, name, maximum) != expected_body:
        raise OSError("private DOCX reclaim digest drifted")
    after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if _stable_metadata(before) != _stable_metadata(after):
        raise OSError("private DOCX reclaim binding drifted")
    os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)


def _manifest_body(value: Mapping[str, Any], maximum: int = 64 * 1024) -> bytes:
    body = (
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(body) > maximum:
        raise OSError("private DOCX manifest exceeded")
    return body


def _validate_reviewed_source(value: ReviewedDocxSource) -> None:
    if type(value) is not ReviewedDocxSource:
        raise OSError("review binding is invalid")
    if (
        type(value.item_id) is not str
        or not value.item_id
        or type(value.source_name) is not str
        or _SOURCE_NAME_RE.fullmatch(value.source_name) is None
        or value.source_name in {".docx", "..docx"}
        or type(value.source_digest) is not str
        or _SHA256_RE.fullmatch(value.source_digest) is None
        or type(value.derived_digest) is not str
        or _SHA256_RE.fullmatch(value.derived_digest) is None
        or value.source_digest == value.derived_digest
        or type(value.source_bytes) is not int
        or value.source_bytes <= 0
        or value.source_bytes > MAX_DOCX_BYTES
        or type(value.derived_bytes) is not int
        or value.derived_bytes != value.source_bytes
        or value.derived_bytes > MAX_DOCX_BYTES
        or value.transformation_id != DOCX_TRANSFORMATION_ID
    ):
        raise OSError("review binding is invalid")


def _validated_review(value: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    if type(value) is not dict or set(value) != _REVIEW_KEYS:
        raise PrivateDocxDomainError("private DOCX review is invalid")
    detached = _validate_builtin_json(value)
    if (
        detached["schema_version"] != _DOCX_REVIEW_SCHEMA_VERSION
        or detached["corpus_id"] != DOCX_PRIVATE_CORPUS_ID
        or detached["transformation_id"] != DOCX_TRANSFORMATION_ID
        or detached["ocr_enabled"] is not False
        or detached["network_accessed"] is not False
        or detached["provider_calls"] is not False
        or detached["promotion_authority"] is not False
        or detached["claims_authority_advance"] is not False
    ):
        raise PrivateDocxDomainError("private DOCX review is invalid")
    documents = detached["documents"]
    if type(documents) is not list or len(documents) != len(DOCX_UAT_ITEM_IDS):
        raise PrivateDocxDomainError("private DOCX review is invalid")
    result: list[tuple[str, str]] = []
    seen_files: set[str] = set()
    for index, document in enumerate(documents):
        if type(document) is not dict or set(document) != _REVIEW_DOCUMENT_KEYS:
            raise PrivateDocxDomainError("private DOCX review is invalid")
        item_id = document["item_id"]
        source_file = document["source_file"]
        if (
            item_id != DOCX_UAT_ITEM_IDS[index]
            or type(source_file) is not str
            or _SOURCE_NAME_RE.fullmatch(source_file) is None
            or source_file in {".docx", "..docx"}
            or source_file in seen_files
        ):
            raise PrivateDocxDomainError("private DOCX review is invalid")
        seen_files.add(source_file)
        result.append((item_id, source_file))
    return tuple(result)


def _derive_reviewed_sources(
    source_root: Path,
    review: Sequence[tuple[str, str]],
) -> tuple[tuple[ReviewedDocxSource, ...], tuple[PreparedDocxSource, ...]]:
    root_descriptor: int | None = None
    try:
        if not isinstance(source_root, Path) or not review:
            raise OSError("review source contract is invalid")
        reject_symlink_ancestors(source_root, include_self=True)
        root_before = os.lstat(source_root)
        if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
            raise OSError("review source root is invalid")
        root_descriptor = os.open(source_root, _DIRECTORY_FLAGS)
        root_opened = os.fstat(root_descriptor)
        if (root_before.st_dev, root_before.st_ino) != (root_opened.st_dev, root_opened.st_ino):
            raise OSError("review source root changed")
        reviewed_sources: list[ReviewedDocxSource] = []
        prepared_sources: list[PreparedDocxSource] = []
        for item_id, source_name in review:
            before = os.stat(source_name, dir_fd=root_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1
                or before.st_size <= 0
                or before.st_size > MAX_DOCX_BYTES
            ):
                raise OSError("review source binding is invalid")
            descriptor = os.open(
                source_name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                if _stable_metadata(before) != _stable_metadata(opened):
                    raise OSError("review source changed")
                source = _read_bounded(descriptor, MAX_DOCX_BYTES)
                derived = restore_nomagic_docx(source)
                after = os.fstat(descriptor)
                public = os.stat(source_name, dir_fd=root_descriptor, follow_symlinks=False)
                if (
                    _stable_metadata(opened) != _stable_metadata(after)
                    or _stable_metadata(opened) != _stable_metadata(public)
                ):
                    raise OSError("review source changed")
            finally:
                os.close(descriptor)
            reviewed = ReviewedDocxSource(
                item_id=item_id,
                source_name=source_name,
                source_digest=_sha256(source),
                derived_digest=_sha256(derived),
                source_bytes=len(source),
                derived_bytes=len(derived),
                transformation_id=DOCX_TRANSFORMATION_ID,
            )
            reviewed_sources.append(reviewed)
            prepared_sources.append(
                PreparedDocxSource(
                    item_id=item_id,
                    source_digest=reviewed.source_digest,
                    derived_digest=reviewed.derived_digest,
                    source_bytes=reviewed.source_bytes,
                    derived_bytes=reviewed.derived_bytes,
                    transformation_id=reviewed.transformation_id,
                    body=derived,
                )
            )
        root_after = os.fstat(root_descriptor)
        root_public = os.lstat(source_root)
        if (
            (root_opened.st_dev, root_opened.st_ino) != (root_after.st_dev, root_after.st_ino)
            or (root_opened.st_dev, root_opened.st_ino) != (root_public.st_dev, root_public.st_ino)
        ):
            raise OSError("review source root changed")
        return tuple(reviewed_sources), tuple(prepared_sources)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        raise PrivateDocxDomainError("private DOCX corpus preparation failed") from None
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _raw_restore_nomagic_docx(source: bytes) -> bytes:
    if type(source) is not bytes or not (8 <= len(source) <= MAX_DOCX_BYTES) or source[:4] != b"\x00" * 4:
        raise PrivateDocxDomainError("private DOCX source condition is invalid")
    derived = b"PK\x03\x04" + source[4:]
    if derived[:4] != b"PK\x03\x04" or derived[4:] != source[4:] or len(derived) < 30:
        raise PrivateDocxDomainError("private DOCX source condition is invalid")
    try:
        local_fields = struct.unpack_from("<4s5H3L2H", derived, 0)
    except struct.error as exc:
        raise PrivateDocxDomainError("private DOCX source condition is invalid") from exc
    version_needed = local_fields[1]
    flags = local_fields[2]
    method = local_fields[3]
    name_length = int.from_bytes(derived[26:28], "little")
    extra_length = int.from_bytes(derived[28:30], "little")
    header_end = 30 + name_length + extra_length
    if (
        version_needed <= 0
        or (flags & ~_ZIP_ALLOWED_GENERAL_PURPOSE_FLAGS) != 0
        or method not in {0, 8}
        or (flags & _ZIP_DEFLATE_OPTION_FLAGS and method != 8)
        or name_length <= 0
        or header_end > len(derived)
        or b"\x00" in derived[30:30 + name_length]
        or any(byte < 32 or byte == 127 for byte in derived[30:30 + name_length])
    ):
        raise PrivateDocxDomainError("private DOCX source condition is invalid")
    return derived


def restore_nomagic_docx(source: bytes) -> bytes:
    """Restore the exact four-byte OOXML local-header signature removed by NOMAGIC."""

    derived = _raw_restore_nomagic_docx(source)
    from .docx_ooxml import DocxPackageError, validate_docx_package

    try:
        validate_docx_package(derived)
    except DocxPackageError as exc:
        raise PrivateDocxDomainError("private DOCX source condition is invalid") from exc
    return derived


def _q(namespace: str, tag: str) -> str:
    return f"{{{namespace}}}{tag}"


def _xml_root(body: bytes) -> ET.Element:
    if type(body) is not bytes or b"\x00" in body:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PrivateDocxDomainError("private DOCX expectation is invalid") from exc
    stripped = text.lstrip()
    if "<!DOCTYPE" in stripped or "<!ENTITY" in stripped:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise PrivateDocxDomainError("private DOCX expectation is invalid") from exc


def _raw_parts(derived: bytes) -> dict[str, bytes]:
    try:
        with zipfile.ZipFile(BytesIO(derived)) as archive:
            infos = sorted(archive.infolist(), key=lambda item: item.filename)
            names: set[str] = set()
            folded: set[str] = set()
            parts: dict[str, bytes] = {}
            for info in infos:
                name = info.filename
                if (
                    type(name) is not str
                    or not name
                    or name.startswith("/")
                    or "\\" in name
                    or any(ord(character) in _CONTROL_BYTES for character in name)
                    or name in names
                    or name.casefold() in folded
                ):
                    raise PrivateDocxDomainError("private DOCX expectation is invalid")
                names.add(name)
                folded.add(name.casefold())
                parts[name] = archive.read(name)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise PrivateDocxDomainError("private DOCX expectation is invalid") from exc
    return parts


def _relationships(parts: Mapping[str, bytes], name: str) -> tuple[dict[str, dict[str, str]], bool]:
    if name not in parts:
        return {}, False
    if len(parts[name]) > _MAX_EXPECTATION_RELATIONSHIP_BYTES:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    root = _xml_root(parts[name])
    if root.tag != _q(_REL_NS, "Relationships"):
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    result: dict[str, dict[str, str]] = {}
    active = False
    relationships = list(root)
    if len(relationships) > _MAX_EXPECTATION_RELATIONSHIPS:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    for relationship in relationships:
        if relationship.tag != _q(_REL_NS, "Relationship"):
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
        rel_id = relationship.attrib.get("Id")
        rel_type = relationship.attrib.get("Type")
        target = relationship.attrib.get("Target", "")
        if type(rel_id) is not str or not rel_id or type(rel_type) is not str or not rel_type:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
        if rel_id in result:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
        result[rel_id] = {
            "Type": rel_type,
            "Target": target,
            "TargetMode": relationship.attrib.get("TargetMode", ""),
        }
        if len(target) > 64 * 1024:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
        if rel_type in _ACTIVE_REL_TYPES:
            active = True
    return result, active


def _raw_inventory_digest(parts: Mapping[str, bytes]) -> str:
    return canonical_digest(
        [
            {"name": name, "digest": _sha256(parts[name])}
            for name in sorted(parts)
        ]
    )


def _active_content(parts: Mapping[str, bytes]) -> bool:
    relationship_active = False
    for name in sorted(part for part in parts if part.endswith(".rels")):
        _, active = _relationships(parts, name)
        relationship_active = relationship_active or active
    if relationship_active:
        return True
    for name in parts:
        folded = name.casefold()
        if (
            folded.startswith("word/embeddings/")
            or folded.startswith("word/activex/")
            or "vbaproject" in folded
            or "oleobject" in folded
            or "afchunk" in folded
        ):
            return True
    for name in sorted(
        part for part in parts if part.startswith("word/") and part.endswith(".xml")
    ):
        root = _xml_root(parts[name])
        if any(node.tag == _q(_W_NS, "altChunk") for node in root.iter()):
            return True
    return False


def _validate_builtin_json(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is list:
        return [_validate_builtin_json(item) for item in value]
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise PrivateDocxDomainError("private DOCX expectation is invalid")
            result[key] = _validate_builtin_json(item)
        return result
    raise PrivateDocxDomainError("private DOCX expectation is invalid")


def _validate_expectation_item(item: Mapping[str, Any], index: int) -> dict[str, Any]:
    if type(item) is not dict or set(item) != _EXPECTATION_ITEM_KEYS:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    detached = _validate_builtin_json(item)
    if detached["item_id"] != _EXPECTATION_ITEM_IDS[index]:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    for field in (
        "source_digest",
        "derived_digest",
        "package_inventory_digest",
        "normalized_text_digest",
        "structural_event_digest",
    ):
        if type(detached[field]) is not str or _SHA256_RE.fullmatch(detached[field]) is None:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
    if detached["source_digest"] == detached["derived_digest"]:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    for field in ("source_bytes", "derived_bytes"):
        if type(detached[field]) is not int or detached[field] <= 0 or detached[field] > MAX_DOCX_BYTES:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
    if detached["transformation_id"] != DOCX_TRANSFORMATION_ID:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    counts = detached["counts"]
    if type(counts) is not dict or set(counts) != set(_COUNT_KEYS):
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    normalized_counts: dict[str, int] = {}
    for name in _COUNT_KEYS:
        if type(counts[name]) is not int or counts[name] < 0:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
        normalized_counts[name] = counts[name]
    detached["counts"] = normalized_counts
    outcome = detached["expected_outcome"]
    rejection = detached["expected_rejection"]
    if outcome == "accept":
        if rejection is not None:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
    elif outcome == "reject":
        if rejection not in _EXPECTATION_REJECTIONS:
            raise PrivateDocxDomainError("private DOCX expectation is invalid")
    else:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    if detached["page_count"] is not None or detached["page_count_reason"] != _EXPECTATION_REASON:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    return detached


def _derive_expectation_item(
    index: int,
    source: bytes,
    derived: bytes,
) -> dict[str, Any]:
    from .docx_expectation_oracle import derive_docx_semantic_expectation
    from .docx_ooxml import DocxLimits, DocxPackageError, validate_docx_package

    parts = _raw_parts(derived)
    counts = {key: 0 for key in _COUNT_KEYS}
    text_digest = canonical_digest([])
    structural_digest = canonical_digest([])
    if _active_content(parts):
        inventory_digest = _raw_inventory_digest(parts)
        outcome = "reject"
        rejection = "active-content"
    else:
        try:
            package = validate_docx_package(derived)
            inventory_digest = _coerce_sha256(package.inventory_digest)
        except DocxPackageError:
            inventory_digest = _raw_inventory_digest(parts)
            outcome = "reject"
            rejection = "invalid-package"
        else:
            signals = derive_docx_semantic_expectation(package, DocxLimits())
            counts = dict(signals.counts)
            text_digest = signals.normalized_text_digest
            structural_digest = signals.structural_event_digest
            outcome = "accept"
            rejection = None
    return _validate_expectation_item(
        {
            "item_id": _EXPECTATION_ITEM_IDS[index],
            "source_digest": _sha256(source),
            "derived_digest": _sha256(derived),
            "source_bytes": len(source),
            "derived_bytes": len(derived),
            "transformation_id": DOCX_TRANSFORMATION_ID,
            "package_inventory_digest": inventory_digest,
            "normalized_text_digest": text_digest,
            "structural_event_digest": structural_digest,
            "counts": counts,
            "expected_outcome": outcome,
            "expected_rejection": rejection,
            "page_count": None,
            "page_count_reason": _EXPECTATION_REASON,
        },
        index,
    )


def build_private_docx_expectation(source_root: Path) -> dict[str, Any]:
    """Build the exact closed representative-domain expectation from reviewed NOMAGIC DOCX sources."""

    try:
        if not isinstance(source_root, Path):
            raise OSError("review source contract is invalid")
        reject_symlink_ancestors(source_root, include_self=True)
        root_before = os.lstat(source_root)
        if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
            raise OSError("review source root is invalid")
        names = sorted(
            name
            for name in os.listdir(source_root)
            if _SOURCE_NAME_RE.fullmatch(name) is not None and name not in {".docx", "..docx"}
        )
        if len(names) != len(_EXPECTATION_ITEM_IDS):
            raise OSError("review source contract is invalid")
        root_descriptor = os.open(source_root, _DIRECTORY_FLAGS)
        try:
            opened = os.fstat(root_descriptor)
            if (root_before.st_dev, root_before.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError("review source root changed")
            items: list[dict[str, Any]] = []
            for index, name in enumerate(names):
                before = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISREG(before.st_mode)
                    or stat.S_ISLNK(before.st_mode)
                    or before.st_nlink != 1
                    or before.st_size <= 0
                    or before.st_size > MAX_DOCX_BYTES
                ):
                    raise OSError("review source binding is invalid")
                descriptor = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                    dir_fd=root_descriptor,
                )
                try:
                    opened_file = os.fstat(descriptor)
                    if _stable_metadata(before) != _stable_metadata(opened_file):
                        raise OSError("review source changed")
                    source = _read_bounded(descriptor, MAX_DOCX_BYTES)
                    derived = _raw_restore_nomagic_docx(source)
                    items.append(_derive_expectation_item(index, source, derived))
                    after = os.fstat(descriptor)
                    public = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
                    if (
                        _stable_metadata(opened_file) != _stable_metadata(after)
                        or _stable_metadata(opened_file) != _stable_metadata(public)
                    ):
                        raise OSError("review source changed")
                finally:
                    os.close(descriptor)
        finally:
            os.close(root_descriptor)
        expectation = {
            "schema_version": _EXPECTATION_SCHEMA_VERSION,
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "items": items,
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        validate_docx_expectation(expectation)
        return json.loads(json.dumps(expectation))
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, PrivateDocxDomainError):
            raise
        raise PrivateDocxDomainError("private DOCX expectation is invalid") from None


def validate_docx_expectation(value: Mapping[str, Any]) -> DocxExpectationCorpus:
    """Validate one exact built-in expectation value and derive stable digests."""

    if type(value) is not dict or set(value) != _EXPECTATION_KEYS:
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    detached = _validate_builtin_json(value)
    if (
        detached["schema_version"] != _EXPECTATION_SCHEMA_VERSION
        or detached["corpus_id"] != DOCX_PRIVATE_CORPUS_ID
        or detached["ocr_enabled"] is not False
        or detached["network_accessed"] is not False
        or detached["provider_calls"] is not False
        or detached["promotion_authority"] is not False
        or detached["claims_authority_advance"] is not False
    ):
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    items = detached["items"]
    if type(items) is not list or len(items) != len(_EXPECTATION_ITEM_IDS):
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    documents = tuple(_validate_expectation_item(item, index) for index, item in enumerate(items))
    if (
        sum(item["expected_outcome"] == "accept" for item in documents)
        != _EXPECTED_ACCEPTED_COUNT
        or sum(item["expected_rejection"] == "active-content" for item in documents)
        != _EXPECTED_ACTIVE_REJECTION_COUNT
        or sum(item["expected_rejection"] == "invalid-package" for item in documents)
        != _EXPECTED_INVALID_REJECTION_COUNT
    ):
        raise PrivateDocxDomainError("private DOCX expectation is invalid")
    configuration_digest = canonical_digest(
        {
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "expected_rejections": sorted(EXPECTED_REJECTIONS),
            "page_count_reason": _EXPECTATION_REASON,
            "schema_version": _EXPECTATION_SCHEMA_VERSION,
            "transformation_id": DOCX_TRANSFORMATION_ID,
        }
    )
    corpus_digest = canonical_digest(
        {
            "schema_version": _EXPECTATION_SCHEMA_VERSION,
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "items": list(documents),
        }
    )
    return DocxExpectationCorpus(
        corpus_id=DOCX_PRIVATE_CORPUS_ID,
        corpus_digest=corpus_digest,
        configuration_digest=configuration_digest,
        documents=documents,
    )


def _qualification_source_set_digest(
    documents: Sequence[Mapping[str, Any]],
) -> str:
    return canonical_digest(
        [
            {
                "item_id": document["item_id"],
                "digest": document["source_digest"],
                "bytes": document["source_bytes"],
            }
            for document in documents
        ]
    )


def _qualification_derived_set_digest(
    documents: Sequence[Mapping[str, Any]],
) -> str:
    return canonical_digest(
        [
            {
                "item_id": document["item_id"],
                "digest": document["derived_digest"],
                "bytes": document["derived_bytes"],
            }
            for document in documents
        ]
    )


def _validate_qualification_input_manifest(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        if type(value) is not dict or set(value) != _QUALIFICATION_INPUT_KEYS:
            raise ValueError
        detached = _validate_builtin_json(value)
        if (
            detached["schema_version"] != _QUALIFICATION_INPUT_SCHEMA_VERSION
            or detached["corpus_id"] != DOCX_PRIVATE_CORPUS_ID
            or type(detached["expectation_digest"]) is not str
            or _SHA256_RE.fullmatch(detached["expectation_digest"]) is None
            or detached["transformation_id"] != DOCX_TRANSFORMATION_ID
            or any(
                detached[name] is not False
                for name in (
                    "ocr_enabled",
                    "network_accessed",
                    "provider_calls",
                    "promotion_authority",
                    "claims_authority_advance",
                )
            )
        ):
            raise ValueError
        documents = detached["documents"]
        if type(documents) is not list or len(documents) != len(_EXPECTATION_ITEM_IDS):
            raise ValueError
        for index, document in enumerate(documents):
            if (
                type(document) is not dict
                or set(document) != _QUALIFICATION_INPUT_DOCUMENT_KEYS
                or document["item_id"] != _EXPECTATION_ITEM_IDS[index]
                or document["fixture_name"] != _QUALIFICATION_FIXTURE_NAMES[index]
                or type(document["source_digest"]) is not str
                or _SHA256_RE.fullmatch(document["source_digest"]) is None
                or type(document["derived_digest"]) is not str
                or _SHA256_RE.fullmatch(document["derived_digest"]) is None
                or document["source_digest"] == document["derived_digest"]
                or type(document["source_bytes"]) is not int
                or not 0 < document["source_bytes"] <= MAX_DOCX_BYTES
                or type(document["derived_bytes"]) is not int
                or not 0 < document["derived_bytes"] <= MAX_DOCX_BYTES
                or document["transformation_id"] != DOCX_TRANSFORMATION_ID
            ):
                raise ValueError
        if (
            detached["source_set_digest"]
            != _qualification_source_set_digest(documents)
            or detached["derived_set_digest"]
            != _qualification_derived_set_digest(documents)
        ):
            raise ValueError
        return detached
    except (KeyError, TypeError, ValueError, PrivateDocxDomainError):
        raise PrivateDocxDomainError(
            "private DOCX qualification input manifest is invalid"
        ) from None


def _qualification_input_manifest(
    review: tuple[ReviewedDocxSource, ...],
    prepared: tuple[PreparedDocxSource, ...],
    expectation: DocxExpectationCorpus,
) -> dict[str, Any]:
    try:
        if (
            type(review) is not tuple
            or type(prepared) is not tuple
            or not isinstance(expectation, DocxExpectationCorpus)
            or len(review) != len(_EXPECTATION_ITEM_IDS)
            or len(prepared) != len(_EXPECTATION_ITEM_IDS)
            or len(expectation.documents) != len(_EXPECTATION_ITEM_IDS)
        ):
            raise ValueError
        documents: list[dict[str, Any]] = []
        for index, (reviewed, restored, expected) in enumerate(
            zip(review, prepared, expectation.documents, strict=True)
        ):
            item_id = _EXPECTATION_ITEM_IDS[index]
            if (
                not isinstance(reviewed, ReviewedDocxSource)
                or not isinstance(restored, PreparedDocxSource)
                or type(expected) is not dict
                or reviewed.item_id != item_id
                or restored.item_id != item_id
                or expected["item_id"] != item_id
                or reviewed.source_digest != restored.source_digest
                or reviewed.source_digest != expected["source_digest"]
                or reviewed.derived_digest != restored.derived_digest
                or reviewed.derived_digest != expected["derived_digest"]
                or reviewed.source_bytes != restored.source_bytes
                or reviewed.source_bytes != expected["source_bytes"]
                or reviewed.derived_bytes != restored.derived_bytes
                or reviewed.derived_bytes != expected["derived_bytes"]
                or reviewed.transformation_id != DOCX_TRANSFORMATION_ID
                or restored.transformation_id != DOCX_TRANSFORMATION_ID
                or expected["transformation_id"] != DOCX_TRANSFORMATION_ID
            ):
                raise ValueError
            source = b"\x00\x00\x00\x00" + restored.body[4:]
            if (
                len(source) != reviewed.source_bytes
                or _sha256(source) != reviewed.source_digest
                or len(restored.body) != reviewed.derived_bytes
                or _sha256(restored.body) != reviewed.derived_digest
            ):
                raise ValueError
            documents.append(
                {
                    "item_id": item_id,
                    "fixture_name": _QUALIFICATION_FIXTURE_NAMES[index],
                    "source_digest": reviewed.source_digest,
                    "derived_digest": reviewed.derived_digest,
                    "source_bytes": reviewed.source_bytes,
                    "derived_bytes": reviewed.derived_bytes,
                    "transformation_id": DOCX_TRANSFORMATION_ID,
                }
            )
        return _validate_qualification_input_manifest(
            {
                "schema_version": _QUALIFICATION_INPUT_SCHEMA_VERSION,
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "expectation_digest": expectation.corpus_digest,
                "source_set_digest": _qualification_source_set_digest(documents),
                "derived_set_digest": _qualification_derived_set_digest(documents),
                "transformation_id": DOCX_TRANSFORMATION_ID,
                "documents": documents,
                "ocr_enabled": False,
                "network_accessed": False,
                "provider_calls": False,
                "promotion_authority": False,
                "claims_authority_advance": False,
            }
        )
    except (KeyError, TypeError, ValueError, PrivateDocxDomainError):
        raise PrivateDocxDomainError(
            "private DOCX qualification input manifest is invalid"
        ) from None


def _qualification_input_intent(manifest: Mapping[str, Any]) -> dict[str, Any]:
    validated = _validate_qualification_input_manifest(manifest)
    return {
        "schema_version": "ao.lore.private-docx-qualification-input-intent.v0.1",
        "phase": "preparing",
        "final_name": _QUALIFICATION_INPUT_FINAL,
        "staging_name": _QUALIFICATION_INPUT_STAGE,
        "manifest": validated,
        "manifest_digest": canonical_digest(validated),
        "expectation_digest": validated["expectation_digest"],
        "source_set_digest": validated["source_set_digest"],
        "derived_set_digest": validated["derived_set_digest"],
        "transformation_id": DOCX_TRANSFORMATION_ID,
        "authority": False,
    }


def _qualification_input_bodies(
    manifest: Mapping[str, Any],
    expectation: Mapping[str, Any],
    prepared: Sequence[PreparedDocxSource],
) -> tuple[bytes, bytes, dict[str, bytes]]:
    expectation_body = (
        json.dumps(
            expectation,
            allow_nan=False,
            sort_keys=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(expectation_body) > 8 * 1024 * 1024:
        raise PrivateDocxDomainError(
            "private DOCX qualification input preparation failed"
        )
    manifest_body = _manifest_body(manifest, 256 * 1024)
    fixtures: dict[str, bytes] = {}
    for document, restored in zip(manifest["documents"], prepared, strict=True):
        body = b"\x00\x00\x00\x00" + restored.body[4:]
        if (
            document["item_id"] != restored.item_id
            or len(body) != document["source_bytes"]
            or _sha256(body) != document["source_digest"]
            or len(restored.body) != document["derived_bytes"]
            or _sha256(restored.body) != document["derived_digest"]
        ):
            raise PrivateDocxDomainError(
                "private DOCX qualification input preparation failed"
            )
        fixtures[document["fixture_name"]] = body
    return expectation_body, manifest_body, fixtures


def _validate_qualification_input_tree_at(
    directory_descriptor: int,
    *,
    expected_manifest: Mapping[str, Any] | None = None,
    expected_expectation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if set(os.listdir(directory_descriptor)) != {
        "expectation.json",
        "manifest.json",
        "corpus",
    }:
        raise PrivateDocxDomainError(
            "private DOCX qualification input state is invalid"
        )
    expectation_body = _read_file_at(
        directory_descriptor, "expectation.json", 8 * 1024 * 1024
    )
    manifest_body = _read_file_at(directory_descriptor, "manifest.json", 256 * 1024)
    try:
        expectation_value = parse_strict_json(
            expectation_body, "private DOCX expectation"
        )
        validated_expectation = validate_docx_expectation(expectation_value)
        manifest = _validate_qualification_input_manifest(
            parse_strict_json(
                manifest_body, "private DOCX qualification input manifest"
            )
        )
    except Exception:
        raise PrivateDocxDomainError(
            "private DOCX qualification input state is invalid"
        ) from None
    if (
        manifest["expectation_digest"] != validated_expectation.corpus_digest
        or (
            expected_manifest is not None
            and manifest != _validate_qualification_input_manifest(expected_manifest)
        )
        or (
            expected_expectation is not None
            and expectation_value != _validate_builtin_json(expected_expectation)
        )
    ):
        raise PrivateDocxDomainError(
            "private DOCX qualification input state is invalid"
        )
    corpus_descriptor = _open_relative_directory(
        directory_descriptor, ("corpus",), create=False
    )
    try:
        if sorted(os.listdir(corpus_descriptor)) != list(_QUALIFICATION_FIXTURE_NAMES):
            raise PrivateDocxDomainError(
                "private DOCX qualification input state is invalid"
            )
        for document in manifest["documents"]:
            body = _read_file_at(
                corpus_descriptor, document["fixture_name"], MAX_DOCX_BYTES
            )
            if (
                len(body) != document["source_bytes"]
                or _sha256(body) != document["source_digest"]
            ):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input state is invalid"
                )
            derived = _raw_restore_nomagic_docx(body)
            if (
                len(derived) != document["derived_bytes"]
                or _sha256(derived) != document["derived_digest"]
            ):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input state is invalid"
                )
    finally:
        os.close(corpus_descriptor)
    return manifest


def _qualification_input_intent_body(manifest: Mapping[str, Any]) -> bytes:
    return _manifest_body(_qualification_input_intent(manifest), 256 * 1024)


def _load_qualification_input_intent_at(
    private_descriptor: int,
) -> tuple[dict[str, Any], bytes]:
    body = _read_file_at(
        private_descriptor, _QUALIFICATION_INPUT_INTENT, 256 * 1024
    )
    try:
        intent = parse_strict_json(
            body, "private DOCX qualification input intent"
        )
        if set(intent) != {
            "schema_version",
            "phase",
            "final_name",
            "staging_name",
            "manifest",
            "manifest_digest",
            "expectation_digest",
            "source_set_digest",
            "derived_set_digest",
            "transformation_id",
            "authority",
        }:
            raise ValueError
        manifest = _validate_qualification_input_manifest(intent["manifest"])
        if intent != _qualification_input_intent(manifest):
            raise ValueError
    except Exception:
        raise PrivateDocxDomainError(
            "private DOCX qualification input state is invalid"
        ) from None
    return manifest, body


def _validate_qualification_input_intent_at(
    private_descriptor: int, manifest: Mapping[str, Any]
) -> bytes:
    loaded_manifest, actual = _load_qualification_input_intent_at(
        private_descriptor
    )
    expected = _qualification_input_intent_body(manifest)
    if actual != expected or loaded_manifest != _validate_qualification_input_manifest(
        manifest
    ):
        raise PrivateDocxDomainError(
            "private DOCX qualification input state is invalid"
        )
    return actual


def _reclaim_qualification_input_stage_at(
    private_descriptor: int,
    *,
    expectation_body: bytes,
    manifest_body: bytes,
    fixtures: Mapping[str, bytes],
) -> None:
    stage_descriptor = os.open(
        _QUALIFICATION_INPUT_STAGE,
        _DIRECTORY_FLAGS,
        dir_fd=private_descriptor,
    )
    corpus_descriptor: int | None = None
    try:
        allowed_stage = {
            "corpus",
            "expectation.json",
            "manifest.json",
            _rollback_name("expectation.json"),
            _rollback_name("manifest.json"),
        }
        if not set(os.listdir(stage_descriptor)).issubset(allowed_stage):
            raise PrivateDocxDomainError(
                "private DOCX qualification input state is invalid"
            )
        if _binding_exists_at(stage_descriptor, "corpus"):
            corpus_descriptor = _open_relative_directory(
                stage_descriptor, ("corpus",), create=False
            )
            rollback_fixtures = {
                _rollback_name(name): body for name, body in fixtures.items()
            }
            if not set(os.listdir(corpus_descriptor)).issubset(
                set(fixtures) | set(rollback_fixtures)
            ):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input state is invalid"
                )
            for name, body in fixtures.items():
                if _binding_exists_at(corpus_descriptor, name):
                    _unlink_verified_at(
                        corpus_descriptor, name, body, MAX_DOCX_BYTES
                    )
            for name, body in rollback_fixtures.items():
                if _binding_exists_at(corpus_descriptor, name):
                    _unlink_verified_at(
                        corpus_descriptor, name, body, MAX_DOCX_BYTES
                    )
            if os.listdir(corpus_descriptor):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input state is invalid"
                )
            os.close(corpus_descriptor)
            corpus_descriptor = None
            os.rmdir("corpus", dir_fd=stage_descriptor)
            os.fsync(stage_descriptor)
        for name, body in (
            ("expectation.json", expectation_body),
            ("manifest.json", manifest_body),
            (_rollback_name("expectation.json"), expectation_body),
            (_rollback_name("manifest.json"), manifest_body),
        ):
            if _binding_exists_at(stage_descriptor, name):
                _unlink_verified_at(
                    stage_descriptor,
                    name,
                    body,
                    max(256 * 1024, len(body)),
                )
        if os.listdir(stage_descriptor):
            raise PrivateDocxDomainError(
                "private DOCX qualification input state is invalid"
            )
    finally:
        if corpus_descriptor is not None:
            os.close(corpus_descriptor)
        os.close(stage_descriptor)
    os.rmdir(_QUALIFICATION_INPUT_STAGE, dir_fd=private_descriptor)
    os.fsync(private_descriptor)


def _recover_qualification_input_preparation_at(
    private_descriptor: int,
    *,
    manifest: Mapping[str, Any],
    expectation: Mapping[str, Any],
    expectation_body: bytes,
    manifest_body: bytes,
    fixtures: Mapping[str, bytes],
) -> dict[str, Any] | None:
    entries = set(os.listdir(private_descriptor))
    intent_present = _QUALIFICATION_INPUT_INTENT in entries
    stage_present = _QUALIFICATION_INPUT_STAGE in entries
    final_present = _QUALIFICATION_INPUT_FINAL in entries
    if not intent_present:
        return None
    allowed = {_LOCK_NAME, _QUALIFICATION_INPUT_INTENT}
    if stage_present:
        allowed.add(_QUALIFICATION_INPUT_STAGE)
    if final_present:
        allowed.add(_QUALIFICATION_INPUT_FINAL)
    if entries != allowed or (stage_present and final_present):
        raise PrivateDocxDomainError(
            "private DOCX qualification input state is invalid"
        )
    intent_body = _validate_qualification_input_intent_at(
        private_descriptor, manifest
    )
    if final_present:
        final_descriptor = _open_relative_directory(
            private_descriptor, (_QUALIFICATION_INPUT_FINAL,), create=False
        )
        try:
            result = _validate_qualification_input_tree_at(
                final_descriptor,
                expected_manifest=manifest,
                expected_expectation=expectation,
            )
        finally:
            os.close(final_descriptor)
        _unlink_verified_at(
            private_descriptor,
            _QUALIFICATION_INPUT_INTENT,
            intent_body,
            256 * 1024,
        )
        return result
    if stage_present:
        _reclaim_qualification_input_stage_at(
            private_descriptor,
            expectation_body=expectation_body,
            manifest_body=manifest_body,
            fixtures=fixtures,
        )
    _unlink_verified_at(
        private_descriptor,
        _QUALIFICATION_INPUT_INTENT,
        intent_body,
        256 * 1024,
    )
    return None


def prepare_private_docx_qualification_inputs(
    review: tuple[ReviewedDocxSource, ...],
    expectation: Mapping[str, Any],
    *,
    source_root: Path,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Atomically publish the exact reviewed 100-document qualification inputs."""

    root_descriptor: int | None = None
    private_descriptor: int | None = None
    lock_descriptor: int | None = None
    stage_descriptor: int | None = None
    corpus_descriptor: int | None = None
    final_descriptor: int | None = None
    publication_started = False
    try:
        validated_expectation = validate_docx_expectation(expectation)
        prepared = load_reviewed_docx_sources(source_root, review)
        derived_expectation = tuple(
            _derive_expectation_item(
                index,
                b"\x00\x00\x00\x00" + restored.body[4:],
                restored.body,
            )
            for index, restored in enumerate(prepared)
        )
        if derived_expectation != validated_expectation.documents:
            raise PrivateDocxDomainError(
                "private DOCX qualification input preparation failed"
            )
        manifest = _qualification_input_manifest(
            review, prepared, validated_expectation
        )
        expectation_body, manifest_body, fixtures = _qualification_input_bodies(
            manifest, expectation, prepared
        )
        intent_body = _qualification_input_intent_body(manifest)

        reject_symlink_ancestors(
            Path(runtime_root) if runtime_root is not None else Path("."),
            include_self=False,
        )
        _, root_descriptor = _open_runtime_root(runtime_root)
        private_descriptor = _open_relative_directory(
            root_descriptor, ("private-docx",), create=True
        )
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=True)
        recovered = _recover_qualification_input_preparation_at(
            private_descriptor,
            manifest=manifest,
            expectation=expectation,
            expectation_body=expectation_body,
            manifest_body=manifest_body,
            fixtures=fixtures,
        )
        if recovered is not None:
            return recovered
        entries = set(os.listdir(private_descriptor))
        if entries in (
            {_LOCK_NAME, _QUALIFICATION_INPUT_FINAL},
            {_LOCK_NAME, _QUALIFICATION_INPUT_FINAL, "qualification.json"},
        ):
            final_descriptor = _open_relative_directory(
                private_descriptor, (_QUALIFICATION_INPUT_FINAL,), create=False
            )
            result = _validate_qualification_input_tree_at(
                final_descriptor,
                expected_manifest=manifest,
                expected_expectation=expectation,
            )
            if "qualification.json" in entries:
                _load_qualification_result_at(
                    private_descriptor,
                    expected_expectation_digest=result["expectation_digest"],
                )
            return result
        if entries != {_LOCK_NAME}:
            raise OSError("private DOCX qualification input state is invalid")

        _persist_bytes_at(
            private_descriptor, _QUALIFICATION_INPUT_INTENT, intent_body
        )
        os.mkdir(_QUALIFICATION_INPUT_STAGE, 0o700, dir_fd=private_descriptor)
        stage_descriptor = os.open(
            _QUALIFICATION_INPUT_STAGE, _DIRECTORY_FLAGS, dir_fd=private_descriptor
        )
        os.mkdir("corpus", 0o700, dir_fd=stage_descriptor)
        corpus_descriptor = os.open("corpus", _DIRECTORY_FLAGS, dir_fd=stage_descriptor)
        for name, body in fixtures.items():
            _persist_bytes_at(corpus_descriptor, name, body)
        os.fsync(corpus_descriptor)
        _persist_bytes_at(stage_descriptor, "expectation.json", expectation_body)
        _persist_bytes_at(stage_descriptor, "manifest.json", manifest_body)
        os.fsync(stage_descriptor)
        _validate_qualification_input_tree_at(
            stage_descriptor,
            expected_manifest=manifest,
            expected_expectation=expectation,
        )
        os.close(corpus_descriptor)
        corpus_descriptor = None
        os.close(stage_descriptor)
        stage_descriptor = None
        publication_started = True
        _rename_noreplace_at(
            private_descriptor,
            _QUALIFICATION_INPUT_STAGE,
            private_descriptor,
            _QUALIFICATION_INPUT_FINAL,
        )
        os.fsync(private_descriptor)
        final_descriptor = _open_relative_directory(
            private_descriptor, (_QUALIFICATION_INPUT_FINAL,), create=False
        )
        result = _validate_qualification_input_tree_at(
            final_descriptor,
            expected_manifest=manifest,
            expected_expectation=expectation,
        )
        _unlink_verified_at(
            private_descriptor,
            _QUALIFICATION_INPUT_INTENT,
            intent_body,
            256 * 1024,
        )
        return result
    except BaseException as exc:
        if not isinstance(exc, Exception):
            if publication_started and private_descriptor is not None:
                try:
                    os.fsync(private_descriptor)
                except OSError:
                    pass
            raise
        if isinstance(exc, PrivateDocxDomainError):
            raise
        raise PrivateDocxDomainError(
            "private DOCX qualification input preparation failed"
        ) from None
    finally:
        for descriptor in (
            final_descriptor,
            corpus_descriptor,
            stage_descriptor,
            lock_descriptor,
            private_descriptor,
            root_descriptor,
        ):
            if descriptor is not None:
                os.close(descriptor)


def load_reviewed_docx_sources(
    source_root: Path,
    review: tuple[ReviewedDocxSource, ...],
) -> tuple[PreparedDocxSource, ...]:
    """Hold descriptor-bound reviewed bytes after the exact raw header restoration."""

    root_descriptor: int | None = None
    try:
        if not isinstance(source_root, Path) or type(review) is not tuple or not review:
            raise OSError("review source contract is invalid")
        reject_symlink_ancestors(source_root, include_self=True)
        root_before = os.lstat(source_root)
        if stat.S_ISLNK(root_before.st_mode) or not stat.S_ISDIR(root_before.st_mode):
            raise OSError("review source root is invalid")
        seen_names: set[str] = set()
        seen_casefold: set[str] = set()
        seen_items: set[str] = set()
        for reviewed in review:
            _validate_reviewed_source(reviewed)
            folded = reviewed.source_name.casefold()
            if (
                reviewed.item_id in seen_items
                or reviewed.source_name in seen_names
                or folded in seen_casefold
            ):
                raise OSError("review binding is invalid")
            seen_items.add(reviewed.item_id)
            seen_names.add(reviewed.source_name)
            seen_casefold.add(folded)

        root_descriptor = os.open(source_root, _DIRECTORY_FLAGS)
        root_opened = os.fstat(root_descriptor)
        if (root_before.st_dev, root_before.st_ino) != (root_opened.st_dev, root_opened.st_ino):
            raise OSError("review source root changed")

        prepared: list[PreparedDocxSource] = []
        for reviewed in review:
            before = os.stat(reviewed.source_name, dir_fd=root_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or before.st_nlink != 1
                or before.st_size != reviewed.source_bytes
                or before.st_size <= 0
                or before.st_size > MAX_DOCX_BYTES
            ):
                raise OSError("review source binding is invalid")
            descriptor = os.open(
                reviewed.source_name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=root_descriptor,
            )
            try:
                opened = os.fstat(descriptor)
                if _stable_metadata(before) != _stable_metadata(opened):
                    raise OSError("review source changed")
                source = _read_bounded(descriptor, MAX_DOCX_BYTES)
                if len(source) != reviewed.source_bytes or _sha256(source) != reviewed.source_digest:
                    raise OSError("review source digest drifted")
                derived = _raw_restore_nomagic_docx(source)
                if len(derived) != reviewed.derived_bytes or _sha256(derived) != reviewed.derived_digest:
                    raise OSError("review source digest drifted")
                after = os.fstat(descriptor)
                public = os.stat(reviewed.source_name, dir_fd=root_descriptor, follow_symlinks=False)
                if (
                    _stable_metadata(opened) != _stable_metadata(after)
                    or _stable_metadata(opened) != _stable_metadata(public)
                ):
                    raise OSError("review source changed")
            finally:
                os.close(descriptor)
            prepared.append(
                PreparedDocxSource(
                    item_id=reviewed.item_id,
                    source_digest=reviewed.source_digest,
                    derived_digest=reviewed.derived_digest,
                    source_bytes=reviewed.source_bytes,
                    derived_bytes=reviewed.derived_bytes,
                    transformation_id=reviewed.transformation_id,
                    body=derived,
                )
            )
        root_after = os.fstat(root_descriptor)
        root_public = os.lstat(source_root)
        if (
            (root_opened.st_dev, root_opened.st_ino) != (root_after.st_dev, root_after.st_ino)
            or (root_opened.st_dev, root_opened.st_ino) != (root_public.st_dev, root_public.st_ino)
        ):
            raise OSError("review source root changed")
        return tuple(prepared)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        raise PrivateDocxDomainError("private DOCX source qualification failed") from None
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _corpus_manifest(
    reviewed_sources: Sequence[ReviewedDocxSource],
    review_value: Mapping[str, Any],
    *,
    expectation_digest: str | None = None,
) -> dict[str, Any]:
    review_digest = canonical_digest(review_value)
    if expectation_digest is None:
        expectation_digest = canonical_digest(
            {
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "items": [
                    {
                        "item_id": reviewed.item_id,
                        "original_digest": reviewed.source_digest,
                        "derived_digest": reviewed.derived_digest,
                        "transformation_id": reviewed.transformation_id,
                    }
                    for reviewed in reviewed_sources
                ],
            }
        )
    return {
        "schema_version": _DOCX_CORPUS_SCHEMA_VERSION,
        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
        "review_digest": review_digest,
        "expectation_digest": expectation_digest,
        "documents": [
            {
                "item_id": reviewed.item_id,
                "source_name": reviewed.source_name,
                "derived_name": f"{reviewed.item_id}.docx",
                "source_digest": reviewed.source_digest,
                "derived_digest": reviewed.derived_digest,
                "source_bytes": reviewed.source_bytes,
                "derived_bytes": reviewed.derived_bytes,
                "transformation_id": reviewed.transformation_id,
            }
            for reviewed in reviewed_sources
        ],
        "ocr_enabled": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


def _load_qualification_result_at(
    private_descriptor: int,
    *,
    expected_expectation_digest: str | None,
) -> dict[str, Any]:
    body = _read_file_at(
        private_descriptor, "qualification.json", 8 * 1024 * 1024
    )
    try:
        value = parse_strict_json(body, "private DOCX qualification")
        from .docx_ooxml import (
            DocxLimits,
            docx_configuration_digest,
            validate_docx_benchmark,
        )

        validated = dict(
            validate_docx_benchmark(
                value,
                expected_configuration_digest=docx_configuration_digest(
                    DocxLimits()
                ),
                expected_corpus_digest=expected_expectation_digest,
            )
        )
        if validated["decision"] != "hold":
            raise ValueError
        return validated
    except Exception:
        raise PrivateDocxDomainError(
            "private DOCX qualification is invalid"
        ) from None


def _qualification_context_digest_at(
    private_descriptor: int,
    *,
    fallback_digest: str | None,
) -> str:
    entries = set(os.listdir(private_descriptor))
    expectation_digest = fallback_digest
    if _QUALIFICATION_INPUT_FINAL in entries:
        final_descriptor = _open_relative_directory(
            private_descriptor, (_QUALIFICATION_INPUT_FINAL,), create=False
        )
        try:
            inputs = _validate_qualification_input_tree_at(final_descriptor)
        finally:
            os.close(final_descriptor)
        expectation_digest = inputs["expectation_digest"]
    if expectation_digest is None:
        raise PrivateDocxDomainError("private DOCX qualification is invalid")
    if "qualification.json" in entries:
        _load_qualification_result_at(
            private_descriptor,
            expected_expectation_digest=expectation_digest,
        )
    return expectation_digest


def _validate_corpus_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _CORPUS_KEYS:
        raise PrivateDocxDomainError("private DOCX corpus manifest is invalid")
    detached = _validate_builtin_json(value)
    if (
        detached["schema_version"] != _DOCX_CORPUS_SCHEMA_VERSION
        or detached["corpus_id"] != DOCX_PRIVATE_CORPUS_ID
        or type(detached["review_digest"]) is not str
        or _SHA256_RE.fullmatch(detached["review_digest"]) is None
        or type(detached["expectation_digest"]) is not str
        or _SHA256_RE.fullmatch(detached["expectation_digest"]) is None
        or detached["ocr_enabled"] is not False
        or detached["network_accessed"] is not False
        or detached["provider_calls"] is not False
        or detached["promotion_authority"] is not False
        or detached["claims_authority_advance"] is not False
    ):
        raise PrivateDocxDomainError("private DOCX corpus manifest is invalid")
    documents = detached["documents"]
    if type(documents) is not list or len(documents) != len(DOCX_UAT_ITEM_IDS):
        raise PrivateDocxDomainError("private DOCX corpus manifest is invalid")
    seen_derived: set[str] = set()
    for index, document in enumerate(documents):
        if type(document) is not dict or set(document) != _CORPUS_DOCUMENT_KEYS:
            raise PrivateDocxDomainError("private DOCX corpus manifest is invalid")
        if (
            document["item_id"] != DOCX_UAT_ITEM_IDS[index]
            or type(document["source_name"]) is not str
            or _SOURCE_NAME_RE.fullmatch(document["source_name"]) is None
            or type(document["derived_name"]) is not str
            or _SOURCE_NAME_RE.fullmatch(document["derived_name"]) is None
            or document["derived_name"] in seen_derived
            or type(document["source_digest"]) is not str
            or _SHA256_RE.fullmatch(document["source_digest"]) is None
            or type(document["derived_digest"]) is not str
            or _SHA256_RE.fullmatch(document["derived_digest"]) is None
            or document["source_digest"] == document["derived_digest"]
            or type(document["source_bytes"]) is not int
            or type(document["derived_bytes"]) is not int
            or document["source_bytes"] <= 0
            or document["derived_bytes"] <= 0
            or document["transformation_id"] != DOCX_TRANSFORMATION_ID
        ):
            raise PrivateDocxDomainError("private DOCX corpus manifest is invalid")
        seen_derived.add(document["derived_name"])
    return detached


def _load_private_docx_manifest_at(corpus_descriptor: int) -> dict[str, Any]:
    from .docx_ooxml import validate_docx_package

    manifest = _validate_corpus_manifest(
        json.loads(_read_file_at(corpus_descriptor, "manifest.json", 64 * 1024).decode("utf-8"))
    )
    expected_names = {"manifest.json"} | {
        document["derived_name"] for document in manifest["documents"]
    }
    if set(os.listdir(corpus_descriptor)) != expected_names:
        raise PrivateDocxDomainError("private DOCX corpus manifest is invalid")
    for document in manifest["documents"]:
        body = _read_file_at(corpus_descriptor, document["derived_name"], MAX_DOCX_BYTES)
        if _sha256(body) != document["derived_digest"] or len(body) != document["derived_bytes"]:
            raise PrivateDocxDomainError("private DOCX corpus manifest is invalid")
        validate_docx_package(body)
    return manifest


def _recover_corpus_stage_at(
    private_descriptor: int,
    manifest: Mapping[str, Any],
    prepared_sources: Sequence[PreparedDocxSource],
) -> None:
    stage_name = ".corpus-staging"
    try:
        stage_descriptor = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=private_descriptor)
    except FileNotFoundError:
        return
    try:
        expected_files = {f"{prepared.item_id}.docx": prepared.body for prepared in prepared_sources}
        rollback_names = {
            _rollback_name(name): (name, body) for name, body in expected_files.items()
        }
        rollback_names[_rollback_name("manifest.json")] = ("manifest.json", _manifest_body(manifest))
        expected_entries = set(expected_files) | {"manifest.json"} | set(rollback_names)
        actual_entries = set(os.listdir(stage_descriptor))
        if not actual_entries.issubset(expected_entries):
            raise OSError("private DOCX preparation staging drifted")
        for name, body in expected_files.items():
            if _binding_exists_at(stage_descriptor, name):
                _unlink_verified_at(stage_descriptor, name, body, MAX_DOCX_BYTES)
        if _binding_exists_at(stage_descriptor, "manifest.json"):
            _unlink_verified_at(stage_descriptor, "manifest.json", _manifest_body(manifest), 64 * 1024)
        for rollback, (_, body) in rollback_names.items():
            if _binding_exists_at(stage_descriptor, rollback):
                _unlink_verified_at(stage_descriptor, rollback, body, max(64 * 1024, len(body)))
        if os.listdir(stage_descriptor):
            raise OSError("private DOCX preparation staging remained")
    finally:
        os.close(stage_descriptor)
    os.rmdir(stage_name, dir_fd=private_descriptor)
    os.fsync(private_descriptor)


def prepare_private_docx_corpus(
    review: Mapping[str, Any], *, source_root: Path, runtime_root: Path | None = None
) -> dict[str, Any]:
    """Persist one exact derived DOCX corpus beneath the private runtime root."""

    root_descriptor: int | None = None
    private_descriptor: int | None = None
    lock_descriptor: int | None = None
    stage_descriptor: int | None = None
    try:
        validated_review = _validated_review(review)
        reviewed_sources, prepared_sources = _derive_reviewed_sources(source_root, validated_review)
        fallback_manifest = _validate_corpus_manifest(
            _corpus_manifest(reviewed_sources, review)
        )
        reject_symlink_ancestors(Path(runtime_root) if runtime_root is not None else Path("."), include_self=False)
        _, root_descriptor = _open_runtime_root(runtime_root)
        private_descriptor = _open_relative_directory(root_descriptor, ("private-docx",), create=True)
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=True)
        entries = set(os.listdir(private_descriptor))
        qualification_entries = {
            name
            for name in (_QUALIFICATION_INPUT_FINAL, "qualification.json")
            if name in entries
        }
        base_entries = {_LOCK_NAME} | qualification_entries
        if entries not in (
            base_entries,
            base_entries | {"corpus"},
            base_entries | {".corpus-staging"},
        ):
            raise PrivateDocxDomainError("private DOCX corpus preparation failed")
        expectation_digest = _qualification_context_digest_at(
            private_descriptor,
            fallback_digest=fallback_manifest["expectation_digest"],
        )
        manifest = _validate_corpus_manifest(
            _corpus_manifest(
                reviewed_sources,
                review,
                expectation_digest=expectation_digest,
            )
        )
        _recover_corpus_stage_at(private_descriptor, manifest, prepared_sources)
        if set(os.listdir(private_descriptor)) not in (
            base_entries,
            base_entries | {"corpus"},
        ):
            raise PrivateDocxDomainError("private DOCX corpus preparation failed")
        if _binding_exists_at(private_descriptor, "corpus"):
            corpus_descriptor = os.open("corpus", _DIRECTORY_FLAGS, dir_fd=private_descriptor)
            try:
                existing = _load_private_docx_manifest_at(corpus_descriptor)
            finally:
                os.close(corpus_descriptor)
            if existing != manifest:
                raise PrivateDocxDomainError("private DOCX corpus preparation failed")
            return existing
        stage_name = ".corpus-staging"
        if _binding_exists_at(private_descriptor, stage_name):
            raise PrivateDocxDomainError("private DOCX corpus preparation failed")
        os.mkdir(stage_name, 0o700, dir_fd=private_descriptor)
        stage_descriptor = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=private_descriptor)
        for prepared in prepared_sources:
            _persist_bytes_at(stage_descriptor, f"{prepared.item_id}.docx", prepared.body)
        _persist_bytes_at(stage_descriptor, "manifest.json", _manifest_body(manifest))
        _rename_noreplace_at(private_descriptor, stage_name, private_descriptor, "corpus")
        os.fsync(private_descriptor)
        corpus_descriptor = os.open("corpus", _DIRECTORY_FLAGS, dir_fd=private_descriptor)
        try:
            return _load_private_docx_manifest_at(corpus_descriptor)
        finally:
            os.close(corpus_descriptor)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, PrivateDocxDomainError):
            raise
        raise PrivateDocxDomainError("private DOCX corpus preparation failed") from None
    finally:
        for descriptor in (stage_descriptor, lock_descriptor, private_descriptor, root_descriptor):
            if descriptor is not None:
                os.close(descriptor)


def load_private_docx_manifest(*, runtime_root: Path | None = None) -> dict[str, Any]:
    """Load and revalidate the persisted private DOCX derived corpus manifest."""

    root_descriptor: int | None = None
    private_descriptor: int | None = None
    lock_descriptor: int | None = None
    corpus_descriptor: int | None = None
    try:
        reject_symlink_ancestors(Path(runtime_root) if runtime_root is not None else Path("."), include_self=False)
        _, root_descriptor = _open_runtime_root(runtime_root)
        private_descriptor = _open_relative_directory(root_descriptor, ("private-docx",), create=False)
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=False)
        corpus_descriptor = _open_relative_directory(private_descriptor, ("corpus",), create=False)
        return _load_private_docx_manifest_at(corpus_descriptor)
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        raise PrivateDocxDomainError("private DOCX corpus manifest could not be loaded") from None
    finally:
        for descriptor in (corpus_descriptor, lock_descriptor, private_descriptor, root_descriptor):
            if descriptor is not None:
                os.close(descriptor)


def _cleanup_qualification_preparation_at(private_descriptor: int) -> None:
    manifest, intent_body = _load_qualification_input_intent_at(
        private_descriptor
    )
    entries = set(os.listdir(private_descriptor))
    has_stage = _QUALIFICATION_INPUT_STAGE in entries
    has_final = _QUALIFICATION_INPUT_FINAL in entries
    allowed = {_LOCK_NAME, _QUALIFICATION_INPUT_INTENT}
    if has_stage:
        allowed.add(_QUALIFICATION_INPUT_STAGE)
    if has_final:
        allowed.add(_QUALIFICATION_INPUT_FINAL)
    if entries != allowed or (has_stage and has_final):
        raise PrivateDocxDomainError(
            "private DOCX qualification input cleanup failed"
        )
    if has_stage:
        stage_descriptor = os.open(
            _QUALIFICATION_INPUT_STAGE,
            _DIRECTORY_FLAGS,
            dir_fd=private_descriptor,
        )
        corpus_descriptor: int | None = None
        try:
            stage_entries = set(os.listdir(stage_descriptor))
            allowed_stage = {
                "corpus",
                "expectation.json",
                "manifest.json",
                _rollback_name("expectation.json"),
                _rollback_name("manifest.json"),
            }
            if not stage_entries.issubset(allowed_stage):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                )
            if "corpus" in stage_entries:
                corpus_descriptor = _open_relative_directory(
                    stage_descriptor, ("corpus",), create=False
                )
                by_name = {
                    document["fixture_name"]: document
                    for document in manifest["documents"]
                }
                rollback_by_name = {
                    _rollback_name(name): document
                    for name, document in by_name.items()
                }
                remaining = set(os.listdir(corpus_descriptor))
                if not remaining.issubset(
                    set(by_name) | set(rollback_by_name)
                ):
                    raise PrivateDocxDomainError(
                        "private DOCX qualification input cleanup failed"
                    )
                for name in sorted(remaining):
                    document = by_name.get(name) or rollback_by_name[name]
                    body = _read_file_at(corpus_descriptor, name, MAX_DOCX_BYTES)
                    if (
                        len(body) != document["source_bytes"]
                        or _sha256(body) != document["source_digest"]
                    ):
                        raise PrivateDocxDomainError(
                            "private DOCX qualification input cleanup failed"
                        )
                    derived = _raw_restore_nomagic_docx(body)
                    if (
                        len(derived) != document["derived_bytes"]
                        or _sha256(derived) != document["derived_digest"]
                    ):
                        raise PrivateDocxDomainError(
                            "private DOCX qualification input cleanup failed"
                        )
                    _unlink_verified_at(
                        corpus_descriptor, name, body, MAX_DOCX_BYTES
                    )
                os.close(corpus_descriptor)
                corpus_descriptor = None
                os.rmdir("corpus", dir_fd=stage_descriptor)
                os.fsync(stage_descriptor)
            for name in ("expectation.json", _rollback_name("expectation.json")):
                if not _binding_exists_at(stage_descriptor, name):
                    continue
                body = _read_file_at(
                    stage_descriptor, name, 8 * 1024 * 1024
                )
                try:
                    expectation = validate_docx_expectation(
                        parse_strict_json(body, "private DOCX expectation")
                    )
                except Exception:
                    raise PrivateDocxDomainError(
                        "private DOCX qualification input cleanup failed"
                    ) from None
                if expectation.corpus_digest != manifest["expectation_digest"]:
                    raise PrivateDocxDomainError(
                        "private DOCX qualification input cleanup failed"
                    )
                _unlink_verified_at(
                    stage_descriptor, name, body, 8 * 1024 * 1024
                )
            expected_manifest_body = _manifest_body(manifest, 256 * 1024)
            for name in ("manifest.json", _rollback_name("manifest.json")):
                if _binding_exists_at(stage_descriptor, name):
                    _unlink_verified_at(
                        stage_descriptor,
                        name,
                        expected_manifest_body,
                        256 * 1024,
                    )
            if os.listdir(stage_descriptor):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                )
        finally:
            if corpus_descriptor is not None:
                os.close(corpus_descriptor)
            os.close(stage_descriptor)
        os.rmdir(_QUALIFICATION_INPUT_STAGE, dir_fd=private_descriptor)
        os.fsync(private_descriptor)
    if has_final:
        final_descriptor = _open_relative_directory(
            private_descriptor, (_QUALIFICATION_INPUT_FINAL,), create=False
        )
        try:
            _validate_qualification_input_tree_at(
                final_descriptor, expected_manifest=manifest
            )
        finally:
            os.close(final_descriptor)
    _unlink_verified_at(
        private_descriptor,
        _QUALIFICATION_INPUT_INTENT,
        intent_body,
        256 * 1024,
    )


def _reclaim_qualification_input_tree_at(private_descriptor: int) -> None:
    final_descriptor = os.open(
        _QUALIFICATION_INPUT_RECLAIM,
        _DIRECTORY_FLAGS,
        dir_fd=private_descriptor,
    )
    corpus_descriptor: int | None = None
    try:
        entries = set(os.listdir(final_descriptor))
        if not entries.issubset({"expectation.json", "manifest.json", "corpus"}):
            raise PrivateDocxDomainError(
                "private DOCX qualification input cleanup failed"
            )
        if not entries:
            manifest = None
            manifest_body = None
        else:
            if "manifest.json" not in entries:
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                )
            manifest_body = _read_file_at(
                final_descriptor, "manifest.json", 256 * 1024
            )
            try:
                manifest = _validate_qualification_input_manifest(
                    parse_strict_json(
                        manifest_body,
                        "private DOCX qualification input manifest",
                    )
                )
            except Exception:
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                ) from None
        if manifest is not None and "expectation.json" in entries:
            expectation_body = _read_file_at(
                final_descriptor, "expectation.json", 8 * 1024 * 1024
            )
            try:
                expectation = validate_docx_expectation(
                    parse_strict_json(
                        expectation_body, "private DOCX expectation"
                    )
                )
            except Exception:
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                ) from None
            if expectation.corpus_digest != manifest["expectation_digest"]:
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                )
        if manifest is not None and _binding_exists_at(
            private_descriptor, "qualification.json"
        ):
            _load_qualification_result_at(
                private_descriptor,
                expected_expectation_digest=manifest["expectation_digest"],
            )
        if manifest is not None and "corpus" in entries:
            corpus_descriptor = _open_relative_directory(
                final_descriptor, ("corpus",), create=False
            )
            remaining = set(os.listdir(corpus_descriptor))
            if not remaining.issubset(set(_QUALIFICATION_FIXTURE_NAMES)):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                )
            by_name = {
                document["fixture_name"]: document
                for document in manifest["documents"]
            }
            for name in _QUALIFICATION_FIXTURE_NAMES:
                if name not in remaining:
                    continue
                document = by_name[name]
                body = _read_file_at(corpus_descriptor, name, MAX_DOCX_BYTES)
                if (
                    len(body) != document["source_bytes"]
                    or _sha256(body) != document["source_digest"]
                ):
                    raise PrivateDocxDomainError(
                        "private DOCX qualification input cleanup failed"
                    )
                derived = _raw_restore_nomagic_docx(body)
                if (
                    len(derived) != document["derived_bytes"]
                    or _sha256(derived) != document["derived_digest"]
                ):
                    raise PrivateDocxDomainError(
                        "private DOCX qualification input cleanup failed"
                    )
                _unlink_verified_at(
                    corpus_descriptor, name, body, MAX_DOCX_BYTES
                )
            if os.listdir(corpus_descriptor):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                )
            os.close(corpus_descriptor)
            corpus_descriptor = None
            os.rmdir("corpus", dir_fd=final_descriptor)
            os.fsync(final_descriptor)
        if manifest is not None and _binding_exists_at(
            final_descriptor, "expectation.json"
        ):
            expectation_body = _read_file_at(
                final_descriptor, "expectation.json", 8 * 1024 * 1024
            )
            _unlink_verified_at(
                final_descriptor,
                "expectation.json",
                expectation_body,
                8 * 1024 * 1024,
            )
        if manifest_body is not None and _binding_exists_at(
            final_descriptor, "manifest.json"
        ):
            _unlink_verified_at(
                final_descriptor,
                "manifest.json",
                manifest_body,
                256 * 1024,
            )
        if os.listdir(final_descriptor):
            raise PrivateDocxDomainError(
                "private DOCX qualification input cleanup failed"
            )
    finally:
        if corpus_descriptor is not None:
            os.close(corpus_descriptor)
        os.close(final_descriptor)
    os.rmdir(_QUALIFICATION_INPUT_RECLAIM, dir_fd=private_descriptor)
    os.fsync(private_descriptor)


def _cleanup_qualification_input_tree_at(private_descriptor: int) -> None:
    final_descriptor = _open_relative_directory(
        private_descriptor, (_QUALIFICATION_INPUT_FINAL,), create=False
    )
    try:
        _validate_qualification_input_tree_at(final_descriptor)
    finally:
        os.close(final_descriptor)
    publication_started = False
    try:
        publication_started = True
        _rename_noreplace_at(
            private_descriptor,
            _QUALIFICATION_INPUT_FINAL,
            private_descriptor,
            _QUALIFICATION_INPUT_RECLAIM,
        )
        os.fsync(private_descriptor)
    except BaseException:
        if publication_started:
            try:
                os.fsync(private_descriptor)
            except OSError:
                pass
        raise
    _reclaim_qualification_input_tree_at(private_descriptor)


def _cleanup_legacy_qualification_expectation_at(private_descriptor: int) -> None:
    body = _read_file_at(private_descriptor, "expectation.json", 8 * 1024 * 1024)
    try:
        value = parse_strict_json(body, "private DOCX expectation")
        validate_docx_expectation(value)
    except Exception:
        raise PrivateDocxDomainError(
            "private DOCX qualification input cleanup failed"
        ) from None
    _unlink_verified_at(
        private_descriptor,
        "expectation.json",
        body,
        8 * 1024 * 1024,
    )


def cleanup_private_docx_corpus(*, runtime_root: Path | None = None) -> dict[str, Any]:
    """Remove the exact runtime-owned private DOCX corpus when present."""

    root_descriptor: int | None = None
    private_descriptor: int | None = None
    lock_descriptor: int | None = None
    corpus_descriptor: int | None = None
    try:
        reject_symlink_ancestors(Path(runtime_root) if runtime_root is not None else Path("."), include_self=False)
        _, root_descriptor = _open_runtime_root(runtime_root)
        try:
            private_descriptor = _open_relative_directory(root_descriptor, ("private-docx",), create=False)
        except FileNotFoundError:
            return {"corpus_id": DOCX_PRIVATE_CORPUS_ID, "removed": False, "verified_documents": 0}
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=True)
        entries = set(os.listdir(private_descriptor))
        if _QUALIFICATION_INPUT_INTENT in entries:
            _cleanup_qualification_preparation_at(private_descriptor)
            entries = set(os.listdir(private_descriptor))
        if entries == {_LOCK_NAME, "expectation.json"}:
            _cleanup_legacy_qualification_expectation_at(private_descriptor)
            entries = set(os.listdir(private_descriptor))
        known_coexistence = {
            _LOCK_NAME,
            _QUALIFICATION_INPUT_FINAL,
            "qualification.json",
            "corpus",
        }
        if _QUALIFICATION_INPUT_FINAL in entries:
            if (
                not {_LOCK_NAME, _QUALIFICATION_INPUT_FINAL}.issubset(entries)
                or not entries.issubset(known_coexistence)
            ):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                )
            _qualification_context_digest_at(
                private_descriptor,
                fallback_digest=None,
            )
            _cleanup_qualification_input_tree_at(private_descriptor)
            entries = set(os.listdir(private_descriptor))
        if _QUALIFICATION_INPUT_RECLAIM in entries:
            known_reclaim_coexistence = {
                _LOCK_NAME,
                _QUALIFICATION_INPUT_RECLAIM,
                "qualification.json",
                "corpus",
            }
            if (
                not {_LOCK_NAME, _QUALIFICATION_INPUT_RECLAIM}.issubset(entries)
                or not entries.issubset(known_reclaim_coexistence)
            ):
                raise PrivateDocxDomainError(
                    "private DOCX qualification input cleanup failed"
                )
            if "qualification.json" in entries:
                _load_qualification_result_at(
                    private_descriptor,
                    expected_expectation_digest=None,
                )
            _reclaim_qualification_input_tree_at(private_descriptor)
            entries = set(os.listdir(private_descriptor))
        qualification_entries = (
            {"qualification.json"} if "qualification.json" in entries else set()
        )
        if qualification_entries:
            _load_qualification_result_at(
                private_descriptor,
                expected_expectation_digest=None,
            )
        idle_entries = {_LOCK_NAME} | qualification_entries
        corpus_entries = idle_entries | {"corpus"}
        staging_entries = idle_entries | {".corpus-staging"}
        if entries == idle_entries:
            return {"corpus_id": DOCX_PRIVATE_CORPUS_ID, "removed": False, "verified_documents": 0}
        if entries == staging_entries:
            _remove_exact_empty_directory_at(private_descriptor, ".corpus-staging")
            if set(os.listdir(private_descriptor)) != idle_entries:
                raise PrivateDocxDomainError("private DOCX corpus cleanup failed")
            return {"corpus_id": DOCX_PRIVATE_CORPUS_ID, "removed": False, "verified_documents": 0}
        if entries != corpus_entries:
            raise PrivateDocxDomainError("private DOCX corpus cleanup failed")
        try:
            corpus_descriptor = _open_relative_directory(private_descriptor, ("corpus",), create=False)
        except FileNotFoundError:
            raise PrivateDocxDomainError("private DOCX corpus cleanup failed") from None
        if not os.listdir(corpus_descriptor):
            os.close(corpus_descriptor)
            corpus_descriptor = None
            _remove_exact_empty_directory_at(private_descriptor, "corpus")
            if set(os.listdir(private_descriptor)) != idle_entries:
                raise PrivateDocxDomainError("private DOCX corpus cleanup failed")
            return {"corpus_id": DOCX_PRIVATE_CORPUS_ID, "removed": False, "verified_documents": 0}
        manifest = _load_private_docx_manifest_at(corpus_descriptor)
        if qualification_entries:
            _load_qualification_result_at(
                private_descriptor,
                expected_expectation_digest=manifest["expectation_digest"],
            )
        for document in manifest["documents"]:
            _unlink_verified_at(
                corpus_descriptor,
                document["derived_name"],
                _read_file_at(corpus_descriptor, document["derived_name"], MAX_DOCX_BYTES),
                MAX_DOCX_BYTES,
            )
        _unlink_verified_at(corpus_descriptor, "manifest.json", _manifest_body(manifest), 64 * 1024)
        if os.listdir(corpus_descriptor):
            raise PrivateDocxDomainError("private DOCX corpus cleanup failed")
        os.rmdir("corpus", dir_fd=private_descriptor)
        os.fsync(private_descriptor)
        if not os.listdir(private_descriptor):
            os.rmdir("private-docx", dir_fd=root_descriptor)
            os.fsync(root_descriptor)
        return {
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "removed": True,
            "verified_documents": len(manifest["documents"]),
        }
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, PrivateDocxDomainError):
            raise
        raise PrivateDocxDomainError("private DOCX corpus cleanup failed") from None
    finally:
        for descriptor in (corpus_descriptor, lock_descriptor, private_descriptor, root_descriptor):
            if descriptor is not None:
                os.close(descriptor)
