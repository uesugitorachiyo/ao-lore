import copy
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ao_lore.benchmark import canonical_digest
from ao_lore.home import repository_root
from ao_lore.ocr_contracts import AUTHORITY_FIELDS, OCR_POLICY_DIGEST
from ao_lore.private_document_uat import (
    PrivateDocumentPreparedRun,
    PrivateDocumentUatDependencies,
    private_document_policy_digest,
    private_document_state_body,
)
from ao_lore.private_ocr_uat import (
    OcrActivationInputs,
    PreparedOcrCampaignInputs,
    PrivateOcrPreparedRun,
    PRIVATE_OCR_UAT_POLICY,
    OcrUatDependencies,
    PreparedOcrUat,
    load_private_ocr_evidence,
    load_private_ocr_activation,
    load_private_ocr_activation_inputs,
    load_private_ocr_campaign_inputs,
    load_private_ocr_corpus,
    cleanup_private_ocr_corpus,
    prepare_private_ocr_corpus,
    prepare_private_ocr_campaign_inputs,
    prepare_private_ocr_uat_run,
    cleanup_private_ocr_uat_run,
    cleanup_private_ocr_campaign,
    build_private_ocr_uat_dependencies,
    private_ocr_worker_contract,
    private_ocr_calibration_contract,
    project_private_ocr_prepared_run,
    persist_private_ocr_evidence,
    persist_private_ocr_activation,
    run_private_ocr_uat,
    project_private_ocr_action,
)
from ao_lore.ocr_raster import RasterPageOutput
from tests.test_ao_lore_ocr_activation import (
    CORPUS_CONFIGURATION_DIGEST,
    CORPUS_DIGEST,
    MODEL_DIGESTS,
    ORACLE_DIGEST,
    DIGEST as ACTIVATION_RUNTIME_DIGEST,
    body_and_digest,
    qualification,
)
from ao_lore.private_ocr_uat_worker import validate_uat_worker_contract
from ao_lore.private_ocr_calibration_worker import validate_calibration_contract
from tests.private_calibration import private_calibration


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


@dataclass(frozen=True)
class _Run:
    batch_manifest: dict
    batch_manifest_digest: str
    corpus_manifest: dict
    corpus_root: Path


