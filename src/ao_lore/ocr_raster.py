"""Descriptor-held, bounded, deterministic OCR source raster publication."""

from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import os
import re
import stat
import struct
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Callable

from ._strict_io import ContractError, parse_strict_json, reject_symlink_ancestors
from .ocr_contracts import OCR_POLICY_DIGEST, validate_raster_manifest


_MAX_SOURCE_BYTES = 50 * 1024 * 1024
_MAX_PAGES = 100
_MAX_PIXELS_PER_PAGE = 25_000_000
_MAX_PIXELS_TOTAL = 1_000_000_000
_MAX_PAGE_BYTES = 100 * 1024 * 1024
_MAX_RENDERED_BYTES = 500 * 1024 * 1024
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
_RENAME_NOREPLACE = 1
_CONFIGURATION = {
    "aggregate_rendered_bytes": _MAX_RENDERED_BYTES,
    "decoded_bytes_per_page": _MAX_PAGE_BYTES,
    "dpi": 300,
    "format": "png",
    "maximum_pages": _MAX_PAGES,
    "maximum_pixels_document": _MAX_PIXELS_TOTAL,
    "maximum_pixels_page": _MAX_PIXELS_PER_PAGE,
    "maximum_source_bytes": _MAX_SOURCE_BYTES,
    "render_timeout_seconds": 180,
    "rendering": "srgb-lossless-fixed-antialiasing-v1",
}
OCR_RASTER_CONFIGURATION_DIGEST = "sha256:" + sha256(
    json.dumps(_CONFIGURATION, sort_keys=True, separators=(",", ":")).encode("ascii")
).hexdigest()


@dataclass(frozen=True)
class ReviewedOcrSource:
    source_id: str
    path: Path
    media_type: str
    source_digest: str
    explicit_ocr: bool


@dataclass(frozen=True)
class RasterPageOutput:
    width: int
    height: int
    body: bytes


@dataclass(frozen=True)
class RasterPage:
    page_id: str
    width: int
    height: int
    size: int
    digest: str
    path: Path


@dataclass(frozen=True)
class RasterBundle:
    root: Path
    root_identity: tuple[int, int]
    source_id: str
    source_digest: str
    media_type: str
    configuration_digest: str
    renderer_digest: str
    dpi: int
    manifest_digest: str
    pages: tuple[RasterPage, ...]


Renderer = Callable[[bytes, str, dict[str, object]], tuple[RasterPageOutput, ...]]


class OcrRasterRejected(ContractError):
    """A source-level closed rejection suitable for qualification scoring."""

    def __init__(self, category: str):
        if category not in {"invalid_source", "encrypted_source", "resource_limit"}:
            raise ContractError("OCR raster rejection category differs")
        super().__init__(category)
        self.category = category


