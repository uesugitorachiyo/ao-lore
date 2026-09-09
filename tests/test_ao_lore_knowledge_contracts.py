import importlib
import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator
from ao_lore.benchmark import canonical_digest
from tests.schema_registry import shipped_validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"
DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64

KNOWLEDGE_SCHEMAS = {
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
    "okf-candidate-v0.3.schema.json",
}


def load_schema(name):
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def assert_recursively_closed(testcase, value, location="$"):
    if isinstance(value, dict):
        if value.get("type") == "object":
            testcase.assertIs(
                value.get("additionalProperties"),
                False,
                f"open object at {location}",
            )
        for key, child in value.items():
            assert_recursively_closed(testcase, child, f"{location}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_recursively_closed(testcase, child, f"{location}/{index}")


def claim():
    return {
        "claim_id": "claim-1",
        "text": "Reviewed evidence supports this bounded claim.",
        "source_block_ids": ["block-1"],
        "citation_id": "citation-1",
    }


def citation():
    return {
        "citation_id": "citation-1",
        "render_text": "Reviewed fixture, block 1",
        "source_block_ids": ["block-1"],
    }


def knowledge_policy():
    return {"sensitivity": "public", "stale_after": None}


def candidate():
    return {
        "schema_version": "ao.lore.okf-candidate.v0.2",
        "candidate_id": "candidate-1",
        "document_ir_digest": DIGEST,
        "concepts": [
            {
                "candidate_concept_id": "concept-1",
                "title": "Fixture policy",
                "source_block_ids": ["block-1"],
                "status": "proposed",
                "candidate_claim_ids": ["claim-1"],
            }
        ],
        "claim_mappings": [{"claim_id": "claim-1", "block_ids": ["block-1"]}],
        "links": [],
        "contradiction_warnings": [],
        "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
        "claims_digest": DIGEST,
        "claims": [claim()],
        "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
        "citations_digest": OTHER_DIGEST,
        "citations": [citation()],
        "knowledge_policy": knowledge_policy(),
        "canonical": False,
        "promotion_authority": False,
    }


def canonical_entry():
    return {
        "schema_version": "ao.lore.okf-canonical-entry.v0.3",
        "policy_version": "ao.lore.promotion-policy.v0.1",
        "canonical_entry_id": "entry-1",
        "semantic_key": DIGEST,
        "candidate_id": "candidate-1",
        "candidate_digest": OTHER_DIGEST,
        "document_ir_digest": DIGEST,
        "source_digest": OTHER_DIGEST,
        "provenance_digest": DIGEST,
        "parser": {"parser_id": "fixture-parser", "parser_version": "1.0.0"},
        "accepted_review_head_digest": OTHER_DIGEST,
        "concepts": candidate()["concepts"],
        "claim_mappings": candidate()["claim_mappings"],
        "links": [],
        "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
        "claims_digest": DIGEST,
        "claims": [claim()],
        "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
        "citations_digest": OTHER_DIGEST,
        "citations": [citation()],
        "knowledge_policy": knowledge_policy(),
    }


def evidence_projection():
    return {
        "schema_version": "ao.lore.knowledge-evidence-projection.v0.1",
        "evidence_id": "evidence-0123456789abcdef0123456789abcdef",
        "requirement_ids": ["requirement-query"],
        "supported_claims": [{"claim_id": "claim-1", "text": claim()["text"]}],
        "source_ref": "canonical:entry-1#citation-1",
        "source_digest": OTHER_DIGEST,
        "citation": "Reviewed fixture, block 1",
        "provenance": {
            "generation_id": "generation-1",
            "generation_manifest_digest": DIGEST,
            "canonical_entry_id": "entry-1",
            "canonical_entry_digest": OTHER_DIGEST,
            "candidate_id": "candidate-1",
            "provenance_digest": DIGEST,
            "parser_id": "fixture-parser",
            "parser_version": "1.0.0",
            "entry_schema_version": "ao.lore.okf-canonical-entry.v0.3",
        },
        "freshness_met": True,
        "trust_met": True,
        "verified": True,
    }


def document_origin(block_id="block-1", evidence_id="document-evidence-1"):
    return {
        "workspace_id": "workspace-1", "document_store_id": "store-1",
        "generation_digest": DIGEST, "document_id": "document-1",
        "document_ir_digest": DIGEST, "source_id": "source-1",
        "source_digest": OTHER_DIGEST, "evidence_kind": "document_block",
        "evidence_id": evidence_id, "evidence_digest": DIGEST,
        "block_id": block_id, "render_text": "Reviewed fixture evidence.",
        "source_span": {"page": 1, "start": 0, "end": 10},
        "authority_role": "technical_guidance", "sensitivity": "public",
        "freshness_status": "current", "qualification_codes": [],
    }


def canonical_entry_v0_4():
    module = importlib.import_module("ao_lore.knowledge_contracts")
    value = canonical_entry()
    for field in ("document_ir_digest", "source_digest", "parser"):
        value.pop(field)
    value.update(
        schema_version="ao.lore.okf-canonical-entry.v0.4",
        evidence_origins=[document_origin()],
    )
    value["evidence_selection_digest"] = canonical_digest({
        "domain": "ao.lore.evidence-selection.v0.1", "evidence": value["evidence_origins"],
    })
    value["claims_digest"] = module.claims_digest(value["claims"])
    value["citations_digest"] = module.citations_digest(value["citations"])
    value["semantic_key"] = module.semantic_key_v0_3(
        value["concepts"], value["claim_mappings"], value["links"],
        value["claims_digest"], value["citations_digest"], value["knowledge_policy"],
    )
    return value


class KnowledgeSchemaTests(unittest.TestCase):
    def validator(self, schema_name):
        return shipped_validator(schema_name)

    def validate(self, schema_name, instance):
        errors = sorted(
            self.validator(schema_name).iter_errors(instance),
            key=lambda error: list(error.absolute_path),
        )
        self.assertEqual([], errors, "\n".join(error.message for error in errors))

    def assert_rejected(self, schema_name, instance):
        self.assertFalse(
            self.validator(schema_name).is_valid(instance),
            f"{schema_name} unexpectedly accepted {instance!r}",
        )

    def test_schema_family_is_draft_2020_12_valid_and_recursively_closed(self):
        for name in KNOWLEDGE_SCHEMAS:
            with self.subTest(name=name):
                schema = load_schema(name)
                Draft202012Validator.check_schema(schema)
                self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
                self.assertEqual(
                    f"https://schemas.ao-lore.example/{name}", schema["$id"],
                )
                assert_recursively_closed(self, schema)

    def test_claim_and_citation_sets_accept_exact_bounded_payloads(self):
        self.validate(
            "canonical-claim-set-v0.1.schema.json",
            {"schema_version": "ao.lore.canonical-claim-set.v0.1", "claims": [claim()]},
        )
        self.validate(
            "canonical-citation-set-v0.1.schema.json",
            {"schema_version": "ao.lore.canonical-citation-set.v0.1", "citations": [citation()]},
        )
        oversized = claim()
        oversized["text"] = "x" * 1025
        self.assert_rejected(
            "canonical-claim-set-v0.1.schema.json",
            {"schema_version": "ao.lore.canonical-claim-set.v0.1", "claims": [oversized]},
        )
        private_locator = citation()
        private_locator["source_path"] = "/private/source.pdf"
        self.assert_rejected(
            "canonical-citation-set-v0.1.schema.json",
            {"schema_version": "ao.lore.canonical-citation-set.v0.1", "citations": [private_locator]},
        )

    def test_candidate_result_and_canonical_entry_preserve_payload_shape(self):
        value = candidate()
        self.validate("okf-candidate-v0.2.schema.json", value)
        self.validate(
            "distillation-result-v0.2.schema.json",
            {
                "schema_version": "ao.lore.distillation-result.v0.2",
                "candidate": value,
                "candidate_digest": DIGEST,
                "distillation_trace": {
                    "adapter": "fixture-distiller",
                    "policy_version": "v0.2",
                    "input_block_count": 1,
                    "candidate_concept_count": 1,
                    "candidate_claim_count": 1,
                    "candidate_citation_count": 1,
                    "private_reasoning_persisted": False,
                },
            },
        )
        entry = canonical_entry()
        self.validate("okf-canonical-entry-v0.3.schema.json", entry)
        for field in ("claims_schema_version", "claims_digest", "claims", "citations_schema_version", "citations_digest", "citations", "knowledge_policy"):
            self.assertEqual(value[field], entry[field], field)

    def test_candidate_and_canonical_entry_reject_authority_and_private_fields(self):
        for schema_name, value in (
            ("okf-candidate-v0.2.schema.json", candidate()),
            ("okf-canonical-entry-v0.3.schema.json", canonical_entry()),
        ):
            for forbidden in ("reviewer", "rationale", "authorization_id", "source_path", "provider_transcript"):
                with self.subTest(schema=schema_name, forbidden=forbidden):
                    changed = deepcopy(value)
                    changed[forbidden] = "forbidden"
                    self.assert_rejected(schema_name, changed)

    def test_v0_4_canonical_entry_is_closed_multi_origin_without_fake_single_origin_fields(self):
        entry = canonical_entry_v0_4()
        self.validate("okf-canonical-entry-v0.4.schema.json", entry)
        schema = load_schema("okf-canonical-entry-v0.4.schema.json")
        for forbidden in ("document_ir_digest", "source_digest", "parser"):
            self.assertNotIn(forbidden, schema["properties"])
            changed = deepcopy(entry); changed[forbidden] = DIGEST
            self.assert_rejected("okf-canonical-entry-v0.4.schema.json", changed)

    def test_v0_4_origin_arrays_reject_schema_level_duplicates(self):
        entry = canonical_entry_v0_4()
        entry["evidence_origins"].append(deepcopy(entry["evidence_origins"][0]))
        self.assert_rejected("okf-canonical-entry-v0.4.schema.json", entry)

        snapshot = {
            "snapshot_schema_version": "ao.lore.knowledge-snapshot.v0.2",
            "latest_generation_id": "generation-1", "latest_generation_manifest_digest": DIGEST,
            "snapshot_digest": OTHER_DIGEST,
            "effective_entries": [{
                "canonical_entry_id": "entry-1", "canonical_entry_digest": DIGEST,
                "entry_schema_version": "ao.lore.okf-canonical-entry.v0.4",
                "answerability": "answerable", "claim_count": 1,
                "evidence_origin_identities": [
                    {"workspace_id": "workspace-1", "evidence_kind": "document_block", "evidence_id": "origin-1", "evidence_digest": OTHER_DIGEST},
                    {"evidence_digest": OTHER_DIGEST, "evidence_id": "origin-1", "evidence_kind": "document_block", "workspace_id": "workspace-1"},
                ],
            }],
            "legacy_v0_2_entry_count": 0, "answerable_v0_3_entry_count": 0,
            "answerable_v0_4_entry_count": 1,
        }
        self.assert_rejected("knowledge-snapshot-v0.2.schema.json", snapshot)
        module = importlib.import_module("ao_lore.knowledge_contracts")
        with self.assertRaises(module.KnowledgeContractError):
            module.validate_snapshot_readback(snapshot)

    def test_v0_4_rejects_multi_anchor_claims_and_citations_only(self):
        module = importlib.import_module("ao_lore.knowledge_contracts")
        for field in ("claims", "citations"):
            changed = canonical_entry_v0_4()
            changed["evidence_origins"].append(document_origin("block-2", "document-evidence-2"))
            changed["evidence_selection_digest"] = canonical_digest({
                "domain": "ao.lore.evidence-selection.v0.1", "evidence": changed["evidence_origins"],
            })
            changed[field][0]["source_block_ids"].append("block-2")
            if field == "claims":
                changed["claim_mappings"][0]["block_ids"].append("block-2")
                changed["claims_digest"] = module.claims_digest(changed["claims"])
            else:
                changed["citations_digest"] = module.citations_digest(changed["citations"])
            changed["semantic_key"] = module.semantic_key_v0_3(
                changed["concepts"], changed["claim_mappings"], changed["links"],
                changed["claims_digest"], changed["citations_digest"], changed["knowledge_policy"],
            )
            with self.subTest(field=field):
                self.assert_rejected("okf-canonical-entry-v0.4.schema.json", changed)
                with self.assertRaises(module.KnowledgeContractError):
                    module.validate_knowledge_payload(changed["schema_version"], changed)

        legacy_schema = load_schema("okf-canonical-entry-v0.3.schema.json")
        self.assertEqual(1, legacy_schema["$defs"]["claim"]["properties"]["source_block_ids"]["minItems"])
        self.assertEqual(5000, legacy_schema["$defs"]["claim"]["properties"]["source_block_ids"]["maxItems"])

    def test_snapshot_schema_pairs_version_with_exact_answerability(self):
        base = {
            "snapshot_schema_version": "ao.lore.knowledge-snapshot.v0.2",
            "latest_generation_id": "generation-1", "latest_generation_manifest_digest": DIGEST,
            "snapshot_digest": OTHER_DIGEST, "legacy_v0_2_entry_count": 0,
            "answerable_v0_3_entry_count": 0, "answerable_v0_4_entry_count": 1,
        }
        invalid_entries = (
            {"canonical_entry_id": "entry-1", "canonical_entry_digest": DIGEST, "entry_schema_version": "ao.lore.okf-canonical-entry.v0.2", "answerability": "answerable", "claim_count": 0},
            {"canonical_entry_id": "entry-1", "canonical_entry_digest": DIGEST, "entry_schema_version": "ao.lore.okf-canonical-entry.v0.3", "answerability": "metadata_only", "claim_count": 0},
        )
        for entry in invalid_entries:
            changed = deepcopy(base); changed["effective_entries"] = [entry]
            with self.subTest(version=entry["entry_schema_version"]):
                self.assert_rejected("knowledge-snapshot-v0.2.schema.json", changed)

    def test_v0_2_readbacks_require_positive_v0_4_presence(self):
        status = {
            "schema_version": "ao.lore.knowledge-status-readback.v0.2", "status": "completed",
            "snapshot_digest": DIGEST, "latest_generation_id": "generation-1",
            "latest_generation_manifest_digest": OTHER_DIGEST, "generation_count": 1,
            "effective_entry_count": 1, "answerable_v0_3_entry_count": 1,
            "answerable_v0_4_entry_count": 0, "legacy_v0_2_entry_count": 0,
            "claim_count": 1, "answerability_status": "fully_answerable",
        }
        search = {
            "schema_version": "ao.lore.knowledge-search-readback.v0.2", "status": "completed",
            "snapshot_digest": DIGEST, "query_digest": OTHER_DIGEST, "hits": [],
            "legacy_v0_2_entry_count": 0, "answerable_v0_3_entry_count": 1,
            "answerable_v0_4_entry_count": 0,
        }
        snapshot = {
            "snapshot_schema_version": "ao.lore.knowledge-snapshot.v0.2",
            "latest_generation_id": "generation-1", "latest_generation_manifest_digest": OTHER_DIGEST,
            "snapshot_digest": DIGEST,
            "effective_entries": [{
                "canonical_entry_id": "entry-1", "canonical_entry_digest": DIGEST,
                "entry_schema_version": "ao.lore.okf-canonical-entry.v0.3",
                "answerability": "answerable", "claim_count": 1,
            }],
            "legacy_v0_2_entry_count": 0, "answerable_v0_3_entry_count": 1,
            "answerable_v0_4_entry_count": 0,
        }
        module = importlib.import_module("ao_lore.knowledge_contracts")
        for schema_name, value, validator in (
            ("knowledge-status-readback-v0.2.schema.json", status, module.validate_status_readback),
            ("knowledge-search-readback-v0.2.schema.json", search, module.validate_search_readback),
            ("knowledge-snapshot-v0.2.schema.json", snapshot, module.validate_snapshot_readback),
        ):
            with self.subTest(schema=schema_name):
                self.assert_rejected(schema_name, value)
                with self.assertRaises(module.KnowledgeContractError):
                    validator(value)

    def test_other_v0_2_readbacks_already_require_v0_4_specific_bindings(self):
        projection = evidence_projection()
        projection["schema_version"] = "ao.lore.knowledge-evidence-projection.v0.2"
        self.assert_rejected("knowledge-evidence-projection-v0.2.schema.json", projection)
        answer = {
            "schema_version": "ao.lore.knowledge-answer-readback.v0.2", "status": "refuse",
            "snapshot_digest": DIGEST, "coverage_report_digest": OTHER_DIGEST,
            "claim_ids": [], "evidence_ids": [], "citations": [],
            "legacy_v0_2_entry_count": 0, "answerable_v0_4_entry_count": 0,
            "answer": "", "reason_code": "coverage_insufficient",
        }
        self.assert_rejected("knowledge-answer-readback-v0.2.schema.json", answer)

    def test_answer_runtime_rejects_reordered_semantic_duplicate_citations(self):
        module = importlib.import_module("ao_lore.knowledge_contracts")
        identity = {"workspace_id": "workspace-1", "evidence_kind": "document_block", "evidence_id": "origin-1", "evidence_digest": DIGEST}
        first = {"source_ref": "canonical:entry-1#citation-1", "origin_identity": identity}
        second = {"origin_identity": {key: identity[key] for key in reversed(identity)}, "source_ref": "canonical:entry-1#citation-1"}
        value = {
            "schema_version": "ao.lore.knowledge-answer-readback.v0.2", "status": "answer",
            "snapshot_digest": DIGEST, "coverage_report_digest": OTHER_DIGEST,
            "claim_ids": ["claim-1"], "evidence_ids": ["evidence-0123456789abcdef0123456789abcdef"],
            "citations": [first, second], "legacy_v0_2_entry_count": 0,
            "answerable_v0_4_entry_count": 1, "answer": "Bounded answer.", "reason_code": "none",
        }
        with self.assertRaises(module.KnowledgeContractError):
            module.validate_answer_readback(value)

    def test_runtime_v0_4_requires_unambiguous_promoted_origin_anchors(self):
        module = importlib.import_module("ao_lore.knowledge_contracts")
        checked = module.validate_knowledge_payload(
            "ao.lore.okf-canonical-entry.v0.4", canonical_entry_v0_4()
        )
        self.assertEqual([document_origin()], checked["evidence_origins"])
        drifted = canonical_entry_v0_4(); drifted["evidence_selection_digest"] = OTHER_DIGEST
        with self.assertRaises(module.KnowledgeContractError):
            module.validate_knowledge_payload(drifted["schema_version"], drifted)
        for mutation in ("duplicate", "missing", "ambiguous"):
            changed = canonical_entry_v0_4()
            if mutation == "duplicate":
                changed["evidence_origins"].append(deepcopy(changed["evidence_origins"][0]))
            elif mutation == "missing":
                changed["claims"][0]["source_block_ids"] = ["block-missing"]
                changed["claim_mappings"][0]["block_ids"] = ["block-missing"]
                changed["citations"][0]["source_block_ids"] = ["block-missing"]
                changed["claims_digest"] = module.claims_digest(changed["claims"])
                changed["citations_digest"] = module.citations_digest(changed["citations"])
            else:
                second = document_origin(evidence_id="document-evidence-2")
                changed["evidence_origins"].append(second)
            with self.subTest(mutation=mutation), self.assertRaises(module.KnowledgeContractError):
                module.validate_knowledge_payload(changed["schema_version"], changed)

    def test_snapshot_accepts_empty_and_mixed_version_projections(self):
        empty = {
            "snapshot_schema_version": "ao.lore.knowledge-snapshot.v0.1",
            "latest_generation_id": None,
            "latest_generation_manifest_digest": None,
            "snapshot_digest": DIGEST,
            "effective_entries": [],
            "legacy_v0_2_entry_count": 0,
            "answerable_v0_3_entry_count": 0,
        }
        self.validate("knowledge-snapshot-v0.1.schema.json", empty)
        mixed = deepcopy(empty)
        mixed.update(
            {
                "latest_generation_id": "generation-2",
                "latest_generation_manifest_digest": OTHER_DIGEST,
                "effective_entries": [
                    {"canonical_entry_id": "legacy-entry", "canonical_entry_digest": DIGEST, "entry_schema_version": "ao.lore.okf-canonical-entry.v0.2", "answerability": "metadata_only", "claim_count": 0},
                    {"canonical_entry_id": "entry-1", "canonical_entry_digest": OTHER_DIGEST, "entry_schema_version": "ao.lore.okf-canonical-entry.v0.3", "answerability": "answerable", "claim_count": 1},
                ],
                "legacy_v0_2_entry_count": 1,
                "answerable_v0_3_entry_count": 1,
            }
        )
        self.validate("knowledge-snapshot-v0.1.schema.json", mixed)

    def test_status_search_evidence_and_answer_examples_match_locked_outcomes(self):
        self.validate(
            "knowledge-status-readback-v0.1.schema.json",
            {
                "schema_version": "ao.lore.knowledge-status-readback.v0.1",
                "status": "completed",
                "snapshot_digest": DIGEST,
                "latest_generation_id": "generation-1",
                "latest_generation_manifest_digest": OTHER_DIGEST,
                "generation_count": 1,
                "effective_entry_count": 1,
                "answerable_v0_3_entry_count": 1,
                "legacy_v0_2_entry_count": 0,
                "claim_count": 1,
                "answerability_status": "fully_answerable",
            },
        )
        self.validate(
            "knowledge-search-readback-v0.1.schema.json",
            {
                "schema_version": "ao.lore.knowledge-search-readback.v0.1",
                "status": "completed",
                "snapshot_digest": DIGEST,
                "query_digest": OTHER_DIGEST,
                "hits": [
                    {
                        "classification": "answerable",
                        "evidence_id": evidence_projection()["evidence_id"],
                        "canonical_entry_id": "entry-1",
                        "claim_id": "claim-1",
                        "citation_id": "citation-1",
                        "claim_text": claim()["text"],
                        "citation": citation()["render_text"],
                        "source_ref": "canonical:entry-1#citation-1",
                        "score_components": {
                            "claim_exact_match": False,
                            "claim_substring_match": True,
                            "matched_claim_token_count": 2,
                            "matched_citation_token_count": 0,
                        },
                    }
                ],
                "legacy_v0_2_entry_count": 0,
                "answerable_v0_3_entry_count": 1,
            },
        )
        self.validate("knowledge-evidence-projection-v0.1.schema.json", evidence_projection())
        for status, reason in (
            ("answer", "none"),
            ("partial", "coverage_insufficient"),
            ("refuse", "no_answerable_entries"),
            ("investigate", "canonical_state_invalid"),
        ):
            with self.subTest(status=status):
                self.validate(
                    "knowledge-answer-readback-v0.1.schema.json",
                    {
                        "schema_version": "ao.lore.knowledge-answer-readback.v0.1",
                        "status": status,
                        "snapshot_digest": DIGEST,
                        "coverage_report_digest": OTHER_DIGEST,
                        "claim_ids": ["claim-1"] if status in {"answer", "partial"} else [],
                        "evidence_ids": [evidence_projection()["evidence_id"]] if status in {"answer", "partial"} else [],
                        "citations": ["canonical:entry-1#citation-1"] if status in {"answer", "partial"} else [],
                        "legacy_v0_2_entry_count": 0,
                        "answer": claim()["text"] if status in {"answer", "partial"} else "",
                        "reason_code": reason,
                    },
                )

    def test_readbacks_never_accept_raw_query_or_mutation_authority(self):
        status = {
            "schema_version": "ao.lore.knowledge-status-readback.v0.1",
            "status": "completed",
            "snapshot_digest": DIGEST,
            "latest_generation_id": None,
            "latest_generation_manifest_digest": None,
            "generation_count": 0,
            "effective_entry_count": 0,
            "answerable_v0_3_entry_count": 0,
            "legacy_v0_2_entry_count": 0,
            "claim_count": 0,
            "answerability_status": "empty",
        }
        for forbidden in ("query", "normalized_query", "candidate_root", "promotion_authority", "network_authority"):
            changed = deepcopy(status)
            changed[forbidden] = "forbidden"
            self.assert_rejected("knowledge-status-readback-v0.1.schema.json", changed)


class KnowledgeRuntimeContractRedTests(unittest.TestCase):
    def test_runtime_contract_validator_module_exists(self):
        module = importlib.import_module("ao_lore.knowledge_contracts")
        self.assertTrue(callable(module.validate_knowledge_payload))

    def test_runtime_reader_module_exposes_snapshot_validation(self):
        module = importlib.import_module("ao_lore.knowledge")
        self.assertTrue(callable(module.read_knowledge_snapshot))

    def test_v0_2_snapshot_runtime_contract_is_closed(self):
        module = importlib.import_module("ao_lore.knowledge_contracts")
        value = {
            "snapshot_schema_version": "ao.lore.knowledge-snapshot.v0.2",
            "latest_generation_id": "generation-1", "latest_generation_manifest_digest": DIGEST,
            "snapshot_digest": OTHER_DIGEST,
            "effective_entries": [{
                "canonical_entry_id": "entry-1", "canonical_entry_digest": DIGEST,
                "entry_schema_version": "ao.lore.okf-canonical-entry.v0.4",
                "answerability": "answerable", "claim_count": 1,
                "evidence_origin_identities": [{
                    "workspace_id": "workspace-1", "evidence_kind": "document_block",
                    "evidence_id": "origin-1", "evidence_digest": OTHER_DIGEST,
                }],
            }],
            "legacy_v0_2_entry_count": 0, "answerable_v0_3_entry_count": 0,
            "answerable_v0_4_entry_count": 1,
        }
        self.assertEqual(value, module.validate_snapshot_readback(value))
        value["unexpected"] = False
        with self.assertRaises(module.KnowledgeContractError):
            module.validate_snapshot_readback(value)

    def test_multiple_claims_may_share_one_exact_citation(self):
        module = importlib.import_module("ao_lore.knowledge_contracts")
        value = candidate()
        second_claim = {
            "claim_id": "claim-2",
            "text": "A second reviewed claim uses the same exact citation.",
            "source_block_ids": ["block-1"],
            "citation_id": "citation-1",
        }
        value["claims"].append(second_claim)
        value["claim_mappings"].append(
            {"claim_id": "claim-2", "block_ids": ["block-1"]}
        )
        value["claims_digest"] = module.claims_digest(value["claims"])
        value["citations_digest"] = module.citations_digest(value["citations"])

        checked = module.validate_knowledge_payload(value["schema_version"], value)

        self.assertEqual("citation-1", checked["claims"][1]["citation_id"])


if __name__ == "__main__":
    unittest.main()
