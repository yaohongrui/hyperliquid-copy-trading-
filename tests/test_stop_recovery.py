"""Stop-out suppression must only apply while the account is flat.

A stop (native stop order, liquidation or a manual close) suppresses later
leader adds until the leader goes flat.  Once the account holds the symbol
again -- manually reopened, or left with a residual -- following must resume.
"""

import tempfile
import unittest
from pathlib import Path

from core.engine import Engine
from core.models import Order, Position
from state.store import StateStore


class Exchange:
    config = {"fixed_balance_usdt": 100}

    def __init__(self, live=0):
        self.live = live
        self.orders = []

    async def equity(self):
        return 100

    async def position_size(self, symbol):
        return self.live

    async def entry_quote(self, symbol, side):
        return 1

    async def fetch_position_detail(self, symbol):
        return None

    async def create_order(self, symbol, side, amount, order_type, price, reduce_only):
        self.orders.append((symbol, side, amount, reduce_only))
        self.live += amount if side == 'buy' else -amount
        return Order("1", symbol, side, amount, 1, "closed", reduce_only)


class StopRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def engine(self, live=0):
        return Engine(Exchange(live=live), {"L": {"total_margin": 100}})

    async def open_position(self, engine):
        await engine.process("L", {"S": Position("S", 100, 1)})
        self.assertEqual(engine.exchange.orders, [("S", "buy", 100, False)])

    async def stop_out(self, engine):
        engine.exchange.live = 0
        await engine.process("L", {"S": Position("S", 110, 1)})
        self.assertIn("L:S", engine.stopped)

    async def test_increase_is_followed_after_the_account_is_reopened(self):
        engine = self.engine()
        await self.open_position(engine)
        await self.stop_out(engine)

        # The user restores the position manually; the next leader add must be
        # followed instead of skipped forever.
        engine.exchange.live = 100
        await engine.process("L", {"S": Position("S", 120, 1)})

        self.assertNotIn("L:S", engine.stopped)
        self.assertEqual(engine.exchange.orders[-1], ("S", "buy", 10, False))

    async def test_flat_account_still_suppresses_increases(self):
        engine = self.engine()
        await self.open_position(engine)
        await self.stop_out(engine)

        await engine.process("L", {"S": Position("S", 120, 1)})
        self.assertIn("L:S", engine.stopped)
        self.assertEqual(engine.exchange.orders, [("S", "buy", 100, False)])

    async def test_opposite_account_position_does_not_clear_the_stop(self):
        engine = self.engine()
        await self.open_position(engine)
        await self.stop_out(engine)

        engine.exchange.live = -100
        await engine.process("L", {"S": Position("S", 120, 1)})
        self.assertIn("L:S", engine.stopped)

    async def test_reconcile_recovers_a_manually_reopened_position(self):
        engine = self.engine()
        await self.open_position(engine)

        engine.exchange.live = 0
        await engine.reconcile_protections()
        self.assertIn("L:S", engine.stopped)
        self.assertNotIn("__default__:S", engine.owners)

        # Manual reopen happens without a leader signal; the five-second
        # reconciliation must reclaim ownership and drop the suppression.
        engine.exchange.live = 60
        await engine.reconcile_protections()
        self.assertNotIn("L:S", engine.stopped)
        self.assertIn("__default__:S", engine.owners)
        self.assertTrue(engine.entered.get("L:S"))


class OwnerMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = StateStore(Path(self._tmp.name) / "state.json")

    async def test_owner_key_follows_a_corrected_execution_account(self):
        self.store.save({
            "symbol_owners": {"gate_primary:BTC": "E"},
            "protection": {"gate_primary:BTC": {"id": "1", "contracts": 1, "trigger": 1}},
            "breakeven": {"gate_primary:BTC": True},
            "entered": {"E:BTC": True},
            "stopped": {"E:BTC": True},
        })

        engine = Engine(
            Exchange(),
            {"E": {"total_margin": 100, "execution_account": "binance_primary"}},
            state_store=self.store,
        )

        self.assertEqual(engine.owners, {"binance_primary:BTC": "E"})
        self.assertIn("binance_primary:BTC", engine.runtime_state["protection"])
        self.assertNotIn("gate_primary:BTC", engine.runtime_state["protection"])
        self.assertIn("binance_primary:BTC", engine.breakeven)
        self.assertNotIn("gate_primary:BTC", engine.breakeven)


if __name__ == "__main__":
    unittest.main()
