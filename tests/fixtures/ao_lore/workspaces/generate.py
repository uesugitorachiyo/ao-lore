#!/usr/bin/env python3
"""Generate deterministic, synthetic workspace rehearsal material."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = HERE / "fixture-spec.json"
REPOSITORY = HERE.parents[3]
SOURCE_ROOT = REPOSITORY / "src"
FIXED_CAMPAIGN = REPOSITORY / ".ao-lore" / "workspace-rehearsal"
REJECTION = "workspace fixture rejected\n"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_graph import (
    build_evidence_graph,
    claim_record,
    operational_question_record,
    relationship_edge_record,
)
from ao_lore.evidence_graph_contracts import AUTHORITY_FIELDS as GRAPH_AUTHORITY_FIELDS
from ao_lore.evidence_graph_contracts import canonical_evidence_digest
from ao_lore.workspace_contracts import AUTHORITY_FIELDS, WORKSPACE_SCHEMA_VERSIONS


def _bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8") + b"\n"
    )


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _bind(value: dict, field: str) -> dict:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def _load_spec() -> dict:
    value = json.loads(SPEC.read_text(encoding="utf-8"))
    if SPEC.read_bytes() != _bytes(value):
        raise ValueError("fixture spec is not canonical")
    if value.get("schema_version") != "ao.lore.workspace-rehearsal-spec.v0.1":
        raise ValueError("fixture spec version differs")
    return value


def _graph(item: dict, index: int) -> dict:
    workspace_id = item["workspace_id"]
    source_id = f"source-{index:02d}"
    source_digest = "sha256:" + str(index) * 64
    workflow_id = f"workflow-{index:02d}"
    workflow_digest = canonical_evidence_digest(workflow_id, "root-workflow")
    question = operational_question_record(
        prompt="How should synthetic records preserve moisture sequences?",
        expected_outcome="partial",
        required_authority_roles=["technical_guidance"],
        required_evidence_ids=[],
        forbidden_evidence_ids=[],
        qualifications=["Synthetic rehearsal evidence only."],
    )
    claim = claim_record(
        source_id=source_id,
        source_digest=source_digest,
        authority_role="technical_guidance",
        excerpt=item["excerpt"],
        citation_anchor=f"fixture-anchor-{index:02d}",
        operational_question_ids=[question["question_id"]],
        subject_terms=["moisture", "records", f"sequence-{index:02d}"],
        semantic_reason_codes=["topic_match", "citation_supported"],
    )
    edges = [
        relationship_edge_record(
            edge_type="provides_remediation_for",
            source_kind="claim",
            source_id=claim["claim_id"],
            source_evidence_digest=claim["excerpt_digest"],
            target_kind="workflow",
            target_id=workflow_id,
            target_evidence_digest=workflow_digest,
            supporting_excerpt_digest=claim["excerpt_digest"],
            reason_code="technical_remediation",
            qualification="Synthetic sequence only.",
        ),
        relationship_edge_record(
            edge_type="cites",
            source_kind="source",
            source_id=source_id,
            source_evidence_digest=source_digest,
            target_kind="claim",
            target_id=claim["claim_id"],
            target_evidence_digest=claim["excerpt_digest"],
            supporting_excerpt_digest=claim["excerpt_digest"],
            reason_code="source_citation",
        ),
    ]
    source = {
        "source_id": source_id,
        "source_digest": source_digest,
        # Ephemeral validation material only; this value is never written by
        # the fixture generator. The reserved host cannot resolve online.
        "canonical_locator": "https" + ":/" + "/fixture.invalid/" + f"source-{index:02d}",
        "retrieved_at": "2026-08-13T12:00:00Z",
        "publisher": "Synthetic Publisher",
        "jurisdiction": "Synthetic Scope",
        "authority_role": "technical_guidance",
        "version": "fixture-01",
        "effective_date": "2026-08-13",
        "operational_question_ids": [question["question_id"]],
        "relationship_edge_ids": sorted(edge["edge_id"] for edge in edges),
        "status": "current",
        "media_type": "text/plain",
        "retained_artifact_digests": [source_digest],
    }
    registry = _bind({
        "schema_version": "ao.lore.evidence-source-registry.v0.1",
        "registry_id": f"source-registry-{index:02d}",
        "root_workflow_id": workflow_id,
        "sources": [source],
        "registry_digest": "sha256:" + "0" * 64,
        **{field: False for field in GRAPH_AUTHORITY_FIELDS},
    }, "registry_digest")
    question["required_evidence_ids"] = [claim["claim_id"]]
    question = _bind({key: value for key, value in question.items() if key != "question_digest"}, "question_digest")
    source["operational_question_ids"] = [question["question_id"]]
    registry["registry_digest"] = canonical_digest({key: value for key, value in registry.items() if key != "registry_digest"})
    claim["operational_question_ids"] = [question["question_id"]]
    return build_evidence_graph(
        registry, [claim], edges, [question],
        root_workflow_digest=workflow_digest,
        as_of_date="2026-08-13",
    )


def _definition(item: dict, graph: dict) -> dict:
    return _bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[0],
        "workspace_id": item["workspace_id"],
        "workspace_version": 1,
        "workspace_type": item["type"],
        "domain": "synthetic-records",
        "jurisdiction": "synthetic-scope",
        "lifecycle_status": "active",
        "root_workflow_id": graph["root_workflow_id"],
        "root_workflow_digest": graph["root_workflow_digest"],
        "source_registry_id": f"sources-{item['workspace_id']}",
        "source_registry_digest": graph["source_registry_digest"],
        "graph_id": graph["graph_id"],
        "graph_digest": graph["graph_digest"],
        "freshness_policy_id": None,
        "freshness_policy_digest": None,
        "freshness_summary_status": "not_observed",
        "freshness_summary_id": None,
        "freshness_summary_digest": None,
        "reference_workspace_ids": list(item["references"]),
        "definition_digest": "sha256:" + "0" * 64,
        **{field: False for field in AUTHORITY_FIELDS},
    }, "definition_digest")


def material() -> dict:
    """Build ephemeral graph material that is never part of fixture output."""
    spec = _load_spec()
    graphs = {}
    definitions = []
    for index, item in enumerate(spec["workspaces"], 1):
        graph = _graph(item, index)
        graphs[item["workspace_id"]] = graph
        definitions.append(_definition(item, graph))
    definitions.sort(key=lambda item: item["workspace_id"])
    return {
        "schema_version": "ao.lore.workspace-rehearsal-bundle.v0.1",
        "definitions": definitions,
        "graphs": {key: graphs[key] for key in sorted(graphs)},
    }


def documents() -> dict[str, bytes]:
    spec = _load_spec()
    files = {
        "fixture-spec.json": SPEC.read_bytes(),
    }
    manifest = {
        "schema_version": "ao.lore.workspace-rehearsal-fixture.v0.1",
        "workspace_ids": sorted(item["workspace_id"] for item in spec["workspaces"]),
        "fixture_digest": canonical_digest([
            {"name": name, "digest": _digest(body)} for name, body in sorted(files.items())
        ]),
        "fixture_file_count": 2,
        "network_used": False,
        "customer_data": False,
        "canonical_repository_state": False,
        "authority_advanced": False,
    }
    files["fixture-manifest.json"] = _bytes(manifest)
    return files


def _validate_out(path: Path) -> Path:
    out = Path(os.path.abspath(path))
    campaign = FIXED_CAMPAIGN.resolve()
    if out == campaign or campaign not in out.parents:
        raise ValueError("fixture output is not campaign-owned")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        out = _validate_out(args.out)
        expected = documents()
        if args.check:
            if not out.is_dir():
                return 1
            actual = {
                path.relative_to(out).as_posix()
                for path in out.iterdir() if path.is_file()
            }
            if actual != set(expected):
                return 1
            return 0 if all((out / name).read_bytes() == body for name, body in expected.items()) else 1
        if out.exists():
            return 1
        out.mkdir(parents=True)
        for name, body in expected.items():
            (out / name).write_bytes(body)
        return 0
    except (OSError, ValueError, KeyError, TypeError):
        sys.stderr.write(REJECTION)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
