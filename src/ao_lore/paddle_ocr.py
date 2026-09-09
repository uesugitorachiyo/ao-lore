"""Networkless, resource-bounded PaddleOCR worker launch and readback validation."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import signal
import socket
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ._strict_io import ContractError, parse_strict_json, reject_symlink_ancestors
from .ocr_contracts import OCR_CANDIDATES, OCR_POLICY_DIGEST, validate_worker_contract, validate_worker_result
from .ocr_raster import RasterBundle, validate_raster_bundle
from .ocr_runtime import validate_ocr_runtime_tree_digest


OCR_WORKER_CONFIGURATION_DIGEST = "sha256:" + sha256(
    b"ao-lore-paddle-ocr-worker-v0.1|gpu:0|cudnn-deterministic|cpu-deterministic|doc-orientation|textline-orientation|no-fallback|offline|bounded"
).hexdigest()
_MODEL_NAMES = {
    f"candidate-{index}": pair for index, pair in enumerate(OCR_CANDIDATES, 1)
}
_DENIED_SYSCALLS = ("fork", "vfork")
_CLONE_THREAD = 0x00010000
_MAX_RESULT_BYTES = 16 * 1024 * 1024
_NVIDIA_CAP_NAME = re.compile(r"^nvidia-cap[0-9]+$")


@dataclass(frozen=True)
class OcrRuntimeBinding:
    root: Path
    runtime_digest: str
    gpu_identity_digest: str


def _require_digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != 71 or not value.startswith("sha256:") or any(
        character not in "0123456789abcdef" for character in value[7:]
    ):
        raise ContractError(f"{label} must be a lowercase sha256 digest")
    return value


def _candidate_digest(root: Path, candidate_id: str) -> str:
    if candidate_id not in _MODEL_NAMES:
        raise ContractError("OCR candidate identity differs")
    records = []
    for model in _MODEL_NAMES[candidate_id]:
        directory = root / "installed" / "models" / (model + "_infer")
        reject_symlink_ancestors(directory, include_self=True)
        if not directory.is_dir():
            raise ContractError("OCR candidate model is absent")
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(root).as_posix()
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                raise ContractError("OCR candidate model tree differs")
            if stat.S_ISDIR(info.st_mode):
                records.append([relative, "directory", 0, ""])
            elif stat.S_ISREG(info.st_mode):
                digest = sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                records.append([relative, "file", info.st_size, digest.hexdigest()])
            else:
                raise ContractError("OCR candidate model tree differs")
    raw = json.dumps(records, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "sha256:" + sha256(raw).hexdigest()


def _runtime(value: object) -> OcrRuntimeBinding:
    if type(value) is not OcrRuntimeBinding or type(value.root) is not type(Path()):
        raise ContractError("OCR runtime binding must be exact")
    _require_digest(value.runtime_digest, "runtime digest")
    _require_digest(value.gpu_identity_digest, "GPU identity digest")
    root = Path(os.path.abspath(os.fspath(value.root)))
    reject_symlink_ancestors(root, include_self=True)
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ContractError("OCR runtime root differs")
    interpreter = root / "installed" / "bin" / "python"
    python_info = os.lstat(interpreter)
    if not stat.S_ISREG(python_info.st_mode) or stat.S_ISLNK(python_info.st_mode) or python_info.st_nlink != 1 or not python_info.st_mode & 0o111:
        raise ContractError("OCR runtime interpreter differs")
    return OcrRuntimeBinding(root, value.runtime_digest, value.gpu_identity_digest)


def build_ocr_worker_contract(runtime: OcrRuntimeBinding, raster: RasterBundle,
                              candidate_id: str, *, run_id: str) -> dict[str, object]:
    if candidate_id not in _MODEL_NAMES:
        raise ContractError("OCR candidate identity differs")
    checked_runtime = _runtime(runtime)
    validate_ocr_runtime_tree_digest(checked_runtime.root, checked_runtime.runtime_digest)
    checked_raster = validate_raster_bundle(raster)
    model_digest = _candidate_digest(checked_runtime.root, candidate_id)
    value = {
        "schema_version": "ao.lore.private-ocr-worker-contract.v0.1",
        "policy_digest": OCR_POLICY_DIGEST,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "runtime_digest": checked_runtime.runtime_digest,
        "model_set_digest": model_digest,
        "raster_manifest_digest": checked_raster.manifest_digest,
        "configuration_digest": OCR_WORKER_CONFIGURATION_DIGEST,
        "gpu_identity_digest": checked_runtime.gpu_identity_digest,
        "page_ids": [page.page_id for page in checked_raster.pages],
        "network_accessed": False,
        "provider_calls": False,
        "promotion": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
    }
    return validate_worker_contract(value)


def _write_all(descriptor: int, body: bytes) -> None:
    view = memoryview(body)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("OCR launch short write")
        view = view[written:]


def _sealed_descriptor(name: str, body: bytes) -> int:
    descriptor = os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        _write_all(descriptor, body)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        if fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) != seals:
            raise OSError("OCR launch seal differs")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class _ScmpArgCmp(ctypes.Structure):
    _fields_ = (("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64))


def _seccomp_descriptor() -> int:
    descriptor = os.memfd_create("ao-lore-paddle-seccomp", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = (ctypes.c_uint32,)
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_rule_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint)
    library.seccomp_rule_add.restype = ctypes.c_int
    library.seccomp_rule_add_array.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_ScmpArgCmp))
    library.seccomp_rule_add_array.restype = ctypes.c_int
    library.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_export_bpf.argtypes = (ctypes.c_void_p, ctypes.c_int)
    library.seccomp_export_bpf.restype = ctypes.c_int
    library.seccomp_release.argtypes = (ctypes.c_void_p,)
    context = library.seccomp_init(0x7FFF0000)
    if not context:
        os.close(descriptor)
        raise OSError("OCR seccomp initialization failed")
    try:
        action = 0x00050000 | errno.EPERM
        for name in _DENIED_SYSCALLS:
            number = library.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number < 0 or library.seccomp_rule_add(context, action, number, 0) != 0:
                raise OSError("OCR seccomp rule failed")
        clone3_number = library.seccomp_syscall_resolve_name(b"clone3")
        if clone3_number < 0 or library.seccomp_rule_add(context, 0x00050000 | errno.ENOSYS, clone3_number, 0) != 0:
            raise OSError("OCR clone3 seccomp rule failed")
        clone_number = library.seccomp_syscall_resolve_name(b"clone")
        comparison = _ScmpArgCmp(0, 7, _CLONE_THREAD, 0)
        if clone_number < 0 or library.seccomp_rule_add_array(context, action, clone_number, 1, ctypes.byref(comparison)) != 0:
            raise OSError("OCR clone seccomp rule failed")
        if library.seccomp_export_bpf(context, descriptor) != 0:
            raise OSError("OCR seccomp export failed")
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    finally:
        library.seccomp_release(context)


def _run_bounded(command: tuple[str, ...], descriptors: tuple[int, ...]) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        command, cwd="/", env={}, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=descriptors,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=240)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise ContractError("Paddle OCR worker timed out") from exc
    if process.returncode != 0 or len(stdout) > 4096 or len(stderr) > 65536:
        raise ContractError("Paddle OCR worker failed")
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def validate_ocr_worker_readback(value: object) -> dict[str, object]:
    return validate_worker_result(value)


def _append_nvidia_devices(command: list[str]) -> None:
    for device in ("nvidia0", "nvidiactl", "nvidia-modeset", "nvidia-uvm", "nvidia-uvm-tools"):
        path = Path("/dev") / device
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISCHR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ContractError("Paddle OCR GPU device differs")
        command.extend(("--dev-bind", str(path), str(path)))
    caps = Path("/dev/nvidia-caps")
    try:
        caps_info = os.lstat(caps)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(caps_info.st_mode) or stat.S_ISLNK(caps_info.st_mode):
        raise ContractError("Paddle OCR GPU device differs")
    command.extend(("--dir", "/dev/nvidia-caps"))
    for path in sorted(caps.iterdir()):
        if _NVIDIA_CAP_NAME.fullmatch(path.name) is None:
            raise ContractError("Paddle OCR GPU device differs")
        info = os.lstat(path)
        if not stat.S_ISCHR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ContractError("Paddle OCR GPU device differs")
        command.extend(("--dev-bind", str(path), str(path)))


def run_paddle_ocr_sandbox_probe(runtime_root: Path) -> dict[str, bool]:
    runtime = Path(os.path.abspath(os.fspath(runtime_root)))
    reject_symlink_ancestors(runtime, include_self=True)
    seccomp_fd = _seccomp_descriptor()
    repository = Path(__file__).resolve().parents[2]
    try:
        with tempfile.TemporaryDirectory(prefix="ao-lore-paddle-probe-") as temporary:
            result_root = Path(temporary)
            command = [
                "/usr/bin/bwrap", "--die-with-parent", "--new-session", "--unshare-net",
                "--unshare-ipc", "--unshare-pid", "--unshare-uts", "--tmpfs", "/",
                "--seccomp", str(seccomp_fd), "--ro-bind", "/usr", "/usr",
                "--ro-bind", "/bin", "/bin", "--ro-bind", "/lib", "/lib",
                "--ro-bind", "/lib64", "/lib64", "--ro-bind", str(runtime), "/runtime",
                "--ro-bind", str(repository / "tests"), "/app/tests",
                "--bind", str(result_root), "/result", "--dev", "/dev",
            ]
            _append_nvidia_devices(command)
            command.extend((
                "--proc", "/proc", "--tmpfs", "/tmp", "--clearenv",
                "--setenv", "HOME", "/tmp", "--setenv", "LANG", "C.UTF-8",
                "--setenv", "PYTHONPATH", "/app", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "/usr/bin/prlimit", "--cpu=180:180", "--as=34359738368:34359738368",
                "--data=17179869184:17179869184", "--fsize=67108864:67108864",
                "--nofile=128:128", "--nproc=256:256", "--core=0:0", "--",
                "/runtime/installed/bin/python", "-m", "tests.paddle_ocr_worker_fixture",
            ))
            _run_bounded(tuple(command), (seccomp_fd,))
            path = result_root / "probe.json"
            entries = list(result_root.iterdir())
            if entries != [path]:
                raise ContractError("Paddle OCR sandbox probe entries differ")
            value = parse_strict_json(path.read_bytes(), "Paddle OCR sandbox proof")
            expected = {
                "tcp_denied", "udp_sendto_denied", "udp_sendmsg_denied", "dns_denied",
                "unix_denied", "fork_denied", "vfork_denied", "clone_denied",
                "clone3_denied", "system_denied", "outside_write_denied",
                "result_write_allowed", "limits_exact", "network_namespace_unshared",
            }
            if type(value) is not dict or set(value) != expected or any(type(item) is not bool for item in value.values()):
                raise ContractError("Paddle OCR sandbox proof differs")
            return value
    finally:
        os.close(seccomp_fd)


def _verify_parent_network_isolation() -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            probe.sendto(b"x", ("127.0.0.1", 9))
        except OSError as exc:
            if exc.errno != errno.EPERM:
                raise ContractError("OCR parent network isolation differs") from exc
        else:
            raise ContractError("OCR parent network isolation differs")
    finally:
        probe.close()


def run_paddle_ocr_worker(runtime: OcrRuntimeBinding, raster: RasterBundle,
                          candidate_id: str, *, run_id: str,
                          parent_network_isolated: bool = False) -> dict[str, object]:
    if type(parent_network_isolated) is not bool:
        raise ContractError("OCR parent network isolation flag differs")
    if parent_network_isolated:
        _verify_parent_network_isolation()
    checked_runtime = _runtime(runtime)
    checked_raster = validate_raster_bundle(raster)
    contract = build_ocr_worker_contract(checked_runtime, checked_raster, candidate_id, run_id=run_id)
    raw_contract = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("ascii")
    contract_fd = _sealed_descriptor("ao-lore-paddle-contract", raw_contract)
    seccomp_fd = _seccomp_descriptor()
    manifest_body = (checked_raster.root / "manifest.json").read_bytes()
    if "sha256:" + sha256(manifest_body).hexdigest() != checked_raster.manifest_digest:
        os.close(contract_fd)
        os.close(seccomp_fd)
        raise ContractError("Paddle OCR raster manifest drifted")
    manifest_fd = _sealed_descriptor("ao-lore-paddle-raster-manifest", manifest_body)
    page_descriptors: list[tuple[str, int]] = []
    try:
        for page in checked_raster.pages:
            body = page.path.read_bytes()
            if len(body) != page.size or "sha256:" + sha256(body).hexdigest() != page.digest:
                raise ContractError("Paddle OCR raster page drifted")
            page_descriptors.append((page.page_id, _sealed_descriptor("ao-lore-paddle-page", body)))
    except BaseException:
        os.close(contract_fd)
        os.close(seccomp_fd)
        os.close(manifest_fd)
        for _, descriptor in page_descriptors:
            os.close(descriptor)
        raise
    repository = Path(__file__).resolve().parents[2]
    try:
        with tempfile.TemporaryDirectory(prefix="ao-lore-paddle-result-") as temporary:
            result_root = Path(temporary)
            command = [
                "/usr/bin/bwrap", "--die-with-parent", "--new-session",
            ]
            if not parent_network_isolated:
                command.append("--unshare-net")
            command.extend([
                "--unshare-ipc", "--unshare-pid", "--unshare-uts", "--tmpfs", "/",
                "--dir", "/launch", "--ro-bind-data", str(contract_fd), "/launch/worker-contract.json",
                "--seccomp", str(seccomp_fd), "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
                "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
                "--ro-bind", str(checked_runtime.root), "/runtime", "--dir", "/pages",
                "--file", str(manifest_fd), "/pages/manifest.json",
                "--ro-bind", str(repository / "src"), "/app/src",
                "--bind", str(result_root), "/result", "--dev", "/dev",
            ])
            for page_id, descriptor in page_descriptors:
                command.extend(("--file", str(descriptor), f"/pages/{page_id}.png"))
            _append_nvidia_devices(command)
            command.extend((
                "--proc", "/proc", "--tmpfs", "/tmp", "--clearenv",
                "--setenv", "HOME", "/tmp", "--setenv", "XDG_CACHE_HOME", "/tmp/cache",
                "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONPATH", "/app/src",
                "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--setenv", "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True",
                "--setenv", "HF_HUB_OFFLINE", "1", "--setenv", "TRANSFORMERS_OFFLINE", "1",
                "--setenv", "FLAGS_cudnn_deterministic", "True",
                "--setenv", "FLAGS_cpu_deterministic", "True",
                "--setenv", "OPENBLAS_NUM_THREADS", "1", "--setenv", "OMP_NUM_THREADS", "1",
                "--setenv", "MKL_NUM_THREADS", "1", "--setenv", "NUMEXPR_NUM_THREADS", "1",
                "/usr/bin/prlimit", "--cpu=180:180", "--as=34359738368:34359738368",
                "--data=17179869184:17179869184", "--fsize=67108864:67108864",
                "--nofile=128:128", "--nproc=256:256", "--core=0:0", "--",
                "/runtime/installed/bin/python", "-m", "ao_lore.paddle_ocr_worker",
            ))
            descriptors = (contract_fd, seccomp_fd, manifest_fd, *(descriptor for _, descriptor in page_descriptors))
            _run_bounded(tuple(command), descriptors)
            entries = list(result_root.iterdir())
            if len(entries) != 1 or entries[0].name != "worker-result.json":
                raise ContractError("Paddle OCR worker result entries differ")
            info = os.lstat(entries[0])
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or not 1 <= info.st_size <= _MAX_RESULT_BYTES:
                raise ContractError("Paddle OCR worker result file differs")
            raw = entries[0].read_bytes()
            result = validate_worker_result(parse_strict_json(raw, "Paddle OCR worker result"))
            if (
                result["run_id"] != contract["run_id"]
                or result["candidate_id"] != contract["candidate_id"]
                or result["runtime_digest"] != contract["runtime_digest"]
                or result["model_set_digest"] != contract["model_set_digest"]
                or result["configuration_digest"] != contract["configuration_digest"]
                or [page["page_id"] for page in result["pages"]] != contract["page_ids"]
            ):
                raise ContractError("Paddle OCR worker result binding differs")
            if raw != json.dumps(result, sort_keys=True, separators=(",", ":")).encode("ascii"):
                raise ContractError("Paddle OCR worker result bytes differ")
            return result
    finally:
        os.close(contract_fd)
        os.close(seccomp_fd)
        os.close(manifest_fd)
        for _, descriptor in page_descriptors:
            os.close(descriptor)
