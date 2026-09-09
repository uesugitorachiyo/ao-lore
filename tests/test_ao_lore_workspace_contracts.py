import json
import math
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

import ao_lore.workspace_contracts as workspace_contracts
from ao_lore._strict_io import ContractError, parse_strict_json
from ao_lore.benchmark import canonical_digest
from ao_lore.workspace_contracts import (
    AUTHORITY_FIELDS,
    WORKSPACE_SCHEMA_VERSIONS,
    validate_workspace_definition,
    validate_workspace_operation_readback,
    validate_workspace_query_readback,
    validate_workspace_registry_generation,
    validate_workspace_registry_inspection,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"


def digest(character="a"):
    return "sha256:" + character * 64


def bind(value, field):
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def authority():
    return {field: False for field in AUTHORITY_FIELDS}


def definition(workspace_id="reference-01", workspace_type="reference", references=None):
    return bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[0],
        "workspace_id": workspace_id,
        "workspace_version": 1,
        "workspace_type": workspace_type,
        "domain": "synthetic-guidance",
        "jurisdiction": "test-jurisdiction",
        "lifecycle_status": "active",
        "root_workflow_id": f"workflow-{workspace_id}",
        "root_workflow_digest": digest("1"),
        "source_registry_id": f"source-registry-{workspace_id}",
        "source_registry_digest": digest("2"),
        "graph_id": f"graph-{workspace_id}",
        "graph_digest": digest("3"),
        "freshness_policy_id": None,
        "freshness_policy_digest": None,
        "freshness_summary_status": "not_observed",
        "freshness_summary_id": None,
        "freshness_summary_digest": None,
        "reference_workspace_ids": [] if references is None else references,
        "definition_digest": digest("0"),
        **authority(),
    }, "definition_digest")


def definition_v2(workspace_id="reference-01", workspace_type="reference", references=None, *, graph=True,
                  document_generation_digest=digest("6")):
    value = definition(workspace_id, workspace_type, references)
    value["schema_version"] = "ao.lore.workspace-definition.v0.2"
    value["document_store_id"] = "documents-" + workspace_id
    value["document_generation_digest"] = document_generation_digest
    if not graph:
        value["graph_id"] = None
        value["graph_digest"] = None
    return bind(value, "definition_digest")


def generation():
    return bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[1],
        "registry_id": "registry-01",
        "sequence": 1,
        "predecessor_registry_digest": None,
        "generated_at": "2026-08-13T12:00:00Z",
        "workspaces": [
            definition("property-01", "property", ["reference-01"]),
            definition(),
        ],
        "registry_digest": digest("0"),
        **authority(),
    }, "registry_digest")


def inspection():
    current = generation()
    return bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[2],
        "inspection_id": "inspection-01",
        "registry_id": current["registry_id"],
        "registry_digest": current["registry_digest"],
        "sequence": current["sequence"],
        "status": "active",
        "reason_code": "ok",
        "workspace_ids": ["property-01", "reference-01"],
        "workspace_count": 2,
        "active_count": 2,
        "inactive_count": 0,
        "investigate_count": 0,
        "inspection_digest": digest("0"),
        **authority(),
    }, "inspection_digest")


def empty_inspection():
    return bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[2],
        "inspection_id": "inspection-empty",
        "registry_id": None,
        "registry_digest": None,
        "sequence": 0,
        "status": "empty",
        "reason_code": "ok",
        "workspace_ids": [],
        "workspace_count": 0,
        "active_count": 0,
        "inactive_count": 0,
        "investigate_count": 0,
        "inspection_digest": digest("0"),
        **authority(),
    }, "inspection_digest")


def operation():
    current = generation()
    return bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[3],
        "operation_id": "operation-01",
        "operation": "inspect",
        "workspace_id": "property-01",
        "registry_id": current["registry_id"],
        "registry_digest": current["registry_digest"],
        "status": "active",
        "reason_code": "ok",
        "affected_workspace_ids": ["property-01"],
        "readback_digest": digest("0"),
        **authority(),
    }, "readback_digest")