def _write_all(descriptor: int, body: bytes) -> None:
    offset = 0
    while offset < len(body):
        written = os.write(descriptor, body[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, _DIRECTORY_FLAGS)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sealed_memfd(name: str, body: bytes) -> int:
    descriptor = os.memfd_create(name, os.MFD_CLOEXEC | getattr(os, "MFD_ALLOW_SEALING", 0))
    try:
        _write_all(descriptor, body)
        os.lseek(descriptor, 0, os.SEEK_SET)
        seals = fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS, seals)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def render_in_ocr_sandbox(source_body: bytes, media_type: str,
                          configuration: dict[str, object], *,
                          runtime_root: Path) -> tuple[RasterPageOutput, ...]:
    """Render through the sealed runtime in a networkless minimal root."""

    if type(source_body) is not bytes or not 1 <= len(source_body) <= _MAX_SOURCE_BYTES:
        raise ContractError("OCR raster worker source differs")
    if media_type not in ("application/pdf", "image/png", "image/jpeg"):
        raise ContractError("OCR raster worker media type differs")
    if type(configuration) is not dict or configuration.get("dpi") != 300:
        raise ContractError("OCR raster worker configuration differs")
    runtime = Path(os.path.abspath(os.fspath(runtime_root)))
    reject_symlink_ancestors(runtime, include_self=True)
    runtime_info = os.lstat(runtime)
    if not stat.S_ISDIR(runtime_info.st_mode) or stat.S_ISLNK(runtime_info.st_mode):
        raise ContractError("OCR raster worker runtime differs")
    contract = json.dumps({
        "schema": "ao.lore.ocr-raster-worker.v0.1",
        "media_type": media_type,
        "dpi": 300,
    }, sort_keys=True, separators=(",", ":")).encode("ascii")
    source_fd = _sealed_memfd("ao-lore-ocr-raster-source", source_body)
    contract_fd = _sealed_memfd("ao-lore-ocr-raster-contract", contract)
    repository = Path(__file__).resolve().parents[2]
    try:
        with tempfile.TemporaryDirectory(prefix="ao-lore-ocr-raster-") as temporary:
            output = Path(temporary)
            command = (
                "/usr/bin/bwrap", "--die-with-parent", "--new-session",
                "--unshare-net", "--unshare-ipc", "--unshare-pid", "--unshare-uts",
                "--tmpfs", "/", "--ro-bind", "/usr", "/usr", "--ro-bind", "/bin", "/bin",
                "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
                "--ro-bind", str(runtime), "/runtime", "--ro-bind", str(repository / "src"), "/app/src",
                "--dir", "/input", "--file", str(source_fd), "/input/source",
                "--file", str(contract_fd), "/input/contract.json",
                "--bind", str(output), "/output", "--dev", "/dev", "--proc", "/proc",
                "--tmpfs", "/tmp", "--clearenv", "--setenv", "HOME", "/tmp",
                "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
                "--setenv", "PYTHONPATH", "/app/src", "--setenv", "HF_HUB_OFFLINE", "1",
                "--setenv", "TRANSFORMERS_OFFLINE", "1",
                "/runtime/installed/bin/python", "-m", "ao_lore.ocr_raster_worker",
            )
            try:
                completed = subprocess.run(
                    command, check=False, cwd="/", env={}, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
                    pass_fds=(source_fd, contract_fd),
                )
            except BaseException as exc:
                if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
                    raise
                raise ContractError("OCR raster sandbox failed") from exc
            if completed.returncode != 0 or len(completed.stdout) > 65536 or len(completed.stderr) > 65536:
                raise ContractError("OCR raster sandbox failed")
            result = parse_strict_json(completed.stdout, "OCR raster worker result")
            if type(result) is dict and set(result) == {
                "schema", "category", "network_accessed", "provider_calls", "authority_advanced"
            }:
                if (
                    result["schema"] != "ao.lore.ocr-raster-worker-rejection.v0.1"
                    or result["category"] not in {"invalid_source", "encrypted_source", "resource_limit"}
                    or any(result[field] is not False for field in (
                        "network_accessed", "provider_calls", "authority_advanced"
                    ))
                    or list(output.iterdir())
                ):
                    raise ContractError("OCR raster worker rejection differs")
                raise OcrRasterRejected(result["category"])
            if type(result) is not dict or set(result) != {
                "schema", "pages", "network_accessed", "provider_calls", "authority_advanced"
            } or result["schema"] != "ao.lore.ocr-raster-worker-result.v0.1" or any(
                result[field] is not False for field in ("network_accessed", "provider_calls", "authority_advanced")
            ) or type(result["pages"]) is not list or not 1 <= len(result["pages"]) <= _MAX_PAGES:
                raise ContractError("OCR raster worker result differs")
            outputs: list[RasterPageOutput] = []
            allowed: set[str] = set()
            for expected, page in enumerate(result["pages"], 1):
                page_id = f"page-{expected:04d}"
                if type(page) is not dict or set(page) != {"page_id", "width", "height", "size"} or page["page_id"] != page_id:
                    raise ContractError("OCR raster worker page differs")
                path = output / (page_id + ".png")
                allowed.add(path.name)
                info = os.lstat(path)
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_size != page["size"]:
                    raise ContractError("OCR raster worker output differs")
                body = path.read_bytes()
                width, height = _png_dimensions(body)
                if type(page["width"]) is not int or type(page["height"]) is not int or (width, height) != (page["width"], page["height"]):
                    raise ContractError("OCR raster worker dimensions differ")
                outputs.append(RasterPageOutput(width, height, body))
            if {item.name for item in output.iterdir()} != allowed:
                raise ContractError("OCR raster worker entries differ")
            return tuple(outputs)
    finally:
        os.close(source_fd)
        os.close(contract_fd)


