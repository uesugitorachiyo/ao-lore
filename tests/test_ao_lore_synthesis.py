import copy
import inspect
import unittest

from ao_lore.synthesis import (
    DeterministicSynthesizer,
    ScriptedSynthesizer,
    SynthesisDraft,
    SynthesisError,
    synthesize_from_evidence,
)
import ao_lore.synthesis as synthesis_module


DIGEST = "sha256:" + "e" * 64


def ledger(verified=True, citation="[policy](brain/policy.md)"):
    return [
        {
            "evidence_id": "ev-policy",
            "requirement_ids": ["policy"],
            "supported_claims": [{"claim_id": "claim-policy", "text": "The reviewed policy requires cited evidence."}],
            "source_ref": "brain/policy.md",
            "source_digest": DIGEST,
            "citation": citation,
            "provenance": {"kind": "canonical-okf", "path": "brain/policy.md"},
            "freshness_met": True,
            "trust_met": True,
            "verified": verified,
        }
    ]


def coverage(decision="answer", all_gates=True):
    gates = {
        "coverage_target_met": all_gates,
        "mandatory_requirements_met": all_gates,
        "provenance_present": all_gates,
        "citations_present": all_gates,
        "freshness_met": all_gates,
        "trust_met": all_gates,
        "no_hard_contradiction": all_gates,
        "deterministic_validation_passed": all_gates,
    }
    return {
        "schema_version": "ao.lore.evidence-coverage-report.v0.1",
        "query_digest": DIGEST,
        "profile_id": "decision-v1",
        "target": 0.8,
        "coverage": 1.0 if all_gates else 0.25,
        "requirements": [
            {"id": "policy", "weight": 1, "status": "satisfied" if all_gates else "missing", "satisfaction_value": 1 if all_gates else 0, "evidence_refs": ["brain/policy.md"] if all_gates else [], "coverage_gain": 1 if all_gates else 0}
        ],
        "gates": gates,
        "coverage_history": [],
        "decision": decision,
        "reason": "fixture",
    }


class EvidenceSynthesisTests(unittest.TestCase):
    def test_sufficient_ledger_yields_only_exact_supported_claims(self):
        result = synthesize_from_evidence(
            "What is the policy?", ledger(), coverage(), "text",
            {"required": True}, {"include_limitations": True}, DeterministicSynthesizer(),
        )
        self.assertEqual(result["status"], "answer")
        self.assertIn("The reviewed policy requires cited evidence.", result["answer"])
        self.assertIn("[policy](brain/policy.md)", result["answer"])
        self.assertEqual(result["claim_ids"], ["claim-policy"])
        self.assertEqual(result["coverage_report_digest"].split(":")[0], "sha256")

    def test_scripted_adapter_cannot_add_unsupported_claim(self):
        adapter = ScriptedSynthesizer(lambda payload: SynthesisDraft(["invented"], []))
        with self.assertRaises(SynthesisError):
            synthesize_from_evidence("query", ledger(), coverage(), "text", {"required": True}, {}, adapter)

    def test_unverified_or_uncited_evidence_fails_closed(self):
        with self.assertRaises(SynthesisError):
            synthesize_from_evidence("query", ledger(verified=False), coverage(), "text", {"required": True}, {}, DeterministicSynthesizer())
        with self.assertRaises(SynthesisError):
            synthesize_from_evidence("query", ledger(citation=""), coverage(), "text", {"required": True}, {}, DeterministicSynthesizer())

    def test_insufficient_ledger_forces_partial_or_refusal(self):
        adapter = ScriptedSynthesizer(lambda payload: SynthesisDraft(["claim-policy"], ["limited"]))
        partial = synthesize_from_evidence("query", ledger(), coverage("partial", False), "text", {"required": True}, {}, adapter)
        self.assertEqual(partial["status"], "partial")
        self.assertIn("Insufficient evidence", partial["answer"])
        refused = synthesize_from_evidence("query", ledger(), coverage("refuse", False), "text", {"required": True}, {}, adapter)
        self.assertEqual(refused["status"], "refuse")
        self.assertEqual(refused["claim_ids"], [])

    def test_replan_report_cannot_be_turned_into_an_answer(self):
        with self.assertRaises(SynthesisError):
            synthesize_from_evidence("query", ledger(), coverage("replan", False), "text", {"required": True}, {}, DeterministicSynthesizer())

    def test_adapter_receives_no_browse_or_parser_capability(self):
        observed = {}

        def handler(payload):
            observed["keys"] = sorted(payload)
            return SynthesisDraft(["claim-policy"], [])

        synthesize_from_evidence("query", ledger(), coverage(), "text", {"required": True}, {}, ScriptedSynthesizer(handler))
        self.assertEqual(observed["keys"], ["answer_format", "citation_requirements", "coverage_report", "evidence_ledger", "normalized_query", "qualification_requirements"])
        source = inspect.getsource(synthesis_module).lower()
        for forbidden in ("browse_node", "parserregistry", "read_bounded_source", "brain_root", "open("):
            self.assertNotIn(forbidden, source)

    def test_inputs_are_not_mutated(self):
        entries = ledger()
        report = coverage()
        before_entries, before_report = copy.deepcopy(entries), copy.deepcopy(report)
        synthesize_from_evidence("query", entries, report, "structured", {"required": True}, {}, DeterministicSynthesizer())
        self.assertEqual(entries, before_entries)
        self.assertEqual(report, before_report)

    def test_coverage_may_bind_exact_evidence_ids(self):
        entries = ledger()
        report = coverage()
        report["requirements"][0]["evidence_refs"] = ["ev-policy"]
        result = synthesize_from_evidence(
            "query", entries, report, "text", {"required": True}, {}, DeterministicSynthesizer()
        )
        self.assertEqual(["ev-policy"], result["evidence_ids"])

    def test_qualifications_are_strictly_bounded(self):
        for qualifications in (["x"] * 33, ["x" * 513]):
            with self.subTest(size=len(qualifications)), self.assertRaises(SynthesisError):
                synthesize_from_evidence(
                    "query", ledger(), coverage(), "text", {"required": True}, {},
                    ScriptedSynthesizer(lambda payload, q=qualifications: SynthesisDraft(["claim-policy"], q)),
                )


if __name__ == "__main__":
    unittest.main()
