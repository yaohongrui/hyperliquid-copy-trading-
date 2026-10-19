import unittest

from core.engine import Engine
from core.models import Order, Position


class EquityExchange:
    config = {}

    def __init__(self, equity=100):
        self.equity_value = equity
        self.live = 0.0
        self.orders = []

    async def equity(self):
        return self.equity_value

    async def position_size(self, symbol):
        return self.live

    async def entry_quote(self, symbol, side):
        return 1

    async def create_order(self, symbol, side, amount, order_type, price, reduce_only):
        self.orders.append((symbol, side, amount))
        self.live += amount if side == 'buy' else -amount
        return Order(str(len(self.orders)), symbol, side, amount, 1, 'closed', reduce_only)


def long_snapshot(size):
    return {'S': Position('S', size, 1)} if size else {}


class ProportionalDecreaseTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, exchange):
        return Engine(exchange, {'L': {'total_margin': 100, 'coefficient': 1}})

    async def test_decrease_uses_leader_fraction_not_scaled_delta(self):
        exchange = EquityExchange(equity=100)
        engine = self.engine(exchange)
        await engine.process('L', long_snapshot(100))
        self.assertEqual(exchange.orders, [('S', 'buy', 100)])

        # Equity halves, so ratio is now 0.5.  The leader closes half of its
        # position; we must close half of ours (50), not 25% of the leader
        # delta scaled by the new ratio.
        exchange.equity_value = 50
        await engine.process('L', long_snapshot(50))
        self.assertEqual(exchange.orders[-1], ('S', 'sell', 50))
        self.assertAlmostEqual(exchange.live, 50)

    async def test_decrease_does_not_over_reduce_when_equity_grows(self):
        exchange = EquityExchange(equity=100)
        engine = self.engine(exchange)
        await engine.process('L', long_snapshot(100))

        exchange.equity_value = 200
        await engine.process('L', long_snapshot(50))
        self.assertEqual(exchange.orders[-1], ('S', 'sell', 50))
        self.assertAlmostEqual(exchange.live, 50)

    async def test_full_close_still_flattens_the_live_position(self):
        exchange = EquityExchange(equity=100)
        engine = self.engine(exchange)
        await engine.process('L', long_snapshot(100))

        exchange.equity_value = 50
        await engine.process('L', {})
        self.assertEqual(exchange.orders[-1], ('S', 'sell', 100))
        self.assertAlmostEqual(exchange.live, 0)


if __name__ == '__main__':
    unittest.main()
