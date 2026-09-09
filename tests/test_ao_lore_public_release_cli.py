import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/prepare-sanitized-repository.py"


class PublicReleaseCliTests(unittest.TestCase):
    def test_cli_surface_is_fixed_and_contains_no_remote_options(self):
        self.assertTrue(SCRIPT.is_file())
        spec = importlib.util.spec_from_file_location("prepare_sanitized", SCRIPT)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        parser = module.parser()
        self.assertEqual(set(), parser.parse_args([]).__dict__.keys() - {"check"})
        for option in ("--source", "--destination", "--remote", "--url", "--force", "--author"):
            with self.assertRaises(SystemExit): parser.parse_args([option, "x"])


if __name__ == "__main__": unittest.main()
