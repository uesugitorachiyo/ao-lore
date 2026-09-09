import json
import re
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore._strict_io import ContractError, parse_strict_json
from tests.schema_registry import cross_file_references


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"


def _resolve_local_ref(root, reference):
    value = root
    for component in reference.removeprefix("#/").split("/"):
        value = value[component.replace("~1", "/").replace("~0", "~")]
    return value


def _schema_accepts(schema, value, root=None):
    """Validate the dependency-free JSON Schema subset used by these contracts."""

    root = schema if root is None else root
    if "$ref" in schema:
        return _schema_accepts(_resolve_local_ref(root, schema["$ref"]), value, root)
    if "oneOf" in schema:
        return sum(_schema_accepts(branch, value, root) for branch in schema["oneOf"]) == 1
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False

    expected_type = schema.get("type")
    type_matches = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "null": lambda item: item is None,
    }
    if expected_type is not None and not type_matches[expected_type](value):
        return False
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            return False
        if len(value) > schema.get("maxLength", len(value)):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
    if isinstance(value, int) and not isinstance(value, bool):
        if value < schema.get("minimum", value):
            return False
        if value > schema.get("maximum", value):
            return False
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            return False
        if len(value) > schema.get("maxItems", len(value)):
            return False
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                return False
        if "items" in schema and any(
            not _schema_accepts(schema["items"], item, root) for item in value
        ):
            return False
    if isinstance(value, dict):
        required = set(schema.get("required", []))
        if not required.issubset(value):
            return False
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and not set(value).issubset(properties):
            return False
        for key, child in value.items():
            if key in properties and not _schema_accepts(properties[key], child, root):
                return False
    return True


