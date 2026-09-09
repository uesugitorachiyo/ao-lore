"""Owned, no-replace preparation of the sealed Paddle OCR runtime inputs."""

from __future__ import annotations

import json
import os
import stat
import ctypes
import errno
import re
import subprocess
import tarfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlsplit

from ._strict_io import ContractError, ensure_contained, parse_strict_json, reject_symlink_ancestors
from .home import repository_root
from .ocr_contracts import OCR_CANDIDATES


_FINAL_NAME = "ocr-runtime-v0.1"
_PARTIAL_NAME = ".ocr-runtime-v0.1.partial"
_MAX_FILES = 100000
_MAX_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
_MAX_DEPTH = 12
_MAX_MODEL_ARCHIVE_MEMBERS = 256
_MAX_MODEL_EXPANDED_BYTES = 4 * 1024 * 1024 * 1024
_MODEL_PATHS = (
    "models/PP-LCNet_x1_0_doc_ori_infer.tar",
    "models/PP-LCNet_x1_0_textline_ori_infer.tar",
    "models/PP-OCRv5_mobile_det_infer.tar",
    "models/PP-OCRv6_medium_det_infer.tar",
    "models/PP-OCRv6_medium_rec_infer.tar",
    "models/PP-OCRv6_small_det_infer.tar",
    "models/PP-OCRv6_small_rec_infer.tar",
    "models/en_PP-OCRv5_mobile_rec_infer.tar",
)
_REQUIRED_PACKAGE_PREFIXES = (
    "paddleocr-3.7.0-",
    "paddlex-3.7.0-",
    "paddlepaddle_gpu-3.3.0-",
)
_PACKAGE_VERSIONS = (
    "paddleocr==3.7.0", "paddlex==3.7.0", "paddlepaddle-gpu==3.3.0",
)
_WHEEL_NAME = re.compile(r"^[A-Za-z0-9_.]+-[A-Za-z0-9_.+]+-[A-Za-z0-9_.-]+\.whl$")
_MIN_ARTIFACTS = len(_MODEL_PATHS) + len(_REQUIRED_PACKAGE_PREFIXES)
_MAX_ARTIFACTS = 256
_EXECUTABLE_PATHS = frozenset((
    "installed/bin/python", "installed/bin/python3", "installed/bin/python3.12",
))
_PACKAGE_HOSTS = frozenset(("files.pythonhosted.org", "paddle-whl.bj.bcebos.com"))
_MODEL_HOSTS = frozenset(("paddle-model-ecology.bj.bcebos.com",))
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_RENAME_NOREPLACE = 1
_NVIDIA_DEVICE_NAMES = (
    "nvidia0", "nvidiactl", "nvidia-modeset", "nvidia-uvm", "nvidia-uvm-tools",
)
_NVIDIA_CAP_NAME = re.compile(r"^nvidia-cap[0-9]+$")


@dataclass(frozen=True)
class OfficialArtifact:
    relative_path: str
    role: str
    origin: str
    size: int
    digest: str


@dataclass(frozen=True)
class GpuIdentity:
    name: str
    compute_capability: str
    driver_version: str
    device_uuid_digest: str
    cuda_version: str
    paddle_check_passed: bool
    gpu_execution_observed: bool
    cpu_fallback_observed: bool
    telemetry_observed: bool


@dataclass(frozen=True)
class PreparedOcrRuntime:
    root: Path
    root_identity: tuple[int, int]
    runtime_digest: str
    file_count: int
    total_bytes: int
    gpu_identity: GpuIdentity
    tree_manifest: tuple["RuntimeTreeEntry", ...]
    artifact_manifest_digest: str
    model_inventory_digest: str
    python_version: str
    package_versions: tuple[str, ...]
    cuda_version: str
    candidates: tuple[tuple[str, str], ...]
    artifacts: tuple[OfficialArtifact, ...]


@dataclass(frozen=True)
class RuntimeTreeEntry:
    path: str
    size: int
    digest: str


CompatibilityProbe = Callable[[Path], GpuIdentity]
RuntimeInstaller = Callable[[Path], None]
CommandRunner = Callable[..., object]


