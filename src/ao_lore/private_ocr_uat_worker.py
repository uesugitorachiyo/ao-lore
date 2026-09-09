"""Strict batch-worker contract boundary for private English OCR UAT."""

from __future__ import annotations

import json
import os
import re
import signal
import stat
from pathlib import Path
from typing import Any, Mapping

from ._strict_io import ContractError, parse_strict_json
from .batch_ingestion import (
    BatchDependencies,
    append_terminal_item,
    ingest_verified_batch,
    validate_batch_manifest,
    validate_checkpoint,
)
from .benchmark import canonical_digest
from .ingestion import (
    default_ocr_ingestion_dependencies,
    ingest_ocr_document,
    read_verified_pdf_source,
)
from .model_roles import ActivatedOcrParser
from .ocr_contracts import (
    AUTHORITY_FIELDS, validate_qualification, validate_raster_manifest,
)
from .ocr_raster import ReviewedOcrSource, _load_bundle
from .ocr_runtime import probe_offline_gpu_runtime
from .paddle_ocr import OcrRuntimeBinding, run_paddle_ocr_worker


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_BATCH = re.compile(r"^batch-[a-z0-9][a-z0-9-]{0,62}$")
_STAGING = re.compile(r"^private-ocr-uat-[0-9a-f]{24}$|^private-ocr-uat-fixture$")
_PHASES = {"interrupted": (0, 1), "resume": (1, 4), "rerun": (4, 4)}
_ORIGIN_KEYS = {
    "qualification_digest", "runtime_digest", "model_set_digest",
    "selected_candidate_id", "campaign_origin_digest", "configuration_digest",
    "batch_id", "batch_manifest_digest", "sources", *AUTHORITY_FIELDS,
    "batch_manifest", "staging_name",
}


class PrivateOcrWorkerError(ContractError):
    """Raised when a private OCR UAT worker contract is invalid."""


def _fail() -> PrivateOcrWorkerError:
    return PrivateOcrWorkerError("private OCR UAT worker contract is invalid")


def _require_digest(value: Any) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _fail()
    return value


def validate_origin_fields(value: Mapping[str, Any]) -> dict[str, Any]:
    """Detach and validate the common path-free UAT origin fields."""

    if type(value) is not dict or set(value) != _ORIGIN_KEYS:
        raise _fail()
    for field in AUTHORITY_FIELDS:
        if value[field] is not False:
            raise _fail()
    selected = value["selected_candidate_id"]
    batch_id = value["batch_id"]
    staging_name = value["staging_name"]
    if (
        type(selected) is not str
        or selected not in {"candidate-1", "candidate-2", "candidate-3"}
        or type(batch_id) is not str
        or _BATCH.fullmatch(batch_id) is None
        or type(staging_name) is not str
        or _STAGING.fullmatch(staging_name) is None
    ):
        raise _fail()
    sources = value["sources"]
    if type(sources) is not list or len(sources) != 4:
        raise _fail()
    detached_sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for source in sources:
        if type(source) is not dict or set(source) != {
            "item_id", "source_digest", "raster_manifest_digest"
        }:
            raise _fail()
        item_id = source["item_id"]
        if type(item_id) is not str or _IDENTIFIER.fullmatch(item_id) is None or item_id in seen:
            raise _fail()
        seen.add(item_id)
        detached_sources.append({
            "item_id": item_id,
            "source_digest": _require_digest(source["source_digest"]),
            "raster_manifest_digest": _require_digest(source["raster_manifest_digest"]),
        })
    try:
        batch_manifest = validate_batch_manifest(value["batch_manifest"])
    except (ContractError, TypeError, ValueError) as exc:
        raise _fail() from exc
    if (
        batch_manifest["batch_id"] != batch_id
        or canonical_digest(batch_manifest) != value["batch_manifest_digest"]
        or [item["item_id"] for item in batch_manifest["documents"]]
        != [item["item_id"] for item in detached_sources]
    ):
        raise _fail()
    result = {
        field: _require_digest(value[field])
        for field in (
            "qualification_digest", "runtime_digest", "model_set_digest",
            "campaign_origin_digest", "configuration_digest", "batch_manifest_digest",
        )
    }
    result.update({
        "selected_candidate_id": selected,
        "batch_id": batch_id,
        "batch_manifest": batch_manifest,
        "staging_name": staging_name,
        "sources": detached_sources,
        **{field: False for field in AUTHORITY_FIELDS},
    })
    return result


