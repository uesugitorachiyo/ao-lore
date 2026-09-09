import copy
import unittest
from datetime import datetime, timezone

from ao_lore.benchmark import canonical_digest
from ao_lore.knowledge import KnowledgeReadError, project_knowledge_evidence
from tests.test_ao_lore_knowledge_snapshot import _entry_v0_4, _origin


DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


def entry(*, stale_after=None, sensitivity="public"):
    return {
        "schema_version": "ao.lore.okf-canonical-entry.v0.3",
        "canonical_entry_id": "entry-one",
        "candidate_id": "candidate-one",
        "source_digest": OTHER_DIGEST,
        "provenance_digest": DIGEST,
        "parser": {"parser_id": "fixture-parser", "parser_version": "1.0"},
        "knowledge_policy": {"sensitivity": sensitivity, "stale_after": stale_after},
        "claims": [{"claim_id": "claim-one", "text": "Canonical policy is enabled.", "citation_id": "citation-one"}],
        "citations": [{"citation_id": "citation-one", "render_text": "Reviewed fixture citation."}],
    }


def snapshot(value=None):
    value = entry() if value is None else value
    return {
        "snapshot_schema_version": "ao.lore.knowledge-snapshot.v0.1",
        "latest_generation_id": "generation-one",
        "latest_generation_manifest_digest": DIGEST,
        "snapshot_digest": OTHER_DIGEST,
        "effective_entries": [{
            "canonical_entry_id": "entry-one",
            "canonical_entry_digest": canonical_digest(value),
            "entry_schema_version": "ao.lore.okf-canonical-entry.v0.3",
            "answerability": "answerable",
            "claim_count": 1,
        }],
        "legacy_v0_2_entry_count": 0,
        "answerable_v0_3_entry_count": 1,
        "_verified_active_entries": {"entry-one": value},
    }


def search(*, evidence_id="evidence-0123456789abcdef0123456789abcdef"):
    return {
        "schema_version": "ao.lore.knowledge-search-readback.v0.1",
        "status": "completed",
        "snapshot_digest": OTHER_DIGEST,
        "query_digest": DIGEST,
        "hits": [{
            "classification": "answerable",
            "evidence_id": evidence_id,
            "canonical_entry_id": "entry-one",
            "claim_id": "claim-one",
            "citation_id": "citation-one",
            "claim_text": "Canonical policy is enabled.",
            "citation": "Reviewed fixture citation.",
            "source_ref": "canonical:entry-one#citation-one",
            "score_components": {
                "claim_exact_match": False,
                "claim_substring_match": True,
                "matched_claim_token_count": 2,
                "matched_citation_token_count": 0,
            },
        }],
        "legacy_v0_2_entry_count": 0,
        "answerable_v0_3_entry_count": 1,
    }


