import inspect
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from ao_lore.benchmark import canonical_digest
from ao_lore.knowledge_contracts import citations_digest, claims_digest, semantic_key_v0_3


DIGEST = "sha256:" + "a" * 64


def _entry(entry_id, version="v0.2"):
    value = {
        "schema_version": f"ao.lore.okf-canonical-entry.{version}",
        "policy_version": "ao.lore.promotion-policy.v0.1",
        "canonical_entry_id": entry_id,
        "semantic_key": canonical_digest({"domain": "fixture-semantic", "entry_id": entry_id}),
        "candidate_id": f"candidate-{entry_id}",
        "candidate_digest": DIGEST,
        "document_ir_digest": DIGEST,
        "source_digest": DIGEST,
        "provenance_digest": DIGEST,
        "parser": {"parser_id": "fixture-parser", "parser_version": "1.0"},
        "accepted_review_head_digest": DIGEST,
        "concepts": [],
        "claim_mappings": [],
        "links": [],
    }
    if version == "v0.3":
        value.update({
            "claims_schema_version": "ao.lore.canonical-claim-set.v0.1",
            "claims_digest": canonical_digest({"domain": "ao.lore.canonical-claim-set.v0.1", "claims": []}),
            "claims": [],
            "citations_schema_version": "ao.lore.canonical-citation-set.v0.1",
            "citations_digest": canonical_digest({"domain": "ao.lore.canonical-citation-set.v0.1", "citations": []}),
            "citations": [],
            "knowledge_policy": {"sensitivity": "public", "stale_after": None},
        })
        value["semantic_key"] = canonical_digest({
            "domain": "ao.lore.semantic-key.v0.3", "concepts": [], "claim_mappings": [], "links": [],
            "claims_digest": value["claims_digest"], "citations_digest": value["citations_digest"],
            "knowledge_policy": value["knowledge_policy"],
        })
    return value


def _origin(block_id="block-one", evidence_id="origin-one"):
    return {
        "workspace_id": "workspace-one", "document_store_id": "store-one",
        "generation_digest": DIGEST, "document_id": "document-one",
        "document_ir_digest": DIGEST, "source_id": "source-one",
        "source_digest": DIGEST, "evidence_kind": "document_block",
        "evidence_id": evidence_id, "evidence_digest": DIGEST,
        "block_id": block_id, "render_text": "Canonical policy is enabled.",
        "source_span": {"page": 1, "start": 0, "end": 28},
        "authority_role": "technical_guidance", "sensitivity": "public",
        "freshness_status": "current", "qualification_codes": [],
    }


def _entry_v0_4(entry_id="entry-four", text="Canonical policy is enabled."):
    claim = {"claim_id": f"claim-{entry_id}", "text": text, "source_block_ids": ["block-one"], "citation_id": f"citation-{entry_id}"}
    citation = {"citation_id": f"citation-{entry_id}", "render_text": "Reviewed promoted origin.", "source_block_ids": ["block-one"]}
    value = _entry(entry_id, "v0.3")
    for field in ("document_ir_digest", "source_digest", "parser"):
        value.pop(field)
    value.update(
        schema_version="ao.lore.okf-canonical-entry.v0.4",
        evidence_origins=[_origin()],
        claims=[claim], claims_digest=claims_digest([claim]),
        citations=[citation], citations_digest=citations_digest([citation]),
        claim_mappings=[{"claim_id": claim["claim_id"], "block_ids": ["block-one"]}],
    )
    value["evidence_selection_digest"] = canonical_digest({"domain": "ao.lore.evidence-selection.v0.1", "evidence": value["evidence_origins"]})
    value["semantic_key"] = semantic_key_v0_3(value["concepts"], value["claim_mappings"], value["links"], value["claims_digest"], value["citations_digest"], value["knowledge_policy"])
    return value


