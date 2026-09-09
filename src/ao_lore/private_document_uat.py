"""Shared lifecycle orchestration for AO Lore private document UAT."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ._strict_io import (
    ContractError,
    ensure_contained,
    require_exact_keys,
    require_identifier,
)
from .batch_ingestion import validate_checkpoint
from .benchmark import canonical_digest
from .home import repository_root, runtime_home

_DIGEST_RE = r"^sha256:[0-9a-f]{64}$"


class PrivateDocumentUatError(ContractError):
    """Raised when shared private-document UAT orchestration drifts."""


def _fail(message: str = "private document UAT contract is invalid") -> PrivateDocumentUatError:
    return PrivateDocumentUatError(message)


def _strict_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":"))
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise _fail() from exc
    if not isinstance(decoded, dict):
        raise _fail()
    return decoded


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise _fail(f"{label} is invalid")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise _fail(f"{label} is invalid") from exc
    return value


def _snapshot(value: Any, label: str) -> str:
    return _digest(value, label)


def _validate_mapping_callback(
    callback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    value: Any,
    label: str,
) -> dict[str, Any]:
    pristine = _strict_copy(value)
    validated = callback(_strict_copy(pristine))
    detached = _strict_copy(validated)
    if detached != pristine:
        raise _fail(f"{label} is invalid")
    return detached


@dataclass(frozen=True)
class PrivateDocumentCampaignPolicy:
    format_id: str
    corpus_id: str
    media_type: str
    extension: str
    parser_id: str
    parser_version: str
    worker_module: str
    calibration_worker_module: str
    evidence_namespace: str
    validate_manifest: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    validate_qualification: Callable[[Mapping[str, Any]], Mapping[str, Any]]
    validate_calibration: Callable[[Any], Any]


def private_document_policy_digest(policy: PrivateDocumentCampaignPolicy) -> str:
    return canonical_digest(
        {
            "format_id": policy.format_id,
            "corpus_id": policy.corpus_id,
            "media_type": policy.media_type,
            "extension": policy.extension,
            "parser_id": policy.parser_id,
            "parser_version": policy.parser_version,
            "worker_module": policy.worker_module,
            "calibration_worker_module": policy.calibration_worker_module,
            "evidence_namespace": policy.evidence_namespace,
        }
    )


def private_document_state_body(
    policy: PrivateDocumentCampaignPolicy, **fields: Any
) -> dict[str, Any]:
    if any(name in {"format_id", "policy_digest"} for name in fields):
        raise _fail()
    body = {
        "format_id": policy.format_id,
        "policy_digest": private_document_policy_digest(policy),
    }
    body.update(fields)
    return body


@dataclass(frozen=True)
class PrivateDocumentPreparedRun:
    format_id: str
    policy_digest: str
    batch_id: str
    manifest: Mapping[str, Any]
    manifest_digest: str
    manifest_identity: tuple[int, int]
    staging_name: str
    control_identity: tuple[int, int]
    source_bindings: tuple[Mapping[str, Any], ...]
    qualification_binding: Mapping[str, Any]
    expectation_binding: Mapping[str, Any]


@dataclass(frozen=True)
class PrivateDocumentUatDependencies:
    policy: PrivateDocumentCampaignPolicy
    prepare_run: Callable[[Mapping[str, Any], Path], Any]
    launch_process: Callable[[Any, str], Any]
    calibration: Callable[[Mapping[str, Any], Path], Any]
    cleanup: Callable[[Any], bool]
    brain_snapshot: Callable[[], str]
    candidate_snapshot: Callable[[], Mapping[str, Mapping[str, Any]]]
    load_manifest: Callable[[Path], Mapping[str, Any]] | None = None
    validate_prepared_run: Callable[[Any, Mapping[str, Any], Path], Any] | None = None
    project_run: Callable[[Any], PrivateDocumentPreparedRun] | None = None
    conversion_snapshot: Callable[[], int] | None = None
    process_poll: Callable[[Any], int | None] | None = None
    inspect_checkpoint: Callable[[Any], Mapping[str, Any] | None] | None = None
    inspect_barrier: Callable[[Any, Mapping[str, Any]], bool] | None = None
    send_signal: Callable[[Any], None] | None = None
    terminate_process: Callable[[Any], None] | None = None
    wait_process: Callable[[Any], Mapping[str, Any]] | None = None
    load_final: Callable[[Any], Mapping[str, Any] | None] | None = None
    load_queue: Callable[[], Sequence[Mapping[str, Any]]] | None = None
    verify_terminal: Callable[[Any, bytes, Mapping[str, Any] | None], Mapping[str, Any]] | None = None
    verify_queue: Callable[[Any, Mapping[str, Any], Mapping[str, Mapping[str, Any]]], list[dict[str, Any]]] | None = None
    assemble_readback: Callable[[Any, Mapping[str, Any], Mapping[str, Any], str, Any, Mapping[str, int]], dict[str, Any]] | None = None
    persist_readback: Callable[[Any, Mapping[str, Any], str, str], str] | None = None
    require_verified_process: Callable[[Any, str], None] | None = None
    expected_item_ids: Callable[[Mapping[str, Any]], Sequence[str]] | None = None
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    checkpoint_timeout: float = 120.0
    poll_interval: float = 0.05


def _validate_dependencies(
    value: Any,
) -> PrivateDocumentUatDependencies:
    if not isinstance(value, PrivateDocumentUatDependencies):
        raise _fail("private document UAT dependencies are invalid")
    callbacks = (
        value.policy.validate_manifest,
        value.policy.validate_qualification,
        value.policy.validate_calibration,
        value.prepare_run,
        value.launch_process,
        value.calibration,
        value.cleanup,
        value.brain_snapshot,
        value.candidate_snapshot,
        value.load_manifest,
        value.validate_prepared_run,
        value.project_run,
        value.conversion_snapshot,
        value.process_poll,
        value.inspect_checkpoint,
        value.inspect_barrier,
        value.send_signal,
        value.terminate_process,
        value.wait_process,
        value.load_final,
        value.load_queue,
        value.verify_terminal,
        value.verify_queue,
        value.assemble_readback,
        value.persist_readback,
        value.require_verified_process,
        value.expected_item_ids,
        value.monotonic,
        value.sleep,
    )
    if any(not callable(callback) for callback in callbacks):
        raise _fail("private document UAT dependencies are invalid")
    for number in (value.checkpoint_timeout, value.poll_interval):
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            raise _fail("private document UAT dependencies are invalid")
    if value.checkpoint_timeout <= 0 or not 0 < value.poll_interval <= value.checkpoint_timeout:
        raise _fail("private document UAT dependencies are invalid")
    return value


def _state_prefix(
    value: Mapping[str, Any], label: str, policy: PrivateDocumentCampaignPolicy
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _fail(f"{label} is invalid")
    copied = _strict_copy(value)
    if (
        copied.get("format_id") != policy.format_id
        or copied.get("policy_digest") != private_document_policy_digest(policy)
    ):
        raise _fail(f"{label} is invalid")
    return copied


def _identity_list(value: Any, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
    ):
        raise _fail(f"{label} is invalid")
    return value[0], value[1]


def _identity(value: Any, label: str) -> tuple[int, int]:
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or any(not isinstance(part, int) or isinstance(part, bool) or part < 0 for part in value)
    ):
        raise _fail(f"{label} is invalid")
    return value


def _validate_shared_run(
    value: PrivateDocumentPreparedRun,
    manifest: Mapping[str, Any],
    policy: PrivateDocumentCampaignPolicy,
) -> PrivateDocumentPreparedRun:
    if not isinstance(value, PrivateDocumentPreparedRun):
        raise _fail("private document UAT prepared run is invalid")
    if (
        value.format_id != policy.format_id
        or value.policy_digest != private_document_policy_digest(policy)
        or value.manifest_digest != canonical_digest(manifest)
        or _strict_copy(value.manifest) != _strict_copy(manifest)
    ):
        raise _fail("private document UAT prepared run is invalid")
    require_identifier(value.batch_id, "private document UAT batch identity")
    if not isinstance(value.staging_name, str) or not value.staging_name:
        raise _fail("private document UAT prepared run is invalid")
    _identity(value.control_identity, "private document UAT control identity")
    if not isinstance(value.source_bindings, tuple) or not value.source_bindings:
        raise _fail("private document UAT source bindings are invalid")
    _identity(value.manifest_identity, "private document UAT manifest identity")
    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) != len(value.source_bindings):
        raise _fail("private document UAT source bindings are invalid")
    validated_sources: list[dict[str, Any]] = []
    for document, binding in zip(documents, value.source_bindings, strict=True):
        copied = _state_prefix(binding, "private document UAT source binding", policy)
        require_exact_keys(
            copied,
            ("format_id", "policy_digest", "item_id", "source_digest"),
            "private document UAT source binding",
        )
        if (
            copied["item_id"] != document.get("item_id")
            or copied["source_digest"] != document.get("source_digest")
        ):
            raise _fail("private document UAT source bindings are invalid")
        validated_sources.append(copied)
    qualification_binding = _state_prefix(
        value.qualification_binding,
        "private document UAT qualification binding",
        policy,
    )
    require_exact_keys(
        qualification_binding,
        ("format_id", "policy_digest", "manifest_digest", "manifest_identity", "qualification"),
        "private document UAT qualification binding",
    )
    if (
        qualification_binding["manifest_digest"] != value.manifest_digest
        or _identity_list(
            qualification_binding["manifest_identity"],
            "private document UAT qualification binding manifest identity",
        )
        != value.manifest_identity
    ):
        raise _fail("private document UAT qualification binding is invalid")
    qualification_value = _validate_mapping_callback(
        policy.validate_qualification,
        qualification_binding["qualification"],
        "private document UAT qualification binding",
    )
    expectation_binding = _state_prefix(
        value.expectation_binding,
        "private document UAT expectation binding",
        policy,
    )
    require_exact_keys(
        expectation_binding,
        (
            "format_id",
            "policy_digest",
            "manifest_digest",
            "manifest_identity",
            "item_ids",
            "source_digests",
        ),
        "private document UAT expectation binding",
    )
    if (
        expectation_binding["manifest_digest"] != value.manifest_digest
        or _identity_list(
            expectation_binding["manifest_identity"],
            "private document UAT expectation binding manifest identity",
        )
        != value.manifest_identity
    ):
        raise _fail("private document UAT expectation binding is invalid")
    if (
        not isinstance(expectation_binding["item_ids"], list)
        or not isinstance(expectation_binding["source_digests"], list)
        or expectation_binding["item_ids"]
        != [document.get("item_id") for document in documents]
        or expectation_binding["source_digests"]
        != [document.get("source_digest") for document in documents]
    ):
        raise _fail("private document UAT expectation binding is invalid")
    return value


def _candidate_snapshot(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or len(value) > 10_000:
        raise _fail("private document UAT candidate snapshot is invalid")
    result: dict[str, dict[str, Any]] = {}
    exact_keys = {
        "candidate_id",
        "candidate_digest",
        "provenance_digest",
        "source_digest",
        "review_status",
    }
    for candidate_id, supplied in value.items():
        identifier = require_identifier(candidate_id, "private document UAT candidate identity")
        if (
            not identifier.startswith("candidate-")
            or not isinstance(supplied, Mapping)
            or set(supplied) != exact_keys
            or supplied.get("candidate_id") != identifier
        ):
            raise _fail("private document UAT candidate snapshot is invalid")
        item = _strict_copy(supplied)
        for field in ("candidate_digest", "provenance_digest", "source_digest"):
            _digest(item[field], f"private document UAT candidate {field}")
        if item["review_status"] not in {"unreviewed", "accepted", "rejected"}:
            raise _fail("private document UAT candidate snapshot is invalid")
        result[identifier] = item
    return result


def _process_result(value: Any) -> tuple[int, bytes, bytes]:
    if not isinstance(value, Mapping) or set(value) != {"returncode", "stdout", "stderr"}:
        raise _fail("private document UAT process result is invalid")
    returncode = value["returncode"]
    stdout = value["stdout"]
    stderr = value["stderr"]
    if (
        not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or type(stdout) is not bytes
        or type(stderr) is not bytes
    ):
        raise _fail("private document UAT process result is invalid")
    return returncode, stdout, stderr


def _conversion_snapshot(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise _fail("private document UAT conversion journal is invalid")
    return value


def _wrap_failure(exc: BaseException) -> PrivateDocumentUatError:
    if isinstance(exc, PrivateDocumentUatError) and str(exc) == "private document UAT failed":
        return exc
    return PrivateDocumentUatError("private document UAT failed")


def run_private_document_uat(
    *,
    runtime_root: Path | None = None,
    dependencies: PrivateDocumentUatDependencies,
) -> dict[str, Any]:
    selected_runtime = runtime_home() if runtime_root is None else Path(runtime_root)
    selected_runtime, _ = ensure_contained(
        selected_runtime, repository_root(), "private document UAT runtime root"
    )
    deps = _validate_dependencies(dependencies)
    run: Any | None = None
    active_process: Any | None = None
    cleanup_attempted = False
    primary: BaseException | None = None
    try:
        manifest = _validate_mapping_callback(
            deps.policy.validate_manifest,
            deps.load_manifest(selected_runtime),
            "private document UAT manifest",
        )
        expected_ids = list(deps.expected_item_ids(manifest))
        run = deps.prepare_run(_strict_copy(manifest), selected_runtime)
        run = deps.validate_prepared_run(run, manifest, selected_runtime)
        shared_run = _validate_shared_run(deps.project_run(run), manifest, deps.policy)

        def require_current_run() -> None:
            current = deps.validate_prepared_run(run, manifest, selected_runtime)
            projected = _validate_shared_run(
                deps.project_run(current), manifest, deps.policy
            )
            if current != run or projected != shared_run:
                raise _fail("private document UAT prepared run drifted")

        brain_before = _snapshot(
            deps.brain_snapshot(), "private document UAT brain snapshot"
        )
        candidates_before = _candidate_snapshot(deps.candidate_snapshot())
        require_current_run()
        conversions_before = _conversion_snapshot(deps.conversion_snapshot())

        interrupted_process = deps.launch_process(run, "interrupted")
        active_process = interrupted_process
        deadline = deps.monotonic() + deps.checkpoint_timeout
        checkpoint: dict[str, Any] | None = None
        while deps.monotonic() <= deadline:
            if deps.process_poll(interrupted_process) is not None:
                raise _fail("private document UAT process exited before interruption")
            supplied_checkpoint = deps.inspect_checkpoint(run)
            if supplied_checkpoint is None:
                deps.sleep(deps.poll_interval)
                continue
            observed = validate_checkpoint(
                supplied_checkpoint, run.batch_manifest, run.batch_manifest_digest
            )
            if 0 < observed["processed"] < len(expected_ids):
                if deps.inspect_barrier(run, observed) is True:
                    checkpoint = observed
                    break
                deps.sleep(deps.poll_interval)
                continue
            if observed["processed"] >= len(expected_ids):
                raise _fail("private document UAT checkpoint became terminal before interruption")
            deps.sleep(deps.poll_interval)
        if checkpoint is None:
            raise _fail("private document UAT checkpoint was not reached")
        deps.send_signal(interrupted_process)
        returncode, stdout, _stderr = _process_result(deps.wait_process(interrupted_process))
        deps.require_verified_process(interrupted_process, "interrupted")
        active_process = None
        if returncode not in {
            -signal.SIGINT,
            -signal.SIGTERM,
            128 + signal.SIGINT,
            128 + signal.SIGTERM,
        } or stdout:
            raise _fail("private document UAT interruption outcome is invalid")
        conversions_interrupted = _conversion_snapshot(deps.conversion_snapshot())
        if conversions_interrupted - conversions_before != checkpoint["processed"]:
            raise _fail("private document UAT interrupted conversion observation drifted")

        require_current_run()
        resumed_process = deps.launch_process(run, "resume")
        active_process = resumed_process
        returncode, stdout, _stderr = _process_result(deps.wait_process(resumed_process))
        deps.require_verified_process(resumed_process, "resume")
        active_process = None
        if returncode != 0:
            raise _fail("private document UAT resume failed")
        terminal = deps.verify_terminal(run, stdout, deps.load_final(run))
        conversions_resumed = _conversion_snapshot(deps.conversion_snapshot())
        resumed_delta = conversions_resumed - conversions_interrupted
        if resumed_delta != len(expected_ids) - checkpoint["processed"]:
            raise _fail("private document UAT resumed conversion observation drifted")

        require_current_run()
        rerun_process = deps.launch_process(run, "rerun")
        active_process = rerun_process
        returncode, rerun_stdout, _rerun_stderr = _process_result(deps.wait_process(rerun_process))
        deps.require_verified_process(rerun_process, "rerun")
        active_process = None
        if returncode != 0:
            raise _fail("private document UAT rerun failed")
        rerun_terminal = deps.verify_terminal(run, rerun_stdout, deps.load_final(run))
        conversions_rerun = _conversion_snapshot(deps.conversion_snapshot())
        if terminal != rerun_terminal or stdout != rerun_stdout:
            raise _fail("private document UAT rerun output drifted")
        if conversions_rerun != conversions_resumed:
            raise _fail("private document UAT rerun performed a conversion")

        require_current_run()
        results = deps.verify_queue(deps.load_queue(), terminal, candidates_before)
        terminal = {**terminal, "items": results}
        calibration_supplied = deps.calibration(run.corpus_manifest, run.corpus_root)
        calibration = deps.policy.validate_calibration(calibration_supplied)
        if isinstance(calibration, Mapping):
            calibration = _strict_copy(calibration)
        brain_post_run = _snapshot(
            deps.brain_snapshot(), "private document UAT brain snapshot"
        )
        if brain_post_run != brain_before:
            raise _fail("private document UAT brain drifted")
        cleanup_attempted = True
        if deps.cleanup(run) is not True:
            raise _fail("private document UAT cleanup drifted")
        brain_post_cleanup = _snapshot(
            deps.brain_snapshot(), "private document UAT brain snapshot"
        )
        if brain_post_cleanup != brain_before:
            raise _fail("private document UAT brain drifted")
        calibration_reopened = deps.policy.validate_calibration(calibration_supplied)
        if isinstance(calibration_reopened, Mapping):
            calibration_reopened = _strict_copy(calibration_reopened)
        if calibration_reopened != calibration:
            raise _fail("private document UAT calibration drifted")

        report = deps.assemble_readback(
            run,
            terminal,
            checkpoint,
            brain_before,
            calibration,
            {
                "initial": conversions_interrupted - conversions_before,
                "resumed": resumed_delta,
                "rerun": conversions_rerun - conversions_resumed,
            },
        )
        deps.persist_readback(
            run,
            report,
            brain_post_run,
            brain_post_cleanup,
        )
        return report
    except (KeyboardInterrupt, SystemExit) as exc:
        primary = exc
        raise
    except BaseException as exc:
        primary = exc
        raise _wrap_failure(exc) from exc
    finally:
        if active_process is not None:
            try:
                if deps.process_poll(active_process) is None:
                    deps.terminate_process(active_process)
            except BaseException:
                if primary is None:
                    raise PrivateDocumentUatError("private document UAT failed")
        if run is not None and not cleanup_attempted:
            try:
                cleanup_attempted = True
                cleaned = deps.cleanup(run)
                if cleaned is not True:
                    raise _fail("private document UAT cleanup drifted")
            except BaseException as cleanup_error:
                if primary is None:
                    raise PrivateDocumentUatError("private document UAT failed") from cleanup_error
