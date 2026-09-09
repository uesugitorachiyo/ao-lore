"""Strict dependency-free contracts for public candidate quality review artifacts."""

from __future__ import annotations

import copy
import hmac
import re
import unicodedata
from typing import Any

from ._strict_io import ContractError
from .benchmark import BenchmarkError, canonical_digest


AUTHORITY_FIELDS = (
    "canonical_query_invoked",
    "review_event_appended",
    "candidate_decision_taken",
    "promotion_prepared",
    "promotion_applied",
    "provider_calls",
    "network_accessed",
    "credential_used",
    "publication",
    "release",
    "deployment",
    "authority_advanced",
)
QUESTION_CATEGORIES = (
    "fact",
    "procedure",
    "qualification",
    "unsupported",
)
QUESTION_OUTCOMES = ("supported", "refusal")
ANNOTATION_LABELS = (
    "useful_exact",
    "exact_but_fragmented",
    "exact_but_low_value",
    "duplicate",
    "misleading_without_context",
    "binding_error",
)
RECOMMENDATIONS = ("pass", "hold", "reject")
SELECTION_PRIORITY = (
    "binding_risk",
    "normalized_duplicate",
    "fragmentation",
    "position",
    "length",
    "block_type",
    "digest_fill",
)
RECOVERY_PHASES = (
    "intake",
    "verification",
    "sampling",
    "annotation",
    "questions",
    "summary",
)
RECOVERY_STATUSES = (
    "staged",
    "interrupted",
    "ready_to_resume",
    "completed",
)
SELECTION_REASON_CODES = (
    "binding_risk",
    "normalized_duplicate",
    "fragmentation",
    "first_position",
    "middle_position",
    "final_position",
    "longest_claim",
    "shortest_claim",
    "block_type_representative",
    "digest_fill",
)

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC_RE = re.compile(
    r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$"
)
_RELATIVE_PATH_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]{0,63}(?:/[a-z0-9][a-z0-9._-]{0,63}){0,7}$"
)
_CANONICAL_EVIDENCE_QUERY_RE = re.compile(r"^(?!.*[A-Z])[^\W_]+(?: [^\W_]+)*$")
_CLAIM_COUNTS = {
    "candidate-01": 19,
    "candidate-02": 13,
    "candidate-03": 73,
    "candidate-04": 21,
    "candidate-05": 71,
    "candidate-06": 289,
}
_PUBLIC_CLAIM_COUNTS = {
    "candidate-e8a1d75cfea465ec": 19,
    "candidate-09a469bbdfb31e94": 13,
    "candidate-442165094a6376a5": 73,
    "candidate-87e6e6384bc39a86": 21,
    "candidate-e8173156259e8d7d": 71,
    "candidate-00906bcc7e111305": 289,
}
_SAMPLE_ALLOCATIONS = {
    "candidate-01": 19,
    "candidate-02": 13,
    "candidate-03": 14,
    "candidate-04": 12,
    "candidate-05": 14,
    "candidate-06": 24,
}
_PUBLIC_SAMPLE_ALLOCATIONS = dict(zip(_PUBLIC_CLAIM_COUNTS, _SAMPLE_ALLOCATIONS.values()))
_TOTAL_CLAIMS = 486
_TOTAL_CANDIDATES = 6
_TOTAL_QUESTIONS = 24
_TOTAL_SAMPLED_CLAIMS = 96


