import unittest

from core.engine import Engine
from core.models import Order, Position


class FixedBalanceExchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self):
        self.equity_calls = 0
        self.orders = []

    async def equity(self):
        self.equity_calls += 1
        return 100

    async def position_size(self, symbol):
        return 0

    async def entry_quote(self, symbol, side):
        return 1

    async def create_order(self, symbol, side, amount, order_type="market", price=None, reduce_only=False):
        self.orders.append((symbol, side, amount, reduce_only))
        return Order("1", symbol, side, amount, price, "closed", reduce_only)


class FixedBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_fixed_balance_skips_equity_rest_call(self):
        exchange = FixedBalanceExchange()
        engine = Engine(exchange, {"L": {"total_margin": 100, "coefficient": 1}})

        await engine.process("L", {})
        await engine.process("L", {"S": Position("S", 100, 1)})

        self.assertEqual(exchange.equity_calls, 0)
        self.assertEqual(exchange.orders, [("S", "buy", 100, False)])


class ProtectionExchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self):
        self.cancel_calls = []
        self.set_calls = []

    async def position_size(self, symbol):
        return 1

    async def fetch_position_detail(self, symbol):
        return {"contracts": 1, "contractSize": 1, "entryPrice": 100,
                "side": "long", "markPrice": 100}

    async def cancel_protection_orders(self, symbol, keep_id=None):
        self.cancel_calls.append((symbol, keep_id))

    async def set_protection(self, symbol, side, amount, trigger):
        self.set_calls.append((symbol, side, amount, trigger))
        return "p1"


class ProtectionChurnTests(unittest.IsolatedAsyncioTestCase):
    async def test_unchanged_position_does_not_relist_conditional_orders(self):
        exchange = ProtectionExchange()
        engine = Engine(exchange, {"L": {
            "total_margin": 100,
            "position_stop_loss": {"enabled": True, "max_loss_usdt": 10},
        }})
        engine.owners = {"__default__:BTC": "L"}

        await engine.reconcile_protections()
        await engine.reconcile_protections()

        self.assertEqual(exchange.set_calls, [("BTC", "sell", 1.0, 90.0)])
        # The first pass cleans stale conditional orders; the unchanged second
        # pass must not list them again.
        self.assertEqual(exchange.cancel_calls, [("BTC", None)])


if __name__ == "__main__":
    unittest.main()
