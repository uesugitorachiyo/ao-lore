#!/usr/bin/env python3
"""Generate one deterministic public-safe promotion fixture."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def digest(value):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode()


def documents():
    candidate_id = "candidate-public-fixture"
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.1", "candidate_id": candidate_id,
        "document_ir_digest": "sha256:" + "1" * 64, "concepts": [], "claim_mappings": [], "links": [],
        "contradiction_warnings": [], "canonical": False, "promotion_authority": False,
    }
    candidate_digest = digest(candidate)
    provenance = {
        "schema_version": "ao.lore.candidate-provenance.v0.1", "candidate_id": candidate_id, "candidate_digest": candidate_digest,
        "document_ir_digest": candidate["document_ir_digest"], "source_digest": "sha256:" + "2" * 64,
        "parser_id": "fixture-parser", "parser_version": "1.0.0", "parse_quality_report_digest": "sha256:" + "3" * 64,
        "parser_selection_report_digest": "sha256:" + "4" * 64, "distillation_trace_digest": "sha256:" + "5" * 64,
        "created_at": "2026-08-11T11:00:00Z",
    }
    review = {
        "schema_version": "ao.lore.candidate-review-event.v0.1", "sequence": 1, "candidate_id": candidate_id,
        "candidate_digest": candidate_digest, "previous_event_digest": None, "decision": "accept", "reviewer": "fixture-reviewer",
        "rationale": "public fixture accepted", "recorded_at": "2026-08-11T11:05:00Z",
    }
    review["event_digest"] = digest(review)
    files = {
        f"working/candidates/{candidate_id}/candidate.json": encoded(candidate),
        f"working/candidates/{candidate_id}/provenance.json": encoded(provenance),
        f"working/candidates/{candidate_id}/reviews/000001-{review['event_digest'][7:]}.json": encoded(review),
        "brain/README.md": b"# Public-safe fixture brain\n",
    }
    manifest = {
        "schema_version": "ao.lore.promotion-fixture.v0.1", "candidate_ids": [candidate_id],
        "files": [{"path": path, "digest": "sha256:" + hashlib.sha256(body).hexdigest()} for path, body in sorted(files.items())],
        "live_authorization": False, "network_authority": False, "canonical_repository_brain": False,
    }
    files["fixture-manifest.json"] = encoded(manifest)
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = documents()
    if args.check:
        for relative, body in expected.items():
            path = args.out / relative
            if not path.is_file() or path.read_bytes() != body:
                print(f"fixture differs: {relative}", file=sys.stderr)
                return 1
        actual = {path.relative_to(args.out).as_posix() for path in args.out.rglob("*") if path.is_file()}
        if actual != set(expected):
            print("fixture contains unknown files", file=sys.stderr)
            return 1
        return 0
    if args.out.exists():
        print("output already exists", file=sys.stderr)
        return 1
    for relative, body in expected.items():
        path = args.out / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (args.out / ".ao-lore" / "promotions" / "proposals").mkdir(parents=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
