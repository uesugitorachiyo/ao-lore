import hashlib
import importlib.util
import io
import json
import os
import shutil
import signal
import socket
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ao_lore.docx_ooxml import (
    DOCX_PARSER_ID,
    DOCX_PARSER_VERSION,
    DocxLimits,
    docx_configuration_digest,
)
from ao_lore.home import repository_root
from ao_lore.benchmark import canonical_digest
from ao_lore.private_docx_domain import (
    DOCX_MIME,
    DOCX_PRIVATE_CORPUS_ID,
    DOCX_TRANSFORMATION_ID,
    DOCX_UAT_ITEM_IDS,
    prepare_private_docx_corpus,
)
from ao_lore.private_docx_uat import (
    PRIVATE_DOCX_UAT_POLICY_DIGEST,
    PrivateDocxCalibrationEvidence,
    PrivateDocxUatError,
    _validate_private_docx_calibration_aggregate,
    _validate_private_docx_calibration_evidence,
    _docx_calibration_contract,
    _docx_calibration_sandbox_spec,
    _docx_sandbox_spec,
    _inspect_private_docx_barrier,
    _launch_private_docx_worker,
    _run_private_docx_calibration_worker,
    _evaluate_private_docx_calibration,
    _CalibrationDeadline,
    _invoke_private_docx_calibration_attempt,
    cleanup_private_docx_uat_run,
    _signal_private_docx_worker,
    _wait_private_docx_worker,
    _worker_run_contract,
    prepare_private_docx_uat_run,
    run_private_docx_uat,
    derive_private_docx_tuning_decision,
    validate_private_docx_readback,
)
import ao_lore.private_docx_uat_worker as docx_worker
from tests.private_calibration import private_calibration

from tests.test_ao_lore_private_docx_domain import generator_module


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
DIGEST_E = "sha256:" + "e" * 64
DIGEST_F = "sha256:" + "f" * 64


