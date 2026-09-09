"""Read-only, fail-closed snapshots of committed canonical knowledge."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ._strict_io import ContractError, parse_strict_json
from .benchmark import BenchmarkError, canonical_digest
from .home import repository_root, runtime_home
from .knowledge_contracts import (
    KnowledgeContractError,
    origin_for_anchors,
    origin_identity,
    validate_answer_readback,
    validate_evidence_projection,
    validate_knowledge_payload,
    validate_search_readback,
    validate_snapshot_readback,
    validate_status_readback,
)
from .navigation import CoverageNavigator, NavigationError, build_requirement_plan
from .scoring import ScoringError, compute_evidence_coverage


MAX_GENERATIONS = 10_000
MAX_EFFECTIVE_ENTRIES = 10_000
MAX_RESTORE_DEPTH = 10_000
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ENTRY_BYTES = 4 * 1024 * 1024
MAX_QUERY_CHARACTERS = 4096
MAX_QUERY_TOKENS = 256
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 200
MAX_SEARCH_READBACK_BYTES = 1024 * 1024
MAX_STATUS_READBACK_BYTES = 1024 * 1024
MAX_ANSWER_READBACK_BYTES = 1024 * 1024
MAX_ANSWER_BYTES = 32 * 1024
KNOWLEDGE_MAX_NODES = 200
KNOWLEDGE_MAX_REPLANS = 8
KNOWLEDGE_MAX_SECONDS = 60
KNOWLEDGE_MAX_TOKENS = 100_000
GENERATION_RE = re.compile(r"^(?P<sequence>[0-9]{6})-(?P<id>[a-z0-9][a-z0-9._-]{0,127})$")
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
MANIFEST_KEYS = {
    "schema_version", "policy_version", "sequence", "generation_id", "operation", "promotion_id",
    "prior_generation", "prior_brain_inventory_digest", "transition", "logical_state_digest",
    "proposal_digest", "authorization_digest", "transaction_intent_digest", "created_at", "manifest_digest",
}
LEGACY_ENTRY_KEYS = {
    "schema_version", "policy_version", "canonical_entry_id", "semantic_key", "candidate_id",
    "candidate_digest", "document_ir_digest", "source_digest", "provenance_digest", "parser",
    "accepted_review_head_digest", "concepts", "claim_mappings", "links",
}


class KnowledgeReadError(ValueError):
    """Canonical state could not be proven coherent and safe to read."""


@dataclass(frozen=True)
class _KnowledgeDependencies:
    """Private test seam; public callers always use fixed repository-owned roots."""

    brain_root: Path
    coordination_lock: Path | None
    snapshot_hook: Callable[[], None] = lambda: None


@dataclass(frozen=True)
class _Generation:
    generation_id: str
    manifest_digest: str
    manifest: dict[str, Any]
    entry: dict[str, Any] | None
    entry_digest: str | None


def _default_dependencies() -> _KnowledgeDependencies:
    return _KnowledgeDependencies(
        repository_root() / "brain",
        runtime_home() / "promotions" / "promotion.lock",
    )


def _fail(message: str, cause: Exception | None = None) -> KnowledgeReadError:
    error = KnowledgeReadError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _directory_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns


def _require_identifier(value: Any, label: str) -> str:
    if type(value) is not str or IDENTIFIER_RE.fullmatch(value) is None:
        raise KnowledgeReadError(f"{label} is invalid")
    return value


def _require_digest(value: Any, label: str) -> str:
    if type(value) is not str or DIGEST_RE.fullmatch(value) is None:
        raise KnowledgeReadError(f"{label} is invalid")
    return value


def _casefold_unique(names: list[str], label: str) -> None:
    if len(names) != len(set(names)) or len(names) != len({name.casefold() for name in names}):
        raise KnowledgeReadError(f"{label} contains an alias collision")


def _open_directory(name: str | Path, *, dir_fd: int | None = None, label: str) -> tuple[int, os.stat_result]:
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=dir_fd)
        info = os.fstat(descriptor)
    except OSError as exc:
        raise _fail(f"{label} is unavailable", exc)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise KnowledgeReadError(f"{label} is not a directory")
    return descriptor, info


def _read_json_at(directory_fd: int, name: str, label: str, maximum: int) -> tuple[dict[str, Any], bytes, os.stat_result]:
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise KnowledgeReadError(f"{label} is not a bounded single-link regular file")
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise KnowledgeReadError(f"{label} changed while opening")
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
        if len(body) > maximum or _identity(opened) != _identity(after):
            raise KnowledgeReadError(f"{label} changed or exceeds its byte budget")
        return parse_strict_json(body, label), body, after
    except KnowledgeReadError:
        raise
    except (OSError, ContractError) as exc:
        raise _fail(f"{label} could not be read safely", exc)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def _shared_coordination_lock(path: Path | None) -> Iterator[None]:
    """Join promotion coordination without creating or changing runtime state."""

    if path is None:
        yield
        return
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        yield
        return
    except OSError as exc:
        raise _fail("promotion coordination lock is unavailable", exc)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != 0:
        raise KnowledgeReadError("promotion coordination lock is invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise _fail("promotion coordination lock is unavailable", exc)
    try:
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise KnowledgeReadError("promotion coordination lock changed")
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        current = os.stat(path, follow_symlinks=False)
        if _identity(opened) != _identity(current):
            raise KnowledgeReadError("promotion coordination lock changed")
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _validate_legacy_entry(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != LEGACY_ENTRY_KEYS:
        raise KnowledgeReadError("legacy canonical entry keys differ")
    if value["schema_version"] != "ao.lore.okf-canonical-entry.v0.2" or value["policy_version"] != "ao.lore.promotion-policy.v0.1":
        raise KnowledgeReadError("legacy canonical entry version differs")
    for field in ("canonical_entry_id", "candidate_id"):
        _require_identifier(value[field], field)
    for field in ("semantic_key", "candidate_digest", "document_ir_digest", "source_digest", "provenance_digest", "accepted_review_head_digest"):
        _require_digest(value[field], field)
    parser = value["parser"]
    if type(parser) is not dict or set(parser) != {"parser_id", "parser_version"}:
        raise KnowledgeReadError("legacy parser is invalid")
    _require_identifier(parser["parser_id"], "parser identity")
    if type(parser["parser_version"]) is not str or not 1 <= len(parser["parser_version"]) <= 256:
        raise KnowledgeReadError("parser version is invalid")
    for field in ("concepts", "claim_mappings", "links"):
        if type(value[field]) is not list or len(value[field]) > 10_000:
            raise KnowledgeReadError(f"legacy {field} is invalid")
    def ids(items: Any, label: str) -> None:
        if type(items) is not list or len(items) > 10_000:
            raise KnowledgeReadError(f"{label} is invalid")
        checked = [_require_identifier(item, label) for item in items]
        if len(checked) != len(set(checked)):
            raise KnowledgeReadError(f"{label} contains duplicates")
    for concept in value["concepts"]:
        allowed = (
            {"candidate_concept_id", "title", "source_block_ids", "status"},
            {"candidate_concept_id", "title", "source_block_ids", "status", "candidate_claim_ids"},
        )
        if type(concept) is not dict or set(concept) not in allowed:
            raise KnowledgeReadError("legacy concept keys differ")
        _require_identifier(concept["candidate_concept_id"], "concept identity")
        if type(concept["title"]) is not str or not 1 <= len(concept["title"]) <= 256 or concept["status"] != "proposed":
            raise KnowledgeReadError("legacy concept is invalid")
        ids(concept["source_block_ids"], "concept source blocks")
        if "candidate_claim_ids" in concept:
            ids(concept["candidate_claim_ids"], "concept claims")
    mapping_ids: set[str] = set()
    for mapping in value["claim_mappings"]:
        if type(mapping) is not dict or set(mapping) != {"claim_id", "block_ids"}:
            raise KnowledgeReadError("legacy claim mapping keys differ")
        claim_id = _require_identifier(mapping["claim_id"], "claim identity")
        if claim_id in mapping_ids:
            raise KnowledgeReadError("legacy claim identities are duplicated")
        mapping_ids.add(claim_id)
        ids(mapping["block_ids"], "claim blocks")
    for link in value["links"]:
        if type(link) is not dict or set(link) != {"source_block_id", "label", "target", "status"}:
            raise KnowledgeReadError("legacy link keys differ")
        _require_identifier(link["source_block_id"], "link source block")
        if any(type(link[field]) is not str or len(link[field]) > 256 for field in ("label", "target")) or link["status"] != "proposed":
            raise KnowledgeReadError("legacy link is invalid")
    try:
        canonical_digest(value)
    except BenchmarkError as exc:
        raise _fail("legacy canonical entry is not strict JSON", exc)
    return copy.deepcopy(dict(value))


def _validate_entry(value: dict[str, Any], entry_id: str) -> dict[str, Any]:
    version = value.get("schema_version")
    try:
        if version == "ao.lore.okf-canonical-entry.v0.2":
            result = _validate_legacy_entry(value)
        elif version in {"ao.lore.okf-canonical-entry.v0.3", "ao.lore.okf-canonical-entry.v0.4"}:
            result = validate_knowledge_payload(version, value)
        else:
            raise KnowledgeReadError("canonical entry version is unsupported")
    except KnowledgeContractError as exc:
        raise _fail("answerable canonical entry is invalid", exc)
    if result["canonical_entry_id"] != entry_id:
        raise KnowledgeReadError("canonical entry identity differs")
    return result


def _validate_manifest(value: dict[str, Any], sequence: int, generation_id: str) -> None:
    if set(value) != MANIFEST_KEYS:
        raise KnowledgeReadError("generation manifest keys differ")
    if value["schema_version"] != "ao.lore.brain-generation-manifest.v0.1" or value["policy_version"] != "ao.lore.promotion-policy.v0.1":
        raise KnowledgeReadError("generation manifest version differs")
    if type(value["sequence"]) is not int or value["sequence"] != sequence or value["generation_id"] != generation_id:
        raise KnowledgeReadError("generation manifest identity differs")
    _require_identifier(generation_id, "generation identity")
    _require_identifier(value["promotion_id"], "promotion identity")
    for field in ("prior_brain_inventory_digest", "logical_state_digest", "proposal_digest", "authorization_digest", "transaction_intent_digest", "manifest_digest"):
        _require_digest(value[field], field)
    created_at = value["created_at"]
    if type(created_at) is not str or not 1 <= len(created_at) <= 32 or not created_at.endswith("Z"):
        raise KnowledgeReadError("generation timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(created_at[:-1] + "+00:00")
    except ValueError as exc:
        raise _fail("generation timestamp is invalid", exc)
    if timestamp.tzinfo is None or timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
        raise KnowledgeReadError("generation timestamp is invalid")
    prior = value["prior_generation"]
    if prior is not None:
        if type(prior) is not dict or set(prior) != {"generation_id", "manifest_digest"}:
            raise KnowledgeReadError("prior generation binding is invalid")
        _require_identifier(prior["generation_id"], "prior generation identity")
        _require_digest(prior["manifest_digest"], "prior generation digest")
    expected = canonical_digest({key: item for key, item in value.items() if key != "manifest_digest"})
    if value["manifest_digest"] != expected:
        raise KnowledgeReadError("generation manifest digest differs")


def _read_generation(generations_fd: int, name: str, sequence: int, generation_id: str) -> tuple[_Generation, tuple[int, int, int, int]]:
    directory_fd, directory_before = _open_directory(name, dir_fd=generations_fd, label="generation directory")
    try:
        names = sorted(os.listdir(directory_fd))
        _casefold_unique(names, "generation directory")
        if names != ["entries", "manifest.json"]:
            raise KnowledgeReadError("generation directory allowlist differs")
        manifest, _, _ = _read_json_at(directory_fd, "manifest.json", "generation manifest", MAX_MANIFEST_BYTES)
        _validate_manifest(manifest, sequence, generation_id)
        entries_fd, entries_before = _open_directory("entries", dir_fd=directory_fd, label="canonical entries")
        try:
            entry_names = sorted(os.listdir(entries_fd))
            _casefold_unique(entry_names, "canonical entries")
            operation = manifest["operation"]
            transition = manifest["transition"]
            entry = None
            entry_digest = None
            if operation == "apply":
                if type(transition) is not dict or set(transition) != {"kind", "canonical_entry_id", "canonical_entry_digest"} or transition.get("kind") != "add":
                    raise KnowledgeReadError("apply transition differs")
                entry_id = _require_identifier(transition["canonical_entry_id"], "canonical entry identity")
                _require_digest(transition["canonical_entry_digest"], "canonical entry digest")
                if entry_names != [f"{entry_id}.json"]:
                    raise KnowledgeReadError("apply entry allowlist differs")
                raw_entry, _, _ = _read_json_at(entries_fd, entry_names[0], "canonical entry", MAX_ENTRY_BYTES)
                entry = _validate_entry(raw_entry, entry_id)
                entry_digest = canonical_digest(entry)
                if entry_digest != transition["canonical_entry_digest"]:
                    raise KnowledgeReadError("canonical entry digest differs")
                logical = canonical_digest({
                    "domain": "ao.lore.logical-brain-state.v0.1",
                    "prior_generation_digest": None if manifest["prior_generation"] is None else manifest["prior_generation"]["manifest_digest"],
                    "operation": "apply", "added_entry_digests": [entry_digest],
                })
            elif operation == "rollback":
                if entry_names:
                    raise KnowledgeReadError("rollback entries must be empty")
                if type(transition) is not dict or set(transition) != {"kind", "restored_generation_id", "restored_manifest_digest"} or transition.get("kind") != "restore":
                    raise KnowledgeReadError("rollback transition differs")
                restored_id, restored_digest = transition["restored_generation_id"], transition["restored_manifest_digest"]
                if (restored_id is None) != (restored_digest is None):
                    raise KnowledgeReadError("restore target binding differs")
                if restored_id is not None:
                    _require_identifier(restored_id, "restored generation identity")
                    _require_digest(restored_digest, "restored generation digest")
                logical = canonical_digest({"domain": "ao.lore.logical-brain-state.v0.1", "restored_generation_id": restored_id, "restored_manifest_digest": restored_digest})
            else:
                raise KnowledgeReadError("generation operation is unsupported")
            if manifest["logical_state_digest"] != logical:
                raise KnowledgeReadError("generation logical state differs")
            entries_after = os.fstat(entries_fd)
            if _directory_identity(entries_before) != _directory_identity(entries_after):
                raise KnowledgeReadError("canonical entries changed during snapshot")
        finally:
            os.close(entries_fd)
        directory_after = os.fstat(directory_fd)
        if _directory_identity(directory_before) != _directory_identity(directory_after):
            raise KnowledgeReadError("generation directory changed during snapshot")
        return _Generation(generation_id, manifest["manifest_digest"], copy.deepcopy(manifest), entry, entry_digest), _directory_identity(directory_before)
    finally:
        os.close(directory_fd)


def _snapshot(
    dependencies: _KnowledgeDependencies,
    *,
    _verified_active: dict[str, tuple[dict[str, Any], str]] | None = None,
    _generation_count: list[int] | None = None,
) -> dict[str, Any]:
    brain_fd, brain_before = _open_directory(dependencies.brain_root, label="canonical brain")
    try:
        generations_fd, generations_before = _open_directory("generations", dir_fd=brain_fd, label="canonical generations")
        try:
            names = sorted(os.listdir(generations_fd))
            _casefold_unique(names, "canonical generations")
            if len(names) > MAX_GENERATIONS:
                raise KnowledgeReadError("generation budget exceeded")
            generations: list[_Generation] = []
            identities: list[tuple[str, tuple[int, int, int, int]]] = []
            by_id: dict[str, int] = {}
            previous: _Generation | None = None
            for sequence, name in enumerate(names, 1):
                match = GENERATION_RE.fullmatch(name)
                if match is None or int(match.group("sequence")) != sequence:
                    raise KnowledgeReadError("generation sequence is invalid")
                generation_id = match.group("id")
                if generation_id in by_id:
                    raise KnowledgeReadError("generation identity is duplicated")
                generation, identity = _read_generation(generations_fd, name, sequence, generation_id)
                prior = generation.manifest["prior_generation"]
                if previous is None:
                    if prior is not None:
                        raise KnowledgeReadError("first generation has a prior generation")
                elif prior != {"generation_id": previous.generation_id, "manifest_digest": previous.manifest_digest}:
                    raise KnowledgeReadError("prior generation binding differs")
                by_id[generation_id] = len(generations)
                generations.append(generation)
                identities.append((name, identity))
                previous = generation

            states: list[dict[str, tuple[dict[str, Any], str]]] = []
            replay_depths: list[int] = []
            for index, generation in enumerate(generations):
                transition = generation.manifest["transition"]
                if transition["kind"] == "add":
                    state = {} if index == 0 else dict(states[index - 1])
                    depth = 1 if index == 0 else replay_depths[index - 1] + 1
                    entry_id = transition["canonical_entry_id"]
                    semantic_key = generation.entry["semantic_key"]  # type: ignore[index]
                    if entry_id in state or any(item[0]["semantic_key"] == semantic_key for item in state.values()):
                        raise KnowledgeReadError("active canonical identity collision exists")
                    state[entry_id] = (copy.deepcopy(generation.entry), generation.entry_digest)  # type: ignore[arg-type]
                else:
                    target_id = transition["restored_generation_id"]
                    if target_id is None:
                        state = {}
                        depth = 1
                    else:
                        target = by_id.get(target_id)
                        if target is None or target >= index or generations[target].manifest_digest != transition["restored_manifest_digest"]:
                            raise KnowledgeReadError("restore target binding differs")
                        state = dict(states[target])
                        depth = replay_depths[target] + 1
                if depth > MAX_RESTORE_DEPTH:
                    raise KnowledgeReadError("restore depth budget exceeded")
                states.append(dict(state))
                replay_depths.append(depth)

            active = {} if not states else states[-1]
            if _generation_count is not None:
                _generation_count.append(len(generations))
            if len(active) > MAX_EFFECTIVE_ENTRIES:
                raise KnowledgeReadError("effective entry budget exceeded")
            projections = []
            legacy = answerable_v03 = answerable_v04 = 0
            for entry_id in sorted(active):
                entry, digest = active[entry_id]
                version = entry["schema_version"]
                is_answerable = version in {"ao.lore.okf-canonical-entry.v0.3", "ao.lore.okf-canonical-entry.v0.4"}
                legacy += not is_answerable
                answerable_v03 += version == "ao.lore.okf-canonical-entry.v0.3"
                answerable_v04 += version == "ao.lore.okf-canonical-entry.v0.4"
                projection = {
                    "canonical_entry_id": entry_id, "canonical_entry_digest": digest,
                    "entry_schema_version": version,
                    "answerability": "answerable" if is_answerable else "metadata_only",
                    "claim_count": len(entry.get("claims", [])),
                }
                if version == "ao.lore.okf-canonical-entry.v0.4":
                    projection["evidence_origin_identities"] = [origin_identity(item) for item in entry["evidence_origins"]]
                projections.append(projection)

            dependencies.snapshot_hook()
            generations_after = os.fstat(generations_fd)
            if _directory_identity(generations_before) != _directory_identity(generations_after):
                raise KnowledgeReadError("canonical generations changed during snapshot")
            for name, identity in identities:
                current = os.stat(name, dir_fd=generations_fd, follow_symlinks=False)
                if _directory_identity(current) != identity:
                    raise KnowledgeReadError("generation directory was replaced during snapshot")
            if _verified_active is not None:
                _verified_active.update(copy.deepcopy(active))
        finally:
            os.close(generations_fd)
        brain_after = os.fstat(brain_fd)
        if _directory_identity(brain_before) != _directory_identity(brain_after):
            raise KnowledgeReadError("canonical brain changed during snapshot")
    finally:
        os.close(brain_fd)
    latest = generations[-1] if generations else None
    latest_id = latest.generation_id if latest else None
    latest_digest = latest.manifest_digest if latest else None
    snapshot_version = "v0.2" if answerable_v04 else "v0.1"
    snapshot_digest = canonical_digest({
        "domain": f"ao.lore.knowledge-snapshot.{snapshot_version}", "latest_generation_id": latest_id,
        "latest_generation_manifest_digest": latest_digest,
        "effective_entry_digests": [item["canonical_entry_digest"] for item in projections],
    })
    return validate_snapshot_readback({
        "snapshot_schema_version": f"ao.lore.knowledge-snapshot.{snapshot_version}",
        "latest_generation_id": latest_id, "latest_generation_manifest_digest": latest_digest,
        "snapshot_digest": snapshot_digest, "effective_entries": projections,
        "legacy_v0_2_entry_count": int(legacy), "answerable_v0_3_entry_count": int(answerable_v03),
        **({"answerable_v0_4_entry_count": int(answerable_v04)} if answerable_v04 else {}),
    })


def read_knowledge_snapshot(*, _dependencies: _KnowledgeDependencies | None = None) -> dict[str, Any]:
    """Return one descriptor-coherent projection of committed canonical state."""

    dependencies = _default_dependencies() if _dependencies is None else _dependencies
    try:
        with _shared_coordination_lock(dependencies.coordination_lock):
            return _snapshot(dependencies)
    except KnowledgeReadError:
        raise
    except (OSError, ContractError, BenchmarkError) as exc:
        raise _fail("canonical knowledge snapshot was rejected", exc)


def _normalize_query(query: Any) -> tuple[str, tuple[str, ...], frozenset[str]]:
    if type(query) is not str or len(query) > MAX_QUERY_CHARACTERS:
        raise KnowledgeReadError("knowledge query is invalid")
    try:
        query.encode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise _fail("knowledge query is invalid", exc)
    normalized, tokens, token_set = _normalized_text(query)
    if len(tokens) > MAX_QUERY_TOKENS:
        raise KnowledgeReadError("knowledge query token budget exceeded")
    return normalized, tokens, token_set


def _normalized_text(value: str) -> tuple[str, tuple[str, ...], frozenset[str]]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    characters: list[str] = []
    separated = False
    for character in normalized:
        if character.isalnum():
            characters.append(character)
            separated = False
        elif characters and not separated:
            characters.append(" ")
            separated = True
    normalized = "".join(characters).strip(" ")
    tokens = tuple(normalized.split(" ")) if normalized else ()
    return normalized, tokens, frozenset(tokens)


def _evidence_id(
    snapshot: Mapping[str, Any], entry_id: str, entry_digest: str, claim_id: str, citation_id: str,
) -> str:
    digest = canonical_digest({
        "domain": "ao.lore.evidence-id.v0.1",
        "generation_id": snapshot["latest_generation_id"],
        "generation_manifest_digest": snapshot["latest_generation_manifest_digest"],
        "canonical_entry_id": entry_id,
        "canonical_entry_digest": entry_digest,
        "claim_id": claim_id,
        "citation_id": citation_id,
    })
    return "evidence-" + digest[7:39]


def _search_verified_snapshot(
    snapshot: Mapping[str, Any],
    active: Mapping[str, tuple[dict[str, Any], str]],
    normalized_query: str,
    query_tokens: frozenset[str],
    limit: int,
) -> dict[str, Any]:
    ranked: list[tuple[tuple[int, int, int, int, str, str], dict[str, Any]]] = []
    metadata: list[dict[str, Any]] = []
    claim_identities: set[str] = set()
    for entry_id in sorted(active):
        entry, entry_digest = active[entry_id]
        if entry["schema_version"] == "ao.lore.okf-canonical-entry.v0.2":
            for concept in entry["concepts"]:
                title, _, title_tokens = _normalized_text(concept["title"])
                if query_tokens.intersection(title_tokens) or (normalized_query and normalized_query in title):
                    metadata.append({
                        "classification": "metadata_only",
                        "canonical_entry_id": entry_id,
                        "concept_id": concept["candidate_concept_id"],
                        "concept_title": concept["title"],
                    })
            continue
        if entry["knowledge_policy"]["sensitivity"] == "restricted":
            continue
        citations = {item["citation_id"]: item for item in entry["citations"]}
        for claim in entry["claims"]:
            claim_id = claim["claim_id"]
            if claim_id in claim_identities:
                raise KnowledgeReadError("active claim identity is duplicated")
            claim_identities.add(claim_id)
            citation = citations[claim["citation_id"]]
            claim_text, _, claim_tokens = _normalized_text(claim["text"])
            _, _, citation_tokens = _normalized_text(citation["render_text"])
            claim_matches = len(query_tokens.intersection(claim_tokens))
            citation_matches = len(query_tokens.intersection(citation_tokens))
            exact = bool(normalized_query) and normalized_query == claim_text
            substring = bool(normalized_query) and normalized_query in claim_text
            if not claim_matches and not citation_matches and not substring:
                continue
            score = {
                "claim_exact_match": exact,
                "claim_substring_match": substring,
                "matched_claim_token_count": claim_matches,
                "matched_citation_token_count": citation_matches,
            }
            hit = {
                "classification": "answerable",
                "evidence_id": _evidence_id(snapshot, entry_id, entry_digest, claim_id, citation["citation_id"]),
                "canonical_entry_id": entry_id,
                "claim_id": claim_id,
                "citation_id": citation["citation_id"],
                "claim_text": claim["text"],
                "citation": citation["render_text"],
                "source_ref": f"canonical:{entry_id}#{citation['citation_id']}",
                "score_components": score,
            }
            if entry["schema_version"] == "ao.lore.okf-canonical-entry.v0.4":
                origin = origin_for_anchors(entry["evidence_origins"], claim["source_block_ids"])
                hit["origin_identity"] = origin_identity(origin)
            rank = (-int(exact), -int(substring), -claim_matches, -citation_matches, entry_id, claim_id)
            ranked.append((rank, hit))
    ranked.sort(key=lambda item: item[0])
    metadata.sort(key=lambda item: (item["canonical_entry_id"], item["concept_id"]))
    hits = [item[1] for item in ranked]
    hits.extend(metadata)
    v04_count = snapshot.get("answerable_v0_4_entry_count", 0)
    return {
        "schema_version": "ao.lore.knowledge-search-readback.v0.2" if v04_count else "ao.lore.knowledge-search-readback.v0.1",
        "status": "completed",
        "snapshot_digest": snapshot["snapshot_digest"],
        "query_digest": canonical_digest({
            "domain": "ao.lore.knowledge-query.v0.1",
            "normalized_query": normalized_query,
        }),
        "hits": hits[:limit],
        "legacy_v0_2_entry_count": snapshot["legacy_v0_2_entry_count"],
        "answerable_v0_3_entry_count": snapshot["answerable_v0_3_entry_count"],
        **({"answerable_v0_4_entry_count": v04_count} if v04_count else {}),
    }


def search_knowledge(
    query: str,
    *,
    limit: int = DEFAULT_SEARCH_LIMIT,
    _dependencies: _KnowledgeDependencies | None = None,
) -> dict[str, Any]:
    """Search one verified active snapshot using bounded deterministic lexical matching."""

    normalized, _, token_set = _normalize_query(query)
    if type(limit) is not int or not 1 <= limit <= MAX_SEARCH_LIMIT:
        raise KnowledgeReadError("knowledge search limit is invalid")
    dependencies = _default_dependencies() if _dependencies is None else _dependencies
    active: dict[str, tuple[dict[str, Any], str]] = {}
    try:
        with _shared_coordination_lock(dependencies.coordination_lock):
            snapshot = _snapshot(dependencies, _verified_active=active)
        result = _search_verified_snapshot(snapshot, active, normalized, token_set, limit)
        checked = validate_search_readback(result)
        encoded = json.dumps(
            checked, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > MAX_SEARCH_READBACK_BYTES:
            raise KnowledgeReadError("knowledge search readback budget exceeded")
        return checked
    except KnowledgeReadError:
        raise
    except (OSError, ContractError, BenchmarkError, KnowledgeContractError) as exc:
        raise _fail("canonical knowledge search was rejected", exc)


def _bounded_readback(value: dict[str, Any], maximum: int, label: str) -> dict[str, Any]:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise KnowledgeReadError(f"{label} budget exceeded")
    return value


def knowledge_status(*, _dependencies: _KnowledgeDependencies | None = None) -> dict[str, Any]:
    """Report compatibility and answerability for the fixed canonical roots."""

    dependencies = _default_dependencies() if _dependencies is None else _dependencies
    generation_count: list[int] = []
    try:
        with _shared_coordination_lock(dependencies.coordination_lock):
            snapshot = _snapshot(dependencies, _generation_count=generation_count)
        legacy = snapshot["legacy_v0_2_entry_count"]
        v04_count = snapshot.get("answerable_v0_4_entry_count", 0)
        answerable = snapshot["answerable_v0_3_entry_count"] + v04_count
        effective = snapshot["effective_entries"]
        status = (
            "empty" if not effective else "legacy_only" if not answerable else
            "mixed_version_partial" if legacy else "fully_answerable"
        )
        report = validate_status_readback({
            "schema_version": "ao.lore.knowledge-status-readback.v0.2" if v04_count else "ao.lore.knowledge-status-readback.v0.1",
            "status": "completed",
            "snapshot_digest": snapshot["snapshot_digest"],
            "latest_generation_id": snapshot["latest_generation_id"],
            "latest_generation_manifest_digest": snapshot["latest_generation_manifest_digest"],
            "generation_count": generation_count[0],
            "effective_entry_count": len(effective),
            "answerable_v0_3_entry_count": snapshot["answerable_v0_3_entry_count"],
            "legacy_v0_2_entry_count": legacy,
            "claim_count": sum(item["claim_count"] for item in effective),
            "answerability_status": status,
            **({"answerable_v0_4_entry_count": v04_count} if v04_count else {}),
        })
        return _bounded_readback(report, MAX_STATUS_READBACK_BYTES, "knowledge status readback")
    except KnowledgeReadError:
        raise
    except (OSError, ContractError, BenchmarkError, KnowledgeContractError) as exc:
        raise _fail("canonical knowledge status was rejected", exc)


def _knowledge_requirement_plan(query_digest: str) -> dict[str, Any]:
    return build_requirement_plan(
        query_digest,
        "canonical-knowledge-v1",
        [{
            "id": "requirement-query",
            "criterion": "support the canonical knowledge query",
            "importance_weight": 1,
            "evidence_type": "canonical-claim",
            "lifecycle_condition": "active-and-current",
            "trust_tier": "canonical-reviewed",
            "citation_required": True,
            "provenance_required": True,
            "mandatory": True,
        }],
        {
            "max_depth": KNOWLEDGE_MAX_NODES,
            "max_nodes": KNOWLEDGE_MAX_NODES,
            "max_tokens": KNOWLEDGE_MAX_TOKENS,
            "max_seconds": KNOWLEDGE_MAX_SECONDS,
            "max_replans": KNOWLEDGE_MAX_REPLANS,
        },
    )


def _knowledge_coverage_profile() -> dict[str, Any]:
    return {
        "profile_id": "canonical-knowledge-v1",
        "target": 1.0,
        "satisfaction_values": {
            "satisfied": 1.0,
            "partially_satisfied": 0.5,
            "missing": 0.0,
            "contradictory": 0.0,
            "stale_only": 0.0,
            "disqualified": 0.0,
        },
    }


def _empty_coverage(plan: Mapping[str, Any]) -> dict[str, Any]:
    return compute_evidence_coverage(
        plan["query_digest"],
        plan["requirements"],
        _knowledge_coverage_profile(),
        {
            "provenance_present": True,
            "citations_present": True,
            "freshness_met": True,
            "trust_met": True,
            "no_hard_contradiction": True,
            "deterministic_validation_passed": True,
        },
        budget_exhausted=True,
    )


def _knowledge_now(now: datetime | None) -> datetime:
    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise KnowledgeReadError("knowledge projection clock is invalid")
    return current.astimezone(timezone.utc)


def _claims_contradict(claims: list[str]) -> bool:
    """Detect only an exact deterministic positive/negative lexical pair."""

    normalized = [_normalized_text(text)[0].split(" ") for text in claims]
    positive = {tuple(token for token in tokens if token != "not") for tokens in normalized}
    negative = {tuple(token for token in tokens if token != "not") for tokens in normalized if "not" in tokens}
    return bool(negative.intersection(positive)) and any("not" not in tokens for tokens in normalized)


def project_knowledge_evidence(
    snapshot: Mapping[str, Any],
    search_readback: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Project verified active search hits through fixed coverage navigation.

    ``_verified_active_entries`` is an in-memory consumer binding produced by the
    snapshot reader's private orchestration path. It is never returned.
    """

    try:
        if type(snapshot) is not dict or type(search_readback) is not dict:
            raise KnowledgeReadError("knowledge projection inputs are invalid")
        checked_search = validate_search_readback(search_readback)
        if checked_search["snapshot_digest"] != snapshot.get("snapshot_digest"):
            raise KnowledgeReadError("knowledge search snapshot binding differs")
        active = snapshot.get("_verified_active_entries")
        if type(active) is not dict:
            raise KnowledgeReadError("verified active entry context is absent")
        effective = snapshot.get("effective_entries")
        if type(effective) is not list:
            raise KnowledgeReadError("knowledge snapshot entries are invalid")
        effective_index = {
            item["canonical_entry_id"]: item
            for item in effective
            if type(item) is dict and type(item.get("canonical_entry_id")) is str
        }
        plan = _knowledge_requirement_plan(checked_search["query_digest"])
        answerable = [hit for hit in checked_search["hits"] if hit["classification"] == "answerable"]
        if len(answerable) != len(checked_search["hits"]):
            raise KnowledgeReadError("non-answerable search hits cannot enter navigation")
        current = _knowledge_now(now)
        claims = [hit["claim_text"] for hit in answerable]
        hard_contradiction = _claims_contradict(claims)
        candidates: list[tuple[dict[str, Any], dict[str, Any], int]] = []
        for hit in answerable:
            entry_id = hit["canonical_entry_id"]
            entry = active.get(entry_id)
            summary = effective_index.get(entry_id)
            if type(entry) is not dict or type(summary) is not dict:
                raise KnowledgeReadError("search hit is not bound to an active canonical entry")
            entry_digest = canonical_digest(entry)
            entry_version = entry.get("schema_version")
            if (summary.get("canonical_entry_digest") != entry_digest
                    or summary.get("entry_schema_version") != entry_version
                    or entry_version not in {"ao.lore.okf-canonical-entry.v0.3", "ao.lore.okf-canonical-entry.v0.4"}):
                raise KnowledgeReadError("active canonical entry binding differs")
            claim_matches = [item for item in entry.get("claims", []) if item.get("claim_id") == hit["claim_id"]]
            citation_matches = [item for item in entry.get("citations", []) if item.get("citation_id") == hit["citation_id"]]
            if len(claim_matches) != 1 or len(citation_matches) != 1:
                raise KnowledgeReadError("claim or citation binding is absent")
            claim, citation = claim_matches[0], citation_matches[0]
            expected_evidence_id = _evidence_id(
                snapshot, entry_id, entry_digest, claim["claim_id"], citation["citation_id"],
            )
            if (claim.get("citation_id") != citation.get("citation_id")
                    or hit["evidence_id"] != expected_evidence_id
                    or hit["claim_text"] != claim.get("text")
                    or hit["citation"] != citation.get("render_text")
                    or hit["source_ref"] != f"canonical:{entry_id}#{citation['citation_id']}"):
                raise KnowledgeReadError("search evidence binding differs")
            stale_after = entry.get("knowledge_policy", {}).get("stale_after")
            freshness_met = stale_after is None or current <= datetime.fromisoformat(stale_after[:-1] + "+00:00")
            trust_met = entry.get("knowledge_policy", {}).get("sensitivity") in {"public", "internal"}
            projection_body = {
                "schema_version": "ao.lore.knowledge-evidence-projection.v0.2" if entry_version.endswith("v0.4") else "ao.lore.knowledge-evidence-projection.v0.1",
                "evidence_id": expected_evidence_id,
                "requirement_ids": ["requirement-query"],
                "supported_claims": [{"claim_id": claim["claim_id"], "text": claim["text"]}],
                "source_ref": hit["source_ref"],
                "citation": citation["render_text"],
                "provenance": {
                    "generation_id": snapshot.get("latest_generation_id"),
                    "generation_manifest_digest": snapshot.get("latest_generation_manifest_digest"),
                    "canonical_entry_id": entry_id,
                    "canonical_entry_digest": entry_digest,
                    "candidate_id": entry.get("candidate_id"),
                    "provenance_digest": entry.get("provenance_digest"),
                    "entry_schema_version": entry.get("schema_version"),
                },
                "freshness_met": freshness_met,
                "trust_met": trust_met,
                "verified": True,
            }
            if entry_version.endswith("v0.4"):
                origin = origin_for_anchors(entry["evidence_origins"], claim["source_block_ids"])
                if hit.get("origin_identity") != origin_identity(origin):
                    raise KnowledgeReadError("search origin binding differs")
                projection_body["origin"] = origin
                projection_body["provenance"]["evidence_selection_digest"] = entry["evidence_selection_digest"]
            else:
                parser = entry.get("parser")
                if type(parser) is not dict:
                    raise KnowledgeReadError("canonical parser provenance is absent")
                projection_body["source_digest"] = entry.get("source_digest")
                projection_body["provenance"]["parser_id"] = parser.get("parser_id")
                projection_body["provenance"]["parser_version"] = parser.get("parser_version")
            projection = validate_evidence_projection(projection_body)
            status = "contradictory" if hard_contradiction else (
                "stale_only" if not freshness_met else ("disqualified" if not trust_met else "satisfied")
            )
            candidates.append((projection, {
                "requirement_id": "requirement-query",
                "status": status,
                "evidence_ref": expected_evidence_id,
                "provenance_present": True,
                "citation_present": True,
                "freshness_met": freshness_met,
                "trust_met": trust_met,
                "hard_contradiction": hard_contradiction,
            }, max(1, len(_normalized_text(claim["text"])[1]))))
        navigator = CoverageNavigator(plan, _knowledge_coverage_profile())
        report: dict[str, Any] | None = None
        projections: list[dict[str, Any]] = []
        for projection, event, tokens in candidates:
            if navigator.should_stop:
                break
            trace = navigator.trace()
            remaining = trace["remaining_budgets"]
            if remaining["nodes"] <= 0 or remaining["seconds"] <= 0 or tokens > remaining["tokens"]:
                break
            report = navigator.visit(projection["evidence_id"], [event], estimated_tokens=tokens, estimated_seconds=1)
            projections.append(projection)
        if report is None:
            report = _empty_coverage(plan)
        elif not navigator.should_stop and candidates:
            trace = navigator.trace()
            if trace["remaining_budgets"]["replans"] > 0:
                navigator.replan([{"node_id": candidates[-1][0]["evidence_id"], "targets": ["requirement-query"]}])
        return {
            "query_digest": checked_search["query_digest"],
            "requirement_plan": copy.deepcopy(plan),
            "coverage_report": copy.deepcopy(report),
            "evidence_projection": copy.deepcopy(projections),
            "downstream_state": "complete" if report["decision"] == "answer" else "bounded_incomplete",
        }
    except KnowledgeReadError:
        raise
    except (BenchmarkError, KnowledgeContractError, NavigationError, ScoringError, KeyError, TypeError, ValueError) as exc:
        raise _fail("canonical knowledge evidence projection was rejected", exc)


