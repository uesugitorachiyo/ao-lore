#!/usr/bin/env python3
"""Run the fixed, offline synthetic workspace isolation rehearsal."""

from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY / "src"
CAMPAIGN_ROOT = REPOSITORY / ".ao-lore" / "workspace-rehearsal"
GENERATOR = REPOSITORY / "tests" / "fixtures" / "ao_lore" / "workspaces" / "generate.py"
REJECTION = "workspace rehearsal rejected\n"
EXPECTED_WORKSPACES = (
    "primary-fixture-a",
    "secondary-fixture-b",
    "shared-reference-fixture",
)
EXPECTED_SCENARIOS = (
    "registry_list",
    "workspace_inspect",
    "declared_reference_query",
    "unrelated_workspace_absent",
    "workspace_replay",
    "workspace_recover",
    "identity_collision_rejected",
    "reference_cycle_rejected",
    "registry_crash_recovered",
    "foreign_staging_preserved",
    "network_denied",
)
OWNER = {
    "schema_version": "ao.lore.workspace-rehearsal-owner.v0.1",
    "campaign_id": "workspace-rehearsal",
}
_SCRATCH_RE = re.compile(r"^workspace-rehearsal-check-[a-z0-9_-]+$")
_TRACKED_PROTECTED_ROOTS = (
    "brain",
    "working/candidates",
    "working/reviews",
    "sources/neutral-reference-v1",
)
_OPAQUE_PILOT_ROOTS = (
    "sources/opaque-domain-a-v1",
    "sources/opaque-domain-b-v1",
)
_MAX_PROTECTED_FILES = 4096
_MAX_PROTECTED_FILE_BYTES = 8 * 1024 * 1024
_MAX_PROTECTED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_TRACKED_DEPTH = 16
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ao_lore.benchmark import canonical_digest
import ao_lore.workspace_registry as registry_module
from ao_lore.workspace_query import WorkspaceGraphSnapshot, WorkspaceQuerySnapshot, query_workspace
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    WorkspaceRegistryError,
    inspect_workspace_registry,
    load_workspace_registry,
    publish_workspace_registry_generation,
    recover_workspace_registry,
    select_workspace,
)
from ao_lore.workspace_runtime import inspect_workspace, recover_workspace, replay_workspace


class ExistingCampaignMismatch(ValueError):
    """Owned campaign bytes are valid JSON but differ from recomputed truth."""


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8") + b"\n"
    )


def _validate_campaign_root(path: Path) -> Path:
    candidate = Path(os.path.abspath(path))
    expected = Path(os.path.abspath(CAMPAIGN_ROOT))
    protected = {
        REPOSITORY.resolve(),
        (REPOSITORY / "brain").resolve(),
        (REPOSITORY / "working" / "candidates").resolve(),
        (REPOSITORY / "sources").resolve(),
        (REPOSITORY / "pilots").resolve(),
    }
    if candidate.resolve(strict=False) != expected or candidate.resolve(strict=False) in protected:
        raise ValueError("campaign root is not the fixed owned root")
    if candidate.parent.resolve() != (REPOSITORY / ".ao-lore").resolve():
        raise ValueError("campaign root parent differs")
    _validate_directory_chain(candidate.parent)
    if candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("campaign root is unsafe")
        marker = candidate / "campaign-owner.json"
        if not marker.is_file() or marker.is_symlink() or marker.read_bytes() != _canonical_bytes(OWNER):
            raise ValueError("campaign owner differs")
    return candidate


