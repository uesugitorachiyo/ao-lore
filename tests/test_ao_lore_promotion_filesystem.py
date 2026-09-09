import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ao_lore.promotion import PromotionError, _PromotionDependencies, _inventory, _promotion_lock, _publish_directory_no_replace, stable_json_file
from ao_lore.candidates import (
    _CandidateReviewDependencies,
    _append_review_with_dependencies,
)
from tests.test_ao_lore_promotion_prepare import build_fixture


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "tests" / "fixtures" / "ao_lore" / "promotion" / "generate.py"


class PromotionFilesystemTests(unittest.TestCase):
    def test_fixture_generator_is_deterministic_and_checkable(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            target = Path(name) / "fixture"
            subprocess.run(["python3", str(GENERATOR), "--out", str(target)], check=True)
            subprocess.run(["python3", str(GENERATOR), "--out", str(target), "--check"], check=True)
            manifest = json.loads((target / "fixture-manifest.json").read_text())
            self.assertEqual("ao.lore.promotion-fixture.v0.1", manifest["schema_version"])
            self.assertEqual(["candidate-public-fixture"], manifest["candidate_ids"])
            self.assertNotIn(str(ROOT), json.dumps(manifest))

    def test_stable_json_rejects_duplicate_keys_links_and_oversize(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            duplicate = root / "duplicate.json"
            duplicate.write_text('{"a":1,"a":2}\n')
            with self.assertRaises(PromotionError):
                stable_json_file(duplicate, root=root, label="fixture")
            good = root / "good.json"
            good.write_text('{"a":1}\n')
            linked = root / "linked.json"
            os.link(good, linked)
            with self.assertRaises(PromotionError):
                stable_json_file(good, root=root, label="fixture")
            large = root / "large.json"
            large.write_bytes(b"{" + b" " * (1024 * 1024) + b"}")
            with self.assertRaises(PromotionError):
                stable_json_file(large, root=root, label="fixture")

    def test_publication_is_descriptor_anchored_against_parent_symlink_swap(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name); staging = root / "staging"; generations = root / "generations"; outside = root / "outside"
            staging.mkdir(); generations.mkdir(); outside.mkdir(); source = staging / "transaction"; source.mkdir()
            original = generations.with_name("generations-original")
            generations.rename(original); generations.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(PromotionError): _publish_directory_no_replace(source, generations / "000001-generation-x")
            self.assertEqual([], list(outside.iterdir())); self.assertTrue(source.exists())

    def test_lock_rejects_promotions_root_swap_and_inventory_rejects_links(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name); candidates = root / "candidates"; brain = root / "brain"; promotions = root / "promotions"
            candidates.mkdir(); brain.mkdir(); promotions.mkdir(); outside = root / "outside"; outside.mkdir()
            deps = _PromotionDependencies(candidates, brain, promotions)
            original = promotions.with_name("promotions-original")
            promotions.rename(original); promotions.symlink_to(outside, target_is_directory=True)
            with self.assertRaises((PromotionError, OSError)):
                with _promotion_lock(deps): pass
            (brain / "file").write_text("safe"); (brain / "link").symlink_to("file")
            with self.assertRaises(PromotionError): _inventory(brain)

    def test_global_lock_serializes_real_competing_threads(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name); candidates = root / "candidates"; brain = root / "brain"; promotions = root / "promotions"
            candidates.mkdir(); brain.mkdir(); promotions.mkdir()
            deps = _PromotionDependencies(candidates, brain, promotions)
            entered = threading.Event(); acquired = threading.Event()
            def competitor():
                entered.set()
                with _promotion_lock(deps): acquired.set()
            with _promotion_lock(deps):
                thread = threading.Thread(target=competitor)
                thread.start(); self.assertTrue(entered.wait(1))
                time.sleep(0.05)
                self.assertFalse(acquired.is_set())
            thread.join(1)
            self.assertFalse(thread.is_alive()); self.assertTrue(acquired.is_set())

    def test_review_append_waits_for_global_promotion_lock(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates") as name:
            root = Path(name); candidate_id, candidates, brain, _ = build_fixture(root)
            deps = _PromotionDependencies(candidates, brain, root / "promotions")
            entered = threading.Event(); completed = threading.Event(); errors = []
            def reviewer():
                entered.set()
                try:
                    _append_review_with_dependencies(
                        candidate_id,
                        "accept",
                        "fixture-reviewer",
                        candidate_root=candidates,
                        recorded_at="2026-08-11T12:00:00Z",
                        dependencies=_CandidateReviewDependencies(
                            lambda: _promotion_lock(deps)
                        ),
                    )
                except Exception as exc: errors.append(exc)
                finally: completed.set()
            with _promotion_lock(deps):
                thread = threading.Thread(target=reviewer); thread.start(); self.assertTrue(entered.wait(1))
                time.sleep(0.05); self.assertFalse(completed.is_set())
            thread.join(1)
            self.assertFalse(thread.is_alive()); self.assertEqual([], errors); self.assertTrue(completed.is_set())

    def test_competing_destination_publish_has_exactly_one_winner(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name); staging = root / "staging"; generations = root / "brain" / "generations"
            staging.mkdir(); generations.mkdir(parents=True)
            sources = [staging / "one", staging / "two"]
            for source in sources: source.mkdir()
            destination = generations / "000001-generation-fixture"; outcomes = []
            def publish(source):
                try: _publish_directory_no_replace(source, destination); outcomes.append("published")
                except PromotionError: outcomes.append("rejected")
            threads = [threading.Thread(target=publish, args=(source,)) for source in sources]
            for thread in threads: thread.start()
            for thread in threads: thread.join(1)
            self.assertEqual(1, outcomes.count("published")); self.assertEqual(1, outcomes.count("rejected"))
            self.assertTrue(destination.is_dir())


if __name__ == "__main__":
    unittest.main()
