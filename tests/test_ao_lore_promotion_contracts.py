import copy
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore.promotion import _PromotionDependencies, apply_promotion, build_rollback_proposal, inspect_promotion, prepare_promotion, recover_promotions, rollback_promotion
from tests.test_ao_lore_promotion_authorization import authorization
from tests.test_ao_lore_promotion_prepare import FIXED_NOW, SOURCE_HEAD, build_fixture
from tests.schema_registry import shipped_validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"
PROMOTION_SCHEMAS = {
    "okf-canonical-entry-v0.2.schema.json",
    "brain-generation-manifest-v0.1.schema.json",
    "promotion-proposal-v0.1.schema.json",
    "promotion-proposal-v0.2.schema.json",
    "okf-canonical-entry-v0.3.schema.json",
    "okf-canonical-entry-v0.4.schema.json",
    "okf-candidate-v0.3.schema.json",
    "promotion-authorization-v0.1.schema.json",
    "promotion-transaction-v0.1.schema.json",
    "promotion-authorization-consumption-v0.1.schema.json",
    "promotion-audit-event-v0.1.schema.json",
    "promotion-recovery-event-v0.1.schema.json",
    "promotion-inspection-v0.1.schema.json",
    "promotion-operation-readback-v0.1.schema.json",
    "rollback-proposal-v0.1.schema.json",
}
DIGEST = "sha256:" + "0" * 64


