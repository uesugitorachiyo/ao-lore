"""Strict calibration-worker contract boundary for private English OCR UAT."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from ._strict_io import parse_strict_json
from .benchmark import canonical_digest
from .ocr_contracts import AUTHORITY_FIELDS, validate_worker_result
from .paddle_ocr import OcrRuntimeBinding, run_paddle_ocr_worker
from .private_ocr_uat_worker import _ORIGIN_KEYS, PrivateOcrWorkerError, validate_origin_fields
from .private_ocr_uat_worker import _load_activation, _load_rasters, _read_bounded


def validate_calibration_contract(value: object) -> dict[str, Any]:
    keys = {"schema_version", "attempts_per_item"} | _ORIGIN_KEYS
    if type(value) is not dict or set(value) != keys:
        raise PrivateOcrWorkerError("private OCR calibration contract is invalid")
    if (
        value["schema_version"] != "ao.lore.private-ocr-calibration-contract.v0.1"
        or type(value["attempts_per_item"]) is not int
        or value["attempts_per_item"] != 2
    ):
        raise PrivateOcrWorkerError("private OCR calibration contract is invalid")
    origin = validate_origin_fields({key: value[key] for key in _ORIGIN_KEYS})
    return {
        "schema_version": value["schema_version"],
        "attempts_per_item": 2,
        **origin,
    }


def load_immutable_calibration_contract(
    path: Path = Path("/launch/worker-contract.json"),
    *,
    mount_bound: bool = False,
) -> dict[str, Any]:
    if type(mount_bound) is not bool:
        raise PrivateOcrWorkerError(
            "private OCR calibration contract is invalid"
        )
    body = _read_bounded(
        path, 1024 * 1024, "calibration contract",
        expected_nlink=0 if mount_bound else 1,
    )
    try:
        value = validate_calibration_contract(
            parse_strict_json(body, "private OCR immutable calibration contract")
        )
        expected = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    except (PrivateOcrWorkerError, TypeError, ValueError) as exc:
        raise PrivateOcrWorkerError(
            "private OCR calibration contract is invalid"
        ) from exc
    if body != expected:
        raise PrivateOcrWorkerError("private OCR calibration contract is invalid")
    return value


def _semantic_result(value: object, *, run_id: str) -> tuple[str, int, int]:
    result = validate_worker_result(value)
    if result["run_id"] != run_id:
        raise PrivateOcrWorkerError("private OCR calibration result is invalid")
    detached = {
        key: item for key, item in result.items()
        if key not in {"run_id", "latency_microseconds", "peak_gpu_memory_bytes"}
    }
    lines = sum(len(page["detections"]) for page in result["pages"])
    return canonical_digest(detached), len(result["pages"]), lines


def calibrate_contract(contract: object) -> dict[str, Any]:
    """Run the selected model exactly twice per representative raster bundle."""

    checked = validate_calibration_contract(contract)
    activation, gpu_digest = _load_activation(checked)
    bundles = _load_rasters(checked)
    runtime = OcrRuntimeBinding(
        Path("/ocr-runtime"), activation.runtime_digest, gpu_digest
    )
    items: list[dict[str, Any]] = []
    any_failure = False
    any_weakness = False
    for source in checked["sources"]:
        bundle = bundles[source["source_digest"]]
        digests: list[str] = []
        pages = len(bundle.pages)
        detected_lines = 0
        failed = False
        for attempt in (1, 2):
            run_id = "run-" + canonical_digest({
                "batch_id": checked["batch_id"],
                "item_id": source["item_id"],
                "calibration_attempt": attempt,
            })[7:39]
            try:
                result = run_paddle_ocr_worker(
                    runtime, bundle, activation.candidate_id, run_id=run_id,
                    parent_network_isolated=True,
                )
                digest, observed_pages, lines = _semantic_result(result, run_id=run_id)
                if observed_pages != pages:
                    raise PrivateOcrWorkerError(
                        "private OCR calibration result is invalid"
                    )
                detected_lines = lines if attempt == 1 else detected_lines
                digests.append(digest)
            except Exception:
                failed = True
                digests.append(canonical_digest({
                    "failure": "worker_failure", "attempt": attempt,
                }))
        identical = not failed and len(set(digests)) == 1
        any_failure = any_failure or failed or not identical
        any_weakness = any_weakness or (not failed and detected_lines == 0)
        items.append({
            "item_id": source["item_id"],
            "raster_manifest_digest": source["raster_manifest_digest"],
            "page_count": pages,
            "detected_lines": detected_lines,
            "attempt_digests": digests,
            "identical": identical,
        })
    decision = (
        "investigate" if any_failure
        else "candidate_change" if any_weakness
        else "hold"
    )
    aggregate = {
        "schema_version": "ao.lore.private-ocr-calibration-aggregate.v0.1",
        "attempts_per_item": 2,
        "items": items,
        "decision": decision,
        "qualification_digest": checked["qualification_digest"],
        "campaign_origin_digest": checked["campaign_origin_digest"],
        **{field: False for field in AUTHORITY_FIELDS},
    }
    return {
        "schema_version": "ao.lore.private-ocr-calibration-result.v0.1",
        "attempts_per_item": 2,
        "aggregate_digest": canonical_digest(aggregate),
        "decision": decision,
        "items": items,
        **{field: False for field in AUTHORITY_FIELDS},
    }


def validate_calibration_result(
    value: object, *, contract: Mapping[str, Any],
) -> dict[str, Any]:
    checked_contract = validate_calibration_contract(contract)
    keys = {
        "schema_version", "attempts_per_item", "aggregate_digest",
        "decision", "items", *AUTHORITY_FIELDS,
    }
    if type(value) is not dict or set(value) != keys:
        raise PrivateOcrWorkerError("private OCR calibration result is invalid")
    if (
        value["schema_version"] != "ao.lore.private-ocr-calibration-result.v0.1"
        or value["attempts_per_item"] != 2
        or value["decision"] not in {"hold", "candidate_change", "investigate"}
        or any(value[field] is not False for field in AUTHORITY_FIELDS)
        or type(value["aggregate_digest"]) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value["aggregate_digest"]) is None
        or type(value["items"]) is not list
        or len(value["items"]) != 4
    ):
        raise PrivateOcrWorkerError("private OCR calibration result is invalid")
    detached: list[dict[str, Any]] = []
    for expected, item in zip(checked_contract["sources"], value["items"], strict=True):
        if (
            type(item) is not dict
            or set(item) != {
                "item_id", "raster_manifest_digest", "page_count",
                "detected_lines", "attempt_digests", "identical",
            }
            or item["item_id"] != expected["item_id"]
            or item["raster_manifest_digest"] != expected["raster_manifest_digest"]
            or type(item["page_count"]) is not int
            or not 1 <= item["page_count"] <= 100
            or type(item["detected_lines"]) is not int
            or not 0 <= item["detected_lines"] <= 1_000_000
            or type(item["attempt_digests"]) is not list
            or len(item["attempt_digests"]) != 2
            or any(
                type(digest) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                for digest in item["attempt_digests"]
            )
            or type(item["identical"]) is not bool
            or item["identical"]
            != (len(set(item["attempt_digests"])) == 1 and item["detected_lines"] >= 0)
        ):
            raise PrivateOcrWorkerError("private OCR calibration result is invalid")
        detached.append(dict(item))
    if value["decision"] == "hold" and any(
        not item["identical"] or item["detected_lines"] == 0 for item in detached
    ):
        raise PrivateOcrWorkerError("private OCR calibration result is invalid")
    expected_aggregate = canonical_digest({
        "schema_version": "ao.lore.private-ocr-calibration-aggregate.v0.1",
        "attempts_per_item": 2,
        "items": detached,
        "decision": value["decision"],
        "qualification_digest": checked_contract["qualification_digest"],
        "campaign_origin_digest": checked_contract["campaign_origin_digest"],
        **{field: False for field in AUTHORITY_FIELDS},
    })
    if value["aggregate_digest"] != expected_aggregate:
        raise PrivateOcrWorkerError("private OCR calibration result is invalid")
    return {
        "schema_version": value["schema_version"],
        "attempts_per_item": 2,
        "aggregate_digest": value["aggregate_digest"],
        "decision": value["decision"],
        "items": detached,
        **{field: False for field in AUTHORITY_FIELDS},
    }


def main() -> int:
    result = calibrate_contract(
        load_immutable_calibration_contract(mount_bound=True)
    )
    body = json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    view = memoryview(body)
    while view:
        written = os.write(1, view)
        if written <= 0:
            raise PrivateOcrWorkerError("private OCR calibration output failed")
        view = view[written:]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
