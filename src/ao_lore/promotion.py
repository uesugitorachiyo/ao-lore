"""Governed candidate-promotion preparation primitives.

This module does not authenticate operators or apply canonical mutations.  Its
only state-changing preparation effects are an exclusive proposal and bounded
append-only audit events below explicitly owned promotion runtime state.
"""

from __future__ import annotations

import fcntl
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .benchmark import BenchmarkError, canonical_digest
from .candidates import CandidateError, validate_distillation_result, validate_provenance
from .home import repository_root, runtime_home
from .knowledge_contracts import (
    KnowledgeContractError,
    semantic_key_v0_3,
    validate_knowledge_payload,
)


MAX_JSON_BYTES = 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_FILES = 10_000
MAX_DEPTH = 8
MAX_GENERATION_BYTES = 64 * 1024 * 1024
MAX_AUDIT_EVENTS = 100_000
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REVIEW_RE = re.compile(r"^(?P<sequence>[0-9]{6})-(?P<digest>[0-9a-f]{64})\.json$")
GENERATION_RE = re.compile(r"^(?P<sequence>[0-9]{6})-(?P<id>[a-z0-9][a-z0-9._-]{0,127})$")
AUDIT_RE = re.compile(r"^(?P<sequence>[0-9]{6})-(?P<digest>[0-9a-f]{64})\.json$")
AUDIT_KEYS = {
    "schema_version", "sequence", "event_type", "promotion_id", "authorization_id", "transaction_id",
    "proposal_digest", "authorization_digest", "transaction_digest", "previous_event_digest", "recorded_at",
    "status", "reason_code", "event_digest",
}
AUTHORIZATION_KEYS = {
    "schema_version", "policy_version", "operation", "authorization_id", "nonce", "operator_id",
    "authentication_scope", "proposal_digest", "promotion_id", "candidate_digest", "provenance_digest",
    "accepted_review_head_digest", "canonical_entry_digest", "expected_brain_inventory_digest",
    "expected_prior_generation_id", "expected_result_generation_id", "allowed_write_set_digest", "source_head",
    "issued_at", "expires_at", "one_use_only", "product_self_issued", "provider_authority", "network_authority",
    "publication_authority", "release_authority", "deployment_authority", "batch_authority", "overwrite_authority",
    "unattended_authority", "credential_authority", "authority_advance",
}
PROPOSAL_V0_1_KEYS = {
    "schema_version", "policy_version", "proposal_id", "promotion_id", "candidate_id",
    "candidate_digest", "provenance_digest", "accepted_review_head_digest", "document_ir_digest",
    "source_digest", "source_head", "prior_brain_inventory_digest", "prior_generation_id",
    "prior_generation_digest", "canonical_entry", "canonical_entry_digest", "semantic_key",
    "expected_generation_id", "allowed_write_set", "allowed_write_set_digest", "prepared_at",
    "expires_at", "proposal_digest",
}
PROPOSAL_V0_2_KEYS = {
    "schema_version", "policy_version", "proposal_id", "promotion_id", "candidate_id",
    "candidate_digest", "provenance_digest", "evidence_selection_digest", "evidence_origins",
    "accepted_review_head_digest", "source_head", "prior_brain_inventory_digest",
    "prior_generation_id", "prior_generation_digest", "canonical_entry", "canonical_entry_digest",
    "semantic_key", "expected_generation_id", "allowed_write_set", "allowed_write_set_digest",
    "prepared_at", "expires_at", "proposal_digest",
}
FALSE_AUTHORITY_FIELDS = {
    "product_self_issued", "provider_authority", "network_authority", "publication_authority", "release_authority",
    "deployment_authority", "batch_authority", "overwrite_authority", "unattended_authority",
    "credential_authority", "authority_advance",
}
_LOCK_STATE = threading.local()


class PromotionError(ValueError):
    """A stable promotion contract or containment failure."""


def _error(message: str, cause: Exception | None = None) -> PromotionError:
    result = PromotionError(message)
    if cause is not None:
        result.__cause__ = cause
    return result


def _strict_object(body: bytes, label: str) -> dict[str, Any]:
    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise PromotionError(f"{label} contains duplicate keys")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise PromotionError(f"{label} contains a non-finite number")

    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except PromotionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _error(f"{label} is invalid JSON", exc)
    if type(value) is not dict:
        raise PromotionError(f"{label} must be an object")
    return value


def _contained(path: Path, root: Path, label: str) -> Path:
    selected = Path(os.path.abspath(path))
    allowed = Path(os.path.abspath(root))
    try:
        selected.relative_to(allowed)
    except ValueError as exc:
        raise _error(f"{label} escapes its owned root", exc)
    return selected


def _real_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise _error(f"{label} is unavailable", exc)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise PromotionError(f"{label} must be a real directory")
    return info


def stable_json_file(
    path: Path, *, root: Path, label: str, maximum: int = MAX_JSON_BYTES
) -> tuple[dict[str, Any], bytes]:
    """Read a stable, contained, single-link ordinary JSON file."""

    target = _contained(Path(path), Path(root), label)
    _real_directory(Path(root), f"{label} root")
    try:
        before = os.lstat(target)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise PromotionError(f"{label} must be a single-link regular file")
        if before.st_size > maximum:
            raise PromotionError(f"{label} exceeds its byte budget")
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
            if identity(before) != identity(opened):
                raise PromotionError(f"{label} changed during validation")
            body = b""
            while len(body) <= maximum:
                chunk = os.read(descriptor, min(65536, maximum + 1 - len(body)))
                if not chunk:
                    break
                body += chunk
            after = os.fstat(descriptor)
            if identity(opened) != identity(after):
                raise PromotionError(f"{label} changed during validation")
        finally:
            os.close(descriptor)
    except PromotionError:
        raise
    except OSError as exc:
        raise _error(f"{label} could not be read safely", exc)
    if len(body) > maximum:
        raise PromotionError(f"{label} exceeds its byte budget")
    return _strict_object(body, label), body


def _canonical(value: Any, label: str) -> str:
    try:
        return canonical_digest(value)
    except BenchmarkError as exc:
        raise _error(f"{label} is not strict JSON", exc)


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _write_exclusive(path: Path, value: Mapping[str, Any], root: Path, label: str) -> None:
    target = _contained(path, root, label)
    _real_directory(target.parent, f"{label} parent")
    body = (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()
    if len(body) > MAX_JSON_BYTES:
        raise PromotionError(f"{label} exceeds its byte budget")
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise _error(f"{label} already exists", exc)
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _system_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _git_head() -> str:
    head = (repository_root() / ".git")
    # Worktrees use a gitfile; the fixed source-head provider is replaceable in tests.
    import subprocess
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repository_root(), check=True, capture_output=True, text=True)
    return result.stdout.strip()


@dataclass(frozen=True)
class _PromotionDependencies:
    candidate_root: Path
    brain_root: Path
    promotions_root: Path
    clock: Callable[[], str] = _system_now
    source_head: Callable[[], str] = _git_head
    failpoint: Callable[[str], None] = lambda _: None
    candidate_loader: Callable[
        [str], tuple[dict[str, Any], dict[str, Any], str, str]
    ] | None = None

    @property
    def proposals_root(self) -> Path:
        return self.promotions_root / "proposals"


def _default_dependencies() -> _PromotionDependencies:
    return _PromotionDependencies(
        repository_root() / "working" / "candidates",
        repository_root() / "brain",
        runtime_home() / "promotions",
    )


@contextmanager
def promotion_lock_for_review() -> Iterator[None]:
    """Acquire the production promotion lock before candidate-local locking."""

    with _promotion_lock(_default_dependencies()):
        yield