class KnowledgeNavigationProjectionTests(unittest.TestCase):
    def test_v0_4_projection_carries_exact_promoted_origin_without_parser(self):
        value = _entry_v0_4()
        snap = snapshot(value)
        snap["effective_entries"][0]["canonical_entry_id"] = "entry-four"
        snap["_verified_active_entries"] = {"entry-four": value}
        snap.update(snapshot_schema_version="ao.lore.knowledge-snapshot.v0.2", answerable_v0_3_entry_count=0, answerable_v0_4_entry_count=1)
        snap["effective_entries"][0].update(entry_schema_version=value["schema_version"], evidence_origin_identities=[{
            "workspace_id": "workspace-one", "evidence_kind": "document_block",
            "evidence_id": "origin-one", "evidence_digest": DIGEST,
        }])
        hit = search()
        hit.update(schema_version="ao.lore.knowledge-search-readback.v0.2", answerable_v0_3_entry_count=0, answerable_v0_4_entry_count=1)
        hit["hits"][0].update(
            canonical_entry_id="entry-four", claim_id="claim-entry-four",
            citation_id="citation-entry-four", claim_text="Canonical policy is enabled.",
            citation="Reviewed promoted origin.", source_ref="canonical:entry-four#citation-entry-four",
            origin_identity={"workspace_id": "workspace-one", "evidence_kind": "document_block", "evidence_id": "origin-one", "evidence_digest": DIGEST},
        )
        from ao_lore.knowledge import _evidence_id
        hit["hits"][0]["evidence_id"] = _evidence_id(snap, "entry-four", canonical_digest(value), "claim-entry-four", "citation-entry-four")
        projection = project_knowledge_evidence(snap, hit, now=NOW)["evidence_projection"][0]
        self.assertEqual("ao.lore.knowledge-evidence-projection.v0.2", projection["schema_version"])
        self.assertEqual(_origin(), projection["origin"])
        self.assertNotIn("parser_id", projection["provenance"])

    def test_projects_exact_mandatory_requirement_branch_and_evidence_bindings(self):
        value = entry()
        snap = snapshot(value)
        hit = search()
        # Bind the fixture hit to the exact canonical evidence identity.
        from ao_lore.knowledge import _evidence_id
        hit["hits"][0]["evidence_id"] = _evidence_id(
            snap, "entry-one", canonical_digest(value), "claim-one", "citation-one"
        )

        result = project_knowledge_evidence(snap, hit, now=NOW)

        self.assertEqual(DIGEST, result["query_digest"])
        requirement = result["requirement_plan"]["requirements"]
        self.assertEqual(1, len(requirement))
        self.assertEqual("requirement-query", requirement[0]["id"])
        self.assertTrue(requirement[0]["mandatory"])
        self.assertEqual(
            {"max_depth": 200, "max_nodes": 200, "max_tokens": 100000, "max_seconds": 60, "max_replans": 8},
            result["requirement_plan"]["budgets"],
        )
        projection = result["evidence_projection"][0]
        self.assertEqual(["requirement-query"], projection["requirement_ids"])
        self.assertEqual(hit["hits"][0]["evidence_id"], projection["evidence_id"])
        self.assertEqual([{"claim_id": "claim-one", "text": "Canonical policy is enabled."}], projection["supported_claims"])
        self.assertEqual("canonical:entry-one#citation-one", projection["source_ref"])
        self.assertEqual(OTHER_DIGEST, projection["source_digest"])
        self.assertEqual("generation-one", projection["provenance"]["generation_id"])
        self.assertEqual(canonical_digest(value), projection["provenance"]["canonical_entry_digest"])
        self.assertEqual("candidate-one", projection["provenance"]["candidate_id"])
        self.assertTrue(projection["verified"])
        self.assertEqual("answer", result["coverage_report"]["decision"])
        self.assertEqual("complete", result["downstream_state"])
        self.assertNotIn("query", result)
        self.assertNotIn("branches", result)

    def test_rejects_missing_or_drifted_exact_bindings_and_noncanonical_hits(self):
        base = entry()
        cases = {}
        no_context = snapshot(base); no_context.pop("_verified_active_entries"); cases["provenance"] = (no_context, search())
        missing_citation = entry(); missing_citation["citations"] = []; cases["citation"] = (snapshot(missing_citation), search())
        stale_hit = search(evidence_id="evidence-" + "0" * 32); cases["evidence"] = (snapshot(base), stale_hit)
        metadata = search(); metadata["hits"][0] = {"classification": "metadata_only", "canonical_entry_id": "entry-one", "concept_id": "concept-one", "concept_title": "Policy"}; cases["metadata"] = (snapshot(base), metadata)
        for label, (snap, readback) in cases.items():
            with self.subTest(label=label), self.assertRaises(KnowledgeReadError):
                project_knowledge_evidence(snap, readback, now=NOW)

    def test_stale_and_untrusted_supported_evidence_preserves_failed_gates(self):
        variants = {
            "stale": entry(stale_after="2026-08-11T00:00:00Z"),
            "untrusted": entry(sensitivity="restricted"),
        }
        for label, value in variants.items():
            snap = snapshot(value)
            readback = search()
            from ao_lore.knowledge import _evidence_id
            readback["hits"][0]["evidence_id"] = _evidence_id(snap, "entry-one", canonical_digest(value), "claim-one", "citation-one")
            result = project_knowledge_evidence(snap, readback, now=NOW)
            with self.subTest(label=label):
                self.assertEqual(1, len(result["evidence_projection"]))
                self.assertEqual(label != "stale", result["evidence_projection"][0]["freshness_met"])
                self.assertEqual(label != "untrusted", result["evidence_projection"][0]["trust_met"])
                self.assertEqual(label != "stale", result["coverage_report"]["gates"]["freshness_met"])
                self.assertEqual(label != "untrusted", result["coverage_report"]["gates"]["trust_met"])
                self.assertIn(result["coverage_report"]["decision"], {"partial", "refuse", "replan"})
                self.assertEqual("bounded_incomplete", result["downstream_state"])

    def test_contradictions_are_preserved_as_bounded_downstream_state(self):
        value = entry()
        second = copy.deepcopy(value)
        second["canonical_entry_id"] = "entry-two"
        second["candidate_id"] = "candidate-two"
        second["claims"][0] = {"claim_id": "claim-two", "text": "Canonical policy is not enabled.", "citation_id": "citation-two"}
        second["citations"][0] = {"citation_id": "citation-two", "render_text": "Contradictory reviewed citation."}
        snap = snapshot(value)
        snap["effective_entries"].append({"canonical_entry_id": "entry-two", "canonical_entry_digest": canonical_digest(second), "entry_schema_version": second["schema_version"], "answerability": "answerable", "claim_count": 1})
        snap["_verified_active_entries"]["entry-two"] = second
        readback = search()
        from ao_lore.knowledge import _evidence_id
        readback["hits"][0]["evidence_id"] = _evidence_id(snap, "entry-one", canonical_digest(value), "claim-one", "citation-one")
        conflicting = copy.deepcopy(readback["hits"][0])
        conflicting.update(canonical_entry_id="entry-two", claim_id="claim-two", citation_id="citation-two", claim_text=second["claims"][0]["text"], citation=second["citations"][0]["render_text"], source_ref="canonical:entry-two#citation-two")
        conflicting["evidence_id"] = _evidence_id(snap, "entry-two", canonical_digest(second), "claim-two", "citation-two")
        readback["hits"].append(conflicting)
        result = project_knowledge_evidence(snap, readback, now=NOW)
        self.assertEqual(2, len(result["evidence_projection"]))
        self.assertEqual("refuse", result["coverage_report"]["decision"])
        self.assertEqual("bounded_incomplete", result["downstream_state"])

    def test_empty_hits_and_fixed_budget_exhaustion_are_bounded_not_success(self):
        no_hits = search(); no_hits["hits"] = []
        result = project_knowledge_evidence(snapshot(), no_hits, now=NOW)
        self.assertEqual([], result["evidence_projection"])
        self.assertEqual("bounded_incomplete", result["downstream_state"])
        self.assertFalse(result["coverage_report"]["gates"]["mandatory_requirements_met"])

    def test_only_visited_supported_claims_enter_the_evidence_ledger(self):
        first = entry()
        second = copy.deepcopy(first)
        second["canonical_entry_id"] = "entry-two"
        second["candidate_id"] = "candidate-two"
        second["claims"][0] = {"claim_id": "claim-two", "text": "Canonical policy remains enabled.", "citation_id": "citation-two"}
        second["citations"][0] = {"citation_id": "citation-two", "render_text": "Second reviewed citation."}
        snap = snapshot(first)
        snap["effective_entries"].append({"canonical_entry_id": "entry-two", "canonical_entry_digest": canonical_digest(second), "entry_schema_version": second["schema_version"], "answerability": "answerable", "claim_count": 1})
        snap["_verified_active_entries"]["entry-two"] = second
        readback = search()
        from ao_lore.knowledge import _evidence_id
        readback["hits"][0]["evidence_id"] = _evidence_id(snap, "entry-one", canonical_digest(first), "claim-one", "citation-one")
        unvisited = copy.deepcopy(readback["hits"][0])
        unvisited.update(canonical_entry_id="entry-two", claim_id="claim-two", citation_id="citation-two", claim_text=second["claims"][0]["text"], citation=second["citations"][0]["render_text"], source_ref="canonical:entry-two#citation-two")
        unvisited["evidence_id"] = _evidence_id(snap, "entry-two", canonical_digest(second), "claim-two", "citation-two")
        readback["hits"].append(unvisited)

        result = project_knowledge_evidence(snap, readback, now=NOW)

        self.assertEqual([readback["hits"][0]["evidence_id"]], [item["evidence_id"] for item in result["evidence_projection"]])


if __name__ == "__main__":
    unittest.main()
