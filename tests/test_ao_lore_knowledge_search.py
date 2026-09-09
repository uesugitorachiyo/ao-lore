import copy
import json
import tempfile
import unittest
from pathlib import Path

from ao_lore.benchmark import canonical_digest
from ao_lore.knowledge_contracts import citations_digest, claims_digest, semantic_key_v0_3
from tests.test_ao_lore_knowledge_snapshot import DIGEST, GenerationFixture, _entry, _entry_v0_4


def _answerable(entry_id, claims, citations, *, concepts=None, links=None):
    value = _entry(entry_id, "v0.3")
    concepts = [] if concepts is None else concepts
    links = [] if links is None else links
    value.update(
        concepts=concepts,
        claim_mappings=[
            {"claim_id": claim["claim_id"], "block_ids": claim["source_block_ids"]}
            for claim in claims
        ],
        links=links,
        claims=claims,
        claims_digest=claims_digest(claims),
        citations=citations,
        citations_digest=citations_digest(citations),
    )
    value["semantic_key"] = semantic_key_v0_3(
        concepts,
        value["claim_mappings"],
        links,
        value["claims_digest"],
        value["citations_digest"],
        value["knowledge_policy"],
    )
    return value


def _claim(claim_id, text, citation_id=None):
    citation_id = citation_id or f"citation-{claim_id}"
    return {
        "claim_id": claim_id,
        "text": text,
        "source_block_ids": [f"block-{claim_id}"],
        "citation_id": citation_id,
    }


def _citation(claim_id, text, citation_id=None):
    citation_id = citation_id or f"citation-{claim_id}"
    return {
        "citation_id": citation_id,
        "render_text": text,
        "source_block_ids": [f"block-{claim_id}"],
    }


class KnowledgeSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = GenerationFixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def search(self, query, limit=20):
        from ao_lore.knowledge import _KnowledgeDependencies, search_knowledge

        return search_knowledge(
            query,
            limit=limit,
            _dependencies=_KnowledgeDependencies(self.fixture.brain, None),
        )

    def test_normalizes_nfkc_casefold_and_non_alphanumeric_runs(self):
        claims = [_claim("claim-one", "Café STRASSE policy 42")]
        citations = [_citation("claim-one", "Fixture citation")]
        self.fixture.add("generation-one", _answerable("entry-one", claims, citations))

        first = self.search("ＣＡＦＥ́---straße")
        second = self.search("café strasse")

        self.assertEqual(["claim-one"], [hit["claim_id"] for hit in first["hits"]])
        self.assertEqual(second, first)
        self.assertNotIn("query", first)
        self.assertNotIn("normalized_query", first)

    def test_rejects_hostile_query_and_limit_budgets_before_snapshot_traversal(self):
        from ao_lore.knowledge import KnowledgeReadError, _KnowledgeDependencies, search_knowledge

        missing = _KnowledgeDependencies(Path(self.temp.name) / "must-not-open", None)
        for query in ("x" * 4097, " ".join(["x"] * 257), "\ud800"):
            with self.subTest(query_length=len(query)), self.assertRaises(KnowledgeReadError):
                search_knowledge(query, _dependencies=missing)
        for limit in (0, -1, 201, True, 1.0, "20"):
            with self.subTest(limit=limit), self.assertRaises(KnowledgeReadError):
                search_knowledge("safe", limit=limit, _dependencies=missing)

    def test_default_limit_maximum_limit_and_zero_match_are_bounded(self):
        claims = [_claim(f"claim-{index:03d}", f"shared token {index}") for index in range(205)]
        citations = [_citation(f"claim-{index:03d}", f"citation {index}") for index in range(205)]
        self.fixture.add("generation-one", _answerable("entry-one", claims, citations))

        self.assertEqual(20, len(self.search("shared")["hits"]))
        self.assertEqual(200, len(self.search("shared", 200)["hits"]))
        self.assertEqual([], self.search("absent")["hits"])

    def test_orders_by_exact_substring_claim_tokens_citation_tokens_then_identity(self):
        entries = [
            _answerable("entry-z", [_claim("claim-z", "red blue")], [_citation("claim-z", "plain")]),
            _answerable("entry-y", [_claim("claim-y", "red blue extra")], [_citation("claim-y", "plain")]),
            _answerable("entry-x", [_claim("claim-x", "red other")], [_citation("claim-x", "blue")]),
            _answerable("entry-b", [_claim("claim-b", "red blue extra")], [_citation("claim-b", "plain")]),
            _answerable("entry-a", [_claim("claim-a", "red blue extra")], [_citation("claim-a", "plain")]),
        ]
        for index, entry in enumerate(entries, 1):
            self.fixture.add(f"generation-{index}", entry)

        hits = self.search("red blue", 20)["hits"]
        self.assertEqual(
            ["entry-z", "entry-a", "entry-b", "entry-y", "entry-x"],
            [hit["canonical_entry_id"] for hit in hits],
        )
        self.assertEqual(
            {
                "claim_exact_match": True,
                "claim_substring_match": True,
                "matched_claim_token_count": 2,
                "matched_citation_token_count": 0,
            },
            hits[0]["score_components"],
        )

    def test_uses_only_active_snapshot_and_keeps_legacy_metadata_non_authoritative(self):
        inactive = self.fixture.add(
            "generation-one",
            _answerable(
                "entry-inactive",
                [_claim("claim-inactive", "needle exact")],
                [_citation("claim-inactive", "needle")],
            ),
        )
        self.fixture.restore("generation-two", None)
        legacy = _entry("entry-legacy")
        legacy["concepts"] = [{
            "candidate_concept_id": "concept-needle",
            "title": "Needle metadata",
            "source_block_ids": [],
            "status": "proposed",
        }]
        self.fixture.add("generation-three", legacy)
        active = _answerable(
            "entry-active",
            [_claim("claim-active", "needle remains")],
            [_citation("claim-active", "active citation")],
        )
        self.fixture.add("generation-four", active)
        candidates = Path(self.temp.name) / "working" / "candidates" / "accepted"
        candidates.mkdir(parents=True)
        (candidates / "hostile.json").write_text('{"needle":"stronger candidate"}', encoding="utf-8")

        hits = self.search("needle")["hits"]

        self.assertEqual(["entry-active", "entry-legacy"], [hit["canonical_entry_id"] for hit in hits])
        metadata = hits[1]
        self.assertEqual("metadata_only", metadata["classification"])
        for forbidden in ("evidence_id", "claim_id", "claim_text", "citation", "source_ref", "score_components"):
            self.assertNotIn(forbidden, metadata)
        self.assertNotIn(inactive["generation_id"], json.dumps(hits, sort_keys=True))

    def test_restricted_claims_never_cross_the_public_search_boundary(self):
        restricted = _answerable(
            "entry-restricted",
            [_claim("claim-restricted", "restricted launch phrase")],
            [_citation("claim-restricted", "restricted fixture citation")],
        )
        restricted["knowledge_policy"] = {
            "sensitivity": "restricted",
            "stale_after": None,
        }
        restricted["semantic_key"] = semantic_key_v0_3(
            restricted["concepts"],
            restricted["claim_mappings"],
            restricted["links"],
            restricted["claims_digest"],
            restricted["citations_digest"],
            restricted["knowledge_policy"],
        )
        self.fixture.add("generation-one", restricted)

        result = self.search("restricted launch phrase")

        self.assertEqual([], result["hits"])
        self.assertNotIn("restricted launch phrase", json.dumps(result, sort_keys=True))

    def test_preserves_contradictions_and_derives_stable_evidence_and_query_digests(self):
        for index, text in enumerate(("policy is enabled", "policy is not enabled"), 1):
            claim_id = f"claim-{index}"
            self.fixture.add(
                f"generation-{index}",
                _answerable(
                    f"entry-{index}",
                    [_claim(claim_id, text)],
                    [_citation(claim_id, f"reviewed citation {index}")],
                ),
            )

        first = self.search("policy enabled")
        second = self.search("POLICY---ENABLED")

        self.assertEqual(2, len(first["hits"]))
        self.assertEqual(first, second)
        self.assertEqual(2, len({hit["evidence_id"] for hit in first["hits"]}))
        self.assertEqual(
            canonical_digest({"domain": "ao.lore.knowledge-query.v0.1", "normalized_query": "policy enabled"}),
            first["query_digest"],
        )

    def test_rejects_duplicate_claim_identity_across_active_entries(self):
        for index in (1, 2):
            self.fixture.add(
                f"generation-{index}",
                _answerable(
                    f"entry-{index}",
                    [_claim("claim-shared", f"shared statement {index}")],
                    [_citation("claim-shared", f"citation {index}")],
                ),
            )
        from ao_lore.knowledge import KnowledgeReadError

        with self.assertRaises(KnowledgeReadError):
            self.search("shared")

    def test_results_are_detached_and_runtime_contract_validated(self):
        claims = [_claim("claim-one", "detached result")]
        citations = [_citation("claim-one", "detached citation")]
        self.fixture.add("generation-one", _answerable("entry-one", claims, citations))
        result = self.search("detached")
        original = copy.deepcopy(result)
        result["hits"][0]["claim_text"] = "mutated"
        self.assertEqual(original, self.search("detached"))

    def test_v0_4_search_is_origin_qualified_and_v0_2_versioned(self):
        self.fixture.add("generation-one", _entry_v0_4())
        result = self.search("policy enabled")
        self.assertEqual("ao.lore.knowledge-search-readback.v0.2", result["schema_version"])
        self.assertEqual(1, result["answerable_v0_4_entry_count"])
        self.assertEqual({
            "workspace_id": "workspace-one", "evidence_kind": "document_block",
            "evidence_id": "origin-one", "evidence_digest": DIGEST,
        }, result["hits"][0]["origin_identity"])


if __name__ == "__main__":
    unittest.main()
