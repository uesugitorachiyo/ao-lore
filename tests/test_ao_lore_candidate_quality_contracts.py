import json
import hashlib
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore._strict_io import ContractError, parse_strict_json
from ao_lore.benchmark import canonical_digest
from ao_lore.candidate_quality_contracts import (
    ANNOTATION_LABELS,
    AUTHORITY_FIELDS,
    QUESTION_CATEGORIES,
    QUESTION_OUTCOMES,
    RECOVERY_PHASES,
    RECOVERY_STATUSES,
    RECOMMENDATIONS,
    validate_candidate_quality_annotations,
    validate_candidate_quality_campaign,
    validate_candidate_quality_question_results,
    validate_candidate_quality_questions,
    validate_candidate_quality_recovery,
    validate_candidate_quality_result,
    validate_candidate_quality_sample,
    validate_candidate_quality_sampling_policy,
    validate_candidate_quality_summary,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"
SCHEMA_NAMES = {
    "candidate-quality-campaign-v0.1.schema.json",
    "candidate-quality-sampling-policy-v0.1.schema.json",
    "candidate-quality-sample-v0.1.schema.json",
    "candidate-quality-annotations-v0.1.schema.json",
    "candidate-quality-questions-v0.1.schema.json",
    "candidate-quality-question-results-v0.1.schema.json",
    "candidate-quality-result-v0.1.schema.json",
    "candidate-quality-summary-v0.1.schema.json",
    "candidate-quality-recovery-v0.1.schema.json",
}
CLAIM_COUNTS = (19, 13, 73, 21, 71, 289)
SAMPLE_ALLOCATIONS = (19, 13, 14, 12, 14, 24)
TOTAL_CLAIMS = 486
TOTAL_SAMPLED_CLAIMS = 96
TOTAL_QUESTIONS = 24


def _load_schema(name: str) -> dict:
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def _assert_recursively_closed(testcase: unittest.TestCase, value, location: str = "$") -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            testcase.assertIs(
                value.get("additionalProperties"),
                False,
                f"open object at {location}",
            )
        for key, child in value.items():
            _assert_recursively_closed(testcase, child, f"{location}/{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_recursively_closed(testcase, child, f"{location}/{index}")


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _with_self_digest(value: dict, field: str) -> dict:
    bound = deepcopy(value)
    bound[field] = canonical_digest({key: item for key, item in bound.items() if key != field})
    return bound


class CandidateQualityContractTests(unittest.TestCase):
    def candidate_ids(self) -> list[str]:
        return [f"candidate-{index:02d}" for index in range(1, 7)]

    def authority(self) -> dict[str, bool]:
        return {field: False for field in AUTHORITY_FIELDS}

    def candidate_bindings(self) -> list[dict[str, object]]:
        result = []
        for index, claim_count in enumerate(CLAIM_COUNTS, 1):
            result.append(
                {
                    "candidate_id": f"candidate-{index:02d}",
                    "candidate_digest": _digest(chr(ord("a") + index - 1)),
                    "provenance_digest": _digest(chr(ord("g") + index - 1)),
                    "source_digest": _digest(chr(ord("m") + index - 1)),
                    "claim_count": claim_count,
                }
            )
        return result

    def sampling_policy(self) -> dict[str, object]:
        value = {
            "schema_version": "ao.lore.candidate-quality-sampling-policy.v0.1",
            "policy_id": "candidate-quality-policy-20260812",
            "campaign_id": "candidate-quality-campaign-20260812",
            "terminal_readback_digest": _digest("t"),
            "total_candidate_count": 6,
            "total_claim_count": TOTAL_CLAIMS,
            "total_sample_count": TOTAL_SAMPLED_CLAIMS,
            "allocations": [
                {
                    "candidate_id": f"candidate-{index:02d}",
                    "candidate_digest": _digest(chr(ord("a") + index - 1)),
                    "provenance_digest": _digest(chr(ord("g") + index - 1)),
                    "source_digest": _digest(chr(ord("m") + index - 1)),
                    "claim_count": claim_count,
                    "sample_count": sample_count,
                }
                for index, (claim_count, sample_count) in enumerate(
                    zip(CLAIM_COUNTS, SAMPLE_ALLOCATIONS), 1
                )
            ],
            "selection_priority": list(
                (
                    "binding_risk",
                    "normalized_duplicate",
                    "fragmentation",
                    "position",
                    "length",
                    "block_type",
                    "digest_fill",
                )
            ),
            **self.authority(),
        }
        return _with_self_digest(value, "policy_digest")

    def sample(self) -> dict[str, object]:
        selections = []
        labels = (
            "binding_risk",
            "normalized_duplicate",
            "fragmentation",
            "first_position",
            "middle_position",
            "final_position",
            "longest_claim",
            "shortest_claim",
            "block_type_representative",
            "digest_fill",
        )
        ordinal = 1
        for candidate_index, count in enumerate(SAMPLE_ALLOCATIONS, 1):
            for claim_index in range(1, count + 1):
                selections.append(
                    {
                        "sample_ordinal": ordinal,
                        "candidate_id": f"candidate-{candidate_index:02d}",
                        "claim_id": f"claim-{candidate_index:02d}-{claim_index:03d}",
                        "citation_id": f"citation-{candidate_index:02d}-{claim_index:03d}",
                        "claim_record_digest": _digest(hex((ordinal % 16))[2:]),
                        "candidate_digest": _digest(chr(ord("a") + candidate_index - 1)),
                        "provenance_digest": _digest(chr(ord("g") + candidate_index - 1)),
                        "source_digest": _digest(chr(ord("m") + candidate_index - 1)),
                        "reason_codes": [labels[(ordinal - 1) % len(labels)]],
                    }
                )
                ordinal += 1
        value = {
            "schema_version": "ao.lore.candidate-quality-sample.v0.1",
            "sample_id": "candidate-quality-sample-20260812",
            "campaign_id": "candidate-quality-campaign-20260812",
            "policy_digest": self.sampling_policy()["policy_digest"],
            "total_selected_claims": TOTAL_SAMPLED_CLAIMS,
            "selections": selections,
            **self.authority(),
        }
        return _with_self_digest(value, "sample_digest")

    def annotations(self) -> dict[str, object]:
        sample = self.sample()
        annotations = []
        annotation_counts = {label: 0 for label in ANNOTATION_LABELS}
        for selection in sample["selections"]:
            ordinal = selection["sample_ordinal"]
            classification = ANNOTATION_LABELS[(ordinal - 1) % len(ANNOTATION_LABELS)]
            annotation_counts[classification] += 1
            annotations.append(
                {
                    "sample_ordinal": ordinal,
                    "candidate_id": selection["candidate_id"],
                    "claim_id": selection["claim_id"],
                    "citation_id": selection["citation_id"],
                    "source_digest": selection["source_digest"],
                    "policy_digest": sample["policy_digest"],
                    "classification": classification,
                    "rationale": f"public rationale {ordinal:03d}",
                }
            )
        value = {
            "schema_version": "ao.lore.candidate-quality-annotations.v0.1",
            "annotations_id": "candidate-quality-annotations-20260812",
            "campaign_id": "candidate-quality-campaign-20260812",
            "sample_digest": sample["sample_digest"],
            "reviewer_id": "reviewer-01",
            "reviewed_at": "2026-08-12T12:34:56Z",
            "annotation_counts": annotation_counts,
            "annotations": annotations,
            **self.authority(),
        }
        return _with_self_digest(value, "annotations_digest")

    def questions(self) -> dict[str, object]:
        questions = []
        categories = list(QUESTION_CATEGORIES)
        for candidate_index in range(1, 7):
            for offset, category in enumerate(categories, 1):
                question_index = (candidate_index - 1) * 4 + offset
                expected_outcome = "refusal" if category == "unsupported" else "supported"
                questions.append(
                    {
                        "question_id": f"question-{question_index:02d}",
                        "candidate_id": f"candidate-{candidate_index:02d}",
                        "source_digest": _digest(chr(ord("m") + candidate_index - 1)),
                        "category": category,
                        "prompt": f"public question {question_index:02d}",
                        "expected_outcome": expected_outcome,
                        "expected_claim_ids": []
                        if expected_outcome == "refusal"
                        else [f"claim-{candidate_index:02d}-{offset:03d}"],
                    }
                )
                if expected_outcome == "supported":
                    questions[-1]["evidence_query"] = f"public evidence {question_index:02d}"
        value = {
            "schema_version": "ao.lore.candidate-quality-questions.v0.1",
            "question_set_id": "candidate-quality-questions-20260812",
            "campaign_id": "candidate-quality-campaign-20260812",
            "question_count": TOTAL_QUESTIONS,
            "questions": questions,
            **self.authority(),
        }
        return _with_self_digest(value, "questions_digest")

    def question_results(self) -> dict[str, object]:
        questions = self.questions()
        results = []
        for question in questions["questions"]:
            supported = question["expected_outcome"] == "supported"
            results.append(
                {
                    "question_id": question["question_id"],
                    "candidate_id": question["candidate_id"],
                    "expected_outcome": question["expected_outcome"],
                    "actual_outcome": question["expected_outcome"],
                    "supporting_claim_ids": list(question["expected_claim_ids"]),
                    "supporting_claim_count": len(question["expected_claim_ids"]),
                    "evidence_match_millionths": 0 if not supported else 1_000_000,
                }
            )
        value = {
            "schema_version": "ao.lore.candidate-quality-question-results.v0.1",
            "question_results_id": "candidate-quality-question-results-20260812",
            "campaign_id": "candidate-quality-campaign-20260812",
            "questions_digest": questions["questions_digest"],
            "result_count": TOTAL_QUESTIONS,
            "results": results,
            **self.authority(),
        }
        return _with_self_digest(value, "question_results_digest")

    def result(self) -> dict[str, object]:
        value = {
            "schema_version": "ao.lore.candidate-quality-result.v0.1",
            "result_id": "candidate-quality-result-01-20260812",
            "campaign_id": "candidate-quality-campaign-20260812",
            "candidate_id": "candidate-01",
            "candidate_digest": _digest("a"),
            "provenance_digest": _digest("g"),
            "source_digest": _digest("m"),
            "sample_digest": self.sample()["sample_digest"],
            "annotations_digest": self.annotations()["annotations_digest"],
            "questions_digest": self.questions()["questions_digest"],
            "question_results_digest": self.question_results()["question_results_digest"],
            "total_candidate_claim_count": 19,
            "sampled_claim_count": 19,
            "question_count": 4,
            "classification_counts": {
                "useful_exact": 19,
                "exact_but_fragmented": 0,
                "exact_but_low_value": 0,
                "duplicate": 0,
                "misleading_without_context": 0,
                "binding_error": 0,
            },
            "question_outcome_counts": {
                "supported": 3,
                "refusal": 1,
                "mismatch": 0,
            },
            "automatic_check_counts": {
                "verified_claim_count": 19,
                "verified_citation_count": 19,
                "duplicate_suspect_count": 0,
                "fragmentation_suspect_count": 0,
                "binding_error_count": 0,
            },
            "recommendation": "pass",
            **self.authority(),
        }
        return _with_self_digest(value, "result_digest")

    def campaign(self) -> dict[str, object]:
        results = [_digest(seed) for seed in "uvwxyz"]
        value = {
            "schema_version": "ao.lore.candidate-quality-campaign.v0.1",
            "campaign_id": "candidate-quality-campaign-20260812",
            "correlation_id": "ao-lore-public-candidate-quality-review-20260812",
            "terminal_readback_digest": _digest("t"),
            "total_candidate_count": 6,
            "total_claim_count": TOTAL_CLAIMS,
            "total_sampled_claim_count": TOTAL_SAMPLED_CLAIMS,
            "total_question_count": TOTAL_QUESTIONS,
            "candidate_bindings": self.candidate_bindings(),
            "sampling_policy_digest": self.sampling_policy()["policy_digest"],
            "sample_digest": self.sample()["sample_digest"],
            "annotations_digest": self.annotations()["annotations_digest"],
            "questions_digest": self.questions()["questions_digest"],
            "question_results_digest": self.question_results()["question_results_digest"],
            "result_digests": results,
            **self.authority(),
        }
        return _with_self_digest(value, "campaign_digest")

    def summary(self) -> dict[str, object]:
        value = {
            "schema_version": "ao.lore.candidate-quality-summary.v0.1",
            "summary_id": "candidate-quality-summary-20260812",
            "campaign_id": "candidate-quality-campaign-20260812",
            "correlation_id": "ao-lore-public-candidate-quality-review-20260812",
            "terminal_readback_digest": _digest("t"),
            "campaign_digest": self.campaign()["campaign_digest"],
            "total_candidate_count": 6,
            "total_sampled_claim_count": TOTAL_SAMPLED_CLAIMS,
            "total_question_count": TOTAL_QUESTIONS,
            "candidate_recommendations": [
                {
                    "candidate_id": candidate_id,
                    "recommendation": "pass" if index < 2 else "hold",
                    "result_digest": _digest(chr(ord("u") + index)),
                }
                for index, candidate_id in enumerate(self.candidate_ids())
            ],
            "corpus_recommendation": "hold",
            **self.authority(),
        }
        return _with_self_digest(value, "summary_digest")

    def recovery(self) -> dict[str, object]:
        value = {
            "schema_version": "ao.lore.candidate-quality-recovery.v0.1",
            "recovery_id": "candidate-quality-recovery-20260812",
            "campaign_id": "candidate-quality-campaign-20260812",
            "campaign_digest": self.campaign()["campaign_digest"],
            "phase": "annotation",
            "status": "ready_to_resume",
            "retained_artifact_digests": [
                self.sampling_policy()["policy_digest"],
                self.sample()["sample_digest"],
            ],
            "owned_staging_relpaths": [
                "campaign-20260812/staging/annotations.partial.json",
                "campaign-20260812/staging/sample.lock",
            ],
            **self.authority(),
        }
        return _with_self_digest(value, "recovery_digest")

    def cases(self):
        return (
            (
                "candidate-quality-campaign-v0.1.schema.json",
                self.campaign(),
                validate_candidate_quality_campaign,
                "campaign_digest",
            ),
            (
                "candidate-quality-sampling-policy-v0.1.schema.json",
                self.sampling_policy(),
                validate_candidate_quality_sampling_policy,
                "policy_digest",
            ),
            (
                "candidate-quality-sample-v0.1.schema.json",
                self.sample(),
                validate_candidate_quality_sample,
                "sample_digest",
            ),
            (
                "candidate-quality-annotations-v0.1.schema.json",
                self.annotations(),
                validate_candidate_quality_annotations,
                "annotations_digest",
            ),
            (
                "candidate-quality-questions-v0.1.schema.json",
                self.questions(),
                validate_candidate_quality_questions,
                "questions_digest",
            ),
            (
                "candidate-quality-question-results-v0.1.schema.json",
                self.question_results(),
                validate_candidate_quality_question_results,
                "question_results_digest",
            ),
            (
                "candidate-quality-result-v0.1.schema.json",
                self.result(),
                validate_candidate_quality_result,
                "result_digest",
            ),
            (
                "candidate-quality-summary-v0.1.schema.json",
                self.summary(),
                validate_candidate_quality_summary,
                "summary_digest",
            ),
            (
                "candidate-quality-recovery-v0.1.schema.json",
                self.recovery(),
                validate_candidate_quality_recovery,
                "recovery_digest",
            ),
        )

    def validate_schema(self, schema_name: str, sample: dict) -> None:
        Draft202012Validator(_load_schema(schema_name)).validate(sample)

    def assert_rejected_by_both(self, schema_name: str, sample: dict, validator) -> None:
        self.assertFalse(
            Draft202012Validator(_load_schema(schema_name)).is_valid(sample),
            f"{schema_name} unexpectedly accepted {sample!r}",
        )
        with self.assertRaises(ContractError):
            validator(sample)

    def test_schema_family_is_draft_2020_12_valid_and_recursively_closed(self):
        self.assertEqual(
            SCHEMA_NAMES,
            {name for name in SCHEMA_NAMES if (SCHEMA_ROOT / name).exists()},
        )
        for name in SCHEMA_NAMES:
            with self.subTest(name=name):
                schema = _load_schema(name)
                Draft202012Validator.check_schema(schema)
                self.assertEqual(
                    "https://json-schema.org/draft/2020-12/schema",
                    schema["$schema"],
                )
                self.assertEqual(f"https://schemas.ao-lore.example/{name}", schema["$id"])
                _assert_recursively_closed(self, schema)

    def test_closed_enums_match_the_public_candidate_quality_design(self):
        self.assertEqual(
            QUESTION_CATEGORIES,
            (
                "fact",
                "procedure",
                "qualification",
                "unsupported",
            ),
        )
        self.assertEqual(QUESTION_OUTCOMES, ("supported", "refusal"))
        self.assertEqual(
            ANNOTATION_LABELS,
            (
                "useful_exact",
                "exact_but_fragmented",
                "exact_but_low_value",
                "duplicate",
                "misleading_without_context",
                "binding_error",
            ),
        )
        self.assertEqual(RECOMMENDATIONS, ("pass", "hold", "reject"))
        self.assertEqual(
            RECOVERY_PHASES,
            ("intake", "verification", "sampling", "annotation", "questions", "summary"),
        )
        self.assertEqual(
            RECOVERY_STATUSES,
            ("staged", "interrupted", "ready_to_resume", "completed"),
        )

    def test_sampling_policy_schema_binds_candidate_identity_digests(self):
        schema = _load_schema("candidate-quality-sampling-policy-v0.1.schema.json")
        item_properties = schema["properties"]["allocations"]["items"]["properties"]
        self.assertEqual(
            {"candidate_id", "candidate_digest", "provenance_digest", "source_digest", "claim_count", "sample_count"},
            set(item_properties),
        )
        self.assertEqual({"$ref": "#/$defs/digest"}, item_properties["candidate_digest"])
        self.assertEqual({"$ref": "#/$defs/digest"}, item_properties["provenance_digest"])
        self.assertEqual({"$ref": "#/$defs/digest"}, item_properties["source_digest"])

    def test_schema_and_runtime_validators_accept_identical_detached_samples(self):
        for name, sample, validator, _digest_field in self.cases():
            with self.subTest(name=name):
                schema = _load_schema(name)
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(sample)
                validated = validator(sample)
                self.assertEqual(validated, sample)
                self.assertIsNot(validated, sample)
                sample.clear()
                self.assertTrue(validated)

    def test_duplicate_json_keys_reject_before_validation(self):
        raw = b'{"schema_version":"x","schema_version":"y"}'
        with self.assertRaisesRegex(ContractError, "duplicate JSON key"):
            parse_strict_json(raw, "candidate quality contract")

    def test_unknown_case_variant_and_missing_false_authority_values_reject(self):
        for name, sample, validator, _digest_field in self.cases():
            with self.subTest(name=name, mutation="unknown"):
                changed = deepcopy(sample)
                changed["unknown"] = 1
                self.assert_rejected_by_both(name, changed, validator)
            with self.subTest(name=name, mutation="case-variant"):
                changed = deepcopy(sample)
                value = changed.pop("campaign_id", changed.pop("sample_id", None))
                if value is None:
                    key = next(iter(k for k in sample if k.endswith("_id") and k != "schema_version"))
                    value = changed.pop(key)
                    changed[key.upper()] = value
                else:
                    changed["Campaign_id"] = value
                self.assert_rejected_by_both(name, changed, validator)
            with self.subTest(name=name, mutation="missing-authority"):
                changed = deepcopy(sample)
                changed.pop(AUTHORITY_FIELDS[0])
                self.assert_rejected_by_both(name, changed, validator)
            with self.subTest(name=name, mutation="true-authority"):
                changed = deepcopy(sample)
                changed[AUTHORITY_FIELDS[0]] = True
                self.assert_rejected_by_both(name, changed, validator)

    def test_wrong_ids_digests_counts_and_runtime_self_digest_mismatch_reject(self):
        for name, sample, validator, digest_field in self.cases():
            with self.subTest(name=name, mutation="identifier"):
                changed = deepcopy(sample)
                key = next(iter(field for field in changed if field.endswith("_id") and field != "schema_version"))
                changed[key] = "Invalid"
                self.assert_rejected_by_both(name, changed, validator)
            with self.subTest(name=name, mutation="digest-pattern"):
                changed = deepcopy(sample)
                first_digest_key = next(iter(field for field in changed if field.endswith("_digest")))
                changed[first_digest_key] = "sha256:not-a-digest"
                self.assert_rejected_by_both(name, changed, validator)
            with self.subTest(name=name, mutation="self-digest"):
                changed = deepcopy(sample)
                changed[digest_field] = _digest("0")
                with self.assertRaises(ContractError):
                    validator(changed)
            with self.subTest(name=name, mutation="count"):
                changed = deepcopy(sample)
                if "total_claim_count" in changed:
                    changed["total_claim_count"] -= 1
                elif "total_selected_claims" in changed:
                    changed["total_selected_claims"] -= 1
                elif "question_count" in changed:
                    changed["question_count"] -= 1
                elif "result_count" in changed:
                    changed["result_count"] -= 1
                elif "total_sampled_claim_count" in changed:
                    changed["total_sampled_claim_count"] -= 1
                else:
                    changed["question_count"] = 5
                self.assert_rejected_by_both(name, changed, validator)

    def test_absolute_paths_nonfinite_values_and_over_budget_payloads_reject(self):
        recovery = self.recovery()
        recovery["owned_staging_relpaths"][0] = "/private/tmp/recovery.json"
        self.assert_rejected_by_both(
            "candidate-quality-recovery-v0.1.schema.json",
            recovery,
            validate_candidate_quality_recovery,
        )

        result = self.result()
        result["automatic_check_counts"]["verified_claim_count"] = float("nan")
        self.assert_rejected_by_both(
            "candidate-quality-result-v0.1.schema.json",
            result,
            validate_candidate_quality_result,
        )

        annotations = self.annotations()
        annotations["annotations"][0]["rationale"] = "x" * 513
        self.assert_rejected_by_both(
            "candidate-quality-annotations-v0.1.schema.json",
            annotations,
            validate_candidate_quality_annotations,
        )

        sample = self.sample()
        sample["selections"].append(
            {
                "sample_ordinal": 97,
                "candidate_id": "candidate-06",
                "claim_id": "claim-06-097",
                "citation_id": "citation-06-097",
                "claim_record_digest": _digest("9"),
                "candidate_digest": _digest("f"),
                "provenance_digest": _digest("l"),
                "source_digest": _digest("r"),
                "reason_codes": ["digest_fill"],
            }
        )
        self.assert_rejected_by_both(
            "candidate-quality-sample-v0.1.schema.json",
            sample,
            validate_candidate_quality_sample,
        )

    def test_annotations_reject_conflicting_nested_policy_digest_with_recomputed_self_digest(self):
        annotations = self.annotations()
        annotations["annotations"][0]["policy_digest"] = _digest("conflicting-policy")
        annotations = _with_self_digest(annotations, "annotations_digest")
        self.assertTrue(
            Draft202012Validator(
                _load_schema("candidate-quality-annotations-v0.1.schema.json")
            ).is_valid(annotations)
        )
        with self.assertRaises(ContractError):
            validate_candidate_quality_annotations(annotations)

    def test_annotations_require_counts_even_with_recomputed_self_digest(self):
        annotations = self.annotations()
        annotations.pop("annotation_counts")
        annotations = _with_self_digest(annotations, "annotations_digest")
        self.assertFalse(
            Draft202012Validator(
                _load_schema("candidate-quality-annotations-v0.1.schema.json")
            ).is_valid(annotations)
        )
        with self.assertRaises(ContractError):
            validate_candidate_quality_annotations(annotations)

    def test_questions_reject_conflicting_candidate_source_digest_with_recomputed_self_digest(self):
        questions = self.questions()
        questions["questions"][1]["source_digest"] = _digest("conflicting-source")
        questions = _with_self_digest(questions, "questions_digest")
        self.assertTrue(
            Draft202012Validator(
                _load_schema("candidate-quality-questions-v0.1.schema.json")
            ).is_valid(questions)
        )
        with self.assertRaises(ContractError):
            validate_candidate_quality_questions(questions)

    def test_questions_require_supported_evidence_query_and_keep_unsupported_questions_anchorless(self):
        questions = self.questions()
        questions["questions"][0]["prompt"] = "working controls in the procedure"
        questions["questions"][0]["evidence_query"] = "synthetic public claim 01 005"
        questions = _with_self_digest(questions, "questions_digest")
        self.assertTrue(
            Draft202012Validator(
                _load_schema("candidate-quality-questions-v0.1.schema.json")
            ).is_valid(questions)
        )
        validated = validate_candidate_quality_questions(questions)
        self.assertEqual("working controls in the procedure", validated["questions"][0]["prompt"])
        self.assertEqual("synthetic public claim 01 005", validated["questions"][0]["evidence_query"])
        self.assertNotIn("evidence_query", validated["questions"][3])

    def test_questions_require_canonical_evidence_query(self):
        questions = self.questions()
        questions["questions"][0]["evidence_query"] = "Synthetic public claim 01-005."
        questions = _with_self_digest(questions, "questions_digest")
        self.assertFalse(
            Draft202012Validator(
                _load_schema("candidate-quality-questions-v0.1.schema.json")
            ).is_valid(questions)
        )
        with self.assertRaises(ContractError):
            validate_candidate_quality_questions(questions)

        normalized = self.questions()
        normalized["questions"][0]["evidence_query"] = "synthetic public claim 01 005"
        normalized = _with_self_digest(normalized, "questions_digest")
        self.assertTrue(
            Draft202012Validator(
                _load_schema("candidate-quality-questions-v0.1.schema.json")
            ).is_valid(normalized)
        )
        self.assertEqual(
            "synthetic public claim 01 005",
            validate_candidate_quality_questions(normalized)["questions"][0]["evidence_query"],
        )

    def test_sample_rejects_conflicting_candidate_digests_with_recomputed_self_digest(self):
        sample = self.sample()
        sample["selections"][1]["candidate_digest"] = _digest("conflicting-candidate-digest")
        sample = _with_self_digest(sample, "sample_digest")
        self.assertTrue(
            Draft202012Validator(
                _load_schema("candidate-quality-sample-v0.1.schema.json")
            ).is_valid(sample)
        )
        with self.assertRaises(ContractError):
            validate_candidate_quality_sample(sample)

    def test_schema_and_runtime_share_the_same_bounded_timestamp_rule(self):
        annotations = self.annotations()
        annotations["reviewed_at"] = "2026-99-99T99:99:99Z"
        self.assert_rejected_by_both(
            "candidate-quality-annotations-v0.1.schema.json",
            annotations,
            validate_candidate_quality_annotations,
        )

    def test_schema_and_runtime_share_the_same_relative_owned_path_rule(self):
        recovery = self.recovery()
        recovery["owned_staging_relpaths"][0] = "campaign-20260812/../escape.json"
        self.assert_rejected_by_both(
            "candidate-quality-recovery-v0.1.schema.json",
            recovery,
            validate_candidate_quality_recovery,
        )

    def test_question_results_runtime_rejects_unknown_candidate_id_with_recomputed_self_digest(self):
        results = self.question_results()
        results["results"][0]["candidate_id"] = "candidate-99"
        results = _with_self_digest(results, "question_results_digest")
        self.assertTrue(
            Draft202012Validator(
                _load_schema("candidate-quality-question-results-v0.1.schema.json")
            ).is_valid(results)
        )
        with self.assertRaises(ContractError):
            validate_candidate_quality_question_results(results)

    def test_question_results_runtime_rejects_candidate_misdistribution_with_recomputed_self_digest(self):
        results = self.question_results()
        for row in results["results"]:
            row["candidate_id"] = "candidate-01"
        results = _with_self_digest(results, "question_results_digest")
        self.assertTrue(
            Draft202012Validator(
                _load_schema("candidate-quality-question-results-v0.1.schema.json")
            ).is_valid(results)
        )
        with self.assertRaises(ContractError):
            validate_candidate_quality_question_results(results)

    def test_result_rejects_candidate_specific_sample_allocation_drift_with_recomputed_self_digest(self):
        result = self.result()
        result["candidate_id"] = "candidate-03"
        result["total_candidate_claim_count"] = 73
        result["sampled_claim_count"] = 1
        result["automatic_check_counts"]["verified_claim_count"] = 73
        result["automatic_check_counts"]["verified_citation_count"] = 73
        result["classification_counts"] = {
            "useful_exact": 1,
            "exact_but_fragmented": 0,
            "exact_but_low_value": 0,
            "duplicate": 0,
            "misleading_without_context": 0,
            "binding_error": 0,
        }
        result = _with_self_digest(result, "result_digest")
        self.assertFalse(
            Draft202012Validator(
                _load_schema("candidate-quality-result-v0.1.schema.json")
            ).is_valid(result),
            "schema should express candidate-specific sampled claim allocations",
        )
        with self.assertRaises(ContractError):
            validate_candidate_quality_result(result)


if __name__ == "__main__":
    unittest.main()
