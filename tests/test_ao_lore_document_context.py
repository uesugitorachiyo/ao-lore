"""Behavioral regressions for bounded context around lexical evidence hits."""

import unittest
from copy import deepcopy

from ao_lore.benchmark import canonical_digest
from ao_lore.document_evidence_contracts import validate_workspace_document_query_readback
from ao_lore.document_evidence_query import DocumentEvidenceQueryError, query_workspace_documents
from tests.test_ao_lore_document_evidence_query import generation


def fixture(*, sensitivity="internal", freshness="current"):
    value = generation(sensitivity=sensitivity, freshness=freshness)
    texts = [
        ("heading", "Incident"),
        ("paragraph", "Urgent attendance acknowledged for equipment."),
        ("paragraph", "A named contact verified physical obstruction. Add two hours."),
        ("heading", "Unrelated procedure"),
        ("paragraph", "Archive maintenance records annually."),
    ]
    blocks = []
    start = 0
    for index, (kind, text) in enumerate(texts):
        blocks.append({"id": f"b{index}", "type": kind, "text": text,
                       "source_span": {"start": start, "end": start + len(text)}})
        start += len(text) + 2
    document = value["documents"][0]
    document["document_ir"]["blocks"] = blocks
    return reseal(value)


def reseal(value):
    for document in value["documents"]:
        document["document_ir_digest"] = canonical_digest(document["document_ir"])
    value["generation_digest"] = canonical_digest({k: v for k, v in value.items() if k != "generation_digest"})
    return value


