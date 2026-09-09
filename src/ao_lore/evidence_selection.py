"""Pure governed evidence-selection preparation."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import subprocess
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from ._strict_io import ContractError
from .benchmark import canonical_digest
from .candidates import CandidateError, load_verified_candidate, persist_candidate
from .evidence_selection_contracts import (
    validate_selection_authorization,
    validate_selection_consumption,
    validate_selection_inspection,
    validate_selection_proposal,
    validate_selection_transaction,
)
from .home import repository_root
from .workspace_documents import WorkspaceDocumentDependencies, load_workspace_documents
from .workspace_query import WorkspaceQuerySnapshot
from .workspace_registry import (
    WorkspaceRegistryDependencies,
    WorkspaceRegistrySnapshot,
    load_workspace_registry,
    select_workspace,
)


_SOURCE_HEAD = re.compile(r"^[0-9a-f]{40}$")
SELECTION_APPLY_FAILPOINTS = (
    "after_selection_reservation",
    "after_selection_candidate",
    "after_selection_consumption",
    "after_selection_transaction",
    "after_selection_completion",
)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class EvidenceSelectionError(ValueError):
    """An explicit evidence selection is unsafe or unbound."""


def _fail(message: str, cause: Exception | None = None) -> EvidenceSelectionError:
    error = EvidenceSelectionError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


@dataclass(frozen=True)
class EvidenceSelectionDependencies:
    runtime_root: Path
    candidate_root: Path
    source_head: Callable[[], str]
    now: Callable[[], str]
    failpoint: Callable[[str], None] = lambda _name: None


@dataclass(frozen=True)
class _EvidenceSelectionApplyDependencies:
    candidate_lock: Callable[[], AbstractContextManager[None]]
    persist_candidate: Callable[
        [dict[str, Any], dict[str, Any]], dict[str, Any]
    ]
    load_candidate: Callable[[str], dict[str, Any]]
    candidate_children: Callable[[str], set[str]]


def _timestamp(value: str, label: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise _fail(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise _fail(f"{label} is invalid", exc)
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise _fail(f"{label} is invalid")
    return parsed


def _json_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")


def _open_absolute(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    current = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_child(parent: int, name: str) -> tuple[int, tuple[int, int]]:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise _fail("selection directory is invalid")
    return descriptor, (info.st_dev, info.st_ino)


def _ensure_child(parent: int, name: str) -> tuple[int, tuple[int, int]]:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    return _open_child(parent, name)


def _read_regular(parent: int, name: str, maximum: int = 4 * 1024 * 1024) -> bytes:
    descriptor = None
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise _fail("selection file is invalid")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if identity(before) != identity(opened):
            raise _fail("selection file changed")
        chunks = []
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
        if len(body) > maximum or len(body) != opened.st_size or identity(opened) != identity(after) or identity(after) != identity(rebound):
            raise _fail("selection file changed")
        return body
    except OSError as exc:
        raise _fail("selection file is invalid", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json_fd(parent: int, name: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular(parent, name).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(f"{label} is invalid", exc)
    if type(value) is not dict:
        raise _fail(f"{label} is invalid")
    return value


def _read_json_path(path: Path, label: str) -> dict[str, Any]:
    parent = _open_absolute(path.parent)
    try:
        return _read_json_fd(parent, path.name, label)
    finally:
        os.close(parent)


def _write_json_fd(parent: int, name: str, value: dict[str, Any]) -> None:
    body = _json_bytes(value)
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
    except FileExistsError:
        existing = _read_json_fd(parent, name, "selection state")
        if existing != value:
            raise _fail("selection state conflicts")
        return
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    except OSError as exc:
        raise _fail("selection state is invalid", exc)
    finally:
        os.close(descriptor)
    os.fsync(parent)


def _entry_exists(parent: int, name: str) -> bool:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        raise _fail("selection state is invalid")
    return True


def _names(descriptor: int, maximum: int = 256) -> list[str]:
    names = sorted(os.listdir(descriptor))
    if len(names) > maximum:
        raise _fail("selection entry budget exceeded")
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise _fail("selection state aliases")
    return names


def _selection_root(runtime_root: Path, workspace_id: str) -> Path:
    return Path(runtime_root) / "workspaces" / "state" / workspace_id / "selection"


def _index_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / ".selection-index"


def _index_entry(runtime_root: Path, kind: str, identity: str) -> Path:
    return _index_root(runtime_root) / kind / f"{identity}.json"


@contextmanager
def _open_index_directory(runtime_root: Path, kind: str, *, create: bool) -> Iterator[int]:
    descriptors: list[int] = []
    try:
        root = _open_absolute(Path(runtime_root))
        descriptors.append(root)
        index_fd, _ = (_ensure_child(root, ".selection-index") if create else _open_child(root, ".selection-index"))
        descriptors.append(index_fd)
        kind_fd, _ = (_ensure_child(index_fd, kind) if create else _open_child(index_fd, kind))
        descriptors.append(kind_fd)
        yield kind_fd
    except OSError as exc:
        raise _fail("selection index is invalid", exc)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _open_selection_directory(
    runtime_root: Path,
    workspace_id: str,
    *,
    subdirectory: str | None = None,
    create: bool,
) -> Iterator[int]:
    descriptors: list[int] = []
    try:
        root = _open_absolute(Path(runtime_root))
        descriptors.append(root)
        workspaces_fd, _ = _open_child(root, "workspaces")
        descriptors.append(workspaces_fd)
        state_fd, _ = _open_child(workspaces_fd, "state")
        descriptors.append(state_fd)
        workspace_fd, _ = _open_child(state_fd, workspace_id)
        descriptors.append(workspace_fd)
        selection_fd, _ = (_ensure_child(workspace_fd, "selection") if create else _open_child(workspace_fd, "selection"))
        descriptors.append(selection_fd)
        current = selection_fd
        if subdirectory is not None:
            current, _ = (_ensure_child(selection_fd, subdirectory) if create else _open_child(selection_fd, subdirectory))
            descriptors.append(current)
        yield current
    except OSError as exc:
        raise _fail("selection state is invalid", exc)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_index(runtime_root: Path, kind: str, identity: str, workspace_id: str) -> None:
    with _open_index_directory(runtime_root, kind, create=True) as kind_fd:
        _write_json_fd(kind_fd, f"{identity}.json", {"workspace_id": workspace_id})


def _lookup_workspace_id(runtime_root: Path, kind: str, identity: str) -> str:
    with _open_index_directory(runtime_root, kind, create=False) as kind_fd:
        value = _read_json_fd(kind_fd, f"{identity}.json", f"{kind} index")
    workspace_id = value.get("workspace_id")
    if type(workspace_id) is not str:
        raise _fail(f"{kind} index is invalid")
    return workspace_id


@contextmanager
def _directory_lock(root: Path, filename: str) -> Iterator[None]:
    root_descriptor = _open_absolute(Path(root))
    try:
        descriptor = os.open(
            filename,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_descriptor,
        )
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != 0:
                raise _fail("selection lock is invalid")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
    finally:
        os.close(root_descriptor)


def _actual_source_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root(),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise _fail("source head is unavailable", exc)
    value = result.stdout.strip()
    if _SOURCE_HEAD.fullmatch(value) is None:
        raise _fail("source head is invalid")
    return value


def _verified_source_head(dependencies: EvidenceSelectionDependencies) -> str:
    declared = dependencies.source_head()
    if type(declared) is not str or _SOURCE_HEAD.fullmatch(declared) is None:
        raise _fail("source head is invalid")
    actual = _actual_source_head()
    if declared != actual:
        raise _fail("source head is stale")
    return actual


def _selection_inspection(
    proposal: dict[str, Any],
    authorization: dict[str, Any] | None,
    transaction_id: str | None,
    candidate_id: str | None,
    *,
    status: str,
    reason_code: str,
) -> dict[str, Any]:
    value = {
        "schema_version": "ao.lore.evidence-selection-inspection.v0.1",
        "proposal_id": proposal["proposal_id"],
        "proposal_digest": proposal["proposal_digest"],
        "authorization_id": None if authorization is None else authorization["authorization_id"],
        "transaction_id": transaction_id,
        "candidate_id": candidate_id,
        "status": status,
        "reason_code": reason_code,
        "inspection_digest": "",
        "candidate_creation": False,
        "legal_advice": False,
        "property_decision": False,
        "candidate_review": False,
        "candidate_decision": False,
        "promotion": False,
        "canonical_query": False,
        "provider": False,
        "credential": False,
        "private_data": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
    }
    value["inspection_digest"] = canonical_digest(
        {key: item for key, item in value.items() if key != "inspection_digest"}
    )
    return validate_selection_inspection(value)


def _selection_recovery_readback(
    workspace_id: str,
    *,
    status: str,
    reason_code: str,
    proposal_ids: list[str],
    candidate_ids: list[str],
) -> dict[str, Any]:
    from .evidence_selection_contracts import validate_selection_recovery

    value = {
        "schema_version": "ao.lore.evidence-selection-recovery.v0.1",
        "workspace_id": workspace_id,
        "status": status,
        "reason_code": reason_code,
        "proposal_ids": proposal_ids,
        "candidate_ids": candidate_ids,
        "recovery_digest": "",
        "candidate_creation": False,
        "legal_advice": False,
        "property_decision": False,
        "candidate_review": False,
        "candidate_decision": False,
        "promotion": False,
        "canonical_query": False,
        "provider": False,
        "credential": False,
        "private_data": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
    }
    value["recovery_digest"] = canonical_digest(
        {key: item for key, item in value.items() if key != "recovery_digest"}
    )
    return validate_selection_recovery(value)


def _load_matching_authorizations(
    runtime_root: Path, workspace_id: str, proposal_id: str
) -> list[dict[str, Any]]:
    with _open_selection_directory(
        runtime_root, workspace_id, subdirectory="authorizations", create=False
    ) as authorizations_fd:
        return [
            validate_selection_authorization(
                _read_json_fd(authorizations_fd, name, "selection authorization")
            )
            for name in _names(authorizations_fd)
            if name.endswith(".json")
        ]


def _expected_committed_inspection(
    proposal: dict[str, Any],
    authorization: dict[str, Any],
    transaction_id: str,
    candidate_id: str,
) -> dict[str, Any]:
    return _selection_inspection(
        proposal,
        authorization,
        transaction_id,
        candidate_id,
        status="committed",
        reason_code="ok",
    )


def _validate_committed_selection_state(
    runtime_root: Path,
    workspace_id: str,
    proposal: dict[str, Any],
    authorization: dict[str, Any],
    transaction: dict[str, Any],
    candidate_root: Path,
    *,
    candidate_loader: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidate_id = proposal["candidate"]["candidate_id"]
    candidate = (
        load_verified_candidate(candidate_id, candidate_root=candidate_root)
        if candidate_loader is None
        else candidate_loader(candidate_id)
    )
    if candidate["inspection"]["review_status"] != "unreviewed":
        raise _fail("selection candidate review state differs")
    with _open_selection_directory(runtime_root, workspace_id, subdirectory="consumed", create=False) as consumed_fd:
        consumption = validate_selection_consumption(
            _read_json_fd(consumed_fd, f"{authorization['authorization_id']}.json", "selection consumption")
        )
    if (
        consumption["proposal_id"] != proposal["proposal_id"]
        or consumption["proposal_digest"] != proposal["proposal_digest"]
        or consumption["authorization_id"] != authorization["authorization_id"]
        or consumption["authorization_digest"] != authorization["authorization_digest"]
        or consumption["candidate_id"] != candidate_id
    ):
        raise _fail("selection consumption differs")
    expected = _expected_committed_inspection(
        proposal, authorization, transaction["transaction_id"], candidate_id
    )
    with _open_selection_directory(runtime_root, workspace_id, subdirectory="audits", create=False) as audits_fd:
        if _names(audits_fd) != [f"{transaction['transaction_id']}.json"]:
            raise _fail("selection terminal artifacts differ")
        audit = validate_selection_inspection(
            _read_json_fd(audits_fd, f"{transaction['transaction_id']}.json", "selection audit")
        )
    with _open_selection_directory(runtime_root, workspace_id, subdirectory="completions", create=False) as completions_fd:
        if _names(completions_fd) != [f"{transaction['transaction_id']}.json"]:
            raise _fail("selection terminal artifacts differ")
        completion = validate_selection_inspection(
            _read_json_fd(completions_fd, f"{transaction['transaction_id']}.json", "selection completion")
        )
    if audit != expected or completion != expected:
        raise _fail("selection terminal artifacts differ")
    return expected


def _recover_committed_suffix(
    runtime_root: Path,
    workspace_id: str,
    proposal: dict[str, Any],
    authorization: dict[str, Any],
    transaction: dict[str, Any],
    candidate_root: Path,
) -> bool:
    candidate_id = proposal["candidate"]["candidate_id"]
    candidate = load_verified_candidate(candidate_id, candidate_root=candidate_root)
    if candidate["inspection"]["review_status"] != "unreviewed":
        return False
    try:
        with _open_selection_directory(runtime_root, workspace_id, subdirectory="consumed", create=False) as consumed_fd:
            consumption = validate_selection_consumption(
                _read_json_fd(consumed_fd, f"{authorization['authorization_id']}.json", "selection consumption")
            )
    except EvidenceSelectionError:
        return False
    if (
        consumption["proposal_id"] != proposal["proposal_id"]
        or consumption["proposal_digest"] != proposal["proposal_digest"]
        or consumption["authorization_id"] != authorization["authorization_id"]
        or consumption["authorization_digest"] != authorization["authorization_digest"]
        or consumption["candidate_id"] != candidate_id
    ):
        return False
    audit_names = _selection_subdir_entries(runtime_root, workspace_id, "audits")
    completion_names = _selection_subdir_entries(runtime_root, workspace_id, "completions")
    expected_name = f"{transaction['transaction_id']}.json"
    if any(name != expected_name for name in audit_names) or any(name != expected_name for name in completion_names):
        return False
    if audit_names and completion_names:
        return False
    _ensure_terminal_artifacts(
        runtime_root,
        workspace_id,
        transaction["transaction_id"],
        proposal,
        authorization,
        candidate_id,
    )
    return True


def _selection_subdir_entries(
    runtime_root: Path, workspace_id: str, subdirectory: str
) -> list[str]:
    try:
        with _open_selection_directory(
            runtime_root, workspace_id, subdirectory=subdirectory, create=False
        ) as descriptor:
            return _names(descriptor)
    except EvidenceSelectionError:
        return []


def _document_blocks(blocks: list[dict[str, Any]]):
    for block in blocks:
        yield block
        yield from _document_blocks(block.get("children", []))


def _snapshot_evidence(snapshot: WorkspaceQuerySnapshot) -> dict[tuple[str, str, str], dict[str, Any]]:
    if type(snapshot) is not WorkspaceQuerySnapshot:
        raise _fail("workspace snapshot is invalid")
    records: dict[tuple[str, str, str], dict[str, Any]] = {}
    _generation, graphs, documents = snapshot._open_progressive()
    for workspace_id, generation in documents.items():
        for document in generation["documents"]:
            for block in _document_blocks(document["document_ir"]["blocks"]):
                record = {
                    "workspace_id": workspace_id,
                    "document_store_id": generation["document_store_id"],
                    "generation_digest": generation["generation_digest"],
                    "document_id": document["document_id"],
                    "document_ir_digest": document["document_ir_digest"],
                    "source_id": document["source_id"],
                    "source_digest": document["source_digest"],
                    "evidence_kind": "document_block",
                    "evidence_id": canonical_digest(
                        {
                            "workspace_id": workspace_id,
                            "generation_digest": generation["generation_digest"],
                            "document_id": document["document_id"],
                            "block_id": block["id"],
                        }
                    ),
                    "evidence_digest": canonical_digest(block),
                    "block_id": block["id"],
                    "render_text": block["text"],
                    "source_span": copy.deepcopy(block["source_span"]),
                    "authority_role": document["authority_role"],
                    "sensitivity": document["sensitivity"],
                    "freshness_status": document["freshness_status"],
                    "qualification_codes": copy.deepcopy(document["qualification_codes"]),
                }
                records[(workspace_id, "document_block", record["evidence_id"])] = record
    for workspace_id, (graph, freshness) in graphs.items():
        sources = {item["source_id"]: item for item in graph["sources"]}
        claims = {item["claim_id"]: item for item in graph["claims"]}
        freshness_by_source = {}
        if freshness is not None:
            freshness_by_source = {
                item["source_id"]: item["classification"]
                for item in freshness["results"]
            }
        conflicted_claim_ids = {
            edge["source_id"]
            for edge in graph["edges"]
            if edge["edge_type"] == "conflicts_with" and edge["source_kind"] == "claim"
        } | {
            edge["target_id"]
            for edge in graph["edges"]
            if edge["edge_type"] == "conflicts_with" and edge["target_kind"] == "claim"
        }
        for claim in graph["claims"]:
            freshness_status = freshness_by_source.get(claim["source_id"], "unchanged")
            record = {
                "workspace_id": workspace_id,
                "graph_id": graph["graph_id"],
                "graph_digest": graph["graph_digest"],
                "source_id": claim["source_id"],
                "source_digest": sources[claim["source_id"]]["source_digest"],
                "evidence_kind": "graph_claim",
                "evidence_id": claim["claim_id"],
                "evidence_digest": claim["excerpt_digest"],
                "claim_id": claim["claim_id"],
                "excerpt": claim["excerpt"],
                "citation_anchor": claim["citation_anchor"],
                "authority_role": claim["authority_role"],
                "freshness_status": "current" if freshness_status == "unchanged" else freshness_status,
                "qualification_codes": copy.deepcopy(claim.get("semantic_reason_codes", [])),
                "conflict_status": "conflicted" if claim["claim_id"] in conflicted_claim_ids else "clear",
                "semantic_review": "verified",
            }
            records[(workspace_id, "graph_claim", record["evidence_id"])] = record
        for edge in graph["edges"]:
            if edge["source_kind"] == "claim":
                source_id = claims[edge["source_id"]]["source_id"]
            elif edge["source_kind"] == "source":
                source_id = edge["source_id"]
            else:
                source_id = graph["claims"][0]["source_id"]
            freshness_status = freshness_by_source.get(source_id, "unchanged")
            record = {
                "workspace_id": workspace_id,
                "graph_id": graph["graph_id"],
                "graph_digest": graph["graph_digest"],
                "source_id": source_id,
                "source_digest": sources[source_id]["source_digest"],
                "evidence_kind": "graph_edge",
                "evidence_id": edge["edge_id"],
                "evidence_digest": edge["edge_digest"],
                "edge_id": edge["edge_id"],
                "source_claim_id": edge["source_id"],
                "target_claim_id": edge["target_id"],
                "edge_type": edge["edge_type"],
                "supporting_excerpt_digest": edge["supporting_excerpt_digest"],
                "authority_role": sources[source_id]["authority_role"],
                "freshness_status": "current" if freshness_status == "unchanged" else freshness_status,
                "qualification_codes": [edge["reason_code"]],
                "conflict_status": "conflicted" if edge["edge_type"] == "conflicts_with" else "clear",
                "semantic_review": "verified",
            }
            records[(workspace_id, "graph_edge", record["evidence_id"])] = record
    return records


def _runtime_graph_snapshot(
    runtime_root: Path,
    workspace_id: str,
    definition: dict[str, Any],
):
    from .workspace_query import WorkspaceGraphSnapshot

    if definition["graph_id"] is None or definition["graph_digest"] is None:
        raise _fail("workspace graph binding differs")
    graph_path = (
        Path(runtime_root)
        / "workspaces"
        / "state"
        / workspace_id
        / "graph"
        / "generations"
        / f"{definition['graph_digest'].split(':', 1)[1]}.json"
    )
    graph = _read_json_path(graph_path, "workspace graph")
    summary = None
    if definition["freshness_summary_status"] == "observed":
        summary_path = (
            Path(runtime_root)
            / "workspaces"
            / "state"
            / workspace_id
            / "freshness"
            / "summaries"
            / f"{definition['freshness_summary_id']}.json"
        )
        summary = _read_json_path(summary_path, "workspace freshness summary")
    return WorkspaceGraphSnapshot(workspace_id, graph, summary)


def _runtime_document_snapshot(runtime_root: Path, workspace_id: str):
    from .workspace_query import WorkspaceDocumentQuerySnapshot

    generation = load_workspace_documents(
        WorkspaceDocumentDependencies(runtime_root), workspace_id
    ).generation
    if generation is None:
        raise _fail("workspace document binding differs")
    return WorkspaceDocumentQuerySnapshot(workspace_id, generation)


def _runtime_snapshot_for_proposal(
    runtime_root: Path, proposal: dict[str, Any]
) -> WorkspaceQuerySnapshot:
    from .workspace_query import WorkspaceQuerySnapshot

    registry = load_workspace_registry(WorkspaceRegistryDependencies(runtime_root))
    if not registry.generations or registry.generations[-1]["registry_digest"] != proposal["registry_digest"]:
        raise _fail("workspace registry changed")
    selection = select_workspace(registry, proposal["primary_workspace_id"])
    definitions = {
        selection.primary["workspace_id"]: selection.primary,
        **{item["workspace_id"]: item for item in selection.references},
    }
    selected_workspace_ids = [item["workspace_id"] for item in proposal["evidence"]]
    if any(workspace_id not in definitions for workspace_id in selected_workspace_ids):
        raise _fail("selection evidence is unreachable")
    graph_ids = {item["workspace_id"] for item in proposal["evidence"] if item["evidence_kind"].startswith("graph_")}
    document_ids = {item["workspace_id"] for item in proposal["evidence"] if item["evidence_kind"] == "document_block"}
    graphs = tuple(
        _runtime_graph_snapshot(runtime_root, workspace_id, definitions[workspace_id])
        for workspace_id in sorted(graph_ids)
    )
    documents = tuple(
        _runtime_document_snapshot(runtime_root, workspace_id)
        for workspace_id in sorted(document_ids)
    )
    snapshot = WorkspaceQuerySnapshot(
        WorkspaceRegistrySnapshot(registry.generations, registry.workspaces),
        selection,
        graphs,
        documents=documents,
    )
    current = _resolve_selected_evidence(
        snapshot,
        proposal["primary_workspace_id"],
        tuple(item["evidence_id"] for item in proposal["evidence"]),
    )
    if current != proposal["evidence"]:
        raise _fail("selection evidence drifted from current workspace state")
    return snapshot


def _resolve_selected_evidence(
    snapshot: WorkspaceQuerySnapshot,
    primary_workspace_id: str,
    evidence_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    if type(snapshot) is not WorkspaceQuerySnapshot:
        raise _fail("workspace snapshot is invalid")
    if type(primary_workspace_id) is not str or primary_workspace_id != snapshot._primary_workspace_id:
        raise _fail("workspace selection differs")
    if type(evidence_ids) is not tuple or not 1 <= len(evidence_ids) <= 32:
        raise _fail("selection evidence identities are invalid")
    if len(evidence_ids) != len(set(evidence_ids)):
        raise _fail("selection evidence identities contain duplicates")
    records = list(_snapshot_evidence(snapshot).values())
    by_evidence_id: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_evidence_id.setdefault(record["evidence_id"], []).append(record)
    selected = []
    for evidence_id in evidence_ids:
        if type(evidence_id) is not str:
            raise _fail("selection evidence identity is invalid")
        matches = by_evidence_id.get(evidence_id, [])
        if not matches:
            raise _fail("selection evidence is unreachable")
        if len(matches) != 1:
            raise _fail("selection evidence identity is ambiguous")
        selected.append(copy.deepcopy(matches[0]))
    return selected


def _selection_digest(evidence: list[dict[str, Any]]) -> str:
    return canonical_digest({"domain": "ao.lore.evidence-selection.v0.1", "evidence": evidence})


def _citation_id(selection_digest: str, evidence: dict[str, Any]) -> str:
    return "citation-" + canonical_digest(
        {
            "domain": "ao.lore.evidence-candidate.citation-id.v0.1",
            "selection_digest": selection_digest,
            "workspace_id": evidence["workspace_id"],
            "evidence_id": evidence["evidence_id"],
        }
    )[7:31]


def _claim_id(selection_digest: str, evidence: dict[str, Any]) -> str:
    return "claim-" + canonical_digest(
        {
            "domain": "ao.lore.evidence-candidate.claim-id.v0.1",
            "selection_digest": selection_digest,
            "workspace_id": evidence["workspace_id"],
            "evidence_id": evidence["evidence_id"],
        }
    )[7:31]


def _candidate_targets(
    proposal_id: str,
    candidate_id: str,
    primary_workspace_id: str,
    registry_digest: str,
    evidence: list[dict[str, Any]],
    *,
    created_at: str,
) -> tuple[str, str, str]:
    selection_digest = _selection_digest(evidence)
    document_blocks = [item for item in evidence if item["evidence_kind"] == "document_block"]
    graph_claims = [item for item in evidence if item["evidence_kind"] == "graph_claim"]
    graph_edges = [item for item in evidence if item["evidence_kind"] == "graph_edge"]
    citation_ids = {}
    citations = []
    for item in [*graph_claims, *document_blocks]:
        current = _citation_id(selection_digest, item)
        citation_ids[(item["workspace_id"], item["evidence_kind"], item["evidence_id"])] = current
        citations.append(
            {
                "citation_id": current,
                "render_text": item["excerpt"] if item["evidence_kind"] == "graph_claim" else item["render_text"],
                "source_block_ids": [item["claim_id"] if item["evidence_kind"] == "graph_claim" else item["block_id"]],
            }
        )
    claims = []
    claim_mappings = []
    for item in [*document_blocks, *graph_claims]:
        source_block_id = item["claim_id"] if item["evidence_kind"] == "graph_claim" else item["block_id"]
        claim = _claim_id(selection_digest, item)
        claims.append(
            {
                "claim_id": claim,
                "text": item["excerpt"] if item["evidence_kind"] == "graph_claim" else item["render_text"],
                "source_block_ids": [source_block_id],
                "citation_id": citation_ids[(item["workspace_id"], item["evidence_kind"], item["evidence_id"])],
            }
        )
        claim_mappings.append({"claim_id": claim, "block_ids": [source_block_id]})
    selected_claim_ids = {item["claim_id"] for item in graph_claims}
    links = []
    for item in graph_edges:
        if item["source_claim_id"] in selected_claim_ids and item["target_claim_id"] in selected_claim_ids:
            links.append(
                {
                    "source_block_id": item["source_claim_id"],
                    "label": item["edge_type"],
                    "target": item["target_claim_id"],
                    "status": "proposed",
                }
            )
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.3",
        "candidate_id": candidate_id,
        "evidence_selection_digest": selection_digest,
        "evidence_origins": evidence,
        "concepts": [],
        "claim_mappings": claim_mappings,
        "links": links,
        "contradiction_warnings": [],
        "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
        "claims_digest": canonical_digest({"domain": "ao.lore.canonical-claim-set.v0.1", "claims": claims}),
        "claims": claims,
        "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
        "citations_digest": canonical_digest({"domain": "ao.lore.canonical-citation-set.v0.1", "citations": citations}),
        "citations": citations,
        "knowledge_policy": {
            "sensitivity": "internal"
            if any(item.get("sensitivity", "internal") == "internal" for item in document_blocks)
            else "public",
            "stale_after": None,
        },
        "canonical": False,
        "promotion_authority": False,
    }
    candidate_digest = canonical_digest(candidate)
    provenance = {
        "schema_version": "ao.lore.candidate-provenance.v0.3",
        "candidate_id": candidate_id,
        "candidate_digest": candidate_digest,
        "proposal_id": proposal_id,
        "evidence_selection_digest": selection_digest,
        "primary_workspace_id": primary_workspace_id,
        "registry_digest": registry_digest,
        "evidence_origins": evidence,
        "created_at": created_at,
    }
    return selection_digest, candidate_digest, canonical_digest(provenance)


def prepare_evidence_selection(
    snapshot: WorkspaceQuerySnapshot,
    primary_workspace_id: str,
    evidence_ids: tuple[str, ...],
    *,
    now: str,
) -> dict[str, object]:
    """Create a detached proposal; perform no writes and grant no authority."""

    prepared_at = _timestamp(now, "prepared_at")
    selected = _resolve_selected_evidence(snapshot, primary_workspace_id, evidence_ids)
    if any(item["evidence_kind"] == "document_block" and item["sensitivity"] == "restricted" for item in selected):
        raise _fail("restricted evidence cannot be selected")
    for item in selected:
        if item["freshness_status"] != "current":
            raise _fail("selected evidence freshness requires review")
        if item["evidence_kind"] in {"graph_claim", "graph_edge"}:
            if item["authority_role"] == "case_evidence":
                raise _fail("restricted graph evidence cannot be selected")
            if item["conflict_status"] != "clear":
                raise _fail("conflicting graph evidence cannot be selected")
            if item["semantic_review"] != "verified":
                raise _fail("ambiguous graph evidence cannot be selected")
    graph_claim_ids = {item["claim_id"] for item in selected if item["evidence_kind"] == "graph_claim"}
    for item in selected:
        if item["evidence_kind"] == "graph_edge":
            if item["source_claim_id"] not in graph_claim_ids or item["target_claim_id"] not in graph_claim_ids:
                raise _fail("graph edge endpoints must be selected explicitly")
    generation = snapshot.registry.generations[-1]
    selection_seed = canonical_digest(
        {
            "domain": "ao.lore.selection-request-id.v0.1",
            "primary_workspace_id": primary_workspace_id,
            "evidence_ids": list(evidence_ids),
            "requested_at": _json_time(prepared_at),
        }
    )
    request_id = "selection-request-" + selection_seed[7:31]
    request = {
        "schema_version": "ao.lore.evidence-selection-request.v0.1",
        "request_id": request_id,
        "primary_workspace_id": primary_workspace_id,
        "registry_digest": generation["registry_digest"],
        "requested_at": _json_time(prepared_at),
        "evidence_ids": list(evidence_ids),
        "request_digest": "",
        "candidate_creation": False,
        "legal_advice": False,
        "property_decision": False,
        "candidate_review": False,
        "candidate_decision": False,
        "promotion": False,
        "canonical_query": False,
        "provider": False,
        "credential": False,
        "private_data": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
    }
    request["request_digest"] = canonical_digest({key: value for key, value in request.items() if key != "request_digest"})
    proposal_seed = canonical_digest(
        {
            "domain": "ao.lore.selection-proposal-id.v0.1",
            "request_digest": request["request_digest"],
            "prepared_at": _json_time(prepared_at),
        }
    )
    proposal_id = "proposal-" + proposal_seed[7:31]
    candidate_id = "candidate-" + canonical_digest(
        {
            "domain": "ao.lore.selection-candidate-id.v0.1",
            "proposal_id": proposal_id,
            "registry_digest": generation["registry_digest"],
        }
    )[7:31]
    selection_digest, candidate_digest, provenance_digest = _candidate_targets(
        proposal_id,
        candidate_id,
        primary_workspace_id,
        generation["registry_digest"],
        selected,
        created_at=_json_time(prepared_at),
    )
    write_set = [
        f"candidates/{candidate_id}/candidate.json",
        f"candidates/{candidate_id}/provenance.json",
        f"candidates/{candidate_id}/reviews",
    ]
    proposal = {
        "schema_version": "ao.lore.evidence-selection-proposal.v0.1",
        "proposal_id": proposal_id,
        "request_digest": request["request_digest"],
        "primary_workspace_id": primary_workspace_id,
        "registry_digest": generation["registry_digest"],
        "definition_digest": snapshot.registry.workspaces[
            [item["workspace_id"] for item in snapshot.registry.workspaces].index(primary_workspace_id)
        ]["definition_digest"],
        "prepared_at": _json_time(prepared_at),
        "expires_at": _json_time(prepared_at + timedelta(minutes=5)),
        "evidence_selection_digest": selection_digest,
        "candidate": {
            "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "provenance_digest": provenance_digest,
        },
        "allowed_write_set": write_set,
        "allowed_write_set_digest": canonical_digest(
            {"domain": "ao.lore.evidence-selection.write-set.v0.1", "paths": write_set}
        ),
        "evidence": selected,
        "proposal_digest": "",
        "candidate_creation": False,
        "legal_advice": False,
        "property_decision": False,
        "candidate_review": False,
        "candidate_decision": False,
        "promotion": False,
        "canonical_query": False,
        "provider": False,
        "credential": False,
        "private_data": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
    }
    proposal["proposal_digest"] = canonical_digest(
        {key: value for key, value in proposal.items() if key != "proposal_digest"}
    )
    try:
        return validate_selection_proposal(proposal)
    except Exception as exc:
        raise _fail("selection proposal is invalid", exc)


def persist_prepared_selection(
    dependencies: EvidenceSelectionDependencies,
    proposal: dict[str, object],
) -> dict[str, object]:
    validated = validate_selection_proposal(copy.deepcopy(proposal))
    workspace_id = validated["primary_workspace_id"]
    with _open_selection_directory(
        Path(dependencies.runtime_root), workspace_id, subdirectory="proposals", create=True
    ) as proposals_fd:
        _write_json_fd(proposals_fd, f"{validated['proposal_id']}.json", validated)
    _write_index(
        Path(dependencies.runtime_root), "proposals", validated["proposal_id"], workspace_id
    )
    return validated


def authorize_evidence_selection(
    dependencies: EvidenceSelectionDependencies,
    proposal: dict[str, object],
    operator_id: str,
) -> dict[str, object]:
    validated = validate_selection_proposal(copy.deepcopy(proposal))
    if _timestamp(validated["expires_at"], "proposal expires_at") <= _timestamp(
        dependencies.now(), "current time"
    ):
        raise _fail("selection proposal expired")
    actual_source_head = _verified_source_head(dependencies)
    workspace_id = validated["primary_workspace_id"]
    with _open_selection_directory(
        Path(dependencies.runtime_root), workspace_id, subdirectory="proposals", create=True
    ) as proposals_fd:
        _write_json_fd(proposals_fd, f"{validated['proposal_id']}.json", validated)
    _write_index(Path(dependencies.runtime_root), "proposals", validated["proposal_id"], workspace_id)
    issued_at = _timestamp(dependencies.now(), "issued_at")
    authorization = {
        "schema_version": "ao.lore.evidence-selection-authorization.v0.1",
        "authorization_id": "authorization-" + canonical_digest(
            {
                "domain": "ao.lore.evidence-selection.authorization-id.v0.1",
                "proposal_digest": validated["proposal_digest"],
                "issued_at": _json_time(issued_at),
                "source_head": actual_source_head,
            }
        )[7:31],
        "proposal_id": validated["proposal_id"],
        "proposal_digest": validated["proposal_digest"],
        "operation": "apply",
        "operator_id": operator_id,
        "nonce": "nonce-" + canonical_digest(
            {
                "domain": "ao.lore.evidence-selection.nonce.v0.1",
                "proposal_digest": validated["proposal_digest"],
                "issued_at": _json_time(issued_at),
                "operator_id": operator_id,
            }
        )[7:31],
        "source_head": actual_source_head,
        "issued_at": _json_time(issued_at),
        "expires_at": _json_time(issued_at + timedelta(minutes=5)),
        "candidate_creation": True,
        "one_use_only": True,
        "product_self_issued": False,
        "legal_advice": False,
        "property_decision": False,
        "candidate_review": False,
        "candidate_decision": False,
        "promotion": False,
        "canonical_query": False,
        "provider": False,
        "credential": False,
        "private_data": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
        "authorization_digest": "",
    }
    authorization["authorization_digest"] = canonical_digest(
        {
            key: value
            for key, value in authorization.items()
            if key != "authorization_digest"
        }
    )
    validated_authorization = validate_selection_authorization(authorization)
    with _open_selection_directory(
        Path(dependencies.runtime_root), workspace_id, subdirectory="authorizations", create=True
    ) as authorizations_fd:
        _write_json_fd(
            authorizations_fd,
            f"{validated_authorization['authorization_id']}.json",
            validated_authorization,
        )
    _write_index(Path(dependencies.runtime_root), "authorizations", validated_authorization["authorization_id"], workspace_id)
    return validated_authorization


def _load_apply_state(
    runtime_root: Path, proposal_id: str, authorization_id: str
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    workspace_id = _lookup_workspace_id(runtime_root, "proposals", proposal_id)
    auth_workspace = _lookup_workspace_id(runtime_root, "authorizations", authorization_id)
    if auth_workspace != workspace_id:
        raise _fail("selection authorization conflicts with proposal binding")
    with _open_selection_directory(runtime_root, workspace_id, subdirectory="proposals", create=False) as proposals_fd:
        proposal = validate_selection_proposal(
            _read_json_fd(proposals_fd, f"{proposal_id}.json", "selection proposal")
        )
    with _open_selection_directory(runtime_root, workspace_id, subdirectory="authorizations", create=False) as authorizations_fd:
        authorization = validate_selection_authorization(
            _read_json_fd(authorizations_fd, f"{authorization_id}.json", "selection authorization")
        )
    if authorization["proposal_id"] != proposal["proposal_id"] or authorization["proposal_digest"] != proposal["proposal_digest"]:
        raise _fail("selection authorization differs from proposal")
    return workspace_id, proposal, authorization


def _selection_result(
    proposal: dict[str, Any],
    authorization: dict[str, Any],
    transaction_id: str,
) -> dict[str, object]:
    return {
        "status": "committed",
        "proposal_id": proposal["proposal_id"],
        "authorization_id": authorization["authorization_id"],
        "transaction_id": transaction_id,
        "candidate_id": proposal["candidate"]["candidate_id"],
        "candidate_digest": proposal["candidate"]["candidate_digest"],
        "provenance_digest": proposal["candidate"]["provenance_digest"],
    }


def _candidate_children(candidate_root: Path, candidate_id: str) -> set[str]:
    path = Path(candidate_root) / candidate_id
    try:
        return {item.name for item in path.iterdir()}
    except OSError as exc:
        raise _fail("candidate state is invalid", exc)


def _ensure_terminal_artifacts(
    runtime_root: Path,
    workspace_id: str,
    transaction_id: str,
    proposal: dict[str, Any],
    authorization: dict[str, Any],
    candidate_id: str,
) -> None:
    audit = _selection_inspection(
        proposal,
        authorization,
        transaction_id,
        candidate_id,
        status="committed",
        reason_code="ok",
    )
    with _open_selection_directory(runtime_root, workspace_id, subdirectory="audits", create=True) as audits_fd:
        _write_json_fd(audits_fd, f"{transaction_id}.json", audit)
    with _open_selection_directory(runtime_root, workspace_id, subdirectory="completions", create=True) as completions_fd:
        _write_json_fd(completions_fd, f"{transaction_id}.json", audit)


def _apply_evidence_selection_with_dependencies(
    dependencies: EvidenceSelectionDependencies,
    proposal_id: str,
    authorization_id: str,
    *,
    apply_dependencies: _EvidenceSelectionApplyDependencies,
) -> dict[str, object]:
    """Private apply path with one exact candidate persistence capability."""

    if type(apply_dependencies) is not _EvidenceSelectionApplyDependencies:
        raise _fail("selection apply dependencies are invalid")

    runtime_root = Path(dependencies.runtime_root)
    with apply_dependencies.candidate_lock():
        workspace_id = _lookup_workspace_id(runtime_root, "proposals", proposal_id)
        with _directory_lock(_selection_root(runtime_root, workspace_id), "selection.lock"):
            workspace_id, proposal, authorization = _load_apply_state(
                runtime_root, proposal_id, authorization_id
            )
            actual_source_head = _verified_source_head(dependencies)
            if authorization["source_head"] != actual_source_head:
                raise _fail("selection authorization source head is stale")
            transaction_id = "transaction-" + canonical_digest(
                {
                    "domain": "ao.lore.evidence-selection.transaction-id.v0.1",
                    "proposal_digest": proposal["proposal_digest"],
                    "authorization_digest": authorization["authorization_digest"],
                }
            )[7:31]
            with _open_selection_directory(runtime_root, workspace_id, subdirectory="transactions", create=True) as transactions_fd:
                transaction_exists = _entry_exists(transactions_fd, f"{transaction_id}.json")
                if transaction_exists:
                    transaction = validate_selection_transaction(
                        _read_json_fd(transactions_fd, f"{transaction_id}.json", "selection transaction")
                    )
                else:
                    transaction = None
            if transaction is not None:
                candidate = apply_dependencies.load_candidate(
                    proposal["candidate"]["candidate_id"]
                )
                if candidate["inspection"]["review_status"] != "unreviewed":
                    raise _fail("selection candidate review state differs")
                _ensure_terminal_artifacts(
                    runtime_root,
                    workspace_id,
                    transaction["transaction_id"],
                    proposal,
                    authorization,
                    proposal["candidate"]["candidate_id"],
                )
                return _selection_result(proposal, authorization, transaction["transaction_id"])
            if _timestamp(proposal["expires_at"], "proposal expires_at") <= _timestamp(
                dependencies.now(), "current time"
            ):
                raise _fail("selection proposal expired")
            expires = _timestamp(authorization["expires_at"], "authorization expires_at")
            if expires <= _timestamp(dependencies.now(), "current time"):
                raise _fail("selection authorization expired")
            snapshot = _runtime_snapshot_for_proposal(runtime_root, proposal)
            from .evidence_candidate import materialize_evidence_candidate

            candidate, provenance = materialize_evidence_candidate(proposal, snapshot)
            reservation = {
                "proposal_id": proposal["proposal_id"],
                "authorization_id": authorization["authorization_id"],
                "authorization_digest": authorization["authorization_digest"],
                "proposal_digest": proposal["proposal_digest"],
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": proposal["candidate"]["candidate_digest"],
                "nonce": authorization["nonce"],
                "source_head": authorization["source_head"],
                "state": "reserved",
                "recorded_at": dependencies.now(),
            }
            with _open_selection_directory(runtime_root, workspace_id, subdirectory="reservations", create=True) as reservations_fd:
                _write_json_fd(reservations_fd, f"{authorization['authorization_id']}.json", reservation)
            dependencies.failpoint("after_selection_reservation")
            result = {
                "schema_version": "ao.lore.distillation-result.v0.2",
                "candidate": candidate,
                "candidate_digest": proposal["candidate"]["candidate_digest"],
                "distillation_trace": {
                    "adapter": "evidence-selection",
                    "policy_version": "v0.3",
                    "input_block_count": len(candidate["claim_mappings"]),
                    "candidate_concept_count": len(candidate["concepts"]),
                    "candidate_claim_count": len(candidate["claims"]),
                    "candidate_citation_count": len(candidate["citations"]),
                    "private_reasoning_persisted": False,
                },
            }
            persisted = apply_dependencies.persist_candidate(result, provenance)
            dependencies.failpoint("after_selection_candidate")
            reopened = apply_dependencies.load_candidate(candidate["candidate_id"])
            if reopened["inspection"]["review_status"] != "unreviewed":
                raise _fail("selection candidate review state differs")
            expected_children = {
                item.rsplit("/", 1)[1] for item in proposal["allowed_write_set"]
            }
            if (
                apply_dependencies.candidate_children(candidate["candidate_id"])
                != expected_children
            ):
                raise _fail("selection candidate write set differs")
            consumption = {
                "schema_version": "ao.lore.evidence-selection-consumption.v0.1",
                "consumption_id": "consumption-" + canonical_digest(
                    {
                        "domain": "ao.lore.evidence-selection.consumption-id.v0.1",
                        "authorization_digest": authorization["authorization_digest"],
                        "proposal_digest": proposal["proposal_digest"],
                    }
                )[7:31],
                "proposal_id": proposal["proposal_id"],
                "proposal_digest": proposal["proposal_digest"],
                "authorization_id": authorization["authorization_id"],
                "authorization_digest": authorization["authorization_digest"],
                "candidate_id": candidate["candidate_id"],
                "nonce": authorization["nonce"],
                "consumed_at": dependencies.now(),
                "status": "consumed",
                "consumption_digest": "",
                "candidate_creation": False,
                "legal_advice": False,
                "property_decision": False,
                "candidate_review": False,
                "candidate_decision": False,
                "promotion": False,
                "canonical_query": False,
                "provider": False,
                "credential": False,
                "private_data": False,
                "publication": False,
                "release": False,
                "deployment": False,
                "authority_advanced": False,
            }
            consumption["consumption_digest"] = canonical_digest(
                {
                    key: value
                    for key, value in consumption.items()
                    if key != "consumption_digest"
                }
            )
            validated_consumption = validate_selection_consumption(consumption)
            with _open_selection_directory(runtime_root, workspace_id, subdirectory="consumed", create=True) as consumed_fd:
                _write_json_fd(consumed_fd, f"{authorization['authorization_id']}.json", validated_consumption)
            dependencies.failpoint("after_selection_consumption")
            transaction = {
                "schema_version": "ao.lore.evidence-selection-transaction.v0.1",
                "transaction_id": transaction_id,
                "proposal_id": proposal["proposal_id"],
                "proposal_digest": proposal["proposal_digest"],
                "authorization_id": authorization["authorization_id"],
                "authorization_digest": authorization["authorization_digest"],
                "candidate_id": candidate["candidate_id"],
                "candidate_digest": proposal["candidate"]["candidate_digest"],
                "provenance_digest": proposal["candidate"]["provenance_digest"],
                "allowed_write_set_digest": proposal["allowed_write_set_digest"],
                "status": "committed",
                "committed_at": dependencies.now(),
                "transaction_digest": "",
                "candidate_creation": False,
                "legal_advice": False,
                "property_decision": False,
                "candidate_review": False,
                "candidate_decision": False,
                "promotion": False,
                "canonical_query": False,
                "provider": False,
                "credential": False,
                "private_data": False,
                "publication": False,
                "release": False,
                "deployment": False,
                "authority_advanced": False,
            }
            transaction["transaction_digest"] = canonical_digest(
                {
                    key: value
                    for key, value in transaction.items()
                    if key != "transaction_digest"
                }
            )
            validated_transaction = validate_selection_transaction(transaction)
            with _open_selection_directory(runtime_root, workspace_id, subdirectory="transactions", create=True) as transactions_fd:
                _write_json_fd(transactions_fd, f"{transaction_id}.json", validated_transaction)
            dependencies.failpoint("after_selection_transaction")
            _ensure_terminal_artifacts(
                runtime_root,
                workspace_id,
                transaction_id,
                proposal,
                authorization,
                candidate["candidate_id"],
            )
            dependencies.failpoint("after_selection_completion")
            return _selection_result(proposal, authorization, transaction_id)


def apply_evidence_selection(
    dependencies: EvidenceSelectionDependencies,
    proposal_id: str,
    authorization_id: str,
) -> dict[str, object]:
    """Consume exact authority and publish one non-canonical candidate."""

    return _apply_evidence_selection_with_dependencies(
        dependencies,
        proposal_id,
        authorization_id,
        apply_dependencies=_EvidenceSelectionApplyDependencies(
            lambda: _directory_lock(
                Path(dependencies.candidate_root), ".candidate.lock"
            ),
            lambda result, provenance: persist_candidate(
                result,
                provenance,
                candidate_root=dependencies.candidate_root,
            ),
            lambda candidate_id: load_verified_candidate(
                candidate_id, candidate_root=dependencies.candidate_root,
            ),
            lambda candidate_id: _candidate_children(
                dependencies.candidate_root, candidate_id,
            ),
        ),
    )


def _inspect_evidence_selection_with_candidate_loader(
    dependencies: EvidenceSelectionDependencies,
    proposal_id: str,
    *,
    candidate_loader: Callable[[str], dict[str, Any]] | None,
) -> dict[str, object]:
    runtime_root = Path(dependencies.runtime_root)
    workspace_id = _lookup_workspace_id(runtime_root, "proposals", proposal_id)
    proposal, authorization, transaction = None, None, None
    with _open_selection_directory(runtime_root, workspace_id, subdirectory="proposals", create=False) as proposals_fd:
        proposal = validate_selection_proposal(
            _read_json_fd(proposals_fd, f"{proposal_id}.json", "selection proposal")
        )
    authorizations = [
        item for item in _load_matching_authorizations(runtime_root, workspace_id, proposal_id)
        if item["proposal_id"] == proposal_id
    ]
    if len(authorizations) > 1:
        return _selection_inspection(
            proposal, None, None, None, status="investigate", reason_code="ambiguous_authorization"
        )
    authorization = None if not authorizations else authorizations[0]
    if authorization is None:
        return _selection_inspection(
            proposal, None, None, None, status="prepared", reason_code="authorization_absent"
        )
    transaction_id = "transaction-" + canonical_digest(
        {
            "domain": "ao.lore.evidence-selection.transaction-id.v0.1",
            "proposal_digest": proposal["proposal_digest"],
            "authorization_digest": authorization["authorization_digest"],
        }
    )[7:31]
    try:
        with _open_selection_directory(runtime_root, workspace_id, subdirectory="transactions", create=False) as transactions_fd:
            if _entry_exists(transactions_fd, f"{transaction_id}.json"):
                transaction = validate_selection_transaction(
                    _read_json_fd(transactions_fd, f"{transaction_id}.json", "selection transaction")
                )
    except EvidenceSelectionError:
        transaction = None
    if transaction is None:
        return _selection_inspection(
            proposal,
            authorization,
            None,
            None,
            status="authorized",
            reason_code="pending_apply",
        )
    try:
        return _validate_committed_selection_state(
            runtime_root,
            workspace_id,
            proposal,
            authorization,
            transaction,
            dependencies.candidate_root,
            candidate_loader=candidate_loader,
        )
    except (EvidenceSelectionError, ContractError, CandidateError):
        return _selection_inspection(
            proposal,
            authorization,
            transaction_id,
            proposal["candidate"]["candidate_id"],
            status="investigate",
            reason_code="committed_state_invalid",
        )


def inspect_evidence_selection(
    dependencies: EvidenceSelectionDependencies,
    proposal_id: str,
) -> dict[str, object]:
    return _inspect_evidence_selection_with_candidate_loader(
        dependencies, proposal_id, candidate_loader=None,
    )


def recover_evidence_selections(
    dependencies: EvidenceSelectionDependencies,
    workspace_id: str,
) -> dict[str, object]:
    runtime_root = Path(dependencies.runtime_root)
    selection_root = _selection_root(runtime_root, workspace_id)
    if not selection_root.exists():
        return _selection_recovery_readback(
            workspace_id,
            status="empty",
            reason_code="no_selection_state",
            proposal_ids=[],
            candidate_ids=[],
        )
    foreign = False
    known = {"proposals", "authorizations", "reservations", "consumed", "transactions", "audits", "completions", "selection.lock"}
    selection_fd = None
    try:
        selection_fd = _open_absolute(selection_root)
        for name in _names(selection_fd):
            if name not in known:
                foreign = True
        proposal_ids: list[str] = []
        with _open_selection_directory(runtime_root, workspace_id, subdirectory="proposals", create=True) as proposals_fd:
            proposal_ids = [name[:-5] for name in _names(proposals_fd) if name.endswith(".json")]
        if foreign:
            return _selection_recovery_readback(
                workspace_id,
                status="investigate",
                reason_code="foreign_selection_state",
                proposal_ids=proposal_ids,
                candidate_ids=[],
            )
        suspicious = (
            any(not name.endswith(".json") for name in _selection_subdir_entries(runtime_root, workspace_id, "reservations"))
            or any(not name.endswith(".json") for name in _selection_subdir_entries(runtime_root, workspace_id, "consumed"))
            or any(not name.endswith(".json") for name in _selection_subdir_entries(runtime_root, workspace_id, "transactions"))
            or any(not name.endswith(".json") for name in _selection_subdir_entries(runtime_root, workspace_id, "audits"))
            or any(not name.endswith(".json") for name in _selection_subdir_entries(runtime_root, workspace_id, "completions"))
        )
        if suspicious:
            return _selection_recovery_readback(
                workspace_id,
                status="investigate",
                reason_code="foreign_selection_state",
                proposal_ids=proposal_ids,
                candidate_ids=[],
            )
        if not proposal_ids:
            return _selection_recovery_readback(
                workspace_id,
                status="empty",
                reason_code="no_selection_state",
                proposal_ids=[],
                candidate_ids=[],
            )
        if len(proposal_ids) > 1:
            return _selection_recovery_readback(
                workspace_id,
                status="investigate",
                reason_code="ambiguous_selection_state",
                proposal_ids=proposal_ids,
                candidate_ids=[],
            )
        proposal_id = proposal_ids[0]
        with _open_selection_directory(runtime_root, workspace_id, subdirectory="proposals", create=False) as proposals_fd:
            proposal = validate_selection_proposal(
                _read_json_fd(proposals_fd, f"{proposal_id}.json", "selection proposal")
            )
        inspection = inspect_evidence_selection(dependencies, proposal_id)
        audit_names = _selection_subdir_entries(runtime_root, workspace_id, "audits")
        completion_names = _selection_subdir_entries(runtime_root, workspace_id, "completions")
        if inspection["status"] != "committed" and (audit_names or completion_names):
            return _selection_recovery_readback(
                workspace_id,
                status="investigate",
                reason_code="foreign_selection_state",
                proposal_ids=[proposal_id],
                candidate_ids=[],
            )
        if inspection["status"] == "investigate":
            try:
                matches = [
                    item
                    for item in _load_matching_authorizations(runtime_root, workspace_id, proposal_id)
                    if item["proposal_id"] == proposal_id
                ]
            except EvidenceSelectionError:
                matches = []
            if len(matches) == 1:
                authorization = matches[0]
                transaction_id = "transaction-" + canonical_digest(
                    {
                        "domain": "ao.lore.evidence-selection.transaction-id.v0.1",
                        "proposal_digest": proposal["proposal_digest"],
                        "authorization_digest": authorization["authorization_digest"],
                    }
                )[7:31]
                try:
                    with _open_selection_directory(runtime_root, workspace_id, subdirectory="transactions", create=False) as transactions_fd:
                        if _entry_exists(transactions_fd, f"{transaction_id}.json"):
                            transaction = validate_selection_transaction(
                                _read_json_fd(transactions_fd, f"{transaction_id}.json", "selection transaction")
                            )
                        else:
                            transaction = None
                except EvidenceSelectionError:
                    transaction = None
                if transaction is not None and _recover_committed_suffix(
                    runtime_root,
                    workspace_id,
                    proposal,
                    authorization,
                    transaction,
                    dependencies.candidate_root,
                ):
                    return _selection_recovery_readback(
                        workspace_id,
                        status="recovered",
                        reason_code="recovered_exact_state",
                        proposal_ids=[proposal_id],
                        candidate_ids=[proposal["candidate"]["candidate_id"]],
                    )
            return _selection_recovery_readback(
                workspace_id,
                status="investigate",
                reason_code=inspection["reason_code"],
                proposal_ids=[proposal_id],
                candidate_ids=[] if inspection["candidate_id"] is None else [inspection["candidate_id"]],
            )
        if inspection["status"] == "committed":
            return _selection_recovery_readback(
                workspace_id,
                status="no_op",
                reason_code="completed_terminal_evidence",
                proposal_ids=[proposal_id],
                candidate_ids=[inspection["candidate_id"]],
            )
        if inspection["status"] != "authorized":
            return _selection_recovery_readback(
                workspace_id,
                status="investigate",
                reason_code="ambiguous_selection_state",
                proposal_ids=[proposal_id],
                candidate_ids=[],
            )
        try:
            matches = [
                item
                for item in _load_matching_authorizations(runtime_root, workspace_id, proposal_id)
                if item["proposal_id"] == proposal_id
            ]
        except EvidenceSelectionError:
            matches = []
        if len(matches) != 1:
            return _selection_recovery_readback(
                workspace_id,
                status="investigate",
                reason_code="ambiguous_selection_state",
                proposal_ids=[proposal_id],
                candidate_ids=[],
            )
        result = apply_evidence_selection(
            EvidenceSelectionDependencies(
                runtime_root=runtime_root,
                candidate_root=dependencies.candidate_root,
                source_head=dependencies.source_head,
                now=dependencies.now,
            ),
            proposal_id,
            matches[0]["authorization_id"],
        )
        return _selection_recovery_readback(
            workspace_id,
            status="recovered",
            reason_code="recovered_exact_state",
            proposal_ids=[proposal_id],
            candidate_ids=[result["candidate_id"]],
        )
    finally:
        if selection_fd is not None:
            os.close(selection_fd)
