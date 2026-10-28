"""``combined_leader_positions`` must route leaders to the right report."""

import unittest
from unittest.mock import patch

from sources.position_queries import combined_leader_positions

HL_LEADER = {"source": "hyperliquid", "address": "0x" + "ab" * 20}
BN_LEADER = {"source": "binance", "portfolio_id": "123"}


class CombinedLeaderPositionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_hyperliquid_only_matches_leader_positions(self):
        leaders = {"HL": HL_LEADER}
        seen = []

        async def fake_leader_positions(configs):
            seen.append(configs)
            return "SENTINEL HL REPORT"

        with patch("sources.position_queries.leader_positions", fake_leader_positions):
            result = await combined_leader_positions(leaders)

        self.assertEqual(result, "SENTINEL HL REPORT")
        self.assertEqual(seen, [{"HL": HL_LEADER}])

    async def test_binance_only_uses_the_report_callable(self):
        leaders = {"BN": BN_LEADER}

        async def fake_leader_positions(configs):
            raise AssertionError("Hyperliquid report must not run for Binance-only leaders")

        with patch("sources.position_queries.leader_positions", fake_leader_positions):
            result = await combined_leader_positions(leaders, binance_report=lambda: "BN REPORT")

        self.assertEqual(result, "BN REPORT")

    async def test_mixed_output_puts_hyperliquid_first_with_blank_line(self):
        leaders = {"HL": HL_LEADER, "BN": BN_LEADER}
        seen = []

        async def fake_leader_positions(configs):
            seen.append(configs)
            return "HL REPORT"

        with patch("sources.position_queries.leader_positions", fake_leader_positions):
            result = await combined_leader_positions(leaders, binance_report=lambda: "BN REPORT")

        self.assertEqual(result, "HL REPORT\n\nBN REPORT")
        self.assertEqual(seen, [{"HL": HL_LEADER}])

    async def test_binance_without_report_callable_reports_unavailable(self):
        leaders = {"BN": BN_LEADER}
        result = await combined_leader_positions(leaders)
        self.assertEqual(result, "Binance leader positions unavailable")

    async def test_no_leaders_reports_not_configured(self):
        async def fake_leader_positions(configs):
            raise AssertionError("Hyperliquid report must not run with no leaders")

        def fake_report():
            raise AssertionError("Binance report must not run with no leaders")

        with patch("sources.position_queries.leader_positions", fake_leader_positions):
            result = await combined_leader_positions({}, binance_report=fake_report)

        self.assertEqual(result, "No leaders configured.")


if __name__ == "__main__":
    unittest.main()
