import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore.promotion import PromotionError, _PromotionDependencies, apply_promotion, prepare_promotion, validate_audit_chain
from tests.test_ao_lore_promotion_authorization import authorization
from tests.test_ao_lore_promotion_prepare import FIXED_NOW, SOURCE_HEAD, build_fixture, canonical


ROOT = Path(__file__).resolve().parents[1]


class PromotionApplyTests(unittest.TestCase):
    def test_private_candidate_loader_is_used_for_prepare_apply_and_retry(self):
        from ao_lore.promotion import _load_candidate

        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            candidate_id, candidates, brain, runtime = build_fixture(
                root, evidence=True,
            )
            calls = []

            def sealed_loader(selected_id):
                calls.append(selected_id)
                return _load_candidate(selected_id, candidates)

            deps = _PromotionDependencies(
                candidates,
                brain,
                runtime,
                lambda: FIXED_NOW,
                lambda: SOURCE_HEAD,
                candidate_loader=sealed_loader,
            )
            proposal_path = runtime / "proposals" / "sealed.json"
            with patch(
                "ao_lore.promotion._load_candidate",
                side_effect=AssertionError("path candidate loader used"),
            ):
                proposal = prepare_promotion(
                    candidate_id, proposal_path, dependencies=deps,
                )
                authorization_path = root / "authorization-sealed.json"
                authorization_path.write_text(
                    json.dumps(authorization(proposal), sort_keys=True) + "\n"
                )
                first = apply_promotion(
                    proposal_path, authorization_path, dependencies=deps,
                )
                second = apply_promotion(
                    proposal_path, authorization_path, dependencies=deps,
                )

            self.assertEqual(first, second)
            self.assertEqual([candidate_id, candidate_id], calls)

    def test_v0_4_apply_persists_exact_entry_and_is_exactly_retry_safe(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            candidate_id, candidates, brain, runtime = build_fixture(root, evidence=True)
            deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            proposal_path = runtime / "proposals" / "evidence.json"
            proposal = prepare_promotion(candidate_id, proposal_path, dependencies=deps)
            auth_path = root / "authorization-evidence.json"
            auth_path.write_text(json.dumps(authorization(proposal), sort_keys=True) + "\n")
            first = apply_promotion(proposal_path, auth_path, dependencies=deps)
            second = apply_promotion(proposal_path, auth_path, dependencies=deps)
            self.assertEqual(first, second)
            stored = json.loads(next((brain / "generations").glob("*/entries/*.json")).read_text())
            self.assertEqual(proposal["canonical_entry"], stored)
            self.assertEqual("ao.lore.okf-canonical-entry.v0.4", stored["schema_version"])

    def test_v0_2_apply_rejects_expired_or_unsupported_proposal(self):
        for case in ("expired", "unsupported"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
                root = Path(name)
                candidate_id, candidates, brain, runtime = build_fixture(root, evidence=True)
                deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
                proposal_path = runtime / "proposals" / "evidence.json"
                proposal = prepare_promotion(candidate_id, proposal_path, dependencies=deps)
                if case == "expired":
                    proposal["prepared_at"] = "2026-08-11T11:54:59Z"
                    proposal["expires_at"] = "2026-08-11T11:59:59Z"
                else:
                    proposal["schema_version"] = "ao.lore.promotion-proposal.v9.9"
                proposal["proposal_digest"] = canonical({k: v for k, v in proposal.items() if k != "proposal_digest"})
                proposal_path.write_text(json.dumps(proposal, sort_keys=True) + "\n")
                value = authorization(proposal)
                auth_path = root / "authorization-evidence.json"
                auth_path.write_text(json.dumps(value, sort_keys=True) + "\n")
                with self.assertRaises(PromotionError):
                    apply_promotion(proposal_path, auth_path, dependencies=deps)

    def test_answerable_apply_publishes_exact_v0_3_entry(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            candidate_id, candidates, brain, runtime = build_fixture(root, answerable=True)
            deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            proposal_path = runtime / "proposals" / "answerable.json"
            proposal = prepare_promotion(candidate_id, proposal_path, dependencies=deps)
            auth_path = root / "authorization-answerable.json"
            auth_path.write_text(json.dumps(authorization(proposal), sort_keys=True) + "\n")
            apply_promotion(proposal_path, auth_path, dependencies=deps)
            entry_path = next((brain / "generations").glob("*/entries/*.json"))
            self.assertEqual(proposal["canonical_entry"], json.loads(entry_path.read_text()))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        root = Path(self.temp.name)
        candidate_id, candidates, brain, runtime = build_fixture(root)
        self.fail_at = None
        self.boundaries = []
        def failpoint(name):
            self.boundaries.append(name)
            if name == self.fail_at:
                raise RuntimeError("fixture process loss")
        self.dependencies = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD, failpoint)
        self.proposal_path = self.dependencies.proposals_root / "proposal.json"
        self.proposal = prepare_promotion(candidate_id, self.proposal_path, dependencies=self.dependencies)
        self.authorization = authorization(self.proposal)
        self.authorization_path = root / "authorization.json"
        self.authorization_path.write_text(json.dumps(self.authorization, sort_keys=True) + "\n")
        self.candidate_before = {p.relative_to(candidates).as_posix(): p.read_bytes() for p in candidates.rglob("*") if p.is_file()}
        self.readme_before = (brain / "README.md").read_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def test_apply_commits_one_additive_generation_and_all_terminal_evidence(self):
        result = apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        self.assertEqual("committed", result["status"])
        generation = next((self.dependencies.brain_root / "generations").iterdir())
        self.assertEqual({"manifest.json", "entries"}, {p.name for p in generation.iterdir()})
        self.assertEqual(1, len(list((generation / "entries").glob("*.json"))))
        self.assertEqual(self.readme_before, (self.dependencies.brain_root / "README.md").read_bytes())
        after = {p.relative_to(self.dependencies.candidate_root).as_posix(): p.read_bytes() for p in self.dependencies.candidate_root.rglob("*") if p.is_file()}
        self.assertEqual(self.candidate_before, after)
        self.assertTrue(list((self.dependencies.promotions_root / "transactions").glob("*.json")))
        self.assertEqual(2, len(list((self.dependencies.promotions_root / "consumed").glob("*.json"))))
        phases = [json.loads(p.read_text())["phase"] for p in sorted((self.dependencies.promotions_root / "recovery").glob("*.json"))]
        self.assertEqual(["authorized", "staged", "generation_published", "terminal"], phases)
        for boundary in ("after_reservation", "after_authorized_intent", "after_staging", "after_generation_publish", "after_audit", "after_consumption_commit", "after_transaction", "after_terminal_recovery"):
            self.assertIn(boundary, self.boundaries)

    def test_exact_retry_returns_same_result_and_semantic_drift_rejects(self):
        first = apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        second = apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        self.assertEqual(first, second)
        changed = copy.deepcopy(self.authorization); changed["nonce"] = "nonce-drift"
        self.authorization_path.write_text(json.dumps(changed, sort_keys=True) + "\n")
        with self.assertRaises(PromotionError):
            apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)

    def test_failure_after_reservation_never_publishes_generation(self):
        self.fail_at = "after_authorized_intent"
        with self.assertRaisesRegex(RuntimeError, "process loss"):
            apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        self.assertFalse((self.dependencies.brain_root / "generations").exists())
        self.assertTrue(list((self.dependencies.promotions_root / "consumed").glob("*-reserved.json")))
        self.assertFalse(list((self.dependencies.promotions_root / "transactions").glob("*.json")))

    def test_stable_identity_authorization_rejection_is_audited(self):
        expired = copy.deepcopy(self.authorization)
        expired["expires_at"] = "2026-08-11T11:59:59Z"
        self.authorization_path.write_text(json.dumps(expired, sort_keys=True) + "\n")
        with self.assertRaises(PromotionError):
            apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        events = validate_audit_chain(self.dependencies)
        self.assertEqual("apply_rejected", events[-1]["event_type"])
        self.assertEqual("authorization_rejected", events[-1]["reason_code"])

    def test_stale_candidate_fails_closed(self):
        candidate = self.dependencies.candidate_root / self.proposal["candidate_id"] / "candidate.json"
        candidate.write_text("{}\n")
        with self.assertRaises(PromotionError):
            apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        self.assertEqual("candidate_drift", validate_audit_chain(self.dependencies)[-1]["reason_code"])

    def test_source_and_brain_drift_have_stable_rejection_audits(self):
        stale_source = _PromotionDependencies(self.dependencies.candidate_root, self.dependencies.brain_root, self.dependencies.promotions_root, lambda: FIXED_NOW, lambda: "2" * 40)
        with self.assertRaises(PromotionError): apply_promotion(self.proposal_path, self.authorization_path, dependencies=stale_source)
        self.assertEqual("source_head_drift", validate_audit_chain(self.dependencies)[-1]["reason_code"])
        (self.dependencies.brain_root / "README.md").write_text("drift\n")
        with self.assertRaises(PromotionError): apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        self.assertEqual("brain_drift", validate_audit_chain(self.dependencies)[-1]["reason_code"])

    def test_foreign_destination_fails_closed(self):
        generation_name = self.proposal["allowed_write_set"][0].split("/")[1]
        foreign = self.dependencies.brain_root / "generations" / generation_name
        foreign.mkdir(parents=True)
        (foreign / "foreign.txt").write_text("preserve")
        with self.assertRaises(PromotionError):
            apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        self.assertEqual("preserve", (foreign / "foreign.txt").read_text())


if __name__ == "__main__":
    unittest.main()
