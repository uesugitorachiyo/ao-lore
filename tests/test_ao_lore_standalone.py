import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore._strict_io import ContractError
from ao_lore.home import repository_root, require_runtime_output, runtime_home
from ao_lore.selfcheck import _validate_repository_root, run_selfcheck


class StandaloneBoundaryTests(unittest.TestCase):
    def test_private_ocr_operator_help_is_lazy_and_standalone(self):
        script = repository_root() / "scripts/private-ocr-uat.py"
        command = (
            "import runpy,sys; "
            "sys.argv=['private-ocr-uat.py','--help']; "
            "\ntry: runpy.run_path(" + repr(str(script)) + ", run_name='__main__')\n"
            "except SystemExit as exc: assert exc.code == 0\n"
            "assert 'ao_lore.private_ocr_uat' not in sys.modules; "
            "assert not any(name == 'paddle' or name.startswith(('paddle.', 'paddleocr')) for name in sys.modules); "
            "assert not any(name.startswith(('ao_mission', 'ao_atlas', 'ao_blueprint', "
            "'ao_foundry', 'ao_command')) for name in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", command], cwd=repository_root(),
            env={**os.environ, "PYTHONPATH": "src"}, capture_output=True,
            text=True, check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_private_docx_operator_help_is_lazy_and_standalone(self):
        script = repository_root() / "scripts/private-docx-uat.py"
        command = (
            "import runpy,sys; "
            "sys.argv=['private-docx-uat.py','--help']; "
            "\ntry: runpy.run_path("
            + repr(str(script))
            + ", run_name='__main__')\n"
            "except SystemExit as exc: assert exc.code == 0\n"
            "assert 'ao_lore.private_docx_uat' not in sys.modules; "
            "assert 'ao_lore.private_docx_domain' not in sys.modules; "
            "assert not any(name == 'docling' or name.startswith('docling.') for name in sys.modules); "
            "assert not any(name.startswith(('ao_mission', 'ao_atlas', 'ao_blueprint', "
            "'ao_foundry', 'ao_command')) for name in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", command],
            cwd=repository_root(),
            env={**os.environ, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_private_pdf_operator_help_is_lazy_and_standalone(self):
        script = repository_root() / "scripts/private-pdf-uat.py"
        command = (
            "import runpy,sys; "
            "sys.argv=['private-pdf-uat.py','--help']; "
            "\ntry: runpy.run_path("
            + repr(str(script))
            + ", run_name='__main__')\n"
            "except SystemExit as exc: assert exc.code == 0\n"
            "assert 'ao_lore.private_pdf_uat' not in sys.modules; "
            "assert not any(name == 'docling' or name.startswith('docling.') for name in sys.modules); "
            "assert not any(name.startswith(('ao_mission', 'ao_atlas', 'ao_blueprint', "
            "'ao_foundry', 'ao_command')) for name in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", command],
            cwd=repository_root(),
            env={**os.environ, "PYTHONPATH": "src"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_core_import_does_not_load_optional_docling_and_pin_is_exact(self):
        command = (
            "import sys; import ao_lore.__main__; import ao_lore.ingestion; "
            "import ao_lore.candidate_queue; "
            "assert not any(name.startswith('ao_lore.workspace_') for name in sys.modules); "
            "assert 'ao_lore.batch_ingestion' not in sys.modules; "
            "assert 'ao_lore.docx_benchmark' not in sys.modules; "
            "assert not any(name == 'docling' or name.startswith('docling.') "
            "for name in sys.modules); "
            "assert not any(name.startswith(('ao_mission', 'ao_atlas', "
            "'ao_blueprint', 'ao_foundry', 'ao_command')) for name in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", command], cwd=repository_root(),
            env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pyproject = (repository_root() / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('pdf = ["docling==2.118.1"]', pyproject)

    def test_help_does_not_import_workspace_product_modules(self):
        command = (
            "import sys; from ao_lore.__main__ import main; "
            "\ntry: main(['--help'])\n"
            "except SystemExit as exc: assert exc.code == 0\n"
            "assert not any(name.startswith('ao_lore.workspace_') for name in sys.modules)"
        )
        result = subprocess.run(
            [sys.executable, "-c", command], cwd=repository_root(),
            env={**os.environ, "PYTHONPATH": "src"}, capture_output=True, text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installed_source_imports_standalone_outside_repository(self):
        source = repository_root() / "src"
        command = (
            "import sys; import ao_lore.__main__; import ao_lore.ingestion; "
            "import ao_lore.candidate_queue; "
            "assert 'ao_lore.batch_ingestion' not in sys.modules; "
            "assert 'ao_lore.docx_benchmark' not in sys.modules; "
            "assert not any(name == 'docling' or name.startswith('docling.') "
            "for name in sys.modules)"
        )
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-c", command],
                cwd=directory,
                env={**os.environ, "PYTHONPATH": str(source)},
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_canonical_repository_and_default_home_are_local(self):
        root = repository_root()
        self.assertEqual(root, repository_root())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(root / ".ao-lore", runtime_home())

    def test_configured_home_and_outputs_cannot_escape_repository(self):
        with patch.dict(os.environ, {"AO_LORE_HOME": "/tmp/ao-lore-escape"}, clear=True):
            with self.assertRaisesRegex(ContractError, "escapes"):
                runtime_home()
        foreign_root = Path(
            "/", "opt", "fixture-host", "projects", "product",
        )
        with patch.dict(
            os.environ,
            {"AO_LORE_HOME": str(foreign_root / ".ao-lore")},
            clear=True,
        ):
            with self.assertRaisesRegex(ContractError, "escapes"):
                require_runtime_output(foreign_root / "outside.json")

    def test_selfcheck_proves_no_sibling_dependencies_or_symlinks(self):
        report = run_selfcheck()
        self.assertEqual("pass", report["status"])
        self.assertEqual(0, report["sibling_dependencies"])
        self.assertEqual(0, report["symlinks"])

    def test_selfcheck_accepts_the_current_verified_git_checkout(self):
        report = run_selfcheck()

        self.assertEqual("pass", report["status"])
        serialized = json.dumps(report, sort_keys=True)
        self.assertNotIn(str(repository_root()), serialized)
        self.assertNotIn(str(runtime_home()), serialized)
        self.assertNotIn("repository_root", report)
        self.assertNotIn("runtime_home", report)

    def test_selfcheck_rejects_a_mismatched_git_toplevel(self):
        root = repository_root()
        probe = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"{root.parent}\n{root / '.git'}\n",
            stderr="",
        )
        with patch("ao_lore.selfcheck.subprocess.run", return_value=probe):
            with self.assertRaisesRegex(ContractError, "verified Git checkout"):
                _validate_repository_root(root)


if __name__ == "__main__":
    unittest.main()