def _assert_closed_objects(testcase, schema):
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            testcase.assertIs(schema.get("additionalProperties"), False)
        for value in schema.values():
            _assert_closed_objects(testcase, value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_closed_objects(testcase, value)


def _has_duplicate_document_field(manifest, field):
    values = [document[field] for document in manifest["documents"]]
    return len(values) != len(set(values))


class AOLoreContractTests(unittest.TestCase):
    expected_schemas = {
        "document-ir.schema.json",
        "parser-capability-profile.schema.json",
        "parser-selection-profile.schema.json",
        "parser-selection-report.schema.json",
        "parse-quality-profile.schema.json",
        "parse-quality-report.schema.json",
        "parser-benchmark-manifest.schema.json",
        "evidence-requirement-plan.schema.json",
        "evidence-coverage-report.schema.json",
        "model-role-config.schema.json",
        "model-call-trace.schema.json",
        "okf-candidate.schema.json",
        "attempt-v0.1.schema.json",
        "comparison-v0.1.schema.json",
        "evaluation-v0.1.schema.json",
        "monitoring-baseline-v0.1.schema.json",
        "monitoring-observation-v0.1.schema.json",
        "monitoring-verdict-v0.1.schema.json",
        "candidate-provenance-v0.1.schema.json",
        "candidate-provenance-v0.2.schema.json",
        "candidate-review-event-v0.1.schema.json",
        "candidate-inspection-v0.1.schema.json",
        "single-document-ingest-readback-v0.1.schema.json",
        "candidate-queue-item-v0.1.schema.json",
        "candidate-queue-readback-v0.1.schema.json",
        "ingest-batch-manifest-v0.1.schema.json",
        "ingest-batch-manifest-v0.2.schema.json",
        "ingest-batch-manifest-v0.3.schema.json",
        "ingest-batch-checkpoint-v0.1.schema.json",
        "ingest-batch-readback-v0.1.schema.json",
        "private-pdf-uat-manifest-v0.1.schema.json",
        "private-pdf-uat-readback-v0.1.schema.json",
        "private-pdf-domain-review-v0.1.schema.json",
        "private-docx-domain-review-v0.1.schema.json",
        "private-docx-expectation-v0.1.schema.json",
        "private-docx-uat-readback-v0.1.schema.json",
        "private-ocr-runtime-v0.1.schema.json",
        "private-ocr-model-set-v0.1.schema.json",
        "private-ocr-raster-manifest-v0.1.schema.json",
        "private-ocr-worker-contract-v0.1.schema.json",
        "private-ocr-worker-result-v0.1.schema.json",
        "private-ocr-qualification-v0.1.schema.json",
        "private-ocr-uat-readback-v0.1.schema.json",
        "okf-canonical-entry-v0.2.schema.json",
        "brain-generation-manifest-v0.1.schema.json",
        "promotion-proposal-v0.1.schema.json",
        "promotion-proposal-v0.2.schema.json",
        "promotion-authorization-v0.1.schema.json",
        "promotion-transaction-v0.1.schema.json",
        "promotion-authorization-consumption-v0.1.schema.json",
        "promotion-audit-event-v0.1.schema.json",
        "promotion-recovery-event-v0.1.schema.json",
        "promotion-inspection-v0.1.schema.json",
        "promotion-operation-readback-v0.1.schema.json",
        "rollback-proposal-v0.1.schema.json",
        "okf-candidate-v0.2.schema.json",
        "distillation-result-v0.2.schema.json",
        "okf-canonical-entry-v0.3.schema.json",
        "canonical-claim-set-v0.1.schema.json",
        "canonical-citation-set-v0.1.schema.json",
        "knowledge-snapshot-v0.1.schema.json",
        "knowledge-search-readback-v0.1.schema.json",
        "knowledge-evidence-projection-v0.1.schema.json",
        "knowledge-answer-readback-v0.1.schema.json",
        "knowledge-status-readback-v0.1.schema.json",
        "okf-canonical-entry-v0.4.schema.json",
        "knowledge-snapshot-v0.2.schema.json",
        "knowledge-search-readback-v0.2.schema.json",
        "knowledge-evidence-projection-v0.2.schema.json",
        "knowledge-answer-readback-v0.2.schema.json",
        "knowledge-status-readback-v0.2.schema.json",
        "candidate-quality-campaign-v0.1.schema.json",
        "candidate-quality-sampling-policy-v0.1.schema.json",
        "candidate-quality-sample-v0.1.schema.json",
        "candidate-quality-annotations-v0.1.schema.json",
        "candidate-quality-questions-v0.1.schema.json",
        "candidate-quality-question-results-v0.1.schema.json",
        "candidate-quality-result-v0.1.schema.json",
        "candidate-quality-summary-v0.1.schema.json",
        "candidate-quality-recovery-v0.1.schema.json",
        "evidence-source-registry-v0.1.schema.json",
        "evidence-acquisition-record-v0.1.schema.json",
        "evidence-authority-role-v0.1.schema.json",
        "evidence-relationship-edge-v0.1.schema.json",
        "evidence-operational-question-v0.1.schema.json",
        "evidence-graph-manifest-v0.1.schema.json",
        "evidence-graph-inspection-v0.1.schema.json",
        "evidence-query-readback-v0.1.schema.json",
        "evidence-campaign-summary-v0.1.schema.json",
        "evidence-recovery-v0.1.schema.json",
        "evidence-freshness-policy-v0.1.schema.json",
        "evidence-freshness-observation-v0.1.schema.json",
        "evidence-freshness-comparison-v0.1.schema.json",
        "evidence-freshness-summary-v0.1.schema.json",
        "workspace-definition-v0.1.schema.json",
        "workspace-registry-generation-v0.1.schema.json",
        "workspace-registry-inspection-v0.1.schema.json",
        "workspace-operation-readback-v0.1.schema.json",
        "workspace-query-readback-v0.1.schema.json",
        "workspace-document-generation-v0.1.schema.json",
        "workspace-document-ingest-readback-v0.1.schema.json",
        "workspace-document-query-readback-v0.1.schema.json",
        "workspace-definition-v0.2.schema.json",
        "workspace-query-readback-v0.2.schema.json",
        "evidence-selection-request-v0.1.schema.json",
        "evidence-selection-proposal-v0.1.schema.json",
        "evidence-selection-authorization-v0.1.schema.json",
        "evidence-selection-consumption-v0.1.schema.json",
        "evidence-selection-transaction-v0.1.schema.json",
        "evidence-selection-inspection-v0.1.schema.json",
        "evidence-selection-recovery-v0.1.schema.json",
        "okf-candidate-v0.3.schema.json",
        "candidate-provenance-v0.3.schema.json",
        "public-release-policy-v0.1.schema.json",
        "public-release-manifest-v0.1.schema.json",
        "public-release-readiness-v0.1.schema.json",
        "sanitized-lifecycle-rehearsal-v0.1.schema.json",
    }

    def load(self, name: str) -> dict:
        with (SCHEMA_ROOT / name).open(encoding="utf-8") as handle:
            return json.load(handle)

    def test_complete_schema_family_exists_and_uses_draft_2020_12(self):
        self.assertEqual(
            self.expected_schemas,
            {path.name for path in SCHEMA_ROOT.glob("*.schema.json")},
        )
        for name in self.expected_schemas:
            schema = self.load(name)
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema",
                schema["$schema"],
                name,
            )
            self.assertEqual(
                f"https://schemas.ao-lore.example/{name}",
                schema["$id"],
            )
        references = cross_file_references()
        self.assertEqual(11, len(references))
        self.assertEqual(11, len(set(references)))

    def test_capability_profile_is_extensible_but_core_identity_is_strict(self):
        schema = self.load("parser-capability-profile.schema.json")
        self.assertTrue(schema["additionalProperties"])
        self.assertEqual(
            {"schema_version", "parser_id", "parser_version", "formats", "document_ir_versions", "benchmark_refs"},
            set(schema["required"]),
        )
        format_properties = schema["properties"]["formats"]["items"]["properties"]
        for field in (
            "mime_types", "extensions", "structural_elements", "native_status",
            "ocr", "layout_aware", "tables", "links", "footnotes",
            "source_coordinates", "images", "captions", "languages",
        ):
            self.assertIn(field, format_properties)

    def test_selection_and_quality_scores_are_distinct_contracts(self):
        selection = self.load("parser-selection-report.schema.json")
        quality = self.load("parse-quality-report.schema.json")
        self.assertIn("selection_score", selection["properties"]["candidates"]["items"]["properties"])
        self.assertNotIn("quality_score", selection["properties"])
        self.assertIn("overall_quality_score", quality["properties"])
        self.assertNotIn("selection_score", quality["properties"])
        self.assertEqual(
            ["accept", "fallback", "quarantine", "reject"],
            quality["properties"]["decision"]["enum"],
        )

    def test_coverage_contract_keeps_requirement_states_and_hard_gates(self):
        schema = self.load("evidence-coverage-report.schema.json")
        state = schema["properties"]["requirements"]["items"]["properties"]["status"]
        self.assertEqual(
            ["satisfied", "partially_satisfied", "missing", "contradictory", "stale_only", "disqualified"],
            state["enum"],
        )
        gates = schema["properties"]["gates"]["required"]
        self.assertEqual(
            {"coverage_target_met", "mandatory_requirements_met", "provenance_present", "citations_present", "freshness_met", "trust_met", "no_hard_contradiction", "deterministic_validation_passed"},
            set(gates),
        )

    def test_model_roles_are_explicit_and_independently_configured(self):
        schema = self.load("model-role-config.schema.json")
        self.assertEqual(
            ["parser", "distiller", "navigator", "synthesizer"],
            schema["properties"]["role"]["enum"],
        )
        for field in (
            "adapter", "provider", "model_id", "policy_version", "output_schema_version",
            "context_limit", "output_limit", "temperature", "timeout_ms", "retries",
            "network_policy", "privacy_policy", "token_budget", "monetary_budget",
            "fallback_policy", "cache_policy",
        ):
            self.assertIn(field, schema["required"])
        self.assertIn("no_implicit_cross_role_inheritance", schema["required"])

    def test_architecture_contains_required_algorithms_and_sequences(self):
        text = (ROOT / "docs" / "architecture" / "ao-lore.md").read_text(encoding="utf-8")
        for phrase in (
            "Eligibility filtering",
            "Capability scoring",
            "Numeric parse-quality gates",
            "Evidence coverage",
            "Model-role separation",
            "Docling versus challenger benchmark",
            "Primary parse passes quality gate",
            "Primary parse invokes fallback",
            "Coverage reached before depth",
            "Depth exhausted without coverage",
            "Coverage-gap delta replan",
            "Parser to IR to distiller",
            "Navigator to ledger to synthesizer",
            "Mixed local and frontier deployment",
            "Role failure without cross-role fallback",
        ):
            self.assertIn(phrase, text)

    def test_candidate_persistence_schema_family_is_strict(self):
        provenance = self.load("candidate-provenance-v0.1.schema.json")
        provenance_v2 = self.load("candidate-provenance-v0.2.schema.json")
        event = self.load("candidate-review-event-v0.1.schema.json")
        inspection = self.load("candidate-inspection-v0.1.schema.json")
        for schema in (provenance, provenance_v2, event, inspection):
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema",
                schema["$schema"],
            )
            self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            {
                "format_id",
                "corpus_id",
                "original_digest",
                "derived_digest",
                "transformation_id",
                "expectation_digest",
            },
            set(provenance_v2["properties"]["source_origin"]["required"]),
        )
        self.assertEqual(
            {"const": "docx"},
            provenance_v2["properties"]["source_origin"]["properties"]["format_id"],
        )
        self.assertEqual(["accept", "reject"], event["properties"]["decision"]["enum"])
        self.assertEqual(
            [{"type": "null"}, {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}],
            event["properties"]["previous_event_digest"]["oneOf"],
        )
        self.assertFalse(inspection["properties"]["canonical"]["const"])
        self.assertFalse(inspection["properties"]["promotion_authority"]["const"])

    def test_candidate_provenance_v0_2_accepts_only_exact_docx_origin(self):
        schema = self.load("candidate-provenance-v0.2.schema.json")
        valid = {
            "schema_version": "ao.lore.candidate-provenance.v0.2",
            "candidate_id": "candidate-docx-01",
            "candidate_digest": "sha256:" + "1" * 64,
            "document_ir_digest": "sha256:" + "2" * 64,
            "source_digest": "sha256:" + "3" * 64,
            "parser_id": "native-docx-ooxml",
            "parser_version": "1.0.0",
            "parse_quality_report_digest": "sha256:" + "4" * 64,
            "parser_selection_report_digest": "sha256:" + "5" * 64,
            "distillation_trace_digest": "sha256:" + "6" * 64,
            "created_at": "2026-08-09T15:00:00Z",
            "source_origin": {
                "format_id": "docx",
                "corpus_id": "docx-nomagic-uk-public-sector-v1",
                "original_digest": "sha256:" + "7" * 64,
                "derived_digest": "sha256:" + "3" * 64,
                "transformation_id": "restore-ooxml-local-header-v1",
                "expectation_digest": "sha256:" + "8" * 64,
            },
        }
        self.assertTrue(_schema_accepts(schema, valid))
        for field, value in (
            ("format_id", "pdf"),
            ("corpus_id", "other"),
            ("transformation_id", "other"),
        ):
            changed = deepcopy(valid)
            changed["source_origin"][field] = value
            self.assertFalse(_schema_accepts(schema, changed), (field, value))

    def test_ingest_and_queue_schema_family_is_strict(self):
        ingest = self.load("single-document-ingest-readback-v0.1.schema.json")
        item = self.load("candidate-queue-item-v0.1.schema.json")
        queue = self.load("candidate-queue-readback-v0.1.schema.json")
        for schema in (ingest, item, queue):
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema",
                schema["$schema"],
            )
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual({"const": False}, schema["properties"]["canonical"])
            self.assertEqual(
                {"const": False},
                schema["properties"]["promotion_authority"],
            )
        self.assertEqual(
            "ao.lore.single-document-ingest-readback.v0.1",
            ingest["properties"]["schema_version"]["const"],
        )
        self.assertEqual(
            "ao.lore.candidate-queue-item.v0.1",
            item["properties"]["schema_version"]["const"],
        )
        self.assertEqual(
            "ao.lore.candidate-queue-readback.v0.1",
            queue["properties"]["schema_version"]["const"],
        )
        self.assertEqual(
            {
                "schema_version", "status", "candidate_id", "candidate_digest",
                "provenance_digest", "source_digest", "parser_id", "parser_version",
                "document_ir_digest", "parser_selection_report_digest",
                "parse_quality_report_digest", "distillation_trace_digest",
                "review_status", "next_commands", "canonical", "promotion_authority",
            },
            set(ingest["required"]),
        )
        self.assertEqual(
            {
                "schema_version", "candidate_id", "candidate_digest",
                "provenance_digest", "created_at", "source_digest", "parser_id",
                "parser_version", "review_status", "verified_review_events",
                "latest_event_digest", "canonical", "promotion_authority",
            },
            set(item["required"]),
        )
        self.assertEqual(
            {
                "schema_version", "requested_status", "limit", "after",
                "returned_count", "items", "next_after", "canonical",
                "promotion_authority",
            },
            set(queue["required"]),
        )
        self.assertEqual(
            ["unreviewed", "accepted", "rejected", "all"],
            queue["properties"]["requested_status"]["enum"],
        )
        self.assertEqual(["created", "unchanged"], ingest["properties"]["status"]["enum"])
        self.assertEqual(
            ["unreviewed", "accepted", "rejected"],
            ingest["properties"]["review_status"]["enum"],
        )
        self.assertEqual(
            ["unreviewed", "accepted", "rejected"],
            item["properties"]["review_status"]["enum"],
        )
        self.assertEqual(200, queue["properties"]["limit"]["maximum"])
        self.assertEqual(200, queue["properties"]["items"]["maxItems"])
        self.assertEqual(
            {"$ref": "#/$defs/queue_item"},
            queue["properties"]["items"]["items"],
        )
        self.assertEqual(
            {
                key: item[key]
                for key in ("type", "additionalProperties", "required", "properties")
            },
            queue["$defs"]["queue_item"],
        )
        self.assertEqual(200, queue["properties"]["returned_count"]["maximum"])
        self.assertEqual(999999, item["properties"]["verified_review_events"]["maximum"])
        self.assertEqual(
            "^sha256:[0-9a-f]{64}$",
            ingest["properties"]["candidate_digest"]["pattern"],
        )
        self.assertEqual(
            [{"type": "null"}, {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}],
            item["properties"]["latest_event_digest"]["oneOf"],
        )
        identifier_or_null = [
            {"type": "null"},
            {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{0,127}$"},
        ]
        self.assertEqual(identifier_or_null, queue["properties"]["after"]["oneOf"])
        self.assertEqual(identifier_or_null, queue["properties"]["next_after"]["oneOf"])

    def test_queue_item_reference_is_self_contained_and_preserves_strict_contract(self):
        queue = self.load("candidate-queue-readback-v0.1.schema.json")
        item = self.load("candidate-queue-item-v0.1.schema.json")

        def references(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "$ref":
                        yield child
                    yield from references(child)
            elif isinstance(value, list):
                for child in value:
                    yield from references(child)

        self.assertEqual(["#/$defs/queue_item"], list(references(queue)))
        embedded = queue["$defs"]["queue_item"]
        standalone = {
            key: item[key]
            for key in ("type", "additionalProperties", "required", "properties")
        }
        self.assertEqual(standalone, embedded)
        self.assertFalse(embedded["additionalProperties"])
        self.assertEqual({"const": False}, embedded["properties"]["canonical"])
        self.assertEqual(
            {"const": False},
            embedded["properties"]["promotion_authority"],
        )

    def test_batch_ingestion_schema_family_is_strict(self):
        names = (
            "ingest-batch-manifest-v0.1.schema.json",
            "ingest-batch-manifest-v0.2.schema.json",
            "ingest-batch-manifest-v0.3.schema.json",
            "ingest-batch-checkpoint-v0.1.schema.json",
            "ingest-batch-readback-v0.1.schema.json",
        )
        manifest, manifest_v2, manifest_v3, checkpoint, readback = (
            self.load(name) for name in names
        )
        for name, schema in zip(
            names, (manifest, manifest_v2, manifest_v3, checkpoint, readback)
        ):
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema", schema["$schema"]
            )
            self.assertEqual(f"https://schemas.ao-lore.example/{name}", schema["$id"])
            _assert_closed_objects(self, schema)

        documents = manifest["properties"]["documents"]
        self.assertEqual((1, 100), (documents["minItems"], documents["maxItems"]))
        self.assertTrue(documents["uniqueItems"])
        document_comment = documents["$comment"].lower()
        for invariant in ("runtime", "item_id", "source"):
            self.assertIn(invariant, document_comment)
        self.assertEqual({"const": True}, manifest["properties"]["continue_on_error"])
        self.assertEqual({"const": "ocr"}, manifest_v3["properties"]["format_id"])
        self.assertEqual(
            {"const": "paddle-ocr-english"},
            manifest_v3["properties"]["parser_id"],
        )
        for field in ("canonical", "promotion_authority"):
            self.assertEqual({"const": False}, checkpoint["properties"][field])
            self.assertEqual({"const": False}, readback["properties"][field])
        checkpoint_comment = checkpoint["$comment"].lower()
        for invariant in ("runtime", "manifest_items", "items", "identity", "arithmetic"):
            self.assertIn(invariant, checkpoint_comment)
        readback_comment = readback["$comment"].lower()
        for invariant in ("runtime", "count", "order"):
            self.assertIn(invariant, readback_comment)

        success_required = {
            "item_id", "source_digest", "status", "candidate_id",
            "candidate_digest", "provenance_digest", "review_status",
        }
        rejection_required = {"item_id", "source_digest", "status", "error_code"}
        for schema in (checkpoint, readback):
            branches = schema["properties"]["items"]["items"]["oneOf"]
            self.assertEqual(
                ["#/$defs/success_item", "#/$defs/rejected_item"],
                [branch["$ref"] for branch in branches],
            )
            self.assertEqual(success_required, set(schema["$defs"]["success_item"]["required"]))
            self.assertEqual(rejection_required, set(schema["$defs"]["rejected_item"]["required"]))

        digest_a = "sha256:" + "a" * 64
        digest_b = "sha256:" + "b" * 64
        document = {"item_id": "guide", "source": "sources/guides/guide.pdf", "source_digest": digest_a}
        valid_manifest = {
            "schema_version": "ao.lore.ingest-batch-manifest.v0.1",
            "batch_id": "batch-guides-01",
            "documents": [document],
            "continue_on_error": True,
        }
        self.assertTrue(_schema_accepts(manifest, valid_manifest))
        with_context = deepcopy(valid_manifest)
        with_context["documents"][0]["candidate_context"] = {
            "schema_version": "ao.lore.candidate-comparison-context.v0.1",
            "candidates": [],
        }
        self.assertTrue(_schema_accepts(manifest, with_context))
        with_policy = deepcopy(valid_manifest)
        with_policy["documents"][0]["knowledge_policy"] = {
            "sensitivity": "public",
            "stale_after": None,
        }
        self.assertTrue(_schema_accepts(manifest, with_policy))
        policy_definition = manifest["$defs"]["knowledge_policy"]
        self.assertEqual(
            {"sensitivity", "stale_after"},
            set(policy_definition["required"]),
        )
        self.assertIs(policy_definition["additionalProperties"], False)
        for invalid_policy in (
            {"sensitivity": "public"},
            {"sensitivity": "secret", "stale_after": None},
            {"sensitivity": "public", "stale_after": None, "extra": True},
            {"sensitivity": "public", "stale_after": "2027-01-01T00:00:00+00:00"},
        ):
            invalid_policy_manifest = deepcopy(valid_manifest)
            invalid_policy_manifest["documents"][0]["knowledge_policy"] = invalid_policy
            self.assertFalse(_schema_accepts(manifest, invalid_policy_manifest))

        invalid_manifests = []
        for source in ("/sources/guide.pdf", r"sources\guide.pdf", "sources/../guide.pdf", "sources/./guide.pdf", "sources//guide.pdf"):
            value = deepcopy(valid_manifest)
            value["documents"][0]["source"] = source
            invalid_manifests.append(value)
        malformed_digest = deepcopy(valid_manifest)
        malformed_digest["documents"][0]["source_digest"] = "sha256:ABC"
        invalid_manifests.append(malformed_digest)
        widened = deepcopy(valid_manifest)
        widened["continue_on_error"] = False
        invalid_manifests.append(widened)
        empty = deepcopy(valid_manifest)
        empty["documents"] = []
        invalid_manifests.append(empty)
        oversized = deepcopy(valid_manifest)
        oversized["documents"] = [
            {
                "item_id": f"guide-{index}",
                "source": f"sources/guides/guide-{index}.pdf",
                "source_digest": digest_a,
            }
            for index in range(101)
        ]
        invalid_manifests.append(oversized)
        duplicated = deepcopy(valid_manifest)
        duplicated["documents"].append(deepcopy(document))
        invalid_manifests.append(duplicated)
        for value in invalid_manifests:
            self.assertFalse(_schema_accepts(manifest, value), value)

        docx_documents = manifest_v2["properties"]["documents"]
        self.assertEqual((1, 100), (docx_documents["minItems"], docx_documents["maxItems"]))
        self.assertTrue(docx_documents["uniqueItems"])
        for field, expected in (
            ("schema_version", {"const": "ao.lore.ingest-batch-manifest.v0.2"}),
            ("format_id", {"const": "docx"}),
            (
                "media_type",
                {
                    "const": (
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    )
                },
            ),
            ("parser_id", {"const": "native-docx-ooxml"}),
            ("parser_version", {"const": "1.0.0"}),
            ("continue_on_error", {"const": True}),
        ):
            self.assertEqual(expected, manifest_v2["properties"][field])
        docx_comment = docx_documents["$comment"].lower()
        for invariant in ("runtime", "item_id", "source"):
            self.assertIn(invariant, docx_comment)
        valid_docx_manifest = {
            "schema_version": "ao.lore.ingest-batch-manifest.v0.2",
            "batch_id": "batch-guides-01",
            "format_id": "docx",
            "media_type": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            "parser_id": "native-docx-ooxml",
            "parser_version": "1.0.0",
            "documents": [
                {
                    "item_id": "guide",
                    "source": "sources/guides/guide.docx",
                    "source_digest": digest_a,
                }
            ],
            "continue_on_error": True,
        }
        self.assertTrue(_schema_accepts(manifest_v2, valid_docx_manifest))
        for field, value in (
            ("format_id", "pdf"),
            ("media_type", "application/pdf"),
            ("parser_id", "docling"),
            ("parser_version", "2.118.1"),
            ("continue_on_error", False),
        ):
            changed = deepcopy(valid_docx_manifest)
            changed[field] = value
            self.assertFalse(_schema_accepts(manifest_v2, changed), (field, value))
        for locator in (
            "/sources/guide.docx",
            r"sources\guide.docx",
            "sources/../guide.docx",
            "sources/./guide.docx",
            "sources//guide.docx",
            "sources/guide.pdf",
        ):
            changed = deepcopy(valid_docx_manifest)
            changed["documents"][0]["source"] = locator
            self.assertFalse(_schema_accepts(manifest_v2, changed), locator)

        duplicate_item_id = deepcopy(valid_manifest)
        duplicate_item_id["documents"].append({
            "item_id": "guide",
            "source": "sources/guides/alternate.pdf",
            "source_digest": digest_b,
        })
        duplicate_source = deepcopy(valid_manifest)
        duplicate_source["documents"].append({
            "item_id": "alternate",
            "source": "sources/guides/guide.pdf",
            "source_digest": digest_b,
        })
        for field, sample in (
            ("item_id", duplicate_item_id),
            ("source", duplicate_source),
        ):
            self.assertTrue(_has_duplicate_document_field(sample, field))
            self.assertIn(field, document_comment)

        success = {
            "item_id": "guide", "source_digest": digest_a, "status": "created",
            "candidate_id": "candidate-guide", "candidate_digest": digest_b,
            "provenance_digest": digest_a, "review_status": "unreviewed",
        }
        rejected = {
            "item_id": "appendix", "source_digest": digest_b, "status": "rejected",
            "error_code": "source_digest_mismatch",
        }
        valid_readback = {
            "schema_version": "ao.lore.ingest-batch-readback.v0.1",
            "batch_id": "batch-guides-01", "manifest_digest": digest_a,
            "status": "partial", "total": 2, "created": 1, "unchanged": 0,
            "rejected": 1, "processed": 2, "items": [success, rejected],
            "next_commands": [
                "ao-lore candidate inspect --candidate-id candidate-guide",
                "ao-lore candidate review --candidate-id candidate-guide "
                "--decision accept --reviewer <reviewer-id>",
                "ao-lore candidate list --json",
            ],
            "canonical": False, "promotion_authority": False,
        }
        self.assertTrue(_schema_accepts(readback, valid_readback))
        for invalid_command in (
            "ao-lore candidate inspect candidate-guide",
            "ao-lore candidate review --candidate-id candidate-guide --decision reject --reviewer <reviewer-id>",
            "ao-lore candidate review --candidate-id candidate-guide --decision accept --reviewer alice",
            "ao-lore candidate queue",
        ):
            invalid_commands = deepcopy(valid_readback)
            invalid_commands["next_commands"] = [invalid_command]
            self.assertFalse(_schema_accepts(readback, invalid_commands), invalid_command)
        mixed = deepcopy(valid_readback)
        mixed["items"][1]["candidate_id"] = "candidate-appendix"
        self.assertFalse(_schema_accepts(readback, mixed))
        bad_count_shape = deepcopy(valid_readback)
        bad_count_shape["processed"] = -1
        self.assertFalse(_schema_accepts(readback, bad_count_shape))
        authority_widening = deepcopy(valid_readback)
        authority_widening["promotion_authority"] = True
        self.assertFalse(_schema_accepts(readback, authority_widening))

        valid_checkpoint = {
            "schema_version": "ao.lore.ingest-batch-checkpoint.v0.1",
            "batch_id": "batch-guides-01", "manifest_digest": digest_a,
            "manifest_items": [{"item_id": "guide", "source_digest": digest_a}],
            "items": [success], "created": 1, "unchanged": 0, "rejected": 0,
            "processed": 1, "next_item_index": 1,
            "previous_checkpoint_digest": None, "checkpoint_digest": digest_b,
            "canonical": False, "promotion_authority": False,
        }
        self.assertTrue(_schema_accepts(checkpoint, valid_checkpoint))
        for field, value in (
            ("previous_checkpoint_digest", "sha256:ABC"),
            ("next_item_index", -1),
            ("next_item_index", 101),
            ("canonical", True),
            ("promotion_authority", True),
        ):
            invalid_checkpoint = deepcopy(valid_checkpoint)
            invalid_checkpoint[field] = value
            self.assertFalse(_schema_accepts(checkpoint, invalid_checkpoint), (field, value))
        mixed_checkpoint = deepcopy(valid_checkpoint)
        mixed_checkpoint["items"][0]["error_code"] = "document_rejected"
        self.assertFalse(_schema_accepts(checkpoint, mixed_checkpoint))

    def test_private_pdf_uat_schema_family_is_strict(self):
        names = (
            "private-pdf-uat-manifest-v0.1.schema.json",
            "private-pdf-uat-readback-v0.1.schema.json",
        )
        manifest, readback = (self.load(name) for name in names)
        for name, schema in zip(names, (manifest, readback)):
            self.assertEqual(
                "https://json-schema.org/draft/2020-12/schema", schema["$schema"]
            )
            self.assertEqual(f"https://schemas.ao-lore.example/{name}", schema["$id"])
            _assert_closed_objects(self, schema)

        self.assertEqual(
            {
                "schema_version",
                "corpus_id",
                "parser_id",
                "parser_version",
                "ocr_enabled",
                "documents",
                "provider_calls",
                "promotion_authority",
                "claims_authority_advance",
            },
            set(manifest["required"]),
        )
        documents = manifest["properties"]["documents"]
        self.assertEqual((1, 20), (documents["minItems"], documents["maxItems"]))
        self.assertTrue(documents["uniqueItems"])
        manifest_comment = documents["$comment"].lower()
        for invariant in ("runtime", "item_id", "file", "expected_text", "required_block_types", "ordered", "sequence"):
            self.assertIn(invariant, manifest_comment)
        for field, expected in (
            ("schema_version", {"const": "ao.lore.private-pdf-uat-manifest.v0.1"}),
            ("parser_id", {"const": "docling"}),
            ("parser_version", {"const": "2.118.1"}),
            ("ocr_enabled", {"const": False}),
            ("provider_calls", {"const": False}),
            ("promotion_authority", {"const": False}),
            ("claims_authority_advance", {"const": False}),
        ):
            self.assertEqual(expected, manifest["properties"][field])
        self.assertEqual(
            ["heading", "paragraph", "list", "table", "code", "image", "caption", "footnote", "link"],
            manifest["$defs"]["block_type"]["enum"],
        )

        valid_manifest = {
            "schema_version": "ao.lore.private-pdf-uat-manifest.v0.1",
            "corpus_id": "ubuntu-pdf-seed-v1",
            "parser_id": "docling",
            "parser_version": "2.118.1",
            "ocr_enabled": False,
            "documents": [
                {
                    "item_id": "seed-01",
                    "file": "input/seed-01.pdf",
                    "source_digest": "sha256:" + "1" * 64,
                    "expected_text": ["Shared MIME-info Database"],
                    "required_block_types": ["heading", "paragraph"],
                }
            ],
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        self.assertTrue(_schema_accepts(manifest, valid_manifest))
        for field, value in (
            ("parser_id", "other"),
            ("parser_version", "2.118.2"),
            ("ocr_enabled", True),
            ("provider_calls", True),
        ):
            changed = deepcopy(valid_manifest)
            changed[field] = value
            self.assertFalse(_schema_accepts(manifest, changed), (field, value))
        for locator in (
            "/private/seed-01.pdf",
            r"input\seed-01.pdf",
            "input/../seed-01.pdf",
            "input/./seed-01.pdf",
            "input//seed-01.pdf",
            "input/seed-01.txt",
        ):
            changed = deepcopy(valid_manifest)
            changed["documents"][0]["file"] = locator
            self.assertFalse(_schema_accepts(manifest, changed), locator)
        duplicate_anchor = deepcopy(valid_manifest)
        duplicate_anchor["documents"][0]["expected_text"] = ["x", "x"]
        self.assertFalse(_schema_accepts(manifest, duplicate_anchor))
        duplicate_type = deepcopy(valid_manifest)
        duplicate_type["documents"][0]["required_block_types"] = ["heading", "heading"]
        self.assertFalse(_schema_accepts(manifest, duplicate_type))

        duplicate_anchor_manifest = deepcopy(valid_manifest)
        duplicate_anchor_manifest["documents"].append(
            {
                "item_id": "seed-02",
                "file": "input/seed-02.pdf",
                "source_digest": "sha256:" + "2" * 64,
                "expected_text": ["Shared MIME-info Database"],
                "required_block_types": ["heading"],
            }
        )
        duplicate_type_set_manifest = deepcopy(valid_manifest)
        duplicate_type_set_manifest["documents"].append(
            {
                "item_id": "seed-02",
                "file": "input/seed-02.pdf",
                "source_digest": "sha256:" + "2" * 64,
                "expected_text": ["Another anchor"],
                "required_block_types": ["paragraph", "heading"],
            }
        )
        self.assertTrue(_schema_accepts(manifest, duplicate_anchor_manifest))
        self.assertTrue(_schema_accepts(manifest, duplicate_type_set_manifest))
        unordered_manifest = deepcopy(valid_manifest)
        unordered_manifest["documents"] = [
            {
                "item_id": "seed-02",
                "file": "input/seed-02.pdf",
                "source_digest": "sha256:" + "2" * 64,
                "expected_text": ["Printer Task Information"],
                "required_block_types": ["heading", "table"],
            },
            deepcopy(valid_manifest["documents"][0]),
        ]
        self.assertTrue(_schema_accepts(manifest, unordered_manifest))

        self.assertEqual(
            {"#/$defs/not_supplied", "#/$defs/terminal"},
            {branch["$ref"] for branch in readback["oneOf"]},
        )
        readback_comment = readback["$comment"].lower()
        for invariant in (
            "path-free",
            "count",
            "arithmetic",
            "brain",
            "conversion",
            "results",
            "ordered",
            "caller-supplied",
            "manifest",
            "lexical",
        ):
            self.assertIn(invariant, readback_comment)
        self.assertEqual(
            {
                "schema_version",
                "lifecycle_status",
                "qualified",
                "ocr_used",
                "provider_calls",
                "promotion_authority",
                "claims_authority_advance",
            },
            set(readback["$defs"]["not_supplied"]["required"]),
        )
        self.assertEqual(
            "not_supplied",
            readback["$defs"]["not_supplied"]["properties"]["lifecycle_status"]["const"],
        )
        self.assertEqual(
            {
                "schema_version",
                "corpus_id",
                "corpus_digest",
                "configuration_digest",
                "qualification_digest",
                "aggregate_digest",
                "uat_digest",
                "campaign_origin_digest",
                "lifecycle_status",
                "tuning_decision",
                "qualified",
                "counts",
                "conversion_counts",
                "brain_before_digest",
                "brain_after_digest",
                "results",
                "aggregate",
                "ocr_enabled",
                "ocr_used",
                "network_accessed",
                "provider_calls",
                "promotion_authority",
                "claims_authority_advance",
            },
            set(readback["$defs"]["terminal"]["required"]),
        )
        self.assertEqual(
            ["completed", "partial"],
            readback["$defs"]["terminal"]["properties"]["lifecycle_status"]["enum"],
        )
        self.assertEqual(
            ["hold", "candidate_change", "investigate"],
            readback["$defs"]["terminal"]["properties"]["tuning_decision"]["enum"],
        )
        self.assertEqual(
            ["unreviewed", "accepted", "rejected"],
            readback["$defs"]["success_item"]["properties"]["review_status"]["enum"],
        )
        for field in (
            "qualified",
            "ocr_used",
            "provider_calls",
            "promotion_authority",
            "claims_authority_advance",
        ):
            self.assertEqual({"const": False}, readback["$defs"]["terminal"]["properties"][field])
        for field in ("ocr_enabled", "network_accessed"):
            self.assertEqual({"const": False}, readback["$defs"]["terminal"]["properties"][field])

        valid_readback = {
            "schema_version": "ao.lore.private-pdf-uat-readback.v0.1",
            "corpus_id": "ubuntu-pdf-seed-v1",
            "corpus_digest": "sha256:" + "1" * 64,
            "configuration_digest": "sha256:" + "2" * 64,
            "qualification_digest": "sha256:" + "3" * 64,
            "aggregate_digest": "sha256:" + "4" * 64,
            "uat_digest": "sha256:" + "5" * 64,
            "campaign_origin_digest": "sha256:" + "7" * 64,
            "lifecycle_status": "partial",
            "tuning_decision": "hold",
            "qualified": False,
            "counts": {
                "total": 2,
                "created": 1,
                "unchanged": 0,
                "rejected": 1,
                "processed": 2,
                "successful": 1,
                "candidate_queue": 1,
                "interrupted": 1,
                "resumed": 1,
                "rerun": 1,
            },
            "conversion_counts": {"initial": 1, "resumed": 1, "rerun": 0},
            "brain_before_digest": "sha256:" + "6" * 64,
            "brain_after_digest": "sha256:" + "6" * 64,
            "results": [
                {
                    "item_id": "seed-01",
                    "source_digest": "sha256:" + "1" * 64,
                    "status": "created",
                    "candidate_id": "candidate-seed-01",
                    "candidate_digest": "sha256:" + "2" * 64,
                    "provenance_digest": "sha256:" + "3" * 64,
                    "review_status": "accepted",
                },
                {
                    "item_id": "seed-02",
                    "source_digest": "sha256:" + "4" * 64,
                    "status": "rejected",
                    "error_code": "invalid_pdf",
                },
            ],
            "aggregate": {
                "metrics": {
                    "structural_fidelity": 0.5,
                    "text_fidelity": 1.0,
                    "source_location_fidelity": 0.5,
                },
                "exclusions": [],
                "stable_failures": {
                    "conversion_failed": 0,
                    "digest_mismatch": 0,
                    "encrypted_document": 0,
                    "invalid_pdf": 1,
                    "no_readable_content": 0,
                },
                "latency_seconds": {"minimum": 1.0, "maximum": 2.0, "mean": 1.5},
                "peak_memory_bytes": {"minimum": 1024.0, "maximum": 2048.0, "mean": 1536.0},
                "repeatability": {"runs": 2, "identical": True, "score": 1.0},
            },
            "ocr_enabled": False,
            "ocr_used": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        self.assertTrue(_schema_accepts(readback, valid_readback))
        fresh_created = deepcopy(valid_readback)
        fresh_created["results"][0]["review_status"] = "unreviewed"
        self.assertTrue(_schema_accepts(readback, fresh_created))
        unknown_review = deepcopy(valid_readback)
        unknown_review["results"][0]["review_status"] = "pending"
        self.assertFalse(_schema_accepts(readback, unknown_review))
        all_rejected = deepcopy(valid_readback)
        all_rejected["tuning_decision"] = "investigate"
        all_rejected["counts"] = {
            "total": 2,
            "created": 0,
            "unchanged": 0,
            "rejected": 2,
            "processed": 2,
            "successful": 0,
            "candidate_queue": 0,
            "interrupted": 1,
            "resumed": 1,
            "rerun": 1,
        }
        all_rejected["results"] = [
            {
                "item_id": "seed-01",
                "source_digest": "sha256:" + "1" * 64,
                "status": "rejected",
                "error_code": "invalid_pdf",
            },
            {
                "item_id": "seed-02",
                "source_digest": "sha256:" + "2" * 64,
                "status": "rejected",
                "error_code": "no_readable_content",
            },
        ]
        all_rejected["aggregate"]["metrics"] = {}
        all_rejected["aggregate"]["exclusions"] = [
            "structural_fidelity",
            "text_fidelity",
            "source_location_fidelity",
        ]
        all_rejected["aggregate"]["stable_failures"] = {
            "conversion_failed": 0,
            "digest_mismatch": 0,
            "encrypted_document": 0,
            "invalid_pdf": 1,
            "no_readable_content": 1,
        }
        self.assertTrue(_schema_accepts(readback, all_rejected))
        valid_not_supplied = {
            "schema_version": "ao.lore.private-pdf-uat-readback.v0.1",
            "lifecycle_status": "not_supplied",
            "qualified": False,
            "ocr_used": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        self.assertTrue(_schema_accepts(readback, valid_not_supplied))
        invalid_readback = deepcopy(valid_readback)
        invalid_readback["results"][1]["error_code"] = "private/path.pdf"
        self.assertFalse(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(valid_readback)
        invalid_readback["aggregate"]["exclusions"] = ["source_location_fidelity"] * 2
        self.assertFalse(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(all_rejected)
        invalid_readback["aggregate"]["exclusions"] = ["structural_fidelity", "text_fidelity"]
        self.assertTrue(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(valid_readback)
        invalid_readback["qualified"] = True
        self.assertFalse(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(all_rejected)
        invalid_readback["counts"]["successful"] = 1
        self.assertTrue(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(all_rejected)
        invalid_readback["counts"]["candidate_queue"] = 1
        self.assertTrue(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(all_rejected)
        invalid_readback["lifecycle_status"] = "completed"
        self.assertTrue(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(valid_readback)
        invalid_readback["tuning_decision"] = "candidate_change"
        self.assertTrue(_schema_accepts(readback, invalid_readback))
        representative_readback = deepcopy(valid_readback)
        representative_readback["corpus_id"] = "pdf-nomagic-uk-public-sector-v1"
        representative_readback["tuning_decision"] = "candidate_change"
        self.assertTrue(_schema_accepts(readback, representative_readback))
        invalid_readback = deepcopy(valid_not_supplied)
        invalid_readback["tuning_decision"] = "hold"
        self.assertFalse(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(valid_not_supplied)
        invalid_readback["corpus_id"] = "ubuntu-pdf-seed-v1"
        self.assertFalse(_schema_accepts(readback, invalid_readback))
        invalid_readback = deepcopy(valid_not_supplied)
        invalid_readback["network_attempts"] = 0
        self.assertFalse(_schema_accepts(readback, invalid_readback))

    def test_private_docx_uat_readback_schema_is_strict(self):
        schema = self.load("private-docx-uat-readback-v0.1.schema.json")
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema", schema["$schema"]
        )
        self.assertEqual(
            "https://schemas.ao-lore.example/private-docx-uat-readback-v0.1.schema.json",
            schema["$id"],
        )
        _assert_closed_objects(self, schema)

        digest = "sha256:" + "1" * 64
        valid = {
            "schema_version": "ao.lore.private-docx-uat-readback.v0.1",
            "corpus_id": "docx-nomagic-uk-public-sector-v1",
            "corpus_digest": digest,
            "configuration_digest": "sha256:" + "2" * 64,
            "qualification_digest": "sha256:" + "3" * 64,
            "aggregate_digest": "sha256:" + "4" * 64,
            "uat_digest": "sha256:" + "5" * 64,
            "campaign_origin_digest": "sha256:" + "6" * 64,
            "lifecycle_status": "partial",
            "tuning_decision": "hold",
            "qualified": False,
            "counts": {
                "total": 4,
                "created": 4,
                "unchanged": 0,
                "rejected": 0,
                "processed": 4,
                "successful": 4,
                "candidate_queue": 4,
                "interrupted": 1,
                "resumed": 1,
                "rerun": 1,
            },
            "conversion_counts": {"initial": 1, "resumed": 3, "rerun": 0},
            "results": [
                {
                    "item_id": "docx-domain-01",
                    "source_digest": digest,
                    "status": "created",
                    "candidate_id": "candidate-docx-domain-01",
                    "candidate_digest": "sha256:" + "7" * 64,
                    "provenance_digest": "sha256:" + "8" * 64,
                    "review_status": "unreviewed",
                },
                {
                    "item_id": "docx-domain-02",
                    "source_digest": "sha256:" + "9" * 64,
                    "status": "created",
                    "candidate_id": "candidate-docx-domain-02",
                    "candidate_digest": "sha256:" + "a" * 64,
                    "provenance_digest": "sha256:" + "b" * 64,
                    "review_status": "accepted",
                },
                {
                    "item_id": "docx-domain-03",
                    "source_digest": "sha256:" + "c" * 64,
                    "status": "created",
                    "candidate_id": "candidate-docx-domain-03",
                    "candidate_digest": "sha256:" + "d" * 64,
                    "provenance_digest": "sha256:" + "e" * 64,
                    "review_status": "rejected",
                },
                {
                    "item_id": "docx-domain-04",
                    "source_digest": "sha256:" + "f" * 64,
                    "status": "created",
                    "candidate_id": "candidate-docx-domain-04",
                    "candidate_digest": "sha256:" + "0" * 64,
                    "provenance_digest": "sha256:" + "1" * 64,
                    "review_status": "unreviewed",
                },
            ],
            "aggregate": {
                "metrics": {
                    "structural_fidelity": 1.0,
                    "text_fidelity": 1.0,
                    "source_location_fidelity": 1.0,
                    "expected_outcome_accuracy": 1.0,
                    "expected_rejection_count": 12,
                    "accepted_document_count": 88,
                },
                "exclusions": [],
                "stable_failures": {
                    "invalid_package": 1,
                    "unsupported_active_content": 11,
                    "representative_unexpected": 0,
                },
                "latency_seconds": {"min": 0.1, "mean": 0.15, "max": 0.2},
                "peak_memory_bytes": {"min": 1024, "mean": 1536.0, "max": 2048},
                "repeatability": {"runs": 2, "identical": True, "score": 1.0},
            },
            "ocr_enabled": False,
            "ocr_used": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        self.assertTrue(_schema_accepts(schema, valid))

        leaked = deepcopy(valid)
        leaked["results"][0]["source"] = "sources/private-docx-uat-abc/docx-domain-01.docx"
        self.assertFalse(_schema_accepts(schema, leaked))

        widened = deepcopy(valid)
        widened["promotion_authority"] = True
        self.assertFalse(_schema_accepts(schema, widened))

    def test_generated_candidate_contents_are_ignored_but_readme_is_tracked(self):
        rules = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("working/candidates/*", rules)
        self.assertIn("!working/candidates/README.md", rules)


class PrivatePdfDomainReviewSchemaTests(unittest.TestCase):
    schema_name = "private-pdf-domain-review-v0.1.schema.json"

    def setUp(self):
        with (SCHEMA_ROOT / self.schema_name).open(encoding="utf-8") as handle:
            self.schema = json.load(handle)
        self.review = {
            "schema_version": "ao.lore.private-pdf-domain-review.v0.1",
            "corpus_id": "pdf-nomagic-uk-public-sector-v1",
            "provenance": {
                "dataset_id": "napierone-pdf-nomagic",
                "document_digest": "sha256:adbf22aeb0a01e677cadc3805687f21b30ac4e4c70ed7694cdb72afb8101be65",
                "transformation_id": "pdf-nomagic-restore-five-byte-header-v1",
            },
            "representative_domain": True,
            "documents": [
                {
                    "item_id": f"domain-{index:02d}",
                    "source_file": f"00{number}-pdf-nomagic.pdf",
                    "source_digest": "sha256:" + str(index) * 64,
                    "derived_digest": "sha256:" + str(index + 4) * 64,
                    "page_count": pages,
                    "expected_text": [f"PRIVATE-ANCHOR-{index}"],
                    "required_block_types": ["heading", "paragraph"],
                }
                for index, (number, pages) in enumerate(
                    ((10, 1), (12, 2), (24, 4), (26, 10)), start=1
                )
            ],
            "parser_id": "docling",
            "parser_version": "2.118.1",
            "ocr_enabled": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }

    def test_schema_is_draft_2020_12_and_recursively_closed(self):
        Draft202012Validator.check_schema(self.schema)
        self.assertEqual(
            "https://json-schema.org/draft/2020-12/schema", self.schema["$schema"]
        )
        self.assertEqual(
            f"https://schemas.ao-lore.example/{self.schema_name}", self.schema["$id"]
        )
        _assert_closed_objects(self, self.schema)

    def test_exact_review_is_valid(self):
        Draft202012Validator(self.schema).validate(self.review)

    def test_identity_order_pages_transform_and_authority_are_fixed(self):
        validator = Draft202012Validator(self.schema)
        mutations = (
            ("seed identity", lambda value: value["documents"][0].update(item_id="seed-01")),
            ("wrong order", lambda value: value["documents"].reverse()),
            ("wrong pages", lambda value: value["documents"][2].update(page_count=3)),
            ("other transform", lambda value: value["provenance"].update(transformation_id="other")),
            ("not representative", lambda value: value.update(representative_domain=False)),
            ("OCR", lambda value: value.update(ocr_enabled=True)),
            ("provider", lambda value: value.update(provider_calls=True)),
            ("promotion", lambda value: value.update(promotion_authority=True)),
            ("authority", lambda value: value.update(claims_authority_advance=True)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = deepcopy(self.review)
                mutate(changed)
                self.assertFalse(validator.is_valid(changed))

    def test_missing_null_case_variant_unknown_and_unsafe_locators_reject(self):
        validator = Draft202012Validator(self.schema)
        mutations = []
        for field in self.schema["required"]:
            mutations.append((f"missing {field}", lambda value, field=field: value.pop(field)))
        mutations.extend(
            (
                ("null", lambda value: value.update(corpus_id=None)),
                ("case", lambda value: value.update(parser_id="Docling")),
                ("unknown", lambda value: value.update(unknown=True)),
            )
        )
        for locator in (
            "/private/0010.pdf",
            r"folder\0010.pdf",
            "../0010.pdf",
            "folder/./0010.pdf",
            "folder//0010.pdf",
        ):
            mutations.append(
                (locator, lambda value, locator=locator: value["documents"][0].update(source_file=locator))
            )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = deepcopy(self.review)
                mutate(changed)
                self.assertFalse(validator.is_valid(changed))


class PrivateDocxContractSchemaTests(unittest.TestCase):
    review_schema_name = "private-docx-domain-review-v0.1.schema.json"
    expectation_schema_name = "private-docx-expectation-v0.1.schema.json"
    corpus_id = "docx-nomagic-uk-public-sector-v1"
    transformation_id = "restore-ooxml-local-header-v1"
    review_items = (
        ("docx-domain-01", "0016-docx-nomagic.docx"),
        ("docx-domain-02", "0012-docx-nomagic.docx"),
        ("docx-domain-03", "0094-docx-nomagic.docx"),
        ("docx-domain-04", "0063-docx-nomagic.docx"),
    )
    rejection_categories = (
        "invalid-package",
        "active-content",
    )

    def setUp(self):
        with (SCHEMA_ROOT / self.review_schema_name).open(encoding="utf-8") as handle:
            self.review_schema = json.load(handle)
        with (SCHEMA_ROOT / self.expectation_schema_name).open(encoding="utf-8") as handle:
            self.expectation_schema = json.load(handle)
        self.review = {
            "schema_version": "ao.lore.private-docx-domain-review.v0.1",
            "corpus_id": self.corpus_id,
            "transformation_id": self.transformation_id,
            "documents": [
                {"item_id": item_id, "source_file": source_file}
                for item_id, source_file in self.review_items
            ],
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        self.expectation = {
            "schema_version": "ao.lore.private-docx-expectation.v0.1",
            "corpus_id": self.corpus_id,
            "items": [self._expectation_item(index) for index in range(1, 101)],
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }

    def _digest(self, index: int) -> str:
        return f"sha256:{index:064x}"

    def _expectation_item(self, index: int) -> dict[str, object]:
        outcome = "reject" if index % 10 == 0 else "accept"
        counts = {
            "paragraphs": index,
            "headings": index % 5,
            "lists": index % 4,
            "tables": index % 3,
            "rows": index + 1,
            "cells": index + 2,
            "links": index % 2,
            "footnotes": index % 3,
            "endnotes": index % 4,
            "drawings": index % 2,
            "media": index % 3,
        }
        return {
            "item_id": f"docx-{index:04d}",
            "source_digest": self._digest(index),
            "derived_digest": self._digest(index + 1000),
            "source_bytes": 1024 + index,
            "derived_bytes": 2048 + index,
            "transformation_id": self.transformation_id,
            "package_inventory_digest": self._digest(index + 2000),
            "normalized_text_digest": self._digest(index + 3000),
            "structural_event_digest": self._digest(index + 4000),
            "counts": counts,
            "expected_outcome": outcome,
            "expected_rejection": (
                None if outcome == "accept" else self.rejection_categories[(index // 10) % len(self.rejection_categories)]
            ),
            "page_count": None,
            "page_count_reason": "ooxml-pagination-unavailable",
        }

    def _is_exact_builtin(self, value):
        if value is None:
            return True
        if type(value) in (str, bool):
            return True
        if type(value) is int:
            return True
        if type(value) is list:
            return all(self._is_exact_builtin(item) for item in value)
        if type(value) is dict:
            return all(type(key) is str and self._is_exact_builtin(item) for key, item in value.items())
        return False

    def _review_runtime_valid(self, value):
        if not self._is_exact_builtin(value):
            return False
        return value["documents"] == [
            {"item_id": item_id, "source_file": source_file}
            for item_id, source_file in self.review_items
        ]

    def _expectation_runtime_valid(self, value):
        if not self._is_exact_builtin(value):
            return False
        expected_ids = [f"docx-{index:04d}" for index in range(1, 101)]
        actual_ids = [item["item_id"] for item in value["items"]]
        if actual_ids != expected_ids:
            return False
        return all(item["source_digest"] != item["derived_digest"] for item in value["items"])

    def test_private_docx_schemas_are_draft_2020_12_and_recursively_closed(self):
        for name, schema in (
            (self.review_schema_name, self.review_schema),
            (self.expectation_schema_name, self.expectation_schema),
        ):
            with self.subTest(schema=name):
                Draft202012Validator.check_schema(schema)
                self.assertEqual(
                    "https://json-schema.org/draft/2020-12/schema", schema["$schema"]
                )
                self.assertEqual(f"https://schemas.ao-lore.example/{name}", schema["$id"])
                _assert_closed_objects(self, schema)

    def test_exact_review_and_expectation_are_valid(self):
        Draft202012Validator(self.review_schema).validate(self.review)
        Draft202012Validator(self.expectation_schema).validate(self.expectation)
        self.assertTrue(self._review_runtime_valid(self.review))
        self.assertTrue(self._expectation_runtime_valid(self.expectation))

    def test_review_shape_and_authority_flags_are_fixed(self):
        validator = Draft202012Validator(self.review_schema)
        documents = self.review_schema["properties"]["documents"]
        self.assertEqual((4, 4), (documents["minItems"], documents["maxItems"]))
        mutations = (
            ("wrong order", lambda value: value["documents"].reverse()),
            (
                "alternate transformation",
                lambda value: value.update(transformation_id="other-transform"),
            ),
            ("absolute path", lambda value: value["documents"][0].update(source_file="/abs.docx")),
            ("backslash path", lambda value: value["documents"][0].update(source_file=r"folder\name.docx")),
            ("dot segment", lambda value: value["documents"][0].update(source_file="../escape.docx")),
            ("ocr", lambda value: value.update(ocr_enabled=True)),
            ("network", lambda value: value.update(network_accessed=True)),
            ("provider", lambda value: value.update(provider_calls=True)),
            ("promotion", lambda value: value.update(promotion_authority=True)),
            ("authority", lambda value: value.update(claims_authority_advance=True)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = deepcopy(self.review)
                mutate(changed)
                if label == "wrong order":
                    self.assertTrue(validator.is_valid(changed))
                    self.assertFalse(self._review_runtime_valid(changed))
                else:
                    self.assertFalse(validator.is_valid(changed))

    def test_expectation_shape_order_and_closed_values_are_fixed(self):
        validator = Draft202012Validator(self.expectation_schema)
        items = self.expectation_schema["properties"]["items"]
        self.assertEqual((100, 100), (items["minItems"], items["maxItems"]))
        self.assertEqual(
            ["active-content", "invalid-package"],
            sorted(
                self.expectation_schema["$defs"]["reject_item"]["properties"]
                ["expected_rejection"]["enum"]
            ),
        )
        mutations = (
            ("wrong sequence", lambda value: value["items"].reverse()),
            (
                "alternate transformation",
                lambda value: value["items"][0].update(transformation_id="other-transform"),
            ),
            (
                "equal digests",
                lambda value: value["items"][0].update(derived_digest=value["items"][0]["source_digest"]),
            ),
            (
                "invalid counts",
                lambda value: value["items"][0].update(counts={**value["items"][0]["counts"], "paragraphs": -1}),
            ),
            (
                "unknown rejection",
                lambda value: value["items"][9].update(expected_rejection="unknown-category"),
            ),
            (
                "non-null page count",
                lambda value: value["items"][0].update(page_count=1),
            ),
            ("ocr", lambda value: value.update(ocr_enabled=True)),
            ("network", lambda value: value.update(network_accessed=True)),
            ("provider", lambda value: value.update(provider_calls=True)),
            ("promotion", lambda value: value.update(promotion_authority=True)),
            ("authority", lambda value: value.update(claims_authority_advance=True)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label):
                changed = deepcopy(self.expectation)
                mutate(changed)
                if label in {"wrong sequence", "equal digests"}:
                    self.assertTrue(validator.is_valid(changed))
                    self.assertFalse(self._expectation_runtime_valid(changed))
                else:
                    self.assertFalse(validator.is_valid(changed))

    def test_runtime_contract_rejects_non_builtin_json_types(self):
        class DictSubclass(dict):
            pass

        class StringSubclass(str):
            pass

        review = deepcopy(self.review)
        review["documents"] = [
            DictSubclass(review["documents"][0]),
            *review["documents"][1:],
        ]
        expectation = deepcopy(self.expectation)
        expectation["items"][0]["item_id"] = StringSubclass("docx-0001")
        self.assertFalse(self._review_runtime_valid(review))
        self.assertFalse(self._expectation_runtime_valid(expectation))

    def test_duplicate_json_keys_reject_before_semantic_validation(self):
        cases = (
            (
                "review",
                json.dumps(self.review, sort_keys=True).replace(
                    '"corpus_id": "docx-nomagic-uk-public-sector-v1"',
                    '"corpus_id": "docx-nomagic-uk-public-sector-v1", "corpus_id": "other"',
                    1,
                ),
            ),
            (
                "expectation",
                json.dumps(self.expectation, sort_keys=True).replace(
                    '"page_count_reason": "ooxml-pagination-unavailable"',
                    '"page_count_reason": "ooxml-pagination-unavailable", "page_count_reason": "other"',
                    1,
                ),
            ),
        )
        for label, body in cases:
            with self.subTest(label=label):
                with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
                    parse_strict_json(body.encode("utf-8"), label)


if __name__ == "__main__":
    unittest.main()
