import json
import math
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore._strict_io import ContractError
from ao_lore.benchmark import canonical_digest
from ao_lore import evidence_graph_contracts as contracts


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"
VERSIONS = (
    "ao.lore.evidence-freshness-policy.v0.1",
    "ao.lore.evidence-freshness-observation.v0.1",
    "ao.lore.evidence-freshness-comparison.v0.1",
    "ao.lore.evidence-freshness-summary.v0.1",
)
CLASSIFICATIONS = ("unchanged", "updated", "superseded", "unavailable", "investigate")
REASON_CODES = (
    "content_unchanged", "content_changed", "declared_successor_verified",
    "terminal_unavailable", "redirect_drift", "media_type_drift", "locator_drift",
    "version_ambiguous", "effective_date_ambiguous", "prior_binding_drift",
    "unsafe_observation",
)


def digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def authority() -> dict[str, bool]:
    return {field: False for field in contracts.AUTHORITY_FIELDS}


def bind(value: dict, field: str) -> dict:
    result = deepcopy(value)
    result[field] = canonical_digest({key: item for key, item in result.items() if key != field})
    return result


def official_locator(path: str = "fixture") -> str:
    return "https://fixture.invalid/" + path


def technical_locator() -> str:
    return "https://fixture.invalid/technical"


def policy() -> dict:
    return bind({
        "schema_version": VERSIONS[0], "policy_id": "freshness-policy-01",
        "bundle_id": "synthetic-sources-01", "graph_id": "graph-01",
        "graph_digest": digest("a"), "source_registry_digest": digest("b"),
        "verification_interval_seconds": 86400,
        "maximum_observation_age_seconds": 2592000,
        "maximum_observation_bytes": 16777216, "maximum_redirect_hops": 8,
        "allowed_media_types": ["application/pdf", "text/html"],
        "allowed_terminal_classifications": list(CLASSIFICATIONS),
        "sources": [{
            "source_id": "policy-source-01",
            "canonical_locator": official_locator(),
            "prior_record_digest": digest("d"), "prior_content_digest": digest("c"),
            "prior_media_type": "text/html", "declared_successor_locator": None,
        }], **authority(),
    }, "policy_digest")


def observation() -> dict:
    return bind({
        "schema_version": VERSIONS[1], "observation_id": "observation-01",
        "policy_id": "freshness-policy-01", "bundle_id": "synthetic-sources-01",
        "graph_id": "graph-01", "graph_digest": digest("a"),
        "source_id": "policy-source-01", "prior_record_digest": digest("d"),
        "requested_locator": official_locator(),
        "final_locator": official_locator(),
        "observed_at": "2026-08-13T18:00:00Z", "status": "observed",
        "http_status": 200, "media_type": "text/html", "byte_count": 2048,
        "content_digest": digest("c"), "redirect_chain": [],
        "version": "current", "effective_date": None, **authority(),
    }, "observation_digest")


def comparison() -> dict:
    observed = observation()
    return bind({
        "schema_version": VERSIONS[2], "comparison_id": "comparison-01",
        "policy_id": "freshness-policy-01", "bundle_id": "synthetic-sources-01",
        "graph_id": "graph-01", "graph_digest": digest("a"),
        "source_id": "policy-source-01", "prior_record_digest": digest("d"),
        "observation_id": observed["observation_id"],
        "observation_digest": observed["observation_digest"],
        "classification": "unchanged", "reason_codes": ["content_unchanged"],
        "prior_content_digest": digest("c"), "observed_content_digest": digest("c"),
        "prior_locator": observed["requested_locator"],
        "observed_locator": observed["final_locator"],
        "prior_media_type": "text/html", "observed_media_type": "text/html",
        "observed_version": observed["version"],
        "observed_effective_date": observed["effective_date"],
        "declared_successor_locator": None, "qualification": None, **authority(),
    }, "comparison_digest")


