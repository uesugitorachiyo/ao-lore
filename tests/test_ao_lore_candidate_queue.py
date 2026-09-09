import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ao_lore.__main__ import main
from ao_lore.benchmark import canonical_digest
from ao_lore.candidate_queue import CandidateQueueError, list_candidates
from ao_lore.candidates import append_review, load_verified_candidate, persist_candidate


ROOT = Path(__file__).resolve().parents[1]


def _candidate(candidate_id, created_at):
    candidate = {
        "schema_version": "ao.lore.okf-candidate.v0.1",
        "candidate_id": candidate_id,
        "document_ir_digest": "sha256:" + "1" * 64,
        "concepts": [],
        "claim_mappings": [],
        "links": [],
        "contradiction_warnings": [],
        "canonical": False,
        "promotion_authority": False,
    }
    trace = {
        "adapter": "deterministic-distiller",
        "private_reasoning_persisted": False,
    }
    result = {
        "schema_version": "ao.lore.distillation-result.v0.1",
        "candidate": candidate,
        "candidate_digest": canonical_digest(candidate),
        "distillation_trace": trace,
    }
    provenance = {
        "schema_version": "ao.lore.candidate-provenance.v0.1",
        "candidate_id": candidate_id,
        "candidate_digest": result["candidate_digest"],
        "document_ir_digest": candidate["document_ir_digest"],
        "source_digest": "sha256:" + "2" * 64,
        "parser_id": "docling",
        "parser_version": "2.118.1",
        "parse_quality_report_digest": "sha256:" + "3" * 64,
        "parser_selection_report_digest": "sha256:" + "4" * 64,
        "distillation_trace_digest": canonical_digest(trace),
        "created_at": created_at,
    }
    return result, provenance


def _queue_report(*, status="unreviewed", limit=50, after=None):
    return {
        "schema_version": "ao.lore.candidate-queue-readback.v0.1",
        "requested_status": status,
        "limit": limit,
        "after": after,
        "returned_count": 1,
        "items": [
            {
                "schema_version": "ao.lore.candidate-queue-item.v0.1",
                "candidate_id": "candidate-cli-item",
                "candidate_digest": "sha256:" + "1" * 64,
                "provenance_digest": "sha256:" + "2" * 64,
                "created_at": "2026-08-09T14:00:00Z",
                "source_digest": "sha256:" + "3" * 64,
                "parser_id": "docling",
                "parser_version": "2.118.1",
                "review_status": "unreviewed",
                "verified_review_events": 0,
                "latest_event_digest": None,
                "canonical": False,
                "promotion_authority": False,
            }
        ],
        "next_after": None,
        "canonical": False,
        "promotion_authority": False,
    }


class CandidateQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "working" / "candidates")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def add_candidate(self, suffix, created_at, decision=None):
        candidate_id = f"candidate-{suffix}"
        result, provenance = _candidate(candidate_id, created_at)
        persist_candidate(result, provenance, candidate_root=self.root)
        if decision is not None:
            append_review(
                candidate_id,
                decision,
                "queue-reviewer",
                candidate_root=self.root,
                recorded_at=created_at,
            )
        return candidate_id

    def test_lists_verified_unreviewed_candidates_in_created_order(self):
        later = self.add_candidate("later", "2026-08-09T14:01:00Z")
        earlier = self.add_candidate("earlier", "2026-08-09T14:00:00Z")

        report = list_candidates(candidate_root=self.root)

        self.assertEqual(report["schema_version"], "ao.lore.candidate-queue-readback.v0.1")
        self.assertEqual(report["requested_status"], "unreviewed")
        self.assertEqual(report["limit"], 50)
        self.assertIsNone(report["after"])
        self.assertEqual([item["candidate_id"] for item in report["items"]], [earlier, later])
        self.assertEqual(report["returned_count"], 2)
        self.assertIsNone(report["next_after"])
        self.assertFalse(report["canonical"])
        self.assertFalse(report["promotion_authority"])

    def test_filters_accepted_rejected_and_all(self):
        unreviewed = self.add_candidate("unreviewed", "2026-08-09T14:00:00Z")
        accepted = self.add_candidate("accepted", "2026-08-09T14:01:00Z", "accept")
        rejected = self.add_candidate("rejected", "2026-08-09T14:02:00Z", "reject")

        self.assertEqual(
            [item["candidate_id"] for item in list_candidates(status="accepted", candidate_root=self.root)["items"]],
            [accepted],
        )
        self.assertEqual(
            [item["candidate_id"] for item in list_candidates(status="rejected", candidate_root=self.root)["items"]],
            [rejected],
        )
        self.assertEqual(
            [item["candidate_id"] for item in list_candidates(status="all", candidate_root=self.root)["items"]],
            [unreviewed, accepted, rejected],
        )

    def test_equal_timestamps_use_candidate_id_tie_break(self):
        second = self.add_candidate("beta", "2026-08-09T14:00:00Z")
        first = self.add_candidate("alpha", "2026-08-09T14:00:00Z")

        report = list_candidates(status="all", candidate_root=self.root)

        self.assertEqual([item["candidate_id"] for item in report["items"]], [first, second])

    def test_cursor_is_exclusive_and_must_exist_in_full_collection(self):
        first = self.add_candidate("first", "2026-08-09T14:00:00Z", "accept")
        second = self.add_candidate("second", "2026-08-09T14:01:00Z")

        report = list_candidates(status="all", after=first, candidate_root=self.root)

        self.assertEqual([item["candidate_id"] for item in report["items"]], [second])
        self.assertEqual(report["after"], first)
        with self.assertRaisesRegex(CandidateQueueError, "does not identify"):
            list_candidates(after="candidate-missing", candidate_root=self.root)

    def test_filtering_pages_advance_by_last_scanned_candidate(self):
        first = self.add_candidate("first", "2026-08-09T14:00:00Z")
        self.add_candidate("accepted", "2026-08-09T14:01:00Z", "accept")
        third = self.add_candidate("third", "2026-08-09T14:02:00Z")
        self.add_candidate("rejected", "2026-08-09T14:03:00Z", "reject")

        page_one = list_candidates(limit=1, candidate_root=self.root)
        page_two = list_candidates(limit=1, after=page_one["next_after"], candidate_root=self.root)
        page_three = list_candidates(limit=1, after=page_two["next_after"], candidate_root=self.root)

        self.assertEqual([item["candidate_id"] for item in page_one["items"]], [first])
        self.assertEqual(page_one["next_after"], first)
        self.assertEqual([item["candidate_id"] for item in page_two["items"]], [third])
        self.assertEqual(page_two["next_after"], third)
        self.assertEqual(page_three["items"], [])
        self.assertIsNone(page_three["next_after"])

    def test_limit_and_status_validation_fail_closed(self):
        self.assertEqual(list_candidates(limit=200, candidate_root=self.root)["limit"], 200)
        for value in (0, 201, True, 1.5, "1", None):
            with self.subTest(limit=value), self.assertRaises(CandidateQueueError):
                list_candidates(limit=value, candidate_root=self.root)
        for value in ("pending", "", None, []):
            with self.subTest(status=value), self.assertRaises(CandidateQueueError):
                list_candidates(status=value, candidate_root=self.root)

    def test_invalid_cursors_fail_closed(self):
        for value in ("not-a-candidate", "../candidate-bad", "Candidate-upper", True):
            with self.subTest(after=value), self.assertRaises(CandidateQueueError):
                list_candidates(after=value, candidate_root=self.root)

    def test_root_allows_only_regular_readme_and_candidate_directories(self):
        (self.root / "README.md").write_text("queue\n", encoding="utf-8")
        self.add_candidate("valid", "2026-08-09T14:00:00Z")
        self.assertEqual(list_candidates(candidate_root=self.root)["returned_count"], 1)

        (self.root / "unexpected.txt").write_text("no\n", encoding="utf-8")
        with self.assertRaisesRegex(CandidateQueueError, "unexpected entry"):
            list_candidates(candidate_root=self.root)

    def test_symlink_and_malformed_candidate_directory_fail_closed(self):
        target = self.root / "target"
        target.mkdir()
        link = self.root / "candidate-linked"
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(CandidateQueueError):
            list_candidates(candidate_root=self.root)
        link.unlink()
        target.rmdir()

        wrong_prefix = self.root / "notcandidate"
        wrong_prefix.mkdir()
        with self.assertRaisesRegex(CandidateQueueError, "candidate- prefix"):
            list_candidates(candidate_root=self.root)
        wrong_prefix.rmdir()

        (self.root / "candidate-UPPER").mkdir()
        with self.assertRaisesRegex(CandidateQueueError, "bounded lowercase identifier"):
            list_candidates(candidate_root=self.root)

    def test_corrupt_candidate_and_provenance_drift_fail_closed(self):
        candidate_id = self.add_candidate("corrupt", "2026-08-09T14:00:00Z")
        candidate_path = self.root / candidate_id / "candidate.json"
        original = candidate_path.read_text(encoding="utf-8")
        candidate_path.write_text("{}\n", encoding="utf-8")
        with self.assertRaisesRegex(CandidateQueueError, "failed verification"):
            list_candidates(candidate_root=self.root)
        candidate_path.write_text(original, encoding="utf-8")

        provenance_path = self.root / candidate_id / "provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["candidate_digest"] = "sha256:" + "f" * 64
        provenance_path.write_text(json.dumps(provenance) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(CandidateQueueError, "failed verification"):
            list_candidates(candidate_root=self.root)

    def test_review_drift_fails_closed(self):
        candidate_id = self.add_candidate(
            "review-drift", "2026-08-09T14:00:00Z", "accept"
        )
        event_path = next((self.root / candidate_id / "reviews").glob("*.json"))
        event = json.loads(event_path.read_text(encoding="utf-8"))
        event["event_digest"] = "sha256:" + "f" * 64
        event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")

        with self.assertRaisesRegex(CandidateQueueError, "failed verification"):
            list_candidates(candidate_root=self.root)

    def test_hard_candidate_count_fails_before_projection(self):
        self.add_candidate("one", "2026-08-09T14:00:00Z")
        self.add_candidate("two", "2026-08-09T14:01:00Z")

        with patch("ao_lore.candidate_queue.MAX_CANDIDATES", 1):
            with self.assertRaisesRegex(CandidateQueueError, "hard candidate limit"):
                list_candidates(candidate_root=self.root)

    def test_hard_limit_stops_iteration_at_first_excess_candidate(self):
        class FakeEntry:
            def __init__(self, name):
                self.name = name

            def stat(self, *, follow_symlinks):
                self.follow_symlinks = follow_symlinks
                return os.stat_result((stat.S_IFDIR, 0, 0, 0, 0, 0, 0, 0, 0, 0))

        class BoundedScandir:
            def __init__(self):
                self.entries = iter([FakeEntry("candidate-one"), FakeEntry("candidate-two")])
                self.calls = 0
                self.entered = False
                self.exited = False

            def __enter__(self):
                self.entered = True
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.exited = True
                return False

            def __iter__(self):
                return self

            def __next__(self):
                self.calls += 1
                if self.calls > 2:
                    raise AssertionError("candidate enumeration consumed past hard bound")
                return next(self.entries)

        scanner = BoundedScandir()
        with patch("ao_lore.candidate_queue.MAX_CANDIDATES", 1), patch(
            "ao_lore.candidate_queue.os.scandir", return_value=scanner
        ):
            with self.assertRaisesRegex(CandidateQueueError, "hard candidate limit"):
                list_candidates(candidate_root=self.root)
        self.assertEqual(scanner.calls, 2)
        self.assertTrue(scanner.entered)
        self.assertTrue(scanner.exited)

    def test_verification_error_does_not_disclose_filesystem_paths(self):
        candidate_id = self.add_candidate("missing-file", "2026-08-09T14:00:00Z")
        (self.root / candidate_id / "candidate.json").unlink()

        with self.assertRaises(CandidateQueueError) as raised:
            list_candidates(candidate_root=self.root)

        self.assertEqual(
            str(raised.exception),
            f"candidate {candidate_id} failed verification",
        )
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertNotIn(str(ROOT), str(raised.exception))
        self.assertIsNotNone(raised.exception.__cause__)

    def test_verified_loader_returns_detached_data_without_paths(self):
        candidate_id = self.add_candidate("detached", "2026-08-09T14:00:00Z")

        loaded = load_verified_candidate(candidate_id, candidate_root=self.root)
        loaded["candidate"]["concepts"].append({"concept_id": "mutated-copy"})
        reloaded = load_verified_candidate(candidate_id, candidate_root=self.root)

        self.assertEqual(set(reloaded), {"candidate", "provenance", "inspection"})
        self.assertEqual(reloaded["candidate"]["concepts"], [])
        self.assertNotIn("path", json.dumps(reloaded).lower())

    def test_verified_loader_uses_one_bound_candidate_snapshot(self):
        candidate_id = self.add_candidate("one-snapshot", "2026-08-09T14:00:00Z")
        from ao_lore import candidates

        original = candidates._load_bound_candidate
        with patch(
            "ao_lore.candidates._load_bound_candidate", wraps=original
        ) as load_bound:
            loaded = load_verified_candidate(candidate_id, candidate_root=self.root)

        self.assertEqual(load_bound.call_count, 1)
        self.assertEqual(loaded["candidate"]["candidate_id"], candidate_id)
        self.assertEqual(loaded["inspection"]["candidate_id"], candidate_id)


class CandidateQueueCliTests(unittest.TestCase):
    def setUp(self):
        self.corrupt_id = "candidate-cli-corrupt"
        self.corrupt_target = ROOT / "working" / "candidates" / self.corrupt_id
        shutil.rmtree(self.corrupt_target, ignore_errors=True)

    def tearDown(self):
        shutil.rmtree(self.corrupt_target, ignore_errors=True)

    def invoke(self, argv):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                status = main(argv)
            except SystemExit as exc:
                status = int(exc.code)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_json_defaults_to_pending_normalized_as_unreviewed(self):
        report = _queue_report()
        with patch("ao_lore.__main__.list_candidates", return_value=report) as listing:
            status, output, error = self.invoke(["candidate", "list", "--json"])

        self.assertEqual((status, error), (0, ""))
        self.assertEqual(json.loads(output), report)
        self.assertEqual(output, json.dumps(report, sort_keys=True) + "\n")
        listing.assert_called_once_with(status="unreviewed", limit=50, after=None)

    def test_status_limit_and_cursor_are_forwarded(self):
        calls = (
            (["--status", "pending"], "unreviewed", 50, None),
            (["--status", "accepted"], "accepted", 50, None),
            (["--status", "rejected"], "rejected", 50, None),
            (["--status", "all"], "all", 50, None),
            (["--limit", "7"], "unreviewed", 7, None),
            (["--after", "candidate-cli-item"], "unreviewed", 50, "candidate-cli-item"),
        )
        for arguments, expected_status, expected_limit, expected_after in calls:
            with self.subTest(arguments=arguments):
                report = _queue_report(
                    status=expected_status,
                    limit=expected_limit,
                    after=expected_after,
                )
                with patch("ao_lore.__main__.list_candidates", return_value=report) as listing:
                    status, output, error = self.invoke(
                        ["candidate", "list", *arguments, "--json"]
                    )
                self.assertEqual((status, error), (0, ""))
                self.assertEqual(json.loads(output), report)
                listing.assert_called_once_with(
                    status=expected_status,
                    limit=expected_limit,
                    after=expected_after,
                )

    def test_human_output_is_a_stable_projection_of_the_same_report(self):
        report = _queue_report()
        with patch("ao_lore.__main__.list_candidates", return_value=report):
            status, output, error = self.invoke(["candidate", "list"])

        self.assertEqual((status, error), (0, ""))
        self.assertEqual(
            output,
            "CANDIDATE_ID\tREVIEW_STATUS\tCREATED_AT\tPARSER\tREVIEWS\n"
            "candidate-cli-item\tunreviewed\t2026-08-09T14:00:00Z\t"
            "docling@2.118.1\t0\n",
        )
        for private_value in (
            report["items"][0]["source_digest"],
            report["items"][0]["candidate_digest"],
            report["items"][0]["provenance_digest"],
            "reviewer",
            "rationale",
        ):
            self.assertNotIn(private_value, output)

    def test_human_output_escapes_terminal_controls_but_json_preserves_values(self):
        report = _queue_report()
        parser_version = "2.118.1\nFORGED\tROW\r\x1b[31m\x7f"
        report["items"][0]["parser_version"] = parser_version

        with patch("ao_lore.__main__.list_candidates", return_value=report):
            human_status, human_output, human_error = self.invoke(["candidate", "list"])
        with patch("ao_lore.__main__.list_candidates", return_value=report):
            json_status, json_output, json_error = self.invoke(
                ["candidate", "list", "--json"]
            )

        self.assertEqual((human_status, human_error), (0, ""))
        self.assertEqual(len(human_output.splitlines()), 2)
        self.assertEqual(human_output.splitlines()[1].count("\t"), 4)
        self.assertNotIn("\r", human_output)
        self.assertNotIn("\x1b", human_output)
        self.assertNotIn("\x7f", human_output)
        self.assertIn(
            r"2.118.1\nFORGED\tROW\r\x1b[31m\x7f",
            human_output,
        )
        self.assertEqual((json_status, json_error), (0, ""))
        self.assertEqual(
            json.loads(json_output)["items"][0]["parser_version"],
            parser_version,
        )

    def test_corrupt_storage_rejection_is_public_safe(self):
        self.corrupt_target.mkdir()
        private_path = self.corrupt_target / "private-source-name.json"
        private_path.write_text("{}\n", encoding="utf-8")

        status, output, error = self.invoke(["candidate", "list", "--json"])

        self.assertEqual(status, 2)
        self.assertEqual(output, "")
        self.assertEqual(error, "ao-lore: candidate operation rejected\n")
        self.assertNotIn(str(ROOT), error)
        self.assertNotIn("private-source-name", error)

    def test_candidate_list_has_no_root_override(self):
        status, output, error = self.invoke(["candidate", "list", "--help"])

        self.assertEqual((status, error), (0, ""))
        self.assertNotIn("candidate-root", output)


if __name__ == "__main__":
    unittest.main()