def _answer_outcome(
    *,
    status: str,
    snapshot_digest: str,
    coverage_report_digest: str,
    legacy_count: int,
    reason_code: str,
    answer: str = "",
    claim_ids: list[str] | None = None,
    evidence_ids: list[str] | None = None,
    citations: list[Any] | None = None,
    answerable_v0_4_count: int = 0,
) -> dict[str, Any]:
    checked = validate_answer_readback({
        "schema_version": "ao.lore.knowledge-answer-readback.v0.2" if answerable_v0_4_count else "ao.lore.knowledge-answer-readback.v0.1",
        "status": status,
        "snapshot_digest": snapshot_digest,
        "coverage_report_digest": coverage_report_digest,
        "claim_ids": [] if claim_ids is None else claim_ids,
        "evidence_ids": [] if evidence_ids is None else evidence_ids,
        "citations": [] if citations is None else citations,
        "legacy_v0_2_entry_count": legacy_count,
        "answer": answer,
        "reason_code": reason_code,
        **({"answerable_v0_4_entry_count": answerable_v0_4_count} if answerable_v0_4_count else {}),
    })
    return _bounded_readback(checked, MAX_ANSWER_READBACK_BYTES, "knowledge answer readback")


def _investigation_reason(error: KnowledgeReadError) -> str:
    message = str(error)
    if "unsupported" in message or "version" in message:
        return "unsupported_active_version"
    if "changed" in message or "replaced" in message or "unstable" in message:
        return "snapshot_drift"
    return "canonical_state_invalid"


