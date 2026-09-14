import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rehearse-sanitized-lifecycle.py"
CAMPAIGN = ROOT / ".ao-lore" / "sanitized-lifecycle-rehearsal"
REPORT_NAME = "sanitized-lifecycle-report.json"
REJECTION = "sanitized lifecycle rehearsal rejected\n"
FORBIDDEN_ARGUMENTS = (
    "--root", "--source", "--workspace", "--candidate", "--proposal",
    "--authorization", "--clock", "--provider", "--network", "--failpoint",
    "--force", "--authority",
)


def _inventory(root: Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class SanitizedLifecycleRehearsalTests(unittest.TestCase):
    @staticmethod
    def _environment() -> dict[str, str]:
        environment = dict(os.environ)
        forbidden_names = {
            "PYTHONPATH", "AO_LORE_HOME", "GIT_DIR", "GIT_WORK_TREE",
            "GIT_INDEX_FILE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        }
        for name in forbidden_names:
            environment.pop(name, None)
        for name in tuple(environment):
            upper = name.upper()
            if (
                upper.startswith("GIT_")
                or upper.endswith(("_TOKEN", "_CREDENTIAL", "_CREDENTIALS"))
            ):
                environment.pop(name, None)
        return environment

    @staticmethod
    def _module(name: str):
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        if spec is None or spec.loader is None:
            raise AssertionError("rehearsal module unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            env=self._environment(),
            capture_output=True,
            text=True,
            timeout=120,
        )

    @staticmethod
    def _invoke(module, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = module.main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def _isolated_retained_run(self, module) -> Path:
        CAMPAIGN.mkdir(parents=True, exist_ok=True)
        run = CAMPAIGN / ("test-retained-" + secrets.token_hex(8))
        module.RETAINED_RUN = run
        self.addCleanup(shutil.rmtree, run, True)
        return run

    def test_direct_then_check_are_canonical_byte_identical_and_path_free(self):
        direct = self._run_script()
        self.assertEqual(0, direct.returncode, direct.stderr)
        self.assertEqual("", direct.stderr)
        retained = CAMPAIGN / "retained" / REPORT_NAME
        self.assertTrue(retained.is_file())
        before_body = retained.read_bytes()
        before_stat = retained.stat()
        before_inventory = _inventory(CAMPAIGN)

        checked = self._run_script("--check")
        self.assertEqual(0, checked.returncode, checked.stderr)
        self.assertEqual("", checked.stderr)
        self.assertEqual(direct.stdout, checked.stdout)
        self.assertEqual(before_body, checked.stdout.encode("utf-8"))
        self.assertEqual(before_body, retained.read_bytes())
        self.assertEqual(before_stat.st_ino, retained.stat().st_ino)
        self.assertEqual(before_stat.st_mtime_ns, retained.stat().st_mtime_ns)
        self.assertEqual(before_inventory, _inventory(CAMPAIGN))

        report = json.loads(direct.stdout)
        self.assertEqual(
            "ao.lore.sanitized-lifecycle-rehearsal.v0.1",
            report["schema_version"],
        )
        self.assertTrue(report["byte_identical_check"])
        self.assertEqual(0, report["network_calls"])
        self.assertEqual(0, report["provider_calls"])
        self.assertTrue(all(
            value is False for value in report["external_authority"].values()
        ))
        for output in (direct.stdout, checked.stdout):
            self.assertNotIn(str(ROOT), output)
            self.assertNotIn("/home/", output)
            self.assertNotIn("/opt/", output)

    def test_check_uses_a_fresh_run_and_does_not_rewrite_retained_report(self):
        module = self._module("sanitized_lifecycle_rehearsal_fresh_check")
        retained_run = self._isolated_retained_run(module)
        direct_code, direct_stdout, direct_stderr = self._invoke(module, [])
        self.assertEqual((0, ""), (direct_code, direct_stderr))
        retained = retained_run / REPORT_NAME
        before = retained.read_bytes()
        before_stat = retained.stat()
        scratch: list[Path] = []
        real_mkdtemp = module.tempfile.mkdtemp

        def recorded_mkdtemp(*args, **kwargs):
            created = Path(real_mkdtemp(*args, **kwargs))
            scratch.append(created)
            return str(created)

        with patch.object(module.tempfile, "mkdtemp", side_effect=recorded_mkdtemp):
            code, stdout, stderr = self._invoke(module, ["--check"])
        self.assertEqual((0, direct_stdout, ""), (code, stdout, stderr))
        self.assertEqual(1, len(scratch))
        self.assertNotEqual(retained_run, scratch[0])
        self.assertFalse(scratch[0].exists())
        self.assertEqual(before, retained.read_bytes())
        self.assertEqual(before_stat.st_ino, retained.stat().st_ino)
        self.assertEqual(before_stat.st_mtime_ns, retained.stat().st_mtime_ns)

    def test_check_returns_nonzero_on_canonical_report_drift_without_rewrite(self):
        module = self._module("sanitized_lifecycle_rehearsal_drift")
        retained_run = self._isolated_retained_run(module)
        self.assertEqual(0, self._invoke(module, [])[0])
        retained = retained_run / REPORT_NAME
        original = retained.read_bytes()
        report = json.loads(original)
        report["summary"] = "Offline synthetic governed lifecycle completed with retained drift."
        report["report_digest"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in report.items() if key != "report_digest"},
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        drifted = (
            json.dumps(
                report,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        retained.write_bytes(drifted)
        before_stat = retained.stat()
        code, stdout, stderr = self._invoke(module, ["--check"])
        self.assertEqual(1, code)
        self.assertEqual("", stdout)
        self.assertEqual("", stderr)
        self.assertEqual(drifted, retained.read_bytes())
        self.assertEqual(before_stat.st_ino, retained.stat().st_ino)

    def test_normal_rehearsal_uses_no_network_http_or_provider_entry_point(self):
        module = self._module("sanitized_lifecycle_rehearsal_offline")
        self._isolated_retained_run(module)
        with (
            patch.object(module, "_deny_network", side_effect=AssertionError("network denied")) as network_guard,
            patch.object(module, "_provider_entry", side_effect=AssertionError("provider denied")) as provider_guard,
        ):
            code, stdout, stderr = self._invoke(module, [])
        self.assertEqual((0, ""), (code, stderr))
        network_guard.assert_not_called()
        provider_guard.assert_not_called()
        report = json.loads(stdout)
        self.assertEqual(0, report["network_calls"])
        self.assertEqual(0, report["provider_calls"])

    def test_product_path_has_no_direct_network_http_or_provider_api(self):
        lifecycle = ROOT / "src/ao_lore/sanitized_lifecycle.py"
        expected_lifecycle_modules = {
            "_strict_io",
            "benchmark",
            "candidates",
            "document_evidence_query",
            "evidence_graph",
            "evidence_graph_contracts",
            "evidence_selection",
            "evidence_selection_contracts",
            "home",
            "knowledge",
            "knowledge_contracts",
            "promotion",
            "sanitized_lifecycle_contracts",
            "workspace_contracts",
            "workspace_documents",
            "workspace_query",
            "workspace_registry",
        }
        lifecycle_tree = ast.parse(
            lifecycle.read_text(encoding="utf-8"), filename=str(lifecycle)
        )
        direct_lifecycle_modules = {
            node.module
            for node in ast.walk(lifecycle_tree)
            if isinstance(node, ast.ImportFrom)
            and node.level == 1
            and node.module is not None
        }
        self.assertEqual(expected_lifecycle_modules, direct_lifecycle_modules)
        paths = tuple(
            ROOT / "src/ao_lore" / f"{module}.py"
            for module in sorted(expected_lifecycle_modules)
        ) + (lifecycle, SCRIPT)
        forbidden_modules = {
            "aiohttp", "anthropic", "http.client", "httpx", "openai",
            "requests", "socket", "urllib.request",
        }
        forbidden_calls = {
            "BoundedUrllibClient", "HTTPConnection", "HTTPSConnection",
            "Request", "create_connection", "socket", "urlopen",
        }
        for path in paths:
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                imported: set[str] = set()
                called: set[str] = set()
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imported.update(alias.name for alias in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imported.add(node.module)
                        self.assertNotIn("provider", node.module.casefold())
                    elif isinstance(node, ast.Call):
                        if isinstance(node.func, ast.Name):
                            called.add(node.func.id)
                        elif isinstance(node.func, ast.Attribute):
                            called.add(node.func.attr)
                direct_external_imports = forbidden_modules.intersection(imported)
                self.assertEqual(set(), direct_external_imports)
                self.assertTrue(forbidden_calls.isdisjoint(called), called)

    def test_cli_accepts_only_optional_check_and_redacts_rejections(self):
        for argument in (*FORBIDDEN_ARGUMENTS, "--help", "unexpected"):
            with self.subTest(argument=argument):
                rejected = self._run_script(argument)
                self.assertEqual(2, rejected.returncode)
                self.assertEqual("", rejected.stdout)
                self.assertEqual(REJECTION, rejected.stderr)
                self.assertNotIn(argument, rejected.stderr)

    def test_documentation_states_fixture_and_authority_boundaries(self):
        workflow = (ROOT / "workflows/rehearse-sanitized-lifecycle.md").read_text(
            encoding="utf-8"
        ).casefold()
        workflow = " ".join(workflow.split())
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8").casefold()
        for phrase in (
            "synthetic fixture authority", "disposable brain",
            "does not authorize real candidate review", "does not authorize real promotion",
            "does not authorize github publication", "private visibility only",
        ):
            self.assertIn(phrase, workflow)
        self.assertIn("rehearse-sanitized-lifecycle.py --check", workflow)
        self.assertIn(".ao-lore/sanitized-lifecycle-rehearsal", agents)
        self.assertIn("fixed-root", agents)


if __name__ == "__main__":
    unittest.main()
