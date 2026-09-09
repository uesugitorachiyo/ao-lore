"""Strict non-canonical candidate persistence and human review."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ._strict_io import (
    ContractError,
    _file_identity,
    ensure_contained,
    parse_strict_json,
    reject_symlink_ancestors,
    require_exact_keys,
    require_identifier,
    strict_read_json,
    write_exclusive_json,
)
from .benchmark import BenchmarkError, canonical_digest
from .evidence_selection_contracts import (
    validate_selection_evidence,
)
from .home import repository_root
from .knowledge_contracts import (
    KnowledgeContractError,
    validate_distillation_trace,
    validate_knowledge_payload,
)
from .private_docx_domain import DOCX_PRIVATE_CORPUS_ID, DOCX_TRANSFORMATION_ID


MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_RATIONALE = 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
EVENT_NAME_RE = re.compile(r"^(?P<sequence>[0-9]{6})-(?P<digest>[0-9a-f]{64})\.json$")

RESULT_KEYS = {
    "schema_version", "candidate", "candidate_digest", "distillation_trace"
}
CANDIDATE_V0_1_KEYS = {
    "schema_version", "candidate_id", "document_ir_digest", "concepts",
    "claim_mappings", "links", "contradiction_warnings", "canonical",
    "promotion_authority",
}
CANDIDATE_V0_2_KEYS = CANDIDATE_V0_1_KEYS | {
    "claims_schema_version", "claims_digest", "claims", "citations_schema_version",
    "citations_digest", "citations", "knowledge_policy",
}
CANDIDATE_V0_3_KEYS = {
    "schema_version", "candidate_id", "evidence_selection_digest",
    "evidence_origins", "concepts", "claim_mappings", "links",
    "contradiction_warnings", "claims_schema_version", "claims_digest",
    "claims", "citations_schema_version", "citations_digest", "citations",
    "knowledge_policy", "canonical", "promotion_authority",
}
PROVENANCE_V0_1_KEYS = {
    "schema_version", "candidate_id", "candidate_digest",
    "document_ir_digest", "source_digest", "parser_id", "parser_version",
    "parse_quality_report_digest", "parser_selection_report_digest",
    "distillation_trace_digest", "created_at",
}
SOURCE_ORIGIN_KEYS = {
    "format_id",
    "corpus_id",
    "original_digest",
    "derived_digest",
    "transformation_id",
    "expectation_digest",
}
PROVENANCE_V0_2_KEYS = PROVENANCE_V0_1_KEYS | {"source_origin"}
PROVENANCE_V0_3_KEYS = {
    "schema_version", "candidate_id", "candidate_digest", "proposal_id",
    "evidence_selection_digest", "primary_workspace_id", "registry_digest",
    "evidence_origins", "created_at",
}
EVENT_KEYS = {
    "schema_version", "sequence", "candidate_id", "candidate_digest",
    "previous_event_digest", "decision", "reviewer", "rationale",
    "recorded_at", "event_digest",
}


class CandidateError(ContractError):
    """Raised when candidate state violates containment or integrity."""


def _fail(message: str, exc: Exception | None = None) -> CandidateError:
    error = CandidateError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise CandidateError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CandidateError(f"{label} must be a UTC RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise _fail(f"{label} must be a UTC RFC3339 timestamp", exc)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise CandidateError(f"{label} must be a UTC RFC3339 timestamp")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Mapping[str, Any], label: str) -> str:
    try:
        return canonical_digest(value)
    except BenchmarkError as exc:
        raise _fail(f"{label} must contain strict JSON data", exc)


def _validate_source_origin(
    source_origin: Any, source_digest: str
) -> dict[str, Any]:
    if type(source_origin) is not dict:
        raise CandidateError("candidate provenance source origin is malformed")
    try:
        require_exact_keys(source_origin, SOURCE_ORIGIN_KEYS, "candidate provenance source_origin")
    except ContractError as exc:
        raise _fail(str(exc), exc)
    if any(type(source_origin[field]) is not str for field in SOURCE_ORIGIN_KEYS):
        raise CandidateError("candidate provenance source origin is malformed")
    if (
        source_origin["format_id"] != "docx"
        or source_origin["corpus_id"] != DOCX_PRIVATE_CORPUS_ID
        or source_origin["transformation_id"] != DOCX_TRANSFORMATION_ID
    ):
        raise CandidateError("candidate provenance source origin is malformed")
    original_digest = _digest(
        source_origin["original_digest"], "provenance.source_origin.original_digest"
    )
    derived_digest = _digest(
        source_origin["derived_digest"], "provenance.source_origin.derived_digest"
    )
    _digest(
        source_origin["expectation_digest"],
        "provenance.source_origin.expectation_digest",
    )
    if original_digest == derived_digest or derived_digest != source_digest:
        raise CandidateError("candidate provenance source origin is malformed")
    copy = json.loads(json.dumps(source_origin))
    _canonical(copy, "candidate provenance source origin")
    return copy


def validate_candidate_document(
    candidate: Any, expected_candidate_version: str | None = None
) -> tuple[dict[str, Any], str]:
    if not isinstance(candidate, Mapping):
        raise CandidateError("candidate must be an object")
    try:
        candidate_version = candidate.get("schema_version")
        expected_keys = (
            CANDIDATE_V0_2_KEYS
            if candidate_version == "ao.lore.okf-candidate.v0.2"
            else CANDIDATE_V0_3_KEYS
            if candidate_version == "ao.lore.okf-candidate.v0.3"
            else CANDIDATE_V0_1_KEYS
        )
        require_exact_keys(candidate, expected_keys, "candidate")
        candidate_id = require_identifier(candidate["candidate_id"], "candidate_id")
    except ContractError as exc:
        raise _fail(str(exc), exc)
    if not candidate_id.startswith("candidate-"):
        raise CandidateError("candidate_id must use the candidate- prefix")
    if candidate["schema_version"] not in {
        "ao.lore.okf-candidate.v0.1", "ao.lore.okf-candidate.v0.2",
        "ao.lore.okf-candidate.v0.3",
    } or (
        expected_candidate_version is not None
        and candidate["schema_version"] != expected_candidate_version
    ):
        raise CandidateError("unsupported candidate version")
    for field in ("concepts", "claim_mappings", "links", "contradiction_warnings"):
        if not isinstance(candidate[field], list):
            raise CandidateError(f"candidate.{field} must be an array")
    if candidate["canonical"] is not False or candidate["promotion_authority"] is not False:
        raise CandidateError("candidate cannot claim canonical or promotion authority")
    if candidate["schema_version"] == "ao.lore.okf-candidate.v0.1":
        _digest(candidate["document_ir_digest"], "document_ir_digest")
    else:
        try:
            validate_knowledge_payload(candidate["schema_version"], dict(candidate))
        except KnowledgeContractError as exc:
            label = (
                "answerable candidate payload is invalid"
                if candidate["schema_version"] == "ao.lore.okf-candidate.v0.2"
                else "evidence candidate payload is invalid"
            )
            raise _fail(label, exc)
    candidate_copy = json.loads(json.dumps(candidate))
    actual_digest = _canonical(candidate_copy, "candidate")
    return candidate_copy, actual_digest


def validate_distillation_result(
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    if not isinstance(result, Mapping):
        raise CandidateError("distillation result must be an object")
    try:
        require_exact_keys(result, RESULT_KEYS, "distillation result")
    except ContractError as exc:
        raise _fail(str(exc), exc)
    result_version = result["schema_version"]
    if result_version not in {"ao.lore.distillation-result.v0.1", "ao.lore.distillation-result.v0.2"}:
        raise CandidateError("unsupported distillation result version")
    if result_version == "ao.lore.distillation-result.v0.1":
        candidate_copy, actual_digest = validate_candidate_document(
            result["candidate"], "ao.lore.okf-candidate.v0.1"
        )
    else:
        candidate_copy, actual_digest = validate_candidate_document(result["candidate"])
        if candidate_copy["schema_version"] not in {
            "ao.lore.okf-candidate.v0.2",
            "ao.lore.okf-candidate.v0.3",
        }:
            raise CandidateError("unsupported candidate version")
    supplied_digest = _digest(result["candidate_digest"], "candidate_digest")
    if supplied_digest != actual_digest:
        raise CandidateError("candidate digest mismatch")
    if not isinstance(result["distillation_trace"], Mapping):
        raise CandidateError("distillation_trace must be an object")
    if result_version == "ao.lore.distillation-result.v0.2":
        try:
            validate_distillation_trace(result["distillation_trace"], candidate_copy)
        except KnowledgeContractError as exc:
            raise _fail("answerable distillation trace is invalid", exc)
    _canonical(result["distillation_trace"], "distillation_trace")
    return candidate_copy, actual_digest


def validate_provenance(
    provenance: Mapping[str, Any],
    candidate: Mapping[str, Any],
    candidate_digest: str,
) -> dict[str, Any]:
    if type(provenance) is not dict:
        raise CandidateError("candidate provenance must be an object")
    schema_version = provenance.get("schema_version")
    try:
        if schema_version == "ao.lore.candidate-provenance.v0.1":
            require_exact_keys(
                provenance, PROVENANCE_V0_1_KEYS, "candidate provenance"
            )
        elif schema_version == "ao.lore.candidate-provenance.v0.2":
            require_exact_keys(
                provenance, PROVENANCE_V0_2_KEYS, "candidate provenance"
            )
        elif schema_version == "ao.lore.candidate-provenance.v0.3":
            require_exact_keys(
                provenance, PROVENANCE_V0_3_KEYS, "candidate provenance"
            )
        else:
            raise CandidateError("unsupported candidate provenance version")
        candidate_id = require_identifier(provenance["candidate_id"], "provenance.candidate_id")
    except (ContractError, CandidateError) as exc:
        if isinstance(exc, CandidateError):
            raise
        raise _fail(str(exc), exc)
    if candidate_id != candidate["candidate_id"]:
        raise CandidateError("candidate provenance identity mismatch")
    if _digest(provenance["candidate_digest"], "provenance.candidate_digest") != candidate_digest:
        raise CandidateError("candidate provenance digest mismatch")
    if schema_version in {
        "ao.lore.candidate-provenance.v0.1",
        "ao.lore.candidate-provenance.v0.2",
    }:
        parser_id = require_identifier(provenance["parser_id"], "provenance.parser_id")
        if _digest(provenance["document_ir_digest"], "provenance.document_ir_digest") != candidate["document_ir_digest"]:
            raise CandidateError("candidate provenance document IR mismatch")
        source_digest = _digest(provenance["source_digest"], "provenance.source_digest")
        _digest(provenance["parse_quality_report_digest"], "provenance.parse_quality_report_digest")
        _digest(provenance["parser_selection_report_digest"], "provenance.parser_selection_report_digest")
        _digest(provenance["distillation_trace_digest"], "provenance.distillation_trace_digest")
        if not parser_id or type(provenance["parser_version"]) is not str or not provenance["parser_version"] or len(provenance["parser_version"]) > 128:
            raise CandidateError("candidate provenance parser identity is malformed")
        if schema_version == "ao.lore.candidate-provenance.v0.2":
            _validate_source_origin(provenance["source_origin"], source_digest)
    else:
        if candidate.get("schema_version") != "ao.lore.okf-candidate.v0.3":
            raise CandidateError("candidate provenance version differs")
        require_identifier(provenance["proposal_id"], "provenance.proposal_id")
        if _digest(provenance["evidence_selection_digest"], "provenance.evidence_selection_digest") != candidate["evidence_selection_digest"]:
            raise CandidateError("candidate provenance selection digest mismatch")
        primary_workspace_id = require_identifier(provenance["primary_workspace_id"], "provenance.primary_workspace_id")
        _digest(provenance["registry_digest"], "provenance.registry_digest")
        evidence_origins = provenance["evidence_origins"]
        if type(evidence_origins) is not list or not 1 <= len(evidence_origins) <= 32:
            raise CandidateError("candidate provenance evidence origins are invalid")
        validated_origins = [validate_selection_evidence(item) for item in evidence_origins]
        if validated_origins != candidate["evidence_origins"]:
            raise CandidateError("candidate provenance evidence origins differ")
        if primary_workspace_id not in {
            item["workspace_id"] for item in validated_origins
        }:
            raise CandidateError("candidate provenance primary workspace differs")
    _timestamp(provenance["created_at"], "provenance.created_at")
    copy = json.loads(json.dumps(provenance))
    _canonical(copy, "candidate provenance")
    return copy


def build_candidate_provenance(
    parse_result: Mapping[str, Any],
    distillation_result: Mapping[str, Any],
    *,
    created_at: str,
    source_origin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build exact persistence bindings without filesystem or authority effects."""

    parse_keys = {
        "schema_version", "document_digest", "selection_report", "attempts",
        "selected_parser", "quality_report", "decision", "document_ir",
    }
    if not isinstance(parse_result, Mapping) or set(parse_result) != parse_keys:
        raise CandidateError("production parse result keys differ")
    if (
        parse_result["schema_version"] != "ao.lore.production-parse-result.v0.1"
        or parse_result["decision"] != "accept"
    ):
        raise CandidateError("candidate provenance requires an accepted parse")
    document_ir = parse_result["document_ir"]
    if not isinstance(document_ir, Mapping):
        raise CandidateError("accepted parse lacks document IR")
    source = document_ir.get("source")
    parser = document_ir.get("parser")
    if not isinstance(source, Mapping) or not isinstance(parser, Mapping):
        raise CandidateError("accepted parse provenance is malformed")
    source_digest = _digest(parse_result["document_digest"], "parse document_digest")
    if source.get("digest") != source_digest:
        raise CandidateError("accepted parse source digest mismatch")
    parser_id = parser.get("parser_id")
    parser_version = parser.get("parser_version")
    try:
        parser_id = require_identifier(parser_id, "parse parser_id")
    except ContractError as exc:
        raise _fail(str(exc), exc)
    if (
        parse_result["selected_parser"] != parser_id
        or not isinstance(parser_version, str)
        or not parser_version
        or len(parser_version) > 128
    ):
        raise CandidateError("accepted parse parser binding mismatch")
    candidate, candidate_digest = validate_distillation_result(distillation_result)
    ir_digest = _canonical(document_ir, "document IR")
    if candidate["document_ir_digest"] != ir_digest:
        raise CandidateError("candidate document IR binding mismatch")
    quality = parse_result["quality_report"]
    selection = parse_result["selection_report"]
    trace = distillation_result["distillation_trace"]
    if not isinstance(quality, Mapping) or not isinstance(selection, Mapping):
        raise CandidateError("accepted parse reports are malformed")
    if quality.get("parser_id") != parser_id or quality.get("parser_version") != parser_version:
        raise CandidateError("parse quality report parser binding mismatch")
    timestamp = _timestamp(created_at, "provenance.created_at")
    provenance: dict[str, Any] = {
        "schema_version": "ao.lore.candidate-provenance.v0.1",
        "candidate_id": candidate["candidate_id"],
        "candidate_digest": candidate_digest,
        "document_ir_digest": ir_digest,
        "source_digest": source_digest,
        "parser_id": parser_id,
        "parser_version": parser_version,
        "parse_quality_report_digest": _canonical(quality, "parse quality report"),
        "parser_selection_report_digest": _canonical(selection, "parser selection report"),
        "distillation_trace_digest": _canonical(trace, "distillation trace"),
        "created_at": timestamp,
    }
    if source_origin is not None:
        provenance["schema_version"] = "ao.lore.candidate-provenance.v0.2"
        provenance["source_origin"] = json.loads(json.dumps(source_origin))
    return validate_provenance(provenance, candidate, candidate_digest)


