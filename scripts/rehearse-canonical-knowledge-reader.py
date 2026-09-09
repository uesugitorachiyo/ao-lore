#!/usr/bin/env python3
"""Rehearse canonical knowledge reading against disposable public fixtures."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ao_lore.knowledge import (
    KnowledgeReadError,
    _KnowledgeDependencies,
    answer_knowledge,
    knowledge_status,
    search_knowledge,
)
from ao_lore.promotion import (
    PromotionError,
    _PromotionDependencies,
    apply_promotion,
    build_rollback_proposal,
    inspect_promotion,
    prepare_promotion,
    recover_promotions,
    rollback_promotion,
)


GENERATOR = REPOSITORY / "tests" / "fixtures" / "ao_lore" / "knowledge" / "generate.py"
FIXED_NOW = "2026-08-12T12:00:00Z"
SOURCE_HEAD = "1" * 40
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REJECTION = "knowledge rehearsal rejected\n"
EXPECTED_SCENARIOS = (
    "answerable_v0_2_promotion",
    "status_search_answer",
    "partial_refuse_investigate",
    "rollback_exclusion",
    "recover_interrupted_apply",
    "mixed_legacy_new",
    "empty_snapshot",
    "corruption_rejected",
    "reader_vs_apply",
    "reader_vs_rollback",
    "reader_vs_recover",
    "two_readers",
    "foreign_writer_preserved",
    "exact_rerun",
)


def _canonical(value):
    body = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n", encoding="utf-8")


def _dependencies(root):
    return _PromotionDependencies(
        root / "working" / "candidates",
        root / "brain",
        root / ".ao-lore" / "promotions",
        lambda: FIXED_NOW,
        lambda: SOURCE_HEAD,
    )


def _generate(root):
    subprocess.run([sys.executable, str(GENERATOR), "--out", str(root)], check=True, capture_output=True)
    return _dependencies(root)


def _authorization(proposal, operation="apply", suffix="fixture"):
    return {
        "schema_version": "ao.lore.promotion-authorization.v0.1",
        "policy_version": "ao.lore.promotion-policy.v0.1",
        "operation": operation,
        "authorization_id": f"authorization-{operation}-{suffix}",
        "nonce": f"nonce-{operation}-{suffix}",
        "operator_id": "public-fixture-operator",
        "authentication_scope": "local_operator_assertion",
        "proposal_digest": proposal["proposal_digest"],
        "promotion_id": proposal["promotion_id"],
        "candidate_digest": proposal["candidate_digest"],
        "provenance_digest": proposal["provenance_digest"],
        "accepted_review_head_digest": proposal["accepted_review_head_digest"],
        "canonical_entry_digest": proposal["canonical_entry_digest"],
        "expected_brain_inventory_digest": proposal.get("current_brain_inventory_digest", proposal.get("prior_brain_inventory_digest")),
        "expected_prior_generation_id": proposal.get("current_generation_id", proposal.get("prior_generation_id")),
        "expected_result_generation_id": proposal["expected_generation_id"],
        "allowed_write_set_digest": proposal["allowed_write_set_digest"],
        "source_head": proposal["source_head"],
        "issued_at": "2026-08-12T11:59:00Z",
        "expires_at": "2026-08-12T12:01:00Z",
        "one_use_only": True,
        "product_self_issued": False,
        "provider_authority": False,
        "network_authority": False,
        "publication_authority": False,
        "release_authority": False,
        "deployment_authority": False,
        "batch_authority": False,
        "overwrite_authority": False,
        "unattended_authority": False,
        "credential_authority": False,
        "authority_advance": False,
    }


def _prepare_apply(root, candidate_id="candidate-answerable", suffix="fixture"):
    dependencies = _generate(root)
    proposal_path = dependencies.proposals_root / f"{suffix}.json"
    proposal = prepare_promotion(candidate_id, proposal_path, dependencies=dependencies)
    authorization_path = root / f"authorization-{suffix}.json"
    _write(authorization_path, _authorization(proposal, suffix=suffix))
    applied = apply_promotion(proposal_path, authorization_path, dependencies=dependencies)
    return dependencies, proposal, applied


def _rollback(dependencies, root, proposal, applied, suffix="fixture"):
    rollback = build_rollback_proposal(applied["promotion_id"], dependencies=dependencies)
    authorization_path = root / f"rollback-{suffix}.json"
    _write(authorization_path, _authorization(rollback, "rollback", suffix))
    return rollback_promotion(proposal["promotion_id"], authorization_path, dependencies=dependencies)


def _parallel(*operations):
    barrier = threading.Barrier(len(operations) + 1)
    results = []
    errors = []

    def run(operation):
        barrier.wait()
        try:
            results.append(operation())
        except (KnowledgeReadError, PromotionError) as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=run, args=(operation,)) for operation in operations]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(5)
    if any(thread.is_alive() for thread in threads):
        raise RuntimeError("fixture concurrency did not terminate")
    return results, errors


def _rehearse(root):
    passed = {}

    happy = root / "happy"
    dependencies, proposal, applied = _prepare_apply(happy)
    if proposal["canonical_entry"]["schema_version"] != "ao.lore.okf-canonical-entry.v0.3":
        raise RuntimeError("answerable fixture was not promoted additively")
    passed["answerable_v0_2_promotion"] = "passed"
    knowledge_dependencies = _KnowledgeDependencies(dependencies.brain_root, None)
    status = knowledge_status(_dependencies=knowledge_dependencies)
    search = search_knowledge("canonical rehearsal policy", _dependencies=knowledge_dependencies)
    answer = answer_knowledge("canonical rehearsal policy", _dependencies=knowledge_dependencies)
    if status["answerable_v0_3_entry_count"] != 1 or len(search["hits"]) != 1 or answer["status"] != "answer":
        raise RuntimeError("reader happy path failed")
    passed["status_search_answer"] = "passed"

    empty = root / "empty"
    empty_dependencies = _generate(empty)
    empty_knowledge = _KnowledgeDependencies(empty_dependencies.brain_root, None)
    if knowledge_status(_dependencies=empty_knowledge)["effective_entry_count"] != 0:
        raise RuntimeError("empty fixture was not empty")
    if answer_knowledge("absent", _dependencies=empty_knowledge)["status"] != "refuse":
        raise RuntimeError("empty fixture did not refuse")
    passed["empty_snapshot"] = "passed"

    stale = root / "stale"
    stale_dependencies, _, _ = _prepare_apply(stale, "candidate-stale", "stale")
    stale_answer = answer_knowledge("canonical rehearsal archive", _dependencies=_KnowledgeDependencies(stale_dependencies.brain_root, None))
    unmatched = answer_knowledge("absent tokens", _dependencies=knowledge_dependencies)
    manifest = next((happy / "brain" / "generations").glob("*/manifest.json"))
    original_manifest = manifest.read_bytes()
    corrupted = json.loads(original_manifest)
    corrupted["manifest_digest"] = "sha256:" + "0" * 64
    _write(manifest, corrupted)
    investigated = answer_knowledge("canonical rehearsal policy", _dependencies=knowledge_dependencies)
    manifest.write_bytes(original_manifest)
    if (stale_answer["status"], unmatched["status"], investigated["status"]) != ("partial", "refuse", "investigate"):
        raise RuntimeError("reader outcomes were incomplete")
    passed["partial_refuse_investigate"] = "passed"
    passed["corruption_rejected"] = "passed"

    _rollback(dependencies, happy, proposal, applied)
    if search_knowledge("canonical rehearsal policy", _dependencies=knowledge_dependencies)["hits"]:
        raise RuntimeError("rolled-back claim remained active")
    passed["rollback_exclusion"] = "passed"

    recovery = root / "recovery"
    recovery_dependencies = _generate(recovery)
    proposal_path = recovery_dependencies.proposals_root / "recovery.json"
    recovery_proposal = prepare_promotion("candidate-answerable", proposal_path, dependencies=recovery_dependencies)
    authorization_path = recovery / "recovery-authorization.json"
    _write(authorization_path, _authorization(recovery_proposal, suffix="recovery"))

    def failpoint(name):
        if name == "after_authorized_intent":
            raise RuntimeError("public fixture interruption")

    crashing = _PromotionDependencies(
        recovery_dependencies.candidate_root,
        recovery_dependencies.brain_root,
        recovery_dependencies.promotions_root,
        lambda: FIXED_NOW,
        lambda: SOURCE_HEAD,
        failpoint,
    )
    try:
        apply_promotion(proposal_path, authorization_path, dependencies=crashing)
        raise RuntimeError("fixture interruption did not occur")
    except RuntimeError as exc:
        if str(exc) != "public fixture interruption":
            raise
    if recover_promotions(dependencies=recovery_dependencies)["status"] != "recovered":
        raise RuntimeError("fixture recovery failed")
    passed["recover_interrupted_apply"] = "passed"

    mixed = root / "mixed"
    mixed_dependencies, _, _ = _prepare_apply(mixed, "candidate-legacy", "legacy")
    answerable_path = mixed_dependencies.proposals_root / "answerable.json"
    answerable_proposal = prepare_promotion("candidate-answerable", answerable_path, dependencies=mixed_dependencies)
    answerable_authorization = mixed / "answerable-authorization.json"
    _write(answerable_authorization, _authorization(answerable_proposal, suffix="mixed"))
    apply_promotion(answerable_path, answerable_authorization, dependencies=mixed_dependencies)
    mixed_status = knowledge_status(_dependencies=_KnowledgeDependencies(mixed_dependencies.brain_root, None))
    if mixed_status["answerability_status"] != "mixed_version_partial":
        raise RuntimeError("mixed lineage was not explicit")
    passed["mixed_legacy_new"] = "passed"

    apply_race = root / "reader-apply"
    apply_dependencies = _generate(apply_race)
    apply_proposal_path = apply_dependencies.proposals_root / "apply.json"
    apply_proposal = prepare_promotion("candidate-answerable", apply_proposal_path, dependencies=apply_dependencies)
    apply_authorization = apply_race / "authorization.json"
    _write(apply_authorization, _authorization(apply_proposal, suffix="reader-apply"))
    results, _ = _parallel(
        lambda: knowledge_status(_dependencies=_KnowledgeDependencies(apply_dependencies.brain_root, None)),
        lambda: apply_promotion(apply_proposal_path, apply_authorization, dependencies=apply_dependencies),
    )
    if not results or inspect_promotion(apply_proposal["promotion_id"], dependencies=apply_dependencies)["status"] != "committed":
        raise RuntimeError("reader/apply race failed")
    passed["reader_vs_apply"] = "passed"

    rollback_race = root / "reader-rollback"
    rollback_dependencies, rollback_proposal_source, rollback_applied = _prepare_apply(rollback_race, suffix="reader-rollback")
    rollback_proposal = build_rollback_proposal(rollback_applied["promotion_id"], dependencies=rollback_dependencies)
    rollback_authorization = rollback_race / "rollback.json"
    _write(rollback_authorization, _authorization(rollback_proposal, "rollback", "reader-rollback"))
    _parallel(
        lambda: search_knowledge("canonical", _dependencies=_KnowledgeDependencies(rollback_dependencies.brain_root, None)),
        lambda: rollback_promotion(rollback_proposal_source["promotion_id"], rollback_authorization, dependencies=rollback_dependencies),
    )
    if inspect_promotion(rollback_applied["promotion_id"], dependencies=rollback_dependencies)["status"] != "rolled_back":
        raise RuntimeError("reader/rollback race failed")
    passed["reader_vs_rollback"] = "passed"

    recovery_race = root / "reader-recover"
    recovery_race_dependencies = _generate(recovery_race)
    recovery_race_proposal_path = recovery_race_dependencies.proposals_root / "proposal.json"
    recovery_race_proposal = prepare_promotion("candidate-answerable", recovery_race_proposal_path, dependencies=recovery_race_dependencies)
    recovery_race_authorization = recovery_race / "authorization.json"
    _write(recovery_race_authorization, _authorization(recovery_race_proposal, suffix="reader-recover"))
    crashing_race = _PromotionDependencies(
        recovery_race_dependencies.candidate_root,
        recovery_race_dependencies.brain_root,
        recovery_race_dependencies.promotions_root,
        lambda: FIXED_NOW,
        lambda: SOURCE_HEAD,
        failpoint,
    )
    try:
        apply_promotion(recovery_race_proposal_path, recovery_race_authorization, dependencies=crashing_race)
    except RuntimeError:
        pass
    _parallel(
        lambda: knowledge_status(_dependencies=_KnowledgeDependencies(recovery_race_dependencies.brain_root, None)),
        lambda: recover_promotions(dependencies=recovery_race_dependencies),
    )
    if inspect_promotion(recovery_race_proposal["promotion_id"], dependencies=recovery_race_dependencies)["status"] != "committed":
        raise RuntimeError("reader/recover race failed")
    passed["reader_vs_recover"] = "passed"

    reader_dependencies = _KnowledgeDependencies(mixed_dependencies.brain_root, None)
    reader_results, reader_errors = _parallel(
        lambda: search_knowledge("canonical", _dependencies=reader_dependencies),
        lambda: search_knowledge("canonical", _dependencies=reader_dependencies),
    )
    if reader_errors or len(reader_results) != 2 or reader_results[0] != reader_results[1]:
        raise RuntimeError("two-reader determinism failed")
    passed["two_readers"] = "passed"

    foreign = root / "foreign-writer"
    foreign_dependencies = _generate(foreign)
    foreign_proposal_path = foreign_dependencies.proposals_root / "proposal.json"
    foreign_proposal = prepare_promotion("candidate-answerable", foreign_proposal_path, dependencies=foreign_dependencies)
    foreign_authorization = foreign / "authorization.json"
    _write(foreign_authorization, _authorization(foreign_proposal, suffix="foreign"))
    generation_name = foreign_proposal["allowed_write_set"][0].split("/")[1]
    foreign_generation = foreign_dependencies.brain_root / "generations" / generation_name
    foreign_generation.mkdir(parents=True)
    marker = foreign_generation / "foreign.txt"
    marker.write_text("preserve", encoding="utf-8")
    try:
        apply_promotion(foreign_proposal_path, foreign_authorization, dependencies=foreign_dependencies)
        raise RuntimeError("foreign generation was accepted")
    except PromotionError:
        pass
    if marker.read_text(encoding="utf-8") != "preserve":
        raise RuntimeError("foreign writer state changed")
    passed["foreign_writer_preserved"] = "passed"

    fixture_check = subprocess.run(
        [sys.executable, str(GENERATOR), "--check", "--out", str(empty)],
        capture_output=True,
    )
    if fixture_check.returncode != 0:
        raise RuntimeError("fixture exact rerun failed")
    passed["exact_rerun"] = "passed"
    if tuple(passed) != EXPECTED_SCENARIOS:
        passed = {name: passed[name] for name in EXPECTED_SCENARIOS}
    return passed


def _validate_root(root):
    repository = REPOSITORY.resolve()
    runtime = repository.joinpath(".ao-lore").resolve()
    forbidden = {
        repository,
        repository.joinpath("brain").resolve(),
        repository.joinpath("working", "candidates").resolve(),
        runtime,
    }
    if root in forbidden or runtime not in root.parents:
        raise ValueError("unowned root")


def _validate_evidence(value, before_digest, after_digest):
    expected_keys = {
        "schema_version", "scenario_statuses", "scenario_count", "fixture_digest", "fixture_file_count",
        "canonical_brain_before_digest", "canonical_brain_after_digest", "canonical_brain_unchanged",
        "default_candidates_accessed", "default_brain_accessed", "live_authorization_used",
        "live_promotion_executed", "real_canonical_query_executed", "network_used", "credentials_used",
        "evidence_digest",
    }
    if type(value) is not dict or set(value) != expected_keys:
        return False
    if value["schema_version"] != "ao.lore.knowledge-rehearsal.v0.1":
        return False
    if value["canonical_brain_before_digest"] != before_digest or value["canonical_brain_after_digest"] != after_digest:
        return False
    if set(value["scenario_statuses"]) != set(EXPECTED_SCENARIOS):
        return False
    if any(status != "passed" for status in value["scenario_statuses"].values()):
        return False
    projection = {key: item for key, item in value.items() if key != "evidence_digest"}
    return value["evidence_digest"] == _canonical(projection)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--canonical-brain-before-digest", required=True)
    parser.add_argument("--canonical-brain-after-digest", required=True)
    arguments = parser.parse_args()
    try:
        root = Path(os.path.abspath(arguments.root))
        _validate_root(root)
        before_digest = arguments.canonical_brain_before_digest
        after_digest = arguments.canonical_brain_after_digest
        if not DIGEST_RE.fullmatch(before_digest) or not DIGEST_RE.fullmatch(after_digest) or before_digest != after_digest:
            raise ValueError("external inventory drift")
        evidence_path = root / "rehearsal-evidence.json"
        if arguments.check:
            value = json.loads(evidence_path.read_text(encoding="utf-8"))
            if not _validate_evidence(value, before_digest, after_digest):
                return 1
            fixture = root / "empty"
            checked = subprocess.run([sys.executable, str(GENERATOR), "--check", "--out", str(fixture)], capture_output=True)
            if checked.returncode != 0:
                return 1
            sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            return 0
        if root.exists():
            raise ValueError("root exists")
        root.mkdir(parents=True)
        statuses = _rehearse(root)
        fixture_manifest = json.loads((root / "empty" / "fixture-manifest.json").read_text(encoding="utf-8"))
        evidence = {
            "schema_version": "ao.lore.knowledge-rehearsal.v0.1",
            "scenario_statuses": {name: statuses[name] for name in sorted(statuses)},
            "scenario_count": len(statuses),
            "fixture_digest": fixture_manifest["fixture_digest"],
            "fixture_file_count": fixture_manifest["file_count"],
            "canonical_brain_before_digest": before_digest,
            "canonical_brain_after_digest": after_digest,
            "canonical_brain_unchanged": True,
            "default_candidates_accessed": False,
            "default_brain_accessed": False,
            "live_authorization_used": False,
            "live_promotion_executed": False,
            "real_canonical_query_executed": False,
            "network_used": False,
            "credentials_used": False,
        }
        evidence["evidence_digest"] = _canonical(evidence)
        evidence_path.write_text(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        sys.stdout.write(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError, RuntimeError):
        sys.stderr.write(REJECTION)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
