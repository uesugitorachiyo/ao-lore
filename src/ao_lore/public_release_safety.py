"""Pure, deterministic safety scan for a candidate public release tree."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from urllib.parse import urlsplit
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._strict_io import ContractError
from .evidence_graph_contracts import validate_dns_hostname
from .public_release_contracts import policy_allows_path, validate_public_release_policy


@dataclass(frozen=True)
class PublicReleaseLimits:
    max_files: int = 4096
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 256 * 1024 * 1024
    max_depth: int = 12


_PRIVATE_PATH = re.compile(rb"(?:/(?:home|Users)/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._~+-]+)*|/opt/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._~+-]+)+|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\[^\s\x00]+)")
_EMAIL = re.compile(rb"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IP = re.compile(rb"\b(?:10|169\.254|172\.(?:1[6-9]|2\d|3[01])|192\.168)(?:\.\d{1,3}){2,3}\b")
_SECRET = re.compile(rb"(?i)(?:BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY|(?:aws_secret_access_key|github_token|openai_api_key|api[_-]?key|password)\s*[:=]\s*[A-Za-z0-9_./+\-=]{16,})")
_LOCATOR = re.compile(rb"(?i)\b(?:https?|ssh|ftp)://[^\s\x00<>'\"]{1,2048}")
_INTERNAL_HOST = re.compile(
    rb"(?i)\b(?:host(?:name)?|server|connect(?:ing)?(?:\s+to)?)\s*[:=]?\s*"
    rb"(?:localhost|[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.(?:corp|home|internal|invalid|lan|local|test))\b"
)
_BINARY_SIGNATURES = (b"PK\x03\x04", b"%PDF-", b"\x7fELF", b"\x89PNG\r\n\x1a\n", b"\xd0\xcf\x11\xe0")
_PRIVATE_HOST_SUFFIXES = (".corp", ".home", ".internal", ".invalid", ".lan", ".local", ".test")


def _has_private_network_locator(body: bytes) -> bool:
    if _INTERNAL_HOST.search(body):
        return True
    for match in _LOCATOR.finditer(body):
        locator = match.group(0)
        try:
            decoded = locator.decode("ascii")
            parsed = urlsplit(decoded)
            hostname = parsed.hostname or ""
            port = parsed.port
            validate_dns_hostname(hostname, "public release locator hostname")
        except (ContractError, UnicodeError, ValueError):
            return True
        expected_netloc = hostname if port is None else f"{hostname}:{port}"
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.netloc != expected_netloc
            or parsed.geturl() != decoded
            or "\\" in decoded
            or "(?:" in decoded
            or "." not in hostname
            or port not in {None, 80, 443}
            or hostname == "localhost"
            or hostname.endswith(_PRIVATE_HOST_SUFFIXES)
        ):
            return True
    return False


def _read_regular(parent_fd: int, name: str, before: os.stat_result, maximum: int) -> tuple[bytes | None, str | None]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        return None, "unstable_read"
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_mode) != (before.st_dev, before.st_ino, before.st_mode):
            return None, "unstable_read"
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
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            return None, "unstable_read"
        return body, None
    finally:
        os.close(descriptor)


def _content_reasons(path: str, body: bytes, policy: dict[str, Any]) -> set[str]:
    reasons: set[str] = set()
    binary_allowed = path in policy["binary_files"]
    if (b"\x00" in body or body.startswith(_BINARY_SIGNATURES)) and not binary_allowed:
        reasons.add("binary_not_allowed")
    if not binary_allowed and any(byte < 32 and byte not in (9, 10, 13) for byte in body):
        reasons.add("control_character")
    if _PRIVATE_PATH.search(body):
        reasons.add("private_path")
    for match in _EMAIL.finditer(body):
        email = match.group(0).lower()
        if not email.endswith(b".invalid"):
            reasons.add("email_address")
    if _IP.search(body):
        reasons.add("ip_address")
    if _SECRET.search(body):
        reasons.add("secret_candidate")
    if _has_private_network_locator(body):
        reasons.add("network_locator")
    return reasons


def scan_public_tree(root: Path, policy: dict[str, object], *, limits: PublicReleaseLimits = PublicReleaseLimits()) -> dict[str, object]:
    """Scan regular files under ``root`` without writes, subprocesses, or network."""
    validated = validate_public_release_policy(policy)
    root = Path(root)
    try:
        root_info = os.lstat(root)
    except OSError as exc:
        raise ContractError("public tree root is unavailable") from exc
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ContractError("public tree root must be a real directory")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root, flags)
    except OSError as exc:
        raise ContractError("public tree root could not be held") from exc

    reasons: set[str] = set()
    findings: dict[str, int] = {}
    entries: list[dict[str, object]] = []
    casefold_paths: set[str] = set()
    total_bytes = 0

    def reject(reason: str) -> None:
        reasons.add(reason)
        findings[reason] = findings.get(reason, 0) + 1

    def walk(directory_fd: int, prefix: str, depth: int) -> None:
        nonlocal total_bytes
        if depth > limits.max_depth:
            reject("depth_exceeded")
            return
        try:
            names = sorted(entry.name for entry in os.scandir(directory_fd))
        except OSError:
            reject("unstable_directory")
            return
        for name in names:
            path = f"{prefix}/{name}" if prefix else name
            folded = path.casefold()
            if folded in casefold_paths:
                reject("case_collision")
            casefold_paths.add(folded)
            try:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                reject("unstable_read")
                continue
            if stat.S_ISLNK(info.st_mode):
                reject("symlink_not_allowed")
                continue
            if stat.S_ISDIR(info.st_mode):
                try:
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError:
                    reject("unstable_directory")
                    continue
                try:
                    walk(child_fd, path, depth + 1)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode):
                reject("special_file_not_allowed")
                continue
            if info.st_nlink != 1:
                reject("hardlink_not_allowed")
            if not policy_allows_path(path, validated):
                reject("path_not_allowed")
            executable = bool(info.st_mode & 0o111)
            if executable and not any(path.startswith(item) for item in validated["executable_prefixes"]):
                reject("executable_mode_not_allowed")
            if info.st_size > limits.max_file_bytes or info.st_size > validated["limits"]["max_file_bytes"]:
                reject("file_size_exceeded")
            body, error = _read_regular(directory_fd, name, info, min(limits.max_file_bytes, validated["limits"]["max_file_bytes"]))
            if error:
                reject(error)
                continue
            assert body is not None
            if len(body) > min(limits.max_file_bytes, validated["limits"]["max_file_bytes"]):
                reject("file_size_exceeded")
            digest = hashlib.sha256(body).hexdigest()
            content_reasons = _content_reasons(path, body, validated)
            exceptions = [item for item in validated["exceptions"] if item["path"] == path and item["sha256"] == digest]
            for exception in exceptions:
                if exception["reason_code"] == "inert_network_locator":
                    content_reasons.discard("network_locator")
                else:
                    suppressed = {"inert_private_path": "private_path", "inert_secret_candidate": "secret_candidate"}[exception["reason_code"]]
                    content_reasons.discard(suppressed)
            for reason in content_reasons:
                reject(reason)
            total_bytes += len(body)
            entries.append({"path": path, "mode": "100755" if executable else "100644", "size": len(body), "sha256": digest})

    try:
        walk(root_fd, "", 0)
    finally:
        os.close(root_fd)
    if len(entries) > min(limits.max_files, validated["limits"]["max_files"]):
        reject("file_count_exceeded")
    if total_bytes > min(limits.max_total_bytes, validated["limits"]["max_total_bytes"]):
        reject("total_size_exceeded")
    entries.sort(key=lambda item: item["path"])
    manifest_digest = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {
        "status": "rejected" if reasons else "accepted",
        "reason_codes": sorted(reasons),
        "finding_counts": {key: findings[key] for key in sorted(findings)},
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "manifest_digest": manifest_digest,
    }