def load_schema(name):
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def walk_schema(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_schema(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_schema(child)


class PromotionContractStructureTests(unittest.TestCase):
    def test_schema_family_is_valid_draft_2020_12(self):
        for name in PROMOTION_SCHEMAS:
            schema = load_schema(name)
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema",
                schema["$schema"],
                name,
            )
            self.assertEqual(
                f"https://schemas.ao-lore.example/{name}", schema["$id"], name
            )
            Draft202012Validator.check_schema(schema)

    def test_every_inline_object_is_closed(self):
        for name in PROMOTION_SCHEMAS:
            for node in walk_schema(load_schema(name)):
                if node.get("type") == "object":
                    self.assertIs(node.get("additionalProperties"), False, name)

    def test_every_inline_collection_and_public_string_is_bounded(self):
        for name in PROMOTION_SCHEMAS:
            for node in walk_schema(load_schema(name)):
                if node.get("type") == "array":
                    self.assertIn("maxItems", node, name)
                if node.get("type") == "integer":
                    self.assertIn("maximum", node, name)
                if node.get("type") == "string" and not {
                    "$ref", "const", "enum"
                }.intersection(node):
                    pattern = node.get("pattern", "")
                    self.assertTrue("maxLength" in node or "{" in pattern, name)

    def test_authorization_keeps_assertion_scope_and_all_authority_false(self):
        schema = load_schema("promotion-authorization-v0.1.schema.json")
        properties = schema["properties"]
        self.assertEqual(
            {"const": "local_operator_assertion"}, properties["authentication_scope"]
        )
        self.assertEqual({"const": True}, properties["one_use_only"])
        for field in (
            "product_self_issued",
            "provider_authority",
            "network_authority",
            "publication_authority",
            "release_authority",
            "deployment_authority",
            "batch_authority",
            "overwrite_authority",
            "unattended_authority",
            "credential_authority",
            "authority_advance",
        ):
            self.assertEqual({"const": False}, properties[field], field)

    def test_audit_contract_has_no_private_or_arbitrary_text_fields(self):
        properties = load_schema("promotion-audit-event-v0.1.schema.json")[
            "properties"
        ]
        for forbidden in (
            "path",
            "content",
            "rationale",
            "reviewer",
            "operator_id",
            "hostname",
            "username",
            "exception",
            "message",
        ):
            self.assertNotIn(forbidden, properties)
        self.assertEqual(
            "^[a-z][a-z0-9_]{0,63}$", properties["reason_code"]["pattern"]
        )

    def test_entry_payload_has_no_authority_or_private_locator(self):
        properties = load_schema("okf-canonical-entry-v0.2.schema.json")[
            "properties"
        ]
        for forbidden in (
            "proposal_digest",
            "authorization_digest",
            "transaction_digest",
            "operator_id",
            "reviewer",
            "rationale",
            "source_path",
            "source_locator",
        ):
            self.assertNotIn(forbidden, properties)


class PromotionContractSampleTests(unittest.TestCase):
    def test_v0_2_proposal_is_closed_and_references_canonical_v0_4(self):
        schema = load_schema("promotion-proposal-v0.2.schema.json")
        self.assertEqual({"$ref": "okf-canonical-entry-v0.4.schema.json"}, schema["properties"]["canonical_entry"])
        self.assertNotIn("document_ir_digest", schema["properties"])
        self.assertNotIn("source_digest", schema["properties"])

    def authorization(self):
        return {
            "schema_version": "ao.lore.promotion-authorization.v0.1",
            "policy_version": "ao.lore.promotion-policy.v0.1",
            "operation": "apply",
            "authorization_id": "authorization-fixture-1",
            "nonce": "nonce-fixture-1",
            "operator_id": "opaque-fixture-operator",
            "authentication_scope": "local_operator_assertion",
            "proposal_digest": DIGEST,
            "promotion_id": "promotion-fixture-1",
            "candidate_digest": DIGEST,
            "provenance_digest": DIGEST,
            "accepted_review_head_digest": DIGEST,
            "canonical_entry_digest": DIGEST,
            "expected_brain_inventory_digest": DIGEST,
            "expected_prior_generation_id": None,
            "expected_result_generation_id": "generation-fixture-1",
            "allowed_write_set_digest": DIGEST,
            "source_head": "0" * 40,
            "issued_at": "2026-08-11T00:00:00Z",
            "expires_at": "2026-08-11T00:05:00Z",
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

    def test_authorization_accepts_exact_fixture_and_rejects_broadening(self):
        validator = Draft202012Validator(
            load_schema("promotion-authorization-v0.1.schema.json")
        )
        authorization = self.authorization()
        self.assertTrue(validator.is_valid(authorization))
        for field in (
            "network_authority",
            "publication_authority",
            "overwrite_authority",
            "authority_advance",
        ):
            broadened = copy.deepcopy(authorization)
            broadened[field] = True
            self.assertFalse(validator.is_valid(broadened), field)
        unknown = copy.deepcopy(authorization)
        unknown["force"] = True
        self.assertFalse(validator.is_valid(unknown))

    def test_operation_readback_is_bounded_and_closed(self):
        readback = {
            "schema_version": "ao.lore.promotion-operation-readback.v0.1",
            "operation": "recover",
            "status": "no_op",
            "promotion_id": None,
            "proposal_id": None,
            "authorization_id": None,
            "transaction_id": None,
            "generation_id": None,
            "result_digest": None,
            "reason_code": "no_pending_recovery",
            "retry_safe": True,
            "next_command": "none",
        }
        validator = Draft202012Validator(
            load_schema("promotion-operation-readback-v0.1.schema.json")
        )
        self.assertTrue(validator.is_valid(readback))
        readback["reason_code"] = "contains unsafe free-form text"
        self.assertFalse(validator.is_valid(readback))

    def test_every_runtime_artifact_and_readback_matches_its_schema(self):
        schemas = {
            load_schema(name)["properties"]["schema_version"]["const"]:
                shipped_validator(name)
            for name in PROMOTION_SCHEMAS
        }
        for evidence in (False, True):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
                root = Path(name)
                candidate_id, candidates, brain, promotions = build_fixture(root, evidence=evidence)
                deps = _PromotionDependencies(candidates, brain, promotions, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
                proposal_path = deps.proposals_root / "proposal.json"
                proposal = prepare_promotion(candidate_id, proposal_path, dependencies=deps)
                self.assertEqual(
                    "ao.lore.promotion-proposal.v0.2" if evidence else "ao.lore.promotion-proposal.v0.1",
                    proposal["schema_version"],
                )
                self.assertEqual(
                    "ao.lore.okf-canonical-entry.v0.4" if evidence else "ao.lore.okf-canonical-entry.v0.2",
                    proposal["canonical_entry"]["schema_version"],
                )
                apply_auth = authorization(proposal)
                apply_auth_path = root / "apply-authorization.json"
                apply_auth_path.write_text(json.dumps(apply_auth, sort_keys=True) + "\n")
                applied = apply_promotion(proposal_path, apply_auth_path, dependencies=deps)
                inspected_apply = inspect_promotion(proposal["promotion_id"], dependencies=deps)
                rollback = build_rollback_proposal(proposal["promotion_id"], dependencies=deps)
                rollback_auth = authorization(proposal)
                rollback_auth.update({
                    "operation": "rollback", "authorization_id": "authorization-rollback-parity", "nonce": "nonce-rollback-parity",
                    "proposal_digest": rollback["proposal_digest"], "expected_brain_inventory_digest": rollback["current_brain_inventory_digest"],
                    "expected_prior_generation_id": rollback["current_generation_id"], "expected_result_generation_id": rollback["expected_generation_id"],
                    "allowed_write_set_digest": rollback["allowed_write_set_digest"],
                })
                rollback_auth_path = root / "rollback-authorization.json"
                rollback_auth_path.write_text(json.dumps(rollback_auth, sort_keys=True) + "\n")
                rolled_back = rollback_promotion(proposal["promotion_id"], rollback_auth_path, dependencies=deps)
                inspected_rollback = inspect_promotion(proposal["promotion_id"], dependencies=deps)
                no_op = recover_promotions(dependencies=deps)
                values = [proposal, proposal["canonical_entry"], apply_auth, applied, inspected_apply, rollback, rollback_auth, rolled_back, inspected_rollback, no_op]
                for artifact_root in (deps.promotions_root / "consumed", deps.promotions_root / "audit", deps.promotions_root / "recovery", deps.promotions_root / "transactions", brain / "generations"):
                    values.extend(json.loads(path.read_text()) for path in artifact_root.rglob("*.json"))
                for value in values:
                    schema_version = value["schema_version"]
                    self.assertIn(schema_version, schemas)
                    schemas[schema_version].validate(value)


if __name__ == "__main__":
    unittest.main()
