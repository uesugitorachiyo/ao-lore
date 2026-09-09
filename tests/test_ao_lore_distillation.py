import copy
import inspect
import unittest

from ao_lore.benchmark import canonical_digest
from ao_lore.distillation import (
    DeterministicDistiller,
    DistillationError,
    DistillationProposal,
    ScriptedDistiller,
    distill_document_ir,
)
import ao_lore.distillation as distillation_module


def document_ir():
    digest = "sha256:" + "a" * 64
    return {
        "schema_version": "ao.lore.document-ir.v0.1",
        "document_id": digest,
        "source": {"resource": "sources/note.md", "digest": digest, "media_type": "text/markdown"},
        "parser": {"parser_id": "native-markdown", "parser_version": "1", "configuration_digest": canonical_digest({})},
        "blocks": [
            {"id": "b1", "type": "heading", "text": "Policy", "level": 1, "source_span": {"start": 0, "end": 8}},
            {"id": "b2", "type": "paragraph", "text": "Use reviewed evidence.", "source_span": {"start": 9, "end": 31}},
        ],
        "metadata": {"title": "Policy note"},
    }


class DistillationTests(unittest.TestCase):
    def test_explicit_answerable_proposal_emits_exact_v0_2_payload_from_ir(self):
        ir = document_ir()
        before = copy.deepcopy(ir)
        proposal = DistillationProposal(
            concepts=[],
            claim_mappings=[{"claim_id": "claim-1", "block_ids": ["b2"]}],
            links=[],
            contradiction_warnings=[],
            trace={
                "adapter": "scripted-distiller",
                "policy_version": "answerable-v0.2",
                "input_block_count": 2,
                "candidate_concept_count": 0,
                "candidate_claim_count": 1,
                "candidate_citation_count": 1,
                "private_reasoning_persisted": False,
            },
            claims=[{
                "claim_id": "claim-1",
                "text": "Use reviewed evidence.",
                "source_block_ids": ["b2"],
                "citation_id": "citation-1",
            }],
            citations=[{
                "citation_id": "citation-1",
                "render_text": "Use reviewed evidence.",
                "source_block_ids": ["b2"],
            }],
            knowledge_policy={"sensitivity": "public", "stale_after": None},
        )

        result = distill_document_ir(ir, ScriptedDistiller(lambda *_: proposal))

        self.assertEqual(ir, before)
        self.assertEqual("ao.lore.distillation-result.v0.2", result["schema_version"])
        self.assertEqual("ao.lore.okf-candidate.v0.2", result["candidate"]["schema_version"])
        self.assertEqual(proposal.claims, result["candidate"]["claims"])
        self.assertEqual(proposal.citations, result["candidate"]["citations"])
        self.assertEqual(proposal.knowledge_policy, result["candidate"]["knowledge_policy"])

    def test_answerable_payload_cannot_invent_text_or_block_anchors(self):
        base = dict(
            concepts=[], claim_mappings=[{"claim_id": "claim-1", "block_ids": ["b2"]}],
            links=[], contradiction_warnings=[], trace={
                "adapter": "scripted", "policy_version": "v0.2", "input_block_count": 2,
                "candidate_concept_count": 0, "candidate_claim_count": 1,
                "candidate_citation_count": 1, "private_reasoning_persisted": False,
            }, knowledge_policy={"sensitivity": "public", "stale_after": None},
        )
        for label, claims, citations in (
            ("invented claim", [{"claim_id": "claim-1", "text": "Invented", "source_block_ids": ["b2"], "citation_id": "citation-1"}], [{"citation_id": "citation-1", "render_text": "Use reviewed evidence.", "source_block_ids": ["b2"]}]),
            ("invented citation", [{"claim_id": "claim-1", "text": "Use reviewed evidence.", "source_block_ids": ["b2"], "citation_id": "citation-1"}], [{"citation_id": "citation-1", "render_text": "private/source.pdf", "source_block_ids": ["b2"]}]),
            ("unknown anchor", [{"claim_id": "claim-1", "text": "Use reviewed evidence.", "source_block_ids": ["missing"], "citation_id": "citation-1"}], [{"citation_id": "citation-1", "render_text": "Use reviewed evidence.", "source_block_ids": ["missing"]}]),
        ):
            with self.subTest(label=label), self.assertRaises(DistillationError):
                distill_document_ir(document_ir(), ScriptedDistiller(lambda *_: DistillationProposal(claims=claims, citations=citations, **base)))

    def test_partial_answerable_payload_does_not_silently_upgrade_legacy(self):
        with self.assertRaises(DistillationError):
            DistillationProposal(
                concepts=[], claim_mappings=[], links=[], contradiction_warnings=[], trace={},
                claims=[], citations=None, knowledge_policy={"sensitivity": "public", "stale_after": None},
            )

    def test_deterministic_distiller_produces_only_noncanonical_candidate(self):
        ir = document_ir()
        before = copy.deepcopy(ir)
        result = distill_document_ir(ir, DeterministicDistiller())
        candidate = result["candidate"]
        self.assertEqual(ir, before)
        self.assertEqual(candidate["document_ir_digest"], canonical_digest(ir))
        self.assertFalse(candidate["canonical"])
        self.assertFalse(candidate["promotion_authority"])
        self.assertEqual(candidate["concepts"][0]["title"], "Policy")
        self.assertEqual(candidate["claim_mappings"][0]["block_ids"], ["b2"])
        self.assertEqual("ao.lore.okf-candidate.v0.1", candidate["schema_version"])
        self.assertNotIn("claims", candidate)

    def test_deterministic_distiller_emits_exact_answerable_payload_only_with_policy(self):
        ir = document_ir()
        policy = {"sensitivity": "public", "stale_after": None}
        context = {
            "schema_version": "ao.lore.candidate-comparison-context.v0.1",
            "candidates": [],
            "knowledge_policy": policy,
        }

        first = distill_document_ir(ir, DeterministicDistiller(), context)
        second = distill_document_ir(ir, DeterministicDistiller(), context)

        self.assertEqual(first, second)
        self.assertEqual("ao.lore.distillation-result.v0.2", first["schema_version"])
        candidate = first["candidate"]
        self.assertEqual("ao.lore.okf-candidate.v0.2", candidate["schema_version"])
        self.assertEqual(policy, candidate["knowledge_policy"])
        self.assertEqual([{
            "claim_id": "candidate-claim-1",
            "text": "Use reviewed evidence.",
            "source_block_ids": ["b2"],
            "citation_id": "candidate-citation-1",
        }], candidate["claims"])
        self.assertEqual([{
            "citation_id": "candidate-citation-1",
            "render_text": "Use reviewed evidence.",
            "source_block_ids": ["b2"],
        }], candidate["citations"])
        self.assertEqual(1, first["distillation_trace"]["candidate_claim_count"])
        self.assertEqual(1, first["distillation_trace"]["candidate_citation_count"])

    def test_deterministic_answerability_rejects_malformed_policy(self):
        for policy in (
            {"sensitivity": "public"},
            {"sensitivity": "public", "stale_after": None, "extra": True},
            {"sensitivity": "secret", "stale_after": None},
        ):
            context = {
                "schema_version": "ao.lore.candidate-comparison-context.v0.1",
                "candidates": [],
                "knowledge_policy": policy,
            }
            with self.subTest(policy=policy), self.assertRaises(DistillationError):
                distill_document_ir(document_ir(), DeterministicDistiller(), context)

    def test_long_source_block_is_never_truncated_into_answer_evidence(self):
        ir = document_ir()
        ir["blocks"][1]["text"] = "x" * 513
        ir["blocks"][1]["source_span"]["end"] = 522
        context = {
            "schema_version": "ao.lore.candidate-comparison-context.v0.1",
            "candidates": [],
            "knowledge_policy": {"sensitivity": "internal", "stale_after": None},
        }

        result = distill_document_ir(ir, DeterministicDistiller(), context)

        candidate = result["candidate"]
        self.assertEqual([{"claim_id": "candidate-claim-1", "block_ids": ["b2"]}], candidate["claim_mappings"])
        self.assertEqual([], candidate["claims"])
        self.assertEqual([], candidate["citations"])
        self.assertNotIn("x" * 512, repr(candidate["claims"]))

    def test_scripted_distiller_receives_ir_and_explicit_candidate_context_only(self):
        observed = {}

        def handler(ir, context):
            observed["keys"] = sorted(ir)
            observed["context"] = context
            return DistillationProposal(
                concepts=[{"candidate_concept_id": "c1", "title": "Proposed"}],
                claim_mappings=[{"claim_id": "claim-1", "block_ids": ["b2"]}],
                links=[], contradiction_warnings=[], trace={"policy": "scripted-v1"},
            )

        result = distill_document_ir(document_ir(), ScriptedDistiller(handler), {"comparison_claims": []})
        self.assertNotIn("source_bytes", observed["keys"])
        self.assertEqual(observed["context"], {"comparison_claims": []})
        self.assertEqual(result["distillation_trace"]["policy"], "scripted-v1")

    def test_non_ir_and_unknown_top_level_fields_fail_closed(self):
        with self.assertRaises(DistillationError):
            distill_document_ir({"source_path": "private.pdf"}, DeterministicDistiller())
        ir = document_ir()
        ir["raw_source_bytes"] = "forbidden"
        with self.assertRaises(DistillationError):
            distill_document_ir(ir, DeterministicDistiller())

    def test_claim_mapping_must_reference_existing_ir_blocks(self):
        adapter = ScriptedDistiller(lambda ir, context: DistillationProposal(
            concepts=[], claim_mappings=[{"claim_id": "claim", "block_ids": ["missing"]}],
            links=[], contradiction_warnings=[], trace={},
        ))
        with self.assertRaises(DistillationError):
            distill_document_ir(document_ir(), adapter)

    def test_module_has_no_source_format_or_promotion_api(self):
        source = inspect.getsource(distillation_module).lower()
        for forbidden in ("docling", "application/pdf", "open(", "read_bytes", "brain/", "promote_candidate"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