def query():
    current = generation()
    return bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[4],
        "query_id": "query-01",
        "prompt_digest": digest("4"),
        "registry_id": current["registry_id"],
        "registry_digest": current["registry_digest"],
        "primary_workspace_id": "property-01",
        "consulted_workspace_ids": ["property-01", "reference-01"],
        "outcome": "answer",
        "reason_code": "ok",
        "evidence": [{
            "workspace_id": "reference-01",
            "graph_id": "graph-reference-01",
            "source_id": "source-01",
            "evidence_kind": "claim",
            "evidence_id": "claim-01",
            "evidence_digest": digest("5"),
        }],
        "qualifications": ["Synthetic evidence only."],
        "readback_digest": digest("0"),
        **authority(),
    }, "readback_digest")


def generation_v2():
    return bind({
        "schema_version": WORKSPACE_SCHEMA_VERSIONS[1],
        "registry_id": "registry-01",
        "sequence": 1,
        "predecessor_registry_digest": None,
        "generated_at": "2026-08-13T12:00:00Z",
        "workspaces": [
            definition_v2("property-01", "property", ["reference-01"], graph=False),
            definition_v2(),
        ],
        "registry_digest": digest("0"),
        **authority(),
    }, "registry_digest")


def query_v2(*, document_id="record-01"):
    current = generation_v2()
    return bind({
        "schema_version": "ao.lore.workspace-query-readback.v0.2",
        "query_id": "query-01",
        "prompt_digest": digest("4"),
        "registry_id": current["registry_id"],
        "registry_digest": current["registry_digest"],
        "primary_workspace_id": "property-01",
        "consulted_workspace_ids": ["property-01", "reference-01"],
        "outcome": "answer",
        "reason_code": "ok",
        "evidence": [{
            "workspace_id": "reference-01",
            "document_store_id": "documents-reference-01",
            "generation_digest": digest("6"),
            "document_id": document_id,
            "source_id": "source-01",
            "evidence_kind": "document_block",
            "evidence_id": digest("7"),
            "evidence_digest": digest("5"),
        }],
        "qualifications": ["Synthetic evidence only."],
        "readback_digest": digest("0"),
        **authority(),
    }, "readback_digest")


VALIDATORS = (
    validate_workspace_definition,
    validate_workspace_registry_generation,
    validate_workspace_registry_inspection,
    validate_workspace_operation_readback,
    validate_workspace_query_readback,
)


