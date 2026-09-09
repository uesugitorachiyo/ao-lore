import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from ao_lore.promotion import _PromotionDependencies, apply_promotion, inspect_promotion, prepare_promotion, recover_promotions, reserve_authorization, validate_authorization
from tests.test_ao_lore_promotion_authorization import authorization
from tests.test_ao_lore_promotion_prepare import FIXED_NOW, SOURCE_HEAD, build_fixture


ROOT = Path(__file__).resolve().parents[1]


class PromotionRecoveryTests(unittest.TestCase):
    def fixture(self, fail_at=None, *, answerable=False, evidence=False):
        temp = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        root = Path(temp.name); candidate_id, candidates, brain, runtime = build_fixture(root, answerable=answerable, evidence=evidence)
        def failpoint(name):
            if name == fail_at: raise RuntimeError("fixture process loss")
        deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD, failpoint)
        proposal_path = deps.proposals_root / "proposal.json"; proposal = prepare_promotion(candidate_id, proposal_path, dependencies=deps)
        auth_path = root / "authorization.json"; auth_path.write_text(json.dumps(authorization(proposal), sort_keys=True) + "\n")
        return temp, deps, proposal_path, auth_path

    def test_no_pending_is_no_op(self):
        temp, deps, _, _ = self.fixture()
        try: self.assertEqual("no_op", recover_promotions(dependencies=deps)["status"])
        finally: temp.cleanup()

    def test_authorized_intent_resumes_exact_transaction(self):
        temp, deps, proposal, auth = self.fixture("after_authorized_intent")
        try:
            with self.assertRaises(RuntimeError): apply_promotion(proposal, auth, dependencies=deps)
            promotion_id = json.loads(proposal.read_text())["promotion_id"]
            inspected = inspect_promotion(promotion_id, dependencies=deps)
            self.assertEqual(("recovery_pending", "promotion recover"), (inspected["status"], inspected["next_command"]))
            deps = _PromotionDependencies(deps.candidate_root, deps.brain_root, deps.promotions_root, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            result = recover_promotions(dependencies=deps)
            self.assertEqual("recovered", result["status"])
            self.assertEqual("no_op", recover_promotions(dependencies=deps)["status"])
        finally: temp.cleanup()

    def test_answerable_recovery_preserves_exact_v0_3_payload(self):
        temp, crashing, proposal_path, auth_path = self.fixture("after_authorized_intent", answerable=True)
        try:
            expected = json.loads(proposal_path.read_text())["canonical_entry"]
            with self.assertRaises(RuntimeError):
                apply_promotion(proposal_path, auth_path, dependencies=crashing)
            resumed = _PromotionDependencies(crashing.candidate_root, crashing.brain_root, crashing.promotions_root, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            self.assertEqual("recovered", recover_promotions(dependencies=resumed)["status"])
            stored = json.loads(next((resumed.brain_root / "generations").glob("*/entries/*.json")).read_text())
            self.assertEqual(expected, stored)
            self.assertEqual("committed", inspect_promotion(json.loads(proposal_path.read_text())["promotion_id"], dependencies=resumed)["status"])
        finally:
            temp.cleanup()

    def test_evidence_recovery_preserves_exact_v0_4_payload(self):
        temp, crashing, proposal_path, auth_path = self.fixture("after_authorized_intent", evidence=True)
        try:
            expected = json.loads(proposal_path.read_text())["canonical_entry"]
            with self.assertRaises(RuntimeError):
                apply_promotion(proposal_path, auth_path, dependencies=crashing)
            resumed = _PromotionDependencies(crashing.candidate_root, crashing.brain_root, crashing.promotions_root, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            self.assertEqual("recovered", recover_promotions(dependencies=resumed)["status"])
            stored = json.loads(next((resumed.brain_root / "generations").glob("*/entries/*.json")).read_text())
            self.assertEqual(expected, stored)
            self.assertEqual("ao.lore.okf-canonical-entry.v0.4", stored["schema_version"])
        finally:
            temp.cleanup()

    def test_recovery_conflict_matrix_has_stable_reason_codes(self):
        cases = ("proposal_evidence_conflict", "reservation_evidence_conflict", "retained_authorization_missing", "recovery_binding_conflict")
        for reason in cases:
            with self.subTest(reason=reason):
                temp, deps, proposal, auth = self.fixture("after_authorized_intent")
                try:
                    with self.assertRaises(RuntimeError): apply_promotion(proposal, auth, dependencies=deps)
                    if reason == "proposal_evidence_conflict":
                        proposal.unlink()
                    elif reason == "reservation_evidence_conflict":
                        next((deps.promotions_root / "consumed").glob("*-reserved.json")).unlink()
                    elif reason == "retained_authorization_missing":
                        next((deps.promotions_root / "retained").glob("*.json")).unlink()
                    else:
                        candidate = next(deps.candidate_root.glob("candidate-*/candidate.json"))
                        candidate.write_text("{}\n")
                    result = recover_promotions(dependencies=deps)
                    self.assertEqual(("investigate", reason), (result["status"], result["reason_code"]))
                finally: temp.cleanup()

    def test_two_unrepresented_reservations_are_ambiguous(self):
        temp, deps, proposal_path, auth_path = self.fixture()
        try:
            proposal = json.loads(proposal_path.read_text())
            first, first_digest = validate_authorization(auth_path, proposal, "apply", dependencies=deps)
            reserve_authorization(first, first_digest, proposal, dependencies=deps)
            second = authorization(proposal)
            second.update({"authorization_id": "authorization-second-fixture", "nonce": "nonce-second-fixture"})
            second_path = auth_path.with_name("authorization-second.json")
            second_path.write_text(json.dumps(second, sort_keys=True) + "\n")
            second, second_digest = validate_authorization(second_path, proposal, "apply", dependencies=deps)
            reserve_authorization(second, second_digest, proposal, dependencies=deps)
            result = recover_promotions(dependencies=deps)
            self.assertEqual(("investigate", "ambiguous_pending_recovery"), (result["status"], result["reason_code"]))
        finally: temp.cleanup()

    def test_public_apply_and_recover_contend_without_corruption(self):
        temp, crashing, proposal, auth = self.fixture("after_authorized_intent")
        try:
            with self.assertRaises(RuntimeError): apply_promotion(proposal, auth, dependencies=crashing)
            deps = _PromotionDependencies(crashing.candidate_root, crashing.brain_root, crashing.promotions_root, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            barrier = threading.Barrier(3); results = []; errors = []
            def run(operation):
                barrier.wait()
                try: results.append(operation())
                except Exception as exc: errors.append(exc)
            threads = [
                threading.Thread(target=run, args=(lambda: apply_promotion(proposal, auth, dependencies=deps),)),
                threading.Thread(target=run, args=(lambda: recover_promotions(dependencies=deps),)),
            ]
            for thread in threads: thread.start()
            barrier.wait()
            for thread in threads: thread.join(2)
            self.assertEqual([], errors); self.assertEqual(2, len(results))
            promotion_id = json.loads(proposal.read_text())["promotion_id"]
            self.assertEqual("committed", inspect_promotion(promotion_id, dependencies=deps)["status"])
            self.assertEqual(1, len(list((deps.brain_root / "generations").iterdir())))
        finally: temp.cleanup()

    def test_reservation_only_is_discovered_and_inspection_routes_recover(self):
        temp, deps, proposal, auth = self.fixture("after_reservation")
        try:
            with self.assertRaises(RuntimeError): apply_promotion(proposal, auth, dependencies=deps)
            proposal_value = json.loads(proposal.read_text())
            inspected = inspect_promotion(proposal_value["promotion_id"], dependencies=deps)
            self.assertEqual(("recovery_pending", "promotion recover"), (inspected["status"], inspected["next_command"]))
            resumed = _PromotionDependencies(deps.candidate_root, deps.brain_root, deps.promotions_root, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            self.assertEqual("recovered", recover_promotions(dependencies=resumed)["status"])
        finally: temp.cleanup()

    def test_every_nonterminal_apply_boundary_resumes_or_completes(self):
        for boundary in ("after_staging", "after_generation_publish", "after_audit", "after_consumption_commit", "after_transaction"):
            with self.subTest(boundary=boundary):
                temp, deps, proposal, auth = self.fixture(boundary)
                try:
                    with self.assertRaises(RuntimeError): apply_promotion(proposal, auth, dependencies=deps)
                    resumed = _PromotionDependencies(deps.candidate_root, deps.brain_root, deps.promotions_root, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
                    result = recover_promotions(dependencies=resumed)
                    self.assertEqual("recovered", result["status"])
                    self.assertEqual("no_op", recover_promotions(dependencies=resumed)["status"])
                    self.assertEqual(1, len(list((deps.brain_root / "generations").iterdir())))
                finally: temp.cleanup()

    def test_foreign_or_unowned_staging_is_preserved_for_investigation(self):
        temp, deps, proposal, auth = self.fixture("after_authorized_intent")
        try:
            with self.assertRaises(RuntimeError): apply_promotion(proposal, auth, dependencies=deps)
            match = re.fullmatch(r"(transaction-[0-9a-f]{32})-[0-9]{6}-[0-9a-f]{64}\.json", next((deps.promotions_root / "recovery").iterdir()).name)
            self.assertIsNotNone(match)
            transaction_id = match.group(1)
            stage = deps.promotions_root / "staging" / transaction_id; stage.mkdir(parents=True)
            (stage / "foreign.txt").write_text("preserve")
            result = recover_promotions(dependencies=deps)
            self.assertEqual("investigate", result["status"])
            self.assertEqual("preserve", (stage / "foreign.txt").read_text())
        finally: temp.cleanup()


if __name__ == "__main__": unittest.main()
