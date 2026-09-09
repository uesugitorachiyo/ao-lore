"""Private read-only intake and automatic claim verification for candidate quality review."""

from __future__ import annotations

import copy
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import unicodedata
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._strict_io import (
    ContractError,
    ensure_contained,
    parse_strict_json,
    reject_symlink_ancestors,
    require_exact_keys,
    require_identifier,
    require_text,
)
from .benchmark import BenchmarkError, canonical_digest
from .candidates import validate_candidate_document, validate_provenance
from .candidate_quality_contracts import (
    ANNOTATION_LABELS,
    AUTHORITY_FIELDS,
    QUESTION_CATEGORIES,
    QUESTION_OUTCOMES,
    SELECTION_PRIORITY,
    validate_candidate_quality_annotations,
    validate_candidate_quality_campaign,
    validate_candidate_quality_question_results,
    validate_candidate_quality_questions,
    validate_candidate_quality_recovery,
    validate_candidate_quality_result,
    validate_candidate_quality_sample,
    validate_candidate_quality_sampling_policy,
    validate_candidate_quality_summary,
)
from .home import repository_root


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_PDF_BYTES = 64 * 1024 * 1024
_EXPECTED_CANDIDATE_COUNT = 6
_EXPECTED_CANDIDATE_IDS = tuple(f"candidate-{index:02d}" for index in range(1, _EXPECTED_CANDIDATE_COUNT + 1))
_EXPECTED_CLAIM_COUNTS = {
    "candidate-01": 19,
    "candidate-02": 13,
    "candidate-03": 73,
    "candidate-04": 21,
    "candidate-05": 71,
    "candidate-06": 289,
}
_EXPECTED_SAMPLE_ALLOCATIONS = {
    "candidate-01": 19,
    "candidate-02": 13,
    "candidate-03": 14,
    "candidate-04": 12,
    "candidate-05": 14,
    "candidate-06": 24,
}
_TOTAL_SAMPLED_CLAIMS = 96
_PDF_RELPATH_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\.pdf$")
_EVENT_NAME_RE = re.compile(r"^(?P<sequence>[0-9]{6})-(?P<digest>[0-9a-f]{64})\.json$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$"
)
_PUNCTUATION_ENDINGS = tuple(".!?:;)]}\"'")
_CANONICAL_EVIDENCE_QUERY_RE = re.compile(r"^(?!.*[A-Z])[^\W_]+(?: [^\W_]+)*$")


@dataclass(frozen=True)
class _CampaignDependencies:
    repository_root: Path
    candidate_root: Path
    source_root: Path
    terminal_readback_path: Path


@dataclass(frozen=True)
class _HeldDirectory:
    path: Path
    root: Path
    descriptor: int
    identity: tuple[int, int]
    label: str


def _fail(message: str, exc: BaseException | None = None) -> ContractError:
    error = ContractError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _authority_flags() -> dict[str, bool]:
    return {field: False for field in AUTHORITY_FIELDS}


