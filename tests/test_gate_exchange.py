import unittest

import ccxt.async_support as ccxt

from core.symbols import SymbolMap
from exchanges.base import OrderQuantityTooSmall
from exchanges.ccxt_exchange import CcxtExchange

BTC = "BTC/USDT:USDT"
BTC_MARKET = {
    "id": "BTC_USDT", "swap": True, "base": "BTC", "quote": "USDT",
    "contractSize": 0.0001,
    "limits": {"amount": {"min": 1}, "leverage": {"max": 100}},
}
PUMP = "PUMP/USDT:USDT"
PUMP_MARKET = {
    "id": "PUMP_USDT", "swap": True, "base": "PUMP", "quote": "USDT",
    "contractSize": 1000,
    "limits": {"amount": {"min": 1}, "leverage": {"max": 25}},
}


class FakeGate:
    """Minimal async ccxt.gate stand-in; records every signed call."""

    def __init__(self, markets=None, positions=None, open_orders=None,
                 price_orders=None, balance=None):
        self.markets = dict(markets or {BTC: BTC_MARKET, PUMP: PUMP_MARKET})
        self.positions = positions if positions is not None else []
        self.open_orders = open_orders or []
        self.price_orders = price_orders or []
        self.balance = balance or {"info": [{"currency": "USDT", "total": "100"}]}
        self.order_calls = []
        self.leverage_calls = []
        self.cancel_calls = []
        self.precision = {}

    def amount_to_precision(self, market, amount):
        if self.precision:
            return self.precision(market, amount)
        return str(amount)

    def price_to_precision(self, market, price):
        return str(price)

    async def set_leverage(self, value, market, params=None):
        self.leverage_calls.append((value, market, params))

    async def create_order(self, market, order_type, side, amount, price=None, params=None):
        self.order_calls.append((market, order_type, side, amount, price, params))
        if params and params.get("reduceOnly"):
            # A reduce-only market order flattens the position.
            self.positions = []
        return {"id": "123", "filled": amount, "amount": amount,
                "average": 1.0, "status": "closed"}

    async def fetch_positions(self, symbols=None):
        return list(self.positions)

    async def fetch_balance(self, params=None):
        return self.balance

    async def fetch_open_orders(self, *args, **kwargs):
        return list(self.open_orders)

    async def cancel_order(self, order_id, symbol=None, params=None):
        self.cancel_calls.append((order_id, symbol, params))

    async def privateFuturesGetSettlePriceOrders(self, params=None):
        return list(self.price_orders)

    async def privateFuturesDeleteSettlePriceOrdersOrderId(self, params=None):
        self.cancel_calls.append(("price", params.get("order_id"), None))


def make(client, config=None, mapping=None):
    exchange = CcxtExchange.__new__(CcxtExchange)
    exchange.config = config or {
        "exchange": "gate", "leverage": 40, "margin_mode": "crossed", "settle": "usdt"}
    exchange.name = "gate"
    exchange.symbol_map = SymbolMap(mapping)
    exchange.client = client
    exchange._markets_loaded = True
    exchange._leverage_set = {}
    exchange._leverage_max = {}
    exchange._ledger_since = None
    return exchange


class GateOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_quantity_is_converted_to_contracts_with_ioc(self):
        client = FakeGate()
        await make(client).create_order("PUMP", "buy", 3000)

        self.assertEqual(len(client.order_calls), 1)
        market, order_type, side, amount, price, params = client.order_calls[0]
        self.assertEqual((market, order_type, side, amount, price),
                         (PUMP, "market", "buy", 3.0, None))
        self.assertEqual(params, {"timeInForce": "IOC", "tif": "ioc"})

    async def test_leverage_is_clamped_and_cached(self):
        client = FakeGate()
        exchange = make(client)

        await exchange.create_order("PUMP", "buy", 1000)
        await exchange.create_order("PUMP", "buy", 1000)

        self.assertEqual(client.leverage_calls,
                         [(0, PUMP, {"cross_leverage_limit": 25})])
        self.assertEqual(len(client.order_calls), 2)

    async def test_reduce_only_order_skips_leverage_and_sets_flag(self):
        client = FakeGate()
        await make(client).create_order("BTC", "sell", 0.0002, reduce_only=True)

        self.assertEqual(client.leverage_calls, [])
        self.assertEqual(client.order_calls[0][5],
                         {"reduceOnly": True, "timeInForce": "IOC", "tif": "ioc"})

    async def test_too_small_quantity_is_mapped(self):
        client = FakeGate()
        client.precision = lambda market, amount: "0"
        with self.assertRaises(OrderQuantityTooSmall):
            await make(client).create_order("BTC", "buy", 1e-9)

    async def test_precision_error_is_mapped(self):
        client = FakeGate()

        def raise_invalid(market, amount):
            raise ccxt.InvalidOrder("amount is less than the minimum")

        client.precision = raise_invalid
        with self.assertRaises(OrderQuantityTooSmall):
            await make(client).create_order("BTC", "buy", 1)


class GateProtectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_protection_takes_contracts_and_is_reduce_only(self):
        client = FakeGate()
        order_id = await make(client).set_protection("BTC", "sell", 3, 65000)

        market, order_type, side, amount, price, params = client.order_calls[0]
        self.assertEqual((market, order_type, side, amount, price),
                         (BTC, "market", "sell", 3, None))
        self.assertEqual(params, {"triggerPrice": 65000.0, "reduceOnly": True})
        self.assertEqual(order_id, "123")

    async def test_protection_prefers_gate_string_id(self):
        class Client(FakeGate):
            async def create_order(self, *args, **kwargs):
                return {"id": "123", "info": {"id_string": "999"}}

        self.assertEqual(
            await make(Client()).set_protection("BTC", "sell", 3, 65000), "999")

    async def test_cancel_protection_uses_string_id_endpoint(self):
        client = FakeGate()
        await make(client).cancel_protection("999", "BTC")
        self.assertEqual(client.cancel_calls, [("price", "999", None)])


class GateAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_equity_includes_unrealised_pnl(self):
        client = FakeGate(balance={"info": [
            {"currency": "USDT", "total": "100", "unrealised_pnl": "5"}]})
        self.assertEqual(await make(client).equity(), 105.0)
        self.assertEqual(await make(client).wallet_balance(), 100.0)

    async def test_missing_usdt_row_is_zero_not_an_exception(self):
        client = FakeGate(balance={"info": [{"currency": "BTC", "total": "1"}]})
        self.assertEqual(await make(client).equity(), 0.0)
        self.assertEqual(await make(client).wallet_balance(), 0.0)

    async def test_position_size_is_signed_base_units(self):
        client = FakeGate(positions=[{
            "symbol": BTC, "contracts": 2, "contractSize": 0.0001, "side": "short"}])
        self.assertAlmostEqual(await make(client).position_size("BTC"), -0.0002)

    async def test_close_all_positions_cancels_then_closes_contracts(self):
        short = {"symbol": BTC, "contracts": 2, "contractSize": 0.0001, "side": "long"}
        client = FakeGate(positions=[short])
        exchange = make(client)

        await exchange.close_all_positions()

        market, order_type, side, amount, price, params = client.order_calls[0]
        self.assertEqual((market, order_type, side, amount),
                         (BTC, "market", "sell", 2.0))
        self.assertEqual(params, {"reduceOnly": True, "timeInForce": "IOC", "tif": "ioc"})

    async def test_has_open_positions(self):
        client = FakeGate(positions=[
            {"symbol": BTC, "contracts": 0, "side": "long"},
            {"symbol": PUMP, "contracts": 3, "side": "short"},
        ])
        self.assertTrue(await make(client).has_open_positions())
        self.assertFalse(await make(FakeGate()).has_open_positions())


if __name__ == "__main__":
    unittest.main()
