import types
import unittest

from core.symbols import SymbolMap
from exchanges.ccxt_exchange import CcxtExchange


MARKETS = {
    "BTC/USDT:USDT": {"swap": True, "base": "BTC", "quote": "USDT"},
    "AMD/USDT:USDT": {"swap": True, "base": "AMD", "quote": "USDT"},
    "AMD/USDT": {"swap": False, "base": "AMD", "quote": "USDT"},
    "AMD/USDC:USDC": {"swap": True, "base": "AMD", "quote": "USDC"},
    "1000PEPE/USDT:USDT": {"swap": True, "base": "1000PEPE", "quote": "USDT"},
}


def exchange(mapping=None, markets=None):
    instance = CcxtExchange.__new__(CcxtExchange)
    instance.client = types.SimpleNamespace(markets=dict(MARKETS if markets is None else markets))
    instance.symbol_map = SymbolMap(mapping)
    return instance


class CcxtFetchOrderTests(unittest.IsolatedAsyncioTestCase):
    def instance(self, client):
        exchange = CcxtExchange.__new__(CcxtExchange)
        exchange.client = client
        exchange.symbol_map = SymbolMap()
        return exchange

    async def test_contract_fill_is_converted_to_base_units(self):
        class Client:
            markets = {
                "ESPORTS/USDT:USDT": {
                    "swap": True, "base": "ESPORTS", "quote": "USDT",
                    "contractSize": 10,
                },
            }

            async def fetch_order(self, order_id, symbol=None):
                self.called = (order_id, symbol)
                return {
                    "id": order_id, "status": "closed", "filled": 20.0,
                    "remaining": 0.0, "average": 0.99, "price": 1.0,
                    "side": "buy",
                }

        client = Client()
        order = await self.instance(client).fetch_order("123", "ESPORTS")

        self.assertEqual(client.called, ("123", "ESPORTS/USDT:USDT"))
        self.assertEqual(order["status"], "closed")
        self.assertEqual(order["filled"], 200.0)
        self.assertEqual(order["remaining"], 0.0)
        self.assertEqual(order["average"], 0.99)

    async def test_order_not_found_is_reported_as_none(self):
        class Client:
            markets = {
                "ESPORTS/USDT:USDT": {
                    "swap": True, "base": "ESPORTS", "quote": "USDT",
                    "contractSize": 1,
                },
            }

            async def fetch_order(self, order_id, symbol=None):
                raise RuntimeError('{"label":"ORDER_NOT_FOUND"}')

        self.assertIsNone(await self.instance(Client()).fetch_order("123", "ESPORTS"))


class CcxtSymbolTests(unittest.TestCase):
    def test_direct_venue_symbol(self):
        instance = exchange()
        self.assertEqual(instance._symbol("BTC/USDT:USDT"), "BTC/USDT:USDT")
        self.assertEqual(instance._symbol("BTC_USDT"), "BTC/USDT:USDT")
        # close_all_positions() passes venue symbols straight back in.
        self.assertEqual(instance._symbol("AMD/USDC:USDC"), "AMD/USDC:USDC")

    def test_unmapped_hip3_falls_back_to_base(self):
        self.assertEqual(exchange()._symbol("XYZ:AMD"), "AMD/USDT:USDT")

    def test_mapped_unified(self):
        instance = exchange({"XYZ:AMD": "AMD/USDT:USDT"})
        self.assertEqual(instance._symbol("XYZ:AMD"), "AMD/USDT:USDT")

    def test_mapped_native_name_with_underscore(self):
        instance = exchange({"XYZ:AMD": "AMD_USDT"})
        self.assertEqual(instance._symbol("XYZ:AMD"), "AMD/USDT:USDT")

    def test_mapped_bare_base(self):
        instance = exchange({"XYZ:AMD": "AMD"})
        self.assertEqual(instance._symbol("XYZ:AMD"), "AMD/USDT:USDT")

    def test_mapped_lowercase_unified(self):
        instance = exchange({"XYZ:AMD": "amd/usdt:usdt"})
        self.assertEqual(instance._symbol("XYZ:AMD"), "AMD/USDT:USDT")

    def test_mapped_spot_target_is_rejected(self):
        instance = exchange({"XYZ:AMD": "AMD/USDT"})
        with self.assertRaisesRegex(ValueError, "not a USDT perpetual"):
            instance._symbol("XYZ:AMD")

    def test_mapped_non_usdt_target_is_rejected(self):
        instance = exchange({"XYZ:AMD": "AMD/USDC:USDC"})
        with self.assertRaisesRegex(ValueError, "not a USDT perpetual"):
            instance._symbol("XYZ:AMD")

    def test_unknown_unmapped_symbol_mentions_symbol_map(self):
        with self.assertRaisesRegex(ValueError, "symbol_map"):
            exchange()._symbol("NOPE")

    def test_unknown_mapped_symbol_reports_target(self):
        instance = exchange({"NOPE": "NOPE/USDT:USDT"})
        with self.assertRaisesRegex(ValueError, "NOPE/USDT:USDT"):
            instance._symbol("NOPE")

    def test_validate_aggregates_errors_and_skips_skip_entries(self):
        instance = exchange({"A": "AAA", "B": "BBB", "C": "skip"})
        with self.assertRaises(ValueError) as caught:
            instance.validate_symbol_map()
        message = str(caught.exception)
        self.assertIn("AAA", message)
        self.assertIn("BBB", message)
        self.assertNotIn("C ->", message)

    def test_validate_accepts_valid_mapping(self):
        exchange({"XYZ:AMD": "AMD_USDT"}).validate_symbol_map()


if __name__ == "__main__":
    unittest.main()