def _rename_noreplace_at(parent_descriptor: int, source_name: str, destination_name: str) -> None:
    """Atomically publish one directory without replacing another binding."""

    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_descriptor, os.fsencode(source_name), parent_descriptor,
        os.fsencode(destination_name), _RENAME_NOREPLACE,
    )
    if result != 0:
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def _require_exact_text(value: object, label: str, maximum: int = 512) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ContractError(f"{label} must be bounded exact text")
    if any(ord(character) < 32 or ord(character) > 126 for character in value):
        raise ContractError(f"{label} must be printable ASCII")
    return value


def _require_digest(value: object, label: str) -> str:
    text = _require_exact_text(value, label, 71)
    if len(text) != 71 or not text.startswith("sha256:") or any(
        character not in "0123456789abcdef" for character in text[7:]
    ):
        raise ContractError(f"{label} must be a sha256 digest")
    return text


def _validate_relative_path(value: object) -> str:
    text = _require_exact_text(value, "artifact relative path", 256)
    pure = PurePosixPath(text)
    if (
        pure.is_absolute()
        or not pure.parts
        or any(part in ("", ".", "..") for part in pure.parts)
        or pure.as_posix() != text
        or "\\" in text
        or len(pure.parts) > _MAX_DEPTH
    ):
        raise ContractError("artifact relative path is unsafe")
    return text


def _validate_origin(origin: object, role: str) -> str:
    text = _require_exact_text(origin, "artifact origin", 1024)
    parsed = urlsplit(text)
    allowed = _PACKAGE_HOSTS if role == "package" else _MODEL_HOSTS
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "//" in parsed.path
        or any(part in (".", "..") for part in PurePosixPath(parsed.path).parts)
    ):
        raise ContractError("artifact origin is not an approved official origin")
    return text


def _validate_artifacts(artifacts: object) -> tuple[OfficialArtifact, ...]:
    if type(artifacts) is not tuple or not _MIN_ARTIFACTS <= len(artifacts) <= _MAX_ARTIFACTS:
        raise ContractError("OCR artifact set differs")
    output = []
    seen = set()
    for artifact in artifacts:
        if type(artifact) is not OfficialArtifact:
            raise ContractError("OCR artifact identity must be exact")
        path = _validate_relative_path(artifact.relative_path)
        if path in seen:
            raise ContractError("duplicate OCR artifact path")
        seen.add(path)
        role = _require_exact_text(artifact.role, "artifact role", 16)
        expected_role = "package" if path.startswith("packages/") else "model"
        if role != expected_role:
            raise ContractError("OCR artifact role differs")
        _validate_origin(artifact.origin, role)
        if type(artifact.size) is not int or not 1 <= artifact.size <= _MAX_TOTAL_BYTES:
            raise ContractError("OCR artifact size is invalid")
        _require_digest(artifact.digest, "artifact digest")
        output.append(artifact)
    model_paths = tuple(sorted(path for path in seen if path.startswith("models/")))
    package_paths = tuple(sorted(path for path in seen if path.startswith("packages/")))
    if model_paths != _MODEL_PATHS or len(package_paths) != len(seen) - len(model_paths):
        raise ContractError("OCR model artifact paths differ")
    package_names = tuple(PurePosixPath(path).name for path in package_paths)
    if any(not _WHEEL_NAME.fullmatch(name) for name in package_names):
        raise ContractError("OCR package artifact filename differs")
    if any(sum(name.startswith(prefix) for name in package_names) != 1 for prefix in _REQUIRED_PACKAGE_PREFIXES):
        raise ContractError("required OCR package artifact differs")
    return tuple(sorted(output, key=lambda item: item.relative_path))


def _artifact_digest(artifacts: tuple[OfficialArtifact, ...], *, role: str | None = None) -> str:
    selected = [artifact for artifact in artifacts if role is None or artifact.role == role]
    value = {
        "schema_version": "ao.lore.ocr-artifact-lock.v0.1",
        "artifacts": [{
            "path": artifact.relative_path, "role": artifact.role,
            "origin": artifact.origin, "size": artifact.size, "digest": artifact.digest,
        } for artifact in selected],
    }
    body = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return "sha256:" + sha256(body).hexdigest()


