"""Deterministic construction and immutable publication of evidence graphs."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from .benchmark import canonical_digest
from .evidence_graph_contracts import (
    AUTHORITY_FIELDS,
    ROLE_PRECEDENCE,
    canonical_evidence_digest,
    validate_detached_freshness_summary_against_manifest,
    validate_graph_inspection_against_manifest,
    validate_graph_manifest,
    validate_operational_question,
    validate_relationship_edge,
    validate_source_registry,
)


class EvidenceGraphError(ValueError):
    """The graph is invalid, unsafe, or conflicts with immutable state."""


@dataclass(frozen=True)
class GraphDependencies:
    root: Path
    failpoint: Callable[[str], None] = lambda _: None
    _root_identity: tuple[int, int] | None = None


def _false_authority() -> dict[str, bool]:
    return {field: False for field in AUTHORITY_FIELDS}


def _bind(value: dict, field: str) -> dict:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def claim_record(*, source_id: str, source_digest: str, authority_role: str,
                 excerpt: str, citation_anchor: str,
                 operational_question_ids: list[str], subject_terms: list[str],
                 semantic_reason_codes: list[str]) -> dict:
    excerpt_digest = canonical_evidence_digest(excerpt, "excerpt")
    identity = canonical_evidence_digest({
        "source_id": source_id, "source_digest": source_digest,
        "excerpt_digest": excerpt_digest, "citation_anchor": citation_anchor,
    }, "claim")
    return {
        "claim_id": "claim-" + identity.split(":", 1)[1][:24],
        "source_id": source_id, "source_digest": source_digest,
        "authority_role": authority_role, "excerpt": excerpt,
        "excerpt_digest": excerpt_digest, "citation_anchor": citation_anchor,
        "operational_question_ids": list(operational_question_ids),
        "subject_terms": list(subject_terms),
        "semantic_reason_codes": list(semantic_reason_codes),
    }


def operational_question_record(*, prompt: str, expected_outcome: str,
                                required_authority_roles: list[str],
                                required_evidence_ids: list[str],
                                forbidden_evidence_ids: list[str],
                                qualifications: list[str]) -> dict:
    identity = canonical_evidence_digest(prompt, "operational-question")
    body = {
        "schema_version": "ao.lore.evidence-operational-question.v0.1",
        "question_id": "question-" + identity.split(":", 1)[1][:24],
        "prompt": prompt, "expected_outcome": expected_outcome,
        "required_authority_roles": list(required_authority_roles),
        "required_evidence_ids": list(required_evidence_ids),
        "forbidden_evidence_ids": list(forbidden_evidence_ids),
        "qualifications": list(qualifications), "question_digest": "",
    }
    return _bind(body, "question_digest")


def relationship_edge_record(*, edge_type: str, source_kind: str, source_id: str,
                             source_evidence_digest: str, target_kind: str,
                             target_id: str, target_evidence_digest: str,
                             supporting_excerpt_digest: str, reason_code: str,
                             qualification: str | None = None) -> dict:
    core = {
        "edge_type": edge_type, "source_kind": source_kind,
        "source_id": source_id, "source_evidence_digest": source_evidence_digest,
        "target_kind": target_kind, "target_id": target_id,
        "target_evidence_digest": target_evidence_digest,
        "supporting_excerpt_digest": supporting_excerpt_digest,
        "reason_code": reason_code, "qualification": qualification,
    }
    identity = canonical_evidence_digest(core, "relationship-edge")
    body = {"schema_version": "ao.lore.evidence-relationship-edge.v0.1",
            "edge_id": "edge-" + identity.split(":", 1)[1][:24], **core,
            "edge_digest": ""}
    return _bind(body, "edge_digest")


def _unique(items: Iterable[dict], key: str) -> None:
    values = [item[key] for item in items]
    if len(values) != len(set(values)):
        raise EvidenceGraphError(f"duplicate {key} collision")


def build_evidence_graph(registry: dict, claims: list[dict], edges: list[dict],
                         questions: list[dict], *, root_workflow_digest: str,
                         as_of_date: str) -> dict:
    try:
        registry = validate_source_registry(registry)
        edges = [validate_relationship_edge(item) for item in edges]
        questions = [validate_operational_question(item) for item in questions]
        date.fromisoformat(as_of_date)
    except Exception as exc:
        raise EvidenceGraphError("graph input is invalid") from exc
    if not (1 <= len(claims) <= 512 and 1 <= len(edges) <= 2048 and 1 <= len(questions) <= 32):
        raise EvidenceGraphError("graph budget exceeded")
    _unique(claims, "claim_id"); _unique(edges, "edge_id"); _unique(questions, "question_id")
    sources = sorted(registry["sources"], key=lambda x: x["source_id"])
    normalized_registry = dict(registry)
    normalized_registry["sources"] = sources
    normalized_registry["registry_digest"] = canonical_digest(
        {key: item for key, item in normalized_registry.items() if key != "registry_digest"}
    )
    claims = sorted(claims, key=lambda x: x["claim_id"])
    edges = sorted(edges, key=lambda x: x["edge_id"])
    questions = sorted(questions, key=lambda x: x["question_id"])
    source_by = {x["source_id"]: x for x in sources}
    claim_by = {x["claim_id"]: x for x in claims}
    question_ids = {x["question_id"] for x in questions}
    endpoints = {
        "source": {x["source_id"]: x["source_digest"] for x in sources},
        "claim": {x["claim_id"]: x["excerpt_digest"] for x in claims},
        "workflow": {registry["root_workflow_id"]: root_workflow_digest},
    }
    for source in sources:
        if source["effective_date"] and source["effective_date"] > as_of_date:
            raise EvidenceGraphError("future effective date")
        if not set(source["operational_question_ids"]).issubset(question_ids):
            raise EvidenceGraphError("source question binding differs")
    for claim in claims:
        source = source_by.get(claim["source_id"])
        if source is None or source["source_digest"] != claim["source_digest"]:
            raise EvidenceGraphError("claim source binding differs")
        if source["authority_role"] != claim["authority_role"]:
            raise EvidenceGraphError("claim authority differs")
        if canonical_evidence_digest(claim["excerpt"], "excerpt") != claim["excerpt_digest"]:
            raise EvidenceGraphError("claim excerpt differs")
        if not set(claim["operational_question_ids"]).issubset(question_ids):
            raise EvidenceGraphError("claim question binding differs")
    adjacency: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for edge in edges:
        sk, tk = edge["source_kind"], edge["target_kind"]
        if edge["source_id"] not in endpoints[sk] or edge["target_id"] not in endpoints[tk]:
            raise EvidenceGraphError("edge endpoint differs")
        if endpoints[sk][edge["source_id"]] != edge["source_evidence_digest"] or endpoints[tk][edge["target_id"]] != edge["target_evidence_digest"]:
            raise EvidenceGraphError("edge endpoint digest differs")
        if edge["supporting_excerpt_digest"] not in {x["excerpt_digest"] for x in claims}:
            raise EvidenceGraphError("supporting excerpt differs")
        source_role = (source_by[edge["source_id"]]["authority_role"] if sk == "source"
                       else claim_by[edge["source_id"]]["authority_role"] if sk == "claim" else None)
        if edge["edge_type"] == "defines" and source_role != "primary_law":
            raise EvidenceGraphError("authority cannot define law")
        if edge["edge_type"] == "supersedes":
            if sk != "source" or tk != "source":
                raise EvidenceGraphError("unsupported supersession")
            left, right = source_by[edge["source_id"]], source_by[edge["target_id"]]
            if left["authority_role"] != right["authority_role"] or left["publisher"] != right["publisher"] or not left["version"] or not right["version"]:
                raise EvidenceGraphError("unsupported supersession")
        adjacency.setdefault((sk, edge["source_id"]), set()).add((tk, edge["target_id"]))
        adjacency.setdefault((tk, edge["target_id"]), set()).add((sk, edge["source_id"]))
    edge_ids = {x["edge_id"] for x in edges}
    incident_by_source = {source["source_id"]: set() for source in sources}
    for edge in edges:
        related = set()
        if edge["source_kind"] == "source": related.add(edge["source_id"])
        elif edge["source_kind"] == "claim": related.add(claim_by[edge["source_id"]]["source_id"])
        if edge["target_kind"] == "source": related.add(edge["target_id"])
        elif edge["target_kind"] == "claim": related.add(claim_by[edge["target_id"]]["source_id"])
        for source_id in related:
            incident_by_source[source_id].add(edge["edge_id"])
    for source in sources:
        declared = set(source["relationship_edge_ids"])
        if not declared.issubset(edge_ids) or declared != incident_by_source[source["source_id"]]:
            raise EvidenceGraphError("source edge binding differs")
    evidence_ids = set(source_by) | set(claim_by) | edge_ids
    for question in questions:
        required = set(question["required_evidence_ids"])
        forbidden = set(question["forbidden_evidence_ids"])
        if not required.issubset(evidence_ids) or not forbidden.issubset(evidence_ids):
            raise EvidenceGraphError("question evidence binding differs")
    root = ("workflow", registry["root_workflow_id"])
    seen, pending = {root}, [root]
    while pending:
        node = pending.pop()
        for nxt in adjacency.get(node, ()):
            if nxt not in seen: seen.add(nxt); pending.append(nxt)
    if any(("source", x["source_id"]) not in seen for x in sources) or any(("claim", x["claim_id"]) not in seen for x in claims):
        raise EvidenceGraphError("orphan evidence")
    graph_id = "graph-" + canonical_evidence_digest({"registry": normalized_registry["registry_digest"], "root": root_workflow_digest}, "graph-id").split(":", 1)[1][:24]
    graph = {
        "schema_version": "ao.lore.evidence-graph-manifest.v0.1",
        "graph_id": graph_id, "root_workflow_id": registry["root_workflow_id"],
        "root_workflow_digest": root_workflow_digest,
        "source_registry_digest": normalized_registry["registry_digest"], "sources": sources,
        "claims": claims, "edges": edges, "operational_questions": questions,
        "graph_digest": "", **_false_authority(),
    }
    try:
        return validate_graph_manifest(_bind(graph, "graph_digest"))
    except Exception as exc:
        raise EvidenceGraphError("graph manifest is invalid") from exc


def inspect_evidence_graph(
    graph: dict, *, as_of_date: str, freshness_summary: dict | None = None,
) -> dict:
    try:
        graph = validate_graph_manifest(graph); date.fromisoformat(as_of_date)
    except Exception as exc:
        raise EvidenceGraphError("graph inspection input is invalid") from exc
    freshness = None
    if freshness_summary is not None:
        freshness = validate_detached_freshness_summary_against_manifest(
            freshness_summary, graph,
        )
    stale = sum(x["status"] != "current" for x in graph["sources"])
    conflicts = sum(x["edge_type"] == "conflicts_with" for x in graph["edges"])
    freshness_affected = freshness is not None and any(
        item["classification"] != "unchanged" for item in freshness["results"]
    )
    body = {
        "schema_version": "ao.lore.evidence-graph-inspection.v0.1",
        "graph_id": graph["graph_id"], "graph_digest": graph["graph_digest"],
        "result": "investigate" if stale or conflicts or freshness_affected else "pass",
        "source_count": len(graph["sources"]), "claim_count": len(graph["claims"]),
        "edge_count": len(graph["edges"]), "question_count": len(graph["operational_questions"]),
        "orphan_source_count": 0, "orphan_claim_count": 0,
        "conflict_count": conflicts, "stale_source_count": stale,
        "inspection_digest": "", **_false_authority(),
    }
    return validate_graph_inspection_against_manifest(_bind(body, "inspection_digest"), graph,
        expected_orphan_source_count=0, expected_orphan_claim_count=0)


def _bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()


def _safe_root(root: Path, expected_identity: tuple[int, int] | None = None) -> int:
    getattr(root, "_assert_bound", lambda: None)()
    descriptor = None
    try:
        before = os.lstat(root)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise EvidenceGraphError("graph root is unsafe")
        before_identity = before.st_dev, before.st_ino
        if expected_identity is not None and before_identity != expected_identity:
            raise EvidenceGraphError("graph root changed")
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened = os.fstat(descriptor)
        rebound = os.stat(root, follow_symlinks=False)
        opened_identity = opened.st_dev, opened.st_ino
        rebound_identity = rebound.st_dev, rebound.st_ino
        if (not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(rebound.st_mode)
                or opened_identity != before_identity
                or rebound_identity != opened_identity
                or (expected_identity is not None and opened_identity != expected_identity)):
            raise EvidenceGraphError("graph root changed")
        result, descriptor = descriptor, None
        return result
    except EvidenceGraphError:
        raise
    except OSError as exc:
        raise EvidenceGraphError("graph root is unsafe") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read(fd: int, name: str) -> bytes:
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4 * 1024 * 1024:
        raise EvidenceGraphError("graph artifact is unsafe")
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try: body = os.read(child, 4 * 1024 * 1024 + 1)
    finally: os.close(child)
    return body


def _setup(root_fd: int) -> tuple[int, int]:
    fds = []
    for name in ("generations", "staging"):
        try: os.mkdir(name, 0o700, dir_fd=root_fd); os.fsync(root_fd)
        except FileExistsError: pass
        fds.append(os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd))
    return fds[0], fds[1]


def _locked(root_fd: int) -> int:
    fd = os.open("graph.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=root_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size:
        os.close(fd); raise EvidenceGraphError("graph lock is unsafe")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def publish_evidence_graph(graph: dict, deps: GraphDependencies) -> dict:
    try: graph = validate_graph_manifest(graph)
    except Exception as exc: raise EvidenceGraphError("graph is invalid") from exc
    body = _bytes(graph); token = graph["graph_digest"].split(":", 1)[1]; name = token + ".json"
    root_fd = _safe_root(deps.root, deps._root_identity)
    try:
        lock_fd = _locked(root_fd); generations_fd, staging_fd = _setup(root_fd)
        try:
            try:
                existing = _read(generations_fd, name)
                if existing == body: return {"status": "completed", "classification": "no_op", "graph_digest": graph["graph_digest"]}
                raise EvidenceGraphError("graph destination conflict")
            except FileNotFoundError: pass
            stage = token + ".part"
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=staging_fd)
            try: os.write(fd, body); os.fsync(fd)
            finally: os.close(fd)
            deps.failpoint("after_staging")
            try: os.link(stage, name, src_dir_fd=staging_fd, dst_dir_fd=generations_fd, follow_symlinks=False)
            except FileExistsError as exc: raise EvidenceGraphError("graph destination conflict") from exc
            os.fsync(generations_fd); os.unlink(stage, dir_fd=staging_fd); os.fsync(staging_fd)
            return {"status": "completed", "classification": "complete", "graph_digest": graph["graph_digest"]}
        finally:
            os.close(generations_fd); os.close(staging_fd); fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)
    finally:
        os.close(root_fd)
        getattr(deps.root, "_assert_bound", lambda: None)()


def recover_evidence_graph(deps: GraphDependencies) -> list[dict]:
    root_fd = _safe_root(deps.root, deps._root_identity); results = []
    try:
        lock_fd = _locked(root_fd); generations_fd, staging_fd = _setup(root_fd)
        try:
            for name in sorted(os.listdir(staging_fd)):
                if not name.endswith(".part"): continue
                token = name[:-5]; body = _read(staging_fd, name)
                try:
                    value = json.loads(body); graph = validate_graph_manifest(value)
                except Exception:
                    results.append({"classification": "investigate"}); continue
                if graph["graph_digest"].split(":", 1)[1] != token:
                    results.append({"classification": "investigate"}); continue
                destination = token + ".json"
                try:
                    current = _read(generations_fd, destination)
                    if current != body: results.append({"classification": "investigate"}); continue
                except FileNotFoundError:
                    os.link(name, destination, src_dir_fd=staging_fd, dst_dir_fd=generations_fd, follow_symlinks=False); os.fsync(generations_fd)
                os.unlink(name, dir_fd=staging_fd); os.fsync(staging_fd)
                results.append({"classification": "complete", "graph_digest": graph["graph_digest"]})
            return results
        finally:
            os.close(generations_fd); os.close(staging_fd); fcntl.flock(lock_fd, fcntl.LOCK_UN); os.close(lock_fd)
    finally:
        os.close(root_fd)
        getattr(deps.root, "_assert_bound", lambda: None)()
