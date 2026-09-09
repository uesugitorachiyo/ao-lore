"""Adversarial process used only by the actual Paddle OCR sandbox test."""

import ctypes
import errno
import json
import os
import resource
import socket
from pathlib import Path


def denied(call):
    try:
        value = call()
        return value in (-1, None) and ctypes.get_errno() in (errno.EPERM, errno.ENOSYS)
    except BaseException:
        return True


def main():
    libc = ctypes.CDLL(None, use_errno=True)
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp.settimeout(0.2)
    tcp_denied = denied(lambda: tcp.connect(("1.1.1.1", 80)))
    tcp.close()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sendto_denied = denied(lambda: udp.sendto(b"x", ("1.1.1.1", 53)))
    udp_sendmsg_denied = denied(lambda: udp.sendmsg([b"x"], [], 0, ("1.1.1.1", 53)))
    udp.close()
    unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_denied = denied(lambda: unix.connect("/run/host-control.sock"))
    unix.close()
    fork_denied = denied(os.fork)
    vfork_denied = denied(libc.vfork)
    clone_denied = denied(lambda: libc.syscall(56, 17, 0, 0, 0, 0))
    clone3_denied = denied(lambda: libc.syscall(435, 0, 0))
    system_denied = libc.system(b"true") != 0
    outside_write_denied = denied(lambda: Path("/usr/ao-lore-forbidden").write_text("x"))
    limits = {
        "cpu": resource.getrlimit(resource.RLIMIT_CPU),
        "as": resource.getrlimit(resource.RLIMIT_AS),
        "data": resource.getrlimit(resource.RLIMIT_DATA),
        "fsize": resource.getrlimit(resource.RLIMIT_FSIZE),
        "nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
        "nproc": resource.getrlimit(resource.RLIMIT_NPROC),
        "core": resource.getrlimit(resource.RLIMIT_CORE),
    }
    exact = {
        "cpu": (180, 180), "as": (34359738368, 34359738368),
        "data": (17179869184, 17179869184), "fsize": (67108864, 67108864),
        "nofile": (128, 128), "nproc": (256, 256), "core": (0, 0),
    }
    body = {
        "tcp_denied": tcp_denied,
        "udp_sendto_denied": udp_sendto_denied,
        "udp_sendmsg_denied": udp_sendmsg_denied,
        "dns_denied": denied(lambda: socket.getaddrinfo("example.com", 80)),
        "unix_denied": unix_denied,
        "fork_denied": fork_denied,
        "vfork_denied": vfork_denied,
        "clone_denied": clone_denied,
        "clone3_denied": clone3_denied,
        "system_denied": system_denied,
        "outside_write_denied": outside_write_denied,
        "result_write_allowed": True,
        "limits_exact": limits == exact,
        "network_namespace_unshared": not Path("/etc/resolv.conf").exists(),
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii")
    descriptor = os.open("/result/probe.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    main()
