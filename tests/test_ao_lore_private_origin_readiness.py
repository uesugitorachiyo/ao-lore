import importlib.util
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore.public_release_contracts import canonical_digest, validate_public_release_readiness
from tests.test_ao_lore_public_release_safety import EXPECTED_WORKFLOW


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/rehearse-private-origin-readiness.py"
EXPECTED_REQUIREMENTS = (
    b"--index-url "
    + b"https://pypi.org/simple"
    + b"\n--only-binary=:all:\nattrs==23.2.0 --hash=sha256:99b87a485a5820b23b879f04c2305b44b951b502fd64be915879d77a7e8fc6f1\njsonschema==4.10.3 --hash=sha256:443442f9ac2fdfde7bc99079f0ba08e5d167fc67749e9fc706a393bc8857ca48\npyrsistent==0.20.0 --hash=sha256:cae40a9e3ce178415040a0383f00e8d68b569e97f31928a3a8ad37e3fde6df6a\n"
)
REAL_WORKFLOW_SHA256 = "5d51db43186722b7f32d8d75344b1345918b2fac1750865e493b93dc49ddc608"
REAL_REQUIREMENTS_SHA256 = "19f9eb7827f34df22b5634a621e7d4d279fc0a19ec7d383bd9cbc7608841baeb"


