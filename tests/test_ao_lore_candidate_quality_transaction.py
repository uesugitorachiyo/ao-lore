import copy
import json
import tempfile
import threading
import importlib.util
import unittest
from pathlib import Path

from ao_lore._strict_io import ContractError
from ao_lore.candidate_quality import (
    _derive_candidate_quality_results,
    _publish_candidate_quality_campaign,
)
from tests.private_calibration import private_calibration


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".ao-lore/public-candidate-quality-review-20260812/artifacts"


@private_calibration
class CandidateQualityTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inputs = {
            name: json.loads((ARTIFACTS / f"{name}.json").read_text())
            for name in ("verified", "policy", "sample", "annotations", "questions", "question-results")
        }

    def _derive(self, *, annotations=None):
        return _derive_candidate_quality_results(
            self.inputs["verified"], self.inputs["policy"], self.inputs["sample"],
            annotations or self.inputs["annotations"], self.inputs["questions"], self.inputs["question-results"]
        )

    def test_real_evidence_derives_six_exact_non_authoritative_holds(self):
        results, campaign, summary = self._derive()
        expected_ids = [row["candidate_id"] for row in self.inputs["verified"]["candidate_results"]]
        self.assertEqual([row["candidate_id"] for row in results], expected_ids)
        self.assertEqual([row["recommendation"] for row in results], ["hold"] * 6)
        self.assertEqual(summary["corpus_recommendation"], "hold")
        self.assertEqual(summary["campaign_digest"], campaign["campaign_digest"])
        for artifact in [*results, summary]:
            for key in (
                "canonical_query_invoked", "review_event_appended", "candidate_decision_taken",
                "promotion_prepared", "promotion_applied", "provider_calls", "network_accessed",
                "credential_used", "publication", "release", "deployment", "authority_advanced",
            ):
                self.assertIs(artifact[key], False)

    def test_material_binding_error_rejects(self):
        annotations = copy.deepcopy(self.inputs["annotations"])
        annotations["annotations"][0]["classification"] = "binding_error"
        annotations["annotation_counts"]["useful_exact"] -= 1
        annotations["annotation_counts"]["binding_error"] += 1
        annotations.pop("annotations_digest")
        from ao_lore.candidate_quality import _self_digest
        annotations = _self_digest(annotations, "annotations_digest", "candidate quality annotations")
        results, _campaign, _summary = self._derive(annotations=annotations)
        self.assertEqual(results[0]["recommendation"], "reject")

    def test_publication_is_exclusive_idempotent_and_preserves_foreign_state(self):
        results, campaign, summary = self._derive()
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as raw:
            root = Path(raw)
            (root / "foreign.txt").write_text("keep", encoding="utf-8")
            first = _publish_candidate_quality_campaign(root, results, campaign, summary)
            second = _publish_candidate_quality_campaign(root, results, campaign, summary)
            self.assertEqual(first, second)
            self.assertEqual((root / "foreign.txt").read_text(), "keep")
            self.assertTrue((root / first["publication_relpath"] / "summary.json").is_file())
            self.assertTrue((root / first["publication_relpath"] / "recovery.json").is_file())

    def test_collision_and_symlink_root_fail_closed(self):
        results, campaign, summary = self._derive()
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as raw:
            root = Path(raw)
            readback = _publish_candidate_quality_campaign(root, results, campaign, summary)
            target = root / readback["publication_relpath"] / "summary.json"
            target.write_text("{}", encoding="utf-8")
            with self.assertRaises(ContractError):
                _publish_candidate_quality_campaign(root, results, campaign, summary)
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as raw:
            base = Path(raw)
            target = base / "target"
            target.mkdir()
            link = base / "link"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(ContractError):
                _publish_candidate_quality_campaign(link, results, campaign, summary)

    def test_every_durable_boundary_resumes_exactly(self):
        results, campaign, summary = self._derive()
        for phase in ("intent", "results", "summary", "recovery", "publication"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as raw:
                root = Path(raw)
                with self.assertRaises(RuntimeError):
                    _publish_candidate_quality_campaign(root, results, campaign, summary, fail_after=phase)
                staged = next(root.glob("staging-*") , None)
                if staged is not None:
                    recovery_records = list(staged.glob("recovery-*.json"))
                    self.assertTrue(recovery_records)
                resumed = _publish_candidate_quality_campaign(root, results, campaign, summary)
                self.assertEqual(resumed["campaign_digest"], summary["campaign_digest"])

    def test_foreign_staging_is_preserved(self):
        results, campaign, summary = self._derive()
        suffix = summary["campaign_digest"].split(":", 1)[1][:32]
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as raw:
            root = Path(raw)
            stage = root / f"staging-{suffix}"
            stage.mkdir()
            (stage / "foreign.txt").write_text("keep", encoding="utf-8")
            with self.assertRaises((ContractError, FileNotFoundError)):
                _publish_candidate_quality_campaign(root, results, campaign, summary)
            self.assertEqual((stage / "foreign.txt").read_text(), "keep")

    def test_competing_writers_serialize_to_one_exact_publication(self):
        results, campaign, summary = self._derive()
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as raw:
            root = Path(raw)
            outputs = []
            errors = []
            barrier = threading.Barrier(2)

            def publish():
                try:
                    barrier.wait()
                    outputs.append(_publish_candidate_quality_campaign(root, results, campaign, summary))
                except BaseException as exc:  # test captures worker failure for the parent assertion
                    errors.append(exc)

            threads = [threading.Thread(target=publish) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(errors)
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(len(list(root.glob("campaign-*"))), 1)

    def test_script_check_refuses_to_create_missing_publication(self):
        script = ROOT / "scripts/rehearse-public-candidate-quality-review.py"
        spec = importlib.util.spec_from_file_location("candidate_quality_rehearsal", script)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as raw:
            module.PUBLICATION_ROOT = Path(raw)
            with self.assertRaises((SystemExit, ContractError)):
                module.main(["--check"])
            self.assertEqual(list(Path(raw).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
