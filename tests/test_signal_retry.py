import unittest

from core.engine import Engine
from core.models import Order, Position


class FlakyExchange:
    config = {'fixed_balance_usdt': 100}

    def __init__(self):
        self.live = {}
        self.orders = []
        self.fail_symbols = set()

    async def equity(self):
        return 100

    async def position_size(self, symbol):
        return self.live.get(symbol, 0.0)

    async def entry_quote(self, symbol, side):
        return 1

    async def create_order(self, symbol, side, amount, order_type, price, reduce_only):
        if symbol in self.fail_symbols:
            raise RuntimeError('rate limited')
        self.orders.append((symbol, side, amount))
        self.live[symbol] = self.live.get(symbol, 0.0) + (amount if side == 'buy' else -amount)
        return Order(str(len(self.orders)), symbol, side, amount, 1, 'closed', reduce_only)


def snapshot():
    return {'A': Position('A', 10, 1), 'B': Position('B', 5, 1)}


class SignalRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_symbol_keeps_old_baseline_and_retries(self):
        exchange = FlakyExchange()
        exchange.fail_symbols = {'A'}
        engine = Engine(exchange, {'L': {'total_margin': 100, 'coefficient': 1}})

        await engine.process('L', snapshot())
        self.assertEqual([order[0] for order in exchange.orders], ['B'])
        self.assertNotIn('A', engine.previous['L'])
        self.assertIn('B', engine.previous['L'])

        exchange.fail_symbols = set()
        await engine.process('L', snapshot())
        self.assertEqual([order[0] for order in exchange.orders], ['B', 'A'])
        self.assertIn('A', engine.previous['L'])

    async def test_failed_startup_symbol_keeps_startup_price_check(self):
        exchange = FlakyExchange()
        exchange.fail_symbols = {'A'}
        engine = Engine(exchange, {'L': {'total_margin': 100, 'coefficient': 1}})
        await engine.process('L', snapshot())

        # The retry is still a startup entry: an unfavorable market must skip
        # instead of chasing the leader.
        exchange.fail_symbols = set()

        async def unfavorable(symbol, side):
            return 2

        exchange.entry_quote = unfavorable
        await engine.process('L', snapshot())
        self.assertEqual([order[0] for order in exchange.orders], ['B'])
        self.assertIn('A', engine.previous['L'])


if __name__ == '__main__':
    unittest.main()
