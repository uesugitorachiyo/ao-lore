import hashlib
import json
import os
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore._strict_io import ContractError
from ao_lore.public_release_contracts import validate_public_release_policy
from ao_lore.public_release_safety import PublicReleaseLimits, _content_reasons, scan_public_tree
from tests.neutral_fixture_utils import neutral_https_locator, neutral_public_https_locator


ROOT = Path(__file__).resolve().parents[1]


EXPECTED_WORKFLOW = b"""name: AO Lore clean gate

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions: {}

jobs:
  check:
    runs-on: ubuntu-24.04
    timeout-minutes: 15
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          persist-credentials: false
      - uses: actions/setup-python@42375524e23c412d93fb67b49958b491fce71c38
        with:
          python-version: '3.11'
      - run: python -m pip install --disable-pip-version-check --no-input --require-hashes --requirement ci-requirements.txt
      - run: make check
"""
# Independent binding copied from the approved canonical bytes once.
REAL_WORKFLOW_SHA256 = "5d51db43186722b7f32d8d75344b1345918b2fac1750865e493b93dc49ddc608"


def _workflow_with(field, value):
    """Make one bounded mutation of the canonical workflow bytes."""
    text = EXPECTED_WORKFLOW.decode()
    if field == "extra-top-level":
        text = text.replace("name: AO Lore clean gate\n", "name: AO Lore clean gate\nextra: true\n")
    elif field == "extra-job":
        text = text.replace("  check:\n", "  check:\n    extra: true\n")
    elif field == "extra-step":
        text = text.replace("      - run: make check\n", "      - run: make check\n        extra: true\n")
    elif field == "missing-timeout":
        text = text.replace("    timeout-minutes: 15\n", "")
    elif field == "missing-default-deny":
        text = text.replace("permissions: {}\n", "")
    elif field == "missing-job-permission":
        text = text.replace("    permissions:\n      contents: read\n", "")
    elif field == "missing-persist":
        text = text.replace("          persist-credentials: false\n", "")
    elif field in {"permissions", "run", "uses", "persist-credentials"}:
        if field == "permissions":
            text = text.replace("      contents: read", f"      {value}")
        elif field == "run":
            text = text.replace("      - run: make check", f"      - run: {value}")
        elif field == "uses":
            text = text.replace(
                "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
                value,
            )
        else:
            text = text.replace("persist-credentials: false", value)
    else:
        raise AssertionError(f"unknown workflow mutation: {field}")
    return text.encode()


def assert_workflow_contract(workflow):
    """Test-local exact-byte allowlist; this intentionally is not a YAML parser."""
    if workflow != EXPECTED_WORKFLOW:
        raise ContractError("workflow is not the canonical allowlisted contract")
    text = workflow.decode("utf-8")
    if not re.search(r"actions/(?:checkout|setup-python)@[0-9a-f]{40}", text):
        raise ContractError("workflow action reference is not a full immutable SHA")


class PublicReleaseSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.policy = validate_public_release_policy(json.loads((ROOT / "docs/public-release/export-policy.json").read_text()))

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, body=b"safe public text\n", mode=0o644):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        target.chmod(mode)
        return target

    def test_safe_tree_is_accepted_with_deterministic_path_free_summary(self):
        self.write("README.md")
        first = scan_public_tree(self.root, self.policy)
        second = scan_public_tree(self.root, self.policy)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(first["reason_codes"], [])
        self.assertEqual(first["file_count"], 1)
        self.assertNotIn(self.temp.name, json.dumps(first))

    def test_rejects_private_paths_secrets_binary_and_control_text(self):
        cases = (
            (b"installed at /home/alice/private\n", "private_path"),
            (b"AWS_SECRET_ACCESS_KEY=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890ABCD\n", "secret_candidate"),
            (b"\x00\x01binary", "binary_not_allowed"),
            (b"safe\x07text", "control_character"),
            (b"contact alice" + b"@" + b"example.com\n", "email_address"),
            (b"connect 192" + b".168.7.22\n", "ip_address"),
            (b"-----BEGIN PRIVATE KEY-----\nabc\n", "secret_candidate"),
        )
        for body, reason in cases:
            with self.subTest(reason=reason):
                path = self.write("README.md", body)
                report = scan_public_tree(self.root, self.policy)
                self.assertEqual(report["status"], "rejected")
                self.assertIn(reason, report["reason_codes"])
                path.unlink()

    def test_arbitrary_github_noreply_address_is_an_email_finding(self):
        self.write(
            "README.md",
            b"contact arbitrary-account@users." + b"noreply.github.com\n",
        )
        report = scan_public_tree(self.root, self.policy)
        self.assertEqual("rejected", report["status"])
        self.assertIn("email_address", report["reason_codes"])

    def test_rejects_private_network_locators_but_accepts_public_official_urls(self):
        for locator in (
            b"https://build.service.internal/artifact\n",
            b"http://localhost:8080/status\n",
            b"ssh://operator" + b"@" + b"intranet.corp/repository\n",
            b"hostname=database.service.internal\n",
        ):
            with self.subTest(locator=locator):
                path = self.write("README.md", locator)
                report = scan_public_tree(self.root, self.policy)
                self.assertEqual("rejected", report["status"])
                self.assertIn("network_locator", report["reason_codes"])
                path.unlink()
        self.write(
            "README.md",
            neutral_public_https_locator(("source",), ("record",)).encode() + b"\n",
        )
        self.assertEqual("accepted", scan_public_tree(self.root, self.policy)["status"])

    def test_rejects_non_dns_and_noncanonical_locator_hosts(self):
        rejected_hosts = (
            "127" + ".0.0.1",
            "127" + ".1",
            "0177" + ".0.0.1",
            "0x7f" + ".0.0.1",
            "localhost.",
            "8" + ".8.8.8",
            "[::1]",
            "[2001:4860:4860::8888]",
            "Docs.example.com",
            "docs.example.com.",
            "bad_host.example.com",
        )
        for host in rejected_hosts:
            with self.subTest(host=host):
                path = self.write(
                    "README.md", f"https://{host}/record\n".encode(),
                )
                report = scan_public_tree(self.root, self.policy)
                self.assertEqual("rejected", report["status"])
                self.assertIn("network_locator", report["reason_codes"])
                path.unlink()

        self.write("README.md", b"https://docs.example.com/record\n")
        self.assertEqual("accepted", scan_public_tree(self.root, self.policy)["status"])

    def test_malformed_locator_text_is_never_skipped(self):
        malformed = (
            b"https://localhost\\record",
            b"https://127.0.0.1\\record",
            b"https://0177.0.0.1\\record",
            b"https://docs.example.com/(?:record)",
        )
        for locator in malformed:
            with self.subTest(locator=locator):
                path = self.write("README.md", locator + b"\n")
                report = scan_public_tree(self.root, self.policy)
                self.assertEqual("rejected", report["status"])
                self.assertIn("network_locator", report["reason_codes"])
                path.unlink()

        self.write("README.md", b"https://docs.example.com/record\n")
        self.assertEqual("accepted", scan_public_tree(self.root, self.policy)["status"])

    def test_literal_unsafe_locator_bytes_are_findings_without_named_exemptions(self):
        reserved_invalid = neutral_https_locator(("source",), ("in", "valid"), ("record",))
        reserved_test = neutral_https_locator(("source",), ("te", "st"), ("record",))
        locators = (
            "https" + "://" + "ao" + "." + "local/record",
            "https" + "://" + "source" + "." + "internal/record",
            reserved_invalid,
            reserved_test,
            "https" + "://" + "192" + ".168.20.4/record",
            "https" + "://" + "operator" + "@" + "source.example/record",
            "https" + "://[broken/record",
        )
        for locator in locators:
            with self.subTest(locator=locator):
                target = self.write("README.md", locator.encode() + b"\n")
                report = scan_public_tree(self.root, self.policy)
                self.assertEqual("rejected", report["status"])
                self.assertTrue(report["reason_codes"])
                target.unlink()

    def test_network_locator_exception_is_exactly_digest_bound(self):
        target = self.write(
            "tests/fixtures/inert.txt",
            neutral_https_locator(
                ("fixture", "source"), ("in", "valid"), ("resource",),
            ).encode() + b"\n",
        )
        self.assertIn("network_locator", scan_public_tree(self.root, self.policy)["reason_codes"])
        policy = json.loads(json.dumps(self.policy))
        policy["exceptions"] = [{
            "path": "tests/fixtures/inert.txt",
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "reason_code": "inert_network_locator",
            "test_only": True,
        }]
        from ao_lore.public_release_contracts import canonical_digest
        policy["policy_digest"] = canonical_digest(policy, omit="policy_digest")
        self.assertEqual("accepted", scan_public_tree(self.root, policy)["status"])
        target.write_bytes(target.read_bytes() + b"changed\n")
        self.assertIn("network_locator", scan_public_tree(self.root, policy)["reason_codes"])

    def test_network_locator_exception_never_suppresses_unrelated_email_or_ip(self):
        unsafe_locator = neutral_https_locator(
            ("fixture", "source"), ("in", "valid"), ("resource",),
        ).encode()
        unrelated_values = (
            (b"operator" + b"@" + b"public.example", "email_address"),
            (b"192" + b".168.40.8", "ip_address"),
        )
        for unrelated, reason in unrelated_values:
            with self.subTest(reason=reason):
                target = self.write(
                    "tests/fixtures/inert.txt",
                    unsafe_locator + b"\n" + unrelated + b"\n",
                )
                policy = json.loads(json.dumps(self.policy))
                policy["exceptions"] = [{
                    "path": "tests/fixtures/inert.txt",
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "reason_code": "inert_network_locator",
                    "test_only": True,
                }]
                from ao_lore.public_release_contracts import canonical_digest
                policy["policy_digest"] = canonical_digest(
                    policy, omit="policy_digest",
                )
                report = scan_public_tree(self.root, policy)
                self.assertEqual("rejected", report["status"])
                self.assertIn(reason, report["reason_codes"])
                target.unlink()

    def test_rejects_links_special_files_modes_collisions_and_budgets(self):
        self.write("README.md")
        os.link(self.root / "README.md", self.root / "LICENSE")
        self.assertIn("hardlink_not_allowed", scan_public_tree(self.root, self.policy)["reason_codes"])
        (self.root / "LICENSE").unlink()
        (self.root / "alias").symlink_to("README.md")
        self.assertIn("symlink_not_allowed", scan_public_tree(self.root, self.policy)["reason_codes"])
        (self.root / "alias").unlink()
        os.mkfifo(self.root / "pipe")
        self.assertIn("special_file_not_allowed", scan_public_tree(self.root, self.policy)["reason_codes"])
        (self.root / "pipe").unlink()
        self.write("LICENSE", mode=0o755)
        self.assertIn("executable_mode_not_allowed", scan_public_tree(self.root, self.policy)["reason_codes"])
        (self.root / "LICENSE").unlink()
        self.write("readme.md")
        self.assertIn("case_collision", scan_public_tree(self.root, self.policy)["reason_codes"])
        self.assertIn("file_count_exceeded", scan_public_tree(self.root, self.policy, limits=PublicReleaseLimits(max_files=1))["reason_codes"])

    def test_rejects_unallowed_archive_depth_and_oversize(self):
        self.write("README.md", b"PK\x03\x04archive")
        self.assertIn("binary_not_allowed", scan_public_tree(self.root, self.policy)["reason_codes"])
        (self.root / "README.md").unlink()
        self.write("src/a/b/c/d.txt", b"safe")
        self.assertIn("depth_exceeded", scan_public_tree(self.root, self.policy, limits=PublicReleaseLimits(max_depth=2))["reason_codes"])
        self.assertIn("file_size_exceeded", scan_public_tree(self.root, self.policy, limits=PublicReleaseLimits(max_file_bytes=3))["reason_codes"])

    def test_exact_inert_exception_accepts_only_bound_bytes(self):
        target = self.write("tests/fixtures/inert.txt", b"example /home/user only\n")
        policy = json.loads(json.dumps(self.policy))
        policy["exceptions"] = [{"path": "tests/fixtures/inert.txt", "sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "reason_code": "inert_private_path", "test_only": True}]
        from ao_lore.public_release_contracts import canonical_digest
        policy["policy_digest"] = canonical_digest(policy, omit="policy_digest")
        self.assertEqual(scan_public_tree(self.root, policy)["status"], "accepted")
        target.write_bytes(target.read_bytes() + b"changed")
        self.assertEqual(scan_public_tree(self.root, policy)["status"], "rejected")

    def test_scanner_does_not_use_subprocess_or_network(self):
        self.write("README.md")
        with patch("subprocess.run", side_effect=AssertionError("subprocess used")), patch.object(socket, "socket", side_effect=AssertionError("network used")):
            self.assertEqual(scan_public_tree(self.root, self.policy)["status"], "accepted")

    def test_non_directory_root_fails_closed(self):
        target = self.write("README.md")
        with self.assertRaises(ContractError):
            scan_public_tree(target, self.policy)

    def test_workflow_contract_requires_read_only_pinned_clean_gate(self):
        workflow = (ROOT / ".github/workflows/ci.yml").read_bytes()
        self.assertEqual(EXPECTED_WORKFLOW, workflow)
        self.assertEqual(hashlib.sha256(EXPECTED_WORKFLOW).hexdigest(), REAL_WORKFLOW_SHA256)
        assert_workflow_contract(workflow)

    def test_workflow_contract_rejects_bounded_unsafe_mutations(self):
        mutations = (
            ("missing-timeout", ""),
            ("missing-default-deny", ""),
            ("missing-job-permission", ""),
            ("missing-persist", ""),
            ("permissions", "contents: write"),
            ("permissions", "contents: read-write"),
            ("run", "cat .ao-lore/private-calibration-assets.json"),
            ("run", "gh release create v1"),
            ("run", "echo ${{ secrets.TEST }}"),
            ("run", "echo github.token"),
            ("run", "echo AO_LORE_HOME working/candidates/ brain/"),
            ("run", "provider upload publish deploy"),
            ("uses", "actions/checkout@v4"),
            ("uses", "some/publish-action@" + "a" * 39),
            ("uses", "actions/checkout@" + "A" * 40),
            ("persist-credentials", "persist-credentials: true"),
            ("extra-top-level", ""),
            ("extra-job", ""),
            ("extra-step", ""),
        )
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                with self.assertRaises(ContractError):
                    assert_workflow_contract(_workflow_with(field, value))

    def test_safety_fixture_exceptions_are_exactly_digest_bound(self):
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        target = "tests/test_ao_lore_public_release_safety.py"
        records = [item for item in policy["exceptions"] if item.get("path") == target]
        self.assertEqual(3, len(records))
        self.assertEqual(
            {"inert_network_locator", "inert_private_path", "inert_secret_candidate"},
            {item.get("reason_code") for item in records},
        )
        expected_sha = hashlib.sha256((ROOT / target).read_bytes()).hexdigest()
        for item in records:
            self.assertEqual(target, item.get("path"))
            self.assertIs(item.get("test_only"), True)
            self.assertEqual(expected_sha, item.get("sha256"))
            self.assertNotRegex(item["path"], r"[*?]|(?:^|/)[^/]+/\*$")

    def test_every_policy_exception_is_an_existing_exact_current_test_binding(self):
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        allowed_reasons = {
            "inert_network_locator", "inert_private_path", "inert_secret_candidate",
        }
        paths = []
        for item in policy["exceptions"]:
            path = item["path"]
            target = ROOT / path
            paths.append((path, item["reason_code"]))
            self.assertTrue(path.startswith("tests/"))
            self.assertTrue(target.is_file(), path)
            self.assertIs(item["test_only"], True)
            self.assertIn(item["reason_code"], allowed_reasons)
            self.assertNotRegex(path, r"[*?]|(?:^|/)[^/]+/\*$")
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), item["sha256"])
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))

    def test_every_exception_reason_is_semantically_necessary_for_bound_bytes(self):
        policy = json.loads((ROOT / "docs/public-release/export-policy.json").read_text())
        reason_map = {
            "inert_network_locator": {"network_locator"},
            "inert_private_path": {"private_path"},
            "inert_secret_candidate": {"secret_candidate"},
        }
        for item in policy["exceptions"]:
            with self.subTest(path=item["path"], reason=item["reason_code"]):
                body = (ROOT / item["path"]).read_bytes()
                raw_reasons = _content_reasons(item["path"], body, policy)
                self.assertTrue(reason_map[item["reason_code"]].intersection(raw_reasons))


if __name__ == "__main__":
    unittest.main()
