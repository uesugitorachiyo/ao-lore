import hashlib
import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore._strict_io import ContractError, parse_strict_json
from ao_lore.sanitized_lifecycle_contracts import (
    validate_sanitized_lifecycle_rehearsal,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "ao-lore" / "sanitized-lifecycle-rehearsal-v0.1.schema.json"


def _digest(value):
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _with_self_digest(value):
    result = deepcopy(value)
    result.pop("report_digest", None)
    result["report_digest"] = _digest(result)
    return result


def _valid_report():
    value = {
        "schema_version": "ao.lore.sanitized-lifecycle-rehearsal.v0.1",
        "rehearsal_id": "sanitized-lifecycle-proof",
        "completed_at": "2026-08-14T12:00:00Z",
        "summary": "Offline synthetic governed lifecycle completed deterministically.",
        "corpus_digest": "01" * 32,
        "document_digest": "02" * 32,
        "evidence_digest": "03" * 32,
        "candidate_digest": "04" * 32,
        "review_digest": "05" * 32,
        "proposal_digest": "06" * 32,
        "authorization_digest": "07" * 32,
        "promotion_digest": "08" * 32,
        "generation_digest": "09" * 32,
        "search_digest": "0a" * 32,
        "answer_digest": "0b" * 32,
        "inventory_digest": "0c" * 32,
        "document_ids": ["document.alpha", "document.beta", "document.unrelated"],
        "evidence_ids": ["evidence.alpha", "evidence.beta", "evidence.relationship"],
        "candidate_ids": ["candidate.alpha"],
        "review_ids": ["review.alpha"],
        "canonical_entry_ids": ["entry.alpha"],
        "counts": {
            "documents": 3,
            "evidence_identities": 3,
            "candidates": 1,
            "reviews": 1,
            "proposals": 1,
            "authorizations": 1,
            "promotions": 1,
            "canonical_entries": 1,
            "search_hits": 1,
            "answers": 1,
        },
        "scenario_statuses": [
            {"scenario_id": "document-ingest", "status": "pass"},
            {"scenario_id": "governed-selection", "status": "pass"},
            {"scenario_id": "authorized-promotion", "status": "pass"},
            {"scenario_id": "canonical-readback", "status": "pass"},
            {"scenario_id": "deterministic-replay", "status": "pass"},
        ],
        "byte_identical_check": True,
        "network_calls": 0,
        "provider_calls": 0,
        "external_authority": {
            "live_state_accessed": False,
            "customer_data_accessed": False,
            "github_accessed": False,
            "publication_executed": False,
            "release_executed": False,
            "deployment_executed": False,
            "credentials_used": False,
            "authority_advanced": False,
        },
    }
    return _with_self_digest(value)


class SanitizedLifecycleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.validator = Draft202012Validator(cls.schema)

    def assertRejectedByBoth(self, value):
        self.assertFalse(self.validator.is_valid(value))
        with self.assertRaises(ContractError):
            validate_sanitized_lifecycle_rehearsal(value)

    def test_valid_report_has_schema_runtime_parity_and_is_detached(self):
        report = _valid_report()
        Draft202012Validator.check_schema(self.schema)
        self.validator.validate(report)
        validated = validate_sanitized_lifecycle_rehearsal(report)
        self.assertEqual(report, validated)
        self.assertIsNot(report, validated)
        self.assertIsNot(report["counts"], validated["counts"])
        self.assertIsNot(report["scenario_statuses"], validated["scenario_statuses"])
        report["counts"]["documents"] = 9
        self.assertEqual(3, validated["counts"]["documents"])

    def test_report_is_closed_at_every_object_boundary(self):
        for mutation in (
            lambda value: value.pop("answer_digest"),
            lambda value: value.update(unexpected=True),
            lambda value: value["counts"].update(unexpected=0),
            lambda value: value["scenario_statuses"][0].update(unexpected=False),
            lambda value: value["external_authority"].update(unexpected=False),
        ):
            with self.subTest(mutation=mutation):
                value = _valid_report()
                mutation(value)
                self.assertRejectedByBoth(value)

    def test_identifier_arrays_are_bounded_and_unique(self):
        for field, maximum in (
            ("document_ids", 16),
            ("evidence_ids", 64),
            ("candidate_ids", 16),
            ("review_ids", 16),
            ("canonical_entry_ids", 16),
        ):
            with self.subTest(field=field, failure="duplicate"):
                value = _valid_report()
                value[field].append(value[field][0])
                value = _with_self_digest(value)
                self.assertRejectedByBoth(value)
            with self.subTest(field=field, failure="bound"):
                value = _valid_report()
                value[field] = [f"{field[:-1]}.{index:03d}" for index in range(maximum + 1)]
                value = _with_self_digest(value)
                self.assertRejectedByBoth(value)

    def test_digests_are_exact_lowercase_sha256_values(self):
        digest_fields = [field for field in _valid_report() if field.endswith("_digest")]
        for field in digest_fields:
            with self.subTest(field=field):
                value = _valid_report()
                value[field] = "sha256:" + "a" * 64
                with self.assertRaises(ContractError):
                    validate_sanitized_lifecycle_rehearsal(value)
                self.assertFalse(self.validator.is_valid(value))

    def test_counts_bind_identity_arrays_and_remain_bounded(self):
        value = _valid_report()
        value["counts"]["documents"] = 4
        value = _with_self_digest(value)
        self.assertTrue(self.validator.is_valid(value))
        with self.assertRaises(ContractError):
            validate_sanitized_lifecycle_rehearsal(value)
        for field in ("proposals", "authorizations", "promotions", "search_hits", "answers"):
            invalid = _valid_report()
            invalid["counts"][field] = 17
            invalid = _with_self_digest(invalid)
            self.assertRejectedByBoth(invalid)

    def test_scenarios_preserve_order_and_reject_duplicate_ids(self):
        report = _valid_report()
        validated = validate_sanitized_lifecycle_rehearsal(report)
        self.assertEqual(
            [item["scenario_id"] for item in report["scenario_statuses"]],
            [item["scenario_id"] for item in validated["scenario_statuses"]],
        )
        invalid = _valid_report()
        invalid["scenario_statuses"].append({"scenario_id": "document-ingest", "status": "fail"})
        invalid = _with_self_digest(invalid)
        with self.assertRaises(ContractError):
            validate_sanitized_lifecycle_rehearsal(invalid)

    def test_text_controls_private_paths_and_timestamp_drift_fail_closed(self):
        private_home = "/" + "home" + "/operator/private"
        for replacement in ("safe\u0007text", private_home, "file:/opt/private", "C:\\private"):
            with self.subTest(replacement=replacement):
                value = _valid_report()
                value["summary"] = replacement
                value = _with_self_digest(value)
                self.assertRejectedByBoth(value)
        for timestamp in ("2026-08-14 12:00:00Z", "2026-08-14T12:00:00.1Z", "2026-08-14T12:00:00+00:00"):
            value = _valid_report()
            value["completed_at"] = timestamp
            value = _with_self_digest(value)
            self.assertRejectedByBoth(value)

    def test_public_text_rejects_generic_absolute_posix_paths_but_allows_urls_and_prose(self):
        for replacement in (
            "/etc/passwd",
            "/Users/alice/private",
            "Artifact was read from /var/lib/private-state.",
        ):
            with self.subTest(replacement=replacement):
                value = _valid_report()
                value["summary"] = replacement
                value = _with_self_digest(value)
                self.assertRejectedByBoth(value)
        for replacement in (
            "Public reference https://example.test/knowledge/lifecycle",
            "Offline input/output comparison completed.",
            "Synthetic evidence and review completed without private state.",
        ):
            with self.subTest(replacement=replacement):
                value = _valid_report()
                value["summary"] = replacement
                value = _with_self_digest(value)
                self.validator.validate(value)
                self.assertEqual(value, validate_sanitized_lifecycle_rehearsal(value))

    def test_network_provider_replay_and_external_authority_are_constants(self):
        for mutation in (
            lambda value: value.update(network_calls=1),
            lambda value: value.update(provider_calls=1),
            lambda value: value.update(byte_identical_check=False),
            lambda value: value["external_authority"].update(github_accessed=True),
            lambda value: value["external_authority"].update(credentials_used=True),
            lambda value: value["external_authority"].update(authority_advanced=True),
        ):
            value = _valid_report()
            mutation(value)
            value = _with_self_digest(value)
            self.assertRejectedByBoth(value)

    def test_wrong_self_digest_and_duplicate_json_keys_are_rejected(self):
        value = _valid_report()
        value["report_digest"] = "f" * 64
        self.assertTrue(self.validator.is_valid(value))
        with self.assertRaises(ContractError):
            validate_sanitized_lifecycle_rehearsal(value)
        with self.assertRaises(ContractError):
            parse_strict_json(b'{"schema_version":"one","schema_version":"two"}', "rehearsal")


if __name__ == "__main__":
    unittest.main()
