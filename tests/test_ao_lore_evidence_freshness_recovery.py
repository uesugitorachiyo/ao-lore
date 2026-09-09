import tempfile
import unittest
from pathlib import Path

from ao_lore.evidence_freshness import (
    FreshnessDependencies,
    compare_freshness_observation,
    persist_freshness_transaction,
    recover_freshness_transactions,
    summarize_freshness,
)
from tests.test_ao_lore_evidence_freshness import graph, observation, policy, prior_record


class Crash(RuntimeError): pass


class EvidenceFreshnessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)/"source"; self.root.mkdir()
        record=prior_record(); self.graph=graph(); self.policy=policy([record], self.graph)
        observed=observation(self.policy, record); compared=compare_freshness_observation(self.policy, record, self.graph, observed)
        self.observations=[observed]; self.comparisons=[compared]
        self.summary=summarize_freshness(self.policy, self.graph, self.observations, self.comparisons)
    def tearDown(self): self.temp.cleanup()
    def persist(self, failpoint=lambda _: None):
        return persist_freshness_transaction(self.policy, self.graph, self.observations, self.comparisons, self.summary, FreshnessDependencies(self.root, failpoint))

    def test_unknown_root_and_foreign_recovery_return_investigate_and_preserve(self):
        freshness=self.root/"freshness"; freshness.mkdir()
        foreign=freshness/"foreign"; foreign.write_text("preserve")
        result=recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual("investigate", result[0]["classification"])
        self.assertEqual("preserve", foreign.read_text())
        foreign.unlink()
        for name in ("policies", "observations", "comparisons", "summaries", "recovery", "staging"):
            (freshness/name).mkdir(exist_ok=True)
        recovery_foreign=freshness/"recovery"/"foreign.json"
        recovery_foreign.write_text("{}\n")
        result=recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual("investigate", result[0]["classification"])
        self.assertEqual("{}\n", recovery_foreign.read_text())

    def test_each_crash_boundary_recovers_or_investigates_without_deletion(self):
        boundaries=("after_intent", "after_staging", "after_observation", "after_comparison", "after_summary", "after_terminal")
        for boundary in boundaries:
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                self.root=Path(directory)/"source"; self.root.mkdir()
                def fail(name):
                    if name == boundary: raise Crash(name)
                with self.assertRaises(Crash): self.persist(fail)
                before={str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
                result=recover_freshness_transactions(FreshnessDependencies(self.root))
                self.assertEqual(1, len(result)); self.assertIn(result[0]["classification"], {"complete", "investigate"})
                after={str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
                for name, body in before.items():
                    if "/staging/" in f"/{name}" or name.endswith(".intent.json"):
                        continue
                    self.assertEqual(body, after[name])

    def test_terminal_recovery_needs_no_staging_or_intent(self):
        terminal = self.persist(); freshness = self.root / "freshness"
        self.assertEqual([], list((freshness / "staging").iterdir()))
        self.assertFalse(any(path.name.endswith(".intent.json") for path in (freshness / "recovery").iterdir()))
        recovered = recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual([terminal], recovered)
        self.assertEqual([], list((freshness / "staging").iterdir()))

    def test_cleanup_crash_windows_are_idempotent(self):
        for boundary in ("after_staging_cleanup", "after_intent_cleanup"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                self.root=Path(directory)/"source"; self.root.mkdir()
                def fail(name):
                    if name == boundary: raise Crash(name)
                with self.assertRaises(Crash): self.persist(fail)
                result=recover_freshness_transactions(FreshnessDependencies(self.root))
                self.assertEqual("complete", result[0]["classification"])
                freshness=self.root/"freshness"
                self.assertEqual([], list((freshness/"staging").iterdir()))
                self.assertFalse(any(path.name.endswith(".intent.json") for path in (freshness/"recovery").iterdir()))

    def test_terminal_cleanup_preserves_foreign_staging_and_investigates(self):
        def fail(name):
            if name == "after_terminal": raise Crash(name)
        with self.assertRaises(Crash): self.persist(fail)
        foreign=self.root/"freshness"/"staging"/"foreign.json"
        foreign.write_text("preserve")
        before={path.name: path.read_bytes() for path in (self.root/"freshness"/"staging").iterdir()}
        result=recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual("investigate", result[0]["classification"])
        after={path.name: path.read_bytes() for path in (self.root/"freshness"/"staging").iterdir()}
        self.assertEqual(before, after)

    def test_foreign_staging_and_duplicate_or_oversized_json_are_bounded_investigate(self):
        freshness=self.root/"freshness"; (freshness/"staging").mkdir(parents=True)
        (freshness/"staging"/"foreign.json").write_text('{"x":1,"x":2}')
        result=recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual("investigate", result[0]["classification"])
        self.assertTrue((freshness/"staging"/"foreign.json").exists())
        (freshness/"staging"/"foreign.json").unlink()
        (freshness/"staging"/"oversized.json").write_bytes(b"x" * (1024 * 1024 + 1))
        result=recover_freshness_transactions(FreshnessDependencies(self.root))
        self.assertEqual("investigate", result[0]["classification"])
        self.assertEqual(1024 * 1024 + 1, (freshness/"staging"/"oversized.json").stat().st_size)