def validate_uat_worker_contract(value: object) -> dict[str, Any]:
    keys = {"schema_version", "phase", "expected_start", "expected_end"} | _ORIGIN_KEYS
    if type(value) is not dict or set(value) != keys:
        raise _fail()
    phase = value["phase"]
    if type(phase) is not str or phase not in _PHASES:
        raise _fail()
    start, end = _PHASES[phase]
    if (
        type(value["expected_start"]) is not int
        or type(value["expected_end"]) is not int
        or (value["expected_start"], value["expected_end"]) != (start, end)
        or value["schema_version"] != "ao.lore.private-ocr-uat-worker-contract.v0.1"
    ):
        raise _fail()
    origin = validate_origin_fields({key: value[key] for key in _ORIGIN_KEYS})
    return {
        "schema_version": value["schema_version"],
        "phase": phase,
        "expected_start": start,
        "expected_end": end,
        **origin,
    }


def load_immutable_contract(
    path: Path = Path("/launch/worker-contract.json"),
    *,
    mount_bound: bool = False,
) -> dict[str, Any]:
    """Descriptor-read and byte-bind the sole phase launch contract."""

    if type(mount_bound) is not bool:
        raise _fail()
    expected_nlink = 0 if mount_bound else 1
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != expected_nlink
            or not 1 <= before.st_size <= 1024 * 1024
        ):
            raise OSError("contract identity differs")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        body = b""
        while len(body) <= 1024 * 1024:
            chunk = os.read(descriptor, min(64 * 1024, 1024 * 1024 + 1 - len(body)))
            if not chunk:
                break
            body += chunk
        after = os.fstat(descriptor)
        public = os.lstat(path)
        if (
            len(body) > 1024 * 1024
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (public.st_dev, public.st_ino)
        ):
            raise OSError("contract drifted")
        value = validate_uat_worker_contract(
            parse_strict_json(body, "private OCR immutable phase contract")
        )
        expected = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if body != expected:
            raise OSError("contract bytes differ")
        return value
    except (ContractError, OSError, TypeError, ValueError) as exc:
        raise _fail() from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


_SELECTION_PROFILE = {
    "profile_id": "ocr-select-v1",
    "weights": {"text_fidelity": 1.0},
    "penalty_weights": {},
    "normalization_bounds": {},
    "missing_optional_policy": "ineligible",
    "tie_break_order": ["parser_id"],
}
_QUALITY_PROFILE = {
    "profile_id": "ocr-quality-v1",
    "component_weights": {
        "text_coverage": 0.4,
        "structural_completeness": 0.3,
        "source_span_coverage": 0.2,
        "document_ir_valid": 0.1,
    },
    "accept_threshold": 0.8,
    "fallback_threshold": 0.7,
    "maximum_fallbacks": 0,
    "critical_failures": [
        "invalid_ir", "source_digest_mismatch", "parser_crash",
        "no_readable_content", "lost_provenance",
    ],
    "intermediate_decision": "quarantine",
    "exhausted_decision": "reject",
}


def _read_bounded(
    path: Path, maximum: int, label: str, *, expected_nlink: int = 1,
) -> bytes:
    if type(expected_nlink) is not int or expected_nlink not in {0, 1}:
        raise _fail()
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != expected_nlink
            or not 1 <= before.st_size <= maximum
        ):
            raise OSError(f"{label} identity differs")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        body = b""
        while len(body) <= maximum:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(body)))
            if not chunk:
                break
            body += chunk
        after = os.fstat(descriptor)
        public = os.lstat(path)
        if (
            len(body) > maximum
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (public.st_dev, public.st_ino)
        ):
            raise OSError(f"{label} drifted")
        return body
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _load_activation(contract: Mapping[str, Any]) -> tuple[ActivatedOcrParser, str]:
    from .private_ocr_uat import OcrActivationInputs, load_private_ocr_activation

    root = Path("/app/.ao-lore/evidence/private-ocr-activation/current")
    qualification_body = _read_bounded(root / "qualification.json", 1024 * 1024, "qualification")
    activation_body = _read_bounded(root / "activation.json", 512 * 1024, "activation")
    record = parse_strict_json(activation_body, "private OCR activation")
    if type(record) is not dict:
        raise _fail()
    try:
        inputs = OcrActivationInputs(
            qualification_body=qualification_body,
            qualification_digest=record["qualification_digest"],
            runtime_digest=record["runtime_digest"],
            corpus_digest=record["corpus_digest"],
            corpus_configuration_digest=record["corpus_configuration_digest"],
            oracle_digest=record["oracle_digest"],
            model_digests=tuple(record["model_digests"]),
            selected_result_digest=record["selected_result_digest"],
        )
        activation = load_private_ocr_activation(
            runtime_root=Path("/app/.ao-lore"), inputs=inputs
        )
        qualification = validate_qualification(
            parse_strict_json(qualification_body, "private OCR qualification")
        )
        gpu_digest = _require_digest(
            probe_offline_gpu_runtime(
                Path("/ocr-runtime"), parent_network_isolated=True,
            ).device_uuid_digest
        )
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise _fail() from exc
    if (
        activation.qualification_digest != contract["qualification_digest"]
        or activation.runtime_digest != contract["runtime_digest"]
        or activation.model_set_digest != contract["model_set_digest"]
        or activation.candidate_id != contract["selected_candidate_id"]
    ):
        raise _fail()
    return activation, gpu_digest


