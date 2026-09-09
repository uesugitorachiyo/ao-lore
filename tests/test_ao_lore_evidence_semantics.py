import copy
import unittest

from ao_lore.benchmark import canonical_digest
from ao_lore.evidence_graph import claim_record
from ao_lore.evidence_semantics import EvidenceSemanticError, review_claim_semantics


SOURCE_DIGEST = "sha256:" + "1" * 64


def document_ir(text="A source shall maintain procedure in good working order.", *, block_type="paragraph", source_span=None):
    if source_span is None:
        source_span = {"start": 10, "end": 10 + len(text)}
    value = {
        "schema_version": "ao.lore.document-ir.v0.1",
        "document_id": SOURCE_DIGEST,
        "source": {"resource": "official.html", "digest": SOURCE_DIGEST, "media_type": "text/html"},
        "parser": {"parser_id": "fixture", "parser_version": "1", "configuration_digest": "sha256:" + "2" * 64},
        "blocks": [{"id": "block-1", "type": block_type, "text": text, "source_span": source_span}],
        "metadata": {},
    }
    return value


def claim(text="A source shall maintain procedure in good working order.", terms=None):
    return claim_record(
        source_id="source-policy", source_digest=SOURCE_DIGEST, authority_role="primary_law",
        excerpt=text, citation_anchor="block-1", operational_question_ids=["question-1"],
        subject_terms=terms or ["source", "procedure"],
        semantic_reason_codes=["topic_match", "citation_supported"],
    )


def review(item, rationale="The source procedure duty is directly stated."):
    ir = document_ir(item["excerpt"])
    return {
        "claim_id": item["claim_id"], "document_ir_digest": canonical_digest(ir),
        "block_id": "block-1", "source_span": {"start": 10, "end": 10 + len(item["excerpt"])},
        "excerpt_digest": item["excerpt_digest"], "rationale": rationale,
    }, ir


class EvidenceSemanticsTests(unittest.TestCase):
    def test_exact_document_ir_excerpt_topic_and_source_span_are_bound(self):
        item = claim()
        semantic_review, ir = review(item)
        result = review_claim_semantics([item], [semantic_review], {"source-policy": ir})
        self.assertEqual(result[0]["reason_codes"], ["topic_match", "citation_supported"])
        self.assertEqual(result[0]["excerpt_digest"], item["excerpt_digest"])
        self.assertEqual(result[0]["source_span"], semantic_review["source_span"])

        changed = copy.deepcopy(ir)
        changed["blocks"][0]["text"] += " Changed."
        changed_review = copy.deepcopy(semantic_review)
        changed_review["document_ir_digest"] = canonical_digest(changed)
        with self.assertRaisesRegex(EvidenceSemanticError, "excerpt binding differs"):
            review_claim_semantics([item], [changed_review], {"source-policy": changed})

    def test_nfkc_casefold_and_unicode_alphanumeric_topic_matching_is_exact(self):
        text = "The SOURCE maintains the CONTROL system."
        item = claim(text, ["ｓｏｕｒｃｅ", "control system"])
        semantic_review, ir = review(item, "Source CONTROL system requirement")
        result = review_claim_semantics([item], [semantic_review], {"source-policy": ir})
        self.assertEqual(result[0]["reason_codes"], ["topic_match", "citation_supported"])

    def test_pdf_and_ocr_page_coordinates_are_preserved_and_exactly_bound(self):
        item = claim()
        spans = {
            "pdf": {"start": 10, "end": 10 + len(item["excerpt"]), "page": 3,
                    "coordinates": [10.25, 20, 300.5, 42.75]},
            "ocr": {"start": 10, "end": 10 + len(item["excerpt"]), "page": 7,
                    "coordinates": [12, 22, 312, 52]},
        }
        for kind, span in spans.items():
            with self.subTest(kind=kind):
                ir = document_ir(item["excerpt"], source_span=span)
                semantic_review, _ = review(item)
                semantic_review["source_span"] = copy.deepcopy(span)
                semantic_review["document_ir_digest"] = canonical_digest(ir)
                result = review_claim_semantics([item], [semantic_review], {"source-policy": ir})
                self.assertEqual(result[0]["source_span"], span)
                self.assertIsNot(result[0]["source_span"], span)

        span = spans["pdf"]
        ir = document_ir(item["excerpt"], source_span=span)
        semantic_review, _ = review(item)
        semantic_review["source_span"] = copy.deepcopy(span)
        semantic_review["document_ir_digest"] = canonical_digest(ir)
        for mutate in (
            lambda value: value.update(page=4),
            lambda value: value["coordinates"].__setitem__(2, 301.5),
            lambda value: value.update(section="private"),
        ):
            changed = copy.deepcopy(semantic_review)
            mutate(changed["source_span"])
            with self.assertRaisesRegex(EvidenceSemanticError, "excerpt binding differs|source span is invalid"):
                review_claim_semantics([item], [changed], {"source-policy": ir})

    def test_wrong_topic_and_copied_rationales_reject(self):
        item = claim()
        semantic_review, ir = review(item, "Archive retention interval")
        with self.assertRaisesRegex(EvidenceSemanticError, "topic mismatch"):
            review_claim_semantics([item], [semantic_review], {"source-policy": ir})

        first = claim("A source shall maintain procedure.", ["source", "procedure"])
        second = claim_record(
            source_id="source-guide", source_digest="sha256:" + "3" * 64,
            authority_role="technical_guidance", excerpt="Control control prevents record growth.",
            citation_anchor="block-1", operational_question_ids=["question-1"],
            subject_terms=["control", "record"], semantic_reason_codes=["topic_match", "citation_supported"],
        )
        first_review, first_ir = review(first, "Source procedure control record guidance")
        second_review, second_ir = review(second, "Source procedure control record guidance")
        second_ir["document_id"] = second["source_digest"]
        second_ir["source"]["digest"] = second["source_digest"]
        second_review["document_ir_digest"] = canonical_digest(second_ir)
        with self.assertRaisesRegex(EvidenceSemanticError, "copied rationale"):
            review_claim_semantics(
                [first, second], [first_review, second_review],
                {"source-policy": first_ir, "source-guide": second_ir},
            )

    def test_layout_artifacts_reject_by_closed_structural_predicates(self):
        for text, block_type in (("Page 12", "paragraph"), ("Required procedures", "footer"), ("OCR � fragment", "paragraph")):
            item = claim(text, ["required"] if "Required" in text else ["page"] if "Page" in text else ["fragment"])
            semantic_review, ir = review(item, f"The {item['subject_terms'][0]} evidence is relevant")
            ir["blocks"][0]["type"] = block_type
            semantic_review["document_ir_digest"] = canonical_digest(ir)
            with self.assertRaisesRegex(EvidenceSemanticError, "layout artifact"):
                review_claim_semantics([item], [semantic_review], {"source-policy": ir})

    def test_review_is_bounded_closed_and_never_mutates_candidate_or_canonical_state(self):
        item = claim()
        semantic_review, ir = review(item)
        original_claim, original_ir = copy.deepcopy(item), copy.deepcopy(ir)
        semantic_review["unknown"] = True
        with self.assertRaisesRegex(EvidenceSemanticError, "review keys differ"):
            review_claim_semantics([item], [semantic_review], {"source-policy": ir})
        self.assertEqual(item, original_claim)
        self.assertEqual(ir, original_ir)


if __name__ == "__main__":
    unittest.main()
