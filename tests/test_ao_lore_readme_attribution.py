from pathlib import Path
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ReadmeAttributionTests(unittest.TestCase):
    def test_credits_research_without_claiming_affiliation_or_endorsement(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        normalized = " ".join(readme.casefold().split())

        self.assertIn("https://arxiv.org/abs/2604.14572", readme)
        self.assertIn("independent implementation", normalized)
        self.assertNotIn("official implementation", normalized)
        self.assertNotIn("endorsed by", normalized)
        self.assertNotIn("affiliated with", normalized)


if __name__ == "__main__":
    unittest.main()
