import hashlib
import json
import os
import shutil
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from ao_lore._strict_io import ContractError
from ao_lore.benchmark import canonical_digest
from ao_lore.candidates import append_review, persist_candidate
from ao_lore.knowledge_contracts import citations_digest, claims_digest

from ao_lore.candidate_quality import (
    _CampaignDependencies,
    _verify_campaign_inputs,
    verify_all_claims,
    verify_campaign_inputs,
)


ROOT = Path(__file__).resolve().parents[1]
CANDIDATE_COUNTS = (19, 13, 73, 21, 71, 289)


def _sha256_bytes(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _source_blocks(candidate_index: int, claim_count: int) -> list[dict]:
    blocks = []
    for claim_index in range(1, claim_count + 1):
        blocks.append(
            {
                "block_id": f"b{claim_index:03d}",
                "text": f"Candidate {candidate_index:02d} evidence sentence {claim_index:03d}.",
                "block_type": "paragraph",
            }
        )
    return blocks


def _pdf_bytes(candidate_index: int) -> bytes:
    return (
        b"%PDF-1.7\n"
        + f"% synthetic candidate {candidate_index:02d}\n".encode("utf-8")
        + b"1 0 obj\n<< /Type /Catalog >>\nendobj\n"
        + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"
    )


def _candidate_result(candidate_index: int, source_digest: str, source_blocks: list[dict]) -> dict:
    candidate_id = f"candidate-{candidate_index:02d}"
    claims = []
    citations = []
    claim_mappings = []
    for claim_index, block in enumerate(source_blocks, 1):
        claim_id = f"claim-{candidate_index:02d}-{claim_index:03d}"
        citation_id = f"citation-{candidate_index:02d}-{claim_index:03d}"
        claims.append(
            {
                "claim_id": claim_id,
                "text": block["text"],
                "source_block_ids": [block["block_id"]],
                "citation_id": citation_id,
            }
        )
        citations.append(
            {
                "citation_id": citation_id,
                "render_text": block["text"],
                "source_block_ids": [block["block_id"]],
            }
        )
        claim_mappings.append({"claim_id": claim_id, "block_ids": [block["block_id"]]})
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.2",
        "candidate_id": candidate_id,
        "document_ir_digest": "sha256:" + format(candidate_index, "064x"),
        "concepts": [],
        "claim_mappings": claim_mappings,
        "links": [],
        "contradiction_warnings": [],
        "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
        "claims_digest": claims_digest(claims),
        "claims": claims,
        "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
        "citations_digest": citations_digest(citations),
        "citations": citations,
        "knowledge_policy": {"sensitivity": "public", "stale_after": None},
        "canonical": False,
        "promotion_authority": False,
    }
    return {
        "schema_version": "ao.lore.distillation-result.v0.2",
        "candidate": candidate,
        "candidate_digest": canonical_digest(candidate),
        "distillation_trace": {
            "adapter": "synthetic-public-fixture",
            "policy_version": "v0.2",
            "input_block_count": len(source_blocks),
            "candidate_concept_count": 0,
            "candidate_claim_count": len(claims),
            "candidate_citation_count": len(citations),
            "private_reasoning_persisted": False,
        },
    }


def _provenance(result: dict, source_digest: str, candidate_index: int) -> dict:
    return {
        "schema_version": "ao.lore.candidate-provenance.v0.1",
        "candidate_id": result["candidate"]["candidate_id"],
        "candidate_digest": result["candidate_digest"],
        "document_ir_digest": result["candidate"]["document_ir_digest"],
        "source_digest": source_digest,
        "parser_id": "synthetic-parser",
        "parser_version": "1.0.0",
        "parse_quality_report_digest": "sha256:" + format(candidate_index + 100, "064x"),
        "parser_selection_report_digest": "sha256:" + format(candidate_index + 200, "064x"),
        "distillation_trace_digest": canonical_digest(result["distillation_trace"]),
        "created_at": "2026-08-12T12:00:00Z",
    }


