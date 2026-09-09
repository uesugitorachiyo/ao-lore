"""Evidence-ledger-only answer synthesis with deterministic claim binding."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from .benchmark import BenchmarkError, canonical_digest


class SynthesisError(ValueError):
    """Raised when evidence or a draft cannot support the requested answer."""


@dataclass(frozen=True)
class SynthesisDraft:
    claim_ids: Sequence[str]
    qualifications: Sequence[str]


class SynthesizerAdapter(Protocol):
    def synthesize(self, payload: Mapping[str, Any]) -> SynthesisDraft: ...


class ScriptedSynthesizer:
    """Deterministic adapter with no retrieval or mutation surface."""

    def __init__(self, handler: Callable[[Mapping[str, Any]], SynthesisDraft]) -> None:
        self._handler = handler

    def synthesize(self, payload: Mapping[str, Any]) -> SynthesisDraft:
        result = self._handler(copy.deepcopy(payload))
        if not isinstance(result, SynthesisDraft):
            raise SynthesisError("synthesizer adapter must return SynthesisDraft")
        return result


class DeterministicSynthesizer:
    """Local baseline that selects every verified claim in ledger order."""

    def synthesize(self, payload: Mapping[str, Any]) -> SynthesisDraft:
        claim_ids = [
            claim["claim_id"]
            for entry in payload["evidence_ledger"]
            for claim in entry["supported_claims"]
        ]
        qualifications = []
        if payload["qualification_requirements"].get("include_limitations"):
            qualifications.append("The answer is limited to the verified evidence ledger supplied to synthesis.")
        return SynthesisDraft(claim_ids=claim_ids, qualifications=qualifications)


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise SynthesisError(f"{field} must be a string")
    return value


def _validate_coverage_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, Mapping):
        raise SynthesisError("coverage report must be an object")
    required = {
        "schema_version",
        "query_digest",
        "profile_id",
        "target",
        "coverage",
        "requirements",
        "gates",
        "coverage_history",
        "decision",
        "reason",
    }
    if set(report) != required or report["schema_version"] != "ao.lore.evidence-coverage-report.v0.1":
        raise SynthesisError("coverage report has missing, unknown, or unsupported fields")
    gates = report["gates"]
    gate_names = {
        "coverage_target_met",
        "mandatory_requirements_met",
        "provenance_present",
        "citations_present",
        "freshness_met",
        "trust_met",
        "no_hard_contradiction",
        "deterministic_validation_passed",
    }
    if not isinstance(gates, Mapping) or set(gates) != gate_names or any(
        not isinstance(value, bool) for value in gates.values()
    ):
        raise SynthesisError("coverage report sufficiency gates are malformed")
    decision = report["decision"]
    if decision not in {"answer", "partial", "refuse", "replan"}:
        raise SynthesisError("coverage report decision is invalid")
    if decision == "answer" and not all(gates.values()):
        raise SynthesisError("answer decision requires every sufficiency gate")
    if isinstance(report["coverage"], bool) or not isinstance(report["coverage"], (int, float)):
        raise SynthesisError("coverage must be numeric")
    if isinstance(report["target"], bool) or not isinstance(report["target"], (int, float)):
        raise SynthesisError("coverage target must be numeric")
    if decision == "answer" and report["coverage"] < report["target"]:
        raise SynthesisError("answer decision is below its coverage target")
    if not isinstance(report["requirements"], list) or not isinstance(report["coverage_history"], list):
        raise SynthesisError("coverage requirements and history must be arrays")
    try:
        canonical_digest(report)
    except BenchmarkError as exc:
        raise SynthesisError("coverage report must be strict JSON data") from exc
    return copy.deepcopy(dict(report))


def _validate_ledger(entries: Any, *, citation_required: bool) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(entries, list):
        raise SynthesisError("evidence ledger must be an array")
    required = {
        "evidence_id",
        "requirement_ids",
        "supported_claims",
        "source_ref",
        "source_digest",
        "citation",
        "provenance",
        "freshness_met",
        "trust_met",
        "verified",
    }
    normalized: list[dict[str, Any]] = []
    evidence_ids: set[str] = set()
    claims: dict[str, dict[str, Any]] = {}
    for raw in entries:
        if not isinstance(raw, Mapping) or set(raw) != required:
            raise SynthesisError("evidence entry has missing or unknown fields")
        evidence_id = _string(raw["evidence_id"], "evidence_id")
        if evidence_id in evidence_ids:
            raise SynthesisError("evidence identities must be unique")
        evidence_ids.add(evidence_id)
        requirement_ids = raw["requirement_ids"]
        if not isinstance(requirement_ids, list) or not requirement_ids or any(
            not isinstance(item, str) or not item for item in requirement_ids
        ):
            raise SynthesisError("evidence requirement bindings are malformed")
        source_ref = _string(raw["source_ref"], "source_ref")
        digest = _string(raw["source_digest"], "source_digest")
        if len(digest) != 71 or not digest.startswith("sha256:"):
            raise SynthesisError("source digest must be SHA-256")
        citation = _string(raw["citation"], "citation", allow_empty=True)
        if citation_required and not citation:
            raise SynthesisError("required evidence citation is missing")
        if not isinstance(raw["provenance"], Mapping) or not raw["provenance"]:
            raise SynthesisError("evidence provenance is missing")
        for field in ("freshness_met", "trust_met", "verified"):
            if not isinstance(raw[field], bool):
                raise SynthesisError(f"evidence {field} must be boolean")
        if raw["verified"] is not True:
            raise SynthesisError("unverified evidence cannot enter synthesis")
        supported = raw["supported_claims"]
        if not isinstance(supported, list) or not supported:
            raise SynthesisError("evidence must declare supported claims")
        normalized_claims: list[dict[str, str]] = []
        for claim in supported:
            if not isinstance(claim, Mapping) or set(claim) != {"claim_id", "text"}:
                raise SynthesisError("supported claim is malformed")
            claim_id = _string(claim["claim_id"], "claim_id")
            text = _string(claim["text"], "claim.text")
            if claim_id in claims:
                raise SynthesisError("claim identities must be unique")
            bound = {
                "claim_id": claim_id,
                "text": text,
                "evidence_id": evidence_id,
                "source_ref": source_ref,
                "citation": citation,
            }
            claims[claim_id] = bound
            normalized_claims.append({"claim_id": claim_id, "text": text})
        normalized.append(
            {
                "evidence_id": evidence_id,
                "requirement_ids": list(requirement_ids),
                "supported_claims": normalized_claims,
                "source_ref": source_ref,
                "source_digest": digest,
                "citation": citation,
                "provenance": copy.deepcopy(dict(raw["provenance"])),
                "freshness_met": raw["freshness_met"],
                "trust_met": raw["trust_met"],
                "verified": True,
            }
        )
    try:
        canonical_digest(normalized)
    except BenchmarkError as exc:
        raise SynthesisError("evidence ledger must be strict JSON data") from exc
    return normalized, claims


def _verify_coverage_bindings(report: Mapping[str, Any], entries: Sequence[Mapping[str, Any]]) -> None:
    source_refs = {entry["source_ref"] for entry in entries}
    evidence_ids = {entry["evidence_id"] for entry in entries}
    evidence_requirement_ids = {
        requirement_id for entry in entries for requirement_id in entry["requirement_ids"]
    }
    for requirement in report["requirements"]:
        if not isinstance(requirement, Mapping):
            raise SynthesisError("coverage requirement result is malformed")
        requirement_id = requirement.get("id")
        status = requirement.get("status")
        refs = requirement.get("evidence_refs")
        if not isinstance(requirement_id, str) or not isinstance(refs, list):
            raise SynthesisError("coverage requirement binding is malformed")
        if status == "satisfied":
            if requirement_id not in evidence_requirement_ids:
                raise SynthesisError("satisfied requirement lacks a ledger binding")
            if any(ref not in source_refs and ref not in evidence_ids for ref in refs):
                raise SynthesisError("coverage report references evidence outside the ledger")


def _render(
    status: str,
    selected_claims: Sequence[Mapping[str, Any]],
    qualifications: Sequence[str],
    answer_format: str,
) -> str | dict[str, Any]:
    prefix = {
        "answer": "Verified evidence:",
        "partial": "Insufficient evidence for a complete answer. Verified subset:",
    }[status]
    if answer_format == "structured":
        return {
            "summary": prefix,
            "claims": [
                {"text": claim["text"], "citation": claim["citation"], "source_ref": claim["source_ref"]}
                for claim in selected_claims
            ],
            "qualifications": list(qualifications),
        }
    lines = [prefix]
    for claim in selected_claims:
        lines.append(f"- {claim['text']} {claim['citation']}".rstrip())
    if qualifications:
        lines.append("Qualifications:")
        lines.extend(f"- {item}" for item in qualifications)
    return "\n".join(lines)


def synthesize_from_evidence(
    normalized_query: str,
    evidence_ledger: Sequence[Mapping[str, Any]],
    coverage_report: Mapping[str, Any],
    answer_format: str,
    citation_requirements: Mapping[str, Any],
    qualification_requirements: Mapping[str, Any],
    adapter: SynthesizerAdapter,
) -> dict[str, Any]:
    """Synthesize only exact, verified ledger claims under the final decision."""

    _string(normalized_query, "normalized_query")
    if answer_format not in {"text", "structured"}:
        raise SynthesisError("unsupported answer format")
    if not isinstance(citation_requirements, Mapping) or not isinstance(
        qualification_requirements, Mapping
    ):
        raise SynthesisError("citation and qualification requirements must be objects")
    report = _validate_coverage_report(coverage_report)
    entries, claim_index = _validate_ledger(
        evidence_ledger, citation_required=citation_requirements.get("required") is True
    )
    _verify_coverage_bindings(report, entries)
    if report["decision"] == "replan":
        raise SynthesisError("coverage-gap replan must return to navigation")
    if report["decision"] == "answer" and any(
        not entry["freshness_met"] or not entry["trust_met"] for entry in entries
    ):
        raise SynthesisError("answer ledger contradicts freshness or trust gates")
    if report["decision"] == "refuse":
        refusal = "The evidence policy requires refusal; no answer claims were emitted."
        return {
            "schema_version": "ao.lore.synthesis-result.v0.1",
            "status": "refuse",
            "answer": refusal if answer_format == "text" else {"summary": refusal, "claims": [], "qualifications": []},
            "claim_ids": [],
            "evidence_ids": [],
            "coverage_report_digest": canonical_digest(report),
            "private_reasoning_persisted": False,
        }

    payload = {
        "normalized_query": normalized_query,
        "evidence_ledger": entries,
        "coverage_report": report,
        "answer_format": answer_format,
        "citation_requirements": copy.deepcopy(dict(citation_requirements)),
        "qualification_requirements": copy.deepcopy(dict(qualification_requirements)),
    }
    draft = adapter.synthesize(copy.deepcopy(payload))
    if not isinstance(draft, SynthesisDraft):
        raise SynthesisError("synthesizer adapter returned an invalid draft")
    if len(set(draft.claim_ids)) != len(draft.claim_ids):
        raise SynthesisError("synthesis draft repeats a claim")
    if any(claim_id not in claim_index for claim_id in draft.claim_ids):
        raise SynthesisError("synthesis draft contains an unsupported claim")
    if len(draft.qualifications) > 32 or any(
        not isinstance(item, str) or not item or len(item) > 512
        or len(item.encode("utf-8")) > 2048 for item in draft.qualifications
    ):
        raise SynthesisError("synthesis qualifications must be non-empty strings")
    selected = [claim_index[claim_id] for claim_id in draft.claim_ids]
    status = "answer" if report["decision"] == "answer" else "partial"
    answer = _render(status, selected, draft.qualifications, answer_format)
    return {
        "schema_version": "ao.lore.synthesis-result.v0.1",
        "status": status,
        "answer": answer,
        "claim_ids": list(draft.claim_ids),
        "evidence_ids": sorted({claim["evidence_id"] for claim in selected}),
        "coverage_report_digest": canonical_digest(report),
        "private_reasoning_persisted": False,
    }