def load_docx_operator():
    path = repository_root() / "scripts/private-docx-uat.py"
    spec = importlib.util.spec_from_file_location("private_docx_uat_operator", path)
    if spec is None or spec.loader is None:
        raise AssertionError("private DOCX operator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def operator_run_readback():
    return {
        "schema_version": "ao.lore.private-docx-uat-readback.v0.1",
        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
        "corpus_digest": DIGEST_A,
        "configuration_digest": DIGEST_B,
        "qualification_digest": DIGEST_C,
        "aggregate_digest": DIGEST_D,
        "uat_digest": DIGEST_E,
        "campaign_origin_digest": DIGEST_F,
        "lifecycle_status": "completed",
        "tuning_decision": "hold",
        "qualified": False,
        "counts": {
            "total": 4,
            "created": 4,
            "unchanged": 0,
            "rejected": 0,
            "processed": 4,
            "successful": 4,
            "candidate_queue": 4,
            "interrupted": 1,
            "resumed": 1,
            "rerun": 1,
        },
        "conversion_counts": {"initial": 1, "resumed": 3, "rerun": 0},
        "results": [
            {
                "item_id": item_id,
                "source_digest": DIGEST_A,
                "status": "created",
                "candidate_id": f"candidate-{index}",
                "candidate_digest": DIGEST_B,
                "provenance_digest": DIGEST_C,
                "review_status": "unreviewed",
            }
            for index, item_id in enumerate(DOCX_UAT_ITEM_IDS, start=1)
        ],
        "aggregate": calibration_aggregate(),
        "ocr_enabled": False,
        "ocr_used": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


class _PreparedResult:
    def __init__(self):
        expectation_digest = DIGEST_F
        self.corpus_manifest = {
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "expectation_digest": expectation_digest,
            "documents": [
                {
                    "item_id": item_id,
                    "source_digest": DIGEST_A,
                    "derived_digest": DIGEST_B,
                    "transformation_id": DOCX_TRANSFORMATION_ID,
                }
                for item_id in DOCX_UAT_ITEM_IDS
            ],
        }
        self.batch_manifest = {
            "format_id": "docx",
            "documents": [
                {"item_id": item_id} for item_id in DOCX_UAT_ITEM_IDS
            ],
        }
        self.sources = tuple(
            {
                "item_id": item_id,
                "original_digest": DIGEST_A,
                "derived_digest": DIGEST_B,
                "transformation_id": DOCX_TRANSFORMATION_ID,
            }
            for item_id in DOCX_UAT_ITEM_IDS
        )
        self.batch_manifest_digest = self._digest(self.batch_manifest)
        self.corpus_digest = self._digest(self.corpus_manifest)
        self.expectation_digest = expectation_digest
        self.staging_name = "private-docx-uat-" + "a" * 24
        self.staging_identity = (1, 2)
        self.control_identity = (3, 4)
        self.journal_identity = (5, 6)
        self.runtime_root = Path("/tmp/ao-lore-private-docx-operator")
        self.corpus_root = self.runtime_root / "private-docx" / "corpus"

    @staticmethod
    def _digest(value):
        body = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(body).hexdigest()


class _CountingStream(io.StringIO):
    def __init__(self, *, fail_write=False, fail_flush=False, base=False):
        super().__init__()
        self.fail_write = fail_write
        self.fail_flush = fail_flush
        self.base = base
        self.writes = 0
        self.flushes = 0

    def write(self, value):
        self.writes += 1
        if self.fail_write:
            if self.base:
                raise KeyboardInterrupt()
            raise OSError("private write detail")
        return super().write(value)

    def flush(self):
        self.flushes += 1
        if self.fail_flush:
            if self.base:
                raise KeyboardInterrupt()
            raise OSError("private flush detail")
        return super().flush()


class _ReturnedWriteStream(_CountingStream):
    def __init__(self, returned):
        super().__init__()
        self.returned = returned

    def write(self, value):
        self.writes += 1
        if type(self.returned) is int and self.returned > 0:
            io.StringIO.write(self, value[: self.returned])
        return self.returned


class _FailingDiagnosticStream(io.StringIO):
    def __init__(
        self, *, fail_write=True, fail_flush=False, fail_once=False, base=False
    ):
        super().__init__()
        self.fail_write = fail_write
        self.fail_flush = fail_flush
        self.fail_once = fail_once
        self.base = base
        self.writes = 0
        self.flushes = 0

    def write(self, value):
        self.writes += 1
        if self.fail_write:
            if self.fail_once:
                self.fail_write = False
            if self.base:
                raise KeyboardInterrupt("PRIVATE_CONTROL_SENTINEL")
            raise OSError("PRIVATE_EXCEPTION_SENTINEL")
        return super().write(value)

    def flush(self):
        self.flushes += 1
        if self.fail_flush:
            if self.base:
                raise KeyboardInterrupt("PRIVATE_CONTROL_SENTINEL")
            raise OSError("PRIVATE_EXCEPTION_SENTINEL")
        return super().flush()


@private_calibration
class PrivateDocxOperatorTests(unittest.TestCase):
    def setUp(self):
        self.operator = load_docx_operator()
        self.calls = []
        self.prepared_validations = 0
        prepared_type = _PreparedResult
        readback = operator_run_readback()

        def prepare():
            self.calls.append("prepare-seed")
            return prepared_type()

        def run():
            self.calls.append("run")
            return readback

        def validate_prepared(value):
            self.prepared_validations += 1
            self.assertIs(type(value), prepared_type)
            return value

        def validate(value, *, expected_item_ids):
            self.assertEqual(list(DOCX_UAT_ITEM_IDS), list(expected_item_ids))
            self.assertEqual(readback, value)
            return value

        def cleanup():
            self.calls.append("cleanup")
            return {
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "removed": True,
                "verified_documents": 4,
            }

        self.service = types.SimpleNamespace(
            DOCX_PRIVATE_CORPUS_ID=DOCX_PRIVATE_CORPUS_ID,
            DOCX_TRANSFORMATION_ID=DOCX_TRANSFORMATION_ID,
            DOCX_UAT_ITEM_IDS=DOCX_UAT_ITEM_IDS,
            PrivateDocxPreparedRun=prepared_type,
            prepare_private_docx_uat_run=prepare,
            validate_private_docx_prepared_run=validate_prepared,
            run_private_docx_uat=run,
            validate_private_docx_readback=validate,
            cleanup_private_docx_corpus=cleanup,
        )

    def test_prepare_uses_live_product_validator_before_projection(self):
        status, _stdout, _stderr, _ = self.invoke(["prepare-seed", "--json"])
        self.assertEqual(0, status)
        self.assertEqual(1, self.prepared_validations)

        rejected = types.SimpleNamespace(**vars(self.service))
        rejected.validate_private_docx_prepared_run = lambda value: (
            _ for _ in ()
        ).throw(ValueError("private state drift"))
        status, stdout, stderr, _ = self.invoke(
            ["prepare-seed", "--json"], service=rejected
        )
        self.assertEqual(2, status)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("ao-lore-private-docx-uat: rejected\n", stderr.getvalue())

    def test_live_operator_facade_exposes_product_qualification_preparation(self):
        service = self.operator._load_service()
        from ao_lore.private_docx_domain import (
            prepare_private_docx_qualification_inputs,
        )

        self.assertIs(
            prepare_private_docx_qualification_inputs,
            service.prepare_private_docx_qualification_inputs,
        )

    def test_live_cleanup_recovers_precreated_empty_corpus_skeleton(self):
        with tempfile.TemporaryDirectory(dir=repository_root() / "working") as temporary:
            runtime_root = Path(temporary) / "runtime"
            empty_corpus = runtime_root / "private-docx" / "corpus"
            empty_corpus.mkdir(parents=True)
            stdout = _CountingStream()
            stderr = _CountingStream()
            with patch.dict(os.environ, {"AO_LORE_HOME": str(runtime_root)}), patch.object(
                self.operator.sys, "stdout", stdout
            ), patch.object(self.operator.sys, "stderr", stderr):
                status = self.operator.main(["cleanup", "--json"])

            self.assertEqual(0, status)
            self.assertEqual("", stderr.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertFalse(result["removed"])
            self.assertEqual(0, result["verified_documents"])
            self.assertFalse(empty_corpus.exists())

    def invoke(self, argv, *, service=None, stdout=None, stderr=None):
        out = stdout or _CountingStream()
        err = stderr or _CountingStream()
        with patch.object(
            self.operator,
            "_load_service",
            return_value=self.service if service is None else service,
        ) as load, patch.object(self.operator.sys, "stdout", out), patch.object(
            self.operator.sys, "stderr", err
        ):
            status = self.operator.main(argv)
        return status, out, err, load

    def test_exact_actions_accept_json_before_or_after_action(self):
        for action in ("prepare-seed", "run", "cleanup"):
            for argv in (["--json", action], [action, "--json"]):
                with self.subTest(argv=argv):
                    self.calls.clear()
                    status, stdout, stderr, _ = self.invoke(argv)
                    self.assertEqual(0, status)
                    self.assertEqual([action], self.calls)
                    self.assertEqual("", stderr.getvalue())
                    projection = json.loads(stdout.getvalue())
                    self.assertEqual(action, projection["action"])
                    self.assertEqual(DOCX_PRIVATE_CORPUS_ID, projection["corpus_id"])
                    self.assertNotIn("digest", stdout.getvalue())
                    self.assertNotIn("path", stdout.getvalue())
                    self.assertFalse(projection["promotion_authority"])
                    self.assertFalse(projection["claims_authority_advance"])

    def test_success_projects_fixed_content_free_contract_and_writes_once(self):
        expected = {
            "action": "run",
            "claims_authority_advance": False,
            "conversion_counts": {"initial": 1, "rerun": 0, "resumed": 3},
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "counts": operator_run_readback()["counts"],
            "item_ids": list(DOCX_UAT_ITEM_IDS),
            "lifecycle_status": "completed",
            "network_accessed": False,
            "ocr_enabled": False,
            "ocr_used": False,
            "promotion_authority": False,
            "provider_calls": False,
            "qualified": False,
            "tuning_decision": "hold",
        }
        status, stdout, stderr, _ = self.invoke(["run", "--json"])
        self.assertEqual(0, status)
        self.assertEqual(
            json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n",
            stdout.getvalue(),
        )
        self.assertEqual(1, stdout.writes)
        self.assertEqual(1, stdout.flushes)
        self.assertEqual("", stderr.getvalue())

    def test_invalid_arguments_reject_before_lazy_import_without_echo(self):
        for argv in (
            [],
            ["unknown"],
            ["run", "--source", "secret.docx"],
            ["run\nsecret"],
            ["--json", "run", "--json"],
        ):
            with self.subTest(argv=argv):
                status, stdout, stderr, load = self.invoke(argv)
                self.assertEqual(2, status)
                self.assertEqual("", stdout.getvalue())
                self.assertEqual("ao-lore-private-docx-uat: rejected\n", stderr.getvalue())
                load.assert_not_called()
                self.assertNotIn("secret", stderr.getvalue())

    def test_hostile_or_unbounded_service_results_are_rejected(self):
        class HostileDict(dict):
            pass

        hostile = types.SimpleNamespace(**vars(self.service))
        hostile.run_private_docx_uat = lambda: HostileDict(operator_run_readback())
        deep = operator_run_readback()
        nested = deep["aggregate"]
        for _ in range(40):
            nested["extra"] = {}
            nested = nested["extra"]
        unbounded = types.SimpleNamespace(**vars(self.service))
        unbounded.run_private_docx_uat = lambda: deep
        for service in (hostile, unbounded):
            with self.subTest(service=service):
                status, stdout, stderr, _ = self.invoke(["run", "--json"], service=service)
                self.assertEqual(2, status)
                self.assertEqual("", stdout.getvalue())
                self.assertEqual("ao-lore-private-docx-uat: rejected\n", stderr.getvalue())

    def test_prepare_uses_one_budget_and_requires_every_runtime_identity_field(self):
        over_budget = types.SimpleNamespace(**vars(self.service))

        def oversized_prepare():
            result = _PreparedResult()
            result.corpus_manifest["padding"] = [
                None
                for _ in range(self.operator._MAX_RESULT_NODES // 2)
            ]
            result.batch_manifest["padding"] = [
                None
                for _ in range(self.operator._MAX_RESULT_NODES // 2)
            ]
            result.corpus_digest = result._digest(result.corpus_manifest)
            result.batch_manifest_digest = result._digest(result.batch_manifest)
            return result

        over_budget.prepare_private_docx_uat_run = oversized_prepare
        omitted = types.SimpleNamespace(**vars(self.service))

        def omitted_prepare():
            result = _PreparedResult()
            del result.journal_identity
            return result

        omitted.prepare_private_docx_uat_run = omitted_prepare
        for service in (over_budget, omitted):
            with self.subTest(service=service):
                status, stdout, stderr, _ = self.invoke(
                    ["prepare-seed", "--json"], service=service
                )
                self.assertEqual(2, status)
                self.assertEqual("", stdout.getvalue())
                self.assertEqual(
                    "ao-lore-private-docx-uat: rejected\n", stderr.getvalue()
                )

    def test_stdout_write_must_return_exact_builtin_full_length(self):
        class HostileInt(int):
            pass

        for returned in (0, 1, None, HostileInt(10_000)):
            stdout = _ReturnedWriteStream(returned)
            with self.subTest(returned=returned):
                status, _stdout, stderr, _ = self.invoke(
                    ["run", "--json"], stdout=stdout
                )
                self.assertEqual(2, status)
                self.assertEqual(1, stdout.writes)
                self.assertEqual(
                    "ao-lore-private-docx-uat: rejected\n", stderr.getvalue()
                )

    def test_ordinary_service_write_and_flush_failures_have_one_rejection(self):
        failing = types.SimpleNamespace(**vars(self.service))
        failing.run_private_docx_uat = lambda: (_ for _ in ()).throw(ValueError("secret"))
        for service, stdout in (
            (failing, _CountingStream()),
            (self.service, _CountingStream(fail_write=True)),
            (self.service, _CountingStream(fail_flush=True)),
        ):
            with self.subTest(stdout=stdout):
                status, _stdout, stderr, _ = self.invoke(
                    ["run", "--json"], service=service, stdout=stdout
                )
                self.assertEqual(2, status)
                self.assertEqual("ao-lore-private-docx-uat: rejected\n", stderr.getvalue())
                self.assertEqual(1, stderr.writes)
                self.assertEqual(1, stderr.flushes)

    def test_stderr_write_and_flush_failures_are_contained_and_neutralized(self):
        for stderr in (
            _FailingDiagnosticStream(
                fail_write=True, fail_flush=False, fail_once=True
            ),
            _FailingDiagnosticStream(fail_write=True, fail_flush=True),
            _FailingDiagnosticStream(fail_write=False, fail_flush=True),
        ):
            with self.subTest(stderr=stderr):
                status, stdout, _stderr, load = self.invoke(
                    ["unknown", "PRIVATE_ARGUMENT_SENTINEL"], stderr=stderr
                )
                self.assertEqual(2, status)
                self.assertEqual("", stdout.getvalue())
                self.assertEqual(1, stderr.writes)
                self.assertEqual(1, stderr.flushes)
                self.assertNotIn("PRIVATE", stderr.getvalue())
                load.assert_not_called()

    def test_stderr_base_exceptions_propagate(self):
        for stderr in (
            _FailingDiagnosticStream(fail_write=True, base=True),
            _FailingDiagnosticStream(
                fail_write=False, fail_flush=True, base=True
            ),
        ):
            with self.subTest(stderr=stderr), self.assertRaises(KeyboardInterrupt):
                self.invoke(["unknown"], stderr=stderr)

    def test_base_exceptions_from_service_write_or_flush_propagate(self):
        interrupted = types.SimpleNamespace(**vars(self.service))
        interrupted.run_private_docx_uat = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        cases = (
            (interrupted, _CountingStream()),
            (self.service, _CountingStream(fail_write=True, base=True)),
            (self.service, _CountingStream(fail_flush=True, base=True)),
        )
        for service, stdout in cases:
            with self.subTest(stdout=stdout), self.assertRaises(KeyboardInterrupt):
                self.invoke(["run", "--json"], service=service, stdout=stdout)


def calibration_aggregate() -> dict:
    return {
        "metrics": {
            "text_fidelity": 1.0,
            "structural_fidelity": 1.0,
            "source_location_fidelity": 1.0,
            "expected_outcome_accuracy": 1.0,
            "expected_rejection_count": 12,
            "accepted_document_count": 88,
        },
        "exclusions": [],
        "stable_failures": {
            "invalid_package": 1,
            "unsupported_active_content": 11,
            "representative_unexpected": 0,
        },
        "latency_seconds": {"min": 0.01, "mean": 0.02, "max": 0.03},
        "peak_memory_bytes": {"min": 1024, "mean": 1536.0, "max": 2048},
        "repeatability": {"runs": 2, "identical": True, "score": 1.0},
    }


@private_calibration
class PrivateDocxCalibrationTests(unittest.TestCase):
    def evidence(self, aggregate=None):
        return PrivateDocxCalibrationEvidence(
            aggregate=calibration_aggregate() if aggregate is None else aggregate,
            corpus_digest=DIGEST_A,
            expectation_digest=DIGEST_B,
            qualification_digest=DIGEST_C,
            configuration_digest=DIGEST_D,
            source_set_digest=DIGEST_E,
            derived_set_digest=DIGEST_F,
            transformation_id=DOCX_TRANSFORMATION_ID,
            sandbox_digest=DIGEST_A,
        )

    def test_calibration_aggregate_has_exact_six_key_closed_boundary(self):
        validated = _validate_private_docx_calibration_aggregate(calibration_aggregate())
        self.assertEqual(
            {
                "metrics", "exclusions", "stable_failures", "latency_seconds",
                "peak_memory_bytes", "repeatability",
            },
            set(validated),
        )
        self.assertEqual(12, validated["metrics"]["expected_rejection_count"])
        self.assertEqual(88, validated["metrics"]["accepted_document_count"])
        self.assertEqual(1, validated["stable_failures"]["invalid_package"])
        invalid = calibration_aggregate()
        invalid["unexpected_failures"] = []
        with self.assertRaises(PrivateDocxUatError):
            _validate_private_docx_calibration_aggregate(invalid)

    def test_calibration_rejects_hostile_numbers_and_closed_count_drift(self):
        class HostileFloat(float):
            pass

        for field, value in (
            ("text_fidelity", HostileFloat(1.0)),
            ("expected_rejection_count", 11),
            ("accepted_document_count", 89),
        ):
            invalid = calibration_aggregate()
            invalid["metrics"][field] = value
            with self.subTest(field=field), self.assertRaises(PrivateDocxUatError):
                _validate_private_docx_calibration_aggregate(invalid)

    def test_calibration_evidence_is_deeply_detached_and_origin_bound(self):
        aggregate = calibration_aggregate()
        evidence = _validate_private_docx_calibration_evidence(self.evidence(aggregate))
        aggregate["metrics"]["text_fidelity"] = 0.0
        self.assertEqual(1.0, evidence.aggregate["metrics"]["text_fidelity"])
        with self.assertRaises(TypeError):
            evidence.aggregate["metrics"]["text_fidelity"] = 0.0

        invalid = self.evidence()
        object.__setattr__(invalid, "transformation_id", "other")
        with self.assertRaises(PrivateDocxUatError):
            _validate_private_docx_calibration_evidence(invalid)

    def test_tuning_decision_is_exactly_hold_candidate_change_or_investigate(self):
        aggregate = calibration_aggregate()
        self.assertEqual("hold", derive_private_docx_tuning_decision(aggregate))

        weak = calibration_aggregate()
        weak["metrics"]["structural_fidelity"] = 0.49
        self.assertEqual("candidate_change", derive_private_docx_tuning_decision(weak))

        unstable = calibration_aggregate()
        unstable["repeatability"] = {"runs": 2, "identical": False, "score": 0.0}
        self.assertEqual("investigate", derive_private_docx_tuning_decision(unstable))

        failed = calibration_aggregate()
        failed["stable_failures"]["representative_unexpected"] = 1
        self.assertEqual("investigate", derive_private_docx_tuning_decision(failed))


def readback() -> dict:
    aggregate = calibration_aggregate()
    return {
        "schema_version": "ao.lore.private-docx-uat-readback.v0.1",
        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
        "corpus_digest": DIGEST_A,
        "configuration_digest": DIGEST_B,
        "qualification_digest": DIGEST_C,
        "aggregate_digest": canonical_digest(aggregate),
        "uat_digest": DIGEST_E,
        "campaign_origin_digest": DIGEST_F,
        "lifecycle_status": "partial",
        "tuning_decision": "hold",
        "qualified": False,
        "counts": {
            "total": 4,
            "created": 4,
            "unchanged": 0,
            "rejected": 0,
            "processed": 4,
            "successful": 4,
            "candidate_queue": 4,
            "interrupted": 1,
            "resumed": 1,
            "rerun": 1,
        },
        "conversion_counts": {
            "initial": 1,
            "resumed": 3,
            "rerun": 0,
        },
        "results": [
            {
                "item_id": "docx-domain-01",
                "source_digest": DIGEST_A,
                "status": "created",
                "candidate_id": "candidate-docx-domain-01",
                "candidate_digest": DIGEST_B,
                "provenance_digest": DIGEST_C,
                "review_status": "unreviewed",
            },
            {
                "item_id": "docx-domain-02",
                "source_digest": DIGEST_B,
                "status": "created",
                "candidate_id": "candidate-docx-domain-02",
                "candidate_digest": DIGEST_C,
                "provenance_digest": DIGEST_D,
                "review_status": "unreviewed",
            },
            {
                "item_id": "docx-domain-03",
                "source_digest": DIGEST_C,
                "status": "created",
                "candidate_id": "candidate-docx-domain-03",
                "candidate_digest": DIGEST_D,
                "provenance_digest": DIGEST_E,
                "review_status": "unreviewed",
            },
            {
                "item_id": "docx-domain-04",
                "source_digest": DIGEST_D,
                "status": "created",
                "candidate_id": "candidate-docx-domain-04",
                "candidate_digest": DIGEST_E,
                "provenance_digest": DIGEST_F,
                "review_status": "unreviewed",
            },
        ],
        "aggregate": aggregate,
        "ocr_enabled": False,
        "ocr_used": False,
        "network_accessed": False,
        "provider_calls": False,
        "promotion_authority": False,
        "claims_authority_advance": False,
    }


@private_calibration
class PrivateDocxPreparationTests(unittest.TestCase):
    def setUp(self):
        generator = generator_module()
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root() / "working")
        self.root = Path(self.temporary.name)
        self.runtime_root = self.root / "runtime"
        self.source_root = self.root / "reviewed"
        self.source_root.mkdir()
        self.review = {
            "schema_version": "ao.lore.private-docx-domain-review.v0.1",
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "transformation_id": DOCX_TRANSFORMATION_ID,
            "documents": [],
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        for index in range(1, 5):
            canonical = generator.build_docx_fixture(
                paragraphs=({"text": f"Docx UAT sample {index}", "style": "Heading1"},),
            )
            source = b"\x00\x00\x00\x00" + canonical[4:]
            name = f"{index:04d}-docx-nomagic.docx"
            (self.source_root / name).write_bytes(source)
            self.review["documents"].append(
                {
                    "item_id": f"docx-domain-{index:02d}",
                    "source_file": name,
                }
            )
        self.corpus_manifest = prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=self.runtime_root,
        )

    def tearDown(self):
        sources_root = repository_root() / "sources"
        staging_names: set[str] = set()
        for path in self.root.rglob("prepared-run-evidence.json"):
            try:
                staging_names.add(json.loads(path.read_text(encoding="utf-8"))["staging_name"])
            except (OSError, KeyError, json.JSONDecodeError):
                pass
        for path in self.root.rglob("preparation-intent.json"):
            try:
                staging_names.add(json.loads(path.read_text(encoding="utf-8"))["staging_name"])
            except (OSError, KeyError, json.JSONDecodeError):
                pass
        for staging_name in staging_names:
            shutil.rmtree(sources_root / staging_name, ignore_errors=True)
            control = sources_root / ("." + staging_name)
            shutil.rmtree(control, ignore_errors=True)
            shutil.rmtree(control.with_name(control.name + "-foreign"), ignore_errors=True)
            shutil.rmtree(
                repository_root() / "working/candidates" / staging_name,
                ignore_errors=True,
            )
        self.temporary.cleanup()

    def test_prepare_run_binds_v2_batch_manifest_and_docx_provenance(self):
        prepared = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual("docx", prepared.batch_manifest["format_id"])
        self.assertEqual(DOCX_MIME, prepared.batch_manifest["media_type"])
        self.assertEqual(DOCX_PARSER_ID, prepared.batch_manifest["parser_id"])
        self.assertEqual(DOCX_PARSER_VERSION, prepared.batch_manifest["parser_version"])
        self.assertEqual("ao.lore.ingest-batch-manifest.v0.2", prepared.batch_manifest["schema_version"])
        self.assertEqual(
            ["docx-domain-01", "docx-domain-02", "docx-domain-03", "docx-domain-04"],
            [document["item_id"] for document in prepared.batch_manifest["documents"]],
        )
        self.assertTrue(
            all(
                document["source"].startswith("sources/private-docx-uat-")
                and document["source"].endswith(".docx")
                for document in prepared.batch_manifest["documents"]
            )
        )
        self.assertEqual(
            self.corpus_manifest["expectation_digest"],
            prepared.expectation_digest,
        )
        self.assertEqual(
            [document["derived_digest"] for document in self.corpus_manifest["documents"]],
            [source["derived_digest"] for source in prepared.sources],
        )
        self.assertTrue(
            all(
                source["original_digest"] != source["derived_digest"]
                and source["transformation_id"] == DOCX_TRANSFORMATION_ID
                for source in prepared.sources
            )
        )

    def test_validate_readback_accepts_path_free_value_and_rejects_authority_drift(self):
        value = readback()
        validated = validate_private_docx_readback(
            value,
            expected_item_ids=[
                "docx-domain-01",
                "docx-domain-02",
                "docx-domain-03",
                "docx-domain-04",
            ],
        )
        value["results"][0]["item_id"] = "mutated"
        self.assertEqual("docx-domain-01", validated["results"][0]["item_id"])

        invalid = readback()
        invalid["promotion_authority"] = True
        with self.assertRaises(PrivateDocxUatError):
            validate_private_docx_readback(
                invalid,
                expected_item_ids=[
                    "docx-domain-01",
                    "docx-domain-02",
                    "docx-domain-03",
                    "docx-domain-04",
                ],
            )

        leaked = readback()
        leaked["results"][0]["source"] = "sources/private-docx-uat/run.docx"
        with self.assertRaises(PrivateDocxUatError):
            validate_private_docx_readback(
                leaked,
                expected_item_ids=[
                    "docx-domain-01",
                    "docx-domain-02",
                    "docx-domain-03",
                    "docx-domain-04",
                ],
            )

    def test_prepare_run_rejects_post_load_corpus_swap_and_preserves_replacement(self):
        import ao_lore.private_docx_uat as private_docx_uat

        real_read = private_docx_uat._read_file_at
        swapped = False
        foreign = b"PK\x03\x04foreign"
        target = self.runtime_root / "private-docx" / "corpus" / "docx-domain-04.docx"

        def swap_after_first_read(descriptor, name, maximum):
            nonlocal swapped
            body = real_read(descriptor, name, maximum)
            if name == "docx-domain-01.docx" and not swapped:
                swapped = True
                target.write_bytes(foreign)
            return body

        with patch.object(private_docx_uat, "_read_file_at", side_effect=swap_after_first_read):
            with self.assertRaisesRegex(
                PrivateDocxUatError, "private DOCX UAT contract is invalid"
            ):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual(foreign, target.read_bytes())

    def test_prepare_run_rejects_symlinked_sources_ancestor_and_preserves_external_target(self):
        import ao_lore.private_docx_uat as private_docx_uat

        fake_repo = self.root / "fake-repo"
        fake_repo.mkdir()
        external = self.root / "external-sources"
        external.mkdir()
        sentinel = external / "sentinel.txt"
        sentinel.write_text("unchanged", encoding="utf-8")
        (fake_repo / "sources").symlink_to(external, target_is_directory=True)

        with patch.object(private_docx_uat, "repository_root", return_value=fake_repo):
            with self.assertRaisesRegex(
                PrivateDocxUatError, "private DOCX UAT contract is invalid"
            ):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual("unchanged", sentinel.read_text(encoding="utf-8"))

    def test_prepare_run_is_idempotent_via_prepared_evidence(self):
        first = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        second = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual(first, second)

    def test_prepare_run_recovers_after_interrupted_control_manifest_publish(self):
        import ao_lore.private_docx_uat as private_docx_uat

        real_persist = private_docx_uat._persist_bytes_at
        interrupted = False

        def interrupt_control_publish(descriptor, name, body):
            nonlocal interrupted
            real_persist(descriptor, name, body)
            if name == "run-manifest.json" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_docx_uat, "_persist_bytes_at", side_effect=interrupt_control_publish):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        prepared = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual("docx", prepared.batch_manifest["format_id"])
        self.assertFalse((self.runtime_root / "uat" / "private-docx" / "preparation-intent.json").exists())

    def test_prepare_run_recovers_after_interrupted_prepared_evidence_persist(self):
        import ao_lore.private_docx_uat as private_docx_uat

        real_persist = private_docx_uat._persist_prepared_run_evidence
        interrupted = False

        def interrupt_evidence(prepared, runtime):
            nonlocal interrupted
            result = real_persist(prepared, runtime)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(private_docx_uat, "_persist_prepared_run_evidence", side_effect=interrupt_evidence):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        prepared = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual("docx", prepared.batch_manifest["format_id"])

    def test_prepare_run_recovers_after_interrupted_empty_journal_publish(self):
        import ao_lore.private_docx_uat as private_docx_uat

        real_persist_empty = private_docx_uat._persist_empty_file_at
        interrupted = False

        def interrupt_journal(descriptor, name):
            nonlocal interrupted
            real_persist_empty(descriptor, name)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_docx_uat, "_persist_empty_file_at", side_effect=interrupt_journal):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        prepared = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual("docx", prepared.batch_manifest["format_id"])

    def test_prepare_run_recovery_rejects_control_identity_swap_and_preserves_foreign_tree(self):
        import ao_lore.private_docx_uat as private_docx_uat

        real_persist = private_docx_uat._persist_bytes_at
        interrupted = False

        def interrupt_control_publish(descriptor, name, body):
            nonlocal interrupted
            real_persist(descriptor, name, body)
            if name == "run-manifest.json" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_docx_uat, "_persist_bytes_at", side_effect=interrupt_control_publish):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)

        intent = json.loads(
            (self.runtime_root / "uat/private-docx/preparation-intent.json").read_text(
                encoding="utf-8"
            )
        )
        control_root = repository_root() / "sources" / ("." + intent["staging_name"])
        foreign = control_root.with_name(control_root.name + "-foreign")
        if foreign.exists():
            shutil.rmtree(foreign)
        control_root.rename(foreign)
        control_root.mkdir()
        (control_root / "run-manifest.json").write_text("foreign", encoding="utf-8")
        with self.assertRaises(PrivateDocxUatError):
            prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual("foreign", (control_root / "run-manifest.json").read_text(encoding="utf-8"))

    def test_prepare_run_recovery_rejects_journal_directory_swap_and_preserves_foreign_tree(self):
        import ao_lore.private_docx_uat as private_docx_uat

        real_persist = private_docx_uat._persist_prepared_run_evidence
        interrupted = False

        def interrupt_evidence(prepared, runtime):
            nonlocal interrupted
            result = real_persist(prepared, runtime)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(private_docx_uat, "_persist_prepared_run_evidence", side_effect=interrupt_evidence):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)

        token = canonical_digest(self.corpus_manifest).removeprefix("sha256:")[:24]
        batch_id = f"batch-private-docx-uat-{token}"
        journal_root = self.runtime_root / "uat" / "private-docx" / batch_id
        foreign = self.runtime_root / "uat" / "private-docx" / f"{batch_id}-foreign"
        if foreign.exists():
            shutil.rmtree(foreign)
        journal_root.rename(foreign)
        journal_root.mkdir(parents=True)
        sentinel = journal_root / "sentinel.txt"
        sentinel.write_text("foreign", encoding="utf-8")
        with self.assertRaises(PrivateDocxUatError):
            prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual("foreign", sentinel.read_text(encoding="utf-8"))

    def test_prepare_run_recovery_rejects_journal_file_hardlink_swap_and_preserves_foreign_bytes(self):
        import ao_lore.private_docx_uat as private_docx_uat

        real_persist_empty = private_docx_uat._persist_empty_file_at
        interrupted = False

        def interrupt_journal(descriptor, name):
            nonlocal interrupted
            real_persist_empty(descriptor, name)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_docx_uat, "_persist_empty_file_at", side_effect=interrupt_journal):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)

        token = canonical_digest(self.corpus_manifest).removeprefix("sha256:")[:24]
        batch_id = f"batch-private-docx-uat-{token}"
        journal_root = self.runtime_root / "uat" / "private-docx" / batch_id
        journal = journal_root / "conversion-journal.jsonl"
        journal.unlink()
        foreign = journal_root / "foreign.bin"
        foreign.write_bytes(b"foreign")
        os.link(foreign, journal)
        with self.assertRaises(PrivateDocxUatError):
            prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual(b"foreign", journal.read_bytes())

    def test_prepare_run_rejects_extra_journal_entry_and_preserves_it(self):
        prepared = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        batch_id = prepared.batch_manifest["batch_id"]
        journal_root = self.runtime_root / "uat" / "private-docx" / batch_id
        extra = journal_root / "extra.txt"
        extra.write_text("foreign", encoding="utf-8")
        with self.assertRaises(PrivateDocxUatError):
            prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual("foreign", extra.read_text(encoding="utf-8"))

    def test_prepare_run_closes_descriptors_on_baseexception(self):
        import ao_lore.private_docx_uat as private_docx_uat

        before = len(os.listdir("/proc/self/fd"))
        with patch.object(
            private_docx_uat,
            "_persist_prepared_run_evidence",
            side_effect=BaseException,
        ):
            with self.assertRaises(BaseException):
                prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))

    def test_recovered_prepared_run_rejects_forged_source_provenance(self):
        prepared = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        evidence_path = (
            self.runtime_root / "uat/private-docx"
            / prepared.batch_manifest["batch_id"] / "prepared-run-evidence.json"
        )
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["sources"][0]["original_digest"] = DIGEST_A
        evidence_path.write_text(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with self.assertRaises(PrivateDocxUatError):
            prepare_private_docx_uat_run(runtime_root=self.runtime_root)

    def test_separate_preparations_use_isolated_staging_and_control_trees(self):
        second_runtime = self.root / "runtime-second"
        prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=second_runtime,
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(
                prepare_private_docx_uat_run, runtime_root=self.runtime_root
            )
            second_future = executor.submit(
                prepare_private_docx_uat_run, runtime_root=second_runtime
            )
            first = first_future.result()
            second = second_future.result()
        self.assertNotEqual(first.staging_name, second.staging_name)
        self.assertTrue((repository_root() / "sources" / first.staging_name).is_dir())
        self.assertTrue((repository_root() / "sources" / second.staging_name).is_dir())
        self.assertTrue((repository_root() / "sources" / ("." + first.staging_name)).is_dir())
        self.assertTrue((repository_root() / "sources" / ("." + second.staging_name)).is_dir())


@private_calibration
class PrivateDocxWorkerTests(unittest.TestCase):
    def setUp(self):
        PrivateDocxPreparationTests.setUp(self)
        self.prepared = prepare_private_docx_uat_run(runtime_root=self.runtime_root)
        qualification = {
            "schema_version": "ao.lore.docx-benchmark-result.v0.1",
            "fixture_corpus_digest": self.prepared.expectation_digest,
            "parser_id": "native-docx-ooxml",
            "parser_version": "1.0.0",
            "parser_configuration_digest": docx_configuration_digest(DocxLimits()),
            "document_ir_version": "ao.lore.document-ir.v0.1",
            "attempts_per_document": 2,
            "document_count": 100,
            "metrics": {
                "text_fidelity": 1.0,
                "structural_fidelity": 1.0,
                "source_location_fidelity": 1.0,
                "expected_outcome_accuracy": 1.0,
                "expected_rejection_count": 12,
                "accepted_document_count": 88,
            },
            "exclusions": [],
            "stable_failures": {"invalid_package": 1, "unsupported_active_content": 11},
            "repeatability": {"runs": 2, "identical": True, "score": 1.0},
            "unexpected_failures": [],
            "decision": "hold",
        }
        qualification["result_digest"] = canonical_digest(qualification)
        self.qualification = qualification
        qualification_path = self.runtime_root / "private-docx" / "qualification.json"
        qualification_path.write_text(
            json.dumps(qualification, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def tearDown(self):
        PrivateDocxPreparationTests.tearDown(self)

    def test_worker_contract_binds_policy_qualification_expectation_and_sources(self):
        contract = _worker_run_contract(
            self.prepared,
            "interrupted",
            qualification=self.qualification,
            sandbox={"proof": DIGEST_A},
            runtime_root=self.runtime_root,
        )
        self.assertEqual("docx", contract["format_id"])
        self.assertEqual(PRIVATE_DOCX_UAT_POLICY_DIGEST, contract["policy_digest"])
        self.assertEqual("interrupted", contract["phase"])
        self.assertEqual(
            (0, 1, 1),
            (
                contract["starting_processed"],
                contract["terminal_processed"],
                contract["conversion_delta"],
            ),
        )
        self.assertEqual(self.prepared.batch_manifest_digest, contract["batch_manifest_digest"])
        self.assertEqual(canonical_digest(self.qualification), contract["qualification_digest"])
        self.assertEqual(self.prepared.expectation_digest, contract["expectation_digest"])
        self.assertEqual(
            {"device": self.prepared.journal_identity[0], "inode": self.prepared.journal_identity[1]},
            contract["journal_identity"],
        )
        self.assertEqual(
            [
                {
                    "item_id": source["item_id"],
                    "derived_digest": source["derived_digest"],
                    "original_digest": source["original_digest"],
                    "transformation_id": source["transformation_id"],
                }
                for source in self.prepared.sources
            ],
            contract["sources"],
        )
        self.assertEqual(
            canonical_digest({key: value for key, value in contract.items() if key != "run_digest"}),
            contract["run_digest"],
        )

    def test_calibration_contract_binds_exact_origin_and_dedicated_sandbox(self):
        sandbox = _docx_calibration_sandbox_spec(
            self.prepared,
            bwrap_version="bubblewrap 0.9.0",
            runtime_root=self.runtime_root,
        )
        mounts = {mount["name"]: mount for mount in sandbox["mounts"]}
        self.assertEqual("read-only", mounts["staged-sources"]["mode"])
        self.assertEqual("read-only", mounts["qualification"]["mode"])
        self.assertEqual("read-write", mounts["run-state"]["mode"])
        self.assertNotIn("candidate-state", mounts)
        self.assertNotIn("batch-state", mounts)
        self.assertEqual(
            [__import__("sys").executable, "-m", "ao_lore.private_docx_calibration_worker"],
            sandbox["argv"][-3:],
        )
        contract = _docx_calibration_contract(
            self.prepared,
            self.qualification,
            sandbox,
            runtime_root=self.runtime_root,
        )
        self.assertEqual("calibration", contract["phase"])
        self.assertEqual(self.prepared.corpus_digest, contract["corpus_digest"])
        self.assertEqual(self.prepared.expectation_digest, contract["expectation_digest"])
        self.assertEqual(
            ["docx-domain-01", "docx-domain-02", "docx-domain-03", "docx-domain-04"],
            [item["item_id"] for item in contract["documents"]],
        )
        self.assertTrue(all(
            item["expectation"]["expected_outcome"] == "accept"
            for item in contract["documents"]
        ))
        self.assertTrue(all(
            set(item["expectation"]) == {
                "package_inventory_digest", "normalized_text_digest",
                "structural_event_digest", "counts", "expected_outcome",
                "expected_rejection", "page_count", "page_count_reason",
            }
            for item in contract["documents"]
        ))
        self.assertEqual(
            canonical_digest({key: value for key, value in contract.items() if key != "run_digest"}),
            contract["run_digest"],
        )

    def test_actual_calibration_worker_runs_twice_and_reclaims_owned_artifacts(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        evidence = _run_private_docx_calibration_worker(
            self.prepared,
            self.qualification,
            runtime_root=self.runtime_root,
        )
        self.assertEqual("hold", derive_private_docx_tuning_decision(evidence.aggregate))
        self.assertEqual(2, evidence.aggregate["repeatability"]["runs"])
        state = self.runtime_root / "uat/private-docx" / self.prepared.batch_manifest["batch_id"]
        self.assertFalse((state / "calibration-run-contract.json").exists())
        self.assertFalse((state / "calibration-result.json").exists())

    def _calibration_unit_inputs(self):
        sandbox = _docx_calibration_sandbox_spec(
            self.prepared,
            bwrap_version="bubblewrap 0.9.0",
            runtime_root=self.runtime_root,
        )
        contract = _docx_calibration_contract(
            self.prepared,
            self.qualification,
            sandbox,
            runtime_root=self.runtime_root,
        )
        held = [
            (document, (repository_root() / document["locator"]).read_bytes())
            for document in contract["documents"]
        ]
        from ao_lore.docx_ooxml import NativeDocxOoxmlAdapter

        adapter = NativeDocxOoxmlAdapter(
            self.qualification,
            expected_corpus_digest=self.prepared.expectation_digest,
        )
        return contract, held, adapter

    def test_calibration_scores_all_eight_attempts_and_candidate_change_is_reachable(self):
        from ao_lore.parsing import ParseOutput

        contract, held, native = self._calibration_unit_inputs()

        class WeakAdapter:
            capability = native.capability

            def parse(self, source):
                output = native.parse(source)
                output.document_ir["blocks"][0]["text"] = "repeatable weakness"
                return ParseOutput(
                    output.document_ir,
                    output.quality_components,
                    output.critical_failures,
                )

        aggregate = _evaluate_private_docx_calibration(
            WeakAdapter(), contract, held, lambda adapter, source: (adapter.parse(source), 0.1, 10)
        )
        self.assertEqual(0.0, aggregate["metrics"]["text_fidelity"])
        self.assertEqual(1.0, aggregate["metrics"]["expected_outcome_accuracy"])
        self.assertEqual("candidate_change", derive_private_docx_tuning_decision(aggregate))

    def test_calibration_rejects_reused_direct_and_delayed_mutation(self):
        contract, held, native = self._calibration_unit_inputs()

        first = native.parse({
            "resource": f"private-docx/{held[0][0]['item_id']}.docx",
            "digest": held[0][0]["derived_digest"],
            "media_type": DOCX_MIME,
            "data": held[0][1],
        })

        class Reused:
            capability = native.capability

            def parse(self, _source):
                return first

        with self.assertRaisesRegex(OSError, "reused"):
            _evaluate_private_docx_calibration(
                Reused(), contract, held,
                lambda adapter, source: (adapter.parse(source), 0.1, 10),
            )

        class SourceMutator:
            capability = native.capability

            def parse(self, source):
                result = native.parse(source)
                source["digest"] = DIGEST_A
                return result

        with self.assertRaisesRegex(OSError, "source was mutated"):
            _evaluate_private_docx_calibration(
                SourceMutator(), contract, held,
                lambda adapter, source: (adapter.parse(source), 0.1, 10),
            )

        retained = []

        def delayed(adapter, source):
            if retained:
                retained[0].document_ir["blocks"][0]["text"] = "late mutation"
            result = adapter.parse(source)
            retained.append(result)
            return result, 0.1, 10

        with self.assertRaisesRegex(OSError, "after return"):
            _evaluate_private_docx_calibration(native, contract, held, delayed)

    def test_calibration_unexpected_active_content_investigates_and_deadline_is_uncatchable_by_exception(self):
        contract, held, native = self._calibration_unit_inputs()

        class UnexpectedActive:
            capability = native.capability

            def parse(self, _source):
                raise RuntimeError("unsupported active content")

        aggregate = _evaluate_private_docx_calibration(
            UnexpectedActive(), contract, held,
            lambda adapter, source: (adapter.parse(source), 0.1, 10),
        )
        self.assertEqual(8, aggregate["stable_failures"]["representative_unexpected"])
        self.assertEqual("investigate", derive_private_docx_tuning_decision(aggregate))
        self.assertTrue(issubclass(_CalibrationDeadline, BaseException))
        self.assertFalse(issubclass(_CalibrationDeadline, Exception))

        source = {
            "resource": f"private-docx/{held[0][0]['item_id']}.docx",
            "digest": held[0][0]["derived_digest"],
            "media_type": DOCX_MIME,
            "data": held[0][1],
        }
        events = []

        class CatchAndForge:
            def parse(self, selected):
                try:
                    os.kill(os.getpid(), signal.SIGALRM)
                except BaseException:
                    return native.parse(selected)

        def alarm(_signum, _frame):
            events.append(1.0)
            raise _CalibrationDeadline()

        previous = signal.signal(signal.SIGALRM, alarm)
        try:
            with self.assertRaisesRegex(OSError, "suppressed"):
                _invoke_private_docx_calibration_attempt(
                    CatchAndForge(), source, 1.0, events
                )
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            signal.signal(signal.SIGALRM, previous)

    def test_calibration_rejects_mutated_quality_and_critical_failures(self):
        from ao_lore.parsing import ParseOutput

        contract, held, native = self._calibration_unit_inputs()

        class MutatedOutput:
            capability = native.capability

            def __init__(self, critical):
                self.critical = critical

            def parse(self, source):
                output = native.parse(source)
                quality = dict(output.quality_components)
                quality["text_coverage"] = 0.0
                return ParseOutput(
                    output.document_ir,
                    quality,
                    ("forged",) if self.critical else output.critical_failures,
                )

        for critical in (False, True):
            with self.subTest(critical=critical), self.assertRaisesRegex(
                OSError, "output contract"
            ):
                _evaluate_private_docx_calibration(
                    MutatedOutput(critical), contract, held,
                    lambda adapter, source: (adapter.parse(source), 0.1, 10),
                )

    def test_calibration_measurement_requires_one_exact_operation_result(self):
        contract, held, native = self._calibration_unit_inputs()
        source = {
            "resource": f"private-docx/{held[0][0]['item_id']}.docx",
            "digest": held[0][0]["derived_digest"],
            "media_type": DOCX_MIME,
            "data": held[0][1],
        }
        precomputed = native.parse(source)

        def no_call(_adapter, _source):
            return precomputed, 0.1, 10

        def double_call(adapter, selected):
            first = adapter.parse(selected)
            try:
                adapter.parse(selected)
            except OSError:
                pass
            return first, 0.1, 10

        def replacement(adapter, selected):
            adapter.parse(selected)
            return native.parse(selected), 0.1, 10

        for label, measurement in (
            ("no-call", no_call),
            ("double-call", double_call),
            ("replacement", replacement),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(
                OSError, "changed operation semantics"
            ):
                _evaluate_private_docx_calibration(
                    native, contract, held, measurement
                )

    def test_calibration_measurement_rejects_caught_exception_and_forged_result(self):
        contract, held, native = self._calibration_unit_inputs()
        source = {
            "resource": f"private-docx/{held[0][0]['item_id']}.docx",
            "digest": held[0][0]["derived_digest"],
            "media_type": DOCX_MIME,
            "data": held[0][1],
        }
        forged = native.parse(source)

        class FailingAdapter:
            capability = native.capability

            def parse(self, _source):
                raise RuntimeError("operation failure")

        def catch_and_forge(adapter, selected):
            try:
                adapter.parse(selected)
            except RuntimeError:
                return forged, 0.1, 10

        with self.assertRaisesRegex(OSError, "changed operation semantics"):
            _evaluate_private_docx_calibration(
                FailingAdapter(), contract, held, catch_and_forge
            )

    def test_calibration_artifact_reclaim_preserves_identical_replacement_inode(self):
        module = __import__("ao_lore.private_docx_uat", fromlist=["os"])
        state = self.runtime_root / "artifact-race"
        state.mkdir()
        descriptor = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            body = b'{"owned":true}\n'
            owned = module._publish_calibration_artifact_at(
                descriptor, "calibration-result.json", body
            )
            original_rename = module._rename_noreplace_at
            swapped = False
            held_descriptors = []

            def swap_before_rename(source_fd, source, destination_fd, destination):
                nonlocal swapped
                if not swapped and source == "calibration-result.json":
                    swapped = True
                    held_descriptors.append(os.open(source, os.O_RDONLY, dir_fd=source_fd))
                    os.unlink(source, dir_fd=source_fd)
                    replacement = os.open(
                        source,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=source_fd,
                    )
                    try:
                        os.write(replacement, body)
                    finally:
                        os.close(replacement)
                return original_rename(
                    source_fd, source, destination_fd, destination
                )

            with patch.object(module, "_rename_noreplace_at", side_effect=swap_before_rename):
                self.assertFalse(
                    module._unlink_owned_calibration_artifact_at(descriptor, owned)
                )
            quarantines = list(state.glob(".private-docx-calibration-reclaim-*"))
            self.assertEqual(1, len(quarantines))
            self.assertEqual(body, quarantines[0].read_bytes())
            self.assertNotEqual(owned.identity[1], quarantines[0].stat().st_ino)
            for held_descriptor in held_descriptors:
                os.close(held_descriptor)
        finally:
            os.close(descriptor)

    def test_verified_cleanup_removes_only_owned_runtime_and_staging_trees(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        rerun = _launch_private_docx_worker(
            self.prepared, "rerun", qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        result = _wait_private_docx_worker(rerun)
        self.assertEqual(0, result["returncode"], result["stderr"])
        self.assertTrue(cleanup_private_docx_uat_run(self.prepared))
        for _kind, path in __import__("ao_lore.private_docx_uat", fromlist=["_cleanup_target_paths"])._cleanup_target_paths(self.prepared):
            self.assertFalse(path.exists(), path)
        self.assertFalse((self.runtime_root / "private-docx/corpus").exists())

    def test_full_campaign_publishes_two_file_evidence_only_after_cleanup(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        report = run_private_docx_uat(runtime_root=self.runtime_root)
        self.assertEqual("completed", report["lifecycle_status"])
        self.assertEqual("hold", report["tuning_decision"])
        self.assertEqual({"initial": 1, "resumed": 3, "rerun": 0}, report["conversion_counts"])
        evidence = (
            self.runtime_root / "evidence/private-docx-uat"
            / self.prepared.batch_manifest["batch_id"]
        )
        self.assertEqual(
            {"private-docx-uat-readback.json", "cleaned-state.json"},
            {path.name for path in evidence.iterdir()},
        )
        cleaned = json.loads((evidence / "cleaned-state.json").read_text(encoding="utf-8"))
        self.assertEqual(
            cleaned["brain_before_digest"],
            cleaned["brain_after_work_digest"],
        )
        self.assertEqual(
            cleaned["brain_after_work_digest"],
            cleaned["brain_after_cleanup_digest"],
        )
        self.assertFalse(any(
            path.name.startswith(".pending-")
            for path in evidence.parent.iterdir()
        ))
        for _kind, path in __import__("ao_lore.private_docx_uat", fromlist=["_cleanup_target_paths"])._cleanup_target_paths(self.prepared):
            self.assertFalse(path.exists(), path)

    def test_cleanup_recovers_interruption_after_first_quarantine_rename(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        rerun = _launch_private_docx_worker(
            self.prepared, "rerun", qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(0, _wait_private_docx_worker(rerun)["returncode"])
        module = __import__("ao_lore.private_docx_uat", fromlist=["_persist_uat_document"])
        original = module._persist_uat_document
        interrupted = False

        def stop_after_rename(directory, runtime, name, value):
            nonlocal interrupted
            if (
                name.startswith("cleanup-")
                and not interrupted
                and any(target.get("phase") == "quarantined" for target in value.get("targets", []))
            ):
                interrupted = True
                raise KeyboardInterrupt()
            return original(directory, runtime, name, value)

        with patch.object(module, "_persist_uat_document", side_effect=stop_after_rename):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_docx_uat_run(self.prepared)
        self.assertTrue(interrupted)
        self.assertTrue(cleanup_private_docx_uat_run(self.prepared))
        for _kind, path in module._cleanup_target_paths(self.prepared):
            self.assertFalse(path.exists(), path)

    def test_cleanup_rejects_unknown_entry_without_deleting_it(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        rerun = _launch_private_docx_worker(
            self.prepared, "rerun", qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(0, _wait_private_docx_worker(rerun)["returncode"])
        foreign = repository_root() / "sources" / self.prepared.staging_name / "foreign.bin"
        foreign.write_bytes(b"foreign")
        with self.assertRaisesRegex(PrivateDocxUatError, "entries"):
            cleanup_private_docx_uat_run(self.prepared)
        self.assertEqual(b"foreign", foreign.read_bytes())

    def test_cleanup_recovers_after_a_reclaimed_entry_ledger_interrupt(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        rerun = _launch_private_docx_worker(
            self.prepared, "rerun", qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(0, _wait_private_docx_worker(rerun)["returncode"])
        module = __import__("ao_lore.private_docx_uat", fromlist=["_persist_uat_document"])
        original = module._persist_uat_document
        interrupted = False

        def stop_after_first_reclaim(directory, runtime, name, value):
            nonlocal interrupted
            if (
                name.startswith("cleanup-") and not interrupted
                and any(len(target.get("reclaimed", [])) == 1 for target in value.get("targets", []))
            ):
                interrupted = True
                raise KeyboardInterrupt()
            return original(directory, runtime, name, value)

        with patch.object(module, "_persist_uat_document", side_effect=stop_after_first_reclaim):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_docx_uat_run(self.prepared)
        self.assertTrue(interrupted)
        self.assertTrue(cleanup_private_docx_uat_run(self.prepared))

    def test_cleanup_recovers_interruption_after_entry_quarantine_rename(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        rerun = _launch_private_docx_worker(
            self.prepared, "rerun", qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(0, _wait_private_docx_worker(rerun)["returncode"])
        module = __import__("ao_lore.private_docx_uat", fromlist=["_rename_noreplace_at"])
        original = module._rename_noreplace_at
        interrupted = False

        def stop_after_entry_rename(source_fd, source, destination_fd, destination):
            nonlocal interrupted
            result = original(source_fd, source, destination_fd, destination)
            if not interrupted and str(destination).startswith(".private-docx-reclaim-"):
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(
            module, "_rename_noreplace_at", side_effect=stop_after_entry_rename
        ):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_docx_uat_run(self.prepared)
        self.assertTrue(interrupted)
        self.assertTrue(cleanup_private_docx_uat_run(self.prepared))

    def test_cleanup_recovery_rejects_traversal_quarantine_without_touching_foreign(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        rerun = _launch_private_docx_worker(
            self.prepared, "rerun", qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(0, _wait_private_docx_worker(rerun)["returncode"])
        module = __import__("ao_lore.private_docx_uat", fromlist=["_persist_uat_document"])
        original = module._persist_uat_document

        def stop_after_plan(directory, runtime, name, value):
            body = original(directory, runtime, name, value)
            if name.startswith("cleanup-") and all(
                target.get("phase") in {"planned", "absent"}
                for target in value.get("targets", [])
            ):
                raise KeyboardInterrupt()
            return body

        with patch.object(module, "_persist_uat_document", side_effect=stop_after_plan):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_docx_uat_run(self.prepared)
        marker = next((self.runtime_root / "uat/private-docx").glob("cleanup-*.json"))
        recovery = json.loads(marker.read_text(encoding="utf-8"))
        malicious = next(target for target in recovery["targets"] if target["present"])
        malicious["quarantine"] = "../../foreign"
        marker.write_text(
            json.dumps(recovery, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        foreign = self.root / "foreign"
        foreign.mkdir()
        sentinel = foreign / "sentinel"
        sentinel.write_bytes(b"preserve")
        with self.assertRaisesRegex(PrivateDocxUatError, "recovery is invalid"):
            cleanup_private_docx_uat_run(self.prepared)
        self.assertEqual(b"preserve", sentinel.read_bytes())

    def test_cleanup_recovers_crash_after_quarantine_root_rmdir(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        rerun = _launch_private_docx_worker(
            self.prepared, "rerun", qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(0, _wait_private_docx_worker(rerun)["returncode"])
        module = __import__("ao_lore.private_docx_uat", fromlist=["os"])
        original = module.os.rmdir
        interrupted = False

        def stop_after_root_rmdir(path, *args, **kwargs):
            nonlocal interrupted
            result = original(path, *args, **kwargs)
            if not interrupted and str(path).startswith(".private-docx-cleanup-"):
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(module.os, "rmdir", side_effect=stop_after_root_rmdir):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_docx_uat_run(self.prepared)
        self.assertTrue(interrupted)
        self.assertTrue(cleanup_private_docx_uat_run(self.prepared))

    def test_retained_evidence_recovers_one_bounded_partial_prefix(self):
        module = __import__("ao_lore.private_docx_uat", fromlist=["_write_all"])
        original = module._write_all
        interrupted = False

        def stop_mid_write(descriptor, body, label):
            nonlocal interrupted
            if label == "retained evidence" and not interrupted:
                interrupted = True
                os.write(descriptor, body[: max(1, len(body) // 2)])
                raise KeyboardInterrupt()
            return original(descriptor, body, label)

        with patch.object(module, "_write_all", side_effect=stop_mid_write):
            with self.assertRaises(KeyboardInterrupt):
                module._persist_private_docx_evidence(
                    self.prepared, readback(), DIGEST_A, DIGEST_A,
                    brain_before=DIGEST_A,
                )
        evidence_parent = self.runtime_root / "evidence/private-docx-uat"
        pending = evidence_parent / f".pending-{self.prepared.batch_manifest['batch_id']}"
        self.assertTrue(pending.is_dir())
        self.assertTrue(module._persist_private_docx_evidence(
            self.prepared, readback(), DIGEST_A, DIGEST_A,
            brain_before=DIGEST_A,
        ).startswith("sha256:"))
        final = evidence_parent / self.prepared.batch_manifest["batch_id"]
        self.assertEqual(
            {"private-docx-uat-readback.json", "cleaned-state.json"},
            {path.name for path in final.iterdir()},
        )
        self.assertFalse(pending.exists())

    def test_retained_evidence_refuses_and_preserves_foreign_staging(self):
        module = __import__("ao_lore.private_docx_uat", fromlist=["_write_all"])
        evidence_parent = self.runtime_root / "evidence/private-docx-uat"
        evidence_parent.mkdir(parents=True)
        pending = evidence_parent / f".pending-{self.prepared.batch_manifest['batch_id']}"
        pending.mkdir()
        foreign = pending / "foreign.bin"
        foreign.write_bytes(b"preserve")
        with self.assertRaisesRegex(PrivateDocxUatError, "foreign entries"):
            module._persist_private_docx_evidence(
                self.prepared, readback(), DIGEST_A, DIGEST_A,
                brain_before=DIGEST_A,
            )
        self.assertEqual(b"preserve", foreign.read_bytes())

    def test_retained_evidence_refuses_unknown_staging_identity_and_nonprefix(self):
        module = __import__("ao_lore.private_docx_uat", fromlist=["_write_all"])
        evidence_parent = self.runtime_root / "evidence/private-docx-uat"
        evidence_parent.mkdir(parents=True)
        unknown = evidence_parent / ".pending-foreign"
        unknown.mkdir()
        sentinel = unknown / "sentinel"
        sentinel.write_bytes(b"preserve")
        with self.assertRaisesRegex(PrivateDocxUatError, "foreign staging"):
            module._persist_private_docx_evidence(
                self.prepared, readback(), DIGEST_A, DIGEST_A,
                brain_before=DIGEST_A,
            )
        self.assertEqual(b"preserve", sentinel.read_bytes())
        unknown.rename(evidence_parent / "preserved-foreign")
        pending = evidence_parent / f".pending-{self.prepared.batch_manifest['batch_id']}"
        pending.mkdir()
        drift = pending / ".writing-evidence-intent.json"
        drift.write_bytes(b"not-a-valid-prefix")
        with self.assertRaisesRegex(PrivateDocxUatError, "staging drifted"):
            module._persist_private_docx_evidence(
                self.prepared, readback(), DIGEST_A, DIGEST_A,
                brain_before=DIGEST_A,
            )
        self.assertEqual(b"not-a-valid-prefix", drift.read_bytes())

    def test_retained_evidence_recovers_after_intent_removal_before_publish(self):
        module = __import__("ao_lore.private_docx_uat", fromlist=["_rename_noreplace_at"])
        original = module._rename_noreplace_at
        interrupted = False

        def stop_publish(source_fd, source, destination_fd, destination):
            nonlocal interrupted
            if str(source).startswith(".pending-") and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return original(source_fd, source, destination_fd, destination)

        with patch.object(module, "_rename_noreplace_at", side_effect=stop_publish):
            with self.assertRaises(KeyboardInterrupt):
                module._persist_private_docx_evidence(
                    self.prepared, readback(), DIGEST_A, DIGEST_A,
                    brain_before=DIGEST_A,
                )
        self.assertTrue(interrupted)
        self.assertTrue(module._persist_private_docx_evidence(
            self.prepared, readback(), DIGEST_A, DIGEST_A,
            brain_before=DIGEST_A,
        ).startswith("sha256:"))

    def test_network_and_process_guards_restore_every_callable_on_baseexception(self):
        originals = {
            "connect": socket.socket.connect,
            "sendto": socket.socket.sendto,
            "popen": __import__("subprocess").Popen,
            "system": os.system,
        }
        attempts: list[str] = []
        with self.assertRaises(KeyboardInterrupt):
            with docx_worker._execution_guards(attempts):
                self.assertIsNot(socket.socket.connect, originals["connect"])
                self.assertIsNot(socket.socket.sendto, originals["sendto"])
                self.assertIsNot(__import__("subprocess").Popen, originals["popen"])
                self.assertIsNot(os.system, originals["system"])
                raise KeyboardInterrupt()
        self.assertIs(socket.socket.connect, originals["connect"])
        self.assertIs(socket.socket.sendto, originals["sendto"])
        self.assertIs(__import__("subprocess").Popen, originals["popen"])
        self.assertIs(os.system, originals["system"])

    def test_seccomp_builder_closes_descriptor_on_baseexception(self):
        docx_uat_module = __import__("ao_lore.private_docx_uat", fromlist=["ctypes"])
        before = len(os.listdir("/proc/self/fd"))
        with patch.object(
            docx_uat_module.ctypes, "CDLL", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                docx_uat_module._seccomp_descriptor()
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))

    def _held_worker(self, *, phase="resume"):
        state = self.runtime_root / "worker-unit"
        state.mkdir()
        journal_path = state / "conversion-journal.jsonl"
        journal_path.write_bytes(b"")
        parent = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        journal = os.open(journal_path, os.O_RDWR | os.O_APPEND)
        contract = {
            "phase": phase,
            "batch_manifest_digest": self.prepared.batch_manifest_digest,
            "expectation_digest": self.prepared.expectation_digest,
            "run_digest": DIGEST_A,
            "journal_identity": {
                "device": os.fstat(journal).st_dev,
                "inode": os.fstat(journal).st_ino,
            },
        }
        return docx_worker._HeldRun(
            contract=contract,
            batch=dict(self.prepared.batch_manifest),
            qualification=dict(self.qualification),
            sources={},
            source_origins={},
            journal_parent_descriptor=parent,
            journal_descriptor=journal,
            attempts=[],
            descriptors=[parent, journal],
        )

    def test_conversion_accounting_appends_on_success_and_exception_not_baseexception(self):
        from ao_lore.docx_ooxml import NativeDocxOoxmlAdapter

        held = self._held_worker()
        original = NativeDocxOoxmlAdapter.parse
        try:
            with patch.object(NativeDocxOoxmlAdapter, "parse", return_value={"ok": True}):
                with docx_worker._counted_adapter(held):
                    self.assertEqual({"ok": True}, NativeDocxOoxmlAdapter.parse(object(), {}))
                self.assertEqual(1, docx_worker._journal_count(held))
            with patch.object(NativeDocxOoxmlAdapter, "parse", side_effect=RuntimeError("parse")):
                with self.assertRaises(RuntimeError):
                    with docx_worker._counted_adapter(held):
                        NativeDocxOoxmlAdapter.parse(object(), {})
                self.assertEqual(2, docx_worker._journal_count(held))
            with patch.object(NativeDocxOoxmlAdapter, "parse", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    with docx_worker._counted_adapter(held):
                        NativeDocxOoxmlAdapter.parse(object(), {})
                self.assertEqual(2, docx_worker._journal_count(held))
            self.assertIs(NativeDocxOoxmlAdapter.parse, original)
        finally:
            held.close()

    def test_interrupted_phase_barrier_precedes_second_adapter_call(self):
        from ao_lore.docx_ooxml import NativeDocxOoxmlAdapter

        held = self._held_worker(phase="interrupted")
        docx_worker._append_conversion_event(held)
        try:
            with (
                patch.object(NativeDocxOoxmlAdapter, "parse", return_value={"unexpected": True}),
                patch.object(docx_worker, "_persist_barrier") as barrier,
                patch.object(signal, "pause", side_effect=KeyboardInterrupt),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    with docx_worker._counted_adapter(held):
                        NativeDocxOoxmlAdapter.parse(object(), {})
            barrier.assert_called_once_with(held, 1)
            self.assertEqual(1, docx_worker._journal_count(held))
        finally:
            held.close()

    def test_caught_network_attempt_aborts_without_conversion_accounting(self):
        from ao_lore.docx_ooxml import NativeDocxOoxmlAdapter

        held = self._held_worker()

        def caught_attempt(*_args, **_kwargs):
            try:
                socket.getaddrinfo("example.invalid", 443)
            except BaseException:
                return {"forged": True}

        try:
            with patch.object(NativeDocxOoxmlAdapter, "parse", side_effect=caught_attempt):
                with self.assertRaises(docx_worker._NetworkAttempt):
                    with docx_worker._execution_guards(held.attempts), docx_worker._counted_adapter(held):
                        NativeDocxOoxmlAdapter.parse(object(), {})
            self.assertEqual(0, docx_worker._journal_count(held))
        finally:
            held.close()

    def test_worker_main_closes_held_descriptors_and_restores_guards_on_baseexception(self):
        from ao_lore.docx_ooxml import NativeDocxOoxmlAdapter

        held = self._held_worker()
        descriptors = list(held.descriptors)
        original_parse = NativeDocxOoxmlAdapter.parse
        original_connect = socket.socket.connect
        with patch.object(docx_worker, "_load_run_contract", return_value=held):
            with self.assertRaises(KeyboardInterrupt):
                docx_worker.main(after_validation=lambda _held: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertIs(NativeDocxOoxmlAdapter.parse, original_parse)
        self.assertIs(socket.socket.connect, original_connect)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def _persist_worker_contract(self, phase="interrupted"):
        contract = _worker_run_contract(
            self.prepared,
            phase,
            qualification=self.qualification,
            sandbox={"proof": DIGEST_A},
            runtime_root=self.runtime_root,
        )
        path = (
            self.runtime_root
            / "uat/private-docx"
            / self.prepared.batch_manifest["batch_id"]
            / "worker-run-contract.json"
        )
        path.write_text(
            json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        return contract, path

    def test_worker_loads_held_sources_once_and_rejects_public_source_swap(self):
        _contract, contract_path = self._persist_worker_contract()
        contract_body = contract_path.read_bytes()
        with (
            patch.object(docx_worker, "runtime_home", return_value=self.runtime_root),
            patch.object(docx_worker, "_validate_sandbox_contract"),
        ):
            held = docx_worker._load_run_contract(expected_contract_body=contract_body)
        first = self.prepared.batch_manifest["documents"][0]
        staged = repository_root() / first["source"]
        original = held.sources[first["source"]]
        replacement = staged.with_suffix(".replacement")
        replacement.write_bytes(b"PK\x03\x04foreign")
        replacement.replace(staged)
        try:
            self.assertEqual(original, held.sources[first["source"]])
            self.assertEqual(first["source_digest"], "sha256:" + __import__("hashlib").sha256(original).hexdigest())
            dependencies = docx_worker._batch_dependencies(held)
            self.assertEqual(
                original,
                dependencies.read_source(staged, first["source_digest"]),
            )
        finally:
            held.close()

        with (
            patch.object(docx_worker, "runtime_home", return_value=self.runtime_root),
            patch.object(docx_worker, "_validate_sandbox_contract"),
        ):
            with self.assertRaisesRegex(OSError, "source"):
                docx_worker._load_run_contract(expected_contract_body=contract_body)
        self.assertEqual(b"PK\x03\x04foreign", staged.read_bytes())

    def test_worker_rejects_contract_manifest_and_journal_swaps_without_mutation(self):
        contract, contract_path = self._persist_worker_contract()
        contract_body = contract_path.read_bytes()
        manifest_path = repository_root() / contract["manifest_locator"]
        journal_path = self.runtime_root / contract["journal_locator"]
        cases = (
            (contract_path, b'{"foreign":true}\n', "contract"),
            (manifest_path, b'{"foreign":true}\n', "manifest"),
            (journal_path, b"foreign\n", "journal"),
        )
        for target, foreign, label in cases:
            with self.subTest(label=label):
                backup = target.with_name(target.name + ".original")
                os.link(target, backup)
                temporary = target.with_name(target.name + ".foreign")
                temporary.write_bytes(foreign)
                temporary.replace(target)
                try:
                    with (
                        patch.object(docx_worker, "runtime_home", return_value=self.runtime_root),
                        patch.object(docx_worker, "_validate_sandbox_contract"),
                    ):
                        with self.assertRaises(Exception):
                            docx_worker._load_run_contract(
                                expected_contract_body=contract_body
                            )
                    self.assertEqual(foreign, target.read_bytes())
                finally:
                    backup.replace(target)

    def test_worker_rejects_self_consistent_rw_contract_forgery_against_immutable_launch_copy(self):
        contract, contract_path = self._persist_worker_contract()
        expected_body = contract_path.read_bytes()
        contract["phase"] = "rerun"
        contract["run_digest"] = canonical_digest(
            {key: value for key, value in contract.items() if key != "run_digest"}
        )
        contract_path.write_text(
            json.dumps(contract, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        with (
            patch.object(docx_worker, "runtime_home", return_value=self.runtime_root),
            patch.object(docx_worker, "_validate_sandbox_contract"),
        ):
            with self.assertRaisesRegex(OSError, "immutable|contract"):
                docx_worker._load_run_contract(expected_contract_body=expected_body)

    def test_fresh_resume_and_rerun_contracts_are_rejected(self):
        for phase in ("resume", "rerun"):
            with self.subTest(phase=phase):
                with self.assertRaisesRegex(PrivateDocxUatError, "phase|arithmetic"):
                    _worker_run_contract(
                        self.prepared,
                        phase,
                        qualification=self.qualification,
                        sandbox={"proof": DIGEST_A},
                        runtime_root=self.runtime_root,
                    )

    def test_actual_bwrap_denies_network_process_host_filesystem_and_extra_mounts(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        process = _launch_private_docx_worker(
            self.prepared,
            "rerun",
            qualification=self.qualification,
            runtime_root=self.runtime_root,
            module="tests.private_docx_uat_worker_fixture",
            pythonpath=f"{repository_root() / 'src'}:{repository_root()}",
        )
        result = _wait_private_docx_worker(process)
        self.assertEqual(0, result["returncode"], result["stderr"])
        observed = json.loads(result["stdout"])
        self.assertTrue(all(observed.values()), observed)
        self.assertTrue(observed["native_ctypes_fork"])
        self.assertTrue(observed["resource_limits"])

    def test_actual_bwrap_worker_output_is_hard_capped(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        self._complete_actual_campaign()
        state = self.runtime_root / "uat/private-docx" / self.prepared.batch_manifest["batch_id"]
        (state / "fixture-action.json").write_text('{"action":"stdout-flood"}\n', encoding="utf-8")
        process = _launch_private_docx_worker(
            self.prepared,
            "rerun",
            qualification=self.qualification,
            runtime_root=self.runtime_root,
            module="tests.private_docx_uat_worker_fixture",
            pythonpath=f"{repository_root() / 'src'}:{repository_root()}",
        )
        with self.assertRaisesRegex(PrivateDocxUatError, "output"):
            _wait_private_docx_worker(process)

    def test_sandbox_mounts_only_exact_staging_control_and_worker_state(self):
        sandbox = _docx_sandbox_spec(
            self.prepared,
            "resume",
            command=[__import__("sys").executable, "-m", "ao_lore.private_docx_uat_worker"],
            pythonpath=str(repository_root() / "src"),
            bwrap_version="bubblewrap 0.9.0",
            runtime_root=self.runtime_root,
        )
        mounts = {mount["name"]: mount for mount in sandbox["mounts"]}
        self.assertEqual("read-only", mounts["staged-sources"]["mode"])
        self.assertEqual("read-only", mounts["control-manifest"]["mode"])
        self.assertEqual("read-only", mounts["qualification"]["mode"])
        self.assertEqual("read-write", mounts["batch-state"]["mode"])
        self.assertEqual("read-write", mounts["candidate-state"]["mode"])
        self.assertEqual("read-write", mounts["run-state"]["mode"])
        self.assertNotIn(str(self.runtime_root / "private-docx/corpus"), [mount["target"] for mount in mounts.values()])
        self.assertNotIn(str(repository_root()), [mount["target"] for mount in mounts.values()])

        self.assertEqual(
            {
                "cpu_seconds": 60,
                "address_space_bytes": 2 * 1024 * 1024 * 1024,
                "data_bytes": 1024 * 1024 * 1024,
                "file_size_bytes": 64 * 1024 * 1024,
                "open_files": 64,
                "processes": 1,
            },
            sandbox["resource_limits"],
        )
        self.assertEqual("seccomp-deny-clone-fork-vfork-clone3", sandbox["child_process_policy"])
        self.assertEqual("/run/ao-lore-private-docx-contract.json", sandbox["immutable_contract"]["path"])
        self.assertTrue(sandbox["immutable_contract"]["sealed"])

    def test_worker_rejects_environment_drift_and_any_extra_mount(self):
        sandbox = _docx_sandbox_spec(
            self.prepared,
            "resume",
            command=[__import__("sys").executable, "-m", "ao_lore.private_docx_uat_worker"],
            pythonpath=str(repository_root() / "src"),
            bwrap_version="bubblewrap 0.9.0",
            runtime_root=self.runtime_root,
        )
        state = self.runtime_root / "uat/private-docx" / self.prepared.batch_manifest["batch_id"]
        with patch.dict(os.environ, sandbox["environment"], clear=True):
            docx_worker._validate_sandbox_contract(
                sandbox,
                root=self.runtime_root,
                state=state,
                batch_id=self.prepared.batch_manifest["batch_id"],
                staging_name=self.prepared.staging_name,
            )
            extra = json.loads(json.dumps(sandbox))
            extra["mounts"].append(dict(extra["mounts"][0], name="unexpected-extra"))
            with self.assertRaisesRegex(OSError, "mount"):
                docx_worker._validate_sandbox_contract(
                    extra,
                    root=self.runtime_root,
                    state=state,
                    batch_id=self.prepared.batch_manifest["batch_id"],
                    staging_name=self.prepared.staging_name,
                )
        drifted = dict(sandbox["environment"], UNDECLARED="1")
        with patch.dict(os.environ, drifted, clear=True):
            with self.assertRaisesRegex(OSError, "environment|sandbox"):
                docx_worker._validate_sandbox_contract(
                    sandbox,
                    root=self.runtime_root,
                    state=state,
                    batch_id=self.prepared.batch_manifest["batch_id"],
                    staging_name=self.prepared.staging_name,
                )

    def test_sandbox_rejects_host_unix_socket_inside_any_read_only_tree(self):
        staged = repository_root() / "sources" / self.prepared.staging_name
        socket_path = staged / "host.sock"
        short = Path("/tmp") / f"ao-docx-socket-{os.getpid()}"
        short.symlink_to(staged, target_is_directory=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(short / "host.sock"))
            with self.assertRaisesRegex(PrivateDocxUatError, "mount"):
                _docx_sandbox_spec(
                    self.prepared,
                    "resume",
                    command=[__import__("sys").executable, "-m", "ao_lore.private_docx_uat_worker"],
                    pythonpath=str(repository_root() / "src"),
                    bwrap_version="bubblewrap 0.9.0",
                    runtime_root=self.runtime_root,
                )
            self.assertTrue(socket_path.exists())
        finally:
            server.close()
            socket_path.unlink(missing_ok=True)
            short.unlink(missing_ok=True)

    def test_actual_launch_rejects_fresh_resume_and_rerun_before_process_start(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        for phase in ("resume", "rerun"):
            with self.subTest(phase=phase), patch(
                "ao_lore.private_docx_uat._probe_docx_sandbox",
                return_value="bubblewrap 0.9.0",
            ), patch("ao_lore.private_docx_uat.subprocess.Popen") as popen:
                with self.assertRaisesRegex(PrivateDocxUatError, "arithmetic"):
                    _launch_private_docx_worker(
                        self.prepared,
                        phase,
                        qualification=self.qualification,
                        runtime_root=self.runtime_root,
                    )
                popen.assert_not_called()

    def test_actual_worker_interrupts_at_exact_checkpoint_bound_barrier(self):
        if not Path("/usr/bin/bwrap").is_file():
            self.skipTest("bubblewrap is unavailable")
        process = _launch_private_docx_worker(
            self.prepared,
            "interrupted",
            qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        state = self.runtime_root / "uat/private-docx" / self.prepared.batch_manifest["batch_id"]
        barrier_path = state / "barrier-ready.json"
        deadline = __import__("time").monotonic() + 10.0
        while not barrier_path.exists() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
        self.assertTrue(barrier_path.exists())
        barrier = json.loads(barrier_path.read_text(encoding="utf-8"))
        checkpoint = json.loads(
            (
                self.runtime_root
                / "batches"
                / self.prepared.batch_manifest["batch_id"]
                / "checkpoint.json"
            ).read_text(encoding="utf-8")
        )
        held = self._reopen_held_journal("interrupted")
        try:
            self.assertEqual((1, 1, 1), (checkpoint["processed"], docx_worker._journal_count(held), barrier["processed"]))
            self.assertEqual(checkpoint["checkpoint_digest"], barrier["checkpoint_digest"])
            self.assertEqual(barrier["journal_count"], checkpoint["processed"])
        finally:
            held.close()
        _signal_private_docx_worker(process)
        interrupted = _wait_private_docx_worker(process)
        self.assertIn(interrupted["returncode"], {-signal.SIGTERM, 128 + signal.SIGTERM})
        self.assertEqual(b"", interrupted["stdout"])
        with self.assertRaisesRegex(PrivateDocxUatError, "signal"):
            _signal_private_docx_worker(process)
        resumed = _launch_private_docx_worker(
            self.prepared,
            "resume",
            qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        resumed_result = _wait_private_docx_worker(resumed)
        self.assertEqual(0, resumed_result["returncode"], resumed_result["stderr"])
        held = self._reopen_held_journal("resume")
        try:
            self.assertEqual(4, docx_worker._journal_count(held))
        finally:
            held.close()
        rerun = _launch_private_docx_worker(
            self.prepared,
            "rerun",
            qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        rerun_result = _wait_private_docx_worker(rerun)
        self.assertEqual(0, rerun_result["returncode"], rerun_result["stderr"])
        held = self._reopen_held_journal("rerun")
        try:
            self.assertEqual(4, docx_worker._journal_count(held))
        finally:
            held.close()
        self.assertEqual(json.loads(resumed_result["stdout"]), json.loads(rerun_result["stdout"]))

    def test_checkpoint_before_barrier_is_temporarily_not_ready(self):
        self._persist_worker_contract(phase="interrupted")

        self.assertIsNone(
            _inspect_private_docx_barrier(
                self.prepared,
                runtime_root=self.runtime_root,
            )
        )

    def _complete_actual_campaign(self):
        interrupted = _launch_private_docx_worker(
            self.prepared,
            "interrupted",
            qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        barrier = (
            self.runtime_root / "uat/private-docx"
            / self.prepared.batch_manifest["batch_id"] / "barrier-ready.json"
        )
        deadline = __import__("time").monotonic() + 10.0
        while not barrier.exists() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.01)
        if not barrier.exists():
            self.fail("worker did not publish its interruption barrier")
        _signal_private_docx_worker(interrupted)
        interrupted_result = _wait_private_docx_worker(interrupted)
        self.assertIn(
            interrupted_result["returncode"],
            {-signal.SIGTERM, 128 + signal.SIGTERM},
        )
        resumed = _launch_private_docx_worker(
            self.prepared,
            "resume",
            qualification=self.qualification,
            runtime_root=self.runtime_root,
        )
        resumed_result = _wait_private_docx_worker(resumed)
        self.assertEqual(0, resumed_result["returncode"], resumed_result["stderr"])

    def test_terminate_worker_kills_descendants_after_leader_exits(self):
        subprocess = __import__("subprocess")
        process = subprocess.Popen(
            [
                __import__("sys").executable,
                "-c",
                (
                    "import os,time; pid=os.fork(); "
                    "(time.sleep(60) if pid==0 else None); "
                    "os._exit(0) if pid!=0 else os._exit(0)"
                ),
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process.wait(timeout=5)
        docx_worker_pid = process.pid
        docx_uat = __import__("ao_lore.private_docx_uat", fromlist=["_terminate_worker"])
        docx_uat._terminate_worker(process)
        with self.assertRaises(ProcessLookupError):
            os.killpg(docx_worker_pid, 0)

    def _reopen_held_journal(self, phase):
        state = self.runtime_root / "uat/private-docx" / self.prepared.batch_manifest["batch_id"]
        parent = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        journal = os.open(state / "conversion-journal.jsonl", os.O_RDWR | os.O_APPEND)
        return docx_worker._HeldRun(
            contract={
                "phase": phase,
                "batch_manifest_digest": self.prepared.batch_manifest_digest,
                "run_digest": DIGEST_A,
                "journal_identity": {
                    "device": os.fstat(journal).st_dev,
                    "inode": os.fstat(journal).st_ino,
                },
            },
            batch=dict(self.prepared.batch_manifest),
            qualification=dict(self.qualification),
            sources={},
            source_origins={},
            journal_parent_descriptor=parent,
            journal_descriptor=journal,
            attempts=[],
            descriptors=[parent, journal],
        )


if __name__ == "__main__":
    unittest.main()
