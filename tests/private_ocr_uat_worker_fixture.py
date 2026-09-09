"""Actual outer OCR phase sandbox probe used only by its integration test."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path

from ao_lore._strict_io import parse_strict_json
from ao_lore.private_ocr_uat_worker import validate_uat_worker_contract


def _denied(operation) -> bool:
    try:
        operation()
    except BaseException:
        return True
    return False


def _socket_creation_allowed() -> bool:
    try:
        stream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return False
    stream.close()
    return True


def main() -> int:
    raw = Path("/launch/worker-contract.json").read_bytes()
    contract = validate_uat_worker_contract(
        parse_strict_json(raw, "private OCR immutable phase contract")
    )
    def connect(family: int, kind: int, address: object, *, payload: bytes | None = None):
        stream = socket.socket(family, kind)
        try:
            return stream.connect(address) if payload is None else stream.sendto(payload, address)
        finally:
            stream.close()

    proof = {
        "contract_valid": True,
        "socket_creation_allowed": _socket_creation_allowed(),
        "tcp_denied": _denied(lambda: connect(socket.AF_INET, socket.SOCK_STREAM, ("127.0.0.1", 9))),
        "udp_denied": _denied(lambda: connect(socket.AF_INET, socket.SOCK_DGRAM, ("127.0.0.1", 9), payload=b"x")),
        "dns_denied": _denied(lambda: socket.getaddrinfo("example.invalid", 443)),
        "host_unix_socket_absent": _denied(lambda: connect(socket.AF_UNIX, socket.SOCK_STREAM, "/app/.ao-lore/host.sock")),
        "outside_write_denied": _denied(lambda: Path("/app/.ao-lore/foreign").write_bytes(b"x")),
    }
    nested = subprocess.run(
        [
            "/usr/bin/bwrap", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
            "--tmpfs", "/",
            "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
            "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
            "--dev", "/dev", "--proc", "/proc", "--", "/usr/bin/true",
        ],
        env={}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=10, check=False,
    )
    proof["nested_bwrap_allowed"] = (
        nested.returncode == 0 and nested.stdout == b"" and len(nested.stderr) <= 4096
    )
    proof["authority"] = False
    target = Path("/app/.ao-lore/uat/private-ocr") / contract["batch_id"] / "sandbox-proof.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        body = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode("ascii")
        os.write(descriptor, body)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return 0 if all(value is True for key, value in proof.items() if key != "authority") else 1


if __name__ == "__main__":
    raise SystemExit(main())
