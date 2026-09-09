import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from ao_lore.model_roles import RoleConfigError, activate_ocr_parser, load_ocr_parser_activation
from ao_lore.ocr_benchmark import (
    OcrBenchmarkError,
    OcrCandidatePerformance,
    ocr_benchmark_configuration_digest,
    select_ocr_candidate,
)
from ao_lore.ocr_contracts import AUTHORITY_FIELDS, OCR_POLICY_DIGEST
from ao_lore.parsing import default_capability_profiles


DIGEST = "sha256:" + "d" * 64
MODEL_DIGESTS = tuple("sha256:" + digit * 64 for digit in "123")
CORPUS_CONFIGURATION_DIGEST = "sha256:" + "c" * 64
CORPUS_DIGEST = "sha256:" + "a" * 64
ORACLE_DIGEST = "sha256:" + "b" * 64


def candidate(candidate_id, *, character=990000, recall=980000, order=1000000, passing=True):
    return {
        "candidate_id": candidate_id,
        "result_digest": "sha256:" + candidate_id[-1] * 64,
        "character_accuracy_millionths": character,
        "word_accuracy_millionths": 990000,
        "detection_precision_millionths": 990000,
        "detection_recall_millionths": recall,
        "reading_order_pair_accuracy_millionths": order,
        "mean_polygon_iou_millionths": 900000,
        "page_coverage_millionths": 1000000,
        "expected_outcome_accuracy_millionths": 1000000,
        "hallucinated_lines": 0,
        "mean_latency_microseconds": 123456,
        "peak_gpu_memory_bytes": 987654321,
        "repeatability_identical": True,
        "all_hard_gates_pass": passing,
    }


def qualification():
    configuration_digest = ocr_benchmark_configuration_digest(
        corpus_configuration_digest=CORPUS_CONFIGURATION_DIGEST,
        model_digests=MODEL_DIGESTS,
    )
    return {
        "schema_version": "ao.lore.private-ocr-qualification.v0.1",
        "policy_digest": OCR_POLICY_DIGEST,
        "runtime_digest": DIGEST,
        "corpus_digest": CORPUS_DIGEST,
        "oracle_digest": ORACLE_DIGEST,
        "configuration_digest": configuration_digest,
        "candidates": [candidate(f"candidate-{index}") for index in range(1, 4)],
        "selected_candidate_id": "candidate-1",
        "decision": "hold",
        **{field: False for field in AUTHORITY_FIELDS},
    }


def body_and_digest(value):
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    return body, "sha256:" + hashlib.sha256(body).hexdigest()


