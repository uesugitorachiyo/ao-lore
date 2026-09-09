import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path

from ao_lore.candidates import inspect_candidate
from ao_lore._strict_io import ContractError
from ao_lore.evidence_graph import GraphDependencies, publish_evidence_graph
from ao_lore.evidence_selection import EvidenceSelectionError
from ao_lore.workspace_documents import (
    WorkspaceDocumentDependencies,
    WorkspaceDocumentError,
    publish_workspace_documents,
)
from ao_lore.workspace_query import (
    WorkspaceDocumentQuerySnapshot,
    WorkspaceGraphSnapshot,
    WorkspaceQuerySnapshot,
)
from ao_lore.workspace_registry import (
    WorkspaceRegistryDependencies,
    WorkspaceRegistrySnapshot,
    load_workspace_registry,
    publish_workspace_registry_generation,
    select_workspace,
)
from tests.test_ao_lore_document_evidence_query import generation as document_generation
from tests.test_ao_lore_evidence_candidate import (
    definition,
    document_block_from_snapshot,
    graph_claim_from_snapshot,
)
from tests.test_ao_lore_workspace_query import graph_variant


ROOT = Path(__file__).resolve().parents[1]
ACTUAL_HEAD = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=ROOT,
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()


def fixture_snapshot(root: Path) -> WorkspaceQuerySnapshot:
    graph = graph_variant("graph-workspace-a")
    publish_workspace_registry_generation(
        (definition("workspace-a", None, graph),),
        WorkspaceRegistryDependencies(root),
    )
    ir = document_generation()["documents"][0]["document_ir"]
    publish_workspace_documents(
        WorkspaceDocumentDependencies(root),
        "workspace-a",
        (ir,),
    )
    graph_root = root / "workspaces" / "state" / "workspace-a" / "graph"
    graph_root.mkdir(parents=True, exist_ok=True)
    publish_evidence_graph(graph, GraphDependencies(graph_root))
    registry = load_workspace_registry(WorkspaceRegistryDependencies(root))
    selection = select_workspace(registry, "workspace-a")
    generation = document_generation()
    generation = publish_workspace_documents(
        WorkspaceDocumentDependencies(root), "workspace-a", ()
    ) if False else None
    from ao_lore.workspace_documents import load_workspace_documents
    current_generation = load_workspace_documents(
        WorkspaceDocumentDependencies(root), "workspace-a"
    ).generation
    return WorkspaceQuerySnapshot(
        WorkspaceRegistrySnapshot(registry.generations, registry.workspaces),
        selection,
        (WorkspaceGraphSnapshot("workspace-a", graph),),
        documents=(WorkspaceDocumentQuerySnapshot("workspace-a", current_generation),),
    )


