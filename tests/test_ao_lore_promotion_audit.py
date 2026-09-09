import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore.promotion import (
    PromotionError,
    _PromotionDependencies,
    apply_promotion,
    inspect_promotion,
    prepare_promotion,
    validate_audit_chain,
)
from tests.test_ao_lore_promotion_authorization import authorization
from tests.test_ao_lore_promotion_prepare import FIXED_NOW, SOURCE_HEAD, build_fixture


ROOT = Path(__file__).resolve().parents[1]


class PromotionAuditTests(unittest.TestCase):
    def test_v0_4_commit_is_reopened_by_audit_and_inspection(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            candidate_id, candidates, brain, runtime = build_fixture(root, evidence=True)
            deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            proposal_path = runtime / "proposals" / "evidence.json"
            proposal = prepare_promotion(candidate_id, proposal_path, dependencies=deps)
            auth_path = root / "authorization-evidence.json"
            auth_path.write_text(json.dumps(authorization(proposal), sort_keys=True) + "\n")
            apply_promotion(proposal_path, auth_path, dependencies=deps)
            self.assertEqual("apply_committed", validate_audit_chain(deps)[-1]["event_type"])
            inspected = inspect_promotion(proposal["promotion_id"], dependencies=deps)
            self.assertEqual("committed", inspected["status"])
            self.assertTrue(inspected["canonical_state_valid"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        root = Path(self.temp.name)
        candidate_id, candidates, brain, runtime = build_fixture(root)
        self.dependencies = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
        self.proposal_path = self.dependencies.proposals_root / "proposal.json"
        self.proposal = prepare_promotion(candidate_id, self.proposal_path, dependencies=self.dependencies)
        self.authorization = authorization(self.proposal)
        self.authorization_path = root / "authorization.json"
        self.authorization_path.write_text(json.dumps(self.authorization, sort_keys=True) + "\n")

    def tearDown(self):
        self.temp.cleanup()

    def audit_paths(self):
        return sorted((self.dependencies.promotions_root / "audit").glob("*.json"))

    def test_full_chain_and_committed_inspection_are_independently_verified(self):
        prepared = inspect_promotion(self.proposal["promotion_id"], dependencies=self.dependencies)
        self.assertEqual("prepared", prepared["status"])
        schema = json.loads((ROOT / "schemas" / "ao-lore" / "promotion-inspection-v0.1.schema.json").read_text())
        Draft202012Validator(schema).validate(prepared)
        result = apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        events = validate_audit_chain(self.dependencies)
        self.assertEqual(["prepare_started", "prepare_succeeded", "apply_committed"], [event["event_type"] for event in events])
        inspected = inspect_promotion(self.proposal["promotion_id"], dependencies=self.dependencies)
        Draft202012Validator(schema).validate(inspected)
        self.assertEqual("committed", inspected["status"])
        self.assertEqual(result["transaction_id"], inspected["transaction_id"])
        self.assertTrue(inspected["canonical_state_valid"])
        self.assertEqual("promotion rollback", inspected["next_command"])

    def test_omission_insertion_reorder_replacement_truncation_and_replay_fail(self):
        apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        originals = [(path.name, path.read_bytes()) for path in self.audit_paths()]
        mutations = ("omission", "insertion", "reorder", "replacement", "truncation", "replay")
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                for path in self.audit_paths(): path.unlink()
                for name, body in originals: (self.dependencies.promotions_root / "audit" / name).write_bytes(body)
                paths = self.audit_paths()
                if mutation == "omission": paths[1].unlink()
                elif mutation == "insertion": pass
                elif mutation == "reorder":
                    first = json.loads(paths[0].read_text()); first["sequence"] = 2; paths[0].write_text(json.dumps(first))
                elif mutation == "replacement": paths[1].write_text('{}\n')
                elif mutation == "truncation": paths[-1].write_bytes(paths[-1].read_bytes()[:20])
                else: paths[-1].write_bytes(paths[0].read_bytes())
                if mutation == "insertion":
                    (paths[0].parent / ("000000-" + "0" * 64 + ".json")).write_text('{}\n')
                with self.assertRaises(PromotionError): validate_audit_chain(self.dependencies)

    def test_semantic_transition_and_filename_digest_fail_closed(self):
        path = self.audit_paths()[1]
        event = json.loads(path.read_text()); event["event_type"] = "rollback_committed"
        path.write_text(json.dumps(event))
        with self.assertRaises(PromotionError): validate_audit_chain(self.dependencies)

    def test_inspection_rejects_transaction_consumption_generation_and_entry_drift(self):
        result = apply_promotion(self.proposal_path, self.authorization_path, dependencies=self.dependencies)
        targets = [
            next((self.dependencies.promotions_root / "transactions").glob("*.json")),
            next((self.dependencies.promotions_root / "consumed").glob("*-committed.json")),
            next((self.dependencies.brain_root / "generations").glob("*/manifest.json")),
            next((self.dependencies.brain_root / "generations").glob("*/entries/*.json")),
        ]
        for target in targets:
            with self.subTest(target=target.name):
                body = target.read_bytes(); target.write_text('{}\n')
                with self.assertRaises(PromotionError): inspect_promotion(self.proposal["promotion_id"], dependencies=self.dependencies)
                target.write_bytes(body)

    def test_inspection_detects_concurrent_snapshot_and_never_leaks_inputs(self):
        def transition(name):
            if name == "inspect_snapshot":
                (self.dependencies.brain_root / "foreign-secret.txt").write_text("PRIVATE-CONTENT")
        deps = _PromotionDependencies(self.dependencies.candidate_root, self.dependencies.brain_root, self.dependencies.promotions_root, lambda: FIXED_NOW, lambda: SOURCE_HEAD, transition)
        result = inspect_promotion(self.proposal["promotion_id"], dependencies=deps)
        self.assertEqual("recovery_pending", result["status"])
        self.assertEqual("investigate", result["recovery_classification"])
        secret = self.dependencies.promotions_root / "audit" / "PRIVATE-HOST-user-path.json"
        secret.write_text("PRIVATE-CONTENT")
        with self.assertRaises(PromotionError) as caught: validate_audit_chain(self.dependencies)
        rendered = str(caught.exception)
        self.assertNotIn("PRIVATE-HOST", rendered); self.assertNotIn("PRIVATE-CONTENT", rendered); self.assertNotIn(str(self.dependencies.promotions_root), rendered)


if __name__ == "__main__":
    unittest.main()
