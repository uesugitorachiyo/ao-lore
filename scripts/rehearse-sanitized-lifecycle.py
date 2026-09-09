#!/usr/bin/env python3
"""Run the fixed, offline synthetic governed-knowledge lifecycle rehearsal."""

from __future__ import annotations

import contextlib
import fcntl
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NoReturn


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY / "src"
CAMPAIGN_ROOT = REPOSITORY / ".ao-lore" / "sanitized-lifecycle-rehearsal"
RETAINED_RUN = CAMPAIGN_ROOT / "retained"
GENERATOR = (
    REPOSITORY / "tests" / "fixtures" / "ao_lore"
    / "sanitized_lifecycle" / "generate.py"
)
REPORT_NAME = "sanitized-lifecycle-report.json"
OWNER_NAME = ".ao-lore-sanitized-lifecycle-owner.json"
OWNER_BYTES = b'{"owner":"ao-lore-sanitized-lifecycle-fixture","schema_version":"v0.1"}\n'
NOW = "2026-08-14T12:00:00Z"
REJECTION = "sanitized lifecycle rehearsal rejected\n"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)

if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ao_lore.sanitized_lifecycle import (  # noqa: E402
    SanitizedLifecycleError,
    _run_sanitized_lifecycle_in_rehearsal_run,
)
from ao_lore.sanitized_lifecycle_contracts import (  # noqa: E402
    validate_sanitized_lifecycle_rehearsal,
)


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        stat.S_IFMT(left.st_mode),
    ) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    ) and stat.S_ISDIR(left.st_mode) and stat.S_ISDIR(right.st_mode)


def _verify_campaign(descriptor: int, held: os.stat_result) -> None:
    rebound = os.lstat(CAMPAIGN_ROOT)
    if not _same_directory(held, os.fstat(descriptor)) or not _same_directory(
        held, rebound
    ):
        raise ValueError("campaign rebound")


@contextmanager
def _campaign_lock() -> Iterator[int]:
    runtime = REPOSITORY / ".ao-lore"
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime_info = os.lstat(runtime)
    if stat.S_ISLNK(runtime_info.st_mode) or not stat.S_ISDIR(runtime_info.st_mode):
        raise ValueError("runtime root")
    CAMPAIGN_ROOT.mkdir(mode=0o700, exist_ok=True)
    campaign_info = os.lstat(CAMPAIGN_ROOT)
    if stat.S_ISLNK(campaign_info.st_mode) or not stat.S_ISDIR(campaign_info.st_mode):
        raise ValueError("campaign root")
    descriptor = os.open(CAMPAIGN_ROOT, _DIRECTORY_FLAGS)
    locked = False
    try:
        held = os.fstat(descriptor)
        if not _same_directory(campaign_info, held):
            raise ValueError("campaign open")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        _verify_campaign(descriptor, held)
        yield descriptor
        _verify_campaign(descriptor, held)
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_owner(run_descriptor: int) -> None:
    descriptor = os.open(OWNER_NAME, _FILE_FLAGS, 0o600, dir_fd=run_descriptor)
    try:
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
            raise ValueError("owner file")
        view = memoryview(OWNER_BYTES)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short owner write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fixture_module():
    spec = importlib.util.spec_from_file_location(
        "ao_lore_sanitized_lifecycle_fixture_generator", GENERATOR
    )
    if spec is None or spec.loader is None:
        raise ValueError("fixture generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare_run(
    campaign_descriptor: int, run: Path, *, already_created: bool = False,
) -> None:
    if run.parent != CAMPAIGN_ROOT or not run.name:
        raise ValueError("run root")
    if not already_created:
        os.mkdir(run.name, mode=0o700, dir_fd=campaign_descriptor)
    before = os.stat(run.name, dir_fd=campaign_descriptor, follow_symlinks=False)
    descriptor = os.open(run.name, _DIRECTORY_FLAGS, dir_fd=campaign_descriptor)
    try:
        held = os.fstat(descriptor)
        if not _same_directory(before, held) or os.listdir(descriptor):
            raise ValueError("run root")
        _write_owner(descriptor)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fixture_stderr = io.StringIO()
    with contextlib.redirect_stderr(fixture_stderr):
        generated = _fixture_module().main(["--out", str(run / "fixture")])
    if generated != 0 or fixture_stderr.getvalue():
        raise ValueError("fixture generation")
    rebound = os.stat(run.name, dir_fd=campaign_descriptor, follow_symlinks=False)
    if not _same_directory(before, rebound):
        raise ValueError("run rebound")