class EvidenceSelectionApplyTests(unittest.TestCase):
    def setUp(self):
        self.runtime = tempfile.TemporaryDirectory()
        self.candidates = tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates")
        self.root = Path(self.runtime.name)
        self.candidate_root = Path(self.candidates.name)
        self.snapshot = fixture_snapshot(self.root)
        from ao_lore.evidence_selection import (
            EvidenceSelectionDependencies,
            authorize_evidence_selection,
            prepare_evidence_selection,
        )

        self.dependencies = EvidenceSelectionDependencies(
            runtime_root=self.root,
            candidate_root=self.candidate_root,
            source_head=lambda: ACTUAL_HEAD,
            now=lambda: "2026-08-14T12:00:00Z",
        )
        proposal = prepare_evidence_selection(
            self.snapshot,
            "workspace-a",
            (
                document_block_from_snapshot(self.snapshot)["evidence_id"],
                graph_claim_from_snapshot(self.snapshot)["evidence_id"],
            ),
            now="2026-08-14T12:00:00Z",
        )
        self.proposal = proposal
        self.authorization = authorize_evidence_selection(
            self.dependencies,
            self.proposal,
            "fixture-operator",
        )
        self.proposal_id = self.proposal["proposal_id"]
        self.authorization_id = self.authorization["authorization_id"]

    def tearDown(self):
        self.runtime.cleanup()
        self.candidates.cleanup()

    def test_apply_consumes_one_exact_authorization_and_persists_candidate(self):
        from ao_lore.evidence_selection import apply_evidence_selection

        report = apply_evidence_selection(
            self.dependencies,
            self.proposal_id,
            self.authorization_id,
        )
        self.assertEqual(report["status"], "committed")
        self.assertEqual(
            inspect_candidate(report["candidate_id"], candidate_root=self.candidate_root)["review_status"],
            "unreviewed",
        )
        candidate_root = self.candidate_root / report["candidate_id"]
        expected = {item.rsplit("/", 1)[1] for item in self.proposal["allowed_write_set"]}
        self.assertEqual(
            expected,
            {item.name for item in candidate_root.iterdir()},
        )

    def test_private_apply_threads_only_the_sealed_persistence_callable(self):
        from ao_lore.candidates import load_verified_candidate, persist_candidate
        from ao_lore.evidence_selection import (
            _EvidenceSelectionApplyDependencies,
            _apply_evidence_selection_with_dependencies,
        )

        calls = []

        @contextmanager
        def sealed_lock():
            calls.append("lock-enter")
            try:
                yield
            finally:
                calls.append("lock-exit")

        def sealed_persist(result, provenance):
            calls.append(result["candidate"]["candidate_id"])
            return persist_candidate(
                result, provenance, candidate_root=self.candidate_root,
            )

        report = _apply_evidence_selection_with_dependencies(
            self.dependencies,
            self.proposal_id,
            self.authorization_id,
            apply_dependencies=_EvidenceSelectionApplyDependencies(
                sealed_lock,
                sealed_persist,
                lambda candidate_id: load_verified_candidate(
                    candidate_id, candidate_root=self.candidate_root,
                ),
                lambda candidate_id: {
                    item.name
                    for item in (self.candidate_root / candidate_id).iterdir()
                },
            ),
        )

        self.assertEqual(
            ["lock-enter", report["candidate_id"], "lock-exit"], calls,
        )
        self.assertEqual(
            "unreviewed",
            inspect_candidate(
                report["candidate_id"], candidate_root=self.candidate_root,
            )["review_status"],
        )

    def test_apply_never_creates_review_or_canonical_state(self):
        from ao_lore.evidence_selection import apply_evidence_selection

        report = apply_evidence_selection(
            self.dependencies,
            self.proposal_id,
            self.authorization_id,
        )
        inspection = inspect_candidate(report["candidate_id"], candidate_root=self.candidate_root)
        self.assertEqual(0, inspection["verified_review_events"])
        self.assertFalse((self.root / "brain").exists())
        selection_root = self.root / "workspaces" / "state" / "workspace-a" / "selection"
        audit = json.loads((selection_root / "audits" / f"{report['transaction_id']}.json").read_text(encoding="utf-8"))
        completion = json.loads((selection_root / "completions" / f"{report['transaction_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(("committed", "committed"), (audit["status"], completion["status"]))
        self.assertEqual(report["candidate_id"], audit["candidate_id"])
        self.assertEqual(report["candidate_id"], completion["candidate_id"])

    def test_apply_rejects_source_head_drift_before_mutation(self):
        from ao_lore.evidence_selection import (
            EvidenceSelectionDependencies,
            apply_evidence_selection,
        )

        drifted = EvidenceSelectionDependencies(
            runtime_root=self.root,
            candidate_root=self.candidate_root,
            source_head=lambda: "0" * 40,
            now=lambda: "2026-08-14T12:00:00Z",
        )
        with self.assertRaises(Exception):
            apply_evidence_selection(
                drifted,
                self.proposal_id,
                self.authorization_id,
            )

    def test_apply_rejects_expired_proposal_and_parent_symlink_escape(self):
        from ao_lore.evidence_selection import (
            EvidenceSelectionDependencies,
            EvidenceSelectionError,
            apply_evidence_selection,
        )

        expired = EvidenceSelectionDependencies(
            runtime_root=self.root,
            candidate_root=self.candidate_root,
            source_head=lambda: ACTUAL_HEAD,
            now=lambda: "2026-08-14T12:06:00Z",
        )
        with self.assertRaises(EvidenceSelectionError):
            apply_evidence_selection(
                expired,
                self.proposal_id,
                self.authorization_id,
            )

        index = self.root / ".selection-index"
        backup = self.root / ".selection-index-backup"
        index.rename(backup)
        target = self.root / "external-index"
        target.mkdir()
        index.symlink_to(target, target_is_directory=True)
        try:
            with self.assertRaises(EvidenceSelectionError):
                apply_evidence_selection(
                    self.dependencies,
                    self.proposal_id,
                    self.authorization_id,
                )
        finally:
            index.unlink()
            backup.rename(index)

    def test_apply_exact_retry_is_stable_and_competing_calls_serialize(self):
        from ao_lore.evidence_selection import apply_evidence_selection

        first = apply_evidence_selection(
            self.dependencies,
            self.proposal_id,
            self.authorization_id,
        )
        second = apply_evidence_selection(
            self.dependencies,
            self.proposal_id,
            self.authorization_id,
        )
        self.assertEqual(first, second)

        barrier = threading.Barrier(2)
        results = []
        errors = []

        def run():
            try:
                barrier.wait()
                results.append(
                    apply_evidence_selection(
                        self.dependencies,
                        self.proposal_id,
                        self.authorization_id,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(2, len(results))

    def test_apply_state_integrity_matrix(self):
        from ao_lore.benchmark import canonical_digest
        from ao_lore.evidence_selection import (
            EvidenceSelectionError,
            apply_evidence_selection,
        )
        from tests.test_ao_lore_evidence_candidate import definition

        selection_root = self.root / "workspaces" / "state" / "workspace-a" / "selection"
        proposal_path = selection_root / "proposals" / f"{self.proposal_id}.json"
        auth_path = selection_root / "authorizations" / f"{self.authorization_id}.json"
        auth_index = self.root / ".selection-index" / "authorizations" / f"{self.authorization_id}.json"

        def mutate_json(path, mutate):
            value = json.loads(path.read_text(encoding="utf-8"))
            mutate(value)
            digest_field = next(
                field for field in ("authorization_digest", "proposal_digest") if field in value
            )
            value[digest_field] = canonical_digest(
                {key: item for key, item in value.items() if key != digest_field}
            )
            path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")

        cases = []

        def workspace_mismatch():
            auth_index.write_text(json.dumps({"workspace_id": "workspace-b"}) + "\n", encoding="utf-8")
            return [auth_index]
        cases.append(("workspace mismatch", workspace_mismatch))

        def digest_mismatch():
            mutate_json(auth_path, lambda value: value.update(proposal_digest="sha256:" + "f" * 64))
            return [auth_path]
        cases.append(("proposal/auth digest mismatch", digest_mismatch))

        def stored_source_head_drift():
            mutate_json(auth_path, lambda value: value.update(source_head="0" * 40))
            return [auth_path]
        cases.append(("stored source_head drift", stored_source_head_drift))

        def auth_expiry():
            mutate_json(auth_path, lambda value: value.update(expires_at="2026-08-14T11:59:59Z"))
            return [auth_path]
        cases.append(("authorization expiry", auth_expiry))

        def registry_drift():
            publish_workspace_registry_generation(
                (definition("workspace-a", None, graph_variant("graph-workspace-a"), workspace_version=99),),
                WorkspaceRegistryDependencies(self.root),
            )
            return []
        cases.append(("registry drift", registry_drift))

        def evidence_drift():
            generations = self.root / "workspaces" / "state" / "workspace-a" / "documents" / "generations"
            manifest = next(generations.iterdir()) / "manifest.json"
            value = json.loads(manifest.read_text(encoding="utf-8"))
            value["documents"][0]["document_ir"]["blocks"][0]["text"] = "mutated"
            manifest.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
            return [manifest]
        cases.append(("evidence drift", evidence_drift))

        def write_set_drift():
            value = json.loads(proposal_path.read_text(encoding="utf-8"))
            value["allowed_write_set"] = value["allowed_write_set"][:-1]
            value["allowed_write_set_digest"] = canonical_digest(
                {"domain": "ao.lore.evidence-selection.write-set.v0.1", "paths": value["allowed_write_set"]}
            )
            value["proposal_digest"] = canonical_digest(
                {key: item for key, item in value.items() if key != "proposal_digest"}
            )
            proposal_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
            return [proposal_path]
        cases.append(("actual write-set drift", write_set_drift))

        for label, mutate in cases:
            with self.subTest(label=label):
                temp = tempfile.TemporaryDirectory()
                root = Path(temp.name)
                snapshot = fixture_snapshot(root)
                from ao_lore.evidence_selection import (
                    EvidenceSelectionDependencies,
                    authorize_evidence_selection,
                    prepare_evidence_selection,
                )
                deps = EvidenceSelectionDependencies(
                    runtime_root=root,
                    candidate_root=self.candidate_root,
                    source_head=lambda: ACTUAL_HEAD,
                    now=lambda: "2026-08-14T12:00:00Z",
                )
                proposal = prepare_evidence_selection(
                    snapshot,
                    "workspace-a",
                    (
                        document_block_from_snapshot(snapshot)["evidence_id"],
                        graph_claim_from_snapshot(snapshot)["evidence_id"],
                    ),
                    now="2026-08-14T12:00:00Z",
                )
                authorization = authorize_evidence_selection(
                    deps, proposal, "fixture-operator"
                )
                selection_root = root / "workspaces" / "state" / "workspace-a" / "selection"
                proposal_path = selection_root / "proposals" / f"{proposal['proposal_id']}.json"
                auth_path = selection_root / "authorizations" / f"{authorization['authorization_id']}.json"
                auth_index = root / ".selection-index" / "authorizations" / f"{authorization['authorization_id']}.json"
                mutate = locals()["mutate"] if False else mutate
                mutate.__closure__
                # rebind paths in closures by name where needed
                if label == "workspace mismatch":
                    auth_index.write_text(json.dumps({"workspace_id": "workspace-b"}) + "\n", encoding="utf-8")
                elif label == "proposal/auth digest mismatch":
                    value = json.loads(auth_path.read_text(encoding="utf-8"))
                    value["proposal_digest"] = "sha256:" + "f" * 64
                    value["authorization_digest"] = canonical_digest({key: item for key, item in value.items() if key != "authorization_digest"})
                    auth_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
                elif label == "stored source_head drift":
                    value = json.loads(auth_path.read_text(encoding="utf-8"))
                    value["source_head"] = "0" * 40
                    value["authorization_digest"] = canonical_digest({key: item for key, item in value.items() if key != "authorization_digest"})
                    auth_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
                elif label == "authorization expiry":
                    value = json.loads(auth_path.read_text(encoding="utf-8"))
                    value["expires_at"] = "2026-08-14T11:59:59Z"
                    value["authorization_digest"] = canonical_digest({key: item for key, item in value.items() if key != "authorization_digest"})
                    auth_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
                elif label == "registry drift":
                    publish_workspace_registry_generation(
                        (definition("workspace-a", None, graph_variant("graph-workspace-a"), workspace_version=99),),
                        WorkspaceRegistryDependencies(root),
                    )
                elif label == "evidence drift":
                    generations = root / "workspaces" / "state" / "workspace-a" / "documents" / "generations"
                    manifest = next(generations.iterdir()) / "manifest.json"
                    value = json.loads(manifest.read_text(encoding="utf-8"))
                    value["documents"][0]["document_ir"]["blocks"][0]["text"] = "mutated"
                    manifest.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
                elif label == "actual write-set drift":
                    value = json.loads(proposal_path.read_text(encoding="utf-8"))
                    value["allowed_write_set"] = value["allowed_write_set"][:-1]
                    value["allowed_write_set_digest"] = canonical_digest({"domain": "ao.lore.evidence-selection.write-set.v0.1", "paths": value["allowed_write_set"]})
                    value["proposal_digest"] = canonical_digest({key: item for key, item in value.items() if key != "proposal_digest"})
                    proposal_path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
                with self.assertRaises((EvidenceSelectionError, ContractError, WorkspaceDocumentError)):
                    apply_evidence_selection(
                        deps,
                        proposal["proposal_id"],
                        authorization["authorization_id"],
                    )
                temp.cleanup()

    def test_recovery_ambiguity_branches(self):
        from ao_lore.evidence_selection import (
            EvidenceSelectionDependencies,
            persist_prepared_selection,
            prepare_evidence_selection,
            recover_evidence_selections,
        )

        second = prepare_evidence_selection(
            self.snapshot,
            "workspace-a",
            (graph_claim_from_snapshot(self.snapshot)["evidence_id"],),
            now="2026-08-14T12:00:01Z",
        )
        persist_prepared_selection(self.dependencies, second)
        result = recover_evidence_selections(self.dependencies, "workspace-a")
        self.assertEqual("investigate", result["status"])


if __name__ == "__main__":
    unittest.main()