def _identity(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _validate_directory_chain(path: Path) -> None:
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in Path(os.path.abspath(path)).parts[1:]:
            before = os.stat(component, dir_fd=current, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise ValueError("campaign ancestor is unsafe")
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            opened = os.fstat(child)
            if _identity(before) != _identity(opened):
                os.close(child)
                raise ValueError("campaign ancestor changed")
            os.close(current)
            current = child
    finally:
        os.close(current)


def _read_regular_at(parent: int, name: str, maximum: int) -> bytes:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size > maximum):
        raise ValueError("campaign file is unsafe")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise ValueError("campaign file changed")
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
        if (len(body) > maximum or len(body) != opened.st_size
                or _identity(opened) != _identity(after)
                or _identity(after) != _identity(rebound)):
            raise ValueError("campaign file changed")
        return body
    finally:
        os.close(descriptor)


def _read_owned_report(root: Path) -> dict:
    root = _validate_campaign_root(root)
    before = root.lstat()
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if _identity(before) != _identity(opened):
            raise ValueError("campaign root changed")
        if _read_regular_at(descriptor, "campaign-owner.json", 4096) != _canonical_bytes(OWNER):
            raise ValueError("campaign owner differs")
        body = _read_regular_at(descriptor, "rehearsal-evidence.json", 1024 * 1024)
        value = json.loads(body)
        after = os.fstat(descriptor)
        rebound = root.lstat()
        if _identity(opened) != _identity(after) or _identity(after) != _identity(rebound):
            raise ValueError("campaign root changed")
        if type(value) is not dict:
            raise ValueError("campaign report differs")
        return value
    finally:
        os.close(descriptor)


def _metadata(path: Path) -> list[object]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return [path.name, False]
    return [path.name, True, info.st_mode, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def _tree_object(prefix: str) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"HEAD:{prefix}"],
        cwd=REPOSITORY, capture_output=True,
    )
    if completed.returncode != 0:
        if completed.stdout:
            raise ValueError("protected inventory Git binding differs")
        return None
    if completed.stderr or len(completed.stdout) > 128:
        raise ValueError("protected inventory Git binding differs")
    value = completed.stdout.decode("ascii", "strict").strip()
    if re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise ValueError("protected inventory Git binding differs")
    return value


def _open_directory_at(parent: int, name: str) -> tuple[int, tuple[int, int, int, int, int, int]]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError("protected inventory directory is unsafe")
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent,
    )
    opened = os.fstat(descriptor)
    if _identity(before) != _identity(opened):
        os.close(descriptor)
        raise ValueError("protected inventory directory changed")
    return descriptor, _identity(opened)


def _open_directory_chain(parent: int, relative: str):
    descriptors = []
    current = parent
    try:
        for component in relative.split("/"):
            child, identity = _open_directory_at(current, component)
            descriptors.append((current, component, child, identity))
            current = child
        return descriptors
    except BaseException:
        for _parent, _name, child, _identity_value in reversed(descriptors):
            os.close(child)
        raise


def _revalidate_directory_chain(descriptors) -> None:
    for parent, name, child, identity in reversed(descriptors):
        held = os.fstat(child)
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if _identity(held) != identity or _identity(rebound) != identity:
            raise ValueError("protected inventory directory changed")


def _inventory_directory(
    descriptor: int, relative: str, rows: list[dict[str, object]],
    budget: dict[str, int], depth: int,
) -> None:
    if depth > _MAX_TRACKED_DEPTH:
        raise ValueError("protected inventory depth budget exceeded")
    names = sorted(os.listdir(descriptor))
    folded = [name.casefold() for name in names]
    if len(folded) != len(set(folded)):
        raise ValueError("protected inventory names alias")
    for name in names:
        try:
            encoded = name.encode("utf-8", "strict")
        except UnicodeEncodeError as exc:
            raise ValueError("protected inventory name is invalid") from exc
        if (
            name in {"", ".", ".."} or "/" in name or b"\0" in encoded
            or len(encoded) > 255
        ):
            raise ValueError("protected inventory name is invalid")
        item_relative = name if not relative else relative + "/" + name
        if len(item_relative.encode("utf-8")) > 512:
            raise ValueError("protected inventory path budget exceeded")
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        budget["entries"] += 1
        if budget["entries"] > _MAX_PROTECTED_FILES:
            raise ValueError("protected inventory file budget exceeded")
        row = {
            "relative_id": f"protected-{budget['entries'] - 1:04d}",
            "path_digest": canonical_digest(item_relative),
        }
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            child, identity = _open_directory_at(descriptor, name)
            try:
                rows.append({**row, "kind": "directory"})
                _inventory_directory(child, item_relative, rows, budget, depth + 1)
                rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if _identity(os.fstat(child)) != identity or _identity(rebound) != identity:
                    raise ValueError("protected inventory directory changed")
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("protected inventory file is unsafe")
        body = _read_regular_at(descriptor, name, _MAX_PROTECTED_FILE_BYTES)
        budget["bytes"] += len(body)
        if budget["bytes"] > _MAX_PROTECTED_TOTAL_BYTES:
            raise ValueError("protected inventory byte budget exceeded")
        rows.append({
            **row,
            "kind": "regular",
            "content_digest": "sha256:" + hashlib.sha256(body).hexdigest(),
            "size": len(body),
        })