def _sha256_bytes(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ContractError(f"{label} must be a lowercase sha256 digest")
    return value


def _canonical(value: Any, label: str) -> str:
    try:
        return canonical_digest(value)
    except BenchmarkError as exc:
        raise _fail(f"{label} must contain strict JSON data", exc)


def _self_digest(value: dict[str, Any], field: str, label: str) -> dict[str, Any]:
    result = _deep_copy(value)
    result[field] = _canonical({key: item for key, item in result.items() if key != field}, label)
    return result


def _regular_file_identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise OSError("unsafe regular file")
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _anchored_parts(path: Path, root: Path, label: str) -> tuple[Path, Path, tuple[str, ...]]:
    selected, absolute_root = ensure_contained(Path(path), Path(root), label)
    relative = selected.relative_to(absolute_root)
    return selected, absolute_root, relative.parts


def _open_anchored_directory(path: Path, root: Path, label: str) -> int:
    selected, absolute_root, parts = _anchored_parts(path, root, label)
    current = os.open(absolute_root, _DIRECTORY_FLAGS)
    try:
        info = os.fstat(current)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("not a directory")
        for component in parts:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        opened = os.fstat(current)
        if not stat.S_ISDIR(opened.st_mode):
            raise OSError("not a directory")
        return current
    except BaseException:
        try:
            os.close(current)
        except OSError:
            pass
        raise


def _hold_directory(path: Path, root: Path, label: str) -> _HeldDirectory:
    descriptor: int | None = None
    try:
        selected, absolute_root, _ = _anchored_parts(path, root, label)
        descriptor = _open_anchored_directory(selected, absolute_root, label)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise OSError("not a directory")
        held = _HeldDirectory(
            path=selected,
            root=absolute_root,
            descriptor=descriptor,
            identity=(info.st_dev, info.st_ino),
            label=label,
        )
        descriptor = None
        return held
    except (ContractError, OSError) as exc:
        raise _fail(f"{label} must be a contained real directory", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _assert_held_directory(held: _HeldDirectory) -> None:
    try:
        info = os.fstat(held.descriptor)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != held.identity:
            raise OSError("held directory changed")
        rebound = _open_anchored_directory(held.path, held.root, held.label)
        try:
            public = os.fstat(rebound)
        finally:
            os.close(rebound)
        if not stat.S_ISDIR(public.st_mode) or (public.st_dev, public.st_ino) != held.identity:
            raise OSError("directory binding changed")
    except OSError as exc:
        raise _fail(f"{held.label} binding changed", exc)


def _list_directory(held: _HeldDirectory) -> list[str]:
    _assert_held_directory(held)
    try:
        entries = sorted(os.listdir(held.descriptor))
    except OSError as exc:
        raise _fail(f"{held.label} could not be listed", exc)
    _assert_held_directory(held)
    return entries


def _read_bytes_at(held: _HeldDirectory, name: str, label: str, *, maximum: int) -> bytes:
    _assert_held_directory(held)
    try:
        before = os.stat(name, dir_fd=held.descriptor, follow_symlinks=False)
        before_identity = _regular_file_identity(before)
        if before.st_size > maximum:
            raise OSError("file too large")
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=held.descriptor)
        try:
            opened = os.fstat(descriptor)
            if _regular_file_identity(opened) != before_identity:
                raise OSError("file changed")
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            body = b"".join(chunks)
            after = os.fstat(descriptor)
            public = os.stat(name, dir_fd=held.descriptor, follow_symlinks=False)
            if (
                len(body) > maximum
                or _regular_file_identity(after) != before_identity
                or _regular_file_identity(public) != before_identity
            ):
                raise OSError("file changed")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise _fail(f"{label} could not be loaded", exc)
    _assert_held_directory(held)
    return body


def _read_json_at(held: _HeldDirectory, name: str, label: str) -> tuple[dict[str, Any], bytes]:
    body = _read_bytes_at(held, name, label, maximum=_MAX_JSON_BYTES)
    try:
        return parse_strict_json(body, label), body
    except ContractError as exc:
        raise _fail(f"{label} could not be loaded", exc)


def _approved_campaign_dependencies() -> _CampaignDependencies:
    root = repository_root()
    return _CampaignDependencies(
        repository_root=root,
        candidate_root=root / "working" / "candidates",
        source_root=root / "sources",
        terminal_readback_path=root / ".ao-lore" / "public-candidate-quality-review-20260812" / "terminal-readback.json",
    )


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if type(manifest) is not dict:
        raise ContractError("campaign manifest must be an exact object")
    require_exact_keys(
        manifest,
        ("campaign_id", "correlation_id", "terminal_readback_digest", "candidates"),
        "campaign manifest",
    )
    candidates = manifest["candidates"]
    if type(candidates) is not list or len(candidates) != _EXPECTED_CANDIDATE_COUNT:
        raise ContractError("campaign manifest candidates differ")
    normalized = []
    candidate_ids: list[str] = []
    source_relpaths: list[str] = []
    for raw in candidates:
        if type(raw) is not dict:
            raise ContractError("campaign manifest candidate must be an exact object")
        require_exact_keys(
            raw,
            (
                "candidate_id",
                "candidate_digest",
                "provenance_digest",
                "source_relpath",
                "source_digest",
                "claim_count",
                "review_status",
                "latest_event_digest",
            ),
            "campaign manifest candidate",
        )
        candidate_id = require_identifier(raw["candidate_id"], "candidate_id")
        source_relpath = raw["source_relpath"]
        if type(source_relpath) is not str or _PDF_RELPATH_RE.fullmatch(source_relpath) is None:
            raise ContractError("source_relpath must name a bounded relative pdf")
        claim_count = raw["claim_count"]
        if type(claim_count) is not int or isinstance(claim_count, bool) or claim_count <= 0:
            raise ContractError("claim_count differs")
        if raw["review_status"] != "unreviewed":
            raise ContractError("campaign manifest candidates must remain unreviewed")
        if raw["latest_event_digest"] is not None:
            raise ContractError("campaign manifest review head must remain empty")
        candidate_ids.append(candidate_id)
        source_relpaths.append(source_relpath)
        normalized.append(
            {
                "candidate_id": candidate_id,
                "candidate_digest": _digest(raw["candidate_digest"], "candidate_digest"),
                "provenance_digest": _digest(raw["provenance_digest"], "provenance_digest"),
                "source_relpath": source_relpath,
                "source_digest": _digest(raw["source_digest"], "source_digest"),
                "claim_count": claim_count,
                "review_status": "unreviewed",
                "latest_event_digest": None,
            }
        )
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ContractError("campaign manifest candidate identities must be unique")
    if len(source_relpaths) != len(set(source_relpaths)):
        raise ContractError("campaign manifest source relpaths must be unique")
    return {
        "campaign_id": require_identifier(manifest["campaign_id"], "campaign_id"),
        "correlation_id": require_identifier(manifest["correlation_id"], "correlation_id"),
        "terminal_readback_digest": _digest(manifest["terminal_readback_digest"], "terminal_readback_digest"),
        "candidates": normalized,
    }


def _validate_dependencies(dependencies: _CampaignDependencies) -> _CampaignDependencies:
    if type(dependencies) is not _CampaignDependencies:
        raise ContractError("campaign dependencies must be exact")
    repo_root = Path(dependencies.repository_root)
    candidate_root = Path(dependencies.candidate_root)
    source_root = Path(dependencies.source_root)
    terminal_readback_path = Path(dependencies.terminal_readback_path)
    ensure_contained(candidate_root, repo_root, "candidate root")
    ensure_contained(source_root, repo_root, "source root")
    ensure_contained(terminal_readback_path, repo_root, "terminal readback")
    return _CampaignDependencies(
        repository_root=repo_root,
        candidate_root=candidate_root,
        source_root=source_root,
        terminal_readback_path=terminal_readback_path,
    )


def _validate_terminal_readback(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError("terminal readback must be an exact object")
    require_exact_keys(
        value,
        ("schema_version", "correlation_id", "status", "authority_advanced"),
        "terminal readback",
    )
    if type(value["authority_advanced"]) is not bool or value["authority_advanced"] is not False:
        raise ContractError("terminal readback authority widened")
    return {
        "schema_version": require_text(value["schema_version"], "terminal readback schema_version", maximum=128),
        "correlation_id": require_identifier(value["correlation_id"], "terminal readback correlation_id"),
        "status": require_text(value["status"], "terminal readback status", maximum=64),
        "authority_advanced": False,
    }


def _validate_review_event(
    event: Any,
    *,
    sequence: int,
    candidate_id: str,
    candidate_digest: str,
    previous_digest: str | None,
) -> dict[str, Any]:
    if type(event) is not dict:
        raise ContractError("review event must be an exact object")
    require_exact_keys(
        event,
        (
            "schema_version",
            "sequence",
            "candidate_id",
            "candidate_digest",
            "previous_event_digest",
            "decision",
            "reviewer",
            "rationale",
            "recorded_at",
            "event_digest",
        ),
        "review event",
    )
    if event["schema_version"] != "ao.lore.candidate-review-event.v0.1":
        raise ContractError("review event schema version differs")
    if event["sequence"] != sequence:
        raise ContractError("review event sequence differs")
    if require_identifier(event["candidate_id"], "review event candidate_id") != candidate_id:
        raise ContractError("review event candidate binding differs")
    if _digest(event["candidate_digest"], "review event candidate_digest") != candidate_digest:
        raise ContractError("review event candidate binding differs")
    if event["previous_event_digest"] != previous_digest:
        raise ContractError("review event previous digest differs")
    if event["decision"] not in {"accept", "reject"}:
        raise ContractError("review event decision differs")
    require_identifier(event["reviewer"], "reviewer")
    if type(event["rationale"]) is not str or len(event["rationale"]) > 1024:
        raise ContractError("review event rationale differs")
    if type(event["recorded_at"]) is not str or not event["recorded_at"].endswith("Z"):
        raise ContractError("review event recorded_at differs")
    actual = _canonical(
        {key: value for key, value in event.items() if key != "event_digest"},
        "review event",
    )
    if _digest(event["event_digest"], "review event digest") != actual:
        raise ContractError("review event digest differs")
    return _deep_copy(event)


def _inspect_reviews(held: _HeldDirectory, candidate_id: str, candidate_digest: str) -> dict[str, Any]:
    entries = _list_directory(held)
    previous: str | None = None
    latest_decision: str | None = None
    for sequence, name in enumerate(entries, start=1):
        match = _EVENT_NAME_RE.fullmatch(name)
        if match is None or int(match.group("sequence")) != sequence:
            raise ContractError("review event filenames must be contiguous and canonical")
        event, _ = _read_json_at(held, name, "review event")
        validated = _validate_review_event(
            event,
            sequence=sequence,
            candidate_id=candidate_id,
            candidate_digest=candidate_digest,
            previous_digest=previous,
        )
        if match.group("digest") != validated["event_digest"][7:]:
            raise ContractError("review event filename digest differs")
        previous = validated["event_digest"]
        latest_decision = validated["decision"]
    review_status = {None: "unreviewed", "accept": "accepted", "reject": "rejected"}[latest_decision]
    return {
        "schema_version": "ao.lore.candidate-inspection.v0.1",
        "candidate_id": candidate_id,
        "candidate_digest": candidate_digest,
        "verified_review_events": len(entries),
        "review_status": review_status,
        "latest_event_digest": previous,
    }


def _verify_campaign_inputs(manifest: Any, *, dependencies: _CampaignDependencies) -> dict[str, Any]:
    manifest_value = _validate_manifest(manifest)
    deps = _validate_dependencies(dependencies)
    with ExitStack() as stack:
        candidate_root = _hold_directory(deps.candidate_root, deps.repository_root, "candidate root")
        stack.callback(os.close, candidate_root.descriptor)
        source_root = _hold_directory(deps.source_root, deps.repository_root, "source root")
        stack.callback(os.close, source_root.descriptor)
        terminal_parent = _hold_directory(
            deps.terminal_readback_path.parent,
            deps.repository_root,
            "terminal readback parent",
        )
        stack.callback(os.close, terminal_parent.descriptor)
        expected_candidate_ids = [item["candidate_id"] for item in manifest_value["candidates"]]
        expected_sources = [item["source_relpath"] for item in manifest_value["candidates"]]
        if _list_directory(candidate_root) != expected_candidate_ids:
            raise ContractError("candidate root entries differ")
        if _list_directory(source_root) != expected_sources:
            raise ContractError("source root entries differ")

        candidates = []
        for item in manifest_value["candidates"]:
            candidate_dir = _hold_directory(
                candidate_root.path / item["candidate_id"],
                deps.repository_root,
                "candidate directory",
            )
            try:
                if _list_directory(candidate_dir) != ["candidate.json", "provenance.json", "reviews"]:
                    raise ContractError("candidate directory entries differ")
                candidate_value, _ = _read_json_at(candidate_dir, "candidate.json", "candidate")
                candidate, candidate_digest = validate_candidate_document(candidate_value)
                provenance_value, _ = _read_json_at(
                    candidate_dir, "provenance.json", "candidate provenance"
                )
                provenance = validate_provenance(provenance_value, candidate, candidate_digest)
                reviews = _hold_directory(
                    candidate_dir.path / "reviews",
                    deps.repository_root,
                    "candidate reviews",
                )
                try:
                    inspection = _inspect_reviews(
                        reviews,
                        candidate["candidate_id"],
                        candidate_digest,
                    )
                finally:
                    os.close(reviews.descriptor)
            finally:
                os.close(candidate_dir.descriptor)

            if candidate["candidate_id"] != item["candidate_id"]:
                raise ContractError("candidate identity drift detected")
            if candidate_digest != item["candidate_digest"]:
                raise ContractError("candidate digest drift detected")
            provenance_digest = _canonical(provenance, "candidate provenance")
            if provenance_digest != item["provenance_digest"]:
                raise ContractError("candidate provenance drift detected")
            if provenance["source_digest"] != item["source_digest"]:
                raise ContractError("candidate source digest drift detected")
            if len(candidate["claims"]) != item["claim_count"]:
                raise ContractError("candidate claim count drift detected")
            if candidate["knowledge_policy"]["sensitivity"] != "public":
                raise ContractError("candidate policy must remain public")
            if candidate["canonical"] is not False or candidate["promotion_authority"] is not False:
                raise ContractError("candidate authority widened")
            if inspection["verified_review_events"] != 0:
                raise ContractError("candidate reviews must remain empty")
            if inspection["review_status"] != "unreviewed" or inspection["latest_event_digest"] is not None:
                raise ContractError("candidate review head drift detected")

            source_bytes = _read_bytes_at(
                source_root,
                item["source_relpath"],
                "source pdf",
                maximum=_MAX_PDF_BYTES,
            )
            if not source_bytes.startswith(b"%PDF-"):
                raise ContractError("source pdf signature differs")
            if _sha256_bytes(source_bytes) != item["source_digest"]:
                raise ContractError("candidate source drift detected")

            candidates.append(
                {
                    "candidate_id": item["candidate_id"],
                    "candidate_digest": candidate_digest,
                    "provenance_digest": provenance_digest,
                    "source_relpath": item["source_relpath"],
                    "source_digest": item["source_digest"],
                    "claim_count": item["claim_count"],
                    "review_status": "unreviewed",
                    "latest_event_digest": None,
                    "candidate": _deep_copy(candidate),
                    "provenance": _deep_copy(provenance),
                    "inspection": {
                        **inspection,
                        "provenance_digest": provenance_digest,
                        "canonical": False,
                        "promotion_authority": False,
                    },
                    "source_bytes": source_bytes,
                }
            )

        terminal_name = deps.terminal_readback_path.name
        readback_value, readback_body = _read_json_at(
            terminal_parent,
            terminal_name,
            "terminal readback",
        )
        if _sha256_bytes(readback_body) != manifest_value["terminal_readback_digest"]:
            raise ContractError("terminal readback drift detected")
        terminal_readback = _validate_terminal_readback(readback_value)
        if terminal_readback["correlation_id"] != manifest_value["correlation_id"]:
            raise ContractError("terminal readback correlation drift detected")
        return {
            "campaign_id": manifest_value["campaign_id"],
            "correlation_id": manifest_value["correlation_id"],
            "terminal_readback_digest": manifest_value["terminal_readback_digest"],
            "terminal_readback": terminal_readback,
            "candidate_count": len(candidates),
            "total_claim_count": sum(item["claim_count"] for item in candidates),
            "candidates": candidates,
        }


def verify_campaign_inputs(manifest: Any) -> dict[str, Any]:
    return _verify_campaign_inputs(manifest, dependencies=_approved_campaign_dependencies())


def _normalize_text(text: str) -> str:
    collapsed = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    return re.sub(r"\s+", " ", collapsed)


def _block_type_from_record(record: dict[str, Any]) -> str:
    block_type = record.get("block_type", "unknown")
    if type(block_type) is not str or not block_type:
        raise ContractError("claim record block_type differs")
    return block_type


def _binding_risk_suspect(record: dict[str, Any]) -> bool:
    explicit = record.get("binding_risk_suspect")
    if explicit is not None:
        if type(explicit) is not bool:
            raise ContractError("claim record binding_risk_suspect differs")
        return explicit
    source_block_ids = record.get("source_block_ids")
    if type(source_block_ids) is list:
        return len(source_block_ids) != 1
    return False


def _fragmentation_suspect(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    if stripped[-1] not in _PUNCTUATION_ENDINGS:
        return True
    first = stripped[0]
    return first.isalpha() and not first.isupper()


def _validate_candidate_claim_surface(entry: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    require_exact_keys(
        entry,
        (
            "candidate_id",
            "candidate_digest",
            "provenance_digest",
            "source_relpath",
            "source_digest",
            "claim_count",
            "review_status",
            "latest_event_digest",
            "candidate",
            "provenance",
            "inspection",
            "source_bytes",
        ),
        "verified campaign candidate",
    )
    candidate_id = require_identifier(entry["candidate_id"], "candidate_id")
    expected_candidate_digest = _digest(entry["candidate_digest"], "candidate_digest")
    expected_provenance_digest = _digest(entry["provenance_digest"], "provenance_digest")
    _digest(entry["source_digest"], "source_digest")
    if type(entry["claim_count"]) is not int or isinstance(entry["claim_count"], bool) or entry["claim_count"] <= 0:
        raise ContractError("claim count differs")
    if entry["review_status"] != "unreviewed" or entry["latest_event_digest"] is not None:
        raise ContractError("review status differs")
    if type(entry["source_bytes"]) is not bytes or not entry["source_bytes"].startswith(b"%PDF-"):
        raise ContractError("source pdf differs")
    if _sha256_bytes(entry["source_bytes"]) != entry["source_digest"]:
        raise ContractError("source digest binding differs")

    candidate = _deep_copy(entry["candidate"])
    candidate, actual_candidate_digest = validate_candidate_document(candidate)
    if actual_candidate_digest != expected_candidate_digest:
        raise ContractError("candidate digest binding differs")
    if candidate["candidate_id"] != candidate_id:
        raise ContractError("candidate binding differs")

    provenance = _deep_copy(entry["provenance"])
    provenance = validate_provenance(provenance, candidate, actual_candidate_digest)
    actual_provenance_digest = _canonical(provenance, "candidate provenance")
    if actual_provenance_digest != expected_provenance_digest:
        raise ContractError("provenance digest binding differs")
    if provenance["source_digest"] != entry["source_digest"]:
        raise ContractError("source digest binding differs")

    return candidate_id, candidate, provenance


def verify_all_claims(intake: Any) -> dict[str, Any]:
    if type(intake) is not dict:
        raise ContractError("verified campaign inputs must be an exact object")
    require_exact_keys(
        intake,
        (
            "campaign_id",
            "correlation_id",
            "terminal_readback_digest",
            "terminal_readback",
            "candidate_count",
            "total_claim_count",
            "candidates",
        ),
        "verified campaign inputs",
    )
    candidates = intake["candidates"]
    if type(candidates) is not list or len(candidates) != _EXPECTED_CANDIDATE_COUNT:
        raise ContractError("verified campaign candidates differ")
    candidate_ids = [candidate.get("candidate_id") for candidate in candidates if type(candidate) is dict]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ContractError("verified campaign candidate identities must be unique")

    candidate_results = []
    total_claims = 0
    total_citations = 0
    for entry in candidates:
        candidate_id, candidate, provenance = _validate_candidate_claim_surface(entry)
        claims = candidate["claims"]
        citations = candidate["citations"]
        mappings = candidate["claim_mappings"]
        if not (
            type(claims) is list
            and type(citations) is list
            and type(mappings) is list
            and len(claims) == len(citations) == len(mappings) == entry["claim_count"]
        ):
            raise ContractError("candidate knowledge counts differ")

        citation_ids: set[str] = set()
        citation_index: dict[str, dict[str, Any]] = {}
        for citation in citations:
            if type(citation) is not dict:
                raise ContractError("citation must be an exact object")
            require_exact_keys(citation, ("citation_id", "render_text", "source_block_ids"), "citation")
            citation_id = require_identifier(citation["citation_id"], "citation_id")
            if citation_id in citation_ids:
                raise ContractError("citation identities must be unique")
            citation_ids.add(citation_id)
            citation_index[citation_id] = citation

        mapping_ids: set[str] = set()
        mapping_index: dict[str, list[str]] = {}
        for mapping in mappings:
            if type(mapping) is not dict:
                raise ContractError("claim mapping must be an exact object")
            require_exact_keys(mapping, ("claim_id", "block_ids"), "claim mapping")
            claim_id = require_identifier(mapping["claim_id"], "mapping claim_id")
            if claim_id in mapping_ids:
                raise ContractError("claim mapping identities must be unique")
            mapping_ids.add(claim_id)
            block_ids = mapping["block_ids"]
            if type(block_ids) is not list or not block_ids:
                raise ContractError("claim mapping evidence differs")
            normalized_blocks = [require_identifier(block_id, "block_id") for block_id in block_ids]
            if len(normalized_blocks) != len(set(normalized_blocks)):
                raise ContractError("claim mapping evidence differs")
            mapping_index[claim_id] = normalized_blocks

        claim_ids: set[str] = set()
        records = []
        for ordinal, claim in enumerate(claims, start=1):
            if type(claim) is not dict:
                raise ContractError("claim must be an exact object")
            require_exact_keys(claim, ("claim_id", "text", "source_block_ids", "citation_id"), "claim")
            claim_id = require_identifier(claim["claim_id"], "claim_id")
            if claim_id in claim_ids:
                raise ContractError("claim identities must be unique")
            claim_ids.add(claim_id)
            claim_text = require_text(claim["text"], "claim text", maximum=4096)
            claim_blocks = claim["source_block_ids"]
            if type(claim_blocks) is not list or not claim_blocks:
                raise ContractError("claim evidence differs")
            claim_block_ids = [require_identifier(block_id, "block_id") for block_id in claim_blocks]
            if len(claim_block_ids) != len(set(claim_block_ids)):
                raise ContractError("claim evidence differs")
            citation_id = require_identifier(claim["citation_id"], "citation_id")
            citation = citation_index.get(citation_id)
            if citation is None:
                raise ContractError("claim citation binding differs")
            citation_text = require_text(citation["render_text"], "citation text", maximum=4096)
            citation_block_ids = [require_identifier(block_id, "block_id") for block_id in citation["source_block_ids"]]
            if claim_block_ids != citation_block_ids:
                raise ContractError("claim citation evidence differs")
            if mapping_index.get(claim_id) != claim_block_ids:
                raise ContractError("claim mapping evidence differs")
            if claim_text != citation_text:
                raise ContractError("claim text must remain exact")
            record = {
                "candidate_id": candidate_id,
                "claim_ordinal": ordinal,
                "claim_id": claim_id,
                "citation_id": citation_id,
                "source_block_ids": list(claim_block_ids),
                "claim_text": claim_text,
                "citation_text": citation_text,
            }
            record["claim_record_digest"] = _canonical(record, "claim record")
            records.append(record)

        exact_counts: dict[str, int] = {}
        normalized_counts: dict[str, int] = {}
        for record in records:
            exact_counts[record["claim_text"]] = exact_counts.get(record["claim_text"], 0) + 1
            normalized = _normalize_text(record["claim_text"])
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1

        duplicate_count = 0
        normalized_duplicate_count = 0
        fragmentation_count = 0
        for record in records:
            duplicate = exact_counts[record["claim_text"]] > 1
            normalized_duplicate = normalized_counts[_normalize_text(record["claim_text"])] > 1
            fragmented = _fragmentation_suspect(record["claim_text"])
            record["duplicate_suspect"] = duplicate
            record["normalized_duplicate_suspect"] = normalized_duplicate
            record["fragmentation_suspect"] = fragmented
            record["binding_risk_suspect"] = len(record["source_block_ids"]) != 1
            record["block_type"] = "unknown"
            if duplicate:
                duplicate_count += 1
            if normalized_duplicate:
                normalized_duplicate_count += 1
            if fragmented:
                fragmentation_count += 1

        total_claims += len(records)
        total_citations += len(citations)
        candidate_results.append(
            _verified_candidate_result(
                {
                    "candidate_id": candidate_id,
                    "candidate_digest": entry["candidate_digest"],
                    "provenance_digest": entry["provenance_digest"],
                    "source_digest": provenance["source_digest"],
                    "claim_count": len(records),
                    "citation_count": len(citations),
                    "claim_records": records,
                    "automatic_check_counts": {
                        "verified_claim_count": len(records),
                        "verified_citation_count": len(citations),
                        "duplicate_suspect_count": duplicate_count,
                        "normalized_duplicate_suspect_count": normalized_duplicate_count,
                        "fragmentation_suspect_count": fragmentation_count,
                        "binding_error_count": 0,
                    },
                }
            )
        )

    return {
        "campaign_id": require_identifier(intake["campaign_id"], "campaign_id"),
        "correlation_id": require_identifier(intake["correlation_id"], "correlation_id"),
        "terminal_readback_digest": _digest(intake["terminal_readback_digest"], "terminal_readback_digest"),
        "total_verified_claim_count": total_claims,
        "total_verified_citation_count": total_citations,
        "candidate_results": candidate_results,
    }


def _verified_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{label} differs")
    return value


def _validated_claim_record(record: Any, candidate_id: str) -> dict[str, Any]:
    if type(record) is not dict:
        raise ContractError("claim record must be an exact object")
    source_block_ids = record.get("source_block_ids")
    if type(source_block_ids) is not list or not source_block_ids:
        raise ContractError("claim record source_block_ids differ")
    normalized_source_block_ids = [require_identifier(block_id, "block_id") for block_id in source_block_ids]
    if len(normalized_source_block_ids) != len(set(normalized_source_block_ids)):
        raise ContractError("claim record source_block_ids differ")
    normalized = {
        "candidate_id": require_identifier(record.get("candidate_id"), "candidate_id"),
        "claim_ordinal": record.get("claim_ordinal"),
        "claim_id": require_identifier(record.get("claim_id"), "claim_id"),
        "citation_id": require_identifier(record.get("citation_id"), "citation_id"),
        "claim_record_digest": _digest(record.get("claim_record_digest"), "claim_record_digest"),
        "claim_text": require_text(record.get("claim_text"), "claim_text", maximum=4096),
        "citation_text": require_text(record.get("citation_text"), "citation_text", maximum=4096),
        "source_block_ids": normalized_source_block_ids,
        "duplicate_suspect": _verified_bool(record.get("duplicate_suspect", False), "duplicate_suspect"),
        "normalized_duplicate_suspect": _verified_bool(
            record.get("normalized_duplicate_suspect", False),
            "normalized_duplicate_suspect",
        ),
        "fragmentation_suspect": _verified_bool(record.get("fragmentation_suspect", False), "fragmentation_suspect"),
        "binding_risk_suspect": _binding_risk_suspect(record),
        "block_type": _block_type_from_record(record),
    }
    if type(normalized["claim_ordinal"]) is not int or normalized["claim_ordinal"] <= 0:
        raise ContractError("claim record claim_ordinal differs")
    if normalized["candidate_id"] != candidate_id:
        raise ContractError("claim record candidate binding differs")
    if normalized["claim_text"] != normalized["citation_text"]:
        raise ContractError("claim record citation text binding differs")
    expected_digest = _canonical(
        {
            "candidate_id": candidate_id,
            "claim_ordinal": normalized["claim_ordinal"],
            "claim_id": normalized["claim_id"],
            "citation_id": normalized["citation_id"],
            "source_block_ids": normalized["source_block_ids"],
            "claim_text": normalized["claim_text"],
            "citation_text": normalized["citation_text"],
        },
        "claim record",
    )
    if normalized["claim_record_digest"] != expected_digest:
        raise ContractError("claim record digest differs")
    return normalized


def _verified_candidate_result(value: dict[str, Any]) -> dict[str, Any]:
    return _self_digest(value, "verified_result_digest", "verified candidate result")


def _sorted_unique_by_digest(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result = []
    for record in sorted(records, key=lambda item: (item["claim_record_digest"], item["claim_id"])):
        if record["claim_id"] in seen:
            raise ContractError("claim identities must be unique within each candidate")
        seen.add(record["claim_id"])
        result.append(record)
    return result


def _validated_candidate_result(entry: Any) -> dict[str, Any]:
    if type(entry) is not dict:
        raise ContractError("candidate result must be an exact object")
    candidate_id = require_identifier(entry.get("candidate_id"), "candidate_id")
    if candidate_id not in _EXPECTED_CLAIM_COUNTS:
        raise ContractError("candidate result candidate differs")
    claim_count = entry.get("claim_count")
    citation_count = entry.get("citation_count")
    if type(claim_count) is not int or claim_count != _EXPECTED_CLAIM_COUNTS[candidate_id]:
        raise ContractError("candidate result claim count differs")
    if type(citation_count) is not int or citation_count != claim_count:
        raise ContractError("candidate result citation count differs")
    claim_records = entry.get("claim_records")
    if type(claim_records) is not list or len(claim_records) != claim_count:
        raise ContractError("candidate result claim records differ")
    normalized_records = [_validated_claim_record(record, candidate_id) for record in claim_records]
    by_ordinal = sorted(normalized_records, key=lambda item: item["claim_ordinal"])
    if [record["claim_ordinal"] for record in by_ordinal] != list(range(1, claim_count + 1)):
        raise ContractError("candidate result claim ordinals differ")
    if len({record["claim_id"] for record in normalized_records}) != len(normalized_records):
        raise ContractError("claim identities must be unique within each candidate")
    automatic_counts = entry.get("automatic_check_counts")
    if type(automatic_counts) is not dict:
        raise ContractError("automatic check counts differ")
    result = {
        "candidate_id": candidate_id,
        "candidate_digest": _digest(entry.get("candidate_digest"), "candidate_digest"),
        "provenance_digest": _digest(entry.get("provenance_digest"), "provenance_digest"),
        "source_digest": _digest(entry.get("source_digest"), "source_digest"),
        "claim_count": claim_count,
        "citation_count": citation_count,
        "claim_records": by_ordinal,
        "automatic_check_counts": _deep_copy(automatic_counts),
        "verified_result_digest": _digest(entry.get("verified_result_digest"), "verified_result_digest"),
    }
    expected_digest = _canonical(
        {key: value for key, value in result.items() if key != "verified_result_digest"},
        "verified candidate result",
    )
    if result["verified_result_digest"] != expected_digest:
        raise ContractError("verified candidate result digest differs")
    return result


def _validated_verified_claims(verified_claims: Any) -> dict[str, Any]:
    if type(verified_claims) is not dict:
        raise ContractError("verified claim set must be an exact object")
    candidate_results = verified_claims.get("candidate_results")
    if type(candidate_results) is not list or len(candidate_results) != _EXPECTED_CANDIDATE_COUNT:
        raise ContractError("verified claim set candidates differ")
    normalized = [_validated_candidate_result(entry) for entry in candidate_results]
    candidate_index = {entry["candidate_id"]: entry for entry in normalized}
    if set(candidate_index) != set(_EXPECTED_CANDIDATE_IDS):
        raise ContractError("verified claim set candidates differ")
    ordered = [candidate_index[candidate_id] for candidate_id in _EXPECTED_CANDIDATE_IDS]
    total_verified_claim_count = verified_claims.get("total_verified_claim_count")
    total_verified_citation_count = verified_claims.get("total_verified_citation_count")
    if type(total_verified_claim_count) is not int or total_verified_claim_count != sum(
        entry["claim_count"] for entry in ordered
    ):
        raise ContractError("verified claim total differs")
    if type(total_verified_citation_count) is not int or total_verified_citation_count != sum(
        entry["citation_count"] for entry in ordered
    ):
        raise ContractError("verified citation total differs")
    return {
        "campaign_id": require_identifier(verified_claims.get("campaign_id"), "campaign_id"),
        "correlation_id": require_identifier(verified_claims.get("correlation_id"), "correlation_id"),
        "terminal_readback_digest": _digest(
            verified_claims.get("terminal_readback_digest"),
            "terminal_readback_digest",
        ),
        "total_verified_claim_count": total_verified_claim_count,
        "total_verified_citation_count": total_verified_citation_count,
        "candidate_results": ordered,
    }


def _normalize_question_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    collapsed: list[str] = []
    previous_space = False
    for character in normalized:
        if character.isalnum():
            collapsed.append(character)
            previous_space = False
        elif not previous_space:
            collapsed.append(" ")
            previous_space = True
    return "".join(collapsed).strip()


def _tokenize_question_text(text: str) -> tuple[str, ...]:
    normalized = _normalize_question_text(text)
    if not normalized:
        return ()
    return tuple(token for token in normalized.split(" ") if token)


def _normalize_evidence_query(value: Any) -> str:
    query = require_text(value, "evidence_query", maximum=256)
    stripped = query.strip()
    normalized = _normalize_question_text(stripped)
    if not normalized:
        raise ContractError("candidate quality question evidence differs")
    if re.fullmatch(r"[a-z0-9._-]{1,128}", normalized) is not None:
        raise ContractError("candidate quality question evidence differs")
    return normalized


def _validate_evidence_query(value: Any) -> str:
    query = require_text(value, "evidence_query", maximum=256)
    stripped = query.strip()
    normalized = _normalize_question_text(stripped)
    if not normalized:
        raise ContractError("candidate quality question evidence differs")
    if _CANONICAL_EVIDENCE_QUERY_RE.fullmatch(normalized) is None:
        raise ContractError("candidate quality question evidence differs")
    if query != normalized:
        raise ContractError("candidate quality question evidence differs")
    if re.fullmatch(r"[a-z0-9._-]{1,128}", normalized) is not None:
        raise ContractError("candidate quality question evidence differs")
    return normalized


def _candidate_claim_index(candidate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["claim_id"]: record for record in candidate["claim_records"]}


def _validated_precommitted_question_definitions(
    verified: dict[str, Any],
    definitions: Any,
) -> list[dict[str, Any]]:
    if type(definitions) is not list or len(definitions) != _EXPECTED_CANDIDATE_COUNT * len(QUESTION_CATEGORIES):
        raise ContractError("candidate quality questions differ")
    candidates = {candidate["candidate_id"]: candidate for candidate in verified["candidate_results"]}
    per_candidate: dict[str, dict[str, dict[str, Any]]] = {
        candidate_id: {} for candidate_id in _EXPECTED_CANDIDATE_IDS
    }
    for raw in definitions:
        if type(raw) is not dict:
            raise ContractError("candidate quality questions differ")
        category = require_text(raw["category"], "category", maximum=32)
        if category not in QUESTION_CATEGORIES:
            raise ContractError("candidate quality question category differs")
        required_keys = (
            "candidate_id",
            "source_digest",
            "category",
            "prompt",
            "expected_outcome",
            "expected_claim_ids",
        )
        if category != "unsupported":
            required_keys = required_keys + ("evidence_query",)
        require_exact_keys(raw, required_keys, "candidate quality question definition")
        candidate_id = require_identifier(raw["candidate_id"], "candidate_id")
        candidate = candidates.get(candidate_id)
        if candidate is None:
            raise ContractError("candidate quality question candidate differs")
        if category in per_candidate[candidate_id]:
            raise ContractError("candidate question coverage differs")
        source_digest = _digest(raw["source_digest"], "source_digest")
        if source_digest != candidate["source_digest"]:
            raise ContractError("candidate quality question source differs")
        prompt = require_text(raw["prompt"], "prompt", maximum=256)
        expected_outcome = require_text(raw["expected_outcome"], "expected_outcome", maximum=16)
        if expected_outcome not in QUESTION_OUTCOMES:
            raise ContractError("candidate quality question outcome differs")
        claim_ids = raw["expected_claim_ids"]
        if type(claim_ids) is not list:
            raise ContractError("candidate quality question evidence differs")
        normalized_claim_ids = [require_identifier(claim_id, "expected_claim_id") for claim_id in claim_ids]
        if len(normalized_claim_ids) != len(set(normalized_claim_ids)):
            raise ContractError("candidate quality question evidence differs")
        claim_index = _candidate_claim_index(candidate)
        for claim_id in normalized_claim_ids:
            if claim_id not in claim_index:
                raise ContractError("candidate quality question evidence differs")
        if category == "unsupported":
            if expected_outcome != "refusal" or normalized_claim_ids:
                raise ContractError("candidate quality question evidence differs")
        elif expected_outcome != "supported" or not normalized_claim_ids:
            raise ContractError("candidate quality question evidence differs")
        evidence_query = None
        if category == "unsupported":
            if "evidence_query" in raw:
                raise ContractError("candidate quality question evidence differs")
        else:
            evidence_query = _normalize_evidence_query(raw["evidence_query"])
        per_candidate[candidate_id][category] = {
            "candidate_id": candidate_id,
            "source_digest": source_digest,
            "category": category,
            "prompt": prompt,
            **({"evidence_query": evidence_query} if evidence_query is not None else {}),
            "expected_outcome": expected_outcome,
            "expected_claim_ids": normalized_claim_ids,
        }
    ordered = []
    for candidate_id in _EXPECTED_CANDIDATE_IDS:
        coverage = per_candidate[candidate_id]
        if set(coverage) != set(QUESTION_CATEGORIES):
            raise ContractError("candidate question coverage differs")
        for category in QUESTION_CATEGORIES:
            ordered.append(coverage[category])
    return ordered


def _validated_question_bindings(
    verified: dict[str, Any],
    questions: Any,
) -> dict[str, Any]:
    validated_questions = validate_candidate_quality_questions(questions)
    if validated_questions["campaign_id"] != verified["campaign_id"]:
        raise ContractError("candidate quality question campaign differs")
    candidates = {candidate["candidate_id"]: candidate for candidate in verified["candidate_results"]}
    for question in validated_questions["questions"]:
        candidate = candidates.get(question["candidate_id"])
        if candidate is None:
            raise ContractError("candidate quality question candidate differs")
        if question["source_digest"] != candidate["source_digest"]:
            raise ContractError("candidate quality question source differs")
        claim_index = _candidate_claim_index(candidate)
        for claim_id in question["expected_claim_ids"]:
            if claim_id not in claim_index:
                raise ContractError("candidate quality question evidence differs")
    return validated_questions


def _build_candidate_quality_questions(verified_claims: Any, definitions: Any) -> dict[str, Any]:
    verified = _validated_verified_claims(verified_claims)
    normalized_definitions = _validated_precommitted_question_definitions(verified, definitions)
    questions = []
    for question_ordinal, definition in enumerate(normalized_definitions, start=1):
        question = {
            "question_id": f"question-{question_ordinal:02d}",
            "candidate_id": definition["candidate_id"],
            "source_digest": definition["source_digest"],
            "category": definition["category"],
            "prompt": definition["prompt"],
            "expected_outcome": definition["expected_outcome"],
            "expected_claim_ids": list(definition["expected_claim_ids"]),
        }
        if "evidence_query" in definition:
            question["evidence_query"] = definition["evidence_query"]
        questions.append(question)
    artifact = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-questions.v0.1",
            "question_set_id": "candidate-quality-questions-20260812",
            "campaign_id": verified["campaign_id"],
            "question_count": len(questions),
            "questions": questions,
            **_authority_flags(),
        },
        "questions_digest",
        "candidate quality questions",
    )
    return validate_candidate_quality_questions(artifact)


def _matching_question_records(candidate: dict[str, Any], evidence_query: str) -> list[dict[str, Any]]:
    query_tokens = _tokenize_question_text(evidence_query)
    if not query_tokens:
        return []
    phrase_matches = []
    token_matches = []
    query_counts = Counter(query_tokens)
    for record in candidate["claim_records"]:
        record_tokens = _tokenize_question_text(record["claim_text"])
        if len(record_tokens) >= len(query_tokens) and any(
            tuple(record_tokens[index : index + len(query_tokens)]) == query_tokens
            for index in range(len(record_tokens) - len(query_tokens) + 1)
        ):
            phrase_matches.append(record)
            continue
        record_counts = Counter(record_tokens)
        if all(record_counts[token] >= count for token, count in query_counts.items()):
            token_matches.append(record)
    matched = phrase_matches if phrase_matches else token_matches
    matched = sorted(matched, key=lambda item: (item["claim_record_digest"], item["claim_id"]))
    if len(matched) > 4:
        raise ContractError("candidate quality question evidence differs")
    return matched


def _match_millionths(expected_claim_ids: list[str], actual_claim_ids: list[str]) -> int:
    if expected_claim_ids == actual_claim_ids:
        return 1_000_000
    expected = set(expected_claim_ids)
    actual = set(actual_claim_ids)
    union = expected | actual
    if not union:
        return 0
    intersection = expected & actual
    return (len(intersection) * 1_000_000) // len(union)


def _evaluate_candidate_quality_question(candidate_result: Any, question: Any) -> dict[str, Any]:
    candidate = _validated_candidate_result(candidate_result)
    if type(question) is not dict:
        raise ContractError("candidate quality question differs")
    candidate_id = require_identifier(question.get("candidate_id"), "candidate_id")
    if candidate_id != candidate["candidate_id"]:
        raise ContractError("candidate quality question candidate differs")
    source_digest = _digest(question.get("source_digest"), "source_digest")
    if source_digest != candidate["source_digest"]:
        raise ContractError("candidate quality question source differs")
    category = require_text(question.get("category"), "category", maximum=32)
    if category not in QUESTION_CATEGORIES:
        raise ContractError("candidate quality question category differs")
    prompt = require_text(question.get("prompt"), "prompt", maximum=256)
    expected_outcome = require_text(question.get("expected_outcome"), "expected_outcome", maximum=16)
    if expected_outcome not in QUESTION_OUTCOMES:
        raise ContractError("candidate quality question outcome differs")
    expected_claim_ids = question.get("expected_claim_ids")
    if type(expected_claim_ids) is not list:
        raise ContractError("candidate quality question evidence differs")
    normalized_claim_ids = [require_identifier(claim_id, "expected_claim_id") for claim_id in expected_claim_ids]
    if len(normalized_claim_ids) != len(set(normalized_claim_ids)):
        raise ContractError("candidate quality question evidence differs")
    claim_index = _candidate_claim_index(candidate)
    for claim_id in normalized_claim_ids:
        if claim_id not in claim_index:
            raise ContractError("candidate quality question evidence differs")
    if category == "unsupported":
        if expected_outcome != "refusal" or normalized_claim_ids:
            raise ContractError("candidate quality question evidence differs")
        if "evidence_query" in question:
            raise ContractError("candidate quality question evidence differs")
        matching_records: list[dict[str, Any]] = []
    elif expected_outcome != "supported" or not normalized_claim_ids:
        raise ContractError("candidate quality question evidence differs")
    else:
        evidence_query = _validate_evidence_query(question.get("evidence_query"))
        matching_records = _matching_question_records(candidate, evidence_query)
    actual_claim_ids = [record["claim_id"] for record in matching_records]
    actual_claim_texts = [record["claim_text"] for record in matching_records]
    actual_outcome = "supported" if matching_records else "refusal"
    return {
        "question_id": require_identifier(question.get("question_id"), "question_id"),
        "candidate_id": candidate["candidate_id"],
        "category": category,
        "query": prompt,
        "evidence_query": None if category == "unsupported" else evidence_query,
        "expected_outcome": expected_outcome,
        "actual_outcome": actual_outcome,
        "supporting_claim_ids": actual_claim_ids,
        "supporting_claim_texts": actual_claim_texts,
        "evidence_match_millionths": 0
        if actual_outcome == "refusal"
        else _match_millionths(normalized_claim_ids, actual_claim_ids),
    }


def _evaluate_candidate_quality_questions(verified_claims: Any, questions: Any) -> dict[str, Any]:
    verified = _validated_verified_claims(verified_claims)
    validated_questions = _validated_question_bindings(verified, questions)
    candidates = {candidate["candidate_id"]: candidate for candidate in verified["candidate_results"]}
    results = []
    for question in validated_questions["questions"]:
        detail = _evaluate_candidate_quality_question(candidates[question["candidate_id"]], question)
        results.append(
            {
                "question_id": detail["question_id"],
                "candidate_id": detail["candidate_id"],
                "expected_outcome": detail["expected_outcome"],
                "actual_outcome": detail["actual_outcome"],
                "supporting_claim_ids": detail["supporting_claim_ids"],
                "supporting_claim_count": len(detail["supporting_claim_ids"]),
                "evidence_match_millionths": detail["evidence_match_millionths"],
            }
        )
    artifact = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-question-results.v0.1",
            "question_results_id": "candidate-quality-question-results-20260812",
            "campaign_id": verified["campaign_id"],
            "questions_digest": validated_questions["questions_digest"],
            "result_count": len(results),
            "results": results,
            **_authority_flags(),
        },
        "question_results_digest",
        "candidate quality question results",
    )
    return validate_candidate_quality_question_results(artifact)


def build_sampling_policy(verified_claims: Any) -> dict[str, Any]:
    verified = _validated_verified_claims(verified_claims)
    policy = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-sampling-policy.v0.1",
            "policy_id": "candidate-quality-policy-20260812",
            "campaign_id": verified["campaign_id"],
            "terminal_readback_digest": verified["terminal_readback_digest"],
            "total_candidate_count": _EXPECTED_CANDIDATE_COUNT,
            "total_claim_count": verified["total_verified_claim_count"],
            "total_sample_count": _TOTAL_SAMPLED_CLAIMS,
            "allocations": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_digest": candidate["candidate_digest"],
                    "provenance_digest": candidate["provenance_digest"],
                    "source_digest": candidate["source_digest"],
                    "claim_count": candidate["claim_count"],
                    "sample_count": _EXPECTED_SAMPLE_ALLOCATIONS[candidate["candidate_id"]],
                }
                for candidate in verified["candidate_results"]
            ],
            "selection_priority": list(SELECTION_PRIORITY),
            **_authority_flags(),
        },
        "policy_digest",
        "candidate quality sampling policy",
    )
    return validate_candidate_quality_sampling_policy(policy)


