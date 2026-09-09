"""Descriptor-coherent immutable workspace registry storage."""

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
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from ._strict_io import ContractError, parse_strict_json
from .benchmark import BenchmarkError, canonical_digest
from .workspace_contracts import (
    AUTHORITY_FIELDS,
    WORKSPACE_SCHEMA_VERSIONS,
    validate_workspace_definition,
    validate_workspace_registry_generation,
    validate_workspace_registry_inspection,
)


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_GENERATION_RE = re.compile(r"^(?P<sequence>[0-9]{10})-(?P<registry>[a-z0-9][a-z0-9._-]{0,127})$")
_DEFINITION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}\.json$")
_RUN_RE = re.compile(r"^registry-run-[0-9a-f]{32}$")
_MAX_FILE_BYTES = 1024 * 1024
_MAX_GENERATIONS = 128
_MAX_DIRECTORY_ENTRIES = 256
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


class WorkspaceRegistryError(ValueError):
    """Registry state is absent, unsafe, inconsistent, or conflicting."""


def _system_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class WorkspaceRegistryDependencies:
    runtime_root: Path
    failpoint: Callable[[str], None] = lambda _name: None


@dataclass(frozen=True)
class _WorkspaceRegistryProofDependencies(WorkspaceRegistryDependencies):
    clock: Callable[[], str] = _system_now


def _dependency_clock(deps: WorkspaceRegistryDependencies) -> Callable[[], str]:
    if type(deps) is _WorkspaceRegistryProofDependencies:
        return deps.clock
    return _system_now


@dataclass(frozen=True)
class WorkspaceRegistrySnapshot:
    generations: tuple[dict[str, Any], ...]
    workspaces: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class WorkspaceSelection:
    generation: dict[str, Any]
    primary: dict[str, Any]
    references: tuple[dict[str, Any], ...]


def _fail(message: str, exc: BaseException | None = None) -> WorkspaceRegistryError:
    error = WorkspaceRegistryError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _bind(value: dict[str, Any], field: str) -> dict[str, Any]:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def _names(descriptor: int, maximum: int = _MAX_DIRECTORY_ENTRIES) -> list[str]:
    names = sorted(os.listdir(descriptor))
    if len(names) > maximum:
        raise _fail("workspace registry entry budget exceeded")
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise _fail("workspace registry names alias")
    return names


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


