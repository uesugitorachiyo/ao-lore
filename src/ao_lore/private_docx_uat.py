"""Strict preparation and readback contracts for private DOCX UAT."""

from __future__ import annotations

import json
import hashlib
import ctypes
import errno
import fcntl
import math
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
from typing import Any, Mapping, Sequence

from ._strict_io import parse_strict_json, strict_read_json
from .batch_ingestion import validate_batch_manifest, validate_checkpoint
from .benchmark import canonical_digest
from .docx_ooxml import (
    DOCX_ACCEPTED_DOCUMENT_COUNT,
    DOCX_ACTIVE_CONTENT_REJECTION_COUNT,
    DOCX_EXPECTED_REJECTION_COUNT,
    DOCX_INVALID_PACKAGE_REJECTION_COUNT,
    DOCX_FLOORS,
    DOCX_PARSER_ID,
    DOCX_PARSER_VERSION,
)
from .home import repository_root, runtime_home
from .private_document_uat import (
    PrivateDocumentCampaignPolicy,
    PrivateDocumentPreparedRun,
    PrivateDocumentUatDependencies,
    private_document_policy_digest,
    private_document_state_body,
    run_private_document_uat,
)
from .private_docx_domain import (
    DOCX_MIME,
    DOCX_PRIVATE_CORPUS_ID,
    DOCX_TRANSFORMATION_ID,
    DOCX_UAT_ITEM_IDS,
    MAX_DOCX_BYTES,
    _load_private_docx_manifest_at,
    _manifest_body,
    _open_directory,
    _open_private_lock,
    _open_relative_directory,
    _open_runtime_root,
    _persist_bytes_at,
    _read_file_at,
    _rename_noreplace_at,
    _rollback_name,
    _unlink_verified_at,
    _binding_exists_at,
    load_private_docx_manifest,
)


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STAGING_PREFIX = "private-docx-uat-"
_BATCH_PREFIX = "batch-private-docx-uat-"
_CONTROL_PREFIX = ".private-docx-uat-"
_STATE_DIRECTORY = ("uat", "private-docx")
_INTENT_NAME = "preparation-intent.json"
_PREPARED_EVIDENCE_NAME = "prepared-run-evidence.json"
_WORKER_CONTRACT_NAME = "worker-run-contract.json"
_WORKER_BARRIER_NAME = "barrier-ready.json"
_WORKER_SCHEMA_VERSION = "ao.lore.private-docx-uat-worker-run.v0.1"
_SANDBOX_SCHEMA_VERSION = "ao.lore.private-docx-uat-sandbox.v0.1"
_BWRAP = "/usr/bin/bwrap"
_WORKER_STDOUT_MAX_BYTES = 1024 * 1024
_WORKER_STDERR_MAX_BYTES = 1024 * 1024
_WORKER_TIMEOUT_SECONDS = 300.0
_CALIBRATION_TIMEOUT_SECONDS = 300.0
_CLEANUP_QUARANTINE_RE = re.compile(
    r"^\.private-docx-cleanup-(candidates|batch|staging|control|corpus|state)-[0-9a-f]{24}$"
)
_CALIBRATION_ITEM_TIMEOUT_SECONDS = 30.0
_IMMUTABLE_CONTRACT_PATH = "/run/ao-lore-private-docx-contract.json"
_CALIBRATION_CONTRACT_NAME = "calibration-run-contract.json"
_CALIBRATION_RESULT_NAME = "calibration-result.json"
_CALIBRATION_CONTRACT_SCHEMA_VERSION = "ao.lore.private-docx-calibration-run.v0.1"
_CALIBRATION_RESULT_SCHEMA_VERSION = "ao.lore.private-docx-calibration-result.v0.1"
_RESOURCE_LIMITS = {
    "cpu_seconds": 60,
    "address_space_bytes": 2 * 1024 * 1024 * 1024,
    "data_bytes": 1024 * 1024 * 1024,
    "file_size_bytes": 64 * 1024 * 1024,
    "open_files": 64,
    "processes": 1,
}
_PHASE_ARITHMETIC = {
    "interrupted": (0, 1, 1),
    "resume": (1, 4, 3),
    "rerun": (4, 4, 0),
}
_DENIED_CHILD_SYSCALLS = ("clone", "clone3", "fork", "vfork")


PRIVATE_DOCX_UAT_POLICY = PrivateDocumentCampaignPolicy(
    format_id="docx",
    corpus_id=DOCX_PRIVATE_CORPUS_ID,
    media_type=DOCX_MIME,
    extension=".docx",
    parser_id=DOCX_PARSER_ID,
    parser_version=DOCX_PARSER_VERSION,
    worker_module="ao_lore.private_docx_uat_worker",
    calibration_worker_module="ao_lore.private_docx_calibration_worker",
    evidence_namespace="private-docx-uat",
    validate_manifest=lambda value: _strict_copy(value),
    validate_qualification=lambda value: _strict_copy(value),
    validate_calibration=lambda value: _validate_private_docx_calibration_evidence(value),
)
PRIVATE_DOCX_UAT_POLICY_DIGEST = private_document_policy_digest(
    PRIVATE_DOCX_UAT_POLICY
)


class PrivateDocxUatError(ValueError):
    """Raised when private DOCX UAT state drifts from its exact contract."""


@dataclass(frozen=True)
class PrivateDocxPreparedRun:
    batch_manifest: Mapping[str, Any]
    batch_manifest_digest: str
    corpus_manifest: Mapping[str, Any]
    corpus_digest: str
    expectation_digest: str
    staging_name: str
    staging_identity: tuple[int, int]
    control_identity: tuple[int, int]
    journal_identity: tuple[int, int]
    sources: tuple[Mapping[str, str], ...]
    runtime_root: Path
    corpus_root: Path


@dataclass(frozen=True)
class PrivateDocxCalibrationEvidence:
    """Detached origin bindings for one private DOCX calibration result."""

    aggregate: Mapping[str, Any]
    corpus_digest: str
    expectation_digest: str
    qualification_digest: str
    configuration_digest: str
    source_set_digest: str
    derived_set_digest: str
    transformation_id: str
    sandbox_digest: str


