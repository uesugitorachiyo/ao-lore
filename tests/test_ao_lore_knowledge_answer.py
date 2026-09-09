import json
import tempfile
import unittest
from pathlib import Path

from ao_lore.knowledge_contracts import citations_digest, claims_digest, semantic_key_v0_3
from tests.test_ao_lore_knowledge_snapshot import GenerationFixture, _entry, _entry_v0_4


def answerable(entry_id="entry-one", *, text="Canonical policy is enabled.", sensitivity="public", stale_after=None):
    claim = {"claim_id": f"claim-{entry_id}", "text": text, "source_block_ids": ["block-one"], "citation_id": f"citation-{entry_id}"}
    citation = {"citation_id": f"citation-{entry_id}", "render_text": "Reviewed canonical citation.", "source_block_ids": ["block-one"]}
    value = _entry(entry_id, "v0.3")
    value.update(
        claims=[claim], claims_digest=claims_digest([claim]),
        citations=[citation], citations_digest=citations_digest([citation]),
        claim_mappings=[{"claim_id": claim["claim_id"], "block_ids": ["block-one"]}],
        knowledge_policy={"sensitivity": sensitivity, "stale_after": stale_after},
    )
    value["semantic_key"] = semantic_key_v0_3(
        value["concepts"], value["claim_mappings"], value["links"],
        value["claims_digest"], value["citations_digest"], value["knowledge_policy"],
    )
    return value


class KnowledgeAnswerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = GenerationFixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def dependencies(self):
        from ao_lore.knowledge import _KnowledgeDependencies
        return _KnowledgeDependencies(self.fixture.brain, None)

    def answer(self, query):
        from ao_lore.knowledge import answer_knowledge
        return answer_knowledge(query, _dependencies=self.dependencies())

    def status(self):
        from ao_lore.knowledge import knowledge_status
        return knowledge_status(_dependencies=self.dependencies())

    def test_status_reports_generation_compatibility_and_claim_counts(self):
        self.fixture.add("generation-one", _entry("entry-legacy"))
        self.fixture.add("generation-two", answerable())
        report = self.status()
        self.assertEqual("completed", report["status"])
        self.assertEqual(2, report["generation_count"])
        self.assertEqual(2, report["effective_entry_count"])
        self.assertEqual(1, report["claim_count"])
        self.assertEqual("mixed_version_partial", report["answerability_status"])

    def test_answer_is_exact_evidence_bound_and_contains_no_query_or_replan(self):
        self.fixture.add("generation-one", answerable())
        report = self.answer("POLICY enabled")
        self.assertEqual("answer", report["status"])
        self.assertEqual("none", report["reason_code"])
        self.assertEqual(["claim-entry-one"], report["claim_ids"])
        self.assertEqual(["canonical:entry-one#citation-entry-one"], report["citations"])
        self.assertEqual(1, len(report["evidence_ids"]))
        self.assertIn("Canonical policy is enabled.", report["answer"])
        rendered = json.dumps(report, sort_keys=True)
        self.assertNotIn("POLICY enabled", rendered)
        self.assertNotIn("replan", rendered)
        self.assertLessEqual(len(report["answer"]), 32768)

    def test_refuses_empty_legacy_only_and_unmatched_without_claims(self):
        cases = (
            ("empty", None, "no_answerable_entries"),
            ("legacy", _entry("entry-legacy"), "legacy_metadata_only"),
            ("unmatched", answerable(), "coverage_insufficient"),
        )
        for label, entry, reason in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                fixture = GenerationFixture(root)
                if entry is not None:
                    fixture.add("generation-one", entry)
                from ao_lore.knowledge import _KnowledgeDependencies, answer_knowledge
                result = answer_knowledge("absent tokens", _dependencies=_KnowledgeDependencies(fixture.brain, None))
                self.assertEqual("refuse", result["status"])
                self.assertEqual(reason, result["reason_code"])
                self.assertEqual([], result["claim_ids"])
                self.assertEqual("", result["answer"])

    def test_contradiction_refuses_and_stale_evidence_is_bounded_partial(self):
        self.fixture.add("generation-one", answerable("entry-one", text="Policy is enabled."))
        self.fixture.add("generation-two", answerable("entry-two", text="Policy is not enabled."))
        refused = self.answer("policy enabled")
        self.assertEqual(("refuse", "hard_contradiction"), (refused["status"], refused["reason_code"]))
        self.assertEqual([], refused["claim_ids"])

        with tempfile.TemporaryDirectory() as root:
            fixture = GenerationFixture(root)
            fixture.add("generation-one", answerable(stale_after="2000-01-01T00:00:00Z"))
            from ao_lore.knowledge import _KnowledgeDependencies, answer_knowledge
            partial = answer_knowledge("policy enabled", _dependencies=_KnowledgeDependencies(fixture.brain, None))
            self.assertEqual(("partial", "freshness_gate_failed"), (partial["status"], partial["reason_code"]))
            self.assertEqual(["claim-entry-one"], partial["claim_ids"])

    def test_corrupt_canonical_state_investigates_without_unsupported_text(self):
        self.fixture.add("generation-one", answerable(text="PRIVATE UNSUPPORTED CLAIM"))
        manifest = self.fixture.generations / "000001-generation-one" / "manifest.json"
        value = json.loads(manifest.read_text())
        value["manifest_digest"] = "sha256:" + "0" * 64
        manifest.write_text(json.dumps(value))
        report = self.answer("PRIVATE UNSUPPORTED CLAIM")
        self.assertEqual(("investigate", "canonical_state_invalid"), (report["status"], report["reason_code"]))
        self.assertEqual([], report["claim_ids"])
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_v0_4_answer_uses_origin_qualified_v0_2_citations(self):
        self.fixture.add("generation-one", _entry_v0_4())
        report = self.answer("policy enabled")
        self.assertEqual("ao.lore.knowledge-answer-readback.v0.2", report["schema_version"])
        self.assertEqual([{
            "source_ref": "canonical:entry-four#citation-entry-four",
            "origin_identity": {
                "workspace_id": "workspace-one", "evidence_kind": "document_block",
                "evidence_id": "origin-one", "evidence_digest": "sha256:" + "a" * 64,
            },
        }], report["citations"])


if __name__ == "__main__":
    unittest.main()
