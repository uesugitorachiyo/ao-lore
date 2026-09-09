import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from ao_lore.promotion import PromotionError, _PromotionDependencies, prepare_promotion, validate_audit_chain
from ao_lore.knowledge_contracts import claims_digest, citations_digest, semantic_key_v0_3


ROOT = Path(__file__).resolve().parents[1]
FIXED_NOW = "2026-08-11T12:00:00Z"
SOURCE_HEAD = "1" * 40


def canonical(value):
    import hashlib
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_fixture(root, *, answerable=False, evidence=False, candidate_id="candidate-public-fixture"):
    candidates = root / "working" / "candidates"
    brain = root / "brain"
    runtime = root / ".ao-lore" / "promotions"
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.1", "candidate_id": candidate_id,
        "document_ir_digest": "sha256:" + "1" * 64, "concepts": [], "claim_mappings": [], "links": [],
        "contradiction_warnings": [], "canonical": False, "promotion_authority": False,
    }
    if evidence:
        candidate.pop("document_ir_digest")
        origin = {
            "workspace_id": "workspace-public-fixture", "document_store_id": "documents-public-fixture",
            "generation_digest": "sha256:" + "6" * 64, "document_id": "document-public-fixture",
            "document_ir_digest": "sha256:" + "7" * 64, "source_id": "source-public-fixture",
            "source_digest": "sha256:" + "8" * 64, "evidence_kind": "document_block",
            "evidence_id": "evidence-public-fixture", "evidence_digest": "sha256:" + "9" * 64,
            "block_id": "block-public-fixture", "render_text": "Reviewed exact workspace evidence.",
            "source_span": {"page": 1, "start": 0, "end": 32}, "authority_role": "operator_procedure",
            "sensitivity": "internal", "freshness_status": "current", "qualification_codes": [],
        }
        origins = [origin]
        selection_digest = canonical({"domain": "ao.lore.evidence-selection.v0.1", "evidence": origins})
        claims = [{"claim_id": "claim-1", "text": origin["render_text"], "source_block_ids": [origin["block_id"]], "citation_id": "citation-1"}]
        citations = [{"citation_id": "citation-1", "render_text": origin["render_text"], "source_block_ids": [origin["block_id"]]}]
        candidate.update({
            "schema_version": "ao.lore.okf-candidate.v0.3",
            "evidence_selection_digest": selection_digest, "evidence_origins": origins,
            "claim_mappings": [{"claim_id": "claim-1", "block_ids": [origin["block_id"]]}],
            "claims_schema_version": "ao.lore.canonical-claim-set.v0.1", "claims_digest": claims_digest(claims), "claims": claims,
            "citations_schema_version": "ao.lore.canonical-citation-set.v0.1", "citations_digest": citations_digest(citations), "citations": citations,
            "knowledge_policy": {"sensitivity": "internal", "stale_after": None},
        })
    elif answerable:
        claims = [{"claim_id": "claim-1", "text": "Reviewed evidence is required.", "source_block_ids": ["b1"], "citation_id": "citation-1"}]
        citations = [{"citation_id": "citation-1", "render_text": "Reviewed evidence is required.", "source_block_ids": ["b1"]}]
        candidate.update({
            "schema_version": "ao.lore.okf-candidate.v0.2",
            "claim_mappings": [{"claim_id": "claim-1", "block_ids": ["b1"]}],
            "claims_schema_version": "ao.lore.canonical-claim-set.v0.1", "claims_digest": claims_digest(claims), "claims": claims,
            "citations_schema_version": "ao.lore.canonical-citation-set.v0.1", "citations_digest": citations_digest(citations), "citations": citations,
            "knowledge_policy": {"sensitivity": "public", "stale_after": None},
        })
    candidate_digest = canonical(candidate)
    provenance = ({
        "schema_version": "ao.lore.candidate-provenance.v0.3", "candidate_id": candidate_id,
        "candidate_digest": candidate_digest, "proposal_id": "proposal-evidence-selection-fixture",
        "evidence_selection_digest": candidate["evidence_selection_digest"],
        "primary_workspace_id": "workspace-public-fixture", "registry_digest": "sha256:" + "a" * 64,
        "evidence_origins": json.loads(json.dumps(candidate["evidence_origins"])), "created_at": "2026-08-11T11:00:00Z",
    } if evidence else {
        "schema_version": "ao.lore.candidate-provenance.v0.1", "candidate_id": candidate_id,
        "candidate_digest": candidate_digest, "document_ir_digest": candidate["document_ir_digest"],
        "source_digest": "sha256:" + "2" * 64, "parser_id": "fixture-parser", "parser_version": "1.0.0",
        "parse_quality_report_digest": "sha256:" + "3" * 64, "parser_selection_report_digest": "sha256:" + "4" * 64,
        "distillation_trace_digest": "sha256:" + "5" * 64, "created_at": "2026-08-11T11:00:00Z",
    })
    event = {
        "schema_version": "ao.lore.candidate-review-event.v0.1", "sequence": 1, "candidate_id": candidate_id,
        "candidate_digest": candidate_digest, "previous_event_digest": None, "decision": "accept",
        "reviewer": "fixture-reviewer", "rationale": "public fixture accepted", "recorded_at": "2026-08-11T11:05:00Z",
    }
    event["event_digest"] = canonical(event)
    target = candidates / candidate_id
    write_json(target / "candidate.json", candidate)
    write_json(target / "provenance.json", provenance)
    write_json(target / "reviews" / f"000001-{event['event_digest'][7:]}.json", event)
    brain.mkdir(parents=True)
    (brain / "README.md").write_text("# Fixture brain\n", encoding="utf-8")
    (runtime / "proposals").mkdir(parents=True)
    return candidate_id, candidates, brain, runtime


