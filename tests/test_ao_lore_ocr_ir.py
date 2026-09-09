import copy
import json
import unittest

from ao_lore.benchmark import canonical_digest
from ao_lore.ocr_contracts import OCR_POLICY_DIGEST
from ao_lore.ocr_ir import OcrIrError, canonicalize_ocr_result
from ao_lore.parsing import build_ocr_parse_output


DIGEST = "sha256:" + "1" * 64


def detection(index, text, polygon, confidence=900000):
    return {
        "index": index,
        "text": text,
        "confidence_millionths": confidence,
        "polygon": [list(point) for point in polygon],
    }


def worker_result(detections, *, width=500, height=500):
    return {
        "schema_version": "ao.lore.private-ocr-worker-result.v0.1",
        "policy_digest": OCR_POLICY_DIGEST,
        "run_id": "run-1",
        "candidate_id": "candidate-1",
        "runtime_digest": DIGEST,
        "model_set_digest": "sha256:" + "2" * 64,
        "configuration_digest": "sha256:" + "3" * 64,
        "latency_microseconds": 123456,
        "peak_gpu_memory_bytes": 987654321,
        "pages": [{
            "page_id": "page-0001",
            "width": width,
            "height": height,
            "detections": detections,
        }],
        "network_accessed": False,
        "provider_calls": False,
        "promotion": False,
        "publication": False,
        "release": False,
        "deployment": False,
        "authority_advanced": False,
    }


def source():
    return {
        "resource": "source-0123456789abcdef.pdf",
        "digest": "sha256:" + "a" * 64,
        "media_type": "application/pdf",
    }