def _mapping(value: Any, keys: tuple[str, ...], label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError(f"{label} must be an exact object")
    if set(value) != set(keys):
        raise ContractError(f"{label} keys differ")
    if any(type(key) is not str for key in value):
        raise ContractError(f"{label} keys must be exact strings")
    return value


def _list(value: Any, label: str, minimum: int, maximum: int) -> list[Any]:
    if type(value) is not list or not minimum <= len(value) <= maximum:
        raise ContractError(f"{label} must be a bounded exact array")
    return value


def _text(value: Any, label: str, *, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ContractError(f"{label} must be bounded exact text")
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise ContractError(f"{label} contains a forbidden control character")
    return value


def _evidence_query(value: Any, label: str) -> str:
    query = _text(value, label, maximum=256)
    stripped = query.strip()
    normalized = unicodedata.normalize("NFKC", stripped).casefold()
    collapsed: list[str] = []
    previous_space = False
    for character in normalized:
        if character.isalnum():
            collapsed.append(character)
            previous_space = False
        elif not previous_space:
            collapsed.append(" ")
            previous_space = True
    canonical = "".join(collapsed).strip()
    if not canonical:
        raise ContractError("candidate quality question evidence differs")
    if _CANONICAL_EVIDENCE_QUERY_RE.fullmatch(canonical) is None:
        raise ContractError("candidate quality question evidence differs")
    if query != canonical:
        raise ContractError("candidate quality question evidence differs")
    if re.fullmatch(r"[a-z0-9._-]{1,128}", canonical) is not None:
        raise ContractError("candidate quality question evidence differs")
    return canonical


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label, maximum=128)
    if _ID_RE.fullmatch(text) is None:
        raise ContractError(f"{label} must be a bounded lowercase identifier")
    return text


def _digest(value: Any, label: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        raise ContractError(f"{label} must be a lowercase sha256 digest")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(f"{label} must be a bounded exact integer")
    return value


def _utc_timestamp(value: Any, label: str) -> str:
    text = _text(value, label, maximum=20)
    if _UTC_RE.fullmatch(text) is None:
        raise ContractError(f"{label} must be an exact UTC timestamp")
    return text


def _relative_path(value: Any, label: str) -> str:
    text = _text(value, label, maximum=256)
    if _RELATIVE_PATH_RE.fullmatch(text) is None:
        raise ContractError(f"{label} must remain a relative owned path")
    return text


def _unique_strings(values: list[str], label: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ContractError(f"{label} must be unique")
    return values


def _authority(body: dict[str, Any]) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for field in AUTHORITY_FIELDS:
        if type(body[field]) is not bool:
            raise ContractError(f"{field} must be an exact boolean")
        if body[field] is not False:
            raise ContractError(f"{field} must remain false")
        result[field] = False
    return result


def _digest_check(value: dict[str, Any], field: str, label: str) -> str:
    supplied = _digest(value[field], field)
    try:
        expected = canonical_digest({key: item for key, item in value.items() if key != field})
    except BenchmarkError as exc:
        raise ContractError(f"{label} is not canonical JSON") from exc
    if not hmac.compare_digest(supplied, expected):
        raise ContractError(f"{label} digest differs")
    return supplied


def _candidate_binding(value: Any) -> dict[str, Any]:
    item = _mapping(
        value,
        ("candidate_id", "candidate_digest", "provenance_digest", "source_digest", "claim_count"),
        "candidate binding",
    )
    candidate_id = _identifier(item["candidate_id"], "candidate_id")
    return {
        "candidate_id": candidate_id,
        "candidate_digest": _digest(item["candidate_digest"], "candidate_digest"),
        "provenance_digest": _digest(item["provenance_digest"], "provenance_digest"),
        "source_digest": _digest(item["source_digest"], "source_digest"),
        "claim_count": _integer(item["claim_count"], "claim_count", 1, _TOTAL_CLAIMS),
    }


def validate_candidate_quality_campaign(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "campaign_id",
        "correlation_id",
        "terminal_readback_digest",
        "total_candidate_count",
        "total_claim_count",
        "total_sampled_claim_count",
        "total_question_count",
        "candidate_bindings",
        "sampling_policy_digest",
        "sample_digest",
        "annotations_digest",
        "questions_digest",
        "question_results_digest",
        "result_digests",
        "campaign_digest",
    ) + AUTHORITY_FIELDS
    body = _mapping(value, keys, "candidate quality campaign")
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-campaign.v0.1":
        raise ContractError("candidate quality campaign schema version differs")
    bindings = [_candidate_binding(item) for item in _list(body["candidate_bindings"], "candidate_bindings", 6, 6)]
    candidate_ids = _unique_strings([item["candidate_id"] for item in bindings], "candidate bindings")
    if candidate_ids not in (list(_CLAIM_COUNTS), list(_PUBLIC_CLAIM_COUNTS)):
        raise ContractError("candidate bindings differ")
    if [item["claim_count"] for item in bindings] != list(_CLAIM_COUNTS.values()):
        raise ContractError("candidate claim counts differ")
    result_digests = _unique_strings(
        [_digest(item, "result_digest") for item in _list(body["result_digests"], "result_digests", 6, 6)],
        "result digests",
    )
    result = {
        "schema_version": body["schema_version"],
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "correlation_id": _identifier(body["correlation_id"], "correlation_id"),
        "terminal_readback_digest": _digest(body["terminal_readback_digest"], "terminal_readback_digest"),
        "total_candidate_count": _integer(body["total_candidate_count"], "total_candidate_count", 6, 6),
        "total_claim_count": _integer(body["total_claim_count"], "total_claim_count", _TOTAL_CLAIMS, _TOTAL_CLAIMS),
        "total_sampled_claim_count": _integer(
            body["total_sampled_claim_count"],
            "total_sampled_claim_count",
            _TOTAL_SAMPLED_CLAIMS,
            _TOTAL_SAMPLED_CLAIMS,
        ),
        "total_question_count": _integer(body["total_question_count"], "total_question_count", _TOTAL_QUESTIONS, _TOTAL_QUESTIONS),
        "candidate_bindings": bindings,
        "sampling_policy_digest": _digest(body["sampling_policy_digest"], "sampling_policy_digest"),
        "sample_digest": _digest(body["sample_digest"], "sample_digest"),
        "annotations_digest": _digest(body["annotations_digest"], "annotations_digest"),
        "questions_digest": _digest(body["questions_digest"], "questions_digest"),
        "question_results_digest": _digest(body["question_results_digest"], "question_results_digest"),
        "result_digests": result_digests,
        **_authority(body),
    }
    _digest_check({**result, "campaign_digest": body["campaign_digest"]}, "campaign_digest", "candidate quality campaign")
    result["campaign_digest"] = body["campaign_digest"]
    return result


def validate_candidate_quality_sampling_policy(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "policy_id",
        "campaign_id",
        "terminal_readback_digest",
        "total_candidate_count",
        "total_claim_count",
        "total_sample_count",
        "allocations",
        "selection_priority",
        "policy_digest",
    ) + AUTHORITY_FIELDS
    body = _mapping(value, keys, "candidate quality sampling policy")
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-sampling-policy.v0.1":
        raise ContractError("candidate quality sampling policy schema version differs")
    allocations = []
    for raw in _list(body["allocations"], "allocations", 6, 6):
        item = _mapping(
            raw,
            (
                "candidate_id",
                "candidate_digest",
                "provenance_digest",
                "source_digest",
                "claim_count",
                "sample_count",
            ),
            "allocation",
        )
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        allocations.append(
            {
                "candidate_id": candidate_id,
                "candidate_digest": _digest(item["candidate_digest"], "candidate_digest"),
                "provenance_digest": _digest(item["provenance_digest"], "provenance_digest"),
                "source_digest": _digest(item["source_digest"], "source_digest"),
                "claim_count": _integer(item["claim_count"], "claim_count", 1, _TOTAL_CLAIMS),
                "sample_count": _integer(item["sample_count"], "sample_count", 1, _TOTAL_SAMPLED_CLAIMS),
            }
        )
    allocation_ids = [item["candidate_id"] for item in allocations]
    if allocation_ids not in (list(_SAMPLE_ALLOCATIONS), list(_PUBLIC_SAMPLE_ALLOCATIONS)):
        raise ContractError("sampling policy candidate order differs")
    if [item["claim_count"] for item in allocations] != list(_CLAIM_COUNTS.values()):
        raise ContractError("sampling policy claim counts differ")
    if [item["sample_count"] for item in allocations] != list(_SAMPLE_ALLOCATIONS.values()):
        raise ContractError("sampling policy sample counts differ")
    priority = _list(body["selection_priority"], "selection_priority", len(SELECTION_PRIORITY), len(SELECTION_PRIORITY))
    if tuple(priority) != SELECTION_PRIORITY:
        raise ContractError("sampling priority differs")
    result = {
        "schema_version": body["schema_version"],
        "policy_id": _identifier(body["policy_id"], "policy_id"),
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "terminal_readback_digest": _digest(body["terminal_readback_digest"], "terminal_readback_digest"),
        "total_candidate_count": _integer(body["total_candidate_count"], "total_candidate_count", 6, 6),
        "total_claim_count": _integer(body["total_claim_count"], "total_claim_count", _TOTAL_CLAIMS, _TOTAL_CLAIMS),
        "total_sample_count": _integer(body["total_sample_count"], "total_sample_count", _TOTAL_SAMPLED_CLAIMS, _TOTAL_SAMPLED_CLAIMS),
        "allocations": allocations,
        "selection_priority": list(priority),
        **_authority(body),
    }
    _digest_check({**result, "policy_digest": body["policy_digest"]}, "policy_digest", "candidate quality sampling policy")
    result["policy_digest"] = body["policy_digest"]
    return result


def validate_candidate_quality_sample(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "sample_id",
        "campaign_id",
        "policy_digest",
        "total_selected_claims",
        "selections",
        "sample_digest",
    ) + AUTHORITY_FIELDS
    body = _mapping(value, keys, "candidate quality sample")
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-sample.v0.1":
        raise ContractError("candidate quality sample schema version differs")
    selections = []
    ordinals: list[int] = []
    observed_ids = [raw.get("candidate_id") for raw in body.get("selections", []) if type(raw) is dict]
    allocation_family = _PUBLIC_SAMPLE_ALLOCATIONS if observed_ids and observed_ids[0] in _PUBLIC_SAMPLE_ALLOCATIONS else _SAMPLE_ALLOCATIONS
    per_candidate: dict[str, int] = {candidate_id: 0 for candidate_id in allocation_family}
    per_candidate_bindings: dict[str, tuple[str, str, str]] = {}
    claim_keys: set[tuple[str, str]] = set()
    for raw in _list(body["selections"], "selections", _TOTAL_SAMPLED_CLAIMS, _TOTAL_SAMPLED_CLAIMS):
        item = _mapping(
            raw,
            (
                "sample_ordinal",
                "candidate_id",
                "claim_id",
                "citation_id",
                "claim_record_digest",
                "candidate_digest",
                "provenance_digest",
                "source_digest",
                "reason_codes",
            ),
            "sample selection",
        )
        ordinal = _integer(item["sample_ordinal"], "sample_ordinal", 1, _TOTAL_SAMPLED_CLAIMS)
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        claim_id = _identifier(item["claim_id"], "claim_id")
        citation_id = _identifier(item["citation_id"], "citation_id")
        reason_codes = _list(item["reason_codes"], "reason_codes", 1, 4)
        if len(reason_codes) != len(set(reason_codes)) or any(code not in SELECTION_REASON_CODES for code in reason_codes):
            raise ContractError("reason codes differ")
        if (candidate_id, claim_id) in claim_keys:
            raise ContractError("sample claim bindings must be unique")
        claim_keys.add((candidate_id, claim_id))
        if candidate_id not in per_candidate:
            raise ContractError("sample candidate differs")
        per_candidate[candidate_id] += 1
        ordinals.append(ordinal)
        candidate_digest = _digest(item["candidate_digest"], "candidate_digest")
        provenance_digest = _digest(item["provenance_digest"], "provenance_digest")
        source_digest = _digest(item["source_digest"], "source_digest")
        binding = (candidate_digest, provenance_digest, source_digest)
        if candidate_id in per_candidate_bindings and per_candidate_bindings[candidate_id] != binding:
            raise ContractError("sample candidate digest bindings differ")
        per_candidate_bindings.setdefault(candidate_id, binding)
        selections.append(
            {
                "sample_ordinal": ordinal,
                "candidate_id": candidate_id,
                "claim_id": claim_id,
                "citation_id": citation_id,
                "claim_record_digest": _digest(item["claim_record_digest"], "claim_record_digest"),
                "candidate_digest": candidate_digest,
                "provenance_digest": provenance_digest,
                "source_digest": source_digest,
                "reason_codes": list(reason_codes),
            }
        )
    if ordinals != list(range(1, _TOTAL_SAMPLED_CLAIMS + 1)):
        raise ContractError("sample ordinals differ")
    if per_candidate != allocation_family:
        raise ContractError("sample candidate allocations differ")
    result = {
        "schema_version": body["schema_version"],
        "sample_id": _identifier(body["sample_id"], "sample_id"),
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "policy_digest": _digest(body["policy_digest"], "policy_digest"),
        "total_selected_claims": _integer(
            body["total_selected_claims"],
            "total_selected_claims",
            _TOTAL_SAMPLED_CLAIMS,
            _TOTAL_SAMPLED_CLAIMS,
        ),
        "selections": selections,
        **_authority(body),
    }
    _digest_check({**result, "sample_digest": body["sample_digest"]}, "sample_digest", "candidate quality sample")
    result["sample_digest"] = body["sample_digest"]
    return result


def validate_candidate_quality_annotations(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "annotations_id",
        "campaign_id",
        "sample_digest",
        "reviewer_id",
        "reviewed_at",
        "annotation_counts",
        "annotations",
        "annotations_digest",
    ) + AUTHORITY_FIELDS
    if type(value) is not dict or set(value) != set(keys) or any(type(key) is not str for key in value):
        raise ContractError("candidate quality annotations keys differ")
    body = dict(value)
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-annotations.v0.1":
        raise ContractError("candidate quality annotations schema version differs")
    annotations = []
    ordinals: list[int] = []
    shared_policy_digest: str | None = None
    per_candidate_sources: dict[str, str] = {}
    for raw in _list(body["annotations"], "annotations", _TOTAL_SAMPLED_CLAIMS, _TOTAL_SAMPLED_CLAIMS):
        item = _mapping(
            raw,
            (
                "sample_ordinal",
                "candidate_id",
                "claim_id",
                "citation_id",
                "source_digest",
                "policy_digest",
                "classification",
                "rationale",
            ),
            "annotation",
        )
        classification = _text(item["classification"], "classification", maximum=64)
        if classification not in ANNOTATION_LABELS:
            raise ContractError("annotation classification differs")
        ordinal = _integer(item["sample_ordinal"], "sample_ordinal", 1, _TOTAL_SAMPLED_CLAIMS)
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        source_digest = _digest(item["source_digest"], "source_digest")
        policy_digest = _digest(item["policy_digest"], "policy_digest")
        if shared_policy_digest is None:
            shared_policy_digest = policy_digest
        elif shared_policy_digest != policy_digest:
            raise ContractError("annotation policy digest differs")
        if candidate_id in per_candidate_sources and per_candidate_sources[candidate_id] != source_digest:
            raise ContractError("annotation source digest differs")
        per_candidate_sources.setdefault(candidate_id, source_digest)
        ordinals.append(ordinal)
        annotations.append(
            {
                "sample_ordinal": ordinal,
                "candidate_id": candidate_id,
                "claim_id": _identifier(item["claim_id"], "claim_id"),
                "citation_id": _identifier(item["citation_id"], "citation_id"),
                "source_digest": source_digest,
                "policy_digest": policy_digest,
                "classification": classification,
                "rationale": _text(item["rationale"], "rationale", maximum=512),
            }
        )
    if ordinals != list(range(1, _TOTAL_SAMPLED_CLAIMS + 1)):
        raise ContractError("annotation ordinals differ")
    annotation_counts = {label: 0 for label in ANNOTATION_LABELS}
    for annotation in annotations:
        annotation_counts[annotation["classification"]] += 1
    supplied_counts = body.get("annotation_counts")
    if type(supplied_counts) is not dict or set(supplied_counts) != set(ANNOTATION_LABELS):
        raise ContractError("annotation counts differ")
    normalized_counts = {}
    for label in ANNOTATION_LABELS:
        normalized_counts[label] = _integer(
            supplied_counts[label],
            f"annotation_counts.{label}",
            0,
            _TOTAL_SAMPLED_CLAIMS,
        )
    if sum(normalized_counts.values()) != _TOTAL_SAMPLED_CLAIMS or normalized_counts != annotation_counts:
        raise ContractError("annotation counts differ")
    result = {
        "schema_version": body["schema_version"],
        "annotations_id": _identifier(body["annotations_id"], "annotations_id"),
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "sample_digest": _digest(body["sample_digest"], "sample_digest"),
        "reviewer_id": _identifier(body["reviewer_id"], "reviewer_id"),
        "reviewed_at": _utc_timestamp(body["reviewed_at"], "reviewed_at"),
        "annotation_counts": normalized_counts,
        "annotations": annotations,
        **_authority(body),
    }
    _digest_check({**result, "annotations_digest": body["annotations_digest"]}, "annotations_digest", "candidate quality annotations")
    result["annotations_digest"] = body["annotations_digest"]
    return result


def validate_candidate_quality_questions(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "question_set_id",
        "campaign_id",
        "question_count",
        "questions",
        "questions_digest",
    ) + AUTHORITY_FIELDS
    body = _mapping(value, keys, "candidate quality questions")
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-questions.v0.1":
        raise ContractError("candidate quality questions schema version differs")
    questions = []
    seen_ids: set[str] = set()
    per_candidate: dict[str, list[str]] = {candidate_id: [] for candidate_id in _CLAIM_COUNTS}
    per_candidate_sources: dict[str, str] = {}
    for raw in _list(body["questions"], "questions", _TOTAL_QUESTIONS, _TOTAL_QUESTIONS):
        if type(raw) is not dict:
            raise ContractError("question must be an exact object")
        category = _text(raw["category"], "category", maximum=32)
        if category not in QUESTION_CATEGORIES:
            raise ContractError("question category differs")
        keys = (
            "question_id",
            "candidate_id",
            "source_digest",
            "category",
            "prompt",
            "expected_outcome",
            "expected_claim_ids",
        )
        if category != "unsupported":
            keys = keys + ("evidence_query",)
        item = _mapping(raw, keys, "question")
        question_id = _identifier(item["question_id"], "question_id")
        if question_id in seen_ids:
            raise ContractError("question identifiers must be unique")
        seen_ids.add(question_id)
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        expected_outcome = _text(item["expected_outcome"], "expected_outcome", maximum=16)
        if expected_outcome not in QUESTION_OUTCOMES:
            raise ContractError("question outcome differs")
        expected_claim_ids = [_identifier(claim_id, "expected_claim_id") for claim_id in _list(item["expected_claim_ids"], "expected_claim_ids", 0, 4)]
        _unique_strings(expected_claim_ids, "expected claim ids")
        if category == "unsupported":
            if expected_outcome != "refusal" or expected_claim_ids:
                raise ContractError("unsupported questions must refuse")
        elif expected_outcome != "supported" or not expected_claim_ids:
            raise ContractError("supported questions must bind exact claims")
        per_candidate.setdefault(candidate_id, []).append(category)
        source_digest = _digest(item["source_digest"], "source_digest")
        if candidate_id in per_candidate_sources and per_candidate_sources[candidate_id] != source_digest:
            raise ContractError("candidate question source digest differs")
        per_candidate_sources.setdefault(candidate_id, source_digest)
        prompt = _text(item["prompt"], "prompt", maximum=256)
        normalized_item = {
            "question_id": question_id,
            "candidate_id": candidate_id,
            "source_digest": source_digest,
            "category": category,
            "prompt": prompt,
            "expected_outcome": expected_outcome,
            "expected_claim_ids": expected_claim_ids,
        }
        if category != "unsupported":
            normalized_item["evidence_query"] = _evidence_query(item["evidence_query"], "evidence_query")
        questions.append(
            normalized_item
        )
    expected_categories = list(QUESTION_CATEGORIES)
    for candidate_id in _CLAIM_COUNTS:
        if per_candidate.get(candidate_id) != expected_categories:
            raise ContractError("candidate question coverage differs")
    result = {
        "schema_version": body["schema_version"],
        "question_set_id": _identifier(body["question_set_id"], "question_set_id"),
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "question_count": _integer(body["question_count"], "question_count", _TOTAL_QUESTIONS, _TOTAL_QUESTIONS),
        "questions": questions,
        **_authority(body),
    }
    _digest_check({**result, "questions_digest": body["questions_digest"]}, "questions_digest", "candidate quality questions")
    result["questions_digest"] = body["questions_digest"]
    return result


def validate_candidate_quality_question_results(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "question_results_id",
        "campaign_id",
        "questions_digest",
        "result_count",
        "results",
        "question_results_digest",
    ) + AUTHORITY_FIELDS
    body = _mapping(value, keys, "candidate quality question results")
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-question-results.v0.1":
        raise ContractError("candidate quality question results schema version differs")
    results = []
    question_ids: list[str] = []
    candidate_counts: dict[str, int] = {candidate_id: 0 for candidate_id in _CLAIM_COUNTS}
    candidate_question_pairs: set[tuple[str, str]] = set()
    for raw in _list(body["results"], "results", _TOTAL_QUESTIONS, _TOTAL_QUESTIONS):
        item = _mapping(
            raw,
            (
                "question_id",
                "candidate_id",
                "expected_outcome",
                "actual_outcome",
                "supporting_claim_ids",
                "supporting_claim_count",
                "evidence_match_millionths",
            ),
            "question result",
        )
        expected_outcome = _text(item["expected_outcome"], "expected_outcome", maximum=16)
        actual_outcome = _text(item["actual_outcome"], "actual_outcome", maximum=16)
        if expected_outcome not in QUESTION_OUTCOMES or actual_outcome not in QUESTION_OUTCOMES:
            raise ContractError("question result outcome differs")
        supporting_claim_ids = [_identifier(claim_id, "supporting_claim_id") for claim_id in _list(item["supporting_claim_ids"], "supporting_claim_ids", 0, 4)]
        _unique_strings(supporting_claim_ids, "supporting claim ids")
        supporting_claim_count = _integer(item["supporting_claim_count"], "supporting_claim_count", 0, 4)
        if supporting_claim_count != len(supporting_claim_ids):
            raise ContractError("supporting claim count differs")
        evidence = _integer(item["evidence_match_millionths"], "evidence_match_millionths", 0, 1_000_000)
        if actual_outcome == "refusal":
            if supporting_claim_ids or evidence != 0:
                raise ContractError("refusal question results must remain unsupported")
        elif not supporting_claim_ids:
            raise ContractError("supported question results must retain evidence")
        question_id = _identifier(item["question_id"], "question_id")
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        if candidate_id not in candidate_counts:
            raise ContractError("question result candidate differs")
        pair = (candidate_id, question_id)
        if pair in candidate_question_pairs:
            raise ContractError("question result candidate bindings must be unique")
        candidate_question_pairs.add(pair)
        candidate_counts[candidate_id] += 1
        question_ids.append(question_id)
        results.append(
            {
                "question_id": question_id,
                "candidate_id": candidate_id,
                "expected_outcome": expected_outcome,
                "actual_outcome": actual_outcome,
                "supporting_claim_ids": supporting_claim_ids,
                "supporting_claim_count": supporting_claim_count,
                "evidence_match_millionths": evidence,
            }
        )
    if len(question_ids) != len(set(question_ids)):
        raise ContractError("question result identifiers must be unique")
    if any(count != 4 for count in candidate_counts.values()):
        raise ContractError("question result candidate coverage differs")
    result = {
        "schema_version": body["schema_version"],
        "question_results_id": _identifier(body["question_results_id"], "question_results_id"),
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "questions_digest": _digest(body["questions_digest"], "questions_digest"),
        "result_count": _integer(body["result_count"], "result_count", _TOTAL_QUESTIONS, _TOTAL_QUESTIONS),
        "results": results,
        **_authority(body),
    }
    _digest_check({**result, "question_results_digest": body["question_results_digest"]}, "question_results_digest", "candidate quality question results")
    result["question_results_digest"] = body["question_results_digest"]
    return result


def validate_candidate_quality_result(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "result_id",
        "campaign_id",
        "candidate_id",
        "candidate_digest",
        "provenance_digest",
        "source_digest",
        "sample_digest",
        "annotations_digest",
        "questions_digest",
        "question_results_digest",
        "total_candidate_claim_count",
        "sampled_claim_count",
        "question_count",
        "classification_counts",
        "question_outcome_counts",
        "automatic_check_counts",
        "recommendation",
        "result_digest",
    ) + AUTHORITY_FIELDS
    body = _mapping(value, keys, "candidate quality result")
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-result.v0.1":
        raise ContractError("candidate quality result schema version differs")
    candidate_id = _identifier(body["candidate_id"], "candidate_id")
    expected_claim_count = {**_CLAIM_COUNTS, **_PUBLIC_CLAIM_COUNTS}.get(candidate_id)
    if expected_claim_count is None:
        raise ContractError("candidate quality result candidate differs")
    class_counts = _mapping(
        body["classification_counts"],
        ANNOTATION_LABELS,
        "classification_counts",
    )
    normalized_class_counts = {
        key: _integer(class_counts[key], key, 0, _TOTAL_SAMPLED_CLAIMS)
        for key in ANNOTATION_LABELS
    }
    question_counts = _mapping(
        body["question_outcome_counts"],
        ("supported", "refusal", "mismatch"),
        "question_outcome_counts",
    )
    normalized_question_counts = {
        key: _integer(question_counts[key], key, 0, 4)
        for key in ("supported", "refusal", "mismatch")
    }
    auto_counts = _mapping(
        body["automatic_check_counts"],
        (
            "verified_claim_count",
            "verified_citation_count",
            "duplicate_suspect_count",
            "fragmentation_suspect_count",
            "binding_error_count",
        ),
        "automatic_check_counts",
    )
    normalized_auto_counts = {
        key: _integer(auto_counts[key], key, 0, _TOTAL_CLAIMS)
        for key in auto_counts
    }
    expected_sampled_claim_count = {**_SAMPLE_ALLOCATIONS, **_PUBLIC_SAMPLE_ALLOCATIONS}[candidate_id]
    sampled_claim_count = _integer(
        body["sampled_claim_count"],
        "sampled_claim_count",
        expected_sampled_claim_count,
        expected_sampled_claim_count,
    )
    if sum(normalized_class_counts.values()) != sampled_claim_count:
        raise ContractError("classification counts differ")
    if sum(normalized_question_counts.values()) != 4:
        raise ContractError("question outcome counts differ")
    if normalized_auto_counts["verified_claim_count"] != expected_claim_count:
        raise ContractError("verified claim count differs")
    if normalized_auto_counts["verified_citation_count"] != expected_claim_count:
        raise ContractError("verified citation count differs")
    recommendation = _text(body["recommendation"], "recommendation", maximum=16)
    if recommendation not in RECOMMENDATIONS:
        raise ContractError("recommendation differs")
    result = {
        "schema_version": body["schema_version"],
        "result_id": _identifier(body["result_id"], "result_id"),
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "candidate_id": candidate_id,
        "candidate_digest": _digest(body["candidate_digest"], "candidate_digest"),
        "provenance_digest": _digest(body["provenance_digest"], "provenance_digest"),
        "source_digest": _digest(body["source_digest"], "source_digest"),
        "sample_digest": _digest(body["sample_digest"], "sample_digest"),
        "annotations_digest": _digest(body["annotations_digest"], "annotations_digest"),
        "questions_digest": _digest(body["questions_digest"], "questions_digest"),
        "question_results_digest": _digest(body["question_results_digest"], "question_results_digest"),
        "total_candidate_claim_count": _integer(body["total_candidate_claim_count"], "total_candidate_claim_count", expected_claim_count, expected_claim_count),
        "sampled_claim_count": sampled_claim_count,
        "question_count": _integer(body["question_count"], "question_count", 4, 4),
        "classification_counts": normalized_class_counts,
        "question_outcome_counts": normalized_question_counts,
        "automatic_check_counts": normalized_auto_counts,
        "recommendation": recommendation,
        **_authority(body),
    }
    _digest_check({**result, "result_digest": body["result_digest"]}, "result_digest", "candidate quality result")
    result["result_digest"] = body["result_digest"]
    return result


def validate_candidate_quality_summary(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "summary_id",
        "campaign_id",
        "correlation_id",
        "terminal_readback_digest",
        "campaign_digest",
        "total_candidate_count",
        "total_sampled_claim_count",
        "total_question_count",
        "candidate_recommendations",
        "corpus_recommendation",
        "summary_digest",
    ) + AUTHORITY_FIELDS
    body = _mapping(value, keys, "candidate quality summary")
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-summary.v0.1":
        raise ContractError("candidate quality summary schema version differs")
    recommendations = []
    candidate_ids: list[str] = []
    for raw in _list(body["candidate_recommendations"], "candidate_recommendations", 6, 6):
        item = _mapping(raw, ("candidate_id", "recommendation", "result_digest"), "candidate recommendation")
        recommendation = _text(item["recommendation"], "recommendation", maximum=16)
        if recommendation not in RECOMMENDATIONS:
            raise ContractError("candidate recommendation differs")
        candidate_id = _identifier(item["candidate_id"], "candidate_id")
        candidate_ids.append(candidate_id)
        recommendations.append(
            {
                "candidate_id": candidate_id,
                "recommendation": recommendation,
                "result_digest": _digest(item["result_digest"], "result_digest"),
            }
        )
    if candidate_ids not in (list(_CLAIM_COUNTS), list(_PUBLIC_CLAIM_COUNTS)):
        raise ContractError("summary candidate order differs")
    corpus_recommendation = _text(body["corpus_recommendation"], "corpus_recommendation", maximum=16)
    if corpus_recommendation not in RECOMMENDATIONS:
        raise ContractError("corpus recommendation differs")
    result = {
        "schema_version": body["schema_version"],
        "summary_id": _identifier(body["summary_id"], "summary_id"),
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "correlation_id": _identifier(body["correlation_id"], "correlation_id"),
        "terminal_readback_digest": _digest(body["terminal_readback_digest"], "terminal_readback_digest"),
        "campaign_digest": _digest(body["campaign_digest"], "campaign_digest"),
        "total_candidate_count": _integer(body["total_candidate_count"], "total_candidate_count", 6, 6),
        "total_sampled_claim_count": _integer(
            body["total_sampled_claim_count"],
            "total_sampled_claim_count",
            _TOTAL_SAMPLED_CLAIMS,
            _TOTAL_SAMPLED_CLAIMS,
        ),
        "total_question_count": _integer(body["total_question_count"], "total_question_count", _TOTAL_QUESTIONS, _TOTAL_QUESTIONS),
        "candidate_recommendations": recommendations,
        "corpus_recommendation": corpus_recommendation,
        **_authority(body),
    }
    _digest_check({**result, "summary_digest": body["summary_digest"]}, "summary_digest", "candidate quality summary")
    result["summary_digest"] = body["summary_digest"]
    return result


def validate_candidate_quality_recovery(value: Any) -> dict[str, Any]:
    keys = (
        "schema_version",
        "recovery_id",
        "campaign_id",
        "campaign_digest",
        "phase",
        "status",
        "retained_artifact_digests",
        "owned_staging_relpaths",
        "recovery_digest",
    ) + AUTHORITY_FIELDS
    body = _mapping(value, keys, "candidate quality recovery")
    if _text(body["schema_version"], "schema_version", maximum=64) != "ao.lore.candidate-quality-recovery.v0.1":
        raise ContractError("candidate quality recovery schema version differs")
    phase = _text(body["phase"], "phase", maximum=16)
    if phase not in RECOVERY_PHASES:
        raise ContractError("recovery phase differs")
    status = _text(body["status"], "status", maximum=24)
    if status not in RECOVERY_STATUSES:
        raise ContractError("recovery status differs")
    retained_artifact_digests = _unique_strings(
        [_digest(item, "retained_artifact_digest") for item in _list(body["retained_artifact_digests"], "retained_artifact_digests", 1, 32)],
        "retained artifact digests",
    )
    owned_staging_relpaths = _unique_strings(
        [_relative_path(item, "owned_staging_relpath") for item in _list(body["owned_staging_relpaths"], "owned_staging_relpaths", 1, 32)],
        "owned staging relpaths",
    )
    result = {
        "schema_version": body["schema_version"],
        "recovery_id": _identifier(body["recovery_id"], "recovery_id"),
        "campaign_id": _identifier(body["campaign_id"], "campaign_id"),
        "campaign_digest": _digest(body["campaign_digest"], "campaign_digest"),
        "phase": phase,
        "status": status,
        "retained_artifact_digests": retained_artifact_digests,
        "owned_staging_relpaths": owned_staging_relpaths,
        **_authority(body),
    }
    _digest_check({**result, "recovery_digest": body["recovery_digest"]}, "recovery_digest", "candidate quality recovery")
    result["recovery_digest"] = body["recovery_digest"]
    return result


__all__ = [
    "ANNOTATION_LABELS",
    "AUTHORITY_FIELDS",
    "QUESTION_CATEGORIES",
    "QUESTION_OUTCOMES",
    "RECOVERY_PHASES",
    "RECOVERY_STATUSES",
    "RECOMMENDATIONS",
    "validate_candidate_quality_annotations",
    "validate_candidate_quality_campaign",
    "validate_candidate_quality_question_results",
    "validate_candidate_quality_questions",
    "validate_candidate_quality_recovery",
    "validate_candidate_quality_result",
    "validate_candidate_quality_sample",
    "validate_candidate_quality_sampling_policy",
    "validate_candidate_quality_summary",
]
