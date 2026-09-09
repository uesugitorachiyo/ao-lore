import fcntl
import json
import os
import shutil
import tempfile
import threading
import unittest
from copy import deepcopy
from contextlib import contextmanager
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from ao_lore.benchmark import canonical_digest
from ao_lore.candidates import (
    _CandidateReviewDependencies,
    _candidate_persistence_context_for_sanitized_rehearsal,
    _candidate_review_dependencies_for_proof,
    _append_review_with_dependencies,
    _persist_candidate_with_persistence_dependencies,
    _sealed_candidate_store_for_sanitized_rehearsal,
    _persist_candidate_with_dependencies,
    CandidateError,
    append_review,
    inspect_candidate,
    load_verified_candidate,
    persist_candidate,
    validate_candidate_document,
    validate_provenance,
)
from ao_lore.knowledge_contracts import claims_digest, citations_digest
from ao_lore.private_docx_domain import (
    DOCX_PRIVATE_CORPUS_ID,
    DOCX_TRANSFORMATION_ID,
)
from ao_lore.__main__ import main


ROOT = Path(__file__).resolve().parents[1]
TIME1 = "2026-08-09T14:00:00Z"
TIME2 = "2026-08-09T14:01:00Z"


def candidate_result():
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.1",
        "candidate_id": "candidate-0123456789abcdef",
        "document_ir_digest": "sha256:" + "1" * 64,
        "concepts": [],
        "claim_mappings": [],
        "links": [],
        "contradiction_warnings": [],
        "canonical": False,
        "promotion_authority": False,
    }
    return {
        "schema_version": "ao.lore.distillation-result.v0.1",
        "candidate": candidate,
        "candidate_digest": canonical_digest(candidate),
        "distillation_trace": {"adapter": "deterministic-distiller", "private_reasoning_persisted": False},
    }


def candidate_result_v0_2():
    claims = [{"claim_id": "claim-1", "text": "Reviewed evidence is required.", "source_block_ids": ["b1"], "citation_id": "citation-1"}]
    citations = [{"citation_id": "citation-1", "render_text": "Reviewed evidence is required.", "source_block_ids": ["b1"]}]
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.2",
        "candidate_id": "candidate-answerable-fixture",
        "document_ir_digest": "sha256:" + "1" * 64,
        "concepts": [], "claim_mappings": [{"claim_id": "claim-1", "block_ids": ["b1"]}], "links": [],
        "contradiction_warnings": [],
        "claims_schema_version": "ao.lore.canonical-claim-set.v0.1", "claims_digest": claims_digest(claims), "claims": claims,
        "citations_schema_version": "ao.lore.canonical-citation-set.v0.1", "citations_digest": citations_digest(citations), "citations": citations,
        "knowledge_policy": {"sensitivity": "public", "stale_after": None},
        "canonical": False, "promotion_authority": False,
    }
    return {"schema_version": "ao.lore.distillation-result.v0.2", "candidate": candidate, "candidate_digest": canonical_digest(candidate),
            "distillation_trace": {"adapter": "fixture", "policy_version": "v0.2", "input_block_count": 1, "candidate_concept_count": 0, "candidate_claim_count": 1, "candidate_citation_count": 1, "private_reasoning_persisted": False}}


def provenance(result):
    return {
        "schema_version": "ao.lore.candidate-provenance.v0.1",
        "candidate_id": result["candidate"]["candidate_id"],
        "candidate_digest": result["candidate_digest"],
        "document_ir_digest": result["candidate"]["document_ir_digest"],
        "source_digest": "sha256:" + "2" * 64,
        "parser_id": "docling",
        "parser_version": "2.118.1",
        "parse_quality_report_digest": "sha256:" + "3" * 64,
        "parser_selection_report_digest": "sha256:" + "4" * 64,
        "distillation_trace_digest": canonical_digest(result["distillation_trace"]),
        "created_at": TIME1,
    }


def provenance_v0_2(result):
    source_digest = "sha256:" + "2" * 64
    return {
        "schema_version": "ao.lore.candidate-provenance.v0.2",
        "candidate_id": result["candidate"]["candidate_id"],
        "candidate_digest": result["candidate_digest"],
        "document_ir_digest": result["candidate"]["document_ir_digest"],
        "source_digest": source_digest,
        "parser_id": "native-docx-ooxml",
        "parser_version": "1.0.0",
        "parse_quality_report_digest": "sha256:" + "3" * 64,
        "parser_selection_report_digest": "sha256:" + "4" * 64,
        "distillation_trace_digest": canonical_digest(result["distillation_trace"]),
        "created_at": TIME1,
        "source_origin": {
            "format_id": "docx",
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "original_digest": "sha256:" + "9" * 64,
            "derived_digest": source_digest,
            "transformation_id": DOCX_TRANSFORMATION_ID,
            "expectation_digest": "sha256:" + "a" * 64,
        },
    }