def answer_knowledge(
    query: str,
    *,
    _dependencies: _KnowledgeDependencies | None = None,
) -> dict[str, Any]:
    """Answer from one coherent fixed-root snapshot and its verified evidence ledger."""

    normalized, _, tokens = _normalize_query(query)
    query_digest = canonical_digest({
        "domain": "ao.lore.knowledge-query.v0.1", "normalized_query": normalized,
    })
    invalid_digest = canonical_digest({"domain": "ao.lore.invalid-knowledge-state.v0.1"})
    empty_coverage = canonical_digest({"domain": "ao.lore.unavailable-coverage.v0.1"})
    snapshot_digest = invalid_digest
    legacy_count = 0
    answerable_v0_4_count = 0
    dependencies = _default_dependencies() if _dependencies is None else _dependencies
    try:
        active: dict[str, tuple[dict[str, Any], str]] = {}
        with _shared_coordination_lock(dependencies.coordination_lock):
            snapshot = _snapshot(dependencies, _verified_active=active)
        snapshot_digest = snapshot["snapshot_digest"]
        legacy_count = snapshot["legacy_v0_2_entry_count"]
        answerable_v0_4_count = snapshot.get("answerable_v0_4_entry_count", 0)
        answerable_count = snapshot["answerable_v0_3_entry_count"] + answerable_v0_4_count
        if answerable_count == 0:
            reason = "legacy_metadata_only" if legacy_count else "no_answerable_entries"
            coverage = _empty_coverage(_knowledge_requirement_plan(query_digest))
            return _answer_outcome(
                status="refuse", snapshot_digest=snapshot_digest,
                coverage_report_digest=canonical_digest(coverage), legacy_count=legacy_count,
                reason_code=reason,
                answerable_v0_4_count=answerable_v0_4_count,
            )

        search = _search_verified_snapshot(snapshot, active, normalized, tokens, MAX_SEARCH_LIMIT)
        search["hits"] = [item for item in search["hits"] if item["classification"] == "answerable"]
        search = validate_search_readback(search)
        private_snapshot = copy.deepcopy(snapshot)
        private_snapshot["_verified_active_entries"] = {
            entry_id: copy.deepcopy(bound[0]) for entry_id, bound in active.items()
        }
        navigation = project_knowledge_evidence(private_snapshot, search)
        coverage = navigation["coverage_report"]
        coverage_digest = canonical_digest(coverage)
        ledger = navigation["evidence_projection"]
        if not ledger:
            return _answer_outcome(
                status="refuse", snapshot_digest=snapshot_digest,
                coverage_report_digest=coverage_digest, legacy_count=legacy_count,
                reason_code="coverage_insufficient",
                answerable_v0_4_count=answerable_v0_4_count,
            )
        gates = coverage["gates"]
        if not gates["no_hard_contradiction"]:
            return _answer_outcome(
                status="refuse", snapshot_digest=snapshot_digest,
                coverage_report_digest=coverage_digest, legacy_count=legacy_count,
                reason_code="hard_contradiction",
                answerable_v0_4_count=answerable_v0_4_count,
            )
        if not gates["trust_met"]:
            return _answer_outcome(
                status="refuse", snapshot_digest=snapshot_digest,
                coverage_report_digest=coverage_digest, legacy_count=legacy_count,
                reason_code="trust_gate_failed",
                answerable_v0_4_count=answerable_v0_4_count,
            )

        public_decision = coverage["decision"]
        reason_code = "none"
        if public_decision == "replan" or not gates["freshness_met"]:
            coverage = copy.deepcopy(coverage)
            coverage["decision"] = "partial"
            coverage["reason"] = "bounded canonical evidence did not pass every answer gate"
            public_decision = "partial"
            reason_code = "freshness_gate_failed" if not gates["freshness_met"] else "coverage_insufficient"
            coverage_digest = canonical_digest(coverage)
        elif public_decision == "partial":
            reason_code = "coverage_insufficient"
        elif public_decision != "answer":
            return _answer_outcome(
                status="refuse", snapshot_digest=snapshot_digest,
                coverage_report_digest=coverage_digest, legacy_count=legacy_count,
                reason_code="coverage_insufficient",
                answerable_v0_4_count=answerable_v0_4_count,
            )

        from .synthesis import DeterministicSynthesizer, synthesize_from_evidence

        synthesis_ledger = []
        for item in ledger:
            detached = copy.deepcopy(item)
            detached.pop("schema_version", None)
            if "origin" in detached:
                detached["source_digest"] = detached.pop("origin")["evidence_digest"]
            synthesis_ledger.append(detached)
        synthesized = synthesize_from_evidence(
            normalized,
            synthesis_ledger,
            coverage,
            "text",
            {"required": True},
            {"include_limitations": public_decision == "partial"},
            DeterministicSynthesizer(),
        )
        claim_ids = synthesized["claim_ids"]
        selected = {
            claim["claim_id"]: item
            for item in ledger for claim in item["supported_claims"]
        }
        evidence_ids = sorted({selected[claim_id]["evidence_id"] for claim_id in claim_ids})
        citations = []
        for claim_id in claim_ids:
            item = selected[claim_id]
            citation_value: Any = item["source_ref"]
            if item["schema_version"] == "ao.lore.knowledge-evidence-projection.v0.2":
                citation_value = {"source_ref": item["source_ref"], "origin_identity": origin_identity(item["origin"])}
            if citation_value not in citations:
                citations.append(citation_value)
        citations.sort(key=repr)
        answer = synthesized["answer"]
        if type(answer) is not str or len(answer.encode("utf-8")) > MAX_ANSWER_BYTES:
            raise KnowledgeReadError("knowledge answer budget exceeded")
        return _answer_outcome(
            status=public_decision, snapshot_digest=snapshot_digest,
            coverage_report_digest=coverage_digest, legacy_count=legacy_count,
            reason_code=reason_code, answer=answer, claim_ids=claim_ids,
            evidence_ids=evidence_ids, citations=citations,
            answerable_v0_4_count=answerable_v0_4_count,
        )
    except KnowledgeReadError as exc:
        return _answer_outcome(
            status="investigate", snapshot_digest=snapshot_digest,
            coverage_report_digest=empty_coverage, legacy_count=legacy_count,
            reason_code=_investigation_reason(exc),
            answerable_v0_4_count=answerable_v0_4_count,
        )
    except (OSError, ContractError, BenchmarkError, KnowledgeContractError, NavigationError, ScoringError, ValueError) as exc:
        error = _fail("canonical knowledge answer was rejected", exc)
        return _answer_outcome(
            status="investigate", snapshot_digest=snapshot_digest,
            coverage_report_digest=empty_coverage, legacy_count=legacy_count,
            reason_code=_investigation_reason(error),
            answerable_v0_4_count=answerable_v0_4_count,
        )
