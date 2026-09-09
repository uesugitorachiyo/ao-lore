"""Pure, detached evidence freshness comparison and summary derivation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from ._strict_io import ContractError, parse_strict_json, require_identifier
from .benchmark import canonical_digest
from .evidence_acquisition import (
    AcquisitionDependencies,
    AcquisitionError,
    AcquisitionLimits,
    AcquisitionSpec,
    _artifact_inventory,
    _fetch_terminal,
    _listdir as _list_acquisition_directory,
    _locked_root,
    _mkdir,
    _publish_file_no_replace,
    _read_destination,
    _read_json as _read_acquisition_json,
    _read_regular as _read_acquisition_regular,
    _write_staging_file,
)
from .evidence_graph_contracts import (
    AUTHORITY_FIELDS,
    FRESHNESS_CLASSIFICATIONS,
    FRESHNESS_REASON_CODES,
    SCHEMA_VERSIONS,
    validate_acquisition_record,
    validate_freshness_comparison,
    validate_freshness_observation,
    validate_freshness_policy,
    validate_freshness_summary,
    validate_freshness_summary_against_manifest,
    validate_graph_manifest,
    validate_recovery,
)


class FreshnessStorageError(ValueError):
    """Freshness persistence containment or integrity failure."""


@dataclass(frozen=True)
class FreshnessLimits:
    """Immutable filesystem budgets; overridden only by internal tests."""

    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_public_artifacts_per_kind: int = 128
    max_live_staging_entries: int = 64
    max_live_attempts: int = 16
    max_completed_attempts: int = 128

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in (
            self.max_file_bytes, self.max_total_bytes,
            self.max_public_artifacts_per_kind, self.max_live_staging_entries,
            self.max_live_attempts, self.max_completed_attempts,
        )):
            raise ValueError("freshness limits are invalid")
        if self.max_public_artifacts_per_kind < self.max_completed_attempts:
            raise ValueError("freshness public history limit is inconsistent")


@dataclass(frozen=True)
class FreshnessDependencies:
    """Private dependency injection for a disposable retained-source root."""

    source_root: Path
    failpoint: Callable[[str], None] = lambda _: None
    limits: FreshnessLimits = FreshnessLimits()
    _source_root_identity: tuple[int, int] | None = None
    _workspace_namespace: bool = False


_FRESHNESS_DIRECTORIES = (
    "policies", "observations", "comparisons", "summaries", "recovery", "staging",
)
_FRESHNESS_ROOT_DESCRIPTOR = "__freshness_root_descriptor__"
_FRESHNESS_PARENT_DESCRIPTOR = "__freshness_parent_descriptor__"
_FRESHNESS_WORKSPACE_NAMESPACE = "__freshness_workspace_namespace__"
_BUNDLE_KEYS = {
    "schema_version", "attempt_id", "policy", "graph_id", "graph_digest",
    "observations", "comparisons", "summary", "writes", "bundle_digest",
}


def _authority() -> dict[str, bool]:
    return {field: False for field in AUTHORITY_FIELDS}


def _derived_id(prefix: str, value: Any) -> str:
    return f"{prefix}-{canonical_digest(value).removeprefix('sha256:')[:32]}"


def _bind_digest(value: dict[str, Any], field: str) -> dict[str, Any]:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def _ordered_reasons(reasons: set[str]) -> list[str]:
    return [reason for reason in FRESHNESS_REASON_CODES if reason in reasons]


def _revalidate_directory_binding(parent: int, name: str, descriptor: int,
                                  error_type: type[Exception],
                                  message: str) -> None:
    try:
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        held = os.fstat(descriptor)
    except OSError as exc:
        raise error_type(message) from exc
    if (not stat.S_ISDIR(named.st_mode) or not stat.S_ISDIR(held.st_mode)
            or (named.st_dev, named.st_ino) != (held.st_dev, held.st_ino)):
        raise error_type(message)


def _revalidate_freshness_directory_bindings(directories: dict[str, int]) -> None:
    root = directories[_FRESHNESS_ROOT_DESCRIPTOR]
    freshness = directories[_FRESHNESS_PARENT_DESCRIPTOR]
    if not directories.get(_FRESHNESS_WORKSPACE_NAMESPACE, False):
        _revalidate_directory_binding(
            root, "freshness", freshness, FreshnessStorageError,
            "freshness root changed",
        )
    for name in _FRESHNESS_DIRECTORIES:
        _revalidate_directory_binding(
            freshness, name, directories[name], FreshnessStorageError,
            f"freshness {name} directory changed",
        )


def _parse_utc_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _refresh_scope_digest(policy: dict[str, Any],
                          sources: list[dict[str, Any]]) -> str:
    return canonical_digest({
        "policy_digest": policy["policy_digest"],
        "graph_digest": policy["graph_digest"],
        "sources": sources,
    })


def _refresh_attempt_id(scope_digest: str, invocation_sequence: int) -> str:
    return "refresh-run-" + canonical_digest({
        "scope_digest": scope_digest,
        "invocation_sequence": invocation_sequence,
    })[7:39]


def _validate_observation_age(policy: dict[str, Any], observed_at: str,
                              as_of: str, label: str) -> None:
    observed = _parse_utc_timestamp(observed_at)
    current = _parse_utc_timestamp(as_of)
    if observed > current:
        raise AcquisitionError(f"{label} is in the future")
    if (current - observed).total_seconds() > policy["maximum_observation_age_seconds"]:
        raise AcquisitionError(f"{label} exceeds maximum observation age")


def validate_freshness_summary_currency(summary: Any, policy: Any,
                                        as_of: str) -> dict[str, Any]:
    expected_policy = validate_freshness_policy(policy)
    candidate = dict(summary) if type(summary) is not dict else summary
    validated = validate_freshness_summary(candidate, policy=expected_policy)
    observed = _parse_utc_timestamp(validated["observed_at"])
    current = _parse_utc_timestamp(as_of)
    if observed > current:
        raise ContractError("freshness summary is in the future")
    if (current - observed).total_seconds() > expected_policy["verification_interval_seconds"]:
        raise ContractError("freshness summary is stale")
    return validated


def _spec_successor_metadata(spec: AcquisitionSpec) -> tuple[str | None, str | None, str | None]:
    return (
        getattr(spec, "successor_locator", None),
        getattr(spec, "successor_version", None),
        getattr(spec, "successor_effective_date", None),
    )


def _successor_observation_metadata(spec: AcquisitionSpec,
                                    final_locator: str | None) -> tuple[str | None, str | None]:
    successor_locator, successor_version, successor_effective_date = _spec_successor_metadata(spec)
    if final_locator != successor_locator:
        return None, None
    return successor_version, successor_effective_date


def _baseline_source(policy: dict[str, Any], prior: dict[str, Any],
                     observation: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        item for item in policy["sources"]
        if item["source_id"] == observation["source_id"]
    ]
    if not candidates:
        candidates = [
            item for item in policy["sources"]
            if item["source_id"] == prior["source_id"]
        ]
    if len(candidates) != 1:
        raise ContractError("freshness baseline source is not exact")
    return candidates[0]


def compare_freshness_observation(policy: Any, prior_record: Any,
                                  graph: Any, observation: Any) -> dict[str, Any]:
    """Classify one validated observation without I/O or input mutation."""

    expected_policy = validate_freshness_policy(policy)
    prior = validate_acquisition_record(prior_record)
    expected_graph = validate_graph_manifest(graph)
    observed = validate_freshness_observation(observation)
    if (
        expected_graph["graph_id"], expected_graph["graph_digest"],
        expected_graph["source_registry_digest"],
    ) != (
        expected_policy["graph_id"], expected_policy["graph_digest"],
        expected_policy["source_registry_digest"],
    ):
        raise ContractError("freshness comparison graph binding differs")
    baseline = _baseline_source(expected_policy, prior, observed)
    graph_sources = [
        item for item in expected_graph["sources"]
        if item["source_id"] == baseline["source_id"]
    ]
    if len(graph_sources) != 1:
        raise ContractError("freshness comparison graph source is not exact")
    source = graph_sources[0]

    binding_drift = any(
        observed[field] != expected_policy[field]
        for field in ("policy_id", "bundle_id", "graph_id", "graph_digest")
    ) or any((
        prior["record_digest"] != baseline["prior_record_digest"],
        prior["source_id"] != baseline["source_id"],
        prior["requested_locator"] != baseline["canonical_locator"],
        prior["content_digest"] != baseline["prior_content_digest"],
        prior["media_type"] != baseline["prior_media_type"],
        source["source_id"] != baseline["source_id"],
        source["source_digest"] != baseline["prior_content_digest"],
        source["canonical_locator"] != baseline["canonical_locator"],
        source["media_type"] != baseline["prior_media_type"],
        source["status"] != "current",
        observed["source_id"] != baseline["source_id"],
        observed["prior_record_digest"] != baseline["prior_record_digest"],
        observed["requested_locator"] != baseline["canonical_locator"],
    ))

    reasons: set[str] = set()
    if binding_drift:
        reasons.add("prior_binding_drift")
    if observed["status"] == "investigate":
        reasons.add("unsafe_observation")
    if (observed["byte_count"] > expected_policy["maximum_observation_bytes"]
            or len(observed["redirect_chain"]) > expected_policy["maximum_redirect_hops"]
            or (observed["media_type"] is not None
                and observed["media_type"] not in expected_policy["allowed_media_types"])
            or (observed["status"] == "unavailable"
                and observed["http_status"] not in {404, 410})):
        reasons.add("unsafe_observation")
    if observed["redirect_chain"]:
        reasons.add("redirect_drift")
    if observed["media_type"] is not None and observed["media_type"] != baseline["prior_media_type"]:
        reasons.add("media_type_drift")
    if observed["final_locator"] is not None and observed["final_locator"] != baseline["canonical_locator"]:
        reasons.add("locator_drift")
    successor_observed = (
        baseline["declared_successor_locator"] is not None
        and observed["final_locator"] == baseline["declared_successor_locator"]
    )
    if successor_observed and observed["version"] is None:
        reasons.add("version_ambiguous")
    if successor_observed and observed["effective_date"] is None:
        reasons.add("effective_date_ambiguous")

    successor_verified = (
        successor_observed
        and observed["version"] is not None
        and observed["effective_date"] is not None
    )
    unsafe_reasons = {
        "redirect_drift", "media_type_drift", "locator_drift", "version_ambiguous",
        "effective_date_ambiguous", "unsafe_observation",
    }
    if successor_verified:
        unsafe_reasons -= {"redirect_drift", "locator_drift"}

    if binding_drift or reasons & unsafe_reasons:
        classification = "investigate"
    elif observed["status"] == "unavailable":
        classification = "unavailable"
        reasons.add("terminal_unavailable")
    elif successor_verified:
        classification = "superseded"
        reasons.add("declared_successor_verified")
    elif observed["content_digest"] != baseline["prior_content_digest"]:
        classification = "updated"
        reasons.add("content_changed")
    else:
        classification = "unchanged"
        reasons.add("content_unchanged")

    context = {
        "policy_digest": expected_policy["policy_digest"],
        "prior_record_digest": baseline["prior_record_digest"],
        "observation_digest": observed["observation_digest"],
        "classification": classification,
        "reason_codes": _ordered_reasons(reasons),
    }
    result = {
        "schema_version": SCHEMA_VERSIONS[12],
        "comparison_id": _derived_id("comparison", context),
        "policy_id": expected_policy["policy_id"], "bundle_id": expected_policy["bundle_id"],
        "graph_id": expected_policy["graph_id"], "graph_digest": expected_policy["graph_digest"],
        "source_id": baseline["source_id"], "prior_record_digest": baseline["prior_record_digest"],
        "observation_id": observed["observation_id"],
        "observation_digest": observed["observation_digest"], "classification": classification,
        "reason_codes": context["reason_codes"],
        "prior_content_digest": baseline["prior_content_digest"],
        "observed_content_digest": observed["content_digest"],
        "prior_locator": baseline["canonical_locator"], "observed_locator": observed["final_locator"],
        "prior_media_type": baseline["prior_media_type"], "observed_media_type": observed["media_type"],
        "observed_version": observed["version"],
        "observed_effective_date": observed["effective_date"],
        "declared_successor_locator": baseline["declared_successor_locator"],
        "qualification": None, **_authority(),
    }
    if classification not in expected_policy["allowed_terminal_classifications"]:
        raise ContractError("freshness classification is not allowed by policy")
    _bind_digest(result, "comparison_digest")
    return validate_freshness_comparison(result, policy=expected_policy)


def summarize_freshness(policy: Any, graph: Any, observations: Any,
                        comparisons: Any) -> dict[str, Any]:
    """Derive a deterministic exact-coverage summary from detached artifacts."""

    expected_policy = validate_freshness_policy(policy)
    expected_graph = validate_graph_manifest(graph)
    if type(observations) is not list or type(comparisons) is not list:
        raise ContractError("freshness summary context must be exact arrays")
    if (
        expected_graph["graph_id"], expected_graph["graph_digest"],
        expected_graph["source_registry_digest"],
    ) != (
        expected_policy["graph_id"], expected_policy["graph_digest"],
        expected_policy["source_registry_digest"],
    ):
        raise ContractError("freshness summary graph binding differs")
    policy_sources = {
        item["source_id"]: item["prior_record_digest"] for item in expected_policy["sources"]
    }
    if {item["source_id"] for item in expected_graph["sources"]} != set(policy_sources):
        raise ContractError("freshness summary graph source coverage differs")

    observed_items = [
        validate_freshness_observation(item, policy=expected_policy) for item in observations
    ]
    observed_by_id = {item["observation_id"]: item for item in observed_items}
    comparison_items = []
    for item in comparisons:
        if type(item) is not dict:
            raise ContractError("freshness comparison must be an object")
        comparison_items.append(validate_freshness_comparison(
            item, policy=expected_policy,
            observation=observed_by_id.get(item.get("observation_id")),
        ))
    compared_by_source = {item["source_id"]: item for item in comparison_items}
    if (len(observed_by_id) != len(observed_items)
            or len(compared_by_source) != len(comparison_items)
            or set(compared_by_source) != set(policy_sources)
            or len(observed_items) != len(policy_sources)):
        raise ContractError("freshness summary context identities differ")

    results = []
    for source_id in policy_sources:
        compared = compared_by_source[source_id]
        observed = observed_by_id.get(compared["observation_id"])
        if observed is None or any((
            compared["prior_record_digest"] != policy_sources[source_id],
            compared["observation_digest"] != observed["observation_digest"],
            observed["source_id"] != source_id,
        )):
            raise ContractError("freshness summary result binding differs")
        results.append({field: compared[field] for field in (
            "source_id", "prior_record_digest", "observation_id", "observation_digest",
            "comparison_id", "comparison_digest", "classification", "reason_codes",
        )})

    observed_at = max(item["observed_at"] for item in observed_items)
    context = {
        "policy_digest": expected_policy["policy_digest"],
        "graph_digest": expected_graph["graph_digest"],
        "observed_at": observed_at,
        "results": results,
    }
    result = {
        "schema_version": SCHEMA_VERSIONS[13],
        "summary_id": _derived_id("freshness-summary", context),
        "policy_id": expected_policy["policy_id"], "policy_digest": expected_policy["policy_digest"],
        "bundle_id": expected_policy["bundle_id"], "graph_id": expected_graph["graph_id"],
        "graph_digest": expected_graph["graph_digest"], "observed_at": observed_at,
        "results": results,
        **{
            f"{classification}_count": sum(
                item["classification"] == classification for item in results
            )
            for classification in FRESHNESS_CLASSIFICATIONS
        },
        "rebuild_required": any(
            item["classification"] in {"updated", "superseded"} for item in results
        ),
        **_authority(),
    }
    _bind_digest(result, "summary_digest")
    return validate_freshness_summary_against_manifest(
        result, expected_graph, policy=expected_policy,
        observations=observed_items, comparisons=comparison_items,
    )


def _json_bytes(value: object) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"),
    ) + "\n").encode("utf-8")


def _byte_digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _is_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 71 and value.startswith("sha256:")
            and all(character in "0123456789abcdef" for character in value[7:]))


def _real_directory(path: Path, label: str) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise FreshnessStorageError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise FreshnessStorageError(f"{label} must be a real directory")


def _open_real_path(path: Path) -> int:
    """Open every absolute path component without following a symlink."""

    absolute = Path(os.path.abspath(path))
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in absolute.parts[1:]:
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current,
            )
            os.close(current); current = child
        return current
    except OSError as exc:
        os.close(current)
        raise FreshnessStorageError("source root has an unsafe ancestor") from exc


def _open_directory(parent: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent,
        )
    except OSError as exc:
        raise FreshnessStorageError("freshness directory is invalid") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise FreshnessStorageError("freshness directory is invalid")
    return descriptor


def _names(descriptor: int, maximum: int = 128) -> list[str]:
    try:
        result = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise FreshnessStorageError("freshness directory is invalid") from exc
    if len(result) > maximum:
        raise FreshnessStorageError("freshness file count budget exceeded")
    folded = [name.casefold() for name in result]
    if len(folded) != len(set(folded)):
        raise FreshnessStorageError("freshness names collide")
    return result


@contextmanager
def _freshness_directories(root: int, *, workspace_namespace: bool = False):
    descriptors: list[int] = []
    try:
        freshness = os.dup(root) if workspace_namespace else _open_directory(root, "freshness", create=True)
        descriptors.append(freshness)
        allowed = set(_FRESHNESS_DIRECTORIES)
        if workspace_namespace:
            allowed.add("acquisition.lock")
        existing = _names(freshness, len(allowed))
        if any(name not in allowed for name in existing):
            raise FreshnessStorageError("freshness root has an unknown entry")
        directories: dict[str, int] = {
            _FRESHNESS_ROOT_DESCRIPTOR: root,
            _FRESHNESS_PARENT_DESCRIPTOR: freshness,
            _FRESHNESS_WORKSPACE_NAMESPACE: workspace_namespace,
        }
        for name in _FRESHNESS_DIRECTORIES:
            child = _open_directory(freshness, name, create=True)
            descriptors.append(child); directories[name] = child
        yield directories
        _revalidate_freshness_directory_bindings(directories)
    except FreshnessStorageError:
        raise
    except OSError as exc:
        raise FreshnessStorageError("freshness storage is invalid") from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


@contextmanager
def _locked_freshness(deps: FreshnessDependencies):
    assert_bound = getattr(deps.source_root, "_assert_bound", lambda: None)
    assert_bound()
    _real_directory(deps.source_root, "source root")
    descriptors: list[int] = []
    lock = None
    try:
        root = _open_real_path(deps.source_root)
        descriptors.append(root); root_info = os.fstat(root)
        if (deps._source_root_identity is not None
                and (root_info.st_dev, root_info.st_ino) != deps._source_root_identity):
            raise FreshnessStorageError("source root changed")
        rebound = os.stat(deps.source_root, follow_symlinks=False)
        if (root_info.st_dev, root_info.st_ino) != (rebound.st_dev, rebound.st_ino):
            raise FreshnessStorageError("source root changed")
        lock = os.open(
            "acquisition.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root,
        )
        lock_info = os.fstat(lock)
        if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1
                or lock_info.st_size != 0):
            raise FreshnessStorageError("acquisition lock is invalid")
        fcntl.flock(lock, fcntl.LOCK_EX)
        rebound = os.stat(deps.source_root, follow_symlinks=False)
        if (root_info.st_dev, root_info.st_ino) != (rebound.st_dev, rebound.st_ino):
            raise FreshnessStorageError("source root changed")
        assert_bound()
        with _freshness_directories(root, workspace_namespace=deps._workspace_namespace) as directories:
            yield directories
        rebound = os.stat(deps.source_root, follow_symlinks=False)
        if (root_info.st_dev, root_info.st_ino) != (rebound.st_dev, rebound.st_ino):
            raise FreshnessStorageError("source root changed")
        assert_bound()
    except FreshnessStorageError:
        raise
    except OSError as exc:
        raise FreshnessStorageError("freshness storage is invalid") from exc
    finally:
        if lock is not None:
            try: fcntl.flock(lock, fcntl.LOCK_UN)
            finally: os.close(lock)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_regular(parent: int, name: str, maximum: int = FreshnessLimits().max_file_bytes) -> bytes:
    descriptor = None
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > maximum):
            raise FreshnessStorageError("freshness file is invalid")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        )
        if identity(before) != identity(opened):
            raise FreshnessStorageError("freshness file changed")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk: break
            chunks.append(chunk); remaining -= len(chunk)
        after = os.fstat(descriptor)
        if identity(opened) != identity(after):
            raise FreshnessStorageError("freshness file changed")
        body = b"".join(chunks)
        if len(body) > maximum or len(body) != opened.st_size:
            raise FreshnessStorageError("freshness byte budget exceeded")
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if identity(after) != identity(rebound):
            raise FreshnessStorageError("freshness name changed")
        return body
    except FreshnessStorageError:
        raise
    except OSError as exc:
        raise FreshnessStorageError("freshness file is invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json(parent: int, name: str, label: str,
               limits: FreshnessLimits = FreshnessLimits()) -> dict[str, Any]:
    try:
        value = parse_strict_json(_read_regular(parent, name, limits.max_file_bytes), label)
    except ContractError as exc:
        raise FreshnessStorageError(f"{label} is invalid") from exc
    if type(value) is not dict:
        raise FreshnessStorageError(f"{label} is invalid")
    return value


def _write_exclusive(parent: int, name: str, body: bytes,
                     limits: FreshnessLimits = FreshnessLimits()) -> bool:
    if len(body) > limits.max_file_bytes:
        raise FreshnessStorageError("freshness byte budget exceeded")
    try:
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent,
        )
    except FileExistsError:
        return False
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(body):
            raise FreshnessStorageError("freshness publication is invalid")
    finally:
        os.close(descriptor)
    os.fsync(parent)
    return True


def _put_exact(parent: int, name: str, body: bytes,
               limits: FreshnessLimits = FreshnessLimits()) -> None:
    if _write_exclusive(parent, name, body, limits):
        return
    if _read_regular(parent, name, limits.max_file_bytes) != body:
        raise FreshnessStorageError("freshness destination conflicts")


def _recovery_value(attempt: str, status: str, intended: str, staged: str | None,
                    destination: str | None, classification: str) -> dict[str, Any]:
    value = {
        "schema_version": SCHEMA_VERSIONS[9], "attempt_id": attempt,
        "phase": "freshness", "status": status, "intended_digest": intended,
        "staged_digest": staged, "destination_digest": destination,
        "classification": classification, **_authority(),
    }
    _bind_digest(value, "recovery_digest")
    return validate_recovery(value)


def _validate_bundle(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) != _BUNDLE_KEYS:
        raise FreshnessStorageError("freshness transaction bundle is invalid")
    if value["schema_version"] != "ao.lore.evidence-freshness-transaction.v0.1":
        raise FreshnessStorageError("freshness transaction bundle is invalid")
    require_identifier(value["attempt_id"], "attempt_id")
    expected_policy = validate_freshness_policy(value["policy"])
    if (value["graph_id"], value["graph_digest"]) != (
            expected_policy["graph_id"], expected_policy["graph_digest"]):
        raise FreshnessStorageError("freshness transaction graph binding differs")
    if type(value["observations"]) is not list or type(value["comparisons"]) is not list:
        raise FreshnessStorageError("freshness transaction identities are invalid")
    for collection, id_key, digest_key in (
        (value["observations"], "observation_id", "observation_digest"),
        (value["comparisons"], "comparison_id", "comparison_digest"),
    ):
        if not 1 <= len(collection) <= 16:
            raise FreshnessStorageError("freshness transaction identity count differs")
        if any(type(item) is not dict or set(item) != {id_key, digest_key} for item in collection):
            raise FreshnessStorageError("freshness transaction identities are invalid")
        for item in collection:
            require_identifier(item[id_key], id_key)
            if not _is_digest(item[digest_key]):
                raise FreshnessStorageError("freshness transaction digest is invalid")
    if type(value["summary"]) is not dict or set(value["summary"]) != {"summary_id", "summary_digest"}:
        raise FreshnessStorageError("freshness transaction summary identity is invalid")
    require_identifier(value["summary"]["summary_id"], "summary_id")
    if not _is_digest(value["summary"]["summary_digest"]):
        raise FreshnessStorageError("freshness transaction summary digest is invalid")
    writes = value["writes"]
    if type(writes) is not list or not 4 <= len(writes) <= 34:
        raise FreshnessStorageError("freshness transaction write set is invalid")
    expected_names = set()
    for item in writes:
        if type(item) is not dict or set(item) != {"directory", "name", "byte_digest"}:
            raise FreshnessStorageError("freshness transaction write set is invalid")
        if item["directory"] not in {"policies", "observations", "comparisons", "summaries"}:
            raise FreshnessStorageError("freshness transaction write set is invalid")
        require_identifier(item["name"].removesuffix(".json"), "write name")
        if not item["name"].endswith(".json") or (item["directory"], item["name"]) in expected_names:
            raise FreshnessStorageError("freshness transaction write set is invalid")
        if not _is_digest(item["byte_digest"]):
            raise FreshnessStorageError("freshness transaction byte digest is invalid")
        expected_names.add((item["directory"], item["name"]))
    required_names = {("policies", expected_policy["policy_id"] + ".json")}
    required_names.update(
        ("observations", item["observation_id"] + ".json")
        for item in value["observations"]
    )
    required_names.update(
        ("comparisons", item["comparison_id"] + ".json")
        for item in value["comparisons"]
    )
    required_names.add(("summaries", value["summary"]["summary_id"] + ".json"))
    if expected_names != required_names:
        raise FreshnessStorageError("freshness transaction write coverage differs")
    seed = {
        "schema_version": value["schema_version"],
        "policy_digest": expected_policy["policy_digest"],
        "summary_digest": value["summary"]["summary_digest"], "writes": writes,
    }
    if value["attempt_id"] != "freshness-" + canonical_digest(seed)[7:39]:
        raise FreshnessStorageError("freshness transaction attempt identity differs")
    if value["bundle_digest"] != canonical_digest({k: v for k, v in value.items() if k != "bundle_digest"}):
        raise FreshnessStorageError("freshness transaction digest differs")
    return value


def _transaction_bundle(policy: Any, graph: Any, observations: Any,
                        comparisons: Any, summary: Any) -> tuple[dict[str, Any], dict[tuple[str, str], bytes]]:
    expected_policy = validate_freshness_policy(policy)
    expected_graph = validate_graph_manifest(graph)
    if type(observations) is not list or type(comparisons) is not list:
        raise ContractError("freshness transaction context must be exact arrays")
    observed = [validate_freshness_observation(item, policy=expected_policy) for item in observations]
    observed_by_id = {item["observation_id"]: item for item in observed}
    compared = [validate_freshness_comparison(
        item, policy=expected_policy, observation=observed_by_id.get(item.get("observation_id")),
    ) for item in comparisons]
    expected_summary = validate_freshness_summary(
        dict(summary), policy=expected_policy, observations=observed, comparisons=compared,
    )
    if (expected_graph["graph_id"], expected_graph["graph_digest"]) != (
            expected_policy["graph_id"], expected_policy["graph_digest"]):
        raise ContractError("freshness transaction graph binding differs")
    source_order = [item["source_id"] for item in expected_policy["sources"]]
    observed_by_source = {item["source_id"]: item for item in observed}
    compared_by_source = {item["source_id"]: item for item in compared}
    if (set(observed_by_source) != set(source_order)
            or set(compared_by_source) != set(source_order)):
        raise ContractError("freshness transaction source coverage differs")
    observed = [observed_by_source[source_id] for source_id in source_order]
    compared = [compared_by_source[source_id] for source_id in source_order]
    artifacts = [("policies", expected_policy["policy_id"] + ".json", expected_policy)]
    artifacts += [("observations", item["observation_id"] + ".json", item) for item in observed]
    artifacts += [("comparisons", item["comparison_id"] + ".json", item) for item in compared]
    artifacts += [("summaries", expected_summary["summary_id"] + ".json", expected_summary)]
    bodies = {(directory, name): _json_bytes(value) for directory, name, value in artifacts}
    writes = [{"directory": directory, "name": name, "byte_digest": _byte_digest(bodies[(directory, name)])}
              for directory, name, _ in artifacts]
    seed = {
        "schema_version": "ao.lore.evidence-freshness-transaction.v0.1",
        "policy_digest": expected_policy["policy_digest"],
        "summary_digest": expected_summary["summary_digest"], "writes": writes,
    }
    attempt = "freshness-" + canonical_digest(seed).removeprefix("sha256:")[:32]
    bundle = {
        "schema_version": "ao.lore.evidence-freshness-transaction.v0.1",
        "attempt_id": attempt, "policy": expected_policy,
        "graph_id": expected_graph["graph_id"], "graph_digest": expected_graph["graph_digest"],
        "observations": [{"observation_id": item["observation_id"], "observation_digest": item["observation_digest"]} for item in observed],
        "comparisons": [{"comparison_id": item["comparison_id"], "comparison_digest": item["comparison_digest"]} for item in compared],
        "summary": {"summary_id": expected_summary["summary_id"], "summary_digest": expected_summary["summary_digest"]},
        "writes": writes,
    }
    _bind_digest(bundle, "bundle_digest")
    return _validate_bundle(bundle), bodies


def _stage_name(attempt: str, directory: str, name: str) -> str:
    return f"{attempt}--{directory}--{name}"


def _validate_artifact_bodies(bundle: dict[str, Any],
                              bodies: dict[tuple[str, str], bytes]) -> None:
    policy_key = ("policies", bundle["policy"]["policy_id"] + ".json")
    policy = validate_freshness_policy(parse_strict_json(bodies[policy_key], "freshness policy"))
    if policy != bundle["policy"] or _json_bytes(policy) != bodies[policy_key]:
        raise FreshnessStorageError("freshness policy bytes differ")
    observations = []
    for identity in bundle["observations"]:
        key = ("observations", identity["observation_id"] + ".json")
        observed = validate_freshness_observation(
            parse_strict_json(bodies[key], "freshness observation"), policy=policy,
        )
        if (observed["observation_digest"] != identity["observation_digest"]
                or _json_bytes(observed) != bodies[key]):
            raise FreshnessStorageError("freshness observation bytes differ")
        observations.append(observed)
    observed_by_id = {item["observation_id"]: item for item in observations}
    comparisons = []
    for identity in bundle["comparisons"]:
        key = ("comparisons", identity["comparison_id"] + ".json")
        raw = parse_strict_json(bodies[key], "freshness comparison")
        compared = validate_freshness_comparison(
            raw, policy=policy, observation=observed_by_id.get(raw.get("observation_id")),
        )
        if (compared["comparison_digest"] != identity["comparison_digest"]
                or _json_bytes(compared) != bodies[key]):
            raise FreshnessStorageError("freshness comparison bytes differ")
        comparisons.append(compared)
    summary_key = ("summaries", bundle["summary"]["summary_id"] + ".json")
    summary = validate_freshness_summary(
        parse_strict_json(bodies[summary_key], "freshness summary"), policy=policy,
        observations=observations, comparisons=comparisons,
    )
    if (summary["summary_digest"] != bundle["summary"]["summary_digest"]
            or _json_bytes(summary) != bodies[summary_key]):
        raise FreshnessStorageError("freshness summary bytes differ")


def _recovery_entry_limit(limits: FreshnessLimits) -> int:
    return 52 * limits.max_completed_attempts + 52 * limits.max_live_attempts


def _inventory(directories: dict[str, int], limits: FreshnessLimits) -> None:
    total = 0
    for directory in _FRESHNESS_DIRECTORIES:
        maximum = (limits.max_public_artifacts_per_kind
                   if directory in {"policies", "observations", "comparisons", "summaries"}
                   else limits.max_live_staging_entries if directory == "staging"
                   else _recovery_entry_limit(limits))
        for name in _names(directories[directory], maximum):
            info = os.stat(name, dir_fd=directories[directory], follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limits.max_file_bytes:
                raise FreshnessStorageError("freshness inventory is invalid")
            total += info.st_size
            if total > limits.max_total_bytes:
                raise FreshnessStorageError("freshness total byte budget exceeded")


def _validate_public_inventory(directories: dict[str, int], limits: FreshnessLimits) -> None:
    specifications = {
        "policies": (validate_freshness_policy, "policy_id"),
        "observations": (validate_freshness_observation, "observation_id"),
        "comparisons": (validate_freshness_comparison, "comparison_id"),
        "summaries": (validate_freshness_summary, "summary_id"),
    }
    for directory, (validator, identity) in specifications.items():
        for name in _names(
                directories[directory], limits.max_public_artifacts_per_kind):
            if not name.endswith(".json"):
                raise FreshnessStorageError("freshness artifact name is invalid")
            try:
                value = validator(_read_json(
                    directories[directory], name, f"freshness {directory} artifact", limits,
                ))
            except ContractError as exc:
                raise FreshnessStorageError("freshness artifact is invalid") from exc
            if name != value[identity] + ".json":
                raise FreshnessStorageError("freshness artifact name differs")


def _refresh_run(value: Any) -> dict[str, Any]:
    keys = {
        "schema_version", "attempt_id", "operation", "policy", "graph_id",
        "graph_digest", "started_at", "invocation_sequence",
        "scope_digest", "sources", "run_digest",
    }
    if type(value) is not dict or set(value) != keys:
        raise FreshnessStorageError("freshness refresh run is invalid")
    policy = validate_freshness_policy(value["policy"])
    if (value["schema_version"] != "ao.lore.evidence-freshness-refresh-run.v0.2"
            or value["operation"] != "refresh_official_evidence"
            or value["graph_id"] != policy["graph_id"]
            or value["graph_digest"] != policy["graph_digest"]
            or type(value["sources"]) is not list
            or len(value["sources"]) != len(policy["sources"])):
        raise FreshnessStorageError("freshness refresh run binding differs")
    sources = []
    source_keys = {
        "index", "source_id", "locator", "media_types",
        "prior_record_digest", "prior_content_digest",
        "declared_successor_locator", "declared_successor_version",
        "declared_successor_effective_date",
    }
    for index, (item, expected) in enumerate(zip(value["sources"], policy["sources"])):
        if (type(item) is not dict or set(item) != source_keys
                or type(item["index"]) is not int or item["index"] != index
                or type(item["media_types"]) is not list
                or item["media_types"] != [expected["prior_media_type"]]
                or item["source_id"] != expected["source_id"]
                or item["locator"] != expected["canonical_locator"]
                or item["prior_record_digest"] != expected["prior_record_digest"]
                or item["prior_content_digest"] != expected["prior_content_digest"]
                or item["declared_successor_locator"] != expected["declared_successor_locator"]):
            raise FreshnessStorageError("freshness refresh run source differs")
        if item["declared_successor_locator"] is None:
            if (item["declared_successor_version"] is not None
                    or item["declared_successor_effective_date"] is not None):
                raise FreshnessStorageError("freshness refresh run source differs")
        elif ((item["declared_successor_version"] is None)
                != (item["declared_successor_effective_date"] is None)):
            raise FreshnessStorageError("freshness refresh run source differs")
        sources.append(dict(item))
    result = {
        "schema_version": value["schema_version"], "attempt_id": value["attempt_id"],
        "operation": value["operation"], "policy": policy,
        "graph_id": value["graph_id"],
        "graph_digest": value["graph_digest"],
        "started_at": value["started_at"],
        "invocation_sequence": value["invocation_sequence"],
        "scope_digest": value["scope_digest"],
        "sources": sources,
        "run_digest": value["run_digest"],
    }
    try:
        require_identifier(result["attempt_id"], "attempt_id")
    except ContractError as exc:
        raise FreshnessStorageError("freshness refresh run identity is invalid") from exc
    if type(result["started_at"]) is not str or type(result["invocation_sequence"]) is not int:
        raise FreshnessStorageError("freshness refresh run identity is invalid")
    maximum_sequence = FreshnessLimits().max_completed_attempts + FreshnessLimits().max_live_attempts
    if not 1 <= result["invocation_sequence"] <= maximum_sequence:
        raise FreshnessStorageError("freshness refresh run identity is invalid")
    try:
        _parse_utc_timestamp(result["started_at"])
    except ValueError as exc:
        raise FreshnessStorageError("freshness refresh run identity is invalid") from exc
    if not _is_digest(result["scope_digest"]):
        raise FreshnessStorageError("freshness refresh run identity is invalid")
    expected_digest = canonical_digest({
        key: item for key, item in result.items() if key != "run_digest"
    })
    expected_scope_digest = _refresh_scope_digest(policy, sources)
    expected_attempt = _refresh_attempt_id(
        expected_scope_digest,
        result["invocation_sequence"],
    )
    if (result["attempt_id"] != expected_attempt
            or result["scope_digest"] != expected_scope_digest
            or result["run_digest"] != expected_digest):
        raise FreshnessStorageError("freshness refresh run digest differs")
    return result


def _refresh_prepare(value: Any, run: dict[str, Any], previous_digest: str) -> dict[str, Any]:
    keys = {
        "schema_version", "attempt_id", "index", "source_id",
        "previous_digest", "observation", "comparison", "prepare_digest",
    }
    if type(value) is not dict or set(value) != keys:
        raise FreshnessStorageError("freshness refresh prepare is invalid")
    index = value["index"]
    try:
        observed = validate_freshness_observation(
            value["observation"], policy=run["policy"],
        )
        compared = validate_freshness_comparison(
            value["comparison"], policy=run["policy"], observation=observed,
        )
    except ContractError as exc:
        raise FreshnessStorageError("freshness refresh prepare artifact is invalid") from exc
    if (type(index) is not int or not 0 <= index < len(run["sources"])
            or value["schema_version"] != "ao.lore.evidence-freshness-refresh-prepare.v0.1"
            or value["attempt_id"] != run["attempt_id"]
            or value["source_id"] != run["sources"][index]["source_id"]
            or value["previous_digest"] != previous_digest
            or observed["source_id"] != value["source_id"]
            or compared["source_id"] != value["source_id"]
            or value["prepare_digest"] != canonical_digest({
                key: item for key, item in value.items() if key != "prepare_digest"
            })):
        raise FreshnessStorageError("freshness refresh prepare binding differs")
    result = dict(value); result["observation"] = observed; result["comparison"] = compared
    return result


def _refresh_fetch(value: Any, run: dict[str, Any], previous_digest: str) -> dict[str, Any]:
    keys = {
        "schema_version", "attempt_id", "run_digest", "index", "source_id",
        "previous_digest", "requested_locator", "final_locator", "observed_at",
        "status", "http_status", "media_type", "byte_count", "content_digest",
        "redirect_chain", "version", "effective_date", "fetch_digest",
    }
    if type(value) is not dict or set(value) != keys:
        raise FreshnessStorageError("freshness refresh fetch receipt is invalid")
    index = value["index"]
    if (type(index) is not int or not 0 <= index < len(run["sources"])
            or value["schema_version"] != "ao.lore.evidence-freshness-refresh-fetch.v0.1"
            or value["attempt_id"] != run["attempt_id"]
            or value["run_digest"] != run["run_digest"]
            or value["source_id"] != run["sources"][index]["source_id"]
            or value["previous_digest"] != previous_digest):
        raise FreshnessStorageError("freshness refresh fetch receipt binding differs")
    context = run["policy"]["sources"][index]
    observation = {
        "schema_version": SCHEMA_VERSIONS[11],
        "observation_id": _derived_id("observation", {
            "policy_digest": run["policy"]["policy_digest"],
            "source_id": value["source_id"], "observed_at": value["observed_at"],
            "http_status": value["http_status"],
            "final_locator": value["final_locator"],
            "content_digest": value["content_digest"],
            "redirect_chain": value["redirect_chain"],
        }),
        "policy_id": run["policy"]["policy_id"],
        "bundle_id": run["policy"]["bundle_id"],
        "graph_id": run["policy"]["graph_id"],
        "graph_digest": run["policy"]["graph_digest"],
        "source_id": value["source_id"],
        "prior_record_digest": context["prior_record_digest"],
        "requested_locator": value["requested_locator"],
        "final_locator": value["final_locator"], "observed_at": value["observed_at"],
        "status": value["status"], "http_status": value["http_status"],
        "media_type": value["media_type"], "byte_count": value["byte_count"],
        "content_digest": value["content_digest"],
        "redirect_chain": value["redirect_chain"], "version": value["version"],
        "effective_date": value["effective_date"], **_authority(),
    }
    _bind_digest(observation, "observation_digest")
    try:
        validate_freshness_observation(observation, policy=run["policy"])
    except ContractError as exc:
        raise FreshnessStorageError("freshness refresh fetch observation is invalid") from exc
    successor_locator = run["sources"][index]["declared_successor_locator"]
    successor_version = run["sources"][index]["declared_successor_version"]
    successor_effective_date = run["sources"][index]["declared_successor_effective_date"]
    if (value["requested_locator"] != run["sources"][index]["locator"]
            or (value["final_locator"] == successor_locator and (
                value["version"] != successor_version
                or value["effective_date"] != successor_effective_date
            ))
            or (value["final_locator"] != successor_locator and (
                value["version"] is not None or value["effective_date"] is not None
            ))
            or value["fetch_digest"] != canonical_digest({
                key: item for key, item in value.items() if key != "fetch_digest"
            })):
        raise FreshnessStorageError("freshness refresh fetch receipt differs")
    return dict(value)


def _refresh_step(value: Any, run: dict[str, Any], previous_digest: str,
                  prepare: dict[str, Any],
                  directories: dict[str, int], limits: FreshnessLimits) -> dict[str, Any]:
    keys = {
        "schema_version", "attempt_id", "index", "source_id",
        "previous_digest", "prepare_digest", "observation_id", "observation_digest",
        "comparison_id", "comparison_digest", "step_digest",
    }
    if type(value) is not dict or set(value) != keys:
        raise FreshnessStorageError("freshness refresh step is invalid")
    index = value["index"]
    if (type(index) is not int or not 0 <= index < len(run["sources"])
            or value["schema_version"] != "ao.lore.evidence-freshness-refresh-step.v0.1"
            or value["attempt_id"] != run["attempt_id"]
            or value["source_id"] != run["sources"][index]["source_id"]
            or value["previous_digest"] != previous_digest
            or value["prepare_digest"] != prepare["prepare_digest"]):
        raise FreshnessStorageError("freshness refresh step binding differs")
    expected_digest = canonical_digest({
        key: item for key, item in value.items() if key != "step_digest"
    })
    if value["step_digest"] != expected_digest:
        raise FreshnessStorageError("freshness refresh step digest differs")
    try:
        observed = validate_freshness_observation(_read_json(
            directories["observations"], value["observation_id"] + ".json",
            "freshness refresh observation", limits,
        ), policy=run["policy"])
        compared = validate_freshness_comparison(_read_json(
            directories["comparisons"], value["comparison_id"] + ".json",
            "freshness refresh comparison", limits,
        ), policy=run["policy"], observation=observed)
    except ContractError as exc:
        raise FreshnessStorageError("freshness refresh step artifact is invalid") from exc
    if any((
        observed["source_id"] != value["source_id"],
        observed["observation_digest"] != value["observation_digest"],
        compared["source_id"] != value["source_id"],
        compared["comparison_digest"] != value["comparison_digest"],
        compared["observation_id"] != observed["observation_id"],
        observed != prepare["observation"],
        compared != prepare["comparison"],
    )):
        raise FreshnessStorageError("freshness refresh step artifact differs")
    return dict(value)


def _refresh_done(value: Any, run: dict[str, Any], last_digest: str,
                  steps: list[dict[str, Any]], directories: dict[str, int],
                  limits: FreshnessLimits) -> dict[str, Any]:
    keys = {
        "schema_version", "attempt_id", "last_step_digest",
        "summary_id", "summary_digest", "done_digest",
    }
    if type(value) is not dict or set(value) != keys:
        raise FreshnessStorageError("freshness refresh completion is invalid")
    if (value["schema_version"] != "ao.lore.evidence-freshness-refresh-done.v0.1"
            or value["attempt_id"] != run["attempt_id"]
            or value["last_step_digest"] != last_digest
            or len(steps) != len(run["sources"])
            or value["done_digest"] != canonical_digest({
                key: item for key, item in value.items() if key != "done_digest"
            })):
        raise FreshnessStorageError("freshness refresh completion binding differs")
    try:
        observations = [validate_freshness_observation(_read_json(
            directories["observations"], item["observation_id"] + ".json",
            "freshness refresh observation", limits,
        ), policy=run["policy"]) for item in steps]
        comparisons = [validate_freshness_comparison(_read_json(
            directories["comparisons"], item["comparison_id"] + ".json",
            "freshness refresh comparison", limits,
        ), policy=run["policy"], observation=observed)
            for item, observed in zip(steps, observations)]
        summary = validate_freshness_summary(_read_json(
            directories["summaries"], value["summary_id"] + ".json",
            "freshness refresh summary", limits,
        ), policy=run["policy"], observations=observations, comparisons=comparisons)
    except ContractError as exc:
        raise FreshnessStorageError("freshness refresh completion artifact is invalid") from exc
    if summary["summary_digest"] != value["summary_digest"]:
        raise FreshnessStorageError("freshness refresh summary differs")
    return dict(value)


def _refresh_journal_inventory(directories: dict[str, int], limits: FreshnessLimits
                               ) -> tuple[set[str], dict[str, dict[str, Any]]]:
    names = _names(directories["recovery"], _recovery_entry_limit(limits))
    run_names = [name for name in names if name.endswith(".run.json")]
    journal_names: set[str] = set(); runs: dict[str, dict[str, Any]] = {}
    for run_name in run_names:
        run = _refresh_run(_read_json(
            directories["recovery"], run_name, "freshness refresh run", limits,
        ))
        if run_name != run["attempt_id"] + ".run.json" or run["attempt_id"] in runs:
            raise FreshnessStorageError("freshness refresh run name differs")
        journal_names.add(run_name)
        steps = []; prepares = []; fetches = []; previous = run["run_digest"]
        for index in range(len(run["sources"])):
            fetch_name = f"{run['attempt_id']}.step-{index:04d}.fetch.json"
            prepare_name = f"{run['attempt_id']}.step-{index:04d}.prepare.json"
            step_name = f"{run['attempt_id']}.step-{index:04d}.json"
            if fetch_name not in names:
                if prepare_name in names or step_name in names:
                    raise FreshnessStorageError("freshness refresh fetch receipt is missing")
                break
            fetch = _refresh_fetch(_read_json(
                directories["recovery"], fetch_name,
                "freshness refresh fetch receipt", limits,
            ), run, previous)
            fetches.append(fetch); journal_names.add(fetch_name)
            if prepare_name not in names:
                if step_name in names:
                    raise FreshnessStorageError("freshness refresh prepare is missing")
                break
            prepare = _refresh_prepare(_read_json(
                directories["recovery"], prepare_name,
                "freshness refresh prepare", limits,
            ), run, previous)
            prepares.append(prepare); journal_names.add(prepare_name)
            if step_name not in names:
                break
            step = _refresh_step(_read_json(
                directories["recovery"], step_name, "freshness refresh step", limits,
            ), run, previous, prepare, directories, limits)
            steps.append(step); previous = step["step_digest"]; journal_names.add(step_name)
        if len(prepares) > len(steps) + 1:
            raise FreshnessStorageError("freshness refresh prepare sequence differs")
        unexpected_steps = [name for name in names if name.startswith(
            run["attempt_id"] + ".step-") and name not in journal_names]
        if unexpected_steps:
            raise FreshnessStorageError("freshness refresh step sequence differs")
        done_name = run["attempt_id"] + ".done.json"
        done = None
        if done_name in names:
            done = _refresh_done(_read_json(
                directories["recovery"], done_name,
                "freshness refresh completion", limits,
            ), run, previous, steps, directories, limits)
            journal_names.add(done_name)
        runs[run["attempt_id"]] = {
            "run": run, "fetches": fetches, "prepares": prepares,
            "steps": steps, "done": done,
        }
    orphan_journals = [name for name in names if (
        ".step-" in name or name.endswith(".done.json")
    ) and name not in journal_names]
    if orphan_journals:
        raise FreshnessStorageError("freshness refresh journal is orphaned")
    completed = sum(state["done"] is not None for state in runs.values())
    if completed > limits.max_completed_attempts:
        raise FreshnessStorageError("freshness refresh completed history budget exceeded")
    if len(runs) - completed > limits.max_live_attempts:
        raise FreshnessStorageError("freshness refresh live history budget exceeded")
    maximum_sequence = limits.max_completed_attempts + limits.max_live_attempts
    scopes: dict[str, list[dict[str, Any]]] = {}
    for state in runs.values():
        scopes.setdefault(state["run"]["scope_digest"], []).append(state)
    for states in scopes.values():
        sequences = sorted(state["run"]["invocation_sequence"] for state in states)
        if any(sequence > maximum_sequence for sequence in sequences):
            raise FreshnessStorageError("freshness refresh invocation history overflow")
        if sequences != list(range(1, len(sequences) + 1)):
            raise FreshnessStorageError("freshness refresh invocation history differs")
    return journal_names, runs


def _recovery_inventory(directories: dict[str, int], limits: FreshnessLimits
                        ) -> tuple[dict[str, dict[str, Any]], set[str],
                                   dict[str, dict[str, Any]]]:
    """Validate exact owned recovery groups and return bundles/staging names."""

    journal_names, refresh_runs = _refresh_journal_inventory(directories, limits)
    suffixes = (".bundle.json", ".intent.json", ".completed.json")
    groups: dict[str, set[str]] = {}
    for name in _names(directories["recovery"], _recovery_entry_limit(limits)):
        if name in journal_names:
            continue
        matches = [suffix for suffix in suffixes if name.endswith(suffix)]
        if len(matches) != 1:
            raise FreshnessStorageError("freshness recovery filename is invalid")
        suffix = matches[0]; attempt = name[:-len(suffix)]
        try:
            require_identifier(attempt, "attempt_id")
        except ContractError as exc:
            raise FreshnessStorageError("freshness recovery filename is invalid") from exc
        if not attempt.startswith("freshness-"):
            raise FreshnessStorageError("freshness recovery filename is invalid")
        groups.setdefault(attempt, set()).add(suffix)

    completed_count = sum(".completed.json" in members for members in groups.values())
    live_count = len(groups) - completed_count
    if completed_count > limits.max_completed_attempts:
        raise FreshnessStorageError("freshness completed history budget exceeded")
    if live_count > limits.max_live_attempts:
        raise FreshnessStorageError("freshness live attempt budget exceeded")
    bundles: dict[str, dict[str, Any]] = {}; staging: set[str] = set()
    for attempt, members in groups.items():
        terminal = ".completed.json" in members
        allowed_members = ({".bundle.json", ".completed.json", ".intent.json"}
                           if terminal else {".bundle.json", ".intent.json"})
        required_members = ({".bundle.json", ".completed.json"}
                            if terminal else {".bundle.json", ".intent.json"})
        if not required_members.issubset(members) or not members.issubset(allowed_members):
            raise FreshnessStorageError("freshness recovery group is incomplete")
        bundle = _validate_bundle(_read_json(
            directories["recovery"], attempt + ".bundle.json",
            "freshness transaction bundle", limits,
        ))
        if bundle["attempt_id"] != attempt:
            raise FreshnessStorageError("freshness transaction name differs")
        if ".intent.json" in members:
            try:
                intent = validate_recovery(_read_json(
                    directories["recovery"], attempt + ".intent.json",
                    "freshness recovery intent", limits,
                ))
            except ContractError as exc:
                raise FreshnessStorageError("freshness recovery intent is invalid") from exc
            if ((intent["phase"], intent["attempt_id"], intent["status"],
                 intent["intended_digest"], intent["classification"]) !=
                    ("freshness", attempt, "staged", bundle["bundle_digest"], "resume")):
                raise FreshnessStorageError("freshness recovery intent binding differs")
        if terminal:
            try:
                terminal = validate_recovery(_read_json(
                    directories["recovery"], attempt + ".completed.json",
                    "freshness terminal recovery", limits,
                ))
            except ContractError as exc:
                raise FreshnessStorageError("freshness terminal recovery is invalid") from exc
            if ((terminal["phase"], terminal["attempt_id"], terminal["status"],
                 terminal["intended_digest"], terminal["destination_digest"],
                 terminal["classification"]) !=
                    ("freshness", attempt, "completed", bundle["bundle_digest"],
                     bundle["bundle_digest"], "complete")):
                raise FreshnessStorageError("freshness terminal binding differs")
        bundles[attempt] = bundle
        if not terminal or ".intent.json" in members:
            staging.update(
                _stage_name(attempt, item["directory"], item["name"])
                for item in bundle["writes"]
            )
    return bundles, staging, refresh_runs


def _intent_names(attempt: str) -> tuple[str, str, str]:
    return attempt + ".bundle.json", attempt + ".intent.json", attempt + ".completed.json"


def _preflight_capacity(bundle: dict[str, Any], directories: dict[str, int],
                        limits: FreshnessLimits) -> None:
    """Reject a new history before creating any transaction-owned file."""

    additions = {name: set() for name in (
        "policies", "observations", "comparisons", "summaries",
    )}
    for write in bundle["writes"]:
        additions[write["directory"]].add(write["name"])
    for directory, intended in additions.items():
        existing = set(_names(
            directories[directory], limits.max_public_artifacts_per_kind,
        ))
        if len(existing | intended) > limits.max_public_artifacts_per_kind:
            raise FreshnessStorageError("freshness public history budget exceeded")

    recovery = set(_names(
        directories["recovery"], _recovery_entry_limit(limits),
    ))
    bundle_name, _, terminal_name = _intent_names(bundle["attempt_id"])
    if bundle_name not in recovery:
        completed = sum(name.endswith(".completed.json") for name in recovery)
        if completed >= limits.max_completed_attempts:
            raise FreshnessStorageError("freshness completed history budget exceeded")
    elif terminal_name in recovery:
        return

    staging = set(_names(
        directories["staging"], limits.max_live_staging_entries,
    ))
    intended_staging = {
        _stage_name(bundle["attempt_id"], item["directory"], item["name"])
        for item in bundle["writes"]
    }
    if len(staging | intended_staging) > limits.max_live_staging_entries:
        raise FreshnessStorageError("freshness live staging budget exceeded")


def _unlink_exact(parent: int, name: str, expected: bytes, limits: FreshnessLimits) -> None:
    """Unlink only a descriptor-verified exact-owned immutable file."""

    descriptor = None
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > limits.max_file_bytes):
            raise FreshnessStorageError("freshness cleanup target is invalid")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        opened = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns,
        )
        if identity(before) != identity(opened):
            raise FreshnessStorageError("freshness cleanup target changed")
        body = b""
        while len(body) <= limits.max_file_bytes:
            chunk = os.read(descriptor, min(
                65536, limits.max_file_bytes + 1 - len(body),
            ))
            if not chunk: break
            body += chunk
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if (identity(opened) != identity(after) or identity(after) != identity(rebound)
                or body != expected):
            raise FreshnessStorageError("freshness cleanup ownership differs")
        os.unlink(name, dir_fd=parent)
        os.fsync(parent)
    except FreshnessStorageError:
        raise
    except OSError as exc:
        raise FreshnessStorageError("freshness cleanup failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _cleanup_completed(bundle: dict[str, Any], bodies: dict[tuple[str, str], bytes],
                       directories: dict[str, int], failpoint: Callable[[str], None],
                       limits: FreshnessLimits) -> None:
    attempt = bundle["attempt_id"]
    staging_names = set(_names(
        directories["staging"], limits.max_live_staging_entries,
    ))
    for write in bundle["writes"]:
        key = (write["directory"], write["name"])
        stage = _stage_name(attempt, *key)
        if stage in staging_names:
            _unlink_exact(directories["staging"], stage, bodies[key], limits)
    failpoint("after_staging_cleanup")
    _revalidate_freshness_directory_bindings(directories)
    intent_name = attempt + ".intent.json"
    recovery_names = set(_names(
        directories["recovery"], _recovery_entry_limit(limits),
    ))
    if intent_name in recovery_names:
        intent = _recovery_value(
            attempt, "staged", bundle["bundle_digest"], None, None, "resume",
        )
        _unlink_exact(directories["recovery"], intent_name, _json_bytes(intent), limits)
    failpoint("after_intent_cleanup")
    _revalidate_freshness_directory_bindings(directories)


def _public_bodies(bundle: dict[str, Any], directories: dict[str, int],
                   limits: FreshnessLimits) -> dict[tuple[str, str], bytes]:
    bodies = {}
    for write in bundle["writes"]:
        key = (write["directory"], write["name"])
        body = _read_regular(
            directories[write["directory"]], write["name"], limits.max_file_bytes,
        )
        if _byte_digest(body) != write["byte_digest"]:
            raise FreshnessStorageError("freshness destination differs")
        bodies[key] = body
    _validate_artifact_bodies(bundle, bodies)
    return bodies


def _execute_bundle(bundle: dict[str, Any], bodies: dict[tuple[str, str], bytes],
                    directories: dict[str, int], failpoint: Callable[[str], None],
                    limits: FreshnessLimits) -> dict[str, Any]:
    attempt = bundle["attempt_id"]; bundle_name, intent_name, terminal_name = _intent_names(attempt)
    try:
        _validate_artifact_bodies(bundle, bodies)
    except (AttributeError, ContractError, KeyError, TypeError) as exc:
        raise FreshnessStorageError("freshness artifact bundle is invalid") from exc
    bundle_body = _json_bytes(bundle); intended = bundle["bundle_digest"]
    existing_recovery = set(os.listdir(directories["recovery"]))
    if terminal_name in existing_recovery:
        terminal = validate_recovery(_read_json(
            directories["recovery"], terminal_name,
            "freshness terminal recovery", limits,
        ))
        if ((terminal["attempt_id"], terminal["intended_digest"],
             terminal["destination_digest"], terminal["classification"]) !=
                (attempt, intended, intended, "complete")):
            raise FreshnessStorageError("freshness terminal binding differs")
        public = _public_bodies(bundle, directories, limits)
        _cleanup_completed(bundle, public, directories, failpoint, limits)
        return terminal
    _put_exact(directories["recovery"], bundle_name, bundle_body, limits)
    intent = _recovery_value(attempt, "staged", intended, None, None, "resume")
    _put_exact(directories["recovery"], intent_name, _json_bytes(intent), limits)
    failpoint("after_intent")
    _revalidate_freshness_directory_bindings(directories)
    for write in bundle["writes"]:
        key = (write["directory"], write["name"]); body = bodies[key]
        if _byte_digest(body) != write["byte_digest"]:
            raise FreshnessStorageError("freshness staged bytes differ")
        _put_exact(directories["staging"], _stage_name(attempt, *key), body, limits)
    staged = _byte_digest(bundle_body)
    failpoint("after_staging")
    _revalidate_freshness_directory_bindings(directories)
    observed_done = compared_done = False
    for write in bundle["writes"]:
        key = (write["directory"], write["name"]); body = bodies[key]
        _put_exact(directories[write["directory"]], write["name"], body, limits)
        if write["directory"] == "observations": observed_done = True
        if write["directory"] == "comparisons": compared_done = True
        if observed_done and write["directory"] != "observations":
            failpoint("after_observation")
            _revalidate_freshness_directory_bindings(directories)
            observed_done = False
        if compared_done and write["directory"] != "comparisons":
            failpoint("after_comparison")
            _revalidate_freshness_directory_bindings(directories)
            compared_done = False
        if write["directory"] == "summaries":
            failpoint("after_summary")
            _revalidate_freshness_directory_bindings(directories)
    for write in bundle["writes"]:
        body = _read_regular(
            directories[write["directory"]], write["name"], limits.max_file_bytes,
        )
        if _byte_digest(body) != write["byte_digest"]:
            raise FreshnessStorageError("freshness destination differs")
    terminal = _recovery_value(attempt, "completed", intended, staged, intended, "complete")
    _put_exact(directories["recovery"], terminal_name, _json_bytes(terminal), limits)
    failpoint("after_terminal")
    _revalidate_freshness_directory_bindings(directories)
    _cleanup_completed(bundle, bodies, directories, failpoint, limits)
    return terminal


def persist_freshness_transaction(policy: Any, graph: Any, observations: Any,
                                  comparisons: Any, summary: Any,
                                  dependencies: FreshnessDependencies) -> dict[str, Any]:
    """Persist one exact detached freshness result beneath its retained-source root."""

    bundle, bodies = _transaction_bundle(policy, graph, observations, comparisons, summary)
    with _locked_freshness(dependencies) as directories:
        return _persist_bundle_locked(bundle, bodies, directories, dependencies)


def _persist_bundle_locked(bundle: dict[str, Any],
                           bodies: dict[tuple[str, str], bytes],
                           directories: dict[str, int],
                           dependencies: FreshnessDependencies) -> dict[str, Any]:
    _inventory(directories, dependencies.limits)
    _validate_public_inventory(directories, dependencies.limits)
    _, owned_staging, _ = _recovery_inventory(directories, dependencies.limits)
    _preflight_capacity(bundle, directories, dependencies.limits)
    allowed_staging = {
        _stage_name(bundle["attempt_id"], item["directory"], item["name"])
        for item in bundle["writes"]
    }
    allowed_staging.update(owned_staging)
    if any(name not in allowed_staging for name in _names(
            directories["staging"],
            dependencies.limits.max_live_staging_entries)):
        raise FreshnessStorageError("foreign freshness staging exists")
    return _execute_bundle(
        bundle, bodies, directories, dependencies.failpoint, dependencies.limits,
    )


def _refresh_preflight(policy: Any, graph: Any,
                       specs: Sequence[AcquisitionSpec]) -> tuple[
                           dict[str, Any], dict[str, Any]]:
    expected_policy = validate_freshness_policy(policy)
    expected_graph = validate_graph_manifest(graph)
    if type(specs) not in {list, tuple}:
        raise ContractError("freshness refresh specifications must be an exact sequence")
    sources = expected_policy["sources"]
    if len(specs) != len(sources):
        raise ContractError("freshness refresh source coverage differs")
    if (
        expected_graph["graph_id"], expected_graph["graph_digest"],
        expected_graph["source_registry_digest"],
    ) != (
        expected_policy["graph_id"], expected_policy["graph_digest"],
        expected_policy["source_registry_digest"],
    ):
        raise ContractError("freshness refresh graph binding differs")
    graph_sources = {item["source_id"]: item for item in expected_graph["sources"]}
    if len(graph_sources) != len(expected_graph["sources"]):
        raise ContractError("freshness refresh graph sources differ")
    for source, spec in zip(sources, specs):
        if not isinstance(spec, AcquisitionSpec):
            raise ContractError("freshness refresh specification is invalid")
        successor_locator, successor_version, successor_effective_date = _spec_successor_metadata(spec)
        graph_source = graph_sources.get(source["source_id"])
        if graph_source is None or any((
            spec.source_id != source["source_id"],
            spec.locator != source["canonical_locator"],
            spec.media_types != (source["prior_media_type"],),
            successor_locator != source["declared_successor_locator"],
            graph_source["source_digest"] != source["prior_content_digest"],
            graph_source["canonical_locator"] != source["canonical_locator"],
            graph_source["media_type"] != source["prior_media_type"],
            graph_source["status"] != "current",
        )):
            raise ContractError("freshness refresh source binding differs")
        if successor_locator is None:
            if successor_version is not None or successor_effective_date is not None:
                raise ContractError("freshness refresh specification is invalid")
        elif ((successor_version is None) != (successor_effective_date is None)):
            raise ContractError("freshness refresh specification is invalid")
    if set(graph_sources) != {item["source_id"] for item in sources}:
        raise ContractError("freshness refresh graph source coverage differs")
    return expected_policy, expected_graph


def _retained_refresh_context(root: int, policy: dict[str, Any],
                              limits: AcquisitionLimits) -> tuple[
                                  list[dict[str, Any]], int, int, int, int, set[str], int]:
    records_fd = _mkdir(root, "records")
    staging_fd = _mkdir(root, "staging")
    artifacts_root_fd = _mkdir(root, "artifacts")
    artifacts_fd = _mkdir(artifacts_root_fd, "sha256")
    try:
        expected_names = {
            source["source_id"] + ".json" for source in policy["sources"]
        }
        if set(_list_acquisition_directory(records_fd)) != expected_names:
            raise AcquisitionError("acquisition record inventory differs")
        records = []
        for source in policy["sources"]:
            record = validate_acquisition_record(_read_acquisition_json(
                records_fd, source["source_id"] + ".json", 256 * 1024,
                "acquisition record",
            ))
            if any((
                record["source_id"] != source["source_id"],
                record["record_digest"] != source["prior_record_digest"],
                record["requested_locator"] != source["canonical_locator"],
                record["content_digest"] != source["prior_content_digest"],
                record["media_type"] != source["prior_media_type"],
            )):
                raise AcquisitionError("retained acquisition record differs")
            records.append(record)
        names, total = _artifact_inventory(artifacts_fd, limits)
        for record in records:
            digest_name = record["content_digest"].removeprefix("sha256:")
            body = _read_destination(
                artifacts_fd, digest_name, limits.max_response_bytes,
            )
            if _byte_digest(body) != record["content_digest"]:
                raise AcquisitionError("retained artifact digest drift")
        return records, records_fd, staging_fd, artifacts_root_fd, artifacts_fd, names, total
    except Exception:
        os.close(artifacts_fd); os.close(artifacts_root_fd)
        os.close(staging_fd); os.close(records_fd)
        raise


def _revalidate_retained_refresh_context(root: int, records_fd: int, staging_fd: int,
                                         artifacts_root_fd: int,
                                         artifacts_fd: int) -> None:
    _revalidate_directory_binding(
        root, "records", records_fd, AcquisitionError,
        "retained records directory changed",
    )
    _revalidate_directory_binding(
        root, "staging", staging_fd, AcquisitionError,
        "retained staging directory changed",
    )
    _revalidate_directory_binding(
        root, "artifacts", artifacts_root_fd, AcquisitionError,
        "retained artifacts directory changed",
    )
    _revalidate_directory_binding(
        artifacts_root_fd, "sha256", artifacts_fd, AcquisitionError,
        "retained artifact digest directory changed",
    )


def _unlink_refresh_stage_exact(staging_fd: int, name: str, expected: bytes,
                                limit: int) -> None:
    descriptor = None
    try:
        before = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > limit):
            raise AcquisitionError("staged refresh artifact is invalid")
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staging_fd)
        opened = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns,
        )
        if identity(before) != identity(opened):
            raise AcquisitionError("staged refresh artifact changed")
        body = b""
        while len(body) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(body)))
            if not chunk: break
            body += chunk
        after = os.fstat(descriptor)
        rebound = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
        if (identity(opened) != identity(after) or identity(after) != identity(rebound)
                or body != expected):
            raise AcquisitionError("staged refresh artifact conflicts")
        os.unlink(name, dir_fd=staging_fd); os.fsync(staging_fd)
    except AcquisitionError:
        raise
    except OSError as exc:
        raise AcquisitionError("staged refresh artifact cleanup failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _retain_refresh_body(staging_fd: int, artifacts_fd: int, body: bytes,
                         names: set[str], total: int,
                         limits: AcquisitionLimits, *, publish: bool) -> int:
    digest_name = hashlib.sha256(body).hexdigest()
    stage_name = "freshness-" + digest_name + ".part"
    staging_names = set(os.listdir(staging_fd))
    if any(name.startswith("freshness-") and name.endswith(".part")
           and name != stage_name for name in staging_names):
        raise AcquisitionError("foreign staged refresh artifact exists")
    if stage_name in staging_names:
        staged = _read_acquisition_regular(
            staging_fd, stage_name, limits.max_response_bytes,
        )
        if staged != body or hashlib.sha256(staged).hexdigest() != digest_name:
            raise AcquisitionError("staged refresh artifact conflicts")
    if digest_name in names:
        if _read_destination(artifacts_fd, digest_name, limits.max_response_bytes) != body:
            raise AcquisitionError("artifact destination conflicts")
        if stage_name in staging_names:
            _unlink_refresh_stage_exact(
                staging_fd, stage_name, body, limits.max_response_bytes,
            )
        return total
    if (len(names) + 1 > limits.max_retained_files
            or total + len(body) > limits.max_total_retained_bytes):
        raise AcquisitionError("retained artifact budget exceeded")
    if stage_name not in staging_names:
        _write_staging_file(staging_fd, stage_name, body)
    if not publish:
        return total
    published = _publish_file_no_replace(
        staging_fd, stage_name, artifacts_fd, digest_name,
    )
    if not published:
        if _read_destination(artifacts_fd, digest_name, limits.max_response_bytes) != body:
            raise AcquisitionError("artifact destination conflicts")
        _unlink_refresh_stage_exact(
            staging_fd, stage_name, body, limits.max_response_bytes,
        )
    names.add(digest_name)
    return total + len(body)


def _new_refresh_fetch(run: dict[str, Any], index: int, previous: str,
                       terminal: Any, observed_at: str,
                       spec: AcquisitionSpec) -> dict[str, Any]:
    if terminal.http_status == 200:
        status = "observed"; media_type = terminal.media_type
        content_digest = _byte_digest(terminal.body)
    elif terminal.http_status in {404, 410}:
        status = "unavailable"; media_type = content_digest = None
    else:
        status = "investigate"; media_type = content_digest = None
    version, effective_date = _successor_observation_metadata(spec, terminal.final_locator)
    value = {
        "schema_version": "ao.lore.evidence-freshness-refresh-fetch.v0.1",
        "attempt_id": run["attempt_id"], "run_digest": run["run_digest"],
        "index": index, "source_id": run["sources"][index]["source_id"],
        "previous_digest": previous,
        "requested_locator": run["sources"][index]["locator"],
        "final_locator": terminal.final_locator, "observed_at": observed_at,
        "status": status, "http_status": terminal.http_status,
        "media_type": media_type, "byte_count": len(terminal.body),
        "content_digest": content_digest,
        "redirect_chain": list(terminal.redirect_chain),
        "version": version, "effective_date": effective_date,
    }
    _bind_digest(value, "fetch_digest")
    return _refresh_fetch(value, run, previous)


def _observation_from_refresh_fetch(run: dict[str, Any], fetch: dict[str, Any]
                                    ) -> dict[str, Any]:
    source = run["policy"]["sources"][fetch["index"]]
    value = {
        "schema_version": SCHEMA_VERSIONS[11],
        "observation_id": _derived_id("observation", {
            "policy_digest": run["policy"]["policy_digest"],
            "source_id": fetch["source_id"], "observed_at": fetch["observed_at"],
            "http_status": fetch["http_status"],
            "final_locator": fetch["final_locator"],
            "content_digest": fetch["content_digest"],
            "redirect_chain": fetch["redirect_chain"],
        }),
        "policy_id": run["policy"]["policy_id"],
        "bundle_id": run["policy"]["bundle_id"],
        "graph_id": run["policy"]["graph_id"],
        "graph_digest": run["policy"]["graph_digest"],
        "source_id": fetch["source_id"],
        "prior_record_digest": source["prior_record_digest"],
        **{field: fetch[field] for field in (
            "requested_locator", "final_locator", "observed_at", "status",
            "http_status", "media_type", "byte_count", "content_digest",
            "redirect_chain", "version", "effective_date",
        )}, **_authority(),
    }
    _bind_digest(value, "observation_digest")
    return validate_freshness_observation(value, policy=run["policy"])


def _refresh_fetch_body(fetch: dict[str, Any], staging_fd: int,
                        artifacts_fd: int, limits: AcquisitionLimits) -> bytes:
    if fetch["status"] != "observed":
        return b""
    digest_name = fetch["content_digest"].removeprefix("sha256:")
    stage_name = "freshness-" + digest_name + ".part"
    if stage_name in set(os.listdir(staging_fd)):
        body = _read_acquisition_regular(
            staging_fd, stage_name, limits.max_response_bytes,
        )
    else:
        body = _read_destination(
            artifacts_fd, digest_name, limits.max_response_bytes,
        )
    if (_byte_digest(body) != fetch["content_digest"]
            or len(body) != fetch["byte_count"]):
        raise AcquisitionError("freshness fetch receipt body differs")
    return body


def _new_refresh_run(policy: dict[str, Any], graph: dict[str, Any],
                     records: list[dict[str, Any]],
                     specs: Sequence[AcquisitionSpec], *,
                     started_at: str,
                     invocation_sequence: int) -> dict[str, Any]:
    sources = _refresh_run_sources(policy, records, specs)
    scope_digest = _refresh_scope_digest(policy, sources)
    attempt = _refresh_attempt_id(
        scope_digest, invocation_sequence,
    )
    value = {
        "schema_version": "ao.lore.evidence-freshness-refresh-run.v0.2",
        "attempt_id": attempt, "operation": "refresh_official_evidence",
        "policy": policy,
        "graph_id": graph["graph_id"], "graph_digest": graph["graph_digest"],
        "started_at": started_at,
        "invocation_sequence": invocation_sequence,
        "scope_digest": scope_digest,
        "sources": sources,
    }
    _bind_digest(value, "run_digest")
    return _refresh_run(value)


def _refresh_run_sources(policy: dict[str, Any], records: list[dict[str, Any]],
                         specs: Sequence[AcquisitionSpec]) -> list[dict[str, Any]]:
    return [{
        "index": index, "source_id": source["source_id"],
        "locator": spec.locator, "media_types": list(spec.media_types),
        "prior_record_digest": record["record_digest"],
        "prior_content_digest": record["content_digest"],
        "declared_successor_locator": _spec_successor_metadata(spec)[0],
        "declared_successor_version": _spec_successor_metadata(spec)[1],
        "declared_successor_effective_date": _spec_successor_metadata(spec)[2],
    } for index, (source, record, spec) in enumerate(zip(
        policy["sources"], records, specs,
    ))]


def _new_refresh_step(run: dict[str, Any], index: int, previous: str,
                      prepare: dict[str, Any],
                      observed: dict[str, Any],
                      compared: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": "ao.lore.evidence-freshness-refresh-step.v0.1",
        "attempt_id": run["attempt_id"], "index": index,
        "source_id": run["sources"][index]["source_id"],
        "previous_digest": previous,
        "prepare_digest": prepare["prepare_digest"],
        "observation_id": observed["observation_id"],
        "observation_digest": observed["observation_digest"],
        "comparison_id": compared["comparison_id"],
        "comparison_digest": compared["comparison_digest"],
    }
    _bind_digest(value, "step_digest")
    return value


def _new_refresh_prepare(run: dict[str, Any], index: int, previous: str,
                         observed: dict[str, Any],
                         compared: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": "ao.lore.evidence-freshness-refresh-prepare.v0.1",
        "attempt_id": run["attempt_id"], "index": index,
        "source_id": run["sources"][index]["source_id"],
        "previous_digest": previous,
        "observation": observed, "comparison": compared,
    }
    _bind_digest(value, "prepare_digest")
    return _refresh_prepare(value, run, previous)


def _new_refresh_done(run: dict[str, Any], last_step: str,
                      summary: dict[str, Any]) -> dict[str, Any]:
    value = {
        "schema_version": "ao.lore.evidence-freshness-refresh-done.v0.1",
        "attempt_id": run["attempt_id"], "last_step_digest": last_step,
        "summary_id": summary["summary_id"],
        "summary_digest": summary["summary_digest"],
    }
    _bind_digest(value, "done_digest")
    return value


def _next_refresh_invocation_sequence(runs: dict[str, dict[str, Any]],
                                      scope_digest: str,
                                      limits: FreshnessLimits) -> int:
    states = [
        state for state in runs.values()
        if state["run"]["scope_digest"] == scope_digest
    ]
    if not states:
        return 1
    sequences = sorted(state["run"]["invocation_sequence"] for state in states)
    maximum_sequence = limits.max_completed_attempts + limits.max_live_attempts
    if any(sequence > maximum_sequence for sequence in sequences):
        raise FreshnessStorageError("freshness refresh invocation history overflow")
    if sequences != list(range(1, len(sequences) + 1)):
        raise FreshnessStorageError("freshness refresh invocation history differs")
    next_sequence = sequences[-1] + 1
    if next_sequence > maximum_sequence:
        raise FreshnessStorageError("freshness refresh invocation history overflow")
    return next_sequence


def _refresh_step_artifacts(state: dict[str, Any], directories: dict[str, int],
                            limits: FreshnessLimits) -> tuple[
                                list[dict[str, Any]], list[dict[str, Any]]]:
    policy = state["run"]["policy"]
    observations = []; comparisons = []
    for step in state["steps"]:
        observed = validate_freshness_observation(_read_json(
            directories["observations"], step["observation_id"] + ".json",
            "freshness refresh observation", limits,
        ), policy=policy)
        compared = validate_freshness_comparison(_read_json(
            directories["comparisons"], step["comparison_id"] + ".json",
            "freshness refresh comparison", limits,
        ), policy=policy, observation=observed)
        observations.append(observed); comparisons.append(compared)
    return observations, comparisons


def _commit_refresh_prepare(run: dict[str, Any], prepare: dict[str, Any],
                            directories: dict[str, int],
                            dependencies: AcquisitionDependencies,
                            limits: FreshnessLimits) -> dict[str, Any]:
    observed = prepare["observation"]; compared = prepare["comparison"]
    _revalidate_freshness_directory_bindings(directories)
    _put_exact(
        directories["observations"], observed["observation_id"] + ".json",
        _json_bytes(observed), limits,
    )
    dependencies.failpoint("after_refresh_observation")
    _revalidate_freshness_directory_bindings(directories)
    _put_exact(
        directories["comparisons"], compared["comparison_id"] + ".json",
        _json_bytes(compared), limits,
    )
    dependencies.failpoint("after_refresh_comparison")
    _revalidate_freshness_directory_bindings(directories)
    step = _new_refresh_step(
        run, prepare["index"], prepare["previous_digest"], prepare,
        observed, compared,
    )
    dependencies.failpoint("before_refresh_commit")
    _revalidate_freshness_directory_bindings(directories)
    _put_exact(
        directories["recovery"],
        f"{run['attempt_id']}.step-{prepare['index']:04d}.json",
        _json_bytes(step), limits,
    )
    return step


@contextmanager
def _refresh_freshness_directories(
    source_descriptor: int,
    dependencies: FreshnessDependencies,
    *, separate_namespace: bool,
):
    """Hold freshness storage after the retained-source lock is acquired."""
    if not separate_namespace:
        with _freshness_directories(source_descriptor) as directories:
            yield directories
        return
    with _locked_freshness(dependencies) as directories:
        yield directories


def refresh_official_evidence(policy: Any, graph: Any,
                              specs: Sequence[AcquisitionSpec],
                              dependencies: AcquisitionDependencies, *,
                              limits: AcquisitionLimits = AcquisitionLimits(),
                              freshness_dependencies: FreshnessDependencies | None = None,
                              ) -> dict[str, Any]:
    """Revalidate one exact retained official bundle and persist detached results."""

    expected_policy, expected_graph = _refresh_preflight(policy, graph, specs)
    bounded = AcquisitionLimits(
        max_specs=limits.max_specs,
        max_redirects=min(limits.max_redirects, expected_policy["maximum_redirect_hops"]),
        max_response_bytes=min(
            limits.max_response_bytes, expected_policy["maximum_observation_bytes"],
        ),
        max_header_bytes=limits.max_header_bytes,
        connect_timeout_seconds=limits.connect_timeout_seconds,
        per_spec_timeout_seconds=limits.per_spec_timeout_seconds,
        total_timeout_seconds=limits.total_timeout_seconds,
        max_retained_files=limits.max_retained_files,
        max_directory_levels=limits.max_directory_levels,
        max_total_retained_bytes=limits.max_total_retained_bytes,
    )
    started = dependencies.monotonic()
    trusted_now = dependencies.clock()
    if freshness_dependencies is not None and type(freshness_dependencies) is not FreshnessDependencies:
        raise FreshnessStorageError("freshness dependencies are invalid")
    separate_freshness = freshness_dependencies is not None
    if separate_freshness:
        try:
            source_info = os.stat(dependencies.source_root, follow_symlinks=False)
            freshness_info = os.stat(
                freshness_dependencies.source_root, follow_symlinks=False,
            )
        except OSError as exc:
            raise FreshnessStorageError(
                "freshness namespace is unavailable",
            ) from exc
        if (source_info.st_dev, source_info.st_ino) == (
            freshness_info.st_dev, freshness_info.st_ino,
        ):
            raise FreshnessStorageError(
                "freshness namespace must differ from retained sources",
            )
    freshness_deps = (
        FreshnessDependencies(dependencies.source_root, dependencies.failpoint)
        if freshness_dependencies is None else freshness_dependencies
    )
    observations: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    with _locked_root(dependencies.source_root) as root:
        context = _retained_refresh_context(root, expected_policy, bounded)
        (prior_records, records_fd, staging_fd, artifacts_root_fd,
         artifacts_fd, names, total) = context
        try:
            with _refresh_freshness_directories(
                root, freshness_deps, separate_namespace=separate_freshness,
            ) as directories:
                _inventory(directories, freshness_deps.limits)
                _validate_public_inventory(directories, freshness_deps.limits)
                _, _, runs = _recovery_inventory(
                    directories, freshness_deps.limits,
                )
                scope_sources = _refresh_run_sources(
                    expected_policy, prior_records, specs,
                )
                scope_digest = _refresh_scope_digest(
                    expected_policy, scope_sources,
                )
                matching_incomplete = [
                    state for state in runs.values()
                    if state["run"]["scope_digest"] == scope_digest
                    and state["done"] is None
                ]
                if len(matching_incomplete) > 1:
                    raise FreshnessStorageError("freshness refresh scope is ambiguous")
                if any(
                        state["done"] is None
                        and state["run"]["scope_digest"] != scope_digest
                        for state in runs.values()):
                    raise FreshnessStorageError("another freshness refresh is incomplete")
                if matching_incomplete:
                    run = matching_incomplete[0]["run"]
                else:
                    invocation_sequence = _next_refresh_invocation_sequence(
                        runs, scope_digest, freshness_deps.limits,
                    )
                    run = _new_refresh_run(
                        expected_policy, expected_graph, prior_records, specs,
                        started_at=trusted_now,
                        invocation_sequence=invocation_sequence,
                    )
                    _put_exact(
                        directories["recovery"], run["attempt_id"] + ".run.json",
                        _json_bytes(run), freshness_deps.limits,
                    )
                    dependencies.failpoint("after_refresh_intent")
                    _revalidate_freshness_directory_bindings(directories)
                    _revalidate_retained_refresh_context(
                        root, records_fd, staging_fd, artifacts_root_fd, artifacts_fd,
                    )
                _, _, runs = _recovery_inventory(
                    directories, freshness_deps.limits,
                )
                state = runs[run["attempt_id"]]
                if state["run"] != run:
                    raise FreshnessStorageError("freshness refresh intent conflicts")
                observations, comparisons = _refresh_step_artifacts(
                    state, directories, freshness_deps.limits,
                )
                for observed in observations:
                    _validate_observation_age(
                        expected_policy, observed["observed_at"],
                        trusted_now, "freshness observation",
                    )
                if state["done"] is not None:
                    _revalidate_retained_refresh_context(
                        root, records_fd, staging_fd, artifacts_root_fd, artifacts_fd,
                    )
                    return validate_freshness_summary(_read_json(
                        directories["summaries"], state["done"]["summary_id"] + ".json",
                        "freshness refresh summary", freshness_deps.limits,
                    ), policy=expected_policy, observations=observations,
                        comparisons=comparisons)
                previous = (state["steps"][-1]["step_digest"]
                            if state["steps"] else run["run_digest"])
                start_index = len(state["steps"])
                source_context = list(zip(
                    expected_policy["sources"], prior_records, specs,
                ))
                if len(state["prepares"]) > start_index:
                    prepare = state["prepares"][start_index]
                    _validate_observation_age(
                        expected_policy, prepare["observation"]["observed_at"],
                        trusted_now, "freshness prepared observation",
                    )
                    step = _commit_refresh_prepare(
                        run, prepare, directories, dependencies,
                        freshness_deps.limits,
                    )
                    observations.append(prepare["observation"])
                    comparisons.append(prepare["comparison"])
                    previous = step["step_digest"]
                    start_index += 1
                for index in range(start_index, len(source_context)):
                    _, record, spec = source_context[index]
                    _revalidate_retained_refresh_context(
                        root, records_fd, staging_fd, artifacts_root_fd, artifacts_fd,
                    )
                    if len(state["fetches"]) > index:
                        fetch = state["fetches"][index]
                        _validate_observation_age(
                            expected_policy, fetch["observed_at"],
                            trusted_now, "freshness fetch receipt",
                        )
                        body = _refresh_fetch_body(
                            fetch, staging_fd, artifacts_fd, bounded,
                        )
                    else:
                        if any(name.startswith("freshness-") and name.endswith(".part")
                               for name in os.listdir(staging_fd)):
                            raise AcquisitionError(
                                "staged refresh artifact lacks a fetch receipt",
                            )
                        terminal = _fetch_terminal(
                            spec, dependencies, bounded,
                            validate_terminal_response=True,
                        )
                        if dependencies.monotonic() - started > bounded.total_timeout_seconds:
                            raise AcquisitionError("total acquisition time budget exceeded")
                        body = terminal.body
                        if terminal.http_status == 200:
                            _retain_refresh_body(
                                staging_fd, artifacts_fd, body, names, total,
                                bounded, publish=False,
                            )
                            dependencies.failpoint(
                                "after_refresh_body_stage_before_receipt",
                            )
                            _revalidate_retained_refresh_context(
                                root, records_fd, staging_fd, artifacts_root_fd, artifacts_fd,
                            )
                        fetch = _new_refresh_fetch(
                            run, index, previous, terminal, trusted_now, spec,
                        )
                        _put_exact(
                            directories["recovery"],
                            f"{run['attempt_id']}.step-{index:04d}.fetch.json",
                            _json_bytes(fetch), freshness_deps.limits,
                        )
                        dependencies.failpoint("after_refresh_fetch_receipt")
                        _revalidate_freshness_directory_bindings(directories)
                        _revalidate_retained_refresh_context(
                            root, records_fd, staging_fd, artifacts_root_fd, artifacts_fd,
                        )
                        if terminal.http_status == 200:
                            dependencies.failpoint("after_refresh_body_stage")
                            _revalidate_retained_refresh_context(
                                root, records_fd, staging_fd, artifacts_root_fd, artifacts_fd,
                            )
                    if fetch["status"] == "observed":
                        _revalidate_retained_refresh_context(
                            root, records_fd, staging_fd, artifacts_root_fd, artifacts_fd,
                        )
                        total = _retain_refresh_body(
                            staging_fd, artifacts_fd, body, names, total,
                            bounded, publish=True,
                        )
                    observed = _observation_from_refresh_fetch(run, fetch)
                    compared = compare_freshness_observation(
                        expected_policy, record, expected_graph, observed,
                    )
                    prepare = _new_refresh_prepare(
                        run, index, previous, observed, compared,
                    )
                    _put_exact(
                        directories["recovery"],
                        f"{run['attempt_id']}.step-{index:04d}.prepare.json",
                        _json_bytes(prepare),
                        freshness_deps.limits,
                    )
                    dependencies.failpoint("after_refresh_prepare")
                    _revalidate_freshness_directory_bindings(directories)
                    step = _commit_refresh_prepare(
                        run, prepare, directories, dependencies,
                        freshness_deps.limits,
                    )
                    observations.append(observed); comparisons.append(compared)
                    previous = step["step_digest"]
                summary = summarize_freshness(
                    expected_policy, expected_graph, observations, comparisons,
                )
                bundle, bodies = _transaction_bundle(
                    expected_policy, expected_graph, observations, comparisons, summary,
                )
                _persist_bundle_locked(bundle, bodies, directories, freshness_deps)
                done = _new_refresh_done(run, previous, summary)
                _put_exact(
                    directories["recovery"], run["attempt_id"] + ".done.json",
                    _json_bytes(done), freshness_deps.limits,
                )
                _revalidate_freshness_directory_bindings(directories)
                _revalidate_retained_refresh_context(
                    root, records_fd, staging_fd, artifacts_root_fd, artifacts_fd,
                )
                return summary
        finally:
            for descriptor in (
                    records_fd, artifacts_fd, artifacts_root_fd, staging_fd):
                os.close(descriptor)


def _investigate(attempt: str = "freshness-foreign") -> dict[str, Any]:
    zero = "sha256:" + "0" * 64
    return _recovery_value(attempt, "conflict", zero, None, None, "investigate")


def recover_freshness_transactions(dependencies: FreshnessDependencies) -> list[dict[str, Any]]:
    """Resume only exact owned freshness intents; preserve uncertain bytes."""

    results: list[dict[str, Any]] = []
    try:
        with _locked_freshness(dependencies) as directories:
            try:
                _inventory(directories, dependencies.limits)
                _validate_public_inventory(directories, dependencies.limits)
                bundles, owned_staging, refresh_runs = _recovery_inventory(
                    directories, dependencies.limits,
                )
                staging_names = set(_names(
                    directories["staging"],
                    dependencies.limits.max_live_staging_entries,
                ))
                if staging_names - owned_staging:
                    return [_investigate()]
                for attempt, bundle in bundles.items():
                    try:
                        _, intent_name, terminal_name = _intent_names(attempt)
                        recovery_names = set(os.listdir(directories["recovery"]))
                        if terminal_name in recovery_names:
                            terminal = validate_recovery(_read_json(
                                directories["recovery"], terminal_name,
                                "freshness terminal recovery", dependencies.limits,
                            ))
                            if terminal["destination_digest"] != bundle["bundle_digest"]:
                                raise FreshnessStorageError("freshness terminal binding differs")
                            bodies = _public_bodies(
                                bundle, directories, dependencies.limits,
                            )
                        else:
                            intent = validate_recovery(_read_json(
                                directories["recovery"], intent_name,
                                "freshness recovery intent", dependencies.limits,
                            ))
                            if (intent["phase"], intent["attempt_id"],
                                    intent["intended_digest"]) != (
                                    "freshness", attempt, bundle["bundle_digest"]):
                                raise FreshnessStorageError(
                                    "freshness recovery binding differs",
                                )
                            bodies = {}
                            for write in bundle["writes"]:
                                stage = _stage_name(
                                    attempt, write["directory"], write["name"],
                                )
                                body = _read_regular(
                                    directories["staging"], stage,
                                    dependencies.limits.max_file_bytes,
                                )
                                if _byte_digest(body) != write["byte_digest"]:
                                    raise FreshnessStorageError(
                                        "freshness staged bytes differ",
                                    )
                                bodies[(write["directory"], write["name"])] = body
                        results.append(_execute_bundle(
                            bundle, bodies, directories, dependencies.failpoint,
                            dependencies.limits,
                        ))
                    except (ContractError, FreshnessStorageError, OSError, KeyError,
                            TypeError, ValueError):
                        results.append(_investigate(attempt))
                for attempt, state in refresh_runs.items():
                    if state["done"] is not None:
                        continue
                    staged = (
                        state["prepares"][-1]["prepare_digest"]
                        if len(state["prepares"]) > len(state["steps"])
                        else state["steps"][-1]["step_digest"]
                        if state["steps"] else None
                    )
                    results.append(_recovery_value(
                        attempt, "staged", state["run"]["run_digest"],
                        staged, None, "resume",
                    ))
            except FreshnessStorageError:
                return [_investigate()]
    except FreshnessStorageError:
        return [_investigate()]
    return results