def _copy_verified_source(
    root: Path, artifact: OfficialArtifact, destination: Path | None = None,
) -> int:
    path = root.joinpath(*PurePosixPath(artifact.relative_path).parts)
    reject_symlink_ancestors(path)
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1:
        raise ContractError("OCR artifact must be one regular single-link file")
    if before.st_size != artifact.size:
        raise ContractError("OCR artifact size drifted")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    output = None
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino, opened.st_size) != (
            before.st_dev, before.st_ino, before.st_size
        ) or opened.st_nlink != 1:
            raise ContractError("OCR artifact identity drifted")
        if destination is not None:
            output = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        digest = sha256()
        total = 0
        remaining = artifact.size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if output is not None:
                view = memoryview(chunk)
                while view:
                    written = os.write(output, view)
                    if written <= 0:
                        raise ContractError("OCR runtime copy short write")
                    view = view[written:]
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if output is not None:
            os.fchmod(output, 0o444)
            os.fsync(output)
    finally:
        if output is not None:
            os.close(output)
        os.close(descriptor)
    public_after = os.lstat(path)
    if (
        total != artifact.size
        or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
        or (public_after.st_dev, public_after.st_ino, public_after.st_size)
        != (opened.st_dev, opened.st_ino, opened.st_size)
        or "sha256:" + digest.hexdigest() != artifact.digest
    ):
        raise ContractError("OCR artifact bytes drifted")
    return total


def _hash_bound_file(path: Path, info: os.stat_result) -> tuple[int, str]:
    reject_symlink_ancestors(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size)
        ):
            raise ContractError("prepared OCR runtime file identity drifted")
        digest = sha256()
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > _MAX_TOTAL_BYTES:
                raise ContractError("prepared OCR runtime file exceeds bound")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    rebound = os.lstat(path)
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if total != info.st_size or any(getattr(item, field) != getattr(info, field) for item in (opened, after, rebound) for field in stable):
        raise ContractError("prepared OCR runtime file drifted during scan")
    return total, "sha256:" + digest.hexdigest()


def _tree_digest(entries: tuple[RuntimeTreeEntry, ...]) -> str:
    value = {"schema_version": "ao.lore.ocr-runtime-tree.v0.1", "files": [
        {"path": item.path, "size": item.size, "digest": item.digest} for item in entries
    ]}
    body = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    return "sha256:" + sha256(body).hexdigest()


def _sealed_file_mode(relative: str) -> int:
    return 0o555 if relative in _EXECUTABLE_PATHS else 0o444


def _scan_runtime_tree(root: Path, *, require_sealed: bool,
                       artifact_paths: frozenset[str]) -> tuple[RuntimeTreeEntry, ...]:
    entries: list[RuntimeTreeEntry] = []
    total = 0
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        relative = path.relative_to(root).as_posix()
        if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            if require_sealed and stat.S_IMODE(info.st_mode) != 0o555:
                raise ContractError("prepared OCR runtime directory mode drifted")
            continue
        if (
            not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1
            or require_sealed and stat.S_IMODE(info.st_mode) != _sealed_file_mode(relative)
        ):
            raise ContractError("prepared OCR runtime entry is invalid")
        if relative not in artifact_paths and not relative.startswith("installed/"):
            raise ContractError("prepared OCR runtime has an unknown file")
        size, digest = _hash_bound_file(path, info)
        total += size
        entries.append(RuntimeTreeEntry(relative, size, digest))
        if len(entries) > _MAX_FILES or total > _MAX_TOTAL_BYTES:
            raise ContractError("prepared OCR runtime tree exceeds bound")
    if not any(entry.path == "installed/bin/python" for entry in entries):
        raise ContractError("installed OCR runtime entry point is absent")
    return tuple(entries)


def _mkdir_path(root: Path, relative: str) -> None:
    current = root
    for part in PurePosixPath(relative).parts[:-1]:
        current = current / part
        try:
            os.mkdir(current, 0o700)
        except FileExistsError:
            info = os.lstat(current)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise ContractError("OCR runtime directory collision")