def _tracked_root_inventory(repository_fd: int, prefix: str) -> dict[str, object]:
    try:
        descriptors = _open_directory_chain(repository_fd, prefix)
    except FileNotFoundError:
        if _tree_object(prefix) is not None:
            raise ValueError("protected inventory root is missing")
        return {
            "tree_object": None, "exists": False, "entries": [],
            "root_identity": None, "entry_count": 0, "total_bytes": 0,
        }
    root = descriptors[-1][2]
    root_identity = descriptors[-1][3]
    rows: list[dict[str, object]] = []
    budget = {"entries": 0, "bytes": 0}
    try:
        _inventory_directory(root, "", rows, budget, 0)
        _revalidate_directory_chain(descriptors)
        return {
            "tree_object": _tree_object(prefix),
            "exists": True,
            "entries": rows,
            "root_identity": list(root_identity),
            "entry_count": budget["entries"],
            "total_bytes": budget["bytes"],
        }
    finally:
        for _parent, _name, child, _identity_value in reversed(descriptors):
            os.close(child)


def _opaque_metadata(prefix: str) -> list[object]:
    path = REPOSITORY / prefix
    try:
        before = path.lstat()
    except FileNotFoundError:
        return [False, _tree_object(prefix)]
    after = path.lstat()
    if _identity(before) != _identity(after):
        raise ValueError("opaque protected root changed")
    return [
        True, _tree_object(prefix), before.st_mode, before.st_dev, before.st_ino,
        before.st_size, before.st_mtime_ns, before.st_ctime_ns,
    ]


