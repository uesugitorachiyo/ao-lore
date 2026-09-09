import importlib.util
import json
import os
import hashlib
import tempfile
import unittest
import zipfile
from io import BytesIO, StringIO
from pathlib import Path
from unittest.mock import patch

from ao_lore.__main__ import main
from ao_lore import docx_benchmark as docx_benchmark_module
from ao_lore import docx_ooxml as docx_ooxml_module
from ao_lore.docx_benchmark import (
    derive_docx_tuning_decision,
    fixed_docx_benchmark_paths,
    load_fixed_docx_fixture_data,
    run_docx_benchmark,
)
from ao_lore.docx_ooxml import NativeDocxOoxmlAdapter, DocxLimits, docx_configuration_digest
from ao_lore.parsing import ParseOutput, ParsingError
from ao_lore.private_docx_domain import (
    DOCX_MIME,
    DOCX_PRIVATE_CORPUS_ID,
    DOCX_TRANSFORMATION_ID,
    ReviewedDocxSource,
    build_private_docx_expectation,
    validate_docx_expectation,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "ao_lore" / "docx"

_ATTACHED_TEMPLATE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/attachedTemplate"
)
_PACKAGE_REL = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/package"
)


def generator_module():
    path = FIXTURE_ROOT / "generate.py"
    spec = importlib.util.spec_from_file_location("ao_lore_docx_fixture_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def qualification_manifest(
    *,
    decision: str = "hold",
    fixture_corpus_digest: str = "sha256:" + "a" * 64,
    configuration_digest: str | None = None,
    result_digest: str | None = None,
    metrics: dict[str, object] | None = None,
    repeatability: dict[str, object] | None = None,
    unexpected_failures: list[str] | None = None,
    stable_failures: dict[str, object] | None = None,
) -> dict[str, object]:
    manifest = {
        "schema_version": "ao.lore.docx-benchmark-result.v0.1",
        "fixture_corpus_digest": fixture_corpus_digest,
        "parser_id": "native-docx-ooxml",
        "parser_version": "1.0.0",
        "parser_configuration_digest": configuration_digest or docx_configuration_digest(DocxLimits()),
        "document_ir_version": "ao.lore.document-ir.v0.1",
        "attempts_per_document": 2,
        "document_count": 100,
        "metrics": metrics
        or {
            "text_fidelity": 1.0,
            "structural_fidelity": 1.0,
            "source_location_fidelity": 1.0,
            "expected_outcome_accuracy": 1.0,
            "expected_rejection_count": 12,
            "accepted_document_count": 88,
        },
        "exclusions": [],
        "stable_failures": stable_failures
        or {"invalid_package": 1, "unsupported_active_content": 11},
        "repeatability": repeatability or {"runs": 2, "identical": True, "score": 1.0},
        "unexpected_failures": unexpected_failures or [],
        "decision": decision,
    }
    manifest["result_digest"] = result_digest or "sha256:" + hashlib.sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return manifest


