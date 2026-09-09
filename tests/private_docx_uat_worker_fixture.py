"""Adversarial subprocess fixture for the private DOCX bubblewrap boundary."""

from __future__ import annotations

import json
import ctypes
import os
import resource
import socket
import subprocess
from pathlib import Path

from ao_lore.private_docx_uat_worker import (
    _NetworkAttempt,
    _ProcessAttempt,
    _execution_guards,
)


def _denied(operation, expected) -> bool:
    try:
        operation()
    except expected:
        return True
    except (OSError, PermissionError, FileNotFoundError):
        return True
    return False


def main() -> int:
    root = Path(os.environ["AO_LORE_HOME"])
    action_files = list((root / "uat/private-docx").glob("batch-*/fixture-action.json"))
    if action_files:
        action = json.loads(action_files[0].read_text(encoding="utf-8"))
        if action == {"action": "stdout-flood"}:
            os.write(1, b"x" * (1024 * 1024 + 1))
            return 0
    attempts: list[str] = []
    with _execution_guards(attempts):
        guarded = {
            "tcp": _denied(
                lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("127.0.0.1", 9)),
                _NetworkAttempt,
            ),
            "udp_sendto": _denied(
                lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b"x", ("127.0.0.1", 9)),
                _NetworkAttempt,
            ),
            "udp_sendmsg": _denied(
                lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendmsg([b"x"], [], 0, ("127.0.0.1", 9)),
                _NetworkAttempt,
            ),
            "dns": _denied(lambda: socket.getaddrinfo("example.invalid", 443), _NetworkAttempt),
            "af_unix": _denied(
                lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect("/run/ao-lore-host.sock"),
                _NetworkAttempt,
            ),
            "subprocess": _denied(lambda: subprocess.run(["/usr/bin/true"]), _ProcessAttempt),
            "native_helper": _denied(lambda: os.system("/usr/bin/true"), _ProcessAttempt),
        }
    state = action_files[0].parent if action_files else next((root / "uat/private-docx").glob("batch-*"))
    write_probe = state / "fixture-write.tmp"
    write_probe.write_bytes(b"ok")
    write_probe.unlink()
    guarded.update(
        {
            "host_write": _denied(
                lambda: Path("/etc/ao-lore-private-docx-probe").write_bytes(b"x"),
                (OSError, PermissionError),
            ),
            "private_corpus_hidden": not (root / "private-docx/corpus").exists(),
            "extra_mount_hidden": not Path(
                "/", "home", "fixture-operator", "Downloads",
            ).exists(),
            "environment_fixed": set(os.environ)
            == {
                "AO_LORE_HOME", "HOME", "LANG", "LC_ALL", "PATH", "PWD",
                "PYTHONNOUSERSITE", "PYTHONPATH", "XDG_CACHE_HOME",
            },
            "run_state_write": True,
            "native_ctypes_fork": _native_fork_denied(),
            "resource_limits": _resource_limits_exact(),
        }
    )
    print(json.dumps(guarded, sort_keys=True))
    return 0


def _native_fork_denied() -> bool:
    libc = ctypes.CDLL(None, use_errno=True)
    child = libc.fork()
    if child == 0:
        os._exit(77)
    if child > 0:
        os.waitpid(child, 0)
        return False
    return ctypes.get_errno() in {1, 11}


def _resource_limits_exact() -> bool:
    expected = {
        resource.RLIMIT_CPU: 60,
        resource.RLIMIT_AS: 2 * 1024 * 1024 * 1024,
        resource.RLIMIT_DATA: 1024 * 1024 * 1024,
        resource.RLIMIT_FSIZE: 64 * 1024 * 1024,
        resource.RLIMIT_NOFILE: 64,
        resource.RLIMIT_NPROC: 1,
    }
    return all(resource.getrlimit(name) == (value, value) for name, value in expected.items())


if __name__ == "__main__":
    raise SystemExit(main())
