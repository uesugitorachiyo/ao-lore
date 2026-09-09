#!/usr/bin/env python3
"""Generate a deterministic public-safe evidence-freshness rehearsal fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "fixture-spec.json"
REPOSITORY = ROOT.parents[3]
REJECTION = "evidence freshness fixture rejected\n"


def _canonical_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _write(path: Path, value: object) -> None:
    path.write_bytes(_canonical_bytes(value))


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _load_spec() -> dict:
    value = json.loads(SPEC.read_text(encoding="utf-8"))
    body = _canonical_bytes(value)
    if SPEC.read_bytes() != body:
        raise SystemExit("fixture spec is not canonical")
    return value


def _manifest(spec: dict) -> dict:
    spec_body = _canonical_bytes(spec)
    return {
        "schema_version": "ao.lore.evidence-freshness-rehearsal-fixture.v0.1",
        "scenario_ids": spec["scenario_ids"],
        "fixture_digest": _digest(spec_body),
        "fixture_file_count": 2,
        "fixture_files": ["fixture-manifest.json", "fixture-spec.json"],
        "canonical_repository_sources": False,
        "network_used": False,
    }


def _validate_out(path: Path) -> Path:
    out = Path(os.path.abspath(path))
    repository = REPOSITORY.resolve()
    runtime = (repository / ".ao-lore").resolve()
    forbidden = {
        repository,
        (repository / "brain").resolve(),
        (repository / "working" / "candidates").resolve(),
        (repository / "sources").resolve(),
        runtime,
    }
    if out in forbidden or runtime not in out.parents:
        raise ValueError("unowned root")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    spec = _load_spec()
    manifest = _manifest(spec)
    try:
        out = _validate_out(args.out)
        manifest_path = out / "fixture-manifest.json"
        spec_path = out / "fixture-spec.json"

        if args.check:
            if not out.is_dir():
                return 1
            if not manifest_path.is_file() or not spec_path.is_file():
                return 1
            if spec_path.read_bytes() != _canonical_bytes(spec):
                return 1
            if manifest_path.read_bytes() != _canonical_bytes(manifest):
                return 1
            return 0

        out.mkdir(parents=True, exist_ok=False)
        _write(spec_path, spec)
        _write(manifest_path, manifest)
        return 0
    except (OSError, ValueError):
        sys.stderr.write(REJECTION)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
