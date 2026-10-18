import tempfile
import unittest
from pathlib import Path

from api.console import ConsoleStore
from core.engine import Engine
from core.models import Order, Position
from main import _seed_entered
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


class StopReentryTests(unittest.IsolatedAsyncioTestCase):
    def engine(self):
        return Engine(Exchange(live=0), {"L": {"total_margin": 100}})

    async def open_position(self, engine):
        await engine.process("L", {"S": Position("S", 100, 1)})
        self.assertEqual(engine.exchange.orders, [("S", "buy", 100, False)])

    async def test_native_stop_blocks_incremental_adds_until_leader_reopens(self):
        engine = self.engine()
        await self.open_position(engine)

        # The exchange-side stop fires: the account is flat, the leader is not.
        engine.exchange.live = 0
        await engine.process("L", {"S": Position("S", 110, 1)})
        await engine.process("L", {"S": Position("S", 120, 1)})
        self.assertEqual(engine.exchange.orders, [("S", "buy", 100, False)])
        self.assertIn("L:S", engine.stopped)

        # The leader goes flat: the stopped episode is over.
        await engine.process("L", {})
        self.assertNotIn("L:S", engine.stopped)

        # A fresh leader position is copied again.
        await engine.process("L", {"S": Position("S", 50, 1)})
        self.assertEqual(engine.exchange.orders[-1], ("S", "buy", 50, False))

    async def test_reconcile_marks_stop_and_releases_owner(self):
        engine = self.engine()
        await self.open_position(engine)
        self.assertIn("__default__:S", engine.owners)

        engine.exchange.live = 0
        await engine.reconcile_protections()
        self.assertIn("L:S", engine.stopped)
        self.assertNotIn("__default__:S", engine.owners)

        await engine.process("L", {"S": Position("S", 110, 1)})
        self.assertEqual(engine.exchange.orders, [("S", "buy", 100, False)])

    async def test_leader_close_then_reopen_is_not_suppressed(self):
        engine = self.engine()
        await self.open_position(engine)

        # The leader closes and our copy closes normally, so this is not a stop.
        await engine.process("L", {})
        self.assertNotIn("L:S", engine.stopped)
        self.assertEqual(engine.exchange.orders[-1], ("S", "sell", 100, True))

        await engine.process("L", {"S": Position("S", 50, 1)})
        self.assertEqual(engine.exchange.orders[-1], ("S", "buy", 50, False))

    async def test_restart_after_stop_does_not_reenter_on_first_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            exchange = Exchange(live=0)
            engine = Engine(exchange, {"L": {"total_margin": 100}}, state_store=store)
            await engine.process("L", {"S": Position("S", 100, 1)})
            self.assertEqual(exchange.orders, [("S", "buy", 100, False)])

            # The stop fires while the process is down; the leader still holds.
            exchange.live = 0
            restarted = Engine(exchange, {"L": {"total_margin": 100}}, state_store=store)
            await restarted.process("L", {"S": Position("S", 100, 1)})
            self.assertEqual(exchange.orders, [("S", "buy", 100, False)])
            self.assertIn("L:S", restarted.stopped)

    async def test_seed_entered_from_existing_local_fills(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ConsoleStore(Path(directory) / "console.sqlite3")
            store.insert_local_fills([{
                "account": "acct", "leader": "A", "order_id": "o1", "symbol": "XYZ:SKHX",
                "side": "buy", "quantity": 1, "price": 10, "reduce_only": False,
            }])
            engine = Engine(Exchange(live=0), {"A": {"total_margin": 100, "execution_account": "acct"}})
            _seed_entered(engine, store)
            self.assertTrue(engine.entered.get("A:XYZ:SKHX"))


if __name__ == "__main__":
    unittest.main()