class _FrozenDict(dict):
    """JSON-compatible mapping that rejects mutation after validation."""

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("validated private DOCX calibration evidence is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


def _freeze_json(value: Any) -> Any:
    if type(value) is dict:
        return _FrozenDict({key: _freeze_json(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _strict_score(value: Any) -> float:
    if type(value) not in {int, float}:
        raise _fail("private DOCX calibration aggregate is invalid")
    numeric = float(value)
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
        raise _fail("private DOCX calibration aggregate is invalid")
    return numeric


def _strict_nonnegative_number(value: Any) -> int | float:
    if type(value) not in {int, float}:
        raise _fail("private DOCX calibration aggregate is invalid")
    if not math.isfinite(float(value)) or value < 0:
        raise _fail("private DOCX calibration aggregate is invalid")
    return value


def _validate_private_docx_calibration_aggregate(value: Any) -> Mapping[str, Any]:
    """Validate and detach the authoritative six-key DOCX aggregate."""

    if isinstance(value, _FrozenDict):
        value = json.loads(json.dumps(value, allow_nan=False))
    if type(value) is not dict or set(value) != {
        "metrics", "exclusions", "stable_failures", "latency_seconds",
        "peak_memory_bytes", "repeatability",
    }:
        raise _fail("private DOCX calibration aggregate is invalid")
    supplied_metrics = value.get("metrics")
    if type(supplied_metrics) is not dict or any(
        type(supplied_metrics.get(name)) not in {int, float}
        for name in (
            "text_fidelity", "structural_fidelity", "source_location_fidelity",
            "expected_outcome_accuracy",
        )
    ):
        raise _fail("private DOCX calibration aggregate is invalid")
    detached = _strict_copy(value)
    metrics = detached["metrics"]
    metric_keys = {
        "text_fidelity", "structural_fidelity", "source_location_fidelity",
        "expected_outcome_accuracy", "expected_rejection_count",
        "accepted_document_count",
    }
    if type(metrics) is not dict or set(metrics) != metric_keys:
        raise _fail("private DOCX calibration aggregate is invalid")
    normalized_metrics = {
        name: _strict_score(metrics[name])
        for name in (
            "text_fidelity", "structural_fidelity", "source_location_fidelity",
            "expected_outcome_accuracy",
        )
    }
    for name, expected in (
        ("expected_rejection_count", DOCX_EXPECTED_REJECTION_COUNT),
        ("accepted_document_count", DOCX_ACCEPTED_DOCUMENT_COUNT),
    ):
        if type(metrics[name]) is not int or metrics[name] != expected:
            raise _fail("private DOCX calibration aggregate is invalid")
        normalized_metrics[name] = metrics[name]
    exclusions = detached["exclusions"]
    if (
        type(exclusions) is not list
        or any(type(item) is not str or not item for item in exclusions)
        or len(exclusions) != len(set(exclusions))
    ):
        raise _fail("private DOCX calibration aggregate is invalid")
    failures = detached["stable_failures"]
    if (
        type(failures) is not dict
        or set(failures) != {
            "invalid_package", "unsupported_active_content", "representative_unexpected"
        }
        or type(failures["unsupported_active_content"]) is not int
        or failures["unsupported_active_content"] != DOCX_ACTIVE_CONTENT_REJECTION_COUNT
        or type(failures["invalid_package"]) is not int
        or failures["invalid_package"] != DOCX_INVALID_PACKAGE_REJECTION_COUNT
        or type(failures["representative_unexpected"]) is not int
        or not 0 <= failures["representative_unexpected"] <= 8
    ):
        raise _fail("private DOCX calibration aggregate is invalid")
    summaries: dict[str, dict[str, int | float]] = {}
    for name in ("latency_seconds", "peak_memory_bytes"):
        supplied = detached[name]
        if type(supplied) is not dict or set(supplied) != {"min", "mean", "max"}:
            raise _fail("private DOCX calibration aggregate is invalid")
        normalized = {
            key: _strict_nonnegative_number(supplied[key])
            for key in ("min", "mean", "max")
        }
        if not normalized["min"] <= normalized["mean"] <= normalized["max"]:
            raise _fail("private DOCX calibration aggregate is invalid")
        if name == "peak_memory_bytes" and (
            type(normalized["min"]) is not int or type(normalized["max"]) is not int
        ):
            raise _fail("private DOCX calibration aggregate is invalid")
        summaries[name] = normalized
    repeatability = detached["repeatability"]
    if (
        type(repeatability) is not dict
        or set(repeatability) != {"runs", "identical", "score"}
        or type(repeatability["runs"]) is not int
        or repeatability["runs"] != 2
        or type(repeatability["identical"]) is not bool
    ):
        raise _fail("private DOCX calibration aggregate is invalid")
    normalized_repeatability = {
        "runs": 2,
        "identical": repeatability["identical"],
        "score": _strict_score(repeatability["score"]),
    }
    if normalized_repeatability["score"] != (
        1.0 if normalized_repeatability["identical"] else 0.0
    ):
        raise _fail("private DOCX calibration aggregate is invalid")
    normalized = {
        "metrics": normalized_metrics,
        "exclusions": list(exclusions),
        "stable_failures": dict(failures),
        "latency_seconds": summaries["latency_seconds"],
        "peak_memory_bytes": summaries["peak_memory_bytes"],
        "repeatability": normalized_repeatability,
    }
    return _freeze_json(normalized)


def _validate_private_docx_calibration_evidence(
    value: Any,
) -> PrivateDocxCalibrationEvidence:
    if not isinstance(value, PrivateDocxCalibrationEvidence):
        raise _fail("private DOCX calibration evidence is invalid")
    digests = (
        value.corpus_digest, value.expectation_digest, value.qualification_digest,
        value.configuration_digest, value.source_set_digest, value.derived_set_digest,
        value.sandbox_digest,
    )
    if (
        any(type(item) is not str or _DIGEST_RE.fullmatch(item) is None for item in digests)
        or value.transformation_id != DOCX_TRANSFORMATION_ID
    ):
        raise _fail("private DOCX calibration evidence is invalid")
    return PrivateDocxCalibrationEvidence(
        aggregate=_validate_private_docx_calibration_aggregate(value.aggregate),
        corpus_digest=value.corpus_digest,
        expectation_digest=value.expectation_digest,
        qualification_digest=value.qualification_digest,
        configuration_digest=value.configuration_digest,
        source_set_digest=value.source_set_digest,
        derived_set_digest=value.derived_set_digest,
        transformation_id=value.transformation_id,
        sandbox_digest=value.sandbox_digest,
    )


def derive_private_docx_tuning_decision(value: Any) -> str:
    """Derive only hold, candidate_change, or investigate from sealed evidence."""

    aggregate = _validate_private_docx_calibration_aggregate(value)
    if (
        aggregate["stable_failures"]["unsupported_active_content"]
        != DOCX_ACTIVE_CONTENT_REJECTION_COUNT
        or aggregate["stable_failures"]["invalid_package"]
        != DOCX_INVALID_PACKAGE_REJECTION_COUNT
        or aggregate["stable_failures"]["representative_unexpected"] != 0
        or not aggregate["repeatability"]["identical"]
        or aggregate["metrics"]["expected_outcome_accuracy"] != 1.0
    ):
        return "investigate"
    weak = [
        name for name, floor in DOCX_FLOORS.items()
        if aggregate["metrics"][name] < floor
    ]
    return "candidate_change" if weak else "hold"


def _fail(message: str = "private DOCX UAT contract is invalid") -> PrivateDocxUatError:
    return PrivateDocxUatError(message)


def _control_directory(staging_name: str) -> str:
    if re.fullmatch(r"private-docx-uat-[0-9a-f]{24}", staging_name) is None:
        raise _fail()
    return _CONTROL_PREFIX + staging_name.removeprefix(_STAGING_PREFIX)


def _expected_source_bindings(
    corpus_manifest: Mapping[str, Any],
) -> tuple[dict[str, str], ...]:
    return tuple(
        {
            "item_id": document["item_id"],
            "original_digest": document["source_digest"],
            "derived_digest": document["derived_digest"],
            "transformation_id": document["transformation_id"],
        }
        for document in corpus_manifest["documents"]
    )


def _strict_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    if not isinstance(decoded, dict):
        raise _fail()
    return decoded


def _read_exact_file(path: Path, maximum: int) -> bytes:
    info = os.lstat(path)
    if (
        not path.exists()
        or path.is_symlink()
        or not path.is_file()
        or info.st_nlink != 1
        or info.st_size <= 0
        or info.st_size > maximum
    ):
        raise _fail("private DOCX source staging is invalid")
    body = path.read_bytes()
    if len(body) != info.st_size:
        raise _fail("private DOCX source staging is invalid")
    return body


def _corpus_root(runtime_root: Path | None) -> Path:
    root = Path(runtime_root) if runtime_root is not None else runtime_home()
    return root / "private-docx" / "corpus"


def _persist_empty_file_at(directory_descriptor: int, name: str) -> None:
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        info = os.fstat(descriptor)
        if info.st_size != 0:
            raise _fail()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    verify = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if verify.st_size != 0:
        raise _fail()
    os.fsync(directory_descriptor)


def _state_root(runtime_root: Path | None) -> Path:
    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    return runtime.joinpath(*_STATE_DIRECTORY)


def _persist_uat_document(
    directory: Path,
    runtime: Path,
    name: str,
    value: Mapping[str, Any],
) -> bytes:
    body = _manifest_body(value, 64 * 1024)
    descriptor = _open_directory(directory, runtime, create=True)
    temporary = f".{name}-{secrets.token_hex(12)}.tmp"
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
                raise OSError("private DOCX UAT document short write")
            view = view[written:]
        os.fsync(file_descriptor)
        os.close(file_descriptor)
        file_descriptor = None
        os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
        if _read_file_at(descriptor, name, 64 * 1024) != body:
            raise OSError("private DOCX UAT document reopen drifted")
        return body
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        try:
            os.unlink(temporary, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def _prepared_run_evidence(value: "PrivateDocxPreparedRun") -> dict[str, Any]:
    return {
        "schema_version": "ao.lore.private-docx-uat-prepared-run.v0.1",
        "batch_manifest": _strict_copy(value.batch_manifest),
        "batch_manifest_digest": value.batch_manifest_digest,
        "corpus_manifest": _strict_copy(value.corpus_manifest),
        "corpus_digest": value.corpus_digest,
        "expectation_digest": value.expectation_digest,
        "staging_name": value.staging_name,
        "staging_identity": list(value.staging_identity),
        "control_identity": list(value.control_identity),
        "journal_identity": list(value.journal_identity),
        "sources": [dict(item) for item in value.sources],
        "authority": False,
    }


def _validate_identity(value: Any) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(isinstance(part, bool) or not isinstance(part, int) or part < 0 for part in value)
    ):
        raise _fail()
    return value[0], value[1]


def _validate_prepared_run_evidence(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {
            "schema_version",
            "batch_manifest",
            "batch_manifest_digest",
            "corpus_manifest",
            "corpus_digest",
            "expectation_digest",
            "staging_name",
            "staging_identity",
            "control_identity",
            "journal_identity",
            "sources",
            "authority",
        }
        or value.get("schema_version") != "ao.lore.private-docx-uat-prepared-run.v0.1"
        or value.get("authority") is not False
    ):
        raise _fail()
    result = _strict_copy(value)
    batch_manifest = validate_batch_manifest(result["batch_manifest"])
    if not isinstance(result["corpus_manifest"], Mapping):
        raise _fail()
    corpus_manifest = _strict_copy(result["corpus_manifest"])
    if (
        result["batch_manifest_digest"] != canonical_digest(batch_manifest)
        or canonical_digest(corpus_manifest) != result["corpus_digest"]
        or type(result["corpus_digest"]) is not str
        or _DIGEST_RE.fullmatch(result["corpus_digest"]) is None
        or type(result["expectation_digest"]) is not str
        or _DIGEST_RE.fullmatch(result["expectation_digest"]) is None
        or re.fullmatch(r"private-docx-uat-[0-9a-f]{24}", result["staging_name"]) is None
        or not isinstance(result["sources"], list)
        or [item["item_id"] for item in batch_manifest["documents"]]
        != [item["item_id"] for item in corpus_manifest["documents"]]
        or tuple(result["sources"]) != _expected_source_bindings(corpus_manifest)
    ):
        raise _fail()
    _validate_identity(result["staging_identity"])
    _validate_identity(result["control_identity"])
    _validate_identity(result["journal_identity"])
    return result


def _load_prepared_run_evidence(directory: Path, runtime: Path) -> tuple[dict[str, Any], bytes]:
    value, body = strict_read_json(
        directory / _PREPARED_EVIDENCE_NAME,
        "private DOCX UAT prepared evidence",
        max_bytes=64 * 1024,
        root=runtime,
    )
    return _validate_prepared_run_evidence(value), body


def _prepared_run_from_evidence(
    value: Mapping[str, Any], runtime_root: Path
) -> PrivateDocxPreparedRun:
    validated = _validate_prepared_run_evidence(value)
    return PrivateDocxPreparedRun(
        batch_manifest=_strict_copy(validated["batch_manifest"]),
        batch_manifest_digest=validated["batch_manifest_digest"],
        corpus_manifest=_strict_copy(validated["corpus_manifest"]),
        corpus_digest=validated["corpus_digest"],
        expectation_digest=validated["expectation_digest"],
        staging_name=validated["staging_name"],
        staging_identity=_validate_identity(validated["staging_identity"]),
        control_identity=_validate_identity(validated["control_identity"]),
        journal_identity=_validate_identity(validated["journal_identity"]),
        sources=tuple(dict(item) for item in validated["sources"]),
        runtime_root=runtime_root,
        corpus_root=runtime_root / "private-docx" / "corpus",
    )


def _persist_prepared_run_evidence(value: PrivateDocxPreparedRun, runtime: Path) -> bytes:
    return _persist_uat_document(
        runtime / "uat" / "private-docx" / value.batch_manifest["batch_id"],
        runtime,
        _PREPARED_EVIDENCE_NAME,
        _prepared_run_evidence(value),
    )


def _unlink_empty_file_at(directory_descriptor: int, name: str) -> None:
    info = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if info.st_size != 0:
        raise _fail()
    os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)


def _validate_empty_journal_at(
    directory_descriptor: int,
    name: str,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    info = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        info.st_size != 0
        or info.st_nlink != 1
    ):
        raise _fail()
    descriptor = os.open(
        name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_descriptor
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_size != 0
            or opened.st_nlink != 1
            or (expected_identity is not None and expected_identity != (opened.st_dev, opened.st_ino))
            or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise _fail()
        after = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            after.st_size != 0
            or after.st_nlink != 1
            or (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise _fail()
        return opened.st_dev, opened.st_ino
    finally:
        os.close(descriptor)


def _expected_stage_bodies(
    manifest: Mapping[str, Any], corpus_descriptor: int
) -> dict[str, bytes]:
    bodies: dict[str, bytes] = {}
    for document in manifest["documents"]:
        body = _read_file_at(corpus_descriptor, document["derived_name"], MAX_DOCX_BYTES)
        if len(body) != document["derived_bytes"]:
            raise _fail()
        bodies[f"{document['item_id']}.docx"] = body
    return bodies


def _intent_value(
    *,
    staging_name: str,
    batch_manifest: Mapping[str, Any],
    corpus_manifest: Mapping[str, Any],
    states: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": "ao.lore.private-docx-uat-preparation.v0.1",
        "phase": "planned",
        "staging_name": staging_name,
        "batch_manifest": _strict_copy(batch_manifest),
        "batch_manifest_digest": canonical_digest(batch_manifest),
        "corpus_manifest_digest": canonical_digest(corpus_manifest),
        "expectation_digest": corpus_manifest["expectation_digest"],
        "states": _strict_copy(states),
        "authority": False,
    }


def _validate_intent(
    value: Mapping[str, Any],
    *,
    staging_name: str,
    batch_manifest: Mapping[str, Any],
    corpus_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {
            "schema_version",
            "phase",
            "staging_name",
            "batch_manifest",
            "batch_manifest_digest",
            "corpus_manifest_digest",
            "expectation_digest",
            "states",
            "authority",
        }
    ):
        raise _fail()
    intent = _strict_copy(value)
    if (
        intent["schema_version"] != "ao.lore.private-docx-uat-preparation.v0.1"
        or intent["phase"] != "planned"
        or intent["staging_name"] != staging_name
        or intent["batch_manifest_digest"] != canonical_digest(batch_manifest)
        or canonical_digest(intent["batch_manifest"]) != intent["batch_manifest_digest"]
        or intent["corpus_manifest_digest"] != canonical_digest(corpus_manifest)
        or intent["expectation_digest"] != corpus_manifest["expectation_digest"]
        or intent["authority"] is not False
        or not isinstance(intent["states"], Mapping)
        or set(intent["states"]) != {"staging", "control", "journal"}
    ):
        raise _fail()
    for state in intent["states"].values():
        if (
            not isinstance(state, Mapping)
            or set(state) != {"phase", "identity", "pending", "created"}
            or state["phase"] not in {"planned", "creating", "created", "reclaiming", "completed"}
            or (state["identity"] is not None and not isinstance(state["identity"], list))
            or (state["pending"] is not None and not isinstance(state["pending"], str))
            or not isinstance(state["created"], list)
        ):
            raise _fail()
    return intent


def _validate_existing_prepared_run(
    *,
    runtime: Path,
    repo_root: Path,
    prepared: PrivateDocxPreparedRun,
    expected_batch_manifest: Mapping[str, Any],
    expected_corpus_manifest: Mapping[str, Any],
    expected_bodies: Mapping[str, bytes],
) -> PrivateDocxPreparedRun:
    if (
        prepared.batch_manifest_digest != canonical_digest(expected_batch_manifest)
        or prepared.corpus_digest != canonical_digest(expected_corpus_manifest)
        or prepared.expectation_digest != expected_corpus_manifest["expectation_digest"]
        or _strict_copy(prepared.batch_manifest) != _strict_copy(expected_batch_manifest)
        or _strict_copy(prepared.corpus_manifest) != _strict_copy(expected_corpus_manifest)
        or tuple(dict(item) for item in prepared.sources)
        != _expected_source_bindings(expected_corpus_manifest)
    ):
        raise _fail()
    source_descriptor = _open_directory(repo_root / "sources" / prepared.staging_name, repo_root, create=False)
    control_descriptor = _open_directory(
        repo_root / "sources" / _control_directory(prepared.staging_name),
        repo_root,
        create=False,
    )
    journal_root = runtime / "uat" / "private-docx" / prepared.batch_manifest["batch_id"]
    journal_descriptor = _open_directory(journal_root, runtime, create=False)
    try:
        if prepared.staging_identity != (os.fstat(source_descriptor).st_dev, os.fstat(source_descriptor).st_ino):
            raise _fail()
        if prepared.control_identity != (os.fstat(control_descriptor).st_dev, os.fstat(control_descriptor).st_ino):
            raise _fail()
        if set(os.listdir(journal_descriptor)) != {"conversion-journal.jsonl", _PREPARED_EVIDENCE_NAME}:
            raise _fail()
        journal_identity = _validate_empty_journal_at(
            journal_descriptor,
            "conversion-journal.jsonl",
            expected_identity=prepared.journal_identity,
        )
        if prepared.journal_identity != journal_identity:
            raise _fail()
        if set(os.listdir(source_descriptor)) != set(expected_bodies):
            raise _fail()
        for name, body in expected_bodies.items():
            if _read_file_at(source_descriptor, name, MAX_DOCX_BYTES) != body:
                raise _fail()
        if set(os.listdir(control_descriptor)) != {"run-manifest.json"}:
            raise _fail()
        if _read_file_at(control_descriptor, "run-manifest.json", 64 * 1024) != _manifest_body(expected_batch_manifest):
            raise _fail()
        evidence, _ = _load_prepared_run_evidence(journal_root, runtime)
        if _prepared_run_from_evidence(evidence, runtime) != prepared:
            raise _fail()
        return prepared
    finally:
        os.close(journal_descriptor)
        os.close(control_descriptor)
        os.close(source_descriptor)


def _validate_live_prepared_run(
    run: PrivateDocxPreparedRun,
    manifest: Mapping[str, Any],
    runtime: Path,
) -> PrivateDocxPreparedRun:
    if (
        run.runtime_root != runtime
        or run.corpus_root != runtime / "private-docx" / "corpus"
        or run.corpus_digest != canonical_digest(manifest)
        or _strict_copy(run.corpus_manifest) != _strict_copy(manifest)
        or tuple(dict(item) for item in run.sources) != _expected_source_bindings(manifest)
    ):
        raise _fail("private DOCX UAT prepared run drifted")
    source = _open_directory(
        repository_root() / "sources" / run.staging_name,
        repository_root(), create=False,
    )
    control = _open_directory(
        repository_root() / "sources" / _control_directory(run.staging_name),
        repository_root(), create=False,
    )
    state_path = runtime / "uat" / "private-docx" / run.batch_manifest["batch_id"]
    state = _open_directory(state_path, runtime, create=False)
    try:
        if (
            run.staging_identity != (os.fstat(source).st_dev, os.fstat(source).st_ino)
            or run.control_identity != (os.fstat(control).st_dev, os.fstat(control).st_ino)
            or set(os.listdir(source)) != {f"{item['item_id']}.docx" for item in run.sources}
            or set(os.listdir(control)) != {"run-manifest.json"}
        ):
            raise _fail("private DOCX UAT prepared run drifted")
        for item in run.sources:
            body = _read_file_at(source, f"{item['item_id']}.docx", MAX_DOCX_BYTES)
            if "sha256:" + hashlib.sha256(body).hexdigest() != item["derived_digest"]:
                raise _fail("private DOCX UAT prepared source drifted")
        if _read_file_at(control, "run-manifest.json", 64 * 1024) != _manifest_body(run.batch_manifest):
            raise _fail("private DOCX UAT prepared manifest drifted")
        allowed = {
            _PREPARED_EVIDENCE_NAME, "conversion-journal.jsonl",
            _WORKER_CONTRACT_NAME, _WORKER_BARRIER_NAME,
            "sandbox-home", "sandbox-cache",
        }
        entries = set(os.listdir(state))
        if not {_PREPARED_EVIDENCE_NAME, "conversion-journal.jsonl"} <= entries or not entries <= allowed:
            raise _fail("private DOCX UAT state entries drifted")
        evidence, _ = _load_prepared_run_evidence(state_path, runtime)
        if _prepared_run_from_evidence(evidence, runtime) != run:
            raise _fail("private DOCX UAT prepared evidence drifted")
        _conversion_journal_count(run, runtime_root=runtime)
        return run
    finally:
        os.close(state)
        os.close(control)
        os.close(source)


def _recover_preparation_intent(
    *,
    runtime: Path,
    repo_root: Path,
    batch_manifest: Mapping[str, Any],
    corpus_manifest: Mapping[str, Any],
    expected_bodies: Mapping[str, bytes],
    staging_name: str,
) -> None:
    state_root = _state_root(runtime)
    preparation_path = state_root / _INTENT_NAME
    if not preparation_path.exists():
        return
    intent, intent_body = strict_read_json(
        preparation_path,
        "private DOCX UAT preparation intent",
        max_bytes=64 * 1024,
        root=runtime,
    )
    working = _validate_intent(
        intent,
        staging_name=staging_name,
        batch_manifest=batch_manifest,
        corpus_manifest=corpus_manifest,
    )

    def persist() -> bytes:
        return _persist_uat_document(state_root, runtime, _INTENT_NAME, working)

    def reclaim_source() -> None:
        source_root = repo_root / "sources" / staging_name
        if not source_root.exists():
            return
        source_descriptor = _open_directory(source_root, repo_root, create=False)
        try:
            identity = working["states"]["staging"]["identity"]
            if identity is not None and tuple(identity) != (os.fstat(source_descriptor).st_dev, os.fstat(source_descriptor).st_ino):
                raise _fail()
            working["states"]["staging"]["phase"] = "reclaiming"
            persist()
            actual = set(os.listdir(source_descriptor))
            expected_entries = set(expected_bodies) | {
                _rollback_name(name) for name in expected_bodies
            }
            if not actual.issubset(expected_entries):
                raise _fail()
            for name, body in expected_bodies.items():
                if name in actual:
                    working["states"]["staging"]["pending"] = f"delete:{name}"
                    persist()
                    _unlink_verified_at(source_descriptor, name, body, MAX_DOCX_BYTES)
                    working["states"]["staging"]["pending"] = None
                    persist()
                rollback = _rollback_name(name)
                if rollback in actual:
                    working["states"]["staging"]["pending"] = f"delete:{rollback}"
                    persist()
                    _unlink_verified_at(source_descriptor, rollback, body, MAX_DOCX_BYTES)
                    working["states"]["staging"]["pending"] = None
                    persist()
        finally:
            os.close(source_descriptor)
        os.rmdir(source_root)
        working["states"]["staging"]["phase"] = "completed"
        persist()

    def reclaim_control() -> None:
        control_root = repo_root / "sources" / _control_directory(staging_name)
        if not control_root.exists():
            return
        control_descriptor = _open_directory(control_root, repo_root, create=False)
        try:
            identity = working["states"]["control"]["identity"]
            if identity is not None and tuple(identity) != (os.fstat(control_descriptor).st_dev, os.fstat(control_descriptor).st_ino):
                raise _fail()
            working["states"]["control"]["phase"] = "reclaiming"
            persist()
            entries = set(os.listdir(control_descriptor))
            expected_entries = {"run-manifest.json", _rollback_name("run-manifest.json")}
            if not entries.issubset(expected_entries):
                raise _fail()
            if "run-manifest.json" in entries:
                working["states"]["control"]["pending"] = "delete:run-manifest.json"
                persist()
                _unlink_verified_at(control_descriptor, "run-manifest.json", _manifest_body(batch_manifest), 64 * 1024)
                working["states"]["control"]["pending"] = None
                persist()
            rollback = _rollback_name("run-manifest.json")
            if rollback in entries:
                working["states"]["control"]["pending"] = f"delete:{rollback}"
                persist()
                _unlink_verified_at(control_descriptor, rollback, _manifest_body(batch_manifest), 64 * 1024)
                working["states"]["control"]["pending"] = None
                persist()
        finally:
            os.close(control_descriptor)
        os.rmdir(control_root)
        working["states"]["control"]["phase"] = "completed"
        persist()

    def reclaim_journal() -> None:
        journal_root = runtime / "uat" / "private-docx" / batch_manifest["batch_id"]
        if not journal_root.exists():
            return
        journal_descriptor = _open_directory(journal_root, runtime, create=False)
        try:
            identity = working["states"]["journal"]["identity"]
            if identity is not None and tuple(identity) != (os.fstat(journal_descriptor).st_dev, os.fstat(journal_descriptor).st_ino):
                raise _fail()
            entries = set(os.listdir(journal_descriptor))
            if not entries.issubset({"conversion-journal.jsonl", _PREPARED_EVIDENCE_NAME}):
                raise _fail()
            working["states"]["journal"]["phase"] = "reclaiming"
            persist()
            if "conversion-journal.jsonl" in entries:
                working["states"]["journal"]["pending"] = "delete:conversion-journal.jsonl"
                persist()
                _validate_empty_journal_at(
                    journal_descriptor,
                    "conversion-journal.jsonl",
                    expected_identity=None,
                )
                _unlink_empty_file_at(journal_descriptor, "conversion-journal.jsonl")
                working["states"]["journal"]["pending"] = None
                persist()
            if _PREPARED_EVIDENCE_NAME in entries:
                working["states"]["journal"]["pending"] = f"delete:{_PREPARED_EVIDENCE_NAME}"
                persist()
                evidence_body = _load_prepared_run_evidence(journal_root, runtime)[1]
                _unlink_verified_at(journal_descriptor, _PREPARED_EVIDENCE_NAME, evidence_body, 64 * 1024)
                working["states"]["journal"]["pending"] = None
                persist()
        finally:
            os.close(journal_descriptor)
        os.rmdir(journal_root)
        working["states"]["journal"]["phase"] = "completed"
        final_body = persist()
        intent_descriptor = _open_directory(state_root, runtime, create=False)
        try:
            _unlink_verified_at(intent_descriptor, _INTENT_NAME, final_body, 64 * 1024)
        finally:
            os.close(intent_descriptor)

    reclaim_source()
    reclaim_control()
    reclaim_journal()


def prepare_private_docx_uat_run(*, runtime_root: Path | None = None) -> PrivateDocxPreparedRun:
    """Prepare one crash-recoverable DOCX UAT run from the persisted derived corpus."""
    root_descriptor: int | None = None
    private_descriptor: int | None = None
    lock_descriptor: int | None = None
    corpus_descriptor: int | None = None
    sources_root_descriptor: int | None = None
    source_descriptor: int | None = None
    control_descriptor: int | None = None
    journal_descriptor: int | None = None
    try:
        runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
        _, root_descriptor = _open_runtime_root(runtime_root)
        private_descriptor = _open_relative_directory(root_descriptor, ("private-docx",), create=False)
        lock_descriptor = _open_private_lock(private_descriptor, exclusive=False)
        corpus_descriptor = _open_relative_directory(private_descriptor, ("corpus",), create=False)
        manifest = _load_private_docx_manifest_at(corpus_descriptor)
        corpus_digest = canonical_digest(manifest)
        token = corpus_digest.removeprefix("sha256:")[:24]
        batch_id = _BATCH_PREFIX + token

        repo_root = repository_root()
        expected_bodies = _expected_stage_bodies(manifest, corpus_descriptor)
        source_bindings = _expected_source_bindings(manifest)

        journal_root = runtime / "uat" / "private-docx" / batch_id
        evidence_path = journal_root / _PREPARED_EVIDENCE_NAME
        intent_path = _state_root(runtime) / _INTENT_NAME
        if evidence_path.exists():
            retained_evidence, _ = _load_prepared_run_evidence(journal_root, runtime)
            staging_name = retained_evidence["staging_name"]
        elif intent_path.exists():
            retained_intent, _ = strict_read_json(
                intent_path,
                "private DOCX UAT preparation intent",
                max_bytes=64 * 1024,
                root=runtime,
            )
            staging_name = retained_intent.get("staging_name")
            if type(staging_name) is not str or re.fullmatch(
                r"private-docx-uat-[0-9a-f]{24}", staging_name
            ) is None:
                raise _fail()
        else:
            staging_name = _STAGING_PREFIX + secrets.token_hex(12)

        batch_documents = [
            {
                "item_id": document["item_id"],
                "source": f"sources/{staging_name}/{document['item_id']}.docx",
                "source_digest": document["derived_digest"],
            }
            for document in manifest["documents"]
        ]
        batch_manifest = validate_batch_manifest(
            {
                "schema_version": "ao.lore.ingest-batch-manifest.v0.2",
                "batch_id": batch_id,
                "format_id": "docx",
                "media_type": DOCX_MIME,
                "parser_id": DOCX_PARSER_ID,
                "parser_version": DOCX_PARSER_VERSION,
                "documents": batch_documents,
                "continue_on_error": True,
            }
        )
        batch_manifest_digest = canonical_digest(batch_manifest)

        _recover_preparation_intent(
            runtime=runtime,
            repo_root=repo_root,
            batch_manifest=batch_manifest,
            corpus_manifest=manifest,
            expected_bodies=expected_bodies,
            staging_name=staging_name,
        )

        if journal_root.joinpath(_PREPARED_EVIDENCE_NAME).exists():
            evidence, _ = _load_prepared_run_evidence(journal_root, runtime)
            prepared = _prepared_run_from_evidence(evidence, runtime)
            return _validate_existing_prepared_run(
                runtime=runtime,
                repo_root=repo_root,
                prepared=prepared,
                expected_batch_manifest=batch_manifest,
                expected_corpus_manifest=manifest,
                expected_bodies=expected_bodies,
            )

        state_root = _state_root(runtime)
        states = {
            name: {"phase": "planned", "identity": None, "pending": None, "created": []}
            for name in ("staging", "control", "journal")
        }
        intent_body = _persist_uat_document(
            state_root,
            runtime,
            _INTENT_NAME,
            _intent_value(
                staging_name=staging_name,
                batch_manifest=batch_manifest,
                corpus_manifest=manifest,
                states=states,
            ),
        )

        def persist_intent() -> bytes:
            nonlocal intent_body
            intent_body = _persist_uat_document(
                state_root,
                runtime,
                _INTENT_NAME,
                _intent_value(
                    staging_name=staging_name,
                    batch_manifest=batch_manifest,
                    corpus_manifest=manifest,
                    states=states,
                ),
            )
            return intent_body

        if (repo_root / "sources" / staging_name).exists():
            raise _fail()
        if (repo_root / "sources" / _control_directory(staging_name)).exists():
            raise _fail()
        if journal_root.exists():
            raise _fail()

        sources_root_descriptor = _open_directory(repo_root / "sources", repo_root, create=True)
        states["staging"]["phase"] = "creating"
        persist_intent()
        source_descriptor = _open_directory(repo_root / "sources" / staging_name, repo_root, create=True)
        source_info = os.fstat(source_descriptor)
        states["staging"].update({"phase": "created", "identity": [source_info.st_dev, source_info.st_ino]})
        persist_intent()

        states["control"]["phase"] = "creating"
        persist_intent()
        control_descriptor = _open_directory(
            repo_root / "sources" / _control_directory(staging_name), repo_root, create=True
        )
        control_info = os.fstat(control_descriptor)
        states["control"].update({"phase": "created", "identity": [control_info.st_dev, control_info.st_ino]})
        persist_intent()

        for name, body in expected_bodies.items():
            states["staging"]["pending"] = name
            persist_intent()
            _persist_bytes_at(source_descriptor, name, body)
            states["staging"]["pending"] = None
            states["staging"]["created"].append(name)
            persist_intent()

        states["control"]["pending"] = "run-manifest.json"
        persist_intent()
        _persist_bytes_at(control_descriptor, "run-manifest.json", _manifest_body(batch_manifest))
        states["control"]["pending"] = None
        states["control"]["created"].append("run-manifest.json")
        persist_intent()

        states["journal"]["phase"] = "creating"
        persist_intent()
        journal_descriptor = _open_directory(journal_root, runtime, create=True)
        journal_root_info = os.fstat(journal_descriptor)
        states["journal"].update({"phase": "created", "identity": [journal_root_info.st_dev, journal_root_info.st_ino]})
        persist_intent()
        states["journal"]["pending"] = "conversion-journal.jsonl"
        persist_intent()
        _persist_empty_file_at(journal_descriptor, "conversion-journal.jsonl")
        states["journal"]["pending"] = None
        states["journal"]["created"].append("conversion-journal.jsonl")
        persist_intent()

        journal_file_info = os.stat("conversion-journal.jsonl", dir_fd=journal_descriptor, follow_symlinks=False)
        prepared = PrivateDocxPreparedRun(
            batch_manifest=_strict_copy(batch_manifest),
            batch_manifest_digest=batch_manifest_digest,
            corpus_manifest=_strict_copy(manifest),
            corpus_digest=corpus_digest,
            expectation_digest=manifest["expectation_digest"],
            staging_name=staging_name,
            staging_identity=(source_info.st_dev, source_info.st_ino),
            control_identity=(control_info.st_dev, control_info.st_ino),
            journal_identity=(journal_file_info.st_dev, journal_file_info.st_ino),
            sources=source_bindings,
            runtime_root=runtime,
            corpus_root=runtime / "private-docx" / "corpus",
        )
        states["journal"]["pending"] = _PREPARED_EVIDENCE_NAME
        persist_intent()
        _persist_prepared_run_evidence(prepared, runtime)
        states["journal"]["pending"] = None
        states["journal"]["created"].append(_PREPARED_EVIDENCE_NAME)
        intent_descriptor = _open_directory(state_root, runtime, create=False)
        try:
            _unlink_verified_at(intent_descriptor, _INTENT_NAME, persist_intent(), 64 * 1024)
        finally:
            os.close(intent_descriptor)
        return prepared
    except Exception as exc:
        if isinstance(exc, PrivateDocxUatError):
            raise
        raise _fail() from exc
    finally:
        for descriptor in (
            journal_descriptor,
            control_descriptor,
            source_descriptor,
            sources_root_descriptor,
            corpus_descriptor,
            lock_descriptor,
            private_descriptor,
            root_descriptor,
        ):
            if descriptor is not None:
                os.close(descriptor)


def _worker_run_contract(
    run: PrivateDocxPreparedRun,
    phase: str,
    *,
    qualification: Mapping[str, Any],
    sandbox: Mapping[str, Any],
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    """Build the exact immutable contract consumed by one DOCX worker."""

    if phase not in _PHASE_ARITHMETIC:
        raise _fail("private DOCX UAT worker phase is invalid")
    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    starting_processed, terminal_processed, conversion_delta = _validate_phase_start(
        run, phase, runtime_root=runtime
    )
    qualification_value = _strict_copy(qualification)
    from .docx_ooxml import DocxLimits, docx_configuration_digest, validate_docx_benchmark

    validate_docx_benchmark(
        qualification_value,
        expected_configuration_digest=docx_configuration_digest(DocxLimits()),
        expected_corpus_digest=run.expectation_digest,
    )
    if qualification_value.get("decision") != "hold":
        raise _fail("private DOCX UAT qualification is invalid")
    state = runtime / "uat" / "private-docx" / run.batch_manifest["batch_id"]
    evidence, evidence_body = _load_prepared_run_evidence(state, runtime)
    if _prepared_run_from_evidence(evidence, runtime) != run:
        raise _fail("private DOCX UAT prepared evidence drifted")
    qualification_path = runtime / "private-docx" / "qualification.json"
    qualification_body = _read_exact_file(qualification_path, 8 * 1024 * 1024)
    try:
        decoded_qualification = json.loads(qualification_body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise _fail("private DOCX UAT qualification is invalid") from exc
    if decoded_qualification != qualification_value:
        raise _fail("private DOCX UAT qualification drifted")
    qualification_info = os.stat(qualification_path, follow_symlinks=False)
    control_name = _control_directory(run.staging_name)
    manifest_path = repository_root() / "sources" / control_name / "run-manifest.json"
    control_descriptor = _open_directory(manifest_path.parent, repository_root(), create=False)
    try:
        control_info = os.fstat(control_descriptor)
    finally:
        os.close(control_descriptor)
    if (control_info.st_dev, control_info.st_ino) != run.control_identity:
        raise _fail("private DOCX UAT worker control directory drifted")
    manifest_body = _read_exact_file(manifest_path, 64 * 1024)
    if manifest_body != _manifest_body(run.batch_manifest):
        raise _fail("private DOCX UAT worker manifest drifted")
    manifest_info = os.stat(manifest_path, follow_symlinks=False)
    sources = [
        {
            "item_id": item["item_id"],
            "derived_digest": item["derived_digest"],
            "original_digest": item["original_digest"],
            "transformation_id": item["transformation_id"],
        }
        for item in run.sources
    ]
    if (
        [item["item_id"] for item in sources]
        != [item["item_id"] for item in run.batch_manifest["documents"]]
        or [item["derived_digest"] for item in sources]
        != [item["source_digest"] for item in run.batch_manifest["documents"]]
    ):
        raise _fail("private DOCX UAT worker sources drifted")
    contract: dict[str, Any] = {
        "schema_version": _WORKER_SCHEMA_VERSION,
        "format_id": "docx",
        "policy_digest": PRIVATE_DOCX_UAT_POLICY_DIGEST,
        "phase": phase,
        "starting_processed": starting_processed,
        "terminal_processed": terminal_processed,
        "conversion_delta": conversion_delta,
        "batch_id": run.batch_manifest["batch_id"],
        "batch_manifest_digest": run.batch_manifest_digest,
        "manifest_locator": f"sources/{control_name}/run-manifest.json",
        "control_identity": {"device": control_info.st_dev, "inode": control_info.st_ino},
        "manifest_identity": {"device": manifest_info.st_dev, "inode": manifest_info.st_ino},
        "manifest_body_digest": "sha256:" + hashlib.sha256(manifest_body).hexdigest(),
        "qualification_locator": "private-docx/qualification.json",
        "qualification_identity": {
            "device": qualification_info.st_dev,
            "inode": qualification_info.st_ino,
        },
        "qualification_body_digest": "sha256:" + hashlib.sha256(qualification_body).hexdigest(),
        "qualification_digest": canonical_digest(qualification_value),
        "expectation_digest": run.expectation_digest,
        "corpus_digest": run.corpus_digest,
        "prepared_evidence_digest": canonical_digest(evidence),
        "prepared_evidence_body_digest": "sha256:" + hashlib.sha256(evidence_body).hexdigest(),
        "staging_name": run.staging_name,
        "staging_identity": {"device": run.staging_identity[0], "inode": run.staging_identity[1]},
        "journal_locator": (
            f"uat/private-docx/{run.batch_manifest['batch_id']}/conversion-journal.jsonl"
        ),
        "journal_identity": {"device": run.journal_identity[0], "inode": run.journal_identity[1]},
        "barrier_locator": (
            f"uat/private-docx/{run.batch_manifest['batch_id']}/{_WORKER_BARRIER_NAME}"
        ),
        "sources": sources,
        "sandbox": _strict_copy(sandbox),
        "authority": False,
    }
    contract["run_digest"] = canonical_digest(contract)
    return contract


def _sandbox_mount(path: Path, name: str, mode: str) -> dict[str, Any]:
    try:
        before = os.lstat(path)
        if stat.S_ISLNK(before.st_mode) or not (
            stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)
        ):
            raise OSError("invalid mount type")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if stat.S_ISDIR(before.st_mode):
            flags |= os.O_DIRECTORY
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("mount identity drifted")
    except OSError as exc:
        raise _fail("private DOCX UAT sandbox mount is invalid") from exc
    return {
        "name": name,
        "target": str(path),
        "mode": mode,
        "identity": {"device": opened.st_dev, "inode": opened.st_ino},
    }


def _require_socket_free_tree(root: Path) -> None:
    entries = 0
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    root_descriptor = os.open(root, flags)

    def walk(descriptor: int, depth: int) -> None:
        nonlocal entries
        if depth > 64:
            raise OSError("sandbox mount depth exceeded")
        for name in os.listdir(descriptor):
            entries += 1
            if entries > 100_000 or name in {".", ".."} or "/" in name or "\x00" in name:
                raise OSError("sandbox mount entries exceeded")
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISSOCK(info.st_mode):
                raise OSError("sandbox mount contains a socket")
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                child = os.open(name, flags, dir_fd=descriptor)
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise OSError("sandbox mount drifted")
                    walk(child, depth + 1)
                finally:
                    os.close(child)

    try:
        walk(root_descriptor, 0)
    finally:
        os.close(root_descriptor)


def _docx_sandbox_spec(
    run: PrivateDocxPreparedRun,
    phase: str,
    *,
    command: Sequence[str],
    pythonpath: str,
    bwrap_version: str,
    runtime_root: Path | None = None,
    contract_fd: int = 97,
    seccomp_fd: int = 98,
) -> dict[str, Any]:
    """Construct the exact minimal native-DOCX worker sandbox."""

    if (
        phase not in {"interrupted", "resume", "rerun"}
        or type(command) not in {list, tuple}
        or not command
        or any(type(part) is not str or not part or "\x00" in part for part in command)
        or command[-3:-1] != [sys.executable, "-m"]
        or command[-1]
        not in {"ao_lore.private_docx_uat_worker", "tests.private_docx_uat_worker_fixture"}
        or type(pythonpath) is not str
        or not pythonpath
        or re.fullmatch(r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", bwrap_version) is None
        or type(contract_fd) is not int
        or contract_fd < 3
        or type(seccomp_fd) is not int
        or seccomp_fd < 3
        or contract_fd == seccomp_fd
    ):
        raise _fail("private DOCX UAT sandbox command is invalid")
    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    state = runtime / "uat" / "private-docx" / run.batch_manifest["batch_id"]
    batch = runtime / "batches" / run.batch_manifest["batch_id"]
    candidates = repository_root() / "working" / "candidates" / run.staging_name
    home = state / "sandbox-home"
    cache = state / "sandbox-cache"
    for path in (batch, home, cache):
        descriptor = _open_directory(path, runtime, create=True)
        os.close(descriptor)
    candidates.mkdir(mode=0o700, parents=False, exist_ok=True)
    if candidates.is_symlink() or not candidates.is_dir():
        raise _fail("private DOCX UAT candidate state is invalid")
    repository = repository_root()
    read_only_trees = [
        repository / "src",
        repository / "sources" / run.staging_name,
        repository / "sources" / _control_directory(run.staging_name),
        batch,
        candidates,
        state,
    ]
    if command[-1] == "tests.private_docx_uat_worker_fixture":
        read_only_trees.append(repository / "tests")
    try:
        for tree in read_only_trees:
            _require_socket_free_tree(tree)
    except OSError as exc:
        raise _fail("private DOCX UAT sandbox mount is invalid") from exc
    mounts = [
        _sandbox_mount(Path("/usr"), "system-usr", "read-only"),
        _sandbox_mount(Path("/etc"), "system-etc", "read-only"),
        _sandbox_mount(repository / "src", "ao-lore-src", "read-only"),
    ]
    if command[-1] == "tests.private_docx_uat_worker_fixture":
        mounts.append(_sandbox_mount(repository / "tests", "ao-lore-tests", "read-only"))
    mounts.extend(
        [
            _sandbox_mount(
                repository / "sources" / run.staging_name,
                "staged-sources",
                "read-only",
            ),
            _sandbox_mount(
                repository / "sources" / _control_directory(run.staging_name),
                "control-manifest",
                "read-only",
            ),
            _sandbox_mount(
                runtime / "private-docx" / "qualification.json",
                "qualification",
                "read-only",
            ),
            _sandbox_mount(batch, "batch-state", "read-write"),
            _sandbox_mount(candidates, "candidate-state", "read-write"),
            _sandbox_mount(state, "run-state", "read-write"),
        ]
    )
    environment = {
        "AO_LORE_HOME": str(runtime),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PWD": str(repository),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": pythonpath,
        "XDG_CACHE_HOME": str(cache),
    }
    argv = [
        _BWRAP, "--die-with-parent", "--unshare-net", "--unshare-pid",
        "--tmpfs", "/", "--dir", "/run",
        "--perms", "0400", "--ro-bind-data", str(contract_fd),
        _IMMUTABLE_CONTRACT_PATH, "--seccomp", str(seccomp_fd),
    ]
    for mount in mounts:
        argv.extend(
            (
                "--bind" if mount["mode"] == "read-write" else "--ro-bind",
                mount["target"],
                mount["target"],
            )
        )
    argv.extend(
        (
            "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--symlink", "usr/sbin", "/sbin",
            "--dev", "/dev",
            "--proc", "/proc",
            "--tmpfs", "/tmp",
            "--tmpfs", str(home),
            "--tmpfs", str(cache),
            "--clearenv",
        )
    )
    for key in sorted(environment):
        if key != "PWD":
            argv.extend(("--setenv", key, environment[key]))
    limit_argv = [
        "/usr/bin/prlimit",
        f"--cpu={_RESOURCE_LIMITS['cpu_seconds']}:{_RESOURCE_LIMITS['cpu_seconds']}",
        f"--as={_RESOURCE_LIMITS['address_space_bytes']}:{_RESOURCE_LIMITS['address_space_bytes']}",
        f"--data={_RESOURCE_LIMITS['data_bytes']}:{_RESOURCE_LIMITS['data_bytes']}",
        f"--fsize={_RESOURCE_LIMITS['file_size_bytes']}:{_RESOURCE_LIMITS['file_size_bytes']}",
        f"--nofile={_RESOURCE_LIMITS['open_files']}:{_RESOURCE_LIMITS['open_files']}",
        f"--nproc={_RESOURCE_LIMITS['processes']}:{_RESOURCE_LIMITS['processes']}",
        "--core=0:0",
        "--",
    ]
    argv.extend(("--chdir", str(repository), "--", *limit_argv, *command))
    return {
        "schema_version": _SANDBOX_SCHEMA_VERSION,
        "mechanism": _BWRAP,
        "version": bwrap_version,
        "argv": argv,
        "network_namespace": "unshared",
        "root_filesystem": "allowlisted",
        "mounts": mounts,
        "environment": environment,
        "immutable_contract": {
            "path": _IMMUTABLE_CONTRACT_PATH,
            "fd": contract_fd,
            "sealed": True,
        },
        "resource_limits": dict(_RESOURCE_LIMITS),
        "child_process_policy": "seccomp-deny-clone-fork-vfork-clone3",
        "seccomp": {"fd": seccomp_fd, "denied_syscalls": list(_DENIED_CHILD_SYSCALLS)},
        "authority": False,
    }


def _write_all(descriptor: int, body: bytes, label: str) -> None:
    view = memoryview(body)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError(f"private DOCX UAT {label} short write")
        view = view[written:]


def _seal_descriptor(descriptor: int) -> None:
    seals = (
        fcntl.F_SEAL_WRITE
        | fcntl.F_SEAL_GROW
        | fcntl.F_SEAL_SHRINK
        | fcntl.F_SEAL_SEAL
    )
    fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
    if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != seals:
        raise OSError("private DOCX UAT launch descriptor sealing failed")
    os.lseek(descriptor, 0, os.SEEK_SET)


def _seccomp_descriptor() -> int:
    descriptor = os.memfd_create(
        "ao-lore-private-docx-seccomp",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    library: Any | None = None
    context: int | None = None
    try:
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
        library.seccomp_init.argtypes = (ctypes.c_uint32,)
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_rule_add.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint,
        )
        library.seccomp_rule_add.restype = ctypes.c_int
        library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
        library.seccomp_syscall_resolve_name.restype = ctypes.c_int
        library.seccomp_export_bpf.argtypes = (ctypes.c_void_p, ctypes.c_int)
        library.seccomp_export_bpf.restype = ctypes.c_int
        library.seccomp_release.argtypes = (ctypes.c_void_p,)
        context = library.seccomp_init(0x7FFF0000)
        if not context:
            raise OSError("private DOCX UAT seccomp initialization failed")
        action = 0x00050000 | errno.EPERM
        for name in _DENIED_CHILD_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0 or library.seccomp_rule_add(context, action, number, 0) != 0:
                raise OSError("private DOCX UAT seccomp rule failed")
        if library.seccomp_export_bpf(context, descriptor) != 0:
            raise OSError("private DOCX UAT seccomp export failed")
        os.fsync(descriptor)
        _seal_descriptor(descriptor)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        if library is not None and context:
            library.seccomp_release(context)


def _probe_docx_sandbox() -> str:
    try:
        info = os.lstat(_BWRAP)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OSError("bubblewrap executable is invalid")
        version_result = subprocess.run(
            [_BWRAP, "--version"],
            cwd=repository_root(),
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5.0,
            check=False,
        )
        version = version_result.stdout.decode("ascii", errors="strict").strip()
        if (
            version_result.returncode != 0
            or version_result.stderr
            or re.fullmatch(r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", version) is None
        ):
            raise OSError("bubblewrap version is invalid")
        probe = subprocess.run(
            [
                _BWRAP, "--unshare-net", "--tmpfs", "/",
                "--ro-bind", "/usr", "/usr", "--ro-bind", "/etc", "/etc",
                "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
                "--symlink", "usr/lib64", "/lib64", "--dev", "/dev",
                "--proc", "/proc", "--", "/usr/bin/true",
            ],
            cwd=repository_root(),
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            check=False,
        )
        if probe.returncode != 0 or probe.stdout or len(probe.stderr) > 4096:
            raise OSError("bubblewrap isolation probe failed")
        return version
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise _fail("private DOCX UAT sandbox is unavailable") from exc


def _launch_private_docx_worker(
    run: PrivateDocxPreparedRun,
    phase: str,
    *,
    qualification: Mapping[str, Any],
    runtime_root: Path | None = None,
    module: str = "ao_lore.private_docx_uat_worker",
    pythonpath: str | None = None,
) -> subprocess.Popen[bytes]:
    if module not in {
        "ao_lore.private_docx_uat_worker",
        "tests.private_docx_uat_worker_fixture",
    }:
        raise _fail("private DOCX UAT worker module is invalid")
    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    _validate_phase_start(run, phase, runtime_root=runtime)
    version = _probe_docx_sandbox()
    contract_descriptor = os.memfd_create(
        "ao-lore-private-docx-contract",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    seccomp_descriptor: int | None = None
    try:
        seccomp_descriptor = _seccomp_descriptor()
        sandbox = _docx_sandbox_spec(
            run,
            phase,
            command=[sys.executable, "-m", module],
            pythonpath=pythonpath or str(repository_root() / "src"),
            bwrap_version=version,
            runtime_root=runtime,
            contract_fd=contract_descriptor,
            seccomp_fd=seccomp_descriptor,
        )
        contract = _worker_run_contract(
            run,
            phase,
            qualification=qualification,
            sandbox=sandbox,
            runtime_root=runtime,
        )
        contract_path = (
            runtime / "uat" / "private-docx" / run.batch_manifest["batch_id"]
            / _WORKER_CONTRACT_NAME
        )
        if phase == "interrupted" and contract_path.with_name(_WORKER_BARRIER_NAME).exists():
            raise _fail("private DOCX UAT worker barrier is stale")
        contract_body = _persist_uat_document(
            contract_path.parent, runtime, contract_path.name, contract
        )
        _write_all(contract_descriptor, contract_body, "immutable contract")
        os.fsync(contract_descriptor)
        _seal_descriptor(contract_descriptor)
        process = subprocess.Popen(
            sandbox["argv"],
            cwd=repository_root(),
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            shell=False,
            pass_fds=(contract_descriptor, seccomp_descriptor),
        )
        process._ao_lore_process_group = process.pid  # type: ignore[attr-defined]
    finally:
        os.close(contract_descriptor)
        if seccomp_descriptor is not None:
            os.close(seccomp_descriptor)
    process._ao_lore_contract_path = contract_path  # type: ignore[attr-defined]
    process._ao_lore_contract_body = contract_body  # type: ignore[attr-defined]
    process._ao_lore_runtime_root = runtime  # type: ignore[attr-defined]
    process._ao_lore_sandbox_digest = canonical_digest(sandbox)  # type: ignore[attr-defined]
    process._ao_lore_sandbox_phase = phase  # type: ignore[attr-defined]
    process._ao_lore_prepared_run = run  # type: ignore[attr-defined]
    return process


def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
    process_group = getattr(process, "_ao_lore_process_group", process.pid)
    if type(process_group) is not int or process_group <= 1:
        raise _fail("private DOCX UAT worker process group is invalid")

    def group_exists() -> bool:
        try:
            os.killpg(process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise _fail("private DOCX UAT worker process group is unverifiable") from exc

    if not group_exists():
        process.poll()
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        process.poll()
        return
    deadline = time.monotonic() + 2.0
    while group_exists() and time.monotonic() < deadline:
        process.poll()
        time.sleep(0.01)
    if group_exists():
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            process.poll()
            return
        deadline = time.monotonic() + 2.0
        while group_exists() and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.01)
    process.poll()
    if group_exists():
        raise _fail("private DOCX UAT worker process group survived termination")


def _signal_private_docx_worker(process: subprocess.Popen[bytes]) -> None:
    """Send the single authorized interruption signal to a waiting worker."""

    if (
        not isinstance(process, subprocess.Popen)
        or getattr(process, "_ao_lore_sandbox_phase", None) != "interrupted"
        or getattr(process, "_ao_lore_signal_sent", False) is True
        or process.poll() is not None
        or not isinstance(getattr(process, "_ao_lore_runtime_root", None), Path)
        or not isinstance(getattr(process, "_ao_lore_prepared_run", None), PrivateDocxPreparedRun)
    ):
        raise _fail("private DOCX UAT worker signal is invalid")
    try:
        _inspect_private_docx_barrier(
            getattr(process, "_ao_lore_prepared_run"),
            runtime_root=getattr(process, "_ao_lore_runtime_root"),
        )
    except Exception as exc:
        if isinstance(exc, PrivateDocxUatError):
            raise
        raise _fail("private DOCX UAT worker signal is invalid") from exc
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except OSError as exc:
        raise _fail("private DOCX UAT worker signal failed") from exc
    process._ao_lore_signal_sent = True  # type: ignore[attr-defined]


def _conversion_journal_count(
    run: PrivateDocxPreparedRun, *, runtime_root: Path
) -> int:
    state = runtime_root / "uat" / "private-docx" / run.batch_manifest["batch_id"]
    parent = _open_directory(state, runtime_root, create=False)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            "conversion-journal.jsonl",
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        opened = os.fstat(descriptor)
        public = os.stat(
            "conversion-journal.jsonl", dir_fd=parent, follow_symlinks=False
        )
        if (
            (opened.st_dev, opened.st_ino) != run.journal_identity
            or (public.st_dev, public.st_ino) != run.journal_identity
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > 64 * 1024
        ):
            raise _fail("private DOCX UAT conversion journal drifted")
        body = os.pread(descriptor, 64 * 1024 + 1, 0)
        if len(body) != opened.st_size:
            raise _fail("private DOCX UAT conversion journal drifted")
        previous: str | None = None
        lines = body.splitlines(keepends=True)
        for sequence, line in enumerate(lines, 1):
            event = parse_strict_json(line, "private DOCX UAT conversion journal")
            if (
                not line.endswith(b"\n")
                or type(event) is not dict
                or set(event)
                != {"schema_version", "sequence", "previous_event_digest", "event_digest"}
                or event["schema_version"] != "ao.lore.private-docx-conversion-event.v0.1"
                or event["sequence"] != sequence
                or event["previous_event_digest"] != previous
                or event["event_digest"]
                != canonical_digest({key: value for key, value in event.items() if key != "event_digest"})
            ):
                raise _fail("private DOCX UAT conversion journal drifted")
            previous = event["event_digest"]
        return len(lines)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent)


def _campaign_progress(
    run: PrivateDocxPreparedRun, *, runtime_root: Path
) -> tuple[int, int]:
    checkpoint_path = (
        runtime_root / "batches" / run.batch_manifest["batch_id"] / "checkpoint.json"
    )
    if checkpoint_path.exists():
        checkpoint_value, _ = strict_read_json(
            checkpoint_path,
            "private DOCX UAT phase checkpoint",
            max_bytes=64 * 1024,
            root=runtime_root,
        )
        checkpoint = validate_checkpoint(
            checkpoint_value, run.batch_manifest, run.batch_manifest_digest
        )
        processed = checkpoint["processed"]
    else:
        processed = 0
    return processed, _conversion_journal_count(run, runtime_root=runtime_root)


def _validate_phase_start(
    run: PrivateDocxPreparedRun, phase: str, *, runtime_root: Path
) -> tuple[int, int, int]:
    if phase not in _PHASE_ARITHMETIC:
        raise _fail("private DOCX UAT worker phase is invalid")
    arithmetic = _PHASE_ARITHMETIC[phase]
    starting_processed = arithmetic[0]
    if _campaign_progress(run, runtime_root=runtime_root) != (
        starting_processed,
        starting_processed,
    ):
        raise _fail("private DOCX UAT worker phase arithmetic is invalid")
    return arithmetic


def _inspect_private_docx_barrier(
    run: PrivateDocxPreparedRun, *, runtime_root: Path
) -> dict[str, Any] | None:
    state = runtime_root / "uat" / "private-docx" / run.batch_manifest["batch_id"]
    barrier_path = state / _WORKER_BARRIER_NAME
    try:
        os.lstat(barrier_path)
    except FileNotFoundError:
        return None
    contract, _ = strict_read_json(
        state / _WORKER_CONTRACT_NAME,
        "private DOCX UAT retained worker contract",
        max_bytes=512 * 1024,
        root=runtime_root,
    )
    barrier, _ = strict_read_json(
        barrier_path,
        "private DOCX UAT worker barrier",
        max_bytes=64 * 1024,
        root=runtime_root,
    )
    checkpoint_value, _ = strict_read_json(
        runtime_root / "batches" / run.batch_manifest["batch_id"] / "checkpoint.json",
        "private DOCX UAT live checkpoint",
        max_bytes=64 * 1024,
        root=runtime_root,
    )
    checkpoint = validate_checkpoint(
        checkpoint_value, run.batch_manifest, run.batch_manifest_digest
    )
    if (
        type(contract) is not dict
        or contract.get("phase") != "interrupted"
        or contract.get("run_digest")
        != canonical_digest({key: value for key, value in contract.items() if key != "run_digest"})
        or type(barrier) is not dict
        or set(barrier)
        != {
            "schema_version", "format_id", "policy_digest", "phase", "batch_id",
            "batch_manifest_digest", "checkpoint_digest", "processed", "journal_count",
            "run_digest", "authority",
        }
        or barrier["schema_version"] != "ao.lore.private-docx-uat-barrier.v0.1"
        or barrier["format_id"] != "docx"
        or barrier["policy_digest"] != PRIVATE_DOCX_UAT_POLICY_DIGEST
        or barrier["phase"] != "interrupted"
        or barrier["batch_id"] != run.batch_manifest["batch_id"]
        or barrier["batch_manifest_digest"] != run.batch_manifest_digest
        or barrier["checkpoint_digest"] != checkpoint["checkpoint_digest"]
        or barrier["processed"] != 1
        or barrier["journal_count"] != 1
        or barrier["run_digest"] != contract["run_digest"]
        or barrier["authority"] is not False
        or checkpoint["processed"] != 1
        or _conversion_journal_count(run, runtime_root=runtime_root) != 1
    ):
        raise _fail("private DOCX UAT worker barrier is invalid")
    return dict(barrier)


def _wait_private_docx_worker(
    process: subprocess.Popen[bytes], *, timeout: float = _WORKER_TIMEOUT_SECONDS
) -> dict[str, Any]:
    if process.stdout is None or process.stderr is None or timeout <= 0:
        raise _fail("private DOCX UAT worker process is invalid")
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
                raise subprocess.TimeoutExpired("private DOCX UAT worker", timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired("private DOCX UAT worker", timeout)
            for key, _mask in events:
                target = key.data
                maximum = (
                    _WORKER_STDOUT_MAX_BYTES if target is stdout else _WORKER_STDERR_MAX_BYTES
                )
                chunk = os.read(key.fileobj.fileno(), min(65536, maximum + 1 - len(target)))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target.extend(chunk)
                if len(target) > maximum:
                    raise _fail("private DOCX UAT worker output exceeded its bound")
        returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _terminate_worker(process)
        raise _fail("private DOCX UAT worker timed out") from exc
    except BaseException:
        _terminate_worker(process)
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()
    contract_path = getattr(process, "_ao_lore_contract_path", None)
    contract_body = getattr(process, "_ao_lore_contract_body", None)
    runtime = getattr(process, "_ao_lore_runtime_root", None)
    if not isinstance(contract_path, Path) or not isinstance(contract_body, bytes) or not isinstance(runtime, Path):
        raise _fail("private DOCX UAT worker contract drifted")
    retained, retained_body = strict_read_json(
        contract_path,
        "private DOCX UAT retained worker contract",
        max_bytes=512 * 1024,
        root=runtime,
    )
    if (
        retained_body != contract_body
        or not isinstance(retained, Mapping)
        or retained.get("phase") != getattr(process, "_ao_lore_sandbox_phase", None)
        or canonical_digest(retained.get("sandbox"))
        != getattr(process, "_ao_lore_sandbox_digest", None)
    ):
        raise _fail("private DOCX UAT worker contract drifted")
    sandbox_phase = getattr(process, "_ao_lore_sandbox_phase", None)
    if sandbox_phase != "calibration":
        prepared_run = getattr(process, "_ao_lore_prepared_run", None)
        if not isinstance(prepared_run, PrivateDocxPreparedRun):
            raise _fail("private DOCX UAT worker phase arithmetic drifted")
        expected_terminal = _PHASE_ARITHMETIC[sandbox_phase][1]
        if _campaign_progress(prepared_run, runtime_root=runtime) != (
            expected_terminal,
            expected_terminal,
        ):
            raise _fail("private DOCX UAT worker phase arithmetic drifted")
    process._ao_lore_sandbox_verified = True  # type: ignore[attr-defined]
    return {"returncode": returncode, "stdout": bytes(stdout), "stderr": bytes(stderr)}


@dataclass(frozen=True)
class _OwnedCalibrationArtifact:
    name: str
    identity: tuple[int, int]
    body: bytes


def _capture_calibration_artifact_at(
    parent_descriptor: int, name: str, maximum: int = 512 * 1024
) -> _OwnedCalibrationArtifact:
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= maximum
    ):
        raise _fail("private DOCX calibration artifact is invalid")
    body = _read_file_at(parent_descriptor, name, maximum)
    after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns
    ) != (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise _fail("private DOCX calibration artifact drifted")
    return _OwnedCalibrationArtifact(name, (before.st_dev, before.st_ino), body)


def _publish_calibration_artifact_at(
    parent_descriptor: int, name: str, body: bytes
) -> _OwnedCalibrationArtifact:
    if type(body) is not bytes or not body or len(body) > 512 * 1024:
        raise _fail("private DOCX calibration artifact is invalid")
    temporary = f".{name}-{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        _write_all(descriptor, body, "calibration artifact")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(
            temporary, name,
            src_dir_fd=parent_descriptor, dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        return _capture_calibration_artifact_at(parent_descriptor, name)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass


def _unlink_owned_calibration_artifact_at(
    parent_descriptor: int, artifact: _OwnedCalibrationArtifact | None
) -> bool:
    if artifact is None:
        return False
    try:
        current = _capture_calibration_artifact_at(parent_descriptor, artifact.name)
    except FileNotFoundError:
        return False
    if current != artifact:
        return False
    quarantine = f".private-docx-calibration-reclaim-{secrets.token_hex(12)}"
    try:
        _rename_noreplace_at(
            parent_descriptor, artifact.name, parent_descriptor, quarantine
        )
    except FileNotFoundError:
        return False
    os.fsync(parent_descriptor)
    moved = _capture_calibration_artifact_at(parent_descriptor, quarantine)
    if moved.identity != artifact.identity or moved.body != artifact.body:
        # Preserve the unowned binding under its quarantine name for inspection.
        return False
    os.unlink(quarantine, dir_fd=parent_descriptor)
    os.fsync(parent_descriptor)
    return True


def _private_docx_source_sets(run: PrivateDocxPreparedRun) -> tuple[str, str]:
    sources = [
        {
            "item_id": source["item_id"],
            "original_digest": source["original_digest"],
            "transformation_id": source["transformation_id"],
        }
        for source in run.sources
    ]
    derived = [
        {"item_id": source["item_id"], "derived_digest": source["derived_digest"]}
        for source in run.sources
    ]
    return canonical_digest(sources), canonical_digest(derived)


def _docx_calibration_sandbox_spec(
    run: PrivateDocxPreparedRun,
    *,
    bwrap_version: str,
    runtime_root: Path | None = None,
    contract_fd: int = 97,
    seccomp_fd: int = 98,
) -> dict[str, Any]:
    """Build the calibration-only variant of the verified DOCX sandbox."""

    if (
        re.fullmatch(r"bubblewrap [0-9]+(?:\.[0-9]+){1,3}", bwrap_version) is None
        or type(contract_fd) is not int or contract_fd < 3
        or type(seccomp_fd) is not int or seccomp_fd < 3
        or contract_fd == seccomp_fd
    ):
        raise _fail("private DOCX calibration sandbox is invalid")
    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    repository = repository_root()
    state = runtime / "uat" / "private-docx" / run.batch_manifest["batch_id"]
    staged = repository / "sources" / run.staging_name
    qualification = runtime / "private-docx" / "qualification.json"
    for tree in (repository / "src", staged, state):
        try:
            _require_socket_free_tree(tree)
        except OSError as exc:
            raise _fail("private DOCX calibration sandbox mount is invalid") from exc
    mounts = [
        _sandbox_mount(Path("/usr"), "system-usr", "read-only"),
        _sandbox_mount(Path("/etc"), "system-etc", "read-only"),
        _sandbox_mount(repository / "src", "ao-lore-src", "read-only"),
        _sandbox_mount(staged, "staged-sources", "read-only"),
        _sandbox_mount(qualification, "qualification", "read-only"),
        _sandbox_mount(state, "run-state", "read-write"),
    ]
    home = "/tmp/ao-lore-private-docx-calibration-home"
    environment = {
        "AO_LORE_HOME": str(runtime),
        "HOME": home,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PWD": str(repository),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(repository / "src"),
        "XDG_CACHE_HOME": "/tmp/ao-lore-private-docx-calibration-cache",
    }
    argv = [
        _BWRAP, "--die-with-parent", "--unshare-net", "--unshare-pid",
        "--tmpfs", "/", "--dir", "/run", "--perms", "0400",
        "--ro-bind-data", str(contract_fd), _IMMUTABLE_CONTRACT_PATH,
        "--seccomp", str(seccomp_fd),
    ]
    for mount in mounts:
        argv.extend((
            "--bind" if mount["mode"] == "read-write" else "--ro-bind",
            mount["target"], mount["target"],
        ))
    argv.extend((
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--clearenv",
    ))
    for key in sorted(environment):
        if key != "PWD":
            argv.extend(("--setenv", key, environment[key]))
    limit_argv = [
        "/usr/bin/prlimit",
        f"--cpu={_RESOURCE_LIMITS['cpu_seconds']}:{_RESOURCE_LIMITS['cpu_seconds']}",
        f"--as={_RESOURCE_LIMITS['address_space_bytes']}:{_RESOURCE_LIMITS['address_space_bytes']}",
        f"--data={_RESOURCE_LIMITS['data_bytes']}:{_RESOURCE_LIMITS['data_bytes']}",
        f"--fsize={_RESOURCE_LIMITS['file_size_bytes']}:{_RESOURCE_LIMITS['file_size_bytes']}",
        f"--nofile={_RESOURCE_LIMITS['open_files']}:{_RESOURCE_LIMITS['open_files']}",
        f"--nproc={_RESOURCE_LIMITS['processes']}:{_RESOURCE_LIMITS['processes']}",
        "--core=0:0", "--",
    ]
    command = [sys.executable, "-m", "ao_lore.private_docx_calibration_worker"]
    argv.extend(("--chdir", str(repository), "--", *limit_argv, *command))
    return {
        "schema_version": _SANDBOX_SCHEMA_VERSION,
        "mechanism": _BWRAP,
        "version": bwrap_version,
        "argv": argv,
        "network_namespace": "unshared",
        "root_filesystem": "allowlisted",
        "mounts": mounts,
        "environment": environment,
        "immutable_contract": {"path": _IMMUTABLE_CONTRACT_PATH, "fd": contract_fd, "sealed": True},
        "resource_limits": dict(_RESOURCE_LIMITS),
        "child_process_policy": "seccomp-deny-clone-fork-vfork-clone3",
        "seccomp": {"fd": seccomp_fd, "denied_syscalls": list(_DENIED_CHILD_SYSCALLS)},
        "authority": False,
    }


def _docx_calibration_contract(
    run: PrivateDocxPreparedRun,
    qualification: Mapping[str, Any],
    sandbox: Mapping[str, Any],
    *,
    runtime_root: Path | None = None,
) -> dict[str, Any]:
    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    qualification_value = _strict_copy(qualification)
    from .docx_ooxml import (
        DocxLimits,
        docx_configuration_digest,
        validate_docx_benchmark,
        validate_docx_package,
    )
    from .docx_expectation_oracle import derive_docx_semantic_expectation
    from .private_docx_domain import _active_content, _coerce_sha256, _raw_parts

    configuration_digest = docx_configuration_digest(DocxLimits())
    validate_docx_benchmark(
        qualification_value,
        expected_configuration_digest=configuration_digest,
        expected_corpus_digest=run.expectation_digest,
    )
    qualification_path = runtime / "private-docx" / "qualification.json"
    qualification_body = _read_exact_file(qualification_path, 8 * 1024 * 1024)
    qualification_info = os.stat(qualification_path, follow_symlinks=False)
    if parse_strict_json(qualification_body, "private DOCX calibration qualification") != qualification_value:
        raise _fail("private DOCX calibration qualification drifted")
    state = runtime / "uat" / "private-docx" / run.batch_manifest["batch_id"]
    state_info = os.stat(state, follow_symlinks=False)
    staged = repository_root() / "sources" / run.staging_name
    staged_info = os.stat(staged, follow_symlinks=False)
    documents: list[dict[str, Any]] = []
    for batch_document, source in zip(
        run.batch_manifest["documents"], run.sources, strict=True
    ):
        locator = batch_document["source"]
        path = repository_root() / locator
        info = os.stat(path, follow_symlinks=False)
        body = _read_exact_file(path, MAX_DOCX_BYTES)
        if (
            source["item_id"] != batch_document["item_id"]
            or source["derived_digest"] != batch_document["source_digest"]
            or "sha256:" + hashlib.sha256(body).hexdigest() != source["derived_digest"]
            or source["transformation_id"] != DOCX_TRANSFORMATION_ID
        ):
            raise _fail("private DOCX calibration source drifted")
        parts = _raw_parts(body)
        if _active_content(parts):
            raise _fail("private DOCX calibration representative source is active")
        package = validate_docx_package(body)
        signals = derive_docx_semantic_expectation(package, DocxLimits())
        documents.append({
            "item_id": source["item_id"],
            "locator": locator,
            "identity": {"device": info.st_dev, "inode": info.st_ino},
            "size": len(body),
            "original_digest": source["original_digest"],
            "derived_digest": source["derived_digest"],
            "transformation_id": source["transformation_id"],
            "expectation": {
                "package_inventory_digest": _coerce_sha256(package.inventory_digest),
                "normalized_text_digest": signals.normalized_text_digest,
                "structural_event_digest": signals.structural_event_digest,
                "counts": dict(signals.counts),
                "expected_outcome": "accept",
                "expected_rejection": None,
                "page_count": None,
                "page_count_reason": "ooxml-pagination-unavailable",
            },
        })
    source_set_digest, derived_set_digest = _private_docx_source_sets(run)
    contract = {
        "schema_version": _CALIBRATION_CONTRACT_SCHEMA_VERSION,
        "format_id": "docx",
        "policy_digest": PRIVATE_DOCX_UAT_POLICY_DIGEST,
        "phase": "calibration",
        "batch_id": run.batch_manifest["batch_id"],
        "batch_manifest_digest": run.batch_manifest_digest,
        "corpus_digest": run.corpus_digest,
        "expectation_digest": run.expectation_digest,
        "qualification_locator": "private-docx/qualification.json",
        "qualification_identity": {"device": qualification_info.st_dev, "inode": qualification_info.st_ino},
        "qualification_body_digest": "sha256:" + hashlib.sha256(qualification_body).hexdigest(),
        "qualification_digest": canonical_digest(qualification_value),
        "configuration_digest": configuration_digest,
        "source_set_digest": source_set_digest,
        "derived_set_digest": derived_set_digest,
        "transformation_id": DOCX_TRANSFORMATION_ID,
        "staging_name": run.staging_name,
        "staging_identity": {"device": staged_info.st_dev, "inode": staged_info.st_ino},
        "documents": documents,
        "output_locator": (
            f"uat/private-docx/{run.batch_manifest['batch_id']}/{_CALIBRATION_RESULT_NAME}"
        ),
        "output_parent_identity": {"device": state_info.st_dev, "inode": state_info.st_ino},
        "item_timeout_seconds": _CALIBRATION_ITEM_TIMEOUT_SECONDS,
        "sandbox": _strict_copy(sandbox),
        "authority": False,
    }
    contract["run_digest"] = canonical_digest(contract)
    return contract


def _launch_private_docx_calibration_worker(
    run: PrivateDocxPreparedRun,
    qualification: Mapping[str, Any],
    *,
    runtime_root: Path | None = None,
) -> subprocess.Popen[bytes]:
    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    version = _probe_docx_sandbox()
    contract_descriptor = os.memfd_create(
        "ao-lore-private-docx-calibration-contract",
        os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    seccomp_descriptor: int | None = None
    parent_descriptor: int | None = None
    owned_contract: _OwnedCalibrationArtifact | None = None
    try:
        seccomp_descriptor = _seccomp_descriptor()
        sandbox = _docx_calibration_sandbox_spec(
            run,
            bwrap_version=version,
            runtime_root=runtime,
            contract_fd=contract_descriptor,
            seccomp_fd=seccomp_descriptor,
        )
        contract = _docx_calibration_contract(
            run, qualification, sandbox, runtime_root=runtime
        )
        state = runtime / "uat" / "private-docx" / run.batch_manifest["batch_id"]
        parent_descriptor = _open_directory(state, runtime, create=False)
        if _binding_exists_at(parent_descriptor, _CALIBRATION_CONTRACT_NAME) or _binding_exists_at(
            parent_descriptor, _CALIBRATION_RESULT_NAME
        ):
            raise _fail("private DOCX calibration artifact collision")
        contract_body = _manifest_body(contract, 512 * 1024)
        owned_contract = _publish_calibration_artifact_at(
            parent_descriptor, _CALIBRATION_CONTRACT_NAME, contract_body
        )
        _write_all(contract_descriptor, contract_body, "calibration immutable contract")
        os.fsync(contract_descriptor)
        _seal_descriptor(contract_descriptor)
        process = subprocess.Popen(
            sandbox["argv"],
            cwd=repository_root(),
            env={},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            shell=False,
            pass_fds=(contract_descriptor, seccomp_descriptor),
        )
        process._ao_lore_process_group = process.pid  # type: ignore[attr-defined]
    except BaseException:
        if parent_descriptor is not None:
            _unlink_owned_calibration_artifact_at(parent_descriptor, owned_contract)
            os.close(parent_descriptor)
        raise
    finally:
        os.close(contract_descriptor)
        if seccomp_descriptor is not None:
            os.close(seccomp_descriptor)
    process._ao_lore_contract_path = state / _CALIBRATION_CONTRACT_NAME  # type: ignore[attr-defined]
    process._ao_lore_contract_body = contract_body  # type: ignore[attr-defined]
    process._ao_lore_contract_artifact = owned_contract  # type: ignore[attr-defined]
    process._ao_lore_artifact_parent_descriptor = parent_descriptor  # type: ignore[attr-defined]
    process._ao_lore_runtime_root = runtime  # type: ignore[attr-defined]
    process._ao_lore_sandbox_digest = canonical_digest(sandbox)  # type: ignore[attr-defined]
    process._ao_lore_sandbox_phase = "calibration"  # type: ignore[attr-defined]
    return process


def _calibration_runtime_path(runtime: Path, locator: Any, name: str) -> Path:
    if (
        type(locator) is not str
        or not locator
        or "\x00" in locator
        or Path(locator).is_absolute()
        or ".." in Path(locator).parts
        or Path(locator).name != name
    ):
        raise OSError("private DOCX calibration locator is invalid")
    path = runtime / locator
    try:
        path.relative_to(runtime)
    except ValueError as exc:
        raise OSError("private DOCX calibration locator is invalid") from exc
    return path


def _load_private_docx_calibration_contract() -> tuple[dict[str, Any], dict[str, Any], list[tuple[dict[str, Any], bytes]], Path]:
    """Validate the immutable calibration contract and detach all source bytes once."""

    runtime = runtime_home()
    from .private_docx_uat_worker import _read_held_file

    immutable_descriptors: list[int] = []
    try:
        body, _immutable_info = _read_held_file(
            Path(_IMMUTABLE_CONTRACT_PATH),
            512 * 1024,
            "calibration immutable contract",
            immutable_descriptors,
            required_links=None,
        )
    finally:
        while immutable_descriptors:
            os.close(immutable_descriptors.pop())
    contract = parse_strict_json(body, "private DOCX calibration worker contract")
    keys = {
        "schema_version", "format_id", "policy_digest", "phase", "batch_id",
        "batch_manifest_digest", "corpus_digest", "expectation_digest",
        "qualification_locator", "qualification_identity", "qualification_body_digest",
        "qualification_digest", "configuration_digest", "source_set_digest",
        "derived_set_digest", "transformation_id", "staging_name", "staging_identity",
        "documents", "output_locator", "output_parent_identity", "item_timeout_seconds",
        "sandbox", "authority", "run_digest",
    }
    if (
        type(contract) is not dict
        or set(contract) != keys
        or contract["schema_version"] != _CALIBRATION_CONTRACT_SCHEMA_VERSION
        or contract["format_id"] != "docx"
        or contract["policy_digest"] != PRIVATE_DOCX_UAT_POLICY_DIGEST
        or contract["phase"] != "calibration"
        or contract["transformation_id"] != DOCX_TRANSFORMATION_ID
        or contract["authority"] is not False
        or contract["run_digest"]
        != canonical_digest({key: value for key, value in contract.items() if key != "run_digest"})
    ):
        raise OSError("private DOCX calibration contract is invalid")
    for field in (
        "batch_manifest_digest", "corpus_digest", "expectation_digest",
        "qualification_body_digest", "qualification_digest", "configuration_digest",
        "source_set_digest", "derived_set_digest", "run_digest",
    ):
        if type(contract[field]) is not str or _DIGEST_RE.fullmatch(contract[field]) is None:
            raise OSError("private DOCX calibration contract is invalid")
    timeout = contract["item_timeout_seconds"]
    if type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise OSError("private DOCX calibration contract is invalid")
    sandbox = contract["sandbox"]
    if (
        type(sandbox) is not dict
        or sandbox.get("mechanism") != _BWRAP
        or sandbox.get("network_namespace") != "unshared"
        or sandbox.get("root_filesystem") != "allowlisted"
        or sandbox.get("authority") is not False
        or sandbox.get("child_process_policy") != "seccomp-deny-clone-fork-vfork-clone3"
        or sandbox.get("seccomp", {}).get("denied_syscalls") != list(_DENIED_CHILD_SYSCALLS)
        or sandbox.get("immutable_contract", {}).get("path") != _IMMUTABLE_CONTRACT_PATH
        or sandbox.get("immutable_contract", {}).get("sealed") is not True
        or sandbox.get("argv", [])[-3:] != [
            sys.executable, "-m", "ao_lore.private_docx_calibration_worker"
        ]
    ):
        raise OSError("private DOCX calibration sandbox is invalid")
    qualification_path = _calibration_runtime_path(
        runtime, contract["qualification_locator"], "qualification.json"
    )
    qualification_body = _read_exact_file(qualification_path, 8 * 1024 * 1024)
    qualification_info = os.stat(qualification_path, follow_symlinks=False)
    qualification = parse_strict_json(
        qualification_body, "private DOCX calibration worker qualification"
    )
    from .docx_ooxml import validate_docx_benchmark

    validate_docx_benchmark(
        qualification,
        expected_configuration_digest=contract["configuration_digest"],
        expected_corpus_digest=contract["expectation_digest"],
    )
    if (
        contract["qualification_identity"]
        != {"device": qualification_info.st_dev, "inode": qualification_info.st_ino}
        or contract["qualification_body_digest"]
        != "sha256:" + hashlib.sha256(qualification_body).hexdigest()
        or contract["qualification_digest"] != canonical_digest(qualification)
        or qualification.get("decision") != "hold"
    ):
        raise OSError("private DOCX calibration qualification drifted")
    staging = repository_root() / "sources" / contract["staging_name"]
    staging_info = os.stat(staging, follow_symlinks=False)
    if contract["staging_identity"] != {"device": staging_info.st_dev, "inode": staging_info.st_ino}:
        raise OSError("private DOCX calibration staging drifted")
    documents = contract["documents"]
    if type(documents) is not list or len(documents) != len(DOCX_UAT_ITEM_IDS):
        raise OSError("private DOCX calibration documents are invalid")
    held: list[tuple[dict[str, Any], bytes]] = []
    source_set: list[dict[str, str]] = []
    derived_set: list[dict[str, str]] = []
    for index, document in enumerate(documents):
        expected_keys = {
            "item_id", "locator", "identity", "size", "original_digest",
            "derived_digest", "transformation_id", "expectation",
        }
        if (
            type(document) is not dict
            or set(document) != expected_keys
            or document["item_id"] != DOCX_UAT_ITEM_IDS[index]
            or document["transformation_id"] != DOCX_TRANSFORMATION_ID
            or type(document["size"]) is not int
            or not 0 < document["size"] <= MAX_DOCX_BYTES
            or any(
                type(document[field]) is not str or _DIGEST_RE.fullmatch(document[field]) is None
                for field in ("original_digest", "derived_digest")
            )
        ):
            raise OSError("private DOCX calibration documents are invalid")
        expectation = document["expectation"]
        count_keys = {
            "paragraphs", "headings", "lists", "tables", "rows", "cells",
            "links", "footnotes", "endnotes", "drawings", "media",
        }
        if (
            type(expectation) is not dict
            or set(expectation) != {
                "package_inventory_digest", "normalized_text_digest",
                "structural_event_digest", "counts", "expected_outcome",
                "expected_rejection", "page_count", "page_count_reason",
            }
            or any(
                type(expectation[field]) is not str
                or _DIGEST_RE.fullmatch(expectation[field]) is None
                for field in (
                    "package_inventory_digest", "normalized_text_digest",
                    "structural_event_digest",
                )
            )
            or type(expectation["counts"]) is not dict
            or set(expectation["counts"]) != count_keys
            or any(
                type(value) is not int or isinstance(value, bool) or value < 0
                for value in expectation["counts"].values()
            )
            or expectation["expected_outcome"] != "accept"
            or expectation["expected_rejection"] is not None
            or expectation["page_count"] is not None
            or expectation["page_count_reason"] != "ooxml-pagination-unavailable"
        ):
            raise OSError("private DOCX calibration documents are invalid")
        expected_locator = f"sources/{contract['staging_name']}/{document['item_id']}.docx"
        if document["locator"] != expected_locator:
            raise OSError("private DOCX calibration documents are invalid")
        path = repository_root() / document["locator"]
        info = os.stat(path, follow_symlinks=False)
        source = _read_exact_file(path, MAX_DOCX_BYTES)
        if (
            document["identity"] != {"device": info.st_dev, "inode": info.st_ino}
            or document["size"] != len(source)
            or document["derived_digest"] != "sha256:" + hashlib.sha256(source).hexdigest()
        ):
            raise OSError("private DOCX calibration source drifted")
        from .docx_expectation_oracle import derive_docx_semantic_expectation
        from .docx_ooxml import DocxLimits, validate_docx_package
        from .private_docx_domain import (
            _active_content, _coerce_sha256, _raw_parts,
        )

        parts = _raw_parts(source)
        package = validate_docx_package(source)
        signals = derive_docx_semantic_expectation(package, DocxLimits())
        if _active_content(parts) or expectation != {
            "package_inventory_digest": _coerce_sha256(package.inventory_digest),
            "normalized_text_digest": signals.normalized_text_digest,
            "structural_event_digest": signals.structural_event_digest,
            "counts": dict(signals.counts),
            "expected_outcome": "accept",
            "expected_rejection": None,
            "page_count": None,
            "page_count_reason": "ooxml-pagination-unavailable",
        }:
            raise OSError("private DOCX calibration expectation drifted")
        detached = _strict_copy(document)
        held.append((detached, source))
        source_set.append({
            "item_id": document["item_id"],
            "original_digest": document["original_digest"],
            "transformation_id": document["transformation_id"],
        })
        derived_set.append({
            "item_id": document["item_id"],
            "derived_digest": document["derived_digest"],
        })
    if (
        canonical_digest(source_set) != contract["source_set_digest"]
        or canonical_digest(derived_set) != contract["derived_set_digest"]
    ):
        raise OSError("private DOCX calibration origin drifted")
    output = _calibration_runtime_path(
        runtime, contract["output_locator"], _CALIBRATION_RESULT_NAME
    )
    parent_info = os.stat(output.parent, follow_symlinks=False)
    if contract["output_parent_identity"] != {
        "device": parent_info.st_dev, "inode": parent_info.st_ino
    }:
        raise OSError("private DOCX calibration output drifted")
    sandbox_keys = {
        "schema_version", "mechanism", "version", "argv", "network_namespace",
        "root_filesystem", "mounts", "environment", "immutable_contract",
        "resource_limits", "child_process_policy", "seccomp", "authority",
    }

    def expected_mount(path: Path, name: str, mode: str) -> dict[str, Any]:
        info = os.stat(path, follow_symlinks=False)
        return {
            "name": name,
            "target": str(path),
            "mode": mode,
            "identity": {"device": info.st_dev, "inode": info.st_ino},
        }

    expected_mounts = [
        expected_mount(Path("/usr"), "system-usr", "read-only"),
        expected_mount(Path("/etc"), "system-etc", "read-only"),
        expected_mount(repository_root() / "src", "ao-lore-src", "read-only"),
        expected_mount(staging, "staged-sources", "read-only"),
        expected_mount(qualification_path, "qualification", "read-only"),
        expected_mount(output.parent, "run-state", "read-write"),
    ]
    expected_environment = {
        "AO_LORE_HOME": str(runtime),
        "HOME": "/tmp/ao-lore-private-docx-calibration-home",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PWD": str(repository_root()),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(repository_root() / "src"),
        "XDG_CACHE_HOME": "/tmp/ao-lore-private-docx-calibration-cache",
    }
    immutable_fd = sandbox.get("immutable_contract", {}).get("fd")
    seccomp_fd = sandbox.get("seccomp", {}).get("fd")
    if (
        set(sandbox) != sandbox_keys
        or sandbox["schema_version"] != _SANDBOX_SCHEMA_VERSION
        or sandbox["mounts"] != expected_mounts
        or sandbox["environment"] != expected_environment
        or dict(os.environ) != expected_environment
        or sandbox["resource_limits"] != _RESOURCE_LIMITS
        or sandbox["immutable_contract"]
        != {"path": _IMMUTABLE_CONTRACT_PATH, "fd": immutable_fd, "sealed": True}
        or type(immutable_fd) is not int or immutable_fd < 3
        or sandbox["seccomp"]
        != {"fd": seccomp_fd, "denied_syscalls": list(_DENIED_CHILD_SYSCALLS)}
        or type(seccomp_fd) is not int or seccomp_fd < 3 or seccomp_fd == immutable_fd
    ):
        raise OSError("private DOCX calibration sandbox binding is invalid")
    expected_argv = [
        _BWRAP, "--die-with-parent", "--unshare-net", "--unshare-pid",
        "--tmpfs", "/", "--dir", "/run", "--perms", "0400",
        "--ro-bind-data", str(immutable_fd), _IMMUTABLE_CONTRACT_PATH,
        "--seccomp", str(seccomp_fd),
    ]
    for mount in expected_mounts:
        expected_argv.extend((
            "--bind" if mount["mode"] == "read-write" else "--ro-bind",
            mount["target"], mount["target"],
        ))
    expected_argv.extend((
        "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--clearenv",
    ))
    for key in sorted(expected_environment):
        if key != "PWD":
            expected_argv.extend(("--setenv", key, expected_environment[key]))
    expected_argv.extend((
        "--chdir", str(repository_root()), "--", "/usr/bin/prlimit",
        f"--cpu={_RESOURCE_LIMITS['cpu_seconds']}:{_RESOURCE_LIMITS['cpu_seconds']}",
        f"--as={_RESOURCE_LIMITS['address_space_bytes']}:{_RESOURCE_LIMITS['address_space_bytes']}",
        f"--data={_RESOURCE_LIMITS['data_bytes']}:{_RESOURCE_LIMITS['data_bytes']}",
        f"--fsize={_RESOURCE_LIMITS['file_size_bytes']}:{_RESOURCE_LIMITS['file_size_bytes']}",
        f"--nofile={_RESOURCE_LIMITS['open_files']}:{_RESOURCE_LIMITS['open_files']}",
        f"--nproc={_RESOURCE_LIMITS['processes']}:{_RESOURCE_LIMITS['processes']}",
        "--core=0:0", "--", sys.executable, "-m", "ao_lore.private_docx_calibration_worker",
    ))
    if sandbox["argv"] != expected_argv:
        raise OSError("private DOCX calibration sandbox argv is invalid")
    return contract, _strict_copy(qualification), held, output


class _CalibrationDeadline(BaseException):
    pass


def _invoke_private_docx_calibration_attempt(
    adapter: Any,
    source: dict[str, Any],
    timeout: float,
    deadline_events: list[float],
) -> tuple[Any, float, int]:
    import tracemalloc

    before_deadlines = len(deadline_events)
    tracemalloc.start()
    started = time.perf_counter()
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        result = adapter.parse(source)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        duration = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    if len(deadline_events) != before_deadlines:
        raise OSError("private DOCX calibration deadline was suppressed")
    return result, duration, peak


def _evaluate_private_docx_calibration(
    adapter: Any,
    contract: Mapping[str, Any],
    held: Sequence[tuple[Mapping[str, Any], bytes]],
    invoke: Any,
) -> dict[str, Any]:
    """Independently score every detached representative attempt."""

    from .docx_benchmark import _canonical_output, _detach_output
    from .parsing import ParseOutput

    capability = getattr(adapter, "capability", None)
    if (
        not isinstance(capability, Mapping)
        or capability.get("parser_id") != DOCX_PARSER_ID
        or capability.get("version") != DOCX_PARSER_VERSION
    ):
        raise OSError("private DOCX calibration adapter identity is invalid")
    durations: list[float] = []
    peaks: list[int] = []
    text_scores: list[float] = []
    structural_scores: list[float] = []
    location_scores: list[float] = []
    correct_outcomes = 0
    unexpected = 0
    repeatable = True
    seen_results: set[int] = set()
    retained: list[tuple[Any, Mapping[str, Any], str]] = []
    expected_quality = {
        "text_coverage": 1.0,
        "structural_completeness": 1.0,
        "source_span_coverage": 1.0,
        "document_ir_valid": 1.0,
    }
    for document, body in held:
        expectation = document["expectation"]
        attempt_digests: list[str] = []
        for _attempt in range(2):
            source = {
                "resource": f"private-docx/{document['item_id']}.docx",
                "digest": document["derived_digest"],
                "media_type": DOCX_MIME,
                "data": body,
            }
            operation_calls = 0
            operation_result: Any = None
            operation_error: BaseException | None = None

            class ObservedAdapter:
                capability = adapter.capability

                def parse(self, selected_source: Mapping[str, Any]) -> Any:
                    nonlocal operation_calls, operation_result, operation_error
                    operation_calls += 1
                    if operation_calls != 1 or selected_source is not source:
                        raise OSError(
                            "private DOCX calibration measurement changed operation semantics"
                        )
                    try:
                        operation_result = adapter.parse(selected_source)
                    except BaseException as exc:
                        operation_error = exc
                        raise
                    return operation_result

            try:
                measured = invoke(ObservedAdapter(), source)
            except BaseException as exc:
                if (
                    operation_calls != 1
                    or operation_error is None
                    or exc is not operation_error
                ):
                    raise OSError(
                        "private DOCX calibration measurement changed operation semantics"
                    ) from (exc if isinstance(exc, Exception) else None)
                if not isinstance(exc, Exception):
                    raise
                unexpected += 1
                repeatable = False
                text_scores.append(0.0)
                structural_scores.append(0.0)
                location_scores.append(0.0)
                continue
            try:
                result, duration, peak = measured
            except BaseException as exc:
                raise OSError(
                    "private DOCX calibration measurement changed operation semantics"
                ) from (exc if isinstance(exc, Exception) else None)
            if (
                operation_calls != 1
                or operation_error is not None
                or result is not operation_result
            ):
                raise OSError(
                    "private DOCX calibration measurement changed operation semantics"
                )
            if source != {
                "resource": f"private-docx/{document['item_id']}.docx",
                "digest": document["derived_digest"],
                "media_type": DOCX_MIME,
                "data": body,
            }:
                raise OSError("private DOCX calibration source was mutated")
            if type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
                raise OSError("private DOCX calibration duration is invalid")
            if type(peak) is not int or isinstance(peak, bool) or peak < 0:
                raise OSError("private DOCX calibration memory result is invalid")
            if id(result) in seen_results:
                raise OSError("private DOCX calibration result was reused")
            if (
                type(result) is not ParseOutput
                or type(result.document_ir) is not dict
                or type(result.quality_components) is not dict
                or type(result.critical_failures) is not tuple
            ):
                raise OSError("private DOCX calibration output contract is invalid")
            seen_results.add(id(result))
            detached = _detach_output(result)
            normalized = _canonical_output(
                detached, source, DOCX_PARSER_ID, DOCX_PARSER_VERSION
            )
            if (
                detached.document_ir["parser"]["configuration_digest"]
                != contract["configuration_digest"]
                or detached.quality_components != expected_quality
                or tuple(detached.critical_failures) != ()
            ):
                raise OSError("private DOCX calibration output contract is invalid")
            original = _canonical_output(
                result, source, DOCX_PARSER_ID, DOCX_PARSER_VERSION
            )
            if original["sealed_digest"] != normalized["sealed_digest"]:
                raise OSError("private DOCX calibration result mutated")
            retained.append((result, dict(source), original["sealed_digest"]))
            attempt_digests.append(normalized["sealed_digest"])
            durations.append(float(duration))
            peaks.append(peak)
            inventory_ok = (
                normalized["inventory_digest"]
                == expectation["package_inventory_digest"]
            )
            if inventory_ok:
                correct_outcomes += 1
            else:
                unexpected += 1
            text_scores.append(
                1.0
                if normalized["normalized_text_digest"]
                == expectation["normalized_text_digest"]
                else 0.0
            )
            matches = sum(
                normalized["counts"][name] == expectation["counts"][name]
                for name in expectation["counts"]
            )
            structural_scores.append(
                (
                    matches
                    + int(
                        normalized["structural_event_digest"]
                        == expectation["structural_event_digest"]
                    )
                )
                / (len(expectation["counts"]) + 1)
            )
            location_scores.append(normalized["source_location_score"])
        if len(attempt_digests) != 2 or attempt_digests[0] != attempt_digests[1]:
            repeatable = False
    for result, source, sealed_digest in retained:
        reopened = _canonical_output(
            result, source, DOCX_PARSER_ID, DOCX_PARSER_VERSION
        )
        if reopened["sealed_digest"] != sealed_digest:
            raise OSError("private DOCX calibration result mutated after return")
    attempts = len(held) * 2
    if not durations or len(text_scores) != attempts:
        # All attempts may fail normally; keep summaries well-formed and investigate.
        durations = durations or [0.0]
        peaks = peaks or [0]
    aggregate = {
        "metrics": {
            "text_fidelity": sum(text_scores) / attempts,
            "structural_fidelity": sum(structural_scores) / attempts,
            "source_location_fidelity": sum(location_scores) / attempts,
            "expected_outcome_accuracy": correct_outcomes / attempts,
            "expected_rejection_count": DOCX_EXPECTED_REJECTION_COUNT,
            "accepted_document_count": DOCX_ACCEPTED_DOCUMENT_COUNT,
        },
        "exclusions": [],
        "stable_failures": {
            "invalid_package": DOCX_INVALID_PACKAGE_REJECTION_COUNT,
            "unsupported_active_content": DOCX_ACTIVE_CONTENT_REJECTION_COUNT,
            "representative_unexpected": unexpected,
        },
        "latency_seconds": {
            "min": min(durations),
            "mean": sum(durations) / len(durations),
            "max": max(durations),
        },
        "peak_memory_bytes": {
            "min": min(peaks),
            "mean": sum(peaks) / len(peaks),
            "max": max(peaks),
        },
        "repeatability": {
            "runs": 2,
            "identical": repeatable,
            "score": 1.0 if repeatable else 0.0,
        },
    }
    return json.loads(json.dumps(_validate_private_docx_calibration_aggregate(aggregate)))


def _private_docx_calibration_worker_entry() -> int:
    """Execute exactly two fixed-order native parses for each held DOCX input."""

    try:
        from .docx_ooxml import NativeDocxOoxmlAdapter
        from .private_docx_uat_worker import (
            _NetworkAttempt, _ProcessAttempt, _execution_guards,
        )

        contract, qualification, held, output = _load_private_docx_calibration_contract()
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        finally:
            os.close(devnull)
        adapter = NativeDocxOoxmlAdapter(
            qualification,
            expected_corpus_digest=contract["expectation_digest"],
        )
        attempts: list[str] = []
        deadline_events: list[float] = []

        def alarm(_signum: int, _frame: Any) -> None:
            deadline_events.append(time.monotonic())
            raise _CalibrationDeadline("private DOCX calibration item timed out")

        def invoke(selected_adapter: Any, source: dict[str, Any]) -> tuple[Any, float, int]:
            return _invoke_private_docx_calibration_attempt(
                selected_adapter,
                source,
                contract["item_timeout_seconds"],
                deadline_events,
            )

        previous_alarm = signal.signal(signal.SIGALRM, alarm)
        try:
            with _execution_guards(attempts):
                aggregate = _evaluate_private_docx_calibration(
                    adapter, contract, held, invoke
                )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous_alarm)
        if attempts:
            raise OSError("private DOCX calibration guarded operation was attempted")
        normalized = _validate_private_docx_calibration_aggregate(aggregate)
        result = {
            "schema_version": _CALIBRATION_RESULT_SCHEMA_VERSION,
            "run_digest": contract["run_digest"],
            "corpus_digest": contract["corpus_digest"],
            "expectation_digest": contract["expectation_digest"],
            "qualification_digest": contract["qualification_digest"],
            "configuration_digest": contract["configuration_digest"],
            "source_set_digest": contract["source_set_digest"],
            "derived_set_digest": contract["derived_set_digest"],
            "transformation_id": contract["transformation_id"],
            "sandbox_digest": canonical_digest(contract["sandbox"]),
            "aggregate": json.loads(json.dumps(normalized)),
            "aggregate_digest": canonical_digest(normalized),
            "authority": False,
        }
        parent = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            _publish_calibration_artifact_at(
                parent, output.name, _manifest_body(result, 512 * 1024)
            )
        finally:
            os.close(parent)
        return 0
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return 1


def _run_private_docx_calibration_worker(
    run: PrivateDocxPreparedRun,
    qualification: Mapping[str, Any],
    *,
    runtime_root: Path | None = None,
) -> PrivateDocxCalibrationEvidence:
    """Launch and verify one calibration worker, then reclaim only owned artifacts."""

    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    process: subprocess.Popen[bytes] | None = None
    parent_descriptor: int | None = None
    owned_contract: _OwnedCalibrationArtifact | None = None
    owned_result: _OwnedCalibrationArtifact | None = None
    try:
        process = _launch_private_docx_calibration_worker(
            run, qualification, runtime_root=runtime
        )
        parent_descriptor = getattr(
            process, "_ao_lore_artifact_parent_descriptor", None
        )
        owned_contract = getattr(process, "_ao_lore_contract_artifact", None)
        outcome = _wait_private_docx_worker(
            process, timeout=_CALIBRATION_TIMEOUT_SECONDS
        )
        if getattr(process, "_ao_lore_sandbox_verified", False) is not True:
            raise _fail("private DOCX calibration sandbox was not verified")
        process = None
        if outcome != {"returncode": 0, "stdout": b"", "stderr": b""}:
            raise _fail("private DOCX calibration worker failed")
        if (
            type(parent_descriptor) is not int
            or not isinstance(owned_contract, _OwnedCalibrationArtifact)
            or _capture_calibration_artifact_at(
                parent_descriptor, _CALIBRATION_CONTRACT_NAME
            ) != owned_contract
        ):
            raise _fail("private DOCX calibration contract ownership drifted")
        owned_result = _capture_calibration_artifact_at(
            parent_descriptor, _CALIBRATION_RESULT_NAME
        )
        contract = parse_strict_json(
            owned_contract.body, "private DOCX retained calibration contract"
        )
        result = parse_strict_json(
            owned_result.body, "private DOCX calibration result"
        )
        expected_keys = {
            "schema_version", "run_digest", "corpus_digest", "expectation_digest",
            "qualification_digest", "configuration_digest", "source_set_digest",
            "derived_set_digest", "transformation_id", "sandbox_digest",
            "aggregate", "aggregate_digest", "authority",
        }
        aggregate = _validate_private_docx_calibration_aggregate(result.get("aggregate"))
        if (
            type(result) is not dict
            or set(result) != expected_keys
            or result["schema_version"] != _CALIBRATION_RESULT_SCHEMA_VERSION
            or result["run_digest"] != contract["run_digest"]
            or result["corpus_digest"] != run.corpus_digest
            or result["expectation_digest"] != run.expectation_digest
            or result["qualification_digest"] != canonical_digest(qualification)
            or result["configuration_digest"] != contract["configuration_digest"]
            or result["source_set_digest"] != contract["source_set_digest"]
            or result["derived_set_digest"] != contract["derived_set_digest"]
            or result["transformation_id"] != DOCX_TRANSFORMATION_ID
            or result["sandbox_digest"] != canonical_digest(contract["sandbox"])
            or result["aggregate_digest"] != canonical_digest(aggregate)
            or result["authority"] is not False
        ):
            raise _fail("private DOCX calibration result drifted")
        evidence = _validate_private_docx_calibration_evidence(
            PrivateDocxCalibrationEvidence(
                aggregate=result["aggregate"],
                corpus_digest=result["corpus_digest"],
                expectation_digest=result["expectation_digest"],
                qualification_digest=result["qualification_digest"],
                configuration_digest=result["configuration_digest"],
                source_set_digest=result["source_set_digest"],
                derived_set_digest=result["derived_set_digest"],
                transformation_id=result["transformation_id"],
                sandbox_digest=result["sandbox_digest"],
            )
        )
        if not _unlink_owned_calibration_artifact_at(parent_descriptor, owned_result):
            raise _fail("private DOCX calibration result cleanup drifted")
        owned_result = None
        if not _unlink_owned_calibration_artifact_at(parent_descriptor, owned_contract):
            raise _fail("private DOCX calibration contract cleanup drifted")
        owned_contract = None
        return evidence
    except BaseException as exc:
        if process is not None:
            try:
                _terminate_worker(process)
            except Exception:
                pass
        if parent_descriptor is not None:
            _unlink_owned_calibration_artifact_at(parent_descriptor, owned_result)
            _unlink_owned_calibration_artifact_at(parent_descriptor, owned_contract)
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, PrivateDocxUatError) and str(exc) == "private DOCX calibration failed":
            raise
        raise _fail("private DOCX calibration failed") from exc
    finally:
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _capture_cleanup_tree(path: Path) -> dict[str, Any]:
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise _fail("private DOCX UAT cleanup target is invalid")
    records: list[dict[str, Any]] = []
    total = 0
    for root, directories, files in os.walk(path, topdown=True, followlinks=False):
        directories.sort()
        files.sort()
        relative_root = Path(root).relative_to(path)
        for name in directories:
            target = Path(root) / name
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _fail("private DOCX UAT cleanup target is invalid")
            records.append({
                "kind": "directory", "relative": (relative_root / name).as_posix(),
                "identity": [info.st_dev, info.st_ino],
            })
        for name in files:
            target = Path(root) / name
            info = os.lstat(target)
            if (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or info.st_size > 64 * 1024 * 1024
            ):
                raise _fail("private DOCX UAT cleanup target is invalid")
            body = target.read_bytes()
            if len(body) != info.st_size:
                raise _fail("private DOCX UAT cleanup target drifted")
            total += len(body)
            if total > 128 * 1024 * 1024 or len(records) >= 10_000:
                raise _fail("private DOCX UAT cleanup target exceeded its bound")
            records.append({
                "kind": "file", "relative": (relative_root / name).as_posix(),
                "identity": [info.st_dev, info.st_ino],
                "size": len(body),
                "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            })
    after = os.lstat(path)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise _fail("private DOCX UAT cleanup target drifted")
    value = {
        "identity": [before.st_dev, before.st_ino],
        "records": records,
        "total_bytes": total,
    }
    value["tree_digest"] = canonical_digest(value)
    return value


def _validate_cleanup_top_entries(
    run: PrivateDocxPreparedRun, kind: str, path: Path
) -> None:
    present = set(os.listdir(path))
    if kind == "staging":
        expected = {f"{item['item_id']}.docx" for item in run.sources}
    elif kind == "control":
        expected = {"run-manifest.json"}
    elif kind == "corpus":
        expected = {"manifest.json"} | {
            f"{item['item_id']}.docx" for item in run.sources
        }
    elif kind == "batch":
        expected = {"checkpoint.json", "final-readback.json"}
    elif kind == "state":
        expected = {
            _PREPARED_EVIDENCE_NAME, "conversion-journal.jsonl",
            _WORKER_CONTRACT_NAME, _WORKER_BARRIER_NAME,
            "sandbox-home", "sandbox-cache",
        }
    elif kind == "candidates":
        from .batch_ingestion import load_final_batch_readback

        final = load_final_batch_readback(
            run.batch_manifest["batch_id"],
            batch_root=run.runtime_root / "batches" / run.batch_manifest["batch_id"],
        )
        if final is None:
            raise _fail("private DOCX UAT cleanup final state is absent")
        expected = {
            item["candidate_id"] for item in final["items"]
            if item["status"] in {"created", "unchanged"}
        }
    else:
        raise _fail("private DOCX UAT cleanup target is invalid")
    if present != expected:
        raise _fail("private DOCX UAT cleanup target entries drifted")


def _cleanup_target_paths(run: PrivateDocxPreparedRun) -> list[tuple[str, Path]]:
    return [
        ("candidates", repository_root() / "working" / "candidates" / run.staging_name),
        ("batch", run.runtime_root / "batches" / run.batch_manifest["batch_id"]),
        ("staging", repository_root() / "sources" / run.staging_name),
        ("control", repository_root() / "sources" / _control_directory(run.staging_name)),
        ("corpus", run.runtime_root / "private-docx" / "corpus"),
        ("state", run.runtime_root / "uat" / "private-docx" / run.batch_manifest["batch_id"]),
    ]


def _cleanup_boundary(run: PrivateDocxPreparedRun, kind: str) -> Path:
    if kind in {"candidates", "staging", "control"}:
        return repository_root()
    if kind in {"batch", "corpus", "state"}:
        return run.runtime_root
    raise _fail("private DOCX UAT cleanup target is invalid")


def _open_cleanup_parent(
    run: PrivateDocxPreparedRun,
    kind: str,
    path: Path,
    expected_identity: Sequence[int] | None = None,
) -> int:
    descriptor = _open_directory(
        path.parent, _cleanup_boundary(run, kind), create=False
    )
    info = os.fstat(descriptor)
    identity = [info.st_dev, info.st_ino]
    if expected_identity is not None and identity != list(expected_identity):
        os.close(descriptor)
        raise _fail("private DOCX UAT cleanup parent drifted")
    return descriptor


def _cleanup_relative_parts(value: Any) -> tuple[str, ...]:
    if type(value) is not str or not value or "\\" in value or "\x00" in value:
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    candidate = Path(value)
    parts = tuple(value.split("/"))
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    if candidate.as_posix() != value:
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    return parts


def _validate_cleanup_target(target: Any, kind: str) -> None:
    expected = {
        "kind", "present", "quarantine", "captured", "parent_identity",
        "phase", "reclaimed", "pending",
    }
    if type(target) is not dict or set(target) != expected or target["kind"] != kind:
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    _validate_identity(target["parent_identity"])
    if type(target["present"]) is not bool:
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    if type(target["reclaimed"]) is not list or len(target["reclaimed"]) != len(set(target["reclaimed"])):
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    for relative in target["reclaimed"]:
        _cleanup_relative_parts(relative)
    if target["pending"] is not None:
        _cleanup_relative_parts(target["pending"])
    if not target["present"]:
        if (
            target["quarantine"] is not None or target["captured"] is not None
            or target["phase"] != "absent" or target["reclaimed"]
            or target["pending"] is not None
        ):
            raise _fail("private DOCX UAT cleanup recovery is invalid")
        return
    quarantine = target["quarantine"]
    if (
        type(quarantine) is not str
        or _CLEANUP_QUARANTINE_RE.fullmatch(quarantine) is None
        or not quarantine.startswith(f".private-docx-cleanup-{kind}-")
        or target["phase"] not in {"planned", "quarantined", "removed"}
    ):
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    captured = target["captured"]
    if (
        type(captured) is not dict
        or set(captured) != {"identity", "records", "total_bytes", "tree_digest"}
    ):
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    _validate_identity(captured["identity"])
    if (
        type(captured["records"]) is not list
        or type(captured["total_bytes"]) is not int
        or isinstance(captured["total_bytes"], bool)
        or not 0 <= captured["total_bytes"] <= 128 * 1024 * 1024
        or type(captured["tree_digest"]) is not str
        or _DIGEST_RE.fullmatch(captured["tree_digest"]) is None
    ):
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    relatives: set[str] = set()
    total = 0
    for record in captured["records"]:
        if type(record) is not dict or record.get("kind") not in {"file", "directory"}:
            raise _fail("private DOCX UAT cleanup recovery is invalid")
        required = {"kind", "relative", "identity"}
        if record["kind"] == "file":
            required |= {"size", "digest"}
        if set(record) != required:
            raise _fail("private DOCX UAT cleanup recovery is invalid")
        _cleanup_relative_parts(record["relative"])
        if record["relative"] in relatives:
            raise _fail("private DOCX UAT cleanup recovery is invalid")
        relatives.add(record["relative"])
        _validate_identity(record["identity"])
        if record["kind"] == "file":
            if (
                type(record["size"]) is not int or isinstance(record["size"], bool)
                or not 0 <= record["size"] <= 64 * 1024 * 1024
                or type(record["digest"]) is not str
                or _DIGEST_RE.fullmatch(record["digest"]) is None
            ):
                raise _fail("private DOCX UAT cleanup recovery is invalid")
            total += record["size"]
    if (
        total != captured["total_bytes"]
        or canonical_digest({key: value for key, value in captured.items() if key != "tree_digest"})
        != captured["tree_digest"]
        or not set(target["reclaimed"]) <= relatives
        or (target["pending"] is not None and target["pending"] not in relatives)
    ):
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    if (
        target["phase"] == "planned"
        and (target["reclaimed"] or target["pending"] is not None)
    ) or (
        target["phase"] == "removed"
        and (
            set(target["reclaimed"]) != relatives
            or target["pending"] is not None
        )
    ):
        raise _fail("private DOCX UAT cleanup recovery is invalid")


def _reclaim_captured_cleanup_tree(
    path: Path,
    target: dict[str, Any],
    persist: Any,
    *,
    parent_descriptor: int,
    quarantine_name: str,
) -> None:
    captured = target["captured"]
    root_descriptor = os.open(
        path, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    )
    root_info = os.fstat(root_descriptor)
    if [root_info.st_dev, root_info.st_ino] != captured["identity"]:
        os.close(root_descriptor)
        raise _fail("private DOCX UAT cleanup quarantine drifted")
    directory_identities = {
        record["relative"]: record["identity"]
        for record in captured["records"] if record["kind"] == "directory"
    }

    def open_record_parent(relative: str) -> tuple[int, str]:
        parts = _cleanup_relative_parts(relative)
        descriptor = os.dup(root_descriptor)
        traversed: list[str] = []
        try:
            for component in parts[:-1]:
                traversed.append(component)
                child = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=descriptor,
                )
                os.close(descriptor)
                descriptor = child
                if [os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino] != directory_identities.get("/".join(traversed)):
                    raise _fail("private DOCX UAT cleanup quarantine drifted")
            return descriptor, parts[-1]
        except BaseException:
            os.close(descriptor)
            raise
    reclaimed = target["reclaimed"]
    if (
        type(reclaimed) is not list
        or any(type(item) is not str for item in reclaimed)
        or len(reclaimed) != len(set(reclaimed))
    ):
        raise _fail("private DOCX UAT cleanup progress drifted")
    known = {record["relative"] for record in captured["records"]}
    if not set(reclaimed) <= known:
        raise _fail("private DOCX UAT cleanup progress drifted")
    try:
        for record in reversed(captured["records"]):
            record_parent, entry_name = open_record_parent(record["relative"])
            try:
                exists = _binding_exists_at(record_parent, entry_name)
                reclaim_name = (
                    ".private-docx-reclaim-"
                    + hashlib.sha256(record["relative"].encode("utf-8")).hexdigest()[:24]
                )
                reclaim_exists = _binding_exists_at(record_parent, reclaim_name)
                if record["relative"] in reclaimed:
                    if exists or reclaim_exists:
                        raise _fail("private DOCX UAT cleanup target reappeared")
                    continue
                if target["pending"] not in {None, record["relative"]}:
                    raise _fail("private DOCX UAT cleanup progress drifted")
                if target["pending"] == record["relative"] and not exists:
                    if reclaim_exists:
                        moved = os.stat(
                            reclaim_name,
                            dir_fd=record_parent,
                            follow_symlinks=False,
                        )
                        if [moved.st_dev, moved.st_ino] != record["identity"]:
                            raise _fail("private DOCX UAT cleanup quarantine drifted")
                        if record["kind"] == "file":
                            body = _read_file_at(
                                record_parent, reclaim_name, 64 * 1024 * 1024
                            )
                            if (
                                len(body) != record["size"]
                                or "sha256:" + hashlib.sha256(body).hexdigest()
                                != record["digest"]
                            ):
                                raise _fail("private DOCX UAT cleanup quarantine drifted")
                            os.unlink(reclaim_name, dir_fd=record_parent)
                        else:
                            os.rmdir(reclaim_name, dir_fd=record_parent)
                        os.fsync(record_parent)
                    reclaimed.append(record["relative"])
                    target["pending"] = None
                    persist()
                    continue
                if reclaim_exists:
                    raise _fail("private DOCX UAT cleanup reclaim collision")
                if target["pending"] is None:
                    target["pending"] = record["relative"]
                    persist()
                info = os.stat(entry_name, dir_fd=record_parent, follow_symlinks=False)
                if [info.st_dev, info.st_ino] != record["identity"]:
                    raise _fail("private DOCX UAT cleanup quarantine drifted")
                _rename_noreplace_at(
                    record_parent, entry_name, record_parent, reclaim_name
                )
                os.fsync(record_parent)
                moved = os.stat(
                    reclaim_name, dir_fd=record_parent, follow_symlinks=False
                )
                if [moved.st_dev, moved.st_ino] != record["identity"]:
                    raise _fail("private DOCX UAT cleanup quarantine drifted")
                if record["kind"] == "file":
                    body = _read_file_at(record_parent, reclaim_name, 64 * 1024 * 1024)
                    if (
                        len(body) != record["size"]
                        or "sha256:" + hashlib.sha256(body).hexdigest() != record["digest"]
                    ):
                        raise _fail("private DOCX UAT cleanup quarantine drifted")
                    os.unlink(reclaim_name, dir_fd=record_parent)
                else:
                    os.rmdir(reclaim_name, dir_fd=record_parent)
                os.fsync(record_parent)
                reclaimed.append(record["relative"])
                target["pending"] = None
                persist()
            finally:
                os.close(record_parent)
        if os.listdir(root_descriptor):
            raise _fail("private DOCX UAT cleanup quarantine contains foreign entries")
    finally:
        os.close(root_descriptor)
    root_info = os.stat(
        quarantine_name, dir_fd=parent_descriptor, follow_symlinks=False
    )
    if [root_info.st_dev, root_info.st_ino] != captured["identity"]:
        raise _fail("private DOCX UAT cleanup quarantine drifted")
    os.rmdir(quarantine_name, dir_fd=parent_descriptor)


def cleanup_private_docx_uat_run(run: PrivateDocxPreparedRun) -> bool:
    """Crash-resume exact owned cleanup for one prepared DOCX campaign."""

    if not isinstance(run, PrivateDocxPreparedRun):
        raise _fail("private DOCX UAT cleanup run is invalid")
    marker_root = run.runtime_root / "uat" / "private-docx"
    marker_name = f"cleanup-{run.batch_manifest['batch_id']}.json"
    marker_path = marker_root / marker_name
    fixed = _cleanup_target_paths(run)
    if marker_path.exists():
        recovery, recovery_body = strict_read_json(
            marker_path, "private DOCX UAT cleanup recovery",
            max_bytes=512 * 1024, root=run.runtime_root,
        )
    else:
        targets = []
        for kind, path in fixed:
            parent_descriptor = _open_cleanup_parent(run, kind, path)
            try:
                parent_info = os.fstat(parent_descriptor)
                present = _binding_exists_at(parent_descriptor, path.name)
                anchored = Path(f"/proc/self/fd/{parent_descriptor}") / path.name
                if present:
                    _validate_cleanup_top_entries(run, kind, anchored)
                    captured = _capture_cleanup_tree(anchored)
                else:
                    captured = None
            finally:
                os.close(parent_descriptor)
            if present:
                targets.append({
                    "kind": kind,
                    "present": True,
                    "quarantine": f".private-docx-cleanup-{kind}-{secrets.token_hex(12)}",
                    "captured": captured,
                    "parent_identity": [parent_info.st_dev, parent_info.st_ino],
                    "phase": "planned",
                    "reclaimed": [],
                    "pending": None,
                })
            else:
                targets.append({
                    "kind": kind, "present": False, "quarantine": None,
                    "captured": None, "phase": "absent",
                    "reclaimed": [],
                    "pending": None,
                    "parent_identity": [parent_info.st_dev, parent_info.st_ino],
                })
        recovery = {
            "schema_version": "ao.lore.private-docx-uat-cleanup-recovery.v0.1",
            "format_id": "docx",
            "policy_digest": PRIVATE_DOCX_UAT_POLICY_DIGEST,
            "batch_id": run.batch_manifest["batch_id"],
            "batch_manifest_digest": run.batch_manifest_digest,
            "corpus_digest": run.corpus_digest,
            "staging_name": run.staging_name,
            "targets": targets,
            "corpus_cleaned": False,
            "authority": False,
        }
        recovery_body = _persist_uat_document(
            marker_root, run.runtime_root, marker_name, recovery
        )

    def persist() -> bytes:
        nonlocal recovery_body
        recovery_body = _persist_uat_document(
            marker_root, run.runtime_root, marker_name, recovery
        )
        return recovery_body

    if (
        type(recovery) is not dict
        or set(recovery) != {
            "schema_version", "format_id", "policy_digest", "batch_id",
            "batch_manifest_digest", "corpus_digest", "staging_name", "targets",
            "corpus_cleaned", "authority",
        }
        or recovery["schema_version"] != "ao.lore.private-docx-uat-cleanup-recovery.v0.1"
        or recovery["format_id"] != "docx"
        or recovery["policy_digest"] != PRIVATE_DOCX_UAT_POLICY_DIGEST
        or recovery["batch_id"] != run.batch_manifest["batch_id"]
        or recovery["batch_manifest_digest"] != run.batch_manifest_digest
        or recovery["corpus_digest"] != run.corpus_digest
        or recovery["staging_name"] != run.staging_name
        or recovery["authority"] is not False
        or type(recovery["targets"]) is not list
        or len(recovery["targets"]) != len(fixed)
    ):
        raise _fail("private DOCX UAT cleanup recovery is invalid")
    for target, (kind, source) in zip(recovery["targets"], fixed, strict=True):
        _validate_cleanup_target(target, kind)
        parent_descriptor = _open_cleanup_parent(
            run, kind, source, target["parent_identity"]
        )
        try:
            source_exists = _binding_exists_at(parent_descriptor, source.name)
            quarantine_name = target.get("quarantine")
            quarantine_exists = (
                type(quarantine_name) is str
                and _binding_exists_at(parent_descriptor, quarantine_name)
            )
            if target["phase"] in {"absent", "removed"}:
                if source_exists or quarantine_exists:
                    raise _fail("private DOCX UAT cleanup target reappeared")
                continue
            if type(quarantine_name) is not str:
                raise _fail("private DOCX UAT cleanup recovery is invalid")
            if target["phase"] == "planned":
                if source_exists:
                    if quarantine_exists:
                        raise _fail("private DOCX UAT cleanup quarantine collision")
                    info = os.stat(
                        source.name,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    if [info.st_dev, info.st_ino] != target["captured"]["identity"]:
                        raise _fail("private DOCX UAT cleanup target drifted")
                    _rename_noreplace_at(
                        parent_descriptor,
                        source.name,
                        parent_descriptor,
                        quarantine_name,
                    )
                    os.fsync(parent_descriptor)
                elif not quarantine_exists:
                    raise _fail("private DOCX UAT cleanup target disappeared")
                target["phase"] = "quarantined"
                persist()
            if target["phase"] == "quarantined":
                if _binding_exists_at(parent_descriptor, source.name):
                    raise _fail("private DOCX UAT cleanup quarantine drifted")
                if not _binding_exists_at(parent_descriptor, quarantine_name):
                    captured_relatives = {
                        record["relative"] for record in target["captured"]["records"]
                    }
                    if (
                        target["pending"] is not None
                        or set(target["reclaimed"]) != captured_relatives
                    ):
                        raise _fail("private DOCX UAT cleanup quarantine drifted")
                    target["phase"] = "removed"
                    persist()
                    continue
                quarantine = (
                    Path(f"/proc/self/fd/{parent_descriptor}") / quarantine_name
                )
                _reclaim_captured_cleanup_tree(
                    quarantine,
                    target,
                    persist,
                    parent_descriptor=parent_descriptor,
                    quarantine_name=quarantine_name,
                )
                os.fsync(parent_descriptor)
                target["phase"] = "removed"
                persist()
        finally:
            os.close(parent_descriptor)
    if recovery["corpus_cleaned"] is not True:
        if (run.runtime_root / "private-docx" / "corpus").exists():
            raise _fail("private DOCX UAT corpus cleanup drifted")
        recovery["corpus_cleaned"] = True
        persist()
    descriptor = _open_directory(marker_root, run.runtime_root, create=False)
    try:
        _unlink_verified_at(descriptor, marker_name, recovery_body, 512 * 1024)
    finally:
        os.close(descriptor)
    return True


def _docx_brain_snapshot() -> str:
    root = repository_root() / "brain"
    records: list[dict[str, Any]] = []
    total = 0
    for directory, names, files in os.walk(root, topdown=True, followlinks=False):
        names.sort()
        files.sort()
        for name in names:
            info = os.lstat(Path(directory) / name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise _fail("private DOCX UAT brain snapshot is invalid")
        for name in files:
            path = Path(directory) / name
            info = os.lstat(path)
            if (
                stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or info.st_size > 16 * 1024 * 1024
            ):
                raise _fail("private DOCX UAT brain snapshot is invalid")
            body = path.read_bytes()
            total += len(body)
            if total > 128 * 1024 * 1024 or len(records) >= 10_000:
                raise _fail("private DOCX UAT brain snapshot exceeded its bound")
            records.append({
                "relative": path.relative_to(root).as_posix(),
                "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            })
    return canonical_digest(records)


def _docx_candidate_snapshot(run: PrivateDocxPreparedRun | None = None) -> dict[str, dict[str, Any]]:
    if run is None:
        return {}
    root = repository_root() / "working" / "candidates" / run.staging_name
    if not root.exists():
        return {}
    from .candidate_queue import list_candidates

    report = list_candidates(status="all", limit=200, candidate_root=root)
    if report["next_after"] is not None:
        raise _fail("private DOCX UAT candidate queue exceeded its bound")
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


def _load_docx_final(run: PrivateDocxPreparedRun) -> Mapping[str, Any] | None:
    from .batch_ingestion import load_final_batch_readback

    return load_final_batch_readback(
        run.batch_manifest["batch_id"],
        batch_root=run.runtime_root / "batches" / run.batch_manifest["batch_id"],
    )


def _verify_docx_terminal(
    run: PrivateDocxPreparedRun, stdout: bytes, loaded: Mapping[str, Any] | None
) -> dict[str, Any]:
    from .batch_ingestion import validate_final_batch_readback

    emitted = parse_strict_json(stdout, "private DOCX UAT terminal output")
    emitted = validate_final_batch_readback(emitted)
    if loaded is None:
        raise _fail("private DOCX UAT terminal readback is absent")
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
        raise _fail("private DOCX UAT terminal readback drifted")
    return retained


def _verify_docx_queue(
    values: Any,
    terminal: Mapping[str, Any],
    before: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if type(values) is not dict or before:
        raise _fail("private DOCX UAT candidate queue is invalid")
    results: list[dict[str, Any]] = []
    expected: set[str] = set()
    for item in terminal["items"]:
        if item["status"] == "rejected":
            raise _fail("private DOCX UAT representative item was rejected")
        candidate_id = item["candidate_id"]
        queued = values.get(candidate_id)
        if (
            type(queued) is not dict
            or queued != {
                "candidate_id": candidate_id,
                "candidate_digest": item["candidate_digest"],
                "provenance_digest": item["provenance_digest"],
                "source_digest": item["source_digest"],
                "review_status": item["review_status"],
            }
            or queued["review_status"] != "unreviewed"
        ):
            raise _fail("private DOCX UAT candidate queue drifted")
        expected.add(candidate_id)
        results.append(dict(item))
    if set(values) != expected:
        raise _fail("private DOCX UAT candidate queue contains foreign results")
    return results


def _private_docx_shared_run(
    run: PrivateDocxPreparedRun, qualification: Mapping[str, Any]
) -> PrivateDocumentPreparedRun:
    manifest_path = repository_root() / "sources" / _control_directory(run.staging_name) / "run-manifest.json"
    info = os.stat(manifest_path, follow_symlinks=False)
    identity = (info.st_dev, info.st_ino)
    return PrivateDocumentPreparedRun(
        format_id="docx",
        policy_digest=PRIVATE_DOCX_UAT_POLICY_DIGEST,
        batch_id=run.batch_manifest["batch_id"],
        manifest=_strict_copy(run.corpus_manifest),
        manifest_digest=run.corpus_digest,
        manifest_identity=identity,
        staging_name=run.staging_name,
        control_identity=run.control_identity,
        source_bindings=tuple(
            private_document_state_body(
                PRIVATE_DOCX_UAT_POLICY,
                item_id=item["item_id"],
                source_digest=item["source_digest"],
            )
            for item in run.corpus_manifest["documents"]
        ),
        qualification_binding=private_document_state_body(
            PRIVATE_DOCX_UAT_POLICY,
            manifest_digest=run.corpus_digest,
            manifest_identity=list(identity),
            qualification=_strict_copy(qualification),
        ),
        expectation_binding=private_document_state_body(
            PRIVATE_DOCX_UAT_POLICY,
            manifest_digest=run.corpus_digest,
            manifest_identity=list(identity),
            item_ids=[item["item_id"] for item in run.corpus_manifest["documents"]],
            source_digests=[item["source_digest"] for item in run.corpus_manifest["documents"]],
        ),
    )


def _assemble_private_docx_readback(
    run: PrivateDocxPreparedRun,
    terminal: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
    _brain_digest: str,
    calibration: PrivateDocxCalibrationEvidence,
    conversion_counts: Mapping[str, int],
) -> dict[str, Any]:
    evidence = _validate_private_docx_calibration_evidence(calibration)
    if (
        evidence.corpus_digest != run.corpus_digest
        or evidence.expectation_digest != run.expectation_digest
        or evidence.qualification_digest
        != canonical_digest(_load_docx_qualification(run.runtime_root))
        or evidence.transformation_id != DOCX_TRANSFORMATION_ID
        or (evidence.source_set_digest, evidence.derived_set_digest)
        != _private_docx_source_sets(run)
    ):
        raise _fail("private DOCX UAT calibration origin drifted")
    aggregate = json.loads(json.dumps(evidence.aggregate, allow_nan=False))
    results = list(terminal["items"])
    origin = canonical_digest({
        "corpus_digest": run.corpus_digest,
        "expectation_digest": run.expectation_digest,
        "qualification_digest": evidence.qualification_digest,
        "configuration_digest": evidence.configuration_digest,
        "source_set_digest": evidence.source_set_digest,
        "derived_set_digest": evidence.derived_set_digest,
        "transformation_id": evidence.transformation_id,
    })
    value = {
        "schema_version": "ao.lore.private-docx-uat-readback.v0.1",
        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
        "corpus_digest": run.corpus_digest,
        "configuration_digest": evidence.configuration_digest,
        "qualification_digest": evidence.qualification_digest,
        "aggregate_digest": canonical_digest(aggregate),
        "uat_digest": canonical_digest({
            "batch_manifest_digest": run.batch_manifest_digest,
            "terminal": terminal,
            "checkpoint_digest": checkpoint["checkpoint_digest"],
        }),
        "campaign_origin_digest": origin,
        "lifecycle_status": "completed",
        "tuning_decision": derive_private_docx_tuning_decision(aggregate),
        "qualified": False,
        "counts": {
            "total": 4, "created": 4, "unchanged": 0, "rejected": 0,
            "processed": 4, "successful": 4, "candidate_queue": 4,
            "interrupted": 1, "resumed": 1, "rerun": 1,
        },
        "conversion_counts": dict(conversion_counts),
        "results": results,
        "aggregate": aggregate,
        "ocr_enabled": False, "ocr_used": False, "network_accessed": False,
        "provider_calls": False, "promotion_authority": False,
        "claims_authority_advance": False,
    }
    return validate_private_docx_readback(value, expected_item_ids=DOCX_UAT_ITEM_IDS)


def _load_docx_qualification(runtime: Path) -> dict[str, Any]:
    body = _read_exact_file(runtime / "private-docx" / "qualification.json", 8 * 1024 * 1024)
    value = parse_strict_json(body, "private DOCX UAT qualification")
    if type(value) is not dict:
        raise _fail("private DOCX UAT qualification is invalid")
    return value


def _write_evidence_file_at(descriptor: int, name: str, body: bytes) -> None:
    temporary = f".writing-{name}"
    if _binding_exists_at(descriptor, name):
        if _read_file_at(descriptor, name, 512 * 1024) != body:
            raise _fail("private DOCX retained evidence drifted")
        return
    if _binding_exists_at(descriptor, temporary):
        before = os.stat(temporary, dir_fd=descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > len(body)
        ):
            raise _fail("private DOCX retained evidence staging drifted")
        file_descriptor = os.open(
            temporary,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            partial = b""
            while len(partial) <= len(body):
                chunk = os.read(file_descriptor, min(64 * 1024, len(body) + 1 - len(partial)))
                if not chunk:
                    break
                partial += chunk
        finally:
            os.close(file_descriptor)
        after = os.stat(temporary, dir_fd=descriptor, follow_symlinks=False)
        if (
            (before.st_dev, before.st_ino, before.st_size)
            != (after.st_dev, after.st_ino, after.st_size)
            or not body.startswith(partial)
        ):
            raise _fail("private DOCX retained evidence staging drifted")
        os.unlink(temporary, dir_fd=descriptor)
        os.fsync(descriptor)
    file_descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=descriptor,
    )
    try:
        _write_all(file_descriptor, body, "retained evidence")
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
    if _read_file_at(descriptor, temporary, 512 * 1024) != body:
        raise _fail("private DOCX retained evidence drifted")
    _rename_noreplace_at(descriptor, temporary, descriptor, name)
    os.fsync(descriptor)
    if _read_file_at(descriptor, name, 512 * 1024) != body:
        raise _fail("private DOCX retained evidence drifted")


def _persist_private_docx_evidence(
    run: PrivateDocxPreparedRun,
    report: Mapping[str, Any],
    brain_post_run: str,
    brain_post_cleanup: str,
    *,
    brain_before: str | None = None,
) -> str:
    validated = validate_private_docx_readback(report, expected_item_ids=DOCX_UAT_ITEM_IDS)
    if (
        type(brain_before) is not str
        or brain_before != brain_post_run
        or brain_before != brain_post_cleanup
        or _DIGEST_RE.fullmatch(brain_before) is None
    ):
        raise _fail("private DOCX retained brain binding drifted")
    readback_body = _manifest_body(validated, 512 * 1024)
    readback_digest = "sha256:" + hashlib.sha256(readback_body).hexdigest()
    cleaned = {
        "schema_version": "ao.lore.private-docx-uat-cleaned-state.v0.1",
        "batch_id": run.batch_manifest["batch_id"],
        "batch_manifest_digest": run.batch_manifest_digest,
        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
        "corpus_digest": run.corpus_digest,
        "readback_digest": readback_digest,
        "brain_before_digest": brain_before,
        "brain_after_work_digest": brain_post_run,
        "brain_after_cleanup_digest": brain_post_cleanup,
        "campaign_origin_digest": validated["campaign_origin_digest"],
        "lifecycle_status": "cleaned",
        "authority": False,
    }
    cleaned_body = _manifest_body(cleaned, 512 * 1024)
    parent_path = run.runtime_root / "evidence" / "private-docx-uat"
    parent = _open_directory(parent_path, run.runtime_root, create=True)
    pending = f".pending-{run.batch_manifest['batch_id']}"
    intent = {
        "schema_version": "ao.lore.private-docx-uat-evidence-intent.v0.1",
        "batch_id": run.batch_manifest["batch_id"],
        "readback_name": "private-docx-uat-readback.json",
        "readback_digest": readback_digest,
        "cleaned_name": "cleaned-state.json",
        "cleaned_digest": "sha256:" + hashlib.sha256(cleaned_body).hexdigest(),
        "authority": False,
    }
    intent_body = _manifest_body(intent, 512 * 1024)
    child: int | None = None
    try:
        transient_names = [
            name for name in os.listdir(parent)
            if name.startswith(".pending-") and name != pending
        ]
        if transient_names:
            raise _fail("private DOCX retained evidence foreign staging exists")
        if _binding_exists_at(parent, run.batch_manifest["batch_id"]):
            if _binding_exists_at(parent, pending):
                raise _fail("private DOCX retained evidence ambiguous staging exists")
            existing = os.open(
                run.batch_manifest["batch_id"],
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            try:
                if (
                    set(os.listdir(existing))
                    != {"private-docx-uat-readback.json", "cleaned-state.json"}
                    or _read_file_at(existing, "private-docx-uat-readback.json", 512 * 1024)
                    != readback_body
                    or _read_file_at(existing, "cleaned-state.json", 512 * 1024)
                    != cleaned_body
                ):
                    raise _fail("private DOCX retained evidence collision")
                return readback_digest
            finally:
                os.close(existing)
        if not _binding_exists_at(parent, pending):
            os.mkdir(pending, 0o700, dir_fd=parent)
            os.fsync(parent)
        child = os.open(
            pending,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent,
        )
        allowed = {
            "evidence-intent.json",
            "private-docx-uat-readback.json",
            "cleaned-state.json",
            ".writing-evidence-intent.json",
            ".writing-private-docx-uat-readback.json",
            ".writing-cleaned-state.json",
        }
        if not set(os.listdir(child)) <= allowed:
            raise _fail("private DOCX retained evidence staging contains foreign entries")
        _write_evidence_file_at(child, "evidence-intent.json", intent_body)
        _write_evidence_file_at(child, "private-docx-uat-readback.json", readback_body)
        _write_evidence_file_at(child, "cleaned-state.json", cleaned_body)
        if set(os.listdir(child)) != {
            "evidence-intent.json", "private-docx-uat-readback.json",
            "cleaned-state.json",
        }:
            raise _fail("private DOCX retained evidence entries drifted")
        _unlink_verified_at(
            child, "evidence-intent.json", intent_body, 512 * 1024
        )
        if set(os.listdir(child)) != {
            "private-docx-uat-readback.json", "cleaned-state.json"
        }:
            raise _fail("private DOCX retained evidence entries drifted")
        os.fsync(child)
        _rename_noreplace_at(
            parent, pending, parent, run.batch_manifest["batch_id"]
        )
        os.fsync(parent)
    finally:
        if child is not None:
            os.close(child)
        os.close(parent)
    return readback_digest


def run_private_docx_uat(*, runtime_root: Path | None = None) -> dict[str, Any]:
    """Run the exact interrupted/resume/rerun/calibrate/cleanup DOCX campaign."""

    runtime = Path(runtime_root) if runtime_root is not None else runtime_home()
    qualification = _load_docx_qualification(runtime)
    box: dict[str, PrivateDocxPreparedRun] = {}
    brain_snapshots: list[str] = []

    def prepare(manifest: Mapping[str, Any], selected: Path) -> PrivateDocxPreparedRun:
        run = prepare_private_docx_uat_run(runtime_root=selected)
        if _strict_copy(run.corpus_manifest) != _strict_copy(manifest):
            raise _fail("private DOCX UAT manifest drifted")
        box["run"] = run
        return run

    def require_run() -> PrivateDocxPreparedRun:
        try:
            return box["run"]
        except KeyError as exc:
            raise _fail("private DOCX UAT prepared run is absent") from exc

    def brain_snapshot() -> str:
        value = _docx_brain_snapshot()
        brain_snapshots.append(value)
        return value

    def persist_readback(
        run: PrivateDocxPreparedRun,
        report: Mapping[str, Any],
        brain_post_run: str,
        brain_post_cleanup: str,
    ) -> str:
        if len(brain_snapshots) != 3:
            raise _fail("private DOCX retained brain binding drifted")
        return _persist_private_docx_evidence(
            run,
            report,
            brain_post_run,
            brain_post_cleanup,
            brain_before=brain_snapshots[0],
        )

    dependencies = PrivateDocumentUatDependencies(
        policy=PRIVATE_DOCX_UAT_POLICY,
        prepare_run=prepare,
        launch_process=lambda run, phase: _launch_private_docx_worker(
            run, phase, qualification=qualification, runtime_root=runtime
        ),
        calibration=lambda _manifest, _root: _run_private_docx_calibration_worker(
            require_run(), qualification, runtime_root=runtime
        ),
        cleanup=cleanup_private_docx_uat_run,
        brain_snapshot=brain_snapshot,
        candidate_snapshot=lambda: _docx_candidate_snapshot(box.get("run")),
        load_manifest=lambda selected: load_private_docx_manifest(runtime_root=selected),
        validate_prepared_run=_validate_live_prepared_run,
        project_run=lambda run: _private_docx_shared_run(run, qualification),
        conversion_snapshot=lambda: _conversion_journal_count(
            require_run(), runtime_root=runtime
        ),
        process_poll=lambda process: process.poll(),
        inspect_checkpoint=lambda run: (
            None
            if not (runtime / "batches" / run.batch_manifest["batch_id"] / "checkpoint.json").exists()
            else strict_read_json(
                runtime / "batches" / run.batch_manifest["batch_id"] / "checkpoint.json",
                "private DOCX UAT checkpoint", max_bytes=64 * 1024, root=runtime,
            )[0]
        ),
        inspect_barrier=lambda run, checkpoint: (
            (barrier := _inspect_private_docx_barrier(run, runtime_root=runtime))
            is not None
            and barrier["checkpoint_digest"] == checkpoint["checkpoint_digest"]
        ),
        send_signal=_signal_private_docx_worker,
        terminate_process=_terminate_worker,
        wait_process=_wait_private_docx_worker,
        load_final=_load_docx_final,
        load_queue=lambda: _docx_candidate_snapshot(require_run()),
        verify_terminal=_verify_docx_terminal,
        verify_queue=_verify_docx_queue,
        assemble_readback=_assemble_private_docx_readback,
        persist_readback=persist_readback,
        require_verified_process=lambda process, phase: (
            None
            if getattr(process, "_ao_lore_sandbox_verified", False) is True
            and getattr(process, "_ao_lore_sandbox_phase", None) == phase
            else (_ for _ in ()).throw(_fail("private DOCX UAT sandbox was not verified"))
        ),
        expected_item_ids=lambda manifest: [item["item_id"] for item in manifest["documents"]],
    )
    try:
        return run_private_document_uat(
            runtime_root=runtime, dependencies=dependencies
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        if isinstance(exc, PrivateDocxUatError) and str(exc) == "private DOCX UAT failed":
            raise
        raise _fail("private DOCX UAT failed") from exc


def validate_private_docx_readback(
    value: Mapping[str, Any],
    *,
    expected_item_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Validate one path-free DOCX UAT readback."""

    if type(value) is not dict:
        raise _fail()
    detached = _strict_copy(value)
    exact_keys = {
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
        "results",
        "aggregate",
        "ocr_enabled",
        "ocr_used",
        "network_accessed",
        "provider_calls",
        "promotion_authority",
        "claims_authority_advance",
    }
    if set(detached) != exact_keys:
        raise _fail()
    if (
        detached["schema_version"] != "ao.lore.private-docx-uat-readback.v0.1"
        or detached["corpus_id"] != DOCX_PRIVATE_CORPUS_ID
        or detached["lifecycle_status"] not in {"partial", "completed"}
        or detached["tuning_decision"] not in {"hold", "candidate_change", "investigate"}
        or detached["qualified"] is not False
        or detached["ocr_enabled"] is not False
        or detached["ocr_used"] is not False
        or detached["network_accessed"] is not False
        or detached["provider_calls"] is not False
        or detached["promotion_authority"] is not False
        or detached["claims_authority_advance"] is not False
    ):
        raise _fail()
    for field in (
        "corpus_digest",
        "configuration_digest",
        "qualification_digest",
        "aggregate_digest",
        "uat_digest",
        "campaign_origin_digest",
    ):
        if type(detached[field]) is not str or _DIGEST_RE.fullmatch(detached[field]) is None:
            raise _fail()
    aggregate = _validate_private_docx_calibration_aggregate(detached["aggregate"])
    if (
        detached["aggregate_digest"] != canonical_digest(aggregate)
        or detached["tuning_decision"]
        != derive_private_docx_tuning_decision(aggregate)
    ):
        raise _fail()
    counts = detached["counts"]
    expected_count_keys = {
        "total", "created", "unchanged", "rejected", "processed", "successful",
        "candidate_queue", "interrupted", "resumed", "rerun",
    }
    if (
        type(counts) is not dict
        or set(counts) != expected_count_keys
        or any(type(value) is not int or value < 0 for value in counts.values())
        or counts["total"] != len(DOCX_UAT_ITEM_IDS)
        or counts["processed"] != counts["total"]
        or counts["created"] + counts["unchanged"] + counts["rejected"] != counts["total"]
        or counts["successful"] != counts["created"] + counts["unchanged"]
        or counts["candidate_queue"] != counts["successful"]
        or (counts["interrupted"], counts["resumed"], counts["rerun"]) != (1, 1, 1)
    ):
        raise _fail()
    conversions = detached["conversion_counts"]
    if (
        type(conversions) is not dict
        or conversions != {"initial": 1, "resumed": 3, "rerun": 0}
    ):
        raise _fail()
    if expected_item_ids is not None:
        if list(expected_item_ids) != list(DOCX_UAT_ITEM_IDS):
            raise _fail()
    results = detached["results"]
    if type(results) is not list or len(results) != len(DOCX_UAT_ITEM_IDS):
        raise _fail()
    for index, result in enumerate(results):
        if type(result) is not dict:
            raise _fail()
        if set(result) != {
            "item_id",
            "source_digest",
            "status",
            "candidate_id",
            "candidate_digest",
            "provenance_digest",
            "review_status",
        }:
            raise _fail()
        if (
            result["item_id"] != DOCX_UAT_ITEM_IDS[index]
            or type(result["source_digest"]) is not str
            or _DIGEST_RE.fullmatch(result["source_digest"]) is None
            or result["status"] != "created"
            or type(result["candidate_id"]) is not str
            or "/" in result["candidate_id"]
            or type(result["candidate_digest"]) is not str
            or _DIGEST_RE.fullmatch(result["candidate_digest"]) is None
            or type(result["provenance_digest"]) is not str
            or _DIGEST_RE.fullmatch(result["provenance_digest"]) is None
            or result["review_status"] != "unreviewed"
        ):
            raise _fail()
    return detached
