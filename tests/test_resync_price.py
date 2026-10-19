import unittest

from core.engine import Engine
from core.models import Order, Position


class QuoteExchange:
    config = {'fixed_balance_usdt': 100}

    def __init__(self, quote=1):
        self.quote = quote
        self.live = 0.0
        self.orders = []

    async def equity(self):
        return 100

    async def position_size(self, symbol):
        return self.live

    async def entry_quote(self, symbol, side):
        return self.quote

    async def create_order(self, symbol, side, amount, order_type, price, reduce_only):
        self.orders.append((symbol, side, amount))
        self.live += amount if side == 'buy' else -amount
        return Order(str(len(self.orders)), symbol, side, amount, self.quote, 'closed', reduce_only)


def long_snapshot(size=100):
    return {'S': Position('S', size, 1)}


class ResyncPriceCheckTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, exchange):
        return Engine(exchange, {'L': {'total_margin': 100, 'coefficient': 1}})

    async def test_resync_open_skips_unfavorable_market(self):
        exchange = QuoteExchange(quote=2)
        engine = self.engine(exchange)
        await engine.process('L', {})  # establish the baseline
        await engine.process('L', long_snapshot(), resync=True)
        self.assertEqual(exchange.orders, [])
        self.assertIn('S', engine.previous['L'])

    async def test_live_event_still_follows_at_market(self):
        exchange = QuoteExchange(quote=2)
        engine = self.engine(exchange)
        await engine.process('L', {})
        await engine.process('L', long_snapshot())
        self.assertEqual(exchange.orders, [('S', 'buy', 100)])

    async def test_resync_increase_skips_unfavorable_market(self):
        exchange = QuoteExchange(quote=1)
        engine = self.engine(exchange)
        await engine.process('L', {})
        await engine.process('L', long_snapshot(100), resync=True)
        self.assertEqual(exchange.orders, [('S', 'buy', 100)])

        exchange.quote = 2
        await engine.process('L', long_snapshot(150), resync=True)
        self.assertEqual(exchange.orders, [('S', 'buy', 100)])

    async def test_resync_open_follows_favorable_market(self):
        exchange = QuoteExchange(quote=0.9)
        engine = self.engine(exchange)
        await engine.process('L', {})
        await engine.process('L', long_snapshot(), resync=True)
        self.assertEqual(exchange.orders, [('S', 'buy', 100)])


if __name__ == '__main__':
    unittest.main()
