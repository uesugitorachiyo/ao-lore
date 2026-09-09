"""Crash-safe orchestration for the fixed private English-OCR UAT."""

from __future__ import annotations

import copy
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ._strict_io import ContractError, parse_strict_json
from .benchmark import canonical_digest
from .batch_ingestion import (
    load_final_batch_readback,
    load_or_initialize_checkpoint,
    validate_batch_manifest,
    validate_checkpoint,
    validate_final_batch_readback,
)
from .candidates import load_verified_candidate
from .model_roles import ActivatedOcrParser, RoleConfigError, activate_ocr_parser
from .ocr_contracts import AUTHORITY_FIELDS, OCR_POLICY_DIGEST, validate_uat_readback
from .ocr_raster import (
    OCR_RASTER_CONFIGURATION_DIGEST,
    RasterBundle,
    RasterPageOutput,
    ReviewedOcrSource,
    load_raster_bundle,
    load_raster_bundle_for_cleanup,
    render_in_ocr_sandbox,
    rasterize_ocr_source,
    validate_raster_bundle,
)
from .paddle_ocr import OCR_WORKER_CONFIGURATION_DIGEST
from .home import runtime_home
from .ocr_runtime import validate_ocr_runtime_tree_digest
from .private_document_uat import (
    PrivateDocumentCampaignPolicy,
    PrivateDocumentPreparedRun,
    PrivateDocumentUatDependencies,
    private_document_policy_digest,
    private_document_state_body,
    run_private_document_uat,
)
from .private_pdf_uat import (
    UBUNTU_PDF_SEED,
    _default_brain_snapshot,
    _read_reviewed_source,
    _verify_pdf,
)


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ITEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_SOURCE_BYTES = 50 * 1024 * 1024
_CLEANUP_MAX_ENTRIES = 20_000
_CLEANUP_MAX_DEPTH = 32
_BWRAP = "/usr/bin/bwrap"
_OCR_PHASE_CONTRACT_PATH = "/launch/worker-contract.json"
_OCR_PHASE_RESOURCE_LIMITS = {
    "cpu_seconds": 3600,
    "address_space_bytes": 64 * 1024 * 1024 * 1024,
    "data_bytes": 32 * 1024 * 1024 * 1024,
    "file_size_bytes": 128 * 1024 * 1024,
    "open_files": 256,
    "processes": 512,
}
_OCR_PHASE_DENIED_NETWORK_SYSCALLS = (
    "connect", "sendto", "sendmsg", "sendmmsg",
)
_NVIDIA_CAP_NAME = re.compile(r"^nvidia-cap[0-9]+$")
_LEGACY_OCR_RASTER_CONFIGURATION_DIGEST = (
    "sha256:54a5e18587bd3a2d63d748769aba776dc702a239d47a5885b86a0276c3c8c93a"
)


class PrivateOcrUatError(ContractError):
    """Raised when the private OCR UAT contract drifts."""


def _fail(message: str = "private OCR UAT contract is invalid") -> PrivateOcrUatError:
    return PrivateOcrUatError(message)


def _digest(value: Any) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _fail()
    return value


def _validate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != {"corpus_id", "documents"}:
        raise _fail()
    if value["corpus_id"] != "private-ocr-english-v0-1":
        raise _fail()
    documents = value["documents"]
    if type(documents) is not list or len(documents) != 4:
        raise _fail()
    detached: list[dict[str, str]] = []
    seen: set[str] = set()
    for document in documents:
        if type(document) is not dict or set(document) != {"item_id", "source_digest"}:
            raise _fail()
        item_id = document["item_id"]
        if type(item_id) is not str or _ITEM.fullmatch(item_id) is None or item_id in seen:
            raise _fail()
        seen.add(item_id)
        detached.append({"item_id": item_id, "source_digest": _digest(document["source_digest"])})
    return {"corpus_id": value["corpus_id"], "documents": detached}


def _validate_qualification_binding(value: Mapping[str, Any]) -> dict[str, str]:
    keys = {
        "qualification_digest",
        "runtime_digest",
        "model_set_digest",
        "selected_candidate_id",
        "campaign_origin_digest",
    }
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    selected = value["selected_candidate_id"]
    if type(selected) is not str or selected not in {
        "candidate-1", "candidate-2", "candidate-3"
    }:
        raise _fail()
    return {
        "qualification_digest": _digest(value["qualification_digest"]),
        "runtime_digest": _digest(value["runtime_digest"]),
        "model_set_digest": _digest(value["model_set_digest"]),
        "selected_candidate_id": selected,
        "campaign_origin_digest": _digest(value["campaign_origin_digest"]),
    }


def _validate_calibration(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or set(value) != {"attempts_per_item", "aggregate_digest"}
        or type(value["attempts_per_item"]) is not int
        or value["attempts_per_item"] != 2
    ):
        raise _fail()
    return {"attempts_per_item": 2, "aggregate_digest": _digest(value["aggregate_digest"])}


PRIVATE_OCR_UAT_POLICY = PrivateDocumentCampaignPolicy(
    format_id="ocr",
    corpus_id="private-ocr-english-v0-1",
    media_type="application/pdf",
    extension=".pdf",
    parser_id="paddleocr",
    parser_version="3.7.0",
    worker_module="ao_lore.private_ocr_uat_worker",
    calibration_worker_module="ao_lore.private_ocr_calibration_worker",
    evidence_namespace="private-ocr-uat",
    validate_manifest=_validate_manifest,
    validate_qualification=_validate_qualification_binding,
    validate_calibration=_validate_calibration,
)


@dataclass(frozen=True)
class OcrUatDependencies:
    """Injectable, fully closed shared UAT dependencies."""

    shared: PrivateDocumentUatDependencies


@dataclass(frozen=True)
class PreparedOcrUat:
    """Minimum retained identity needed to publish terminal OCR evidence."""

    runtime_root: Path
    batch_id: str
    batch_manifest_digest: str
    corpus_digest: str
    qualification_digest: str
    campaign_origin_digest: str


@dataclass(frozen=True)
class OcrActivationInputs:
    """Exact reviewed inputs needed to reopen one OCR parser activation."""

    qualification_body: bytes
    qualification_digest: str
    runtime_digest: str
    corpus_digest: str
    corpus_configuration_digest: str
    oracle_digest: str
    model_digests: tuple[str, str, str]
    selected_result_digest: str


@dataclass(frozen=True)
class PreparedOcrCampaignInputs:
    """Path-free campaign origin plus exact private raster bundles."""

    corpus_manifest: Mapping[str, Any]
    corpus_digest: str
    campaign_origin_digest: str
    sources: tuple[Mapping[str, Any], ...]
    rasters: tuple[RasterBundle, ...]


@dataclass(frozen=True)
class PrivateOcrPreparedRun:
    """Exact run-owned source stage and retained control identity."""

    runtime_root: Path
    staging_name: str
    staging_identity: tuple[int, int]
    control_path: Path
    control_identity: tuple[int, int]
    batch_id: str
    batch_manifest: Mapping[str, Any]
    batch_manifest_digest: str
    corpus_manifest: Mapping[str, Any]
    corpus_digest: str
    qualification_digest: str
    campaign_origin_digest: str
    sources: tuple[Mapping[str, Any], ...]

    @property
    def corpus_root(self) -> Path:
        return self.runtime_root / "private-ocr" / "corpus"


def _canonical_body(value: Mapping[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value, allow_nan=False, ensure_ascii=True, sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _fail("private OCR retained evidence is invalid") from exc


def _open_child(parent: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    return os.open(
        name,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
    )


def _open_corpus_lock(private: int, *, exclusive: bool) -> int:
    descriptor = os.open(
        ".corpus.lock",
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=private,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != 0
        ):
            raise _fail("private OCR corpus lock is invalid")
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _write_owned(parent: int, name: str, body: bytes) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent,
    )
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise _fail("private OCR retained evidence write failed")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_cleanup_intent(parent: int, body: bytes) -> None:
    """Complete one exact-prefix staging file and atomically publish it."""

    partial = "cleanup-intent.partial"
    final = "cleanup-intent.json"
    entries = set(os.listdir(parent))
    if final in entries:
        if partial in entries or _read_stable_at(parent, final, 4 * 1024 * 1024) != body:
            raise _fail("private OCR cleanup intent is invalid")
        return
    if partial not in entries:
        descriptor = os.open(
            partial,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent,
        )
    else:
        descriptor = os.open(
            partial, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent,
        )
    try:
        before = os.fstat(descriptor)
        public = os.stat(partial, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > len(body)
            or (before.st_dev, before.st_ino) != (public.st_dev, public.st_ino)
        ):
            raise _fail("private OCR cleanup intent is invalid")
        current = b""
        while len(current) < before.st_size:
            chunk = os.pread(
                descriptor, min(64 * 1024, before.st_size - len(current)),
                len(current),
            )
            if not chunk:
                raise _fail("private OCR cleanup intent is invalid")
            current += chunk
        if current != body[: len(current)]:
            raise _fail("private OCR cleanup intent is invalid")
        os.lseek(descriptor, len(current), os.SEEK_SET)
        view = memoryview(body)[len(current):]
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise _fail("private OCR cleanup intent write failed")
            view = view[written:]
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        public_after = os.stat(partial, dir_fd=parent, follow_symlinks=False)
        if (
            (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or after.st_size != len(body)
            or (after.st_dev, after.st_ino)
            != (public_after.st_dev, public_after.st_ino)
        ):
            raise _fail("private OCR cleanup intent is invalid")
    finally:
        os.close(descriptor)
    _rename_noreplace(parent, partial, final)
    os.fsync(parent)


def _read_at(parent: int, name: str, maximum: int = 512 * 1024) -> bytes:
    descriptor = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent
    )
    try:
        info = os.fstat(descriptor)
        if not 0 <= info.st_size <= maximum or info.st_nlink != 1:
            raise _fail("private OCR retained evidence is invalid")
        body = b""
        while len(body) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(body)))
            if not chunk:
                break
            body += chunk
        after = os.fstat(descriptor)
        if len(body) > maximum or (
            info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size
        ) != (
            after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size
        ):
            raise _fail("private OCR retained evidence is invalid")
        return body
    finally:
        os.close(descriptor)


def _read_stable_at(parent: int, name: str, maximum: int = 512 * 1024) -> bytes:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
    ):
        raise _fail("private OCR state file is invalid")
    body = _read_at(parent, name, maximum)
    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (
        before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size
    ) != (
        after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size
    ):
        raise _fail("private OCR state file drifted")
    return body


def _unlink_exact(parent: int, name: str, body: bytes) -> None:
    reclaim = f".reclaim-{name}"
    entries = set(os.listdir(parent))
    if reclaim in entries:
        if name in entries:
            raise _fail("private OCR retained evidence is ambiguous")
    else:
        _rename_noreplace(parent, name, reclaim)
        os.fsync(parent)
    if _read_at(parent, reclaim) != body:
        raise _fail("private OCR retained evidence is invalid")
    os.unlink(reclaim, dir_fd=parent)
    os.fsync(parent)


