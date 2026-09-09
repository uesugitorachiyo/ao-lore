"""Independent parser, distiller, navigator, and synthesizer role runtime."""

from __future__ import annotations

import copy
import hashlib
import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .benchmark import BenchmarkError, canonical_digest
from ._strict_io import ContractError, parse_strict_json, strict_read_json
from .ocr_benchmark import (
    OcrBenchmarkError,
    ocr_benchmark_configuration_digest,
    select_ocr_candidate,
)
from .ocr_contracts import validate_qualification


class RoleConfigError(ValueError):
    """Raised when role configuration could inherit or cross authority."""


class RoleExecutionError(RuntimeError):
    """Raised when a role cannot produce a schema-bound result safely."""


@dataclass(frozen=True)
class ActivatedOcrParser:
    capability: str
    parser_id: str
    parser_version: str
    candidate_id: str
    runtime_digest: str
    model_set_digest: str
    qualification_digest: str
    selected_result_digest: str
    character_accuracy_millionths: int
    detection_recall_millionths: int
    reading_order_pair_accuracy_millionths: int
    mean_polygon_iou_millionths: int
    page_coverage_millionths: int
    provider_enabled: bool
    fallback_enabled: bool


def _sha256_body(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def activate_ocr_parser(
    retained_qualification: bytes,
    *,
    qualification_digest: str,
    runtime_digest: str,
    corpus_digest: str,
    corpus_configuration_digest: str,
    oracle_digest: str,
    model_digests: tuple[str, str, str],
    selected_result_digest: str,
) -> ActivatedOcrParser:
    """Activate one OCR identity only from exact reviewed qualification evidence."""

    try:
        if type(retained_qualification) is not bytes or not retained_qualification or len(retained_qualification) > 1024 * 1024:
            raise ContractError("OCR qualification bytes differ")
        if type(qualification_digest) is not str or _sha256_body(retained_qualification) != qualification_digest:
            raise ContractError("OCR qualification digest differs")
        decoded = parse_strict_json(retained_qualification, "OCR qualification")
        qualification = validate_qualification(decoded)
        expected_configuration = ocr_benchmark_configuration_digest(
            corpus_configuration_digest=corpus_configuration_digest,
            model_digests=model_digests,
        )
        expected = {
            "runtime_digest": runtime_digest,
            "corpus_digest": corpus_digest,
            "oracle_digest": oracle_digest,
            "configuration_digest": expected_configuration,
        }
        if any(qualification[field] != value for field, value in expected.items()):
            raise ContractError("OCR qualification binding differs")
        if qualification["decision"] not in {"hold", "candidate_change"}:
            raise ContractError("OCR qualification decision differs")
        selected_id = qualification["selected_candidate_id"]
        selected_rows = [row for row in qualification["candidates"] if row["candidate_id"] == selected_id]
        if len(selected_rows) != 1:
            raise ContractError("OCR selected candidate differs")
        selected = selected_rows[0]
        selection = select_ocr_candidate(qualification["candidates"])
        if (
            selection.decision != qualification["decision"]
            or selection.selected_candidate_id != selected_id
            or selected["result_digest"] != selected_result_digest
            or selected["all_hard_gates_pass"] is not True
            or selected["repeatability_identical"] is not True
            or any(row["repeatability_identical"] is not True for row in qualification["candidates"])
        ):
            raise ContractError("OCR selected result does not qualify")
        index = int(str(selected_id).removeprefix("candidate-")) - 1
        selected_model_digest = model_digests[index]
    except (ContractError, OcrBenchmarkError, IndexError, TypeError, ValueError) as exc:
        del exc
        raise RoleConfigError("OCR activation evidence is invalid") from None
    return ActivatedOcrParser(
        capability="ocr-layout",
        parser_id="paddle-ocr-english",
        parser_version="0.1.0",
        candidate_id=str(selected_id),
        runtime_digest=runtime_digest,
        model_set_digest=selected_model_digest,
        qualification_digest=qualification_digest,
        selected_result_digest=selected_result_digest,
        character_accuracy_millionths=int(selected["character_accuracy_millionths"]),
        detection_recall_millionths=int(selected["detection_recall_millionths"]),
        reading_order_pair_accuracy_millionths=int(selected["reading_order_pair_accuracy_millionths"]),
        mean_polygon_iou_millionths=int(selected["mean_polygon_iou_millionths"]),
        page_coverage_millionths=int(selected["page_coverage_millionths"]),
        provider_enabled=False,
        fallback_enabled=False,
    )


def load_ocr_parser_activation(
    qualification_path: str,
    *,
    evidence_root: str,
    qualification_digest: str,
    runtime_digest: str,
    corpus_digest: str,
    corpus_configuration_digest: str,
    oracle_digest: str,
    model_digests: tuple[str, str, str],
    selected_result_digest: str,
) -> ActivatedOcrParser:
    """Descriptor-read one contained retained qualification before activation."""

    try:
        _, body = strict_read_json(
            qualification_path,
            "retained OCR qualification",
            max_bytes=1024 * 1024,
            root=evidence_root,
        )
    except ContractError as exc:
        del exc
        raise RoleConfigError("OCR activation evidence is invalid") from None
    return activate_ocr_parser(
        body,
        qualification_digest=qualification_digest,
        runtime_digest=runtime_digest,
        corpus_digest=corpus_digest,
        corpus_configuration_digest=corpus_configuration_digest,
        oracle_digest=oracle_digest,
        model_digests=model_digests,
        selected_result_digest=selected_result_digest,
    )


ROLES = ("parser", "distiller", "navigator", "synthesizer")
_CONFIG_FIELDS = {
    "schema_version",
    "role",
    "adapter",
    "provider",
    "endpoint",
    "model_id",
    "policy_version",
    "output_schema_version",
    "context_limit",
    "output_limit",
    "temperature",
    "decoding_controls",
    "timeout_ms",
    "retries",
    "network_policy",
    "privacy_policy",
    "credential_environment_variable",
    "token_budget",
    "monetary_budget",
    "fallback_policy",
    "cache_policy",
    "no_implicit_cross_role_inheritance",
}
_ROLE_INPUTS = {
    "parser": {
        "allowed": {"source_document", "source_region", "parser_context"},
        "required": {"parser_context"},
    },
    "distiller": {
        "allowed": {"document_ir", "candidate_context"},
        "required": {"document_ir", "candidate_context"},
    },
    "navigator": {
        "allowed": {
            "normalized_query",
            "routing_metadata",
            "navigation_state",
            "visited_nodes",
            "evidence_summary",
            "coverage_gaps",
            "budgets",
        },
        "required": {
            "normalized_query",
            "routing_metadata",
            "navigation_state",
            "visited_nodes",
            "evidence_summary",
            "coverage_gaps",
            "budgets",
        },
    },
    "synthesizer": {
        "allowed": {
            "normalized_query",
            "evidence_ledger",
            "coverage_report",
            "answer_format",
            "citation_requirements",
        },
        "required": {
            "normalized_query",
            "evidence_ledger",
            "coverage_report",
            "answer_format",
            "citation_requirements",
        },
    },
}


def _string(value: Any, field: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RoleConfigError(f"{field} must be a non-empty string")
    return value


def _integer(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RoleConfigError(f"{field} must be an integer >= {minimum}")
    return value


def _number(value: Any, field: str, minimum: float, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoleConfigError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < minimum or (maximum is not None and result > maximum):
        raise RoleConfigError(f"{field} is outside its allowed range")
    return result


def validate_role_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(config, Mapping) or set(config) != _CONFIG_FIELDS:
        raise RoleConfigError("role configuration has missing or unknown fields")
    if config["schema_version"] != "ao.lore.model-role-config.v0.1":
        raise RoleConfigError("unsupported role configuration version")
    role = config["role"]
    if role not in ROLES:
        raise RoleConfigError("unknown model role")
    for field in ("adapter", "provider", "model_id", "policy_version", "output_schema_version", "privacy_policy"):
        _string(config[field], f"{role}.{field}")
    _string(config["endpoint"], f"{role}.endpoint", nullable=True)
    _string(config["credential_environment_variable"], f"{role}.credential_environment_variable", nullable=True)
    _integer(config["context_limit"], f"{role}.context_limit", 1)
    _integer(config["output_limit"], f"{role}.output_limit", 1)
    _number(config["temperature"], f"{role}.temperature", 0, 2)
    if not isinstance(config["decoding_controls"], Mapping):
        raise RoleConfigError(f"{role}.decoding_controls must be an object")
    _integer(config["timeout_ms"], f"{role}.timeout_ms", 1)
    _integer(config["retries"], f"{role}.retries", 0)
    if config["network_policy"] not in {"offline", "allowlisted_frontier"}:
        raise RoleConfigError(f"{role}.network_policy is invalid")
    _integer(config["token_budget"], f"{role}.token_budget", 0)
    _number(config["monetary_budget"], f"{role}.monetary_budget", 0)
    if config["no_implicit_cross_role_inheritance"] is not True:
        raise RoleConfigError(f"{role} must explicitly deny cross-role inheritance")

    fallback = config["fallback_policy"]
    if not isinstance(fallback, Mapping) or set(fallback) != {"enabled", "adapters"}:
        raise RoleConfigError(f"{role}.fallback_policy is malformed")
    if not isinstance(fallback["enabled"], bool) or not isinstance(fallback["adapters"], list):
        raise RoleConfigError(f"{role}.fallback_policy fields are malformed")
    if any(not isinstance(item, str) or not item for item in fallback["adapters"]):
        raise RoleConfigError(f"{role}.fallback adapters must be strings")
    if len(set(fallback["adapters"])) != len(fallback["adapters"]):
        raise RoleConfigError(f"{role}.fallback adapters must be unique")
    if not fallback["enabled"] and fallback["adapters"]:
        raise RoleConfigError(f"{role}.fallback adapters require enabled=true")

    cache = config["cache_policy"]
    if not isinstance(cache, Mapping) or set(cache) != {"enabled", "ttl_seconds"}:
        raise RoleConfigError(f"{role}.cache_policy is malformed")
    if not isinstance(cache["enabled"], bool):
        raise RoleConfigError(f"{role}.cache_policy.enabled must be boolean")
    _integer(cache["ttl_seconds"], f"{role}.cache_policy.ttl_seconds", 0)
    try:
        canonical_digest(config)
    except BenchmarkError as exc:
        raise RoleConfigError("role configuration must be strict JSON data") from exc
    return copy.deepcopy(dict(config))


def validate_role_configuration_set(configs: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Require four fully specified configs with no shared credential name."""

    if not isinstance(configs, Mapping) or set(configs) != set(ROLES):
        raise RoleConfigError("configuration set must contain exactly four model roles")
    validated: dict[str, dict[str, Any]] = {}
    credential_owners: dict[str, str] = {}
    for role in ROLES:
        config = validate_role_config(configs[role])
        if config["role"] != role:
            raise RoleConfigError(f"configuration key {role} does not match embedded role")
        credential = config["credential_environment_variable"]
        if credential is not None:
            if credential in credential_owners:
                raise RoleConfigError(
                    f"credential environment variable is reused by {credential_owners[credential]} and {role}"
                )
            credential_owners[credential] = role
        validated[role] = config
    return validated


@dataclass(frozen=True)
class RoleResult:
    output: Mapping[str, Any]
    tokens: int
    latency_ms: int
    monetary_cost: float


class ScriptedRoleAdapter:
    """Role-labelled deterministic adapter used for offline verification."""

    def __init__(
        self,
        role: str,
        adapter_id: str,
        handler: Callable[[Mapping[str, Any], Mapping[str, Any]], RoleResult],
    ) -> None:
        if role not in ROLES:
            raise RoleConfigError("scripted adapter has unknown role")
        self.role = role
        self.adapter_id = _string(adapter_id, "adapter_id")
        self._handler = handler

    def execute(self, payload: Mapping[str, Any], config: Mapping[str, Any]) -> RoleResult:
        result = self._handler(copy.deepcopy(payload), copy.deepcopy(config))
        if not isinstance(result, RoleResult):
            raise RoleExecutionError("role adapter must return RoleResult")
        return result


def cache_identity(config: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    """Bind cache identity to role, model/provider policy, schema, and config."""

    validated = validate_role_config(config)
    if not isinstance(payload, Mapping):
        raise RoleConfigError("cache payload must be an object")
    try:
        input_digest = canonical_digest(payload)
    except BenchmarkError as exc:
        raise RoleConfigError("cache input must be strict JSON data") from exc
    return canonical_digest(
        {
            "role": validated["role"],
            "model_id": validated["model_id"],
            "provider": validated["provider"],
            "policy_version": validated["policy_version"],
            "input_digest": input_digest,
            "output_schema_version": validated["output_schema_version"],
            "configuration_digest": canonical_digest(validated),
        }
    )


def _validate_role_input(role: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise RoleExecutionError("role input must be an object")
    fields = set(payload)
    boundary = _ROLE_INPUTS[role]
    if fields - boundary["allowed"] or not boundary["required"].issubset(fields):
        raise RoleExecutionError(f"{role} input violates its authority contract")
    if role == "parser" and not ({"source_document", "source_region"} & fields):
        raise RoleExecutionError("parser input requires a bounded source or region")
    try:
        canonical_digest(payload)
    except BenchmarkError as exc:
        raise RoleExecutionError("role input must be strict JSON data") from exc
    return copy.deepcopy(dict(payload))


class RoleRuntime:
    """Execute role-labelled adapters with explicit same-role fallback only."""

    def __init__(
        self,
        configs: Mapping[str, Mapping[str, Any]],
        adapters: Sequence[ScriptedRoleAdapter],
    ) -> None:
        self._configs = validate_role_configuration_set(configs)
        self._adapters: dict[tuple[str, str], ScriptedRoleAdapter] = {}
        for adapter in adapters:
            key = (adapter.role, adapter.adapter_id)
            if key in self._adapters:
                raise RoleConfigError(f"duplicate role adapter: {adapter.role}/{adapter.adapter_id}")
            self._adapters[key] = adapter
        for role, config in self._configs.items():
            primary_key = (role, config["adapter"])
            if primary_key not in self._adapters:
                raise RoleConfigError(f"configured {role} adapter is unavailable")
            for fallback_id in config["fallback_policy"]["adapters"]:
                if (role, fallback_id) not in self._adapters:
                    owners = sorted(owner for owner, adapter_id in self._adapters if adapter_id == fallback_id)
                    if owners:
                        raise RoleConfigError(
                            f"{role} fallback {fallback_id} belongs to another role: {', '.join(owners)}"
                        )
                    raise RoleConfigError(f"configured {role} fallback is unavailable: {fallback_id}")
        self._cache: dict[str, tuple[int, Mapping[str, Any], Mapping[str, Any]]] = {}

    def _trace(
        self,
        *,
        role: str,
        config: Mapping[str, Any],
        adapter_id: str,
        payload: Mapping[str, Any],
        output: Mapping[str, Any],
        tokens: int,
        latency_ms: int,
        retry_count: int,
        fallback: Mapping[str, Any],
        error_class: str | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": "ao.lore.model-call-trace.v0.1",
            "role": role,
            "adapter": adapter_id,
            "model_id": config["model_id"],
            "input_digest": canonical_digest(payload),
            "output_digest": canonical_digest(output),
            "output_schema_version": config["output_schema_version"],
            "tokens": tokens,
            "latency_ms": latency_ms,
            "retry_count": retry_count,
            "fallback": copy.deepcopy(dict(fallback)),
            "error_state_redacted": error_class,
            "private_reasoning_persisted": False,
        }

    def execute(self, role: str, payload: Mapping[str, Any], *, now_seconds: int) -> dict[str, Any]:
        if role not in ROLES:
            raise RoleExecutionError("unknown role")
        if isinstance(now_seconds, bool) or not isinstance(now_seconds, int) or now_seconds < 0:
            raise RoleExecutionError("now_seconds must be a non-negative integer")
        config = self._configs[role]
        safe_payload = _validate_role_input(role, payload)
        try:
            key = cache_identity(config, safe_payload)
        except RoleConfigError as exc:
            raise RoleExecutionError(str(exc)) from exc
        cached = self._cache.get(key)
        if cached is not None and cached[0] >= now_seconds:
            output = copy.deepcopy(dict(cached[1]))
            trace = self._trace(
                role=role,
                config=config,
                adapter_id=config["adapter"],
                payload=safe_payload,
                output=output,
                tokens=0,
                latency_ms=0,
                retry_count=0,
                fallback={"used": False, "cache_hit": True},
                error_class=None,
            )
            return {"schema_version": "ao.lore.role-execution.v0.1", "output": output, "trace": trace, "cache_identity": key}

        adapter_ids = [config["adapter"]]
        if config["fallback_policy"]["enabled"]:
            adapter_ids.extend(config["fallback_policy"]["adapters"])
        last_error_class: str | None = None
        retry_count = 0
        for adapter_index, adapter_id in enumerate(adapter_ids):
            adapter = self._adapters[(role, adapter_id)]
            for retry in range(config["retries"] + 1):
                retry_count = retry
                try:
                    result = adapter.execute(safe_payload, config)
                    if not isinstance(result.output, Mapping):
                        raise RoleExecutionError("role output must be an object")
                    output = copy.deepcopy(dict(result.output))
                    if output.get("schema_version") != config["output_schema_version"]:
                        raise RoleExecutionError("role output schema version mismatch")
                    if isinstance(result.tokens, bool) or not isinstance(result.tokens, int) or result.tokens < 0:
                        raise RoleExecutionError("role token count is invalid")
                    if isinstance(result.latency_ms, bool) or not isinstance(result.latency_ms, int) or result.latency_ms < 0:
                        raise RoleExecutionError("role latency is invalid")
                    if result.tokens > config["token_budget"]:
                        raise RoleExecutionError("role token budget exceeded")
                    cost = _number(result.monetary_cost, "role monetary cost", 0)
                    if cost > config["monetary_budget"]:
                        raise RoleExecutionError("role monetary budget exceeded")
                    canonical_digest(output)
                    fallback = {
                        "used": adapter_index > 0,
                        "from_adapter": config["adapter"] if adapter_index > 0 else None,
                        "to_adapter": adapter_id if adapter_index > 0 else None,
                        "reason": last_error_class if adapter_index > 0 else None,
                        "cache_hit": False,
                    }
                    trace = self._trace(
                        role=role,
                        config=config,
                        adapter_id=adapter_id,
                        payload=safe_payload,
                        output=output,
                        tokens=result.tokens,
                        latency_ms=result.latency_ms,
                        retry_count=retry_count,
                        fallback=fallback,
                        error_class=last_error_class if adapter_index > 0 else None,
                    )
                    if config["cache_policy"]["enabled"] and config["cache_policy"]["ttl_seconds"] > 0:
                        self._cache[key] = (
                            now_seconds + config["cache_policy"]["ttl_seconds"],
                            copy.deepcopy(output),
                            copy.deepcopy(trace),
                        )
                    return {
                        "schema_version": "ao.lore.role-execution.v0.1",
                        "output": output,
                        "trace": trace,
                        "cache_identity": key,
                    }
                except Exception as exc:
                    last_error_class = type(exc).__name__
                    if retry < config["retries"]:
                        continue
                    break
        raise RoleExecutionError(f"{role} execution failed ({last_error_class or 'unknown error'})")