def _deny_network(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("network access denied")


def _provider_entry(*_args: object, **_kwargs: object) -> NoReturn:
    raise RuntimeError("provider access denied")


def _source_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=REPOSITORY,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )
    if completed.returncode != 0 or completed.stderr:
        raise ValueError("source head")
    value = completed.stdout.decode("ascii", "strict").strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("source head")
    return value


def _run(run: Path) -> bytes:
    report = _run_sanitized_lifecycle_in_rehearsal_run(
        run,
        source_head=_source_head(),
        now=NOW,
        deny_network=_deny_network,
        deny_provider=_provider_entry,
    )
    validated = validate_sanitized_lifecycle_rehearsal(report)
    return _canonical_bytes(validated)


def _remove_contents(descriptor: int) -> None:
    for name in sorted(os.listdir(descriptor)):
        if name in {"", ".", ".."}:
            raise ValueError("scratch entry")
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
            try:
                held = os.fstat(child)
                if not _same_directory(before, held):
                    raise ValueError("scratch directory")
                _remove_contents(child)
                rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not _same_directory(held, rebound):
                    raise ValueError("scratch directory rebound")
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            os.unlink(name, dir_fd=descriptor)
        else:
            raise ValueError("scratch artifact")
    os.fsync(descriptor)


def _remove_scratch(campaign_descriptor: int, run: Path) -> None:
    if run.parent != CAMPAIGN_ROOT or not run.name.startswith("check-"):
        raise ValueError("scratch root")
    before = os.stat(run.name, dir_fd=campaign_descriptor, follow_symlinks=False)
    descriptor = os.open(run.name, _DIRECTORY_FLAGS, dir_fd=campaign_descriptor)
    try:
        held = os.fstat(descriptor)
        if not _same_directory(before, held):
            raise ValueError("scratch root")
        _remove_contents(descriptor)
        rebound = os.stat(run.name, dir_fd=campaign_descriptor, follow_symlinks=False)
        if not _same_directory(held, rebound):
            raise ValueError("scratch root rebound")
    finally:
        os.close(descriptor)
    os.rmdir(run.name, dir_fd=campaign_descriptor)
    os.fsync(campaign_descriptor)


def _direct(campaign_descriptor: int) -> bytes:
    if RETAINED_RUN.parent != CAMPAIGN_ROOT:
        raise ValueError("retained root")
    try:
        os.stat(RETAINED_RUN.name, dir_fd=campaign_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        _prepare_run(campaign_descriptor, RETAINED_RUN)
    return _run(RETAINED_RUN)


def _check(campaign_descriptor: int) -> tuple[int, bytes | None]:
    retained = _run(RETAINED_RUN)
    scratch = Path(tempfile.mkdtemp(prefix="check-", dir=CAMPAIGN_ROOT))
    try:
        _prepare_run(campaign_descriptor, scratch, already_created=True)
        recomputed = _run(scratch)
        if recomputed != retained:
            return 1, None
        return 0, retained
    finally:
        _remove_scratch(campaign_descriptor, scratch)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        if arguments not in ([], ["--check"]):
            raise ValueError("argument contract")
        with _campaign_lock() as campaign_descriptor:
            if arguments:
                status, body = _check(campaign_descriptor)
            else:
                status, body = 0, _direct(campaign_descriptor)
        if body is not None:
            sys.stdout.write(body.decode("utf-8"))
        return status
    except (
        ImportError,
        OSError,
        RuntimeError,
        SanitizedLifecycleError,
        subprocess.SubprocessError,
        TypeError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        sys.stderr.write(REJECTION)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
