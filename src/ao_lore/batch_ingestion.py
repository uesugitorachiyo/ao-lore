"""Fail-closed batch manifest and resumable checkpoint persistence."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ._strict_io import (
    ContractError,
    ensure_contained,
    parse_strict_json,
)
from .benchmark import BenchmarkError, canonical_digest
from .candidates import (
    CandidateError,
    load_verified_candidate,
    validate_candidate_document,
    validate_provenance,
)
from .distillation import DistillationError
from .home import repository_root, runtime_home
from .knowledge_contracts import KnowledgeContractError, validate_knowledge_policy
from .ingestion import (
    DOCX_POLICY,
    DocxProvenanceOrigin,
    IngestionDependencies,
    IngestionError,
    PDF_POLICY,
    SourceDigestMismatchError,
    ingest_docx_document,
    ingest_ocr_document,
    ingest_single_document,
    read_verified_docx_source,
    read_verified_pdf_source,
)
from .parsing import ParsingError


MAX_BATCH_BYTES = 1024 * 1024
CHECKPOINT_NAME = "checkpoint.json"
FINAL_READBACK_NAME = "final-readback.json"
_ALLOWED_BATCH_ENTRIES = {CHECKPOINT_NAME, FINAL_READBACK_NAME}
_OWNED_TEMP_RE = re.compile(
    r"^\.(?:checkpoint\.json|final-readback\.json)-[0-9a-f]{32}\.tmp$"
)
_BATCH_RE = re.compile(r"^batch-[a-z0-9][a-z0-9-]{0,62}$")
_ITEM_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_CANDIDATE_RE = re.compile(r"^candidate-[a-z0-9][a-z0-9._-]{0,117}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ERROR_CODES = {
    "source_invalid", "source_digest_mismatch", "qualification_invalid",
    "parser_rejected", "distillation_rejected", "candidate_conflict",
    "document_rejected",
}
_LOCK = threading.RLock()
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_BATCH_MANIFEST_V0_1 = "ao.lore.ingest-batch-manifest.v0.1"
_BATCH_MANIFEST_V0_2 = "ao.lore.ingest-batch-manifest.v0.2"
_BATCH_MANIFEST_V0_3 = "ao.lore.ingest-batch-manifest.v0.3"


class BatchIngestionError(ValueError):
    """Raised when batch evidence or checkpoint state fails closed."""


@dataclass(frozen=True)
class _BatchPolicy:
    format_id: str
    media_type: str
    extension: str
    parser_id: str
    parser_version: str


_PDF_BATCH_POLICY = _BatchPolicy(
    format_id=PDF_POLICY.format_id,
    media_type=PDF_POLICY.media_type,
    extension=PDF_POLICY.extension,
    parser_id=PDF_POLICY.parser_id,
    parser_version=PDF_POLICY.parser_version,
)
_DOCX_BATCH_POLICY = _BatchPolicy(
    format_id=DOCX_POLICY.format_id,
    media_type=DOCX_POLICY.media_type,
    extension=DOCX_POLICY.extension,
    parser_id=DOCX_POLICY.parser_id,
    parser_version=DOCX_POLICY.parser_version,
)
_OCR_BATCH_POLICY = _BatchPolicy(
    format_id="ocr",
    media_type="application/pdf",
    extension=".pdf",
    parser_id="paddle-ocr-english",
    parser_version="0.1.0",
)


def _pdf_signature_matches(data: bytes) -> bool:
    return isinstance(data, bytes) and data.startswith(b"%PDF-")


def _docx_signature_matches(data: bytes) -> bool:
    if not isinstance(data, bytes):
        return False
    try:
        from .docx_ooxml import validate_docx_package

        validate_docx_package(data)
    except Exception:
        return False
    return True


@dataclass(frozen=True)
class BatchDependencies:
    """Injected, manifest-bound seams for deterministic batch orchestration."""

    benchmark_manifest: Mapping[str, Any]
    selection_profile: Mapping[str, Any]
    quality_profile: Mapping[str, Any]
    format_id: str = _PDF_BATCH_POLICY.format_id
    media_type: str = _PDF_BATCH_POLICY.media_type
    extension: str = _PDF_BATCH_POLICY.extension
    parser_id: str = _PDF_BATCH_POLICY.parser_id
    parser_version: str = _PDF_BATCH_POLICY.parser_version
    signature_check: Callable[[bytes], bool] = _pdf_signature_matches
    expected_corpus_digest: str | None = None
    origin_for: Callable[[str | Path, bytes], DocxProvenanceOrigin] | None = None
    ingest_one: Callable[..., Mapping[str, Any]] = ingest_single_document
    inspect_candidate: Callable[[str], Mapping[str, Any]] = load_verified_candidate
    read_source: Callable[[str | Path, str], bytes] = read_verified_pdf_source
    append_checkpoint: Callable[..., Mapping[str, Any]] | None = None
    ingestion_dependencies: IngestionDependencies | None = None
    candidate_root: Path | None = None


@dataclass(frozen=True)
class _BatchDirectory:
    path: Path
    descriptor: int
    identity: tuple[int, int]


@dataclass
class _BatchSession:
    batch: _BatchDirectory
    descriptor: int
    manifest: Mapping[str, Any]
    manifest_digest: str
    inspect_candidate: Callable[[str], Mapping[str, Any]]
    verified_checkpoint_digest: str | None = None
    checkpoint_identity: tuple[int, int, int, int, int, int] | None = None


_ACTIVE_SESSION = threading.local()


def _fail(message: str, exc: BaseException | None = None) -> BatchIngestionError:
    error = BatchIngestionError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _exact(value: Mapping[str, Any], required: set[str]) -> bool:
    return set(value) == required


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _bounded_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100


def _strict_copy(value: Any, message: str) -> Any:
    try:
        canonical_digest(value)
        return copy.deepcopy(value)
    except (BenchmarkError, TypeError, ValueError) as exc:
        raise _fail(message, exc)


def _valid_source(value: Any, *, extension: str) -> bool:
    if not isinstance(value, str) or len(value) < 13 or len(value) > 1024:
        return False
    if "\\" in value or value.startswith("/"):
        return False
    parts = value.split("/")
    if len(parts) < 2 or parts[0] != "sources" or any(
        not part or part in {".", ".."} or not _SOURCE_PART_RE.fullmatch(part)
        for part in parts
    ):
        return False
    return parts[-1].endswith(extension) and parts[-1] != extension


def _manifest_policy_for_version(
    value: Mapping[str, Any], message: str
) -> _BatchPolicy:
    schema_version = value.get("schema_version")
    if schema_version == _BATCH_MANIFEST_V0_1:
        if not _exact(
            value, {"schema_version", "batch_id", "documents", "continue_on_error"}
        ):
            raise _fail(message)
        return _PDF_BATCH_POLICY
    if schema_version == _BATCH_MANIFEST_V0_2:
        if not _exact(
            value,
            {
                "schema_version",
                "batch_id",
                "format_id",
                "media_type",
                "parser_id",
                "parser_version",
                "documents",
                "continue_on_error",
            },
        ):
            raise _fail(message)
        if (
            value["format_id"] != _DOCX_BATCH_POLICY.format_id
            or value["media_type"] != _DOCX_BATCH_POLICY.media_type
            or value["parser_id"] != _DOCX_BATCH_POLICY.parser_id
            or value["parser_version"] != _DOCX_BATCH_POLICY.parser_version
        ):
            raise _fail(message)
        return _DOCX_BATCH_POLICY
    if schema_version == _BATCH_MANIFEST_V0_3:
        if not _exact(
            value,
            {
                "schema_version", "batch_id", "format_id", "media_type",
                "parser_id", "parser_version", "documents", "continue_on_error",
            },
        ):
            raise _fail(message)
        if (
            value["format_id"] != _OCR_BATCH_POLICY.format_id
            or value["media_type"] != _OCR_BATCH_POLICY.media_type
            or value["parser_id"] != _OCR_BATCH_POLICY.parser_id
            or value["parser_version"] != _OCR_BATCH_POLICY.parser_version
        ):
            raise _fail(message)
        return _OCR_BATCH_POLICY
    raise _fail(message)


def _dependency_policy(dependencies: BatchDependencies) -> _BatchPolicy:
    for policy in (_PDF_BATCH_POLICY, _DOCX_BATCH_POLICY, _OCR_BATCH_POLICY):
        if (
            dependencies.format_id == policy.format_id
            and dependencies.media_type == policy.media_type
            and dependencies.extension == policy.extension
            and dependencies.parser_id == policy.parser_id
            and dependencies.parser_version == policy.parser_version
        ):
            return policy
    raise _fail("batch dependencies are invalid")


def _validate_dependency_policy(
    manifest: Mapping[str, Any], dependencies: BatchDependencies
) -> _BatchPolicy:
    expected = _manifest_policy_for_version(manifest, "batch manifest is invalid")
    actual = _dependency_policy(dependencies)
    if actual != expected:
        raise _fail("batch dependencies are invalid")
    if expected == _PDF_BATCH_POLICY:
        if (
            dependencies.expected_corpus_digest is not None
            or dependencies.origin_for is not None
            or not callable(dependencies.signature_check)
            or dependencies.read_source is read_verified_docx_source
            or dependencies.ingest_one is ingest_docx_document
            or dependencies.signature_check is _docx_signature_matches
        ):
            raise _fail("batch dependencies are invalid")
    if expected == _DOCX_BATCH_POLICY:
        if (
            not isinstance(dependencies.expected_corpus_digest, str)
            or _DIGEST_RE.fullmatch(dependencies.expected_corpus_digest) is None
            or not callable(dependencies.origin_for)
            or not callable(dependencies.signature_check)
            or dependencies.read_source is read_verified_pdf_source
            or dependencies.ingest_one is ingest_single_document
            or dependencies.signature_check is _pdf_signature_matches
        ):
            raise _fail("batch dependencies are invalid")
    if expected == _OCR_BATCH_POLICY:
        from .model_roles import ActivatedOcrParser

        activation = (
            dependencies.ingestion_dependencies.ocr_activation
            if type(dependencies.ingestion_dependencies) is IngestionDependencies
            else None
        )
        if (
            dependencies.expected_corpus_digest is not None
            or dependencies.origin_for is not None
            or dependencies.ingest_one is not ingest_ocr_document
            or dependencies.read_source is not read_verified_pdf_source
            or dependencies.signature_check is not _pdf_signature_matches
            or type(activation) is not ActivatedOcrParser
            or activation.parser_id != expected.parser_id
            or activation.parser_version != expected.parser_version
        ):
            raise _fail("batch dependencies are invalid")
    return expected


def _docx_ingest_kwargs(
    dependencies: BatchDependencies,
    *,
    source: Path,
    source_bytes: bytes,
    expected_source_digest: str,
) -> dict[str, Any]:
    expected_corpus_digest = dependencies.expected_corpus_digest
    origin_for = dependencies.origin_for
    if (
        not isinstance(expected_corpus_digest, str)
        or _DIGEST_RE.fullmatch(expected_corpus_digest) is None
        or not callable(origin_for)
    ):
        raise _fail("batch dependencies are invalid")
    try:
        origin = origin_for(source, source_bytes)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise _fail("batch dependencies are invalid", exc)
    if type(origin) is not DocxProvenanceOrigin:
        raise _fail("batch dependencies are invalid")
    if (
        origin.derived_digest != expected_source_digest
        or origin.expectation_digest != expected_corpus_digest
    ):
        raise _fail("batch dependencies are invalid")
    return {
        "origin": origin,
        "expected_corpus_digest": expected_corpus_digest,
    }


def validate_batch_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach an exact batch manifest."""

    message = "batch manifest is invalid"
    if not isinstance(value, Mapping):
        raise _fail(message)
    result = _strict_copy(value, message)
    policy = _manifest_policy_for_version(result, message)
    if not isinstance(result["batch_id"], str) or not _BATCH_RE.fullmatch(result["batch_id"]):
        raise _fail(message)
    if result["continue_on_error"] is not True:
        raise _fail(message)
    documents = result["documents"]
    if not isinstance(documents, list) or not 1 <= len(documents) <= 100:
        raise _fail(message)
    item_ids: set[str] = set()
    sources: set[str] = set()
    for document in documents:
        allowed_document_keys = {
            frozenset({"item_id", "source", "source_digest"}),
            frozenset({"item_id", "source", "source_digest", "candidate_context"}),
        }
        if policy == _PDF_BATCH_POLICY:
            allowed_document_keys |= {
                frozenset({"item_id", "source", "source_digest", "knowledge_policy"}),
                frozenset({
                    "item_id", "source", "source_digest", "candidate_context",
                    "knowledge_policy",
                }),
            }
        if not isinstance(document, Mapping) or frozenset(document) not in allowed_document_keys:
            raise _fail(message)
        item_id = document["item_id"]
        source = document["source"]
        if not isinstance(item_id, str) or not _ITEM_RE.fullmatch(item_id):
            raise _fail(message)
        if (
            item_id in item_ids
            or not _valid_source(source, extension=policy.extension)
            or source in sources
        ):
            raise _fail(message)
        if not _digest(document["source_digest"]):
            raise _fail(message)
        item_ids.add(item_id)
        sources.add(source)
        if "candidate_context" in document:
            context = document["candidate_context"]
            if not isinstance(context, Mapping) or not _exact(
                context, {"schema_version", "candidates"}
            ) or context["schema_version"] != "ao.lore.candidate-comparison-context.v0.1":
                raise _fail(message)
            candidates = context["candidates"]
            if not isinstance(candidates, list) or len(candidates) > 100:
                raise _fail(message)
            records: set[tuple[str, str]] = set()
            for candidate in candidates:
                if not isinstance(candidate, Mapping) or not _exact(
                    candidate, {"candidate_id", "candidate_digest"}
                ):
                    raise _fail(message)
                candidate_id = candidate["candidate_id"]
                candidate_digest = candidate["candidate_digest"]
                if (
                    not isinstance(candidate_id, str)
                    or not _CANDIDATE_RE.fullmatch(candidate_id)
                    or not _digest(candidate_digest)
                    or (candidate_id, candidate_digest) in records
                ):
                    raise _fail(message)
                records.add((candidate_id, candidate_digest))
        if "knowledge_policy" in document:
            try:
                document["knowledge_policy"] = validate_knowledge_policy(
                    document["knowledge_policy"]
                )
            except KnowledgeContractError as exc:
                raise _fail(message, exc)
    return result


