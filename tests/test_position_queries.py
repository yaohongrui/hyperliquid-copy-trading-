import unittest

from sources.position_queries import _symbol


class PositionSymbolTests(unittest.TestCase):
    def test_removes_hyperliquid_market_namespace(self):
        self.assertEqual(_symbol("XYZ:KORU"), "KORU")

    def test_removes_ccxt_quote_suffix(self):
        self.assertEqual(_symbol("KORU/USDT:USDT"), "KORU")


if __name__ == "__main__":
    unittest.main()
