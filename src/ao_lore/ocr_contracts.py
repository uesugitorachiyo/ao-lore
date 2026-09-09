"""Strict, content-free contracts for AO Lore's bounded English OCR path."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from ._strict_io import ContractError


OCR_POLICY_VERSION = "ao-lore-private-ocr-v0.1"
_POLICY_TEXT = (
    "ao-lore-private-ocr-v0.1|paddleocr=3.7.0|paddlepaddle-gpu=3.3.0|"
    "cuda=12.6|english-printed|offline|no-fallback"
)
OCR_POLICY_DIGEST = "sha256:" + sha256(_POLICY_TEXT.encode("ascii")).hexdigest()
OCR_CANDIDATES = (
    ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
    ("PP-OCRv6_small_det", "PP-OCRv6_small_rec"),
    ("PP-OCRv5_mobile_det", "en_PP-OCRv5_mobile_rec"),
)
OCR_DECISIONS = ("hold", "candidate_change", "investigate")
AUTHORITY_FIELDS = (
    "network_accessed",
    "provider_calls",
    "promotion",
    "publication",
    "release",
    "deployment",
    "authority_advanced",
)

_DIGEST_LENGTH = len("sha256:") + 64


def _mapping(value: object, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError(f"{label} must be an exact object")
    if set(value) != set(keys):
        raise ContractError(f"{label} fields differ")
    if any(type(key) is not str for key in value):
        raise ContractError(f"{label} field names must be exact strings")
    return value


def _list(value: object, label: str, minimum: int, maximum: int) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ContractError(f"{label} must be a bounded exact array")
    return value


def _text(value: object, label: str, maximum: int = 256) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ContractError(f"{label} must be bounded exact text")
    if any(ord(character) < 32 for character in value):
        raise ContractError(f"{label} contains a control character")
    return value


def _ocr_text(value: object) -> str:
    if type(value) is not str or not value or len(value) > 4096:
        raise ContractError("text must be bounded exact text")
    if any(ord(character) < 32 and character not in "\r\n\t" for character in value):
        raise ContractError("text contains a control character")
    return value


def _identifier(value: object, label: str) -> str:
    text = _text(value, label, 128)
    if not text[0].isalnum() or not text[0].islower() and not text[0].isdigit():
        raise ContractError(f"{label} must start with lowercase alphanumeric")
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in text):
        raise ContractError(f"{label} must be a lowercase identifier")
    return text


def _digest(value: object, label: str) -> str:
    if type(value) is not str or len(value) != _DIGEST_LENGTH or not value.startswith("sha256:"):
        raise ContractError(f"{label} must be a sha256 digest")
    if any(character not in "0123456789abcdef" for character in value[7:]):
        raise ContractError(f"{label} must be a lowercase sha256 digest")
    return value


def _integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"{label} must be a bounded exact integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ContractError(f"{label} must be an exact boolean")
    return value


def _authority(value: dict[str, Any]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for field in AUTHORITY_FIELDS:
        if _boolean(value[field], field):
            raise ContractError(f"{field} must remain false")
        result[field] = False
    return result


def _base(value: object, schema: str, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    body = _mapping(value, keys + AUTHORITY_FIELDS, label)
    if _text(body["schema_version"], "schema_version") != schema:
        raise ContractError(f"{label} schema version differs")
    if _digest(body["policy_digest"], "policy_digest") != OCR_POLICY_DIGEST:
        raise ContractError("OCR policy digest differs")
    return body


def validate_runtime(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "policy_digest", "python_version", "paddleocr_version",
        "paddlepaddle_gpu_version", "cuda_version", "runtime_digest", "gpu_identity_digest",
    )
    body = _base(value, "ao.lore.private-ocr-runtime.v0.1", keys, "OCR runtime")
    constants = {
        "python_version": "3.12", "paddleocr_version": "3.7.0",
        "paddlepaddle_gpu_version": "3.3.0", "cuda_version": "12.6",
    }
    for field, expected in constants.items():
        if _text(body[field], field) != expected:
            raise ContractError(f"{field} differs")
    return {field: body[field] for field in keys} | _authority(body)


def validate_model_set(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "policy_digest", "candidates",
        "page_orientation_model_digest", "text_line_orientation_model_digest",
    )
    body = _base(value, "ao.lore.private-ocr-model-set.v0.1", keys, "OCR model set")
    candidates = _list(body["candidates"], "candidates", 3, 3)
    detached: list[dict[str, str]] = []
    for index, (candidate, expected) in enumerate(zip(candidates, OCR_CANDIDATES), 1):
        item = _mapping(candidate, ("candidate_id", "detection_model", "recognition_model", "model_set_digest"), "candidate")
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        if candidate_id != f"candidate-{index}":
            raise ContractError("candidate order or identity differs")
        detection = _text(item["detection_model"], "detection_model")
        recognition = _text(item["recognition_model"], "recognition_model")
        if (detection, recognition) != expected:
            raise ContractError("candidate model pair differs")
        detached.append({
            "candidate_id": candidate_id, "detection_model": detection,
            "recognition_model": recognition,
            "model_set_digest": _digest(item["model_set_digest"], "model_set_digest"),
        })
    result: dict[str, object] = {
        "schema_version": body["schema_version"], "policy_digest": body["policy_digest"],
        "candidates": detached,
        "page_orientation_model_digest": _digest(body["page_orientation_model_digest"], "page_orientation_model_digest"),
        "text_line_orientation_model_digest": _digest(body["text_line_orientation_model_digest"], "text_line_orientation_model_digest"),
    }
    return result | _authority(body)


def _page(value: object, *, detections: bool) -> dict[str, object]:
    keys = ("page_id", "width", "height", "detections") if detections else ("page_id", "width", "height", "size", "digest")
    page = _mapping(value, keys, "page")
    result: dict[str, object] = {
        "page_id": _identifier(page["page_id"], "page_id"),
        "width": _integer(page["width"], "width", 1, 25000000),
        "height": _integer(page["height"], "height", 1, 25000000),
    }
    if not detections:
        result["size"] = _integer(page["size"], "size", 1, 104857600)
        result["digest"] = _digest(page["digest"], "digest")
        return result
    found = _list(page["detections"], "detections", 0, 100000)
    output: list[dict[str, object]] = []
    for expected_index, detection in enumerate(found):
        item = _mapping(detection, ("index", "text", "confidence_millionths", "polygon"), "detection")
        index = _integer(item["index"], "index", 0, 99999)
        if index != expected_index:
            raise ContractError("detection order differs")
        points = _list(item["polygon"], "polygon", 4, 4)
        polygon = []
        for point in points:
            pair = _list(point, "polygon point", 2, 2)
            polygon.append([_integer(pair[0], "x", 0, result["width"]), _integer(pair[1], "y", 0, result["height"])])
        output.append({
            "index": index, "text": _ocr_text(item["text"]),
            "confidence_millionths": _integer(item["confidence_millionths"], "confidence", 0, 1000000),
            "polygon": polygon,
        })
    result["detections"] = output
    return result


def validate_raster_manifest(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "policy_digest", "source_id", "source_digest",
        "configuration_digest", "renderer_digest", "dpi", "pages",
    )
    body = _base(value, "ao.lore.private-ocr-raster-manifest.v0.1", keys, "OCR raster manifest")
    pages = [_page(page, detections=False) for page in _list(body["pages"], "pages", 1, 100)]
    if [page["page_id"] for page in pages] != [f"page-{index:04d}" for index in range(1, len(pages) + 1)]:
        raise ContractError("page order differs")
    result: dict[str, object] = {
        "schema_version": body["schema_version"], "policy_digest": body["policy_digest"],
        "source_id": _identifier(body["source_id"], "source_id"),
        "source_digest": _digest(body["source_digest"], "source_digest"),
        "configuration_digest": _digest(body["configuration_digest"], "configuration_digest"),
        "renderer_digest": _digest(body["renderer_digest"], "renderer_digest"),
        "dpi": _integer(body["dpi"], "dpi", 300, 300), "pages": pages,
    }
    return result | _authority(body)


def validate_worker_contract(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "policy_digest", "run_id", "candidate_id", "runtime_digest",
        "model_set_digest", "raster_manifest_digest", "configuration_digest",
        "gpu_identity_digest", "page_ids",
    )
    body = _base(value, "ao.lore.private-ocr-worker-contract.v0.1", keys, "OCR worker contract")
    page_ids = [_identifier(item, "page_id") for item in _list(body["page_ids"], "page_ids", 1, 100)]
    if page_ids != [f"page-{index:04d}" for index in range(1, len(page_ids) + 1)]:
        raise ContractError("worker page order differs")
    result: dict[str, object] = {
        "schema_version": body["schema_version"], "policy_digest": body["policy_digest"],
        "run_id": _identifier(body["run_id"], "run_id"),
        "candidate_id": _identifier(body["candidate_id"], "candidate_id"),
        **{field: _digest(body[field], field) for field in (
            "runtime_digest", "model_set_digest", "raster_manifest_digest",
            "configuration_digest", "gpu_identity_digest",
        )},
        "page_ids": page_ids,
    }
    return result | _authority(body)


def validate_worker_result(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "policy_digest", "run_id", "candidate_id", "runtime_digest",
        "model_set_digest", "configuration_digest", "latency_microseconds",
        "peak_gpu_memory_bytes", "pages",
    )
    body = _base(value, "ao.lore.private-ocr-worker-result.v0.1", keys, "OCR worker result")
    pages = [_page(page, detections=True) for page in _list(body["pages"], "pages", 1, 100)]
    if [page["page_id"] for page in pages] != [f"page-{index:04d}" for index in range(1, len(pages) + 1)]:
        raise ContractError("result page order differs")
    result: dict[str, object] = {
        "schema_version": body["schema_version"], "policy_digest": body["policy_digest"],
        "run_id": _identifier(body["run_id"], "run_id"),
        "candidate_id": _identifier(body["candidate_id"], "candidate_id"),
        "runtime_digest": _digest(body["runtime_digest"], "runtime_digest"),
        "model_set_digest": _digest(body["model_set_digest"], "model_set_digest"),
        "configuration_digest": _digest(body["configuration_digest"], "configuration_digest"),
        "latency_microseconds": _integer(body["latency_microseconds"], "latency_microseconds", 0, 240000000),
        "peak_gpu_memory_bytes": _integer(body["peak_gpu_memory_bytes"], "peak_gpu_memory_bytes", 0, 34359738368),
        "pages": pages,
    }
    return result | _authority(body)


_METRIC_FIELDS = (
    "character_accuracy_millionths", "word_accuracy_millionths",
    "detection_precision_millionths", "detection_recall_millionths",
    "reading_order_pair_accuracy_millionths", "mean_polygon_iou_millionths",
    "page_coverage_millionths", "expected_outcome_accuracy_millionths",
)


def validate_qualification(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "policy_digest", "runtime_digest", "corpus_digest",
        "oracle_digest", "configuration_digest", "candidates",
        "selected_candidate_id", "decision",
    )
    body = _base(value, "ao.lore.private-ocr-qualification.v0.1", keys, "OCR qualification")
    candidates = _list(body["candidates"], "candidates", 3, 3)
    candidate_keys = ("candidate_id", "result_digest") + _METRIC_FIELDS + (
        "hallucinated_lines", "mean_latency_microseconds", "peak_gpu_memory_bytes",
        "repeatability_identical", "all_hard_gates_pass",
    )
    detached = []
    for index, candidate in enumerate(candidates, 1):
        item = _mapping(candidate, candidate_keys, "qualification candidate")
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        if candidate_id != f"candidate-{index}":
            raise ContractError("qualification candidate order differs")
        output: dict[str, object] = {
            "candidate_id": candidate_id, "result_digest": _digest(item["result_digest"], "result_digest"),
        }
        output.update({field: _integer(item[field], field, 0, 1000000) for field in _METRIC_FIELDS})
        output["hallucinated_lines"] = _integer(item["hallucinated_lines"], "hallucinated_lines", 0, 1000000)
        output["mean_latency_microseconds"] = _integer(item["mean_latency_microseconds"], "mean_latency_microseconds", 0, 240000000)
        output["peak_gpu_memory_bytes"] = _integer(item["peak_gpu_memory_bytes"], "peak_gpu_memory_bytes", 0, 34359738368)
        output["repeatability_identical"] = _boolean(item["repeatability_identical"], "repeatability_identical")
        output["all_hard_gates_pass"] = _boolean(item["all_hard_gates_pass"], "all_hard_gates_pass")
        detached.append(output)
    decision = _text(body["decision"], "decision")
    if decision not in OCR_DECISIONS:
        raise ContractError("qualification decision differs")
    selected = _identifier(body["selected_candidate_id"], "selected_candidate_id")
    if selected not in {item["candidate_id"] for item in detached}:
        raise ContractError("selected candidate is absent")
    result: dict[str, object] = {
        "schema_version": body["schema_version"], "policy_digest": body["policy_digest"],
        **{field: _digest(body[field], field) for field in (
            "runtime_digest", "corpus_digest", "oracle_digest", "configuration_digest",
        )},
        "candidates": detached, "selected_candidate_id": selected, "decision": decision,
    }
    return result | _authority(body)


def validate_uat_readback(value: object) -> dict[str, object]:
    keys = (
        "schema_version", "policy_digest", "qualification_digest", "corpus_digest",
        "aggregate_digest", "initial_conversions", "resumed_conversions", "rerun_conversions",
        "successful_documents", "rejected_documents", "calibration_attempts_per_item",
        "decision", "brain_before_digest", "brain_after_work_digest", "brain_after_cleanup_digest",
    )
    body = _base(value, "ao.lore.private-ocr-uat-readback.v0.1", keys, "OCR UAT readback")
    initial = _integer(body["initial_conversions"], "initial_conversions", 0, 4)
    resumed = _integer(body["resumed_conversions"], "resumed_conversions", 0, 4)
    rerun = _integer(body["rerun_conversions"], "rerun_conversions", 0, 4)
    successful = _integer(body["successful_documents"], "successful_documents", 0, 4)
    rejected = _integer(body["rejected_documents"], "rejected_documents", 0, 4)
    if (initial, resumed, rerun, successful, rejected) != (1, 3, 0, 4, 0):
        raise ContractError("OCR UAT arithmetic differs")
    decision = _text(body["decision"], "decision")
    if decision not in OCR_DECISIONS:
        raise ContractError("OCR UAT decision differs")
    brain = tuple(_digest(body[field], field) for field in (
        "brain_before_digest", "brain_after_work_digest", "brain_after_cleanup_digest",
    ))
    if len(set(brain)) != 1:
        raise ContractError("brain digests differ")
    result: dict[str, object] = {
        "schema_version": body["schema_version"], "policy_digest": body["policy_digest"],
        **{field: _digest(body[field], field) for field in (
            "qualification_digest", "corpus_digest", "aggregate_digest",
        )},
        "initial_conversions": initial, "resumed_conversions": resumed,
        "rerun_conversions": rerun, "successful_documents": successful,
        "rejected_documents": rejected,
        "calibration_attempts_per_item": _integer(body["calibration_attempts_per_item"], "calibration_attempts_per_item", 2, 2),
        "decision": decision,
        "brain_before_digest": brain[0], "brain_after_work_digest": brain[1],
        "brain_after_cleanup_digest": brain[2],
    }
    return result | _authority(body)
