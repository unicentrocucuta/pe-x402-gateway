#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ve import estimate_ve  # noqa: E402


class TestVE(unittest.TestCase):
    def test_escrow_positive(self):
        r = estimate_ve(
            {
                "reward_usd": 200,
                "hours": 2,
                "competitors": 1,
                "pay_type": "escrow",
                "hourly_cost_usd": 25,
            }
        )
        self.assertTrue(r["ok"])
        self.assertIn(r["decision"], {"pursue", "consider"})
        self.assertGreater(r["ve_net_usd"]["central"], 0)

    def test_contest_skip(self):
        r = estimate_ve(
            {
                "reward_usd": 1000,
                "hours": 4,
                "competitors": 50,
                "pay_type": "contest",
            }
        )
        self.assertEqual(r["decision"], "skip")
        self.assertLessEqual(r["p_pay"]["central"], 0.12)

    def test_high_opire_contention_skip(self):
        r = estimate_ve(
            {
                "reward_usd": 100,
                "hours": 1,
                "competitors": 80,
                "pay_type": "opire",
            }
        )
        self.assertEqual(r["decision"], "skip")


if __name__ == "__main__":
    unittest.main()
