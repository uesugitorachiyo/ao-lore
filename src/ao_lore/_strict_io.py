"""Small fail-closed JSON and path helpers shared by AO Lore surfaces."""

from __future__ import annotations

import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Mapping


IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRANSITION_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,127}->[a-z0-9][a-z0-9._-]{0,127}$"
)


class ContractError(ValueError):
    """Raised when supplied evidence violates a strict AO Lore contract."""


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON number is forbidden: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_strict_json(body: bytes, label: str) -> dict[str, Any]:
    try:
        decoded = body.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid {label} JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return value


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def ensure_contained(path: Path, root: Path, label: str) -> tuple[Path, Path]:
    absolute = _absolute(path)
    absolute_root = _absolute(root)
    try:
        absolute.relative_to(absolute_root)
    except ValueError as exc:
        raise ContractError(f"{label} escapes its allowed root") from exc
    return absolute, absolute_root


def reject_symlink_ancestors(path: Path, *, include_self: bool = False) -> None:
    absolute = _absolute(path)
    parts = absolute.parts
    current = Path(parts[0])
    stop = len(parts) if include_self else len(parts) - 1
    for part in parts[1:stop]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError as exc:
            raise ContractError(f"path ancestor does not exist: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise ContractError(f"path ancestor is a symlink: {current}")
        if not stat.S_ISDIR(info.st_mode):
            raise ContractError(f"path ancestor is not a directory: {current}")


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def strict_read_json(
    path: str | os.PathLike[str],
    label: str,
    *,
    max_bytes: int,
    root: str | os.PathLike[str] | None = None,
) -> tuple[dict[str, Any], bytes]:
    target = Path(path)
    if root is not None:
        target, _ = ensure_contained(target, Path(root), label)
    else:
        target = _absolute(target)
    reject_symlink_ancestors(target)
    try:
        before = os.lstat(target)
    except FileNotFoundError as exc:
        raise ContractError(f"missing {label}: {target}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ContractError(f"{label} must be a regular non-link file")
    if before.st_size > max_bytes:
        raise ContractError(f"{label} exceeds {max_bytes} bytes")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or _file_identity(before) != _file_identity(opened)
        ):
            raise ContractError(f"{label} changed during validation")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        try:
            after = os.fstat(descriptor)
            rebound = os.lstat(target)
        except OSError as exc:
            raise ContractError(f"{label} changed during validation") from exc
        if (
            not stat.S_ISREG(after.st_mode)
            or not stat.S_ISREG(rebound.st_mode)
            or after.st_nlink != 1
            or rebound.st_nlink != 1
            or _file_identity(opened) != _file_identity(after)
            or _file_identity(opened) != _file_identity(rebound)
        ):
            raise ContractError(f"{label} changed during validation")
    finally:
        os.close(descriptor)
    if len(body) > max_bytes:
        raise ContractError(f"{label} exceeds {max_bytes} bytes")
    return parse_strict_json(body, label), body


def write_exclusive_json(
    path: str | os.PathLike[str],
    value: Mapping[str, Any],
    *,
    root: str | os.PathLike[str],
) -> None:
    target, _ = ensure_contained(Path(path), Path(root), "output path")
    reject_symlink_ancestors(target)
    parent = target.parent
    info = os.lstat(parent)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ContractError("output parent must be a real directory")
    body = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise ContractError("output already exists") from exc
    remove_on_failure = True
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        remove_on_failure = False
    finally:
        os.close(descriptor)
        if remove_on_failure:
            try:
                target.unlink()
            except FileNotFoundError:
                pass


def require_exact_keys(
    value: Mapping[str, Any], required: Iterable[str], label: str
) -> None:
    expected = set(required)
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing or unknown:
        raise ContractError(f"{label} keys differ; missing={missing}, unknown={unknown}")


def require_identifier(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise ContractError(f"{label} must be a bounded lowercase identifier")
    return value


def require_sha256(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def require_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise ContractError(f"{label} must be between {minimum} and {maximum}")
    return value


def require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{label} must be a boolean")
    return value


def require_text(value: Any, label: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ContractError(f"{label} must be non-empty bounded text")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ContractError(f"{label} contains forbidden control characters")
    return value


def reject_nonfinite(value: float, label: str) -> float:
    if not math.isfinite(value):
        raise ContractError(f"{label} must be finite")
    return value
