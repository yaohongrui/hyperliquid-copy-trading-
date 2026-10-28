import unittest

from sources.routing import (
    DEFAULT_SOURCE,
    binance_leaders,
    hyperliquid_leaders,
    is_hyperliquid,
    leader_source,
)


class RoutingTests(unittest.TestCase):
    def test_defaults_to_hyperliquid(self):
        self.assertEqual(DEFAULT_SOURCE, "hyperliquid")
        self.assertEqual(leader_source({}), "hyperliquid")
        self.assertEqual(leader_source({"source": ""}), "hyperliquid")
        self.assertEqual(leader_source({"source": None}), "hyperliquid")
        self.assertTrue(is_hyperliquid({}))
        self.assertFalse(is_hyperliquid({"source": "binance_live"}))

    def test_custom_source_names_are_not_lowercased(self):
        self.assertEqual(leader_source({"source": "Binance_Live"}), "Binance_Live")

    def test_partition_preserves_disabled_leaders(self):
        leaders = {
            "hl": {"address": "0x1"},
            "bn": {"source": "binance_live", "enabled": False},
            "hl2": {"source": "hyperliquid", "enabled": False},
        }
        self.assertEqual(sorted(hyperliquid_leaders(leaders)), ["hl", "hl2"])
        self.assertEqual(sorted(binance_leaders(leaders)), ["bn"])


if __name__ == "__main__":
    unittest.main()
