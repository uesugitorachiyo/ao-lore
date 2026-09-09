import hashlib
import importlib.util
import inspect
import json
import os
import tempfile
import unittest
from pathlib import Path

from ao_lore import ocr_fixture_oracle
from ao_lore._strict_io import ContractError
from ao_lore.ocr_fixture_oracle import load_ocr_fixture_corpus
from ao_lore.ocr_raster import OcrRasterRejected, render_in_ocr_sandbox


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "ao_lore" / "ocr"
SPEC = FIXTURE_ROOT / "fixture-spec.json"


def generator_module():
    path = FIXTURE_ROOT / "generate.py"
    spec = importlib.util.spec_from_file_location("ao_lore_ocr_fixture_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class OcrFixtureTests(unittest.TestCase):
    EXPECTED_KINDS = {
        "clean", "degraded", "faint", "uneven", "rotated-90", "rotated-180",
        "rotated-270", "skewed", "one-column", "two-column", "dense",
        "punctuation", "blank", "nontext", "mixed-pdf", "malformed-pdf",
        "encrypted-pdf", "oversized", "decompression-bomb", "excessive-pages",
    }

    def test_generator_is_deterministic_and_binds_exact_public_bytes(self):
        generator = generator_module()
        corpus = json.loads(SPEC.read_text(encoding="utf-8"))
        generator.validate_corpus(corpus)
        self.assertEqual({item["kind"] for item in corpus["fixtures"]}, self.EXPECTED_KINDS)
        for fixture in corpus["fixtures"]:
            body = generator.generate_fixture_bytes(fixture)
            self.assertEqual(body, generator.generate_fixture_bytes(fixture))
            self.assertEqual(body, (FIXTURE_ROOT / fixture["file"]).read_bytes())
            self.assertEqual("sha256:" + hashlib.sha256(body).hexdigest(), fixture["sha256"])
            serialized = json.dumps(fixture, sort_keys=True).lower()
            self.assertNotIn("/home/", serialized)
            self.assertNotIn("private", serialized)
        by_kind = {item["kind"]: (FIXTURE_ROOT / item["file"]).read_bytes() for item in corpus["fixtures"]}
        self.assertIn(b"/Subtype /Image", by_kind["mixed-pdf"])
        self.assertIn(b"/BaseFont /Courier", by_kind["mixed-pdf"])
        self.assertIn(b"/Count 101", by_kind["excessive-pages"])
        self.assertEqual(int.from_bytes(by_kind["oversized"][16:20], "big"), 25000001)
        self.assertEqual(int.from_bytes(by_kind["decompression-bomb"][16:20], "big"), 25000000)

    def test_check_mode_is_nonmutating_and_rejects_drift(self):
        generator = generator_module()
        generator.generate(check=True)
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "fixture-spec.json").write_bytes(SPEC.read_bytes())
            for fixture in json.loads(SPEC.read_text(encoding="utf-8"))["fixtures"]:
                (root / fixture["file"]).write_bytes((FIXTURE_ROOT / fixture["file"]).read_bytes())
            first = json.loads((root / "fixture-spec.json").read_text(encoding="utf-8"))["fixtures"][0]
            (root / first["file"]).write_bytes(b"drift")
            with self.assertRaises(ValueError):
                generator.generate(check=True, root=root)

    def test_loader_returns_closed_annotations_and_rejects_digest_or_shape_drift(self):
        corpus = load_ocr_fixture_corpus(SPEC)
        self.assertEqual(len(corpus.fixtures), 20)
        self.assertEqual(corpus.corpus_digest, load_ocr_fixture_corpus(SPEC).corpus_digest)
        self.assertTrue(any(item.expected_outcome == "accepted" for item in corpus.fixtures))
        self.assertTrue(any(item.expected_outcome == "rejected" for item in corpus.fixtures))
        changed = json.loads(SPEC.read_text(encoding="utf-8"))
        changed["fixtures"][0]["sha256"] = "sha256:" + "0" * 64
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            path = root / "fixture-spec.json"
            path.write_text(json.dumps(changed), encoding="utf-8")
            for fixture in changed["fixtures"]:
                (root / fixture["file"]).write_bytes((FIXTURE_ROOT / fixture["file"]).read_bytes())
            with self.assertRaises(ValueError):
                load_ocr_fixture_corpus(path)

    def test_loader_rejects_symlink_and_hardlink_inputs(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            linked_spec = root / "linked-spec.json"
            linked_spec.symlink_to(SPEC)
            with self.assertRaises(ValueError):
                load_ocr_fixture_corpus(linked_spec)
        corpus = json.loads(SPEC.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "fixture-spec.json").write_bytes(SPEC.read_bytes())
            for fixture in corpus["fixtures"]:
                (root / fixture["file"]).write_bytes((FIXTURE_ROOT / fixture["file"]).read_bytes())
            first = root / corpus["fixtures"][0]["file"]
            external = root / "external-copy"
            first.rename(external)
            os.link(external, first)
            with self.assertRaises(ValueError):
                load_ocr_fixture_corpus(root / "fixture-spec.json")

    def test_oracle_source_is_independent_of_worker_and_canonicalizer(self):
        source = inspect.getsource(ocr_fixture_oracle)
        self.assertNotIn("paddle_ocr", source)
        self.assertNotIn("ocr_ir", source)
        self.assertNotIn("build_document_ir", source)
        self.assertNotIn("build_ocr_parse_output", source)

    def test_actual_raster_sandbox_accepts_mixed_pages_and_rejects_limit_matrix(self):
        runtime = ROOT / ".ao-lore" / "ocr-runtime" / "ocr-runtime-v0.1"
        if not runtime.is_dir():
            self.skipTest("sealed OCR runtime is not prepared")
        mixed = render_in_ocr_sandbox(
            (FIXTURE_ROOT / "mixed.pdf").read_bytes(), "application/pdf", {"dpi": 300}, runtime_root=runtime,
        )
        self.assertEqual([(page.width, page.height) for page in mixed], [(600, 300), (600, 300)])
        rotated = render_in_ocr_sandbox(
            (FIXTURE_ROOT / "rotated-90.png").read_bytes(), "image/png", {"dpi": 300}, runtime_root=runtime,
        )
        self.assertEqual([(page.width, page.height) for page in rotated], [(300, 600)])
        cases = {
            "malformed.pdf": ("application/pdf", "invalid_source"),
            "encrypted.pdf": ("application/pdf", "encrypted_source"),
            "excessive-pages.pdf": ("application/pdf", "resource_limit"),
            "oversized.png": ("image/png", "resource_limit"),
            "decompression-bomb.png": ("image/png", "resource_limit"),
        }
        for name, (media_type, category) in cases.items():
            with self.subTest(name=name), self.assertRaises(OcrRasterRejected) as caught:
                render_in_ocr_sandbox(
                    (FIXTURE_ROOT / name).read_bytes(), media_type, {"dpi": 300}, runtime_root=runtime,
                )
            self.assertEqual(caught.exception.category, category)


if __name__ == "__main__":
    unittest.main()
