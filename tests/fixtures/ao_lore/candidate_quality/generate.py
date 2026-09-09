#!/usr/bin/env python3
"""Generate deterministic public-safe candidate-quality sampling fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


SPEC = Path(__file__).with_name("fixture-spec.json")
EXPECTED_SAMPLE_COUNTS = [19, 13, 14, 12, 14, 24]
EXPECTED_CLAIM_COUNTS = [19, 13, 73, 21, 71, 289]
EXPECTED_POLICY_FIELDS = {
    "schema_version": "ao.lore.candidate-quality-sampling-fixture-spec.v0.1",
    "campaign_id": "candidate-quality-campaign-20260812",
    "correlation_id": "ao-lore-public-candidate-quality-review-20260812",
    "terminal_readback_digest": "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
}


def canonical_digest(value):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n"


def _sha(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _load_spec() -> dict:
    specification = json.loads(SPEC.read_text(encoding="utf-8"))
    if specification.get("schema_version") != EXPECTED_POLICY_FIELDS["schema_version"]:
        raise ValueError("fixture spec schema_version differs")
    for field in ("campaign_id", "correlation_id", "terminal_readback_digest"):
        if specification.get(field) != EXPECTED_POLICY_FIELDS[field]:
            raise ValueError(f"fixture spec {field} differs")
    if specification.get("claim_counts") != EXPECTED_CLAIM_COUNTS:
        raise ValueError("fixture spec claim_counts differ")
    if specification.get("sample_counts") != EXPECTED_SAMPLE_COUNTS:
        raise ValueError("fixture spec sample_counts differ")
    if specification.get("public_safe") is not True:
        raise ValueError("fixture spec public_safe differs")
    if specification.get("live_authorization") is not False:
        raise ValueError("fixture spec live_authorization differs")
    if specification.get("network_authority") is not False:
        raise ValueError("fixture spec network_authority differs")
    return specification


def _candidate_id(index: int) -> str:
    return f"candidate-{index:02d}"


def _claim_id(index: int, ordinal: int) -> str:
    return f"claim-{index:02d}-{ordinal:03d}"


def _citation_id(index: int, ordinal: int) -> str:
    return f"citation-{index:02d}-{ordinal:03d}"


def _base_text(index: int, ordinal: int) -> str:
    return f"Synthetic public claim {index:02d}-{ordinal:03d}."


def _core_record(index: int, ordinal: int, text: str, source_block_ids: list[str]) -> dict:
    return {
        "candidate_id": _candidate_id(index),
        "claim_ordinal": ordinal,
        "claim_id": _claim_id(index, ordinal),
        "citation_id": _citation_id(index, ordinal),
        "source_block_ids": list(source_block_ids),
        "claim_text": text,
        "citation_text": text,
    }


def _claim_record(
    index: int,
    ordinal: int,
    *,
    text: str | None = None,
    block_type: str = "paragraph",
    binding_risk_suspect: bool = False,
    normalized_duplicate_suspect: bool = False,
    fragmentation_suspect: bool = False,
) -> dict:
    source_block_ids = [f"b{ordinal:03d}"]
    if binding_risk_suspect:
        source_block_ids.append(f"b{ordinal:03d}-alt")
    claim_text = text if text is not None else _base_text(index, ordinal)
    record = _core_record(index, ordinal, claim_text, source_block_ids)
    record["claim_record_digest"] = canonical_digest(record)
    record["duplicate_suspect"] = False
    record["normalized_duplicate_suspect"] = normalized_duplicate_suspect
    record["fragmentation_suspect"] = fragmentation_suspect
    record["binding_risk_suspect"] = binding_risk_suspect
    record["block_type"] = block_type
    return record


def _candidate_result(index: int, count: int) -> dict:
    records = [_claim_record(index, ordinal) for ordinal in range(1, count + 1)]

    if index == 3:
        records[4] = _claim_record(3, 5, binding_risk_suspect=True)
        records[9] = _claim_record(3, 10, block_type="heading")
        records[10] = _claim_record(3, 11, block_type="list")
        records[11] = _claim_record(3, 12, block_type="table")
        records[12] = _claim_record(3, 13, block_type="procedure")
        records[13] = _claim_record(3, 14, block_type="warning")
        records[14] = _claim_record(3, 15, block_type="qualification")
        records[19] = _claim_record(3, 20, normalized_duplicate_suspect=True)
        records[29] = _claim_record(
            3,
            30,
            text="fragmented synthetic public claim",
            fragmentation_suspect=True,
        )
        records[39] = _claim_record(
            3,
            40,
            text="Synthetic public claim 03-040 with extra bounded detail for longest selection coverage and deterministic policy ordering.",
        )
        records[40] = _claim_record(3, 41, text="Short.")
    elif index == 4:
        for ordinal in range(1, 16):
            records[ordinal - 1] = _claim_record(4, ordinal, binding_risk_suspect=True)
    elif index == 5:
        records[2] = _claim_record(5, 3, binding_risk_suspect=True)
        records[3] = _claim_record(5, 4, normalized_duplicate_suspect=True)
        records[4] = _claim_record(
            5,
            5,
            text="fragmented candidate five claim",
            fragmentation_suspect=True,
        )
        records[7] = _claim_record(5, 8, block_type="appendix")
        records[8] = _claim_record(5, 9, block_type="sidebar")
        records[39] = _claim_record(
            5,
            40,
            text="Synthetic public claim 05-040 with extra bounded detail for longest selection coverage and deterministic fill ordering.",
        )
        records[40] = _claim_record(5, 41, text="Tiny.")
    elif index == 6:
        records[199] = _claim_record(
            6,
            200,
            text="Synthetic public claim 06-200 with extra bounded detail for longest selection coverage and deterministic fill ordering.",
        )
        records[200] = _claim_record(6, 201, text="Min.")

    duplicate_count = sum(1 for record in records if record["duplicate_suspect"])
    normalized_duplicate_count = sum(1 for record in records if record["normalized_duplicate_suspect"])
    fragmentation_count = sum(1 for record in records if record["fragmentation_suspect"])
    result = {
        "candidate_id": _candidate_id(index),
        "candidate_digest": _sha(f"candidate:{index}"),
        "provenance_digest": _sha(f"provenance:{index}"),
        "source_digest": _sha(f"source:{index}"),
        "claim_count": count,
        "citation_count": count,
        "claim_records": records,
        "automatic_check_counts": {
            "verified_claim_count": count,
            "verified_citation_count": count,
            "duplicate_suspect_count": duplicate_count,
            "normalized_duplicate_suspect_count": normalized_duplicate_count,
            "fragmentation_suspect_count": fragmentation_count,
            "binding_error_count": 0,
        },
    }
    result["verified_result_digest"] = canonical_digest(
        {key: value for key, value in result.items() if key != "verified_result_digest"}
    )
    return result


def build_fixture():
    specification = _load_spec()
    claim_counts = specification["claim_counts"]
    verified_claims = {
        "campaign_id": specification["campaign_id"],
        "correlation_id": specification["correlation_id"],
        "terminal_readback_digest": specification["terminal_readback_digest"],
        "total_verified_claim_count": sum(claim_counts),
        "total_verified_citation_count": sum(claim_counts),
        "candidate_results": [
            _candidate_result(index, count)
            for index, count in enumerate(claim_counts, start=1)
        ],
    }
    payload = {
        "schema_version": "ao.lore.candidate-quality-sampling-fixture.v0.1",
        "verified_claims": verified_claims,
        "fixture_digest": "",
        "public_safe": specification["public_safe"],
        "live_authorization": specification["live_authorization"],
        "network_authority": specification["network_authority"],
    }
    payload["fixture_digest"] = canonical_digest(
        {key: value for key, value in payload.items() if key != "fixture_digest"}
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    try:
        payload = build_fixture()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1
    if arguments.check:
        try:
            specification = _load_spec()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 1
        if specification["fixture_digest"] != payload["fixture_digest"]:
            return 1
        if payload["public_safe"] is not True:
            return 1
        if payload["live_authorization"] is not False:
            return 1
        if payload["network_authority"] is not False:
            return 1
        return 0
    sys.stdout.write(encoded(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