class PrivateOriginReadinessTests(unittest.TestCase):

    def temporary_origin(self, root, workflow=EXPECTED_WORKFLOW, requirements=EXPECTED_REQUIREMENTS):
        repository = root / "origin"
        (repository / ".github/workflows").mkdir(parents=True)
        (repository / ".github/workflows/ci.yml").write_bytes(workflow)
        (repository / "ci-requirements.txt").write_bytes(requirements)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
        subprocess.run(["git", "add", "."], cwd=repository, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "origin"],
            cwd=repository,
            check=True,
        )
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        policy["included_files"] = sorted(
            set(policy["included_files"]) | {".github/workflows/ci.yml", "ci-requirements.txt"}
        )
        policy["policy_digest"] = canonical_digest(policy, omit="policy_digest")
        return repository, policy

    def test_real_policy_binds_committed_workflow_and_dependency_bytes(self):
        module = self.load()
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        module.validate_private_origin_export_contract(policy, ROOT)
        self.assertIn(".github/workflows/ci.yml", policy["included_files"])
        self.assertIn("ci-requirements.txt", policy["included_files"])
        self.assertEqual(
            REAL_REQUIREMENTS_SHA256,
            hashlib.sha256((ROOT / "ci-requirements.txt").read_bytes()).hexdigest(),
        )

    def test_private_origin_contract_rejects_omission_and_byte_drift(self):
        module = self.load()
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        for missing in (".github/workflows/ci.yml", "ci-requirements.txt"):
            omitted = json.loads(json.dumps(policy))
            omitted["included_files"] = sorted(
                set(omitted["included_files"]) | {".github/workflows/ci.yml", "ci-requirements.txt"}
            )
            omitted["included_files"].remove(missing)
            omitted["policy_digest"] = canonical_digest(omitted, omit="policy_digest")
            with self.subTest(missing=missing), self.assertRaises(module.ReadinessError):
                module.validate_private_origin_export_contract(omitted, ROOT)
        with tempfile.TemporaryDirectory() as name:
            repository = Path(name)
            (repository / ".github/workflows").mkdir(parents=True)
            (repository / ".github/workflows/ci.yml").write_bytes(b"drift\n")
            (repository / "ci-requirements.txt").write_bytes(EXPECTED_REQUIREMENTS)
            with self.assertRaises(module.ReadinessError):
                module.validate_private_origin_export_contract(policy, repository)

    def test_private_origin_contract_uses_committed_bytes_and_fixed_digests(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as name:
            repository, policy = self.temporary_origin(Path(name))
            # Working-tree drift is not committed evidence and must not alter readiness.
            (repository / ".github/workflows/ci.yml").write_bytes(b"uncommitted drift\n")
            (repository / "ci-requirements.txt").write_bytes(b"uncommitted drift\n")
            module.validate_private_origin_export_contract(policy, repository)
            for relative, expected in (
                (".github/workflows/ci.yml", REAL_WORKFLOW_SHA256),
                ("ci-requirements.txt", REAL_REQUIREMENTS_SHA256),
            ):
                committed = subprocess.run(
                    ["git", "show", f"HEAD:{relative}"], cwd=repository, check=True, capture_output=True
                ).stdout
                self.assertEqual(expected, hashlib.sha256(committed).hexdigest())

            for relative in (".github/workflows/ci.yml", "ci-requirements.txt"):
                with self.subTest(relative=relative):
                    # Start each drift proof from an independently fresh canonical
                    # commit so the other contract file remains unchanged.
                    drift_repository, drift_policy = self.temporary_origin(Path(name) / relative.replace("/", "-"))
                    target = drift_repository / relative
                    target.write_bytes(b"committed drift\n")
                    subprocess.run(["git", "add", relative], cwd=drift_repository, check=True)
                    subprocess.run(
                        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "drift"],
                        cwd=drift_repository,
                        check=True,
                    )
                    with self.assertRaises(module.ReadinessError):
                        module.validate_private_origin_export_contract(drift_policy, drift_repository)

    def test_private_origin_contract_rejects_invalid_or_wrong_policy_digest(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as name:
            repository, policy = self.temporary_origin(Path(name))
            for digest in ("not-a-digest", "0" * 64):
                invalid = json.loads(json.dumps(policy))
                invalid["policy_digest"] = digest
                with self.subTest(digest=digest), self.assertRaises(module.ReadinessError):
                    module.validate_private_origin_export_contract(invalid, repository)

    def test_gate_dependency_version_validation_rejects_missing_or_wrong(self):
        module = self.load()
        with self.assertRaises(module.ReadinessError):
            module._validate_gate_dependency_versions(b"attrs==23.2.0\njsonschema==4.10.3\n")
        with self.assertRaises(module.ReadinessError):
            module._validate_gate_dependency_versions(b"attrs==23.2.0\njsonschema==4.10.3\npyrsistent==0.19.0\n")

    def test_gate_consistency_is_offline_and_does_not_install(self):
        module = self.load()
        arguments = module._gate_consistency_arguments()
        self.assertEqual(["-m", "pip", "check"], arguments)
        self.assertNotIn("install", arguments)
        self.assertNotIn("--index-url", arguments)

    def test_gate_requirements_reject_noncanonical_bytes(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as name:
            clone = Path(name)
            (clone / "ci-requirements.txt").write_bytes(b"wrong\n")
            with self.assertRaises(module.ReadinessError):
                module._validate_gate_requirements(clone)

    def test_gate_environment_is_prepared_before_make_check(self):
        source = SCRIPT.read_text()
        self.assertLess(source.index("_prepare_gate_environment(clone, temporary)"), source.index('["make", "check"]'))
        self.assertNotIn("[sys.executable, \"-m\", \"pip\", \"wheel\"", source)
        self.assertNotIn("[sys.executable, \"-m\", \"pip\", \"install\"", source)
        self.assertNotIn('"PATH": f"{gate / \'bin\'}', source)

    def test_export_binding_rejects_mixed_source_head(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as name:
            repository, policy = self.temporary_origin(Path(name))
            contract = module.validate_private_origin_export_contract(policy, repository)
            export = {"source_head": "0" * 40}
            with self.assertRaises(module.ReadinessError):
                module._validate_export_binding(export, repository, contract)

    def test_gate_interpreter_metadata_rejects_unsupported_or_wrong_runtime(self):
        module = self.load()
        for output in (b"3.10\nattrs==23.2.0\njsonschema==4.10.3\npyrsistent==0.20.0\n", b"3.12\nattrs==23.2.0\njsonschema==4.10.3\n"):
            with self.subTest(output=output), self.assertRaises(module.ReadinessError):
                module._validate_gate_interpreter_metadata(output)

    def test_gate_interpreter_selector_does_not_substitute_caller_interpreter(self):
        module = self.load()
        with patch.object(module.sys, "executable", "/tmp/not-the-invoked-interpreter"):
            with self.assertRaises(module.ReadinessError):
                module._select_gate_interpreter()

    def test_private_origin_contract_rejects_head_change_during_blob_reads(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as name:
            repository, policy = self.temporary_origin(Path(name))
            original_run = module._run
            moved = False

            def run(arguments, **kwargs):
                nonlocal moved
                result = original_run(arguments, **kwargs)
                if not moved and arguments[1:3] == ["cat-file", "blob"]:
                    moved = True
                    (repository / "HEAD-moved.txt").write_text("head moved\n")
                    subprocess.run(["git", "add", "HEAD-moved.txt"], cwd=repository, check=True)
                    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "move"], cwd=repository, check=True)
                return result

            with patch.object(module, "_run", side_effect=run), self.assertRaises(module.ReadinessError):
                module.validate_private_origin_export_contract(policy, repository)

    def test_private_origin_contract_rejects_oversized_committed_blob_before_read(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as name:
            repository, policy = self.temporary_origin(Path(name), workflow=b"x" * 2_000_000)
            original_run = module._run
            commands = []

            def run(arguments, **kwargs):
                commands.append(arguments)
                return original_run(arguments, **kwargs)

            with patch.object(module, "_run", side_effect=run), self.assertRaises(module.ReadinessError):
                module.validate_private_origin_export_contract(policy, repository)
            self.assertFalse(any(arguments[1:3] == ["cat-file", "blob"] for arguments in commands))

    def test_gitleaks_allowlist_is_pattern_scoped_not_path_scoped(self):
        config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
        self.assertNotIn("paths =", config)
        self.assertIn("regexes =", config)
        self.assertNotIn("test_ao_lore_public_release_safety", config)

    @unittest.skipUnless(Path("/usr/bin/gitleaks").is_file(), "gitleaks is not installed")
    def test_gitleaks_rejects_a_future_secret_in_scanner_test_path(self):
        with tempfile.TemporaryDirectory() as name:
            repository = Path(name)
            shutil.copy2(ROOT / ".gitleaks.toml", repository / ".gitleaks.toml")
            target = repository / "tests/test_ao_lore_public_release_safety.py"
            target.parent.mkdir()
            target.write_text("GITHUB_TOKEN=ghp_" + "1A2b3C4d5E6f7G8h9I0j1K2l3M4n5O6p7Q8r\n")
            subprocess.run(["/usr/bin/git", "init", "-q"], cwd=repository, check=True)
            subprocess.run(["/usr/bin/git", "add", "."], cwd=repository, check=True)
            subprocess.run(
                ["/usr/bin/git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q", "-m", "fixture"],
                cwd=repository,
                check=True,
            )
            module = self.load()
            result = subprocess.run(
                module._gitleaks_arguments(module._modern_gitleaks()),
                cwd=repository,
                capture_output=True,
            )
            self.assertNotEqual(0, result.returncode)

    def test_gitleaks_prefers_git_object_history_command(self):
        module = self.load()
        self.assertEqual(
            ["/tool/gitleaks", "git", "--no-banner", "--redact"],
            module._gitleaks_arguments(Path("/tool/gitleaks")),
        )

    def test_rehearsal_report_uses_and_validates_public_readiness_contract(self):
        module = self.load()
        manifest = {
            "source_head": "a" * 40,
            "commit": "b" * 40,
            "manifest_digest": "c" * 64,
            "file_count": 2,
            "total_bytes": 20,
        }
        policy = {"policy_digest": "d" * 64}
        report = module._readiness_report(manifest, policy)
        self.assertEqual("ao.lore.public-release-readiness.v0.1", report["schema_version"])
        self.assertEqual(report, validate_public_release_readiness(report, manifest={
            "source_head": manifest["source_head"],
            "policy_digest": policy["policy_digest"],
            "manifest_digest": manifest["manifest_digest"],
            "file_count": manifest["file_count"],
            "total_bytes": manifest["total_bytes"],
        }))
    def load(self):
        spec = importlib.util.spec_from_file_location("private_origin_readiness", SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        return module

    def staging(self, root):
        staging = root / "staging"; staging.mkdir()
        (staging / "README.md").write_text("safe\n")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=staging, check=True)
        subprocess.run(["git", "add", "."], cwd=staging, check=True)
        environment = {"GIT_AUTHOR_NAME":"AO Lore", "GIT_AUTHOR_EMAIL":"sanitized-export@ao-lore.invalid", "GIT_COMMITTER_NAME":"AO Lore", "GIT_COMMITTER_EMAIL":"sanitized-export@ao-lore.invalid", "GIT_AUTHOR_DATE":"2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE":"2000-01-01T00:00:00Z"}
        subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=staging, env=environment, check=True)
        return staging

    def test_local_bare_remote_round_trip_preserves_exact_clean_commit(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); staging = self.staging(root)
            report = module.local_round_trip(staging, root / "remote.git", root / "clone")
            self.assertEqual(report["staging_commit"], report["clone_commit"])
            self.assertEqual(report["staging_manifest_digest"], report["clone_manifest_digest"])
            self.assertEqual(1, report["reachable_commit_count"])
            self.assertFalse(report["network_used"])

    def test_round_trip_rejects_unexpected_refs_and_existing_destination(self):
        module = self.load()
        with tempfile.TemporaryDirectory() as name:
            root = Path(name); staging = self.staging(root)
            subprocess.run(["git", "tag", "unexpected"], cwd=staging, check=True)
            with self.assertRaises(module.ReadinessError): module.local_round_trip(staging, root / "remote.git", root / "clone")
            subprocess.run(["git", "tag", "-d", "unexpected"], cwd=staging, check=True, capture_output=True)
            (root / "clone").mkdir()
            with self.assertRaises(module.ReadinessError): module.local_round_trip(staging, root / "remote.git", root / "clone")

    def test_public_security_and_contribution_policies_are_explicit(self):
        security = (ROOT / "SECURITY.md").read_text()
        contributing = (ROOT / "CONTRIBUTING.md").read_text()
        self.assertIn("security advisory", security.lower())
        for phrase in ("private evidence", "credentials", "runtime state", "machine-specific paths"):
            self.assertIn(phrase, contributing.lower())
        self.assertNotIn("@gmail.com", security)

    def test_cli_has_only_optional_check_and_no_network_surface(self):
        module = self.load(); parser = module.parser()
        self.assertFalse(parser.parse_args([]).check)
        self.assertTrue(parser.parse_args(["--check"]).check)
        for option in ("--remote", "--url", "--source", "--destination", "--push"):
            with self.assertRaises(SystemExit): parser.parse_args([option, "x"])


if __name__ == "__main__": unittest.main()