def _require_digest(value: object, label: str) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise ContractError(f"{label} must be a lowercase sha256 digest")
    return value


def _validate_source(value: object) -> ReviewedOcrSource:
    if type(value) is not ReviewedOcrSource:
        raise ContractError("reviewed OCR source must be exact")
    if type(value.source_id) is not str or _IDENTIFIER.fullmatch(value.source_id) is None:
        raise ContractError("OCR source id differs")
    if type(value.path) is not type(Path()):
        raise ContractError("OCR source path must be exact")
    if value.media_type not in ("application/pdf", "image/png", "image/jpeg"):
        raise ContractError("OCR source media type differs")
    _require_digest(value.source_digest, "source digest")
    if type(value.explicit_ocr) is not bool:
        raise ContractError("OCR selection must be exact")
    if value.media_type == "application/pdf" and not value.explicit_ocr:
        raise ContractError("PDF requires explicit OCR selection")
    return value


def _read_held_source(value: ReviewedOcrSource) -> tuple[bytes, tuple[int, int, int, int]]:
    path = Path(os.path.abspath(os.fspath(value.path)))
    reject_symlink_ancestors(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ContractError("OCR source file is invalid") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not 1 <= before.st_size <= _MAX_SOURCE_BYTES:
            raise ContractError("OCR source file is invalid")
        chunks: list[bytes] = []
        remaining = _MAX_SOURCE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != identity or len(body) != before.st_size:
        raise ContractError("OCR source changed while held")
    public = os.lstat(path)
    if (public.st_dev, public.st_ino, public.st_size, public.st_mtime_ns) != identity:
        raise ContractError("OCR source identity drifted")
    if "sha256:" + sha256(body).hexdigest() != value.source_digest:
        raise ContractError("OCR source digest differs")
    _validate_source_signature(body, value.media_type)
    return body, identity


def _validate_source_signature(body: bytes, media_type: str) -> None:
    if media_type == "application/pdf":
        if not body.startswith(b"%PDF-") or b"%%EOF" not in body[-1024:]:
            raise OcrRasterRejected("invalid_source")
    elif media_type == "image/png":
        try:
            _png_dimensions(body)
        except ContractError as exc:
            if any(token in str(exc) for token in (
                "dimensions exceed policy", "is oversized", "decoded size differs",
            )):
                raise OcrRasterRejected("resource_limit") from None
            raise OcrRasterRejected("invalid_source") from None
    elif not (body.startswith(b"\xff\xd8") and body.endswith(b"\xff\xd9")):
        raise OcrRasterRejected("invalid_source")


def _png_dimensions(body: bytes) -> tuple[int, int]:
    if len(body) < 45 or not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ContractError("rendered PNG is malformed")
    offset = 8
    width = height = None
    compressed = bytearray()
    found_end = False
    while offset < len(body):
        if offset + 12 > len(body):
            raise ContractError("rendered PNG is truncated")
        length = struct.unpack(">I", body[offset:offset + 4])[0]
        kind = body[offset + 4:offset + 8]
        end = offset + 12 + length
        if end > len(body):
            raise ContractError("rendered PNG is truncated")
        value = body[offset + 8:offset + 8 + length]
        expected_crc = struct.unpack(">I", body[offset + 8 + length:end])[0]
        if zlib.crc32(kind + value) & 0xFFFFFFFF != expected_crc:
            raise ContractError("rendered PNG checksum differs")
        if offset == 8:
            if kind != b"IHDR" or length != 13:
                raise ContractError("rendered PNG header differs")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", value)
            if depth != 8 or color != 2 or compression or filtering or interlace:
                raise ContractError("rendered PNG format differs")
            if width < 1 or height < 1 or width * height > _MAX_PIXELS_PER_PAGE:
                raise ContractError("rendered PNG dimensions exceed policy")
        elif kind == b"IDAT":
            compressed.extend(value)
            if len(compressed) > _MAX_PAGE_BYTES:
                raise ContractError("rendered PNG is oversized")
        elif kind == b"IEND":
            if length or end != len(body):
                raise ContractError("rendered PNG terminal chunk differs")
            found_end = True
            break
        elif kind[0] & 32 == 0:
            raise ContractError("rendered PNG has an unknown critical chunk")
        offset = end
    if width is None or height is None or not found_end or not compressed:
        raise ContractError("rendered PNG structure differs")
    maximum = width * height * 3 + height
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(bytes(compressed), maximum + 1)
    if len(decoded) != maximum or not decoder.eof or decoder.unused_data:
        raise ContractError("rendered PNG decoded size differs")
    return width, height


def _rename_noreplace(parent: int, source: str, target: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.renameat2(parent, source.encode(), parent, target.encode(), _RENAME_NOREPLACE)
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ContractError("OCR raster publication collision")
        raise OSError(error, os.strerror(error))


def _manifest(source: ReviewedOcrSource, renderer_digest: str, pages: tuple[RasterPage, ...]) -> dict[str, object]:
    return validate_raster_manifest({
        "schema_version": "ao.lore.private-ocr-raster-manifest.v0.1",
        "policy_digest": OCR_POLICY_DIGEST,
        "source_id": source.source_id,
        "source_digest": source.source_digest,
        "configuration_digest": OCR_RASTER_CONFIGURATION_DIGEST,
        "renderer_digest": renderer_digest,
        "dpi": 300,
        "pages": [
            {"page_id": page.page_id, "width": page.width, "height": page.height,
             "size": page.size, "digest": page.digest}
            for page in pages
        ],
        "network_accessed": False,
        "provider_calls": False,
        "promotion": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
    })


def _manifest_bytes(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _intent(source: ReviewedOcrSource, renderer_digest: str) -> dict[str, object]:
    return {
        "schema": "ao.lore.ocr-raster-intent.v0.1",
        "source_id": source.source_id,
        "source_digest": source.source_digest,
        "media_type": source.media_type,
        "configuration_digest": OCR_RASTER_CONFIGURATION_DIGEST,
        "renderer_digest": renderer_digest,
    }


def _intent_bytes(source: ReviewedOcrSource, renderer_digest: str) -> bytes:
    return json.dumps(_intent(source, renderer_digest), sort_keys=True, separators=(",", ":")).encode("ascii")


def _validate_intent(
    path: Path, source: ReviewedOcrSource, renderer_digest: str,
    configuration_digest: str = OCR_RASTER_CONFIGURATION_DIGEST,
) -> None:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ContractError("OCR raster intent is invalid") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
        raise ContractError("OCR raster intent is invalid")
    raw = path.read_bytes()
    value = parse_strict_json(raw, "OCR raster intent")
    expected = _intent(source, renderer_digest)
    expected["configuration_digest"] = configuration_digest
    if value != expected or raw != json.dumps(
        expected, sort_keys=True, separators=(",", ":")
    ).encode("ascii"):
        raise ContractError("OCR raster intent differs")


def _load_bundle(
    root: Path, source: ReviewedOcrSource, renderer_digest: str,
    configuration_digest: str = OCR_RASTER_CONFIGURATION_DIGEST,
) -> RasterBundle:
    public = os.lstat(root)
    if not stat.S_ISDIR(public.st_mode) or stat.S_ISLNK(public.st_mode):
        raise ContractError("OCR raster root is invalid")
    manifest_path = root / "manifest.json"
    _validate_intent(
        root / "intent.json", source, renderer_digest, configuration_digest,
    )
    raw = manifest_path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ContractError("OCR raster manifest is oversized")
    parsed = parse_strict_json(raw, "OCR raster manifest")
    value = validate_raster_manifest(parsed)
    if value["source_id"] != source.source_id or value["source_digest"] != source.source_digest or value["renderer_digest"] != renderer_digest or value["configuration_digest"] != configuration_digest:
        raise ContractError("OCR raster manifest binding differs")
    if raw != _manifest_bytes(value):
        raise ContractError("OCR raster manifest bytes differ")
    pages: list[RasterPage] = []
    allowed = {"intent.json", "manifest.json"}
    for item in value["pages"]:
        name = item["page_id"] + ".png"
        allowed.add(name)
        path = root / name
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1 or info.st_size != item["size"]:
            raise ContractError("OCR raster page identity differs")
        body = path.read_bytes()
        width, height = _png_dimensions(body)
        if width != item["width"] or height != item["height"] or "sha256:" + sha256(body).hexdigest() != item["digest"]:
            raise ContractError("OCR raster page binding differs")
        pages.append(RasterPage(item["page_id"], width, height, len(body), item["digest"], path))
    if {entry.name for entry in root.iterdir()} != allowed:
        raise ContractError("OCR raster root entries differ")
    return RasterBundle(root, (public.st_dev, public.st_ino), source.source_id, source.source_digest, source.media_type,
                        configuration_digest, renderer_digest, 300,
                        "sha256:" + sha256(raw).hexdigest(), tuple(pages))


def _remove_exact_partial(partial: Path, identity: tuple[int, int]) -> None:
    current = os.lstat(partial)
    if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != identity:
        raise ContractError("OCR raster partial identity drifted")
    os.chmod(partial, 0o700)
    for child in partial.iterdir():
        info = os.lstat(child)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
            raise ContractError("OCR raster partial entry differs")
        os.unlink(child)
    os.rmdir(partial)


def _recover_partial(partial: Path, final: Path, state: Path, source: ReviewedOcrSource,
                     renderer_digest: str, partial_name: str, final_name: str) -> RasterBundle | None:
    info = os.lstat(partial)
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise ContractError("OCR raster partial collision")
    identity = (info.st_dev, info.st_ino)
    _validate_intent(partial / "intent.json", source, renderer_digest)
    names = {entry.name for entry in partial.iterdir()}
    page_names = sorted(name for name in names if re.fullmatch(r"page-[0-9]{4}\.png", name))
    if names - {"intent.json", "manifest.json", *page_names}:
        raise ContractError("OCR raster partial entries differ")
    if page_names != [f"page-{index:04d}.png" for index in range(1, len(page_names) + 1)]:
        raise ContractError("OCR raster partial page order differs")
    for name in page_names:
        page = partial / name
        page_info = os.lstat(page)
        if not stat.S_ISREG(page_info.st_mode) or stat.S_ISLNK(page_info.st_mode) or page_info.st_nlink != 1:
            raise ContractError("OCR raster partial page differs")
        _png_dimensions(page.read_bytes())
    if "manifest.json" in names:
        _load_bundle(partial, source, renderer_digest)
        for child in partial.iterdir():
            os.chmod(child, 0o444)
        os.chmod(partial, 0o555)
        _fsync_directory(partial)
        parent = os.open(state, _DIRECTORY_FLAGS)
        try:
            _rename_noreplace(parent, partial_name, final_name)
            os.fsync(parent)
        finally:
            os.close(parent)
        return _load_bundle(final, source, renderer_digest)
    _remove_exact_partial(partial, identity)
    _fsync_directory(state)
    return None


def validate_raster_bundle(value: object) -> RasterBundle:
    if type(value) is not RasterBundle:
        raise ContractError("OCR raster bundle must be exact")
    source = ReviewedOcrSource(value.source_id, Path("/unopened"), value.media_type, value.source_digest, True)
    checked = _load_bundle(value.root, source, _require_digest(value.renderer_digest, "renderer digest"))
    if checked != value:
        raise ContractError("OCR raster bundle drifted")
    return value


def load_raster_bundle(root: Path) -> RasterBundle:
    """Reopen one canonical raster bundle using only its retained intent bytes."""

    selected = Path(os.path.abspath(os.fspath(root)))
    reject_symlink_ancestors(selected, include_self=True)
    intent_path = selected / "intent.json"
    intent_info = os.lstat(intent_path)
    if (
        not stat.S_ISREG(intent_info.st_mode)
        or stat.S_ISLNK(intent_info.st_mode)
        or intent_info.st_nlink != 1
        or not 1 <= intent_info.st_size <= 4096
    ):
        raise ContractError("OCR raster intent is invalid")
    raw = intent_path.read_bytes()
    after = os.lstat(intent_path)
    if (
        intent_info.st_dev, intent_info.st_ino, intent_info.st_size,
        intent_info.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ContractError("OCR raster intent drifted")
    value = parse_strict_json(raw, "OCR raster intent")
    keys = {
        "schema", "source_id", "source_digest", "media_type",
        "configuration_digest", "renderer_digest",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or value["schema"] != "ao.lore.ocr-raster-intent.v0.1"
        or value["configuration_digest"] != OCR_RASTER_CONFIGURATION_DIGEST
        or json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii") != raw
    ):
        raise ContractError("OCR raster intent differs")
    source = ReviewedOcrSource(
        value["source_id"], Path("/unopened"), value["media_type"],
        value["source_digest"], True,
    )
    return _load_bundle(
        selected, _validate_source(source),
        _require_digest(value["renderer_digest"], "renderer digest"),
    )


def load_raster_bundle_for_cleanup(
    root: Path, *, allowed_configuration_digests: frozenset[str],
) -> RasterBundle:
    """Fully validate one retained raster under an exact cleanup-only policy."""

    if (
        type(allowed_configuration_digests) is not frozenset
        or not allowed_configuration_digests
        or any(type(item) is not str or _DIGEST.fullmatch(item) is None
               for item in allowed_configuration_digests)
    ):
        raise ContractError("OCR raster cleanup policy differs")
    selected = Path(os.path.abspath(os.fspath(root)))
    reject_symlink_ancestors(selected, include_self=True)
    intent_path = selected / "intent.json"
    intent_info = os.lstat(intent_path)
    if (
        not stat.S_ISREG(intent_info.st_mode)
        or stat.S_ISLNK(intent_info.st_mode)
        or intent_info.st_nlink != 1
        or not 1 <= intent_info.st_size <= 4096
    ):
        raise ContractError("OCR raster intent is invalid")
    raw = intent_path.read_bytes()
    after = os.lstat(intent_path)
    if (
        intent_info.st_dev, intent_info.st_ino, intent_info.st_size,
        intent_info.st_mtime_ns,
    ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ContractError("OCR raster intent drifted")
    value = parse_strict_json(raw, "OCR raster cleanup intent")
    keys = {
        "schema", "source_id", "source_digest", "media_type",
        "configuration_digest", "renderer_digest",
    }
    if (
        type(value) is not dict or set(value) != keys
        or value["schema"] != "ao.lore.ocr-raster-intent.v0.1"
        or value["configuration_digest"] not in allowed_configuration_digests
    ):
        raise ContractError("OCR raster cleanup intent differs")
    source = _validate_source(ReviewedOcrSource(
        value["source_id"], Path("/unopened"), value["media_type"],
        value["source_digest"], True,
    ))
    return _load_bundle(
        selected, source,
        _require_digest(value["renderer_digest"], "renderer digest"),
        value["configuration_digest"],
    )


def rasterize_ocr_source(source: ReviewedOcrSource, *, state_root: Path,
                         renderer_digest: str, renderer: Renderer) -> RasterBundle:
    reviewed = _validate_source(source)
    renderer_digest = _require_digest(renderer_digest, "renderer digest")
    body, source_identity = _read_held_source(reviewed)
    state = Path(os.path.abspath(os.fspath(state_root)))
    reject_symlink_ancestors(state)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_info = os.lstat(state)
    if not stat.S_ISDIR(state_info.st_mode) or stat.S_ISLNK(state_info.st_mode):
        raise ContractError("OCR raster state root is invalid")
    suffix = reviewed.source_digest[7:23]
    final_name = f"raster-{reviewed.source_id}-{suffix}"
    partial_name = "." + final_name + ".partial"
    final = state / final_name
    partial = state / partial_name
    if final.exists():
        return _load_bundle(final, reviewed, renderer_digest)
    if partial.exists():
        recovered = _recover_partial(partial, final, state, reviewed, renderer_digest, partial_name, final_name)
        if recovered is not None:
            return recovered
    partial.mkdir(mode=0o700)
    owned = os.lstat(partial)
    try:
        intent_raw = _intent_bytes(reviewed, renderer_digest)
        descriptor = os.open(partial / "intent.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(descriptor, intent_raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(partial)
        outputs = renderer(body, reviewed.media_type, dict(_CONFIGURATION))
        if type(outputs) is not tuple or not 1 <= len(outputs) <= _MAX_PAGES:
            raise ContractError("OCR renderer page count differs")
        pages: list[RasterPage] = []
        total_pixels = total_bytes = 0
        for index, output in enumerate(outputs, 1):
            if type(output) is not RasterPageOutput or type(output.width) is not int or type(output.height) is not int or type(output.body) is not bytes:
                raise ContractError("OCR renderer page must be exact")
            if not 1 <= len(output.body) <= _MAX_PAGE_BYTES:
                raise ContractError("OCR renderer page bytes exceed policy")
            width, height = _png_dimensions(output.body)
            if (width, height) != (output.width, output.height):
                raise ContractError("OCR renderer page dimensions differ")
            total_pixels += width * height
            total_bytes += len(output.body)
            if total_pixels > _MAX_PIXELS_TOTAL or total_bytes > _MAX_RENDERED_BYTES:
                raise ContractError("OCR renderer aggregate exceeds policy")
            page_id = f"page-{index:04d}"
            path = partial / (page_id + ".png")
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                _write_all(descriptor, output.body)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            pages.append(RasterPage(page_id, width, height, len(output.body), "sha256:" + sha256(output.body).hexdigest(), path))
        manifest = _manifest(reviewed, renderer_digest, tuple(pages))
        raw = _manifest_bytes(manifest)
        descriptor = os.open(partial / "manifest.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            _write_all(descriptor, raw)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(partial)
        current = os.lstat(reviewed.path)
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != source_identity:
            raise ContractError("OCR source identity drifted")
        for path in partial.iterdir():
            os.chmod(path, 0o444)
        os.chmod(partial, 0o555)
        _fsync_directory(partial)
        parent = os.open(state, _DIRECTORY_FLAGS)
        try:
            _rename_noreplace(parent, partial_name, final_name)
            os.fsync(parent)
        finally:
            os.close(parent)
        return _load_bundle(final, reviewed, renderer_digest)
    except BaseException:
        try:
            current = os.lstat(partial)
            if stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                _remove_exact_partial(partial, (owned.st_dev, owned.st_ino))
        except OSError:
            pass
        raise
