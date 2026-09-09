import copy
import json
import tempfile
import unittest
from pathlib import Path

from ao_lore.promotion import (
    PromotionError,
    _PromotionDependencies,
    commit_authorization_consumption,
    reserve_authorization,
    validate_authorization,
)
from tests.test_ao_lore_promotion_prepare import FIXED_NOW, SOURCE_HEAD, build_fixture


ROOT = Path(__file__).resolve().parents[1]


def authorization(proposal):
    return {
        "schema_version": "ao.lore.promotion-authorization.v0.1",
        "policy_version": "ao.lore.promotion-policy.v0.1",
        "operation": "apply",
        "authorization_id": "authorization-public-fixture",
        "nonce": "nonce-public-fixture",
        "operator_id": "opaque-public-fixture",
        "authentication_scope": "local_operator_assertion",
        "proposal_digest": proposal["proposal_digest"],
        "promotion_id": proposal["promotion_id"],
        "candidate_digest": proposal["candidate_digest"],
        "provenance_digest": proposal["provenance_digest"],
        "accepted_review_head_digest": proposal["accepted_review_head_digest"],
        "canonical_entry_digest": proposal["canonical_entry_digest"],
        "expected_brain_inventory_digest": proposal["prior_brain_inventory_digest"],
        "expected_prior_generation_id": None,
        "expected_result_generation_id": proposal["expected_generation_id"],
        "allowed_write_set_digest": proposal["allowed_write_set_digest"],
        "source_head": proposal["source_head"],
        "issued_at": "2026-08-11T11:59:00Z",
        "expires_at": "2026-08-11T12:01:00Z",
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


class PromotionAuthorizationTests(unittest.TestCase):
    def test_v0_1_authorization_generically_binds_v0_2_proposal(self):
        from ao_lore.promotion import prepare_promotion
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            candidate_id, candidates, brain, runtime = build_fixture(root, evidence=True)
            deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            proposal = prepare_promotion(candidate_id, runtime / "proposals" / "evidence.json", dependencies=deps)
            value = authorization(proposal)
            path = root / "authorization-v0.1.json"
            path.write_text(json.dumps(value, sort_keys=True) + "\n")
            validated, _ = validate_authorization(path, proposal, "apply", dependencies=deps)
            self.assertEqual("ao.lore.promotion-authorization.v0.1", validated["schema_version"])
            self.assertEqual("ao.lore.promotion-proposal.v0.2", proposal["schema_version"])

    def setUp(self):
        from ao_lore.promotion import prepare_promotion
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        root = Path(self.temp.name)
        candidate_id, candidates, brain, runtime = build_fixture(root)
        self.dependencies = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
        self.proposal = prepare_promotion(candidate_id, self.dependencies.proposals_root / "proposal.json", dependencies=self.dependencies)
        self.authorization = authorization(self.proposal)
        self.path = root / "authorization.json"
        self.write(self.authorization)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, value):
        self.path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

    def test_exact_assertion_validates_without_authentication_claim(self):
        validated, digest = validate_authorization(self.path, self.proposal, "apply", dependencies=self.dependencies)
        self.assertEqual(self.authorization, validated)
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual("local_operator_assertion", validated["authentication_scope"])

    def test_time_operation_and_all_authority_broadening_fail_closed(self):
        cases = {
            "future": ("issued_at", "2026-08-11T12:00:01Z"),
            "expired": ("expires_at", "2026-08-11T11:59:59Z"),
            "wrong-operation": ("operation", "rollback"),
            "self-issued": ("product_self_issued", True),
            "not-one-use": ("one_use_only", False),
            "network": ("network_authority", True),
            "provider": ("provider_authority", True),
            "publication": ("publication_authority", True),
            "release": ("release_authority", True),
            "deployment": ("deployment_authority", True),
            "batch": ("batch_authority", True),
            "overwrite": ("overwrite_authority", True),
            "unattended": ("unattended_authority", True),
            "credential": ("credential_authority", True),
            "advance": ("authority_advance", True),
        }
        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                changed = copy.deepcopy(self.authorization); changed[field] = value; self.write(changed)
                with self.assertRaises(PromotionError):
                    validate_authorization(self.path, self.proposal, "apply", dependencies=self.dependencies)

    def test_every_exact_binding_and_unknown_or_duplicate_key_fail_closed(self):
        for field in ("proposal_digest", "candidate_digest", "provenance_digest", "accepted_review_head_digest", "canonical_entry_digest", "expected_brain_inventory_digest", "expected_result_generation_id", "allowed_write_set_digest", "source_head"):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.authorization)
                changed[field] = ("2" * 40 if field == "source_head" else "sha256:" + "9" * 64)
                self.write(changed)
                with self.assertRaises(PromotionError):
                    validate_authorization(self.path, self.proposal, "apply", dependencies=self.dependencies)
        changed = copy.deepcopy(self.authorization); changed["unknown"] = False; self.write(changed)
        with self.assertRaises(PromotionError):
            validate_authorization(self.path, self.proposal, "apply", dependencies=self.dependencies)
        self.path.write_text('{"schema_version":"x","schema_version":"y"}\n')
        with self.assertRaises(PromotionError):
            validate_authorization(self.path, self.proposal, "apply", dependencies=self.dependencies)

    def test_reservation_is_immutable_exact_retry_and_unique_id_nonce(self):
        validated, auth_digest = validate_authorization(self.path, self.proposal, "apply", dependencies=self.dependencies)
        first = reserve_authorization(validated, auth_digest, self.proposal, dependencies=self.dependencies)
        second = reserve_authorization(validated, auth_digest, self.proposal, dependencies=self.dependencies)
        self.assertEqual(first, second)
        self.assertEqual("reserved", first["state"])
        drifted = copy.deepcopy(validated); drifted["nonce"] = "nonce-drifted"
        with self.assertRaises(PromotionError):
            reserve_authorization(drifted, auth_digest, self.proposal, dependencies=self.dependencies)
        duplicate = copy.deepcopy(validated); duplicate["authorization_id"] = "authorization-other"
        with self.assertRaises(PromotionError):
            reserve_authorization(duplicate, auth_digest, self.proposal, dependencies=self.dependencies)

    def test_commit_successor_preserves_reserved_and_prevents_reuse(self):
        validated, auth_digest = validate_authorization(self.path, self.proposal, "apply", dependencies=self.dependencies)
        reserved = reserve_authorization(validated, auth_digest, self.proposal, dependencies=self.dependencies)
        committed = commit_authorization_consumption(validated, auth_digest, self.proposal, reserved, dependencies=self.dependencies)
        self.assertEqual("committed", committed["state"])
        self.assertEqual(reserved["record_digest"], committed["previous_record_digest"])
        self.assertEqual(2, len(list((self.dependencies.promotions_root / "consumed").glob("*.json"))))
        with self.assertRaisesRegex(PromotionError, "consumed"):
            validate_authorization(self.path, self.proposal, "apply", dependencies=self.dependencies)


if __name__ == "__main__":
    unittest.main()