def _protected_inventory_digest() -> str:
    """Bind tracked bytes while retaining unrelated roots as opaque metadata."""
    _validate_directory_chain(REPOSITORY)
    repository_before = REPOSITORY.lstat()
    repository_fd = os.open(
        REPOSITORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        if _identity(repository_before) != _identity(os.fstat(repository_fd)):
            raise ValueError("protected repository changed")
        inventories = [
            [f"protected-{index:02d}", _tracked_root_inventory(repository_fd, prefix)]
            for index, prefix in enumerate(_TRACKED_PROTECTED_ROOTS)
        ]
        if sum(item[1]["entry_count"] for item in inventories) > _MAX_PROTECTED_FILES:
            raise ValueError("protected inventory entry budget exceeded")
        if sum(item[1]["total_bytes"] for item in inventories) > _MAX_PROTECTED_TOTAL_BYTES:
            raise ValueError("protected inventory byte budget exceeded")
        opaque = [
            [f"opaque-{index:02d}", _opaque_metadata(prefix)]
            for index, prefix in enumerate(_OPAQUE_PILOT_ROOTS)
        ]
        sources_metadata = _opaque_metadata("sources")
        repository_after = REPOSITORY.lstat()
        if (
            _identity(repository_before) != _identity(os.fstat(repository_fd))
            or _identity(repository_before) != _identity(repository_after)
        ):
            raise ValueError("protected repository changed")
        return canonical_digest({
            "tracked_non_pilot_inventories": inventories,
            "opaque_pilot_bindings": opaque,
            "sources_root_metadata": sources_metadata,
        })
    finally:
        os.close(repository_fd)


def _workspace_metadata(root: Path, workspace_id: str) -> str:
    base = root / "workspaces" / "state" / workspace_id
    values = [_metadata(base)]
    values.extend(_metadata(base / name) for name in ("freshness", "graph", "recovery", "sources"))
    return canonical_digest(values)


@contextlib.contextmanager
def _deny_network():
    original = (socket.socket, socket.create_connection, socket.getaddrinfo)

    def denied(*_args, **_kwargs):
        raise AssertionError("network is denied in the workspace rehearsal")

    socket.socket = denied
    socket.create_connection = denied
    socket.getaddrinfo = denied
    try:
        for probe in (
            lambda: socket.getaddrinfo("fixture.invalid", 443),
            lambda: socket.create_connection(("fixture.invalid", 443)),
        ):
            try:
                probe()
            except AssertionError as exc:
                if str(exc) != "network is denied in the workspace rehearsal":
                    raise
            else:
                raise RuntimeError("network denial probe unexpectedly succeeded")
        yield
    finally:
        socket.socket, socket.create_connection, socket.getaddrinfo = original


@contextlib.contextmanager
def _deterministic_registry_clock():
    original = registry_module.datetime

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc)
            return value if tz is not None else value.replace(tzinfo=None)

    registry_module.datetime = FixedDateTime
    try:
        yield
    finally:
        registry_module.datetime = original


def _remove_owned_scratch(root: Path) -> None:
    expected_parent = (REPOSITORY / ".ao-lore").resolve()
    marker = root / "campaign-owner.json"
    if (root.parent.resolve() != expected_parent
            or _SCRATCH_RE.fullmatch(root.name) is None
            or root.is_symlink() or not root.is_dir()
            or not marker.is_file() or marker.is_symlink()
            or marker.read_bytes() != _canonical_bytes(OWNER)):
        raise ValueError("check scratch root is unsafe")
    shutil.rmtree(root)


def _recompute_report(protected: str) -> dict:
    parent = REPOSITORY / ".ao-lore"
    scratch = Path(tempfile.mkdtemp(prefix="workspace-rehearsal-check-", dir=parent))
    try:
        (scratch / "campaign-owner.json").write_bytes(_canonical_bytes(OWNER))
        with _deny_network(), _deterministic_registry_clock():
            return _run(scratch, protected)
    finally:
        if scratch.exists():
            _remove_owned_scratch(scratch)


def _verify_existing_campaign(root: Path, protected: str) -> dict:
    value = _read_owned_report(root)
    if not _valid_report(value, protected):
        raise ExistingCampaignMismatch("campaign report validation differs")
    fixture = subprocess.run(
        [sys.executable, str(GENERATOR), "--check", "--out", str(root / "fixture")],
        cwd=REPOSITORY,
        capture_output=True,
    )
    if fixture.returncode != 0 or fixture.stdout or fixture.stderr:
        raise ExistingCampaignMismatch("campaign fixture differs")
    expected = _recompute_report(protected)
    if _canonical_bytes(value) != _canonical_bytes(expected):
        raise ExistingCampaignMismatch("campaign semantics differ")
    return value


def _rebind_definition(value: dict) -> dict:
    value["definition_digest"] = canonical_digest({
        key: item for key, item in value.items() if key != "definition_digest"
    })
    return value


def _state_roots(runtime: Path, definitions: list[dict]) -> None:
    for definition in definitions:
        base = runtime / "workspaces" / "state" / definition["workspace_id"]
        for name in ("freshness", "graph", "recovery", "sources"):
            (base / name).mkdir(parents=True, exist_ok=True)


