import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ao_lore._strict_io import ContractError, strict_read_json


ROOT = Path(__file__).resolve().parents[1]


class StrictJsonReadTests(unittest.TestCase):
    def test_rejects_hardlinked_json_artifact(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            root = Path(temporary)
            target = root / "artifact.json"
            target.write_text(json.dumps({"value": "fixture"}) + "\n")
            os.link(target, root / "artifact-copy.json")

            with self.assertRaisesRegex(ContractError, "regular non-link"):
                strict_read_json(
                    target,
                    "artifact",
                    max_bytes=1024,
                    root=root,
                )

    def test_rejects_path_rebound_while_open_descriptor_is_read(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".ao-lore") as temporary:
            root = Path(temporary)
            target = root / "artifact.json"
            target.write_text(json.dumps({"value": "fixture"}) + "\n")
            replacement = root / "replacement.json"
            replacement.write_bytes(target.read_bytes())
            displaced = root / "displaced.json"
            original_read = os.read
            replaced = False

            def rebound(descriptor, maximum):
                nonlocal replaced
                body = original_read(descriptor, maximum)
                if not replaced:
                    replaced = True
                    target.rename(displaced)
                    replacement.rename(target)
                return body

            with patch("ao_lore._strict_io.os.read", side_effect=rebound):
                with self.assertRaisesRegex(ContractError, "artifact changed"):
                    strict_read_json(
                        target,
                        "artifact",
                        max_bytes=1024,
                        root=root,
                    )


if __name__ == "__main__":
    unittest.main()
