import json
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from ao_lore._strict_io import ContractError
from ao_lore.candidate_quality import build_candidate_quality_sample, build_sampling_policy
from ao_lore.candidate_quality_contracts import (
    SELECTION_PRIORITY,
    validate_candidate_quality_sample,
    validate_candidate_quality_sampling_policy,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_SCRIPT = ROOT / "tests" / "fixtures" / "ao_lore" / "candidate_quality" / "generate.py"
CLAIM_COUNTS = (19, 13, 73, 21, 71, 289)
SAMPLE_ALLOCATIONS = (19, 13, 14, 12, 14, 24)


def _canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _canonical_digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _load_fixture() -> dict:
    completed = subprocess.run(
        [sys.executable, str(FIXTURE_SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


class CandidateQualitySamplingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _load_fixture()
        self.verified = deepcopy(self.fixture["verified_claims"])

    def test_fixture_generator_direct_output_and_check_are_stable(self):
        first = subprocess.run(
            [sys.executable, str(FIXTURE_SCRIPT)],
            check=True,
            capture_output=True,
        )
        second = subprocess.run(
            [sys.executable, str(FIXTURE_SCRIPT)],
            check=True,
            capture_output=True,
        )
        checked = subprocess.run(
            [sys.executable, str(FIXTURE_SCRIPT), "--check"],
            capture_output=True,
        )

        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(0, checked.returncode)

    def test_build_sampling_policy_emits_the_fixed_closed_allocation(self):
        policy = build_sampling_policy(self.verified)

        self.assertEqual(policy, validate_candidate_quality_sampling_policy(policy))
        self.assertEqual(list(SELECTION_PRIORITY), policy["selection_priority"])
        self.assertEqual(6, policy["total_candidate_count"])
        self.assertEqual(486, policy["total_claim_count"])
        self.assertEqual(96, policy["total_sample_count"])
        self.assertEqual(
            [f"candidate-{index:02d}" for index in range(1, 7)],
            [item["candidate_id"] for item in policy["allocations"]],
        )
        self.assertEqual(
            [item["candidate_digest"] for item in self.verified["candidate_results"]],
            [item["candidate_digest"] for item in policy["allocations"]],
        )
        self.assertEqual(
            [item["provenance_digest"] for item in self.verified["candidate_results"]],
            [item["provenance_digest"] for item in policy["allocations"]],
        )
        self.assertEqual(
            [item["source_digest"] for item in self.verified["candidate_results"]],
            [item["source_digest"] for item in policy["allocations"]],
        )
        self.assertEqual(list(CLAIM_COUNTS), [item["claim_count"] for item in policy["allocations"]])
        self.assertEqual(list(SAMPLE_ALLOCATIONS), [item["sample_count"] for item in policy["allocations"]])
        self.assertEqual(self.verified["terminal_readback_digest"], policy["terminal_readback_digest"])

    def test_build_candidate_quality_sample_selects_exactly_ninety_six_distinct_claims(self):
        sample = build_candidate_quality_sample(self.verified)

        self.assertEqual(sample, validate_candidate_quality_sample(sample))
        self.assertEqual(96, sample["total_selected_claims"])
        self.assertEqual(
            len(sample["selections"]),
            len({(item["candidate_id"], item["claim_id"]) for item in sample["selections"]}),
        )
        self.assertEqual(
            list(range(1, 97)),
            [item["sample_ordinal"] for item in sample["selections"]],
        )
        first_two = [item for item in sample["selections"] if item["candidate_id"] in {"candidate-01", "candidate-02"}]
        self.assertEqual(32, len(first_two))
        self.assertEqual(
            [f"claim-01-{ordinal:03d}" for ordinal in range(1, 20)],
            [item["claim_id"] for item in first_two[:19]],
        )
        self.assertEqual(
            [f"claim-02-{ordinal:03d}" for ordinal in range(1, 14)],
            [item["claim_id"] for item in first_two[19:]],
        )

    def test_partial_candidate_selection_respects_the_exact_priority_order(self):
        sample = build_candidate_quality_sample(self.verified)
        candidate_three = [item for item in sample["selections"] if item["candidate_id"] == "candidate-03"]
        reason_index = {item["claim_id"]: tuple(item["reason_codes"]) for item in candidate_three}

        self.assertEqual(
            [
                "claim-03-005",
                "claim-03-020",
                "claim-03-030",
                "claim-03-001",
                "claim-03-037",
                "claim-03-073",
                "claim-03-040",
                "claim-03-041",
                "claim-03-010",
                "claim-03-011",
                "claim-03-012",
                "claim-03-013",
                "claim-03-014",
                "claim-03-015",
            ],
            [item["claim_id"] for item in candidate_three],
        )
        self.assertEqual(("binding_risk",), reason_index["claim-03-005"])
        self.assertEqual(("normalized_duplicate",), reason_index["claim-03-020"])
        self.assertEqual(("fragmentation",), reason_index["claim-03-030"])
        self.assertEqual(("first_position",), reason_index["claim-03-001"])
        self.assertEqual(("middle_position",), reason_index["claim-03-037"])
        self.assertEqual(("final_position",), reason_index["claim-03-073"])
        self.assertEqual(("longest_claim",), reason_index["claim-03-040"])
        self.assertEqual(("shortest_claim",), reason_index["claim-03-041"])
        self.assertEqual(("block_type_representative",), reason_index["claim-03-010"])

    def test_tied_strata_break_by_claim_record_digest(self):
        sample = build_candidate_quality_sample(self.verified)
        candidate_four = [item for item in sample["selections"] if item["candidate_id"] == "candidate-04"]
        source_candidate = self.verified["candidate_results"][3]
        eligible = [record for record in source_candidate["claim_records"] if record["binding_risk_suspect"]]
        expected = {
            record["claim_id"]
            for record in sorted(eligible, key=lambda item: item["claim_record_digest"])[:12]
        }

        self.assertEqual(expected, {item["claim_id"] for item in candidate_four})
        self.assertTrue(all(item["reason_codes"] == ["binding_risk"] for item in candidate_four))

    def test_sampler_is_invariant_to_candidate_and_claim_record_reorder(self):
        baseline_policy = build_sampling_policy(self.verified)
        baseline_sample = build_candidate_quality_sample(self.verified, policy=baseline_policy)

        reordered = deepcopy(self.verified)
        reordered["candidate_results"].reverse()
        for candidate in reordered["candidate_results"]:
            candidate["claim_records"] = list(reversed(candidate["claim_records"]))

        self.assertEqual(baseline_policy, build_sampling_policy(reordered))
        self.assertEqual(baseline_sample, build_candidate_quality_sample(reordered))

    def test_sampler_handles_unexpected_block_types_and_digest_fill(self):
        sample = build_candidate_quality_sample(self.verified)
        candidate_five = [item for item in sample["selections"] if item["candidate_id"] == "candidate-05"]
        reason_index = {item["claim_id"]: tuple(item["reason_codes"]) for item in candidate_five}
        filled = [item["claim_id"] for item in candidate_five if item["reason_codes"] == ["digest_fill"]]

        self.assertIn("claim-05-008", reason_index)
        self.assertIn("claim-05-009", reason_index)
        self.assertEqual(("block_type_representative",), reason_index["claim-05-008"])
        self.assertEqual(("block_type_representative",), reason_index["claim-05-009"])
        self.assertEqual(4, len(filled))

    def test_sampler_rejects_duplicate_claim_identities(self):
        mutated = deepcopy(self.verified)
        mutated["candidate_results"][2]["claim_records"][1]["claim_id"] = mutated["candidate_results"][2]["claim_records"][0]["claim_id"]
        with self.assertRaises(ContractError):
            build_candidate_quality_sample(mutated)

    def test_sampler_rejects_candidate_binding_drift_without_verified_result_rebinding(self):
        mutated = deepcopy(self.verified)
        mutated["candidate_results"][0]["candidate_digest"] = "sha256:" + "1" * 64

        with self.assertRaises(ContractError):
            build_sampling_policy(mutated)

    def test_sampler_rejects_insufficient_claims_for_fixed_policy(self):
        mutated = deepcopy(self.verified)
        mutated["candidate_results"][5]["claim_records"] = mutated["candidate_results"][5]["claim_records"][:20]
        mutated["candidate_results"][5]["claim_count"] = 20
        mutated["total_verified_claim_count"] = sum(item["claim_count"] for item in mutated["candidate_results"])
        mutated["total_verified_citation_count"] = mutated["total_verified_claim_count"]

        with self.assertRaises(ContractError):
            build_sampling_policy(mutated)

    def test_sampler_rejects_changed_policy_and_allocation_mismatch(self):
        policy = build_sampling_policy(self.verified)
        changed_priority = deepcopy(policy)
        changed_priority["selection_priority"] = list(reversed(changed_priority["selection_priority"]))

        changed_allocation = deepcopy(policy)
        changed_allocation["allocations"][2]["sample_count"] = 15

        with self.assertRaises(ContractError):
            build_candidate_quality_sample(self.verified, policy=changed_priority)
        with self.assertRaises(ContractError):
            build_candidate_quality_sample(self.verified, policy=changed_allocation)

    def test_sampler_rejects_missing_terminal_readback_digest(self):
        mutated = deepcopy(self.verified)
        mutated.pop("terminal_readback_digest")

        with self.assertRaises(ContractError):
            build_sampling_policy(mutated)

    def test_sampler_rejects_supplied_policy_with_terminal_digest_drift_even_if_self_consistent(self):
        policy = build_sampling_policy(self.verified)
        drifted = deepcopy(policy)
        drifted["terminal_readback_digest"] = "sha256:" + "0" * 64
        drifted["policy_digest"] = _canonical_digest({key: value for key, value in drifted.items() if key != "policy_digest"})

        with self.assertRaises(ContractError):
            build_candidate_quality_sample(self.verified, policy=drifted)

    def test_sampler_rejects_supplied_policy_with_candidate_digest_drift_even_if_self_consistent(self):
        policy = build_sampling_policy(self.verified)
        drifted = deepcopy(policy)
        drifted["allocations"][0]["candidate_digest"] = "sha256:" + "2" * 64
        drifted["policy_digest"] = _canonical_digest({key: value for key, value in drifted.items() if key != "policy_digest"})

        with self.assertRaises(ContractError):
            build_candidate_quality_sample(self.verified, policy=drifted)

    def test_fixture_generator_rejects_sample_count_drift_in_copied_spec(self):
        with tempfile.TemporaryDirectory() as tempdir:
            fixture_dir = Path(tempdir) / "candidate_quality"
            shutil.copytree(FIXTURE_SCRIPT.parent, fixture_dir)
            spec_path = fixture_dir / "fixture-spec.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["sample_counts"] = [19, 13, 14, 12, 14, 25]
            spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            script_path = fixture_dir / "generate.py"

            direct = subprocess.run([sys.executable, str(script_path)], capture_output=True)
            checked = subprocess.run([sys.executable, str(script_path), "--check"], capture_output=True)

            self.assertNotEqual(0, direct.returncode)
            self.assertNotEqual(0, checked.returncode)

    def test_sampler_reruns_with_byte_identical_policy_and_sample(self):
        first_policy = build_sampling_policy(self.verified)
        second_policy = build_sampling_policy(self.verified)
        first_sample = build_candidate_quality_sample(self.verified, policy=first_policy)
        second_sample = build_candidate_quality_sample(self.verified, policy=second_policy)

        self.assertEqual(_canonical_bytes(first_policy), _canonical_bytes(second_policy))
        self.assertEqual(_canonical_bytes(first_sample), _canonical_bytes(second_sample))


if __name__ == "__main__":
    unittest.main()