def _first_record_by_digest(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    return sorted(records, key=lambda item: (item["claim_record_digest"], item["claim_id"]))[0]


def _position_records(records: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    first = records[0]
    middle = records[(len(records) - 1) // 2]
    final = records[-1]
    ordered: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for reason, record in (
        ("first_position", first),
        ("middle_position", middle),
        ("final_position", final),
    ):
        if record["claim_id"] in seen:
            continue
        seen.add(record["claim_id"])
        ordered.append((reason, record))
    return ordered


def _length_records(records: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    lengths = [(len(record["claim_text"]), record) for record in records]
    longest_size = max(length for length, _record in lengths)
    shortest_size = min(length for length, _record in lengths)
    longest = _first_record_by_digest([record for length, record in lengths if length == longest_size])
    shortest = _first_record_by_digest([record for length, record in lengths if length == shortest_size])
    ordered: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for reason, record in (
        ("longest_claim", longest),
        ("shortest_claim", shortest),
    ):
        if record is None or record["claim_id"] in seen:
            continue
        seen.add(record["claim_id"])
        ordered.append((reason, record))
    return ordered


def _block_type_records(records: list[dict[str, Any]], selected_claim_ids: set[str]) -> list[tuple[str, dict[str, Any]]]:
    observed_types: list[str] = []
    seen_types: set[str] = set()
    for record in records:
        block_type = record["block_type"]
        if block_type not in seen_types:
            seen_types.add(block_type)
            observed_types.append(block_type)
    covered_types = {record["block_type"] for record in records if record["claim_id"] in selected_claim_ids}
    ordered: list[tuple[str, dict[str, Any]]] = []
    for block_type in observed_types:
        if block_type in covered_types:
            continue
        representative = _first_record_by_digest(
            [
                record
                for record in records
                if record["block_type"] == block_type and record["claim_id"] not in selected_claim_ids
            ]
        )
        if representative is None:
            continue
        ordered.append(("block_type_representative", representative))
        selected_claim_ids.add(representative["claim_id"])
    for _reason, record in ordered:
        selected_claim_ids.remove(record["claim_id"])
    return ordered


def _append_ranked(
    selected: list[tuple[str, dict[str, Any]]],
    selected_claim_ids: set[str],
    eligible: list[tuple[str, dict[str, Any]]],
    remaining: int,
) -> int:
    ranked = [(reason, record) for reason, record in eligible if record["claim_id"] not in selected_claim_ids]
    if not ranked or remaining <= 0:
        return remaining
    if len(ranked) > remaining:
        ranked = sorted(ranked, key=lambda item: (item[1]["claim_record_digest"], item[1]["claim_id"]))[:remaining]
    for reason, record in ranked:
        if record["claim_id"] in selected_claim_ids:
            continue
        selected.append((reason, record))
        selected_claim_ids.add(record["claim_id"])
        remaining -= 1
        if remaining == 0:
            break
    return remaining


def _candidate_sample_records(candidate: dict[str, Any], sample_count: int) -> list[tuple[str, dict[str, Any]]]:
    records = candidate["claim_records"]
    if len(records) < sample_count:
        raise ContractError("candidate does not contain enough claims for sampling")
    if sample_count == candidate["claim_count"]:
        return [("digest_fill", record) for record in records]

    selected: list[tuple[str, dict[str, Any]]] = []
    selected_claim_ids: set[str] = set()
    remaining = sample_count
    remaining = _append_ranked(
        selected,
        selected_claim_ids,
        [("binding_risk", record) for record in _sorted_unique_by_digest([record for record in records if record["binding_risk_suspect"]])],
        remaining,
    )
    remaining = _append_ranked(
        selected,
        selected_claim_ids,
        [
            ("normalized_duplicate", record)
            for record in _sorted_unique_by_digest([record for record in records if record["normalized_duplicate_suspect"]])
        ],
        remaining,
    )
    remaining = _append_ranked(
        selected,
        selected_claim_ids,
        [("fragmentation", record) for record in _sorted_unique_by_digest([record for record in records if record["fragmentation_suspect"]])],
        remaining,
    )
    remaining = _append_ranked(selected, selected_claim_ids, _position_records(records), remaining)
    remaining = _append_ranked(selected, selected_claim_ids, _length_records(records), remaining)
    remaining = _append_ranked(selected, selected_claim_ids, _block_type_records(records, selected_claim_ids), remaining)
    remaining = _append_ranked(
        selected,
        selected_claim_ids,
        [("digest_fill", record) for record in _sorted_unique_by_digest(records)],
        remaining,
    )
    if remaining != 0:
        raise ContractError("candidate sampling allocation could not be satisfied")
    return selected


def _sampling_policy_bindings(policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["candidate_id"]: item for item in policy["allocations"]}


def build_candidate_quality_sample(verified_claims: Any, *, policy: Any | None = None) -> dict[str, Any]:
    verified = _validated_verified_claims(verified_claims)
    expected_policy = build_sampling_policy(verified)
    validated_policy = expected_policy if policy is None else validate_candidate_quality_sampling_policy(policy)
    if validated_policy != expected_policy:
        raise ContractError("sampling policy binding differs")
    policy_bindings = _sampling_policy_bindings(validated_policy)
    selections = []
    sample_ordinal = 1
    for candidate in verified["candidate_results"]:
        binding = policy_bindings[candidate["candidate_id"]]
        if (
            binding["candidate_digest"] != candidate["candidate_digest"]
            or binding["provenance_digest"] != candidate["provenance_digest"]
            or binding["source_digest"] != candidate["source_digest"]
            or binding["claim_count"] != candidate["claim_count"]
        ):
            raise ContractError("sampling policy candidate binding differs")
        sample_count = binding["sample_count"]
        for reason_code, record in _candidate_sample_records(candidate, sample_count):
            selections.append(
                {
                    "sample_ordinal": sample_ordinal,
                    "candidate_id": candidate["candidate_id"],
                    "claim_id": record["claim_id"],
                    "citation_id": record["citation_id"],
                    "claim_record_digest": record["claim_record_digest"],
                    "candidate_digest": binding["candidate_digest"],
                    "provenance_digest": binding["provenance_digest"],
                    "source_digest": binding["source_digest"],
                    "reason_codes": [reason_code],
                }
            )
            sample_ordinal += 1
    if sample_ordinal != _TOTAL_SAMPLED_CLAIMS + 1:
        raise ContractError("sample selection count differs")
    sample = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-sample.v0.1",
            "sample_id": "candidate-quality-sample-20260812",
            "campaign_id": verified["campaign_id"],
            "policy_digest": validated_policy["policy_digest"],
            "total_selected_claims": _TOTAL_SAMPLED_CLAIMS,
            "selections": selections,
            **_authority_flags(),
        },
        "sample_digest",
        "candidate quality sample",
    )
    return validate_candidate_quality_sample(sample)


def _validated_reviewer_id(value: Any) -> str:
    return require_identifier(value, "reviewer_id")


def _validated_reviewed_at(value: Any) -> str:
    if type(value) is not str or _UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise ContractError("reviewed_at must be an exact UTC timestamp")
    return value


def _validated_sample_binding(verified_claims: Any, sample: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    verified = _validated_verified_claims(verified_claims)
    expected_sample = build_candidate_quality_sample(verified)
    validated_sample = validate_candidate_quality_sample(sample)
    if validated_sample != expected_sample:
        raise ContractError("annotation sample binding differs")
    return verified, validated_sample


def _claim_record_index(verified: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in verified["candidate_results"]:
        for record in candidate["claim_records"]:
            key = (candidate["candidate_id"], record["claim_id"])
            if key in index:
                raise ContractError("verified claim identities must be unique")
            index[key] = record
    return index


def _selection_index(sample: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {selection["sample_ordinal"]: selection for selection in sample["selections"]}


def _build_candidate_quality_annotation_template(
    verified_claims: Any,
    sample: Any,
    *,
    reviewer_id: Any,
    reviewed_at: Any,
) -> dict[str, Any]:
    # Detailed sampled text is only for ignored runtime annotation work products.
    verified, validated_sample = _validated_sample_binding(verified_claims, sample)
    claim_index = _claim_record_index(verified)
    template_annotations = []
    for selection in validated_sample["selections"]:
        record = claim_index.get((selection["candidate_id"], selection["claim_id"]))
        if record is None:
            raise ContractError("annotation sample claim binding differs")
        if (
            selection["citation_id"] != record["citation_id"]
            or selection["claim_record_digest"] != record["claim_record_digest"]
        ):
            raise ContractError("annotation sample citation binding differs")
        template_annotations.append(
            {
                "sample_ordinal": selection["sample_ordinal"],
                "candidate_id": selection["candidate_id"],
                "candidate_digest": selection["candidate_digest"],
                "provenance_digest": selection["provenance_digest"],
                "claim_id": selection["claim_id"],
                "citation_id": selection["citation_id"],
                "claim_record_digest": selection["claim_record_digest"],
                "source_digest": selection["source_digest"],
                "policy_digest": validated_sample["policy_digest"],
                "reason_codes": list(selection["reason_codes"]),
                "claim_text": record["claim_text"],
                "citation_text": record["citation_text"],
            }
        )
    return {
        "schema_version": "ao.lore.candidate-quality-annotation-template.v0.1",
        "template_id": "candidate-quality-annotation-template-20260812",
        "campaign_id": validated_sample["campaign_id"],
        "sample_digest": validated_sample["sample_digest"],
        "policy_digest": validated_sample["policy_digest"],
        "reviewer_id": _validated_reviewer_id(reviewer_id),
        "reviewed_at": _validated_reviewed_at(reviewed_at),
        "allowed_labels": list(ANNOTATION_LABELS),
        "annotations": template_annotations,
    }


def _validated_annotation_template(value: Any, *, sample: dict[str, Any]) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError("annotation template must be an exact object")
    require_exact_keys(
        value,
        (
            "schema_version",
            "template_id",
            "campaign_id",
            "sample_digest",
            "policy_digest",
            "reviewer_id",
            "reviewed_at",
            "allowed_labels",
            "annotations",
        ),
        "annotation template",
    )
    if value["schema_version"] != "ao.lore.candidate-quality-annotation-template.v0.1":
        raise ContractError("annotation template schema version differs")
    allowed_labels = value["allowed_labels"]
    if type(allowed_labels) is not list or list(allowed_labels) != list(ANNOTATION_LABELS):
        raise ContractError("annotation labels differ")
    annotations = value["annotations"]
    if type(annotations) is not list or len(annotations) != _TOTAL_SAMPLED_CLAIMS:
        raise ContractError("annotation count differs")
    normalized_annotations = []
    selection_index = _selection_index(sample)
    ordinals: list[int] = []
    seen_claims: set[tuple[str, str]] = set()
    for raw in annotations:
        if type(raw) is not dict:
            raise ContractError("annotation must be an exact object")
        keys = set(raw)
        allowed = {
            "sample_ordinal",
            "candidate_id",
            "candidate_digest",
            "provenance_digest",
            "claim_id",
            "citation_id",
            "claim_record_digest",
            "source_digest",
            "policy_digest",
            "reason_codes",
            "claim_text",
            "citation_text",
            "classification",
            "rationale",
        }
        required = allowed - {"classification", "rationale"}
        if keys != required and keys != allowed:
            raise ContractError("annotation keys differ")
        ordinal = raw.get("sample_ordinal")
        if type(ordinal) is not int or not 1 <= ordinal <= _TOTAL_SAMPLED_CLAIMS:
            raise ContractError("annotation ordinal differs")
        selection = selection_index.get(ordinal)
        if selection is None:
            raise ContractError("annotation ordinal differs")
        candidate_id = require_identifier(raw.get("candidate_id"), "candidate_id")
        claim_id = require_identifier(raw.get("claim_id"), "claim_id")
        identity = (candidate_id, claim_id)
        if identity in seen_claims:
            raise ContractError("annotation claim bindings must be unique")
        seen_claims.add(identity)
        if (
            candidate_id != selection["candidate_id"]
            or claim_id != selection["claim_id"]
            or require_identifier(raw.get("citation_id"), "citation_id") != selection["citation_id"]
            or _digest(raw.get("candidate_digest"), "candidate_digest") != selection["candidate_digest"]
            or _digest(raw.get("provenance_digest"), "provenance_digest") != selection["provenance_digest"]
            or _digest(raw.get("claim_record_digest"), "claim_record_digest") != selection["claim_record_digest"]
            or _digest(raw.get("source_digest"), "source_digest") != selection["source_digest"]
            or _digest(raw.get("policy_digest"), "policy_digest") != sample["policy_digest"]
        ):
            raise ContractError("annotation sample binding differs")
        reason_codes = raw.get("reason_codes")
        if type(reason_codes) is not list or list(reason_codes) != list(selection["reason_codes"]):
            raise ContractError("annotation reason codes differ")
        claim_text = require_text(raw.get("claim_text"), "claim_text", maximum=4096)
        citation_text = require_text(raw.get("citation_text"), "citation_text", maximum=4096)
        if claim_text != citation_text:
            raise ContractError("annotation claim citation text binding differs")
        classification = raw.get("classification")
        rationale = raw.get("rationale")
        if classification is None or rationale is None:
            raise ContractError("annotation publication requires complete labels and rationale")
        if classification not in ANNOTATION_LABELS:
            raise ContractError("annotation classification differs")
        normalized_annotations.append(
            {
                "sample_ordinal": ordinal,
                "candidate_id": candidate_id,
                "candidate_digest": selection["candidate_digest"],
                "provenance_digest": selection["provenance_digest"],
                "claim_id": claim_id,
                "citation_id": selection["citation_id"],
                "claim_record_digest": selection["claim_record_digest"],
                "source_digest": selection["source_digest"],
                "policy_digest": sample["policy_digest"],
                "reason_codes": list(selection["reason_codes"]),
                "claim_text": claim_text,
                "citation_text": citation_text,
                "classification": classification,
                "rationale": require_text(rationale, "rationale", maximum=512),
            }
        )
        ordinals.append(ordinal)
    if sorted(ordinals) != list(range(1, _TOTAL_SAMPLED_CLAIMS + 1)):
        raise ContractError("annotation ordinals differ")
    if value["campaign_id"] != sample["campaign_id"]:
        raise ContractError("annotation campaign binding differs")
    if _digest(value["sample_digest"], "sample_digest") != sample["sample_digest"]:
        raise ContractError("annotation sample digest differs")
    if _digest(value["policy_digest"], "policy_digest") != sample["policy_digest"]:
        raise ContractError("annotation policy digest differs")
    return {
        "schema_version": value["schema_version"],
        "template_id": require_identifier(value["template_id"], "template_id"),
        "campaign_id": require_identifier(value["campaign_id"], "campaign_id"),
        "sample_digest": sample["sample_digest"],
        "policy_digest": sample["policy_digest"],
        "reviewer_id": _validated_reviewer_id(value["reviewer_id"]),
        "reviewed_at": _validated_reviewed_at(value["reviewed_at"]),
        "allowed_labels": list(ANNOTATION_LABELS),
        "annotations": normalized_annotations,
    }


def _build_candidate_quality_annotations(
    verified_claims: Any,
    sample: Any,
    annotations: Any,
) -> dict[str, Any]:
    # Reviewer identity, timestamp, labels, and rationale remain ignored runtime evidence only.
    _verified, validated_sample = _validated_sample_binding(verified_claims, sample)
    validated_template = _validated_annotation_template(annotations, sample=validated_sample)
    annotation_counts = {label: 0 for label in ANNOTATION_LABELS}
    published_annotations = []
    for annotation in validated_template["annotations"]:
        annotation_counts[annotation["classification"]] += 1
        published_annotations.append(
            {
                "sample_ordinal": annotation["sample_ordinal"],
                "candidate_id": annotation["candidate_id"],
                "claim_id": annotation["claim_id"],
                "citation_id": annotation["citation_id"],
                "source_digest": annotation["source_digest"],
                "policy_digest": annotation["policy_digest"],
                "classification": annotation["classification"],
                "rationale": annotation["rationale"],
            }
        )
    artifact = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-annotations.v0.1",
            "annotations_id": "candidate-quality-annotations-20260812",
            "campaign_id": validated_sample["campaign_id"],
            "sample_digest": validated_sample["sample_digest"],
            "reviewer_id": validated_template["reviewer_id"],
            "reviewed_at": validated_template["reviewed_at"],
            "annotation_counts": annotation_counts,
            "annotations": published_annotations,
            **_authority_flags(),
        },
        "annotations_digest",
        "candidate quality annotations",
    )
    return validate_candidate_quality_annotations(artifact)


def _derive_candidate_quality_results(
    verified_claims: Any,
    sampling_policy: Any,
    sample: Any,
    annotations: Any,
    questions: Any,
    question_results: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Derive evaluation-only recommendations from already verified evidence."""
    if type(verified_claims) is not dict or type(verified_claims.get("candidate_results")) is not list:
        raise ContractError("verified candidate quality evidence differs")
    checked_annotations = validate_candidate_quality_annotations(annotations)
    checked_policy = validate_candidate_quality_sampling_policy(sampling_policy)
    checked_sample = validate_candidate_quality_sample(sample)
    checked_questions = validate_candidate_quality_questions(questions)
    checked_question_results = validate_candidate_quality_question_results(question_results)
    campaign_id = require_identifier(verified_claims.get("campaign_id"), "campaign_id")
    if any(item["campaign_id"] != campaign_id for item in (checked_policy, checked_sample, checked_annotations, checked_questions, checked_question_results)):
        raise ContractError("candidate quality campaign binding differs")
    if checked_sample["policy_digest"] != checked_policy["policy_digest"] or checked_annotations["sample_digest"] != checked_sample["sample_digest"]:
        raise ContractError("candidate quality sample binding differs")
    if checked_question_results["questions_digest"] != checked_questions["questions_digest"]:
        raise ContractError("candidate quality question binding differs")

    verified_rows = verified_claims["candidate_results"]
    if len(verified_rows) != _EXPECTED_CANDIDATE_COUNT:
        raise ContractError("candidate quality candidate count differs")
    aliases = tuple(f"candidate-{index:02d}" for index in range(1, 7))
    annotation_rows = checked_annotations["annotations"]
    question_rows = checked_question_results["results"]
    derived: list[dict[str, Any]] = []
    for ordinal, verified in enumerate(verified_rows):
        if type(verified) is not dict:
            raise ContractError("verified candidate quality result differs")
        candidate_id = require_identifier(verified.get("candidate_id"), "candidate_id")
        alias = aliases[ordinal]
        classifications = Counter(
            row["classification"] for row in annotation_rows if row["candidate_id"] == candidate_id
        )
        sampled = sum(classifications.values())
        outcomes = {"supported": 0, "refusal": 0, "mismatch": 0}
        candidate_questions = [row for row in question_rows if row["candidate_id"] == alias]
        if len(candidate_questions) != 4:
            raise ContractError("candidate quality question alias binding differs")
        for row in candidate_questions:
            exact = row["actual_outcome"] == row["expected_outcome"]
            if row["actual_outcome"] == "supported":
                exact = exact and row["evidence_match_millionths"] == 1_000_000 and bool(row["supporting_claim_ids"])
            else:
                exact = exact and row["evidence_match_millionths"] == 0 and not row["supporting_claim_ids"]
            outcomes[row["actual_outcome"] if exact else "mismatch"] += 1
        auto = verified.get("automatic_check_counts")
        if type(auto) is not dict:
            raise ContractError("candidate quality automatic checks differ")
        automatic_counts = {
            "verified_claim_count": auto.get("verified_claim_count"),
            "verified_citation_count": auto.get("verified_citation_count"),
            "duplicate_suspect_count": auto.get("duplicate_suspect_count"),
            "fragmentation_suspect_count": auto.get("fragmentation_suspect_count"),
            "binding_error_count": auto.get("binding_error_count"),
        }
        class_counts = {label: classifications.get(label, 0) for label in ANNOTATION_LABELS}
        material = (
            class_counts["binding_error"] > 0
            or class_counts["misleading_without_context"] > 0
            or automatic_counts["binding_error_count"] != 0
            or outcomes["mismatch"] != 0
        )
        passes = (
            not material
            and sampled > 0
            and class_counts["useful_exact"] * 100 >= sampled * 90
            and class_counts["duplicate"] * 100 <= sampled * 5
            and class_counts["exact_but_fragmented"] * 100 <= sampled * 5
        )
        recommendation = "reject" if material else ("pass" if passes else "hold")
        result = _self_digest(
            {
                "schema_version": "ao.lore.candidate-quality-result.v0.1",
                "result_id": f"candidate-quality-result-{ordinal + 1:02d}",
                "campaign_id": campaign_id,
                "candidate_id": candidate_id,
                "candidate_digest": _digest(verified.get("candidate_digest"), "candidate_digest"),
                "provenance_digest": _digest(verified.get("provenance_digest"), "provenance_digest"),
                "source_digest": _digest(verified.get("source_digest"), "source_digest"),
                "sample_digest": checked_annotations["sample_digest"],
                "annotations_digest": checked_annotations["annotations_digest"],
                "questions_digest": checked_questions["questions_digest"],
                "question_results_digest": checked_question_results["question_results_digest"],
                "total_candidate_claim_count": verified.get("claim_count"),
                "sampled_claim_count": sampled,
                "question_count": 4,
                "classification_counts": class_counts,
                "question_outcome_counts": outcomes,
                "automatic_check_counts": automatic_counts,
                "recommendation": recommendation,
                **_authority_flags(),
            },
            "result_digest",
            "candidate quality result",
        )
        derived.append(validate_candidate_quality_result(result))
    campaign = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-campaign.v0.1",
            "campaign_id": campaign_id,
            "correlation_id": require_identifier(verified_claims.get("correlation_id"), "correlation_id"),
            "terminal_readback_digest": _digest(verified_claims.get("terminal_readback_digest"), "terminal_readback_digest"),
            "total_candidate_count": 6,
            "total_claim_count": 486,
            "total_sampled_claim_count": 96,
            "total_question_count": 24,
            "candidate_bindings": [
                {
                    "candidate_id": require_identifier(row.get("candidate_id"), "candidate_id"),
                    "candidate_digest": _digest(row.get("candidate_digest"), "candidate_digest"),
                    "provenance_digest": _digest(row.get("provenance_digest"), "provenance_digest"),
                    "source_digest": _digest(row.get("source_digest"), "source_digest"),
                    "claim_count": row.get("claim_count"),
                }
                for row in verified_rows
            ],
            "sampling_policy_digest": checked_policy["policy_digest"],
            "sample_digest": checked_sample["sample_digest"],
            "annotations_digest": checked_annotations["annotations_digest"],
            "questions_digest": checked_questions["questions_digest"],
            "question_results_digest": checked_question_results["question_results_digest"],
            "result_digests": [row["result_digest"] for row in derived],
            **_authority_flags(),
        },
        "campaign_digest",
        "candidate quality campaign",
    )
    campaign = validate_candidate_quality_campaign(campaign)
    severity = {"pass": 0, "hold": 1, "reject": 2}
    corpus = max((row["recommendation"] for row in derived), key=severity.__getitem__)
    summary = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-summary.v0.1",
            "summary_id": "candidate-quality-summary-20260812",
            "campaign_id": campaign_id,
            "correlation_id": require_identifier(verified_claims.get("correlation_id"), "correlation_id"),
            "terminal_readback_digest": verified_claims["terminal_readback_digest"],
            "campaign_digest": campaign["campaign_digest"],
            "total_candidate_count": 6,
            "total_sampled_claim_count": 96,
            "total_question_count": 24,
            "candidate_recommendations": [
                {"candidate_id": row["candidate_id"], "recommendation": row["recommendation"], "result_digest": row["result_digest"]}
                for row in derived
            ],
            "corpus_recommendation": corpus,
            **_authority_flags(),
        },
        "summary_digest",
        "candidate quality summary",
    )
    return _deep_copy(derived), campaign, validate_candidate_quality_summary(summary)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def _write_durable_exclusive(parent_fd: int, name: str, value: Any) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, 0o600, dir_fd=parent_fd)
    try:
        body = _json_bytes(value)
        written = 0
        while written < len(body):
            written += os.write(fd, body[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_staged_json_at(parent_fd: int, name: str) -> Any:
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_JSON_BYTES:
            raise ContractError("candidate quality staged artifact differs")
        body = b""
        while len(body) <= _MAX_JSON_BYTES:
            chunk = os.read(fd, min(65536, _MAX_JSON_BYTES + 1 - len(body)))
            if not chunk:
                break
            body += chunk
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size):
            raise ContractError("candidate quality staged artifact changed")
        return parse_strict_json(body, label="candidate quality staged artifact")
    finally:
        os.close(fd)


def _ensure_durable_json(parent_fd: int, name: str, value: Any) -> None:
    try:
        _write_durable_exclusive(parent_fd, name, value)
    except FileExistsError:
        if _read_staged_json_at(parent_fd, name) != value:
            raise ContractError("candidate quality staged artifact collision")


def _rename_directory_noreplace(parent_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ContractError("candidate quality no-replace publication is unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _verify_publication_at(
    root_fd: int,
    final_name: str,
    expected_names: set[str],
    checked_results: list[dict[str, Any]],
    checked_campaign: dict[str, Any],
    checked_summary: dict[str, Any],
    recovery: dict[str, Any],
) -> None:
    fd = os.open(final_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
    try:
        before = os.fstat(fd)
        named = os.stat(final_name, dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode) or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino):
            raise ContractError("candidate quality publication collision")
        if set(os.listdir(fd)) != expected_names:
            raise ContractError("candidate quality publication collision")
        expected_intent = {
            "campaign_digest": checked_summary["campaign_digest"],
            "summary_digest": checked_summary["summary_digest"],
        }
        if _read_staged_json_at(fd, "intent.json") != expected_intent:
            raise ContractError("candidate quality publication collision")
        if validate_candidate_quality_campaign(_read_staged_json_at(fd, "campaign.json")) != checked_campaign:
            raise ContractError("candidate quality publication collision")
        if validate_candidate_quality_summary(_read_staged_json_at(fd, "summary.json")) != checked_summary:
            raise ContractError("candidate quality publication collision")
        if validate_candidate_quality_recovery(_read_staged_json_at(fd, "recovery.json")) != recovery:
            raise ContractError("candidate quality publication collision")
        for index, expected in enumerate(checked_results, start=1):
            if validate_candidate_quality_result(_read_staged_json_at(fd, f"result-{index:02d}.json")) != expected:
                raise ContractError("candidate quality publication collision")
        after = os.fstat(fd)
        rebound = os.stat(final_name, dir_fd=root_fd, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or (after.st_dev, after.st_ino) != (rebound.st_dev, rebound.st_ino):
            raise ContractError("candidate quality publication changed")
    finally:
        os.close(fd)


def _publish_candidate_quality_campaign(
    output_root: Path,
    results: Any,
    campaign: Any,
    summary: Any,
    *,
    fail_after: str | None = None,
) -> dict[str, Any]:
    """Durably publish one immutable campaign directory, or verify an exact retry."""
    root = Path(output_root)
    reject_symlink_ancestors(root, include_self=True)
    try:
        root_stat = root.lstat()
    except OSError as exc:
        raise _fail("candidate quality output root is unavailable", exc)
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        raise ContractError("candidate quality output root is unsafe")
    checked_results = [validate_candidate_quality_result(row) for row in results]
    checked_campaign = validate_candidate_quality_campaign(campaign)
    checked_summary = validate_candidate_quality_summary(summary)
    if checked_campaign["campaign_digest"] != checked_summary["campaign_digest"]:
        raise ContractError("candidate quality campaign summary binding differs")
    if checked_campaign["result_digests"] != [row["result_digest"] for row in checked_results]:
        raise ContractError("candidate quality campaign result binding differs")
    if [
        (row["candidate_id"], row["recommendation"], row["result_digest"])
        for row in checked_results
    ] != [
        (row["candidate_id"], row["recommendation"], row["result_digest"])
        for row in checked_summary["candidate_recommendations"]
    ]:
        raise ContractError("candidate quality publication binding differs")
    suffix = checked_summary["campaign_digest"].split(":", 1)[1]
    final_name = f"campaign-{suffix}"
    stage_name = f"staging-{suffix[:32]}"
    recovery = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-recovery.v0.1",
            "recovery_id": f"candidate-quality-recovery-{suffix[:16]}",
            "campaign_id": checked_summary["campaign_id"],
            "campaign_digest": checked_summary["campaign_digest"],
            "phase": "summary",
            "status": "ready_to_resume",
            "retained_artifact_digests": [row["result_digest"] for row in checked_results] + [checked_summary["summary_digest"]],
            "owned_staging_relpaths": [stage_name],
            **_authority_flags(),
        },
        "recovery_digest",
        "candidate quality recovery",
    )
    recovery = validate_candidate_quality_recovery(recovery)
    expected_names = {
        "intent.json",
        "summary.json",
        "campaign.json",
        "recovery.json",
        "recovery-intent.json",
        "recovery-results.json",
        "recovery-summary.json",
        *(f"result-{index:02d}.json" for index in range(1, len(checked_results) + 1)),
    }
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    try:
        current = os.fstat(root_fd)
        if (current.st_dev, current.st_ino) != (root_stat.st_dev, root_stat.st_ino):
            raise ContractError("candidate quality output root changed")
        lock_fd = os.open(".campaign.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=root_fd)
        try:
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                raise ContractError("candidate quality campaign lock is unsafe")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                os.stat(final_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                destination_exists = False
            else:
                destination_exists = True
            if destination_exists:
                _verify_publication_at(root_fd, final_name, expected_names, checked_results, checked_campaign, checked_summary, recovery)
                return {"campaign_digest": checked_summary["campaign_digest"], "summary_digest": checked_summary["summary_digest"], "recovery_digest": recovery["recovery_digest"], "publication_relpath": final_name}
            created_stage = True
            try:
                os.mkdir(stage_name, 0o700, dir_fd=root_fd)
            except FileExistsError:
                created_stage = False
            stage_fd = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
            try:
                present_names = set(os.listdir(stage_fd))
                if not present_names.issubset(expected_names):
                    raise ContractError("candidate quality owned staging collision")
                intent = {"campaign_digest": checked_summary["campaign_digest"], "summary_digest": checked_summary["summary_digest"]}
                if created_stage:
                    _write_durable_exclusive(stage_fd, "intent.json", intent)
                elif _read_staged_json_at(stage_fd, "intent.json") != intent:
                    raise ContractError("candidate quality owned staging collision")
                def phase_record(name: str, phase: str, status: str, retained: list[str]) -> None:
                    record = _self_digest(
                        {
                            "schema_version": "ao.lore.candidate-quality-recovery.v0.1",
                            "recovery_id": f"candidate-quality-{name}-{suffix[:16]}",
                            "campaign_id": checked_summary["campaign_id"],
                            "campaign_digest": checked_summary["campaign_digest"],
                            "phase": phase,
                            "status": status,
                            "retained_artifact_digests": retained,
                            "owned_staging_relpaths": [stage_name],
                            **_authority_flags(),
                        },
                        "recovery_digest",
                        "candidate quality recovery",
                    )
                    _ensure_durable_json(stage_fd, f"recovery-{name}.json", validate_candidate_quality_recovery(record))
                phase_record("intent", "intake", "ready_to_resume", [checked_campaign["campaign_digest"]])
                os.fsync(stage_fd)
                if fail_after == "intent":
                    raise RuntimeError("candidate quality failpoint: intent")
                for index, row in enumerate(checked_results, start=1):
                    _ensure_durable_json(stage_fd, f"result-{index:02d}.json", row)
                phase_record("results", "verification", "ready_to_resume", [row["result_digest"] for row in checked_results])
                os.fsync(stage_fd)
                if fail_after == "results":
                    raise RuntimeError("candidate quality failpoint: results")
                _ensure_durable_json(stage_fd, "summary.json", checked_summary)
                _ensure_durable_json(stage_fd, "campaign.json", checked_campaign)
                phase_record("summary", "summary", "ready_to_resume", [checked_campaign["campaign_digest"], checked_summary["summary_digest"]])
                os.fsync(stage_fd)
                if fail_after == "summary":
                    raise RuntimeError("candidate quality failpoint: summary")
                _ensure_durable_json(stage_fd, "recovery.json", recovery)
                os.fsync(stage_fd)
                if fail_after == "recovery":
                    raise RuntimeError("candidate quality failpoint: recovery")
            finally:
                os.close(stage_fd)
            try:
                _rename_directory_noreplace(root_fd, stage_name, final_name)
            except OSError as exc:
                raise _fail("candidate quality publication collision", exc)
            os.fsync(root_fd)
            if fail_after == "publication":
                raise RuntimeError("candidate quality failpoint: publication")
            _verify_publication_at(root_fd, final_name, expected_names, checked_results, checked_campaign, checked_summary, recovery)
            return {"campaign_digest": checked_summary["campaign_digest"], "summary_digest": checked_summary["summary_digest"], "recovery_digest": recovery["recovery_digest"], "publication_relpath": final_name}
        finally:
            os.close(lock_fd)
    finally:
        os.close(root_fd)


def _verify_candidate_quality_publication(
    output_root: Path,
    results: Any,
    campaign: Any,
    summary: Any,
) -> dict[str, Any]:
    root = Path(output_root)
    reject_symlink_ancestors(root, include_self=True)
    checked_results = [validate_candidate_quality_result(row) for row in results]
    checked_campaign = validate_candidate_quality_campaign(campaign)
    checked_summary = validate_candidate_quality_summary(summary)
    suffix = checked_campaign["campaign_digest"].split(":", 1)[1]
    final_name = f"campaign-{suffix}"
    stage_name = f"staging-{suffix[:32]}"
    recovery = _self_digest(
        {
            "schema_version": "ao.lore.candidate-quality-recovery.v0.1",
            "recovery_id": f"candidate-quality-recovery-{suffix[:16]}",
            "campaign_id": checked_summary["campaign_id"],
            "campaign_digest": checked_summary["campaign_digest"],
            "phase": "summary",
            "status": "ready_to_resume",
            "retained_artifact_digests": [row["result_digest"] for row in checked_results] + [checked_summary["summary_digest"]],
            "owned_staging_relpaths": [stage_name],
            **_authority_flags(),
        },
        "recovery_digest",
        "candidate quality recovery",
    )
    recovery = validate_candidate_quality_recovery(recovery)
    expected_names = {
        "intent.json", "summary.json", "campaign.json", "recovery.json",
        "recovery-intent.json", "recovery-results.json", "recovery-summary.json",
        *(f"result-{index:02d}.json" for index in range(1, len(checked_results) + 1)),
    }
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise _fail("candidate quality publication is unavailable", exc)
    try:
        _verify_publication_at(root_fd, final_name, expected_names, checked_results, checked_campaign, checked_summary, recovery)
    except OSError as exc:
        raise _fail("candidate quality publication is unavailable", exc)
    finally:
        os.close(root_fd)
    return {"campaign_digest": checked_summary["campaign_digest"], "summary_digest": checked_summary["summary_digest"], "recovery_digest": recovery["recovery_digest"], "publication_relpath": final_name}


__all__ = [
    "build_candidate_quality_sample",
    "build_sampling_policy",
    "verify_all_claims",
    "verify_campaign_inputs",
]