class GenerationFixture:
    def __init__(self, root):
        self.root = Path(root)
        self.brain = self.root / "brain"
        self.generations = self.brain / "generations"
        self.generations.mkdir(parents=True)
        self.records = []

    def add(self, generation_id, entry, *, prior=None):
        sequence = len(self.records) + 1
        prior = self.records[-1] if prior is None and self.records else prior
        entry_digest = canonical_digest(entry)
        logical = canonical_digest({
            "domain": "ao.lore.logical-brain-state.v0.1",
            "prior_generation_digest": None if prior is None else prior["manifest_digest"],
            "operation": "apply", "added_entry_digests": [entry_digest],
        })
        transition = {"kind": "add", "canonical_entry_id": entry["canonical_entry_id"], "canonical_entry_digest": entry_digest}
        return self._write(sequence, generation_id, "apply", prior, transition, logical, entry)

    def restore(self, generation_id, target, *, prior=None):
        sequence = len(self.records) + 1
        prior = self.records[-1] if prior is None and self.records else prior
        logical = canonical_digest({
            "domain": "ao.lore.logical-brain-state.v0.1",
            "restored_generation_id": None if target is None else target["generation_id"],
            "restored_manifest_digest": None if target is None else target["manifest_digest"],
        })
        transition = {
            "kind": "restore", "restored_generation_id": None if target is None else target["generation_id"],
            "restored_manifest_digest": None if target is None else target["manifest_digest"],
        }
        return self._write(sequence, generation_id, "rollback", prior, transition, logical, None)

    def _write(self, sequence, generation_id, operation, prior, transition, logical, entry):
        directory = self.generations / f"{sequence:06d}-{generation_id}"
        entries = directory / "entries"
        entries.mkdir(parents=True)
        if entry is not None:
            (entries / f"{entry['canonical_entry_id']}.json").write_text(json.dumps(entry), encoding="utf-8")
        manifest = {
            "schema_version": "ao.lore.brain-generation-manifest.v0.1", "policy_version": "ao.lore.promotion-policy.v0.1",
            "sequence": sequence, "generation_id": generation_id, "operation": operation, "promotion_id": f"promotion-{sequence}",
            "prior_generation": None if prior is None else {"generation_id": prior["generation_id"], "manifest_digest": prior["manifest_digest"]},
            "prior_brain_inventory_digest": DIGEST, "transition": transition, "logical_state_digest": logical,
            "proposal_digest": DIGEST, "authorization_digest": DIGEST, "transaction_intent_digest": DIGEST,
            "created_at": "2026-08-12T00:00:00Z",
        }
        manifest["manifest_digest"] = canonical_digest(manifest)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.records.append(manifest)
        return manifest


class KnowledgeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = GenerationFixture(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def read(self):
        from ao_lore.knowledge import _KnowledgeDependencies, read_knowledge_snapshot
        return read_knowledge_snapshot(_dependencies=_KnowledgeDependencies(self.fixture.brain, None))

    def test_empty_root_returns_stable_detached_snapshot_without_mutation(self):
        before = sorted(str(path.relative_to(self.fixture.root)) for path in self.fixture.root.rglob("*"))
        snapshot = self.read()
        after = sorted(str(path.relative_to(self.fixture.root)) for path in self.fixture.root.rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual([], snapshot["effective_entries"])
        self.assertIsNone(snapshot["latest_generation_id"])
        self.assertEqual(0, snapshot["legacy_v0_2_entry_count"])
        self.assertNotIn("brain_root", inspect.signature(__import__("ao_lore.knowledge", fromlist=["read_knowledge_snapshot"]).read_knowledge_snapshot).parameters)

    def test_replays_mixed_add_restore_and_rollback_of_rollback(self):
        first = self.fixture.add("generation-one", _entry("entry-one"))
        second = self.fixture.add("generation-two", _entry("entry-two", "v0.3"))
        third = self.fixture.restore("generation-three", first)
        self.fixture.restore("generation-four", second)
        snapshot = self.read()
        self.assertEqual("generation-four", snapshot["latest_generation_id"])
        self.assertEqual(["entry-one", "entry-two"], [item["canonical_entry_id"] for item in snapshot["effective_entries"]])
        self.assertEqual(1, snapshot["legacy_v0_2_entry_count"])
        self.assertEqual(1, snapshot["answerable_v0_3_entry_count"])
        self.assertEqual("answerable", snapshot["effective_entries"][1]["answerability"])

    def test_v0_4_snapshot_uses_v0_2_contract_and_preserves_origin_identity(self):
        self.fixture.add("generation-one", _entry_v0_4())
        snapshot = self.read()
        self.assertEqual("ao.lore.knowledge-snapshot.v0.2", snapshot["snapshot_schema_version"])
        self.assertEqual(1, snapshot["answerable_v0_4_entry_count"])
        self.assertEqual([{
            "workspace_id": "workspace-one", "evidence_kind": "document_block",
            "evidence_id": "origin-one", "evidence_digest": DIGEST,
        }], snapshot["effective_entries"][0]["evidence_origin_identities"])

    def test_restore_to_genesis_and_nested_restore_are_exact(self):
        first = self.fixture.add("generation-one", _entry("entry-one"))
        genesis = self.fixture.restore("generation-two", None)
        self.fixture.restore("generation-three", genesis)
        self.assertEqual([], self.read()["effective_entries"])

    def test_rejects_manifest_self_prior_entry_and_logical_drift(self):
        cases = ("self", "prior", "entry", "logical")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                fixture = GenerationFixture(root)
                fixture.add("generation-one", _entry("entry-one"))
                fixture.add("generation-two", _entry("entry-two"))
                if case == "entry":
                    path = fixture.generations / "000002-generation-two" / "entries" / "entry-two.json"
                    value = json.loads(path.read_text()); value["candidate_id"] = "candidate-drift"; path.write_text(json.dumps(value))
                else:
                    path = fixture.generations / "000002-generation-two" / "manifest.json"
                    value = json.loads(path.read_text())
                    if case == "self": value["manifest_digest"] = DIGEST
                    if case == "prior": value["prior_generation"]["manifest_digest"] = DIGEST
                    if case == "logical": value["logical_state_digest"] = DIGEST
                    if case != "self": value["manifest_digest"] = canonical_digest({k: v for k, v in value.items() if k != "manifest_digest"})
                    path.write_text(json.dumps(value))
                from ao_lore.knowledge import KnowledgeReadError, _KnowledgeDependencies, read_knowledge_snapshot
                with self.assertRaises(KnowledgeReadError):
                    read_knowledge_snapshot(_dependencies=_KnowledgeDependencies(fixture.brain, None))

    def test_rejects_names_gaps_aliases_collisions_and_foreign_files(self):
        bad_names = ("000002-generation-one", "000001-GENERATION", "000001-generation-one ")
        for bad_name in bad_names:
            with self.subTest(name=bad_name), tempfile.TemporaryDirectory() as root:
                fixture = GenerationFixture(root); fixture.add("generation-one", _entry("entry-one"))
                original = next(fixture.generations.iterdir()); original.rename(fixture.generations / bad_name)
                from ao_lore.knowledge import KnowledgeReadError, _KnowledgeDependencies, read_knowledge_snapshot
                with self.assertRaises(KnowledgeReadError): read_knowledge_snapshot(_dependencies=_KnowledgeDependencies(fixture.brain, None))
        self.fixture.add("generation-one", _entry("entry-one"))
        (self.fixture.generations / "foreign.txt").write_text("x")
        with self.assertRaises(Exception): self.read()

    def test_rejects_symlink_hardlink_special_duplicate_json_and_future_version(self):
        mutators = ("symlink", "hardlink", "special", "duplicate", "future")
        for mutation in mutators:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                fixture = GenerationFixture(root); fixture.add("generation-one", _entry("entry-one"))
                generation = fixture.generations / "000001-generation-one"
                entry_path = generation / "entries" / "entry-one.json"
                if mutation == "symlink":
                    entry_path.unlink(); entry_path.symlink_to(generation / "manifest.json")
                elif mutation == "hardlink": os.link(entry_path, generation / "entries" / "entry-copy.json")
                elif mutation == "special": os.mkfifo(generation / "entries" / "pipe")
                elif mutation == "duplicate": entry_path.write_text('{"schema_version":"x","schema_version":"y"}')
                else:
                    value = json.loads(entry_path.read_text()); value["schema_version"] = "ao.lore.okf-canonical-entry.v9.9"; entry_path.write_text(json.dumps(value))
                from ao_lore.knowledge import KnowledgeReadError, _KnowledgeDependencies, read_knowledge_snapshot
                with self.assertRaises(KnowledgeReadError): read_knowledge_snapshot(_dependencies=_KnowledgeDependencies(fixture.brain, None))

    def test_rejects_missing_restore_forward_restore_and_duplicate_active_entry(self):
        self.fixture.add("generation-one", _entry("entry-one"))
        self.fixture.add("generation-two", _entry("entry-one"))
        with self.assertRaises(Exception): self.read()

        for mode in ("missing", "forward"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                fixture = GenerationFixture(root)
                first = fixture.add("generation-one", _entry("entry-one"))
                second = fixture.restore("generation-two", first)
                third = fixture.add("generation-three", _entry("entry-three"))
                path = fixture.generations / "000002-generation-two" / "manifest.json"
                value = json.loads(path.read_text())
                target = {"generation_id": "generation-missing", "manifest_digest": DIGEST} if mode == "missing" else {
                    "generation_id": third["generation_id"], "manifest_digest": third["manifest_digest"]
                }
                value["transition"].update(restored_generation_id=target["generation_id"], restored_manifest_digest=target["manifest_digest"])
                value["logical_state_digest"] = canonical_digest({"domain": "ao.lore.logical-brain-state.v0.1", **value["transition"]})
                value["logical_state_digest"] = canonical_digest({
                    "domain": "ao.lore.logical-brain-state.v0.1", "restored_generation_id": target["generation_id"],
                    "restored_manifest_digest": target["manifest_digest"],
                })
                value["manifest_digest"] = canonical_digest({k: v for k, v in value.items() if k != "manifest_digest"})
                path.write_text(json.dumps(value))
                from ao_lore.knowledge import KnowledgeReadError, _KnowledgeDependencies, read_knowledge_snapshot
                with self.assertRaises(KnowledgeReadError): read_knowledge_snapshot(_dependencies=_KnowledgeDependencies(fixture.brain, None))

    def test_rejects_malformed_legacy_nested_contract_and_effective_budget(self):
        malformed = _entry("entry-one")
        malformed["concepts"] = [{"candidate_concept_id": "concept-one", "title": "Title", "source_block_ids": [], "status": "canonical"}]
        self.fixture.add("generation-one", malformed)
        with self.assertRaises(Exception): self.read()

        with tempfile.TemporaryDirectory() as root:
            fixture = GenerationFixture(root)
            fixture.add("generation-one", _entry("entry-one"))
            fixture.add("generation-two", _entry("entry-two"))
            from unittest import mock
            from ao_lore.knowledge import KnowledgeReadError, _KnowledgeDependencies, read_knowledge_snapshot
            with mock.patch("ao_lore.knowledge.MAX_EFFECTIVE_ENTRIES", 1), self.assertRaises(KnowledgeReadError):
                read_knowledge_snapshot(_dependencies=_KnowledgeDependencies(fixture.brain, None))

    def test_rejects_generation_directory_replacement_during_read(self):
        self.fixture.add("generation-one", _entry("entry-one"))
        original = self.fixture.generations / "000001-generation-one"

        def replace():
            moved = self.fixture.generations / "moved"
            original.rename(moved)
            shutil.copytree(moved, original)

        from ao_lore.knowledge import KnowledgeReadError, _KnowledgeDependencies, read_knowledge_snapshot
        dependencies = _KnowledgeDependencies(self.fixture.brain, None, replace)
        with self.assertRaises(KnowledgeReadError):
            read_knowledge_snapshot(_dependencies=dependencies)

    def test_manifest_nested_shapes_and_timestamp_fail_with_reader_error(self):
        for mutation in ("prior", "timestamp"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as root:
                fixture = GenerationFixture(root)
                fixture.add("generation-one", _entry("entry-one"))
                fixture.add("generation-two", _entry("entry-two"))
                path = fixture.generations / "000002-generation-two" / "manifest.json"
                value = json.loads(path.read_text())
                if mutation == "prior":
                    value["prior_generation"] = "generation-one"
                else:
                    value["created_at"] = "2026-99-99T00:00:00Z"
                value["manifest_digest"] = canonical_digest({k: v for k, v in value.items() if k != "manifest_digest"})
                path.write_text(json.dumps(value))
                from ao_lore.knowledge import KnowledgeReadError, _KnowledgeDependencies, read_knowledge_snapshot
                with self.assertRaises(KnowledgeReadError):
                    read_knowledge_snapshot(_dependencies=_KnowledgeDependencies(fixture.brain, None))

    def test_generation_and_replay_depth_budgets_fail_closed(self):
        self.fixture.add("generation-one", _entry("entry-one"))
        self.fixture.add("generation-two", _entry("entry-two"))
        from unittest import mock
        from ao_lore.knowledge import KnowledgeReadError
        with mock.patch("ao_lore.knowledge.MAX_GENERATIONS", 1), self.assertRaises(KnowledgeReadError):
            self.read()
        with mock.patch("ao_lore.knowledge.MAX_RESTORE_DEPTH", 1), self.assertRaises(KnowledgeReadError):
            self.read()


if __name__ == "__main__":
    unittest.main()