def extract_locked_ocr_models(runtime_root: Path) -> None:
    """Expand only the eight closed official model archives without link handling."""

    root = Path(runtime_root)
    models = root / "installed" / "models"
    try:
        os.mkdir(models, 0o700)
    except FileExistsError as exc:
        raise ContractError("installed OCR model directory collision") from exc
    expanded_total = 0
    for relative in _MODEL_PATHS:
        archive_path = root.joinpath(*PurePosixPath(relative).parts)
        archive_name = PurePosixPath(relative).name
        expected_root = archive_name.removesuffix(".tar")
        try:
            with tarfile.open(archive_path, mode="r:*") as archive:
                members = archive.getmembers()
                if not 2 <= len(members) <= _MAX_MODEL_ARCHIVE_MEMBERS:
                    raise ContractError("OCR model archive member count differs")
                if members[0].name.rstrip("/") != expected_root or not members[0].isdir():
                    raise ContractError("OCR model archive root differs")
                regular_count = 0
                seen_names: set[str] = set()
                for member in members:
                    name = member.name.rstrip("/")
                    pure = PurePosixPath(name)
                    if (
                        pure.is_absolute() or not pure.parts
                        or pure.parts[0] != expected_root
                        or any(part in ("", ".", "..") for part in pure.parts)
                        or pure.as_posix() != name or "\\" in name
                        or len(pure.parts) > _MAX_DEPTH
                    ):
                        raise ContractError("OCR model archive path is unsafe")
                    if name in seen_names:
                        raise ContractError("OCR model archive path is duplicated")
                    seen_names.add(name)
                    destination = models.joinpath(*pure.parts)
                    if member.isdir():
                        try:
                            os.mkdir(destination, 0o700)
                        except FileExistsError:
                            info = os.lstat(destination)
                            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                                raise ContractError("OCR model archive directory collision")
                        continue
                    if not member.isreg() or member.size < 0:
                        raise ContractError("OCR model archive entry is not a regular file")
                    regular_count += 1
                    expanded_total += member.size
                    if expanded_total > _MAX_MODEL_EXPANDED_BYTES:
                        raise ContractError("OCR model archive bytes exceed bound")
                    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    descriptor = os.open(
                        destination,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                    try:
                        source = archive.extractfile(member)
                        if source is None:
                            raise ContractError("OCR model archive file is absent")
                        remaining = member.size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ContractError("OCR model archive file is truncated")
                            view = memoryview(chunk)
                            while view:
                                written = os.write(descriptor, view)
                                if written <= 0:
                                    raise ContractError("OCR model extraction short write")
                                view = view[written:]
                            remaining -= len(chunk)
                        if source.read(1):
                            raise ContractError("OCR model archive file exceeds declared size")
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
                if regular_count == 0:
                    raise ContractError("OCR model archive has no model files")
        except (tarfile.TarError, OSError) as exc:
            if isinstance(exc, ContractError):
                raise
            raise ContractError("OCR model archive is invalid") from exc


def install_offline_ocr_runtime(
    runtime_root: Path, *, command_runner: CommandRunner = subprocess.run,
) -> None:
    """Install the locked wheelhouse and model archives with network resolution disabled."""

    root = Path(runtime_root)
    packages = root / "packages"
    gpu_wheels = tuple(packages.glob("paddlepaddle_gpu-3.3.0-*.whl"))
    if len(gpu_wheels) != 1:
        raise ContractError("locked Paddle GPU wheel differs")
    installed = root / "installed"
    environment = {
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/bin:/bin",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_NO_INDEX": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
    }
    common = {
        "check": True,
        "cwd": "/",
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 900,
    }
    commands = (
        ("/usr/bin/python3", "-m", "venv", "--copies", "--without-pip", str(installed)),
        (
            "/usr/bin/python3", "-m", "pip", "--python", str(installed / "bin" / "python"),
            "install", "--no-index", "--no-cache-dir", "--no-compile",
            "--find-links", str(packages), "paddleocr==3.7.0", "paddlex[ocr-core]==3.7.0",
            str(gpu_wheels[0]),
        ),
        (
            "/usr/bin/python3", "-m", "pip", "--python", str(installed / "bin" / "python"),
            "check",
        ),
    )
    try:
        for command in commands:
            command_runner(command, **common)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise ContractError("offline OCR runtime installation failed") from exc
    lib64 = installed / "lib64"
    try:
        info = os.lstat(lib64)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISLNK(info.st_mode) or os.readlink(lib64) != "lib":
            raise ContractError("offline OCR runtime library alias differs")
        os.unlink(lib64)
    bin_root = installed / "bin"
    for entry in tuple(bin_root.iterdir()):
        if entry.name not in ("python", "python3", "python3.12"):
            info = os.lstat(entry)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                raise ContractError("offline OCR entry point is invalid")
            os.unlink(entry)
    for cache in sorted(installed.rglob("__pycache__"), key=lambda item: len(item.parts), reverse=True):
        for entry in cache.iterdir():
            info = os.lstat(entry)
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                raise ContractError("offline OCR bytecode cache is invalid")
            os.unlink(entry)
        os.rmdir(cache)
    extract_locked_ocr_models(root)


_GPU_PROBE_SCRIPT = r'''import contextlib
import importlib.metadata
import io
import json
import subprocess
import sys
import paddle

captured = io.StringIO()
with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
    paddle.utils.run_check()
paddle.set_device("gpu:0")
value = paddle.to_tensor([1.0, 2.0], place=paddle.CUDAPlace(0))
observed = bool((value * 2).numpy().tolist() == [2.0, 4.0])
identity = subprocess.run(
    ["/usr/bin/nvidia-smi", "--query-gpu=name,compute_cap,driver_version,uuid", "--format=csv,noheader,nounits", "--id=0"],
    check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
).stdout.decode("utf-8").strip().split(", ")
if len(identity) != 4:
    raise RuntimeError("gpu identity differs")
print(json.dumps({
    "name": identity[0], "compute_capability": identity[1],
    "driver_version": identity[2], "device_uuid": identity[3],
    "cuda_version": str(paddle.version.cuda()),
    "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
    "paddleocr_version": importlib.metadata.version("paddleocr"),
    "paddlex_version": importlib.metadata.version("paddlex"),
    "paddlepaddle_gpu_version": importlib.metadata.version("paddlepaddle-gpu"),
    "paddle_check_passed": True, "gpu_execution_observed": observed,
    "cpu_fallback_observed": "gpu" not in str(value.place).lower(),
}, sort_keys=True, separators=(",", ":")))'''


def _append_nvidia_devices(command: list[str]) -> None:
    for name in _NVIDIA_DEVICE_NAMES:
        path = Path("/dev") / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        if not stat.S_ISCHR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ContractError("offline OCR GPU device differs")
        command.extend(("--dev-bind", str(path), str(path)))
    caps = Path("/dev/nvidia-caps")
    try:
        caps_info = os.lstat(caps)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(caps_info.st_mode) or stat.S_ISLNK(caps_info.st_mode):
        raise ContractError("offline OCR GPU device differs")
    command.extend(("--dir", str(caps)))
    for path in sorted(caps.iterdir()):
        if _NVIDIA_CAP_NAME.fullmatch(path.name) is None:
            raise ContractError("offline OCR GPU device differs")
        info = os.lstat(path)
        if not stat.S_ISCHR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ContractError("offline OCR GPU device differs")
        command.extend(("--dev-bind", str(path), str(path)))


def probe_offline_gpu_runtime(
    runtime_root: Path, *, command_runner: CommandRunner = subprocess.run,
    parent_network_isolated: bool = False,
) -> GpuIdentity:
    """Prove the pinned runtime executes on the expected GPU in a network namespace."""

    if type(parent_network_isolated) is not bool:
        raise ContractError("offline OCR GPU isolation binding differs")
    root = Path(os.path.abspath(os.fspath(runtime_root)))
    reject_symlink_ancestors(root, include_self=True)
    command = [
        "/usr/bin/bwrap", "--die-with-parent", "--new-session",
    ]
    if not parent_network_isolated:
        command.append("--unshare-net")
    command.extend([
        "--unshare-ipc", "--unshare-pid", "--unshare-uts", "--tmpfs", "/",
        "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
        "--ro-bind", "/lib", "/lib",
        "--ro-bind", "/lib64", "/lib64", "--ro-bind", str(root), "/runtime",
        "--dev", "/dev",
    ])
    _append_nvidia_devices(command)
    command.extend((
        "--proc", "/proc", "--tmpfs", "/tmp", "--clearenv",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True",
        "--setenv", "PADDLE_PDX_MODEL_SOURCE", "bos",
        "--setenv", "HF_HUB_OFFLINE", "1",
        "--setenv", "TRANSFORMERS_OFFLINE", "1",
        "/runtime/installed/bin/python", "-c", _GPU_PROBE_SCRIPT,
    ))
    try:
        completed = command_runner(
            tuple(command), check=False, cwd="/", env={}, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
        )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise ContractError("offline OCR GPU probe failed") from exc
    stdout = getattr(completed, "stdout", None)
    stderr = getattr(completed, "stderr", None)
    if (
        type(getattr(completed, "returncode", None)) is not int
        or completed.returncode != 0 or type(stdout) is not bytes or len(stdout) > 4096
        or type(stderr) is not bytes or len(stderr) > 65536
    ):
        raise ContractError("offline OCR GPU probe failed")
    value = parse_strict_json(stdout, "OCR GPU probe")
    expected = {
        "name", "compute_capability", "driver_version", "device_uuid", "cuda_version",
        "python_version", "paddleocr_version", "paddlex_version",
        "paddlepaddle_gpu_version", "paddle_check_passed",
        "gpu_execution_observed", "cpu_fallback_observed",
    }
    if set(value) != expected:
        raise ContractError("offline OCR GPU probe fields differ")
    constants = {
        "name": "NVIDIA GeForce RTX 4070 SUPER", "compute_capability": "8.9",
        "cuda_version": "12.6", "python_version": "3.12",
        "paddleocr_version": "3.7.0", "paddlex_version": "3.7.0",
        "paddlepaddle_gpu_version": "3.3.0",
    }
    for field, expected_value in constants.items():
        if value[field] != expected_value:
            raise ContractError("offline OCR GPU identity differs")
    for field in ("driver_version", "device_uuid"):
        _require_exact_text(value[field], field, 128)
    if (
        value["paddle_check_passed"] is not True
        or value["gpu_execution_observed"] is not True
        or value["cpu_fallback_observed"] is not False
    ):
        raise ContractError("offline OCR GPU execution differs")
    return GpuIdentity(
        name=value["name"], compute_capability=value["compute_capability"],
        driver_version=value["driver_version"],
        device_uuid_digest="sha256:" + sha256(value["device_uuid"].encode("ascii")).hexdigest(),
        cuda_version=value["cuda_version"], paddle_check_passed=True,
        gpu_execution_observed=True, cpu_fallback_observed=False,
        telemetry_observed=False,
    )


def _remove_owned_tree(path: Path, identity: tuple[int, int]) -> bool:
    try:
        public = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(public.st_mode) or stat.S_ISLNK(public.st_mode) or (public.st_dev, public.st_ino) != identity:
        return False
    descriptor = os.open(path, _DIRECTORY_FLAGS)
    try:
        if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != identity:
            return False
        os.fchmod(descriptor, 0o700)
        for name in os.listdir(descriptor):
            child = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            child_path = path / name
            if stat.S_ISDIR(child.st_mode) and not stat.S_ISLNK(child.st_mode):
                if not _remove_owned_tree(child_path, (child.st_dev, child.st_ino)):
                    raise ContractError("owned OCR cleanup drifted")
            elif stat.S_ISREG(child.st_mode) and not stat.S_ISLNK(child.st_mode) and child.st_nlink == 1:
                rebound = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (rebound.st_dev, rebound.st_ino) != (child.st_dev, child.st_ino):
                    raise ContractError("owned OCR cleanup drifted")
                os.unlink(name, dir_fd=descriptor)
            else:
                raise ContractError("owned OCR cleanup found an invalid entry")
    finally:
        os.close(descriptor)
    rebound = os.lstat(path)
    if (rebound.st_dev, rebound.st_ino) != identity:
        return False
    os.rmdir(path)
    return True


def _scan_final(root: Path, artifacts: tuple[OfficialArtifact, ...]) -> tuple[tuple[int, int], tuple[RuntimeTreeEntry, ...]]:
    before = os.lstat(root)
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode) or stat.S_IMODE(before.st_mode) != 0o555:
        raise ContractError("prepared OCR runtime root is invalid")
    for artifact in artifacts:
        path = root.joinpath(*PurePosixPath(artifact.relative_path).parts)
        reject_symlink_ancestors(path)
        info = os.lstat(path)
        if (
            not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o444
            or info.st_size != artifact.size
        ):
            raise ContractError("prepared OCR runtime file is invalid")
        size, digest = _hash_bound_file(path, info)
        if size != artifact.size or digest != artifact.digest:
            raise ContractError("prepared OCR runtime file drifted")
    entries = _scan_runtime_tree(
        root, require_sealed=True,
        artifact_paths=frozenset(artifact.relative_path for artifact in artifacts),
    )
    for directory in (item for item in root.rglob("*") if item.is_dir()):
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o555:
            raise ContractError("prepared OCR runtime directory drifted")
    after = os.lstat(root)
    if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_dev, after.st_ino, after.st_mtime_ns, after.st_ctime_ns
    ):
        raise ContractError("prepared OCR runtime root drifted")
    return (before.st_dev, before.st_ino), entries