def _candidate_root(
    candidate_root: Path | None,
    *,
    allowed_root: Path | None = None,
) -> Path:
    root = repository_root()
    allowed = root / "working" / "candidates" if allowed_root is None else Path(allowed_root)
    selected = allowed if candidate_root is None else Path(candidate_root)
    try:
        selected, _ = ensure_contained(selected, allowed, "candidate root")
        reject_symlink_ancestors(selected, include_self=True)
        info = os.lstat(selected)
    except (ContractError, OSError) as exc:
        raise _fail("candidate root must be a contained real directory", exc)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CandidateError("candidate root must be a contained real directory")
    return selected


def _candidate_directory(root: Path, candidate_id: str) -> Path:
    try:
        safe_id = require_identifier(candidate_id, "candidate_id")
        path, _ = ensure_contained(root / safe_id, root, "candidate path")
    except ContractError as exc:
        raise _fail(str(exc), exc)
    if not safe_id.startswith("candidate-"):
        raise CandidateError("candidate_id must use the candidate- prefix")
    return path


def _strict_load(path: Path, label: str, root: Path) -> dict[str, Any]:
    try:
        value, _ = strict_read_json(path, label, max_bytes=MAX_RECORD_BYTES, root=root)
        return value
    except ContractError as exc:
        raise _fail(str(exc), exc)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_existing(
    target: Path,
    root: Path,
    candidate: Mapping[str, Any],
    provenance: Mapping[str, Any],
) -> None:
    try:
        reject_symlink_ancestors(target, include_self=True)
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise CandidateError("existing candidate path is not a real directory")
        stored_candidate = _strict_load(target / "candidate.json", "existing candidate", root)
        stored_provenance = _strict_load(target / "provenance.json", "existing provenance", root)
        reviews = target / "reviews"
        review_info = os.lstat(reviews)
        if stat.S_ISLNK(review_info.st_mode) or not stat.S_ISDIR(review_info.st_mode):
            raise CandidateError("existing candidate reviews path is not a real directory")
    except (CandidateError, ContractError, OSError) as exc:
        if isinstance(exc, CandidateError):
            raise
        raise _fail("existing candidate state is invalid", exc)
    if stored_candidate != candidate or stored_provenance != provenance:
        raise CandidateError("existing candidate conflicts with supplied content")


