"""Fixed-boundary worker for private native-DOCX batch ingestion."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from ._strict_io import parse_strict_json, strict_read_json
from .batch_ingestion import (
    BatchDependencies,
    _docx_signature_matches,
    ingest_verified_batch,
    validate_batch_manifest,
    validate_checkpoint,
)
from .benchmark import canonical_digest
from .home import repository_root, runtime_home
from .ingestion import DocxProvenanceOrigin, ingest_verified_docx
from .private_docx_domain import (
    DOCX_MIME,
    DOCX_PRIVATE_CORPUS_ID,
    DOCX_TRANSFORMATION_ID,
    MAX_DOCX_BYTES,
)
from .private_docx_uat import (
    PRIVATE_DOCX_UAT_POLICY_DIGEST,
    _BWRAP,
    _DENIED_CHILD_SYSCALLS,
    _IMMUTABLE_CONTRACT_PATH,
    _PHASE_ARITHMETIC,
    _RESOURCE_LIMITS,
    _SANDBOX_SCHEMA_VERSION,
    _WORKER_BARRIER_NAME,
    _WORKER_CONTRACT_NAME,
    _WORKER_SCHEMA_VERSION,
    _control_directory,
    _require_socket_free_tree,
    _validate_prepared_run_evidence,
)


_MAX_JOURNAL_BYTES = 64 * 1024
_MAX_CONTRACT_BYTES = 512 * 1024
_PREPARED_EVIDENCE_NAME = "prepared-run-evidence.json"


@dataclass
class _HeldRun:
    contract: dict[str, Any]
    batch: dict[str, Any]
    qualification: dict[str, Any]
    sources: dict[str, bytes]
    source_origins: dict[str, DocxProvenanceOrigin]
    journal_parent_descriptor: int
    journal_descriptor: int
    attempts: list[str]
    descriptors: list[int]

    def close(self) -> None:
        while self.descriptors:
            os.close(self.descriptors.pop())


class _NetworkAttempt(BaseException):
    """Unswallowable violation raised for any network or socket attempt."""


class _ProcessAttempt(BaseException):
    """Unswallowable violation raised for any child-process attempt."""


def _rejecting(attempts: list[str], name: str, error: type[BaseException]):
    def reject(*_args: Any, **_kwargs: Any) -> Any:
        attempts.append(name)
        raise error(f"private DOCX UAT worker {name} attempt")

    return reject


@contextlib.contextmanager
def _execution_guards(attempts: list[str]) -> Iterator[None]:
    """Install and always restore socket/DNS/process guards."""

    socket_names = ("connect", "connect_ex", "sendto", "sendmsg", "bind", "listen")
    socket_originals = {name: getattr(socket.socket, name) for name in socket_names}
    module_socket_originals = {
        "create_connection": socket.create_connection,
        "getaddrinfo": socket.getaddrinfo,
        "gethostbyname": socket.gethostbyname,
        "gethostbyname_ex": socket.gethostbyname_ex,
    }
    process_names = ("Popen", "run", "call", "check_call", "check_output")
    process_originals = {name: getattr(subprocess, name) for name in process_names}
    os_names = tuple(
        name
        for name in (
            "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp",
            "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
            "spawnvp", "spawnvpe",
        )
        if hasattr(os, name)
    )
    os_originals = {name: getattr(os, name) for name in os_names}
    try:
        for name in socket_names:
            setattr(socket.socket, name, _rejecting(attempts, name, _NetworkAttempt))
        for name in module_socket_originals:
            setattr(socket, name, _rejecting(attempts, name, _NetworkAttempt))
        for name in process_names:
            setattr(subprocess, name, _rejecting(attempts, name, _ProcessAttempt))
        for name in os_names:
            setattr(os, name, _rejecting(attempts, name, _ProcessAttempt))
        yield
    finally:
        for name, value in socket_originals.items():
            setattr(socket.socket, name, value)
        for name, value in module_socket_originals.items():
            setattr(socket, name, value)
        for name, value in process_originals.items():
            setattr(subprocess, name, value)
        for name, value in os_originals.items():
            setattr(os, name, value)


def _identity(value: Any) -> tuple[int, int] | None:
    if (
        type(value) is not dict
        or set(value) != {"device", "inode"}
        or any(type(value[name]) is not int or value[name] < 0 for name in value)
    ):
        return None
    return value["device"], value["inode"]


def _read_held_file(
    path: Path,
    maximum: int,
    label: str,
    descriptors: list[int],
    *,
    required_links: int | None = 1,
) -> tuple[bytes, os.stat_result]:
    try:
        before = os.lstat(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        descriptors.append(descriptor)
        opened = os.fstat(descriptor)
        body = os.pread(descriptor, maximum + 1, 0)
        after = os.fstat(descriptor)
        public = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise OSError(f"private DOCX UAT worker {label} is invalid") from exc
    stable = lambda value: (
        value.st_dev, value.st_ino, value.st_mode, value.st_nlink,
        value.st_size, value.st_mtime_ns,
    )
    if (
        not stat.S_ISREG(opened.st_mode)
        or (required_links is not None and opened.st_nlink != required_links)
        or opened.st_size <= 0
        or opened.st_size > maximum
        or len(body) != opened.st_size
        or stable(before) != stable(opened)
        or stable(opened) != stable(after)
        or stable(opened) != stable(public)
    ):
        raise OSError(f"private DOCX UAT worker {label} is invalid")
    return body, opened


def _validate_sandbox_contract(
    sandbox: Any, *, root: Path, state: Path, batch_id: str, staging_name: str
) -> None:
    keys = {
        "schema_version", "mechanism", "version", "argv", "network_namespace",
        "root_filesystem", "mounts", "environment", "immutable_contract",
        "resource_limits", "child_process_policy", "seccomp", "authority",
    }
    if (
        type(sandbox) is not dict
        or set(sandbox) != keys
        or sandbox["schema_version"] != _SANDBOX_SCHEMA_VERSION
        or sandbox["mechanism"] != _BWRAP
        or sandbox["network_namespace"] != "unshared"
        or sandbox["root_filesystem"] != "allowlisted"
        or sandbox["authority"] is not False
        or type(sandbox["version"]) is not str
        or type(sandbox["argv"]) is not list
        or sandbox["argv"][:8]
        != [
            _BWRAP, "--die-with-parent", "--unshare-net", "--unshare-pid",
            "--tmpfs", "/", "--dir", "/run",
        ]
        or sandbox["argv"][-3:-1] != [sys.executable, "-m"]
        or sandbox["argv"][-1]
        not in {"ao_lore.private_docx_uat_worker", "tests.private_docx_uat_worker_fixture"}
        or type(sandbox["mounts"]) is not list
        or type(sandbox["environment"]) is not dict
        or dict(os.environ) != sandbox["environment"]
        or type(sandbox["immutable_contract"]) is not dict
        or sandbox["immutable_contract"]
        != {
            "path": _IMMUTABLE_CONTRACT_PATH,
            "fd": sandbox.get("immutable_contract", {}).get("fd"),
            "sealed": True,
        }
        or type(sandbox["immutable_contract"].get("fd")) is not int
        or sandbox["immutable_contract"]["fd"] < 3
        or sandbox["resource_limits"] != _RESOURCE_LIMITS
        or sandbox["child_process_policy"]
        != "seccomp-deny-clone-fork-vfork-clone3"
        or type(sandbox["seccomp"]) is not dict
        or sandbox["seccomp"]
        != {
            "fd": sandbox.get("seccomp", {}).get("fd"),
            "denied_syscalls": list(_DENIED_CHILD_SYSCALLS),
        }
        or type(sandbox["seccomp"].get("fd")) is not int
        or sandbox["seccomp"]["fd"] < 3
        or sandbox["seccomp"]["fd"] == sandbox["immutable_contract"]["fd"]
    ):
        raise OSError("private DOCX UAT worker sandbox contract is invalid")
    module = sandbox["argv"][-1]
    expected_pythonpath = (
        str(repository_root() / "src")
        if module == "ao_lore.private_docx_uat_worker"
        else f"{repository_root() / 'src'}:{repository_root()}"
    )
    expected_environment = {
        "AO_LORE_HOME": str(root),
        "HOME": str(state / "sandbox-home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "PWD": str(repository_root()),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": expected_pythonpath,
        "XDG_CACHE_HOME": str(state / "sandbox-cache"),
    }
    if sandbox["environment"] != expected_environment:
        raise OSError("private DOCX UAT worker sandbox environment is invalid")
    repository = repository_root()

    def expected_mount(path: Path, name: str, mode: str) -> dict[str, Any]:
        info = os.stat(path, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not (
            stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
        ):
            raise OSError("private DOCX UAT worker sandbox mounts are invalid")
        return {
            "name": name,
            "target": str(path),
            "mode": mode,
            "identity": {"device": info.st_dev, "inode": info.st_ino},
        }

    expected_mounts = [
        expected_mount(Path("/usr"), "system-usr", "read-only"),
        expected_mount(Path("/etc"), "system-etc", "read-only"),
        expected_mount(repository / "src", "ao-lore-src", "read-only"),
    ]
    if module == "tests.private_docx_uat_worker_fixture":
        expected_mounts.append(
            expected_mount(repository / "tests", "ao-lore-tests", "read-only")
        )
    expected_mounts.extend(
        [
            expected_mount(
                repository / "sources" / staging_name,
                "staged-sources",
                "read-only",
            ),
            expected_mount(
                repository / "sources" / _control_directory(staging_name),
                "control-manifest",
                "read-only",
            ),
            expected_mount(
                root / "private-docx" / "qualification.json",
                "qualification",
                "read-only",
            ),
            expected_mount(root / "batches" / batch_id, "batch-state", "read-write"),
            expected_mount(
                repository / "working" / "candidates" / staging_name,
                "candidate-state",
                "read-write",
            ),
            expected_mount(state, "run-state", "read-write"),
        ]
    )
    try:
        for mount in expected_mounts:
            if mount["name"] in {
                "ao-lore-src", "ao-lore-tests", "staged-sources", "control-manifest",
                "batch-state", "candidate-state", "run-state",
            }:
                _require_socket_free_tree(Path(mount["target"]))
    except OSError as exc:
        raise OSError("private DOCX UAT worker sandbox mount contains a socket") from exc
    if sandbox["mounts"] != expected_mounts:
        raise OSError("private DOCX UAT worker sandbox mounts are invalid")
    expected_argv = [
        _BWRAP, "--die-with-parent", "--unshare-net", "--unshare-pid",
        "--tmpfs", "/", "--dir", "/run", "--perms", "0400",
        "--ro-bind-data", str(sandbox["immutable_contract"]["fd"]),
        _IMMUTABLE_CONTRACT_PATH, "--seccomp", str(sandbox["seccomp"]["fd"]),
    ]
    for mount in expected_mounts:
        expected_argv.extend(
            (
                "--bind" if mount["mode"] == "read-write" else "--ro-bind",
                mount["target"],
                mount["target"],
            )
        )
    expected_argv.extend(
        (
            "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--tmpfs", str(state / "sandbox-home"),
            "--tmpfs", str(state / "sandbox-cache"), "--clearenv",
        )
    )
    for key in sorted(expected_environment):
        if key != "PWD":
            expected_argv.extend(("--setenv", key, expected_environment[key]))
    expected_argv.extend(
        (
            "--chdir", str(repository), "--", "/usr/bin/prlimit",
            f"--cpu={_RESOURCE_LIMITS['cpu_seconds']}:{_RESOURCE_LIMITS['cpu_seconds']}",
            f"--as={_RESOURCE_LIMITS['address_space_bytes']}:{_RESOURCE_LIMITS['address_space_bytes']}",
            f"--data={_RESOURCE_LIMITS['data_bytes']}:{_RESOURCE_LIMITS['data_bytes']}",
            f"--fsize={_RESOURCE_LIMITS['file_size_bytes']}:{_RESOURCE_LIMITS['file_size_bytes']}",
            f"--nofile={_RESOURCE_LIMITS['open_files']}:{_RESOURCE_LIMITS['open_files']}",
            f"--nproc={_RESOURCE_LIMITS['processes']}:{_RESOURCE_LIMITS['processes']}",
            "--core=0:0", "--", sys.executable, "-m", module,
        )
    )
    if sandbox["argv"] != expected_argv:
        raise OSError("private DOCX UAT worker sandbox argv is invalid")


def _load_run_contract(*, expected_contract_body: bytes | None = None) -> _HeldRun:
    root = runtime_home()
    descriptors: list[int] = []
    try:
        if expected_contract_body is None:
            immutable_body, _immutable_info = _read_held_file(
                Path(_IMMUTABLE_CONTRACT_PATH),
                _MAX_CONTRACT_BYTES,
                "immutable contract",
                descriptors,
                required_links=None,
            )
        elif (
            type(expected_contract_body) is not bytes
            or not expected_contract_body
            or len(expected_contract_body) > _MAX_CONTRACT_BYTES
        ):
            raise OSError("private DOCX UAT worker immutable contract is invalid")
        else:
            immutable_body = expected_contract_body
        contract = parse_strict_json(
            immutable_body, "private DOCX UAT worker immutable contract"
        )
        keys = {
            "schema_version", "format_id", "policy_digest", "phase",
            "starting_processed", "terminal_processed", "conversion_delta", "batch_id",
            "batch_manifest_digest", "manifest_locator", "control_identity", "manifest_identity",
            "manifest_body_digest", "qualification_locator", "qualification_identity",
            "qualification_body_digest", "qualification_digest", "expectation_digest",
            "corpus_digest", "prepared_evidence_digest", "prepared_evidence_body_digest",
            "staging_name", "staging_identity", "journal_locator", "journal_identity",
            "barrier_locator", "sources", "sandbox", "authority", "run_digest",
        }
        phase = contract.get("phase") if type(contract) is dict else None
        if (
            type(contract) is not dict
            or set(contract) != keys
            or contract["schema_version"] != _WORKER_SCHEMA_VERSION
            or contract["format_id"] != "docx"
            or contract["policy_digest"] != PRIVATE_DOCX_UAT_POLICY_DIGEST
            or re.fullmatch(r"batch-private-docx-uat-[0-9a-f]{24}", contract["batch_id"])
            is None
            or phase not in _PHASE_ARITHMETIC
            or (
                contract["starting_processed"],
                contract["terminal_processed"],
                contract["conversion_delta"],
            )
            != _PHASE_ARITHMETIC[phase]
            or contract["authority"] is not False
            or contract["run_digest"]
            != canonical_digest(
                {key: value for key, value in contract.items() if key != "run_digest"}
            )
            or contract["manifest_locator"]
            != f"sources/{_control_directory(contract['staging_name'])}/run-manifest.json"
        ):
            raise OSError("private DOCX UAT worker immutable contract is invalid")
        state = root / "uat" / "private-docx" / contract["batch_id"]
        contract_body, _contract_info = _read_held_file(
            state / _WORKER_CONTRACT_NAME,
            _MAX_CONTRACT_BYTES,
            "contract",
            descriptors,
        )
        if contract_body != immutable_body:
            raise OSError("private DOCX UAT worker immutable contract drifted")
        manifest_path = repository_root() / contract["manifest_locator"]
        control_descriptor = os.open(
            manifest_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(control_descriptor)
        control_info = os.fstat(control_descriptor)
        if set(os.listdir(control_descriptor)) != {"run-manifest.json"}:
            raise OSError("private DOCX UAT worker control manifest is invalid")
        manifest_body, manifest_info = _read_held_file(
            manifest_path, 64 * 1024, "manifest", descriptors
        )
        batch = validate_batch_manifest(
            parse_strict_json(manifest_body, "private DOCX UAT worker manifest")
        )
        control_public = os.lstat(manifest_path.parent)
        if (
            contract["batch_id"] != batch["batch_id"]
            or contract["batch_manifest_digest"] != canonical_digest(batch)
            or _identity(contract["control_identity"])
            != (control_info.st_dev, control_info.st_ino)
            or (control_public.st_dev, control_public.st_ino)
            != (control_info.st_dev, control_info.st_ino)
            or _identity(contract["manifest_identity"])
            != (manifest_info.st_dev, manifest_info.st_ino)
            or contract["manifest_body_digest"]
            != "sha256:" + hashlib.sha256(manifest_body).hexdigest()
        ):
            raise OSError("private DOCX UAT worker contract is invalid")
        _validate_sandbox_contract(
            contract["sandbox"], root=root, state=state,
            batch_id=batch["batch_id"], staging_name=contract["staging_name"],
        )
        qualification_body, qualification_info = _read_held_file(
            root / contract["qualification_locator"],
            8 * 1024 * 1024,
            "qualification",
            descriptors,
        )
        qualification = parse_strict_json(
            qualification_body, "private DOCX UAT worker qualification"
        )
        from .docx_ooxml import DocxLimits, docx_configuration_digest, validate_docx_benchmark

        validate_docx_benchmark(
            qualification,
            expected_configuration_digest=docx_configuration_digest(DocxLimits()),
            expected_corpus_digest=contract["expectation_digest"],
        )
        if (
            _identity(contract["qualification_identity"])
            != (qualification_info.st_dev, qualification_info.st_ino)
            or contract["qualification_body_digest"]
            != "sha256:" + hashlib.sha256(qualification_body).hexdigest()
            or contract["qualification_digest"] != canonical_digest(qualification)
            or qualification.get("decision") != "hold"
        ):
            raise OSError("private DOCX UAT worker qualification drifted")
        evidence_body, _evidence_info = _read_held_file(
            state / _PREPARED_EVIDENCE_NAME,
            64 * 1024,
            "prepared evidence",
            descriptors,
        )
        evidence = _validate_prepared_run_evidence(
            parse_strict_json(evidence_body, "private DOCX UAT prepared evidence")
        )
        if (
            canonical_digest(evidence) != contract["prepared_evidence_digest"]
            or "sha256:" + hashlib.sha256(evidence_body).hexdigest()
            != contract["prepared_evidence_body_digest"]
            or evidence["batch_manifest_digest"] != contract["batch_manifest_digest"]
            or evidence["corpus_digest"] != contract["corpus_digest"]
            or evidence["expectation_digest"] != contract["expectation_digest"]
        ):
            raise OSError("private DOCX UAT worker prepared evidence drifted")
        expected_sources = [
            {
                "item_id": source["item_id"],
                "derived_digest": source["derived_digest"],
                "original_digest": source["original_digest"],
                "transformation_id": source["transformation_id"],
            }
            for source in evidence["sources"]
        ]
        if contract["sources"] != expected_sources:
            raise OSError("private DOCX UAT worker source provenance drifted")
        staging_path = repository_root() / "sources" / contract["staging_name"]
        staging_descriptor = os.open(
            staging_path,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(staging_descriptor)
        staging_info = os.fstat(staging_descriptor)
        if _identity(contract["staging_identity"]) != (staging_info.st_dev, staging_info.st_ino):
            raise OSError("private DOCX UAT worker staging source drifted")
        expected_names = {f"{item['item_id']}.docx" for item in contract["sources"]}
        if set(os.listdir(staging_descriptor)) != expected_names:
            raise OSError("private DOCX UAT worker source entries drifted")
        held_sources: dict[str, bytes] = {}
        origins: dict[str, DocxProvenanceOrigin] = {}
        for document, origin in zip(batch["documents"], contract["sources"], strict=True):
            expected_locator = f"sources/{contract['staging_name']}/{origin['item_id']}.docx"
            if (
                document["item_id"] != origin["item_id"]
                or document["source"] != expected_locator
                or document["source_digest"] != origin["derived_digest"]
                or origin["transformation_id"] != DOCX_TRANSFORMATION_ID
            ):
                raise OSError("private DOCX UAT worker source mapping drifted")
            name = f"{origin['item_id']}.docx"
            source_descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=staging_descriptor,
            )
            descriptors.append(source_descriptor)
            before = os.fstat(source_descriptor)
            body = os.pread(source_descriptor, MAX_DOCX_BYTES + 1, 0)
            after = os.fstat(source_descriptor)
            public = os.stat(name, dir_fd=staging_descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size <= 0
                or before.st_size > MAX_DOCX_BYTES
                or len(body) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (before.st_dev, before.st_ino) != (public.st_dev, public.st_ino)
                or "sha256:" + hashlib.sha256(body).hexdigest() != origin["derived_digest"]
            ):
                raise OSError("private DOCX UAT worker held source drifted")
            held_sources[document["source"]] = body
            origins[document["source"]] = DocxProvenanceOrigin(
                corpus_id=DOCX_PRIVATE_CORPUS_ID,
                original_digest=origin["original_digest"],
                derived_digest=origin["derived_digest"],
                transformation_id=origin["transformation_id"],
                expectation_digest=contract["expectation_digest"],
            )
        journal_path = root / contract["journal_locator"]
        if (
            contract["journal_locator"]
            != f"uat/private-docx/{batch['batch_id']}/conversion-journal.jsonl"
            or contract["barrier_locator"]
            != f"uat/private-docx/{batch['batch_id']}/{_WORKER_BARRIER_NAME}"
        ):
            raise OSError("private DOCX UAT worker state locator drifted")
        journal_parent = os.open(
            journal_path.parent,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(journal_parent)
        journal_descriptor = os.open(
            journal_path.name,
            os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=journal_parent,
        )
        descriptors.append(journal_descriptor)
        held = _HeldRun(
            contract=contract,
            batch=batch,
            qualification=qualification,
            sources=held_sources,
            source_origins=origins,
            journal_parent_descriptor=journal_parent,
            journal_descriptor=journal_descriptor,
            attempts=[],
            descriptors=descriptors,
        )
        _verify_public_journal(held)
        expected_start = contract["starting_processed"]
        checkpoint_path = root / "batches" / batch["batch_id"] / "checkpoint.json"
        if checkpoint_path.exists():
            checkpoint_value, _ = strict_read_json(
                checkpoint_path,
                "private DOCX UAT worker starting checkpoint",
                max_bytes=64 * 1024,
                root=root,
            )
            checkpoint = validate_checkpoint(
                checkpoint_value, batch, contract["batch_manifest_digest"]
            )
            checkpoint_processed = checkpoint["processed"]
        else:
            checkpoint_processed = 0
        if (checkpoint_processed, _journal_count(held)) != (
            expected_start,
            expected_start,
        ):
            raise OSError("private DOCX UAT worker phase arithmetic is invalid")
        return held
    except BaseException:
        while descriptors:
            os.close(descriptors.pop())
        raise


def _verify_public_journal(held: _HeldRun) -> os.stat_result:
    opened = os.fstat(held.journal_descriptor)
    public = os.stat(
        "conversion-journal.jsonl",
        dir_fd=held.journal_parent_descriptor,
        follow_symlinks=False,
    )
    expected = _identity(held.contract.get("journal_identity"))
    if (
        expected is None
        or (opened.st_dev, opened.st_ino) != expected
        or (public.st_dev, public.st_ino) != expected
        or not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or opened.st_size < 0
        or opened.st_size > _MAX_JOURNAL_BYTES
    ):
        raise OSError("private DOCX UAT conversion journal binding drifted")
    return opened


def _journal_snapshot(held: _HeldRun) -> tuple[int, str | None, os.stat_result]:
    before = _verify_public_journal(held)
    body = os.pread(held.journal_descriptor, _MAX_JOURNAL_BYTES + 1, 0)
    after = os.fstat(held.journal_descriptor)
    if (
        len(body) > _MAX_JOURNAL_BYTES
        or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise OSError("private DOCX UAT conversion journal is invalid")
    previous: str | None = None
    lines = body.splitlines(keepends=True)
    for sequence, line in enumerate(lines, 1):
        if not line.endswith(b"\n"):
            raise OSError("private DOCX UAT conversion journal is invalid")
        event = parse_strict_json(line, "private DOCX UAT conversion journal")
        if (
            type(event) is not dict
            or set(event)
            != {"schema_version", "sequence", "previous_event_digest", "event_digest"}
            or event["schema_version"] != "ao.lore.private-docx-conversion-event.v0.1"
            or event["sequence"] != sequence
            or event["previous_event_digest"] != previous
            or event["event_digest"]
            != canonical_digest({key: value for key, value in event.items() if key != "event_digest"})
        ):
            raise OSError("private DOCX UAT conversion journal is invalid")
        previous = event["event_digest"]
    _verify_public_journal(held)
    return len(lines), previous, after


def _journal_count(held: _HeldRun) -> int:
    return _journal_snapshot(held)[0]


def _append_conversion_event(held: _HeldRun) -> None:
    count, previous, info = _journal_snapshot(held)
    event: dict[str, Any] = {
        "schema_version": "ao.lore.private-docx-conversion-event.v0.1",
        "sequence": count + 1,
        "previous_event_digest": previous,
    }
    event["event_digest"] = canonical_digest(event)
    encoded = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if info.st_size + len(encoded) > _MAX_JOURNAL_BYTES:
        raise OSError("private DOCX UAT conversion journal is full")
    _verify_public_journal(held)
    view = memoryview(encoded)
    while view:
        written = os.write(held.journal_descriptor, view)
        if written <= 0:
            raise OSError("private DOCX UAT conversion journal short write")
        view = view[written:]
    os.fsync(held.journal_descriptor)
    os.fsync(held.journal_parent_descriptor)
    if _journal_count(held) != count + 1:
        raise OSError("private DOCX UAT conversion journal append drifted")


def _persist_barrier(held: _HeldRun, count: int) -> None:
    if held.contract.get("phase") != "interrupted" or count != 1:
        raise OSError("private DOCX UAT worker barrier count is invalid")
    root = runtime_home()
    deadline = time.monotonic() + 30.0
    checkpoint: dict[str, Any] | None = None
    while time.monotonic() <= deadline:
        try:
            supplied, _ = strict_read_json(
                root / "batches" / held.batch["batch_id"] / "checkpoint.json",
                "private DOCX UAT live checkpoint",
                max_bytes=64 * 1024,
                root=root,
            )
            observed = validate_checkpoint(
                supplied, held.batch, held.contract["batch_manifest_digest"]
            )
        except FileNotFoundError:
            time.sleep(0.005)
            continue
        if observed["processed"] == count:
            checkpoint = observed
            break
        if observed["processed"] > count:
            raise OSError("private DOCX UAT worker checkpoint passed barrier")
        time.sleep(0.005)
    if checkpoint is None or _journal_count(held) != count:
        raise OSError("private DOCX UAT worker barrier timed out")
    barrier = {
        "schema_version": "ao.lore.private-docx-uat-barrier.v0.1",
        "format_id": "docx",
        "policy_digest": PRIVATE_DOCX_UAT_POLICY_DIGEST,
        "phase": "interrupted",
        "batch_id": held.batch["batch_id"],
        "batch_manifest_digest": held.contract["batch_manifest_digest"],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "processed": count,
        "journal_count": count,
        "run_digest": held.contract["run_digest"],
        "authority": False,
    }
    body = (json.dumps(barrier, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = f".barrier-{os.getpid()}-{time.monotonic_ns()}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=held.journal_parent_descriptor,
        )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("private DOCX UAT worker barrier short write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(
            temporary,
            _WORKER_BARRIER_NAME,
            src_dir_fd=held.journal_parent_descriptor,
            dst_dir_fd=held.journal_parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(temporary, dir_fd=held.journal_parent_descriptor)
        os.fsync(held.journal_parent_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=held.journal_parent_descriptor)
        except FileNotFoundError:
            pass


@contextlib.contextmanager
def _counted_adapter(held: _HeldRun) -> Iterator[None]:
    from .docx_ooxml import NativeDocxOoxmlAdapter

    original = NativeDocxOoxmlAdapter.parse

    def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        if held.attempts:
            raise _NetworkAttempt("private DOCX UAT guarded operation was caught")
        count = _journal_count(held)
        if held.contract.get("phase") == "interrupted" and count >= 1:
            if count != 1:
                raise OSError("private DOCX UAT worker interruption count drifted")
            _persist_barrier(held, count)
            while True:
                signal.pause()
        try:
            result = original(self, *args, **kwargs)
        except Exception:
            if held.attempts:
                raise _NetworkAttempt("private DOCX UAT guarded operation was caught")
            _append_conversion_event(held)
            raise
        except BaseException:
            if held.attempts:
                raise _NetworkAttempt("private DOCX UAT guarded operation was caught")
            raise
        if held.attempts:
            raise _NetworkAttempt("private DOCX UAT guarded operation was caught")
        _append_conversion_event(held)
        return result

    NativeDocxOoxmlAdapter.parse = counted
    try:
        yield
    finally:
        NativeDocxOoxmlAdapter.parse = original


def _batch_dependencies(held: _HeldRun) -> BatchDependencies:
    from .__main__ import _docx_ingest_profiles

    selection, quality = _docx_ingest_profiles(held.qualification)

    def source_key(path: str | Path) -> str:
        try:
            return Path(path).relative_to(repository_root()).as_posix()
        except ValueError as exc:
            raise OSError("private DOCX UAT held source locator drifted") from exc

    def read_source(path: str | Path, expected_digest: str) -> bytes:
        key = source_key(path)
        body = held.sources.get(key)
        if (
            body is None
            or "sha256:" + hashlib.sha256(body).hexdigest() != expected_digest
        ):
            raise OSError("private DOCX UAT held source binding drifted")
        return body

    def origin_for(path: str | Path, body: bytes) -> DocxProvenanceOrigin:
        key = source_key(path)
        origin = held.source_origins.get(key)
        if origin is None or held.sources.get(key) != body:
            raise OSError("private DOCX UAT held source provenance drifted")
        return origin

    def ingest_one(path: str | Path, **kwargs: Any) -> Mapping[str, Any]:
        body = read_source(path, kwargs["expected_source_digest"])
        digest = kwargs["expected_source_digest"]
        return ingest_verified_docx(
            body,
            source_digest=digest,
            resource="source-" + digest.removeprefix("sha256:")[:16] + ".docx",
            benchmark_manifest=kwargs["benchmark_manifest"],
            expected_corpus_digest=kwargs["expected_corpus_digest"],
            selection_profile=kwargs["selection_profile"],
            quality_profile=kwargs["quality_profile"],
            origin=kwargs["origin"],
            candidate_context=kwargs["candidate_context"],
            dependencies=kwargs["dependencies"],
            candidate_root=kwargs["candidate_root"],
        )

    state = runtime_home() / "uat" / "private-docx" / held.batch["batch_id"]
    candidate_root = repository_root() / "working" / "candidates" / held.contract["staging_name"]
    return BatchDependencies(
        benchmark_manifest=held.qualification,
        selection_profile=selection,
        quality_profile=quality,
        format_id="docx",
        media_type=DOCX_MIME,
        extension=".docx",
        parser_id="native-docx-ooxml",
        parser_version="1.0.0",
        signature_check=_docx_signature_matches,
        expected_corpus_digest=held.contract["expectation_digest"],
        origin_for=origin_for,
        ingest_one=ingest_one,
        read_source=read_source,
        candidate_root=candidate_root,
    )


def main(
    *,
    after_validation: Callable[[_HeldRun], None] | None = None,
    after_ingest: Callable[[_HeldRun], None] | None = None,
) -> int:
    held = _load_run_contract()
    report: Mapping[str, Any] | None = None
    callback_started = False
    try:
        with _execution_guards(held.attempts):
            callback_started = True
            if after_validation is not None:
                after_validation(held)
            try:
                with _counted_adapter(held):
                    report = ingest_verified_batch(
                        held.batch,
                        held.contract["batch_manifest_digest"],
                        dependencies=_batch_dependencies(held),
                    )
                if held.attempts:
                    raise _NetworkAttempt("private DOCX UAT guarded operation was caught")
            finally:
                if callback_started and after_ingest is not None:
                    after_ingest(held)
        if held.attempts:
            raise _NetworkAttempt("private DOCX UAT guarded operation was caught")
    finally:
        held.close()
    if report is None:
        raise OSError("private DOCX UAT worker produced no report")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