def _rewrite_archive(body: bytes, replacements: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(BytesIO(body)) as source, zipfile.ZipFile(output, "w") as target:
        for info in sorted(source.infolist(), key=lambda item: item.filename):
            target.writestr(info, replacements.get(info.filename, source.read(info.filename)))
        for name, content in sorted(replacements.items()):
            if name not in source.namelist():
                info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                target.writestr(info, content)
    return output.getvalue()


def _append_relationship(body: bytes, rel_type: str, target: str, *, target_mode: str | None = None) -> bytes:
    with zipfile.ZipFile(BytesIO(body)) as archive:
        rels = archive.read("word/_rels/document.xml.rels")
    addition = (
        f'<Relationship Id="rId999" Type="{rel_type}" Target="{target}"'
        + (f' TargetMode="{target_mode}"' if target_mode else "")
        + "/>"
    ).encode("utf-8")
    replaced = rels.replace(b"</Relationships>", addition + b"</Relationships>")
    replacements = {"word/_rels/document.xml.rels": replaced}
    if rel_type == _PACKAGE_REL:
        replacements["word/embeddings/oleObject1.bin"] = b"PACKAGE"
    return _rewrite_archive(body, replacements)


class _NumericFloat(float):
    pass


class _NumericInt(int):
    pass


class DocxBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.generator = generator_module()
        cls.accepted = [
            (FIXTURE_ROOT / name).read_bytes()
            for name in (
                "minimal-paragraph.docx",
                "hierarchy-and-list.docx",
                "nested-table.docx",
                "links-and-notes.docx",
                "media-and-drawing.docx",
                "mixed-structure.docx",
            )
        ]
        base = cls.generator.build_docx_fixture(paragraphs=({"text": "Private reviewed DOCX"},))
        cls.rejected = [
            _append_relationship(base, _ATTACHED_TEMPLATE_REL, "https://example.invalid/template.dotm", target_mode="External"),
            _append_relationship(base, _PACKAGE_REL, "embeddings/oleObject1.bin"),
        ]

    def build_corpus(self) -> tuple[Path, dict[str, bytes], dict[str, object]]:
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "working")
        root = Path(temporary.name)
        derived_by_id: dict[str, bytes] = {}
        for index in range(100, 0, -1):
            if index >= 90:
                derived = self.rejected[(index - 90) % len(self.rejected)]
            elif index == 40:
                derived = _rewrite_archive(
                    self.accepted[(index - 1) % len(self.accepted)],
                    {"opaque.bin": b"synthetic-unowned-opaque-part"},
                )
            else:
                derived = self.accepted[(index - 1) % len(self.accepted)]
            source = b"\x00\x00\x00\x00" + derived[4:]
            (root / f"{index:04d}-reviewed.docx").write_bytes(source)
        expectation = build_private_docx_expectation(root)
        validated = validate_docx_expectation(expectation)
        for index, item in enumerate(validated.documents, 1):
            filename = f"{index:04d}-reviewed.docx"
            source = (root / filename).read_bytes()
            derived_by_id[item["item_id"]] = b"PK\x03\x04" + source[4:]
        self.addCleanup(temporary.cleanup)
        return root, derived_by_id, expectation

    def adapter(self) -> NativeDocxOoxmlAdapter:
        manifest = qualification_manifest()
        return NativeDocxOoxmlAdapter(
            manifest,
            expected_corpus_digest=manifest["fixture_corpus_digest"],
        )

    def test_fixed_paths_separate_qualification_inputs_from_uat_corpus(self):
        runtime = ROOT / ".ao-lore"
        paths = fixed_docx_benchmark_paths(runtime)
        self.assertEqual(
            runtime / "private-docx" / "qualification-inputs",
            paths["inputs"],
        )
        self.assertEqual(
            paths["inputs"] / "expectation.json",
            paths["expectation"],
        )
        self.assertEqual(paths["inputs"] / "corpus", paths["corpus"])
        self.assertEqual(
            runtime / "private-docx" / "qualification.json",
            paths["qualification"],
        )

    def test_expectation_builder_is_deterministic_and_classifies_exact_active_rejections(self):
        _, _, first = self.build_corpus()
        _, _, second = self.build_corpus()
        self.assertEqual(first, second)
        with patch("ao_lore.docx_ooxml.NativeDocxOoxmlAdapter", side_effect=AssertionError("must not be called")):
            _, _, third = self.build_corpus()
        items = third["items"]
        self.assertEqual([item["item_id"] for item in items], [f"docx-{index:04d}" for index in range(1, 101)])
        self.assertEqual(
            11,
            sum(item["expected_rejection"] == "active-content" for item in items),
        )
        self.assertEqual(
            1,
            sum(
                item["expected_outcome"] == "reject" and item["expected_rejection"] != "active-content"
                for item in items
            ),
        )

    def test_runner_produces_exact_hold_manifest_with_real_adapter(self):
        _, derived_by_id, expectation = self.build_corpus()
        result = run_docx_benchmark(
            self.adapter(),
            expectation,
            fixture_data=derived_by_id,
        )
        self.assertEqual(2, result["attempts_per_document"])
        self.assertEqual(100, result["document_count"])
        self.assertEqual("hold", result["decision"])
        self.assertEqual([], result["unexpected_failures"])
        self.assertEqual(
            {"invalid_package": 1, "unsupported_active_content": 11},
            result["stable_failures"],
        )
        self.assertEqual({"runs": 2, "identical": True, "score": 1.0}, result["repeatability"])
        self.assertEqual(1.0, result["metrics"]["expected_outcome_accuracy"])
        self.assertEqual(12, result["metrics"]["expected_rejection_count"])
        self.assertEqual(88, result["metrics"]["accepted_document_count"])

    def test_measurement_contract_attacks_fail_closed(self):
        _, derived_by_id, expectation = self.build_corpus()

        def no_call(_operation):
            return (object(), 0.0, 0)

        def double_call(operation):
            first = operation()
            operation()
            return (first, 0.0, 0)

        def replaced_output(operation):
            operation()
            return (ParseOutput({}, {}), 0.0, 0)

        def hostile_numeric(operation):
            result = operation()
            return (result, _NumericFloat(0.0), _NumericInt(0))

        for measurement in (no_call, double_call, replaced_output, hostile_numeric):
            with self.subTest(measurement=measurement):
                with self.assertRaises(ParsingError):
                    run_docx_benchmark(
                        self.adapter(),
                        expectation,
                        fixture_data=derived_by_id,
                        measurement=measurement,
                    )

    def test_unrelated_reject_fixture_failure_never_counts_as_expected_active_content(self):
        _, derived_by_id, expectation = self.build_corpus()
        adapter = self.adapter()
        rejected_ids = {
            item["item_id"]
            for item in expectation["items"]
            if item["expected_outcome"] == "reject"
        }

        class MisclassifyingAdapter:
            capability = adapter.capability

            def parse(self, source):
                if source["digest"] in {
                    item["derived_digest"]
                    for item in expectation["items"]
                    if item["item_id"] in rejected_ids
                }:
                    raise ParsingError("DOCX package is invalid")
                return adapter.parse(source)

        result = run_docx_benchmark(
            MisclassifyingAdapter(),
            expectation,
            fixture_data=derived_by_id,
        )
        self.assertEqual("investigate", result["decision"])
        self.assertEqual(
            {"invalid_package": 0, "unsupported_active_content": 0},
            result["stable_failures"],
        )
        self.assertNotEqual([], result["unexpected_failures"])

    def test_negative_expectations_cannot_become_accepts(self):
        _, derived_by_id, expectation = self.build_corpus()
        adapter = self.adapter()
        accepted = next(
            item
            for item in expectation["items"]
            if item["expected_outcome"] == "accept"
        )
        rejected_digests = {
            item["derived_digest"]
            for item in expectation["items"]
            if item["expected_outcome"] == "reject"
        }

        class AcceptingNegativeAdapter:
            capability = adapter.capability

            def parse(self, source):
                if source["digest"] not in rejected_digests:
                    return adapter.parse(source)
                template = adapter.parse(
                    {
                        "resource": f"private-docx/{accepted['item_id']}.docx",
                        "digest": accepted["derived_digest"],
                        "media_type": DOCX_MIME,
                        "data": derived_by_id[accepted["item_id"]],
                    }
                )
                document_ir = json.loads(json.dumps(template.document_ir))
                document_ir["document_id"] = source["digest"]
                document_ir["source"] = {
                    "resource": source["resource"],
                    "digest": source["digest"],
                    "media_type": source["media_type"],
                }
                return ParseOutput(
                    document_ir,
                    dict(template.quality_components),
                    tuple(template.critical_failures),
                )

        result = run_docx_benchmark(
            AcceptingNegativeAdapter(),
            expectation,
            fixture_data=derived_by_id,
        )
        self.assertEqual("investigate", result["decision"])
        self.assertEqual(
            {"invalid_package": 0, "unsupported_active_content": 0},
            result["stable_failures"],
        )
        self.assertTrue(
            all(
                f"{item['item_id']}:unexpected_rejection_outcome"
                in result["unexpected_failures"]
                for item in expectation["items"]
                if item["expected_outcome"] == "reject"
            )
        )

    def test_output_mutation_and_repeatability_drift_investigate(self):
        _, derived_by_id, expectation = self.build_corpus()
        adapter = self.adapter()

        class MutatingAdapter:
            capability = adapter.capability

            def __init__(self):
                self._calls = 0

            def parse(self, source):
                self._calls += 1
                output = adapter.parse(source)
                if self._calls % 2 == 0:
                    output.document_ir["blocks"][0]["text"] += " drift"
                return output

        result = run_docx_benchmark(
            MutatingAdapter(),
            expectation,
            fixture_data=derived_by_id,
        )
        self.assertEqual("investigate", result["decision"])
        self.assertIn("repeatability", json.dumps(result, sort_keys=True))

    def test_repeatable_metric_regression_yields_candidate_change_with_metric_identities_only(self):
        _, derived_by_id, expectation = self.build_corpus()
        adapter = self.adapter()

        class WeakTextAdapter:
            capability = adapter.capability

            def parse(self, source):
                output = adapter.parse(source)
                for block in output.document_ir["blocks"]:
                    if isinstance(block.get("text"), str):
                        block["text"] = ""
                return output

        result = run_docx_benchmark(
            WeakTextAdapter(),
            expectation,
            fixture_data=derived_by_id,
        )
        self.assertEqual("candidate_change", result["decision"])
        self.assertEqual(["text_fidelity"], result["unexpected_failures"])
        self.assertEqual("candidate_change", derive_docx_tuning_decision(result))

    def test_parser_only_text_mutation_is_detected_against_independent_expectation(self):
        _, derived_by_id, expectation = self.build_corpus()
        original_add_block = docx_ooxml_module._IrBuilder.add_block

        def drifted_add_block(builder, block_type, text, span, **kwargs):
            return original_add_block(
                builder,
                block_type,
                "repeatable parser drift" if text else text,
                span,
                **kwargs,
            )

        with patch.object(
            docx_ooxml_module._IrBuilder, "add_block", drifted_add_block
        ):
            result = run_docx_benchmark(
                self.adapter(), expectation, fixture_data=derived_by_id
            )

        self.assertEqual("candidate_change", result["decision"])
        self.assertEqual(["text_fidelity"], result["unexpected_failures"])
        self.assertLess(result["metrics"]["text_fidelity"], 1.0)

    def test_cli_uses_fixed_docx_inputs_and_writes_manifest(self):
        _, derived_by_id, expectation = self.build_corpus()
        runtime = tempfile.TemporaryDirectory(dir=ROOT / "working")
        runtime_root = Path(runtime.name)
        self.addCleanup(runtime.cleanup)
        private_root = runtime_root / "private-docx"
        inputs_root = private_root / "qualification-inputs"
        corpus_root = inputs_root / "corpus"
        corpus_root.mkdir(parents=True)
        (inputs_root / "expectation.json").write_text(json.dumps(expectation), encoding="utf-8")
        manifest = qualification_manifest()
        (private_root / "qualification.json").write_text(json.dumps(manifest), encoding="utf-8")
        for index, item_id in enumerate(sorted(derived_by_id), 1):
            (corpus_root / f"{index:04d}-reviewed.docx").write_bytes(
                b"\x00\x00\x00\x00" + derived_by_id[item_id][4:]
            )
        out = runtime_root / "docx-benchmark.json"
        stdout = StringIO()
        stderr = StringIO()
        with patch.dict(os.environ, {"AO_LORE_HOME": str(runtime_root)}), patch(
            "sys.stdout", stdout
        ), patch("sys.stderr", stderr):
            status = main(["benchmark", "docx", "--out", str(out)])
        self.assertEqual((status, stderr.getvalue()), (0, ""))
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual("hold", written["decision"])
        self.assertEqual(written["result_digest"], json.loads(stdout.getvalue())["result_digest"])

    def test_exact_execution_uses_one_product_preparation_and_consumes_manifest(self):
        source_root, _derived_by_id, expectation = self.build_corpus()
        validated = validate_docx_expectation(expectation)
        review = tuple(
            ReviewedDocxSource(
                item_id=document["item_id"],
                source_name=f"{index:04d}-reviewed.docx",
                source_digest=document["source_digest"],
                derived_digest=document["derived_digest"],
                source_bytes=document["source_bytes"],
                derived_bytes=document["derived_bytes"],
                transformation_id=DOCX_TRANSFORMATION_ID,
            )
            for index, document in enumerate(validated.documents, 1)
        )
        runtime = tempfile.TemporaryDirectory(dir=ROOT / "working")
        runtime_root = Path(runtime.name)
        self.addCleanup(runtime.cleanup)
        consumed = []

        def adapter_factory(inputs_manifest):
            consumed.append(json.loads(json.dumps(dict(inputs_manifest))))
            qualification = qualification_manifest(
                fixture_corpus_digest=inputs_manifest["expectation_digest"]
            )
            return NativeDocxOoxmlAdapter(
                qualification,
                expected_corpus_digest=inputs_manifest["expectation_digest"],
            )

        with patch.object(
            docx_benchmark_module,
            "prepare_private_docx_qualification_inputs",
            wraps=docx_benchmark_module.prepare_private_docx_qualification_inputs,
        ) as prepare, patch.object(
            Path,
            "write_bytes",
            side_effect=AssertionError("execution must not write inputs directly"),
        ), patch.object(
            Path,
            "write_text",
            side_effect=AssertionError("execution must not write inputs directly"),
        ):
            result = docx_benchmark_module.run_private_docx_qualification(
                adapter_factory,
                review,
                expectation,
                source_root=source_root,
                runtime_root=runtime_root,
            )

        self.assertEqual(1, prepare.call_count)
        self.assertEqual(1, len(consumed))
        self.assertEqual(consumed[0]["expectation_digest"], result["fixture_corpus_digest"])
        self.assertEqual("hold", result["decision"])

    def test_fixed_fixture_loader_rejects_symlink_hardlink_fifo_and_wrong_names(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "working") as name:
            root = Path(name)
            for index in range(1, 101):
                (root / f"{index:04d}-reviewed.docx").write_bytes(b"\x00\x00\x00\x00ABCD")
            load_fixed_docx_fixture_data(root)

            escaped = root / "0099-reviewed.docx"
            escaped.unlink()
            target = root / "outside.docx"
            target.write_bytes(b"\x00\x00\x00\x00ABCD")
            escaped.symlink_to(target.name)
            with self.assertRaises(ParsingError):
                load_fixed_docx_fixture_data(root)
            escaped.unlink()
            escaped.write_bytes(b"\x00\x00\x00\x00ABCD")

            hardlink = root / "0100-reviewed.docx"
            hardlink.unlink()
            os.link(root / "0001-reviewed.docx", hardlink)
            with self.assertRaises(ParsingError):
                load_fixed_docx_fixture_data(root)
            hardlink.unlink()
            hardlink.write_bytes(b"\x00\x00\x00\x00ABCD")

            fifo = root / "0050-reviewed.docx"
            fifo.unlink()
            os.mkfifo(fifo)
            with self.assertRaises(ParsingError):
                load_fixed_docx_fixture_data(root)
            fifo.unlink()

    def test_fixed_fixture_loader_rejects_root_symlink_and_drift(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "working") as name:
            root = Path(name)
            for index in range(1, 101):
                (root / f"{index:04d}-reviewed.docx").write_bytes(b"\x00\x00\x00\x00ABCD")
            link = root.parent / f"{root.name}-link"
            link.symlink_to(root, target_is_directory=True)
            try:
                with self.assertRaises(ParsingError):
                    load_fixed_docx_fixture_data(link)
            finally:
                link.unlink()

            wrong = root / "0099-bad.docx"
            (root / "0099-reviewed.docx").rename(wrong)
            with self.assertRaises(ParsingError):
                load_fixed_docx_fixture_data(root)