@private_calibration
class PrivateOcrUatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=repository_root())
        self.runtime = Path(self.temporary.name)
        self.prepared_runs = []
        self.prepared_campaigns = []
        self.documents = [
            {"item_id": f"ocr-{index:02d}", "source_digest": "sha256:" + str(index) * 64}
            for index in range(1, 5)
        ]
        self.manifest = {"corpus_id": "private-ocr-english-v0-1", "documents": self.documents}
        self.batch = {
            "schema_version": "ao.lore.ingest-batch-manifest.v0.3",
            "batch_id": "batch-private-ocr-fixture",
            "format_id": "ocr",
            "media_type": "application/pdf",
            "parser_id": "paddle-ocr-english",
            "parser_version": "0.1.0",
            "continue_on_error": True,
            "documents": [
                {
                    **copy.deepcopy(item),
                    "source": f"sources/private-ocr/{item['item_id']}.pdf",
                }
                for item in self.documents
            ],
        }

    def tearDown(self):
        for inputs, campaign, run in reversed(self.prepared_campaigns):
            cleanup_private_ocr_campaign(run, inputs, campaign)
        for run in reversed(self.prepared_runs):
            cleanup_private_ocr_uat_run(run)
        self.temporary.cleanup()

    def _activation_inputs(self):
        value = qualification()
        value["decision"] = "candidate_change"
        value["candidates"][2]["all_hard_gates_pass"] = False
        value["candidates"][2]["expected_outcome_accuracy_millionths"] = 950000
        value["candidates"][2]["hallucinated_lines"] = 2
        body, digest = body_and_digest(value)
        return OcrActivationInputs(
            qualification_body=body,
            qualification_digest=digest,
            runtime_digest=ACTIVATION_RUNTIME_DIGEST,
            corpus_digest=CORPUS_DIGEST,
            corpus_configuration_digest=CORPUS_CONFIGURATION_DIGEST,
            oracle_digest=ORACLE_DIGEST,
            model_digests=MODEL_DIGESTS,
            selected_result_digest=value["candidates"][0]["result_digest"],
        )

    def _prepared_campaign_run(self):
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        campaign = prepare_private_ocr_campaign_inputs(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            renderer_digest=DIGEST_B,
            renderer=lambda *_: (RasterPageOutput(600, 300, png),),
        )
        run = prepare_private_ocr_uat_run(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            campaign=campaign,
        )
        self.prepared_runs.append(run)
        self.prepared_campaigns.append((inputs, campaign, run))
        return inputs, campaign, run

    def test_phase_sandbox_is_empty_root_networkless_and_exactly_mounted(self):
        import ao_lore.private_ocr_uat as module

        inputs, _campaign, run = self._prepared_campaign_run()
        spec = module._ocr_phase_sandbox_spec(
            run,
            inputs,
            "interrupted",
            command=[sys.executable, "-m", "ao_lore.private_ocr_uat_worker"],
            bwrap_version="bubblewrap 0.8.0",
            contract_fd=97,
        )
        argv = spec["argv"]
        self.assertEqual(spec["root_filesystem"], "allowlisted-empty-root")
        self.assertEqual(spec["network_namespace"], "unshared")
        for option in ("--unshare-net", "--unshare-ipc", "--unshare-pid", "--unshare-uts"):
            self.assertIn(option, argv)
        self.assertIn("--tmpfs", argv)
        self.assertNotIn(("--ro-bind", "/", "/"), tuple(zip(argv, argv[1:], argv[2:])))
        mounts = {item["name"]: item for item in spec["mounts"]}
        self.assertEqual(mounts["staged-sources"]["mode"], "read-only")
        self.assertEqual(mounts["raster-bundles"]["mode"], "read-only")
        self.assertEqual(mounts["activation-evidence"]["mode"], "read-only")
        self.assertEqual(mounts["ocr-runtime"]["mode"], "read-only")
        self.assertEqual(mounts["batch-state"]["mode"], "read-write")
        self.assertEqual(mounts["candidate-state"]["mode"], "read-write")
        self.assertEqual(mounts["run-state"]["mode"], "read-write")
        self.assertEqual(spec["immutable_contract"], {
            "path": "/launch/worker-contract.json", "fd": 97, "sealed": True,
        })
        self.assertEqual(spec["environment"]["AO_LORE_HOME"], "/app/.ao-lore")
        self.assertEqual(spec["environment"]["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"], "True")
        self.assertEqual(spec["environment"]["HF_HUB_OFFLINE"], "1")
        self.assertFalse(spec["authority"])

    def test_phase_sandbox_rejects_non_character_nvidia_node(self):
        import ao_lore.private_ocr_uat as module

        inputs, _campaign, run = self._prepared_campaign_run()
        real_lstat = os.lstat

        def differing(path):
            if os.fspath(path) == "/dev/nvidia0":
                return os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return real_lstat(path)

        with patch.object(module.os, "lstat", side_effect=differing), self.assertRaises(Exception):
            module._ocr_phase_sandbox_spec(
                run, inputs, "interrupted",
                command=[sys.executable, "-m", "ao_lore.private_ocr_uat_worker"],
                bwrap_version="bubblewrap 0.8.0", contract_fd=97,
            )

    def test_phase_sandbox_rejects_foreign_capability_name(self):
        import ao_lore.private_ocr_uat as module

        foreign = Path("/dev/nvidia-caps/foreign")
        real_lstat = os.lstat

        def capability(path):
            if Path(path) == foreign:
                return os.stat_result((stat.S_IFCHR | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return real_lstat(path)

        with (
            patch.object(Path, "iterdir", return_value=iter((foreign,))),
            patch.object(module.os, "lstat", side_effect=capability),
            self.assertRaisesRegex(Exception, "private OCR GPU device differs"),
        ):
            module._append_private_ocr_nvidia_devices([])

    def test_phase_launcher_uses_sealed_contract_and_new_process_group(self):
        import ao_lore.private_ocr_uat as module

        inputs, _campaign, run = self._prepared_campaign_run()
        captured = {}

        class Process:
            pid = 43210

        def popen(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            captured["contract"] = os.pread(kwargs["pass_fds"][0], 1024 * 1024, 0)
            captured["seals"] = fcntl.fcntl(kwargs["pass_fds"][0], fcntl.F_GET_SEALS)
            return Process()

        with (
            patch.object(module, "_probe_ocr_phase_sandbox", return_value="bubblewrap 0.8.0"),
            patch.object(module.subprocess, "Popen", side_effect=popen),
        ):
            process = module._launch_private_ocr_worker(run, inputs, "interrupted")
        self.assertTrue(captured["contract"])
        required = fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL
        self.assertEqual(captured["seals"], required)
        self.assertTrue(captured["kwargs"]["start_new_session"])
        self.assertEqual(process._ao_lore_process_group, 43210)
        self.assertEqual(process._ao_lore_phase, "interrupted")

    def test_phase_launcher_rejects_fresh_resume_and_rerun_before_process_start(self):
        import ao_lore.private_ocr_uat as module

        inputs, _campaign, run = self._prepared_campaign_run()
        with patch.object(module, "_probe_ocr_phase_sandbox") as probe:
            for phase in ("resume", "rerun"):
                with self.subTest(phase=phase), self.assertRaises(Exception):
                    module._launch_private_ocr_worker(run, inputs, phase)
        probe.assert_not_called()

    def test_phase_termination_kills_descendants_after_leader_exits(self):
        import ao_lore.private_ocr_uat as module

        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,time; p=os.fork(); os._exit(0) if p else time.sleep(30)",
            ],
            start_new_session=True,
        )
        process._ao_lore_process_group = process.pid
        try:
            deadline = time.monotonic() + 2
            while process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            module._terminate_private_ocr_worker(process)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def test_actual_phase_sandbox_denies_network_host_sockets_and_outside_writes(self):
        import ao_lore.private_ocr_uat as module

        inputs, _campaign, run = self._prepared_campaign_run()
        process = module._launch_private_ocr_worker(
            run, inputs, "interrupted", module="tests.private_ocr_uat_worker_fixture"
        )
        outcome = module._wait_private_ocr_worker(process, timeout=30)
        stdout, stderr = outcome["stdout"], outcome["stderr"]
        proof_path = (
            self.runtime / "uat" / "private-ocr" / run.batch_id / "sandbox-proof.json"
        )
        proof_body = proof_path.read_bytes()
        proof_path.unlink()
        proof = json.loads(proof_body.decode("ascii"))
        self.assertEqual(outcome["returncode"], 0, repr(proof) + stderr.decode("utf-8", errors="replace"))
        self.assertEqual(stdout, b"")
        self.assertTrue(process._ao_lore_sandbox_verified)
        self.assertEqual(proof, {
            "authority": False,
            "contract_valid": True,
            "dns_denied": True,
            "host_unix_socket_absent": True,
            "nested_bwrap_allowed": True,
            "outside_write_denied": True,
            "socket_creation_allowed": True,
            "tcp_denied": True,
            "udp_denied": True,
        })

    def test_activation_is_atomic_idempotent_and_semantically_reopened(self):
        inputs = self._activation_inputs()
        first = persist_private_ocr_activation(
            runtime_root=self.runtime, inputs=inputs
        )
        final = self.runtime / "evidence" / "private-ocr-activation" / "current"
        self.assertEqual(
            {item.name for item in final.iterdir()},
            {"qualification.json", "activation.json"},
        )
        identities = {item.name: item.stat().st_ino for item in final.iterdir()}
        second = persist_private_ocr_activation(
            runtime_root=self.runtime, inputs=inputs
        )
        reopened = load_private_ocr_activation(
            runtime_root=self.runtime, inputs=inputs
        )
        self.assertEqual(first, second)
        self.assertEqual(reopened, first)
        self.assertEqual(
            identities, {item.name: item.stat().st_ino for item in final.iterdir()}
        )
        self.assertEqual(first.candidate_id, "candidate-1")
        self.assertFalse(first.provider_enabled)
        self.assertFalse(first.fallback_enabled)

    def test_activation_inputs_reopen_from_only_canonical_retained_bytes(self):
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        self.assertEqual(
            load_private_ocr_activation_inputs(runtime_root=self.runtime), inputs
        )
        activation = (
            self.runtime / "evidence" / "private-ocr-activation" / "current"
            / "activation.json"
        )
        body = activation.read_bytes()
        activation.chmod(0o600)
        activation.write_bytes(
            body.replace(b'"provider_enabled":false', b'"provider_enabled":true')
        )
        with self.assertRaises(Exception):
            load_private_ocr_activation_inputs(runtime_root=self.runtime)

    def test_activation_recovers_partial_publication_and_rejects_drift(self):
        inputs = self._activation_inputs()
        with patch("ao_lore.private_ocr_uat._rename_noreplace") as rename:
            rename.side_effect = KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                persist_private_ocr_activation(
                    runtime_root=self.runtime, inputs=inputs
                )
        self.assertEqual(
            persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs),
            load_private_ocr_activation(runtime_root=self.runtime, inputs=inputs),
        )
        changed = copy.deepcopy(inputs)
        object.__setattr__(changed, "selected_result_digest", DIGEST_B)
        with self.assertRaises(Exception):
            load_private_ocr_activation(runtime_root=self.runtime, inputs=changed)

    def test_campaign_inputs_bind_four_sources_rasters_and_activation(self):
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        corpus = prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        calls = []

        def render(body, media_type, configuration):
            calls.append((len(body), media_type, dict(configuration)))
            return (RasterPageOutput(600, 300, png),)

        prepared = prepare_private_ocr_campaign_inputs(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            renderer_digest=DIGEST_B,
            renderer=render,
        )
        self.assertIs(type(prepared), PreparedOcrCampaignInputs)
        self.assertEqual(len(prepared.sources), 4)
        self.assertEqual(len(calls), 4)
        self.assertEqual(
            [item["source_digest"] for item in prepared.sources],
            [item["source_digest"] for item in corpus["documents"]],
        )
        self.assertEqual(
            {item["selected_candidate_id"] for item in prepared.sources},
            {"candidate-1"},
        )
        self.assertEqual(
            prepare_private_ocr_campaign_inputs(
                runtime_root=self.runtime,
                activation_inputs=inputs,
                renderer_digest=DIGEST_B,
                renderer=lambda *_: self.fail("idempotent reopen rendered again"),
            ),
            prepared,
        )

    def test_campaign_inputs_reopen_from_canonical_retained_state(self):
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        campaign = prepare_private_ocr_campaign_inputs(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            renderer_digest=DIGEST_B,
            renderer=lambda *_: (RasterPageOutput(600, 300, png),),
        )
        self.assertEqual(
            load_private_ocr_campaign_inputs(runtime_root=self.runtime), campaign
        )

    def test_campaign_inputs_reject_foreign_raster_state(self):
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        raster_root = self.runtime / "private-ocr" / "rasters"
        raster_root.mkdir()
        (raster_root / "foreign").mkdir()
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        with self.assertRaises(Exception):
            prepare_private_ocr_campaign_inputs(
                runtime_root=self.runtime,
                activation_inputs=inputs,
                renderer_digest=DIGEST_B,
                renderer=lambda *_: (RasterPageOutput(600, 300, png),),
            )

    def test_prepared_run_publishes_exact_random_stage_and_cleans_it(self):
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        campaign = prepare_private_ocr_campaign_inputs(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            renderer_digest=DIGEST_B,
            renderer=lambda *_: (RasterPageOutput(600, 300, png),),
        )
        before = {
            item.name for item in (repository_root() / "sources").iterdir()
            if item.name.startswith("private-ocr-uat-")
        }
        run = prepare_private_ocr_uat_run(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            campaign=campaign,
        )
        self.assertIs(type(run), PrivateOcrPreparedRun)
        self.assertRegex(run.staging_name, r"^private-ocr-uat-[0-9a-f]{24}$")
        self.assertEqual(run.batch_manifest["batch_id"], run.batch_id)
        self.assertEqual(
            [item["source_digest"] for item in run.batch_manifest["documents"]],
            [item["source_digest"] for item in campaign.sources],
        )
        self.assertTrue(cleanup_private_ocr_uat_run(run))
        self.assertTrue(cleanup_private_ocr_uat_run(run))
        after = {
            item.name for item in (repository_root() / "sources").iterdir()
            if item.name.startswith("private-ocr-uat-")
        }
        self.assertEqual(after, before)

    def test_prepared_run_failure_reclaims_only_its_owned_stage(self):
        before = {
            item.name for item in (repository_root() / "sources").iterdir()
            if item.name.startswith("private-ocr-uat-")
        }
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        campaign = prepare_private_ocr_campaign_inputs(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            renderer_digest=DIGEST_B,
            renderer=lambda *_: (RasterPageOutput(600, 300, png),),
        )
        original = __import__(
            "ao_lore.private_ocr_uat", fromlist=["_write_owned"]
        )._write_owned
        writes = 0

        def interrupt(parent, name, body):
            nonlocal writes
            if name.endswith(".pdf"):
                writes += 1
                if writes == 2:
                    raise KeyboardInterrupt()
            return original(parent, name, body)

        with patch("ao_lore.private_ocr_uat._write_owned", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_ocr_uat_run(
                    runtime_root=self.runtime,
                    activation_inputs=inputs,
                    campaign=campaign,
                )
        self.assertEqual(before, {
            item.name for item in (repository_root() / "sources").iterdir()
            if item.name.startswith("private-ocr-uat-")
        })
        self.assertFalse((self.runtime / "private-ocr" / "runs").exists())

    def test_prepared_run_projects_shared_and_immutable_phase_contracts(self):
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        campaign = prepare_private_ocr_campaign_inputs(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            renderer_digest=DIGEST_B,
            renderer=lambda *_: (RasterPageOutput(600, 300, png),),
        )
        run = prepare_private_ocr_uat_run(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            campaign=campaign,
        )
        shared = project_private_ocr_prepared_run(run, inputs)
        self.assertIs(type(shared), PrivateDocumentPreparedRun)
        self.assertEqual(shared.batch_id, run.batch_id)
        self.assertEqual(shared.manifest_digest, canonical_digest(shared.manifest))
        for phase, arithmetic in (
            ("interrupted", (0, 1)), ("resume", (1, 4)), ("rerun", (4, 4)),
        ):
            contract = private_ocr_worker_contract(run, inputs, phase)
            self.assertEqual(
                (contract["expected_start"], contract["expected_end"]), arithmetic
            )
            self.assertEqual(contract["campaign_origin_digest"], campaign.campaign_origin_digest)
            self.assertEqual(contract["batch_manifest_digest"], run.batch_manifest_digest)
            self.assertEqual(contract["batch_manifest"], run.batch_manifest)
            self.assertEqual(contract["staging_name"], run.staging_name)
        self.assertTrue(cleanup_private_ocr_uat_run(run))

    def test_calibration_contract_and_sandbox_are_read_only_and_exactly_two_attempts(self):
        import ao_lore.private_ocr_uat as module

        inputs, _campaign, run = self._prepared_campaign_run()
        contract = private_ocr_calibration_contract(run, inputs)
        self.assertEqual(contract["attempts_per_item"], 2)
        spec = module._ocr_phase_sandbox_spec(
            run, inputs, "calibration",
            command=[sys.executable, "-m", "ao_lore.private_ocr_calibration_worker"],
            bwrap_version="bubblewrap 0.8.0", contract_fd=97, seccomp_fd=98,
        )
        mounts = {item["name"]: item for item in spec["mounts"]}
        self.assertNotIn("batch-state", mounts)
        self.assertNotIn("candidate-state", mounts)
        self.assertNotIn("staged-sources", mounts)
        self.assertEqual(mounts["run-state"]["mode"], "read-write")
        self.assertEqual(spec["child_process_policy"], "nested-bwrap-workers-only")

    def test_calibration_runs_every_raster_twice_and_closes_decisions(self):
        import ao_lore.private_ocr_calibration_worker as calibration
        from tests.test_ao_lore_ocr_ingestion import activation, worker_result

        contract = {
            "schema_version": "ao.lore.private-ocr-calibration-contract.v0.1",
            "attempts_per_item": 2,
            **self._origin_contract(),
        }
        bundles = {
            source["source_digest"]: SimpleNamespace(
                source_id=source["item_id"], pages=(SimpleNamespace(page_id="page-0001"),)
            )
            for source in contract["sources"]
        }

        def result(runtime, bundle, candidate, *, run_id, parent_network_isolated):
            value = worker_result()
            value["run_id"] = run_id
            return value

        with (
            patch.object(calibration, "_load_activation", return_value=(activation(), DIGEST_A)),
            patch.object(calibration, "_load_rasters", return_value=bundles),
            patch.object(calibration, "run_paddle_ocr_worker", side_effect=result) as paddle,
        ):
            report = calibration.calibrate_contract(contract)
        self.assertEqual(report["decision"], "hold")
        self.assertEqual(report["attempts_per_item"], 2)
        self.assertEqual(len(report["items"]), 4)
        self.assertTrue(all(item["identical"] for item in report["items"]))
        self.assertEqual(paddle.call_count, 8)
        self.assertTrue(all(
            call.kwargs["parent_network_isolated"] is True
            for call in paddle.call_args_list
        ))

        calls = 0
        def unstable(runtime, bundle, candidate, *, run_id, parent_network_isolated):
            nonlocal calls
            calls += 1
            value = result(runtime, bundle, candidate, run_id=run_id,
                           parent_network_isolated=parent_network_isolated)
            if calls == 2:
                value["pages"][0]["detections"][0]["text"] = "changed"
            return value

        with (
            patch.object(calibration, "_load_activation", return_value=(activation(), DIGEST_A)),
            patch.object(calibration, "_load_rasters", return_value=bundles),
            patch.object(calibration, "run_paddle_ocr_worker", side_effect=unstable),
        ):
            report = calibration.calibrate_contract(contract)
        self.assertEqual(report["decision"], "investigate")

    def test_calibration_launcher_seals_contract_without_batch_or_candidate_state(self):
        import ao_lore.private_ocr_uat as module

        inputs, _campaign, run = self._prepared_campaign_run()
        captured = {}

        class Process:
            pid = 43211

        def popen(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["body"] = os.pread(kwargs["pass_fds"][0], 1024 * 1024, 0)
            return Process()

        with (
            patch.object(module, "_probe_ocr_phase_sandbox", return_value="bubblewrap 0.8.0"),
            patch.object(module.subprocess, "Popen", side_effect=popen),
        ):
            process = module._launch_private_ocr_calibration_worker(run, inputs)
        self.assertTrue(captured["body"])
        self.assertEqual(process._ao_lore_phase, "calibration")
        self.assertNotIn(str(self.runtime / "batches" / run.batch_id), captured["argv"])
        self.assertNotIn(
            str(repository_root() / "working" / "candidates" / run.staging_name),
            captured["argv"],
        )
        self.assertFalse((self.runtime / "batches" / run.batch_id).exists())
        self.assertFalse(
            (repository_root() / "working" / "candidates" / run.staging_name).exists()
        )

    def test_campaign_cleanup_removes_only_owned_runtime_and_preserves_activation(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        module._ocr_phase_sandbox_spec(
            run, inputs, "interrupted",
            command=[sys.executable, "-m", "ao_lore.private_ocr_uat_worker"],
            bwrap_version="bubblewrap 0.8.0", contract_fd=97, seccomp_fd=98,
        )
        activation = self.runtime / "evidence" / "private-ocr-activation" / "current"
        activation_bodies = {path.name: path.read_bytes() for path in activation.iterdir()}
        self.assertTrue(cleanup_private_ocr_campaign(run, inputs, campaign))
        self.assertTrue(cleanup_private_ocr_campaign(run, inputs, campaign))
        self.assertEqual(
            activation_bodies,
            {path.name: path.read_bytes() for path in activation.iterdir()},
        )
        self.assertFalse((repository_root() / "sources" / run.staging_name).exists())
        self.assertFalse((self.runtime / "batches" / run.batch_id).exists())
        self.assertFalse((self.runtime / "private-ocr" / "rasters").exists())
        self.assertFalse((self.runtime / "private-ocr" / "corpus").exists())

    def test_campaign_cleanup_rejects_and_preserves_foreign_candidate_state(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        module._ocr_phase_sandbox_spec(
            run, inputs, "interrupted",
            command=[sys.executable, "-m", "ao_lore.private_ocr_uat_worker"],
            bwrap_version="bubblewrap 0.8.0", contract_fd=97, seccomp_fd=98,
        )
        candidate = repository_root() / "working" / "candidates" / run.staging_name
        foreign = candidate / "foreign"
        foreign.write_bytes(b"foreign")
        try:
            with self.assertRaises(Exception):
                cleanup_private_ocr_campaign(run, inputs, campaign)
            self.assertEqual(foreign.read_bytes(), b"foreign")
        finally:
            foreign.unlink()
            cleanup_private_ocr_campaign(run, inputs, campaign)

    def test_campaign_cleanup_rejects_dangling_foreign_quarantine_symlink(self):
        inputs, campaign, run = self._prepared_campaign_run()
        quarantine = (
            repository_root() / "working" / "candidates"
            / f".private-ocr-cleanup-{run.staging_name}"
        )
        quarantine.symlink_to(self.runtime / "missing-foreign-target")
        try:
            with self.assertRaises(Exception):
                cleanup_private_ocr_campaign(run, inputs, campaign)
            self.assertTrue(quarantine.is_symlink())
        finally:
            quarantine.unlink()
            cleanup_private_ocr_campaign(run, inputs, campaign)

    def test_campaign_cleanup_bounds_directory_only_tree_before_intent(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        candidate = repository_root() / "working" / "candidates" / run.staging_name
        candidate.mkdir(parents=True)
        for index in range(3):
            (candidate / f"foreign-{index}").mkdir()
        with patch.object(module, "_CLEANUP_MAX_ENTRIES", 2):
            with self.assertRaises(Exception):
                cleanup_private_ocr_campaign(run, inputs, campaign)
        self.assertEqual(len(tuple(candidate.iterdir())), 3)
        for path in candidate.iterdir():
            path.rmdir()
        candidate.rmdir()
        cleanup_private_ocr_campaign(run, inputs, campaign)

    def test_cleanup_reclaim_preserves_same_byte_inode_replacement(self):
        import ao_lore.private_ocr_uat as module

        tree = self.runtime / "owned-cleanup-tree"
        tree.mkdir()
        target = tree / "result.json"
        target.write_bytes(b"owned bytes")
        expected = module._cleanup_tree_manifest(tree)
        displaced = self.runtime / "displaced-owned-result.json"
        original_manifest = module._cleanup_tree_manifest
        swapped = False

        def swap_after_manifest(root):
            nonlocal swapped
            current = original_manifest(root)
            if not swapped:
                swapped = True
                target.rename(displaced)
                target.write_bytes(b"owned bytes")
            return current

        with patch.object(module, "_cleanup_tree_manifest", side_effect=swap_after_manifest):
            with self.assertRaises(Exception):
                module._reclaim_cleanup_tree(tree, expected)
        self.assertEqual(target.read_bytes(), b"owned bytes")
        self.assertEqual(displaced.read_bytes(), b"owned bytes")
        target.unlink()
        tree.rmdir()
        displaced.unlink()

    def test_cleanup_manifest_hashes_large_file_with_bounded_memory(self):
        import ao_lore.private_ocr_uat as module

        tree = self.runtime / "large-cleanup-tree"
        tree.mkdir()
        target = tree / "result.bin"
        chunk = b"bounded cleanup bytes" * 4096
        with target.open("wb") as stream:
            for _ in range(192):
                stream.write(chunk)
        tracemalloc.start()
        try:
            manifest = module._cleanup_tree_manifest(tree)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(manifest["entries"][0]["size"], target.stat().st_size)
        self.assertLess(peak, 4 * 1024 * 1024)
        target.unlink()
        tree.rmdir()

    def test_campaign_cleanup_recovers_after_raster_quarantine_and_partial_reclaim(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        original_rename = module._rename_noreplace
        interrupted = False

        def interrupt_rename(parent, source, destination):
            nonlocal interrupted
            result = original_rename(parent, source, destination)
            if source == "rasters" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(module, "_rename_noreplace", side_effect=interrupt_rename):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_ocr_campaign(run, inputs, campaign)
        self.assertTrue(cleanup_private_ocr_campaign(run, inputs, campaign))

    def test_campaign_cleanup_recovers_partial_intent_write(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        module._ocr_phase_sandbox_spec(
            run, inputs, "interrupted",
            command=[sys.executable, "-m", "ao_lore.private_ocr_uat_worker"],
            bwrap_version="bubblewrap 0.8.0", contract_fd=97, seccomp_fd=98,
        )
        original_write = module.os.write
        interrupted = False

        def interrupt_write(descriptor, body):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                original_write(descriptor, body[: max(1, len(body) // 2)])
                raise KeyboardInterrupt()
            return original_write(descriptor, body)

        with patch.object(module.os, "write", side_effect=interrupt_write):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_ocr_campaign(run, inputs, campaign)
        self.assertTrue(cleanup_private_ocr_campaign(run, inputs, campaign))

    def test_campaign_cleanup_preserves_foreign_partial_intent(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        module._ocr_phase_sandbox_spec(
            run, inputs, "interrupted",
            command=[sys.executable, "-m", "ao_lore.private_ocr_uat_worker"],
            bwrap_version="bubblewrap 0.8.0", contract_fd=97, seccomp_fd=98,
        )
        partial = (
            self.runtime / "uat" / "private-ocr" / run.batch_id
            / "cleanup-intent.partial"
        )
        partial.write_bytes(b"foreign")
        try:
            with self.assertRaises(Exception):
                cleanup_private_ocr_campaign(run, inputs, campaign)
            self.assertEqual(partial.read_bytes(), b"foreign")
        finally:
            partial.unlink()
        self.assertTrue(cleanup_private_ocr_campaign(run, inputs, campaign))

    def test_campaign_cleanup_recovers_after_intent_removal_before_state_rmdir(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        original_rmdir = module.os.rmdir
        interrupted = False

        def interrupt_state_rmdir(path, *args, **kwargs):
            nonlocal interrupted
            if os.fspath(path) == os.fspath(
                self.runtime / "uat" / "private-ocr" / run.batch_id
            ) and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return original_rmdir(path, *args, **kwargs)

        with patch.object(module.os, "rmdir", side_effect=interrupt_state_rmdir):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_ocr_campaign(run, inputs, campaign)
        self.assertTrue(cleanup_private_ocr_campaign(run, inputs, campaign))

        # A fresh run proves that deletion after one verified file is also resumable.
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        campaign = prepare_private_ocr_campaign_inputs(
            runtime_root=self.runtime, activation_inputs=inputs,
            renderer_digest=DIGEST_B,
            renderer=lambda *_: (RasterPageOutput(600, 300, png),),
        )
        run = prepare_private_ocr_uat_run(
            runtime_root=self.runtime, activation_inputs=inputs, campaign=campaign
        )
        self.prepared_runs.append(run)
        original_unlink = module.os.unlink
        deleted = False

        def interrupt_unlink(path, *args, **kwargs):
            nonlocal deleted
            result = original_unlink(path, *args, **kwargs)
            if ".rasters-cleanup-" in os.fspath(path) and not deleted:
                deleted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(module.os, "unlink", side_effect=interrupt_unlink):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_ocr_campaign(run, inputs, campaign)
        self.assertTrue(cleanup_private_ocr_campaign(run, inputs, campaign))

    def _shared_run(self):
        policy_digest = private_document_policy_digest(PRIVATE_OCR_UAT_POLICY)
        manifest_digest = canonical_digest(self.manifest)
        return PrivateDocumentPreparedRun(
            format_id="ocr",
            policy_digest=policy_digest,
            batch_id=self.batch["batch_id"],
            manifest=copy.deepcopy(self.manifest),
            manifest_digest=manifest_digest,
            manifest_identity=(3, 4),
            staging_name="private-ocr-uat-fixture",
            control_identity=(1, 2),
            source_bindings=tuple(
                private_document_state_body(
                    PRIVATE_OCR_UAT_POLICY,
                    item_id=item["item_id"],
                    source_digest=item["source_digest"],
                )
                for item in self.documents
            ),
            qualification_binding=private_document_state_body(
                PRIVATE_OCR_UAT_POLICY,
                manifest_digest=manifest_digest,
                manifest_identity=[3, 4],
                qualification={
                    "qualification_digest": DIGEST_A,
                    "runtime_digest": DIGEST_B,
                    "model_set_digest": DIGEST_C,
                    "selected_candidate_id": "candidate-1",
                    "campaign_origin_digest": DIGEST_A,
                },
            ),
            expectation_binding=private_document_state_body(
                PRIVATE_OCR_UAT_POLICY,
                manifest_digest=manifest_digest,
                manifest_identity=[3, 4],
                item_ids=[item["item_id"] for item in self.documents],
                source_digests=[item["source_digest"] for item in self.documents],
            ),
        )

    def test_exact_campaign_is_one_three_zero_and_publishes_after_cleanup(self):
        events = []
        conversions = iter((0, 1, 4, 4))
        processes = [object(), object(), object()]
        process_results = [
            {"returncode": -signal.SIGINT, "stdout": b"", "stderr": b""},
            {"returncode": 0, "stdout": b"terminal", "stderr": b""},
            {"returncode": 0, "stdout": b"terminal", "stderr": b""},
        ]
        initial_checkpoint = {
            "schema_version": "ao.lore.ingest-batch-checkpoint.v0.1",
            "batch_id": self.batch["batch_id"],
            "manifest_digest": canonical_digest(self.batch),
            "manifest_items": [
                {"item_id": item["item_id"], "source_digest": item["source_digest"]}
                for item in self.documents
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
        checkpoint = {
            **{key: copy.deepcopy(value) for key, value in initial_checkpoint.items() if key not in {
                "items", "created", "processed", "next_item_index",
                "previous_checkpoint_digest", "checkpoint_digest",
            }},
            "items": [{
                "item_id": self.documents[0]["item_id"],
                "source_digest": self.documents[0]["source_digest"],
                "status": "created",
                "candidate_id": "candidate-ocr-01",
                "candidate_digest": DIGEST_B,
                "provenance_digest": DIGEST_C,
                "review_status": "unreviewed",
            }],
            "created": 1,
            "unchanged": 0,
            "rejected": 0,
            "processed": 1,
            "next_item_index": 1,
            "previous_checkpoint_digest": initial_checkpoint["checkpoint_digest"],
        }
        checkpoint["checkpoint_digest"] = canonical_digest(checkpoint)
        terminal = {"items": copy.deepcopy(self.documents)}
        run = _Run(
            batch_manifest=copy.deepcopy(self.batch),
            batch_manifest_digest=canonical_digest(self.batch),
            corpus_manifest=copy.deepcopy(self.manifest),
            corpus_root=self.runtime / "corpus",
        )

        def assemble(_run, _terminal, _checkpoint, brain, calibration, counts):
            events.append("assemble")
            self.assertEqual(calibration, {"attempts_per_item": 2, "aggregate_digest": DIGEST_C})
            return {
                "schema_version": "ao.lore.private-ocr-uat-readback.v0.1",
                "policy_digest": OCR_POLICY_DIGEST,
                "qualification_digest": DIGEST_A,
                "corpus_digest": DIGEST_B,
                "aggregate_digest": DIGEST_C,
                "initial_conversions": counts["initial"],
                "resumed_conversions": counts["resumed"],
                "rerun_conversions": counts["rerun"],
                "successful_documents": 4,
                "rejected_documents": 0,
                "calibration_attempts_per_item": 2,
                "decision": "hold",
                "brain_before_digest": brain,
                "brain_after_work_digest": brain,
                "brain_after_cleanup_digest": brain,
                **{field: False for field in AUTHORITY_FIELDS},
            }

        shared = PrivateDocumentUatDependencies(
            policy=PRIVATE_OCR_UAT_POLICY,
            prepare_run=lambda manifest, runtime: events.append("prepare") or run,
            launch_process=lambda current, phase: events.append(f"launch:{phase}") or processes.pop(0),
            calibration=lambda manifest, root: events.append("calibration") or {"attempts_per_item": 2, "aggregate_digest": DIGEST_C},
            cleanup=lambda current: events.append("cleanup") or True,
            brain_snapshot=lambda: events.append("brain") or DIGEST_A,
            candidate_snapshot=lambda: {},
            load_manifest=lambda runtime: copy.deepcopy(self.manifest),
            validate_prepared_run=lambda current, manifest, runtime: current,
            project_run=lambda current: self._shared_run(),
            conversion_snapshot=conversions.__next__,
            process_poll=lambda process: None,
            inspect_checkpoint=lambda current: copy.deepcopy(checkpoint),
            inspect_barrier=lambda current, observed: True,
            send_signal=lambda process: events.append("signal"),
            terminate_process=lambda process: events.append("terminate"),
            wait_process=lambda process: process_results.pop(0),
            load_final=lambda current: copy.deepcopy(terminal),
            load_queue=lambda: {},
            verify_terminal=lambda current, stdout, loaded: copy.deepcopy(loaded),
            verify_queue=lambda queue, current, before: copy.deepcopy(current["items"]),
            assemble_readback=assemble,
            persist_readback=lambda current, report, after_work, after_cleanup: events.append("persist") or DIGEST_C,
            require_verified_process=lambda process, phase: None,
            expected_item_ids=lambda manifest: [item["item_id"] for item in manifest["documents"]],
            checkpoint_timeout=1.0,
            poll_interval=0.01,
        )
        report = run_private_ocr_uat(
            runtime_root=self.runtime,
            dependencies=OcrUatDependencies(shared=shared),
        )
        self.assertEqual((report["initial_conversions"], report["resumed_conversions"], report["rerun_conversions"]), (1, 3, 0))
        self.assertEqual(report["calibration_attempts_per_item"], 2)
        self.assertEqual(3, events.count("brain"))
        self.assertLess(events.index("cleanup"), events.index("assemble"))
        self.assertLess(events.index("assemble"), events.index("persist"))

    def test_default_dependencies_bind_one_exact_activation_and_campaign(self):
        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        campaign = prepare_private_ocr_campaign_inputs(
            runtime_root=self.runtime,
            activation_inputs=inputs,
            renderer_digest=DIGEST_B,
            renderer=lambda *_: (RasterPageOutput(600, 300, png),),
        )
        dependencies = build_private_ocr_uat_dependencies(
            activation_inputs=inputs, campaign=campaign
        )
        self.assertIs(type(dependencies), OcrUatDependencies)
        self.assertEqual(dependencies.shared.policy, PRIVATE_OCR_UAT_POLICY)
        shared_manifest = dependencies.shared.load_manifest(self.runtime)
        self.assertEqual(set(shared_manifest), {"corpus_id", "documents"})
        self.assertEqual(
            list(dependencies.shared.expected_item_ids(shared_manifest)),
            [item["item_id"] for item in campaign.sources],
        )
        prepared = dependencies.shared.prepare_run(shared_manifest, self.runtime)
        self.prepared_runs.append(prepared)
        self.assertEqual(
            prepared.corpus_root, self.runtime / "private-ocr" / "corpus"
        )

    def test_fixed_actions_reopen_activation_prepare_inputs_and_clean_them(self):
        import ao_lore.private_ocr_uat as module

        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        with (
            patch.dict(os.environ, {"AO_LORE_HOME": str(self.runtime)}),
            patch.object(
                module,
                "render_in_ocr_sandbox",
                side_effect=lambda *_args, **_kwargs: (
                    RasterPageOutput(600, 300, png),
                ),
            ),
        ):
            self.assertEqual(
                project_private_ocr_action("qualify")["status"], "qualified"
            )
            self.assertEqual(
                project_private_ocr_action("prepare-corpus")["status"], "prepared"
            )
            self.assertEqual(
                project_private_ocr_action("cleanup")["status"], "cleaned"
            )
        self.assertTrue(
            (self.runtime / "evidence" / "private-ocr-activation" / "current").is_dir()
        )
        self.assertFalse((self.runtime / "private-ocr" / "corpus").exists())
        self.assertFalse((self.runtime / "private-ocr" / "rasters").exists())

    def test_fixed_cleanup_reclaims_partial_campaign_preparation(self):
        import ao_lore.private_ocr_uat as module

        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        calls = 0

        def fail_second(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ContractError("reviewed renderer failure")
            return (RasterPageOutput(600, 300, png),)

        with self.assertRaises(Exception):
            prepare_private_ocr_campaign_inputs(
                runtime_root=self.runtime, activation_inputs=inputs,
                renderer_digest=DIGEST_B, renderer=fail_second,
            )
        self.assertEqual(len(tuple((self.runtime / "private-ocr" / "rasters").iterdir())), 1)
        with patch.dict(os.environ, {"AO_LORE_HOME": str(self.runtime)}):
            self.assertEqual(project_private_ocr_action("cleanup")["status"], "cleaned")
        self.assertFalse((self.runtime / "private-ocr" / "rasters").exists())
        self.assertFalse((self.runtime / "private-ocr" / "corpus").exists())

    def test_fixed_cleanup_recovers_partial_preparation_after_raster_reclaim(self):
        import ao_lore.private_ocr_uat as module

        inputs = self._activation_inputs()
        persist_private_ocr_activation(runtime_root=self.runtime, inputs=inputs)
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        png = (
            Path(__file__).parent / "fixtures" / "ao_lore" / "ocr" / "clean.png"
        ).read_bytes()
        calls = 0

        def fail_second(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("reviewed renderer failure")
            return (RasterPageOutput(600, 300, png),)

        with self.assertRaises(Exception):
            prepare_private_ocr_campaign_inputs(
                runtime_root=self.runtime, activation_inputs=inputs,
                renderer_digest=DIGEST_B, renderer=fail_second,
            )
        original = module._reclaim_cleanup_tree
        interrupted = False

        def interrupt_after_reclaim(root, records):
            nonlocal interrupted
            result = original(root, records)
            if root.name == ".rasters-preparation-reclaim" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(module, "_reclaim_cleanup_tree", side_effect=interrupt_after_reclaim):
            with self.assertRaises(KeyboardInterrupt):
                module.cleanup_private_ocr_prepared_inputs(
                    runtime_root=self.runtime, activation_inputs=inputs,
                )
        with patch.dict(os.environ, {"AO_LORE_HOME": str(self.runtime)}):
            self.assertEqual(project_private_ocr_action("cleanup")["status"], "cleaned")
        self.assertFalse((self.runtime / "private-ocr" / "corpus").exists())

    def test_fixed_cleanup_reopens_and_reclaims_one_hard_crashed_run(self):
        inputs, _campaign, run = self._prepared_campaign_run()
        with patch.dict(os.environ, {"AO_LORE_HOME": str(self.runtime)}):
            self.assertEqual(
                project_private_ocr_action("cleanup")["status"], "cleaned"
            )
        self.prepared_runs.remove(run)
        self.assertFalse(run.control_path.exists())
        self.assertFalse((repository_root() / "sources" / run.staging_name).exists())
        self.assertFalse((self.runtime / "private-ocr" / "rasters").exists())
        self.assertTrue(
            (self.runtime / "evidence" / "private-ocr-activation" / "current").is_dir()
        )

    def test_fixed_cleanup_recovers_after_raster_reclaim_hard_crash(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        module._ocr_phase_sandbox_spec(
            run, inputs, "interrupted",
            command=[sys.executable, "-m", "ao_lore.private_ocr_uat_worker"],
            bwrap_version="bubblewrap 0.8.0", contract_fd=97, seccomp_fd=98,
        )
        original = module._reclaim_cleanup_tree
        interrupted = False

        def interrupt_after_rasters(root, records):
            nonlocal interrupted
            result = original(root, records)
            if root.name.startswith(".rasters-cleanup-") and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(module, "_reclaim_cleanup_tree", side_effect=interrupt_after_rasters):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_ocr_campaign(run, inputs, campaign)
        self.assertFalse((self.runtime / "private-ocr" / "rasters").exists())
        with patch.dict(os.environ, {"AO_LORE_HOME": str(self.runtime)}):
            self.assertEqual(project_private_ocr_action("cleanup")["status"], "cleaned")
        self.assertFalse(run.control_path.exists())
        self.assertFalse((repository_root() / "sources" / run.staging_name).exists())

    def test_fixed_cleanup_recovers_after_corpus_reclaim_hard_crash(self):
        import ao_lore.private_ocr_uat as module

        inputs, campaign, run = self._prepared_campaign_run()
        module._ocr_phase_sandbox_spec(
            run, inputs, "interrupted",
            command=[sys.executable, "-m", "ao_lore.private_ocr_uat_worker"],
            bwrap_version="bubblewrap 0.8.0", contract_fd=97, seccomp_fd=98,
        )
        original = module.cleanup_private_ocr_corpus
        interrupted = False

        def interrupt_after_corpus(*, runtime_root):
            nonlocal interrupted
            result = original(runtime_root=runtime_root)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(
            module, "cleanup_private_ocr_corpus", side_effect=interrupt_after_corpus,
        ):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_ocr_campaign(run, inputs, campaign)
        self.assertFalse(run.control_path.exists())
        self.assertFalse((self.runtime / "private-ocr" / "corpus").exists())
        with patch.dict(os.environ, {"AO_LORE_HOME": str(self.runtime)}):
            self.assertEqual(project_private_ocr_action("cleanup")["status"], "cleaned")
        self.assertFalse(
            (self.runtime / "uat" / "private-ocr" / run.batch_id).exists()
        )

    def test_preparation_rejects_a_second_live_run(self):
        inputs, campaign, run = self._prepared_campaign_run()
        with self.assertRaises(Exception):
            prepare_private_ocr_uat_run(
                runtime_root=self.runtime,
                activation_inputs=inputs,
                campaign=campaign,
            )
        self.assertTrue(run.control_path.is_dir())

    def test_qualification_binding_requires_runtime_model_selection_and_origin(self):
        validator = PRIVATE_OCR_UAT_POLICY.validate_qualification
        value = {
            "qualification_digest": DIGEST_A,
            "runtime_digest": DIGEST_B,
            "model_set_digest": DIGEST_C,
            "selected_candidate_id": "candidate-1",
            "campaign_origin_digest": DIGEST_A,
        }
        self.assertEqual(validator(value), value)
        for field in tuple(value):
            with self.subTest(field=field):
                drifted = dict(value)
                del drifted[field]
                with self.assertRaises(Exception):
                    validator(drifted)

    def _origin_contract(self):
        return {
            "qualification_digest": DIGEST_A,
            "runtime_digest": DIGEST_B,
            "model_set_digest": DIGEST_C,
            "selected_candidate_id": "candidate-1",
            "campaign_origin_digest": DIGEST_A,
            "configuration_digest": DIGEST_B,
            "batch_id": self.batch["batch_id"],
            "batch_manifest_digest": canonical_digest(self.batch),
            "batch_manifest": copy.deepcopy(self.batch),
            "staging_name": "private-ocr-uat-fixture",
            "sources": [
                {
                    "item_id": item["item_id"],
                    "source_digest": item["source_digest"],
                    "raster_manifest_digest": DIGEST_C,
                }
                for item in self.documents
            ],
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def test_worker_phase_contract_binds_exact_arithmetic_and_origin(self):
        for phase, start, end in (
            ("interrupted", 0, 1), ("resume", 1, 4), ("rerun", 4, 4)
        ):
            with self.subTest(phase=phase):
                value = {
                    "schema_version": "ao.lore.private-ocr-uat-worker-contract.v0.1",
                    "phase": phase,
                    "expected_start": start,
                    "expected_end": end,
                    **self._origin_contract(),
                }
                self.assertEqual(validate_uat_worker_contract(value), value)
                drifted = dict(value)
                drifted["expected_end"] = (end + 1) % 5
                with self.assertRaises(Exception):
                    validate_uat_worker_contract(drifted)

    def test_worker_loads_only_exact_canonical_immutable_contract_bytes(self):
        from ao_lore.private_ocr_uat_worker import load_immutable_contract

        value = {
            "schema_version": "ao.lore.private-ocr-uat-worker-contract.v0.1",
            "phase": "interrupted", "expected_start": 0, "expected_end": 1,
            **self._origin_contract(),
        }
        path = self.runtime / "worker-contract.json"
        canonical = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        path.write_bytes(canonical)
        self.assertEqual(load_immutable_contract(path), value)
        path.write_bytes(json.dumps(value, indent=2).encode("ascii"))
        with self.assertRaises(Exception):
            load_immutable_contract(path)

    def test_worker_accepts_only_the_zero_link_bwrap_contract_mount(self):
        import ao_lore.private_ocr_uat_worker as worker

        value = {
            "schema_version": "ao.lore.private-ocr-uat-worker-contract.v0.1",
            "phase": "interrupted", "expected_start": 0, "expected_end": 1,
            **self._origin_contract(),
        }
        path = self.runtime / "worker-contract.json"
        path.write_bytes(json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii"))
        real_lstat, real_fstat = os.lstat, os.fstat

        def zero_link(info):
            return SimpleNamespace(
                st_mode=info.st_mode, st_nlink=0, st_size=info.st_size,
                st_dev=info.st_dev, st_ino=info.st_ino,
                st_mtime_ns=info.st_mtime_ns,
            )

        with (
            patch.object(worker.os, "lstat", side_effect=lambda target: zero_link(real_lstat(target))),
            patch.object(worker.os, "fstat", side_effect=lambda descriptor: zero_link(real_fstat(descriptor))),
        ):
            self.assertEqual(
                worker.load_immutable_contract(path, mount_bound=True), value
            )
            with self.assertRaises(Exception):
                worker.load_immutable_contract(path)

    def test_calibration_loader_requests_the_zero_link_bwrap_contract_mount(self):
        import ao_lore.private_ocr_calibration_worker as worker

        value = {
            "schema_version": "ao.lore.private-ocr-calibration-contract.v0.1",
            "attempts_per_item": 2,
            **self._origin_contract(),
        }
        body = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        with patch.object(worker, "_read_bounded", return_value=body) as read:
            self.assertEqual(
                worker.load_immutable_calibration_contract(
                    self.runtime / "worker-contract.json", mount_bound=True
                ),
                value,
            )
        read.assert_called_once_with(
            self.runtime / "worker-contract.json", 1024 * 1024,
            "calibration contract", expected_nlink=0,
        )

    def test_worker_resume_binds_activation_rasters_and_parent_network_isolation(self):
        import ao_lore.private_ocr_uat_worker as worker
        from tests.test_ao_lore_ocr_ingestion import activation

        contract = {
            "schema_version": "ao.lore.private-ocr-uat-worker-contract.v0.1",
            "phase": "resume", "expected_start": 1, "expected_end": 4,
            **self._origin_contract(),
        }
        bundles = {
            source["source_digest"]: SimpleNamespace(source_id=source["item_id"])
            for source in contract["sources"]
        }
        captured = {}

        def dependencies(active, execute):
            captured["activation"] = active
            captured["execute"] = execute
            return object()

        def ingest(manifest, digest, *, dependencies, batch_root):
            captured["batch_dependencies"] = dependencies
            captured["batch_root"] = batch_root
            for source in contract["sources"]:
                captured.setdefault("results", []).append(
                    captured["execute"]({"digest": source["source_digest"]})
                )
            return {"schema_version": "fixture", "items": []}

        with (
            patch.object(worker, "_current_processed", side_effect=(1, 4)),
            patch.object(worker, "_load_activation", return_value=(activation(), DIGEST_A)),
            patch.object(worker, "_load_rasters", return_value=bundles),
            patch.object(worker, "default_ocr_ingestion_dependencies", side_effect=dependencies),
            patch.object(worker, "ingest_verified_batch", side_effect=ingest),
            patch.object(worker, "run_paddle_ocr_worker", return_value={"worker": "result"}) as paddle,
        ):
            result = worker.execute_phase(contract)
        self.assertEqual(result, {"schema_version": "fixture", "items": []})
        self.assertEqual(captured["activation"], activation())
        self.assertEqual(captured["results"], [{"worker": "result"}] * 4)
        self.assertEqual(paddle.call_count, 4)
        self.assertTrue(all(
            call.kwargs["parent_network_isolated"] is True
            for call in paddle.call_args_list
        ))

    def test_worker_reprobes_the_offline_gpu_instead_of_reading_qualification_metadata(self):
        import ao_lore.private_ocr_uat as uat
        import ao_lore.private_ocr_uat_worker as worker

        inputs = self._activation_inputs()
        _checked, activation, record = uat._validate_activation_inputs(inputs)
        activation_body = json.dumps(
            record, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii") + b"\n"
        contract = self._origin_contract()
        contract.update({
            "qualification_digest": inputs.qualification_digest,
            "runtime_digest": inputs.runtime_digest,
            "model_set_digest": activation.model_set_digest,
            "selected_candidate_id": activation.candidate_id,
        })

        def retained(path, maximum, label, *, expected_nlink=1):
            del path, maximum, expected_nlink
            return (
                inputs.qualification_body
                if label == "qualification" else activation_body
            )

        gpu = SimpleNamespace(device_uuid_digest=DIGEST_B)
        with (
            patch.object(worker, "_read_bounded", side_effect=retained),
            patch.object(uat, "load_private_ocr_activation", return_value=activation),
            patch.object(worker, "probe_offline_gpu_runtime", return_value=gpu) as probe,
        ):
            self.assertEqual(
                worker._load_activation(contract), (activation, DIGEST_B)
            )
        probe.assert_called_once_with(
            Path("/ocr-runtime"), parent_network_isolated=True,
        )

    def test_calibration_contract_is_exactly_two_attempts_per_item(self):
        value = {
            "schema_version": "ao.lore.private-ocr-calibration-contract.v0.1",
            "attempts_per_item": 2,
            **self._origin_contract(),
        }
        self.assertEqual(validate_calibration_contract(value), value)
        for attempts in (0, 1, 3, True):
            with self.subTest(attempts=attempts):
                drifted = dict(value)
                drifted["attempts_per_item"] = attempts
                with self.assertRaises(Exception):
                    validate_calibration_contract(drifted)

    def _readback(self):
        return {
            "schema_version": "ao.lore.private-ocr-uat-readback.v0.1",
            "policy_digest": OCR_POLICY_DIGEST,
            "qualification_digest": DIGEST_A,
            "corpus_digest": DIGEST_B,
            "aggregate_digest": self._calibration_result()["aggregate_digest"],
            "initial_conversions": 1,
            "resumed_conversions": 3,
            "rerun_conversions": 0,
            "successful_documents": 4,
            "rejected_documents": 0,
            "calibration_attempts_per_item": 2,
            "decision": "hold",
            "brain_before_digest": DIGEST_A,
            "brain_after_work_digest": DIGEST_A,
            "brain_after_cleanup_digest": DIGEST_A,
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def _calibration_result(self):
        items = [
            {
                "item_id": item["item_id"],
                "raster_manifest_digest": DIGEST_C,
                "page_count": 1,
                "detected_lines": 1,
                "attempt_digests": [DIGEST_A, DIGEST_A],
                "identical": True,
            }
            for item in self.documents
        ]
        aggregate_digest = canonical_digest({
            "schema_version": "ao.lore.private-ocr-calibration-aggregate.v0.1",
            "attempts_per_item": 2,
            "items": items,
            "decision": "hold",
            "qualification_digest": DIGEST_A,
            "campaign_origin_digest": DIGEST_C,
            **{field: False for field in AUTHORITY_FIELDS},
        })
        return {
            "schema_version": "ao.lore.private-ocr-calibration-result.v0.1",
            "attempts_per_item": 2,
            "aggregate_digest": aggregate_digest,
            "decision": "hold",
            "items": items,
            **{field: False for field in AUTHORITY_FIELDS},
        }

    def test_terminal_evidence_publishes_exactly_two_files_after_equal_brains(self):
        prepared = PreparedOcrUat(
            runtime_root=self.runtime,
            batch_id=self.batch["batch_id"],
            batch_manifest_digest=canonical_digest(self.batch),
            corpus_digest=DIGEST_B,
            qualification_digest=DIGEST_A,
            campaign_origin_digest=DIGEST_C,
        )
        digest = persist_private_ocr_evidence(
            prepared,
            self._readback(),
            calibration_result=self._calibration_result(),
            brain_before=DIGEST_A,
            brain_after_work=DIGEST_A,
            brain_after_cleanup=DIGEST_A,
        )
        root = self.runtime / "evidence" / "private-ocr-uat" / self.batch["batch_id"]
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(
            {"private-ocr-uat-readback.json", "cleaned-state.json"},
            {path.name for path in root.iterdir()},
        )
        self.assertEqual(
            digest,
            persist_private_ocr_evidence(
                prepared,
                self._readback(),
                calibration_result=self._calibration_result(),
                brain_before=DIGEST_A,
                brain_after_work=DIGEST_A,
                brain_after_cleanup=DIGEST_A,
            ),
        )

    def test_terminal_evidence_rejects_brain_drift_before_creating_evidence(self):
        prepared = PreparedOcrUat(
            runtime_root=self.runtime,
            batch_id=self.batch["batch_id"],
            batch_manifest_digest=canonical_digest(self.batch),
            corpus_digest=DIGEST_B,
            qualification_digest=DIGEST_A,
            campaign_origin_digest=DIGEST_C,
        )
        with self.assertRaises(Exception):
            persist_private_ocr_evidence(
                prepared,
                self._readback(),
                calibration_result=self._calibration_result(),
                brain_before=DIGEST_A,
                brain_after_work=DIGEST_B,
                brain_after_cleanup=DIGEST_A,
            )
        self.assertFalse((self.runtime / "evidence").exists())

    def test_terminal_evidence_recovers_after_readback_write_crash(self):
        import ao_lore.private_ocr_uat as module

        prepared = PreparedOcrUat(
            runtime_root=self.runtime,
            batch_id=self.batch["batch_id"],
            batch_manifest_digest=canonical_digest(self.batch),
            corpus_digest=DIGEST_B,
            qualification_digest=DIGEST_A,
            campaign_origin_digest=DIGEST_C,
        )
        original = module._write_owned

        def interrupt(parent, name, body):
            original(parent, name, body)
            if name == "private-ocr-uat-readback.json":
                raise KeyboardInterrupt()

        with patch.object(module, "_write_owned", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                persist_private_ocr_evidence(
                    prepared,
                    self._readback(),
                    calibration_result=self._calibration_result(),
                    brain_before=DIGEST_A,
                    brain_after_work=DIGEST_A,
                    brain_after_cleanup=DIGEST_A,
                )
        digest = persist_private_ocr_evidence(
            prepared,
            self._readback(),
            calibration_result=self._calibration_result(),
            brain_before=DIGEST_A,
            brain_after_work=DIGEST_A,
            brain_after_cleanup=DIGEST_A,
        )
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        final = self.runtime / "evidence" / "private-ocr-uat" / self.batch["batch_id"]
        self.assertEqual(2, len(tuple(final.iterdir())))

    def test_terminal_evidence_recovers_after_intent_removal_before_rename(self):
        import ao_lore.private_ocr_uat as module

        prepared = PreparedOcrUat(
            runtime_root=self.runtime,
            batch_id=self.batch["batch_id"],
            batch_manifest_digest=canonical_digest(self.batch),
            corpus_digest=DIGEST_B,
            qualification_digest=DIGEST_A,
            campaign_origin_digest=DIGEST_C,
        )
        with patch.object(module, "_rename_noreplace", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                persist_private_ocr_evidence(
                    prepared,
                    self._readback(),
                    calibration_result=self._calibration_result(),
                    brain_before=DIGEST_A,
                    brain_after_work=DIGEST_A,
                    brain_after_cleanup=DIGEST_A,
                )
        digest = persist_private_ocr_evidence(
            prepared,
            self._readback(),
            calibration_result=self._calibration_result(),
            brain_before=DIGEST_A,
            brain_after_work=DIGEST_A,
            brain_after_cleanup=DIGEST_A,
        )
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

    def test_terminal_evidence_reopens_only_when_readback_and_cleanup_cross_bind(self):
        prepared = PreparedOcrUat(
            runtime_root=self.runtime,
            batch_id=self.batch["batch_id"],
            batch_manifest_digest=canonical_digest(self.batch),
            corpus_digest=DIGEST_B,
            qualification_digest=DIGEST_A,
            campaign_origin_digest=DIGEST_C,
        )
        persist_private_ocr_evidence(
            prepared,
            self._readback(),
            calibration_result=self._calibration_result(),
            brain_before=DIGEST_A,
            brain_after_work=DIGEST_A,
            brain_after_cleanup=DIGEST_A,
        )
        self.assertEqual(load_private_ocr_evidence(prepared), self._readback())
        cleaned = (
            self.runtime / "evidence" / "private-ocr-uat" / self.batch["batch_id"]
            / "cleaned-state.json"
        )
        body = cleaned.read_bytes()
        cleaned.chmod(0o600)
        cleaned.write_bytes(body.replace(b'"authority":false', b'"authority":true'))
        with self.assertRaises(Exception):
            load_private_ocr_evidence(prepared)

    def test_terminal_evidence_recomputes_retained_calibration_aggregate(self):
        prepared = PreparedOcrUat(
            runtime_root=self.runtime,
            batch_id=self.batch["batch_id"],
            batch_manifest_digest=canonical_digest(self.batch),
            corpus_digest=DIGEST_B,
            qualification_digest=DIGEST_A,
            campaign_origin_digest=DIGEST_C,
        )
        persist_private_ocr_evidence(
            prepared,
            self._readback(),
            calibration_result=self._calibration_result(),
            brain_before=DIGEST_A,
            brain_after_work=DIGEST_A,
            brain_after_cleanup=DIGEST_A,
        )
        cleaned_path = (
            self.runtime / "evidence" / "private-ocr-uat" / self.batch["batch_id"]
            / "cleaned-state.json"
        )
        cleaned = json.loads(cleaned_path.read_text(encoding="ascii"))
        cleaned["calibration_result"]["items"][0]["detected_lines"] = 2
        cleaned_path.chmod(0o600)
        cleaned_path.write_bytes(
            json.dumps(
                cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        )
        with self.assertRaises(Exception):
            load_private_ocr_evidence(prepared)

    def test_fixed_reviewed_corpus_publishes_atomically_and_reopens_idempotently(self):
        first = prepare_private_ocr_corpus(runtime_root=self.runtime)
        corpus = self.runtime / "private-ocr" / "corpus"
        first_identity = corpus.stat().st_ino
        second = prepare_private_ocr_corpus(runtime_root=self.runtime)
        self.assertEqual(first, second)
        self.assertEqual(first_identity, corpus.stat().st_ino)
        self.assertEqual(first, load_private_ocr_corpus(runtime_root=self.runtime))
        self.assertEqual(
            ["seed-01", "seed-02", "seed-03", "seed-04"],
            [item["item_id"] for item in first["documents"]],
        )
        self.assertEqual(
            {"manifest.json", "input"},
            {path.name for path in corpus.iterdir()},
        )
        self.assertEqual(4, len(tuple((corpus / "input").iterdir())))

    def test_fixed_reviewed_corpus_recovers_after_partial_source_write(self):
        import ao_lore.private_ocr_uat as module

        original = module._write_owned

        def interrupt(parent, name, body):
            original(parent, name, body)
            if name == "seed-02.pdf":
                raise KeyboardInterrupt()

        with patch.object(module, "_write_owned", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_ocr_corpus(runtime_root=self.runtime)
        prepared = prepare_private_ocr_corpus(runtime_root=self.runtime)
        self.assertEqual(4, len(prepared["documents"]))
        self.assertFalse(
            (self.runtime / "private-ocr" / ".corpus.partial").exists()
        )

    def test_fixed_reviewed_corpus_recovers_after_intent_removal_before_publish(self):
        import ao_lore.private_ocr_uat as module

        original = module._rename_noreplace

        def interrupt(parent, source, destination):
            if source == ".corpus.partial":
                raise KeyboardInterrupt()
            return original(parent, source, destination)

        with patch.object(module, "_rename_noreplace", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_ocr_corpus(runtime_root=self.runtime)
        prepared = prepare_private_ocr_corpus(runtime_root=self.runtime)
        self.assertEqual(4, len(prepared["documents"]))
        self.assertTrue((self.runtime / "private-ocr" / "corpus").is_dir())

    def test_fixed_reviewed_corpus_cleanup_is_idempotent_and_evidence_safe(self):
        prepare_private_ocr_corpus(runtime_root=self.runtime)
        evidence = self.runtime / "evidence"
        evidence.mkdir()
        marker = evidence / "retained"
        marker.write_bytes(b"evidence")
        self.assertTrue(cleanup_private_ocr_corpus(runtime_root=self.runtime))
        self.assertTrue(cleanup_private_ocr_corpus(runtime_root=self.runtime))
        self.assertFalse((self.runtime / "private-ocr" / "corpus").exists())
        self.assertEqual(b"evidence", marker.read_bytes())

    def test_fixed_reviewed_corpus_cleanup_recovers_after_quarantine_rename(self):
        import ao_lore.private_ocr_uat as module

        prepare_private_ocr_corpus(runtime_root=self.runtime)
        original = module._rename_noreplace

        def interrupt(parent, source, destination):
            result = original(parent, source, destination)
            if source == "corpus" and destination == ".corpus.reclaim":
                raise KeyboardInterrupt()
            return result

        with patch.object(module, "_rename_noreplace", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                cleanup_private_ocr_corpus(runtime_root=self.runtime)
        self.assertTrue(cleanup_private_ocr_corpus(runtime_root=self.runtime))
        self.assertEqual(
            set(),
            {name for name in (self.runtime / "private-ocr").iterdir() if name.name != ".corpus.lock"},
        )

    def test_fixed_reviewed_corpus_cleanup_reclaims_exact_partial_preparation(self):
        import ao_lore.private_ocr_uat as module

        original = module._write_owned

        def interrupt(parent, name, body):
            original(parent, name, body)
            if name == "seed-01.pdf":
                raise KeyboardInterrupt()

        with patch.object(module, "_write_owned", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_ocr_corpus(runtime_root=self.runtime)
        self.assertTrue(cleanup_private_ocr_corpus(runtime_root=self.runtime))
        self.assertFalse(
            (self.runtime / "private-ocr" / ".corpus.partial").exists()
        )

    def test_fixed_reviewed_corpus_refuses_a_concurrent_operator(self):
        private = self.runtime / "private-ocr"
        private.mkdir()
        lock = private / ".corpus.lock"
        descriptor = lock.open("w+b")
        try:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(Exception):
                prepare_private_ocr_corpus(runtime_root=self.runtime)
            self.assertEqual({".corpus.lock"}, {path.name for path in private.iterdir()})
        finally:
            descriptor.close()

    def test_fixed_reviewed_corpus_loader_rejects_inode_swap_during_read(self):
        import ao_lore.private_ocr_uat as module

        prepare_private_ocr_corpus(runtime_root=self.runtime)
        original = module._read_at
        swapped = False

        def replace_after_read(parent, name, maximum=512 * 1024):
            nonlocal swapped
            body = original(parent, name, maximum)
            if name == "seed-01.pdf" and not swapped:
                swapped = True
                replacement = self.runtime / "replacement.pdf"
                replacement.write_bytes(body)
                os.replace(replacement, self.runtime / "private-ocr" / "corpus" / "input" / name)
            return body

        with patch.object(module, "_read_at", side_effect=replace_after_read):
            with self.assertRaises(Exception):
                load_private_ocr_corpus(runtime_root=self.runtime)


if __name__ == "__main__":
    unittest.main()