class WorkspaceContractTests(unittest.TestCase):
    def test_v0_2_workspace_may_bind_documents_without_graph(self):
        value = definition_v2(graph=False)
        self.assertEqual(None, validate_workspace_definition(value)["graph_id"])
        schema = self.schema("ao.lore.workspace-definition.v0.2")
        self.assertTrue(Draft202012Validator(schema).is_valid(value))

    def test_v0_2_workspace_may_declare_an_empty_document_store(self):
        value = definition_v2(graph=False, document_generation_digest=None)
        self.assertEqual(None, validate_workspace_definition(value)["document_generation_digest"])
        schema = self.schema("ao.lore.workspace-definition.v0.2")
        self.assertTrue(Draft202012Validator(schema).is_valid(value))

    def test_v0_2_workspace_rejects_partial_graph_or_document_binding(self):
        for field, replacement in (("graph_id", "graph-a"), ("graph_digest", digest("3")), ("document_store_id", None)):
            value = definition_v2(graph=False)
            value[field] = replacement
            bind(value, "definition_digest")
            with self.assertRaises(ContractError):
                validate_workspace_definition(value)

    def test_v0_2_query_document_evidence_accepts_digest_document_id_with_schema_runtime_parity(self):
        value = query_v2(document_id=digest("a"))
        schema = self.schema("ao.lore.workspace-query-readback.v0.2")
        self.assertTrue(Draft202012Validator(schema).is_valid(value))
        self.assertEqual(
            value,
            validate_workspace_query_readback(
                value,
                generation=generation_v2(),
                selected_workspace_ids={"property-01", "reference-01"},
            ),
        )

    def test_v0_2_query_document_evidence_rejects_malformed_digest_like_document_ids(self):
        schema = self.schema("ao.lore.workspace-query-readback.v0.2")
        for bad_document_id in (
            "sha256:" + "A" * 64,
            "SHA256:" + "a" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
        ):
            value = query_v2(document_id=bad_document_id)
            with self.subTest(document_id=bad_document_id):
                self.assertFalse(Draft202012Validator(schema).is_valid(value))
                with self.assertRaises(ContractError):
                    validate_workspace_query_readback(
                        value,
                        generation=generation_v2(),
                        selected_workspace_ids={"property-01", "reference-01"},
                    )
    def schema(self, version):
        name = version.removeprefix("ao.lore.").replace(".v", "-v") + ".schema.json"
        return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))

    def test_schema_family_is_registered_draft_2020_12_and_recursively_closed(self):
        self.assertEqual(5, len(WORKSPACE_SCHEMA_VERSIONS))
        for version in WORKSPACE_SCHEMA_VERSIONS:
            schema = self.schema(version)
            self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
            Draft202012Validator.check_schema(schema)
            self._assert_closed(schema)

    def _assert_closed(self, value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                self.assertIs(value.get("additionalProperties"), False)
            for child in value.values():
                self._assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                self._assert_closed(child)

    def test_valid_samples_have_schema_runtime_parity_and_exact_self_digests(self):
        values = (definition(), generation(), inspection(), operation(), query())
        for version, validator, value in zip(WORKSPACE_SCHEMA_VERSIONS, VALIDATORS, values):
            self.assertTrue(Draft202012Validator(self.schema(version)).is_valid(value), version)
            self.assertEqual(value, validator(value))
            changed = deepcopy(value)
            changed[next(key for key in changed if key.endswith("_digest"))] = digest("f")
            with self.assertRaises(ContractError):
                validator(changed)

    def test_empty_registry_inspection_is_closed_schema_runtime_and_context_valid(self):
        value = empty_inspection()
        schema = self.schema(WORKSPACE_SCHEMA_VERSIONS[2])
        self.assertTrue(Draft202012Validator(schema).is_valid(value))
        self.assertEqual(value, validate_workspace_registry_inspection(value, generation=None))
        with self.assertRaises(ContractError):
            validate_workspace_registry_inspection(value, generation=generation())

    def test_exact_enums_and_all_authority_flags_are_fail_closed(self):
        schema = self.schema(WORKSPACE_SCHEMA_VERSIONS[0])
        self.assertEqual(["reference", "property", "matter", "operations"], schema["properties"]["workspace_type"]["enum"])
        self.assertEqual(["active", "inactive", "investigate"], schema["properties"]["lifecycle_status"]["enum"])
        reasons = self.schema(WORKSPACE_SCHEMA_VERSIONS[3])["$defs"]["reason_code"]["enum"]
        self.assertEqual(["ok", "workspace_unknown", "workspace_inactive", "registry_invalid", "identity_collision", "reference_not_declared", "reference_cycle", "workspace_binding_drift", "freshness_investigation_required", "recovery_pending"], reasons)
        for validator, value in zip(VALIDATORS, (definition(), generation(), inspection(), operation(), query())):
            for field in AUTHORITY_FIELDS:
                changed = deepcopy(value)
                changed[field] = True
                with self.assertRaises(ContractError):
                    validator(changed)

    def test_exact_workspace_reason_codes_are_publicly_exported(self):
        self.assertEqual(
            ("ok", "workspace_unknown", "workspace_inactive", "registry_invalid",
             "identity_collision", "reference_not_declared", "reference_cycle",
             "workspace_binding_drift", "freshness_investigation_required",
             "recovery_pending"),
            workspace_contracts.WORKSPACE_REASON_CODES,
        )

    def test_unknown_keys_malformed_values_booleans_and_nonfinite_fail(self):
        values = (definition(), generation(), inspection(), operation(), query())
        for version, validator, value in zip(WORKSPACE_SCHEMA_VERSIONS, VALIDATORS, values):
            for mutation in (
                lambda item: item.update(unexpected=True),
                lambda item: item.update(**{next(key for key in item if key.endswith("_digest")): "sha256:BAD"}),
            ):
                changed = deepcopy(value)
                mutation(changed)
                self.assertFalse(Draft202012Validator(self.schema(version)).is_valid(changed))
                with self.assertRaises(ContractError):
                    validator(changed)
        for bad in (True, math.nan, math.inf):
            changed = definition()
            changed["workspace_version"] = bad
            with self.assertRaises(ContractError):
                validate_workspace_definition(changed)

    def test_public_text_and_private_locator_shapes_are_rejected(self):
        for unsafe in ("/home/operator/private", "/opt/customer/data", "file:///tmp/private", "C:\\private", "Synthetic /TmP/private marker"):
            changed = query()
            changed["qualifications"] = [unsafe]
            changed = bind(changed, "readback_digest")
            self.assertFalse(Draft202012Validator(self.schema(WORKSPACE_SCHEMA_VERSIONS[4])).is_valid(changed))
            with self.assertRaises(ContractError):
                validate_workspace_query_readback(changed)

    def test_definition_and_generation_require_deterministic_identity_order(self):
        changed = definition("property-01", "property", ["reference-02", "reference-01"])
        self.assertTrue(Draft202012Validator(self.schema(WORKSPACE_SCHEMA_VERSIONS[0])).is_valid(changed))
        with self.assertRaises(ContractError):
            validate_workspace_definition(changed)
        changed = generation()
        changed["workspaces"].reverse()
        changed = bind(changed, "registry_digest")
        self.assertTrue(Draft202012Validator(self.schema(WORKSPACE_SCHEMA_VERSIONS[1])).is_valid(changed))
        with self.assertRaises(ContractError):
            validate_workspace_registry_generation(changed)

    def test_registry_timestamp_has_exact_schema_runtime_parity(self):
        schema = self.schema(WORKSPACE_SCHEMA_VERSIONS[1])
        for timestamp in ("2026-08-13 12:00:00Z", "2026-08-13T12:00:00.1Z"):
            changed = generation()
            changed["generated_at"] = timestamp
            changed = bind(changed, "registry_digest")
            self.assertFalse(Draft202012Validator(schema).is_valid(changed))
            with self.assertRaises(ContractError):
                validate_workspace_registry_generation(changed)

    def test_generation_rejects_duplicate_identities_and_unsafe_reference_graphs(self):
        mutations = []
        duplicate = generation()
        duplicate["workspaces"].append(deepcopy(duplicate["workspaces"][0]))
        mutations.append(duplicate)
        collision = generation()
        collision["workspaces"][1]["graph_id"] = collision["workspaces"][0]["graph_id"]
        collision["workspaces"][1] = bind(collision["workspaces"][1], "definition_digest")
        mutations.append(collision)
        missing = generation()
        missing["workspaces"][1]["reference_workspace_ids"] = ["missing-reference"]
        missing["workspaces"][1] = bind(missing["workspaces"][1], "definition_digest")
        mutations.append(missing)
        private_import = generation()
        private_import["workspaces"][1]["workspace_type"] = "property"
        private_import["workspaces"][1] = bind(private_import["workspaces"][1], "definition_digest")
        mutations.append(private_import)
        transitive = generation()
        transitive["workspaces"][1]["reference_workspace_ids"] = ["reference-02"]
        transitive["workspaces"][1] = bind(transitive["workspaces"][1], "definition_digest")
        transitive["workspaces"].append(definition("reference-02"))
        mutations.append(transitive)
        for changed in mutations:
            changed = bind(changed, "registry_digest")
            with self.assertRaises(ContractError):
                validate_workspace_registry_generation(changed)

    def test_context_aware_inspection_and_operation_bind_generation(self):
        current = generation()
        self.assertEqual(inspection(), validate_workspace_registry_inspection(inspection(), generation=current))
        self.assertEqual(operation(), validate_workspace_operation_readback(operation(), generation=current, selected_workspace_ids={"property-01"}))
        for value, validator, kwargs in (
            (inspection(), validate_workspace_registry_inspection, {"generation": {**current, "registry_digest": digest("e")}}),
            (operation(), validate_workspace_operation_readback, {"generation": current, "selected_workspace_ids": {"reference-01"}}),
        ):
            with self.assertRaises(ContractError):
                validator(value, **kwargs)

    def test_operation_context_rejects_nonexistent_caller_selected_workspaces(self):
        changed = operation()
        changed["workspace_id"] = "missing-01"
        changed["affected_workspace_ids"] = ["missing-01"]
        changed = bind(changed, "readback_digest")
        with self.assertRaises(ContractError):
            validate_workspace_operation_readback(
                changed,
                generation=generation(),
                selected_workspace_ids={"missing-01"},
            )

    def test_context_membership_is_independently_bound_for_inspection_and_query(self):
        changed = inspection()
        changed["workspace_ids"] = ["missing-01", "property-01"]
        changed = bind(changed, "inspection_digest")
        with self.assertRaises(ContractError):
            validate_workspace_registry_inspection(changed, generation=generation())
        changed = query()
        changed["primary_workspace_id"] = "missing-01"
        changed["consulted_workspace_ids"] = ["missing-01", "reference-01"]
        changed = bind(changed, "readback_digest")
        with self.assertRaises(ContractError):
            validate_workspace_query_readback(
                changed,
                generation=generation(),
                selected_workspace_ids={"missing-01", "reference-01"},
            )

    def test_readback_statuses_and_outcomes_have_closed_reason_pairings(self):
        inspection_schema = self.schema(WORKSPACE_SCHEMA_VERSIONS[2])
        operation_schema = self.schema(WORKSPACE_SCHEMA_VERSIONS[3])
        query_schema = self.schema(WORKSPACE_SCHEMA_VERSIONS[4])
        self.assertEqual(
            ["active", "empty", "complete", "success", "inactive", "investigate"],
            operation_schema["properties"]["status"]["enum"],
        )
        cases = (
            (inspection(), validate_workspace_registry_inspection, inspection_schema, "status", "active", "registry_invalid"),
            (inspection(), validate_workspace_registry_inspection, inspection_schema, "status", "investigate", "ok"),
            (operation(), validate_workspace_operation_readback, operation_schema, "status", "success", "workspace_unknown"),
            (operation(), validate_workspace_operation_readback, operation_schema, "status", "inactive", "ok"),
            (query(), validate_workspace_query_readback, query_schema, "outcome", "answer", "workspace_inactive"),
            (query(), validate_workspace_query_readback, query_schema, "outcome", "investigate", "ok"),
        )
        for value, validator, schema, field, result, reason in cases:
            changed = deepcopy(value)
            changed[field] = result
            changed["reason_code"] = reason
            changed = bind(changed, "inspection_digest" if field == "status" and "inspection_digest" in changed else "readback_digest")
            self.assertFalse(Draft202012Validator(schema).is_valid(changed))
            with self.assertRaises(ContractError):
                validator(changed)
        for status in ("active", "empty", "complete", "success"):
            changed = operation()
            changed["status"] = status
            changed = bind(changed, "readback_digest")
            self.assertTrue(Draft202012Validator(operation_schema).is_valid(changed))
            self.assertEqual(changed, validate_workspace_operation_readback(changed))

    def test_query_requires_origin_tuple_and_exact_selected_workspace_context(self):
        current = generation()
        selected = {"property-01", "reference-01"}
        self.assertEqual(query(), validate_workspace_query_readback(query(), generation=current, selected_workspace_ids=selected))
        bare = query()
        bare["evidence"] = ["claim-01"]
        self.assertFalse(Draft202012Validator(self.schema(WORKSPACE_SCHEMA_VERSIONS[4])).is_valid(bare))
        with self.assertRaises(ContractError):
            validate_workspace_query_readback(bare)
        for mutate in (
            lambda item: item["evidence"][0].update(workspace_id="unrelated-01"),
            lambda item: item["evidence"][0].update(graph_id="wrong-graph"),
            lambda item: item.update(consulted_workspace_ids=["property-01"]),
        ):
            changed = query()
            mutate(changed)
            changed = bind(changed, "readback_digest")
            with self.assertRaises(ContractError):
                validate_workspace_query_readback(changed, generation=current, selected_workspace_ids=selected)

    def test_query_evidence_is_unique_and_deterministically_origin_ordered(self):
        changed = query()
        second = deepcopy(changed["evidence"][0])
        second.update(workspace_id="property-01", graph_id="graph-property-01")
        changed["evidence"].append(second)
        changed = bind(changed, "readback_digest")
        with self.assertRaises(ContractError):
            validate_workspace_query_readback(changed)
        duplicate = query()
        duplicate["evidence"].append(deepcopy(duplicate["evidence"][0]))
        duplicate = bind(duplicate, "readback_digest")
        with self.assertRaises(ContractError):
            validate_workspace_query_readback(duplicate)

    def test_duplicate_json_keys_and_nonfinite_json_are_rejected(self):
        with self.assertRaises(ContractError):
            parse_strict_json(b'{"schema_version":"x","schema_version":"y"}', "workspace")
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            with self.assertRaises(ContractError):
                parse_strict_json(b'{"value":' + token + b'}', "workspace")


if __name__ == "__main__":
    unittest.main()