def _persist_candidate(
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    candidate_root: Path | None = None,
    allowed_candidate_root: Path | None = None,
) -> dict[str, Any]:
    candidate, candidate_digest = validate_distillation_result(result)
    validated_provenance = validate_provenance(provenance, candidate, candidate_digest)
    root = _candidate_root(candidate_root, allowed_root=allowed_candidate_root)
    target = _candidate_directory(root, candidate["candidate_id"])
    if target.exists() or target.is_symlink():
        try:
            _validate_existing(target, root, candidate, validated_provenance)
        except CandidateError as exc:
            raise _fail(f"existing candidate is invalid: {exc}", exc)
        return {"status": "unchanged", "candidate_id": candidate["candidate_id"], "candidate_digest": candidate_digest}

    temporary = Path(tempfile.mkdtemp(prefix=f".{candidate['candidate_id']}.", dir=root))
    complete = False
    try:
        os.chmod(temporary, 0o700)
        write_exclusive_json(temporary / "candidate.json", candidate, root=temporary)
        write_exclusive_json(temporary / "provenance.json", validated_provenance, root=temporary)
        (temporary / "reviews").mkdir(mode=0o700)
        _fsync_directory(temporary)
        try:
            os.rename(temporary, target)
        except FileExistsError:
            _validate_existing(target, root, candidate, validated_provenance)
            return {"status": "unchanged", "candidate_id": candidate["candidate_id"], "candidate_digest": candidate_digest}
        _fsync_directory(root)
        complete = True
    except (ContractError, OSError) as exc:
        if isinstance(exc, CandidateError):
            raise
        raise _fail("candidate persistence failed closed", exc)
    finally:
        if not complete and temporary.exists():
            shutil.rmtree(temporary)
    return {"status": "created", "candidate_id": candidate["candidate_id"], "candidate_digest": candidate_digest}