class PromotionPrepareTests(unittest.TestCase):
    def test_v0_3_candidate_maps_without_loss_to_v0_2_proposal_and_v0_4_entry(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            candidate_id, candidates, brain, runtime = build_fixture(root, evidence=True)
            candidate_path = candidates / candidate_id / "candidate.json"
            candidate_bytes = candidate_path.read_bytes()
            source = json.loads(candidate_bytes)
            deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            proposal = prepare_promotion(candidate_id, runtime / "proposals" / "evidence.json", dependencies=deps)
            self.assertEqual("ao.lore.promotion-proposal.v0.2", proposal["schema_version"])
            self.assertEqual("ao.lore.okf-canonical-entry.v0.4", proposal["canonical_entry"]["schema_version"])
            for field in ("evidence_selection_digest", "evidence_origins", "claims", "citations", "knowledge_policy"):
                self.assertEqual(source[field], proposal[field] if field in proposal else proposal["canonical_entry"][field])
            for fabricated in ("document_ir_digest", "source_digest", "parser"):
                self.assertNotIn(fabricated, proposal)
                self.assertNotIn(fabricated, proposal["canonical_entry"])
            self.assertEqual(candidate_bytes, candidate_path.read_bytes())

    def test_v0_3_prepare_rejects_mismatched_and_ambiguous_origin_bindings(self):
        for case in ("mismatch", "ambiguous"):
            with self.subTest(case=case), tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
                root = Path(name)
                candidate_id, candidates, brain, runtime = build_fixture(root, evidence=True)
                provenance_path = candidates / candidate_id / "provenance.json"
                provenance = json.loads(provenance_path.read_text())
                if case == "mismatch":
                    provenance["evidence_selection_digest"] = "sha256:" + "b" * 64
                else:
                    second = json.loads(json.dumps(provenance["evidence_origins"][0]))
                    second["evidence_id"] = "evidence-second"
                    second["evidence_digest"] = "sha256:" + "c" * 64
                    provenance["evidence_origins"].append(second)
                    candidate_path = candidates / candidate_id / "candidate.json"
                    candidate = json.loads(candidate_path.read_text())
                    candidate["evidence_origins"] = json.loads(json.dumps(provenance["evidence_origins"]))
                    candidate["evidence_selection_digest"] = canonical({"domain": "ao.lore.evidence-selection.v0.1", "evidence": candidate["evidence_origins"]})
                    write_json(candidate_path, candidate)
                    provenance["candidate_digest"] = canonical(candidate)
                    provenance["evidence_selection_digest"] = candidate["evidence_selection_digest"]
                    review_path = next((candidates / candidate_id / "reviews").iterdir())
                    event = json.loads(review_path.read_text()); review_path.unlink()
                    event["candidate_digest"] = provenance["candidate_digest"]
                    event["event_digest"] = canonical({k: v for k, v in event.items() if k != "event_digest"})
                    write_json(review_path.with_name(f"000001-{event['event_digest'][7:]}.json"), event)
                write_json(provenance_path, provenance)
                deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
                with self.assertRaises(PromotionError):
                    prepare_promotion(candidate_id, runtime / "proposals" / f"{case}.json", dependencies=deps)

    def test_v0_2_candidate_maps_to_v0_3_entry_with_exact_reviewed_payload(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            root = Path(name)
            candidate_id, candidates, brain, runtime = build_fixture(root, answerable=True)
            deps = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)
            proposal = prepare_promotion(candidate_id, runtime / "proposals" / "answerable.json", dependencies=deps)
            source = json.loads((candidates / candidate_id / "candidate.json").read_text())
            entry = proposal["canonical_entry"]
            self.assertEqual("ao.lore.okf-canonical-entry.v0.3", entry["schema_version"])
            for field in ("claims_schema_version", "claims_digest", "claims", "citations_schema_version", "citations_digest", "citations", "knowledge_policy"):
                self.assertEqual(source[field], entry[field])
            self.assertEqual(semantic_key_v0_3(source["concepts"], source["claim_mappings"], source["links"], source["claims_digest"], source["citations_digest"], source["knowledge_policy"]), entry["semantic_key"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore")
        self.root = Path(self.temp.name)
        self.candidate_id, candidates, brain, runtime = build_fixture(self.root)
        self.dependencies = _PromotionDependencies(candidates, brain, runtime, lambda: FIXED_NOW, lambda: SOURCE_HEAD)

    def tearDown(self):
        self.temp.cleanup()

    def test_two_public_prepares_serialize_without_corrupting_state(self):
        output = self.dependencies.proposals_root / "contended.json"
        barrier = threading.Barrier(3); results = []; errors = []
        def prepare():
            barrier.wait()
            try: results.append(prepare_promotion(self.candidate_id, output, dependencies=self.dependencies))
            except PromotionError as exc: errors.append(exc)
        threads = [threading.Thread(target=prepare) for _ in range(2)]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join(2)
        self.assertEqual(1, len(results)); self.assertEqual(1, len(errors))
        self.assertEqual(results[0], json.loads(output.read_text()))
        self.assertEqual(["prepare_started", "prepare_succeeded", "prepare_started", "prepare_rejected"], [event["event_type"] for event in validate_audit_chain(self.dependencies)])

    def test_prepare_is_deterministic_and_binds_exact_state(self):
        first = prepare_promotion(self.candidate_id, self.dependencies.proposals_root / "first.json", dependencies=self.dependencies)
        second = prepare_promotion(self.candidate_id, self.dependencies.proposals_root / "second.json", dependencies=self.dependencies)
        self.assertEqual(first["proposal_digest"], second["proposal_digest"])
        self.assertEqual(first["promotion_id"], second["promotion_id"])
        self.assertEqual(first["source_head"], SOURCE_HEAD)
        self.assertEqual(first["canonical_entry_digest"], canonical(first["canonical_entry"]))
        self.assertEqual(first["candidate_digest"], first["canonical_entry"]["candidate_digest"])
        self.assertEqual(first["accepted_review_head_digest"], first["canonical_entry"]["accepted_review_head_digest"])
        self.assertEqual(2, len(first["allowed_write_set"]))
        self.assertEqual(first, json.loads((self.dependencies.proposals_root / "first.json").read_text()))
        self.assertFalse((self.dependencies.brain_root / "generations").exists())
        audit = sorted((self.dependencies.promotions_root / "audit").glob("*.json"))
        self.assertEqual(4, len(audit))

    def test_prepare_requires_accepted_review_and_no_contradictions(self):
        event_path = next((self.dependencies.candidate_root / self.candidate_id / "reviews").iterdir())
        event = json.loads(event_path.read_text())
        event["decision"] = "reject"
        event["event_digest"] = canonical({k: v for k, v in event.items() if k != "event_digest"})
        replacement = event_path.with_name(f"000001-{event['event_digest'][7:]}.json")
        event_path.unlink()
        write_json(replacement, event)
        with self.assertRaisesRegex(PromotionError, "accepted"):
            prepare_promotion(self.candidate_id, self.dependencies.proposals_root / "rejected.json", dependencies=self.dependencies)

        candidate_path = self.dependencies.candidate_root / self.candidate_id / "candidate.json"
        candidate = json.loads(candidate_path.read_text())
        candidate["contradiction_warnings"] = ["unresolved"]
        write_json(candidate_path, candidate)
        with self.assertRaises(PromotionError):
            prepare_promotion(self.candidate_id, self.dependencies.proposals_root / "warning.json", dependencies=self.dependencies)

    def test_prepare_output_is_contained_and_exclusive(self):
        output = self.dependencies.proposals_root / "proposal.json"
        prepare_promotion(self.candidate_id, output, dependencies=self.dependencies)
        with self.assertRaisesRegex(PromotionError, "exists"):
            prepare_promotion(self.candidate_id, output, dependencies=self.dependencies)
        terminal = json.loads(sorted((self.dependencies.promotions_root / "audit").glob("*.json"))[-1].read_text())
        self.assertEqual("prepare_rejected", terminal["event_type"])
        self.assertEqual("proposal_write_rejected", terminal["reason_code"])
        with self.assertRaisesRegex(PromotionError, "proposal output"):
            prepare_promotion(self.candidate_id, self.root / "escape.json", dependencies=self.dependencies)

    def test_candidate_unknown_entry_and_brain_link_fail_closed(self):
        (self.dependencies.candidate_root / self.candidate_id / "unknown.txt").write_text("x")
        with self.assertRaises(PromotionError):
            prepare_promotion(self.candidate_id, self.dependencies.proposals_root / "unknown.json", dependencies=self.dependencies)
        (self.dependencies.candidate_root / self.candidate_id / "unknown.txt").unlink()
        os.symlink("README.md", self.dependencies.brain_root / "alias")
        with self.assertRaises(PromotionError):
            prepare_promotion(self.candidate_id, self.dependencies.proposals_root / "linked.json", dependencies=self.dependencies)


if __name__ == "__main__":
    unittest.main()
