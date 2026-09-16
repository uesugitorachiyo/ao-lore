from __future__ import annotations

import hashlib
import unittest

from ao_lore.benchmark import canonical_digest
from ao_lore.document_evidence_export import DocumentEvidenceExportError, export_document_passages
from ao_lore.document_evidence_query import query_workspace_documents


def generation(text: str, *, sensitivity: str = "public"):
    block = {"id": "b1", "type": "paragraph", "text": text,
             "source_span": {"start": 0, "end": len(text)}}
    source_digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    ir = {"schema_version": "ao.lore.document-ir.v0.1", "document_id": "sample",
          "source": {"resource": "inbox/sample.txt", "digest": source_digest, "media_type": "text/plain"},
          "blocks": [block], "metadata": {}}
    doc = {"document_id": "sample", "document_ir_digest": canonical_digest(ir),
           "source_id": "native-sample", "source_digest": source_digest,
           "source_record_digest": canonical_digest("sample"), "media_type": "text/plain",
           "authority_role": "operator_procedure", "sensitivity": sensitivity, "version": "1",
           "effective_date": None, "freshness_status": "current", "qualification_codes": [], "document_ir": ir}
    value = {"schema_version": "ao.lore.workspace-document-generation.v0.1", "generation_id": "documents-sandbox-0000000001",
             "sequence": 1, "prior_generation_digest": None, "workspace_id": "sandbox", "document_store_id": "documents-sandbox",
             "registry_digest": canonical_digest("registry"), "created_at": "2026-09-15T00:00:00Z", "documents": [doc],
             "generation_digest": "sha256:" + "0" * 64}
    value["generation_digest"] = canonical_digest({key: item for key, item in value.items() if key != "generation_digest"})
    return value


class EvidenceSetPortTests(unittest.TestCase):
    def test_export_preserves_exact_unicode_character_offsets(self):
        text = "木の数量。\nThe verified quantity is 17 units."
        current = generation(text)
        readback = query_workspace_documents(current, "sandbox", "verified quantity")
        packets = export_document_passages(current, readback, "sandbox", [{
            "native_source_id": "native-sample", "source_id": "SAMPLE",
            "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text,
        }])
        self.assertEqual("SAMPLE", packets[0]["source_id"])
        self.assertEqual(text[packets[0]["char_start"]:packets[0]["char_end"]], packets[0]["text"])

    def test_export_fails_closed_when_original_text_no_longer_matches_evidence(self):
        text = "The verified quantity is 17 units."
        current = generation(text)
        readback = query_workspace_documents(current, "sandbox", "verified quantity")
        replacement = "The verified quantity is 18 units."
        with self.assertRaises(DocumentEvidenceExportError):
            export_document_passages(current, readback, "sandbox", [{
                "native_source_id": "native-sample", "source_id": "SAMPLE",
                "sha256": hashlib.sha256(replacement.encode()).hexdigest(), "text": replacement,
            }])

    def test_subject_heading_prevents_near_name_selection(self):
        left = "# Birch Depot — terms\nThe amended price is 29 credits."
        right = "# Birch Depot East — terms\nThe amended price is 81 credits."
        base = generation(left)
        other = generation(right)
        other["documents"][0]["document_id"] = "east"
        other["documents"][0]["source_id"] = "native-east"
        other["documents"][0]["document_ir"]["document_id"] = "east"
        other["documents"][0]["document_ir_digest"] = canonical_digest(other["documents"][0]["document_ir"])
        base["documents"].append(other["documents"][0])
        base["documents"].sort(key=lambda item: item["document_id"])
        base["generation_digest"] = canonical_digest({key: item for key, item in base.items() if key != "generation_digest"})
        result = query_workspace_documents(base, "sandbox", "What is Birch Depot East's amended price?")
        self.assertIn("81 credits", "\n".join(item["render_text"] for item in result["evidence"]))
        self.assertNotIn("29 credits", "\n".join(item["render_text"] for item in result["evidence"]))

    def test_unrelated_restricted_subject_does_not_block_named_public_subject(self):
        public = generation("# Birch Depot — terms\nThe amended price is 29 credits.")
        restricted = generation("# Cedar Depot — terms\nThe amended price is 81 credits.", sensitivity="restricted")
        restricted["documents"][0]["document_id"] = "cedar"
        restricted["documents"][0]["source_id"] = "native-cedar"
        restricted["documents"][0]["document_ir"]["document_id"] = "cedar"
        restricted["documents"][0]["document_ir_digest"] = canonical_digest(restricted["documents"][0]["document_ir"])
        public["documents"].append(restricted["documents"][0])
        public["documents"].sort(key=lambda item: item["document_id"])
        public["generation_digest"] = canonical_digest({key: item for key, item in public.items() if key != "generation_digest"})
        result = query_workspace_documents(public, "sandbox", "What is Birch Depot's amended price?")
        self.assertEqual("answer", result["outcome"])
        self.assertIn("29 credits", "\n".join(item["render_text"] for item in result["evidence"]))
