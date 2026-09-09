"""Private, disposable proof of AO Lore's governed knowledge lifecycle."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, NoReturn

from ._strict_io import (
    ContractError,
    ensure_contained,
    reject_symlink_ancestors,
    require_identifier,
    strict_read_json,
)
from .benchmark import canonical_digest
from .candidates import (
    _CandidateReviewDependencies,
    _SealedCandidateStore,
    _append_review_with_dependencies,
    _sealed_candidate_store_for_sanitized_rehearsal,
    inspect_candidate,
)
from .evidence_graph import (
    GraphDependencies,
    build_evidence_graph,
    claim_record,
    inspect_evidence_graph,
    operational_question_record,
    publish_evidence_graph,
    relationship_edge_record,
)
from .evidence_graph_contracts import AUTHORITY_FIELDS as GRAPH_AUTHORITY_FIELDS
from .evidence_graph_contracts import validate_graph_manifest
from .evidence_selection import (
    _EvidenceSelectionApplyDependencies,
    _apply_evidence_selection_with_dependencies,
    _inspect_evidence_selection_with_candidate_loader,
    EvidenceSelectionDependencies,
    apply_evidence_selection,
    authorize_evidence_selection,
    inspect_evidence_selection,
    prepare_evidence_selection,
)
from .evidence_selection_contracts import validate_selection_inspection
from .document_evidence_query import query_workspace_documents
from .knowledge import (
    _KnowledgeDependencies,
    answer_knowledge,
    knowledge_status,
    search_knowledge,
)
from .knowledge_contracts import origin_for_anchors, origin_identity
from .home import repository_root
from .promotion import (
    _PromotionDependencies,
    _consumption_records,
    _promotion_lock,
    _recovery_events,
    _terminal_retry,
    apply_promotion,
    inspect_promotion,
    prepare_promotion,
    validate_audit_chain,
)
from .sanitized_lifecycle_contracts import validate_sanitized_lifecycle_rehearsal
from .workspace_contracts import AUTHORITY_FIELDS as WORKSPACE_AUTHORITY_FIELDS
from .workspace_documents import (
    _WorkspaceDocumentProofDependencies,
    load_workspace_documents,
    publish_workspace_documents,
)
from .workspace_query import (
    WorkspaceDocumentQuerySnapshot,
    WorkspaceGraphSnapshot,
    WorkspaceQuerySnapshot,
    query_workspace,
)
from .workspace_registry import (
    _WorkspaceRegistryProofDependencies,
    WorkspaceRegistrySnapshot,
    load_workspace_registry,
    publish_workspace_registry_generation,
    select_workspace,
)


_OWNER_NAME = ".ao-lore-sanitized-lifecycle-owner.json"
_OWNER_BYTES = b'{"owner":"ao-lore-sanitized-lifecycle-fixture","schema_version":"v0.1"}\n'
_WORKSPACE_ID = "workspace-sanitized-lifecycle"
_LAYOUT = ("candidate", "brain", "promotions", "workspace-registry", "workspace-documents")
_REPORT_NAME = "sanitized-lifecycle-report.json"
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


class SanitizedLifecycleError(ValueError):
    """The disposable lifecycle proof is absent, unsafe, or inconsistent."""


@dataclass(frozen=True)
class _OfflineCapabilities:
    deny_network: Callable[..., NoReturn]
    deny_provider: Callable[..., NoReturn]


_OFFLINE_CAPABILITIES: ContextVar[_OfflineCapabilities | None] = ContextVar(
    "ao_lore_sanitized_lifecycle_offline_capabilities",
    default=None,
)


def _offline_capabilities() -> _OfflineCapabilities:
    capabilities = _OFFLINE_CAPABILITIES.get()
    if capabilities is None:
        raise SanitizedLifecycleError("lifecycle offline capability is unavailable")
    return capabilities


def _network_entry(*_args: object, **_kwargs: object) -> NoReturn:
    """Route a private network boundary through the current lifecycle context."""

    _offline_capabilities().deny_network("ao-lore-network-denial-guard")
    raise SanitizedLifecycleError("lifecycle denial guard is invalid")


def _provider_gateway(*_args: object, **_kwargs: object) -> NoReturn:
    """Route a private provider boundary through the current lifecycle context."""

    _offline_capabilities().deny_provider("ao-lore-provider-denial-guard")
    raise SanitizedLifecycleError("lifecycle denial guard is invalid")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _hex_digest(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)[:-1]).hexdigest()


def _strip_digest(value: str) -> str:
    if not value.startswith("sha256:") or len(value) != 71:
        raise SanitizedLifecycleError("lifecycle digest binding differs")
    return value[7:]


def _bind(value: dict[str, Any], field: str) -> dict[str, Any]:
    value[field] = canonical_digest(
        {key: item for key, item in value.items() if key != field}
    )
    return value


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev, value.st_ino, value.st_size,
        value.st_mtime_ns, value.st_ctime_ns,
    )


def _read_regular_at(
    parent: int, name: str, *, label: str, maximum: int,
) -> bytes:
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 <= before.st_size <= maximum
        ):
            raise SanitizedLifecycleError(f"{label} is invalid")
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent)
    except (OSError, SanitizedLifecycleError) as exc:
        if isinstance(exc, SanitizedLifecycleError):
            raise
        raise SanitizedLifecycleError(f"{label} is invalid") from exc
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before) or opened.st_nlink != 1:
            raise SanitizedLifecycleError(f"{label} changed during validation")
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
            or _file_identity(after) != _file_identity(opened)
            or _file_identity(rebound) != _file_identity(opened)
            or rebound.st_nlink != 1
        ):
            raise SanitizedLifecycleError(f"{label} changed during validation")
        return body
    finally:
        os.close(descriptor)


def _write_or_verify_exact_child(directory: Path, name: str, body: bytes) -> None:
    try:
        before = os.lstat(directory)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise SanitizedLifecycleError("disposable proof brain differs")
        parent = os.open(directory, _DIRECTORY_FLAGS)
    except (OSError, SanitizedLifecycleError) as exc:
        if isinstance(exc, SanitizedLifecycleError):
            raise
        raise SanitizedLifecycleError("disposable proof brain differs") from exc
    try:
        opened = os.fstat(parent)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise SanitizedLifecycleError("disposable proof brain changed")
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
        except FileExistsError:
            if _read_regular_at(
                parent, name, label="disposable proof brain marker",
                maximum=len(body),
            ) != body:
                raise SanitizedLifecycleError("disposable proof brain differs")
        except OSError as exc:
            raise SanitizedLifecycleError("disposable proof brain differs") from exc
        else:
            try:
                view = memoryview(body)
                while view:
                    view = view[os.write(descriptor, view):]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(parent)
        if _read_regular_at(
            parent, name, label="disposable proof brain marker",
            maximum=len(body),
        ) != body:
            raise SanitizedLifecycleError("disposable proof brain differs")
        rebound = os.stat(directory, follow_symlinks=False)
        if (rebound.st_dev, rebound.st_ino) != (opened.st_dev, opened.st_ino):
            raise SanitizedLifecycleError("disposable proof brain changed")
    finally:
        os.close(parent)


def _validate_root(root: Path) -> Path:
    allowed_parent = repository_root() / "working" / "candidates"
    try:
        selected, _ = ensure_contained(Path(root), allowed_parent, "proof root")
        if selected == allowed_parent:
            raise ContractError("proof root must be disposable")
        reject_symlink_ancestors(selected, include_self=True)
        info = os.lstat(selected)
        owner = selected / _OWNER_NAME
        owner_value, owner_body = strict_read_json(
            owner, "proof owner", max_bytes=256, root=selected,
        )
    except (ContractError, OSError) as exc:
        raise SanitizedLifecycleError("disposable proof root is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or owner_body != _OWNER_BYTES
        or owner_value
        != {"owner": "ao-lore-sanitized-lifecycle-fixture", "schema_version": "v0.1"}
    ):
        raise SanitizedLifecycleError("disposable proof root is invalid")
    fixture = selected / "fixture"
    fixture_info = os.lstat(fixture)
    if stat.S_ISLNK(fixture_info.st_mode) or not stat.S_ISDIR(fixture_info.st_mode):
        raise SanitizedLifecycleError("synthetic fixture root is invalid")
    return selected


def _validate_fixed_rehearsal_root(root: Path) -> Path:
    campaign = repository_root() / ".ao-lore" / "sanitized-lifecycle-rehearsal"
    try:
        selected, _ = ensure_contained(Path(root), campaign, "fixed rehearsal root")
        if selected.parent != campaign:
            raise ContractError("fixed rehearsal root must be a direct campaign child")
        require_identifier(selected.name, "fixed rehearsal run")
        reject_symlink_ancestors(selected, include_self=True)
        info = os.lstat(selected)
        owner, owner_body = strict_read_json(
            selected / _OWNER_NAME,
            "fixed rehearsal owner",
            max_bytes=256,
            root=selected,
        )
        owner_info = os.lstat(selected / _OWNER_NAME)
    except (ContractError, OSError) as exc:
        raise SanitizedLifecycleError("fixed rehearsal root is invalid") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or owner_body != _OWNER_BYTES
        or owner
        != {"owner": "ao-lore-sanitized-lifecycle-fixture", "schema_version": "v0.1"}
        or not stat.S_ISREG(owner_info.st_mode)
        or owner_info.st_nlink != 1
    ):
        raise SanitizedLifecycleError("fixed rehearsal root is invalid")
    return selected


def _strict_artifact(path: Path, label: str, root: Path) -> tuple[dict[str, Any], bytes]:
    try:
        value, body = strict_read_json(
            path, label, max_bytes=_MAX_ARTIFACT_BYTES, root=root,
        )
    except (ContractError, OSError) as exc:
        raise SanitizedLifecycleError("retained lifecycle state is invalid") from exc
    if type(value) is not dict:
        raise SanitizedLifecycleError("retained lifecycle state is invalid")
    return value, body


def _artifact_inventory(
    *,
    candidate_body: bytes,
    provenance_body: bytes,
    review_body: bytes,
    proposal_body: bytes,
    authorization_body: bytes,
    entry_body: bytes,
    manifest_body: bytes,
    origin_readback_binding: dict[str, Any],
    promotion_evidence: dict[str, str],
    workspace_evidence: dict[str, str],
) -> dict[str, str]:
    return {
        "candidate_body_digest": hashlib.sha256(candidate_body).hexdigest(),
        "provenance_body_digest": hashlib.sha256(provenance_body).hexdigest(),
        "review_body_digest": hashlib.sha256(review_body).hexdigest(),
        "proposal_body_digest": hashlib.sha256(proposal_body).hexdigest(),
        "authorization_body_digest": hashlib.sha256(authorization_body).hexdigest(),
        "canonical_entry_body_digest": hashlib.sha256(entry_body).hexdigest(),
        "generation_manifest_body_digest": hashlib.sha256(manifest_body).hexdigest(),
        "origin_readback_binding_digest": canonical_digest(origin_readback_binding),
        "promotion_evidence_digest": _hex_digest(promotion_evidence),
        "workspace_evidence_digest": _hex_digest(workspace_evidence),
    }


def _strict_directory_inventory(
    directory: Path, label: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, bytes]]:
    try:
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SanitizedLifecycleError("retained promotion evidence is invalid")
        entries = sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        raise SanitizedLifecycleError("retained promotion evidence is invalid") from exc
    values: dict[str, dict[str, Any]] = {}
    bodies: dict[str, bytes] = {}
    for entry in entries:
        value, body = _strict_artifact(entry, label, directory)
        values[entry.name] = value
        bodies[entry.name] = body
    return values, bodies


def _validate_closed_root_inventory(
    directory: Path,
    *,
    expected_files: set[str],
    expected_directories: set[str],
    label: str,
) -> None:
    try:
        before = os.lstat(directory)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise SanitizedLifecycleError(f"{label} inventory differs")
        descriptor = os.open(directory, _DIRECTORY_FLAGS)
    except (OSError, SanitizedLifecycleError) as exc:
        if isinstance(exc, SanitizedLifecycleError):
            raise
        raise SanitizedLifecycleError(f"{label} inventory differs") from exc
    try:
        opened = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(opened):
            raise SanitizedLifecycleError(f"{label} inventory changed")
        observed: set[str] = set()
        with os.scandir(descriptor) as entries:
            for entry in entries:
                name = entry.name
                observed.add(name)
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if name in expected_files:
                    valid = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                elif name in expected_directories:
                    valid = stat.S_ISDIR(info.st_mode)
                else:
                    valid = False
                if not valid:
                    raise SanitizedLifecycleError(f"{label} inventory differs")
        if observed != expected_files | expected_directories:
            raise SanitizedLifecycleError(f"{label} inventory differs")
        after = os.fstat(descriptor)
        rebound = os.stat(directory, follow_symlinks=False)
        if (
            _file_identity(opened) != _file_identity(after)
            or _file_identity(opened) != _file_identity(rebound)
        ):
            raise SanitizedLifecycleError(f"{label} inventory changed")
    except OSError as exc:
        raise SanitizedLifecycleError(f"{label} inventory changed") from exc
    finally:
        os.close(descriptor)


def _stable_tree_inventory(root: Path, label: str) -> dict[str, str]:
    result: dict[str, str] = {".": "directory"}
    entry_count = 0
    byte_count = 0

    def visit(directory: int, prefix: str) -> None:
        nonlocal entry_count, byte_count
        opened = os.fstat(directory)
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            raise SanitizedLifecycleError(f"{label} inventory changed") from exc
        for name in names:
            entry_count += 1
            if entry_count > 512:
                raise SanitizedLifecycleError(f"{label} inventory differs")
            relative = name if not prefix else f"{prefix}/{name}"
            try:
                before = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISREG(before.st_mode):
                    if before.st_nlink != 1:
                        raise SanitizedLifecycleError(f"{label} inventory differs")
                    body = _read_regular_at(
                        directory,
                        name,
                        label=f"{label} artifact",
                        maximum=_MAX_ARTIFACT_BYTES,
                    )
                    byte_count += len(body)
                    if byte_count > 32 * 1024 * 1024:
                        raise SanitizedLifecycleError(f"{label} inventory differs")
                    result[relative] = hashlib.sha256(body).hexdigest()
                elif stat.S_ISDIR(before.st_mode):
                    child = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory)
                    try:
                        child_opened = os.fstat(child)
                        if _file_identity(before) != _file_identity(child_opened):
                            raise SanitizedLifecycleError(
                                f"{label} inventory changed"
                            )
                        result[relative] = "directory"
                        visit(child, relative)
                        child_after = os.fstat(child)
                        child_rebound = os.stat(
                            name, dir_fd=directory, follow_symlinks=False
                        )
                        if (
                            _file_identity(child_opened)
                            != _file_identity(child_after)
                            or _file_identity(child_opened)
                            != _file_identity(child_rebound)
                        ):
                            raise SanitizedLifecycleError(
                                f"{label} inventory changed"
                            )
                    finally:
                        os.close(child)
                else:
                    raise SanitizedLifecycleError(f"{label} inventory differs")
            except OSError as exc:
                raise SanitizedLifecycleError(f"{label} inventory changed") from exc
        after = os.fstat(directory)
        if _file_identity(opened) != _file_identity(after):
            raise SanitizedLifecycleError(f"{label} inventory changed")

    try:
        before = os.lstat(root)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise SanitizedLifecycleError(f"{label} inventory differs")
        descriptor = os.open(root, _DIRECTORY_FLAGS)
    except (OSError, SanitizedLifecycleError) as exc:
        if isinstance(exc, SanitizedLifecycleError):
            raise
        raise SanitizedLifecycleError(f"{label} inventory differs") from exc
    try:
        opened = os.fstat(descriptor)
        if _file_identity(before) != _file_identity(opened):
            raise SanitizedLifecycleError(f"{label} inventory changed")
        visit(descriptor, "")
        rebound = os.stat(root, follow_symlinks=False)
        if _file_identity(opened) != _file_identity(rebound):
            raise SanitizedLifecycleError(f"{label} inventory changed")
        return result
    finally:
        os.close(descriptor)


def _workspace_evidence_inventory(
    root: Path,
    *,
    source_head: str,
    now: str,
    candidate_id: str,
    candidate_root: Path | None = None,
    candidate_loader: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, str]:
    runtime_root = root / "workspace-registry"
    try:
        registry = load_workspace_registry(
            _WorkspaceRegistryProofDependencies(runtime_root, clock=lambda: now)
        )
        selection = select_workspace(registry, _WORKSPACE_ID)
        documents = load_workspace_documents(
            _WorkspaceDocumentProofDependencies(runtime_root, clock=lambda: now),
            _WORKSPACE_ID,
        ).generation
        if documents is None:
            raise SanitizedLifecycleError("retained workspace evidence differs")
        graph_digest = selection.primary.get("graph_digest")
        graph_id = selection.primary.get("graph_id")
        if type(graph_digest) is not str or type(graph_id) is not str:
            raise SanitizedLifecycleError("retained workspace evidence differs")
        graph_root = (
            runtime_root / "workspaces" / "state" / _WORKSPACE_ID / "graph"
        )
        graph, _ = _strict_artifact(
            graph_root / "generations" / f"{_strip_digest(graph_digest)}.json",
            "retained graph generation",
            graph_root,
        )
        graph = validate_graph_manifest(graph)
        inspection = inspect_evidence_graph(graph, as_of_date=now[:10])
        if (
            graph.get("graph_id") != graph_id
            or graph.get("graph_digest") != graph_digest
            or inspection.get("result") != "pass"
        ):
            raise SanitizedLifecycleError("retained workspace evidence differs")
        proposal_root = (
            runtime_root
            / "workspaces"
            / "state"
            / _WORKSPACE_ID
            / "selection"
            / "proposals"
        )
        proposals, _ = _strict_directory_inventory(
            proposal_root, "retained selection proposal"
        )
        if len(proposals) != 1:
            raise SanitizedLifecycleError("retained workspace evidence differs")
        proposal = next(iter(proposals.values()))
        proposal_id = proposal.get("proposal_id")
        if type(proposal_id) is not str:
            raise SanitizedLifecycleError("retained workspace evidence differs")
        selection_dependencies = EvidenceSelectionDependencies(
                runtime_root=runtime_root,
                candidate_root=(root / "candidate" if candidate_root is None else candidate_root),
                source_head=lambda: source_head,
                now=lambda: now,
        )
        selection_inspection = (
            inspect_evidence_selection(selection_dependencies, proposal_id)
            if candidate_loader is None
            else _inspect_evidence_selection_with_candidate_loader(
                selection_dependencies,
                proposal_id,
                candidate_loader=candidate_loader,
            )
        )
        if (
            (
                selection_inspection.get("status"),
                selection_inspection.get("reason_code"),
            )
            != ("investigate", "committed_state_invalid")
            or selection_inspection.get("candidate_id") != candidate_id
        ):
            raise SanitizedLifecycleError("retained workspace evidence differs")
        selection_root = proposal_root.parent
        audits, _ = _strict_directory_inventory(
            selection_root / "audits", "retained selection audit"
        )
        completions, _ = _strict_directory_inventory(
            selection_root / "completions", "retained selection completion"
        )
        if len(audits) != 1 or len(completions) != 1:
            raise SanitizedLifecycleError("retained workspace evidence differs")
        committed_selection = validate_selection_inspection(
            next(iter(audits.values()))
        )
        if (
            committed_selection != next(iter(completions.values()))
            or committed_selection.get("status") != "committed"
            or committed_selection.get("reason_code") != "ok"
            or committed_selection.get("candidate_id") != candidate_id
        ):
            raise SanitizedLifecycleError("retained workspace evidence differs")
        variable_terminal_prefixes = (
            "workspaces/registry/recovery/",
            (
                "workspaces/state/"
                f"{_WORKSPACE_ID}/documents/recovery/"
            ),
        )
        result = {
            f"workspace-registry/{name}": digest
            for name, digest in _stable_tree_inventory(
                runtime_root, "retained workspace evidence"
            ).items()
            if not name.startswith(variable_terminal_prefixes)
        }
        result.update({
            f"workspace-documents/{name}": digest
            for name, digest in _stable_tree_inventory(
                root / "workspace-documents", "retained workspace evidence"
            ).items()
        })
        result["derived/registry-digest"] = registry.generations[-1][
            "registry_digest"
        ]
        result["derived/document-generation-digest"] = documents[
            "generation_digest"
        ]
        result["derived/graph-digest"] = graph_digest
        result["derived/selection-inspection-digest"] = selection_inspection[
            "inspection_digest"
        ]
        result["derived/selection-commit-digest"] = committed_selection[
            "inspection_digest"
        ]
        return result
    except SanitizedLifecycleError:
        raise
    except (ContractError, KeyError, OSError, TypeError, ValueError) as exc:
        raise SanitizedLifecycleError("retained workspace evidence differs") from exc


def _promotion_evidence_inventory(
    dependencies: _PromotionDependencies,
    proposal: dict[str, Any],
    authorization: dict[str, Any],
    authorization_digest: str,
    expected_result_digest: str,
) -> dict[str, str]:
    audit_events = validate_audit_chain(dependencies)
    inspection = inspect_promotion(
        proposal["promotion_id"], dependencies=dependencies,
    )
    terminal = _terminal_retry(
        proposal, authorization, authorization_digest, dependencies,
    )
    if terminal is None:
        raise SanitizedLifecycleError("retained terminal transaction is absent")
    transaction_id = terminal["transaction_id"]
    if (
        terminal.get("status") != "committed"
        or terminal.get("result_digest") != expected_result_digest
        or inspection.get("status") != "committed"
        or inspection.get("operation") != "apply"
        or inspection.get("transaction_id") != transaction_id
        or inspection.get("transaction_digest") != expected_result_digest
        or inspection.get("audit_head_digest")
        != (audit_events[-1]["event_digest"] if audit_events else None)
        or inspection.get("canonical_state_valid") is not True
        or inspection.get("recovery_classification") != "no_op"
    ):
        raise SanitizedLifecycleError("retained promotion inspection differs")
    consumptions = _consumption_records(dependencies)
    recovery_events = _recovery_events(dependencies, transaction_id)
    if (
        [item.get("state") for item in consumptions] != ["committed", "reserved"]
        or {item.get("authorization_digest") for item in consumptions}
        != {authorization_digest}
        or [item.get("phase") for item in recovery_events]
        != ["authorized", "staged", "generation_published", "terminal"]
        or any(item.get("transaction_id") != transaction_id for item in recovery_events)
    ):
        raise SanitizedLifecycleError("retained promotion lifecycle evidence differs")

    directories = {
        "transactions": dependencies.promotions_root / "transactions",
        "consumed": dependencies.promotions_root / "consumed",
        "retained": dependencies.promotions_root / "retained",
        "audit": dependencies.promotions_root / "audit",
        "recovery": dependencies.promotions_root / "recovery",
    }
    values: dict[str, dict[str, dict[str, Any]]] = {}
    bodies: dict[str, dict[str, bytes]] = {}
    for name, directory in directories.items():
        values[name], bodies[name] = _strict_directory_inventory(
            directory, f"retained promotion {name} evidence",
        )
    expected_names = {
        "transactions": {f"{transaction_id}.json"},
        "consumed": {
            f"{authorization['authorization_id']}-reserved.json",
            f"{authorization['authorization_id']}-committed.json",
        },
        "retained": {f"{authorization_digest[7:]}.json"},
        "audit": {
            f"{item['sequence']:06d}-{item['event_digest'][7:]}.json"
            for item in audit_events
        },
        "recovery": {
            f"{transaction_id}-{item['sequence']:06d}-{item['event_digest'][7:]}.json"
            for item in recovery_events
        },
    }
    if any(set(values[name]) != expected_names[name] for name in directories):
        raise SanitizedLifecycleError("retained promotion evidence inventory differs")
    if (
        list(values["audit"].values()) != audit_events
        or list(values["consumed"].values()) != consumptions
        or list(values["recovery"].values()) != recovery_events
        or next(iter(values["retained"].values())) != authorization
        or next(iter(values["transactions"].values())).get("transaction_digest")
        != expected_result_digest
    ):
        raise SanitizedLifecycleError("retained promotion evidence content differs")
    result = {
        f"{directory}/{name}": hashlib.sha256(body).hexdigest()
        for directory in sorted(bodies)
        for name, body in sorted(bodies[directory].items())
    }
    result["derived/inspection.json"] = hashlib.sha256(
        _json_bytes(inspection)
    ).hexdigest()
    result["derived/terminal-readback.json"] = hashlib.sha256(
        _json_bytes(terminal)
    ).hexdigest()
    return result


def _origin_readback_binding(
    entry: dict[str, Any], status: dict[str, Any], search: dict[str, Any],
    answer: dict[str, Any], unrelated_source_id: str,
) -> dict[str, Any]:
    citations_by_id = {item["citation_id"]: item for item in entry["citations"]}
    expected_by_claim = {}
    for claim in entry["claims"]:
        citation = citations_by_id[claim["citation_id"]]
        expected_by_claim[claim["claim_id"]] = {
            "citation_id": citation["citation_id"],
            "origin_identity": origin_identity(
                origin_for_anchors(entry["evidence_origins"], claim["source_block_ids"])
            ),
            "source_ref": f"canonical:{entry['canonical_entry_id']}#{citation['citation_id']}",
        }
    if (
        status.get("schema_version") != "ao.lore.knowledge-status-readback.v0.2"
        or search.get("schema_version") != "ao.lore.knowledge-search-readback.v0.2"
        or answer.get("schema_version") != "ao.lore.knowledge-answer-readback.v0.2"
        or status.get("answerable_v0_4_entry_count") != 1
        or answer.get("status") != "answer"
        or set(expected_by_claim) != {item.get("claim_id") for item in search["hits"]}
        or unrelated_source_id in json.dumps((entry, search, answer), sort_keys=True)
    ):
        raise SanitizedLifecycleError("canonical v0.4 readback differs")
    for hit in search["hits"]:
        expected = expected_by_claim[hit["claim_id"]]
        if (
            hit.get("canonical_entry_id") != entry["canonical_entry_id"]
            or hit.get("citation_id") != expected["citation_id"]
            or hit.get("origin_identity") != expected["origin_identity"]
            or hit.get("source_ref") != expected["source_ref"]
        ):
            raise SanitizedLifecycleError("canonical search origin binding differs")
    search_by_claim = {item["claim_id"]: item for item in search["hits"]}
    claim_ids = answer.get("claim_ids", [])
    evidence_ids = answer.get("evidence_ids", [])
    expected_citations = [
        {
            "source_ref": search_by_claim[claim_id]["source_ref"],
            "origin_identity": search_by_claim[claim_id]["origin_identity"],
        }
        for claim_id in claim_ids if claim_id in search_by_claim
    ]
    if (
        not claim_ids
        or len(claim_ids) != len(evidence_ids)
        or any(
            claim_id not in search_by_claim
            or evidence_id != search_by_claim[claim_id]["evidence_id"]
            for claim_id, evidence_id in zip(claim_ids, evidence_ids, strict=True)
        )
        or {_json_bytes(item) for item in answer.get("citations", [])}
        != {_json_bytes(item) for item in expected_citations}
    ):
        raise SanitizedLifecycleError("canonical answer origin binding differs")
    return {
        "status_schema_version": status["schema_version"],
        "search_schema_version": search["schema_version"],
        "answer_schema_version": answer["schema_version"],
        "promoted_origin_identities": [
            origin_identity(item) for item in entry["evidence_origins"]
        ],
        "search": [
            {
                key: item[key] for key in (
                    "claim_id", "evidence_id", "citation_id", "source_ref",
                    "origin_identity",
                )
            }
            for item in search["hits"]
        ],
        "answer_claim_ids": answer["claim_ids"],
        "answer_evidence_ids": answer["evidence_ids"],
        "answer_citations": answer["citations"],
    }


def _validate_replay_state(
    root: Path,
    report: dict[str, object],
    bundle: dict[str, Any],
    source_head: str,
    *,
    candidate_root: Path | None = None,
    candidate_store: _SealedCandidateStore | None = None,
) -> None:
    selected_candidate_root = root / "candidate" if candidate_root is None else candidate_root
    fixed_layout = selected_candidate_root != root / "candidate"
    _validate_closed_root_inventory(
        root,
        expected_files={_OWNER_NAME, _REPORT_NAME},
        expected_directories=(
            {"fixture", "working", *(_LAYOUT[1:])}
            if fixed_layout
            else {"fixture", *_LAYOUT}
        ),
        label="retained proof root",
    )
    if _hex_digest(bundle) != report.get("corpus_digest"):
        raise SanitizedLifecycleError("retained fixture binding differs")
    if fixed_layout:
        _validate_closed_root_inventory(
            root / "working",
            expected_files=set(),
            expected_directories={"candidates"},
            label="retained proof working root",
        )
    retained_paths = {
        name: (selected_candidate_root if name == "candidate" else root / name)
        for name in _LAYOUT
    }
    for name in _LAYOUT:
        info = os.lstat(retained_paths[name])
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SanitizedLifecycleError("retained proof layout differs")
    candidate_ids = report["candidate_ids"]
    canonical_ids = report["canonical_entry_ids"]
    if type(candidate_ids) is not list or type(canonical_ids) is not list:
        raise SanitizedLifecycleError("retained proof identities differ")
    proposal_files = sorted((root / "promotions" / "proposals").iterdir())
    if len(proposal_files) != 1:
        raise SanitizedLifecycleError("retained proposal inventory differs")
    proposal, proposal_body = _strict_artifact(
        proposal_files[0], "retained proposal", root / "promotions",
    )
    if (
        proposal.get("schema_version") != "ao.lore.promotion-proposal.v0.2"
        or proposal.get("proposal_digest")
        != "sha256:" + report["proposal_digest"]
        or proposal.get("source_head") != source_head
    ):
        raise SanitizedLifecycleError("retained proposal binding differs")
    candidate_root = selected_candidate_root
    if fixed_layout:
        if candidate_store is None:
            raise SanitizedLifecycleError("sealed candidate store is unavailable")
        candidate_directories = candidate_store.candidate_ids()
    else:
        _validate_closed_root_inventory(
            candidate_root,
            expected_files={".candidate.lock"},
            expected_directories=set(candidate_ids),
            label="retained candidate",
        )
        candidate_directories = []
        for item in candidate_root.iterdir():
            info = os.lstat(item)
            if stat.S_ISLNK(info.st_mode):
                raise SanitizedLifecycleError("retained candidate inventory differs")
            if stat.S_ISDIR(info.st_mode):
                candidate_directories.append(item.name)
        candidate_directories.sort()
    if candidate_directories != candidate_ids:
        raise SanitizedLifecycleError("retained candidate inventory differs")
    candidate_directory = candidate_root / candidate_ids[0]
    if fixed_layout:
        artifacts = candidate_store.artifacts(candidate_ids[0])
        candidate, candidate_body = artifacts["candidate.json"]
        provenance, provenance_body = artifacts["provenance.json"]
    else:
        _validate_closed_root_inventory(
            candidate_directory,
            expected_files={"candidate.json", "provenance.json"},
            expected_directories={"reviews"},
            label="retained candidate entry",
        )
        candidate, candidate_body = _strict_artifact(
            candidate_directory / "candidate.json", "retained candidate", candidate_root,
        )
        provenance, provenance_body = _strict_artifact(
            candidate_directory / "provenance.json", "retained provenance", candidate_root,
        )
    if (
        candidate.get("schema_version") != "ao.lore.okf-candidate.v0.3"
        or provenance.get("schema_version") != "ao.lore.candidate-provenance.v0.3"
        or canonical_digest(candidate) != "sha256:" + report["candidate_digest"]
        or provenance.get("candidate_digest") != canonical_digest(candidate)
        or candidate.get("evidence_origins") != provenance.get("evidence_origins")
    ):
        raise SanitizedLifecycleError("retained candidate lineage differs")
    review_name = f"000001-{report['review_digest']}.json"
    if fixed_layout:
        review_events = artifacts["reviews"]
        if len(review_events) != 1 or review_events[0][0] != review_name:
            raise SanitizedLifecycleError("retained review inventory differs")
        _, review, review_body = review_events[0]
    else:
        reviews = candidate_directory / "reviews"
        _validate_closed_root_inventory(
            reviews,
            expected_files={review_name},
            expected_directories=set(),
            label="retained review",
        )
        review, review_body = _strict_artifact(
            reviews / review_name, "retained review", reviews,
        )
    if review.get("event_digest") != "sha256:" + report["review_digest"]:
        raise SanitizedLifecycleError("retained review binding differs")
    if fixed_layout:
        inspection = candidate_store.inspect(candidate_ids[0])
    else:
        inspection = inspect_candidate(
            candidate_ids[0], candidate_root=candidate_root,
        )
    if inspection["review_status"] != "accepted" or inspection["verified_review_events"] != 1:
        raise SanitizedLifecycleError("retained review chain differs")
    if (
        proposal.get("candidate_digest") != canonical_digest(candidate)
        or proposal.get("provenance_digest") != canonical_digest(provenance)
        or proposal.get("accepted_review_head_digest") != review.get("event_digest")
    ):
        raise SanitizedLifecycleError("retained proposal binding differs")
    write_set = proposal.get("allowed_write_set")
    if type(write_set) is not list or len(write_set) != 2:
        raise SanitizedLifecycleError("retained canonical write set differs")
    entry_path = root / "brain" / write_set[0]
    manifest_path = root / "brain" / write_set[1]
    _validate_closed_root_inventory(
        root / "brain",
        expected_files={"README.md"},
        expected_directories={"generations"},
        label="retained brain",
    )
    _validate_closed_root_inventory(
        root / "promotions",
        expected_files={
            "authorization-sanitized-lifecycle.json",
            "promotion.lock",
        },
        expected_directories={
            "audit",
            "consumed",
            "proposals",
            "recovery",
            "retained",
            "staging",
            "transactions",
        },
        label="retained promotions",
    )
    entry, entry_body = _strict_artifact(entry_path, "retained canonical entry", root / "brain")
    manifest, manifest_body = _strict_artifact(
        manifest_path, "retained generation manifest", root / "brain",
    )
    if entry != proposal.get("canonical_entry"):
        raise SanitizedLifecycleError("retained canonical content differs")
    if entry.get("canonical_entry_id") != canonical_ids[0]:
        raise SanitizedLifecycleError("retained canonical identity differs")
    if proposal.get("canonical_entry_digest") != canonical_digest(entry):
        raise SanitizedLifecycleError("retained canonical digest differs")
    if manifest.get("manifest_digest") != "sha256:" + report["generation_digest"]:
        raise SanitizedLifecycleError("retained canonical binding differs")
    authorization, authorization_body = _strict_artifact(
        root / "promotions" / "authorization-sanitized-lifecycle.json",
        "retained authorization", root / "promotions",
    )
    if (
        authorization.get("proposal_digest") != proposal.get("proposal_digest")
        or hashlib.sha256(authorization_body).hexdigest()
        != report["authorization_digest"]
    ):
        raise SanitizedLifecycleError("retained authorization binding differs")
    promotion_dependencies = _PromotionDependencies(
        candidate_root, root / "brain", root / "promotions",
        lambda: report["completed_at"], lambda: proposal["source_head"],
        candidate_loader=(
            None if candidate_store is None else candidate_store.promotion_state
        ),
    )
    promotion_evidence = _promotion_evidence_inventory(
        promotion_dependencies, proposal, authorization,
        "sha256:" + report["authorization_digest"],
        "sha256:" + report["promotion_digest"],
    )
    dependencies = _KnowledgeDependencies(
        root / "brain", root / "promotions" / "promotion.lock",
    )
    status = knowledge_status(_dependencies=dependencies)
    search = search_knowledge(
        "expense receipts remote schedules", _dependencies=dependencies,
    )
    answer = answer_knowledge(
        "When are expense receipts submitted and remote schedules approved?",
        _dependencies=dependencies,
    )
    unrelated_source_id = next(
        item["source_id"] for item in bundle["claims"]
        if item["document_id"] == bundle["unrelated_document_id"]
    )
    binding = _origin_readback_binding(
        entry, status, search, answer, unrelated_source_id,
    )
    workspace_evidence = _workspace_evidence_inventory(
        root,
        source_head=source_head,
        now=report["completed_at"],
        candidate_id=candidate_ids[0],
        candidate_root=candidate_root,
        candidate_loader=(None if candidate_store is None else candidate_store.load),
    )
    inventory = _artifact_inventory(
        candidate_body=candidate_body, provenance_body=provenance_body,
        review_body=review_body, proposal_body=proposal_body,
        authorization_body=authorization_body, entry_body=entry_body,
        manifest_body=manifest_body, origin_readback_binding=binding,
        promotion_evidence=promotion_evidence,
        workspace_evidence=workspace_evidence,
    )
    if (
        _hex_digest(search) != report["search_digest"]
        or _hex_digest(answer) != report["answer_digest"]
        or _hex_digest(inventory) != report["inventory_digest"]
    ):
        raise SanitizedLifecycleError("retained lifecycle replay differs")


def _ensure_layout(
    root: Path, *, candidate_root: Path | None = None,
) -> dict[str, Path]:
    paths = {name: root / name for name in _LAYOUT}
    if candidate_root is not None:
        working = root / "working"
        working.mkdir(mode=0o700, exist_ok=True)
        paths["candidate"] = candidate_root
    for path in paths.values():
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SanitizedLifecycleError("disposable proof layout is invalid")
    _write_or_verify_exact_child(
        paths["brain"], "README.md", b"# Disposable synthetic proof brain\n",
    )
    generations = paths["brain"] / "generations"
    generations.mkdir(mode=0o700, exist_ok=True)
    generation_info = os.lstat(generations)
    if stat.S_ISLNK(generation_info.st_mode) or not stat.S_ISDIR(generation_info.st_mode):
        raise SanitizedLifecycleError("disposable proof generations root differs")
    (paths["promotions"] / "proposals").mkdir(mode=0o700, exist_ok=True)
    return paths


def _load_fixture(root: Path) -> dict[str, Any]:
    path = root / "fixture" / "fixture-bundle.json"
    try:
        value, _ = strict_read_json(
            path, "synthetic fixture bundle", max_bytes=64 * 1024,
            root=root / "fixture",
        )
    except (ContractError, OSError) as exc:
        raise SanitizedLifecycleError("synthetic fixture bundle is invalid") from exc
    if (
        type(value) is not dict
        or value.get("schema_version") != "ao.lore.sanitized-lifecycle-fixture.v0.1"
        or value.get("synthetic_public_safe") is not True
        or value.get("network_used") is not False
        or value.get("provider_used") is not False
        or len(value.get("documents", [])) != 3
    ):
        raise SanitizedLifecycleError("synthetic fixture bundle differs")
    return value


def _workspace_definition(
    source_registry_digest: str, graph: dict[str, Any]
) -> dict[str, Any]:
    return _bind({
        "schema_version": "ao.lore.workspace-definition.v0.2",
        "workspace_id": _WORKSPACE_ID,
        "workspace_version": 1,
        "workspace_type": "operations",
        "domain": "synthetic-guidance",
        "jurisdiction": "fixture-scope",
        "lifecycle_status": "active",
        "root_workflow_id": "workflow-policy-compliance",
        "root_workflow_digest": canonical_digest("workflow-policy-compliance"),
        "source_registry_id": "sources-sanitized-lifecycle",
        "source_registry_digest": source_registry_digest,
        "graph_id": graph["graph_id"],
        "graph_digest": graph["graph_digest"],
        "freshness_policy_id": None,
        "freshness_policy_digest": None,
        "freshness_summary_status": "not_observed",
        "freshness_summary_id": None,
        "freshness_summary_digest": None,
        "reference_workspace_ids": [],
        "document_store_id": "documents-sanitized-lifecycle",
        "document_generation_digest": None,
        "definition_digest": "sha256:" + "0" * 64,
        **{field: False for field in WORKSPACE_AUTHORITY_FIELDS},
    }, "definition_digest")


def _synthetic_graph_locator(source_id: str) -> str:
    return (
        "https" + "://" + "source" + "." + "in" + "valid" +
        "/synthetic/record/" + source_id
    )


def _graph_material(bundle: dict[str, Any]) -> tuple[dict[str, Any], str]:
    selected = [
        item for item in bundle["claims"]
        if item["document_id"] in set(bundle["selected_document_ids"])
    ]
    question = operational_question_record(
        prompt="How do expense receipts and remote schedules support compliance?",
        expected_outcome="answer",
        required_authority_roles=["operator_procedure"],
        required_evidence_ids=[],
        forbidden_evidence_ids=[],
        qualifications=["Synthetic public-safe operating guidance only."],
    )
    claims = [
        claim_record(
            source_id=item["source_id"],
            source_digest=item["source_digest"],
            authority_role=item["authority_role"],
            excerpt=item["excerpt"],
            citation_anchor=item["citation_anchor"],
            operational_question_ids=[question["question_id"]],
            subject_terms=item["subject_terms"],
            semantic_reason_codes=item["semantic_reason_codes"],
        )
        for item in selected
    ]
    workflow_digest = canonical_digest("workflow-policy-compliance")
    implementation_edges = [
        relationship_edge_record(
            edge_type="implements",
            source_kind="claim",
            source_id=claim["claim_id"],
            source_evidence_digest=claim["excerpt_digest"],
            target_kind="workflow",
            target_id="workflow-policy-compliance",
            target_evidence_digest=workflow_digest,
            supporting_excerpt_digest=claim["excerpt_digest"],
            reason_code="workflow_implementation",
        )
        for claim in claims
    ]
    citation_edges = [
        relationship_edge_record(
            edge_type="cites",
            source_kind="source",
            source_id=claim["source_id"],
            source_evidence_digest=claim["source_digest"],
            target_kind="claim",
            target_id=claim["claim_id"],
            target_evidence_digest=claim["excerpt_digest"],
            supporting_excerpt_digest=claim["excerpt_digest"],
            reason_code="source_citation",
        )
        for claim in claims
    ]
    edges = [*implementation_edges, *citation_edges]
    sources = []
    for claim, implementation, citation in zip(
        claims, implementation_edges, citation_edges, strict=True,
    ):
        sources.append({
            "source_id": claim["source_id"],
            "source_digest": claim["source_digest"],
            "canonical_locator": _synthetic_graph_locator(claim["source_id"]),
            "retrieved_at": "2026-08-14T12:00:00Z",
            "publisher": "AO Lore synthetic fixture",
            "jurisdiction": "Fixture scope",
            "authority_role": "operator_procedure",
            "version": "fixture-1",
            "effective_date": "2026-08-14",
            "operational_question_ids": [question["question_id"]],
            "relationship_edge_ids": sorted(
                [implementation["edge_id"], citation["edge_id"]]
            ),
            "status": "current",
            "media_type": "text/plain",
            "retained_artifact_digests": [claim["source_digest"]],
        })
    registry = _bind({
        "schema_version": "ao.lore.evidence-source-registry.v0.1",
        "registry_id": "sources-sanitized-lifecycle",
        "root_workflow_id": "workflow-policy-compliance",
        "sources": sources,
        **{field: False for field in GRAPH_AUTHORITY_FIELDS},
    }, "registry_digest")
    question["required_evidence_ids"] = sorted(claim["claim_id"] for claim in claims)
    _bind(question, "question_digest")
    graph = build_evidence_graph(
        registry,
        claims,
        edges,
        [question],
        root_workflow_digest=workflow_digest,
        as_of_date="2026-08-14",
    )
    return graph, registry["registry_digest"]


def _snapshot(runtime_root: Path, graph: dict[str, Any] | None) -> WorkspaceQuerySnapshot:
    registry = load_workspace_registry(_WorkspaceRegistryProofDependencies(runtime_root))
    selection = select_workspace(registry, _WORKSPACE_ID)
    document_generation = load_workspace_documents(
        _WorkspaceDocumentProofDependencies(runtime_root), _WORKSPACE_ID
    ).generation
    if document_generation is None:
        raise SanitizedLifecycleError("workspace documents are unavailable")
    graphs = () if graph is None else (WorkspaceGraphSnapshot(_WORKSPACE_ID, graph),)
    return WorkspaceQuerySnapshot(
        WorkspaceRegistrySnapshot(registry.generations, registry.workspaces),
        selection,
        graphs,
        documents=(WorkspaceDocumentQuerySnapshot(_WORKSPACE_ID, document_generation),),
    )


def _scenario_snapshot(
    snapshot: WorkspaceQuerySnapshot,
    graph: dict[str, Any],
    document_generation: dict[str, Any],
) -> WorkspaceQuerySnapshot:
    generation, _graphs, _documents = snapshot._open_progressive()
    definition = next(
        item for item in generation["workspaces"]
        if item["workspace_id"] == _WORKSPACE_ID
    )
    definition["graph_id"] = graph["graph_id"]
    definition["graph_digest"] = graph["graph_digest"]
    definition["document_generation_digest"] = document_generation["generation_digest"]
    _bind(definition, "definition_digest")
    _bind(generation, "registry_digest")
    registry = WorkspaceRegistrySnapshot((generation,), tuple(generation["workspaces"]))
    return WorkspaceQuerySnapshot(
        registry,
        select_workspace(registry, _WORKSPACE_ID),
        (WorkspaceGraphSnapshot(_WORKSPACE_ID, graph),),
        documents=(
            WorkspaceDocumentQuerySnapshot(_WORKSPACE_ID, document_generation),
        ),
    )


def _require_query_scenario(
    result: dict[str, Any],
    *,
    outcome: str,
    qualifications: list[str],
    empty_evidence: bool,
    expected_origins: set[tuple[str, str]] | None = None,
) -> None:
    actual_origins = [
        (item["evidence_kind"], item["source_id"])
        for item in result["evidence"]
    ]
    if (
        result["outcome"] != outcome
        or result["qualifications"] != qualifications
        or (bool(result["evidence"]) if empty_evidence else not bool(result["evidence"]))
        or (
            expected_origins is not None
            and (
                set(actual_origins) != expected_origins
                or len(actual_origins) != len(expected_origins)
            )
        )
        or any(result[field] is not False for field in WORKSPACE_AUTHORITY_FIELDS)
    ):
        raise SanitizedLifecycleError("neutral query scenario differs")


def _verify_neutral_query_scenarios(
    snapshot: WorkspaceQuerySnapshot,
    graph: dict[str, Any],
    document_generation: dict[str, Any],
) -> None:
    missing = query_workspace(snapshot, _WORKSPACE_ID, "qzeta unmatched")
    _require_query_scenario(
        missing,
        outcome="refuse",
        qualifications=["The connected public graph does not support this question."],
        empty_evidence=True,
    )

    restricted_generation = deepcopy(document_generation)
    next(
        item for item in restricted_generation["documents"]
        if item["document_id"] == "policy-expense-receipts"
    )["sensitivity"] = "restricted"
    _bind(restricted_generation, "generation_digest")
    restricted = query_workspace(
        _scenario_snapshot(snapshot, graph, restricted_generation),
        _WORKSPACE_ID,
        "expense receipts",
    )
    _require_query_scenario(
        restricted,
        outcome="refuse",
        qualifications=[
            "Restricted evidence cannot support a workspace answer.",
            "Restricted evidence cannot be rendered.",
        ],
        empty_evidence=True,
    )

    stale_generation = deepcopy(document_generation)
    next(
        item for item in stale_generation["documents"]
        if item["document_id"] == "policy-expense-receipts"
    )["freshness_status"] = "stale"
    _bind(stale_generation, "generation_digest")
    stale = query_workspace(
        _scenario_snapshot(snapshot, graph, stale_generation),
        _WORKSPACE_ID,
        "expense receipts",
    )
    _require_query_scenario(
        stale,
        outcome="investigate",
        qualifications=["Document freshness requires review."],
        empty_evidence=False,
        expected_origins={
            ("document_block", "source-policy-expense-receipts"),
            ("graph_claim", "source-expense-receipts"),
        },
    )

    conflict_graph = deepcopy(graph)
    left, right = conflict_graph["claims"][:2]
    conflict = relationship_edge_record(
        edge_type="conflicts_with",
        source_kind="claim",
        source_id=left["claim_id"],
        source_evidence_digest=left["excerpt_digest"],
        target_kind="claim",
        target_id=right["claim_id"],
        target_evidence_digest=right["excerpt_digest"],
        supporting_excerpt_digest=left["excerpt_digest"],
        reason_code="documented_conflict",
    )
    conflict_graph["edges"].append(conflict)
    for source in conflict_graph["sources"]:
        if source["source_id"] in {left["source_id"], right["source_id"]}:
            source["relationship_edge_ids"] = sorted({
                *source["relationship_edge_ids"], conflict["edge_id"],
            })
    _bind(conflict_graph, "graph_digest")
    conflict_result = query_workspace(
        _scenario_snapshot(snapshot, conflict_graph, document_generation),
        _WORKSPACE_ID,
        "expense receipts remote schedules",
    )
    _require_query_scenario(
        conflict_result,
        outcome="investigate",
        qualifications=["Conflicting or stale evidence remains unresolved."],
        empty_evidence=False,
        expected_origins={
            ("document_block", "source-policy-expense-receipts"),
            ("document_block", "source-policy-remote-schedules"),
            ("graph_claim", "source-expense-receipts"),
            ("graph_claim", "source-remote-schedules"),
        },
    )
    conflict_ids = {
        item["evidence_id"] for item in conflict_result["evidence"]
        if item["evidence_kind"] == "graph_claim"
    }
    if conflict_ids != {left["claim_id"], right["claim_id"]}:
        raise SanitizedLifecycleError("neutral conflict evidence differs")

    capacity_graph = deepcopy(graph)
    source = capacity_graph["sources"][0]
    source["status"] = "historical"
    question_ids = capacity_graph["claims"][0]["operational_question_ids"]
    for index in range(129):
        claim = claim_record(
            source_id=source["source_id"],
            source_digest=source["source_digest"],
            authority_role=source["authority_role"],
            excerpt=f"Capacity control record {index}.",
            citation_anchor=f"capacity-{index}",
            operational_question_ids=question_ids,
            subject_terms=["capacity", "control", "record"],
            semantic_reason_codes=["topic_match", "citation_supported"],
        )
        capacity_graph["claims"].append(claim)
        edge = relationship_edge_record(
            edge_type="explains",
            source_kind="claim",
            source_id=claim["claim_id"],
            source_evidence_digest=claim["excerpt_digest"],
            target_kind="workflow",
            target_id=capacity_graph["root_workflow_id"],
            target_evidence_digest=capacity_graph["root_workflow_digest"],
            supporting_excerpt_digest=claim["excerpt_digest"],
            reason_code="official_explanation",
        )
        capacity_graph["edges"].append(edge)
        source["relationship_edge_ids"].append(edge["edge_id"])
    _bind(capacity_graph, "graph_digest")
    capacity = query_workspace(
        _scenario_snapshot(snapshot, capacity_graph, document_generation),
        _WORKSPACE_ID,
        "capacity control record",
    )
    _require_query_scenario(
        capacity,
        outcome="investigate",
        qualifications=[
            "Conflicting or stale evidence remains unresolved.",
            "Evidence budget cannot represent every required gate identity.",
        ],
        empty_evidence=True,
    )


def _authorization(proposal: dict[str, Any], now: str) -> dict[str, Any]:
    parsed = datetime.fromisoformat(now.removesuffix("Z") + "+00:00")
    before = (parsed - timedelta(minutes=1)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    after = (parsed + timedelta(minutes=1)).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "schema_version": "ao.lore.promotion-authorization.v0.1",
        "policy_version": "ao.lore.promotion-policy.v0.1",
        "operation": "apply",
        "authorization_id": "authorization-sanitized-lifecycle",
        "nonce": "nonce-sanitized-lifecycle",
        "operator_id": "fixture-operator",
        "authentication_scope": "local_operator_assertion",
        "proposal_digest": proposal["proposal_digest"],
        "promotion_id": proposal["promotion_id"],
        "candidate_digest": proposal["candidate_digest"],
        "provenance_digest": proposal["provenance_digest"],
        "accepted_review_head_digest": proposal["accepted_review_head_digest"],
        "canonical_entry_digest": proposal["canonical_entry_digest"],
        "expected_brain_inventory_digest": proposal["prior_brain_inventory_digest"],
        "expected_prior_generation_id": proposal["prior_generation_id"],
        "expected_result_generation_id": proposal["expected_generation_id"],
        "allowed_write_set_digest": proposal["allowed_write_set_digest"],
        "source_head": proposal["source_head"],
        "issued_at": before,
        "expires_at": after,
        "one_use_only": True,
        "product_self_issued": False,
        "provider_authority": False,
        "network_authority": False,
        "publication_authority": False,
        "release_authority": False,
        "deployment_authority": False,
        "batch_authority": False,
        "overwrite_authority": False,
        "unattended_authority": False,
        "credential_authority": False,
        "authority_advance": False,
    }


def _write_exclusive(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        body = _json_bytes(value)
        view = memoryview(body)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _existing_report(root: Path) -> dict[str, object] | None:
    path = root / _REPORT_NAME
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    try:
        value, body = strict_read_json(
            path, "retained lifecycle report", max_bytes=64 * 1024, root=root,
        )
        validated = validate_sanitized_lifecycle_rehearsal(value)
    except (ContractError, OSError) as exc:
        raise SanitizedLifecycleError("retained lifecycle report is invalid") from exc
    if body != _json_bytes(validated):
        raise SanitizedLifecycleError("retained lifecycle report is invalid")
    return validated


@contextmanager
def _offline_guard(
    deny_network: Callable[..., NoReturn],
    deny_provider: Callable[..., NoReturn],
) -> Iterator[None]:
    if not callable(deny_network) or not callable(deny_provider):
        raise SanitizedLifecycleError("lifecycle denial guard is invalid")
    token = _OFFLINE_CAPABILITIES.set(
        _OfflineCapabilities(
            deny_network=deny_network,
            deny_provider=deny_provider,
        )
    )
    try:
        yield
    finally:
        _OFFLINE_CAPABILITIES.reset(token)


def _run_sanitized_lifecycle(
    root: Path,
    *,
    source_head: str,
    now: str,
    deny_network: Callable[..., NoReturn],
    deny_provider: Callable[..., NoReturn],
    fixed_rehearsal: bool,
    _sealed_store: _SealedCandidateStore | None = None,
    _prepared_paths: dict[str, Path] | None = None,
) -> dict[str, object]:
    """Run one offline governed lifecycle only inside an owned disposable root."""

    selected_root = (
        _validate_fixed_rehearsal_root(Path(root))
        if fixed_rehearsal
        else _validate_root(Path(root))
    )
    candidate_root = (
        selected_root / "working" / "candidates"
        if fixed_rehearsal
        else selected_root / "candidate"
    )
    if not isinstance(source_head, str) or len(source_head) != 40 or any(ch not in "0123456789abcdef" for ch in source_head):
        raise SanitizedLifecycleError("source head is invalid")
    try:
        datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise SanitizedLifecycleError("lifecycle clock is invalid") from exc
    replay = _existing_report(selected_root)
    if replay is not None:
        try:
            if fixed_rehearsal:
                with _sealed_candidate_store_for_sanitized_rehearsal(
                    selected_root
                ) as replay_store:
                    _validate_replay_state(
                        selected_root,
                        replay,
                        _load_fixture(selected_root),
                        source_head,
                        candidate_root=candidate_root,
                        candidate_store=replay_store,
                    )
            else:
                _validate_replay_state(
                    selected_root,
                    replay,
                    _load_fixture(selected_root),
                    source_head,
                    candidate_root=candidate_root,
                )
        except SanitizedLifecycleError:
            raise
        except (ContractError, ValueError, OSError, KeyError, IndexError, TypeError) as exc:
            raise SanitizedLifecycleError(
                "retained lifecycle state is invalid"
            ) from exc
        return replay
    if _prepared_paths is None:
        try:
            _validate_closed_root_inventory(
                selected_root,
                expected_files={_OWNER_NAME},
                expected_directories={"fixture"},
                label="pristine proof root",
            )
        except SanitizedLifecycleError as exc:
            raise SanitizedLifecycleError(
                "retained lifecycle report is missing"
            ) from exc
        paths = _ensure_layout(
            selected_root,
            candidate_root=candidate_root if fixed_rehearsal else None,
        )
        if fixed_rehearsal:
            with _sealed_candidate_store_for_sanitized_rehearsal(
                selected_root
            ) as sealed_store:
                return _run_sanitized_lifecycle(
                    selected_root,
                    source_head=source_head,
                    now=now,
                    deny_network=deny_network,
                    deny_provider=deny_provider,
                    fixed_rehearsal=True,
                    _sealed_store=sealed_store,
                    _prepared_paths=paths,
                )
    else:
        paths = _prepared_paths
        if not fixed_rehearsal or _sealed_store is None:
            raise SanitizedLifecycleError("sealed lifecycle state is invalid")
    bundle = _load_fixture(selected_root)
    graph, source_registry_digest = _graph_material(bundle)
    runtime_root = paths["workspace-registry"]
    registry_dependencies = _WorkspaceRegistryProofDependencies(
        runtime_root, clock=lambda: now,
    )
    document_dependencies = _WorkspaceDocumentProofDependencies(
        runtime_root, clock=lambda: now,
    )
    initial = publish_workspace_registry_generation(
        (_workspace_definition(source_registry_digest, graph),), registry_dependencies
    )
    document_values = []
    for item in bundle["documents"]:
        try:
            document_ir, _ = strict_read_json(
                selected_root / "fixture" / item["document_ir_file"],
                "synthetic document IR", max_bytes=64 * 1024,
                root=selected_root / "fixture",
            )
        except (ContractError, OSError) as exc:
            raise SanitizedLifecycleError("synthetic document IR is invalid") from exc
        for block in document_ir["blocks"]:
            block["source_span"] = {"page": 1, **block["source_span"]}
        document_values.append(document_ir)
    document_irs = tuple(document_values)
    publication = publish_workspace_documents(
        document_dependencies,
        _WORKSPACE_ID,
        document_irs,
        expected_registry_digest=initial["registry_digest"],
        expected_definition_digest=initial["workspaces"][0]["definition_digest"],
    )
    document_generation = load_workspace_documents(
        document_dependencies, _WORKSPACE_ID
    ).generation
    if document_generation is None:
        raise SanitizedLifecycleError("workspace documents are unavailable")
    pre_graph_query = query_workspace_documents(
        document_generation,
        _WORKSPACE_ID,
        "expense receipts and remote-work schedules",
    )
    if not pre_graph_query["evidence"]:
        raise SanitizedLifecycleError("pre-graph document retrieval returned no evidence")

    graph_root = runtime_root / "workspaces" / "state" / _WORKSPACE_ID / "graph"
    graph_root.mkdir(mode=0o700, parents=True)
    publish_evidence_graph(graph, GraphDependencies(graph_root))
    progressive_snapshot = _snapshot(runtime_root, graph)
    progressive_query = query_workspace(
        progressive_snapshot,
        _WORKSPACE_ID,
        "expense receipts and remote-work schedules compliance",
    )
    evidence = progressive_query["evidence"]
    if len(evidence) < 3:
        raise SanitizedLifecycleError("progressive retrieval evidence is incomplete")
    _verify_neutral_query_scenarios(
        progressive_snapshot, graph, document_generation,
    )

    selected_document_ids = set(bundle["selected_document_ids"])
    selected_documents = [
        item for item in document_generation["documents"]
        if item["document_id"] in selected_document_ids
    ]
    document = selected_documents[0]
    graph_source_id = next(
        item["source_id"]
        for item in bundle["claims"]
        if item["document_id"] in selected_document_ids
        and item["document_id"] != document["document_id"]
    )
    document_evidence_id = canonical_digest({
        "workspace_id": _WORKSPACE_ID,
        "generation_digest": document_generation["generation_digest"],
        "document_id": document["document_id"],
        "block_id": document["document_ir"]["blocks"][0]["id"],
    })
    graph_evidence_id = next(
        item["evidence_id"]
        for item in evidence
        if item.get("evidence_kind") == "graph_claim"
        and item.get("source_id") == graph_source_id
    )
    selected_evidence_ids = (document_evidence_id, graph_evidence_id)
    if len(selected_evidence_ids) != 2:
        raise SanitizedLifecycleError("governed document evidence selection differs")
    selection_proposal = prepare_evidence_selection(
        progressive_snapshot, _WORKSPACE_ID, selected_evidence_ids, now=now,
    )
    selection_dependencies = EvidenceSelectionDependencies(
        runtime_root=runtime_root,
        candidate_root=paths["candidate"],
        source_head=lambda: source_head,
        now=lambda: now,
    )
    selection_authorization = authorize_evidence_selection(
        selection_dependencies, selection_proposal, "fixture-operator",
    )
    if fixed_rehearsal:
        apply_dependencies = _EvidenceSelectionApplyDependencies(
            _sealed_store.candidate_lock,
            _sealed_store.persist,
            _sealed_store.load,
            _sealed_store.children,
        )
        selection_result = _apply_evidence_selection_with_dependencies(
            selection_dependencies,
            selection_proposal["proposal_id"],
            selection_authorization["authorization_id"],
            apply_dependencies=apply_dependencies,
        )
        retry_result = _apply_evidence_selection_with_dependencies(
            selection_dependencies,
            selection_proposal["proposal_id"],
            selection_authorization["authorization_id"],
            apply_dependencies=apply_dependencies,
        )
    else:
        selection_result = apply_evidence_selection(
            selection_dependencies,
            selection_proposal["proposal_id"],
            selection_authorization["authorization_id"],
        )
        retry_result = apply_evidence_selection(
            selection_dependencies,
            selection_proposal["proposal_id"],
            selection_authorization["authorization_id"],
        )
    if retry_result != selection_result:
        raise SanitizedLifecycleError("selection exact retry differs")

    candidate_id = selection_result["candidate_id"]
    candidate_path = paths["candidate"] / candidate_id / "candidate.json"
    provenance_path = paths["candidate"] / candidate_id / "provenance.json"
    if fixed_rehearsal:
        candidate_artifacts = _sealed_store.artifacts(candidate_id)
        candidate_value, candidate_before = candidate_artifacts["candidate.json"]
        provenance_value, provenance_before = candidate_artifacts["provenance.json"]
        candidate_count = len(_sealed_store.candidate_ids())
    else:
        candidate_value, candidate_before = _strict_artifact(
            candidate_path, "governed candidate", paths["candidate"],
        )
        provenance_value, provenance_before = _strict_artifact(
            provenance_path, "governed provenance", paths["candidate"],
        )
        candidate_count = len(
            [item for item in paths["candidate"].iterdir() if item.is_dir()]
        )
    unrelated_source_id = next(
        item["source_id"]
        for item in bundle["claims"]
        if item["document_id"] == bundle["unrelated_document_id"]
    )
    if (
        candidate_value.get("schema_version") != "ao.lore.okf-candidate.v0.3"
        or provenance_value.get("schema_version") != "ao.lore.candidate-provenance.v0.3"
        or candidate_value.get("evidence_origins") != provenance_value.get("evidence_origins")
        or {item["evidence_kind"] for item in candidate_value["evidence_origins"]}
        != {"document_block", "graph_claim"}
        or unrelated_source_id
        in {item["source_id"] for item in candidate_value["evidence_origins"]}
        or candidate_count != 1
    ):
        raise SanitizedLifecycleError("governed multi-origin candidate differs")
    promotion_dependencies = _PromotionDependencies(
        paths["candidate"], paths["brain"], paths["promotions"],
        lambda: now, lambda: source_head,
        candidate_loader=(
            None if _sealed_store is None else _sealed_store.promotion_state
        ),
    )

    before_status = knowledge_status(
        _dependencies=_KnowledgeDependencies(paths["brain"], paths["promotions"] / "promotion.lock")
    )
    before_search = search_knowledge(
        "expense receipts", _dependencies=_KnowledgeDependencies(paths["brain"], paths["promotions"] / "promotion.lock")
    )
    before_answer = answer_knowledge(
        "When are expense receipts submitted?",
        _dependencies=_KnowledgeDependencies(paths["brain"], paths["promotions"] / "promotion.lock"),
    )
    if before_status["effective_entry_count"] != 0 or before_search["hits"] or before_answer["status"] != "refuse":
        raise SanitizedLifecycleError("non-canonical candidate leaked into canonical readback")

    @contextmanager
    def review_lock():
        with _promotion_lock(promotion_dependencies):
            yield

    if fixed_rehearsal:
        with review_lock():
            review = _sealed_store.append_review(
                candidate_id,
                "accept",
                "fixture-reviewer",
                "Synthetic public-safe evidence accepted for disposable proof.",
                recorded_at=now,
            )
        reviewed_artifacts = _sealed_store.artifacts(candidate_id)
        candidate_after_review = reviewed_artifacts["candidate.json"][1]
        provenance_after_review = reviewed_artifacts["provenance.json"][1]
    else:
        review = _append_review_with_dependencies(
            candidate_id,
            "accept",
            "fixture-reviewer",
            "Synthetic public-safe evidence accepted for disposable proof.",
            recorded_at=now,
            dependencies=_CandidateReviewDependencies(review_lock, paths["candidate"]),
        )
        candidate_after_review = _strict_artifact(
            candidate_path, "governed candidate", paths["candidate"],
        )[1]
        provenance_after_review = _strict_artifact(
            provenance_path, "governed provenance", paths["candidate"],
        )[1]
    if (
        candidate_before != candidate_after_review
        or provenance_before != provenance_after_review
    ):
        raise SanitizedLifecycleError("candidate bytes changed during review")
    if fixed_rehearsal:
        inspection = _sealed_store.inspect(candidate_id)
    else:
        inspection = inspect_candidate(
            candidate_id, candidate_root=paths["candidate"],
        )
    if inspection["review_status"] != "accepted" or inspection["verified_review_events"] != 1:
        raise SanitizedLifecycleError("candidate review state differs")

    proposal_path = paths["promotions"] / "proposals" / "sanitized-lifecycle.json"
    proposal = prepare_promotion(
        candidate_id, proposal_path, dependencies=promotion_dependencies,
    )
    if proposal["schema_version"] != "ao.lore.promotion-proposal.v0.2" or proposal["canonical_entry"]["schema_version"] != "ao.lore.okf-canonical-entry.v0.4":
        raise SanitizedLifecycleError("multi-origin promotion mapping differs")
    if (
        proposal["canonical_entry"]["evidence_origins"]
        != candidate_value["evidence_origins"]
        or proposal["canonical_entry"]["evidence_selection_digest"]
        != candidate_value["evidence_selection_digest"]
    ):
        raise SanitizedLifecycleError("multi-origin canonical lineage differs")
    authorization = _authorization(proposal, now)
    authorization_path = paths["promotions"] / "authorization-sanitized-lifecycle.json"
    _write_exclusive(authorization_path, authorization)
    promotion = apply_promotion(
        proposal_path, authorization_path, dependencies=promotion_dependencies,
    )
    if apply_promotion(
        proposal_path, authorization_path, dependencies=promotion_dependencies,
    ) != promotion:
        raise SanitizedLifecycleError("promotion exact retry differs")
    if fixed_rehearsal:
        promoted_artifacts = _sealed_store.artifacts(candidate_id)
        candidate_after_promotion = promoted_artifacts["candidate.json"][1]
        provenance_after_promotion = promoted_artifacts["provenance.json"][1]
    else:
        candidate_after_promotion = _strict_artifact(
            candidate_path, "governed candidate", paths["candidate"],
        )[1]
        provenance_after_promotion = _strict_artifact(
            provenance_path, "governed provenance", paths["candidate"],
        )[1]
    if (
        candidate_before != candidate_after_promotion
        or provenance_before != provenance_after_promotion
    ):
        raise SanitizedLifecycleError("candidate bytes changed during promotion")

    knowledge_dependencies = _KnowledgeDependencies(
        paths["brain"], paths["promotions"] / "promotion.lock",
    )
    status = knowledge_status(_dependencies=knowledge_dependencies)
    search = search_knowledge(
        "expense receipts remote schedules", _dependencies=knowledge_dependencies,
    )
    answer = answer_knowledge(
        "When are expense receipts submitted and remote schedules approved?",
        _dependencies=knowledge_dependencies,
    )
    entry = proposal["canonical_entry"]
    origin_readback_binding = _origin_readback_binding(
        entry, status, search, answer, unrelated_source_id,
    )

    evidence_ids = sorted({
        "evidence-" + _strip_digest(item["evidence_digest"])[:24]
        for item in evidence
    })
    document_ids = sorted(item["document_id"] for item in bundle["documents"])
    canonical_entry_id = proposal["canonical_entry"]["canonical_entry_id"]
    if fixed_rehearsal:
        review_events = _sealed_store.artifacts(candidate_id)["reviews"]
        if len(review_events) != 1:
            raise SanitizedLifecycleError("candidate review inventory differs")
        _, _, review_body = review_events[0]
    else:
        review_files = sorted(
            (paths["candidate"] / candidate_id / "reviews").iterdir()
        )
        if len(review_files) != 1:
            raise SanitizedLifecycleError("candidate review inventory differs")
        _, review_body = _strict_artifact(
            review_files[0], "governed review", paths["candidate"],
        )
    stored_proposal, proposal_body = _strict_artifact(
        proposal_path, "governed proposal", paths["promotions"],
    )
    if stored_proposal != proposal:
        raise SanitizedLifecycleError("governed proposal bytes differ")
    stored_authorization, authorization_body = _strict_artifact(
        authorization_path, "governed authorization", paths["promotions"],
    )
    if stored_authorization != authorization:
        raise SanitizedLifecycleError("governed authorization bytes differ")
    entry_path = paths["brain"] / proposal["allowed_write_set"][0]
    manifest_path = paths["brain"] / proposal["allowed_write_set"][1]
    stored_entry, entry_body = _strict_artifact(
        entry_path, "governed canonical entry", paths["brain"],
    )
    _, manifest_body = _strict_artifact(
        manifest_path, "governed generation manifest", paths["brain"],
    )
    if stored_entry != entry:
        raise SanitizedLifecycleError("governed canonical entry differs")
    promotion_evidence = _promotion_evidence_inventory(
        promotion_dependencies, proposal, authorization,
        "sha256:" + hashlib.sha256(authorization_body).hexdigest(),
        promotion["result_digest"],
    )
    workspace_evidence = _workspace_evidence_inventory(
        selected_root,
        source_head=source_head,
        now=now,
        candidate_id=candidate_id,
        candidate_root=paths["candidate"],
        candidate_loader=(None if _sealed_store is None else _sealed_store.load),
    )
    artifact_inventory = _artifact_inventory(
        candidate_body=candidate_before, provenance_body=provenance_before,
        review_body=review_body, proposal_body=proposal_body,
        authorization_body=authorization_body, entry_body=entry_body,
        manifest_body=manifest_body,
        origin_readback_binding=origin_readback_binding,
        promotion_evidence=promotion_evidence,
        workspace_evidence=workspace_evidence,
    )
    report: dict[str, Any] = {
        "schema_version": "ao.lore.sanitized-lifecycle-rehearsal.v0.1",
        "rehearsal_id": "sanitized-lifecycle-proof",
        "completed_at": now,
        "summary": "Offline synthetic governed lifecycle completed deterministically.",
        "corpus_digest": _hex_digest(bundle),
        "document_digest": _strip_digest(publication["generation_digest"]),
        "evidence_digest": _strip_digest(selection_proposal["evidence_selection_digest"]),
        "candidate_digest": _strip_digest(proposal["candidate_digest"]),
        "review_digest": _strip_digest(review["event_digest"]),
        "proposal_digest": _strip_digest(proposal["proposal_digest"]),
        "authorization_digest": hashlib.sha256(authorization_body).hexdigest(),
        "promotion_digest": _strip_digest(promotion["result_digest"]),
        "generation_digest": _strip_digest(status["latest_generation_manifest_digest"]),
        "search_digest": _hex_digest(search),
        "answer_digest": _hex_digest(answer),
        "inventory_digest": _hex_digest(artifact_inventory),
        "document_ids": document_ids,
        "evidence_ids": evidence_ids,
        "candidate_ids": [candidate_id],
        "review_ids": ["review-" + _strip_digest(review["event_digest"])[:24]],
        "canonical_entry_ids": [canonical_entry_id],
        "counts": {
            "documents": len(document_ids),
            "evidence_identities": len(evidence_ids),
            "candidates": 1,
            "reviews": 1,
            "proposals": 1,
            "authorizations": 1,
            "promotions": 1,
            "canonical_entries": 1,
            "search_hits": len(search["hits"]),
            "answers": 1,
        },
        "scenario_statuses": [
            {"scenario_id": "document-retrieval-before-graph", "status": "pass"},
            {"scenario_id": "optional-graph-retrieval", "status": "pass"},
            {"scenario_id": "governed-selection", "status": "pass"},
            {"scenario_id": "accepted-review", "status": "pass"},
            {"scenario_id": "authorized-promotion", "status": "pass"},
            {"scenario_id": "canonical-readback", "status": "pass"},
            {"scenario_id": "origin-qualified-readback", "status": "pass"},
            {"scenario_id": "deterministic-replay", "status": "pass"},
            {"scenario_id": "query-missing", "status": "pass"},
            {"scenario_id": "query-restricted", "status": "pass"},
            {"scenario_id": "query-stale", "status": "pass"},
            {"scenario_id": "query-documented-conflict", "status": "pass"},
            {"scenario_id": "query-capacity-saturation", "status": "pass"},
        ],
        "byte_identical_check": True,
        "network_calls": 0,
        "provider_calls": 0,
        "external_authority": {
            "live_state_accessed": False,
            "customer_data_accessed": False,
            "github_accessed": False,
            "publication_executed": False,
            "release_executed": False,
            "deployment_executed": False,
            "credentials_used": False,
            "authority_advanced": False,
        },
    }
    report["report_digest"] = _hex_digest(report)
    validated = validate_sanitized_lifecycle_rehearsal(report)
    _write_exclusive(selected_root / _REPORT_NAME, validated)
    return validated


def run_sanitized_lifecycle(
    root: Path,
    *,
    source_head: str,
    now: str,
    deny_network: Callable[..., NoReturn],
    deny_provider: Callable[..., NoReturn],
) -> dict[str, object]:
    """Run one offline governed lifecycle in a disposable candidate subtree."""

    with _offline_guard(deny_network, deny_provider):
        return _run_sanitized_lifecycle(
            root,
            source_head=source_head,
            now=now,
            deny_network=deny_network,
            deny_provider=deny_provider,
            fixed_rehearsal=False,
        )


def _run_sanitized_lifecycle_in_rehearsal_run(
    run_root: Path,
    *,
    source_head: str,
    now: str,
    deny_network: Callable[..., NoReturn],
    deny_provider: Callable[..., NoReturn],
) -> dict[str, object]:
    """Private fixed-campaign entry; grants no public or CLI authority."""

    with _offline_guard(deny_network, deny_provider):
        return _run_sanitized_lifecycle(
            run_root,
            source_head=source_head,
            now=now,
            deny_network=deny_network,
            deny_provider=deny_provider,
            fixed_rehearsal=True,
        )


__all__ = ["SanitizedLifecycleError", "run_sanitized_lifecycle"]
