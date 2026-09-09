"""Descriptor-anchored immutable storage for one workspace's document evidence."""

from __future__ import annotations

import copy
import ctypes
import errno
import fcntl
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ._strict_io import ContractError, parse_strict_json
from .benchmark import BenchmarkError, canonical_digest
from .document_evidence_contracts import validate_workspace_document_generation
from .workspace_registry import (
    _WorkspaceRegistryProofDependencies,
    WorkspaceRegistryDependencies,
    WorkspaceRegistryError,
    WorkspaceSelection,
    _locked_storage as _locked_workspace_registry_storage,
    _publish_exact_generation_locked,
    load_workspace_registry,
    publish_exact_workspace_registry_generation,
    select_workspace,
)
from .workspace_contracts import AUTHORITY_FIELDS, validate_workspace_definition, validate_workspace_registry_generation


DOCUMENT_PUBLICATION_FAILPOINTS = (
    "after_document_intent",
    "after_document_staging",
    "after_document_publication",
    "after_document_registry_publication",
    "after_document_completion",
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_GENERATION = re.compile(r"^(?P<sequence>[0-9]{10})-(?P<id>[a-z0-9][a-z0-9._-]{0,127})$")
_RUN = re.compile(r"^document-run-[0-9a-f]{32}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_GENERATIONS = 128
_MAX_ENTRIES = 256
_MAX_MANIFEST = 256 * 1024 * 1024
_MAX_TERMINAL_RUNS = 32
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class WorkspaceDocumentError(ValueError):
    """Workspace document state is unsafe, inconsistent, or conflicting."""


def _system_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class WorkspaceDocumentDependencies:
    runtime_root: Path
    failpoint: Callable[[str], None] = lambda _name: None


@dataclass(frozen=True)
class _WorkspaceDocumentProofDependencies(WorkspaceDocumentDependencies):
    clock: Callable[[], str] = _system_now


def _dependency_clock(
    dependencies: WorkspaceDocumentDependencies,
) -> Callable[[], str]:
    if type(dependencies) is _WorkspaceDocumentProofDependencies:
        return dependencies.clock
    return _system_now


def _registry_dependencies(
    dependencies: WorkspaceDocumentDependencies,
) -> WorkspaceRegistryDependencies:
    if type(dependencies) is _WorkspaceDocumentProofDependencies:
        return _WorkspaceRegistryProofDependencies(
            Path(dependencies.runtime_root),
            clock=dependencies.clock,
        )
    return WorkspaceRegistryDependencies(Path(dependencies.runtime_root))


@dataclass(frozen=True)
class WorkspaceDocumentSnapshot:
    generation: dict[str, object] | None


def _fail(message: str, cause: BaseException | None = None) -> WorkspaceDocumentError:
    error = WorkspaceDocumentError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


def _workspace_id(value: Any) -> str:
    if type(value) is not str or _ID.fullmatch(value) is None:
        raise _fail("workspace identity is invalid")
    return value


def _document_root_path(root_path: Path, workspace_id: str) -> Path:
    return Path(root_path) / "workspaces" / "state" / workspace_id / "documents"


def _registry_selection(runtime_root: Path, workspace_id: str) -> WorkspaceSelection:
    try:
        return select_workspace(
            load_workspace_registry(WorkspaceRegistryDependencies(runtime_root)),
            workspace_id,
        )
    except WorkspaceRegistryError as exc:
        raise _fail("workspace document registry binding is invalid", exc)


def _json_bytes(value: dict[str, Any]) -> bytes:
    try:
        return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()
    except (TypeError, ValueError) as exc:
        raise _fail("workspace document value is not strict JSON", exc)


def _open_absolute(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    current = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current); current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _open_child(parent: int, name: str) -> tuple[int, tuple[int, int]]:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor); raise _fail("workspace document directory is invalid")
    return descriptor, (info.st_dev, info.st_ino)


def _ensure_child(parent: int, name: str) -> tuple[int, tuple[int, int]]:
    try:
        os.mkdir(name, 0o700, dir_fd=parent); os.fsync(parent)
    except FileExistsError:
        pass
    return _open_child(parent, name)


def _revalidate_child(parent: int, name: str, descriptor: int, identity: tuple[int, int]) -> None:
    held = os.fstat(descriptor)
    rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISDIR(rebound.st_mode) or (held.st_dev, held.st_ino) != identity
            or (rebound.st_dev, rebound.st_ino) != identity):
        raise _fail("workspace document directory changed")