def summary() -> dict:
    observed = observation()
    compared = comparison()
    return bind({
        "schema_version": VERSIONS[3], "summary_id": "freshness-summary-01",
        "policy_id": "freshness-policy-01", "policy_digest": policy()["policy_digest"],
        "bundle_id": "synthetic-sources-01", "graph_id": "graph-01",
        "graph_digest": digest("a"), "observed_at": "2026-08-13T18:00:00Z",
        "results": [{
            "source_id": "policy-source-01", "prior_record_digest": digest("d"),
            "observation_id": observed["observation_id"],
            "observation_digest": observed["observation_digest"],
            "comparison_id": compared["comparison_id"],
            "comparison_digest": compared["comparison_digest"],
            "classification": "unchanged", "reason_codes": ["content_unchanged"],
        }],
        "unchanged_count": 1, "updated_count": 0, "superseded_count": 0,
        "unavailable_count": 0, "investigate_count": 0,
        "rebuild_required": False, **authority(),
    }, "summary_digest")


SAMPLES = (policy, observation, comparison, summary)


class EvidenceFreshnessContractTests(unittest.TestCase):
    def test_fixture_locators_use_only_reserved_host(self):
        self.assertEqual("https://fixture.invalid/fixture", official_locator())

    def schema(self, index: int) -> dict:
        name = VERSIONS[index].removeprefix("ao.lore.").replace(".", "-", 1) + ".schema.json"
        return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))

    def validator(self, index: int):
        return getattr(contracts, (
            "validate_freshness_policy", "validate_freshness_observation",
            "validate_freshness_comparison", "validate_freshness_summary",
        )[index])

    def assert_rejected(self, index: int, value: dict) -> None:
        self.assertFalse(Draft202012Validator(self.schema(index)).is_valid(value))
        with self.assertRaises(ContractError):
            self.validator(index)(value)

    def test_four_schemas_are_draft_2020_12_and_recursively_closed(self):
        self.assertEqual(VERSIONS, contracts.SCHEMA_VERSIONS[-4:])
        for index in range(4):
            schema = self.schema(index)
            Draft202012Validator.check_schema(schema)
            self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])

            def closed(item):
                if isinstance(item, dict):
                    if item.get("type") == "object":
                        self.assertIs(item.get("additionalProperties"), False)
                    for child in item.values():
                        closed(child)
                elif isinstance(item, list):
                    for child in item:
                        closed(child)
            closed(schema)

    def test_exact_classification_and_reason_code_enums(self):
        self.assertEqual(CLASSIFICATIONS, contracts.FRESHNESS_CLASSIFICATIONS)
        self.assertEqual(REASON_CODES, contracts.FRESHNESS_REASON_CODES)
        self.assertEqual(list(CLASSIFICATIONS), self.schema(2)["properties"]["classification"]["enum"])
        self.assertEqual(list(REASON_CODES), self.schema(2)["$defs"]["reason_code"]["enum"])
        for field, replacement in (("classification", "fresh"), ("reason_codes", ["free_form"])):
            value = comparison()
            value[field] = replacement
            self.assert_rejected(2, value)

    def test_valid_self_bound_samples_have_schema_runtime_and_dispatch_agreement(self):
        for index, factory in enumerate(SAMPLES):
            value = factory()
            self.assertTrue(Draft202012Validator(self.schema(index)).is_valid(value))
            validated = self.validator(index)(value)
            self.assertEqual(value, validated)
            self.assertIsNot(value, validated)
            self.assertEqual(value, contracts.validate_evidence_payload(value))

    def test_unknown_fields_control_characters_private_paths_and_authority_fail_closed(self):
        cases = []
        value = policy(); value["extra"] = True; cases.append((0, value))
        value = policy(); value["sources"][0]["canonical_locator"] += "\t"; value = bind(value, "policy_digest"); cases.append((0, value))
        value = observation(); value["version"] = "current\n"; value = bind(value, "observation_digest"); cases.append((1, value))
        value = comparison(); value["qualification"] = "unsafe\rtext"; value = bind(value, "comparison_digest"); cases.append((2, value))
        value = comparison(); value["qualification"] = "/" + "home/operator/private"; value = bind(value, "comparison_digest"); cases.append((2, value))
        for index, value in cases:
            self.assert_rejected(index, value)
        for index, factory in enumerate(SAMPLES):
            schema = self.schema(index)
            for field in contracts.AUTHORITY_FIELDS:
                self.assertEqual({"const": False}, schema["properties"][field])
                value = factory(); value[field] = True
                self.assert_rejected(index, value)

    def test_exact_official_locator_rules_apply_to_every_locator(self):
        unsafe = (
            "http://fixture.invalid/guide", "https://127.0.0.1/guide",
            "https://bad-.example.test/guide", "https://user@fixture.invalid/guide",
            "https://fixture.invalid:444/guide", technical_locator() + "#fragment",
            "https://fixture.invalid", "https://FIXTURE.INVALID/guide",
        )
        for locator in unsafe:
            value = observation()
            value["requested_locator"] = locator
            value = bind(value, "observation_digest")
            self.assert_rejected(1, value)

    def test_arrays_text_and_observation_bytes_are_bounded(self):
        value = policy(); value["allowed_media_types"] = ["text/html"] * 9; value = bind(value, "policy_digest"); self.assert_rejected(0, value)
        value = policy(); value["sources"][0]["source_id"] = "x" * 129; value = bind(value, "policy_digest"); self.assert_rejected(0, value)
        value = observation(); value["byte_count"] = 16777217; value = bind(value, "observation_digest"); self.assert_rejected(1, value)
        value = observation(); value["redirect_chain"] = [value["requested_locator"]] * 9; value = bind(value, "observation_digest"); self.assert_rejected(1, value)

    def test_policy_requires_closed_interval_age_and_terminal_classification_fields(self):
        value = policy()
        self.assertEqual(
            {"type": "integer", "minimum": 1, "maximum": 31536000},
            self.schema(0)["properties"]["verification_interval_seconds"],
        )
        self.assertEqual(
            {"type": "integer", "minimum": 1, "maximum": 31536000},
            self.schema(0)["properties"]["maximum_observation_age_seconds"],
        )
        self.assertEqual(
            list(CLASSIFICATIONS),
            self.schema(0)["properties"]["allowed_terminal_classifications"]["items"]["enum"],
        )
        for field, replacement in (
            ("verification_interval_seconds", 0),
            ("maximum_observation_age_seconds", 31536001),
            ("allowed_terminal_classifications", ["updated", "updated"]),
            ("allowed_terminal_classifications", ["unknown"]),
            ("allowed_terminal_classifications", []),
        ):
            drifted = policy()
            drifted[field] = replacement
            drifted = bind(drifted, "policy_digest")
            self.assert_rejected(0, drifted)

    def test_duplicate_source_and_result_identities_are_runtime_rejected(self):
        value = policy(); value["sources"].append(deepcopy(value["sources"][0])); value = bind(value, "policy_digest")
        self.assertTrue(Draft202012Validator(self.schema(0)).is_valid(value))
        with self.assertRaises(ContractError): contracts.validate_freshness_policy(value)
        value = summary(); value["results"].append(deepcopy(value["results"][0])); value["unchanged_count"] = 2; value = bind(value, "summary_digest")
        self.assertTrue(Draft202012Validator(self.schema(3)).is_valid(value))
        with self.assertRaises(ContractError): contracts.validate_freshness_summary(value)

    def test_nonfinite_numbers_and_booleans_as_integers_are_rejected(self):
        for replacement in (math.nan, math.inf, True):
            value = observation(); value["byte_count"] = replacement
            with self.assertRaises(ContractError): contracts.validate_freshness_observation(value)
            self.assertFalse(Draft202012Validator(self.schema(1)).is_valid(value))

    def test_self_digests_and_semantic_classification_bindings_fail_closed(self):
        for index, factory, field in (
            (0, policy, "policy_digest"), (1, observation, "observation_digest"),
            (2, comparison, "comparison_digest"), (3, summary, "summary_digest"),
        ):
            value = factory(); value[field] = digest("f")
            with self.assertRaises(ContractError): self.validator(index)(value)
        value = comparison(); value["observed_content_digest"] = digest("e"); value = bind(value, "comparison_digest")
        with self.assertRaises(ContractError): contracts.validate_freshness_comparison(value)
        value = comparison(); value["classification"] = "updated"; value["reason_codes"] = ["content_changed"]; value = bind(value, "comparison_digest")
        with self.assertRaises(ContractError): contracts.validate_freshness_comparison(value)

    def test_bundle_source_record_and_graph_bindings_cannot_drift(self):
        base_policy = policy()
        for field, replacement in (
            ("bundle_id", "other-bundle"), ("source_id", "other-source"),
            ("prior_record_digest", digest("e")), ("graph_digest", digest("e")),
        ):
            value = observation(); value[field] = replacement; value = bind(value, "observation_digest")
            with self.assertRaises(ContractError):
                contracts.validate_freshness_observation(value, policy=base_policy)

        base_observation = observation()
        for field, replacement in (
            ("bundle_id", "other-bundle"), ("source_id", "other-source"),
            ("prior_record_digest", digest("e")), ("graph_digest", digest("e")),
        ):
            value = comparison(); value[field] = replacement; value = bind(value, "comparison_digest")
            with self.assertRaises(ContractError):
                contracts.validate_freshness_comparison(
                    value, policy=base_policy, observation=base_observation
                )

        base_comparison = comparison()
        value = summary(); value["bundle_id"] = "other-bundle"; value = bind(value, "summary_digest")
        with self.assertRaises(ContractError):
            contracts.validate_freshness_summary(
                value, policy=base_policy,
                observations=[base_observation], comparisons=[base_comparison],
            )
        for field, replacement in (("source_id", "other-source"), ("prior_record_digest", digest("e"))):
            value = summary(); value["results"][0][field] = replacement; value = bind(value, "summary_digest")
            with self.assertRaises(ContractError):
                contracts.validate_freshness_summary(
                    value, policy=base_policy,
                    observations=[base_observation], comparisons=[base_comparison],
                )

    def test_summary_counts_and_reason_classification_pairs_are_exact(self):
        value = summary(); value["unchanged_count"] = 0; value["investigate_count"] = 1; value = bind(value, "summary_digest")
        with self.assertRaises(ContractError): contracts.validate_freshness_summary(value)
        value = summary(); value["results"][0]["reason_codes"] = ["content_changed"]; value = bind(value, "summary_digest")
        with self.assertRaises(ContractError): contracts.validate_freshness_summary(value)

    def test_summary_rebuild_required_is_a_bound_boolean(self):
        value = summary()
        self.assertIs(
            contracts.validate_freshness_summary(
                value, policy=policy(), observations=[observation()],
                comparisons=[comparison()],
            )["rebuild_required"],
            False,
        )
        for replacement in (True, 0, "false"):
            drifted = summary()
            drifted["rebuild_required"] = replacement
            drifted = bind(drifted, "summary_digest")
            self.assertEqual(
                replacement is True,
                Draft202012Validator(self.schema(3)).is_valid(drifted),
            )
            with self.assertRaises(ContractError):
                contracts.validate_freshness_summary(
                    drifted, policy=policy(), observations=[observation()],
                    comparisons=[comparison()],
                )
        updated = summary()
        updated["results"][0]["classification"] = "updated"
        updated["results"][0]["reason_codes"] = ["content_changed"]
        updated["unchanged_count"] = 0
        updated["updated_count"] = 1
        updated["rebuild_required"] = True
        updated = bind(updated, "summary_digest")
        self.assertIs(contracts.validate_freshness_summary(updated)["rebuild_required"], True)

    def test_unchanged_rejects_locator_drift_with_identical_content(self):
        value = comparison(); value["observed_locator"] = technical_locator(); value = bind(value, "comparison_digest")
        with self.assertRaises(ContractError):
            contracts.validate_freshness_comparison(value)

    def test_unchanged_rejects_media_drift_with_identical_content(self):
        value = comparison(); value["observed_media_type"] = "application/pdf"; value = bind(value, "comparison_digest")
        with self.assertRaises(ContractError):
            contracts.validate_freshness_comparison(value)

    def test_summary_policy_binding_rejects_foreign_source(self):
        base_policy = policy()
        value = summary(); value["results"][0]["source_id"] = "foreign-source"; value = bind(value, "summary_digest")
        with self.assertRaises(ContractError):
            contracts.validate_freshness_summary(value, policy=base_policy)

    def test_summary_policy_binding_rejects_missing_source(self):
        base_policy = policy()
        second_source = deepcopy(base_policy["sources"][0])
        second_source.update(
            source_id="procedure-source", canonical_locator=technical_locator(),
            prior_record_digest=digest("1"), prior_content_digest=digest("2"),
        )
        base_policy["sources"].append(second_source)
        base_policy = bind(base_policy, "policy_digest")
        value = summary(); value["policy_digest"] = base_policy["policy_digest"]; value = bind(value, "summary_digest")
        with self.assertRaises(ContractError):
            contracts.validate_freshness_summary(value, policy=base_policy)

    def test_superseded_requires_verified_version_and_effective_date_evidence(self):
        value = comparison()
        value["classification"] = "superseded"
        value["reason_codes"] = ["declared_successor_verified"]
        value["declared_successor_locator"] = value["observed_locator"]
        value["observed_version"] = None
        value["observed_effective_date"] = None
        value = bind(value, "comparison_digest")
        self.assertFalse(Draft202012Validator(self.schema(2)).is_valid(value))
        with self.assertRaises(ContractError):
            contracts.validate_freshness_comparison(value)

    def test_superseded_accepts_bound_successor_version_and_effective_date_evidence(self):
        successor_locator = technical_locator()
        base_policy = policy()
        base_policy["sources"][0]["declared_successor_locator"] = successor_locator
        base_policy = bind(base_policy, "policy_digest")

        observed = observation()
        observed["final_locator"] = successor_locator
        observed["version"] = "2026 edition"
        observed["effective_date"] = "2026-01-01"
        observed = bind(observed, "observation_digest")

        value = comparison()
        value.update(
            observation_digest=observed["observation_digest"],
            classification="superseded",
            reason_codes=["declared_successor_verified"],
            observed_locator=successor_locator,
            observed_version=observed["version"],
            observed_effective_date=observed["effective_date"],
            declared_successor_locator=successor_locator,
        )
        value = bind(value, "comparison_digest")
        self.assertTrue(Draft202012Validator(self.schema(2)).is_valid(value))
        self.assertEqual(
            value,
            contracts.validate_freshness_comparison(
                value, policy=base_policy, observation=observed,
            ),
        )

    def test_superseded_rejects_ambiguous_version_evidence(self):
        value = comparison()
        value.update(
            classification="superseded",
            reason_codes=["declared_successor_verified", "version_ambiguous"],
            declared_successor_locator=value["observed_locator"],
            observed_version="2026 edition",
            observed_effective_date="2026-01-01",
        )
        value = bind(value, "comparison_digest")
        self.assertFalse(Draft202012Validator(self.schema(2)).is_valid(value))
        with self.assertRaises(ContractError):
            contracts.validate_freshness_comparison(value)

    def test_superseded_rejects_ambiguous_effective_date_evidence(self):
        value = comparison()
        value.update(
            classification="superseded",
            reason_codes=["declared_successor_verified", "effective_date_ambiguous"],
            declared_successor_locator=value["observed_locator"],
            observed_version="2026 edition",
            observed_effective_date="2026-01-01",
        )
        value = bind(value, "comparison_digest")
        self.assertFalse(Draft202012Validator(self.schema(2)).is_valid(value))
        with self.assertRaises(ContractError):
            contracts.validate_freshness_comparison(value)


if __name__ == "__main__":
    unittest.main()
