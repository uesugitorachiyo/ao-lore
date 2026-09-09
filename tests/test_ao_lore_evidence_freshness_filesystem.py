import os
import tempfile
import threading
import unittest
from pathlib import Path

from ao_lore.evidence_freshness import (
    FreshnessDependencies,
    FreshnessLimits,
    FreshnessStorageError,
    persist_freshness_transaction,
)
from tests.test_ao_lore_evidence_freshness import bind, graph, observation, policy, prior_record
from ao_lore.evidence_freshness import compare_freshness_observation, summarize_freshness


class EvidenceFreshnessFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "source"
        self.root.mkdir()
        self.record = prior_record(); self.graph = graph(); self.policy = policy([self.record], self.graph)
        self.observation = observation(self.policy, self.record)
        self.comparison = compare_freshness_observation(self.policy, self.record, self.graph, self.observation)
        self.summary = summarize_freshness(self.policy, self.graph, [self.observation], [self.comparison])

    def tearDown(self): self.temp.cleanup()

    def persist(self, deps=None):
        return persist_freshness_transaction(
            self.policy, self.graph, [self.observation], [self.comparison], self.summary,
            deps or FreshnessDependencies(self.root),
        )

    def layout(self):
        freshness = self.root / "freshness"; freshness.mkdir()
        for name in ("policies", "observations", "comparisons", "summaries", "recovery", "staging"):
            (freshness / name).mkdir()
        return freshness

    def test_publishes_exact_immutable_artifacts_and_exact_retry(self):
        first = self.persist(); second = self.persist()
        self.assertEqual(first, second); self.assertEqual("complete", first["classification"])
        freshness = self.root / "freshness"
        self.assertEqual({"policies", "observations", "comparisons", "summaries", "recovery", "staging"}, {p.name for p in freshness.iterdir()})
        self.assertEqual(1, len(list((freshness / "policies").iterdir())))
        self.assertEqual(1, len(list((freshness / "observations").iterdir())))
        self.assertEqual(1, len(list((freshness / "comparisons").iterdir())))
        self.assertEqual(1, len(list((freshness / "summaries").iterdir())))
        self.assertEqual([], list((freshness / "staging").iterdir()))
        recovery_names = {item.name for item in (freshness / "recovery").iterdir()}
        self.assertEqual(2, len(recovery_names))
        self.assertTrue(any(name.endswith(".bundle.json") for name in recovery_names))
        self.assertTrue(any(name.endswith(".completed.json") for name in recovery_names))
        self.assertFalse(any(name.endswith(".intent.json") for name in recovery_names))

    def test_completed_history_does_not_exhaust_live_attempt_budget(self):
        for index in range(24):
            policy_value = dict(self.policy); policy_value["policy_id"] = f"policy-{index:02d}"
            policy_value = bind(policy_value, "policy_digest")
            observed = observation(policy_value, self.record, observed_at=f"2026-08-{index + 1:02d}T18:00:00Z")
            observed["observation_id"] = f"observation-{index:02d}"
            observed = bind(observed, "observation_digest")
            compared = compare_freshness_observation(policy_value, self.record, self.graph, observed)
            summary_value = summarize_freshness(policy_value, self.graph, [observed], [compared])
            persist_freshness_transaction(
                policy_value, self.graph, [observed], [compared], summary_value,
                FreshnessDependencies(self.root),
            )
        freshness = self.root / "freshness"
        self.assertEqual([], list((freshness / "staging").iterdir()))
        recovery_names = [item.name for item in (freshness / "recovery").iterdir()]
        self.assertEqual(48, len(recovery_names))
        self.assertFalse(any(name.endswith(".intent.json") for name in recovery_names))

    def test_public_and_completed_history_limits_share_an_exact_boundary(self):
        defaults = FreshnessLimits()
        self.assertGreaterEqual(
            defaults.max_public_artifacts_per_kind, defaults.max_completed_attempts,
        )
        limits = FreshnessLimits(
            max_public_artifacts_per_kind=4, max_completed_attempts=4,
        )
        for index in range(5):
            policy_value = dict(self.policy); policy_value["policy_id"] = f"boundary-{index}"
            policy_value = bind(policy_value, "policy_digest")
            observed = observation(policy_value, self.record, observed_at=f"2026-08-{index + 1:02d}T19:00:00Z")
            observed["observation_id"] = f"boundary-observation-{index}"
            observed = bind(observed, "observation_digest")
            compared = compare_freshness_observation(policy_value, self.record, self.graph, observed)
            summary_value = summarize_freshness(policy_value, self.graph, [observed], [compared])
            arguments = (
                policy_value, self.graph, [observed], [compared], summary_value,
                FreshnessDependencies(self.root, limits=limits),
            )
            if index < 4:
                persist_freshness_transaction(*arguments)
            else:
                before = {str(path.relative_to(self.root)): path.read_bytes()
                          for path in self.root.rglob("*") if path.is_file()}
                with self.assertRaisesRegex(FreshnessStorageError, "history budget"):
                    persist_freshness_transaction(*arguments)
                after = {str(path.relative_to(self.root)): path.read_bytes()
                         for path in self.root.rglob("*") if path.is_file()}
                self.assertEqual(before, after)
        freshness = self.root / "freshness"
        self.assertEqual([], list((freshness / "staging").iterdir()))
        self.assertEqual(8, len(list((freshness / "recovery").iterdir())))

    def test_unknown_freshness_root_entry_rejects_before_publication(self):
        freshness = self.root / "freshness"; freshness.mkdir()
        foreign = freshness / "foreign"; foreign.write_text("preserve")
        with self.assertRaisesRegex(FreshnessStorageError, "unknown entry"):
            self.persist()
        self.assertEqual("preserve", foreign.read_text())
        self.assertEqual({"foreign"}, {item.name for item in freshness.iterdir()})

    def test_foreign_recovery_entry_blocks_persist_without_publishing(self):
        freshness = self.layout(); foreign = freshness / "recovery" / "foreign.json"
        foreign.write_text("{}\n")
        before = {str(path.relative_to(freshness)): path.read_bytes()
                  for path in freshness.rglob("*") if path.is_file()}
        with self.assertRaises(FreshnessStorageError): self.persist()
        after = {str(path.relative_to(freshness)): path.read_bytes()
                 for path in freshness.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual([], list((freshness / "policies").iterdir()))

    def test_entry_count_and_total_tree_byte_budgets_reject_before_publication(self):
        for label, limits, populate in (
            ("file count", FreshnessLimits(max_live_staging_entries=6),
             lambda root: [(root / "staging" / f"foreign-{index}").write_bytes(b"") for index in range(7)]),
            ("total byte", FreshnessLimits(max_total_bytes=4),
             lambda root: (root / "staging" / "foreign").write_bytes(b"12345")),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "source"; self.root.mkdir()
                freshness = self.layout(); populate(freshness)
                before = {str(path.relative_to(freshness)): path.read_bytes()
                          for path in freshness.rglob("*") if path.is_file()}
                with self.assertRaisesRegex(FreshnessStorageError, label):
                    self.persist(FreshnessDependencies(self.root, limits=limits))
                after = {str(path.relative_to(freshness)): path.read_bytes()
                         for path in freshness.rglob("*") if path.is_file()}
                self.assertEqual(before, after)

    def test_destination_conflict_and_unsafe_ancestors_fail_closed(self):
        self.persist()
        target = next((self.root / "freshness" / "summaries").iterdir())
        target.unlink(); target.write_text("{}\n")
        with self.assertRaises(FreshnessStorageError): self.persist()
        other = Path(self.temp.name) / "other"; other.mkdir()
        symlink_root = Path(self.temp.name) / "linked"; symlink_root.symlink_to(other, target_is_directory=True)
        with self.assertRaises(FreshnessStorageError):
            persist_freshness_transaction(self.policy, self.graph, [self.observation], [self.comparison], self.summary, FreshnessDependencies(symlink_root))
        real_parent = Path(self.temp.name) / "real-parent"; real_parent.mkdir()
        (real_parent / "source").mkdir()
        linked_parent = Path(self.temp.name) / "linked-parent"; linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(FreshnessStorageError):
            persist_freshness_transaction(self.policy, self.graph, [self.observation], [self.comparison], self.summary, FreshnessDependencies(linked_parent / "source"))

    def test_hardlink_fifo_duplicate_json_casefold_and_foreign_entries_reject(self):
        self.persist(); freshness = self.root / "freshness"
        cases = []
        policy_file = next((freshness / "policies").iterdir())
        hardlink = freshness / "policies" / "foreign.json"; os.link(policy_file, hardlink); cases.append(hardlink)
        for item in cases:
            with self.assertRaises(FreshnessStorageError): self.persist()
            item.unlink()
        fifo = freshness / "staging" / "foreign"; os.mkfifo(fifo)
        with self.assertRaises(FreshnessStorageError): self.persist()
        fifo.unlink()
        duplicate = freshness / "staging" / "foreign.json"; duplicate.write_text('{"x":1,"x":2}')
        with self.assertRaises(FreshnessStorageError): self.persist()
        duplicate.unlink()
        existing = next((freshness / "observations").iterdir())
        collision = existing.with_name(existing.name.upper()); collision.write_text("{}")
        with self.assertRaises(FreshnessStorageError): self.persist()

    def test_root_and_owned_directory_replacement_are_detected(self):
        moved = Path(self.temp.name) / "moved-source"
        def replace_root(name):
            if name == "after_intent":
                self.root.rename(moved); self.root.mkdir()
        with self.assertRaises(FreshnessStorageError):
            self.persist(FreshnessDependencies(self.root, replace_root))
        self.root.rmdir(); moved.rename(self.root)
        moved_freshness = self.root / "moved-freshness"
        def replace_freshness(name):
            if name == "after_staging_cleanup":
                (self.root / "freshness").rename(moved_freshness)
                (self.root / "freshness").mkdir()
        with self.assertRaises(FreshnessStorageError):
            self.persist(FreshnessDependencies(self.root, replace_freshness))

    def test_each_owned_child_directory_replacement_fails_closed(self):
        cases = (
            ("policies", "after_staging"),
            ("observations", "after_observation"),
            ("comparisons", "after_comparison"),
            ("summaries", "after_summary"),
            ("recovery", "after_terminal"),
            ("staging", "after_intent"),
        )
        for child_name, boundary in cases:
            with self.subTest(child=child_name, boundary=boundary), tempfile.TemporaryDirectory() as directory:
                self.root = Path(directory) / "source"
                self.root.mkdir()
                moved = self.root / f"moved-{child_name}"

                def replace_child(name):
                    if name == boundary:
                        target = self.root / "freshness" / child_name
                        target.rename(moved)
                        target.mkdir()

                with self.assertRaises(FreshnessStorageError):
                    self.persist(FreshnessDependencies(self.root, replace_child))
                self.assertEqual([], list((self.root / "freshness" / child_name).iterdir()))
                self.assertTrue(moved.exists())

    def test_concurrent_exact_writers_converge(self):
        results=[]; errors=[]
        def run():
            try: results.append(self.persist())
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=run) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual([], errors); self.assertEqual(4, len(results)); self.assertTrue(all(x == results[0] for x in results))