class OcrActivationTests(unittest.TestCase):
    def test_closed_ranking_uses_only_passing_candidates_and_exact_keys(self):
        rows = (
            candidate("candidate-1", character=990000),
            candidate("candidate-2", character=995000),
            candidate("candidate-3", character=995000),
        )
        performance = (
            OcrCandidatePerformance("candidate-1", 900, 90),
            OcrCandidatePerformance("candidate-2", 800, 80),
            OcrCandidatePerformance("candidate-3", 700, 80),
        )
        selection = select_ocr_candidate(rows, performance)
        self.assertEqual(
            selection.rank_keys,
            ("character_error", "hallucinations", "detection_recall", "reading_order", "peak_gpu_memory", "mean_latency", "parser_id"),
        )
        self.assertEqual(selection.selected_candidate_id, "candidate-3")
        self.assertEqual(selection.decision, "hold")

        failed = list(copy.deepcopy(rows))
        failed[2]["all_hard_gates_pass"] = False
        selection = select_ocr_candidate(tuple(failed), performance)
        self.assertEqual(selection.selected_candidate_id, "candidate-2")
        self.assertEqual(selection.decision, "candidate_change")

    def test_activation_requires_exact_retained_hold_and_exposes_only_ocr(self):
        value = qualification()
        body, digest = body_and_digest(value)
        activation = activate_ocr_parser(
            body,
            qualification_digest=digest,
            runtime_digest=DIGEST,
            corpus_digest=CORPUS_DIGEST,
            corpus_configuration_digest=CORPUS_CONFIGURATION_DIGEST,
            oracle_digest=ORACLE_DIGEST,
            model_digests=MODEL_DIGESTS,
            selected_result_digest=value["candidates"][0]["result_digest"],
        )
        self.assertEqual(activation.capability, "ocr-layout")
        self.assertEqual(activation.candidate_id, "candidate-1")
        self.assertFalse(activation.provider_enabled)
        self.assertFalse(activation.fallback_enabled)

        before = {item["parser_id"]: item for item in default_capability_profiles()}
        after = {item["parser_id"]: item for item in default_capability_profiles(ocr_activation=activation)}
        self.assertEqual(before["docling"], after["docling"])
        self.assertFalse(after["docling"].get("ocr_capable", False))
        self.assertEqual(before["native-docx-ooxml"], after["native-docx-ooxml"])
        self.assertNotIn("ocr-layout", after)
        self.assertTrue(after["paddle-ocr-english"]["runtime_available"])
        self.assertEqual(after["paddle-ocr-english"]["capability_id"], "ocr-layout")
        self.assertEqual(after["paddle-ocr-english"]["benchmark_result_refs"], [digest])

    def test_activation_rejects_drift_nonhold_failed_gate_authority_and_duplicates(self):
        base = qualification()
        mutations = []
        for field, value in (
            ("runtime_digest", "sha256:" + "0" * 64),
            ("corpus_digest", "sha256:" + "0" * 64),
            ("oracle_digest", "sha256:" + "0" * 64),
            ("configuration_digest", "sha256:" + "0" * 64),
            ("promotion", True),
        ):
            changed = copy.deepcopy(base); changed[field] = value; mutations.append(changed)
        changed = copy.deepcopy(base); changed["candidates"][0]["all_hard_gates_pass"] = False; mutations.append(changed)
        changed = copy.deepcopy(base); changed["candidates"][0]["repeatability_identical"] = False; mutations.append(changed)
        for changed in mutations:
            body, digest = body_and_digest(changed)
            with self.subTest(changed=changed):
                with self.assertRaises(RoleConfigError):
                    activate_ocr_parser(
                        body, qualification_digest=digest, runtime_digest=DIGEST,
                        corpus_digest=CORPUS_DIGEST,
                        corpus_configuration_digest=CORPUS_CONFIGURATION_DIGEST,
                        oracle_digest=ORACLE_DIGEST, model_digests=MODEL_DIGESTS,
                        selected_result_digest=base["candidates"][0]["result_digest"],
                    )
        body, digest = body_and_digest(base)
        with self.assertRaises(RoleConfigError):
            activate_ocr_parser(
                body, qualification_digest="sha256:" + "0" * 64,
                runtime_digest=DIGEST, corpus_digest=CORPUS_DIGEST,
                corpus_configuration_digest=CORPUS_CONFIGURATION_DIGEST,
                oracle_digest=ORACLE_DIGEST, model_digests=MODEL_DIGESTS,
                selected_result_digest=base["candidates"][0]["result_digest"],
            )
        duplicate = body.replace(b'"decision":"hold"', b'"decision":"hold","decision":"hold"')
        with self.assertRaises(RoleConfigError):
            activate_ocr_parser(
                duplicate, qualification_digest="sha256:" + hashlib.sha256(duplicate).hexdigest(),
                runtime_digest=DIGEST, corpus_digest=CORPUS_DIGEST,
                corpus_configuration_digest=CORPUS_CONFIGURATION_DIGEST,
                oracle_digest=ORACLE_DIGEST, model_digests=MODEL_DIGESTS,
                selected_result_digest=base["candidates"][0]["result_digest"],
            )

    def test_activation_accepts_reviewed_candidate_change_winner(self):
        value = qualification()
        value["decision"] = "candidate_change"
        value["candidates"][2]["all_hard_gates_pass"] = False
        value["candidates"][2]["expected_outcome_accuracy_millionths"] = 950000
        value["candidates"][2]["hallucinated_lines"] = 2
        body, digest = body_and_digest(value)
        activation = activate_ocr_parser(
            body,
            qualification_digest=digest,
            runtime_digest=DIGEST,
            corpus_digest=CORPUS_DIGEST,
            corpus_configuration_digest=CORPUS_CONFIGURATION_DIGEST,
            oracle_digest=ORACLE_DIGEST,
            model_digests=MODEL_DIGESTS,
            selected_result_digest=value["candidates"][0]["result_digest"],
        )
        self.assertEqual(activation.candidate_id, "candidate-1")

    def test_activation_loader_requires_contained_regular_retained_file(self):
        value = qualification()
        body, digest = body_and_digest(value)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            retained = root / "qualification.json"
            retained.write_bytes(body)
            kwargs = {
                "qualification_digest": digest, "runtime_digest": DIGEST,
                "corpus_digest": CORPUS_DIGEST,
                "corpus_configuration_digest": CORPUS_CONFIGURATION_DIGEST,
                "oracle_digest": ORACLE_DIGEST, "model_digests": MODEL_DIGESTS,
                "selected_result_digest": value["candidates"][0]["result_digest"],
            }
            self.assertEqual(
                load_ocr_parser_activation(str(retained), evidence_root=str(root), **kwargs).candidate_id,
                "candidate-1",
            )
            outside = root.parent / (root.name + "-outside.json")
            outside.write_bytes(body)
            self.addCleanup(outside.unlink)
            with self.assertRaises(RoleConfigError):
                load_ocr_parser_activation(str(outside), evidence_root=str(root), **kwargs)
            retained.unlink()
            retained.symlink_to(outside)
            with self.assertRaises(RoleConfigError):
                load_ocr_parser_activation(str(retained), evidence_root=str(root), **kwargs)
    def test_ranking_rejects_missing_duplicate_or_mismatched_performance(self):
        rows = tuple(candidate(f"candidate-{index}") for index in range(1, 4))
        good = tuple(OcrCandidatePerformance(f"candidate-{index}", index, index) for index in range(1, 4))
        for bad_rows, bad_performance in (
            (rows[:2], good),
            (rows, good[:2]),
            (rows, (good[0], good[0], good[2])),
        ):
            with self.assertRaises(OcrBenchmarkError):
                select_ocr_candidate(bad_rows, bad_performance)


if __name__ == "__main__":
    unittest.main()
