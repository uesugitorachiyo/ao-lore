#!/usr/bin/env python3
"""Generate deterministic public-safe canonical-reader rehearsal inputs."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


SPEC = Path(__file__).with_name("fixture-spec.json")


def canonical_digest(value):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()


def candidate_document(specification):
    candidate_id = specification["candidate_id"]
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.1",
        "candidate_id": candidate_id,
        "document_ir_digest": "sha256:" + "1" * 64,
        "concepts": [],
        "claim_mappings": [],
        "links": [],
        "contradiction_warnings": [],
        "canonical": False,
        "promotion_authority": False,
    }
    if specification["version"] == "v0.2":
        claim = {
            "claim_id": "claim-fixture",
            "text": specification["claim"],
            "source_block_ids": ["block-fixture"],
            "citation_id": "citation-fixture",
        }
        citation = {
            "citation_id": "citation-fixture",
            "render_text": specification["citation"],
            "source_block_ids": ["block-fixture"],
        }
        candidate.update({
            "schema_version": "ao.lore.okf-candidate.v0.2",
            "claim_mappings": [{"claim_id": "claim-fixture", "block_ids": ["block-fixture"]}],
            "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
            "claims_digest": canonical_digest({"domain": "ao.lore.canonical-claim-set.v0.1", "claims": [claim]}),
            "claims": [claim],
            "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
            "citations_digest": canonical_digest({"domain": "ao.lore.canonical-citation-set.v0.1", "citations": [citation]}),
            "citations": [citation],
            "knowledge_policy": {"sensitivity": "public", "stale_after": specification["stale_after"]},
        })
    return candidate


def documents():
    specification = json.loads(SPEC.read_text(encoding="utf-8"))
    files = {"brain/README.md": b"# Disposable public-safe fixture brain\n"}
    candidate_ids = []
    for index, item in enumerate(specification["candidates"], 1):
        candidate = candidate_document(item)
        candidate_id = candidate["candidate_id"]
        candidate_ids.append(candidate_id)
        candidate_digest = canonical_digest(candidate)
        provenance = {
            "schema_version": "ao.lore.candidate-provenance.v0.1",
            "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "document_ir_digest": candidate["document_ir_digest"],
            "source_digest": "sha256:" + f"{index + 1:x}" * 64,
            "parser_id": "public-fixture-parser",
            "parser_version": "1.0.0",
            "parse_quality_report_digest": "sha256:" + "4" * 64,
            "parser_selection_report_digest": "sha256:" + "5" * 64,
            "distillation_trace_digest": "sha256:" + "6" * 64,
            "created_at": "2026-08-12T10:00:00Z",
        }
        review = {
            "schema_version": "ao.lore.candidate-review-event.v0.1",
            "sequence": 1,
            "candidate_id": candidate_id,
            "candidate_digest": candidate_digest,
            "previous_event_digest": None,
            "decision": "accept",
            "reviewer": "public-fixture-reviewer",
            "rationale": "public fixture accepted",
            "recorded_at": "2026-08-12T10:05:00Z",
        }
        review["event_digest"] = canonical_digest(review)
        prefix = f"working/candidates/{candidate_id}"
        files[f"{prefix}/candidate.json"] = encoded(candidate)
        files[f"{prefix}/provenance.json"] = encoded(provenance)
        files[f"{prefix}/reviews/000001-{review['event_digest'][7:]}.json"] = encoded(review)
    manifest = {
        "schema_version": "ao.lore.knowledge-rehearsal-fixture.v0.1",
        "candidate_ids": sorted(candidate_ids),
        "file_count": len(files),
        "fixture_digest": canonical_digest([
            {"path": path, "digest": "sha256:" + hashlib.sha256(body).hexdigest()}
            for path, body in sorted(files.items())
        ]),
        "canonical_repository_brain": False,
        "live_authorization": False,
        "network_authority": False,
    }
    files["fixture-manifest.json"] = encoded(manifest)
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    expected = documents()
    if arguments.check:
        actual = {path.relative_to(arguments.out).as_posix() for path in arguments.out.rglob("*") if path.is_file()}
        for relative, body in expected.items():
            path = arguments.out / relative
            if not path.is_file() or path.read_bytes() != body:
                return 1
        allowed = set(expected)
        if actual - allowed:
            return 1
        return 0
    if arguments.out.exists():
        return 1
    for relative, body in expected.items():
        path = arguments.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (arguments.out / "brain" / "generations").mkdir()
    (arguments.out / ".ao-lore" / "promotions" / "proposals").mkdir(parents=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
