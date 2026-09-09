#!/usr/bin/env python3
"""Run the fixed offline governed evidence-selection rehearsal."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from unittest import mock
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY / "src"
CAMPAIGN_ROOT = REPOSITORY / ".ao-lore" / "evidence-selection-rehearsal"
OWNER = {
    "schema_version": "ao.lore.evidence-selection-rehearsal-owner.v0.1",
    "campaign_id": "evidence-selection-rehearsal",
}
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ao_lore.evidence_graph import GraphDependencies, publish_evidence_graph
from ao_lore.evidence_selection import (
    EvidenceSelectionDependencies,
    SELECTION_APPLY_FAILPOINTS,
    apply_evidence_selection,
    authorize_evidence_selection,
    inspect_evidence_selection,
    prepare_evidence_selection,
    recover_evidence_selections,
)
from ao_lore.workspace_documents import WorkspaceDocumentDependencies, publish_workspace_documents
from ao_lore.workspace_query import (
    WorkspaceDocumentQuerySnapshot,
    WorkspaceGraphSnapshot,
    WorkspaceQuerySnapshot,
)
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    WorkspaceRegistrySnapshot,
    load_workspace_registry,
    publish_workspace_registry_generation,
    select_workspace,
)
from tests.test_ao_lore_document_evidence_query import generation as document_generation
from tests.test_ao_lore_evidence_candidate import (
    definition,
    document_block_from_snapshot,
    graph_claim_from_snapshot,
)
from tests.test_ao_lore_workspace_query import graph_variant


ACTUAL_HEAD = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=REPOSITORY,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()


def _bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


def _campaign_root() -> Path:
    CAMPAIGN_ROOT.mkdir(parents=True, exist_ok=True)
    owner = CAMPAIGN_ROOT / "campaign-owner.json"
    if owner.exists():
        if owner.read_bytes() != _bytes(OWNER):
            raise ValueError("campaign owner differs")
    else:
        owner.write_bytes(_bytes(OWNER))
    return CAMPAIGN_ROOT


def _fixture_snapshot(root: Path, *, graph_enabled: bool) -> WorkspaceQuerySnapshot:
    graph = None if not graph_enabled else graph_variant("graph-workspace-a")
    publish_workspace_registry_generation(
        (definition("workspace-a", None, graph),),
        WorkspaceRegistryDependencies(root),
    )
    ir = document_generation()["documents"][0]["document_ir"]
    publish_workspace_documents(
        WorkspaceDocumentDependencies(root),
        "workspace-a",
        (ir,),
    )
    if graph is not None:
        graph_root = root / "workspaces" / "state" / "workspace-a" / "graph"
        graph_root.mkdir(parents=True, exist_ok=True)
        publish_evidence_graph(graph, GraphDependencies(graph_root))
    registry = load_workspace_registry(WorkspaceRegistryDependencies(root))
    selection = select_workspace(registry, "workspace-a")
    from ao_lore.workspace_documents import load_workspace_documents

    current_generation = load_workspace_documents(
        WorkspaceDocumentDependencies(root), "workspace-a"
    ).generation
    graphs = () if graph is None else (WorkspaceGraphSnapshot("workspace-a", graph),)
    return WorkspaceQuerySnapshot(
        WorkspaceRegistrySnapshot(registry.generations, registry.workspaces),
        selection,
        graphs,
        documents=(WorkspaceDocumentQuerySnapshot("workspace-a", current_generation),),
    )


def _selection(root: Path, candidate_root: Path, *, graph_enabled: bool):
    snapshot = _fixture_snapshot(root, graph_enabled=graph_enabled)
    deps = EvidenceSelectionDependencies(
        runtime_root=root,
        candidate_root=candidate_root,
        source_head=lambda: ACTUAL_HEAD,
        now=lambda: "2026-08-14T12:00:00Z",
    )
    evidence = [document_block_from_snapshot(snapshot)["evidence_id"]]
    if graph_enabled:
        evidence.append(graph_claim_from_snapshot(snapshot)["evidence_id"])
    proposal = prepare_evidence_selection(
        snapshot,
        "workspace-a",
        tuple(evidence),
        now="2026-08-14T12:00:00Z",
    )
    authorization = authorize_evidence_selection(deps, proposal, "fixture-operator")
    return deps, proposal, authorization


def _report_once() -> dict[str, object]:
    candidate_before = review_before = review_after = brain_before = brain_after = candidate_after = 0
    network_used = False
    provider_used = False
    scenarios: dict[str, str] = {}

    def deny_network(*_args, **_kwargs):
        nonlocal network_used
        network_used = True
        raise AssertionError("network attempted")

    def deny_provider(*_args, **_kwargs):
        nonlocal provider_used
        provider_used = True
        raise AssertionError("provider attempted")

    for graph_enabled, label in ((False, "document_only"), (True, "combined")):
        runtime_dir = Path(tempfile.mkdtemp(dir=REPOSITORY / ".ao-lore"))
        candidate_root = Path(tempfile.mkdtemp(dir=REPOSITORY / "working" / "candidates"))
        try:
            deps, proposal, authorization = _selection(runtime_dir, candidate_root, graph_enabled=graph_enabled)
            candidate_before = len(list(candidate_root.glob("candidate-*")))
            review_before = sum(
                len(list((path / "reviews").glob("*.json")))
                for path in candidate_root.glob("candidate-*")
                if (path / "reviews").is_dir()
            )
            brain_before = len(list((runtime_dir / "brain").rglob("*"))) if (runtime_dir / "brain").exists() else 0
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch("socket.create_connection", side_effect=deny_network)
                )
                stack.enter_context(
                    mock.patch("urllib.request.build_opener", side_effect=deny_provider)
                )
                applied = apply_evidence_selection(deps, proposal["proposal_id"], authorization["authorization_id"])
                inspection = inspect_evidence_selection(deps, proposal["proposal_id"])
            if inspection["status"] != "committed" or applied["candidate_id"] != proposal["candidate"]["candidate_id"]:
                raise ValueError("selection rehearsal binding differs")
            candidate_after = len(list(candidate_root.glob("candidate-*")))
            review_after = sum(
                len(list((path / "reviews").glob("*.json")))
                for path in candidate_root.glob("candidate-*")
                if (path / "reviews").is_dir()
            )
            brain_after = len(list((runtime_dir / "brain").rglob("*"))) if (runtime_dir / "brain").exists() else 0
            scenarios[label] = "passed"
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            shutil.rmtree(candidate_root, ignore_errors=True)
    for failpoint in SELECTION_APPLY_FAILPOINTS:
        runtime_dir = Path(tempfile.mkdtemp(dir=REPOSITORY / ".ao-lore"))
        candidate_root = Path(tempfile.mkdtemp(dir=REPOSITORY / "working" / "candidates"))
        try:
            def stop(name):
                if name == failpoint:
                    raise RuntimeError(name)

            deps, proposal, authorization = _selection(runtime_dir, candidate_root, graph_enabled=True)
            deps = EvidenceSelectionDependencies(
                runtime_root=runtime_dir,
                candidate_root=candidate_root,
                source_head=lambda: ACTUAL_HEAD,
                now=lambda: "2026-08-14T12:00:00Z",
                failpoint=stop,
            )
            try:
                apply_evidence_selection(deps, proposal["proposal_id"], authorization["authorization_id"])
            except RuntimeError:
                pass
            resumed = EvidenceSelectionDependencies(
                runtime_root=runtime_dir,
                candidate_root=candidate_root,
                source_head=lambda: ACTUAL_HEAD,
                now=lambda: "2026-08-14T12:00:00Z",
            )
            result = recover_evidence_selections(resumed, "workspace-a")
            if result["status"] not in {"recovered", "no_op"}:
                raise ValueError("selection rehearsal recovery differs")
            scenarios[failpoint] = "passed"
        finally:
            shutil.rmtree(runtime_dir, ignore_errors=True)
            shutil.rmtree(candidate_root, ignore_errors=True)
    return {
        "schema_version": "ao.lore.evidence-selection-rehearsal.v0.1",
        "scenario_statuses": scenarios,
        "scenario_count": len(scenarios),
        "candidate_count_delta": candidate_after - candidate_before,
        "review_inventory_unchanged": review_before == review_after,
        "brain_inventory_unchanged": brain_before == brain_after,
        "candidate_inventory_before": candidate_before,
        "candidate_inventory_after": candidate_after,
        "network_used": network_used,
        "provider_used": provider_used,
        "canonical_authority": False,
    }


def _report() -> dict[str, object]:
    _campaign_root()
    first = _report_once()
    second = _report_once()
    byte_identical = _bytes(first) == _bytes(second)
    report = dict(first)
    report["byte_identical_check"] = byte_identical
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rehearse-evidence-selection")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    path = _campaign_root() / "rehearsal-evidence.json"
    report = _report()
    body = _bytes(report)
    if args.check:
        if not path.exists():
            return 2
        existing = path.read_bytes()
        if existing != body:
            return 2
        sys.stdout.write(existing.decode("utf-8"))
        return 0
    path.write_bytes(body)
    sys.stdout.write(body.decode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