def load_batch_manifest(path: str | os.PathLike[str]) -> tuple[dict[str, Any], str]:
    """Load a contained strict manifest and return it with its canonical digest."""

    try:
        value = _read_anchored_json(
            Path(path), repository_root(), "batch manifest", MAX_BATCH_BYTES
        )
        manifest = validate_batch_manifest(value)
        return manifest, canonical_digest(manifest)
    except (BatchIngestionError, ContractError, BenchmarkError, OSError) as exc:
        if isinstance(exc, BatchIngestionError):
            raise
        raise _fail("batch manifest could not be loaded", exc)


def _anchored_path(
    path: Path, root: Path, label: str
) -> tuple[Path, Path, tuple[str, ...]]:
    selected, absolute_root = ensure_contained(path, root, label)
    relative = selected.relative_to(absolute_root)
    return selected, absolute_root, relative.parts


def _open_anchored_directory(
    path: Path, root: Path, *, create: bool
) -> int:
    """Open a directory through held root-relative no-follow descriptors."""

    _, absolute_root, parts = _anchored_path(path, root, "batch state")
    current: int | None = None
    try:
        current = os.open(os.path.sep, _DIRECTORY_FLAGS)
        for root_part in absolute_root.parts[1:]:
            child = os.open(root_part, _DIRECTORY_FLAGS, dir_fd=current)
            previous = current
            current = child
            os.close(previous)
        root_info = os.fstat(current)
        if not stat.S_ISDIR(root_info.st_mode):
            raise OSError("invalid root")
        for part in parts:
            child: int | None = None
            try:
                try:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o700, dir_fd=current)
                    except FileExistsError:
                        pass
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
                child_info = os.fstat(child)
                if not stat.S_ISDIR(child_info.st_mode):
                    raise OSError("invalid directory component")
            except BaseException:
                if child is not None:
                    os.close(child)
                raise
            previous = current
            current = child
            os.close(previous)
        return current
    except BaseException:
        if current is not None:
            os.close(current)
        raise


