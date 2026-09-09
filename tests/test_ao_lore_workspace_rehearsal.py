import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import socket
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HTTPS = "https" + "://"
INVALID_TLD = "." + "invalid"
SCRIPT = ROOT / "scripts" / "rehearse-workspaces.py"
GENERATOR = ROOT / "tests" / "fixtures" / "ao_lore" / "workspaces" / "generate.py"
CAMPAIGN = ROOT / ".ao-lore" / "workspace-rehearsal"
WORKSPACE_IDS = [
    "primary-fixture-a",
    "secondary-fixture-b",
    "shared-reference-fixture",
]
SCENARIOS = {
    "registry_list",
    "workspace_inspect",
    "declared_reference_query",
    "unrelated_workspace_absent",
    "workspace_replay",
    "workspace_recover",
    "identity_collision_rejected",
    "reference_cycle_rejected",
    "registry_crash_recovered",
    "foreign_staging_preserved",
    "network_denied",
}


def inventory(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class WorkspaceRehearsalTests(unittest.TestCase):
    @staticmethod
    def remove_campaign():
        if CAMPAIGN.is_symlink():
            CAMPAIGN.unlink()
        else:
            shutil.rmtree(CAMPAIGN, ignore_errors=True)

    def setUp(self):
        self.remove_campaign()

    def tearDown(self):
        self.remove_campaign()

    @staticmethod
    def environment() -> dict[str, str]:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("AO_LORE_HOME", None)
        return environment

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=ROOT,
            env=self.environment(),
            capture_output=True,
            text=True,
            timeout=45,
        )

    @staticmethod
    def protected_repository(root: Path) -> None:
        files = {
            "brain/README.md": b"canonical fixture\n",
            "working/candidates/README.md": b"candidate fixture\n",
            "working/reviews/review.json": b"{}\n",
            "sources/neutral-reference-v1/record.json": b"{}\n",
        }
        for relative, body in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        for pilot in (
            "sources/opaque-domain-a-v1",
            "sources/opaque-domain-b-v1",
        ):
            path = root / pilot
            path.mkdir(parents=True)
            (path / "private-pilot.bin").write_bytes(b"must-not-open")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "fixture" + "@" + "example" + INVALID_TLD], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Fixture"], cwd=root, check=True)
        subprocess.run(
            ["git", "add", "brain", "working", "sources/neutral-reference-v1"],
            cwd=root, check=True,
        )
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    @staticmethod
    def rehearsal_module(name: str):
        spec = importlib.util.spec_from_file_location(name, SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_generator_is_deterministic_neutral_and_exactly_checkable(self):
        output = CAMPAIGN / "generated"
        generated = subprocess.run(
            [sys.executable, str(GENERATOR), "--out", str(output)],
            cwd=ROOT,
            env=self.environment(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, generated.returncode, generated.stderr)
        before = inventory(output)
        checked = subprocess.run(
            [sys.executable, str(GENERATOR), "--check", "--out", str(output)],
            cwd=ROOT,
            env=self.environment(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(0, checked.returncode, checked.stderr)
        self.assertEqual(before, inventory(output))
        manifest = json.loads((output / "fixture-manifest.json").read_text())
        self.assertEqual("ao.lore.workspace-rehearsal-fixture.v0.1", manifest["schema_version"])
        self.assertEqual(WORKSPACE_IDS, manifest["workspace_ids"])
        self.assertEqual(2, manifest["fixture_file_count"])
        self.assertEqual(
            {"fixture-manifest.json", "fixture-spec.json"},
            set(inventory(output)),
        )
        self.assertFalse(manifest["network_used"])
        self.assertFalse(manifest["customer_data"])
        tracked = (ROOT / "tests/fixtures/ao_lore/workspaces/fixture-spec.json").read_text().casefold()
        for forbidden in (
            "http://", HTTPS, "file://", "customer", "company", "named-place",
            "account", "decision", "address", "@",
        ):
            self.assertNotIn(forbidden, tracked)

        generated_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted(output.iterdir())
            if path.is_file()
        )
        lowered = generated_text.casefold()
        self.assertNotIn("://", generated_text)
        for forbidden in (
            "source" + "." + "gov", "agency" + "." + "gov",
            "/home/", "/tmp/", "file://", "named-place",
            "customer name", "street address",
        ):
            self.assertNotIn(forbidden, lowered)
        import ao_lore.evidence_graph_contracts as contracts
        self.assertFalse(hasattr(contracts, "OFFICIAL_HOSTS"))
        self.assertEqual(
            HTTPS + "fixture" + INVALID_TLD + "/source",
            contracts.validate_https_locator(
                HTTPS + "fixture" + INVALID_TLD + "/source", "fixture locator",
            ),
        )

    def test_direct_then_check_are_byte_identical_and_path_free(self):
        first = self.run_script()
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertEqual("", first.stderr)
        before = inventory(CAMPAIGN)
        second = self.run_script("--check")
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(before, inventory(CAMPAIGN))

        report = json.loads(first.stdout)
        self.assertEqual("ao.lore.workspace-rehearsal.v0.1", report["schema_version"])
        self.assertEqual(SCENARIOS, set(report["scenario_statuses"]))
        self.assertTrue(all(value == "passed" for value in report["scenario_statuses"].values()))
        self.assertEqual(len(SCENARIOS), report["scenario_count"])
        self.assertEqual(WORKSPACE_IDS, report["workspace_ids"])
        self.assertEqual(
            ["primary-fixture-a", "shared-reference-fixture"],
            report["query_consulted_workspace_ids"],
        )
        self.assertEqual(
            ["primary-fixture-a", "shared-reference-fixture"],
            report["query_evidence_workspace_ids"],
        )
        self.assertNotIn("secondary-fixture-b", report["query_consulted_workspace_ids"])
        self.assertNotIn("secondary-fixture-b", report["query_evidence_workspace_ids"])
        self.assertTrue(report["protected_inventory_unchanged"])
        self.assertEqual(report["protected_inventory_before_digest"], report["protected_inventory_after_digest"])
        self.assertTrue(report["foreign_staging_preserved"])
        for field in (
            "network_used", "credentials_used", "customer_data_accessed",
            "default_brain_accessed", "default_candidates_accessed",
            "default_sources_accessed", "pilot_content_accessed",
            "live_refresh_executed", "provider_used", "publication_authority",
            "release_authority", "deployment_authority", "authority_advanced",
        ):
            self.assertIs(report[field], False, field)
        public = json.dumps(report, sort_keys=True)
        self.assertNotIn("://", public)
        for forbidden in (str(ROOT), str(CAMPAIGN), "/opt/", "/home/", "file://"):
            self.assertNotIn(forbidden, public)

    def test_only_check_is_public_and_root_validation_refuses_protected_or_unowned(self):
        rejected = self.run_script("--root", str(CAMPAIGN))
        self.assertNotEqual(0, rejected.returncode)
        self.assertEqual("", rejected.stdout)

        spec = importlib.util.spec_from_file_location("workspace_rehearsal", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        forbidden = (
            ROOT,
            ROOT / "brain",
            ROOT / "working" / "candidates",
            ROOT / "sources",
            ROOT / "pilots",
            Path(tempfile.gettempdir()) / "unowned-workspace-rehearsal",
        )
        for path in forbidden:
            with self.subTest(path=path), self.assertRaises(ValueError):
                module._validate_campaign_root(path)

        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn("git\", \"ls-files", source)
        self.assertNotIn("rglob", source)
        self.assertNotIn("os.scandir", source)

    def test_report_digest_and_owned_marker_fail_closed(self):
        self.assertEqual(0, self.run_script().returncode)
        report_path = CAMPAIGN / "rehearsal-evidence.json"
        original = json.loads(report_path.read_text())

        def write_bound(value):
            projection = {key: item for key, item in value.items() if key != "evidence_digest"}
            body = json.dumps(projection, ensure_ascii=False, allow_nan=False,
                              sort_keys=True, separators=(",", ":")).encode()
            value["evidence_digest"] = "sha256:" + hashlib.sha256(body).hexdigest()
            report_path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")

        mutations = {
            "consulted": lambda value: value.__setitem__("query_consulted_workspace_ids", ["primary-fixture-a"]),
            "evidence": lambda value: value.__setitem__("query_evidence_workspace_ids", ["primary-fixture-a"]),
            "foreign": lambda value: value.__setitem__("foreign_staging_preserved", False),
            "scenario": lambda value: value["scenario_statuses"].__setitem__("network_denied", "failed"),
            "count": lambda value: value.__setitem__("scenario_count", 10),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                value = json.loads(json.dumps(original))
                mutate(value)
                write_bound(value)
                tampered_bytes = report_path.read_bytes()
                plain = self.run_script()
                self.assertEqual(2, plain.returncode)
                self.assertEqual("", plain.stdout)
                self.assertEqual(tampered_bytes, report_path.read_bytes())
                checked = self.run_script("--check")
                self.assertEqual(1, checked.returncode)
                self.assertEqual("", checked.stdout)
        write_bound(original)
        valid_bytes = report_path.read_bytes()
        plain = self.run_script()
        self.assertEqual(0, plain.returncode, plain.stderr)
        self.assertEqual(valid_bytes, plain.stdout.encode())
        self.assertEqual(valid_bytes, report_path.read_bytes())
        self.assertEqual(0, self.run_script("--check").returncode)

    def test_symlink_root_and_replacement_during_read_are_rejected(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            CAMPAIGN.symlink_to(Path(temporary), target_is_directory=True)
            rejected = self.run_script()
            self.assertEqual(2, rejected.returncode)
            self.assertEqual("workspace rehearsal rejected\n", rejected.stderr)
            CAMPAIGN.unlink()

        CAMPAIGN.mkdir()
        (CAMPAIGN / "campaign-owner.json").write_text("{}\n", encoding="utf-8")
        rejected = self.run_script("--check")
        self.assertEqual(2, rejected.returncode)
        self.assertEqual("workspace rehearsal rejected\n", rejected.stderr)
        shutil.rmtree(CAMPAIGN)

        self.assertEqual(0, self.run_script().returncode)
        spec = importlib.util.spec_from_file_location("workspace_rehearsal_swap", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_read = module._read_regular_at
        displaced = CAMPAIGN.with_name("workspace-rehearsal-displaced")

        def swapping_read(parent, name, maximum):
            body = original_read(parent, name, maximum)
            if name == "rehearsal-evidence.json":
                CAMPAIGN.rename(displaced)
                CAMPAIGN.mkdir()
                (CAMPAIGN / "campaign-owner.json").write_bytes(
                    (displaced / "campaign-owner.json").read_bytes()
                )
                (CAMPAIGN / "rehearsal-evidence.json").write_bytes(body)
            return body

        try:
            with patch.object(module, "_read_regular_at", swapping_read):
                with self.assertRaises(ValueError):
                    module._read_owned_report(CAMPAIGN)
        finally:
            shutil.rmtree(CAMPAIGN, ignore_errors=True)
            if displaced.exists():
                displaced.rename(CAMPAIGN)

    def test_network_guard_probes_denial_and_restores_functions(self):
        spec = importlib.util.spec_from_file_location("workspace_rehearsal_network", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        originals = (socket.socket, socket.create_connection, socket.getaddrinfo)
        with module._deny_network():
            self.assertNotEqual(originals, (socket.socket, socket.create_connection, socket.getaddrinfo))
        self.assertEqual(originals, (socket.socket, socket.create_connection, socket.getaddrinfo))
        with self.assertRaisesRegex(RuntimeError, "injected"):
            with module._deny_network():
                raise RuntimeError("injected")
        self.assertEqual(originals, (socket.socket, socket.create_connection, socket.getaddrinfo))

    def test_protected_inventory_binds_tracked_worktree_content(self):
        module = self.rehearsal_module("workspace_rehearsal_inventory_content")
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.protected_repository(repository)
            with patch.object(module, "REPOSITORY", repository):
                before = module._protected_inventory_digest()
                (repository / "brain/README.md").write_bytes(b"changed canonical fixture\n")
                after = module._protected_inventory_digest()
        self.assertNotEqual(before, after)

    def test_protected_inventory_rejects_links_special_files_and_root_swap(self):
        module = self.rehearsal_module("workspace_rehearsal_inventory_safety")
        mutations = ("symlink", "hardlink", "fifo")
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory)
                self.protected_repository(repository)
                target = repository / "brain/README.md"
                original = target.read_bytes()
                target.unlink()
                if mutation == "symlink":
                    replacement = repository / "replacement.txt"
                    replacement.write_bytes(original)
                    target.symlink_to(replacement)
                elif mutation == "hardlink":
                    replacement = repository / "replacement.txt"
                    replacement.write_bytes(original)
                    os.link(replacement, target)
                else:
                    os.mkfifo(target)
                with patch.object(module, "REPOSITORY", repository), self.assertRaises(ValueError):
                    module._protected_inventory_digest()

        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.protected_repository(repository)
            original_read = module._read_regular_at
            displaced = repository / "brain-displaced"

            def swapping_read(parent, name, maximum):
                body = original_read(parent, name, maximum)
                if name == "README.md" and not displaced.exists():
                    (repository / "brain").rename(displaced)
                    (repository / "brain").mkdir()
                    (repository / "brain/README.md").write_bytes(body)
                return body

            with patch.object(module, "REPOSITORY", repository), patch.object(
                module, "_read_regular_at", side_effect=swapping_read,
            ), self.assertRaises(ValueError):
                module._protected_inventory_digest()

    def test_unrelated_roots_are_opaque_and_never_walked_or_opened(self):
        module = self.rehearsal_module("workspace_rehearsal_inventory_opaque")
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.protected_repository(repository)
            original = module._tracked_root_inventory
            original_os_open = module.os.open
            inventoried = []
            opened_pilots = []

            def tracked(repository_fd, prefix):
                inventoried.append(prefix)
                self.assertNotIn("domain-a", prefix)
                self.assertNotIn("domain-b", prefix)
                return original(repository_fd, prefix)

            def guarded_open(path, *args, **kwargs):
                if isinstance(path, (str, bytes, os.PathLike)):
                    candidate = Path(os.fsdecode(path))
                    if candidate.is_absolute() and any(
                        candidate == repository / pilot
                        or repository / pilot in candidate.parents
                        for pilot in module._OPAQUE_PILOT_ROOTS
                    ):
                        opened_pilots.append(candidate)
                return original_os_open(path, *args, **kwargs)

            with patch.object(module, "REPOSITORY", repository), patch.object(
                module, "_tracked_root_inventory", side_effect=tracked,
            ), patch.object(
                module.os, "open", side_effect=guarded_open,
            ), patch.object(Path, "rglob", side_effect=AssertionError("opaque tree walked")), patch(
                "builtins.open", side_effect=AssertionError("opaque content opened"),
            ):
                digest = module._protected_inventory_digest()

        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(
            ["brain", "working/candidates", "working/reviews",
             "sources/neutral-reference-v1"],
            inventoried,
        )
        self.assertEqual([], opened_pilots)

    def test_protected_inventory_enforces_total_entry_and_byte_budgets(self):
        module = self.rehearsal_module("workspace_rehearsal_inventory_budgets")
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            self.protected_repository(repository)
            with patch.object(module, "REPOSITORY", repository), patch.object(
                module, "_MAX_PROTECTED_FILES", 1,
            ), self.assertRaisesRegex(ValueError, "budget exceeded"):
                module._protected_inventory_digest()
            with patch.object(module, "REPOSITORY", repository), patch.object(
                module, "_MAX_PROTECTED_TOTAL_BYTES", 1,
            ), self.assertRaisesRegex(ValueError, "budget exceeded"):
                module._protected_inventory_digest()


if __name__ == "__main__":
    unittest.main()
