import json
import math
import re
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore._strict_io import ContractError, parse_strict_json
import ao_lore.evidence_graph_contracts as contracts
from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_graph_contracts import (
    AUTHORITY_FIELDS,
    AUTHORITY_ROLES,
    EDGE_REASON_CODES,
    EDGE_TYPES,
    SCHEMA_VERSIONS,
    canonical_evidence_digest,
    validate_source_registry,
    validate_acquisition_record,
    validate_graph_manifest,
    validate_freshness_policy,
    validate_freshness_observation,
    validate_freshness_comparison,
    validate_graph_inspection_against_manifest,
    validate_query_readback_against_manifest,
    validate_evidence_payload,
)


ROOT = Path(__file__).resolve().parents[1]
HTTPS = "https" + "://"
INVALID_TLD = "." + "invalid"
TEST_TLD = "." + "test"
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"
SCHEMA_NAMES = tuple(
    version.removeprefix("ao.lore.").replace(".", "-", 1) + ".schema.json"
    for version in SCHEMA_VERSIONS[:10]
)


def digest(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


def bind(value: dict, field: str) -> dict:
    result = deepcopy(value)
    result[field] = canonical_digest({key: item for key, item in result.items() if key != field})
    return result


def authority() -> dict[str, bool]:
    return {field: False for field in AUTHORITY_FIELDS}


def official_locator(path: str = "fixture") -> str:
    return HTTPS + "fixture" + INVALID_TLD + "/" + path


def test_locator(host: str = "docs", path: str = "/") -> str:
    return HTTPS + host + TEST_TLD + path


def edge(excerpt_digest: str | None = None) -> dict:
    return bind({
        "schema_version": "ao.lore.evidence-relationship-edge.v0.1",
        "edge_id": "edge-01", "edge_type": "defines", "source_kind": "claim",
        "source_id": "claim-01", "source_evidence_digest": digest("a"),
        "target_kind": "workflow", "target_id": "control-procedure",
        "target_evidence_digest": digest("b"),
        "supporting_excerpt_digest": excerpt_digest or digest("c"),
        "reason_code": "statutory_definition", "qualification": None,
    }, "edge_digest")


def question() -> dict:
    return bind({
        "schema_version": "ao.lore.evidence-operational-question.v0.1",
        "question_id": "question-01", "prompt": "Which source defines the relevant duty?",
        "expected_outcome": "answer", "required_authority_roles": ["primary_law"],
        "required_evidence_ids": ["claim-01"], "forbidden_evidence_ids": [],
        "qualifications": ["General public information only."],
    }, "question_digest")


def source() -> dict:
    return {
        "source_id": "policy-source-01", "source_digest": digest("d"),
        "canonical_locator": official_locator("fixture"),
        "retrieved_at": "2026-08-12T20:00:00Z", "publisher": "Synthetic Policy Register",
        "jurisdiction": "Synthetic", "authority_role": "primary_law", "version": "current",
        "effective_date": None, "operational_question_ids": ["question-01"],
        "relationship_edge_ids": ["edge-01", "edge-02"], "status": "current", "media_type": "text/html",
        "retained_artifact_digests": [digest("d")],
    }


def samples() -> dict[str, dict]:
    excerpt = "Policy controls shall be maintained in good working order."
    values = {}
    values[SCHEMA_VERSIONS[0]] = bind({
        "schema_version": SCHEMA_VERSIONS[0], "registry_id": "registry-01",
        "root_workflow_id": "control-procedure", "sources": [source()], **authority(),
    }, "registry_digest")
    values[SCHEMA_VERSIONS[1]] = bind({
        "schema_version": SCHEMA_VERSIONS[1], "acquisition_id": "acquisition-01",
        "source_id": "policy-source-01", "requested_locator": source()["canonical_locator"],
        "final_locator": source()["canonical_locator"], "retrieved_at": "2026-08-12T20:00:00Z",
        "status": "acquired", "http_status": 200, "media_type": "text/html", "byte_count": 1024,
        "content_digest": digest("d"), "redirect_chain": [], **authority(),
    }, "record_digest")
    values[SCHEMA_VERSIONS[2]] = bind({
        "schema_version": SCHEMA_VERSIONS[2], "role": "primary_law", "precedence": 5,
        "enacted_law": True, "may_override_roles": list(AUTHORITY_ROLES[1:]),
    }, "role_digest")
    values[SCHEMA_VERSIONS[3]] = edge()
    values[SCHEMA_VERSIONS[4]] = question()
    claim = {
        "claim_id": "claim-01", "source_id": "policy-source-01", "source_digest": digest("d"),
        "authority_role": "primary_law", "excerpt": excerpt,
        "excerpt_digest": canonical_evidence_digest(excerpt, "excerpt"), "citation_anchor": "control-section-01",
        "operational_question_ids": ["question-01"], "subject_terms": ["control"],
        "semantic_reason_codes": ["topic_match", "citation_supported"],
    }
    graph_edge = edge(claim["excerpt_digest"])
    graph_edge["source_evidence_digest"] = claim["excerpt_digest"]
    graph_edge["target_evidence_digest"] = digest("e")
    graph_edge = bind(graph_edge, "edge_digest")
    source_edge = deepcopy(graph_edge)
    source_edge.update(
        edge_id="edge-02", edge_type="cites", source_kind="source",
        source_id="policy-source-01", source_evidence_digest=digest("d"),
        target_kind="claim", target_id="claim-01",
        target_evidence_digest=claim["excerpt_digest"],
        reason_code="source_citation",
    )
    source_edge = bind(source_edge, "edge_digest")
    graph_source = source()
    graph_source["relationship_edge_ids"] = ["edge-01", "edge-02"]
    values[SCHEMA_VERSIONS[5]] = bind({
        "schema_version": SCHEMA_VERSIONS[5], "graph_id": "graph-01",
        "root_workflow_id": "control-procedure", "root_workflow_digest": digest("e"),
        "source_registry_digest": values[SCHEMA_VERSIONS[0]]["registry_digest"],
        "sources": [graph_source], "claims": [claim],
        "edges": [graph_edge, source_edge],
        "operational_questions": [question()], **authority(),
    }, "graph_digest")
    values[SCHEMA_VERSIONS[6]] = bind({
        "schema_version": SCHEMA_VERSIONS[6], "graph_id": "graph-01",
        "graph_digest": values[SCHEMA_VERSIONS[5]]["graph_digest"], "result": "pass",
        "source_count": 1, "claim_count": 1, "edge_count": 2, "question_count": 1,
        "orphan_source_count": 0, "orphan_claim_count": 0, "conflict_count": 0,
        "stale_source_count": 0, **authority(),
    }, "inspection_digest")
    values[SCHEMA_VERSIONS[7]] = bind({
        "schema_version": SCHEMA_VERSIONS[7], "query_id": "query-01",
        "prompt_digest": digest("f"), "graph_digest": values[SCHEMA_VERSIONS[5]]["graph_digest"],
        "outcome": "answer", "evidence_ids": ["claim-01"], "authority_roles": ["primary_law"],
        "qualifications": ["General public information only."], **authority(),
    }, "readback_digest")
    values[SCHEMA_VERSIONS[8]] = bind({
        "schema_version": SCHEMA_VERSIONS[8], "campaign_id": "neutral-evidence-01",
        "correlation_id": "ao-lore-neutral-evidence-20260812",
        "registry_digest": values[SCHEMA_VERSIONS[0]]["registry_digest"],
        "graph_digest": values[SCHEMA_VERSIONS[5]]["graph_digest"],
        "inspection_digest": values[SCHEMA_VERSIONS[6]]["inspection_digest"], "uat_digest": digest("1"),
        "source_count": 1, "claim_count": 1, "edge_count": 1, "question_count": 1,
        "passed_question_count": 1, "refused_question_count": 0,
        "investigate_question_count": 0, "zero_orphans": True, **authority(),
    }, "summary_digest")
    values[SCHEMA_VERSIONS[9]] = bind({
        "schema_version": SCHEMA_VERSIONS[9], "attempt_id": "attempt-01",
        "phase": "acquisition", "status": "completed", "intended_digest": digest("2"),
        "staged_digest": digest("2"), "destination_digest": digest("2"),
        "classification": "complete", **authority(),
    }, "recovery_digest")
    return values


class EvidenceGraphContractTests(unittest.TestCase):
    def schema(self, version: str) -> dict:
        name = version.removeprefix("ao.lore.").replace(".", "-", 1) + ".schema.json"
        return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))

    def assert_runtime_rejects(self, value: dict) -> None:
        with self.assertRaises(ContractError):
            validate_evidence_payload(value)

    def test_exact_ten_schema_family_is_draft_2020_12_and_recursively_closed(self):
        self.assertEqual(10, len(SCHEMA_NAMES))
        for version in SCHEMA_VERSIONS[:10]:
            schema = self.schema(version)
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

    def test_complete_reserved_locator_artifacts_have_schema_runtime_parity(self):
        locator = official_locator("neutral-source")
        freshness = __import__("tests.test_ao_lore_evidence_freshness_contracts", fromlist=["SAMPLES"])
        registry = deepcopy(samples()[SCHEMA_VERSIONS[0]])
        registry["sources"][0]["canonical_locator"] = locator
        registry = bind(registry, "registry_digest")
        acquisition = deepcopy(samples()[SCHEMA_VERSIONS[1]])
        acquisition["source_id"] = registry["sources"][0]["source_id"]
        acquisition["requested_locator"] = locator
        acquisition["final_locator"] = locator
        acquisition["content_digest"] = registry["sources"][0]["source_digest"]
        acquisition = bind(acquisition, "record_digest")
        graph = deepcopy(samples()[SCHEMA_VERSIONS[5]])
        graph["sources"] = deepcopy(registry["sources"])
        graph["source_registry_digest"] = registry["registry_digest"]
        graph = bind(graph, "graph_digest")
        policy = freshness.policy()
        policy.update(
            graph_id=graph["graph_id"], graph_digest=graph["graph_digest"],
            source_registry_digest=registry["registry_digest"],
        )
        policy["sources"][0].update(
            source_id=acquisition["source_id"], canonical_locator=locator,
            prior_record_digest=acquisition["record_digest"],
            prior_content_digest=acquisition["content_digest"],
            prior_media_type=acquisition["media_type"],
        )
        policy = bind(policy, "policy_digest")
        observation = freshness.observation()
        observation.update(
            policy_id=policy["policy_id"], bundle_id=policy["bundle_id"],
            graph_id=policy["graph_id"], graph_digest=policy["graph_digest"],
            source_id=acquisition["source_id"],
            prior_record_digest=acquisition["record_digest"],
            requested_locator=locator, final_locator=locator,
            content_digest=acquisition["content_digest"],
            media_type=acquisition["media_type"],
        )
        observation = bind(observation, "observation_digest")
        comparison = freshness.comparison()
        comparison.update(
            policy_id=policy["policy_id"], bundle_id=policy["bundle_id"],
            graph_id=policy["graph_id"], graph_digest=policy["graph_digest"],
            source_id=observation["source_id"],
            prior_record_digest=observation["prior_record_digest"],
            observation_id=observation["observation_id"],
            observation_digest=observation["observation_digest"],
            prior_content_digest=policy["sources"][0]["prior_content_digest"],
            observed_content_digest=observation["content_digest"],
            prior_locator=policy["sources"][0]["canonical_locator"],
            observed_locator=observation["final_locator"],
            prior_media_type=policy["sources"][0]["prior_media_type"],
            observed_media_type=observation["media_type"],
            observed_version=observation["version"],
            observed_effective_date=observation["effective_date"],
            declared_successor_locator=policy["sources"][0]["declared_successor_locator"],
        )
        comparison = bind(comparison, "comparison_digest")
        self.assertEqual(registry["sources"][0]["source_id"], acquisition["source_id"])
        self.assertEqual(registry["sources"][0]["canonical_locator"], acquisition["requested_locator"])
        self.assertEqual(registry["sources"][0]["source_digest"], acquisition["content_digest"])
        self.assertEqual(registry["registry_digest"], graph["source_registry_digest"])
        self.assertEqual(registry["sources"], graph["sources"])
        self.assertEqual((graph["graph_id"], graph["graph_digest"]), (policy["graph_id"], policy["graph_digest"]))
        self.assertEqual(registry["registry_digest"], policy["source_registry_digest"])
        self.assertEqual(acquisition["record_digest"], policy["sources"][0]["prior_record_digest"])
        self.assertEqual(policy["policy_id"], observation["policy_id"])
        self.assertEqual(acquisition["record_digest"], observation["prior_record_digest"])
        self.assertEqual(policy["policy_id"], comparison["policy_id"])
        self.assertEqual(observation["observation_digest"], comparison["observation_digest"])
        cases = (
            ("evidence-source-registry-v0.1.schema.json", registry, validate_source_registry),
            ("evidence-acquisition-record-v0.1.schema.json", acquisition, validate_acquisition_record),
            ("evidence-graph-manifest-v0.1.schema.json", graph, validate_graph_manifest),
            ("evidence-freshness-policy-v0.1.schema.json", policy, validate_freshness_policy),
            ("evidence-freshness-observation-v0.1.schema.json", observation, lambda value: validate_freshness_observation(value, policy=policy)),
            ("evidence-freshness-comparison-v0.1.schema.json", comparison, lambda value: validate_freshness_comparison(value, policy=policy, observation=observation)),
        )
        for schema_name, artifact, runtime_validator in cases:
            with self.subTest(schema=schema_name):
                schema = json.loads((SCHEMA_ROOT / schema_name).read_text())
                self.assertEqual([], list(Draft202012Validator(schema).iter_errors(artifact)))
                self.assertEqual(artifact, runtime_validator(artifact))
                self.assertEqual(artifact, validate_evidence_payload(artifact))

        drifted = deepcopy(observation)
        drifted["prior_record_digest"] = digest("9")
        drifted = bind(drifted, "observation_digest")
        observation_schema = json.loads((SCHEMA_ROOT / "evidence-freshness-observation-v0.1.schema.json").read_text())
        self.assertEqual([], list(Draft202012Validator(observation_schema).iter_errors(drifted)))
        self.assertEqual(drifted, validate_freshness_observation(drifted))
        with self.assertRaises(ContractError):
            validate_freshness_observation(drifted, policy=policy)

    def test_valid_samples_have_schema_runtime_parity_and_are_copied(self):
        for version, value in samples().items():
            errors = list(Draft202012Validator(self.schema(version)).iter_errors(value))
            self.assertEqual([], errors, (version, errors))
            validated = validate_evidence_payload(value)
            self.assertEqual(value, validated)
            self.assertIsNot(value, validated)

    def test_six_locator_schemas_share_domain_neutral_runtime_grammar(self):
        schema_names = (
            "evidence-acquisition-record-v0.1.schema.json",
            "evidence-source-registry-v0.1.schema.json",
            "evidence-graph-manifest-v0.1.schema.json",
            "evidence-freshness-policy-v0.1.schema.json",
            "evidence-freshness-observation-v0.1.schema.json",
            "evidence-freshness-comparison-v0.1.schema.json",
        )
        valid = (
            official_locator("source"),
            test_locator(),
            test_locator("a-b", ":443/path?version=1"),
            test_locator("v2.docs123", "/path"),
        )
        invalid = (
            "http" + "://" + "docs" + TEST_TLD + "/path", HTTPS + "DOCS" + TEST_TLD + "/path",
            HTTPS + "user@docs" + TEST_TLD + "/path", HTTPS + "127.0.0.1/path",
            HTTPS + "[::1]/path", HTTPS + "docs" + TEST_TLD + "." + "/path",
            HTTPS + "*.example" + TEST_TLD + "/path", HTTPS + "-bad.example" + TEST_TLD + "/path",
            HTTPS + "bad-.example" + TEST_TLD + "/path", HTTPS + "docs" + TEST_TLD + ":444/path",
            HTTPS + "docs" + TEST_TLD + "/path#fragment", HTTPS + "docs" + TEST_TLD + "/path\n",
            HTTPS + "docs" + TEST_TLD + "/path\x85drift",
            HTTPS + "docs" + TEST_TLD + "/" + "p" * 1024,
            HTTPS + "a" * 64 + ".example/path",
            HTTPS + ".".join(("a" * 63,) * 4) + "/path",
            HTTPS + "docs" + TEST_TLD + "/?" + "q" * 2048,
            HTTPS + "0x7f000001/path", HTTPS + "0x7f.0.0.1/path",
            HTTPS + "127.0.0x0.1/path", HTTPS + "2130706433/path",
            HTTPS + "017700000001/path", HTTPS + "127.1/path",
            HTTPS + "0177.0.0.1/path",
            " " + HTTPS + "docs" + TEST_TLD + "/path",
            HTTPS + "docs" + TEST_TLD + "/path ",
            "HTTPS" + "://docs" + TEST_TLD + "/path",
            "hTtPs" + "://docs" + TEST_TLD + "/path",
        )
        patterns = set()
        for schema_name in schema_names:
            raw_schema = (SCHEMA_ROOT / schema_name).read_text(encoding="utf-8")
            self.assertIn(r"https:\\/\\/", raw_schema, schema_name)
            locator_schema = json.loads(raw_schema)["$defs"]["locator"]
            self.assertIn(r"https:\/\/", locator_schema["pattern"], schema_name)
            unescaped_pattern = locator_schema["pattern"].replace(r"\/", "/")
            patterns.add(locator_schema["pattern"])
            validator = Draft202012Validator(locator_schema)
            for locator in valid:
                with self.subTest(schema=schema_name, locator=locator):
                    self.assertEqual(
                        re.fullmatch(locator_schema["pattern"], locator) is not None,
                        re.fullmatch(unescaped_pattern, locator) is not None,
                    )
                    self.assertTrue(validator.is_valid(locator))
                    self.assertEqual(locator, contracts._locator(locator, "locator"))
            for locator in invalid:
                with self.subTest(schema=schema_name, locator=locator):
                    self.assertEqual(
                        re.fullmatch(locator_schema["pattern"], locator) is not None,
                        re.fullmatch(unescaped_pattern, locator) is not None,
                    )
                    self.assertFalse(validator.is_valid(locator))
                    with self.assertRaises(ContractError):
                        contracts._locator(locator, "locator")
        self.assertEqual(1, len(patterns))

    def test_legacy_ipv4_numeric_recognizer_is_bounded_and_network_free(self):
        accepted = (
            "2130706433", "017700000001", "0x7f000001",
            "127.1", "0177.00000001", "0x7f.0x1",
            "127.0.1", "0177.0.01", "0x7f.0.0x1",
            "127.0.0.1", "0177.0.0.01", "0x7f.0.0.0x1",
            "4294967295", "037777777777", "0xffffffff",
        )
        rejected = (
            "", "example" + TEST_TLD, "1.2.3.4.5", "0x", "08",
            "4294967296", "040000000000", "0x100000000",
            "256.1", "1.16777216", "1.256.1", "1.1.65536",
            "1.1.1.256", "1.-1", "1..1",
        )

        for host in accepted:
            with self.subTest(host=host):
                self.assertTrue(contracts._is_legacy_ipv4_numeric(host))
        for host in rejected:
            with self.subTest(host=host):
                self.assertFalse(contracts._is_legacy_ipv4_numeric(host))

        source = Path(contracts.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import " + "socket", source)
        self.assertIn("import ipaddress", source)

    def test_recovery_contract_accepts_freshness_without_changing_legacy_phases(self):
        for phase in ("acquisition", "graph_publication", "freshness"):
            value = samples()[SCHEMA_VERSIONS[9]]
            value["phase"] = phase
            value = bind(value, "recovery_digest")
            self.assertTrue(Draft202012Validator(self.schema(SCHEMA_VERSIONS[9])).is_valid(value))
            self.assertEqual(phase, validate_evidence_payload(value)["phase"])

    def test_closed_role_edge_and_semantic_reason_enums(self):
        self.assertEqual(6, len(AUTHORITY_ROLES))
        self.assertEqual(11, len(EDGE_TYPES))
        self.assertEqual(10, len(EDGE_REASON_CODES))
        for version, field, replacement in (
            (SCHEMA_VERSIONS[2], "role", "legal_authority"),
            (SCHEMA_VERSIONS[3], "edge_type", "overrides"),
            (SCHEMA_VERSIONS[3], "reason_code", "free_form_reason"),
        ):
            value = samples()[version]
            value[field] = replacement
            self.assertFalse(Draft202012Validator(self.schema(version)).is_valid(value))
            self.assert_runtime_rejects(value)
        graph = samples()[SCHEMA_VERSIONS[5]]
        graph["claims"][0]["semantic_reason_codes"] = ["looks_good"]
        self.assertFalse(Draft202012Validator(self.schema(SCHEMA_VERSIONS[5])).is_valid(graph))
        self.assert_runtime_rejects(graph)

    def test_unknown_fields_digests_locators_and_authority_widening_fail_closed(self):
        base = samples()[SCHEMA_VERSIONS[0]]
        for mutate in (
            lambda value: value.update(extra=True),
            lambda value: value.update(registry_digest="sha256:BAD"),
            lambda value: value["sources"][0].update(canonical_locator=HTTPS + "EVIL.example/guide"),
            lambda value: value["sources"][0].update(canonical_locator=HTTPS + "127.0.0.1/guide"),
            lambda value: value["sources"][0].update(canonical_locator="file://" + "/" + "home/user/private"),
            lambda value: value.update(promotion=True),
        ):
            changed = deepcopy(base)
            mutate(changed)
            self.assertFalse(Draft202012Validator(self.schema(SCHEMA_VERSIONS[0])).is_valid(changed))
            self.assert_runtime_rejects(changed)
        drifted = deepcopy(base)
        drifted["root_workflow_id"] = "different-workflow"
        self.assertTrue(Draft202012Validator(self.schema(SCHEMA_VERSIONS[0])).is_valid(drifted))
        self.assert_runtime_rejects(drifted)

    def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self):
        with self.assertRaises(ContractError):
            parse_strict_json(b'{"schema_version":"x","schema_version":"y"}', "evidence")
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            with self.assertRaises(ContractError):
                parse_strict_json(b'{"value":' + token + b"}", "evidence")
        value = samples()[SCHEMA_VERSIONS[6]]
        value["source_count"] = math.nan
        self.assert_runtime_rejects(value)

    def test_oversized_arrays_strings_and_duplicate_identities_are_rejected(self):
        question_value = samples()[SCHEMA_VERSIONS[4]]
        question_value["prompt"] = "x" * 1025
        self.assertFalse(Draft202012Validator(self.schema(SCHEMA_VERSIONS[4])).is_valid(question_value))
        self.assert_runtime_rejects(question_value)
        graph = samples()[SCHEMA_VERSIONS[5]]
        graph["sources"] = [deepcopy(graph["sources"][0]) for _ in range(17)]
        self.assertFalse(Draft202012Validator(self.schema(SCHEMA_VERSIONS[5])).is_valid(graph))
        self.assert_runtime_rejects(graph)
        graph = samples()[SCHEMA_VERSIONS[5]]
        graph["sources"].append(deepcopy(graph["sources"][0]))
        self.assertTrue(Draft202012Validator(self.schema(SCHEMA_VERSIONS[5])).is_valid(graph))
        self.assert_runtime_rejects(graph)

    def test_semantic_bindings_and_cross_field_safety_are_runtime_enforced(self):
        graph = samples()[SCHEMA_VERSIONS[5]]
        graph["claims"][0]["excerpt"] = "Different text."
        self.assert_runtime_rejects(graph)
        question_value = samples()[SCHEMA_VERSIONS[4]]
        question_value["forbidden_evidence_ids"] = ["claim-01"]
        self.assert_runtime_rejects(question_value)
        inspection = samples()[SCHEMA_VERSIONS[6]]
        inspection["orphan_claim_count"] = 1
        inspection = bind(inspection, "inspection_digest")
        self.assert_runtime_rejects(inspection)
        role = samples()[SCHEMA_VERSIONS[2]]
        role["may_override_roles"].insert(0, "primary_law")
        role = bind(role, "role_digest")
        self.assert_runtime_rejects(role)

    def test_self_rehashed_relational_graph_drift_fails_closed(self):
        base = samples()[SCHEMA_VERSIONS[5]]
        mutations = (
            lambda value: value["claims"][0].update(source_id="missing-source"),
            lambda value: value["claims"][0].update(source_digest=digest("9")),
            lambda value: value["claims"][0].update(authority_role="technical_guidance"),
            lambda value: value["edges"][0].update(source_id="missing-claim"),
            lambda value: value["edges"][0].update(source_evidence_digest=digest("9")),
            lambda value: value["edges"][0].update(supporting_excerpt_digest=digest("9")),
            lambda value: value["sources"][0].update(relationship_edge_ids=["edge-unknown"]),
            lambda value: value["sources"][0].update(operational_question_ids=["missing-question"]),
            lambda value: value["claims"][0].update(operational_question_ids=["missing-question"]),
            lambda value: value["operational_questions"][0].update(required_evidence_ids=["missing-evidence"]),
            lambda value: value["operational_questions"][0].update(required_authority_roles=["technical_guidance"]),
            lambda value: value["edges"][0].update(edge_type="defines", source_kind="workflow", source_id="control-procedure", source_evidence_digest=digest("b")),
        )
        for mutate in mutations:
            changed = deepcopy(base)
            mutate(changed)
            for item in changed["edges"]:
                item["edge_digest"] = canonical_digest(
                    {key: child for key, child in item.items() if key != "edge_digest"}
                )
            for item in changed["operational_questions"]:
                item["question_digest"] = canonical_digest(
                    {key: child for key, child in item.items() if key != "question_digest"}
                )
            changed = bind(changed, "graph_digest")
            with self.subTest(mutate=mutate), self.assertRaisesRegex(
                ContractError, "graph manifest relationships are invalid"
            ):
                validate_graph_manifest(changed)

    def test_schema_rejects_runtime_forbidden_controls_in_every_public_text_shape(self):
        cases = []

        for field in ("publisher", "jurisdiction", "version", "canonical_locator"):
            value = samples()[SCHEMA_VERSIONS[0]]
            value["sources"][0][field] += "\t"
            cases.append((SCHEMA_VERSIONS[0], bind(value, "registry_digest"), field))

        for field in ("requested_locator", "final_locator"):
            value = samples()[SCHEMA_VERSIONS[1]]
            value[field] += "\t"
            cases.append((SCHEMA_VERSIONS[1], bind(value, "record_digest"), field))
        value = samples()[SCHEMA_VERSIONS[1]]
        value["redirect_chain"] = [value["requested_locator"] + "\t"]
        cases.append((SCHEMA_VERSIONS[1], bind(value, "record_digest"), "redirect_chain"))

        value = samples()[SCHEMA_VERSIONS[3]]
        value["qualification"] = "scope\t"
        cases.append((SCHEMA_VERSIONS[3], bind(value, "edge_digest"), "qualification"))

        for field in ("prompt", "qualifications"):
            value = samples()[SCHEMA_VERSIONS[4]]
            if field == "prompt":
                value[field] += "\t"
            else:
                value[field] = ["scope\t"]
            cases.append((SCHEMA_VERSIONS[4], bind(value, "question_digest"), field))

        graph_mutations = (
            ("source.publisher", lambda value: value["sources"][0].update(publisher="agency\t")),
            ("source.jurisdiction", lambda value: value["sources"][0].update(jurisdiction="state\t")),
            ("source.version", lambda value: value["sources"][0].update(version="current\t")),
            ("source.locator", lambda value: value["sources"][0].update(canonical_locator=value["sources"][0]["canonical_locator"] + "\t")),
            ("claim.excerpt", lambda value: value["claims"][0].update(excerpt=value["claims"][0]["excerpt"] + "\t")),
            ("claim.citation_anchor", lambda value: value["claims"][0].update(citation_anchor="control\t")),
            ("claim.subject_terms", lambda value: value["claims"][0].update(subject_terms=["control\t"])),
            ("edge.qualification", lambda value: value["edges"][0].update(qualification="scope\t")),
            ("question.prompt", lambda value: value["operational_questions"][0].update(prompt="question\t")),
            ("question.qualifications", lambda value: value["operational_questions"][0].update(qualifications=["scope\t"])),
        )
        for label, mutate in graph_mutations:
            value = samples()[SCHEMA_VERSIONS[5]]
            mutate(value)
            if label == "claim.excerpt":
                value["claims"][0]["excerpt_digest"] = canonical_evidence_digest(
                    value["claims"][0]["excerpt"], "excerpt"
                )
            if label.startswith("edge."):
                value["edges"][0] = bind(value["edges"][0], "edge_digest")
            if label.startswith("question."):
                value["operational_questions"][0] = bind(
                    value["operational_questions"][0], "question_digest"
                )
            cases.append((SCHEMA_VERSIONS[5], bind(value, "graph_digest"), label))

        value = samples()[SCHEMA_VERSIONS[7]]
        value["qualifications"] = ["scope\t"]
        cases.append((SCHEMA_VERSIONS[7], bind(value, "readback_digest"), "qualifications"))

        for version, value, label in cases:
            self.assertFalse(
                Draft202012Validator(self.schema(version)).is_valid(value),
                (version, label),
            )
            self.assert_runtime_rejects(value)

        for codepoint in range(32):
            value = samples()[SCHEMA_VERSIONS[4]]
            value["prompt"] = f"prefix{chr(codepoint)}suffix"
            value = bind(value, "question_digest")
            self.assertFalse(
                Draft202012Validator(self.schema(SCHEMA_VERSIONS[4])).is_valid(value),
                codepoint,
            )
            self.assert_runtime_rejects(value)

    def test_authority_role_schema_accepts_only_runtime_legal_combinations(self):
        for index, role in enumerate(AUTHORITY_ROLES):
            value = bind({
                "schema_version": SCHEMA_VERSIONS[2],
                "role": role,
                "precedence": 5 - index,
                "enacted_law": role == "primary_law",
                "may_override_roles": list(AUTHORITY_ROLES[index + 1:]),
            }, "role_digest")
            self.assertTrue(Draft202012Validator(self.schema(SCHEMA_VERSIONS[2])).is_valid(value))
            self.assertEqual(value, validate_evidence_payload(value))

            for mutate in (
                lambda item: item.update(precedence=(item["precedence"] + 1) % 6),
                lambda item: item.update(enacted_law=not item["enacted_law"]),
                lambda item: item.update(may_override_roles=list(reversed(item["may_override_roles"]))),
                lambda item: item.update(may_override_roles=[]),
            ):
                changed = deepcopy(value)
                mutate(changed)
                changed = bind(changed, "role_digest")
                if changed != value:
                    self.assertFalse(
                        Draft202012Validator(self.schema(SCHEMA_VERSIONS[2])).is_valid(changed),
                        (role, changed),
                    )
                    self.assert_runtime_rejects(changed)

    def test_inspection_is_bound_to_manifest_digest_counts_and_expected_orphans(self):
        manifest = samples()[SCHEMA_VERSIONS[5]]
        inspection = samples()[SCHEMA_VERSIONS[6]]
        self.assertEqual(
            inspection,
            validate_graph_inspection_against_manifest(
                inspection,
                manifest,
                expected_orphan_source_count=0,
                expected_orphan_claim_count=0,
            ),
        )
        mutations = (
            lambda value: value.update(graph_digest=digest("9")),
            lambda value: value.update(source_count=0),
            lambda value: value.update(source_count=2),
            lambda value: value.update(claim_count=0),
            lambda value: value.update(claim_count=2),
            lambda value: value.update(edge_count=0),
            lambda value: value.update(edge_count=3),
            lambda value: value.update(question_count=0),
            lambda value: value.update(question_count=2),
            lambda value: value.update(orphan_source_count=1, result="investigate"),
            lambda value: value.update(orphan_claim_count=1, result="investigate"),
        )
        for mutate in mutations:
            changed = deepcopy(inspection)
            mutate(changed)
            changed = bind(changed, "inspection_digest")
            with self.assertRaises(ContractError):
                validate_graph_inspection_against_manifest(
                    changed,
                    manifest,
                    expected_orphan_source_count=0,
                    expected_orphan_claim_count=0,
                )

        contextual_orphan = deepcopy(inspection)
        contextual_orphan.update(result="investigate", orphan_source_count=1)
        contextual_orphan = bind(contextual_orphan, "inspection_digest")
        self.assertEqual(
            contextual_orphan,
            validate_graph_inspection_against_manifest(
                contextual_orphan,
                manifest,
                expected_orphan_source_count=1,
                expected_orphan_claim_count=0,
            ),
        )
        with self.assertRaises(ContractError):
            validate_graph_inspection_against_manifest(
                inspection,
                manifest,
                expected_orphan_source_count=1,
                expected_orphan_claim_count=0,
            )

    def test_query_readback_is_bound_to_graph_and_known_evidence_identities(self):
        manifest = samples()[SCHEMA_VERSIONS[5]]
        readback = samples()[SCHEMA_VERSIONS[7]]
        self.assertEqual(
            readback,
            validate_query_readback_against_manifest(readback, manifest),
        )
        for mutate in (
            lambda value: value.update(graph_digest=digest("9")),
            lambda value: value.update(evidence_ids=["unknown-evidence"]),
        ):
            changed = deepcopy(readback)
            mutate(changed)
            changed = bind(changed, "readback_digest")
            with self.assertRaises(ContractError):
                validate_query_readback_against_manifest(changed, manifest)

        explicit = deepcopy(readback)
        self.assertEqual(
            explicit,
            validate_query_readback_against_manifest(
                explicit,
                manifest,
                allowed_evidence_ids={"claim-01"},
            ),
        )
        with self.assertRaises(ContractError):
            validate_query_readback_against_manifest(
                explicit,
                manifest,
                allowed_evidence_ids={"edge-01"},
            )


if __name__ == "__main__":
    unittest.main()