def _open_or_create_runtime(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    try:
        return _open_absolute(absolute)
    except FileNotFoundError:
        if absolute == Path("/") or not absolute.name:
            raise
        parent = _open_absolute(absolute.parent)
        try:
            try:
                os.mkdir(absolute.name, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            descriptor, _ = _open_child(parent, absolute.name)
            return descriptor
        finally:
            os.close(parent)


def _open_child(parent: int, name: str) -> tuple[int, tuple[int, int]]:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise _fail("workspace registry directory is invalid")
    return descriptor, (info.st_dev, info.st_ino)


def _revalidate_child(parent: int, name: str, descriptor: int, identity: tuple[int, int]) -> None:
    held = os.fstat(descriptor)
    rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISDIR(held.st_mode) or not stat.S_ISDIR(rebound.st_mode)
            or (held.st_dev, held.st_ino) != identity
            or (rebound.st_dev, rebound.st_ino) != identity):
        raise _fail("workspace registry directory changed")


def _read_regular(parent: int, name: str, maximum: int = _MAX_FILE_BYTES) -> bytes:
    descriptor = None
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise _fail("workspace registry file is invalid")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(before) != identity(opened):
            raise _fail("workspace registry file changed")
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
        if len(body) > maximum or len(body) != opened.st_size or identity(opened) != identity(after) or identity(after) != identity(rebound):
            raise _fail("workspace registry file changed")
        return body
    except WorkspaceRegistryError:
        raise
    except OSError as exc:
        raise _fail("workspace registry file is invalid", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json(parent: int, name: str, label: str) -> dict[str, Any]:
    try:
        value = parse_strict_json(_read_regular(parent, name), label)
    except ContractError as exc:
        raise _fail(f"{label} is invalid", exc)
    if type(value) is not dict:
        raise _fail(f"{label} is invalid")
    return value


def _validate_generation_directory(parent: int, name: str) -> dict[str, Any]:
    match = _GENERATION_RE.fullmatch(name)
    if match is None or int(match.group("sequence")) < 1:
        raise _fail("workspace registry generation name is invalid")
    directory, identity = _open_child(parent, name)
    definitions_fd = None
    try:
        if _names(directory) != ["definitions", "manifest.json"]:
            raise _fail("workspace registry generation inventory differs")
        manifest = validate_workspace_registry_generation(_read_json(directory, "manifest.json", "workspace registry manifest"))
        if manifest["sequence"] != int(match.group("sequence")) or manifest["registry_id"] != match.group("registry"):
            raise _fail("workspace registry generation name binding differs")
        definitions_fd, definitions_identity = _open_child(directory, "definitions")
        expected_names = [item["workspace_id"] + ".json" for item in manifest["workspaces"]]
        if _names(definitions_fd, 64) != expected_names:
            raise _fail("workspace definition inventory differs")
        detached = []
        for expected, filename in zip(manifest["workspaces"], expected_names):
            actual = validate_workspace_definition(_read_json(definitions_fd, filename, "workspace definition"))
            if actual != expected or _json_bytes(actual) != _read_regular(definitions_fd, filename):
                raise _fail("workspace definition binding differs")
            detached.append(actual)
        manifest["workspaces"] = detached
        _revalidate_child(directory, "definitions", definitions_fd, definitions_identity)
        _revalidate_child(parent, name, directory, identity)
        return manifest
    except (ContractError, BenchmarkError) as exc:
        raise _fail("workspace registry generation is invalid", exc)
    finally:
        if definitions_fd is not None:
            os.close(definitions_fd)
        os.close(directory)


def _validate_identity_collisions(workspaces: Iterable[dict[str, Any]]) -> None:
    values = list(workspaces)
    for field in ("workspace_id", "root_workflow_id", "source_registry_id", "graph_id"):
        identities = [item[field] for item in values if item[field] is not None]
        if len(identities) != len(set(identities)):
            raise _fail("workspace registry identity collision")


def load_workspace_registry(deps: WorkspaceRegistryDependencies) -> WorkspaceRegistrySnapshot:
    """Replay the complete descriptor-coherent registry generation chain."""
    if not isinstance(deps, WorkspaceRegistryDependencies):
        raise _fail("workspace registry dependencies are invalid")
    root_path = Path(deps.runtime_root)
    if not root_path.exists():
        return WorkspaceRegistrySnapshot((), ())
    descriptors: list[int] = []
    try:
        root = _open_absolute(root_path); descriptors.append(root)
        root_identity = tuple((lambda info: (info.st_dev, info.st_ino))(os.fstat(root)))
        root_names = _names(root)
        if "workspaces" not in root_names:
            return WorkspaceRegistrySnapshot((), ())
        workspaces_fd, workspaces_identity = _open_child(root, "workspaces"); descriptors.append(workspaces_fd)
        if any(name not in {"registry", "state"} for name in _names(workspaces_fd)):
            raise _fail("workspace root inventory differs")
        if "registry" not in _names(workspaces_fd):
            raise _fail("workspace registry is partial")
        registry_fd, registry_identity = _open_child(workspaces_fd, "registry"); descriptors.append(registry_fd)
        allowed_registry = {"generations", "registry.lock", "recovery", "staging"}
        registry_names = _names(registry_fd)
        if any(name not in allowed_registry for name in registry_names) or "generations" not in registry_names:
            raise _fail("workspace registry inventory differs")
        if "registry.lock" in registry_names:
            lock_info = os.stat("registry.lock", dir_fd=registry_fd, follow_symlinks=False)
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1 or lock_info.st_size != 0:
                raise _fail("workspace registry lock is invalid")
        optional_descriptors: dict[str, int] = {}
        for optional_directory in ("recovery", "staging"):
            if optional_directory in registry_names:
                optional_fd, _ = _open_child(registry_fd, optional_directory)
                descriptors.append(optional_fd)
                optional_descriptors[optional_directory] = optional_fd
        if len(optional_descriptors) == 1:
            raise _fail("workspace registry recovery inventory is partial")
        generations_fd, generations_identity = _open_child(registry_fd, "generations"); descriptors.append(generations_fd)
        generation_names = _names(generations_fd, _MAX_GENERATIONS)
        generations = [_validate_generation_directory(generations_fd, name) for name in generation_names]
        for expected_sequence, generation in enumerate(generations, 1):
            if generation["sequence"] != expected_sequence:
                raise _fail("workspace registry sequence is not contiguous")
            expected_predecessor = None if expected_sequence == 1 else generations[expected_sequence - 2]["registry_digest"]
            if generation["predecessor_registry_digest"] != expected_predecessor:
                raise _fail("workspace registry lineage differs")
            _validate_identity_collisions(generation["workspaces"])
        if optional_descriptors:
            inventory = _classify_recovery_inventory({
                "recovery": optional_descriptors["recovery"],
                "staging": optional_descriptors["staging"],
                "generations": generations_fd,
            })
            if inventory["foreign"] or inventory["owned_runs"] or any(
                    value is None for value in inventory["terminals"].values()):
                raise _fail("workspace registry recovery requires investigation")
        deps.failpoint("before_registry_revalidation")
        _revalidate_child(registry_fd, "generations", generations_fd, generations_identity)
        _revalidate_child(workspaces_fd, "registry", registry_fd, registry_identity)
        _revalidate_child(root, "workspaces", workspaces_fd, workspaces_identity)
        rebound = os.stat(root_path, follow_symlinks=False)
        if root_identity != (rebound.st_dev, rebound.st_ino):
            raise _fail("workspace runtime root changed")
        latest = () if not generations else tuple(copy.deepcopy(generations[-1]["workspaces"]))
        return WorkspaceRegistrySnapshot(tuple(copy.deepcopy(generations)), latest)
    except WorkspaceRegistryError:
        raise
    except (OSError, ContractError, BenchmarkError) as exc:
        raise _fail("workspace registry is invalid", exc)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def select_workspace(snapshot: WorkspaceRegistrySnapshot, workspace_id: str) -> WorkspaceSelection:
    if not isinstance(snapshot, WorkspaceRegistrySnapshot) or type(workspace_id) is not str:
        raise _fail("workspace selection is invalid")
    if not snapshot.generations:
        raise _fail("workspace is unknown")
    generation = copy.deepcopy(snapshot.generations[-1])
    by_id = {item["workspace_id"]: item for item in generation["workspaces"]}
    primary = by_id.get(workspace_id)
    if primary is None:
        raise _fail("workspace is unknown")
    if primary["lifecycle_status"] != "active":
        raise _fail("workspace is inactive")
    references = []
    for reference_id in primary["reference_workspace_ids"]:
        reference = by_id.get(reference_id)
        if reference is None or reference["workspace_type"] != "reference" or reference["lifecycle_status"] != "active":
            raise _fail("workspace reference is unavailable")
        references.append(copy.deepcopy(reference))
    return WorkspaceSelection(generation, copy.deepcopy(primary), tuple(sorted(references, key=lambda item: item["workspace_id"])))


def inspect_workspace_registry(snapshot: WorkspaceRegistrySnapshot) -> dict[str, Any]:
    if not isinstance(snapshot, WorkspaceRegistrySnapshot):
        raise _fail("workspace registry snapshot is invalid")
    if not snapshot.generations:
        identity = canonical_digest({"registry": None, "operation": "inspect"})
        body = {
            "schema_version": WORKSPACE_SCHEMA_VERSIONS[2],
            "inspection_id": "inspection-" + identity.split(":", 1)[1][:24],
            "registry_id": None, "registry_digest": None, "sequence": 0,
            "status": "empty", "reason_code": "ok", "workspace_ids": [],
            "workspace_count": 0, "active_count": 0, "inactive_count": 0,
            "investigate_count": 0, "inspection_digest": "sha256:" + "0" * 64,
            **{field: False for field in AUTHORITY_FIELDS},
        }
        return validate_workspace_registry_inspection(_bind(body, "inspection_digest"), generation=None)
    generation = snapshot.generations[-1]
    ids = sorted(item["workspace_id"] for item in generation["workspaces"])
    counts = {status: sum(item["lifecycle_status"] == status for item in generation["workspaces"])
              for status in ("active", "inactive", "investigate")}
    identity = canonical_digest({"registry": generation["registry_digest"], "operation": "inspect"})
    body = {
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[2], "inspection_id": "inspection-" + identity.split(":", 1)[1][:24],
        "registry_id": generation["registry_id"], "registry_digest": generation["registry_digest"],
        "sequence": generation["sequence"], "status": "active", "reason_code": "ok",
        "workspace_ids": ids, "workspace_count": len(ids), "active_count": counts["active"],
        "inactive_count": counts["inactive"], "investigate_count": counts["investigate"],
        "inspection_digest": "sha256:" + "0" * 64, **{field: False for field in AUTHORITY_FIELDS},
    }
    return validate_workspace_registry_inspection(_bind(body, "inspection_digest"), generation=generation)


def _mkdir(parent: int, name: str) -> tuple[int, tuple[int, int]]:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError:
        pass
    return _open_child(parent, name)


def _mkdir_exclusive(parent: int, name: str) -> tuple[int, tuple[int, int]]:
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
        os.fsync(parent)
    except FileExistsError as exc:
        raise _fail("workspace registry bootstrap raced", exc)
    return _open_child(parent, name)


@contextmanager
def _locked_storage(deps: WorkspaceRegistryDependencies, *, reject_partial: bool = False):
    descriptors: list[int] = []
    lock = None
    root_locked = False
    try:
        root = _open_or_create_runtime(Path(deps.runtime_root)); descriptors.append(root)
        fcntl.flock(root, fcntl.LOCK_EX); root_locked = True
        root_names = _names(root)
        workspace_preexisted = "workspaces" in root_names
        if workspace_preexisted:
            workspaces, _ = _open_child(root, "workspaces")
        else:
            workspaces, _ = (_mkdir_exclusive(root, "workspaces") if reject_partial
                             else _mkdir(root, "workspaces"))
        descriptors.append(workspaces)
        workspace_names = _names(workspaces)
        if any(name not in {"registry", "state"} for name in workspace_names):
            raise _fail("workspace root inventory differs")
        if reject_partial and workspace_preexisted and "registry" not in workspace_names:
            raise _fail("workspace registry is partial")
        registry_preexisted = "registry" in workspace_names
        if registry_preexisted:
            registry, _ = _open_child(workspaces, "registry")
        else:
            registry, _ = (_mkdir_exclusive(workspaces, "registry") if reject_partial
                            else _mkdir(workspaces, "registry"))
        descriptors.append(registry)
        required = {"generations", "recovery", "staging", "registry.lock"}
        registry_names = set(_names(registry))
        if reject_partial and registry_preexisted and registry_names != required:
            raise _fail("workspace registry is partial")
        if registry_preexisted and "registry.lock" in registry_names:
            lock = os.open("registry.lock", os.O_RDWR | os.O_NOFOLLOW, dir_fd=registry)
        else:
            lock = os.open("registry.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=registry)
        lock_info = os.fstat(lock)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1 or lock_info.st_size != 0:
            raise _fail("workspace registry lock is invalid")
        fcntl.flock(lock, fcntl.LOCK_EX)
        if any(name not in required for name in _names(registry)):
            raise _fail("workspace registry inventory differs")
        directories = {"root": root, "workspaces": workspaces, "registry": registry}
        for name in ("generations", "recovery", "staging"):
            if name in registry_names:
                child, _ = _open_child(registry, name)
            else:
                child, _ = (_mkdir_exclusive(registry, name) if reject_partial
                            else _mkdir(registry, name))
            descriptors.append(child); directories[name] = child
        yield directories
    except WorkspaceRegistryError:
        raise
    except OSError as exc:
        raise _fail("workspace registry storage is invalid", exc)
    finally:
        if lock is not None:
            try:
                fcntl.flock(lock, fcntl.LOCK_UN)
            finally:
                os.close(lock)
        if root_locked and descriptors:
            fcntl.flock(descriptors[0], fcntl.LOCK_UN)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _write_exclusive(parent: int, name: str, body: bytes) -> None:
    if len(body) > _MAX_FILE_BYTES:
        raise _fail("workspace registry byte budget exceeded")
    descriptor = None
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(body):
            raise _fail("workspace registry publication is invalid")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    os.fsync(parent)


def _put_exact(parent: int, name: str, body: bytes) -> None:
    try:
        _write_exclusive(parent, name, body)
    except FileExistsError:
        if _read_regular(parent, name) != body:
            raise _fail("workspace registry immutable state conflicts")


def _rename_noreplace(source_parent: int, source: str, destination_parent: int, destination: str) -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise _fail("Linux no-replace publication is unavailable")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(source_parent, os.fsencode(source), destination_parent, os.fsencode(destination), _RENAME_NOREPLACE)
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        return False
    raise OSError(error, os.strerror(error))


def _intent_value(run_id: str, generation: dict[str, Any], directory: str) -> dict[str, Any]:
    body = {
        "schema_version": "ao.lore.workspace-registry-intent.v0.1", "run_id": run_id,
        "generation_directory": directory, "generation": generation,
        "generation_digest": generation["registry_digest"], "intent_digest": "sha256:" + "0" * 64,
    }
    return _bind(body, "intent_digest")


def _completed_value(intent: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema_version": "ao.lore.workspace-registry-completed.v0.1", "run_id": intent["run_id"],
        "generation_directory": intent["generation_directory"],
        "generation_digest": intent["generation_digest"], "result": "complete",
        "completed_digest": "sha256:" + "0" * 64,
    }
    return _bind(body, "completed_digest")


def _validate_intent(value: Any, run_id: str) -> dict[str, Any]:
    keys = {"schema_version", "run_id", "generation_directory", "generation", "generation_digest", "intent_digest"}
    if type(value) is not dict or set(value) != keys or value.get("schema_version") != "ao.lore.workspace-registry-intent.v0.1" or value.get("run_id") != run_id:
        raise _fail("workspace registry intent is invalid")
    generation = validate_workspace_registry_generation(value["generation"])
    expected_directory = f"{generation['sequence']:010d}-{generation['registry_id']}"
    if value["generation_directory"] != expected_directory or value["generation_digest"] != generation["registry_digest"]:
        raise _fail("workspace registry intent binding differs")
    expected = canonical_digest({key: item for key, item in value.items() if key != "intent_digest"})
    if value["intent_digest"] != expected:
        raise _fail("workspace registry intent digest differs")
    return copy.deepcopy(value)


def _validate_completed(value: Any, run_id: str) -> dict[str, Any]:
    keys = {"schema_version", "run_id", "generation_directory", "generation_digest", "result", "completed_digest"}
    if type(value) is not dict or set(value) != keys or value.get("schema_version") != "ao.lore.workspace-registry-completed.v0.1" or value.get("run_id") != run_id or value.get("result") != "complete":
        raise _fail("workspace registry completion is invalid")
    expected = canonical_digest({key: item for key, item in value.items() if key != "completed_digest"})
    if value["completed_digest"] != expected:
        raise _fail("workspace registry completion digest differs")
    return copy.deepcopy(value)


def _stage_generation(directories: dict[str, int], intent: dict[str, Any], failpoint: Callable[[str], None]) -> None:
    staging = directories["staging"]
    run_id = intent["run_id"]
    names = _names(staging)
    if run_id in names:
        stage, _ = _open_child(staging, run_id)
    else:
        stage, _ = _mkdir(staging, run_id)
    definitions_fd = None
    try:
        allowed = {"manifest.json", "definitions"}
        if any(name not in allowed for name in _names(stage)):
            raise _fail("workspace registry staging conflicts")
        generation = intent["generation"]
        _put_exact(stage, "manifest.json", _json_bytes(generation)); failpoint("after_staged_file_fsync")
        definitions_fd, _ = _mkdir(stage, "definitions")
        expected = {item["workspace_id"] + ".json" for item in generation["workspaces"]}
        if any(name not in expected for name in _names(definitions_fd, 64)):
            raise _fail("workspace registry staging conflicts")
        for definition in generation["workspaces"]:
            _put_exact(definitions_fd, definition["workspace_id"] + ".json", _json_bytes(definition))
            failpoint("after_staged_file_fsync")
        os.fsync(definitions_fd)
        os.fsync(stage)
        failpoint("after_stage_directory_fsync")
    finally:
        if definitions_fd is not None:
            os.close(definitions_fd)
        os.close(stage)


def _unlink_exact(parent: int, name: str, expected: bytes) -> None:
    if _read_regular(parent, name) != expected:
        raise _fail("workspace registry cleanup binding differs")
    os.unlink(name, dir_fd=parent)
    os.fsync(parent)


def _cleanup_stage(directories: dict[str, int], intent: dict[str, Any]) -> None:
    staging = directories["staging"]
    run_id = intent["run_id"]
    if run_id not in _names(staging):
        return
    stage, stage_identity = _open_child(staging, run_id)
    definitions_fd = None
    try:
        generation = intent["generation"]
        if _names(stage) != ["definitions", "manifest.json"] or _read_regular(stage, "manifest.json") != _json_bytes(generation):
            raise _fail("workspace registry staging cleanup differs")
        definitions_fd, definitions_identity = _open_child(stage, "definitions")
        expected_names = [item["workspace_id"] + ".json" for item in generation["workspaces"]]
        if _names(definitions_fd, 64) != expected_names:
            raise _fail("workspace registry staging cleanup differs")
        for definition, name in zip(generation["workspaces"], expected_names):
            _unlink_exact(definitions_fd, name, _json_bytes(definition))
        _revalidate_child(stage, "definitions", definitions_fd, definitions_identity)
        os.close(definitions_fd); definitions_fd = None
        os.rmdir("definitions", dir_fd=stage); os.fsync(stage)
        _unlink_exact(stage, "manifest.json", _json_bytes(generation))
        _revalidate_child(staging, run_id, stage, stage_identity)
        os.close(stage); stage = -1
        os.rmdir(run_id, dir_fd=staging); os.fsync(staging)
    finally:
        if definitions_fd is not None:
            os.close(definitions_fd)
        if stage >= 0:
            os.close(stage)


def _resume_intent(directories: dict[str, int], intent: dict[str, Any], failpoint: Callable[[str], None]) -> dict[str, Any]:
    generation = intent["generation"]
    destination = intent["generation_directory"]
    generations = directories["generations"]
    if destination not in _names(generations, _MAX_GENERATIONS):
        _stage_generation(directories, intent, failpoint)
        if not _rename_noreplace(directories["staging"], intent["run_id"], generations, destination):
            if destination not in _names(generations, _MAX_GENERATIONS):
                raise _fail("workspace registry generation publication conflicts")
        else:
            failpoint("after_generation_rename")
            os.fsync(generations)
            failpoint("after_generations_fsync")
    published = _validate_generation_directory(generations, destination)
    published_fd, _ = _open_child(generations, destination)
    try:
        if published != generation or _json_bytes(published) != _read_regular(published_fd, "manifest.json"):
            raise _fail("workspace registry published generation differs")
    finally:
        os.close(published_fd)
    failpoint("after_published_reopen")
    completed = _completed_value(intent)
    completed_name = intent["run_id"] + ".completed.json"
    _put_exact(directories["recovery"], completed_name, _json_bytes(completed))
    failpoint("after_completed_evidence")
    _cleanup_stage(directories, intent)
    failpoint("after_staging_cleanup")
    intent_name = intent["run_id"] + ".intent.json"
    if intent_name in _names(directories["recovery"]):
        _unlink_exact(directories["recovery"], intent_name, _json_bytes(intent))
    failpoint("after_intent_cleanup")
    return generation


def _classify_recovery_inventory(directories: dict[str, int]) -> dict[str, Any]:
    recovery_names = _names(directories["recovery"], _MAX_DIRECTORY_ENTRIES)
    staging_names = _names(directories["staging"], _MAX_DIRECTORY_ENTRIES)
    owned_runs = {name.removesuffix(".intent.json") for name in recovery_names if name.endswith(".intent.json") and _RUN_RE.fullmatch(name.removesuffix(".intent.json"))}
    completed_runs = {name.removesuffix(".completed.json") for name in recovery_names if name.endswith(".completed.json") and _RUN_RE.fullmatch(name.removesuffix(".completed.json"))}
    allowed_recovery = {run + ".intent.json" for run in owned_runs} | {run + ".completed.json" for run in completed_runs}
    foreign = set(recovery_names) - allowed_recovery
    foreign.update(name for name in staging_names if name not in owned_runs)
    terminals: dict[str, dict[str, Any] | None] = {}
    for run_id in sorted(completed_runs - owned_runs):
        try:
            completed = _validate_completed(_read_json(directories["recovery"], run_id + ".completed.json", "workspace registry completion"), run_id)
            published = _validate_generation_directory(directories["generations"], completed["generation_directory"])
            if published["registry_digest"] != completed["generation_digest"]:
                raise _fail("workspace registry completion binding differs")
            terminals[run_id] = completed
        except (WorkspaceRegistryError, ContractError, OSError, BenchmarkError):
            terminals[run_id] = None
    return {"owned_runs": owned_runs, "completed_runs": completed_runs,
            "foreign": foreign, "terminals": terminals}


def _recover_locked(directories: dict[str, int], failpoint: Callable[[str], None]) -> list[dict[str, Any]]:
    inventory = _classify_recovery_inventory(directories)
    owned_runs = inventory["owned_runs"]
    outcomes: list[dict[str, Any]] = []
    if inventory["foreign"]:
        outcomes.append({"result": "investigate", "reason_code": "recovery_pending"})
    for run_id, completed in inventory["terminals"].items():
        if completed is not None:
            outcomes.append({"result": "complete", "reason_code": "ok", "run_id": run_id,
                             "generation_digest": completed["generation_digest"]})
        else:
            outcomes.append({"result": "investigate", "reason_code": "recovery_pending", "run_id": run_id})
    for run_id in sorted(owned_runs):
        try:
            intent = _validate_intent(_read_json(directories["recovery"], run_id + ".intent.json", "workspace registry intent"), run_id)
            generation = _resume_intent(directories, intent, failpoint)
            outcomes.append({"result": "complete", "reason_code": "ok", "run_id": run_id,
                             "generation_digest": generation["registry_digest"]})
        except (WorkspaceRegistryError, ContractError, OSError, BenchmarkError):
            outcomes.append({"result": "investigate", "reason_code": "recovery_pending", "run_id": run_id})
    return outcomes


def _normalized_definitions(definitions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(definitions, (str, bytes, dict)):
        raise _fail("workspace definitions are invalid")
    try:
        values = [validate_workspace_definition(copy.deepcopy(item)) for item in definitions]
    except (ContractError, TypeError) as exc:
        raise _fail("workspace definitions are invalid", exc)
    values.sort(key=lambda item: item["workspace_id"])
    if len(values) > 64:
        raise _fail("workspace definition budget exceeded")
    _validate_identity_collisions(values)
    return values


def _assert_generation_advances(previous: dict[str, Any] | None,
                                generation: dict[str, Any]) -> None:
    if previous is None:
        if generation["sequence"] != 1 or generation["predecessor_registry_digest"] is not None:
            raise _fail("workspace registry generation publication conflicts")
        return
    if (generation["sequence"] != previous["sequence"] + 1
            or generation["predecessor_registry_digest"] != previous["registry_digest"]):
        raise _fail("workspace registry generation publication conflicts")
    previous_by = {item["workspace_id"]: item for item in previous["workspaces"]}
    for item in generation["workspaces"]:
        retained = previous_by.get(item["workspace_id"])
        if retained is not None and (
            item["workspace_version"] < retained["workspace_version"]
            or (item["workspace_version"] == retained["workspace_version"] and item != retained)
        ):
            raise _fail("workspace definition version does not advance")


def _publish_exact_generation_locked(generation: dict[str, Any],
                                     deps: WorkspaceRegistryDependencies,
                                     directories: dict[str, int]) -> dict[str, Any]:
    recovery = _recover_locked(directories, lambda _name: None)
    if any(item["result"] != "complete" for item in recovery):
        raise _fail("workspace registry recovery requires investigation")
    snapshot = load_workspace_registry(deps)
    current = None if not snapshot.generations else snapshot.generations[-1]
    if current == generation:
        return copy.deepcopy(current)
    _assert_generation_advances(current, generation)
    run_id = "registry-run-" + secrets.token_hex(16)
    directory = f"{generation['sequence']:010d}-{generation['registry_id']}"
    intent = _intent_value(run_id, generation, directory)
    _write_exclusive(directories["recovery"], run_id + ".intent.json", _json_bytes(intent))
    deps.failpoint("after_intent_fsync")
    return copy.deepcopy(_resume_intent(directories, intent, deps.failpoint))


def publish_exact_workspace_registry_generation(generation: dict[str, Any],
                                                deps: WorkspaceRegistryDependencies) -> dict[str, Any]:
    """Publish one exact validated immutable generation or fail closed on drift."""
    try:
        candidate = validate_workspace_registry_generation(copy.deepcopy(generation))
    except (ContractError, TypeError, BenchmarkError) as exc:
        raise _fail("workspace registry generation is invalid", exc)
    with _locked_storage(deps, reject_partial=True) as directories:
        return _publish_exact_generation_locked(candidate, deps, directories)


def publish_workspace_registry_generation(definitions: Iterable[dict[str, Any]], deps: WorkspaceRegistryDependencies) -> dict[str, Any]:
    """Publish one complete immutable generation or return an exact retry."""
    proposed = _normalized_definitions(definitions)
    with _locked_storage(deps, reject_partial=True) as directories:
        recovery = _recover_locked(directories, lambda _name: None)
        if any(item["result"] != "complete" for item in recovery):
            raise _fail("workspace registry recovery requires investigation")
        snapshot = load_workspace_registry(deps)
        if snapshot.generations and snapshot.generations[-1]["workspaces"] == proposed:
            return copy.deepcopy(snapshot.generations[-1])
        previous = None if not snapshot.generations else snapshot.generations[-1]
        sequence = 1 if previous is None else previous["sequence"] + 1
        generated_at = _dependency_clock(deps)()
        seed = canonical_digest({"sequence": sequence, "predecessor": None if previous is None else previous["registry_digest"], "workspaces": proposed})
        registry_id = "registry-" + seed.split(":", 1)[1][:24]
        generation = {
            "schema_version": WORKSPACE_SCHEMA_VERSIONS[1], "registry_id": registry_id,
            "sequence": sequence, "predecessor_registry_digest": None if previous is None else previous["registry_digest"],
            "generated_at": generated_at, "workspaces": proposed, "registry_digest": "sha256:" + "0" * 64,
            **{field: False for field in AUTHORITY_FIELDS},
        }
        generation = validate_workspace_registry_generation(_bind(generation, "registry_digest"))
        return _publish_exact_generation_locked(generation, deps, directories)


def recover_workspace_registry(deps: WorkspaceRegistryDependencies) -> tuple[dict[str, Any], ...]:
    """Resume exact owned registry intents and preserve every uncertain entry."""
    with _locked_storage(deps) as directories:
        return tuple(copy.deepcopy(_recover_locked(directories, deps.failpoint)))
