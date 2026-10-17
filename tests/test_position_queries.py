import unittest

from core.symbols import SymbolMap
from sources.position_queries import _display, _symbol


class PositionSymbolTests(unittest.TestCase):
    def test_removes_hyperliquid_market_namespace(self):
        self.assertEqual(_symbol("XYZ:KORU"), "KORU")

    def test_removes_ccxt_quote_suffix(self):
        self.assertEqual(_symbol("KORU/USDT:USDT"), "KORU")


class PositionDisplayTests(unittest.TestCase):
    def test_maps_venue_symbol_to_hl_name(self):
        mapping = SymbolMap({"XYZ:AMD": "AMD/USDT:USDT"})
        self.assertEqual(_display("AMD/USDT:USDT", mapping), "XYZ:AMD")

    def test_preserves_exact_hl_namespace(self):
        mapping = SymbolMap({"XYZ:AMD": "AMD/USDT:USDT"})
        self.assertEqual(_display("XYZ:AMD", mapping), "XYZ:AMD")

    def test_without_map_uses_base(self):
        self.assertEqual(_display("KORU/USDT:USDT"), "KORU")

    def test_missing_value_is_placeholder(self):
        self.assertEqual(_display(None), "?")


if __name__ == "__main__":
    unittest.main()
