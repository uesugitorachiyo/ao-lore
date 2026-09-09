import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ao_lore.promotion import PromotionError, _PromotionDependencies, apply_promotion, build_rollback_proposal, inspect_promotion, prepare_promotion, recover_promotions, rollback_promotion, validate_audit_chain
from tests.test_ao_lore_promotion_authorization import authorization
from tests.test_ao_lore_promotion_prepare import FIXED_NOW, SOURCE_HEAD, build_fixture


ROOT = Path(__file__).resolve().parents[1]


class PromotionRollbackTests(unittest.TestCase):
    def test_mixed_version_lineage_inspects_and_rolls_back_without_payload_rewrite(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            legacy_id, candidates, brain, runtime = build_fixture(root)
            deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            legacy_path = deps.proposals_root / "legacy.json"
            legacy = prepare_promotion(legacy_id, legacy_path, dependencies=deps)
            legacy_auth = root / "apply-legacy.json"
            legacy_auth.write_text(json.dumps(authorization(legacy), sort_keys=True) + "\n")
            legacy_apply = apply_promotion(legacy_path, legacy_auth, dependencies=deps)

            donor = root / "donor"
            candidate_id, donor_candidates, _, _ = build_fixture(donor, answerable=True, candidate_id="candidate-answerable-fixture")
            shutil.copytree(donor_candidates / candidate_id, candidates / candidate_id)
            proposal_path = deps.proposals_root / "answerable.json"
            proposal = prepare_promotion(candidate_id, proposal_path, dependencies=deps)
            apply_auth = root / "apply-answerable.json"
            answerable_auth = authorization(proposal)
            answerable_auth.update({"authorization_id": "authorization-answerable-apply", "nonce": "nonce-answerable-apply", "expected_prior_generation_id": proposal["prior_generation_id"]})
            apply_auth.write_text(json.dumps(answerable_auth, sort_keys=True) + "\n")
            applied = apply_promotion(proposal_path, apply_auth, dependencies=deps)
            self.assertEqual("committed", inspect_promotion(legacy_apply["promotion_id"], dependencies=deps)["status"])
            self.assertEqual("committed", inspect_promotion(applied["promotion_id"], dependencies=deps)["status"])
            rollback = build_rollback_proposal(applied["promotion_id"], dependencies=deps)
            rollback_auth = authorization(proposal)
            rollback_auth.update({"operation": "rollback", "authorization_id": "authorization-answerable-rollback", "nonce": "nonce-answerable-rollback", "proposal_digest": rollback["proposal_digest"], "expected_brain_inventory_digest": rollback["current_brain_inventory_digest"], "expected_prior_generation_id": rollback["current_generation_id"], "expected_result_generation_id": rollback["expected_generation_id"], "allowed_write_set_digest": rollback["allowed_write_set_digest"]})
            rollback_path = root / "rollback-answerable.json"
            rollback_path.write_text(json.dumps(rollback_auth, sort_keys=True) + "\n")
            rollback_promotion(applied["promotion_id"], rollback_path, dependencies=deps)
            stored = json.loads(next((brain / "generations").glob("000002-*/entries/*.json")).read_text())
            self.assertEqual(proposal["canonical_entry"], stored)
            self.assertEqual("rolled_back", inspect_promotion(applied["promotion_id"], dependencies=deps)["status"])
            self.assertEqual("ao.lore.okf-canonical-entry.v0.2", legacy["canonical_entry"]["schema_version"])
            self.assertEqual("ao.lore.okf-canonical-entry.v0.3", stored["schema_version"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        root = Path(self.temp.name); candidate_id, candidates, brain, runtime = build_fixture(root)
        self.deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
        proposal_path = self.deps.proposals_root / "proposal.json"; proposal = prepare_promotion(candidate_id, proposal_path, dependencies=self.deps)
        auth = authorization(proposal); auth_path = root / "apply-authorization.json"; auth_path.write_text(json.dumps(auth, sort_keys=True) + "\n")
        self.apply = apply_promotion(proposal_path, auth_path, dependencies=self.deps)
        self.rollback_proposal = build_rollback_proposal(proposal["promotion_id"], dependencies=self.deps)
        rollback_auth = authorization(proposal)
        rollback_auth.update({
            "operation": "rollback", "authorization_id": "authorization-rollback-fixture", "nonce": "nonce-rollback-fixture",
            "proposal_digest": self.rollback_proposal["proposal_digest"],
            "expected_brain_inventory_digest": self.rollback_proposal["current_brain_inventory_digest"],
            "expected_prior_generation_id": self.rollback_proposal["current_generation_id"],
            "expected_result_generation_id": self.rollback_proposal["expected_generation_id"],
            "allowed_write_set_digest": self.rollback_proposal["allowed_write_set_digest"],
        })
        self.auth_path = root / "rollback-authorization.json"; self.auth_path.write_text(json.dumps(rollback_auth, sort_keys=True) + "\n")

    def tearDown(self): self.temp.cleanup()

    def test_rollback_appends_empty_generation_and_preserves_all_history(self):
        before_generations = sorted((self.deps.brain_root / "generations").iterdir())
        before_consumed = list((self.deps.promotions_root / "consumed").glob("*.json"))
        result = rollback_promotion(self.apply["promotion_id"], self.auth_path, dependencies=self.deps)
        self.assertEqual("rolled_back", result["status"])
        generations = sorted((self.deps.brain_root / "generations").iterdir())
        self.assertEqual(2, len(generations)); self.assertTrue(before_generations[0].exists())
        self.assertEqual([], list((generations[-1] / "entries").iterdir()))
        manifest = json.loads((generations[-1] / "manifest.json").read_text())
        self.assertEqual("rollback", manifest["operation"]); self.assertEqual("restore", manifest["transition"]["kind"])
        self.assertGreater(len(list((self.deps.promotions_root / "consumed").glob("*.json"))), len(before_consumed))
        self.assertEqual(result, rollback_promotion(self.apply["promotion_id"], self.auth_path, dependencies=self.deps))
        self.assertEqual("rolled_back", inspect_promotion(self.apply["promotion_id"], dependencies=self.deps)["status"])

    def test_second_distinct_rollback_and_wrong_generation_are_rejected(self):
        rollback_promotion(self.apply["promotion_id"], self.auth_path, dependencies=self.deps)
        other = json.loads(self.auth_path.read_text()); other["authorization_id"] = "authorization-second"; other["nonce"] = "nonce-second"
        second = self.auth_path.with_name("second.json"); second.write_text(json.dumps(other, sort_keys=True) + "\n")
        with self.assertRaises(PromotionError): rollback_promotion(self.apply["promotion_id"], second, dependencies=self.deps)

    def test_stable_identity_rollback_rejection_is_audited(self):
        expired = json.loads(self.auth_path.read_text()); expired["expires_at"] = "2026-08-11T11:59:59Z"
        self.auth_path.write_text(json.dumps(expired, sort_keys=True) + "\n")
        with self.assertRaises(PromotionError): rollback_promotion(self.apply["promotion_id"], self.auth_path, dependencies=self.deps)
        self.assertEqual("rollback_rejected", validate_audit_chain(self.deps)[-1]["event_type"])

    def test_rollback_crash_boundaries_recover_exactly(self):
        for boundary in ("rollback_after_reservation", "rollback_after_authorized_intent", "rollback_after_staging", "rollback_after_generation_publish", "rollback_after_audit", "rollback_after_consumption_commit", "rollback_after_transaction", "rollback_after_terminal_recovery"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
                root = Path(name); candidate_id, candidates, brain, runtime = build_fixture(root)
                base = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
                proposal_path = base.proposals_root / "proposal.json"; proposal = prepare_promotion(candidate_id, proposal_path, dependencies=base)
                apply_auth = root / "apply.json"; apply_auth.write_text(json.dumps(authorization(proposal), sort_keys=True) + "\n")
                applied = apply_promotion(proposal_path, apply_auth, dependencies=base)
                rollback = build_rollback_proposal(applied["promotion_id"], dependencies=base)
                rollback_auth = authorization(proposal); rollback_auth.update({"operation": "rollback", "authorization_id": "authorization-rollback-fixture", "nonce": "nonce-rollback-fixture", "proposal_digest": rollback["proposal_digest"], "expected_brain_inventory_digest": rollback["current_brain_inventory_digest"], "expected_prior_generation_id": rollback["current_generation_id"], "expected_result_generation_id": rollback["expected_generation_id"], "allowed_write_set_digest": rollback["allowed_write_set_digest"]})
                auth_path = root / "rollback.json"; auth_path.write_text(json.dumps(rollback_auth, sort_keys=True) + "\n")
                def failpoint(value):
                    if value == boundary: raise RuntimeError("fixture process loss")
                crashing = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD, failpoint)
                with self.assertRaises(RuntimeError): rollback_promotion(applied["promotion_id"], auth_path, dependencies=crashing)
                result = recover_promotions(dependencies=base)
                self.assertEqual("no_op" if boundary == "rollback_after_terminal_recovery" else "recovered", result["status"])
                self.assertEqual("rolled_back", inspect_promotion(applied["promotion_id"], dependencies=base)["status"])


if __name__ == "__main__": unittest.main()