def _names(descriptor: int, maximum: int = _MAX_ENTRIES) -> list[str]:
    names = sorted(os.listdir(descriptor))
    if len(names) > maximum or len({name.casefold() for name in names}) != len(names):
        raise _fail("workspace document inventory is invalid")
    return names


def _read_regular(parent: int, name: str, maximum: int = _MAX_MANIFEST) -> bytes:
    descriptor = None
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise _fail("workspace document file is invalid")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(descriptor)
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(opened):
            raise _fail("workspace document file changed")
        chunks = []; remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk: break
            chunks.append(chunk); remaining -= len(chunk)
        value = b"".join(chunks)
        after = os.fstat(descriptor); rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if len(value) > maximum or len(value) != opened.st_size or identity(opened) != identity(after) or identity(after) != identity(rebound):
            raise _fail("workspace document file changed")
        return value
    except WorkspaceDocumentError:
        raise
    except OSError as exc:
        raise _fail("workspace document file is invalid", exc)
    finally:
        if descriptor is not None: os.close(descriptor)


def _read_json(parent: int, name: str, label: str) -> dict[str, Any]:
    try:
        value = parse_strict_json(_read_regular(parent, name), label)
    except ContractError as exc:
        raise _fail(f"{label} is invalid", exc)
    if type(value) is not dict:
        raise _fail(f"{label} is invalid")
    return value


