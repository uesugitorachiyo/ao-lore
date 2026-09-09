"""Pure direct-reference queries over a validated immutable workspace snapshot."""

from __future__ import annotations

import copy
import json
import unicodedata
from typing import Iterable

from ._strict_io import ContractError
from .benchmark import canonical_digest
from .evidence_graph_contracts import (
    AUTHORITY_FIELDS,
    validate_detached_freshness_summary_against_manifest,
    validate_graph_manifest,
)
from .evidence_query import (
    EvidenceQueryError,
    _selected_evidence_records,
    query_evidence_graph,
)
from .document_evidence_contracts import validate_workspace_document_generation
from .document_evidence_query import (
    DocumentEvidenceQueryError,
    query_workspace_documents,
)
from .workspace_contracts import (
    WORKSPACE_SCHEMA_VERSIONS,
    WORKSPACE_QUERY_READBACK_VERSIONS,
    validate_workspace_query_readback,
    validate_workspace_registry_generation,
)
from .workspace_registry import (
    WorkspaceRegistryError,
    WorkspaceRegistrySnapshot,
    WorkspaceSelection,
    select_workspace,
)


class WorkspaceQueryError(ValueError):
    """The immutable workspace selection cannot support a safe query."""


def _contains_category_c(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _validate_prompt(prompt: str) -> None:
    if (
        type(prompt) is not str
        or not 1 <= len(prompt) <= 1024
        or _contains_category_c(prompt)
    ):
        raise WorkspaceQueryError("workspace query prompt is invalid")


def _json_bytes(value: dict) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class WorkspaceGraphSnapshot:
    """One validated graph and optional sealed freshness capability."""

    __slots__ = ("_workspace_id", "_graph_bytes", "_freshness_summary")

    def __init__(self, workspace_id: str, graph: dict,
                 freshness_summary: dict | None = None) -> None:
        if type(workspace_id) is not str:
            raise WorkspaceQueryError("workspace graph identity is invalid")
        try:
            validated = validate_graph_manifest(graph)
            freshness = None
            if freshness_summary is not None:
                validate_detached_freshness_summary_against_manifest(
                    freshness_summary, validated,
                )
                freshness = copy.deepcopy(freshness_summary)
        except (ContractError, TypeError, ValueError) as exc:
            raise WorkspaceQueryError("workspace graph snapshot is invalid") from exc
        object.__setattr__(self, "_workspace_id", workspace_id)
        object.__setattr__(self, "_graph_bytes", _json_bytes(validated))
        object.__setattr__(self, "_freshness_summary", freshness)

    def __setattr__(self, _name, _value):
        raise AttributeError("workspace graph snapshots are immutable")

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def _open(self) -> tuple[dict, dict | None]:
        graph = json.loads(self._graph_bytes)
        freshness = (
            None if self._freshness_summary is None
            else copy.deepcopy(self._freshness_summary)
        )
        return graph, freshness


class WorkspaceDocumentQuerySnapshot:
    """One immutable verified document generation captured before querying."""

    __slots__ = ("_workspace_id", "_generation_bytes")

    def __init__(self, workspace_id: str, generation: dict) -> None:
        try:
            validated = validate_workspace_document_generation(generation)
            if type(workspace_id) is not str or validated["workspace_id"] != workspace_id:
                raise WorkspaceQueryError("workspace document identity differs")
        except (ContractError, TypeError, ValueError) as exc:
            raise WorkspaceQueryError("workspace document snapshot is invalid") from exc
        object.__setattr__(self, "_workspace_id", workspace_id)
        object.__setattr__(self, "_generation_bytes", _json_bytes(validated))

    def __setattr__(self, _name, _value):
        raise AttributeError("workspace document snapshots are immutable")

    @property
    def workspace_id(self) -> str:
        return self._workspace_id

    def _open(self) -> dict:
        return json.loads(self._generation_bytes)


class WorkspaceQuerySnapshot:
    """Closed registry and graph material captured before query execution."""

    __slots__ = (
        "_generation_bytes", "_primary_workspace_id", "_graphs", "_documents", "_conflict_bytes",
    )

    def __init__(self, registry: WorkspaceRegistrySnapshot,
                 selection: WorkspaceSelection,
                 graphs: Iterable[WorkspaceGraphSnapshot], *,
                 documents: Iterable[WorkspaceDocumentQuerySnapshot] = (),
                 cross_workspace_conflicts: tuple[tuple[tuple[str, str, str, str],
                                                        tuple[str, str, str, str]], ...] = ()) -> None:
        try:
            if not isinstance(registry, WorkspaceRegistrySnapshot) or not registry.generations:
                raise WorkspaceQueryError("workspace registry snapshot is invalid")
            generation = validate_workspace_registry_generation(registry.generations[-1])
            if type(selection) is not WorkspaceSelection:
                raise WorkspaceQueryError("workspace selection differs")
            expected_selection = select_workspace(
                registry, selection.primary.get("workspace_id"),
            )
            if selection != expected_selection or selection.generation != generation:
                raise WorkspaceQueryError("workspace selection differs")
            items = tuple(graphs)
            document_items = tuple(documents)
            if any(type(item) is not WorkspaceGraphSnapshot for item in items):
                raise WorkspaceQueryError("workspace graph coverage differs")
            if any(type(item) is not WorkspaceDocumentQuerySnapshot for item in document_items):
                raise WorkspaceQueryError("workspace document coverage differs")
            ids = [item.workspace_id for item in items]
            if len(ids) != len(set(ids)):
                raise WorkspaceQueryError("workspace graph identities differ")
            document_ids = [item.workspace_id for item in document_items]
            if len(document_ids) != len(set(document_ids)):
                raise WorkspaceQueryError("workspace document identities differ")
            selected_definitions = (selection.primary, *selection.references)
            definitions = {
                item["workspace_id"]: item for item in selected_definitions
            }
            expected_graphs = {
                workspace_id for workspace_id, definition in definitions.items()
                if definition["graph_id"] is not None
            }
            expected_documents = {
                workspace_id for workspace_id, definition in definitions.items()
                if definition.get("document_generation_digest") is not None
            }
            if set(ids) != expected_graphs:
                raise WorkspaceQueryError("workspace graph coverage differs")
            if set(document_ids) != expected_documents:
                raise WorkspaceQueryError("workspace document coverage differs")
            ordered = tuple(sorted(items, key=lambda item: item.workspace_id))
            ordered_documents = tuple(sorted(document_items, key=lambda item: item.workspace_id))
            graph_values = {}
            for item in ordered:
                graph, freshness = item._open()
                graph_values[item.workspace_id] = graph
                definition = definitions[item.workspace_id]
                if (graph["graph_id"], graph["graph_digest"]) != (
                    definition["graph_id"], definition["graph_digest"],
                ):
                    raise WorkspaceQueryError("workspace graph binding differs")
                if definition["freshness_summary_status"] == "not_observed":
                    if freshness is not None:
                        raise WorkspaceQueryError("workspace freshness summary is not declared")
                    continue
                if freshness is None:
                    raise WorkspaceQueryError("workspace freshness summary is required")
                summary = validate_detached_freshness_summary_against_manifest(
                    freshness, graph,
                )
                expected = (
                    definition["freshness_summary_id"],
                    definition["freshness_summary_digest"],
                    definition["graph_id"], definition["graph_digest"],
                    definition["freshness_policy_id"],
                    definition["freshness_policy_digest"],
                )
                actual = (
                    summary["summary_id"], summary["summary_digest"],
                    summary["graph_id"], summary["graph_digest"],
                    summary["policy_id"], summary["policy_digest"],
                )
                if actual != expected:
                    raise WorkspaceQueryError("workspace freshness summary binding differs")
            for item in ordered_documents:
                generation_value = item._open()
                definition = definitions[item.workspace_id]
                if (
                    generation_value["workspace_id"] != item.workspace_id
                    or generation_value["registry_digest"] != generation["predecessor_registry_digest"]
                    or generation["predecessor_registry_digest"] is None
                ):
                    raise WorkspaceQueryError("workspace document lineage differs")
                if (
                    generation_value["document_store_id"],
                    generation_value["generation_digest"],
                ) != (
                    definition["document_store_id"],
                    definition["document_generation_digest"],
                ):
                    raise WorkspaceQueryError("workspace document binding differs")
            conflicts = _validate_cross_workspace_conflicts(
                cross_workspace_conflicts, definitions, graph_values,
            )
        except WorkspaceQueryError:
            raise
        except (ContractError, TypeError, ValueError) as exc:
            raise WorkspaceQueryError("workspace query snapshot is invalid") from exc
        object.__setattr__(self, "_generation_bytes", _json_bytes(generation))
        object.__setattr__(
            self, "_primary_workspace_id", selection.primary["workspace_id"],
        )
        object.__setattr__(self, "_graphs", ordered)
        object.__setattr__(self, "_documents", ordered_documents)
        object.__setattr__(self, "_conflict_bytes", json.dumps(conflicts).encode("utf-8"))

    def __setattr__(self, _name, _value):
        raise AttributeError("workspace query snapshots are immutable")

    @property
    def registry(self) -> WorkspaceRegistrySnapshot:
        generation = json.loads(self._generation_bytes)
        return WorkspaceRegistrySnapshot((generation,), tuple(generation["workspaces"]))

    @property
    def graphs(self) -> tuple[WorkspaceGraphSnapshot, ...]:
        return self._graphs

    @property
    def documents(self) -> tuple[WorkspaceDocumentQuerySnapshot, ...]:
        return self._documents

    def _open(self) -> tuple[dict, dict[str, tuple[dict, dict | None]]]:
        generation = json.loads(self._generation_bytes)
        return generation, {item.workspace_id: item._open() for item in self._graphs}

    def _open_progressive(self) -> tuple[dict, dict[str, tuple[dict, dict | None]], dict[str, dict]]:
        generation, graphs = self._open()
        return generation, graphs, {
            item.workspace_id: item._open() for item in self._documents
        }

    def _conflicts(self) -> tuple[tuple[tuple[str, str, str, str],
                                       tuple[str, str, str, str]], ...]:
        return tuple(tuple(tuple(endpoint) for endpoint in binding)
                     for binding in json.loads(self._conflict_bytes))


def _validate_cross_workspace_conflicts(
    value: object, definitions: dict[str, dict], graphs: dict[str, dict],
) -> tuple[tuple[tuple[str, str, str, str], tuple[str, str, str, str]], ...]:
    if type(value) is not tuple:
        raise WorkspaceQueryError("workspace conflict bindings are invalid")
    if len(value) > 128:
        raise WorkspaceQueryError("workspace conflict binding budget exceeded")
    result = []
    for binding in value:
        if (type(binding) is not tuple or len(binding) != 2
                or any(type(endpoint) is not tuple or len(endpoint) != 4
                       or any(type(field) is not str for field in endpoint)
                       for endpoint in binding)):
            raise WorkspaceQueryError("workspace conflict binding is invalid")
        if binding != tuple(sorted(binding)) or binding[0][0] == binding[1][0]:
            raise WorkspaceQueryError("workspace conflict binding order differs")
        workspace_ids = {binding[0][0], binding[1][0]}
        if any(workspace_id not in definitions for workspace_id in workspace_ids):
            raise WorkspaceQueryError("workspace conflict endpoint differs")
        left, right = binding[0][0], binding[1][0]
        if (right not in definitions[left]["reference_workspace_ids"]
                and left not in definitions[right]["reference_workspace_ids"]):
            raise WorkspaceQueryError("workspace conflict is not directly declared")
        for workspace_id, graph_id, claim_id, claim_digest in binding:
            graph = graphs[workspace_id]
            claim = next((item for item in graph["claims"]
                          if item["claim_id"] == claim_id), None)
            if (graph_id != graph["graph_id"] or claim is None
                    or claim["excerpt_digest"] != claim_digest):
                raise WorkspaceQueryError("workspace conflict endpoint differs")
            explicit = any(
                edge["edge_type"] == "conflicts_with" and (
                    (edge["source_kind"], edge["source_id"], edge["source_evidence_digest"])
                    == ("claim", claim_id, claim_digest)
                    or (edge["target_kind"], edge["target_id"], edge["target_evidence_digest"])
                    == ("claim", claim_id, claim_digest)
                )
                for edge in graph["edges"]
            )
            if not explicit:
                raise WorkspaceQueryError("workspace conflict endpoint differs")
        result.append(binding)
    if tuple(result) != tuple(sorted(result)) or len(result) != len(set(result)):
        raise WorkspaceQueryError("workspace conflict bindings are not ordered and unique")
    return tuple(result)


_RESTRICTED_REFUSAL = "Restricted evidence cannot support a workspace answer."
_SATURATION = "Evidence budget cannot represent every required gate identity."
_GATE_QUALIFICATIONS = (
    _RESTRICTED_REFUSAL,
    "Conflicting or stale evidence remains unresolved.",
    "Source freshness requires review.",
    _SATURATION,
)


def _qualifications(values: Iterable[str]) -> list[str]:
    unique = set(values)
    mandatory = [item for item in _GATE_QUALIFICATIONS if item in unique]
    optional = sorted(unique - set(mandatory))
    return [*mandatory, *optional][:16]


def _cross_workspace_conflict_evidence(
    bindings: Iterable[tuple[tuple[str, str, str, str],
                             tuple[str, str, str, str]]],
    selected_origins: Iterable[dict],
) -> list[dict]:
    """Return selected origins joined by an exact snapshot conflict binding."""

    selected = {}
    for item in selected_origins:
        if item["evidence_kind"] == "claim":
            identity = (
                item["workspace_id"], item["graph_id"],
                item["evidence_id"], item["evidence_digest"],
            )
            selected[identity] = item
    conflicts = {}
    for left, right in bindings:
        if left in selected and right in selected:
            conflicts[tuple(selected[left].values())] = selected[left]
            conflicts[tuple(selected[right].values())] = selected[right]
    return sorted(conflicts.values(), key=lambda item: tuple(item.values()))


def _query_graph_only_legacy(snapshot: WorkspaceQuerySnapshot, primary_workspace_id: str,
                             prompt: str) -> dict:
    """Query a primary and only its sorted direct references, entirely in memory."""

    if type(snapshot) is not WorkspaceQuerySnapshot:
        raise WorkspaceQueryError("workspace query snapshot is invalid")
    generation, graphs = snapshot._open()
    if primary_workspace_id != snapshot._primary_workspace_id:
        raise WorkspaceQueryError("workspace selection differs")
    try:
        registry = WorkspaceRegistrySnapshot((generation,), tuple(generation["workspaces"]))
        selection = select_workspace(registry, primary_workspace_id)
    except WorkspaceRegistryError as exc:
        raise WorkspaceQueryError(str(exc)) from exc

    selected_definitions = (selection.primary, *selection.references)
    readbacks = []
    mandatory = []
    optional = []
    qualifications = []
    restricted = False
    try:
        for definition in selected_definitions:
            graph, freshness = graphs[definition["workspace_id"]]
            readback = query_evidence_graph(
                graph, prompt, freshness_summary=freshness,
            )
            records = _selected_evidence_records(graph, readback["evidence_ids"])
            origins = [{
                "workspace_id": definition["workspace_id"],
                "graph_id": graph["graph_id"],
                "source_id": item["source_id"],
                "evidence_kind": item["evidence_kind"],
                "evidence_id": item["evidence_id"],
                "evidence_digest": item["evidence_digest"],
            } for item in records]
            restricted = restricted or any(
                item["authority_role"] == "case_evidence" for item in records
            )
            (mandatory if readback["outcome"] == "investigate" else optional).extend(origins)
            qualifications.extend(readback["qualifications"])
            readbacks.append(readback)
    except KeyError as exc:
        raise WorkspaceQueryError("workspace graph coverage differs") from exc
    except (ContractError, EvidenceQueryError) as exc:
        raise WorkspaceQueryError("workspace graph query failed") from exc

    cross_conflicts = _cross_workspace_conflict_evidence(
        snapshot._conflicts(),
        (*mandatory, *optional),
    )
    if cross_conflicts:
        mandatory.extend(cross_conflicts)
        qualifications.append("Conflicting or stale evidence remains unresolved.")

    if restricted:
        outcome, evidence = "refuse", []
        qualifications.append(_RESTRICTED_REFUSAL)
    else:
        unique_mandatory = {tuple(item.values()): item for item in mandatory}
        unique_optional = {tuple(item.values()): item for item in optional}
        for identity in unique_mandatory:
            unique_optional.pop(identity, None)
        mandatory_items = sorted(unique_mandatory.values(), key=lambda item: tuple(item.values()))
        optional_items = sorted(unique_optional.values(), key=lambda item: tuple(item.values()))
        saturated = len(mandatory_items) > 128
        evidence = [] if saturated else list(mandatory_items)
        if not saturated:
            evidence.extend(optional_items[:128 - len(evidence)])
        evidence.sort(key=lambda item: tuple(item.values()))
        if saturated:
            outcome = "investigate"
            qualifications.append(_SATURATION)
        elif any(item["outcome"] == "investigate" for item in readbacks):
            outcome = "investigate"
        elif cross_conflicts:
            outcome = "investigate"
        elif any(item["outcome"] == "answer" for item in readbacks):
            outcome = "answer"
        elif any(item["outcome"] == "partial" for item in readbacks):
            outcome = "partial"
        else:
            outcome, evidence = "refuse", []

    qualifications = _qualifications(qualifications)
    reason = "ok"
    if outcome == "investigate":
        reason = (
            "freshness_investigation_required"
            if "Source freshness requires review." in qualifications
            else "workspace_binding_drift"
        )
    elif outcome == "refuse":
        reason = "workspace_binding_drift"
    consulted = sorted(item["workspace_id"] for item in selected_definitions)
    core = {
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[4],
        "query_id": "query-" + canonical_digest({
            "prompt": prompt, "registry": generation["registry_digest"],
            "primary_workspace_id": primary_workspace_id,
        }).split(":", 1)[1][:24],
        "prompt_digest": canonical_digest(prompt),
        "registry_id": generation["registry_id"],
        "registry_digest": generation["registry_digest"],
        "primary_workspace_id": primary_workspace_id,
        "consulted_workspace_ids": consulted,
        "outcome": outcome,
        "reason_code": reason,
        "evidence": evidence,
        "qualifications": qualifications,
        "readback_digest": "sha256:" + "0" * 64,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    core["readback_digest"] = canonical_digest({
        key: value for key, value in core.items() if key != "readback_digest"
    })
    try:
        return validate_workspace_query_readback(
            core, generation=generation, selected_workspace_ids=set(consulted),
            graph_manifests=[graphs[item["workspace_id"]][0]
                             for item in selected_definitions],
        )
    except ContractError as exc:
        raise WorkspaceQueryError("workspace query readback is invalid") from exc


def _progressive_origin_key(item: dict) -> tuple[str, ...]:
    return tuple(str(value) for value in item.values())


def _query_progressive(snapshot: WorkspaceQuerySnapshot, primary_workspace_id: str,
                       prompt: str) -> dict:
    _validate_prompt(prompt)
    generation, graphs, documents = snapshot._open_progressive()
    if primary_workspace_id != snapshot._primary_workspace_id:
        raise WorkspaceQueryError("workspace selection differs")
    try:
        registry = WorkspaceRegistrySnapshot((generation,), tuple(generation["workspaces"]))
        selection = select_workspace(registry, primary_workspace_id)
    except WorkspaceRegistryError as exc:
        raise WorkspaceQueryError(str(exc)) from exc
    selected_definitions = (selection.primary, *selection.references)
    restricted = False
    mandatory: list[dict] = []
    optional: list[dict] = []
    qualifications: list[str] = []
    component_outcomes: list[str] = []
    graph_fallback = False

    for definition in selected_definitions:
        workspace_id = definition["workspace_id"]
        document_generation = documents.get(workspace_id)
        if document_generation is not None:
            try:
                readback = query_workspace_documents(
                    document_generation, workspace_id, prompt, limit=128,
                )
            except DocumentEvidenceQueryError as exc:
                raise WorkspaceQueryError("workspace document query failed") from exc
            document_origins = [{
                "workspace_id": workspace_id,
                "document_store_id": document_generation["document_store_id"],
                "generation_digest": document_generation["generation_digest"],
                "document_id": item["document_id"],
                "source_id": item["source_id"],
                "evidence_kind": "document_block",
                "evidence_id": item["evidence_id"],
                "evidence_digest": item["block_digest"],
            } for item in readback["evidence"]]
            restricted = restricted or readback["reason_code"] == "restricted_evidence"
            target = mandatory if readback["outcome"] == "investigate" else optional
            target.extend(document_origins)
            qualifications.extend(readback["qualifications"])
            component_outcomes.append(readback["outcome"])

        graph_value = graphs.get(workspace_id)
        if graph_value is not None:
            graph, freshness = graph_value
            try:
                readback = query_evidence_graph(graph, prompt, freshness_summary=freshness)
                records = _selected_evidence_records(graph, readback["evidence_ids"])
            except (ContractError, EvidenceQueryError, TypeError, ValueError) as exc:
                if document_generation is None or definition["freshness_summary_status"] == "observed":
                    raise WorkspaceQueryError("workspace graph query failed") from exc
                graph_fallback = True
                qualifications.append("Optional relationship evidence was unavailable.")
                continue
            graph_origins = [{
                "workspace_id": workspace_id,
                "graph_id": graph["graph_id"],
                "source_id": item["source_id"],
                "evidence_kind": "graph_claim" if item["evidence_kind"] == "claim" else "graph_edge",
                "evidence_id": item["evidence_id"],
                "evidence_digest": item["evidence_digest"],
            } for item in records]
            restricted = restricted or any(item["authority_role"] == "case_evidence" for item in records)
            target = mandatory if readback["outcome"] == "investigate" else optional
            target.extend(graph_origins)
            qualifications.extend(readback["qualifications"])
            component_outcomes.append(readback["outcome"])

    selected_graph_origins = {
        (item["workspace_id"], item["graph_id"], item["evidence_id"], item["evidence_digest"]): item
        for item in (*mandatory, *optional) if item["evidence_kind"] == "graph_claim"
    }
    for left, right in snapshot._conflicts():
        if left in selected_graph_origins and right in selected_graph_origins:
            mandatory.extend((selected_graph_origins[left], selected_graph_origins[right]))
            qualifications.append("Conflicting or stale evidence remains unresolved.")
            component_outcomes.append("investigate")

    unique_mandatory = {_progressive_origin_key(item): item for item in mandatory}
    unique_optional = {_progressive_origin_key(item): item for item in optional}
    for identity in unique_mandatory:
        unique_optional.pop(identity, None)
    mandatory_items = sorted(unique_mandatory.values(), key=_progressive_origin_key)
    optional_items = sorted(unique_optional.values(), key=_progressive_origin_key)
    saturated = len(mandatory_items) > 128
    evidence = [] if saturated else list(mandatory_items)
    optional_saturated = False
    if not saturated:
        remaining = 128 - len(evidence)
        optional_saturated = len(optional_items) > remaining
        evidence.extend(optional_items[:remaining])
    evidence.sort(key=_progressive_origin_key)

    if restricted:
        outcome, evidence = "refuse", []
        qualifications.append(_RESTRICTED_REFUSAL)
    elif saturated:
        outcome = "investigate"; qualifications.append(_SATURATION)
    elif mandatory_items or "investigate" in component_outcomes:
        outcome = "investigate"
    elif evidence:
        outcome = "partial" if graph_fallback or optional_saturated or "partial" in component_outcomes else "answer"
    else:
        outcome, evidence = "refuse", []
    qualifications = _qualifications(qualifications)
    reason = "ok" if outcome in {"answer", "partial"} else (
        "freshness_investigation_required" if outcome == "investigate" else "workspace_binding_drift"
    )
    consulted = sorted(item["workspace_id"] for item in selected_definitions)
    core = {
        "schema_version": WORKSPACE_QUERY_READBACK_VERSIONS[1],
        "query_id": "query-" + canonical_digest({"prompt": prompt, "registry": generation["registry_digest"], "primary_workspace_id": primary_workspace_id}).split(":", 1)[1][:24],
        "prompt_digest": canonical_digest(prompt), "registry_id": generation["registry_id"],
        "registry_digest": generation["registry_digest"], "primary_workspace_id": primary_workspace_id,
        "consulted_workspace_ids": consulted, "outcome": outcome, "reason_code": reason,
        "evidence": evidence, "qualifications": qualifications,
        "readback_digest": "sha256:" + "0" * 64,
        **{field: False for field in AUTHORITY_FIELDS},
    }
    core["readback_digest"] = canonical_digest({key: value for key, value in core.items() if key != "readback_digest"})
    try:
        return validate_workspace_query_readback(
            core, generation=generation, selected_workspace_ids=set(consulted),
            graph_manifests=[value[0] for value in graphs.values()],
            document_generations=list(documents.values()),
        )
    except ContractError as exc:
        raise WorkspaceQueryError("workspace query readback is invalid") from exc


def query_workspace(snapshot: WorkspaceQuerySnapshot, primary_workspace_id: str,
                    prompt: str) -> dict:
    """Query one immutable graph-only or progressive workspace snapshot."""

    if type(snapshot) is not WorkspaceQuerySnapshot:
        raise WorkspaceQueryError("workspace query snapshot is invalid")
    _validate_prompt(prompt)
    generation, _graphs, documents = snapshot._open_progressive()
    if not documents and all(
        item["schema_version"] == "ao.lore.workspace-definition.v0.1"
        for item in generation["workspaces"]
    ):
        return _query_graph_only_legacy(snapshot, primary_workspace_id, prompt)
    return _query_progressive(snapshot, primary_workspace_id, prompt)