def evidence_origins():
    return [
        {
            "workspace_id": "workspace-a",
            "document_store_id": "documents-workspace-a",
            "generation_digest": "sha256:" + "1" * 64,
            "document_id": "policy-record",
            "document_ir_digest": "sha256:" + "2" * 64,
            "source_id": "source-policy-record",
            "source_digest": "sha256:" + "3" * 64,
            "evidence_kind": "document_block",
            "evidence_id": canonical_digest("document-block-a"),
            "evidence_digest": canonical_digest("document-block-a-digest"),
            "block_id": "block-a",
            "render_text": "Control record details",
            "source_span": {"page": 2, "start": 10, "end": 31},
            "authority_role": "operator_procedure",
            "sensitivity": "internal",
            "freshness_status": "current",
            "qualification_codes": ["fixture-only"],
        },
        {
            "workspace_id": "workspace-a",
            "graph_id": "graph-workspace-a",
            "graph_digest": "sha256:" + "4" * 64,
            "source_id": "source-policy",
            "source_digest": "sha256:" + "5" * 64,
            "evidence_kind": "graph_claim",
            "evidence_id": "claim-a",
            "evidence_digest": canonical_digest("graph-claim-a-digest"),
            "claim_id": "claim-a",
            "excerpt": "The policy requires a documented control.",
            "citation_anchor": "control-record-01",
            "authority_role": "operator_procedure",
            "freshness_status": "current",
            "qualification_codes": [],
            "conflict_status": "clear",
            "semantic_review": "verified",
        },
    ]


def candidate_result_v0_3():
    origins = evidence_origins()
    claims = [
        {
            "claim_id": "claim-1",
            "text": origins[0]["render_text"],
            "source_block_ids": [origins[0]["block_id"]],
            "citation_id": "citation-1",
        }
    ]
    citations = [
        {
            "citation_id": "citation-1",
            "render_text": origins[1]["excerpt"],
            "source_block_ids": [origins[0]["block_id"]],
        }
    ]
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.3",
        "candidate_id": "candidate-evidence-selection-fixture",
        "evidence_selection_digest": "sha256:" + "6" * 64,
        "evidence_origins": origins,
        "concepts": [],
        "claim_mappings": [{"claim_id": "claim-1", "block_ids": [origins[0]["block_id"]]}],
        "links": [],
        "contradiction_warnings": [],
        "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
        "claims_digest": claims_digest(claims),
        "claims": claims,
        "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
        "citations_digest": citations_digest(citations),
        "citations": citations,
        "knowledge_policy": {"sensitivity": "internal", "stale_after": None},
        "canonical": False,
        "promotion_authority": False,
    }
    return {
        "schema_version": "ao.lore.distillation-result.v0.2",
        "candidate": candidate,
        "candidate_digest": canonical_digest(candidate),
        "distillation_trace": {
            "adapter": "fixture",
            "policy_version": "v0.3",
            "input_block_count": 1,
            "candidate_concept_count": 0,
            "candidate_claim_count": 1,
            "candidate_citation_count": 1,
            "private_reasoning_persisted": False,
        },
    }


def provenance_v0_3(result):
    return {
        "schema_version": "ao.lore.candidate-provenance.v0.3",
        "candidate_id": result["candidate"]["candidate_id"],
        "candidate_digest": result["candidate_digest"],
        "proposal_id": "proposal-selection-01",
        "evidence_selection_digest": result["candidate"]["evidence_selection_digest"],
        "primary_workspace_id": "workspace-a",
        "registry_digest": "sha256:" + "8" * 64,
        "evidence_origins": deepcopy(result["candidate"]["evidence_origins"]),
        "created_at": TIME1,
    }


class StringSubclass(str):
    pass


class CandidateTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates")
        self.root = Path(self.temp.name)
        self.result = candidate_result()
        self.provenance = provenance(self.result)

    def tearDown(self):
        self.temp.cleanup()


