import json
import os
import subprocess
import sys
import tempfile
import unittest
import shutil
from pathlib import Path

from ao_lore.public_release_contracts import validate_public_release_policy
from ao_lore.public_release_safety import _content_reasons


ROOT = Path(__file__).resolve().parents[1]


class PublicTreePathTests(unittest.TestCase):
    def test_negative_path_fixtures_need_no_private_path_exceptions(self):
        policy = validate_public_release_policy(json.loads(
            (ROOT / "docs/public-release/export-policy.json").read_text(encoding="utf-8")
        ))
        paths = {
            "tests/private_docx_uat_worker_fixture.py",
            "tests/test_ao_lore_public_tree_paths.py",
            "tests/test_ao_lore_standalone.py",
        }
        self.assertTrue(paths.isdisjoint({
            item["path"] for item in policy["exceptions"]
            if item["reason_code"] == "inert_private_path"
        }))

    def test_public_runtime_and_operator_docs_have_no_private_paths(self):
        policy = validate_public_release_policy(json.loads(
            (ROOT / "docs/public-release/export-policy.json").read_text(encoding="utf-8")
        ))
        for relative in ("src/ao_lore/selfcheck.py", "README.md", "AGENTS.md", "scripts/rehearse-promotion-controls.py"):
            body = (ROOT / relative).read_bytes()
            self.assertNotIn("private_path", _content_reasons(relative, body, policy))
        for body in (
            ("/" + "/".join(("opt", "fixture-host", "projects", "product", "state.json"))).encode(),
            ("/" + "/".join(("home", "fixture-user", "private", "state.json"))).encode(),
        ):
            self.assertIn("private_path", _content_reasons("probe.txt", body, policy))

    def test_selfcheck_accepts_independent_fresh_clone(self):
        with tempfile.TemporaryDirectory() as name:
            clone = Path(name) / "independent"
            clone.mkdir()
            listed = subprocess.run(["/usr/bin/git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.split(b"\0")
            for raw in filter(None, listed):
                relative = Path(os.fsdecode(raw))
                source = ROOT / relative
                if source.is_file():
                    target = clone / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            subprocess.run(["/usr/bin/git", "init", "-q"], cwd=clone, check=True)
            subprocess.run(["/usr/bin/git", "add", "."], cwd=clone, check=True)
            subprocess.run(["/usr/bin/git", "-c", "user.name=AO Lore Test", "-c", "user.email=ao-lore-test@tests.invalid", "commit", "-q", "-m", "test snapshot"], cwd=clone, check=True)
            result = subprocess.run(
                [sys.executable, "-m", "ao_lore.selfcheck"],
                cwd=clone,
                env={**os.environ, "PYTHONPATH": "src", "AO_LORE_HOME": str(clone / ".ao-lore")},
                text=True,
                capture_output=True,
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual("pass", report["status"])
            self.assertEqual(0, report["sibling_dependencies"])
            self.assertNotIn(str(clone), result.stdout)

            leak = clone / "src/ao_lore/forbidden_literal.py"
            escape = "/" + "/".join((
                "opt", "fixture-host", "projects", "ao" + "-mission",
            ))
            leak.write_text(f'DEPENDENCY = "{escape}"\n')
            result = subprocess.run(
                [sys.executable, "-m", "ao_lore.selfcheck"],
                cwd=clone,
                env={**os.environ, "PYTHONPATH": "src", "AO_LORE_HOME": str(clone / ".ao-lore")},
                text=True,
                capture_output=True,
            )
            self.assertEqual(1, result.returncode)
            self.assertIn("sibling dependency tokens found", result.stdout)

    def test_public_docs_do_not_depend_on_internal_plans(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        integration = (ROOT / "tests/test_ao_lore_integration.py").read_text(encoding="utf-8")
        self.assertNotIn("docs/superpowers/", readme)
        self.assertNotIn("docs/superpowers/", integration)
        self.assertTrue((ROOT / "docs/architecture/progressive-evidence.md").is_file())


if __name__ == "__main__":
    unittest.main()