def _validate_gpu(value: object) -> GpuIdentity:
    if type(value) is not GpuIdentity:
        raise ContractError("GPU compatibility readback must be exact")
    _require_exact_text(value.name, "GPU name")
    if _require_exact_text(value.compute_capability, "compute capability", 8) != "8.9":
        raise ContractError("GPU compute capability differs")
    _require_exact_text(value.driver_version, "driver version", 32)
    _require_digest(value.device_uuid_digest, "device UUID digest")
    if value.cuda_version != "12.6":
        raise ContractError("CUDA version differs")
    if any(type(flag) is not bool for flag in (
        value.paddle_check_passed, value.gpu_execution_observed,
        value.cpu_fallback_observed, value.telemetry_observed,
    )):
        raise ContractError("GPU compatibility flags must be exact")
    if not value.paddle_check_passed or not value.gpu_execution_observed or value.cpu_fallback_observed or value.telemetry_observed:
        raise ContractError("GPU compatibility proof failed")
    return value


def prepare_ocr_runtime(*, source_root: Path, state_root: Path,
                        artifacts: tuple[OfficialArtifact, ...],
                        runtime_installer: RuntimeInstaller,
                        compatibility_probe: CompatibilityProbe) -> PreparedOcrRuntime:
    checked = _validate_artifacts(artifacts)
    artifact_manifest_digest = _artifact_digest(checked)
    model_inventory_digest = _artifact_digest(checked, role="model")
    source = Path(os.path.abspath(os.fspath(source_root)))
    state, _ = ensure_contained(Path(state_root), repository_root(), "OCR runtime state")
    reject_symlink_ancestors(source, include_self=True)
    source_info = os.lstat(source)
    if not stat.S_ISDIR(source_info.st_mode) or stat.S_ISLNK(source_info.st_mode):
        raise ContractError("OCR artifact source root is invalid")
    reject_symlink_ancestors(state)
    try:
        os.mkdir(state, 0o700)
    except FileExistsError:
        info = os.lstat(state)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ContractError("OCR runtime state is invalid")
    final = state / _FINAL_NAME
    partial = state / _PARTIAL_NAME
    if final.exists():
        identity, entries = _scan_final(final, checked)
        gpu = _validate_gpu(compatibility_probe(final))
        return PreparedOcrRuntime(
            final, identity, _tree_digest(entries), len(entries),
            sum(item.size for item in entries), gpu, entries,
            artifact_manifest_digest, model_inventory_digest, "3.12",
            _PACKAGE_VERSIONS, "12.6", OCR_CANDIDATES, checked,
        )
    if partial.exists():
        raise ContractError("OCR runtime partial collision")
    os.mkdir(partial, 0o700)
    partial_info = os.lstat(partial)
    owned_identity = (partial_info.st_dev, partial_info.st_ino)
    try:
        for artifact in checked:
            destination = partial.joinpath(*PurePosixPath(artifact.relative_path).parts)
            _mkdir_path(partial, artifact.relative_path)
            _copy_verified_source(source, artifact, destination)
        try:
            runtime_installer(partial)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                raise
            raise ContractError("OCR runtime installer failed") from exc
        unsealed_entries = _scan_runtime_tree(
            partial, require_sealed=False,
            artifact_paths=frozenset(artifact.relative_path for artifact in checked),
        )
        for child in (item for item in partial.rglob("*") if item.is_file()):
            os.chmod(child, _sealed_file_mode(child.relative_to(partial).as_posix()))
        for directory in sorted((item for item in partial.rglob("*") if item.is_dir()), key=lambda item: len(item.parts), reverse=True):
            os.chmod(directory, 0o555)
        os.chmod(partial, 0o555)
        gpu = _validate_gpu(compatibility_probe(partial))
        source_after = sum(_copy_verified_source(source, artifact) for artifact in checked)
        if source_after != sum(item.size for item in checked):
            raise ContractError("OCR artifact source drifted")
        parent = os.open(state, _DIRECTORY_FLAGS)
        try:
            _rename_noreplace_at(parent, _PARTIAL_NAME, _FINAL_NAME)
            os.fsync(parent)
        finally:
            os.close(parent)
        identity, entries = _scan_final(final, checked)
        if entries != unsealed_entries:
            raise ContractError("OCR runtime manifest drifted")
        return PreparedOcrRuntime(
            final, identity, _tree_digest(entries), len(entries),
            sum(item.size for item in entries), gpu, entries,
            artifact_manifest_digest, model_inventory_digest, "3.12",
            _PACKAGE_VERSIONS, "12.6", OCR_CANDIDATES, checked,
        )
    except BaseException as exc:
        target = final if final.exists() and not partial.exists() else partial
        _remove_owned_tree(target, owned_identity)
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        if isinstance(exc, ContractError):
            raise
        raise ContractError("OCR runtime preparation failed") from exc


