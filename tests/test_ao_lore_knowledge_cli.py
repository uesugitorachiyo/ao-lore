import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from ao_lore.__main__ import _parser, main


class KnowledgeCliTests(unittest.TestCase):
    def invoke(self, argv):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_parser_exposes_only_locked_knowledge_surface(self):
        parser = _parser()
        parser.parse_args(["knowledge", "status", "--json"])
        parser.parse_args(["knowledge", "search", "--query", "safe", "--json"])
        parser.parse_args(["knowledge", "search", "--query", "safe", "--limit", "200", "--json"])
        parser.parse_args(["knowledge", "answer", "--query", "safe", "--json"])
        forbidden = (
            "--request", "--stdin", "--root", "--brain-root", "--generations-root", "--index",
            "--model", "--provider", "--cache", "--network", "--clock", "--policy", "--budget",
            "--trust", "--force", "--concurrency", "--candidate-root",
        )
        for flag in forbidden:
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                parser.parse_args(["knowledge", "answer", "--query", "safe", "--json", flag, "x"])

    def test_json_dispatch_validates_then_writes_once(self):
        report = {
            "schema_version": "ao.lore.knowledge-status-readback.v0.1", "status": "completed",
            "snapshot_digest": "sha256:" + "a" * 64, "latest_generation_id": None,
            "latest_generation_manifest_digest": None, "generation_count": 0,
            "effective_entry_count": 0, "answerable_v0_3_entry_count": 0,
            "legacy_v0_2_entry_count": 0, "claim_count": 0, "answerability_status": "empty",
        }
        stream = StringIO()
        with patch("ao_lore.knowledge.knowledge_status", return_value=report), patch("sys.stdout.write", wraps=stream.write) as write, redirect_stderr(StringIO()):
            self.assertEqual(0, main(["knowledge", "status", "--json"]))
        self.assertEqual(1, write.call_count)
        self.assertEqual(report, json.loads(stream.getvalue()))

    def test_errors_are_fixed_content_free_and_process_control_propagates(self):
        secret = "PRIVATE QUERY /private/root"
        with patch("ao_lore.knowledge.answer_knowledge", side_effect=ValueError(secret)):
            code, out, err = self.invoke(["knowledge", "answer", "--query", secret, "--json"])
        self.assertEqual((2, "", "ao-lore: knowledge operation rejected\n"), (code, out, err))
        with patch("ao_lore.knowledge.answer_knowledge", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): main(["knowledge", "answer", "--query", "safe", "--json"])
        with patch("ao_lore.knowledge.answer_knowledge", side_effect=SystemExit(9)):
            with self.assertRaises(SystemExit): main(["knowledge", "answer", "--query", "safe", "--json"])

    def test_stdout_failure_is_redacted_and_reader_is_read_only(self):
        report = {
            "schema_version": "ao.lore.knowledge-status-readback.v0.1", "status": "completed",
            "snapshot_digest": "sha256:" + "a" * 64, "latest_generation_id": None,
            "latest_generation_manifest_digest": None, "generation_count": 0,
            "effective_entry_count": 0, "answerable_v0_3_entry_count": 0,
            "legacy_v0_2_entry_count": 0, "claim_count": 0, "answerability_status": "empty",
        }
        with patch("ao_lore.knowledge.knowledge_status", return_value=report), patch("sys.stdout.write", side_effect=OSError("private")), redirect_stderr(StringIO()) as err:
            self.assertEqual(2, main(["knowledge", "status", "--json"]))
        self.assertEqual("ao-lore: knowledge operation rejected\n", err.getvalue())


if __name__ == "__main__":
    unittest.main()