def _rebind_detached_candidate(entry: dict) -> None:
    candidate = entry["candidate"]
    candidate["claims_digest"] = claims_digest(candidate["claims"])
    candidate["citations_digest"] = citations_digest(candidate["citations"])
    candidate_digest = canonical_digest(candidate)
    entry["candidate_digest"] = candidate_digest
    entry["provenance"]["candidate_digest"] = candidate_digest
    entry["provenance_digest"] = canonical_digest(entry["provenance"])


class CandidateQualityFixture:
    def __init__(self):
        self.candidate_temp = tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates")
        self.state_temp = tempfile.TemporaryDirectory(dir=ROOT / "working")
        self.candidate_root = Path(self.candidate_temp.name)
        self.state_root = Path(self.state_temp.name)
        self.source_root = self.state_root / "synthetic-sources"
        self.source_root.mkdir()
        self.terminal_readback_path = self.state_root / "terminal-readback.json"
        self.dependencies = _CampaignDependencies(
            repository_root=ROOT,
            candidate_root=self.candidate_root,
            source_root=self.source_root,
            terminal_readback_path=self.terminal_readback_path,
        )
        self.sources: list[dict] = []
        self.results: list[dict] = []
        self.provenances: list[dict] = []
        self._seed()
        self.manifest = self._manifest()

    def cleanup(self) -> None:
        self.candidate_temp.cleanup()
        self.state_temp.cleanup()

    def _seed(self) -> None:
        for index, count in enumerate(CANDIDATE_COUNTS, 1):
            source_blocks = _source_blocks(index, count)
            body = _pdf_bytes(index)
            relpath = f"candidate-{index:02d}.pdf"
            (self.source_root / relpath).write_bytes(body)
            source_digest = _sha256_bytes(body)
            result = _candidate_result(index, source_digest, source_blocks)
            provenance = _provenance(result, source_digest, index)
            persist_candidate(result, provenance, candidate_root=self.candidate_root)
            self.sources.append(
                {
                    "candidate_id": f"candidate-{index:02d}",
                    "relpath": relpath,
                    "digest": source_digest,
                    "source_blocks": source_blocks,
                    "body": body,
                }
            )
            self.results.append(deepcopy(result))
            self.provenances.append(deepcopy(provenance))
        readback = {
            "schema_version": "ao.lore.synthetic-terminal-readback.v0.1",
            "correlation_id": "ao-lore-public-candidate-quality-review-20260812",
            "status": "ready",
            "authority_advanced": False,
        }
        self.terminal_readback_path.write_text(
            json.dumps(readback, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _manifest(self) -> dict:
        candidates = []
        for index, count in enumerate(CANDIDATE_COUNTS, 1):
            candidates.append(
                {
                    "candidate_id": f"candidate-{index:02d}",
                    "candidate_digest": self.results[index - 1]["candidate_digest"],
                    "provenance_digest": canonical_digest(self.provenances[index - 1]),
                    "source_relpath": self.sources[index - 1]["relpath"],
                    "source_digest": self.sources[index - 1]["digest"],
                    "claim_count": count,
                    "review_status": "unreviewed",
                    "latest_event_digest": None,
                }
            )
        return {
            "campaign_id": "candidate-quality-campaign-20260812",
            "correlation_id": "ao-lore-public-candidate-quality-review-20260812",
            "terminal_readback_digest": _sha256_bytes(self.terminal_readback_path.read_bytes()),
            "candidates": candidates,
        }

    def rewrite_candidate(self, candidate_index: int, mutate) -> None:
        candidate_id = f"candidate-{candidate_index:02d}"
        path = self.candidate_root / candidate_id / "candidate.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        mutate(current)
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        updated_digest = canonical_digest(current)
        self.results[candidate_index - 1]["candidate"] = deepcopy(current)
        self.results[candidate_index - 1]["candidate_digest"] = updated_digest
        provenance_path = self.candidate_root / candidate_id / "provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["candidate_digest"] = updated_digest
        provenance_path.write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self.provenances[candidate_index - 1] = deepcopy(provenance)
        self.manifest = self._manifest()


class CandidateQualityIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CandidateQualityFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_verify_campaign_inputs_accepts_the_exact_six_item_manifest(self):
        verified = _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

        self.assertEqual("candidate-quality-campaign-20260812", verified["campaign_id"])
        self.assertEqual(6, verified["candidate_count"])
        self.assertEqual(sum(CANDIDATE_COUNTS), verified["total_claim_count"])
        self.assertEqual(
            [f"candidate-{index:02d}" for index in range(1, 7)],
            [item["candidate_id"] for item in verified["candidates"]],
        )
        self.assertEqual(
            [f"candidate-{index:02d}.pdf" for index in range(1, 7)],
            [item["source_relpath"] for item in verified["candidates"]],
        )

    def test_verify_campaign_inputs_accepts_digest_bound_pdf_bytes_and_rejects_source_drift(self):
        verified = _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)
        self.assertEqual(
            self.fixture.sources[0]["body"],
            verified["candidates"][0]["source_bytes"],
        )

        source_path = self.fixture.source_root / "candidate-01.pdf"
        source_path.write_bytes(source_path.read_bytes() + b"drift")

        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_candidate_drift(self):
        path = self.fixture.candidate_root / "candidate-01" / "candidate.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        current["claims"][0]["text"] = "changed"
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_provenance_drift(self):
        path = self.fixture.candidate_root / "candidate-01" / "provenance.json"
        current = json.loads(path.read_text(encoding="utf-8"))
        current["parser_version"] = "2.0.0"
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_terminal_readback_drift(self):
        path = self.fixture.terminal_readback_path
        current = json.loads(path.read_text(encoding="utf-8"))
        current["status"] = "changed"
        path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_review_head_changes(self):
        append_review(
            "candidate-01",
            "accept",
            "reviewer-01",
            candidate_root=self.fixture.candidate_root,
            recorded_at="2026-08-12T12:05:00Z",
        )

        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_reviewed_candidate_even_with_matching_manifest(self):
        append_review(
            "candidate-01",
            "reject",
            "reviewer-01",
            candidate_root=self.fixture.candidate_root,
            recorded_at="2026-08-12T12:05:00Z",
        )
        manifest = deepcopy(self.fixture.manifest)
        review_name = next(
            (self.fixture.candidate_root / "candidate-01" / "reviews").iterdir()
        ).name
        manifest["candidates"][0]["review_status"] = "rejected"
        manifest["candidates"][0]["latest_event_digest"] = "sha256:" + review_name[7:-5]

        with self.assertRaises(ContractError):
            _verify_campaign_inputs(manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_non_public_policy(self):
        self.fixture.rewrite_candidate(
            1,
            lambda candidate: candidate["knowledge_policy"].update(sensitivity="internal"),
        )

        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_authority_widening(self):
        self.fixture.rewrite_candidate(
            1,
            lambda candidate: candidate.__setitem__("promotion_authority", True),
        )

        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_symlink_hardlink_fifo_unknown_files_and_duplicate_json(self):
        candidate_path = self.fixture.candidate_root / "candidate-01" / "candidate.json"
        candidate_copy = self.fixture.state_root / "candidate-copy.json"
        candidate_copy.write_bytes(candidate_path.read_bytes())
        candidate_path.unlink()
        os.symlink(candidate_copy, candidate_path)
        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)
        candidate_path.unlink()
        candidate_path.write_bytes(candidate_copy.read_bytes())

        provenance_path = self.fixture.candidate_root / "candidate-01" / "provenance.json"
        provenance_copy = self.fixture.state_root / "provenance-copy.json"
        provenance_copy.write_bytes(provenance_path.read_bytes())
        provenance_path.unlink()
        os.link(provenance_copy, provenance_path)
        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)
        provenance_path.unlink()
        provenance_path.write_bytes(provenance_copy.read_bytes())

        source_path = self.fixture.source_root / "candidate-01.pdf"
        source_path.unlink()
        os.mkfifo(source_path)
        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)
        source_path.unlink()
        source_path.write_bytes(self.fixture.sources[0]["body"])

        (self.fixture.source_root / "unexpected.txt").write_text("extra", encoding="utf-8")
        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)
        (self.fixture.source_root / "unexpected.txt").unlink()

        self.fixture.terminal_readback_path.write_text(
            '{"schema_version":"x","schema_version":"y"}\n',
            encoding="utf-8",
        )
        with self.assertRaises(ContractError):
            _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def test_verify_campaign_inputs_rejects_directory_replacement(self):
        detached = self.fixture.state_root / "detached-candidates"
        replacement = self.fixture.state_root / "replacement-candidates"
        shutil.copytree(self.fixture.candidate_root, replacement)
        original_hold = __import__("ao_lore.candidate_quality", fromlist=["_hold_directory"])._hold_directory
        swapped = False

        def hold_then_replace(path, root, label):
            nonlocal swapped
            held = original_hold(path, root, label)
            if Path(path) == self.fixture.candidate_root and not swapped:
                os.rename(self.fixture.candidate_root, detached)
                os.rename(replacement, self.fixture.candidate_root)
                swapped = True
            return held

        try:
            with patch("ao_lore.candidate_quality._hold_directory", side_effect=hold_then_replace):
                with self.assertRaises(ContractError):
                    _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)
        finally:
            if swapped:
                os.rename(self.fixture.candidate_root, replacement)
                os.rename(detached, self.fixture.candidate_root)
            shutil.rmtree(replacement, ignore_errors=True)

    def test_public_api_no_longer_accepts_arbitrary_dependency_overrides(self):
        with self.assertRaises(TypeError):
            verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)


class CandidateQualityClaimVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CandidateQualityFixture()
        self.intake = _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_verify_all_claims_returns_stable_exact_records(self):
        verified = verify_all_claims(self.intake)
        rerun = verify_all_claims(self.intake)

        self.assertEqual(verified, rerun)
        self.assertEqual(486, verified["total_verified_claim_count"])
        self.assertEqual(486, verified["total_verified_citation_count"])
        self.assertEqual(
            [f"candidate-{index:02d}" for index in range(1, 7)],
            [item["candidate_id"] for item in verified["candidate_results"]],
        )
        first = verified["candidate_results"][0]["claim_records"][0]
        self.assertIn("verified_result_digest", verified["candidate_results"][0])
        self.assertEqual("Candidate 01 evidence sentence 001.", first["claim_text"])
        self.assertEqual("Candidate 01 evidence sentence 001.", first["citation_text"])
        self.assertEqual(["b001"], first["source_block_ids"])
        self.assertFalse(first["duplicate_suspect"])
        self.assertFalse(first["normalized_duplicate_suspect"])
        self.assertFalse(first["fragmentation_suspect"])

    def test_verify_all_claims_rejects_duplicate_claim_citation_and_mapping_identities(self):
        for mutation in (
            lambda value: value["candidates"][0]["candidate"]["claims"][1].__setitem__(
                "claim_id", value["candidates"][0]["candidate"]["claims"][0]["claim_id"]
            ),
            lambda value: value["candidates"][0]["candidate"]["citations"][1].__setitem__(
                "citation_id", value["candidates"][0]["candidate"]["citations"][0]["citation_id"]
            ),
            lambda value: value["candidates"][0]["candidate"]["claim_mappings"][1].__setitem__(
                "claim_id", value["candidates"][0]["candidate"]["claim_mappings"][0]["claim_id"]
            ),
        ):
            mutated = deepcopy(self.intake)
            mutation(mutated)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ContractError):
                    verify_all_claims(mutated)

    def test_verify_all_claims_rejects_claim_citation_mapping_drift(self):
        for mutation in (
            lambda value: value["candidates"][0]["candidate"]["claims"][0].__setitem__("citation_id", "citation-mismatch"),
            lambda value: value["candidates"][0]["candidate"]["citations"][0].__setitem__("render_text", "rewritten"),
            lambda value: value["candidates"][0]["candidate"]["claim_mappings"][0].__setitem__("block_ids", ["b002"]),
        ):
            mutated = deepcopy(self.intake)
            mutation(mutated)
            with self.subTest(mutation=mutation):
                with self.assertRaises(ContractError):
                    verify_all_claims(mutated)

    def test_verify_all_claims_rejects_empty_or_unbounded_evidence(self):
        mutated = deepcopy(self.intake)
        mutated["candidates"][0]["candidate"]["claims"][0]["source_block_ids"] = []
        with self.assertRaises(ContractError):
            verify_all_claims(mutated)

        mutated = deepcopy(self.intake)
        oversized = "x" * 5000
        mutated["candidates"][0]["candidate"]["claims"][0]["text"] = oversized
        mutated["candidates"][0]["candidate"]["citations"][0]["render_text"] = oversized
        with self.assertRaises(ContractError):
            verify_all_claims(mutated)

    def test_verify_all_claims_rejects_detached_candidate_tampering_without_digest_updates(self):
        mutated = deepcopy(self.intake)
        changed = "Detached tamper text."
        mutated["candidates"][0]["candidate"]["claims"][0]["text"] = changed
        mutated["candidates"][0]["candidate"]["citations"][0]["render_text"] = changed

        with self.assertRaises(ContractError):
            verify_all_claims(mutated)

    def test_verify_all_claims_rejects_detached_provenance_tampering_without_digest_updates(self):
        mutated = deepcopy(self.intake)
        mutated["candidates"][0]["provenance"]["parser_version"] = "9.9.9"

        with self.assertRaises(ContractError):
            verify_all_claims(mutated)

    def test_verify_all_claims_does_not_flag_cross_candidate_duplicates(self):
        mutated = deepcopy(self.intake)
        duplicate_text = mutated["candidates"][0]["candidate"]["claims"][0]["text"]
        mutated["candidates"][1]["candidate"]["claims"][0]["text"] = duplicate_text
        mutated["candidates"][1]["candidate"]["citations"][0]["render_text"] = duplicate_text
        _rebind_detached_candidate(mutated["candidates"][1])

        normalized = "  candidate 01 evidence sentence 001  "
        mutated["candidates"][2]["candidate"]["claims"][0]["text"] = normalized
        mutated["candidates"][2]["candidate"]["citations"][0]["render_text"] = normalized
        _rebind_detached_candidate(mutated["candidates"][2])

        verified = verify_all_claims(mutated)
        candidate_two = verified["candidate_results"][1]["claim_records"][0]
        candidate_three = verified["candidate_results"][2]["claim_records"][0]

        self.assertFalse(candidate_two["duplicate_suspect"])
        self.assertFalse(candidate_three["normalized_duplicate_suspect"])
        self.assertEqual(duplicate_text, candidate_two["claim_text"])
        self.assertEqual(normalized, candidate_three["claim_text"])

    def test_verify_all_claims_flags_only_within_candidate_duplicates(self):
        mutated = deepcopy(self.intake)
        duplicate_text = mutated["candidates"][0]["candidate"]["claims"][0]["text"]
        mutated["candidates"][0]["candidate"]["claims"][1]["text"] = duplicate_text
        mutated["candidates"][0]["candidate"]["citations"][1]["render_text"] = duplicate_text

        normalized = "  candidate 01 evidence sentence 001  "
        mutated["candidates"][0]["candidate"]["claims"][2]["text"] = normalized
        mutated["candidates"][0]["candidate"]["citations"][2]["render_text"] = normalized
        _rebind_detached_candidate(mutated["candidates"][0])

        verified = verify_all_claims(mutated)
        candidate_one = verified["candidate_results"][0]["claim_records"]

        self.assertTrue(candidate_one[0]["duplicate_suspect"])
        self.assertTrue(candidate_one[1]["duplicate_suspect"])
        self.assertTrue(candidate_one[0]["normalized_duplicate_suspect"])
        self.assertTrue(candidate_one[2]["normalized_duplicate_suspect"])

    def test_verify_all_claims_flags_fragmentation_risk_and_preserves_stable_order(self):
        mutated = deepcopy(self.intake)
        fragmented = "partial sentence without punctuation"
        mutated["candidates"][3]["candidate"]["claims"][0]["text"] = fragmented
        mutated["candidates"][3]["candidate"]["citations"][0]["render_text"] = fragmented
        _rebind_detached_candidate(mutated["candidates"][3])

        verified = verify_all_claims(mutated)

        self.assertEqual(
            [f"candidate-{index:02d}" for index in range(1, 7)],
            [item["candidate_id"] for item in verified["candidate_results"]],
        )
        self.assertEqual(
            list(range(1, CANDIDATE_COUNTS[3] + 1)),
            [
                record["claim_ordinal"]
                for record in verified["candidate_results"][3]["claim_records"]
            ],
        )
        self.assertTrue(
            verified["candidate_results"][3]["claim_records"][0]["fragmentation_suspect"]
        )


class CandidateQualityDescriptorCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = CandidateQualityFixture()

    def tearDown(self) -> None:
        self.fixture.cleanup()

    def test_verify_campaign_inputs_closes_prior_directory_holds_when_second_acquisition_fails(self):
        module = __import__("ao_lore.candidate_quality", fromlist=["_verify_campaign_inputs"])
        original_hold = module._hold_directory
        opened: list[int] = []
        closed: list[int] = []
        call_count = 0

        def hold_side_effect(path, root, label):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise ContractError("synthetic second hold failure")
            held = original_hold(path, root, label)
            opened.append(held.descriptor)
            return held

        original_close = module.os.close

        def close_side_effect(descriptor):
            closed.append(descriptor)
            return original_close(descriptor)

        with patch("ao_lore.candidate_quality._hold_directory", side_effect=hold_side_effect), patch(
            "ao_lore.candidate_quality.os.close", side_effect=close_side_effect
        ):
            with self.assertRaises(ContractError):
                _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

        self.assertEqual(1, len(opened))
        self.assertIn(opened[0], closed)
        with self.assertRaises(OSError):
            os.fstat(opened[0])

    def test_verify_campaign_inputs_closes_prior_directory_holds_when_third_acquisition_fails(self):
        module = __import__("ao_lore.candidate_quality", fromlist=["_verify_campaign_inputs"])
        original_hold = module._hold_directory
        opened: list[int] = []
        closed: list[int] = []
        call_count = 0

        def hold_side_effect(path, root, label):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise ContractError("synthetic third hold failure")
            held = original_hold(path, root, label)
            opened.append(held.descriptor)
            return held

        original_close = module.os.close

        def close_side_effect(descriptor):
            closed.append(descriptor)
            return original_close(descriptor)

        with patch("ao_lore.candidate_quality._hold_directory", side_effect=hold_side_effect), patch(
            "ao_lore.candidate_quality.os.close", side_effect=close_side_effect
        ):
            with self.assertRaises(ContractError):
                _verify_campaign_inputs(self.fixture.manifest, dependencies=self.fixture.dependencies)

        self.assertEqual(2, len(opened))
        self.assertTrue(set(opened).issubset(set(closed)))
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