def persist_candidate(
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    return _persist_candidate(
        result,
        provenance,
        candidate_root=candidate_root,
    )


_SANITIZED_REHEARSAL_OWNER_NAME = ".ao-lore-sanitized-lifecycle-owner.json"
_SANITIZED_REHEARSAL_OWNER_BYTES = (
    b'{"owner":"ao-lore-sanitized-lifecycle-fixture","schema_version":"v0.1"}\n'
)
_DIRECTORY_NOFOLLOW = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _directory_identity(descriptor: int) -> tuple[int, int]:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise CandidateError("candidate persistence boundary is invalid")
    return info.st_dev, info.st_ino


def _open_directory_at(parent: int, name: str) -> int:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise CandidateError("sealed candidate directory is invalid")
    descriptor = os.open(name, _DIRECTORY_NOFOLLOW, dir_fd=parent)
    opened = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
        os.close(descriptor)
        raise CandidateError("sealed candidate directory changed")
    return descriptor


def _read_regular_at(
    parent: int, name: str, label: str, maximum: int = MAX_RECORD_BYTES,
) -> bytes:
    descriptor = None
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 <= before.st_size <= maximum
        ):
            raise CandidateError(f"sealed {label} is invalid")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(opened) or opened.st_nlink != 1:
            raise CandidateError(f"sealed {label} changed")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            len(body) > maximum
            or after.st_nlink != 1
            or rebound.st_nlink != 1
            or _file_identity(opened) != _file_identity(after)
            or _file_identity(opened) != _file_identity(rebound)
        ):
            raise CandidateError(f"sealed {label} changed")
        return body
    except OSError as exc:
        raise _fail(f"sealed {label} is invalid", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json_at(parent: int, name: str, label: str) -> tuple[dict[str, Any], bytes]:
    body = _read_regular_at(parent, name, label)
    try:
        return parse_strict_json(body, label), body
    except ContractError as exc:
        raise _fail(f"sealed {label} is invalid", exc)


def _write_json_at(parent: int, name: str, value: Mapping[str, Any]) -> bytes:
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = None
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short candidate write")
            view = view[written:]
        os.fsync(descriptor)
        created = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(created.st_mode)
            or created.st_nlink != 1
            or _file_identity(created) != _file_identity(rebound)
        ):
            raise CandidateError("sealed candidate write changed")
        return body
    except OSError as exc:
        raise _fail("sealed candidate write failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


class _SealedCandidateStore:
    """Private dirfd-only candidate capability for one fixed rehearsal run."""

    def __init__(
        self,
        campaign_root: Path,
        campaign_descriptor: int,
        campaign_identity: tuple[int, int],
        proof_root: Path,
        proof_descriptor: int,
        proof_identity: tuple[int, int],
        candidate_root: Path,
        candidate_descriptor: int,
        candidate_identity: tuple[int, int],
        boundary_hook: Callable[[str], None],
    ) -> None:
        self._campaign_root = campaign_root
        self._campaign_descriptor = campaign_descriptor
        self._campaign_identity = campaign_identity
        self._proof_root = proof_root
        self._proof_descriptor = proof_descriptor
        self._proof_identity = proof_identity
        self.candidate_root = candidate_root
        self._descriptor = candidate_descriptor
        self._identity = candidate_identity
        self._boundary_hook = boundary_hook
        self._candidate_mutex = threading.RLock()
        self._closed = False

    def _verify(self) -> None:
        if self._closed:
            raise CandidateError("sealed candidate store is closed")
        for path, descriptor, identity in (
            (
                self._campaign_root,
                self._campaign_descriptor,
                self._campaign_identity,
            ),
            (self._proof_root, self._proof_descriptor, self._proof_identity),
            (self.candidate_root, self._descriptor, self._identity),
        ):
            try:
                held = os.fstat(descriptor)
                rebound = os.stat(path, follow_symlinks=False)
            except OSError as exc:
                raise _fail("candidate persistence boundary changed", exc)
            if (
                not stat.S_ISDIR(held.st_mode)
                or not stat.S_ISDIR(rebound.st_mode)
                or (held.st_dev, held.st_ino) != identity
                or (rebound.st_dev, rebound.st_ino) != identity
            ):
                raise CandidateError("candidate persistence boundary changed")

    def _before(self, event: str) -> None:
        self._verify()
        self._boundary_hook(f"{event}.before")

    def _after(self, event: str) -> None:
        self._boundary_hook(f"{event}.after")
        self._verify()

    def close(self) -> None:
        self._closed = True

    def _candidate_fd(self, candidate_id: str) -> int:
        try:
            safe_id = require_identifier(candidate_id, "candidate_id")
        except ContractError as exc:
            raise _fail("candidate identity is invalid", exc)
        if not safe_id.startswith("candidate-"):
            raise CandidateError("candidate identity is invalid")
        return _open_directory_at(self._descriptor, safe_id)

    def _verify_candidate_marker(self, descriptor: int) -> None:
        self._verify()
        try:
            held_marker = os.fstat(descriptor)
            path_marker = os.stat(
                ".candidate.lock",
                dir_fd=self._descriptor,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise _fail("sealed candidate lock changed", exc)
        if (
            not stat.S_ISREG(held_marker.st_mode)
            or held_marker.st_nlink != 1
            or held_marker.st_size != 0
            or not stat.S_ISREG(path_marker.st_mode)
            or path_marker.st_nlink != 1
            or path_marker.st_size != 0
            or (held_marker.st_dev, held_marker.st_ino)
            != (path_marker.st_dev, path_marker.st_ino)
        ):
            raise CandidateError("sealed candidate lock changed")

    @contextmanager
    def candidate_lock(self) -> Iterator[None]:
        with self._candidate_mutex:
            with self._candidate_marker():
                yield

    @contextmanager
    def _candidate_marker(self) -> Iterator[None]:
        self._before("candidate-lock")
        marker_descriptor = None
        try:
            marker_descriptor = os.open(
                ".candidate.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._descriptor,
            )
            info = os.fstat(marker_descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
                raise CandidateError("sealed candidate lock is invalid")
            self._after("candidate-lock")
            self._verify_candidate_marker(marker_descriptor)
            yield
        except OSError as exc:
            raise _fail("sealed candidate lock is invalid", exc)
        finally:
            if marker_descriptor is not None:
                lock_error = None
                try:
                    self._verify_candidate_marker(marker_descriptor)
                except CandidateError as exc:
                    lock_error = exc
                try:
                    os.close(marker_descriptor)
                except OSError as exc:
                    if lock_error is None:
                        lock_error = _fail(
                            "sealed candidate lock is invalid", exc,
                        )
                if lock_error is not None:
                    raise lock_error

    def _load_values(
        self, candidate_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, bytes, bytes, int]:
        candidate_fd = self._candidate_fd(candidate_id)
        try:
            if set(os.listdir(candidate_fd)) != {
                "candidate.json", "provenance.json", "reviews",
            }:
                raise CandidateError("sealed candidate inventory differs")
            candidate_value, candidate_body = _read_json_at(
                candidate_fd, "candidate.json", "candidate",
            )
            candidate, candidate_digest = validate_candidate_document(candidate_value)
            provenance_value, provenance_body = _read_json_at(
                candidate_fd, "provenance.json", "candidate provenance",
            )
            provenance = validate_provenance(
                provenance_value, candidate, candidate_digest,
            )
            if candidate["candidate_id"] != candidate_id:
                raise CandidateError("sealed candidate identity differs")
            return (
                candidate, provenance, candidate_digest,
                candidate_body, provenance_body, candidate_fd,
            )
        except BaseException:
            os.close(candidate_fd)
            raise

    def _inspection(
        self,
        candidate: Mapping[str, Any],
        provenance: Mapping[str, Any],
        candidate_digest: str,
        candidate_fd: int,
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any], bytes]]]:
        reviews_fd = _open_directory_at(candidate_fd, "reviews")
        try:
            names = sorted(os.listdir(reviews_fd))
            if len(names) > 1024:
                raise CandidateError("sealed review chain exceeds budget")
            previous = None
            latest = None
            events: list[tuple[str, dict[str, Any], bytes]] = []
            for sequence, name in enumerate(names, 1):
                match = EVENT_NAME_RE.fullmatch(name)
                if match is None or int(match.group("sequence")) != sequence:
                    raise CandidateError("review event filenames must be contiguous and canonical")
                value, body = _read_json_at(reviews_fd, name, "review event")
                event = _validate_event(
                    value,
                    sequence=sequence,
                    candidate_id=candidate["candidate_id"],
                    candidate_digest=candidate_digest,
                    previous_digest=previous,
                )
                if match.group("digest") != event["event_digest"][7:]:
                    raise CandidateError("review event filename digest mismatch")
                previous = event["event_digest"]
                latest = event["decision"]
                events.append((name, event, body))
            inspection = {
                "schema_version": "ao.lore.candidate-inspection.v0.1",
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": candidate_digest,
                "provenance_digest": _canonical(provenance, "candidate provenance"),
                "verified_review_events": len(events),
                "review_status": {
                    None: "unreviewed", "accept": "accepted", "reject": "rejected",
                }[latest],
                "latest_event_digest": previous,
                "canonical": candidate["canonical"],
                "promotion_authority": candidate["promotion_authority"],
            }
            return inspection, events
        finally:
            os.close(reviews_fd)

    def persist(
        self, result: Mapping[str, Any], provenance: Mapping[str, Any],
    ) -> dict[str, Any]:
        candidate, candidate_digest = validate_distillation_result(result)
        bound = validate_provenance(provenance, candidate, candidate_digest)
        candidate_id = candidate["candidate_id"]
        self._before("persist")
        temporary = f".{candidate_id}.{secrets.token_hex(8)}"
        created = False
        temporary_fd = None
        try:
            try:
                existing_fd = self._candidate_fd(candidate_id)
            except FileNotFoundError:
                existing_fd = None
            if existing_fd is not None:
                os.close(existing_fd)
                reopened = self.load(candidate_id, _emit_boundary=False)
                if reopened["candidate"] != candidate or reopened["provenance"] != bound:
                    raise CandidateError("existing candidate conflicts with supplied content")
                return {
                    "status": "unchanged", "candidate_id": candidate_id,
                    "candidate_digest": candidate_digest,
                }
            os.mkdir(temporary, 0o700, dir_fd=self._descriptor)
            created = True
            temporary_fd = _open_directory_at(self._descriptor, temporary)
            _write_json_at(temporary_fd, "candidate.json", candidate)
            _write_json_at(temporary_fd, "provenance.json", bound)
            os.mkdir("reviews", 0o700, dir_fd=temporary_fd)
            os.fsync(temporary_fd)
            try:
                os.rename(
                    temporary, candidate_id,
                    src_dir_fd=self._descriptor, dst_dir_fd=self._descriptor,
                )
            except FileExistsError:
                raise CandidateError("candidate persistence raced")
            os.fsync(self._descriptor)
            created = False
            return {
                "status": "created", "candidate_id": candidate_id,
                "candidate_digest": candidate_digest,
            }
        finally:
            if temporary_fd is not None:
                os.close(temporary_fd)
            if created:
                try:
                    cleanup_fd = _open_directory_at(self._descriptor, temporary)
                    try:
                        try:
                            reviews_fd = _open_directory_at(cleanup_fd, "reviews")
                        except (CandidateError, OSError):
                            reviews_fd = None
                        if reviews_fd is not None:
                            os.close(reviews_fd)
                            os.rmdir("reviews", dir_fd=cleanup_fd)
                        for name in ("candidate.json", "provenance.json"):
                            try:
                                os.unlink(name, dir_fd=cleanup_fd)
                            except FileNotFoundError:
                                pass
                    finally:
                        os.close(cleanup_fd)
                    os.rmdir(temporary, dir_fd=self._descriptor)
                except (CandidateError, OSError):
                    pass
            self._after("persist")

    def load(self, candidate_id: str, *, _emit_boundary: bool = True) -> dict[str, Any]:
        if _emit_boundary:
            self._before("load")
        try:
            candidate, provenance, digest, _, _, candidate_fd = self._load_values(
                candidate_id
            )
            try:
                inspection, _ = self._inspection(
                    candidate, provenance, digest, candidate_fd,
                )
                return json.loads(json.dumps({
                    "candidate": candidate,
                    "provenance": provenance,
                    "inspection": inspection,
                }))
            finally:
                os.close(candidate_fd)
        finally:
            if _emit_boundary:
                self._after("load")

    def artifacts(self, candidate_id: str) -> dict[str, object]:
        self._before("artifacts")
        try:
            (
                candidate, provenance, digest,
                candidate_body, provenance_body, candidate_fd,
            ) = self._load_values(candidate_id)
            try:
                _, events = self._inspection(candidate, provenance, digest, candidate_fd)
                return {
                    "candidate.json": (candidate, candidate_body),
                    "provenance.json": (provenance, provenance_body),
                    "reviews": events,
                }
            finally:
                os.close(candidate_fd)
        finally:
            self._after("artifacts")

    def candidate_ids(self) -> list[str]:
        self._before("enumerate")
        try:
            result = []
            for name in sorted(os.listdir(self._descriptor)):
                if name == ".candidate.lock":
                    _read_regular_at(self._descriptor, name, "candidate lock", maximum=0)
                    continue
                info = os.stat(name, dir_fd=self._descriptor, follow_symlinks=False)
                if (
                    not name.startswith("candidate-")
                    or stat.S_ISLNK(info.st_mode)
                    or not stat.S_ISDIR(info.st_mode)
                ):
                    raise CandidateError("sealed candidate root inventory differs")
                result.append(name)
            return result
        finally:
            self._after("enumerate")

    def children(self, candidate_id: str) -> set[str]:
        self._before("enumerate")
        candidate_fd = None
        try:
            candidate_fd = self._candidate_fd(candidate_id)
            return set(os.listdir(candidate_fd))
        finally:
            if candidate_fd is not None:
                os.close(candidate_fd)
            self._after("enumerate")

    def inspect(self, candidate_id: str) -> dict[str, Any]:
        self._before("inspect")
        try:
            candidate, provenance, digest, _, _, candidate_fd = self._load_values(
                candidate_id
            )
            try:
                return self._inspection(candidate, provenance, digest, candidate_fd)[0]
            finally:
                os.close(candidate_fd)
        finally:
            self._after("inspect")

    def append_review(
        self,
        candidate_id: str,
        decision: str,
        reviewer: str,
        rationale: str = "",
        *,
        recorded_at: str | None = None,
    ) -> dict[str, Any]:
        if decision not in {"accept", "reject"}:
            raise CandidateError("review decision must be accept or reject")
        try:
            reviewer = require_identifier(reviewer, "reviewer")
        except ContractError as exc:
            raise _fail("reviewer is invalid", exc)
        if not isinstance(rationale, str) or len(rationale) > MAX_RATIONALE:
            raise CandidateError("review rationale must be bounded text")
        timestamp = _timestamp(recorded_at or _now(), "recorded_at")
        self._before("append-review")
        candidate_fd = lock_fd = reviews_fd = None
        try:
            candidate, provenance, digest, _, _, candidate_fd = self._load_values(
                candidate_id
            )
            lock_fd = os.open(
                ".review.lock",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=candidate_fd,
            )
            inspection, _ = self._inspection(candidate, provenance, digest, candidate_fd)
            sequence = inspection["verified_review_events"] + 1
            event = {
                "schema_version": "ao.lore.candidate-review-event.v0.1",
                "sequence": sequence,
                "candidate_id": candidate_id,
                "candidate_digest": digest,
                "previous_event_digest": inspection["latest_event_digest"],
                "decision": decision,
                "reviewer": reviewer,
                "rationale": rationale,
                "recorded_at": timestamp,
            }
            event["event_digest"] = _canonical(event, "review event")
            event = _validate_event(
                event,
                sequence=sequence,
                candidate_id=candidate_id,
                candidate_digest=digest,
                previous_digest=inspection["latest_event_digest"],
            )
            reviews_fd = _open_directory_at(candidate_fd, "reviews")
            _write_json_at(
                reviews_fd,
                f"{sequence:06d}-{event['event_digest'][7:]}.json",
                event,
            )
            os.fsync(reviews_fd)
            return event
        except FileExistsError as exc:
            raise _fail("candidate review is already locked", exc)
        finally:
            if reviews_fd is not None:
                os.close(reviews_fd)
            if lock_fd is not None:
                os.close(lock_fd)
            if candidate_fd is not None:
                try:
                    os.unlink(".review.lock", dir_fd=candidate_fd)
                except FileNotFoundError:
                    pass
                os.close(candidate_fd)
            self._after("append-review")

    def promotion_state(
        self, candidate_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any], str, str]:
        self._before("promotion-state")
        try:
            candidate, provenance, digest, _, _, candidate_fd = self._load_values(
                candidate_id
            )
            try:
                inspection, _ = self._inspection(
                    candidate, provenance, digest, candidate_fd,
                )
                if inspection["review_status"] != "accepted":
                    raise CandidateError("candidate latest review must be accepted")
                return candidate, provenance, digest, inspection["latest_event_digest"]
            finally:
                os.close(candidate_fd)
        finally:
            self._after("promotion-state")


