import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import tempfile
import tracemalloc
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import ao_lore.ocr_runtime as ocr_runtime
from ao_lore._strict_io import ContractError
from ao_lore.ocr_runtime import (
    GpuIdentity,
    OfficialArtifact,
    extract_locked_ocr_models,
    install_offline_ocr_runtime,
    probe_offline_gpu_runtime,
    prepare_ocr_runtime,
    validate_prepared_ocr_runtime,
)
from ao_lore.ocr_contracts import OCR_CANDIDATES


ROOT = Path(__file__).resolve().parents[1]


class OcrRuntimePreparationTests(unittest.TestCase):
    @staticmethod
    def _model_archive(name: str) -> bytes:
        output = io.BytesIO()
        root = name.removesuffix(".tar")
        with tarfile.open(fileobj=output, mode="w") as archive:
            directory = tarfile.TarInfo(root + "/")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
            body = ("sealed " + root).encode("ascii")
            member = tarfile.TarInfo(root + "/inference.pdiparams")
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
        return output.getvalue()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.temporary.name)
        self.source = self.root / "downloads"
        self.state = self.root / "state"
        self.source.mkdir()
        self.state.mkdir()
        files = {
            "packages/paddleocr-3.7.0-py3-none-any.whl": b"synthetic official paddleocr wheel",
            "packages/paddlex-3.7.0-py3-none-any.whl": b"synthetic official paddlex wheel",
            "packages/paddlepaddle_gpu-3.3.0-cp312-cp312-linux_x86_64.whl": b"synthetic official paddle gpu wheel",
            "packages/numpy-2.3.5-cp312-cp312-manylinux.whl": b"synthetic locked dependency wheel",
            **{f"models/{name}": self._model_archive(name) for name in (
                "PP-OCRv6_medium_det_infer.tar", "PP-OCRv6_medium_rec_infer.tar",
                "PP-OCRv6_small_det_infer.tar", "PP-OCRv6_small_rec_infer.tar",
                "PP-OCRv5_mobile_det_infer.tar", "en_PP-OCRv5_mobile_rec_infer.tar",
                "PP-LCNet_x1_0_doc_ori_infer.tar", "PP-LCNet_x1_0_textline_ori_infer.tar",
            )},
        }
        self.artifacts = []
        for relative, body in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            origin = (
                "https://files.pythonhosted.org/packages/official/" + path.name
                if relative.startswith("packages/")
                else "https://paddle-model-ecology.bj.bcebos.com/official/" + path.name
            )
            self.artifacts.append(OfficialArtifact(
                relative_path=relative,
                role="package" if relative.startswith("packages/") else "model",
                origin=origin,
                size=len(body),
                digest="sha256:" + hashlib.sha256(body).hexdigest(),
            ))

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def probe(runtime_root: Path) -> GpuIdentity:
        if not (runtime_root / "packages" / "paddleocr-3.7.0-py3-none-any.whl").is_file():
            raise AssertionError("probe did not receive prepared runtime")
        return GpuIdentity(
            name="NVIDIA GeForce RTX 4070 SUPER",
            compute_capability="8.9",
            driver_version="610.43.02",
            device_uuid_digest="sha256:" + "d" * 64,
            cuda_version="12.6",
            paddle_check_passed=True,
            gpu_execution_observed=True,
            cpu_fallback_observed=False,
            telemetry_observed=False,
        )

    @staticmethod
    def installer(runtime_root: Path) -> None:
        installed = runtime_root / "installed" / "bin"
        installed.mkdir(parents=True)
        (installed / "python").write_bytes(b"synthetic installed Python entry point")
        extract_locked_ocr_models(runtime_root)

    def test_prepare_seals_exact_artifacts_and_reopens_idempotently(self):
        first = prepare_ocr_runtime(
            source_root=self.source,
            state_root=self.state,
            artifacts=tuple(self.artifacts),
            runtime_installer=self.installer,
            compatibility_probe=self.probe,
        )
        second = prepare_ocr_runtime(
            source_root=self.source,
            state_root=self.state,
            artifacts=tuple(self.artifacts),
            runtime_installer=self.installer,
            compatibility_probe=self.probe,
        )
        self.assertEqual(first.runtime_digest, second.runtime_digest)
        self.assertEqual(first.root_identity, second.root_identity)
        self.assertEqual(first.file_count, 21)
        self.assertIn("installed/bin/python", tuple(entry.path for entry in first.tree_manifest))
        self.assertEqual(first.gpu_identity.compute_capability, "8.9")
        self.assertEqual(first.python_version, "3.12")
        self.assertEqual(first.package_versions, (
            "paddleocr==3.7.0", "paddlex==3.7.0", "paddlepaddle-gpu==3.3.0",
        ))
        self.assertEqual(first.cuda_version, "12.6")
        self.assertEqual(first.candidates, OCR_CANDIDATES)
        self.assertTrue(first.artifact_manifest_digest.startswith("sha256:"))
        self.assertTrue(first.model_inventory_digest.startswith("sha256:"))
        validate_prepared_ocr_runtime(first)
        with self.assertRaises(ContractError):
            validate_prepared_ocr_runtime(replace(
                first, artifact_manifest_digest="sha256:" + "0" * 64,
            ))
        for path in first.root.rglob("*"):
            mode = stat.S_IMODE(os.lstat(path).st_mode)
            relative = path.relative_to(first.root).as_posix()
            expected = 0o555 if path.is_dir() or relative in {
                "installed/bin/python", "installed/bin/python3", "installed/bin/python3.12",
            } else 0o444
            self.assertEqual(mode, expected)

    def test_verified_official_download_root_may_be_outside_repository(self):
        with tempfile.TemporaryDirectory() as external:
            external_source = Path(external) / "downloads"
            shutil.copytree(self.source, external_source)
            prepared = prepare_ocr_runtime(
                source_root=external_source,
                state_root=self.state,
                artifacts=tuple(self.artifacts),
                runtime_installer=self.installer,
                compatibility_probe=self.probe,
            )
        validate_prepared_ocr_runtime(prepared)

    def test_model_archive_traversal_rejects_without_publishing(self):
        target = self.source / "models" / "PP-OCRv6_medium_det_infer.tar"
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            body = b"escape"
            member = tarfile.TarInfo("../escape")
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
        target.write_bytes(output.getvalue())
        artifacts = list(self.artifacts)
        index = next(index for index, item in enumerate(artifacts) if item.relative_path.endswith("medium_det_infer.tar"))
        artifacts[index] = OfficialArtifact(
            relative_path=artifacts[index].relative_path, role="model",
            origin=artifacts[index].origin, size=target.stat().st_size,
            digest="sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
        )
        with self.assertRaises(ContractError):
            prepare_ocr_runtime(
                source_root=self.source, state_root=self.state,
                artifacts=tuple(artifacts), runtime_installer=self.installer,
                compatibility_probe=self.probe,
            )
        self.assertEqual(list(self.state.iterdir()), [])

    def test_large_artifacts_are_copied_and_hashed_with_bounded_memory(self):
        target = self.source / "models" / "PP-OCRv6_medium_det_infer.tar"
        output = io.BytesIO()
        root = "PP-OCRv6_medium_det_infer"
        with tarfile.open(fileobj=output, mode="w") as archive:
            directory = tarfile.TarInfo(root + "/")
            directory.type = tarfile.DIRTYPE
            archive.addfile(directory)
            body = b"x" * (24 * 1024 * 1024)
            member = tarfile.TarInfo(root + "/inference.pdiparams")
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
        target.write_bytes(output.getvalue())
        artifacts = list(self.artifacts)
        index = next(index for index, item in enumerate(artifacts) if item.relative_path.endswith("medium_det_infer.tar"))
        artifacts[index] = OfficialArtifact(
            relative_path=artifacts[index].relative_path, role="model",
            origin=artifacts[index].origin, size=target.stat().st_size,
            digest="sha256:" + hashlib.sha256(target.read_bytes()).hexdigest(),
        )
        del body, output
        tracemalloc.start()
        try:
            prepare_ocr_runtime(
                source_root=self.source, state_root=self.state,
                artifacts=tuple(artifacts), runtime_installer=self.installer,
                compatibility_probe=self.probe,
            )
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 16 * 1024 * 1024)

    def test_offline_installer_uses_fixed_no_network_commands_and_removes_venv_link(self):
        partial = self.root / "partial"
        shutil.copytree(self.source, partial)
        calls = []

        def runner(command, **options):
            calls.append((tuple(command), dict(options)))
            if tuple(command[:4]) == ("/usr/bin/python3", "-m", "venv", "--copies"):
                installed = partial / "installed"
                (installed / "bin").mkdir(parents=True)
                (installed / "lib").mkdir()
                (installed / "lib64").symlink_to("lib")
                (installed / "bin" / "python").write_bytes(b"python")
            return object()

        install_offline_ocr_runtime(partial, command_runner=runner)
        self.assertEqual(len(calls), 3)
        install_command = calls[1][0]
        self.assertIn("--no-index", install_command)
        self.assertIn("--no-cache-dir", install_command)
        self.assertNotIn("lib64", tuple(path.name for path in (partial / "installed").iterdir()))
        self.assertEqual(len(tuple((partial / "installed" / "models").iterdir())), 8)
        for _command, options in calls:
            self.assertEqual(options["env"]["PIP_NO_INDEX"], "1")
            self.assertEqual(options["env"]["PYTHONDONTWRITEBYTECODE"], "1")

    def test_gpu_probe_is_network_isolated_and_hashes_device_uuid(self):
        runtime = self.root / "runtime"
        (runtime / "installed" / "bin").mkdir(parents=True)
        (runtime / "installed" / "bin" / "python").write_bytes(b"python")
        raw = {
            "name": "NVIDIA GeForce RTX 4070 SUPER",
            "compute_capability": "8.9",
            "driver_version": "610.43.02",
            "device_uuid": "GPU-private-uuid",
            "cuda_version": "12.6",
            "python_version": "3.12",
            "paddleocr_version": "3.7.0",
            "paddlex_version": "3.7.0",
            "paddlepaddle_gpu_version": "3.3.0",
            "paddle_check_passed": True,
            "gpu_execution_observed": True,
            "cpu_fallback_observed": False,
        }
        calls = []

        def runner(command, **options):
            calls.append((tuple(command), dict(options)))
            return type("Result", (), {"returncode": 0, "stdout": json.dumps(raw).encode(), "stderr": b""})()

        result = probe_offline_gpu_runtime(runtime, command_runner=runner)
        self.assertEqual(result.device_uuid_digest, "sha256:" + hashlib.sha256(b"GPU-private-uuid").hexdigest())
        self.assertFalse(result.telemetry_observed)
        command = calls[0][0]
        self.assertIn("--unshare-net", command)
        self.assertIn("--clearenv", command)
        self.assertIn("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", command)
        self.assertNotIn("GPU-private-uuid", repr(command))
        self.assertNotIn(("--ro-bind", "/", "/"), tuple(zip(command, command[1:], command[2:])))
        self.assertIn(("--ro-bind", str(runtime), "/runtime"), tuple(zip(command, command[1:], command[2:])))
        self.assertIn(("--ro-bind", "/bin", "/bin"), tuple(zip(command, command[1:], command[2:])))
        self.assertIn(("--dev", "/dev"), tuple(zip(command, command[1:])))
        self.assertNotIn(("--dev-bind", "/dev", "/dev"), tuple(zip(command, command[1:], command[2:])))
        core_devices = {
            "/dev/nvidia0", "/dev/nvidiactl", "/dev/nvidia-modeset",
            "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools",
        }
        device_bindings = {
            source for option, source, target in zip(command, command[1:], command[2:])
            if option == "--dev-bind" and source == target
        }
        self.assertTrue(all(
            source in core_devices
            or source.removeprefix("/dev/nvidia-caps/nvidia-cap").isdigit()
            for source in device_bindings
        ))
        calls.clear()
        nested = probe_offline_gpu_runtime(
            runtime, command_runner=runner, parent_network_isolated=True,
        )
        self.assertEqual(nested, result)
        self.assertNotIn("--unshare-net", calls[0][0])

    def test_gpu_probe_rejects_non_character_nvidia_device(self):
        runtime = self.root / "runtime"
        (runtime / "installed" / "bin").mkdir(parents=True)
        (runtime / "installed" / "bin" / "python").write_bytes(b"python")
        real_lstat = os.lstat

        def differing(path):
            if os.fspath(path) == "/dev/nvidia0":
                return os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0))
            return real_lstat(path)

        with patch("ao_lore.ocr_runtime.os.lstat", side_effect=differing), self.assertRaises(ContractError):
            probe_offline_gpu_runtime(runtime, command_runner=lambda *_args, **_kwargs: self.fail("probe ran"))

    def test_gpu_probe_rejects_symlinked_runtime_before_launch(self):
        actual = self.root / "actual-runtime"
        (actual / "installed" / "bin").mkdir(parents=True)
        (actual / "installed" / "bin" / "python").write_bytes(b"python")
        linked = self.root / "linked-runtime"
        linked.symlink_to(actual, target_is_directory=True)
        calls = []
        with self.assertRaises(ContractError):
            probe_offline_gpu_runtime(
                linked,
                command_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
            )
        self.assertEqual(calls, [])

    def test_gpu_probe_rejects_foreign_capability_device_name(self):
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
            patch("ao_lore.ocr_runtime.os.lstat", side_effect=capability),
            self.assertRaises(ContractError),
        ):
            ocr_runtime._append_nvidia_devices([])

    def test_unofficial_drift_link_hardlink_and_special_artifacts_reject(self):
        cases = []
        unofficial = list(self.artifacts)
        unofficial[0] = OfficialArtifact(**{**unofficial[0].__dict__, "origin": "https://example.invalid/wheel"})
        cases.append(("unofficial", tuple(unofficial)))
        drifted = list(self.artifacts)
        drifted[0] = OfficialArtifact(**{**drifted[0].__dict__, "digest": "sha256:" + "0" * 64})
        cases.append(("drifted", tuple(drifted)))
        for label, artifacts in cases:
            with self.subTest(label=label), self.assertRaises(ContractError):
                prepare_ocr_runtime(source_root=self.source, state_root=self.state / label, artifacts=artifacts, runtime_installer=self.installer, compatibility_probe=self.probe)

        original = self.source / self.artifacts[0].relative_path
        link = self.source / "packages" / "linked.whl"
        link.symlink_to(original.name)
        linked = OfficialArtifact("packages/linked.whl", "package", self.artifacts[0].origin, original.stat().st_size, self.artifacts[0].digest)
        with self.assertRaises(ContractError):
            prepare_ocr_runtime(source_root=self.source, state_root=self.state / "link", artifacts=(*self.artifacts, linked), runtime_installer=self.installer, compatibility_probe=self.probe)

        hardlink = self.source / "packages" / "hardlinked.whl"
        os.link(original, hardlink)
        hardlinked = OfficialArtifact("packages/hardlinked.whl", "package", self.artifacts[0].origin, original.stat().st_size, self.artifacts[0].digest)
        with self.assertRaises(ContractError):
            prepare_ocr_runtime(source_root=self.source, state_root=self.state / "hardlink", artifacts=(*self.artifacts, hardlinked), runtime_installer=self.installer, compatibility_probe=self.probe)

    def test_foreign_collision_and_failed_probe_preserve_foreign_state(self):
        collision = self.state / "ocr-runtime-v0.1"
        collision.mkdir()
        sentinel = collision / "foreign"
        sentinel.write_bytes(b"foreign")
        with self.assertRaises(ContractError):
            prepare_ocr_runtime(source_root=self.source, state_root=self.state, artifacts=tuple(self.artifacts), runtime_installer=self.installer, compatibility_probe=self.probe)
        self.assertEqual(sentinel.read_bytes(), b"foreign")

        clean_state = self.root / "failed-probe"
        clean_state.mkdir()
        def failed_probe(_root):
            raise RuntimeError("private diagnostic")
        with self.assertRaises(ContractError):
            prepare_ocr_runtime(source_root=self.source, state_root=clean_state, artifacts=tuple(self.artifacts), runtime_installer=self.installer, compatibility_probe=failed_probe)
        self.assertEqual(list(clean_state.iterdir()), [])

    def test_publish_never_replaces_foreign_final_created_in_last_window(self):
        foreign_identity = None
        real_rename = ocr_runtime._rename_noreplace_at

        def race(parent_descriptor, source, destination):
            nonlocal foreign_identity
            os.mkdir(destination, dir_fd=parent_descriptor)
            info = os.stat(destination, dir_fd=parent_descriptor, follow_symlinks=False)
            foreign_identity = (info.st_dev, info.st_ino)
            return real_rename(parent_descriptor, source, destination)

        with patch("ao_lore.ocr_runtime._rename_noreplace_at", side_effect=race):
            with self.assertRaises(ContractError):
                prepare_ocr_runtime(
                    source_root=self.source,
                    state_root=self.state,
                    artifacts=tuple(self.artifacts),
                    runtime_installer=self.installer,
                    compatibility_probe=self.probe,
                )
        final = self.state / "ocr-runtime-v0.1"
        self.assertTrue(final.is_dir())
        info = os.lstat(final)
        self.assertEqual((info.st_dev, info.st_ino), foreign_identity)
        self.assertEqual(list(final.iterdir()), [])

    def test_expected_artifact_special_file_rejects_without_residue(self):
        target = self.source / self.artifacts[-1].relative_path
        target.unlink()
        os.mkfifo(target)
        with self.assertRaises(ContractError):
            prepare_ocr_runtime(
                source_root=self.source,
                state_root=self.state,
                artifacts=tuple(self.artifacts),
                runtime_installer=self.installer,
                compatibility_probe=self.probe,
            )
        self.assertEqual(list(self.state.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