def _read_anchored_json(
    path: Path, root: Path, label: str, max_bytes: int
) -> dict[str, Any]:
    """Read a bounded file through one held root-relative descriptor chain."""

    selected, absolute_root, parts = _anchored_path(path, root, label)
    if not parts:
        raise ContractError(f"{label} must name a file")
    parent = selected.parent
    parent_descriptor = _open_anchored_directory(
        parent, absolute_root, create=False
    )
    file_descriptor: int | None = None
    try:
        name = parts[-1]
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise OSError("state file is not regular")
        if before.st_size > max_bytes:
            raise OSError("state file is too large")
        file_descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError("state file changed")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(file_descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        after = os.fstat(file_descriptor)
        public = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            len(body) > max_bytes
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns,
                opened.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns,
                after.st_ctime_ns)
            or (opened.st_dev, opened.st_ino) != (public.st_dev, public.st_ino)
        ):
            raise OSError("state file changed")
        return parse_strict_json(body, label)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        os.close(parent_descriptor)


def _ensure_real_directory(path: Path, root: Path) -> _BatchDirectory:
    descriptor: int | None = None
    try:
        selected, _, _ = _anchored_path(path, root, "batch state")
        descriptor = _open_anchored_directory(selected, root, create=True)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("invalid batch directory")
        result = _BatchDirectory(
            path=selected,
            descriptor=descriptor,
            identity=(info.st_dev, info.st_ino),
        )
        descriptor = None
        return result
    except BaseException as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        if isinstance(exc, (ContractError, OSError)):
            raise _fail("batch state directory is invalid", exc)
        raise


def _batch_directory(batch_id: str, batch_root: Path | None) -> _BatchDirectory:
    if not isinstance(batch_id, str) or not _BATCH_RE.fullmatch(batch_id):
        raise _fail("batch identity is invalid")
    repo = repository_root()
    selected = (
        runtime_home() / "batches" / batch_id
        if batch_root is None
        else Path(batch_root)
    )
    return _ensure_real_directory(selected, repo)


@contextmanager
def _locked_batch(batch: _BatchDirectory):
    """Serialize state inspection and replacement without adding lock entries."""

    descriptor: int | None = batch.descriptor
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != batch.identity
        ):
            raise OSError("invalid batch directory")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        _assert_batch_binding(batch.path, descriptor, batch.identity)
        _recover_owned_temporaries(descriptor)
        _validate_batch_entries(descriptor)
        _assert_batch_binding(batch.path, descriptor, batch.identity)
    except BaseException as exc:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except BaseException:
                pass
        if isinstance(exc, BatchIngestionError):
            raise
        if isinstance(exc, (ContractError, OSError)):
            raise _fail("batch state directory is invalid", exc)
        raise
    primary: BaseException | None = None
    try:
        yield descriptor
        _assert_batch_binding(batch.path, descriptor, batch.identity)
        _validate_batch_entries(descriptor)
        _assert_batch_binding(batch.path, descriptor, batch.identity)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup: BaseException | None = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except BaseException as exc:
            cleanup = exc
        try:
            os.close(descriptor)
        except BaseException as exc:
            if cleanup is None:
                cleanup = exc
        if primary is None and cleanup is not None:
            if isinstance(cleanup, (ContractError, OSError)):
                raise _fail("batch state lock cleanup failed", cleanup)
            raise cleanup


def _assert_batch_binding(
    batch: Path, descriptor: int, identity: tuple[int, int]
) -> None:
    try:
        held = os.fstat(descriptor)
        public_descriptor = _open_anchored_directory(
            batch, repository_root(), create=False
        )
        try:
            public = os.fstat(public_descriptor)
        finally:
            os.close(public_descriptor)
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(public.st_mode)
            or (held.st_dev, held.st_ino) != identity
            or (public.st_dev, public.st_ino) != identity
        ):
            raise OSError("batch directory binding changed")
    except (ContractError, OSError) as exc:
        raise _fail("batch state directory binding changed", exc)


def _validate_batch_entries(descriptor: int) -> None:
    try:
        for name in os.listdir(descriptor):
            if name not in _ALLOWED_BATCH_ENTRIES:
                raise OSError("unexpected entry")
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise OSError("invalid fixed entry")
    except OSError as exc:
        raise _fail("batch state contains invalid entries", exc)


