"""Internal PaddleOCR inference worker for the sealed sandbox."""

from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess
import time
from decimal import Decimal, ROUND_HALF_EVEN
from hashlib import sha256
from pathlib import Path

from ._strict_io import ContractError, parse_strict_json
from .ocr_contracts import OCR_CANDIDATES, validate_worker_contract, validate_raster_manifest, validate_worker_result
from .paddle_ocr import _candidate_digest


def _integer(value) -> int:
    return int(Decimal(str(float(value))).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _confidence(value) -> int:
    return int((Decimal(str(float(value))) * 1_000_000).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def _restore_polygon_to_source(
    polygon: list[list[int]], angle: object, width: int, height: int
) -> list[list[int]]:
    """Invert Paddle's right-angle document rotation into source coordinates."""

    if type(angle) is not int or angle not in (0, 90, 180, 270):
        raise ContractError("Paddle OCR document orientation differs")
    output: list[list[int]] = []
    for point in polygon:
        if type(point) is not list or len(point) != 2 or any(type(value) is not int for value in point):
            raise ContractError("Paddle OCR polygon differs")
        x, y = point
        if angle == 0:
            restored = [x, y]
        elif angle == 90:
            restored = [width - y, x]
        elif angle == 180:
            restored = [width - x, height - y]
        else:
            restored = [y, height - x]
        if not 0 <= restored[0] <= width or not 0 <= restored[1] <= height:
            raise ContractError("Paddle OCR restored polygon exceeds source")
        output.append(restored)
    return output


def _detection_records(
    texts: object, scores: object, polygons: object, *,
    angle: object, width: int, height: int,
) -> list[dict[str, object]]:
    if (
        not hasattr(texts, "__len__")
        or not hasattr(scores, "__len__")
        or not hasattr(polygons, "__len__")
        or not len(texts) == len(scores) == len(polygons)
        or len(texts) > 100000
    ):
        raise ContractError("Paddle OCR detections differ")
    detections: list[dict[str, object]] = []
    for index, (text, score, polygon) in enumerate(zip(texts, scores, polygons)):
        if type(text) is not str or len(text) > 4096:
            raise ContractError("Paddle OCR text differs")
        if not text:
            continue
        points = _restore_polygon_to_source(
            [[_integer(point[0]), _integer(point[1])] for point in polygon],
            angle, width, height,
        )
        detections.append({
            "index": len(detections), "text": text,
            "confidence_millionths": _confidence(score),
            "polygon": points,
        })
    return detections


def _load_contract() -> tuple[dict[str, object], bytes]:
    raw = Path("/launch/worker-contract.json").read_bytes()
    value = validate_worker_contract(parse_strict_json(raw, "Paddle OCR worker contract"))
    if raw != json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii"):
        raise ContractError("Paddle OCR worker contract bytes differ")
    return value, raw


def _load_manifest(contract: dict[str, object]) -> dict[str, object]:
    raw = Path("/pages/manifest.json").read_bytes()
    if "sha256:" + sha256(raw).hexdigest() != contract["raster_manifest_digest"]:
        raise ContractError("Paddle OCR raster manifest digest differs")
    value = validate_raster_manifest(parse_strict_json(raw, "Paddle OCR raster manifest"))
    if [page["page_id"] for page in value["pages"]] != contract["page_ids"]:
        raise ContractError("Paddle OCR raster page identity differs")
    return value


def _run(contract: dict[str, object], manifest: dict[str, object]) -> tuple[list[dict[str, object]], int, int]:
    real_popen = subprocess.Popen

    class BoundCompatibilityReadback:
        def __init__(self, stdout: bytes):
            self.stdout = stdout

        def communicate(self):
            return self.stdout, b""

    def no_child_compatibility(command, *args, **kwargs):
        if command == "ldd --version | awk '/ldd/{print $NF}'":
            version = platform.libc_ver()[1]
            if not version:
                raise PermissionError("libc identity unavailable")
            return BoundCompatibilityReadback(version.encode("ascii"))
        if type(command) is str and command.startswith("ldd ") and "|grep libgomp|awk '{print $3}'" in command:
            return BoundCompatibilityReadback(b"libgomp.so.1")
        raise PermissionError("child process denied")

    subprocess.Popen = no_child_compatibility
    try:
        import paddle
        from paddleocr import PaddleOCR
    finally:
        subprocess.Popen = real_popen

    candidate_index = int(contract["candidate_id"].split("-")[1]) - 1
    detection, recognition = OCR_CANDIDATES[candidate_index]
    model_root = Path("/runtime/installed/models")
    paddle.set_device("gpu:0")
    paddle.device.reset_max_memory_allocated("gpu:0")
    started = time.monotonic_ns()
    log_descriptor = os.open("/tmp/paddle-worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(log_descriptor, "w", encoding="utf-8", closefd=True) as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            engine = PaddleOCR(
                text_detection_model_name=detection,
                text_detection_model_dir=str(model_root / (detection + "_infer")),
                text_recognition_model_name=recognition,
                text_recognition_model_dir=str(model_root / (recognition + "_infer")),
                doc_orientation_classify_model_name="PP-LCNet_x1_0_doc_ori",
                doc_orientation_classify_model_dir=str(model_root / "PP-LCNet_x1_0_doc_ori_infer"),
                textline_orientation_model_name="PP-LCNet_x1_0_textline_ori",
                textline_orientation_model_dir=str(model_root / "PP-LCNet_x1_0_textline_ori_infer"),
                use_doc_orientation_classify=True,
                use_doc_unwarping=False,
                use_textline_orientation=True,
                device="gpu:0",
            )
            outputs = []
            for page in manifest["pages"]:
                result = engine.predict(str(Path("/pages") / (page["page_id"] + ".png")))
                if type(result) is not list or len(result) != 1:
                    raise ContractError("Paddle OCR page result differs")
                item = result[0]
                preprocessing = item["doc_preprocessor_res"]
                if not hasattr(preprocessing, "get"):
                    raise ContractError("Paddle OCR orientation result differs")
                angle = preprocessing.get("angle")
                texts = item["rec_texts"]
                scores = item["rec_scores"]
                polygons = item["rec_polys"]
                detections = _detection_records(
                    texts, scores, polygons, angle=angle,
                    width=page["width"], height=page["height"],
                )
                outputs.append({
                    "page_id": page["page_id"], "width": page["width"],
                    "height": page["height"], "detections": detections,
                })
            latency_microseconds = (time.monotonic_ns() - started) // 1000
            peak_gpu_memory_bytes = int(paddle.device.max_memory_allocated("gpu:0"))
            return outputs, latency_microseconds, peak_gpu_memory_bytes
    except BaseException:
        try:
            os.close(log_descriptor)
        except OSError:
            pass
        raise


def main() -> int:
    contract, _ = _load_contract()
    manifest = _load_manifest(contract)
    if _candidate_digest(Path("/runtime"), contract["candidate_id"]) != contract["model_set_digest"]:
        raise ContractError("Paddle OCR model set digest differs")
    pages, latency_microseconds, peak_gpu_memory_bytes = _run(contract, manifest)
    if _candidate_digest(Path("/runtime"), contract["candidate_id"]) != contract["model_set_digest"]:
        raise ContractError("Paddle OCR model set drifted")
    _load_manifest(contract)
    result = validate_worker_result({
        "schema_version": "ao.lore.private-ocr-worker-result.v0.1",
        "policy_digest": contract["policy_digest"],
        "run_id": contract["run_id"],
        "candidate_id": contract["candidate_id"],
        "runtime_digest": contract["runtime_digest"],
        "model_set_digest": contract["model_set_digest"],
        "configuration_digest": contract["configuration_digest"],
        "latency_microseconds": latency_microseconds,
        "peak_gpu_memory_bytes": peak_gpu_memory_bytes,
        "pages": pages,
        "network_accessed": False,
        "provider_calls": False,
        "promotion": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
    })
    raw = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    descriptor = os.open("/result/worker-result.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Paddle OCR result short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
