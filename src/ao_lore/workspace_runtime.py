"""Validated descriptor-anchored runtime contexts for isolated workspaces."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from ._strict_io import ContractError
from .benchmark import canonical_digest
from .evidence_graph_contracts import (
    AUTHORITY_FIELDS,
    validate_detached_freshness_summary_against_manifest,
    validate_freshness_policy,
    validate_graph_manifest,
)
from .workspace_contracts import (
    WORKSPACE_SCHEMA_VERSIONS,
    validate_workspace_operation_readback,
)
from .workspace_documents import (
    WorkspaceDocumentDependencies,
    WorkspaceDocumentError,
    WorkspaceDocumentSnapshot,
    load_workspace_documents,
)
from .workspace_registry import (
    WorkspaceRegistryDependencies,
    WorkspaceRegistryError,
    WorkspaceRegistrySnapshot,
    load_workspace_registry,
    select_workspace,
)


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_WORKSPACE_CHILDREN = {"documents", "freshness", "graph", "inbox", "recovery", "sources"}
_MAX_ENTRIES = 4096
_MAX_DEPTH = 8
_MAX_BOUND_JSON_BYTES = 4 * 1024 * 1024
_MAX_WORKSPACE_SOURCE_BYTES = 50 * 1024 * 1024
_INBOX_FILE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: str
    workspace_type: str
    registry_generation_id: str
    registry_generation_digest: str
    definition_digest: str
    graph_id: str | None
    graph_digest: str | None
    document_store_id: str | None
    document_generation_digest: str | None
    freshness_policy_digest: str | None
    state_root: Path


@dataclass(frozen=True)
class WorkspaceInboxSource:
    """One exact source version read from a selected workspace inbox."""

    workspace_id: str
    registry_digest: str
    definition_digest: str
    locator: str
    path: Path
    data: bytes
    root_identity: tuple[int, int]
    workspaces_identity: tuple[int, int]
    state_identity: tuple[int, int]
    workspace_identity: tuple[int, int]
    inbox_identity: tuple[int, int]
    source_identity: tuple[int, int, int, int, int]


@dataclass(frozen=True)
class _ContextSeal:
    reference: weakref.ReferenceType[WorkspaceContext]
    runtime_root: Path
    values: tuple[object, ...]
    root_identity: tuple[int, int]
    workspaces_identity: tuple[int, int]
    state_identity: tuple[int, int]
    workspace_identity: tuple[int, int]


_CONTEXT_SEALS: dict[int, _ContextSeal] = {}
_CONTEXT_SEALS_LOCK = threading.Lock()


def _context_values(context: WorkspaceContext) -> tuple[object, ...]:
    return tuple(
        getattr(context, name) for name in WorkspaceContext.__dataclass_fields__
    )


def _seal_context(context: WorkspaceContext, runtime_root: Path,
                  root_identity: tuple[int, int],
                  workspaces_identity: tuple[int, int],
                  state_identity: tuple[int, int],
                  workspace_identity: tuple[int, int]) -> None:
    key = id(context)

    def discard(reference: weakref.ReferenceType[WorkspaceContext]) -> None:
        with _CONTEXT_SEALS_LOCK:
            current = _CONTEXT_SEALS.get(key)
            if current is not None and current.reference is reference:
                _CONTEXT_SEALS.pop(key, None)

    reference = weakref.ref(context, discard)
    seal = _ContextSeal(
        reference=reference,
        runtime_root=runtime_root,
        values=_context_values(context),
        root_identity=root_identity,
        workspaces_identity=workspaces_identity,
        state_identity=state_identity,
        workspace_identity=workspace_identity,
    )
    with _CONTEXT_SEALS_LOCK:
        _CONTEXT_SEALS[key] = seal


def _context_seal(context: WorkspaceContext) -> _ContextSeal:
    if type(context) is not WorkspaceContext:
        raise _fail("workspace context is invalid")
    with _CONTEXT_SEALS_LOCK:
        seal = _CONTEXT_SEALS.get(id(context))
    if (seal is None or seal.reference() is not context
            or seal.values != _context_values(context)):
        raise _fail("workspace context identity is invalid")
    snapshot = load_workspace_registry(WorkspaceRegistryDependencies(seal.runtime_root))
    selection = select_workspace(snapshot, context.workspace_id)
    if (
        selection.generation["registry_id"],
        selection.generation["registry_digest"],
        selection.primary["definition_digest"],
        selection.primary["workspace_type"],
        selection.primary["graph_id"],
        selection.primary["graph_digest"],
        selection.primary.get("document_store_id"),
        selection.primary.get("document_generation_digest"),
        selection.primary["freshness_policy_digest"],
    ) != (
        context.registry_generation_id,
        context.registry_generation_digest,
        context.definition_digest,
        context.workspace_type,
        context.graph_id,
        context.graph_digest,
        context.document_store_id,
        context.document_generation_digest,
        context.freshness_policy_digest,
    ):
        raise _fail("workspace context registry binding differs")
    return seal


def _fail(message: str, exc: BaseException | None = None) -> WorkspaceRegistryError:
    error = WorkspaceRegistryError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _identity(descriptor: int) -> tuple[int, int]:
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        raise _fail("workspace state directory is invalid")
    return info.st_dev, info.st_ino


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
    return descriptor, _identity(descriptor)


def _revalidate(parent: int, name: str, descriptor: int,
                identity: tuple[int, int]) -> None:
    held = os.fstat(descriptor)
    rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISDIR(rebound.st_mode)
            or (held.st_dev, held.st_ino) != identity
            or (rebound.st_dev, rebound.st_ino) != identity):
        raise _fail("workspace state directory changed")


def _file_identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _workspace_inbox_locator(value: object, expected_suffix: str) -> tuple[str, str]:
    if (
        type(value) is not str
        or type(expected_suffix) is not str
        or expected_suffix not in {".pdf", ".docx"}
        or "\\" in value
    ):
        raise _fail("workspace source locator is invalid")
    parts = value.split("/")
    if (
        len(parts) != 2
        or parts[0] != "inbox"
        or any(part in {"", ".", ".."} for part in parts)
        or _INBOX_FILE.fullmatch(parts[1]) is None
        or not parts[1].endswith(expected_suffix)
    ):
        raise _fail("workspace source locator is invalid")
    return parts[0], parts[1]


def _same_workspace_selection(
    dependencies: WorkspaceRegistryDependencies,
    workspace_id: str,
    registry_digest: str,
    definition_digest: str,
) -> None:
    replayed = select_workspace(load_workspace_registry(dependencies), workspace_id)
    if (
        replayed.generation["registry_digest"] != registry_digest
        or replayed.primary["definition_digest"] != definition_digest
    ):
        raise _fail("workspace registry changed")


def read_workspace_inbox_source(
    dependencies: WorkspaceRegistryDependencies,
    workspace_id: str,
    source_locator: str,
    *,
    expected_suffix: str,
) -> WorkspaceInboxSource:
    """Select first, then read one fixed, contained, no-follow inbox source."""

    if type(dependencies) is not WorkspaceRegistryDependencies:
        raise _fail("workspace dependencies are invalid")
    selection = select_workspace(load_workspace_registry(dependencies), workspace_id)
    _inbox_name, source_name = _workspace_inbox_locator(
        source_locator, expected_suffix,
    )
    runtime_root = Path(os.path.abspath(dependencies.runtime_root))
    descriptors: list[int] = []
    source_fd: int | None = None
    try:
        root = _open_absolute(runtime_root)
        descriptors.append(root)
        root_identity = _identity(root)
        workspaces, workspaces_identity = _open_child(root, "workspaces")
        descriptors.append(workspaces)
        state, state_identity = _open_child(workspaces, "state")
        descriptors.append(state)
        workspace, workspace_identity = _open_child(state, workspace_id)
        descriptors.append(workspace)
        inbox, inbox_identity = _open_child(workspace, "inbox")
        descriptors.append(inbox)
        before = os.stat(source_name, dir_fd=inbox, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > _MAX_WORKSPACE_SOURCE_BYTES
        ):
            raise _fail("workspace source is invalid")
        source_fd = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=inbox)
        opened = os.fstat(source_fd)
        if _file_identity(before) != _file_identity(opened):
            raise _fail("workspace source changed")
        chunks: list[bytes] = []
        remaining = _MAX_WORKSPACE_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(source_fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        dependencies.failpoint("before_workspace_ingest_source_revalidation")
        after = os.fstat(source_fd)
        rebound = os.stat(source_name, dir_fd=inbox, follow_symlinks=False)
        if (
            len(data) > _MAX_WORKSPACE_SOURCE_BYTES
            or len(data) != opened.st_size
            or _file_identity(opened) != _file_identity(after)
            or _file_identity(after) != _file_identity(rebound)
        ):
            raise _fail("workspace source changed")
        _same_workspace_selection(
            dependencies,
            workspace_id,
            selection.generation["registry_digest"],
            selection.primary["definition_digest"],
        )
        _revalidate(workspace, "inbox", inbox, inbox_identity)
        _revalidate(state, workspace_id, workspace, workspace_identity)
        _revalidate(workspaces, "state", state, state_identity)
        _revalidate(root, "workspaces", workspaces, workspaces_identity)
        return WorkspaceInboxSource(
            workspace_id=workspace_id,
            registry_digest=selection.generation["registry_digest"],
            definition_digest=selection.primary["definition_digest"],
            locator=source_locator,
            path=runtime_root / "workspaces" / "state" / workspace_id / source_locator,
            data=data,
            root_identity=root_identity,
            workspaces_identity=workspaces_identity,
            state_identity=state_identity,
            workspace_identity=workspace_identity,
            inbox_identity=inbox_identity,
            source_identity=_file_identity(opened),
        )
    except WorkspaceRegistryError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _fail("workspace source is invalid", exc)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def revalidate_workspace_inbox_source(
    dependencies: WorkspaceRegistryDependencies,
    source: WorkspaceInboxSource,
) -> None:
    """Reject replacement or registry drift before publication."""

    if type(dependencies) is not WorkspaceRegistryDependencies or type(source) is not WorkspaceInboxSource:
        raise _fail("workspace source binding is invalid")
    _inbox_name, source_name = _workspace_inbox_locator(
        source.locator, source.path.suffix,
    )
    runtime_root = Path(os.path.abspath(dependencies.runtime_root))
    if source.path != runtime_root / "workspaces" / "state" / source.workspace_id / source.locator:
        raise _fail("workspace source binding is invalid")
    descriptors: list[int] = []
    source_fd: int | None = None
    try:
        root = _open_absolute(runtime_root)
        descriptors.append(root)
        if _identity(root) != source.root_identity:
            raise _fail("workspace runtime root changed")
        workspaces, workspaces_identity = _open_child(root, "workspaces")
        descriptors.append(workspaces)
        state, state_identity = _open_child(workspaces, "state")
        descriptors.append(state)
        workspace, workspace_identity = _open_child(state, source.workspace_id)
        descriptors.append(workspace)
        inbox, inbox_identity = _open_child(workspace, "inbox")
        descriptors.append(inbox)
        if (
            workspaces_identity != source.workspaces_identity
            or state_identity != source.state_identity
            or workspace_identity != source.workspace_identity
            or inbox_identity != source.inbox_identity
        ):
            raise _fail("workspace source root changed")
        source_fd = os.open(source_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=inbox)
        current = os.fstat(source_fd)
        rebound = os.stat(source_name, dir_fd=inbox, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or _file_identity(current) != source.source_identity
            or _file_identity(rebound) != source.source_identity
        ):
            raise _fail("workspace source changed")
        _same_workspace_selection(
            dependencies,
            source.workspace_id,
            source.registry_digest,
            source.definition_digest,
        )
    except WorkspaceRegistryError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _fail("workspace source is invalid", exc)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _json_bindings(value: object, context: WorkspaceContext) -> None:
    if isinstance(value, dict):
        bindings = {
            "workspace_id": context.workspace_id,
            "graph_id": context.graph_id,
            "graph_digest": context.graph_digest,
            "document_store_id": context.document_store_id,
            "registry_generation_digest": context.registry_generation_digest,
            "definition_digest": context.definition_digest,
        }
        if context.freshness_policy_digest is not None:
            bindings["freshness_policy_digest"] = context.freshness_policy_digest
            bindings["policy_digest"] = context.freshness_policy_digest
        for key, expected in bindings.items():
            if key in value and value[key] != expected:
                raise _fail("workspace artifact binding differs")
        for item in value.values():
            _json_bindings(item, context)
    elif isinstance(value, list):
        for item in value:
            _json_bindings(item, context)


def _validate_inventory(descriptor: int, context: WorkspaceContext,
                        depth: int = 0, budget: list[int] | None = None) -> None:
    if depth > _MAX_DEPTH:
        raise _fail("workspace state depth budget exceeded")
    if budget is None:
        budget = [0]
    names = sorted(os.listdir(descriptor))
    budget[0] += len(names)
    if budget[0] > _MAX_ENTRIES:
        raise _fail("workspace state entry budget exceeded")
    if depth == 0 and any(name not in _WORKSPACE_CHILDREN for name in names):
        raise _fail("workspace state inventory differs")
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise _fail("workspace state names alias")
    for name in names:
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child, identity = _open_child(descriptor, name)
            try:
                _validate_inventory(child, context, depth + 1, budget)
                _revalidate(descriptor, name, child, identity)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _fail("workspace state artifact is invalid")
        if name.endswith(".json"):
            if info.st_size > _MAX_BOUND_JSON_BYTES:
                raise _fail("workspace state artifact byte budget exceeded")
            child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                body = os.read(child, _MAX_BOUND_JSON_BYTES + 1)
                after = os.fstat(child)
            finally:
                os.close(child)
            rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            expected = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if expected != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) or expected != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or expected != (rebound.st_dev, rebound.st_ino, rebound.st_size, rebound.st_mtime_ns, rebound.st_ctime_ns) or len(body) != info.st_size:
                raise _fail("workspace state artifact changed")
            try:
                value = json.loads(body, object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise _fail("workspace state artifact is invalid", exc)
            _json_bindings(value, context)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def open_workspace_context(dependencies: WorkspaceRegistryDependencies,
                           workspace_id: str, *,
                           _snapshot: WorkspaceRegistrySnapshot | None = None) -> WorkspaceContext:
    """Select the registry first, then validate exactly one active state child."""
    snapshot = load_workspace_registry(dependencies) if _snapshot is None else _snapshot
    selection = select_workspace(snapshot, workspace_id)
    generation = selection.generation
    definition = selection.primary
    state_path = Path(os.path.abspath(dependencies.runtime_root)) / "workspaces" / "state" / workspace_id
    context = WorkspaceContext(
        workspace_id=workspace_id,
        workspace_type=definition["workspace_type"],
        registry_generation_id=generation["registry_id"],
        registry_generation_digest=generation["registry_digest"],
        definition_digest=definition["definition_digest"],
        graph_id=definition["graph_id"],
        graph_digest=definition["graph_digest"],
        document_store_id=definition.get("document_store_id"),
        document_generation_digest=definition.get("document_generation_digest"),
        freshness_policy_digest=definition["freshness_policy_digest"],
        state_root=state_path,
    )
    descriptors: list[int] = []
    try:
        root = _open_absolute(Path(dependencies.runtime_root)); descriptors.append(root)
        workspaces, workspaces_identity = _open_child(root, "workspaces"); descriptors.append(workspaces)
        state, state_identity = _open_child(workspaces, "state"); descriptors.append(state)
        selected, selected_identity = _open_child(state, workspace_id); descriptors.append(selected)
        _validate_inventory(selected, context)
        dependencies.failpoint("before_workspace_revalidation")
        replayed = load_workspace_registry(dependencies)
        rebound = select_workspace(replayed, workspace_id)
        if (rebound.generation["registry_id"], rebound.generation["registry_digest"],
                rebound.primary) != (generation["registry_id"], generation["registry_digest"], definition):
            raise _fail("workspace registry changed")
        _revalidate(state, workspace_id, selected, selected_identity)
        _revalidate(workspaces, "state", state, state_identity)
        _revalidate(root, "workspaces", workspaces, workspaces_identity)
        _seal_context(
            context,
            Path(os.path.abspath(dependencies.runtime_root)),
            _identity(root),
            workspaces_identity,
            state_identity,
            selected_identity,
        )
        return context
    except WorkspaceRegistryError:
        raise
    except (OSError, ValueError) as exc:
        raise _fail("workspace state is invalid", exc)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def load_workspace_document_context(
    dependencies: WorkspaceRegistryDependencies,
    workspace_id: str,
) -> WorkspaceDocumentSnapshot:
    """Select registry first, then open only the named document state root."""

    if type(dependencies) is not WorkspaceRegistryDependencies:
        raise _fail("workspace dependencies are invalid")
    snapshot = load_workspace_registry(dependencies)
    selection = select_workspace(snapshot, workspace_id)
    definition = selection.primary
    if definition.get("document_store_id") is None:
        raise _fail("workspace has no document binding")
    try:
        document_snapshot = load_workspace_documents(
            WorkspaceDocumentDependencies(
                runtime_root=dependencies.runtime_root,
                failpoint=dependencies.failpoint,
            ),
            workspace_id,
        )
    except WorkspaceDocumentError as exc:
        raise _fail("workspace document context is invalid", exc)
    rebound = select_workspace(load_workspace_registry(dependencies), workspace_id)
    if (
        (rebound.generation["registry_id"], rebound.generation["registry_digest"], rebound.primary)
        != (selection.generation["registry_id"], selection.generation["registry_digest"], definition)
    ):
        raise _fail("workspace registry changed")
    generation = document_snapshot.generation
    if definition["document_generation_digest"] is None:
        if generation is not None:
            raise _fail("workspace document context binding differs")
    elif (
        generation is None
        or generation["workspace_id"] != workspace_id
        or generation["document_store_id"] != definition["document_store_id"]
        or generation["generation_digest"] != definition["document_generation_digest"]
        or selection.generation["predecessor_registry_digest"] is None
        or generation["registry_digest"] != selection.generation["predecessor_registry_digest"]
    ):
        raise _fail("workspace document context binding differs")
    return document_snapshot


def _validated_engine_root(context: WorkspaceContext,
                           child_name: str) -> tuple[Path, tuple[int, int]]:
    seal = _context_seal(context)
    root = workspaces = state_parent = workspace = child = None
    try:
        root = _open_absolute(seal.runtime_root)
        if _identity(root) != seal.root_identity:
            raise _fail("workspace runtime root changed")
        workspaces, workspaces_identity = _open_child(root, "workspaces")
        if workspaces_identity != seal.workspaces_identity:
            raise _fail("workspace root changed")
        state_parent, state_identity = _open_child(workspaces, "state")
        if state_identity != seal.state_identity:
            raise _fail("workspace state root changed")
        workspace, workspace_identity = _open_child(state_parent, context.workspace_id)
        if workspace_identity != seal.workspace_identity:
            raise _fail("workspace state identity changed")
        child, child_identity = _open_child(workspace, child_name)
        _revalidate(workspace, child_name, child, child_identity)
        _revalidate(state_parent, context.workspace_id, workspace, workspace_identity)
        _revalidate(workspaces, "state", state_parent, state_identity)
        _revalidate(root, "workspaces", workspaces, workspaces_identity)
        return context.state_root / child_name, child_identity
    except WorkspaceRegistryError:
        raise
    except OSError as exc:
        raise _fail("workspace engine root is invalid", exc)
    finally:
        for descriptor in (child, workspace, state_parent, workspaces, root):
            if descriptor is not None:
                os.close(descriptor)


def _workspace_acquisition_dependencies(context: WorkspaceContext, http,
                                        clock: Callable[[], str],
                                        monotonic: Callable[[], float],
                                        failpoint: Callable[[str], None] = lambda _name: None):
    from .evidence_acquisition import AcquisitionDependencies
    root, identity = _validated_engine_root(context, "sources")
    return AcquisitionDependencies(root, http, clock, monotonic, failpoint,
                                   _source_root_identity=identity)


def _workspace_graph_dependencies(context: WorkspaceContext,
                                  failpoint: Callable[[str], None] = lambda _name: None):
    from .evidence_graph import GraphDependencies
    root, identity = _validated_engine_root(context, "graph")
    return GraphDependencies(root, failpoint, _root_identity=identity)


def _workspace_freshness_dependencies(context: WorkspaceContext,
                                      failpoint: Callable[[str], None] = lambda _name: None,
                                      *, limits=None):
    from .evidence_freshness import FreshnessDependencies, FreshnessLimits
    root, identity = _validated_engine_root(context, "freshness")
    return FreshnessDependencies(
        root, failpoint, FreshnessLimits() if limits is None else limits,
        _source_root_identity=identity, _workspace_namespace=True,
    )


def _operation_context(
    dependencies: WorkspaceRegistryDependencies, workspace_id: str,
) -> tuple[WorkspaceRegistrySnapshot, dict[str, Any], WorkspaceContext]:
    snapshot = load_workspace_registry(dependencies)
    selection = select_workspace(snapshot, workspace_id)
    context = open_workspace_context(
        dependencies, workspace_id, _snapshot=snapshot,
    )
    return snapshot, selection.generation, context


def _operation_material(
    snapshot: WorkspaceRegistrySnapshot, context: WorkspaceContext,
    graph: Any, *, freshness_summary: Any | None = None,
    policy: Any | None = None, allow_new_summary: bool = False,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
    try:
        definition = select_workspace(snapshot, context.workspace_id).primary
        validated_graph = validate_graph_manifest(graph)
        graph_binding = (
            validated_graph["root_workflow_id"],
            validated_graph["root_workflow_digest"],
            validated_graph["source_registry_digest"],
            validated_graph["graph_id"],
            validated_graph["graph_digest"],
        )
        definition_binding = (
            definition["root_workflow_id"],
            definition["root_workflow_digest"],
            definition["source_registry_digest"],
            definition["graph_id"],
            definition["graph_digest"],
        )
        if graph_binding != definition_binding:
            raise _fail("workspace graph binding differs")

        validated_policy = None
        if policy is not None:
            validated_policy = validate_freshness_policy(policy)
            if (
                validated_policy["graph_id"],
                validated_policy["graph_digest"],
                validated_policy["policy_id"],
                validated_policy["policy_digest"],
            ) != (
                definition["graph_id"],
                definition["graph_digest"],
                definition["freshness_policy_id"],
                definition["freshness_policy_digest"],
            ):
                raise _fail("workspace freshness policy binding differs")

        validated_summary = None
        if freshness_summary is not None:
            validated_summary = validate_detached_freshness_summary_against_manifest(
                freshness_summary, validated_graph,
            )
            if (
                validated_summary["policy_id"],
                validated_summary["policy_digest"],
            ) != (
                definition["freshness_policy_id"],
                definition["freshness_policy_digest"],
            ):
                raise _fail("workspace freshness summary binding differs")
        if not allow_new_summary:
            if definition["freshness_summary_status"] == "not_observed":
                if validated_summary is not None:
                    raise _fail("workspace freshness summary is not declared")
            elif validated_summary is None or (
                validated_summary["summary_id"],
                validated_summary["summary_digest"],
            ) != (
                definition["freshness_summary_id"],
                definition["freshness_summary_digest"],
            ):
                raise _fail("workspace freshness summary binding differs")
        return validated_graph, validated_summary, validated_policy
    except WorkspaceRegistryError:
        raise
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise _fail("workspace operation binding is invalid", exc)


def _operation_readback(
    generation: dict[str, Any], context: WorkspaceContext, operation: str,
    *, status: str, reason_code: str, engine_result: Any,
) -> dict[str, Any]:
    _json_bindings(engine_result, context)
    identity = canonical_digest({
        "operation": operation,
        "workspace_id": context.workspace_id,
        "registry_digest": context.registry_generation_digest,
        "engine_result": engine_result,
    }).removeprefix("sha256:")[:32]
    body = {
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[3],
        "operation_id": f"{operation}-{identity}",
        "operation": operation,
        "workspace_id": context.workspace_id,
        "registry_id": context.registry_generation_id,
        "registry_digest": context.registry_generation_digest,
        "status": status,
        "reason_code": reason_code,
        "affected_workspace_ids": [context.workspace_id],
        "readback_digest": "sha256:" + "0" * 64,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    body["readback_digest"] = canonical_digest({
        key: value for key, value in body.items() if key != "readback_digest"
    })
    try:
        return validate_workspace_operation_readback(
            body,
            generation=generation,
            selected_workspace_ids={context.workspace_id},
        )
    except ContractError as exc:
        raise _fail("workspace operation readback is invalid", exc)


def inspect_workspace(
    dependencies: WorkspaceRegistryDependencies, workspace_id: str,
    graph: Any, *, as_of_date: str,
    freshness_summary: Any | None = None,
) -> dict[str, Any]:
    """Inspect one graph without resolving or opening declared references."""
    from .evidence_graph import inspect_evidence_graph

    snapshot, generation, context = _operation_context(dependencies, workspace_id)
    validated_graph, validated_summary, _ = _operation_material(
        snapshot, context, graph, freshness_summary=freshness_summary,
    )
    _workspace_graph_dependencies(context)
    if validated_summary is not None:
        _workspace_freshness_dependencies(context)
    inspection = inspect_evidence_graph(
        validated_graph,
        as_of_date=as_of_date,
        freshness_summary=validated_summary,
    )
    investigate = inspection["result"] == "investigate"
    return _operation_readback(
        generation, context, "inspect",
        status="investigate" if investigate else "active",
        reason_code="workspace_binding_drift" if investigate else "ok",
        engine_result=inspection,
    )


def refresh_workspace(
    dependencies: WorkspaceRegistryDependencies, workspace_id: str,
    policy: Any, graph: Any, specs: Sequence[Any], *, http: Any,
    clock: Callable[[], str], monotonic: Callable[[], float], limits: Any,
) -> dict[str, Any]:
    """Refresh one exact workspace; declared references are never traversed."""
    from .evidence_freshness import refresh_official_evidence

    snapshot, generation, context = _operation_context(dependencies, workspace_id)
    validated_graph, _, validated_policy = _operation_material(
        snapshot, context, graph, policy=policy, allow_new_summary=True,
    )
    acquisition = _workspace_acquisition_dependencies(
        context, http, clock, monotonic,
    )
    freshness = _workspace_freshness_dependencies(context)
    summary = refresh_official_evidence(
        validated_policy, validated_graph, specs, acquisition, limits=limits,
        freshness_dependencies=freshness,
    )
    _, validated_summary, _ = _operation_material(
        snapshot, context, validated_graph, freshness_summary=summary,
        policy=validated_policy, allow_new_summary=True,
    )
    investigate = any(
        item["classification"] != "unchanged"
        for item in validated_summary["results"]
    )
    return _operation_readback(
        generation, context, "refresh",
        status="investigate" if investigate else "complete",
        reason_code="freshness_investigation_required" if investigate else "ok",
        engine_result=validated_summary,
    )


def replay_workspace(
    dependencies: WorkspaceRegistryDependencies, workspace_id: str,
    graph: Any, *, freshness_summary: Any | None = None,
) -> dict[str, Any]:
    """Replay one exact graph into only its held workspace namespace."""
    from .evidence_graph import publish_evidence_graph

    snapshot, generation, context = _operation_context(dependencies, workspace_id)
    validated_graph, _, _ = _operation_material(
        snapshot, context, graph, freshness_summary=freshness_summary,
    )
    report = publish_evidence_graph(
        validated_graph, _workspace_graph_dependencies(context),
    )
    investigate = report.get("classification") == "investigate"
    return _operation_readback(
        generation, context, "replay",
        status="investigate" if investigate else "complete",
        reason_code="workspace_binding_drift" if investigate else "ok",
        engine_result=report,
    )


def recover_workspace(
    dependencies: WorkspaceRegistryDependencies, workspace_id: str,
) -> dict[str, Any]:
    """Recover graph and freshness transactions for one named workspace only."""
    from .evidence_freshness import recover_freshness_transactions
    from .evidence_graph import recover_evidence_graph

    _snapshot, generation, context = _operation_context(dependencies, workspace_id)
    graph_results = recover_evidence_graph(_workspace_graph_dependencies(context))
    freshness_results = recover_freshness_transactions(
        _workspace_freshness_dependencies(context),
    )
    results = [*graph_results, *freshness_results]
    investigate = any(
        item.get("classification") == "investigate" for item in results
    )
    return _operation_readback(
        generation, context, "recover",
        status="investigate" if investigate else "complete",
        reason_code="recovery_pending" if investigate else "ok",
        engine_result=results,
    )
