import unittest

from core.symbols import SymbolMap
from exchanges.base import OrderQuantityTooSmall
from exchanges.hyperliquid_exchange import HyperliquidExchange


class FakeInfo:
    coin_to_asset = {"BTC": 0, "xyz:TSLA": 110000}
    asset_to_sz_decimals = {0: 5, 110000: 4}

    def user_state(self, address, dex=""):
        if dex == "xyz":
            return {"assetPositions": [{"position": {
                "coin": "xyz:TSLA", "szi": "-2", "entryPx": "200",
                "positionValue": "500", "unrealizedPnl": "10", "leverage": {"value": 5},
            }}]}
        return {"assetPositions": [{"position": {
            "coin": "BTC", "szi": "0.5", "entryPx": "100",
            "positionValue": "55", "unrealizedPnl": "5", "leverage": {"value": 10},
        }}]}

    def open_orders(self, address, dex=""):
        if dex:
            return []
        return [{"oid": 11, "coin": "BTC", "side": "A", "sz": "0.5", "limitPx": "90",
                 "isTrigger": True, "reduceOnly": True, "triggerPx": "90"}]

    def all_mids(self, dex=""):
        return {"BTC": "100", "xyz:TSLA": "250"}

    def meta(self, dex=""):
        return {"universe": []}


class FakeClient:
    def __init__(self, result=None):
        self.orders = []
        self.cancels = []
        self.leverage = []
        self.result = result or _filled(1, "0.12345", "100")

    def order(self, coin, is_buy, sz, px, order_type, reduce_only=False):
        self.orders.append({"coin": coin, "is_buy": is_buy, "sz": sz, "px": px,
                            "order_type": order_type, "reduce_only": reduce_only})
        return self.result

    def cancel(self, coin, oid):
        self.cancels.append((coin, oid))

    def update_leverage(self, leverage, coin, is_cross=True):
        self.leverage.append((leverage, coin))


def _filled(oid, total_sz, avg_px):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"oid": oid, "totalSz": total_sz, "avgPx": avg_px}}]}}}


def _resting(oid):
    return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}


def make_exchange(client=None, config=None):
    exchange = HyperliquidExchange(config or {})
    exchange.info = FakeInfo()
    exchange.client = client or FakeClient()
    exchange.address = "0xabc"
    exchange.dexes = ["", "xyz"]
    exchange._coins = {name.upper(): name for name in exchange.info.coin_to_asset}
    exchange._max_leverage = {"BTC": 40, "XYZ:TSLA": 10}
    exchange._leverage_set = {}
    exchange.symbol_map = SymbolMap()
    return exchange


class RoundingTests(unittest.TestCase):
    def test_size_uses_coin_sz_decimals(self):
        exchange = make_exchange()
        self.assertEqual(exchange._round_size("BTC", 0.1234567), 0.12346)
        self.assertEqual(exchange._round_size("xyz:TSLA", 1.23456), 1.2346)

    def test_price_uses_five_significant_figures(self):
        exchange = make_exchange()
        self.assertEqual(exchange._round_price("BTC", 123.456789), 123.5)
        self.assertEqual(exchange._round_price("xyz:TSLA", 250.123456), 250.12)


class PositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_hip3_positions_are_aggregated_and_canonicalized(self):
        exchange = make_exchange()

        rows = await exchange.fetch_positions()

        by_symbol = {row["symbol"]: row for row in rows}
        self.assertEqual(set(by_symbol), {"BTC", "xyz:TSLA"})
        self.assertEqual(by_symbol["xyz:TSLA"]["side"], "short")
        self.assertEqual(by_symbol["xyz:TSLA"]["contracts"], 2)
        self.assertEqual(by_symbol["xyz:TSLA"]["markPrice"], 250)

    async def test_position_size_is_signed(self):
        exchange = make_exchange()
        self.assertEqual(await exchange.position_size("BTC"), 0.5)
        self.assertEqual(await exchange.position_size("XYZ:TSLA"), -2)


class OrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_market_order_uses_ioc_limit_and_parses_fill(self):
        client = FakeClient(_filled(7, "0.1", "101"))
        exchange = make_exchange(client)

        order = await exchange.create_order("BTC", "buy", 0.1)

        call = client.orders[0]
        self.assertEqual(call["order_type"], {"limit": {"tif": "Ioc"}})
        self.assertFalse(call["reduce_only"])
        self.assertEqual(order.id, "7")
        self.assertEqual(order.status, "closed")
        self.assertEqual(order.amount, 0.1)
        self.assertEqual(order.price, 101.0)

    async def test_resting_limit_order_keeps_full_size(self):
        client = FakeClient(_resting(8))
        exchange = make_exchange(client)

        order = await exchange.create_order("BTC", "buy", 0.2, "limit", 90)

        self.assertEqual(order.status, "open")
        self.assertEqual(order.amount, 0.2)
        self.assertEqual(client.orders[0]["order_type"], {"limit": {"tif": "Gtc"}})

    async def test_minimum_order_error_is_accumulated(self):
        client = FakeClient({"status": "err", "response": "Order must have minimum value of $10"})
        exchange = make_exchange(client)

        with self.assertRaises(OrderQuantityTooSmall):
            await exchange.create_order("BTC", "buy", 0.001)

    async def test_leverage_is_sent_once_per_coin(self):
        client = FakeClient()
        exchange = make_exchange(client, {"leverage": 10})

        await exchange.create_order("BTC", "buy", 0.1)
        await exchange.create_order("BTC", "buy", 0.1)

        self.assertEqual(client.leverage, [(10, "BTC")])

    async def test_trigger_protection_is_reduce_only_stop(self):
        client = FakeClient(_resting(9))
        exchange = make_exchange(client)

        oid = await exchange.set_protection("BTC", "sell", 0.5, 90)

        self.assertEqual(oid, "9")
        call = client.orders[0]
        self.assertTrue(call["reduce_only"])
        self.assertEqual(call["order_type"]["trigger"]["triggerPx"], 90)
        self.assertTrue(call["order_type"]["trigger"]["isMarket"])


if __name__ == "__main__":
    unittest.main()