def _load_rasters(contract: Mapping[str, Any]) -> dict[str, Any]:
    bundles: dict[str, Any] = {}
    for source in contract["sources"]:
        root = Path("/rasters") / (
            f"raster-{source['item_id']}-{source['source_digest'][7:23]}"
        )
        raw = _read_bounded(root / "manifest.json", 1024 * 1024, "raster manifest")
        manifest = validate_raster_manifest(
            parse_strict_json(raw, "private OCR raster manifest")
        )
        reviewed = ReviewedOcrSource(
            source["item_id"], Path("/unopened"), "application/pdf",
            source["source_digest"], True,
        )
        bundle = _load_bundle(root, reviewed, manifest["renderer_digest"])
        if bundle.manifest_digest != source["raster_manifest_digest"]:
            raise _fail()
        bundles[source["source_digest"]] = bundle
    if len(bundles) != 4:
        raise _fail()
    return bundles


def _current_processed(contract: Mapping[str, Any]) -> int:
    path = Path("/app/.ao-lore/batches") / contract["batch_id"] / "checkpoint.json"
    try:
        body = _read_bounded(path, 1024 * 1024, "phase checkpoint")
    except FileNotFoundError:
        return 0
    checkpoint = validate_checkpoint(
        parse_strict_json(body, "private OCR phase checkpoint"),
        contract["batch_manifest"], contract["batch_manifest_digest"],
    )
    return checkpoint["processed"]


def _write_barrier(contract: Mapping[str, Any], checkpoint: Mapping[str, Any]) -> None:
    path = (
        Path("/app/.ao-lore/uat/private-ocr")
        / contract["batch_id"] / "worker-barrier.json"
    )
    body = json.dumps({
        "schema_version": "ao.lore.private-ocr-worker-barrier.v0.1",
        "batch_id": contract["batch_id"],
        "phase": "interrupted",
        "processed": 1,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "contract_digest": canonical_digest(contract),
        "authority": False,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("barrier short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def execute_phase(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Execute one exact 0→1, 1→4, or 4→4 batch phase."""

    checked = validate_uat_worker_contract(contract)
    if _current_processed(checked) != checked["expected_start"]:
        raise _fail()
    activation, gpu_digest = _load_activation(checked)
    bundles = _load_rasters(checked)
    runtime = OcrRuntimeBinding(
        Path("/ocr-runtime"), activation.runtime_digest, gpu_digest
    )

    def execute(source: Mapping[str, Any]) -> object:
        if type(source) is not dict or type(source.get("digest")) is not str:
            raise _fail()
        bundle = bundles.get(source["digest"])
        if bundle is None:
            raise _fail()
        run_id = "run-" + canonical_digest({
            "batch_id": checked["batch_id"],
            "item_id": bundle.source_id,
            "phase": checked["phase"],
        })[7:39]
        return run_paddle_ocr_worker(
            runtime, bundle, activation.candidate_id,
            run_id=run_id,
            parent_network_isolated=True,
        )

    dependencies = default_ocr_ingestion_dependencies(activation, execute)
    appender = append_terminal_item
    if checked["phase"] == "interrupted":
        def interrupting_appender(checkpoint, item, **kwargs):
            advanced = append_terminal_item(checkpoint, item, **kwargs)
            if advanced["processed"] != 1:
                raise _fail()
            _write_barrier(checked, advanced)
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.pause()
            raise _fail()
        appender = interrupting_appender
    batch_dependencies = BatchDependencies(
        benchmark_manifest={
            "qualification_digest": activation.qualification_digest,
            "selected_candidate_id": activation.candidate_id,
            "model_set_digest": activation.model_set_digest,
        },
        selection_profile=_SELECTION_PROFILE,
        quality_profile=_QUALITY_PROFILE,
        format_id="ocr", media_type="application/pdf", extension=".pdf",
        parser_id="paddle-ocr-english", parser_version="0.1.0",
        ingest_one=ingest_ocr_document,
        read_source=read_verified_pdf_source,
        append_checkpoint=appender,
        ingestion_dependencies=dependencies,
        candidate_root=Path("/app/working/candidates") / checked["staging_name"],
    )
    result = ingest_verified_batch(
        checked["batch_manifest"], checked["batch_manifest_digest"],
        dependencies=batch_dependencies,
        batch_root=Path("/app/.ao-lore/batches") / checked["batch_id"],
    )
    if _current_processed(checked) != checked["expected_end"]:
        raise _fail()
    return result


def main() -> int:
    contract = load_immutable_contract(mount_bound=True)
    result = execute_phase(contract)
    body = json.dumps(
        result, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    view = memoryview(body)
    while view:
        written = os.write(1, view)
        if written <= 0:
            raise _fail()
        view = view[written:]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