class DocumentContextTests(unittest.TestCase):
    def query(self, value=None, **options):
        return query_workspace_documents(value or fixture(), "workspace-a", "urgent attendance", **options)

    def test_expansion_recovers_adjacent_condition_with_exact_provenance(self):
        value = fixture()
        before = deepcopy(value)
        baseline = self.query(value)
        self.assertEqual(["b1"], [e["block_id"] for e in baseline["evidence"]])
        expanded = self.query(value, adjacent_blocks=1)
        self.assertEqual(["b1", "b2"], [e["block_id"] for e in expanded["evidence"]])
        condition = expanded["evidence"][1]
        self.assertEqual(value["documents"][0]["document_ir"]["blocks"][2]["text"], condition["render_text"])
        self.assertEqual(value["documents"][0]["document_ir"]["blocks"][2]["source_span"], condition["source_span"])
        self.assertEqual(expanded, validate_workspace_document_query_readback(expanded, generation=value, workspace_id="workspace-a"))
        self.assertEqual(before, value)
        self.assertEqual(baseline["evidence"][0], expanded["evidence"][0])
        self.assertNotEqual(baseline["query_id"], expanded["query_id"])

    def test_expansion_stops_at_heading_and_parent_boundaries(self):
        value = fixture()
        self.assertEqual(["b1", "b2"], [e["block_id"] for e in self.query(value, adjacent_blocks=2)["evidence"]])
        blocks = value["documents"][0]["document_ir"]["blocks"]
        blocks[0]["children"] = blocks[1:3]
        del blocks[1:3]
        reseal(value)
        self.assertEqual(["b1", "b2"], [e["block_id"] for e in self.query(value, adjacent_blocks=2)["evidence"]])

    def test_overlapping_windows_deduplicate_without_losing_conflicting_text(self):
        value = fixture()
        blocks = value["documents"][0]["document_ir"]["blocks"]
        blocks[2]["text"] = "Urgent attendance requires four hours, contrary to the earlier two-hour rule."
        blocks[2]["source_span"]["end"] = blocks[2]["source_span"]["start"] + len(blocks[2]["text"])
        reseal(value)
        result = self.query(value, adjacent_blocks=2)
        self.assertEqual({"b1", "b2"}, {e["block_id"] for e in result["evidence"]})
        self.assertEqual(2, len(result["evidence"]))
        self.assertIn("contrary", result["evidence"][1]["render_text"])

    def test_count_and_character_limits_never_cut_a_block(self):
        value = fixture()
        seed_length = len(value["documents"][0]["document_ir"]["blocks"][1]["text"])
        for options in ({"context_limit": 1}, {"max_context_chars": seed_length}):
            result = self.query(value, adjacent_blocks=1, **options)
            self.assertEqual(["b1"], [e["block_id"] for e in result["evidence"]])
            self.assertEqual("partial", result["outcome"])
            self.assertTrue(result["qualifications"])
            validate_workspace_document_query_readback(result, generation=value, workspace_id="workspace-a")
        result = self.query(value, adjacent_blocks=1, max_context_chars=1)
        self.assertEqual([], result["evidence"])
        self.assertEqual("refuse", result["outcome"])

    def test_restricted_and_stale_documents_keep_existing_gates(self):
        restricted = self.query(fixture(sensitivity="restricted"), adjacent_blocks=1)
        self.assertEqual("restricted_evidence", restricted["reason_code"])
        self.assertEqual([], restricted["evidence"])
        stale = self.query(fixture(freshness="stale"), adjacent_blocks=1)
        self.assertEqual("investigate", stale["outcome"])
        self.assertEqual(2, len(stale["evidence"]))

    def test_no_hit_does_not_expand_and_results_are_deterministic(self):
        value = fixture()
        result = query_workspace_documents(value, "workspace-a", "unmatched", adjacent_blocks=2)
        self.assertEqual([], result["evidence"])
        self.assertEqual(self.query(value, adjacent_blocks=1), self.query(value, adjacent_blocks=1))
        self.assertEqual(self.query(value), self.query(value, adjacent_blocks=0))

    def test_invalid_expansion_controls_fail_closed(self):
        for options in ({"adjacent_blocks": True}, {"adjacent_blocks": -1}, {"adjacent_blocks": 3},
                        {"context_limit": True}, {"context_limit": 0}, {"context_limit": 129},
                        {"max_context_chars": True}, {"max_context_chars": 0}, {"max_context_chars": 1048577}):
            with self.subTest(options=options), self.assertRaises(DocumentEvidenceQueryError):
                self.query(**options)

    def test_expansion_preserves_ranked_seeds_before_spending_remaining_slots(self):
        value = fixture()
        blocks = value["documents"][0]["document_ir"]["blocks"]
        blocks[4]["text"] = "Urgent attendance next procedure"
        blocks[4]["source_span"]["end"] = blocks[4]["source_span"]["start"] + len(blocks[4]["text"])
        reseal(value)
        baseline = self.query(value, limit=2)
        expanded = self.query(value, limit=2, adjacent_blocks=1, context_limit=3)
        self.assertEqual(baseline["evidence"], expanded["evidence"][:2])
        self.assertEqual("b2", expanded["evidence"][2]["block_id"])

    def test_budget_cannot_hide_restricted_or_stale_ranked_evidence(self):
        restricted = self.query(fixture(sensitivity="restricted"), adjacent_blocks=1, max_context_chars=1)
        self.assertEqual("restricted_evidence", restricted["reason_code"])
        stale = self.query(fixture(freshness="stale"), adjacent_blocks=1, max_context_chars=1)
        self.assertEqual("investigate", stale["outcome"])

    def test_neighbor_is_not_returned_without_its_oversized_seed(self):
        value = fixture()
        block = value["documents"][0]["document_ir"]["blocks"][2]
        block["text"] = "Exception applies."
        block["source_span"]["end"] = block["source_span"]["start"] + len(block["text"])
        reseal(value)
        result = self.query(value, adjacent_blocks=1, max_context_chars=20)
        self.assertEqual([], result["evidence"])

    def test_windows_do_not_cross_documents_with_reused_block_ids(self):
        value = fixture()
        other = deepcopy(value["documents"][0])
        other["document_id"] = other["document_ir"]["document_id"] = "unrelated-record"
        other["source_id"] = "unrelated-source"
        other["document_ir"]["blocks"] = [{"id": "b2", "type": "paragraph", "text": "Unrelated confidential appendix.",
                                            "source_span": {"start": 0, "end": 32}}]
        other["sensitivity"] = "restricted"
        value["documents"].append(other)
        reseal(value)
        result = self.query(value, adjacent_blocks=2)
        self.assertEqual({"source-policy-record"}, {e["source_id"] for e in result["evidence"]})
        self.assertEqual(2, len(result["evidence"]))
