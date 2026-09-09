#!/usr/bin/env python3
"""Run fixture-only promotion rehearsal and emit bounded public-safe evidence."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

# Repository scripts are directly executable before package installation.  The
# source root is fixed from this script's own location, never caller input.
REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ao_lore.promotion import (
    PromotionError, _PromotionDependencies, apply_promotion, build_rollback_proposal,
    inspect_promotion, prepare_promotion, recover_promotions, rollback_promotion,
)


GENERATOR = REPOSITORY / "tests" / "fixtures" / "ao_lore" / "promotion" / "generate.py"
CAMPAIGN = REPOSITORY / ".ao-lore" / "promotion-controls-rehearsal"
NOW = "2026-08-11T12:00:00Z"
SOURCE_HEAD = "1" * 40


def inventory(root):
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink(): raise RuntimeError("brain inventory contains a link")
        if path.is_file():
            body = path.read_bytes(); records.append([path.relative_to(root).as_posix(), len(body), hashlib.sha256(body).hexdigest()])
    return hashlib.sha256(json.dumps(records, separators=(",", ":")).encode()).hexdigest()


def generate(root):
    subprocess.run([sys.executable, str(GENERATOR), "--out", str(root)], check=True)
    return _PromotionDependencies(root / "working" / "candidates", root / "brain", root / ".ao-lore" / "promotions", lambda: NOW, lambda: SOURCE_HEAD)


def auth(proposal, operation="apply"):
    value = {
        "schema_version": "ao.lore.promotion-authorization.v0.1", "policy_version": "ao.lore.promotion-policy.v0.1",
        "operation": operation, "authorization_id": f"authorization-{operation}-rehearsal", "nonce": f"nonce-{operation}-rehearsal",
        "operator_id": "opaque-rehearsal-operator", "authentication_scope": "local_operator_assertion",
        "proposal_digest": proposal["proposal_digest"], "promotion_id": proposal["promotion_id"], "candidate_digest": proposal["candidate_digest"],
        "provenance_digest": proposal["provenance_digest"], "accepted_review_head_digest": proposal["accepted_review_head_digest"],
        "canonical_entry_digest": proposal["canonical_entry_digest"],
        "expected_brain_inventory_digest": proposal.get("current_brain_inventory_digest", proposal.get("prior_brain_inventory_digest")),
        "expected_prior_generation_id": proposal.get("current_generation_id", proposal.get("prior_generation_id")),
        "expected_result_generation_id": proposal["expected_generation_id"], "allowed_write_set_digest": proposal["allowed_write_set_digest"],
        "source_head": proposal["source_head"], "issued_at": "2026-08-11T11:59:00Z", "expires_at": "2026-08-11T12:01:00Z",
        "one_use_only": True, "product_self_issued": False, "provider_authority": False, "network_authority": False,
        "publication_authority": False, "release_authority": False, "deployment_authority": False, "batch_authority": False,
        "overwrite_authority": False, "unattended_authority": False, "credential_authority": False, "authority_advance": False,
    }
    return value


def write(path, value): path.write_text(json.dumps(value, sort_keys=True) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve(); repository = REPOSITORY.resolve(); campaign = CAMPAIGN.resolve()
    local_runtime = repository / ".ao-lore"
    owned = campaign in root.parents or local_runtime in root.parents
    if root in {repository, repository / "brain", local_runtime} or not owned:
        print("rehearsal root must be a new campaign-owned disposable directory", file=sys.stderr); return 2
    evidence_path = root / "rehearsal-evidence.json"
    if args.check:
        value = json.loads(evidence_path.read_text())
        return 0 if value.get("schema_version") == "ao.lore.promotion-rehearsal.v0.1" and value.get("real_brain_unchanged") is True and value.get("scenarios_passed") == 5 else 1
    if root.exists(): print("rehearsal root already exists", file=sys.stderr); return 2
    root.mkdir(parents=True)
    real_before = inventory(repository / "brain")
    scenarios = []

    happy = root / "happy"; deps = generate(happy)
    proposal_path = deps.proposals_root / "proposal.json"; proposal = prepare_promotion("candidate-public-fixture", proposal_path, dependencies=deps)
    apply_auth = happy / "apply-authorization.json"; write(apply_auth, auth(proposal))
    apply_promotion(proposal_path, apply_auth, dependencies=deps); inspect_promotion(proposal["promotion_id"], dependencies=deps)
    rollback = build_rollback_proposal(proposal["promotion_id"], dependencies=deps); rollback_auth = happy / "rollback-authorization.json"; write(rollback_auth, auth(rollback, "rollback"))
    rollback_promotion(proposal["promotion_id"], rollback_auth, dependencies=deps); inspected = inspect_promotion(proposal["promotion_id"], dependencies=deps)
    if inspected["status"] != "rolled_back" or recover_promotions(dependencies=deps)["status"] != "no_op": raise RuntimeError("happy rehearsal failed")
    scenarios.append("prepare_apply_inspect_rollback_inspect_recover")

    stale = root / "stale"; deps = generate(stale); proposal_path = deps.proposals_root / "proposal.json"; proposal = prepare_promotion("candidate-public-fixture", proposal_path, dependencies=deps)
    stale_auth = auth(proposal); stale_auth["expires_at"] = "2026-08-11T11:59:59Z"; path = stale / "authorization.json"; write(path, stale_auth)
    try: apply_promotion(proposal_path, path, dependencies=deps); raise RuntimeError("stale authorization accepted")
    except PromotionError: scenarios.append("stale_authorization_rejected")

    crash = root / "crash"; base = generate(crash)
    def failpoint(name):
        if name == "after_generation_publish": raise RuntimeError("fixture process loss")
    deps = _PromotionDependencies(base.candidate_root, base.brain_root, base.promotions_root, lambda: NOW, lambda: SOURCE_HEAD, failpoint)
    proposal_path = deps.proposals_root / "proposal.json"; proposal = prepare_promotion("candidate-public-fixture", proposal_path, dependencies=deps); path = crash / "authorization.json"; write(path, auth(proposal))
    try: apply_promotion(proposal_path, path, dependencies=deps)
    except RuntimeError: pass
    resumed = _PromotionDependencies(base.candidate_root, base.brain_root, base.promotions_root, lambda: NOW, lambda: SOURCE_HEAD)
    if recover_promotions(dependencies=resumed)["status"] != "recovered": raise RuntimeError("crash recovery failed")
    scenarios.append("crash_recovered")

    corrupt = root / "corrupt"; deps = generate(corrupt); proposal_path = deps.proposals_root / "proposal.json"; proposal = prepare_promotion("candidate-public-fixture", proposal_path, dependencies=deps); path = corrupt / "authorization.json"; write(path, auth(proposal)); apply_promotion(proposal_path, path, dependencies=deps)
    next((deps.promotions_root / "transactions").glob("*.json")).write_text("{}\n")
    try: inspect_promotion(proposal["promotion_id"], dependencies=deps); raise RuntimeError("corruption accepted")
    except PromotionError: scenarios.append("corruption_rejected")

    concurrent = root / "concurrent"; deps = generate(concurrent); proposal_path = deps.proposals_root / "proposal.json"; proposal = prepare_promotion("candidate-public-fixture", proposal_path, dependencies=deps); path = concurrent / "authorization.json"; write(path, auth(proposal))
    generation = proposal["allowed_write_set"][0].split("/")[1]; foreign = deps.brain_root / "generations" / generation; foreign.mkdir(parents=True); (foreign / "foreign.txt").write_text("preserve")
    try: apply_promotion(proposal_path, path, dependencies=deps); raise RuntimeError("foreign writer accepted")
    except PromotionError:
        if (foreign / "foreign.txt").read_text() != "preserve": raise RuntimeError("foreign state changed")
        scenarios.append("concurrent_writer_preserved")

    real_after = inventory(repository / "brain")
    evidence = {"schema_version": "ao.lore.promotion-rehearsal.v0.1", "scenarios": scenarios, "scenarios_passed": len(scenarios), "real_brain_before": real_before, "real_brain_after": real_after, "real_brain_unchanged": real_before == real_after, "live_promotion": False, "network_used": False, "credentials_used": False}
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return 0 if evidence["real_brain_unchanged"] and len(scenarios) == 5 else 1


if __name__ == "__main__": raise SystemExit(main())