@dataclass(frozen=True)
class _CandidatePersistenceDependencies:
    store: _SealedCandidateStore


def _verify_candidate_persistence_boundary(
    dependencies: _CandidatePersistenceDependencies,
) -> None:
    if type(dependencies) is not _CandidatePersistenceDependencies:
        raise CandidateError("candidate persistence dependencies are invalid")
    dependencies.store._verify()


@contextmanager
def _candidate_persistence_context_for_sanitized_rehearsal(
    run_root: Path,
) -> Iterator[_CandidatePersistenceDependencies]:
    """Hold an exact ignored rehearsal candidate root by descriptor identity."""

    with _sealed_candidate_store_for_sanitized_rehearsal(run_root) as store:
        yield _CandidatePersistenceDependencies(store)


@contextmanager
def _sealed_candidate_store_for_sanitized_rehearsal(
    run_root: Path,
    *,
    boundary_hook: Callable[[str], None] = lambda _event: None,
) -> Iterator[_SealedCandidateStore]:
    campaign = repository_root() / ".ao-lore" / "sanitized-lifecycle-rehearsal"
    descriptors: list[int] = []
    run_locked = False
    store = None
    try:
        selected, _ = ensure_contained(Path(run_root), campaign, "rehearsal run")
        if selected.parent != campaign:
            raise ContractError("rehearsal run must be a direct campaign child")
        require_identifier(selected.name, "rehearsal run")
        reject_symlink_ancestors(selected, include_self=True)
        owner, owner_body = strict_read_json(
            selected / _SANITIZED_REHEARSAL_OWNER_NAME,
            "rehearsal owner",
            max_bytes=256,
            root=selected,
        )
        owner_info = os.lstat(selected / _SANITIZED_REHEARSAL_OWNER_NAME)
        if (
            owner_body != _SANITIZED_REHEARSAL_OWNER_BYTES
            or owner != {
                "owner": "ao-lore-sanitized-lifecycle-fixture",
                "schema_version": "v0.1",
            }
            or owner_info.st_nlink != 1
            or not stat.S_ISREG(owner_info.st_mode)
        ):
            raise ContractError("rehearsal owner differs")

        campaign_fd = os.open(campaign, _DIRECTORY_NOFOLLOW)
        descriptors.append(campaign_fd)
        run_fd = os.open(selected.name, _DIRECTORY_NOFOLLOW, dir_fd=campaign_fd)
        descriptors.append(run_fd)
        fcntl.flock(run_fd, fcntl.LOCK_EX)
        run_locked = True
        working_fd = os.open("working", _DIRECTORY_NOFOLLOW, dir_fd=run_fd)
        descriptors.append(working_fd)
        candidate_fd = os.open(
            "candidates", _DIRECTORY_NOFOLLOW, dir_fd=working_fd,
        )
        descriptors.append(candidate_fd)
        candidate_root = selected / "working" / "candidates"
        store = _SealedCandidateStore(
            campaign,
            campaign_fd,
            _directory_identity(campaign_fd),
            selected,
            run_fd,
            _directory_identity(run_fd),
            candidate_root,
            candidate_fd,
            _directory_identity(candidate_fd),
            boundary_hook,
        )
        store._verify()
        yield store
    except CandidateError:
        raise
    except (ContractError, OSError, ValueError) as exc:
        raise _fail("candidate persistence boundary is invalid", exc)
    finally:
        boundary_error = None
        if store is not None:
            try:
                store._verify()
            except CandidateError as exc:
                boundary_error = exc
            store.close()
        unlock_error = None
        if run_locked:
            try:
                fcntl.flock(run_fd, fcntl.LOCK_UN)
            except OSError as exc:
                unlock_error = _fail(
                    "candidate persistence boundary is invalid", exc,
                )
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if boundary_error is not None:
            raise boundary_error
        if unlock_error is not None:
            raise unlock_error


