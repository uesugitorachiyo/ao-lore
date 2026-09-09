import json
import unittest

from jsonschema import Draft202012Validator

from ao_lore.ocr_benchmark import OcrBenchmarkError, run_ocr_benchmark
from ao_lore.ocr_fixture_oracle import load_ocr_fixture_corpus

from tests.test_ao_lore_ocr_fixtures import SPEC


DIGEST = "sha256:" + "d" * 64


class ScriptedOcrRunner:
    def __init__(self, mutations=None):
        self.mutations = mutations or {}
        self.calls = []

    def run(self, candidate_id, fixture, attempt):
        self.calls.append((candidate_id, fixture.fixture_id, attempt))
        mutation = self.mutations.get((candidate_id, fixture.fixture_id, attempt), self.mutations.get(candidate_id))
        if fixture.expected_outcome == "rejected":
            category = fixture.expected_failure
            if mutation == "unexpected-accept":
                return document_ir(fixture)
            raise OcrBenchmarkError(category)
        result = document_ir(fixture)
        if mutation == "text":
            result["blocks"][0]["text"] += " changed"
            offset = 0
            for block in result["blocks"]:
                block["source_span"] = {"start": offset, "end": offset + len(block["text"])}
                offset += len(block["text"]) + 1
        elif mutation == "order":
            result["blocks"] = list(reversed(result["blocks"]))
        elif mutation == "geometry":
            for point in result["blocks"][0]["location"]["polygon"]:
                point[0] += 100
        elif mutation == "page":
            result["blocks"][0]["location"]["page"] += 1
        elif mutation == "hallucination":
            result["blocks"].append({
                "id": "extra", "type": "paragraph", "text": "invented",
                "source_span": {"start": 0, "end": 8},
                "location": {"page": 1, "polygon": [[1, 1], [2, 1], [2, 2], [1, 2]], "confidence_millionths": 1},
            })
        return result


def document_ir(fixture):
    blocks = []
    offset = 0
    for page in fixture.pages:
        for line in page.lines:
            end = offset + len(line.text)
            blocks.append({
                "id": "ocr-%06d" % (len(blocks) + 1), "type": "paragraph",
                "text": line.text, "source_span": {"start": offset, "end": end},
                "location": {
                    "page": page.page_number,
                    "polygon": [list(point) for point in line.polygon],
                    "confidence_millionths": 1000000,
                },
            })
            offset = end + 1
    return {
        "schema_version": "ao.lore.document-ir.v0.1",
        "document_id": fixture.source_digest,
        "source": {"resource": fixture.file_name, "digest": fixture.source_digest, "media_type": fixture.media_type},
        "parser": {"parser_id": "paddle-ocr-english", "parser_version": "0.1.0", "configuration_digest": DIGEST},
        "blocks": blocks,
        "metadata": {"format": "ocr", "page_count": len(fixture.pages), "authority_advanced": False},
    }


class OcrBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_ocr_fixture_corpus(SPEC)

    def test_three_perfect_candidates_run_twice_and_emit_exact_hold(self):
        runner = ScriptedOcrRunner()
        result = run_ocr_benchmark(
            self.corpus, runner, runtime_digest=DIGEST,
            model_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64),
        )
        self.assertEqual(len(runner.calls), 120)
        self.assertEqual(result["decision"], "hold")
        self.assertEqual(result["selected_candidate_id"], "candidate-1")
        schema = json.loads((SPEC.parents[4] / "schemas" / "ao-lore" / "private-ocr-qualification-v0.1.schema.json").read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(result)
        repeated = run_ocr_benchmark(
            self.corpus, ScriptedOcrRunner(), runtime_digest=DIGEST,
            model_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64),
        )
        self.assertEqual(result, repeated)
        self.assertEqual(
            json.dumps(result, sort_keys=True, separators=(",", ":")),
            json.dumps(repeated, sort_keys=True, separators=(",", ":")),
        )
        self.assertFalse(any(result[field] for field in (
            "network_accessed", "provider_calls", "promotion", "publication",
            "release", "deployment", "authority_advanced",
        )))
        for candidate in result["candidates"]:
            self.assertTrue(candidate["all_hard_gates_pass"])
            self.assertTrue(candidate["repeatability_identical"])
            self.assertEqual(candidate["hallucinated_lines"], 0)
            for field in (
                "character_accuracy_millionths", "word_accuracy_millionths",
                "detection_precision_millionths", "detection_recall_millionths",
                "reading_order_pair_accuracy_millionths", "mean_polygon_iou_millionths",
                "page_coverage_millionths", "expected_outcome_accuracy_millionths",
            ):
                self.assertEqual(candidate[field], 1000000)

    def test_repeatable_weakness_yields_candidate_change_with_passing_candidate(self):
        runner = ScriptedOcrRunner({"candidate-1": "text"})
        result = run_ocr_benchmark(
            self.corpus, runner, runtime_digest=DIGEST,
            model_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64),
        )
        self.assertEqual(result["decision"], "candidate_change")
        self.assertEqual(result["selected_candidate_id"], "candidate-2")
        self.assertFalse(result["candidates"][0]["all_hard_gates_pass"])

    def test_instability_and_unexpected_outcome_investigate(self):
        accepted = next(item for item in self.corpus.fixtures if item.expected_outcome == "accepted")
        unstable = ScriptedOcrRunner({("candidate-1", accepted.fixture_id, 2): "text"})
        result = run_ocr_benchmark(
            self.corpus, unstable, runtime_digest=DIGEST,
            model_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64),
        )
        self.assertEqual(result["decision"], "investigate")
        self.assertFalse(result["candidates"][0]["repeatability_identical"])
        rejected = next(item for item in self.corpus.fixtures if item.expected_outcome == "rejected")
        unexpected = ScriptedOcrRunner({("candidate-1", rejected.fixture_id, 1): "unexpected-accept"})
        result = run_ocr_benchmark(
            self.corpus, unexpected, runtime_digest=DIGEST,
            model_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64),
        )
        self.assertEqual(result["decision"], "investigate")

    def test_repeatable_unexpected_outcome_is_candidate_change(self):
        result = run_ocr_benchmark(
            self.corpus,
            ScriptedOcrRunner({"candidate-3": "unexpected-accept"}),
            runtime_digest=DIGEST,
            model_digests=(
                "sha256:" + "1" * 64,
                "sha256:" + "2" * 64,
                "sha256:" + "3" * 64,
            ),
        )
        self.assertEqual(result["decision"], "candidate_change")
        self.assertEqual(result["selected_candidate_id"], "candidate-1")
        self.assertTrue(result["candidates"][2]["repeatability_identical"])
        self.assertFalse(result["candidates"][2]["all_hard_gates_pass"])

    def test_oracle_detects_parser_text_order_geometry_page_and_hallucination_mutations(self):
        for mutation in ("text", "order", "geometry", "page", "hallucination"):
            with self.subTest(mutation=mutation):
                result = run_ocr_benchmark(
                    self.corpus, ScriptedOcrRunner({"candidate-1": mutation}),
                    runtime_digest=DIGEST,
                    model_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64),
                )
                self.assertIn(result["decision"], {"candidate_change", "investigate"})
                self.assertFalse(result["candidates"][0]["all_hard_gates_pass"])

    def test_hostile_runner_output_fails_closed_without_subclass_laundering(self):
        class Hostile(dict):
            pass

        runner = ScriptedOcrRunner()
        runner.run = lambda candidate_id, fixture, attempt: Hostile(document_ir(fixture))
        with self.assertRaises(OcrBenchmarkError):
            run_ocr_benchmark(
                self.corpus, runner, runtime_digest=DIGEST,
                model_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64),
            )
        runner.run = lambda candidate_id, fixture, attempt: (_ for _ in ()).throw(OcrBenchmarkError("/private/source.png TOKEN=secret"))
        with self.assertRaisesRegex(OcrBenchmarkError, "^OCR benchmark adapter boundary failed$") as raised:
            run_ocr_benchmark(
                self.corpus, runner, runtime_digest=DIGEST,
                model_digests=("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64),
            )
        self.assertIsNone(raised.exception.__cause__)
        for runtime_digest, model_digests in (
            ("sha256:" + "X" * 64, ("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64)),
            (DIGEST, ("sha256:" + "1" * 64, "sha256:" + "1" * 64, "sha256:" + "3" * 64)),
        ):
            with self.assertRaises(OcrBenchmarkError):
                run_ocr_benchmark(self.corpus, ScriptedOcrRunner(), runtime_digest=runtime_digest, model_digests=model_digests)


if __name__ == "__main__":
    unittest.main()
