"""Evidence-coverage-first navigation with bounded lexical graph traversal."""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping, Sequence

from .scoring import ScoringError, branch_priority, compute_evidence_coverage


class NavigationError(ValueError):
    """Raised when a plan, ledger event, or budget transition is unsafe."""


_STATUSES = {
    "satisfied",
    "partially_satisfied",
    "missing",
    "contradictory",
    "stale_only",
    "disqualified",
}
_REQUIREMENT_FIELDS = {
    "id",
    "criterion",
    "importance_weight",
    "evidence_type",
    "lifecycle_condition",
    "trust_tier",
    "citation_required",
    "provenance_required",
    "mandatory",
}
_BUDGET_LIMITS = {
    "max_depth": 0,
    "max_nodes": 1,
    "max_tokens": 1,
    "max_seconds": 1,
    "max_replans": 0,
}


def _finite(value: Any, field: str, *, low: float = 0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NavigationError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < low:
        raise NavigationError(f"{field} must be finite and >= {low}")
    return result


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise NavigationError(f"{field} must be a non-empty string")
    return value


def build_requirement_plan(
    query_digest: str,
    profile_id: str,
    requirements: Sequence[Mapping[str, Any]],
    budgets: Mapping[str, Any],
) -> dict[str, Any]:
    """Create a strict initial evidence-requirement plan."""

    _text(query_digest, "query_digest")
    _text(profile_id, "profile_id")
    if not requirements:
        raise NavigationError("at least one requirement is required")
    normalized: list[dict[str, Any]] = []
    identities: set[str] = set()
    for raw in requirements:
        if not isinstance(raw, Mapping):
            raise NavigationError("requirements must be objects")
        # Existing plan objects may be revalidated; derived ledger fields are ignored.
        unknown = set(raw) - _REQUIREMENT_FIELDS - {"status", "evidence_refs", "coverage_gain"}
        if unknown:
            raise NavigationError(f"unknown requirement fields: {', '.join(sorted(unknown))}")
        missing = _REQUIREMENT_FIELDS - set(raw)
        if missing:
            raise NavigationError(f"missing requirement fields: {', '.join(sorted(missing))}")
        requirement_id = _text(raw["id"], "requirement.id")
        if requirement_id in identities:
            raise NavigationError(f"duplicate requirement identity: {requirement_id}")
        identities.add(requirement_id)
        weight = _finite(raw["importance_weight"], f"{requirement_id}.importance_weight")
        if weight == 0:
            raise NavigationError("requirement importance must be positive")
        for field in ("criterion", "evidence_type", "lifecycle_condition"):
            _text(raw[field], f"{requirement_id}.{field}")
        if raw["trust_tier"] is not None:
            _text(raw["trust_tier"], f"{requirement_id}.trust_tier")
        for field in ("citation_required", "provenance_required", "mandatory"):
            if not isinstance(raw[field], bool):
                raise NavigationError(f"{requirement_id}.{field} must be boolean")
        normalized.append(
            {
                **{field: copy.deepcopy(raw[field]) for field in _REQUIREMENT_FIELDS},
                "status": "missing",
                "evidence_refs": [],
                "coverage_gain": 0.0,
            }
        )
    if set(budgets) != set(_BUDGET_LIMITS):
        raise NavigationError("budgets have missing or unknown fields")
    normalized_budgets: dict[str, int] = {}
    for field, minimum in _BUDGET_LIMITS.items():
        value = budgets[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise NavigationError(f"{field} must be an integer >= {minimum}")
        normalized_budgets[field] = value
    return {
        "schema_version": "ao.lore.evidence-requirement-plan.v0.1",
        "query_digest": query_digest,
        "profile_id": profile_id,
        "requirements": normalized,
        "budgets": normalized_budgets,
    }


class CoverageNavigator:
    """Mutable run state whose success condition is evidence sufficiency."""

    def __init__(self, plan: Mapping[str, Any], coverage_profile: Mapping[str, Any]):
        validated = build_requirement_plan(
            plan.get("query_digest"), plan.get("profile_id"), plan.get("requirements", []), plan.get("budgets", {})
        )
        self._query_digest = validated["query_digest"]
        self._requirements = {item["id"]: item for item in validated["requirements"]}
        self._coverage_profile = copy.deepcopy(dict(coverage_profile))
        self._visited: list[str] = []
        self._rejected: dict[str, str] = {}
        self._evidence: dict[str, list[dict[str, Any]]] = {key: [] for key in self._requirements}
        self._coverage_history: list[dict[str, Any]] = []
        self._remaining = {
            "depth": validated["budgets"]["max_depth"],
            "nodes": validated["budgets"]["max_nodes"],
            "tokens": validated["budgets"]["max_tokens"],
            "seconds": validated["budgets"]["max_seconds"],
            "replans": validated["budgets"]["max_replans"],
        }
        self._plan_versions: list[dict[str, Any]] = [
            {"version": 1, "kind": "initial", "target_requirement_ids": sorted(self._requirements), "branches": []}
        ]
        self._last_report: dict[str, Any] | None = None
        self._should_stop = False
        self._total_tokens = 0
        self._traversal_after_target = 0
        self._no_gain_replans = 0

    @property
    def should_stop(self) -> bool:
        """True only for successful evidence sufficiency, not budget exhaustion."""

        return self._should_stop

    def _unresolved(self) -> set[str]:
        return {key for key, item in self._requirements.items() if item["status"] != "satisfied"}

    def choose_branch(self, branches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Choose expected weighted coverage gain per estimated cost."""

        ranked: list[dict[str, Any]] = []
        unresolved = self._unresolved()
        for branch in branches:
            node_id = _text(branch.get("node_id"), "branch.node_id")
            if node_id in self._visited or node_id in self._rejected:
                continue
            cost = _finite(branch.get("estimated_cost"), f"{node_id}.estimated_cost")
            if cost == 0:
                raise NavigationError("branch estimated cost must be positive")
            gains = branch.get("expected_requirement_gains")
            if not isinstance(gains, Mapping):
                raise NavigationError("branch gains must be an object")
            weighted_gain = 0.0
            targeted: list[str] = []
            for requirement_id, raw_gain in gains.items():
                if requirement_id not in self._requirements:
                    raise NavigationError(f"branch targets unknown requirement: {requirement_id}")
                gain = _finite(raw_gain, f"{node_id}.{requirement_id}.gain")
                if gain > 1:
                    raise NavigationError("expected requirement gain cannot exceed 1")
                if requirement_id in unresolved and gain > 0:
                    weighted_gain += self._requirements[requirement_id]["importance_weight"] * gain
                    targeted.append(requirement_id)
            try:
                priority = branch_priority(weighted_gain, cost)
            except ScoringError as exc:
                raise NavigationError(str(exc)) from exc
            ranked.append(
                {
                    "node_id": node_id,
                    "priority": round(priority, 12),
                    "expected_weighted_gain": round(weighted_gain, 12),
                    "estimated_cost": cost,
                    "target_requirement_ids": sorted(targeted),
                }
            )
        if not ranked:
            raise NavigationError("no eligible navigation branch")
        ranked.sort(key=lambda item: (-item["priority"], item["node_id"]))
        return ranked[0]

    def reject_branch(self, node_id: str, reason: str) -> None:
        node_id = _text(node_id, "node_id")
        if node_id in self._visited:
            raise NavigationError("visited nodes cannot be rejected retroactively")
        self._rejected[node_id] = _text(reason, "reason")

    def _validate_events(self, events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        required_fields = {
            "requirement_id",
            "status",
            "evidence_ref",
            "provenance_present",
            "citation_present",
            "freshness_met",
            "trust_met",
            "hard_contradiction",
        }
        for raw in events:
            if not isinstance(raw, Mapping) or set(raw) != required_fields:
                raise NavigationError("evidence events have missing or unknown fields")
            requirement_id = _text(raw["requirement_id"], "evidence.requirement_id")
            if requirement_id not in self._requirements:
                raise NavigationError(f"evidence targets unknown requirement: {requirement_id}")
            status = _text(raw["status"], "evidence.status")
            if status not in _STATUSES:
                raise NavigationError(f"unknown evidence status: {status}")
            event = {
                "requirement_id": requirement_id,
                "status": status,
                "evidence_ref": _text(raw["evidence_ref"], "evidence.evidence_ref"),
            }
            for field in (
                "provenance_present",
                "citation_present",
                "freshness_met",
                "trust_met",
                "hard_contradiction",
            ):
                if not isinstance(raw[field], bool):
                    raise NavigationError(f"evidence.{field} must be boolean")
                event[field] = raw[field]
            normalized.append(event)
        return normalized

    def _aggregate_requirement(self, requirement_id: str) -> None:
        events = self._evidence[requirement_id]
        statuses = {event["status"] for event in events}
        if "contradictory" in statuses or any(event["hard_contradiction"] for event in events):
            status = "contradictory"
        elif "satisfied" in statuses:
            status = "satisfied"
        elif "partially_satisfied" in statuses:
            status = "partially_satisfied"
        elif "stale_only" in statuses:
            status = "stale_only"
        elif "disqualified" in statuses:
            status = "disqualified"
        else:
            status = "missing"
        self._requirements[requirement_id]["status"] = status
        self._requirements[requirement_id]["evidence_refs"] = sorted(
            {event["evidence_ref"] for event in events}
        )

    def _external_gates(self) -> dict[str, bool]:
        relevant_events: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        for requirement_id, requirement in self._requirements.items():
            if requirement["status"] in {"satisfied", "partially_satisfied"}:
                relevant_events.extend((requirement, event) for event in self._evidence[requirement_id])
        # A run containing only stale or disqualified evidence must retain the
        # failed gate instead of treating an empty relevant set as passing.
        if not relevant_events:
            for requirement_id, requirement in self._requirements.items():
                relevant_events.extend((requirement, event) for event in self._evidence[requirement_id])
        provenance = all(not requirement["provenance_required"] or event["provenance_present"] for requirement, event in relevant_events)
        citations = all(not requirement["citation_required"] or event["citation_present"] for requirement, event in relevant_events)
        return {
            "provenance_present": provenance,
            "citations_present": citations,
            "freshness_met": all(event["freshness_met"] for _, event in relevant_events),
            "trust_met": all(event["trust_met"] for _, event in relevant_events),
            "no_hard_contradiction": not any(
                event["hard_contradiction"] or event["status"] == "contradictory"
                for events in self._evidence.values()
                for event in events
            ),
            "deterministic_validation_passed": True,
        }

    def _budget_exhausted(self) -> bool:
        return any(self._remaining[field] <= 0 for field in ("depth", "nodes", "tokens", "seconds"))

    def visit(
        self,
        node_id: str,
        evidence_events: Sequence[Mapping[str, Any]],
        *,
        estimated_tokens: int,
        estimated_seconds: int,
    ) -> dict[str, Any]:
        """Record one evidence-bearing node and recompute sufficiency."""

        node_id = _text(node_id, "node_id")
        if self._should_stop:
            raise NavigationError("navigation already reached evidence sufficiency")
        if node_id in self._visited:
            raise NavigationError("node was already visited")
        if node_id in self._rejected:
            raise NavigationError("rejected branch cannot be visited")
        tokens = estimated_tokens
        seconds = estimated_seconds
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            raise NavigationError("estimated_tokens must be a non-negative integer")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
            raise NavigationError("estimated_seconds must be a non-negative integer")
        if self._remaining["depth"] <= 0 or self._remaining["nodes"] <= 0:
            raise NavigationError("navigation depth or node budget is exhausted")
        if tokens > self._remaining["tokens"] or seconds > self._remaining["seconds"]:
            raise NavigationError("node estimate exceeds remaining token or time budget")
        events = self._validate_events(evidence_events)
        previous_coverage = self._last_report["coverage"] if self._last_report else 0.0

        self._visited.append(node_id)
        self._remaining["depth"] -= 1
        self._remaining["nodes"] -= 1
        self._remaining["tokens"] -= tokens
        self._remaining["seconds"] -= seconds
        self._total_tokens += tokens
        affected: set[str] = set()
        for event in events:
            requirement_id = event["requirement_id"]
            self._evidence[requirement_id].append(event)
            affected.add(requirement_id)
        for requirement_id in affected:
            self._aggregate_requirement(requirement_id)
        try:
            report = compute_evidence_coverage(
                self._query_digest,
                list(self._requirements.values()),
                self._coverage_profile,
                self._external_gates(),
                budget_exhausted=self._budget_exhausted(),
            )
        except ScoringError as exc:
            raise NavigationError(str(exc)) from exc
        gain = round(report["coverage"] - previous_coverage, 12)
        history_event = {
            "event": "evidence-node",
            "node_id": node_id,
            "coverage": report["coverage"],
            "gain": gain,
        }
        self._coverage_history.append(history_event)
        report["coverage_history"] = copy.deepcopy(self._coverage_history)
        self._last_report = report
        self._should_stop = report["decision"] == "answer"
        return copy.deepcopy(report)

    def replan(self, branches: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Append a gap-specific delta plan without resetting run memory."""

        if self._should_stop:
            raise NavigationError("sufficient runs cannot replan")
        if self._remaining["replans"] <= 0:
            raise NavigationError("replan budget is exhausted")
        unresolved = self._unresolved()
        targets: set[str] = set()
        normalized_branches: list[dict[str, Any]] = []
        for branch in branches:
            if not isinstance(branch, Mapping) or set(branch) != {"node_id", "targets"}:
                raise NavigationError("delta branches require node_id and targets")
            node_id = _text(branch["node_id"], "delta.node_id")
            raw_targets = branch["targets"]
            if not isinstance(raw_targets, list) or not raw_targets:
                raise NavigationError("delta branch targets must be non-empty")
            branch_targets = sorted(set(raw_targets))
            if any(target not in unresolved for target in branch_targets):
                raise NavigationError("delta plan must target unresolved requirements")
            targets.update(branch_targets)
            normalized_branches.append({"node_id": node_id, "targets": branch_targets})
        if not targets:
            raise NavigationError("delta plan must target a coverage gap")
        prior_coverage = self._last_report["coverage"] if self._last_report else 0.0
        if len(self._plan_versions) > 1 and self._plan_versions[-1].get("starting_coverage") == prior_coverage:
            self._no_gain_replans += 1
        self._remaining["replans"] -= 1
        delta = {
            "version": len(self._plan_versions) + 1,
            "kind": "coverage-gap-delta",
            "starting_coverage": prior_coverage,
            "target_requirement_ids": sorted(targets),
            "branches": sorted(normalized_branches, key=lambda item: item["node_id"]),
        }
        self._plan_versions.append(delta)
        return copy.deepcopy(delta)

    def trace(self) -> dict[str, Any]:
        """Return an explainable, reasoning-free navigation trace."""

        coverage = self._last_report["coverage"] if self._last_report else 0.0
        satisfied = sum(item["status"] == "satisfied" for item in self._requirements.values())
        unresolved = [item for item in self._requirements.values() if item["status"] != "satisfied"]
        unresolved.sort(key=lambda item: (-item["importance_weight"], item["id"]))
        return {
            "schema_version": "ao.lore.navigation-trace.v0.1",
            "query_digest": self._query_digest,
            "requirements": copy.deepcopy(list(self._requirements.values())),
            "visited_nodes": list(self._visited),
            "rejected_branches": copy.deepcopy(self._rejected),
            "plan_versions": copy.deepcopy(self._plan_versions),
            "coverage_history": copy.deepcopy(self._coverage_history),
            "remaining_budgets": copy.deepcopy(self._remaining),
            "nodes_per_satisfied_requirement": len(self._visited) / satisfied if satisfied else None,
            "evidence_yield_per_node": coverage / len(self._visited) if self._visited else 0.0,
            "tokens_per_coverage_point": self._total_tokens / coverage if coverage else None,
            "traversal_after_target": self._traversal_after_target,
            "replans_with_no_coverage_gain": self._no_gain_replans,
            "highest_weight_unresolved_requirement": unresolved[0]["id"] if unresolved else None,
            "final_coverage_report": copy.deepcopy(self._last_report),
            "private_reasoning_persisted": False,
        }