def _persist_candidate_with_persistence_dependencies(
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    dependencies: _CandidatePersistenceDependencies,
) -> dict[str, Any]:
    """Persist only while the sealed rehearsal candidate descriptor is held."""

    _verify_candidate_persistence_boundary(dependencies)
    persisted = dependencies.store.persist(result, provenance)
    _verify_candidate_persistence_boundary(dependencies)
    return persisted


def _load_bound_candidate(candidate_id: str, root: Path) -> tuple[Path, dict[str, Any], str, dict[str, Any]]:
    target = _candidate_directory(root, candidate_id)
    try:
        reject_symlink_ancestors(target, include_self=True)
        info = os.lstat(target)
    except (ContractError, OSError) as exc:
        raise _fail("candidate does not exist as a real directory", exc)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CandidateError("candidate does not exist as a real directory")
    candidate_doc = _strict_load(target / "candidate.json", "candidate", root)
    candidate, candidate_digest = validate_candidate_document(candidate_doc)
    provenance_doc = _strict_load(target / "provenance.json", "candidate provenance", root)
    validated_provenance = validate_provenance(provenance_doc, candidate, candidate_digest)
    return target, candidate, candidate_digest, validated_provenance


def _validate_event(
    event: Mapping[str, Any],
    *,
    sequence: int,
    candidate_id: str,
    candidate_digest: str,
    previous_digest: str | None,
) -> dict[str, Any]:
    try:
        require_exact_keys(event, EVENT_KEYS, "review event")
        require_identifier(event["candidate_id"], "review event candidate_id")
        require_identifier(event["reviewer"], "review event reviewer")
    except ContractError as exc:
        raise _fail(str(exc), exc)
    if event["schema_version"] != "ao.lore.candidate-review-event.v0.1":
        raise CandidateError("unsupported review event version")
    if event["sequence"] != sequence:
        raise CandidateError("review event sequence mismatch")
    if event["candidate_id"] != candidate_id or event["candidate_digest"] != candidate_digest:
        raise CandidateError("review event candidate binding mismatch")
    if event["previous_event_digest"] != previous_digest:
        raise CandidateError("review event previous digest mismatch")
    if event["decision"] not in {"accept", "reject"}:
        raise CandidateError("review decision must be accept or reject")
    rationale = event["rationale"]
    if not isinstance(rationale, str) or len(rationale) > MAX_RATIONALE:
        raise CandidateError("review rationale is malformed")
    _timestamp(event["recorded_at"], "review recorded_at")
    supplied = _digest(event["event_digest"], "review event_digest")
    body = {key: value for key, value in event.items() if key != "event_digest"}
    actual = _canonical(body, "review event")
    if supplied != actual:
        raise CandidateError("review event digest mismatch")
    return json.loads(json.dumps(event))


