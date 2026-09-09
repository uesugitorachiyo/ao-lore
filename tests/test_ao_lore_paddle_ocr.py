import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore._strict_io import ContractError
from ao_lore.ocr_raster import ReviewedOcrSource, rasterize_ocr_source, render_in_ocr_sandbox
from ao_lore.paddle_ocr import (
    OCR_WORKER_CONFIGURATION_DIGEST,
    OcrRuntimeBinding,
    build_ocr_worker_contract,
    run_paddle_ocr_worker,
    run_paddle_ocr_sandbox_probe,
    validate_ocr_worker_readback,
    _verify_parent_network_isolation,
)
from ao_lore.paddle_ocr_worker import _detection_records, _restore_polygon_to_source


RUNTIME_DIGEST = "sha256:6002a11549b0bcb58e23edb4e98badfc58677f149e1533d6b1d11ec4ffe142bf"
GPU_DIGEST = "sha256:1c189d7a9c0dc0536bb870a71fbb2a01856122b3e3932059dcb728a068b784c1"


class PaddleOcrWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = Path(__file__).parents[1] / ".ao-lore" / "ocr-runtime" / "ocr-runtime-v0.1"

    def tearDown(self):
        self.temp.cleanup()

    def test_device_mount_builder_rejects_non_character_nvidia_node(self):
        import ao_lore.paddle_ocr as module

        real_lstat = os.lstat

        def differing(path):
            if os.fspath(path) == "/dev/nvidia0":
                return os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return real_lstat(path)

        with patch.object(module.os, "lstat", side_effect=differing), self.assertRaises(ContractError):
            module._append_nvidia_devices([])

    def test_empty_recognition_hypotheses_are_not_semantic_detections(self):
        records = _detection_records(
            ["", "Visible English"], [0.1, 0.9],
            [
                [[0, 0], [5, 0], [5, 5], [0, 5]],
                [[1, 1], [9, 1], [9, 4], [1, 4]],
            ],
            angle=0, width=10, height=5,
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["index"], 0)
        self.assertEqual(records[0]["text"], "Visible English")
        for invalid in (None, b"text", "x" * 4097):
            with self.subTest(invalid=type(invalid).__name__), self.assertRaises(ContractError):
                _detection_records(
                    [invalid], [0.9], [[[1, 1], [9, 1], [9, 4], [1, 4]]],
                    angle=0, width=10, height=5,
                )

    def test_device_mount_builder_rejects_foreign_capability_name(self):
        import ao_lore.paddle_ocr as module

        caps = Path("/dev/nvidia-caps")
        foreign = Path("/dev/nvidia-caps/foreign")
        real_lstat = os.lstat

        def capability(path):
            if Path(path) == caps:
                return os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 0, 0, 0, 0, 0, 0, 0))
            if Path(path) == foreign:
                return os.stat_result((stat.S_IFCHR | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return real_lstat(path)

        with (
            patch.object(Path, "iterdir", return_value=iter((foreign,))),
            patch.object(module.os, "lstat", side_effect=capability),
            self.assertRaises(ContractError),
        ):
            module._append_nvidia_devices([])

    def test_parent_network_isolation_requires_an_active_kernel_filter(self):
        with self.assertRaises(ContractError):
            run_paddle_ocr_worker(
                OcrRuntimeBinding(self.runtime, RUNTIME_DIGEST, GPU_DIGEST),
                object(), "candidate-1", run_id="run-parent-network",
                parent_network_isolated=True,
            )

    def test_parent_network_isolation_allows_socket_creation_but_requires_denied_send(self):
        class Probe:
            closed = False

            def sendto(self, payload, address):
                del payload, address
                raise PermissionError(1, "denied")

            def close(self):
                self.closed = True

        probe = Probe()
        with patch("ao_lore.paddle_ocr.socket.socket", return_value=probe):
            _verify_parent_network_isolation()
        self.assertTrue(probe.closed)

    def _bundle(self):
        sample = self.runtime / "installed" / "models" / "PP-LCNet_x1_0_textline_ori_infer" / "img_textline180_demo_res.jpg"
        body = sample.read_bytes()
        reviewed = ReviewedOcrSource(
            "worker-sample", sample, "image/jpeg",
            "sha256:" + hashlib.sha256(body).hexdigest(), True,
        )
        return rasterize_ocr_source(
            reviewed,
            state_root=self.root / "raster",
            renderer_digest="sha256:" + "a" * 64,
            renderer=lambda data, media, config: render_in_ocr_sandbox(
                data, media, config, runtime_root=self.runtime
            ),
        )

    def test_contract_binds_runtime_candidate_and_raster(self):
        if not self.runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        runtime = OcrRuntimeBinding(self.runtime, RUNTIME_DIGEST, GPU_DIGEST)
        bundle = self._bundle()
        contract = build_ocr_worker_contract(runtime, bundle, "candidate-1", run_id="run-worker-01")
        self.assertEqual(contract["runtime_digest"], RUNTIME_DIGEST)
        self.assertEqual(contract["gpu_identity_digest"], GPU_DIGEST)
        self.assertEqual(contract["page_ids"], ["page-0001"])
        self.assertEqual(contract["candidate_id"], "candidate-1")
        self.assertEqual(
            OCR_WORKER_CONFIGURATION_DIGEST,
            "sha256:" + hashlib.sha256(
                b"ao-lore-paddle-ocr-worker-v0.1|gpu:0|cudnn-deterministic|cpu-deterministic|doc-orientation|textline-orientation|no-fallback|offline|bounded"
            ).hexdigest(),
        )

    def test_launcher_binds_paddle_deterministic_flags(self):
        if not self.runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        runtime = OcrRuntimeBinding(self.runtime, RUNTIME_DIGEST, GPU_DIGEST)

        def inspect(command, descriptors):
            del descriptors
            joined = tuple(command)
            for name in ("FLAGS_cudnn_deterministic", "FLAGS_cpu_deterministic"):
                position = joined.index(name)
                self.assertEqual(joined[position - 1], "--setenv")
                self.assertEqual(joined[position + 1], "True")
            raise ContractError("captured deterministic worker")

        with patch("ao_lore.paddle_ocr._run_bounded", side_effect=inspect):
            with self.assertRaisesRegex(ContractError, "captured deterministic worker"):
                run_paddle_ocr_worker(
                    runtime, self._bundle(), "candidate-3", run_id="run-determinism"
                )

    def test_orientation_polygons_are_restored_to_original_page_coordinates(self):
        polygon = [[10, 20], [30, 20], [30, 40], [10, 40]]
        self.assertEqual(_restore_polygon_to_source(polygon, 0, 100, 200), polygon)
        self.assertEqual(
            _restore_polygon_to_source(polygon, 90, 100, 200),
            [[80, 10], [80, 30], [60, 30], [60, 10]],
        )
        self.assertEqual(
            _restore_polygon_to_source(polygon, 180, 100, 200),
            [[90, 180], [70, 180], [70, 160], [90, 160]],
        )
        self.assertEqual(
            _restore_polygon_to_source(polygon, 270, 100, 200),
            [[20, 190], [20, 170], [40, 170], [40, 190]],
        )
        for bad in (-90, 45, 360, True):
            with self.subTest(bad=bad), self.assertRaises(ContractError):
                _restore_polygon_to_source(polygon, bad, 100, 200)

    def test_actual_worker_runs_medium_candidate_offline_and_returns_bound_result(self):
        if not self.runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        runtime = OcrRuntimeBinding(self.runtime, RUNTIME_DIGEST, GPU_DIGEST)
        result = run_paddle_ocr_worker(runtime, self._bundle(), "candidate-1", run_id="run-worker-01")
        checked = validate_ocr_worker_readback(result)
        self.assertEqual(checked, result)
        self.assertEqual(result["runtime_digest"], RUNTIME_DIGEST)
        self.assertEqual(result["candidate_id"], "candidate-1")
        self.assertGreater(result["latency_microseconds"], 0)
        self.assertGreater(result["peak_gpu_memory_bytes"], 0)
        self.assertEqual(result["pages"][0]["page_id"], "page-0001")
        self.assertGreaterEqual(len(result["pages"][0]["detections"]), 1)
        self.assertFalse(any(result[field] for field in (
            "network_accessed", "provider_calls", "promotion", "publication",
            "release", "deployment", "authority_advanced",
        )))
        self.assertEqual(list((self.root / "raster").glob(".worker-*")), [])

    def test_actual_worker_restores_right_angle_pages_to_source_coordinates(self):
        if not self.runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        runtime = OcrRuntimeBinding(self.runtime, RUNTIME_DIGEST, GPU_DIGEST)
        fixture_root = Path(__file__).parent / "fixtures" / "ao_lore" / "ocr"
        expected_text = {
            90: "ROTATED TEXT",
            180: "UPSIDE DOWN TEXT",
            270: "SIDEWAYS TEXT",
        }
        for angle in (90, 180, 270):
            fixture_id = f"rotated-{angle}"
            source = fixture_root / f"{fixture_id}.png"
            body = source.read_bytes()
            reviewed = ReviewedOcrSource(
                fixture_id, source, "image/png",
                "sha256:" + hashlib.sha256(body).hexdigest(), True,
            )
            bundle = rasterize_ocr_source(
                reviewed, state_root=self.root / "rotations",
                renderer_digest="sha256:" + "a" * 64,
                renderer=lambda data, media, config: render_in_ocr_sandbox(
                    data, media, config, runtime_root=self.runtime
                ),
            )
            result = run_paddle_ocr_worker(
                runtime, bundle, "candidate-1", run_id=f"run-{fixture_id}"
            )
            with self.subTest(angle=angle):
                self.assertEqual(
                    [item["text"] for item in result["pages"][0]["detections"]],
                    [expected_text[angle]],
                )
                page = result["pages"][0]
                self.assertTrue(all(
                    0 <= x <= page["width"] and 0 <= y <= page["height"]
                    for item in page["detections"] for x, y in item["polygon"]
                ))

    def test_actual_sandbox_denies_network_children_and_outside_writes(self):
        if not self.runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        proof = run_paddle_ocr_sandbox_probe(self.runtime)
        self.assertTrue(all(proof[field] for field in (
            "tcp_denied", "udp_sendto_denied", "udp_sendmsg_denied", "dns_denied",
            "unix_denied", "fork_denied", "vfork_denied", "clone_denied",
            "clone3_denied", "system_denied", "outside_write_denied",
            "result_write_allowed", "limits_exact", "network_namespace_unshared",
        )))

    def test_unknown_candidate_and_runtime_digest_drift_fail_before_worker(self):
        if not self.runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        runtime = OcrRuntimeBinding(self.runtime, RUNTIME_DIGEST, GPU_DIGEST)
        with self.assertRaisesRegex(ContractError, "candidate identity"):
            build_ocr_worker_contract(runtime, self._bundle(), "candidate-4", run_id="run-worker-01")
        with patch("ao_lore.paddle_ocr.validate_ocr_runtime_tree_digest", side_effect=ContractError("prepared OCR runtime digest drifted")):
            with self.assertRaisesRegex(ContractError, "digest drifted"):
                run_paddle_ocr_worker(runtime, self._bundle(), "candidate-1", run_id="run-worker-01")


if __name__ == "__main__":
    unittest.main()
