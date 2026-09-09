import json
import unittest
from copy import deepcopy
from pathlib import Path

from jsonschema import Draft202012Validator

from ao_lore._strict_io import ContractError, parse_strict_json
from ao_lore.ocr_contracts import (
    AUTHORITY_FIELDS,
    OCR_CANDIDATES,
    OCR_POLICY_DIGEST,
    validate_model_set,
    validate_qualification,
    validate_raster_manifest,
    validate_runtime,
    validate_uat_readback,
    validate_worker_contract,
    validate_worker_result,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "schemas" / "ao-lore"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


class PrivateOcrContractTests(unittest.TestCase):
    def runtime(self):
        return {
            "schema_version": "ao.lore.private-ocr-runtime.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "python_version": "3.12",
            "paddleocr_version": "3.7.0",
            "paddlepaddle_gpu_version": "3.3.0",
            "cuda_version": "12.6",
            "runtime_digest": DIGEST_A,
            "gpu_identity_digest": DIGEST_B,
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def model_set(self):
        return {
            "schema_version": "ao.lore.private-ocr-model-set.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "candidates": [
                {
                    "candidate_id": f"candidate-{index}",
                    "detection_model": pair[0],
                    "recognition_model": pair[1],
                    "model_set_digest": (DIGEST_A, DIGEST_B, DIGEST_C)[index - 1],
                }
                for index, pair in enumerate(OCR_CANDIDATES, 1)
            ],
            "page_orientation_model_digest": DIGEST_A,
            "text_line_orientation_model_digest": DIGEST_B,
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def raster(self):
        return {
            "schema_version": "ao.lore.private-ocr-raster-manifest.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "source_id": "source-01",
            "source_digest": DIGEST_A,
            "configuration_digest": DIGEST_B,
            "renderer_digest": DIGEST_C,
            "dpi": 300,
            "pages": [{"page_id": "page-0001", "width": 1200, "height": 1600, "size": 128, "digest": DIGEST_A}],
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def worker_contract(self):
        return {
            "schema_version": "ao.lore.private-ocr-worker-contract.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "run_id": "run-01",
            "candidate_id": "candidate-1",
            "runtime_digest": DIGEST_A,
            "model_set_digest": DIGEST_B,
            "raster_manifest_digest": DIGEST_C,
            "configuration_digest": DIGEST_A,
            "gpu_identity_digest": DIGEST_B,
            "page_ids": ["page-0001"],
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def worker_result(self):
        return {
            "schema_version": "ao.lore.private-ocr-worker-result.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "run_id": "run-01",
            "candidate_id": "candidate-1",
            "runtime_digest": DIGEST_A,
            "model_set_digest": DIGEST_B,
            "configuration_digest": DIGEST_A,
            "latency_microseconds": 123456,
            "peak_gpu_memory_bytes": 987654321,
            "pages": [{
                "page_id": "page-0001", "width": 1200, "height": 1600,
                "detections": [{
                    "index": 0, "text": "Public fixture text", "confidence_millionths": 990000,
                    "polygon": [[10, 10], [200, 10], [200, 40], [10, 40]],
                }],
            }],
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def qualification(self):
        candidate = {
            "candidate_id": "candidate-1", "result_digest": DIGEST_A,
            "character_accuracy_millionths": 990000, "word_accuracy_millionths": 970000,
            "detection_precision_millionths": 990000, "detection_recall_millionths": 970000,
            "reading_order_pair_accuracy_millionths": 1000000,
            "mean_polygon_iou_millionths": 850000, "page_coverage_millionths": 1000000,
            "hallucinated_lines": 0, "expected_outcome_accuracy_millionths": 1000000,
            "mean_latency_microseconds": 123456,
            "peak_gpu_memory_bytes": 987654321,
            "repeatability_identical": True, "all_hard_gates_pass": True,
        }
        return {
            "schema_version": "ao.lore.private-ocr-qualification.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "runtime_digest": DIGEST_A,
            "corpus_digest": DIGEST_B,
            "oracle_digest": DIGEST_C,
            "configuration_digest": DIGEST_A,
            "candidates": [candidate, {**candidate, "candidate_id": "candidate-2", "result_digest": DIGEST_B}, {**candidate, "candidate_id": "candidate-3", "result_digest": DIGEST_C}],
            "selected_candidate_id": "candidate-1",
            "decision": "hold",
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def uat(self):
        return {
            "schema_version": "ao.lore.private-ocr-uat-readback.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "qualification_digest": DIGEST_A,
            "corpus_digest": DIGEST_B,
            "aggregate_digest": DIGEST_C,
            "initial_conversions": 1,
            "resumed_conversions": 3,
            "rerun_conversions": 0,
            "successful_documents": 4,
            "rejected_documents": 0,
            "calibration_attempts_per_item": 2,
            "decision": "hold",
            "brain_before_digest": DIGEST_A,
            "brain_after_work_digest": DIGEST_A,
            "brain_after_cleanup_digest": DIGEST_A,
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def cases(self):
        return (
            ("private-ocr-runtime-v0.1.schema.json", self.runtime(), validate_runtime),
            ("private-ocr-model-set-v0.1.schema.json", self.model_set(), validate_model_set),
            ("private-ocr-raster-manifest-v0.1.schema.json", self.raster(), validate_raster_manifest),
            ("private-ocr-worker-contract-v0.1.schema.json", self.worker_contract(), validate_worker_contract),
            ("private-ocr-worker-result-v0.1.schema.json", self.worker_result(), validate_worker_result),
            ("private-ocr-qualification-v0.1.schema.json", self.qualification(), validate_qualification),
            ("private-ocr-uat-readback-v0.1.schema.json", self.uat(), validate_uat_readback),
        )

    def test_exact_candidate_matrix_and_policy_are_fixed(self):
        self.assertEqual(OCR_CANDIDATES, (
            ("PP-OCRv6_medium_det", "PP-OCRv6_medium_rec"),
            ("PP-OCRv6_small_det", "PP-OCRv6_small_rec"),
            ("PP-OCRv5_mobile_det", "en_PP-OCRv5_mobile_rec"),
        ))
        self.assertRegex(OCR_POLICY_DIGEST, r"^sha256:[0-9a-f]{64}$")

    def test_schema_and_runtime_validators_accept_exact_detached_values(self):
        for name, sample, validator in self.cases():
            with self.subTest(name=name):
                schema = json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(sample)
                validated = validator(sample)
                self.assertEqual(validated, sample)
                self.assertIsNot(validated, sample)
                sample.clear()
                self.assertTrue(validated)

    def test_unknown_hostile_and_authority_values_reject(self):
        for name, sample, validator in self.cases():
            with self.subTest(name=name, mutation="unknown"):
                value = deepcopy(sample)
                value["unknown"] = 1
                with self.assertRaises(ContractError):
                    validator(value)
            with self.subTest(name=name, mutation="authority"):
                value = deepcopy(sample)
                value[AUTHORITY_FIELDS[0]] = True
                with self.assertRaises(ContractError):
                    validator(value)
            with self.subTest(name=name, mutation="hostile-bool"):
                value = deepcopy(sample)
                if "initial_conversions" in value:
                    value["initial_conversions"] = True
                elif "dpi" in value:
                    value["dpi"] = True
                else:
                    value["policy_digest"] = str.__new__(type("Hostile", (str,), {}), value["policy_digest"])
                with self.assertRaises(ContractError):
                    validator(value)

    def test_duplicate_json_keys_reject_before_validation(self):
        body = b'{"schema_version":"x","schema_version":"y"}'
        with self.assertRaises(ContractError):
            parse_strict_json(body, "private OCR contract")


if __name__ == "__main__":
    unittest.main()