class CandidatePersistenceTests(CandidateTestCase):
    def _sealed_run(self, name: str) -> Path:
        campaign = ROOT / ".ao-lore" / "sanitized-lifecycle-rehearsal"
        campaign.mkdir(parents=True, exist_ok=True)
        run = campaign / name
        run.mkdir()
        (run / ".ao-lore-sanitized-lifecycle-owner.json").write_bytes(
            b'{"owner":"ao-lore-sanitized-lifecycle-fixture","schema_version":"v0.1"}\n'
        )
        (run / "working" / "candidates").mkdir(parents=True)
        self.addCleanup(shutil.rmtree, run, True)
        return run

    def test_sealed_rehearsal_persistence_uses_exact_descriptor_bound_root(self):
        run = self._sealed_run("candidate-persistence-test")
        candidate_root = run / "working" / "candidates"

        with _candidate_persistence_context_for_sanitized_rehearsal(run) as dependencies:
            persisted = _persist_candidate_with_persistence_dependencies(
                self.result, self.provenance, dependencies=dependencies,
            )

        self.assertEqual("created", persisted["status"])
        self.assertTrue((candidate_root / self.result["candidate"]["candidate_id"]).is_dir())
        self.assertFalse((ROOT / "working" / "candidates" / self.result["candidate"]["candidate_id"]).exists())

    def test_sealed_rehearsal_persistence_rejects_rebound_and_hardlinked_owner(self):
        rebound_run = self._sealed_run("candidate-rebound-test")
        candidate_root = rebound_run / "working" / "candidates"
        displaced = rebound_run / "working" / "candidates-old"
        with self.assertRaisesRegex(CandidateError, "candidate persistence boundary"):
            with _candidate_persistence_context_for_sanitized_rehearsal(rebound_run) as dependencies:
                candidate_root.rename(displaced)
                candidate_root.mkdir()
                _persist_candidate_with_persistence_dependencies(
                    self.result, self.provenance, dependencies=dependencies,
                )

        hardlink_run = self._sealed_run("candidate-owner-link-test")
        marker = hardlink_run / ".ao-lore-sanitized-lifecycle-owner.json"
        extra = hardlink_run / "owner-copy.json"
        os.link(marker, extra)
        with self.assertRaisesRegex(CandidateError, "candidate persistence boundary"):
            with _candidate_persistence_context_for_sanitized_rehearsal(hardlink_run):
                pass

    def test_sealed_rehearsal_persistence_rejects_missing_symlink_and_foreign_roots(self):
        campaign = ROOT / ".ao-lore" / "sanitized-lifecycle-rehearsal"
        campaign.mkdir(parents=True, exist_ok=True)
        missing = campaign / "missing-run"
        with self.assertRaisesRegex(CandidateError, "candidate persistence boundary"):
            with _candidate_persistence_context_for_sanitized_rehearsal(missing):
                pass

        target = self._sealed_run("candidate-symlink-target")
        link = campaign / "candidate-symlink-run"
        link.symlink_to(target, target_is_directory=True)
        self.addCleanup(link.unlink, True)
        with self.assertRaisesRegex(CandidateError, "candidate persistence boundary"):
            with _candidate_persistence_context_for_sanitized_rehearsal(link):
                pass

        for forbidden in (
            ROOT / "working" / "candidates",
            ROOT / "brain",
            ROOT / ".ao-lore",
            Path("/proc"),
        ):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(
                CandidateError, "candidate persistence boundary"
            ):
                with _candidate_persistence_context_for_sanitized_rehearsal(forbidden):
                    pass

    def test_sealed_store_anchors_every_candidate_operation_during_swap_back(self):
        run = self._sealed_run("candidate-store-swap-test")
        working = run / "working"
        candidate_root = working / "candidates"
        attacker = working / "attacker"
        displaced = working / "held-candidates"
        attacker.mkdir()
        active_event = None
        observed = []

        def swap_hook(event):
            observed.append(event)
            if event == f"{active_event}.before":
                candidate_root.rename(displaced)
                attacker.rename(candidate_root)
            elif event == f"{active_event}.after":
                candidate_root.rename(attacker)
                displaced.rename(candidate_root)

        with _sealed_candidate_store_for_sanitized_rehearsal(
            run, boundary_hook=swap_hook,
        ) as store:
            active_event = "candidate-lock"
            with store.candidate_lock():
                pass
            active_event = "persist"
            persisted = store.persist(self.result, self.provenance)
            active_event = "load"
            reopened = store.load(self.result["candidate"]["candidate_id"])
            active_event = "artifacts"
            artifacts = store.artifacts(self.result["candidate"]["candidate_id"])
            active_event = "enumerate"
            identities = store.candidate_ids()
            children = store.children(self.result["candidate"]["candidate_id"])
            active_event = "inspect"
            inspection = store.inspect(self.result["candidate"]["candidate_id"])
            active_event = "append-review"
            review = store.append_review(
                self.result["candidate"]["candidate_id"],
                "accept",
                "fixture-reviewer",
                recorded_at=TIME1,
            )
            active_event = "promotion-state"
            promotion_state = store.promotion_state(
                self.result["candidate"]["candidate_id"]
            )
            store_descriptor = store._descriptor

        self.assertEqual("created", persisted["status"])
        self.assertEqual(self.result["candidate"], reopened["candidate"])
        self.assertEqual(
            {"candidate.json", "provenance.json", "reviews"}, set(artifacts),
        )
        self.assertEqual([self.result["candidate"]["candidate_id"]], identities)
        self.assertEqual(
            {"candidate.json", "provenance.json", "reviews"}, children,
        )
        self.assertEqual("unreviewed", inspection["review_status"])
        self.assertEqual(1, review["sequence"])
        self.assertEqual(self.result["candidate"], promotion_state[0])
        self.assertEqual([], list(attacker.iterdir()))
        for event in (
            "candidate-lock", "persist", "load", "artifacts", "enumerate",
            "inspect", "append-review", "promotion-state",
        ):
            self.assertIn(f"{event}.before", observed)
            self.assertIn(f"{event}.after", observed)
        with self.assertRaisesRegex(CandidateError, "closed"):
            store.candidate_ids()
        with self.assertRaises(OSError):
            os.fstat(store_descriptor)

    def test_sealed_candidate_lock_rejects_post_flock_inode_swap_without_leak(self):
        run = self._sealed_run("candidate-lock-swap-test")
        candidate_root = run / "working" / "candidates"
        lock_path = candidate_root / ".candidate.lock"
        displaced_path = candidate_root / ".candidate.lock.displaced"
        lock_path.touch(mode=0o600)

        def swap_lock(event):
            if event == "candidate-lock.after":
                lock_path.rename(displaced_path)
                lock_path.touch(mode=0o600)

        with _sealed_candidate_store_for_sanitized_rehearsal(
            run, boundary_hook=swap_lock,
        ) as store:
            store_descriptor = store._descriptor
            with self.assertRaisesRegex(CandidateError, "lock changed") as raised:
                with store.candidate_lock():
                    self.fail("swapped candidate lock was entered")
            self.assertNotIn(str(run), str(raised.exception))

            for path in (displaced_path, lock_path):
                descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)

        with self.assertRaises(OSError):
            os.fstat(store_descriptor)

    def test_sealed_candidate_lock_serializes_and_detects_replacement_on_exit(self):
        run = self._sealed_run("candidate-lock-exit-swap-test")
        candidate_root = run / "working" / "candidates"
        lock_path = candidate_root / ".candidate.lock"
        displaced_path = candidate_root / ".candidate.lock.displaced"
        attempted = threading.Event()
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def contend():
            attempted.set()
            try:
                with _sealed_candidate_store_for_sanitized_rehearsal(run) as second:
                    entered.set()
                    with second.candidate_lock():
                        release.wait(1)
            except Exception as exc:
                errors.append(exc)

        with _sealed_candidate_store_for_sanitized_rehearsal(run) as first:
            with first.candidate_lock():
                safe_thread = threading.Thread(target=contend)
                safe_thread.start()
                self.assertTrue(attempted.wait(1))
                probe = os.open(
                    run, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
                try:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                finally:
                    os.close(probe)
                self.assertFalse(entered.wait(0.05))
        self.assertTrue(entered.wait(1))
        release.set()
        safe_thread.join(1)
        self.assertFalse(safe_thread.is_alive())
        self.assertEqual([], errors)

        attempted.clear()
        entered.clear()
        release.clear()
        with self.assertRaisesRegex(CandidateError, "lock changed"):
            with _sealed_candidate_store_for_sanitized_rehearsal(run) as first:
                with first.candidate_lock():
                    lock_path.rename(displaced_path)
                    lock_path.touch(mode=0o600)
                    replaced_thread = threading.Thread(target=contend)
                    replaced_thread.start()
                    self.assertTrue(attempted.wait(1))
                    self.assertFalse(entered.wait(0.05))

        self.assertTrue(displaced_path.is_file())
        self.assertTrue(lock_path.is_file())
        self.assertTrue(entered.wait(1))
        release.set()
        replaced_thread.join(1)
        self.assertFalse(replaced_thread.is_alive())
        self.assertEqual([], errors)

        for path in (displaced_path, lock_path):
            descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def test_sealed_candidate_lock_detects_replacement_during_body_exception(self):
        class BodyFailure(RuntimeError):
            pass

        run = self._sealed_run("candidate-lock-exception-swap-test")
        candidate_root = run / "working" / "candidates"
        lock_path = candidate_root / ".candidate.lock"
        displaced_path = candidate_root / ".candidate.lock.displaced"

        with _sealed_candidate_store_for_sanitized_rehearsal(run) as store:
            proof_descriptor = store._proof_descriptor
            candidate_descriptor = store._descriptor
            with self.assertRaisesRegex(CandidateError, "lock changed") as raised:
                with store.candidate_lock():
                    lock_path.rename(displaced_path)
                    lock_path.touch(mode=0o600)
                    raise BodyFailure("protected body failed")

        self.assertIsInstance(raised.exception.__context__, BodyFailure)
        for descriptor in (proof_descriptor, candidate_descriptor):
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        with _sealed_candidate_store_for_sanitized_rehearsal(run) as reopened:
            with reopened.candidate_lock():
                pass

    def test_sealed_candidate_lock_serializes_threads_sharing_one_store(self):
        run = self._sealed_run("candidate-shared-store-lock-test")
        first_entered = threading.Event()
        second_attempted = threading.Event()
        second_entered = threading.Event()
        release_first = threading.Event()
        errors = []

        with _sealed_candidate_store_for_sanitized_rehearsal(run) as store:
            def first_worker():
                try:
                    with store.candidate_lock():
                        first_entered.set()
                        release_first.wait(1)
                except Exception as exc:
                    errors.append(exc)

            def second_worker():
                second_attempted.set()
                try:
                    with store.candidate_lock():
                        second_entered.set()
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=first_worker)
            second = threading.Thread(target=second_worker)
            first.start()
            self.assertTrue(first_entered.wait(1))
            second.start()
            self.assertTrue(second_attempted.wait(1))
            self.assertFalse(second_entered.wait(0.05))
            release_first.set()
            first.join(1)
            second.join(1)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertTrue(second_entered.is_set())
            self.assertEqual([], errors)

            with store.candidate_lock():
                with store.candidate_lock():
                    nested_entry = True
            self.assertTrue(nested_entry)

    def test_sealed_run_swap_creates_isolated_domain_and_cannot_touch_held_state(self):
        run = self._sealed_run("candidate-run-swap-test")
        displaced = run.with_name(run.name + "-held")
        replacement = run.with_name(run.name + "-replacement")
        entered = threading.Event()
        release = threading.Event()
        errors = []
        self.addCleanup(shutil.rmtree, displaced, True)
        self.addCleanup(shutil.rmtree, replacement, True)

        def initialize(selected):
            selected.mkdir()
            (selected / ".ao-lore-sanitized-lifecycle-owner.json").write_bytes(
                b'{"owner":"ao-lore-sanitized-lifecycle-fixture",'
                b'"schema_version":"v0.1"}\n'
            )
            (selected / "working" / "candidates").mkdir(parents=True)

        def contend():
            try:
                with _sealed_candidate_store_for_sanitized_rehearsal(run) as second:
                    with second.candidate_lock():
                        entered.set()
                        release.wait(1)
            except Exception as exc:
                errors.append(exc)

        with _sealed_candidate_store_for_sanitized_rehearsal(run) as first:
            with first.candidate_lock():
                run.rename(displaced)
                initialize(run)
                thread = threading.Thread(target=contend)
                thread.start()
                try:
                    self.assertTrue(entered.wait(1))
                    with self.assertRaisesRegex(
                        CandidateError, "persistence boundary changed",
                    ):
                        first.persist(self.result, self.provenance)
                    self.assertEqual(
                        {".candidate.lock"},
                        {item.name for item in displaced.joinpath(
                            "working", "candidates",
                        ).iterdir()},
                    )
                    self.assertEqual(
                        {".candidate.lock"},
                        {item.name for item in run.joinpath(
                            "working", "candidates",
                        ).iterdir()},
                    )
                finally:
                    release.set()
                    thread.join(1)
                    run.rename(replacement)
                    displaced.rename(run)

        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)

    def test_v0_2_candidate_persists_exactly_while_v0_1_remains_unchanged(self):
        result = candidate_result_v0_2()
        bound = provenance(result)
        persisted = persist_candidate(result, bound, candidate_root=self.root)
        stored = json.loads((self.root / result["candidate"]["candidate_id"] / "candidate.json").read_text())
        self.assertEqual("created", persisted["status"])
        self.assertEqual(result["candidate"], stored)
        self.assertEqual("ao.lore.okf-candidate.v0.1", self.result["candidate"]["schema_version"])

    def test_v0_2_candidate_reopens_after_persistence(self):
        result = candidate_result_v0_2()
        persist_candidate(result, provenance(result), candidate_root=self.root)

        reopened = load_verified_candidate(
            result["candidate"]["candidate_id"], candidate_root=self.root
        )

        self.assertEqual(result["candidate"], reopened["candidate"])
        self.assertEqual(result["candidate_digest"], reopened["inspection"]["candidate_digest"])

    def test_v0_3_candidate_persists_and_reopens_with_selection_bindings(self):
        result = candidate_result_v0_3()
        bound = provenance_v0_3(result)

        persisted = persist_candidate(result, bound, candidate_root=self.root)
        reopened = load_verified_candidate(
            result["candidate"]["candidate_id"], candidate_root=self.root
        )

        self.assertEqual("created", persisted["status"])
        self.assertEqual(
            result["candidate"]["evidence_selection_digest"],
            reopened["candidate"]["evidence_selection_digest"],
        )
        self.assertEqual(
            result["candidate"]["evidence_origins"],
            reopened["candidate"]["evidence_origins"],
        )

    def test_persistence_is_atomic_idempotent_and_exact(self):
        first = persist_candidate(self.result, self.provenance, candidate_root=self.root)
        second = persist_candidate(self.result, self.provenance, candidate_root=self.root)
        target = self.root / self.result["candidate"]["candidate_id"]
        self.assertEqual(first["status"], "created")
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(json.loads((target / "candidate.json").read_text()), self.result["candidate"])
        self.assertEqual(json.loads((target / "provenance.json").read_text()), self.provenance)
        self.assertTrue((target / "reviews").is_dir())

    def test_identity_digest_and_authority_drift_fail_closed(self):
        for mutation in ("unsafe_id", "digest", "canonical", "promotion"):
            with self.subTest(mutation=mutation):
                result = candidate_result()
                bound = provenance(result)
                if mutation == "unsafe_id":
                    result["candidate"]["candidate_id"] = "../escape"
                    bound["candidate_id"] = "../escape"
                    result["candidate_digest"] = canonical_digest(result["candidate"])
                    bound["candidate_digest"] = result["candidate_digest"]
                elif mutation == "digest":
                    result["candidate_digest"] = "sha256:" + "0" * 64
                    bound["candidate_digest"] = result["candidate_digest"]
                elif mutation == "canonical":
                    result["candidate"]["canonical"] = True
                    result["candidate_digest"] = canonical_digest(result["candidate"])
                    bound["candidate_digest"] = result["candidate_digest"]
                else:
                    result["candidate"]["promotion_authority"] = True
                    result["candidate_digest"] = canonical_digest(result["candidate"])
                    bound["candidate_digest"] = result["candidate_digest"]
                with self.assertRaises(CandidateError):
                    persist_candidate(result, bound, candidate_root=self.root)

    def test_conflicting_existing_content_fails_closed(self):
        persist_candidate(self.result, self.provenance, candidate_root=self.root)
        target = self.root / self.result["candidate"]["candidate_id"] / "candidate.json"
        target.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "existing candidate"):
            persist_candidate(self.result, self.provenance, candidate_root=self.root)

    def test_unknown_and_duplicate_provenance_fields_fail_closed(self):
        unknown = dict(self.provenance, unexpected=True)
        with self.assertRaises(CandidateError):
            persist_candidate(self.result, unknown, candidate_root=self.root)

        persist_candidate(self.result, self.provenance, candidate_root=self.root)
        path = self.root / self.result["candidate"]["candidate_id"] / "provenance.json"
        path.write_text('{"schema_version":"x","schema_version":"y"}\n', encoding="utf-8")
        with self.assertRaises(CandidateError):
            inspect_candidate(self.result["candidate"]["candidate_id"], candidate_root=self.root)

    def test_v0_2_provenance_requires_exact_origin_binding(self):
        bound = provenance_v0_2(self.result)
        validated = validate_provenance(
            bound, self.result["candidate"], self.result["candidate_digest"]
        )
        self.assertEqual(validated, bound)

        for label, mutate in (
            ("unknown origin field", lambda value: value["source_origin"].update(unexpected=True)),
            ("wrong format", lambda value: value["source_origin"].update(format_id="pdf")),
            ("wrong corpus", lambda value: value["source_origin"].update(corpus_id="other")),
            ("same digests", lambda value: value["source_origin"].update(original_digest=value["source_origin"]["derived_digest"])),
            ("derived drift", lambda value: value["source_origin"].update(derived_digest="sha256:" + "b" * 64)),
            ("transform drift", lambda value: value["source_origin"].update(transformation_id="other")),
            ("string subclass", lambda value: value["source_origin"].__setitem__("format_id", StringSubclass("docx"))),
        ):
            with self.subTest(label=label):
                changed = json.loads(json.dumps(bound))
                mutate(changed)
                with self.assertRaises(CandidateError):
                    validate_provenance(
                        changed,
                        self.result["candidate"],
                        self.result["candidate_digest"],
                    )

    def test_v0_3_provenance_requires_exact_selection_and_origin_binding(self):
        result = candidate_result_v0_3()
        bound = provenance_v0_3(result)
        validated = validate_provenance(
            bound, result["candidate"], result["candidate_digest"]
        )
        self.assertEqual(validated, bound)

        for label, mutate in (
            ("workspace drift", lambda value: value.update(primary_workspace_id="workspace-b")),
            ("selection drift", lambda value: value.update(evidence_selection_digest="sha256:" + "a" * 64)),
            ("origin count drift", lambda value: value["evidence_origins"].pop()),
            ("origin digest drift", lambda value: value["evidence_origins"][0].update(evidence_digest="sha256:" + "b" * 64)),
        ):
            with self.subTest(label=label):
                changed = json.loads(json.dumps(bound))
                mutate(changed)
                with self.assertRaises(CandidateError):
                    validate_provenance(
                        changed,
                        result["candidate"],
                        result["candidate_digest"],
                    )

    def test_v0_3_candidate_rejects_knowledge_payload_drift(self):
        result = candidate_result_v0_3()
        candidate = json.loads(json.dumps(result["candidate"]))

        for label, mutate in (
            (
                "claims schema version drift",
                lambda value: value.update(
                    claims_schema_version="ao.lore.canonical-claim-set.v9.9"
                ),
            ),
            (
                "claims digest drift",
                lambda value: value.update(claims_digest="sha256:" + "c" * 64),
            ),
            (
                "claim mapping anchor drift",
                lambda value: value["claim_mappings"][0].update(block_ids=["wrong-block"]),
            ),
            (
                "citation anchor drift",
                lambda value: value["citations"][0].update(source_block_ids=["wrong-block"]),
            ),
            (
                "citations schema version drift",
                lambda value: value.update(
                    citations_schema_version="ao.lore.canonical-citation-set.v9.9"
                ),
            ),
            (
                "malformed link drift",
                lambda value: value.update(
                    links=[{"source_block_id": "wrong-block", "label": "implements", "target": "wrong-target", "status": "proposed"}]
                ),
            ),
        ):
            with self.subTest(label=label):
                changed = json.loads(json.dumps(candidate))
                mutate(changed)
                with self.assertRaises(CandidateError):
                    validate_candidate_document(changed)

    def test_non_regular_stored_candidate_fails_closed(self):
        persist_candidate(self.result, self.provenance, candidate_root=self.root)
        path = self.root / self.result["candidate"]["candidate_id"] / "candidate.json"
        path.unlink()
        path.mkdir()
        with self.assertRaises(CandidateError):
            inspect_candidate(self.result["candidate"]["candidate_id"], candidate_root=self.root)

    def test_root_escape_and_symlink_fail_closed(self):
        with tempfile.TemporaryDirectory() as outside:
            with self.assertRaisesRegex(CandidateError, "candidate root"):
                persist_candidate(self.result, self.provenance, candidate_root=Path(outside))
        sibling = ROOT / "working" / "not-the-candidate-root"
        sibling.mkdir(exist_ok=True)
        try:
            with self.assertRaisesRegex(CandidateError, "candidate root"):
                persist_candidate(self.result, self.provenance, candidate_root=sibling)
        finally:
            sibling.rmdir()
        link = ROOT / "working" / "candidates" / (Path(self.temp.name).name + "-link")
        link.symlink_to(self.root, target_is_directory=True)
        try:
            with self.assertRaises(CandidateError):
                persist_candidate(self.result, self.provenance, candidate_root=link)
        finally:
            link.unlink()


