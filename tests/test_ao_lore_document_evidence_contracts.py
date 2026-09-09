import json
import math
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore._strict_io import ContractError, parse_strict_json
from ao_lore.benchmark import BenchmarkError, canonical_digest
from ao_lore.document_evidence_contracts import (
    AUTHORITY_FIELDS,
    DOCUMENT_EVIDENCE_SCHEMA_VERSIONS,
    validate_workspace_document_generation,
    validate_workspace_document_ingest_readback,
    validate_workspace_document_query_readback,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas" / "ao-lore"


def digest(text: str) -> str:
    return canonical_digest(text)


def bind(value: dict, field: str) -> dict:
    value[field] = canonical_digest({key: item for key, item in value.items() if key != field})
    return value


def authority() -> dict:
    return {field: False for field in AUTHORITY_FIELDS}


def document_ir(document_id: str, text: str = "verified text") -> dict:
    source_digest = digest("source-" + document_id)
    return {
        "schema_version": "ao.lore.document-ir.v0.1",
        "document_id": document_id,
        "source": {"resource": "inbox/manual.pdf", "digest": source_digest, "media_type": "application/pdf"},
        "parser": {"parser_id": "fixture-parser", "parser_version": "1", "configuration_digest": digest("parser")},
        "blocks": [{"id": "block-1", "type": "paragraph", "text": text, "source_span": {"page": 1, "start": 0, "end": len(text), "coordinates": [0.0, 0.0, 1.0, 1.0]}}],
        "metadata": {},
    }


def document_binding(document_id: str) -> dict:
    ir = document_ir(document_id)
    return {
        "document_id": document_id,
        "document_ir_digest": canonical_digest(ir),
        "source_id": "source-" + document_id,
        "source_digest": ir["source"]["digest"],
        "source_record_digest": digest("record-" + document_id),
        "media_type": "application/pdf",
        "authority_role": "operator_procedure",
        "sensitivity": "internal",
        "version": "1",
        "effective_date": "2026-08-13",
        "freshness_status": "current",
        "qualification_codes": ["fixture-only"],
        "document_ir": ir,
    }


def generation(document_ids: tuple[str, ...] = ("a", "b")) -> dict:
    return bind({
        "schema_version": DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[0],
        "generation_id": "documents-workspace-a-0000000001",
        "sequence": 1,
        "prior_generation_digest": None,
        "workspace_id": "workspace-a",
        "document_store_id": "documents-workspace-a",
        "registry_digest": digest("registry"),
        "created_at": "2026-08-13T12:00:00Z",
        "documents": [document_binding(document_id) for document_id in document_ids],
        "generation_digest": digest("placeholder"),
    }, "generation_digest")


class DocumentEvidenceContractTests(unittest.TestCase):
    def schema(self, version: str) -> dict:
        name = version.removeprefix("ao.lore.").replace(".v", "-v") + ".schema.json"
        return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))

    def test_schema_family_is_valid_and_closed(self):
        self.assertEqual(3, len(DOCUMENT_EVIDENCE_SCHEMA_VERSIONS))
        for version in DOCUMENT_EVIDENCE_SCHEMA_VERSIONS:
            schema = self.schema(version)
            Draft202012Validator.check_schema(schema)
            self.assertFalse(schema["additionalProperties"])

    def test_generation_binds_ordered_exact_ir_and_source_digests(self):
        value = generation()
        self.assertTrue(Draft202012Validator(self.schema(value["schema_version"])).is_valid(value))
        validated = validate_workspace_document_generation(value)
        self.assertEqual(["a", "b"], [item["document_id"] for item in validated["documents"]])
        validated["documents"][0]["document_ir"]["blocks"][0]["text"] = "changed"
        self.assertEqual("verified text", value["documents"][0]["document_ir"]["blocks"][0]["text"])

    def test_generation_rejects_duplicates_order_drift_and_ir_digest_drift(self):
        for mutate in (
            lambda value: value["documents"].append(deepcopy(value["documents"][0])),
            lambda value: value["documents"].reverse(),
            lambda value: value["documents"][0].update(document_ir_digest=digest("drift")),
        ):
            value = generation()
            mutate(value)
            bind(value, "generation_digest")
            with self.assertRaises(ContractError):
                validate_workspace_document_generation(value)

    def test_generation_rejects_unknown_authority_private_paths_nonfinite_and_oversized_text(self):
        mutations = (
            lambda value: value.update(extra=True),
            lambda value: value["documents"][0].update(authority_role="administrator"),
            lambda value: value["documents"][0]["document_ir"]["source"].update(resource="/home/customer/manual.pdf"),
            lambda value: value["documents"][0]["document_ir"]["blocks"][0]["source_span"].update(coordinates=[math.nan, 0, 1, 1]),
            lambda value: value["documents"][0]["document_ir"]["blocks"][0].update(text="x" * (1024 * 1024 + 1)),
        )
        for mutate in mutations:
            value = generation()
            mutate(value)
            try:
                value["documents"][0]["document_ir_digest"] = canonical_digest(value["documents"][0]["document_ir"])
                bind(value, "generation_digest")
            except BenchmarkError:
                pass
            with self.assertRaises((ContractError, ValueError)):
                validate_workspace_document_generation(value)

    def test_generation_rejects_unsafe_public_source_resource_shapes(self):
        resources = (
            "../../private/customer/manual.pdf",
            "../private/manual.pdf",
            "./manual.pdf",
            "private/customer/manual.pdf",
            "private-docx/customer.docx",
            "https://example.invalid/manual.pdf",
            "file:///tmp/manual.pdf",
            "C:/customer/manual.pdf",
            "customer\\manual.pdf",
        )
        for resource in resources:
            value = generation()
            value["documents"][0]["document_ir"]["source"]["resource"] = resource
            value["documents"][0]["document_ir_digest"] = canonical_digest(value["documents"][0]["document_ir"])
            bind(value, "generation_digest")
            with self.subTest(resource=resource), self.assertRaises(ContractError):
                validate_workspace_document_generation(value)

    def test_source_resource_schema_and_runtime_share_the_same_locator_grammar(self):
        validator = Draft202012Validator(self.schema(DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[0]))
        safe_resources = ("manual.pdf", "inbox/manual.pdf")
        unsafe_resources = (
            "https://example.invalid/manual.pdf",
            "../manual.pdf",
            "./manual.pdf",
            "private/manual.pdf",
        )

        for resource in safe_resources:
            value = generation()
            value["documents"][0]["document_ir"]["source"]["resource"] = resource
            value["documents"][0]["document_ir_digest"] = canonical_digest(value["documents"][0]["document_ir"])
            bind(value, "generation_digest")
            with self.subTest(resource=resource, expected="safe"):
                self.assertTrue(validator.is_valid(value))
                validated = validate_workspace_document_generation(value)
                self.assertEqual(resource, validated["documents"][0]["document_ir"]["source"]["resource"])

        for resource in unsafe_resources:
            value = generation()
            value["documents"][0]["document_ir"]["source"]["resource"] = resource
            value["documents"][0]["document_ir_digest"] = canonical_digest(value["documents"][0]["document_ir"])
            bind(value, "generation_digest")
            with self.subTest(resource=resource, expected="unsafe"):
                self.assertFalse(validator.is_valid(value))
                with self.assertRaises(ContractError):
                    validate_workspace_document_generation(value)

    def test_document_id_accepts_digest_identity_with_schema_runtime_parity(self):
        document_id = digest("canonical-document")
        current = generation((document_id,))
        current["documents"][0]["source_id"] = "source-canonical"
        current["documents"][0]["source_record_digest"] = digest("record-canonical")
        bind(current, "generation_digest")
        generation_schema = Draft202012Validator(self.schema(current["schema_version"]))
        self.assertTrue(generation_schema.is_valid(current))
        validated = validate_workspace_document_generation(current)
        self.assertEqual(document_id, validated["documents"][0]["document_id"])

        evidence = {
            "evidence_id": digest("evidence"), "workspace_id": "workspace-a",
            "generation_digest": current["generation_digest"], "document_id": document_id,
            "document_ir_digest": current["documents"][0]["document_ir_digest"],
            "source_id": "source-canonical", "source_digest": current["documents"][0]["source_digest"],
            "block_id": "block-1", "block_digest": canonical_digest(current["documents"][0]["document_ir"]["blocks"][0]),
            "render_text": "verified text", "source_span": {"page": 1, "start": 0, "end": 13, "coordinates": [0.0, 0.0, 1.0, 1.0]},
            "authority_role": "operator_procedure", "sensitivity": "internal",
            "freshness_status": "current", "qualification_codes": ["fixture-only"],
        }
        query = bind({
            "schema_version": DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[2], "query_id": "query-a",
            "prompt_digest": digest("prompt"), "workspace_id": "workspace-a",
            "generation_digest": current["generation_digest"], "outcome": "answer",
            "reason_code": "ok", "evidence": [evidence], "qualifications": ["fixture-only"],
            "readback_digest": digest("placeholder"), **authority(),
        }, "readback_digest")
        query_schema = Draft202012Validator(self.schema(query["schema_version"]))
        self.assertTrue(query_schema.is_valid(query))
        self.assertEqual(query, validate_workspace_document_query_readback(query, generation=current, workspace_id="workspace-a"))

        ingest = bind({
            "schema_version": DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[1], "ingest_id": "ingest-a",
            "workspace_id": "workspace-a", "status": "published", "document_id": document_id,
            "source_digest": current["documents"][0]["source_digest"],
            "document_ir_digest": current["documents"][0]["document_ir_digest"],
            "generation_id": current["generation_id"], "generation_digest": current["generation_digest"],
            "readback_digest": digest("placeholder"), **authority(),
        }, "readback_digest")
        ingest_schema = Draft202012Validator(self.schema(ingest["schema_version"]))
        self.assertTrue(ingest_schema.is_valid(ingest))
        self.assertEqual(ingest, validate_workspace_document_ingest_readback(ingest, generation=current, workspace_id="workspace-a"))

    def test_document_id_rejects_malformed_digest_like_values_with_schema_runtime_parity(self):
        current = generation((digest("canonical-document"),))
        current["documents"][0]["source_id"] = "source-canonical"
        current["documents"][0]["source_record_digest"] = digest("record-canonical")
        bind(current, "generation_digest")
        query = bind({
            "schema_version": DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[2], "query_id": "query-a",
            "prompt_digest": digest("prompt"), "workspace_id": "workspace-a",
            "generation_digest": current["generation_digest"], "outcome": "answer",
            "reason_code": "ok", "evidence": [{
                "evidence_id": digest("evidence"), "workspace_id": "workspace-a",
                "generation_digest": current["generation_digest"], "document_id": current["documents"][0]["document_id"],
                "document_ir_digest": current["documents"][0]["document_ir_digest"],
                "source_id": "source-canonical", "source_digest": current["documents"][0]["source_digest"],
                "block_id": "block-1", "block_digest": canonical_digest(current["documents"][0]["document_ir"]["blocks"][0]),
                "render_text": "verified text", "source_span": {"page": 1, "start": 0, "end": 13, "coordinates": [0.0, 0.0, 1.0, 1.0]},
                "authority_role": "operator_procedure", "sensitivity": "internal",
                "freshness_status": "current", "qualification_codes": ["fixture-only"],
            }], "qualifications": ["fixture-only"], "readback_digest": digest("placeholder"), **authority(),
        }, "readback_digest")
        ingest = bind({
            "schema_version": DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[1], "ingest_id": "ingest-a",
            "workspace_id": "workspace-a", "status": "published", "document_id": current["documents"][0]["document_id"],
            "source_digest": current["documents"][0]["source_digest"],
            "document_ir_digest": current["documents"][0]["document_ir_digest"],
            "generation_id": current["generation_id"], "generation_digest": current["generation_digest"],
            "readback_digest": digest("placeholder"), **authority(),
        }, "readback_digest")
        bad_document_ids = (
            "sha256:" + "A" * 64,
            "SHA256:" + "a" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
        )

        for bad_document_id in bad_document_ids:
            changed_generation = generation((bad_document_id,))
            changed_generation["documents"][0]["source_id"] = "source-canonical"
            changed_generation["documents"][0]["source_record_digest"] = digest("record-canonical")
            bind(changed_generation, "generation_digest")
            with self.subTest(surface="generation", document_id=bad_document_id):
                self.assertFalse(Draft202012Validator(self.schema(changed_generation["schema_version"])).is_valid(changed_generation))
                with self.assertRaises(ContractError):
                    validate_workspace_document_generation(changed_generation)

            changed_query = deepcopy(query)
            changed_query["evidence"][0]["document_id"] = bad_document_id
            bind(changed_query, "readback_digest")
            with self.subTest(surface="query", document_id=bad_document_id):
                self.assertFalse(Draft202012Validator(self.schema(changed_query["schema_version"])).is_valid(changed_query))
                with self.assertRaises(ContractError):
                    validate_workspace_document_query_readback(changed_query, generation=current, workspace_id="workspace-a")

            changed_ingest = deepcopy(ingest)
            changed_ingest["document_id"] = bad_document_id
            bind(changed_ingest, "readback_digest")
            with self.subTest(surface="ingest", document_id=bad_document_id):
                self.assertFalse(Draft202012Validator(self.schema(changed_ingest["schema_version"])).is_valid(changed_ingest))
                with self.assertRaises(ContractError):
                    validate_workspace_document_ingest_readback(changed_ingest, generation=current, workspace_id="workspace-a")

    def test_query_and_ingest_bind_exact_generation_workspace_and_false_authority(self):
        current = generation()
        evidence = {
            "evidence_id": digest("evidence"), "workspace_id": "workspace-a",
            "generation_digest": current["generation_digest"], "document_id": "a",
            "document_ir_digest": current["documents"][0]["document_ir_digest"],
            "source_id": "source-a", "source_digest": current["documents"][0]["source_digest"],
            "block_id": "block-1", "block_digest": canonical_digest(current["documents"][0]["document_ir"]["blocks"][0]),
            "render_text": "verified text", "source_span": {"page": 1, "start": 0, "end": 13, "coordinates": [0.0, 0.0, 1.0, 1.0]},
            "authority_role": "operator_procedure", "sensitivity": "internal",
            "freshness_status": "current", "qualification_codes": ["fixture-only"],
        }
        query = bind({
            "schema_version": DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[2], "query_id": "query-a",
            "prompt_digest": digest("prompt"), "workspace_id": "workspace-a",
            "generation_digest": current["generation_digest"], "outcome": "answer",
            "reason_code": "ok", "evidence": [evidence], "qualifications": ["fixture-only"],
            "readback_digest": digest("placeholder"), **authority(),
        }, "readback_digest")
        self.assertEqual(query, validate_workspace_document_query_readback(query, generation=current, workspace_id="workspace-a"))
        ingest = bind({
            "schema_version": DOCUMENT_EVIDENCE_SCHEMA_VERSIONS[1], "ingest_id": "ingest-a",
            "workspace_id": "workspace-a", "status": "published", "document_id": "a",
            "source_digest": current["documents"][0]["source_digest"],
            "document_ir_digest": current["documents"][0]["document_ir_digest"],
            "generation_id": current["generation_id"], "generation_digest": current["generation_digest"],
            "readback_digest": digest("placeholder"), **authority(),
        }, "readback_digest")
        self.assertEqual(ingest, validate_workspace_document_ingest_readback(ingest, generation=current, workspace_id="workspace-a"))
        for value, validator in ((query, validate_workspace_document_query_readback), (ingest, validate_workspace_document_ingest_readback)):
            changed = deepcopy(value); changed["promotion"] = True; bind(changed, "readback_digest")
            with self.assertRaises(ContractError):
                validator(changed, generation=current, workspace_id="workspace-a")

        changed = deepcopy(query)
        changed["evidence"][0]["evidence_id"] = "evidence-01"
        bind(changed, "readback_digest")
        with self.assertRaises(ContractError):
            validate_workspace_document_query_readback(changed, generation=current, workspace_id="workspace-a")

    def test_duplicate_json_keys_fail_before_runtime_validation(self):
        with self.assertRaises(ContractError):
            parse_strict_json(b'{"schema_version":"a","schema_version":"b"}', "document generation")


if __name__ == "__main__":
    unittest.main()