def _write_file(parent: int, name: str, body: bytes) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    try:
        offset = 0
        while offset < len(body):
            offset += os.write(descriptor, body[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent)


def _replace_file(parent: int, name: str, body: bytes) -> None:
    temporary = "." + name + "." + secrets.token_hex(16)
    _write_file(parent, temporary, body)
    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    os.fsync(parent)


def _rename_noreplace(source_parent: int, source: str, destination_parent: int, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.renameat2(source_parent, source.encode(), destination_parent, destination.encode(), _RENAME_NOREPLACE)
    if result != 0:
        value = ctypes.get_errno()
        if value == errno.EEXIST:
            raise FileExistsError(destination)
        raise OSError(value, os.strerror(value))
    os.fsync(source_parent); os.fsync(destination_parent)


def _open_runtime_documents(root_path: Path, workspace_id: str, *, create: bool) -> tuple[list[int], dict[str, tuple[int, tuple[int, int]]]] | None:
    descriptors: list[int] = []
    opened: dict[str, tuple[int, tuple[int, int]]] = {}
    try:
        if create:
            try:
                root = _open_absolute(root_path)
            except FileNotFoundError:
                parent = _open_absolute(root_path.parent)
                try:
                    root, root_identity = _ensure_child(parent, root_path.name)
                finally:
                    os.close(parent)
            else:
                info = os.fstat(root); root_identity = (info.st_dev, info.st_ino)
        else:
            try: root = _open_absolute(root_path)
            except FileNotFoundError: return None
            info = os.fstat(root); root_identity = (info.st_dev, info.st_ino)
        descriptors.append(root); opened["root"] = (root, root_identity)
        parent = root
        for label, name in (("workspaces", "workspaces"), ("state", "state"), ("workspace", workspace_id), ("documents", "documents")):
            try:
                child, identity = _ensure_child(parent, name) if create else _open_child(parent, name)
            except FileNotFoundError:
                for descriptor in reversed(descriptors): os.close(descriptor)
                return None
            descriptors.append(child); opened[label] = (child, identity); parent = child
        return descriptors, opened
    except BaseException:
        for descriptor in reversed(descriptors): os.close(descriptor)
        raise


def _validate_generation(parent: int, name: str, workspace_id: str) -> dict[str, Any]:
    match = _GENERATION.fullmatch(name)
    if match is None:
        raise _fail("workspace document generation name is invalid")
    directory, identity = _open_child(parent, name)
    try:
        if _names(directory) != ["manifest.json"]:
            raise _fail("workspace document generation inventory differs")
        raw = _read_regular(directory, "manifest.json")
        try:
            value = validate_workspace_document_generation(parse_strict_json(raw, "workspace document generation"))
        except ContractError as exc:
            raise _fail("workspace document generation is invalid", exc)
        if _json_bytes(value) != raw or value["sequence"] != int(match.group("sequence")) or value["generation_id"] != match.group("id") or value["workspace_id"] != workspace_id:
            raise _fail("workspace document generation binding differs")
        _revalidate_child(parent, name, directory, identity)
        return value
    finally:
        os.close(directory)


def _load_generations(generations_fd: int, workspace_id: str) -> list[dict[str, Any]]:
    names = _names(generations_fd, _MAX_GENERATIONS)
    values = [_validate_generation(generations_fd, name, workspace_id) for name in names]
    for sequence, value in enumerate(values, 1):
        predecessor = None if sequence == 1 else values[sequence - 2]["generation_digest"]
        if value["sequence"] != sequence or value["prior_generation_digest"] != predecessor:
            raise _fail("workspace document generation lineage differs")
    return values


def _validate_document_inventory(documents_fd: int) -> tuple[int, int, int, int]:
    names = _names(documents_fd)
    allowed = {"generations", "recovery", "staging", "documents.lock"}
    if set(names) - allowed or "generations" not in names:
        raise _fail("workspace document inventory differs")
    lock_info = os.stat("documents.lock", dir_fd=documents_fd, follow_symlinks=False)
    if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1 or lock_info.st_size != 0:
        raise _fail("workspace document lock is invalid")
    result = []
    for name in ("generations", "recovery", "staging"):
        descriptor, _ = _open_child(documents_fd, name); result.append(descriptor)
    lock_fd = os.open("documents.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=documents_fd)
    result.append(lock_fd)
    return tuple(result)  # type: ignore[return-value]


def _prepare_documents(documents_fd: int) -> tuple[int, int, int, int]:
    for name in ("generations", "recovery", "staging"):
        descriptor, _ = _ensure_child(documents_fd, name); os.close(descriptor)
    try: _write_file(documents_fd, "documents.lock", b"")
    except FileExistsError: pass
    return _validate_document_inventory(documents_fd)


def load_workspace_documents(dependencies: WorkspaceDocumentDependencies, workspace_id: str) -> WorkspaceDocumentSnapshot:
    """Read one exact workspace document lineage without creating state."""
    if not isinstance(dependencies, WorkspaceDocumentDependencies):
        raise _fail("workspace document dependencies are invalid")
    workspace_id = _workspace_id(workspace_id)
    runtime_root = Path(dependencies.runtime_root)
    selection = _registry_selection(runtime_root, workspace_id)
    if not _document_root_path(runtime_root, workspace_id).exists():
        if selection.primary.get("document_generation_digest") is not None:
            raise _fail("workspace document registry binding differs")
        return WorkspaceDocumentSnapshot(None)
    opened = _open_runtime_documents(runtime_root, workspace_id, create=False)
    if opened is None: return WorkspaceDocumentSnapshot(None)
    descriptors, chain = opened; extra: list[int] = []
    try:
        documents_fd = chain["documents"][0]
        generations_fd, recovery_fd, staging_fd, lock_fd = _validate_document_inventory(documents_fd)
        extra.extend((generations_fd, recovery_fd, staging_fd, lock_fd))
        if _names(staging_fd) or _active_recovery(recovery_fd, generations_fd, workspace_id):
            raise _fail("workspace document recovery requires investigation")
        values = _load_generations(generations_fd, workspace_id)
        dependencies.failpoint("before_document_revalidation")
        if values:
            _ensure_current_registry_binding(selection, values[-1])
        elif selection.primary.get("document_generation_digest") is not None:
            raise _fail("workspace document registry binding differs")
        _revalidate_chain(runtime_root, workspace_id, chain)
        return WorkspaceDocumentSnapshot(None if not values else copy.deepcopy(values[-1]))
    finally:
        for descriptor in reversed(extra): os.close(descriptor)
        for descriptor in reversed(descriptors): os.close(descriptor)


def _revalidate_chain(root_path: Path, workspace_id: str, chain: dict[str, tuple[int, tuple[int, int]]]) -> None:
    root_fd, root_identity = chain["root"]
    rebound = os.stat(root_path, follow_symlinks=False)
    if (rebound.st_dev, rebound.st_ino) != root_identity:
        raise _fail("workspace runtime root changed")
    for parent_label, label, name in (("root", "workspaces", "workspaces"), ("workspaces", "state", "state"),
                                      ("state", "workspace", workspace_id), ("workspace", "documents", "documents")):
        _revalidate_child(chain[parent_label][0], name, chain[label][0], chain[label][1])


def _binding(ir: dict[str, Any]) -> dict[str, Any]:
    metadata = ir.get("metadata")
    if type(metadata) is not dict:
        raise _fail("document IR metadata is invalid")
    source = ir.get("source")
    if type(source) is not dict:
        raise _fail("document IR source is invalid")
    source_id = metadata.get("source_id", "source-" + ir.get("document_id", ""))
    source_record = metadata.get("source_record_digest", canonical_digest(source))
    return {
        "document_id": ir.get("document_id"), "document_ir_digest": canonical_digest(ir),
        "source_id": source_id, "source_digest": source.get("digest"), "source_record_digest": source_record,
        "media_type": source.get("media_type"), "authority_role": metadata.get("authority_role", "technical_guidance"),
        "sensitivity": metadata.get("sensitivity", "internal"), "version": metadata.get("version", "1"),
        "effective_date": metadata.get("effective_date"), "freshness_status": metadata.get("freshness_status", "current"),
        "qualification_codes": metadata.get("qualification_codes", []), "document_ir": copy.deepcopy(ir),
    }


def _generation(workspace_id: str, document_store_id: str, registry_digest: str,
                documents: list[dict[str, Any]], prior: dict[str, Any] | None,
                clock: Callable[[], str]) -> dict[str, Any]:
    sequence = 1 if prior is None else prior["sequence"] + 1
    value = {
        "schema_version": "ao.lore.workspace-document-generation.v0.1",
        "generation_id": f"documents-{workspace_id}-{sequence:010d}", "sequence": sequence,
        "prior_generation_digest": None if prior is None else prior["generation_digest"],
        "workspace_id": workspace_id, "document_store_id": document_store_id,
        "registry_digest": registry_digest,
        "created_at": clock(),
        "documents": documents, "generation_digest": "sha256:" + "0" * 64,
    }
    value["generation_digest"] = canonical_digest({key: item for key, item in value.items() if key != "generation_digest"})
    try: return validate_workspace_document_generation(value)
    except (ContractError, BenchmarkError) as exc: raise _fail("document IR publication input is invalid", exc)


def _updated_workspace_definition(definition: dict[str, Any], generation_digest: str) -> dict[str, Any]:
    value = copy.deepcopy(definition)
    if "document_store_id" not in value or "document_generation_digest" not in value:
        raise _fail("workspace document registry binding is invalid")
    value["workspace_version"] += 1
    value["document_generation_digest"] = generation_digest
    value["definition_digest"] = canonical_digest({
        key: item for key, item in value.items() if key != "definition_digest"
    })
    try:
        return validate_workspace_definition(value)
    except (ContractError, BenchmarkError) as exc:
        raise _fail("workspace document registry binding is invalid", exc)


def _registry_generation(selection: WorkspaceSelection, generation: dict[str, Any],
                         clock: Callable[[], str]) -> dict[str, Any]:
    previous = selection.generation
    updated = _updated_workspace_definition(selection.primary, generation["generation_digest"])
    workspaces = []
    for item in previous["workspaces"]:
        workspaces.append(updated if item["workspace_id"] == updated["workspace_id"] else copy.deepcopy(item))
    sequence = previous["sequence"] + 1
    generated_at = clock()
    seed = canonical_digest({
        "sequence": sequence,
        "predecessor": previous["registry_digest"],
        "workspaces": workspaces,
    })
    value = {
        "schema_version": previous["schema_version"],
        "registry_id": "registry-" + seed.split(":", 1)[1][:24],
        "sequence": sequence,
        "predecessor_registry_digest": previous["registry_digest"],
        "generated_at": generated_at,
        "workspaces": workspaces,
        "registry_digest": "sha256:" + "0" * 64,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    value["registry_digest"] = canonical_digest({
        key: item for key, item in value.items() if key != "registry_digest"
    })
    try:
        return validate_workspace_registry_generation(value)
    except (ContractError, BenchmarkError) as exc:
        raise _fail("workspace document registry binding is invalid", exc)


def _intent(run_id: str, target: str, generation: dict[str, Any],
            registry_generation: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": "ao.lore.workspace-document-publication-intent.v0.1",
        "run_id": run_id, "target": target, "generation": generation,
        "registry_generation": registry_generation, "intent_digest": "sha256:" + "0" * 64,
    }
    value["intent_digest"] = canonical_digest({key: item for key, item in value.items() if key != "intent_digest"})
    return value


def _read_intent(run_fd: int) -> dict[str, Any]:
    body = _read_json(run_fd, "intent.json", "workspace document publication intent")
    if set(body) != {"schema_version", "run_id", "target", "generation", "registry_generation", "intent_digest"} or body["schema_version"] != "ao.lore.workspace-document-publication-intent.v0.1":
        raise _fail("workspace document publication intent is invalid")
    expected = canonical_digest({key: item for key, item in body.items() if key != "intent_digest"})
    if body["intent_digest"] != expected:
        raise _fail("workspace document publication intent digest differs")
    body["generation"] = validate_workspace_document_generation(body["generation"])
    try:
        body["registry_generation"] = validate_workspace_registry_generation(body["registry_generation"])
    except (ContractError, BenchmarkError) as exc:
        raise _fail("workspace document publication intent is invalid", exc)
    return body


def _read_terminal_intent_metadata(run_fd: int) -> dict[str, Any]:
    return _read_intent(run_fd)


def _completion(run_id: str, generation_digest: str, registry_generation_digest: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "generation_digest": generation_digest,
        "registry_generation_digest": registry_generation_digest,
        "status": "complete",
    }


def _read_completion(run_fd: int, run_id: str, intent: dict[str, Any]) -> dict[str, Any]:
    body = _read_json(run_fd, "completion.json", "workspace document publication completion")
    expected = _completion(
        run_id,
        intent["generation"]["generation_digest"],
        intent["registry_generation"]["registry_digest"],
    )
    if set(body) != set(expected) or body != expected:
        raise _fail("workspace document completion differs")
    return body


def _validate_published_generation(intent: dict[str, Any], generations_fd: int, workspace_id: str) -> dict[str, Any]:
    published = _validate_generation(generations_fd, intent["target"], workspace_id)
    if published != intent["generation"]:
        raise _fail("workspace document published generation differs")
    return published


def _ensure_current_registry_binding(selection: WorkspaceSelection, generation: dict[str, Any]) -> None:
    if (
        generation["workspace_id"] != selection.primary["workspace_id"]
        or generation["document_store_id"] != selection.primary["document_store_id"]
        or selection.primary.get("document_generation_digest") != generation["generation_digest"]
        or selection.generation["predecessor_registry_digest"] != generation["registry_digest"]
    ):
        raise _fail("workspace document registry binding differs")


def _expected_registry_binding_matches(selection: WorkspaceSelection,
                                       expected_registry_digest: str | None,
                                       expected_definition_digest: str | None) -> bool:
    if expected_registry_digest is None and expected_definition_digest is None:
        return True
    if (
        type(expected_registry_digest) is not str
        or type(expected_definition_digest) is not str
        or _DIGEST.fullmatch(expected_registry_digest) is None
        or _DIGEST.fullmatch(expected_definition_digest) is None
    ):
        raise _fail("workspace document publication input is invalid")
    return (
        selection.generation["registry_digest"] == expected_registry_digest
        and selection.primary["definition_digest"] == expected_definition_digest
    )


def _publication_result(generation: dict[str, Any], *, status: str) -> dict[str, Any]:
    if status not in {"published", "unchanged"}:
        raise _fail("workspace document publication result is invalid")
    result = copy.deepcopy(generation)
    result["status"] = status
    return result


def _unlink_exact(parent: int, name: str, expected: bytes) -> None:
    if _read_regular(parent, name) != expected:
        raise _fail("workspace document cleanup differs")
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)


def _remove_terminal_run(recovery_fd: int, run_id: str, intent: dict[str, Any]) -> None:
    run_fd, identity = _open_child(recovery_fd, run_id)
    try:
        if _names(run_fd) != ["completion.json", "intent.json"]:
            raise _fail("workspace document cleanup differs")
        _unlink_exact(
            run_fd,
            "completion.json",
            _json_bytes(_completion(
                run_id,
                intent["generation"]["generation_digest"],
                intent["registry_generation"]["registry_digest"],
            )),
        )
        _unlink_exact(run_fd, "intent.json", _json_bytes(intent))
        _revalidate_child(recovery_fd, run_id, run_fd, identity)
    finally:
        os.close(run_fd)
    os.rmdir(run_id, dir_fd=recovery_fd)
    os.fsync(recovery_fd)


def _compact_terminal_recovery(recovery_fd: int, generations_fd: int, workspace_id: str) -> None:
    terminal: list[tuple[int, str, dict[str, Any]]] = []
    for name in _names(recovery_fd, _MAX_GENERATIONS):
        if _RUN.fullmatch(name) is None:
            raise _fail("foreign workspace document recovery state exists")
        run_fd, _ = _open_child(recovery_fd, name)
        try:
            names = _names(run_fd)
            if set(names) not in ({"intent.json"}, {"intent.json", "completion.json"}):
                raise _fail("workspace document recovery inventory differs")
            if "completion.json" not in names:
                continue
            intent = _read_terminal_intent_metadata(run_fd)
            if intent["run_id"] != name:
                raise _fail("workspace document recovery identity differs")
            _read_completion(run_fd, name, intent)
            if intent["generation"]["workspace_id"] != workspace_id:
                raise _fail("workspace document recovery origin differs")
            terminal.append((intent["generation"]["sequence"], name, intent))
        finally:
            os.close(run_fd)
    if len(terminal) <= _MAX_TERMINAL_RUNS:
        return
    terminal.sort(key=lambda item: (item[0], item[1]))
    for _sequence, run_id, _intent in terminal[:-_MAX_TERMINAL_RUNS]:
        run_fd, _ = _open_child(recovery_fd, run_id)
        try:
            intent = _read_intent(run_fd)
            if intent["run_id"] != run_id:
                raise _fail("workspace document recovery identity differs")
            if intent["generation"]["workspace_id"] != workspace_id:
                raise _fail("workspace document recovery origin differs")
            _read_completion(run_fd, run_id, intent)
            _validate_published_generation(intent, generations_fd, workspace_id)
        finally:
            os.close(run_fd)
        _remove_terminal_run(recovery_fd, run_id, intent)


def _active_recovery(recovery_fd: int, generations_fd: int, workspace_id: str) -> bool:
    active = False
    for name in _names(recovery_fd, _MAX_GENERATIONS):
        if _RUN.fullmatch(name) is None: raise _fail("foreign workspace document recovery state exists")
        run_fd, _ = _open_child(recovery_fd, name)
        try:
            names = _names(run_fd)
            if set(names) not in ({"intent.json"}, {"intent.json", "completion.json"}):
                raise _fail("workspace document recovery inventory differs")
            intent = _read_intent(run_fd)
            if intent["run_id"] != name: raise _fail("workspace document recovery identity differs")
            if "completion.json" not in names: active = True
            else:
                _read_completion(run_fd, name, intent)
                _validate_published_generation(intent, generations_fd, workspace_id)
        finally: os.close(run_fd)
    return active


def _active_recovery_pending(recovery_fd: int) -> bool:
    active = False
    for name in _names(recovery_fd, _MAX_GENERATIONS):
        if _RUN.fullmatch(name) is None:
            raise _fail("foreign workspace document recovery state exists")
        run_fd, _ = _open_child(recovery_fd, name)
        try:
            names = _names(run_fd)
            if set(names) not in ({"intent.json"}, {"intent.json", "completion.json"}):
                raise _fail("workspace document recovery inventory differs")
            if "completion.json" not in names:
                intent = _read_intent(run_fd)
                if intent["run_id"] != name:
                    raise _fail("workspace document recovery identity differs")
                active = True
                continue
            intent = _read_terminal_intent_metadata(run_fd)
            if intent["run_id"] != name:
                raise _fail("workspace document recovery identity differs")
            _read_completion(run_fd, name, intent)
        finally:
            os.close(run_fd)
    return active


def _stage_and_publish(intent: dict[str, Any], recovery_fd: int, staging_fd: int, generations_fd: int,
                       dependencies: WorkspaceDocumentDependencies,
                       registry_dependencies: WorkspaceRegistryDependencies,
                       registry_directories: dict[str, int]) -> None:
    run_id = intent["run_id"]; target = intent["target"]
    staging_names = _names(staging_fd)
    if any(name != run_id for name in staging_names): raise _fail("foreign workspace document staging state exists")
    generation_names = _names(generations_fd, _MAX_GENERATIONS)
    if target not in generation_names:
        if run_id not in staging_names:
            stage_fd, _ = _ensure_child(staging_fd, run_id)
            try: _write_file(stage_fd, "manifest.json", _json_bytes(intent["generation"]))
            finally: os.close(stage_fd)
        dependencies.failpoint("after_document_staging")
        _rename_noreplace(staging_fd, run_id, generations_fd, target)
    else:
        existing = _validate_generation(generations_fd, target, intent["generation"]["workspace_id"])
        if existing != intent["generation"]: raise _fail("workspace document publication target conflicts")
    dependencies.failpoint("after_document_publication")
    try:
        published = _publish_exact_generation_locked(
            intent["registry_generation"],
            registry_dependencies,
            registry_directories,
        )
        if published != intent["registry_generation"]:
            raise _fail("workspace document registry binding differs")
    except WorkspaceRegistryError as exc:
        raise _fail("workspace document registry binding differs", exc)
    dependencies.failpoint("after_document_registry_publication")
    run_fd, _ = _open_child(recovery_fd, run_id)
    try:
        names = _names(run_fd)
        if "completion.json" not in names:
            _write_file(run_fd, "completion.json", _json_bytes(_completion(
                run_id,
                intent["generation"]["generation_digest"],
                intent["registry_generation"]["registry_digest"],
            )))
    finally: os.close(run_fd)
    _compact_terminal_recovery(recovery_fd, generations_fd, intent["generation"]["workspace_id"])
    dependencies.failpoint("after_document_completion")


def publish_workspace_documents(dependencies: WorkspaceDocumentDependencies, workspace_id: str,
                                document_irs: tuple[dict[str, object], ...],
                                *,
                                expected_registry_digest: str | None = None,
                                expected_definition_digest: str | None = None) -> dict[str, object]:
    """Append one complete immutable generation under the selected workspace."""
    if not isinstance(dependencies, WorkspaceDocumentDependencies) or type(document_irs) is not tuple or not 1 <= len(document_irs) <= 1024:
        raise _fail("workspace document publication input is invalid")
    workspace_id = _workspace_id(workspace_id)
    runtime_root = Path(dependencies.runtime_root)
    registry_dependencies = _registry_dependencies(dependencies)
    with _locked_workspace_registry_storage(registry_dependencies, reject_partial=True) as registry_directories:
        selection = _registry_selection(runtime_root, workspace_id)
        binding_matches = _expected_registry_binding_matches(
            selection,
            expected_registry_digest,
            expected_definition_digest,
        )
        opened = _open_runtime_documents(runtime_root, workspace_id, create=False)
        created = opened is None
        if opened is None:
            if not binding_matches:
                raise _fail("workspace document registry binding differs")
            opened = _open_runtime_documents(runtime_root, workspace_id, create=True)
        assert opened is not None
        descriptors, chain = opened; extra: list[int] = []
        try:
            documents_fd = chain["documents"][0]
            if created:
                generations_fd, recovery_fd, staging_fd, lock_fd = _prepare_documents(documents_fd)
            else:
                generations_fd, recovery_fd, staging_fd, lock_fd = _validate_document_inventory(documents_fd)
            extra.extend((generations_fd, recovery_fd, staging_fd, lock_fd))
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if _active_recovery_pending(recovery_fd) or _names(staging_fd): raise _fail("workspace document recovery is pending")
            values = _load_generations(generations_fd, workspace_id); prior = None if not values else values[-1]
            if prior is not None:
                _ensure_current_registry_binding(selection, prior)
            elif selection.primary.get("document_generation_digest") is not None:
                raise _fail("workspace document registry binding differs")
            incoming = [_binding(copy.deepcopy(item)) for item in document_irs]
            incoming_ids = [item["document_id"] for item in incoming]
            if len(incoming_ids) != len(set(incoming_ids)): raise _fail("document publication contains duplicate identities")
            existing = {} if prior is None else {item["document_id"]: copy.deepcopy(item) for item in prior["documents"]}
            if prior is not None and all(item["document_id"] in existing and existing[item["document_id"]] == item for item in incoming):
                return _publication_result(prior, status="unchanged")
            if not binding_matches:
                raise _fail("workspace document registry binding differs")
            existing.update({item["document_id"]: item for item in incoming})
            clock = _dependency_clock(dependencies)
            generation = _generation(
                workspace_id,
                selection.primary["document_store_id"],
                selection.generation["registry_digest"],
                [existing[key] for key in sorted(existing)],
                prior,
                clock,
            )
            registry_generation = _registry_generation(
                selection, generation, clock,
            )
            run_id = "document-run-" + secrets.token_hex(16)
            target = f"{generation['sequence']:010d}-{generation['generation_id']}"
            intent = _intent(run_id, target, generation, registry_generation)
            run_fd, _ = _ensure_child(recovery_fd, run_id)
            try: _write_file(run_fd, "intent.json", _json_bytes(intent))
            finally: os.close(run_fd)
            dependencies.failpoint("after_document_intent")
            _stage_and_publish(
                intent,
                recovery_fd,
                staging_fd,
                generations_fd,
                dependencies,
                registry_dependencies,
                registry_directories,
            )
            _revalidate_chain(runtime_root, workspace_id, chain)
            return _publication_result(generation, status="published")
        except WorkspaceDocumentError: raise
        except (OSError, ContractError, BenchmarkError) as exc: raise _fail("workspace document publication failed", exc)
        finally:
            for descriptor in reversed(extra): os.close(descriptor)
            for descriptor in reversed(descriptors): os.close(descriptor)


def recover_workspace_documents(dependencies: WorkspaceDocumentDependencies, workspace_id: str) -> dict[str, object]:
    """Resume only an exact owned document publication intent."""
    if not isinstance(dependencies, WorkspaceDocumentDependencies): raise _fail("workspace document dependencies are invalid")
    workspace_id = _workspace_id(workspace_id)
    runtime_root = Path(dependencies.runtime_root)
    registry_dependencies = _registry_dependencies(dependencies)
    with _locked_workspace_registry_storage(registry_dependencies, reject_partial=True) as registry_directories:
        selection = _registry_selection(runtime_root, workspace_id)
        if not _document_root_path(runtime_root, workspace_id).exists():
            if selection.primary.get("document_generation_digest") is not None:
                raise _fail("workspace document registry binding differs")
            return {"status": "empty", "workspace_id": workspace_id, "generation_digest": None}
        opened = _open_runtime_documents(runtime_root, workspace_id, create=False)
        if opened is None: return {"status": "empty", "workspace_id": workspace_id, "generation_digest": None}
        descriptors, chain = opened; extra: list[int] = []
        try:
            documents_fd = chain["documents"][0]
            generations_fd, recovery_fd, staging_fd, lock_fd = _validate_document_inventory(documents_fd); extra.extend((generations_fd, recovery_fd, staging_fd, lock_fd))
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            run_names = _names(recovery_fd, _MAX_GENERATIONS)
            if any(_RUN.fullmatch(name) is None for name in run_names): raise _fail("foreign workspace document recovery state exists")
            staging_names = _names(staging_fd)
            if any(_RUN.fullmatch(name) is None for name in staging_names): raise _fail("foreign workspace document staging state exists")
            active: list[dict[str, Any]] = []; last_complete = None
            for name in run_names:
                run_fd, _ = _open_child(recovery_fd, name)
                try:
                    names = _names(run_fd); intent = _read_intent(run_fd)
                    if intent["run_id"] != name or set(names) not in ({"intent.json"}, {"intent.json", "completion.json"}): raise _fail("workspace document recovery inventory differs")
                    if "completion.json" in names:
                        _read_completion(run_fd, name, intent)
                        _validate_published_generation(intent, generations_fd, workspace_id)
                        last_complete = intent
                    else: active.append(intent)
                finally: os.close(run_fd)
            if len(active) > 1: raise _fail("multiple workspace document publications require investigation")
            if not active:
                if staging_names: raise _fail("foreign workspace document staging state exists")
                values = _load_generations(generations_fd, workspace_id)
                if values:
                    _ensure_current_registry_binding(selection, values[-1])
                elif selection.primary.get("document_generation_digest") is not None:
                    raise _fail("workspace document registry binding differs")
                digest = None if not values else values[-1]["generation_digest"]
                return {"status": "complete" if last_complete else "empty", "workspace_id": workspace_id, "generation_digest": digest}
            intent = active[0]
            if intent["generation"]["workspace_id"] != workspace_id: raise _fail("workspace document recovery origin differs")
            if staging_names and staging_names != [intent["run_id"]]: raise _fail("foreign workspace document staging state exists")
            _stage_and_publish(
                intent,
                recovery_fd,
                staging_fd,
                generations_fd,
                dependencies,
                registry_dependencies,
                registry_directories,
            )
            values = _load_generations(generations_fd, workspace_id)
            if not values or values[-1]["generation_digest"] != intent["generation"]["generation_digest"]: raise _fail("workspace document recovery did not converge")
            selection = _registry_selection(runtime_root, workspace_id)
            _ensure_current_registry_binding(selection, values[-1])
            _revalidate_chain(runtime_root, workspace_id, chain)
            return {"status": "recovered", "workspace_id": workspace_id, "generation_digest": values[-1]["generation_digest"]}
        except WorkspaceDocumentError: raise
        except (OSError, ContractError, BenchmarkError) as exc: raise _fail("workspace document recovery failed", exc)
        finally:
            for descriptor in reversed(extra): os.close(descriptor)
            for descriptor in reversed(descriptors): os.close(descriptor)
