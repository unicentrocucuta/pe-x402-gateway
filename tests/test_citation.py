#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from citation import score_claim  # noqa: E402


class TestCitation(unittest.TestCase):
    def test_supported(self):
        r = score_claim(
            "USDC transfers settle on Base in seconds",
            [
                {
                    "title": "Base docs",
                    "text": "USDC transfers on Base typically settle in seconds with low fees.",
                }
            ],
        )
        self.assertEqual(r["label"], "supported")
        self.assertGreaterEqual(r["support_score"], 0.35)

    def test_unsupported(self):
        r = score_claim(
            "The moon is made of green cheese and sells for one bitcoin",
            [{"title": "geology", "text": "Lunar regolith is mostly silicate rock and dust."}],
        )
        self.assertIn(r["label"], ("unsupported", "partial", "no_sources"))
        self.assertLess(r["support_score"], 0.55)

    def test_no_sources(self):
        r = score_claim("anything", [])
        self.assertEqual(r["label"], "no_sources")

    def test_empty_claim(self):
        r = score_claim("", [{"text": "hello world"}])
        self.assertEqual(r["label"], "invalid_claim")


if __name__ == "__main__":
    unittest.main()