def _inspect_bound_candidate(
    target: Path,
    candidate: Mapping[str, Any],
    candidate_digest: str,
    provenance: Mapping[str, Any],
    root: Path,
) -> dict[str, Any]:
    candidate_id = candidate["candidate_id"]
    reviews = target / "reviews"
    try:
        reject_symlink_ancestors(reviews, include_self=True)
        info = os.lstat(reviews)
    except (ContractError, OSError) as exc:
        raise _fail("candidate reviews path is invalid", exc)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CandidateError("candidate reviews path is invalid")
    entries = sorted(reviews.iterdir(), key=lambda path: path.name)
    previous: str | None = None
    latest_decision: str | None = None
    for sequence, path in enumerate(entries, start=1):
        match = EVENT_NAME_RE.fullmatch(path.name)
        if match is None or int(match.group("sequence")) != sequence:
            raise CandidateError("review event filenames must be contiguous and canonical")
        event = _strict_load(path, "review event", root)
        event = _validate_event(
            event,
            sequence=sequence,
            candidate_id=candidate_id,
            candidate_digest=candidate_digest,
            previous_digest=previous,
        )
        if match.group("digest") != event["event_digest"][7:]:
            raise CandidateError("review event filename digest mismatch")
        previous = event["event_digest"]
        latest_decision = event["decision"]
    review_status = {None: "unreviewed", "accept": "accepted", "reject": "rejected"}[latest_decision]
    return {
        "schema_version": "ao.lore.candidate-inspection.v0.1",
        "candidate_id": candidate_id,
        "candidate_digest": candidate_digest,
        "provenance_digest": _canonical(provenance, "candidate provenance"),
        "verified_review_events": len(entries),
        "review_status": review_status,
        "latest_event_digest": previous,
        "canonical": candidate["canonical"],
        "promotion_authority": candidate["promotion_authority"],
    }


def _inspect_candidate(
    candidate_id: str,
    *,
    candidate_root: Path | None = None,
    allowed_candidate_root: Path | None = None,
) -> dict[str, Any]:
    root = _candidate_root(candidate_root, allowed_root=allowed_candidate_root)
    target, candidate, candidate_digest, provenance = _load_bound_candidate(
        candidate_id, root
    )
    return _inspect_bound_candidate(
        target, candidate, candidate_digest, provenance, root
    )