@contextmanager
def _promotion_lock(dependencies: _PromotionDependencies) -> Iterator[None]:
    lock_key = os.fspath(dependencies.promotions_root.resolve(strict=False))
    if getattr(_LOCK_STATE, "key", None) == lock_key:
        _LOCK_STATE.depth += 1
        try:
            yield
        finally:
            _LOCK_STATE.depth -= 1
        return
    dependencies.promotions_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_descriptor = os.open(dependencies.promotions_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_before = os.fstat(root_descriptor)
    descriptor = os.open("promotion.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root_descriptor)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
            raise PromotionError("promotion lock is invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        path_root = os.stat(dependencies.promotions_root, follow_symlinks=False)
        path_lock = os.stat("promotion.lock", dir_fd=root_descriptor, follow_symlinks=False)
        if (root_before.st_dev, root_before.st_ino) != (path_root.st_dev, path_root.st_ino) or (info.st_dev, info.st_ino) != (path_lock.st_dev, path_lock.st_ino):
            raise PromotionError("promotion lock changed while acquiring")
        _LOCK_STATE.key = lock_key; _LOCK_STATE.depth = 1
        yield
    finally:
        _LOCK_STATE.key = None; _LOCK_STATE.depth = 0
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        os.close(root_descriptor)


def _inventory(root: Path) -> tuple[str, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    total = 0
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root_info = os.fstat(root_fd)
    def visit(directory_fd: int, prefix: str, depth: int) -> None:
        nonlocal total
        if depth > MAX_DEPTH: raise PromotionError("brain inventory exceeds depth budget")
        names = sorted(os.listdir(directory_fd))
        if len(names) != len({name.casefold() for name in names}): raise PromotionError("brain inventory has a case-fold collision")
        for name in names:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            relative = f"{prefix}/{name}" if prefix else name
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    opened = os.fstat(child)
                    if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino): raise PromotionError("brain directory changed during inventory")
                    records.append({"path": relative, "kind": "directory", "mode": "private" if before.st_mode & 0o077 == 0 else "shared", "bytes": 0, "digest": None})
                    visit(child, relative, depth + 1)
                    after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns): raise PromotionError("brain directory changed during inventory")
                finally: os.close(child)
            elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                if before.st_size > MAX_ENTRY_BYTES: raise PromotionError("brain entry exceeds byte budget")
                file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
                try:
                    opened = os.fstat(file_fd)
                    if (before.st_dev, before.st_ino, before.st_size) != (opened.st_dev, opened.st_ino, opened.st_size): raise PromotionError("brain entry changed during inventory")
                    hasher = hashlib.sha256(); remaining = before.st_size
                    while remaining:
                        chunk = os.read(file_fd, min(65536, remaining))
                        if not chunk: raise PromotionError("brain entry truncated during inventory")
                        hasher.update(chunk); remaining -= len(chunk)
                    after = os.fstat(file_fd)
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns): raise PromotionError("brain entry changed during inventory")
                finally: os.close(file_fd)
                path_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (path_after.st_dev, path_after.st_ino, path_after.st_size, path_after.st_mtime_ns, path_after.st_ctime_ns): raise PromotionError("brain entry path changed during inventory")
                total += before.st_size; records.append({"path": relative, "kind": "file", "mode": "private" if before.st_mode & 0o077 == 0 else "shared", "bytes": before.st_size, "digest": "sha256:" + hasher.hexdigest()})
            else: raise PromotionError("brain inventory contains an unsupported entry")
            if len(records) > MAX_FILES or total > MAX_GENERATION_BYTES: raise PromotionError("brain inventory exceeds aggregate budget")
    try:
        visit(root_fd, "", 0)
        path_info = os.stat(root, follow_symlinks=False)
        if (root_info.st_dev, root_info.st_ino) != (path_info.st_dev, path_info.st_ino): raise PromotionError("brain root changed during inventory")
    finally: os.close(root_fd)
    return _canonical({"domain": "ao.lore.brain-inventory.v0.1", "entries": records}, "brain inventory"), records


def _load_candidate(candidate_id: str, root: Path) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    if not isinstance(candidate_id, str) or not candidate_id.startswith("candidate-") or not ID_RE.fullmatch(candidate_id):
        raise PromotionError("candidate identity is invalid")
    target = _contained(root / candidate_id, root, "candidate")
    _real_directory(target, "candidate")
    if {entry.name for entry in target.iterdir()} != {"candidate.json", "provenance.json", "reviews"}:
        raise PromotionError("candidate directory allowlist differs")
    candidate, _ = stable_json_file(target / "candidate.json", root=target, label="candidate", maximum=MAX_ENTRY_BYTES)
    provenance, _ = stable_json_file(target / "provenance.json", root=target, label="candidate provenance")
    candidate_digest = _canonical(candidate, "candidate")
    try:
        result_version = (
            "ao.lore.distillation-result.v0.2"
            if candidate.get("schema_version") in {
                "ao.lore.okf-candidate.v0.2", "ao.lore.okf-candidate.v0.3",
            }
            else "ao.lore.distillation-result.v0.1"
        )
        trace = ({
            "adapter": "persisted-candidate-validation",
            "policy_version": "v0.3" if candidate.get("schema_version") == "ao.lore.okf-candidate.v0.3" else "v0.2",
            "input_block_count": 0, "candidate_concept_count": len(candidate.get("concepts", [])),
            "candidate_claim_count": len(candidate.get("claims", [])),
            "candidate_citation_count": len(candidate.get("citations", [])),
            "private_reasoning_persisted": False,
        } if result_version == "ao.lore.distillation-result.v0.2" else {})
        candidate, validated_digest = validate_distillation_result({
            "schema_version": result_version, "candidate": candidate,
            "candidate_digest": candidate_digest, "distillation_trace": trace,
        })
        provenance = validate_provenance(provenance, candidate, validated_digest)
    except CandidateError as exc:
        raise _error("candidate semantic validation failed", exc)
    if candidate.get("candidate_id") != candidate_id:
        raise PromotionError("candidate authority or identity is invalid")
    if candidate.get("schema_version") != "ao.lore.okf-candidate.v0.3":
        for field in ("source_digest", "document_ir_digest"):
            if not isinstance(provenance.get(field), str) or not DIGEST_RE.fullmatch(provenance[field]):
                raise PromotionError("candidate provenance digest is invalid")
    reviews = target / "reviews"
    _real_directory(reviews, "candidate reviews")
    entries = sorted(reviews.iterdir(), key=lambda item: item.name)
    if not entries or len(entries) > 1024:
        raise PromotionError("candidate must have a bounded review chain")
    previous = None
    latest = None
    for sequence, path in enumerate(entries, 1):
        match = REVIEW_RE.fullmatch(path.name)
        if match is None or int(match.group("sequence")) != sequence:
            raise PromotionError("review chain filenames differ")
        event, _ = stable_json_file(path, root=reviews, label="review event")
        supplied = event.get("event_digest")
        actual = _canonical({key: value for key, value in event.items() if key != "event_digest"}, "review event")
        if supplied != actual or match.group("digest") != actual[7:] or event.get("previous_event_digest") != previous or event.get("candidate_digest") != candidate_digest:
            raise PromotionError("review chain binding differs")
        previous, latest = supplied, event.get("decision")
    if latest != "accept":
        raise PromotionError("candidate latest review must be accepted")
    return candidate, provenance, candidate_digest, previous


def _load_promotion_candidate(
    dependencies: _PromotionDependencies, candidate_id: str,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    try:
        return (
            _load_candidate(candidate_id, dependencies.candidate_root)
            if dependencies.candidate_loader is None
            else dependencies.candidate_loader(candidate_id)
        )
    except CandidateError as exc:
        raise _error("candidate semantic validation failed", exc)


def _latest_generation(brain: Path) -> tuple[int, str | None, str | None, set[str], set[str]]:
    generations = brain / "generations"
    if not generations.exists():
        return 0, None, None, set(), set()
    _real_directory(generations, "brain generations")
    ids: set[str] = set()
    semantic: set[str] = set()
    latest_id = latest_digest = None
    entries = sorted(generations.iterdir(), key=lambda value: value.name)
    for sequence, directory in enumerate(entries, 1):
        match = GENERATION_RE.fullmatch(directory.name)
        if match is None or int(match.group("sequence")) != sequence:
            raise PromotionError("brain generation sequence is invalid")
        manifest, _ = stable_json_file(directory / "manifest.json", root=directory, label="generation manifest")
        if manifest.get("generation_id") != match.group("id") or manifest.get("sequence") != sequence:
            raise PromotionError("brain generation manifest identity differs")
        manifest_digest = manifest.get("manifest_digest")
        if manifest_digest != _canonical({key: value for key, value in manifest.items() if key != "manifest_digest"}, "generation manifest"):
            raise PromotionError("brain generation manifest digest differs")
        latest_id, latest_digest = manifest.get("generation_id"), manifest_digest
        entry_root = directory / "entries"
        if entry_root.exists():
            _real_directory(entry_root, "canonical entries")
            for path in entry_root.iterdir():
                if path.suffix != ".json" or not ID_RE.fullmatch(path.stem):
                    raise PromotionError("canonical entry filename is invalid")
                value, _ = stable_json_file(path, root=entry_root, label="canonical entry", maximum=MAX_ENTRY_BYTES)
                if value.get("canonical_entry_id") != path.stem or not DIGEST_RE.fullmatch(str(value.get("semantic_key", ""))):
                    raise PromotionError("canonical entry identity differs")
                if value.get("schema_version") in {
                    "ao.lore.okf-canonical-entry.v0.3", "ao.lore.okf-canonical-entry.v0.4",
                }:
                    try:
                        validate_knowledge_payload(value["schema_version"], value)
                    except KnowledgeContractError as exc:
                        raise _error("answerable canonical entry is invalid", exc)
                elif value.get("schema_version") != "ao.lore.okf-canonical-entry.v0.2":
                    raise PromotionError("canonical entry version is unsupported")
                if value.get("canonical_entry_id") in ids or value.get("semantic_key") in semantic:
                    raise PromotionError("canonical identity collision exists")
                ids.add(value.get("canonical_entry_id")); semantic.add(value.get("semantic_key"))
    return len(entries), latest_id, latest_digest, ids, semantic


def _audit(
    dependencies: _PromotionDependencies,
    event_type: str,
    promotion_id: str,
    proposal_digest: str | None,
    status: str,
    reason_code: str = "none",
    authorization_id: str | None = None,
    authorization_digest: str | None = None,
    transaction_id: str | None = None,
    transaction_digest: str | None = None,
) -> None:
    root = dependencies.promotions_root / "audit"
    root.mkdir(mode=0o700, exist_ok=True)
    verified = validate_audit_chain(dependencies)
    entries = sorted(root.iterdir(), key=lambda value: value.name)
    if len(entries) >= MAX_AUDIT_EVENTS:
        raise PromotionError("promotion audit is exhausted")
    previous = verified[-1]["event_digest"] if verified else None
    event = {
        "schema_version": "ao.lore.promotion-audit-event.v0.1", "sequence": len(entries) + 1,
        "event_type": event_type, "promotion_id": promotion_id, "authorization_id": authorization_id, "transaction_id": transaction_id,
        "proposal_digest": proposal_digest, "authorization_digest": authorization_digest, "transaction_digest": transaction_digest,
        "previous_event_digest": previous, "recorded_at": dependencies.clock(), "status": status,
        "reason_code": reason_code,
    }
    event["event_digest"] = _canonical(event, "promotion audit event")
    _write_exclusive(root / f"{event['sequence']:06d}-{event['event_digest'][7:]}.json", event, root, "promotion audit event")


def _reject_operation(
    deps: _PromotionDependencies, operation: str, proposal: Mapping[str, Any], reason_code: str,
    message: str, authorization_digest: str | None = None,
) -> None:
    _audit(deps, f"{operation}_rejected", proposal["promotion_id"], proposal["proposal_digest"], "rejected", reason_code, authorization_digest=authorization_digest)
    raise PromotionError(message)


def validate_audit_chain(dependencies: _PromotionDependencies | None = None) -> list[dict[str, Any]]:
    """Verify and detach the complete bounded promotion audit chain."""

    deps = dependencies or _default_dependencies()
    root = deps.promotions_root / "audit"
    _real_directory(root, "promotion audit root")
    paths = sorted(root.iterdir(), key=lambda value: value.name)
    if len(paths) > MAX_AUDIT_EVENTS:
        raise PromotionError("promotion audit exceeds its event budget")
    events: list[dict[str, Any]] = []
    previous = None
    seen_digests: set[str] = set()
    per_promotion_terminal: set[str] = set()
    last_by_promotion: dict[str, str] = {}
    allowed = {
        None: {"prepare_started"},
        "prepare_started": {"prepare_succeeded", "prepare_rejected"},
        "prepare_succeeded": {"prepare_started", "apply_started", "apply_committed", "apply_rejected", "inspect", "rollback_prepared"},
        "prepare_rejected": {"prepare_started"},
        "apply_started": {"apply_committed", "apply_rejected"},
        "apply_rejected": {"apply_started", "prepare_started", "apply_rejected"},
        "apply_committed": {"inspect", "recover_started", "rollback_prepared"},
        "inspect": {"inspect", "recover_started", "rollback_prepared"},
        "recover_started": {"recover_completed", "recover_investigate"},
        "recover_completed": {"inspect", "rollback_prepared"},
        "recover_investigate": {"inspect", "recover_started"},
        "rollback_prepared": {"rollback_started", "rollback_rejected"},
        "rollback_started": {"rollback_committed", "rollback_rejected"},
        "rollback_rejected": {"rollback_started", "rollback_rejected"},
        "rollback_committed": {"inspect", "recover_started"},
    }
    status_by_type = {
        "prepare_started": "started", "prepare_succeeded": "succeeded", "prepare_rejected": "rejected",
        "apply_started": "started", "apply_committed": "committed", "apply_rejected": "rejected",
        "inspect": "succeeded", "recover_started": "started", "recover_completed": "completed",
        "recover_investigate": "investigate", "rollback_prepared": "succeeded", "rollback_started": "started",
        "rollback_committed": "committed", "rollback_rejected": "rejected",
    }
    for sequence, path in enumerate(paths, 1):
        match = AUDIT_RE.fullmatch(path.name)
        if match is None or int(match.group("sequence")) != sequence:
            raise PromotionError("promotion audit filenames are not contiguous")
        event, _ = stable_json_file(path, root=root, label="promotion audit event")
        if set(event) != AUDIT_KEYS or event.get("schema_version") != "ao.lore.promotion-audit-event.v0.1":
            raise PromotionError("promotion audit event contract differs")
        actual = _canonical({key: value for key, value in event.items() if key != "event_digest"}, "promotion audit event")
        if event.get("sequence") != sequence or event.get("event_digest") != actual or match.group("digest") != actual[7:] or event.get("previous_event_digest") != previous:
            raise PromotionError("promotion audit digest chain differs")
        if actual in seen_digests:
            raise PromotionError("promotion audit event is replayed")
        promotion_id = event.get("promotion_id")
        if not isinstance(promotion_id, str) or not ID_RE.fullmatch(promotion_id):
            raise PromotionError("promotion audit identity differs")
        event_type = event.get("event_type")
        if event_type not in allowed.get(last_by_promotion.get(promotion_id), set()) or event.get("status") != status_by_type.get(event_type):
            raise PromotionError("promotion audit semantic transition differs")
        if event_type in {"apply_committed", "rollback_committed"}:
            terminal_key = f"{promotion_id}:{event_type}"
            if terminal_key in per_promotion_terminal:
                raise PromotionError("promotion audit terminal outcome is duplicated")
            per_promotion_terminal.add(terminal_key)
        last_by_promotion[promotion_id] = event_type
        seen_digests.add(actual); previous = actual; events.append(event)
    return json.loads(json.dumps(events))


def prepare_promotion(candidate_id: str, output: Path, *, dependencies: _PromotionDependencies | None = None) -> dict[str, Any]:
    """Prepare one deterministic proposal without mutating candidate or brain."""

    deps = dependencies or _default_dependencies()
    with _promotion_lock(deps):
        output = _contained(Path(output), deps.proposals_root, "proposal output")
        candidate, provenance, candidate_digest, review_digest = (
            _load_promotion_candidate(deps, candidate_id)
        )
        warnings = candidate.get("contradiction_warnings")
        if warnings != []:
            raise PromotionError("candidate has unresolved contradiction warnings")
        brain_digest, _ = _inventory(deps.brain_root)
        sequence, prior_generation_id, prior_generation_digest, existing_ids, existing_semantic = _latest_generation(deps.brain_root)
        payload = {field: candidate[field] for field in ("concepts", "claim_mappings", "links")}
        evidence_bound = candidate["schema_version"] == "ao.lore.okf-candidate.v0.3"
        answerable = candidate["schema_version"] in {
            "ao.lore.okf-candidate.v0.2", "ao.lore.okf-candidate.v0.3",
        }
        answerable_payload = ({field: candidate[field] for field in (
            "claims_schema_version", "claims_digest", "claims", "citations_schema_version",
            "citations_digest", "citations", "knowledge_policy",
        )} if answerable else {})
        semantic_key = (
            semantic_key_v0_3(payload["concepts"], payload["claim_mappings"], payload["links"],
                              candidate["claims_digest"], candidate["citations_digest"], candidate["knowledge_policy"])
            if answerable else _canonical({"domain": "ao.lore.semantic-key.v0.2", **payload}, "semantic key")
        )
        entry_version = "v0.4" if evidence_bound else "v0.3" if answerable else "v0.2"
        canonical_entry_id = "entry-" + _canonical({"domain": f"ao.lore.canonical-entry-id.{entry_version}", "candidate_digest": candidate_digest, "semantic_key": semantic_key}, "canonical entry identity")[7:39]
        if canonical_entry_id in existing_ids or semantic_key in existing_semantic:
            raise PromotionError("canonical identity or semantic key collides")
        canonical_entry = {
            "schema_version": f"ao.lore.okf-canonical-entry.{entry_version}", "policy_version": "ao.lore.promotion-policy.v0.1",
            "canonical_entry_id": canonical_entry_id, "semantic_key": semantic_key, "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "provenance_digest": _canonical(provenance, "candidate provenance"),
            "accepted_review_head_digest": review_digest, **payload, **answerable_payload,
        }
        if evidence_bound:
            canonical_entry.update({
                "evidence_selection_digest": candidate["evidence_selection_digest"],
                "evidence_origins": json.loads(json.dumps(candidate["evidence_origins"])),
            })
        else:
            canonical_entry.update({
                "document_ir_digest": candidate["document_ir_digest"],
                "source_digest": provenance["source_digest"],
                "parser": {"parser_id": provenance["parser_id"], "parser_version": provenance["parser_version"]},
            })
        if answerable:
            try:
                validate_knowledge_payload(canonical_entry["schema_version"], canonical_entry)
            except KnowledgeContractError as exc:
                raise _error("answerable canonical entry is invalid", exc)
        entry_digest = _canonical(canonical_entry, "canonical entry")
        generation_seed = _canonical({"domain": "ao.lore.generation-id.v0.1", "sequence": sequence + 1, "entry_digest": entry_digest, "prior_generation_digest": prior_generation_digest}, "generation identity")
        generation_id = "generation-" + generation_seed[7:39]
        promotion_seed = _canonical({"domain": "ao.lore.promotion-id.v0.1", "candidate_digest": candidate_digest, "canonical_entry_digest": entry_digest, "prior_brain_inventory_digest": brain_digest, "expected_generation_id": generation_id}, "promotion identity")
        promotion_id = "promotion-" + promotion_seed[7:39]
        generation_name = f"{sequence + 1:06d}-{generation_id}"
        write_set = [f"generations/{generation_name}/entries/{canonical_entry_id}.json", f"generations/{generation_name}/manifest.json"]
        prepared = deps.clock()
        try:
            parsed = datetime.fromisoformat(prepared.removesuffix("Z") + "+00:00")
        except ValueError as exc:
            raise _error("preparation clock is invalid", exc)
        expires = (parsed + timedelta(minutes=5)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        proposal_id = "proposal-" + _canonical({"domain": "ao.lore.proposal-id.v0.1", "promotion_id": promotion_id, "prepared_at": prepared}, "proposal identity")[7:39]
        source_head = deps.source_head()
        if not isinstance(source_head, str) or not re.fullmatch(r"[0-9a-f]{40}", source_head):
            raise PromotionError("source head is invalid")
        proposal = {
            "schema_version": "ao.lore.promotion-proposal.v0.2" if evidence_bound else "ao.lore.promotion-proposal.v0.1",
            "policy_version": "ao.lore.promotion-policy.v0.1",
            "proposal_id": proposal_id, "promotion_id": promotion_id, "candidate_id": candidate_id, "candidate_digest": candidate_digest,
            "provenance_digest": canonical_entry["provenance_digest"], "accepted_review_head_digest": review_digest,
            "source_head": source_head,
            "prior_brain_inventory_digest": brain_digest, "prior_generation_id": prior_generation_id,
            "prior_generation_digest": prior_generation_digest,
            "canonical_entry": canonical_entry, "canonical_entry_digest": entry_digest, "semantic_key": semantic_key,
            "expected_generation_id": generation_id, "allowed_write_set": write_set,
            "allowed_write_set_digest": _canonical({"domain": "ao.lore.allowed-write-set.v0.1", "paths": write_set}, "allowed write set"),
            "prepared_at": prepared, "expires_at": expires,
        }
        if evidence_bound:
            proposal.update({
                "evidence_selection_digest": candidate["evidence_selection_digest"],
                "evidence_origins": json.loads(json.dumps(candidate["evidence_origins"])),
            })
        else:
            proposal.update({
                "document_ir_digest": candidate["document_ir_digest"],
                "source_digest": provenance["source_digest"],
            })
        proposal["proposal_digest"] = _canonical(proposal, "promotion proposal")
        _validate_promotion_proposal(proposal)
        _audit(deps, "prepare_started", promotion_id, proposal["proposal_digest"], "started")
        try:
            _write_exclusive(output, proposal, deps.proposals_root, "proposal output")
        except PromotionError:
            _audit(
                deps,
                "prepare_rejected",
                promotion_id,
                proposal["proposal_digest"],
                "rejected",
                "proposal_write_rejected",
            )
            raise
        _audit(deps, "prepare_succeeded", promotion_id, proposal["proposal_digest"], "succeeded")
        return json.loads(json.dumps(proposal))


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 32 or not value.endswith("Z"):
        raise PromotionError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise _error(f"{label} is invalid", exc)
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise PromotionError(f"{label} is invalid")
    return parsed


def _validate_promotion_proposal(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate exact proposal-version shape and cross-bindings without mutating state."""

    if type(value) is not dict:
        raise PromotionError("promotion proposal must be an object")
    version = value.get("schema_version")
    expected_keys = (
        PROPOSAL_V0_1_KEYS if version == "ao.lore.promotion-proposal.v0.1"
        else PROPOSAL_V0_2_KEYS if version == "ao.lore.promotion-proposal.v0.2"
        else None
    )
    if expected_keys is None:
        raise PromotionError("promotion proposal version differs")
    if set(value) != expected_keys or value.get("policy_version") != "ao.lore.promotion-policy.v0.1":
        raise PromotionError("promotion proposal contract differs")
    supplied = value.get("proposal_digest")
    actual = _canonical({key: child for key, child in value.items() if key != "proposal_digest"}, "promotion proposal")
    if supplied != actual:
        raise PromotionError("promotion proposal digest differs")
    for field in ("proposal_id", "promotion_id", "candidate_id", "expected_generation_id"):
        if not isinstance(value.get(field), str) or not ID_RE.fullmatch(value[field]):
            raise PromotionError("promotion proposal identity differs")
    for field in (
        "candidate_digest", "provenance_digest", "accepted_review_head_digest",
        "prior_brain_inventory_digest", "canonical_entry_digest", "semantic_key",
        "allowed_write_set_digest",
    ):
        if not isinstance(value.get(field), str) or not DIGEST_RE.fullmatch(value[field]):
            raise PromotionError("promotion proposal digest binding differs")
    if not isinstance(value.get("source_head"), str) or not re.fullmatch(r"[0-9a-f]{40}", value["source_head"]):
        raise PromotionError("promotion proposal source head differs")
    prepared = _parse_time(value.get("prepared_at"), "proposal prepared_at")
    expires = _parse_time(value.get("expires_at"), "proposal expires_at")
    if expires != prepared + timedelta(minutes=5):
        raise PromotionError("promotion proposal expiry differs")
    entry = value.get("canonical_entry")
    if type(entry) is not dict or _canonical(entry, "canonical entry") != value["canonical_entry_digest"]:
        raise PromotionError("promotion proposal canonical entry differs")
    if (
        entry.get("candidate_id") != value["candidate_id"]
        or entry.get("candidate_digest") != value["candidate_digest"]
        or entry.get("provenance_digest") != value["provenance_digest"]
        or entry.get("accepted_review_head_digest") != value["accepted_review_head_digest"]
        or entry.get("semantic_key") != value["semantic_key"]
    ):
        raise PromotionError("promotion proposal canonical bindings differ")
    entry_id = entry.get("canonical_entry_id")
    generation_name = f"{value['allowed_write_set'][0].split('/')[1]}" if type(value.get("allowed_write_set")) is list and value["allowed_write_set"] else ""
    expected_write_set = [
        f"generations/{generation_name}/entries/{entry_id}.json",
        f"generations/{generation_name}/manifest.json",
    ]
    if value.get("allowed_write_set") != expected_write_set:
        raise PromotionError("promotion proposal write set differs")
    expected_write_digest = _canonical({"domain": "ao.lore.allowed-write-set.v0.1", "paths": expected_write_set}, "allowed write set")
    if value["allowed_write_set_digest"] != expected_write_digest:
        raise PromotionError("promotion proposal write set digest differs")
    if not generation_name.endswith("-" + value["expected_generation_id"]):
        raise PromotionError("promotion proposal generation binding differs")
    if version == "ao.lore.promotion-proposal.v0.2":
        if entry.get("schema_version") != "ao.lore.okf-canonical-entry.v0.4":
            raise PromotionError("promotion proposal canonical version differs")
        if (
            value["evidence_selection_digest"] != entry.get("evidence_selection_digest")
            or value["evidence_origins"] != entry.get("evidence_origins")
        ):
            raise PromotionError("promotion proposal evidence binding differs")
        try:
            validate_knowledge_payload("ao.lore.okf-canonical-entry.v0.4", entry)
        except KnowledgeContractError as exc:
            raise _error("promotion proposal canonical evidence is invalid", exc)
    elif entry.get("schema_version") not in {
        "ao.lore.okf-canonical-entry.v0.2", "ao.lore.okf-canonical-entry.v0.3",
    }:
        raise PromotionError("promotion proposal canonical version differs")
    return json.loads(json.dumps(value))


def _require_unexpired_proposal(value: Mapping[str, Any], dependencies: _PromotionDependencies) -> None:
    if _parse_time(value["expires_at"], "proposal expires_at") <= _parse_time(dependencies.clock(), "current time"):
        raise PromotionError("promotion proposal is expired")


def _consumption_records(dependencies: _PromotionDependencies) -> list[dict[str, Any]]:
    root = dependencies.promotions_root / "consumed"
    if not root.exists():
        return []
    _real_directory(root, "authorization consumption root")
    records = []
    for path in sorted(root.iterdir(), key=lambda value: value.name):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}-(?:reserved|committed)\.json", path.name):
            raise PromotionError("authorization consumption filename is invalid")
        value, _ = stable_json_file(path, root=root, label="authorization consumption")
        if value.get("record_digest") != _canonical({key: child for key, child in value.items() if key != "record_digest"}, "authorization consumption"):
            raise PromotionError("authorization consumption digest differs")
        records.append(value)
    return records


def validate_authorization(
    path: Path,
    proposal: Mapping[str, Any],
    operation: str,
    *,
    dependencies: _PromotionDependencies | None = None,
    allow_exact_reserved: bool = False,
) -> tuple[dict[str, Any], str]:
    """Validate an asserted one-shot authorization without authenticating it."""

    deps = dependencies or _default_dependencies()
    value, body = stable_json_file(Path(path), root=Path(path).parent, label="promotion authorization")
    if set(value) != AUTHORIZATION_KEYS:
        raise PromotionError("promotion authorization keys differ")
    if value["schema_version"] != "ao.lore.promotion-authorization.v0.1" or value["policy_version"] != "ao.lore.promotion-policy.v0.1":
        raise PromotionError("promotion authorization version differs")
    for field in ("authorization_id", "nonce"):
        if not isinstance(value[field], str) or not ID_RE.fullmatch(value[field]):
            raise PromotionError("promotion authorization identity is invalid")
    if not isinstance(value["operator_id"], str) or not 1 <= len(value["operator_id"]) <= 256:
        raise PromotionError("promotion authorization operator assertion is invalid")
    if value["authentication_scope"] != "local_operator_assertion" or value["one_use_only"] is not True:
        raise PromotionError("promotion authorization scope is invalid")
    if any(value[field] is not False for field in FALSE_AUTHORITY_FIELDS):
        raise PromotionError("promotion authorization is overbroad or self-issued")
    if operation not in {"apply", "rollback"} or value["operation"] != operation:
        raise PromotionError("promotion authorization operation differs")
    now = _parse_time(deps.clock(), "current time")
    issued = _parse_time(value["issued_at"], "authorization issued_at")
    expires = _parse_time(value["expires_at"], "authorization expires_at")
    if issued > now or expires <= now or expires <= issued:
        raise PromotionError("promotion authorization is future-issued or expired")
    rollback = operation == "rollback"
    bindings = {
        "proposal_digest": "proposal_digest", "promotion_id": "promotion_id", "candidate_digest": "candidate_digest",
        "provenance_digest": "provenance_digest", "accepted_review_head_digest": "accepted_review_head_digest",
        "canonical_entry_digest": "canonical_entry_digest", "expected_brain_inventory_digest": "current_brain_inventory_digest" if rollback else "prior_brain_inventory_digest",
        "expected_result_generation_id": "expected_generation_id", "allowed_write_set_digest": "allowed_write_set_digest",
        "source_head": "source_head",
    }
    for authorization_field, proposal_field in bindings.items():
        if value[authorization_field] != proposal.get(proposal_field):
            raise PromotionError("promotion authorization binding differs")
    expected_prior = proposal.get("current_generation_id") if rollback else proposal.get("prior_generation_id")
    if value["expected_prior_generation_id"] != expected_prior:
        raise PromotionError("promotion authorization prior generation differs")
    if deps.source_head() != value["source_head"]:
        raise PromotionError("promotion authorization source head is stale")
    records = _consumption_records(deps)
    matching = [record for record in records if record.get("authorization_id") == value["authorization_id"] or record.get("nonce") == value["nonce"]]
    if matching:
        exact_reserved = all(
            record.get("state") in {"reserved", "committed"}
            and record.get("authorization_id") == value["authorization_id"]
            and record.get("nonce") == value["nonce"]
            and record.get("authorization_digest") == _digest(body)
            and record.get("proposal_digest") == proposal["proposal_digest"]
            for record in matching
        )
        if not allow_exact_reserved or not exact_reserved:
            raise PromotionError("promotion authorization is already consumed or duplicated")
    current_brain, _ = _inventory(deps.brain_root)
    if current_brain != value["expected_brain_inventory_digest"] and not (allow_exact_reserved and matching):
        raise PromotionError("promotion authorization brain binding is stale")
    return json.loads(json.dumps(value)), _digest(body)


def _consumption_value(
    authorization: Mapping[str, Any],
    authorization_digest: str,
    proposal: Mapping[str, Any],
    state: str,
    previous: str | None,
    dependencies: _PromotionDependencies,
) -> dict[str, Any]:
    intent = _canonical({
        "domain": "ao.lore.transaction-intent.v0.1", "authorization_digest": authorization_digest,
        "operation": authorization["operation"], "proposal_digest": proposal["proposal_digest"],
        "promotion_id": proposal["promotion_id"], "allowed_write_set_digest": proposal["allowed_write_set_digest"],
    }, "transaction intent")
    value = {
        "schema_version": "ao.lore.promotion-authorization-consumption.v0.1",
        "authorization_id": authorization["authorization_id"], "nonce": authorization["nonce"],
        "authorization_digest": authorization_digest, "operation": authorization["operation"],
        "proposal_digest": proposal["proposal_digest"], "promotion_id": proposal["promotion_id"],
        "transaction_intent_digest": intent, "allowed_write_set_digest": proposal["allowed_write_set_digest"],
        "state": state, "previous_record_digest": previous, "recorded_at": dependencies.clock(),
    }
    value["record_digest"] = _canonical(value, "authorization consumption")
    return value


def reserve_authorization(
    authorization: Mapping[str, Any], authorization_digest: str, proposal: Mapping[str, Any], *,
    dependencies: _PromotionDependencies | None = None,
) -> dict[str, Any]:
    deps = dependencies or _default_dependencies()
    with _promotion_lock(deps):
        root = deps.promotions_root / "consumed"; root.mkdir(mode=0o700, exist_ok=True)
        records = _consumption_records(deps)
        path = root / f"{authorization['authorization_id']}-reserved.json"
        expected = _consumption_value(authorization, authorization_digest, proposal, "reserved", None, deps)
        if path.exists():
            existing, _ = stable_json_file(path, root=root, label="authorization reservation")
            stable_fields = set(expected) - {"recorded_at", "record_digest"}
            if all(existing.get(field) == expected[field] for field in stable_fields):
                return existing
            raise PromotionError("authorization reservation conflicts")
        if any(record.get("authorization_id") == authorization["authorization_id"] or record.get("nonce") == authorization["nonce"] for record in records):
            raise PromotionError("authorization identity or nonce is duplicated")
        _write_exclusive(path, expected, root, "authorization reservation")
        return expected


def commit_authorization_consumption(
    authorization: Mapping[str, Any], authorization_digest: str, proposal: Mapping[str, Any], reserved: Mapping[str, Any], *,
    dependencies: _PromotionDependencies | None = None,
) -> dict[str, Any]:
    deps = dependencies or _default_dependencies()
    with _promotion_lock(deps):
        root = deps.promotions_root / "consumed"; _real_directory(root, "authorization consumption root")
        reservation_path = root / f"{authorization['authorization_id']}-reserved.json"
        stored, _ = stable_json_file(reservation_path, root=root, label="authorization reservation")
        if stored != reserved or stored.get("state") != "reserved":
            raise PromotionError("authorization reservation differs")
        committed = _consumption_value(authorization, authorization_digest, proposal, "committed", stored["record_digest"], deps)
        path = root / f"{authorization['authorization_id']}-committed.json"
        if path.exists():
            existing, _ = stable_json_file(path, root=root, label="authorization consumption")
            stable_fields = set(committed) - {"recorded_at", "record_digest"}
            if all(existing.get(field) == committed[field] for field in stable_fields):
                return existing
            raise PromotionError("authorization consumption conflicts")
        _write_exclusive(path, committed, root, "authorization consumption")
        return committed


def _reserve_unlocked(
    authorization: Mapping[str, Any], authorization_digest: str, proposal: Mapping[str, Any], deps: _PromotionDependencies
) -> dict[str, Any]:
    root = deps.promotions_root / "consumed"; root.mkdir(mode=0o700, exist_ok=True)
    records = _consumption_records(deps)
    path = root / f"{authorization['authorization_id']}-reserved.json"
    expected = _consumption_value(authorization, authorization_digest, proposal, "reserved", None, deps)
    if path.exists():
        existing, _ = stable_json_file(path, root=root, label="authorization reservation")
        stable = set(expected) - {"recorded_at", "record_digest"}
        if all(existing.get(field) == expected[field] for field in stable):
            return existing
        raise PromotionError("authorization reservation conflicts")
    if any(record.get("authorization_id") == authorization["authorization_id"] or record.get("nonce") == authorization["nonce"] for record in records):
        raise PromotionError("authorization identity or nonce is duplicated")
    _write_exclusive(path, expected, root, "authorization reservation")
    return expected


def _commit_consumption_unlocked(
    authorization: Mapping[str, Any], authorization_digest: str, proposal: Mapping[str, Any], reserved: Mapping[str, Any], deps: _PromotionDependencies
) -> dict[str, Any]:
    root = deps.promotions_root / "consumed"
    stored, _ = stable_json_file(root / f"{authorization['authorization_id']}-reserved.json", root=root, label="authorization reservation")
    if stored != reserved or stored.get("state") != "reserved":
        raise PromotionError("authorization reservation differs")
    value = _consumption_value(authorization, authorization_digest, proposal, "committed", stored["record_digest"], deps)
    path = root / f"{authorization['authorization_id']}-committed.json"
    if path.exists():
        existing, _ = stable_json_file(path, root=root, label="authorization consumption")
        stable = set(value) - {"recorded_at", "record_digest"}
        if all(existing.get(field) == value[field] for field in stable):
            return existing
        raise PromotionError("authorization consumption conflicts")
    _write_exclusive(path, value, root, "authorization consumption")
    return value


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_directory_no_replace(source: Path, destination: Path) -> None:
    """Use Linux renameat2(RENAME_NOREPLACE); never emulate overwrite safety."""

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise PromotionError("Linux no-replace publication is unavailable")
    source_parent = source.parent; destination_parent = destination.parent
    destination_root = destination_parent.parent
    source_fd = destination_root_fd = destination_fd = None
    try:
        source_fd = os.open(source_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        destination_root_fd = os.open(destination_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        destination_fd = os.open(destination_parent.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=destination_root_fd)
    except OSError as exc:
        for descriptor in (destination_fd, destination_root_fd, source_fd):
            if descriptor is not None: os.close(descriptor)
        raise _error("publication parent is invalid", exc)
    try:
        source_parent_info = os.fstat(source_fd); destination_root_info = os.fstat(destination_root_fd); destination_parent_info = os.fstat(destination_fd)
        source_info = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISDIR(source_info.st_mode): raise PromotionError("staging publication source is invalid")
        source_path_info = os.stat(source_parent, follow_symlinks=False); destination_root_path_info = os.stat(destination_root, follow_symlinks=False)
        destination_path_info = os.stat(destination_parent.name, dir_fd=destination_root_fd, follow_symlinks=False)
        if (source_parent_info.st_dev, source_parent_info.st_ino) != (source_path_info.st_dev, source_path_info.st_ino) or (destination_root_info.st_dev, destination_root_info.st_ino) != (destination_root_path_info.st_dev, destination_root_path_info.st_ino) or (destination_parent_info.st_dev, destination_parent_info.st_ino) != (destination_path_info.st_dev, destination_path_info.st_ino):
            raise PromotionError("publication parent changed")
        result = renameat2(source_fd, os.fsencode(source.name), destination_fd, os.fsencode(destination.name), 1)
        if result != 0:
            number = ctypes.get_errno()
            if number == 17: raise PromotionError("generation destination already exists")
            raise _error("generation no-replace publication failed", OSError(number, os.strerror(number)))
        published = os.stat(destination.name, dir_fd=destination_fd, follow_symlinks=False)
        if (published.st_dev, published.st_ino) != (source_info.st_dev, source_info.st_ino): raise PromotionError("published generation identity differs")
        os.fsync(destination_fd)
    finally:
        os.close(destination_fd); os.close(destination_root_fd); os.close(source_fd)


def _recovery_event(
    deps: _PromotionDependencies, transaction_id: str, intent_digest: str, promotion_id: str,
    phase: str, generation_id: str | None, classification: str, action: str,
    operation: str = "apply",
) -> dict[str, Any]:
    root = deps.promotions_root / "recovery"; root.mkdir(mode=0o700, exist_ok=True)
    prefix = f"{transaction_id}-"
    entries = sorted((path for path in root.iterdir() if path.name.startswith(prefix)), key=lambda path: path.name)
    previous = None
    for sequence, path in enumerate(entries, 1):
        value, _ = stable_json_file(path, root=root, label="promotion recovery event")
        actual = _canonical({key: child for key, child in value.items() if key != "event_digest"}, "promotion recovery event")
        if value.get("sequence") != sequence or value.get("previous_event_digest") != previous or value.get("event_digest") != actual:
            raise PromotionError("promotion recovery chain differs")
        previous = actual
    value = {
        "schema_version": "ao.lore.promotion-recovery-event.v0.1", "sequence": len(entries) + 1,
        "transaction_id": transaction_id, "transaction_intent_digest": intent_digest, "promotion_id": promotion_id,
        "operation": operation, "phase": phase, "classification": classification, "action": action,
        "generation_id": generation_id, "previous_event_digest": previous, "recorded_at": deps.clock(),
    }
    value["event_digest"] = _canonical(value, "promotion recovery event")
    _write_exclusive(root / f"{transaction_id}-{value['sequence']:06d}-{value['event_digest'][7:]}.json", value, root, "promotion recovery event")
    return value


def _recovery_events(deps: _PromotionDependencies, transaction_id: str) -> list[dict[str, Any]]:
    root = deps.promotions_root / "recovery"
    if not root.exists():
        return []
    result = []
    previous = None
    for sequence, path in enumerate(sorted(path for path in root.iterdir() if path.name.startswith(transaction_id + "-")), 1):
        value, _ = stable_json_file(path, root=root, label="promotion recovery event")
        actual = _canonical({key: child for key, child in value.items() if key != "event_digest"}, "promotion recovery event")
        if value.get("sequence") != sequence or value.get("previous_event_digest") != previous or value.get("event_digest") != actual:
            raise PromotionError("promotion recovery chain differs")
        previous = actual; result.append(value)
    return result


def _ensure_recovery_event(
    deps: _PromotionDependencies, transaction_id: str, intent_digest: str, promotion_id: str,
    phase: str, generation_id: str | None, classification: str, action: str,
    operation: str = "apply",
) -> dict[str, Any]:
    events = _recovery_events(deps, transaction_id)
    existing = [event for event in events if event.get("phase") == phase]
    if existing:
        event = existing[0]
        if event.get("transaction_intent_digest") != intent_digest or event.get("promotion_id") != promotion_id or event.get("generation_id") != generation_id or event.get("operation") != operation:
            raise PromotionError("promotion recovery phase conflicts")
        return event
    return _recovery_event(deps, transaction_id, intent_digest, promotion_id, phase, generation_id, classification, action, operation=operation)


def _retain_authorization(
    deps: _PromotionDependencies, authorization: Mapping[str, Any], authorization_digest: str, original_body: bytes
) -> Path:
    root = deps.promotions_root / "retained"; root.mkdir(mode=0o700, exist_ok=True)
    path = root / f"{authorization_digest[7:]}.json"
    if path.exists():
        existing, _ = stable_json_file(path, root=root, label="retained authorization")
        if existing != authorization:
            raise PromotionError("retained authorization conflicts")
        return path
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(original_body)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(root)
    return path


def _terminal_retry(
    proposal: Mapping[str, Any], authorization: Mapping[str, Any], authorization_digest: str, deps: _PromotionDependencies
) -> dict[str, Any] | None:
    transaction_id = "transaction-" + _canonical({
        "domain": "ao.lore.transaction-id.v0.1", "promotion_id": proposal.get("promotion_id"),
        "authorization_digest": authorization_digest, "proposal_digest": proposal.get("proposal_digest"),
    }, "transaction identity")[7:39]
    root = deps.promotions_root / "transactions"
    path = root / f"{transaction_id}.json"
    if not path.exists():
        return None
    transaction, _ = stable_json_file(path, root=root, label="promotion transaction")
    if transaction.get("authorization_digest") != authorization_digest or transaction.get("proposal_digest") != proposal.get("proposal_digest"):
        raise PromotionError("completed transaction retry differs")
    if transaction.get("transaction_digest") != _canonical({key: value for key, value in transaction.items() if key != "transaction_digest"}, "promotion transaction"):
        raise PromotionError("completed transaction digest differs")
    generation_name = proposal["allowed_write_set"][0].split("/")[1]
    generation = deps.brain_root / "generations" / generation_name
    manifest, _ = stable_json_file(generation / "manifest.json", root=generation, label="completed generation manifest")
    if manifest.get("manifest_digest") != transaction.get("result_manifest_digest"):
        raise PromotionError("completed generation evidence differs")
    consumed = deps.promotions_root / "consumed"
    committed, _ = stable_json_file(consumed / f"{authorization['authorization_id']}-committed.json", root=consumed, label="committed authorization consumption")
    if committed.get("authorization_digest") != authorization_digest or committed.get("state") != "committed":
        raise PromotionError("completed authorization evidence differs")
    recovery = deps.promotions_root / "recovery"
    terminal_events = [] if not recovery.exists() else [
        stable_json_file(candidate, root=recovery, label="terminal recovery event")[0]
        for candidate in sorted(recovery.iterdir()) if candidate.name.startswith(transaction_id + "-")
    ]
    if not terminal_events or terminal_events[-1].get("phase") != "terminal":
        raise PromotionError("completed recovery evidence differs")
    audit = deps.promotions_root / "audit"
    audit_events = [stable_json_file(candidate, root=audit, label="promotion audit event")[0] for candidate in sorted(audit.iterdir())]
    expected_audit_type = "rollback_committed" if transaction.get("operation") == "rollback" else "apply_committed"
    if not any(event.get("event_type") == expected_audit_type and event.get("transaction_id") == transaction_id for event in audit_events):
        raise PromotionError("completed audit evidence differs")
    value = {
        "schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": transaction.get("operation", "apply"), "status": "rolled_back" if transaction.get("operation") == "rollback" else "committed",
        "promotion_id": proposal["promotion_id"], "proposal_id": proposal["proposal_id"],
        "authorization_id": authorization["authorization_id"], "transaction_id": transaction_id,
        "generation_id": transaction["result_generation_id"], "result_digest": transaction["transaction_digest"],
        "reason_code": "none", "retry_safe": True, "next_command": "promotion inspect",
    }
    return value


def apply_promotion(
    proposal_path: Path, authorization_path: Path, *, dependencies: _PromotionDependencies | None = None
) -> dict[str, Any]:
    """Apply one exactly authorized additive generation against owned roots."""

    deps = dependencies or _default_dependencies()
    with _promotion_lock(deps):
        proposal, _ = stable_json_file(Path(proposal_path), root=deps.proposals_root, label="promotion proposal")
        proposal = _validate_promotion_proposal(proposal)
        raw_authorization, authorization_body = stable_json_file(Path(authorization_path), root=Path(authorization_path).parent, label="promotion authorization")
        authorization_digest = _digest(authorization_body)
        retry = _terminal_retry(proposal, raw_authorization, authorization_digest, deps)
        if retry is not None:
            return retry
        _require_unexpired_proposal(proposal, deps)
        try:
            authorization, validated_digest = validate_authorization(
                authorization_path, proposal, "apply", dependencies=deps, allow_exact_reserved=True
            )
        except PromotionError as exc:
            reason = "source_head_drift" if "source head" in str(exc) else "brain_drift" if "brain" in str(exc) else "authorization_rejected"
            _audit(deps, "apply_rejected", proposal["promotion_id"], proposal["proposal_digest"], "rejected", reason, authorization_digest=authorization_digest)
            raise
        if validated_digest != authorization_digest:
            raise PromotionError("promotion authorization changed")
        try:
            candidate, provenance, candidate_digest, review_digest = (
                _load_promotion_candidate(deps, proposal["candidate_id"])
            )
        except PromotionError:
            _reject_operation(deps, "apply", proposal, "candidate_drift", "promotion candidate became stale", authorization_digest)
        try:
            current_brain, _ = _inventory(deps.brain_root)
            sequence, prior_id, prior_digest, existing_ids, existing_semantic = _latest_generation(deps.brain_root)
        except PromotionError:
            _reject_operation(deps, "apply", proposal, "brain_drift", "promotion brain became invalid", authorization_digest)
        generation_name = proposal["allowed_write_set"][0].split("/")[1]
        destination = deps.brain_root / "generations" / generation_name
        published_already = destination.exists()
        if candidate_digest != proposal["candidate_digest"] or _canonical(provenance, "candidate provenance") != proposal["provenance_digest"] or review_digest != proposal["accepted_review_head_digest"]:
            _reject_operation(deps, "apply", proposal, "candidate_drift", "promotion candidate bindings became stale", authorization_digest)
        if not published_already and (current_brain != proposal["prior_brain_inventory_digest"] or prior_id != proposal["prior_generation_id"] or prior_digest != proposal["prior_generation_digest"]):
            _reject_operation(deps, "apply", proposal, "brain_drift", "promotion brain binding became stale", authorization_digest)
        if _canonical(proposal["canonical_entry"], "canonical entry") != proposal["canonical_entry_digest"] or (not published_already and (proposal["canonical_entry"]["canonical_entry_id"] in existing_ids or proposal["semantic_key"] in existing_semantic)):
            _reject_operation(deps, "apply", proposal, "generation_drift", "promotion generation binding became stale", authorization_digest)
        if deps.source_head() != proposal["source_head"]:
            _reject_operation(deps, "apply", proposal, "source_head_drift", "promotion source head became stale", authorization_digest)
        expected_sequence = int(generation_name[:6])
        expected_name = f"{expected_sequence:06d}-{proposal['expected_generation_id']}"
        if generation_name != expected_name:
            _reject_operation(deps, "apply", proposal, "write_set_drift", "promotion write set generation differs", authorization_digest)
        transaction_id = "transaction-" + _canonical({"domain": "ao.lore.transaction-id.v0.1", "promotion_id": proposal["promotion_id"], "authorization_digest": authorization_digest, "proposal_digest": proposal["proposal_digest"]}, "transaction identity")[7:39]
        intent_digest = _canonical({"domain": "ao.lore.transaction-intent.v0.1", "authorization_digest": authorization_digest, "operation": "apply", "proposal_digest": proposal["proposal_digest"], "promotion_id": proposal["promotion_id"], "allowed_write_set_digest": proposal["allowed_write_set_digest"]}, "transaction intent")
        _retain_authorization(deps, authorization, authorization_digest, authorization_body)
        reserved = _reserve_unlocked(authorization, authorization_digest, proposal, deps)
        deps.failpoint("after_reservation")
        _ensure_recovery_event(deps, transaction_id, intent_digest, proposal["promotion_id"], "authorized", None, "resume", "create_intent")
        deps.failpoint("after_authorized_intent")
        staging_root = deps.promotions_root / "staging"; staging_root.mkdir(mode=0o700, exist_ok=True)
        stage = staging_root / transaction_id
        entries = stage / "entries"
        entry_id = proposal["canonical_entry"]["canonical_entry_id"]
        logical_state = _canonical({"domain": "ao.lore.logical-brain-state.v0.1", "prior_generation_digest": proposal["prior_generation_digest"], "operation": "apply", "added_entry_digests": [proposal["canonical_entry_digest"]]}, "logical brain state")
        manifest = {
            "schema_version": "ao.lore.brain-generation-manifest.v0.1", "policy_version": "ao.lore.promotion-policy.v0.1",
            "sequence": expected_sequence, "generation_id": proposal["expected_generation_id"], "operation": "apply", "promotion_id": proposal["promotion_id"],
            "prior_generation": None if proposal["prior_generation_id"] is None else {"generation_id": proposal["prior_generation_id"], "manifest_digest": proposal["prior_generation_digest"]},
            "prior_brain_inventory_digest": proposal["prior_brain_inventory_digest"],
            "transition": {"kind": "add", "canonical_entry_id": entry_id, "canonical_entry_digest": proposal["canonical_entry_digest"]},
            "logical_state_digest": logical_state, "proposal_digest": proposal["proposal_digest"], "authorization_digest": authorization_digest,
            "transaction_intent_digest": intent_digest, "created_at": deps.clock(),
        }
        manifest["manifest_digest"] = _canonical(manifest, "generation manifest")
        if not published_already:
            if stage.exists():
                stored_manifest, _ = stable_json_file(stage / "manifest.json", root=stage, label="staged generation manifest")
                stored_entry, _ = stable_json_file(entries / f"{entry_id}.json", root=entries, label="staged canonical entry", maximum=MAX_ENTRY_BYTES)
                if stored_manifest != manifest or stored_entry != proposal["canonical_entry"]:
                    raise PromotionError("owned promotion staging conflicts")
            else:
                entries.mkdir(mode=0o700, parents=True)
                _write_exclusive(entries / f"{entry_id}.json", proposal["canonical_entry"], entries, "staged canonical entry")
                _write_exclusive(stage / "manifest.json", manifest, stage, "staged generation manifest")
                _fsync_directory(entries); _fsync_directory(stage); _fsync_directory(staging_root)
        _ensure_recovery_event(deps, transaction_id, intent_digest, proposal["promotion_id"], "staged", proposal["expected_generation_id"], "resume", "publish_generation")
        deps.failpoint("after_staging")
        generations = deps.brain_root / "generations"; generations.mkdir(mode=0o700, exist_ok=True)
        if not published_already:
            _publish_directory_no_replace(stage, destination)
            _fsync_directory(generations); _fsync_directory(deps.brain_root)
        published_manifest, _ = stable_json_file(destination / "manifest.json", root=destination, label="published generation manifest")
        published_entry, _ = stable_json_file(destination / "entries" / f"{entry_id}.json", root=destination, label="published canonical entry", maximum=MAX_ENTRY_BYTES)
        if published_manifest != manifest or published_entry != proposal["canonical_entry"]:
            raise PromotionError("published generation differs")
        _latest_generation(deps.brain_root)
        _ensure_recovery_event(deps, transaction_id, intent_digest, proposal["promotion_id"], "generation_published", proposal["expected_generation_id"], "complete", "complete_evidence")
        deps.failpoint("after_generation_publish")
        audit_events = validate_audit_chain(deps)
        if not any(event.get("event_type") == "apply_committed" and event.get("transaction_id") == transaction_id for event in audit_events):
            _audit(deps, "apply_committed", proposal["promotion_id"], proposal["proposal_digest"], "committed", authorization_id=authorization["authorization_id"], authorization_digest=authorization_digest, transaction_id=transaction_id)
        deps.failpoint("after_audit")
        committed_consumption = _commit_consumption_unlocked(authorization, authorization_digest, proposal, reserved, deps)
        deps.failpoint("after_consumption_commit")
        resulting_brain, _ = _inventory(deps.brain_root)
        transaction = {
            "schema_version": "ao.lore.promotion-transaction.v0.1", "transaction_id": transaction_id, "transaction_intent_digest": intent_digest,
            "promotion_id": proposal["promotion_id"], "operation": "apply", "proposal_digest": proposal["proposal_digest"],
            "authorization_id": authorization["authorization_id"], "authorization_digest": authorization_digest,
            "prior_brain_inventory_digest": proposal["prior_brain_inventory_digest"], "result_brain_inventory_digest": resulting_brain,
            "result_generation_id": proposal["expected_generation_id"], "result_manifest_digest": manifest["manifest_digest"],
            "allowed_write_set_digest": proposal["allowed_write_set_digest"], "status": "committed", "committed_at": deps.clock(),
        }
        transaction["transaction_digest"] = _canonical(transaction, "promotion transaction")
        transaction_root = deps.promotions_root / "transactions"; transaction_root.mkdir(mode=0o700, exist_ok=True)
        transaction_path = transaction_root / f"{transaction_id}.json"
        if transaction_path.exists():
            stored, _ = stable_json_file(transaction_path, root=transaction_root, label="promotion transaction")
            if stored != transaction:
                raise PromotionError("promotion transaction conflicts")
        else:
            _write_exclusive(transaction_path, transaction, transaction_root, "promotion transaction")
        deps.failpoint("after_transaction")
        _ensure_recovery_event(deps, transaction_id, intent_digest, proposal["promotion_id"], "terminal", proposal["expected_generation_id"], "no_op", "none")
        deps.failpoint("after_terminal_recovery")
        result = {
            "schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "apply", "status": "committed",
            "promotion_id": proposal["promotion_id"], "proposal_id": proposal["proposal_id"], "authorization_id": authorization["authorization_id"],
            "transaction_id": transaction_id, "generation_id": proposal["expected_generation_id"], "result_digest": transaction["transaction_digest"],
            "reason_code": "none", "retry_safe": True, "next_command": "promotion inspect",
        }
        return result


def inspect_promotion(
    promotion_id: str, *, dependencies: _PromotionDependencies | None = None
) -> dict[str, Any]:
    """Derive a read-only promotion projection from independently verified evidence."""

    deps = dependencies or _default_dependencies()
    if not isinstance(promotion_id, str) or not ID_RE.fullmatch(promotion_id):
        raise PromotionError("promotion identity is invalid")
    audit_before = validate_audit_chain(deps)
    brain_before, _ = _inventory(deps.brain_root)
    proposals: list[dict[str, Any]] = []
    _real_directory(deps.proposals_root, "promotion proposals root")
    for path in sorted(deps.proposals_root.iterdir()):
        proposal, _ = stable_json_file(path, root=deps.proposals_root, label="promotion proposal")
        if proposal.get("promotion_id") == promotion_id:
            proposals.append(_validate_promotion_proposal(proposal))
    if not proposals:
        raise PromotionError("promotion is not found")
    if len({proposal["proposal_digest"] for proposal in proposals}) != 1:
        raise PromotionError("promotion proposals conflict")
    proposal = proposals[0]
    related_audit = [event for event in audit_before if event.get("promotion_id") == promotion_id]
    if not related_audit or not any(event["event_type"] == "prepare_succeeded" and event["proposal_digest"] == proposal["proposal_digest"] for event in related_audit):
        raise PromotionError("promotion preparation audit differs")
    transaction_root = deps.promotions_root / "transactions"
    transactions: list[dict[str, Any]] = []
    if transaction_root.exists():
        for path in sorted(transaction_root.iterdir()):
            value, _ = stable_json_file(path, root=transaction_root, label="promotion transaction")
            if value.get("promotion_id") == promotion_id:
                if value.get("transaction_digest") != _canonical({key: child for key, child in value.items() if key != "transaction_digest"}, "promotion transaction"):
                    raise PromotionError("promotion transaction digest differs")
                transactions.append(value)
    operations = {value.get("operation") for value in transactions}
    if len(transactions) > 2 or (len(transactions) == 2 and operations != {"apply", "rollback"}):
        raise PromotionError("promotion has conflicting terminal transactions")
    deps.failpoint("inspect_snapshot")
    audit_after = validate_audit_chain(deps)
    brain_after, _ = _inventory(deps.brain_root)
    audit_head = audit_after[-1]["event_digest"] if audit_after else None
    if audit_before != audit_after or brain_before != brain_after:
        return {
            "schema_version": "ao.lore.promotion-inspection.v0.1", "promotion_id": promotion_id,
            "status": "recovery_pending", "operation": None, "proposal_digest": proposal["proposal_digest"],
            "authorization_id": None, "authorization_digest": None, "transaction_id": None, "transaction_digest": None,
            "generation_id": None, "generation_manifest_digest": None, "recovery_classification": "investigate",
            "audit_head_digest": audit_head, "canonical_state_valid": False, "retry_safe": True,
            "next_command": "promotion inspect",
        }
    if not transactions:
        if any(event.get("event_type") == "apply_committed" for event in related_audit):
            raise PromotionError("promotion committed transaction is missing")
        reservations = [record for record in _consumption_records(deps) if record.get("promotion_id") == promotion_id and record.get("state") == "reserved"]
        if reservations:
            reservation = reservations[0]
            return {
                "schema_version": "ao.lore.promotion-inspection.v0.1", "promotion_id": promotion_id,
                "status": "recovery_pending", "operation": reservation["operation"], "proposal_digest": proposal["proposal_digest"],
                "authorization_id": reservation["authorization_id"], "authorization_digest": reservation["authorization_digest"],
                "transaction_id": None, "transaction_digest": None, "generation_id": None, "generation_manifest_digest": None,
                "recovery_classification": "resume", "audit_head_digest": audit_head, "canonical_state_valid": True,
                "retry_safe": True, "next_command": "promotion recover",
            }
        return {
            "schema_version": "ao.lore.promotion-inspection.v0.1", "promotion_id": promotion_id,
            "status": "prepared", "operation": None, "proposal_digest": proposal["proposal_digest"],
            "authorization_id": None, "authorization_digest": None, "transaction_id": None, "transaction_digest": None,
            "generation_id": None, "generation_manifest_digest": None, "recovery_classification": "no_op",
            "audit_head_digest": audit_head, "canonical_state_valid": True, "retry_safe": True,
            "next_command": "promotion apply",
        }
    if len(transactions) == 2:
        apply_transaction = next(value for value in transactions if value["operation"] == "apply")
        rollback_transaction = next(value for value in transactions if value["operation"] == "rollback")
        if rollback_transaction.get("prior_brain_inventory_digest") != apply_transaction.get("result_brain_inventory_digest") or rollback_transaction.get("result_brain_inventory_digest") != brain_after:
            raise PromotionError("rollback transaction lineage differs")
        rollback_consumption = [record for record in _consumption_records(deps) if record.get("authorization_id") == rollback_transaction["authorization_id"] and record.get("state") == "committed"]
        if len(rollback_consumption) != 1 or rollback_consumption[0].get("authorization_digest") != rollback_transaction["authorization_digest"]:
            raise PromotionError("rollback authorization consumption differs")
        rollback_events = [event for event in related_audit if event.get("event_type") == "rollback_committed" and event.get("transaction_id") == rollback_transaction["transaction_id"]]
        if len(rollback_events) != 1:
            raise PromotionError("rollback committed audit differs")
        _, latest_id, latest_digest, _, _ = _latest_generation(deps.brain_root)
        if latest_id != rollback_transaction["result_generation_id"] or latest_digest != rollback_transaction["result_manifest_digest"]:
            raise PromotionError("rollback terminal generation differs")
        return {
            "schema_version": "ao.lore.promotion-inspection.v0.1", "promotion_id": promotion_id,
            "status": "rolled_back", "operation": "rollback", "proposal_digest": rollback_transaction["proposal_digest"],
            "authorization_id": rollback_transaction["authorization_id"], "authorization_digest": rollback_transaction["authorization_digest"],
            "transaction_id": rollback_transaction["transaction_id"], "transaction_digest": rollback_transaction["transaction_digest"],
            "generation_id": rollback_transaction["result_generation_id"], "generation_manifest_digest": rollback_transaction["result_manifest_digest"],
            "recovery_classification": "no_op", "audit_head_digest": audit_head, "canonical_state_valid": True,
            "retry_safe": True, "next_command": "promotion inspect",
        }
    transaction = transactions[0]
    if transaction.get("operation") != "apply" or transaction.get("proposal_digest") != proposal["proposal_digest"]:
        raise PromotionError("promotion terminal transaction binding differs")
    consumptions = _consumption_records(deps)
    committed = [record for record in consumptions if record.get("authorization_id") == transaction["authorization_id"] and record.get("state") == "committed"]
    if len(committed) != 1 or committed[0].get("authorization_digest") != transaction["authorization_digest"] or committed[0].get("proposal_digest") != proposal["proposal_digest"]:
        raise PromotionError("promotion authorization consumption differs")
    generation_name = proposal["allowed_write_set"][0].split("/")[1]
    generation = deps.brain_root / "generations" / generation_name
    manifest, _ = stable_json_file(generation / "manifest.json", root=generation, label="promotion generation manifest")
    entry_id = proposal["canonical_entry"]["canonical_entry_id"]
    entry, _ = stable_json_file(generation / "entries" / f"{entry_id}.json", root=generation, label="promotion canonical entry", maximum=MAX_ENTRY_BYTES)
    if (
        manifest.get("manifest_digest") != transaction["result_manifest_digest"]
        or manifest.get("promotion_id") != promotion_id
        or manifest.get("authorization_digest") != transaction["authorization_digest"]
        or manifest.get("transaction_intent_digest") != transaction["transaction_intent_digest"]
        or entry != proposal["canonical_entry"]
        or _canonical(entry, "promotion canonical entry") != proposal["canonical_entry_digest"]
    ):
        raise PromotionError("promotion canonical generation differs")
    _, latest_id, _, _, _ = _latest_generation(deps.brain_root)
    is_latest = latest_id == transaction["result_generation_id"]
    if not any(event.get("event_type") == "apply_committed" and event.get("transaction_id") == transaction["transaction_id"] and event.get("authorization_digest") == transaction["authorization_digest"] for event in related_audit):
        raise PromotionError("promotion committed audit differs")
    return {
        "schema_version": "ao.lore.promotion-inspection.v0.1", "promotion_id": promotion_id,
        "status": "committed", "operation": "apply", "proposal_digest": proposal["proposal_digest"],
        "authorization_id": transaction["authorization_id"], "authorization_digest": transaction["authorization_digest"],
        "transaction_id": transaction["transaction_id"], "transaction_digest": transaction["transaction_digest"],
        "generation_id": transaction["result_generation_id"], "generation_manifest_digest": transaction["result_manifest_digest"],
        "recovery_classification": "no_op", "audit_head_digest": audit_head, "canonical_state_valid": True,
        "retry_safe": True, "next_command": "promotion rollback" if is_latest else "promotion inspect",
    }


def _recover_promotions_locked(deps: _PromotionDependencies) -> dict[str, Any]:
    recovery_root = deps.promotions_root / "recovery"
    recovery_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    represented_intents = {
        event["transaction_intent_digest"]
        for path in recovery_root.iterdir()
        if re.fullmatch(r"transaction-[0-9a-f]{32}-[0-9]{6}-[0-9a-f]{64}\.json", path.name)
        for event in [stable_json_file(path, root=recovery_root, label="promotion recovery event")[0]]
    }
    unrepresented = [
        record for record in _consumption_records(deps)
        if record.get("state") == "reserved" and record.get("transaction_intent_digest") not in represented_intents
    ]
    if unrepresented:
        if len(unrepresented) != 1:
            return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "investigate", "promotion_id": None, "proposal_id": None, "authorization_id": None, "transaction_id": None, "generation_id": None, "result_digest": None, "reason_code": "ambiguous_pending_recovery", "retry_safe": False, "next_command": "promotion inspect"}
        record = unrepresented[0]
        transaction_id = "transaction-" + _canonical({"domain": "ao.lore.transaction-id.v0.1", "promotion_id": record["promotion_id"], "authorization_digest": record["authorization_digest"], "proposal_digest": record["proposal_digest"]}, "transaction identity")[7:39]
        _ensure_recovery_event(deps, transaction_id, record["transaction_intent_digest"], record["promotion_id"], "authorized", None, "resume", "create_intent", operation=record.get("operation", "apply"))
    transaction_ids = sorted({
        match.group(1) for path in recovery_root.iterdir()
        if (match := re.fullmatch(r"(transaction-[0-9a-f]{32})-[0-9]{6}-[0-9a-f]{64}\.json", path.name))
    })
    pending = []
    for transaction_id in transaction_ids:
        events = _recovery_events(deps, transaction_id)
        if events and events[-1].get("phase") != "terminal":
            pending.append((transaction_id, events))
    if not pending:
        return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "no_op", "promotion_id": None, "proposal_id": None, "authorization_id": None, "transaction_id": None, "generation_id": None, "result_digest": None, "reason_code": "no_pending_recovery", "retry_safe": True, "next_command": "none"}
    if len(pending) != 1:
        return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "investigate", "promotion_id": None, "proposal_id": None, "authorization_id": None, "transaction_id": None, "generation_id": None, "result_digest": None, "reason_code": "ambiguous_pending_recovery", "retry_safe": False, "next_command": "promotion inspect"}
    transaction_id, events = pending[0]
    latest = events[-1]
    promotion_id = latest["promotion_id"]
    transactions = deps.promotions_root / "transactions"
    transaction_path = transactions / f"{transaction_id}.json"
    if transaction_path.exists():
        transaction, _ = stable_json_file(transaction_path, root=transactions, label="promotion transaction")
        _ensure_recovery_event(deps, transaction_id, latest["transaction_intent_digest"], promotion_id, "terminal", transaction["result_generation_id"], "no_op", "none", operation=transaction["operation"])
        return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "recovered", "promotion_id": promotion_id, "proposal_id": None, "authorization_id": transaction["authorization_id"], "transaction_id": transaction_id, "generation_id": transaction["result_generation_id"], "result_digest": transaction["transaction_digest"], "reason_code": "completed_terminal_evidence", "retry_safe": True, "next_command": "promotion inspect"}
    proposals = []
    proposal_root = deps.promotions_root / "rollback-proposals" if latest.get("operation") == "rollback" else deps.proposals_root
    for path in proposal_root.iterdir():
        value, _ = stable_json_file(path, root=proposal_root, label="promotion proposal")
        if value.get("promotion_id") == promotion_id:
            proposals.append((path, value if latest.get("operation") == "rollback" else _validate_promotion_proposal(value)))
    if len(proposals) != 1:
        return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "investigate", "promotion_id": promotion_id, "proposal_id": None, "authorization_id": None, "transaction_id": transaction_id, "generation_id": None, "result_digest": None, "reason_code": "proposal_evidence_conflict", "retry_safe": False, "next_command": "promotion inspect"}
    proposal_path, proposal = proposals[0]
    consumption = [record for record in _consumption_records(deps) if record.get("transaction_intent_digest") == latest["transaction_intent_digest"]]
    reservations = [record for record in consumption if record.get("state") == "reserved"]
    committed = [record for record in consumption if record.get("state") == "committed"]
    if len(reservations) != 1 or len(committed) > 1 or (committed and committed[0].get("previous_record_digest") != reservations[0].get("record_digest")):
        return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "investigate", "promotion_id": promotion_id, "proposal_id": proposal["proposal_id"], "authorization_id": None, "transaction_id": transaction_id, "generation_id": None, "result_digest": None, "reason_code": "reservation_evidence_conflict", "retry_safe": False, "next_command": "promotion inspect"}
    reservation = reservations[0]
    retained = deps.promotions_root / "retained" / f"{reservation['authorization_digest'][7:]}.json"
    if not retained.exists():
        return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "investigate", "promotion_id": promotion_id, "proposal_id": proposal["proposal_id"], "authorization_id": reservation["authorization_id"], "transaction_id": transaction_id, "generation_id": None, "result_digest": None, "reason_code": "retained_authorization_missing", "retry_safe": False, "next_command": "promotion inspect"}
    stage = deps.promotions_root / "staging" / transaction_id
    if stage.exists():
        try:
            manifest, _ = stable_json_file(stage / "manifest.json", root=stage, label="owned staging manifest")
            if manifest.get("transaction_intent_digest") != latest["transaction_intent_digest"]:
                raise PromotionError("staging ownership differs")
        except PromotionError:
            return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "investigate", "promotion_id": promotion_id, "proposal_id": proposal["proposal_id"], "authorization_id": reservation["authorization_id"], "transaction_id": transaction_id, "generation_id": None, "result_digest": None, "reason_code": "staging_ownership_uncertain", "retry_safe": False, "next_command": "promotion inspect"}
    try:
        if reservation.get("operation") == "rollback":
            result = rollback_promotion(promotion_id, retained, dependencies=deps)
        else:
            result = apply_promotion(proposal_path, retained, dependencies=deps)
    except PromotionError:
        return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "recover", "status": "investigate", "promotion_id": promotion_id, "proposal_id": proposal["proposal_id"], "authorization_id": reservation["authorization_id"], "transaction_id": transaction_id, "generation_id": None, "result_digest": None, "reason_code": "recovery_binding_conflict", "retry_safe": False, "next_command": "promotion inspect"}
    return {**result, "operation": "recover", "status": "recovered", "reason_code": "resumed_exact_transaction"}


def recover_promotions(*, dependencies: _PromotionDependencies | None = None) -> dict[str, Any]:
    """Classify and resume retained promotion transactions under one global lock."""

    deps = dependencies or _default_dependencies()
    with _promotion_lock(deps):
        return _recover_promotions_locked(deps)


def _build_rollback_proposal_locked(
    promotion_id: str, *, dependencies: _PromotionDependencies | None = None
) -> dict[str, Any]:
    """Derive, retain, and audit one rollback proposal for a committed apply."""

    deps = dependencies or _default_dependencies()
    inspected = inspect_promotion(promotion_id, dependencies=deps)
    if inspected["status"] != "committed" or inspected["operation"] != "apply":
        raise PromotionError("rollback requires one committed apply")
    proposal = None
    for path in deps.proposals_root.iterdir():
        value, _ = stable_json_file(path, root=deps.proposals_root, label="promotion proposal")
        if value.get("promotion_id") == promotion_id:
            proposal = value
    if proposal is None:
        raise PromotionError("rollback source proposal is missing")
    transactions = deps.promotions_root / "transactions"
    apply_transaction, _ = stable_json_file(transactions / f"{inspected['transaction_id']}.json", root=transactions, label="apply transaction")
    sequence, current_id, current_manifest_digest, _, _ = _latest_generation(deps.brain_root)
    if current_id != inspected["generation_id"]:
        raise PromotionError("rollback target is not the current generation")
    brain_digest, _ = _inventory(deps.brain_root)
    expected_seed = _canonical({"domain": "ao.lore.rollback-generation-id.v0.1", "promotion_id": promotion_id, "current_manifest_digest": current_manifest_digest, "sequence": sequence + 1}, "rollback generation identity")
    expected_id = "generation-" + expected_seed[7:39]
    generation_name = f"{sequence + 1:06d}-{expected_id}"
    write_set = [f"generations/{generation_name}/manifest.json"]
    prepared = deps.clock(); expires = (_parse_time(prepared, "rollback prepared_at") + timedelta(minutes=5)).isoformat(timespec="seconds").replace("+00:00", "Z")
    value = {
        "schema_version": "ao.lore.rollback-proposal.v0.1", "policy_version": "ao.lore.promotion-policy.v0.1",
        "proposal_id": "rollback-proposal-" + _canonical({"domain": "ao.lore.rollback-proposal-id.v0.1", "promotion_id": promotion_id, "current_manifest_digest": current_manifest_digest}, "rollback proposal identity")[7:39],
        "promotion_id": promotion_id, "candidate_digest": proposal["candidate_digest"], "provenance_digest": proposal["provenance_digest"],
        "accepted_review_head_digest": proposal["accepted_review_head_digest"], "canonical_entry_digest": proposal["canonical_entry_digest"],
        "source_head": proposal["source_head"], "current_brain_inventory_digest": brain_digest, "current_generation_id": current_id,
        "current_generation_manifest_digest": current_manifest_digest,
        "restored_generation_id": proposal["prior_generation_id"], "restored_generation_manifest_digest": proposal["prior_generation_digest"],
        "expected_generation_id": expected_id, "allowed_write_set": write_set,
        "allowed_write_set_digest": _canonical({"domain": "ao.lore.allowed-write-set.v0.1", "paths": write_set}, "rollback write set"),
        "prepared_at": prepared, "expires_at": expires,
    }
    value["proposal_digest"] = _canonical(value, "rollback proposal")
    root = deps.promotions_root / "rollback-proposals"; root.mkdir(mode=0o700, exist_ok=True)
    path = root / f"{value['proposal_id']}.json"
    if path.exists():
        existing, _ = stable_json_file(path, root=root, label="rollback proposal")
        if existing != value:
            raise PromotionError("rollback proposal conflicts")
        return existing
    _write_exclusive(path, value, root, "rollback proposal")
    _audit(deps, "rollback_prepared", promotion_id, value["proposal_digest"], "succeeded")
    return value


def build_rollback_proposal(
    promotion_id: str, *, dependencies: _PromotionDependencies | None = None
) -> dict[str, Any]:
    """Derive and retain one rollback proposal under the global lock."""

    deps = dependencies or _default_dependencies()
    with _promotion_lock(deps):
        return _build_rollback_proposal_locked(promotion_id, dependencies=deps)


def rollback_promotion(
    promotion_id: str, authorization_path: Path, *, dependencies: _PromotionDependencies | None = None
) -> dict[str, Any]:
    """Append a separately authorized zero-entry restoration generation."""

    deps = dependencies or _default_dependencies()
    with _promotion_lock(deps):
        rollback_root = deps.promotions_root / "rollback-proposals"
        proposals = [] if not rollback_root.exists() else [stable_json_file(path, root=rollback_root, label="rollback proposal")[0] for path in rollback_root.iterdir()]
        matching = [value for value in proposals if value.get("promotion_id") == promotion_id]
        if len(matching) != 1:
            raise PromotionError("rollback proposal is missing or ambiguous")
        proposal = matching[0]
        raw, body = stable_json_file(Path(authorization_path), root=Path(authorization_path).parent, label="rollback authorization")
        auth_digest = _digest(body)
        retry = _terminal_retry(proposal, raw, auth_digest, deps)
        if retry is not None:
            return {**retry, "operation": "rollback", "status": "rolled_back", "next_command": "promotion inspect"}
        try:
            authorization, validated = validate_authorization(authorization_path, proposal, "rollback", dependencies=deps, allow_exact_reserved=True)
        except PromotionError as exc:
            reason = "source_head_drift" if "source head" in str(exc) else "brain_drift" if "brain" in str(exc) else "authorization_rejected"
            _audit(deps, "rollback_rejected", promotion_id, proposal["proposal_digest"], "rejected", reason, authorization_digest=auth_digest)
            raise
        if validated != auth_digest:
            raise PromotionError("rollback authorization changed")
        try:
            sequence, current_id, current_digest, _, _ = _latest_generation(deps.brain_root)
            brain_digest, _ = _inventory(deps.brain_root)
        except PromotionError:
            _reject_operation(deps, "rollback", proposal, "brain_drift", "rollback brain became invalid", auth_digest)
        generation_name = proposal["allowed_write_set"][0].split("/")[1]
        generations = deps.brain_root / "generations"; destination = generations / generation_name
        published_already = destination.exists()
        if not published_already and (current_id != proposal["current_generation_id"] or current_digest != proposal["current_generation_manifest_digest"] or brain_digest != proposal["current_brain_inventory_digest"]):
            _reject_operation(deps, "rollback", proposal, "brain_drift", "rollback current generation differs", auth_digest)
        if deps.source_head() != proposal["source_head"]:
            _reject_operation(deps, "rollback", proposal, "source_head_drift", "rollback source head differs", auth_digest)
        if not published_already and generation_name != f"{sequence + 1:06d}-{proposal['expected_generation_id']}":
            _reject_operation(deps, "rollback", proposal, "write_set_drift", "rollback write set generation differs", auth_digest)
        transaction_id = "transaction-" + _canonical({"domain": "ao.lore.transaction-id.v0.1", "promotion_id": promotion_id, "authorization_digest": auth_digest, "proposal_digest": proposal["proposal_digest"]}, "rollback transaction identity")[7:39]
        existing_rollbacks = [event for event in validate_audit_chain(deps) if event.get("promotion_id") == promotion_id and event.get("event_type") == "rollback_committed"]
        if any(event.get("transaction_id") != transaction_id for event in existing_rollbacks):
            raise PromotionError("promotion is already rolled back")
        intent = _canonical({"domain": "ao.lore.transaction-intent.v0.1", "authorization_digest": auth_digest, "operation": "rollback", "proposal_digest": proposal["proposal_digest"], "promotion_id": promotion_id, "allowed_write_set_digest": proposal["allowed_write_set_digest"]}, "rollback intent")
        _retain_authorization(deps, authorization, auth_digest, body)
        reserved = _reserve_unlocked(authorization, auth_digest, proposal, deps)
        deps.failpoint("rollback_after_reservation")
        _ensure_recovery_event(deps, transaction_id, intent, promotion_id, "authorized", None, "resume", "create_intent", operation="rollback")
        deps.failpoint("rollback_after_authorized_intent")
        staging = deps.promotions_root / "staging"; staging.mkdir(mode=0o700, exist_ok=True)
        stage = staging / transaction_id; entries = stage / "entries"
        restored_logical = _canonical({"domain": "ao.lore.logical-brain-state.v0.1", "restored_generation_id": proposal["restored_generation_id"], "restored_manifest_digest": proposal["restored_generation_manifest_digest"]}, "restored logical state")
        manifest = {
            "schema_version": "ao.lore.brain-generation-manifest.v0.1", "policy_version": "ao.lore.promotion-policy.v0.1", "sequence": int(generation_name[:6]),
            "generation_id": proposal["expected_generation_id"], "operation": "rollback", "promotion_id": promotion_id,
            "prior_generation": {"generation_id": proposal["current_generation_id"], "manifest_digest": proposal["current_generation_manifest_digest"]}, "prior_brain_inventory_digest": proposal["current_brain_inventory_digest"],
            "transition": {"kind": "restore", "restored_generation_id": proposal["restored_generation_id"], "restored_manifest_digest": proposal["restored_generation_manifest_digest"]},
            "logical_state_digest": restored_logical, "proposal_digest": proposal["proposal_digest"], "authorization_digest": auth_digest,
            "transaction_intent_digest": intent, "created_at": deps.clock(),
        }
        manifest["manifest_digest"] = _canonical(manifest, "rollback manifest")
        if not published_already:
            if stage.exists():
                stored, _ = stable_json_file(stage / "manifest.json", root=stage, label="rollback staging manifest")
                if stored != manifest: raise PromotionError("rollback staging conflicts")
            else:
                entries.mkdir(mode=0o700, parents=True)
                _write_exclusive(stage / "manifest.json", manifest, stage, "rollback manifest"); _fsync_directory(entries); _fsync_directory(stage); _fsync_directory(staging)
        _ensure_recovery_event(deps, transaction_id, intent, promotion_id, "staged", proposal["expected_generation_id"], "resume", "publish_generation", operation="rollback")
        deps.failpoint("rollback_after_staging")
        if not published_already:
            _publish_directory_no_replace(stage, destination); _fsync_directory(generations); _fsync_directory(deps.brain_root)
        _latest_generation(deps.brain_root)
        _ensure_recovery_event(deps, transaction_id, intent, promotion_id, "generation_published", proposal["expected_generation_id"], "complete", "complete_evidence", operation="rollback")
        deps.failpoint("rollback_after_generation_publish")
        events = validate_audit_chain(deps)
        if not any(event.get("event_type") == "rollback_started" and event.get("transaction_id") == transaction_id for event in events):
            _audit(deps, "rollback_started", promotion_id, proposal["proposal_digest"], "started", authorization_id=authorization["authorization_id"], authorization_digest=auth_digest, transaction_id=transaction_id)
        if not any(event.get("event_type") == "rollback_committed" and event.get("transaction_id") == transaction_id for event in events):
            _audit(deps, "rollback_committed", promotion_id, proposal["proposal_digest"], "committed", authorization_id=authorization["authorization_id"], authorization_digest=auth_digest, transaction_id=transaction_id)
        deps.failpoint("rollback_after_audit")
        _commit_consumption_unlocked(authorization, auth_digest, proposal, reserved, deps)
        deps.failpoint("rollback_after_consumption_commit")
        result_brain, _ = _inventory(deps.brain_root)
        transaction = {"schema_version": "ao.lore.promotion-transaction.v0.1", "transaction_id": transaction_id, "transaction_intent_digest": intent, "promotion_id": promotion_id, "operation": "rollback", "proposal_digest": proposal["proposal_digest"], "authorization_id": authorization["authorization_id"], "authorization_digest": auth_digest, "prior_brain_inventory_digest": proposal["current_brain_inventory_digest"], "result_brain_inventory_digest": result_brain, "result_generation_id": proposal["expected_generation_id"], "result_manifest_digest": manifest["manifest_digest"], "allowed_write_set_digest": proposal["allowed_write_set_digest"], "status": "committed", "committed_at": deps.clock()}
        transaction["transaction_digest"] = _canonical(transaction, "rollback transaction")
        transactions = deps.promotions_root / "transactions"; transaction_path = transactions / f"{transaction_id}.json"
        if not transaction_path.exists(): _write_exclusive(transaction_path, transaction, transactions, "rollback transaction")
        deps.failpoint("rollback_after_transaction")
        _ensure_recovery_event(deps, transaction_id, intent, promotion_id, "terminal", proposal["expected_generation_id"], "no_op", "none", operation="rollback")
        deps.failpoint("rollback_after_terminal_recovery")
        return {"schema_version": "ao.lore.promotion-operation-readback.v0.1", "operation": "rollback", "status": "rolled_back", "promotion_id": promotion_id, "proposal_id": proposal["proposal_id"], "authorization_id": authorization["authorization_id"], "transaction_id": transaction_id, "generation_id": proposal["expected_generation_id"], "result_digest": transaction["transaction_digest"], "reason_code": "none", "retry_safe": True, "next_command": "promotion inspect"}
