from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(relative: str) -> str:
    path = ROOT / relative
    return path.read_text(encoding="utf-8") if path.is_file() else ""


class WorkspaceDocumentationTests(unittest.TestCase):
    def test_current_product_uses_company_local_workspace_vocabulary(self):
        readme = read("README.md")
        roadmap = read("ROADMAP-ADDENDUM.md")
        workflow = read("docs/workflows/workspaces.md")
        for text in (readme, roadmap, workflow):
            normalized = " ".join(text.split())
            self.assertIn("one company", normalized)
            self.assertIn("workspace", normalized)
        self.assertIn("reference, property, matter, or operations", workflow)
        self.assertIn("## Month 2 — Isolated evidence workspaces\n\nCompleted.", roadmap)
        self.assertNotIn("Status: In progress.", roadmap)
        self.assertIn("1,405 tests and 3 skips", roadmap)
        self.assertIn("private-PDF sandbox", roadmap)

    def test_fresh_checkout_and_direct_reference_boundaries_are_documented(self):
        workflow = read("docs/workflows/workspaces.md")
        normalized = " ".join(workflow.split())
        self.assertIn("fresh public checkout has an empty workspace registry", normalized)
        self.assertIn("does not create registry state", normalized)
        self.assertIn("direct, read-only, one-way, and non-transitive", normalized)
        self.assertIn("originating workspace", normalized)
        self.assertIn("graph, source, claim or edge, and evidence digest", normalized)

    def test_fixed_workspace_cli_and_results_are_documented(self):
        workflow = read("docs/workflows/workspaces.md")
        commands = (
            "ao-lore workspace list --json",
            "ao-lore workspace inspect --workspace <registered-id> --json",
            'ao-lore workspace query --workspace <registered-id> --prompt "<text>" --json',
            "ao-lore workspace refresh --workspace <registered-id> --json",
            "ao-lore workspace replay --workspace <registered-id> --json",
            "ao-lore workspace recover --workspace <registered-id> --json",
        )
        for command in commands:
            self.assertIn(command, workflow)
        for result in ("answer", "partial", "refuse", "investigate"):
            self.assertIn(f"`{result}`", workflow)
        for reason in (
            "workspace_unknown",
            "workspace_inactive",
            "registry_invalid",
            "identity_collision",
            "reference_not_declared",
            "reference_cycle",
            "workspace_binding_drift",
            "freshness_investigation_required",
            "recovery_pending",
        ):
            self.assertIn(f"`{reason}`", workflow)

    def test_lifecycle_rehearsal_compatibility_backup_and_candidate_path_are_documented(self):
        readme = read("README.md")
        workflow = read("docs/workflows/workspaces.md")
        self.assertIn("scripts/rehearse-workspaces.py --check", readme)
        self.assertIn("active`, `inactive`, or `investigate", workflow)
        self.assertIn("offline", workflow)
        self.assertNotIn("compatibility", workflow.lower())
        self.assertIn("generic", readme.lower())
        self.assertIn("backup", workflow.lower())
        self.assertIn("separately governed candidate", workflow)

    def test_workspace_docs_explicitly_deny_out_of_scope_authority(self):
        workflow = read("docs/workflows/workspaces.md")
        normalized = " ".join(workflow.split()).lower()
        for denial in (
            "no multi-company tenancy",
            "no customer data",
            "no implicit global search",
            "no candidate or canonical authority",
            "no live acquisition or refresh",
            "no publication",
        ):
            self.assertIn(denial, normalized)

    def test_refresh_workflow_uses_only_selected_generic_workspace(self):
        refresh = read("docs/workflows/refresh-evidence-graph.md")
        normalized = " ".join(refresh.split()).lower()
        self.assertIn("ao-lore workspace refresh --workspace <registered-id> --json", refresh)
        self.assertIn("selected workspace", normalized)
        self.assertIn("generic workspace", normalized)
        self.assertIn("newly observed bytes differing from the immutable retained baseline", normalized)
        self.assertIn("follow only validated allowlisted redirect hops", normalized)
        self.assertIn("no credentials or providers", normalized)
        self.assertIn("does not cascade", normalized)
        self.assertIn("clean baseline is offline by default", normalized)
        self.assertIn("separately reviewed trusted domain transport", normalized)
        self.assertIn("globally routable", normalized)
        self.assertIn("connected peer", normalized)
        self.assertIn("disposable refresh fixtures do not inspect canonical roots", normalized)
        readme = " ".join(read("README.md").split()).lower()
        self.assertIn("clean baseline is offline by default", readme)
        self.assertIn("separately reviewed trusted domain transport", readme)
        self.assertIn("globally routable", readme)
        self.assertIn("connected peer", readme)
        self.assertNotIn("scripts/rehearse-evidence-freshness.py", refresh)
        self.assertNotIn("ao-lore evidence-graph refresh", refresh)
        self.assertNotIn("acquire --json", refresh)

    def test_workspace_rehearsal_protected_inventory_boundary_is_explicit(self):
        workflow = read("docs/workflows/workspaces.md")
        normalized = " ".join(workflow.split()).lower()
        self.assertIn("bounded metadata/inventory for protected tracked roots", normalized)
        self.assertIn("opaque external/private roots are never walked/opened", normalized)

    def test_agent_instructions_preserve_durable_workspace_safety(self):
        agents = read("AGENTS.md")
        normalized = " ".join(agents.split()).lower()
        self.assertIn("One AO Lore deployment belongs to one company", agents)
        self.assertIn("Workspace reference imports are direct, read-only, one-way, and non-transitive", agents)
        self.assertIn("Every workspace query evidence identity", agents)
        self.assertIn("Fresh public checkouts expose an empty workspace registry", agents)
        self.assertIn("selected `workspace refresh` is the only operation that may use the network", normalized)
        self.assertIn("clean core has no default http transport", normalized)
        self.assertIn("unavailable unless a trusted private deployment transport is installed", normalized)
        self.assertIn("policy-bound locator", normalized)
        self.assertIn("validated explicit redirect hosts or peer policy", normalized)
        self.assertIn("no public url, host, root, network-policy, force, or authority controls", normalized)
        self.assertIn("all other operations are offline", normalized)
        self.assertNotIn("evidence-graph acquire", normalized)
        self.assertNotIn("evidence-graph refresh", normalized)


if __name__ == "__main__":
    unittest.main()