def _recover_owned_temporaries(descriptor: int) -> None:
    """Remove only regular writer-owned crash residue while holding the lock."""

    removed = False
    try:
        for name in os.listdir(descriptor):
            if _OWNED_TEMP_RE.fullmatch(name) is None:
                continue
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise OSError("unsafe temporary state")
            opened = os.open(
                name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                held = os.fstat(opened)
                if (
                    not stat.S_ISREG(held.st_mode)
                    or (held.st_dev, held.st_ino) != (info.st_dev, info.st_ino)
                    or held.st_nlink != 1
                ):
                    raise OSError("temporary state changed")
                public = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (public.st_dev, public.st_ino) != (held.st_dev, held.st_ino):
                    raise OSError("temporary state changed")
                os.unlink(name, dir_fd=descriptor)
                removed = True
            finally:
                os.close(opened)
        if removed:
            os.fsync(descriptor)
    except OSError as exc:
        raise _fail("batch state contains invalid temporary entries", exc)


def _assert_session_state(session: _BatchSession) -> None:
    """Require the locked batch path, entry set, and checkpoint inode to be stable."""

    _assert_batch_binding(
        session.batch.path, session.descriptor, session.batch.identity
    )
    _validate_batch_entries(session.descriptor)
    try:
        current = os.stat(
            CHECKPOINT_NAME,
            dir_fd=session.descriptor,
            follow_symlinks=False,
        )
        identity = _regular_file_identity(current)
    except OSError as exc:
        raise _fail("batch checkpoint session binding changed", exc)
    if session.checkpoint_identity is None or identity != session.checkpoint_identity:
        raise _fail("batch checkpoint session binding changed")
    _assert_batch_binding(
        session.batch.path, session.descriptor, session.batch.identity
    )


def _manifest_items(manifest: Mapping[str, Any]) -> list[dict[str, str]]:
    return [
        {"item_id": document["item_id"], "source_digest": document["source_digest"]}
        for document in manifest["documents"]
    ]


def _reconstruct_checkpoint(
    manifest: Mapping[str, Any],
    manifest_digest: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Replay a bounded terminal prefix into its only valid digest chain."""

    current = _initial_checkpoint(manifest, manifest_digest)
    for item in items:
        updated = copy.deepcopy(current)
        updated["items"].append(copy.deepcopy(item))
        updated[item["status"]] += 1
        updated["processed"] += 1
        updated["next_item_index"] += 1
        updated["previous_checkpoint_digest"] = current["checkpoint_digest"]
        updated.pop("checkpoint_digest")
        updated["checkpoint_digest"] = canonical_digest(updated)
        current = updated
    return current


def _validate_terminal_item(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail("batch checkpoint is invalid")
    item = _strict_copy(value, "batch checkpoint is invalid")
    common = {"item_id", "source_digest", "status"}
    if (
        not isinstance(item.get("item_id"), str)
        or not _ITEM_RE.fullmatch(item["item_id"])
        or not _digest(item.get("source_digest"))
    ):
        raise _fail("batch checkpoint is invalid")
    status_value = item.get("status")
    if not isinstance(status_value, str):
        raise _fail("batch checkpoint is invalid")
    if status_value in {"created", "unchanged"}:
        if set(item) != common | {
            "candidate_id", "candidate_digest", "provenance_digest", "review_status"
        }:
            raise _fail("batch checkpoint is invalid")
        if (
            not isinstance(item["candidate_id"], str)
            or not _CANDIDATE_RE.fullmatch(item["candidate_id"])
            or not _digest(item["candidate_digest"])
            or not _digest(item["provenance_digest"])
            or not isinstance(item["review_status"], str)
            or item["review_status"] not in {"unreviewed", "accepted", "rejected"}
        ):
            raise _fail("batch checkpoint is invalid")
    elif status_value == "rejected":
        if (
            set(item) != common | {"error_code"}
            or not isinstance(item["error_code"], str)
            or item["error_code"] not in _ERROR_CODES
        ):
            raise _fail("batch checkpoint is invalid")
    else:
        raise _fail("batch checkpoint is invalid")
    return item


def validate_checkpoint(
    value: Mapping[str, Any], manifest: Mapping[str, Any], manifest_digest: str
) -> dict[str, Any]:
    """Validate checkpoint shape, digest chain, manifest binding, and arithmetic."""

    manifest_value = validate_batch_manifest(manifest)
    if not _digest(manifest_digest) or canonical_digest(manifest_value) != manifest_digest:
        raise _fail("batch checkpoint manifest binding is invalid")
    if not isinstance(value, Mapping):
        raise _fail("batch checkpoint is invalid")
    checkpoint = _strict_copy(value, "batch checkpoint is invalid")
    keys = {
        "schema_version", "batch_id", "manifest_digest", "manifest_items", "items",
        "created", "unchanged", "rejected", "processed", "next_item_index",
        "previous_checkpoint_digest", "checkpoint_digest", "canonical",
        "promotion_authority",
    }
    if not _exact(checkpoint, keys):
        raise _fail("batch checkpoint is invalid")
    if (
        checkpoint["schema_version"] != "ao.lore.ingest-batch-checkpoint.v0.1"
        or checkpoint["batch_id"] != manifest_value["batch_id"]
        or checkpoint["manifest_digest"] != manifest_digest
        or checkpoint["manifest_items"] != _manifest_items(manifest_value)
        or checkpoint["canonical"] is not False
        or checkpoint["promotion_authority"] is not False
        or not _digest(checkpoint["checkpoint_digest"])
    ):
        raise _fail("batch checkpoint is invalid")
    for field in ("created", "unchanged", "rejected", "processed", "next_item_index"):
        if not _bounded_int(checkpoint[field]):
            raise _fail("batch checkpoint is invalid")
    if not isinstance(checkpoint["items"], list) or len(checkpoint["items"]) > 100:
        raise _fail("batch checkpoint is invalid")
    items = [_validate_terminal_item(item) for item in checkpoint["items"]]
    expected_manifest = checkpoint["manifest_items"]
    if len(items) > len(expected_manifest):
        raise _fail("batch checkpoint is invalid")
    for index, item in enumerate(items):
        expected = expected_manifest[index]
        if item["item_id"] != expected["item_id"] or item["source_digest"] != expected["source_digest"]:
            raise _fail("batch checkpoint is invalid")
    counts = {
        "created": sum(item["status"] == "created" for item in items),
        "unchanged": sum(item["status"] == "unchanged" for item in items),
        "rejected": sum(item["status"] == "rejected" for item in items),
    }
    processed = len(items)
    if (
        any(checkpoint[key] != count for key, count in counts.items())
        or checkpoint["processed"] != processed
        or checkpoint["next_item_index"] != processed
    ):
        raise _fail("batch checkpoint is invalid")
    previous = checkpoint["previous_checkpoint_digest"]
    if (processed == 0 and previous is not None) or (processed > 0 and not _digest(previous)):
        raise _fail("batch checkpoint is invalid")
    digest_body = {key: child for key, child in checkpoint.items() if key != "checkpoint_digest"}
    if canonical_digest(digest_body) != checkpoint["checkpoint_digest"]:
        raise _fail("batch checkpoint is invalid")
    checkpoint["items"] = items
    reconstructed = _reconstruct_checkpoint(
        manifest_value, manifest_digest, items
    )
    if checkpoint != reconstructed:
        raise _fail("batch checkpoint is invalid")
    return checkpoint


def _verify_candidates(
    checkpoint: Mapping[str, Any],
    inspect_candidate_fn: Callable[[str], Mapping[str, Any]],
    *,
    session: _BatchSession | None = None,
) -> None:
    for item in checkpoint["items"]:
        if item["status"] == "rejected":
            continue
        try:
            if session is not None:
                _assert_session_state(session)
            loaded = inspect_candidate_fn(item["candidate_id"])
            if session is not None:
                _assert_session_state(session)
            if not isinstance(loaded, Mapping) or set(loaded) != {"candidate", "provenance", "inspection"}:
                raise ValueError("invalid candidate")
            loaded = _strict_copy(loaded, "batch candidate binding is invalid")
            candidate = loaded["candidate"]
            provenance = loaded["provenance"]
            inspection = loaded["inspection"]
            if not all(isinstance(child, Mapping) for child in (candidate, provenance, inspection)):
                raise ValueError("invalid candidate")
            candidate_value, candidate_digest = validate_candidate_document(candidate)
            provenance_value = validate_provenance(
                provenance, candidate_value, candidate_digest
            )
            inspection_keys = {
                "schema_version", "candidate_id", "candidate_digest",
                "provenance_digest", "verified_review_events", "review_status",
                "latest_event_digest", "canonical", "promotion_authority",
            }
            if set(inspection) != inspection_keys:
                raise ValueError("invalid candidate inspection")
            review_events = inspection["verified_review_events"]
            latest_event = inspection["latest_event_digest"]
            if (
                inspection["schema_version"]
                != "ao.lore.candidate-inspection.v0.1"
                or not isinstance(review_events, int)
                or isinstance(review_events, bool)
                or not 0 <= review_events <= 999999
                or inspection["review_status"]
                not in {"unreviewed", "accepted", "rejected"}
                or (
                    latest_event is not None
                    and not _digest(latest_event)
                )
                or (review_events == 0) != (latest_event is None)
                or (review_events == 0)
                != (inspection["review_status"] == "unreviewed")
                or inspection["canonical"] is not False
                or inspection["promotion_authority"] is not False
                or candidate_value["canonical"] is not False
                or candidate_value["promotion_authority"] is not False
            ):
                raise ValueError("invalid candidate inspection")
            if (
                candidate_value["candidate_id"] != item["candidate_id"]
                or candidate_digest != item["candidate_digest"]
                or provenance_value["candidate_id"] != item["candidate_id"]
                or provenance_value["candidate_digest"] != item["candidate_digest"]
                or provenance_value["source_digest"] != item["source_digest"]
                or canonical_digest(provenance_value) != item["provenance_digest"]
                or inspection.get("candidate_id") != item["candidate_id"]
                or inspection.get("candidate_digest") != item["candidate_digest"]
                or inspection.get("provenance_digest") != item["provenance_digest"]
                or inspection.get("review_status") != item["review_status"]
            ):
                raise ValueError("binding drift")
        except Exception as exc:
            raise _fail("batch candidate binding is invalid", exc)


def _regular_file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise OSError("state file is not a safe regular file")
    return (
        info.st_dev, info.st_ino, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns, info.st_nlink,
    )


def _read_json_at_verified(
    descriptor: int, name: str, label: str
) -> tuple[dict[str, Any], tuple[int, int, int, int, int, int]]:
    try:
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        before_identity = _regular_file_identity(before)
        if before.st_size > MAX_BATCH_BYTES:
            raise OSError("state file is too large")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(name, flags, dir_fd=descriptor)
        try:
            opened = os.fstat(file_descriptor)
            if (
                _regular_file_identity(opened) != before_identity
            ):
                raise OSError("state file changed")
            chunks: list[bytes] = []
            remaining = MAX_BATCH_BYTES + 1
            while remaining:
                chunk = os.read(file_descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b"".join(chunks)
            after = os.fstat(file_descriptor)
            if (
                (opened.st_dev, opened.st_ino, opened.st_size)
                != (after.st_dev, after.st_ino, after.st_size)
                or opened.st_mtime_ns != after.st_mtime_ns
                or opened.st_ctime_ns != after.st_ctime_ns
            ):
                raise OSError("state file changed")
            if len(body) > MAX_BATCH_BYTES:
                raise OSError("state file is too large")
            parsed = parse_strict_json(body, label)
            stable = os.fstat(file_descriptor)
            public = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            opened_state = _regular_file_identity(opened)
            if (
                opened_state != _regular_file_identity(stable)
                or opened_state != _regular_file_identity(public)
            ):
                raise OSError("state file changed")
            return parsed, opened_state
        finally:
            os.close(file_descriptor)
    except (ContractError, OSError) as exc:
        raise _fail(f"{label} could not be loaded", exc)


def _read_json_at(descriptor: int, name: str, label: str) -> dict[str, Any]:
    return _read_json_at_verified(descriptor, name, label)[0]


def _load_checkpoint(
    descriptor: int,
    manifest: Mapping[str, Any],
    manifest_digest: str,
    inspect_candidate_fn: Callable[[str], Mapping[str, Any]],
    *,
    verify_candidates: bool,
) -> dict[str, Any]:
    return _load_checkpoint_with_identity(
        descriptor, manifest, manifest_digest, inspect_candidate_fn,
        verify_candidates=verify_candidates,
    )[0]


def _load_checkpoint_with_identity(
    descriptor: int,
    manifest: Mapping[str, Any],
    manifest_digest: str,
    inspect_candidate_fn: Callable[[str], Mapping[str, Any]],
    *,
    verify_candidates: bool,
) -> tuple[dict[str, Any], tuple[int, int, int, int, int, int]]:
    try:
        value, identity = _read_json_at_verified(
            descriptor, CHECKPOINT_NAME, "batch checkpoint"
        )
        checkpoint = validate_checkpoint(value, manifest, manifest_digest)
        if verify_candidates:
            _verify_candidates(checkpoint, inspect_candidate_fn)
            current = os.stat(
                CHECKPOINT_NAME, dir_fd=descriptor, follow_symlinks=False
            )
            if _regular_file_identity(current) != identity:
                raise OSError("batch checkpoint changed during candidate inspection")
        return copy.deepcopy(checkpoint), identity
    except (BatchIngestionError, ContractError, BenchmarkError, OSError) as exc:
        if isinstance(exc, BatchIngestionError):
            raise
        raise _fail("batch checkpoint could not be loaded", exc)


def _persist_json_at(
    batch: Path,
    directory_descriptor: int,
    name: str,
    label: str,
    value: Mapping[str, Any],
) -> None:
    if name not in _ALLOWED_BATCH_ENTRIES:
        raise _fail(f"{label} target is invalid")
    body = (json.dumps(value, ensure_ascii=False, allow_nan=False,
                       sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if len(body) > MAX_BATCH_BYTES:
        raise _fail(f"{label} is too large")
    temporary: str | None = None
    temporary_identity: tuple[int, int] | None = None
    file_descriptor: int | None = None
    identity_info = os.fstat(directory_descriptor)
    identity = (identity_info.st_dev, identity_info.st_ino)
    try:
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        for _ in range(32):
            temporary = f".{name}-{secrets.token_hex(16)}.tmp"
            try:
                file_descriptor = os.open(
                    temporary, flags, 0o600, dir_fd=directory_descriptor
                )
                break
            except FileExistsError:
                temporary = None
        if file_descriptor is None or temporary is None:
            raise OSError("temporary state collision")
        info = os.fstat(file_descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("invalid temporary")
        temporary_identity = (info.st_dev, info.st_ino)
        view = memoryview(body)
        while view:
            written = os.write(file_descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        _assert_batch_binding(batch, directory_descriptor, identity)
        current_temporary = os.stat(
            temporary,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current_temporary.st_mode)
            or (current_temporary.st_dev, current_temporary.st_ino)
            != temporary_identity
        ):
            raise OSError("temporary checkpoint changed")
        os.replace(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        temporary = None
        os.fsync(directory_descriptor)
        _assert_batch_binding(batch, directory_descriptor, identity)
    except (BatchIngestionError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, BatchIngestionError):
            raise
        raise _fail(f"{label} could not be persisted", exc)
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary is not None:
            try:
                info = os.stat(
                    temporary,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if (
                    stat.S_ISREG(info.st_mode)
                    and not stat.S_ISLNK(info.st_mode)
                    and (info.st_dev, info.st_ino) == temporary_identity
                ):
                    os.unlink(temporary, dir_fd=directory_descriptor)
            except OSError as exc:
                raise _fail(f"{label} temporary cleanup failed", exc)


def _persist_checkpoint(
    batch: Path, directory_descriptor: int, checkpoint: Mapping[str, Any]
) -> None:
    _persist_json_at(
        batch,
        directory_descriptor,
        CHECKPOINT_NAME,
        "batch checkpoint",
        checkpoint,
    )


def _initial_checkpoint(manifest: Mapping[str, Any], manifest_digest: str) -> dict[str, Any]:
    checkpoint: dict[str, Any] = {
        "schema_version": "ao.lore.ingest-batch-checkpoint.v0.1",
        "batch_id": manifest["batch_id"],
        "manifest_digest": manifest_digest,
        "manifest_items": _manifest_items(manifest),
        "items": [],
        "created": 0,
        "unchanged": 0,
        "rejected": 0,
        "processed": 0,
        "next_item_index": 0,
        "previous_checkpoint_digest": None,
        "canonical": False,
        "promotion_authority": False,
    }
    checkpoint["checkpoint_digest"] = canonical_digest(checkpoint)
    return checkpoint


def load_or_initialize_checkpoint(
    manifest: Mapping[str, Any],
    manifest_digest: str,
    batch_root: Path | None = None,
    inspect_candidate_fn: Callable[[str], Mapping[str, Any]] = load_verified_candidate,
) -> dict[str, Any]:
    """Reopen a verified checkpoint or atomically initialize its exact state."""

    manifest_value = validate_batch_manifest(manifest)
    if not _digest(manifest_digest) or canonical_digest(manifest_value) != manifest_digest:
        raise _fail("batch manifest digest is invalid")
    with _LOCK:
        batch = _batch_directory(manifest_value["batch_id"], batch_root)
        with _locked_batch(batch) as descriptor:
            try:
                info = os.stat(
                    CHECKPOINT_NAME,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                checkpoint = _initial_checkpoint(manifest_value, manifest_digest)
                _persist_checkpoint(batch.path, descriptor, checkpoint)
            else:
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise _fail("batch checkpoint file is invalid")
            return _load_checkpoint(
                descriptor, manifest_value, manifest_digest, inspect_candidate_fn,
                verify_candidates=True,
            )


def append_terminal_item(
    checkpoint: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    manifest_digest: str,
    batch_root: Path | None = None,
    inspect_candidate_fn: Callable[[str], Mapping[str, Any]] = load_verified_candidate,
) -> dict[str, Any]:
    """Append the next exact terminal result to a durably current checkpoint."""

    manifest_value = validate_batch_manifest(manifest)
    supplied = validate_checkpoint(checkpoint, manifest_value, manifest_digest)
    terminal = _validate_terminal_item(item)
    active = getattr(_ACTIVE_SESSION, "value", None)
    if (
        isinstance(active, _BatchSession)
        and active.manifest == manifest_value
        and active.manifest_digest == manifest_digest
    ):
        return _append_terminal_item_at(active, supplied, terminal)
    with _LOCK:
        batch = _batch_directory(manifest_value["batch_id"], batch_root)
        with _locked_batch(batch) as descriptor:
            session = _BatchSession(
                batch, descriptor, manifest_value, manifest_digest,
                inspect_candidate_fn,
            )
            _, session.checkpoint_identity = _load_checkpoint_with_identity(
                descriptor, manifest_value, manifest_digest,
                inspect_candidate_fn, verify_candidates=False,
            )
            return _append_terminal_item_at(session, supplied, terminal)


def _append_terminal_item_at(
    session: _BatchSession,
    supplied: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> dict[str, Any]:
    """Append through an already locked, inode-bound batch session."""

    _assert_session_state(session)
    durable = _load_checkpoint(
        session.descriptor, session.manifest, session.manifest_digest,
        session.inspect_candidate, verify_candidates=False,
    )
    if session.verified_checkpoint_digest != durable["checkpoint_digest"]:
        _verify_candidates(
            durable, session.inspect_candidate, session=session
        )
        session.verified_checkpoint_digest = durable["checkpoint_digest"]
    if durable != supplied:
        raise _fail("supplied checkpoint is stale")
    index = durable["next_item_index"]
    if index >= len(durable["manifest_items"]):
        raise _fail("batch checkpoint is terminal")
    expected = durable["manifest_items"][index]
    if (
        terminal["item_id"] != expected["item_id"]
        or terminal["source_digest"] != expected["source_digest"]
    ):
        raise _fail("terminal item is out of order")
    updated = _reconstruct_checkpoint(
        session.manifest,
        session.manifest_digest,
        [*durable["items"], terminal],
    )
    validate_checkpoint(updated, session.manifest, session.manifest_digest)
    _verify_candidates(
        {"items": [terminal]}, session.inspect_candidate, session=session
    )
    _assert_session_state(session)
    session.verified_checkpoint_digest = updated["checkpoint_digest"]
    _persist_checkpoint(session.batch.path, session.descriptor, updated)
    reopened, reopened_identity = _load_checkpoint_with_identity(
        session.descriptor, session.manifest, session.manifest_digest,
        session.inspect_candidate, verify_candidates=False,
    )
    if reopened != updated:
        raise _fail("durable checkpoint did not match appended state")
    session.checkpoint_identity = reopened_identity
    _assert_session_state(session)
    return reopened


def _expected_next_commands(items: list[dict[str, Any]]) -> list[str]:
    commands: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item["status"] == "rejected":
            continue
        candidate_id = item["candidate_id"]
        for command in (
            f"ao-lore candidate inspect --candidate-id {candidate_id}",
            f"ao-lore candidate review --candidate-id {candidate_id} --decision accept --reviewer <reviewer-id>",
        ):
            if command not in seen:
                seen.add(command)
                commands.append(command)
    commands.append("ao-lore candidate list --json")
    return commands


def validate_final_batch_readback(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a terminal aggregate without granting it authority."""

    message = "final batch readback is invalid"
    if not isinstance(value, Mapping):
        raise _fail(message)
    report = _strict_copy(value, message)
    keys = {
        "schema_version", "batch_id", "manifest_digest", "status", "total",
        "created", "unchanged", "rejected", "processed", "items",
        "next_commands", "canonical", "promotion_authority",
    }
    if not _exact(report, keys):
        raise _fail(message)
    if (
        report["schema_version"] != "ao.lore.ingest-batch-readback.v0.1"
        or not isinstance(report["batch_id"], str)
        or _BATCH_RE.fullmatch(report["batch_id"]) is None
        or not _digest(report["manifest_digest"])
        or report["status"] not in {"completed", "partial"}
        or report["canonical"] is not False
        or report["promotion_authority"] is not False
    ):
        raise _fail(message)
    for field in ("total", "created", "unchanged", "rejected", "processed"):
        if not _bounded_int(report[field]):
            raise _fail(message)
    if report["total"] < 1 or report["processed"] < 1:
        raise _fail(message)
    if not isinstance(report["items"], list):
        raise _fail(message)
    items = [_validate_terminal_item(item) for item in report["items"]]
    item_digests = [canonical_digest(item) for item in items]
    item_ids = [item["item_id"] for item in items]
    if (
        len(item_digests) != len(set(item_digests))
        or len(item_ids) != len(set(item_ids))
    ):
        raise _fail(message)
    counts = {
        "created": sum(item["status"] == "created" for item in items),
        "unchanged": sum(item["status"] == "unchanged" for item in items),
        "rejected": sum(item["status"] == "rejected" for item in items),
    }
    if (
        report["total"] != len(items)
        or report["processed"] != len(items)
        or any(report[key] != count for key, count in counts.items())
        or report["status"] != ("completed" if counts["rejected"] == 0 else "partial")
        or not isinstance(report["next_commands"], list)
        or report["next_commands"] != _expected_next_commands(items)
    ):
        raise _fail(message)
    report["items"] = items
    return report


def _checkpoint_projection(
    checkpoint: Mapping[str, Any], manifest: Mapping[str, Any], manifest_digest: str
) -> dict[str, Any]:
    if checkpoint["processed"] != len(manifest["documents"]):
        raise _fail("batch checkpoint is not terminal")
    report = {
        "schema_version": "ao.lore.ingest-batch-readback.v0.1",
        "batch_id": manifest["batch_id"],
        "manifest_digest": manifest_digest,
        "status": "completed" if checkpoint["rejected"] == 0 else "partial",
        "total": len(manifest["documents"]),
        "created": checkpoint["created"],
        "unchanged": checkpoint["unchanged"],
        "rejected": checkpoint["rejected"],
        "processed": checkpoint["processed"],
        "items": copy.deepcopy(checkpoint["items"]),
        "next_commands": _expected_next_commands(checkpoint["items"]),
        "canonical": False,
        "promotion_authority": False,
    }
    return validate_final_batch_readback(report)


def _load_or_initialize_checkpoint_at(session: _BatchSession) -> dict[str, Any]:
    try:
        info = os.stat(
            CHECKPOINT_NAME, dir_fd=session.descriptor, follow_symlinks=False
        )
    except FileNotFoundError:
        checkpoint = _initial_checkpoint(
            session.manifest, session.manifest_digest
        )
        _persist_checkpoint(session.batch.path, session.descriptor, checkpoint)
    else:
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise _fail("batch checkpoint file is invalid")
    checkpoint, identity = _load_checkpoint_with_identity(
        session.descriptor, session.manifest, session.manifest_digest,
        session.inspect_candidate, verify_candidates=True,
    )
    session.checkpoint_identity = identity
    session.verified_checkpoint_digest = checkpoint["checkpoint_digest"]
    _assert_session_state(session)
    return checkpoint


def _load_final_at(session: _BatchSession) -> dict[str, Any] | None:
    try:
        os.stat(
            FINAL_READBACK_NAME,
            dir_fd=session.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    return validate_final_batch_readback(
        _read_json_at(
            session.descriptor, FINAL_READBACK_NAME, "final batch readback"
        )
    )


def _persist_or_verify_final_at(
    session: _BatchSession, checkpoint: Mapping[str, Any]
) -> dict[str, Any]:
    expected = _checkpoint_projection(
        checkpoint, session.manifest, session.manifest_digest
    )
    _assert_session_state(session)
    durable, durable_identity = _load_checkpoint_with_identity(
        session.descriptor, session.manifest, session.manifest_digest,
        session.inspect_candidate, verify_candidates=False,
    )
    if durable_identity != session.checkpoint_identity or durable != checkpoint:
        raise _fail("batch checkpoint changed before finalization")
    _verify_candidates(
        durable, session.inspect_candidate, session=session
    )
    _assert_session_state(session)
    reopened = _load_final_at(session)
    if reopened is None:
        _persist_json_at(
            session.batch.path, session.descriptor, FINAL_READBACK_NAME,
            "final batch readback", expected,
        )
        reopened = _load_final_at(session)
    if reopened != expected:
        raise _fail("final batch readback binding is invalid")
    _assert_session_state(session)
    return reopened


def _validate_single_readback(
    value: Mapping[str, Any],
    expected_source_digest: str,
    *,
    expected_parser_id: str,
    expected_parser_version: str,
) -> dict[str, Any]:
    message = "single-document ingest readback is invalid"
    if not isinstance(value, Mapping):
        raise _fail(message)
    report = _strict_copy(value, message)
    keys = {
        "schema_version", "status", "candidate_id", "candidate_digest",
        "provenance_digest", "source_digest", "parser_id", "parser_version",
        "document_ir_digest", "parser_selection_report_digest",
        "parse_quality_report_digest", "distillation_trace_digest",
        "review_status", "next_commands", "canonical", "promotion_authority",
    }
    digest_fields = {
        "candidate_digest", "provenance_digest", "source_digest",
        "document_ir_digest", "parser_selection_report_digest",
        "parse_quality_report_digest", "distillation_trace_digest",
    }
    candidate_id = report.get("candidate_id")
    expected_commands = [
        f"ao-lore candidate inspect --candidate-id {candidate_id}",
        f"ao-lore candidate review --candidate-id {candidate_id} --decision accept --reviewer <reviewer-id>",
    ]
    if (
        not _exact(report, keys)
        or report["schema_version"] != "ao.lore.single-document-ingest-readback.v0.1"
        or report["status"] not in {"created", "unchanged"}
        or not isinstance(candidate_id, str)
        or _CANDIDATE_RE.fullmatch(candidate_id) is None
        or any(not _digest(report[field]) for field in digest_fields)
        or report["source_digest"] != expected_source_digest
        or report["parser_id"] != expected_parser_id
        or report["parser_version"] != expected_parser_version
        or report["review_status"] not in {"unreviewed", "accepted", "rejected"}
        or report["next_commands"] != expected_commands
        or report["canonical"] is not False
        or report["promotion_authority"] is not False
    ):
        raise _fail(message)
    return report


def _error_chain(exc: BaseException):
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def _document_error_code(exc: BaseException) -> str | None:
    chain = tuple(_error_chain(exc))
    permitted = (
        IngestionError,
        ParsingError,
        DistillationError,
        CandidateError,
        BenchmarkError,
    )
    if not isinstance(exc, permitted) or any(
        not isinstance(item, permitted) for item in chain
    ):
        return None
    if any(isinstance(item, SourceDigestMismatchError) for item in chain):
        return "source_digest_mismatch"
    if any(isinstance(item, ParsingError) for item in chain):
        return "parser_rejected"
    if any(isinstance(item, DistillationError) for item in chain):
        return "distillation_rejected"
    if any(isinstance(item, CandidateError) for item in chain):
        return "candidate_conflict"
    if any(isinstance(item, BenchmarkError) for item in chain):
        return "qualification_invalid"
    if isinstance(exc, IngestionError):
        return "document_rejected"
    return None


def _source_error_code(exc: IngestionError) -> str | None:
    chain = tuple(_error_chain(exc))
    permitted = all(
        isinstance(item, SourceDigestMismatchError)
        or type(item) is IngestionError
        or isinstance(item, OSError)
        or type(item) in {TypeError, ValueError}
        for item in chain
    )
    if not permitted:
        return None
    return (
        "source_digest_mismatch"
        if any(isinstance(item, SourceDigestMismatchError) for item in chain)
        else "source_invalid"
    )


def _terminal_success(
    document: Mapping[str, Any], readback: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "item_id": document["item_id"],
        "source_digest": document["source_digest"],
        "status": readback["status"],
        "candidate_id": readback["candidate_id"],
        "candidate_digest": readback["candidate_digest"],
        "provenance_digest": readback["provenance_digest"],
        "review_status": readback["review_status"],
    }


def ingest_batch(
    manifest_path: str | os.PathLike[str],
    *,
    dependencies: BatchDependencies,
    batch_root: Path | None = None,
) -> dict[str, Any]:
    """Ingest a strict manifest sequentially with verified resume state."""

    if not isinstance(dependencies, BatchDependencies):
        raise _fail("batch dependencies are invalid")
    benchmark_manifest = _strict_copy(
        dependencies.benchmark_manifest, "batch qualification is invalid"
    )
    selection_profile = _strict_copy(
        dependencies.selection_profile, "batch qualification is invalid"
    )
    quality_profile = _strict_copy(
        dependencies.quality_profile, "batch qualification is invalid"
    )
    manifest, manifest_digest = load_batch_manifest(manifest_path)
    return _ingest_verified_batch(
        manifest,
        manifest_digest,
        dependencies,
        benchmark_manifest,
        selection_profile,
        quality_profile,
        batch_root,
    )


def ingest_verified_batch(
    manifest: Mapping[str, Any],
    manifest_digest: str,
    *,
    dependencies: BatchDependencies,
    batch_root: Path | None = None,
) -> dict[str, Any]:
    """Ingest a detached, already-verified manifest without reopening its path."""

    if not isinstance(dependencies, BatchDependencies):
        raise _fail("batch dependencies are invalid")
    verified = validate_batch_manifest(manifest)
    if not _digest(manifest_digest) or canonical_digest(verified) != manifest_digest:
        raise _fail("batch manifest digest is invalid")
    benchmark_manifest = _strict_copy(
        dependencies.benchmark_manifest, "batch qualification is invalid"
    )
    selection_profile = _strict_copy(
        dependencies.selection_profile, "batch qualification is invalid"
    )
    quality_profile = _strict_copy(
        dependencies.quality_profile, "batch qualification is invalid"
    )
    return _ingest_verified_batch(
        verified,
        manifest_digest,
        dependencies,
        benchmark_manifest,
        selection_profile,
        quality_profile,
        batch_root,
    )


def _ingest_verified_batch(
    manifest: Mapping[str, Any],
    manifest_digest: str,
    dependencies: BatchDependencies,
    benchmark_manifest: Mapping[str, Any],
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
    batch_root: Path | None,
) -> dict[str, Any]:
    _validate_dependency_policy(manifest, dependencies)
    inspect_candidate_fn = dependencies.inspect_candidate
    if (
        dependencies.candidate_root is not None
        and inspect_candidate_fn is load_verified_candidate
    ):
        inspect_candidate_fn = lambda candidate_id: load_verified_candidate(
            candidate_id, candidate_root=dependencies.candidate_root
        )
    with _LOCK:
        batch = _batch_directory(manifest["batch_id"], batch_root)
        with _locked_batch(batch) as descriptor:
            session = _BatchSession(
                batch, descriptor, manifest, manifest_digest,
                inspect_candidate_fn,
            )
            previous_session = getattr(_ACTIVE_SESSION, "value", None)
            _ACTIVE_SESSION.value = session
            try:
                return _ingest_batch_at(
                    session, dependencies, benchmark_manifest,
                    selection_profile, quality_profile,
                )
            finally:
                _ACTIVE_SESSION.value = previous_session


def _ingest_batch_at(
    session: _BatchSession,
    dependencies: BatchDependencies,
    benchmark_manifest: Mapping[str, Any],
    selection_profile: Mapping[str, Any],
    quality_profile: Mapping[str, Any],
) -> dict[str, Any]:
    checkpoint = _load_or_initialize_checkpoint_at(session)
    existing_final = _load_final_at(session)
    documents = session.manifest["documents"]
    if existing_final is not None and checkpoint["processed"] != len(documents):
        raise _fail("final batch readback exists before terminal checkpoint")
    appender = dependencies.append_checkpoint or append_terminal_item
    for document in documents[checkpoint["next_item_index"]:]:
        _assert_session_state(session)
        source = repository_root() / document["source"]
        terminal: dict[str, Any] | None = None
        ingest_kwargs: dict[str, Any] = {}
        try:
            data = dependencies.read_source(source, document["source_digest"])
        except IngestionError as exc:
            code = _source_error_code(exc)
            if code is None:
                raise
        else:
            code = (
                "source_digest_mismatch"
                if not isinstance(data, bytes)
                or not dependencies.signature_check(data)
                or "sha256:" + hashlib.sha256(data).hexdigest()
                != document["source_digest"]
                else None
            )
            if code is None and dependencies.format_id == _DOCX_BATCH_POLICY.format_id:
                ingest_kwargs = _docx_ingest_kwargs(
                    dependencies,
                    source=source,
                    source_bytes=data,
                    expected_source_digest=document["source_digest"],
                )
        _assert_session_state(session)
        if code is not None:
            terminal = {
                "item_id": document["item_id"],
                "source_digest": document["source_digest"],
                "status": "rejected",
                "error_code": code,
            }
        else:
            try:
                context = copy.deepcopy(document.get("candidate_context", {
                    "schema_version": "ao.lore.candidate-comparison-context.v0.1",
                    "candidates": [],
                }))
                if "knowledge_policy" in document:
                    context["knowledge_policy"] = copy.deepcopy(document["knowledge_policy"])
                readback = dependencies.ingest_one(
                    source,
                    benchmark_manifest=copy.deepcopy(benchmark_manifest),
                    selection_profile=copy.deepcopy(selection_profile),
                    quality_profile=copy.deepcopy(quality_profile),
                    candidate_context=context,
                    expected_source_digest=document["source_digest"],
                    dependencies=dependencies.ingestion_dependencies,
                    candidate_root=dependencies.candidate_root,
                    **ingest_kwargs,
                )
                terminal = _terminal_success(
                    document,
                    _validate_single_readback(
                        readback,
                        document["source_digest"],
                        expected_parser_id=dependencies.parser_id,
                        expected_parser_version=dependencies.parser_version,
                    ),
                )
            except BaseException as exc:
                rejection_code = _document_error_code(exc)
                if rejection_code is None:
                    raise
                terminal = {
                    "item_id": document["item_id"],
                    "source_digest": document["source_digest"],
                    "status": "rejected",
                    "error_code": rejection_code,
                }
        _assert_session_state(session)
        if terminal is None:
            raise _fail("batch terminal item was not derived")
        _assert_session_state(session)
        proposed = validate_checkpoint(
            dict(appender(
                checkpoint, terminal,
                manifest=session.manifest,
                manifest_digest=session.manifest_digest,
                batch_root=session.batch.path,
                inspect_candidate_fn=session.inspect_candidate,
            )),
            session.manifest,
            session.manifest_digest,
        )
        _assert_session_state(session)
        durable, durable_identity = _load_checkpoint_with_identity(
            session.descriptor, session.manifest, session.manifest_digest,
            session.inspect_candidate, verify_candidates=False,
        )
        if (
            durable_identity != session.checkpoint_identity
            or durable != proposed
        ):
            raise _fail("checkpoint appender did not durably advance batch state")
        if session.verified_checkpoint_digest != durable["checkpoint_digest"]:
            _verify_candidates(
                durable, session.inspect_candidate, session=session
            )
            session.verified_checkpoint_digest = durable["checkpoint_digest"]
        checkpoint = durable
        _assert_session_state(session)
    return _persist_or_verify_final_at(session, checkpoint)


def load_final_batch_readback(
    batch_id: str, *, batch_root: Path | None = None
) -> dict[str, Any] | None:
    """Strictly load the fixed final-readback file when it is present."""

    with _LOCK:
        batch = _batch_directory(batch_id, batch_root)
        with _locked_batch(batch) as descriptor:
            try:
                os.stat(
                    FINAL_READBACK_NAME,
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return None
            try:
                value = _read_json_at(
                    descriptor,
                    FINAL_READBACK_NAME,
                    "final batch readback",
                )
                return validate_final_batch_readback(value)
            except (BatchIngestionError, ContractError, OSError) as exc:
                if isinstance(exc, BatchIngestionError):
                    raise
                raise _fail("final batch readback could not be loaded", exc)