class CandidateReviewTests(CandidateTestCase):
    def setUp(self):
        super().setUp()
        persist_candidate(self.result, self.provenance, candidate_root=self.root)
        self.candidate_id = self.result["candidate"]["candidate_id"]

    def test_reviews_are_append_only_chained_and_project_latest_state(self):
        first = append_review(
            self.candidate_id, "accept", "operator-one", "evidence checked",
            candidate_root=self.root, recorded_at=TIME1,
        )
        second = append_review(
            self.candidate_id, "reject", "operator-two", "conflict found",
            candidate_root=self.root, recorded_at=TIME2,
        )
        report = inspect_candidate(self.candidate_id, candidate_root=self.root)
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(second["previous_event_digest"], first["event_digest"])
        self.assertEqual(report["verified_review_events"], 2)
        self.assertEqual(report["review_status"], "rejected")
        self.assertEqual(report["latest_event_digest"], second["event_digest"])
        self.assertFalse(report["canonical"])
        self.assertFalse(report["promotion_authority"])

    def test_private_review_dependencies_use_the_injected_promotion_lock(self):
        calls = []

        @contextmanager
        def fixture_promotion_lock():
            calls.append("entered")
            try:
                yield
            finally:
                calls.append("exited")

        event = _append_review_with_dependencies(
            self.candidate_id,
            "accept",
            "fixture-reviewer",
            candidate_root=self.root,
            recorded_at=TIME1,
            dependencies=_CandidateReviewDependencies(fixture_promotion_lock),
        )

        self.assertEqual(["entered", "exited"], calls)
        self.assertEqual(
            "accepted",
            inspect_candidate(self.candidate_id, candidate_root=self.root)["review_status"],
        )
        self.assertEqual(1, event["sequence"])

    def test_private_proof_root_is_exact_disposable_layout(self):
        unrelated_inventory = tuple(sorted(path.name for path in self.root.iterdir()))
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            proof_root = Path(name)
            for child in (
                "candidate", "brain", "promotions", "workspace-registry",
                "workspace-documents",
            ):
                (proof_root / child).mkdir()
            dependencies = _candidate_review_dependencies_for_proof(proof_root)

            persisted = _persist_candidate_with_dependencies(
                self.result, self.provenance, dependencies=dependencies,
            )
            event = _append_review_with_dependencies(
                self.candidate_id,
                "accept",
                "fixture-reviewer",
                recorded_at=TIME1,
                dependencies=dependencies,
            )

            self.assertEqual("created", persisted["status"])
            self.assertEqual(1, event["sequence"])
            self.assertEqual(
                unrelated_inventory,
                tuple(sorted(path.name for path in self.root.iterdir())),
            )

    def test_private_proof_root_rejects_missing_symlink_foreign_and_protected_roots(self):
        missing = ROOT / ".ao-lore" / "missing-proof-root"
        with self.assertRaisesRegex(CandidateError, "proof root"):
            _candidate_review_dependencies_for_proof(missing)
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            target = Path(name)
            link = target.with_name(target.name + "-link")
            link.symlink_to(target, target_is_directory=True)
            try:
                with self.assertRaisesRegex(CandidateError, "proof root"):
                    _candidate_review_dependencies_for_proof(link)
            finally:
                link.unlink()
        for forbidden in (
            ROOT / ".ao-lore",
            ROOT / "working" / "candidates",
            ROOT / "brain",
            Path("/proc"),
        ):
            with self.subTest(forbidden=forbidden), self.assertRaisesRegex(
                CandidateError, "proof root"
            ):
                _candidate_review_dependencies_for_proof(forbidden)

    def test_private_proof_candidate_root_cannot_escape_its_boundary(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as name:
            proof_root = Path(name)
            for child in (
                "candidate", "brain", "promotions", "workspace-registry",
                "workspace-documents",
            ):
                (proof_root / child).mkdir()
            dependencies = _candidate_review_dependencies_for_proof(proof_root)
            with self.assertRaisesRegex(CandidateError, "candidate root"):
                _append_review_with_dependencies(
                    self.candidate_id,
                    "accept",
                    "fixture-reviewer",
                    candidate_root=ROOT / "working" / "candidates",
                    recorded_at=TIME1,
                    dependencies=dependencies,
                )

    def test_unreviewed_candidate_inspects_cleanly(self):
        report = inspect_candidate(self.candidate_id, candidate_root=self.root)
        self.assertEqual(report["review_status"], "unreviewed")
        self.assertEqual(report["verified_review_events"], 0)
        self.assertIsNone(report["latest_event_digest"])

    def test_invalid_review_input_fails_closed(self):
        for decision, reviewer, rationale, timestamp in (
            ("approve", "operator", "", TIME1),
            ("accept", "../operator", "", TIME1),
            ("accept", "operator", "x" * 1025, TIME1),
            ("accept", "operator", "", "not-a-time"),
        ):
            with self.subTest(decision=decision, reviewer=reviewer, timestamp=timestamp):
                with self.assertRaises(CandidateError):
                    append_review(
                        self.candidate_id, decision, reviewer, rationale,
                        candidate_root=self.root, recorded_at=timestamp,
                    )

    def test_event_tampering_and_duplicate_json_fail_closed(self):
        event = append_review(
            self.candidate_id, "accept", "operator-one",
            candidate_root=self.root, recorded_at=TIME1,
        )
        review_path = next((self.root / self.candidate_id / "reviews").glob("*.json"))
        body = review_path.read_text(encoding="utf-8")
        review_path.write_text(body.replace(event["event_digest"], "sha256:" + "f" * 64), encoding="utf-8")
        with self.assertRaisesRegex(CandidateError, "event digest"):
            inspect_candidate(self.candidate_id, candidate_root=self.root)
        review_path.write_text('{"schema_version":"x","schema_version":"y"}\n', encoding="utf-8")
        with self.assertRaises(CandidateError):
            inspect_candidate(self.candidate_id, candidate_root=self.root)

    def test_event_gap_rename_and_chain_drift_fail_closed(self):
        first = append_review(
            self.candidate_id, "accept", "operator-one",
            candidate_root=self.root, recorded_at=TIME1,
        )
        append_review(
            self.candidate_id, "reject", "operator-two",
            candidate_root=self.root, recorded_at=TIME2,
        )
        reviews = self.root / self.candidate_id / "reviews"
        second_path = next(path for path in reviews.glob("000002-*.json"))
        renamed = reviews / second_path.name.replace("000002-", "000003-")
        second_path.rename(renamed)
        with self.assertRaisesRegex(CandidateError, "contiguous"):
            inspect_candidate(self.candidate_id, candidate_root=self.root)
        renamed.rename(second_path)

        event = json.loads(second_path.read_text(encoding="utf-8"))
        event["previous_event_digest"] = "sha256:" + "e" * 64
        event["event_digest"] = canonical_digest(
            {key: value for key, value in event.items() if key != "event_digest"}
        )
        drifted = reviews / f"000002-{event['event_digest'][7:]}.json"
        second_path.unlink()
        drifted.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self.assertNotEqual(event["previous_event_digest"], first["event_digest"])
        with self.assertRaisesRegex(CandidateError, "previous digest"):
            inspect_candidate(self.candidate_id, candidate_root=self.root)

    def test_review_event_symlink_fails_closed(self):
        append_review(
            self.candidate_id, "accept", "operator-one",
            candidate_root=self.root, recorded_at=TIME1,
        )
        reviews = self.root / self.candidate_id / "reviews"
        path = next(reviews.glob("*.json"))
        saved = path.with_suffix(".saved")
        path.rename(saved)
        path.symlink_to(saved.name)
        with self.assertRaises(CandidateError):
            inspect_candidate(self.candidate_id, candidate_root=self.root)

    def test_candidate_digest_drift_invalidates_review_projection(self):
        append_review(
            self.candidate_id, "accept", "operator-one",
            candidate_root=self.root, recorded_at=TIME1,
        )
        candidate_path = self.root / self.candidate_id / "candidate.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate["concepts"].append({"concept_id": "changed"})
        candidate_path.write_text(json.dumps(candidate) + "\n", encoding="utf-8")
        with self.assertRaises(CandidateError):
            inspect_candidate(self.candidate_id, candidate_root=self.root)

    def test_candidate_file_symlink_fails_closed(self):
        candidate_path = self.root / self.candidate_id / "candidate.json"
        saved = candidate_path.with_suffix(".saved")
        candidate_path.rename(saved)
        candidate_path.symlink_to(saved.name)
        with self.assertRaises(CandidateError):
            inspect_candidate(self.candidate_id, candidate_root=self.root)


class CandidateCliTests(unittest.TestCase):
    def setUp(self):
        self.result = candidate_result()
        self.provenance = provenance(self.result)
        self.candidate_id = self.result["candidate"]["candidate_id"]
        self.target = ROOT / "working" / "candidates" / self.candidate_id
        shutil.rmtree(self.target, ignore_errors=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "working")
        self.result_path = Path(self.temp.name) / "result.json"
        self.provenance_path = Path(self.temp.name) / "provenance.json"
        self.result_path.write_text(json.dumps(self.result) + "\n", encoding="utf-8")
        self.provenance_path.write_text(json.dumps(self.provenance) + "\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.target, ignore_errors=True)
        self.temp.cleanup()

    def invoke(self, argv):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                status = main(argv)
            except SystemExit as exc:
                status = int(exc.code)
        return status, stdout.getvalue(), stderr.getvalue()

    def persist(self):
        return self.invoke([
            "candidate", "persist", "--result", str(self.result_path),
            "--provenance", str(self.provenance_path),
        ])

    def test_candidate_cli_persists_inspects_accepts_and_rejects(self):
        status, output, error = self.persist()
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output)["status"], "created")

        status, output, error = self.invoke([
            "candidate", "inspect", "--candidate-id", self.candidate_id,
        ])
        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output)["review_status"], "unreviewed")

        for decision, expected in (("accept", "accepted"), ("reject", "rejected")):
            status, output, error = self.invoke([
                "candidate", "review", "--candidate-id", self.candidate_id,
                "--decision", decision, "--reviewer", "operator-one",
                "--rationale", "reviewed",
            ])
            self.assertEqual((status, error), (0, ""))
            report = json.loads(output)
            self.assertEqual(report["review_status"], expected)
            self.assertFalse(report["canonical"])
            self.assertFalse(report["promotion_authority"])

    def test_candidate_cli_rejects_invalid_and_missing_candidate(self):
        status, _, error = self.invoke([
            "candidate", "review", "--candidate-id", self.candidate_id,
            "--decision", "accept", "--reviewer", "../operator",
        ])
        self.assertEqual(status, 2)
        self.assertIn("candidate operation rejected", error)

        status, _, error = self.invoke([
            "candidate", "inspect", "--candidate-id", self.candidate_id,
        ])
        self.assertEqual(status, 2)
        self.assertIn("candidate operation rejected", error)

    def test_candidate_cli_redacts_input_paths_and_has_no_root_override(self):
        private_path = Path(self.temp.name) / "private-secret-name.json"
        status, _, error = self.invoke([
            "candidate", "persist", "--result", str(private_path),
            "--provenance", str(self.provenance_path),
        ])
        self.assertEqual(status, 2)
        self.assertNotIn("private-secret-name", error)
        help_status, help_output, _ = self.invoke(["candidate", "persist", "--help"])
        self.assertEqual(help_status, 0)
        self.assertNotIn("candidate-root", help_output)



if __name__ == "__main__":
    unittest.main()
