#!/usr/bin/env python3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pay_path import classify_pay_path, classify_pay_path_batch  # noqa: E402


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

    def test_batch_classifies_mixed(self):
        r = classify_pay_path_batch(
            {
                "items": [
                    {
                        "title": "[BOUNTY $100] HOOK",
                        "body": "powered by Opire reward $100",
                        "comments": 2,
                        "repo": "claude-builders-bounty/claude-builders-bounty",
                        "url": "https://example.com/1",
                    },
                    {
                        "title": "noise",
                        "body": "This repo is using Opire - Everyone can add rewards /reward 100",
                        "comments": 0,
                        "repo": "probe-rs/probe-rs",
                        "url": "https://example.com/2",
                    },
                    {
                        "title": "Add feature X",
                        "body": "https://algora.io/bounty/xyz $250 USDC on merge",
                        "comments": 1,
                        "repo": "org/repo",
                        "url": "https://example.com/3",
                    },
                ]
            }
        )
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 3)
        self.assertGreaterEqual(r["counts"]["skip"], 1)
        self.assertGreaterEqual(r["counts"]["pursue"] + r["counts"]["consider"], 1)
        self.assertTrue(any(x.get("decision") == "pursue" for x in r["items"]))


if __name__ == "__main__":
    unittest.main()