def _rename_noreplace(parent: int, source: str, destination: str) -> None:
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    if renameat2(
        parent, os.fsencode(source), parent, os.fsencode(destination), 1
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _validate_activation_inputs(
    value: Any,
) -> tuple[OcrActivationInputs, ActivatedOcrParser, dict[str, Any]]:
    if type(value) is not OcrActivationInputs:
        raise _fail("private OCR activation inputs are invalid")
    if (
        type(value.qualification_body) is not bytes
        or not value.qualification_body
        or len(value.qualification_body) > 1024 * 1024
        or type(value.model_digests) is not tuple
        or len(value.model_digests) != 3
        or len(set(value.model_digests)) != 3
    ):
        raise _fail("private OCR activation inputs are invalid")
    for digest in (
        value.qualification_digest,
        value.runtime_digest,
        value.corpus_digest,
        value.corpus_configuration_digest,
        value.oracle_digest,
        *value.model_digests,
        value.selected_result_digest,
    ):
        _digest(digest)
    if (
        "sha256:" + hashlib.sha256(value.qualification_body).hexdigest()
        != value.qualification_digest
    ):
        raise _fail("private OCR activation qualification drifted")
    try:
        activation = activate_ocr_parser(
            value.qualification_body,
            qualification_digest=value.qualification_digest,
            runtime_digest=value.runtime_digest,
            corpus_digest=value.corpus_digest,
            corpus_configuration_digest=value.corpus_configuration_digest,
            oracle_digest=value.oracle_digest,
            model_digests=value.model_digests,
            selected_result_digest=value.selected_result_digest,
        )
    except RoleConfigError as exc:
        raise _fail("private OCR activation evidence is invalid") from exc
    record = {
        "schema_version": "ao.lore.private-ocr-activation.v0.1",
        "qualification_digest": value.qualification_digest,
        "runtime_digest": value.runtime_digest,
        "corpus_digest": value.corpus_digest,
        "corpus_configuration_digest": value.corpus_configuration_digest,
        "oracle_digest": value.oracle_digest,
        "model_digests": list(value.model_digests),
        "selected_candidate_id": activation.candidate_id,
        "selected_model_digest": activation.model_set_digest,
        "selected_result_digest": value.selected_result_digest,
        "capability": activation.capability,
        "parser_id": activation.parser_id,
        "parser_version": activation.parser_version,
        "provider_enabled": False,
        "fallback_enabled": False,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    return value, activation, record


def _activation_bodies(
    inputs: OcrActivationInputs,
) -> tuple[ActivatedOcrParser, bytes, bytes, bytes]:
    _validated, activation, record = _validate_activation_inputs(inputs)
    activation_body = _canonical_body(record)
    intent_body = _canonical_body({
        "schema_version": "ao.lore.private-ocr-activation-intent.v0.1",
        "qualification_digest": inputs.qualification_digest,
        "activation_digest": "sha256:" + hashlib.sha256(activation_body).hexdigest(),
        "authority": False,
    })
    return activation, inputs.qualification_body, activation_body, intent_body


def _validate_activation_directory(
    parent: int,
    name: str,
    qualification_body: bytes,
    activation_body: bytes,
) -> None:
    directory = _open_child(parent, name, create=False)
    try:
        if set(os.listdir(directory)) != {"qualification.json", "activation.json"}:
            raise _fail("private OCR activation state is invalid")
        if (
            _read_stable_at(directory, "qualification.json", 1024 * 1024)
            != qualification_body
            or _read_stable_at(directory, "activation.json") != activation_body
        ):
            raise _fail("private OCR activation state drifted")
    finally:
        os.close(directory)


def persist_private_ocr_activation(
    *, runtime_root: Path, inputs: OcrActivationInputs
) -> ActivatedOcrParser:
    """Publish and reopen the exact reviewed OCR activation transaction."""

    activation, qualification_body, activation_body, intent_body = (
        _activation_bodies(inputs)
    )
    runtime = os.open(
        Path(runtime_root),
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    evidence = namespace = pending_descriptor = None
    try:
        evidence = _open_child(runtime, "evidence", create=True)
        namespace = _open_child(evidence, "private-ocr-activation", create=True)
        entries = set(os.listdir(namespace))
        if not entries <= {"current", ".pending"} or entries == {"current", ".pending"}:
            raise _fail("private OCR activation contains foreign state")
        if "current" in entries:
            _validate_activation_directory(
                namespace, "current", qualification_body, activation_body
            )
            return activation
        if ".pending" not in entries:
            os.mkdir(".pending", 0o700, dir_fd=namespace)
            os.fsync(namespace)
        pending_descriptor = _open_child(namespace, ".pending", create=False)
        allowed = {"intent.json", "qualification.json", "activation.json"}
        staged = set(os.listdir(pending_descriptor))
        if not staged <= allowed:
            raise _fail("private OCR activation staging contains foreign state")
        terminal = staged == {"qualification.json", "activation.json"}
        if staged and not terminal:
            if "intent.json" not in staged or _read_at(
                pending_descriptor, "intent.json"
            ) != intent_body:
                raise _fail("private OCR activation staging is unauthenticated")
        elif not staged:
            _write_owned(pending_descriptor, "intent.json", intent_body)
            staged.add("intent.json")
        for filename, body in (
            ("qualification.json", qualification_body),
            ("activation.json", activation_body),
        ):
            if filename in staged:
                if _read_at(pending_descriptor, filename, 1024 * 1024) != body:
                    raise _fail("private OCR activation staging drifted")
            else:
                _write_owned(pending_descriptor, filename, body)
        if "intent.json" in set(os.listdir(pending_descriptor)):
            _unlink_exact(pending_descriptor, "intent.json", intent_body)
        os.fsync(pending_descriptor)
        _rename_noreplace(namespace, ".pending", "current")
        os.fsync(namespace)
        return activation
    finally:
        for descriptor in (pending_descriptor, namespace, evidence, runtime):
            if descriptor is not None:
                os.close(descriptor)


def load_private_ocr_activation(
    *, runtime_root: Path, inputs: OcrActivationInputs
) -> ActivatedOcrParser:
    """Revalidate retained activation bytes against exact reviewed inputs."""

    activation, qualification_body, activation_body, _intent = _activation_bodies(inputs)
    runtime = os.open(
        Path(runtime_root),
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    evidence = namespace = None
    try:
        evidence = _open_child(runtime, "evidence", create=False)
        namespace = _open_child(evidence, "private-ocr-activation", create=False)
        if set(os.listdir(namespace)) != {"current"}:
            raise _fail("private OCR activation state is invalid")
        _validate_activation_directory(
            namespace, "current", qualification_body, activation_body
        )
        return activation
    except (ContractError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivateOcrUatError):
            raise
        raise _fail("private OCR activation state is invalid") from exc
    finally:
        for descriptor in (namespace, evidence, runtime):
            if descriptor is not None:
                os.close(descriptor)


def load_private_ocr_activation_inputs(*, runtime_root: Path) -> OcrActivationInputs:
    """Reconstruct exact activation inputs from the canonical retained transaction."""

    root = Path(runtime_root)
    runtime = os.open(
        root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    evidence = namespace = current = None
    try:
        evidence = _open_child(runtime, "evidence", create=False)
        namespace = _open_child(evidence, "private-ocr-activation", create=False)
        if set(os.listdir(namespace)) != {"current"}:
            raise _fail("private OCR activation state is invalid")
        current = _open_child(namespace, "current", create=False)
        if set(os.listdir(current)) != {"qualification.json", "activation.json"}:
            raise _fail("private OCR activation state is invalid")
        qualification_body = _read_at(current, "qualification.json", 1024 * 1024)
        activation_body = _read_at(current, "activation.json", 1024 * 1024)
        record = parse_strict_json(activation_body, "private OCR activation record")
        if type(record) is not dict:
            raise _fail("private OCR activation state is invalid")
        model_digests = record.get("model_digests")
        if type(model_digests) is not list:
            raise _fail("private OCR activation state is invalid")
        inputs = OcrActivationInputs(
            qualification_body=qualification_body,
            qualification_digest=record.get("qualification_digest"),
            runtime_digest=record.get("runtime_digest"),
            corpus_digest=record.get("corpus_digest"),
            corpus_configuration_digest=record.get("corpus_configuration_digest"),
            oracle_digest=record.get("oracle_digest"),
            model_digests=tuple(model_digests),
            selected_result_digest=record.get("selected_result_digest"),
        )
        _activation, expected_qualification, expected_activation, _intent = (
            _activation_bodies(inputs)
        )
        if (
            qualification_body != expected_qualification
            or activation_body != expected_activation
        ):
            raise _fail("private OCR activation state drifted")
        return inputs
    except (ContractError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivateOcrUatError):
            raise
        raise _fail("private OCR activation state is invalid") from exc
    finally:
        for descriptor in (current, namespace, evidence, runtime):
            if descriptor is not None:
                os.close(descriptor)


def _validate_prepared(value: Any) -> PreparedOcrUat:
    if type(value) is not PreparedOcrUat:
        raise _fail("private OCR prepared UAT is invalid")
    if (
        not isinstance(value.runtime_root, Path)
        or type(value.batch_id) is not str
        or re.fullmatch(r"batch-[a-z0-9][a-z0-9-]{0,62}", value.batch_id) is None
    ):
        raise _fail("private OCR prepared UAT is invalid")
    for digest in (
        value.batch_manifest_digest, value.corpus_digest,
        value.qualification_digest, value.campaign_origin_digest,
    ):
        _digest(digest)
    return value


def _validate_retained_calibration_result(
    value: Any, *, qualification_digest: str, campaign_origin_digest: str,
) -> dict[str, Any]:
    keys = {
        "schema_version", "attempts_per_item", "aggregate_digest",
        "decision", "items", *AUTHORITY_FIELDS,
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or value["schema_version"]
        != "ao.lore.private-ocr-calibration-result.v0.1"
        or type(value["attempts_per_item"]) is not int
        or value["attempts_per_item"] != 2
        or value["decision"] not in {"hold", "candidate_change", "investigate"}
        or any(value[field] is not False for field in AUTHORITY_FIELDS)
        or type(value["items"]) is not list
        or len(value["items"]) != 4
    ):
        raise _fail("private OCR retained calibration is invalid")
    aggregate_digest = _digest(value["aggregate_digest"])
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in value["items"]:
        if (
            type(item) is not dict
            or set(item) != {
                "item_id", "raster_manifest_digest", "page_count",
                "detected_lines", "attempt_digests", "identical",
            }
            or type(item["item_id"]) is not str
            or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item["item_id"]) is None
            or item["item_id"] in seen_ids
            or type(item["page_count"]) is not int
            or not 1 <= item["page_count"] <= 100
            or type(item["detected_lines"]) is not int
            or not 0 <= item["detected_lines"] <= 1_000_000
            or type(item["attempt_digests"]) is not list
            or len(item["attempt_digests"]) != 2
            or type(item["identical"]) is not bool
        ):
            raise _fail("private OCR retained calibration is invalid")
        seen_ids.add(item["item_id"])
        attempt_digests = [_digest(digest) for digest in item["attempt_digests"]]
        identical = len(set(attempt_digests)) == 1
        if item["identical"] is not identical:
            raise _fail("private OCR retained calibration is invalid")
        items.append({
            "item_id": item["item_id"],
            "raster_manifest_digest": _digest(item["raster_manifest_digest"]),
            "page_count": item["page_count"],
            "detected_lines": item["detected_lines"],
            "attempt_digests": attempt_digests,
            "identical": identical,
        })
    if value["decision"] == "hold" and any(
        not item["identical"] or item["detected_lines"] == 0 for item in items
    ):
        raise _fail("private OCR retained calibration is invalid")
    detached = {
        "schema_version": value["schema_version"],
        "attempts_per_item": 2,
        "aggregate_digest": aggregate_digest,
        "decision": value["decision"],
        "items": items,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    expected_aggregate = canonical_digest({
        "schema_version": "ao.lore.private-ocr-calibration-aggregate.v0.1",
        "attempts_per_item": 2,
        "items": items,
        "decision": value["decision"],
        "qualification_digest": _digest(qualification_digest),
        "campaign_origin_digest": _digest(campaign_origin_digest),
        **{field: False for field in AUTHORITY_FIELDS},
    })
    if aggregate_digest != expected_aggregate:
        raise _fail("private OCR retained calibration is invalid")
    return detached


def persist_private_ocr_evidence(
    prepared: PreparedOcrUat,
    report: Mapping[str, Any],
    *,
    calibration_result: Mapping[str, Any],
    brain_before: str,
    brain_after_work: str,
    brain_after_cleanup: str,
) -> str:
    """Atomically publish the sole two-file terminal OCR evidence directory."""

    run = _validate_prepared(prepared)
    if not (
        _digest(brain_before)
        == _digest(brain_after_work)
        == _digest(brain_after_cleanup)
    ):
        raise _fail("private OCR retained brain binding drifted")
    validated = validate_uat_readback(copy.deepcopy(report))
    calibration = _validate_retained_calibration_result(
        copy.deepcopy(calibration_result),
        qualification_digest=run.qualification_digest,
        campaign_origin_digest=run.campaign_origin_digest,
    )
    if (
        validated["qualification_digest"] != run.qualification_digest
        or validated["corpus_digest"] != run.corpus_digest
        or validated["brain_before_digest"] != brain_before
        or validated["brain_after_work_digest"] != brain_after_work
        or validated["brain_after_cleanup_digest"] != brain_after_cleanup
        or calibration["attempts_per_item"]
        != validated["calibration_attempts_per_item"]
        or calibration["aggregate_digest"] != validated["aggregate_digest"]
        or calibration["decision"] != validated["decision"]
    ):
        raise _fail("private OCR retained evidence binding drifted")
    readback_body = _canonical_body(validated)
    readback_digest = "sha256:" + hashlib.sha256(readback_body).hexdigest()
    cleaned_body = _canonical_body({
        "schema_version": "ao.lore.private-ocr-uat-cleaned-state.v0.1",
        "batch_id": run.batch_id,
        "batch_manifest_digest": run.batch_manifest_digest,
        "corpus_digest": run.corpus_digest,
        "qualification_digest": run.qualification_digest,
        "campaign_origin_digest": run.campaign_origin_digest,
        "readback_digest": readback_digest,
        "brain_before_digest": brain_before,
        "brain_after_work_digest": brain_after_work,
        "brain_after_cleanup_digest": brain_after_cleanup,
        "calibration_result": calibration,
        "lifecycle_status": "cleaned",
        "authority": False,
    })
    intent_body = _canonical_body({
        "schema_version": "ao.lore.private-ocr-uat-evidence-intent.v0.1",
        "batch_id": run.batch_id,
        "readback_digest": readback_digest,
        "cleaned_digest": "sha256:" + hashlib.sha256(cleaned_body).hexdigest(),
        "authority": False,
    })
    runtime = os.open(
        run.runtime_root,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    evidence = namespace = pending_descriptor = None
    pending = f".pending-{run.batch_id}"
    try:
        evidence = _open_child(runtime, "evidence", create=True)
        namespace = _open_child(evidence, "private-ocr-uat", create=True)
        names = set(os.listdir(namespace))
        foreign = {name for name in names if name not in {run.batch_id, pending}}
        if foreign:
            raise _fail("private OCR retained evidence contains foreign state")
        if run.batch_id in names:
            if pending in names:
                raise _fail("private OCR retained evidence is ambiguous")
            final = _open_child(namespace, run.batch_id, create=False)
            try:
                if set(os.listdir(final)) != {
                    "private-ocr-uat-readback.json", "cleaned-state.json"
                }:
                    raise _fail("private OCR retained evidence collision")
                if (
                    _read_at(final, "private-ocr-uat-readback.json") != readback_body
                    or _read_at(final, "cleaned-state.json") != cleaned_body
                ):
                    raise _fail("private OCR retained evidence collision")
                return readback_digest
            finally:
                os.close(final)
        if pending not in names:
            os.mkdir(pending, 0o700, dir_fd=namespace)
            os.fsync(namespace)
        pending_descriptor = _open_child(namespace, pending, create=False)
        allowed = {
            "evidence-intent.json", "private-ocr-uat-readback.json",
            "cleaned-state.json", ".reclaim-evidence-intent.json",
        }
        staged = set(os.listdir(pending_descriptor))
        if not staged <= allowed:
            raise _fail("private OCR retained evidence staging contains foreign state")
        expected = (
            ("evidence-intent.json", intent_body),
            ("private-ocr-uat-readback.json", readback_body),
            ("cleaned-state.json", cleaned_body),
        )
        reclaim_intent = ".reclaim-evidence-intent.json"
        terminal_names = {"private-ocr-uat-readback.json", "cleaned-state.json"}
        if reclaim_intent in staged:
            if "evidence-intent.json" in staged:
                raise _fail("private OCR retained evidence is ambiguous")
            if _read_at(pending_descriptor, reclaim_intent) != intent_body:
                raise _fail("private OCR retained evidence staging drifted")
            os.unlink(reclaim_intent, dir_fd=pending_descriptor)
            os.fsync(pending_descriptor)
            staged.remove(reclaim_intent)
        terminal_staged = staged == terminal_names
        if terminal_staged:
            if (
                _read_at(pending_descriptor, "private-ocr-uat-readback.json")
                != readback_body
                or _read_at(pending_descriptor, "cleaned-state.json") != cleaned_body
            ):
                raise _fail("private OCR retained evidence staging drifted")
        elif staged and "evidence-intent.json" not in staged:
            raise _fail("private OCR retained evidence staging is unauthenticated")
        if not terminal_staged:
            for name, body in expected:
                if name in staged:
                    if _read_at(pending_descriptor, name) != body:
                        raise _fail("private OCR retained evidence staging drifted")
                else:
                    _write_owned(pending_descriptor, name, body)
                    staged.add(name)
            if staged != {
                "evidence-intent.json", "private-ocr-uat-readback.json",
                "cleaned-state.json",
            }:
                raise _fail("private OCR retained evidence staging drifted")
            _unlink_exact(pending_descriptor, "evidence-intent.json", intent_body)
        os.fsync(pending_descriptor)
        _rename_noreplace(namespace, pending, run.batch_id)
        os.fsync(namespace)
        return readback_digest
    finally:
        for descriptor in (pending_descriptor, namespace, evidence, runtime):
            if descriptor is not None:
                os.close(descriptor)


def load_private_ocr_evidence(prepared: PreparedOcrUat) -> dict[str, object]:
    """Reopen and cross-bind one completed two-file private OCR evidence set."""

    run = _validate_prepared(prepared)
    runtime = os.open(
        run.runtime_root,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    evidence = namespace = final = None
    try:
        evidence = _open_child(runtime, "evidence", create=False)
        namespace = _open_child(evidence, "private-ocr-uat", create=False)
        if set(os.listdir(namespace)) != {run.batch_id}:
            raise _fail("private OCR retained evidence contains foreign state")
        final = _open_child(namespace, run.batch_id, create=False)
        if set(os.listdir(final)) != {
            "private-ocr-uat-readback.json", "cleaned-state.json"
        }:
            raise _fail("private OCR retained evidence is invalid")
        readback_body = _read_at(final, "private-ocr-uat-readback.json")
        cleaned_body = _read_at(final, "cleaned-state.json")
        readback = validate_uat_readback(
            parse_strict_json(readback_body, "private OCR retained readback")
        )
        if _canonical_body(readback) != readback_body:
            raise _fail("private OCR retained readback bytes drifted")
        cleaned = parse_strict_json(cleaned_body, "private OCR cleaned state")
        keys = {
            "schema_version", "batch_id", "batch_manifest_digest",
            "corpus_digest", "qualification_digest", "campaign_origin_digest",
            "readback_digest", "brain_before_digest", "brain_after_work_digest",
            "brain_after_cleanup_digest", "calibration_result",
            "lifecycle_status", "authority",
        }
        if type(cleaned) is not dict or set(cleaned) != keys:
            raise _fail("private OCR cleaned state is invalid")
        expected = {
            "schema_version": "ao.lore.private-ocr-uat-cleaned-state.v0.1",
            "batch_id": run.batch_id,
            "batch_manifest_digest": run.batch_manifest_digest,
            "corpus_digest": run.corpus_digest,
            "qualification_digest": run.qualification_digest,
            "campaign_origin_digest": run.campaign_origin_digest,
            "readback_digest": "sha256:" + hashlib.sha256(readback_body).hexdigest(),
            "brain_before_digest": readback["brain_before_digest"],
            "brain_after_work_digest": readback["brain_after_work_digest"],
            "brain_after_cleanup_digest": readback["brain_after_cleanup_digest"],
            "calibration_result": _validate_retained_calibration_result(
                cleaned["calibration_result"],
                qualification_digest=run.qualification_digest,
                campaign_origin_digest=run.campaign_origin_digest,
            ),
            "lifecycle_status": "cleaned",
            "authority": False,
        }
        if (
            expected["calibration_result"]["attempts_per_item"]
            != readback["calibration_attempts_per_item"]
            or expected["calibration_result"]["aggregate_digest"]
            != readback["aggregate_digest"]
            or expected["calibration_result"]["decision"] != readback["decision"]
            or cleaned != expected
            or _canonical_body(cleaned) != cleaned_body
        ):
            raise _fail("private OCR cleaned state binding drifted")
        return readback
    except (ContractError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivateOcrUatError):
            raise
        raise _fail("private OCR retained evidence is invalid") from exc
    finally:
        for descriptor in (final, namespace, evidence, runtime):
            if descriptor is not None:
                os.close(descriptor)


def _validate_corpus_manifest(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "corpus_id", "format_id", "media_type",
        "parser_id", "parser_version", "documents", *AUTHORITY_FIELDS,
    }
    if type(value) is not dict or set(value) != keys:
        raise _fail("private OCR corpus manifest is invalid")
    if (
        value["schema_version"] != "ao.lore.private-ocr-uat-corpus.v0.1"
        or value["corpus_id"] != "private-ocr-english-v0-1"
        or value["format_id"] != "ocr"
        or value["media_type"] != "application/pdf"
        or value["parser_id"] != "paddle-ocr-english"
        or value["parser_version"] != "0.1.0"
        or any(value[field] is not False for field in AUTHORITY_FIELDS)
    ):
        raise _fail("private OCR corpus manifest is invalid")
    documents = value["documents"]
    if type(documents) is not list or len(documents) != 4:
        raise _fail("private OCR corpus manifest is invalid")
    expected_ids = [seed.item_id for seed in UBUNTU_PDF_SEED]
    detached: list[dict[str, Any]] = []
    for index, document in enumerate(documents):
        item_id = expected_ids[index]
        if (
            type(document) is not dict
            or set(document) != {
                "item_id", "file", "media_type", "source_digest", "size"
            }
            or document["item_id"] != item_id
            or document["file"] != f"input/{item_id}.pdf"
            or document["media_type"] != "application/pdf"
            or type(document["size"]) is not int
            or not 1 <= document["size"] <= _MAX_SOURCE_BYTES
        ):
            raise _fail("private OCR corpus manifest is invalid")
        detached.append({
            "item_id": item_id,
            "file": document["file"],
            "media_type": "application/pdf",
            "source_digest": _digest(document["source_digest"]),
            "size": document["size"],
        })
    return {
        "schema_version": value["schema_version"],
        "corpus_id": value["corpus_id"],
        "format_id": value["format_id"],
        "media_type": value["media_type"],
        "parser_id": value["parser_id"],
        "parser_version": value["parser_version"],
        "documents": detached,
        **{field: False for field in AUTHORITY_FIELDS},
    }


def _load_private_ocr_corpus_at(private: int) -> dict[str, Any]:
    corpus = _open_child(private, "corpus", create=False)
    inputs = None
    try:
        if set(os.listdir(corpus)) != {"manifest.json", "input"}:
            raise _fail("private OCR corpus contains foreign state")
        manifest_body = _read_stable_at(corpus, "manifest.json")
        manifest = _validate_corpus_manifest(
            parse_strict_json(manifest_body, "private OCR corpus manifest")
        )
        if _canonical_body(manifest) != manifest_body:
            raise _fail("private OCR corpus manifest bytes drifted")
        inputs = _open_child(corpus, "input", create=False)
        expected = {f"{item['item_id']}.pdf" for item in manifest["documents"]}
        if set(os.listdir(inputs)) != expected:
            raise _fail("private OCR corpus inputs drifted")
        for item in manifest["documents"]:
            body = _read_stable_at(
                inputs, f"{item['item_id']}.pdf", _MAX_SOURCE_BYTES
            )
            if (
                len(body) != item["size"]
                or "sha256:" + hashlib.sha256(body).hexdigest()
                != item["source_digest"]
                or not body.startswith(b"%PDF-")
            ):
                raise _fail("private OCR corpus input drifted")
        return manifest
    finally:
        if inputs is not None:
            os.close(inputs)
        os.close(corpus)


def _load_private_ocr_corpus(
    *, runtime_root: Path, allow_prepared_run: bool,
) -> dict[str, Any]:
    runtime = os.open(
        Path(runtime_root),
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    private = lock = None
    try:
        private = _open_child(runtime, "private-ocr", create=False)
        lock = _open_corpus_lock(private, exclusive=False)
        allowed = {"corpus", "rasters", ".corpus.lock"}
        if allow_prepared_run:
            allowed.add("runs")
        if not set(os.listdir(private)) <= allowed or "corpus" not in os.listdir(private):
            raise _fail("private OCR corpus state is invalid")
        if "runs" in os.listdir(private):
            runs = _open_child(private, "runs", create=False)
            try:
                names = os.listdir(runs)
                if (
                    not 1 <= len(names) <= 1
                    or re.fullmatch(r"batch-private-ocr-[0-9a-f]{16}", names[0]) is None
                ):
                    raise _fail("private OCR prepared run state is invalid")
            finally:
                os.close(runs)
        return _load_private_ocr_corpus_at(private)
    except (ContractError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivateOcrUatError):
            raise
        raise _fail("private OCR corpus is invalid") from exc
    finally:
        if lock is not None:
            os.close(lock)
        if private is not None:
            os.close(private)
        os.close(runtime)


def prepare_private_ocr_corpus(*, runtime_root: Path) -> dict[str, Any]:
    """Atomically prepare the four fixed reviewed public PDFs for OCR UAT."""

    prepared: list[tuple[str, bytes, str]] = []
    for seed in UBUNTU_PDF_SEED:
        body = _read_reviewed_source(seed.source)
        _verify_pdf(body)
        prepared.append((
            seed.item_id, body, "sha256:" + hashlib.sha256(body).hexdigest()
        ))
    manifest = _validate_corpus_manifest({
        "schema_version": "ao.lore.private-ocr-uat-corpus.v0.1",
        "corpus_id": "private-ocr-english-v0-1",
        "format_id": "ocr",
        "media_type": "application/pdf",
        "parser_id": "paddle-ocr-english",
        "parser_version": "0.1.0",
        "documents": [
            {
                "item_id": item_id,
                "file": f"input/{item_id}.pdf",
                "media_type": "application/pdf",
                "source_digest": digest,
                "size": len(body),
            }
            for item_id, body, digest in prepared
        ],
        **{field: False for field in AUTHORITY_FIELDS},
    })
    manifest_body = _canonical_body(manifest)
    intent_body = _canonical_body({
        "schema_version": "ao.lore.private-ocr-uat-corpus-intent.v0.1",
        "manifest_digest": canonical_digest(manifest),
        "manifest": manifest,
        "authority": False,
    })
    runtime = os.open(
        Path(runtime_root),
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    private = lock = stage = inputs = None
    try:
        private = _open_child(runtime, "private-ocr", create=True)
        lock = _open_corpus_lock(private, exclusive=True)
        entries = set(os.listdir(private))
        if "corpus" in entries:
            if entries - {"corpus", "rasters", ".corpus.lock"}:
                raise _fail("private OCR corpus state contains foreign entries")
            loaded = _load_private_ocr_corpus_at(private)
            if loaded != manifest:
                raise _fail("private OCR corpus conflicts with reviewed sources")
            return loaded
        if entries - {".corpus.lock", ".corpus.partial"}:
            raise _fail("private OCR corpus preparation state is unresolved")
        if ".corpus.partial" not in entries:
            os.mkdir(".corpus.partial", 0o700, dir_fd=private)
            os.fsync(private)
        stage = _open_child(private, ".corpus.partial", create=False)
        staged = set(os.listdir(stage))
        reclaim_intent = ".reclaim-intent.json"
        if not staged <= {"intent.json", reclaim_intent, "input", "manifest.json"}:
            raise _fail("private OCR corpus staging contains foreign state")
        if reclaim_intent in staged:
            if "intent.json" in staged or _read_at(stage, reclaim_intent) != intent_body:
                raise _fail("private OCR corpus staging intent drifted")
            os.unlink(reclaim_intent, dir_fd=stage)
            os.fsync(stage)
            staged.remove(reclaim_intent)
        terminal_staged = staged == {"input", "manifest.json"}
        intent_present = "intent.json" in staged
        if staged and not terminal_staged:
            if not intent_present or _read_at(stage, "intent.json") != intent_body:
                raise _fail("private OCR corpus staging intent drifted")
        elif not staged:
            _write_owned(stage, "intent.json", intent_body)
            staged.add("intent.json")
            intent_present = True
        if "input" not in staged:
            os.mkdir("input", 0o700, dir_fd=stage)
            os.fsync(stage)
        inputs = _open_child(stage, "input", create=False)
        expected_names = {f"{item_id}.pdf" for item_id, _body, _digest_value in prepared}
        existing_names = set(os.listdir(inputs))
        if not existing_names <= expected_names:
            raise _fail("private OCR corpus staging inputs contain foreign state")
        for item_id, body, _digest_value in prepared:
            name = f"{item_id}.pdf"
            if name in existing_names:
                if _read_at(inputs, name, _MAX_SOURCE_BYTES) != body:
                    raise _fail("private OCR corpus staged input drifted")
            else:
                _write_owned(inputs, name, body)
        os.fsync(inputs)
        if "manifest.json" in staged:
            if _read_at(stage, "manifest.json") != manifest_body:
                raise _fail("private OCR corpus staged manifest drifted")
        else:
            _write_owned(stage, "manifest.json", manifest_body)
        os.fsync(stage)
        for seed, (_item_id, original, _digest_value) in zip(
            UBUNTU_PDF_SEED, prepared, strict=True
        ):
            if _read_reviewed_source(seed.source) != original:
                raise _fail("private OCR reviewed source drifted")
        if intent_present:
            _unlink_exact(stage, "intent.json", intent_body)
        _rename_noreplace(private, ".corpus.partial", "corpus")
        os.fsync(private)
        loaded = _load_private_ocr_corpus_at(private)
        if loaded != manifest:
            raise _fail("private OCR corpus publication drifted")
        return loaded
    except (ContractError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivateOcrUatError):
            raise
        raise _fail("private OCR corpus preparation failed") from exc
    finally:
        for descriptor in (inputs, stage, lock, private, runtime):
            if descriptor is not None:
                os.close(descriptor)


def load_private_ocr_corpus(*, runtime_root: Path) -> dict[str, Any]:
    return _load_private_ocr_corpus(
        runtime_root=runtime_root, allow_prepared_run=False
    )


def prepare_private_ocr_campaign_inputs(
    *,
    runtime_root: Path,
    activation_inputs: OcrActivationInputs,
    renderer_digest: str,
    renderer: Callable[[bytes, str, dict[str, object]], tuple[RasterPageOutput, ...]],
) -> PreparedOcrCampaignInputs:
    """Bind the fixed four-source corpus to immutable reviewed page bundles."""

    if not callable(renderer):
        raise _fail("private OCR campaign renderer is invalid")
    _digest(renderer_digest)
    activation = load_private_ocr_activation(
        runtime_root=Path(runtime_root), inputs=activation_inputs
    )
    corpus = load_private_ocr_corpus(runtime_root=Path(runtime_root))
    corpus_digest = canonical_digest(corpus)
    corpus_root = Path(runtime_root) / "private-ocr" / "corpus" / "input"
    raster_root = Path(runtime_root) / "private-ocr" / "rasters"
    expected_final = {
        f"raster-{item['item_id']}-{item['source_digest'][7:23]}"
        for item in corpus["documents"]
    }
    if raster_root.exists():
        entries = {item.name for item in raster_root.iterdir()}
        expected_durable = expected_final | {f".{name}.partial" for name in expected_final}
        if not entries <= expected_durable:
            raise _fail("private OCR raster state contains foreign entries")
    rasters: list[RasterBundle] = []
    sources: list[dict[str, Any]] = []
    for item in corpus["documents"]:
        source = ReviewedOcrSource(
            item["item_id"],
            corpus_root / f"{item['item_id']}.pdf",
            "application/pdf",
            item["source_digest"],
            True,
        )
        bundle = rasterize_ocr_source(
            source,
            state_root=raster_root,
            renderer_digest=renderer_digest,
            renderer=renderer,
        )
        rasters.append(bundle)
        sources.append({
            "item_id": item["item_id"],
            "source_digest": item["source_digest"],
            "raster_manifest_digest": bundle.manifest_digest,
            "page_count": len(bundle.pages),
            "selected_candidate_id": activation.candidate_id,
        })
    if {item.name for item in raster_root.iterdir()} != expected_final:
        raise _fail("private OCR raster state did not become terminal")
    origin = canonical_digest({
        "schema_version": "ao.lore.private-ocr-campaign-origin.v0.1",
        "corpus_digest": corpus_digest,
        "qualification_digest": activation.qualification_digest,
        "runtime_digest": activation.runtime_digest,
        "model_set_digest": activation.model_set_digest,
        "selected_candidate_id": activation.candidate_id,
        "selected_result_digest": activation.selected_result_digest,
        "renderer_digest": renderer_digest,
        "raster_configuration_digest": OCR_RASTER_CONFIGURATION_DIGEST,
        "sources": sources,
        **{field: False for field in AUTHORITY_FIELDS},
    })
    return PreparedOcrCampaignInputs(
        corpus_manifest=copy.deepcopy(corpus),
        corpus_digest=corpus_digest,
        campaign_origin_digest=origin,
        sources=tuple(copy.deepcopy(sources)),
        rasters=tuple(rasters),
    )


def load_private_ocr_campaign_inputs(
    *, runtime_root: Path,
) -> PreparedOcrCampaignInputs:
    """Reopen the exact four-item campaign from canonical retained state."""

    runtime = Path(runtime_root)
    activation_inputs = load_private_ocr_activation_inputs(runtime_root=runtime)
    activation = load_private_ocr_activation(
        runtime_root=runtime, inputs=activation_inputs
    )
    corpus = _load_private_ocr_corpus(
        runtime_root=runtime, allow_prepared_run=True
    )
    corpus_digest = canonical_digest(corpus)
    raster_root = runtime / "private-ocr" / "rasters"
    expected_names = [
        f"raster-{item['item_id']}-{item['source_digest'][7:23]}"
        for item in corpus["documents"]
    ]
    if (
        not raster_root.is_dir()
        or {path.name for path in raster_root.iterdir()} != set(expected_names)
    ):
        raise _fail("private OCR raster state is invalid")
    rasters = tuple(load_raster_bundle(raster_root / name) for name in expected_names)
    sources = tuple({
        "item_id": item["item_id"],
        "source_digest": item["source_digest"],
        "raster_manifest_digest": raster.manifest_digest,
        "page_count": len(raster.pages),
        "selected_candidate_id": activation.candidate_id,
    } for item, raster in zip(corpus["documents"], rasters, strict=True))
    origin = canonical_digest({
        "schema_version": "ao.lore.private-ocr-campaign-origin.v0.1",
        "corpus_digest": corpus_digest,
        "qualification_digest": activation.qualification_digest,
        "runtime_digest": activation.runtime_digest,
        "model_set_digest": activation.model_set_digest,
        "selected_candidate_id": activation.candidate_id,
        "selected_result_digest": activation.selected_result_digest,
        "renderer_digest": rasters[0].renderer_digest,
        "raster_configuration_digest": OCR_RASTER_CONFIGURATION_DIGEST,
        "sources": list(sources),
        **{field: False for field in AUTHORITY_FIELDS},
    })
    value = PreparedOcrCampaignInputs(
        corpus_manifest=copy.deepcopy(corpus), corpus_digest=corpus_digest,
        campaign_origin_digest=origin, sources=tuple(copy.deepcopy(sources)),
        rasters=rasters,
    )
    return _validate_campaign_inputs(
        value, runtime_root=runtime, activation_inputs=activation_inputs,
        allow_prepared_run=True,
    )


def _validate_campaign_inputs(
    value: Any,
    *,
    runtime_root: Path,
    activation_inputs: OcrActivationInputs,
    allow_prepared_run: bool = False,
) -> PreparedOcrCampaignInputs:
    if type(value) is not PreparedOcrCampaignInputs:
        raise _fail("private OCR campaign inputs are invalid")
    activation = load_private_ocr_activation(
        runtime_root=runtime_root, inputs=activation_inputs
    )
    corpus = _load_private_ocr_corpus(
        runtime_root=runtime_root, allow_prepared_run=allow_prepared_run
    )
    if (
        value.corpus_manifest != corpus
        or value.corpus_digest != canonical_digest(corpus)
        or type(value.sources) is not tuple
        or type(value.rasters) is not tuple
        or len(value.sources) != 4
        or len(value.rasters) != 4
    ):
        raise _fail("private OCR campaign inputs drifted")
    _digest(value.campaign_origin_digest)
    for document, source, raster in zip(
        corpus["documents"], value.sources, value.rasters, strict=True
    ):
        checked = validate_raster_bundle(raster)
        if (
            type(source) is not dict
            or set(source) != {
                "item_id", "source_digest", "raster_manifest_digest",
                "page_count", "selected_candidate_id",
            }
            or source != {
                "item_id": document["item_id"],
                "source_digest": document["source_digest"],
                "raster_manifest_digest": checked.manifest_digest,
                "page_count": len(checked.pages),
                "selected_candidate_id": activation.candidate_id,
            }
        ):
            raise _fail("private OCR campaign source binding drifted")
    expected_origin = canonical_digest({
        "schema_version": "ao.lore.private-ocr-campaign-origin.v0.1",
        "corpus_digest": value.corpus_digest,
        "qualification_digest": activation.qualification_digest,
        "runtime_digest": activation.runtime_digest,
        "model_set_digest": activation.model_set_digest,
        "selected_candidate_id": activation.candidate_id,
        "selected_result_digest": activation.selected_result_digest,
        "renderer_digest": value.rasters[0].renderer_digest,
        "raster_configuration_digest": OCR_RASTER_CONFIGURATION_DIGEST,
        "sources": list(value.sources),
        **{field: False for field in AUTHORITY_FIELDS},
    })
    if (
        any(raster.renderer_digest != value.rasters[0].renderer_digest for raster in value.rasters)
        or value.campaign_origin_digest != expected_origin
    ):
        raise _fail("private OCR campaign origin drifted")
    return value


def _run_record(run: PrivateOcrPreparedRun) -> dict[str, Any]:
    return {
        "schema_version": "ao.lore.private-ocr-prepared-run.v0.1",
        "staging_name": run.staging_name,
        "staging_identity": list(run.staging_identity),
        "control_identity": list(run.control_identity),
        "batch_id": run.batch_id,
        "batch_manifest": copy.deepcopy(run.batch_manifest),
        "batch_manifest_digest": run.batch_manifest_digest,
        "corpus_digest": run.corpus_digest,
        "qualification_digest": run.qualification_digest,
        "campaign_origin_digest": run.campaign_origin_digest,
        "sources": list(copy.deepcopy(run.sources)),
        **{field: False for field in AUTHORITY_FIELDS},
    }


def _run_from_record(
    record: Any, *, runtime_root: Path, corpus_manifest: Any,
) -> PrivateOcrPreparedRun:
    """Detach one path-free prepared-run record without reopening run state."""

    keys = {
        "schema_version", "staging_name", "staging_identity",
        "control_identity", "batch_id", "batch_manifest",
        "batch_manifest_digest", "corpus_digest", "qualification_digest",
        "campaign_origin_digest", "sources", *AUTHORITY_FIELDS,
    }
    if (
        type(record) is not dict
        or set(record) != keys
        or record["schema_version"] != "ao.lore.private-ocr-prepared-run.v0.1"
        or any(record[field] is not False for field in AUTHORITY_FIELDS)
        or type(record["staging_name"]) is not str
        or re.fullmatch(r"private-ocr-uat-[0-9a-f]{24}", record["staging_name"])
        is None
        or type(record["batch_id"]) is not str
        or re.fullmatch(r"batch-private-ocr-[0-9a-f]{16}", record["batch_id"])
        is None
        or type(record["staging_identity"]) is not list
        or len(record["staging_identity"]) != 2
        or any(type(item) is not int or item < 0 for item in record["staging_identity"])
        or type(record["control_identity"]) is not list
        or len(record["control_identity"]) != 2
        or any(type(item) is not int or item < 0 for item in record["control_identity"])
        or type(record["sources"]) is not list
        or len(record["sources"]) != 4
    ):
        raise _fail("private OCR prepared run state is invalid")
    corpus = _validate_corpus_manifest(corpus_manifest)
    try:
        batch = validate_batch_manifest(record["batch_manifest"])
    except (ContractError, TypeError, ValueError) as exc:
        raise _fail("private OCR prepared run state is invalid") from exc
    sources: list[dict[str, Any]] = []
    selected: str | None = None
    for document, batch_item, source in zip(
        corpus["documents"], batch["documents"], record["sources"], strict=True,
    ):
        if (
            type(source) is not dict
            or set(source) != {
                "item_id", "source_digest", "raster_manifest_digest",
                "page_count", "selected_candidate_id",
            }
            or source["item_id"] != document["item_id"]
            or source["source_digest"] != document["source_digest"]
            or _digest(source["raster_manifest_digest"])
            != source["raster_manifest_digest"]
            or type(source["page_count"]) is not int
            or not 1 <= source["page_count"] <= 100
            or type(source["selected_candidate_id"]) is not str
            or source["selected_candidate_id"]
            not in {"candidate-1", "candidate-2", "candidate-3"}
            or batch_item["item_id"] != source["item_id"]
            or batch_item["source_digest"] != source["source_digest"]
            or batch_item["source"] != (
                f"sources/{record['staging_name']}/{source['item_id']}.pdf"
            )
        ):
            raise _fail("private OCR prepared run state is invalid")
        if selected is None:
            selected = source["selected_candidate_id"]
        elif source["selected_candidate_id"] != selected:
            raise _fail("private OCR prepared run state is invalid")
        sources.append(copy.deepcopy(source))
    runtime = Path(runtime_root)
    run = PrivateOcrPreparedRun(
        runtime_root=runtime,
        staging_name=record["staging_name"],
        staging_identity=tuple(record["staging_identity"]),
        control_path=runtime / "private-ocr" / "runs" / record["batch_id"],
        control_identity=tuple(record["control_identity"]),
        batch_id=record["batch_id"],
        batch_manifest=batch,
        batch_manifest_digest=_digest(record["batch_manifest_digest"]),
        corpus_manifest=corpus,
        corpus_digest=_digest(record["corpus_digest"]),
        qualification_digest=_digest(record["qualification_digest"]),
        campaign_origin_digest=_digest(record["campaign_origin_digest"]),
        sources=tuple(sources),
    )
    if (
        batch["batch_id"] != run.batch_id
        or canonical_digest(batch) != run.batch_manifest_digest
        or canonical_digest(corpus) != run.corpus_digest
        or _run_record(run) != record
    ):
        raise _fail("private OCR prepared run state is invalid")
    return run


def prepare_private_ocr_uat_run(
    *,
    runtime_root: Path,
    activation_inputs: OcrActivationInputs,
    campaign: PreparedOcrCampaignInputs,
) -> PrivateOcrPreparedRun:
    """Publish one random run-owned source stage and exact control record."""

    runtime_root = Path(runtime_root)
    checked = _validate_campaign_inputs(
        campaign, runtime_root=runtime_root, activation_inputs=activation_inputs
    )
    activation = load_private_ocr_activation(
        runtime_root=runtime_root, inputs=activation_inputs
    )
    source_parent_path = Path(__file__).resolve().parents[2] / "sources"
    source_parent = os.open(
        source_parent_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    stage = None
    staging_name = ""
    staging_identity: tuple[int, int] | None = None
    staged_bodies: dict[str, bytes] = {}
    completed = False
    try:
        for _ in range(32):
            staging_name = "private-ocr-uat-" + secrets.token_hex(12)
            try:
                os.mkdir(staging_name, 0o700, dir_fd=source_parent)
                os.fsync(source_parent)
                break
            except FileExistsError:
                staging_name = ""
        if not staging_name:
            raise _fail("private OCR source stage collision")
        stage = _open_child(source_parent, staging_name, create=False)
        stage_info = os.fstat(stage)
        staging_identity = (stage_info.st_dev, stage_info.st_ino)
        corpus_input = runtime_root / "private-ocr" / "corpus" / "input"
        for item in checked.corpus_manifest["documents"]:
            body = (corpus_input / f"{item['item_id']}.pdf").read_bytes()
            if (
                len(body) != item["size"]
                or "sha256:" + hashlib.sha256(body).hexdigest()
                != item["source_digest"]
            ):
                raise _fail("private OCR source stage input drifted")
            _write_owned(stage, f"{item['item_id']}.pdf", body)
            staged_bodies[f"{item['item_id']}.pdf"] = body
        os.fsync(stage)
        batch_id = "batch-private-ocr-" + secrets.token_hex(8)
        batch_manifest = {
            "schema_version": "ao.lore.ingest-batch-manifest.v0.3",
            "batch_id": batch_id,
            "format_id": "ocr",
            "media_type": "application/pdf",
            "parser_id": "paddle-ocr-english",
            "parser_version": "0.1.0",
            "continue_on_error": True,
            "documents": [
                {
                    "item_id": source["item_id"],
                    "source": (
                        f"sources/{staging_name}/{source['item_id']}.pdf"
                    ),
                    "source_digest": source["source_digest"],
                }
                for source in checked.sources
            ],
        }
        batch_manifest_digest = canonical_digest(batch_manifest)
        private_path = runtime_root / "private-ocr"
        private = os.open(
            private_path,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        runs = control = None
        try:
            runs = _open_child(private, "runs", create=True)
            os.mkdir(batch_id, 0o700, dir_fd=runs)
            os.fsync(runs)
            control = _open_child(runs, batch_id, create=False)
            control_info = os.fstat(control)
            control_identity = (control_info.st_dev, control_info.st_ino)
            run = PrivateOcrPreparedRun(
                runtime_root=runtime_root,
                staging_name=staging_name,
                staging_identity=staging_identity,
                control_path=private_path / "runs" / batch_id,
                control_identity=control_identity,
                batch_id=batch_id,
                batch_manifest=batch_manifest,
                batch_manifest_digest=batch_manifest_digest,
                corpus_manifest=copy.deepcopy(checked.corpus_manifest),
                corpus_digest=checked.corpus_digest,
                qualification_digest=activation.qualification_digest,
                campaign_origin_digest=checked.campaign_origin_digest,
                sources=tuple(copy.deepcopy(checked.sources)),
            )
            _write_owned(control, "prepared.json", _canonical_body(_run_record(run)))
            os.fsync(control)
            completed = True
            return run
        finally:
            for descriptor in (control, runs, private):
                if descriptor is not None:
                    os.close(descriptor)
    finally:
        if not completed and stage is not None and staging_identity is not None:
            current = os.fstat(stage)
            if (
                (current.st_dev, current.st_ino) == staging_identity
                and set(os.listdir(stage)) <= set(staged_bodies)
            ):
                for name, body in staged_bodies.items():
                    if name in os.listdir(stage):
                        _unlink_exact(stage, name, body)
                if not os.listdir(stage):
                    os.close(stage)
                    stage = None
                    os.rmdir(staging_name, dir_fd=source_parent)
                    os.fsync(source_parent)
        if stage is not None:
            os.close(stage)
        os.close(source_parent)


def load_private_ocr_prepared_run(
    *, runtime_root: Path, activation_inputs: OcrActivationInputs,
    campaign: PreparedOcrCampaignInputs,
) -> PrivateOcrPreparedRun:
    """Reopen the sole exact prepared run after an operator process restart."""

    runtime = Path(runtime_root)
    _validate_campaign_inputs(
        campaign, runtime_root=runtime, activation_inputs=activation_inputs,
        allow_prepared_run=True,
    )
    runs_path = runtime / "private-ocr" / "runs"
    runs = os.open(
        runs_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    control = None
    try:
        names = os.listdir(runs)
        if (
            len(names) != 1
            or re.fullmatch(r"batch-private-ocr-[0-9a-f]{16}", names[0]) is None
        ):
            raise _fail("private OCR prepared run state is invalid")
        batch_id = names[0]
        control = _open_child(runs, batch_id, create=False)
        if set(os.listdir(control)) != {"prepared.json"}:
            raise _fail("private OCR prepared run state is invalid")
        body = _read_stable_at(control, "prepared.json", 1024 * 1024)
        record = parse_strict_json(body, "private OCR prepared run")
        run = _run_from_record(
            record, runtime_root=runtime, corpus_manifest=campaign.corpus_manifest,
        )
        if run.batch_id != batch_id or _canonical_body(_run_record(run)) != body:
            raise _fail("private OCR prepared run bytes drifted")
        checked, _activation, _identity = _validate_private_ocr_prepared_run(
            run, activation_inputs
        )
        if (
            checked.corpus_digest != campaign.corpus_digest
            or checked.campaign_origin_digest != campaign.campaign_origin_digest
            or checked.sources != campaign.sources
        ):
            raise _fail("private OCR prepared run campaign drifted")
        return checked
    except (ContractError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivateOcrUatError):
            raise
        raise _fail("private OCR prepared run state is invalid") from exc
    finally:
        if control is not None:
            os.close(control)
        os.close(runs)


def cleanup_private_ocr_uat_run(run: PrivateOcrPreparedRun) -> bool:
    """Remove only the exact prepared source stage and control record."""

    if type(run) is not PrivateOcrPreparedRun:
        raise _fail("private OCR prepared run is invalid")
    record_body = _canonical_body(_run_record(run))
    source_parent = os.open(
        Path(__file__).resolve().parents[2] / "sources",
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    stage = None
    try:
        try:
            stage = _open_child(source_parent, run.staging_name, create=False)
        except FileNotFoundError:
            stage = None
        if stage is not None:
            info = os.fstat(stage)
            if (info.st_dev, info.st_ino) != run.staging_identity:
                raise _fail("private OCR source stage drifted")
            expected = {
                f"{item['item_id']}.pdf": item for item in run.sources
            }
            if set(os.listdir(stage)) != set(expected):
                raise _fail("private OCR source stage contains foreign state")
            for name, item in expected.items():
                body = _read_at(stage, name, _MAX_SOURCE_BYTES)
                if "sha256:" + hashlib.sha256(body).hexdigest() != item["source_digest"]:
                    raise _fail("private OCR source stage drifted")
                _unlink_exact(stage, name, body)
            os.close(stage)
            stage = None
            os.rmdir(run.staging_name, dir_fd=source_parent)
            os.fsync(source_parent)
    finally:
        if stage is not None:
            os.close(stage)
        os.close(source_parent)
    private = os.open(
        run.runtime_root / "private-ocr",
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    runs = control = None
    try:
        try:
            runs = _open_child(private, "runs", create=False)
        except FileNotFoundError:
            return True
        try:
            control = _open_child(runs, run.batch_id, create=False)
        except FileNotFoundError:
            control = None
        if control is not None:
            info = os.fstat(control)
            if (info.st_dev, info.st_ino) != run.control_identity:
                raise _fail("private OCR run control drifted")
            if set(os.listdir(control)) != {"prepared.json"} or _read_at(
                control, "prepared.json"
            ) != record_body:
                raise _fail("private OCR run control drifted")
            _unlink_exact(control, "prepared.json", record_body)
            os.close(control)
            control = None
            os.rmdir(run.batch_id, dir_fd=runs)
            os.fsync(runs)
        if not os.listdir(runs):
            os.close(runs)
            runs = None
            os.rmdir("runs", dir_fd=private)
            os.fsync(private)
        return True
    finally:
        for descriptor in (control, runs, private):
            if descriptor is not None:
                os.close(descriptor)


def _validate_private_ocr_prepared_run(
    run: Any, activation_inputs: OcrActivationInputs
) -> tuple[PrivateOcrPreparedRun, ActivatedOcrParser, tuple[int, int]]:
    if type(run) is not PrivateOcrPreparedRun:
        raise _fail("private OCR prepared run is invalid")
    activation = load_private_ocr_activation(
        runtime_root=run.runtime_root, inputs=activation_inputs
    )
    if (
        run.qualification_digest != activation.qualification_digest
        or canonical_digest(run.batch_manifest) != run.batch_manifest_digest
        or run.batch_manifest.get("batch_id") != run.batch_id
        or run.corpus_digest != canonical_digest(run.corpus_manifest)
        or len(run.sources) != 4
    ):
        raise _fail("private OCR prepared run binding drifted")
    source_parent = os.open(
        Path(__file__).resolve().parents[2] / "sources",
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    stage = None
    try:
        stage = _open_child(source_parent, run.staging_name, create=False)
        info = os.fstat(stage)
        if (info.st_dev, info.st_ino) != run.staging_identity:
            raise _fail("private OCR prepared source stage drifted")
        expected = {f"{item['item_id']}.pdf": item for item in run.sources}
        if set(os.listdir(stage)) != set(expected):
            raise _fail("private OCR prepared source stage drifted")
        for name, item in expected.items():
            body = _read_stable_at(stage, name, _MAX_SOURCE_BYTES)
            if "sha256:" + hashlib.sha256(body).hexdigest() != item["source_digest"]:
                raise _fail("private OCR prepared source stage drifted")
    finally:
        if stage is not None:
            os.close(stage)
        os.close(source_parent)
    control = os.open(
        run.control_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        info = os.fstat(control)
        if (info.st_dev, info.st_ino) != run.control_identity:
            raise _fail("private OCR prepared control drifted")
        body = _read_stable_at(control, "prepared.json")
        if set(os.listdir(control)) != {"prepared.json"} or body != _canonical_body(
            _run_record(run)
        ):
            raise _fail("private OCR prepared control drifted")
        prepared_info = os.stat(
            "prepared.json", dir_fd=control, follow_symlinks=False
        )
        manifest_identity = (prepared_info.st_dev, prepared_info.st_ino)
    finally:
        os.close(control)
    return run, activation, manifest_identity


def project_private_ocr_prepared_run(
    run: PrivateOcrPreparedRun, activation_inputs: OcrActivationInputs
) -> PrivateDocumentPreparedRun:
    """Project one exact OCR run into the shared UAT lifecycle contract."""

    checked, activation, identity = _validate_private_ocr_prepared_run(
        run, activation_inputs
    )
    shared_manifest = _validate_manifest({
        "corpus_id": checked.corpus_manifest["corpus_id"],
        "documents": [
            {"item_id": item["item_id"], "source_digest": item["source_digest"]}
            for item in checked.sources
        ],
    })
    shared_manifest_digest = canonical_digest(shared_manifest)
    return PrivateDocumentPreparedRun(
        format_id="ocr",
        policy_digest=private_document_policy_digest(PRIVATE_OCR_UAT_POLICY),
        batch_id=checked.batch_id,
        manifest=copy.deepcopy(shared_manifest),
        manifest_digest=shared_manifest_digest,
        manifest_identity=identity,
        staging_name=checked.staging_name,
        control_identity=checked.control_identity,
        source_bindings=tuple(
            private_document_state_body(
                PRIVATE_OCR_UAT_POLICY,
                item_id=item["item_id"],
                source_digest=item["source_digest"],
            )
            for item in checked.sources
        ),
        qualification_binding=private_document_state_body(
            PRIVATE_OCR_UAT_POLICY,
            manifest_digest=shared_manifest_digest,
            manifest_identity=list(identity),
            qualification={
                "qualification_digest": activation.qualification_digest,
                "runtime_digest": activation.runtime_digest,
                "model_set_digest": activation.model_set_digest,
                "selected_candidate_id": activation.candidate_id,
                "campaign_origin_digest": checked.campaign_origin_digest,
            },
        ),
        expectation_binding=private_document_state_body(
            PRIVATE_OCR_UAT_POLICY,
            manifest_digest=shared_manifest_digest,
            manifest_identity=list(identity),
            item_ids=[item["item_id"] for item in checked.sources],
            source_digests=[item["source_digest"] for item in checked.sources],
        ),
    )


def private_ocr_worker_contract(
    run: PrivateOcrPreparedRun,
    activation_inputs: OcrActivationInputs,
    phase: str,
) -> dict[str, Any]:
    """Build one immutable, path-free phase contract from retained run state."""

    checked, activation, _identity = _validate_private_ocr_prepared_run(
        run, activation_inputs
    )
    arithmetic = {"interrupted": (0, 1), "resume": (1, 4), "rerun": (4, 4)}
    if type(phase) is not str or phase not in arithmetic:
        raise _fail("private OCR worker phase is invalid")
    start, end = arithmetic[phase]
    value = {
        "schema_version": "ao.lore.private-ocr-uat-worker-contract.v0.1",
        "phase": phase,
        "expected_start": start,
        "expected_end": end,
        "qualification_digest": activation.qualification_digest,
        "runtime_digest": activation.runtime_digest,
        "model_set_digest": activation.model_set_digest,
        "selected_candidate_id": activation.candidate_id,
        "campaign_origin_digest": checked.campaign_origin_digest,
        "configuration_digest": OCR_WORKER_CONFIGURATION_DIGEST,
        "batch_id": checked.batch_id,
        "batch_manifest_digest": checked.batch_manifest_digest,
        "batch_manifest": copy.deepcopy(checked.batch_manifest),
        "staging_name": checked.staging_name,
        "sources": [
            {
                "item_id": item["item_id"],
                "source_digest": item["source_digest"],
                "raster_manifest_digest": item["raster_manifest_digest"],
            }
            for item in checked.sources
        ],
        **{field: False for field in AUTHORITY_FIELDS},
    }
    from .private_ocr_uat_worker import validate_uat_worker_contract

    return validate_uat_worker_contract(value)


def private_ocr_calibration_contract(
    run: PrivateOcrPreparedRun,
    activation_inputs: OcrActivationInputs,
) -> dict[str, Any]:
    """Build the immutable twice-per-item representative calibration contract."""

    worker = private_ocr_worker_contract(run, activation_inputs, "rerun")
    value = {
        "schema_version": "ao.lore.private-ocr-calibration-contract.v0.1",
        "attempts_per_item": 2,
        **{
            key: copy.deepcopy(item)
            for key, item in worker.items()
            if key not in {"schema_version", "phase", "expected_start", "expected_end"}
        },
    }
    from .private_ocr_calibration_worker import validate_calibration_contract

    return validate_calibration_contract(value)


def _ocr_phase_mount(source: Path, target: str, name: str, mode: str) -> dict[str, Any]:
    if (
        type(target) is not str
        or not target.startswith("/")
        or "\x00" in target
        or type(name) is not str
        or _ITEM.fullmatch(name) is None
        or mode not in {"read-only", "read-write"}
    ):
        raise _fail("private OCR phase sandbox mount is invalid")
    try:
        info = os.lstat(source)
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise OSError("mount type differs")
        descriptor = os.open(
            source,
            os.O_RDONLY
            | (os.O_DIRECTORY if stat.S_ISDIR(info.st_mode) else 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise OSError("mount identity drifted")
    except OSError as exc:
        raise _fail("private OCR phase sandbox mount is invalid") from exc
    return {
        "name": name,
        "source": str(source),
        "target": target,
        "mode": mode,
        "identity": {"device": opened.st_dev, "inode": opened.st_ino},
    }


def _ocr_phase_paths(
    run: PrivateOcrPreparedRun, *, calibration: bool = False,
) -> dict[str, Path]:
    repository = Path(__file__).resolve().parents[2]
    runtime = Path(run.runtime_root)
    batch = runtime / "batches" / run.batch_id
    candidates = repository / "working" / "candidates" / run.staging_name
    state = runtime / "uat" / "private-ocr" / run.batch_id
    for path in ((state,) if calibration else (batch, candidates, state)):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _fail("private OCR phase state is invalid")
    return {
        "repository": repository,
        "runtime": runtime,
        "batch": batch,
        "candidates": candidates,
        "state": state,
        "sources": repository / "sources" / run.staging_name,
        "rasters": runtime / "private-ocr" / "rasters",
        "activation": runtime / "evidence" / "private-ocr-activation" / "current",
        "ocr_runtime": repository / ".ao-lore" / "ocr-runtime" / "ocr-runtime-v0.1",
    }


def _append_private_ocr_nvidia_devices(argv: list[str]) -> None:
    for device in (
        "nvidia0", "nvidiactl", "nvidia-modeset", "nvidia-uvm", "nvidia-uvm-tools",
    ):
        path = Path("/dev") / device
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISCHR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise _fail("private OCR GPU device differs")
        argv.extend(("--dev-bind", str(path), str(path)))
    caps = Path("/dev/nvidia-caps")
    try:
        caps_info = os.lstat(caps)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(caps_info.st_mode) or stat.S_ISLNK(caps_info.st_mode):
        raise _fail("private OCR GPU device differs")
    argv.extend(("--dir", "/dev/nvidia-caps"))
    for path in sorted(caps.iterdir()):
        if _NVIDIA_CAP_NAME.fullmatch(path.name) is None:
            raise _fail("private OCR GPU device differs")
        info = os.lstat(path)
        if not stat.S_ISCHR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise _fail("private OCR GPU device differs")
        argv.extend(("--dev-bind", str(path), str(path)))


def _ocr_phase_sandbox_spec(
    run: PrivateOcrPreparedRun,
    activation_inputs: OcrActivationInputs,
    phase: str,
    *,
    command: Sequence[str],
    bwrap_version: str,
    contract_fd: int = 97,
    seccomp_fd: int = 98,
) -> dict[str, Any]:
    """Build the fixed empty-root supervisor namespace for one OCR phase."""

    calibration = phase == "calibration"
    if calibration:
        private_ocr_calibration_contract(run, activation_inputs)
    else:
        private_ocr_worker_contract(run, activation_inputs, phase)
    if (
        type(command) not in {list, tuple}
        or list(command[-3:-1]) != [sys.executable, "-m"]
        or command[-1]
        not in {
            "ao_lore.private_ocr_uat_worker",
            "ao_lore.private_ocr_calibration_worker",
            "tests.private_ocr_uat_worker_fixture",
        }
        or calibration != (command[-1] == "ao_lore.private_ocr_calibration_worker")
        or any(type(part) is not str or not part or "\x00" in part for part in command)
        or re.fullmatch(r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", bwrap_version) is None
        or type(contract_fd) is not int
        or contract_fd < 3
        or type(seccomp_fd) is not int
        or seccomp_fd < 3
        or seccomp_fd == contract_fd
    ):
        raise _fail("private OCR phase sandbox command is invalid")
    paths = _ocr_phase_paths(run, calibration=calibration)
    mounts = [
        _ocr_phase_mount(Path("/usr"), "/usr", "system-usr", "read-only"),
        _ocr_phase_mount(paths["repository"] / "src", "/app/src", "ao-lore-src", "read-only"),
        _ocr_phase_mount(paths["rasters"], "/rasters", "raster-bundles", "read-only"),
        _ocr_phase_mount(paths["activation"], "/app/.ao-lore/evidence/private-ocr-activation/current", "activation-evidence", "read-only"),
        _ocr_phase_mount(paths["ocr_runtime"], "/ocr-runtime", "ocr-runtime", "read-only"),
        _ocr_phase_mount(paths["state"], f"/app/.ao-lore/uat/private-ocr/{run.batch_id}", "run-state", "read-write"),
    ]
    if not calibration:
        mounts.extend((
            _ocr_phase_mount(paths["sources"], f"/app/sources/{run.staging_name}", "staged-sources", "read-only"),
            _ocr_phase_mount(paths["batch"], f"/app/.ao-lore/batches/{run.batch_id}", "batch-state", "read-write"),
            _ocr_phase_mount(paths["candidates"], f"/app/working/candidates/{run.staging_name}", "candidate-state", "read-write"),
        ))
    if command[-1] == "tests.private_ocr_uat_worker_fixture":
        mounts.append(_ocr_phase_mount(
            paths["repository"] / "tests", "/app/tests", "ao-lore-tests", "read-only"
        ))
    environment = {
        "AO_LORE_HOME": "/app/.ao-lore",
        "HOME": "/tmp/home",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PWD": "/app",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "/app/src" + (":/app" if command[-1].startswith("tests.") else ""),
        "XDG_CACHE_HOME": "/tmp/cache",
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    argv = [
        _BWRAP, "--die-with-parent", "--new-session",
        "--unshare-net", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
        "--perms", "0555", "--tmpfs", "/",
        "--perms", "0555", "--dir", "/launch",
        "--perms", "0555", "--dir", "/app",
        "--perms", "0555", "--dir", "/app/sources",
        "--perms", "0555", "--dir", "/app/working",
        "--perms", "0555", "--dir", "/app/working/candidates",
        "--perms", "0555", "--dir", "/app/.ao-lore",
        "--perms", "0555", "--dir", "/app/.ao-lore/batches",
        "--perms", "0555", "--dir", "/app/.ao-lore/evidence",
        "--perms", "0555", "--dir", "/app/.ao-lore/evidence/private-ocr-activation",
        "--perms", "0555", "--dir", "/app/.ao-lore/uat",
        "--perms", "0555", "--dir", "/app/.ao-lore/uat/private-ocr",
        "--perms", "0400",
        "--ro-bind-data", str(contract_fd), _OCR_PHASE_CONTRACT_PATH,
        "--seccomp", str(seccomp_fd),
    ]
    for mount in mounts:
        argv.extend((
            "--bind" if mount["mode"] == "read-write" else "--ro-bind",
            mount["source"], mount["target"],
        ))
    argv.extend((
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
        "--dev", "/dev",
    ))
    _append_private_ocr_nvidia_devices(argv)
    argv.extend(("--proc", "/proc", "--tmpfs", "/tmp", "--clearenv"))
    for key in sorted(environment):
        if key != "PWD":
            argv.extend(("--setenv", key, environment[key]))
    limits = _OCR_PHASE_RESOURCE_LIMITS
    argv.extend((
        "--chdir", "/app", "--", "/usr/bin/prlimit",
        f"--cpu={limits['cpu_seconds']}:{limits['cpu_seconds']}",
        f"--as={limits['address_space_bytes']}:{limits['address_space_bytes']}",
        f"--data={limits['data_bytes']}:{limits['data_bytes']}",
        f"--fsize={limits['file_size_bytes']}:{limits['file_size_bytes']}",
        f"--nofile={limits['open_files']}:{limits['open_files']}",
        f"--nproc={limits['processes']}:{limits['processes']}",
        "--core=0:0", "--", *command,
    ))
    return {
        "schema_version": (
            "ao.lore.private-ocr-calibration-sandbox.v0.1"
            if calibration else "ao.lore.private-ocr-phase-sandbox.v0.1"
        ),
        "mechanism": _BWRAP,
        "version": bwrap_version,
        "argv": argv,
        "network_namespace": "unshared",
        "root_filesystem": "allowlisted-empty-root",
        "mounts": mounts,
        "environment": environment,
        "immutable_contract": {
            "path": _OCR_PHASE_CONTRACT_PATH, "fd": contract_fd, "sealed": True,
        },
        "resource_limits": dict(limits),
        "seccomp": {
            "fd": seccomp_fd,
            "denied_syscalls": list(_OCR_PHASE_DENIED_NETWORK_SYSCALLS),
        },
        "child_process_policy": "nested-bwrap-workers-only",
        "authority": False,
    }


def _sealed_phase_contract(body: bytes) -> int:
    descriptor = os.memfd_create(
        "ao-lore-private-ocr-phase", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
    )
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("private OCR phase contract short write")
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        )
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != seals:
            raise OSError("private OCR phase contract sealing failed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _ocr_phase_seccomp_descriptor() -> int:
    """Seal a filter denying every direct outbound network syscall."""

    descriptor = os.memfd_create(
        "ao-lore-private-ocr-network-seccomp",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    library: Any | None = None
    context: int | None = None
    try:
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
        library.seccomp_init.argtypes = (ctypes.c_uint32,)
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_rule_add.argtypes = (
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        )
        library.seccomp_rule_add.restype = ctypes.c_int
        library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
        library.seccomp_syscall_resolve_name.restype = ctypes.c_int
        library.seccomp_export_bpf.argtypes = (ctypes.c_void_p, ctypes.c_int)
        library.seccomp_export_bpf.restype = ctypes.c_int
        library.seccomp_release.argtypes = (ctypes.c_void_p,)
        context = library.seccomp_init(0x7FFF0000)
        if not context:
            raise OSError("private OCR network filter initialization failed")
        action = 0x00050000 | errno.EPERM
        for name in _OCR_PHASE_DENIED_NETWORK_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0 or library.seccomp_rule_add(context, action, number, 0) != 0:
                raise OSError("private OCR network filter rule failed")
        if library.seccomp_export_bpf(context, descriptor) != 0:
            raise OSError("private OCR network filter export failed")
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = (
            fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        )
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != seals:
            raise OSError("private OCR network filter sealing failed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        if library is not None and context:
            library.seccomp_release(context)


def _probe_ocr_phase_sandbox() -> str:
    try:
        info = os.lstat(_BWRAP)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("bubblewrap identity differs")
        version_result = subprocess.run(
            [_BWRAP, "--version"], env={}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5, check=False,
        )
        version = version_result.stdout.decode("ascii", errors="strict").strip()
        if (
            version_result.returncode != 0
            or version_result.stderr
            or re.fullmatch(r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", version) is None
        ):
            raise OSError("bubblewrap version differs")
        probe = subprocess.run(
            [
                _BWRAP, "--unshare-net", "--unshare-ipc", "--unshare-pid",
                "--unshare-uts", "--tmpfs", "/", "--ro-bind", "/usr", "/usr",
                "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
                "--symlink", "usr/lib64", "/lib64", "--dev", "/dev",
                "--proc", "/proc", "--", "/usr/bin/true",
            ],
            env={}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10, check=False,
        )
        if probe.returncode != 0 or probe.stdout or len(probe.stderr) > 4096:
            raise OSError("bubblewrap probe failed")
        return version
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _fail("private OCR phase sandbox is unavailable") from exc


def _launch_private_ocr_worker(
    run: PrivateOcrPreparedRun,
    activation_inputs: OcrActivationInputs,
    phase: str,
    *,
    module: str = "ao_lore.private_ocr_uat_worker",
) -> subprocess.Popen[bytes]:
    """Launch one phase supervisor from an immutable contract descriptor."""

    contract = private_ocr_worker_contract(run, activation_inputs, phase)
    checkpoint_path = Path(run.runtime_root) / "batches" / run.batch_id / "checkpoint.json"
    if checkpoint_path.exists():
        try:
            info = os.lstat(checkpoint_path)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or not 1 <= info.st_size <= 1024 * 1024
            ):
                raise OSError("checkpoint identity differs")
            body = checkpoint_path.read_bytes()
            after = os.lstat(checkpoint_path)
            if (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
            ) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
            ):
                raise OSError("checkpoint drifted")
            checkpoint = validate_checkpoint(
                parse_strict_json(body, "private OCR phase checkpoint"),
                run.batch_manifest,
                run.batch_manifest_digest,
            )
            processed = checkpoint["processed"]
        except (ContractError, OSError, TypeError, ValueError) as exc:
            raise _fail("private OCR phase checkpoint is invalid") from exc
    else:
        processed = 0
    if processed != contract["expected_start"]:
        raise _fail("private OCR worker phase arithmetic is invalid")
    version = _probe_ocr_phase_sandbox()
    command = [sys.executable, "-m", module]
    descriptor = -1
    seccomp_descriptor = -1
    try:
        sandbox = _ocr_phase_sandbox_spec(
            run, activation_inputs, phase, command=command,
            bwrap_version=version, contract_fd=97, seccomp_fd=98,
        )
        body = json.dumps(
            contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        retained_path = (
            Path(run.runtime_root) / "uat" / "private-ocr" / run.batch_id
            / f"worker-contract-{phase}.json"
        )
        if retained_path.exists():
            if _read_stable_path(retained_path, 1024 * 1024) != body:
                raise _fail("private OCR retained worker contract drifted")
        else:
            parent = os.open(
                retained_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                _write_owned(parent, retained_path.name, body)
                os.fsync(parent)
            finally:
                os.close(parent)
        descriptor = _sealed_phase_contract(body)
        seccomp_descriptor = _ocr_phase_seccomp_descriptor()
        # Bubblewrap consumes the inherited descriptor number, not the test-only
        # placeholder used while constructing the deterministic specification.
        sandbox = _ocr_phase_sandbox_spec(
            run, activation_inputs, phase, command=command,
            bwrap_version=version, contract_fd=descriptor,
            seccomp_fd=seccomp_descriptor,
        )
        process = subprocess.Popen(
            sandbox["argv"], cwd=Path(__file__).resolve().parents[2], env={},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, shell=False,
            pass_fds=(descriptor, seccomp_descriptor),
        )
        process._ao_lore_process_group = process.pid  # type: ignore[attr-defined]
        process._ao_lore_phase = phase  # type: ignore[attr-defined]
        process._ao_lore_module = module  # type: ignore[attr-defined]
        process._ao_lore_prepared_run = run  # type: ignore[attr-defined]
        process._ao_lore_contract_body = body  # type: ignore[attr-defined]
        process._ao_lore_contract_path = retained_path  # type: ignore[attr-defined]
        process._ao_lore_sandbox_digest = canonical_digest(sandbox)  # type: ignore[attr-defined]
        return process
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if seccomp_descriptor >= 0:
            os.close(seccomp_descriptor)


def _launch_private_ocr_calibration_worker(
    run: PrivateOcrPreparedRun,
    activation_inputs: OcrActivationInputs,
) -> subprocess.Popen[bytes]:
    """Launch the dedicated read-only representative calibration worker."""

    version = _probe_ocr_phase_sandbox()
    contract = private_ocr_calibration_contract(run, activation_inputs)
    _ocr_phase_paths(run, calibration=True)
    body = json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    descriptor = seccomp_descriptor = -1
    retained_path = (
        Path(run.runtime_root) / "uat" / "private-ocr" / run.batch_id
        / "calibration-contract.json"
    )
    try:
        if retained_path.exists():
            if _read_stable_path(retained_path, 1024 * 1024) != body:
                raise _fail("private OCR calibration contract drifted")
        else:
            parent = os.open(
                retained_path.parent,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                _write_owned(parent, retained_path.name, body)
                os.fsync(parent)
            finally:
                os.close(parent)
        descriptor = _sealed_phase_contract(body)
        seccomp_descriptor = _ocr_phase_seccomp_descriptor()
        sandbox = _ocr_phase_sandbox_spec(
            run, activation_inputs, "calibration",
            command=[sys.executable, "-m", "ao_lore.private_ocr_calibration_worker"],
            bwrap_version=version, contract_fd=descriptor,
            seccomp_fd=seccomp_descriptor,
        )
        process = subprocess.Popen(
            sandbox["argv"], cwd=Path(__file__).resolve().parents[2], env={},
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, shell=False,
            pass_fds=(descriptor, seccomp_descriptor),
        )
        process._ao_lore_process_group = process.pid  # type: ignore[attr-defined]
        process._ao_lore_phase = "calibration"  # type: ignore[attr-defined]
        process._ao_lore_module = "ao_lore.private_ocr_calibration_worker"  # type: ignore[attr-defined]
        process._ao_lore_prepared_run = run  # type: ignore[attr-defined]
        process._ao_lore_contract_body = body  # type: ignore[attr-defined]
        process._ao_lore_contract_path = retained_path  # type: ignore[attr-defined]
        process._ao_lore_sandbox_digest = canonical_digest(sandbox)  # type: ignore[attr-defined]
        return process
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if seccomp_descriptor >= 0:
            os.close(seccomp_descriptor)


def _wait_private_ocr_calibration_worker(
    process: subprocess.Popen[bytes], *, timeout: float = 3600.0,
) -> dict[str, Any]:
    if process.stdout is None or process.stderr is None or timeout <= 0:
        raise _fail("private OCR calibration process is invalid")
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_private_ocr_worker(process)
        process.communicate()
        raise _fail("private OCR calibration timed out") from exc
    if (
        process.returncode != 0
        or not 1 <= len(stdout) <= 8 * 1024 * 1024
        or len(stderr) > 64 * 1024
    ):
        raise _fail("private OCR calibration worker failed")
    run = getattr(process, "_ao_lore_prepared_run", None)
    body = getattr(process, "_ao_lore_contract_body", None)
    retained_path = getattr(process, "_ao_lore_contract_path", None)
    if (
        type(run) is not PrivateOcrPreparedRun
        or type(body) is not bytes
        or not isinstance(retained_path, Path)
        or _read_stable_path(retained_path, 1024 * 1024) != body
    ):
        raise _fail("private OCR calibration contract drifted")
    from .private_ocr_calibration_worker import (
        validate_calibration_contract, validate_calibration_result,
    )
    contract = validate_calibration_contract(
        parse_strict_json(body, "private OCR retained calibration contract")
    )
    result = validate_calibration_result(
        parse_strict_json(stdout, "private OCR calibration result"),
        contract=contract,
    )
    process._ao_lore_sandbox_verified = True  # type: ignore[attr-defined]
    return result


def _terminate_private_ocr_worker(process: subprocess.Popen[bytes]) -> None:
    """Terminate the complete outer supervisor process group, even if its leader exited."""

    group = getattr(process, "_ao_lore_process_group", process.pid)
    if type(group) is not int or group <= 1:
        raise _fail("private OCR worker process group is invalid")

    def exists() -> bool:
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise _fail("private OCR worker process group is unverifiable") from exc

    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not exists():
            process.poll()
            return
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            process.poll()
            return
        deadline = time.monotonic() + 2.0
        while exists() and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.01)
    process.poll()
    if exists():
        raise _fail("private OCR worker process group survived termination")


def _private_ocr_checkpoint(run: PrivateOcrPreparedRun) -> dict[str, Any] | None:
    path = Path(run.runtime_root) / "batches" / run.batch_id / "checkpoint.json"
    if not path.exists():
        return None
    try:
        return validate_checkpoint(
            parse_strict_json(
                _read_stable_path(path, 1024 * 1024),
                "private OCR live checkpoint",
            ),
            run.batch_manifest,
            run.batch_manifest_digest,
        )
    except (ContractError, OSError, TypeError, ValueError) as exc:
        raise _fail("private OCR live checkpoint is invalid") from exc


def _read_stable_path(path: Path, maximum: int) -> bytes:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 1 <= before.st_size <= maximum
        ):
            raise OSError("file identity differs")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        body = b""
        while len(body) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(body)))
            if not chunk:
                break
            body += chunk
        after = os.fstat(descriptor)
        public = os.lstat(path)
        if (
            len(body) > maximum
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (public.st_dev, public.st_ino)
        ):
            raise OSError("file drifted")
        return body
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _inspect_private_ocr_barrier(
    run: PrivateOcrPreparedRun, checkpoint: Mapping[str, Any]
) -> bool:
    path = (
        Path(run.runtime_root) / "uat" / "private-ocr"
        / run.batch_id / "worker-barrier.json"
    )
    try:
        barrier = parse_strict_json(
            _read_stable_path(path, 64 * 1024), "private OCR worker barrier"
        )
    except FileNotFoundError:
        return False
    contract_body = _read_stable_path(
        path.with_name("worker-contract-interrupted.json"), 1024 * 1024
    )
    from .private_ocr_uat_worker import validate_uat_worker_contract
    contract = validate_uat_worker_contract(
        parse_strict_json(contract_body, "private OCR retained worker contract")
    )
    if json.dumps(
        contract, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii") != contract_body:
        raise _fail("private OCR retained worker contract drifted")
    if (
        type(barrier) is not dict
        or set(barrier) != {
            "schema_version", "batch_id", "phase", "processed",
            "checkpoint_digest", "contract_digest", "authority",
        }
        or barrier["schema_version"] != "ao.lore.private-ocr-worker-barrier.v0.1"
        or barrier["batch_id"] != run.batch_id
        or barrier["phase"] != "interrupted"
        or barrier["processed"] != 1
        or barrier["checkpoint_digest"] != checkpoint.get("checkpoint_digest")
        or barrier["authority"] is not False
        or checkpoint.get("processed") != 1
        or barrier["contract_digest"] != canonical_digest(contract)
    ):
        raise _fail("private OCR worker barrier is invalid")
    return True


def _signal_private_ocr_worker(process: subprocess.Popen[bytes]) -> None:
    group = getattr(process, "_ao_lore_process_group", process.pid)
    if type(group) is not int or group <= 1:
        raise _fail("private OCR worker process group is invalid")
    try:
        os.killpg(group, signal.SIGINT)
    except OSError as exc:
        raise _fail("private OCR worker interruption failed") from exc


def _wait_private_ocr_worker(
    process: subprocess.Popen[bytes], *, timeout: float = 3600.0
) -> dict[str, Any]:
    if process.stdout is None or process.stderr is None or timeout <= 0:
        raise _fail("private OCR worker process is invalid")
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    try:
        selector.register(process.stdout, selectors.EVENT_READ, stdout)
        selector.register(process.stderr, selectors.EVENT_READ, stderr)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("private OCR worker", timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired("private OCR worker", timeout)
            for key, _mask in events:
                target = key.data
                maximum = 8 * 1024 * 1024 if target is stdout else 64 * 1024
                chunk = os.read(key.fileobj.fileno(), min(64 * 1024, maximum + 1 - len(target)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if len(target) > maximum:
                    raise _fail("private OCR worker output exceeded its bound")
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _terminate_private_ocr_worker(process)
        raise _fail("private OCR worker timed out") from exc
    except BaseException:
        _terminate_private_ocr_worker(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    run = getattr(process, "_ao_lore_prepared_run", None)
    phase = getattr(process, "_ao_lore_phase", None)
    contract_body = getattr(process, "_ao_lore_contract_body", None)
    from .private_ocr_uat_worker import validate_uat_worker_contract
    if (
        type(run) is not PrivateOcrPreparedRun
        or phase not in {"interrupted", "resume", "rerun"}
        or type(contract_body) is not bytes
        or json.dumps(
            validate_uat_worker_contract(parse_strict_json(
                contract_body, "private OCR retained worker contract"
            )), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ).encode("ascii") != contract_body
    ):
        raise _fail("private OCR worker contract drifted")
    if getattr(process, "_ao_lore_module", None) == "ao_lore.private_ocr_uat_worker":
        checkpoint = _private_ocr_checkpoint(run)
        expected = {"interrupted": 1, "resume": 4, "rerun": 4}[phase]
        if checkpoint is None or checkpoint["processed"] != expected:
            raise _fail("private OCR worker phase arithmetic drifted")
    process._ao_lore_sandbox_verified = True  # type: ignore[attr-defined]
    return {"returncode": returncode, "stdout": bytes(stdout), "stderr": bytes(stderr)}


def _private_ocr_candidate_snapshot(
    run: PrivateOcrPreparedRun | None,
) -> dict[str, dict[str, Any]]:
    if run is None:
        return {}
    root = Path(__file__).resolve().parents[2] / "working" / "candidates" / run.staging_name
    if not root.exists():
        return {}
    from .candidate_queue import list_candidates

    report = list_candidates(status="all", limit=200, candidate_root=root)
    if report["next_after"] is not None:
        raise _fail("private OCR candidate queue exceeded its bound")
    return {
        item["candidate_id"]: {
            "candidate_id": item["candidate_id"],
            "candidate_digest": item["candidate_digest"],
            "provenance_digest": item["provenance_digest"],
            "source_digest": item["source_digest"],
            "review_status": item["review_status"],
        }
        for item in report["items"]
    }


def _load_private_ocr_final(run: PrivateOcrPreparedRun) -> Mapping[str, Any] | None:
    return load_final_batch_readback(
        run.batch_id,
        batch_root=Path(run.runtime_root) / "batches" / run.batch_id,
    )


def _verify_private_ocr_terminal(
    run: PrivateOcrPreparedRun, stdout: bytes, loaded: Mapping[str, Any] | None,
) -> dict[str, Any]:
    emitted = validate_final_batch_readback(
        parse_strict_json(stdout, "private OCR terminal output")
    )
    if loaded is None:
        raise _fail("private OCR terminal readback is absent")
    retained = validate_final_batch_readback(loaded)
    if (
        emitted != retained
        or retained["batch_id"] != run.batch_id
        or retained["manifest_digest"] != run.batch_manifest_digest
        or [item["item_id"] for item in retained["items"]]
        != [item["item_id"] for item in run.batch_manifest["documents"]]
        or [item["source_digest"] for item in retained["items"]]
        != [item["source_digest"] for item in run.batch_manifest["documents"]]
    ):
        raise _fail("private OCR terminal readback drifted")
    return retained


def _verify_private_ocr_queue(
    values: Any,
    terminal: Mapping[str, Any],
    before: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if type(values) is not dict or before:
        raise _fail("private OCR candidate queue is invalid")
    expected: set[str] = set()
    results: list[dict[str, Any]] = []
    for item in terminal["items"]:
        if item["status"] == "rejected":
            raise _fail("private OCR representative item was rejected")
        candidate_id = item["candidate_id"]
        queued = values.get(candidate_id)
        expected_value = {
            "candidate_id": candidate_id,
            "candidate_digest": item["candidate_digest"],
            "provenance_digest": item["provenance_digest"],
            "source_digest": item["source_digest"],
            "review_status": item["review_status"],
        }
        if (
            type(queued) is not dict
            or queued != expected_value
            or queued["review_status"] != "unreviewed"
        ):
            raise _fail("private OCR candidate queue drifted")
        expected.add(candidate_id)
        results.append(dict(item))
    if set(values) != expected:
        raise _fail("private OCR candidate queue contains foreign results")
    return results


def _remove_quarantined_file(parent: int, name: str, body: bytes) -> None:
    if _read_at(parent, name, max(_MAX_SOURCE_BYTES, len(body))) != body:
        raise _fail("private OCR corpus cleanup binding drifted")
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    after = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (
        before.st_dev, before.st_ino, before.st_mode, before.st_nlink, before.st_size
    ) != (
        after.st_dev, after.st_ino, after.st_mode, after.st_nlink, after.st_size
    ):
        raise _fail("private OCR corpus cleanup binding drifted")
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)


def _corpus_intent(body: bytes) -> tuple[dict[str, Any], bytes]:
    value = parse_strict_json(body, "private OCR corpus intent")
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "manifest_digest", "manifest", "authority"}
        or value["schema_version"] != "ao.lore.private-ocr-uat-corpus-intent.v0.1"
        or value["authority"] is not False
    ):
        raise _fail("private OCR corpus intent is invalid")
    manifest = _validate_corpus_manifest(value["manifest"])
    if value["manifest_digest"] != canonical_digest(manifest):
        raise _fail("private OCR corpus intent binding drifted")
    canonical = _canonical_body({
        "schema_version": value["schema_version"],
        "manifest_digest": value["manifest_digest"],
        "manifest": manifest,
        "authority": False,
    })
    if canonical != body:
        raise _fail("private OCR corpus intent bytes drifted")
    return manifest, canonical


def _reclaim_private_ocr_partial(private: int) -> None:
    stage = _open_child(private, ".corpus.partial", create=False)
    inputs = None
    try:
        allowed = {
            "intent.json", ".reclaim-intent.json", "input", "manifest.json"
        }
        entries = set(os.listdir(stage))
        if not entries <= allowed:
            raise _fail("private OCR corpus partial contains foreign state")
        intent_name = (
            "intent.json" if "intent.json" in entries else ".reclaim-intent.json"
            if ".reclaim-intent.json" in entries else None
        )
        if intent_name is None:
            if entries:
                raise _fail("private OCR corpus partial is unauthenticated")
            os.close(stage)
            stage = None
            os.rmdir(".corpus.partial", dir_fd=private)
            os.fsync(private)
            return
        manifest, intent_body = _corpus_intent(_read_at(stage, intent_name))
        if "manifest.json" in entries:
            manifest_body = _read_at(stage, "manifest.json")
            if manifest_body != _canonical_body(manifest):
                raise _fail("private OCR corpus partial manifest drifted")
        if "input" in entries:
            inputs = _open_child(stage, "input", create=False)
            expected = {f"{item['item_id']}.pdf": item for item in manifest["documents"]}
            input_entries = set(os.listdir(inputs))
            allowed_inputs = set(expected) | {f".reclaim-{name}" for name in expected}
            if not input_entries <= allowed_inputs:
                raise _fail("private OCR corpus partial inputs contain foreign state")
            for name, item in expected.items():
                selected = name if name in input_entries else f".reclaim-{name}"
                if selected not in input_entries:
                    continue
                body = _read_at(inputs, selected, _MAX_SOURCE_BYTES)
                if (
                    len(body) != item["size"]
                    or "sha256:" + hashlib.sha256(body).hexdigest()
                    != item["source_digest"]
                ):
                    raise _fail("private OCR corpus partial input drifted")
                if selected == name:
                    _unlink_exact(inputs, name, body)
                else:
                    _remove_quarantined_file(inputs, selected, body)
            if os.listdir(inputs):
                raise _fail("private OCR corpus partial inputs remain")
            os.close(inputs)
            inputs = None
            os.rmdir("input", dir_fd=stage)
            os.fsync(stage)
        if "manifest.json" in set(os.listdir(stage)):
            body = _canonical_body(manifest)
            _unlink_exact(stage, "manifest.json", body)
        if intent_name == "intent.json":
            _unlink_exact(stage, "intent.json", intent_body)
        else:
            _remove_quarantined_file(stage, intent_name, intent_body)
        if os.listdir(stage):
            raise _fail("private OCR corpus partial entries remain")
    finally:
        if inputs is not None:
            os.close(inputs)
        if stage is not None:
            os.close(stage)
    os.rmdir(".corpus.partial", dir_fd=private)
    os.fsync(private)


def _reclaim_private_ocr_corpus(private: int) -> None:
    reclaim = _open_child(private, ".corpus.reclaim", create=False)
    inputs = None
    try:
        allowed_root = {
            "manifest.json", ".reclaim-manifest.json", "input"
        }
        entries = set(os.listdir(reclaim))
        if not entries <= allowed_root or "input" not in entries:
            raise _fail("private OCR corpus cleanup state is invalid")
        manifest_name = (
            "manifest.json" if "manifest.json" in entries else ".reclaim-manifest.json"
        )
        manifest_body = _read_at(reclaim, manifest_name)
        manifest = _validate_corpus_manifest(
            parse_strict_json(manifest_body, "private OCR cleanup manifest")
        )
        if _canonical_body(manifest) != manifest_body:
            raise _fail("private OCR corpus cleanup manifest drifted")
        inputs = _open_child(reclaim, "input", create=False)
        expected = {f"{item['item_id']}.pdf": item for item in manifest["documents"]}
        allowed_inputs = set(expected) | {f".reclaim-{name}" for name in expected}
        input_entries = set(os.listdir(inputs))
        if not input_entries <= allowed_inputs:
            raise _fail("private OCR corpus cleanup contains foreign inputs")
        for name, item in expected.items():
            original = name in input_entries
            quarantine = f".reclaim-{name}" in input_entries
            if original and quarantine:
                raise _fail("private OCR corpus cleanup input is ambiguous")
            if not original and not quarantine:
                continue
            selected = name if original else f".reclaim-{name}"
            body = _read_at(inputs, selected, _MAX_SOURCE_BYTES)
            if (
                len(body) != item["size"]
                or "sha256:" + hashlib.sha256(body).hexdigest()
                != item["source_digest"]
            ):
                raise _fail("private OCR corpus cleanup input drifted")
            if original:
                _unlink_exact(inputs, name, body)
            else:
                _remove_quarantined_file(inputs, selected, body)
        if os.listdir(inputs):
            raise _fail("private OCR corpus cleanup inputs remain")
        os.close(inputs)
        inputs = None
        os.rmdir("input", dir_fd=reclaim)
        os.fsync(reclaim)
        if manifest_name == "manifest.json":
            _unlink_exact(reclaim, "manifest.json", manifest_body)
        else:
            _remove_quarantined_file(reclaim, manifest_name, manifest_body)
        if os.listdir(reclaim):
            raise _fail("private OCR corpus cleanup entries remain")
    finally:
        if inputs is not None:
            os.close(inputs)
        os.close(reclaim)
    os.rmdir(".corpus.reclaim", dir_fd=private)
    os.fsync(private)


def cleanup_private_ocr_corpus(*, runtime_root: Path) -> bool:
    """Reclaim only the exact product-owned private OCR corpus transaction."""

    runtime = os.open(
        Path(runtime_root),
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    private = lock = None
    try:
        try:
            private = _open_child(runtime, "private-ocr", create=False)
        except FileNotFoundError:
            return True
        lock = _open_corpus_lock(private, exclusive=True)
        entries = set(os.listdir(private))
        allowed = {
            "corpus", ".corpus.reclaim", ".corpus.partial", ".corpus.lock"
        }
        if not entries <= allowed or {"corpus", ".corpus.reclaim"} <= entries:
            raise _fail("private OCR corpus cleanup state is invalid")
        if ".corpus.partial" in entries:
            if "corpus" in entries or ".corpus.reclaim" in entries:
                raise _fail("private OCR corpus cleanup state is ambiguous")
            _reclaim_private_ocr_partial(private)
        if "corpus" in entries:
            _load_private_ocr_corpus_at(private)
            _rename_noreplace(private, "corpus", ".corpus.reclaim")
            os.fsync(private)
        if ".corpus.reclaim" in set(os.listdir(private)):
            _reclaim_private_ocr_corpus(private)
        if set(os.listdir(private)) - {".corpus.lock"}:
            raise _fail("private OCR corpus cleanup did not reach idle state")
        return True
    except (ContractError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivateOcrUatError):
            raise
        raise _fail("private OCR corpus cleanup failed") from exc
    finally:
        if lock is not None:
            os.close(lock)
        if private is not None:
            os.close(private)
        os.close(runtime)


def _hash_cleanup_file(path: Path, maximum: int) -> tuple[int, str, os.stat_result]:
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1 or not 0 <= before.st_size <= maximum
    ):
        raise _fail("private OCR cleanup tree is invalid")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev, opened.st_ino, opened.st_mode,
            opened.st_nlink, opened.st_size,
        ) != (
            before.st_dev, before.st_ino, before.st_mode,
            before.st_nlink, before.st_size,
        ):
            raise _fail("private OCR cleanup tree drifted")
        digest = hashlib.sha256()
        size = 0
        while size <= maximum:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - size))
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    public_after = os.lstat(path)
    identity = (
        before.st_dev, before.st_ino, before.st_mode, before.st_nlink,
        before.st_size, before.st_mtime_ns,
    )
    if (
        size > maximum or size != before.st_size
        or identity != (
            after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
            after.st_size, after.st_mtime_ns,
        )
        or identity != (
            public_after.st_dev, public_after.st_ino, public_after.st_mode,
            public_after.st_nlink, public_after.st_size, public_after.st_mtime_ns,
        )
    ):
        raise _fail("private OCR cleanup tree drifted")
    return size, "sha256:" + digest.hexdigest(), before


def _cleanup_tree_manifest(root: Path) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    total = 0
    if not os.path.lexists(root):
        return {"root_device": None, "root_inode": None, "entries": records}
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise _fail("private OCR cleanup tree is invalid")
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names.sort()
        files.sort()
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(root)
            info = os.lstat(path)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or len(relative.parts) > _CLEANUP_MAX_DEPTH
                or len(records) >= _CLEANUP_MAX_ENTRIES
            ):
                raise _fail("private OCR cleanup tree is invalid")
            records.append({
                "relative": relative.as_posix(),
                "kind": "directory", "size": 0, "digest": None,
                "device": info.st_dev, "inode": info.st_ino,
            })
        for name in files:
            path = Path(directory) / name
            relative = path.relative_to(root)
            info = os.lstat(path)
            if (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or info.st_size > 1024 * 1024 * 1024
                or len(relative.parts) > _CLEANUP_MAX_DEPTH
                or len(records) >= _CLEANUP_MAX_ENTRIES
            ):
                raise _fail("private OCR cleanup tree is invalid")
            size, digest, opened = _hash_cleanup_file(path, 1024 * 1024 * 1024)
            total += size
            if total > 4 * 1024 * 1024 * 1024:
                raise _fail("private OCR cleanup tree exceeded its bound")
            records.append({
                "relative": relative.as_posix(),
                "kind": "file", "size": size, "digest": digest,
                "device": opened.st_dev, "inode": opened.st_ino,
            })
    return {
        "root_device": root_info.st_dev,
        "root_inode": root_info.st_ino,
        "entries": records,
    }


def _validate_cleanup_records(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list or len(value) > _CLEANUP_MAX_ENTRIES:
        raise _fail("private OCR cleanup intent is invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if (
            type(item) is not dict
            or set(item) != {
                "relative", "kind", "size", "digest", "device", "inode",
            }
            or type(item["relative"]) is not str
            or not item["relative"]
            or item["relative"].startswith("/")
            or any(part in {"", ".", ".."} for part in item["relative"].split("/"))
            or len(item["relative"].split("/")) > _CLEANUP_MAX_DEPTH
            or item["relative"] in seen
            or item["kind"] not in {"directory", "file"}
            or type(item["size"]) is not int
            or item["size"] < 0
            or type(item["device"]) is not int
            or item["device"] < 0
            or type(item["inode"]) is not int
            or item["inode"] < 1
        ):
            raise _fail("private OCR cleanup intent is invalid")
        if item["kind"] == "directory":
            if item["size"] != 0 or item["digest"] is not None:
                raise _fail("private OCR cleanup intent is invalid")
        elif (
            item["size"] > 1024 * 1024 * 1024
            or type(item["digest"]) is not str
            or _DIGEST.fullmatch(item["digest"]) is None
        ):
            raise _fail("private OCR cleanup intent is invalid")
        seen.add(item["relative"])
        result.append(dict(item))
    return result


def _validate_cleanup_tree(value: Any) -> dict[str, Any]:
    if (
        type(value) is not dict
        or set(value) != {"root_device", "root_inode", "entries"}
        or not (
            value["root_device"] is None and value["root_inode"] is None
            or type(value["root_device"]) is int
            and value["root_device"] >= 0
            and type(value["root_inode"]) is int
            and value["root_inode"] >= 1
        )
    ):
        raise _fail("private OCR cleanup intent is invalid")
    entries = _validate_cleanup_records(value["entries"])
    if value["root_device"] is None and entries:
        raise _fail("private OCR cleanup intent is invalid")
    return {
        "root_device": value["root_device"],
        "root_inode": value["root_inode"],
        "entries": entries,
    }


def _validate_cleanup_intent(
    value: Any, *, runtime_root: Path,
) -> tuple[dict[str, Any], PrivateOcrPreparedRun]:
    keys = {
        "schema_version", "batch_id", "batch_manifest_digest",
        "corpus_digest", "qualification_digest", "campaign_origin_digest",
        "staging_name", "run", "corpus_manifest", "trees", "authority",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or value["schema_version"]
        != "ao.lore.private-ocr-cleanup-intent.v0.1"
        or value["authority"] is not False
        or type(value["trees"]) is not dict
        or set(value["trees"]) != {"candidate", "batch", "rasters"}
    ):
        raise _fail("private OCR cleanup intent is invalid")
    run = _run_from_record(
        value["run"], runtime_root=Path(runtime_root),
        corpus_manifest=value["corpus_manifest"],
    )
    if (
        value["batch_id"] != run.batch_id
        or value["batch_manifest_digest"] != run.batch_manifest_digest
        or value["corpus_digest"] != run.corpus_digest
        or value["qualification_digest"] != run.qualification_digest
        or value["campaign_origin_digest"] != run.campaign_origin_digest
        or value["staging_name"] != run.staging_name
    ):
        raise _fail("private OCR cleanup intent is invalid")
    trees = {name: _validate_cleanup_tree(tree) for name, tree in value["trees"].items()}
    detached = {
        **{
            key: copy.deepcopy(value[key])
            for key in keys - {"trees"}
        },
        "trees": trees,
    }
    return detached, run


def load_private_ocr_cleanup_run(
    *, runtime_root: Path, activation_inputs: OcrActivationInputs,
) -> PrivateOcrPreparedRun:
    """Reconstruct one exact cleanup-owned run after its inputs were reclaimed."""

    runtime_path = Path(runtime_root)
    activation = load_private_ocr_activation(
        runtime_root=runtime_path, inputs=activation_inputs,
    )
    runtime = os.open(
        runtime_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    uat = namespace = state = None
    try:
        uat = _open_child(runtime, "uat", create=False)
        namespace = _open_child(uat, "private-ocr", create=False)
        names = os.listdir(namespace)
        if (
            len(names) != 1
            or re.fullmatch(r"batch-private-ocr-[0-9a-f]{16}", names[0])
            is None
        ):
            raise _fail("private OCR cleanup recovery state is invalid")
        state = _open_child(namespace, names[0], create=False)
        allowed = {
            "cleanup-intent.json", "worker-contract-interrupted.json",
            "worker-contract-resume.json", "worker-contract-rerun.json",
            "worker-barrier.json", "calibration-contract.json",
        }
        if (
            "cleanup-intent.json" not in os.listdir(state)
            or not set(os.listdir(state)) <= allowed
        ):
            raise _fail("private OCR cleanup recovery state is invalid")
        body = _read_stable_at(state, "cleanup-intent.json", 4 * 1024 * 1024)
        intent, run = _validate_cleanup_intent(
            parse_strict_json(body, "private OCR cleanup intent"),
            runtime_root=runtime_path,
        )
        if (
            _canonical_body(intent) != body
            or run.batch_id != names[0]
            or run.qualification_digest != activation.qualification_digest
            or any(
                source["selected_candidate_id"] != activation.candidate_id
                for source in run.sources
            )
        ):
            raise _fail("private OCR cleanup recovery state is invalid")
        return run
    except (ContractError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, PrivateOcrUatError):
            raise
        raise _fail("private OCR cleanup recovery state is invalid") from exc
    finally:
        for descriptor in (state, namespace, uat, runtime):
            if descriptor is not None:
                os.close(descriptor)


def _reclaim_cleanup_tree(root: Path, expected_records: Any) -> None:
    expected_tree = _validate_cleanup_tree(expected_records)
    expected = {item["relative"]: item for item in expected_tree["entries"]}
    if not os.path.lexists(root):
        return
    if expected_tree["root_device"] is None:
        raise _fail("private OCR cleanup quarantine drifted")
    current = _cleanup_tree_manifest(root)
    if (
        current["root_device"], current["root_inode"],
    ) != (
        expected_tree["root_device"], expected_tree["root_inode"],
    ):
        raise _fail("private OCR cleanup quarantine drifted")
    root_info = os.lstat(root)
    if (root_info.st_dev, root_info.st_ino) != (
        expected_tree["root_device"], expected_tree["root_inode"],
    ):
        raise _fail("private OCR cleanup quarantine drifted")
    for item in current["entries"]:
        if expected.get(item["relative"]) != item:
            raise _fail("private OCR cleanup quarantine drifted")
    os.chmod(root, 0o700)
    for item in current["entries"]:
        if item["kind"] == "directory":
            os.chmod(root / item["relative"], 0o700)
    for item in reversed(current["entries"]):
        path = root / item["relative"]
        info = os.lstat(path)
        if (info.st_dev, info.st_ino) != (item["device"], item["inode"]):
            raise _fail("private OCR cleanup quarantine drifted")
        if item["kind"] == "file":
            os.unlink(path)
        else:
            os.rmdir(path)
    root_after = os.lstat(root)
    if (root_after.st_dev, root_after.st_ino) != (
        expected_tree["root_device"], expected_tree["root_inode"],
    ):
        raise _fail("private OCR cleanup quarantine drifted")
    os.rmdir(root)


def cleanup_private_ocr_prepared_inputs(
    *, runtime_root: Path, activation_inputs: OcrActivationInputs,
) -> bool:
    """Recover and reclaim an exact pre-run corpus/raster preparation."""

    runtime = Path(runtime_root)
    activation = load_private_ocr_activation(
        runtime_root=runtime, inputs=activation_inputs,
    )
    private_path = runtime / "private-ocr"
    private = os.open(
        private_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    intent_name = "cleanup-intent.json"
    partial_name = "cleanup-intent.partial"
    raster_path = private_path / "rasters"
    reclaim_path = private_path / ".rasters-preparation-reclaim"
    try:
        names = set(os.listdir(private))
        intent: dict[str, Any]
        if intent_name in names:
            if partial_name in names:
                raise _fail("private OCR prepared-input cleanup is ambiguous")
            body = _read_stable_at(private, intent_name, 16 * 1024 * 1024)
            value = parse_strict_json(body, "private OCR prepared-input cleanup")
            if (
                type(value) is not dict
                or set(value) != {
                    "schema_version", "qualification_digest", "corpus_digest",
                    "corpus_manifest", "tree", "authority",
                }
                or value["schema_version"]
                != "ao.lore.private-ocr-prepared-input-cleanup.v0.1"
                or value["qualification_digest"] != activation.qualification_digest
                or value["authority"] is not False
            ):
                raise _fail("private OCR prepared-input cleanup is invalid")
            corpus = _validate_corpus_manifest(value["corpus_manifest"])
            if value["corpus_digest"] != canonical_digest(corpus):
                raise _fail("private OCR prepared-input cleanup is invalid")
            intent = {
                **copy.deepcopy(value),
                "corpus_manifest": corpus,
                "tree": _validate_cleanup_tree(value["tree"]),
            }
            if _canonical_body(intent) != body:
                raise _fail("private OCR prepared-input cleanup is invalid")
        else:
            if reclaim_path.exists():
                raise _fail("private OCR prepared-input quarantine is unauthenticated")
            corpus = load_private_ocr_corpus(runtime_root=runtime)
            expected = {
                f"raster-{item['item_id']}-{item['source_digest'][7:23]}"
                for item in corpus["documents"]
            }
            if not raster_path.is_dir():
                raise _fail("private OCR prepared-input rasters are invalid")
            entries = {path.name for path in raster_path.iterdir()}
            if not entries or not entries <= expected:
                raise _fail("private OCR prepared-input rasters are invalid")
            by_name = {
                f"raster-{item['item_id']}-{item['source_digest'][7:23]}": item
                for item in corpus["documents"]
            }
            allowed_configurations = frozenset({
                OCR_RASTER_CONFIGURATION_DIGEST,
                _LEGACY_OCR_RASTER_CONFIGURATION_DIGEST,
            })
            for name in sorted(entries):
                bundle = load_raster_bundle_for_cleanup(
                    raster_path / name,
                    allowed_configuration_digests=allowed_configurations,
                )
                item = by_name[name]
                if (
                    bundle.source_id != item["item_id"]
                    or bundle.source_digest != item["source_digest"]
                ):
                    raise _fail("private OCR prepared-input raster binding drifted")
            intent = {
                "schema_version":
                "ao.lore.private-ocr-prepared-input-cleanup.v0.1",
                "qualification_digest": activation.qualification_digest,
                "corpus_digest": canonical_digest(corpus),
                "corpus_manifest": copy.deepcopy(corpus),
                "tree": _cleanup_tree_manifest(raster_path),
                "authority": False,
            }
            _publish_cleanup_intent(private, _canonical_body(intent))
        if raster_path.exists() and reclaim_path.exists():
            raise _fail("private OCR prepared-input cleanup is ambiguous")
        if raster_path.exists():
            _rename_noreplace(private, "rasters", reclaim_path.name)
            os.fsync(private)
        _reclaim_cleanup_tree(reclaim_path, intent["tree"])
        if intent_name in set(os.listdir(private)):
            _unlink_exact(private, intent_name, _canonical_body(intent))
        os.fsync(private)
    finally:
        os.close(private)
    cleanup_private_ocr_corpus(runtime_root=runtime)
    return True


def cleanup_private_ocr_campaign(
    run: PrivateOcrPreparedRun,
    activation_inputs: OcrActivationInputs,
    campaign: PreparedOcrCampaignInputs | None,
) -> bool:
    """Restartably reclaim only one exact run's inputs, rasters, and outputs."""

    if type(run) is not PrivateOcrPreparedRun:
        raise _fail("private OCR cleanup run is invalid")
    repository = Path(__file__).resolve().parents[2]
    runtime = Path(run.runtime_root)
    state = runtime / "uat" / "private-ocr" / run.batch_id
    intent_path = state / "cleanup-intent.json"
    candidate_parent = repository / "working" / "candidates"
    batch_parent = runtime / "batches"
    private = runtime / "private-ocr"
    roots = {
        "candidate": candidate_parent / run.staging_name,
        "batch": batch_parent / run.batch_id,
        "rasters": private / "rasters",
    }
    quarantines = {
        "candidate": candidate_parent / f".private-ocr-cleanup-{run.staging_name}",
        "batch": batch_parent / f".private-ocr-cleanup-{run.batch_id}",
        "rasters": private / f".rasters-cleanup-{run.batch_id}",
    }
    present = os.path.lexists
    source = repository / "sources" / run.staging_name
    fully_absent = (
        not present(source)
        and not present(run.control_path)
        and not present(state)
        and not any(present(path) for path in roots.values())
        and not any(present(path) for path in quarantines.values())
        and not present(private / "corpus")
        and not present(private / ".corpus.partial")
        and not present(private / ".corpus.reclaim")
    )
    if fully_absent:
        return True
    state_info = os.lstat(state) if present(state) else None
    completed_except_state = (
        state_info is not None
        and stat.S_ISDIR(state_info.st_mode)
        and not stat.S_ISLNK(state_info.st_mode)
        and not any(state.iterdir())
        and not present(source)
        and not present(run.control_path)
        and not any(present(path) for path in roots.values())
        and not any(present(path) for path in quarantines.values())
        and not present(private / "corpus")
        and not present(private / ".corpus.partial")
        and not present(private / ".corpus.reclaim")
    )
    if completed_except_state:
        os.rmdir(state)
        return True
    intent: dict[str, Any]
    if present(intent_path):
        if present(state / "cleanup-intent.partial"):
            raise _fail("private OCR cleanup state is ambiguous")
        intent_body = _read_stable_path(intent_path, 4 * 1024 * 1024)
        parsed_intent = parse_strict_json(
            intent_body,
            "private OCR cleanup intent",
        )
        intent, intended_run = _validate_cleanup_intent(
            parsed_intent, runtime_root=runtime,
        )
        if (
            _canonical_body(intent) != intent_body
            or intended_run != run
        ):
            raise _fail("private OCR cleanup intent is invalid")
    else:
        if any(present(path) for path in quarantines.values()):
            raise _fail("private OCR cleanup quarantine is unauthenticated")
        _validate_private_ocr_prepared_run(run, activation_inputs)
        if (
            type(campaign) is not PreparedOcrCampaignInputs
            or campaign.corpus_manifest != run.corpus_manifest
            or campaign.corpus_digest != run.corpus_digest
            or campaign.campaign_origin_digest != run.campaign_origin_digest
            or tuple(campaign.sources) != tuple(run.sources)
            or type(campaign.rasters) is not tuple
            or len(campaign.rasters) != 4
        ):
            raise _fail("private OCR cleanup campaign binding drifted")
        checked_campaign = campaign
        raster_names = {raster.root.name for raster in checked_campaign.rasters}
        if set(path.name for path in roots["rasters"].iterdir()) != raster_names:
            raise _fail("private OCR cleanup rasters drifted")
        for raster in checked_campaign.rasters:
            validate_raster_bundle(raster)
        candidate_entries = (
            set(path.name for path in roots["candidate"].iterdir())
            if present(roots["candidate"]) else set()
        )
        batch_final = _load_private_ocr_final(run) if present(roots["batch"]) else None
        if candidate_entries:
            if batch_final is None:
                raise _fail("private OCR cleanup candidate state is unbound")
            snapshot = _private_ocr_candidate_snapshot(run)
            expected_ids = {
                item["candidate_id"] for item in batch_final["items"]
                if item["status"] != "rejected"
            }
            if set(snapshot) != expected_ids or candidate_entries != expected_ids:
                raise _fail("private OCR cleanup candidate state drifted")
            for candidate_id in expected_ids:
                load_verified_candidate(
                    candidate_id, candidate_root=roots["candidate"]
                )
        elif present(roots["candidate"]) and any(roots["candidate"].iterdir()):
            raise _fail("private OCR cleanup candidate state drifted")
        if present(roots["batch"]):
            entries = {path.name for path in roots["batch"].iterdir()}
            if not entries <= {"checkpoint.json", "final-readback.json"}:
                raise _fail("private OCR cleanup batch state drifted")
            checkpoint = _private_ocr_checkpoint(run)
            if entries and checkpoint is None:
                raise _fail("private OCR cleanup batch state drifted")
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        allowed_state = {
            "worker-contract-interrupted.json", "worker-contract-resume.json",
            "worker-contract-rerun.json", "worker-barrier.json",
            "calibration-contract.json", "cleanup-intent.partial",
        }
        if {path.name for path in state.iterdir()} - allowed_state:
            raise _fail("private OCR cleanup run state contains foreign entries")
        intent = {
            "schema_version": "ao.lore.private-ocr-cleanup-intent.v0.1",
            "batch_id": run.batch_id,
            "batch_manifest_digest": run.batch_manifest_digest,
            "corpus_digest": run.corpus_digest,
            "qualification_digest": run.qualification_digest,
            "campaign_origin_digest": run.campaign_origin_digest,
            "staging_name": run.staging_name,
            "run": _run_record(run),
            "corpus_manifest": copy.deepcopy(run.corpus_manifest),
            "trees": {name: _cleanup_tree_manifest(path) for name, path in roots.items()},
            "authority": False,
        }
        body = _canonical_body(intent)
        parent = os.open(
            state, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            _publish_cleanup_intent(parent, body)
        finally:
            os.close(parent)
    for name in ("candidate", "batch", "rasters"):
        source_root = roots[name]
        quarantine = quarantines[name]
        if present(source_root) and present(quarantine):
            raise _fail("private OCR cleanup state is ambiguous")
        if present(source_root):
            parent = source_root.parent
            descriptor = os.open(
                parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                _rename_noreplace(descriptor, source_root.name, quarantine.name)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _reclaim_cleanup_tree(quarantine, intent["trees"][name])
    cleanup_private_ocr_uat_run(run)
    cleanup_private_ocr_corpus(runtime_root=runtime)
    if present(state):
        allowed = {
            "cleanup-intent.json", "worker-contract-interrupted.json",
            "worker-contract-resume.json", "worker-contract-rerun.json",
            "worker-barrier.json", "calibration-contract.json",
        }
        names = {path.name for path in state.iterdir()}
        if not names <= allowed:
            raise _fail("private OCR cleanup run state contains foreign entries")
        for name in sorted(names - {"cleanup-intent.json"}):
            body = _read_stable_path(state / name, 1024 * 1024)
            if not body:
                raise _fail("private OCR cleanup run state is invalid")
            os.unlink(state / name)
        if present(intent_path):
            os.unlink(intent_path)
        os.rmdir(state)
    return True


def build_private_ocr_uat_dependencies(
    *,
    activation_inputs: OcrActivationInputs,
    campaign: PreparedOcrCampaignInputs,
) -> OcrUatDependencies:
    """Bind one exact activation and representative campaign to the shared lifecycle."""

    _validate_activation_inputs(activation_inputs)
    if type(campaign) is not PreparedOcrCampaignInputs:
        raise _fail("private OCR UAT campaign is invalid")
    run_box: dict[str, PrivateOcrPreparedRun] = {}
    calibration_box: dict[str, dict[str, Any]] = {}

    def require_run(value: Any) -> PrivateOcrPreparedRun:
        current = run_box.get("run")
        if type(value) is not PrivateOcrPreparedRun or current is None or value != current:
            raise _fail("private OCR UAT run binding drifted")
        return current

    def load_manifest(runtime: Path) -> Mapping[str, Any]:
        _validate_campaign_inputs(
            campaign, runtime_root=Path(runtime), activation_inputs=activation_inputs,
            allow_prepared_run=bool(run_box),
        )
        return _validate_manifest({
            "corpus_id": campaign.corpus_manifest["corpus_id"],
            "documents": [
                {"item_id": item["item_id"], "source_digest": item["source_digest"]}
                for item in campaign.sources
            ],
        })

    def prepare(manifest: Mapping[str, Any], runtime: Path) -> PrivateOcrPreparedRun:
        if run_box or manifest != load_manifest(Path(runtime)):
            raise _fail("private OCR UAT preparation drifted")
        run = prepare_private_ocr_uat_run(
            runtime_root=Path(runtime),
            activation_inputs=activation_inputs,
            campaign=campaign,
        )
        run_box["run"] = run
        return run

    def validate_run(
        value: Any, manifest: Mapping[str, Any], runtime: Path,
    ) -> PrivateOcrPreparedRun:
        run = require_run(value)
        checked, _activation, _identity = _validate_private_ocr_prepared_run(
            run, activation_inputs
        )
        if Path(runtime) != checked.runtime_root or manifest != load_manifest(Path(runtime)):
            raise _fail("private OCR UAT prepared run drifted")
        return checked

    def conversion_snapshot() -> int:
        run = run_box.get("run")
        if run is None:
            return 0
        checkpoint = _private_ocr_checkpoint(run)
        return 0 if checkpoint is None else checkpoint["processed"]

    def calibration(
        manifest: Mapping[str, Any], corpus_root: Path,
    ) -> Mapping[str, Any]:
        run = require_run(run_box.get("run"))
        if (
            manifest != run.corpus_manifest
            or Path(corpus_root) != run.runtime_root / "private-ocr" / "corpus"
        ):
            raise _fail("private OCR calibration input drifted")
        process = _launch_private_ocr_calibration_worker(run, activation_inputs)
        result = _wait_private_ocr_calibration_worker(process)
        calibration_box["result"] = copy.deepcopy(result)
        return {
            "attempts_per_item": result["attempts_per_item"],
            "aggregate_digest": result["aggregate_digest"],
        }

    def assemble(
        value: Any,
        terminal: Mapping[str, Any],
        checkpoint: Mapping[str, Any],
        brain: str,
        calibration_summary: Any,
        counts: Mapping[str, int],
    ) -> dict[str, Any]:
        run = require_run(value)
        detailed = calibration_box.get("result")
        if (
            detailed is None
            or calibration_summary != {
                "attempts_per_item": detailed["attempts_per_item"],
                "aggregate_digest": detailed["aggregate_digest"],
            }
            or checkpoint.get("processed") != 1
            or counts != {"initial": 1, "resumed": 3, "rerun": 0}
            or type(terminal) is not dict
            or type(terminal.get("items")) is not list
            or len(terminal["items"]) != 4
        ):
            raise _fail("private OCR terminal assembly drifted")
        successful = sum(item.get("status") != "rejected" for item in terminal["items"])
        rejected = sum(item.get("status") == "rejected" for item in terminal["items"])
        report = {
            "schema_version": "ao.lore.private-ocr-uat-readback.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "qualification_digest": run.qualification_digest,
            "corpus_digest": run.corpus_digest,
            "aggregate_digest": detailed["aggregate_digest"],
            "initial_conversions": 1,
            "resumed_conversions": 3,
            "rerun_conversions": 0,
            "successful_documents": successful,
            "rejected_documents": rejected,
            "calibration_attempts_per_item": detailed["attempts_per_item"],
            "decision": detailed["decision"],
            "brain_before_digest": brain,
            "brain_after_work_digest": brain,
            "brain_after_cleanup_digest": brain,
            **{field: False for field in AUTHORITY_FIELDS},
        }
        return validate_uat_readback(report)

    def persist(
        value: Any, report: Mapping[str, Any],
        brain_after_work: str, brain_after_cleanup: str,
    ) -> str:
        run = require_run(value)
        detailed = calibration_box.get("result")
        if detailed is None:
            raise _fail("private OCR retained calibration is absent")
        prepared = PreparedOcrUat(
            runtime_root=run.runtime_root,
            batch_id=run.batch_id,
            batch_manifest_digest=run.batch_manifest_digest,
            corpus_digest=run.corpus_digest,
            qualification_digest=run.qualification_digest,
            campaign_origin_digest=run.campaign_origin_digest,
        )
        return persist_private_ocr_evidence(
            prepared,
            report,
            calibration_result=detailed,
            brain_before=report["brain_before_digest"],
            brain_after_work=brain_after_work,
            brain_after_cleanup=brain_after_cleanup,
        )

    def require_verified_process(process: Any, phase: str) -> None:
        if (
            phase not in {"interrupted", "resume", "rerun"}
            or getattr(process, "_ao_lore_phase", None) != phase
            or getattr(process, "_ao_lore_sandbox_verified", None) is not True
        ):
            raise _fail("private OCR worker sandbox proof is absent")

    shared = PrivateDocumentUatDependencies(
        policy=PRIVATE_OCR_UAT_POLICY,
        prepare_run=prepare,
        launch_process=lambda value, phase: _launch_private_ocr_worker(
            require_run(value), activation_inputs, phase
        ),
        calibration=calibration,
        cleanup=lambda value: cleanup_private_ocr_campaign(
            require_run(value), activation_inputs, campaign
        ),
        brain_snapshot=_default_brain_snapshot,
        candidate_snapshot=lambda: _private_ocr_candidate_snapshot(
            run_box.get("run")
        ),
        load_manifest=load_manifest,
        validate_prepared_run=validate_run,
        project_run=lambda value: project_private_ocr_prepared_run(
            require_run(value), activation_inputs
        ),
        conversion_snapshot=conversion_snapshot,
        process_poll=lambda process: process.poll(),
        inspect_checkpoint=lambda value: _private_ocr_checkpoint(require_run(value)),
        inspect_barrier=lambda value, observed: _inspect_private_ocr_barrier(
            require_run(value), observed
        ),
        send_signal=_signal_private_ocr_worker,
        terminate_process=_terminate_private_ocr_worker,
        wait_process=_wait_private_ocr_worker,
        load_final=lambda value: _load_private_ocr_final(require_run(value)),
        load_queue=lambda: _private_ocr_candidate_snapshot(run_box.get("run")),
        verify_terminal=lambda value, stdout, loaded: _verify_private_ocr_terminal(
            require_run(value), stdout, loaded
        ),
        verify_queue=_verify_private_ocr_queue,
        assemble_readback=assemble,
        persist_readback=persist,
        require_verified_process=require_verified_process,
        expected_item_ids=lambda manifest: [
            item["item_id"] for item in _validate_manifest(manifest)["documents"]
        ],
    )
    return OcrUatDependencies(shared=shared)


def run_private_ocr_uat(
    *,
    runtime_root: Path | None = None,
    dependencies: OcrUatDependencies,
) -> dict[str, object]:
    """Run and validate one exact private OCR interrupted/resume/rerun campaign."""

    if type(dependencies) is not OcrUatDependencies:
        raise _fail("private OCR UAT dependencies are invalid")
    if dependencies.shared.policy != PRIVATE_OCR_UAT_POLICY:
        raise _fail("private OCR UAT dependencies are invalid")
    try:
        report = run_private_document_uat(
            runtime_root=runtime_root,
            dependencies=dependencies.shared,
        )
        return validate_uat_readback(copy.deepcopy(report))
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if isinstance(exc, PrivateOcrUatError) and str(exc) == "private OCR UAT failed":
            raise
        raise PrivateOcrUatError("private OCR UAT failed") from exc


_FIXED_OCR_RUNTIME_DIGEST = (
    "sha256:6002a11549b0bcb58e23edb4e98badfc58677f149e1533d6b1d11ec4ffe142bf"
)
_PRIVATE_OCR_ACTIONS = {
    "prepare-runtime", "qualify", "prepare-corpus", "run", "cleanup",
}


def _execute_private_ocr_action(action: str) -> str:
    runtime = runtime_home()
    repository = Path(__file__).resolve().parents[2]
    sealed_runtime = repository / ".ao-lore" / "ocr-runtime" / "ocr-runtime-v0.1"
    if action == "prepare-runtime":
        validate_ocr_runtime_tree_digest(sealed_runtime, _FIXED_OCR_RUNTIME_DIGEST)
        return "prepared"
    if action == "qualify":
        inputs = load_private_ocr_activation_inputs(runtime_root=runtime)
        load_private_ocr_activation(runtime_root=runtime, inputs=inputs)
        return "qualified"
    if action == "prepare-corpus":
        inputs = load_private_ocr_activation_inputs(runtime_root=runtime)
        prepare_private_ocr_corpus(runtime_root=runtime)
        renderer_body = _read_stable_path(
            Path(__file__).with_name("ocr_raster_worker.py"), 1024 * 1024
        )
        renderer_digest = "sha256:" + hashlib.sha256(renderer_body).hexdigest()
        prepare_private_ocr_campaign_inputs(
            runtime_root=runtime,
            activation_inputs=inputs,
            renderer_digest=renderer_digest,
            renderer=lambda body, media_type, configuration: render_in_ocr_sandbox(
                body, media_type, configuration, runtime_root=sealed_runtime
            ),
        )
        return "prepared"
    if action == "run":
        inputs = load_private_ocr_activation_inputs(runtime_root=runtime)
        campaign = load_private_ocr_campaign_inputs(runtime_root=runtime)
        run_private_ocr_uat(
            runtime_root=runtime,
            dependencies=build_private_ocr_uat_dependencies(
                activation_inputs=inputs, campaign=campaign
            ),
        )
        return "completed"
    if action == "cleanup":
        rasters = runtime / "private-ocr" / "rasters"
        prepared_cleanup = runtime / "private-ocr" / "cleanup-intent.json"
        prepared_cleanup_partial = runtime / "private-ocr" / "cleanup-intent.partial"
        cleanup_state = runtime / "uat" / "private-ocr"
        runs = runtime / "private-ocr" / "runs"
        if (
            not os.path.lexists(runs)
            and any(os.path.lexists(path) for path in (
                rasters, prepared_cleanup, prepared_cleanup_partial,
            ))
        ):
            inputs = load_private_ocr_activation_inputs(runtime_root=runtime)
            cleanup_private_ocr_prepared_inputs(
                runtime_root=runtime, activation_inputs=inputs,
            )
        elif os.path.lexists(rasters):
            inputs = load_private_ocr_activation_inputs(runtime_root=runtime)
            campaign = load_private_ocr_campaign_inputs(runtime_root=runtime)
            run = (
                load_private_ocr_prepared_run(
                    runtime_root=runtime,
                    activation_inputs=inputs,
                    campaign=campaign,
                )
                if os.path.lexists(runs)
                else prepare_private_ocr_uat_run(
                    runtime_root=runtime,
                    activation_inputs=inputs,
                    campaign=campaign,
                )
            )
            cleanup_private_ocr_campaign(run, inputs, campaign)
        elif os.path.lexists(cleanup_state):
            inputs = load_private_ocr_activation_inputs(runtime_root=runtime)
            run = load_private_ocr_cleanup_run(
                runtime_root=runtime, activation_inputs=inputs,
            )
            cleanup_private_ocr_campaign(run, inputs, None)
        else:
            cleanup_private_ocr_corpus(runtime_root=runtime)
        return "cleaned"
    raise _fail("private OCR operator action is invalid")


def project_private_ocr_action(action: str) -> dict[str, object]:
    """Execute one fixed private OCR action and expose only a safe projection."""

    if type(action) is not str or action not in _PRIVATE_OCR_ACTIONS:
        raise _fail("private OCR operator action is invalid")
    status = _execute_private_ocr_action(action)
    expected = {
        "prepare-runtime": "prepared",
        "qualify": "qualified",
        "prepare-corpus": "prepared",
        "run": "completed",
        "cleanup": "cleaned",
    }[action]
    if type(status) is not str or status != expected:
        raise _fail("private OCR operator result is invalid")
    return {
        "action": action,
        "status": status,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }
