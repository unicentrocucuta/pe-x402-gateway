#!/usr/bin/env python3
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from server import _bounty_triage_payload  # noqa: E402


class TestBountyTriage(unittest.TestCase):
    def test_returns_ok_structure(self):
        r = _bounty_triage_payload({"limit": 5})
        self.assertTrue(r.get("ok"))
        self.assertIn("items", r)
        self.assertLessEqual(len(r["items"]), 5)
        self.assertIn("filters", r)

    def test_items_have_decision(self):
        r = _bounty_triage_payload({"limit": 10})
        for it in r.get("items") or []:
            self.assertIn(it.get("decision"), {
                "consider",
                "pursue",
                "skip",
                "skip_unfunded_or_noisy",
            })


if __name__ == "__main__":
    unittest.main()
