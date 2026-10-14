import tempfile
import unittest
from pathlib import Path

from core.engine import Engine
from core.models import Order, Position
from exchanges.base import OrderQuantityTooSmall
from state.store import StateStore


class LotExchange:
    config = {'fixed_balance_usdt': 100}

    def __init__(self):
        self.live = 0.0
        self.orders = []

    async def equity(self):
        return 100

    async def position_size(self, symbol):
        return self.live

    async def entry_quote(self, symbol, side):
        return 1

    async def create_order(self, symbol, side, amount, order_type, price, reduce_only):
        if amount < 0.01 - 1e-12:
            raise OrderQuantityTooSmall('minimum 0.01')
        self.orders.append((side, amount, reduce_only))
        self.live += amount if side == 'buy' else -amount
        return Order('1', symbol, side, amount, 1, 'closed', reduce_only)


def snapshot(size):
    return {'XYZ:DELL': Position('XYZ:DELL', size, 1)} if size else {}


class SmallOrderCarryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exchange = LotExchange()
        self.leaders = {'D': {'total_margin': 100, 'coefficient': 1}}

    async def test_small_increases_accumulate_until_orderable(self):
        engine = Engine(self.exchange, self.leaders)
        await engine.process('D', {})
        await engine.process('D', snapshot(0.004))
        await engine.process('D', snapshot(0.008))
        self.assertEqual(self.exchange.orders, [])
        await engine.process('D', snapshot(0.012))
        self.assertEqual(self.exchange.orders, [('buy', 0.012, False)])
        self.assertEqual(engine.small_order_carry, {})

    async def test_decrease_offsets_unfilled_increase(self):
        engine = Engine(self.exchange, self.leaders)
        await engine.process('D', {})
        await engine.process('D', snapshot(0.008))
        await engine.process('D', snapshot(0.003))
        self.assertAlmostEqual(engine.small_order_carry['D:XYZ:DELL']['amount'], 0.003)
        await engine.process('D', snapshot(0))
        self.assertEqual(engine.small_order_carry, {})
        self.assertEqual(self.exchange.orders, [])

    async def test_restart_uses_current_target_without_double_counting_carry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.json')
            engine = Engine(self.exchange, self.leaders, state_store=store)
            await engine.process('D', {})
            await engine.process('D', snapshot(0.004))
            restarted = Engine(self.exchange, self.leaders, state_store=store)
            await restarted.process('D', snapshot(0.004))
            await restarted.process('D', snapshot(0.008))
            await restarted.process('D', snapshot(0.012))
            self.assertEqual(self.exchange.orders, [('buy', 0.012, False)])
            self.assertEqual(store.load()['small_order_carry'], {})

    async def test_restart_discards_stale_carry_when_leader_is_flat(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.json')
            engine = Engine(self.exchange, self.leaders, state_store=store)
            await engine.process('D', {})
            await engine.process('D', snapshot(0.004))
            restarted = Engine(self.exchange, self.leaders, state_store=store)
            await restarted.process('D', {})
            self.assertEqual(store.load()['small_order_carry'], {})
            await restarted.process('D', snapshot(0.011))
            self.assertEqual(self.exchange.orders, [('buy', 0.011, False)])

    async def test_restart_discards_carry_when_account_meets_target(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / 'state.json')
            engine = Engine(self.exchange, self.leaders, state_store=store)
            await engine.process('D', {})
            await engine.process('D', snapshot(0.004))
            self.exchange.live = 0.004
            restarted = Engine(self.exchange, self.leaders, state_store=store)
            await restarted.process('D', snapshot(0.004))
            self.assertEqual(store.load()['small_order_carry'], {})
            await restarted.process('D', snapshot(0.014))
            self.assertEqual(self.exchange.orders, [('buy', 0.01, False)])


if __name__ == '__main__':
    unittest.main()
