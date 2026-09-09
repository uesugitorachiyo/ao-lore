import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest.mock import patch

from ao_lore.benchmark import canonical_digest


REPOSITORY = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = REPOSITORY / "tests" / "fixtures" / "ao_lore" / "sanitized_lifecycle"
GENERATOR = FIXTURE_ROOT / "generate.py"
SPEC = FIXTURE_ROOT / "fixture-spec.json"
OWNER_NAME = ".ao-lore-sanitized-lifecycle-owner.json"
OWNER_BYTES = b'{"owner":"ao-lore-sanitized-lifecycle-fixture","schema_version":"v0.1"}\n'
DOCUMENT_IDS = (
    "policy-emergency-lighting",
    "policy-expense-receipts",
    "policy-remote-schedules",
)
EXPECTED_FILES = {
    "document-ir/policy-emergency-lighting.json",
    "document-ir/policy-expense-receipts.json",
    "document-ir/policy-remote-schedules.json",
    "fixture-bundle.json",
    "sources/policy-emergency-lighting.txt",
    "sources/policy-expense-receipts.txt",
    "sources/policy-remote-schedules.txt",
}


def canonical_bytes(value):
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
        + b"\n"
    )


class SanitizedLifecycleFixtureTests(unittest.TestCase):
    def run_generator(self, *arguments):
        return subprocess.run(
            [sys.executable, str(GENERATOR), *map(str, arguments)],
            cwd=REPOSITORY,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def load_generator(self):
        module_spec = spec_from_file_location("sanitized_lifecycle_fixture_generator", GENERATOR)
        self.assertIsNotNone(module_spec)
        self.assertIsNotNone(module_spec.loader)
        module = module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        return module

    def owned_parent(self, base):
        parent = Path(base) / "owned"
        parent.mkdir(parents=True)
        (parent / OWNER_NAME).write_bytes(OWNER_BYTES)
        return parent

    def inventory(self, root):
        return {
            path.relative_to(root).as_posix(): (
                path.read_bytes(),
                path.lstat().st_mode,
                path.lstat().st_mtime_ns,
            )
            for path in sorted(root.rglob("*"))
            if path.is_file() and not path.is_symlink()
        }

    def test_fixture_spec_is_canonical_bounded_and_declares_exact_public_corpus(self):
        self.assertTrue(SPEC.is_file(), "fixture spec is absent")
        value = json.loads(SPEC.read_text(encoding="utf-8"))
        self.assertEqual(SPEC.read_bytes(), canonical_bytes(value))
        self.assertEqual("ao.lore.sanitized-lifecycle-fixture-spec.v0.1", value["schema_version"])
        self.assertEqual(list(DOCUMENT_IDS), [item["document_id"] for item in value["documents"]])
        self.assertEqual(3, len(value["documents"]))
        texts = [item["text"] for item in value["documents"]]
        self.assertEqual(3, len(set(texts)))
        self.assertTrue(all(20 <= len(text) <= 160 and text.isascii() for text in texts))
        self.assertTrue(all(left not in right for left in texts for right in texts if left != right))
        self.assertEqual("policy-emergency-lighting", value["unrelated_document_id"])
        self.assertEqual(
            ["policy-expense-receipts", "policy-remote-schedules"],
            value["selected_document_ids"],
        )
        self.assertNotIn(value["unrelated_document_id"], value["selected_document_ids"])
        self.assertEqual(2, len(value["relationships"]))

    def test_direct_and_check_are_byte_identical_and_check_is_non_mutating(self):
        self.assertTrue(GENERATOR.is_file(), "fixture generator is absent")
        with tempfile.TemporaryDirectory() as temporary:
            root = self.owned_parent(temporary) / "fixture"
            direct = self.run_generator("--out", root)
            self.assertEqual(0, direct.returncode, direct.stderr.decode())
            self.assertEqual(b"", direct.stderr)
            before = self.inventory(root)
            check = self.run_generator("--check", "--out", root)
            self.assertEqual(0, check.returncode, check.stderr.decode())
            self.assertEqual(b"", check.stderr)
            self.assertEqual(before, self.inventory(root))
            self.assertEqual(EXPECTED_FILES, set(before))

            second_root = self.owned_parent(Path(temporary) / "second") / "fixture"
            regenerated = self.run_generator("--out", second_root)
            self.assertEqual(0, regenerated.returncode, regenerated.stderr.decode())
            self.assertEqual(
                {name: fields[0] for name, fields in before.items()},
                {name: fields[0] for name, fields in self.inventory(second_root).items()},
            )

    def test_sources_document_ir_evidence_and_citations_are_exactly_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = self.owned_parent(temporary) / "fixture"
            result = self.run_generator("--out", root)
            self.assertEqual(0, result.returncode, result.stderr.decode())
            bundle = json.loads((root / "fixture-bundle.json").read_text(encoding="utf-8"))
            self.assertEqual("ao.lore.sanitized-lifecycle-fixture.v0.1", bundle["schema_version"])
            self.assertEqual(3, len(bundle["documents"]))
            self.assertGreaterEqual(len(bundle["evidence_identities"]), 3)
            self.assertEqual(3, len(bundle["evidence_identities"]))
            self.assertIn("claims", bundle)
            self.assertEqual(3, len(bundle["claims"]))

            documents = {item["document_id"]: item for item in bundle["documents"]}
            evidence = {item["document_id"]: item for item in bundle["evidence_identities"]}
            claims = {item["document_id"]: item for item in bundle["claims"]}
            self.assertEqual(set(DOCUMENT_IDS), set(documents))
            self.assertEqual(set(DOCUMENT_IDS), set(evidence))
            self.assertEqual(set(DOCUMENT_IDS), set(claims))
            self.assertEqual(3, len({item["evidence_id"] for item in evidence.values()}))
            for document_id in DOCUMENT_IDS:
                source = (root / documents[document_id]["source_file"]).read_bytes()
                ir_path = root / documents[document_id]["document_ir_file"]
                ir = json.loads(ir_path.read_text(encoding="utf-8"))
                block = ir["blocks"][0]
                identity = evidence[document_id]
                source_digest = "sha256:" + hashlib.sha256(source).hexdigest()
                self.assertEqual(source.decode("ascii"), block["text"])
                self.assertEqual({"start": 0, "end": len(source)}, block["source_span"])
                self.assertEqual(document_id, ir["document_id"])
                self.assertEqual(source_digest, ir["source"]["digest"])
                self.assertEqual(source_digest, identity["source_digest"])
                self.assertEqual(canonical_digest(ir), identity["document_ir_digest"])
                self.assertEqual(canonical_digest(block), identity["block_digest"])
                self.assertEqual(block["id"], identity["block_id"])
                self.assertEqual(block["text"], identity["render_text"])
                self.assertEqual(block["source_span"], identity["source_span"])
                citation = identity["citation"]
                self.assertEqual(document_id, citation["document_id"])
                self.assertEqual(identity["evidence_id"], citation["evidence_id"])
                self.assertEqual(identity["block_digest"], citation["block_digest"])
                self.assertEqual(identity["render_text"], citation["render_text"])
                self.assertEqual(identity["source_span"], citation["source_span"])
                claim = claims[document_id]
                self.assertEqual(identity["source_id"], claim["source_id"])
                self.assertEqual(identity["source_digest"], claim["source_digest"])
                self.assertEqual(identity["block_id"], claim["citation_anchor"])
                self.assertEqual(identity["render_text"], claim["excerpt"])
                self.assertEqual(canonical_digest({
                    "domain": "ao.lore.evidence-graph.excerpt.v0.1",
                    "value": claim["excerpt"],
                }), claim["excerpt_digest"])
                self.assertEqual(
                    canonical_digest({key: value for key, value in citation.items() if key != "citation_digest"}),
                    citation["citation_digest"],
                )

            self.assertEqual(2, len(bundle["relationships"]))
            for relationship in bundle["relationships"]:
                supporting = next(
                    item for item in evidence.values()
                    if item["evidence_id"] == relationship["supporting_evidence_id"]
                )
                self.assertEqual(supporting["render_text"], relationship["supporting_excerpt"])
                claim = next(item for item in claims.values()
                             if item["claim_id"] == relationship["source_claim_id"])
                self.assertEqual(supporting["document_id"], claim["document_id"])
                self.assertEqual(claim["excerpt_digest"], relationship["supporting_excerpt_digest"])
                self.assertEqual(supporting["citation"]["citation_digest"], relationship["citation_digest"])
                self.assertEqual(
                    canonical_digest({key: value for key, value in relationship.items() if key != "relationship_digest"}),
                    relationship["relationship_digest"],
                )
            self.assertEqual(bundle["selected_document_ids"], [
                "policy-expense-receipts", "policy-remote-schedules",
            ])
            self.assertEqual("policy-emergency-lighting", bundle["unrelated_document_id"])

    def test_unowned_prepopulated_escaping_and_linked_roots_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            unowned = base / "unowned" / "fixture"
            unowned.parent.mkdir()
            unowned_result = self.run_generator("--out", unowned)
            self.assertEqual(2, unowned_result.returncode)
            self.assertEqual(b"sanitized lifecycle fixture rejected\n", unowned_result.stderr)
            self.assertNotIn(os.fsencode(temporary), unowned_result.stderr)

            owned = self.owned_parent(base / "prepopulated")
            prepopulated = owned / "fixture"
            prepopulated.mkdir()
            (prepopulated / "foreign.txt").write_text("foreign", encoding="ascii")
            prepopulated_result = self.run_generator("--out", prepopulated)
            self.assertEqual(2, prepopulated_result.returncode)
            self.assertEqual(b"sanitized lifecycle fixture rejected\n", prepopulated_result.stderr)
            self.assertEqual(b"foreign", (prepopulated / "foreign.txt").read_bytes())

            escaping_parent = self.owned_parent(base / "escaping")
            escaping = escaping_parent / ".." / "outside"
            escaping_result = self.run_generator("--out", escaping)
            self.assertEqual(2, escaping_result.returncode)
            self.assertEqual(b"sanitized lifecycle fixture rejected\n", escaping_result.stderr)
            self.assertFalse((base / "escaping" / "outside").exists())

            real_parent = self.owned_parent(base / "linked-parent")
            linked_parent = base / "parent-link"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            linked_result = self.run_generator("--out", linked_parent / "fixture")
            self.assertEqual(2, linked_result.returncode)
            self.assertEqual(b"sanitized lifecycle fixture rejected\n", linked_result.stderr)

            hardlinked_owner_parent = base / "hardlinked-owner"
            hardlinked_owner_parent.mkdir()
            os.link(real_parent / OWNER_NAME, hardlinked_owner_parent / OWNER_NAME)
            hardlinked_result = self.run_generator("--out", hardlinked_owner_parent / "fixture")
            self.assertEqual(2, hardlinked_result.returncode)
            self.assertEqual(b"sanitized lifecycle fixture rejected\n", hardlinked_result.stderr)

    def test_check_rejects_drift_extra_links_and_replaced_root_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            parent = self.owned_parent(base)
            root = parent / "fixture"
            self.assertEqual(0, self.run_generator("--out", root).returncode)
            original = self.inventory(root)

            source = root / "sources" / "policy-expense-receipts.txt"
            source.write_bytes(source.read_bytes() + b" drift")
            drift = self.run_generator("--check", "--out", root)
            self.assertEqual(1, drift.returncode)
            self.assertEqual(b"", drift.stderr)
            self.assertEqual(source.read_bytes(), self.inventory(root)[source.relative_to(root).as_posix()][0])
            source.write_bytes(original[source.relative_to(root).as_posix()][0])

            extra = root / "foreign.txt"
            extra.write_bytes(b"foreign")
            extra_drift = self.run_generator("--check", "--out", root)
            self.assertEqual(1, extra_drift.returncode)
            self.assertEqual(b"", extra_drift.stderr)
            extra.unlink()

            linked = root / "linked-source.txt"
            os.link(source, linked)
            linked_result = self.run_generator("--check", "--out", root)
            self.assertEqual(2, linked_result.returncode)
            self.assertEqual(b"sanitized lifecycle fixture rejected\n", linked_result.stderr)
            linked.unlink()

            replaced = parent / "replaced"
            root.rename(replaced)
            root.symlink_to(replaced, target_is_directory=True)
            replaced_result = self.run_generator("--check", "--out", root)
            self.assertEqual(2, replaced_result.returncode)
            self.assertEqual(b"sanitized lifecycle fixture rejected\n", replaced_result.stderr)
            self.assertNotIn(os.fsencode(temporary), replaced_result.stderr)

    def test_content_mismatch_does_not_bypass_child_rebound_verification(self):
        module = self.load_generator()
        with tempfile.TemporaryDirectory() as temporary:
            parent_path = self.owned_parent(temporary)
            root = parent_path / "fixture"
            direct = self.run_generator("--out", root)
            self.assertEqual(0, direct.returncode, direct.stderr.decode())
            expected = module._documents()
            parent, parent_held, root_name = module._open_owned_parent(root)
            original_read = module._read_regular
            replaced = False

            def mismatch_then_replace(descriptor, name, maximum):
                nonlocal replaced
                body, held = original_read(descriptor, name, maximum)
                if not replaced:
                    (root / "sources").rename(root / "sources-replaced")
                    (root / "sources").mkdir()
                    replaced = True
                    return body + b" drift", held
                return body, held

            try:
                with patch.object(module, "_read_regular", side_effect=mismatch_then_replace):
                    with self.assertRaisesRegex(ValueError, "directory rebound"):
                        module._check_fixture(parent, parent_held, root_name, expected)
            finally:
                os.close(parent)
            self.assertTrue(replaced)

    def test_cli_rejects_every_unrequired_argument(self):
        sentinel = "/private/sentinel-company-policy.txt"
        for arguments in (
            ("--write", "--out", sentinel),
            ("--help",),
            ("--out", sentinel, "unexpected-private-value"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_generator(*arguments)
                self.assertEqual(2, result.returncode)
                self.assertEqual(b"sanitized lifecycle fixture rejected\n", result.stderr)
                self.assertNotIn(sentinel.encode("ascii"), result.stderr)
                self.assertNotIn(b"unexpected-private-value", result.stderr)


if __name__ == "__main__":
    unittest.main()
