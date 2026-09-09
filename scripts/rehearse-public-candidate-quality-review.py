#!/usr/bin/env python3
"""Run or verify the fixed, offline public-candidate quality campaign."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from ao_lore.candidate_quality import (  # noqa: E402
    _derive_candidate_quality_results,
    _publish_candidate_quality_campaign,
    _verify_candidate_quality_publication,
)
from ao_lore.candidate_quality_contracts import validate_candidate_quality_summary  # noqa: E402
from ao_lore._strict_io import parse_strict_json  # noqa: E402


RUNTIME_ROOT = REPOSITORY_ROOT / ".ao-lore/public-candidate-quality-review-20260812"
ARTIFACT_ROOT = RUNTIME_ROOT / "artifacts"
PUBLICATION_ROOT = RUNTIME_ROOT / "campaigns"


def _read(name: str) -> object:
    path = ARTIFACT_ROOT / f"{name}.json"
    body = path.read_bytes()
    if not body or len(body) > 4 * 1024 * 1024:
        raise SystemExit("ao-lore: candidate quality artifact rejected")
    return parse_strict_json(body, label="candidate quality artifact")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify an existing immutable campaign")
    args = parser.parse_args(argv)
    results, campaign, summary = _derive_candidate_quality_results(
        _read("verified"), _read("policy"), _read("sample"), _read("annotations"),
        _read("questions"), _read("question-results")
    )
    if args.check:
        readback = _verify_candidate_quality_publication(PUBLICATION_ROOT, results, campaign, summary)
    else:
        PUBLICATION_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        readback = _publish_candidate_quality_campaign(PUBLICATION_ROOT, results, campaign, summary)
    published = PUBLICATION_ROOT / readback["publication_relpath"] / "summary.json"
    checked = validate_candidate_quality_summary(json.loads(published.read_text(encoding="utf-8")))
    if checked != summary:
        raise SystemExit("ao-lore: candidate quality campaign verification failed")
    output = {
        "schema": "ao.lore.candidate-quality-operation-readback.v0.1",
        "status": "verified" if args.check else "completed",
        "campaign_digest": summary["campaign_digest"],
        "summary_digest": summary["summary_digest"],
        "recovery_digest": readback["recovery_digest"],
        "candidate_count": 6,
        "recommendations": [row["recommendation"] for row in summary["candidate_recommendations"]],
        "safe_to_execute": False,
        "executes_work": False,
        "approves_work": False,
        "mutates_repositories": False,
        "widens_policy": False,
        "publishes_artifacts": False,
    }
    print(json.dumps(output, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
