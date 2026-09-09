import inspect
import unittest
from pathlib import Path

from ao_lore.docx_benchmark import _canonical_output
from ao_lore.docx_ooxml import (
    DOCX_PARSER_ID,
    DOCX_PARSER_VERSION,
    DocxLimits,
    NativeDocxOoxmlAdapter,
    validate_docx_package,
)
from ao_lore.docx_expectation_oracle import derive_docx_semantic_expectation
from ao_lore.docx_expectation_oracle import DocxExpectationOracleError
from ao_lore.parsing import ParsingError

from tests.test_ao_lore_docx_ooxml import (
    _VALID_HYPERLINK_RPR_LEAVES,
    _blip_variant,
    _drawing_variant,
    _formatting_only_run,
    _hyperlink_run,
    _hyperlink_variant,
    _invalid_hyperlink_presentation_cases,
    _invalid_pageref_cases,
    _pageref_hyperlink,
    _pageref_runs,
    _source,
    qualification_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ao_lore" / "docx"


class DocxExpectationOracleTests(unittest.TestCase):
    def adapter(self) -> NativeDocxOoxmlAdapter:
        digest = "sha256:" + "a" * 64
        return NativeDocxOoxmlAdapter(
            qualification_manifest(fixture_corpus_digest=digest),
            expected_corpus_digest=digest,
        )

    def test_oracle_matches_benchmark_visible_semantics_in_body_order(self):
        for path in sorted(FIXTURES.glob("*.docx")):
            with self.subTest(name=path.name):
                body = path.read_bytes()
                source = _source(body)
                oracle = derive_docx_semantic_expectation(
                    validate_docx_package(body), DocxLimits()
                )
                actual = _canonical_output(
                    self.adapter().parse(source),
                    source,
                    DOCX_PARSER_ID,
                    DOCX_PARSER_VERSION,
                )
                self.assertEqual(actual["counts"], oracle.counts)
                self.assertEqual(
                    actual["normalized_text_digest"], oracle.normalized_text_digest
                )
                self.assertEqual(
                    actual["structural_event_digest"],
                    oracle.structural_event_digest,
                )

    def test_oracle_implementation_does_not_use_parser_semantic_helpers(self):
        from ao_lore import docx_expectation_oracle

        source = inspect.getsource(docx_expectation_oracle)
        for forbidden in (
            "NativeDocxOoxmlAdapter",
            "_hyperlink_value",
            "_drawing_value",
            "_table_text_and_attributes",
            "_signals",
            "DocumentIR",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_oracle_and_parser_reject_hidden_hyperlink_and_diagram_content(self):
        cases = {
            "hyperlink-text-attribute": _hyperlink_variant(
                b'w:anchor="Bookmark"',
                b'<w:r><w:t w:unknown="hidden">Visible</w:t></w:r>',
            ),
            "diagram-hidden-word-text": _drawing_variant(
                "http://schemas.openxmlformats.org/drawingml/2006/diagram",
                b"<w:t>Hidden diagram text</w:t>",
            ),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                package = validate_docx_package(body)
                with self.assertRaises(DocxExpectationOracleError):
                    derive_docx_semantic_expectation(package, DocxLimits())
                with self.assertRaises(ParsingError):
                    self.adapter().parse(_source(body))

    def test_oracle_independently_accepts_discarded_hyperlink_presentation(self):
        attributes = (
            b'w:anchor="Bookmark" w:history="1" '
            b'w:tooltip="Reviewed tooltip" w:tgtFrame="_blank"'
        )
        for index, properties in enumerate(_VALID_HYPERLINK_RPR_LEAVES):
            with self.subTest(index=index):
                body = _hyperlink_variant(
                    attributes,
                    _hyperlink_run(
                        properties=properties,
                        attributes=b'w:rsidR="ABCDEF12" w:rsidRPr="1234ABCD"',
                    ),
                )
                oracle = derive_docx_semantic_expectation(
                    validate_docx_package(body), DocxLimits()
                )
                self.assertIn("Visible Link", oracle.text_tokens)
                serialized = repr(oracle)
                for discarded in (
                    "Reviewed tooltip",
                    "_blank",
                    "ABCDEF12",
                    "1234ABCD",
                    "Bookmark",
                ):
                    self.assertNotIn(discarded, serialized)

    def test_oracle_independently_rejects_malicious_hyperlink_presentation(self):
        for label, body in _invalid_hyperlink_presentation_cases().items():
            with self.subTest(label=label):
                package = validate_docx_package(body)
                with self.assertRaises(DocxExpectationOracleError):
                    derive_docx_semantic_expectation(package, DocxLimits())

    def test_oracle_independently_treats_formatting_only_runs_as_inert(self):
        formatting = _formatting_only_run()
        visible = _hyperlink_run(properties=b"", tokens=b"<w:t>Visible</w:t>")
        field = _pageref_runs("_Toc12345678")
        children_by_position = {
            "ordinary": formatting + visible + formatting,
            "before-begin": formatting + field,
            "after-begin": field.replace(b"</w:r>", b"</w:r>" + formatting, 1),
            "after-instruction": field.replace(
                b"</w:instrText></w:r>",
                b"</w:instrText></w:r>" + formatting,
                1,
            ),
            "after-separate": field.replace(
                b'<w:fldChar w:fldCharType="separate" /></w:r>',
                b'<w:fldChar w:fldCharType="separate" /></w:r>' + formatting,
                1,
            ),
            "before-end": field.replace(
                b'<w:r><w:fldChar w:fldCharType="end" /></w:r>',
                formatting + b'<w:r><w:fldChar w:fldCharType="end" /></w:r>',
                1,
            ),
            "after-end": field + formatting,
        }
        for position, children in children_by_position.items():
            with self.subTest(position=position):
                body = _hyperlink_variant(b'w:anchor="Bookmark"', children)
                oracle = derive_docx_semantic_expectation(
                    validate_docx_package(body), DocxLimits()
                )
                self.assertIn(
                    "Visible" if position == "ordinary" else "12",
                    oracle.text_tokens,
                )
                self.assertNotIn("ABCDEF12", repr(oracle))

    def test_oracle_formatting_only_runs_cannot_create_visibility_or_relax_grammar(self):
        cases = {
            "no-rpr": b"<w:r />",
            "empty-rpr": b"<w:r><w:rPr /></w:r>",
            "invalid-rpr": b"<w:r><w:rPr><w:unknown /></w:rPr></w:r>",
            "all-formatting": _formatting_only_run(),
            "field-result-formatting": _pageref_hyperlink(
                result=b"<w:rPr><w:b /></w:rPr>"
            ),
        }
        for label, body_or_children in cases.items():
            with self.subTest(label=label):
                body = (
                    body_or_children
                    if label == "field-result-formatting"
                    else _hyperlink_variant(b'w:anchor="Bookmark"', body_or_children)
                )
                package = validate_docx_package(body)
                with self.assertRaises(DocxExpectationOracleError):
                    derive_docx_semantic_expectation(package, DocxLimits())

    def test_oracle_independently_emits_only_pageref_visible_results(self):
        longest = "A" * 1012
        bodies = (
            _pageref_hyperlink(identifier="A"),
            _pageref_hyperlink(identifier=longest),
            _pageref_hyperlink(
                identifier=longest,
                instruction=" PAGEREF " + longest + " \\h",
                result=(
                    b"<w:t>Page</w:t><w:tab /><w:lastRenderedPageBreak />"
                    b"<w:t>12</w:t>"
                ),
            ),
        )
        for index, body in enumerate(bodies):
            with self.subTest(index=index):
                oracle = derive_docx_semantic_expectation(
                    validate_docx_package(body), DocxLimits()
                )
                self.assertIn("Page 12" if index == 2 else "12", oracle.text_tokens)
                serialized = repr(oracle)
                for discarded in ("PAGEREF", "\\h", "ReviewedBookmark", longest):
                    self.assertNotIn(discarded, serialized)

    def test_oracle_independently_rejects_invalid_pageref_state_and_grammar(self):
        for label, body in _invalid_pageref_cases().items():
            with self.subTest(label=label):
                package = validate_docx_package(body)
                with self.assertRaises(DocxExpectationOracleError):
                    derive_docx_semantic_expectation(package, DocxLimits())

    def test_oracle_independently_discards_print_cstate_for_all_blip_modes(self):
        bodies = (
            _blip_variant("embedded", b'r:embed="rIdMedia1" cstate="print"'),
            _blip_variant("external", b'r:link="rIdMedia1" cstate="print"'),
            _blip_variant(
                "dual",
                b'r:embed="rIdMedia1" r:link="rIdExternalImage" cstate="print"',
            ),
        )
        for index, body in enumerate(bodies):
            with self.subTest(index=index):
                oracle = derive_docx_semantic_expectation(
                    validate_docx_package(body), DocxLimits()
                )
                self.assertEqual(1, oracle.counts["drawings"])
                self.assertEqual(1, oracle.counts["media"])
                self.assertNotIn("cstate", repr(oracle))

    def test_oracle_independently_rejects_invalid_cstate_and_blip_attributes(self):
        cases = {
            "empty": b'r:embed="rIdMedia1" cstate=""',
            "case": b'r:embed="rIdMedia1" cstate="Print"',
            "other": b'r:embed="rIdMedia1" cstate="screen"',
            "drawing-namespaced": b'r:embed="rIdMedia1" a:cstate="print"',
            "relationship-namespaced": b'r:embed="rIdMedia1" r:cstate="print"',
            "unknown": b'r:embed="rIdMedia1" dpi="300"',
        }
        for label, attributes in cases.items():
            with self.subTest(label=label):
                package = validate_docx_package(
                    _blip_variant("embedded", attributes)
                )
                with self.assertRaises(DocxExpectationOracleError):
                    derive_docx_semantic_expectation(package, DocxLimits())


if __name__ == "__main__":
    unittest.main()