def inspect_candidate(
    candidate_id: str,
    *,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    return _inspect_candidate(candidate_id, candidate_root=candidate_root)


def load_verified_candidate(
    candidate_id: str,
    *,
    candidate_root: Path | None = None,
) -> dict[str, Any]:
    """Return detached candidate data after verifying all persisted bindings."""

    return _load_verified_candidate(
        candidate_id, candidate_root=candidate_root,
    )


def _load_verified_candidate(
    candidate_id: str,
    *,
    candidate_root: Path | None = None,
    allowed_candidate_root: Path | None = None,
) -> dict[str, Any]:
    root = _candidate_root(candidate_root, allowed_root=allowed_candidate_root)
    target, candidate, candidate_digest, provenance = _load_bound_candidate(
        candidate_id, root
    )
    inspection = _inspect_bound_candidate(
        target, candidate, candidate_digest, provenance, root
    )
    return json.loads(json.dumps({
        "candidate": candidate,
        "provenance": provenance,
        "inspection": inspection,
    }))


def _load_verified_candidate_with_persistence_dependencies(
    candidate_id: str,
    *,
    dependencies: _CandidatePersistenceDependencies,
) -> dict[str, Any]:
    _verify_candidate_persistence_boundary(dependencies)
    return dependencies.store.load(candidate_id)


def _inspect_candidate_with_persistence_dependencies(
    candidate_id: str,
    *,
    dependencies: _CandidatePersistenceDependencies,
) -> dict[str, Any]:
    _verify_candidate_persistence_boundary(dependencies)
    return dependencies.store.inspect(candidate_id)


def load_verified_candidate_if_present(
    candidate_id: str,
    *,
    candidate_root: Path | None = None,
) -> dict[str, Any] | None:
    """Return a verified candidate, or ``None`` only for an absent target."""

    root = _candidate_root(candidate_root)
    target = _candidate_directory(root, candidate_id)
    try:
        info = os.lstat(target)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _fail("candidate state could not be inspected safely", exc)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CandidateError("candidate does not exist as a real directory")
    return load_verified_candidate(candidate_id, candidate_root=root)


def _append_review_under_global_lock(
    candidate_id: str,
    decision: str,
    reviewer: str,
    rationale: str = "",
    *,
    candidate_root: Path | None = None,
    allowed_candidate_root: Path | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    if decision not in {"accept", "reject"}:
        raise CandidateError("review decision must be accept or reject")
    try:
        reviewer = require_identifier(reviewer, "reviewer")
    except ContractError as exc:
        raise _fail(str(exc), exc)
    if not isinstance(rationale, str) or len(rationale) > MAX_RATIONALE:
        raise CandidateError("review rationale must be bounded text")
    timestamp = _timestamp(recorded_at or _now(), "recorded_at")
    root = _candidate_root(candidate_root, allowed_root=allowed_candidate_root)
    target, _, candidate_digest, _ = _load_bound_candidate(candidate_id, root)
    lock_path = target / ".review.lock"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileExistsError as exc:
        raise _fail("candidate review is already locked", exc)
    try:
        os.close(descriptor)
        current = _inspect_candidate(
            candidate_id,
            candidate_root=root,
            allowed_candidate_root=allowed_candidate_root,
        )
        sequence = current["verified_review_events"] + 1
        if sequence > 999999:
            raise CandidateError("candidate review sequence is exhausted")
        event = {
            "schema_version": "ao.lore.candidate-review-event.v0.1",
            "sequence": sequence,
            "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "previous_event_digest": current["latest_event_digest"],
            "decision": decision,
            "reviewer": reviewer,
            "rationale": rationale,
            "recorded_at": timestamp,
        }
        event["event_digest"] = _canonical(event, "review event")
        _validate_event(
            event,
            sequence=sequence,
            candidate_id=candidate_id,
            candidate_digest=candidate_digest,
            previous_digest=current["latest_event_digest"],
        )
        reviews = target / "reviews"
        path = reviews / f"{sequence:06d}-{event['event_digest'][7:]}.json"
        write_exclusive_json(path, event, root=root)
        _fsync_directory(reviews)
        return event
    except (ContractError, OSError) as exc:
        if isinstance(exc, CandidateError):
            raise
        raise _fail("candidate review failed closed", exc)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class _CandidateReviewDependencies:
    promotion_lock: Callable[[], AbstractContextManager[None]]
    candidate_root: Path | None = None


_PROOF_SUBTREES = {
    "candidate", "brain", "promotions", "workspace-registry",
    "workspace-documents",
}


def _candidate_review_dependencies_for_proof(
    proof_root: Path,
) -> _CandidateReviewDependencies:
    allowed_parent = repository_root() / ".ao-lore"
    try:
        selected, _ = ensure_contained(Path(proof_root), allowed_parent, "proof root")
        if selected == allowed_parent:
            raise ContractError("proof root must be disposable")
        reject_symlink_ancestors(selected, include_self=True)
        info = os.lstat(selected)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ContractError("proof root must be a real directory")
        names = os.listdir(selected)
        if set(names) != _PROOF_SUBTREES or len(names) != len(_PROOF_SUBTREES):
            raise ContractError("proof root layout differs")
        for name in _PROOF_SUBTREES:
            child = os.lstat(selected / name)
            if stat.S_ISLNK(child.st_mode) or not stat.S_ISDIR(child.st_mode):
                raise ContractError("proof root layout differs")
    except (ContractError, OSError) as exc:
        raise _fail("proof root must be a contained disposable directory", exc)
    from .promotion import _PromotionDependencies, _promotion_lock

    promotion_dependencies = _PromotionDependencies(
        selected / "candidate",
        selected / "brain",
        selected / "promotions",
    )
    return _CandidateReviewDependencies(
        lambda: _promotion_lock(promotion_dependencies),
        selected / "candidate",
    )


def _persist_candidate_with_dependencies(
    result: Mapping[str, Any],
    provenance: Mapping[str, Any],
    *,
    dependencies: _CandidateReviewDependencies,
) -> dict[str, Any]:
    if (
        type(dependencies) is not _CandidateReviewDependencies
        or dependencies.candidate_root is None
    ):
        raise CandidateError("candidate review dependencies are invalid")
    return _persist_candidate(
        result,
        provenance,
        candidate_root=dependencies.candidate_root,
        allowed_candidate_root=dependencies.candidate_root,
    )


def _default_candidate_review_dependencies() -> _CandidateReviewDependencies:
    from .promotion import promotion_lock_for_review

    return _CandidateReviewDependencies(promotion_lock_for_review)


def _append_review_with_dependencies(
    candidate_id: str,
    decision: str,
    reviewer: str,
    rationale: str = "",
    *,
    candidate_root: Path | None = None,
    recorded_at: str | None = None,
    dependencies: _CandidateReviewDependencies,
) -> dict[str, Any]:
    """Private review entry point for an exact injected promotion lock."""

    if type(dependencies) is not _CandidateReviewDependencies:
        raise CandidateError("candidate review dependencies are invalid")
    selected_root = (
        dependencies.candidate_root
        if candidate_root is None and dependencies.candidate_root is not None
        else candidate_root
    )
    with dependencies.promotion_lock():
        return _append_review_under_global_lock(
            candidate_id,
            decision,
            reviewer,
            rationale,
            candidate_root=selected_root,
            allowed_candidate_root=dependencies.candidate_root,
            recorded_at=recorded_at,
        )


def append_review(
    candidate_id: str,
    decision: str,
    reviewer: str,
    rationale: str = "",
    *,
    candidate_root: Path | None = None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    """Append a review while holding global promotion then candidate locks."""

    return _append_review_with_dependencies(
        candidate_id,
        decision,
        reviewer,
        rationale,
        candidate_root=candidate_root,
        recorded_at=recorded_at,
        dependencies=_default_candidate_review_dependencies(),
    )
