#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pay_path import classify_pay_path  # noqa: E402


class TestPayPath(unittest.TestCase):
    def test_unfunded_opire_footer(self):
        r = classify_pay_path(
            {
                "title": "RPC: queue access to the same probe",
                "body": (
                    "We should queue access.\n"
                    "This repo is using Opire - what does it mean?\n"
                    "Everyone can add rewards for this issue commenting /reward 100"
                ),
                "comments": 0,
                "repo": "probe-rs/probe-rs",
            }
        )
        self.assertEqual(r["decision"], "skip")
        self.assertEqual(r["pay_type"], "unfunded_opire_footer")

    def test_titled_bounty(self):
        r = classify_pay_path(
            {
                "title": "[BOUNTY $100] HOOK: Pre-tool-use hook",
                "body": "powered by Opire reward $100",
                "comments": 5,
                "repo": "claude-builders-bounty/claude-builders-bounty",
            }
        )
        self.assertIn(r["decision"], {"pursue", "consider"})
        self.assertEqual(r["amount_guess_usd"], 100.0)

    def test_illiquid_myz(self):
        r = classify_pay_path(
            {
                "title": "[BOUNTY B12] Dashboard",
                "body": "Reward in $MYZ token",
                "repo": "MyZubster-Ecosystem/MyZubsterGateway",
                "comments": 10,
            }
        )
        self.assertEqual(r["decision"], "skip")

    def test_zero_bounty_label(self):
        r = classify_pay_path(
            {
                "title": "[MCP] Add remote endpoint",
                "labels": ["bounty", "zero-bounty", "status: competition"],
                "repo": "Ikalus1988/MisakaNet",
                "comments": 1,
            }
        )
        self.assertEqual(r["decision"], "skip")

    def test_algora_marker(self):
        r = classify_pay_path(
            {
                "title": "Add feature X",
                "body": "Funded via https://algora.io/bounty/xyz $250 USDC on merge",
                "comments": 3,
                "repo": "someorg/somerepo",
            }
        )
        self.assertEqual(r["decision"], "pursue")
        self.assertEqual(r["pay_type"], "algora")


if __name__ == "__main__":
    unittest.main()