class OcrIrTests(unittest.TestCase):
    def test_orders_columns_then_lines_and_preserves_exact_location(self):
        value = worker_result([
            detection(0, "right one", ((310, 10), (390, 10), (390, 30), (310, 30))),
            detection(1, "left two", ((10, 50), (90, 50), (90, 70), (10, 70))),
            detection(2, "left one", ((10, 10), (90, 10), (90, 30), (10, 30)), 987654),
        ])
        ir = canonicalize_ocr_result(value, source())
        self.assertEqual([block["text"] for block in ir["blocks"]], ["left one", "left two", "right one"])
        self.assertEqual(ir["blocks"][0]["location"], {
            "page": 1,
            "polygon": [[10, 10], [90, 10], [90, 30], [10, 30]],
            "confidence_millionths": 987654,
        })
        self.assertEqual([block["id"] for block in ir["blocks"]], ["ocr-000001", "ocr-000002", "ocr-000003"])
        self.assertEqual([block["source_span"] for block in ir["blocks"]], [
            {"start": 0, "end": 8}, {"start": 9, "end": 17}, {"start": 18, "end": 27},
        ])

    def test_normalizes_nfc_and_polygon_rotation_without_rewriting_text(self):
        value = worker_result([detection(0, "Cafe\u0301\r\nline\rtwo", ((90, 30), (10, 30), (10, 10), (90, 10)))])
        ir = canonicalize_ocr_result(value, source())
        block = ir["blocks"][0]
        self.assertEqual(block["text"], "Caf\u00e9\nline\ntwo")
        self.assertEqual(block["location"]["polygon"], [[10, 10], [90, 10], [90, 30], [10, 30]])

    def test_stable_line_ties_use_polygon_then_original_index(self):
        value = worker_result([
            detection(0, "second", ((100, 10), (180, 10), (180, 30), (100, 30))),
            detection(1, "first", ((10, 12), (110, 12), (110, 32), (10, 32))),
            detection(2, "overlap", ((20, 24), (80, 24), (80, 44), (20, 44))),
        ])
        first = canonicalize_ocr_result(value, source())
        second = canonicalize_ocr_result(copy.deepcopy(value), source())
        self.assertEqual([block["text"] for block in first["blocks"]], ["first second", "overlap"])
        self.assertEqual(canonical_digest(first), canonical_digest(second))
        self.assertEqual(
            json.dumps(first, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
            json.dumps(second, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
        )

    def test_merges_only_overlapping_fragments_on_the_same_visual_line(self):
        value = worker_result([
            detection(0, "LEFT", ((10, 10), (80, 10), (80, 30), (10, 30)), 990000),
            detection(1, "RIGHT", ((200, 10), (280, 10), (280, 30), (200, 30)), 980000),
            detection(2, "ONE", ((270, 9), (330, 9), (330, 31), (270, 31)), 970000),
            detection(3, "NEXT", ((200, 60), (280, 60), (280, 80), (200, 80)), 960000),
        ])
        ir = canonicalize_ocr_result(value, source())
        self.assertEqual([block["text"] for block in ir["blocks"]], ["LEFT", "RIGHT ONE", "NEXT"])
        self.assertEqual(
            ir["blocks"][1]["location"],
            {
                "page": 1,
                "polygon": [[200, 9], [330, 9], [330, 31], [200, 31]],
                "confidence_millionths": 970000,
            },
        )

    def test_rejects_duplicate_and_invalid_geometry(self):
        good = ((10, 10), (90, 10), (90, 30), (10, 30))
        invalid = (
            [detection(0, "one", good), detection(1, "two", good)],
            [detection(0, "cross", ((10, 10), (90, 30), (90, 10), (10, 30)))],
            [detection(0, "flat", ((10, 10), (90, 10), (90, 10), (10, 10)))],
            [detection(0, "reverse", tuple(reversed(good)))],
            [detection(0, "outside", ((10, 10), (501, 10), (501, 30), (10, 30)))],
        )
        for detections in invalid:
            with self.subTest(detections=detections), self.assertRaises(OcrIrError):
                canonicalize_ocr_result(worker_result(detections), source())

    def test_rejects_empty_normalized_text_hostile_containers_and_locator_source(self):
        with self.assertRaises(OcrIrError):
            canonicalize_ocr_result(worker_result([detection(0, "   ", ((10, 10), (90, 10), (90, 30), (10, 30)))]), source())
        with self.assertRaisesRegex(OcrIrError, "no readable content"):
            canonicalize_ocr_result(worker_result([
                detection(0, "●", ((10, 10), (90, 10), (90, 30), (10, 30))),
                detection(1, "—", ((10, 40), (90, 40), (90, 60), (10, 60))),
            ]), source())
        readable = canonicalize_ocr_result(worker_result([
            detection(0, "Value: 42!", ((10, 10), (90, 10), (90, 30), (10, 30))),
        ]), source())
        self.assertEqual(readable["blocks"][0]["text"], "Value: 42!")

        class HostileDict(dict):
            pass

        with self.assertRaises(OcrIrError):
            canonicalize_ocr_result(HostileDict(worker_result([])), source())
        private = source()
        private["resource"] = "/home/private/source.pdf"
        with self.assertRaises(OcrIrError):
            canonicalize_ocr_result(worker_result([detection(0, "safe", ((10, 10), (90, 10), (90, 30), (10, 30)))]), private)

    def test_rejects_noninteger_and_excessive_observations(self):
        value = worker_result([detection(0, "safe", ((10, 10), (90, 10), (90, 30), (10, 30)))])
        value["pages"][0]["detections"][0]["confidence_millionths"] = 0.5
        with self.assertRaises(OcrIrError):
            canonicalize_ocr_result(value, source())
        many = [
            detection(index, "x", ((index * 2, 0), (index * 2 + 1, 0), (index * 2 + 1, 1), (index * 2, 1)))
            for index in range(10001)
        ]
        with self.assertRaises(OcrIrError):
            canonicalize_ocr_result(worker_result(many, width=25000), source())

    def test_emits_no_private_or_worker_diagnostics(self):
        value = worker_result([detection(0, "Public text", ((10, 10), (90, 10), (90, 30), (10, 30)))])
        ir = canonicalize_ocr_result(value, source())
        body = json.dumps(ir, sort_keys=True)
        for forbidden in ("run-1", "candidate-1", "runtime_digest", "model_set_digest", "/home/", "http://", "https://"):
            self.assertNotIn(forbidden, body)
        self.assertEqual(ir["metadata"], {"format": "ocr", "page_count": 1, "authority_advanced": False})

    def test_parser_boundary_returns_canonical_parse_output(self):
        value = worker_result([detection(0, "Visible", ((10, 10), (90, 10), (90, 30), (10, 30)))])
        output = build_ocr_parse_output(source(), value)
        self.assertEqual(output.document_ir, canonicalize_ocr_result(value, source()))
        self.assertEqual(output.critical_failures, ())
        self.assertEqual(output.quality_components, {
            "text_coverage": 1.0,
            "structural_completeness": 1.0,
            "source_span_coverage": 1.0,
            "document_ir_valid": 1.0,
        })


if __name__ == "__main__":
    unittest.main()