def validate_prepared_ocr_runtime(value: object) -> PreparedOcrRuntime:
    if type(value) is not PreparedOcrRuntime:
        raise ContractError("prepared OCR runtime must be exact")
    if type(value.root_identity) is not tuple or len(value.root_identity) != 2:
        raise ContractError("prepared OCR runtime identity is invalid")
    _require_digest(value.runtime_digest, "runtime digest")
    if type(value.file_count) is not int or not _MIN_ARTIFACTS + 1 <= value.file_count <= _MAX_FILES:
        raise ContractError("prepared OCR runtime file count differs")
    if type(value.total_bytes) is not int or not 1 <= value.total_bytes <= _MAX_TOTAL_BYTES:
        raise ContractError("prepared OCR runtime byte count differs")
    _validate_gpu(value.gpu_identity)
    checked = _validate_artifacts(value.artifacts)
    if (
        _require_digest(value.artifact_manifest_digest, "artifact manifest digest")
        != _artifact_digest(checked)
        or _require_digest(value.model_inventory_digest, "model inventory digest")
        != _artifact_digest(checked, role="model")
    ):
        raise ContractError("prepared OCR artifact binding differs")
    if value.python_version != "3.12" or value.package_versions != _PACKAGE_VERSIONS:
        raise ContractError("prepared OCR package identity differs")
    if value.cuda_version != "12.6" or value.candidates != OCR_CANDIDATES:
        raise ContractError("prepared OCR execution identity differs")
    info = os.lstat(value.root)
    if (info.st_dev, info.st_ino) != value.root_identity:
        raise ContractError("prepared OCR runtime root identity drifted")
    artifact_paths = frozenset(artifact.relative_path for artifact in checked)
    entries = _scan_runtime_tree(value.root, require_sealed=True, artifact_paths=artifact_paths)
    if entries != value.tree_manifest or _tree_digest(entries) != value.runtime_digest:
        raise ContractError("prepared OCR runtime tree drifted")
    return value


def validate_ocr_runtime_tree_digest(root: Path, expected_digest: str) -> str:
    """Reopen the sealed installed runtime and bind every retained byte."""

    if type(root) is not type(Path()) or type(expected_digest) is not str:
        raise ContractError("OCR runtime tree binding must be exact")
    target = Path(os.path.abspath(os.fspath(root)))
    reject_symlink_ancestors(target, include_self=True)
    artifact_paths: set[str] = set()
    for directory in (target / "packages", target / "models"):
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ContractError("prepared OCR artifact directory drifted")
        for path in directory.iterdir():
            entry = os.lstat(path)
            if not stat.S_ISREG(entry.st_mode) or stat.S_ISLNK(entry.st_mode) or entry.st_nlink != 1:
                raise ContractError("prepared OCR artifact entry drifted")
            artifact_paths.add(path.relative_to(target).as_posix())
    entries = _scan_runtime_tree(target, require_sealed=True, artifact_paths=frozenset(artifact_paths))
    observed = _tree_digest(entries)
    if observed != expected_digest:
        raise ContractError("prepared OCR runtime digest drifted")
    return observed
