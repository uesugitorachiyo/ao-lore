#!/usr/bin/env python3
"""Run an offline public-safe evidence-freshness rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ao_lore.__main__ import main as cli_main
from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_acquisition import (
    AcquisitionDependencies,
    AcquisitionLimits,
    AcquisitionSpec,
    HTTPResponse,
    acquire_official_evidence,
)
from ao_lore.evidence_freshness import (
    FreshnessDependencies,
    compare_freshness_observation,
    recover_freshness_transactions,
    refresh_official_evidence,
    summarize_freshness,
)
from ao_lore.evidence_query import query_evidence_graph
from ao_lore.evidence_graph_contracts import AUTHORITY_FIELDS


GENERATOR = REPOSITORY / "tests" / "fixtures" / "ao_lore" / "evidence_freshness" / "generate.py"
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REJECTION = "evidence freshness rehearsal rejected\n"
CLI_REJECTION = "ao-lore: workspace operation rejected\n"
EXPECTED_SCENARIOS = (
    "unchanged",
    "updated",
    "superseded",
    "unavailable",
    "investigate",
    "crash_retry",
    "query_downgrade",
    "redaction",
    "protected_inventory",
)
LIMITS = AcquisitionLimits(
    max_specs=3,
    max_redirects=2,
    max_response_bytes=1024,
    max_header_bytes=128,
    connect_timeout_seconds=1,
    per_spec_timeout_seconds=3,
    total_timeout_seconds=10,
    max_retained_files=8,
    max_directory_levels=4,
    max_total_retained_bytes=4096,
)


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def fetch(self, locator, **limits):
        self.requests.append((locator, limits))
        if not self.responses:
            raise AssertionError("unexpected network request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value):
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _bytes_digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _write(path: Path, value) -> None:
    path.write_bytes(_canonical(value) + b"\n")


def _authority():
    return {field: False for field in AUTHORITY_FIELDS}


def _fixture_locator(*path_components: str) -> str:
    host = "source"
    suffix = ".".join(("example", "com"))
    return "https" + "://" + host + "." + suffix + "/" + "/".join(path_components)


def _bind(value: dict, field: str) -> dict:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def _require_digest(value: str) -> str:
    if not DIGEST_RE.fullmatch(value):
        raise ValueError("inventory digest is invalid")
    return value


def _protected_roots() -> list[tuple[str, Path]]:
    roots = [
        ("brain", REPOSITORY / "brain"),
        ("candidates", REPOSITORY / "working" / "candidates"),
        ("reviews", REPOSITORY / "working" / "reviews"),
    ]
    sources_root = REPOSITORY / "sources"
    if sources_root.is_dir():
        for path in sorted(sources_root.iterdir(), key=lambda item: item.name):
            if path.is_dir():
                roots.append((f"source_{path.name}", path))
    return roots


def _inventory_digest(root: Path) -> str:
    if not root.exists():
        return canonical_digest([])
    rows = []
    for path in sorted(root.rglob("*")):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or (
            not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode)
        ):
            raise ValueError("protected inventory is unsafe")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise ValueError("protected inventory is unsafe")
            rows.append(
                [
                    path.relative_to(root).as_posix(),
                    "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                ]
            )
    return canonical_digest(rows)


def _protected_inventory() -> dict[str, str]:
    return {
        label: _inventory_digest(path)
        for label, path in _protected_roots()
    }


def _run_command(arguments: list[str], *, public_argv: list[str] | None = None) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    completed = subprocess.run(
        arguments,
        cwd=REPOSITORY,
        capture_output=True,
        text=True,
        check=False,
    )
    entry = {
        "argv": public_argv if public_argv is not None else arguments,
        "exit_code": completed.returncode,
    }
    return completed, entry


def _validate_root(root: Path) -> None:
    repository = REPOSITORY.resolve()
    runtime = (repository / ".ao-lore").resolve()
    forbidden = {
        repository,
        (repository / "brain").resolve(),
        (repository / "working" / "candidates").resolve(),
        (repository / "sources").resolve(),
        runtime,
    }
    if root in forbidden or runtime not in root.parents:
        raise ValueError("unowned root")


def _graph() -> dict:
    excerpt = "Official retained guidance."
    excerpt_digest = canonical_digest({
        "domain": "ao.lore.evidence-graph.excerpt.v0.1", "value": excerpt,
    })
    source = {
        "source_id": "source-one",
        "source_digest": "sha256:" + "a" * 64,
        "canonical_locator": _fixture_locator("guide"),
        "retrieved_at": "2026-08-12T18:00:00Z",
        "publisher": "Synthetic Register",
        "jurisdiction": "Synthetic scope",
        "authority_role": "technical_guidance",
        "version": "2025 edition",
        "effective_date": "2025-01-01",
        "operational_question_ids": ["question-one"],
        "relationship_edge_ids": ["edge-one", "edge-source-one"],
        "status": "current",
        "media_type": "text/html",
        "retained_artifact_digests": ["sha256:" + "a" * 64],
    }
    edge = _bind({
        "schema_version": "ao.lore.evidence-relationship-edge.v0.1",
        "edge_id": "edge-one",
        "edge_type": "explains",
        "source_kind": "claim",
        "source_id": "claim-one",
        "source_evidence_digest": excerpt_digest,
        "target_kind": "workflow",
        "target_id": "workflow-one",
        "target_evidence_digest": "sha256:" + "9" * 64,
        "supporting_excerpt_digest": excerpt_digest,
        "reason_code": "official_explanation",
        "qualification": None,
    }, "edge_digest")
    source_edge = _bind({
        "schema_version": "ao.lore.evidence-relationship-edge.v0.1",
        "edge_id": "edge-source-one",
        "edge_type": "cites",
        "source_kind": "source",
        "source_id": source["source_id"],
        "source_evidence_digest": source["source_digest"],
        "target_kind": "claim",
        "target_id": "claim-one",
        "target_evidence_digest": excerpt_digest,
        "supporting_excerpt_digest": excerpt_digest,
        "reason_code": "source_citation",
        "qualification": None,
    }, "edge_digest")
    question = _bind({
        "schema_version": "ao.lore.evidence-operational-question.v0.1",
        "question_id": "question-one",
        "prompt": "What does the guidance require?",
        "expected_outcome": "answer",
        "required_authority_roles": ["technical_guidance"],
        "required_evidence_ids": ["claim-one"],
        "forbidden_evidence_ids": [],
        "qualifications": [],
    }, "question_digest")
    graph = {
        "schema_version": "ao.lore.evidence-graph-manifest.v0.1",
        "graph_id": "graph-one",
        "root_workflow_id": "workflow-one",
        "root_workflow_digest": "sha256:" + "9" * 64,
        "source_registry_digest": "sha256:" + "8" * 64,
        "sources": [source],
        "claims": [{
            "claim_id": "claim-one",
            "source_id": source["source_id"],
            "source_digest": source["source_digest"],
            "authority_role": "technical_guidance",
            "excerpt": excerpt,
            "excerpt_digest": excerpt_digest,
            "citation_anchor": "section one",
            "operational_question_ids": ["question-one"],
            "subject_terms": ["guidance"],
            "semantic_reason_codes": ["topic_match"],
        }],
        "edges": [edge, source_edge],
        "operational_questions": [question],
        **_authority(),
    }
    return _bind(graph, "graph_digest")


def _policy(records: list[dict], graph: dict, *, policy_suffix: str = "one", successor: str | None = None) -> dict:
    value = {
        "schema_version": "ao.lore.evidence-freshness-policy.v0.1",
        "policy_id": f"policy-{policy_suffix}",
        "bundle_id": f"bundle-{policy_suffix}",
        "graph_id": graph["graph_id"],
        "graph_digest": graph["graph_digest"],
        "source_registry_digest": graph["source_registry_digest"],
        "verification_interval_seconds": 86400,
        "maximum_observation_age_seconds": 2592000,
        "maximum_observation_bytes": 4096,
        "maximum_redirect_hops": 2,
        "allowed_media_types": ["application/pdf", "text/html"],
        "allowed_terminal_classifications": [
            "unchanged", "updated", "superseded", "unavailable", "investigate",
        ],
        "sources": [{
            "source_id": record["source_id"],
            "canonical_locator": record["requested_locator"],
            "prior_record_digest": record["record_digest"],
            "prior_content_digest": record["content_digest"],
            "prior_media_type": record["media_type"],
            "declared_successor_locator": successor,
        } for record in records],
        **_authority(),
    }
    return _bind(value, "policy_digest")


def _observation(policy_value: dict, record: dict, *, status="observed", content_seed="a",
                 final_locator=None, media_type="text/html", version="2025 edition",
                 effective_date="2025-01-01", observed_at="2026-08-13T18:00:00Z") -> dict:
    successful = status == "observed"
    final_locator = final_locator if final_locator is not None else record["requested_locator"]
    value = {
        "schema_version": "ao.lore.evidence-freshness-observation.v0.1",
        "observation_id": f"observation-{record['source_id']}-{observed_at[-9:-1].replace(':', '')}",
        "policy_id": policy_value["policy_id"],
        "bundle_id": policy_value["bundle_id"],
        "graph_id": policy_value["graph_id"],
        "graph_digest": policy_value["graph_digest"],
        "source_id": record["source_id"],
        "prior_record_digest": record["record_digest"],
        "requested_locator": record["requested_locator"],
        "final_locator": final_locator if successful else None,
        "observed_at": observed_at,
        "status": status,
        "http_status": 200 if successful else 410,
        "media_type": media_type if successful else None,
        "byte_count": 2048 if successful else 0,
        "content_digest": ("sha256:" + (content_seed * 64)[:64]) if successful else None,
        "redirect_chain": [] if final_locator == record["requested_locator"] else [final_locator],
        "version": version,
        "effective_date": effective_date,
        **_authority(),
    }
    return _bind(value, "observation_digest")


def _prepare(root: Path):
    source_root = root / "source-root"
    source_root.mkdir(parents=True)
    specs = (AcquisitionSpec("source-one", _fixture_locator("guide"), ("text/html",), ()),)
    records = acquire_official_evidence(
        specs,
        AcquisitionDependencies(
            source_root,
            FakeHTTP([HTTPResponse(200, (("Content-Type", "text/html"),), b"baseline")]),
            lambda: "2026-08-12T18:00:00Z",
            lambda: 1.0,
        ),
        limits=LIMITS,
    )
    graph = _graph()
    graph["sources"][0]["source_id"] = records[0]["source_id"]
    graph["sources"][0]["source_digest"] = records[0]["content_digest"]
    graph["sources"][0]["canonical_locator"] = records[0]["requested_locator"]
    graph["sources"][0]["media_type"] = records[0]["media_type"]
    graph["sources"][0]["retained_artifact_digests"] = [records[0]["content_digest"]]
    graph["claims"][0]["source_id"] = records[0]["source_id"]
    graph["claims"][0]["source_digest"] = records[0]["content_digest"]
    for edge in graph["edges"]:
        if edge["source_kind"] == "source":
            edge["source_id"] = records[0]["source_id"]
            edge["source_evidence_digest"] = records[0]["content_digest"]
            _bind(edge, "edge_digest")
    graph = _bind(graph, "graph_digest")
    return source_root, specs, records, graph, _policy(records, graph)


def _refresh(source_root: Path, specs, graph: dict, policy_value: dict, *, body: bytes,
             observed_at: str, status_code: int = 200, failpoint=lambda _: None):
    response = HTTPResponse(status_code, (("Content-Type", "text/html"),), body)
    return refresh_official_evidence(
        policy_value,
        graph,
        specs,
        AcquisitionDependencies(
            source_root,
            FakeHTTP([response]),
            lambda: observed_at,
            lambda: 1.0,
            failpoint,
        ),
        limits=LIMITS,
    )


def _run_rehearsal(root: Path) -> dict[str, str]:
    statuses: dict[str, str] = {}

    unchanged_root = root / "unchanged"
    source_root, specs, records, graph, policy_value = _prepare(unchanged_root)
    unchanged = _refresh(
        source_root,
        specs,
        graph,
        policy_value,
        body=b"baseline",
        observed_at="2026-08-13T18:00:00Z",
    )
    if unchanged["results"][0]["classification"] != "unchanged":
        raise RuntimeError("unchanged rehearsal failed")
    statuses["unchanged"] = "passed"

    updated_root = root / "updated"
    source_root, specs, records, graph, policy_value = _prepare(updated_root)
    updated = _refresh(
        source_root,
        specs,
        graph,
        policy_value,
        body=b"changed",
        observed_at="2026-08-13T18:05:00Z",
    )
    if updated["results"][0]["classification"] != "updated":
        raise RuntimeError("updated rehearsal failed")
    statuses["updated"] = "passed"

    unavailable_root = root / "unavailable"
    source_root, specs, records, graph, policy_value = _prepare(unavailable_root)
    unavailable = _refresh(
        source_root,
        specs,
        graph,
        policy_value,
        body=b"",
        observed_at="2026-08-13T18:10:00Z",
        status_code=410,
    )
    if unavailable["results"][0]["classification"] != "unavailable":
        raise RuntimeError("unavailable rehearsal failed")
    statuses["unavailable"] = "passed"

    investigate_root = root / "investigate"
    source_root, specs, records, graph, policy_value = _prepare(investigate_root)
    investigate = _refresh(
        source_root,
        specs,
        graph,
        policy_value,
        body=b"",
        observed_at="2026-08-13T18:15:00Z",
        status_code=503,
    )
    if investigate["results"][0]["classification"] != "investigate":
        raise RuntimeError("investigate rehearsal failed")
    statuses["investigate"] = "passed"

    crash_root = root / "crash-retry"
    source_root, specs, records, graph, policy_value = _prepare(crash_root)

    def failpoint(name):
        if name == "after_refresh_body_stage":
            raise RuntimeError("fixture crash")

    try:
        _refresh(
            source_root,
            specs,
            graph,
            policy_value,
            body=b"changed-body",
            observed_at="2026-08-13T18:20:00Z",
            failpoint=failpoint,
        )
        raise RuntimeError("crash rehearsal did not interrupt")
    except RuntimeError as exc:
        if str(exc) != "fixture crash":
            raise
    retry = refresh_official_evidence(
        policy_value,
        graph,
        specs,
        AcquisitionDependencies(source_root, FakeHTTP([]), lambda: "2026-08-13T18:20:00Z", lambda: 1.0),
        limits=LIMITS,
    )
    recovered = recover_freshness_transactions(FreshnessDependencies(source_root))
    if retry["results"][0]["classification"] != "updated" or recovered[0]["classification"] != "complete":
        raise RuntimeError("crash retry rehearsal failed")
    statuses["crash_retry"] = "passed"

    successor = _fixture_locator("guide", "successor")
    record = records[0]
    successor_policy = _policy(records, graph, policy_suffix="superseded", successor=successor)
    compared = compare_freshness_observation(
        successor_policy,
        record,
        graph,
        _observation(
            successor_policy,
            record,
            final_locator=successor,
            content_seed="b",
            version="2026 edition",
            effective_date="2026-01-01",
            observed_at="2026-08-13T18:25:00Z",
        ),
    )
    if compared["classification"] != "superseded":
        raise RuntimeError("superseded rehearsal failed")
    statuses["superseded"] = "passed"

    result = query_evidence_graph(graph, "guidance", freshness_summary=updated)
    if result["outcome"] != "investigate" or "Source freshness requires review." not in result["qualifications"]:
        raise RuntimeError("query downgrade rehearsal failed")
    statuses["query_downgrade"] = "passed"

    stdout, stderr = StringIO(), StringIO()
    with patch("ao_lore.__main__._run_workspace", side_effect=ValueError("private")), redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_main(["workspace", "query", "--workspace", "rehearsal", "--prompt", "private", "--json"])
    if code != 2 or stdout.getvalue() != "" or stderr.getvalue() != CLI_REJECTION:
        raise RuntimeError("redaction rehearsal failed")
    statuses["redaction"] = "passed"

    statuses["protected_inventory"] = "passed"
    return statuses


def _validate_evidence(value: object) -> bool:
    expected_keys = {
        "schema_version",
        "scenario_statuses",
        "scenario_count",
        "fixture_digest",
        "fixture_file_count",
        "source_head",
        "command_ledger",
        "protected_inventory_before",
        "protected_inventory_after",
        "canonical_brain_unchanged",
        "candidate_inventory_unchanged",
        "source_inventory_unchanged",
        "default_brain_mutated",
        "default_candidates_mutated",
        "default_sources_mutated",
        "live_refresh_executed",
        "network_used",
        "credentials_used",
        "evidence_digest",
    }
    if type(value) is not dict or set(value) != expected_keys:
        return False
    if value["schema_version"] != "ao.lore.evidence-freshness-rehearsal.v0.1":
        return False
    if re.fullmatch(r"[0-9a-f]{40}", value["source_head"] or "") is None:
        return False
    if type(value["command_ledger"]) is not list or len(value["command_ledger"]) < 3:
        return False
    if any(
        type(item) is not dict
        or set(item) != {"argv", "exit_code"}
        or type(item["argv"]) is not list
        or any(type(arg) is not str for arg in item["argv"])
        or type(item["exit_code"]) is not int
        for item in value["command_ledger"]
    ):
        return False
    if type(value["protected_inventory_before"]) is not dict or type(value["protected_inventory_after"]) is not dict:
        return False
    if value["protected_inventory_before"] != value["protected_inventory_after"]:
        return False
    if set(value["scenario_statuses"]) != set(EXPECTED_SCENARIOS):
        return False
    if any(status != "passed" for status in value["scenario_statuses"].values()):
        return False
    projection = {key: item for key, item in value.items() if key != "evidence_digest"}
    return value["evidence_digest"] == _bytes_digest(_canonical(projection))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--expected-brain-digest")
    args = parser.parse_args(argv)
    try:
        root = Path(os.path.abspath(args.root))
        _validate_root(root)
        expected_brain = (
            _require_digest(args.expected_brain_digest)
            if args.expected_brain_digest is not None
            else None
        )
        before_inventory = _protected_inventory()
        if expected_brain is not None and before_inventory.get("brain") != expected_brain:
            raise ValueError("expected inventory drift")
        if os.environ.get("AO_LORE_EVIDENCE_FRESHNESS_REHEARSAL_FAILPOINT") == "after_before_inventory":
            before_inventory = dict(before_inventory)
            before_inventory["brain"] = "sha256:" + "0" * 64
        evidence_path = root / "rehearsal-evidence.json"
        if args.check:
            value = json.loads(evidence_path.read_text(encoding="utf-8"))
            if value.get("protected_inventory_before") != before_inventory:
                return 1
            if value.get("protected_inventory_after") != before_inventory:
                return 1
            if not _validate_evidence(value):
                return 1
            fixture = root / "fixture"
            checked = subprocess.run(
                [sys.executable, str(GENERATOR), "--check", "--out", str(fixture)],
                cwd=REPOSITORY,
                capture_output=True,
                text=True,
            )
            if checked.returncode != 0:
                return 1
            sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            return 0
        if root.exists():
            raise ValueError("root exists")
        root.mkdir(parents=True)
        ledger = []
        head_result, head_entry = _run_command(["git", "rev-parse", "HEAD"])
        ledger.append(head_entry)
        if head_result.returncode != 0:
            raise ValueError("source head is unavailable")
        source_head = head_result.stdout.strip()
        if re.fullmatch(r"[0-9a-f]{40}", source_head) is None:
            raise ValueError("source head is invalid")
        fixture = root / "fixture"
        fixture_argument = os.path.relpath(str(fixture), REPOSITORY)
        generated, generated_entry = _run_command(
            [sys.executable, str(GENERATOR.relative_to(REPOSITORY)), "--out", fixture_argument],
            public_argv=[
                "PYTHON",
                str(GENERATOR.relative_to(REPOSITORY)),
                "--out",
                "FIXTURE_ROOT",
            ],
        )
        ledger.append(generated_entry)
        if generated.returncode != 0 or generated.stdout or generated.stderr:
            raise ValueError("fixture generator emitted output")
        checked_fixture, checked_entry = _run_command(
            [sys.executable, str(GENERATOR.relative_to(REPOSITORY)), "--check", "--out", fixture_argument],
            public_argv=[
                "PYTHON",
                str(GENERATOR.relative_to(REPOSITORY)),
                "--check",
                "--out",
                "FIXTURE_ROOT",
            ],
        )
        ledger.append(checked_entry)
        if checked_fixture.returncode != 0 or checked_fixture.stdout or checked_fixture.stderr:
            raise ValueError("fixture generator check failed")
        fixture_manifest = json.loads((fixture / "fixture-manifest.json").read_text(encoding="utf-8"))
        statuses = _run_rehearsal(root)
        if tuple(statuses) != EXPECTED_SCENARIOS:
            statuses = {name: statuses[name] for name in EXPECTED_SCENARIOS}
        after_inventory = _protected_inventory()
        if before_inventory != after_inventory:
            raise ValueError("protected inventory drift")
        evidence = {
            "schema_version": "ao.lore.evidence-freshness-rehearsal.v0.1",
            "scenario_statuses": {name: statuses[name] for name in sorted(statuses)},
            "scenario_count": len(statuses),
            "fixture_digest": fixture_manifest["fixture_digest"],
            "fixture_file_count": fixture_manifest["fixture_file_count"],
            "source_head": source_head,
            "command_ledger": ledger,
            "protected_inventory_before": before_inventory,
            "protected_inventory_after": after_inventory,
            "canonical_brain_unchanged": True,
            "candidate_inventory_unchanged": True,
            "source_inventory_unchanged": True,
            "default_brain_mutated": False,
            "default_candidates_mutated": False,
            "default_sources_mutated": False,
            "live_refresh_executed": False,
            "network_used": False,
            "credentials_used": False,
        }
        evidence["evidence_digest"] = _bytes_digest(_canonical(evidence))
        evidence_path.write_bytes(_canonical(evidence) + b"\n")
        sys.stdout.write(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (AssertionError, OSError, KeyError, RuntimeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        sys.stderr.write(REJECTION)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
