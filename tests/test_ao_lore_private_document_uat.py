import copy
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from ao_lore.benchmark import canonical_digest
from ao_lore.home import repository_root
from ao_lore.private_document_uat import (
    PrivateDocumentCampaignPolicy,
    PrivateDocumentPreparedRun,
    PrivateDocumentUatDependencies,
    PrivateDocumentUatError,
    private_document_policy_digest,
    private_document_state_body,
    run_private_document_uat,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


@dataclass(frozen=True)
class _Run:
    batch_manifest: dict
    batch_manifest_digest: str
    corpus_manifest: dict
    corpus_root: Path


class PrivateDocumentUatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root())
        self.runtime = Path(self.temporary.name)
        self.manifest = {
            "corpus_id": "fixture-corpus",
            "documents": [
                {"item_id": "item-01", "source_digest": DIGEST_A},
                {"item_id": "item-02", "source_digest": DIGEST_B},
            ],
        }
        self.batch = {
            "schema_version": "ao.lore.ingest-batch-manifest.v0.1",
            "batch_id": "batch-private-uat-fixture",
            "continue_on_error": True,
            "documents": [
                {
                    "item_id": "item-01",
                    "source": "sources/private/item-01.pdf",
                    "source_digest": DIGEST_A,
                },
                {
                    "item_id": "item-02",
                    "source": "sources/private/item-02.pdf",
                    "source_digest": DIGEST_B,
                },
            ],
        }
        self.policy = PrivateDocumentCampaignPolicy(
            format_id="pdf",
            corpus_id="fixture-corpus",
            media_type="application/pdf",
            extension=".pdf",
            parser_id="docling",
            parser_version="2.118.1",
            worker_module="tests.worker",
            calibration_worker_module="tests.calibration_worker",
            evidence_namespace="fixture-uat",
            validate_manifest=lambda value: value,
            validate_qualification=lambda value: value,
            validate_calibration=lambda value: value,
        )
        self.policy_digest = private_document_policy_digest(self.policy)

    def tearDown(self):
        self.temporary.cleanup()

    def _shared_run(self) -> PrivateDocumentPreparedRun:
        return PrivateDocumentPreparedRun(
            format_id="pdf",
            policy_digest=self.policy_digest,
            batch_id=self.batch["batch_id"],
            manifest=copy.deepcopy(self.manifest),
            manifest_digest=canonical_digest(self.manifest),
            manifest_identity=(3, 4),
            staging_name="private-pdf-uat-fixture",
            control_identity=(1, 2),
            source_bindings=(
                private_document_state_body(
                    self.policy,
                    item_id="item-01",
                    source_digest=DIGEST_A,
                ),
                private_document_state_body(
                    self.policy,
                    item_id="item-02",
                    source_digest=DIGEST_B,
                ),
            ),
            qualification_binding=private_document_state_body(
                self.policy,
                manifest_digest=canonical_digest(self.manifest),
                manifest_identity=[3, 4],
                qualification={"result_digest": DIGEST_C},
            ),
            expectation_binding=private_document_state_body(
                self.policy,
                manifest_digest=canonical_digest(self.manifest),
                manifest_identity=[3, 4],
                item_ids=["item-01", "item-02"],
                source_digests=[DIGEST_A, DIGEST_B],
            ),
        )

    def _dependencies(self, *, public: bool = True, **overrides):
        events = []
        processes = [object(), object(), object()]
        results = [
            {"returncode": -2, "stdout": b"", "stderr": b""},
            {"returncode": 0, "stdout": b"resume", "stderr": b""},
            {"returncode": 0, "stdout": b"resume", "stderr": b""},
        ]
        initial_checkpoint = {
            "schema_version": "ao.lore.ingest-batch-checkpoint.v0.1",
            "batch_id": self.batch["batch_id"],
            "manifest_digest": canonical_digest(self.batch),
            "manifest_items": [
                {"item_id": "item-01", "source_digest": DIGEST_A},
                {"item_id": "item-02", "source_digest": DIGEST_B},
            ],
            "items": [],
            "created": 0,
            "unchanged": 0,
            "rejected": 0,
            "processed": 0,
            "next_item_index": 0,
            "previous_checkpoint_digest": None,
            "canonical": False,
            "promotion_authority": False,
        }
        initial_checkpoint["checkpoint_digest"] = canonical_digest(initial_checkpoint)
        checkpoints = [
            {
                "schema_version": "ao.lore.ingest-batch-checkpoint.v0.1",
                "batch_id": self.batch["batch_id"],
                "manifest_digest": canonical_digest(self.batch),
                "manifest_items": copy.deepcopy(initial_checkpoint["manifest_items"]),
                "items": [
                    {
                        "item_id": "item-01",
                        "source_digest": DIGEST_A,
                        "status": "created",
                        "candidate_id": "candidate-item-01",
                        "candidate_digest": DIGEST_B,
                        "provenance_digest": DIGEST_C,
                        "review_status": "unreviewed",
                    }
                ],
                "created": 1,
                "unchanged": 0,
                "rejected": 0,
                "processed": 1,
                "next_item_index": 1,
                "previous_checkpoint_digest": initial_checkpoint["checkpoint_digest"],
                "canonical": False,
                "promotion_authority": False,
            }
        ]
        checkpoints[0]["checkpoint_digest"] = canonical_digest(checkpoints[0])
        terminal = {
            "items": [
                {
                    "item_id": "item-01",
                    "source_digest": DIGEST_A,
                    "status": "created",
                    "candidate_id": "candidate-item-01",
                    "candidate_digest": DIGEST_B,
                    "provenance_digest": DIGEST_C,
                    "review_status": "unreviewed",
                },
                {
                    "item_id": "item-02",
                    "source_digest": DIGEST_B,
                    "status": "rejected",
                    "error_code": "digest_mismatch",
                },
            ]
        }
        queue = [
            {
                "candidate_id": "candidate-item-01",
                "candidate_digest": DIGEST_B,
                "provenance_digest": DIGEST_C,
                "source_digest": DIGEST_A,
                "review_status": "unreviewed",
            }
        ]
        report = {"schema_version": "shared", "lifecycle_status": "partial"}

        def prepare(manifest, runtime):
            events.append("prepare")
            return _Run(
                batch_manifest=copy.deepcopy(self.batch),
                batch_manifest_digest=canonical_digest(self.batch),
                corpus_manifest=copy.deepcopy(manifest),
                corpus_root=runtime / "corpus",
            )

        defaults = dict(
            policy=self.policy,
            prepare_run=prepare,
            launch_process=lambda run, phase: events.append(f"launch:{phase}") or processes.pop(0),
            calibration=lambda manifest, root: events.append("calibration") or {"ok": True},
            cleanup=lambda run: events.append("cleanup") or True,
            brain_snapshot=lambda: events.append("brain") or DIGEST_A,
            candidate_snapshot=lambda: events.append("candidates") or {},
            load_manifest=lambda runtime: events.append("load") or copy.deepcopy(self.manifest),
            validate_prepared_run=lambda run, manifest, runtime: run,
            project_run=lambda run: self._shared_run(),
            conversion_snapshot=iter([0, 1, 2, 2]).__next__,
            process_poll=lambda process: None,
            inspect_checkpoint=lambda run: events.append("checkpoint") or copy.deepcopy(checkpoints[0]),
            inspect_barrier=lambda run, checkpoint: True,
            send_signal=lambda process: events.append("signal"),
            terminate_process=lambda process: events.append("terminate"),
            wait_process=lambda process: events.append("wait") or results.pop(0),
            load_final=lambda run: events.append("final") or copy.deepcopy(terminal),
            load_queue=lambda: events.append("queue") or copy.deepcopy(queue),
            verify_terminal=lambda run, stdout, final: copy.deepcopy(final),
            verify_queue=lambda values, current, before: copy.deepcopy(current["items"]),
            assemble_readback=lambda run, current, checkpoint, brain, calibration, counts: (
                events.append("assemble") or {**report, "conversion_counts": counts}
            ),
            persist_readback=lambda run, value, after_run, after_cleanup: events.append("persist") or DIGEST_C,
            require_verified_process=lambda process, phase: events.append(f"verify:{phase}"),
            expected_item_ids=lambda manifest: [item["item_id"] for item in manifest["documents"]],
            checkpoint_timeout=1.0,
            poll_interval=0.01,
        )
        defaults.update(overrides)
        dependency_type = PrivateDocumentUatDependencies if public else PrivateDocumentUatDependencies
        return events, dependency_type(**defaults)

    def test_shared_runner_preserves_interrupted_resume_rerun_order(self):
        events, dependencies = self._dependencies()
        report = run_private_document_uat(
            runtime_root=self.runtime,
            dependencies=dependencies,
        )
        self.assertEqual(
            [
                "load",
                "prepare",
                "brain",
                "candidates",
                "launch:interrupted",
                "checkpoint",
                "signal",
                "wait",
                "verify:interrupted",
                "launch:resume",
                "wait",
                "verify:resume",
                "final",
                "launch:rerun",
                "wait",
                "verify:rerun",
                "final",
                "queue",
                "calibration",
                "brain",
                "cleanup",
                "brain",
                "assemble",
                "persist",
            ],
            events,
        )
        self.assertEqual(
            {"initial": 1, "resumed": 1, "rerun": 0},
            report["conversion_counts"],
        )

    def test_shared_runner_polls_when_checkpoint_precedes_barrier(self):
        barrier_states = iter((False, True))
        events, dependencies = self._dependencies(
            inspect_barrier=lambda run, checkpoint: next(barrier_states),
            sleep=lambda interval: events.append("sleep"),
        )

        report = run_private_document_uat(
            runtime_root=self.runtime,
            dependencies=dependencies,
        )

        self.assertEqual(2, events.count("checkpoint"))
        self.assertEqual(1, events.count("sleep"))
        self.assertEqual(
            {"initial": 1, "resumed": 1, "rerun": 0},
            report["conversion_counts"],
        )

    def test_shared_runner_rejects_non_prefixed_generic_bindings(self):
        events, dependencies = self._dependencies(
            project_run=lambda run: PrivateDocumentPreparedRun(
                format_id="pdf",
                policy_digest=self.policy_digest,
                batch_id=self.batch["batch_id"],
                manifest=copy.deepcopy(self.manifest),
                manifest_digest=canonical_digest(self.manifest),
                manifest_identity=(3, 4),
                staging_name="private-pdf-uat-fixture",
                control_identity=(1, 2),
                source_bindings=({"item_id": "item-01"},),
                qualification_binding=private_document_state_body(
                    self.policy,
                    manifest_digest=canonical_digest(self.manifest),
                    manifest_identity=[3, 4],
                    qualification={"result_digest": DIGEST_C},
                ),
                expectation_binding=private_document_state_body(
                    self.policy,
                    manifest_digest=canonical_digest(self.manifest),
                    manifest_identity=[3, 4],
                    item_ids=["item-01", "item-02"],
                    source_digests=[DIGEST_A, DIGEST_B],
                ),
            )
        )
        with self.assertRaisesRegex(
            PrivateDocumentUatError, "^private document UAT failed$"
        ):
            run_private_document_uat(
                runtime_root=self.runtime,
                dependencies=dependencies,
            )
        self.assertIn("cleanup", events)

    def test_public_dependencies_support_the_full_runtime_contract(self):
        events, dependencies = self._dependencies(public=True)
        report = run_private_document_uat(
            runtime_root=self.runtime,
            dependencies=dependencies,
        )
        self.assertEqual({"initial": 1, "resumed": 1, "rerun": 0}, report["conversion_counts"])
        self.assertIn("persist", events)

    def test_state_body_rejects_reserved_overwrite_attempts(self):
        with self.assertRaisesRegex(
            PrivateDocumentUatError, "^private document UAT contract is invalid$"
        ):
            private_document_state_body(
                self.policy,
                format_id="docx",
            )
        with self.assertRaisesRegex(
            PrivateDocumentUatError, "^private document UAT contract is invalid$"
        ):
            private_document_state_body(
                self.policy,
                policy_digest=DIGEST_A,
            )

    def test_shared_runner_rejects_forged_docx_source_binding(self):
        docx_policy = PrivateDocumentCampaignPolicy(
            format_id="docx",
            corpus_id="fixture-corpus",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            extension=".docx",
            parser_id="docling",
            parser_version="2.118.1",
            worker_module="tests.worker",
            calibration_worker_module="tests.calibration_worker",
            evidence_namespace="fixture-uat",
            validate_manifest=lambda value: value,
            validate_qualification=lambda value: value,
            validate_calibration=lambda value: value,
        )
        docx_digest = private_document_policy_digest(docx_policy)
        events, dependencies = self._dependencies(
            policy=docx_policy,
            project_run=lambda run: PrivateDocumentPreparedRun(
                format_id="docx",
                policy_digest=docx_digest,
                batch_id=self.batch["batch_id"],
                manifest=copy.deepcopy(self.manifest),
                manifest_digest=canonical_digest(self.manifest),
                manifest_identity=(3, 4),
                staging_name="private-docx-uat-fixture",
                control_identity=(1, 2),
                source_bindings=(
                    private_document_state_body(
                        docx_policy,
                        item_id="item-02",
                        source_digest=DIGEST_A,
                    ),
                    private_document_state_body(
                        docx_policy,
                        item_id="item-01",
                        source_digest=DIGEST_B,
                    ),
                ),
                qualification_binding=private_document_state_body(
                    docx_policy,
                    manifest_digest=canonical_digest(self.manifest),
                    manifest_identity=[3, 4],
                    qualification={"result_digest": DIGEST_C},
                ),
                expectation_binding=private_document_state_body(
                    docx_policy,
                    manifest_digest=canonical_digest(self.manifest),
                    manifest_identity=[3, 4],
                    item_ids=["item-01", "item-02"],
                    source_digests=[DIGEST_A, DIGEST_B],
                ),
            ),
        )
        with self.assertRaisesRegex(
            PrivateDocumentUatError, "^private document UAT failed$"
        ):
            run_private_document_uat(
                runtime_root=self.runtime,
                dependencies=dependencies,
            )
        self.assertEqual([], [event for event in events if event.startswith("launch:")])

    def test_qualification_validator_runs_before_work_and_on_each_revalidation(self):
        calls = []

        def validate_qualification(value):
            calls.append(copy.deepcopy(value))
            return value

        policy = PrivateDocumentCampaignPolicy(
            format_id=self.policy.format_id,
            corpus_id=self.policy.corpus_id,
            media_type=self.policy.media_type,
            extension=self.policy.extension,
            parser_id=self.policy.parser_id,
            parser_version=self.policy.parser_version,
            worker_module=self.policy.worker_module,
            calibration_worker_module=self.policy.calibration_worker_module,
            evidence_namespace=self.policy.evidence_namespace,
            validate_manifest=self.policy.validate_manifest,
            validate_qualification=validate_qualification,
            validate_calibration=self.policy.validate_calibration,
        )
        events, dependencies = self._dependencies(policy=policy)
        run_private_document_uat(
            runtime_root=self.runtime,
            dependencies=dependencies,
        )
        self.assertEqual(5, len(calls))
        self.assertLess(events.index("prepare"), events.index("brain"))
        self.assertEqual({"result_digest": DIGEST_C}, calls[0])

    def test_qualification_validator_failure_happens_before_launch(self):
        events, dependencies = self._dependencies(
            policy=PrivateDocumentCampaignPolicy(
                format_id=self.policy.format_id,
                corpus_id=self.policy.corpus_id,
                media_type=self.policy.media_type,
                extension=self.policy.extension,
                parser_id=self.policy.parser_id,
                parser_version=self.policy.parser_version,
                worker_module=self.policy.worker_module,
                calibration_worker_module=self.policy.calibration_worker_module,
                evidence_namespace=self.policy.evidence_namespace,
                validate_manifest=self.policy.validate_manifest,
                validate_qualification=lambda value: (_ for _ in ()).throw(ValueError("boom")),
                validate_calibration=self.policy.validate_calibration,
            ),
        )
        with self.assertRaisesRegex(
            PrivateDocumentUatError, "^private document UAT failed$"
        ):
            run_private_document_uat(
                runtime_root=self.runtime,
                dependencies=dependencies,
            )
        self.assertEqual([], [event for event in events if event.startswith("launch:")])

    def test_qualification_validator_rejects_in_place_laundering_before_launch(self):
        def launder(value):
            value["result_digest"] = DIGEST_A
            return value

        events, dependencies = self._dependencies(
            policy=PrivateDocumentCampaignPolicy(
                format_id=self.policy.format_id,
                corpus_id=self.policy.corpus_id,
                media_type=self.policy.media_type,
                extension=self.policy.extension,
                parser_id=self.policy.parser_id,
                parser_version=self.policy.parser_version,
                worker_module=self.policy.worker_module,
                calibration_worker_module=self.policy.calibration_worker_module,
                evidence_namespace=self.policy.evidence_namespace,
                validate_manifest=self.policy.validate_manifest,
                validate_qualification=launder,
                validate_calibration=self.policy.validate_calibration,
            ),
        )
        with self.assertRaisesRegex(
            PrivateDocumentUatError, "^private document UAT failed$"
        ):
            run_private_document_uat(
                runtime_root=self.runtime,
                dependencies=dependencies,
            )
        self.assertEqual([], [event for event in events if event.startswith("launch:")])

    def test_manifest_validator_rejects_in_place_laundering_before_prepare(self):
        def launder(value):
            value["documents"][0]["source_digest"] = DIGEST_C
            return value

        events, dependencies = self._dependencies(
            policy=PrivateDocumentCampaignPolicy(
                format_id=self.policy.format_id,
                corpus_id=self.policy.corpus_id,
                media_type=self.policy.media_type,
                extension=self.policy.extension,
                parser_id=self.policy.parser_id,
                parser_version=self.policy.parser_version,
                worker_module=self.policy.worker_module,
                calibration_worker_module=self.policy.calibration_worker_module,
                evidence_namespace=self.policy.evidence_namespace,
                validate_manifest=launder,
                validate_qualification=self.policy.validate_qualification,
                validate_calibration=self.policy.validate_calibration,
            ),
        )
        with self.assertRaisesRegex(
            PrivateDocumentUatError, "^private document UAT failed$"
        ):
            run_private_document_uat(
                runtime_root=self.runtime,
                dependencies=dependencies,
            )
        self.assertNotIn("prepare", events)
        self.assertEqual([], [event for event in events if event.startswith("launch:")])

    def test_delayed_calibration_mutation_during_cleanup_fails_before_persist(self):
        calibration = {"aggregate": {"score": 1.0}}

        def cleanup(_run):
            calibration["aggregate"]["score"] = 0.0
            return True

        events, dependencies = self._dependencies(
            calibration=lambda manifest, root: calibration,
            cleanup=cleanup,
        )
        with self.assertRaisesRegex(
            PrivateDocumentUatError, "^private document UAT failed$"
        ):
            run_private_document_uat(
                runtime_root=self.runtime,
                dependencies=dependencies,
            )
        self.assertNotIn("persist", events)