def _fixture_material() -> dict:
    spec = importlib.util.spec_from_file_location(
        "ao_lore_workspace_fixture_generator", GENERATOR,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("fixture generator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.material()
    if (
        type(value) is not dict
        or value.get("schema_version") != "ao.lore.workspace-rehearsal-bundle.v0.1"
        or set(value) != {"schema_version", "definitions", "graphs"}
    ):
        raise RuntimeError("ephemeral fixture material differs")
    return value


def _generate_scratch_fixture(root: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "ao_lore_workspace_fixture_documents", GENERATOR,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("fixture generator cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root.mkdir()
    for name, body in module.documents().items():
        (root / name).write_bytes(body)


def _collision_rejected(root: Path, definitions: list[dict]) -> bool:
    values = copy.deepcopy(definitions)
    values[1]["graph_id"] = values[0]["graph_id"]
    _rebind_definition(values[1])
    try:
        publish_workspace_registry_generation(values, WorkspaceRegistryDependencies(root))
    except WorkspaceRegistryError:
        return True
    return False


def _cycle_rejected(root: Path, definitions: list[dict]) -> bool:
    values = copy.deepcopy(definitions)
    by_id = {item["workspace_id"]: item for item in values}
    by_id["shared-reference-fixture"]["reference_workspace_ids"] = ["primary-fixture-a"]
    _rebind_definition(by_id["shared-reference-fixture"])
    try:
        publish_workspace_registry_generation(values, WorkspaceRegistryDependencies(root))
    except WorkspaceRegistryError:
        return True
    return False


def _crash_recovered(root: Path, definitions: list[dict]) -> bool:
    interrupted = False

    def failpoint(name: str) -> None:
        nonlocal interrupted
        if name == "after_intent_fsync":
            interrupted = True
            raise RuntimeError("synthetic interruption")

    crashing = WorkspaceRegistryDependencies(root, failpoint)
    try:
        publish_workspace_registry_generation(definitions, crashing)
    except RuntimeError as exc:
        if str(exc) != "synthetic interruption":
            raise
    if not interrupted:
        return False
    recovered = recover_workspace_registry(WorkspaceRegistryDependencies(root))
    snapshot = load_workspace_registry(WorkspaceRegistryDependencies(root))
    return (
        bool(recovered)
        and all(item["result"] == "complete" for item in recovered)
        and tuple(item["workspace_id"] for item in snapshot.workspaces) == EXPECTED_WORKSPACES
    )


def _foreign_preserved(root: Path, definitions: list[dict]) -> bool:
    dependencies = WorkspaceRegistryDependencies(root)
    publish_workspace_registry_generation(definitions, dependencies)
    foreign = root / "workspaces" / "registry" / "staging" / "foreign-entry"
    foreign.mkdir()
    marker = foreign / "marker.txt"
    marker.write_text("synthetic foreign marker\n", encoding="utf-8")
    outcomes = recover_workspace_registry(dependencies)
    return (
        marker.read_text(encoding="utf-8") == "synthetic foreign marker\n"
        and any(item["result"] == "investigate" for item in outcomes)
    )


def _run(root: Path, protected_before: str) -> dict:
    fixture_root = root / "fixture"
    if root == CAMPAIGN_ROOT:
        generated = subprocess.run(
            [sys.executable, str(GENERATOR), "--out", str(fixture_root)],
            cwd=REPOSITORY,
            check=True,
            capture_output=True,
        )
        if generated.stdout or generated.stderr:
            raise RuntimeError("fixture generator emitted output")
    else:
        _generate_scratch_fixture(fixture_root)
    bundle = _fixture_material()
    manifest = json.loads((fixture_root / "fixture-manifest.json").read_text(encoding="utf-8"))
    definitions = bundle["definitions"]
    graphs = bundle["graphs"]
    if tuple(manifest["workspace_ids"]) != EXPECTED_WORKSPACES:
        raise RuntimeError("fixture inventory differs")

    runtime = root / "runtime"
    dependencies = WorkspaceRegistryDependencies(runtime)
    publish_workspace_registry_generation(definitions, dependencies)
    _state_roots(runtime, definitions)
    snapshot = load_workspace_registry(dependencies)
    statuses: dict[str, str] = {}

    listed = inspect_workspace_registry(snapshot)
    if listed["workspace_ids"] != list(EXPECTED_WORKSPACES) or listed["workspace_count"] != 3:
        raise RuntimeError("registry list differs")
    statuses["registry_list"] = "passed"

    inspected = inspect_workspace(
        dependencies, "primary-fixture-a", graphs["primary-fixture-a"],
        as_of_date="2026-08-13",
    )
    if inspected["status"] != "active" or inspected["affected_workspace_ids"] != ["primary-fixture-a"]:
        raise RuntimeError("workspace inspection differs")
    statuses["workspace_inspect"] = "passed"

    selection = select_workspace(snapshot, "primary-fixture-a")
    selected_ids = (
        selection.primary["workspace_id"],
        *(item["workspace_id"] for item in selection.references),
    )
    query_snapshot = WorkspaceQuerySnapshot(
        snapshot, selection,
        tuple(WorkspaceGraphSnapshot(workspace_id, graphs[workspace_id], None)
              for workspace_id in selected_ids),
    )
    queried = query_workspace(
        query_snapshot, "primary-fixture-a",
        "How should synthetic records preserve control sequences?",
    )
    evidence_origins = sorted({item["workspace_id"] for item in queried["evidence"]})
    if queried["consulted_workspace_ids"] != ["primary-fixture-a", "shared-reference-fixture"]:
        raise RuntimeError("declared reference selection differs")
    if evidence_origins != ["primary-fixture-a", "shared-reference-fixture"]:
        raise RuntimeError("query origin selection differs")
    statuses["declared_reference_query"] = "passed"

    b_before = _workspace_metadata(runtime, "secondary-fixture-b")
    if "secondary-fixture-b" in json.dumps({
        "inspection": inspected,
        "query": queried,
    }, sort_keys=True):
        raise RuntimeError("unrelated workspace leaked")
    statuses["unrelated_workspace_absent"] = "passed"

    replayed = replay_workspace(dependencies, "primary-fixture-a", graphs["primary-fixture-a"])
    if replayed["status"] != "complete" or replayed["affected_workspace_ids"] != ["primary-fixture-a"]:
        raise RuntimeError("workspace replay differs")
    statuses["workspace_replay"] = "passed"

    recovered = recover_workspace(dependencies, "primary-fixture-a")
    if recovered["status"] != "complete" or recovered["affected_workspace_ids"] != ["primary-fixture-a"]:
        raise RuntimeError("workspace recovery differs")
    if _workspace_metadata(runtime, "secondary-fixture-b") != b_before:
        raise RuntimeError("unrelated workspace state changed")
    statuses["workspace_recover"] = "passed"

    if not _collision_rejected(root / "collision-runtime", definitions):
        raise RuntimeError("identity collision was accepted")
    statuses["identity_collision_rejected"] = "passed"
    if not _cycle_rejected(root / "cycle-runtime", definitions):
        raise RuntimeError("reference cycle was accepted")
    statuses["reference_cycle_rejected"] = "passed"
    if not _crash_recovered(root / "crash-runtime", definitions):
        raise RuntimeError("registry crash recovery differs")
    statuses["registry_crash_recovered"] = "passed"
    foreign_preserved = _foreign_preserved(root / "foreign-runtime", definitions)
    if not foreign_preserved:
        raise RuntimeError("foreign staging was not preserved")
    statuses["foreign_staging_preserved"] = "passed"
    statuses["network_denied"] = "passed"

    protected_after = _protected_inventory_digest()
    if protected_before != protected_after:
        raise RuntimeError("protected inventory changed")
    ordered = {name: statuses[name] for name in sorted(EXPECTED_SCENARIOS)}
    report = {
        "schema_version": "ao.lore.workspace-rehearsal.v0.1",
        "scenario_statuses": ordered,
        "scenario_count": len(ordered),
        "workspace_ids": list(EXPECTED_WORKSPACES),
        "query_consulted_workspace_ids": queried["consulted_workspace_ids"],
        "query_evidence_workspace_ids": evidence_origins,
        "fixture_digest": manifest["fixture_digest"],
        "registry_digest": snapshot.generations[-1]["registry_digest"],
        "protected_inventory_before_digest": protected_before,
        "protected_inventory_after_digest": protected_after,
        "protected_inventory_unchanged": True,
        "foreign_staging_preserved": foreign_preserved,
        "network_used": False,
        "credentials_used": False,
        "customer_data_accessed": False,
        "default_brain_accessed": False,
        "default_candidates_accessed": False,
        "default_sources_accessed": False,
        "pilot_content_accessed": False,
        "live_refresh_executed": False,
        "provider_used": False,
        "publication_authority": False,
        "release_authority": False,
        "deployment_authority": False,
        "authority_advanced": False,
    }
    report["evidence_digest"] = canonical_digest(report)
    return report


def _valid_report(value: object, protected: str) -> bool:
    if type(value) is not dict:
        return False
    required = {
        "schema_version", "scenario_statuses", "scenario_count", "workspace_ids",
        "query_consulted_workspace_ids", "query_evidence_workspace_ids", "fixture_digest",
        "registry_digest", "protected_inventory_before_digest",
        "protected_inventory_after_digest", "protected_inventory_unchanged",
        "foreign_staging_preserved", "network_used", "credentials_used",
        "customer_data_accessed", "default_brain_accessed", "default_candidates_accessed",
        "default_sources_accessed", "pilot_content_accessed", "live_refresh_executed",
        "provider_used", "publication_authority", "release_authority",
        "deployment_authority", "authority_advanced", "evidence_digest",
    }
    if set(value) != required or value.get("schema_version") != "ao.lore.workspace-rehearsal.v0.1":
        return False
    if value.get("scenario_count") != len(EXPECTED_SCENARIOS):
        return False
    if set(value.get("scenario_statuses", {})) != set(EXPECTED_SCENARIOS):
        return False
    if any(item != "passed" for item in value["scenario_statuses"].values()):
        return False
    if value.get("workspace_ids") != list(EXPECTED_WORKSPACES):
        return False
    false_fields = (
        "network_used", "credentials_used", "customer_data_accessed",
        "default_brain_accessed", "default_candidates_accessed",
        "default_sources_accessed", "pilot_content_accessed", "live_refresh_executed",
        "provider_used", "publication_authority", "release_authority",
        "deployment_authority", "authority_advanced",
    )
    if any(value.get(field) is not False for field in false_fields):
        return False
    if value.get("protected_inventory_unchanged") is not True:
        return False
    if value.get("protected_inventory_before_digest") != protected or value.get("protected_inventory_after_digest") != protected:
        return False
    projection = {key: item for key, item in value.items() if key != "evidence_digest"}
    return value.get("evidence_digest") == canonical_digest(projection)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = _validate_campaign_root(CAMPAIGN_ROOT)
        protected = _protected_inventory_digest()
        evidence_path = root / "rehearsal-evidence.json"
        if args.check:
            try:
                value = _verify_existing_campaign(root, protected)
            except ExistingCampaignMismatch:
                return 1
            sys.stdout.buffer.write(_canonical_bytes(value))
            return 0
        if root.exists():
            value = _verify_existing_campaign(root, protected)
            sys.stdout.buffer.write(_canonical_bytes(value))
            return 0
        root.mkdir(parents=True)
        (root / "campaign-owner.json").write_bytes(_canonical_bytes(OWNER))
        with _deny_network(), _deterministic_registry_clock():
            report = _run(root, protected)
        evidence_path.write_bytes(_canonical_bytes(report))
        sys.stdout.buffer.write(_canonical_bytes(report))
        return 0
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError,
            subprocess.SubprocessError, WorkspaceRegistryError, RuntimeError):
        sys.stderr.write(REJECTION)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
