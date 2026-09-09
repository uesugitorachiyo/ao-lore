import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from ao_lore import candidate_quality
from ao_lore._strict_io import ContractError
from ao_lore.candidate_quality_contracts import (
    QUESTION_CATEGORIES,
    validate_candidate_quality_question_results,
    validate_candidate_quality_questions,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SCRIPT = ROOT / "tests" / "fixtures" / "ao_lore" / "candidate_quality" / "generate.py"
SUPPORTED_QUESTION_ORDINALS = {"fact": 5, "procedure": 6, "qualification": 7}


def _load_fixture() -> dict:
    completed = subprocess.run(
        [sys.executable, str(FIXTURE_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _candidate_question_ids(candidate_index: int) -> list[str]:
    start = (candidate_index - 1) * 4 + 1
    return [f"question-{ordinal:02d}" for ordinal in range(start, start + 4)]


def _claim_record(candidate: dict, ordinal: int) -> dict:
    return deepcopy(candidate["claim_records"][ordinal - 1])


def _rebind_claim_record(record: dict) -> None:
    record["claim_record_digest"] = candidate_quality._canonical(
        {
            "candidate_id": record["candidate_id"],
            "claim_ordinal": record["claim_ordinal"],
            "claim_id": record["claim_id"],
            "citation_id": record["citation_id"],
            "source_block_ids": record["source_block_ids"],
            "claim_text": record["claim_text"],
            "citation_text": record["citation_text"],
        },
        "claim record",
    )


def _rebind_candidate_result(candidate: dict) -> None:
    candidate["verified_result_digest"] = candidate_quality._canonical(
        {key: value for key, value in candidate.items() if key != "verified_result_digest"},
        "verified candidate result",
    )


def _precommitted_definitions(verified: dict) -> list[dict]:
    definitions = []
    for candidate in verified["candidate_results"]:
        for category in QUESTION_CATEGORIES:
            if category == "unsupported":
                definitions.append(
                    {
                        "candidate_id": candidate["candidate_id"],
                        "source_digest": candidate["source_digest"],
                        "category": category,
                        "prompt": f"{candidate['candidate_id']} unsupported no evidence expected",
                        "expected_outcome": "refusal",
                        "expected_claim_ids": [],
                    }
                )
                continue
            record = candidate["claim_records"][SUPPORTED_QUESTION_ORDINALS[category] - 1]
            definitions.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "source_digest": candidate["source_digest"],
                    "category": category,
                    "prompt": record["claim_text"],
                    "evidence_query": candidate_quality._normalize_question_text(record["claim_text"]),
                    "expected_outcome": "supported",
                    "expected_claim_ids": [record["claim_id"]],
                }
            )
    return definitions


class CandidateQualityQuestionTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = _load_fixture()
        self.verified = fixture["verified_claims"]

    def test_private_question_builder_requires_precommitted_questions_and_preserves_expected_evidence(self):
        self.assertEqual(
            QUESTION_CATEGORIES,
            ("fact", "procedure", "qualification", "unsupported"),
        )
        self.assertFalse(hasattr(candidate_quality, "build_candidate_quality_questions"))
        self.assertTrue(hasattr(candidate_quality, "_build_candidate_quality_questions"))
        self.assertNotIn("_build_candidate_quality_questions", candidate_quality.__all__)

        definitions = _precommitted_definitions(self.verified)
        questions = candidate_quality._build_candidate_quality_questions(self.verified, definitions)

        self.assertEqual(questions, validate_candidate_quality_questions(questions))
        self.assertEqual(24, questions["question_count"])
        self.assertEqual(24, len({item["question_id"] for item in questions["questions"]}))

        for candidate_index in range(1, 7):
            with self.subTest(candidate=f"candidate-{candidate_index:02d}"):
                candidate_id = f"candidate-{candidate_index:02d}"
                expected_ids = _candidate_question_ids(candidate_index)
                expected_claim_ids = [
                    [f"claim-{candidate_index:02d}-{SUPPORTED_QUESTION_ORDINALS['fact']:03d}"],
                    [f"claim-{candidate_index:02d}-{SUPPORTED_QUESTION_ORDINALS['procedure']:03d}"],
                    [f"claim-{candidate_index:02d}-{SUPPORTED_QUESTION_ORDINALS['qualification']:03d}"],
                    [],
                ]
                candidate_questions = [
                    item for item in questions["questions"] if item["candidate_id"] == candidate_id
                ]
                self.assertEqual(expected_ids, [item["question_id"] for item in candidate_questions])
                self.assertEqual(list(QUESTION_CATEGORIES), [item["category"] for item in candidate_questions])
                self.assertEqual(
                    ["supported", "supported", "supported", "refusal"],
                    [item["expected_outcome"] for item in candidate_questions],
                )
                self.assertEqual(
                    expected_claim_ids,
                    [item["expected_claim_ids"] for item in candidate_questions],
                )
                self.assertEqual(
                    [definition["prompt"] for definition in definitions[(candidate_index - 1) * 4 : candidate_index * 4]],
                    [item["prompt"] for item in candidate_questions],
                )
                self.assertEqual(
                    [
                        definitions[(candidate_index - 1) * 4 + 0]["evidence_query"],
                        definitions[(candidate_index - 1) * 4 + 1]["evidence_query"],
                        definitions[(candidate_index - 1) * 4 + 2]["evidence_query"],
                        None,
                    ],
                    [item.get("evidence_query") for item in candidate_questions],
                )

    def test_private_question_evaluator_matches_queries_independently_without_importing_knowledge(self):
        self.assertFalse(hasattr(candidate_quality, "evaluate_candidate_quality_questions"))
        self.assertTrue(hasattr(candidate_quality, "_evaluate_candidate_quality_questions"))
        self.assertNotIn("_evaluate_candidate_quality_questions", candidate_quality.__all__)
        self.assertTrue(hasattr(candidate_quality, "_evaluate_candidate_quality_question"))
        self.assertNotIn("_evaluate_candidate_quality_question", candidate_quality.__all__)

        verified = deepcopy(self.verified)
        first_candidate = verified["candidate_results"][0]
        for ordinal in (1, 2, 3):
            record = first_candidate["claim_records"][ordinal - 1]
            record["claim_text"] = f"Unrelated first-pass text {ordinal:03d}."
            record["citation_text"] = record["claim_text"]
            _rebind_claim_record(record)
        second_candidate = verified["candidate_results"][1]
        procedure_record = second_candidate["claim_records"][SUPPORTED_QUESTION_ORDINALS["procedure"] - 1]
        procedure_record["claim_text"] = "Cafe\u0301 STEP 02-006 Kelvin \u212a."
        procedure_record["citation_text"] = procedure_record["claim_text"]
        _rebind_claim_record(procedure_record)
        _rebind_candidate_result(first_candidate)
        _rebind_candidate_result(second_candidate)

        definitions = _precommitted_definitions(verified)
        definitions[5]["prompt"] = "CAF\u00c9 step 02 006 kelvin k"
        definitions[5]["evidence_query"] = candidate_quality._normalize_question_text(procedure_record["claim_text"])
        questions = candidate_quality._build_candidate_quality_questions(verified, definitions)

        original_import = __import__

        def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
            blocked_roots = (
                "ao_lore.knowledge",
                "ao_lore.synthesis",
                "ao_lore.navigation",
            )
            if name in blocked_roots or any(name.startswith(root + ".") for root in blocked_roots):
                raise AssertionError(f"unexpected import: {name}")
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=guarded_import):
            first_detail = candidate_quality._evaluate_candidate_quality_question(
                verified["candidate_results"][0],
                questions["questions"][0],
            )
            second_procedure_detail = candidate_quality._evaluate_candidate_quality_question(
                verified["candidate_results"][1],
                questions["questions"][5],
            )
            results = candidate_quality._evaluate_candidate_quality_questions(verified, questions)

        self.assertEqual(results, validate_candidate_quality_question_results(results))
        self.assertEqual("supported", first_detail["actual_outcome"])
        self.assertEqual(["claim-01-005"], first_detail["supporting_claim_ids"])
        self.assertEqual(["Synthetic public claim 01-005."], first_detail["supporting_claim_texts"])
        self.assertEqual(1_000_000, first_detail["evidence_match_millionths"])
        self.assertEqual("supported", second_procedure_detail["actual_outcome"])
        self.assertEqual(["claim-02-006"], second_procedure_detail["supporting_claim_ids"])
        self.assertEqual(["Cafe\u0301 STEP 02-006 Kelvin \u212a."], second_procedure_detail["supporting_claim_texts"])
        self.assertEqual(1_000_000, second_procedure_detail["evidence_match_millionths"])

        supported = [item for item in results["results"] if item["actual_outcome"] == "supported"]
        refused = [item for item in results["results"] if item["actual_outcome"] == "refusal"]
        self.assertEqual(18, len(supported))
        self.assertEqual(6, len(refused))
        self.assertTrue(all(item["supporting_claim_count"] == 1 for item in supported))
        self.assertTrue(all(item["evidence_match_millionths"] == 1_000_000 for item in supported))
        self.assertTrue(all(item["supporting_claim_count"] == 0 for item in refused))
        self.assertTrue(all(item["supporting_claim_ids"] == [] for item in refused))
        self.assertTrue(all(item["evidence_match_millionths"] == 0 for item in refused))

    def test_private_question_builder_and_evaluator_split_prompt_from_evidence_query(self):
        paraphrase_definitions = _precommitted_definitions(self.verified)
        fact_record = self.verified["candidate_results"][0]["claim_records"][
            SUPPORTED_QUESTION_ORDINALS["fact"] - 1
        ]
        procedure_record = self.verified["candidate_results"][2]["claim_records"][
            SUPPORTED_QUESTION_ORDINALS["procedure"] - 1
        ]
        paraphrase_definitions[0]["prompt"] = "working controls in the procedure"
        paraphrase_definitions[0]["evidence_query"] = candidate_quality._normalize_question_text(fact_record["claim_text"])
        paraphrase_definitions[9]["prompt"] = "public claim"
        paraphrase_definitions[9]["evidence_query"] = "public claim"

        questions = candidate_quality._build_candidate_quality_questions(self.verified, paraphrase_definitions)

        self.assertEqual(questions, validate_candidate_quality_questions(questions))
        self.assertEqual("working controls in the procedure", questions["questions"][0]["prompt"])
        self.assertEqual(
            candidate_quality._normalize_question_text(fact_record["claim_text"]),
            questions["questions"][0]["evidence_query"],
        )
        self.assertNotIn("evidence_query", questions["questions"][3])

        first_detail = candidate_quality._evaluate_candidate_quality_question(
            self.verified["candidate_results"][0],
            questions["questions"][0],
        )
        with self.assertRaises(ContractError):
            candidate_quality._evaluate_candidate_quality_question(
                self.verified["candidate_results"][2],
                questions["questions"][9],
            )

        self.assertEqual("supported", first_detail["actual_outcome"])
        self.assertEqual(["claim-01-005"], first_detail["supporting_claim_ids"])
        self.assertEqual(1_000_000, first_detail["evidence_match_millionths"])

    def test_builder_rejects_invalid_precommitted_definitions(self):
        definitions = _precommitted_definitions(self.verified)

        missing = definitions[:-1]

        duplicate_category = deepcopy(definitions)
        duplicate_category[1]["category"] = "fact"

        wrong_source = deepcopy(definitions)
        wrong_source[0]["source_digest"] = "sha256:" + "1" * 64

        wrong_expected = deepcopy(definitions)
        wrong_expected[0]["expected_claim_ids"] = ["claim-01-999"]

        unsupported_with_evidence = deepcopy(definitions)
        unsupported_with_evidence[3]["expected_claim_ids"] = ["claim-01-008"]

        for invalid in (
            missing,
            duplicate_category,
            wrong_source,
            wrong_expected,
            unsupported_with_evidence,
        ):
            with self.assertRaises(ContractError):
                candidate_quality._build_candidate_quality_questions(self.verified, invalid)

    def test_evaluator_rejects_foreign_redigested_campaign_binding(self):
        definitions = _precommitted_definitions(self.verified)
        questions = candidate_quality._build_candidate_quality_questions(self.verified, definitions)

        foreign = deepcopy(questions)
        foreign["campaign_id"] = "foreign-campaign-20260812"
        foreign["questions_digest"] = candidate_quality._canonical(
            {key: value for key, value in foreign.items() if key != "questions_digest"},
            "candidate quality questions",
        )

        with self.assertRaises(ContractError):
            candidate_quality._evaluate_candidate_quality_questions(self.verified, foreign)

    def test_question_evaluator_returns_contract_mismatches_for_wrong_expectations_and_wrong_evidence(self):
        definitions = _precommitted_definitions(self.verified)

        wrong_expectation = deepcopy(definitions)
        wrong_expectation[0]["expected_claim_ids"] = ["claim-01-008"]
        wrong_expectation_questions = candidate_quality._build_candidate_quality_questions(
            self.verified,
            wrong_expectation,
        )
        wrong_expectation_results = candidate_quality._evaluate_candidate_quality_questions(
            self.verified,
            wrong_expectation_questions,
        )
        self.assertEqual("supported", wrong_expectation_results["results"][0]["actual_outcome"])
        self.assertEqual(["claim-01-005"], wrong_expectation_results["results"][0]["supporting_claim_ids"])
        self.assertEqual(0, wrong_expectation_results["results"][0]["evidence_match_millionths"])
        self.assertEqual(
            wrong_expectation_results,
            validate_candidate_quality_question_results(wrong_expectation_results),
        )

        unrelated_query = deepcopy(definitions)
        unrelated_query[1]["prompt"] = "query with no matching evidence anywhere"
        unrelated_query[1]["evidence_query"] = "query with no matching evidence anywhere"
        unrelated_questions = candidate_quality._build_candidate_quality_questions(self.verified, unrelated_query)
        unrelated_results = candidate_quality._evaluate_candidate_quality_questions(
            self.verified,
            unrelated_questions,
        )
        self.assertEqual("refusal", unrelated_results["results"][1]["actual_outcome"])
        self.assertEqual([], unrelated_results["results"][1]["supporting_claim_ids"])
        self.assertEqual(0, unrelated_results["results"][1]["evidence_match_millionths"])

        wrong_evidence = deepcopy(definitions)
        wrong_evidence[2]["prompt"] = self.verified["candidate_results"][0]["claim_records"][7]["claim_text"]
        wrong_evidence[2]["evidence_query"] = candidate_quality._normalize_question_text(
            self.verified["candidate_results"][0]["claim_records"][7]["claim_text"]
        )
        wrong_questions = candidate_quality._build_candidate_quality_questions(self.verified, wrong_evidence)
        wrong_results = candidate_quality._evaluate_candidate_quality_questions(
            self.verified,
            wrong_questions,
        )
        self.assertEqual("supported", wrong_results["results"][2]["actual_outcome"])
        self.assertEqual(["claim-01-008"], wrong_results["results"][2]["supporting_claim_ids"])
        self.assertEqual(0, wrong_results["results"][2]["evidence_match_millionths"])

    def test_question_matching_respects_whole_token_boundaries_for_numeric_sequences(self):
        verified = deepcopy(self.verified)
        candidate = verified["candidate_results"][0]

        target = candidate["claim_records"][SUPPORTED_QUESTION_ORDINALS["fact"] - 1]
        collision = candidate["claim_records"][SUPPORTED_QUESTION_ORDINALS["fact"]]
        target["claim_text"] = "Procedure 01-01 is required."
        target["citation_text"] = target["claim_text"]
        collision["claim_text"] = "Procedure 01-010 is prohibited."
        collision["citation_text"] = collision["claim_text"]
        _rebind_claim_record(target)
        _rebind_claim_record(collision)
        _rebind_candidate_result(candidate)

        definitions = _precommitted_definitions(verified)
        definitions[0]["prompt"] = "procedure 01 01"
        definitions[0]["evidence_query"] = "procedure 01 01"
        questions = candidate_quality._build_candidate_quality_questions(verified, definitions)

        detail = candidate_quality._evaluate_candidate_quality_question(
            verified["candidate_results"][0],
            questions["questions"][0],
        )
        self.assertEqual("supported", detail["actual_outcome"])
        self.assertEqual(["claim-01-005"], detail["supporting_claim_ids"])
        self.assertNotIn("claim-01-006", detail["supporting_claim_ids"])
        self.assertEqual(1_000_000, detail["evidence_match_millionths"])

    def test_source_import_graph_avoids_knowledge_and_detail_apis_remain_private(self):
        source = Path(candidate_quality.__file__).read_text(encoding="utf-8")
        self.assertNotIn("from .knowledge import", source)
        self.assertNotIn("import ao_lore.knowledge", source)
        self.assertNotIn("from .synthesis import", source)
        self.assertNotIn("import ao_lore.synthesis", source)
        self.assertNotIn('"build_candidate_quality_questions"', source)
        self.assertNotIn('"evaluate_candidate_quality_questions"', source)


if __name__ == "__main__":
    unittest.main()
