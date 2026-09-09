import unittest
from copy import deepcopy
from unittest.mock import patch

from ao_lore.benchmark import canonical_digest
from ao_lore.document_evidence_query import DocumentEvidenceQueryError, query_workspace_documents
from ao_lore.document_evidence_contracts import validate_workspace_document_query_readback


def generation(*, sensitivity="internal", freshness="current"):
    blocks = [
        {"id": "z", "type": "paragraph", "text": "Procedure record details", "source_span": {"page": 2, "start": 10, "end": 34}},
        {"id": "a", "type": "heading", "text": "PROCEDURE", "source_span": {"page": 1, "start": 0, "end": 9},
         "children": [{"id": "nested", "type": "paragraph", "text": "ＲＥＣＯＲＤ procedure schedule", "source_span": {"page": 1, "start": 7, "end": 32}}]},
    ]
    ir = {"schema_version": "ao.lore.document-ir.v0.1", "document_id": "policy-record", "source": {"resource": "inbox/policy-record.pdf", "digest": canonical_digest("source"), "media_type": "application/pdf"}, "blocks": blocks, "metadata": {}}
    document = {"document_id": "policy-record", "document_ir_digest": canonical_digest(ir), "source_id": "source-policy-record", "source_digest": ir["source"]["digest"], "source_record_digest": canonical_digest("record"), "media_type": "application/pdf", "authority_role": "operator_procedure", "sensitivity": sensitivity, "version": "1", "effective_date": "2026-08-13", "freshness_status": freshness, "qualification_codes": ["fixture-only"], "document_ir": ir}
    value = {"schema_version": "ao.lore.workspace-document-generation.v0.1", "generation_id": "documents-workspace-a-0000000001", "sequence": 1, "prior_generation_digest": None, "workspace_id": "workspace-a", "document_store_id": "documents-workspace-a", "registry_digest": canonical_digest("registry"), "created_at": "2026-08-13T12:00:00Z", "documents": [document], "generation_digest": canonical_digest("placeholder")}
    value["generation_digest"] = canonical_digest({k: v for k, v in value.items() if k != "generation_digest"})
    return value


class DocumentEvidenceQueryTests(unittest.TestCase):
    def test_exact_substring_ranks_before_token_matches_and_preserves_span(self):
        result = query_workspace_documents(generation(), "workspace-a", "procedure record")
        self.assertEqual("answer", result["outcome"])
        self.assertEqual(["z", "nested", "a"], [item["block_id"] for item in result["evidence"]])
        self.assertEqual("Procedure record details", result["evidence"][0]["render_text"])
        self.assertEqual({"page": 2, "start": 10, "end": 34}, result["evidence"][0]["source_span"])
        self.assertTrue(result["evidence"][0]["evidence_id"].startswith("sha256:"))
        self.assertEqual(result, validate_workspace_document_query_readback(result, generation=generation(), workspace_id="workspace-a"))

    def test_nfkc_casefold_nested_blocks_and_deterministic_limit(self):
        result = query_workspace_documents(generation(), "workspace-a", "RECORD PROCEDURE", limit=1)
        self.assertEqual(["nested"], [item["block_id"] for item in result["evidence"]])

    def test_restricted_is_never_rendered_and_stale_investigates(self):
        restricted = query_workspace_documents(generation(sensitivity="restricted"), "workspace-a", "procedure")
        self.assertEqual(("refuse", [], "restricted_evidence"), (restricted["outcome"], restricted["evidence"], restricted["reason_code"]))
        stale = query_workspace_documents(generation(freshness="stale"), "workspace-a", "procedure")
        self.assertEqual("investigate", stale["outcome"])
        self.assertTrue(stale["evidence"])

    def test_rejects_unicode_category_c_prompt(self):
        with self.assertRaises(DocumentEvidenceQueryError):
            query_workspace_documents(generation(), "workspace-a", "procedure\u202erecord")

    def test_zero_hits_is_refusal_and_results_are_detached_without_io(self):
        source = generation()
        with patch("builtins.open", side_effect=AssertionError("I/O attempted")):
            result = query_workspace_documents(source, "workspace-a", "unmatched")
        self.assertEqual(("refuse", []), (result["outcome"], result["evidence"]))
        hit = query_workspace_documents(source, "workspace-a", "procedure")
        hit["evidence"][0]["source_span"]["start"] = 999
        self.assertEqual(10, source["documents"][0]["document_ir"]["blocks"][0]["source_span"]["start"])

    def test_rejects_bad_inputs_and_duplicate_document_ids(self):
        duplicate = generation(); duplicate["documents"].append(deepcopy(duplicate["documents"][0])); duplicate["generation_digest"] = canonical_digest({k: v for k, v in duplicate.items() if k != "generation_digest"})
        for value, workspace, prompt, limit in ((duplicate, "workspace-a", "procedure", 20), (generation(), "wrong", "procedure", 20), (generation(), "workspace-a", "bad\nquery", 20), (generation(), "workspace-a", "procedure", 0)):
            with self.subTest(workspace=workspace, prompt=prompt, limit=limit), self.assertRaises(DocumentEvidenceQueryError):
                query_workspace_documents(value, workspace, prompt, limit=limit)


if __name__ == "__main__":
    unittest.main()
