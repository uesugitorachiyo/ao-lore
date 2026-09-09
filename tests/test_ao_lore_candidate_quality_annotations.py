import json
import subprocess
import sys
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore._strict_io import ContractError
from ao_lore import candidate_quality
from ao_lore.candidate_quality_contracts import ANNOTATION_LABELS, validate_candidate_quality_annotations


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SCRIPT = ROOT / "tests" / "fixtures" / "ao_lore" / "candidate_quality" / "generate.py"
ANNOTATIONS_SCHEMA_PATH = ROOT / "schemas" / "ao-lore" / "candidate-quality-annotations-v0.1.schema.json"


def _load_fixture() -> dict:
    completed = subprocess.run(
        [sys.executable, str(FIXTURE_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def _completed_template(template: dict) -> dict:
    completed = deepcopy(template)
    for index, entry in enumerate(completed["annotations"]):
        entry["classification"] = ANNOTATION_LABELS[index % len(ANNOTATION_LABELS)]
        entry["rationale"] = f"synthetic rationale {index + 1:03d}"
    return completed


class CandidateQualityAnnotationTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = _load_fixture()
        self.verified = fixture["verified_claims"]
        self.sample = candidate_quality.build_candidate_quality_sample(self.verified)
        self.annotation_schema = json.loads(ANNOTATIONS_SCHEMA_PATH.read_text(encoding="utf-8"))

    def test_private_annotation_template_is_deterministic_and_detached_and_not_exported(self):
        self.assertFalse(hasattr(candidate_quality, "build_candidate_quality_annotation_template"))
        self.assertTrue(hasattr(candidate_quality, "_build_candidate_quality_annotation_template"))
        self.assertNotIn("_build_candidate_quality_annotation_template", candidate_quality.__all__)

        first = candidate_quality._build_candidate_quality_annotation_template(
            self.verified,
            self.sample,
            reviewer_id="reviewer-01",
            reviewed_at="2026-08-12T12:34:56Z",
        )
        second = candidate_quality._build_candidate_quality_annotation_template(
            self.verified,
            self.sample,
            reviewer_id="reviewer-01",
            reviewed_at="2026-08-12T12:34:56Z",
        )

        self.assertEqual(first, second)
        self.assertEqual(self.sample["sample_digest"], first["sample_digest"])
        self.assertEqual(self.sample["policy_digest"], first["policy_digest"])
        self.assertEqual("reviewer-01", first["reviewer_id"])
        self.assertEqual("2026-08-12T12:34:56Z", first["reviewed_at"])
        self.assertEqual(96, len(first["annotations"]))
        self.assertEqual(
            list(range(1, 97)),
            [entry["sample_ordinal"] for entry in first["annotations"]],
        )
        self.assertIn("claim_text", first["annotations"][0])
        self.assertIn("citation_text", first["annotations"][0])
        self.assertEqual(first["annotations"][0]["claim_text"], first["annotations"][0]["citation_text"])
        self.assertNotIn("classification", first["annotations"][0])
        self.assertNotIn("rationale", first["annotations"][0])

        first["annotations"][0]["claim_text"] = "mutated"
        regenerated = candidate_quality._build_candidate_quality_annotation_template(
            self.verified,
            self.sample,
            reviewer_id="reviewer-01",
            reviewed_at="2026-08-12T12:34:56Z",
        )
        self.assertEqual(second, regenerated)

    def test_private_annotation_artifact_builder_emits_counts_and_is_not_exported(self):
        self.assertFalse(hasattr(candidate_quality, "build_candidate_quality_annotations"))
        self.assertTrue(hasattr(candidate_quality, "_build_candidate_quality_annotations"))
        self.assertNotIn("_build_candidate_quality_annotations", candidate_quality.__all__)

        template = candidate_quality._build_candidate_quality_annotation_template(
            self.verified,
            self.sample,
            reviewer_id="reviewer-01",
            reviewed_at="2026-08-12T12:34:56Z",
        )
        completed = _completed_template(template)
        published = candidate_quality._build_candidate_quality_annotations(
            self.verified,
            self.sample,
            completed,
        )

        self.assertEqual(published, validate_candidate_quality_annotations(published))
        self.assertEqual("reviewer-01", published["reviewer_id"])
        self.assertEqual("2026-08-12T12:34:56Z", published["reviewed_at"])
        self.assertEqual(self.sample["sample_digest"], published["sample_digest"])
        self.assertEqual(
            {
                "useful_exact": 16,
                "exact_but_fragmented": 16,
                "exact_but_low_value": 16,
                "duplicate": 16,
                "misleading_without_context": 16,
                "binding_error": 16,
            },
            published["annotation_counts"],
        )
        self.assertEqual(96, sum(published["annotation_counts"].values()))
        self.assertEqual(
            [entry["candidate_id"] for entry in self.sample["selections"]],
            [entry["candidate_id"] for entry in published["annotations"]],
        )
        self.assertEqual(
            [entry["citation_id"] for entry in self.sample["selections"]],
            [entry["citation_id"] for entry in published["annotations"]],
        )
        self.assertNotIn("claim_text", published["annotations"][0])
        self.assertNotIn("citation_text", published["annotations"][0])

        completed["annotations"][0]["classification"] = "binding_error"
        self.assertEqual("useful_exact", published["annotations"][0]["classification"])

    def test_private_annotation_artifact_builder_rejects_incomplete_unknown_and_drifted_annotations(self):
        template = candidate_quality._build_candidate_quality_annotation_template(
            self.verified,
            self.sample,
            reviewer_id="reviewer-01",
            reviewed_at="2026-08-12T12:34:56Z",
        )
        completed = _completed_template(template)

        missing = deepcopy(completed)
        missing["annotations"].pop()

        duplicate = deepcopy(completed)
        duplicate["annotations"][1]["sample_ordinal"] = duplicate["annotations"][0]["sample_ordinal"]
        duplicate["annotations"][1]["claim_id"] = duplicate["annotations"][0]["claim_id"]

        unsampled = deepcopy(completed)
        unsampled["annotations"][0]["claim_id"] = "claim-03-999"

        unknown = deepcopy(completed)
        unknown["annotations"][0]["classification"] = "invented_label"

        invalid_time = deepcopy(completed)
        invalid_time["reviewed_at"] = "2026-08-12 12:34:56"

        invalid_rationale = deepcopy(completed)
        invalid_rationale["annotations"][0]["rationale"] = "x" * 513

        policy_drift = deepcopy(completed)
        policy_drift["annotations"][0]["policy_digest"] = "sha256:" + "1" * 64

        source_drift = deepcopy(completed)
        source_drift["annotations"][0]["source_digest"] = "sha256:" + "2" * 64

        citation_drift = deepcopy(completed)
        citation_drift["annotations"][0]["citation_id"] = "citation-03-999"

        stale_sample = deepcopy(self.sample)
        stale_sample["selections"][0]["citation_id"] = "citation-03-998"

        for invalid in (
            missing,
            duplicate,
            unsampled,
            unknown,
            invalid_time,
            invalid_rationale,
            policy_drift,
            source_drift,
            citation_drift,
        ):
            with self.assertRaises(ContractError):
                candidate_quality._build_candidate_quality_annotations(
                    self.verified,
                    self.sample,
                    invalid,
                )

        with self.assertRaises(ContractError):
            candidate_quality._build_candidate_quality_annotations(
                self.verified,
                stale_sample,
                completed,
            )

    def test_annotations_require_closed_counts_in_schema_and_runtime(self):
        template = candidate_quality._build_candidate_quality_annotation_template(
            self.verified,
            self.sample,
            reviewer_id="reviewer-01",
            reviewed_at="2026-08-12T12:34:56Z",
        )
        completed = _completed_template(template)
        published = candidate_quality._build_candidate_quality_annotations(
            self.verified,
            self.sample,
            completed,
        )

        missing_counts = deepcopy(published)
        missing_counts.pop("annotation_counts")
        digest_body = {key: value for key, value in missing_counts.items() if key != "annotations_digest"}
        missing_counts["annotations_digest"] = candidate_quality._canonical(
            digest_body,
            "candidate quality annotations",
        )

        wrong_counts = deepcopy(published)
        wrong_counts["annotation_counts"]["useful_exact"] -= 1
        wrong_counts["annotation_counts"]["binding_error"] += 1
        wrong_counts["annotations_digest"] = candidate_quality._canonical(
            {key: value for key, value in wrong_counts.items() if key != "annotations_digest"},
            "candidate quality annotations",
        )

        validator = Draft202012Validator(self.annotation_schema)
        self.assertFalse(validator.is_valid(missing_counts))
        with self.assertRaises(ContractError):
            validate_candidate_quality_annotations(missing_counts)

        self.assertTrue(validator.is_valid(wrong_counts))
        with self.assertRaises(ContractError):
            validate_candidate_quality_annotations(wrong_counts)
