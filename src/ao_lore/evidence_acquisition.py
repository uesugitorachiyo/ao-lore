"""Contained acquisition of bounded public evidence from per-source hosts."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, Sequence
from urllib.parse import urljoin, urlsplit

from ._strict_io import ContractError, parse_strict_json, require_identifier
from .benchmark import canonical_digest
from .evidence_graph_contracts import (
    AUTHORITY_FIELDS,
    validate_acquisition_record,
    validate_dns_hostname,
    validate_https_locator,
    validate_recovery,
)


class AcquisitionError(ValueError):
    """Stable acquisition policy, containment, or integrity failure."""


MAX_REDIRECT_HOSTS = 8


@dataclass(frozen=True)
class AcquisitionLimits:
    max_specs: int = 12
    max_redirects: int = 8
    max_response_bytes: int = 16 * 1024 * 1024
    max_header_bytes: int = 64 * 1024
    connect_timeout_seconds: int = 5
    per_spec_timeout_seconds: int = 30
    total_timeout_seconds: int = 180
    max_retained_files: int = 64
    max_directory_levels: int = 4
    max_total_retained_bytes: int = 128 * 1024 * 1024


@dataclass(frozen=True)
class AcquisitionSpec:
    source_id: str
    locator: str
    media_types: tuple[str, ...]
    redirect_hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


@dataclass(frozen=True)
class _TerminalFetch:
    final_locator: str
    media_type: str | None
    body: bytes
    redirect_chain: tuple[str, ...]
    http_status: int


class HTTPClient(Protocol):
    def fetch(self, locator: str, **limits: int) -> HTTPResponse: ...


@dataclass(frozen=True)
class AcquisitionDependencies:
    """Trusted internal dependencies; ``http`` grants no public network authority."""

    source_root: Path
    http: HTTPClient
    clock: Callable[[], str]
    monotonic: Callable[[], float]
    failpoint: Callable[[str], None] = lambda _: None
    _source_root_identity: tuple[int, int] | None = None


def _redirect_hosts(spec: AcquisitionSpec) -> frozenset[str]:
    hosts = spec.redirect_hosts
    if type(hosts) is not tuple or len(hosts) > MAX_REDIRECT_HOSTS:
        raise AcquisitionError("redirect host policy is invalid")
    try:
        validated = tuple(validate_dns_hostname(host, "redirect host") for host in hosts)
    except ContractError as exc:
        raise AcquisitionError("redirect host policy is invalid") from exc
    if len(validated) != len(set(validated)):
        raise AcquisitionError("redirect host policy is invalid")
    if any(host == "invalid" or host.endswith(".invalid") for host in validated):
        raise AcquisitionError("reserved synthetic locator is not fetchable")
    return frozenset(validated)


def _policy_hosts(spec: AcquisitionSpec) -> frozenset[str]:
    redirect_hosts = _redirect_hosts(spec)
    try:
        validate_https_locator(spec.locator, "official locator")
        initial_host = urlsplit(spec.locator).hostname
    except (ContractError, ValueError) as exc:
        raise AcquisitionError("official locator is invalid") from exc
    assert initial_host is not None
    if initial_host == "invalid" or initial_host.endswith(".invalid"):
        raise AcquisitionError("reserved synthetic locator is not fetchable")
    if initial_host in redirect_hosts:
        raise AcquisitionError("redirect host policy is invalid")
    return frozenset((initial_host, *redirect_hosts))


def _official(locator: str, allowed_hosts: frozenset[str]) -> str:
    try:
        validate_https_locator(locator, "official locator")
    except ContractError as exc:
        raise AcquisitionError("official locator is invalid") from exc
    hostname = urlsplit(locator).hostname
    assert hostname is not None
    if hostname == "invalid" or hostname.endswith(".invalid"):
        raise AcquisitionError("reserved synthetic locator is not fetchable")
    if hostname not in allowed_hosts:
        raise AcquisitionError("official locator host is not allowed")
    return locator


def _real_dir(path: Path, label: str) -> os.stat_result:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise AcquisitionError(f"{label} is unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AcquisitionError(f"{label} must be a real directory")
    return info


def _mkdir(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except OSError as exc:
        raise AcquisitionError("owned directory is invalid") from exc
    info = os.fstat(descriptor)
    if not stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        raise AcquisitionError("owned directory is invalid")
    return descriptor


@contextmanager
def _locked_root(root: Path, expected_identity: tuple[int, int] | None = None):
    assert_bound = getattr(root, "_assert_bound", lambda: None)
    assert_bound()
    _real_dir(root, "source root")
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        before = os.fstat(root_fd)
        if expected_identity is not None and (before.st_dev, before.st_ino) != expected_identity:
            raise AcquisitionError("source root changed")
        lock_fd = os.open("acquisition.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
        lock = os.fstat(lock_fd)
        if not stat.S_ISREG(lock.st_mode) or lock.st_nlink != 1 or lock.st_size != 0:
            raise AcquisitionError("acquisition lock is invalid")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        current = os.stat(root, follow_symlinks=False)
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise AcquisitionError("source root changed")
        yield root_fd
        assert_bound()
    except AcquisitionError:
        raise
    except OSError as exc:
        raise AcquisitionError("source root is invalid") from exc
    finally:
        if "lock_fd" in locals():
            fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)
        if "root_fd" in locals():
            os.close(root_fd)


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()


def _listdir(fd: int) -> list[str]:
    try:
        return sorted(os.listdir(fd))
    except OSError as exc:
        raise AcquisitionError("owned directory is invalid") from exc


def _write_exclusive(parent_fd: int, name: str, body: bytes) -> None:
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise AcquisitionError("owned record already exists") from exc
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.fsync(parent_fd)


def _write_staging_file(parent_fd: int, name: str, body: bytes) -> None:
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise AcquisitionError("staging artifact already exists") from exc
    try:
        view = memoryview(body)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        opened = os.fstat(fd)
        path_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != len(body)
            or (opened.st_dev, opened.st_ino) != (path_info.st_dev, path_info.st_ino)
            or path_info.st_nlink != 1
            or path_info.st_size != len(body)
        ):
            raise AcquisitionError("staged artifact is invalid")
    finally:
        os.close(fd)
    os.fsync(parent_fd)


def _read_regular(parent_fd: int, name: str, maximum: int) -> bytes:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
            raise AcquisitionError("owned file is invalid")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise AcquisitionError("owned file changed")
        body = b""
        while len(body) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(body)))
            if not chunk: break
            body += chunk
        after = os.fstat(fd); os.close(fd)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise AcquisitionError("owned file changed")
    except AcquisitionError:
        raise
    except OSError as exc:
        raise AcquisitionError("owned file is invalid") from exc
    if len(body) > maximum:
        raise AcquisitionError("owned file exceeds byte budget")
    return body


def _read_json(parent_fd: int, name: str, maximum: int, label: str) -> dict[str, object]:
    body = _read_regular(parent_fd, name, maximum)
    try:
        value = parse_strict_json(body, label)
    except ContractError as exc:
        raise AcquisitionError(f"{label} is invalid") from exc
    return value


def _read_destination(parent_fd: int, name: str, maximum: int) -> bytes:
    try:
        return _read_regular(parent_fd, name, maximum)
    except AcquisitionError as exc:
        raise AcquisitionError("artifact destination is invalid") from exc


def _artifact_inventory(artifacts_fd: int, limits: AcquisitionLimits) -> tuple[set[str], int]:
    names = set(_listdir(artifacts_fd))
    total = 0
    for name in names:
        try:
            info = os.stat(name, dir_fd=artifacts_fd, follow_symlinks=False)
        except OSError as exc:
            raise AcquisitionError("artifact destination is invalid") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AcquisitionError("artifact destination is invalid")
        total += info.st_size
        if info.st_size > limits.max_response_bytes:
            raise AcquisitionError("retained artifact byte budget exceeded")
    return names, total


def _unlink_if_present(parent_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AcquisitionError("owned state cleanup failed") from exc
    os.fsync(parent_fd)


def _publish_file_no_replace(staging_fd: int, stage_name: str, artifacts_fd: int, digest_name: str) -> bool:
    source = os.stat(stage_name, dir_fd=staging_fd, follow_symlinks=False)
    if not stat.S_ISREG(source.st_mode) or source.st_nlink != 1:
        raise AcquisitionError("staged artifact is invalid")
    libc = ctypes.CDLL(None, use_errno=True); renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AcquisitionError("Linux no-replace publication is unavailable")
    result = renameat2(staging_fd, os.fsencode(stage_name), artifacts_fd, os.fsencode(digest_name), 1)
    if result != 0:
        number = ctypes.get_errno()
        if number == errno.EEXIST:
            return False
        raise AcquisitionError("artifact no-replace publication failed") from OSError(number, os.strerror(number))
    published = os.stat(digest_name, dir_fd=artifacts_fd, follow_symlinks=False)
    if (source.st_dev, source.st_ino) != (published.st_dev, published.st_ino):
        raise AcquisitionError("published artifact identity differs")
    os.fsync(artifacts_fd)
    return True


def _false_authority() -> dict[str, bool]:
    return {field: False for field in AUTHORITY_FIELDS}


def _recovery(attempt: str, status: str, intended: str, staged: str | None,
              destination: str | None, classification: str) -> dict[str, object]:
    value = {"schema_version": "ao.lore.evidence-recovery.v0.1", "attempt_id": attempt,
             "phase": "acquisition", "status": status, "intended_digest": intended,
             "staged_digest": staged, "destination_digest": destination,
             "classification": classification, "recovery_digest": "", **_false_authority()}
    value["recovery_digest"] = canonical_digest({key: child for key, child in value.items() if key != "recovery_digest"})
    return validate_recovery(value)


def _record(spec: AcquisitionSpec, final: str, retrieved: str, media: str, body: bytes,
            redirects: list[str], status: int) -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    value = {"schema_version": "ao.lore.evidence-acquisition-record.v0.1",
             "acquisition_id": _acquisition_id(spec),
             "source_id": spec.source_id, "requested_locator": spec.locator, "final_locator": final,
             "retrieved_at": retrieved, "status": "acquired", "http_status": status,
             "media_type": media, "byte_count": len(body), "content_digest": digest,
             "redirect_chain": redirects, "record_digest": "", **_false_authority()}
    value["record_digest"] = canonical_digest({key: child for key, child in value.items() if key != "record_digest"})
    return validate_acquisition_record(value)


def _acquisition_id(spec: AcquisitionSpec) -> str:
    return "acquisition-" + hashlib.sha256(
        (spec.source_id + spec.locator).encode()
    ).hexdigest()[:32]


def _validate_cached_acquisition(
    spec: AcquisitionSpec,
    stored: dict[str, object],
    artifacts_fd: int,
    limits: AcquisitionLimits,
) -> bytes:
    """Bind an immutable cached record to the active source policy."""

    try:
        allowed_hosts = _policy_hosts(spec)
        if (
            stored["source_id"] != spec.source_id
            or stored["requested_locator"] != spec.locator
            or stored["acquisition_id"] != _acquisition_id(spec)
            or stored["status"] != "acquired"
            or stored["http_status"] != 200
            or stored["final_locator"] is None
            or stored["media_type"] is None
            or stored["content_digest"] is None
            or stored["media_type"] not in spec.media_types
        ):
            raise ValueError("cached record binding differs")
        redirects = stored["redirect_chain"]
        if type(redirects) is not list or len(redirects) > limits.max_redirects:
            raise ValueError("cached redirect chain differs")
        final_locator = stored["final_locator"]
        if (
            (not redirects and final_locator != stored["requested_locator"])
            or (redirects and final_locator != redirects[-1])
        ):
            raise ValueError("cached final locator differs")
        for locator in (stored["requested_locator"], *redirects, final_locator):
            _official(locator, allowed_hosts)
        digest_name = stored["content_digest"].split(":", 1)[1]
        body = _read_destination(
            artifacts_fd, digest_name, limits.max_response_bytes,
        )
        if (
            len(body) != stored["byte_count"]
            or "sha256:" + hashlib.sha256(body).hexdigest()
            != stored["content_digest"]
        ):
            raise ValueError("cached retained artifact differs")
        return body
    except (AcquisitionError, KeyError, TypeError, ValueError) as exc:
        raise AcquisitionError("cached acquisition is invalid") from exc


def _pending_name(attempt: str) -> str:
    return attempt + ".record.json"


def _read_record(parent_fd: int, name: str, maximum: int, label: str) -> dict[str, object]:
    try:
        return validate_acquisition_record(_read_json(parent_fd, name, maximum, label))
    except ContractError as exc:
        raise AcquisitionError(f"{label} is invalid") from exc


def _read_completed_attempts(recovery_fd: int) -> set[str]:
    attempts: set[str] = set()
    for name in _listdir(recovery_fd):
        if not name.endswith(".completed.json"):
            continue
        try:
            value = validate_recovery(_read_json(recovery_fd, name, 256 * 1024, "evidence recovery"))
        except Exception as exc:
            raise AcquisitionError("evidence recovery is invalid") from exc
        attempts.add(value["attempt_id"])
    return attempts


def _exact_artifact_body(
    artifacts_fd: int,
    digest: str,
    limits: AcquisitionLimits,
) -> bytes:
    digest_name = digest.split(":", 1)[1]
    body = _read_destination(artifacts_fd, digest_name, limits.max_response_bytes)
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise AcquisitionError("artifact destination differs")
    return body


def _publish_or_verify_record(
    records_fd: int,
    record: dict[str, object],
) -> None:
    name = record["source_id"] + ".json"
    body = _json_bytes(record)
    try:
        _write_exclusive(records_fd, name, body)
        return
    except AcquisitionError as exc:
        if "already exists" not in str(exc):
            raise
    existing = _read_record(records_fd, name, 256 * 1024, "acquisition record")
    if existing != record:
        raise AcquisitionError("acquisition record conflicts")


def _publish_or_verify_terminal(recovery_fd: int, terminal: dict[str, object]) -> None:
    name = terminal["attempt_id"] + ".completed.json"
    body = _json_bytes(terminal)
    try:
        _write_exclusive(recovery_fd, name, body)
        return
    except AcquisitionError as exc:
        if "already exists" not in str(exc):
            raise
    existing = validate_recovery(_read_json(recovery_fd, name, 256 * 1024, "evidence recovery"))
    if existing != terminal:
        raise AcquisitionError("terminal recovery conflicts")


def _header(response: HTTPResponse, name: str, limits: AcquisitionLimits) -> str | None:
    total = sum(len(key.encode()) + len(value.encode()) + 4 for key, value in response.headers)
    if total > limits.max_header_bytes:
        raise AcquisitionError("response header byte budget exceeded")
    values = [value for key, value in response.headers if key.casefold() == name.casefold()]
    if len(values) > 1:
        raise AcquisitionError("response header is ambiguous")
    return values[0] if values else None


def _fetch_terminal(spec: AcquisitionSpec, deps: AcquisitionDependencies,
                    limits: AcquisitionLimits, *,
                    validate_terminal_response: bool = False) -> _TerminalFetch:
    """Fetch one bounded official response without interpreting terminal status."""

    allowed_hosts = _policy_hosts(spec)
    locator = _official(spec.locator, allowed_hosts); redirects: list[str] = []
    for _ in range(limits.max_redirects + 1):
        response = deps.http.fetch(locator, max_response_bytes=limits.max_response_bytes,
                                   max_header_bytes=limits.max_header_bytes,
                                   connect_timeout_seconds=limits.connect_timeout_seconds,
                                   timeout_seconds=limits.per_spec_timeout_seconds)
        if not isinstance(response.body, bytes) or len(response.body) > limits.max_response_bytes:
            raise AcquisitionError("response byte budget exceeded")
        if response.status in {301, 302, 303, 307, 308}:
            target = _header(response, "Location", limits)
            if target is None:
                raise AcquisitionError("redirect lacks a location")
            if (target != target.strip()
                    or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in target)):
                raise AcquisitionError("official locator is invalid")
            try:
                raw_target = urlsplit(target)
            except ValueError as exc:
                raise AcquisitionError("official locator is invalid") from exc
            if ((raw_target.scheme or raw_target.netloc)
                    and not target.startswith("https://")):
                raise AcquisitionError("official locator is invalid")
            if len(redirects) >= limits.max_redirects:
                raise AcquisitionError("redirect budget exceeded")
            try:
                redirected = urljoin(locator, target)
            except ValueError as exc:
                raise AcquisitionError("official locator is invalid") from exc
            locator = _official(redirected, allowed_hosts); redirects.append(locator); continue
        if (validate_terminal_response
                and (type(response.status) is not int or not 100 <= response.status <= 599)):
            raise AcquisitionError("official response status is invalid")
        if response.status != 200:
            if validate_terminal_response:
                _header(response, "Content-Type", limits)
            return _TerminalFetch(
                locator, None, response.body, tuple(redirects), response.status,
            )
        media_header = _header(response, "Content-Type", limits)
        if media_header is None:
            raise AcquisitionError("response media type is missing")
        media = media_header.split(";", 1)[0].strip().casefold()
        if media not in spec.media_types:
            raise AcquisitionError("response media type differs")
        return _TerminalFetch(
            locator, media, response.body, tuple(redirects), response.status,
        )
    raise AcquisitionError("redirect budget exceeded")


def _fetch(spec: AcquisitionSpec, deps: AcquisitionDependencies, limits: AcquisitionLimits) -> tuple[str, str, bytes, list[str], int]:
    terminal = _fetch_terminal(spec, deps, limits)
    if terminal.http_status != 200 or terminal.media_type is None:
        raise AcquisitionError("required official source is unavailable")
    return (
        terminal.final_locator, terminal.media_type, terminal.body,
        list(terminal.redirect_chain), terminal.http_status,
    )


def acquire_official_evidence(specs: Sequence[AcquisitionSpec], deps: AcquisitionDependencies,
                              *, limits: AcquisitionLimits = AcquisitionLimits()) -> list[dict[str, object]]:
    if not 1 <= len(specs) <= limits.max_specs or len({item.source_id for item in specs}) != len(specs):
        raise AcquisitionError("acquisition specification set is invalid")
    for spec in specs:
        require_identifier(spec.source_id, "source_id")
        if type(spec.media_types) is not tuple or not spec.media_types:
            raise AcquisitionError("acquisition specification set is invalid")
        _official(spec.locator, _policy_hosts(spec))
    started = deps.monotonic(); results = []
    with _locked_root(deps.source_root, deps._source_root_identity) as root_fd:
        records_fd = _mkdir(root_fd, "records"); staging_fd = _mkdir(root_fd, "staging")
        artifacts_root_fd = _mkdir(root_fd, "artifacts"); artifacts_fd = _mkdir(artifacts_root_fd, "sha256")
        recovery_fd = _mkdir(root_fd, "recovery"); pending_fd = _mkdir(root_fd, "pending")
        try:
            existing_names, total = _artifact_inventory(artifacts_fd, limits)
            record_names = set(_listdir(records_fd))
            for spec in specs:
                record_name = spec.source_id + ".json"
                if record_name in record_names:
                    try:
                        stored = _read_record(
                            records_fd, record_name, 256 * 1024,
                            "acquisition record",
                        )
                        _validate_cached_acquisition(
                            spec, stored, artifacts_fd, limits,
                        )
                    except AcquisitionError as exc:
                        if str(exc) == "cached acquisition is invalid":
                            raise
                        raise AcquisitionError(
                            "cached acquisition is invalid"
                        ) from exc
                    results.append(stored); continue
                final, media, body, redirects, http_status = _fetch(spec, deps, limits)
                if deps.monotonic() - started > limits.total_timeout_seconds:
                    raise AcquisitionError("total acquisition time budget exceeded")
                digest_name = hashlib.sha256(body).hexdigest(); digest = "sha256:" + digest_name
                if len(existing_names) + 1 > limits.max_retained_files or total + len(body) > limits.max_total_retained_bytes:
                    raise AcquisitionError("retained artifact budget exceeded")
                record = _record(spec, final, deps.clock(), media, body, redirects, http_status)
                attempt = "attempt-" + hashlib.sha256((spec.source_id + digest).encode()).hexdigest()[:32]
                stage_name = attempt + ".part"; intent_name = attempt + ".json"
                _write_exclusive(pending_fd, _pending_name(attempt), _json_bytes(record))
                intent = _recovery(attempt, "staged", digest, digest, None, "resume")
                _write_exclusive(recovery_fd, intent_name, _json_bytes(intent))
                deps.failpoint("after_intent")
                _write_staging_file(staging_fd, stage_name, body)
                deps.failpoint("after_staging")
                published = _publish_file_no_replace(staging_fd, stage_name, artifacts_fd, digest_name)
                if not published:
                    existing = _read_destination(artifacts_fd, digest_name, limits.max_response_bytes)
                    if existing != body:
                        raise AcquisitionError("artifact destination conflicts")
                    os.unlink(stage_name, dir_fd=staging_fd); os.fsync(staging_fd)
                deps.failpoint("after_artifact_publish")
                _publish_or_verify_record(records_fd, record)
                deps.failpoint("after_record")
                terminal = _recovery(attempt, "completed", digest, digest, digest, "complete")
                _publish_or_verify_terminal(recovery_fd, terminal)
                deps.failpoint("after_terminal")
                results.append(record); existing_names.add(digest_name); total += len(body); record_names.add(record_name)
        finally:
            for descriptor in (pending_fd, recovery_fd, artifacts_fd, artifacts_root_fd, staging_fd, records_fd): os.close(descriptor)
    return results


def recover_official_evidence(deps: AcquisitionDependencies, *, limits: AcquisitionLimits = AcquisitionLimits()) -> list[dict[str, object]]:
    results = []
    with _locked_root(deps.source_root, deps._source_root_identity) as root_fd:
        staging_fd = _mkdir(root_fd, "staging"); artifacts_root_fd = _mkdir(root_fd, "artifacts")
        artifacts_fd = _mkdir(artifacts_root_fd, "sha256"); recovery_fd = _mkdir(root_fd, "recovery")
        records_fd = _mkdir(root_fd, "records"); pending_fd = _mkdir(root_fd, "pending")
        try:
            completed_attempts = _read_completed_attempts(recovery_fd)
            intents = {}
            for name in _listdir(recovery_fd):
                if name.endswith(".completed.json"):
                    continue
                try:
                    value = validate_recovery(_read_json(recovery_fd, name, 256 * 1024, "evidence recovery"))
                    if value["attempt_id"] in completed_attempts:
                        continue
                    intents[value["attempt_id"]] = value
                except Exception:
                    results.append(_recovery("attempt-foreign", "conflict", "sha256:" + "0" * 64, None, None, "investigate"))
            staging_names = {name for name in _listdir(staging_fd) if name.endswith(".part")}
            foreign_staging = [name for name in _listdir(staging_fd) if name not in staging_names or name[:-5] not in intents]
            for _name in foreign_staging:
                results.append(_recovery("attempt-foreign", "conflict", "sha256:" + "0" * 64, None, None, "investigate"))
            for attempt, intent in intents.items():
                pending_name = _pending_name(attempt)
                try:
                    record = _read_record(pending_fd, pending_name, 256 * 1024, "acquisition record")
                    if record["content_digest"] != intent["intended_digest"]:
                        raise AcquisitionError("pending acquisition record differs")
                    stage_name = attempt + ".part"
                    has_stage = stage_name in staging_names
                    has_artifact = intent["intended_digest"].split(":", 1)[1] in set(_listdir(artifacts_fd))
                    if has_stage and has_artifact:
                        raise AcquisitionError("publication state is ambiguous")
                    if has_stage:
                        body = _read_regular(staging_fd, stage_name, limits.max_response_bytes)
                        digest = "sha256:" + hashlib.sha256(body).hexdigest()
                        if digest != intent["intended_digest"]:
                            raise AcquisitionError("staged digest differs")
                        published = _publish_file_no_replace(staging_fd, stage_name, artifacts_fd, digest.split(":", 1)[1])
                        if not published:
                            if _read_destination(artifacts_fd, digest.split(":", 1)[1], limits.max_response_bytes) != body:
                                raise AcquisitionError("destination differs")
                            _unlink_if_present(staging_fd, stage_name)
                    elif has_artifact:
                        body = _exact_artifact_body(artifacts_fd, intent["intended_digest"], limits)
                    else:
                        raise AcquisitionError("owned acquisition intent is incomplete")
                    if "sha256:" + hashlib.sha256(body).hexdigest() != record["content_digest"]:
                        raise AcquisitionError("retained artifact digest drift")
                    _publish_or_verify_record(records_fd, record)
                    terminal = _recovery(attempt, "completed", intent["intended_digest"], intent["staged_digest"], intent["intended_digest"], "complete")
                    _publish_or_verify_terminal(recovery_fd, terminal)
                    results.append(terminal)
                except AcquisitionError:
                    results.append(_recovery(attempt, "conflict", intent["intended_digest"], intent["staged_digest"], None, "investigate"))
        finally:
            for descriptor in (pending_fd, records_fd, recovery_fd, artifacts_fd, artifacts_root_fd, staging_fd): os.close(descriptor)
    return results
