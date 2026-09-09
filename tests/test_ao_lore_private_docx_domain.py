import hashlib
import importlib.util
import inspect
import json
import os
import struct
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from ao_lore import private_docx_domain as private_docx_domain_module
from ao_lore._strict_io import parse_strict_json
from ao_lore.docx_ooxml import DocxLimits, docx_configuration_digest
from ao_lore.private_docx_domain import (
    DOCX_MIME,
    DOCX_TRANSFORMATION_ID,
    MAX_DOCX_BYTES,
    EXPECTED_REJECTIONS,
    DOCX_PRIVATE_CORPUS_ID,
    PreparedDocxSource,
    DocxExpectationCorpus,
    PrivateDocxDomainError,
    ReviewedDocxSource,
    build_private_docx_expectation,
    cleanup_private_docx_corpus,
    load_private_docx_manifest,
    load_reviewed_docx_sources,
    prepare_private_docx_corpus,
    restore_nomagic_docx,
    validate_docx_expectation,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "ao_lore" / "docx"
SPEC_PATH = FIXTURE_ROOT / "fixture-spec.json"
LEGACY_EMPTY_INTENT_SCHEMA = "ao.lore.private-docx-empty-cleanup-intent.v0.1"
LEGACY_EMPTY_INTENT_POLICY = "remove-exact-empty-private-docx-directory-v1"
LEGACY_EMPTY_INTENT_NAME = ".empty-docx-cleanup-intent.json"
LEGACY_EMPTY_INTENT_STAGE = ".empty-docx-cleanup-intent.staging"


def legacy_empty_intent_body(intent):
    return (json.dumps(intent, sort_keys=True, separators=(",", ":")) + "\n").encode()


def generator_module():
    path = FIXTURE_ROOT / "generate.py"
    spec = importlib.util.spec_from_file_location("ao_lore_docx_fixture_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PrivateDocxFixtureTests(unittest.TestCase):
    EXPECTED_IDS = (
        "minimal-paragraph",
        "hierarchy-and-list",
        "nested-table",
        "links-and-notes",
        "media-and-drawing",
        "mixed-structure",
    )

    def test_spec_has_exact_public_safe_fixture_set(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertEqual(self.EXPECTED_IDS, tuple(fixture["id"] for fixture in corpus["fixtures"]))
        serialized = json.dumps(corpus, sort_keys=True).lower()
        self.assertNotIn("private", serialized)
        self.assertNotIn("ocr", serialized)
        self.assertNotIn("file://", serialized)

    def test_generate_public_api_signature_is_exact(self):
        generator = generator_module()
        self.assertEqual(
            "(destination: 'Path', *, check: 'bool') -> 'None'",
            str(inspect.signature(generator.generate)),
        )

    def test_generation_is_deterministic_and_binds_exact_bytes(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        generator = generator_module()
        for fixture in corpus["fixtures"]:
            with self.subTest(fixture=fixture["id"]):
                first = generator.generate_fixture_bytes(fixture)
                second = generator.generate_fixture_bytes(fixture)
                self.assertEqual(first, second)
                self.assertEqual(
                    "sha256:" + hashlib.sha256(first).hexdigest(),
                    fixture["sha256"],
                )
                self.assertEqual(first, (FIXTURE_ROOT / fixture["file"]).read_bytes())

    def test_generated_fixtures_expose_declared_ooxml_structures(self):
        corpus = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        bodies = {
            fixture["id"]: (FIXTURE_ROOT / fixture["file"]).read_bytes()
            for fixture in corpus["fixtures"]
        }
        expected_members = (
            "[Content_Types].xml",
            "_rels/.rels",
            "docProps/app.xml",
            "docProps/core.xml",
            "word/_rels/document.xml.rels",
            "word/document.xml",
            "word/styles.xml",
        )
        for fixture_id, body in bodies.items():
            with self.subTest(fixture=fixture_id):
                with zipfile.ZipFile(BytesIO(body)) as archive:
                    names = archive.namelist()
                    self.assertEqual(names, sorted(names))
                    for member in expected_members:
                        self.assertIn(member, names)
                    self.assertEqual(
                        (2020, 1, 1, 0, 0, 0),
                        archive.getinfo(names[0]).date_time,
                    )
        with zipfile.ZipFile(BytesIO(bodies["hierarchy-and-list"])) as archive:
            self.assertIn("word/numbering.xml", archive.namelist())
            self.assertIn(b"<w:numPr>", archive.read("word/document.xml"))
            self.assertIn(b"Heading 1", archive.read("word/styles.xml"))
        with zipfile.ZipFile(BytesIO(bodies["nested-table"])) as archive:
            document = archive.read("word/document.xml")
            self.assertGreaterEqual(document.count(b"<w:tbl"), 2)
        with zipfile.ZipFile(BytesIO(bodies["links-and-notes"])) as archive:
            self.assertIn("word/footnotes.xml", archive.namelist())
            self.assertIn("word/endnotes.xml", archive.namelist())
            self.assertIn(b"Hyperlink", archive.read("word/document.xml"))
            self.assertIn(b"relationships/hyperlink", archive.read("word/_rels/document.xml.rels"))
        with zipfile.ZipFile(BytesIO(bodies["media-and-drawing"])) as archive:
            self.assertIn("word/media/pixel.bin", archive.namelist())
            self.assertIn(b"<w:drawing>", archive.read("word/document.xml"))
        with zipfile.ZipFile(BytesIO(bodies["mixed-structure"])) as archive:
            self.assertIn("word/footnotes.xml", archive.namelist())
            self.assertIn("word/endnotes.xml", archive.namelist())
            self.assertIn("word/numbering.xml", archive.namelist())
            self.assertIn("word/media/mixed.bin", archive.namelist())
            self.assertGreaterEqual(archive.read("word/document.xml").count(b"<w:tbl"), 1)

    def test_generator_rejects_duplicate_keys_nonlocal_targets_and_invalid_shapes(self):
        generator = generator_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            duplicate = root / "fixture-spec.json"
            duplicate.write_text(
                '{"schema_version":"ao.lore.docx-fixture-corpus.v0.1",'
                '"fixtures":[],"fixtures":[]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                generator.load_fixture_spec(duplicate)
        fixture = json.loads(SPEC_PATH.read_text(encoding="utf-8"))["fixtures"][0]
        for changed in (
            {**fixture, "unknown": True},
            {**fixture, "file": "../escape.docx"},
            {**fixture, "paragraphs": "not-a-list"},
            {**fixture, "tables": [[["valid", 7]]]},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ValueError):
                    generator.generate_fixture_bytes(changed)

    def test_build_docx_fixture_rejects_control_character_media_filenames(self):
        generator = generator_module()
        for name in ("bad\x00.bin", "bad\n.bin", "bad\x1f.bin", "bad\x7f.bin"):
            with self.subTest(name=repr(name)):
                with self.assertRaises(ValueError):
                    generator.build_docx_fixture(
                        paragraphs=({"text": "Title", "style": "Heading1"},),
                        media=((name, b"x", "application/octet-stream"),),
                    )

    def test_fixture_spec_validation_rejects_control_character_fixture_and_media_names(self):
        generator = generator_module()
        base = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        cases = (
            ("fixture file NUL", {"file": "bad\u0000.docx"}),
            ("fixture file newline", {"file": "bad\n.docx"}),
            (
                "media NUL",
                {"media": [{"name": "bad\u0000.bin", "hex": "01", "content_type": "application/octet-stream"}]},
            ),
            (
                "media control",
                {"media": [{"name": "bad\u0001.bin", "hex": "01", "content_type": "application/octet-stream"}]},
            ),
        )
        for label, mutation in cases:
            with self.subTest(label=label):
                corpus = json.loads(json.dumps(base))
                corpus["fixtures"][0].update(mutation)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "fixture-spec.json"
                    path.write_text(json.dumps(corpus), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        generator.load_fixture_spec(path)

    def test_build_docx_fixture_rejects_inexact_types_and_check_mode_detects_drift(self):
        generator = generator_module()

        class DictSubclass(dict):
            pass

        class StringSubclass(str):
            pass

        with self.assertRaises(ValueError):
            generator.build_docx_fixture(
                paragraphs=(DictSubclass({"text": "Title", "style": "Heading1"}),)
            )
        with self.assertRaises(ValueError):
            generator.build_docx_fixture(
                paragraphs=({"text": StringSubclass("Title"), "style": "Heading1"},)
            )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_copy = root / "fixture-spec.json"
            spec_copy.write_bytes(SPEC_PATH.read_bytes())
            destination = root / "fixtures"
            destination.mkdir()
            generator._generate_with_spec(destination, check=False, spec_path=spec_copy)
            first = next(destination.glob("*.docx"))
            first.write_bytes(first.read_bytes() + b"drift")
            with self.assertRaisesRegex(ValueError, "fixture bytes drifted"):
                generator._generate_with_spec(destination, check=True, spec_path=spec_copy)

    def test_generation_rejects_symlinked_destination_bindings_and_preserves_external_files(self):
        generator = generator_module()
        fixture_name = json.loads(SPEC_PATH.read_text(encoding="utf-8"))["fixtures"][0]["file"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec_copy = root / "fixture-spec.json"
            spec_copy.write_bytes(SPEC_PATH.read_bytes())

            real_root = root / "real-root"
            real_root.mkdir()
            symlink_root = root / "symlink-root"
            symlink_root.symlink_to(real_root, target_is_directory=True)
            with self.assertRaises(ValueError):
                generator._generate_with_spec(symlink_root, check=False, spec_path=spec_copy)

            parent_real = root / "parent-real"
            parent_real.mkdir()
            parent_link = root / "parent-link"
            parent_link.symlink_to(parent_real, target_is_directory=True)
            with self.assertRaises(ValueError):
                generator._generate_with_spec(parent_link / "fixtures", check=False, spec_path=spec_copy)

            destination = root / "destination"
            destination.mkdir()
            external = root / "external.docx"
            external.write_bytes(b"EXTERNAL")
            target = destination / fixture_name
            target.symlink_to(external)
            with self.assertRaises(ValueError):
                generator._generate_with_spec(destination, check=False, spec_path=spec_copy)
            self.assertEqual(b"EXTERNAL", external.read_bytes())
            with self.assertRaises(ValueError):
                generator._generate_with_spec(destination, check=True, spec_path=spec_copy)
            self.assertEqual(b"EXTERNAL", external.read_bytes())


def _digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _reviewed_source(*, item_id: str, source_name: str, source: bytes) -> ReviewedDocxSource:
    derived = b"PK\x03\x04" + source[4:]
    return ReviewedDocxSource(
        item_id=item_id,
        source_name=source_name,
        source_digest=_digest(source),
        derived_digest=_digest(derived),
        source_bytes=len(source),
        derived_bytes=len(derived),
        transformation_id=DOCX_TRANSFORMATION_ID,
    )


def _rewrite_docx(
    body: bytes,
    *,
    replacements: dict[str, bytes] | None = None,
    additions: tuple[tuple[str, bytes], ...] = (),
) -> bytes:
    rewritten = BytesIO()
    replacements = replacements or {}
    with zipfile.ZipFile(BytesIO(body)) as source, zipfile.ZipFile(
        rewritten, "w"
    ) as destination:
        for member in source.infolist():
            destination.writestr(
                member,
                replacements.get(member.filename, source.read(member.filename)),
            )
        for name, payload in additions:
            destination.writestr(name, payload)
    return rewritten.getvalue()


def _with_attached_template(body: bytes) -> bytes:
    relationship_name = "word/_rels/document.xml.rels"
    with zipfile.ZipFile(BytesIO(body)) as package:
        relationships = package.read(relationship_name)
    relationship = (
        b'<Relationship Id="rIdTemplate" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        b'relationships/attachedTemplate" '
        b'Target="https://example.invalid/template.dotm" TargetMode="External"/>'
    )
    return _rewrite_docx(
        body,
        replacements={
            relationship_name: relationships.replace(
                b"</Relationships>", relationship + b"</Relationships>"
            )
        },
    )


def _qualification_result(expectation_digest: str) -> dict[str, object]:
    result = {
        "schema_version": "ao.lore.docx-benchmark-result.v0.1",
        "fixture_corpus_digest": expectation_digest,
        "parser_id": "native-docx-ooxml",
        "parser_version": "1.0.0",
        "parser_configuration_digest": docx_configuration_digest(DocxLimits()),
        "document_ir_version": "ao.lore.document-ir.v0.1",
        "attempts_per_document": 2,
        "document_count": 100,
        "metrics": {
            "text_fidelity": 1.0,
            "structural_fidelity": 1.0,
            "source_location_fidelity": 1.0,
            "expected_outcome_accuracy": 1.0,
            "expected_rejection_count": 12,
            "accepted_document_count": 88,
        },
        "exclusions": [],
        "stable_failures": {
            "invalid_package": 1,
            "unsupported_active_content": 11,
        },
        "repeatability": {"runs": 2, "identical": True, "score": 1.0},
        "unexpected_failures": [],
        "decision": "hold",
    }
    result["result_digest"] = private_docx_domain_module.canonical_digest(result)
    return result


def _patch_first_zip_entry(
    body: bytes,
    *,
    local_flags: int,
    central_flags: int | None = None,
    local_method: int | None = None,
    central_method: int | None = None,
) -> bytes:
    patched = bytearray(body)
    central = body.find(b"PK\x01\x02")
    if central < 0 or body[:4] != b"PK\x03\x04":
        raise AssertionError("synthetic DOCX ZIP layout is invalid")
    struct.pack_into("<H", patched, 6, local_flags)
    struct.pack_into(
        "<H", patched, central + 8,
        local_flags if central_flags is None else central_flags,
    )
    if local_method is not None:
        struct.pack_into("<H", patched, 8, local_method)
    if central_method is not None:
        struct.pack_into("<H", patched, central + 10, central_method)
    return bytes(patched)


class PrivateDocxDomainSourceTests(unittest.TestCase):
    def setUp(self):
        self.generator = generator_module()
        self.canonical = self.generator.build_docx_fixture(paragraphs=({"text": "evidence"},))
        self.source = b"\x00\x00\x00\x00" + self.canonical[4:]
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.reviewed = (
            _reviewed_source(
                item_id="docx-domain-01",
                source_name="domain-01.docx",
                source=self.source,
            ),
        )
        (self.root / self.reviewed[0].source_name).write_bytes(self.source)

    def tearDown(self):
        self.temporary.cleanup()

    def test_exact_four_byte_transform_and_stable_binding(self):
        derived = restore_nomagic_docx(self.source)
        self.assertEqual(self.canonical, derived)
        self.assertEqual(derived[4:], self.source[4:])
        self.assertEqual(DOCX_MIME, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertEqual(DOCX_TRANSFORMATION_ID, "restore-ooxml-local-header-v1")
        self.assertEqual(50 * 1024 * 1024, MAX_DOCX_BYTES)

        loaded = load_reviewed_docx_sources(self.root, self.reviewed)
        self.assertEqual(1, len(loaded))
        self.assertIsInstance(loaded[0], PreparedDocxSource)
        self.assertEqual(self.reviewed[0].item_id, loaded[0].item_id)
        self.assertEqual(self.reviewed[0].source_digest, loaded[0].source_digest)
        self.assertEqual(self.reviewed[0].derived_digest, loaded[0].derived_digest)
        self.assertEqual(self.reviewed[0].source_bytes, loaded[0].source_bytes)
        self.assertEqual(self.reviewed[0].derived_bytes, loaded[0].derived_bytes)
        self.assertEqual(self.reviewed[0].transformation_id, loaded[0].transformation_id)
        self.assertEqual(self.canonical, loaded[0].body)

    def test_held_reviewed_loader_holds_exact_100_item_negative_partition(self):
        active = _with_attached_template(self.canonical)
        invalid = _rewrite_docx(
            self.canonical,
            additions=(("opaque.bin", b"unowned-root-payload"),),
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        review = []
        expected_bodies = []
        for index in range(1, 101):
            canonical = (
                invalid
                if index == 40
                else active
                if 90 <= index <= 100
                else self.canonical
            )
            source = b"\x00\x00\x00\x00" + canonical[4:]
            source_name = f"{index:04d}-reviewed.docx"
            (root / source_name).write_bytes(source)
            review.append(
                _reviewed_source(
                    item_id=f"docx-{index:04d}",
                    source_name=source_name,
                    source=source,
                )
            )
            expected_bodies.append(canonical)

        expectation = build_private_docx_expectation(root)
        self.assertEqual(
            {"accept": 88, "active-content": 11, "invalid-package": 1},
            {
                "accept": sum(
                    item["expected_outcome"] == "accept"
                    for item in expectation["items"]
                ),
                "active-content": sum(
                    item["expected_rejection"] == "active-content"
                    for item in expectation["items"]
                ),
                "invalid-package": sum(
                    item["expected_rejection"] == "invalid-package"
                    for item in expectation["items"]
                ),
            },
        )

        loaded = load_reviewed_docx_sources(root, tuple(review))
        self.assertEqual(tuple(f"docx-{index:04d}" for index in range(1, 101)), tuple(
            item.item_id for item in loaded
        ))
        self.assertEqual(tuple(expected_bodies), tuple(item.body for item in loaded))
        for reviewed, prepared in zip(review, loaded, strict=True):
            self.assertEqual(reviewed.source_digest, prepared.source_digest)
            self.assertEqual(reviewed.derived_digest, prepared.derived_digest)
            self.assertEqual(reviewed.source_bytes, prepared.source_bytes)
            self.assertEqual(reviewed.derived_bytes, prepared.derived_bytes)
            self.assertEqual(reviewed.transformation_id, prepared.transformation_id)

    def test_transform_rejects_ordinary_short_ambiguous_inexact_and_oversized_input(self):
        class BytesSubclass(bytes):
            pass

        non_ooxml_zip_skeleton = b"\x00" * 4 + struct.pack(
            "<5H3L2H",
            20,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            1,
            0,
        ) + b"a"
        impossible_local_header = struct.pack(
            "<4s5H3L2H",
            b"PK\x03\x04",
            0,
            0,
            99,
            0,
            0,
            0,
            0,
            0,
            1,
            0,
        ) + b"a"
        invalid = (
            self.canonical,
            b"",
            b"\x00" * 3,
            b"\x00" * 4,
            b"\x00" * 4 + b"NOTZIP",
            b"\x00" * 4 + b"PK\x05\x06trailer",
            b"\x00" * 4 + b"PK\x03\x05bad-method",
            non_ooxml_zip_skeleton,
            b"\x00" * 4 + impossible_local_header[4:],
            bytearray(self.source),
            memoryview(self.source),
            BytesSubclass(self.source),
        )
        for body in invalid:
            with self.subTest(body_type=type(body), body=bytes(body[:16]) if isinstance(body, (bytes, bytearray, memoryview)) else body):
                with self.assertRaisesRegex(
                    PrivateDocxDomainError, "private DOCX source condition is invalid"
                ):
                    restore_nomagic_docx(body)  # type: ignore[arg-type]

        with patch("ao_lore.private_docx_domain.MAX_DOCX_BYTES", len(self.source) - 1):
            with self.assertRaisesRegex(
                PrivateDocxDomainError, "private DOCX source condition is invalid"
            ):
                restore_nomagic_docx(self.source)

    def test_transform_allows_only_deflate_option_flags_with_consistent_headers(self):
        for flags in (0x0002, 0x0004, 0x0006, 0x0802, 0x0804, 0x0806):
            with self.subTest(allowed_flags=hex(flags)):
                canonical = _patch_first_zip_entry(
                    self.canonical,
                    local_flags=flags,
                )
                source = b"\x00" * 4 + canonical[4:]
                restored = restore_nomagic_docx(source)
                self.assertEqual(canonical, restored)
                self.assertEqual(source[4:], restored[4:])

        for flags in (
            0x0001,
            0x0008,
            0x0010,
            0x0020,
            0x0040,
            0x0080,
            0x0100,
            0x1000,
            0x2000,
            0x4000,
            0x8000,
        ):
            with self.subTest(rejected_flags=hex(flags)):
                canonical = _patch_first_zip_entry(
                    self.canonical,
                    local_flags=flags,
                )
                with self.assertRaisesRegex(
                    PrivateDocxDomainError,
                    "private DOCX source condition is invalid",
                ):
                    restore_nomagic_docx(b"\x00" * 4 + canonical[4:])

        for canonical in (
            _patch_first_zip_entry(
                self.canonical,
                local_flags=0x0006,
                central_flags=0,
            ),
            _patch_first_zip_entry(
                self.canonical,
                local_flags=0,
                central_flags=0x0006,
            ),
            _patch_first_zip_entry(
                self.canonical,
                local_flags=0x0006,
                local_method=0,
                central_method=0,
            ),
        ):
            with self.subTest(rejected_header=canonical[:12]):
                with self.assertRaisesRegex(
                    PrivateDocxDomainError,
                    "private DOCX source condition is invalid",
                ):
                    restore_nomagic_docx(b"\x00" * 4 + canonical[4:])

    def test_held_source_load_rejects_duplicates_drift_links_and_unsafe_nodes(self):
        first = self.root / self.reviewed[0].source_name
        duplicate = (
            self.reviewed[0],
            _reviewed_source(
                item_id="docx-domain-02",
                source_name="DOMAIN-01.docx",
                source=self.source,
            ),
        )
        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX source qualification failed"
        ):
            load_reviewed_docx_sources(self.root, duplicate)

        outside = self.root / "outside.docx"
        outside.write_bytes(self.source)
        first.unlink()
        first.symlink_to(outside.name)
        with self.assertRaises(PrivateDocxDomainError):
            load_reviewed_docx_sources(self.root, self.reviewed)
        first.unlink()
        first.write_bytes(self.source)

        hardlink = self.root / "hardlink.docx"
        os.link(first, hardlink)
        with self.assertRaises(PrivateDocxDomainError):
            load_reviewed_docx_sources(self.root, self.reviewed)
        hardlink.unlink()

        first.unlink()
        os.mkfifo(first)
        with self.assertRaises(PrivateDocxDomainError):
            load_reviewed_docx_sources(self.root, self.reviewed)

        first.unlink()
        first.write_bytes(self.source)
        linked_root = self.root.parent / f"{self.root.name}-link"
        linked_root.symlink_to(self.root, target_is_directory=True)
        try:
            with self.assertRaises(PrivateDocxDomainError):
                load_reviewed_docx_sources(linked_root, self.reviewed)
        finally:
            linked_root.unlink()

    def test_held_source_load_detects_mutation_and_redacts_private_errors(self):
        original_fstat = os.fstat
        calls = 0

        def changed_fstat(descriptor):
            nonlocal calls
            result = original_fstat(descriptor)
            calls += 1
            if calls == 2:
                (self.root / self.reviewed[0].source_name).write_bytes(self.source + b"changed")
            return result

        with patch("ao_lore.private_docx_domain.os.fstat", side_effect=changed_fstat):
            with self.assertRaises(PrivateDocxDomainError):
                load_reviewed_docx_sources(self.root, self.reviewed)

        private = str(self.root / "secret.docx")
        with patch(
            "ao_lore.private_docx_domain._raw_restore_nomagic_docx",
            side_effect=OSError(private),
        ):
            with self.assertRaises(PrivateDocxDomainError) as captured:
                load_reviewed_docx_sources(self.root, self.reviewed)
        self.assertEqual(
            "private DOCX source qualification failed", str(captured.exception)
        )
        self.assertNotIn(private, str(captured.exception))

        with patch(
            "ao_lore.private_docx_domain._raw_restore_nomagic_docx",
            return_value=self.canonical + b"drift",
        ):
            with self.assertRaises(PrivateDocxDomainError):
                load_reviewed_docx_sources(self.root, self.reviewed)

    def test_held_source_load_closes_descriptors_on_exception_and_interrupt(self):
        before = len(os.listdir("/proc/self/fd"))
        with patch(
            "ao_lore.private_docx_domain._raw_restore_nomagic_docx",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                load_reviewed_docx_sources(self.root, self.reviewed)
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))

        before = len(os.listdir("/proc/self/fd"))
        with patch(
            "ao_lore.private_docx_domain._raw_restore_nomagic_docx",
            side_effect=BaseException,
        ):
            with self.assertRaises(BaseException):
                load_reviewed_docx_sources(self.root, self.reviewed)
        self.assertEqual(before, len(os.listdir("/proc/self/fd")))


class PrivateDocxQualificationInputTests(unittest.TestCase):
    def setUp(self):
        self.generator = generator_module()
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "working")
        self.root = Path(self.temporary.name)
        canonical = self.generator.build_docx_fixture(
            paragraphs=({"text": "qualification evidence"},)
        )
        active = _with_attached_template(canonical)
        invalid = _rewrite_docx(
            canonical,
            additions=(("opaque.bin", b"unowned-root-payload"),),
        )
        reviewed = []
        for index in range(1, 101):
            body = invalid if index == 40 else active if index >= 90 else canonical
            source = b"\x00\x00\x00\x00" + body[4:]
            source_name = f"{index:04d}-source.docx"
            (self.root / source_name).write_bytes(source)
            reviewed.append(
                _reviewed_source(
                    item_id=f"docx-{index:04d}",
                    source_name=source_name,
                    source=source,
                )
            )
        self.reviewed = tuple(reviewed)
        self.expectation_value = build_private_docx_expectation(self.root)
        self.expectation = validate_docx_expectation(self.expectation_value)
        self.prepared = load_reviewed_docx_sources(self.root, self.reviewed)

    def tearDown(self):
        self.temporary.cleanup()

    def test_qualification_manifest_is_closed_ordered_and_digest_bound(self):
        manifest = private_docx_domain_module._qualification_input_manifest(
            self.reviewed,
            self.prepared,
            self.expectation,
        )
        validated = private_docx_domain_module._validate_qualification_input_manifest(
            manifest
        )
        self.assertEqual(manifest, validated)
        self.assertEqual(
            "ao.lore.private-docx-qualification-inputs.v0.1",
            manifest["schema_version"],
        )
        self.assertEqual(self.expectation.corpus_digest, manifest["expectation_digest"])
        self.assertEqual(100, len(manifest["documents"]))
        self.assertEqual(
            [f"{index:04d}-reviewed.docx" for index in range(1, 101)],
            [item["fixture_name"] for item in manifest["documents"]],
        )
        self.assertNotIn("source_name", json.dumps(manifest, sort_keys=True))
        self.assertTrue(
            all(
                manifest[name] is False
                for name in (
                    "ocr_enabled",
                    "network_accessed",
                    "provider_calls",
                    "promotion_authority",
                    "claims_authority_advance",
                )
            )
        )

        mutations = []
        unknown = json.loads(json.dumps(manifest))
        unknown["unknown"] = False
        mutations.append(unknown)
        reordered = json.loads(json.dumps(manifest))
        reordered["documents"][0], reordered["documents"][1] = (
            reordered["documents"][1],
            reordered["documents"][0],
        )
        mutations.append(reordered)
        wrong_name = json.loads(json.dumps(manifest))
        wrong_name["documents"][0]["fixture_name"] = "private-name.docx"
        mutations.append(wrong_name)
        drifted = json.loads(json.dumps(manifest))
        drifted["documents"][0]["derived_digest"] = "sha256:" + "f" * 64
        mutations.append(drifted)
        widened = json.loads(json.dumps(manifest))
        widened["network_accessed"] = True
        mutations.append(widened)
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                with self.assertRaises(PrivateDocxDomainError):
                    private_docx_domain_module._validate_qualification_input_manifest(
                        mutation
                    )

    def test_prepare_publishes_one_bound_tree_and_is_idempotent(self):
        originals = {
            reviewed.source_name: (self.root / reviewed.source_name).read_bytes()
            for reviewed in self.reviewed
        }
        runtime = self.root / "runtime"
        first = private_docx_domain_module.prepare_private_docx_qualification_inputs(
            self.reviewed,
            self.expectation_value,
            source_root=self.root,
            runtime_root=runtime,
        )
        final = runtime / "private-docx" / "qualification-inputs"
        corpus = final / "corpus"
        self.assertEqual(
            {"expectation.json", "manifest.json", "corpus"},
            {path.name for path in final.iterdir()},
        )
        self.assertEqual(
            [f"{index:04d}-reviewed.docx" for index in range(1, 101)],
            sorted(path.name for path in corpus.iterdir()),
        )
        self.assertEqual(
            self.expectation_value,
            json.loads((final / "expectation.json").read_text(encoding="utf-8")),
        )
        self.assertEqual(
            first,
            json.loads((final / "manifest.json").read_text(encoding="utf-8")),
        )
        for reviewed, document in zip(
            self.reviewed, first["documents"], strict=True
        ):
            fixture = corpus / document["fixture_name"]
            self.assertEqual(originals[reviewed.source_name], fixture.read_bytes())
            self.assertEqual(reviewed.source_digest, _digest(fixture.read_bytes()))
        identities = {
            path.relative_to(final).as_posix(): (path.stat().st_dev, path.stat().st_ino)
            for path in final.rglob("*")
        }

        second = private_docx_domain_module.prepare_private_docx_qualification_inputs(
            self.reviewed,
            self.expectation_value,
            source_root=self.root,
            runtime_root=runtime,
        )
        self.assertEqual(first, second)
        self.assertEqual(
            identities,
            {
                path.relative_to(final).as_posix(): (
                    path.stat().st_dev,
                    path.stat().st_ino,
                )
                for path in final.rglob("*")
            },
        )
        self.assertFalse(
            (runtime / "private-docx/.qualification-inputs-intent.json").exists()
        )
        self.assertFalse(
            (runtime / "private-docx/.qualification-inputs-staging").exists()
        )
        self.assertEqual(
            originals,
            {
                reviewed.source_name: (self.root / reviewed.source_name).read_bytes()
                for reviewed in self.reviewed
            },
        )

    def test_prepare_recovers_intent_partial_stage_and_published_final(self):
        originals = {
            reviewed.source_name: (self.root / reviewed.source_name).read_bytes()
            for reviewed in self.reviewed
        }

        def interrupt_after_intent(module, real_persist):
            interrupted = False

            def persist(descriptor, name, body):
                nonlocal interrupted
                real_persist(descriptor, name, body)
                if name == ".qualification-inputs-intent.json" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()

            return patch.object(module, "_persist_bytes_at", side_effect=persist)

        def interrupt_mid_fixture(module, real_persist):
            interrupted = False

            def persist(descriptor, name, body):
                nonlocal interrupted
                real_persist(descriptor, name, body)
                if name == "0050-reviewed.docx" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()

            return patch.object(module, "_persist_bytes_at", side_effect=persist)

        def interrupt_before_rename(module, _real_persist):
            return patch.object(module, "_rename_noreplace_at", side_effect=KeyboardInterrupt)

        def interrupt_after_rename(module, _real_persist):
            real_rename = module._rename_noreplace_at
            interrupted = False

            def rename(*args):
                nonlocal interrupted
                real_rename(*args)
                if not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()

            return patch.object(module, "_rename_noreplace_at", side_effect=rename)

        def interrupt_after_intent_removal(module, _real_persist):
            real_unlink = module._unlink_verified_at
            interrupted = False

            def unlink(descriptor, name, body, maximum):
                nonlocal interrupted
                real_unlink(descriptor, name, body, maximum)
                if name == ".qualification-inputs-intent.json" and not interrupted:
                    interrupted = True
                    raise KeyboardInterrupt()

            return patch.object(module, "_unlink_verified_at", side_effect=unlink)

        for index, injector in enumerate(
            (
                interrupt_after_intent,
                interrupt_mid_fixture,
                interrupt_before_rename,
                interrupt_after_rename,
                interrupt_after_intent_removal,
            ),
            1,
        ):
            with self.subTest(injector=injector.__name__):
                runtime = self.root / f"runtime-recovery-{index}"
                real_persist = private_docx_domain_module._persist_bytes_at
                with injector(private_docx_domain_module, real_persist):
                    with self.assertRaises(KeyboardInterrupt):
                        private_docx_domain_module.prepare_private_docx_qualification_inputs(
                            self.reviewed,
                            self.expectation_value,
                            source_root=self.root,
                            runtime_root=runtime,
                        )
                recovered = private_docx_domain_module.prepare_private_docx_qualification_inputs(
                    self.reviewed,
                    self.expectation_value,
                    source_root=self.root,
                    runtime_root=runtime,
                )
                self.assertEqual(100, len(recovered["documents"]))
                private_root = runtime / "private-docx"
                self.assertEqual(
                    {".private-docx.lock", "qualification-inputs"},
                    {path.name for path in private_root.iterdir()},
                )
                self.assertEqual(
                    originals,
                    {
                        reviewed.source_name: (
                            self.root / reviewed.source_name
                        ).read_bytes()
                        for reviewed in self.reviewed
                    },
                )

    def test_prepare_preserves_foreign_or_drifted_qualification_state(self):
        runtime = self.root / "runtime-foreign-intent"
        real_persist = private_docx_domain_module._persist_bytes_at
        interrupted = False

        def persist_intent(descriptor, name, body):
            nonlocal interrupted
            real_persist(descriptor, name, body)
            if name == ".qualification-inputs-intent.json" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(
            private_docx_domain_module, "_persist_bytes_at", side_effect=persist_intent
        ):
            with self.assertRaises(KeyboardInterrupt):
                private_docx_domain_module.prepare_private_docx_qualification_inputs(
                    self.reviewed,
                    self.expectation_value,
                    source_root=self.root,
                    runtime_root=runtime,
                )
        intent = runtime / "private-docx/.qualification-inputs-intent.json"
        foreign_intent = b'{"foreign":true}\n'
        intent.write_bytes(foreign_intent)
        with self.assertRaises(PrivateDocxDomainError):
            private_docx_domain_module.prepare_private_docx_qualification_inputs(
                self.reviewed,
                self.expectation_value,
                source_root=self.root,
                runtime_root=runtime,
            )
        self.assertEqual(foreign_intent, intent.read_bytes())
        with self.assertRaises(PrivateDocxDomainError):
            cleanup_private_docx_corpus(runtime_root=runtime)
        self.assertEqual(foreign_intent, intent.read_bytes())

        stage_runtime = self.root / "runtime-foreign-stage"
        interrupted = False

        def persist_fixture(descriptor, name, body):
            nonlocal interrupted
            real_persist(descriptor, name, body)
            if name == "0050-reviewed.docx" and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(
            private_docx_domain_module, "_persist_bytes_at", side_effect=persist_fixture
        ):
            with self.assertRaises(KeyboardInterrupt):
                private_docx_domain_module.prepare_private_docx_qualification_inputs(
                    self.reviewed,
                    self.expectation_value,
                    source_root=self.root,
                    runtime_root=stage_runtime,
                )
        unknown = (
            stage_runtime
            / "private-docx/.qualification-inputs-staging/corpus/foreign.bin"
        )
        unknown.write_bytes(b"foreign")
        with self.assertRaises(PrivateDocxDomainError):
            private_docx_domain_module.prepare_private_docx_qualification_inputs(
                self.reviewed,
                self.expectation_value,
                source_root=self.root,
                runtime_root=stage_runtime,
            )
        self.assertEqual(b"foreign", unknown.read_bytes())

        no_intent_runtime = self.root / "runtime-no-intent"
        stage = no_intent_runtime / "private-docx/.qualification-inputs-staging"
        stage.mkdir(parents=True)
        with self.assertRaises(PrivateDocxDomainError):
            private_docx_domain_module.prepare_private_docx_qualification_inputs(
                self.reviewed,
                self.expectation_value,
                source_root=self.root,
                runtime_root=no_intent_runtime,
            )
        self.assertTrue(stage.is_dir())

    def test_prepare_rejects_count_preserving_semantic_expectation_swap(self):
        swapped = json.loads(json.dumps(self.expectation_value))
        semantic_fields = (
            "package_inventory_digest",
            "normalized_text_digest",
            "structural_event_digest",
            "counts",
            "expected_outcome",
            "expected_rejection",
            "page_count",
            "page_count_reason",
        )
        for field in semantic_fields:
            swapped["items"][0][field], swapped["items"][89][field] = (
                swapped["items"][89][field],
                swapped["items"][0][field],
            )
        validate_docx_expectation(swapped)
        runtime = self.root / "runtime-semantic-swap"

        with self.assertRaises(PrivateDocxDomainError):
            private_docx_domain_module.prepare_private_docx_qualification_inputs(
                self.reviewed,
                swapped,
                source_root=self.root,
                runtime_root=runtime,
            )

        self.assertFalse((runtime / "private-docx").exists())

    def test_cleanup_recovers_exact_legacy_expectation_only_state(self):
        runtime = self.root / "runtime-legacy-expectation"
        private_root = runtime / "private-docx"
        private_root.mkdir(parents=True)
        (private_root / ".private-docx.lock").write_bytes(b"")
        expectation_body = (
            json.dumps(
                self.expectation_value,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        expectation_path = private_root / "expectation.json"
        expectation_path.write_bytes(expectation_body)

        self.assertEqual(
            {
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "removed": False,
                "verified_documents": 0,
            },
            cleanup_private_docx_corpus(runtime_root=runtime),
        )
        self.assertFalse(expectation_path.exists())
        self.assertEqual(
            {".private-docx.lock"},
            {path.name for path in private_root.iterdir()},
        )

    def test_cleanup_removes_only_validated_qualification_inputs(self):
        runtime = self.root / "runtime-qualification-cleanup"
        private_docx_domain_module.prepare_private_docx_qualification_inputs(
            self.reviewed,
            self.expectation_value,
            source_root=self.root,
            runtime_root=runtime,
        )
        self.assertEqual(
            {
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "removed": False,
                "verified_documents": 0,
            },
            cleanup_private_docx_corpus(runtime_root=runtime),
        )
        private_root = runtime / "private-docx"
        self.assertEqual(
            {".private-docx.lock"},
            {path.name for path in private_root.iterdir()},
        )

    def test_cleanup_recovers_after_publish_and_mid_reclaim_interrupts(self):
        for index, boundary in enumerate(("rename", "fixture"), 1):
            with self.subTest(boundary=boundary):
                runtime = self.root / f"runtime-cleanup-recovery-{index}"
                private_docx_domain_module.prepare_private_docx_qualification_inputs(
                    self.reviewed,
                    self.expectation_value,
                    source_root=self.root,
                    runtime_root=runtime,
                )
                if boundary == "rename":
                    real_rename = private_docx_domain_module._rename_noreplace_at
                    interrupted = False

                    def rename(*args):
                        nonlocal interrupted
                        real_rename(*args)
                        if not interrupted:
                            interrupted = True
                            raise KeyboardInterrupt()

                    injected = patch.object(
                        private_docx_domain_module,
                        "_rename_noreplace_at",
                        side_effect=rename,
                    )
                else:
                    real_unlink = private_docx_domain_module._unlink_verified_at
                    interrupted = False

                    def unlink(descriptor, name, body, maximum):
                        nonlocal interrupted
                        real_unlink(descriptor, name, body, maximum)
                        if name == "0050-reviewed.docx" and not interrupted:
                            interrupted = True
                            raise KeyboardInterrupt()

                    injected = patch.object(
                        private_docx_domain_module,
                        "_unlink_verified_at",
                        side_effect=unlink,
                    )
                with injected:
                    with self.assertRaises(KeyboardInterrupt):
                        cleanup_private_docx_corpus(runtime_root=runtime)
                self.assertEqual(
                    {
                        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                        "removed": False,
                        "verified_documents": 0,
                    },
                    cleanup_private_docx_corpus(runtime_root=runtime),
                )
                self.assertEqual(
                    {".private-docx.lock"},
                    {
                        path.name
                        for path in (runtime / "private-docx").iterdir()
                    },
                )

    def test_cleanup_recovers_exact_preparation_intent_and_partial_stage(self):
        for index, interrupted_name in enumerate(
            (
                ".qualification-inputs-intent.json",
                "0050-reviewed.docx",
            ),
            1,
        ):
            with self.subTest(interrupted_name=interrupted_name):
                runtime = self.root / f"runtime-preparation-cleanup-{index}"
                real_persist = private_docx_domain_module._persist_bytes_at
                interrupted = False

                def persist(descriptor, name, body):
                    nonlocal interrupted
                    real_persist(descriptor, name, body)
                    if name == interrupted_name and not interrupted:
                        interrupted = True
                        raise KeyboardInterrupt()

                with patch.object(
                    private_docx_domain_module,
                    "_persist_bytes_at",
                    side_effect=persist,
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        private_docx_domain_module.prepare_private_docx_qualification_inputs(
                            self.reviewed,
                            self.expectation_value,
                            source_root=self.root,
                            runtime_root=runtime,
                        )
                self.assertEqual(
                    {
                        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                        "removed": False,
                        "verified_documents": 0,
                    },
                    cleanup_private_docx_corpus(runtime_root=runtime),
                )
                self.assertEqual(
                    {".private-docx.lock"},
                    {
                        path.name
                        for path in (runtime / "private-docx").iterdir()
                    },
                )

    def test_qualification_inputs_result_and_uat_corpus_coexist(self):
        runtime = self.root / "runtime-coexistence"
        qualification_inputs = (
            private_docx_domain_module.prepare_private_docx_qualification_inputs(
                self.reviewed,
                self.expectation_value,
                source_root=self.root,
                runtime_root=runtime,
            )
        )
        qualification = _qualification_result(
            qualification_inputs["expectation_digest"]
        )
        qualification_path = runtime / "private-docx/qualification.json"
        qualification_path.write_text(
            json.dumps(qualification, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            qualification_inputs,
            private_docx_domain_module.prepare_private_docx_qualification_inputs(
                self.reviewed,
                self.expectation_value,
                source_root=self.root,
                runtime_root=runtime,
            ),
        )
        uat_sources = self.root / "uat-reviewed"
        uat_sources.mkdir()
        review = {
            "schema_version": "ao.lore.private-docx-domain-review.v0.1",
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "transformation_id": DOCX_TRANSFORMATION_ID,
            "documents": [],
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        for index in range(1, 5):
            canonical = self.generator.build_docx_fixture(
                paragraphs=({"text": f"UAT safe {index}"},)
            )
            source = b"\x00\x00\x00\x00" + canonical[4:]
            name = f"{index:04d}-uat.docx"
            (uat_sources / name).write_bytes(source)
            review["documents"].append(
                {"item_id": f"docx-domain-{index:02d}", "source_file": name}
            )

        corpus = prepare_private_docx_corpus(
            review,
            source_root=uat_sources,
            runtime_root=runtime,
        )
        self.assertEqual(4, len(corpus["documents"]))
        self.assertEqual(
            qualification_inputs["expectation_digest"],
            corpus["expectation_digest"],
        )
        synthetic_repository = self.root / "synthetic-repository"
        synthetic_repository.mkdir()
        from ao_lore import private_docx_uat

        with patch.object(
            private_docx_uat,
            "repository_root",
            return_value=synthetic_repository,
        ):
            prepared_run = private_docx_uat.prepare_private_docx_uat_run(
                runtime_root=runtime
            )
        self.assertEqual(
            qualification_inputs["expectation_digest"],
            prepared_run.expectation_digest,
        )
        self.assertTrue((runtime / "private-docx/qualification-inputs").is_dir())
        self.assertTrue((runtime / "private-docx/corpus").is_dir())
        self.assertEqual(
            qualification,
            json.loads(qualification_path.read_text(encoding="utf-8")),
        )
        self.assertEqual(
            {
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "removed": True,
                "verified_documents": 4,
            },
            cleanup_private_docx_corpus(runtime_root=runtime),
        )
        self.assertTrue(qualification_path.is_file())
        self.assertFalse((runtime / "private-docx/qualification-inputs").exists())
        self.assertFalse((runtime / "private-docx/corpus").exists())

    def test_coexistence_rejects_and_preserves_drifted_qualification(self):
        runtime = self.root / "runtime-drifted-qualification"
        qualification_inputs = (
            private_docx_domain_module.prepare_private_docx_qualification_inputs(
                self.reviewed,
                self.expectation_value,
                source_root=self.root,
                runtime_root=runtime,
            )
        )
        qualification = _qualification_result(
            qualification_inputs["expectation_digest"]
        )
        qualification["fixture_corpus_digest"] = "sha256:" + "f" * 64
        qualification["result_digest"] = private_docx_domain_module.canonical_digest(
            {key: value for key, value in qualification.items() if key != "result_digest"}
        )
        qualification_path = runtime / "private-docx/qualification.json"
        body = json.dumps(
            qualification, sort_keys=True, separators=(",", ":")
        ) + "\n"
        qualification_path.write_text(body, encoding="utf-8")

        with self.assertRaises(PrivateDocxDomainError):
            cleanup_private_docx_corpus(runtime_root=runtime)

        self.assertEqual(body, qualification_path.read_text(encoding="utf-8"))
        self.assertTrue((runtime / "private-docx/qualification-inputs").is_dir())

    def test_reclaim_rejects_post_rename_qualification_drift_before_deletion(self):
        runtime = self.root / "runtime-reclaim-result-drift"
        inputs = private_docx_domain_module.prepare_private_docx_qualification_inputs(
            self.reviewed,
            self.expectation_value,
            source_root=self.root,
            runtime_root=runtime,
        )
        qualification = _qualification_result(inputs["expectation_digest"])
        qualification_path = runtime / "private-docx/qualification.json"
        qualification_path.write_text(
            json.dumps(qualification, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        final = runtime / "private-docx/qualification-inputs"
        reclaim = runtime / "private-docx/.qualification-inputs-reclaim"
        final.rename(reclaim)
        qualification["fixture_corpus_digest"] = "sha256:" + "f" * 64
        qualification["result_digest"] = private_docx_domain_module.canonical_digest(
            {key: value for key, value in qualification.items() if key != "result_digest"}
        )
        drifted_body = (
            json.dumps(qualification, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        qualification_path.write_text(drifted_body, encoding="utf-8")
        before = sorted(
            (path.relative_to(reclaim).as_posix(), path.stat().st_size)
            for path in reclaim.rglob("*")
        )

        with self.assertRaises(PrivateDocxDomainError):
            cleanup_private_docx_corpus(runtime_root=runtime)

        self.assertEqual(drifted_body, qualification_path.read_text(encoding="utf-8"))
        self.assertEqual(
            before,
            sorted(
                (path.relative_to(reclaim).as_posix(), path.stat().st_size)
                for path in reclaim.rglob("*")
            ),
        )

    def test_cleanup_preserves_empty_uat_skeleton_beside_invalid_result(self):
        runtime = self.root / "runtime-invalid-result-empty-corpus"
        private_root = runtime / "private-docx"
        (private_root / "corpus").mkdir(parents=True)
        (private_root / ".private-docx.lock").write_bytes(b"")
        qualification_path = private_root / "qualification.json"
        qualification_path.write_bytes(b"{}\n")

        with self.assertRaises(PrivateDocxDomainError):
            cleanup_private_docx_corpus(runtime_root=runtime)

        self.assertEqual(b"{}\n", qualification_path.read_bytes())
        self.assertTrue((private_root / "corpus").is_dir())


class PrivateDocxExpectationValidationTests(unittest.TestCase):
    def _digest(self, value: int) -> str:
        return f"sha256:{value:064x}"

    def _item(
        self,
        index: int,
        *,
        outcome: str = "accept",
        rejection: str | None = None,
    ) -> dict[str, object]:
        return {
            "item_id": f"docx-{index:04d}",
            "source_digest": self._digest(index),
            "derived_digest": self._digest(index + 1000),
            "source_bytes": 1024 + index,
            "derived_bytes": 2048 + index,
            "transformation_id": DOCX_TRANSFORMATION_ID,
            "package_inventory_digest": self._digest(index + 2000),
            "normalized_text_digest": self._digest(index + 3000),
            "structural_event_digest": self._digest(index + 4000),
            "counts": {
                "paragraphs": index,
                "headings": index % 5,
                "lists": index % 4,
                "tables": index % 3,
                "rows": index + 1,
                "cells": index + 2,
                "links": index % 2,
                "footnotes": index % 3,
                "endnotes": index % 4,
                "drawings": index % 2,
                "media": index % 3,
            },
            "expected_outcome": outcome,
            "expected_rejection": rejection,
            "page_count": None,
            "page_count_reason": "ooxml-pagination-unavailable",
        }

    def expectation(self) -> dict[str, object]:
        items = [self._item(index) for index in range(1, 101)]
        items[39] = self._item(40, outcome="reject", rejection="invalid-package")
        for index in range(90, 101):
            items[index - 1] = self._item(
                index,
                outcome="reject",
                rejection="active-content",
            )
        return {
            "schema_version": "ao.lore.private-docx-expectation.v0.1",
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "items": items,
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }

    def test_validate_expectation_derives_digests_and_exact_closed_rejections(self):
        validated = validate_docx_expectation(self.expectation())
        self.assertIsInstance(validated, DocxExpectationCorpus)
        self.assertEqual(DOCX_PRIVATE_CORPUS_ID, validated.corpus_id)
        self.assertEqual(
            frozenset({"invalid_package", "unsupported_active_content"}),
            EXPECTED_REJECTIONS,
        )
        self.assertEqual(100, len(validated.documents))
        self.assertEqual(
            {"accept": 88, "active-content": 11, "invalid-package": 1},
            {
                "accept": sum(
                    item["expected_outcome"] == "accept"
                    for item in validated.documents
                ),
                "active-content": sum(
                    item["expected_rejection"] == "active-content"
                    for item in validated.documents
                ),
                "invalid-package": sum(
                    item["expected_rejection"] == "invalid-package"
                    for item in validated.documents
                ),
            },
        )
        self.assertEqual("docx-0001", validated.documents[0]["item_id"])
        self.assertEqual("docx-0100", validated.documents[-1]["item_id"])
        self.assertRegex(validated.corpus_digest, r"^sha256:[0-9a-f]{64}$")
        self.assertRegex(validated.configuration_digest, r"^sha256:[0-9a-f]{64}$")

    def test_validate_expectation_rejects_non_builtin_types_wrong_order_and_equal_digests(self):
        class DictSubclass(dict):
            pass

        class StringSubclass(str):
            pass

        for mutate in (
            lambda value: value["items"].reverse(),
            lambda value: value["items"][0].__setitem__("derived_digest", value["items"][0]["source_digest"]),
            lambda value: value["items"].__setitem__(0, DictSubclass(value["items"][0])),
            lambda value: value["items"][0].__setitem__("item_id", StringSubclass("docx-0001")),
        ):
            changed = self.expectation()
            mutate(changed)
            with self.subTest(mutate=mutate):
                with self.assertRaises(PrivateDocxDomainError):
                    validate_docx_expectation(changed)

    def test_counts_accept_object_order_and_normalize_to_contract_order(self):
        expectation = self.expectation()
        expectation["items"][0]["counts"] = dict(
            reversed(list(expectation["items"][0]["counts"].items()))
        )
        canonical_body = json.dumps(
            expectation,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        parsed = parse_strict_json(canonical_body, "private DOCX expectation")

        validated = validate_docx_expectation(parsed)

        self.assertEqual(
            private_docx_domain_module._COUNT_KEYS,
            tuple(validated.documents[0]["counts"]),
        )

    def test_counts_remain_closed_and_strict_json_rejects_duplicates(self):
        mutations = (
            lambda counts: counts.__setitem__("unknown", 0),
            lambda counts: counts.pop("paragraphs"),
            lambda counts: counts.__setitem__("paragraphs", True),
            lambda counts: counts.__setitem__("paragraphs", -1),
            lambda counts: counts.__setitem__("paragraphs", 1.0),
        )
        for mutate in mutations:
            expectation = self.expectation()
            mutate(expectation["items"][0]["counts"])
            with self.subTest(mutate=mutate):
                with self.assertRaises(PrivateDocxDomainError):
                    validate_docx_expectation(expectation)

        with self.assertRaises(Exception):
            parse_strict_json(
                b'{"paragraphs":1,"paragraphs":2}',
                "private DOCX expectation",
            )

    def test_build_expectation_rejects_non_path_input(self):
        with self.assertRaises(PrivateDocxDomainError):
            build_private_docx_expectation("not-a-path")  # type: ignore[arg-type]

    def test_expectation_signal_digests_preserve_document_order(self):
        generator = generator_module()
        first = generator.build_docx_fixture(
            paragraphs=(
                {"text": "Alpha"},
                {"text": "Beta", "style": "Heading1"},
            )
        )
        swapped = generator.build_docx_fixture(
            paragraphs=(
                {"text": "Beta", "style": "Heading1"},
                {"text": "Alpha"},
            )
        )

        from ao_lore.docx_expectation_oracle import derive_docx_semantic_expectation
        from ao_lore.docx_ooxml import DocxLimits, validate_docx_package

        first_oracle = derive_docx_semantic_expectation(
            validate_docx_package(first), DocxLimits()
        )
        swapped_oracle = derive_docx_semantic_expectation(
            validate_docx_package(swapped), DocxLimits()
        )

        self.assertEqual(first_oracle.counts, swapped_oracle.counts)
        self.assertNotEqual(
            first_oracle.normalized_text_digest,
            swapped_oracle.normalized_text_digest,
        )
        self.assertNotEqual(
            first_oracle.structural_event_digest,
            swapped_oracle.structural_event_digest,
        )


class PrivateDocxPersistenceTests(unittest.TestCase):
    REVIEW_ITEM_IDS = (
        "docx-domain-01",
        "docx-domain-02",
        "docx-domain-03",
        "docx-domain-04",
    )

    def setUp(self):
        self.generator = generator_module()
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / "working")
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "reviewed"
        self.runtime_root = self.root / "runtime"
        self.source_root.mkdir()
        self.review = {
            "schema_version": "ao.lore.private-docx-domain-review.v0.1",
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "transformation_id": DOCX_TRANSFORMATION_ID,
            "documents": [],
            "ocr_enabled": False,
            "network_accessed": False,
            "provider_calls": False,
            "promotion_authority": False,
            "claims_authority_advance": False,
        }
        for index, item_id in enumerate(self.REVIEW_ITEM_IDS, start=1):
            canonical = self.generator.build_docx_fixture(
                paragraphs=({"text": f"Docx domain sample {index}", "style": "Heading1"},),
            )
            source = b"\x00\x00\x00\x00" + canonical[4:]
            name = f"{index:04d}-docx-nomagic.docx"
            (self.source_root / name).write_bytes(source)
            self.review["documents"].append({"item_id": item_id, "source_file": name})

    def tearDown(self):
        self.temporary.cleanup()

    def test_prepare_load_and_cleanup_round_trip_is_idempotent(self):
        first = prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=self.runtime_root,
        )
        second = prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(first, second)
        self.assertEqual(first, load_private_docx_manifest(runtime_root=self.runtime_root))
        self.assertEqual(
            self.REVIEW_ITEM_IDS,
            tuple(item["item_id"] for item in first["documents"]),
        )
        self.assertTrue(
            all(item["source_digest"] != item["derived_digest"] for item in first["documents"])
        )

        self.assertEqual(
            {
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "removed": True,
                "verified_documents": 4,
            },
            cleanup_private_docx_corpus(runtime_root=self.runtime_root),
        )
        self.assertEqual(
            {
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "removed": False,
                "verified_documents": 0,
            },
            cleanup_private_docx_corpus(runtime_root=self.runtime_root),
        )

    def test_uat_preparation_rejects_active_and_invalid_reviewed_items(self):
        source_name = self.review["documents"][-1]["source_file"]
        source_path = self.source_root / source_name
        safe = b"PK\x03\x04" + source_path.read_bytes()[4:]
        cases = {
            "active": _with_attached_template(safe),
            "invalid": _rewrite_docx(
                safe,
                additions=(("opaque.bin", b"unowned-root-payload"),),
            ),
        }
        for label, canonical in cases.items():
            with self.subTest(label=label):
                source_path.write_bytes(b"\x00\x00\x00\x00" + canonical[4:])
                with self.assertRaisesRegex(
                    PrivateDocxDomainError,
                    "private DOCX corpus preparation failed",
                ):
                    prepare_private_docx_corpus(
                        self.review,
                        source_root=self.source_root,
                        runtime_root=self.root / f"runtime-{label}",
                    )

    def test_cleanup_recovers_exact_empty_owned_skeletons_and_allows_prepare(self):
        for skeleton_name in ("corpus", ".corpus-staging"):
            with self.subTest(skeleton_name=skeleton_name):
                runtime_root = self.root / f"runtime-{skeleton_name.lstrip('.')}"
                skeleton = runtime_root / "private-docx" / skeleton_name
                skeleton.mkdir(parents=True)

                self.assertEqual(
                    {
                        "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                        "removed": False,
                        "verified_documents": 0,
                    },
                    cleanup_private_docx_corpus(runtime_root=runtime_root),
                )
                self.assertFalse(skeleton.exists())
                self.assertEqual(
                    {private_docx_domain_module._LOCK_NAME},
                    {
                        path.name
                        for path in (runtime_root / "private-docx").iterdir()
                    },
                )

                manifest = prepare_private_docx_corpus(
                    self.review,
                    source_root=self.source_root,
                    runtime_root=runtime_root,
                )
                self.assertEqual(
                    self.REVIEW_ITEM_IDS,
                    tuple(document["item_id"] for document in manifest["documents"]),
                )
                cleanup_private_docx_corpus(runtime_root=runtime_root)

    def test_cleanup_recovers_empty_stage_left_by_prepare_interrupt(self):
        with patch.object(
            private_docx_domain_module,
            "_persist_bytes_at",
            side_effect=KeyboardInterrupt(),
        ):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_corpus(
                    self.review,
                    source_root=self.source_root,
                    runtime_root=self.runtime_root,
                )

        stage = self.runtime_root / "private-docx" / ".corpus-staging"
        self.assertTrue(stage.is_dir())
        self.assertEqual(
            {
                "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                "removed": False,
                "verified_documents": 0,
            },
            cleanup_private_docx_corpus(runtime_root=self.runtime_root),
        )
        self.assertFalse(stage.exists())
        manifest = prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(4, len(manifest["documents"]))

    def test_cleanup_preserves_replacement_detected_before_final_rmdir_boundary(self):
        private_root = self.runtime_root / "private-docx"
        (private_root / "corpus").mkdir(parents=True)
        real_open = private_docx_domain_module._open_bound_empty_directory_at

        def replace_after_open(directory_descriptor, name, expected_identity=None):
            descriptor, opened = real_open(
                directory_descriptor, name, expected_identity
            )
            os.rename(
                name,
                "displaced-empty-corpus",
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            os.mkdir(name, 0o700, dir_fd=directory_descriptor)
            replacement_fd = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory_descriptor
            )
            try:
                marker_fd = os.open(
                    "foreign",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=replacement_fd,
                )
                try:
                    os.write(marker_fd, b"foreign")
                finally:
                    os.close(marker_fd)
            finally:
                os.close(replacement_fd)
            return descriptor, opened

        with patch.object(
            private_docx_domain_module,
            "_open_bound_empty_directory_at",
            side_effect=replace_after_open,
        ):
            with self.assertRaisesRegex(
                PrivateDocxDomainError, "private DOCX corpus cleanup failed"
            ):
                cleanup_private_docx_corpus(runtime_root=self.runtime_root)

        self.assertEqual(b"foreign", (private_root / "corpus" / "foreign").read_bytes())
        self.assertTrue((private_root / "displaced-empty-corpus").is_dir())

    def test_cleanup_retry_is_idempotent_before_and_after_atomic_rmdir(self):
        for source_name in ("corpus", ".corpus-staging"):
            for timing in ("before", "after"):
                with self.subTest(source_name=source_name, timing=timing):
                    runtime_root = self.root / (
                        f"runtime-retry-{source_name.lstrip('.')}-{timing}"
                    )
                    private_root = runtime_root / "private-docx"
                    (private_root / source_name).mkdir(parents=True)
                    real_rmdir = os.rmdir
                    real_fsync = os.fsync
                    fsynced = []

                    def interrupt_rmdir(name, *args, **kwargs):
                        if name in {"corpus", ".corpus-staging"}:
                            if timing == "before":
                                raise KeyboardInterrupt()
                            real_rmdir(name, *args, **kwargs)
                            raise KeyboardInterrupt()
                        return real_rmdir(name, *args, **kwargs)

                    def record_fsync(descriptor):
                        fsynced.append(descriptor)
                        return real_fsync(descriptor)

                    with patch.object(
                        private_docx_domain_module.os,
                        "rmdir",
                        side_effect=interrupt_rmdir,
                    ), patch.object(
                        private_docx_domain_module.os,
                        "fsync",
                        side_effect=record_fsync,
                    ):
                        with self.assertRaises(KeyboardInterrupt):
                            cleanup_private_docx_corpus(runtime_root=runtime_root)

                    self.assertEqual(timing == "after", bool(fsynced))
                    self.assertEqual(
                        timing == "before",
                        (private_root / source_name).exists(),
                    )
                    self.assertEqual(
                        {
                            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
                            "removed": False,
                            "verified_documents": 0,
                        },
                        cleanup_private_docx_corpus(runtime_root=runtime_root),
                    )
                    manifest = prepare_private_docx_corpus(
                        self.review,
                        source_root=self.source_root,
                        runtime_root=runtime_root,
                    )
                    self.assertEqual(4, len(manifest["documents"]))

    def test_empty_cleanup_documents_cooperative_writer_final_syscall_boundary(self):
        contract = private_docx_domain_module._remove_exact_empty_directory_at.__doc__
        self.assertIsNotNone(contract)
        self.assertIn("cooperative-writer lock", contract)
        self.assertIn("non-cooperative same-UID writer", contract)
        self.assertIn("final verified-name-to-rmdir", contract)

    def test_cleanup_rejects_and_preserves_untrusted_empty_quarantines(self):
        cases = ("foreign", "nonempty", "symlink", "fifo", "multiple")
        for case in cases:
            with self.subTest(case=case):
                runtime_root = self.root / f"runtime-quarantine-{case}"
                private_root = runtime_root / "private-docx"
                private_root.mkdir(parents=True)
                marker = private_root / "candidate"
                if case == "foreign":
                    quarantine = private_root / f".empty-docx-state-{'0' * 64}.quarantine"
                    quarantine.mkdir()
                    preserved = [quarantine]
                elif case == "nonempty":
                    marker.mkdir()
                    quarantine = private_root / f".empty-docx-state-{'1' * 64}.quarantine"
                    marker.rename(quarantine)
                    (quarantine / "foreign").write_bytes(b"foreign")
                    preserved = [quarantine / "foreign"]
                elif case == "symlink":
                    target = runtime_root / "foreign-target"
                    target.mkdir()
                    marker.symlink_to(target, target_is_directory=True)
                    quarantine = private_root / f".empty-docx-state-{'2' * 64}.quarantine"
                    marker.rename(quarantine)
                    preserved = [quarantine]
                elif case == "fifo":
                    os.mkfifo(marker)
                    quarantine = private_root / f".empty-docx-state-{'3' * 64}.quarantine"
                    marker.rename(quarantine)
                    preserved = [quarantine]
                else:
                    preserved = []
                    for index, source_name in enumerate(("corpus", ".corpus-staging"), start=4):
                        candidate = private_root / source_name
                        candidate.mkdir()
                        quarantine = private_root / f".empty-docx-state-{str(index) * 64}.quarantine"
                        candidate.rename(quarantine)
                        preserved.append(quarantine)

                with self.assertRaisesRegex(
                    PrivateDocxDomainError, "private DOCX corpus cleanup failed"
                ):
                    cleanup_private_docx_corpus(runtime_root=runtime_root)
                self.assertTrue(all(path.exists() or path.is_symlink() for path in preserved))

    def test_cleanup_rejects_forged_identity_named_quarantine_without_intent(self):
        private_root = self.runtime_root / "private-docx"
        candidate = private_root / "foreign-empty-directory"
        candidate.mkdir(parents=True)
        identity = candidate.stat()
        binding = f"corpus\0{identity.st_dev}\0{identity.st_ino}".encode("ascii")
        quarantine = private_root / (
            f".empty-corpus-{hashlib.sha256(binding).hexdigest()[:24]}.quarantine"
        )
        candidate.rename(quarantine)

        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus cleanup failed"
        ):
            cleanup_private_docx_corpus(runtime_root=self.runtime_root)
        self.assertTrue(quarantine.is_dir())

    def test_cleanup_rejects_correctly_named_quarantine_without_intent(self):
        private_root = self.runtime_root / "private-docx"
        quarantine = private_root / f".empty-docx-state-{'a' * 64}.quarantine"
        quarantine.mkdir(parents=True)
        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus cleanup failed"
        ):
            cleanup_private_docx_corpus(runtime_root=self.runtime_root)
        self.assertTrue(quarantine.is_dir())

    def test_cleanup_rejects_and_preserves_malformed_or_mismatched_intent(self):
        for case in ("malformed", "identity-mismatch"):
            with self.subTest(case=case):
                runtime_root = self.root / f"runtime-intent-reject-{case}"
                private_root = runtime_root / "private-docx"
                private_root.mkdir(parents=True)
                intent_path = private_root / LEGACY_EMPTY_INTENT_NAME
                if case == "malformed":
                    intent_path.write_bytes(b"{}\n")
                    preserved = [intent_path]
                else:
                    token = "b" * 64
                    quarantine = private_root / f".empty-docx-state-{token}.quarantine"
                    quarantine.mkdir()
                    identity = quarantine.stat()
                    parent = private_root.stat()
                    intent = {
                        "schema_version": LEGACY_EMPTY_INTENT_SCHEMA,
                        "format": "docx",
                        "policy": LEGACY_EMPTY_INTENT_POLICY,
                        "source_name": "corpus",
                        "source_device": identity.st_dev,
                        "source_inode": identity.st_ino + 1,
                        "parent_device": parent.st_dev,
                        "parent_inode": parent.st_ino,
                        "token": token,
                        "quarantine_name": quarantine.name,
                    }
                    intent_path.write_bytes(
                        (json.dumps(intent, sort_keys=True, separators=(",", ":")) + "\n").encode()
                    )
                    preserved = [intent_path, quarantine]
                with self.assertRaisesRegex(
                    PrivateDocxDomainError, "private DOCX corpus cleanup failed"
                ):
                    cleanup_private_docx_corpus(runtime_root=runtime_root)
                self.assertTrue(all(path.exists() for path in preserved))

    def test_cleanup_rejects_fully_canonical_forged_intent_without_source_or_quarantine(self):
        private_root = self.runtime_root / "private-docx"
        private_root.mkdir(parents=True)
        parent = private_root.stat()
        token = "e" * 64
        intent = {
            "schema_version": LEGACY_EMPTY_INTENT_SCHEMA,
            "format": "docx",
            "policy": LEGACY_EMPTY_INTENT_POLICY,
            "source_name": "corpus",
            "source_device": parent.st_dev,
            "source_inode": parent.st_ino,
            "parent_device": parent.st_dev,
            "parent_inode": parent.st_ino,
            "token": token,
            "quarantine_name": f".empty-docx-state-{token}.quarantine",
        }
        intent_path = private_root / LEGACY_EMPTY_INTENT_NAME
        intent_path.write_bytes(legacy_empty_intent_body(intent))

        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus cleanup failed"
        ):
            cleanup_private_docx_corpus(runtime_root=self.runtime_root)
        self.assertEqual(
            legacy_empty_intent_body(intent),
            intent_path.read_bytes(),
        )

    def test_cleanup_rejects_fully_canonical_forged_intent_and_matching_quarantine(self):
        private_root = self.runtime_root / "private-docx"
        token = "f" * 64
        quarantine = private_root / f".empty-docx-state-{token}.quarantine"
        quarantine.mkdir(parents=True)
        identity = quarantine.stat()
        parent = private_root.stat()
        intent = {
            "schema_version": LEGACY_EMPTY_INTENT_SCHEMA,
            "format": "docx",
            "policy": LEGACY_EMPTY_INTENT_POLICY,
            "source_name": "corpus",
            "source_device": identity.st_dev,
            "source_inode": identity.st_ino,
            "parent_device": parent.st_dev,
            "parent_inode": parent.st_ino,
            "token": token,
            "quarantine_name": quarantine.name,
        }
        intent_path = private_root / LEGACY_EMPTY_INTENT_NAME
        intent_path.write_bytes(legacy_empty_intent_body(intent))

        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus cleanup failed"
        ):
            cleanup_private_docx_corpus(runtime_root=self.runtime_root)
        self.assertTrue(quarantine.is_dir())
        self.assertEqual(
            legacy_empty_intent_body(intent),
            intent_path.read_bytes(),
        )

    def test_cleanup_preserves_foreign_intent_and_quarantine_collisions(self):
        for case in ("intent", "intent-stage", "quarantine"):
            with self.subTest(case=case):
                runtime_root = self.root / f"runtime-collision-{case}"
                private_root = runtime_root / "private-docx"
                source = private_root / "corpus"
                source.mkdir(parents=True)
                if case == "intent":
                    collision = private_root / LEGACY_EMPTY_INTENT_NAME
                    collision.write_bytes(b"foreign")
                elif case == "intent-stage":
                    collision = private_root / LEGACY_EMPTY_INTENT_STAGE
                    collision.write_bytes(b"foreign")
                else:
                    token = "c" * 64
                    collision = private_root / f".empty-docx-state-{token}.quarantine"
                    collision.mkdir()
                with self.assertRaisesRegex(
                    PrivateDocxDomainError, "private DOCX corpus cleanup failed"
                ):
                    cleanup_private_docx_corpus(runtime_root=runtime_root)
                self.assertTrue(collision.exists())
                self.assertTrue(source.is_dir())

    def test_cleanup_rejects_and_preserves_legacy_intent_stage(self):
        private_root = self.runtime_root / "private-docx"
        source = private_root / "corpus"
        source.mkdir(parents=True)
        stage = private_root / LEGACY_EMPTY_INTENT_STAGE
        stage.write_bytes(b"legacy-owned-looking-stage\n")
        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus cleanup failed"
        ):
            cleanup_private_docx_corpus(runtime_root=self.runtime_root)
        self.assertEqual(b"legacy-owned-looking-stage\n", stage.read_bytes())
        self.assertTrue(source.is_dir())

    def test_prepare_rejects_fsynced_legacy_intent_stage(self):
        private_root = self.runtime_root / "private-docx"
        source = private_root / "corpus"
        source.mkdir(parents=True)
        source_identity = source.stat()
        parent = private_root.stat()
        token = "d" * 64
        intent = {
            "schema_version": LEGACY_EMPTY_INTENT_SCHEMA,
            "format": "docx",
            "policy": LEGACY_EMPTY_INTENT_POLICY,
            "source_name": "corpus",
            "source_device": source_identity.st_dev,
            "source_inode": source_identity.st_ino,
            "parent_device": parent.st_dev,
            "parent_inode": parent.st_ino,
            "token": token,
            "quarantine_name": f".empty-docx-state-{token}.quarantine",
        }
        stage = private_root / LEGACY_EMPTY_INTENT_STAGE
        stage.write_bytes(legacy_empty_intent_body(intent))

        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus preparation failed"
        ):
            prepare_private_docx_corpus(
                self.review,
                source_root=self.source_root,
                runtime_root=self.runtime_root,
            )
        self.assertEqual(legacy_empty_intent_body(intent), stage.read_bytes())
        self.assertTrue(source.is_dir())

    def test_cleanup_with_canonical_source_absent_is_idempotent(self):
        private_root = self.runtime_root / "private-docx"
        private_root.mkdir(parents=True)
        expected = {
            "corpus_id": DOCX_PRIVATE_CORPUS_ID,
            "removed": False,
            "verified_documents": 0,
        }
        self.assertEqual(
            expected, cleanup_private_docx_corpus(runtime_root=self.runtime_root)
        )
        self.assertEqual(
            expected, cleanup_private_docx_corpus(runtime_root=self.runtime_root)
        )
        self.assertEqual(
            {private_docx_domain_module._LOCK_NAME},
            {path.name for path in private_root.iterdir()},
        )

    def test_prepare_rejects_unresolved_legacy_empty_directory_quarantine(self):
        private_root = self.runtime_root / "private-docx"
        quarantine = private_root / f".empty-docx-state-{'9' * 64}.quarantine"
        quarantine.mkdir(parents=True)

        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus preparation failed"
        ):
            prepare_private_docx_corpus(
                self.review,
                source_root=self.source_root,
                runtime_root=self.runtime_root,
            )
        self.assertTrue(quarantine.is_dir())

    def test_cleanup_rejects_and_preserves_ambiguous_empty_runtime_state(self):
        cases = ("unknown-file", "symlink", "fifo", "nonempty-corpus", "cross-format", "both")
        for case in cases:
            with self.subTest(case=case):
                runtime_root = self.root / f"runtime-ambiguous-{case}"
                private_root = runtime_root / "private-docx"
                private_root.mkdir(parents=True)
                if case == "unknown-file":
                    marker = private_root / "foreign"
                    marker.write_bytes(b"foreign")
                elif case == "symlink":
                    target = runtime_root / "foreign-target"
                    target.write_bytes(b"foreign")
                    marker = private_root / "corpus"
                    marker.symlink_to(target)
                elif case == "fifo":
                    marker = private_root / "corpus"
                    os.mkfifo(marker)
                elif case == "nonempty-corpus":
                    marker = private_root / "corpus" / "foreign"
                    marker.parent.mkdir()
                    marker.write_bytes(b"foreign")
                elif case == "cross-format":
                    marker = private_root / "private-pdf"
                    marker.mkdir()
                else:
                    marker = private_root / "corpus"
                    marker.mkdir()
                    (private_root / ".corpus-staging").mkdir()

                with self.assertRaisesRegex(
                    PrivateDocxDomainError, "private DOCX corpus cleanup failed"
                ):
                    cleanup_private_docx_corpus(runtime_root=runtime_root)
                self.assertTrue(marker.exists() or marker.is_symlink())
    def test_load_rejects_manifest_drift_and_prepare_refuses_runtime_symlink(self):
        manifest = prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=self.runtime_root,
        )
        corpus_root = self.runtime_root / "private-docx" / "corpus"
        (corpus_root / "manifest.json").write_text(
            json.dumps({**manifest, "corpus_id": "other"}, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus manifest could not be loaded"
        ):
            load_private_docx_manifest(runtime_root=self.runtime_root)

        linked_runtime = self.root / "runtime-link"
        linked_runtime.symlink_to(self.runtime_root, target_is_directory=True)
        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus preparation failed"
        ):
            prepare_private_docx_corpus(
                self.review,
                source_root=self.source_root,
                runtime_root=linked_runtime,
            )

    def test_prepare_recovers_after_interrupted_canonical_publish(self):
        import ao_lore.private_docx_domain as private_docx_domain

        real_rename = private_docx_domain._rename_noreplace_at
        calls = {"count": 0}

        def interrupt_once(*args, **kwargs):
            calls["count"] += 1
            real_rename(*args, **kwargs)
            if calls["count"] == 1:
                raise KeyboardInterrupt

        with patch.object(private_docx_domain, "_rename_noreplace_at", side_effect=interrupt_once):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_corpus(
                    self.review,
                    source_root=self.source_root,
                    runtime_root=self.runtime_root,
                )

        manifest = prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(manifest, load_private_docx_manifest(runtime_root=self.runtime_root))

    def test_prepare_recovers_interrupted_staging_publish_and_cleans_stage(self):
        import ao_lore.private_docx_domain as private_docx_domain

        real_persist = private_docx_domain._persist_bytes_at
        interrupted = False

        def interrupt_after_first_write(descriptor, name, body):
            nonlocal interrupted
            real_persist(descriptor, name, body)
            if not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with patch.object(private_docx_domain, "_persist_bytes_at", side_effect=interrupt_after_first_write):
            with self.assertRaises(KeyboardInterrupt):
                prepare_private_docx_corpus(
                    self.review,
                    source_root=self.source_root,
                    runtime_root=self.runtime_root,
                )
        private_root = self.runtime_root / "private-docx"
        self.assertTrue((private_root / ".corpus-staging").exists())
        manifest = prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(manifest, load_private_docx_manifest(runtime_root=self.runtime_root))
        self.assertFalse((private_root / ".corpus-staging").exists())

    def test_prepare_preserves_foreign_staging_and_fails_closed(self):
        private_root = self.runtime_root / "private-docx"
        staging = private_root / ".corpus-staging"
        staging.mkdir(parents=True)
        foreign = staging / "foreign.bin"
        foreign.write_bytes(b"foreign")
        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus preparation failed"
        ):
            prepare_private_docx_corpus(
                self.review,
                source_root=self.source_root,
                runtime_root=self.runtime_root,
            )
        self.assertEqual(b"foreign", foreign.read_bytes())

    def test_prepare_recovers_owned_rollback_quarantine_after_directory_fsync_interrupt(self):
        import ao_lore.private_docx_domain as private_docx_domain

        real_fsync = os.fsync
        interrupted = False
        stage_directory_fsyncs = 0

        def interrupt_stage_fsync(descriptor):
            nonlocal interrupted, stage_directory_fsyncs
            result = real_fsync(descriptor)
            try:
                target = os.readlink(f"/proc/self/fd/{descriptor}")
            except OSError:
                target = ""
            if target.endswith("/.corpus-staging"):
                stage_directory_fsyncs += 1
                if stage_directory_fsyncs == 2 and not interrupted:
                    interrupted = True
                    raise OSError("injected stage fsync failure")
            return result

        with patch.object(private_docx_domain.os, "fsync", side_effect=interrupt_stage_fsync):
            with self.assertRaises(PrivateDocxDomainError):
                prepare_private_docx_corpus(
                    self.review,
                    source_root=self.source_root,
                    runtime_root=self.runtime_root,
                )
        rollback = (
            self.runtime_root
            / "private-docx"
            / ".corpus-staging"
            / private_docx_domain._rollback_name("docx-domain-01.docx")
        )
        self.assertTrue(rollback.exists())
        manifest = prepare_private_docx_corpus(
            self.review,
            source_root=self.source_root,
            runtime_root=self.runtime_root,
        )
        self.assertEqual(manifest, load_private_docx_manifest(runtime_root=self.runtime_root))
        private_root = self.runtime_root / "private-docx"
        self.assertEqual(
            sorted(path.name for path in private_root.iterdir()),
            [".private-docx.lock", "corpus"],
        )

    def test_prepare_rejects_foreign_rollback_quarantine_and_preserves_it(self):
        private_root = self.runtime_root / "private-docx"
        staging = private_root / ".corpus-staging"
        staging.mkdir(parents=True)
        foreign = staging / ".rollback-foreign.quarantine"
        foreign.write_bytes(b"foreign")
        with self.assertRaisesRegex(
            PrivateDocxDomainError, "private DOCX corpus preparation failed"
        ):
            prepare_private_docx_corpus(
                self.review,
                source_root=self.source_root,
                runtime_root=self.runtime_root,
            )
        self.assertEqual(b"foreign", foreign.read_bytes())


if __name__ == "__main__":
    unittest.main()
