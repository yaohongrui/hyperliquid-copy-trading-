import unittest

from core.engine import Engine
from core.models import Order, Position
from core.symbols import SymbolMap
from exchanges.ccxt_exchange import CcxtExchange


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


class LeverageClient:
    def __init__(self):
        self.markets = {
            "BTC/USDT:USDT": {
                "swap": True, "quote": "USDT", "contractSize": 1,
                "limits": {"amount": {"min": 0}, "leverage": {"max": 50}},
            },
        }
        self.leverage_calls = []
        self.order_calls = 0

    def amount_to_precision(self, market, amount):
        return str(amount)

    async def set_leverage(self, value, market, params=None):
        self.leverage_calls.append((value, market, params))

    async def create_order(self, *args, **kwargs):
        self.order_calls += 1
        return {"id": "1", "filled": 1, "amount": 1, "average": 1, "status": "closed"}


class LeverageCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_leverage_is_set_once_per_symbol(self):
        exchange = CcxtExchange.__new__(CcxtExchange)
        exchange.client = LeverageClient()
        exchange.name = "binance"
        exchange.config = {"leverage": 10}
        exchange.symbol_map = SymbolMap()
        exchange._leverage_set = {}

        await exchange.create_order("BTC/USDT:USDT", "buy", 1)
        await exchange.create_order("BTC/USDT:USDT", "buy", 1)

        self.assertEqual(exchange.client.leverage_calls, [(10, "BTC/USDT:USDT", None)])
        self.assertEqual(exchange.client.order_calls, 2)


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
