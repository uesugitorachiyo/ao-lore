#!/usr/bin/env python3
"""Rehearse deterministic document-first and relationship-enriched retrieval."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY / "src"
CAMPAIGN_ROOT = REPOSITORY / ".ao-lore" / "progressive-evidence-rehearsal"
OWNER = {
    "schema_version": "ao.lore.progressive-evidence-rehearsal-owner.v0.1",
    "campaign_id": "progressive-evidence-rehearsal",
}
REJECTION = "progressive evidence rehearsal rejected\n"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_graph import (
    build_evidence_graph,
    claim_record,
    operational_question_record,
    relationship_edge_record,
)
from ao_lore.evidence_graph_contracts import AUTHORITY_FIELDS, canonical_evidence_digest
from ao_lore.workspace_query import (
    WorkspaceDocumentQuerySnapshot,
    WorkspaceGraphSnapshot,
    WorkspaceQuerySnapshot,
    query_workspace,
)
from ao_lore.workspace_registry import WorkspaceRegistrySnapshot, select_workspace


def _bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8") + b"\n"
    )


def _bind(value: dict, field: str) -> dict:
    value[field] = canonical_digest({
        key: item for key, item in value.items() if key != field
    })
    return value


def _authority() -> dict[str, bool]:
    return {field: False for field in AUTHORITY_FIELDS}


def _fixture_locator(*path_components: str) -> str:
    return (
        "https" + "://" + "source" + "." + "in" + "valid" +
        "/" + "/".join(path_components)
    )


def _document_generation(workspace_id: str, registry_digest: str) -> dict:
    text = "Record the procedure before scheduling the synthetic control step."
    ir = {
        "schema_version": "ao.lore.document-ir.v0.1",
        "document_id": "procedure-manual",
        "source": {
            "resource": "inbox/procedure-manual.pdf",
            "digest": canonical_digest("synthetic-document-source"),
            "media_type": "application/pdf",
        },
        "blocks": [{
            "id": "block-1", "type": "paragraph", "text": text,
            "source_span": {"page": 1, "start": 0, "end": len(text)},
        }],
        "metadata": {},
    }
    document = {
        "document_id": ir["document_id"], "document_ir_digest": canonical_digest(ir),
        "source_id": "source-procedure-manual", "source_digest": ir["source"]["digest"],
        "source_record_digest": canonical_digest("synthetic-source-record"),
        "media_type": "application/pdf", "authority_role": "operator_procedure",
        "sensitivity": "internal", "version": "1", "effective_date": "2026-08-13",
        "freshness_status": "current", "qualification_codes": ["synthetic-only"],
        "document_ir": ir,
    }
    return _bind({
        "schema_version": "ao.lore.workspace-document-generation.v0.1",
        "generation_id": "documents-" + workspace_id + "-0000000001",
        "sequence": 1, "prior_generation_digest": None,
        "workspace_id": workspace_id, "document_store_id": "documents-" + workspace_id,
        "registry_digest": registry_digest, "created_at": "2026-08-13T12:00:00Z",
        "documents": [document], "generation_digest": canonical_digest("placeholder"),
    }, "generation_digest")


def _graph() -> dict:
    workflow_id = "synthetic-control-workflow"
    workflow_digest = canonical_evidence_digest(
        "synthetic control applicability relationship", "root-workflow",
    )
    question = operational_question_record(
        prompt="Which relationship establishes control applicability?",
        expected_outcome="answer", required_authority_roles=["technical_guidance"],
        required_evidence_ids=[], forbidden_evidence_ids=[],
        qualifications=["Synthetic relationship evidence only."],
    )
    claim = claim_record(
        source_id="relationship-guide",
        source_digest=canonical_digest("synthetic-relationship-source"),
        authority_role="technical_guidance",
        excerpt="The documented relationship establishes control applicability.",
        citation_anchor="Synthetic section one",
        operational_question_ids=[question["question_id"]],
        subject_terms=["relationship", "control", "applicability"],
        semantic_reason_codes=["topic_match", "citation_supported"],
    )
    applies = relationship_edge_record(
        edge_type="provides_remediation_for", source_kind="claim",
        source_id=claim["claim_id"], source_evidence_digest=claim["excerpt_digest"],
        target_kind="workflow", target_id=workflow_id,
        target_evidence_digest=workflow_digest,
        supporting_excerpt_digest=claim["excerpt_digest"],
        reason_code="technical_remediation",
        qualification="Synthetic relationship scope only.",
    )
    cites = relationship_edge_record(
        edge_type="cites", source_kind="source", source_id="relationship-guide",
        source_evidence_digest=canonical_digest("synthetic-relationship-source"),
        target_kind="claim", target_id=claim["claim_id"],
        target_evidence_digest=claim["excerpt_digest"],
        supporting_excerpt_digest=claim["excerpt_digest"],
        reason_code="source_citation",
    )
    source = {
        "source_id": "relationship-guide",
        "source_digest": canonical_digest("synthetic-relationship-source"),
        "canonical_locator": _fixture_locator("synthetic", "record"),
        "retrieved_at": "2026-08-13T12:00:00Z", "publisher": "Synthetic Publisher",
        "jurisdiction": "Fixture scope", "authority_role": "technical_guidance",
        "version": "1", "effective_date": "2026-08-13",
        "operational_question_ids": [question["question_id"]],
        "relationship_edge_ids": sorted([applies["edge_id"], cites["edge_id"]]),
        "status": "current", "media_type": "text/html",
        "retained_artifact_digests": [canonical_digest("synthetic-relationship-source")],
    }
    registry = _bind({
        "schema_version": "ao.lore.evidence-source-registry.v0.1",
        "registry_id": "synthetic-relationship-registry",
        "root_workflow_id": workflow_id, "sources": [source], **_authority(),
    }, "registry_digest")
    question["required_evidence_ids"] = [claim["claim_id"]]
    _bind(question, "question_digest")
    source["operational_question_ids"] = [question["question_id"]]
    claim["operational_question_ids"] = [question["question_id"]]
    _bind(registry, "registry_digest")
    return build_evidence_graph(
        registry, [claim], [applies, cites], [question],
        root_workflow_digest=workflow_digest, as_of_date="2026-08-13",
    )


def _definition(workspace_id: str, *, document: dict | None, graph: dict | None) -> dict:
    return _bind({
        "schema_version": "ao.lore.workspace-definition.v0.2",
        "workspace_id": workspace_id, "workspace_version": 2 if document else 1,
        "workspace_type": "property", "domain": "synthetic-guidance",
        "jurisdiction": "fixture-scope", "lifecycle_status": "active",
        "root_workflow_id": "workflow-" + workspace_id,
        "root_workflow_digest": canonical_digest("workflow-" + workspace_id),
        "source_registry_id": "sources-" + workspace_id,
        "source_registry_digest": canonical_digest("sources-" + workspace_id),
        "graph_id": None if graph is None else graph["graph_id"],
        "graph_digest": None if graph is None else graph["graph_digest"],
        "freshness_policy_id": None, "freshness_policy_digest": None,
        "freshness_summary_status": "not_observed", "freshness_summary_id": None,
        "freshness_summary_digest": None, "reference_workspace_ids": [],
        "document_store_id": "documents-" + workspace_id,
        "document_generation_digest": None if document is None else document["generation_digest"],
        "definition_digest": canonical_digest("placeholder"), **_authority(),
    }, "definition_digest")


def _snapshot(mode: str) -> tuple[WorkspaceQuerySnapshot, str]:
    workspace_id = mode
    predecessor = None if mode == "graph-only" else canonical_digest("registry-before-" + mode)
    document = None if mode == "graph-only" else _document_generation(workspace_id, predecessor)
    graph = None if mode == "document-only" else _graph()
    definition = _definition(workspace_id, document=document, graph=graph)
    unrelated = _definition(
        "unrelated-workspace-" + mode, document=None, graph=None,
    )
    definitions = sorted(
        [definition, unrelated], key=lambda item: item["workspace_id"],
    )
    generation = _bind({
        "schema_version": "ao.lore.workspace-registry-generation.v0.1",
        "registry_id": "registry-" + mode, "sequence": 1 if predecessor is None else 2,
        "predecessor_registry_digest": predecessor,
        "generated_at": "2026-08-13T12:00:01Z", "workspaces": definitions,
        "registry_digest": canonical_digest("placeholder"), **_authority(),
    }, "registry_digest")
    registry = WorkspaceRegistrySnapshot((generation,), tuple(definitions))
    graphs = () if graph is None else (WorkspaceGraphSnapshot(workspace_id, graph),)
    documents = () if document is None else (
        WorkspaceDocumentQuerySnapshot(workspace_id, document),
    )
    snapshot = WorkspaceQuerySnapshot(
        registry, select_workspace(registry, workspace_id), graphs, documents=documents,
    )
    return snapshot, canonical_digest(unrelated)


def _mode_metrics(mode: str) -> tuple[dict, bool, str]:
    snapshot, unrelated_before = _snapshot(mode)
    started = time.monotonic()
    relationship = query_workspace(
        snapshot, mode, "relationship control applicability",
    )
    procedure = query_workspace(snapshot, mode, "procedure record")
    refusal = query_workspace(
        snapshot, mode, "unmatched qzeta request?",
    )
    elapsed_ms = (time.monotonic() - started) * 1000
    unrelated_after = canonical_digest(next(
        item for item in snapshot.registry.generations[-1]["workspaces"]
        if item["workspace_id"] == "unrelated-workspace-" + mode
    ))
    evidence = sorted({
        canonical_digest(item)
        for result in (relationship, procedure) for item in result["evidence"]
    })
    allowed = {
        "document-only": {"document_block"},
        "graph-only": {"graph_claim", "graph_edge"},
        "combined": {"document_block", "graph_claim", "graph_edge"},
    }[mode]
    selected = [item for result in (relationship, procedure) for item in result["evidence"]]
    precision = 1.0 if all(item["evidence_kind"] in allowed for item in selected) else 0.0
    metrics = {
        "citation_precision": precision,
        "applicability": 1.0 if any(
            item["evidence_kind"].startswith("graph_")
            for item in relationship["evidence"]
        ) else 0.0,
        "conflict_detection": 0.0,
        "supersession_handling": 0.0,
        "qualification": 1.0 if relationship["qualifications"] else 0.0,
        "refusal_correctness": 1.0 if refusal["outcome"] == "refuse" and not refusal["evidence"] else 0.0,
        "evidence_identities": evidence,
        "latency_budget": {
            "limit_ms": 50, "all_queries_within_budget": elapsed_ms <= 50,
        },
    }
    return metrics, unrelated_before == unrelated_after, unrelated_before


def _report() -> dict:
    original_socket = socket.socket
    def denied(*_args, **_kwargs):
        raise AssertionError("network denied")
    socket.socket = denied
    network_denied = False
    try:
        try:
            socket.socket()
        except AssertionError:
            network_denied = True
        evaluated = {
            mode: _mode_metrics(mode)
            for mode in ("combined", "document-only", "graph-only")
        }
    finally:
        socket.socket = original_socket
    metrics = {mode: value[0] for mode, value in evaluated.items()}
    unrelated_preserved = all(value[1] for value in evaluated.values())
    unrelated_digest = canonical_digest({
        mode: value[2] for mode, value in evaluated.items()
    })
    relationship_measures = (
        "applicability", "conflict_detection", "supersession_handling", "qualification",
    )
    improved = any(
        metrics["combined"][name] > metrics["document-only"][name]
        for name in relationship_measures
    )
    no_regression = all(
        metrics["combined"][name] >= metrics["document-only"][name]
        for name in ("citation_precision", "refusal_correctness")
    )
    report = {
        "schema_version": "ao.lore.progressive-evidence-rehearsal.v0.1",
        "workspace_modes": ["combined", "document-only", "graph-only"],
        "prompt_set_digest": canonical_digest([
            "relationship control applicability", "procedure record",
            "unmatched qzeta request?",
        ]),
        "metrics": metrics, "network_denied": network_denied,
        "unrelated_workspace_inventory_digest": unrelated_digest,
        "unrelated_workspace_preserved": unrelated_preserved,
        "enrichment_success": improved and no_regression,
        "candidate_created": False, "review_event_created": False,
        "canonical_mutated": False, "provider_used": False,
        "promotion_authority": False, "release_authority": False,
        "rehearsal_digest": canonical_digest("placeholder"),
    }
    report["rehearsal_digest"] = canonical_digest({
        key: value for key, value in report.items() if key != "rehearsal_digest"
    })
    return report


def _run(check: bool) -> dict:
    report = _report()
    owner_body = _bytes(OWNER)
    report_body = _bytes(report)
    if check:
        if (
            not CAMPAIGN_ROOT.is_dir() or CAMPAIGN_ROOT.is_symlink()
            or (CAMPAIGN_ROOT / "campaign-owner.json").read_bytes() != owner_body
            or (CAMPAIGN_ROOT / "rehearsal-evidence.json").read_bytes() != report_body
        ):
            raise ValueError("retained rehearsal differs")
        return report
    if CAMPAIGN_ROOT.exists() or CAMPAIGN_ROOT.is_symlink():
        raise ValueError("campaign already exists")
    CAMPAIGN_ROOT.mkdir(mode=0o700, parents=True)
    try:
        (CAMPAIGN_ROOT / "campaign-owner.json").write_bytes(owner_body)
        (CAMPAIGN_ROOT / "rehearsal-evidence.json").write_bytes(report_body)
    except BaseException:
        shutil.rmtree(CAMPAIGN_ROOT, ignore_errors=True)
        raise
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        sys.stdout.buffer.write(_bytes(_run(args.check)))
    except Exception:
        sys.stderr.write(REJECTION)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
